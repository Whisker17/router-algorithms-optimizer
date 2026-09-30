"""R021-P05 (WHI-1551): the certified integer-allocation contract for the future
`direct_split_certified` (docs/references/research-021/integer-allocation.md).

This module is research evidence, not a strategy. It holds three things, kept apart:

1. **The executable specification** (`certify`): the best-first branch and bound of memo
   §5 over `direct_split`'s repository grid (or the raw-integer domain), on the actual
   bundle, `QuoteCache`, `direct_split.allocation_plan` and the unchanged evaluator. It is
   the reference WHI-1552 must reproduce; it is never registered, profiled or timed.
2. **An independent exhaustive oracle** (`oracle_values`): its own allocation enumeration
   (`itertools` compositions), its own amount rule and its own hand CPMM formula with the
   dust, zero-reserve, source/fee and `uint112` revert rules. It shares no code with
   `certify`, `direct_split` or `pools/`.
3. **Actual-plan replays**: `direct_split.solve`, `direct_split.allocation_plan` and
   `routing.evaluator.evaluate` on every oracle allocation and on committed fixtures.

`uv run python tests/routing/test_integer_allocation_contract.py probe <bundle> <out.json>`
and `... sweep <out.json>` are the bounded evidence passes of memo §9 (separate diagnostic
passes; no timing claim).
"""

from __future__ import annotations

import hashlib
import heapq
import importlib.util
import itertools
import json
import math
import random
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from functools import cache, cached_property
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from benchmark.objective import ObjectiveContext, gross_only, synthetic_fixed_cost
from pools.quote import QuoteLimitExceeded, metered_quotes, quote_exact_in
from pools.result import QuoteStatus
from routing.algorithms import direct_split
from routing.algorithms.base import AlgorithmConfig, Budget, SolveContext, SolveStatus
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import RoutePlan
from routing.search import QuoteCache
from snapshot.bundle import load_bundle
from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
R021 = REPO / "docs" / "references" / "research-021"
FIX = json.loads((R021 / "fixtures" / "integer-allocation.json").read_text(encoding="utf-8"))
RECON = json.loads((R021 / "fixtures" / "reconstructions.json").read_text(encoding="utf-8"))
MEMO = (R021 / "integer-allocation.md").read_text(encoding="utf-8")
ROUTING_FIXTURES = REPO / "tests" / "fixtures"
BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)
BASE_REVISION = "b9e7310ea73b2cfa4a3388b80644199e5188e1d9"

ALGORITHM = "direct_split_certified"
D = 10_000  # fee denominator (pools/constant_product.py FEE_DENOMINATOR)
Leg = tuple[int, int]  # (index into the admitted direct pools, units)


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


# ================================================================== option schema (memo §6)

RESERVED = (
    "max_hops",
    "max_splits",
    "percent_step",
    "chunks",
    "label_hops",
    "label_pruning",
    "time_limit_seconds",
    "max_quotes",
    "max_candidates",
    "shortlist",
    "sampling",
    "controls",
    "recipe",
    "objective",
    "seed",
)
DOMAINS = ("repository_grid", "raw_integer")
INT_RANGES = {
    "max_bound_nodes": (1, 10_000_000),
    "max_open_nodes": (1, 10_000_000),
    "raw_max_amount_in": (1, 1_000_000),
}


class OptionsError(ValueError):
    """An `algorithm_options.direct_split_certified` mapping outside memo §6."""


def validate_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """The factory's `options_validator` (R021-C/1 §9.2): the complete normalized options
    or an `OptionsError` naming the key. No defaults: the preset supplies every value."""
    for key in options:
        if key in RESERVED:
            raise OptionsError(f"{key}: reserved shared setting, not an algorithm option")
        if key != "domain" and key not in INT_RANGES:
            raise OptionsError(f"{key}: unknown option")
    domain = options.get("domain")
    if not isinstance(domain, str) or domain not in DOMAINS:
        raise OptionsError(f"domain: must be one of {DOMAINS}, got {domain!r}")
    raw = domain == "raw_integer"
    required = ["max_bound_nodes", "max_open_nodes", *(["raw_max_amount_in"] if raw else [])]
    for key in required:
        if key not in options:
            raise OptionsError(f"{key}: required")
    if not raw and "raw_max_amount_in" in options:
        raise OptionsError("raw_max_amount_in: only valid with domain raw_integer")
    for key, (lo, hi) in INT_RANGES.items():
        if key in options:
            value = options[key]
            if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
                raise OptionsError(f"{key}: integer in [{lo}, {hi}] required, got {value!r}")
    return {key: options[key] for key in sorted(options)}


def settings_sha256(options: Mapping[str, Any]) -> str:
    return _hash(validate_options(options))


# ================================================================== domain record (memo §2)


def domain_record(
    bundle_ref: str, cohort: str, pool_ids: Sequence[str], max_splits: int, step: int | None
) -> dict[str, Any]:
    """`r021.domain/1` of the case's direct pool set; `step` None = `raw_integer`."""
    grid: dict[str, Any] = (
        {"kind": "raw_integer", "percent_step": None, "remainder": "explicit_integer_legs"}
        if step is None
        else {
            "kind": "repository_grid",
            "percent_step": step,
            "remainder": "last_leg_all_remaining",
        }
    )
    return {
        "schema": "r021.domain/1",
        "universe": {"bundle": bundle_ref, "cohort": cohort, "pools": sorted(pool_ids)},
        "protocols": ["constant_product"],
        "pool_order": list(pool_ids),
        "hops": {"max": 1, "param": None},
        "splits": {"max": max_splits, "param": "search.max_splits", "governs": "allocation"},
        "amount_grid": grid,
        "zero_output_leg": "infeasible",
        "token_reuse": "simple_path",
        "pool_reuse": "disjoint",
        "dag_admission": "plan_token_dag",
        "full_fill": "v1_full_fill",
    }


# ================================================================== bounds (memo §4)


@dataclass(frozen=True)
class PoolView:
    """The continuous relaxation's data of one direct pool for the request direction:
    g(x) = keep*r_out*x / (r_in*D + keep*x), the CPMM output without the final floor."""

    r_in: int
    r_out: int
    keep: int

    @property
    def live(self) -> bool:
        return self.r_in > 0 and self.r_out > 0


def g(p: PoolView, x: int | Fraction) -> Fraction:
    return Fraction(p.keep * p.r_out * x) / (p.r_in * D + p.keep * x)


def dg(p: PoolView, x: int | Fraction) -> Fraction:
    return Fraction(p.keep * p.r_out * p.r_in * D) / (p.r_in * D + p.keep * x) ** 2


_SCALE = 1 << 64


def tangent_hint(pools: Sequence[PoolView], total: int) -> list[int]:
    """Rule T (memo §4.3): integer tangent points from the continuous water-filling
    x_i = max(0, mu*beta_i - alpha_i), alpha = D*r_in/keep, beta ~ sqrt(r_out*alpha)
    (integer square root at scale 2**64). A hint only: any t >= 0 gives a valid bound."""
    alpha = [Fraction(D * p.r_in, p.keep) for p in pools]
    beta = [Fraction(math.isqrt(p.r_out * D * p.r_in * _SCALE**2 // p.keep), _SCALE) for p in pools]
    order = sorted(range(len(pools)), key=lambda i: (alpha[i] / beta[i], i))
    sa, sb, active = Fraction(0), Fraction(0), list[int]()
    for i in order:
        mu = (total + sa + alpha[i]) / (sb + beta[i])
        if active and mu * beta[i] <= alpha[i]:
            break
        sa, sb = sa + alpha[i], sb + beta[i]
        active.append(i)
    mu = (total + sa) / sb
    hint = [0] * len(pools)
    for i in active:
        hint[i] = min(total, max(0, math.floor(mu * beta[i] - alpha[i])))
    return hint


def tangent_bound(pools: Sequence[PoolView], total: int, box: tuple[int, int] | None) -> Fraction:
    """Exact-rational upper bound of max sum g_i(x_i) over x >= 0, sum x = total (and
    x_0 in [lo, hi] when `box`): every concave g_i lies below its tangent at t_i, and a
    linear function over that polytope is maximal at a vertex (memo §4, lemmas B1-B3)."""
    t = tangent_hint(pools, total)
    if box is not None:
        t[0] = min(max(t[0], box[0]), box[1])
    s = [dg(p, x) for p, x in zip(pools, t, strict=True)]
    c = sum((g(p, x) - si * x for p, x, si in zip(pools, t, s, strict=True)), Fraction(0))
    if box is None:
        return c + max(s) * total
    rest = max(s[1:])
    return c + max(s[0] * x + rest * (total - x) for x in box)


def rounded_relaxation(
    views: Sequence[PoolView], live: Sequence[bool], amount: int, units: int, max_legs: int
) -> tuple[Leg, ...] | None:
    """Rule H (memo §5.2): the `max_legs` pools with the largest positive Rule-T hint
    (ties by admitted order), their units floor(t*units/amount) in admitted order, the
    last kept leg taking the remaining units (>= 1 because the hints sum to at most
    `amount`); legs that round to no unit or to a zero non-final amount are dropped.
    None when a single leg remains (the singles stage covers it)."""
    pools = [i for i in range(len(views)) if live[i]]
    if len(pools) < 2 or max_legs < 2:
        return None
    hint = dict(zip(pools, tangent_hint([views[i] for i in pools], amount), strict=True))
    positive = [i for i in pools if hint[i] > 0]
    kept = sorted(sorted(positive, key=lambda i: (-hint[i], i))[:max_legs])
    if len(kept) < 2:
        return None
    shares = [(i, hint[i] * units // amount) for i in kept]
    legs = [(i, u) for i, u in shares[:-1] if u > 0 and amount * u // units > 0]
    last = units - sum(u for _, u in legs)
    return None if not legs else (*legs, (kept[-1], last))


class Bounds:
    """The certified bound rules of memo §4.2. Every method gets only live pools and
    returns an integer upper bound (the objective is an integer) or None = unknown.
    Mutation tests subclass it; the specification uses `BOUNDS`."""

    def final(self, p: PoolView, amount: int) -> int | None:
        return math.floor(g(p, amount))

    def state(self, pools: Sequence[PoolView], total: int, legs_left: int) -> int | None:
        if legs_left == 1:
            return math.floor(max(g(p, total) for p in pools))
        return math.floor(tangent_bound(pools, total, None))

    def interval(
        self, p: PoolView, rest: Sequence[PoolView], total: int, lo: int, hi: int, legs_left: int
    ) -> int | None:
        if legs_left == 1:
            return max(math.floor(tangent_bound([p, q], total, (lo, hi))) for q in rest)
        return math.floor(tangent_bound([p, *rest], total, (lo, hi)))


BOUNDS = Bounds()


# ================================================================== the specification (memo §5)


@dataclass(frozen=True)
class Node:
    """A frontier node. `state`: the legs on pools < j are fixed (`legs`, `used` units,
    `fl` raw input, `gp` exact output); the rest is open and a final leg is still to come
    on a pool >= j. `interval`: additionally pool j is a non-final leg with units in
    [lo, hi]. `final`: pool j is the final leg with every remaining unit."""

    kind: str
    j: int
    used: int
    fl: int
    gp: int
    legs: tuple[Leg, ...]
    ub: int | None
    lo: int = 0
    hi: int = 0


@dataclass
class Certified:
    status: SolveStatus
    plan: RoutePlan | None
    evaluation: Evaluation | None
    score: int | None
    legs: tuple[Leg, ...] | None
    record: dict[str, Any]
    stats: dict[str, Any]
    open_nodes: list[Node] = field(default_factory=list)
    trace: list[Node] = field(default_factory=list)
    units: int = 0


NO_BUDGET = Budget()


class _Stop(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def certify(
    bundle: SnapshotBundle,
    case: Case,
    objective: ObjectiveContext,
    *,
    max_splits: int,
    percent_step: int,
    options: Mapping[str, Any],
    budget: Budget = NO_BUDGET,
    git_revision: str = BASE_REVISION,
    cohort: str = "fixture",
    bounds: Bounds = BOUNDS,
    sink: Callable[[RoutePlan], None] | None = None,
) -> Certified:
    """The normative `direct_split_certified` solve (memo §5). `max_splits`/`percent_step`
    are the profile's `search.*` values; `options` the validated algorithm options."""
    opts = validate_options(options)
    raw = opts["domain"] == "raw_integer"
    token_in, amount_in = case.token_in, case.amount_in
    states = bundle.pools_for_pair(token_in, case.token_out)
    ids = tuple(p.pool_id for p in states)
    domain = domain_record(
        bundle.bundle_hash, cohort, ids, max_splits, None if raw else percent_step
    )
    work = dict.fromkeys(
        (
            "quotes_executed",
            "quotes_memoized",
            "internal_evaluations",
            "bb_nodes_expanded",
            "bound_evaluations",
            "peak_open_nodes",
        ),
        0,
    )
    record: dict[str, Any] = {
        "schema": "r021.diagnostics/1",
        "contract": "R021-C/1",
        "algorithm": ALGORITHM,
        "domain": domain,
        "candidate_domain_hash": _hash(domain),
        "certificate": None,
        "certificate_unavailable_reason": "not_produced",
        "max_candidates_unit": "finalist_plans_evaluated",
        "work": work,
        "scope": {"supported": True, "reason": None},
    }
    stats: dict[str, Any] = {"truncated_by": None, "nodes_pruned_bound": 0}

    def stop_early(status: SolveStatus, reason: str | None) -> Certified:
        if reason is not None:
            record["scope"] = {"supported": False, "reason": reason}
        return Certified(status, None, None, None, None, record, stats)

    if objective.mode != "gross_only":
        return stop_early(SolveStatus.UNSUPPORTED, "objective_not_gross_only")
    if any(not isinstance(p, ConstantProductPoolState) for p in states):
        return stop_early(SolveStatus.UNSUPPORTED, "non_constant_product_direct_pool")
    if not states:
        return stop_early(SolveStatus.NO_ROUTE, None)
    if raw and amount_in > opts["raw_max_amount_in"]:
        return stop_early(SolveStatus.UNSUPPORTED, "raw_integer_amount_above_limit")

    cps = [p for p in states if isinstance(p, ConstantProductPoolState)]
    n, units = len(cps), (amount_in if raw else 100 // percent_step)
    max_legs = min(max_splits, n)
    views = [PoolView(*p.reserves_for(token_in), D - p.fee_bps) for p in cps]
    live = [v.live for v in views]
    cache = QuoteCache(bundle)
    outputs: dict[tuple[int, int], int | None] = {}
    best: list[tuple[int, RoutePlan, Evaluation, tuple[Leg, ...]]] = []
    heap: list[tuple[tuple[int, int, int], Node]] = []
    trace: list[Node] = []
    seq = itertools.count()
    mismatches = 0

    def lower() -> int | None:
        return best[-1][0] if best else None

    def quote(i: int, amount: int) -> int | None:
        key = (i, amount)
        if key not in outputs:
            if budget.max_quotes is not None and cache.misses >= budget.max_quotes:
                raise _Stop("quote_budget")
            result = cache(cps[i], token_in, amount)
            ok = result.status is QuoteStatus.OK and result.amount_in_consumed == amount
            outputs[key] = result.amount_out if ok else None
        return outputs[key]

    def consider(legs: tuple[Leg, ...], value: int) -> None:
        nonlocal mismatches
        current = lower()
        if current is not None and value <= current:
            return
        if (
            budget.max_candidates is not None
            and work["internal_evaluations"] >= budget.max_candidates
        ):
            raise _Stop("candidate_cap")
        plan = direct_split.allocation_plan(case, ids, legs)
        evaluation = evaluate(bundle, case, plan, objective, quote=cache)
        work["internal_evaluations"] += 1
        if evaluation.status is not EvalStatus.OK:
            mismatches += 1
            return
        score = objective.score(evaluation)
        mismatches += score != value
        if current is None or score > current:
            best.append((score, plan, evaluation, legs))
            if sink is not None:
                sink(plan)

    def bound(value: int | None) -> int | None:
        work["bound_evaluations"] += 1
        return value

    def make(
        kind: str, j: int, used: int, fl: int, gp: int, legs: tuple[Leg, ...], **iv: int
    ) -> Node:
        rest = amount_in - fl
        left = max_legs - len(legs)
        after = [views[i] for i in range(j + 1, n) if live[i]]
        ub: int | None
        if kind == "final":
            ub = bound(bounds.final(views[j], rest))
        elif kind == "state":
            ub = bound(bounds.state([views[i] for i in range(j, n) if live[i]], rest, left))
        else:
            lo_amt, hi_amt = amount_in * iv["lo"] // units, amount_in * iv["hi"] // units
            ub = bound(bounds.interval(views[j], after, rest, lo_amt, hi_amt, left - 1))
        node = Node(kind, j, used, fl, gp, legs, None if ub is None else gp + ub, **iv)
        trace.append(node)
        return node

    def children_of_state(node: Node) -> list[Node]:
        j, used, fl, gp, legs = node.j, node.used, node.fl, node.gp, node.legs
        later = any(live[j + 1 :])
        out = []
        if live[j]:
            out.append(make("final", j, used, fl, gp, legs))
        lo, hi = max(1, -(-units // amount_in)), units - used - 1
        if live[j] and later and len(legs) + 1 <= max_legs - 1 and lo <= hi:
            out.append(make("interval", j, used, fl, gp, legs, lo=lo, hi=hi))
        if later:
            out.append(make("state", j + 1, used, fl, gp, legs))
        return out

    def expand(node: Node) -> list[Node]:
        if node.kind == "final":
            out = quote(node.j, amount_in - node.fl)
            if out is not None:
                consider((*node.legs, (node.j, units - node.used)), node.gp + out)
            return []
        if node.kind == "interval":
            if node.lo < node.hi:
                mid = (node.lo + node.hi) // 2
                shape = (node.j, node.used, node.fl, node.gp, node.legs)
                return [
                    make("interval", *shape, lo=node.lo, hi=mid),
                    make("interval", *shape, lo=mid + 1, hi=node.hi),
                ]
            amount = amount_in * node.lo // units
            out = quote(node.j, amount)
            if out is None:
                return []
            legs = (*node.legs, (node.j, node.lo))
            return [
                make(
                    "state", node.j + 1, node.used + node.lo, node.fl + amount, node.gp + out, legs
                )
            ]
        return children_of_state(node)

    def key(node: Node) -> tuple[int, int, int]:
        return (0, 0, next(seq)) if node.ub is None else (1, -node.ub, next(seq))

    def prunable(node: Node) -> bool:
        current = lower()
        return current is not None and node.ub is not None and node.ub <= current

    termination: str | None = None
    if any(live):
        root = make("state", 0, 0, 0, 0, ())
        heap.append((key(root), root))
        work["peak_open_nodes"] = 1
    singles: list[tuple[int, int]] = []
    try:  # every live pool at the full input, in admitted order (memo §5.2)
        for i in range(n):
            out = quote(i, amount_in) if live[i] else None
            if out is not None:
                singles.append((out, -i))
    except _Stop as stop:
        termination = stop.reason
    try:  # the best valid single pool (earliest on ties), then Rule H
        if singles:
            out, neg = max(singles)
            consider(((-neg, units),), out)
        seed = rounded_relaxation(views, live, amount_in, units, max_legs)
        if seed and termination is None:
            amounts = direct_split.leg_amounts(amount_in, seed, units)
            outs = [quote(i, a) for (i, _), a in zip(seed, amounts, strict=True)]
            if None not in outs:
                consider(seed, sum(o for o in outs if o is not None))
    except _Stop as stop:
        termination = termination or stop.reason
    while termination is None and heap:
        entry = heap[0]
        if prunable(entry[1]):
            stats["nodes_pruned_bound"] += len(heap)
            heap.clear()
            break
        if work["bb_nodes_expanded"] >= opts["max_bound_nodes"]:
            termination = "node_cap"
            break
        heapq.heappop(heap)
        work["bb_nodes_expanded"] += 1
        try:
            kids = expand(entry[1])
        except _Stop as stop:
            heapq.heappush(heap, entry)
            termination = stop.reason
            break
        kept = [k for k in kids if not prunable(k)]
        stats["nodes_pruned_bound"] += len(kids) - len(kept)
        if len(heap) + len(kept) > opts["max_open_nodes"]:
            heapq.heappush(heap, entry)
            termination = "state_cap"
            break
        for kid in kept:
            heapq.heappush(heap, (key(kid), kid))
        work["peak_open_nodes"] = max(work["peak_open_nodes"], len(heap))

    work["quotes_executed"], work["quotes_memoized"] = cache.misses, cache.hits
    frontier = [node for _, node in heap]
    lo_score = lower()
    stats.update(
        truncated_by=termination,
        grid_units=units,
        direct_pools=n,
        live_pools=sum(live),
        evaluation_mismatches=mismatches,
    )
    result = Certified(
        SolveStatus.OK, None, None, None, None, record, stats, frontier, trace, units
    )
    if lo_score is None:
        result.status = SolveStatus.NO_ROUTE if termination is None else SolveStatus.TIMEOUT
        return result
    score, plan, evaluation, legs = best[-1]
    ubs = [node.ub for node in frontier]
    upper = None if None in ubs else max([score, *(u for u in ubs if u is not None)])
    certified = upper is not None
    record["certificate"] = {
        "schema": "r021.certificate/1",
        "candidate_domain_hash": record["candidate_domain_hash"],
        "objective": objective.mode,
        "source": {
            "git_revision": git_revision,
            "bundle_hash": bundle.bundle_hash,
            "algorithm": ALGORITHM,
            "effective_settings_sha256": settings_sha256(opts),
        },
        "request": {
            "case_id": case.case_id,
            "token_in": token_in,
            "token_out": case.token_out,
            "amount_in": str(amount_in),
        },
        "lower_raw": str(score),
        "upper_raw": str(upper) if certified else None,
        "gap_raw": str(upper - score) if upper is not None else None,
        "bound_kind": "certified" if certified else "unknown",
        "upper_source": (
            None
            if not certified
            else "exhaustive"
            if termination is None and stats["nodes_pruned_bound"] == 0
            else "exact_rational"
        ),
        "estimate": None,
        "optimality_proven": certified and upper == score,
        "termination": termination or "complete",
    }
    record["certificate_unavailable_reason"] = None
    result.plan, result.evaluation, result.score, result.legs = plan, evaluation, score, legs
    return result


# ================================================================== independent oracle

ORACLE_SOURCES = {"moe_classic_v1": (30, 112)}  # fee_bps, reserve bits (MoePair @ 460bf55)


def oracle_quote(state: ConstantProductPoolState, token_in: str, x: int) -> int | None:
    """Hand CPMM exact input: Solidity getAmountOut in bps form; a positive input whose
    output floors to 0, a zero reserve, an unmigrated source/fee or a post-swap input
    reserve above the source's uint width is infeasible (None). x = 0 is no leg."""
    if x == 0:
        return 0
    if token_in == state.token0:
        r_in, r_out = state.reserve0, state.reserve1
    else:
        r_in, r_out = state.reserve1, state.reserve0
    if r_in <= 0 or r_out <= 0:
        return None
    if state.source_key is not None:
        pin = ORACLE_SOURCES.get(state.source_key)
        if pin is None or state.fee_bps != pin[0] or r_in + x >= 1 << pin[1]:
            return None
    keep = 10_000 - state.fee_bps
    out = x * keep * r_out // (r_in * 10_000 + x * keep)
    return out if 0 < out < r_out else None


def oracle_allocations(
    n: int, amount: int, units: int, max_splits: int
) -> Iterator[tuple[Leg, ...]]:
    """Every repository-grid leg vector: m <= max_splits pools in admitted order, unit
    compositions of `units` into m positive parts, no non-final leg flooring to 0."""
    for m in range(1, min(max_splits, n) + 1):
        for chosen in itertools.combinations(range(n), m):
            for cuts in itertools.combinations(range(1, units), m - 1):
                parts = [b - a for a, b in zip((0, *cuts), (*cuts, units), strict=True)]
                if all(amount * u // units > 0 for u in parts[:-1]):
                    yield tuple(zip(chosen, parts, strict=True))


def oracle_amounts(amount: int, units: int, legs: Sequence[Leg]) -> list[int]:
    head = [amount * u // units for _, u in legs[:-1]]
    return [*head, amount - sum(head)]


def oracle_values(
    states: Sequence[ConstantProductPoolState], token_in: str, amount: int, units: int, splits: int
) -> dict[tuple[Leg, ...], int | None]:
    """Value of every allocation of the domain (None = infeasible)."""
    values: dict[tuple[Leg, ...], int | None] = {}
    for legs in oracle_allocations(len(states), amount, units, splits):
        outs = [
            oracle_quote(states[i], token_in, a)
            for (i, _), a in zip(legs, oracle_amounts(amount, units, legs), strict=True)
        ]
        values[legs] = None if None in outs else sum(o for o in outs if o is not None)
    return values


def optimum(values: Mapping[tuple[Leg, ...], int | None]) -> int | None:
    return max((v for v in values.values() if v is not None), default=None)


def in_region(node: Node, legs: tuple[Leg, ...], units: int) -> bool:
    """Region membership from the node's definition alone (memo §5.1)."""
    if tuple(leg for leg in legs if leg[0] < node.j) != node.legs:
        return False
    if node.kind == "final":
        return legs == (*node.legs, (node.j, units - node.used))
    if node.kind == "interval":
        return any(i == node.j and node.lo <= u <= node.hi for i, u in legs[:-1])
    return legs[-1][0] >= node.j


# ================================================================== repository objects


def cp(
    pool_id: str,
    r_in: int,
    r_out: int,
    fee_bps: int = 30,
    source_key: str | None = None,
    tokens: tuple[str, str] = ("S", "T"),
) -> ConstantProductPoolState:
    return ConstantProductPoolState(
        pool_id=pool_id,
        token0=tokens[0],
        token1=tokens[1],
        reserve0=r_in,
        reserve1=r_out,
        fee_bps=fee_bps,
        source_key=source_key,
    )


def bundle_of(
    states: Sequence[ConstantProductPoolState], ref: str = "fixture:R021-IA"
) -> SnapshotBundle:
    return SnapshotBundle(
        bundle_id="r021-ia",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools={p.pool_id: p for p in states},
        cases=(),
        bundle_hash=ref,
        source_path="<test>",
    )


@cache
def fixture_bundle(name: str) -> SnapshotBundle:
    return load_bundle(ROUTING_FIXTURES / name)


GRID = {"domain": "repository_grid", "max_bound_nodes": 10_000_000, "max_open_nodes": 10_000_000}
RAW = {**GRID, "domain": "raw_integer", "raw_max_amount_in": 1_000_000}


@dataclass(frozen=True)
class Instance:
    states: tuple[ConstantProductPoolState, ...]
    amount: int
    step: int
    splits: int
    raw: bool

    @property
    def units(self) -> int:
        return self.amount if self.raw else 100 // self.step

    def case(self) -> Case:
        return Case("inst", "S", "T", self.amount)

    def run(self, options: Mapping[str, Any] | None = None, **kw: Any) -> Certified:
        return certify(
            bundle_of(self.states),
            self.case(),
            gross_only(),
            max_splits=self.splits,
            percent_step=self.step,
            options=options or (RAW if self.raw else GRID),
            **kw,
        )

    @cached_property
    def values(self) -> dict[tuple[Leg, ...], int | None]:
        return oracle_values(self.states, "S", self.amount, self.units, self.splits)


def random_instance(rng: random.Random, raw: bool) -> Instance:
    """Small multipool instances: dust to large inputs, fees 0-100 bps, dead pools and
    sourced pools a few units below the uint112 revert, random admitted order."""
    n = rng.randint(1, 4 if raw else 5)
    dust = rng.random() < 0.2
    amount = rng.choice([1, 2, 3, 7]) if dust else rng.randint(8, 40 if raw else 300)
    states = []
    for i in range(n):
        roll = rng.random()
        if roll < 0.1:
            states.append(cp(f"p{i}", 0, rng.randint(1, 5000)))
        elif roll < 0.25:  # linear up to a few units below the uint112 revert
            margin = rng.randint(2, max(3, amount))
            depth = (1 << 112) - margin
            states.append(
                cp(f"p{i}", depth, depth * rng.randint(5, 30) // 20, 30, "moe_classic_v1")
            )
        else:
            fee = rng.choice([0, 5, 30, 100])
            r_in = rng.randint(max(2, amount // 4), 4 * amount + 20)
            states.append(cp(f"p{i}", r_in, r_in * rng.randint(5, 60) // 20 + 1, fee))
    rng.shuffle(states)
    step = rng.choice([100, 50, 25, 20, 10, 5, 4])
    return Instance(tuple(states), amount, step, rng.randint(1, 4), raw)


def instances(seed: int, count: int, raw: bool) -> list[Instance]:
    rng = random.Random(seed)
    return [random_instance(rng, raw) for _ in range(count)]


GRID_SUITE = instances(20260930, 40, raw=False)
RAW_SUITE = instances(20261001, 25, raw=True)


def _shared_validator() -> ModuleType:
    """The unchanged R021-C/1 validator (tests/docs/test_research_021_contract.py), loaded
    read-only by path: this lane adds no shared vocabulary or codes."""
    path = REPO / "tests" / "docs" / "test_research_021_contract.py"
    spec = importlib.util.spec_from_file_location("r021_contract_validator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _shared_validator()


def contract_violations(
    result: Certified, case: Case, git_revision: str = BASE_REVISION
) -> set[str]:
    record = result.record
    cert = record["certificate"]
    run = {
        "git_revision": git_revision,
        "bundle_hash": cert["source"]["bundle_hash"] if cert else "",
        "algorithm": ALGORITHM,
        "effective_settings_sha256": cert["source"]["effective_settings_sha256"] if cert else "",
    }
    ctx = {
        "status": result.status.value,
        "final_score": None if result.score is None else str(result.score),
        "objective": "gross_only",
        "quotes_counted": record["work"]["quotes_executed"],
        "hard_killed": False,
        "run": run,
        "request": {
            "case_id": case.case_id,
            "token_in": case.token_in,
            "token_out": case.token_out,
            "amount_in": str(case.amount_in),
        },
    }
    violations: set[str] = VALIDATOR.check_diagnostics(record, ctx)
    return violations


def certificate_errors(inst: Instance, result: Certified) -> list[str]:
    """Independent audit of one run against the oracle (memo §8): the lower bound is a
    feasible value, every emitted upper bound dominates the optimum, zero gap means the
    optimum, and every feasible allocation above the incumbent lies in an open node
    whose bound covers it (frontier coverage)."""
    values = inst.values
    opt = optimum(values)
    cert = result.record["certificate"]
    errors = []
    if opt is None:
        if result.status is SolveStatus.OK:
            errors.append("plan on an infeasible domain")
        return errors
    if cert is None:
        if result.status is not SolveStatus.TIMEOUT:
            errors.append(f"no certificate, status {result.status}")
        lows: list[int] = []
    else:
        lows = [int(cert["lower_raw"])]
        if lows[0] not in values.values():
            errors.append("lower bound is not a feasible value")
        if cert["bound_kind"] == "certified":
            if int(cert["upper_raw"]) < opt:
                errors.append(f"upper {cert['upper_raw']} < optimum {opt}")
            if cert["optimality_proven"] and lows[0] != opt:
                errors.append(f"false zero gap: {lows[0]} < {opt}")
    for legs, value in values.items():
        if value is None or (lows and value <= lows[0]):
            continue
        covering = [n for n in result.open_nodes if in_region(n, legs, inst.units)]
        if not covering:
            errors.append(f"uncovered allocation {legs} = {value}")
        elif any(n.ub is not None and n.ub < value for n in covering):
            errors.append(f"open node under-bounds {legs} = {value}")
    return errors


def node_bound_errors(inst: Instance, result: Certified) -> list[str]:
    """Every node's bound dominates the best feasible allocation of its region."""
    values = [(legs, v) for legs, v in inst.values.items() if v is not None]
    errors = []
    for node in result.trace:
        best = max((v for legs, v in values if in_region(node, legs, inst.units)), default=None)
        if best is not None and node.ub is not None and node.ub < best:
            errors.append(f"{node.kind} j={node.j} legs={node.legs} ub {node.ub} < {best}")
    return errors


# ================================================================== tests: repository semantics


def test_oracle_quote_is_the_repository_cpmm_rule() -> None:
    rng = random.Random(7)
    samples = [
        cp("a", 1000, 1000),
        cp("dead", 0, 10),
        cp("f0", 50, 70, 0),
        cp("f100", 9, 9000, 100),
    ]
    samples += [cp("moe", (1 << 112) - 10, 1 << 111, 30, "moe_classic_v1")]
    samples += [cp("fee", 1000, 1000, 25, "moe_classic_v1"), cp("unk", 1000, 1000, 30, "other")]
    for state in samples:
        for x in [1, 2, 3, 9, 10, 11, *(rng.randint(1, 5000) for _ in range(20))]:
            result = quote_exact_in(state, "S", x)
            ok = result.status is QuoteStatus.OK and result.amount_in_consumed == x
            assert oracle_quote(state, "S", x) == (result.amount_out if ok else None)
    # The failure statuses behind each None (pools/constant_product.py).
    assert (
        quote_exact_in(cp("a", 1000, 1000), "S", 1).status is QuoteStatus.INSUFFICIENT_OUTPUT_AMOUNT
    )
    assert quote_exact_in(cp("d", 0, 10), "S", 5).status is QuoteStatus.INSUFFICIENT_LIQUIDITY
    big = cp("moe", (1 << 112) - 10, 1 << 111, 30, "moe_classic_v1")
    assert quote_exact_in(big, "S", 9).status is QuoteStatus.OK
    assert quote_exact_in(big, "S", 10).status is QuoteStatus.REVERTED


@pytest.mark.parametrize("inst", GRID_SUITE[:25], ids=lambda i: f"n{len(i.states)}-a{i.amount}")
def test_oracle_grid_is_direct_split_and_every_allocation_replays(inst: Instance) -> None:
    """Same-domain claim (memo §2.2): the oracle's enumeration is exactly the domain
    `direct_split` searches, and every allocation's plan replays to its oracle value."""
    bundle, case = bundle_of(inst.states), inst.case()
    ids = tuple(p.pool_id for p in inst.states)
    values = inst.values
    for legs, value in values.items():
        plan = direct_split.allocation_plan(case, ids, legs)
        assert direct_split.leg_amounts(inst.amount, legs, inst.units) == oracle_amounts(
            inst.amount, inst.units, legs
        )
        ev = evaluate(bundle, case, plan, gross_only())
        assert (ev.gross_output if ev.status is EvalStatus.OK else None) == value
    params = {"max_splits": inst.splits, "percent_step": inst.step}
    prepared = direct_split.prepare(bundle, AlgorithmConfig(direct_split.NAME, params))
    solved = direct_split.solve(case, SolveContext(bundle, gross_only(), prepared), Budget())
    opt = optimum(values)
    assert solved.score == opt
    assert (solved.status is SolveStatus.OK) == (opt is not None)


def test_zero_floor_nonfinal_legs_are_not_domain_members_but_lose_nothing() -> None:
    """A non-final leg flooring to 0 is never emitted (direct_split docstring); the same
    amounts are reached with its units moved to the final leg."""
    amount, units = 3, 20
    legs = list(oracle_allocations(3, amount, units, 3))
    assert all(amount * u // units > 0 for leg in legs for _, u in leg[:-1])
    everything = {
        tuple(zip(chosen, parts, strict=True))
        for m in (1, 2, 3)
        for chosen in itertools.combinations(range(3), m)
        for cuts in itertools.combinations(range(1, units), m - 1)
        for parts in [[b - a for a, b in zip((0, *cuts), (*cuts, units), strict=True)]]
    }

    def amounts_of(leg: tuple[Leg, ...]) -> tuple[tuple[int, int], ...]:
        pairs = zip(leg, oracle_amounts(amount, units, leg), strict=True)
        return tuple((i, a) for (i, _), a in pairs if a > 0)

    assert {amounts_of(x) for x in everything} == {amounts_of(x) for x in legs}
    assert len(everything) > len(legs)


# ================================================================== tests: certificates


@pytest.mark.parametrize("inst", GRID_SUITE, ids=lambda i: f"grid-n{len(i.states)}-a{i.amount}")
def test_complete_grid_certificate_is_the_exhaustive_optimum(inst: Instance) -> None:
    result = inst.run()
    opt = optimum(inst.values)
    assert result.stats["truncated_by"] is None
    assert result.stats["evaluation_mismatches"] == 0
    assert certificate_errors(inst, result) == []
    assert node_bound_errors(inst, result) == []
    if opt is None:
        assert result.status is SolveStatus.NO_ROUTE and result.record["certificate"] is None
        return
    cert = result.record["certificate"]
    assert result.score == opt and cert["optimality_proven"] and cert["termination"] == "complete"
    assert contract_violations(result, inst.case()) == set()
    assert result.evaluation is not None and result.evaluation.gross_output == opt


@pytest.mark.parametrize("inst", RAW_SUITE, ids=lambda i: f"raw-n{len(i.states)}-a{i.amount}")
def test_complete_raw_certificate_is_the_exhaustive_optimum(inst: Instance) -> None:
    result = inst.run()
    assert certificate_errors(inst, result) == []
    assert node_bound_errors(inst, result) == []
    opt = optimum(inst.values)
    assert result.score == opt
    if opt is not None:
        assert result.record["certificate"]["optimality_proven"]
        assert contract_violations(result, inst.case()) == set()
        grid_opt = optimum(oracle_values(inst.states, "S", inst.amount, 20, inst.splits))
        assert grid_opt is None or grid_opt <= opt  # the grid is a subset (memo §2.3)


def _truncations(inst: Instance) -> Iterator[tuple[dict[str, Any], Budget]]:
    options = RAW if inst.raw else GRID
    for cap in (1, 2, 3, 5, 8, 13):
        yield {**options, "max_bound_nodes": cap}, Budget()
    for cap in (1, 2, 4):
        yield {**options, "max_open_nodes": cap}, Budget()
    for quotes in (0, 1, 2, 3, 5, 9):
        yield dict(options), Budget(max_quotes=quotes)
    for evals in (0, 1, 2):
        yield dict(options), Budget(max_candidates=evals)


@pytest.mark.parametrize(
    "inst", GRID_SUITE[:20] + RAW_SUITE[:10], ids=lambda i: f"n{len(i.states)}"
)
def test_budget_truncated_certificates_keep_the_optimum_inside(inst: Instance) -> None:
    opt = optimum(inst.values)
    for options, budget in _truncations(inst):
        result = inst.run(options, budget=budget)
        assert certificate_errors(inst, result) == [], (options, budget)
        cert = result.record["certificate"]
        if result.stats["truncated_by"] is not None and cert is not None:
            assert cert["termination"] == result.stats["truncated_by"]
            assert cert["bound_kind"] == "certified"
            assert contract_violations(result, inst.case()) == set()
        if cert is None and opt is not None:
            assert result.status is SolveStatus.TIMEOUT  # never no_route under truncation
        if budget.max_quotes is not None:
            assert result.record["work"]["quotes_executed"] <= budget.max_quotes
        if budget.max_candidates is not None:
            assert result.record["work"]["internal_evaluations"] <= budget.max_candidates


def test_truncation_suite_hits_every_cooperative_stop_and_a_nonzero_gap() -> None:
    stops, gaps = set(), set()
    for inst in GRID_SUITE[:20] + RAW_SUITE[:10]:
        for options, budget in _truncations(inst):
            result = inst.run(options, budget=budget)
            stops.add(result.stats["truncated_by"])
            cert = result.record["certificate"]
            if cert is not None and cert["bound_kind"] == "certified":
                gaps.add(int(cert["gap_raw"]) > 0)
    assert {"node_cap", "state_cap", "quote_budget", "candidate_cap", None} <= stops
    assert True in gaps  # truncated certificates with an honest nonzero gap exist


def test_the_suites_exercise_every_edge_class() -> None:
    """The random suites contain dust, dead pools, near-overflow sourced pools, single
    pools, cardinality 1-4, plateaus and grid-infeasible domains (not vacuous)."""
    both = GRID_SUITE + RAW_SUITE
    assert any(i.amount < 100 // i.step for i in GRID_SUITE)
    assert any(p.reserve0 == 0 for i in both for p in i.states)
    assert any(p.source_key for i in both for p in i.states)
    assert {len(i.states) for i in both} >= {1, 2, 3, 4}
    assert {i.splits for i in both} == {1, 2, 3, 4}
    assert any(optimum(i.values) is None for i in both)
    overflowing = [
        i
        for i in both
        if any(
            p.source_key and oracle_quote(p, "S", i.amount) is None and i.amount > 1
            for p in i.states
        )
    ]
    assert overflowing


# ================================================================== tests: mutations


class MinusOne(Bounds):
    """Underestimates every state bound by one unit."""

    def state(self, pools: Sequence[PoolView], total: int, legs_left: int) -> int | None:
        value = super().state(pools, total, legs_left)
        return None if value is None else value - 1


class TopTwo(Bounds):
    """A 'secret shortlist': relaxes only the two pools with the best marginal price."""

    def state(self, pools: Sequence[PoolView], total: int, legs_left: int) -> int | None:
        short = sorted(pools, key=lambda p: -Fraction(p.keep * p.r_out, p.r_in))[:2]
        return super().state(short, total, legs_left)

    def interval(
        self, p: PoolView, rest: Sequence[PoolView], total: int, lo: int, hi: int, legs_left: int
    ) -> int | None:
        short = sorted(rest, key=lambda q: -Fraction(q.keep * q.r_out, q.r_in))[:1]
        return super().interval(p, short, total, lo, hi, legs_left)


class HintValue(Bounds):
    """Uses the relaxation's value at the hint point (a lower estimate of the continuous
    optimum, like a stationary-point or integer-marginal stop) as if it were a bound."""

    def state(self, pools: Sequence[PoolView], total: int, legs_left: int) -> int | None:
        t = tangent_hint(pools, total)
        t[-1] += total - sum(t)
        return math.floor(sum((g(p, x) for p, x in zip(pools, t, strict=True)), Fraction(0)))


class UnknownAsZero(Bounds):
    """Treats an unknown bound as 0 (the forbidden reading)."""

    def interval(
        self, p: PoolView, rest: Sequence[PoolView], total: int, lo: int, hi: int, legs_left: int
    ) -> int | None:
        return 0


class Unknown(Bounds):
    """Declares interval bounds unknown (as an unsupported region would be)."""

    def interval(
        self, p: PoolView, rest: Sequence[PoolView], total: int, lo: int, hi: int, legs_left: int
    ) -> int | None:
        return None


MUTATION_SUITE = [
    i
    for i in GRID_SUITE + RAW_SUITE + instances(20261002, 40, raw=False)
    if len(i.states) > 1 and i.splits > 1
]


@pytest.mark.parametrize(
    ("mutant", "false_certificate"),
    [(MinusOne(), True), (TopTwo(), True), (UnknownAsZero(), True), (HintValue(), False)],
    ids=lambda v: type(v).__name__,
)
def test_underestimating_bound_mutations_are_detected(
    mutant: Bounds, false_certificate: bool
) -> None:
    """Each mutation is caught by the independent audit; the first three also emit an
    end-to-end false zero-gap certificate somewhere in the suite (memo §8 item 4)."""
    detected, false_zero = 0, 0
    for inst in MUTATION_SUITE:
        result = inst.run(bounds=mutant)
        errors = certificate_errors(inst, result) + node_bound_errors(inst, result)
        detected += bool(errors)
        false_zero += any("false zero gap" in e for e in errors)
    assert detected > 0
    assert (false_zero > 0) is false_certificate


def test_genuine_bounds_pass_the_mutation_audit() -> None:
    for inst in MUTATION_SUITE:
        result = inst.run()
        assert certificate_errors(inst, result) == [] and node_bound_errors(inst, result) == []


def test_unknown_bounds_never_certify_a_truncated_search() -> None:
    for inst in MUTATION_SUITE[:15]:
        result = inst.run(
            {**GRID, "max_bound_nodes": 3} if not inst.raw else {**RAW, "max_bound_nodes": 3},
            bounds=Unknown(),
        )
        cert = result.record["certificate"]
        assert certificate_errors(inst, result) == []
        if cert is not None and any(n.ub is None for n in result.open_nodes):
            assert cert["bound_kind"] == "unknown" and cert["upper_raw"] is None
            assert cert["gap_raw"] is None and not cert["optimality_proven"]
            assert contract_violations(result, inst.case()) == set()
    # An unknown node is never pruned: a complete run still resolves it exhaustively.
    for inst in MUTATION_SUITE[:15]:
        result = inst.run(bounds=Unknown())
        assert certificate_errors(inst, result) == []
        assert result.score == optimum(inst.values)


# ================================================================== tests: counterexamples

R3 = RECON["R3_plateau"]
R6 = RECON["R6_grid_order_and_bounds"]


def _pair(
    p1: Sequence[int], p2: Sequence[int], order: str = "12"
) -> tuple[ConstantProductPoolState, ...]:
    pools = {"1": cp("p1", p1[0], p1[1]), "2": cp("p2", p2[0], p2[1])}
    return tuple(pools[k] for k in order)


def test_r2_integer_marginals_are_not_concave_and_one_unit_legs_are_infeasible() -> None:
    state = cp("p", *RECON["R2_integer_marginals"]["reserves"])
    outs = [oracle_quote(state, "S", x) for x in range(7)]
    assert outs == [0, None, 1, 2, 3, 4, 5]  # q(1) floors to 0: infeasible in the repository
    raw = Instance((state, cp("q", 1000, 1000)), 3, 5, 2, raw=True)
    result = raw.run()
    assert result.score == optimum(raw.values) == 2  # all 3 on one pool: 1+2 has a dust leg
    assert result.record["certificate"]["optimality_proven"]


def test_r3_plateau_stops_a_local_search_but_not_the_certificate() -> None:
    inst = Instance(_pair(R3["pool1"], R3["pool2"]), R3["amount_in"], 5, 2, raw=True)
    by_x = {
        x: sum(
            oracle_quote(s, "S", a) or 0
            for s, a in zip(inst.states, (x, inst.amount - x), strict=True)
        )
        for x in range(inst.amount + 1)
    }
    assert [by_x[x] for x in (0, 1, 2, 3, 15)] == [70, 70, 70, 72, 76]
    x = 1  # a strict +-1 local search from the plateau stops immediately
    assert by_x[x - 1] <= by_x[x] and by_x[x + 1] <= by_x[x]
    result = inst.run()
    assert (
        result.score == R3["raw_integer_max"] and result.record["certificate"]["optimality_proven"]
    )
    # A plateau incumbent is never certified: the root bound already exceeds 70.
    root = result.trace[0]
    assert root.kind == "state" and root.ub is not None and root.ub > by_x[1]


def test_r6_pool_order_changes_the_grid_domain_and_its_certificate() -> None:
    a = R6["amount_in"]
    results = {}
    for order in ("12", "21"):
        inst = Instance(
            _pair(R6["pool1"], R6["pool2"], order), a, R6["percent_step"], R6["max_splits"], False
        )
        results[order] = inst.run()
        assert certificate_errors(inst, results[order]) == []
    raw = Instance(_pair(R6["pool1"], R6["pool2"]), a, 5, 2, raw=True).run()
    assert results["12"].score == R6["grid_optimum_order_p1_p2"]
    assert results["21"].score == R6["grid_optimum_order_p2_p1"]
    assert raw.score == R6["raw_integer_optimum"]
    hashes = {k: r.record["candidate_domain_hash"] for k, r in results.items()}
    assert hashes["12"] != hashes["21"] != raw.record["candidate_domain_hash"]
    # No transfer: the (p1, p2) grid proof's upper bound is below the raw optimum.
    assert raw.score is not None
    assert int(results["12"].record["certificate"]["upper_raw"]) < raw.score


# ================================================================== tests: scope, kill, schema


def test_non_cpmm_direct_pools_and_net_objectives_are_unsupported() -> None:
    mixed = fixture_bundle("routing/mantle_mixed")
    for case in mixed.cases:
        result = certify(mixed, case, gross_only(), max_splits=4, percent_step=5, options=GRID)
        pools = mixed.pools_for_pair(case.token_in, case.token_out)
        if any(not isinstance(p, ConstantProductPoolState) for p in pools):
            assert result.status is SolveStatus.UNSUPPORTED and result.plan is None
            assert result.record["scope"] == {
                "supported": False,
                "reason": "non_constant_product_direct_pool",
            }
            assert result.record["certificate"] is None
            assert result.record["work"]["quotes_executed"] == 0
    inst = GRID_SUITE[0]
    net = certify(
        bundle_of(inst.states),
        inst.case(),
        synthetic_fixed_cost(1),
        max_splits=2,
        percent_step=5,
        options=GRID,
    )
    assert net.status is SolveStatus.UNSUPPORTED
    assert net.record["scope"]["reason"] == "objective_not_gross_only"
    big = Instance(inst.states, 50, 5, 2, raw=True)
    capped = big.run({**RAW, "raw_max_amount_in": 49})
    assert capped.status is SolveStatus.UNSUPPORTED
    assert capped.record["scope"]["reason"] == "raw_integer_amount_above_limit"


def test_a_hard_killed_solve_leaves_only_the_last_reported_candidate() -> None:
    inst = next(
        i for i in GRID_SUITE if i.run().record["work"]["quotes_executed"] > len(i.states) + 1
    )
    needed = inst.run().record["work"]["quotes_executed"]
    reported: list[RoutePlan] = []
    with pytest.raises(QuoteLimitExceeded), metered_quotes(needed - 1):
        inst.run(sink=reported.append)  # the hard meter cuts the solve off: no result at all
    assert reported  # the runner keeps this as last_valid_candidate, never a certificate


def test_the_quote_ledger_is_the_worker_meter_and_bound_work_is_separate() -> None:
    for inst in GRID_SUITE[:10]:
        with metered_quotes(None) as meter:
            result = inst.run()
        work = result.record["work"]
        assert work["quotes_executed"] == meter.counted
        if result.record["certificate"] is not None:
            assert work["bound_evaluations"] >= work["bb_nodes_expanded"] >= 0
            assert work["bound_evaluations"] == len(result.trace)


@pytest.mark.parametrize(
    ("options", "key"),
    [
        ({}, "domain"),
        ({"domain": "grid"}, "domain"),
        ({"domain": "repository_grid", "max_bound_nodes": 5}, "max_open_nodes"),
        ({**GRID, "max_bound_nodes": True}, "max_bound_nodes"),
        ({**GRID, "max_bound_nodes": 0}, "max_bound_nodes"),
        ({**GRID, "max_open_nodes": 1.5}, "max_open_nodes"),
        ({**GRID, "max_open_nodes": float("inf")}, "max_open_nodes"),
        ({**GRID, "max_bound_nodes": 10_000_001}, "max_bound_nodes"),
        ({**GRID, "raw_max_amount_in": 10}, "raw_max_amount_in"),
        ({**RAW, "raw_max_amount_in": 1_000_001}, "raw_max_amount_in"),
        ({"domain": "raw_integer", "max_bound_nodes": 1, "max_open_nodes": 1}, "raw_max_amount_in"),
        ({**GRID, "shortlist": 2}, "shortlist"),
        ({**GRID, "max_splits": 2}, "max_splits"),
        ({**GRID, "percent_step": 5}, "percent_step"),
        ({**GRID, "tolerance": 0}, "tolerance"),
    ],
)
def test_option_validator_refuses_outside_the_schema(options: dict[str, Any], key: str) -> None:
    with pytest.raises(OptionsError, match=key):
        validate_options(options)


def test_preset_and_stress_profile_are_valid_and_pinned() -> None:
    for name in ("preset", "stress_raw"):
        entry = FIX[name]
        assert validate_options(entry["options"]) == entry["options"]
        assert settings_sha256(entry["options"]) == entry["settings_sha256"]
    assert FIX["preset"]["options"]["domain"] == "repository_grid"
    assert all(k in MEMO for k in ("max_bound_nodes", "max_open_nodes", "raw_max_amount_in"))
    for key, (lo, hi) in INT_RANGES.items():
        assert f"`{key}` | integer | {lo:,} … {hi:,}" in MEMO


# ================================================================== tests: published records


def _example_inputs(ex: Mapping[str, Any]) -> tuple[SnapshotBundle, Case]:
    src = ex["bundle"]
    if "fixture" in src:
        bundle = fixture_bundle(src["fixture"])
        return bundle, bundle.case(ex["case_id"])
    states = [cp(pid, r_in, r_out) for pid, r_in, r_out in src["pools"]]
    bundle = bundle_of(states, src["ref"])
    return bundle, Case(ex["case_id"], "S", "T", ex["amount_in"])


def run_example(ex: Mapping[str, Any]) -> Certified:
    bundle, case = _example_inputs(ex)
    budget = Budget(**ex.get("budget", {}))
    return certify(
        bundle,
        case,
        gross_only(),
        max_splits=ex["max_splits"],
        percent_step=ex["percent_step"],
        options=ex["options"],
        budget=budget,
        cohort="fixture",
    )


@pytest.mark.parametrize("ex", FIX["examples"], ids=lambda e: e["id"])
def test_published_examples_are_regenerated_exactly(ex: dict[str, Any]) -> None:
    result = run_example(ex)
    assert result.record == ex["record"]
    assert result.status.value == ex["status"]
    assert (None if result.score is None else str(result.score)) == ex["final_score"]
    bundle, case = _example_inputs(ex)
    states = bundle.pools_for_pair(case.token_in, case.token_out)
    # Every published domain lists every admitted direct pool, in admitted order.
    assert result.record["domain"]["pool_order"] == [p.pool_id for p in states]
    cert = result.record["certificate"]
    if cert is not None:
        assert contract_violations(result, case) == set()
    cps = [p for p in states if isinstance(p, ConstantProductPoolState)]
    if not cps or len(cps) < len(states):
        assert ex["oracle_optimum"] is None and cert is None
        return
    raw = ex["options"]["domain"] == "raw_integer"
    units = case.amount_in if raw else 100 // ex["percent_step"]
    opt = optimum(oracle_values(cps, case.token_in, case.amount_in, units, ex["max_splits"]))
    assert ex["oracle_optimum"] == (None if opt is None else str(opt))
    if cert is not None and opt is not None:
        assert int(cert["lower_raw"]) <= opt <= int(cert["upper_raw"])
        assert cert["optimality_proven"] is (int(cert["lower_raw"]) == int(cert["upper_raw"]))
        if cert["optimality_proven"]:
            assert int(cert["lower_raw"]) == opt


def test_published_r6_domains_are_the_shared_contract_domains() -> None:
    """The R6 records hash to exactly WHI-1547's `grid38_p1p2`, `grid38_p2p1` and `raw38`
    domains (fixtures/examples.json, read-only): no new domain vocabulary."""
    shared = VALIDATOR.EXAMPLES["domain_hashes"]
    mine = {e["id"]: e["record"]["candidate_domain_hash"] for e in FIX["examples"]}
    assert mine["P-IA-R6-GRID-P1P2"] == shared["grid38_p1p2"]
    assert mine["P-IA-R6-GRID-P2P1"] == shared["grid38_p2p1"]
    assert mine["P-IA-R6-RAW"] == shared["raw38"]


def test_real_moe_classic_states_certify_the_direct_split_value() -> None:
    """Real admitted CPMM smoke: every case of the committed Merchant Moe Classic bundle
    (source moe_classic_v1, fee 30, uint112) at the profile grid."""
    bundle = fixture_bundle("moe_classic/bundle")
    prepared = direct_split.prepare(
        bundle, AlgorithmConfig(direct_split.NAME, {"max_splits": 4, "percent_step": 5})
    )
    for case in bundle.cases:
        result = certify(
            bundle,
            case,
            gross_only(),
            max_splits=4,
            percent_step=5,
            options=FIX["preset"]["options"],
        )
        ds = direct_split.solve(case, SolveContext(bundle, gross_only(), prepared), Budget())
        assert result.status is ds.status and result.score == ds.score
        if result.record["certificate"] is not None:
            assert result.record["certificate"]["optimality_proven"]
            assert contract_violations(result, case) == set()


def test_published_identities_are_bound_to_run_and_request() -> None:
    ex = next(e for e in FIX["examples"] if e["record"]["certificate"])
    result = run_example(ex)
    _, case = _example_inputs(ex)
    assert contract_violations(result, case, git_revision="0" * 40) == {"C_IDENTITY"}
    other = Case(case.case_id, case.token_in, case.token_out, case.amount_in + 1)
    assert contract_violations(result, other) == {"C_REQUEST"}


def grid_vs_raw_summary() -> dict[str, Any]:
    """Same pools, same request: raw_integer optimum versus repository_grid optimum
    (percent_step 5), an expanded-domain comparison (memo §9.3)."""
    rng = random.Random(20261003)
    rows = []
    for _ in range(60):
        n, amount, splits = rng.randint(2, 4), rng.randint(20, 60), rng.randint(2, 3)
        reserves = [rng.randint(amount // 2, 3 * amount) for _ in range(n)]
        states = tuple(
            cp(f"p{i}", r, r * rng.randint(10, 40) // 20, rng.choice([5, 30]))
            for i, r in enumerate(reserves)
        )
        grid = optimum(oracle_values(states, "S", amount, 20, splits))
        raw = optimum(oracle_values(states, "S", amount, amount, splits))
        rows.append((grid, raw))
    diffs = [r - g for g, r in rows if g is not None and r is not None]
    return {
        "instances": len(rows),
        "both_feasible": len(diffs),
        "raw_better": sum(d > 0 for d in diffs),
        "equal": sum(d == 0 for d in diffs),
        "max_raw_minus_grid": max(diffs),
        "grid_only_infeasible": sum(g is None and r is not None for g, r in rows),
    }


def test_grid_versus_raw_is_an_expanded_domain_summary() -> None:
    summary = grid_vs_raw_summary()
    assert summary == FIX["grid_vs_raw"]["summary"]
    assert summary["raw_better"] > 0 and all(d >= 0 for d in [summary["max_raw_minus_grid"]])


def test_pinned_probe_summaries_are_internally_consistent() -> None:
    probe = FIX["probe"]
    for split in probe["splits"]:
        assert sum(split["scope"].values()) == split["cases"]
        assert split["scope"].get("supported_multi_pool", 0) == 0
        assert split["pair_structure"]["max_cpmm_pools_per_pair"] == 1


def test_synthetic_magnitude_sweep_reproduces_the_preset_evidence() -> None:
    """Memo §9.2: the pinned node/open/quote figures behind the bounded preset."""
    summary = sweep_summary(sweep()["rows"])
    assert summary == FIX["sweep"]["summary"]
    preset = FIX["preset"]["options"]
    for entry in summary.values():
        assert entry["all_complete_zero_gap"] and entry["all_equal_direct_split"]
        assert entry["bb_nodes_expanded"]["max"] * 5 <= preset["max_bound_nodes"]
        assert entry["peak_open_nodes"]["max"] * 5 <= preset["max_open_nodes"]


# ================================================================== bounded evidence passes


def probe(bundle_dir: str, cohort: str) -> dict[str, Any]:
    """Scope classification of every case of a real bundle and the preset solve of every
    supported case at the daily/full profile grid (percent_step 5, max_splits 4)."""
    bundle = load_bundle(bundle_dir)
    scope: dict[str, int] = {}
    rows = []
    for case in bundle.cases:
        pools = bundle.pools_for_pair(case.token_in, case.token_out)
        if not pools:
            cls = "no_direct_pool"
        elif any(not isinstance(p, ConstantProductPoolState) for p in pools):
            cls = "unsupported_non_cpmm"
        else:
            cls = "supported_single_pool" if len(pools) == 1 else "supported_multi_pool"
        scope[cls] = scope.get(cls, 0) + 1
        if cls.startswith("supported"):
            result = certify(
                bundle,
                case,
                gross_only(),
                max_splits=4,
                percent_step=5,
                options=FIX["preset"]["options"],
                cohort=cohort,
            )
            cert = result.record["certificate"]
            rows.append(
                {
                    "case_id": case.case_id,
                    "status": result.status.value,
                    "work": result.record["work"],
                    "gap": None if cert is None else cert["gap_raw"],
                    "termination": None if cert is None else cert["termination"],
                }
            )
    pairs: dict[frozenset[str], list[bool]] = {}
    for p in bundle.pools.values():
        pairs.setdefault(frozenset((p.token0, p.token1)), []).append(
            isinstance(p, ConstantProductPoolState)
        )
    structure = {
        "pools": len(bundle.pools),
        "cpmm_pools": sum(sum(v) for v in pairs.values()),
        "pairs": len(pairs),
        "all_cpmm_pairs": sum(all(v) for v in pairs.values()),
        "max_cpmm_pools_per_pair": max((sum(v) for v in pairs.values()), default=0),
    }
    return {
        "bundle_hash": bundle.bundle_hash,
        "cases": len(bundle.cases),
        "scope": scope,
        "pair_structure": structure,
        "rows": rows,
    }


def sweep(seed: int = 20261004) -> dict[str, Any]:
    """Synthetic parallel-pool instances at real Mantle CPMM magnitudes (reserves 1e20-1e25,
    fee 30, sourced) - NOT corpus data: node/open/quote counts to completion for the
    profile grid (percent_step 5, max_splits 4) and percent_step 1, beside direct_split's
    quotes on the same instance."""
    rng = random.Random(seed)
    rows = []
    for n in (2, 3, 4, 6, 8):
        for _ in range(6):
            base_in = rng.randint(10**20, 10**25)
            price = Fraction(rng.randint(1, 10**6), rng.randint(1, 10**6))
            states = []
            for i in range(n):
                scale = Fraction(rng.randint(10, 1000), 100)
                drift = Fraction(rng.randint(980, 1020), 1000)
                r_in = int(base_in * scale)
                states.append(
                    cp(f"p{i}", r_in, max(1, int(r_in * price * drift)), 30, "moe_classic_v1")
                )
            amount = int(base_in * Fraction(rng.choice([1, 10, 100, 1000, 5000]), 10_000))
            for step in (5, 1):
                inst = Instance(tuple(states), amount, step, 4, False)
                result = inst.run(
                    {**GRID, "max_bound_nodes": 1_000_000, "max_open_nodes": 1_000_000}
                )
                bundle = bundle_of(states)
                prepared = direct_split.prepare(
                    bundle,
                    AlgorithmConfig(direct_split.NAME, {"max_splits": 4, "percent_step": step}),
                )
                ds = direct_split.solve(
                    inst.case(), SolveContext(bundle, gross_only(), prepared), Budget()
                )
                cert = result.record["certificate"]
                rows.append(
                    {
                        "pools": n,
                        "percent_step": step,
                        "amount_fraction_bp": int(Fraction(amount) / base_in * 10_000),
                        "equal_to_direct_split": ds.score == result.score,
                        "gap": cert["gap_raw"],
                        "work": result.record["work"],
                        "direct_split_quotes": ds.search_stats["quotes_executed"],
                    }
                )
    return {"seed": seed, "rows": rows}


def raw_sweep(seed: int = 5) -> list[dict[str, Any]]:
    """raw_integer stress configuration versus repository_grid (percent_step 5) on the
    same synthetic pools: inputs 100 to 100,000 raw units (the stress limit), 2-4 pools,
    max_splits 4 (an expanded-domain comparison)."""
    rng = random.Random(seed)
    rows = []
    for amount in (100, 1000, 10_000, 100_000):
        for n in (2, 3, 4):
            reserves = [rng.randint(amount, 10 * amount) for _ in range(n)]
            states = tuple(
                cp(f"p{i}", r, r * rng.randint(15, 25) // 20 + 1, 30)
                for i, r in enumerate(reserves)
            )
            raw = Instance(states, amount, 5, 4, True).run(FIX["stress_raw"]["options"])
            grid = Instance(states, amount, 5, 4, False).run(FIX["preset"]["options"])
            cert = raw.record["certificate"]
            rows.append(
                {
                    "amount_in": amount,
                    "pools": n,
                    "raw": {
                        k: cert[k] for k in ("lower_raw", "upper_raw", "gap_raw", "termination")
                    },
                    "grid_optimum": str(grid.score),
                    "raw_work": raw.record["work"],
                    "grid_work": grid.record["work"],
                }
            )
    return rows


def _p50_max(values: Sequence[int]) -> dict[str, int]:
    ordered = sorted(values)
    return {"p50": ordered[len(ordered) // 2], "max": ordered[-1]}


def sweep_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for step in (5, 1):
        mine = [r for r in rows if r["percent_step"] == step]
        entry: dict[str, Any] = {
            key: _p50_max([r["work"][key] for r in mine])
            for key in (
                "bb_nodes_expanded",
                "peak_open_nodes",
                "bound_evaluations",
                "quotes_executed",
            )
        }
        entry["direct_split_quotes"] = _p50_max([r["direct_split_quotes"] for r in mine])
        entry["instances"] = len(mine)
        entry["all_complete_zero_gap"] = all(r["gap"] == "0" for r in mine)
        entry["all_equal_direct_split"] = all(r["equal_to_direct_split"] for r in mine)
        out[str(step)] = entry
    return out


def main(argv: Sequence[str]) -> None:
    if argv[:1] == ["probe"]:
        out = [probe(path, cohort) for path, cohort in zip(argv[1:-1:2], argv[2:-1:2], strict=True)]
        Path(argv[-1]).write_text(
            json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif argv[:1] == ["sweep"]:
        data = sweep()
        data["summary"] = sweep_summary(data["rows"])
        Path(argv[1]).write_text(
            json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif argv[:1] == ["raw-sweep"]:
        text = json.dumps(raw_sweep(), indent=1, sort_keys=True) + "\n"
        Path(argv[1]).write_text(text, encoding="utf-8")
    elif argv[:1] == ["examples"]:  # regenerate the published records (memo §7)
        for ex in FIX["examples"]:
            bundle, case = _example_inputs(ex)
            states = bundle.pools_for_pair(case.token_in, case.token_out)
            cps = [p for p in states if isinstance(p, ConstantProductPoolState)]
            raw = ex["options"]["domain"] == "raw_integer"
            units = case.amount_in if raw else 100 // ex["percent_step"]
            opt = None
            if cps and len(cps) == len(states):
                opt = optimum(
                    oracle_values(cps, case.token_in, case.amount_in, units, ex["max_splits"])
                )
            result = run_example(ex)
            ex["oracle_optimum"] = None if opt is None else str(opt)
            ex["record"], ex["status"] = result.record, result.status.value
            ex["final_score"] = None if result.score is None else str(result.score)
        for name in ("preset", "stress_raw"):
            FIX[name]["settings_sha256"] = settings_sha256(FIX[name]["options"])
        FIX["grid_vs_raw"]["summary"] = grid_vs_raw_summary()
        text = json.dumps(FIX, indent=1, ensure_ascii=False) + "\n"
        (R021 / "fixtures" / "integer-allocation.json").write_text(text, encoding="utf-8")
    else:
        raise SystemExit(
            "usage: probe <bundle> <cohort> [...] <out.json> | sweep <out.json> "
            "| raw-sweep <out.json> | examples"
        )


if __name__ == "__main__":
    main(sys.argv[1:])

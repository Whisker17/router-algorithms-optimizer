"""`direct_split_certified` (WHI-1552, R021-P06; contract R021-C/1 §2 row 2): an exact
best-first branch and bound over `direct_split`'s own allocation domain that returns an
ordinary `RoutePlan` **and** a validated same-domain value bound. Normative specification:
`docs/references/research-021/integer-allocation.md` §2-§7 (WHI-1551, `narrow_go`); its
executable form is `certify` in `tests/routing/test_integer_allocation_contract.py`, which
this module reproduces (same nodes, order, bounds and records). Experimental, `custom`
group; no speed, quote-saving, tie-identity or out-of-domain claim.

**Scope** (§2.1, applied in this order before any quote). The candidate pools are ALL of
`bundle.pools_for_pair` in admitted order -- no shortlist and no hidden subset:
objective not `gross_only` -> `unsupported` (`objective_not_gross_only`); any direct pool not
constant product -> `unsupported` (`non_constant_product_direct_pool`, no CPMM-subset
fallback); no direct pool -> `no_route` (the empty domain is complete); `raw_integer` above
`raw_max_amount_in` -> `unsupported` (`raw_integer_amount_above_limit`).

**Domains.** `repository_grid` is exactly `direct_split`'s grid: `N = 100 / percent_step`
units, at most `search.max_splits` legs on strictly increasing admitted pools, no non-final
leg flooring to 0, amounts `direct_split.leg_amounts` (the last leg takes the remainder) and
plans `direct_split.allocation_plan` (reused, not copied). Proposition P1: untruncated, its
value equals `direct_split`'s; the contribution is the certificate and an honest gap after a
stop, not a better value, and a tie may pick a different allocation. `raw_integer` (`N = A`)
is a separately identified, bounded stress domain (explicit profile only, never in `all`).

**Bounds** (§3-§4) are exact `int`/`fractions.Fraction` arithmetic (no float): every live CPMM
output without the final floor, `g(x) = keep*R_out*x / (R_in*10^4 + keep*x)`, is concave, lies
above the floored quote and below any tangent; a linear function over the relaxed simplex
(optionally with a box on one pool) is maximal at a vertex. Rule T (integer water-filling)
only picks the tangent points; any choice stays sound. A node's bound is its exact prefix
output plus the rule's floor. `final_bound` / `state_bound` / `interval_bound` return an
integer or `None` = unknown; an unknown node is never pruned, sorts first and makes the
upper bound unknown (`bound_kind: unknown`, null upper/gap -- never 0).

**Search** (§5). Root bound first, then the singles stage (every live pool at the full input,
admitted order; the best valid one is the first candidate) and the Rule H seed. A candidate
becomes the incumbent only if its `allocation_plan` replays `ok` through the in-solve
evaluator (same `QuoteCache`, no new quotes), its score equals its quoted additive value and
it is strictly better; every new incumbent is published (`report_candidate`). The frontier is
a heap keyed `(unknown first, -ub, creation sequence)`: deterministic best-first, FIFO among
equal bounds. Children of a node partition its region and are pruned at creation when
`ub <= L`; a top node with `ub <= L` prunes the whole frontier (`complete`). Stops:
`node_cap` (`max_bound_nodes` expansions), `state_cap` (the frontier would exceed
`max_open_nodes`), `quote_budget` (before a quote beyond `Budget.max_quotes`), `candidate_cap`
(before an evaluation beyond `Budget.max_candidates`); each re-pushes the node being expanded,
so every unresolved allocation stays in an open node (T1) and `U = max(L, open bounds)`.
`wall_budget` is not used: the worker's hard wall/quote kill returns no `SolveResult`, so no
certificate survives it (only the runner's `last_valid_candidate`).

**Fail closed** (§5.6). A candidate whose replay is not `ok` or disagrees with its quoted value
is never an incumbent or published; the search stops at once and no certificate is emitted.
The result is the earlier validated incumbent (`ok`, `truncated_by: consistency_failure`), or
`algorithm_error` without one -- never `no_route` or `timeout`.

**Accounting.** The whole search runs inside `routing.evaluator.counted_evaluations()`, so
`internal_evaluations` is the exact total of in-solve replays (it is also the
`Budget.max_candidates` unit `finalist_plans_evaluated`). `quotes_executed` is the
`QuoteCache` miss count (the worker meter), `quotes_memoized` its hits; `bb_nodes_expanded`,
`bound_evaluations` (one per node created; a bound never quotes) and `peak_open_nodes` are
separate units, never divided by quotes. `search_stats["r021"]` is the `r021.diagnostics/1`
record with the certificate (`source` = this run's git revision, bundle, algorithm and
`settings_sha256` of the options; `request` = the exact case), which the runner re-checks
against its own identities.

**Options** (`algorithm_options.direct_split_certified`, §6): `domain` (`repository_grid` |
`raw_integer`), `max_bound_nodes` and `max_open_nodes` (1..10,000,000), `raw_max_amount_in`
(1..1,000,000, required with `raw_integer`, refused otherwise); all required, no solver
defaults. Bounded preset v1 (`PRESET`): `repository_grid / 100000 / 100000`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import heapq
import itertools
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from types import MappingProxyType
from typing import Any

from benchmark.objective import ObjectiveContext
from pools.constant_product import FEE_DENOMINATOR as D
from pools.result import QuoteStatus
from routing.algorithms import direct_split
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
    option_choice,
    option_int,
    require_option_keys,
    settings_sha256,
    validated_options,
)
from routing.algorithms.direct_split import Leg, allocation_plan, leg_amounts
from routing.evaluator import EvalStatus, Evaluation, counted_evaluations, evaluate
from routing.plan import RoutePlan
from routing.search import QuoteCache
from snapshot.models import Case, ConstantProductPoolState, SnapshotBundle

NAME = "direct_split_certified"
REFERENCE = direct_split.NAME

# direct_split's shapes with the constant-product ceiling of its scope (WHI-1605, R1-F1):
# any other direct pool is `unsupported`, so it never matches an unrestricted identity.
CAPABILITIES = dataclasses.replace(direct_split.CAPABILITIES, protocols=("constant_product",))
SEARCH_PARAMS = direct_split.SEARCH_PARAMS  # ("max_splits", "percent_step")

DOMAINS = ("repository_grid", "raw_integer")
OPTION_RANGES: dict[str, tuple[int, int]] = {
    "max_bound_nodes": (1, 10_000_000),
    "max_open_nodes": (1, 10_000_000),
    "raw_max_amount_in": (1, 1_000_000),
}
MAX_CANDIDATES_UNIT = "finalist_plans_evaluated"
# The bounded comparison preset (R021-C/1 §7.1, integer-allocation.md §6), frozen by its bytes.
PRESET: dict[str, Any] = {
    "path": "config/direct_split_certified/preset_v1.yaml",
    "sha256": "d2653303f33d8bd9391c5ee6ae564c2c1fa83366a548063691e87d148e23c70f",
    "key": "R021-P06-direct_split_certified",
    "version": 1,
}

PROVENANCE: Mapping[str, Any] = MappingProxyType(
    {
        "experimental": True,
        "opt_in": True,
        "issue": "WHI-1552",
        "identity": (
            "certified integer branch and bound over direct_split's allocation domain "
            "(repository_grid) or the separately identified raw_integer stress domain; "
            "value certificate within the declared domain only"
        ),
        "contract": (
            "docs/references/research-021/integer-allocation.md §2-§7 (WHI-1551, R021-P05, "
            "narrow_go; PR #60 1ce50763b84b7daf4eec844665848b5b1c27fb6a)"
        ),
        "reference": REFERENCE,
        "scope": "all admitted direct pools constant product, objective gross_only",
        "not_claimed": [
            "a better same-domain value than direct_split (Proposition P1: equal values)",
            "direct_split's tie allocation",
            "optimality outside the declared domain (pool order, grid, cardinality, hops, "
            "protocol or objective)",
            "concentrated / liquidity-book / net-objective coverage (unsupported rows)",
            "any speedup or quote saving",
        ],
    }
)


class DirectSplitCertifiedConfigError(ValueError):
    """`prepare` received an invalid or missing `search.max_splits` / `search.percent_step`."""


def validate_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """The `options_validator` (integer-allocation.md §6): `domain`, `max_bound_nodes` and
    `max_open_nodes` required; `raw_max_amount_in` required with `raw_integer` and refused
    otherwise; unknown keys refused (reserved keys are refused by `validated_options` first);
    the integers never bools, floats or out of range."""
    raw = options.get("domain") == "raw_integer"
    limit = {"raw_max_amount_in"}
    require_option_keys(
        options, {"domain", "max_bound_nodes", "max_open_nodes", *(limit if raw else ())}
    )
    out: dict[str, Any] = {"domain": option_choice(options["domain"], "domain", DOMAINS)}
    for key, (lo, hi) in OPTION_RANGES.items():
        if key in options:
            out[key] = option_int(options[key], key, lo, hi)
    return out


@dataclass(frozen=True)
class PreparedCertified:
    """Immutable per-worker preparation: `direct_split`'s validated grid settings and the
    validated read-only options with their §9.3 settings hash."""

    grid: direct_split.PreparedDirectSplit
    options: Mapping[str, Any]
    settings_sha256: str

    @property
    def max_splits(self) -> int:
        return self.grid.max_splits

    @property
    def percent_step(self) -> int:
        return self.grid.percent_step


def prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> PreparedCertified:
    options = validated_options(FACTORY, config.options)  # the public entry validates too
    try:  # the reused legacy prepare refuses options: it gets the same config without them
        grid = direct_split.prepare(bundle, dataclasses.replace(config, options={}))
    except direct_split.DirectSplitConfigError as exc:
        raise DirectSplitCertifiedConfigError(f"{NAME}: {exc}") from exc
    return PreparedCertified(grid, MappingProxyType(dict(options)), settings_sha256(options))


# ------------------------------------------------------------------ bounds (§3-§4)


@dataclass(frozen=True, slots=True)
class PoolView:
    """One direct pool's relaxation data for the request direction: `g(x) = keep*r_out*x /
    (r_in*D + keep*x)`, the CPMM output without the final floor (`keep = D - fee_bps`)."""

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
    """Rule T (§4): integer tangent points of the continuous water-filling
    `x_i = max(0, mu*beta_i - alpha_i)`, `alpha = D*r_in/keep`, `beta ~ sqrt(r_out*alpha)` (an
    integer square root at scale 2**64). A hint only: any `t >= 0` gives a valid bound."""
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
    """Exact upper bound of `max sum g_i(x_i)` over `x >= 0`, `sum x = total` (and `x_0` in
    `box` when given): each concave `g_i` lies below its tangent at `t_i` (L2), and a linear
    function over that polytope is maximal at a vertex (L3)."""
    t = tangent_hint(pools, total)
    if box is not None:
        t[0] = min(max(t[0], box[0]), box[1])
    s = [dg(p, x) for p, x in zip(pools, t, strict=True)]
    c = sum((g(p, x) - si * x for p, x, si in zip(pools, t, s, strict=True)), Fraction(0))
    if box is None:
        return c + max(s) * total
    rest = max(s[1:])
    return c + max(s[0] * x + rest * (total - x) for x in box)


def final_bound(p: PoolView, amount: int) -> int | None:
    """One allocation: `floor(g(amount))` (L1)."""
    return math.floor(g(p, amount))


def state_bound(pools: Sequence[PoolView], total: int, legs_left: int) -> int | None:
    """Every completion over `pools` (live, >= j) of `total` raw input: one leg left is the
    single best pool (L5), otherwise the tangent relaxation (L4)."""
    if legs_left == 1:
        return math.floor(max(g(p, total) for p in pools))
    return math.floor(tangent_bound(pools, total, None))


def interval_bound(
    p: PoolView, rest: Sequence[PoolView], total: int, lo: int, hi: int, legs_left: int
) -> int | None:
    """Pool `p` non-final with raw input in `[lo, hi]`, then `rest`: with one leg left after
    `p` the best pair (cardinality respected, L5), otherwise the boxed relaxation."""
    if legs_left == 1:
        return max(math.floor(tangent_bound([p, q], total, (lo, hi))) for q in rest)
    return math.floor(tangent_bound([p, *rest], total, (lo, hi)))


def rounded_relaxation(
    views: Sequence[PoolView], live: Sequence[bool], amount: int, units: int, max_legs: int
) -> tuple[Leg, ...] | None:
    """Rule H (§5.2): the `max_legs` pools with the largest positive Rule-T hint (ties by
    admitted order), their units `floor(t*units/amount)` in admitted order, the last kept
    pool taking the remaining units; legs that round to no unit or to a zero non-final
    amount are dropped. `None` when fewer than two legs remain (the singles cover it)."""
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


# ------------------------------------------------------------------ the search (§5)


@dataclass(frozen=True, slots=True)
class Node:
    """A frontier node (§5.1). `state`: the legs on pools < j are fixed (`legs`, `used` units,
    `fl` raw input, `gp` exact output) and a final leg is still to come on a pool >= j.
    `interval`: additionally pool j is a non-final leg with units in [lo, hi]. `final`: pool
    j is the final leg with every remaining unit. `ub` None = unknown."""

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
    """One search: the `SolveResult` plus, for audits, the open frontier at the end and (when
    requested) every created node."""

    result: SolveResult
    frontier: list[Node]
    units: int
    trace: list[Node] | None = field(default=None, repr=False)


class _Stop(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


class _Inconsistent(Exception):
    """A candidate's replay disagrees with its quoted additive value (§5.6)."""

    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail


WORK_UNITS = (
    "quotes_executed",
    "quotes_memoized",
    "internal_evaluations",
    "bb_nodes_expanded",
    "bound_evaluations",
    "peak_open_nodes",
)


def cohort_of(bundle: SnapshotBundle) -> str:
    """The domain universe's cohort (R021-C/1 §3.1): `fixture` for a synthetic bundle, the
    corpus descriptor's cohort for a matched-cohort cut or the cohort a single-request
    bundle records it was cut from (WHI-1606, read at load time), else `full_source`."""
    if bundle.kind == "synthetic":
        return "fixture"
    cohort = (bundle.corpus or {}).get("cohort") or bundle.derived_cohort
    return "sor_compatible" if cohort == "sor_compatible" else "full_source"


def domain_record(
    bundle_ref: str, cohort: str, pool_ids: Sequence[str], max_splits: int, step: int | None
) -> dict[str, Any]:
    """`r021.domain/1` of the request's direct pool set (§2.4); `step` None = `raw_integer`."""
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


def certify(
    bundle: SnapshotBundle,
    case: Case,
    objective: ObjectiveContext,
    prepared: PreparedCertified,
    budget: Budget,
    *,
    git_revision: str | None,
    cohort: str,
    sink: Callable[[RoutePlan], None] | None = None,
    trace: list[Node] | None = None,
) -> Certified:
    """The whole solve (§5); `solve` calls exactly this. `trace`, when given, receives every
    created node (audits only; the registered solve passes none)."""
    with counted_evaluations() as evaluations:
        return _certify(
            bundle, case, objective, prepared, budget, git_revision, cohort, sink, trace,
            lambda: evaluations.count,
        )  # fmt: skip


def _certify(
    bundle: SnapshotBundle,
    case: Case,
    objective: ObjectiveContext,
    prepared: PreparedCertified,
    budget: Budget,
    git_revision: str | None,
    cohort: str,
    sink: Callable[[RoutePlan], None] | None,
    trace: list[Node] | None,
    evaluated: Callable[[], int],
) -> Certified:
    opts = prepared.options
    raw = opts["domain"] == "raw_integer"
    max_splits, step = prepared.max_splits, prepared.percent_step
    token_in, amount_in = case.token_in, case.amount_in
    states = bundle.pools_for_pair(token_in, case.token_out)
    ids = tuple(p.pool_id for p in states)
    domain = domain_record(bundle.bundle_hash, cohort, ids, max_splits, None if raw else step)
    work = dict.fromkeys(WORK_UNITS, 0)
    record: dict[str, Any] = {
        "schema": "r021.diagnostics/1",
        "contract": "R021-C/1",
        "algorithm": NAME,
        "domain": domain,
        "candidate_domain_hash": hashlib.sha256(
            json.dumps(domain, sort_keys=True).encode()
        ).hexdigest(),
        "certificate": None,
        "certificate_unavailable_reason": "not_produced",
        "max_candidates_unit": MAX_CANDIDATES_UNIT,
        "work": work,
        "scope": {"supported": True, "reason": None},
    }
    stats: dict[str, Any] = {
        "domain": opts["domain"],
        **{key: opts[key] for key in OPTION_RANGES if key in opts},
        "max_splits": max_splits,
        "percent_step": None if raw else step,
        "grid_units": None,
        "direct_pools": len(states),
        "live_pools": None,
        "termination": None,
        "truncated_by": None,
        "nodes_pruned_bound": 0,
        "consistency_failure": None,
        "best_splits": None,
        "best_allocation": None,
        "r021": record,
    }

    def result(
        status: SolveStatus,
        *,
        error: str | None = None,
        best: tuple[int, RoutePlan, Evaluation, tuple[Leg, ...]] | None = None,
        truncated: int = 0,
    ) -> SolveResult:
        return SolveResult(
            case_id=case.case_id,
            algorithm=NAME,
            status=status,
            plan=None if best is None else best[1],
            evaluation=None if best is None else best[2],
            score=None if best is None else best[0],
            candidates_considered=work["internal_evaluations"],
            candidates_truncated=truncated,
            error=error,
            search_stats=stats,
        )

    def out_of_scope(status: SolveStatus, reason: str | None, error: str) -> Certified:
        if reason is not None:
            record["scope"] = {"supported": False, "reason": reason}
        return Certified(result(status, error=error), [], 0, trace)

    pair = f"{token_in}/{case.token_out}"
    if objective.mode != "gross_only":
        return out_of_scope(
            SolveStatus.UNSUPPORTED,
            "objective_not_gross_only",
            f"objective {objective.mode} is outside {NAME}'s gross_only ceiling (no net bound)",
        )
    if any(not isinstance(p, ConstantProductPoolState) for p in states):
        return out_of_scope(
            SolveStatus.UNSUPPORTED,
            "non_constant_product_direct_pool",
            f"{pair} has a non-constant-product direct pool; the whole case is unsupported "
            "(no CPMM-subset certificate)",
        )
    if not states:
        return out_of_scope(
            SolveStatus.NO_ROUTE, None, f"no admitted direct pool for {pair} (empty domain)"
        )
    if raw and amount_in > opts["raw_max_amount_in"]:
        return out_of_scope(
            SolveStatus.UNSUPPORTED,
            "raw_integer_amount_above_limit",
            f"amount_in {amount_in} > raw_max_amount_in {opts['raw_max_amount_in']}",
        )

    cps = [p for p in states if isinstance(p, ConstantProductPoolState)]
    n, units = len(cps), (amount_in if raw else 100 // step)
    max_legs = min(max_splits, n)
    views = [PoolView(*p.reserves_for(token_in), D - p.fee_bps) for p in cps]
    live = [v.live for v in views]
    stats.update(grid_units=units, live_pools=sum(live))
    cache = QuoteCache(bundle)
    outputs: dict[tuple[int, int], int | None] = {}
    best: list[tuple[int, RoutePlan, Evaluation, tuple[Leg, ...]]] = []
    heap: list[tuple[tuple[int, int, int], Node]] = []
    seq = itertools.count()

    def lower() -> int | None:
        return best[-1][0] if best else None

    def quote(i: int, amount: int) -> int | None:
        key = (i, amount)
        if key not in outputs:
            if budget.max_quotes is not None and cache.misses >= budget.max_quotes:
                raise _Stop("quote_budget")
            res = cache(cps[i], token_in, amount)
            ok = res.status is QuoteStatus.OK and res.amount_in_consumed == amount
            outputs[key] = res.amount_out if ok else None
        return outputs[key]

    def consider(legs: tuple[Leg, ...], value: int) -> None:
        current = lower()
        if current is not None and value <= current:
            return
        if budget.max_candidates is not None and evaluated() >= budget.max_candidates:
            raise _Stop("candidate_cap")
        plan = allocation_plan(case, ids, legs)
        evaluation = evaluate(bundle, case, plan, objective, quote=cache)
        score = objective.score(evaluation) if evaluation.status is EvalStatus.OK else None
        if score != value:  # fail closed: never an incumbent, never published (§5.6)
            raise _Inconsistent(
                {
                    "legs": [list(leg) for leg in legs],
                    "quoted_value": str(value),
                    "evaluation_status": evaluation.status.value,
                    "evaluated_score": None if score is None else str(score),
                }
            )
        if current is None or score > current:
            best.append((score, plan, evaluation, legs))
            if sink is not None:
                sink(plan)

    def bounded(value: int | None) -> int | None:
        work["bound_evaluations"] += 1
        return value

    def make(
        kind: str, j: int, used: int, fl: int, gp: int, legs: tuple[Leg, ...], **iv: int
    ) -> Node:
        rest = amount_in - fl
        left = max_legs - len(legs)
        ub: int | None
        if kind == "final":
            ub = bounded(final_bound(views[j], rest))
        elif kind == "state":
            ub = bounded(state_bound([views[i] for i in range(j, n) if live[i]], rest, left))
        else:
            after = [views[i] for i in range(j + 1, n) if live[i]]
            lo_amt, hi_amt = amount_in * iv["lo"] // units, amount_in * iv["hi"] // units
            ub = bounded(interval_bound(views[j], after, rest, lo_amt, hi_amt, left - 1))
        node = Node(kind, j, used, fl, gp, legs, None if ub is None else gp + ub, **iv)
        if trace is not None:
            trace.append(node)
        return node

    def children_of_state(node: Node) -> list[Node]:
        j, used, fl, gp, legs = node.j, node.used, node.fl, node.gp, node.legs
        later = any(live[j + 1 :])
        kids = []
        if live[j]:
            kids.append(make("final", j, used, fl, gp, legs))
        lo, hi = max(1, -(-units // amount_in)), units - used - 1
        if live[j] and later and len(legs) + 1 <= max_legs - 1 and lo <= hi:
            kids.append(make("interval", j, used, fl, gp, legs, lo=lo, hi=hi))
        if later:
            kids.append(make("state", j + 1, used, fl, gp, legs))
        return kids

    def expand(node: Node) -> list[Node]:
        if node.kind == "final":
            out = quote(node.j, amount_in - node.fl)
            if out is not None:
                consider((*node.legs, (node.j, units - node.used)), node.gp + out)
            return []
        if node.kind == "interval":
            shape = (node.j, node.used, node.fl, node.gp, node.legs)
            if node.lo < node.hi:
                mid = (node.lo + node.hi) // 2
                return [
                    make("interval", *shape, lo=node.lo, hi=mid),
                    make("interval", *shape, lo=mid + 1, hi=node.hi),
                ]
            amount = amount_in * node.lo // units
            out = quote(node.j, amount)
            if out is None:  # the fixed leg is in every completion: the region is closed
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
    failure: dict[str, Any] | None = None
    if any(live):  # the root bound covers the whole domain (§5.2 step 1)
        root = make("state", 0, 0, 0, 0, ())
        heap.append((key(root), root))
        work["peak_open_nodes"] = 1
    singles: list[tuple[int, int]] = []
    try:  # every live pool at the full input, in admitted order (dead pools never quoted)
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
            amounts = leg_amounts(amount_in, seed, units)
            outs = [quote(i, a) for (i, _), a in zip(seed, amounts, strict=True)]
            if None not in outs:
                consider(seed, sum(o for o in outs if o is not None))
    except _Stop as stop:
        termination = termination or stop.reason
    except _Inconsistent as bad:
        failure = bad.detail
    while termination is None and failure is None and heap:
        entry = heap[0]
        if prunable(entry[1]):  # every remaining bound is <= L (unknown nodes sort first)
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
        except _Stop as stop:  # cooperative stop: the popped node's region stays open
            heapq.heappush(heap, entry)
            termination = stop.reason
            break
        except _Inconsistent as bad:
            failure = bad.detail
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
    work["internal_evaluations"] = evaluated()
    frontier = [node for _, node in heap]
    stats.update(truncated_by=termination, consistency_failure=failure)
    truncated = int(termination == "candidate_cap")
    if best:
        legs = best[-1][3]
        stats["best_splits"] = len(legs)
        stats["best_allocation"] = [
            {
                "pool_id": ids[i],
                "units": u,
                "percent": None if raw else u * step,
                "amount_in": str(a),
            }
            for (i, u), a in zip(legs, leg_amounts(amount_in, legs, units), strict=True)
        ]
    if failure is not None:  # §5.6: no certificate, no completion, no no_route claim
        stats["truncated_by"] = "consistency_failure"
        if not best:
            return Certified(
                result(
                    SolveStatus.ALGORITHM_ERROR,
                    error=f"evaluator replay disagrees with the quoted accounting: {failure}",
                ),
                frontier,
                units,
                trace,
            )
        return Certified(result(SolveStatus.OK, best=best[-1]), frontier, units, trace)
    if not best:
        status = SolveStatus.NO_ROUTE if termination is None else SolveStatus.TIMEOUT
        error = (
            f"complete search: no allocation over the {n} direct pool(s) of {pair} is feasible "
            f"({sum(live)} live)"
            if termination is None
            else f"declared {termination} stop before any valid allocation -- not evidence of "
            "no_route"
        )
        return Certified(result(status, error=error, truncated=truncated), frontier, units, trace)
    score = best[-1][0]
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
            "algorithm": NAME,
            "effective_settings_sha256": prepared.settings_sha256,
        },
        "request": {
            "case_id": case.case_id,
            "token_in": token_in,
            "token_out": case.token_out,
            "amount_in": str(amount_in),
        },
        "lower_raw": str(score),
        "upper_raw": str(upper) if upper is not None else None,
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
    stats["termination"] = termination or "complete"
    return Certified(
        result(SolveStatus.OK, best=best[-1], truncated=truncated), frontier, units, trace
    )


def solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    prepared = context.prepared
    if not isinstance(prepared, PreparedCertified):
        raise TypeError(f"{NAME}.solve needs the PreparedCertified returned by prepare()")
    revision = context.run_identity.get("git_revision")
    return certify(
        context.bundle,
        case,
        context.objective,
        prepared,
        budget,
        git_revision=revision if isinstance(revision, str) else None,
        cohort=cohort_of(context.bundle),
        sink=context.report_candidate,
    ).result


FACTORY = AlgorithmFactory(
    name=NAME,
    solve=solve,
    prepare=prepare,
    capabilities=CAPABILITIES,
    search_params=SEARCH_PARAMS,
    provenance=PROVENANCE,
    options_validator=validate_options,
    options_preset=MappingProxyType(PRESET),
)

__all__ = [
    "FACTORY",
    "NAME",
    "PRESET",
    "Certified",
    "DirectSplitCertifiedConfigError",
    "Node",
    "PoolView",
    "PreparedCertified",
    "certify",
    "prepare",
    "solve",
    "validate_options",
]

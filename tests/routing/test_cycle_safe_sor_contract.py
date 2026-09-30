# SPDX-License-Identifier: GPL-3.0-only
#
# `select_cycle_safe` below is adapted from routing/algorithms/uni_sor_port.py
# `get_best_swap_route_by`, the translation of Uniswap/smart-order-router@
# 04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647 (4.31.10)
# src/routers/alpha-router/functions/best-swap-route.ts:174-881 (GPL-3.0; verbatim text at
# docs/references/licenses/uniswap-smart-order-router-04c7c0b4-LICENSE.txt). MODIFIED: the
# B-S7/B-S9 chooser additionally requires the plan token graph of the combination to stay
# acyclic (docs/references/research-021/cycle-safe-sor.md, WHI-1555). Private internal
# research use; not conveyed (uni-sor-port-contract.md §9.3).
"""R021-P09 (WHI-1555): the cycle-safe SOR selection contract for the future
`uni_sor_cycle_safe` (docs/references/research-021/cycle-safe-sor.md).

Research evidence, not a strategy. Three things are kept apart:

1. **The executable specification** (`select_cycle_safe`, `spec_solve`): the pinned SOR
   combination with one added admission rule at the B-S7/B-S9 chooser, run inside the
   *unchanged* `uni_sor_port` adapter (enumeration, grid, quote table, B-F1 order, D-1
   fill, D-3 replay) by substituting only the selector, plus the variant's own
   publication rule (publish after a valid replay). It is never registered or timed.
2. **Independent expectations**: hand-traced core fixtures, a hand integer
   `getAmountOut` formula, an exhaustive combination oracle with its own (Kahn)
   token-cycle test, and the unchanged evaluator's static check.
3. **Reference reproductions**: the actual `uni_sor_port.solve` / core on the same inputs,
   so every difference is attributable to the admission rule.

`uv run python tests/routing/test_cycle_safe_sor_contract.py probe <bundle> <out.json>
[case ids...]` is the bounded diagnostic of cycle-safe-sor.md §9 (a separate pass; nothing
here is a measured solve and no timing is claimed).
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import importlib.util
import itertools
import json
import random
import sys
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, Literal
from unittest import mock

import pytest

from benchmark.objective import gross_only
from pools.quote import metered_quotes
from routing.algorithms import uni_sor_port as sor
from routing.algorithms.base import (
    AlgorithmConfig,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
)
from routing.algorithms.path_split import split_path_plan
from routing.evaluator import EvalStatus, evaluate
from routing.plan import RoutePlan
from routing.search import Edge
from snapshot.bundle import load_bundle
from snapshot.models import (
    BlockRef,
    Case,
    ConstantProductPoolState,
    PoolState,
    SnapshotBundle,
)

REPO = Path(__file__).resolve().parents[2]
R021 = REPO / "docs" / "references" / "research-021"
FIX = json.loads((R021 / "fixtures" / "cycle-safe-sor.json").read_text(encoding="utf-8"))
MEMO = (R021 / "cycle-safe-sor.md").read_text(encoding="utf-8")
SOR_GOLDENS = REPO / "tests" / "fixtures" / "uni_sor"
CORPUS_FIXTURE = REPO / "tests" / "fixtures" / "corpus" / "bundle"
BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)
NAME = "uni_sor_cycle_safe"
EMPTY_SETTINGS_SHA = hashlib.sha256(json.dumps({}, sort_keys=True).encode()).hexdigest()

Counters = dict[str, int]
Policy = Literal["continue", "skip_percent"]
Admission = Literal["union", "pairwise"]


# ================================================================ 1. specification


def route_edges(rq: sor.RouteQuote) -> tuple[tuple[str, str], ...]:
    """The directed token edges one route adds to the plan token graph (the evaluator's
    `token_in -> token_out` per step, in the SOR's token identity)."""
    path = rq.route.token_path
    return tuple(zip(path, path[1:], strict=False))


def union_has_cycle(routes: Sequence[sor.RouteQuote]) -> bool:
    """Admission test: does the union of the routes' token edges contain a directed cycle?
    Iterative three-colour DFS in sorted order (deterministic)."""
    edges: dict[str, set[str]] = {}
    for rq in routes:
        for a, b in route_edges(rq):
            edges.setdefault(a, set()).add(b)
    colour: dict[str, int] = {}
    for root in sorted(edges):
        if root in colour:
            continue
        colour[root] = 1
        stack = [iter(sorted(edges.get(root, ())))]
        path = [root]
        while stack:
            nxt = next(stack[-1], None)
            if nxt is None:
                colour[path.pop()] = 2
                stack.pop()
            elif colour.get(nxt) == 1:
                return True
            elif nxt not in colour:
                colour[nxt] = 1
                path.append(nxt)
                stack.append(iter(sorted(edges.get(nxt, ()))))
    return False


def first_admissible(
    cur_routes: Sequence[sor.RouteQuote],
    candidates: Sequence[sor.RouteQuote],
    counters: Counters,
    *,
    policy: Policy = "continue",
    admission: Admission = "union",
) -> sor.RouteQuote | None:
    """The cycle-safe B-S7/B-S9 chooser (memo §4.2): scan the percent's sorted group in
    B-S2 order; the pool rule (B-S9) runs first and a pool conflict costs no admission
    check; a pool-disjoint entry is admitted iff the union of the node's routes and the
    entry is acyclic; a rejected entry is counted and the scan continues. `policy` and
    `admission` exist only for the mutation tests (never part of the contract)."""
    used = {pid for r in cur_routes for pid in r.pool_identifiers}
    for cand in candidates:
        if any(pid in used for pid in cand.pool_identifiers):
            continue
        counters["admission_checks"] += 1
        if admission == "union":
            cyclic = union_has_cycle((*cur_routes, cand))
        else:
            cyclic = any(union_has_cycle((r, cand)) for r in cur_routes)
        if not cyclic:
            return cand
        counters["combinations_rejected_cycle"] += 1
        if policy == "skip_percent":
            return None
    return None


@dataclasses.dataclass(frozen=True)
class _Node:
    cur_routes: tuple[sor.RouteQuote, ...]
    percent_index: int
    remaining_percent: int
    special: bool


def select_cycle_safe(
    percent_to_quotes: Mapping[int, Sequence[sor.RouteQuote]],
    percents: Sequence[int],
    *,
    min_splits: int,
    max_splits: int,
    by: Callable[[sor.RouteQuote], int] = lambda r: r.quote_adjusted_for_gas,
    counters: Counters,
    policy: Policy = "continue",
    admission: Admission = "union",
) -> sor.BestSwap | None:
    """`getBestSwapRouteBy` (B-S2...B-S12, B-F1) with the admission rule at its only
    combination point. Everything else -- sort, baseline, seeds, layers, pruning, split
    cap, strict improvement, final V8 order and cached totals -- is the port's, line for
    line. Baseline and seeds are single simple routes and need no check (memo L1)."""
    sorted_groups = {
        percent: tuple(sorted(quotes, key=lambda r: -by(r)))
        for percent, quotes in percent_to_quotes.items()
    }
    best_quote: int | None = None
    best_swap: tuple[sor.RouteQuote, ...] | None = None
    if 100 in sorted_groups and min_splits <= 1:
        best_quote = by(sorted_groups[100][0])
        best_swap = (sorted_groups[100][0],)
    queue: deque[_Node] = deque()
    for i in range(len(percents), -1, -1):
        if i >= len(percents) or percents[i] not in sorted_groups:
            continue
        group = sorted_groups[percents[i]]
        queue.append(_Node((group[0],), i, 100 - percents[i], False))
        if len(group) < 2:
            continue
        queue.append(_Node((group[1],), i, 100 - percents[i], True))
    splits = 1
    while queue:
        layer = len(queue)
        splits += 1
        if splits >= 3 and best_swap is not None and len(best_swap) < splits - 1:
            break
        if splits > max_splits:
            break
        while layer > 0:
            layer -= 1
            node = queue.popleft()
            for i in range(node.percent_index, -1, -1):
                percent_a = percents[i]
                if percent_a > node.remaining_percent or percent_a not in sorted_groups:
                    continue
                route_a = first_admissible(
                    node.cur_routes,
                    sorted_groups[percent_a],
                    counters,
                    policy=policy,
                    admission=admission,
                )
                if route_a is None:
                    continue
                remaining_new = node.remaining_percent - percent_a
                routes_new = (*node.cur_routes, route_a)
                if remaining_new == 0 and splits >= min_splits:
                    quote_new = sum(by(r) for r in routes_new)
                    if best_quote is None or quote_new > best_quote:
                        best_quote = quote_new
                        best_swap = routes_new
                else:
                    queue.append(_Node(routes_new, i, remaining_new, node.special))
    if best_swap is None:
        return None
    final = sor.v8_small_array_sort(
        best_swap, lambda a, b: 1 if b.amount.greater_than(a.amount) else -1
    )
    return sor.BestSwap(
        routes=tuple(final),
        quote=sum(r.quote for r in best_swap),
        quote_gas_adjusted=sum(r.quote_adjusted_for_gas for r in best_swap),
        estimated_gas_used=sum(r.gas.gas_estimate for r in best_swap),
        estimated_gas_used_quote_token=sum(r.gas.gas_cost_in_token for r in best_swap),
        estimated_gas_used_usd=sum(r.gas.gas_cost_in_usd for r in best_swap),
        sorted_by_percent=MappingProxyType(sorted_groups),
    )


def new_counters() -> Counters:
    return {"admission_checks": 0, "combinations_rejected_cycle": 0}


def cycle_safe_selection(
    amount: int,
    percents: Sequence[int],
    quotes: Sequence[sor.RouteQuote],
    *,
    max_splits: int,
    min_splits: int = sor.MIN_SPLITS,
    counters: Counters,
    **mutation: Any,
) -> sor.SwapSelection | None:
    """`getBestSwapRoute` (B-S1 grouping, B-F2) unchanged, with the selector swapped."""
    selector = functools.partial(select_cycle_safe, counters=counters, **mutation)
    with mock.patch.object(sor, "get_best_swap_route_by", selector):
        return sor.get_best_swap_route(
            amount, percents, quotes, min_splits=min_splits, max_splits=max_splits
        )


def spec_solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    """The adapter contract (memo §4.3-§4.6): the unchanged `uni_sor_port` pipeline with
    the cycle-safe selector; a candidate is published only after the in-solve replay
    returned `ok`; `no_route` after rejections names the admission; `search_stats`
    carries the `cycle_safe` block and the R021-C/1 diagnostics."""
    counters = new_counters()
    buffered: list[RoutePlan] = []
    inner = dataclasses.replace(context, candidate_sink=buffered.append)
    selector = functools.partial(select_cycle_safe, counters=counters)
    with mock.patch.object(sor, "get_best_swap_route_by", selector):
        res = sor.solve(case, inner, budget)
    if res.status is SolveStatus.OK and res.plan is not None:
        context.report_candidate(res.plan)
    error = res.error
    no_admissible = (
        res.status is SolveStatus.NO_ROUTE
        and res.search_stats.get("route_quotes", 0) > 0
        and counters["combinations_rejected_cycle"] > 0
    )
    if no_admissible:
        error = (
            f"no admissible complete selection over {res.search_stats['route_quotes']} valid "
            f"quote entries: {counters['combinations_rejected_cycle']} combinations rejected "
            "by plan-token-DAG admission (B-S10 under admission)"
        )
    stats = dict(res.search_stats)
    stats["cycle_safe"] = {
        "admission": "plan_token_dag at the B-S7/B-S9 chooser (continue scan)",
        "admission_checks": counters["admission_checks"],
        "combinations_rejected_cycle": counters["combinations_rejected_cycle"],
        "no_admissible_selection": no_admissible,
        "reference_trajectory": counters["combinations_rejected_cycle"] == 0,
        "fallback": {"used": False},
        "published_before_replay": False,
    }
    out = dataclasses.replace(res, algorithm=NAME, error=error, search_stats=stats)
    stats["r021"] = diagnostics(case, context, out, counters)
    return out


def sor_domain(bundle: SnapshotBundle, prepared: sor.PreparedUniSorPort) -> dict[str, Any]:
    """The `r021.domain/1` record (memo §5). Identical for `uni_sor_port` and the variant:
    the same candidates, grid, split and hop bounds, and the evaluator's feasible set."""
    order = [p.pool_id for p in (*prepared.v3_pools, *prepared.v2_pools)]
    return {
        "schema": "r021.domain/1",
        "universe": {"bundle": bundle.bundle_hash, "cohort": "sor_compatible", "pools": order},
        "protocols": ["constant_product", "concentrated"],
        "pool_order": order,
        "hops": {"max": prepared.max_hops, "param": "search.max_hops"},
        "splits": {
            "max": prepared.max_splits,
            "param": "search.max_splits",
            "governs": "allocation",
        },
        "amount_grid": {
            "kind": "repository_grid",
            "percent_step": prepared.percent_step,
            "remainder": "sor_d1_last_b_f1_route_all_remaining",
            "route_order": "sor_a1_b_r1_b_q1",
        },
        "zero_output_leg": "infeasible",
        "token_reuse": "simple_path",
        "pool_reuse": "disjoint",
        "dag_admission": "plan_token_dag",
        "full_fill": "v1_full_fill",
    }


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


GIT_REVISION = "0" * 40  # the runner supplies the real revision; fixtures use a placeholder


def diagnostics(
    case: Case, context: SolveContext, res: SolveResult, counters: Counters
) -> dict[str, Any]:
    prepared = context.prepared
    assert isinstance(prepared, sor.PreparedUniSorPort)
    domain = sor_domain(context.bundle, prepared)
    domain_hash = canonical_hash(domain)
    stats = res.search_stats
    work = {
        "quotes_executed": int(stats.get("quotes_executed", 0)),
        "quotes_memoized": int(stats.get("quotes_memoized", 0)),
        "internal_evaluations": 1 if stats.get("allocation") is not None else 0,
        "admission_checks": counters["admission_checks"],
        "combinations_rejected_cycle": counters["combinations_rejected_cycle"],
    }
    certificate: dict[str, Any] | None = None
    reason: str | None = "not_produced"
    if res.status is SolveStatus.OK and res.score is not None:
        reason = None
        certificate = {
            "schema": "r021.certificate/1",
            "candidate_domain_hash": domain_hash,
            "objective": context.objective.mode,
            "source": {
                "git_revision": GIT_REVISION,
                "bundle_hash": context.bundle.bundle_hash,
                "algorithm": NAME,
                "effective_settings_sha256": EMPTY_SETTINGS_SHA,
            },
            "request": {
                "case_id": case.case_id,
                "token_in": case.token_in,
                "token_out": case.token_out,
                "amount_in": str(case.amount_in),
            },
            "lower_raw": str(res.score),
            "upper_raw": None,
            "gap_raw": None,
            "bound_kind": "unknown",
            "upper_source": None,
            "estimate": None,
            "optimality_proven": False,
            "termination": "complete",
        }
    return {
        "schema": "r021.diagnostics/1",
        "contract": "R021-C/1",
        "algorithm": NAME,
        "domain": domain,
        "candidate_domain_hash": domain_hash,
        "certificate": certificate,
        "certificate_unavailable_reason": reason,
        "max_candidates_unit": "enumerated_routes_threshold",
        "work": work,
        "fallback": {"used": False, "source": None, "reason": None},
        "scope": {"supported": True, "reason": None},
    }


def validate_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """The option validator of memo §7: no option exists; every key is refused."""
    if not isinstance(options, Mapping):
        raise TypeError(f"expected a mapping, got {type(options).__name__}")
    if options:
        raise ValueError(
            f"{sorted(map(str, options))}: {NAME} has no algorithm_options "
            "(candidates, grid, splits and hops come from the shared profile values)"
        )
    return {}


# ================================================================ 2. independent checks


def kahn_acyclic(edges: Sequence[tuple[str, str]]) -> bool:
    """Independent token-cycle test (Kahn's topological sort), used by the oracle."""
    succ: dict[str, set[str]] = {}
    indeg: dict[str, int] = {}
    for a, b in set(edges):
        succ.setdefault(a, set()).add(b)
        indeg.setdefault(a, 0)
        indeg[b] = indeg.get(b, 0) + 1
    ready = [n for n, d in indeg.items() if d == 0]
    seen = 0
    while ready:
        n = ready.pop()
        seen += 1
        for m in succ.get(n, ()):
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
    return seen == len(indeg)


def cp_out(x: int, r_in: int, r_out: int, fee_bps: int = 30) -> int:
    """Hand `getAmountOut` (Uniswap V2 / MoeLibrary), independent of `pools/`."""
    keep = x * (10000 - fee_bps)
    return keep * r_out // (r_in * 10000 + keep)


@dataclasses.dataclass(frozen=True)
class OracleRoute:
    rid: str
    pools: tuple[str, ...]
    tokens: tuple[str, ...]
    quotes: Mapping[int, int]


def oracle(
    routes: Sequence[OracleRoute], percents: Sequence[int], max_splits: int
) -> tuple[int | None, int | None]:
    """Exhaustive: the best cached value over every set of distinct, pool-disjoint routes
    with grid percents summing to 100 and at most `max_splits` routes -- (best admissible
    under the token-DAG rule, best ignoring it). Shares no code with the selector."""
    best_ok: int | None = None
    best_any: int | None = None
    for k in range(1, max_splits + 1):
        for combo in itertools.combinations(routes, k):
            pools = [p for r in combo for p in r.pools]
            if len(pools) != len(set(pools)):
                continue
            edges = [e for r in combo for e in zip(r.tokens, r.tokens[1:], strict=False)]
            acyclic = kahn_acyclic(edges)
            for ps in itertools.product(percents, repeat=k):
                if sum(ps) != 100 or any(p not in r.quotes for r, p in zip(combo, ps, strict=True)):
                    continue
                value = sum(r.quotes[p] for r, p in zip(combo, ps, strict=True))
                best_any = value if best_any is None else max(best_any, value)
                if acyclic:
                    best_ok = value if best_ok is None else max(best_ok, value)
    return best_ok, best_any


# ================================================================ 3. core fixtures


def core_inputs(
    spec: Mapping[str, Any],
) -> tuple[int, list[int], list[sor.RouteQuote], list[OracleRoute], dict[int, str]]:
    """A core fixture as the port's inputs: V2 routes in fixture order (B-Q1), percent
    ascending, null entries dropped (B-Q2). Amount 100 * 2**k keeps every grid amount an
    exact integer (selection does not depend on it)."""
    amount = 12_800
    percents, amounts = sor.amount_distribution(amount, int(spec["percent_step"]))
    quotes: list[sor.RouteQuote] = []
    oracle_routes: list[OracleRoute] = []
    owner: dict[int, str] = {}
    for r in spec["routes"]:
        tokens = tuple(r["tokens"])
        pairs = [sorted((tokens[i], tokens[i + 1])) for i in range(len(tokens) - 1)]
        pools = tuple(
            sor.SorPool(pid, lo, hi, sor.V2)
            for pid, (lo, hi) in zip(r["pools"], pairs, strict=True)
        )
        route = sor.SorRoute(sor.V2, pools, tokens)
        table = {int(p): int(q) for p, q in r["quotes"].items()}
        oracle_routes.append(OracleRoute(r["id"], tuple(r["pools"]), tokens, table))
        for p, a in zip(percents, amounts, strict=True):
            if p in table:
                rq = sor.RouteQuote(len(quotes), route, p, a, table[p])
                owner[id(rq)] = r["id"]
                quotes.append(rq)
    return amount, percents, quotes, oracle_routes, owner


def shape(selection: sor.SwapSelection | None, owner: Mapping[int, str]) -> list[list[Any]] | None:
    if selection is None:
        return None
    return [[owner[id(r)], r.percent] for r in selection.routes]


def port_selection(spec: Mapping[str, Any]) -> tuple[sor.SwapSelection | None, dict[int, str]]:
    amount, percents, quotes, _, owner = core_inputs(spec)
    return sor.get_best_swap_route(
        amount, percents, quotes, max_splits=int(spec["max_splits"])
    ), owner


def variant_selection(
    spec: Mapping[str, Any], **mutation: Any
) -> tuple[sor.SwapSelection | None, dict[int, str], Counters]:
    amount, percents, quotes, _, owner = core_inputs(spec)
    counters = new_counters()
    sel = cycle_safe_selection(
        amount, percents, quotes, max_splits=int(spec["max_splits"]), counters=counters, **mutation
    )
    return sel, owner, counters


CORE = FIX["core"]


@pytest.mark.parametrize("fid", sorted(CORE))
def test_core_fixture_reference_selection_is_the_hand_trace(fid: str) -> None:
    spec = CORE[fid]
    sel, owner = port_selection(spec)
    exp = spec["expected_port"]
    assert shape(sel, owner) == exp["routes"]
    assert sel is not None and sel.swap.quote == exp["value"]
    assert union_has_cycle(sel.routes) is exp["union_cyclic"]


@pytest.mark.parametrize("fid", sorted(CORE))
def test_core_fixture_variant_selection_and_counters_are_the_hand_trace(fid: str) -> None:
    spec = CORE[fid]
    sel, owner, counters = variant_selection(spec)
    exp = spec["expected_variant"]
    assert shape(sel, owner) == exp["routes"]
    assert (None if sel is None else sel.swap.quote) == exp["value"]
    assert counters == {
        "admission_checks": exp["admission_checks"],
        "combinations_rejected_cycle": exp["combinations_rejected_cycle"],
    }
    if sel is not None:
        edges = [e for r in sel.routes for e in route_edges(r)]
        assert kahn_acyclic(edges), "the variant returned a cyclic union"
        pools = [p for r in sel.routes for p in r.pool_identifiers]
        assert len(pools) == len(set(pools)), "the variant returned a pool conflict"


@pytest.mark.parametrize("fid", sorted(CORE))
def test_core_fixture_oracle_bounds_the_variant_and_it_is_not_optimal(fid: str) -> None:
    spec = CORE[fid]
    _, percents, _, oracle_routes, _ = core_inputs(spec)
    best_ok, best_any = oracle(oracle_routes, percents, int(spec["max_splits"]))
    assert {"best_admissible": best_ok, "best_any": best_any} == spec["expected_oracle"]
    sel, _, _ = variant_selection(spec)
    if sel is None:
        # K6: nothing admissible exists on this grid at all.
        assert best_ok is None
    else:
        assert best_ok is not None and sel.swap.quote <= best_ok


def test_k1_pairwise_admission_is_not_enough() -> None:
    spec = CORE["K1_three_route_cycle"]
    sel, owner, _ = variant_selection(spec, admission="pairwise")
    exp = spec["expected_pairwise_mutation"]
    assert shape(sel, owner) == exp["routes"]
    assert sel is not None and sel.swap.quote == exp["value"]
    assert union_has_cycle(sel.routes) is exp["union_cyclic"]
    # every pair of the triple is acyclic: only the whole union closes a -> b -> c -> a
    assert not any(union_has_cycle(pair) for pair in itertools.combinations(sel.routes, 2))


def test_k2_skip_percent_policy_loses_the_admissible_tie_partner() -> None:
    spec = CORE["K2_tie_continue_scan"]
    sel, owner, _ = variant_selection(spec, policy="skip_percent")
    exp = spec["expected_skip_percent_mutation"]
    assert shape(sel, owner) == exp["routes"]
    assert sel is not None and sel.swap.quote == exp["value"]


def test_k4_zero_rejections_not_an_acyclic_reference_result_is_the_parity_condition() -> None:
    spec = CORE["K4_acyclic_reference_but_different_result"]
    ref, owner = port_selection(spec)
    var, owner2, counters = variant_selection(spec)
    assert ref is not None and not union_has_cycle(ref.routes)
    assert counters["combinations_rejected_cycle"] > 0
    assert shape(ref, owner) != shape(var, owner2)


def test_k5_admission_changes_pruning_and_can_lose_to_a_valid_reference() -> None:
    spec = CORE["K5_pruning_regression"]
    ref, _ = port_selection(spec)
    var, _, _ = variant_selection(spec)
    assert ref is not None and var is not None
    assert not union_has_cycle(ref.routes)
    assert var.swap.quote < ref.swap.quote


# ================================================================ 4. original SOR goldens


def _golden(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SOR_GOLDENS / name).read_text(encoding="utf-8"))
    return data


GOLDEN_CASES = [str(c["case_id"]) for c in _golden("MANIFEST.json")["cases"]]


def _golden_inputs(inp: Mapping[str, Any]) -> tuple[int, list[int], list[sor.RouteQuote], int, int]:
    r = inp["routing"]

    def pools(family: str) -> list[sor.SorPool]:
        return [
            sor.SorPool(
                str(p["pool_id"]), str(p["token0"]).lower(), str(p["token1"]).lower(),
                family, p.get("fee"),
            )
            for p in inp["pools"][family]
        ]  # fmt: skip

    amount = int(inp["amount_in_raw"])
    v3, v2 = pools("V3"), pools("V2")
    routes = {
        fam: sor.compute_family_routes(
            fam, inp["token_in"], inp["token_out"], v3, v2, int(r["max_hops"])
        )
        for fam in sor.FAMILIES
    }
    rows = {
        (str(q["family"]), tuple(q["route_pool_ids"]), int(q["percent"])): q for q in inp["quotes"]
    }

    def quote(route: sor.SorRoute, percent: int, _a: sor.Rational) -> int | None:
        raw = rows[(route.protocol, route.pool_ids, percent)]["raw_quote"]
        return None if raw is None else int(raw)

    def gas(route: sor.SorRoute, percent: int, _a: sor.Rational, _q: int) -> sor.GasScore:
        row = rows[(route.protocol, route.pool_ids, percent)]
        return sor.GasScore(
            int(row["gas_estimate"]),
            int(row["gas_cost_in_quote_token"]),
            int(row["gas_cost_usd_raw"]),
        )

    percents, amounts = sor.amount_distribution(amount, int(r["percent_step"]))
    quotes = sor.build_route_quotes(routes, percents, amounts, quote, gas)
    return amount, percents, quotes, int(r["min_splits"]), int(r["max_splits"])


def _selection_view(sel: sor.SwapSelection | None) -> Any:
    return None if sel is None else sel.to_dict()


@pytest.mark.parametrize("case_id", GOLDEN_CASES)
def test_original_goldens_stay_the_reference_and_the_variant_follows_theorem_p(
    case_id: str,
) -> None:
    """Goldens stay authoritative for `uni_sor_port` only. On each golden the variant
    either rejects nothing and then equals the golden selection exactly (Theorem P), or
    rejects something (the golden's own expectation then does not apply to it)."""
    inp = _golden(f"{case_id}.input.json")
    gold = _golden(f"{case_id}.golden.json")
    amount, percents, quotes, min_splits, max_splits = _golden_inputs(inp)
    ref = sor.get_best_swap_route(
        amount, percents, quotes, min_splits=min_splits, max_splits=max_splits
    )
    counters = new_counters()
    var = cycle_safe_selection(
        amount, percents, quotes, min_splits=min_splits, max_splits=max_splits, counters=counters
    )
    ref_view = _selection_view(ref)
    if gold["result"] is None:
        assert ref_view is None
    else:
        assert ref_view is not None
        assert [r["pool_ids"] for r in ref_view["routes"]] == [
            r["pool_ids"] for r in gold["result"]["routes"]
        ]
    assert counters["combinations_rejected_cycle"] == 0, "a golden now exercises admission"
    assert _selection_view(var) == ref_view


def test_golden_sweep_rejects_nothing_and_every_golden_selection_is_acyclic() -> None:
    rejected = []
    for case_id in GOLDEN_CASES:
        amount, percents, quotes, min_splits, max_splits = _golden_inputs(
            _golden(f"{case_id}.input.json")
        )
        counters = new_counters()
        cycle_safe_selection(
            amount,
            percents,
            quotes,
            min_splits=min_splits,
            max_splits=max_splits,
            counters=counters,
        )
        ref = sor.get_best_swap_route(
            amount, percents, quotes, min_splits=min_splits, max_splits=max_splits
        )
        assert ref is None or not union_has_cycle(ref.routes)
        if counters["combinations_rejected_cycle"]:
            rejected.append(case_id)
    assert rejected == []
    assert len(GOLDEN_CASES) == 35


# ================================================================ 5. adapter fixtures


M112 = 2**112 - 1


def _amount(value: Any) -> int:
    if isinstance(value, int):
        return value
    base, _, minus = str(value).partition("-")
    assert base == "M"
    return M112 - int(minus)


def fixture_bundle(spec: Mapping[str, Any]) -> SnapshotBundle:
    source = spec.get("source_key")
    states: dict[str, PoolState] = {
        pid: ConstantProductPoolState(
            pid, t0, t1, _amount(r0), _amount(r1), fee_bps=int(spec["fee_bps"]), source_key=source
        )
        for pid, (t0, t1, r0, r1) in spec["pools"].items()
    }
    return SnapshotBundle(
        bundle_id=f"whi1555-{spec['case']['case_id']}",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools=states,
        cases=(),
        bundle_hash=f"fixture:WHI-1555-{spec['case']['case_id']}",
        source_path="<test>",
    )


def fixture_case(spec: Mapping[str, Any]) -> Case:
    c = spec["case"]
    return Case(c["case_id"], c["token_in"], c["token_out"], int(c["amount_in"]))


@dataclasses.dataclass
class Run:
    result: SolveResult
    published: list[RoutePlan]
    metered: int


def run(
    solver: Callable[[Case, SolveContext, Budget], SolveResult],
    bundle: SnapshotBundle,
    case: Case,
    params: Mapping[str, int],
    budget: Budget | None = None,
) -> Run:
    prepared = sor.prepare(bundle, AlgorithmConfig(sor.NAME, dict(params)))
    published: list[RoutePlan] = []
    context = SolveContext(bundle, gross_only(), prepared, candidate_sink=published.append)
    with metered_quotes(None) as meter:
        result = solver(case, context, budget or Budget())
    return Run(result, published, meter.counted)


def selected(res: SolveResult) -> list[list[Any]]:
    return [
        [r["pool_ids"], r["percent"], int(r["raw_quote"])]
        for r in res.search_stats["selection"]["routes"]
    ]


A1 = FIX["adapter"]["A1_union_cycle"]
A3 = FIX["adapter"]["A3_no_admissible_overflow"]
A4 = FIX["adapter"]["A4_replay_budget"]


def hand_chain(spec: Mapping[str, Any], pools: Sequence[str], token: str, amount: int) -> int:
    for pid in pools:
        t0, t1, r0, r1 = spec["pools"][pid]
        r_in, r_out = (_amount(r0), _amount(r1)) if token == t0 else (_amount(r1), _amount(r0))
        amount = cp_out(amount, r_in, r_out, int(spec["fee_bps"]))
        token = t1 if token == t0 else t0
    return amount


def test_a1_hand_quotes_are_independent_of_the_code_under_test() -> None:
    a = int(A1["case"]["amount_in"])
    for route, (half, full) in A1["hand_quotes"].items():
        assert hand_chain(A1, list(route), "s", a // 2) == half
        assert hand_chain(A1, list(route), "s", a) == full


def test_a1_reference_selects_a_pool_disjoint_union_cycle_and_the_evaluator_rejects_it() -> None:
    bundle, case = fixture_bundle(A1), fixture_case(A1)
    ref = run(sor.solve, bundle, case, A1["params"])
    exp = A1["expected_port"]
    assert ref.result.status is SolveStatus.INVALID_PLAN
    assert ref.result.error == exp["error"]
    assert selected(ref.result) == exp["routes"]
    assert sum(ref.result.search_stats["routes_enumerated"].values()) == A1["routes_enumerated"]
    routes = ref.result.search_stats["selection"]["routes"]
    # each route is a simple path, and the two share no physical pool ...
    assert all(len(set(r["token_path"])) == len(r["token_path"]) for r in routes)
    assert not set(routes[0]["pool_ids"]) & set(routes[1]["pool_ids"])
    # ... yet the union's token graph has a cycle (independent test).
    edges = [e for r in routes for e in zip(r["token_path"], r["token_path"][1:], strict=False)]
    assert not kahn_acyclic(edges)


def test_a1_variant_rejects_the_union_and_returns_the_admissible_baseline() -> None:
    bundle, case = fixture_bundle(A1), fixture_case(A1)
    var = run(spec_solve, bundle, case, A1["params"])
    exp = A1["expected_variant"]
    assert var.result.status is SolveStatus.OK
    assert selected(var.result) == exp["routes"]
    assert var.result.evaluation is not None
    assert var.result.evaluation.gross_output == exp["gross"]
    assert hand_chain(A1, ["a", "b", "c"], "s", int(case.amount_in)) == exp["gross"]
    block = var.result.search_stats["cycle_safe"]
    assert (block["admission_checks"], block["combinations_rejected_cycle"]) == (
        exp["admission_checks"],
        exp["combinations_rejected_cycle"],
    )
    assert block["reference_trajectory"] is False
    # independent final replay by the unchanged evaluator
    assert var.result.plan is not None
    ev = evaluate(bundle, case, var.result.plan, gross_only())
    assert ev.status is EvalStatus.OK and ev.gross_output == exp["gross"]
    # published exactly once, after the replay, and it is the returned plan
    assert var.published == [var.result.plan]


A1_TOKEN_PATHS = {
    "abc": ("s", "x", "y", "t"),
    "def": ("s", "y", "x", "t"),
    "af": ("s", "x", "t"),
    "dc": ("s", "y", "t"),
    "aec": ("s", "x", "y", "t"),
    "dbf": ("s", "y", "x", "t"),
}


def test_a1_oracle_on_the_real_quote_table() -> None:
    routes = [
        OracleRoute(k, tuple(k), A1_TOKEN_PATHS[k], {50: half, 100: full})
        for k, (half, full) in A1["hand_quotes"].items()
    ]
    best_ok, best_any = oracle(routes, [50, 100], 2)
    assert {"best_admissible": best_ok, "best_any": best_any} == A1["expected_oracle_step50"]
    assert best_ok == A1["expected_variant"]["gross"]


def test_a1_overlap_and_path_local_revisit_are_different_rules() -> None:
    bundle, case = fixture_bundle(A1), fixture_case(A1)
    prepared = sor.prepare(bundle, AlgorithmConfig(sor.NAME, dict(A1["params"])))
    routes = sor.compute_family_routes(
        sor.V2, case.token_in, case.token_out, (), prepared.v2_pools, 3
    )
    by_ids = {r.pool_ids: r for r in routes}
    # (1) physical pool overlap: s-a-x-b-y-c-t and s-a-x-f-t share pool a; their token
    # union is acyclic, and B-S9 excludes the pair before any admission check.
    r1, overlap = by_ids[("a", "b", "c")], by_ids[("a", "f")]
    percents, amounts = sor.amount_distribution(case.amount_in, 50)
    q = [sor.RouteQuote(i, r, 50, amounts[0], 1) for i, r in enumerate((r1, overlap))]
    assert not union_has_cycle(q)
    counters = new_counters()
    assert first_admissible(q[:1], q[1:], counters) is None
    assert counters == new_counters()
    # (2) a path-local revisit s-x-y-x-t (a, b, e, f) is never enumerated (B-R4) ...
    assert all(len(set(r.token_path)) == len(r.token_path) for r in routes)
    assert ("a", "b", "e", "f") not in by_ids
    # ... and as a plan it is an evaluator token cycle on a single route
    walk = (
        Edge("a", "s", "x"), Edge("b", "x", "y"), Edge("e", "y", "x"), Edge("f", "x", "t"),
    )  # fmt: skip
    ev = evaluate(bundle, case, split_path_plan(case, (walk,), (1,)), gross_only())
    assert ev.status is EvalStatus.INVALID_PLAN and "economic token cycle" in (ev.error or "")
    # (3) union cycle: two simple, pool-disjoint routes (A1's reference selection).
    r2 = by_ids[("d", "e", "f")]
    pair = [sor.RouteQuote(i, r, 50, amounts[0], 1) for i, r in enumerate((r1, r2))]
    assert not set(r1.pool_ids) & set(r2.pool_ids) and union_has_cycle(pair)


def test_a1_same_candidates_grid_and_quote_table_as_the_reference() -> None:
    bundle, case = fixture_bundle(A1), fixture_case(A1)
    for params in (A1["params"], {"max_hops": 3, "max_splits": 4, "percent_step": 5}):
        ref = run(sor.solve, bundle, case, params)
        var = run(spec_solve, bundle, case, params)
        for key in ("routes_enumerated", "quote_entries", "quote_entries_null", "route_quotes",
                    "entry_failures", "coverage_mode", "cohort_pools"):  # fmt: skip
            assert var.result.search_stats[key] == ref.result.search_stats[key], key
        # admission costs no quote: the only extra quotes are the replay's, and here the
        # replay is all memo hits (one pool-disjoint route at an exact grid amount)
        assert var.metered == var.result.search_stats["quotes_executed"]
        assert ref.metered == ref.result.search_stats["quotes_executed"]
        assert var.metered == ref.metered
        assert var.result.status is SolveStatus.OK
        # the B-S3 baseline is admissible, so the variant never ends below it
        best_full = max(int(h[1]) for h in A1["hand_quotes"].values())
        assert var.result.evaluation is not None
        assert var.result.evaluation.gross_output >= best_full


def test_a3_no_admissible_selection_is_no_route_and_nothing_is_published() -> None:
    bundle, case = fixture_bundle(A3), fixture_case(A3)
    ref = run(sor.solve, bundle, case, A3["params"])
    assert ref.result.status is SolveStatus.INVALID_PLAN
    assert ref.result.error == A3["expected_port"]["error"]
    var = run(spec_solve, bundle, case, A3["params"])
    exp = A3["expected_variant"]
    assert var.result.status is SolveStatus.NO_ROUTE
    assert var.result.plan is None and len(var.published) == exp["published"]
    block = var.result.search_stats["cycle_safe"]
    assert block["no_admissible_selection"] is True
    assert (block["admission_checks"], block["combinations_rejected_cycle"]) == (
        exp["admission_checks"],
        exp["combinations_rejected_cycle"],
    )
    assert "plan-token-DAG admission" in (var.result.error or "")
    # the 100 % entries are null because the sourced first hops revert (uint112), not a
    # silent drop: they are counted by reason
    assert var.result.search_stats["entry_failures"].get("reverted", 0) > 0


def test_budgets_are_the_references_and_admission_never_resets_them() -> None:
    bundle, case = fixture_bundle(A1), fixture_case(A1)
    n_routes = A1["routes_enumerated"]
    for solver in (sor.solve, spec_solve):
        cap = run(solver, bundle, case, A1["params"], Budget(max_candidates=n_routes - 1))
        assert cap.result.status is SolveStatus.TIMEOUT
        assert cap.result.search_stats["truncated_by"] == "max_candidates"
        assert cap.result.candidates_truncated == n_routes
    table = run(sor.solve, bundle, case, A1["params"]).metered
    for solver in (sor.solve, spec_solve):
        cut = run(solver, bundle, case, A1["params"], Budget(max_quotes=table - 1))
        assert cut.result.status is SolveStatus.TIMEOUT
        assert cut.result.search_stats["truncated_by"] == "max_quotes"
        assert cut.metered == table - 1
    var_cut = run(spec_solve, bundle, case, A1["params"], Budget(max_quotes=table - 1))
    assert var_cut.published == []
    assert var_cut.result.search_stats["r021"]["certificate_unavailable_reason"] == "not_produced"


def test_a4_replay_budget_counts_in_the_same_ledger_and_publication_follows_replay() -> None:
    bundle, case = fixture_bundle(A4), fixture_case(A4)
    exp = A4["expected_capped"]
    ref = run(sor.solve, bundle, case, A4["params"], Budget(max_quotes=exp["max_quotes"]))
    var = run(spec_solve, bundle, case, A4["params"], Budget(max_quotes=exp["max_quotes"]))
    for r in (ref, var):
        assert r.result.status is SolveStatus.TIMEOUT
        assert "replaying" in (r.result.error or "")
        assert r.metered == A4["table_quotes"]
    assert len(ref.published) == exp["port_published"]  # the reference publishes pre-replay
    assert len(var.published) == exp["variant_published"]
    full = run(
        spec_solve,
        bundle,
        case,
        A4["params"],
        Budget(max_quotes=A4["expected_uncapped"]["max_quotes"]),
    )
    assert full.result.status is SolveStatus.OK
    assert full.metered == A4["table_quotes"] + A4["replay_extra_quotes"]
    assert full.result.evaluation is not None
    assert full.result.evaluation.gross_output == cp_out(50_000, 10**6, 10**6) + cp_out(
        50_001, 10**6, 10**6
    )
    assert full.published == [full.result.plan]


# ================================================================ 6. Theorem P, lemmas


def random_bundle(rng: random.Random, n_tokens: int, n_pools: int) -> SnapshotBundle:
    tokens = [f"t{i}" for i in range(n_tokens)]
    states: dict[str, PoolState] = {}
    for k in range(n_pools):
        a, b = rng.sample(tokens, 2)
        states[f"p{k:02d}"] = ConstantProductPoolState(
            f"p{k:02d}", a, b, rng.randint(10**5, 10**7), rng.randint(10**5, 10**7), fee_bps=30
        )
    return SnapshotBundle(
        bundle_id="rand", kind="synthetic", schema_version=1, block=BLOCK, pools=states,
        cases=(), bundle_hash="fixture:WHI-1555-random", source_path="<test>",
    )  # fmt: skip


def random_runs(seed: int, count: int, max_hops: int) -> Iterator[tuple[Run, Run]]:
    rng = random.Random(seed)
    params = {"max_hops": max_hops, "max_splits": 3, "percent_step": 20}
    for _ in range(count):
        bundle = random_bundle(rng, 5, rng.randint(6, 11))
        case = Case("r", "t0", "t4", rng.randint(10**4, 3 * 10**6))
        yield run(sor.solve, bundle, case, params), run(spec_solve, bundle, case, params)


def _is_cycle_error(res: SolveResult) -> bool:
    return res.status is SolveStatus.INVALID_PLAN and "economic token cycle" in (res.error or "")


def test_theorem_p_and_invariants_on_seeded_three_hop_bundles() -> None:
    seen_rejection = seen_identical = seen_cycle = 0
    for ref, var in random_runs(20261005, 60, 3):
        block = var.result.search_stats["cycle_safe"]
        assert not _is_cycle_error(var.result), "the variant produced a token cycle"
        assert var.metered == var.result.search_stats["quotes_executed"]
        if ref.result.search_stats.get("route_quotes") is not None:
            assert (
                var.result.search_stats["route_quotes"] == ref.result.search_stats["route_quotes"]
            )
        if block["combinations_rejected_cycle"] == 0:
            seen_identical += 1
            assert var.result.status is ref.result.status
            assert var.result.search_stats["selection"] == ref.result.search_stats["selection"]
            assert var.result.plan == ref.result.plan
        else:
            seen_rejection += 1
        if _is_cycle_error(ref.result):
            seen_cycle += 1
            assert block["combinations_rejected_cycle"] > 0
    assert seen_identical and seen_rejection and seen_cycle


def test_lemma_two_hops_never_reject_so_the_variant_is_the_reference() -> None:
    for ref, var in random_runs(20261006, 40, 2):
        assert var.result.search_stats["cycle_safe"]["combinations_rejected_cycle"] == 0
        assert var.result.search_stats["selection"] == ref.result.search_stats["selection"]
        assert var.result.status is ref.result.status


def test_corpus_fixture_cases_are_unaffected_noncycle_cases() -> None:
    bundle = load_bundle(CORPUS_FIXTURE)
    params = {"max_hops": 3, "max_splits": 4, "percent_step": 5}
    for case in bundle.cases:
        ref = run(sor.solve, bundle, case, params)
        var = run(spec_solve, bundle, case, params)
        # observed on all 96 cases of this 19-pool excerpt: no cyclic combination forms
        assert var.result.search_stats["cycle_safe"]["combinations_rejected_cycle"] == 0
        assert var.result.search_stats["selection"] == ref.result.search_stats["selection"]
        assert var.result.plan == ref.result.plan
        assert var.result.status is ref.result.status
    assert len(bundle.cases) == 96


# ================================================================ 7. records and options


def _shared_validator() -> ModuleType:
    """The unchanged R021-C/1 validator, loaded read-only by path."""
    path = REPO / "tests" / "docs" / "test_research_021_contract.py"
    spec = importlib.util.spec_from_file_location("r021_contract_validator_1555", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _shared_validator()


def _violations(r: Run, case: Case) -> set[str]:
    record = r.result.search_stats["r021"]
    ctx = {
        "status": r.result.status.value,
        "final_score": None if r.result.score is None else str(r.result.score),
        "objective": "gross_only",
        "quotes_counted": r.metered,
        "hard_killed": False,
        "run": {
            "git_revision": GIT_REVISION,
            "bundle_hash": record["domain"]["universe"]["bundle"],
            "algorithm": NAME,
            "effective_settings_sha256": EMPTY_SETTINGS_SHA,
        },
        "request": {
            "case_id": case.case_id, "token_in": case.token_in,
            "token_out": case.token_out, "amount_in": str(case.amount_in),
        },
    }  # fmt: skip
    found: set[str] = VALIDATOR.check_diagnostics(record, ctx)
    return found


def test_diagnostics_records_pass_the_shared_validator() -> None:
    cases = [
        (A1, None, SolveStatus.OK),
        (A3, None, SolveStatus.NO_ROUTE),
        (A1, Budget(max_candidates=1), SolveStatus.TIMEOUT),
    ]
    for spec, budget, status in cases:
        bundle, case = fixture_bundle(spec), fixture_case(spec)
        r = run(spec_solve, bundle, case, spec["params"], budget)
        assert r.result.status is status
        assert _violations(r, case) == set()
        record = r.result.search_stats["r021"]
        assert record["work"]["combinations_rejected_cycle"] <= record["work"]["admission_checks"]
        if status is SolveStatus.OK:
            assert record["certificate"]["bound_kind"] == "unknown"
            assert record["certificate"]["upper_raw"] is None
        else:
            assert record["certificate"] is None
    # a wrong settings hash is caught (the record is bound to the run identity)
    bundle, case = fixture_bundle(A1), fixture_case(A1)
    r = run(spec_solve, bundle, case, A1["params"])
    r.result.search_stats["r021"]["certificate"]["source"]["effective_settings_sha256"] = "0" * 64
    assert _violations(r, case) == {"C_IDENTITY"}


def test_domain_is_shared_with_the_reference_so_the_comparison_is_same_domain() -> None:
    bundle = fixture_bundle(A1)
    prepared = sor.prepare(bundle, AlgorithmConfig(sor.NAME, dict(A1["params"])))
    domain = sor_domain(bundle, prepared)
    assert VALIDATOR.check_domain(domain) == set()
    contract = json.loads((R021 / "contract-v1.json").read_text(encoding="utf-8"))
    identity = next(i for i in contract["identities"] if i["id"] == NAME)
    assert identity["max_candidates_unit"] == "enumerated_routes_threshold"
    assert set(domain["protocols"]) == set(identity["protocols_ceiling"])
    for unit in ("admission_checks", "combinations_rejected_cycle"):
        assert contract["work_units"][unit] == "search"


def test_option_schema_is_empty_and_the_preset_is_the_empty_mapping() -> None:
    opts = FIX["options"]
    assert validate_options({}) == {}
    bad_options: list[dict[str, Any]] = [
        {"max_hops": 3},
        {"admission": "off"},
        {"shortlist": {}},
        {"policy": "skip"},
    ]
    for bad in bad_options:
        with pytest.raises(ValueError):
            validate_options(bad)
    preset = opts["preset"]
    assert preset == {"key": NAME, "version": 1, "algorithm": NAME, "options": {}}
    assert validate_options(preset["options"]) == {}
    assert opts["settings_sha256"] == EMPTY_SETTINGS_SHA == canonical_hash({})


def test_memo_names_every_fixture_the_outcome_and_the_amendment() -> None:
    for fid in (*CORE, *FIX["adapter"]):
        assert fid in MEMO, fid
    assert "Outcome: `go`" in MEMO
    assert "Amendment text for WHI-1556" in MEMO
    for token in ("admission_checks", "combinations_rejected_cycle", "enumerated_routes_threshold"):
        assert token in MEMO


def test_pinned_corpus_probe_summary_is_internally_consistent() -> None:
    probe = FIX["corpus_probe"]
    assert probe is not None
    parts = {p["name"]: p for p in probe["parts"]}
    for part in parts.values():
        cases = part["cases"]
        assert len(cases) == part["n"]
        assert part["with_rejections"] == sum(1 for c in cases if c["rejected"] > 0)
        for c in cases:
            assert c["variant_cycle"] is False, c["case_id"]  # Theorem S on real state
            assert c["rejected"] <= c["admission_checks"]
            if c["rejected"] == 0:  # Theorem P
                assert c["identical_selection"] is True, c["case_id"]
                assert c["variant_status"] == c["port_status"]
            if c["port_cycle"]:
                assert c["rejected"] > 0 and c["port_status"] == "invalid_plan"
    tuning = parts["tuning"]
    assert tuning["bundle_hash"].startswith("b900b866") and tuning["n"] == 96
    known_tuning = {"emp-09bc4e-201eba-low-2", "emp-09bc4e-201eba-medium-2"}
    assert {c["case_id"] for c in tuning["cases"] if c["port_cycle"]} == known_tuning
    report = parts["report_defect_regressions"]
    assert report["bundle_hash"].startswith("8213b7b0")
    known_report = {
        "emp-09bc4e-201eba-low-1", "emp-09bc4e-201eba-low-3", "emp-09bc4e-201eba-medium-1",
        "emp-09bc4e-201eba-medium-3", "emp-201eba-cda86a-low-1", "emp-201eba-cda86a-low-3",
        "emp-78c1b0-09bc4e-low-3", "emp-78c1b0-deadde-low-1", "emp-78c1b0-deadde-low-3",
        "emp-78c1b0-deadde-low-4", "bnd-09bc4e-201eba-liq_at", "bnd-09bc4e-201eba-liq_above",
    }  # fmt: skip
    assert {c["case_id"] for c in report["cases"]} == known_report
    for c in (*report["cases"], *(c for c in tuning["cases"] if c["port_cycle"])):
        assert (c["port_status"], c["port_cycle"], c["variant_status"]) == (
            "invalid_plan",
            True,
            "ok",
        )


# ================================================================ 8. bounded diagnostic (CLI)


def probe_case(
    bundle: SnapshotBundle, prepared: sor.PreparedUniSorPort, case: Case
) -> dict[str, Any]:
    """One solve of the spec; inside it the reference selector runs on the identical
    quote table, so both selections share one table. The reference plan is then replayed
    by the evaluator separately. Diagnostic only: in-process, not a measured solve."""
    captured: dict[str, Any] = {}
    original = sor.get_best_swap_route_by

    def both(groups: Any, percents: Any, **kw: Any) -> sor.BestSwap | None:
        by = kw.pop("by", lambda r: r.quote_adjusted_for_gas)
        captured["ref"] = original(groups, percents, by=by, **kw)
        return select_cycle_safe(groups, percents, by=by, counters=captured["counters"], **kw)

    captured["counters"] = new_counters()
    published: list[RoutePlan] = []
    context = SolveContext(bundle, gross_only(), prepared, candidate_sink=published.append)
    budget = Budget(max_quotes=300_000)
    with mock.patch.object(sor, "get_best_swap_route_by", both):
        res = sor.solve(case, context, budget)
    counters = captured["counters"]
    ref_swap: sor.BestSwap | None = captured.get("ref")
    ref_status: str | None = None
    ref_cycle = False
    ref_gross: int | None = None
    if ref_swap is not None:
        ref_cycle = union_has_cycle(ref_swap.routes)
        legs = tuple(sor._plan_legs(r.route, bundle) for r in ref_swap.routes)
        plan = split_path_plan(case, legs, tuple(r.percent for r in ref_swap.routes))
        ev = evaluate(bundle, case, plan, gross_only())
        ref_status = "ok" if ev.status is EvalStatus.OK else "invalid_plan"
        ref_gross = ev.gross_output if ev.status is EvalStatus.OK else None
    var_sel = res.search_stats.get("selection")
    var_routes = (
        None if var_sel is None else [(r["pool_ids"], r["percent"]) for r in var_sel["routes"]]
    )
    ref_routes = (
        None
        if ref_swap is None
        else [(list(r.pool_identifiers), r.percent) for r in ref_swap.routes]
    )
    var_cycle = _is_cycle_error(res)
    return {
        "case_id": case.case_id,
        "port_status": ref_status or res.status.value,
        "port_cycle": ref_cycle,
        "variant_status": res.status.value,
        "variant_cycle": var_cycle,
        "admission_checks": counters["admission_checks"],
        "rejected": counters["combinations_rejected_cycle"],
        "identical_selection": var_routes == ref_routes,
        "variant_gross": None
        if res.evaluation is None or res.status is not SolveStatus.OK
        else str(res.evaluation.gross_output),
        "port_gross": None if ref_gross is None else str(ref_gross),
        "quotes_executed": res.search_stats.get("quotes_executed"),
    }


def _probe_worker(args: tuple[str, str]) -> dict[str, Any]:
    bundle_path, case_id = args
    bundle = _cached_bundle(bundle_path)
    prepared = sor.prepare(
        bundle, AlgorithmConfig(sor.NAME, {"max_hops": 3, "max_splits": 4, "percent_step": 5})
    )
    case = next(c for c in bundle.cases if c.case_id == case_id)
    return probe_case(bundle, prepared, case)


@functools.cache
def _cached_bundle(path: str) -> SnapshotBundle:
    return load_bundle(Path(path))


def main(argv: Sequence[str]) -> int:
    if len(argv) < 3 or argv[0] != "probe":
        print("usage: probe <bundle> <out.json> [case ids...]", file=sys.stderr)
        return 2
    from multiprocessing import Pool

    bundle_path, out = argv[1], Path(argv[2])
    bundle = _cached_bundle(bundle_path)
    ids = list(argv[3:]) or [c.case_id for c in bundle.cases]
    with Pool(6) as pool:
        cases = pool.map(_probe_worker, [(bundle_path, i) for i in ids], chunksize=1)
    doc = {
        "bundle": bundle_path,
        "bundle_hash": bundle.bundle_hash,
        "params": {"max_hops": 3, "max_splits": 4, "percent_step": 5, "max_quotes": 300_000},
        "n": len(cases),
        "with_rejections": sum(1 for c in cases if c["rejected"] > 0),
        "cases": cases,
    }
    out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in doc.items() if k != "cases"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

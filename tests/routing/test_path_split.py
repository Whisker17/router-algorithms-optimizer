"""`path_split` (docs/DESIGN.md §2.6, WHI-1440).

Expected values are independent of the code under test:

- **Exhaustive feasible combinations** (`_brute`): every token-simple path by trying
  every ordered pool tuple (`itertools.product`, no graph search), every combination
  of at most `max_splits` pairwise pool-disjoint paths (`itertools.combinations`),
  every positive unit vector over them summing to the grid size, integer amounts by
  the declared rule, and a score that sums independent per-leg outputs -- no pruning,
  no branch-and-bound, no plan builder, no quote cache. Exhaustive comparisons use
  amounts divisible by the grid size, where every leg's amount is its exact floored
  share whatever the leg order.
- **Hand-derived CPMM outputs** (`_chain_out`): the Solidity `getAmountOut` formula
  (generalized to `fee_bps`, with the Moe uint112 reserve cap) chained along a path.
- **Real mixed-family states** (`tests/fixtures/routing/mantle_mixed`): per-leg
  outputs from a plain, memo-free `evaluate()` of each single path.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from benchmark.objective import ObjectiveContext, gross_only
from benchmark.profile import ProfileError, parse_profile
from pools.quote import metered_quotes
from routing.algorithms import direct_split, path_split, single_path
from routing.algorithms.base import (
    AlgorithmConfig,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
)
from routing.algorithms.registry import ALGORITHMS
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID
from routing.search import Edge, build_graph_index, enumerate_paths, path_plan
from snapshot.bundle import load_bundle
from snapshot.models import BlockRef, Case, ConstantProductPoolState, PoolState, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "routing"
BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)
MAX_U112 = (1 << 112) - 1


@cache
def fixture(name: str) -> SnapshotBundle:
    return load_bundle(FIXTURES / name)


def _bundle(*pools: PoolState) -> SnapshotBundle:
    return SnapshotBundle(
        bundle_id="t",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools={p.pool_id: p for p in pools},
        cases=(),
        bundle_hash="deadbeef",
        source_path="<test>",
    )


def _cp(
    pool_id: str,
    t0: str,
    t1: str,
    r0: int,
    r1: int,
    fee_bps: int = 30,
    source_key: str | None = None,
) -> ConstantProductPoolState:
    return ConstantProductPoolState(
        pool_id=pool_id,
        token0=t0,
        token1=t1,
        reserve0=r0,
        reserve1=r1,
        fee_bps=fee_bps,
        source_key=source_key,
    )


CONFIG = {"max_hops": 3, "max_splits": 4, "percent_step": 5}


def _solve(
    bundle: SnapshotBundle,
    case: Case,
    *,
    max_hops: int = 3,
    max_splits: int = 4,
    step: int = 5,
    budget: Budget | None = None,
    objective: ObjectiveContext | None = None,
    sink: list[Any] | None = None,
) -> SolveResult:
    params = {"max_hops": max_hops, "max_splits": max_splits, "percent_step": step}
    prepared = path_split.prepare(bundle, AlgorithmConfig(path_split.NAME, params))
    context = SolveContext(
        bundle=bundle,
        objective=objective or gross_only(),
        prepared=prepared,
        candidate_sink=None if sink is None else sink.append,
    )
    return path_split.solve(case, context, budget or Budget())


def _baselines(
    bundle: SnapshotBundle, case: Case, *, max_hops: int = 3, max_splits: int = 4, step: int = 5
) -> tuple[SolveResult, SolveResult]:
    sp = single_path.solve(
        case,
        SolveContext(
            bundle,
            gross_only(),
            single_path.prepare(bundle, AlgorithmConfig("single_path", {"max_hops": max_hops})),
        ),
        Budget(),
    )
    ds = direct_split.solve(
        case,
        SolveContext(
            bundle,
            gross_only(),
            direct_split.prepare(
                bundle,
                AlgorithmConfig("direct_split", {"max_splits": max_splits, "percent_step": step}),
            ),
        ),
        Budget(),
    )
    return sp, ds


Legs = frozenset[tuple[tuple[str, ...], int]]


def _legs(result: SolveResult) -> Legs:
    """The returned plan's legs as {(pool chain, leg input)}, read from the evaluator's
    own trace: a leg starts at every step drawing from the request fund."""
    assert result.evaluation is not None
    legs: list[tuple[list[str], int]] = []
    for t in result.evaluation.trace:
        if t.inputs[0][0] == REQUEST_FUND_ID:
            legs.append(([], t.amount_in))
        legs[-1][0].append(t.pool_id)
    return frozenset((tuple(chain), amount) for chain, amount in legs)


def _pools_used(result: SolveResult) -> list[str]:
    assert result.plan is not None
    return [s.pool_id for s in result.plan.steps]


# ------------------------------------------------------------ independent oracles


def _v2_out(pool: ConstantProductPoolState, token_in: str, amount_in: int) -> int | None:
    r_in, r_out = (
        (pool.reserve0, pool.reserve1)
        if token_in == pool.token0
        else (pool.reserve1, pool.reserve0)
    )
    if r_in == 0 or r_out == 0:
        return None
    fee_in = amount_in * (10_000 - pool.fee_bps)
    out = fee_in * r_out // (r_in * 10_000 + fee_in)
    if out <= 0 or (pool.source_key is not None and r_in + amount_in > MAX_U112):
        return None
    return out


def _chain_out(
    bundle: SnapshotBundle, chain: tuple[str, ...], token: str, amount: int
) -> int | None:
    for pool_id in chain:
        pool = bundle.pools[pool_id]
        assert isinstance(pool, ConstantProductPoolState)
        out = _v2_out(pool, token, amount)
        if out is None:
            return None
        amount, token = out, pool.other_token(token)
    return amount


def _brute_paths(bundle: SnapshotBundle, a: str, b: str, max_hops: int) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    for hops in range(1, max_hops + 1):
        for chain in itertools.product(list(bundle.pools.values()), repeat=hops):
            token, seen = a, [a]
            for pool in chain:
                if token not in (pool.token0, pool.token1):
                    break
                token = pool.other_token(token)
                seen.append(token)
            else:
                if token == b and len(set(seen)) == len(seen):
                    found.append(tuple(p.pool_id for p in chain))
    return found


OutFn = Callable[[tuple[str, ...], int], int | None]


def _brute(
    bundle: SnapshotBundle,
    case: Case,
    *,
    max_hops: int,
    max_splits: int,
    step: int,
    out: OutFn | None = None,
    allow_conflicts: bool = False,
) -> tuple[int | None, set[Legs]]:
    """(best gross, every allocation achieving it) over every feasible combination:
    at most `max_splits` pairwise pool-disjoint paths (unless `allow_conflicts`, the
    *infeasible* set a conflict-blind search would see), positive grid units summing to
    the grid size, each leg's output quoted independently."""
    n_units = 100 // step
    assert case.amount_in % n_units == 0, "exhaustive parity uses grid-divisible amounts"
    chains = _brute_paths(bundle, case.token_in, case.token_out, max_hops)
    quote = out or (lambda chain, amount: _chain_out(bundle, chain, case.token_in, amount))
    best: int | None = None
    winners: set[Legs] = set()
    for k in range(1, max_splits + 1):
        for combo in itertools.combinations(chains, k):
            pools = [p for chain in combo for p in chain]
            if not allow_conflicts and len(pools) != len(set(pools)):
                continue
            for units in itertools.product(range(1, n_units + 1), repeat=k):
                if sum(units) != n_units:
                    continue
                amounts = [case.amount_in * u // n_units for u in units]
                if 0 in amounts:
                    continue
                outs = [quote(chain, a) for chain, a in zip(combo, amounts, strict=True)]
                if any(o is None for o in outs):
                    continue
                score = sum(o for o in outs if o is not None)
                legs = frozenset(zip(combo, amounts, strict=True))
                if best is None or score > best:
                    best, winners = score, {legs}
                elif score == best:
                    winners.add(legs)
    return best, winners


def _evaluated_chain_out(bundle: SnapshotBundle, case: Case) -> OutFn:
    """One path's output from a memo-free `evaluate()` of its full-input plan."""
    index = build_graph_index(bundle)
    by_chain = {
        tuple(e.pool_id for e in p): p
        for p in enumerate_paths(index, case.token_in, case.token_out, 3)
    }

    def out(chain: tuple[str, ...], amount: int) -> int | None:
        leg = Case("leg", case.token_in, case.token_out, amount)
        ev = evaluate(bundle, leg, path_plan(leg, by_chain[chain]), gross_only())
        return ev.gross_output if ev.status is EvalStatus.OK else None

    return out


# ------------------------------------------------------------ fixtures

# One deep A-C pool feeding two C-B pools, plus a shallow direct pool: splitting across
# the two C-B pools would need the hub pool twice.
OVERLAP = _bundle(
    _cp("hub", "A", "C", 10**12, 10**12, fee_bps=1),
    _cp("cb1", "C", "B", 10**9, 10**9),
    _cp("cb2", "C", "B", 10**9, 10**9),
    _cp("ab", "A", "B", 2 * 10**8, 2 * 10**8),
)

# A deep direct pool and a shallow, cheap two-hop route: the detour is poor at the
# full input and the best marginal price at a small allocation.
SMALL_ONLY = _bundle(
    _cp("deep", "A", "B", 10**10, 10**10, fee_bps=30),
    _cp("s1", "A", "C", 10**9, 10**9, fee_bps=1),
    _cp("s2", "C", "B", 10**9, 103 * 10**7, fee_bps=1),
)

# Many parallel pools: enough pool-disjoint paths for the conflict-aware pruning to fire.
PARALLEL = _bundle(
    _cp("ab1", "A", "B", 10**9, 10**9),
    _cp("ab2", "A", "B", 7 * 10**8, 7 * 10**8, fee_bps=5),
    _cp("ab3", "A", "B", 3 * 10**8, 3 * 10**8, fee_bps=1),
    _cp("ab4", "A", "B", 10**8, 10**8, fee_bps=100),
    _cp("ac1", "A", "C", 10**9, 2 * 10**9),
    _cp("ac2", "A", "C", 5 * 10**8, 10**9, fee_bps=5),
    _cp("ac3", "A", "C", 10**8, 2 * 10**8, fee_bps=100),
    _cp("cb1", "C", "B", 2 * 10**9, 10**9),
    _cp("cb2", "C", "B", 10**9, 5 * 10**8, fee_bps=5),
    _cp("cb3", "C", "B", 2 * 10**8, 10**8, fee_bps=100),
)


# ------------------------------------------------------------ acceptance behavior


def test_multi_hop_split_improves_on_single_path_and_direct_split() -> None:
    bundle = fixture("cpmm_graph")
    case = bundle.case("a_b_large")
    result = _solve(bundle, case)
    sp, ds = _baselines(bundle, case)
    assert sp.score == 114_812_154 and ds.score == 85_567_157
    assert result.status is SolveStatus.OK and result.score is not None
    assert result.score > sp.score > ds.score
    stats = result.search_stats
    assert stats["chosen_source"] == "path_split"
    assert stats["single_path_score"] == str(sp.score)
    assert stats["direct_split_score"] == str(ds.score)
    legs = _legs(result)
    assert len(legs) == stats["best_splits"] >= 2
    assert any(len(chain) > 1 for chain, _ in legs)  # a genuinely multi-hop split
    pools = _pools_used(result)
    assert len(pools) == len(set(pools))  # pool-disjoint legs
    assert sum(a for _, a in legs) == case.amount_in
    fresh = evaluate(bundle, case, result.plan, gross_only())  # type: ignore[arg-type]
    assert fresh.status is EvalStatus.OK and fresh.gross_output == result.score


def test_paths_sharing_a_physical_pool_are_never_selected_together() -> None:
    case = Case("c", "A", "B", 10**8)
    result = _solve(OVERLAP, case, max_splits=3, step=10)
    assert result.status is SolveStatus.OK
    chains = [chain for chain, _ in _legs(result)]
    assert sum("hub" in chain for chain in chains) <= 1
    pools = _pools_used(result)
    assert len(pools) == len(set(pools))
    assert result.search_stats["bnb_conflicts_excluded"] > 0
    # The exclusion binds: a conflict-blind search would pick hub->cb1 + hub->cb2.
    blind, blind_winners = _brute(
        OVERLAP, case, max_hops=3, max_splits=3, step=10, allow_conflicts=True
    )
    best, winners = _brute(OVERLAP, case, max_hops=3, max_splits=3, step=10)
    assert blind is not None and best is not None and blind > best
    assert all(sum("hub" in chain for chain, _ in legs) == 2 for legs in blind_winners)
    assert result.score == best and _legs(result) in winners


def test_conflict_check_and_plan_builder_use_pool_identity() -> None:
    hub_cb1 = (Edge("hub", "A", "C"), Edge("cb1", "C", "B"))
    hub_cb2 = (Edge("hub", "A", "C"), Edge("cb2", "C", "B"))
    direct = (Edge("ab", "A", "B"),)
    assert path_split.paths_conflict(hub_cb1, hub_cb2)
    assert not path_split.paths_conflict(hub_cb1, direct)
    # Same tokens, distinct physical pools: not a conflict.
    assert not path_split.paths_conflict((Edge("cb1", "C", "B"),), (Edge("cb2", "C", "B"),))
    case = Case("c", "A", "B", 1000)
    with pytest.raises(ValueError, match="share a physical pool"):
        path_split.split_path_plan(case, (hub_cb1, hub_cb2), (1, 1))


def test_route_useful_only_at_a_small_allocation_is_retained() -> None:
    case = Case("c", "A", "B", 10**8)
    detour_full = _chain_out(SMALL_ONLY, ("s1", "s2"), "A", 10**8)
    deep_full = _chain_out(SMALL_ONLY, ("deep",), "A", 10**8)
    assert detour_full is not None and deep_full is not None
    assert detour_full * 10 < deep_full * 9  # the full-input quote is poor
    result = _solve(SMALL_ONLY, case)
    assert result.status is SolveStatus.OK
    legs = dict(_legs(result))
    assert ("s1", "s2") in legs and legs[("s1", "s2")] < case.amount_in // 2
    sp, ds = _baselines(SMALL_ONLY, case)
    assert _pools_used(sp) == ["deep"] and ds.score == sp.score
    assert result.score is not None and sp.score is not None and result.score > sp.score
    best, winners = _brute(SMALL_ONLY, case, max_hops=3, max_splits=4, step=5)
    assert result.score == best and _legs(result) in winners


def test_route_failing_at_full_input_still_helps_a_small_split() -> None:
    # A sourced Moe pair near the uint112 reserve cap: 100% reverts, <= 60% fits.
    near_cap = _cp(
        "near_cap",
        "A",
        "C",
        MAX_U112 - 60_000_000,
        MAX_U112 - 60_000_000,
        source_key="moe_classic_v1",
    )
    bundle = _bundle(
        _cp("shallow", "A", "B", 10**9, 10**9), near_cap, _cp("cb", "C", "B", 10**12, 10**12)
    )
    case = Case("c", "A", "B", 10**8)
    assert _chain_out(bundle, ("near_cap", "cb"), "A", 10**8) is None
    result = _solve(bundle, case)
    assert result.status is SolveStatus.OK
    assert result.search_stats["samples_failed"].get("reverted", 0) >= 1
    assert result.search_stats["single_path_score"] == str(
        _chain_out(bundle, ("shallow",), "A", 10**8)
    )
    assert ("near_cap", "cb") in dict(_legs(result))
    best, winners = _brute(bundle, case, max_hops=3, max_splits=4, step=5)
    assert result.score == best and _legs(result) in winners


EXHAUSTIVE = [
    ("cpmm_graph", "a_b_large", 150_000_000, 3, 3, 10),
    ("cpmm_graph", "a_b_large", 150_000_000, 3, 2, 5),
    ("cpmm_graph", "a_b_small", 1_000_000, 3, 3, 10),
    ("cpmm_graph", "a_d_multi_hop", 10_000_000, 3, 3, 10),
    ("cpmm_graph", "a_d_multi_hop", 10_000_000, 2, 2, 20),
    ("cpmm_graph", "a_b_large", 150_000_000, 2, 4, 25),
    ("OVERLAP", "c", 10**8, 2, 3, 10),
    ("OVERLAP", "c", 3 * 10**8, 2, 2, 5),
    ("SMALL_ONLY", "c", 10**8, 2, 3, 10),
    ("SMALL_ONLY", "c", 4 * 10**6, 2, 2, 5),
    ("PARALLEL", "c", 10**8, 2, 2, 10),
    ("PARALLEL", "c", 6 * 10**8, 2, 3, 10),
    ("PARALLEL", "c", 2 * 10**8, 2, 2, 5),
]


def _named(name: str) -> SnapshotBundle:
    return {"OVERLAP": OVERLAP, "SMALL_ONLY": SMALL_ONLY, "PARALLEL": PARALLEL}.get(
        name
    ) or fixture(name)


@pytest.mark.parametrize(
    ("name", "case_id", "amount", "max_hops", "max_splits", "step"), EXHAUSTIVE
)
def test_matches_exhaustive_feasible_combinations(
    name: str, case_id: str, amount: int, max_hops: int, max_splits: int, step: int
) -> None:
    bundle = _named(name)
    tokens = (
        ("A", "B")
        if name != "cpmm_graph"
        else (bundle.case(case_id).token_in, bundle.case(case_id).token_out)
    )
    case = Case(case_id, *tokens, amount)
    result = _solve(bundle, case, max_hops=max_hops, max_splits=max_splits, step=step)
    best, winners = _brute(bundle, case, max_hops=max_hops, max_splits=max_splits, step=step)
    assert result.status is SolveStatus.OK
    assert result.score == best
    assert _legs(result) in winners
    assert len(_legs(result)) <= max_splits
    pools = _pools_used(result)
    assert len(pools) == len(set(pools))


def test_pruning_fires_and_is_disclosed_without_changing_the_answer() -> None:
    case = Case("c", "A", "B", 2 * 10**8)
    result = _solve(PARALLEL, case, max_hops=2, max_splits=2, step=5)
    stats = result.search_stats
    assert stats["conflict_bound_pools"] == 2
    assert stats["entries_rank_pruned"] > 0
    assert stats["samples_bound_pruned"] > 0
    best, winners = _brute(PARALLEL, case, max_hops=2, max_splits=2, step=5)
    assert result.score == best and _legs(result) in winners


@pytest.mark.parametrize("case_id", ["usdt_wmnt_evidence_split", "wmnt_usdt_evidence_split"])
def test_real_mixed_family_choice_matches_exhaustive_feasible_combinations(case_id: str) -> None:
    bundle = fixture("mantle_mixed")
    original = bundle.case(case_id)
    case = Case(case_id, original.token_in, original.token_out, original.amount_in // 4 * 4)
    result = _solve(bundle, case, max_hops=2, max_splits=2, step=25)
    best, winners = _brute(
        bundle, case, max_hops=2, max_splits=2, step=25, out=_evaluated_chain_out(bundle, case)
    )
    assert result.status is SolveStatus.OK
    assert result.score == best and _legs(result) in winners
    fresh = evaluate(bundle, case, result.plan, gross_only())  # type: ignore[arg-type]
    assert fresh.status is EvalStatus.OK and fresh.gross_output == result.score
    assert fresh.trace == result.evaluation.trace  # type: ignore[union-attr]


def test_never_worse_than_the_retained_simpler_routes() -> None:
    for name in ("cpmm_graph", "mantle_mixed"):
        bundle = fixture(name)
        for case in bundle.cases:
            result = _solve(bundle, case)
            sp, ds = _baselines(bundle, case)
            simpler = [r.score for r in (sp, ds) if r.status is SolveStatus.OK]
            if not simpler:
                continue
            assert result.status is SolveStatus.OK and result.score is not None
            assert result.score >= max(s for s in simpler if s is not None)
            pools = _pools_used(result)
            assert len(pools) == len(set(pools))


# ------------------------------------------------------------ grid remainder


@pytest.mark.parametrize("amount", [1, 2, 3, 7, 19, 21, 1_000_003, 10**8 + 17])
@pytest.mark.parametrize("step", [5, 20, 25, 50])
def test_tiny_and_nondivisible_amounts_fully_allocate(amount: int, step: int) -> None:
    # 1 A buys ~1000 B on every route, so even a 1-unit leg has a nonzero output.
    bundle = _bundle(
        _cp("ab", "A", "B", 10**9, 10**12, fee_bps=0),
        _cp("ac", "A", "C", 10**9, 10**12, fee_bps=0),
        _cp("cb", "C", "B", 10**15, 10**15, fee_bps=0),
    )
    result = _solve(bundle, Case("c", "A", "B", amount), step=step, max_splits=2)
    assert result.status is SolveStatus.OK and result.evaluation is not None and result.plan
    evaluation = result.evaluation
    assert evaluation.residuals == {}
    funds = {f.fund_id: f for f in evaluation.funds}
    assert funds[REQUEST_FUND_ID].consumed == amount and funds[REQUEST_FUND_ID].remaining == 0
    assert sum(a for _, a in _legs(result)) == amount
    firsts = [s for s in result.plan.steps if s.inputs[0].fund_id == REQUEST_FUND_ID]
    assert firsts[-1].inputs[0].amount == ALL_REMAINING  # the explicit final allocation
    assert all(isinstance(s.inputs[0].amount, int) for s in firsts[:-1])
    later = [s for s in result.plan.steps if s.inputs[0].fund_id != REQUEST_FUND_ID]
    assert all(s.inputs[0].amount == ALL_REMAINING for s in later)


def test_split_path_plan_assigns_the_remainder_to_the_final_leg() -> None:
    case = Case("c", "A", "B", 1_000_003)
    paths = (
        (Edge("ab1", "A", "B"),),
        (Edge("ac1", "A", "C"), Edge("cb1", "C", "B")),
        (Edge("ac2", "A", "C"), Edge("cb2", "C", "B")),
    )
    plan = path_split.split_path_plan(case, paths, (3, 3, 4))
    ev = evaluate(PARALLEL, case, plan, gross_only())
    assert ev.status is EvalStatus.OK and ev.residuals == {}
    firsts = [t.amount_in for t in ev.trace if t.inputs[0][0] == REQUEST_FUND_ID]
    assert firsts[:2] == [1_000_003 * 3 // 10] * 2
    assert sum(firsts) == case.amount_in
    assert [s.output_fund_id for s in plan.steps] == ["OUT1", "L2H1", "OUT2", "L3H1", "OUT3"]


# ------------------------------------------------------------ objective


@dataclass(frozen=True)
class PerCallCost(ObjectiveContext):
    """Test-only complete-plan cost that grows with the number of pool calls -- a
    non-additive stand-in for a later execution-cost model (docs/DESIGN.md §2.9)."""

    per_call: int = 0

    def score(self, evaluation: Evaluation) -> int:
        return evaluation.gross_output - self.per_call * evaluation.route_features["pool_calls"]


def test_net_cost_reversal_keeps_the_simpler_route() -> None:
    bundle = fixture("cpmm_graph")
    case = bundle.case("a_b_large")
    gross = _solve(bundle, case)
    assert gross.search_stats["chosen_source"] == "path_split"
    objective = PerCallCost(mode="synthetic_fixed_cost", per_call=20_000_000)
    net = _solve(bundle, case, objective=objective)
    assert net.status is SolveStatus.OK and net.plan is not None and net.evaluation is not None
    assert net.search_stats["chosen_source"] in ("single_path", "direct_split")
    assert len(net.plan.steps) < len(gross.plan.steps)  # type: ignore[union-attr]
    assert (
        net.score
        == max(int(f["score"]) for f in net.search_stats["finalists"])
        == net.evaluation.gross_output - 20_000_000 * len(net.plan.steps)
    )


# ------------------------------------------------------------ statuses


def test_unreachable_pair_is_no_route_with_the_single_path_reason() -> None:
    bundle = _bundle(_cp("ab", "A", "B", 10**6, 10**6), _cp("cd", "C", "D", 10**6, 10**6))
    result = _solve(bundle, Case("c", "A", "D", 1000))
    assert result.status is SolveStatus.NO_ROUTE
    assert result.error is not None and "unreachable" in result.error


def test_hop_cap_is_no_route() -> None:
    bundle = fixture("cpmm_graph")
    result = _solve(bundle, bundle.case("a_d_multi_hop"), max_hops=1)
    assert result.status is SolveStatus.NO_ROUTE
    assert result.error is not None and "hop cap" in result.error


def test_dust_keeps_the_only_valid_route() -> None:
    bundle = fixture("cpmm_graph")
    result = _solve(bundle, bundle.case("a_b_dust"))
    assert result.status is SolveStatus.OK
    assert _pools_used(result) == ["ab_1"] and result.score == 2


USDT = "0x201eba5cc46d216ce6dc03f6a759e8e766e956ae"
WMNT = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"
UNI_USDT_WMNT = "0x4cdfc22bf05209de87ee564746dc7e5174631d2b"


def test_only_incomplete_candidates_is_incomplete_snapshot_not_no_route() -> None:
    bundle = _bundle(fixture("mantle_mixed").pools[UNI_USDT_WMNT])
    result = _solve(bundle, Case("huge", USDT, WMNT, 10**18))
    assert result.status is SolveStatus.INCOMPLETE_SNAPSHOT
    assert result.error is not None and "uncollected" in result.error


def test_quote_budget_stops_before_the_meter_and_keeps_the_simpler_route() -> None:
    bundle = fixture("cpmm_graph")
    case = bundle.case("a_b_large")
    unbounded = _solve(bundle, case)
    limit = unbounded.search_stats["quotes_executed"] // 2
    with metered_quotes(limit) as meter:
        result = _solve(bundle, case, budget=Budget(max_quotes=limit))
    assert not meter.exceeded and meter.counted == result.search_stats["quotes_executed"] <= limit
    assert result.status is SolveStatus.OK
    assert result.search_stats["truncated_by"] == "max_quotes"
    assert result.candidates_truncated > 0
    assert result.score is not None and unbounded.score is not None
    assert result.score <= unbounded.score


def test_budget_truncation_without_a_route_is_timeout_never_no_route() -> None:
    bundle = fixture("cpmm_graph")
    result = _solve(bundle, bundle.case("a_d_multi_hop"), budget=Budget(max_quotes=1))
    assert result.status is SolveStatus.TIMEOUT
    assert result.error is not None and "not evidence of no_route" in result.error


def test_candidate_cap_is_declared_truncation() -> None:
    bundle = fixture("cpmm_graph")
    result = _solve(bundle, bundle.case("a_b_large"), budget=Budget(max_candidates=1))
    assert result.status is SolveStatus.OK
    assert result.search_stats["truncated_by"] is not None
    assert result.candidates_truncated > 0


# ------------------------------------------------------------ accounting / plumbing


def test_quote_accounting_is_exact_and_the_final_replay_is_memoized() -> None:
    bundle = fixture("cpmm_graph")
    case = bundle.case("a_b_large")
    with metered_quotes(None) as meter:
        result = _solve(bundle, case)
    assert result.search_stats["quotes_executed"] == meter.counted
    assert result.search_stats["quotes_memoized"] > 0


def test_best_plans_are_published_in_improving_order() -> None:
    bundle = fixture("cpmm_graph")
    published: list[Any] = []
    result = _solve(bundle, bundle.case("a_b_large"), sink=published)
    assert published and published[-1] == result.plan
    scores = [
        evaluate(bundle, bundle.case("a_b_large"), p, gross_only()).gross_output for p in published
    ]
    assert scores == sorted(scores) and len(set(scores)) == len(scores)


def test_solve_is_repeatable_with_one_prepared_config() -> None:
    bundle = fixture("mantle_mixed")
    case = bundle.case("usdt_wmnt_evidence_split")
    first, second = _solve(bundle, case), _solve(bundle, case)
    assert first.plan == second.plan and first.search_stats == second.search_stats


def test_registered_with_multi_hop_split_capability() -> None:
    factory = ALGORITHMS["path_split"]
    assert factory is path_split.FACTORY
    assert factory.capabilities.to_dict() == {
        "multi_hop": True,
        "split": True,
        "shared_pools": False,
    }
    assert factory.search_params == ("max_hops", "max_splits", "percent_step")


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"max_splits": 2, "percent_step": 5},
        {"max_hops": 3, "percent_step": 5},
        {"max_hops": 3, "max_splits": 2},
        {"max_hops": 0, "max_splits": 2, "percent_step": 5},
        {"max_hops": 3, "max_splits": 0, "percent_step": 5},
        {"max_hops": 3, "max_splits": 2, "percent_step": 3},
    ],
)
def test_prepare_rejects_missing_or_invalid_settings(params: dict[str, Any]) -> None:
    with pytest.raises(path_split.PathSplitConfigError):
        path_split.prepare(OVERLAP, AlgorithmConfig(path_split.NAME, params))


def test_solve_without_prepare_is_an_error() -> None:
    with pytest.raises(TypeError, match="prepare"):
        path_split.solve(Case("c", "A", "B", 1), SolveContext(OVERLAP, gross_only()), Budget())


PROFILE: dict[str, Any] = {
    "schema_version": 2,
    "algorithms": ["direct", "single_path", "direct_split", "path_split"],
    "objective": {"mode": "gross_only"},
    "budget": {"time_limit_seconds": 30, "max_quotes": 20000, "max_candidates": None},
    "measurement": {"warmup": 0, "repeats": 1, "seed": 1, "order": "fixed", "memory_pass": False},
    "worker": {"start_method": "spawn", "scope": "algorithm", "prepare_time_limit_seconds": 30},
}


def test_profile_must_declare_every_search_parameter() -> None:
    with pytest.raises(ProfileError, match=r"search\.max_hops"):
        parse_profile(
            {
                **PROFILE,
                "algorithms": ["path_split"],
                "search": {"max_splits": 4, "percent_step": 5},
            },
            "p.yaml",
        )
    profile = parse_profile({**PROFILE, "search": CONFIG}, "p.yaml")
    assert profile.algorithm_config(path_split.FACTORY).params == CONFIG


def test_isolated_runner_records_the_path_split_search(tmp_path: Path) -> None:
    from benchmark.results import load_case_records
    from benchmark.runner import run_experiment

    bundle = fixture("mantle_mixed")
    profile = parse_profile({**PROFILE, "search": CONFIG}, "p.yaml")
    manifest = run_experiment(bundle, profile, results_dir=tmp_path, replay_command="cmd")
    assert manifest.complete
    records = {(r["algorithm"], r["case_id"]): r for r in load_case_records(manifest.run_dir)}
    for case in bundle.cases:
        ps = records[("path_split", case.case_id)]
        assert ps["status"] == "ok"  # the runner's own independent re-evaluation
        assert ps["quotes"]["counted"] == ps["search"]["quotes_executed"]
        for other in ("single_path", "direct_split"):
            rec = records[(other, case.case_id)]
            if rec["status"] == "ok":
                assert int(ps["score"]) >= int(rec["score"])

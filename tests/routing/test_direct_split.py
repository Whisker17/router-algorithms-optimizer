"""`direct_split` (docs/DESIGN.md §2.6, WHI-1439).

Expected values are independent of the code under test:

- **Brute-force enumeration** (`_brute`): every unit vector over the direct pools
  (`itertools.product`) summing to the grid size with at most `max_splits` nonzero
  legs, turned into integer amounts by the declared rule (floored shares, remainder to
  the last nonzero leg in pool order) and scored by summing independent per-leg
  outputs -- no dynamic program, no plan builder, no quote cache.
- **Hand-derived CPMM outputs** (`_v2_out`): the Solidity `getAmountOut` formula
  generalized to `fee_bps`; headline vectors are pinned as literals.
- **Real mixed-family states** (`tests/fixtures/routing/mantle_mixed`): per-leg
  outputs from a plain, memo-free one-step `evaluate()` of each leg.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from benchmark.objective import ObjectiveContext, gross_only, synthetic_fixed_cost
from benchmark.profile import ProfileError, parse_profile
from pools.quote import metered_quotes
from routing.algorithms import direct, direct_split
from routing.algorithms.base import (
    AlgorithmConfig,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
)
from routing.algorithms.registry import ALGORITHMS
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
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
    pool_id: str, r0: int, r1: int, fee_bps: int = 30, source_key: str | None = None
) -> ConstantProductPoolState:
    return ConstantProductPoolState(
        pool_id=pool_id,
        token0="A",
        token1="B",
        reserve0=r0,
        reserve1=r1,
        fee_bps=fee_bps,
        source_key=source_key,
    )


def _solve(
    bundle: SnapshotBundle,
    case: Case,
    *,
    step: int = 5,
    max_splits: int = 4,
    budget: Budget | None = None,
    objective: ObjectiveContext | None = None,
    sink: list[Any] | None = None,
) -> SolveResult:
    prepared = direct_split.prepare(
        bundle,
        AlgorithmConfig(direct_split.NAME, {"max_splits": max_splits, "percent_step": step}),
    )
    context = SolveContext(
        bundle=bundle,
        objective=objective or gross_only(),
        prepared=prepared,
        candidate_sink=None if sink is None else sink.append,
    )
    return direct_split.solve(case, context, budget or Budget())


def _legs(result: SolveResult) -> tuple[tuple[str, int], ...]:
    """The returned plan's (pool, resolved input amount) legs, from the evaluator's own
    trace -- not from the solver's metadata."""
    assert result.evaluation is not None
    return tuple((t.pool_id, t.amount_in) for t in result.evaluation.trace)


# ------------------------------------------------------------ independent oracles


def _v2_out(pool: PoolState, token_in: str, amount_in: int) -> int | None:
    """Hand `getAmountOut`; `None` where the pool would fail (dust output, uint112
    overflow of a sourced Moe pair)."""
    assert isinstance(pool, ConstantProductPoolState)
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


def _evaluated_out(bundle: SnapshotBundle) -> Callable[[PoolState, str, int], int | None]:
    """Per-leg output from a memo-free one-step replay (real mixed-family states)."""

    def out(pool: PoolState, token_in: str, amount_in: int) -> int | None:
        step = SwapStep(
            pool_id=pool.pool_id,
            token_in=token_in,
            token_out=pool.other_token(token_in),
            inputs=(FundInput(REQUEST_FUND_ID, ALL_REMAINING),),
            output_fund_id="O",
        )
        case = Case("leg", token_in, pool.other_token(token_in), amount_in)
        ev = evaluate(bundle, case, RoutePlan((step,)), gross_only())
        return ev.gross_output if ev.status is EvalStatus.OK else None

    return out


Legs = tuple[tuple[str, int], ...]


def _brute(
    bundle: SnapshotBundle,
    case: Case,
    step: int,
    max_splits: int,
    out: Callable[[PoolState, str, int], int | None] = _v2_out,
    per_leg_cost: int = 0,
) -> tuple[int | None, set[Legs]]:
    """(best score, every allocation achieving it) over the whole declared grid."""
    pools = bundle.pools_for_pair(case.token_in, case.token_out)
    n_units = 100 // step
    best: int | None = None
    winners: set[Legs] = set()
    for units in itertools.product(range(n_units + 1), repeat=len(pools)):
        chosen = [i for i, u in enumerate(units) if u]
        if sum(units) != n_units or len(chosen) > max_splits:
            continue
        amounts = {i: case.amount_in * units[i] // n_units for i in chosen[:-1]}
        amounts[chosen[-1]] = case.amount_in - sum(amounts.values())
        legs = tuple((pools[i].pool_id, a) for i, a in sorted(amounts.items()) if a > 0)
        assert sum(a for _, a in legs) == case.amount_in
        outs = [out(bundle.pools[p], case.token_in, a) for p, a in legs]
        if any(o is None for o in outs):
            continue
        score = sum(o for o in outs if o is not None) - per_leg_cost * len(legs)
        if best is None or score > best:
            best, winners = score, {legs}
        elif score == best:
            winners.add(legs)
    return best, winners


# ------------------------------------------------------------ fixtures

# Two identical pools: splitting a large order halves each leg's price impact.
TWIN = _bundle(_cp("p1", 10**9, 10**9), _cp("p2", 10**9, 10**9))
# Unequal fees: a cheap shallow pool and a costly deep one.
FEES = _bundle(_cp("cheap_shallow", 10**9, 10**9, fee_bps=5), _cp("deep", 10**10, 10**10))
# Unequal prices and depths, three pools.
PRICES = _bundle(
    _cp("q1", 10**9, 10**9),
    _cp("q2", 10**9, 1_020_000_000, fee_bps=100),
    _cp("q3", 3 * 10**8, 3 * 10**8, fee_bps=1),
)


# ------------------------------------------------------------ acceptance behavior


def test_two_pool_split_beats_the_best_single_pool() -> None:
    result = _solve(TWIN, Case("big", "A", "B", 10**8))
    assert result.status is SolveStatus.OK
    single = _v2_out(TWIN.pools["p1"], "A", 10**8)
    assert single == 90_661_089 == int(result.search_stats["best_single_gross"])
    assert result.score == 94_965_946 == 2 * _v2_out(TWIN.pools["p1"], "A", 5 * 10**7)  # type: ignore[operator]
    assert _legs(result) == (("p1", 5 * 10**7), ("p2", 5 * 10**7))
    assert result.search_stats["best_splits"] == 2
    direct_result = direct.solve(
        Case("big", "A", "B", 10**8), SolveContext(TWIN, gross_only()), Budget()
    )
    assert direct_result.score == single < result.score


def test_small_trade_correctly_stays_unsplit() -> None:
    case = Case("small", "A", "B", 10_000)
    result = _solve(FEES, case)
    assert result.status is SolveStatus.OK
    assert _legs(result) == (("cheap_shallow", 10_000),)
    assert result.score == 9_994 > _v2_out(FEES.pools["deep"], "A", 10_000)  # type: ignore[operator]
    assert result.search_stats["best_splits"] == 1
    best, winners = _brute(FEES, case, 5, 4)
    assert result.score == best and _legs(result) in winners
    # The same pools do split a large order.
    large = _solve(FEES, Case("large", "A", "B", 10**8))
    assert _legs(large) == (("cheap_shallow", 10**7), ("deep", 9 * 10**7))
    assert large.score == 98_828_101 > 98_715_803 == _v2_out(FEES.pools["deep"], "A", 10**8)


@pytest.mark.parametrize("amount", [1, 2, 3, 7, 19, 21, 1_000_003, 10**8 + 17])
@pytest.mark.parametrize("step", [5, 20, 25, 50])
def test_tiny_and_nondivisible_amounts_fully_allocate(amount: int, step: int) -> None:
    # 1 A buys ~1000 B, so even a 1-unit leg has a nonzero output.
    bundle = _bundle(_cp("p1", 10**9, 10**12, fee_bps=0), _cp("p2", 10**9, 10**12, fee_bps=0))
    case = Case("c", "A", "B", amount)
    result = _solve(bundle, case, step=step, max_splits=2)
    assert result.status is SolveStatus.OK and result.evaluation is not None
    evaluation = result.evaluation
    assert evaluation.residuals == {}
    assert sum(t.amount_in for t in evaluation.trace) == amount  # no overspend, no dust
    funds = {f.fund_id: f for f in evaluation.funds}
    assert funds[REQUEST_FUND_ID].consumed == amount and funds[REQUEST_FUND_ID].remaining == 0
    assert all(t.status == "ok" for t in evaluation.trace)  # no zero-input legs emitted
    assert result.plan is not None
    *explicit, final = result.plan.steps
    assert final.inputs[0].amount == ALL_REMAINING  # the explicit final allocation
    assert all(isinstance(s.inputs[0].amount, int) for s in explicit)
    best, winners = _brute(bundle, case, step, 2)
    assert result.score == best and _legs(result) in winners


def test_every_allocation_plan_assigns_the_remainder_to_the_final_leg() -> None:
    case = Case("c", "A", "B", 1_000_003)
    for legs in [((0, 7), (1, 13)), ((1, 1), (2, 1), (0, 18)), ((0, 3), (1, 3), (2, 14))]:
        amounts = direct_split.leg_amounts(case.amount_in, legs, 20)
        assert sum(amounts) == case.amount_in
        floors = [case.amount_in * u // 20 for _, u in legs]
        assert amounts[:-1] == floors[:-1]
        assert 0 <= amounts[-1] - floors[-1] < len(legs)
        plan = direct_split.allocation_plan(case, ("q1", "q2", "q3"), legs)
        ev = evaluate(PRICES, case, plan, gross_only())
        assert ev.status is EvalStatus.OK and ev.residuals == {}
        assert [t.amount_in for t in ev.trace] == amounts


EXHAUSTIVE = [
    (TWIN, 10**8, 5, 4),
    (TWIN, 123_456_789, 10, 2),
    (FEES, 10**8, 5, 2),
    (FEES, 10**7 + 3, 10, 2),
    (PRICES, 2 * 10**8 + 1, 10, 3),
    (PRICES, 5 * 10**8, 20, 3),
    (PRICES, 5 * 10**8, 20, 2),
    (PRICES, 777, 25, 3),
    (PRICES, 10**6, 50, 3),
    (PRICES, 10**8, 100, 3),
]


@pytest.mark.parametrize(("bundle", "amount", "step", "max_splits"), EXHAUSTIVE)
def test_matches_exhaustive_discrete_enumeration(
    bundle: SnapshotBundle, amount: int, step: int, max_splits: int
) -> None:
    case = Case("c", "A", "B", amount)
    result = _solve(bundle, case, step=step, max_splits=max_splits)
    best, winners = _brute(bundle, case, step, max_splits)
    assert result.status is SolveStatus.OK
    assert result.score == best
    assert _legs(result) in winners
    assert len(_legs(result)) <= max_splits


@pytest.mark.parametrize("case_id", ["usdt_wmnt_evidence_split", "wmnt_usdt_evidence_split"])
def test_real_mixed_family_choice_matches_exhaustive_enumeration(case_id: str) -> None:
    bundle = fixture("mantle_mixed")
    case = bundle.case(case_id)
    assert len(bundle.pools_for_pair(case.token_in, case.token_out)) == 4
    result = _solve(bundle, case, step=20, max_splits=3)
    best, winners = _brute(bundle, case, 20, 3, out=_evaluated_out(bundle))
    assert result.status is SolveStatus.OK
    assert result.score == best and _legs(result) in winners
    fresh = evaluate(bundle, case, result.plan, gross_only())  # type: ignore[arg-type]
    assert fresh.status is EvalStatus.OK and fresh.gross_output == result.score
    assert fresh.trace == result.evaluation.trace  # type: ignore[union-attr]


def test_max_splits_one_is_the_best_direct_pool() -> None:
    bundle = fixture("mantle_mixed")
    for case in bundle.cases:
        split = _solve(bundle, case, max_splits=1)
        single = direct.solve(case, SolveContext(bundle, gross_only()), Budget())
        if single.status is SolveStatus.OK:
            assert split.status is SolveStatus.OK and split.score == single.score
            assert split.plan is not None and len(split.plan.steps) == 1


def test_a_percent_step_of_100_only_samples_full_input() -> None:
    result = _solve(TWIN, Case("big", "A", "B", 10**8), step=100)
    assert result.search_stats["grid_units"] == 1
    assert result.search_stats["samples_quoted"] == 2
    assert _legs(result) == (("p1", 10**8),)  # ties keep the first admitted pool


def test_a_pool_failing_at_full_size_still_helps_a_small_split() -> None:
    # A sourced Moe pair near the uint112 reserve cap: 100% reverts, <= 60% fits.
    near_cap = _cp(
        "near_cap", MAX_U112 - 60_000_000, MAX_U112 - 60_000_000, source_key="moe_classic_v1"
    )
    bundle = _bundle(_cp("shallow", 10**9, 10**9), near_cap)
    case = Case("c", "A", "B", 10**8)
    result = _solve(bundle, case)
    assert result.status is SolveStatus.OK
    assert result.search_stats["samples_failed"].get("reverted", 0) >= 1
    assert result.search_stats["best_single_pool"] == "shallow"
    assert _legs(result) == (("shallow", 4 * 10**7), ("near_cap", 6 * 10**7))
    best, winners = _brute(bundle, case, 5, 4)
    assert result.score == best and _legs(result) in winners
    assert best is not None and best > int(result.search_stats["best_single_gross"])


# ------------------------------------------------------------ objective


@dataclass(frozen=True)
class PerCallCost(ObjectiveContext):
    """Test-only complete-plan cost that grows with the number of pool calls -- a
    non-additive stand-in for a later execution-cost model (docs/DESIGN.md §2.9)."""

    per_call: int = 0

    def score(self, evaluation: Evaluation) -> int:
        return evaluation.gross_output - self.per_call * evaluation.route_features["pool_calls"]


def test_net_cost_reversal_keeps_the_single_pool() -> None:
    case = Case("big", "A", "B", 10**8)
    gross = _solve(TWIN, case)
    assert gross.search_stats["best_splits"] == 2  # 94,965,946 > 90,661,089 gross
    objective = PerCallCost(mode="synthetic_fixed_cost", fixed_cost=0, per_call=5_000_000)
    net = _solve(TWIN, case, objective=objective)
    assert net.status is SolveStatus.OK
    assert _legs(net) == (("p1", 10**8),)
    assert net.score == 90_661_089 - 5_000_000
    best, winners = _brute(TWIN, case, 5, 4, per_leg_cost=5_000_000)
    assert net.score == best and _legs(net) in winners
    # A small per-call cost does not reverse the ranking.
    cheap = _solve(TWIN, case, objective=PerCallCost(mode="synthetic_fixed_cost", per_call=1_000))
    assert cheap.search_stats["best_splits"] == 2


def test_fixed_cost_objective_scores_the_choice() -> None:
    result = _solve(TWIN, Case("big", "A", "B", 10**8), objective=synthetic_fixed_cost(1_000))
    assert result.score == 94_965_946 - 1_000
    assert result.evaluation is not None
    assert result.evaluation.objective_label.startswith("SYNTHETIC")


# ------------------------------------------------------------ statuses


def test_no_direct_pool_is_no_route() -> None:
    bundle = fixture("cpmm_graph")
    result = _solve(bundle, bundle.case("a_d_multi_hop"))
    assert result.status is SolveStatus.NO_ROUTE
    assert result.error is not None and "no admitted direct pool" in result.error


def test_insufficient_liquidity_everywhere_is_no_route() -> None:
    bundle = _bundle(_cp("dry1", 0, 10**6), _cp("dry2", 10**6, 0))
    result = _solve(bundle, Case("c", "A", "B", 1_000))
    assert result.status is SolveStatus.NO_ROUTE
    assert result.search_stats["samples_failed"] == {
        "insufficient_liquidity": result.search_stats["samples_quoted"]
    }
    assert result.error is not None and "insufficient_liquidity" in result.error


def test_dust_keeps_the_only_valid_direct_pool() -> None:
    bundle = fixture("cpmm_graph")
    result = _solve(bundle, bundle.case("a_b_dust"))  # 3 TKA; ab_dry fails every quote
    assert result.status is SolveStatus.OK
    assert _legs(result) == (("ab_1", 3),) and result.score == 2
    assert result.search_stats["samples_failed"]["insufficient_liquidity"] >= 1


USDT = "0x201eba5cc46d216ce6dc03f6a759e8e766e956ae"
WMNT = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"
UNI_USDT_WMNT = "0x4cdfc22bf05209de87ee564746dc7e5174631d2b"


def test_only_incomplete_samples_is_incomplete_snapshot_not_no_route() -> None:
    bundle = _bundle(fixture("mantle_mixed").pools[UNI_USDT_WMNT])
    result = _solve(bundle, Case("huge", USDT, WMNT, 10**18))
    assert result.status is SolveStatus.INCOMPLETE_SNAPSHOT
    assert result.search_stats["samples_incomplete"] >= 1
    assert result.error is not None and "uncollected" in result.error


def test_quote_budget_truncation_keeps_the_single_pool_and_stops_before_the_meter() -> None:
    case = Case("big", "A", "B", 10**8)
    with metered_quotes(2) as meter:
        result = _solve(TWIN, case, budget=Budget(max_quotes=2))
    assert not meter.exceeded and meter.counted == 2 == result.search_stats["quotes_executed"]
    assert result.status is SolveStatus.OK
    assert _legs(result) == (("p1", 10**8),)
    assert result.search_stats["truncated_by"] == "max_quotes"
    assert result.candidates_truncated == result.search_stats["samples_skipped"] > 0


def test_budget_truncation_without_a_route_is_timeout_never_no_route() -> None:
    bundle = _bundle(_cp("dry", 0, 10**6), _cp("p", 10**9, 10**9))
    result = _solve(bundle, Case("c", "A", "B", 1_000), budget=Budget(max_quotes=1))
    assert result.status is SolveStatus.TIMEOUT
    assert result.error is not None and "not evidence of no_route" in result.error
    untruncated = _solve(bundle, Case("c", "A", "B", 1_000))
    assert untruncated.status is SolveStatus.OK


def test_candidate_cap_evaluates_the_single_pool_first() -> None:
    result = _solve(TWIN, Case("big", "A", "B", 10**8), budget=Budget(max_candidates=1))
    assert result.status is SolveStatus.OK
    assert _legs(result) == (("p1", 10**8),)
    assert result.search_stats["finalists_evaluated"] == 1
    assert result.search_stats["truncated_by"] == "max_candidates"
    assert result.candidates_truncated == result.search_stats["finalists_truncated"] >= 1


# ------------------------------------------------------------ accounting / plumbing


def test_quote_accounting_is_exact_and_final_evaluation_is_memoized() -> None:
    case = Case("c", "A", "B", 2 * 10**8 + 1)
    with metered_quotes(None) as meter:
        result = _solve(PRICES, case, step=10, max_splits=3)
    stats = result.search_stats
    assert stats["quotes_executed"] == meter.counted == stats["samples_quoted"]
    assert stats["quotes_memoized"] >= len(_legs(result))  # the final replay costs nothing
    assert stats["grid_units"] == 10 and stats["direct_pools"] == 3
    assert stats["finalists"] <= 3 and stats["finalists_evaluated"] == stats["finalists"]
    assert result.candidates_considered == stats["finalists_evaluated"]
    allocation = stats["best_allocation"]
    assert [(a["pool_id"], int(a["amount_in"])) for a in allocation] == list(_legs(result))
    assert sum(a["percent"] for a in allocation) == 100


def test_best_single_pool_is_published_before_any_split() -> None:
    published: list[Any] = []
    result = _solve(TWIN, Case("big", "A", "B", 10**8), sink=published)
    assert len(published) == 2
    assert len(published[0].steps) == 1 and published[-1] == result.plan


def test_solve_is_repeatable_with_one_prepared_config() -> None:
    bundle = fixture("mantle_mixed")
    case = bundle.case("usdt_wmnt_evidence_split")
    first, second = _solve(bundle, case), _solve(bundle, case)
    assert first.plan == second.plan and first.search_stats == second.search_stats


def test_registered_with_direct_split_capability() -> None:
    factory = ALGORITHMS["direct_split"]
    assert factory is direct_split.FACTORY
    assert factory.capabilities.to_dict() == {"multi_hop": False, "split": True}
    assert factory.search_params == ("max_splits", "percent_step")


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"max_splits": 2},
        {"percent_step": 5},
        {"max_splits": 0, "percent_step": 5},
        {"max_splits": True, "percent_step": 5},
        {"max_splits": 2, "percent_step": 0},
        {"max_splits": 2, "percent_step": 3},
        {"max_splits": 2, "percent_step": 200},
        {"max_splits": 2, "percent_step": 5.0},
    ],
)
def test_prepare_rejects_missing_or_invalid_grid_settings(params: dict[str, Any]) -> None:
    with pytest.raises(direct_split.DirectSplitConfigError):
        direct_split.prepare(TWIN, AlgorithmConfig(direct_split.NAME, params))


def test_solve_without_prepare_is_an_error() -> None:
    with pytest.raises(TypeError, match="prepare"):
        direct_split.solve(Case("c", "A", "B", 1), SolveContext(TWIN, gross_only()), Budget())


PROFILE: dict[str, Any] = {
    "schema_version": 2,
    "algorithms": ["direct", "direct_split"],
    "objective": {"mode": "gross_only"},
    "budget": {"time_limit_seconds": 5, "max_quotes": 1000, "max_candidates": None},
    "measurement": {"warmup": 0, "repeats": 1, "seed": 1, "order": "fixed", "memory_pass": False},
    "worker": {"start_method": "spawn", "scope": "algorithm", "prepare_time_limit_seconds": 30},
}


def test_profile_must_declare_the_grid() -> None:
    with pytest.raises(ProfileError, match=r"search\.max_splits, search\.percent_step"):
        parse_profile(PROFILE, "p.yaml")
    with pytest.raises(ProfileError, match=r"search\.percent_step"):
        parse_profile({**PROFILE, "search": {"max_splits": 4}}, "p.yaml")
    profile = parse_profile({**PROFILE, "search": {"max_splits": 4, "percent_step": 5}}, "p.yaml")
    assert profile.algorithm_config(direct_split.FACTORY).params == {
        "max_splits": 4,
        "percent_step": 5,
    }
    assert profile.resolved()["algorithm_config"]["direct_split"]["capabilities"] == {
        "multi_hop": False,
        "split": True,
    }


@pytest.mark.parametrize(
    ("search", "match"),
    [
        ({"max_splits": 0, "percent_step": 5}, "max_splits"),
        ({"max_splits": 4, "percent_step": 0}, "percent_step"),
        ({"max_splits": 4, "percent_step": 3}, "divisor of 100"),
        ({"max_splits": 4, "percent_step": 150}, "divisor of 100"),
        ({"max_splits": 4, "percent_step": "5"}, "percent_step"),
    ],
)
def test_profile_rejects_invalid_grid_values(search: dict[str, Any], match: str) -> None:
    with pytest.raises(ProfileError, match=match):
        parse_profile({**PROFILE, "search": search}, "p.yaml")


def test_isolated_runner_records_the_split_search(tmp_path: Path) -> None:
    from benchmark.results import load_case_records
    from benchmark.runner import run_experiment

    bundle = fixture("mantle_mixed")
    profile = parse_profile({**PROFILE, "search": {"max_splits": 3, "percent_step": 20}}, "p.yaml")
    manifest = run_experiment(bundle, profile, results_dir=tmp_path, replay_command="cmd")
    assert manifest.complete
    events = {e["algorithm"]: e for e in manifest.prepare_events}
    assert events["direct_split"]["status"] == "ok"
    records = {(r["algorithm"], r["case_id"]): r for r in load_case_records(manifest.run_dir)}
    for case in bundle.cases:
        split, single = records[("direct_split", case.case_id)], records[("direct", case.case_id)]
        assert split["status"] == "ok"  # the runner's own independent re-evaluation
        assert split["quotes"]["counted"] == split["search"]["quotes_executed"]
        if single["status"] == "ok":
            assert int(split["score"]) >= int(single["score"])
        else:
            # `direct` fails the whole case when one pool is incomplete at full size;
            # `direct_split` excludes and discloses such samples instead.
            assert single["status"] == "incomplete_snapshot"
            assert split["search"]["samples_incomplete"] > 0

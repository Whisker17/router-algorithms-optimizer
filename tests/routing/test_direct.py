"""Unit tests for `routing.algorithms.direct`: candidate comparison, typed
no-route, and objective-mode plumbing (docs/DESIGN.md §2.6 acceptance criteria).
"""

from __future__ import annotations

from benchmark.objective import gross_only, synthetic_fixed_cost
from routing.algorithms.base import Budget, SolveContext, SolveStatus
from routing.algorithms.direct import solve
from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle

BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)

POOL_BETTER = ConstantProductPoolState(
    pool_id="pool_a", token0="TKA", token1="TKB", reserve0=1_000_000, reserve1=3_000_000, fee_bps=30
)
POOL_WORSE = ConstantProductPoolState(
    pool_id="pool_b", token0="TKA", token1="TKB", reserve0=2_000_000, reserve1=3_000_000, fee_bps=30
)


def _bundle(pools: dict[str, ConstantProductPoolState]) -> SnapshotBundle:
    return SnapshotBundle(
        bundle_id="b",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools=pools,
        cases=(),
        bundle_hash="deadbeef",
        source_path="<test>",
    )


def test_direct_prefers_better_priced_pool() -> None:
    bundle = _bundle({"pool_a": POOL_BETTER, "pool_b": POOL_WORSE})
    case = Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100_000)
    result = solve(case, SolveContext(bundle=bundle, objective=gross_only()), Budget())
    assert result.status is SolveStatus.OK
    assert result.candidates_considered == 2
    assert result.plan is not None
    assert result.plan.steps[0].pool_id == "pool_a"
    assert result.evaluation is not None
    assert result.evaluation.gross_output == 271_983  # see tests/pools/test_constant_product.py
    assert result.score == 271_983


def test_direct_ignores_unrelated_pool_and_still_picks_best() -> None:
    unrelated = ConstantProductPoolState(
        pool_id="pool_c", token0="TKA", token1="TKZ", reserve0=1, reserve1=1, fee_bps=30
    )
    bundle = _bundle({"pool_a": POOL_BETTER, "pool_b": POOL_WORSE, "pool_c": unrelated})
    case = Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100_000)
    result = solve(case, SolveContext(bundle=bundle, objective=gross_only()), Budget())
    assert result.status is SolveStatus.OK
    assert result.candidates_considered == 2  # pool_c is not for this pair
    assert result.plan is not None
    assert result.plan.steps[0].pool_id == "pool_a"


def test_direct_no_route_when_no_pool_admitted() -> None:
    bundle = _bundle({"pool_a": POOL_BETTER})
    case = Case(case_id="c1", token_in="TKA", token_out="TKD", amount_in=100_000)
    result = solve(case, SolveContext(bundle=bundle, objective=gross_only()), Budget())
    assert result.status is SolveStatus.NO_ROUTE
    assert result.candidates_considered == 0
    assert result.plan is None
    assert result.evaluation is None
    assert result.error is not None


def test_direct_no_route_when_every_candidate_fails() -> None:
    dry_pool = ConstantProductPoolState(
        pool_id="pool_dry", token0="TKA", token1="TKB", reserve0=0, reserve1=1000, fee_bps=30
    )
    bundle = _bundle({"pool_dry": dry_pool})
    case = Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100_000)
    result = solve(case, SolveContext(bundle=bundle, objective=gross_only()), Budget())
    assert result.status is SolveStatus.NO_ROUTE
    assert result.candidates_considered == 1


def test_direct_synthetic_fixed_cost_objective_changes_score_not_gross_output() -> None:
    bundle = _bundle({"pool_a": POOL_BETTER, "pool_b": POOL_WORSE})
    case = Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100_000)
    objective = synthetic_fixed_cost(1_000)
    result = solve(case, SolveContext(bundle=bundle, objective=objective), Budget())
    assert result.status is SolveStatus.OK
    assert result.score == 271_983 - 1_000
    assert result.evaluation is not None
    assert result.evaluation.gross_output == 271_983  # cost never changes the raw evaluation
    assert objective.is_synthetic
    assert objective.label.startswith("SYNTHETIC")


def test_direct_respects_max_candidates_budget() -> None:
    bundle = _bundle({"pool_a": POOL_BETTER, "pool_b": POOL_WORSE})
    case = Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100_000)
    # pools_for_pair returns admitted pools in insertion order (pool_a first);
    # a budget of 1 candidate means only pool_a is ever evaluated.
    result = solve(
        case, SolveContext(bundle=bundle, objective=gross_only()), Budget(max_candidates=1)
    )
    assert result.status is SolveStatus.OK
    assert result.candidates_considered == 1
    assert result.plan is not None
    assert result.plan.steps[0].pool_id == "pool_a"

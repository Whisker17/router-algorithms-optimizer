"""Unit tests for `benchmark.objective.ObjectiveContext`: mode validation and
visible labeling (docs/DESIGN.md §2.9; WHI-1427 acceptance criterion "gross-only
and synthetic-cost objectives share one interface and are visibly labeled").
"""

from __future__ import annotations

import pytest

from benchmark.objective import ObjectiveContext, gross_only, synthetic_fixed_cost
from routing.evaluator import EvalStatus, Evaluation


def _evaluation(
    status: EvalStatus, gross_output: int, estimated_net_output: int | None = None
) -> Evaluation:
    return Evaluation(
        status=status, gross_output=gross_output, estimated_net_output=estimated_net_output
    )


def test_gross_only_scores_by_gross_output() -> None:
    objective = gross_only()
    assert objective.mode == "gross_only"
    assert not objective.is_synthetic
    assert "SYNTHETIC" not in objective.label
    # gross-only evaluations never carry an estimated_net_output (see
    # routing.evaluator._cost_and_net); score() falls back to gross_output.
    assert objective.score(_evaluation(EvalStatus.OK, 500)) == 500


def test_synthetic_fixed_cost_reads_precomputed_net_output_from_the_evaluation() -> None:
    # score() is a thin reader over whatever routing.evaluator.evaluate already
    # computed -- it does not recompute `gross_output - fixed_cost` itself. This
    # mirrors exactly what evaluate(bundle, case, plan, objective) would attach
    # for fixed_cost=120 on a gross_output=500 plan.
    objective = synthetic_fixed_cost(120)
    assert objective.mode == "synthetic_fixed_cost"
    assert objective.is_synthetic
    assert objective.label.startswith("SYNTHETIC")
    evaluation = _evaluation(EvalStatus.OK, 500, estimated_net_output=380)
    assert objective.score(evaluation) == 380


def test_synthetic_fixed_cost_score_can_go_negative() -> None:
    objective = synthetic_fixed_cost(1_000)
    evaluation = _evaluation(EvalStatus.OK, 500, estimated_net_output=-500)
    assert objective.score(evaluation) == -500


def test_score_rejects_non_ok_evaluation() -> None:
    objective = gross_only()
    with pytest.raises(ValueError):
        objective.score(_evaluation(EvalStatus.INVALID_PLAN, 0))


def test_gross_only_rejects_nonzero_fixed_cost() -> None:
    with pytest.raises(ValueError):
        ObjectiveContext(mode="gross_only", fixed_cost=5)


def test_synthetic_fixed_cost_rejects_negative_cost() -> None:
    with pytest.raises(ValueError):
        ObjectiveContext(mode="synthetic_fixed_cost", fixed_cost=-1)


def test_unknown_mode_rejected() -> None:
    with pytest.raises(ValueError):
        ObjectiveContext(mode="not_a_real_mode")  # type: ignore[arg-type]


def test_score_end_to_end_via_evaluate_reads_the_attached_estimate() -> None:
    # End-to-end: evaluate() attaches the cost/net figures, score() reads them
    # back -- no arithmetic is duplicated between the two.
    from routing.evaluator import evaluate
    from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
    from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle

    pool = ConstantProductPoolState(
        pool_id="pool_a", token0="TKA", token1="TKB", reserve0=1000, reserve1=2000, fee_bps=30
    )
    bundle = SnapshotBundle(
        bundle_id="b",
        kind="synthetic",
        schema_version=1,
        block=BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0),
        pools={"pool_a": pool},
        cases=(),
        bundle_hash="deadbeef",
        source_path="<test>",
    )
    case = Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100)
    plan = RoutePlan(
        steps=(
            SwapStep(
                pool_id="pool_a",
                token_in="TKA",
                token_out="TKB",
                inputs=(FundInput(fund_id=REQUEST_FUND_ID, amount=ALL_REMAINING),),
                output_fund_id="OUT",
            ),
        )
    )

    gross_evaluation = evaluate(bundle, case, plan, gross_only())
    assert gross_only().score(gross_evaluation) == 181  # gross_output, no cost attached

    synthetic = synthetic_fixed_cost(50)
    synthetic_evaluation = evaluate(bundle, case, plan, synthetic)
    assert synthetic.score(synthetic_evaluation) == 131  # 181 - 50, read from the evaluation

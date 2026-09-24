"""Unit tests for `benchmark.objective.ObjectiveContext`: mode validation and
visible labeling (docs/DESIGN.md §2.9; WHI-1427 acceptance criterion "gross-only
and synthetic-cost objectives share one interface and are visibly labeled").
"""

from __future__ import annotations

import pytest

from benchmark.objective import ObjectiveContext, gross_only, synthetic_fixed_cost
from routing.evaluator import EvalStatus, Evaluation


def _evaluation(status: EvalStatus, gross_output: int) -> Evaluation:
    return Evaluation(status=status, gross_output=gross_output)


def test_gross_only_scores_by_gross_output() -> None:
    objective = gross_only()
    assert objective.mode == "gross_only"
    assert not objective.is_synthetic
    assert "SYNTHETIC" not in objective.label
    assert objective.score(_evaluation(EvalStatus.OK, 500)) == 500


def test_synthetic_fixed_cost_subtracts_cost_and_is_labeled() -> None:
    objective = synthetic_fixed_cost(120)
    assert objective.mode == "synthetic_fixed_cost"
    assert objective.is_synthetic
    assert objective.label.startswith("SYNTHETIC")
    assert objective.score(_evaluation(EvalStatus.OK, 500)) == 380


def test_synthetic_fixed_cost_score_can_go_negative() -> None:
    objective = synthetic_fixed_cost(1_000)
    assert objective.score(_evaluation(EvalStatus.OK, 500)) == -500


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

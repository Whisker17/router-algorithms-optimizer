"""Shared complete-plan cost/objective seam (docs/DESIGN.md §2.9).

`ObjectiveContext` is the single interface every algorithm uses to score a fully
evaluated plan when choosing among candidates (e.g. `direct` picking the best
admitted pool). Two modes share this one interface:

- `gross_only()` -- scores purely by `Evaluation.gross_output`. This is the only
  mode with any empirical standing before a real cost model lands (the empirical
  cost ticket owns calibration; see docs/DESIGN.md §2.9).
- `synthetic_fixed_cost(...)` -- subtracts a fixed, explicitly labeled synthetic
  cost from gross output. This exists **only** so algorithm/selection-ordering
  tests can exercise cost-sensitive selection before empirical calibration
  exists (docs/DESIGN.md §2.9: "These modes let algorithm work proceed before
  empirical calibration; they cannot substantiate real net-output claims.").
  Every score and every serialized result produced under this mode carries a
  `SYNTHETIC` marker in `label`/`is_synthetic` so a report can never present it
  as a real net-output result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from routing.evaluator import EvalStatus, Evaluation

ObjectiveMode = Literal["gross_only", "synthetic_fixed_cost"]

_MODES: tuple[ObjectiveMode, ...] = ("gross_only", "synthetic_fixed_cost")


@dataclass(frozen=True)
class ObjectiveContext:
    """Construct via `gross_only()`/`synthetic_fixed_cost()` below rather than
    directly, so the mode/`fixed_cost` pairing is always validated."""

    mode: ObjectiveMode
    fixed_cost: int = 0  # only meaningful when mode == "synthetic_fixed_cost";
    # denominated in the case's output-token raw units.

    def __post_init__(self) -> None:
        if self.mode not in _MODES:
            raise ValueError(f"unknown objective mode: {self.mode!r} (expected one of {_MODES})")
        if self.mode == "gross_only" and self.fixed_cost != 0:
            raise ValueError("gross_only objective must not carry a nonzero fixed_cost")
        if self.mode == "synthetic_fixed_cost" and self.fixed_cost < 0:
            raise ValueError(f"fixed_cost must be non-negative, got {self.fixed_cost}")

    @property
    def is_synthetic(self) -> bool:
        return self.mode == "synthetic_fixed_cost"

    @property
    def label(self) -> str:
        """A human-readable, always-visible label. Synthetic-mode labels always
        start with the literal string `SYNTHETIC` so it can never be mistaken
        for a calibrated empirical result in a report or saved run record."""
        if self.mode == "gross_only":
            return "gross-only (no cost model; docs/DESIGN.md §2.9 development mode)"
        return (
            f"SYNTHETIC fixed-cost={self.fixed_cost} (test-only synthetic cost, "
            "NOT an empirical net-output claim -- docs/DESIGN.md §2.9)"
        )

    def score(self, evaluation: Evaluation) -> int:
        """Score a *completed* Evaluation for candidate comparison. Reads the
        already-computed `estimated_net_output`/`gross_output` fields on the
        Evaluation itself (populated by `routing.evaluator.evaluate`, which was
        given this same `ObjectiveContext`) rather than recomputing cost
        arithmetic here -- this keeps exactly one place that turns an objective
        into a cost figure. Only defined for `EvalStatus.OK` plans -- callers
        must filter failed evaluations before scoring (an invalid plan has no
        meaningful output to compare)."""
        if evaluation.status is not EvalStatus.OK:
            raise ValueError(f"cannot score a non-ok evaluation (status={evaluation.status})")
        if evaluation.estimated_net_output is not None:
            return evaluation.estimated_net_output
        return evaluation.gross_output


def gross_only() -> ObjectiveContext:
    return ObjectiveContext(mode="gross_only")


def synthetic_fixed_cost(fixed_cost: int) -> ObjectiveContext:
    return ObjectiveContext(mode="synthetic_fixed_cost", fixed_cost=fixed_cost)

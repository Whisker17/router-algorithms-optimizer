"""Shared `solve()`-side types (docs/DESIGN.md §4.3, §2.10): `SolveContext`,
`Budget`, `SolveResult` and the full solver status vocabulary. Every mandatory
algorithm (docs/DESIGN.md §2.6) implements `solve(case, context, budget) ->
SolveResult` against these same types; only `direct` (WHI-1427) exists yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from benchmark.objective import ObjectiveContext
from routing.evaluator import Evaluation
from routing.plan import RoutePlan
from snapshot.models import SnapshotBundle


class SolveStatus(StrEnum):
    """The full runner status vocabulary (docs/DESIGN.md §2.10). Not every
    algorithm produces every value; `direct` (WHI-1427) only ever returns `OK`
    or `NO_ROUTE`."""

    OK = "ok"
    UNSUPPORTED = "unsupported"
    NO_ROUTE = "no_route"
    TIMEOUT = "timeout"
    INVALID_PLAN = "invalid_plan"
    INCOMPLETE_SNAPSHOT = "incomplete_snapshot"
    MODEL_ERROR = "model_error"
    ALGORITHM_ERROR = "algorithm_error"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class SolveContext:
    """Everything a `solve()` call needs beyond the case itself: the immutable
    bundle it is solving against and the shared objective it scores candidates
    with (docs/DESIGN.md §2.9)."""

    bundle: SnapshotBundle
    objective: ObjectiveContext


@dataclass(frozen=True)
class Budget:
    """Declared solve limits. `max_candidates` is recorded and enforced by
    `direct` (it evaluates every admitted pool, so it is naturally bounded by the
    admitted set); a hard wall-clock timeout and worker-process isolation belong
    to a later measurement-isolation ticket and are not implemented here
    (docs/DESIGN.md §2.10: "Keep the runner initially simple")."""

    max_candidates: int | None = None


@dataclass(frozen=True)
class SolveResult:
    case_id: str
    algorithm: str
    status: SolveStatus
    plan: RoutePlan | None = None
    evaluation: Evaluation | None = None
    score: int | None = None
    candidates_considered: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "algorithm": self.algorithm,
            "status": self.status.value,
            "evaluation": self.evaluation.to_dict() if self.evaluation is not None else None,
            "score": None if self.score is None else str(self.score),
            "candidates_considered": self.candidates_considered,
            "error": self.error,
        }

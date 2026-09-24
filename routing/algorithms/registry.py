"""Explicit algorithm registry (docs/DESIGN.md §4.3: "Do not build dynamic plugin
discovery: an explicit name-to-factory mapping is sufficient."). Only `direct`
(WHI-1427) is registered so far; each further mandatory algorithm (docs/DESIGN.md
§2.6) adds one entry here.
"""

from __future__ import annotations

from typing import Protocol

from routing.algorithms import direct
from routing.algorithms.base import Budget, SolveContext, SolveResult
from snapshot.models import Case


class SolveFn(Protocol):
    def __call__(self, case: Case, context: SolveContext, budget: Budget) -> SolveResult: ...


ALGORITHMS: dict[str, SolveFn] = {
    direct.NAME: direct.solve,
}


def get_algorithm(name: str) -> SolveFn:
    try:
        return ALGORITHMS[name]
    except KeyError as exc:
        raise KeyError(f"unknown algorithm {name!r}; registered: {sorted(ALGORITHMS)}") from exc

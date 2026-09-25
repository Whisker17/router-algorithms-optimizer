"""Explicit algorithm registry (docs/DESIGN.md §4.3: "Do not build dynamic plugin
discovery: an explicit name-to-factory mapping is sufficient."). Only `direct`
(WHI-1427) is registered so far; each further mandatory algorithm (docs/DESIGN.md
§2.6) adds one `AlgorithmFactory` entry here.
"""

from __future__ import annotations

from routing.algorithms import direct
from routing.algorithms.base import AlgorithmFactory, SolveFn

__all__ = ["ALGORITHMS", "SolveFn", "get_algorithm"]

ALGORITHMS: dict[str, AlgorithmFactory] = {
    direct.NAME: direct.FACTORY,
}


def get_algorithm(name: str) -> AlgorithmFactory:
    try:
        return ALGORITHMS[name]
    except KeyError as exc:
        raise KeyError(f"unknown algorithm {name!r}; registered: {sorted(ALGORITHMS)}") from exc

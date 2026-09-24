"""Explicit prepare-source dispatcher (docs/DESIGN.md §4.3-style "no dynamic
plugin discovery"; issue design review finding: "each source collector can
register itself without requiring Agni's CLI work"). Each source ticket adds its own
module and one entry in `COLLECTORS` here, without touching `main.py` or any other
collector: `synthetic` (WHI-1427, offline), `agni` (WHI-1429, fixed-block Agni v3) and
`fusionx` (WHI-1430, fixed-block FusionX v3), both through the shared `concentrated` CL
collector.
"""

from __future__ import annotations

from typing import Protocol

from snapshot.collectors import agni, fusionx, synthetic
from snapshot.collectors.base import PrepareError, PrepareRequest
from snapshot.models import SnapshotBundle

__all__ = ["COLLECTORS", "Collector", "PrepareError", "PrepareRequest", "get_collector"]


class Collector(Protocol):
    def __call__(self, request: PrepareRequest) -> SnapshotBundle: ...


COLLECTORS: dict[str, Collector] = {
    "synthetic": synthetic.collect,
    "agni": agni.collect,
    "fusionx": fusionx.collect,
}


def get_collector(name: str) -> Collector:
    try:
        return COLLECTORS[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown prepare source {name!r}; registered: {sorted(COLLECTORS)}"
        ) from exc

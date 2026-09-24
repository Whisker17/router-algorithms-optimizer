"""Explicit prepare-source dispatcher (docs/DESIGN.md §4.3-style "no dynamic
plugin discovery"; issue design review finding: "each source collector can
register itself without requiring Agni's CLI work"). Only `synthetic` is
registered so far (WHI-1427); each real source ticket adds its own module and one
entry in `COLLECTORS` here, without touching `main.py` or any other collector.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from snapshot.collectors import synthetic
from snapshot.models import SnapshotBundle


class Collector(Protocol):
    def __call__(self, output_dir: Path) -> SnapshotBundle: ...


COLLECTORS: dict[str, Collector] = {
    "synthetic": synthetic.collect,
}


def get_collector(name: str) -> Collector:
    try:
        return COLLECTORS[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown prepare source {name!r}; registered: {sorted(COLLECTORS)}"
        ) from exc

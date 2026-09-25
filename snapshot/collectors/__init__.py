"""Explicit prepare-source dispatcher (docs/DESIGN.md §4.3-style "no dynamic
plugin discovery"; issue design review finding: "each source collector can
register itself without requiring Agni's CLI work"). Each source ticket adds its own
module and one entry in `COLLECTORS` here, without touching `main.py` or any other
collector: `synthetic` (WHI-1427, offline), `agni` (WHI-1429, fixed-block Agni v3),
`fusionx` (WHI-1430, fixed-block FusionX v3) and `uniswap_v3` (WHI-1431, fixed-block
Uniswap v3), all through the shared `concentrated` CL collector, `moe_classic`
(WHI-1432, fixed-block Merchant Moe Classic v1 through the `classic` collector) and
`moe_lb` (WHI-1434, fixed-block Merchant Moe Liquidity Book v2.2 through the
`liquidity_book` collector).
"""

from __future__ import annotations

from typing import Protocol

from snapshot.collectors import agni, classic, fusionx, liquidity_book, synthetic, uniswap_v3
from snapshot.collectors.base import PrepareError, PrepareRequest
from snapshot.models import SnapshotBundle

__all__ = ["COLLECTORS", "Collector", "PrepareError", "PrepareRequest", "get_collector"]


class Collector(Protocol):
    def __call__(self, request: PrepareRequest) -> SnapshotBundle: ...


COLLECTORS: dict[str, Collector] = {
    "synthetic": synthetic.collect,
    "agni": agni.collect,
    "fusionx": fusionx.collect,
    "uniswap_v3": uniswap_v3.collect,
    "moe_classic": classic.collect,
    "moe_lb": liquidity_book.collect,
}


def get_collector(name: str) -> Collector:
    try:
        return COLLECTORS[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown prepare source {name!r}; registered: {sorted(COLLECTORS)}"
        ) from exc

"""Installing the explicit exact quote controls L02-L04 around ONE solve (WHI-1504..1506).

The same code serves the L08 driver (`benchmark.latency.controlled_solve`) and the named
optimized strategy `uni_sor_optimized` (`routing.algorithms.uni_sor_strategies`, WHI-1528:
a heuristic over the `uni_sor_port` core), so both install the controls identically:

- `L02` `skip_empty_spans`, `L03` tick/bin math memos and `L04` the CL prefix memo are
  bound into `pools.concentrated.swap` / `pools.liquidity_book.swap` (the kernels every
  `pools.quote.quote_exact_in` call reaches), only while the solve runs;
- the memo instances are built by the caller for that solve alone (`QuoteControls.fresh`),
  so their construction and population are charged to the timed solve and nothing survives
  it (`per_solve` is the only lifetime);
- the reference kernels are restored in a `finally`, after an error as after a return, and a
  solve never starts from already-replaced kernels. Another process -- the runner's
  independent final evaluation -- never sees a control.

L05 (`incremental_graph` graph reuse) is not a quote control and is not handled here.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from pools import concentrated, liquidity_book
from pools.cl_math import TickMathReuse

# The quote controls and their registered settings keys (config/latency/l08.yaml `controls`).
QUOTE_CONTROL_KEYS: dict[str, frozenset[str]] = {
    "L02": frozenset({"skip_empty_spans"}),
    "L03": frozenset({"tick_capacity", "bin_capacity", "lifetime"}),
    "L04": frozenset({"max_keys", "max_checkpoints", "lifetime"}),
}

# The reference quote kernels, captured once per process: controls always start from them.
REFERENCE_CL_SWAP = concentrated.swap
REFERENCE_LB_SWAP = liquidity_book.swap


@dataclass(frozen=True)
class QuoteControls:
    """The control instances of one solve (None = that control is not installed)."""

    skip_empty_spans: bool
    tick: TickMathReuse | None
    bins: liquidity_book.BinMathReuse | None
    prefix: concentrated.CLPrefixReuse | None

    @classmethod
    def fresh(cls, controls: Mapping[str, Mapping[str, Any]]) -> QuoteControls:
        """New, empty instances for the selected controls' registered settings."""
        l03, l04 = controls.get("L03"), controls.get("L04")
        return cls(
            skip_empty_spans="L02" in controls,
            tick=TickMathReuse(l03["tick_capacity"]) if l03 else None,
            bins=liquidity_book.BinMathReuse(l03["bin_capacity"]) if l03 else None,
            prefix=concentrated.CLPrefixReuse(l04["max_keys"], l04["max_checkpoints"])
            if l04
            else None,
        )

    def stats(self) -> dict[str, Any]:
        return {
            "tick_math": self.tick.stats() if self.tick else None,
            "bin_math": self.bins.stats() if self.bins else None,
            "prefix": self.prefix.stats() if self.prefix else None,
        }


@contextmanager
def installed(controls: QuoteControls) -> Iterator[None]:
    """`controls` bound into the quote kernels for the duration of the block only."""
    if concentrated.swap is not REFERENCE_CL_SWAP or liquidity_book.swap is not REFERENCE_LB_SWAP:
        raise RuntimeError("quote kernels already replaced: controls must start from the reference")
    concentrated.swap = functools.partial(
        REFERENCE_CL_SWAP,
        skip_empty_spans=controls.skip_empty_spans,
        math_reuse=controls.tick,
        prefix_reuse=controls.prefix,
    )
    liquidity_book.swap = functools.partial(REFERENCE_LB_SWAP, math_reuse=controls.bins)
    try:
        yield
    finally:
        concentrated.swap, liquidity_book.swap = REFERENCE_CL_SWAP, REFERENCE_LB_SWAP

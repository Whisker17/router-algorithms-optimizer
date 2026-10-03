"""Pool-family dispatch for the `quote_exact_in(state, token_in, amount_in_raw) ->
SwapResult` seam (docs/DESIGN.md §4.3). Pool-agnostic callers (the evaluator) import
this; family modules stay independent of each other.

Quote metering (WHI-1437, docs/DESIGN.md §2.10: "Return attempted/counted quotes ...
and declared limits"): inside a `metered_quotes(limit)` block every call through this
seam is counted, and a call beyond `limit` raises `QuoteLimitExceeded` instead of
quoting. The benchmark worker opens one meter around each measured `solve()`; nothing
is metered anywhere else (the runner's independent evaluation, collectors, tests), so
outside a meter this is exactly the plain dispatch.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from pools import concentrated, constant_product, liquidity_book
from pools.result import SwapResult
from snapshot.models import (
    ConcentratedPoolState,
    ConstantProductPoolState,
    LiquidityBookPoolState,
    PoolState,
)


class QuoteLimitExceeded(BaseException):  # noqa: N818 -- a hard limit, not an error
    """A metered solve asked for one quote more than its declared `max_quotes`.

    Deliberately a `BaseException`: a solver's broad `except Exception` must not be able
    to swallow a hard budget limit and carry on quoting."""


@dataclass
class QuoteMeter:
    """`attempted` counts every call (including the refused one that exceeded the
    limit); `counted` counts the quotes actually executed and charged to the budget."""

    limit: int | None
    attempted: int = 0
    counted: int = 0
    exceeded: bool = False


_ACTIVE_METER: ContextVar[QuoteMeter | None] = ContextVar("quote_meter", default=None)


@contextmanager
def metered_quotes(limit: int | None) -> Iterator[QuoteMeter]:
    if limit is not None and limit < 0:
        raise ValueError(f"quote limit must be non-negative, got {limit}")
    meter = QuoteMeter(limit=limit)
    token = _ACTIVE_METER.set(meter)
    try:
        yield meter
    finally:
        _ACTIVE_METER.reset(token)


def _charge() -> None:
    meter = _ACTIVE_METER.get()
    if meter is None:
        return
    meter.attempted += 1
    if meter.limit is not None and meter.counted >= meter.limit:
        meter.exceeded = True
        raise QuoteLimitExceeded(f"quote limit {meter.limit} exhausted")
    meter.counted += 1


def quote_exact_in(state: PoolState, token_in: str, amount_in_raw: int) -> SwapResult[PoolState]:
    _charge()
    if isinstance(state, ConstantProductPoolState):
        return constant_product.quote_exact_in(state, token_in, amount_in_raw)
    if isinstance(state, ConcentratedPoolState):
        return concentrated.quote_exact_in(state, token_in, amount_in_raw)
    if isinstance(state, LiquidityBookPoolState):
        return liquidity_book.quote_exact_in(state, token_in, amount_in_raw)
    raise TypeError(f"no quote_exact_in for pool state type {type(state).__name__}")

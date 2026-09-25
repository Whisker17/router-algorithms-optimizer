"""Pool-family dispatch for the `quote_exact_in(state, token_in, amount_in_raw) ->
SwapResult` seam (docs/DESIGN.md §4.3). Pool-agnostic callers (the evaluator) import
this; family modules stay independent of each other."""

from __future__ import annotations

from pools import concentrated, constant_product, liquidity_book
from pools.result import SwapResult
from snapshot.models import (
    ConcentratedPoolState,
    ConstantProductPoolState,
    LiquidityBookPoolState,
    PoolState,
)


def quote_exact_in(state: PoolState, token_in: str, amount_in_raw: int) -> SwapResult[PoolState]:
    if isinstance(state, ConstantProductPoolState):
        return constant_product.quote_exact_in(state, token_in, amount_in_raw)
    if isinstance(state, ConcentratedPoolState):
        return concentrated.quote_exact_in(state, token_in, amount_in_raw)
    if isinstance(state, LiquidityBookPoolState):
        return liquidity_book.quote_exact_in(state, token_in, amount_in_raw)
    raise TypeError(f"no quote_exact_in for pool state type {type(state).__name__}")

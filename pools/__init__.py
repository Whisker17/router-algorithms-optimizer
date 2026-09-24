"""Verified protocol adapters and pure state transitions (docs/DESIGN.md §4.2).

- `constant_product` -- Uniswap-V2-style pools (WHI-1427) and the migrated Merchant Moe
  Classic v1 semantics (`SOURCES`, WHI-1432).
- `concentrated` (+ `cl_math`) -- integer Uniswap-v3-family swap for the admitted
  `uniswap_v3`/`agni_v3`/`fusionx_v3` sources (WHI-1428).
- `quote` -- the pool-agnostic `quote_exact_in(state, token_in, amount_in_raw) ->
  SwapResult` dispatch used by the evaluator (docs/DESIGN.md §4.3); `result` holds the
  shared `SwapResult`/`QuoteStatus`.

Liquidity Book (WHI-1433) adds its own module alongside these.
"""

"""Verified protocol adapters and pure state transitions (docs/DESIGN.md §4.2).

Only `constant_product` (Uniswap-V2/Merchant-Moe-Classic-style pools) is
implemented so far; `pools.constant_product.quote_exact_in` is the concrete
specialization of the `quote_exact_in(state, token_in, amount_in_raw, context) ->
SwapResult` load-bearing interface (docs/DESIGN.md §4.3). Concentrated-liquidity
(WHI-1428) and Liquidity Book (WHI-1433) add their own modules alongside this one.
"""

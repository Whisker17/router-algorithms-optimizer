"""Integer constant-product (Uniswap-V2-style) swap math (docs/DESIGN.md §2.3).

`quote_exact_in` migrates the `getAmountOut` formula into Python integers:

```solidity
// MoeLibrary.getAmountOut (merchant-moe/moe-core@460bf55, src/dex/libraries/
// MoeLibrary.sol, GPL-3.0) -- token-for-token the UniswapV2Library formula
function getAmountOut(uint256 amountIn, uint256 reserveIn, uint256 reserveOut)
    internal pure returns (uint256 amountOut)
{
    require(amountIn > 0, "MoeLibrary: INSUFFICIENT_INPUT_AMOUNT");
    require(reserveIn > 0 && reserveOut > 0, "MoeLibrary: INSUFFICIENT_LIQUIDITY");
    uint256 amountInWithFee = amountIn * 997;
    uint256 numerator = amountInWithFee * reserveOut;
    uint256 denominator = reserveIn * 1000 + amountInWithFee;
    amountOut = numerator / denominator;
}
```

generalized to a configured `fee_bps` (basis points out of 10_000); for `fee_bps=30`
numerator and denominator are both exactly 10x the `997/1000` ones, so the floored
quotient is identical. Solidity's `/` on non-negative uints is EVM `DIV` (floor),
exactly Python's `//` here.

**Real sources (WHI-1432).** A state with a `source_key` is governed by that source's
migrated `ClassicSource` in `SOURCES` -- never by an assumption that "every CPMM is
Uniswap V2". Today the only one is Merchant Moe Classic v1 (`moe_classic_v1`), whose
deployed `MoePair` (clone implementation `0x08477e01...c28B`, forge-rebuilt from
`src/dex/MoePair.sol` @ 460bf55, see `config/protocols.yaml`) maps as follows:

- `MoePair.swap`: `balance{0,1}Adjusted = balance * 1000 - amountIn * 3` and
  `require(adj0 * adj1 >= r0 * r1 * 1000**2, "Moe: K")` -- a *hardcoded* 0.3% input fee:
  no per-pair fee, and no factory fee switch on swaps (`feeTo` only mints LP shares in
  `mint`/`burn` through `_sendFee`). -> `fee_bps == 30` is required
  (`SOURCES[...].fee_bps`). For exact input the largest `amountOut` passing `K` solves
  `(1000 rIn + 997 in)(rOut - out) >= 1000 rIn rOut`, i.e.
  `out <= 997 in rOut / (1000 rIn + 997 in)`: exactly the floored `getAmountOut`.
- `require(amount0Out > 0 || amount1Out > 0, "Moe: INSUFFICIENT_OUTPUT_AMOUNT")` -> an
  output that floors to 0 is `INSUFFICIENT_OUTPUT_AMOUNT`.
- `require(amount0Out < _reserve0 && amount1Out < _reserve1, "Moe: INSUFFICIENT_LIQUIDITY")`
  -> `amount_out >= reserve_out` is `INSUFFICIENT_LIQUIDITY` (unreachable for fee > 0).
- `_update`: `require(balance0 <= type(uint112).max && balance1 <= type(uint112).max,
  "Moe: OVERFLOW")`, then `reserve{0,1} = uint112(balance{0,1})` -> a post-swap
  `reserve_in + amount_in > 2**112 - 1` is `REVERTED`; otherwise the next state is
  `(reserve_in + amount_in, reserve_out - amount_out)`. The pair re-reads balances,
  which equal reserves plus the transfer for the standard (not fee-on-transfer, not
  rebasing) tokens whose balances equal reserves at the block -- the collector
  requires both, and the fork evidence shows the transfer credits exactly `in`.
- `token0()`/`token1()` are the clone's immutable args, sorted by `MoeFactory.createPair`
  -> every sourced state must have `token0 < token1` (`snapshot.bundle`).

Checked arithmetic cannot overflow before `_update` for any admitted input: with
reserves <= 2**112 and `in <= 2**112` the widest product, `997 * in * rOut`, stays below
2**234, and `adj0 * adj1` below 2**246.

This is deliberately narrow: no fee-on-transfer tokens, no flash-swap (`moeCall`)
paths, no `getAmountIn` (Exact Output). `quote_exact_in` never mutates the input
`state`; it returns a fresh `ConstantProductPoolState` for the caller to thread forward
(docs/DESIGN.md §2.3: "It must not mutate the input snapshot.").
"""

from __future__ import annotations

from dataclasses import dataclass

from pools.result import QuoteStatus, SwapResult
from snapshot.models import ConstantProductPoolState

__all__ = [
    "FEE_DENOMINATOR",
    "SOURCES",
    "ClassicSource",
    "QuoteStatus",
    "SwapResult",
    "get_amount_out",
    "quote_exact_in",
]

FEE_DENOMINATOR = 10_000


@dataclass(frozen=True)
class ClassicSource:
    """The migrated, source-specific semantics of one admitted constant-product
    source (see the module docstring's mapping table)."""

    key: str
    fee_bps: int  # the pair's own, fixed swap fee
    reserve_bits: int  # reserve storage width; a larger post-swap balance reverts
    overflow_revert: str  # the pair's revert reason for that overflow
    source_ref: str


SOURCES: dict[str, ClassicSource] = {
    "moe_classic_v1": ClassicSource(
        key="moe_classic_v1",
        fee_bps=30,  # MoePair.swap: balance * 1000 - amountIn * 3
        reserve_bits=112,  # uint112 reserve0/reserve1, MoePair._update
        overflow_revert="Moe: OVERFLOW",
        source_ref="merchant-moe/moe-core@460bf55 src/dex/MoePair.sol",
    ),
}


def get_amount_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int) -> int:
    """The migrated `UniswapV2Library.getAmountOut` formula, generalized to
    `fee_bps` (see module docstring). Callers must ensure `amount_in > 0` and
    `reserve_in > 0 and reserve_out > 0`; this is the pure arithmetic core, kept
    separate from `quote_exact_in`'s status/state handling so it is directly
    unit-testable against independently hand-computed vectors.
    """
    if amount_in <= 0:
        raise ValueError(f"amount_in must be positive, got {amount_in}")
    if reserve_in <= 0 or reserve_out <= 0:
        raise ValueError(f"reserves must be positive, got ({reserve_in}, {reserve_out})")
    if not (0 <= fee_bps < FEE_DENOMINATOR):
        raise ValueError(f"fee_bps must be in [0, {FEE_DENOMINATOR}), got {fee_bps}")
    keep_bps = FEE_DENOMINATOR - fee_bps
    amount_in_with_fee = amount_in * keep_bps
    numerator = amount_in_with_fee * reserve_out
    denominator = reserve_in * FEE_DENOMINATOR + amount_in_with_fee
    return numerator // denominator  # Solidity `/` on uint == floor division here


def quote_exact_in(
    state: ConstantProductPoolState, token_in: str, amount_in_raw: int
) -> SwapResult[ConstantProductPoolState]:
    """`quote_exact_in(pool_state, token_in, amount_in_raw) -> SwapResult`
    (docs/DESIGN.md §4.3 load-bearing interface, constant-product specialization).

    Never mutates `state`. `amount_in_raw` must be a positive integer -- the
    evaluator (docs/DESIGN.md §2.5) is responsible for short-circuiting
    zero-input steps before ever calling a pool, so a non-positive amount here is
    a caller bug, not a normal runtime status, and raises `ValueError`.
    """
    if amount_in_raw <= 0:
        raise ValueError(f"amount_in_raw must be positive, got {amount_in_raw}")

    source: ClassicSource | None = None
    if state.source_key is not None:
        source = SOURCES.get(state.source_key)
        if source is None:
            return _failed(
                state,
                QuoteStatus.UNSUPPORTED,
                f"pool {state.pool_id!r}: no migrated constant-product semantics for source "
                f"{state.source_key!r}",
            )
        if state.fee_bps != source.fee_bps:
            return _failed(
                state,
                QuoteStatus.UNSUPPORTED,
                f"pool {state.pool_id!r}: fee_bps {state.fee_bps} is not {source.key}'s fixed "
                f"{source.fee_bps} ({source.source_ref})",
            )

    if token_in not in (state.token0, state.token1):
        return SwapResult(
            status=QuoteStatus.UNSUPPORTED_TOKEN,
            amount_in_consumed=0,
            amount_out=0,
            new_state=None,
            detail=f"pool {state.pool_id!r} does not hold token {token_in!r}",
        )

    reserve_in, reserve_out = state.reserves_for(token_in)
    if reserve_in <= 0 or reserve_out <= 0:
        return SwapResult(
            status=QuoteStatus.INSUFFICIENT_LIQUIDITY,
            amount_in_consumed=0,
            amount_out=0,
            new_state=None,
            detail=f"pool {state.pool_id!r} has zero reserve on one side "
            f"({reserve_in}, {reserve_out})",
        )

    amount_out = get_amount_out(amount_in_raw, reserve_in, reserve_out, state.fee_bps)
    if amount_out <= 0:
        # On-chain this is `UniswapV2Pair: INSUFFICIENT_OUTPUT_AMOUNT`, not
        # `INSUFFICIENT_LIQUIDITY` -- the pool has real reserves, but this
        # particular `amount_in` is dust that floors to zero output. Keeping a
        # distinct status means a report never conflates "this trade is too
        # small" with "this pool has no liquidity at all".
        return SwapResult(
            status=QuoteStatus.INSUFFICIENT_OUTPUT_AMOUNT,
            amount_in_consumed=0,
            amount_out=0,
            new_state=None,
            detail=(
                f"pool {state.pool_id!r}: amount_in {amount_in_raw} rounds down to 0 output "
                "(dust; on-chain UniswapV2Pair: INSUFFICIENT_OUTPUT_AMOUNT)"
            ),
        )
    if amount_out >= reserve_out:
        # Cannot happen for fee_bps > 0 with the formula above (denominator always
        # exceeds amount_in_with_fee * reserve_out / reserve_out strictly), but is
        # checked explicitly so a future fee_bps=0 edge case never silently drains
        # a pool below its own state-transition domain.
        return SwapResult(
            status=QuoteStatus.INSUFFICIENT_LIQUIDITY,
            amount_in_consumed=0,
            amount_out=0,
            new_state=None,
            detail=f"pool {state.pool_id!r}: quoted output {amount_out} >= reserve {reserve_out}",
        )

    new_reserve_in = reserve_in + amount_in_raw
    if source is not None and new_reserve_in >= 1 << source.reserve_bits:
        return _failed(
            state,
            QuoteStatus.REVERTED,
            f"pool {state.pool_id!r}: reserve {new_reserve_in} after the swap exceeds "
            f"uint{source.reserve_bits} ({source.overflow_revert})",
        )
    new_reserve_out = reserve_out - amount_out
    new_state = state.with_reserves_for(token_in, new_reserve_in, new_reserve_out)
    return SwapResult(
        status=QuoteStatus.OK,
        amount_in_consumed=amount_in_raw,
        amount_out=amount_out,
        new_state=new_state,
    )


def _failed(
    state: ConstantProductPoolState, status: QuoteStatus, detail: str
) -> SwapResult[ConstantProductPoolState]:
    return SwapResult(
        status=status, amount_in_consumed=0, amount_out=0, new_state=None, detail=detail
    )

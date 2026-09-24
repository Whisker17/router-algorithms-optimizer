"""Integer constant-product (Uniswap-V2-style) swap math (docs/DESIGN.md §2.3).

`quote_exact_in` migrates the deployed `UniswapV2Pair`/`UniswapV2Library`
`getAmountOut` formula (also used, byte-for-byte, by Merchant Moe Classic v1 --
see `config/protocols.yaml`'s `moe_classic_v1` source) into Python integers:

```solidity
// UniswapV2Library.getAmountOut (SPDX GPL-3.0, Uniswap/v2-periphery)
function getAmountOut(uint amountIn, uint reserveIn, uint reserveOut)
    internal pure returns (uint amountOut)
{
    require(amountIn > 0, 'UniswapV2Library: INSUFFICIENT_INPUT_AMOUNT');
    require(reserveIn > 0 && reserveOut > 0, 'UniswapV2Library: INSUFFICIENT_LIQUIDITY');
    uint amountInWithFee = amountIn.mul(997);
    uint numerator = amountInWithFee.mul(reserveOut);
    uint denominator = reserveIn.mul(1000).add(amountInWithFee);
    amountOut = numerator / denominator;
}
```

The `997`/`1000` constants are the fixed 0.3% Uniswap V2 fee; this module
generalizes them to a configured `fee_bps` (basis points out of 10_000) so the same
math serves any Classic-family fee, while preserving Solidity's truncating
(floor) integer division (`numerator / denominator` in Solidity is EVM `DIV`,
which floors for non-negative operands -- exactly Python's `//` here since every
operand is non-negative by construction).

This is deliberately narrow: no fee-on-transfer tokens, no flash-swap paths, no
`getAmountIn` (Exact Output). `quote_exact_in` never mutates the input `state`; it
returns a fresh `ConstantProductPoolState` for the caller to thread forward
(docs/DESIGN.md §2.3: "It must not mutate the input snapshot.").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from snapshot.models import ConstantProductPoolState

FEE_DENOMINATOR = 10_000


class QuoteStatus(StrEnum):
    OK = "ok"
    UNSUPPORTED_TOKEN = "unsupported_token"
    INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"


@dataclass(frozen=True)
class SwapResult:
    """Pure output of one `quote_exact_in` call. `new_state` is `None` unless
    `status is QuoteStatus.OK` -- a failed quote never claims a state transition."""

    status: QuoteStatus
    amount_in_consumed: int
    amount_out: int
    new_state: ConstantProductPoolState | None
    detail: str = ""


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
) -> SwapResult:
    """`quote_exact_in(pool_state, token_in, amount_in_raw) -> SwapResult`
    (docs/DESIGN.md §4.3 load-bearing interface, constant-product specialization).

    Never mutates `state`. `amount_in_raw` must be a positive integer -- the
    evaluator (docs/DESIGN.md §2.5) is responsible for short-circuiting
    zero-input steps before ever calling a pool, so a non-positive amount here is
    a caller bug, not a normal runtime status, and raises `ValueError`.
    """
    if amount_in_raw <= 0:
        raise ValueError(f"amount_in_raw must be positive, got {amount_in_raw}")

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
        return SwapResult(
            status=QuoteStatus.INSUFFICIENT_LIQUIDITY,
            amount_in_consumed=0,
            amount_out=0,
            new_state=None,
            detail=f"pool {state.pool_id!r}: amount_in {amount_in_raw} rounds down to 0 output",
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
    new_reserve_out = reserve_out - amount_out
    new_state = state.with_reserves_for(token_in, new_reserve_in, new_reserve_out)
    return SwapResult(
        status=QuoteStatus.OK,
        amount_in_consumed=amount_in_raw,
        amount_out=amount_out,
        new_state=new_state,
    )

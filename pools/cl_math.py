"""Integer migration of the Uniswap-v3-core math libraries used by the swap path
(docs/DESIGN.md §2.3). Every function here is a line-for-line translation of the
deployed Solidity; docs/references/concentrated-liquidity-migration.md holds the full
Solidity-to-Python map, including the per-source verification that Agni and FusionX
ship these libraries unchanged (whitespace/comment/pragma-only differences).

Source: `Uniswap/v3-core` tag `v1.0.0` (commit e3589b1), `contracts/libraries/`
{FullMath, UnsafeMath, TickMath, SqrtPriceMath, SwapMath, LiquidityMath, BitMath,
TickBitmap}.sol, SPDX GPL-2.0-or-later / MIT (FullMath is MIT, the rest GPL-2.0-or-later
after BUSL-1.1's 2023-04-01 change date for the pool; the libraries are GPL/MIT at the
tag). Solidity 0.7.6 semantics apply: `+ - *` on uint/int **wrap silently** unless the
source uses `LowGasSafeMath`/`SafeCast`/an explicit `require`, and every such checked
site raises `SolidityRevert` here. Python integers are unbounded, so every wrapping site
is masked explicitly (`& UINT256_MAX`, ...) and every checked site is tested explicitly.
"""

from __future__ import annotations

import functools
from collections.abc import Callable

UINT128_MAX = (1 << 128) - 1
UINT160_MAX = (1 << 160) - 1
UINT256_MAX = (1 << 256) - 1
INT256_MAX = (1 << 255) - 1
INT256_MIN = -(1 << 255)

Q96 = 1 << 96  # FixedPoint96.Q96
Q128 = 1 << 128  # FixedPoint128.Q128

MIN_TICK = -887272  # TickMath.MIN_TICK
MAX_TICK = 887272  # TickMath.MAX_TICK
MIN_SQRT_RATIO = 4295128739  # TickMath.MIN_SQRT_RATIO
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342  # TickMath.MAX_SQRT_RATIO

FEE_PIPS_DENOMINATOR = 1_000_000  # SwapMath's `1e6`


class SolidityRevert(Exception):  # noqa: N818 -- mirrors the EVM concept
    """The migrated Solidity would revert. `reason` is the on-chain revert string
    where the source has one (e.g. `'SPL'`, `'LS'`, `'T'`), otherwise a short
    `Library.function: condition` description of the failing `require`/checked op."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class MissingState(Exception):  # noqa: N818
    """A read hit snapshot state that was never collected (an unknown tick-bitmap
    word, or an initialized tick without its data). Converted to
    `QuoteStatus.INCOMPLETE_SNAPSHOT` by `pools.concentrated`."""


# ---------------------------------------------------------------------------
# LowGasSafeMath / SafeCast (checked helpers used by the pool and SqrtPriceMath)
# ---------------------------------------------------------------------------


def checked_add_u256(x: int, y: int) -> int:
    """LowGasSafeMath.add(uint256,uint256): `require((z = x + y) >= x)`."""
    z = x + y
    if z > UINT256_MAX:
        raise SolidityRevert("LowGasSafeMath.add: uint256 overflow")
    return z


def checked_mul_u256(x: int, y: int) -> int:
    """LowGasSafeMath.mul(uint256,uint256): `require(x == 0 || (z = x * y) / x == y)`."""
    z = x * y
    if z > UINT256_MAX:
        raise SolidityRevert("LowGasSafeMath.mul: uint256 overflow")
    return z


def checked_sub_i256(x: int, y: int) -> int:
    """LowGasSafeMath.sub(int256,int256): `require((z = x - y) <= x == (y >= 0))`."""
    z = x - y
    if not (INT256_MIN <= z <= INT256_MAX):
        raise SolidityRevert("LowGasSafeMath.sub: int256 overflow")
    return z


def to_int256(y: int) -> int:
    """SafeCast.toInt256(uint256): `require(y < 2**255)`."""
    if y > INT256_MAX:
        raise SolidityRevert("SafeCast.toInt256: overflow")
    return y


def to_uint160(y: int) -> int:
    """SafeCast.toUint160(uint256): `require((z = uint160(y)) == y)`."""
    if y > UINT160_MAX:
        raise SolidityRevert("SafeCast.toUint160: overflow")
    return y


# ---------------------------------------------------------------------------
# FullMath / UnsafeMath
# ---------------------------------------------------------------------------


def mul_div(a: int, b: int, denominator: int) -> int:
    """FullMath.mulDiv: `floor(a*b/denominator)` with full 512-bit precision.
    Reverts (`require(denominator > 0)` / `require(denominator > prod1)`) when the
    denominator is zero or the result does not fit in uint256."""
    if denominator == 0:
        raise SolidityRevert("FullMath.mulDiv: denominator == 0")
    result = (a * b) // denominator
    if result > UINT256_MAX:
        raise SolidityRevert("FullMath.mulDiv: result overflows uint256")
    return result


def mul_div_rounding_up(a: int, b: int, denominator: int) -> int:
    """FullMath.mulDivRoundingUp: `mulDiv` plus one if `mulmod(a,b,d) > 0`, with
    `require(result < type(uint256).max)` before the increment."""
    result = mul_div(a, b, denominator)
    if (a * b) % denominator > 0:
        if result >= UINT256_MAX:
            raise SolidityRevert("FullMath.mulDivRoundingUp: result overflows uint256")
        result += 1
    return result


def div_rounding_up(x: int, y: int) -> int:
    """UnsafeMath.divRoundingUp: assembly `add(div(x, y), gt(mod(x, y), 0))`. EVM
    `DIV`/`MOD` by zero return 0, so `y == 0` yields 0 (no revert)."""
    if y == 0:
        return 0
    return x // y + (1 if x % y > 0 else 0)


# ---------------------------------------------------------------------------
# BitMath
# ---------------------------------------------------------------------------


def most_significant_bit(x: int) -> int:
    """BitMath.mostSignificantBit: `require(x > 0)`; index of the highest set bit."""
    if x <= 0:
        raise SolidityRevert("BitMath.mostSignificantBit: x == 0")
    return x.bit_length() - 1


def least_significant_bit(x: int) -> int:
    """BitMath.leastSignificantBit: `require(x > 0)`; index of the lowest set bit."""
    if x <= 0:
        raise SolidityRevert("BitMath.leastSignificantBit: x == 0")
    return (x & -x).bit_length() - 1


# ---------------------------------------------------------------------------
# TickMath
# ---------------------------------------------------------------------------

_SQRT_RATIO_MULTIPLIERS: tuple[tuple[int, int], ...] = (
    (0x2, 0xFFF97272373D413259A46990580E213A),
    (0x4, 0xFFF2E50F5F656932EF12357CF3C7FDCC),
    (0x8, 0xFFE5CACA7E10E4E61C3624EAA0941CD0),
    (0x10, 0xFFCB9843D60F6159C9DB58835C926644),
    (0x20, 0xFF973B41FA98C081472E6896DFB254C0),
    (0x40, 0xFF2EA16466C96A3843EC78B326B52861),
    (0x80, 0xFE5DEE046A99A2A811C461F1969C3053),
    (0x100, 0xFCBE86C7900A88AEDCFFC83B479AA3A4),
    (0x200, 0xF987A7253AC413176F2B074CF7815E54),
    (0x400, 0xF3392B0822B70005940C7A398E4B70F3),
    (0x800, 0xE7159475A2C29B7443B29C7FA6E889D9),
    (0x1000, 0xD097F3BDFD2022B8845AD8F792AA5825),
    (0x2000, 0xA9F746462D870FDF8A65DC1F90E061E5),
    (0x4000, 0x70D869A156D2A1B890BB3DF62BAF32F7),
    (0x8000, 0x31BE135F97D08FD981231505542FCFA6),
    (0x10000, 0x9AA508B5B7A84E1C677DE54F3E99BC9),
    (0x20000, 0x5D6AF8DEDB81196699C329225EE604),
    (0x40000, 0x2216E584F5FA1EA926041BEDFE98),
    (0x80000, 0x48A170391F7DC42444E8FA2),
)


def get_sqrt_ratio_at_tick(tick: int) -> int:
    """TickMath.getSqrtRatioAtTick(int24) -> uint160 (`require(absTick <= MAX_TICK, 'T')`).
    `ratio * c` never exceeds 2**256 for these constants, so the Solidity
    multiplications are exact and need no masking."""
    abs_tick = -tick if tick < 0 else tick
    if abs_tick > MAX_TICK:
        raise SolidityRevert("T")
    ratio = (
        0xFFFCB933BD6FAD37AA2D162D1A594001
        if abs_tick & 0x1
        else 0x100000000000000000000000000000000
    )
    for bit, multiplier in _SQRT_RATIO_MULTIPLIERS:
        if abs_tick & bit:
            ratio = (ratio * multiplier) >> 128
    if tick > 0:
        ratio = UINT256_MAX // ratio
    # Q128.128 -> Q128.96 rounding up; always fits uint160 for |tick| <= MAX_TICK.
    return (ratio >> 32) + (0 if ratio % (1 << 32) == 0 else 1)


class TickMathReuse:
    """WHI-1505 / L03 experiment, **off unless passed explicitly**: a bounded exact memo
    of `get_sqrt_ratio_at_tick` owned by this instance (no module-global cache).

    The function is pure and its only input is the tick, so a hit returns exactly the
    reference result for that tick in any pool or snapshot. Only plain `int` ticks go
    through the memo; any other input (bool, int subclass, float, unhashable, ...) calls
    the reference function directly, so its type/error behavior is untouched. An
    out-of-range tick raises `SolidityRevert('T')` from the reference function on every
    call: `functools.lru_cache` never stores an exception. Population is lazy (the first
    use of each tick is a miss running the reference math); `capacity` bounds the entry
    count with LRU eviction. `stats()` is the native `cache_info()`."""

    def __init__(self, capacity: int) -> None:
        # A plain positive int: `None` would make `lru_cache` unbounded, 0 disables it.
        if type(capacity) is not int or capacity < 1:
            raise ValueError(f"reuse capacity must be a positive int, got {capacity!r}")
        self.capacity = capacity
        self._memo = functools.lru_cache(maxsize=capacity, typed=True)(get_sqrt_ratio_at_tick)

    def sqrt_ratio_at_tick(self, tick: int) -> int:
        if type(tick) is not int:
            return get_sqrt_ratio_at_tick(tick)
        return self._memo(tick)

    def stats(self) -> dict[str, int]:
        info = self._memo.cache_info()
        return {
            "capacity": self.capacity,
            "entries": info.currsize,
            "hits": info.hits,
            "misses": info.misses,
        }

    def clear(self) -> None:
        self._memo.cache_clear()


def get_tick_at_sqrt_ratio(sqrt_price_x96: int) -> int:
    """TickMath.getTickAtSqrtRatio(uint160) -> int24: greatest tick with
    `getSqrtRatioAtTick(tick) <= sqrtPriceX96`
    (`require(sqrtPriceX96 >= MIN_SQRT_RATIO && sqrtPriceX96 < MAX_SQRT_RATIO, 'R')`).

    Translates the assembly log2 exactly: the msb search, the 14 squaring rounds
    (`r := shr(127, mul(r, r))` cannot overflow since `r < 2**129`), the signed
    `log_2` accumulator (Python's `|`/`>>` on negative ints match two's-complement
    `or`/`sar`), and the tickLow/tickHi disambiguation."""
    if not (MIN_SQRT_RATIO <= sqrt_price_x96 < MAX_SQRT_RATIO):
        raise SolidityRevert("R")
    ratio = sqrt_price_x96 << 32
    msb = ratio.bit_length() - 1  # the eight assembly `gt/shl/or/shr` rounds
    r = ratio >> (msb - 127) if msb >= 128 else ratio << (127 - msb)
    log_2 = (msb - 128) << 64
    for shift in range(63, 49, -1):
        r = (r * r) >> 127
        f = r >> 128
        log_2 |= f << shift
        r >>= f
    log_sqrt10001 = log_2 * 255738958999603826347141  # 128.128 number
    tick_low = (log_sqrt10001 - 3402992956809132418596140100660247210) >> 128
    tick_hi = (log_sqrt10001 + 291339464771989622907027621153398088495) >> 128
    if tick_low == tick_hi:
        return tick_low
    return tick_hi if get_sqrt_ratio_at_tick(tick_hi) <= sqrt_price_x96 else tick_low


# ---------------------------------------------------------------------------
# SqrtPriceMath (Exact Input subset)
# ---------------------------------------------------------------------------


def get_next_sqrt_price_from_amount0_rounding_up_add(
    sqrt_px96: int, liquidity: int, amount: int
) -> int:
    """SqrtPriceMath.getNextSqrtPriceFromAmount0RoundingUp(..., add=true).

    The Solidity chooses between two formulas by detecting uint256 overflow of
    `amount * sqrtPX96` (wrapping `*`, then `/ amount == sqrtPX96`) and of
    `numerator1 + product` (wrapping `+`, then `>= numerator1`). Both detections
    are reproduced with explicit wrapping so the same branch -- and therefore the
    same rounding -- is taken. The `add=false` branch is Exact Output only."""
    if amount == 0:
        return sqrt_px96
    numerator1 = liquidity << 96  # uint256(liquidity) << 96; liquidity is uint128
    product = (amount * sqrt_px96) & UINT256_MAX
    if product // amount == sqrt_px96:
        denominator = (numerator1 + product) & UINT256_MAX
        if denominator >= numerator1:
            return mul_div_rounding_up(numerator1, sqrt_px96, denominator) & UINT160_MAX
    return (
        div_rounding_up(numerator1, checked_add_u256(numerator1 // sqrt_px96, amount)) & UINT160_MAX
    )


def get_next_sqrt_price_from_amount1_rounding_down_add(
    sqrt_px96: int, liquidity: int, amount: int
) -> int:
    """SqrtPriceMath.getNextSqrtPriceFromAmount1RoundingDown(..., add=true):
    `uint256(sqrtPX96).add(quotient).toUint160()`. `liquidity > 0` is guaranteed by
    the caller (`getNextSqrtPriceFromInput`)."""
    if amount <= UINT160_MAX:
        quotient = (amount << 96) // liquidity
    else:
        quotient = mul_div(amount, Q96, liquidity)
    return to_uint160(checked_add_u256(sqrt_px96, quotient))


def get_next_sqrt_price_from_input(
    sqrt_px96: int, liquidity: int, amount_in: int, zero_for_one: bool
) -> int:
    """SqrtPriceMath.getNextSqrtPriceFromInput (`require(sqrtPX96 > 0)`,
    `require(liquidity > 0)`)."""
    if sqrt_px96 <= 0:
        raise SolidityRevert("SqrtPriceMath.getNextSqrtPriceFromInput: sqrtPX96 == 0")
    if liquidity <= 0:
        raise SolidityRevert("SqrtPriceMath.getNextSqrtPriceFromInput: liquidity == 0")
    if zero_for_one:
        return get_next_sqrt_price_from_amount0_rounding_up_add(sqrt_px96, liquidity, amount_in)
    return get_next_sqrt_price_from_amount1_rounding_down_add(sqrt_px96, liquidity, amount_in)


def get_amount0_delta(sqrt_ratio_a: int, sqrt_ratio_b: int, liquidity: int, round_up: bool) -> int:
    """SqrtPriceMath.getAmount0Delta(uint160,uint160,uint128,bool) -> uint256
    (`require(sqrtRatioAX96 > 0)` after sorting)."""
    if sqrt_ratio_a > sqrt_ratio_b:
        sqrt_ratio_a, sqrt_ratio_b = sqrt_ratio_b, sqrt_ratio_a
    numerator1 = liquidity << 96
    numerator2 = sqrt_ratio_b - sqrt_ratio_a
    if sqrt_ratio_a <= 0:
        raise SolidityRevert("SqrtPriceMath.getAmount0Delta: sqrtRatioAX96 == 0")
    if round_up:
        return div_rounding_up(
            mul_div_rounding_up(numerator1, numerator2, sqrt_ratio_b), sqrt_ratio_a
        )
    return mul_div(numerator1, numerator2, sqrt_ratio_b) // sqrt_ratio_a


def get_amount1_delta(sqrt_ratio_a: int, sqrt_ratio_b: int, liquidity: int, round_up: bool) -> int:
    """SqrtPriceMath.getAmount1Delta(uint160,uint160,uint128,bool) -> uint256."""
    if sqrt_ratio_a > sqrt_ratio_b:
        sqrt_ratio_a, sqrt_ratio_b = sqrt_ratio_b, sqrt_ratio_a
    if round_up:
        return mul_div_rounding_up(liquidity, sqrt_ratio_b - sqrt_ratio_a, Q96)
    return mul_div(liquidity, sqrt_ratio_b - sqrt_ratio_a, Q96)


# ---------------------------------------------------------------------------
# SwapMath (Exact Input branch)
# ---------------------------------------------------------------------------


def compute_swap_step_exact_in(
    sqrt_ratio_current_x96: int,
    sqrt_ratio_target_x96: int,
    liquidity: int,
    amount_remaining: int,
    fee_pips: int,
) -> tuple[int, int, int, int]:
    """SwapMath.computeSwapStep for `amountRemaining >= 0` (Exact Input). Returns
    `(sqrtRatioNextX96, amountIn, amountOut, feeAmount)`. The `exactIn == false`
    branches are Exact Output only and are not migrated (callers never pass a
    negative remainder)."""
    if amount_remaining < 0:
        raise ValueError("compute_swap_step_exact_in: Exact Output branch is not migrated")
    zero_for_one = sqrt_ratio_current_x96 >= sqrt_ratio_target_x96

    amount_remaining_less_fee = mul_div(
        amount_remaining, FEE_PIPS_DENOMINATOR - fee_pips, FEE_PIPS_DENOMINATOR
    )
    if zero_for_one:
        amount_in = get_amount0_delta(
            sqrt_ratio_target_x96, sqrt_ratio_current_x96, liquidity, True
        )
    else:
        amount_in = get_amount1_delta(
            sqrt_ratio_current_x96, sqrt_ratio_target_x96, liquidity, True
        )
    if amount_remaining_less_fee >= amount_in:
        sqrt_ratio_next_x96 = sqrt_ratio_target_x96
    else:
        sqrt_ratio_next_x96 = get_next_sqrt_price_from_input(
            sqrt_ratio_current_x96, liquidity, amount_remaining_less_fee, zero_for_one
        )

    reached_target = sqrt_ratio_target_x96 == sqrt_ratio_next_x96  # Solidity `max`
    if zero_for_one:
        if not reached_target:
            amount_in = get_amount0_delta(
                sqrt_ratio_next_x96, sqrt_ratio_current_x96, liquidity, True
            )
        amount_out = get_amount1_delta(
            sqrt_ratio_next_x96, sqrt_ratio_current_x96, liquidity, False
        )
    else:
        if not reached_target:
            amount_in = get_amount1_delta(
                sqrt_ratio_current_x96, sqrt_ratio_next_x96, liquidity, True
            )
        amount_out = get_amount0_delta(
            sqrt_ratio_current_x96, sqrt_ratio_next_x96, liquidity, False
        )

    if not reached_target:
        # take the remainder of the maximum input as fee (unchecked `-` in 0.7.6)
        fee_amount = (amount_remaining - amount_in) & UINT256_MAX
    else:
        fee_amount = mul_div_rounding_up(amount_in, fee_pips, FEE_PIPS_DENOMINATOR - fee_pips)
    return sqrt_ratio_next_x96, amount_in, amount_out, fee_amount


# ---------------------------------------------------------------------------
# LiquidityMath
# ---------------------------------------------------------------------------


def add_delta(x: int, y: int) -> int:
    """LiquidityMath.addDelta(uint128 x, int128 y): reverts `'LS'` on underflow and
    `'LA'` on overflow (the uint128 arithmetic wraps before the `require`)."""
    if y < 0:
        z = (x - (-y)) & UINT128_MAX
        if not z < x:
            raise SolidityRevert("LS")
    else:
        z = (x + y) & UINT128_MAX
        if not z >= x:
            raise SolidityRevert("LA")
    return z


# ---------------------------------------------------------------------------
# TickBitmap
# ---------------------------------------------------------------------------


def next_initialized_tick_within_one_word(
    read_word: Callable[[int], int], tick: int, tick_spacing: int, lte: bool
) -> tuple[int, bool]:
    """TickBitmap.nextInitializedTickWithinOneWord. `read_word(wordPos)` returns the
    `tickBitmap[int16]` word (it raises `MissingState` for an uncollected word).

    Solidity's `compressed = tick / tickSpacing; if (tick < 0 && tick % tickSpacing
    != 0) compressed--;` is exactly floor division, i.e. Python's `//`.
    `position(t)` is `(int16(t >> 8), uint8(t % 256))`; for negative `t` the uint8
    conversion of a negative remainder wraps, which is `t & 0xff` (and `t >> 8` is an
    arithmetic shift in both languages)."""
    compressed = tick // tick_spacing
    if lte:
        word_pos, bit_pos = compressed >> 8, compressed & 0xFF
        mask = (1 << bit_pos) - 1 + (1 << bit_pos)
        masked = read_word(word_pos) & mask
        initialized = masked != 0
        if initialized:
            nxt = (compressed - (bit_pos - most_significant_bit(masked))) * tick_spacing
        else:
            nxt = (compressed - bit_pos) * tick_spacing
    else:
        c1 = compressed + 1
        word_pos, bit_pos = c1 >> 8, c1 & 0xFF
        mask = ~((1 << bit_pos) - 1) & UINT256_MAX
        masked = read_word(word_pos) & mask
        initialized = masked != 0
        if initialized:
            nxt = (c1 + (least_significant_bit(masked) - bit_pos)) * tick_spacing
        else:
            nxt = (c1 + (0xFF - bit_pos)) * tick_spacing
    return nxt, initialized

"""Integer Liquidity Book v2.2 swap simulation for Merchant Moe LB (docs/DESIGN.md §§2.2-2.3;
WHI-1433).

`swap` is a line-for-line migration of the deployed `LBPair.swap(bool swapForY, address to)`
(implementation `0xf6863Db7...DDB3B`), whose runtime bytecode is reproduced by a local build
of `lfj-gg/joe-v2` tag `v2.2.0` (`1297c3822f0605e643155c35948959c0a0d05e17`, MIT, solc
0.8.20 runs=300; `docs/references/protocol-admission.md` §3.5), with the libraries it calls:
`BinHelper.getAmounts`/`getLiquidity`, `FeeHelper`, `PairParameterHelper` (packed
`_parameters` word, `Encoded`), `PriceHelper`, `Uint128x128Math.pow`,
`Uint256x256Math.{mul,shift}{Shift,Div}Round{Down,Up}`, `PackedUint128Math`, `SafeCast` and
the `TreeMath` bin-tree search behind `getNextNonEmptyBin`. The full Solidity-to-Python map,
with every storage field and each justified exclusion (oracle, flash loans, mint/burn,
composition fees, token transfers), is `docs/references/liquidity-book-migration.md`.

Semantics preserved exactly: uint128/uint256 widths and every checked-arithmetic revert,
ceil/floor rounding direction of each fee and price conversion, the per-bin
volatility-accumulator update (`updateVolatilityAccumulator` runs for every non-empty bin,
so the variable fee rises as the swap crosses bins), the time-dependent
`updateReferences(block.timestamp)` filter/decay branches (the timestamp is the state's
frozen `block_timestamp`), the protocol-fee share taken out of each bin's input, and the
`LBPair__OutOfLiquidity` / `LBPair__InsufficientAmountOut` reverts.

Swap hooks: the deployed pairs carry an `LBHooksRewarder` clone with the `beforeSwap` flag.
The pair calls it after taking its reentrancy lock and before any swap math, passing only
the input amount; every state-changing `LBPair` entry point is `nonReentrant` or
access-controlled, so the hook cannot write pair storage, and the rewarder source only
updates its own / MasterChef accounting (and forwards to an optional `LBHooksExtraRewarder`
with the same accounting-only `_beforeSwap`). Both are modeled as explicit no-ops and counted
(`SwapOutcome.hook_calls`) -- only for implementations admitted in `SOURCES`; any other
hook with a swap flag, or a rewarder forwarding to an unadmitted swap-flagged extra hook
(WHI-1434: the collector reads `getExtraHooksParameters()`), is `UNSUPPORTED`. The fork
evidence executes the live hooks.

`swap` never mutates its input; it returns a fresh `LiquidityBookPoolState`.
"""

from __future__ import annotations

import functools
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from pools.result import QuoteStatus, SwapResult
from snapshot.models import LBStaticFeeParameters, LBVariableFeeParameters, LiquidityBookPoolState

# --- Constants.sol / PriceHelper.sol ----------------------------------------------------
SCALE_OFFSET = 128
SCALE = 1 << SCALE_OFFSET
PRECISION = 10**18
BASIS_POINT_MAX = 10_000
MAX_FEE = 10**17  # 10%
MAX_LIQUIDITY_PER_BIN = 65251743116719673010965625540244653191619923014385985379600384103134737
REAL_ID_SHIFT = 1 << 23

UINT24_MAX = (1 << 24) - 1
UINT128_MAX = (1 << 128) - 1
UINT256_MAX = (1 << 256) - 1

# --- Encoded.sol masks ------------------------------------------------------------------
MASK_UINT12 = 0xFFF
MASK_UINT14 = 0x3FFF
MASK_UINT16 = 0xFFFF
MASK_UINT20 = 0xFFFFF
MASK_UINT24 = 0xFFFFFF
MASK_UINT40 = 0xFFFFFFFFFF

# --- PairParameterHelper.sol offsets ----------------------------------------------------
OFFSET_BASE_FACTOR = 0
OFFSET_FILTER_PERIOD = 16
OFFSET_DECAY_PERIOD = 28
OFFSET_REDUCTION_FACTOR = 40
OFFSET_VAR_FEE_CONTROL = 54
OFFSET_PROTOCOL_SHARE = 78
OFFSET_MAX_VOL_ACC = 92
OFFSET_VOL_ACC = 112
OFFSET_VOL_REF = 132
OFFSET_ID_REF = 152
OFFSET_TIME_LAST_UPDATE = 176
OFFSET_ACTIVE_ID = 232

# --- Hooks.sol flags --------------------------------------------------------------------
BEFORE_SWAP_FLAG = 1 << 160
AFTER_SWAP_FLAG = 1 << 161
ADDRESS_MASK = (1 << 160) - 1

# Revert reasons (the Solidity custom-error / panic names).
OUT_OF_LIQUIDITY = "LBPair__OutOfLiquidity"
INSUFFICIENT_AMOUNT_OUT = "LBPair__InsufficientAmountOut"
INSUFFICIENT_AMOUNT_IN = "LBPair__InsufficientAmountIn"
PANIC_ARITHMETIC = "Panic(0x11)"


class LBRevert(Exception):  # noqa: N818
    """The migrated Solidity reverts (custom error, SafeCast or checked-math panic)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MissingState(Exception):  # noqa: N818
    """The swap needs state the snapshot does not contain (bins outside the collected
    range, uncollected fee parameters)."""


class UnsupportedState(Exception):  # noqa: N818
    """The state is outside the admitted semantics (unknown source, unadmitted hook)."""


@dataclass(frozen=True)
class LBSource:
    """Admitted source-specific facts. `key` matches `config/protocols.yaml`."""

    key: str
    pair_implementation: str
    upstream: str
    # Implementations behind a hooks clone whose swap callbacks are verified not to touch
    # pair state or amounts (see module docstring). Lowercase addresses.
    amount_neutral_swap_hooks: frozenset[str]
    # Implementations an admitted rewarder may forward `beforeSwap` to (its
    # `getExtraHooksParameters()` hook), verified the same way. Lowercase addresses.
    amount_neutral_extra_swap_hooks: frozenset[str] = frozenset()


SOURCES: Mapping[str, LBSource] = MappingProxyType(
    {
        "moe_lb_v2_2": LBSource(
            key="moe_lb_v2_2",
            pair_implementation="0xf6863db7323aac43fe8aef0b3ef63aa6b32ddb3b",
            upstream="lfj-gg/joe-v2 v2.2.0 (1297c38) src/LBPair.sol",
            amount_neutral_swap_hooks=frozenset(
                # LBHooksRewarder (Routescan-verified, solc 0.8.20 runs 600): beforeSwap ->
                # _updateAccruedRewardsPerShare (MasterChef.deposit(pid, 0) + own storage).
                {"0xdc0e38cbd08fa532847baecbf26c8b09ed9008a7"}
            ),
            amount_neutral_extra_swap_hooks=frozenset(
                # LBHooksExtraRewarder (Routescan-verified, solc 0.8.20 runs 600): inherits
                # LBHooksBaseRewarder._beforeSwap -> _updateAccruedRewardsPerShare (own
                # storage + pair views only); it forwards to no further hook.
                {"0x2d4bf9f668e5b7c7fe33c8f116ae190669304676"}
            ),
        ),
    }
)


# =========================================================================================
# SafeCast / checked arithmetic helpers
# =========================================================================================


def _safe(value: int, bits: int) -> int:
    if value >> bits:
        raise LBRevert(f"SafeCast__Exceeds{bits}Bits")
    return value


def _checked_add(a: int, b: int, maximum: int) -> int:
    z = a + b
    if z > maximum:
        raise LBRevert(PANIC_ARITHMETIC)
    return z


def _checked_sub(a: int, b: int) -> int:
    if b > a:
        raise LBRevert(PANIC_ARITHMETIC)
    return a - b


# =========================================================================================
# Uint256x256Math.sol
# =========================================================================================


def mul_shift_round_down(x: int, y: int, offset: int) -> int:
    """`mulShiftRoundDown`: floor(x * y / 2**offset); reverts when the 512-bit high word is
    `>= 1 << offset` (i.e. the result does not fit 256 bits)."""
    prod = x * y
    if (prod >> 256) >= (1 << offset):
        raise LBRevert("Uint256x256Math__MulShiftOverflow")
    return prod >> offset


def mul_shift_round_up(x: int, y: int, offset: int) -> int:
    result = mul_shift_round_down(x, y, offset)
    if (x * y) % (1 << offset) != 0:
        result = _checked_add(result, 1, UINT256_MAX)
    return result


def shift_div_round_down(x: int, offset: int, denominator: int) -> int:
    """`shiftDivRoundDown`: floor(x * 2**offset / denominator). `_getEndOfDivRoundDown`
    reverts when the 512-bit high word is `>= denominator` (result >= 2**256)."""
    prod = x << offset
    if (prod >> 256) == 0:
        if denominator == 0:
            raise LBRevert("Panic(0x12)")
        return prod // denominator
    if (prod >> 256) >= denominator:
        raise LBRevert("Uint256x256Math__MulDivOverflow")
    return prod // denominator


def shift_div_round_up(x: int, offset: int, denominator: int) -> int:
    result = shift_div_round_down(x, offset, denominator)
    if (x << offset) % denominator != 0:  # mulmod(x, 1 << offset, denominator)
        result = _checked_add(result, 1, UINT256_MAX)
    return result


# =========================================================================================
# Uint128x128Math.pow / PriceHelper.sol
# =========================================================================================


def pow128(x: int, y: int) -> int:
    """`Uint128x128Math.pow(x, y)` for a 128.128 fixed-point `x` and signed exponent `y`,
    including the assembly's wrapping 256-bit multiplications and 20-bit exponent limit."""
    if y == 0:
        return SCALE
    invert = y < 0
    abs_y = -y if invert else y
    result = 0
    if abs_y < 0x100000:
        result = SCALE
        squared = x
        if x > UINT128_MAX:
            squared = UINT256_MAX // squared
            invert = not invert
        for bit in range(20):
            if abs_y & (1 << bit):
                result = ((result * squared) & UINT256_MAX) >> 128
            squared = ((squared * squared) & UINT256_MAX) >> 128
    if result == 0:
        raise LBRevert("Uint128x128Math__PowUnderflow")
    return UINT256_MAX // result if invert else result


def get_base(bin_step: int) -> int:
    """`PriceHelper.getBase`: 1 + binStep / 10_000 as 128.128."""
    return SCALE + (bin_step << SCALE_OFFSET) // BASIS_POINT_MAX


def get_price_from_id(bin_id: int, bin_step: int) -> int:
    """`PriceHelper.getPriceFromId`: base ** (id - 2**23), 128.128 price of Y per X."""
    return pow128(get_base(bin_step), bin_id - REAL_ID_SHIFT)


class BinMathReuse:
    """WHI-1505 / L03 experiment, **off unless passed explicitly**: a bounded exact memo
    of `get_price_from_id` owned by this instance (no module-global cache).

    The function is pure in its complete input `(bin_id, bin_step)`, which is the memo
    key, so a hit returns exactly the reference price for that pair in any pool or
    snapshot. Only plain `int` arguments go through the memo; anything else calls the
    reference function directly. A reverting input (`Uint128x128Math__PowUnderflow`) is
    recomputed and re-raised on every call: `functools.lru_cache` never stores an
    exception. Lazy population, LRU eviction at `capacity` entries."""

    def __init__(self, capacity: int) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError(f"reuse capacity must be a positive int, got {capacity!r}")
        self.capacity = capacity
        self._memo = functools.lru_cache(maxsize=capacity, typed=True)(get_price_from_id)

    def price_from_id(self, bin_id: int, bin_step: int) -> int:
        if type(bin_id) is not int or type(bin_step) is not int:
            return get_price_from_id(bin_id, bin_step)
        return self._memo(bin_id, bin_step)

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


# =========================================================================================
# Encoded.sol / PairParameterHelper.sol (the packed `_parameters` word)
# =========================================================================================


def _set(encoded: int, value: int, mask: int, offset: int) -> int:
    return (encoded & ~(mask << offset) & UINT256_MAX) | ((value & mask) << offset)


def _get(encoded: int, mask: int, offset: int) -> int:
    return (encoded >> offset) & mask


def encode_parameters(
    static: LBStaticFeeParameters, variable: LBVariableFeeParameters, active_id: int
) -> int:
    """Rebuild `_parameters` from its getters (oracle id left 0: not modeled)."""
    params = 0
    params = _set(params, static.base_factor, MASK_UINT16, OFFSET_BASE_FACTOR)
    params = _set(params, static.filter_period, MASK_UINT12, OFFSET_FILTER_PERIOD)
    params = _set(params, static.decay_period, MASK_UINT12, OFFSET_DECAY_PERIOD)
    params = _set(params, static.reduction_factor, MASK_UINT14, OFFSET_REDUCTION_FACTOR)
    params = _set(params, static.variable_fee_control, MASK_UINT24, OFFSET_VAR_FEE_CONTROL)
    params = _set(params, static.protocol_share, MASK_UINT14, OFFSET_PROTOCOL_SHARE)
    params = _set(params, static.max_volatility_accumulator, MASK_UINT20, OFFSET_MAX_VOL_ACC)
    params = _set(params, variable.volatility_accumulator, MASK_UINT20, OFFSET_VOL_ACC)
    params = _set(params, variable.volatility_reference, MASK_UINT20, OFFSET_VOL_REF)
    params = _set(params, variable.id_reference, MASK_UINT24, OFFSET_ID_REF)
    params = _set(params, variable.time_of_last_update, MASK_UINT40, OFFSET_TIME_LAST_UPDATE)
    return _set(params, active_id, MASK_UINT24, OFFSET_ACTIVE_ID)


def decode_variable(params: int) -> LBVariableFeeParameters:
    return LBVariableFeeParameters(
        volatility_accumulator=_get(params, MASK_UINT20, OFFSET_VOL_ACC),
        volatility_reference=_get(params, MASK_UINT20, OFFSET_VOL_REF),
        id_reference=_get(params, MASK_UINT24, OFFSET_ID_REF),
        time_of_last_update=_get(params, MASK_UINT40, OFFSET_TIME_LAST_UPDATE),
    )


def get_base_fee(params: int, bin_step: int) -> int:
    """`getBaseFee`: baseFactor * binStep * 1e10 (1e18 = 100%)."""
    return _get(params, MASK_UINT16, OFFSET_BASE_FACTOR) * bin_step * 10**10


def get_variable_fee(params: int, bin_step: int) -> int:
    """`getVariableFee`: ceil((volAcc * binStep)**2 * variableFeeControl / 100)."""
    variable_fee_control = _get(params, MASK_UINT24, OFFSET_VAR_FEE_CONTROL)
    if variable_fee_control == 0:
        return 0
    prod = _get(params, MASK_UINT20, OFFSET_VOL_ACC) * bin_step
    return (prod * prod * variable_fee_control + 99) // 100


def get_total_fee(params: int, bin_step: int) -> int:
    return _safe(get_base_fee(params, bin_step) + get_variable_fee(params, bin_step), 128)


def update_references(params: int, timestamp: int) -> int:
    """`updateReferences`: at least `filterPeriod` after the last update the id reference
    moves to the active id and the volatility reference decays (`volAcc * reductionFactor
    / 10_000`) -- or resets to 0 at or beyond `decayPeriod`. Always stamps the time."""
    dt = _checked_sub(timestamp, _get(params, MASK_UINT40, OFFSET_TIME_LAST_UPDATE))
    if dt >= _get(params, MASK_UINT12, OFFSET_FILTER_PERIOD):
        active_id = _get(params, MASK_UINT24, OFFSET_ACTIVE_ID)
        params = _set(params, active_id, MASK_UINT24, OFFSET_ID_REF)
        if dt < _get(params, MASK_UINT12, OFFSET_DECAY_PERIOD):
            vol_acc = _get(params, MASK_UINT20, OFFSET_VOL_ACC)
            reduction = _get(params, MASK_UINT14, OFFSET_REDUCTION_FACTOR)
            vol_ref = (vol_acc * reduction // BASIS_POINT_MAX) & MASK_UINT24  # uint24(...)
        else:
            vol_ref = 0
        if vol_ref > MASK_UINT20:  # setVolatilityReference
            raise LBRevert("PairParametersHelper__InvalidParameter")
        params = _set(params, vol_ref, MASK_UINT20, OFFSET_VOL_REF)
    return _set(params, _safe(timestamp, 40), MASK_UINT40, OFFSET_TIME_LAST_UPDATE)


def update_volatility_accumulator(params: int, active_id: int) -> int:
    """`updateVolatilityAccumulator`: volRef + |activeId - idRef| * 10_000, capped at
    `maxVolatilityAccumulator`."""
    id_ref = _get(params, MASK_UINT24, OFFSET_ID_REF)
    delta_id = abs(active_id - id_ref)
    vol_acc = _get(params, MASK_UINT20, OFFSET_VOL_REF) + delta_id * BASIS_POINT_MAX
    vol_acc = min(vol_acc, _get(params, MASK_UINT20, OFFSET_MAX_VOL_ACC))
    if vol_acc > MASK_UINT20:  # setVolatilityAccumulator (unreachable: cap is a uint20)
        raise LBRevert("PairParametersHelper__InvalidParameter")
    return _set(params, vol_acc, MASK_UINT20, OFFSET_VOL_ACC)


# =========================================================================================
# FeeHelper.sol
# =========================================================================================


def _verify_fee(total_fee: int) -> None:
    if total_fee > MAX_FEE:
        raise LBRevert("FeeHelper__FeeTooLarge")


def get_fee_amount_from(amount_with_fees: int, total_fee: int) -> int:
    """`getFeeAmountFrom`: ceil(amountWithFees * fee / 1e18) -- fee *included* in amount."""
    _verify_fee(total_fee)
    return ((amount_with_fees * total_fee + PRECISION - 1) // PRECISION) & UINT128_MAX


def get_fee_amount(amount: int, total_fee: int) -> int:
    """`getFeeAmount`: ceil(amount * fee / (1e18 - fee)) -- fee *on top of* amount."""
    _verify_fee(total_fee)
    denominator = PRECISION - total_fee
    return ((amount * total_fee + denominator - 1) // denominator) & UINT128_MAX


# =========================================================================================
# PackedUint128Math.sol (bytes32 = x | y << 128)
# =========================================================================================


def _encode(x: int, y: int) -> int:
    return (x & UINT128_MAX) | (y << 128)


def _decode(z: int) -> tuple[int, int]:
    return z & UINT128_MAX, z >> 128


def _packed_add(x: int, y: int) -> int:
    z = (x + y) & UINT256_MAX
    if z < x or (z & UINT128_MAX) < (x & UINT128_MAX):
        raise LBRevert("PackedUint128Math__AddOverflow")
    return z


def _packed_sub(x: int, y: int) -> int:
    z = (x - y) & UINT256_MAX
    if z > x or (z & UINT128_MAX) > (x & UINT128_MAX):
        raise LBRevert("PackedUint128Math__SubUnderflow")
    return z


def _scalar_mul_div_basis_point_round_down(x: int, multiplier: int) -> int:
    if multiplier == 0:
        return 0
    if multiplier > BASIS_POINT_MAX:
        raise LBRevert("PackedUint128Math__MultiplierTooLarge")
    x1, x2 = _decode(x)
    return _encode(x1 * multiplier // BASIS_POINT_MAX, x2 * multiplier // BASIS_POINT_MAX)


# =========================================================================================
# BinHelper.sol
# =========================================================================================


def get_liquidity(x: int, y: int, price: int) -> int:
    """`getLiquidity`: price * x + (y << 128), with the explicit overflow reverts."""
    liquidity = 0
    if x > 0:
        liquidity = (price * x) & UINT256_MAX
        if liquidity // x != price:
            raise LBRevert("BinHelper__LiquidityOverflow")
    if y > 0:
        y = (y << SCALE_OFFSET) & UINT256_MAX
        liquidity = (liquidity + y) & UINT256_MAX
        if liquidity < y:
            raise LBRevert("BinHelper__LiquidityOverflow")
    return liquidity


def get_amounts(
    bin_reserves: int,
    params: int,
    bin_step: int,
    swap_for_y: bool,
    active_id: int,
    amounts_in_left: int,
    *,
    math_reuse: BinMathReuse | None = None,
) -> tuple[int, int, int]:
    """`BinHelper.getAmounts` -> packed `(amountsInWithFees, amountsOutOfBin, totalFees)`.
    `math_reuse` (WHI-1505, experimental, default `None` = reference call) supplies the
    bin price from a bounded exact memo."""
    if math_reuse is None:
        price = get_price_from_id(active_id, bin_step)
    else:
        price = math_reuse.price_from_id(active_id, bin_step)
    reserve_x, reserve_y = _decode(bin_reserves)
    bin_reserve_out = reserve_y if swap_for_y else reserve_x

    if swap_for_y:
        max_amount_in = _safe(shift_div_round_up(bin_reserve_out, SCALE_OFFSET, price), 128)
    else:
        max_amount_in = _safe(mul_shift_round_up(bin_reserve_out, price, SCALE_OFFSET), 128)
    total_fee = get_total_fee(params, bin_step)
    max_fee = get_fee_amount(max_amount_in, total_fee)
    max_amount_in = _checked_add(max_amount_in, max_fee, UINT128_MAX)

    in_x, in_y = _decode(amounts_in_left)
    amount_in128 = in_x if swap_for_y else in_y
    if amount_in128 >= max_amount_in:
        fee128 = max_fee
        amount_in128 = max_amount_in
        amount_out128 = bin_reserve_out
    else:
        fee128 = get_fee_amount_from(amount_in128, total_fee)
        amount_in = _checked_sub(amount_in128, fee128)
        if swap_for_y:
            amount_out128 = _safe(mul_shift_round_down(amount_in, price, SCALE_OFFSET), 128)
        else:
            amount_out128 = _safe(shift_div_round_down(amount_in, SCALE_OFFSET, price), 128)
        amount_out128 = min(amount_out128, bin_reserve_out)

    if swap_for_y:
        amounts_in_with_fees = _encode(amount_in128, 0)
        amounts_out_of_bin = _encode(0, amount_out128)
        total_fees = _encode(fee128, 0)
    else:
        amounts_in_with_fees = _encode(0, amount_in128)
        amounts_out_of_bin = _encode(amount_out128, 0)
        total_fees = _encode(0, fee128)

    after = _packed_sub(_packed_add(bin_reserves, amounts_in_with_fees), amounts_out_of_bin)
    if get_liquidity(*_decode(after), price) > MAX_LIQUIDITY_PER_BIN:
        raise LBRevert("BinHelper__MaxLiquidityPerBinExceeded")
    return amounts_in_with_fees, amounts_out_of_bin, total_fees


# =========================================================================================
# Bin tree (TreeMath.findFirstRight / findFirstLeft via LBPair._getNextNonEmptyBin)
# =========================================================================================


def next_non_empty_bin(
    tree: tuple[int, ...], bin_range: tuple[int, int], swap_for_y: bool, bin_id: int, pool_id: str
) -> int:
    """`_getNextNonEmptyBin(swapForY, id)`: `findFirstRight` (swapForY) returns the largest
    tree member `< id`, or `type(uint24).max` if none; `findFirstLeft` the smallest member
    `> id`, or `0`. `tree` is the sorted tree membership inside the collected `bin_range`;
    a search that leaves the range before finding a member needs uncollected state unless
    the range reaches the end of the id space."""
    lo, hi = bin_range
    if swap_for_y:
        idx = bisect_left(tree, bin_id)
        if idx > 0:
            return tree[idx - 1]
        if lo == 0:
            return UINT24_MAX
        raise MissingState(
            f"pool {pool_id!r}: no collected bin below {bin_id}; the bin tree was only walked "
            f"down to {lo}"
        )
    idx = bisect_right(tree, bin_id)
    if idx < len(tree):
        return tree[idx]
    if hi == UINT24_MAX:
        return 0
    raise MissingState(
        f"pool {pool_id!r}: no collected bin above {bin_id}; the bin tree was only walked "
        f"up to {hi}"
    )


# =========================================================================================
# LBPair.swap
# =========================================================================================


@dataclass(frozen=True)
class BinSwap:
    """One per-bin step, i.e. one `Swap` event: `amount_in` is the input credited to the
    bin (after the protocol-fee share), `total_fee` the full fee, `protocol_fee` the share
    moved to `_protocolFees`, `volatility_accumulator` the value used for this bin."""

    bin_id: int
    amount_in: int
    amount_out: int
    volatility_accumulator: int
    total_fee: int
    protocol_fee: int


@dataclass(frozen=True)
class SwapOutcome:
    amount_in: int
    amount_out: int
    bins: tuple[BinSwap, ...]
    hook_calls: int
    new_state: LiquidityBookPoolState


def _source(state: LiquidityBookPoolState) -> LBSource:
    source = SOURCES.get(state.source_key)
    if source is None:
        raise UnsupportedState(
            f"pool {state.pool_id!r}: LB source {state.source_key!r} is not admitted "
            f"(admitted: {sorted(SOURCES)})"
        )
    return source


def _swap_hook_calls(state: LiquidityBookPoolState, source: LBSource) -> int:
    flags = state.hooks_parameters & (BEFORE_SWAP_FLAG | AFTER_SWAP_FLAG)
    if not flags:
        return 0
    impl = (state.swap_hook_implementation or "").lower()
    if state.hooks_parameters & ADDRESS_MASK == 0 or impl not in source.amount_neutral_swap_hooks:
        raise UnsupportedState(
            f"pool {state.pool_id!r}: swap hook "
            f"{hex(state.hooks_parameters & ADDRESS_MASK)} (implementation "
            f"{state.swap_hook_implementation!r}) is not an admitted amount-neutral hook"
        )
    extra_flags = state.extra_hooks_parameters & (BEFORE_SWAP_FLAG | AFTER_SWAP_FLAG)
    if extra_flags:
        extra = (state.extra_swap_hook_implementation or "").lower()
        if (
            state.extra_hooks_parameters & ADDRESS_MASK == 0
            or extra not in source.amount_neutral_extra_swap_hooks
        ):
            raise UnsupportedState(
                f"pool {state.pool_id!r}: the swap hook forwards to extra hook "
                f"{hex(state.extra_hooks_parameters & ADDRESS_MASK)} (implementation "
                f"{state.extra_swap_hook_implementation!r}), which is not an admitted "
                "amount-neutral extra hook"
            )
    return bin(flags).count("1") + bin(extra_flags).count("1")


def swap(
    state: LiquidityBookPoolState,
    swap_for_y: bool,
    amount_in: int,
    *,
    math_reuse: BinMathReuse | None = None,
) -> SwapOutcome:
    """Migrated `LBPair.swap(swapForY, to)` after `amount_in` of the input token was
    transferred to the pair (the pair's balance equals `_reserves` before the transfer).

    Raises `LBRevert` where the contract reverts, `MissingState` when the traversal or fee
    computation needs uncollected state, `UnsupportedState` for an unadmitted source/hook.
    Never mutates `state`.

    `math_reuse` (WHI-1505 / L03, experimental, **None by default**) takes each bin price
    from the caller's bounded exact memo; `None` (every ordinary caller) is the reference
    call. Performance adoption is deferred to WHI-1510."""
    source = _source(state)
    if state.static_fee is None or state.variable_fee is None:
        raise MissingState(f"pool {state.pool_id!r}: static/variable fee parameters not collected")
    lo, hi = state.bin_range
    if not (lo <= state.active_id <= hi):
        raise MissingState(
            f"pool {state.pool_id!r}: active bin {state.active_id} is outside the collected "
            f"range {state.bin_range}"
        )

    # _reserves holds the bins' total including the protocol fees (getReserves subtracts them).
    reserves = _encode(
        state.reserve_x + state.protocol_fee_x, state.reserve_y + state.protocol_fee_y
    )
    protocol_fees = _encode(state.protocol_fee_x, state.protocol_fee_y)
    reserve_in = _decode(reserves)[0 if swap_for_y else 1]
    # receivedX/Y: balanceOf(this).safe128() - reserve  (balance = reserve + transfer)
    received = _safe(reserve_in + amount_in, 128) - reserve_in
    if received == 0:
        raise LBRevert(INSUFFICIENT_AMOUNT_IN)
    amounts_left = _encode(received, 0) if swap_for_y else _encode(0, received)

    hook_calls = _swap_hook_calls(state, source)  # Hooks.beforeSwap: amount-neutral no-op

    reserves = _packed_add(reserves, amounts_left)
    bin_step = state.bin_step
    params = encode_parameters(state.static_fee, state.variable_fee, state.active_id)
    active_id = state.active_id
    params = update_references(params, state.block_timestamp)
    protocol_share = _get(params, MASK_UINT14, OFFSET_PROTOCOL_SHARE)

    tree = tuple(sorted(state.bins))
    new_bins: dict[int, tuple[int, int]] = {}
    amounts_out = 0
    steps: list[BinSwap] = []

    while True:
        bin_reserves = _encode(*new_bins.get(active_id, state.bins.get(active_id, (0, 0))))
        out_reserve = _decode(bin_reserves)[1 if swap_for_y else 0]
        if out_reserve != 0:  # !binReserves.isEmpty(!swapForY)
            params = update_volatility_accumulator(params, active_id)
            amounts_in_with_fees, amounts_out_of_bin, total_fees = get_amounts(
                bin_reserves,
                params,
                bin_step,
                swap_for_y,
                active_id,
                amounts_left,
                math_reuse=math_reuse,
            )
            if amounts_in_with_fees > 0:
                amounts_left = _packed_sub(amounts_left, amounts_in_with_fees)
                amounts_out = _packed_add(amounts_out, amounts_out_of_bin)
                p_fees = _scalar_mul_div_basis_point_round_down(total_fees, protocol_share)
                if p_fees > 0:
                    protocol_fees = _packed_add(protocol_fees, p_fees)
                    amounts_in_with_fees = _packed_sub(amounts_in_with_fees, p_fees)
                new_bins[active_id] = _decode(
                    _packed_sub(_packed_add(bin_reserves, amounts_in_with_fees), amounts_out_of_bin)
                )
                side = 0 if swap_for_y else 1
                steps.append(
                    BinSwap(
                        bin_id=active_id,
                        amount_in=_decode(amounts_in_with_fees)[side],
                        amount_out=_decode(amounts_out_of_bin)[1 - side],
                        volatility_accumulator=_get(params, MASK_UINT20, OFFSET_VOL_ACC),
                        total_fee=_decode(total_fees)[side],
                        protocol_fee=_decode(p_fees)[side],
                    )
                )
        if amounts_left == 0:
            break
        next_id = next_non_empty_bin(tree, state.bin_range, swap_for_y, active_id, state.pool_id)
        # `if (nextId == 0 || nextId == type(uint24).max) revert LBPair__OutOfLiquidity();`
        # findFirstRight's sentinel is uint24.max, findFirstLeft's is 0; both must stop the
        # walk (treating 0 as a bin id restarts the Y->X search from the bottom forever).
        if next_id == 0 or next_id == UINT24_MAX:
            raise LBRevert(OUT_OF_LIQUIDITY)
        active_id = next_id

    if amounts_out == 0:
        raise LBRevert(INSUFFICIENT_AMOUNT_OUT)
    reserves = _packed_sub(reserves, amounts_out)
    # _oracle.update(parameters, activeId) writes only oracle samples / oracleId: not modeled.
    total_x, total_y = _decode(reserves)
    fee_x, fee_y = _decode(protocol_fees)
    bins: Mapping[int, tuple[int, int]] = state.bins
    if new_bins:
        merged = dict(state.bins)
        merged.update(new_bins)
        bins = merged
    new_state = replace(
        state,
        active_id=active_id,
        reserve_x=total_x - fee_x,
        reserve_y=total_y - fee_y,
        protocol_fee_x=fee_x,
        protocol_fee_y=fee_y,
        variable_fee=decode_variable(params),
        bins=bins,
    )
    out_x, out_y = _decode(amounts_out)
    return SwapOutcome(
        amount_in=received,
        amount_out=out_y if swap_for_y else out_x,
        bins=tuple(steps),
        hook_calls=hook_calls,
        new_state=new_state,
    )


def _failure(status: QuoteStatus, detail: str) -> SwapResult[LiquidityBookPoolState]:
    return SwapResult(
        status=status, amount_in_consumed=0, amount_out=0, new_state=None, detail=detail
    )


def quote_exact_in(
    state: LiquidityBookPoolState, token_in: str, amount_in_raw: int
) -> SwapResult[LiquidityBookPoolState]:
    """`quote_exact_in(pool_state, token_in, amount_in_raw) -> SwapResult`
    (docs/DESIGN.md §4.3), Liquidity Book specialization.

    An LB swap is all-or-nothing: it either consumes the whole input or reverts
    `LBPair__OutOfLiquidity`, which -- over a bin tree walked to the end of the id space
    on that side -- is real liquidity exhaustion (`INSUFFICIENT_LIQUIDITY`). Running out of
    *collected* bins, or missing fee state, is `INCOMPLETE_SNAPSHOT`. A swap whose output
    rounds to zero reverts `LBPair__InsufficientAmountOut` (`INSUFFICIENT_OUTPUT_AMOUNT`)."""
    if amount_in_raw <= 0:
        raise ValueError(f"amount_in_raw must be positive, got {amount_in_raw}")
    if token_in not in (state.token0, state.token1):
        return _failure(
            QuoteStatus.UNSUPPORTED_TOKEN,
            f"pool {state.pool_id!r} does not hold token {token_in!r}",
        )
    swap_for_y = token_in == state.token0
    try:
        outcome = swap(state, swap_for_y, amount_in_raw)
    except MissingState as exc:
        return _failure(QuoteStatus.INCOMPLETE_SNAPSHOT, str(exc))
    except UnsupportedState as exc:
        return _failure(QuoteStatus.UNSUPPORTED, str(exc))
    except LBRevert as exc:
        if exc.reason == OUT_OF_LIQUIDITY:
            return _failure(
                QuoteStatus.INSUFFICIENT_LIQUIDITY,
                f"pool {state.pool_id!r}: the book runs out of bins before {amount_in_raw} "
                f"is swapped (on-chain revert {OUT_OF_LIQUIDITY})",
            )
        if exc.reason == INSUFFICIENT_AMOUNT_OUT:
            return _failure(
                QuoteStatus.INSUFFICIENT_OUTPUT_AMOUNT,
                f"pool {state.pool_id!r}: amount_in {amount_in_raw} rounds down to 0 output "
                f"(on-chain revert {INSUFFICIENT_AMOUNT_OUT})",
            )
        return _failure(
            QuoteStatus.REVERTED, f"pool {state.pool_id!r}: swap reverts ({exc.reason})"
        )
    return SwapResult(
        status=QuoteStatus.OK,
        amount_in_consumed=outcome.amount_in,
        amount_out=outcome.amount_out,
        new_state=outcome.new_state,
        features=MappingProxyType(
            {"lb_bins_swapped": len(outcome.bins), "lb_swap_hook_calls": outcome.hook_calls}
        ),
    )

"""Integer concentrated-liquidity swap simulation for the admitted Uniswap-v3-family
sources (docs/DESIGN.md §2.3; WHI-1428).

`swap` is a line-for-line migration of the deployed pool's `swap` function for Exact
Input (`amountSpecified > 0`): `UniswapV3Pool.swap` (Uniswap/v3-core v1.0.0),
`AgniPool.swap` (agni-protocol/contracts@7278c3a) and `FusionXV3Pool.swap`
(FusionX-Finance/v3-contracts@7f7406e, explorer-verified flattened source). The three
deployed swap paths were diffed (docs/references/concentrated-liquidity-migration.md
§2); they share every math library byte-for-byte modulo formatting and differ only in:

1. **Protocol-fee encoding.** Uniswap: `slot0.feeProtocol` is a uint8 holding two
   4-bit denominators; `feeProtocol = zeroForOne ? fp % 16 : fp >> 4` and
   `delta = feeAmount / feeProtocol`. Agni/FusionX: a uint32 holding two 16-bit
   ratios; `feeProtocol = zeroForOne ? fp % 65536 : fp >> 16` and
   `delta = feeAmount.mul(feeProtocol) / 10000` (checked multiply).
2. **Liquidity-mining hook.** Agni/FusionX call `lmPool.accumulateReward(ts)` once per
   swap and `lmPool.crossLmTick(tick, zeroForOne)` on every initialized-tick crossing
   when `lmPool != address(0)`. These are external calls with no return value and no
   write to pool storage, so they cannot change amounts or pool state; they are
   modeled as explicit no-ops and counted in `SwapOutcome.lm_pool_hook_calls`. The
   only effect not modeled is a *revert inside the hook contract*; the fork evidence
   covers a live FusionX pool with a non-zero `lmPool`.

Every other branch (oracle observation writes, `tickCumulativeOutside` /
`secondsPerLiquidityOutsideX128` / `secondsOutside` tick fields, the `LOK` reentrancy
lock, token transfers and the callback balance check) is either amount-irrelevant or
outside the offline Exact Input domain; each omission is listed in the migration map.

The simulator never mutates its input: every call returns a fresh
`ConcentratedPoolState`. An unknown source key is refused (`UNSUPPORTED`) rather than
treated as "some V3" (DESIGN §2.3: "A shared CL implementation is allowed only after
Agni/FusionX/Uniswap differences are checked; each source retains its own admission
record and tests").
"""

from __future__ import annotations

from bisect import bisect_left
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from types import MappingProxyType

from pools.cl_math import (
    FEE_PIPS_DENOMINATOR,
    MAX_SQRT_RATIO,
    MAX_TICK,
    MIN_SQRT_RATIO,
    MIN_TICK,
    Q128,
    UINT128_MAX,
    UINT256_MAX,
    MissingState,
    SolidityRevert,
    TickMathReuse,
    add_delta,
    checked_mul_u256,
    checked_sub_i256,
    compute_swap_step_exact_in,
    get_sqrt_ratio_at_tick,
    get_tick_at_sqrt_ratio,
    mul_div,
    next_initialized_tick_within_one_word,
    to_int256,
)
from pools.result import QuoteStatus, SwapResult
from snapshot.models import ConcentratedPoolState, TickInfo


class ProtocolFeeRule(StrEnum):
    UNISWAP_V3_DENOMINATOR = "uniswap_v3_denominator"  # uint8, 4-bit halves, fee / n
    PANCAKE_V3_RATIO = "pancake_v3_ratio"  # uint32, 16-bit halves, fee * n / 10000


PANCAKE_PROTOCOL_FEE_SP = 65536  # AgniPool/FusionXV3Pool.PROTOCOL_FEE_SP
PANCAKE_PROTOCOL_FEE_DENOMINATOR = 10000  # ...PROTOCOL_FEE_DENOMINATOR


@dataclass(frozen=True)
class ConcentratedSource:
    """Admitted source-specific swap semantics. `key` matches `config/protocols.yaml`."""

    key: str
    pool_contract: str
    upstream: str
    protocol_fee_rule: ProtocolFeeRule
    has_lm_pool_hook: bool


SOURCES: Mapping[str, ConcentratedSource] = MappingProxyType(
    {
        "uniswap_v3": ConcentratedSource(
            key="uniswap_v3",
            pool_contract="UniswapV3Pool",
            upstream="Uniswap/v3-core v1.0.0 (e3589b1) contracts/UniswapV3Pool.sol",
            protocol_fee_rule=ProtocolFeeRule.UNISWAP_V3_DENOMINATOR,
            has_lm_pool_hook=False,
        ),
        "agni_v3": ConcentratedSource(
            key="agni_v3",
            pool_contract="AgniPool",
            upstream="agni-protocol/contracts@7278c3a core/contracts/AgniPool.sol",
            protocol_fee_rule=ProtocolFeeRule.PANCAKE_V3_RATIO,
            has_lm_pool_hook=True,
        ),
        "fusionx_v3": ConcentratedSource(
            key="fusionx_v3",
            pool_contract="FusionXV3Pool",
            upstream=(
                "FusionX-Finance/v3-contracts@7f7406e projects/v3-core/contracts/FusionXV3Pool.sol"
            ),
            protocol_fee_rule=ProtocolFeeRule.PANCAKE_V3_RATIO,
            has_lm_pool_hook=True,
        ),
    }
)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class UnsupportedState(Exception):  # noqa: N818
    """The state is outside the admitted semantics (unknown source, undeclared hook)."""


@dataclass(frozen=True)
class SwapOutcome:
    """Result of one migrated `pool.swap(recipient, zeroForOne, amountSpecified,
    sqrtPriceLimitX96, data)` call. `amount0`/`amount1` are the pool's signed return
    values (positive = paid into the pool, negative = paid out)."""

    amount0: int
    amount1: int
    new_state: ConcentratedPoolState
    crossed_ticks: tuple[int, ...]
    steps: int
    lm_pool_hook_calls: int


def _source(state: ConcentratedPoolState) -> ConcentratedSource:
    source = SOURCES.get(state.source_key)
    if source is None:
        raise UnsupportedState(
            f"pool {state.pool_id!r}: CL source {state.source_key!r} is not admitted "
            f"(admitted: {sorted(SOURCES)})"
        )
    has_lm_pool = state.lm_pool is not None and state.lm_pool.lower() != ZERO_ADDRESS
    if has_lm_pool and not source.has_lm_pool_hook:
        raise UnsupportedState(
            f"pool {state.pool_id!r}: source {source.key!r} has no LM-pool hook but the "
            f"state carries lm_pool={state.lm_pool!r}"
        )
    return source


def _input_fee_protocol(source: ConcentratedSource, fee_protocol: int, zero_for_one: bool) -> int:
    if source.protocol_fee_rule is ProtocolFeeRule.UNISWAP_V3_DENOMINATOR:
        return fee_protocol % 16 if zero_for_one else fee_protocol >> 4
    return fee_protocol % PANCAKE_PROTOCOL_FEE_SP if zero_for_one else fee_protocol >> 16


def _protocol_fee_delta(source: ConcentratedSource, fee_amount: int, fee_protocol: int) -> int:
    if source.protocol_fee_rule is ProtocolFeeRule.UNISWAP_V3_DENOMINATOR:
        return fee_amount // fee_protocol
    return checked_mul_u256(fee_amount, fee_protocol) // PANCAKE_PROTOCOL_FEE_DENOMINATOR


# A prefix checkpoint: the loop variables after a completed step, with the cumulative
# gross input (`amountIn + feeAmount`) in place of the amount-dependent remainder:
# (consumed, amount_calculated, sqrt_price, tick, fee_growth_global, protocol_fee,
#  liquidity, crossed_count, steps, lm_calls).
_Checkpoint = tuple[int, int, int, int, int, int, int, int, int, int]
_STATE_FIELDS = tuple(f.name for f in fields(ConcentratedPoolState))


class _PrefixRecord:
    __slots__ = ("costs", "crossings", "fingerprint", "points", "state")

    def __init__(self, state: ConcentratedPoolState, fingerprint: tuple[object, ...]) -> None:
        self.state = state  # held strongly: its `id` keys the record and cannot be reused
        self.fingerprint = fingerprint
        self.points: list[_Checkpoint] = []
        self.costs: list[int] = []  # costs[i] == points[i][0], nondecreasing
        self.crossings: list[tuple[int, TickInfo]] = []  # in crossing order


class CLPrefixReuse:
    """WHI-1506 / L04 experiment, **off unless passed explicitly**: exact reuse of
    completed CL traversal steps across *independent* Exact Input queries on one
    original state (never a sequence of swaps). Owned by the caller; no module state.

    A record is keyed by the state object (held, so its identity is stable), direction,
    price limit and the L02 toggle, and checked against a fingerprint of every state
    field (the frozen mappings compare by identity first); a state changed in place is
    re-recorded, a returned `new_state` is a different key. It holds checkpoints after
    each completed *full* step (`computeSwapStep` reached its target with
    `amountRemainingLessFee >= amountIn`), with the cumulative gross input `C`.

    Exactness: with `fee < 1e6`, `mulDiv(R, 1e6 - fee, 1e6) >= a` iff
    `R >= a + mulDivRoundingUp(a, fee, 1e6 - fee)`, the step's gross cost. So step `i`
    of a query of amount `A` takes the full branch -- whose results do not depend on
    the remainder -- iff `A >= C[i+1]`, and runs at all iff `A > C[i]`. The query
    resumes at the first checkpoint `j` with `C[j] >= A` when `C[j] == A` (input
    exhausted exactly: the reference stops there, never crossing later zero-cost
    steps), else at `j - 1` and runs the remaining partial step with the reference
    code; beyond the recorded frontier it runs the reference loop and extends the
    record. Partial steps, errors and the epilogue (full new state) are never cached.

    Bounds: at most `max_keys` records and `max_checkpoints` checkpoints in total (each
    record's initial point counts), least recently used record evicted first; a record
    that cannot grow stops extending and the reference loop continues. `stats()` counts
    queries, resumed queries, reused (not executed) logical steps and evictions."""

    def __init__(self, max_keys: int, max_checkpoints: int) -> None:
        for name, value in (("max_keys", max_keys), ("max_checkpoints", max_checkpoints)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive int, got {value!r}")
        self.max_keys = max_keys
        self.max_checkpoints = max_checkpoints
        self._records: OrderedDict[tuple[int, bool, int, bool], _PrefixRecord] = OrderedDict()
        self._checkpoints = 0
        self._counts = dict.fromkeys(
            ("queries", "resumed", "reused_steps", "recorded_steps", "evictions", "invalidated"),
            0,
        )

    def stats(self) -> dict[str, int]:
        return {
            "max_keys": self.max_keys,
            "max_checkpoints": self.max_checkpoints,
            "keys": len(self._records),
            "checkpoints": self._checkpoints,
        } | self._counts

    def clear(self) -> None:
        self._records.clear()
        self._checkpoints = 0

    def _evict_for(self, keep: _PrefixRecord, keys: int, points: int) -> bool:
        """Evict LRU records other than `keep` until `keys`/`points` more fit."""
        records = self._records
        while len(records) + keys > self.max_keys or self._checkpoints + points > (
            self.max_checkpoints
        ):
            victim = next((k for k, r in records.items() if r is not keep), None)
            if victim is None:
                return False
            self._checkpoints -= len(records.pop(victim).points)
            self._counts["evictions"] += 1
        return True

    def _record(
        self, state: ConcentratedPoolState, key: tuple[int, bool, int, bool], initial: _Checkpoint
    ) -> _PrefixRecord:
        self._counts["queries"] += 1
        fingerprint = tuple(getattr(state, name) for name in _STATE_FIELDS)
        record = self._records.get(key)
        if record is not None:
            if record.state is state and record.fingerprint == fingerprint:
                self._records.move_to_end(key)
                return record
            self._checkpoints -= len(self._records.pop(key).points)  # changed in place
            self._counts["invalidated"] += 1
        record = _PrefixRecord(state, fingerprint)
        record.points.append(initial)
        record.costs.append(0)
        self._evict_for(record, 1, 1)
        self._records[key] = record
        self._checkpoints += 1
        return record

    def _resume_index(self, record: _PrefixRecord, amount: int) -> int:
        costs = record.costs
        j = bisect_left(costs, amount)  # amount > 0 == costs[0], so j >= 1
        at = len(costs) - 1 if j == len(costs) else j if costs[j] == amount else j - 1
        if at:
            self._counts["resumed"] += 1
            self._counts["reused_steps"] += record.points[at][8]
        return at

    def _append(
        self,
        record: _PrefixRecord,
        point: _Checkpoint,
        crossed: list[int],
        ticks: dict[int, TickInfo],
    ) -> bool:
        if not self._evict_for(record, 0, 1):
            return False
        if len(crossed) > len(record.crossings):  # this step crossed one tick
            record.crossings.append((crossed[-1], ticks[crossed[-1]]))
        record.points.append(point)
        record.costs.append(point[0])
        self._checkpoints += 1
        self._counts["recorded_steps"] += 1
        return True


def _empty_span_limit_tick(zero_for_one: bool, sqrt_price_limit_x96: int) -> int:
    """The tick bound of the price limit for empty-span skipping (WHI-1504).

    `getSqrtRatioAtTick` is strictly increasing and `getTickAtSqrtRatio(p)` is the
    greatest tick `t` with `getSqrtRatioAtTick(t) <= p`, so with `L` that tick:
    zeroForOne -- `sqrtPriceNext > limit` iff `tickNext > L`;
    oneForZero -- `sqrtPriceNext < limit` iff `tickNext < U`, where `U` is `L` when
    `getSqrtRatioAtTick(L) == limit` and `L + 1` otherwise. A step may be skipped only
    when its target is the tick (strictly before the limit), never the limit itself."""
    bound = get_tick_at_sqrt_ratio(sqrt_price_limit_x96)
    if zero_for_one or get_sqrt_ratio_at_tick(bound) == sqrt_price_limit_x96:
        return bound
    return bound + 1


def swap(
    state: ConcentratedPoolState,
    zero_for_one: bool,
    amount_specified: int,
    sqrt_price_limit_x96: int,
    *,
    skip_empty_spans: bool = False,
    math_reuse: TickMathReuse | None = None,
    prefix_reuse: CLPrefixReuse | None = None,
) -> SwapOutcome:
    """Migrated pool `swap` for Exact Input (`amount_specified > 0`).

    Raises `SolidityRevert` where the contract reverts, `MissingState` when the
    traversal needs uncollected bitmap/tick state, and `UnsupportedState` for an
    unadmitted source. Never mutates `state`.

    `skip_empty_spans` (WHI-1504, experimental, **off by default**) executes the loop
    iterations of a provably empty zero-liquidity span without their per-step math.
    Every ordinary caller (`quote_exact_in`, the evaluator, all solvers) runs the
    reference loop; only differential tests and the L02 experiment tooling select
    `True`. Its performance adoption is deferred to WHI-1510 (owner amendment
    2026-09-26). With `liquidity == 0` an
    iteration whose target is an *uninitialized* tick strictly before the price limit
    has `computeSwapStep` return `(sqrtPriceNext, 0, 0, 0)` (every delta is a
    multiple of zero liquidity; `fee < 1e6` keeps the fee `mulDivRoundingUp` from
    reverting), so it changes nothing but `sqrtPriceX96`/`tick`, and the next
    iteration's liquidity is still zero. Such iterations still check their bitmap
    word against the collected state (an uncollected word is never empty: the
    reference iteration reads it and raises the same `MissingState`) and are still
    counted in `steps`; the span stops at an initialized
    tick, the price limit and the MIN/MAX tick clamp, where the unchanged reference
    iteration runs. `fee >= 1e6` makes every zero-liquidity iteration revert, so
    such a pool is never skipped (a skipped word read could otherwise turn that
    revert into `MissingState`). `False` (the default) is the unmodified reference
    loop.

    `math_reuse` (WHI-1505 / L03, experimental, **None by default**) takes the loop's
    `getSqrtRatioAtTick(tickNext)` from the caller's bounded exact memo instead of
    recomputing it; every other call and the traversal itself are unchanged. `None` (the
    default, used by every ordinary caller) calls the reference function. Its performance
    adoption is deferred to WHI-1510 as well.

    `prefix_reuse` (WHI-1506 / L04, experimental, **None by default**) starts the loop
    from the caller's exact checkpoint of an earlier independent query on this same
    state/direction/limit/toggle (see `CLPrefixReuse`) and records newly completed full
    steps. Validation, the remaining steps and the complete new state are the reference
    code's; a pool with `fee >= 1e6` never uses it. `None` (every ordinary caller) runs
    the loop from the state. Its performance adoption is deferred to WHI-1510."""
    source = _source(state)
    if amount_specified == 0:
        raise SolidityRevert("AS")
    if amount_specified < 0:
        raise ValueError("Exact Output (amountSpecified < 0) is not migrated")
    to_int256(amount_specified)  # the int256 argument itself must be representable
    # 'LOK' (slot0.unlocked) is not modeled: a pool at a block boundary is unlocked.
    if zero_for_one:
        if not (MIN_SQRT_RATIO < sqrt_price_limit_x96 < state.sqrt_price_x96):
            raise SolidityRevert("SPL")
    elif not (state.sqrt_price_x96 < sqrt_price_limit_x96 < MAX_SQRT_RATIO):
        raise SolidityRevert("SPL")

    has_lm_pool = state.lm_pool is not None and state.lm_pool.lower() != ZERO_ADDRESS
    lm_calls = 1 if has_lm_pool else 0  # lmPool.accumulateReward(blockTimestamp): no-op
    fee_protocol = _input_fee_protocol(source, state.fee_protocol, zero_for_one)
    word_lo, word_hi = state.bitmap_word_range

    def read_word(word_pos: int) -> int:
        if not (word_lo <= word_pos <= word_hi):
            raise MissingState(
                f"pool {state.pool_id!r}: tickBitmap word {word_pos} is outside the "
                f"collected range [{word_lo}, {word_hi}]"
            )
        return state.tick_bitmap.get(word_pos, 0)

    amount_remaining = amount_specified
    amount_calculated = 0
    sqrt_price = state.sqrt_price_x96
    tick = state.tick
    fee_growth_global = (
        state.fee_growth_global0_x128 if zero_for_one else state.fee_growth_global1_x128
    )
    protocol_fee = 0
    liquidity = state.liquidity
    new_ticks: dict[int, TickInfo] = {}
    crossed: list[int] = []
    steps = 0
    skip_empty_spans = skip_empty_spans and state.fee < FEE_PIPS_DENOMINATOR
    limit_tick: int | None = None
    bitmap_get = state.tick_bitmap.get
    sqrt_ratio_at_tick = (
        get_sqrt_ratio_at_tick if math_reuse is None else math_reuse.sqrt_ratio_at_tick
    )
    record = None
    recording = False
    if prefix_reuse is not None and state.fee < FEE_PIPS_DENOMINATOR:
        key = (id(state), zero_for_one, sqrt_price_limit_x96, skip_empty_spans)
        initial = (0, 0, sqrt_price, tick, fee_growth_global, 0, liquidity, 0, 0, lm_calls)
        record = prefix_reuse._record(state, key, initial)
        at = prefix_reuse._resume_index(record, amount_specified)
        consumed, amount_calculated, sqrt_price, tick, fee_growth_global = record.points[at][:5]
        protocol_fee, liquidity, crossed_count, steps, lm_calls = record.points[at][5:]
        amount_remaining = amount_specified - consumed
        crossed = [t for t, _ in record.crossings[:crossed_count]]
        new_ticks = dict(record.crossings[:crossed_count])
        recording = at == len(record.points) - 1  # only the frontier grows

    while amount_remaining != 0 and sqrt_price != sqrt_price_limit_x96:
        if liquidity == 0 and skip_empty_spans:
            if limit_tick is None:
                limit_tick = _empty_span_limit_tick(zero_for_one, sqrt_price_limit_x96)
            # The span's first iteration may start mid-word: the reference search.
            tick_next, initialized = next_initialized_tick_within_one_word(
                read_word, tick, state.tick_spacing, zero_for_one
            )
            if not initialized and (
                tick_next > limit_tick if zero_for_one else tick_next < limit_tick
            ):
                # Every later iteration reads one whole word: it is empty iff that
                # word is collected and zero, and targets the word's far end tick
                # (zeroForOne `word * W`, oneForZero `(word + 1) * W - spacing`). The
                # first word that is nonzero, uncollected or not strictly before the
                # limit is left to the reference iteration below (same read, same
                # `MissingState`). `limit_tick` lies in [MIN_TICK, MAX_TICK], so a
                # target the reference would clamp always stops the span.
                spacing = state.tick_spacing
                word_ticks = 256 * spacing
                if zero_for_one:
                    first = word = tick_next // word_ticks - 1
                    while (
                        word >= word_lo
                        and not bitmap_get(word, 0)
                        and word * word_ticks > limit_tick
                    ):
                        word -= 1
                    steps += 1 + first - word
                    tick = (word + 1) * word_ticks - 1
                else:
                    first = word = (tick_next + spacing) // word_ticks
                    while (
                        word <= word_hi
                        and not bitmap_get(word, 0)
                        and (word + 1) * word_ticks - spacing < limit_tick
                    ):
                        word += 1
                    steps += 1 + word - first
                    tick = word * word_ticks - spacing
                # `sqrt_price` is deliberately left at its pre-span value: the stopping
                # iteration below always runs with zero liquidity, and a zero-liquidity
                # `computeSwapStep` lands on its target whatever its start price (after
                # a non-empty span that target lies strictly beyond the pre-span
                # price), so no skipped price is ever observable.
        steps += 1
        sqrt_price_start = sqrt_price
        tick_next, initialized = next_initialized_tick_within_one_word(
            read_word, tick, state.tick_spacing, zero_for_one
        )
        tick_next = max(MIN_TICK, min(MAX_TICK, tick_next))
        sqrt_price_next = sqrt_ratio_at_tick(tick_next)

        if zero_for_one:
            target = (
                sqrt_price_limit_x96 if sqrt_price_next < sqrt_price_limit_x96 else sqrt_price_next
            )
        else:
            target = (
                sqrt_price_limit_x96 if sqrt_price_next > sqrt_price_limit_x96 else sqrt_price_next
            )
        sqrt_price, amount_in, amount_out, fee_amount = compute_swap_step_exact_in(
            sqrt_price, target, liquidity, amount_remaining, state.fee
        )

        amount_remaining -= to_int256(amount_in + fee_amount)
        amount_calculated = checked_sub_i256(amount_calculated, to_int256(amount_out))

        if fee_protocol > 0:
            delta = _protocol_fee_delta(source, fee_amount, fee_protocol)
            fee_amount -= delta
            protocol_fee = (protocol_fee + (delta & UINT128_MAX)) & UINT128_MAX

        if liquidity > 0:
            fee_growth_global = (
                fee_growth_global + mul_div(fee_amount, Q128, liquidity)
            ) & UINT256_MAX

        if sqrt_price == sqrt_price_next:
            if initialized:
                info = state.ticks.get(tick_next)  # a swap crosses each tick at most once
                if info is None:
                    raise MissingState(
                        f"pool {state.pool_id!r}: initialized tick {tick_next} has no "
                        "collected tick data"
                    )
                if has_lm_pool:
                    lm_calls += 1  # lmPool.crossLmTick(tickNext, zeroForOne): no-op
                # Tick.cross: the input token's growth is the in-flight value, the
                # other token's is the pool's storage value at swap start.
                fg0 = fee_growth_global if zero_for_one else state.fee_growth_global0_x128
                fg1 = state.fee_growth_global1_x128 if zero_for_one else fee_growth_global
                new_ticks[tick_next] = TickInfo(
                    liquidity_gross=info.liquidity_gross,
                    liquidity_net=info.liquidity_net,
                    fee_growth_outside0_x128=(fg0 - info.fee_growth_outside0_x128) & UINT256_MAX,
                    fee_growth_outside1_x128=(fg1 - info.fee_growth_outside1_x128) & UINT256_MAX,
                )
                liquidity_net = -info.liquidity_net if zero_for_one else info.liquidity_net
                liquidity = add_delta(liquidity, liquidity_net)
                crossed.append(tick_next)
            tick = tick_next - 1 if zero_for_one else tick_next
        elif sqrt_price != sqrt_price_start:
            tick = get_tick_at_sqrt_ratio(sqrt_price)

        if recording and prefix_reuse is not None and record is not None:
            # A full step reached its target and cost no more than the remainder (a
            # partial step either stops short or, reaching it, costs more).
            recording = (
                sqrt_price == target
                and amount_remaining >= 0
                and prefix_reuse._append(
                    record,
                    (amount_specified - amount_remaining, amount_calculated, sqrt_price, tick,
                     fee_growth_global, protocol_fee, liquidity, len(crossed), steps, lm_calls),
                    crossed,
                    new_ticks,
                )
            )  # fmt: skip

    protocol_fees0 = state.protocol_fees0
    protocol_fees1 = state.protocol_fees1
    if zero_for_one:
        fee_growth0, fee_growth1 = fee_growth_global, state.fee_growth_global1_x128
        if protocol_fee > 0:
            protocol_fees0 = (protocol_fees0 + protocol_fee) & UINT128_MAX
        amount0, amount1 = amount_specified - amount_remaining, amount_calculated
    else:
        fee_growth0, fee_growth1 = state.fee_growth_global0_x128, fee_growth_global
        if protocol_fee > 0:
            protocol_fees1 = (protocol_fees1 + protocol_fee) & UINT128_MAX
        amount0, amount1 = amount_calculated, amount_specified - amount_remaining

    ticks: Mapping[int, TickInfo] = state.ticks
    if new_ticks:
        merged = dict(state.ticks)
        merged.update(new_ticks)
        ticks = merged
    new_state = ConcentratedPoolState(
        pool_id=state.pool_id,
        source_key=state.source_key,
        token0=state.token0,
        token1=state.token1,
        fee=state.fee,
        tick_spacing=state.tick_spacing,
        sqrt_price_x96=sqrt_price,
        tick=tick,
        liquidity=liquidity,
        fee_protocol=state.fee_protocol,
        fee_growth_global0_x128=fee_growth0,
        fee_growth_global1_x128=fee_growth1,
        protocol_fees0=protocol_fees0,
        protocol_fees1=protocol_fees1,
        bitmap_word_range=state.bitmap_word_range,
        tick_bitmap=state.tick_bitmap,  # a swap never flips bitmap bits
        ticks=ticks,
        lm_pool=state.lm_pool,
    )
    return SwapOutcome(
        amount0=amount0,
        amount1=amount1,
        new_state=new_state,
        crossed_ticks=tuple(crossed),
        steps=steps,
        lm_pool_hook_calls=lm_calls,
    )


def _failure(status: QuoteStatus, detail: str) -> SwapResult[ConcentratedPoolState]:
    return SwapResult(
        status=status, amount_in_consumed=0, amount_out=0, new_state=None, detail=detail
    )


def quote_exact_in(
    state: ConcentratedPoolState, token_in: str, amount_in_raw: int
) -> SwapResult[ConcentratedPoolState]:
    """`quote_exact_in(pool_state, token_in, amount_in_raw) -> SwapResult`
    (docs/DESIGN.md §4.3), CL specialization.

    Runs `swap` with the widest legal price limit (`MIN_SQRT_RATIO + 1` /
    `MAX_SQRT_RATIO - 1`, what QuoterV2/SwapRouter use for a zero limit). A swap that
    stops at that limit with input left over is a partial fill, i.e. real liquidity
    exhaustion over fully collected state: `INSUFFICIENT_LIQUIDITY`, never a silent
    Exact Input success. Missing collected state is `INCOMPLETE_SNAPSHOT`. A zero
    output is `OK` (the pool itself does not revert on a fee-only dust swap and its
    state -- fee growth -- does change); a router's minimum-output check is a plan
    concern, not pool math."""
    if amount_in_raw <= 0:
        raise ValueError(f"amount_in_raw must be positive, got {amount_in_raw}")
    if token_in not in (state.token0, state.token1):
        return _failure(
            QuoteStatus.UNSUPPORTED_TOKEN,
            f"pool {state.pool_id!r} does not hold token {token_in!r}",
        )
    zero_for_one = token_in == state.token0
    limit = MIN_SQRT_RATIO + 1 if zero_for_one else MAX_SQRT_RATIO - 1
    try:
        outcome = swap(state, zero_for_one, amount_in_raw, limit)
    except MissingState as exc:
        return _failure(QuoteStatus.INCOMPLETE_SNAPSHOT, str(exc))
    except UnsupportedState as exc:
        return _failure(QuoteStatus.UNSUPPORTED, str(exc))
    except SolidityRevert as exc:
        if exc.reason == "SPL":
            # The price already sits at the extreme limit: nothing more can be sold
            # in this direction.
            bound = "lower" if zero_for_one else "upper"
            return _failure(
                QuoteStatus.INSUFFICIENT_LIQUIDITY,
                f"pool {state.pool_id!r}: price is already at the {bound} bound "
                "(on-chain revert 'SPL')",
            )
        return _failure(
            QuoteStatus.REVERTED, f"pool {state.pool_id!r}: swap reverts ({exc.reason})"
        )

    consumed = outcome.amount0 if zero_for_one else outcome.amount1
    produced = -(outcome.amount1 if zero_for_one else outcome.amount0)
    features = MappingProxyType(
        {
            "initialized_ticks_crossed": len(outcome.crossed_ticks),
            "swap_steps": outcome.steps,
            "lm_pool_hook_calls": outcome.lm_pool_hook_calls,
        }
    )
    if consumed != amount_in_raw:
        return SwapResult(
            status=QuoteStatus.INSUFFICIENT_LIQUIDITY,
            amount_in_consumed=0,
            amount_out=0,
            new_state=None,
            detail=(
                f"pool {state.pool_id!r}: only {consumed} of {amount_in_raw} input can be "
                f"swapped before the price limit (partial fill would yield {produced})"
            ),
            features=features,
        )
    return SwapResult(
        status=QuoteStatus.OK,
        amount_in_consumed=consumed,
        amount_out=produced,
        new_state=outcome.new_state,
        features=features,
    )

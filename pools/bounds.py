"""Per-pool output upper bounds (WHI-1599; spec `docs/references/research-022/output-bounds.md`
R022-Q01/1 §1, §4, §5.3, §7; pruning use `pruning-contract.md` R022-Q02/1 §3.4, §10.3).

For one pool and swap direction `output_bound` returns either `None` ("no bound") or an
`OutputBound(rate, slack)` of exact rationals, computed from the **original frozen pool state**
only (never from a swapped `new_state`: pruning-contract §3.3), such that for every `OK` quote

    quote_exact_in(state, token_in, x).amount_out  <=  floor(rate * x)

and, for `x + m <= CHUNK_DOMAIN_MAX`, the chunk marginal obeys
`q(x + m) - q(x) <= rate * m + slack` (a chunk search, WHI-1600, adds the slack per hop; a
single path needs the rate alone). `rate` and `slack` are independent: a CL 0->1 pool with
`sqrt_price_x96 >= 2**128` has a rate and no slack.

`None` is never replaced by `0`, by infinity or by a guess, and a pool/direction without a
bound is never pruned on. All arithmetic is integer/`Fraction`; there are no floats. The
checks below mirror the migrated quote code's own gates (`constant_product.quote_exact_in`,
`concentrated._source`, `liquidity_book.get_price_from_id`): a state outside them is `None`.

`build_bounds` computes the whole table of a bundle once (the caller's `prepare` charges it);
the helper itself is pure and takes no part in the metered quote seam.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from time import perf_counter
from types import MappingProxyType

from pools import concentrated, constant_product, liquidity_book
from pools.cl_math import (
    FEE_PIPS_DENOMINATOR,
    Q96,
    UINT128_MAX,
    SolidityRevert,
    get_sqrt_ratio_at_tick,
)
from pools.liquidity_book import MAX_FEE, PRECISION, SCALE, LBRevert, get_price_from_id
from snapshot.models import (
    ConcentratedPoolState,
    ConstantProductPoolState,
    LiquidityBookPoolState,
    PoolState,
    SnapshotBundle,
)

__all__ = [
    "CHUNK_DOMAIN_MAX",
    "BoundTable",
    "OutputBound",
    "bound_out",
    "build_bounds",
    "output_bound",
]

# The chunk-slack domain (output-bounds §5.3): valid only for `x + m <= 2**127`.
CHUNK_DOMAIN_MAX = 1 << 127
# The LB price guard (output-bounds §4.3, Lemma M): `2**38 <= price_int <= 2**218`.
_LB_PRICE_MIN = 1 << 38
_LB_PRICE_MAX = 1 << 218
_Q192 = 1 << 192
_CL_SLACK_PRICE_MAX = 1 << 128  # CL 0->1 slack needs `sqrt_price_x96 < 2**128`


@dataclass(frozen=True, slots=True)
class OutputBound:
    """`rate`: exact rational with `q(x) <= floor(rate * x)`; `slack`: the chunk slack, or
    `None` when only the rate is proved."""

    rate: Fraction
    slack: Fraction | None


def bound_out(rate: Fraction, amount: int) -> int:
    """`floor(rate * amount)` in integers (one hop of the nested-floor path bound)."""
    return (rate.numerator * amount) // rate.denominator


def _sqrt_ratio(tick: int) -> int | None:
    try:
        return get_sqrt_ratio_at_tick(tick)
    except SolidityRevert:
        return None


def _cpmm(state: ConstantProductPoolState, token_in: str) -> OutputBound | None:
    if state.source_key is not None:
        source = constant_product.SOURCES.get(state.source_key)
        if source is None or state.fee_bps != source.fee_bps:
            return None
    if not 0 <= state.fee_bps < constant_product.FEE_DENOMINATOR:
        return None
    reserve_in, reserve_out = state.reserves_for(token_in)
    if reserve_in <= 0 or reserve_out <= 0:
        return None
    keep = constant_product.FEE_DENOMINATOR - state.fee_bps
    rate = Fraction(keep * reserve_out, constant_product.FEE_DENOMINATOR * reserve_in)
    return OutputBound(rate, Fraction(1))


def _cl(state: ConcentratedPoolState, token_in: str) -> OutputBound | None:
    source = concentrated.SOURCES.get(state.source_key)
    if source is None:
        return None
    if (
        state.lm_pool is not None
        and state.lm_pool.lower() != concentrated.ZERO_ADDRESS
        and not source.has_lm_pool_hook
    ):
        return None
    if not 0 <= state.fee < FEE_PIPS_DENOMINATOR:
        return None
    price, zero_for_one = state.sqrt_price_x96, token_in == state.token0
    # Guard G_CL (output-bounds §4.2): the frozen tick must not sit above the price and, for
    # 1->0, not lag more than one tick below it (else the loop's first target lies on the
    # wrong side and the exact quote can out-earn the rate).
    at_tick = _sqrt_ratio(state.tick)
    if at_tick is None or price < at_tick:
        return None
    if not zero_for_one:
        above = _sqrt_ratio(state.tick + 1)
        if above is None or price > above:
            return None
    keep = FEE_PIPS_DENOMINATOR - state.fee
    # L̂ = min(2**128 - 1, liquidity + sum liquidity_gross): every liquidity the swap can see
    liquidity_cap = min(
        UINT128_MAX, state.liquidity + sum(t.liquidity_gross for t in state.ticks.values())
    )
    if zero_for_one:
        rate = Fraction(keep * price * price, FEE_PIPS_DENOMINATOR * _Q192)
        slack = (
            None
            if price >= _CL_SLACK_PRICE_MAX
            else Fraction(price * price, _Q192) + 1 + Fraction(liquidity_cap, Q96)
        )
        return OutputBound(rate, slack)
    rate = Fraction(keep * _Q192, FEE_PIPS_DENOMINATOR * price * price)
    spot = Fraction(_Q192, price * price)
    return OutputBound(rate, spot * (1 + Fraction(liquidity_cap, Q96)) + 1)


def _lb(state: LiquidityBookPoolState, token_in: str) -> OutputBound | None:
    if state.source_key not in liquidity_book.SOURCES:
        return None
    static = state.static_fee
    if static is None or state.bin_step < 1:
        return None
    base_fee = static.base_factor * state.bin_step * 10**10  # β: the base fee (output-bounds §3.1)
    if base_fee > MAX_FEE:
        return None
    try:
        price = get_price_from_id(state.active_id, state.bin_step)
    except LBRevert:
        return None
    if not _LB_PRICE_MIN <= price <= _LB_PRICE_MAX:
        return None
    keep = PRECISION - base_fee
    if token_in == state.token0:  # swapForY: Y out, `price` is Y per X
        return OutputBound(Fraction(keep * price, PRECISION * SCALE), Fraction(price, SCALE) + 1)
    return OutputBound(Fraction(keep * SCALE, PRECISION * price), Fraction(SCALE, price) + 1)


def output_bound(state: PoolState, token_in: str) -> OutputBound | None:
    """The bound of `state` for the swap `token_in -> other token`, or `None` (no bound):
    token not in the pool, unadmitted source/state, or a proof guard that fails."""
    if token_in not in (state.token0, state.token1):
        return None
    if isinstance(state, ConstantProductPoolState):
        return _cpmm(state, token_in)
    if isinstance(state, ConcentratedPoolState):
        return _cl(state, token_in)
    if isinstance(state, LiquidityBookPoolState):
        return _lb(state, token_in)
    return None


@dataclass(frozen=True, slots=True)
class BoundTable:
    """The immutable bound of every `(pool_id, token_in)` direction of a bundle (a missing
    value is `None`), plus what building it cost (pruning-contract §10.3). `seconds` is wall time,
    so it stays on the prepared object and out of `prepare_record`: a recorded `search` must be
    deterministic (replay and `order-check` compare it); the runner's own preparation event
    already charges the bound table's time to the strategy."""

    bounds: Mapping[tuple[str, str], OutputBound | None]
    pool_directions: int
    bounded: int
    rate_only: int
    no_bound: int
    seconds: float

    def prepare_record(self) -> dict[str, int]:
        """The deterministic part of the `search_stats["bound_pruning"]["prepare"]` record."""
        return {
            "pool_directions": self.pool_directions,
            "bounded": self.bounded,
            "rate_only": self.rate_only,
            "no_bound": self.no_bound,
        }


def build_bounds(bundle: SnapshotBundle) -> BoundTable:
    """Eagerly compute every pool direction of `bundle` from its frozen states."""
    started = perf_counter()
    table: dict[tuple[str, str], OutputBound | None] = {}
    for pool_id, state in bundle.pools.items():
        for token_in in (state.token0, state.token1):
            table[(pool_id, token_in)] = output_bound(state, token_in)
    values = list(table.values())
    rate_only = sum(1 for b in values if b is not None and b.slack is None)
    no_bound = sum(1 for b in values if b is None)
    return BoundTable(
        bounds=MappingProxyType(table),
        pool_directions=len(values),
        bounded=len(values) - no_bound - rate_only,
        rate_only=rate_only,
        no_bound=no_bound,
        seconds=perf_counter() - started,
    )

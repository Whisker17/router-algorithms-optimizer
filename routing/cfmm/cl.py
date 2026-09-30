"""Continuous V3 aggregate and immutable prepared interval index of `cfmm_dual` (WHI-1559
component A; `cfmm-dual.md` §8, gates G-L1-G-L3).

Ported from the validated WHI-1557 executable contract model
(`tests/routing/cfmm_contract_model.py` `cl_admitted` / `cl_ladder` / `cl_arb` /
`cl_forward`, sha256 `7fb979394cbcba243058fe38ebdc37b56602735ab3887475c6386851006ac387`;
not imported), keeping its float operation order so the pinned author `UniV3` fixtures
reproduce. The continuous model is the paper's bounded-liquidity aggregate (Diamandis,
Resnick, Chitra, Angeris, arXiv:2302.04938v1, §3-§4.1) as the pinned author code
evaluates it (`bcc-research/CFMMRouter.jl` `5932e42e5077ffc7d8e02c3b3ad2e9ed1d441267`,
`UniV3` `find_arb!`; MIT, notice in `routing/cfmm/NOTICE.md`). Standard library only.

**Known range (§8.1).** Initialized ticks are exactly the set bits of the *collected*
`tickBitmap` words (a word inside `bitmap_word_range` absent from the map is known zero; a
word outside it, or a `TickInfo` absent for a set bit, is unknown). Going down (token0 in)
from the current price with the active liquidity, initialized ticks `t <= tick` are
crossed in descending order (`L -= liquidity_net`) up to the first of: a set bit without
`TickInfo` (`missing_tick_data`: the exact swap cannot cross it), a crossing the exact
swap would revert on (`liquidity_revert`, `LiquidityMath.addDelta`), the collected bottom
`w_lo * 256 * spacing` (`collected_range`) or MIN_TICK (`min_tick`). Going up (token1 in)
ticks `tick < t` (`L += liquidity_net`) up to a missing tick, a revert,
`(w_hi * 256 + 255) * spacing` or MAX_TICK. A direction is **empty**
(`first_word_uncollected`) when the first word the exact swap reads lies outside
`[w_lo, w_hi]` on either side: going down the word of the compressed current tick, going
up the word of `compressed + 1` (`TickBitmap.nextInitializedTickWithinOneWord`). Nothing
beyond the known range is ever extrapolated; missing state is never zero liquidity.
Zero-liquidity pieces inside the known range are kept: crossing them costs no input and
yields no output, as in the exact swap loop.

**Index (prepared, immutable).** `build_cl_index(state)` produces, per direction, the
segment boundaries in sqrt-price space (`edges[0]` = current sqrt price), the per-segment
liquidity and the sequential prefix sums of the fee-free input and of the output of
traversing whole segments. The oracle then needs one binary search plus O(1) work per call
(`ClSide.to_price`); building costs O(W * 256 + T log T) for W non-zero collected words
and T initialized ticks and stores 4 * S + 2 floats per direction for S segments (never
proportional to the collected word *range*). This is the cost of one continuous market
oracle call only: a numeric solve still makes evaluations x |M| oracle calls, and the
exact per-step protocol swap (`pools.concentrated`, the only execution and money
authority) is not made logarithmic by it. The index holds a reference to (never a copy
of) the immutable snapshot state it was built from and `ClIndex.matches` binds it to that
exact state (pool id, price, tick, liquidity, bitmap and tick data); it carries no
case-, price- or solve-specific state, so one index may serve every case and every
objective evaluation of its bundle.

Numbers here are float64 continuous-model estimates, never money.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from operator import neg
from types import MappingProxyType
from typing import Any, Literal

from pools.cl_math import MAX_TICK, MIN_TICK, get_sqrt_ratio_at_tick
from pools.concentrated import SOURCES as CL_SOURCES
from pools.concentrated import ZERO_ADDRESS
from snapshot.models import ConcentratedPoolState, SnapshotBundle

CL = "concentrated"
Q96 = float(1 << 96)
FEE_DENOMINATOR = 1_000_000
UINT128_LIMIT = 1 << 128
WORD_BITS = (1 << 256) - 1  # bits the exact swap can ever read from one bitmap word

Boundary = Literal[
    "collected_range",
    "first_word_uncollected",
    "missing_tick_data",
    "liquidity_revert",
    "min_tick",
    "max_tick",
]


def cl_admitted(state: ConcentratedPoolState) -> bool:
    """A CL pool is a market iff `pools.concentrated` admits its source (the same rule as
    its `_source`: `uniswap_v3`, `agni_v3`, `fusionx_v3`; a non-zero `lm_pool` only for a
    source with the amount-free LM-pool hook) and its fee leaves a positive input share."""
    src = CL_SOURCES.get(state.source_key)
    if src is None or not 0 <= state.fee < FEE_DENOMINATOR:
        return False
    has_lm = state.lm_pool is not None and state.lm_pool.lower() != ZERO_ADDRESS
    return src.has_lm_pool_hook or not has_lm


@dataclass(frozen=True)
class ClSide:
    """One direction's known range. Segment k spans sqrt prices `edges[k] -> edges[k+1]`
    (descending when `zero_for_one`, ascending otherwise) with `liquidity[k]`; `net[k]` /
    `out[k]` are the fee-free input / output of traversing segments 0..k-1 whole (sequential
    float sums in traversal order; `net[0] = out[0] = 0`). `boundary` says why the known
    range ends and `boundary_tick` where (`None` for an empty `first_word_uncollected`)."""

    zero_for_one: bool
    edges: tuple[float, ...]
    liquidity: tuple[float, ...]
    net: tuple[float, ...]
    out: tuple[float, ...]
    boundary: Boundary
    boundary_tick: int | None

    @property
    def segments(self) -> int:
        return len(self.liquidity)

    def to_price(self, target: float) -> tuple[float, float]:
        """(fee-free input, output) of moving the price from the current sqrt price to
        `target` (strictly in this side's direction), clipped at the known boundary: one
        binary search over the edges plus the partial segment. The same float operations,
        in the same order, as a linear walk over the segments."""
        n = len(self.liquidity)
        if n == 0:
            return 0.0, 0.0
        edges = self.edges
        if self.zero_for_one:  # first segment whose end lies strictly below the target
            i = bisect_right(edges, -target, 1, n + 1, key=neg)
        else:  # first segment whose end lies strictly above the target
            i = bisect_right(edges, target, 1, n + 1)
        if i > n:  # the target is at or beyond the known boundary: drain it
            return self.net[n], self.out[n]
        k = i - 1
        liq, start = self.liquidity[k], edges[k]
        if self.zero_for_one:
            return (
                self.net[k] + liq * (1.0 / target - 1.0 / start),
                self.out[k] + liq * (start - target),
            )
        return (
            self.net[k] + liq * (target - start),
            self.out[k] + liq * (1.0 / start - 1.0 / target),
        )

    def forward(self, net_in: float) -> float | None:
        """Continuous output for a fee-free input `net_in` >= 0, or `None` when it would
        leave the known range (never extrapolated). Cancellation-free partial forms."""
        n = len(self.liquidity)
        if n == 0 or not 0.0 <= net_in <= self.net[n]:
            return None
        k = bisect_left(self.net, net_in, 1, n + 1) - 1
        liq, start, rest = self.liquidity[k], self.edges[k], net_in - self.net[k]
        if liq == 0.0 or rest <= 0.0:
            return self.out[k]
        if self.zero_for_one:  # L(sa - sb) = sa*sb*net with 1/sb = 1/sa + net/L
            nxt = 1.0 / (1.0 / start + rest / liq)
            return self.out[k] + start * nxt * rest
        nxt = start + rest / liq  # L(1/sa - 1/sb) = net/(sa*sb) with sb = sa + net/L
        return self.out[k] + rest / (start * nxt)


@dataclass(frozen=True, eq=False)
class ClIndex:
    """The prepared continuous aggregate of one admitted CL pool: its current sqrt price,
    fee factor gamma = 1 - fee/1e6 (the actual pool fee, charged once on the input) and
    the `down` (token0 -> token1) and `up` (token1 -> token0) known ranges. `state` is the
    snapshot object it was built from (a reference, never a copy); `bitmap_words` /
    `initialized_ticks` count the non-zero collected words scanned and the set bits found
    (the build work). Immutable and case-independent."""

    state: ConcentratedPoolState
    sqrt_price: float
    gamma: float
    down: ClSide
    up: ClSide
    bitmap_words: int
    initialized_ticks: int

    @property
    def pool_id(self) -> str:
        return self.state.pool_id

    def matches(self, state: ConcentratedPoolState) -> bool:
        """True iff `state` is the exact snapshot state this index describes (the same
        object, or one equal in every field: id, source, price, tick, liquidity, fee,
        bitmap words and tick data). A different pool or block never reuses an index."""
        return state is self.state or state == self.state

    def side(self, zero_for_one: bool) -> ClSide:
        return self.down if zero_for_one else self.up

    def forward(self, zero_for_one: bool, amount_in: float) -> float | None:
        """Continuous output for a gross input (fee included), `None` beyond the known
        range. A float estimate for comparisons, never an exact quote."""
        return self.side(zero_for_one).forward(amount_in * self.gamma)

    def stats(self) -> dict[str, Any]:
        """Structural size and build work (JSON-ready; floats stored = `float_entries`)."""
        sides = (self.down, self.up)
        return {
            "bitmap_words": self.bitmap_words,
            "initialized_ticks": self.initialized_ticks,
            "segments_down": self.down.segments,
            "segments_up": self.up.segments,
            "float_entries": sum(
                len(s.edges) + len(s.liquidity) + len(s.net) + len(s.out) for s in sides
            ),
            "down_boundary": self.down.boundary,
            "up_boundary": self.up.boundary,
        }


def _sqrt_at(tick: int) -> float:
    return get_sqrt_ratio_at_tick(tick) / Q96


def _initialized(state: ConcentratedPoolState) -> tuple[list[int], int]:
    """Every initialized tick the collected bitmap words declare (ascending) and the
    number of non-zero collected words scanned. Words outside `bitmap_word_range` are
    unknown and never read, whatever the map holds."""
    w_lo, w_hi = state.bitmap_word_range
    words = sorted(
        w for w, bits in state.tick_bitmap.items() if w_lo <= w <= w_hi and bits & WORD_BITS
    )
    ticks: list[int] = []
    for w in words:
        bits = state.tick_bitmap[w] & WORD_BITS
        while bits:
            low = bits & -bits
            ticks.append((w * 256 + low.bit_length() - 1) * state.tick_spacing)
            bits ^= low
    return ticks, len(words)


def _side(state: ConcentratedPoolState, init: list[int], cur: float, zero_for_one: bool) -> ClSide:
    s = state.tick_spacing
    w_lo, w_hi = state.bitmap_word_range
    compressed = state.tick // s
    first_word = compressed >> 8 if zero_for_one else (compressed + 1) >> 8
    if not w_lo <= first_word <= w_hi:
        return ClSide(zero_for_one, (cur,), (), (0.0,), (0.0,), "first_word_uncollected", None)
    boundary: Boundary
    if zero_for_one:
        stop = max(w_lo * 256 * s, MIN_TICK)
        boundary = "min_tick" if stop == MIN_TICK else "collected_range"
        crossed = reversed(init[bisect_left(init, stop) : bisect_right(init, state.tick)])
    else:
        stop = min((w_hi * 256 + 255) * s, MAX_TICK)
        boundary = "max_tick" if stop == MAX_TICK else "collected_range"
        crossed = iter(init[bisect_right(init, state.tick) : bisect_right(init, stop)])
    edges, liq = [cur], []
    liquidity = state.liquidity
    boundary_tick = stop
    ended = False
    for t in crossed:
        end = _sqrt_at(t)
        if (end < edges[-1]) if zero_for_one else (end > edges[-1]):
            edges.append(end)
            liq.append(float(liquidity))
        info = state.ticks.get(t)
        if info is None:
            boundary, boundary_tick, ended = "missing_tick_data", t, True
            break
        liquidity += -info.liquidity_net if zero_for_one else info.liquidity_net
        if not 0 <= liquidity < UINT128_LIMIT:
            boundary, boundary_tick, ended = "liquidity_revert", t, True
            break
    if not ended:
        end = _sqrt_at(stop)
        if (end < edges[-1]) if zero_for_one else (end > edges[-1]):
            edges.append(end)
            liq.append(float(liquidity))
    net, out = [0.0], [0.0]
    a = b = 0.0
    for k, lk in enumerate(liq):
        start, end = edges[k], edges[k + 1]
        if zero_for_one:
            a += lk * (1.0 / end - 1.0 / start)
            b += lk * (start - end)
        else:
            a += lk * (end - start)
            b += lk * (1.0 / start - 1.0 / end)
        net.append(a)
        out.append(b)
    return ClSide(
        zero_for_one, tuple(edges), tuple(liq), tuple(net), tuple(out), boundary, boundary_tick
    )


def build_cl_index(state: ConcentratedPoolState) -> ClIndex:
    """The prepared index of one admitted CL pool (see the module docstring). Raises
    `ValueError` for a pool outside the admitted CL sources."""
    if not cl_admitted(state):
        raise ValueError(
            f"{state.pool_id!r}: CL source {state.source_key!r} (fee {state.fee}, lm_pool "
            f"{state.lm_pool!r}) is outside the admitted cfmm_dual CL domain"
        )
    init, words = _initialized(state)
    cur = state.sqrt_price_x96 / Q96
    gamma = (FEE_DENOMINATOR - state.fee) / FEE_DENOMINATOR
    return ClIndex(
        state=state,
        sqrt_price=cur,
        gamma=gamma,
        down=_side(state, init, cur, True),
        up=_side(state, init, cur, False),
        bitmap_words=words,
        initialized_ticks=len(init),
    )


def prepare_cl_indexes(bundle: SnapshotBundle) -> Mapping[str, ClIndex]:
    """One index per admitted CL pool of `bundle` (bundle insertion order), read-only.
    Meant to run once in a charged preparation; nothing is cached across calls."""
    return MappingProxyType(
        {
            pid: build_cl_index(p)
            for pid, p in bundle.pools.items()
            if isinstance(p, ConcentratedPoolState) and cl_admitted(p)
        }
    )

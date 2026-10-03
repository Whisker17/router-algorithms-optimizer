"""WHI-1559 component A: the continuous V3 aggregate and prepared CL index of `cfmm_dual`
(`routing.cfmm.cl`, the CL oracle/universe/scales of `routing.cfmm.model`; `cfmm-dual.md`
§8, gates G-L1-G-L6). The strategy itself stays CPMM-only until component B.

Evidence classes are kept apart:

1. **Author reference** -- `tests/fixtures/cfmm/author_reference.json`, the pinned
   CFMMRouter.jl `UniV3` run (13 probes, offline, WHI-1557). Tolerance: the §8.3 figure,
   5e-14 relative.
2. **Research model** -- `tests/routing/cfmm_contract_model.py` (WHI-1557, sha256 pinned
   below): a linear walk over the ladder. The runtime index must give the same segments and
   the bitwise same oracle trades (same float operations in the same order); its forward
   curve within 1e-12 relative (prefix sums instead of sequential subtraction).
3. **Exact protocol replay** -- `pools.quote.quote_exact_in` on the pools' own snapshot
   states (`pools.concentrated`, per source): the only money authority. Continuous vs exact
   uses the stated tolerances below, never an integer certificate.
4. **Independent checks** -- optimality of the oracle trade on its own forward curve,
   structural counters taken by wrappers, the real SciPy solve under the shared budget.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import tracemalloc
from collections.abc import Callable, Iterator
from functools import cache
from pathlib import Path
from typing import Any

import cfmm_contract_model as m
import pytest

import routing.cfmm.cl as cl
import routing.cfmm.model as cm
import routing.cfmm.optimizer as co
from benchmark.objective import gross_only
from pools.cl_math import MAX_TICK, MIN_TICK, get_sqrt_ratio_at_tick
from pools.quote import quote_exact_in
from pools.result import QuoteStatus
from routing.cfmm.recovery import RecoveryOptions, recover
from routing.evaluator import EvalStatus, evaluate
from routing.search import QuoteCache
from snapshot.bundle import load_bundle
from snapshot.models import (
    BlockRef,
    Case,
    ConcentratedPoolState,
    ConstantProductPoolState,
    LiquidityBookPoolState,
    PoolState,
    SnapshotBundle,
    TickInfo,
)

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures"
INPUTS = json.loads((FIX / "cfmm" / "author_inputs.json").read_text())
AUTHOR = json.loads((FIX / "cfmm" / "author_reference.json").read_text())
MODEL = json.loads((FIX / "cfmm" / "model_reference.json").read_text())
SETTINGS = co.SolverSettings.from_options(MODEL["preset"])
RESEARCH_MODEL_SHA256 = "7fb979394cbcba243058fe38ebdc37b56602735ab3887475c6386851006ac387"
AUTHOR_PIN = "5932e42e5077ffc7d8e02c3b3ad2e9ed1d441267"
AUTHOR_TOL = 5e-14  # cfmm-dual.md §8.3
BUNDLES = {
    "mantle_mixed": FIX / "routing" / "mantle_mixed",
    "uniswap_v3": FIX / "uniswap_v3" / "bundle",
    "agni_v3": FIX / "agni" / "bundle",
    "fusionx_v3": FIX / "fusionx" / "bundle",
}
BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)
CL_STAGE = (cm.CPMM, cl.CL)
LM = "0x" + "11" * 20


@cache
def bundle(name: str) -> SnapshotBundle:
    return load_bundle(BUNDLES[name])


def rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(a), abs(b), 1e-300)


def synthetic(**over: Any) -> ConcentratedPoolState:
    """The WHI-1557 two-position fixture (the author inputs were produced from it):
    [-1800, -1200) L 4e15 and [-600, 1200) L 1e16, current tick 100, spacing 60, words
    (-1, 0); ticks -1200..-600 are an empty range inside the known range."""
    return dataclasses.replace(m.synthetic_cl(), **over)


def real_cl_pools() -> Iterator[tuple[str, ConcentratedPoolState]]:
    for name in ("uniswap_v3", "agni_v3", "fusionx_v3"):
        for pid, p in bundle(name).pools.items():
            if isinstance(p, ConcentratedPoolState):
                yield f"{name}:{pid[:10]}", p


def word_grid() -> Iterator[tuple[str, ConcentratedPoolState]]:
    """Current ticks at word edges (bit 0 / bit 255) against word ranges around them."""
    for tick in (-15360, -60, 0, 100, 15240, 15300, 15359):
        for words in ((-1, 0), (0, 0), (-1, -1), (1, 1), (-2, -2), (0, 1), (-2, -1)):
            yield (
                f"grid:{tick}:{words}",
                synthetic(
                    tick=tick,
                    sqrt_price_x96=get_sqrt_ratio_at_tick(tick) + 1,
                    bitmap_word_range=words,
                ),
            )


def all_states() -> list[tuple[str, ConcentratedPoolState]]:
    return [
        ("synthetic", synthetic()),
        ("synthetic_missing", m.synthetic_cl(missing_tick_data=True)),
        *real_cl_pools(),
        *word_grid(),
    ]


def vector(trade: cm.Trade | None, token0: str) -> list[float]:
    """[delta0, delta1, lambda0, lambda1] in the author's layout."""
    out = [0.0, 0.0, 0.0, 0.0]
    if trade is not None:
        i = 0 if trade.token_in == token0 else 1
        out[i], out[3 - i] = trade.amount_in, trade.amount_out
    return out


def author_states() -> dict[str, ConcentratedPoolState]:
    real = bundle("mantle_mixed").pools["0x4cdfc22bf05209de87ee564746dc7e5174631d2b"]
    assert isinstance(real, ConcentratedPoolState)
    return {
        "synthetic": m.synthetic_cl(),
        "synthetic_missing_tick": m.synthetic_cl(missing_tick_data=True),
        "real_uniswap_v3_usdt_wmnt": real,
    }


def bundle_of(*pools: PoolState, case: Case) -> SnapshotBundle:
    return SnapshotBundle(
        "x", "synthetic", 1, BLOCK, {p.pool_id: p for p in pools}, (case,), "h", "<t>"
    )


# ------------------------------------------------------------------ 1. pins


def test_port_source_pins() -> None:
    """The runtime port names the research model bytes it was ported from and the author
    pin; the NOTICE records both and the CL attribution."""
    research = (ROOT / "tests" / "routing" / "cfmm_contract_model.py").read_bytes()
    assert hashlib.sha256(research).hexdigest() == RESEARCH_MODEL_SHA256
    source = (ROOT / "routing" / "cfmm" / "cl.py").read_text()
    notice = (ROOT / "routing" / "cfmm" / "NOTICE.md").read_text()
    for text in (source, notice):
        assert RESEARCH_MODEL_SHA256 in text and AUTHOR_PIN in text and "UniV3" in text
    assert AUTHOR["provenance"]["cfmmrouter"]["git_revision"] == AUTHOR_PIN


@pytest.mark.parametrize(
    "name", ["synthetic", "synthetic_missing_tick", "real_uniswap_v3_usdt_wmnt"]
)
def test_author_inputs_are_the_runtime_index_in_author_terms(name: str) -> None:
    """§8.3 mapping: the committed author `UniV3` arguments (boundary prices from the top
    of the known range down, L**2 per interval plus a final 0, gamma, current price) are
    exactly what the runtime index of the same snapshot state describes."""
    index = cl.build_cl_index(author_states()[name])
    up, down = index.up, index.down
    bounds = list(reversed(up.edges[1:])) + list(down.edges[1:])
    liq = list(reversed(up.liquidity[1:])) + [up.liquidity[0]] + list(down.liquidity[1:])
    assert up.liquidity[0] == down.liquidity[0]  # the interval holding the current price
    probes = [c for c in INPUTS["univ3_oracle"] if c["state"] == name]
    assert probes
    for c in probes:
        assert c["current_price"] == index.sqrt_price**2 and c["gamma"] == index.gamma
        assert c["lower_ticks"] == [b * b for b in bounds]
        assert c["liquidity"] == [x * x for x in liq] + [0.0]


# ------------------------------------------------------------------ 2. G-L1 author oracle


@pytest.mark.parametrize("case", INPUTS["univ3_oracle"], ids=lambda c: c["id"])
def test_cl_oracle_matches_the_pinned_author_univ3_run(case: dict[str, Any]) -> None:
    state = author_states()[case["state"]]
    index = cl.build_cl_index(state)
    trade = cm.cl_arb(index, {state.token0: case["v"][0], state.token1: case["v"][1]})
    ref = next(c for c in AUTHOR["univ3_oracle"] if c["id"] == case["id"])
    for got, want in zip(vector(trade, state.token0), ref["delta"] + ref["lambda"], strict=True):
        assert (got == want == 0) or rel(got, want) < AUTHOR_TOL
    assert (trade is None) == case["id"].endswith("-band")  # the no-trade band


def test_all_thirteen_author_probes_are_covered() -> None:
    assert len(INPUTS["univ3_oracle"]) == len(AUTHOR["univ3_oracle"]) == 13


# ------------------------------------------------------------------ 3. research model


@pytest.mark.parametrize(
    ("name", "state"), all_states(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_index_is_the_research_ladder(name: str, state: ConcentratedPoolState) -> None:
    index = cl.build_cl_index(state)
    ladder = m.cl_ladder(state)
    for side, segs, label in (
        (index.down, ladder.down, ladder.down_boundary),
        (index.up, ladder.up, ladder.up_boundary),
    ):
        assert side.edges == (ladder.sqrt_price, *(s.end for s in segs))
        assert side.liquidity == tuple(s.liquidity for s in segs)
        assert all(s.start == e for s, e in zip(segs, side.edges, strict=False))
        if side.boundary == "first_word_uncollected":  # the model labels it collected_range
            assert (segs, label, side.boundary_tick) == ((), "collected_range", None)
        else:
            assert side.boundary == label  # real states never cross a reverting tick
    assert index.gamma == ladder.gamma and index.sqrt_price == ladder.sqrt_price


FACTORS = (
    1e-6,
    0.01,
    0.5,
    0.84,
    0.97,
    0.99,
    0.999,
    0.9996,
    1.0,
    1.0004,
    1.001,
    1.01,
    1.03,
    1.3,
    100.0,
    1e6,
)


@pytest.mark.parametrize(
    ("name", "state"), all_states(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_oracle_equals_the_research_oracle_bitwise(name: str, state: ConcentratedPoolState) -> None:
    index = cl.build_cl_index(state)
    ladder = m.cl_ladder(state)
    p = index.sqrt_price**2
    for f in FACTORS:
        nu = {state.token0: f * p, state.token1: 1.0}
        for allowed in (None, state.token0, state.token1):
            ours, theirs = cm.cl_arb(index, nu, allowed), m.cl_arb(state, ladder, nu, allowed)
            assert (ours is None) == (theirs is None), (f, allowed)
            if ours is not None and theirs is not None:
                assert (ours.token_in, ours.amount_in, ours.amount_out) == (
                    theirs.token_in,
                    theirs.amount_in,
                    theirs.amount_out,
                ), (f, allowed)


@pytest.mark.parametrize(
    ("name", "state"), all_states(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_forward_curve_matches_the_research_forward(
    name: str, state: ConcentratedPoolState
) -> None:
    index = cl.build_cl_index(state)
    ladder = m.cl_ladder(state)
    for zero_for_one in (True, False):
        cap = index.side(zero_for_one).net[-1] / index.gamma
        for amount in (1.0, 1e3, 1e9, 1e15, 0.1 * cap, 0.5 * cap, 0.999 * cap, 1.001 * cap + 1):
            ours, theirs = (
                index.forward(zero_for_one, amount),
                m.cl_forward(ladder, zero_for_one, amount),
            )
            assert (ours is None) == (theirs is None), (zero_for_one, amount)
            if ours is not None and theirs is not None:
                assert ours == theirs == 0.0 or rel(ours, theirs) < 1e-12


# ------------------------------------------------------------------ 4. independent optimality


@pytest.mark.parametrize(
    ("name", "state"),
    [
        ("synthetic", synthetic()),
        ("synthetic_missing", m.synthetic_cl(missing_tick_data=True)),
        *real_cl_pools(),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_oracle_trade_is_the_optimal_arbitrage_of_its_forward_curve(
    name: str, state: ConcentratedPoolState
) -> None:
    """Independent of how the oracle is computed: its trade maximizes
    nu_out * F(x) - nu_in * x over the known range (F = the gross-input forward curve), and
    its output is F(its input) (1e-9 relative or 1e-3 raw units). A no-trade answer means
    a small trade loses value."""
    index = cl.build_cl_index(state)
    p = index.sqrt_price**2
    for f in (0.2, 0.9, 0.99, 0.9990, 1.0, 1.0010, 1.01, 1.1, 5.0):
        nu = {state.token0: f * p, state.token1: 1.0}
        trade = cm.cl_arb(index, nu)
        for zero_for_one in (True, False):
            tin, tout = (
                (state.token0, state.token1) if zero_for_one else (state.token1, state.token0)
            )

            def value(
                x: float,
                tin: str = tin,
                tout: str = tout,
                z: bool = zero_for_one,
                nu: dict[str, float] = nu,
            ) -> float | None:
                y = index.forward(z, x)
                return None if y is None else nu[tout] * y - nu[tin] * x

            cap = index.side(zero_for_one).net[-1] / index.gamma
            if trade is None or trade.token_in != tin:
                if cap > 0:  # a small trade in this direction does not gain
                    eps = 1e-6 * cap
                    small = value(eps)
                    assert small is None or small <= 1e-9 * nu[tin] * eps, (f, zero_for_one)
                continue
            x = trade.amount_in
            y = index.forward(zero_for_one, x)
            if y is None:  # a drained range: x*gamma may round past the boundary
                y = index.forward(zero_for_one, x * (1 - 1e-12))
            # 1e-9 relative, or 1e-3 raw units: the oracle's L*(1/sa - 1/sb) cancels on
            # sub-unit trades (never money; only exact quotes decide amounts)
            assert y is not None and abs(y - trade.amount_out) <= 1e-9 * y + 1e-3
            best = value(x * (1 - 1e-12))
            assert best is not None and best > 0
            for step in (0.999, 0.99, 0.5, 1.001, 1.01):
                other = value(x * step)
                if other is not None:
                    assert other <= best + 1e-9 * nu[tin] * x, (f, step)


# ------------------------------------------------------------------ 5. G-L2 known range


def test_current_word_outside_the_collected_range_on_either_side_is_empty() -> None:
    """Parent-found WHI-1557 bug (comment 2104a98d): tick 100 (spacing 60) lies in word 0.
    Words (1, 1): going up reads word 0 -> unknown; (-2, -2): going down reads word 0 ->
    unknown. Both directions of both states are checked against the exact swap."""
    base = synthetic(tick_bitmap={}, ticks={})
    above = dataclasses.replace(base, bitmap_word_range=(1, 1))
    below = dataclasses.replace(base, bitmap_word_range=(-2, -2))
    for state in (above, below):
        index = cl.build_cl_index(state)
        for zero_for_one, token in ((True, "T0"), (False, "T1")):
            side = index.side(zero_for_one)
            assert (side.segments, side.boundary) == (0, "first_word_uncollected")
            assert index.forward(zero_for_one, 1e6) is None
            exact = quote_exact_in(state, token, 10**6)
            assert exact.status is QuoteStatus.INCOMPLETE_SNAPSHOT
        for nu in ({"T0": 0.5, "T1": 1.0}, {"T0": 2.0, "T1": 1.0}):
            assert cm.cl_arb(index, nu) is None  # never the active liquidity extrapolated


@pytest.mark.parametrize(
    ("name", "state"), list(word_grid()), ids=lambda v: v if isinstance(v, str) else ""
)
def test_direction_is_known_iff_the_exact_swap_can_start(
    name: str, state: ConcentratedPoolState
) -> None:
    """zeroForOne reads the compressed tick's word, oneForZero the word of compressed + 1;
    a direction is known exactly when the exact swap of a price-moving input does not fail
    with incomplete_snapshot, and then exact <= continuous."""
    index = cl.build_cl_index(state)
    for zero_for_one, token in ((True, "T0"), (False, "T1")):
        compressed = state.tick // state.tick_spacing
        word = compressed >> 8 if zero_for_one else (compressed + 1) >> 8
        lo, hi = state.bitmap_word_range
        side = index.side(zero_for_one)
        assert (side.boundary == "first_word_uncollected") == (not lo <= word <= hi)
        exact = quote_exact_in(state, token, 10**6)
        model = index.forward(zero_for_one, 1e6)
        assert (exact.status is QuoteStatus.INCOMPLETE_SNAPSHOT) == (model is None), exact
        if model is not None:
            assert exact.status is QuoteStatus.OK and exact.amount_out <= model + 1


def test_word_edge_regressions() -> None:
    """Bit 255 of the top word: 'up' reads the next (uncollected) word -> empty, 'down'
    known. Bit 0 of the bottom word: 'down' reads that word (known) and stops at its first
    tick; one tick lower (previous word) 'down' is empty and 'up' known."""
    top = 255 * 60  # word 0, bit 255 at spacing 60
    at_top = synthetic(
        tick=top, sqrt_price_x96=get_sqrt_ratio_at_tick(top) + 1, bitmap_word_range=(0, 0)
    )
    idx = cl.build_cl_index(at_top)
    assert idx.up.boundary == "first_word_uncollected" and idx.down.segments > 0
    at_bottom = synthetic(
        tick=30, sqrt_price_x96=get_sqrt_ratio_at_tick(30), bitmap_word_range=(0, 0)
    )
    idx = cl.build_cl_index(at_bottom)  # compressed 0: word 0, bit 0
    assert idx.down.segments == 1 and idx.down.boundary_tick == 0 and idx.up.segments > 0
    below = synthetic(
        tick=-30, sqrt_price_x96=get_sqrt_ratio_at_tick(-30), bitmap_word_range=(0, 0)
    )
    idx = cl.build_cl_index(below)
    assert idx.down.boundary == "first_word_uncollected" and idx.up.segments > 0
    for state, token, ok in ((at_top, "T1", False), (at_bottom, "T0", True), (below, "T0", False)):
        status = quote_exact_in(state, token, 10**6).status
        assert (status is QuoteStatus.OK) == ok, (token, status)


def test_missing_tick_info_ends_the_direction_and_is_never_zero_liquidity() -> None:
    missing = m.synthetic_cl(missing_tick_data=True)  # tick -1800 is set but has no TickInfo
    idx = cl.build_cl_index(missing)
    assert (idx.down.boundary, idx.down.boundary_tick) == ("missing_tick_data", -1800)
    assert idx.down.liquidity == (1e16, 0.0, 4e15)  # nothing below -1800
    assert idx.down.edges[-1] == get_sqrt_ratio_at_tick(-1800) / cl.Q96
    full = cl.build_cl_index(synthetic())
    assert idx.up == full.up  # the other direction is unaffected
    cap = idx.down.net[-1] / idx.gamma
    assert idx.forward(True, cap * 1.01) is None
    assert quote_exact_in(missing, "T0", int(cap * 1.01)).status is QuoteStatus.INCOMPLETE_SNAPSHOT
    within = quote_exact_in(missing, "T0", int(cap * 0.99))
    assert within.status is QuoteStatus.OK
    model = idx.forward(True, float(int(cap * 0.99)))
    assert model is not None and within.amount_out <= model + 1
    # the current price sits exactly on an initialized tick without TickInfo: nothing down
    ticks = dict(synthetic().ticks)
    del ticks[-600]
    on_tick = synthetic(tick=-600, sqrt_price_x96=get_sqrt_ratio_at_tick(-600), ticks=ticks)
    idx = cl.build_cl_index(on_tick)
    assert (idx.down.segments, idx.down.boundary) == (0, "missing_tick_data")
    assert quote_exact_in(on_tick, "T0", 10**6).status is QuoteStatus.INCOMPLETE_SNAPSHOT
    assert cm.cl_arb(idx, {"T0": 0.5 * idx.sqrt_price**2, "T1": 1.0}) is None


@pytest.mark.parametrize("tick_offset", [0, -1])
def test_initial_price_on_an_initialized_tick(tick_offset: int) -> None:
    """Price exactly at tick -600 with `tick` -600 (crossing down is the first step) or
    -601 (the state after a downward cross: -600 is crossed first going up)."""
    liquidity = 10**16 if tick_offset == 0 else 0  # after a downward cross L is 0
    state = synthetic(
        tick=-600 + tick_offset, sqrt_price_x96=get_sqrt_ratio_at_tick(-600), liquidity=liquidity
    )
    idx = cl.build_cl_index(state)
    # both representations of the same price: down crosses into the empty range at once,
    # up starts in the [-600, 1200) position
    assert idx.down.liquidity == (0.0, 4e15, 0.0) and idx.up.liquidity == (1e16, 0.0)
    assert idx.down.edges[0] == idx.up.edges[0] == get_sqrt_ratio_at_tick(-600) / cl.Q96
    for zero_for_one, token in ((True, "T0"), (False, "T1")):
        for amount in (10**6, 10**13, 3 * 10**14):
            exact = quote_exact_in(state, token, amount)
            model = idx.forward(zero_for_one, float(amount))
            if model is None:
                assert exact.status is not QuoteStatus.OK
                continue
            assert exact.status is QuoteStatus.OK
            assert exact.amount_out <= model * (1 + 1e-12) + 1
            assert exact.amount_out >= model * (1 - 1e-9) - 2


def test_min_and_max_tick_bound_the_known_range() -> None:
    """All words collected (spacing 60: words -58..57 cover MIN/MAX_TICK), one position:
    both directions end at the protocol tick limits, and an input beyond them is real
    liquidity exhaustion (insufficient_liquidity), not missing state."""
    words = ((MIN_TICK // 60) >> 8, (MAX_TICK // 60) >> 8)
    state = synthetic(bitmap_word_range=words)
    idx = cl.build_cl_index(state)
    assert (idx.down.boundary, idx.down.boundary_tick) == ("min_tick", MIN_TICK)
    assert (idx.up.boundary, idx.up.boundary_tick) == ("max_tick", MAX_TICK)
    assert idx.down.edges[-1] == get_sqrt_ratio_at_tick(MIN_TICK) / cl.Q96
    assert idx.up.edges[-1] == get_sqrt_ratio_at_tick(MAX_TICK) / cl.Q96
    # zero liquidity beyond both positions: a large input drains the positions and then
    # traverses empty ranges for free, yet stays bounded by the protocol limits
    for zero_for_one, token in ((True, "T0"), (False, "T1")):
        side = idx.side(zero_for_one)
        assert side.liquidity[-1] == 0.0 and side.net[-1] == side.net[-2]
        exact = quote_exact_in(state, token, int(side.net[-1] / idx.gamma * 2) + 10**6)
        assert exact.status is QuoteStatus.INSUFFICIENT_LIQUIDITY
        assert idx.forward(zero_for_one, side.net[-1] / idx.gamma * 2 + 1e6) is None
    lone = synthetic(bitmap_word_range=words, tick_bitmap={}, ticks={})
    idx = cl.build_cl_index(lone)  # no initialized tick at all: one segment to each limit
    assert (idx.down.segments, idx.up.segments) == (1, 1)
    exact = quote_exact_in(lone, "T0", 10**30)
    model = idx.forward(True, 1e30)
    assert exact.status is QuoteStatus.OK and model is not None
    assert model * (1 - 1e-9) - 2 <= exact.amount_out <= model * (1 + 1e-12) + 1


def test_empty_range_inside_the_known_range_is_traversed_at_zero_cost() -> None:
    idx = cl.build_cl_index(synthetic())
    down = idx.down
    assert down.liquidity == (1e16, 0.0, 4e15, 0.0)  # two positions, empty range between
    k = down.liquidity.index(0.0)
    assert down.net[k + 1] == down.net[k] and down.out[k + 1] == down.out[k]
    inside = (down.edges[k] + down.edges[k + 1]) / 2
    assert down.to_price(inside) == (down.net[k], down.out[k])  # no input, no output
    at_start, at_end = down.forward(down.net[k]), down.forward(down.net[k + 1])
    assert at_start == at_end and at_start is not None and rel(at_start, down.out[k]) < 1e-12
    # crossing the empty range into the second position: exact replay agrees
    amount = int((down.net[k] + 0.5 * (down.net[k + 2] - down.net[k])) / idx.gamma)
    exact = quote_exact_in(synthetic(), "T0", amount)
    model = idx.forward(True, float(amount))
    assert exact.status is QuoteStatus.OK and model is not None
    assert model * (1 - 1e-9) - 2 <= exact.amount_out <= model * (1 + 1e-12) + 1
    assert exact.features["initialized_ticks_crossed"] == 2  # -600 and -1200


def test_a_crossing_the_exact_swap_reverts_on_ends_the_direction() -> None:
    """liquidity_net of -1200 inconsistent (L would go negative): the known range stops
    at that tick; the exact swap reverts when it gets there (LiquidityMath 'LS')."""
    ticks = dict(synthetic().ticks)
    ticks[-1200] = TickInfo(4 * 10**15, 10**17, 0, 0)  # crossing down: L = 0 - 1e17 < 0
    state = synthetic(ticks=ticks)
    idx = cl.build_cl_index(state)
    assert (idx.down.boundary, idx.down.boundary_tick) == ("liquidity_revert", -1200)
    assert idx.down.liquidity == (1e16, 0.0)
    cap = idx.down.net[-1] / idx.gamma
    assert idx.forward(True, cap * 1.5) is None
    assert quote_exact_in(state, "T0", int(cap * 1.5)).status is QuoteStatus.REVERTED
    assert quote_exact_in(state, "T0", int(cap * 0.5)).status is QuoteStatus.OK


def test_only_collected_words_and_their_set_bits_define_initialized_ticks() -> None:
    """TickInfo of a tick whose bitmap bit is clear, and bits of a word outside the
    collected range, are never used (the exact swap never reads them)."""
    base = synthetic()
    ticks = {**base.ticks, 600: TickInfo(10**15, 10**15, 0, 0)}
    bitmap = {**base.tick_bitmap, 5: 1, -7: 1 << 3}
    noisy = synthetic(ticks=ticks, tick_bitmap=bitmap)
    a, b = cl.build_cl_index(base), cl.build_cl_index(noisy)
    assert (a.down, a.up, a.initialized_ticks, a.bitmap_words) == (
        b.down,
        b.up,
        b.initialized_ticks,
        b.bitmap_words,
    )
    for token in ("T0", "T1"):
        a_q, b_q = quote_exact_in(base, token, 10**14), quote_exact_in(noisy, token, 10**14)
        assert (a_q.status, a_q.amount_out) == (b_q.status, b_q.amount_out)


# ------------------------------------------------------------------ 6. G-L3/G-L4 per source


def source_state(source: str) -> ConcentratedPoolState:
    """The synthetic positions under each admitted source's own exact semantics: a
    non-zero protocol fee in the source's encoding and, for Agni/FusionX, a live LM hook."""
    fee_protocol = 4 | (5 << 4) if source == "uniswap_v3" else 3300 | (3300 << 16)
    return synthetic(
        pool_id=f"synth_{source}",
        source_key=source,
        fee_protocol=fee_protocol,
        lm_pool=None if source == "uniswap_v3" else LM,
    )


@pytest.mark.parametrize("source", ["uniswap_v3", "agni_v3", "fusionx_v3"])
@pytest.mark.parametrize("zero_for_one", [True, False])
def test_exact_replay_never_exceeds_the_continuous_aggregate(
    source: str, zero_for_one: bool
) -> None:
    """G-L3/G-L4 on the registered synthetic fixture: inside the known range exact <=
    continuous and within 1e-9 relative + 2 raw units (the WHI-1557 tolerance); beyond it
    the exact swap is incomplete_snapshot and the model refuses to extrapolate. The protocol
    fee and the LM hook change no amount, so the continuous model is source-independent."""
    state = source_state(source)
    assert cl.cl_admitted(state)
    idx = cl.build_cl_index(state)
    assert idx.down == cl.build_cl_index(synthetic()).down  # same continuous model
    token = state.token0 if zero_for_one else state.token1
    hooked = 0
    for amount in (1, 7, 10**3, 10**6, 10**12, 3 * 10**14, 4 * 10**14, 9 * 10**14):
        cont = idx.forward(zero_for_one, float(amount))
        exact = quote_exact_in(state, token, amount)
        if cont is None:
            assert exact.status is QuoteStatus.INCOMPLETE_SNAPSHOT
            continue
        assert exact.status is QuoteStatus.OK
        assert exact.amount_out <= cont * (1 + 1e-12) + 1
        assert exact.amount_out >= cont * (1 - 1e-9) - 2
        hooked += exact.features["lm_pool_hook_calls"]
    assert (hooked > 0) == (source != "uniswap_v3")
    missing = dataclasses.replace(
        m.synthetic_cl(missing_tick_data=True), source_key=source, lm_pool=state.lm_pool
    )
    assert cl.build_cl_index(missing).forward(True, 7e14) is None
    assert quote_exact_in(missing, "T0", 7 * 10**14).status is QuoteStatus.INCOMPLETE_SNAPSHOT


@pytest.mark.parametrize(
    ("name", "state"), list(real_cl_pools()), ids=lambda v: v if isinstance(v, str) else ""
)
def test_real_frozen_states_exact_versus_continuous(
    name: str, state: ConcentratedPoolState
) -> None:
    """Every frozen Uniswap v3 / Agni / FusionX state, both directions, tiny to beyond-range
    inputs. Stated tolerance (per exact step: input rounded up twice -- amount and fee --
    and output rounded down once; float model error 1e-9 relative on narrow segments):
    F(a) (1 + 1e-12) + 1 >= exact(a) >= F(a - 2 * steps) (1 - 1e-9) - steps - 1, with
    `steps` the exact swap's own step count. Beyond the known range the model returns
    none and the exact quote is never ok. 1e-9 relative alone does NOT hold on real
    states (dust fee rounding, hundreds of tick steps)."""
    idx = cl.build_cl_index(state)
    for zero_for_one in (True, False):
        token = state.token0 if zero_for_one else state.token1
        cap = idx.side(zero_for_one).net[-1] / idx.gamma
        for amount in (
            1,
            10**3,
            10**6,
            10**9,
            10**12,
            10**15,
            10**18,
            10**21,
            int(cap * 0.5),
            int(cap * 0.999),
            int(cap * 1.001) + 1,
        ):
            if amount <= 0:
                continue
            cont = idx.forward(zero_for_one, float(amount))
            exact = quote_exact_in(state, token, amount)
            if cont is None:
                assert exact.status is not QuoteStatus.OK
                continue
            assert exact.status is QuoteStatus.OK, (amount, exact.detail)
            steps = exact.features["swap_steps"]
            low = idx.forward(zero_for_one, float(max(amount - 2 * steps, 0))) or 0.0
            assert exact.amount_out <= cont * (1 + 1e-12) + 1
            assert exact.amount_out >= low * (1 - 1e-9) - steps - 1


def test_source_admission_is_the_exact_quote_semantics() -> None:
    """Admitted iff `pools.concentrated` executes the state (not `unsupported`) with a fee
    below 100%: Uniswap v3 without an LM pool, Agni/FusionX with or without their no-op LM
    hook; unknown sources and undeclared hooks are outside the CL domain."""
    variants = {
        "uniswap_v3": (synthetic(), True),
        "uniswap_v3_zero_lm": (synthetic(lm_pool="0x" + "00" * 20), True),
        "uniswap_v3_with_lm": (synthetic(lm_pool=LM), False),
        "agni_v3_lm": (source_state("agni_v3"), True),
        "agni_v3_no_lm": (synthetic(source_key="agni_v3"), True),
        "fusionx_v3_lm": (source_state("fusionx_v3"), True),
        "fusionx_v3_no_lm": (synthetic(source_key="fusionx_v3"), True),
        "pancake_v3": (synthetic(source_key="pancake_v3"), False),
    }
    for name, (state, expected) in variants.items():
        assert cl.cl_admitted(state) is expected, name
        status = quote_exact_in(state, "T0", 10**9).status
        assert (status is not QuoteStatus.UNSUPPORTED) is expected, (name, status)
        if not expected:
            with pytest.raises(ValueError, match="outside the admitted cfmm_dual CL domain"):
                cl.build_cl_index(state)
    assert not cl.cl_admitted(synthetic(fee=1_000_000))  # no input share left
    assert cl.cl_admitted(synthetic(fee=999_999))
    lm_pool = bundle("fusionx_v3").pools["0x262255f4770aebe2d0c8b97a46287dcecc2a0aff"]
    assert isinstance(lm_pool, ConcentratedPoolState) and lm_pool.lm_pool not in (
        None,
        "0x" + "00" * 20,
    )
    assert cl.cl_admitted(lm_pool)  # the real FusionX pool with a live LM hook


def test_liquidity_book_never_enters_a_cl_stage_market_set() -> None:
    b = bundle("mantle_mixed")
    lb = [pid for pid, p in b.pools.items() if isinstance(p, LiquidityBookPoolState)]
    assert lb
    kinds: set[type] = set()
    for case in b.cases:
        markets = cm.market_universe(b, case, 3, CL_STAGE)
        kinds.update(type(b.pools[p]) for p in markets)
        assert not set(lb) & set(markets)
    assert kinds == {ConstantProductPoolState, ConcentratedPoolState}
    assert not any(cm.admitted(b.pools[p], (*CL_STAGE, "liquidity_book")) for p in lb)
    with pytest.raises(ValueError):
        cm.market_universe(b, b.cases[0], 3, ("liquidity_book",))
    with pytest.raises(ValueError, match="not an admitted CPMM market"):
        cm.dual_problem(b, b.cases[0], [lb[0]], cl.prepare_cl_indexes(b))


# ------------------------------------------------------------------ 7. problem API, CP compat


def test_cpmm_stage_defaults_are_unchanged() -> None:
    """No CL activation: without `protocols` the universe is the CPMM stage, and without
    `cl_indexes` a CL pool is refused exactly as before; a CPMM-only problem is identical
    whether or not an index mapping is passed."""
    b = bundle("mantle_mixed")
    idx = cl.prepare_cl_indexes(b)
    for case in b.cases:
        cp = cm.market_universe(b, case, 3)
        assert cp == cm.market_universe(b, case, 3, (cm.CPMM,))
        assert all(isinstance(b.pools[p], ConstantProductPoolState) for p in cp)
        plain, with_idx = cm.dual_problem(b, case, cp), cm.dual_problem(b, case, cp, idx)
        assert plain == with_idx and dict(plain.cl) == {}
        sigma = cm.scales(plain)
        for x in ([0.0] * len(plain.variables), [0.3] * len(plain.variables)):
            assert cm.log_objective(plain, sigma, x)[:2] == cm.log_objective(with_idx, sigma, x)[:2]
        mixed = cm.market_universe(b, case, 3, CL_STAGE)
        cl_ids = [p for p in mixed if isinstance(b.pools[p], ConcentratedPoolState)]
        assert cl_ids
        with pytest.raises(ValueError, match="not an admitted CPMM market"):
            cm.dual_problem(b, case, mixed)


@pytest.mark.parametrize("name", list(BUNDLES))
def test_mixed_problem_equals_the_research_model(name: str) -> None:
    """Universe (CPMM + admitted CL, simple_path_union), scales and the dual function with
    trades and gradient equal the WHI-1557 model exactly on every case of the bundle."""
    b = bundle(name)
    idx = cl.prepare_cl_indexes(b)
    for case in b.cases:
        markets = cm.market_universe(b, case, 3, CL_STAGE)
        assert markets == m.market_universe(b, case, 3, [m.CPMM, m.CL])
        ours, theirs = cm.dual_problem(b, case, markets, idx), m.dual_problem(b, case, markets)
        assert ours.variables == theirs.variables
        sigma = cm.scales(ours)
        assert dict(sigma) == m.scales(theirs)
        for shift in (0.0, 0.01, -0.02, 0.5):
            x = [shift * (i + 1) for i in range(len(ours.variables))]
            a, bb = cm.log_objective(ours, sigma, x), m.log_objective(theirs, m.scales(theirs), x)
            assert a[0] == bb[0] and a[1] == bb[1]
            assert [(t.pool_id, t.amount_in, t.amount_out) for t in a[2].trades] == [
                (t.pool_id, t.amount_in, t.amount_out) for t in bb[2].trades
            ]
            assert a[2].oracle_calls == len(markets)


def test_prepared_index_must_be_supplied_and_bound_to_the_exact_state() -> None:
    b = bundle("mantle_mixed")
    case = b.case("usdt_wmnt_evidence_split")
    markets = cm.market_universe(b, case, 3, CL_STAGE)
    idx = dict(cl.prepare_cl_indexes(b))
    cl_ids = [p for p in markets if isinstance(b.pools[p], ConcentratedPoolState)]
    assert len(cl_ids) == 2
    with pytest.raises(ValueError, match="no prepared CL index"):
        cm.dual_problem(b, case, markets, {cl_ids[0]: idx[cl_ids[0]]})
    swapped = {**idx, cl_ids[0]: idx[cl_ids[1]]}  # another pool's index
    with pytest.raises(ValueError, match="not of this snapshot state"):
        cm.dual_problem(b, case, markets, swapped)
    pool = b.pools[cl_ids[0]]
    assert isinstance(pool, ConcentratedPoolState)
    changes: list[dict[str, Any]] = [
        {"liquidity": pool.liquidity + 1},
        {"tick": pool.tick + 1},
        {"sqrt_price_x96": pool.sqrt_price_x96 + 1},
        {"ticks": {}},
    ]
    for change in changes:
        stale = {**idx, cl_ids[0]: cl.build_cl_index(dataclasses.replace(pool, **change))}
        with pytest.raises(ValueError, match="not of this snapshot state"):
            cm.dual_problem(b, case, markets, stale)
    equal_copy = {**idx, cl_ids[0]: cl.build_cl_index(dataclasses.replace(pool))}
    problem = cm.dual_problem(b, case, markets, equal_copy)  # equal state, other object
    assert problem.cl[cl_ids[0]] is equal_copy[cl_ids[0]]
    unadmitted = dataclasses.replace(pool, lm_pool=LM)  # Uniswap v3 with an LM pool
    other = dataclasses.replace(b, pools={**b.pools, pool.pool_id: unadmitted})
    with pytest.raises(ValueError, match="not an admitted CL market"):
        cm.dual_problem(other, case, markets, idx)
    assert pool.pool_id not in cm.market_universe(other, case, 3, CL_STAGE)


def test_restricted_problem_keeps_the_prepared_indexes_and_directions() -> None:
    b = bundle("mantle_mixed")
    case = b.case("usdt_wmnt_evidence_split")
    idx = cl.prepare_cl_indexes(b)
    problem = cm.dual_problem(b, case, cm.market_universe(b, case, 3, CL_STAGE), idx)
    cl_ids = list(problem.cl)
    assert set(cl_ids) == {
        p.pool_id for p in problem.markets if isinstance(p, ConcentratedPoolState)
    }
    pool = b.pools[cl_ids[0]]
    assert isinstance(pool, ConcentratedPoolState)
    sub = cm.restricted(problem, {cl_ids[0]: pool.token1})
    assert dict(sub.cl) == {cl_ids[0]: idx[cl_ids[0]]} and sub.cl[cl_ids[0]] is idx[cl_ids[0]]
    index = idx[cl_ids[0]]
    sells_0 = {pool.token0: 0.5 * index.sqrt_price**2, pool.token1: 1.0}
    assert cm.cl_arb(index, sells_0) is not None and cm.cl_arb(index, sells_0, pool.token1) is None
    assert cm.cl_arb(index, sells_0, pool.token0) == cm.cl_arb(index, sells_0)


# ------------------------------------------------------------------ 8. prepared reuse, size


def test_no_index_work_inside_oracle_objective_or_solve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Indexes are built only by the caller's preparation: with every build path disabled
    the oracle, the objective and a full guarded SciPy solve still run."""
    b = bundle("uniswap_v3")
    idx = cl.prepare_cl_indexes(b)
    case = b.case("usdt_wmnt_medium")
    problem = cm.dual_problem(b, case, cm.market_universe(b, case, 3, CL_STAGE), idx)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("index work during a solve")

    for name in ("build_cl_index", "_side", "_initialized", "_sqrt_at", "get_sqrt_ratio_at_tick"):
        monkeypatch.setattr(cl, name, refuse)
    sigma = cm.scales(problem)
    cm.log_objective(problem, sigma, [0.1] * len(problem.variables))
    sol = co.solve(problem, SETTINGS, co.SolveBudget.for_settings(SETTINGS))
    assert sol.failure is None and sol.evaluations > 0


def test_one_prepared_index_set_serves_every_case_in_any_order() -> None:
    """Case-order independence and no case state in an index: the per-case solve outcome
    is the same whether cases run forward or reversed on ONE prepared index set, and the
    indexes are unchanged afterwards. Preparing twice builds fresh, equal indexes (no
    hidden process-wide cache)."""
    b = bundle("uniswap_v3")
    idx = cl.prepare_cl_indexes(b)
    frozen = {pid: (i.down, i.up) for pid, i in idx.items()}
    cases = b.cases[:12]

    def run(order: list[Case]) -> dict[str, Any]:
        out = {}
        for case in order:
            problem = cm.dual_problem(b, case, cm.market_universe(b, case, 3, CL_STAGE), idx)
            assert all(problem.cl[pid] is idx[pid] for pid in problem.cl)
            sol = co.solve(problem, SETTINGS, co.SolveBudget.for_settings(SETTINGS))
            assert sol.point is not None
            out[case.case_id] = (sol.termination, sol.evaluations, sol.point.x, sol.point.trades)
        return out

    assert run(list(cases)) == run(list(reversed(cases)))
    assert {pid: (i.down, i.up) for pid, i in idx.items()} == frozen
    again = cl.prepare_cl_indexes(b)
    assert list(again) == list(idx) and all(again[p] is not idx[p] for p in idx)
    assert all((again[p].down, again[p].up) == frozen[p] for p in idx)
    assert list(idx) == [p for p, s in b.pools.items() if isinstance(s, ConcentratedPoolState)]


def test_index_is_immutable_and_references_the_snapshot_state() -> None:
    state = synthetic()
    idx = cl.build_cl_index(state)
    assert idx.state is state  # a reference, not a copy of the tick data
    with pytest.raises(dataclasses.FrozenInstanceError):
        idx.gamma = 1.0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        idx.down.boundary = "min_tick"  # type: ignore[misc]
    assert all(
        isinstance(v, tuple)
        for v in (idx.down.edges, idx.down.liquidity, idx.down.net, idx.down.out)
    )


def test_index_size_and_build_work_follow_the_collected_state_not_the_word_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build work is O(non-zero words * 256 + initialized ticks), memory O(segments): a
    3000-word-wide collected range with the same two non-zero words scans two words, makes
    the same number of exact sqrt-ratio calls and allocates about the same memory."""
    narrow = synthetic()
    wide = synthetic(bitmap_word_range=(-3000, 3000))
    calls: list[int] = []
    real = get_sqrt_ratio_at_tick

    def counting(tick: int) -> int:
        calls.append(tick)
        return real(tick)

    monkeypatch.setattr(cl, "get_sqrt_ratio_at_tick", counting)
    peaks, work = [], []
    for state in (narrow, wide):
        calls.clear()
        tracemalloc.start()
        idx = cl.build_cl_index(state)
        peaks.append(tracemalloc.get_traced_memory()[1])
        tracemalloc.stop()
        stats = idx.stats()
        work.append((len(calls), stats["bitmap_words"], stats["initialized_ticks"]))
        segments = idx.down.segments + idx.up.segments
        assert stats["float_entries"] == 4 * segments + 6  # per side: edges/net/out n+1, L n
        assert len(calls) <= stats["initialized_ticks"] + 2
    assert work[0] == work[1] == (6, 2, 4)
    assert peaks[1] <= peaks[0] + 1024
    real_pool = bundle("fusionx_v3").pools["0x262255f4770aebe2d0c8b97a46287dcecc2a0aff"]
    assert isinstance(real_pool, ConcentratedPoolState)
    stats = cl.build_cl_index(real_pool).stats()
    assert stats["segments_down"] + stats["segments_up"] <= stats["initialized_ticks"] + 2


# ------------------------------------------------------------------ 9. shared budget, replay


@pytest.fixture
def oracle_counter(monkeypatch: pytest.MonkeyPatch) -> Callable[[], dict[str, int]]:
    """Counts the real CPMM and CL oracle calls made through the model's dual function."""
    counts = {"cpmm": 0, "cl": 0}
    cpmm, clarb = cm.cpmm_arb, cm.cl_arb

    def c1(*a: Any, **k: Any) -> Any:
        counts["cpmm"] += 1
        return cpmm(*a, **k)

    def c2(*a: Any, **k: Any) -> Any:
        counts["cl"] += 1
        return clarb(*a, **k)

    monkeypatch.setattr(cm, "cpmm_arb", c1)
    monkeypatch.setattr(cm, "cl_arb", c2)
    return lambda: dict(counts)


@pytest.mark.parametrize("cap", [1, 2, 3, 5, 600])
def test_mixed_solve_draws_on_the_same_guarded_budget(
    cap: int, oracle_counter: Callable[[], dict[str, int]]
) -> None:
    """CPMM + CL markets through the unchanged optimizer: charged evaluations never exceed
    `max_function_evaluations` (final point included), every evaluation calls every market
    oracle once (counted by wrappers), and a restricted re-solve draws on the SAME budget."""
    b = bundle("mantle_mixed")
    case = b.case("usdt_wmnt_evidence_split")
    problem = cm.dual_problem(
        b, case, cm.market_universe(b, case, 3, CL_STAGE), cl.prepare_cl_indexes(b)
    )
    n_cl = len(problem.cl)
    assert n_cl == 2 and len(problem.markets) == 5
    settings = dataclasses.replace(SETTINGS, max_function_evaluations=cap)
    budget = co.SolveBudget.for_settings(settings)
    sol = co.solve(problem, settings, budget)
    counts = oracle_counter()
    assert sol.evaluations <= cap and budget.evaluations == sol.evaluations
    assert sol.oracle_calls == counts["cpmm"] + counts["cl"] == sol.evaluations * 5
    assert counts["cl"] == sol.evaluations * n_cl
    assert sol.point is not None
    allowed = {t.pool_id: t.token_in for t in sol.point.trades}
    if not allowed:
        return
    again = co.resolve_restricted(problem, allowed, settings, budget, sol.point.nu)
    total = oracle_counter()
    assert budget.evaluations <= cap and total["cpmm"] + total["cl"] == budget.oracle_calls
    if again is None:  # skipped, never re-funded
        assert budget.remaining_evaluations == 0 or budget.remaining_iterations == 0
        assert budget.evaluations == sol.evaluations
    else:
        assert again.oracle_calls == again.evaluations * len(allowed)
        assert budget.evaluations == sol.evaluations + again.evaluations


def test_mixed_solve_is_deterministic() -> None:
    b = bundle("mantle_mixed")
    case = b.case("wmnt_usdt_evidence_split")
    idx = cl.prepare_cl_indexes(b)
    runs = []
    for _ in range(2):
        problem = cm.dual_problem(b, case, cm.market_universe(b, case, 3, CL_STAGE), idx)
        sol = co.solve(problem, SETTINGS, co.SolveBudget.for_settings(SETTINGS))
        assert sol.point is not None
        runs.append(
            (sol.termination, sol.evaluations, sol.iterations, sol.point.x, sol.point.trades)
        )
    assert runs[0] == runs[1]


@pytest.mark.parametrize(
    ("name", "case_id"),
    [
        ("mantle_mixed", "usdt_wmnt_evidence_split"),
        ("mantle_mixed", "wmnt_usdt_evidence_split"),
        ("uniswap_v3", "usdt_wmnt_medium"),
        ("uniswap_v3", "weth_usdt_small"),
        ("agni_v3", "usdc_wmnt_large"),
        ("fusionx_v3", "wmnt_usdt_large"),
    ],
)
def test_recovered_mixed_plan_replays_exactly(name: str, case_id: str) -> None:
    """The continuous CL solution only chooses support and shares: the unchanged recovery
    quotes every leg exactly on the original states and a fresh evaluator replay (new
    cache) reproduces the recovered gross. Evidence for component B; no strategy change."""
    b = bundle(name)
    case = b.case(case_id)
    markets = cm.market_universe(b, case, 3, CL_STAGE)
    sol = co.solve(
        cm.dual_problem(b, case, markets, cl.prepare_cl_indexes(b)),
        SETTINGS,
        co.SolveBudget.for_settings(SETTINGS),
    )
    assert sol.trades is not None and sol.point is not None
    preset = MODEL["preset"]
    rec = recover(
        b, case, markets, sol.trades, sol.point.nu,
        RecoveryOptions(
            preset["min_split_share"], preset["max_recovery_attempts"], preset["cycle_resolve"]
        ),
        QuoteCache(b), gross_only(),
    )  # fmt: skip
    assert rec.failure is None and rec.plan is not None and rec.gross is not None
    fresh = evaluate(b, case, rec.plan, gross_only())
    assert fresh.status is EvalStatus.OK and fresh.gross_output == rec.gross
    used_cl = [f for f in rec.flows if isinstance(b.pools[f.edge.pool_id], ConcentratedPoolState)]
    assert used_cl
    for f in used_cl:
        exact = quote_exact_in(b.pools[f.edge.pool_id], f.edge.token_in, f.amount_in)
        assert exact.status is QuoteStatus.OK and exact.amount_out == f.amount_out


def test_cl_dust_leg_is_pruned_as_zero_output() -> None:
    """G-L6: a CL pool returns OK with output 0 for fee-only dust; the runtime recovery
    prunes it (`zero_output`) and never donates the input."""
    state = synthetic()
    assert quote_exact_in(state, "T0", 1).amount_out == 0
    cp = ConstantProductPoolState("cp", "T0", "T1", 10**18, 10**18, 30)
    case = Case("dust", "T0", "T1", 2)
    b = bundle_of(state, cp, case=case)
    trades = [cm.Trade(state.pool_id, "T0", "T1", 1.0, 0.9), cm.Trade("cp", "T0", "T1", 1.0, 0.9)]
    rec = recover(
        b,
        case,
        [state.pool_id, "cp"],
        trades,
        {"T0": 1.0},
        RecoveryOptions(1e-6, 8, True),
        QuoteCache(b),
        gross_only(),
    )
    assert rec.pruned == [(state.pool_id, "zero_output", 1)]
    assert rec.plan is not None and rec.gross == 1
    assert math.isfinite(float(rec.gross))

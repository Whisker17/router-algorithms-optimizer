"""WHI-1597 (R022-Q01): the executable contract of the per-pool output upper bounds.

`docs/references/research-022/output-bounds.md` states and proves, per admitted pool family
and swap direction, an exact rational rate `r̄` with

    quote_exact_in(state, token_in, x).amount_out  <=  floor(r̄ * x)       (every OK quote)

and a per-hop chunk slack `s` with `q(x + m) - q(x) <= r̄ * m + s`. The functions in the
CONTRACT section below are that document's formulae, written out as the stated contract.
They are NOT production code (WHI-1599 implements the helper from the document) and the
solver imports nothing from here. Every expected value in the checks comes from the exact
migrated quote seam `pools.quote.quote_exact_in` (or the evaluator replaying it), never from
the contract functions under test: a check compares the seam's integer output with the
contract's rational bound.

The real-corpus check (143 pools of the frozen `mantle-5src-101082044` bundle) needs the
gitignored `data/` directory, which exists only in the primary clone. Point
`ROUTER_CORPUS_BUNDLE` at its `bundle` directory (read-only). Unset, the test falls back to
`<repo>/data/...` and *skips* when that is absent; the tracked 19-pool real fixture bundle
(`tests/fixtures/corpus/bundle`) is always checked, so the corpus claim never rests on a
skip. A set-but-missing `ROUTER_CORPUS_BUNDLE` fails instead of skipping.
"""

from __future__ import annotations

import json
import os
import random
from collections import Counter
from dataclasses import replace
from fractions import Fraction
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from benchmark.objective import gross_only
from pools import concentrated, constant_product, liquidity_book
from pools.cl_math import (
    FEE_PIPS_DENOMINATOR,
    MAX_SQRT_RATIO,
    MIN_SQRT_RATIO,
    Q96,
    UINT128_MAX,
    SolidityRevert,
    get_sqrt_ratio_at_tick,
)
from pools.liquidity_book import (
    MAX_FEE,
    PRECISION,
    REAL_ID_SHIFT,
    SCALE,
    LBRevert,
    get_price_from_id,
)
from pools.quote import quote_exact_in
from pools.result import QuoteStatus, SwapResult
from routing.evaluator import EvalStatus, evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from snapshot.bundle import load_bundle
from snapshot.models import (
    BlockRef,
    Case,
    ConcentratedPoolState,
    ConstantProductPoolState,
    LBStaticFeeParameters,
    LBVariableFeeParameters,
    LiquidityBookPoolState,
    PoolState,
    SnapshotBundle,
    TickInfo,
)

REPO = Path(__file__).resolve().parents[2]
TRACKED_BUNDLE = REPO / "tests" / "fixtures" / "corpus" / "bundle"
CORPUS_ENV = "ROUTER_CORPUS_BUNDLE"
CORPUS_DEFAULT = REPO / "data" / "corpus" / "mantle-5src-101082044" / "bundle"
CORPUS_BUNDLE_HASH = "717c21f35d1f7f6a02f7076b40793eaba564148cc4d187392049e503fa4d3143"
FIXTURES = REPO / "docs" / "references" / "research-022" / "fixtures"

# ======================================================================================
# CONTRACT: the formulae of docs/references/research-022/output-bounds.md (§3-§5).
# `None` is "no bound" and is never replaced by 0 or a guessed rate.
# ======================================================================================

Q192 = 1 << 192
CL_X_MAX = 1 << 127  # the chunk-slack domain: x + m <= 2**127 (output-bounds.md §5)
LB_PRICE_MIN = 1 << 38  # the LB price guard: 2**38 <= price_int <= 2**218 (output-bounds.md §4.3)
LB_PRICE_MAX = 1 << 218


def _cl_ratio(tick: int) -> int | None:
    try:
        return get_sqrt_ratio_at_tick(tick)
    except SolidityRevert:
        return None


def cpmm_rate(state: ConstantProductPoolState, token_in: str) -> Fraction | None:
    if token_in not in (state.token0, state.token1):
        return None
    if state.source_key is not None:
        source = constant_product.SOURCES.get(state.source_key)
        if source is None or state.fee_bps != source.fee_bps:
            return None
    if not (0 <= state.fee_bps < constant_product.FEE_DENOMINATOR):
        return None
    reserve_in, reserve_out = state.reserves_for(token_in)
    if reserve_in <= 0 or reserve_out <= 0:
        return None
    keep = constant_product.FEE_DENOMINATOR - state.fee_bps
    return Fraction(keep * reserve_out, constant_product.FEE_DENOMINATOR * reserve_in)


def cl_rate(state: ConcentratedPoolState, token_in: str) -> Fraction | None:
    if token_in not in (state.token0, state.token1):
        return None
    source = concentrated.SOURCES.get(state.source_key)
    if source is None:
        return None
    if (
        state.lm_pool is not None
        and state.lm_pool.lower() != concentrated.ZERO_ADDRESS
        and not source.has_lm_pool_hook
    ):
        return None
    if not (0 <= state.fee < FEE_PIPS_DENOMINATOR):
        return None
    price, zero_for_one = state.sqrt_price_x96, token_in == state.token0
    at_tick = _cl_ratio(state.tick)
    if at_tick is None or price < at_tick:  # frozen tick must not sit above the price
        return None
    if not zero_for_one:
        above = _cl_ratio(state.tick + 1)  # ... nor lag more than one tick below it
        if above is None or price > above:
            return None
    keep = FEE_PIPS_DENOMINATOR - state.fee
    if zero_for_one:
        return Fraction(keep * price * price, FEE_PIPS_DENOMINATOR * Q192)
    return Fraction(keep * Q192, FEE_PIPS_DENOMINATOR * price * price)


def lb_base_fee(state: LiquidityBookPoolState) -> int | None:
    if state.static_fee is None or state.bin_step < 1:
        return None
    base = state.static_fee.base_factor * state.bin_step * 10**10
    return base if base <= MAX_FEE else None


def lb_price(state: LiquidityBookPoolState) -> int | None:
    try:
        price = get_price_from_id(state.active_id, state.bin_step)
    except LBRevert:
        return None
    return price if LB_PRICE_MIN <= price <= LB_PRICE_MAX else None


def lb_rate(state: LiquidityBookPoolState, token_in: str) -> Fraction | None:
    if token_in not in (state.token0, state.token1):
        return None
    if state.source_key not in liquidity_book.SOURCES:
        return None
    base = lb_base_fee(state)
    if base is None or state.bin_step < 1:
        return None
    price = lb_price(state)
    if price is None:
        return None
    if token_in == state.token0:  # swapForY: Y out, price is Y per X
        return Fraction((PRECISION - base) * price, PRECISION * SCALE)
    return Fraction((PRECISION - base) * SCALE, PRECISION * price)


def output_rate(state: PoolState, token_in: str) -> Fraction | None:
    if isinstance(state, ConstantProductPoolState):
        return cpmm_rate(state, token_in)
    if isinstance(state, ConcentratedPoolState):
        return cl_rate(state, token_in)
    return lb_rate(state, token_in)


def cl_liquidity_cap(state: ConcentratedPoolState) -> int:
    """Any value the active liquidity takes while a swap traverses this state."""
    return min(UINT128_MAX, state.liquidity + sum(t.liquidity_gross for t in state.ticks.values()))


def chunk_slack(state: PoolState, token_in: str) -> Fraction | None:
    """`s` with `q(x + m) - q(x) <= r̄ * m + s` for all x, m >= 0, x + m <= 2**127."""
    rate = output_rate(state, token_in)
    if rate is None:
        return None
    if isinstance(state, ConstantProductPoolState):
        return Fraction(1)
    if isinstance(state, ConcentratedPoolState):
        price = state.sqrt_price_x96
        cap = Fraction(cl_liquidity_cap(state), Q96)
        if token_in == state.token0:
            if price >= 1 << 128:
                return None
            return Fraction(price * price, Q192) + 1 + cap
        spot = Fraction(Q192, price * price)
        return spot * (1 + cap) + 1
    bin_price = lb_price(state)
    assert bin_price is not None
    if token_in == state.token0:
        return Fraction(bin_price, SCALE) + 1
    return Fraction(SCALE, bin_price) + 1


def path_rate(rates: list[Fraction]) -> Fraction:
    out = Fraction(1)
    for rate in rates:
        out *= rate
    return out


def path_slack(rates: list[Fraction], slacks: list[Fraction]) -> Fraction:
    """`s_path = sum_k s_k * prod_{j>k} r̄_j` (hops in path order)."""
    total, tail = Fraction(0), Fraction(1)
    for rate, slack in zip(reversed(rates), reversed(slacks), strict=True):
        total += slack * tail
        tail *= rate
    return total


def floor_bound(rate: Fraction, x: int) -> int:
    return (rate.numerator * x) // rate.denominator


# ======================================================================================
# Shared check helpers (expected values come only from the exact quote seam)
# ======================================================================================

State = PoolState


def other(state: PoolState, token_in: str) -> str:
    return state.other_token(token_in)


def effective_out(result: SwapResult[PoolState]) -> int | None:
    """The quote's output as the extension `q`: OK -> amount_out; a dust input whose output
    floors to zero (`insufficient_output_amount`) -> 0; any other failure -> None."""
    if result.status is QuoteStatus.OK:
        return result.amount_out
    if result.status is QuoteStatus.INSUFFICIENT_OUTPUT_AMOUNT:
        return 0
    return None


class Tally:
    """Counts how many checks actually bit, so a pass cannot be vacuous."""

    def __init__(self) -> None:
        self.statuses: Counter[str] = Counter()
        self.ok = 0
        self.unbounded_ok = 0
        self.tightest = Fraction(0)
        self.marginal_above_rate = 0
        self.marginals = 0
        self.multi_step_ok = 0  # OK quotes that crossed >= 1 initialized tick / swept >= 2 bins

    def summary(self, label: str) -> str:
        return (
            f"{label}: ok_quotes={self.ok} unbounded_ok={self.unbounded_ok} "
            f"multi_step_ok={self.multi_step_ok} chunk_pairs={self.marginals} "
            f"marginal_above_rate={self.marginal_above_rate} "
            f"tightest={float(self.tightest):.9f} statuses={dict(self.statuses)}"
        )

    def bound(self, state: PoolState, token_in: str, x: int) -> None:
        rate = output_rate(state, token_in)
        result = quote_exact_in(state, token_in, x)
        self.statuses[result.status.value] += 1
        if result.status is not QuoteStatus.OK:
            return
        self.ok += 1
        crossed = result.features.get("initialized_ticks_crossed", 0)
        self.multi_step_ok += crossed >= 1 or result.features.get("lb_bins_swapped", 0) >= 2
        if rate is None:
            self.unbounded_ok += 1
            return
        assert result.amount_out <= floor_bound(rate, x), (
            f"{state.pool_id} {token_in} x={x}: out {result.amount_out} > floor({rate} * x)"
        )
        if rate * x > 0:
            self.tightest = max(self.tightest, Fraction(result.amount_out) / (rate * x))

    def chunk(self, state: PoolState, token_in: str, x: int, m: int) -> None:
        slack = chunk_slack(state, token_in)
        rate = output_rate(state, token_in)
        if slack is None or rate is None or x + m > CL_X_MAX:
            return
        q_x = 0 if x == 0 else effective_out(quote_exact_in(state, token_in, x))
        q_xm = effective_out(quote_exact_in(state, token_in, x + m))
        if q_x is None or q_xm is None:
            return
        self.marginals += 1
        marginal = q_xm - q_x
        # q is non-decreasing in the input (output-bounds.md §5.4): the multi-hop slack
        # propagation needs it, so it is checked on every chunk pair, not assumed
        assert marginal >= 0, f"{state.pool_id} {token_in} x={x} m={m}: q decreased by {-marginal}"
        if marginal > rate * m:
            self.marginal_above_rate += 1
        assert marginal <= rate * m + slack, (
            f"{state.pool_id} {token_in} x={x} m={m}: marginal {marginal} > {rate} * m + {slack}"
        )


def amount_grid(rng: random.Random, scale: int, count: int) -> list[int]:
    """Mixed amounts: dust, a log-uniform spread up to 2**100, and `scale`-relative sizes."""
    out = {1, 2, 3, 5}
    for _ in range(count):
        out.add(max(1, int(2 ** rng.uniform(0, 100))))
        out.add(max(1, int(scale * 2 ** rng.uniform(-40, 6))))
    return sorted(out)


def pick_amount(rng: random.Random, scale: int) -> int:
    if rng.random() < 0.5:
        return max(1, int(2 ** rng.uniform(0, 100)))
    return max(1, int(scale * 2 ** rng.uniform(-40, 6)))


# ======================================================================================
# Hand-derived cases and the R2 counterexample
# ======================================================================================


def _load_fixture(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text())
    assert isinstance(data, dict)
    return data


def test_r2_counterexample_reproduced_through_the_exact_seam() -> None:
    fx = _load_fixture("hand_cases.json")
    r2 = fx["r2_cpmm"]
    state = ConstantProductPoolState(
        "r2", "A", "B", r2["reserves"][0], r2["reserves"][1], fee_bps=r2["fee_bps"]
    )
    # q(0..6) is recomputed through the seam, never trusted from the fixture (a zero input is
    # the evaluator's no-call zero; a dust output is `insufficient_output_amount`).
    q = [0]
    for x in range(1, 7):
        out = effective_out(quote_exact_in(state, "A", x))
        assert out is not None
        q.append(out)
    assert q == r2["outputs_x0_to_x6"] == [0, 0, 1, 2, 3, 4, 5]
    assert quote_exact_in(state, "A", 1).status is QuoteStatus.INSUFFICIENT_OUTPUT_AMOUNT
    rate = cpmm_rate(state, "A")
    assert rate == Fraction(997, 1000) == Fraction(r2["rate"])
    # The marginal of one extra unit is 1, but r̄ * m = 0.997 < 1: no slack-free chunk bound
    # exists; s = 1 covers it, and ceil(r̄ m) = 1 is the tight integer form.
    assert q[2] - q[1] == 1 > rate * 1
    assert chunk_slack(state, "A") == 1
    for x in range(0, 40):
        for m in (1, 2, 3, 7):
            q_x = 0 if x == 0 else effective_out(quote_exact_in(state, "A", x))
            q_xm = effective_out(quote_exact_in(state, "A", x + m))
            assert q_x is not None and q_xm is not None
            assert q_xm - q_x <= rate * m + 1
            assert q_xm - q_x <= -(-(rate * m).numerator // (rate * m).denominator)  # ceil


def test_hand_derived_rates() -> None:
    fx = _load_fixture("hand_cases.json")
    for case in fx["rate_cases"]:
        state: PoolState
        kind = case["family"]
        if kind == "cpmm":
            reserve0, reserve1 = case["reserves"]
            state = ConstantProductPoolState("h", "A", "B", reserve0, reserve1, case["fee_bps"])
        elif kind == "cl":
            tick = case["tick"]
            state = ConcentratedPoolState(
                "h", "uniswap_v3", "A", "B", case["fee"], 60, get_sqrt_ratio_at_tick(tick), tick,
                10**18, 0, 0, 0, 0, 0, (-4, 4)
            )  # fmt: skip
        else:
            state = LiquidityBookPoolState(
                "h", "moe_lb_v2_2", "A", "B", case["bin_step"], 1_000_000, case["active_id"],
                0, 0, 0, 0,
                LBStaticFeeParameters(case["base_factor"], 30, 600, 5000, 0, 1000, 350_000),
                LBVariableFeeParameters(0, 0, case["active_id"], 1_000_000), (0, (1 << 24) - 1),
            )  # fmt: skip
        for token_in, expected in case["rates"].items():
            got = output_rate(state, token_in)
            assert got is not None
            if "approx" in case:  # ids away from 2**23: the price is only hand-derived to 1e-30
                assert abs(got - Fraction(expected)) < Fraction(1, 10**30), case["name"]
            else:
                assert got == Fraction(expected), case["name"]


# ======================================================================================
# Randomized states: CPMM
# ======================================================================================


def random_cpmm(rng: random.Random) -> ConstantProductPoolState:
    def reserve() -> int:
        return max(1, int(2 ** rng.uniform(0, 112)))

    fee_bps = rng.choice([0, 1, 5, 30, 30, 30, 100, 500, 2500, 9999])
    return ConstantProductPoolState("cp", "A", "B", reserve(), reserve(), fee_bps)


def test_cpmm_never_underestimates_random_and_boundary() -> None:
    rng = random.Random(22_1597_01)
    tally = Tally()
    for _ in range(3000):
        state = random_cpmm(rng)
        for token in ("A", "B"):
            reserve_in = state.reserves_for(token)[0]
            for x in amount_grid(rng, reserve_in, 2) + [reserve_in, 2 * reserve_in, 2**112]:
                tally.bound(state, token, x)
                tally.chunk(state, token, x, rng.choice([1, 2, 7, x // 3 + 1, x]))
    print(tally.summary("RANDOM cpmm"))
    assert tally.ok > 20_000 and tally.unbounded_ok == 0
    assert tally.statuses["insufficient_output_amount"] > 0 and tally.statuses["reverted"] == 0
    assert tally.tightest > Fraction(99, 100)  # the bound is tight near the spot, not vacuous
    assert tally.marginal_above_rate > 0  # the R2 phenomenon recurs: s > 0 is necessary


def test_cpmm_failure_and_unsupported_states_have_no_bound() -> None:
    sourced = ConstantProductPoolState("s", "A", "B", 1000, 1000, 30, "moe_classic_v1")
    assert cpmm_rate(sourced, "A") == Fraction(997, 1000)
    # the u112 overflow revert is a failed quote (no output), never an OK one above the bound
    big = replace(sourced, reserve0=(1 << 112) - 10, reserve1=(1 << 112) - 10)
    assert quote_exact_in(big, "A", 100).status is QuoteStatus.REVERTED
    for bad in (
        replace(sourced, source_key="no_such_source"),
        replace(sourced, fee_bps=25),  # not moe_classic_v1's fixed 30
        replace(sourced, reserve1=0),
        replace(sourced, reserve0=0),
    ):
        assert cpmm_rate(bad, "A") is None
        assert quote_exact_in(bad, "A", 100).status is not QuoteStatus.OK
    for bad in (
        replace(sourced, fee_bps=10_000, source_key=None),
        replace(sourced, fee_bps=-1, source_key=None),
    ):
        assert cpmm_rate(bad, "A") is None  # the seam itself raises: there is no quote at all
        with pytest.raises(ValueError, match="fee_bps"):
            quote_exact_in(bad, "A", 100)
    assert cpmm_rate(sourced, "C") is None
    assert quote_exact_in(sourced, "C", 5).status is QuoteStatus.UNSUPPORTED_TOKEN


# ======================================================================================
# Randomized states: CL
# ======================================================================================


def _cl_state(
    rng: random.Random,
    *,
    source: str,
    spacing: int,
    fee: int,
    tick: int,
    price_mode: str,
    ranges: list[tuple[int, int, int]],
) -> ConcentratedPoolState:
    nets: dict[int, int] = {}
    gross: dict[int, int] = {}
    for lo, hi, liquidity in ranges:
        nets[lo] = nets.get(lo, 0) + liquidity
        nets[hi] = nets.get(hi, 0) - liquidity
        gross[lo] = gross.get(lo, 0) + liquidity
        gross[hi] = gross.get(hi, 0) + liquidity
    active = sum(liq for lo, hi, liq in ranges if lo <= tick < hi)
    bitmap: dict[int, int] = {}
    for t in nets:
        compressed = t // spacing
        bitmap[compressed >> 8] = bitmap.get(compressed >> 8, 0) | (1 << (compressed & 0xFF))
    words = [(tick // spacing) >> 8, *bitmap]
    base, top = get_sqrt_ratio_at_tick(tick), get_sqrt_ratio_at_tick(tick + 1)
    price = {"low": base, "high": top, "mid": rng.randint(base, top)}[price_mode]
    if source == "uniswap_v3":
        fee_protocol = rng.choice([0, 0x44, 0x55, 0xA4, 0x4A])
    else:
        fee_protocol = rng.choice([0, 300, 300 | 300 << 16, 1000 | 2500 << 16])
    ticks = {
        t: TickInfo(gross[t], nets[t], rng.getrandbits(128), rng.getrandbits(128)) for t in nets
    }
    return ConcentratedPoolState(
        f"cl-{source}", source, "A", "B", fee, spacing, price, tick, active, fee_protocol,
        rng.getrandbits(128), rng.getrandbits(128), 0, 0, (min(words) - 1, max(words) + 1),
        bitmap, ticks,
        "0x00000000000000000000000000000000000000aa" if source != "uniswap_v3" else None,
    )  # fmt: skip


def random_cl(rng: random.Random, *, extreme: bool = False) -> tuple[ConcentratedPoolState, int]:
    source = rng.choice(["uniswap_v3", "agni_v3", "fusionx_v3"])
    spacing = rng.choice([1, 10, 60, 200])
    fee = rng.choice([100, 500, 2500, 3000, 10_000, 100_000])
    tick = rng.randint(-8000, 8000)
    if extreme:  # prices far from 1: down to ~2**-128 and up to ~2**128
        tick = rng.choice([-1, 1]) * rng.randint(700_000, 887_000)
    ranges = []
    for _ in range(rng.randint(0, 5)):
        lo = (tick // spacing + rng.randint(-40, 20)) * spacing
        hi = lo + rng.randint(1, 60) * spacing
        ranges.append((lo, hi, max(1, int(2 ** rng.uniform(0, 100)))))
    mode = rng.choice(["low", "high", "mid", "mid", "mid"])
    state = _cl_state(
        rng, source=source, spacing=spacing, fee=fee, tick=tick, price_mode=mode, ranges=ranges
    )
    scale = max([liq for _, _, liq in ranges] + [2**40])
    return state, scale


def test_cl_never_underestimates_random_both_directions() -> None:
    rng = random.Random(22_1597_02)
    tally: dict[str, Tally] = {"A": Tally(), "B": Tally()}
    for i in range(900):
        state, scale = random_cl(rng, extreme=i % 5 == 0)
        for token in ("A", "B"):
            for x in [*amount_grid(rng, scale, 3), 2**127, 2**128, 2**160, 2**255 - 1]:
                tally[token].bound(state, token, x)
                tally[token].chunk(state, token, x, rng.choice([1, 2, 9, x // 3 + 1, x]))
    for token, t in tally.items():
        print(t.summary(f"RANDOM cl {token}"))
        assert t.ok > 1000 and t.unbounded_ok == 0, (token, t.ok, t.unbounded_ok)
        assert t.statuses["insufficient_liquidity"] + t.statuses["incomplete_snapshot"] > 0
        assert t.tightest > Fraction(99, 100), (token, float(t.tightest))
        assert t.marginals > 1000 and t.multi_step_ok > 100, (token, t.multi_step_ok)


def test_cl_protocol_fee_and_lm_hook_do_not_move_the_output() -> None:
    rng = random.Random(22_1597_03)
    for _ in range(150):
        state, scale = random_cl(rng)
        variants = [
            replace(state, fee_protocol=0),
            replace(
                state, fee_protocol=0x44 if state.source_key == "uniswap_v3" else 2500 | 2500 << 16
            ),
            replace(state, lm_pool=None),
        ]
        for token in ("A", "B"):
            for x in amount_grid(rng, scale, 1):
                outs = {
                    (r.status, r.amount_out)
                    for r in (quote_exact_in(v, token, x) for v in variants)
                    if r.status is not QuoteStatus.INCOMPLETE_SNAPSHOT
                }
                assert len(outs) <= 1, (state.pool_id, token, x, outs)
                assert cl_rate(variants[0], token) == cl_rate(variants[1], token)


def test_cl_tick_consistency_guard_is_necessary() -> None:
    """A frozen `tick` above the price makes the exact 0->1 swap start with a wrong-side
    target and out-earn r̄: the guard returns no bound for exactly that state."""
    price = get_sqrt_ratio_at_tick(-5)
    state = ConcentratedPoolState(
        "lead", "uniswap_v3", "A", "B", 3000, 60, price, 0, 10**18, 0, 0, 0, 0, 0, (-10, 10)
    )
    unguarded = Fraction((FEE_PIPS_DENOMINATOR - 3000) * price * price, FEE_PIPS_DENOMINATOR * Q192)
    result = quote_exact_in(state, "A", 10**6)
    assert result.status is QuoteStatus.OK
    assert result.amount_out > floor_bound(unguarded, 10**6)  # the naive bound would be unsound
    assert cl_rate(state, "A") is None
    # 1->0 with a frozen tick lagging by more than one spacing: the same wrong-side start
    lag = replace(state, tick=0, sqrt_price_x96=get_sqrt_ratio_at_tick(130))
    assert cl_rate(lag, "B") is None
    consistent = replace(state, tick=-5, sqrt_price_x96=price)
    assert cl_rate(consistent, "A") is not None and cl_rate(consistent, "B") is not None


def test_cl_failure_and_boundary_states() -> None:
    rng = random.Random(22_1597_04)
    state = _cl_state(
        rng, source="uniswap_v3", spacing=60, fee=3000, tick=0, price_mode="mid",
        ranges=[(-600, 600, 10**15)],
    )  # fmt: skip
    rate_ab, rate_ba = cl_rate(state, "A"), cl_rate(state, "B")
    assert rate_ab is not None and rate_ba is not None
    # incomplete bitmap: a swap that needs an uncollected word is a failed quote
    narrow = replace(state, bitmap_word_range=(0, 0))
    r = quote_exact_in(narrow, "A", 10**30)
    assert r.status is QuoteStatus.INCOMPLETE_SNAPSHOT and r.amount_out == 0
    # the bound reads slot0 + fee only, so it is unchanged -- harmless because never OK
    assert cl_rate(narrow, "A") == rate_ab
    # partial fill: collected, exhausted state is insufficient liquidity, never an OK partial
    full = replace(state, bitmap_word_range=(-100_000, 100_000))
    r = quote_exact_in(full, "B", 10**40)
    assert r.status is not QuoteStatus.OK and r.amount_out == 0
    # missing tick data for an initialized tick
    r = quote_exact_in(replace(state, ticks={}), "A", 10**30)
    assert r.status is QuoteStatus.INCOMPLETE_SNAPSHOT
    # unknown source, undeclared LM hook, fee >= 100%, wrong token: no bound
    assert cl_rate(replace(state, source_key="nope"), "A") is None
    assert cl_rate(replace(state, lm_pool="0x" + "11" * 20), "A") is None
    assert cl_rate(replace(state, fee=FEE_PIPS_DENOMINATOR), "A") is None
    assert cl_rate(state, "C") is None
    # price extremes: at the limit the quote is `insufficient_liquidity` (SPL), no output
    for tick, token in ((-887272, "A"), (887271, "B")):
        edge = replace(state, tick=tick, sqrt_price_x96=get_sqrt_ratio_at_tick(tick))
        r = quote_exact_in(edge, token, 10**6)
        assert r.status is not QuoteStatus.OK or r.amount_out <= floor_bound(
            cl_rate(edge, token) or Fraction(0), 10**6
        )
    assert MIN_SQRT_RATIO < get_sqrt_ratio_at_tick(-887271) and MAX_SQRT_RATIO > 0
    top = replace(state, tick=887272, sqrt_price_x96=MAX_SQRT_RATIO)
    assert quote_exact_in(top, "B", 10**6).status is QuoteStatus.INSUFFICIENT_LIQUIDITY
    assert cl_rate(top, "B") is None  # tick + 1 is outside TickMath's domain


# ======================================================================================
# Randomized states: LB
# ======================================================================================


def random_lb(rng: random.Random, *, extreme: bool = False) -> tuple[LiquidityBookPoolState, int]:
    step = rng.choice([1, 2, 5, 10, 15, 20, 25, 50, 100])
    active = REAL_ID_SHIFT + rng.randint(-3000, 3000)
    if extreme:
        active = REAL_ID_SHIFT + rng.choice([-1, 1]) * rng.randint(0, 600_000 // step)
    bins: dict[int, tuple[int, int]] = {}

    def reserve() -> int:
        return max(1, int(2 ** rng.uniform(0, 90)))

    for offset in range(-rng.randint(0, 60), rng.randint(0, 60) + 1):
        if rng.random() < 0.55 or offset == 0:
            i = active + offset
            bins[i] = (
                (reserve(), reserve())
                if offset == 0
                else ((0, reserve()) if offset < 0 else (reserve(), 0))
            )
    base_factor = rng.choice([0, 5, 50, 800, 5000]) if step * 5000 <= 10**7 else 5
    filter_period = rng.randint(1, 60)
    decay = rng.randint(filter_period + 1, 600)
    ts = 1_800_000_000
    dt = rng.choice([0, filter_period - 1, filter_period, decay - 1, decay, 10**6])
    max_vol = rng.choice([0, 100_000, 350_000, 1_048_575])
    static = LBStaticFeeParameters(
        base_factor, filter_period, decay, rng.choice([0, 2500, 5000, 10_000]),
        rng.choice([0, 1000, 40_000, 150_000]), rng.choice([0, 1000, 2500]), max_vol,
    )  # fmt: skip
    variable = LBVariableFeeParameters(
        rng.randint(0, max_vol), rng.randint(0, max_vol),
        active + rng.randint(-40, 40), ts - dt,
    )  # fmt: skip
    state = LiquidityBookPoolState(
        "lb", "moe_lb_v2_2", "A", "B", step, ts, active,
        sum(x for x, _ in bins.values()), sum(y for _, y in bins.values()),
        rng.randint(0, 1000), rng.randint(0, 1000), static, variable, (0, (1 << 24) - 1), bins,
    )  # fmt: skip
    return state, max([max(b) for b in bins.values()] + [2**30])


def test_lb_never_underestimates_random_both_directions() -> None:
    rng = random.Random(22_1597_05)
    tally: dict[str, Tally] = {"A": Tally(), "B": Tally()}
    for i in range(1200):
        state, scale = random_lb(rng, extreme=i % 6 == 0)
        for token in ("A", "B"):
            for x in [*amount_grid(rng, scale, 3), 2**127, 2**128 - 1, 2**200]:
                tally[token].bound(state, token, x)
                tally[token].chunk(state, token, x, rng.choice([1, 2, 9, x // 3 + 1, x]))
    for token, t in tally.items():
        print(t.summary(f"RANDOM lb {token}"))
        assert t.ok > 600, (token, t.ok)
        assert t.statuses["insufficient_liquidity"] > 0
        assert t.tightest > Fraction(9, 10), (token, float(t.tightest))
        assert t.marginals > 500 and t.multi_step_ok > 100, (token, t.multi_step_ok)


def test_lb_variable_fee_can_fall_below_the_stored_value_so_only_the_base_fee_is_safe() -> None:
    """Premise check: the stored accumulator overstates the first bin's fee after the
    swap-start reference update, so `fee >= base fee` (variable part >= 0) is the bound."""
    static = LBStaticFeeParameters(5000, 30, 600, 5000, 40_000, 1000, 350_000)
    ts = 1_800_000_000
    active = REAL_ID_SHIFT + 7
    state = LiquidityBookPoolState(
        "decay", "moe_lb_v2_2", "A", "B", 25, ts, active, 10**20, 10**20, 0, 0, static,
        LBVariableFeeParameters(200_000, 200_000, active - 20, ts - 10**6),
        (0, (1 << 24) - 1),
        {active - 1: (0, 10**20), active: (10**20, 10**20), active + 1: (10**20, 0)},
    )  # fmt: skip
    params = liquidity_book.encode_parameters(static, state.variable_fee, active)  # type: ignore[arg-type]
    stored_fee = liquidity_book.get_total_fee(params, 25)
    after = liquidity_book.update_volatility_accumulator(
        liquidity_book.update_references(params, ts), active
    )
    first_fee = liquidity_book.get_total_fee(after, 25)
    base = liquidity_book.get_base_fee(params, 25)
    assert first_fee < stored_fee and first_fee == base  # variable part reset to zero
    rate = lb_rate(state, "A")
    assert rate == Fraction(PRECISION - base, PRECISION) * Fraction(
        get_price_from_id(active, 25), SCALE
    )
    out = quote_exact_in(state, "A", 10**15)
    assert out.status is QuoteStatus.OK and out.amount_out <= floor_bound(rate, 10**15)
    # a bound that used the stored (decayed-away) variable fee would be *under* the base-fee
    # bound's complement: it is the lower fee that makes the output larger
    wrong = Fraction(PRECISION - stored_fee, PRECISION) * Fraction(
        get_price_from_id(active, 25), SCALE
    )
    assert wrong < rate


def test_lb_failure_boundary_and_no_bound_states() -> None:
    rng = random.Random(22_1597_06)
    state, _ = random_lb(rng)
    assert lb_rate(state, "A") is not None
    # missing fee state: incomplete snapshot, no bound
    no_static = replace(state, static_fee=None)
    assert quote_exact_in(no_static, "A", 100).status is QuoteStatus.INCOMPLETE_SNAPSHOT
    assert lb_rate(no_static, "A") is None
    assert lb_rate(replace(state, source_key="nope"), "A") is None
    assert lb_rate(state, "C") is None
    # base fee above the 10% cap: every swap that touches a bin reverts FeeTooLarge
    assert state.static_fee is not None
    over = replace(state, static_fee=replace(state.static_fee, base_factor=65535), bin_step=200)
    assert lb_base_fee(over) is None and lb_rate(over, "A") is None
    # an active bin whose price underflows pow128 (|y| >= 2**20): no bound
    far = replace(state, active_id=REAL_ID_SHIFT - (1 << 20), bin_range=(0, (1 << 24) - 1))
    assert lb_rate(far, "A") is None
    # price outside the proved monotonicity window (2**38 .. 2**218): no bound
    for guard_id in (REAL_ID_SHIFT - 700_000, REAL_ID_SHIFT + 700_000):
        assert lb_price(replace(state, bin_step=1, active_id=guard_id)) is None
    # exhaustion: the walk ends at the bottom of the id space -> insufficient liquidity
    r = quote_exact_in(state, "A", 2**120)
    assert r.status is not QuoteStatus.OK and r.amount_out == 0
    # uncollected range: a walk leaving the collected ids is incomplete, never zero-valued OK
    only_active = {i: b for i, b in state.bins.items() if i == state.active_id}
    clipped = replace(state, bins=only_active, bin_range=(state.active_id, state.active_id))
    r = quote_exact_in(clipped, "A", 2**100)
    assert r.status is not QuoteStatus.OK
    # an unadmitted swap hook is UNSUPPORTED (no OK quote, so no output to bound)
    hooked = replace(state, hooks_parameters=liquidity_book.BEFORE_SWAP_FLAG | 1)
    assert quote_exact_in(hooked, "A", 100).status is QuoteStatus.UNSUPPORTED


@pytest.mark.parametrize("step", [1, 2, 10, 25, 100])
def test_lb_price_is_monotone_over_the_whole_proved_window(step: int) -> None:
    """Lemma M (output-bounds.md §4.3): get_price_from_id is non-decreasing in the id over every
    id whose price lies in [2**-90, 2**90]; adjacent pairs suffice by transitivity."""
    import math

    reach = int(90 * math.log(2) / math.log(1 + step / 10_000)) + 2
    previous = get_price_from_id(REAL_ID_SHIFT - reach, step)
    checked = 0
    for y in range(-reach + 1, reach + 1):
        price = get_price_from_id(REAL_ID_SHIFT + y, step)
        assert price >= previous, (step, y)
        previous = price
        checked += 1
    assert checked == 2 * reach
    # the window really covers the whole guard [2**38, 2**218]
    assert get_price_from_id(REAL_ID_SHIFT - reach, step) < LB_PRICE_MIN
    assert get_price_from_id(REAL_ID_SHIFT + reach, step) > LB_PRICE_MAX


# ======================================================================================
# Same-direction monotonicity, opposite-direction exclusion
# ======================================================================================


def _random_state(rng: random.Random, family: str) -> tuple[PoolState, int]:
    if family == "cpmm":
        state = random_cpmm(rng)
        return state, state.reserve0
    if family == "cl":
        return random_cl(rng)
    return random_lb(rng)


@pytest.mark.parametrize("family", ["cpmm", "cl", "lb"])
def test_same_direction_bound_from_the_original_state_dominates_later_swaps(family: str) -> None:
    rng = random.Random(22_1597_07)
    swaps = checked = shrank = 0
    for _ in range(500):
        state, scale = _random_state(rng, family)
        for token in ("A", "B"):
            rate0 = output_rate(state, token)
            if rate0 is None:
                continue
            current: PoolState = state
            for _ in range(4):
                x = pick_amount(rng, scale)
                result = quote_exact_in(current, token, x)
                if result.status is not QuoteStatus.OK or result.new_state is None:
                    continue
                swaps += 1
                current = result.new_state
                rate_now = output_rate(current, token)
                if rate_now is not None:
                    assert rate_now <= rate0, (family, token, x)
                    shrank += rate_now < rate0
                for y in (pick_amount(rng, scale) for _ in range(6)):
                    later = quote_exact_in(current, token, y)
                    if later.status is QuoteStatus.OK:
                        checked += 1
                        assert later.amount_out <= floor_bound(rate0, y), (family, token, y)
    print(f"SAME-DIRECTION {family}: swaps={swaps} later_quotes_checked={checked} shrank={shrank}")
    assert swaps > 200 and checked > 500 and shrank > 50


@pytest.mark.parametrize("family", ["cpmm", "cl", "lb"])
def test_opposite_direction_reuse_breaks_the_bound_and_the_evaluator_excludes_it(
    family: str,
) -> None:
    rng = random.Random(22_1597_08)
    violations = 0
    for _ in range(400):
        state, scale = _random_state(rng, family)
        rate_ba = output_rate(state, "B")
        if rate_ba is None:
            continue
        x = max(1, int(scale * 2 ** rng.uniform(-3, 2)))
        first = quote_exact_in(state, "A", x)
        if first.status is not QuoteStatus.OK or first.new_state is None:
            continue
        y = first.amount_out
        if y == 0:
            continue
        back = quote_exact_in(first.new_state, "B", y)
        if back.status is QuoteStatus.OK and back.amount_out > floor_bound(rate_ba, y):
            violations += 1
    print(f"OPPOSITE-DIRECTION {family}: counterexamples={violations}")
    assert violations > 0, "no opposite-direction counterexample found"

    # The evaluator never executes such a plan: a pool used A->B then B->A closes a token cycle.
    p1 = ConstantProductPoolState("p1", "A", "B", 10**6, 10**6, 30)
    p2 = ConstantProductPoolState("p2", "A", "C", 10**6, 10**6, 30)
    bundle = SnapshotBundle(
        "t", "synthetic", 1, BlockRef(0, 0, "0x" + "00" * 32, 0), {"p1": p1, "p2": p2}, (),
        "deadbeef", "<test>",
    )  # fmt: skip
    case = Case("c", "A", "C", 1000)
    plan = RoutePlan(
        (
            SwapStep("p1", "A", "B", (FundInput(REQUEST_FUND_ID, 500),), "f1"),
            SwapStep("p1", "B", "A", (FundInput("f1", ALL_REMAINING),), "f2"),
            SwapStep(
                "p2", "A", "C",
                (FundInput(REQUEST_FUND_ID, ALL_REMAINING), FundInput("f2", ALL_REMAINING)), "f3",
            ),
        )
    )  # fmt: skip
    ev = evaluate(bundle, case, plan, gross_only())
    assert ev.status is EvalStatus.INVALID_PLAN
    assert ev.error is not None and ev.error.startswith("economic token cycle")
    assert ev.trace == ()  # rejected statically, before any pool call
    # Same-direction reuse of one pool is admitted and replays on the threaded state.
    twice = RoutePlan(
        (
            SwapStep("p1", "A", "B", (FundInput(REQUEST_FUND_ID, 400),), "g1"),
            SwapStep("p1", "A", "B", (FundInput(REQUEST_FUND_ID, ALL_REMAINING),), "g2"),
            SwapStep(
                "p2", "B", "C",
                (FundInput("g1", ALL_REMAINING), FundInput("g2", ALL_REMAINING)), "g3",
            ),
        )
    )  # fmt: skip
    case_ab = Case("c2", "A", "C", 1000)
    p3 = ConstantProductPoolState("p2", "B", "C", 10**6, 10**6, 30)
    bundle2 = replace(bundle, pools={"p1": p1, "p2": p3})
    ev2 = evaluate(bundle2, case_ab, twice, gross_only())
    assert ev2.status is EvalStatus.OK
    first_out = quote_exact_in(p1, "A", 400)
    second = quote_exact_in(first_out.new_state, "A", 600)  # type: ignore[arg-type]
    assert [t.amount_out for t in ev2.trace[:2]] == [first_out.amount_out, second.amount_out]
    assert second.amount_out <= floor_bound(cpmm_rate(p1, "A") or Fraction(0), 600)


# ======================================================================================
# Real corpus: every pool x both directions x sampled amounts, multi-hop composition
# ======================================================================================


@cache
def _bundle(path: str) -> SnapshotBundle:
    return load_bundle(path)


def _corpus_paths() -> list[tuple[str, Path]]:
    paths = [("tracked-fixture", TRACKED_BUNDLE)]
    env = os.environ.get(CORPUS_ENV)
    if env:
        assert Path(env).is_dir(), f"{CORPUS_ENV}={env!r} is not a directory"
        paths.append(("frozen-corpus", Path(env)))
    elif CORPUS_DEFAULT.is_dir():
        paths.append(("frozen-corpus", CORPUS_DEFAULT))
    return paths


def _amounts_for(bundle: SnapshotBundle, token: str) -> list[int]:
    grid = {1, 2, 3, 7} | {2**k for k in range(0, 100, 4)} | {10**k for k in range(0, 30, 2)}
    cases = sorted({c.amount_in for c in bundle.cases if c.token_in == token})
    return sorted(grid | set(cases[:: max(1, len(cases) // 12)]))


CORPUS_REPORT: dict[str, dict[str, int]] = {}


@pytest.mark.parametrize("label", ["tracked-fixture", "frozen-corpus"])
def test_corpus_pools_both_directions_never_underestimate(label: str) -> None:
    available = dict(_corpus_paths())
    if label not in available:
        pytest.skip(f"{label}: set {CORPUS_ENV} (data/ exists only in the primary clone)")
    bundle = _bundle(str(available[label]))
    if label == "frozen-corpus":
        assert bundle.bundle_hash == CORPUS_BUNDLE_HASH and len(bundle.pools) == 143
    tally = Tally()
    pools = directions = amounts = chunks = 0
    families: Counter[str] = Counter()
    for state in bundle.pools.values():
        pools += 1
        families[type(state).__name__] += 1
        for token in (state.token0, state.token1):
            directions += 1
            rate = output_rate(state, token)
            for x in _amounts_for(bundle, token):
                amounts += 1
                tally.bound(state, token, x)
                if rate is not None and x < CL_X_MAX:
                    chunks += 1
                    tally.chunk(state, token, x, max(1, x // 7))
    assert tally.unbounded_ok == 0, "an OK real-corpus quote had no bound"
    assert tally.ok > 0
    CORPUS_REPORT[label] = {
        "pools": pools, "directions": directions, "amounts": amounts, "ok_quotes": tally.ok,
        "chunk_checks": tally.marginals, **{f"fam_{k}": v for k, v in families.items()},
        **{f"status_{k}": v for k, v in tally.statuses.items()},
    }  # fmt: skip
    print(f"CORPUS {label}: {CORPUS_REPORT[label]}")


@pytest.mark.parametrize("label", ["tracked-fixture", "frozen-corpus"])
def test_corpus_frozen_state_satisfies_the_guards(label: str) -> None:
    available = dict(_corpus_paths())
    if label not in available:
        pytest.skip(f"{label}: set {CORPUS_ENV} (data/ exists only in the primary clone)")
    bundle = _bundle(str(available[label]))
    missing = [
        (s.pool_id, t)
        for s in bundle.pools.values()
        for t in (s.token0, s.token1)
        if output_rate(s, t) is None
    ]
    assert missing == [], f"real pools with no bound: {missing[:5]}"
    for state in bundle.pools.values():
        assert state.token0 != state.token1


@pytest.mark.parametrize("label", ["tracked-fixture", "frozen-corpus"])
def test_corpus_same_direction_and_multi_hop_composition(label: str) -> None:
    available = dict(_corpus_paths())
    if label not in available:
        pytest.skip(f"{label}: set {CORPUS_ENV} (data/ exists only in the primary clone)")
    bundle = _bundle(str(available[label]))
    rng = random.Random(22_1597_09)
    pools = list(bundle.pools.values())
    # same-direction: a swap leaves a state whose bound is not larger, and the original
    # bound still dominates the next quote
    same = 0
    for state in pools:
        for token in (state.token0, state.token1):
            rate0 = output_rate(state, token)
            assert rate0 is not None
            for x in _amounts_for(bundle, token)[::5]:
                first = quote_exact_in(state, token, x)
                if first.status is not QuoteStatus.OK or first.new_state is None:
                    continue
                rate1 = output_rate(first.new_state, token)
                assert rate1 is None or rate1 <= rate0
                second = quote_exact_in(first.new_state, token, max(1, x // 3))
                if second.status is QuoteStatus.OK:
                    same += 1
                    assert second.amount_out <= floor_bound(rate0, max(1, x // 3))
    assert same > 0
    print(f"CORPUS {label} same-direction pairs checked: {same}")
    # multi-hop: simple token paths over distinct pools, exact hop-by-hop replay
    by_token: dict[str, list[tuple[PoolState, str]]] = {}
    for state in pools:
        for token in (state.token0, state.token1):
            by_token.setdefault(token, []).append((state, state.other_token(token)))
    paths = checked = chunk_checked = 0
    for _ in range(4000):
        hops: list[tuple[PoolState, str]] = []
        token = rng.choice(sorted(by_token))
        visited, used = {token}, set()
        for _hop in range(rng.randint(2, 4)):
            options = [
                (s, out) for s, out in by_token.get(token, [])
                if out not in visited and s.pool_id not in used
            ]  # fmt: skip
            if not options:
                break
            state, out = rng.choice(options)
            hops.append((state, token))
            visited.add(out)
            used.add(state.pool_id)
            token = out
        if len(hops) < 2:
            continue
        paths += 1
        rates = [output_rate(s, t) for s, t in hops]
        slacks = [chunk_slack(s, t) for s, t in hops]
        assert all(r is not None for r in rates)
        good_rates = [r for r in rates if r is not None]
        total = path_rate(good_rates)
        for x in (
            rng.choice(_amounts_for(bundle, hops[0][1])),
            rng.choice([10**6, 10**12, 10**18]),
        ):
            out_amount = _replay(hops, x)
            if out_amount is None:
                continue
            checked += 1
            assert out_amount <= floor_bound(total, x), (x, [s.pool_id for s, _ in hops])
            if all(s is not None for s in slacks):
                good_slacks = [s for s in slacks if s is not None]
                m = max(1, x // 5)
                out_more = _replay(hops, x + m)
                if out_more is not None and x + m <= CL_X_MAX:
                    chunk_checked += 1
                    assert out_more - out_amount <= total * m + path_slack(good_rates, good_slacks)
    print(f"CORPUS {label} multi-hop: paths={paths} replays={checked} chunk_pairs={chunk_checked}")
    assert paths > 100 and checked > 100 and chunk_checked > 50


def _replay(hops: list[tuple[PoolState, str]], x: int) -> int | None:
    amount = x
    for state, token in hops:
        if amount == 0:
            return 0
        result = quote_exact_in(state, token, amount)
        if result.status is QuoteStatus.INSUFFICIENT_OUTPUT_AMOUNT:
            return 0
        if result.status is not QuoteStatus.OK:
            return None
        amount = result.amount_out
    return amount


def test_multi_hop_composition_needs_only_per_hop_bounds_on_random_states() -> None:
    rng = random.Random(22_1597_10)
    checked = chunked = 0
    for _ in range(600):
        hops: list[tuple[PoolState, str]] = []
        for _hop in range(rng.randint(2, 3)):
            family = rng.choice(["cpmm", "cl", "lb"])
            state, _scale = _random_state(rng, family)
            hops.append((state, rng.choice(["A", "B"])))
        rates = [output_rate(s, t) for s, t in hops]
        slacks = [chunk_slack(s, t) for s, t in hops]
        if any(r is None for r in rates):
            continue
        good_rates = [r for r in rates if r is not None]
        total = path_rate(good_rates)
        for x in amount_grid(rng, 2**30, 2):
            out = _replay(hops, x)
            if out is None:
                continue
            checked += 1
            assert out <= floor_bound(total, x)
            if any(s is None for s in slacks):
                continue
            good_slacks = [s for s in slacks if s is not None]
            m = rng.choice([1, 2, 5, x // 4 + 1])
            more = _replay(hops, x + m)
            if more is not None and x + m <= CL_X_MAX:
                chunked += 1
                assert more - out <= total * m + path_slack(good_rates, good_slacks)
    print(f"RANDOM multi-hop: replays={checked} chunk_pairs={chunked}")
    assert checked > 500 and chunked > 300

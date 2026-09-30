"""R021-P15 (WHI-1560): Liquidity Book scope, model counterexamples and the proposed
exact-quote LB identity (docs/references/research-021/lb-scope.md).

Research evidence, not a strategy. Four kinds of evidence are kept apart:

1. **Fork evidence** (`tests/fixtures/liquidity_book/*.jsonl.gz`): per-bin `Swap` events
   the *deployed* LBPair emitted on a Mantle fork. Nothing in them comes from Python.
2. **An independent hand model** (`hand_swap`, `hand_schedule`): the fee, reference and
   per-bin amount rules re-derived from the pinned `lfj-gg/joe-v2` v2.2.0 Solidity text
   (`PairParameterHelper`, `FeeHelper`, `BinHelper`). It shares no code with
   `pools/liquidity_book.py`; the only protocol value it borrows is the 128.128 bin price,
   which it derives by hand for ids 2**23 and 2**23 + 1 and takes from the admitted
   `get_price_from_id` elsewhere (lemma L1 is stated relative to the protocol's prices).
3. **Admitted-simulator replays**: `pools.liquidity_book` / `quote_exact_in` and the
   unchanged evaluator on constructed and committed states.
4. **An executable specification** (`lb_sor_select`) of the *proposed future* identity
   `uni_sor_lb` over `uni_sor_port`'s unmodified routing core. It is never registered,
   profiled or timed.

`PYTHONPATH=. uv run python tests/routing/test_lb_scope_contract.py probe <bundle> <out.json>`
is the bounded tuning-split census of memo §6 (enumeration and per-pool checks only, no
solve, no timing). `... examples [probe.json]` rewrites the fixture from the hand model and
the fork evidence.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import random
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from benchmark.objective import gross_only
from pools import liquidity_book as lb
from pools.result import QuoteStatus
from routing.algorithms import uni_sor_port as usp
from routing.algorithms.base import AlgorithmConfig, Budget, SolveContext, SolveStatus
from routing.algorithms.path_split import split_path_plan
from routing.algorithms.registry import ALGORITHMS
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import RoutePlan
from routing.search import QuoteCache
from snapshot.bundle import load_bundle
from snapshot.config import load_catalog
from snapshot.models import (
    Case,
    LBStaticFeeParameters,
    LBVariableFeeParameters,
    LiquidityBookPoolState,
    SnapshotBundle,
)

REPO = Path(__file__).resolve().parents[2]
R021 = REPO / "docs" / "references" / "research-021"
FIXTURE_PATH = R021 / "fixtures" / "lb-scope.json"
FIX: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
MEMO = (R021 / "lb-scope.md").read_text(encoding="utf-8")
CONTRACT = json.loads((R021 / "contract-v1.json").read_text(encoding="utf-8"))
FORK = REPO / "tests" / "fixtures" / "liquidity_book"
FORK_FILES = (
    "controlled",
    "real_wmnt_usdt_15",
    "real_weth_wmnt_10",
    "real_usdc_usdt_1",
    "real_wmnt_usdt_25",
)
MANTLE_MIXED = REPO / "tests" / "fixtures" / "routing" / "mantle_mixed"
MOE_LB = REPO / "tests" / "fixtures" / "moe_lb" / "bundle"

# --- hand constants (Constants.sol / PriceHelper.sol at v2.2.0) ---------------------------
ONE = 10**18  # PRECISION, fee unit
BP = 10_000  # BASIS_POINT_MAX
Q128 = 1 << 128  # SCALE
R = 1 << 23  # REAL_ID_SHIFT: price(R) = 1
T = 1_800_000_000
E = 10**18
TOKEN_X = "0x" + "a1" * 20
TOKEN_Y = "0x" + "b2" * 20
UINT24_MAX = (1 << 24) - 1
MAX_TOTAL_FEE = ONE // 10  # LBPair._MAX_TOTAL_FEE, 10 %
MAX_PROTOCOL_SHARE = 2_500


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


# =========================================================================================
# 1. Independent hand model (from the pinned Solidity text; no pools.liquidity_book code)
# =========================================================================================


def hand_price(bin_id: int, bin_step: int) -> int:
    """`getPriceFromId` for the two ids the hand cases use: base**0 = SCALE and base**1 =
    `getBase` = SCALE + (binStep << 128) / 10_000 (integer division)."""
    if bin_id == R:
        return Q128
    if bin_id == R + 1:
        return Q128 + (bin_step << 128) // BP
    raise ValueError("the hand model only prices ids 2**23 and 2**23 + 1")


def static_params_valid(s: LBStaticFeeParameters, bin_step: int) -> bool:
    """`LBPair._setStaticFeeParameters` + `PairParameterHelper.setStaticFeeParameters`
    acceptance at v2.2.0: not all zero; filterPeriod <= decayPeriod <= uint12;
    reductionFactor <= 10_000; protocolShare <= 2_500; maxVolAcc <= uint20; widths; and
    baseFee + variableFee(maxVolAcc) <= 10 %."""
    fields = (
        s.base_factor,
        s.filter_period,
        s.decay_period,
        s.reduction_factor,
        s.variable_fee_control,
        s.protocol_share,
        s.max_volatility_accumulator,
    )
    if all(v == 0 for v in fields):
        return False
    if not (0 <= s.base_factor < 1 << 16 and 0 <= s.variable_fee_control < 1 << 24):
        return False
    if s.filter_period > s.decay_period or s.decay_period > 0xFFF:
        return False
    if s.reduction_factor > BP or s.protocol_share > MAX_PROTOCOL_SHARE:
        return False
    if s.max_volatility_accumulator > 0xFFFFF:
        return False
    return hand_fee(s, s.max_volatility_accumulator, bin_step) <= MAX_TOTAL_FEE


def hand_fee(s: LBStaticFeeParameters, vol: int, bin_step: int) -> int:
    """`getBaseFee` + `getVariableFee`: baseFactor*binStep*1e10 +
    ceil((vol*binStep)**2 * variableFeeControl / 100) (0 when the control is 0)."""
    base = s.base_factor * bin_step * 10**10
    if s.variable_fee_control == 0:
        return base
    return base + ceil_div((vol * bin_step) ** 2 * s.variable_fee_control, 100)


@dataclass(frozen=True)
class HandFeeState:
    active_id: int
    vol_acc: int
    vol_ref: int
    id_ref: int
    last_update: int


def hand_references(s: LBStaticFeeParameters, f: HandFeeState, now: int) -> HandFeeState:
    """`updateReferences(block.timestamp)` at the start of every swap."""
    dt = now - f.last_update
    assert dt >= 0
    if dt < s.filter_period:
        return replace(f, last_update=now)
    vol_ref = f.vol_acc * s.reduction_factor // BP if dt < s.decay_period else 0
    return replace(f, id_ref=f.active_id, vol_ref=vol_ref, last_update=now)


def hand_vol(s: LBStaticFeeParameters, f: HandFeeState, bin_id: int) -> int:
    """`updateVolatilityAccumulator(activeId)` for each non-empty bin."""
    return min(f.vol_ref + abs(bin_id - f.id_ref) * BP, s.max_volatility_accumulator)


@dataclass(frozen=True)
class HandBook:
    """Y -> X side of a two-bin book at ids R, R+1: X reserves per bin (Y is the input)."""

    bin_step: int
    static: LBStaticFeeParameters
    fees: HandFeeState
    x: tuple[int, int]  # X reserve of bins R and R+1


def hand_swap(book: HandBook, amount: int, now: int) -> tuple[int, HandBook] | None:
    """One Y -> X exact-input swap on the hand book (`LBPair.swap(false)` with
    `BinHelper.getAmounts`): returns (output, next book) or None when the two bins cannot
    absorb the input (the real book would continue or revert; the hand cases never go
    there)."""
    f = hand_references(book.static, book.fees, now)
    x = list(book.x)
    left, out = amount, 0
    active = f.active_id
    vol = f.vol_acc
    for idx, bin_id in enumerate((R, R + 1)):
        if bin_id < active or x[idx] == 0:
            continue
        active = bin_id
        vol = hand_vol(book.static, f, bin_id)
        fee = hand_fee(book.static, vol, book.bin_step)
        price = hand_price(bin_id, book.bin_step)
        max_in = ceil_div(x[idx] * price, Q128)  # mulShiftRoundUp(reserveX, price)
        max_in += ceil_div(max_in * fee, ONE - fee)  # + getFeeAmount (fee on top)
        if left >= max_in:
            out += x[idx]
            left -= max_in
            x[idx] = 0
        else:
            fee_amount = ceil_div(left * fee, ONE)  # getFeeAmountFrom (fee included)
            got = min(((left - fee_amount) * Q128) // price, x[idx])  # shiftDivRoundDown
            out += got
            x[idx] -= got
            left = 0
        if left == 0:
            break
    if left:
        return None
    nxt = replace(f, active_id=active, vol_acc=vol)
    return out, replace(book, fees=nxt, x=(x[0], x[1]))


def hand_quote(book: HandBook, amount: int, now: int) -> int:
    res = hand_swap(book, amount, now)
    assert res is not None
    return res[0]


# =========================================================================================
# 2. Admitted-simulator states
# =========================================================================================


def lb_state(
    *,
    active: int,
    bins: Mapping[int, tuple[int, int]],
    static: LBStaticFeeParameters,
    variable: LBVariableFeeParameters,
    bin_step: int = 1,
    timestamp: int = T,
) -> LiquidityBookPoolState:
    return LiquidityBookPoolState(
        pool_id="0x" + "c3" * 20,
        source_key="moe_lb_v2_2",
        token0=TOKEN_X,
        token1=TOKEN_Y,
        bin_step=bin_step,
        block_timestamp=timestamp,
        active_id=active,
        reserve_x=sum(b[0] for b in bins.values()),
        reserve_y=sum(b[1] for b in bins.values()),
        protocol_fee_x=0,
        protocol_fee_y=0,
        static_fee=static,
        variable_fee=variable,
        bin_range=(0, UINT24_MAX),
        bins=dict(bins),
        hooks_parameters=0,
        swap_hook_implementation=None,
    )


def static_from(d: Mapping[str, int]) -> LBStaticFeeParameters:
    return LBStaticFeeParameters(**d)


def book_from_state(state: LiquidityBookPoolState) -> HandBook:
    """The hand view of a simulator state restricted to bins R, R+1 (inputs, not expected
    values)."""
    v = state.variable_fee
    assert v is not None and state.static_fee is not None
    fees = HandFeeState(
        state.active_id,
        v.volatility_accumulator,
        v.volatility_reference,
        v.id_reference,
        v.time_of_last_update,
    )
    x = (state.bins.get(R, (0, 0))[0], state.bins.get(R + 1, (0, 0))[0])
    return HandBook(state.bin_step, state.static_fee, fees, x)


def sim_quote(state: LiquidityBookPoolState, amount: int) -> tuple[int, LiquidityBookPoolState]:
    res = lb.quote_exact_in(state, TOKEN_Y, amount)
    assert res.status is QuoteStatus.OK, res.detail
    assert res.new_state is not None
    return res.amount_out, res.new_state


@cache
def k3_state() -> LiquidityBookPoolState:
    """K3: the state right after a prior X -> Y swap in the same second walked the price
    down 10 bins (a reachable, ordinary state under the admitted transition)."""
    k = FIX["counterexamples"]["K3"]
    static = static_from(k["static"])
    bins = {R + i: (0, int(k["prior_book_y_per_bin"])) for i in range(11)}
    pre = lb_state(
        active=R + 10,
        bins=bins,
        static=static,
        variable=LBVariableFeeParameters(0, 0, R + 10, T - 3600),
    )
    out = lb.swap(pre, True, int(k["prior_swap_x_in"]))
    return out.new_state


# =========================================================================================
# 3. Hand model and fixture agreement
# =========================================================================================


def test_hand_prices_match_the_admitted_price_function() -> None:
    for bs in (1, 10, 25):
        assert lb.get_price_from_id(R, bs) == hand_price(R, bs)
        assert lb.get_price_from_id(R + 1, bs) == hand_price(R + 1, bs)


def test_counterexample_static_parameters_are_protocol_valid() -> None:
    for key in ("K1", "K3", "K4", "K4_filter30"):
        k = FIX["counterexamples"][key]
        assert static_params_valid(static_from(k["static"]), k.get("bin_step", 1)), key
    # The predicate is not vacuous: exceeding the 10 % cap or filter > decay is refused.
    bad = static_from(FIX["counterexamples"]["K3"]["static"])
    assert not static_params_valid(
        replace(bad, variable_fee_control=(1 << 24) - 1, max_volatility_accumulator=0xFFFFF), 25
    )
    assert not static_params_valid(replace(bad, filter_period=601), 1)


def test_k1_lb_bin_is_linear_not_the_cpmm_curve() -> None:
    """K1: inside one bin the exact LB output is `a - ceil(a*f/1e18)` at price 1 (linear),
    not the fixed-fee CPMM hyperbola on any reserves: an exact LB quote is not a CPMM."""
    k = FIX["counterexamples"]["K1"]
    static = static_from(k["static"])
    state = lb_state(
        active=R,
        bins={R: (int(k["bin_x"]), 0)},
        static=static,
        variable=LBVariableFeeParameters(0, 0, R, T - 3600),
    )
    book = book_from_state(state)
    fee = hand_fee(static, 0, 1)
    assert fee == int(k["fee"])
    for a_s, out_s, cpmm_s in zip(k["amounts"], k["outputs"], k["cpmm_same_reserves"], strict=True):
        a = int(a_s)
        assert a - ceil_div(a * fee, ONE) == int(out_s) == hand_quote(book, a, T)
        assert sim_quote(state, a)[0] == int(out_s)
        # Moe Classic / UniswapV2 getAmountOut on (X=bin_x, Y=bin_x) at the same fee:
        cpmm = (a * (ONE - fee) * int(k["bin_x"])) // (int(k["bin_x"]) * ONE + a * (ONE - fee))
        assert cpmm == int(cpmm_s) < int(out_s)


def test_k3_reachable_state_has_the_recorded_fee_tuple() -> None:
    k = FIX["counterexamples"]["K3"]
    s1 = k3_state()
    v = s1.variable_fee
    assert v is not None
    # Hand prediction: the prior swap reset the references (dt >= decay) at id R+10,
    # then accumulated min(10 * 1e4, maxVolAcc) by the time it reached id R.
    assert (s1.active_id, v.id_reference, v.volatility_reference, v.time_of_last_update) == (
        R,
        R + 10,
        0,
        T,
    )
    assert v.volatility_accumulator == min(10 * BP, k["static"]["max_volatility_accumulator"])
    assert [list(map(str, s1.bins[R])), list(map(str, s1.bins[R + 1]))] == k["post_bins"]


def test_k3_nonconcave_output_breaks_the_tangent_bound() -> None:
    """K3: a Y -> X swap moving back toward idReference inside the filter period sees the
    volatility accumulator *fall* bin by bin (1e5 -> 9e4); the fee drop exceeds the 1-bp
    price step, the marginal rate rises and the exact output is not concave. The
    in-bin tangent at z (slope (1e18 - f0)/1e18 at price 1) then lies *below* the exact
    output at u: no local tangent/secant bound is valid for LB without a concavity
    proof."""
    k = FIX["counterexamples"]["K3"]
    s1 = k3_state()
    book = book_from_state(s1)
    z, m, u = (int(a) for a in k["points"])
    qs = [hand_quote(book, a, T) for a in (z, m, u)]
    assert [str(q) for q in qs] == k["outputs"]
    assert [sim_quote(s1, a)[0] for a in (z, m, u)] == qs
    f0 = hand_fee(book.static, hand_vol(book.static, book.fees, R), 1)
    f1 = hand_fee(book.static, hand_vol(book.static, book.fees, R + 1), 1)
    assert (f0, f1) == (int(k["fee_bin_r"]), int(k["fee_bin_r1"]))
    slope_r = Fraction(ONE - f0, ONE)
    slope_r1 = Fraction(ONE - f1, ONE) * Fraction(Q128, hand_price(R + 1, 1))
    assert slope_r1 > slope_r  # the continuous marginal rate increases across the bin edge
    s_zm = Fraction(qs[1] - qs[0], m - z)
    s_mu = Fraction(qs[2] - qs[1], u - m)
    assert s_mu > s_zm  # integer secants increase: not concave
    tangent = qs[0] + slope_r * (u - z)
    assert qs[2] > tangent
    assert qs[2] - tangent > Fraction(int(k["tangent_violation_min"]))


def test_k5_same_book_other_timestamp_is_concave_and_differs() -> None:
    """K5: the same bins and fee tuple quoted at T + 700 (dt >= decayPeriod) reset
    idReference to the active bin: fees rise across bins, secants fall, and every output
    differs from the T quote -- the quote is a function of the frozen timestamp too."""
    k = FIX["counterexamples"]["K5"]
    s1 = k3_state()
    later = replace(s1, block_timestamp=T + int(k["dt"]))
    book = book_from_state(later)
    z, m, u = (int(a) for a in FIX["counterexamples"]["K3"]["points"])
    qs = [hand_quote(book, a, T + int(k["dt"])) for a in (z, m, u)]
    assert [str(q) for q in qs] == k["outputs"]
    assert [sim_quote(later, a)[0] for a in (z, m, u)] == qs
    assert Fraction(qs[2] - qs[1], u - m) < Fraction(qs[1] - qs[0], m - z)
    assert all(
        str(q) != o for q, o in zip(qs, FIX["counterexamples"]["K3"]["outputs"], strict=True)
    )


def _k4_state(static: LBStaticFeeParameters) -> LiquidityBookPoolState:
    k = FIX["counterexamples"]["K4"]
    return lb_state(
        active=R,
        bins={R: (int(k["bin_x"]), 0), R + 1: (int(k["bin_x"]), 0)},
        static=static,
        variable=LBVariableFeeParameters(0, 0, R, T - 3600),
    )


@pytest.mark.parametrize("key", ["K4", "K4_filter30"])
def test_k4_sequential_versus_merged_depends_on_filter_period(key: str) -> None:
    """K4: two same-direction swaps a then b in one second versus one swap of a + b. With
    filterPeriod 0 (protocol-valid) the second swap resets idReference and decays the
    reference, so splitting pays a lower fee: the pool is path dependent and has no fixed
    trading function of its reserves. With filterPeriod 30 the fee schedule is continued
    and the two agree exactly here."""
    k = FIX["counterexamples"][key]
    static = static_from(k["static"])
    state = _k4_state(static)
    a, b = int(k["a"]), int(k["b"])
    book = book_from_state(state)
    merged = hand_quote(book, a + b, T)
    first = hand_swap(book, a, T)
    assert first is not None
    seq = first[0] + hand_quote(first[1], b, T)
    assert [str(merged), str(seq)] == [k["merged"], k["sequential"]]
    sim_merged = sim_quote(state, a + b)[0]
    out1, s2 = sim_quote(state, a)
    assert (sim_merged, out1 + sim_quote(s2, b)[0]) == (merged, seq)
    if key == "K4":
        assert seq - merged > int(k["min_gain"])
    else:
        assert 0 <= merged - seq <= 2  # the same fee schedule; split rounding only


# =========================================================================================
# 4. Fork evidence (deployed LBPair): the fee is state, not a constant
# =========================================================================================


@dataclass
class ForkSwap:
    file: str
    key: tuple[str, str, int]
    swap_for_y: bool
    events: list[dict[str, Any]]


@cache
def fork_swaps() -> tuple[ForkSwap, ...]:
    out: list[ForkSwap] = []
    for name in FORK_FILES:
        lines = gzip.decompress((FORK / f"{name}.jsonl.gz").read_bytes()).decode().splitlines()
        recs = [json.loads(line) for line in lines if line.strip()]
        swaps: dict[tuple[str, str, int], dict[str, Any]] = {}
        events: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
        for r in recs:
            if r["kind"] in ("swap", "event"):
                key = (r["scenario"], r["sequence"], int(r["step"]))
                if r["kind"] == "swap":
                    swaps[key] = r
                else:
                    events[key].append(r)
        for key, evs in events.items():
            evs.sort(key=lambda e: int(e["index"]))
            out.append(ForkSwap(name, key, bool(swaps[key]["swap_for_y"]), evs))
    return tuple(out)


def fork_facts() -> dict[str, Any]:
    swaps = fork_swaps()
    multi = dec = pairs = rises = 0
    for s in swaps:
        vols = [int(e["volatility_accumulator"]) for e in s.events]
        multi += len(set(vols)) > 1
        dec += any(b < a for a, b in zip(vols, vols[1:], strict=False))
        side_in = 0 if s.swap_for_y else 1
        rates = [
            Fraction(
                int(e["amounts_out"][1 - side_in]),
                int(e["amounts_in"][side_in]) + int(e["protocol_fees"][side_in]),
            )
            for e in s.events[:-1]  # drained bins (the last may be partial)
        ]
        for a, b in zip(rates, rates[1:], strict=False):
            pairs += 1
            rises += b > a
    repeat = {
        s.key[2]: [int(e["volatility_accumulator"]) for e in s.events]
        for s in swaps
        if s.file == "real_wmnt_usdt_15" and s.key[1] == "repeat_x_to_y"
    }
    return {
        "swaps_with_events": len(swaps),
        "swaps_with_several_fee_levels": multi,
        "swaps_with_falling_accumulator": dec,
        "drained_bin_pairs": pairs,
        "realized_rate_increases": rises,
        "repeat_wmnt_usdt_15_first_bin_accumulator": {
            str(step): vols[0] for step, vols in sorted(repeat.items())
        },
        "repeat_wmnt_usdt_15_step0_last_bin_accumulator": repeat[0][-1],
    }


def test_fork_evidence_fee_is_dynamic_and_stateful() -> None:
    facts = fork_facts()
    assert facts == FIX["fork_facts"]
    assert facts["swaps_with_several_fee_levels"] > 0  # not a fixed fee within one swap
    assert facts["swaps_with_falling_accumulator"] > 0  # the fee can fall within a swap
    # The same input twice in one second: the second swap starts where the first ended.
    first = facts["repeat_wmnt_usdt_15_first_bin_accumulator"]
    assert first["1"] == facts["repeat_wmnt_usdt_15_step0_last_bin_accumulator"] != first["0"]
    # Observed parameters: no realized per-bin rate increase (support for the static
    # screen of memo §4.4 on these parameter sets, not a protocol guarantee).
    assert facts["realized_rate_increases"] == 0


@cache
def _fork_pristine_states() -> dict[str, LiquidityBookPoolState]:
    """Pristine pair states via the committed fork-evidence loader (read-only reuse)."""
    spec = importlib.util.spec_from_file_location(
        "_lb_fork_loader", REPO / "tests" / "pools" / "test_liquidity_book.py"
    )
    assert spec is not None and spec.loader is not None
    tlb: Any = importlib.util.module_from_spec(spec)
    sys.modules["_lb_fork_loader"] = tlb
    spec.loader.exec_module(tlb)
    return {
        f"{name}:{sc.name}": sc.state
        for name in FORK_FILES
        for sc in tlb.load(name).scenarios.values()
    }


def hand_schedule(state: LiquidityBookPoolState, swap_for_y: bool) -> list[tuple[int, int, int]]:
    """(bin id, volatility accumulator, total fee) of every non-empty bin the swap would
    visit, in traversal order, by the hand rules (one swap from `state`)."""
    s, v = state.static_fee, state.variable_fee
    assert s is not None and v is not None
    f = hand_references(
        s,
        HandFeeState(
            state.active_id,
            v.volatility_accumulator,
            v.volatility_reference,
            v.id_reference,
            v.time_of_last_update,
        ),
        state.block_timestamp,
    )
    side = 1 if swap_for_y else 0
    ids = sorted(
        (i for i, b in state.bins.items() if b[side] > 0),
        reverse=swap_for_y,
    )
    ids = [i for i in ids if (i <= state.active_id if swap_for_y else i >= state.active_id)]
    out = []
    for i in ids:
        vol = hand_vol(s, f, i)
        out.append((i, vol, hand_fee(s, vol, state.bin_step)))
    return out


def test_hand_schedule_matches_the_deployed_contract_accumulators() -> None:
    """Every first swap from a pristine fork state: the hand accumulator of each visited
    bin equals the deployed contract's `Swap` event value."""
    states = _fork_pristine_states()
    checked = 0
    for s in fork_swaps():
        if s.key[2] != 0:
            continue
        state = states[f"{s.file}:{s.key[0]}"]
        sched = {i: vol for i, vol, _ in hand_schedule(state, s.swap_for_y)}
        for e in s.events:
            assert sched[int(e["id"])] == int(e["volatility_accumulator"]), (s.file, s.key)
            checked += 1
    assert checked == FIX["fork_schedule_bins_checked"]


# =========================================================================================
# 5. Lemma L1: the rational piecewise-linear relaxation dominates the integer quote
# =========================================================================================


def relaxation(state: LiquidityBookPoolState, swap_for_y: bool, x: int) -> Fraction | None:
    """R(x): the swap's own fee schedule (hand rules) and the protocol's bin prices, with
    every rounding removed. Slope per bin (1 - f)*p (X -> Y) or (1 - f)/p (Y -> X); drain
    cost = reserve_out / slope. None above the whole visited book."""
    left = Fraction(x)
    total = Fraction(0)
    side = 1 if swap_for_y else 0
    for i, _vol, fee in hand_schedule(state, swap_for_y):
        p = Fraction(lb.get_price_from_id(i, state.bin_step), Q128)
        slope = Fraction(ONE - fee, ONE) * (p if swap_for_y else 1 / p)
        out = state.bins[i][side]
        cost = out / slope
        if left >= cost:
            total += out
            left -= cost
        else:
            return total + left * slope
    return None if left > 0 else total


def slopes(state: LiquidityBookPoolState, swap_for_y: bool) -> list[Fraction]:
    return [
        Fraction(ONE - fee, ONE)
        * (
            Fraction(lb.get_price_from_id(i, state.bin_step), Q128)
            if swap_for_y
            else Fraction(Q128, lb.get_price_from_id(i, state.bin_step))
        )
        for i, _v, fee in hand_schedule(state, swap_for_y)
    ]


def is_concave(state: LiquidityBookPoolState, swap_for_y: bool) -> bool:
    s = slopes(state, swap_for_y)
    return all(b <= a for a, b in zip(s, s[1:], strict=False))


def _l1_check(state: LiquidityBookPoolState, swap_for_y: bool, x: int) -> bool | None:
    token = state.token0 if swap_for_y else state.token1
    res = lb.quote_exact_in(state, token, x)
    if res.status is not QuoteStatus.OK:
        return None
    bound = relaxation(state, swap_for_y, x)
    assert bound is not None, "a filled integer swap must lie inside the relaxation's book"
    return res.amount_out <= bound


def rounding_gap_ratio(
    state: LiquidityBookPoolState, swap_for_y: bool, x: int, out: int, bins_swapped: int
) -> Fraction:
    """(R(x) - q(x) - 1) / ((2k + 1) * max slope): <= 1 means the relaxation gap is within
    the rounding allowance of k visited bins (memo §4.6; observed, not a proof)."""
    bound = relaxation(state, swap_for_y, x)
    assert bound is not None
    return (bound - out - 1) / ((2 * bins_swapped + 1) * max(slopes(state, swap_for_y)))


def random_state(rng: random.Random) -> LiquidityBookPoolState:
    bs = rng.choice((1, 2, 5, 10, 25, 100))
    while True:
        static = LBStaticFeeParameters(
            base_factor=rng.randrange(0, 30_000),
            filter_period=rng.choice((0, 10, 30)),
            decay_period=rng.choice((120, 600)),
            reduction_factor=rng.choice((0, 5_000, 10_000)),
            variable_fee_control=rng.choice((0, 10**4, 10**5, 10**6, 10**7, (1 << 24) - 1)),
            protocol_share=rng.choice((0, 1_000, 2_500)),
            max_volatility_accumulator=rng.choice((10**4, 10**5, 350_000, 0xFFFFF)),
        )
        if static_params_valid(static, bs):
            break
    active = R + rng.randrange(-3, 4)
    bins: dict[int, tuple[int, int]] = {}
    for i in range(active - 12, active + 13):
        if rng.random() < 0.25 and i != active:
            continue  # an empty bin
        size = rng.choice((1, 10**3, 10**9, 10**18)) * rng.randrange(1, 1000)
        if i < active:
            bins[i] = (0, size)
        elif i > active:
            bins[i] = (size, 0)
        else:
            bins[i] = (size, rng.choice((0, size)))
    variable = LBVariableFeeParameters(
        volatility_accumulator=rng.randrange(0, static.max_volatility_accumulator + 1),
        volatility_reference=rng.randrange(0, static.max_volatility_accumulator + 1),
        id_reference=active + rng.randrange(-15, 16),
        time_of_last_update=T - rng.choice((0, 5, 100, 5_000)),
    )
    return lb_state(active=active, bins=bins, static=static, variable=variable, bin_step=bs)


def test_l1_relaxation_dominates_integer_quotes_on_random_valid_states() -> None:
    rng = random.Random(FIX["l1_random"]["seed"])
    checks = nonconcave = 0
    worst = Fraction(0)
    for _ in range(FIX["l1_random"]["states"]):
        st = random_state(rng)
        for d in (True, False):
            if not hand_schedule(st, d):
                continue
            nonconcave += not is_concave(st, d)
            side = 1 if d else 0
            book = sum(b[side] for b in st.bins.values())
            for _k in range(6):
                x = max(1, int(book * rng.random() ** 2 * 1.3))
                ok = _l1_check(st, d, x)
                if ok is None:
                    continue
                checks += 1
                assert ok, (st, d, x)
                res = lb.quote_exact_in(st, st.token0 if d else st.token1, x)
                ratio = rounding_gap_ratio(
                    st, d, x, res.amount_out, res.features["lb_bins_swapped"]
                )
                worst = max(worst, ratio)
    assert checks >= FIX["l1_random"]["min_checks"]
    assert worst <= 1  # the relaxation gap stays within the per-bin rounding allowance
    assert nonconcave >= 1  # L1 does not need concavity; the random suite contains both


def test_l1_on_k3_and_fork_states() -> None:
    s1 = k3_state()
    assert not is_concave(s1, False)  # K3 is nonconcave, L1 still holds
    for a in FIX["counterexamples"]["K3"]["points"]:
        assert _l1_check(s1, False, int(a)) is True
    for st in _fork_pristine_states().values():
        for d in (True, False):
            side = 1 if d else 0
            book = sum(b[side] for b in st.bins.values())
            for num in (1, 7, 60, 400, 999):
                x = max(1, book * num // 1000)
                assert _l1_check(st, d, x) in (True, None)


# =========================================================================================
# 6. Sequential versus merged on real fork states (filterPeriod > 0)
# =========================================================================================


def test_real_states_sequential_equals_merged_up_to_rounding() -> None:
    worst = 0
    for st in _fork_pristine_states().values():
        if st.static_fee is None or st.static_fee.filter_period == 0:
            continue
        for d, token in ((True, st.token0), (False, st.token1)):
            reserve_in_units = st.reserve_x if d else st.reserve_y
            for div in (30, 70):
                a = reserve_in_units // div
                if a == 0:
                    continue
                m = lb.quote_exact_in(st, token, 2 * a)
                r1 = lb.quote_exact_in(st, token, a)
                if m.status is not QuoteStatus.OK or r1.status is not QuoteStatus.OK:
                    continue
                assert r1.new_state is not None
                r2 = lb.quote_exact_in(r1.new_state, token, a)
                assert r2.status is QuoteStatus.OK
                gap = m.amount_out - (r1.amount_out + r2.amount_out)
                # rounding only: at most what 3 input units buy at the best bin rate + 2
                best = max(slopes(st, d))
                assert 0 <= gap <= 3 * best + 2, (st.pool_id, d, a, gap)
                worst = max(worst, gap)
    assert worst > 0  # not identical either: merging and splitting are distinct plans


# =========================================================================================
# 7. Executable specification of the proposed `uni_sor_lb` (never registered)
# =========================================================================================

LB_FAMILY, MIXED_LB_FAMILY = "LB", "MIXED_LB"
BLOCKS = (*usp.FAMILIES, LB_FAMILY, MIXED_LB_FAMILY)


@dataclass(frozen=True)
class SpecResult:
    status: SolveStatus
    blocks: Mapping[str, int]
    entries: int
    selection: usp.SwapSelection | None
    plan: RoutePlan | None
    evaluation: Evaluation | None
    residual: int | None
    quotes_executed: int


def spec_candidates(
    bundle: SnapshotBundle, include_lb: bool
) -> tuple[list[usp.SorPool], list[usp.SorPool], list[usp.SorPool]]:
    """A-1 plus A-LB1: V3 and V2 lists exactly as `uni_sor_port.prepare`; every admitted
    `moe_lb_v2_2` pool in a third list, same ascending pool-id order."""
    sor = load_catalog(usp.DEFAULT_CATALOG).sor_protocols()
    v3: list[usp.SorPool] = []
    v2: list[usp.SorPool] = []
    lbl: list[usp.SorPool] = []
    for st in sorted(bundle.pools.values(), key=lambda s: s.pool_id.lower()):
        protocol = usp.sor_protocol_of(st, sor)
        if protocol is None:
            if include_lb and isinstance(st, LiquidityBookPoolState):
                assert st.source_key == "moe_lb_v2_2"
                lbl.append(usp.SorPool(st.pool_id, st.token0.lower(), st.token1.lower(), "LB"))
            continue
        (v3 if protocol == usp.V3 else v2).append(usp._sor_pool(st, protocol))
    return v3, v2, lbl


def spec_routes(
    case: Case,
    v3: Sequence[usp.SorPool],
    v2: Sequence[usp.SorPool],
    lbl: Sequence[usp.SorPool],
    max_hops: int,
) -> dict[str, list[usp.SorRoute]]:
    """A-LB2: upstream's V3, V2, MIXED blocks unchanged, then LB (DFS over the LB list) and
    MIXED_LB (DFS over V3 ++ V2 ++ LB keeping routes with >= 1 LB and >= 1 non-LB pool)."""
    routes = {
        fam: usp.compute_family_routes(fam, case.token_in, case.token_out, v3, v2, max_hops)
        for fam in usp.FAMILIES
    }
    t_in = case.token_in.lower()
    routes[LB_FAMILY] = [
        usp.SorRoute(LB_FAMILY, r, usp._token_path(t_in, r))
        for r in usp.compute_all_routes(case.token_in, case.token_out, lbl, max_hops)
    ]
    routes[MIXED_LB_FAMILY] = [
        usp.SorRoute(MIXED_LB_FAMILY, r, usp._token_path(t_in, r))
        for r in usp.compute_all_routes(case.token_in, case.token_out, [*v3, *v2, *lbl], max_hops)
        if any(p.protocol == "LB" for p in r) and not all(p.protocol == "LB" for p in r)
    ]
    return routes


def lb_sor_select(
    bundle: SnapshotBundle,
    case: Case,
    params: Mapping[str, int],
    *,
    include_lb: bool = True,
) -> SpecResult:
    """The proposed selection (memo §5.2) for one case; `params` are the shared
    `search.max_hops`, `search.max_splits` and `search.percent_step`."""
    max_hops, max_splits = params["max_hops"], params["max_splits"]
    percent_step = params["percent_step"]
    v3, v2, lbl = spec_candidates(bundle, include_lb)
    routes = spec_routes(case, v3, v2, lbl, max_hops)
    blocks = {fam: len(routes[fam]) for fam in BLOCKS}
    cache = QuoteCache(bundle)
    if sum(blocks.values()) == 0:
        return SpecResult(SolveStatus.NO_ROUTE, blocks, 0, None, None, None, None, 0)
    percents, amounts = usp.amount_distribution(case.amount_in, percent_step)

    def quote(route: usp.SorRoute, _percent: int, amount: usp.Rational) -> int | None:
        # A-2 null rules unchanged; LB is all-or-nothing, so a partial fill never occurs.
        x = amount.quotient
        if x == 0:
            return None
        pure_v2 = all(p.protocol == usp.V2 for p in route.pools)
        for edge in usp._plan_legs(route, bundle):
            if x == 0:
                return None
            res = cache(bundle.pools[edge.pool_id], edge.token_in, x)
            if res.status is not QuoteStatus.OK or res.amount_in_consumed != x:
                return None
            x = res.amount_out
            if pure_v2 and x == 0:
                return None
        return x

    upstream = {fam: routes[fam] for fam in usp.FAMILIES}
    table = usp.build_route_quotes(upstream, percents, amounts, quote)
    for fam in (LB_FAMILY, MIXED_LB_FAMILY):  # A-LB3: appended after upstream's blocks
        for route in routes[fam]:
            for percent, amount in zip(percents, amounts, strict=True):
                raw = quote(route, percent, amount)
                if raw is not None:
                    table.append(usp.RouteQuote(len(table), route, percent, amount, raw))
    selection = usp.get_best_swap_route(case.amount_in, percents, table, max_splits=max_splits)
    if selection is None:
        return SpecResult(
            SolveStatus.NO_ROUTE, blocks, len(table), None, None, None, None, cache.misses
        )
    plan = split_path_plan(
        case,
        tuple(usp._plan_legs(r.route, bundle) for r in selection.routes),
        tuple(r.percent for r in selection.routes),
    )
    evaluation = evaluate(bundle, case, plan, gross_only(), quote=cache)
    allocation = usp.integer_fill(case.amount_in, selection)
    residual = allocation[-1] - selection.amounts[-1].quotient
    status = SolveStatus.OK if evaluation.status is EvalStatus.OK else SolveStatus.INVALID_PLAN
    return SpecResult(
        status, blocks, len(table), selection, plan, evaluation, residual, cache.misses
    )


def _port(bundle: SnapshotBundle, case: Case, params: Mapping[str, int]) -> Any:
    prepared = usp.prepare(bundle, AlgorithmConfig(usp.NAME, dict(params)))
    return usp.solve(case, SolveContext(bundle, gross_only(), prepared), Budget())


SPEC_PARAMS = {"max_hops": 3, "max_splits": 4, "percent_step": 5}
SPEC_PARAMS_MOE = {"max_hops": 2, "max_splits": 4, "percent_step": 5}


def test_spec_blocks_partition_the_union_route_set() -> None:
    bundle = load_bundle(MANTLE_MIXED)
    v3, v2, lbl = spec_candidates(bundle, True)
    for case in bundle.cases:
        routes = spec_routes(case, v3, v2, lbl, 3)
        blocks = [r.pool_ids for fam in BLOCKS for r in routes[fam]]
        union = [
            tuple(p.pool_id for p in r)
            for r in usp.compute_all_routes(case.token_in, case.token_out, [*v3, *v2, *lbl], 3)
        ]
        assert sorted(blocks) == sorted(union) and len(set(blocks)) == len(blocks)


def test_spec_reduces_to_uni_sor_port_without_lb() -> None:
    """Parity boundary untouched: with no LB candidate the spec's route blocks, quote
    table, selection and plan are exactly `uni_sor_port`'s."""
    bundle = load_bundle(MANTLE_MIXED)
    for case in bundle.cases:
        spec = lb_sor_select(bundle, case, SPEC_PARAMS, include_lb=False)
        port = _port(bundle, case, SPEC_PARAMS)
        assert port.status is SolveStatus.OK and spec.status is SolveStatus.OK
        assert spec.plan == port.plan
        assert spec.evaluation is not None and port.evaluation is not None
        assert spec.evaluation.gross_output == port.evaluation.gross_output
        assert spec.blocks[LB_FAMILY] == spec.blocks[MIXED_LB_FAMILY] == 0


def test_spec_quote_table_is_exact_and_pool_disjoint() -> None:
    """With LB: selected routes are physical-pool disjoint, the plan replays `ok`, and the
    cached table sum equals the evaluator's gross exactly when the D-1 residual is 0 (each
    LB pool is swapped once, from its snapshot state, at the tabled amount)."""
    bundle = load_bundle(MANTLE_MIXED)
    expected = FIX["spec_mantle_mixed"]
    seen: dict[str, Any] = {}
    for case in bundle.cases:
        spec = lb_sor_select(bundle, case, SPEC_PARAMS)
        assert spec.status is SolveStatus.OK and spec.selection is not None
        assert spec.evaluation is not None
        ids = [pid for r in spec.selection.routes for pid in r.pool_identifiers]
        assert len(ids) == len(set(ids))
        if spec.residual == 0:
            assert spec.evaluation.gross_output == spec.selection.swap.quote
        seen[case.case_id] = {
            "routes": [
                [r.protocol, list(r.pool_identifiers), r.percent] for r in spec.selection.routes
            ],
            "evaluated_gross": str(spec.evaluation.gross_output),
            "cached_quote": str(spec.selection.swap.quote),
            "residual": spec.residual,
            "blocks": dict(spec.blocks),
        }
    assert seen == expected
    # Physical conflict with two LB pools of one pair (bin steps 15 and 25): both usable.
    both = {
        "0xf6c9020c9e915808481757779edb53daceae2415",
        "0x365722f12ceb2063286a268b03c654df81b7c00f",
    }
    assert both <= {p for r in expected["usdt_wmnt_evidence_split"]["routes"] for p in r[1]}


def test_spec_turns_lb_only_unsupported_into_routes() -> None:
    """Expanded protocol coverage on the LB-only admission bundle: `uni_sor_port` is
    `unsupported` (D-4); the spec fills, including a case `direct` cannot (book
    exhaustion), and keeps dust as no_route."""
    bundle = load_bundle(MOE_LB)
    params = SPEC_PARAMS_MOE
    for cid, want in FIX["spec_moe_lb"].items():
        case = bundle.case(cid)
        assert _port(bundle, case, params).status is SolveStatus.UNSUPPORTED
        spec = lb_sor_select(bundle, case, params)
        assert spec.status.value == want["status"]
        if spec.evaluation is not None:
            assert str(spec.evaluation.gross_output) == want["evaluated_gross"]


# =========================================================================================
# 8. The five R021-C/1 identities gain no LB capability; uni_sor_port is untouched
# =========================================================================================


def test_no_unproved_lb_capability_for_the_five_identities() -> None:
    ids = {i["id"]: i for i in CONTRACT["identities"]}
    with_lb = {k for k, v in ids.items() if "liquidity_book" in v["protocols_ceiling"]}
    assert with_lb == {"metis_history", "incremental_graph_repair"}  # exact-quote heuristics
    for k in ("direct_split_certified", "uni_sor_cycle_safe", "cfmm_dual"):
        assert "liquidity_book" not in ids[k]["protocols_ceiling"]
    assert usp.CAPABILITIES.protocols == (usp.V2, usp.V3)
    sor = load_catalog(usp.DEFAULT_CATALOG).sor_protocols()
    assert "moe_lb_v2_2" not in sor
    for path in (MANTLE_MIXED, MOE_LB):
        for st in load_bundle(path).pools.values():
            if isinstance(st, LiquidityBookPoolState):
                assert usp.sor_protocol_of(st, sor) is None
    assert FIX["proposed_identity"]["id"] not in ALGORITHMS
    assert FIX["proposed_identity"]["id"] not in ids
    for line in FIX["memo_required_lines"]:
        assert line in MEMO, line


def test_memo_numbers_match_the_fixture() -> None:
    cx = FIX["counterexamples"]
    numbers = [
        cx["K1"]["outputs"][0],
        cx["K1"]["cpmm_same_reserves"][0],
        *cx["K3"]["outputs"],
        *cx["K5"]["outputs"],
        cx["K4"]["merged"],
        cx["K4"]["sequential"],
    ]
    for n in numbers:
        assert f"{int(n):,}" in MEMO, n
    ff = FIX["fork_facts"]
    for key in (
        "swaps_with_events",
        "swaps_with_several_fee_levels",
        "swaps_with_falling_accumulator",
    ):
        assert f"| {ff[key]} |" in MEMO, key
    assert f"0 of {ff['drained_bin_pairs']:,}" in MEMO
    assert f"{FIX['fork_schedule_bins_checked']:,} bins" in MEMO
    census = FIX["tuning_probe"]["result"]["route_census"]
    for row in ("routes_cohort", "routes_with_all_lb", "routes_with_liquid_lb_only"):
        assert f"| {census['2'][row]:,} | {census['3'][row]:,} |" in MEMO, row


# =========================================================================================
# 9. Tuning-split census (CLI) and fixture regeneration
# =========================================================================================


def probe(bundle_dir: str) -> dict[str, Any]:
    bundle = load_bundle(bundle_dir)
    lb_states = [s for s in bundle.pools.values() if isinstance(s, LiquidityBookPoolState)]
    liquid = [s for s in lb_states if s.bins]
    dirs = reset = concave = 0
    for st in liquid:
        s, v = st.static_fee, st.variable_fee
        assert s is not None and v is not None
        for d in (True, False):
            if not hand_schedule(st, d):
                continue
            dirs += 1
            reset += st.block_timestamp - v.time_of_last_update >= s.filter_period
            concave += is_concave(st, d)
    screen: set[tuple[int, LBStaticFeeParameters]] = set()
    screen_pass = 0
    for st in lb_states:
        s = st.static_fee
        assert s is not None
        if (st.bin_step, s) in screen:
            continue
        screen.add((st.bin_step, s))
        mv = s.max_volatility_accumulator
        drop = hand_fee(s, mv, st.bin_step) - hand_fee(s, max(mv - BP, 0), st.bin_step)
        screen_pass += Fraction(drop, ONE) <= Fraction(
            ONE - hand_fee(s, mv, st.bin_step), ONE
        ) * Fraction(st.bin_step, BP + st.bin_step)
    rng = random.Random(1560)
    l1_checks = l1_viol = 0
    worst = Fraction(0)
    for st in liquid:
        for d in (True, False):
            sched = hand_schedule(st, d)
            if not sched:
                continue
            side = 1 if d else 0
            depth = sum(
                Fraction(st.bins[i][side]) / sl
                for (i, _v, _f), sl in zip(sched, slopes(st, d), strict=True)
            )
            for _ in range(40):
                x = max(1, int(depth * Fraction(rng.random()) ** 3))
                token = st.token0 if d else st.token1
                res = lb.quote_exact_in(st, token, x)
                if res.status is not QuoteStatus.OK:
                    continue
                bound = relaxation(st, d, x)
                assert bound is not None
                l1_checks += 1
                l1_viol += res.amount_out > bound
                worst = max(
                    worst,
                    rounding_gap_ratio(st, d, x, res.amount_out, res.features["lb_bins_swapped"]),
                )
    v3, v2, lbl = spec_candidates(bundle, True)
    liquid_ids = {s.pool_id for s in liquid}
    census: dict[str, Any] = {}
    for hops in (2, 3):
        cohort = all_lb = with_liquid = lb_only = 0
        for case in bundle.cases:
            rc = usp.compute_all_routes(case.token_in, case.token_out, [*v3, *v2], hops)
            ra = usp.compute_all_routes(case.token_in, case.token_out, [*v3, *v2, *lbl], hops)
            cohort += len(rc)
            all_lb += len(ra)
            with_liquid += sum(
                1 for r in ra if all(p.protocol != "LB" or p.pool_id in liquid_ids for p in r)
            )
            lb_only += bool(ra) and not rc
        census[str(hops)] = {
            "routes_cohort": cohort,
            "routes_with_all_lb": all_lb,
            "routes_with_liquid_lb_only": with_liquid,
            "lb_only_cases": lb_only,
        }
    return {
        "bundle_id": bundle.bundle_id,
        "bundle_hash": bundle.bundle_hash,
        "cases": len(bundle.cases),
        "lb_pools": len(lb_states),
        "lb_pools_with_bins": len(liquid),
        "pool_directions": dirs,
        "directions_reset_at_first_swap": reset,
        "directions_concave_relaxation": concave,
        "distinct_static_sets": len(screen),
        "static_screen_pass": screen_pass,
        "l1_checks": l1_checks,
        "l1_violations": l1_viol,
        "l1_max_rounding_gap_ratio": f"{float(worst):.3f}",
        "route_census": census,
    }


def test_tuning_probe_matches_fixture_when_the_corpus_is_present() -> None:
    rec = FIX["tuning_probe"]
    path = Path(rec["path_hint"])
    if not (path / "manifest.json").is_file():
        pytest.skip("gitignored corpus split not present in this checkout")
    got = probe(str(path))
    assert got == rec["result"]


def build_fixture(probe_json: str | None) -> dict[str, Any]:
    fix = dict(FIX)
    cx = fix["counterexamples"]
    # K1 (hand)
    k1 = cx["K1"]
    st1 = static_from(k1["static"])
    fee = hand_fee(st1, 0, 1)
    book1 = HandBook(1, st1, HandFeeState(R, 0, 0, R, T - 3600), (int(k1["bin_x"]), 0))
    bx = int(k1["bin_x"])
    k1["fee"] = str(fee)
    k1["outputs"] = [str(hand_quote(book1, int(a), T)) for a in k1["amounts"]]
    k1["cpmm_same_reserves"] = [
        str((int(a) * (ONE - fee) * bx) // (bx * ONE + int(a) * (ONE - fee))) for a in k1["amounts"]
    ]
    # K3 (post state from the admitted transition = input; outputs from the hand model)
    s1 = k3_state()
    cx["K3"]["post_bins"] = [list(map(str, s1.bins[R])), list(map(str, s1.bins[R + 1]))]
    b3 = book_from_state(s1)
    cx["K3"]["outputs"] = [str(hand_quote(b3, int(a), T)) for a in cx["K3"]["points"]]
    cx["K3"]["fee_bin_r"] = str(hand_fee(b3.static, hand_vol(b3.static, b3.fees, R), 1))
    cx["K3"]["fee_bin_r1"] = str(hand_fee(b3.static, hand_vol(b3.static, b3.fees, R + 1), 1))
    dt = int(cx["K5"]["dt"])
    b5 = book_from_state(replace(s1, block_timestamp=T + dt))
    cx["K5"]["outputs"] = [str(hand_quote(b5, int(a), T + dt)) for a in cx["K3"]["points"]]
    for key in ("K4", "K4_filter30"):
        k = cx[key]
        b4 = book_from_state(_k4_state(static_from(k["static"])))
        a, b = int(k["a"]), int(k["b"])
        first = hand_swap(b4, a, T)
        assert first is not None
        k["merged"] = str(hand_quote(b4, a + b, T))
        k["sequential"] = str(first[0] + hand_quote(first[1], b, T))
    fix["fork_facts"] = fork_facts()
    states = _fork_pristine_states()
    fix["fork_schedule_bins_checked"] = sum(
        len(s.events) for s in fork_swaps() if s.key[2] == 0 and f"{s.file}:{s.key[0]}" in states
    )
    bundle = load_bundle(MANTLE_MIXED)
    mm: dict[str, Any] = {}
    for case in bundle.cases:
        spec = lb_sor_select(bundle, case, SPEC_PARAMS)
        assert spec.selection is not None and spec.evaluation is not None
        mm[case.case_id] = {
            "routes": [
                [r.protocol, list(r.pool_identifiers), r.percent] for r in spec.selection.routes
            ],
            "evaluated_gross": str(spec.evaluation.gross_output),
            "cached_quote": str(spec.selection.swap.quote),
            "residual": spec.residual,
            "blocks": dict(spec.blocks),
        }
    fix["spec_mantle_mixed"] = mm
    moe = load_bundle(MOE_LB)
    for cid in fix["spec_moe_lb"]:
        spec = lb_sor_select(moe, moe.case(cid), SPEC_PARAMS_MOE)
        fix["spec_moe_lb"][cid] = {
            "status": spec.status.value,
            "evaluated_gross": None
            if spec.evaluation is None
            else str(spec.evaluation.gross_output),
            "routes": None
            if spec.selection is None
            else [[r.protocol, list(r.pool_identifiers), r.percent] for r in spec.selection.routes],
        }
    if probe_json is not None:
        fix["tuning_probe"]["result"] = json.loads(Path(probe_json).read_text(encoding="utf-8"))
    return fix


def main(argv: list[str]) -> None:
    if argv[:1] == ["probe"] and len(argv) == 3:
        Path(argv[2]).write_text(json.dumps(probe(argv[1]), indent=2) + "\n", encoding="utf-8")
    elif argv[:1] == ["examples"] and len(argv) in (1, 2):
        fix = build_fixture(argv[1] if len(argv) == 2 else None)
        FIXTURE_PATH.write_text(json.dumps(fix, indent=2) + "\n", encoding="utf-8")
    else:
        raise SystemExit("usage: probe <bundle> <out.json> | examples [probe.json]")


if __name__ == "__main__":
    main(sys.argv[1:])

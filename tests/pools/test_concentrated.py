"""Offline replay of independently generated CL evidence (WHI-1428).

Every expected number here comes from `tests/fixtures/concentrated/*.jsonl`, written by
`tools/cl_evidence/test/CaptureCL.t.sol` running the *deployed* Uniswap v3 / Agni /
FusionX pool bytecode on a Mantle fork at the catalog's candidate block (real pools),
or pools deployed through the deployed factories on that fork (controlled pools, whose
bitmap is captured over the full tick range). None of it is produced by the Python
under test. Regenerate with `tools/cl_evidence/regen.sh` (see its README).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from benchmark.objective import gross_only
from pools import cl_math
from pools.cl_math import (
    MAX_SQRT_RATIO,
    MAX_TICK,
    MIN_SQRT_RATIO,
    MIN_TICK,
    SolidityRevert,
    add_delta,
    get_sqrt_ratio_at_tick,
    get_tick_at_sqrt_ratio,
)
from pools.concentrated import SOURCES, swap
from pools.concentrated import quote_exact_in as cl_quote_exact_in
from pools.quote import quote_exact_in
from pools.result import QuoteStatus
from routing.evaluator import EvalStatus, evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from snapshot.models import BlockRef, Case, ConcentratedPoolState, SnapshotBundle, TickInfo

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "concentrated"
PROTOCOLS = yaml.safe_load((REPO / "config" / "protocols.yaml").read_text())
SOURCE_KEYS = ("uniswap_v3", "agni_v3", "fusionx_v3")
SCENARIOS = ("real", "controlled")
ZERO = "0x0000000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# Evidence loading
# ---------------------------------------------------------------------------


@dataclass
class EvSwap:
    zero_for_one: bool
    amount_specified: int
    sqrt_price_limit_x96: int
    amount0: int
    amount1: int
    post: dict[str, Any]
    changed_ticks: dict[int, TickInfo]
    quote: dict[str, Any] | None = None


@dataclass
class EvSequence:
    name: str
    swaps: list[EvSwap] = field(default_factory=list)


@dataclass
class Evidence:
    path: Path
    meta: dict[str, Any]
    state: ConcentratedPoolState
    sequences: dict[str, EvSequence]
    positions: list[dict[str, Any]]


def _tick_info(rec: dict[str, Any]) -> TickInfo:
    assert rec["initialized"] is True
    return TickInfo(
        liquidity_gross=int(rec["liquidity_gross"]),
        liquidity_net=int(rec["liquidity_net"]),
        fee_growth_outside0_x128=int(rec["fee_growth_outside0_x128"]),
        fee_growth_outside1_x128=int(rec["fee_growth_outside1_x128"]),
    )


def load_evidence(source: str, scenario: str) -> Evidence:
    path = FIXTURES / f"{source}_{scenario}.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert records and records[-1] == {"kind": "end"}, f"{path.name}: truncated evidence"
    metas = [r for r in records if r["kind"] == "meta"]
    pres = [r for r in records if r["kind"] == "pre_state"]
    assert len(metas) == 1 and len(pres) == 1
    meta, pre = metas[0], pres[0]
    words = {int(r["word"]): int(r["bitmap"]) for r in records if r["kind"] == "pre_word"}
    ticks = {int(r["tick"]): _tick_info(r) for r in records if r["kind"] == "pre_tick"}
    lm_pool = None if source == "uniswap_v3" else meta["lm_pool"]
    state = ConcentratedPoolState(
        pool_id=meta["pool"],
        source_key=source,
        token0=meta["token0"],
        token1=meta["token1"],
        fee=int(meta["fee"]),
        tick_spacing=int(meta["tick_spacing"]),
        sqrt_price_x96=int(pre["sqrt_price_x96"]),
        tick=int(pre["tick"]),
        liquidity=int(pre["liquidity"]),
        fee_protocol=int(pre["fee_protocol"]),
        fee_growth_global0_x128=int(pre["fee_growth_global0_x128"]),
        fee_growth_global1_x128=int(pre["fee_growth_global1_x128"]),
        protocol_fees0=int(pre["protocol_fees0"]),
        protocol_fees1=int(pre["protocol_fees1"]),
        bitmap_word_range=(int(meta["bitmap_word_lo"]), int(meta["bitmap_word_hi"])),
        tick_bitmap=words,
        ticks=ticks,
        lm_pool=lm_pool,
    )
    sequences: dict[str, EvSequence] = {}
    by_key: dict[tuple[str, int], EvSwap] = {}
    pending_quotes: dict[tuple[str, int], dict[str, Any]] = {}
    for r in records:
        kind = r["kind"]
        if kind == "sequence":
            sequences[r["sequence"]] = EvSequence(r["sequence"])
        elif kind == "quote":
            pending_quotes[(r["sequence"], r["index"])] = r
        elif kind == "swap":
            key = (r["sequence"], r["index"])
            ev = EvSwap(
                zero_for_one=r["zero_for_one"],
                amount_specified=int(r["amount_specified"]),
                sqrt_price_limit_x96=int(r["sqrt_price_limit_x96"]),
                amount0=int(r["amount0"]),
                amount1=int(r["amount1"]),
                post={},
                changed_ticks={},
                quote=pending_quotes.pop(key, None),
            )
            assert len(sequences[r["sequence"]].swaps) == r["index"]
            sequences[r["sequence"]].swaps.append(ev)
            by_key[key] = ev
        elif kind == "post_state":
            by_key[(r["sequence"], r["index"])].post = r
        elif kind == "post_tick":
            by_key[(r["sequence"], r["index"])].changed_ticks[int(r["tick"])] = _tick_info(r)
    assert not pending_quotes
    positions = [r for r in records if r["kind"] == "setup_position"]
    return Evidence(path, meta, state, sequences, positions)


ALL = [(s, sc) for s in SOURCE_KEYS for sc in SCENARIOS]
_CACHE: dict[tuple[str, str], Evidence] = {}


def evidence(source: str, scenario: str) -> Evidence:
    key = (source, scenario)
    if key not in _CACHE:
        _CACHE[key] = load_evidence(source, scenario)
    return _CACHE[key]


def _source_cfg(source: str) -> dict[str, Any]:
    return next(s for s in PROTOCOLS["sources"] if s["key"] == source)


def _changed_ticks(
    before: ConcentratedPoolState, after: ConcentratedPoolState
) -> dict[int, TickInfo]:
    assert set(before.ticks) == set(after.ticks)
    return {t: info for t, info in after.ticks.items() if before.ticks[t] != info}


def _is_default_limit(ev: EvSwap) -> bool:
    return ev.sqrt_price_limit_x96 == (
        MIN_SQRT_RATIO + 1 if ev.zero_for_one else MAX_SQRT_RATIO - 1
    )


def replay(ev_all: Evidence) -> Iterator[tuple[str, int, EvSwap, ConcentratedPoolState, Any]]:
    """Yield (sequence, index, evidence swap, pre-swap state, python outcome); each
    sequence restarts from the captured pre-state, exactly like the harness."""
    for name, seq in ev_all.sequences.items():
        state = ev_all.state
        for idx, ev in enumerate(seq.swaps):
            outcome = swap(state, ev.zero_for_one, ev.amount_specified, ev.sqrt_price_limit_x96)
            yield name, idx, ev, state, outcome
            state = outcome.new_state


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "scenario"), ALL)
def test_evidence_provenance_matches_catalog(source: str, scenario: str) -> None:
    ev = evidence(source, scenario)
    meta = ev.meta
    block = PROTOCOLS["candidate_block"]
    assert meta["schema"] == "cl-evidence/1"
    assert meta["source_key"] == source
    assert meta["chain_id"] == PROTOCOLS["network"]["chain_id"] == 5000
    assert meta["block_number"] == block["number"]
    assert meta["block_timestamp"] == block["timestamp"]
    assert meta["block_hash"] == block["hash"]
    contracts = _source_cfg(source)["contracts"]
    assert meta["factory"].lower() == contracts["factory"]["address"].lower()
    if scenario == "real":
        assert meta["scenario"] == "real_pool"
        assert meta["pool"].lower() == contracts["example_pool"]["address"].lower()
        # The executed pool bytecode is exactly the catalog-pinned deployment.
        assert meta["pool_code_hash"] == contracts["example_pool"]["code_hash"]
    else:
        assert meta["scenario"] == "controlled_pool"
        # Full-range capture: every word a swap can ever read is collected.
        lo = (MIN_TICK // meta["tick_spacing"]) >> 8
        hi = (MAX_TICK // meta["tick_spacing"]) >> 8
        assert (meta["bitmap_word_lo"], meta["bitmap_word_hi"]) == (lo, hi)
        assert len(ev.positions) == 5


def test_fusionx_evidence_exercises_a_live_lm_pool_hook() -> None:
    # The only unmodeled LM-hook effect is a revert inside the hook; the live FusionX
    # example pool has a non-zero lmPool, so its evidence runs the real hook.
    assert evidence("fusionx_v3", "real").meta["lm_pool"] != ZERO
    assert evidence("agni_v3", "real").meta["lm_pool"] == ZERO


# ---------------------------------------------------------------------------
# Swap replay vs contract evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "scenario"), ALL)
def test_swaps_match_contract_amounts_and_next_state(source: str, scenario: str) -> None:
    ev_all = evidence(source, scenario)
    replayed = 0
    for name, idx, ev, before, out in replay(ev_all):
        where = f"{ev_all.path.name}:{name}[{idx}]"
        assert (out.amount0, out.amount1) == (ev.amount0, ev.amount1), where
        s = out.new_state
        post = ev.post
        assert s.sqrt_price_x96 == int(post["sqrt_price_x96"]), where
        assert s.tick == int(post["tick"]), where
        assert s.liquidity == int(post["liquidity"]), where
        assert s.fee_growth_global0_x128 == int(post["fee_growth_global0_x128"]), where
        assert s.fee_growth_global1_x128 == int(post["fee_growth_global1_x128"]), where
        assert s.protocol_fees0 == int(post["protocol_fees0"]), where
        assert s.protocol_fees1 == int(post["protocol_fees1"]), where
        assert s.fee_protocol == int(post["fee_protocol"]) and post["unlocked"] is True, where
        # Exactly the ticks the contract changed, with identical values.
        assert _changed_ticks(before, s) == ev.changed_ticks, where
        assert set(out.crossed_ticks) == set(ev.changed_ticks), where
        replayed += 1
    assert replayed >= 10


@pytest.mark.parametrize(("source", "scenario"), ALL)
def test_quote_exact_in_matches_contract_and_never_mutates(source: str, scenario: str) -> None:
    ev_all = evidence(source, scenario)
    for name, idx, ev, before, out in replay(ev_all):
        if not _is_default_limit(ev):
            continue
        snapshot_before = dataclasses.replace(before)
        token_in = before.token0 if ev.zero_for_one else before.token1
        result = quote_exact_in(before, token_in, ev.amount_specified)
        consumed = ev.amount0 if ev.zero_for_one else ev.amount1
        produced = -(ev.amount1 if ev.zero_for_one else ev.amount0)
        if consumed == ev.amount_specified:
            assert result.status is QuoteStatus.OK, (name, idx, result.detail)
            assert result.amount_in_consumed == consumed
            assert result.amount_out == produced
            assert result.new_state == out.new_state
            assert result.features["initialized_ticks_crossed"] == len(ev.changed_ticks)
        else:
            # Partial fill on the widest legal limit over fully collected state.
            assert scenario == "controlled"
            assert result.status is QuoteStatus.INSUFFICIENT_LIQUIDITY, (name, idx)
            assert result.new_state is None and result.amount_out == 0
        assert before == snapshot_before
        if ev.quote is not None:
            # Deployed QuoterV2 at the same block agrees with the swap and with Python.
            assert int(ev.quote["amount_in"]) == ev.amount_specified
            assert int(ev.quote["amount_out"]) == produced == result.amount_out
            assert int(ev.quote["sqrt_price_x96_after"]) == out.new_state.sqrt_price_x96


def test_evidence_covers_required_case_classes() -> None:
    """AC1/AC2: small, boundary-crossing, multi-tick, insufficient-liquidity, dust and
    two-sequential-swap cases exist, in both directions, for every source."""
    for source in SOURCE_KEYS:
        seen: set[str] = set()
        for scenario in SCENARIOS:
            ev_all = evidence(source, scenario)
            for seq in ev_all.sequences.values():
                if len(seq.swaps) >= 2:
                    seen.add("sequential")
            for _name, _idx, ev, before, out in replay(ev_all):
                seen.add("zero_for_one" if ev.zero_for_one else "one_for_zero")
                crossed = len(out.crossed_ticks)
                if crossed == 0 and _is_default_limit(ev):
                    seen.add("small_no_cross")
                if crossed >= 3:
                    seen.add("multi_tick")
                spacing = before.tick_spacing
                if ((before.tick // spacing) >> 8) != ((out.new_state.tick // spacing) >> 8):
                    seen.add("word_boundary")
                if out.new_state.sqrt_price_x96 in {
                    get_sqrt_ratio_at_tick(t) for t in out.crossed_ticks
                }:
                    seen.add("ends_exactly_on_crossed_tick")
                consumed = ev.amount0 if ev.zero_for_one else ev.amount1
                if _is_default_limit(ev) and consumed < ev.amount_specified:
                    seen.add("insufficient_liquidity")
                if not _is_default_limit(ev) and consumed < ev.amount_specified:
                    seen.add("price_limit_partial")
                if (ev.amount1 if ev.zero_for_one else ev.amount0) == 0:
                    seen.add("dust_zero_output")
                if before.liquidity == 0 or out.new_state.liquidity == 0:
                    seen.add("zero_liquidity_range")
        required = {
            "sequential",
            "zero_for_one",
            "one_for_zero",
            "small_no_cross",
            "multi_tick",
            "word_boundary",
            "insufficient_liquidity",
            "price_limit_partial",
            "dust_zero_output",
            "zero_liquidity_range",
            "ends_exactly_on_crossed_tick",
        }
        assert required <= seen, (source, sorted(required - seen))


def test_protocol_fee_encodings_differ_by_source() -> None:
    """The Agni/FusionX uint32 ratio encoding is not the Uniswap uint8 denominator
    encoding: replaying Agni evidence under Uniswap semantics disagrees with the
    contract, so the shared implementation must keep them apart."""
    ev_all = evidence("agni_v3", "controlled")
    seq = ev_all.sequences["small_both_directions"]
    ev = seq.swaps[0]
    as_uniswap = dataclasses.replace(ev_all.state, source_key="uniswap_v3", lm_pool=None)
    out = swap(as_uniswap, ev.zero_for_one, ev.amount_specified, ev.sqrt_price_limit_x96)
    assert out.new_state.protocol_fees0 != int(ev.post["protocol_fees0"])
    faithful = swap(ev_all.state, ev.zero_for_one, ev.amount_specified, ev.sqrt_price_limit_x96)
    assert faithful.new_state.protocol_fees0 == int(ev.post["protocol_fees0"])


# ---------------------------------------------------------------------------
# Completeness: incomplete snapshot vs real exhaustion (AC3)
# ---------------------------------------------------------------------------


def test_uncollected_bitmap_word_is_incomplete_snapshot_and_input_unchanged() -> None:
    ev_all = evidence("agni_v3", "real")
    state = ev_all.state
    word = (state.tick // state.tick_spacing) >> 8
    trimmed = dataclasses.replace(
        state,
        bitmap_word_range=(word, word),
        tick_bitmap={w: v for w, v in state.tick_bitmap.items() if w == word},
    )
    frozen_copy = dataclasses.replace(trimmed)
    ev = ev_all.sequences["word_boundary_crossing"].swaps[0]
    result = quote_exact_in(trimmed, trimmed.token1, ev.amount_specified)
    assert result.status is QuoteStatus.INCOMPLETE_SNAPSHOT
    assert result.new_state is None and result.amount_out == 0
    assert "outside the collected range" in result.detail
    assert trimmed == frozen_copy
    # The same request on the collected window is a normal fill.
    assert quote_exact_in(state, state.token1, ev.amount_specified).status is QuoteStatus.OK


def test_missing_initialized_tick_data_is_incomplete_snapshot_and_input_unchanged() -> None:
    ev_all = evidence("uniswap_v3", "controlled")
    ev = ev_all.sequences["multi_tick_through_gap"].swaps[0]
    full = swap(ev_all.state, ev.zero_for_one, ev.amount_specified, ev.sqrt_price_limit_x96)
    first_crossed = full.crossed_ticks[0]
    holed = dataclasses.replace(
        ev_all.state, ticks={t: i for t, i in ev_all.state.ticks.items() if t != first_crossed}
    )
    frozen_copy = dataclasses.replace(holed)
    result = quote_exact_in(holed, holed.token0, ev.amount_specified)
    assert result.status is QuoteStatus.INCOMPLETE_SNAPSHOT
    assert f"initialized tick {first_crossed}" in result.detail
    assert holed == frozen_copy


def test_exhaustion_needs_full_range_otherwise_incomplete() -> None:
    ev_all = evidence("fusionx_v3", "controlled")
    ev = ev_all.sequences["insufficient_liquidity_zero_for_one"].swaps[0]
    full = quote_exact_in(ev_all.state, ev_all.state.token0, ev.amount_specified)
    assert full.status is QuoteStatus.INSUFFICIENT_LIQUIDITY
    lo, hi = ev_all.state.bitmap_word_range
    partial_range = dataclasses.replace(ev_all.state, bitmap_word_range=(lo + 5, hi))
    assert (
        quote_exact_in(partial_range, partial_range.token0, ev.amount_specified).status
        is QuoteStatus.INCOMPLETE_SNAPSHOT
    )


def test_selling_at_the_price_bound_is_insufficient_liquidity() -> None:
    ev_all = evidence("uniswap_v3", "controlled")
    ev = ev_all.sequences["insufficient_liquidity_zero_for_one"].swaps[0]
    exhausted = swap(ev_all.state, True, ev.amount_specified, ev.sqrt_price_limit_x96).new_state
    assert exhausted.sqrt_price_x96 == MIN_SQRT_RATIO + 1
    result = quote_exact_in(exhausted, exhausted.token0, 10**18)
    assert result.status is QuoteStatus.INSUFFICIENT_LIQUIDITY
    assert "'SPL'" in result.detail


# ---------------------------------------------------------------------------
# Source gating (AC4)
# ---------------------------------------------------------------------------


def test_unknown_source_and_undeclared_hook_are_unsupported() -> None:
    state = evidence("uniswap_v3", "controlled").state
    assert set(SOURCES) == set(SOURCE_KEYS)
    unknown = dataclasses.replace(state, source_key="some_v3_fork")
    assert cl_quote_exact_in(unknown, state.token0, 10**15).status is QuoteStatus.UNSUPPORTED
    hooked = dataclasses.replace(state, lm_pool="0x19170A0Fb6ee15AC29Feb0C8514fa58F89023107")
    assert cl_quote_exact_in(hooked, state.token0, 10**15).status is QuoteStatus.UNSUPPORTED


def test_unsupported_token_and_bad_amounts() -> None:
    state = evidence("agni_v3", "controlled").state
    assert quote_exact_in(state, "0xdead", 1).status is QuoteStatus.UNSUPPORTED_TOKEN
    with pytest.raises(ValueError):
        quote_exact_in(state, state.token0, 0)
    too_big = quote_exact_in(state, state.token0, 1 << 255)
    assert too_big.status is QuoteStatus.REVERTED


def test_swap_reverts_mirror_contract_requires() -> None:
    state = evidence("agni_v3", "controlled").state
    with pytest.raises(SolidityRevert, match="AS"):
        swap(state, True, 0, MIN_SQRT_RATIO + 1)
    with pytest.raises(SolidityRevert, match="SPL"):
        swap(state, True, 10, state.sqrt_price_x96)  # limit must be strictly below price
    with pytest.raises(SolidityRevert, match="SPL"):
        swap(state, False, 10, MAX_SQRT_RATIO)  # limit must be strictly inside the bound


# ---------------------------------------------------------------------------
# Library-level vectors taken from the Solidity source constants
# ---------------------------------------------------------------------------


def test_tick_math_bounds_are_the_solidity_constants() -> None:
    assert get_sqrt_ratio_at_tick(MIN_TICK) == MIN_SQRT_RATIO
    assert get_sqrt_ratio_at_tick(MAX_TICK) == MAX_SQRT_RATIO
    assert get_sqrt_ratio_at_tick(0) == 1 << 96
    assert get_tick_at_sqrt_ratio(MIN_SQRT_RATIO) == MIN_TICK
    assert get_tick_at_sqrt_ratio(MAX_SQRT_RATIO - 1) == MAX_TICK - 1
    with pytest.raises(SolidityRevert, match="T"):
        get_sqrt_ratio_at_tick(MAX_TICK + 1)
    with pytest.raises(SolidityRevert, match="R"):
        get_tick_at_sqrt_ratio(MAX_SQRT_RATIO)
    with pytest.raises(SolidityRevert, match="R"):
        get_tick_at_sqrt_ratio(MIN_SQRT_RATIO - 1)


def test_tick_math_inverse_consistency() -> None:
    for t in (MIN_TICK, -887271, -280457, -60, -1, 0, 1, 59, 280457, 887271):
        p = get_sqrt_ratio_at_tick(t)
        assert get_tick_at_sqrt_ratio(p) == t
        if t > MIN_TICK:
            assert get_tick_at_sqrt_ratio(p - 1) == t - 1


def test_tick_math_agrees_with_every_captured_pool_price() -> None:
    """Every (sqrtPriceX96, tick) pair the contracts stored is consistent with the
    migrated TickMath: tick == getTickAtSqrtRatio(price), except on a boundary the
    swap left via a downward cross (tick = boundary - 1)."""
    for source, scenario in ALL:
        for _n, _i, ev, _before, _out in replay(evidence(source, scenario)):
            price, tick = int(ev.post["sqrt_price_x96"]), int(ev.post["tick"])
            derived = get_tick_at_sqrt_ratio(price)
            assert tick == derived or (
                tick == derived - 1 and get_sqrt_ratio_at_tick(derived) == price
            )


def test_liquidity_math_reverts() -> None:
    assert add_delta(10, -3) == 7
    assert add_delta(10, 5) == 15
    with pytest.raises(SolidityRevert, match="LS"):
        add_delta(3, -4)
    with pytest.raises(SolidityRevert, match="LA"):
        add_delta(cl_math.UINT128_MAX, 1)


def test_amount0_next_price_takes_the_overflow_fallback_branch() -> None:
    # `amount * sqrtP` overflows uint256, so the Solidity takes the
    # `divRoundingUp(numerator1, numerator1 / sqrtP + amount)` branch; its result
    # still rounds the price *up* (never below the exact real-valued next price).
    sqrt_p, liq = MAX_SQRT_RATIO - 1, 10**30
    amount = (1 << 256) // sqrt_p + 1
    assert amount * sqrt_p > cl_math.UINT256_MAX
    got = cl_math.get_next_sqrt_price_from_amount0_rounding_up_add(sqrt_p, liq, amount)
    numerator1 = liq << 96
    assert got == cl_math.div_rounding_up(numerator1, numerator1 // sqrt_p + amount)
    exact_ceil = -(-numerator1 * sqrt_p // (numerator1 + amount * sqrt_p))
    assert got >= exact_ceil


# ---------------------------------------------------------------------------
# Evaluator seam (WHI-1427 integration)
# ---------------------------------------------------------------------------


def _cl_bundle(
    state: ConcentratedPoolState, amount: int, zero_for_one: bool
) -> tuple[SnapshotBundle, Case, RoutePlan]:
    token_in, token_out = (
        (state.token0, state.token1) if zero_for_one else (state.token1, state.token0)
    )
    case = Case(case_id="c1", token_in=token_in, token_out=token_out, amount_in=amount)
    bundle = SnapshotBundle(
        bundle_id="cl-evidence",
        kind="synthetic",
        schema_version=1,
        block=BlockRef(chain_id=5000, number=101057678, hash="0x" + "00" * 32, timestamp=0),
        pools={state.pool_id: state},
        cases=(case,),
        bundle_hash="0" * 64,
        source_path="<memory>",
    )
    plan = RoutePlan(
        steps=(
            SwapStep(
                pool_id=state.pool_id,
                token_in=token_in,
                token_out=token_out,
                inputs=(FundInput(fund_id=REQUEST_FUND_ID, amount=ALL_REMAINING),),
                output_fund_id="out",
            ),
        )
    )
    return bundle, case, plan


def test_evaluator_replays_a_cl_pool_with_contract_output() -> None:
    ev_all = evidence("agni_v3", "real")
    ev = ev_all.sequences["multi_tick_round_trip"].swaps[0]
    bundle, case, plan = _cl_bundle(ev_all.state, ev.amount_specified, ev.zero_for_one)
    evaluation = evaluate(bundle, case, plan, gross_only())
    assert evaluation.status is EvalStatus.OK, evaluation.error
    assert evaluation.gross_output == -ev.amount1
    assert evaluation.route_features["hops"] == 1
    assert evaluation.route_features["initialized_ticks_crossed"] == len(ev.changed_ticks) >= 3
    next_state = evaluation.next_states[ev_all.state.pool_id]
    assert isinstance(next_state, ConcentratedPoolState)
    assert next_state.sqrt_price_x96 == int(ev.post["sqrt_price_x96"])
    assert bundle.pools[ev_all.state.pool_id] == ev_all.state


def test_evaluator_rejects_cl_partial_fill() -> None:
    ev_all = evidence("uniswap_v3", "controlled")
    ev = ev_all.sequences["insufficient_liquidity_one_for_zero"].swaps[0]
    bundle, case, plan = _cl_bundle(ev_all.state, ev.amount_specified, ev.zero_for_one)
    evaluation = evaluate(bundle, case, plan, gross_only())
    assert evaluation.status is EvalStatus.INVALID_PLAN
    assert evaluation.error is not None and "insufficient_liquidity" in evaluation.error

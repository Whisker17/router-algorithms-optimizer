"""Offline replay of independently captured Liquidity Book evidence (WHI-1433).

Every expected number here comes from `tests/fixtures/liquidity_book/*.jsonl.gz`, written
by `tools/cl_evidence/test/CaptureLB.t.sol` executing the *deployed* Merchant Moe LB v2.2
bytecode on a Mantle fork at the catalog candidate block: four real pairs (clones of the
pinned LBPair implementation, three with live LBHooksRewarder swap hooks) and four pairs
created through the deployed LBFactory with mock tokens. Nothing is produced by the Python
under test. Regenerate with `tools/cl_evidence/capture_lb.sh` (see its README).
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import math
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from benchmark.objective import gross_only
from pools import liquidity_book as lb
from pools.liquidity_book import SOURCES, LBRevert, MissingState, UnsupportedState, swap
from pools.quote import quote_exact_in
from pools.result import QuoteStatus
from routing.evaluator import EvalStatus, evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from snapshot.abi import keccak256_hex
from snapshot.models import (
    BlockRef,
    Case,
    LBStaticFeeParameters,
    LBVariableFeeParameters,
    LiquidityBookPoolState,
    SnapshotBundle,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "liquidity_book"
PROTOCOLS = yaml.safe_load((REPO / "config" / "protocols.yaml").read_text())
CATALOG = next(s for s in PROTOCOLS["sources"] if s["key"] == "moe_lb_v2_2")
FILES = (
    "real_wmnt_usdt_15",
    "real_wmnt_usdt_25",
    "real_usdc_usdt_1",
    "real_weth_wmnt_10",
    "controlled",
)
UINT24_MAX = (1 << 24) - 1


# ---------------------------------------------------------------------------
# Evidence loading
# ---------------------------------------------------------------------------


@dataclass
class EvSwap:
    rec: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    changed_bins: dict[int, tuple[int, int]] = field(default_factory=dict)
    post: dict[str, Any] | None = None

    @property
    def swap_for_y(self) -> bool:
        return bool(self.rec["swap_for_y"])

    @property
    def amount_in(self) -> int:
        return int(self.rec["amount_in"])

    @property
    def timestamp(self) -> int:
        return int(self.rec["timestamp"])

    @property
    def ok(self) -> bool:
        return bool(self.rec["status"] == "ok")


@dataclass
class Scenario:
    name: str
    pair: dict[str, Any]
    state: LiquidityBookPoolState
    pre: dict[str, Any]
    sequences: dict[str, list[EvSwap]]


@dataclass
class EvidenceFile:
    meta: dict[str, Any]
    scenarios: dict[str, Scenario]


def _static(values: list[str]) -> LBStaticFeeParameters:
    return LBStaticFeeParameters(*(int(v) for v in values))


def _variable(values: list[str]) -> LBVariableFeeParameters:
    return LBVariableFeeParameters(*(int(v) for v in values))


def _hook_impl(pair: dict[str, Any]) -> str | None:
    hooks = int(pair["hooks_parameters"], 16)
    return pair["hook_implementation"].lower() if hooks else None


@cache
def load(name: str) -> EvidenceFile:
    path = FIXTURES / f"{name}.jsonl.gz"
    lines = gzip.decompress(path.read_bytes()).decode().splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    assert records and records[-1] == {"kind": "end"}, f"{path.name}: truncated evidence"
    metas = [r for r in records if r["kind"] == "meta"]
    assert len(metas) == 1
    scenarios: dict[str, Scenario] = {}
    pairs = {r["scenario"]: r for r in records if r["kind"] == "pair"}
    for sc, pair in pairs.items():
        bins = {
            int(r["id"]): (int(r["x"]), int(r["y"]))
            for r in records
            if r["kind"] == "bin" and r["scenario"] == sc
        }
        pre = next(
            r
            for r in records
            if r["kind"] == "state" and r["scenario"] == sc and r["label"] == "pre"
        )
        assert int(pre["tree_size"]) == len(bins)
        state = LiquidityBookPoolState(
            pool_id=pair["pair"].lower(),
            source_key="moe_lb_v2_2",
            token0=pair["token_x"].lower(),
            token1=pair["token_y"].lower(),
            bin_step=int(pair["bin_step"]),
            block_timestamp=int(pre["timestamp"]),
            active_id=int(pre["active_id"]),
            reserve_x=int(pre["reserves"][0]),
            reserve_y=int(pre["reserves"][1]),
            protocol_fee_x=int(pre["protocol_fees"][0]),
            protocol_fee_y=int(pre["protocol_fees"][1]),
            static_fee=_static(pre["static"]),
            variable_fee=_variable(pre["variable"]),
            bin_range=(0, UINT24_MAX),  # the tree was walked to both sentinels
            bins=bins,
            hooks_parameters=int(pair["hooks_parameters"], 16),
            swap_hook_implementation=_hook_impl(pair),
        )
        sequences: dict[str, list[EvSwap]] = {}
        by_key: dict[tuple[str, int], EvSwap] = {}
        for r in records:
            if r.get("scenario") != sc:
                continue
            if r["kind"] == "swap":
                ev = EvSwap(r)
                sequences.setdefault(r["sequence"], []).append(ev)
                by_key[(r["sequence"], int(r["step"]))] = ev
            elif r["kind"] == "event":
                by_key[(r["sequence"], int(r["step"]))].events.append(r)
            elif r["kind"] == "bin_changed":
                key = (r["sequence"], int(r["step"]))
                by_key[key].changed_bins[int(r["id"])] = (int(r["x"]), int(r["y"]))
            elif r["kind"] == "state" and r["label"] == "post":
                by_key[(r["sequence"], int(r["step"]))].post = r
        for seq in sequences.values():
            assert [int(s.rec["step"]) for s in seq] == list(range(len(seq)))
        scenarios[sc] = Scenario(sc, pair, state, pre, sequences)
    return EvidenceFile(metas[0], scenarios)


def all_scenarios() -> list[tuple[str, Scenario]]:
    return [(f, sc) for f in FILES for sc in load(f).scenarios.values()]


def all_swaps() -> list[tuple[str, str, EvSwap]]:
    return [
        (sc.name, seq, s)
        for _, sc in all_scenarios()
        for seq, swaps in sc.sequences.items()
        for s in swaps
    ]


def _snapshot(state: LiquidityBookPoolState) -> tuple[Any, ...]:
    """A detached copy of every field (bins as sorted items) to detect any mutation."""
    return tuple(
        tuple(sorted(state.bins.items())) if f.name == "bins" else getattr(state, f.name)
        for f in dataclasses.fields(state)
    )


def _selector(reason: str) -> str:
    return "0x" + keccak256_hex(f"{reason}()".encode())[2:10]


def _revert_selector(data: str) -> str:
    return data[:10].lower()


def _events_as_tuples(ev: EvSwap) -> list[tuple[int, int, int, int, int, int]]:
    side = 0 if ev.swap_for_y else 1
    out = []
    for e in ev.events:
        out.append(
            (
                int(e["id"]),
                int(e["amounts_in"][side]),
                int(e["amounts_out"][1 - side]),
                int(e["volatility_accumulator"]),
                int(e["total_fees"][side]),
                int(e["protocol_fees"][side]),
            )
        )
        # The other side of each packed amount is always zero.
        assert int(e["amounts_in"][1 - side]) == int(e["amounts_out"][side]) == 0
    return out


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FILES)
def test_evidence_provenance_matches_catalog(name: str) -> None:
    meta = load(name).meta
    block = PROTOCOLS["candidate_block"]
    assert int(meta["chain_id"]) == 5000
    assert int(meta["block_number"]) == block["number"]
    assert meta["block_hash"] == block["hash"]
    contracts = CATALOG["contracts"]
    assert meta["factory"].lower() == contracts["factory"]["address"].lower()
    assert meta["factory_code_hash"] == contracts["factory"]["code_hash"]
    impl = contracts["pair_implementation"]
    assert meta["pair_implementation"].lower() == impl["address"].lower()
    assert meta["pair_implementation_code_hash"] == impl["code_hash"]
    assert SOURCES["moe_lb_v2_2"].pair_implementation == impl["address"].lower()
    for sc in load(name).scenarios.values():
        assert sc.pair["factory"].lower() == meta["factory"].lower()
        assert sc.pair["implementation"].lower() == impl["address"].lower()
        if name.startswith("real"):
            assert int(sc.pre["timestamp"]) == block["timestamp"]


def test_real_pairs_carry_the_admitted_rewarder_swap_hook() -> None:
    hooked = 0
    for name in FILES[:4]:
        (sc,) = load(name).scenarios.values()
        flags = int(sc.pair["hooks_parameters"], 16) >> 160
        if flags:
            hooked += 1
            assert flags & 1  # BEFORE_SWAP_FLAG
            assert (
                sc.pair["hook_implementation"].lower()
                in SOURCES["moe_lb_v2_2"].amount_neutral_swap_hooks
            )
            # The live hook really executed inside the fork swaps (it reads its storage).
            reads = [
                int(s.rec["hook_storage_reads"]) for q in sc.sequences.values() for s in q if s.ok
            ]
            assert min(reads) > 0
    assert hooked == 3
    (example,) = load("real_wmnt_usdt_15").scenarios.values()
    assert example.pair["pair"].lower() == CATALOG["contracts"]["example_pair"]["address"].lower()


# ---------------------------------------------------------------------------
# Differential replay
# ---------------------------------------------------------------------------


def _replay(sc: Scenario, swaps: list[EvSwap]) -> None:
    state = sc.state
    bins = dict(state.bins)
    for ev in swaps:
        where = f"{sc.name}/{ev.rec['sequence']}/{ev.rec['step']}"
        at = dataclasses.replace(state, block_timestamp=ev.timestamp)
        if not ev.ok:
            with pytest.raises(LBRevert) as exc:
                swap(at, ev.swap_for_y, ev.amount_in)
            assert _selector(exc.value.reason) == _revert_selector(ev.rec["revert_data"]), where
            continue
        outcome = swap(at, ev.swap_for_y, ev.amount_in)
        assert int(ev.rec["credited"]) == ev.amount_in == outcome.amount_in, where
        out_x, out_y = int(ev.rec["amounts_out_x"]), int(ev.rec["amounts_out_y"])
        assert outcome.amount_out == int(ev.rec["received"]) == (out_y if ev.swap_for_y else out_x)
        got = [
            (
                b.bin_id,
                b.amount_in,
                b.amount_out,
                b.volatility_accumulator,
                b.total_fee,
                b.protocol_fee,
            )
            for b in outcome.bins
        ]
        assert got == _events_as_tuples(ev), where
        post = ev.post
        assert post is not None
        new = outcome.new_state
        assert new.active_id == int(post["active_id"]), where
        assert new.variable_fee == _variable(post["variable"]), where
        assert new.static_fee == _static(post["static"]) == state.static_fee
        assert (new.reserve_x, new.reserve_y) == tuple(int(v) for v in post["reserves"]), where
        assert (new.protocol_fee_x, new.protocol_fee_y) == tuple(
            int(v) for v in post["protocol_fees"]
        ), where
        bins.update(ev.changed_bins)
        assert dict(new.bins) == bins, where  # every bin, not only the traversed ones
        assert int(post["tree_size"]) == len(new.bins)
        assert int(post["timestamp"]) == ev.timestamp == new.block_timestamp
        state = new


@pytest.mark.parametrize("name", FILES)
def test_swaps_match_contract_outputs_events_and_next_state(name: str) -> None:
    for sc in load(name).scenarios.values():
        for swaps in sc.sequences.values():
            _replay(sc, swaps)


@pytest.mark.parametrize("name", FILES)
def test_quote_exact_in_statuses_match_contract_and_never_mutate(name: str) -> None:
    expected_status = {
        _selector(lb.OUT_OF_LIQUIDITY): QuoteStatus.INSUFFICIENT_LIQUIDITY,
        _selector(lb.INSUFFICIENT_AMOUNT_OUT): QuoteStatus.INSUFFICIENT_OUTPUT_AMOUNT,
        _selector("BinHelper__MaxLiquidityPerBinExceeded"): QuoteStatus.REVERTED,
    }
    for sc in load(name).scenarios.values():
        for swaps in sc.sequences.values():
            state = sc.state
            for ev in swaps:
                at = dataclasses.replace(state, block_timestamp=ev.timestamp)
                frozen = _snapshot(at)
                token_in = at.token0 if ev.swap_for_y else at.token1
                result = quote_exact_in(at, token_in, ev.amount_in)
                assert _snapshot(at) == frozen  # the input snapshot is never mutated
                if ev.ok:
                    assert result.status is QuoteStatus.OK
                    assert result.amount_out == int(ev.rec["received"])
                    assert result.amount_in_consumed == ev.amount_in
                    assert result.features["lb_bins_swapped"] == len(ev.events)
                    flags = at.hooks_parameters >> 160
                    assert result.features["lb_swap_hook_calls"] == (1 if flags & 1 else 0)
                    assert isinstance(result.new_state, LiquidityBookPoolState)
                    state = result.new_state
                else:
                    assert result.status is expected_status[_revert_selector(ev.rec["revert_data"])]
                    assert result.new_state is None and result.amount_out == 0


def test_contract_view_quote_agrees_with_execution() -> None:
    """`getSwapOut` (the pair's own view) against the executed swap: a contract-side
    consistency check on the evidence itself, independent of the Python."""
    for _, _, ev in all_swaps():
        quote = ev.rec["quote"]
        if ev.ok:
            assert int(quote["amount_in_left"]) == 0
            assert int(quote["amount_out"]) == int(ev.rec["received"])
        elif "amount_in_left" in quote:
            reason = _revert_selector(ev.rec["revert_data"])
            if reason == _selector(lb.OUT_OF_LIQUIDITY):
                assert int(quote["amount_in_left"]) > 0
            else:
                assert int(quote["amount_out"]) == 0


def test_evidence_covers_required_case_classes() -> None:
    """Coverage is judged on the fork's own data (events, state, timestamps)."""
    swaps = all_swaps()
    ok = [s for _, _, s in swaps if s.ok]
    assert any(s.swap_for_y for s in ok) and any(not s.swap_for_y for s in ok)
    assert max(len(s.events) for s in ok) >= 100  # many bins in one swap
    assert sum(1 for s in ok if len(s.events) >= 2) >= 20
    reverts = {_revert_selector(s.rec["revert_data"]) for _, _, s in swaps if not s.ok}
    assert {
        _selector(lb.OUT_OF_LIQUIDITY),
        _selector(lb.INSUFFICIENT_AMOUNT_OUT),
        _selector("BinHelper__MaxLiquidityPerBinExceeded"),
    } <= reverts
    # Protocol fee share actually taken, dust outputs of one wei.
    assert any(
        int(e["protocol_fees"][0]) + int(e["protocol_fees"][1]) > 0 for s in ok for e in s.events
    )
    assert any(int(s.rec["received"]) == 1 for s in ok)

    # Time-dependent reference branches, from each swap's pre-swap on-chain fee state.
    branches: set[str] = set()
    capped = False
    for _, sc in all_scenarios():
        for seq in sc.sequences.values():
            prev = sc.pre
            for s in seq:
                if not s.ok:
                    continue
                static = _static(prev["static"])
                dt = s.timestamp - int(prev["variable"][3])
                if dt < static.filter_period:
                    branches.add("dt<filter")
                elif dt < static.decay_period:
                    branches.add("filter<=dt<decay")
                else:
                    branches.add("dt>=decay")
                vols = [int(e["volatility_accumulator"]) for e in s.events]
                capped |= static.max_volatility_accumulator in vols and len(vols) > 1
                assert s.post is not None
                prev = s.post
    assert branches == {"dt<filter", "filter<=dt<decay", "dt>=decay"}
    assert capped
    # A real pair enters the swap in the decay (filter <= dt < decay) branch.
    (weth,) = load("real_weth_wmnt_10").scenarios.values()
    st = _static(weth.pre["static"])
    assert (
        st.filter_period
        <= int(weth.pre["timestamp"]) - int(weth.pre["variable"][3])
        < st.decay_period
    )
    # Repeated use at one timestamp: the second identical swap yields a different output.
    for _, sc in all_scenarios():
        if "repeat_x_to_y" in sc.sequences:
            a, b = sc.sequences["repeat_x_to_y"]
            assert a.amount_in == b.amount_in and a.timestamp == b.timestamp
            assert int(b.rec["received"]) < int(a.rec["received"])
    # Exact-drain boundary: one event draining the whole active bin, input fully used.
    boundary = 0
    for _, sc in all_scenarios():
        for seq_name in ("drain_active_y", "bin_boundaries"):
            if seq_name in sc.sequences:
                first = sc.sequences[seq_name][0]
                (e,) = first.events
                assert int(e["amounts_out"][1]) == sc.state.bins[int(e["id"])][1]
                boundary += 1
    assert boundary >= 4


# ---------------------------------------------------------------------------
# Repeated use through the evaluator
# ---------------------------------------------------------------------------


def _bundle(state: LiquidityBookPoolState, case: Case) -> SnapshotBundle:
    return SnapshotBundle(
        bundle_id="lb-evidence",
        kind="synthetic",
        schema_version=1,
        block=BlockRef(5000, 0, "0x" + "00" * 32, state.block_timestamp),
        pools={state.pool_id: state},
        cases=(case,),
        bundle_hash="0x" + "00" * 32,
        source_path="<memory>",
    )


@pytest.mark.parametrize("name", FILES[:4])
def test_evaluator_threads_lb_state_through_repeated_use(name: str) -> None:
    (sc,) = load(name).scenarios.values()
    a, b = sc.sequences["repeat_x_to_y"]
    state = sc.state
    case = Case("repeat", state.token0, state.token1, a.amount_in + b.amount_in)
    plan = RoutePlan(
        steps=(
            SwapStep(
                state.pool_id,
                state.token0,
                state.token1,
                (FundInput(REQUEST_FUND_ID, a.amount_in),),
                "y1",
            ),
            SwapStep(
                state.pool_id,
                state.token0,
                state.token1,
                (FundInput(REQUEST_FUND_ID, ALL_REMAINING),),
                "y2",
            ),
        )
    )
    bundle = _bundle(state, case)
    evaluation = evaluate(bundle, case, plan, gross_only())
    assert evaluation.status is EvalStatus.OK, evaluation.error
    assert [t.amount_out for t in evaluation.trace] == [
        int(a.rec["received"]),
        int(b.rec["received"]),
    ]
    assert evaluation.gross_output == int(a.rec["received"]) + int(b.rec["received"])
    assert evaluation.route_features["lb_bins_swapped"] == len(a.events) + len(b.events)
    final = evaluation.next_states[state.pool_id]
    assert isinstance(final, LiquidityBookPoolState)
    assert b.post is not None
    assert final.active_id == int(b.post["active_id"])
    assert final.variable_fee == _variable(b.post["variable"])
    assert bundle.pools[state.pool_id] is state  # the bundle snapshot is untouched
    # Quoting the second swap against the *original* snapshot would be wrong.
    stale = quote_exact_in(state, state.token0, b.amount_in)
    assert stale.amount_out != int(b.rec["received"])


# ---------------------------------------------------------------------------
# Missing state is explicit and distinct from real exhaustion
# ---------------------------------------------------------------------------


def _exhaust(direction: str) -> tuple[LiquidityBookPoolState, EvSwap]:
    (sc,) = load("real_wmnt_usdt_25").scenarios.values()
    (ev,) = sc.sequences[f"exhaust_{direction}"]
    assert not ev.ok and _revert_selector(ev.rec["revert_data"]) == _selector(lb.OUT_OF_LIQUIDITY)
    return sc.state, ev


def test_exhaustion_is_insufficient_liquidity_only_over_the_whole_tree() -> None:
    for direction, token_idx in (("x_to_y", 0), ("y_to_x", 1)):
        state, ev = _exhaust(direction)
        token_in = (state.token0, state.token1)[token_idx]
        full = quote_exact_in(state, token_in, ev.amount_in)
        assert full.status is QuoteStatus.INSUFFICIENT_LIQUIDITY
        ids = sorted(state.bins)
        # Drop the far end of the book on the side the swap walks into.
        if direction == "x_to_y":
            cut_lo = ids[1]
            partial = dataclasses.replace(
                state,
                bin_range=(cut_lo, UINT24_MAX),
                bins={i: v for i, v in state.bins.items() if i >= cut_lo},
            )
        else:
            cut_hi = ids[-2]
            partial = dataclasses.replace(
                state,
                bin_range=(0, cut_hi),
                bins={i: v for i, v in state.bins.items() if i <= cut_hi},
            )
        result = quote_exact_in(partial, token_in, ev.amount_in)
        assert result.status is QuoteStatus.INCOMPLETE_SNAPSHOT, result.detail


def test_small_swap_inside_a_bounded_range_still_quotes() -> None:
    (sc,) = load("real_wmnt_usdt_15").scenarios.values()
    small = sc.sequences["small_both"][0]
    active = sc.state.active_id
    bounded = dataclasses.replace(
        sc.state,
        bin_range=(active - 5, active + 5),
        bins={i: v for i, v in sc.state.bins.items() if active - 5 <= i <= active + 5},
    )
    result = quote_exact_in(bounded, bounded.token0, small.amount_in)
    assert result.status is QuoteStatus.OK and result.amount_out == int(small.rec["received"])
    far = quote_exact_in(bounded, bounded.token0, sc.sequences["x_then_y"][0].amount_in)
    assert far.status is QuoteStatus.INCOMPLETE_SNAPSHOT


def test_missing_fee_state_or_active_bin_is_incomplete_snapshot() -> None:
    (sc,) = load("real_wmnt_usdt_25").scenarios.values()
    state = sc.state
    for broken in (
        dataclasses.replace(state, static_fee=None),
        dataclasses.replace(state, variable_fee=None),
        dataclasses.replace(
            state,
            bin_range=(state.active_id + 1, UINT24_MAX),
            bins={i: v for i, v in state.bins.items() if i > state.active_id},
        ),
    ):
        frozen = _snapshot(broken)
        result = quote_exact_in(broken, state.token0, 10**15)
        assert result.status is QuoteStatus.INCOMPLETE_SNAPSHOT, result.detail
        assert result.new_state is None
        assert _snapshot(broken) == frozen
        with pytest.raises(MissingState):
            swap(broken, True, 10**15)


def test_unknown_source_and_unadmitted_hook_are_unsupported() -> None:
    (sc,) = load("real_wmnt_usdt_15").scenarios.values()
    state = sc.state
    assert state.hooks_parameters >> 160 & 1
    for broken in (
        dataclasses.replace(state, source_key="joe_v2_1"),
        dataclasses.replace(state, swap_hook_implementation=None),
        dataclasses.replace(state, swap_hook_implementation="0x" + "11" * 20),
        dataclasses.replace(state, hooks_parameters=(1 << 160)),  # flag with no hooks address
    ):
        assert quote_exact_in(broken, state.token0, 10**15).status is QuoteStatus.UNSUPPORTED
        with pytest.raises(UnsupportedState):
            swap(broken, True, 10**15)
    # A hooks word without swap flags (e.g. only mint/burn hooks) needs no admission.
    (no_hook,) = load("real_wmnt_usdt_25").scenarios.values()
    mint_only = dataclasses.replace(
        no_hook.state, hooks_parameters=(1 << 164) | 0x1234, swap_hook_implementation=None
    )
    small = no_hook.sequences["small_both"][0]
    result = quote_exact_in(mint_only, mint_only.token0, small.amount_in)
    assert result.status is QuoteStatus.OK and result.amount_out == int(small.rec["received"])
    assert result.features["lb_swap_hook_calls"] == 0


def test_unsupported_token_and_bad_amounts() -> None:
    (sc,) = load("real_wmnt_usdt_25").scenarios.values()
    assert quote_exact_in(sc.state, "0x" + "22" * 20, 10).status is QuoteStatus.UNSUPPORTED_TOKEN
    with pytest.raises(ValueError):
        quote_exact_in(sc.state, sc.state.token0, 0)
    with pytest.raises(LBRevert) as exc:
        swap(sc.state, True, 0)
    assert exc.value.reason == lb.INSUFFICIENT_AMOUNT_IN


def test_contract_reverts_on_uint128_reserve_overflow_and_clock_underflow() -> None:
    """Checked-arithmetic reverts that fork evidence cannot reach with real tokens: the
    pair's balance cast (`SafeCast.safe128`) and `timestamp - timeOfLastUpdate`."""
    (sc,) = load("real_wmnt_usdt_25").scenarios.values()
    state = sc.state
    total_x = state.reserve_x + state.protocol_fee_x
    result = quote_exact_in(state, state.token0, (1 << 128) - total_x)
    assert result.status is QuoteStatus.REVERTED and "SafeCast__Exceeds128Bits" in result.detail
    assert state.variable_fee is not None
    past = dataclasses.replace(state, block_timestamp=state.variable_fee.time_of_last_update - 1)
    result = quote_exact_in(past, state.token0, 10**15)
    assert result.status is QuoteStatus.REVERTED and "Panic(0x11)" in result.detail


def test_state_rejects_bins_outside_the_collected_range() -> None:
    (sc,) = load("real_wmnt_usdt_25").scenarios.values()
    with pytest.raises(ValueError):
        dataclasses.replace(sc.state, bin_range=(sc.state.active_id, sc.state.active_id))
    with pytest.raises(ValueError):
        dataclasses.replace(sc.state, bin_range=(5, 4))
    frozen = sc.state.bins
    with pytest.raises(TypeError):
        frozen[1] = (0, 0)  # type: ignore[index]


# ---------------------------------------------------------------------------
# Library constants re-derived from their Solidity definitions
# ---------------------------------------------------------------------------


def test_price_and_constants_match_their_definitions() -> None:
    # Constants.MAX_LIQUIDITY_PER_BIN documents itself as
    # (2^256 - 1) / (2 * log(2^128) / log(1.0001)); the literal is that quotient with the
    # denominator taken as the integer 2 * 887272 (= 2 * floor(log(2^128) / log(1.0001))).
    assert math.floor(math.log(2**128) / math.log(1.0001)) == 887272
    assert lb.MAX_LIQUIDITY_PER_BIN == (2**256 - 1) // (2 * 887272)
    # getPriceFromId(2^23) is exactly 1.0 in 128.128 for every bin step.
    for bin_step in (1, 10, 15, 25, 100):
        assert lb.get_price_from_id(1 << 23, bin_step) == 1 << 128
    # One bin up multiplies the price by (1 + binStep / 1e4) up to 128.128 rounding.
    p = lb.get_price_from_id((1 << 23) + 1, 25)
    assert p == lb.get_base(25)
    assert lb.get_base(25) == (1 << 128) + (25 << 128) // 10_000


# ---------------------------------------------------------------------------
# WHI-1505 / L03: bounded exact bin-price reuse (explicit only; off by default)
# ---------------------------------------------------------------------------


def _call(fn: Any, *args: Any) -> tuple[Any, Any]:
    try:
        return ("ok", fn(*args))
    except Exception as exc:  # noqa: BLE001 -- the exact type and message are compared
        return (type(exc), str(exc))


def test_bin_price_reuse_vectors_are_exact_and_keyed_by_id_and_step() -> None:
    reuse = lb.BinMathReuse(capacity=4096)
    ids = [1 << 23, (1 << 23) + 1, (1 << 23) - 1, (1 << 23) + (1 << 20) - 1]
    ids += [(1 << 23) - (1 << 20) + 1] + list(range((1 << 23) - 300, (1 << 23) + 300, 7))
    ids = sorted(set(ids))
    steps = (1, 2, 5, 10, 15, 20, 25, 50, 100)
    outcomes = set()
    for _ in range(2):
        for bin_id in ids:
            for bin_step in steps:
                expected = _call(lb.get_price_from_id, bin_id, bin_step)
                assert _call(reuse.price_from_id, bin_id, bin_step) == expected
                outcomes.add(expected[0])
    assert outcomes == {"ok", LBRevert}  # the far ids underflow at the large steps
    n = len(ids) * len(steps)
    stats = reuse.stats()
    reverts = stats["misses"] - stats["hits"]  # a revert is recomputed, never stored
    assert stats["entries"] == stats["hits"] == n - reverts // 2 and reverts > 0
    # The same id under another bin step is a different key and a different price.
    fresh = lb.BinMathReuse(capacity=2)
    a, b = fresh.price_from_id(ids[1], 10), fresh.price_from_id(ids[1], 25)
    assert (a, b) == (lb.get_price_from_id(ids[1], 10), lb.get_price_from_id(ids[1], 25))
    assert a != b and fresh.stats()["misses"] == 2
    fresh.price_from_id(ids[2], 10)  # evicts (ids[1], 10)
    assert fresh.price_from_id(ids[1], 10) == a
    assert fresh.stats() == {"capacity": 2, "entries": 2, "hits": 0, "misses": 4}


@pytest.mark.parametrize(
    "args",
    [
        (0, 25),  # |id - 2^23| >= 2^20: Uint128x128Math__PowUnderflow
        (UINT24_MAX, 1),
        ((1 << 23) - (1 << 20), 10),
        ((1 << 23) + 5, 1.0),
        (float(1 << 23), 25),
        ("8388608", 25),
        (None, 25),
        (True, 25),
        ((1 << 23) + 1, [25]),
    ],
)
def test_bin_price_reuse_invalid_and_non_int_inputs_behave_like_reference(args: Any) -> None:
    reuse = lb.BinMathReuse(capacity=8)
    reuse.price_from_id(1 << 23, 25)
    for _ in range(2):
        assert _call(reuse.price_from_id, *args) == _call(lb.get_price_from_id, *args)
    assert reuse.stats()["entries"] == 1


@pytest.mark.parametrize("capacity", [0, -1, None, 1.5, True, "8"])
def test_bin_price_reuse_capacity_must_be_a_positive_int(capacity: Any) -> None:
    with pytest.raises(ValueError, match="positive int"):
        lb.BinMathReuse(capacity)


def _lb_swap(state: LiquidityBookPoolState, *args: Any, **kwargs: Any) -> Any:
    try:
        return swap(state, *args, **kwargs)
    except (LBRevert, MissingState) as exc:
        return (type(exc).__name__, exc.args)


def test_bin_price_reuse_swaps_match_reference_on_contract_evidence() -> None:
    # One instance shared across every pair and bin step, a capacity-1 instance and a
    # fresh one; each sequence threads its returned state like the contract replay.
    shared, tiny = lb.BinMathReuse(capacity=4096), lb.BinMathReuse(capacity=1)
    compared = 0
    for _, sc in all_scenarios():
        for swaps in sc.sequences.values():
            state = sc.state
            for ev in swaps:
                at = dataclasses.replace(state, block_timestamp=ev.timestamp)
                frozen = _snapshot(at)
                expected = _lb_swap(at, ev.swap_for_y, ev.amount_in)
                for reuse in (shared, tiny, lb.BinMathReuse(capacity=16)):
                    got = _lb_swap(at, ev.swap_for_y, ev.amount_in, math_reuse=reuse)
                    assert got == expected, (sc.name, ev.rec["sequence"], ev.rec["step"])
                assert _snapshot(at) == frozen
                compared += 1
                if not isinstance(expected, tuple):
                    state = expected.new_state
    assert compared > 50
    assert shared.stats()["hits"] > 0 and tiny.stats()["entries"] == 1


def test_ordinary_lb_callers_never_use_bin_price_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    (sc,) = load("real_wmnt_usdt_25").scenarios.values()
    reuse = lb.BinMathReuse(capacity=64)  # wraps the reference function it was built with
    calls: list[tuple[int, int]] = []
    original = lb.get_price_from_id

    def counted(bin_id: int, bin_step: int) -> int:
        calls.append((bin_id, bin_step))
        return original(bin_id, bin_step)

    monkeypatch.setattr(lb, "get_price_from_id", counted)
    ev = next(e for q in sc.sequences.values() for e in q if e.ok and len(e.events) > 1)
    state = dataclasses.replace(sc.state, block_timestamp=ev.timestamp)
    token_in = state.token0 if ev.swap_for_y else state.token1
    default = swap(state, ev.swap_for_y, ev.amount_in)
    assert len(calls) == len(default.bins) > 1
    calls.clear()
    assert quote_exact_in(state, token_in, ev.amount_in).amount_out == default.amount_out
    assert len(calls) == len(default.bins)
    calls.clear()
    assert swap(state, ev.swap_for_y, ev.amount_in, math_reuse=reuse) == default
    stats = reuse.stats()
    assert calls == [] and stats["hits"] + stats["misses"] == len(default.bins)

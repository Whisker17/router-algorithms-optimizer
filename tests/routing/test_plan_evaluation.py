"""Split / merged funding with shared physical-pool state (docs/DESIGN.md §2.5, WHI-1435).

Every expected value here is independent of the Python under test:

- **Hand-derived CPMM vectors** (`tests/fixtures/routing/cpmm_graph`): `_v2_out` is a
  restatement of the Solidity `UniswapV2Library.getAmountOut` formula
  (`in*997*rOut // (rIn*1000 + in*997)`, generalized to `fee_bps`), applied by hand to
  the fixture's reserves; the headline vectors are also pinned as literals.
- **Fork evidence** for shared CL / LB / Classic pool state: the swap sequences the
  deployed pool bytecode executed on a Mantle fork (`tools/cl_evidence`), stored in
  `tests/fixtures/{concentrated,liquidity_book,moe_classic}/`. The states are the
  unchanged pools of the published per-source bundles at the same block (composed into
  `tests/fixtures/routing/mantle_mixed`), checked against the evidence pre-state below.
"""

from __future__ import annotations

import gzip
import json
from functools import cache
from pathlib import Path
from typing import Any, Literal

import pytest

from benchmark.objective import gross_only
from pools.quote import quote_exact_in
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from snapshot.bundle import load_bundle
from snapshot.models import (
    Case,
    ConcentratedPoolState,
    ConstantProductPoolState,
    LiquidityBookPoolState,
    SnapshotBundle,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
R = REQUEST_FUND_ID
ALL: Literal["ALL_REMAINING"] = ALL_REMAINING
OBJ = gross_only()

USDT = "0x201eba5cc46d216ce6dc03f6a759e8e766e956ae"
WMNT = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"
USDC = "0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9"
UNI_USDT_WMNT = "0x4cdfc22bf05209de87ee564746dc7e5174631d2b"
AGNI_USDC_WMNT = "0x1858d52cf57c07a018171d7a1e68dc081f17144f"
FUSIONX_USDT_WMNT = "0x262255f4770aebe2d0c8b97a46287dcecc2a0aff"
LB25_WMNT_USDT = "0x365722f12ceb2063286a268b03c654df81b7c00f"
LB15_WMNT_USDT = "0xf6c9020c9e915808481757779edb53daceae2415"
MOE_USDT_WMNT = "0x4e7685df06201521f35a182467feefe02c53d847"


@cache
def bundle(name: str) -> SnapshotBundle:
    return load_bundle(FIXTURES / name)


def cpmm() -> SnapshotBundle:
    return bundle("routing/cpmm_graph")


def mixed() -> SnapshotBundle:
    return bundle("routing/mantle_mixed")


def step(
    pool_id: str,
    token_in: str,
    token_out: str,
    out: str,
    *inputs: tuple[str, int | Literal["ALL_REMAINING"]],
) -> SwapStep:
    return SwapStep(pool_id, token_in, token_out, tuple(FundInput(f, a) for f, a in inputs), out)


def run(b: SnapshotBundle, case: Case, *steps: SwapStep) -> Evaluation:
    return evaluate(b, case, RoutePlan(steps), OBJ)


def ok(b: SnapshotBundle, case: Case, *steps: SwapStep) -> Evaluation:
    evaluation = run(b, case, *steps)
    assert evaluation.status is EvalStatus.OK, evaluation.error
    _assert_conserved(case, evaluation)
    return evaluation


def _assert_conserved(case: Case, ev: Evaluation) -> None:
    """Token conservation read off the ledger and the trace: the request fund is fully
    allocated, every step's merged input is exactly what its references consumed,
    every fund's output is what its producing step received, and the only non-zero
    balances left are target-token terminal funds summing to the gross output."""
    funds = {f.fund_id: f for f in ev.funds}
    assert funds[R].produced == funds[R].consumed == case.amount_in
    consumed: dict[str, int] = {}
    for t in ev.trace:
        assert t.amount_in == sum(a for _, a in t.inputs)
        for fid, amount in t.inputs:
            if amount:
                consumed[fid] = consumed.get(fid, 0) + amount
        produced = funds[t.output_fund_id]
        assert (produced.produced, produced.producer_step, produced.token) == (
            t.amount_out,
            t.step,
            t.token_out,
        )
    assert {f: r.consumed for f, r in funds.items() if r.consumed} == consumed
    terminal = [f for f in funds.values() if f.remaining]
    assert all(f.token == case.token_out for f in terminal)
    assert sum(f.remaining for f in terminal) == ev.gross_output


# ---------------------------------------------------------------------------
# Independent expectations
# ---------------------------------------------------------------------------


def _v2_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int = 30) -> int:
    """`UniswapV2Library.getAmountOut` restated (MoeLibrary is token-for-token the same)."""
    keep = 10_000 - fee_bps
    return amount_in * keep * reserve_out // (reserve_in * 10_000 + amount_in * keep)


# The reserves the hand derivations below are made from; the fixture must hold exactly these.
CPMM_RESERVES = {
    "ab_1": ("TKA", "TKB", 200_000_000, 200_000_000, 30),
    "ac_1": ("TKA", "TKC", 1_000_000_000, 1_000_000, 30),
    "ac_2": ("TKA", "TKC", 500_000_000, 500_000, 30),
    "cb_1": ("TKC", "TKB", 1_000_000, 1_000_000_000, 30),
    "cb_2": ("TKC", "TKB", 400_000, 420_000_000, 30),
    "cd_1": ("TKC", "TKD", 1_000_000, 2_000_000, 30),
    "db_1": ("TKD", "TKB", 2_000_000, 1_000_000_000, 30),
    "db_2": ("TKD", "TKB", 1_000_000, 480_000_000, 5),
    "ab_dry": ("TKA", "TKB", 0, 1_000_000, 30),
}


def _records(path: Path) -> list[dict[str, Any]]:
    raw = gzip.decompress(path.read_bytes()).decode() if path.suffix == ".gz" else path.read_text()
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


@cache
def _cl_evidence(source: str) -> list[dict[str, Any]]:
    return _records(FIXTURES / "concentrated" / f"{source}_real.jsonl")


def cl_sequence(source: str, sequence: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    recs = [r for r in _cl_evidence(source) if r.get("sequence") == sequence]
    swaps = [r for r in recs if r["kind"] == "swap"]
    posts = {r["index"]: r for r in recs if r["kind"] == "post_state"}
    return [(s, posts[s["index"]]) for s in swaps]


@cache
def _lb_evidence(name: str) -> list[dict[str, Any]]:
    return _records(FIXTURES / "liquidity_book" / f"real_{name}.jsonl.gz")


def lb_sequence(name: str, sequence: str) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    recs = [r for r in _lb_evidence(name) if r.get("sequence") == sequence]
    swaps = [r for r in recs if r["kind"] == "swap"]
    posts = {r["step"]: r for r in recs if r["kind"] == "state" and r["label"] == "post"}
    return [(s, posts.get(s["step"])) for s in swaps]  # a reverted swap has no post-state


@cache
def _classic_evidence() -> list[dict[str, Any]]:
    return _records(FIXTURES / "moe_classic" / "evidence.jsonl.gz")


def classic_swap(case_id: str, pool: str) -> dict[str, Any]:
    (rec,) = (
        r
        for r in _classic_evidence()
        if r["kind"] == "swap" and r["case_id"] == case_id and r["pool"].lower() == pool
    )
    return rec


def cl_out(rec: dict[str, Any]) -> int:
    return -int(rec["amount1"] if rec["zero_for_one"] else rec["amount0"])


# ---------------------------------------------------------------------------
# The fixtures are what the derivations say they are
# ---------------------------------------------------------------------------


def test_cpmm_fixture_holds_the_reserves_the_vectors_are_derived_from() -> None:
    b = cpmm()
    assert b.kind == "synthetic"
    got = {
        p.pool_id: (p.token0, p.token1, p.reserve0, p.reserve1, p.fee_bps)
        for p in b.pools.values()
        if isinstance(p, ConstantProductPoolState)
    }
    assert got == CPMM_RESERVES
    assert [c.case_id for c in b.cases] == ["a_b_large", "a_b_small", "a_d_multi_hop", "a_b_dust"]


def test_mantle_fixture_is_unchanged_published_state_at_one_block() -> None:
    b = mixed()
    prov = json.loads((FIXTURES / "routing" / "mantle_mixed" / "provenance.json").read_text())
    assert set(b.pools) == {p for src in prov["composed_from"].values() for p in src["pools"]}
    for source, entry in prov["composed_from"].items():
        published = bundle(f"{source}/bundle")
        assert (published.bundle_hash, published.block) == (entry["bundle_hash"], b.block)
        for pool_id in entry["pools"]:
            assert b.pools[pool_id] == published.pools[pool_id]
    # ... and the published state is the pre-state the fork evidence starts from.
    uni = b.pools[UNI_USDT_WMNT]
    assert isinstance(uni, ConcentratedPoolState)
    (pre,) = (r for r in _cl_evidence("uniswap_v3") if r["kind"] == "pre_state")
    assert (uni.sqrt_price_x96, uni.tick, uni.liquidity) == (
        int(pre["sqrt_price_x96"]),
        int(pre["tick"]),
        int(pre["liquidity"]),
    )
    for name, pool_id in (("wmnt_usdt_25", LB25_WMNT_USDT), ("wmnt_usdt_15", LB15_WMNT_USDT)):
        lb = b.pools[pool_id]
        assert isinstance(lb, LiquidityBookPoolState) and lb.variable_fee is not None
        (lb_pre,) = (r for r in _lb_evidence(name) if r["kind"] == "state" and r["label"] == "pre")
        assert lb.active_id == int(lb_pre["active_id"])
        assert lb.variable_fee.volatility_accumulator == int(lb_pre["variable"][0])
    moe = b.pools[MOE_USDT_WMNT]
    assert isinstance(moe, ConstantProductPoolState)
    (moe_pre,) = (
        r
        for r in _classic_evidence()
        if r["kind"] == "pool_state" and r["pool"].lower() == MOE_USDT_WMNT
    )
    assert (moe.reserve0, moe.reserve1) == (int(moe_pre["reserve0"]), int(moe_pre["reserve1"]))


# ---------------------------------------------------------------------------
# Split / merge topologies against hand-derived CPMM vectors
# ---------------------------------------------------------------------------


def test_shared_suffix_merge_matches_hand_vectors_and_independent_quotes_overestimate() -> None:
    """docs/references/pre-research §4.2: split 150M A over ac_1/ac_2, merge both C
    funds into one cb_1 swap. Quoting cb_1 separately for each C amount from the
    original snapshot double-counts cb_1's liquidity by ~5.6%."""
    b = cpmm()
    case = b.case("a_b_large")
    c1 = _v2_out(100_000_000, 1_000_000_000, 1_000_000)  # ac_1
    c2 = _v2_out(50_000_000, 500_000_000, 500_000)  # ac_2
    merged = _v2_out(c1 + c2, 1_000_000, 1_000_000_000)  # cb_1
    assert (c1, c2, merged) == (90_661, 45_330, 119_395_080)
    ev = ok(
        b,
        case,
        step("ac_1", "TKA", "TKC", "c1", (R, 100_000_000)),
        step("ac_2", "TKA", "TKC", "c2", (R, ALL)),
        step("cb_1", "TKC", "TKB", "out", ("c1", ALL), ("c2", ALL)),
    )
    assert [t.amount_out for t in ev.trace] == [c1, c2, merged]
    assert ev.trace[1].inputs == ((R, 50_000_000),)  # ALL_REMAINING took the explicit remainder
    assert ev.gross_output == merged
    assert ev.route_features["merge_steps"] == 1 and ev.route_features["split_funds"] == 1

    # The wrong answer: independent fresh-state quotes of the shared suffix pool, summed.
    cb_1 = b.pools["cb_1"]
    independent = sum(quote_exact_in(cb_1, "TKC", c).amount_out for c in (c1, c2))
    assert independent == _v2_out(c1, 1_000_000, 1_000_000_000) + _v2_out(
        c2, 1_000_000, 1_000_000_000
    )
    assert independent == 126_135_946 > ev.gross_output  # a 5.65% phantom improvement


def test_unmerged_reuse_of_the_suffix_pool_sees_prior_reserves_in_submitted_order() -> None:
    b = cpmm()
    case = b.case("a_b_large")
    c1, c2 = 90_661, 45_330
    b1 = _v2_out(c1, 1_000_000, 1_000_000_000)
    b2 = _v2_out(c2, 1_000_000 + c1, 1_000_000_000 - b1)  # cb_1 after the first use
    prefix = (
        step("ac_1", "TKA", "TKC", "c1", (R, 100_000_000)),
        step("ac_2", "TKA", "TKC", "c2", (R, ALL)),
    )
    ev = ok(
        b,
        case,
        *prefix,
        step("cb_1", "TKC", "TKB", "b1", ("c1", ALL)),
        step("cb_1", "TKC", "TKB", "b2", ("c2", ALL)),
    )
    assert [t.amount_out for t in ev.trace[2:]] == [b1, b2] == [82_896_118, 36_490_222]
    assert ev.gross_output == 119_386_340
    final = ev.next_states["cb_1"]
    assert isinstance(final, ConstantProductPoolState)
    assert (final.reserve0, final.reserve1) == (1_000_000 + c1 + c2, 1_000_000_000 - b1 - b2)
    assert ev.route_features["repeated_pool_calls"] == 1

    # Reversing the two cb_1 steps is a different, equally valid plan: the evaluator
    # keeps each plan's own order rather than normalizing either into the other.
    rev = ok(
        b,
        case,
        *prefix,
        step("cb_1", "TKC", "TKB", "b2", ("c2", ALL)),
        step("cb_1", "TKC", "TKB", "b1", ("c1", ALL)),
    )
    b2r = _v2_out(c2, 1_000_000, 1_000_000_000)
    b1r = _v2_out(c1, 1_000_000 + c2, 1_000_000_000 - b2r)
    assert [t.output_fund_id for t in rev.trace] == ["c1", "c2", "b2", "b1"]
    assert [t.amount_out for t in rev.trace[2:]] == [b2r, b1r] == [43_239_828, 76_146_133]
    assert rev.gross_output == 119_385_961 != ev.gross_output


def test_shared_prefix_then_split() -> None:
    b = cpmm()
    case = b.case("a_b_large")
    c = _v2_out(150_000_000, 1_000_000_000, 1_000_000)
    x = _v2_out(40_000, 400_000, 420_000_000)  # cb_2
    y = _v2_out(c - 40_000, 1_000_000, 1_000_000_000)  # cb_1
    ev = ok(
        b,
        case,
        step("ac_1", "TKA", "TKC", "c", (R, ALL)),
        step("cb_2", "TKC", "TKB", "x", ("c", 40_000)),
        step("cb_1", "TKC", "TKB", "y", ("c", ALL)),
    )
    assert (c, x, y) == (130_094, 38_077_657, 82_420_410)
    assert [t.amount_out for t in ev.trace] == [c, x, y]
    assert ev.trace[2].inputs == (("c", 90_094),)
    assert ev.gross_output == x + y == 120_498_067


def test_split_after_merge() -> None:
    """Split A, merge the C legs into cd_1, then split the D fund over db_1/db_2."""
    b = cpmm()
    case = b.case("a_b_large")
    d = _v2_out(90_661 + 45_330, 1_000_000, 2_000_000)
    e1 = _v2_out(79_596, 2_000_000, 1_000_000_000)
    e2 = _v2_out(d - 79_596, 1_000_000, 480_000_000, fee_bps=5)
    ev = ok(
        b,
        case,
        step("ac_1", "TKA", "TKC", "c1", (R, 100_000_000)),
        step("ac_2", "TKA", "TKC", "c2", (R, ALL)),
        step("cd_1", "TKC", "TKD", "d", ("c1", ALL), ("c2", ALL)),
        step("db_1", "TKD", "TKB", "e1", ("d", 79_596)),
        step("db_2", "TKD", "TKB", "e2", ("d", ALL)),
    )
    assert (d, e1, e2) == (238_790, 38_164_299, 65_890_746)
    assert [t.amount_out for t in ev.trace[2:]] == [d, e1, e2]
    assert ev.gross_output == 104_055_045
    features = ev.route_features
    assert (features["split_funds"], features["merge_steps"], features["hops"]) == (2, 1, 5)


def test_repeated_pool_split_then_merge() -> None:
    """Two consumers of the request fund on the same physical pool ac_1: the second
    sees the reserves the first left; its independent fresh-state quote is 20% high."""
    b = cpmm()
    case = b.case("a_b_large")
    s1 = _v2_out(100_000_000, 1_000_000_000, 1_000_000)
    s2 = _v2_out(50_000_000, 1_100_000_000, 1_000_000 - s1)
    ev = ok(
        b,
        case,
        step("ac_1", "TKA", "TKC", "s1", (R, 100_000_000)),
        step("ac_1", "TKA", "TKC", "s2", (R, ALL)),
        step("cb_1", "TKC", "TKB", "out", ("s1", ALL), ("s2", ALL)),
    )
    assert [t.amount_out for t in ev.trace[:2]] == [s1, s2] == [90_661, 39_423]
    assert quote_exact_in(b.pools["ac_1"], "TKA", 50_000_000).amount_out == 47_482 > s2
    assert ev.gross_output == _v2_out(s1 + s2, 1_000_000, 1_000_000_000) == 114_804_342
    assert ev.route_features["distinct_pools"] == 2
    assert ev.route_features["repeated_pool_calls"] == 1


def test_multi_hop_chain() -> None:
    b = cpmm()
    case = b.case("a_d_multi_hop")
    h1 = _v2_out(10_000_000, 1_000_000_000, 1_000_000)
    h2 = _v2_out(h1, 1_000_000, 2_000_000)
    ev = ok(
        b,
        case,
        step("ac_1", "TKA", "TKC", "c", (R, ALL)),
        step("cd_1", "TKC", "TKD", "d", ("c", ALL)),
    )
    assert (h1, h2) == (9_871, 19_490)
    assert ev.gross_output == h2
    # A three-hop A -> C -> D -> B chain on the large-order case's token pair.
    three = ok(
        b,
        Case("chain", "TKA", "TKB", 10_000_000),
        step("ac_1", "TKA", "TKC", "c", (R, ALL)),
        step("cd_1", "TKC", "TKD", "d", ("c", ALL)),
        step("db_1", "TKD", "TKB", "out", ("d", ALL)),
    )
    assert three.gross_output == _v2_out(h2, 2_000_000, 1_000_000_000) == 9_622_277


# ---------------------------------------------------------------------------
# Shared physical-pool state against fork evidence
# ---------------------------------------------------------------------------


CL_REPEATS = [
    # (published bundle, pool, evidence source, token_in, token_out)
    ("routing/mantle_mixed", UNI_USDT_WMNT, "uniswap_v3", USDT, WMNT),
    ("agni/bundle", AGNI_USDC_WMNT, "agni_v3", USDC, WMNT),
    ("fusionx/bundle", FUSIONX_USDT_WMNT, "fusionx_v3", USDT, WMNT),
]


@pytest.mark.parametrize(("bundle_name", "pool_id", "source", "token_in", "token_out"), CL_REPEATS)
def test_cl_pool_reused_by_two_steps_matches_the_fork_sequence(
    bundle_name: str, pool_id: str, source: str, token_in: str, token_out: str
) -> None:
    """`exact_boundary_then_continue`: the pool swaps to an initialized tick, then a
    second same-direction swap continues from the crossed tick. As a split plan over
    one physical pool, the second step must see the first step's tick state."""
    b = bundle(bundle_name)
    (first, _), (second, post) = cl_sequence(source, "exact_boundary_then_continue")
    assert first["zero_for_one"] and second["zero_for_one"]
    a1, a2 = int(first["amount_specified"]), int(second["amount_specified"])
    ev = ok(
        b,
        Case("cl_repeat", token_in, token_out, a1 + a2),
        step(pool_id, token_in, token_out, "o1", (R, a1)),
        step(pool_id, token_in, token_out, "o2", (R, ALL)),
    )
    assert [t.amount_out for t in ev.trace] == [cl_out(first), cl_out(second)]
    assert ev.gross_output == cl_out(first) + cl_out(second)
    final = ev.next_states[pool_id]
    assert isinstance(final, ConcentratedPoolState)
    assert (final.sqrt_price_x96, final.tick, final.liquidity) == (
        int(post["sqrt_price_x96"]),
        int(post["tick"]),
        int(post["liquidity"]),
    )
    assert ev.trace[0].features["initialized_ticks_crossed"] >= 1
    # Quoting the second leg on the original snapshot visibly overestimates it.
    stale = quote_exact_in(b.pools[pool_id], token_in, a2).amount_out
    assert stale > cl_out(second)


@pytest.mark.parametrize(
    ("name", "pool_id"), [("wmnt_usdt_25", LB25_WMNT_USDT), ("wmnt_usdt_15", LB15_WMNT_USDT)]
)
def test_lb_pair_reused_by_two_steps_matches_the_fork_sequence(name: str, pool_id: str) -> None:
    """`repeat_x_to_y`: the same X->Y amount twice at the frozen timestamp. The second
    swap pays the volatility accumulator the first one raised and walks the bins it
    left, so it receives less."""
    b = mixed()
    (first, post1), (second, post2) = lb_sequence(name, "repeat_x_to_y")
    amount = int(first["amount_in"])
    assert int(second["amount_in"]) == amount
    ev = ok(
        b,
        Case("lb_repeat", WMNT, USDT, 2 * amount),
        step(pool_id, WMNT, USDT, "y1", (R, amount)),
        step(pool_id, WMNT, USDT, "y2", (R, ALL)),
    )
    got1, got2 = int(first["received"]), int(second["received"])
    assert [t.amount_out for t in ev.trace] == [got1, got2]
    assert post1 is not None and post2 is not None
    assert int(post2["variable"][0]) > int(post1["variable"][0])  # accumulator evolved
    final = ev.next_states[pool_id]
    assert isinstance(final, LiquidityBookPoolState) and final.variable_fee is not None
    assert final.active_id == int(post2["active_id"])
    assert final.variable_fee.volatility_accumulator == int(post2["variable"][0])
    assert (final.reserve_x, final.reserve_y) == tuple(int(v) for v in post2["reserves"])
    assert quote_exact_in(b.pools[pool_id], WMNT, amount).amount_out == got1 > got2


def test_classic_pair_second_use_sees_the_fork_post_swap_reserves() -> None:
    """The Classic evidence executes one swap from the bundle state; the second
    same-direction use is hand-derived from the reserves the fork left."""
    b = mixed()
    rec = classic_swap("usdt_wmnt_small", MOE_USDT_WMNT)
    amount = int(rec["amount_in"])
    second = _v2_out(amount, int(rec["reserve0"]), int(rec["reserve1"]))
    ev = ok(
        b,
        Case("moe_repeat", USDT, WMNT, 2 * amount),
        step(MOE_USDT_WMNT, USDT, WMNT, "w1", (R, amount)),
        step(MOE_USDT_WMNT, USDT, WMNT, "w2", (R, ALL)),
    )
    assert [t.amount_out for t in ev.trace] == [int(rec["received"]), second]
    assert quote_exact_in(b.pools[MOE_USDT_WMNT], USDT, amount).amount_out > second


def test_cross_family_split_with_repeated_pools_matches_fork_evidence() -> None:
    """USDT -> WMNT over a Uniswap v3 pool used twice, a Merchant Moe Classic pair and
    a Merchant Moe LB pair, every leg an executed fork swap from the same block."""
    b = mixed()
    case = b.case("usdt_wmnt_evidence_split")
    (u1, _), (u2, _) = cl_sequence("uniswap_v3", "exact_boundary_then_continue")
    moe = classic_swap("usdt_wmnt_small", MOE_USDT_WMNT)
    lb = lb_sequence("wmnt_usdt_25", "y_then_x")[0][0]
    assert lb["swap_for_y"] is False  # Y (USDT) -> X (WMNT) from the pre-state
    ev = ok(
        b,
        case,
        step(UNI_USDT_WMNT, USDT, WMNT, "uni1", (R, int(u1["amount_specified"]))),
        step(MOE_USDT_WMNT, USDT, WMNT, "moe", (R, int(moe["amount_in"]))),
        step(UNI_USDT_WMNT, USDT, WMNT, "uni2", (R, int(u2["amount_specified"]))),
        step(LB25_WMNT_USDT, USDT, WMNT, "lb", (R, ALL)),
    )
    expected = [cl_out(u1), int(moe["received"]), cl_out(u2), int(lb["received"])]
    assert ev.trace[3].amount_in == int(lb["amount_in"])
    assert [t.amount_out for t in ev.trace] == expected
    assert ev.gross_output == sum(expected)
    features = ev.route_features
    assert {k: features[k] for k in features if k.startswith("pool_calls")} == {
        "pool_calls": 4,
        "pool_calls_concentrated": 2,
        "pool_calls_constant_product": 1,
        "pool_calls_liquidity_book": 1,
    }
    assert (features["distinct_pools"], features["repeated_pool_calls"]) == (3, 1)
    assert features["lb_bins_swapped"] >= 1


def test_reverse_direction_split_over_two_lb_pairs_cl_and_classic() -> None:
    b = mixed()
    case = b.case("wmnt_usdt_evidence_split")
    (a1, _), (a2, _) = lb_sequence("wmnt_usdt_25", "repeat_x_to_y")
    (c1, _), (c2, _) = lb_sequence("wmnt_usdt_15", "repeat_x_to_y")
    uni = cl_sequence("uniswap_v3", "word_boundary_crossing")[0][0]
    moe = classic_swap("wmnt_usdt_small", MOE_USDT_WMNT)
    ev = ok(
        b,
        case,
        step(LB25_WMNT_USDT, WMNT, USDT, "a1", (R, int(a1["amount_in"]))),
        step(LB15_WMNT_USDT, WMNT, USDT, "c1", (R, int(c1["amount_in"]))),
        step(UNI_USDT_WMNT, WMNT, USDT, "uni", (R, int(uni["amount_specified"]))),
        step(LB25_WMNT_USDT, WMNT, USDT, "a2", (R, int(a2["amount_in"]))),
        step(MOE_USDT_WMNT, WMNT, USDT, "moe", (R, int(moe["amount_in"]))),
        step(LB15_WMNT_USDT, WMNT, USDT, "c2", (R, ALL)),
    )
    expected = [
        int(a1["received"]),
        int(c1["received"]),
        cl_out(uni),
        int(a2["received"]),
        int(moe["received"]),
        int(c2["received"]),
    ]
    assert [t.amount_out for t in ev.trace] == expected
    assert ev.gross_output == sum(expected)


# ---------------------------------------------------------------------------
# Rejections: double spend, unready/external funds, wrong token, cycles, ids, residuals
# ---------------------------------------------------------------------------

A_B = Case("a_b", "TKA", "TKB", 150_000_000)

REJECTED: list[tuple[str, Case, tuple[SwapStep, ...], str]] = [
    (
        "double_spend_across_steps",
        A_B,
        (
            step("ac_1", "TKA", "TKC", "c1", (R, 100_000_000)),
            step("ac_2", "TKA", "TKC", "c2", (R, 100_000_000)),
            step("cb_1", "TKC", "TKB", "out", ("c1", ALL), ("c2", ALL)),
        ),
        "step 1: fund 'REQUEST' balance 50000000 is less than requested 100000000",
    ),
    (
        "double_spend_within_one_step",
        A_B,
        (step("ab_1", "TKA", "TKB", "out", (R, 100_000_000), (R, 100_000_000)),),
        "step 0: fund 'REQUEST' is referenced more than once by one step",
    ),
    (
        "reuse_of_a_drained_fund",
        A_B,
        (
            step("ab_1", "TKA", "TKB", "o1", (R, ALL)),
            step("ab_1", "TKA", "TKB", "o2", (R, ALL)),
        ),
        "step 1: input fund 'REQUEST' is not available (already fully consumed",
    ),
    (
        "consumer_before_producer",
        A_B,
        (
            step("cb_1", "TKC", "TKB", "out", ("c", ALL)),
            step("ac_1", "TKA", "TKC", "c", (R, ALL)),
        ),
        "step 0: input fund 'c' is not available: not yet produced (its producer is step 1",
    ),
    (
        "step_consumes_its_own_output",
        A_B,
        (step("ab_1", "TKA", "TKB", "out", (R, ALL), ("out", 0)),),
        "not yet produced (its producer is step 0",
    ),
    (
        "external_funding",
        A_B,
        (step("ab_1", "TKA", "TKB", "out", (R, ALL), ("WALLET", 1)),),
        "input fund 'WALLET' is not available: external funding",
    ),
    (
        "unproduced_fund_is_not_zero",
        A_B,
        (step("ab_1", "TKA", "TKB", "out", (R, ALL), ("GHOST", 0)),),
        "input fund 'GHOST' is not available: external funding",
    ),
    (
        "fund_token_differs_from_step_token_in",
        A_B,
        (step("cb_1", "TKC", "TKB", "out", (R, ALL)),),
        "input fund 'REQUEST' holds 'TKA', not the step's token_in 'TKC'",
    ),
    (
        "merge_of_different_tokens",
        A_B,
        (
            step("ac_1", "TKA", "TKC", "c", (R, 100_000_000)),
            step("cb_1", "TKC", "TKB", "out", ("c", ALL), (R, ALL)),
        ),
        "step 1: input fund 'REQUEST' holds 'TKA', not the step's token_in 'TKC'",
    ),
    (
        "token_out_not_the_pools_other_token",
        A_B,
        (step("ac_1", "TKA", "TKB", "out", (R, ALL)),),
        "declared token_out 'TKB' does not match pool's other token 'TKC'",
    ),
    (
        "token_in_not_in_pool",
        A_B,
        (step("cd_1", "TKA", "TKD", "out", (R, ALL)),),
        "declared token_in 'TKA' is not a token of pool 'cd_1'",
    ),
    (
        "economic_token_cycle",
        A_B,
        (
            step("ac_1", "TKA", "TKC", "c", (R, 100_000_000)),
            step("ac_2", "TKA", "TKC", "c2", (R, ALL)),
            step("ac_2", "TKC", "TKA", "a", ("c", ALL)),
            step("ab_1", "TKA", "TKB", "out", ("a", ALL)),
            step("cb_1", "TKC", "TKB", "out2", ("c2", ALL)),
        ),
        "economic token cycle: TKA -> TKC -> TKA",
    ),
    (
        "duplicate_output_fund_id",
        A_B,
        (
            step("ab_1", "TKA", "TKB", "out", (R, 100_000_000)),
            step("ab_1", "TKA", "TKB", "out", (R, ALL)),
        ),
        "step 1: output fund id 'out' is not distinct (already output of step 0)",
    ),
    (
        "output_reuses_the_request_fund_id",
        A_B,
        (step("ab_1", "TKA", "TKB", R, (R, ALL)),),
        "output fund id 'REQUEST' is not distinct (already the request fund)",
    ),
    (
        "unknown_pool",
        A_B,
        (step("nope", "TKA", "TKB", "out", (R, ALL)),),
        "step 0: unknown pool 'nope'",
    ),
    (
        "no_inputs",
        A_B,
        (step("ab_1", "TKA", "TKB", "out"),),
        "step 0: no input-fund references",
    ),
    (
        "negative_amount",
        A_B,
        (step("ab_1", "TKA", "TKB", "out", (R, -1)),),
        "input amount must be a non-negative int or 'ALL_REMAINING', got -1",
    ),
    (
        "boolean_amount",
        A_B,
        (step("ab_1", "TKA", "TKB", "out", (R, True)),),
        "input amount must be a non-negative int or 'ALL_REMAINING', got True",
    ),
    (
        "non_positive_order_input",
        Case("zero", "TKA", "TKB", 0),
        (step("ab_1", "TKA", "TKB", "out", (R, ALL)),),
        "order input must be a positive integer, got 0",
    ),
    (
        "pool_cannot_fill_the_exact_input",
        A_B,
        (step("ab_dry", "TKA", "TKB", "out", (R, ALL)),),
        "step 0: pool 'ab_dry' quote failed (insufficient_liquidity)",
    ),
]


@pytest.mark.parametrize(
    ("case", "steps", "message"),
    [pytest.param(c, s, m, id=name) for name, c, s, m in REJECTED],
)
def test_invalid_plans_fail_with_a_specific_reason(
    case: Case, steps: tuple[SwapStep, ...], message: str
) -> None:
    ev = run(cpmm(), case, *steps)
    assert ev.status is EvalStatus.INVALID_PLAN
    assert ev.error is not None and message in ev.error, ev.error
    assert ev.gross_output == 0 and ev.estimated_cost is None


def test_unconsumed_intermediate_is_a_reported_residual() -> None:
    ev = run(
        cpmm(),
        A_B,
        step("ac_1", "TKA", "TKC", "c", (R, ALL)),
        step("cb_1", "TKC", "TKB", "out", ("c", 100_000)),
    )
    assert ev.status is EvalStatus.INVALID_PLAN
    assert ev.residuals == {"c": 130_094 - 100_000}
    assert ev.error is not None and "'c' (TKC, unconsumed output of step 0): 30094" in ev.error
    # The target-token terminal balance is reported but does not make the plan valid.
    assert ev.gross_output == _v2_out(100_000, 1_000_000, 1_000_000_000)


def test_integer_remainder_needs_an_explicit_final_allocation() -> None:
    """1,000,000 split in thirds over one pool: ALL_REMAINING and an explicit 333,334
    final allocation are the same plan; three 333,333 legs leave 1 wei, which is a
    residual -- never silently donated or folded into a leg."""
    b = cpmm()
    case = b.case("a_b_small")
    third = 333_333

    def thirds(last: int | Literal["ALL_REMAINING"]) -> Evaluation:
        return run(
            b,
            case,
            step("ab_1", "TKA", "TKB", "o1", (R, third)),
            step("ab_1", "TKA", "TKB", "o2", (R, third)),
            step("ab_1", "TKA", "TKB", "o3", (R, last)),
        )

    via_all, explicit = thirds(ALL), thirds(333_334)
    assert via_all == explicit and via_all.status is EvalStatus.OK
    outs, reserves = [], (200_000_000, 200_000_000)
    for amount in (third, third, 333_334):
        o = _v2_out(amount, *reserves)
        outs.append(o)
        reserves = (reserves[0] + amount, reserves[1] - o)
    assert [t.amount_out for t in via_all.trace] == outs == [331_781, 330_681, 329_586]
    dust = thirds(third)
    assert dust.status is EvalStatus.INVALID_PLAN
    assert dust.residuals == {R: 1}
    assert dust.error is not None and "unallocated order input" in dust.error


def test_zero_input_steps_are_deterministic_and_never_call_the_pool() -> None:
    """`ab_dry` has no input-side reserve, so any pool call on it fails; zero-input
    steps on it succeed with zero output, and a zero fund feeds a further zero step."""
    b = cpmm()
    case = b.case("a_b_small")
    steps = (
        step("ab_dry", "TKA", "TKB", "z", (R, 0)),
        step("ab_1", "TKA", "TKB", "out", (R, ALL)),
    )
    ev = ok(b, case, *steps)
    assert ev.trace[0].status == "zero_input" and ev.trace[0].amount_out == 0
    assert "ab_dry" not in ev.next_states
    assert ev.gross_output == _v2_out(1_000_000, 200_000_000, 200_000_000)
    assert (ev.route_features["zero_input_steps"], ev.route_features["pool_calls"]) == (1, 1)
    assert ok(b, case, *steps) == ev  # deterministic
    # A zero reference to a fund an earlier step drained is not a free zero step.
    late = run(b, case, *steps, step("ab_dry", "TKA", "TKB", "z2", (R, 0)))
    assert late.status is EvalStatus.INVALID_PLAN
    assert "'REQUEST' is not available (already fully consumed" in (late.error or "")

    chained = ok(
        b,
        Case("chain", "TKA", "TKB", 1_000_000),
        step("ac_1", "TKA", "TKC", "zc", (R, 0)),
        step("cb_1", "TKC", "TKB", "zb", ("zc", ALL)),  # a produced zero fund, not "missing"
        step("ab_1", "TKA", "TKB", "out", (R, ALL)),
    )
    assert [t.status for t in chained.trace] == ["zero_input", "zero_input", "ok"]
    assert set(chained.next_states) == {"ab_1"}


def test_dust_order_evaluates_exactly() -> None:
    b = cpmm()
    ev = ok(b, b.case("a_b_dust"), step("ab_1", "TKA", "TKB", "out", (R, ALL)))
    assert ev.gross_output == _v2_out(3, 200_000_000, 200_000_000) == 2


# ---------------------------------------------------------------------------
# Isolation: no evaluation can affect the bundle or another evaluation
# ---------------------------------------------------------------------------


def test_evaluation_order_cannot_mutate_the_bundle_or_another_plans_state() -> None:
    b = mixed()
    originals = dict(b.pools)
    (a1, _), _ = lb_sequence("wmnt_usdt_25", "repeat_x_to_y")
    amount = int(a1["amount_in"])
    jobs = [
        (
            Case("reuse", WMNT, USDT, 2 * amount),
            (
                step(LB25_WMNT_USDT, WMNT, USDT, "y1", (R, amount)),
                step(LB25_WMNT_USDT, WMNT, USDT, "y2", (R, ALL)),
            ),
        ),
        (
            Case("single", WMNT, USDT, amount),
            (step(LB25_WMNT_USDT, WMNT, USDT, "y", (R, ALL)),),
        ),
        (
            b.case("usdt_wmnt_evidence_split"),
            (
                step(UNI_USDT_WMNT, USDT, WMNT, "u", (R, 10_000_000)),
                step(LB25_WMNT_USDT, USDT, WMNT, "l", (R, ALL)),
            ),
        ),
    ]
    forward = [run(b, c, *s).to_dict() for c, s in jobs]
    backward = [run(b, c, *s).to_dict() for c, s in reversed(jobs)][::-1]
    assert forward == backward
    # The single-step plan saw the fresh pool, not the state the reuse plan left.
    assert forward[1]["gross_output"] == a1["received"]
    assert dict(b.pools) == dict(load_bundle(FIXTURES / "routing" / "mantle_mixed").pools)
    assert all(b.pools[k] is v for k, v in originals.items())

"""Tests verifying all calculations, transitions, and reproducible examples
published in docs/references/routing-algorithms.md.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from benchmark.objective import gross_only
from benchmark.profile import load_profile
from benchmark.strategies import metis_graph_settings
from pools.constant_product import quote_exact_in as cp_quote
from routing.algorithms import (
    direct,
    direct_split,
    incremental_graph,
    metis_inspired,
    path_split,
    single_path,
    uni_sor_fast,
    uni_sor_port,
    uni_sor_strategies,
)
from routing.algorithms.base import (
    AlgorithmConfig,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
)
from routing.search import QuoteCache, path_label
from snapshot.bundle import load_bundle
from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle

ROOT = Path(__file__).resolve().parents[2]


def make_teaching_bundle() -> SnapshotBundle:
    """Build the shared synthetic pool graph for reproducible teaching examples."""
    block = BlockRef(chain_id=5000, number=1, hash="0x" + "00" * 32, timestamp=0)

    def cp(pid: str, t0: str, t1: str, r0: int, r1: int, fee: int = 30) -> ConstantProductPoolState:
        return ConstantProductPoolState(
            pool_id=pid,
            token0=min(t0, t1),
            token1=max(t0, t1),
            reserve0=r0 if t0 < t1 else r1,
            reserve1=r1 if t0 < t1 else r0,
            fee_bps=fee,
            source_key="moe_classic_v1",
        )

    pools = {
        "P_AB1": cp("P_AB1", "TKA", "TKB", 100_000, 100_000),
        "P_AB2": cp("P_AB2", "TKA", "TKB", 200_000, 180_000),
        "P_AC": cp("P_AC", "TKA", "TKC", 100_000, 200_000),
        "P_CB": cp("P_CB", "TKC", "TKB", 200_000, 150_000),
        "P_AD": cp("P_AD", "TKA", "TKD", 100_000, 150_000),
        "P_DB": cp("P_DB", "TKD", "TKB", 150_000, 120_000),
        "P_CD": cp("P_CD", "TKC", "TKD", 100_000, 100_000),
    }

    return SnapshotBundle(
        bundle_id="synthetic_teaching_bundle",
        kind="synthetic",
        schema_version=1,
        block=block,
        pools=pools,
        cases=(),
        bundle_hash="teaching_bundle_v1",
        source_path="<teaching_examples>",
    )


@pytest.fixture
def teaching_bundle() -> SnapshotBundle:
    return make_teaching_bundle()


def _synthetic_bundle(bundle_id: str, *pools: ConstantProductPoolState) -> SnapshotBundle:
    block = BlockRef(chain_id=5000, number=1, hash="0x" + "00" * 32, timestamp=0)
    return SnapshotBundle(
        bundle_id=bundle_id,
        kind="synthetic",
        schema_version=1,
        block=block,
        pools={p.pool_id: p for p in pools},
        cases=(),
        bundle_hash=f"{bundle_id}_v1",
        source_path="<teaching_examples>",
    )


def make_narrow_bundle() -> SnapshotBundle:
    """The narrow-optimum pools of tests/routing/test_uni_sor_fast.py (§8.5 step 8)."""

    def cp(pid: str, t0: str, t1: str, r0: int, r1: int) -> ConstantProductPoolState:
        return ConstantProductPoolState(pid, t0, t1, r0, r1, fee_bps=30)

    return _synthetic_bundle(
        "narrow_optimum",
        cp("d0", "A", "B", 10**6, 896533),
        cp("d1", "A", "B", 10**7, 11182254),
        cp("ax", "A", "X", 10**6, 10**9),
        cp("xb", "X", "B", 10**6, 10**7),
    )


def make_wide_bundle() -> SnapshotBundle:
    """Nine equal-price pools of increasing depth plus one thin, better-priced pool (§9.5 b)."""
    pools = [
        ConstantProductPoolState(f"D{k}", "A", "B", k * 10**6, k * 10**6, fee_bps=30)
        for k in range(1, 10)
    ]
    pools.append(ConstantProductPoolState("T", "A", "B", 300_000, 600_000, fee_bps=30))
    return _synthetic_bundle("wide_shortlist", *pools)


def solve_strategy(
    name: str,
    bundle: SnapshotBundle,
    case: Case,
    search: dict[str, int],
    budget: Budget | None = None,
) -> SolveResult:
    """Run a named optimized strategy with exactly its registered L08 recipe settings."""
    strategy = uni_sor_strategies.STRATEGIES[name]
    recipe = uni_sor_strategies.registered_settings(strategy.recipe)
    params: dict[str, object] = {**search, **recipe["shortlist"], **recipe["sampling"]}
    params["controls"] = recipe["controls"]
    factory = (
        uni_sor_strategies.ADAPTIVE_FACTORY
        if name == uni_sor_strategies.ADAPTIVE
        else uni_sor_strategies.OPTIMIZED_FACTORY
    )
    assert factory.prepare is not None
    prepared = factory.prepare(bundle, AlgorithmConfig(name, params))
    ctx = SolveContext(bundle=bundle, objective=gross_only(), prepared=prepared)
    return factory.solve(case, ctx, budget or Budget())


def test_hand_calculated_cpmm_formula(teaching_bundle: SnapshotBundle) -> None:
    """Verify teaching model constant product derivation against quote_exact_in."""
    p1 = teaching_bundle.pools["P_AB1"]
    assert isinstance(p1, ConstantProductPoolState)
    dx = 10_000
    fee_bps = 30
    in_with_fee = dx * (10_000 - fee_bps)  # 99,700,000
    num = in_with_fee * p1.reserve1  # 99,700,000 * 100,000 = 9,970,000,000,000
    den = p1.reserve0 * 10_000 + in_with_fee  # 100,000 * 10,000 + 99,700,000 = 1,099,700,000
    expected_dy = num // den  # 9066
    q = cp_quote(p1, "TKA", dx)
    assert q.amount_out == 9066
    assert q.amount_out == expected_dy


def test_direct_competing_pools_and_truncation(teaching_bundle: SnapshotBundle) -> None:
    """Verify direct picks the best direct pool and respects candidate caps."""
    case = Case(case_id="ex_direct", token_in="TKA", token_out="TKB", amount_in=10_000)
    ctx = SolveContext(bundle=teaching_bundle, objective=gross_only())

    # Full solve: P_AB1 (9066) beats P_AB2 (8546)
    res = direct.solve(case, ctx, Budget())
    assert res.status == SolveStatus.OK
    assert res.plan is not None and res.evaluation is not None
    assert res.evaluation.gross_output == 9066
    assert res.plan.steps[0].pool_id == "P_AB1"
    assert res.evaluation.trace[0].amount_in == 10000
    assert res.evaluation.trace[0].amount_out == 9066
    assert res.candidates_considered == 2
    assert res.candidates_truncated == 0

    # Truncation via max_candidates=1
    res_trunc = direct.solve(case, ctx, Budget(max_candidates=1))
    assert res_trunc.candidates_considered == 1
    assert res_trunc.candidates_truncated == 1


def test_single_path_hop_major_and_better_multihop(teaching_bundle: SnapshotBundle) -> None:
    """Verify single_path prefers better 2-hop route over direct pools."""
    case = Case(case_id="ex_sp", token_in="TKA", token_out="TKB", amount_in=10_000)
    prep = single_path.prepare(teaching_bundle, AlgorithmConfig("single_path", {"max_hops": 3}))
    ctx = SolveContext(bundle=teaching_bundle, objective=gross_only(), prepared=prep)

    res = single_path.solve(case, ctx, Budget())
    assert res.status == SolveStatus.OK
    assert res.plan is not None and res.evaluation is not None
    assert res.evaluation.gross_output == 12434
    step_ids = [s.pool_id for s in res.plan.steps]
    assert step_ids == ["P_AC", "P_CB"]
    assert res.evaluation.trace[0].amount_in == 10000
    assert res.evaluation.trace[0].amount_out == 18132
    assert res.evaluation.trace[1].amount_in == 18132
    assert res.evaluation.trace[1].amount_out == 12434
    assert res.search_stats["quotes_executed"] == 10
    assert res.search_stats["quotes_memoized"] == 2


def test_direct_split_dp_transition_and_remainder(teaching_bundle: SnapshotBundle) -> None:
    """Verify direct_split DP transitions, split gain, and remainder handling."""
    cfg = {"max_splits": 2, "percent_step": 10}
    prep = direct_split.prepare(teaching_bundle, AlgorithmConfig("direct_split", cfg))

    # 1. Divisible input: 10,000 TKA splits 70% P_AB1 (6523) + 30% P_AB2 (2652) = 9175
    case = Case(case_id="ex_ds", token_in="TKA", token_out="TKB", amount_in=10_000)
    ctx = SolveContext(bundle=teaching_bundle, objective=gross_only(), prepared=prep)
    res = direct_split.solve(case, ctx, Budget())
    assert res.status == SolveStatus.OK
    assert res.evaluation is not None and res.evaluation.gross_output == 9175
    alloc = res.search_stats.get("best_allocation")
    assert alloc == [
        {"pool_id": "P_AB1", "percent": 70, "amount_in": "7000"},
        {"pool_id": "P_AB2", "percent": 30, "amount_in": "3000"},
    ]
    assert res.evaluation.trace[0].amount_in == 7000
    assert res.evaluation.trace[0].amount_out == 6523
    assert res.evaluation.trace[1].amount_in == 3000
    assert res.evaluation.trace[1].amount_out == 2652

    # 2. Nondivisible input: 10,005 TKA with percent_step=10 (N=10)
    # Competing quotes at 10005: P_AB1 -> 9070, P_AB2 -> 8551
    p1 = teaching_bundle.pools["P_AB1"]
    p2 = teaching_bundle.pools["P_AB2"]
    assert isinstance(p1, ConstantProductPoolState) and isinstance(p2, ConstantProductPoolState)
    assert cp_quote(p1, "TKA", 10005).amount_out == 9070
    assert cp_quote(p2, "TKA", 10005).amount_out == 8551

    # Leg 1: floor(10005 * 7 / 10) = 7003
    # Leg 2: 10005 - 7003 = 3002 (ALL_REMAINING, carrying remainder 1)
    case_nondiv = Case(case_id="ex_ds_nondiv", token_in="TKA", token_out="TKB", amount_in=10_005)
    res_nondiv = direct_split.solve(case_nondiv, ctx, Budget())
    assert res_nondiv.evaluation is not None
    assert res_nondiv.evaluation.gross_output == 9179
    alloc_nondiv = res_nondiv.search_stats.get("best_allocation")
    assert alloc_nondiv == [
        {"pool_id": "P_AB1", "percent": 70, "amount_in": "7003"},
        {"pool_id": "P_AB2", "percent": 30, "amount_in": "3002"},
    ]
    assert res_nondiv.evaluation.trace[0].amount_in == 7003
    assert res_nondiv.evaluation.trace[0].amount_out == 6526
    assert res_nondiv.evaluation.trace[1].amount_in == 3002
    assert res_nondiv.evaluation.trace[1].amount_out == 2653
    assert res_nondiv.search_stats["quotes_executed"] == 25

    # 3. Small input where split does not help: 10 TKA -> single pool wins
    case_small = Case(case_id="ex_ds_small", token_in="TKA", token_out="TKB", amount_in=10)
    res_small = direct_split.solve(case_small, ctx, Budget())
    assert res_small.search_stats.get("best_splits") == 1


def test_path_split_conflict_check_and_disjoint_allocation(
    teaching_bundle: SnapshotBundle,
) -> None:
    """Verify path_split finds disjoint multi-hop routes and rejects conflicting pools."""
    cfg = {"max_hops": 3, "max_splits": 2, "percent_step": 10}
    prep = path_split.prepare(teaching_bundle, AlgorithmConfig("path_split", cfg))
    case = Case(case_id="ex_ps", token_in="TKA", token_out="TKB", amount_in=10_000)
    ctx = SolveContext(bundle=teaching_bundle, objective=gross_only(), prepared=prep)

    res = path_split.solve(case, ctx, Budget())
    assert res.status == SolveStatus.OK
    assert res.evaluation is not None and res.evaluation.gross_output == 12581
    assert res.plan is not None

    # Step pool IDs: P_AC, P_CB for path 1 and P_AD, P_DB for path 2
    step_pools = [s.pool_id for s in res.plan.steps]
    assert step_pools == ["P_AC", "P_CB", "P_AD", "P_DB"]
    path1_pools = {"P_AC", "P_CB"}
    path2_pools = {"P_AD", "P_DB"}
    assert path1_pools.isdisjoint(path2_pools)

    # Check exact evaluated intermediate amounts:
    assert res.evaluation.trace[0].amount_in == 8000
    assert res.evaluation.trace[0].amount_out == 14773
    assert res.evaluation.trace[1].amount_in == 14773
    assert res.evaluation.trace[1].amount_out == 10288
    assert res.evaluation.trace[2].amount_in == 2000
    assert res.evaluation.trace[2].amount_out == 2932
    assert res.evaluation.trace[3].amount_in == 2932
    assert res.evaluation.trace[3].amount_out == 2293
    assert 10288 + 2293 == 12581
    assert res.search_stats["bnb_conflicts_excluded"] == 13
    assert res.search_stats["bnb_nodes"] == 12
    assert res.search_stats["bnb_best_gross"] == {"1": "12434", "2": "12581"}

    # Verify intermediate quote of Path 4 (P_AC -> P_CD -> P_DB) at 4 units (4000 TKA):
    p_ac = teaching_bundle.pools["P_AC"]
    p_cd = teaching_bundle.pools["P_CD"]
    p_db = teaching_bundle.pools["P_DB"]
    assert isinstance(p_ac, ConstantProductPoolState)
    assert isinstance(p_cd, ConstantProductPoolState)
    assert isinstance(p_db, ConstantProductPoolState)
    out_ac_4k = cp_quote(p_ac, "TKA", 4000).amount_out
    out_cd_4k = cp_quote(p_cd, "TKC", out_ac_4k).amount_out
    out_path4_4k = cp_quote(p_db, "TKD", out_cd_4k).amount_out
    assert out_ac_4k == 7670
    assert out_cd_4k == 7103
    assert out_path4_4k == 5409


def test_incremental_graph_shared_pool_and_merged_plan(
    teaching_bundle: SnapshotBundle,
) -> None:
    """Verify incremental_graph shares intermediate pool and executes merged swap."""
    cfg = {"max_hops": 3, "max_splits": 2, "percent_step": 10, "chunks": 10}
    prep = incremental_graph.prepare(teaching_bundle, AlgorithmConfig("incremental_graph", cfg))
    case = Case(case_id="ex_ig", token_in="TKA", token_out="TKB", amount_in=10_000)
    ctx = SolveContext(bundle=teaching_bundle, objective=gross_only(), prepared=prep)

    res = incremental_graph.solve(case, ctx, Budget())
    assert res.status == SolveStatus.OK
    assert res.evaluation is not None and res.evaluation.gross_output == 12892
    assert res.search_stats.get("topology") == "shared_pool"
    assert res.plan is not None

    # Verify merged plan has exactly one step per physical pool used:
    pids = [s.pool_id for s in res.plan.steps]
    assert len(pids) == len(set(pids)), "Merged plan must not reuse physical pools sequentially"
    assert pids == ["P_AC", "P_CD", "P_CB", "P_DB"]

    # Check exact evaluated intermediate amounts:
    assert res.evaluation.trace[0].amount_in == 10000
    assert res.evaluation.trace[0].amount_out == 18132
    assert res.evaluation.trace[1].amount_in == 5562
    assert res.evaluation.trace[1].amount_out == 5253
    assert res.evaluation.trace[2].amount_in == 12570
    assert res.evaluation.trace[2].amount_out == 8844
    assert res.evaluation.trace[3].amount_in == 5253
    assert res.evaluation.trace[3].amount_out == 4048
    assert 8844 + 4048 == 12892
    assert res.search_stats["incremental_chunk_sequence"] == [0, 1, 1, 0, 1, 1, 1, 0, 1, 1]


def test_incremental_graph_dust_carry_and_fallback(teaching_bundle: SnapshotBundle) -> None:
    """Verify incremental_graph zero/dust carry with tiny input and fallback preservation."""
    # 1. Dust input A=3, K=20: 17 empty chunks, 1 carried, 2 allocated
    cfg = {"max_hops": 3, "max_splits": 2, "percent_step": 10, "chunks": 20}
    prep = incremental_graph.prepare(teaching_bundle, AlgorithmConfig("incremental_graph", cfg))
    case_dust = Case(case_id="c_dust", token_in="TKA", token_out="TKB", amount_in=3)
    ctx = SolveContext(bundle=teaching_bundle, objective=gross_only(), prepared=prep)

    res_dust = incremental_graph.solve(case_dust, ctx, Budget())
    assert res_dust.status == SolveStatus.OK
    assert res_dust.search_stats["chunks_empty"] == 17
    assert res_dust.search_stats["chunks_carried"] == 1
    assert res_dust.search_stats["chunks_allocated"] == 2
    assert res_dust.search_stats["incremental_status"] == "ok"

    # 2. Zero-marginal final chunk commit: reserves (100, 10), A=100, K=10
    block = BlockRef(chain_id=5000, number=1, hash="0x" + "00" * 32, timestamp=0)
    p_zero = ConstantProductPoolState(
        pool_id="P_zero",
        token0="TKA",
        token1="TKB",
        reserve0=100,
        reserve1=10,
        fee_bps=30,
        source_key="moe_classic_v1",
    )
    b_zero = SnapshotBundle(
        bundle_id="syn_zero",
        kind="synthetic",
        schema_version=1,
        block=block,
        pools={"P_zero": p_zero},
        cases=(),
        bundle_hash="h_zero",
        source_path="<test>",
    )
    case_zero = Case(case_id="c_zero", token_in="TKA", token_out="TKB", amount_in=100)
    cfg_zero = {"max_hops": 1, "max_splits": 1, "percent_step": 10, "chunks": 10}
    prep_zero = incremental_graph.prepare(b_zero, AlgorithmConfig("incremental_graph", cfg_zero))
    ctx_zero = SolveContext(bundle=b_zero, objective=gross_only(), prepared=prep_zero)
    res_zero = incremental_graph.solve(case_zero, ctx_zero, Budget())
    assert res_zero.status == SolveStatus.OK
    assert res_zero.search_stats["incremental_status"] == "ok"
    assert res_zero.search_stats["incremental_accounted_gross"] == "4"
    assert res_zero.search_stats["chunks_carried"] == 5

    # 3. Fallback preservation: when incremental search produces equal gross to path_split on A=10,
    # simpler route wins the tie (retaining single_path as the chosen source).
    case_small = Case(case_id="c_small", token_in="TKA", token_out="TKB", amount_in=10)
    res_small = incremental_graph.solve(case_small, ctx, Budget())
    assert res_small.status == SolveStatus.OK
    assert res_small.search_stats["chosen_source"] == "single_path"


def test_uni_sor_port_selection_and_parity_behavior(
    teaching_bundle: SnapshotBundle,
) -> None:
    """Verify uni_sor_port selects best routes and obeys port contract."""
    # Test with max_hops=1 on A=10005: 2 pools x 10 = 20 table quotes + 1 replay = 21 quotes
    cfg_h1 = {"max_hops": 1, "max_splits": 2, "percent_step": 10}
    prep_h1 = uni_sor_port.prepare(teaching_bundle, AlgorithmConfig("uni_sor_port", cfg_h1))
    case_nondiv = Case(case_id="ex_sor_nondiv", token_in="TKA", token_out="TKB", amount_in=10_005)
    ctx_h1 = SolveContext(bundle=teaching_bundle, objective=gross_only(), prepared=prep_h1)
    res_h1 = uni_sor_port.solve(case_nondiv, ctx_h1, Budget())
    assert res_h1.status == SolveStatus.OK
    assert res_h1.search_stats["quotes_executed"] == 21

    # Test with max_hops=2 on A=10001: 60 table quotes + 2 replay quotes = 62 quotes
    case_nondiv2 = Case(case_id="ex_sor_nondiv2", token_in="TKA", token_out="TKB", amount_in=10_001)
    cfg = {"max_hops": 2, "max_splits": 2, "percent_step": 10}
    prep = uni_sor_port.prepare(teaching_bundle, AlgorithmConfig("uni_sor_port", cfg))
    ctx_h2 = SolveContext(bundle=teaching_bundle, objective=gross_only(), prepared=prep)
    res_h2 = uni_sor_port.solve(case_nondiv2, ctx_h2, Budget())
    assert res_h2.status == SolveStatus.OK
    assert res_h2.search_stats["quotes_executed"] == 62
    assert res_h2.search_stats["d1_residual"] == "1"

    # Test with max_hops=2 on A=10000: 4 routes (2 1-hop + 2 2-hop) x 10 buckets = 60 pool quotes
    case = Case(case_id="ex_sor", token_in="TKA", token_out="TKB", amount_in=10_000)
    ctx = SolveContext(bundle=teaching_bundle, objective=gross_only(), prepared=prep)

    res = uni_sor_port.solve(case, ctx, Budget())
    assert res.status == SolveStatus.OK
    assert res.evaluation is not None and res.evaluation.gross_output == 12581
    assert res.search_stats["quotes_executed"] == 60
    assert res.search_stats.get("requote_delta") == "0"
    assert res.evaluation.trace[0].amount_in == 8000
    assert res.evaluation.trace[0].amount_out == 14773
    assert res.evaluation.trace[1].amount_in == 14773
    assert res.evaluation.trace[1].amount_out == 10288
    assert res.evaluation.trace[2].amount_in == 2000
    assert res.evaluation.trace[2].amount_out == 2932
    assert res.evaluation.trace[3].amount_in == 2932
    assert res.evaluation.trace[3].amount_out == 2293


TEACHING_SOR5 = {"max_hops": 2, "max_splits": 2, "percent_step": 5}


def _rounds(res: SolveResult) -> list[tuple[str, list[int], int, list[str] | None, str | None]]:
    return [
        (r["kind"], r["added_percents"], r["table_quotes"], r["selection"], r["incumbent"])
        for r in res.search_stats["sampling"]["rounds"]
    ]


def test_uni_sor_adaptive_coarse_to_fine_rounds(teaching_bundle: SnapshotBundle) -> None:
    """Verify §8.5: probe table, coarse 75/25, refinement to 80/20, fixed point, 66 quotes."""
    case = Case(case_id="ex_sor_adaptive", token_in="TKA", token_out="TKB", amount_in=10_000)

    # Hand-checked legs of the coarse 75 % / 25 % selection
    pools = teaching_bundle.pools
    ac, cb, ad, db = pools["P_AC"], pools["P_CB"], pools["P_AD"], pools["P_DB"]
    assert isinstance(ac, ConstantProductPoolState) and isinstance(cb, ConstantProductPoolState)
    assert isinstance(ad, ConstantProductPoolState) and isinstance(db, ConstantProductPoolState)
    assert cp_quote(ac, "TKA", 7500).amount_out == 13914
    assert cp_quote(cb, "TKC", 13914).amount_out == 9729
    assert cp_quote(ad, "TKA", 2500).amount_out == 3647
    assert cp_quote(db, "TKD", 3647).amount_out == 2840

    # Reference on the same 5 % grid: 120 quotes, 80/20 = 12581
    prep = uni_sor_port.prepare(teaching_bundle, AlgorithmConfig("uni_sor_port", TEACHING_SOR5))
    ctx = SolveContext(bundle=teaching_bundle, objective=gross_only(), prepared=prep)
    ref = uni_sor_port.solve(case, ctx, Budget())
    assert ref.evaluation is not None and ref.evaluation.gross_output == 12581
    assert ref.search_stats["quotes_executed"] == 120

    res = solve_strategy(uni_sor_strategies.ADAPTIVE, teaching_bundle, case, TEACHING_SOR5)
    assert res.status == SolveStatus.OK and res.algorithm == "uni_sor_adaptive"
    assert res.evaluation is not None and res.evaluation.gross_output == 12581
    assert res.plan == ref.plan
    s = res.search_stats
    assert s["quotes_executed"] == 66 and s["quotes_memoized"] == 34
    assert s["shortlist"]["quotes"] == {
        "probe": 24, "shortlist_table": 42, "fallback_table": 0, "validation": 0
    }
    assert s["shortlist"]["probe_entries"] == 16
    assert s["search_scope"] == "full_cohort"
    sm = s["sampling"]
    assert sm["coarse_percents"] == [25, 50, 75, 100]
    assert sm["sampled_percents"] == [5, 15, 20, 25, 30, 50, 70, 75, 80, 85, 100]
    assert (sm["sampled_entries"], sm["grid_entries"]) == (44, 80)
    assert sm["seed"] == {"route_id": "V2:P_AC>P_CB", "outcome": "improved"}
    assert sm["validations"] == 3 and sm["stop_reason"] == "converged"
    assert _rounds(res) == [
        ("coarse", [25, 50, 75, 100], 0, ["V2:P_AC>P_CB@75", "V2:P_AD>P_DB@25"], "improved"),
        ("refine", [5, 20, 30, 70, 80], 30, ["V2:P_AC>P_CB@80", "V2:P_AD>P_DB@20"], "improved"),
        ("refine", [15, 85], 12, ["V2:P_AC>P_CB@80", "V2:P_AD>P_DB@20"], "unchanged"),
    ]
    assert 9729 + 2840 == 12569 and 10288 + 2293 == 12581 and 10838 + 1737 == 12575
    tr = res.evaluation.trace
    assert [(t.pool_id, t.amount_in, t.amount_out) for t in tr] == [
        ("P_AC", 8000, 14773),
        ("P_CB", 14773, 10288),
        ("P_AD", 2000, 2932),
        ("P_DB", 2932, 2293),
    ]
    assert s["strategy"]["recipe"]["arm"] == "H3" and s["strategy"]["controls"] == []


def test_uni_sor_adaptive_can_miss_a_narrow_optimum() -> None:
    """Verify §8.5 step 8: the recipe's local fixed point loses 26.67 bps to the reference."""
    bundle = make_narrow_bundle()
    case = Case(case_id="ex_narrow", token_in="A", token_out="B", amount_in=10**8)
    search = {"max_hops": 2, "max_splits": 4, "percent_step": 5}
    prep = uni_sor_port.prepare(bundle, AlgorithmConfig("uni_sor_port", search))
    ref = uni_sor_port.solve(case, SolveContext(bundle, gross_only(), prep), Budget())
    assert ref.evaluation is not None and ref.evaluation.gross_output == 20_795_709
    assert ref.search_stats["quotes_executed"] == 80
    sel = [(r["pool_ids"], r["percent"]) for r in ref.search_stats["selection"]["routes"]]
    assert sel == [(["d1"], 90), (["d0"], 5), (["ax", "xb"], 5)]

    res = solve_strategy(uni_sor_strategies.ADAPTIVE, bundle, case, search)
    assert res.status == SolveStatus.OK
    assert res.evaluation is not None and res.evaluation.gross_output == 20_740_242
    assert res.search_stats["quotes_executed"] == 56
    sm = res.search_stats["sampling"]
    assert sm["stop_reason"] == "converged" and 90 not in sm["sampled_percents"]
    assert [r["selection"] for r in sm["rounds"]] == [
        ["V2:d1@50", "V2:d0@25", "V2:ax>xb@25"],
        ["V2:d1@75", "V2:ax>xb@20", "V2:d0@5"],
        ["V2:d1@80", "V2:d0@10", "V2:ax>xb@10"],
        ["V2:d1@80", "V2:d0@10", "V2:ax>xb@10"],
    ]
    loss = ref.evaluation.gross_output - res.evaluation.gross_output
    assert loss == 55_467
    assert round(loss * 10_000 / ref.evaluation.gross_output, 2) == 26.67


def test_uni_sor_optimized_teaching_graph_and_idle_controls(
    teaching_bundle: SnapshotBundle,
) -> None:
    """Verify §9.5 (a): vacuous shortlist, same rounds, different quote split, zero counters."""
    case = Case(case_id="ex_sor_optimized", token_in="TKA", token_out="TKB", amount_in=10_000)
    res = solve_strategy(uni_sor_strategies.OPTIMIZED, teaching_bundle, case, TEACHING_SOR5)
    assert res.status == SolveStatus.OK and res.algorithm == "uni_sor_optimized"
    assert res.evaluation is not None and res.evaluation.gross_output == 12581
    s = res.search_stats
    assert s["search_scope"] == "full_cohort"
    assert s["shortlist"]["routes_by_probe"] == {"5": 4, "100": 4}
    assert s["quotes_executed"] == 66 and s["quotes_memoized"] == 22
    assert s["shortlist"]["quotes"] == {
        "probe": 12, "shortlist_table": 54, "fallback_table": 0, "validation": 0
    }
    assert [r[2] for r in _rounds(res)] == [18, 24, 12]
    assert s["sampling"]["sampled_percents"] == [5, 15, 20, 25, 30, 50, 70, 75, 80, 85, 100]
    st = s["strategy"]
    assert st["recipe"]["arm"] == "H4" and st["controls"] == ["L02", "L03", "L04"]
    assert st["tick_math"]["misses"] == 0 and st["tick_math"]["hits"] == 0
    assert st["bin_math"]["misses"] == 0 and st["prefix"]["queries"] == 0


def test_uni_sor_optimized_wide_shortlist_and_small_probe() -> None:
    """Verify §9.5 (b): the shortlist cuts D1, the 5 % probe keeps the thin pool T."""
    bundle = make_wide_bundle()
    case = Case(case_id="ex_wide", token_in="A", token_out="B", amount_in=10**6)
    search = {"max_hops": 1, "max_splits": 2, "percent_step": 5}
    pools = bundle.pools
    t, d9, d1, d2 = pools["T"], pools["D9"], pools["D1"], pools["D2"]
    assert isinstance(t, ConstantProductPoolState) and isinstance(d9, ConstantProductPoolState)
    assert isinstance(d1, ConstantProductPoolState) and isinstance(d2, ConstantProductPoolState)
    assert cp_quote(t, "A", 50_000).amount_out == 85493
    assert cp_quote(t, "A", 10**6).amount_out == 461218
    assert cp_quote(d9, "A", 50_000).amount_out == 49575
    assert cp_quote(d1, "A", 10**6).amount_out == 499248
    assert cp_quote(d2, "A", 10**6).amount_out == 665331

    prep = uni_sor_port.prepare(bundle, AlgorithmConfig("uni_sor_port", search))
    ref = uni_sor_port.solve(case, SolveContext(bundle, gross_only(), prep), Budget())
    assert ref.evaluation is not None and ref.evaluation.gross_output == 974_119
    assert ref.search_stats["quotes_executed"] == 200

    ada = solve_strategy(uni_sor_strategies.ADAPTIVE, bundle, case, search)
    assert ada.evaluation is not None and ada.evaluation.gross_output == 974_119
    assert ada.search_stats["quotes_executed"] == 130
    assert ada.search_stats["shortlist"]["quotes"]["probe"] == 40

    res = solve_strategy(uni_sor_strategies.OPTIMIZED, bundle, case, search)
    assert res.status == SolveStatus.OK
    assert res.evaluation is not None and res.evaluation.gross_output == 974_119
    s = res.search_stats
    assert s["search_scope"] == "shortlist"
    assert s["shortlist"]["searched_route_ids"] == [f"V2:D{k}" for k in range(2, 10)] + ["V2:T"]
    assert s["shortlist"]["skipped_routes"] == {"V3": 0, "V2": 1, "MIXED": 0}
    assert res.candidates_truncated == 1 and res.candidates_considered == 10
    assert s["quotes_executed"] == 119
    assert s["shortlist"]["quotes"]["probe"] == 20
    assert s["shortlist"]["quotes"]["shortlist_table"] == 9 * 11
    assert s["sampling"]["seed"] == {"route_id": "V2:D9", "outcome": "improved"}
    assert [r["selection"] for r in s["sampling"]["rounds"]] == [
        ["V2:D9@75", "V2:T@25"],
        ["V2:D9@80", "V2:T@20"],
        ["V2:D9@85", "V2:T@15"],
        ["V2:D9@85", "V2:T@15"],
    ]
    assert 690_390 + 272_280 == 962_670 and 774_520 + 199_599 == 974_119
    assert [(x.pool_id, x.amount_in, x.amount_out) for x in res.evaluation.trace] == [
        ("D9", 850_000, 774_520),
        ("T", 150_000, 199_599),
    ]

    # Counterfactual (not the recipe): probing only at 100 % drops T and loses output.
    params: dict[str, object] = {
        **search,
        "probe_percents": [100],
        "routes_per_probe": 8,
        "direct_routes": 0,
        "coarse_step": 25,
        "refine_radius": 1,
        "soft_max_quotes": None,
    }
    prep_f = uni_sor_fast.prepare(bundle, AlgorithmConfig("uni_sor_fast", params))
    only100 = uni_sor_fast.solve(case, SolveContext(bundle, gross_only(), prep_f), Budget())
    assert only100.evaluation is not None and only100.evaluation.gross_output == 941_683
    assert only100.search_stats["quotes_executed"] == 74
    assert "V2:T" not in only100.search_stats["shortlist"]["searched_route_ids"]
    loss = 974_119 - 941_683
    assert loss == 32_436 and round(loss * 10_000 / 974_119, 2) == 332.98


# ------------------------------------------------------------ metis_inspired (§10, §11.3)


def _metis_prepared(
    bundle: SnapshotBundle, **params: object
) -> metis_inspired.PreparedMetisInspired:
    config: dict[str, object] = {"max_hops": 3, "max_splits": 4, "percent_step": 5, "chunks": 1}
    config.update(params)
    return metis_inspired.prepare(bundle, AlgorithmConfig("metis_inspired", config))


def _metis_solve(bundle: SnapshotBundle, case: Case, **params: object) -> SolveResult:
    prepared = _metis_prepared(bundle, **params)
    return metis_inspired.solve(case, SolveContext(bundle, gross_only(), prepared), Budget())


def _cp5(pid: str, t0: str, t1: str, r0: int, r1: int, fee: int = 5) -> ConstantProductPoolState:
    return ConstantProductPoolState(pid, t0, t1, r0, r1, fee_bps=fee)


def make_parallel_bundle() -> SnapshotBundle:
    """Fixture X1 of tests/routing/test_metis_inspired.py: 3 parallel pools per hop."""
    pools = [
        *(_cp5(f"sb{i}", "S", "B", 10**12 + i * 10**10, 10**12, (5, 30, 100)[i]) for i in range(3)),
        *(_cp5(f"bc{i}", "B", "C", 10**12, 10**12 - i * 10**10, (30, 5, 100)[i]) for i in range(3)),
        *(
            _cp5(f"cd{i}", "C", "D", 10**12 + i * 3 * 10**9, 10**12, (100, 30, 5)[i])
            for i in range(3)
        ),
        _cp5("sd", "S", "D", 10**9, 10**9, 30),
    ]
    return _synthetic_bundle("x1_parallel", *pools)


def make_four_hop_bundle() -> SnapshotBundle:
    """Fixture X3: deep liquidity only along S-B-C-E-D."""
    deep = 10**15
    return _synthetic_bundle(
        "x3_four_hop",
        _cp5("sb", "S", "B", deep, deep),
        _cp5("bc", "B", "C", deep, deep),
        _cp5("ce", "C", "E", deep, deep),
        _cp5("ed", "E", "D", deep, deep),
        _cp5("sd", "S", "D", 10**10, 10**10, 30),
    )


def make_revisit_bundle() -> SnapshotBundle:
    """Fixture X4: the best 2-prefix to X visits Y; the best 4-hop path needs X -> Y."""
    deep = 10**15
    return _synthetic_bundle(
        "x4_revisit",
        _cp5("sy", "S", "Y", deep, deep),
        _cp5("sa", "S", "A", deep, deep),
        _cp5("ax", "A", "X", deep, 10 * deep),
        _cp5("yx", "Y", "X", deep, 20 * deep),
        _cp5("xy", "X", "Y", 5 * deep, deep),
        _cp5("yd", "Y", "D", deep, deep),
    )


def _cp_chain(bundle: SnapshotBundle, token: str, amount: int, *pool_ids: str) -> int:
    for pid in pool_ids:
        pool = bundle.pools[pid]
        assert isinstance(pool, ConstantProductPoolState)
        amount = cp_quote(pool, token, amount).amount_out
        token = pool.other_token(token)
    return amount


def test_metis_inspired_label_layers_on_the_teaching_graph(
    teaching_bundle: SnapshotBundle,
) -> None:
    """Verify §10.5 steps 1-5: chunk-1 layers, distance prunes, choice and final plan."""
    b = teaching_bundle
    case = Case(case_id="ex_metis", token_in="TKA", token_out="TKB", amount_in=10_000)
    # Hand CPMM values of every chunk-1 relaxation (1000 TKA)
    assert [_cp_chain(b, "TKA", 1000, p) for p in ("P_AB1", "P_AB2", "P_AC", "P_AD")] == [
        987, 892, 1974, 1480
    ]  # fmt: skip
    assert _cp_chain(b, "TKA", 1000, "P_AC", "P_CB") == 1461
    assert _cp_chain(b, "TKA", 1000, "P_AC", "P_CD") == 1930
    assert _cp_chain(b, "TKA", 1000, "P_AD", "P_DB") == 1168
    assert _cp_chain(b, "TKA", 1000, "P_AD", "P_CD") == 1454
    assert _cp_chain(b, "TKA", 1000, "P_AC", "P_CD", "P_DB") == 1519
    assert _cp_chain(b, "TKA", 1000, "P_AD", "P_CD", "P_CB") == 1079
    assert 9_970_000 * 200_000 // (10**9 + 9_970_000) == 1974
    assert 19_680_780 * 100_000 // (10**9 + 19_680_780) == 1930
    assert 19_242_100 * 120_000 // (15 * 10**8 + 19_242_100) == 1519

    params = {"max_splits": 2, "percent_step": 10, "chunks": 10, "label_hops": 3,
              "label_pruning": True}  # fmt: skip
    prepared = _metis_prepared(b, **params)
    dist = metis_inspired.hops_to_target(prepared.index, "TKA", "TKB")
    assert dist == {"TKB": 0, "TKA": 1, "TKC": 1, "TKD": 1}

    # Chunk 1 through the solver's own chooser (a fresh allocator: nothing committed)
    alloc = metis_inspired._Allocator(b, case, prepared.index, QuoteCache(b))
    alloc.relaxed = []
    choice = alloc.choose_labels(1000, 3, dist, Budget())
    assert choice is not None and choice[0] == 1519
    assert path_label(choice[1]) == "TKA -[P_AC]-> TKC -[P_CD]-> TKD -[P_DB]-> TKB"
    assert alloc.layers is not None
    layers = [{t: lab.amount for t, lab in layer.items()} for layer in alloc.layers]
    assert layers == [{"TKA": 1000}, {"TKC": 1974, "TKD": 1480}, {"TKD": 1930, "TKC": 1454}, {}]
    assert (alloc.relaxations, alloc.label_distance) == (10, 2)
    assert (alloc.label_revisit, alloc.label_cycle) == (0, 0)

    diag = metis_inspired.diagnose_case(case, b, prepared)
    assert diag["classes"] == {"agree": 10} and diag["quote_subset_violations"] == 0
    first = diag["chunk_records"][0]
    assert (first["label_relaxations"], first["enumeration_paths_scored"]) == (10, 6)
    assert first["label_quotes_executed"] == first["enumeration_quotes_executed"] == 10
    assert diag["chunk_records"][1]["label"]["marginal"] == "1433"

    res = _metis_solve(b, case, **params)
    ig_cfg = {"max_hops": 3, "max_splits": 2, "percent_step": 10, "chunks": 10}
    prep_ig = incremental_graph.prepare(b, AlgorithmConfig("incremental_graph", ig_cfg))
    ref = incremental_graph.solve(case, SolveContext(b, gross_only(), prep_ig), Budget())
    assert res.status == SolveStatus.OK and res.plan == ref.plan
    assert res.evaluation is not None and res.evaluation.gross_output == 12892
    s = res.search_stats
    assert s["incremental_chunk_sequence"] == [0, 1, 1, 0, 1, 1, 1, 0, 1, 1]
    assert s["chosen_source"] == "metis_inspired" and s["topology"] == "shared_pool"
    assert (s["label_relaxations"], s["label_rejected_cycle"], s["label_pruned_distance"]) == (
        82, 9, 11
    )  # fmt: skip
    assert ref.search_stats["paths_scored"] == 51
    assert s["quotes_executed"] == ref.search_stats["quotes_executed"] == 128


def test_metis_inspired_parallel_gain_and_revisit_loss() -> None:
    """Verify §10.5 steps 6-8: X1 work, X3 four-hop gain, X4 token-revisit loss."""
    # Step 6 (X1): 3k+1 relaxations vs k^3+1 paths per chunk, same plan
    par = make_parallel_bundle()
    case1 = Case(case_id="x1", token_in="S", token_out="D", amount_in=10**10)
    m3 = _metis_solve(par, case1, chunks=10, label_hops=3, label_pruning=True)
    ig_cfg = {"max_hops": 3, "max_splits": 4, "percent_step": 5, "chunks": 10}
    prep_ig = incremental_graph.prepare(par, AlgorithmConfig("incremental_graph", ig_cfg))
    a0 = incremental_graph.solve(case1, SolveContext(par, gross_only(), prep_ig), Budget())
    assert m3.plan == a0.plan
    assert m3.evaluation is not None and m3.evaluation.gross_output == 9_692_524_563
    assert (m3.search_stats["label_relaxations"], m3.search_stats["label_pruned_distance"]) == (
        100, 30
    )  # fmt: skip
    assert a0.search_stats["paths_scored"] == 280
    assert (m3.search_stats["quotes_executed"], a0.search_stats["quotes_executed"]) == (848, 1011)

    # Step 7 (X3): a four-hop-only route
    four = make_four_hop_bundle()
    case3 = Case(case_id="x3", token_in="S", token_out="D", amount_in=10**10)
    prep_ig3 = incremental_graph.prepare(
        four, AlgorithmConfig("incremental_graph", {**ig_cfg, "chunks": 1})
    )
    a0_3 = incremental_graph.solve(case3, SolveContext(four, gross_only(), prep_ig3), Budget())
    m4 = _metis_solve(four, case3, label_hops=4, label_pruning=True)
    assert a0_3.score == 4_992_488_733
    assert m4.evaluation is not None and m4.score == 9_979_616_307
    assert [(t.pool_id, t.amount_in, t.amount_out) for t in m4.evaluation.trace] == [
        ("sb", 10**10, 9_994_900_100),
        ("bc", 9_994_900_100, 9_989_802_852),
        ("ce", 9_989_802_852, 9_984_708_255),
        ("ed", 9_984_708_255, 9_979_616_307),
    ]
    assert m4.search_stats["path_split_score"] == "4992488733"  # embedded at max_hops 3

    # Step 8 (X4): token-revisit pruning loses the best four-hop path
    rev = make_revisit_bundle()
    case4 = Case(case_id="x4", token_in="S", token_out="D", amount_in=10**9)
    assert _cp_chain(rev, "S", 10**9, "sy", "yx") == 19_979_965_070
    assert _cp_chain(rev, "S", 10**9, "sa", "ax") == 9_989_982_535
    m4r = _metis_solve(rev, case4, label_hops=4, label_pruning=True)
    off = _metis_solve(rev, case4, label_hops=4, label_pruning=False)
    assert m4r.score == 998_998_253 == _cp_chain(rev, "S", 10**9, "sy", "yd")
    assert off.score == 1_995_991_039
    assert m4r.search_stats["label_skipped_revisit"] == 2
    loss = 1_995_991_039 - 998_998_253
    assert round(loss * 10_000 / 1_995_991_039, 2) == 4994.98


def test_real_state_corpus_fixture_exact_evaluation() -> None:
    """Verify all 9 strategies on real-state fixed-block fixture without mutating bundle."""
    bundle_dir = ROOT / "tests" / "fixtures" / "corpus" / "bundle"
    manifest_before = (bundle_dir / "manifest.json").read_bytes()
    pools_before = (bundle_dir / "pools.json").read_bytes()

    bundle = load_bundle(bundle_dir)
    profile_path = ROOT / "config" / "daily_gross.yaml"
    profile = load_profile(profile_path)
    real_budget = Budget(
        max_quotes=profile.budget.max_quotes,
        time_limit_seconds=profile.budget.time_limit_seconds,
        max_candidates=profile.budget.max_candidates,
    )
    case = Case(
        case_id="real_usdc_usdt0_10k",
        token_in="0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9",  # USDC
        token_out="0x779ded0c9e1022225f8e0630b35a9b54be713736",  # USDT0
        amount_in=10_000_000_000,
    )
    obj = gross_only()

    # 1. direct
    r_dir = direct.solve(case, SolveContext(bundle=bundle, objective=obj), real_budget)
    assert r_dir.status == SolveStatus.OK and r_dir.evaluation is not None
    assert r_dir.evaluation.gross_output == 10000660449
    assert r_dir.candidates_considered == 4

    # 2. single_path
    prep_sp = single_path.prepare(
        bundle, AlgorithmConfig("single_path", profile.search)
    )
    ctx_sp = SolveContext(bundle=bundle, objective=obj, prepared=prep_sp)
    r_sp = single_path.solve(case, ctx_sp, real_budget)
    assert r_sp.status == SolveStatus.OK and r_sp.evaluation is not None
    assert r_sp.evaluation.gross_output == 10000660449
    assert r_sp.search_stats["quotes_executed"] == 4

    # 3. direct_split
    prep_ds = direct_split.prepare(
        bundle, AlgorithmConfig("direct_split", profile.search)
    )
    ctx_ds = SolveContext(bundle=bundle, objective=obj, prepared=prep_ds)
    r_ds = direct_split.solve(case, ctx_ds, real_budget)
    assert r_ds.status == SolveStatus.OK and r_ds.evaluation is not None
    assert r_ds.evaluation.gross_output == 10000660449
    assert r_ds.search_stats["quotes_executed"] == 80

    # 4. path_split
    prep_ps = path_split.prepare(
        bundle, AlgorithmConfig("path_split", profile.search)
    )
    ctx_ps = SolveContext(bundle=bundle, objective=obj, prepared=prep_ps)
    r_ps = path_split.solve(case, ctx_ps, real_budget)
    assert r_ps.status == SolveStatus.OK and r_ps.evaluation is not None
    assert r_ps.evaluation.gross_output == 10000660449
    assert r_ps.search_stats["quotes_executed"] == 80

    # 5. incremental_graph
    ig_dict = dict(profile.search)
    ig_dict.update(profile.graph)
    prep_real_ig = incremental_graph.prepare(
        bundle, AlgorithmConfig("incremental_graph", ig_dict)
    )
    ctx_rig = SolveContext(bundle=bundle, objective=obj, prepared=prep_real_ig)
    r_ig = incremental_graph.solve(case, ctx_rig, real_budget)
    assert r_ig.status == SolveStatus.OK and r_ig.evaluation is not None
    assert r_ig.evaluation.gross_output == 10000663447
    assert r_ig.search_stats["quotes_executed"] == 264
    assert r_ig.plan is not None
    assert len(r_ig.plan.steps) == 2
    assert r_ig.plan.steps[0].inputs[0].amount == 9950000000
    assert r_ig.plan.steps[1].inputs[0].amount == "ALL_REMAINING"
    assert r_ig.evaluation.trace[0].amount_in == 9950000000
    assert r_ig.evaluation.trace[0].amount_out == 9950659893
    assert r_ig.evaluation.trace[1].amount_in == 50000000
    assert r_ig.evaluation.trace[1].amount_out == 50003554

    # 6. uni_sor_port
    prep_sor = uni_sor_port.prepare(
        bundle, AlgorithmConfig("uni_sor_port", profile.search)
    )
    ctx_rsor = SolveContext(bundle=bundle, objective=obj, prepared=prep_sor)
    r_sor = uni_sor_port.solve(case, ctx_rsor, real_budget)
    assert r_sor.status == SolveStatus.OK and r_sor.evaluation is not None
    assert r_sor.evaluation.gross_output == 10000660449
    assert r_sor.search_stats["quotes_executed"] == 40

    # 7-8. The two optimized strategies (§10.3 items 3-4)
    for name, probe, table in (
        (uni_sor_strategies.ADAPTIVE, 8, 4),
        (uni_sor_strategies.OPTIMIZED, 4, 8),
    ):
        r_opt = solve_strategy(name, bundle, case, dict(profile.search), real_budget)
        assert r_opt.status == SolveStatus.OK and r_opt.evaluation is not None
        assert r_opt.evaluation.gross_output == 10000660449
        assert r_opt.plan == r_sor.plan
        so = r_opt.search_stats
        assert so["quotes_executed"] == 12
        quotes = so["shortlist"]["quotes"]
        assert (quotes["probe"], quotes["shortlist_table"]) == (probe, table)
        sm = so["sampling"]
        assert sm["sampled_percents"] == [5, 25, 50, 75, 95, 100]
        assert sm["stop_reason"] == "converged" and sm["validations"] == 1
        assert [r["incumbent"] for r in sm["rounds"]] == ["unchanged", "unchanged"]
        assert sm["incumbent"]["source"] == "full_input_seed"
    # Exact controls on the real Agni V3 pool (uni_sor_optimized, the last loop iteration)
    assert r_opt.algorithm == uni_sor_strategies.OPTIMIZED
    st = so["strategy"]
    assert (st["tick_math"]["misses"], st["tick_math"]["hits"]) == (1, 5)
    assert (st["prefix"]["queries"], st["prefix"]["keys"], st["prefix"]["resumed"]) == (6, 1, 0)
    assert st["prefix"]["reused_steps"] == 0

    # 9. metis_inspired under --strategies all: daily_gross + the pinned M4 label settings
    metis_params = {**profile.search, **profile.graph}
    for key, value in metis_graph_settings().items():
        metis_params.setdefault(key, value)
    assert metis_params["chunks"] == 200 and metis_params["label_hops"] == 4
    prep_mi = metis_inspired.prepare(bundle, AlgorithmConfig("metis_inspired", metis_params))
    r_mi = metis_inspired.solve(case, SolveContext(bundle, obj, prep_mi), real_budget)
    assert r_mi.status == SolveStatus.OK and r_mi.evaluation is not None
    assert r_mi.plan == r_ig.plan and r_mi.evaluation.gross_output == 10000663447
    sm = r_mi.search_stats
    assert sm["quotes_executed"] == 264 and sm["label_relaxations"] == 800
    assert sm["label_pruned_distance"] == sm["label_skipped_revisit"] == 0
    assert sm["label_rejected_cycle"] == 0
    assert sm["marginal_failures"] == {"insufficient_liquidity": 200}
    assert sm["chunk_path_hops"] == {"1": 200}
    assert [a["chunks"] for a in sm["incremental_allocation"]] == [199, 1]
    assert sm["path_split_score"] == "10000660449"

    # Assert byte identity of original files
    assert (bundle_dir / "manifest.json").read_bytes() == manifest_before
    assert (bundle_dir / "pools.json").read_bytes() == pools_before


def test_run_examples_script_executes_successfully() -> None:
    """Verify that docs/examples/routing-algorithms/run_examples.py runs cleanly."""
    script_path = ROOT / "docs" / "examples" / "routing-algorithms" / "run_examples.py"
    spec = importlib.util.spec_from_file_location("run_examples", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.run_all()

"""Tests verifying all calculations, transitions, and reproducible examples
published in docs/references/routing-algorithms.md.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from benchmark.objective import gross_only
from pools.constant_product import quote_exact_in as cp_quote
from routing.algorithms import (
    direct,
    direct_split,
    incremental_graph,
    path_split,
    single_path,
    uni_sor_port,
)
from routing.algorithms.base import AlgorithmConfig, Budget, SolveContext, SolveStatus
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

    # 2. Nondivisible input: 10,005 TKA with percent_step=10 (N=10)
    # Leg 1: floor(10005 * 7 / 10) = 7003
    # Leg 2: 10005 - 7003 = 3002 (ALL_REMAINING, carrying remainder 1)
    case_nondiv = Case(case_id="ex_ds_nondiv", token_in="TKA", token_out="TKB", amount_in=10_005)
    res_nondiv = direct_split.solve(case_nondiv, ctx, Budget())
    alloc_nondiv = res_nondiv.search_stats.get("best_allocation")
    assert alloc_nondiv == [
        {"pool_id": "P_AB1", "percent": 70, "amount_in": "7003"},
        {"pool_id": "P_AB2", "percent": 30, "amount_in": "3002"},
    ]
    assert 7003 + 3002 == 10_005

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


def test_uni_sor_port_selection_and_parity_behavior(
    teaching_bundle: SnapshotBundle,
) -> None:
    """Verify uni_sor_port selects best routes and obeys port contract."""
    cfg = {"max_hops": 2, "max_splits": 2, "percent_step": 10}
    prep = uni_sor_port.prepare(teaching_bundle, AlgorithmConfig("uni_sor_port", cfg))
    case = Case(case_id="ex_sor", token_in="TKA", token_out="TKB", amount_in=10_000)
    ctx = SolveContext(bundle=teaching_bundle, objective=gross_only(), prepared=prep)

    res = uni_sor_port.solve(case, ctx, Budget())
    assert res.status == SolveStatus.OK
    assert res.evaluation is not None and res.evaluation.gross_output == 12581
    assert res.search_stats.get("requote_delta") == "0"


def test_real_state_corpus_fixture_exact_evaluation() -> None:
    """Verify all 6 algorithms on real-state fixed-block fixture without mutating bundle."""
    bundle_dir = ROOT / "tests" / "fixtures" / "corpus" / "bundle"
    manifest_before = (bundle_dir / "manifest.json").read_bytes()
    pools_before = (bundle_dir / "pools.json").read_bytes()

    bundle = load_bundle(bundle_dir)
    case = Case(
        case_id="real_usdc_usdt0_10k",
        token_in="0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9",  # USDC
        token_out="0x779ded0c9e1022225f8e0630b35a9b54be713736",  # USDT0
        amount_in=10_000_000_000,
    )
    obj = gross_only()
    budget = Budget()

    # 1. direct
    r_dir = direct.solve(case, SolveContext(bundle=bundle, objective=obj), budget)
    assert r_dir.status == SolveStatus.OK and r_dir.evaluation is not None
    assert r_dir.evaluation.gross_output == 10000660449

    # 2. single_path
    prep_sp = single_path.prepare(bundle, AlgorithmConfig("single_path", {"max_hops": 3}))
    ctx_sp = SolveContext(bundle=bundle, objective=obj, prepared=prep_sp)
    r_sp = single_path.solve(case, ctx_sp, budget)
    assert r_sp.status == SolveStatus.OK and r_sp.evaluation is not None
    assert r_sp.evaluation.gross_output == 10000660449

    # 3. direct_split
    ds_cfg = {"max_splits": 4, "percent_step": 5}
    prep_ds = direct_split.prepare(bundle, AlgorithmConfig("direct_split", ds_cfg))
    ctx_ds = SolveContext(bundle=bundle, objective=obj, prepared=prep_ds)
    r_ds = direct_split.solve(case, ctx_ds, budget)
    assert r_ds.status == SolveStatus.OK and r_ds.evaluation is not None
    assert r_ds.evaluation.gross_output == 10000660449

    # 4. path_split
    ps_cfg = {"max_hops": 3, "max_splits": 4, "percent_step": 5}
    prep_ps = path_split.prepare(bundle, AlgorithmConfig("path_split", ps_cfg))
    ctx_ps = SolveContext(bundle=bundle, objective=obj, prepared=prep_ps)
    r_ps = path_split.solve(case, ctx_ps, budget)
    assert r_ps.status == SolveStatus.OK and r_ps.evaluation is not None
    assert r_ps.evaluation.gross_output == 10000660449

    # 5. incremental_graph
    ig_cfg = {"max_hops": 3, "max_splits": 4, "percent_step": 5, "chunks": 200}
    prep_ig = incremental_graph.prepare(bundle, AlgorithmConfig("incremental_graph", ig_cfg))
    ctx_ig = SolveContext(bundle=bundle, objective=obj, prepared=prep_ig)
    r_ig = incremental_graph.solve(case, ctx_ig, budget)
    assert r_ig.status == SolveStatus.OK and r_ig.evaluation is not None
    assert r_ig.evaluation.gross_output == 10000663447
    assert r_ig.plan is not None
    assert len(r_ig.plan.steps) == 2
    assert r_ig.plan.steps[0].inputs[0].amount == 9950000000
    assert r_ig.plan.steps[1].inputs[0].amount == "ALL_REMAINING"
    assert r_ig.evaluation.trace[1].amount_in == 50000000

    # 6. uni_sor_port
    sor_cfg = {"max_hops": 2, "max_splits": 4, "percent_step": 5}
    prep_sor = uni_sor_port.prepare(bundle, AlgorithmConfig("uni_sor_port", sor_cfg))
    ctx_sor = SolveContext(bundle=bundle, objective=obj, prepared=prep_sor)
    r_sor = uni_sor_port.solve(case, ctx_sor, budget)
    assert r_sor.status == SolveStatus.OK and r_sor.evaluation is not None
    assert r_sor.evaluation.gross_output == 10000660449

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

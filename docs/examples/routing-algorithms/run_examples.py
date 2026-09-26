"""Reproducible worked examples runner for the six routing algorithms.

Verifies and outputs step-by-step traces for:
1. direct
2. single_path
3. direct_split
4. path_split
5. incremental_graph
6. uni_sor_port
along with the real-state fixed-block snapshot case.

Run with:
    uv run python docs/examples/routing-algorithms/run_examples.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark.objective import gross_only  # noqa: E402
from benchmark.profile import load_profile  # noqa: E402
from pools.constant_product import quote_exact_in as cp_quote  # noqa: E402
from routing.algorithms import (  # noqa: E402
    direct,
    direct_split,
    incremental_graph,
    path_split,
    single_path,
    uni_sor_port,
)
from routing.algorithms.base import (  # noqa: E402
    AlgorithmConfig,
    Budget,
    SolveContext,
    SolveStatus,
)
from routing.search import build_graph_index, enumerate_paths, path_label  # noqa: E402
from snapshot.bundle import load_bundle  # noqa: E402
from snapshot.models import (  # noqa: E402
    BlockRef,
    Case,
    ConstantProductPoolState,
    SnapshotBundle,
)


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


def run_all() -> None:
    bundle = make_teaching_bundle()
    obj = gross_only()
    budget = Budget()

    print("=================================================================")
    print("Mantle Router Algorithm Optimizer - Reproducible Worked Examples")
    print("=================================================================\n")

    # -------------------------------------------------------------
    # 1. CPMM Formula derivation check
    # -------------------------------------------------------------
    print("--- 1. Constant-Product Formula Check ---")
    p1 = bundle.pools["P_AB1"]
    assert isinstance(p1, ConstantProductPoolState)
    dx = 10_000
    fee_bps = 30
    in_with_fee = dx * (10_000 - fee_bps)  # 99,700,000
    num = in_with_fee * p1.reserve1  # 99,700,000 * 100,000
    den = p1.reserve0 * 10_000 + in_with_fee  # 100,000 * 10,000 + 99,700,000
    expected_dy = num // den  # 9066
    q1 = cp_quote(p1, "TKA", dx)
    assert q1.amount_out == expected_dy == 9066, f"Expected 9066, got {q1.amount_out}"
    print(f"Pool P_AB1 in {dx} TKA -> calc {expected_dy} TKB, quote {q1.amount_out} [OK]\n")

    # -------------------------------------------------------------
    # 2. direct
    # -------------------------------------------------------------
    print("--- 2. Algorithm: direct ---")
    case = Case(case_id="ex_direct", token_in="TKA", token_out="TKB", amount_in=10_000)
    res_direct = direct.solve(case, SolveContext(bundle=bundle, objective=obj), budget)
    assert res_direct.status == SolveStatus.OK
    assert res_direct.plan is not None and res_direct.evaluation is not None
    assert res_direct.evaluation.gross_output == 9066
    assert res_direct.plan.steps[0].pool_id == "P_AB1"
    assert res_direct.evaluation.trace[0].amount_in == 10000
    assert res_direct.evaluation.trace[0].amount_out == 9066
    print("Direct pool P_AB1: 9066 TKB, P_AB2: 8546 TKB -> Winner: P_AB1 (9066 TKB) [OK]")

    # Check truncation with max_candidates=1
    trunc_budget = Budget(max_candidates=1)
    res_trunc = direct.solve(case, SolveContext(bundle=bundle, objective=obj), trunc_budget)
    assert res_trunc.candidates_considered == 1
    assert res_trunc.candidates_truncated == 1
    print("Direct candidate truncation: considered 1, truncated 1 [OK]\n")

    # -------------------------------------------------------------
    # 3. single_path
    # -------------------------------------------------------------
    print("--- 3. Algorithm: single_path ---")
    prep_sp = single_path.prepare(bundle, AlgorithmConfig("single_path", {"max_hops": 3}))
    ctx_sp = SolveContext(bundle=bundle, objective=obj, prepared=prep_sp)
    res_sp = single_path.solve(case, ctx_sp, budget)
    assert res_sp.status == SolveStatus.OK
    assert res_sp.plan is not None and res_sp.evaluation is not None
    assert res_sp.evaluation.gross_output == 12434
    p_step_ids = [s.pool_id for s in res_sp.plan.steps]
    assert p_step_ids == ["P_AC", "P_CB"], f"Unexpected steps: {p_step_ids}"
    assert res_sp.evaluation.trace[0].amount_in == 10000
    assert res_sp.evaluation.trace[0].amount_out == 18132
    assert res_sp.evaluation.trace[1].amount_in == 18132
    assert res_sp.evaluation.trace[1].amount_out == 12434
    assert res_sp.search_stats["quotes_executed"] == 10
    assert res_sp.search_stats["quotes_memoized"] == 2
    print("Enumerated paths up to 3 hops:")
    idx = build_graph_index(bundle)
    for p in enumerate_paths(idx, "TKA", "TKB", 3):
        print(f"  {path_label(p)}")
    print("Hop-major search evaluated 1-hop first (9066), then 2-hop (12434).")
    print("Chosen multi-hop path: TKA -[P_AC]-> TKC -[P_CB]-> TKB with 12434 TKB [OK]\n")

    # -------------------------------------------------------------
    # 4. direct_split
    # -------------------------------------------------------------
    print("--- 4. Algorithm: direct_split ---")
    ds_cfg = {"max_splits": 2, "percent_step": 10}
    prep_ds = direct_split.prepare(bundle, AlgorithmConfig("direct_split", ds_cfg))
    ctx_ds = SolveContext(bundle=bundle, objective=obj, prepared=prep_ds)
    res_ds = direct_split.solve(case, ctx_ds, budget)
    assert res_ds.status == SolveStatus.OK
    assert res_ds.plan is not None and res_ds.evaluation is not None
    assert res_ds.evaluation.gross_output == 9175
    alloc = res_ds.search_stats.get("best_allocation")
    assert alloc == [
        {"pool_id": "P_AB1", "percent": 70, "amount_in": "7000"},
        {"pool_id": "P_AB2", "percent": 30, "amount_in": "3000"},
    ]
    assert res_ds.evaluation.trace[0].amount_in == 7000
    assert res_ds.evaluation.trace[0].amount_out == 6523
    assert res_ds.evaluation.trace[1].amount_in == 3000
    assert res_ds.evaluation.trace[1].amount_out == 2652
    print("DP optimal split: 70% P_AB1 (6523) + 30% P_AB2 (2652) = 9175 TKB [OK]")

    # Check nondivisible input
    case_nondiv = Case(case_id="ex_nondiv", token_in="TKA", token_out="TKB", amount_in=10_005)
    ctx_nondiv = SolveContext(bundle=bundle, objective=obj, prepared=prep_ds)
    res_nondiv = direct_split.solve(case_nondiv, ctx_nondiv, budget)
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
    print("Nondivisible 10005 TKA: leg 1 = 7003 (6526), leg 2 = 3002 (2653) -> sum 9179 [OK]\n")

    # -------------------------------------------------------------
    # 5. path_split
    # -------------------------------------------------------------
    print("--- 5. Algorithm: path_split ---")
    ps_cfg = {"max_hops": 3, "max_splits": 2, "percent_step": 10}
    prep_ps = path_split.prepare(bundle, AlgorithmConfig("path_split", ps_cfg))
    ctx_ps = SolveContext(bundle=bundle, objective=obj, prepared=prep_ps)
    res_ps = path_split.solve(case, ctx_ps, budget)
    assert res_ps.status == SolveStatus.OK
    assert res_ps.plan is not None and res_ps.evaluation is not None
    assert res_ps.evaluation.gross_output == 12581
    ps_steps = [(s.pool_id, s.token_in, s.token_out) for s in res_ps.plan.steps]
    assert ps_steps == [
        ("P_AC", "TKA", "TKC"),
        ("P_CB", "TKC", "TKB"),
        ("P_AD", "TKA", "TKD"),
        ("P_DB", "TKD", "TKB"),
    ]
    # Check exact evaluated intermediate amounts:
    assert res_ps.evaluation.trace[0].amount_in == 8000
    assert res_ps.evaluation.trace[0].amount_out == 14773
    assert res_ps.evaluation.trace[1].amount_in == 14773
    assert res_ps.evaluation.trace[1].amount_out == 10288
    assert res_ps.evaluation.trace[2].amount_in == 2000
    assert res_ps.evaluation.trace[2].amount_out == 2932
    assert res_ps.evaluation.trace[3].amount_in == 2932
    assert res_ps.evaluation.trace[3].amount_out == 2293
    assert 10288 + 2293 == 12581
    assert res_ps.search_stats["bnb_conflicts_excluded"] == 13
    print("Path split: 80% P_AC->P_CB (10288) + 20% P_AD->P_DB (2293) = 12581 TKB [OK]")
    print("Pool conflict verification: {P_AC, P_CB} and {P_AD, P_DB} are strictly disjoint [OK]\n")

    # -------------------------------------------------------------
    # 6. incremental_graph
    # -------------------------------------------------------------
    print("--- 6. Algorithm: incremental_graph ---")
    ig_cfg = {"max_hops": 3, "max_splits": 2, "percent_step": 10, "chunks": 10}
    prep_ig = incremental_graph.prepare(bundle, AlgorithmConfig("incremental_graph", ig_cfg))
    ctx_ig = SolveContext(bundle=bundle, objective=obj, prepared=prep_ig)
    res_ig = incremental_graph.solve(case, ctx_ig, budget)
    assert res_ig.status == SolveStatus.OK
    assert res_ig.plan is not None and res_ig.evaluation is not None
    assert res_ig.evaluation.gross_output == 12892
    assert res_ig.search_stats.get("topology") == "shared_pool"
    ig_steps = [(s.pool_id, s.token_in, s.token_out) for s in res_ig.plan.steps]
    assert ig_steps == [
        ("P_AC", "TKA", "TKC"),
        ("P_CD", "TKC", "TKD"),
        ("P_CB", "TKC", "TKB"),
        ("P_DB", "TKD", "TKB"),
    ]
    # Check exact evaluated intermediate amounts:
    assert res_ig.evaluation.trace[0].amount_in == 10000
    assert res_ig.evaluation.trace[0].amount_out == 18132
    assert res_ig.evaluation.trace[1].amount_in == 5562
    assert res_ig.evaluation.trace[1].amount_out == 5253
    assert res_ig.evaluation.trace[2].amount_in == 12570
    assert res_ig.evaluation.trace[2].amount_out == 8844
    assert res_ig.evaluation.trace[3].amount_in == 5253
    assert res_ig.evaluation.trace[3].amount_out == 4048
    assert 8844 + 4048 == 12892
    assert res_ig.search_stats["incremental_chunk_sequence"] == [0, 1, 1, 0, 1, 1, 1, 0, 1, 1]
    print("Shared intermediate pool P_AC receives full 10000 TKA -> 18132 TKC.")
    print("At TKC, flow branches: 5562 to P_CD -> P_DB (4048 TKB) + 12570 to P_CB (8844 TKB).")
    print("Total merged output: 12892 TKB (topology: shared_pool) [OK]\n")

    # -------------------------------------------------------------
    # 7. uni_sor_port
    # -------------------------------------------------------------
    print("--- 7. Algorithm: uni_sor_port ---")
    sor_cfg = {"max_hops": 2, "max_splits": 2, "percent_step": 10}
    prep_sor = uni_sor_port.prepare(bundle, AlgorithmConfig("uni_sor_port", sor_cfg))
    ctx_sor = SolveContext(bundle=bundle, objective=obj, prepared=prep_sor)
    res_sor = uni_sor_port.solve(case, ctx_sor, budget)
    assert res_sor.status == SolveStatus.OK
    assert res_sor.plan is not None and res_sor.evaluation is not None
    assert res_sor.evaluation.gross_output == 12581
    sor_steps = [(s.pool_id, s.token_in, s.token_out) for s in res_sor.plan.steps]
    assert sor_steps == [
        ("P_AC", "TKA", "TKC"),
        ("P_CB", "TKC", "TKB"),
        ("P_AD", "TKA", "TKD"),
        ("P_DB", "TKD", "TKB"),
    ]
    assert res_sor.evaluation.trace[0].amount_in == 8000
    assert res_sor.evaluation.trace[0].amount_out == 14773
    assert res_sor.evaluation.trace[1].amount_in == 14773
    assert res_sor.evaluation.trace[1].amount_out == 10288
    assert res_sor.evaluation.trace[2].amount_in == 2000
    assert res_sor.evaluation.trace[2].amount_out == 2932
    assert res_sor.evaluation.trace[3].amount_in == 2932
    assert res_sor.evaluation.trace[3].amount_out == 2293
    assert res_sor.search_stats.get("requote_delta") == "0"
    print("SOR selection: 80% P_AC->P_CB (10288) + 20% P_AD->P_DB (2293) = 12581 TKB [OK]\n")

    # -------------------------------------------------------------
    # 8. Fixed-Block Real State Case (Matching profile config/daily_gross.yaml)
    # -------------------------------------------------------------
    print("--- 8. Real-State Fixed-Block Corpus Case ---")
    corpus_bundle_dir = ROOT / "tests" / "fixtures" / "corpus" / "bundle"
    corpus_bundle = load_bundle(corpus_bundle_dir)
    profile_path = ROOT / "config" / "daily_gross.yaml"
    profile = load_profile(profile_path)
    real_budget = Budget(
        max_quotes=profile.budget.max_quotes,
        time_limit_seconds=profile.budget.time_limit_seconds,
        max_candidates=profile.budget.max_candidates,
    )
    real_case = Case(
        case_id="real_usdc_usdt0_10k",
        token_in="0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9",  # USDC
        token_out="0x779ded0c9e1022225f8e0630b35a9b54be713736",  # USDT0
        amount_in=10_000_000_000,  # 10,000 USDC (6 decimals)
    )

    # 1. direct
    r_dir = direct.solve(real_case, SolveContext(bundle=corpus_bundle, objective=obj), real_budget)
    assert r_dir.status == SolveStatus.OK and r_dir.evaluation is not None
    assert r_dir.evaluation.gross_output == 10000660449
    assert r_dir.candidates_considered == 4

    # 2. single_path
    prep_real_sp = single_path.prepare(
        corpus_bundle, AlgorithmConfig("single_path", profile.search)
    )
    ctx_rsp = SolveContext(bundle=corpus_bundle, objective=obj, prepared=prep_real_sp)
    r_sp = single_path.solve(real_case, ctx_rsp, real_budget)
    assert r_sp.status == SolveStatus.OK and r_sp.evaluation is not None
    assert r_sp.evaluation.gross_output == 10000660449
    assert r_sp.search_stats["quotes_executed"] == 4

    # 3. direct_split
    prep_real_ds = direct_split.prepare(
        corpus_bundle, AlgorithmConfig("direct_split", profile.search)
    )
    ctx_rds = SolveContext(bundle=corpus_bundle, objective=obj, prepared=prep_real_ds)
    r_ds = direct_split.solve(real_case, ctx_rds, real_budget)
    assert r_ds.status == SolveStatus.OK and r_ds.evaluation is not None
    assert r_ds.evaluation.gross_output == 10000660449
    assert r_ds.search_stats["quotes_executed"] == 80

    # 4. path_split
    prep_real_ps = path_split.prepare(corpus_bundle, AlgorithmConfig("path_split", profile.search))
    ctx_rps = SolveContext(bundle=corpus_bundle, objective=obj, prepared=prep_real_ps)
    r_ps = path_split.solve(real_case, ctx_rps, real_budget)
    assert r_ps.status == SolveStatus.OK and r_ps.evaluation is not None
    assert r_ps.evaluation.gross_output == 10000660449
    assert r_ps.search_stats["quotes_executed"] == 80

    # 5. incremental_graph
    ig_dict = dict(profile.search)
    ig_dict.update(profile.graph)
    prep_real_ig = incremental_graph.prepare(
        corpus_bundle, AlgorithmConfig("incremental_graph", ig_dict)
    )
    ctx_rig = SolveContext(bundle=corpus_bundle, objective=obj, prepared=prep_real_ig)
    r_ig = incremental_graph.solve(real_case, ctx_rig, real_budget)
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
    prep_real_sor = uni_sor_port.prepare(
        corpus_bundle, AlgorithmConfig("uni_sor_port", profile.search)
    )
    ctx_rsor = SolveContext(bundle=corpus_bundle, objective=obj, prepared=prep_real_sor)
    r_sor = uni_sor_port.solve(real_case, ctx_rsor, real_budget)
    assert r_sor.status == SolveStatus.OK and r_sor.evaluation is not None
    assert r_sor.evaluation.gross_output == 10000660449
    assert r_sor.search_stats["quotes_executed"] == 40

    print("Real-state block 101082044 request 10000 USDC -> USDT0 (Profile: daily_gross.yaml):")
    print("  direct:            10000660449 raw (10000.660449 USDT0), 4 quotes")
    print("  single_path:       10000660449 raw (10000.660449 USDT0), 4 quotes")
    print("  direct_split:      10000660449 raw (10000.660449 USDT0), 80 quotes")
    print("  path_split:        10000660449 raw (10000.660449 USDT0), 80 quotes")
    print("  incremental_graph: 10000663447 raw (10000.663447 USDT0) [Agni + Moe LB split]")
    print("  uni_sor_port:      10000660449 raw (10000.660449 USDT0) [LB excluded by D-4]")
    print("All real-state assertions passed [OK]\n")

    print("=================================================================")
    print("All 8 verification suites passed with 100% deterministic equality.")
    print("=================================================================")


if __name__ == "__main__":
    run_all()

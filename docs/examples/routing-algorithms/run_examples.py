"""Reproducible worked examples runner for the eight routing strategies.

Verifies and outputs step-by-step traces for the six base strategies:
1. direct
2. single_path
3. direct_split
4. path_split
5. incremental_graph
6. uni_sor_port
and the two named optimized strategies (recipes of uni_sor_fast):
7. uni_sor_adaptive (L08 arm H3)
8. uni_sor_optimized (L08 arm H4)
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
    uni_sor_fast,
    uni_sor_port,
    uni_sor_strategies,
)
from routing.algorithms.base import (  # noqa: E402
    AlgorithmConfig,
    Budget,
    SolveContext,
    SolveResult,
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
    """The narrow-optimum pools of tests/routing/test_uni_sor_fast.py (guide §8.5 step 8)."""

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
    print(
        f"Direct pool {res_direct.plan.steps[0].pool_id}: "
        f"{res_direct.evaluation.gross_output} TKB [OK]"
    )

    # Check truncation with max_candidates=1
    trunc_budget = Budget(max_candidates=1)
    res_trunc = direct.solve(case, SolveContext(bundle=bundle, objective=obj), trunc_budget)
    assert res_trunc.candidates_considered == 1
    assert res_trunc.candidates_truncated == 1
    print(
        f"Direct candidate truncation: considered {res_trunc.candidates_considered}, "
        f"truncated {res_trunc.candidates_truncated} [OK]\n"
    )

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
    print(
        f"Hop-major search: direct baseline 9066 -> chosen multi-hop {p_step_ids} "
        f"with {res_sp.evaluation.gross_output} TKB "
        f"({res_sp.search_stats['quotes_executed']} quotes, "
        f"{res_sp.search_stats['quotes_memoized']} memo hits) [OK]\n"
    )

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
    t0, t1 = res_ds.evaluation.trace[0], res_ds.evaluation.trace[1]
    assert t0.amount_in == 7000 and t0.amount_out == 6523
    assert t1.amount_in == 3000 and t1.amount_out == 2652
    print(
        f"DP optimal split: 70% P_AB1 ({t0.amount_out}) + 30% P_AB2 ({t1.amount_out}) = "
        f"{res_ds.evaluation.gross_output} TKB [OK]"
    )

    # Check nondivisible input
    case_nondiv = Case(case_id="ex_nondiv", token_in="TKA", token_out="TKB", amount_in=10_005)
    ctx_nondiv = SolveContext(bundle=bundle, objective=obj, prepared=prep_ds)
    res_nondiv = direct_split.solve(case_nondiv, ctx_nondiv, budget)
    assert res_nondiv.evaluation is not None
    assert res_nondiv.evaluation.gross_output == 9179
    nt0, nt1 = res_nondiv.evaluation.trace[0], res_nondiv.evaluation.trace[1]
    assert nt0.amount_in == 7003 and nt0.amount_out == 6526
    assert nt1.amount_in == 3002 and nt1.amount_out == 2653
    assert res_nondiv.search_stats["quotes_executed"] == 25
    print(
        f"Nondivisible 10005 TKA: leg 1 = {nt0.amount_in} ({nt0.amount_out}), "
        f"leg 2 = {nt1.amount_in} ({nt1.amount_out}) -> "
        f"gross {res_nondiv.evaluation.gross_output} "
        f"({res_nondiv.search_stats['quotes_executed']} quotes) [OK]\n"
    )

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
    tr = res_ps.evaluation.trace
    assert tr[0].amount_in == 8000 and tr[0].amount_out == 14773
    assert tr[1].amount_in == 14773 and tr[1].amount_out == 10288
    assert tr[2].amount_in == 2000 and tr[2].amount_out == 2932
    assert tr[3].amount_in == 2932 and tr[3].amount_out == 2293
    assert tr[1].amount_out + tr[3].amount_out == 12581
    assert res_ps.search_stats["bnb_conflicts_excluded"] == 13
    assert res_ps.search_stats["bnb_nodes"] == 12
    print(
        f"Path split: 80% P_AC->P_CB ({tr[1].amount_out}) + "
        f"20% P_AD->P_DB ({tr[3].amount_out}) = "
        f"{res_ps.evaluation.gross_output} TKB [OK]"
    )
    print(
        f"BnB search: {res_ps.search_stats['bnb_nodes']} nodes visited, "
        f"{res_ps.search_stats['bnb_conflicts_excluded']} branches conflict-pruned [OK]\n"
    )

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
    itr = res_ig.evaluation.trace
    assert itr[0].amount_in == 10000 and itr[0].amount_out == 18132
    assert itr[1].amount_in == 5562 and itr[1].amount_out == 5253
    assert itr[2].amount_in == 12570 and itr[2].amount_out == 8844
    assert itr[3].amount_in == 5253 and itr[3].amount_out == 4048
    assert itr[2].amount_out + itr[3].amount_out == 12892
    assert res_ig.search_stats["incremental_chunk_sequence"] == [0, 1, 1, 0, 1, 1, 1, 0, 1, 1]
    print(
        f"Shared intermediate pool P_AC in={itr[0].amount_in} -> out={itr[0].amount_out} TKC.\n"
        f"At TKC, flow branches: {itr[1].amount_in} to P_CD -> P_DB ({itr[3].amount_out} TKB) + "
        f"{itr[2].amount_in} to P_CB ({itr[2].amount_out} TKB).\n"
        f"Total merged output: {res_ig.evaluation.gross_output} TKB (topology: shared_pool) [OK]\n"
    )

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
    str_ = res_sor.evaluation.trace
    assert str_[0].amount_in == 8000 and str_[0].amount_out == 14773
    assert str_[1].amount_in == 14773 and str_[1].amount_out == 10288
    assert str_[2].amount_in == 2000 and str_[2].amount_out == 2932
    assert str_[3].amount_in == 2932 and str_[3].amount_out == 2293
    assert res_sor.search_stats.get("requote_delta") == "0"
    assert res_sor.search_stats["quotes_executed"] == 60
    print(
        f"SOR selection: 80% P_AC->P_CB ({str_[1].amount_out}) + "
        f"20% P_AD->P_DB ({str_[3].amount_out}) = "
        f"{res_sor.evaluation.gross_output} TKB "
        f"({res_sor.search_stats['quotes_executed']} pool quotes) [OK]\n"
    )

    # -------------------------------------------------------------
    # 8. uni_sor_adaptive (recipe H3) on the 5 % grid
    # -------------------------------------------------------------
    print("--- 8. Optimized strategy: uni_sor_adaptive (L08 arm H3) ---")
    sor5 = {"max_hops": 2, "max_splits": 2, "percent_step": 5}
    prep_sor5 = uni_sor_port.prepare(bundle, AlgorithmConfig("uni_sor_port", sor5))
    ctx_sor5 = SolveContext(bundle=bundle, objective=obj, prepared=prep_sor5)
    ref5 = uni_sor_port.solve(case, ctx_sor5, budget)
    assert ref5.evaluation is not None and ref5.evaluation.gross_output == 12581
    assert ref5.search_stats["quotes_executed"] == 120
    res_ada = solve_strategy(uni_sor_strategies.ADAPTIVE, bundle, case, sor5)
    assert res_ada.status == SolveStatus.OK and res_ada.evaluation is not None
    assert res_ada.evaluation.gross_output == 12581 and res_ada.plan == ref5.plan
    assert res_ada.search_stats["quotes_executed"] == 66
    ada_rounds = res_ada.search_stats["sampling"]["rounds"]
    assert [r["added_percents"] for r in ada_rounds] == [
        [25, 50, 75, 100],
        [5, 20, 30, 70, 80],
        [15, 85],
    ]
    assert [r["table_quotes"] for r in ada_rounds] == [0, 30, 12]
    assert res_ada.search_stats["sampling"]["stop_reason"] == "converged"
    for r in ada_rounds:
        print(
            f"  round {r['round']} {r['kind']:<6} +{r['added_percents']} "
            f"-> {r['selection']} ({r['incumbent']})"
        )
    print(
        f"Adaptive sampling: {res_ada.evaluation.gross_output} TKB with "
        f"{res_ada.search_stats['quotes_executed']} quotes "
        f"(uni_sor_port on the same grid: {ref5.search_stats['quotes_executed']}) [OK]"
    )
    narrow = make_narrow_bundle()
    n_case = Case(case_id="ex_narrow", token_in="A", token_out="B", amount_in=10**8)
    n_search = {"max_hops": 2, "max_splits": 4, "percent_step": 5}
    prep_n = uni_sor_port.prepare(narrow, AlgorithmConfig("uni_sor_port", n_search))
    n_ref = uni_sor_port.solve(n_case, SolveContext(narrow, obj, prep_n), budget)
    n_ada = solve_strategy(uni_sor_strategies.ADAPTIVE, narrow, n_case, n_search)
    assert n_ref.evaluation is not None and n_ada.evaluation is not None
    assert n_ref.evaluation.gross_output == 20_795_709
    assert n_ada.evaluation.gross_output == 20_740_242
    n_loss = n_ref.evaluation.gross_output - n_ada.evaluation.gross_output
    print(
        f"Narrow optimum: reference {n_ref.evaluation.gross_output}, adaptive "
        f"{n_ada.evaluation.gross_output} (loss {n_loss} raw = "
        f"{n_loss * 10_000 / n_ref.evaluation.gross_output:.2f} bps, retained) [OK]\n"
    )

    # -------------------------------------------------------------
    # 9. uni_sor_optimized (recipe H4)
    # -------------------------------------------------------------
    print("--- 9. Optimized strategy: uni_sor_optimized (L08 arm H4) ---")
    res_opt = solve_strategy(uni_sor_strategies.OPTIMIZED, bundle, case, sor5)
    assert res_opt.status == SolveStatus.OK and res_opt.evaluation is not None
    assert res_opt.evaluation.gross_output == 12581
    assert res_opt.search_stats["quotes_executed"] == 66
    assert res_opt.search_stats["shortlist"]["quotes"]["probe"] == 12
    assert res_opt.search_stats["strategy"]["tick_math"]["misses"] == 0
    print(
        "Teaching graph: vacuous shortlist (4 routes <= 8), "
        f"{res_opt.evaluation.gross_output} TKB, "
        f"{res_opt.search_stats['quotes_executed']} quotes; "
        "CPMM only, so the L02-L04 counters are 0 [OK]"
    )
    wide = make_wide_bundle()
    w_case = Case(case_id="ex_wide", token_in="A", token_out="B", amount_in=10**6)
    w_search = {"max_hops": 1, "max_splits": 2, "percent_step": 5}
    prep_w = uni_sor_port.prepare(wide, AlgorithmConfig("uni_sor_port", w_search))
    w_ref = uni_sor_port.solve(w_case, SolveContext(wide, obj, prep_w), budget)
    w_ada = solve_strategy(uni_sor_strategies.ADAPTIVE, wide, w_case, w_search)
    w_opt = solve_strategy(uni_sor_strategies.OPTIMIZED, wide, w_case, w_search)
    for r in (w_ref, w_ada, w_opt):
        assert r.evaluation is not None and r.evaluation.gross_output == 974_119
    assert [r.search_stats["quotes_executed"] for r in (w_ref, w_ada, w_opt)] == [200, 130, 119]
    w_ids = w_opt.search_stats["shortlist"]["searched_route_ids"]
    assert w_ids == [f"V2:D{k}" for k in range(2, 10)] + ["V2:T"]
    only100_params: dict[str, object] = {
        **w_search, "probe_percents": [100], "routes_per_probe": 8, "direct_routes": 0,
        "coarse_step": 25, "refine_radius": 1, "soft_max_quotes": None,
    }  # fmt: skip
    prep_100 = uni_sor_fast.prepare(wide, AlgorithmConfig("uni_sor_fast", only100_params))
    w_100 = uni_sor_fast.solve(w_case, SolveContext(wide, obj, prep_100), budget)
    assert w_100.evaluation is not None and w_100.evaluation.gross_output == 941_683
    print(f"Wide graph shortlist (5 % + 100 % probes, top 8 each): {w_ids} (D1 skipped)")
    print(
        "  gross 974119 for port / adaptive / optimized with 200 / 130 / 119 quotes; "
        f"probing 100 % only drops T -> {w_100.evaluation.gross_output} "
        f"({(974_119 - w_100.evaluation.gross_output) * 10_000 / 974_119:.2f} bps loss) [OK]\n"
    )

    # -------------------------------------------------------------
    # 10. Fixed-Block Real State Case (Matching profile config/daily_gross.yaml)
    # -------------------------------------------------------------
    print("--- 10. Real-State Fixed-Block Corpus Case ---")
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
    prep_real_ps = path_split.prepare(
        corpus_bundle, AlgorithmConfig("path_split", profile.search)
    )
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

    # 7-8. optimized strategies
    real_search = dict(profile.search)
    r_ada = solve_strategy(
        uni_sor_strategies.ADAPTIVE, corpus_bundle, real_case, real_search, real_budget
    )
    r_opt = solve_strategy(
        uni_sor_strategies.OPTIMIZED, corpus_bundle, real_case, real_search, real_budget
    )
    for r_o in (r_ada, r_opt):
        assert r_o.status == SolveStatus.OK and r_o.evaluation is not None
        assert r_o.evaluation.gross_output == 10000660449
        assert r_o.search_stats["quotes_executed"] == 12
    opt_ctl = r_opt.search_stats["strategy"]
    assert (opt_ctl["tick_math"]["misses"], opt_ctl["tick_math"]["hits"]) == (1, 5)
    assert opt_ctl["prefix"]["resumed"] == 0

    q_dir = r_dir.candidates_considered
    q_sp = r_sp.search_stats["quotes_executed"]
    q_ds = r_ds.search_stats["quotes_executed"]
    q_ps = r_ps.search_stats["quotes_executed"]
    q_ig = r_ig.search_stats["quotes_executed"]
    q_sor = r_sor.search_stats["quotes_executed"]
    print("Real-state block 101082044 request 10000 USDC -> USDT0 (Profile: daily_gross.yaml):")
    print(f"  direct:            {r_dir.evaluation.gross_output} raw, {q_dir} quotes")
    print(f"  single_path:       {r_sp.evaluation.gross_output} raw, {q_sp} quotes")
    print(f"  direct_split:      {r_ds.evaluation.gross_output} raw, {q_ds} quotes")
    print(f"  path_split:        {r_ps.evaluation.gross_output} raw, {q_ps} quotes")
    print(f"  incremental_graph: {r_ig.evaluation.gross_output} raw, {q_ig} quotes [Agni + Moe LB]")
    print(f"  uni_sor_port:      {r_sor.evaluation.gross_output} raw, {q_sor} quotes [LB excluded]")
    assert r_ada.evaluation is not None and r_opt.evaluation is not None
    print(f"  uni_sor_adaptive:  {r_ada.evaluation.gross_output} raw, 12 quotes [6 of 20 percents]")
    print(
        f"  uni_sor_optimized: {r_opt.evaluation.gross_output} raw, 12 quotes "
        "[L02-L04: tick memo 1 miss / 5 hits]"
    )
    print("All real-state assertions passed [OK]\n")

    print("=================================================================")
    print("All 10 verification suites passed with 100% deterministic equality.")
    print("=================================================================")


if __name__ == "__main__":
    run_all()

"""`metis_inspired` (WHI-1449): the Metis-inspired label search over `incremental_graph`'s
chunk allocation, against the WHI-1448 contract (docs/references/jupiter-metis-challenge.md
§9.2, §10.5 fixtures X1-X7, §10.6 gate S2 diagnostic).

Expected values are independent of the code under test: hand CPMM formulas (`_v2_out`,
the Solidity `getAmountOut` generalized to `fee_bps`), explicit per-path exact quotes
through `pools.quote.quote_exact_in`, hand-counted relaxations/paths, the registered
`incremental_graph` / `path_split` solvers run on their own, and a plain memo-free
`evaluate()` replay of every returned plan.
"""

from __future__ import annotations

import dataclasses
import json
import pickle
import random
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from benchmark.objective import ObjectiveContext, gross_only
from benchmark.profile import ProfileError, load_profile, parse_profile, strategy_group
from pools.quote import metered_quotes, quote_exact_in
from pools.result import QuoteStatus
from routing.algorithms import incremental_graph, metis_inspired, path_split
from routing.algorithms.base import AlgorithmConfig, Budget, SolveContext, SolveResult, SolveStatus
from routing.algorithms.registry import ALGORITHMS, BASE_STRATEGIES, OPTIMIZED_STRATEGIES
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import REQUEST_FUND_ID
from snapshot.bundle import load_bundle
from snapshot.models import BlockRef, Case, ConstantProductPoolState, PoolState, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "routing"
BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)


@cache
def fixture(name: str) -> SnapshotBundle:
    return load_bundle(FIXTURES / name)


def _bundle(*pools: PoolState) -> SnapshotBundle:
    return SnapshotBundle(
        bundle_id="t",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools={p.pool_id: p for p in pools},
        cases=(),
        bundle_hash="deadbeef",
        source_path="<test>",
    )


def _cp(
    pool_id: str, t0: str, t1: str, r0: int, r1: int, fee_bps: int = 30
) -> ConstantProductPoolState:
    return ConstantProductPoolState(
        pool_id=pool_id,
        token0=t0,
        token1=t1,
        reserve0=r0,
        reserve1=r1,
        fee_bps=fee_bps,
        source_key=None,
    )


def _v2_out(pool: PoolState, token_in: str, amount_in: int) -> int:
    assert isinstance(pool, ConstantProductPoolState)
    r_in, r_out = pool.reserves_for(token_in)
    fee_in = amount_in * (10_000 - pool.fee_bps)
    return fee_in * r_out // (r_in * 10_000 + fee_in)


def _chain(bundle: SnapshotBundle, token: str, amount: int, *pool_ids: str) -> int:
    """Hand CPMM output of one swap of `amount` along the pools (original reserves)."""
    for pid in pool_ids:
        pool = bundle.pools[pid]
        amount = _v2_out(pool, token, amount)
        assert isinstance(pool, ConstantProductPoolState)
        token = pool.other_token(token)
    return amount


def _params(
    max_hops: int = 3,
    chunks: int = 20,
    label_hops: int | None = None,
    pruning: bool = True,
    max_splits: int = 4,
    step: int = 5,
) -> dict[str, Any]:
    return {
        "max_hops": max_hops,
        "max_splits": max_splits,
        "percent_step": step,
        "chunks": chunks,
        "label_hops": max_hops if label_hops is None else label_hops,
        "label_pruning": pruning,
    }


def _prepared(bundle: SnapshotBundle, **params: Any) -> metis_inspired.PreparedMetisInspired:
    return metis_inspired.prepare(bundle, AlgorithmConfig(metis_inspired.NAME, _params(**params)))


def _metis(
    bundle: SnapshotBundle,
    case: Case,
    *,
    budget: Budget | None = None,
    objective: ObjectiveContext | None = None,
    sink: list[Any] | None = None,
    **params: Any,
) -> SolveResult:
    context = SolveContext(
        bundle,
        objective or gross_only(),
        _prepared(bundle, **params),
        candidate_sink=None if sink is None else sink.append,
    )
    return metis_inspired.solve(case, context, budget or Budget())


def _a0(
    bundle: SnapshotBundle,
    case: Case,
    *,
    budget: Budget | None = None,
    objective: ObjectiveContext | None = None,
    sink: list[Any] | None = None,
    max_hops: int = 3,
    chunks: int = 20,
) -> SolveResult:
    params = {"max_hops": max_hops, "max_splits": 4, "percent_step": 5, "chunks": chunks}
    prepared = incremental_graph.prepare(bundle, AlgorithmConfig(incremental_graph.NAME, params))
    context = SolveContext(
        bundle,
        objective or gross_only(),
        prepared,
        candidate_sink=None if sink is None else sink.append,
    )
    return incremental_graph.solve(case, context, budget or Budget())


def _path_split(bundle: SnapshotBundle, case: Case, max_hops: int = 3) -> SolveResult:
    params = {"max_hops": max_hops, "max_splits": 4, "percent_step": 5}
    prepared = path_split.prepare(bundle, AlgorithmConfig(path_split.NAME, params))
    return path_split.solve(case, SolveContext(bundle, gross_only(), prepared), Budget())


def _replayed(bundle: SnapshotBundle, case: Case, result: SolveResult) -> Evaluation:
    """Plain, memo-free, unmetered replay of the returned plan; full fill and conservation."""
    assert result.plan is not None and result.evaluation is not None
    ev = evaluate(bundle, case, result.plan, gross_only())
    assert ev.status is EvalStatus.OK, ev.error
    assert ev.gross_output == result.evaluation.gross_output
    assert ev.residuals == {}
    funds = {f.fund_id: f for f in ev.funds}
    assert funds[REQUEST_FUND_ID].consumed == case.amount_in
    terminal = 0
    for f in ev.funds:
        if f.token == case.token_out:
            terminal += f.remaining
        else:
            assert f.remaining == 0, f
    assert terminal == ev.gross_output
    return ev


def _pools_of(result: SolveResult) -> list[str]:
    assert result.plan is not None
    return [s.pool_id for s in result.plan.steps]


# metis_inspired's own search keys; every other key has incremental_graph's meaning.
METIS_KEYS = {
    "label_hops",
    "label_pruning",
    "chunk_search",
    "candidate_unit",
    "label_relaxations",
    "label_rejected_cycle",
    "label_pruned_distance",
    "label_skipped_revisit",
    "label_truncated_chunks",
    "chunk_path_hops",
}


# ------------------------------------------------------------ fixtures

K = 3
# X1: k parallel pools on every hop of S-B-C-D, plus one shallow direct S-D pool.
PARALLEL = _bundle(
    *(
        _cp(f"sb{i}", "S", "B", 10**12 + i * 10**10, 10**12, fee_bps=(5, 30, 100)[i])
        for i in range(K)
    ),
    *(
        _cp(f"bc{i}", "B", "C", 10**12, 10**12 - i * 10**10, fee_bps=(30, 5, 100)[i])
        for i in range(K)
    ),
    *(
        _cp(f"cd{i}", "C", "D", 10**12 + i * 3 * 10**9, 10**12, fee_bps=(100, 30, 5)[i])
        for i in range(K)
    ),
    _cp("sd", "S", "D", 10**9, 10**9),
)
PARALLEL_CASE = Case("x1", "S", "D", 10**10)

# Shared prefix / suffix / single pool (incremental_graph's fixture pattern).
PREFIX = _bundle(
    _cp("hub", "A", "C", 10**12, 10**12, fee_bps=1),
    _cp("cb1", "C", "B", 10**9, 10**9),
    _cp("cb2", "C", "B", 10**9, 10**9),
    _cp("ab", "A", "B", 10**8, 10**8),
)
SUFFIX = _bundle(
    _cp("ac1", "A", "C", 10**9, 10**9),
    _cp("ac2", "A", "C", 10**9, 10**9),
    _cp("hub", "C", "B", 10**12, 10**12, fee_bps=1),
)
SINGLE = _bundle(_cp("ab", "A", "B", 10**9, 10**9, fee_bps=30))
CASE = Case("c", "A", "B", 4 * 10**8)

USDT = "0x201eba5cc46d216ce6dc03f6a759e8e766e956ae"
WMNT = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"
UNI_USDT_WMNT = "0x4cdfc22bf05209de87ee564746dc7e5174631d2b"  # real CL pool, collected range


def _cl() -> PoolState:
    return fixture("mantle_mixed").pools[UNI_USDT_WMNT]


# X2: the only good X -> D pool (real CL state) is collected only for small inputs; the
# maximal S -> USDT label ("big") overshoots it, the dominated one ("small") fits.
FAILURE = _bundle(
    _cp("big", "S", USDT, 10**12, 10**14),
    _cp("small", "S", USDT, 10**9, 10**9),
    _cl(),
    _cp("weak", "S", WMNT, 10**9, 10**15),
)
FAILURE_CASE = Case("x2", "S", WMNT, 10**7)

# X3: deep liquidity only along the 4-hop S-B-C-E-D; the only <= 3-hop route is shallow.
FOUR_HOP = _bundle(
    _cp("sb", "S", "B", 10**15, 10**15, fee_bps=5),
    _cp("bc", "B", "C", 10**15, 10**15, fee_bps=5),
    _cp("ce", "C", "E", 10**15, 10**15, fee_bps=5),
    _cp("ed", "E", "D", 10**15, 10**15, fee_bps=5),
    _cp("sd", "S", "D", 10**10, 10**10),
)
FOUR_HOP_CASE = Case("x3", "S", "D", 10**10)

# X4: the best 2-prefix to X is S-Y-X (yx: 1 Y -> ~20 X); the best 4-hop path is
# S-A-X-Y-D (xy: 1 X -> ~0.2 Y), which needs X -> Y after the prefix already visited Y.
REVISIT = _bundle(
    _cp("sy", "S", "Y", 10**15, 10**15, fee_bps=5),
    _cp("sa", "S", "A", 10**15, 10**15, fee_bps=5),
    _cp("ax", "A", "X", 10**15, 10 * 10**15, fee_bps=5),
    _cp("yx", "Y", "X", 10**15, 20 * 10**15, fee_bps=5),
    _cp("xy", "X", "Y", 5 * 10**15, 10**15, fee_bps=5),
    _cp("yd", "Y", "D", 10**15, 10**15, fee_bps=5),
)
REVISIT_CASE = Case("x4", "S", "D", 10**9)

# X4b (prefix-dependent admission, H >= 4; this implementation's note beyond memo §9.3):
# chunk 1 commits S-X-A-D. In chunk 2 the dominant 2-hop label at V runs S-A-V, whose
# continuation V -> X closes the committed cycle X -> A -> V -> X; the dominated S-B-V
# continues S-B-V-X-D, the enumeration maximum.
DEEP = 10**15
ADMISSION = _bundle(
    _cp("sx", "S", "X", 8 * 10**9, 8 * 10**9, fee_bps=5),
    _cp("xa", "X", "A", DEEP, 2 * DEEP, fee_bps=5),
    _cp("ad", "A", "D", DEEP, 2 * DEEP, fee_bps=5),
    _cp("xd", "X", "D", DEEP, DEEP, fee_bps=5),
    _cp("sa", "S", "A", DEEP, DEEP, fee_bps=5),
    _cp("av", "A", "V", DEEP, DEEP * 11 // 10, fee_bps=5),
    _cp("sb", "S", "B", DEEP, DEEP, fee_bps=5),
    _cp("bv", "B", "V", DEEP, DEEP, fee_bps=5),
    _cp("vx", "V", "X", DEEP, 3 * DEEP, fee_bps=5),
)
ADMISSION_CASE = Case("x4b", "S", "D", 2 * 10**9)

# A tie: "p3" delivers more X than "p1", but the thin X -> D pool rounds both to the same
# output; enumeration keeps the first maximal path (via p1), the label search only
# continues the dominant label (via p3).
TIE = _bundle(
    _cp("p1", "S", "X", 10**9, 10**8),
    _cp("p3", "S", "X", 10**9, 10**8 + 10**6),
    _cp("q", "X", "D", 10**6, 4500),
)
TIE_CASE = Case("tie", "S", "D", 10**4)


# ------------------------------------------------------------ X1 parallel pools (H = 3)


def test_x1_parallel_pools_same_plan_with_hand_counted_work() -> None:
    chunks = 10
    m3 = _metis(PARALLEL, PARALLEL_CASE, chunks=chunks)
    a0 = _a0(PARALLEL, PARALLEL_CASE, chunks=chunks)
    assert m3.status is a0.status is SolveStatus.OK
    assert m3.plan == a0.plan and m3.score == a0.score
    assert m3.evaluation is not None and a0.evaluation is not None
    assert m3.evaluation.gross_output == a0.evaluation.gross_output
    s, r = m3.search_stats, a0.search_stats
    assert s["incremental_allocation"] == r["incremental_allocation"]
    assert s["incremental_chunk_sequence"] == r["incremental_chunk_sequence"]
    assert s["chunks_allocated"] == chunks and s["chunks_carried"] == 0
    # Hand counts per chunk. Label: layer 1 = k edges S->B + the direct S->D; layer 2 = k
    # edges B->C (B->S is never relaxed); layer 3 = k edges C->D (the k edges C->B are
    # distance-pruned). Enumeration: k^3 three-hop paths + the direct one.
    assert s["label_relaxations"] == chunks * (3 * K + 1)
    assert s["label_pruned_distance"] == chunks * K
    assert s["label_skipped_revisit"] == s["label_rejected_cycle"] == 0
    assert r["paths_enumerated"] == K**3 + 1 and r["paths_scored"] == chunks * (K**3 + 1)
    assert s["paths_scored"] is None and s["paths_enumerated"] is None  # other unit
    assert s["candidate_unit"] == "label_relaxation" and s["chunk_search"] == "label"
    assert s["quotes_executed"] <= r["quotes_executed"]
    assert m3.candidates_considered < a0.candidates_considered
    ev = _replayed(PARALLEL, PARALLEL_CASE, m3)
    assert s["accounting_matches_evaluation"] is True
    # Merged plan: every pool called once on its original reserves (hand formula).
    for t in ev.trace:
        assert t.amount_out == _v2_out(PARALLEL.pools[t.pool_id], t.token_in, t.amount_in)
    assert s["chunk_path_hops"] == {"3": chunks}


# ------------------------------------------------------------ X2 non-downward-closed failure


def test_x2_maximal_label_overshoots_a_collected_range_the_dominated_prefix_fits() -> None:
    a = FAILURE_CASE.amount_in
    big, small = _v2_out(FAILURE.pools["big"], "S", a), _v2_out(FAILURE.pools["small"], "S", a)
    assert big > small
    # Explicit per-path exact quotes: the big amount is beyond the collected state, the
    # small one fits; the fitting 2-hop path beats the direct pool.
    over = quote_exact_in(_cl(), USDT, big)
    fit = quote_exact_in(_cl(), USDT, small)
    assert over.status is QuoteStatus.INCOMPLETE_SNAPSHOT
    assert fit.status is QuoteStatus.OK and fit.amount_in_consumed == small
    weak = _v2_out(FAILURE.pools["weak"], "S", a)
    assert fit.amount_out > weak > 0

    m3 = _metis(FAILURE, FAILURE_CASE, chunks=1)
    a0 = _a0(FAILURE, FAILURE_CASE, chunks=1)
    # A0's chunk takes the fitting path; M3's only continuation from its maximal label
    # fails, so its chunk takes the direct pool.
    assert a0.search_stats["incremental_evaluated_gross"] == str(fit.amount_out)
    assert m3.search_stats["incremental_evaluated_gross"] == str(weak)
    assert m3.search_stats["marginal_failures"] == {"incomplete_snapshot": 1}
    assert m3.search_stats["marginal_incomplete"] == 1
    assert "-[big]->" in m3.search_stats["incomplete_example"]
    # The retained simpler candidate still carries the fitting route (never worse).
    baseline = _path_split(FAILURE, FAILURE_CASE)
    assert m3.score == a0.score == baseline.score
    assert m3.search_stats["chosen_source"] != metis_inspired.NAME
    _replayed(FAILURE, FAILURE_CASE, m3)


# ------------------------------------------------------------ X3 four-hop reach


def test_x3_four_hop_route_beats_the_three_hop_reference_by_the_hand_figure() -> None:
    a = FOUR_HOP_CASE.amount_in
    deep = _chain(FOUR_HOP, "S", a, "sb", "bc", "ce", "ed")
    direct = _v2_out(FOUR_HOP.pools["sd"], "S", a)
    assert deep > direct
    a0 = _a0(FOUR_HOP, FOUR_HOP_CASE, chunks=1)
    m4 = _metis(FOUR_HOP, FOUR_HOP_CASE, chunks=1, label_hops=4)
    m4_off = _metis(FOUR_HOP, FOUR_HOP_CASE, chunks=1, label_hops=4, pruning=False)
    assert a0.score == direct
    assert m4.score == m4_off.score == deep
    assert _pools_of(m4) == ["sb", "bc", "ce", "ed"]
    assert m4.search_stats["chunk_path_hops"] == {"4": 1}
    assert m4.search_stats["max_hops"] == 3 and m4.search_stats["label_hops"] == 4
    assert m4.search_stats["path_split_score"] == str(direct)  # embedded at max_hops 3
    assert m4.search_stats["accounting_matches_evaluation"] is True
    _replayed(FOUR_HOP, FOUR_HOP_CASE, m4)
    # With many chunks the 4-hop reach still wins and replays exactly.
    a0_many = _a0(FOUR_HOP, FOUR_HOP_CASE, chunks=20)
    m4_many = _metis(FOUR_HOP, FOUR_HOP_CASE, chunks=20, label_hops=4)
    assert m4_many.score is not None and a0_many.score is not None
    assert m4_many.score > a0_many.score
    assert m4_many.search_stats["accounting_matches_evaluation"] is True
    assert "4" in m4_many.search_stats["chunk_path_hops"]
    _replayed(FOUR_HOP, FOUR_HOP_CASE, m4_many)


# ------------------------------------------------------------ X4 token-revisit loss (H = 4)


def test_x4_token_revisit_pruning_loses_the_best_four_hop_path() -> None:
    a = REVISIT_CASE.amount_in
    via_y = _chain(REVISIT, "S", a, "sy", "yx")
    via_a = _chain(REVISIT, "S", a, "sa", "ax")
    assert via_y > via_a  # the dominant 2-hop label at X already visits Y
    best4 = _chain(REVISIT, "S", a, "sa", "ax", "xy", "yd")
    two_hop = _chain(REVISIT, "S", a, "sy", "yd")
    assert best4 > two_hop
    m4 = _metis(REVISIT, REVISIT_CASE, chunks=1, label_hops=4)
    m4_off = _metis(REVISIT, REVISIT_CASE, chunks=1, label_hops=4, pruning=False)
    assert m4_off.score == best4 and _pools_of(m4_off) == ["sa", "ax", "xy", "yd"]
    assert m4.score == two_hop  # the heuristic limit, by the hand gap best4 - two_hop
    assert m4.search_stats["label_skipped_revisit"] > 0
    _replayed(REVISIT, REVISIT_CASE, m4)
    _replayed(REVISIT, REVISIT_CASE, m4_off)
    # At 3 hops neither the reference nor the label search can reach it.
    assert _a0(REVISIT, REVISIT_CASE, chunks=1).score == two_hop


def test_x4b_prefix_dependent_admission_loses_a_four_hop_continuation() -> None:
    half = ADMISSION_CASE.amount_in // 2
    m4 = _metis(ADMISSION, ADMISSION_CASE, chunks=2, label_hops=4)
    m4_off = _metis(ADMISSION, ADMISSION_CASE, chunks=2, label_hops=4, pruning=False)
    # Hand figures of the merged plans: M4 puts both chunks on S-X-A-D (one swap of the
    # total); M4-off puts chunk 2 on the pool-disjoint S-B-V-X-D.
    same = _chain(ADMISSION, "S", 2 * half, "sx", "xa", "ad")
    split = _chain(ADMISSION, "S", half, "sx", "xa", "ad") + _chain(
        ADMISSION, "S", half, "sb", "bv", "vx", "xd"
    )
    assert m4.search_stats["incremental_evaluated_gross"] == str(same)
    assert m4_off.search_stats["incremental_evaluated_gross"] == str(split)
    assert split > same and m4.search_stats["label_rejected_cycle"] > 0
    diag = _diagnose(ADMISSION, ADMISSION_CASE, chunks=2, label_hops=4)
    assert diag["classes"] == {"agree": 1, "prefix_admission": 1}
    assert diag["unexplained_chunks"] == 0
    assert "closes a committed token cycle" in diag["chunk_records"][1]["detail"]


# ------------------------------------------------------------ X5 common semantics


@pytest.mark.parametrize("bundle", [PREFIX, SUFFIX, SINGLE], ids=["prefix", "suffix", "single"])
@pytest.mark.parametrize("amount", [3, 19, 10**8 + 7, 4 * 10**8])
def test_x5_shared_state_nondivisible_and_dust_match_the_reference(
    bundle: SnapshotBundle, amount: int
) -> None:
    case = Case("c", "A", "B", amount)
    m3, a0 = _metis(bundle, case, chunks=13), _a0(bundle, case, chunks=13)
    assert m3.status is SolveStatus.OK
    assert m3.plan == a0.plan and m3.score == a0.score
    s = m3.search_stats
    assert s["chunks_allocated"] + s["chunks_carried"] == 13 - s["chunks_empty"]
    assert sum(int(x["amount_in"]) for x in s["incremental_allocation"]) == amount
    assert s["incremental_status"] == "ok" and s["accounting_matches_evaluation"] is True
    _replayed(bundle, case, m3)


def test_x5_shared_prefix_and_suffix_are_shared_pool_plans() -> None:
    for bundle, hub_inputs in ((PREFIX, 1), (SUFFIX, 2)):
        m3 = _metis(bundle, CASE)
        assert m3.search_stats["chosen_source"] == metis_inspired.NAME
        assert m3.search_stats["topology"] == "shared_pool"
        assert m3.plan is not None
        hub = [s for s in m3.plan.steps if s.pool_id == "hub"]
        assert len(hub) == 1 and len(hub[0].inputs) == hub_inputs  # one merged call
        ev = _replayed(bundle, CASE, m3)
        for t in ev.trace:
            assert t.amount_out == _v2_out(bundle.pools[t.pool_id], t.token_in, t.amount_in)


def test_x5_dust_is_carried_and_fully_allocated() -> None:
    case = Case("c", "A", "B", 7)
    m3 = _metis(PREFIX, case, chunks=20)
    s = m3.search_stats
    assert s["chunks_empty"] == 13 and s["chunks_carried"] > 0
    assert sum(int(x["amount_in"]) for x in s["incremental_allocation"]) == 7
    _replayed(PREFIX, case, m3)


def test_x5_never_worse_than_the_retained_simpler_routes() -> None:
    for bundle, case_ids in (
        (fixture("cpmm_graph"), ["a_b_large", "a_b_small", "a_d_multi_hop"]),
        (fixture("mantle_mixed"), [c.case_id for c in fixture("mantle_mixed").cases]),
    ):
        for case_id in case_ids:
            case = bundle.case(case_id)
            for hops in (2, 3):
                result = _metis(bundle, case, max_hops=2, label_hops=hops)
                baseline = _path_split(bundle, case, max_hops=2)
                assert result.status is SolveStatus.OK, case_id
                assert result.score is not None and baseline.score is not None
                assert result.score >= baseline.score, case_id
                assert result.search_stats["path_split_score"] == str(baseline.score)
                assert result.search_stats["accounting_matches_evaluation"] is True
                _replayed(bundle, case, result)


# ------------------------------------------------------------ X6 budgets and statuses


def test_x6_quote_budget_stops_before_the_meter_and_is_declared() -> None:
    unbounded = _metis(PREFIX, CASE)
    ps_quotes = _path_split(PREFIX, CASE).search_stats["quotes_executed"]
    limit = ps_quotes + 3
    assert unbounded.search_stats["quotes_executed"] > limit
    with metered_quotes(limit) as meter:
        result = _metis(PREFIX, CASE, budget=Budget(max_quotes=limit))
    s = result.search_stats
    assert not meter.exceeded and meter.counted == s["quotes_executed"] <= limit
    assert result.status is SolveStatus.OK  # the retained simpler route
    assert s["truncated_by"] == "max_quotes" and s["incremental_status"] == "truncated"
    assert s["truncated_stages"] == [metis_inspired.NAME]
    assert s["label_truncated_chunks"] == 1 and result.candidates_truncated > 0
    assert s["chosen_source"] != metis_inspired.NAME
    assert result.score is not None and unbounded.score is not None
    assert result.score < unbounded.score


def test_x6_truncation_without_a_route_is_timeout_never_no_route() -> None:
    bundle = fixture("cpmm_graph")
    result = _metis(bundle, bundle.case("a_d_multi_hop"), budget=Budget(max_quotes=1))
    assert result.status is SolveStatus.TIMEOUT
    assert result.error is not None and "not evidence of no_route" in result.error


def test_x6_candidate_cap_caps_relaxations_per_chunk_as_declared_truncation() -> None:
    free = _metis(PARALLEL, PARALLEL_CASE, chunks=10)
    cap = 4  # layer 1 of X1 needs exactly k + 1 = 4 relaxations
    result = _metis(PARALLEL, PARALLEL_CASE, chunks=10, budget=Budget(max_candidates=cap))
    s = result.search_stats
    assert result.status is SolveStatus.OK
    assert s["truncated_by"] == "max_candidates"
    assert s["label_relaxations"] == 10 * cap < free.search_stats["label_relaxations"]
    assert s["label_truncated_chunks"] == 10
    assert result.candidates_truncated >= 10
    # Only the direct pool was reachable inside the cap: every chunk took it.
    assert s["incremental_allocation"] == [
        {"path": "S -[sd]-> D", "chunks": 10, "amount_in": str(10**10)}
    ]


def test_x6_quote_accounting_is_exact() -> None:
    for bundle, case in ((PREFIX, CASE), (PARALLEL, PARALLEL_CASE), (REVISIT, REVISIT_CASE)):
        with metered_quotes(None) as meter:
            result = _metis(bundle, case, label_hops=4)
        assert result.search_stats["quotes_executed"] == meter.counted


def test_x6_unreachable_and_beyond_the_label_bound_are_no_route() -> None:
    bundle = _bundle(_cp("ab", "A", "B", 10**6, 10**6), _cp("cd", "C", "D", 10**6, 10**6))
    result = _metis(bundle, Case("c", "A", "D", 1000))
    assert result.status is SolveStatus.NO_ROUTE
    assert result.error is not None and "unreachable" in result.error
    assert result.search_stats["incremental_status"] == "no_paths"
    chain = _bundle(*(_cp(f"p{i}", f"T{i}", f"T{i + 1}", 10**9, 10**9) for i in range(5)))
    far = _metis(chain, Case("c", "T0", "T5", 1000), label_hops=4)
    assert far.status is SolveStatus.NO_ROUTE
    assert far.search_stats["incremental_status"] == "no_paths"
    near = _metis(chain, Case("c", "T0", "T4", 1000), label_hops=4)
    assert near.status is SolveStatus.OK and near.search_stats["chunk_path_hops"] == {"4": 20}


def test_x6_only_incomplete_candidates_is_incomplete_snapshot() -> None:
    bundle = _bundle(_cl())
    result = _metis(bundle, Case("huge", USDT, WMNT, 10**18))
    assert result.status is SolveStatus.INCOMPLETE_SNAPSHOT
    assert result.search_stats["incremental_status"].endswith("no_admissible_path")


@dataclass(frozen=True)
class PerCallCost(ObjectiveContext):
    per_call: int = 0

    def score(self, evaluation: Evaluation) -> int:
        return evaluation.gross_output - self.per_call * evaluation.route_features["pool_calls"]


def test_x6_per_call_cost_keeps_the_simpler_route_and_plans_publish_in_order() -> None:
    assert _metis(PREFIX, CASE).search_stats["chosen_source"] == metis_inspired.NAME
    cost = PerCallCost(mode="synthetic_fixed_cost", per_call=10**8)
    net = _metis(PREFIX, CASE, objective=cost)
    assert net.status is SolveStatus.OK and net.search_stats["chosen_source"] != metis_inspired.NAME
    published: list[Any] = []
    result = _metis(PREFIX, CASE, sink=published)
    assert published and published[-1] == result.plan
    scores = [evaluate(PREFIX, CASE, p, gross_only()).gross_output for p in published]
    assert scores == sorted(scores) and len(set(scores)) == len(scores)


# ------------------------------------------------------------ X7 ablation identity


def _normalized(result: SolveResult) -> dict[str, Any]:
    """A `metis_inspired` result in `incremental_graph`'s terms: the variant's identity
    and its own search keys removed, nothing else touched."""
    stats = {k: v for k, v in result.search_stats.items() if k not in METIS_KEYS}
    if stats["chosen_source"] == metis_inspired.NAME:
        stats["chosen_source"] = incremental_graph.NAME
    stats["truncated_stages"] = [
        incremental_graph.NAME if s == metis_inspired.NAME else s for s in stats["truncated_stages"]
    ]
    return {
        **{f.name: getattr(result, f.name) for f in dataclasses.fields(SolveResult)},
        "algorithm": incremental_graph.NAME,
        "search_stats": stats,
    }


def _assert_ablation_identity(
    bundle: SnapshotBundle, case: Case, *, budget: Budget | None = None, **kw: Any
) -> None:
    ref_published: list[Any] = []
    got_published: list[Any] = []
    with metered_quotes(None) as ref_meter:
        ref = _a0(bundle, case, budget=budget, sink=ref_published, **kw)
    with metered_quotes(None) as got_meter:
        got = _metis(bundle, case, budget=budget, sink=got_published, pruning=False, **kw)
    assert got.search_stats["chunk_search"] == "enumeration"
    assert got.search_stats["label_relaxations"] is None
    reference = {
        **{f.name: getattr(ref, f.name) for f in dataclasses.fields(SolveResult)},
        "search_stats": dict(ref.search_stats),
    }
    assert _normalized(got) == reference
    assert got_meter.counted == ref_meter.counted and got_published == ref_published


X7_FIXTURES = [
    (PARALLEL, PARALLEL_CASE),
    (FAILURE, FAILURE_CASE),
    (FOUR_HOP, FOUR_HOP_CASE),
    (REVISIT, REVISIT_CASE),
    (ADMISSION, ADMISSION_CASE),
    (TIE, TIE_CASE),
    (PREFIX, CASE),
    (SUFFIX, CASE),
    (SINGLE, CASE),
    (PREFIX, Case("dust", "A", "B", 7)),
    (_bundle(_cl()), Case("huge", USDT, WMNT, 10**18)),
    (_bundle(_cp("ab", "A", "B", 10**6, 10**6), _cp("cd", "C", "D", 10**6, 10**6)),
     Case("c", "A", "D", 1000)),
]  # fmt: skip


@pytest.mark.parametrize(("bundle", "case"), X7_FIXTURES)
@pytest.mark.parametrize(
    "budget",
    [None, Budget(max_candidates=2), Budget(max_quotes=40)],
    ids=["free", "cand2", "quotes40"],
)
@pytest.mark.parametrize("chunks", [1, 13])
def test_x7_disabled_mechanism_equals_incremental_graph(
    bundle: SnapshotBundle, case: Case, budget: Budget | None, chunks: int
) -> None:
    _assert_ablation_identity(bundle, case, budget=budget, chunks=chunks)


@pytest.mark.parametrize("case_id", [c.case_id for c in fixture("mantle_mixed").cases])
@pytest.mark.parametrize("hops", [2, 3])
@pytest.mark.parametrize(
    "budget", [None, Budget(max_quotes=300), Budget(max_candidates=3)], ids=["free", "q300", "c3"]
)
def test_x7_disabled_mechanism_equals_incremental_graph_on_real_state(
    case_id: str, hops: int, budget: Budget | None
) -> None:
    bundle = fixture("mantle_mixed")
    _assert_ablation_identity(bundle, bundle.case(case_id), budget=budget, max_hops=hops, chunks=37)


def test_x7_cpmm_fixture_with_budgets() -> None:
    bundle = fixture("cpmm_graph")
    for case in bundle.cases:
        for budget in (None, Budget(max_quotes=5), Budget(max_candidates=1)):
            _assert_ablation_identity(bundle, case, budget=budget)


# ------------------------------------------------------------ H-M1a invariants (H <= 3)


def _random_bundle(seed: int) -> tuple[SnapshotBundle, list[Case]]:
    """A small random CPMM graph (parallel pools, both directions, cycles through
    intermediate tokens) and cases from dust to large (incremental_graph's pattern)."""
    rng = random.Random(seed)
    tokens = ["A", "B", "C", "D", "E"][: rng.randint(3, 5)]
    pools = []
    for n in range(rng.randint(4, 9)):
        t0, t1 = rng.sample(tokens, 2)
        r0, r1 = (10 ** rng.randint(5, 12) * rng.randint(1, 9) for _ in range(2))
        pools.append(_cp(f"p{n}", t0, t1, r0, r1, fee_bps=rng.choice([0, 1, 5, 30, 100])))
    amounts = [rng.randint(1, 60), rng.randint(10**5, 10**7), 10 ** rng.randint(6, 11) + 7]
    return _bundle(*pools), [Case(f"c{a}", "A", "B", a) for a in amounts]


def test_label_search_agrees_with_the_reference_up_to_attributed_classes() -> None:
    """S2 on random graphs at H = 2 and 3: a plan differs from incremental_graph only with
    an attributed divergent chunk (tie / quote failure); with every chunk agreeing the plan
    is the reference's and its executed quotes are no more (memo §9.3 quote subset)."""
    agreeing = 0
    for seed in range(40):
        bundle, cases = _random_bundle(seed)
        for case in cases:
            for hops, chunks in ((2, 7), (3, 20)):
                prepared = _prepared(bundle, max_hops=hops, chunks=chunks)
                diag = metis_inspired.diagnose_case(case, bundle, prepared)
                assert diag["unexplained_chunks"] == 0, diag
                assert diag["quote_subset_violations"] == 0, diag
                m3 = _metis(bundle, case, max_hops=hops, chunks=chunks)
                a0 = _a0(bundle, case, max_hops=hops, chunks=chunks)
                s = m3.search_stats
                if s["incremental_status"] == "ok":  # the diagnostic replays the trajectory
                    assert diag["incremental_allocation"] == s["incremental_allocation"]
                    assert diag["incremental_chunk_sequence"] == s["incremental_chunk_sequence"]
                if diag["divergent_chunks"] == 0:
                    agreeing += 1
                    assert m3.plan == a0.plan and m3.score == a0.score
                    assert s["incremental_allocation"] == a0.search_stats["incremental_allocation"]
                    assert s["quotes_executed"] <= a0.search_stats["quotes_executed"]
            for label_hops in (4, 5):  # beyond the derived domain: only the allowed classes
                prepared = _prepared(bundle, max_hops=3, label_hops=label_hops, chunks=20)
                deep = metis_inspired.diagnose_case(case, bundle, prepared)
                assert deep["unexplained_chunks"] == 0, deep
    assert agreeing > 100


# ------------------------------------------------------------ S2 diagnostic pass


def _diagnose(bundle: SnapshotBundle, case: Case, **params: Any) -> dict[str, Any]:
    return metis_inspired.diagnose_case(case, bundle, _prepared(bundle, **params))


def test_diagnostic_classifies_the_distinguishing_fixtures() -> None:
    agree = _diagnose(PARALLEL, PARALLEL_CASE, chunks=10)
    assert agree["classes"] == {"agree": 10} and agree["divergent_chunks"] == 0
    assert agree["quote_subset_violations"] == 0
    assert agree["counters"]["label_relaxations"] == 10 * (3 * K + 1)
    assert agree["counters"]["enumeration_paths_scored"] == 10 * (K**3 + 1)

    fail = _diagnose(FAILURE, FAILURE_CASE, chunks=1)
    assert fail["classes"] == {"quote_failure": 1} and fail["unexplained_chunks"] == 0
    record = fail["chunk_records"][0]
    assert "incomplete_snapshot" in record["detail"] and "-[big]->" in record["detail"]
    assert record["enumeration"]["path"].startswith("S -[small]->")

    a = TIE_CASE.amount_in
    x1, x3 = _v2_out(TIE.pools["p1"], "S", a), _v2_out(TIE.pools["p3"], "S", a)
    assert x3 > x1 and _v2_out(TIE.pools["q"], "X", x3) == _v2_out(TIE.pools["q"], "X", x1) > 0
    tie = _diagnose(TIE, TIE_CASE, chunks=1)
    assert tie["classes"] == {"tie": 1} and tie["unexplained_chunks"] == 0
    record = tie["chunk_records"][0]
    assert record["label"]["marginal"] == record["enumeration"]["marginal"]
    assert "-[p3]->" in record["label"]["path"] and "-[p1]->" in record["enumeration"]["path"]

    revisit = _diagnose(REVISIT, REVISIT_CASE, chunks=1, label_hops=4)
    assert revisit["classes"] == {"token_revisit": 1} and revisit["unexplained_chunks"] == 0
    assert revisit["quote_subset_violations"] is None  # derived for H <= 3 only
    assert revisit["relaxations_outside_enumeration"] > 0  # S-Y-X has no simple completion


def test_diagnostic_flags_an_unsound_label_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """A label search that keeps the *worse* label (a broken dominance rule) produces
    `unattributed` chunks: the diagnostic does not explain away a real bug."""
    original = metis_inspired._Allocator.choose_labels

    def weakest(self: Any, amount: int, hops: int, dist: Any, budget: Budget) -> Any:
        best = original(self, amount, hops, dist, budget)
        return None if best is None else (best[0] - 1, best[1], best[2])

    monkeypatch.setattr(metis_inspired._Allocator, "choose_labels", weakest)
    diag = _diagnose(PARALLEL, PARALLEL_CASE, chunks=4)
    assert diag["unexplained_chunks"] == 4 and diag["classes"] == {"unattributed": 4}


def test_diagnostic_is_a_separate_pass_with_its_own_counters() -> None:
    """The diagnostic never touches a solve's counters or budget: a solve after it is the
    same as a solve without it, and its quotes come from its own caches."""
    before = _metis(REVISIT, REVISIT_CASE, label_hops=4)
    with metered_quotes(None) as meter:
        diag = _diagnose(REVISIT, REVISIT_CASE, label_hops=4)
    after = _metis(REVISIT, REVISIT_CASE, label_hops=4)
    assert before == after
    counters = diag["counters"]
    assert (
        meter.counted == counters["label_quotes_executed"] + counters["enumeration_quotes_executed"]
    )
    assert diag["pass"].startswith("S2 diagnostic")


# ------------------------------------------------------------ plumbing / identity


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"label_hops": None}, "label_hops"),
        ({"label_hops": 2}, "label_hops >= search.max_hops"),
        ({"label_hops": True}, "label_hops"),
        ({"label_hops": 0}, "label_hops"),
        ({"label_pruning": None}, "label_pruning"),
        ({"label_pruning": 1}, "label_pruning"),
        ({"label_pruning": "true"}, "label_pruning"),
        ({"chunks": 0}, "chunks"),
        ({"percent_step": 3}, "percent_step"),
    ],
)
def test_prepare_rejects_missing_or_invalid_settings(change: dict[str, Any], match: str) -> None:
    params = {**_params(), **change}
    params = {k: v for k, v in params.items() if v is not None}
    with pytest.raises(metis_inspired.MetisInspiredConfigError, match=match):
        metis_inspired.prepare(PREFIX, AlgorithmConfig(metis_inspired.NAME, params))


def test_solve_without_prepare_is_an_error() -> None:
    with pytest.raises(TypeError, match="prepare"):
        metis_inspired.solve(CASE, SolveContext(PREFIX, gross_only()), Budget())


def test_registration_identity_and_provenance() -> None:
    factory = ALGORITHMS[metis_inspired.NAME]
    assert factory is metis_inspired.FACTORY
    assert factory.capabilities == incremental_graph.FACTORY.capabilities
    assert factory.search_params == incremental_graph.FACTORY.search_params
    assert factory.graph_params == ("chunks", "label_hops", "label_pruning")
    assert metis_inspired.NAME not in BASE_STRATEGIES + OPTIMIZED_STRATEGIES
    assert strategy_group(metis_inspired.NAME) == "custom"
    import benchmark.worker  # noqa: F401 -- registers the worker boundary's mappingproxy pickling

    shipped = pickle.loads(pickle.dumps(factory))  # as the runner ships it to a spawned worker
    assert shipped.solve is metis_inspired.solve and shipped.prepare is metis_inspired.prepare
    provenance = json.loads(json.dumps(dict(factory.provenance or {})))
    assert "NOT Jupiter Metis" in provenance["identity"]
    assert "upstream" not in provenance  # never labeled as a scoped upstream port
    assert provenance["sources"]["J1"]["sha256"].startswith("825598a5")
    assert provenance["sources"]["J2"]["sha256"].startswith("d7ab87af")
    assert "c5b56369155b4beddef8de4df64e63dd0118662c" in provenance["contract"]
    # The reference solvers are untouched: no new parameters reach them.
    assert incremental_graph.FACTORY.graph_params == ("chunks",)


def test_solves_are_per_case_state_free_in_any_order() -> None:
    bundle = fixture("mantle_mixed")
    prepared = _prepared(bundle, max_hops=2, label_hops=3, chunks=20)
    pools_before = dict(bundle.pools)
    context = SolveContext(bundle, gross_only(), prepared)
    cases = list(bundle.cases)
    forward = [metis_inspired.solve(c, context, Budget()) for c in cases]
    backward = [metis_inspired.solve(c, context, Budget()) for c in reversed(cases)][::-1]
    assert forward == backward
    assert dict(bundle.pools) == pools_before
    assert all(bundle.pools[k] is v for k, v in pools_before.items())


PROFILE: dict[str, Any] = {
    "schema_version": 2,
    "algorithms": ["incremental_graph", "metis_inspired"],
    "objective": {"mode": "gross_only"},
    "search": {"max_hops": 2, "max_splits": 4, "percent_step": 5},
    "graph": {"chunks": 10, "label_hops": 3, "label_pruning": True},
    "budget": {"time_limit_seconds": 60, "max_quotes": 50000, "max_candidates": None},
    "measurement": {"warmup": 0, "repeats": 1, "seed": 1, "order": "fixed", "memory_pass": False},
    "worker": {"start_method": "spawn", "scope": "algorithm", "prepare_time_limit_seconds": 30},
}


def _with_graph(**graph: Any) -> dict[str, Any]:
    return {**PROFILE, "graph": {k: v for k, v in graph.items() if v is not None}}


def test_profile_requires_explicit_label_settings_handed_only_to_metis() -> None:
    with pytest.raises(ProfileError, match=r"graph\.label_hops, graph\.label_pruning"):
        parse_profile(_with_graph(chunks=10), "p.yaml")
    with pytest.raises(ProfileError, match=r"graph\.label_pruning"):
        parse_profile(_with_graph(chunks=10, label_hops=3), "p.yaml")
    with pytest.raises(ProfileError, match=r"label_hops: must be >= search\.max_hops"):
        parse_profile(_with_graph(chunks=10, label_hops=1, label_pruning=True), "p.yaml")
    with pytest.raises(ProfileError, match=r"label_pruning: expected a bool"):
        parse_profile(_with_graph(chunks=10, label_hops=3, label_pruning="yes"), "p.yaml")
    with pytest.raises(ProfileError, match=r"label_hops: expected an integer"):
        parse_profile(_with_graph(chunks=10, label_hops=True, label_pruning=True), "p.yaml")
    with pytest.raises(ProfileError, match=r"unknown key"):
        parse_profile(_with_graph(chunks=10, label_hops=3, label_pruning=True, k=2), "p.yaml")
    no_search = {k: v for k, v in _with_graph(chunks=1, label_hops=3).items() if k != "search"}
    with pytest.raises(ProfileError, match=r"needs search\.max_hops"):
        parse_profile({**no_search, "algorithms": ["direct"]}, "p.yaml")
    profile = parse_profile(PROFILE, "p.yaml")
    assert profile.algorithm_config(incremental_graph.FACTORY).params == {
        "max_hops": 2,
        "max_splits": 4,
        "percent_step": 5,
        "chunks": 10,
    }
    assert profile.algorithm_config(metis_inspired.FACTORY).params == {
        "max_hops": 2,
        "max_splits": 4,
        "percent_step": 5,
        "chunks": 10,
        "label_hops": 3,
        "label_pruning": True,
    }
    resolved = profile.resolved()
    assert resolved["graph"] == {"chunks": 10, "label_hops": 3, "label_pruning": True}
    config = resolved["algorithm_config"]
    assert config["metis_inspired"]["params"]["label_pruning"] is True
    assert "NOT Jupiter Metis" in config["metis_inspired"]["provenance"]["identity"]
    assert "provenance" not in config["incremental_graph"]
    # A profile without the keys resolves exactly as before.
    plain = parse_profile(
        {**PROFILE, "algorithms": ["incremental_graph"], "graph": {"chunks": 10}}, "p"
    )
    assert plain.resolved()["graph"] == {"chunks": 10}


ARMS = {
    "a0": ("incremental_graph", None),
    "m3": ("metis_inspired", {"label_hops": 3, "label_pruning": True}),
    "m4": ("metis_inspired", {"label_hops": 4, "label_pruning": True}),
    "m4_off": ("metis_inspired", {"label_hops": 4, "label_pruning": False}),
}


@pytest.mark.parametrize("arm", sorted(ARMS))
def test_challenge_arm_profiles_are_full_gross_plus_the_registered_keys(arm: str) -> None:
    algorithm, label = ARMS[arm]
    path = REPO / "config" / "metis_challenge" / f"{arm}.yaml"
    profile = load_profile(path)
    assert profile.algorithms == (algorithm,)
    raw = yaml.safe_load(path.read_text())
    full = yaml.safe_load((REPO / "config" / "full_gross.yaml").read_text())
    graph = raw.pop("graph")
    assert graph == {"chunks": 50, **(label or {})}
    assert raw.pop("algorithms") == [algorithm]
    assert full.pop("algorithms") and full.pop("graph") == {"chunks": 50}
    assert raw == full  # every other value is full_gross.yaml's
    assert profile.budget == Budget(time_limit_seconds=900, max_quotes=300000, max_candidates=None)


# ------------------------------------------------------------ runner / worker / report


def test_isolated_worker_run_order_check_and_report(tmp_path: Path) -> None:
    from benchmark.results import load_case_records, load_manifest
    from benchmark.runner import compare_runs, run_experiment
    from report import aggregate as agg
    from report.render import render_report

    bundle = fixture("mantle_mixed")
    profile = parse_profile(PROFILE, "p.yaml")
    fixed = run_experiment(bundle, profile, results_dir=tmp_path / "f", replay_command="cmd")
    reverse_profile = dataclasses.replace(
        profile, measurement=dataclasses.replace(profile.measurement, order="reverse")
    )
    reverse = run_experiment(
        bundle, reverse_profile, results_dir=tmp_path / "r", replay_command="cmd"
    )
    assert fixed.complete and reverse.complete
    assert compare_runs(fixed.run_dir, reverse.run_dir) == []  # no state leak across cases
    records = {(r["algorithm"], r["case_id"]): r for r in load_case_records(fixed.run_dir)}
    for case in bundle.cases:
        got = records[("metis_inspired", case.case_id)]
        ref = records[("incremental_graph", case.case_id)]
        assert got["status"] == "ok"  # the runner's own independent re-evaluation
        assert got["quotes"]["counted"] == got["search"]["quotes_executed"]
        assert got["search"]["label_hops"] == 3 and got["search"]["chunk_search"] == "label"
        assert got["search"]["accounting_matches_evaluation"] is True
        assert int(got["score"]) >= int(got["search"]["path_split_score"])
        assert ref["search"]["max_hops"] == got["search"]["max_hops"] == 2
    manifest = load_manifest(fixed.run_dir)
    config = manifest.resolved_profile["algorithm_config"]["metis_inspired"]
    assert config["params"]["label_hops"] == 3 and "sources" in config["provenance"]
    run = agg.load_run(fixed.run_dir)
    label = agg.algorithm_label(run, "metis_inspired")
    assert label["kind"] == "experimental" and "NOT Jupiter Metis" in label["title"]
    html = render_report(manifest, tmp_path / "report").html.read_text()
    assert "NOT Jupiter Metis" in html


# ------------------------------------------------------------ tools/metis_challenge.py


def _tool() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "metis_challenge", REPO / "tools" / "metis_challenge.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_challenge_tool_diagnoses_and_checks_x7_write_once(tmp_path: Path) -> None:
    tool = _tool()
    out = tmp_path / "s2.json"
    argv = [
        "diagnose",
        "--bundle", str(FIXTURES / "mantle_mixed"),
        "--profile", str(REPO / "config" / "metis_challenge" / "m3.yaml"),
        "--reference-profile", str(REPO / "config" / "metis_challenge" / "a0.yaml"),
        "--case", "usdt_wmnt_evidence_split", "--x7", "--output", str(out),
    ]  # fmt: skip
    assert tool.main(argv) == 0
    document = json.loads(out.read_text())
    assert (
        document["pass"].startswith("WHI-1449 S2 diagnostic")
        and "NOT a measurement" in document["pass"]
    )
    assert document["verdicts"] == {"identical": 1} and document["x7_identical"] is True
    assert document["label_hops"] == 3 and document["allowed_classes"] == ["quote_failure", "tie"]
    row = document["cases"][0]
    assert row["results_from"] == "diagnostic in-process re-solves"
    assert row["diagnostic"]["classes"] == {"agree": 50} and row["quotes_within_reference"] is True
    assert tool.main(argv) == 2  # never overwrites an artifact


def test_challenge_tool_verdicts_never_clamp_an_unexplained_difference() -> None:
    tool = _tool()

    def side(trace: str, quotes: int, truncated: str | None = None) -> dict[str, Any]:
        return {
            "signature": {"status": "ok", "score": "1", "trace": [[trace]]},
            "search": {"quotes_executed": quotes, "truncated_by": truncated},
        }

    def diag(divergent: int, unexplained: int = 0) -> dict[str, Any]:
        return {
            "incremental_allocation": [],
            "incremental_chunk_sequence": [],
            "divergent_chunks": divergent,
            "unexplained_chunks": unexplained,
            "quote_subset_violations": 0,
        }

    assert tool._verdict(diag(0), side("p", 5), side("p", 9), False)["verdict"] == "identical"
    assert tool._verdict(diag(1), side("p", 5), side("q", 9), False)["verdict"] == "attributed"
    assert tool._verdict(diag(0), side("p", 5), side("q", 9), False)["verdict"] == "unexplained"
    assert tool._verdict(diag(1, 1), side("p", 5), side("q", 9), False)["verdict"] == "unexplained"
    excess = tool._verdict(diag(0), side("p", 10), side("p", 9), False)
    assert excess["verdict"] == "unexplained" and excess["quotes_within_reference"] is False
    truncated = tool._verdict(diag(0), side("p", 5, "max_quotes"), side("q", 9), False)
    assert truncated["verdict"] == "budget_order"

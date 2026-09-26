"""`incremental_graph` (docs/DESIGN.md §2.6, WHI-1441).

Expected values are independent of the code under test:

- **Hand-derived CPMM outputs** (`_v2_out`): the Solidity `getAmountOut` formula
  (generalized to `fee_bps`), applied to the returned plan's own per-pool inputs, and an
  explicit **sequential** simulation that updates reserves after every chunk swap
  (`_sequential_chunks`) -- the "repeated fee-bearing swaps" execution the merged plan
  deliberately is not.
- **Independent replay**: every returned plan is replayed again by a plain, memo-free
  `evaluate()` (the runner's own evaluation path).
- **Baselines** are the registered `path_split` / `single_path` / `direct_split`
  solvers run on their own.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from benchmark.objective import ObjectiveContext, gross_only
from benchmark.profile import ProfileError, parse_profile
from pools.quote import metered_quotes
from routing.algorithms import incremental_graph, path_split
from routing.algorithms.base import AlgorithmConfig, Budget, SolveContext, SolveResult, SolveStatus
from routing.algorithms.incremental_graph import (
    PoolFlow,
    chunk_amounts,
    creates_cycle,
    merged_plan,
    topology,
)
from routing.algorithms.registry import ALGORITHMS
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from routing.search import Edge
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


def _params(
    max_hops: int = 3, max_splits: int = 4, step: int = 5, chunks: int = 20
) -> dict[str, int]:
    return {"max_hops": max_hops, "max_splits": max_splits, "percent_step": step, "chunks": chunks}


def _solve(
    bundle: SnapshotBundle,
    case: Case,
    *,
    budget: Budget | None = None,
    objective: ObjectiveContext | None = None,
    sink: list[Any] | None = None,
    **params: int,
) -> SolveResult:
    prepared = incremental_graph.prepare(
        bundle, AlgorithmConfig(incremental_graph.NAME, _params(**params))
    )
    context = SolveContext(
        bundle=bundle,
        objective=objective or gross_only(),
        prepared=prepared,
        candidate_sink=None if sink is None else sink.append,
    )
    return incremental_graph.solve(case, context, budget or Budget())


def _path_split(bundle: SnapshotBundle, case: Case, **params: int) -> SolveResult:
    p = _params(**params)
    del p["chunks"]
    prepared = path_split.prepare(bundle, AlgorithmConfig(path_split.NAME, p))
    return path_split.solve(case, SolveContext(bundle, gross_only(), prepared), Budget())


def _replayed(bundle: SnapshotBundle, case: Case, result: SolveResult) -> Evaluation:
    """Plain, memo-free, unmetered replay of the returned plan."""
    assert result.plan is not None and result.evaluation is not None
    ev = evaluate(bundle, case, result.plan, gross_only())
    assert ev.status is EvalStatus.OK, ev.error
    assert ev.gross_output == result.evaluation.gross_output
    return ev


def _conserved(case: Case, ev: Evaluation) -> None:
    """Full fill: the request fund is fully consumed, nothing but target-token terminal
    balances remain, and those sum to the gross output."""
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


# ------------------------------------------------------------ independent oracles


def _v2_out(pool: ConstantProductPoolState, token_in: str, amount_in: int) -> int:
    r_in, r_out = pool.reserves_for(token_in)
    fee_in = amount_in * (10_000 - pool.fee_bps)
    return fee_in * r_out // (r_in * 10_000 + fee_in)


def _merged_oracle(bundle: SnapshotBundle, ev: Evaluation) -> int:
    """Gross output of the plan's per-pool inputs, each pool called once on its
    original reserves (hand formula), summed over the target-token pools."""
    total = 0
    for t in ev.trace:
        pool = bundle.pools[t.pool_id]
        assert isinstance(pool, ConstantProductPoolState)
        assert t.amount_out == _v2_out(pool, t.token_in, t.amount_in)
    for t in ev.trace:
        if t.token_out == ev.trace[-1].token_out:
            total += t.amount_out
    return total


def _sequential_chunks(
    bundle: SnapshotBundle, chunks: list[tuple[tuple[str, ...], str, int]]
) -> tuple[int, int]:
    """Explicit simulations of the same chunk choices executed as *separate* swaps:
    (sequential: every chunk swaps on reserves updated by all earlier chunks,
    isolated: every chunk swaps on the original reserves -- duplicated liquidity)."""
    reserves = {pid: (p.reserve0, p.reserve1) for pid, p in bundle.pools.items()}  # type: ignore[union-attr]
    seq_total = iso_total = 0
    for chain, token, amount in chunks:
        seq, iso, tok = amount, amount, token
        for pid in chain:
            pool = bundle.pools[pid]
            assert isinstance(pool, ConstantProductPoolState)
            r0, r1 = reserves[pid]
            live = ConstantProductPoolState(
                pool_id=pid,
                token0=pool.token0,
                token1=pool.token1,
                reserve0=r0,
                reserve1=r1,
                fee_bps=pool.fee_bps,
                source_key=None,
            )
            out = _v2_out(live, tok, seq)
            r_in, r_out = live.reserves_for(tok)
            new = live.with_reserves_for(tok, r_in + seq, r_out - out)
            reserves[pid] = (new.reserve0, new.reserve1)
            iso = _v2_out(pool, tok, iso)
            seq, tok = out, pool.other_token(tok)
        seq_total += seq
        iso_total += iso
    return seq_total, iso_total


# ------------------------------------------------------------ fixtures

# Shared prefix: one deep A-C pool feeding two shallow C-B pools. path_split can use the
# hub only once, so it cannot split across cb1 and cb2; the graph splits at C.
PREFIX = _bundle(
    _cp("hub", "A", "C", 10**12, 10**12, fee_bps=1),
    _cp("cb1", "C", "B", 10**9, 10**9),
    _cp("cb2", "C", "B", 10**9, 10**9),
    _cp("ab", "A", "B", 10**8, 10**8),
)

# Shared suffix: two shallow A-C pools merging into one deep C-B pool.
SUFFIX = _bundle(
    _cp("ac1", "A", "C", 10**9, 10**9),
    _cp("ac2", "A", "C", 10**9, 10**9),
    _cp("hub", "C", "B", 10**12, 10**12, fee_bps=1),
)

# One fee-bearing pool: chunking it must not change what one merged swap yields.
SINGLE = _bundle(_cp("ab", "A", "B", 10**9, 10**9, fee_bps=30))

CASE = Case("c", "A", "B", 4 * 10**8)


# ------------------------------------------------------------ acceptance behavior


def test_shared_prefix_beats_path_split_with_a_valid_replayed_plan() -> None:
    result = _solve(PREFIX, CASE)
    baseline = _path_split(PREFIX, CASE)
    assert result.status is SolveStatus.OK and baseline.status is SolveStatus.OK
    assert result.score is not None and baseline.score is not None
    assert result.score > baseline.score
    # path_split cannot use both C-B pools behind the one hub.
    assert baseline.plan is not None
    used = [s.pool_id for s in baseline.plan.steps]
    assert not {"cb1", "cb2"} <= set(used) or "hub" not in used
    stats = result.search_stats
    assert stats["chosen_source"] == "incremental_graph"
    assert stats["topology"] == "shared_pool"
    assert stats["incremental_shared_pools"] == ["hub"]
    assert stats["path_split_score"] == str(baseline.score)
    ev = _replayed(PREFIX, CASE, result)
    _conserved(CASE, ev)
    # One merged hub call whose output is split across both C-B pools at C.
    assert [t.pool_id for t in ev.trace].count("hub") == 1
    assert result.plan is not None
    hub_fund = next(s.output_fund_id for s in result.plan.steps if s.pool_id == "hub")
    consumers = {s.pool_id for s in result.plan.steps for i in s.inputs if i.fund_id == hub_fund}
    assert consumers == {"cb1", "cb2"}
    assert result.score == ev.gross_output == _merged_oracle(PREFIX, ev)


def test_shared_suffix_merges_funding_into_one_step() -> None:
    result = _solve(SUFFIX, CASE)
    baseline = _path_split(SUFFIX, CASE)
    assert result.score is not None and baseline.score is not None
    assert result.score > baseline.score
    assert result.search_stats["topology"] == "shared_pool"
    ev = _replayed(SUFFIX, CASE, result)
    _conserved(CASE, ev)
    assert result.plan is not None
    hub = [s for s in result.plan.steps if s.pool_id == "hub"]
    assert len(hub) == 1 and len(hub[0].inputs) == 2  # merged funding, one pool call
    assert ev.route_features["merge_steps"] == 1
    assert result.score == ev.gross_output == _merged_oracle(SUFFIX, ev)


@pytest.mark.parametrize("bundle", [PREFIX, SUFFIX, SINGLE], ids=["prefix", "suffix", "single"])
def test_naive_marginal_accounting_would_over_claim(bundle: SnapshotBundle) -> None:
    """Naive accounting over-claims: chunk quotes on the original state (duplicated
    initial liquidity) claim more than any execution, and the aggregate marginal
    accounting claims more than the same chunks reach as sequential fee-bearing swaps
    (so marginal accounting must not be executed as sequential steps). The returned
    merged plan reaches exactly its accounting, and the reported output is the
    evaluator's."""
    chunks = 4
    result = _solve(bundle, CASE, chunks=chunks)
    stats = result.search_stats
    assert stats["incremental_status"] == "ok"
    chains = [
        tuple(re.findall(r"-\[([^\]]+)\]->", a["path"])) for a in stats["incremental_allocation"]
    ]
    per_chunk = [chains[i] for i in stats["incremental_chunk_sequence"]]
    amounts = chunk_amounts(CASE.amount_in, chunks)
    sequential, isolated = _sequential_chunks(
        bundle, [(chain, "A", a) for chain, a in zip(per_chunk, amounts, strict=True)]
    )
    evaluated = int(stats["incremental_evaluated_gross"])
    assert int(stats["incremental_accounted_gross"]) == evaluated
    assert stats["accounting_matches_evaluation"] is True
    assert isolated > evaluated  # duplicated initial liquidity
    assert sequential < evaluated  # marginal accounting + sequential steps would over-claim
    # (For the single pool the merged plan ties single_path, which the tie keeps.)
    assert result.score == evaluated == _replayed(bundle, CASE, result).gross_output


def test_single_pool_chunking_equals_one_merged_swap() -> None:
    result = _solve(SINGLE, CASE, chunks=7)
    pool = SINGLE.pools["ab"]
    assert isinstance(pool, ConstantProductPoolState)
    assert result.score == _v2_out(pool, "A", CASE.amount_in)
    assert result.search_stats["incremental_evaluated_gross"] == str(result.score)
    assert result.search_stats["topology"] == "single_route"


def test_duplicated_liquidity_and_inconsistent_flows_are_rejected() -> None:
    """A plan built from accounting that double-counts the hub's initial liquidity asks
    for more than the hub actually produced: the evaluator rejects it. Flows that are
    not conserved are refused by the plan builder."""
    hub = PREFIX.pools["hub"]
    assert isinstance(hub, ConstantProductPoolState)
    half = CASE.amount_in // 2
    naive_hub_out = 2 * _v2_out(hub, "A", half)  # each half on the original reserves
    assert naive_hub_out > _v2_out(hub, "A", CASE.amount_in)
    x1 = naive_hub_out - 10  # more than the hub really produces
    flows = [
        PoolFlow(Edge("hub", "A", "C"), CASE.amount_in, naive_hub_out, 0),
        PoolFlow(Edge("cb1", "C", "B"), x1, 0, 1),
        PoolFlow(Edge("cb2", "C", "B"), naive_hub_out - x1, 0, 2),
    ]
    plan = merged_plan(CASE, flows)
    ev = evaluate(PREFIX, CASE, plan, gross_only())
    assert ev.status is EvalStatus.INVALID_PLAN
    assert ev.error is not None and "less than requested" in ev.error
    with pytest.raises(ValueError, match="not conserved"):
        merged_plan(CASE, [flows[0], PoolFlow(Edge("cb1", "C", "B"), 1, 0, 1)])
    with pytest.raises(ValueError, match="cycle"):
        merged_plan(
            CASE,
            [
                PoolFlow(Edge("hub", "A", "C"), 5, 5, 0),
                PoolFlow(Edge("cb1", "C", "B"), 5, 5, 1),
                PoolFlow(Edge("cb2", "B", "C"), 5, 5, 2),
            ],
        )


def test_explicit_sequential_plan_differs_from_the_merged_accounting() -> None:
    """The same allocation executed as sequential per-chunk steps on one pool (a valid
    plan: later uses see earlier updates) reproduces the explicit sequential simulation
    and falls short of the merged plan the algorithm returns and accounts for."""
    chunks = 4
    amounts = chunk_amounts(CASE.amount_in, chunks)
    steps = tuple(
        SwapStep(
            "ab",
            "A",
            "B",
            (FundInput(REQUEST_FUND_ID, ALL_REMAINING if n == chunks - 1 else a),),
            f"OUT{n}",
        )
        for n, a in enumerate(amounts)
    )
    seq_ev = evaluate(SINGLE, CASE, RoutePlan(steps), gross_only())
    sequential, _ = _sequential_chunks(SINGLE, [(("ab",), "A", a) for a in amounts])
    assert seq_ev.gross_output == sequential
    result = _solve(SINGLE, CASE, chunks=chunks)
    assert result.plan is not None and len(result.plan.steps) == 1
    assert result.score is not None and seq_ev.gross_output < result.score


@pytest.mark.parametrize(
    ("amount", "chunks"), [(10**8 + 7, 7), (3, 20), (7, 20), (19, 20), (4 * 10**8 + 1, 13)]
)
def test_nondivisible_and_tiny_inputs_fully_allocate(amount: int, chunks: int) -> None:
    sizes = chunk_amounts(amount, chunks)
    assert sum(sizes) == amount and len(sizes) == chunks and max(sizes) - min(sizes) <= 1
    case = Case("c", "A", "B", amount)
    result = _solve(PREFIX, case, chunks=chunks)
    assert result.status is SolveStatus.OK
    stats = result.search_stats
    assert stats["chunks_empty"] == sizes.count(0)
    assert stats["chunks_allocated"] + stats["chunks_carried"] == chunks - sizes.count(0)
    assert stats["chunks_allocated"] >= 1
    assert sum(int(a["amount_in"]) for a in stats["incremental_allocation"]) == amount
    assert stats["incremental_status"] == "ok"
    _conserved(case, _replayed(PREFIX, case, result))


def test_never_worse_than_the_retained_simpler_routes() -> None:
    for bundle, cases in (
        (fixture("cpmm_graph"), ["a_b_large", "a_b_small", "a_d_multi_hop"]),
        (fixture("mantle_mixed"), [c.case_id for c in fixture("mantle_mixed").cases]),
    ):
        for case_id in cases:
            case = bundle.case(case_id)
            result = _solve(bundle, case, max_hops=2)
            baseline = _path_split(bundle, case, max_hops=2)
            assert result.status is SolveStatus.OK, case_id
            assert result.score is not None and baseline.score is not None
            assert result.score >= baseline.score, case_id
            assert result.search_stats["path_split_score"] == str(baseline.score)
            ev = _replayed(bundle, case, result)
            _conserved(case, ev)


@pytest.mark.parametrize("case_id", [c.case_id for c in fixture("mantle_mixed").cases])
def test_real_mixed_family_accounting_matches_the_evaluator(case_id: str) -> None:
    """On real CL/LB/CPMM state the merged plan's replay reproduces the aggregate
    marginal accounting exactly (the flows telescope)."""
    bundle = fixture("mantle_mixed")
    case = bundle.case(case_id)
    result = _solve(bundle, case, max_hops=2, chunks=10)
    stats = result.search_stats
    assert stats["incremental_status"] == "ok"
    assert stats["accounting_matches_evaluation"] is True
    assert stats["incremental_accounted_gross"] == stats["incremental_evaluated_gross"]


def test_topology_classifies_capability_matched_and_expanded_results() -> None:
    a = (Edge("p1", "A", "C"), Edge("p2", "C", "B"))
    b = (Edge("p3", "A", "C"), Edge("p4", "C", "B"))
    c = (Edge("p1", "A", "C"), Edge("p4", "C", "B"))
    assert topology([a, a]) == "single_route"
    assert topology([a, b]) == "disjoint_split"
    assert topology([a, c]) == "shared_pool"
    assert ALGORITHMS["incremental_graph"].capabilities.to_dict() == {
        "multi_hop": True,
        "split": True,
        "shared_pools": True,
    }
    assert not ALGORITHMS["path_split"].capabilities.shared_pools


def test_chunk_paths_never_create_a_token_cycle() -> None:
    edges = {("A", "C"), ("C", "B")}
    assert not creates_cycle(edges, (Edge("x", "A", "C"),))
    assert creates_cycle(edges, (Edge("r", "C", "A"),))  # a pool used backwards
    assert creates_cycle({("A", "C"), ("C", "D")}, (Edge("x", "A", "D"), Edge("y", "D", "C")))
    assert not creates_cycle({("A", "C")}, (Edge("x", "A", "D"), Edge("y", "D", "C")))
    # A triangle where later chunks would want C -> D and D -> C.
    tri = _bundle(
        _cp("ac", "A", "C", 10**9, 10**9),
        _cp("ad", "A", "D", 10**9, 10**9),
        _cp("cd", "C", "D", 10**9, 10**9),
        _cp("cb", "C", "B", 10**9, 10**9),
        _cp("db", "D", "B", 10**9, 10**9),
    )
    result = _solve(tri, Case("c", "A", "B", 5 * 10**8), chunks=10)
    assert result.status is SolveStatus.OK and result.search_stats["incremental_status"] == "ok"
    _conserved(
        Case("c", "A", "B", 5 * 10**8), _replayed(tri, Case("c", "A", "B", 5 * 10**8), result)
    )


def test_solve_is_deterministic() -> None:
    bundle = fixture("mantle_mixed")
    case = bundle.case("usdt_wmnt_evidence_split")
    first, second = _solve(bundle, case, max_hops=2), _solve(bundle, case, max_hops=2)
    assert first.plan == second.plan and first.search_stats == second.search_stats
    assert _solve(PREFIX, CASE).search_stats == _solve(PREFIX, CASE).search_stats


# ------------------------------------------------------------ budgets / statuses


def test_quote_budget_stops_before_the_meter_and_is_visible() -> None:
    unbounded = _solve(PREFIX, CASE)
    ps_quotes = _path_split(PREFIX, CASE).search_stats["quotes_executed"]
    limit = ps_quotes + 3  # enough for path_split, not for the chunk allocation
    assert unbounded.search_stats["quotes_executed"] > limit
    with metered_quotes(limit) as meter:
        result = _solve(PREFIX, CASE, budget=Budget(max_quotes=limit))
    assert not meter.exceeded and meter.counted == result.search_stats["quotes_executed"] <= limit
    assert result.status is SolveStatus.OK
    assert result.search_stats["truncated_by"] == "max_quotes"
    assert result.search_stats["incremental_status"] == "truncated"
    assert result.search_stats["chosen_source"] != "incremental_graph"
    assert result.candidates_truncated > 0
    assert result.score is not None and unbounded.score is not None
    assert result.score < unbounded.score


def test_budget_truncation_without_a_route_is_timeout_never_no_route() -> None:
    bundle = fixture("cpmm_graph")
    result = _solve(bundle, bundle.case("a_d_multi_hop"), budget=Budget(max_quotes=1))
    assert result.status is SolveStatus.TIMEOUT
    assert result.error is not None and "not evidence of no_route" in result.error


def test_candidate_cap_is_declared_truncation() -> None:
    result = _solve(PREFIX, CASE, budget=Budget(max_candidates=1))
    assert result.status is SolveStatus.OK
    assert result.search_stats["truncated_by"] is not None
    assert result.search_stats["paths_truncated"] > 0
    assert result.candidates_truncated > 0


def test_quote_accounting_is_exact() -> None:
    with metered_quotes(None) as meter:
        result = _solve(PREFIX, CASE)
    assert result.search_stats["quotes_executed"] == meter.counted
    assert result.search_stats["quotes_memoized"] > 0


def test_unreachable_pair_is_no_route() -> None:
    bundle = _bundle(_cp("ab", "A", "B", 10**6, 10**6), _cp("cd", "C", "D", 10**6, 10**6))
    result = _solve(bundle, Case("c", "A", "D", 1000))
    assert result.status is SolveStatus.NO_ROUTE
    assert result.error is not None and "unreachable" in result.error
    assert result.search_stats["incremental_status"] == "no_paths"


USDT = "0x201eba5cc46d216ce6dc03f6a759e8e766e956ae"
WMNT = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"
UNI_USDT_WMNT = "0x4cdfc22bf05209de87ee564746dc7e5174631d2b"


def test_only_incomplete_candidates_is_incomplete_snapshot_not_no_route() -> None:
    bundle = _bundle(fixture("mantle_mixed").pools[UNI_USDT_WMNT])
    result = _solve(bundle, Case("huge", USDT, WMNT, 10**18))
    assert result.status is SolveStatus.INCOMPLETE_SNAPSHOT
    assert result.search_stats["incremental_status"].endswith("no_admissible_path")


@dataclass(frozen=True)
class PerCallCost(ObjectiveContext):
    per_call: int = 0

    def score(self, evaluation: Evaluation) -> int:
        return evaluation.gross_output - self.per_call * evaluation.route_features["pool_calls"]


def test_per_call_cost_keeps_the_simpler_route() -> None:
    gross = _solve(PREFIX, CASE)
    assert gross.search_stats["chosen_source"] == "incremental_graph"
    net = _solve(PREFIX, CASE, objective=PerCallCost(mode="synthetic_fixed_cost", per_call=10**8))
    assert net.status is SolveStatus.OK and net.evaluation is not None
    assert net.search_stats["chosen_source"] != "incremental_graph"
    assert (
        net.score
        == net.evaluation.gross_output - 10**8 * net.evaluation.route_features["pool_calls"]
    )


def test_best_plans_are_published_in_improving_order() -> None:
    published: list[Any] = []
    result = _solve(PREFIX, CASE, sink=published)
    assert published and published[-1] == result.plan
    scores = [evaluate(PREFIX, CASE, p, gross_only()).gross_output for p in published]
    assert scores == sorted(scores) and len(set(scores)) == len(scores)


# ------------------------------------------------------------ plumbing


@pytest.mark.parametrize(
    "params",
    [
        {"max_hops": 3, "max_splits": 4, "percent_step": 5},
        {"max_hops": 3, "max_splits": 4, "percent_step": 5, "chunks": 0},
        {"max_hops": 3, "max_splits": 4, "percent_step": 5, "chunks": True},
        {"max_splits": 4, "percent_step": 5, "chunks": 20},
        {"max_hops": 3, "max_splits": 4, "percent_step": 3, "chunks": 20},
    ],
)
def test_prepare_rejects_missing_or_invalid_settings(params: dict[str, Any]) -> None:
    with pytest.raises(incremental_graph.IncrementalGraphConfigError):
        incremental_graph.prepare(PREFIX, AlgorithmConfig(incremental_graph.NAME, params))


def test_solve_without_prepare_is_an_error() -> None:
    with pytest.raises(TypeError, match="prepare"):
        incremental_graph.solve(CASE, SolveContext(PREFIX, gross_only()), Budget())


PROFILE: dict[str, Any] = {
    "schema_version": 2,
    "algorithms": ["path_split", "incremental_graph"],
    "objective": {"mode": "gross_only"},
    "search": {"max_hops": 2, "max_splits": 4, "percent_step": 5},
    "budget": {"time_limit_seconds": 60, "max_quotes": 50000, "max_candidates": None},
    "measurement": {"warmup": 0, "repeats": 1, "seed": 1, "order": "fixed", "memory_pass": False},
    "worker": {"start_method": "spawn", "scope": "algorithm", "prepare_time_limit_seconds": 30},
}


def test_profile_must_declare_graph_chunks() -> None:
    with pytest.raises(ProfileError, match=r"graph\.chunks"):
        parse_profile(PROFILE, "p.yaml")
    with pytest.raises(ProfileError, match=r"unknown key"):
        parse_profile({**PROFILE, "graph": {"chunks": 5, "depth": 2}}, "p.yaml")
    with pytest.raises(ProfileError, match=r"graph\.chunks"):
        parse_profile({**PROFILE, "graph": {"chunks": 0}}, "p.yaml")
    profile = parse_profile({**PROFILE, "graph": {"chunks": 20}}, "p.yaml")
    assert profile.algorithm_config(incremental_graph.FACTORY).params == {
        "max_hops": 2,
        "max_splits": 4,
        "percent_step": 5,
        "chunks": 20,
    }
    assert profile.algorithm_config(path_split.FACTORY).params == PROFILE["search"]
    assert profile.resolved()["graph"] == {"chunks": 20}


def test_isolated_runner_records_the_incremental_search(tmp_path: Path) -> None:
    from benchmark.results import load_case_records
    from benchmark.runner import run_experiment

    bundle = fixture("mantle_mixed")
    profile = parse_profile({**PROFILE, "graph": {"chunks": 10}}, "p.yaml")
    manifest = run_experiment(bundle, profile, results_dir=tmp_path, replay_command="cmd")
    assert manifest.complete
    records = {(r["algorithm"], r["case_id"]): r for r in load_case_records(manifest.run_dir)}
    for case in bundle.cases:
        ig = records[("incremental_graph", case.case_id)]
        assert ig["status"] == "ok"  # the runner's own independent re-evaluation
        assert ig["quotes"]["counted"] == ig["search"]["quotes_executed"]
        assert int(ig["score"]) >= int(records[("path_split", case.case_id)]["score"])


# ------------------------------------------------------------ WHI-1507 / L05 exact reuse
#
# `solve(..., graph_reuse=True)` is an explicit, default-off experiment. Its contract: the
# same plan, evaluation, status/error and search counters as the reference loop, except
# the physical `quotes_memoized` (fewer cache lookups) and its own `graph_reuse` counters.

PHYSICAL = ("quotes_memoized", "graph_reuse")


def _run(
    bundle: SnapshotBundle,
    case: Case,
    *,
    reuse: bool,
    budget: Budget | None = None,
    objective: ObjectiveContext | None = None,
    **params: int,
) -> tuple[SolveResult, int, list[Any]]:
    prepared = incremental_graph.prepare(
        bundle, AlgorithmConfig(incremental_graph.NAME, _params(**params))
    )
    published: list[Any] = []
    context = SolveContext(
        bundle, objective or gross_only(), prepared, candidate_sink=published.append
    )
    with metered_quotes(None) as meter:
        result = incremental_graph.solve(case, context, budget or Budget(), graph_reuse=reuse)
    return result, meter.counted, published


def _differential(bundle: SnapshotBundle, case: Case, **kw: Any) -> dict[str, int]:
    """Reference vs reuse: everything equal but the physical fields; returns the reuse
    counters."""
    ref, ref_quotes, ref_published = _run(bundle, case, reuse=False, **kw)
    got, got_quotes, got_published = _run(bundle, case, reuse=True, **kw)
    assert "graph_reuse" not in ref.search_stats
    for field in dataclasses.fields(SolveResult):
        if field.name != "search_stats":
            assert getattr(got, field.name) == getattr(ref, field.name), field.name

    def strip(stats: Mapping[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in stats.items() if k not in PHYSICAL}

    assert strip(got.search_stats) == strip(ref.search_stats)
    assert got_quotes == ref_quotes == ref.search_stats["quotes_executed"]
    assert got_published == ref_published
    assert got.search_stats["quotes_memoized"] <= ref.search_stats["quotes_memoized"]
    stats: dict[str, int] = got.search_stats["graph_reuse"]
    s = ref.search_stats
    scores = stats["scores_reused"] + stats["scores_recomputed"]
    admissions = stats["cycle_decisions_reused"] + stats["cycle_checks_executed"]
    assert stats["score_entries_peak"] <= 2 * s["paths_enumerated"]  # two amounts kept
    if s["incremental_status"] == "truncated":  # the aborted chunk is not in paths_scored
        assert scores > s["paths_scored"] and admissions > s["paths_scored"]
    else:
        assert scores == s["paths_scored"]
        assert admissions == s["paths_scored"] + s["paths_rejected_cycle"]
    return stats


def _random_bundle(seed: int) -> tuple[SnapshotBundle, list[Case]]:
    """A small random CPMM graph (parallel pools, reverse directions, cycles through
    intermediate tokens) and cases from dust to large, divisible or not."""
    import random

    rng = random.Random(seed)
    tokens = ["A", "B", "C", "D", "E"][: rng.randint(3, 5)]
    pools = []
    for n in range(rng.randint(4, 9)):
        t0, t1 = rng.sample(tokens, 2)
        r0, r1 = (10 ** rng.randint(5, 12) * rng.randint(1, 9) for _ in range(2))
        pools.append(_cp(f"p{n}", t0, t1, r0, r1, fee_bps=rng.choice([0, 1, 5, 30, 100])))
    amounts = [rng.randint(1, 60), rng.randint(10**5, 10**7), 10 ** rng.randint(6, 11) + 7]
    return _bundle(*pools), [Case(f"c{a}", "A", "B", a) for a in amounts]


def _random_differentials(**kw: Any) -> dict[str, int]:
    totals: dict[str, int] = {}
    for seed in range(40):
        bundle, cases = _random_bundle(seed)
        for case in cases:
            for hops, chunks in ((2, 1), (3, 7), (4, 20)):
                stats = _differential(bundle, case, max_hops=hops, chunks=chunks, **kw)
                for k, v in stats.items():
                    totals[k] = totals.get(k, 0) + v
    return totals


def test_reuse_matches_the_reference_on_random_graphs() -> None:
    totals = _random_differentials()
    # The generated cases exercise every reuse rule, not just the easy path.
    assert totals["scores_reused"] > 0 and totals["score_invalidations"] > 0
    assert totals["cycle_decisions_reused"] > 0 and totals["closure_edges_added"] > 0
    assert totals["score_amounts_evicted"] > 0
    rejected = carried = 0
    for seed in range(40):
        bundle, cases = _random_bundle(seed)
        for case in cases:
            s = _solve(bundle, case, max_hops=4, chunks=20).search_stats
            rejected += s["paths_rejected_cycle"]
            carried += s["chunks_carried"]
    assert rejected > 0 and carried > 0


@pytest.mark.parametrize(
    "budget",
    [Budget(max_candidates=1), Budget(max_candidates=3), Budget(max_quotes=40)],
    ids=["cand1", "cand3", "quotes40"],
)
def test_reuse_preserves_declared_budgets(budget: Budget) -> None:
    """Budgets are logical (candidates scored, quotes executed): the same truncation."""
    for seed in range(12):
        bundle, cases = _random_bundle(seed)
        for case in cases:
            _differential(bundle, case, budget=budget, max_hops=4, chunks=20)
    ref, _, _ = _run(PREFIX, CASE, reuse=False, budget=Budget(max_quotes=60))
    assert ref.search_stats["incremental_status"] == "truncated"
    _differential(PREFIX, CASE, budget=Budget(max_quotes=60))


@pytest.mark.parametrize("case_id", [c.case_id for c in fixture("mantle_mixed").cases])
@pytest.mark.parametrize("hops", [2, 3])
def test_reuse_matches_the_reference_on_real_mixed_state(case_id: str, hops: int) -> None:
    bundle = fixture("mantle_mixed")
    stats = _differential(bundle, bundle.case(case_id), max_hops=hops, chunks=37)
    assert stats["scores_reused"] > 0


def test_reuse_preserves_fixtures_fallback_and_statuses() -> None:
    for bundle in (PREFIX, SUFFIX, SINGLE):
        for amount in (3, 19, 10**8 + 7, 4 * 10**8):
            _differential(bundle, Case("c", "A", "B", amount), chunks=13)
    # The simpler route wins under a per-call cost: same fallback choice.
    cost = PerCallCost(mode="synthetic_fixed_cost", per_call=10**8)
    _differential(PREFIX, CASE, objective=cost)
    unreachable = _bundle(_cp("ab", "A", "B", 10**6, 10**6), _cp("cd", "C", "D", 10**6, 10**6))
    got, _, _ = _run(unreachable, Case("c", "A", "D", 1000), reuse=True)
    assert got.status is SolveStatus.NO_ROUTE
    assert got.search_stats["graph_reuse"]["scores_reused"] == 0
    only_incomplete = _bundle(fixture("mantle_mixed").pools[UNI_USDT_WMNT])
    _differential(only_incomplete, Case("huge", USDT, WMNT, 10**18))


def test_reuse_counts_shared_failing_prefixes_where_the_reference_does() -> None:
    """Two paths share an incomplete-state first hop: the reference counts the failure
    once per chunk, at the first path reaching it, with that path's label -- a kept
    failure must be counted the same way, not once, and not once per path."""
    bundle = _bundle(
        fixture("mantle_mixed").pools[UNI_USDT_WMNT],
        _cp("w1", WMNT, "B", 10**24, 10**24),
        _cp("w2", WMNT, "B", 10**24, 10**24),
        _cp("ub", USDT, "B", 10**12, 10**12),
    )
    case = Case("c", USDT, "B", 10**18)
    stats = _differential(bundle, case, chunks=5)
    assert stats["scores_reused"] > 0
    ref = _solve(bundle, case, chunks=5).search_stats
    assert ref["marginal_failures"] == {"incomplete_snapshot": 5}
    assert "-[w1]->" in ref["incomplete_example"]


def test_reuse_is_explicit_and_per_solve() -> None:
    """Default off everywhere ordinary callers reach; no state survives a solve; the
    prepared object and bundle are not changed by a reuse solve."""
    import inspect

    assert inspect.signature(incremental_graph.solve).parameters["graph_reuse"].default is False
    assert ALGORITHMS["incremental_graph"].solve is incremental_graph.solve
    assert "graph_reuse" not in _solve(PREFIX, CASE).search_stats
    bundle = fixture("mantle_mixed")
    prepared = incremental_graph.prepare(
        bundle, AlgorithmConfig(incremental_graph.NAME, _params(max_hops=2, chunks=20))
    )
    before = (dict(bundle.pools), prepared.chunks, prepared.path_split)
    context = SolveContext(bundle, gross_only(), prepared)
    cases = list(bundle.cases)
    forward = [incremental_graph.solve(c, context, Budget(), graph_reuse=True) for c in cases]
    backward = [
        incremental_graph.solve(c, context, Budget(), graph_reuse=True) for c in reversed(cases)
    ][::-1]
    fresh = [incremental_graph.solve(c, context, Budget()) for c in cases]
    for f, b, r in zip(forward, backward, fresh, strict=True):
        assert f == b and f.plan == r.plan and f.score == r.score
    assert (dict(bundle.pools), prepared.chunks, prepared.path_split) == before


# Admission: the closure check against `creates_cycle` on growing random DAGs.


def test_closure_admission_equals_creates_cycle_and_is_atomic() -> None:
    import random

    from routing.algorithms.incremental_graph import _ExactReuse

    # Each edge alone is safe against the committed C -> A, together they close a cycle.
    committed = {("C", "A")}
    path = (Edge("x", "A", "B"), Edge("y", "B", "C"))
    for edge in path:
        assert not creates_cycle(committed, (edge,))
    assert creates_cycle(committed, path)
    reuse = _ExactReuse([path])
    reuse.commit((Edge("z", "C", "A"),), [("C", "A")])
    snapshot = ({t: set(r) for t, r in reuse.reach.items()}, reuse.version)
    assert reuse.cyclic(0, path) and reuse.cyclic(0, path)  # second: the kept rejection
    assert ({t: set(r) for t, r in reuse.reach.items()}, reuse.version) == snapshot

    rng = random.Random(1507)
    tokens = "ABCDEFG"
    checked = rejected = 0
    for _ in range(200):
        paths = []
        for _ in range(30):
            hops = rng.sample(tokens, rng.randint(2, 5))
            pairs = zip(hops, hops[1:], strict=False)
            paths.append(tuple(Edge(f"p{a}{b}", a, b) for a, b in pairs))
        reuse, edges = _ExactReuse(paths), set[tuple[str, str]]()
        for _ in range(60):
            j = rng.randrange(len(paths))
            expected = creates_cycle(edges, paths[j])
            assert reuse.cyclic(j, paths[j]) is expected
            checked, rejected = checked + 1, rejected + expected
            if not expected and rng.random() < 0.3:
                new = [(e.token_in, e.token_out) for e in paths[j]]
                reuse.commit(paths[j], [t for t in new if t not in edges])
                edges.update(new)
    assert 0 < rejected < checked


# Sensitivity: a reuse that dropped one invalidation rule would be caught above.


def _mutant_differs(monkeypatch: pytest.MonkeyPatch, mutate: Any) -> bool:
    from routing.algorithms.incremental_graph import _ExactReuse

    original = _ExactReuse.__init__

    def init(self: Any, paths: Any) -> None:
        original(self, paths)
        mutate(self)

    monkeypatch.setattr(_ExactReuse, "__init__", init)
    for seed in range(40):
        bundle, cases = _random_bundle(seed)
        for case in cases:
            try:
                _differential(bundle, case, max_hops=4, chunks=20)
            except (AssertionError, ValueError):
                return True
    return False


def test_differentials_catch_dropped_invalidation_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_pool_invalidation(r: Any) -> None:
        r.pool_paths = {p: [] for p in r.pool_paths}

    def amount_blind(r: Any) -> None:
        entries_for = r.entries_for
        r.entries_for = lambda amount: entries_for(0)

    def admitted_forever(r: Any) -> None:
        r.commit = _keep_version(r.commit, r)

    for mutant in (no_pool_invalidation, amount_blind, admitted_forever):
        with monkeypatch.context() as m:
            assert _mutant_differs(m, mutant), mutant.__name__


def _keep_version(commit: Any, reuse: Any) -> Any:
    def wrapped(path: Any, new_edges: Any) -> None:
        version = reuse.version
        commit(path, new_edges)
        reuse.version = version  # admitted paths are never re-checked

    return wrapped

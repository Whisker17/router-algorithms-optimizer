"""`single_path` (docs/DESIGN.md §2.6, WHI-1438) and the bounded traversal in
`routing.search`.

Expected values are independent of the code under test:

- **Brute-force enumeration** (`_brute_paths`): every ordered choice of pools
  (`itertools.product`) filtered to token-simple `token_in -> token_out` chains -- a
  different algorithm from the DFS in `routing.search`.
- **Hand-derived CPMM outputs** (`_v2_out`): the Solidity
  `UniswapV2Library.getAmountOut` formula (generalized to `fee_bps`) chained along a
  path; headline vectors on `tests/fixtures/routing/cpmm_graph` are pinned as literals.
- **Real mixed-family states** (`tests/fixtures/routing/mantle_mixed`): the solver's
  choice is checked against a plain, memo-free `evaluate()` of every enumerated path.
"""

from __future__ import annotations

import itertools
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from benchmark.objective import gross_only, synthetic_fixed_cost
from benchmark.profile import ProfileError, parse_profile
from pools.quote import metered_quotes
from routing.algorithms import single_path
from routing.algorithms.base import (
    AlgorithmConfig,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
)
from routing.algorithms.registry import ALGORITHMS
from routing.evaluator import EvalStatus, evaluate
from routing.plan import REQUEST_FUND_ID
from routing.search import build_graph_index, enumerate_paths, min_hops, path_plan
from snapshot.bundle import load_bundle
from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "routing"
BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)


@cache
def fixture(name: str) -> SnapshotBundle:
    return load_bundle(FIXTURES / name)


def _bundle(*pools: ConstantProductPoolState, cases: tuple[Case, ...] = ()) -> SnapshotBundle:
    return SnapshotBundle(
        bundle_id="t",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools={p.pool_id: p for p in pools},
        cases=cases,
        bundle_hash="deadbeef",
        source_path="<test>",
    )


def _cp(pool_id: str, t0: str, t1: str, r0: int, r1: int, fee_bps: int = 30) -> Any:
    return ConstantProductPoolState(
        pool_id=pool_id, token0=t0, token1=t1, reserve0=r0, reserve1=r1, fee_bps=fee_bps
    )


def _solve(
    bundle: SnapshotBundle,
    case: Case,
    *,
    max_hops: int = 3,
    budget: Budget | None = None,
    objective: Any = None,
    sink: list[Any] | None = None,
) -> SolveResult:
    prepared = single_path.prepare(
        bundle, AlgorithmConfig(single_path.NAME, {"max_hops": max_hops})
    )
    context = SolveContext(
        bundle=bundle,
        objective=objective or gross_only(),
        prepared=prepared,
        candidate_sink=None if sink is None else sink.append,
    )
    return single_path.solve(case, context, budget or Budget())


def _pools(result: SolveResult) -> list[str]:
    assert result.plan is not None
    return [s.pool_id for s in result.plan.steps]


# ------------------------------------------------------------ independent oracles


def _v2_out(pool: ConstantProductPoolState, token_in: str, amount_in: int) -> int:
    r_in, r_out = (
        (pool.reserve0, pool.reserve1)
        if token_in == pool.token0
        else (pool.reserve1, pool.reserve0)
    )
    fee_in = amount_in * (10_000 - pool.fee_bps)
    return fee_in * r_out // (r_in * 10_000 + fee_in)


def _brute_paths(
    bundle: SnapshotBundle, token_in: str, token_out: str, max_hops: int
) -> set[tuple[str, ...]]:
    """Every token-simple pool chain from token_in to token_out, by trying every
    ordered pool tuple -- no graph search."""
    found: set[tuple[str, ...]] = set()
    pools = list(bundle.pools.values())
    for hops in range(1, max_hops + 1):
        for chain in itertools.product(pools, repeat=hops):
            token, seen, ok = token_in, [token_in], True
            for pool in chain:
                if token not in (pool.token0, pool.token1):
                    ok = False
                    break
                token = pool.other_token(token)
                seen.append(token)
            if ok and token == token_out and len(set(seen)) == len(seen):
                found.add(tuple(p.pool_id for p in chain))
    return found


def _brute_best(
    bundle: SnapshotBundle, case: Case, max_hops: int
) -> tuple[int, set[tuple[str, ...]]] | None:
    """Best hand-computed CPMM output over every bounded simple path, and every
    path achieving it (a valid route needs a positive output at every step)."""
    best: int | None = None
    winners: set[tuple[str, ...]] = set()
    for chain in _brute_paths(bundle, case.token_in, case.token_out, max_hops):
        token, amount, ok = case.token_in, case.amount_in, True
        for pool_id in chain:
            pool = bundle.pools[pool_id]
            assert isinstance(pool, ConstantProductPoolState)
            r_in = pool.reserve0 if token == pool.token0 else pool.reserve1
            if r_in == 0:
                ok = False
                break
            amount = _v2_out(pool, token, amount)
            if amount <= 0:
                ok = False
                break
            token = pool.other_token(token)
        if not ok:
            continue
        if best is None or amount > best:
            best, winners = amount, {chain}
        elif amount == best:
            winners.add(chain)
    return None if best is None else (best, winners)


# ------------------------------------------------------------ routing.search


def test_enumeration_is_hop_major_cycle_free_and_matches_brute_force() -> None:
    bundle = fixture("cpmm_graph")
    index = build_graph_index(bundle)
    for token_in, token_out in itertools.permutations(["TKA", "TKB", "TKC", "TKD"], 2):
        for max_hops in (1, 2, 3, 4):
            paths = list(enumerate_paths(index, token_in, token_out, max_hops))
            chains = [tuple(e.pool_id for e in p) for p in paths]
            assert len(chains) == len(set(chains))
            assert set(chains) == _brute_paths(bundle, token_in, token_out, max_hops)
            assert [len(p) for p in paths] == sorted(len(p) for p in paths)  # hop-major
            for path in paths:
                tokens = [path[0].token_in] + [e.token_out for e in path]
                assert len(set(tokens)) == len(tokens)  # never revisits a token
                assert tokens[0] == token_in and tokens[-1] == token_out


def test_enumeration_on_a_cyclic_graph_never_produces_a_cycle() -> None:
    # A triangle with parallel edges plus a tail: plenty of cycles to fall into.
    bundle = _bundle(
        _cp("ab1", "A", "B", 10**6, 10**6),
        _cp("ab2", "A", "B", 10**6, 10**6),
        _cp("bc", "B", "C", 10**6, 10**6),
        _cp("ca", "C", "A", 10**6, 10**6),
        _cp("cd", "C", "D", 10**6, 10**6),
        _cp("bd", "B", "D", 10**6, 10**6),
    )
    index = build_graph_index(bundle)
    paths = list(enumerate_paths(index, "A", "D", 6))
    assert {tuple(e.pool_id for e in p) for p in paths} == _brute_paths(bundle, "A", "D", 6)
    assert max(len(p) for p in paths) == 3  # a simple path on 4 tokens has <= 3 hops
    for path in paths:
        plan = path_plan(Case("c", "A", "D", 1000), path)
        evaluation = evaluate(bundle, Case("c", "A", "D", 1000), plan, gross_only())
        assert evaluation.status is EvalStatus.OK  # the evaluator's cycle check agrees


def test_min_hops_distinguishes_unreachable_from_beyond_the_cap() -> None:
    index = build_graph_index(fixture("cpmm_graph"))
    assert min_hops(index, "TKA", "TKD") == 2
    assert min_hops(index, "TKA", "TKB") == 1
    assert min_hops(index, "TKA", "TKZ") is None


def test_path_plan_chains_every_output_into_the_next_step() -> None:
    index = build_graph_index(fixture("cpmm_graph"))
    case = Case("c", "TKA", "TKB", 10)
    path = next(p for p in enumerate_paths(index, "TKA", "TKB", 3) if len(p) == 3)
    plan = path_plan(case, path)
    assert plan.steps[0].inputs[0].fund_id == REQUEST_FUND_ID
    for prev, step in itertools.pairwise(plan.steps):
        assert step.inputs[0].fund_id == prev.output_fund_id
        assert step.token_in == prev.token_out
    with pytest.raises(ValueError, match="does not connect"):
        path_plan(Case("c", "TKB", "TKA", 10), path)


# ------------------------------------------------------------ route choice


def test_indirect_route_beats_the_direct_pool_when_it_pays() -> None:
    bundle = fixture("cpmm_graph")
    case = bundle.case("a_b_large")
    result = _solve(bundle, case)
    assert result.status is SolveStatus.OK
    assert _pools(result) == ["ac_1", "cb_1"]
    ac_1, cb_1, ab_1 = (bundle.pools[p] for p in ("ac_1", "cb_1", "ab_1"))
    assert isinstance(ac_1, ConstantProductPoolState)
    assert isinstance(cb_1, ConstantProductPoolState)
    assert isinstance(ab_1, ConstantProductPoolState)
    mid = _v2_out(ac_1, "TKA", 150_000_000)
    assert result.score == _v2_out(cb_1, "TKC", mid) == 114_812_154
    assert _v2_out(ab_1, "TKA", 150_000_000) == 85_567_157  # the direct pool it beat
    assert result.search_stats["best_hops"] == 2
    assert result.search_stats["direct_candidates"] == 2


def test_direct_route_is_retained_when_it_is_best() -> None:
    # Deep direct pool; the two-hop detour pays two fees through shallow pools.
    case = Case("c", "A", "B", 50_000)
    bundle = _bundle(
        _cp("ab", "A", "B", 10**9, 10**9),
        _cp("ac", "A", "C", 10**6, 10**6),
        _cp("cb", "C", "B", 10**6, 10**6),
    )
    result = _solve(bundle, case)
    assert result.status is SolveStatus.OK
    assert _pools(result) == ["ab"]
    assert result.score == _v2_out(bundle.pools["ab"], "A", 50_000) == 49_847  # type: ignore[arg-type]
    detour = _v2_out(bundle.pools["cb"], "C", _v2_out(bundle.pools["ac"], "A", 50_000))  # type: ignore[arg-type]
    assert detour < result.score
    assert result.candidates_considered == 2 and result.candidates_truncated == 0


def test_dust_keeps_the_only_valid_direct_route() -> None:
    bundle = fixture("cpmm_graph")
    result = _solve(bundle, bundle.case("a_b_dust"))
    assert result.status is SolveStatus.OK
    assert _pools(result) == ["ab_1"] and result.score == 2
    # Two-hop legs round the first quote to zero TKC; their extensions are pruned.
    assert result.search_stats["paths_pruned"] > 0


def test_pair_without_a_direct_pool_now_routes() -> None:
    bundle = fixture("cpmm_graph")
    case = bundle.case("a_d_multi_hop")
    assert bundle.pools_for_pair("TKA", "TKD") == ()
    result = _solve(bundle, case)
    assert result.status is SolveStatus.OK
    assert _pools(result) == ["ac_1", "cb_2", "db_2"]
    assert result.score == 20_568


@pytest.mark.parametrize("case_id", ["a_b_large", "a_b_small", "a_d_multi_hop", "a_b_dust"])
@pytest.mark.parametrize("max_hops", [1, 2, 3])
def test_matches_exhaustive_bounded_enumeration(case_id: str, max_hops: int) -> None:
    bundle = fixture("cpmm_graph")
    case = bundle.case(case_id)
    expected = _brute_best(bundle, case, max_hops)
    result = _solve(bundle, case, max_hops=max_hops)
    if expected is None:
        assert result.status is SolveStatus.NO_ROUTE
        return
    best, winners = expected
    assert result.status is SolveStatus.OK
    assert result.score == best
    assert tuple(_pools(result)) in winners
    assert result.candidates_truncated == 0
    assert result.search_stats["paths_enumerated"] == len(
        _brute_paths(bundle, case.token_in, case.token_out, max_hops)
    )


def test_ties_keep_the_first_candidate_in_hop_major_order() -> None:
    bundle = _bundle(_cp("ab2", "A", "B", 10**6, 10**6), _cp("ab1", "A", "B", 10**6, 10**6))
    result = _solve(bundle, Case("c", "A", "B", 1000))
    assert _pools(result) == ["ab2"]  # bundle insertion order, not pool-id order


def test_objective_scores_the_choice() -> None:
    bundle = fixture("cpmm_graph")
    case = bundle.case("a_b_large")
    result = _solve(bundle, case, objective=synthetic_fixed_cost(1_000))
    assert result.status is SolveStatus.OK
    assert result.evaluation is not None
    assert result.score == result.evaluation.gross_output - 1_000 == 114_811_154
    assert result.evaluation.objective_label.startswith("SYNTHETIC")


# ------------------------------------------------------------ plan / evaluation agreement


@pytest.mark.parametrize("case_id", ["a_b_large", "a_b_small", "a_d_multi_hop"])
def test_evaluated_output_agrees_with_the_path_and_integer_intermediates(case_id: str) -> None:
    bundle = fixture("cpmm_graph")
    case = bundle.case(case_id)
    result = _solve(bundle, case)
    assert result.plan is not None and result.evaluation is not None
    fresh = evaluate(bundle, case, result.plan, gross_only())
    assert fresh.status is EvalStatus.OK
    assert fresh.gross_output == result.evaluation.gross_output == result.score
    assert fresh.trace == result.evaluation.trace
    amount, token = case.amount_in, case.token_in
    for step, trace in zip(result.plan.steps, fresh.trace, strict=True):
        pool = bundle.pools[step.pool_id]
        assert isinstance(pool, ConstantProductPoolState)
        assert (trace.pool_id, trace.token_in, trace.amount_in) == (step.pool_id, token, amount)
        amount, token = _v2_out(pool, token, amount), step.token_out
        assert trace.amount_out == amount
    assert token == case.token_out and fresh.gross_output == amount
    assert fresh.route_features["hops"] == len(result.plan.steps)
    assert fresh.route_features["split_funds"] == 0 and fresh.route_features["merge_steps"] == 0


def test_mixed_family_choice_matches_a_memo_free_exhaustive_evaluation() -> None:
    bundle = fixture("mantle_mixed")
    index = build_graph_index(bundle)
    for case in bundle.cases:
        best: int | None = None
        for path in enumerate_paths(index, case.token_in, case.token_out, 3):
            evaluation = evaluate(bundle, case, path_plan(case, path), gross_only())
            if evaluation.status is EvalStatus.OK and (
                best is None or evaluation.gross_output > best
            ):
                best = evaluation.gross_output
        result = _solve(bundle, case)
        assert result.status is SolveStatus.OK, (case.case_id, result.error)
        assert result.score == best, case.case_id
        assert result.plan is not None
        assert evaluate(bundle, case, result.plan, gross_only()).gross_output == best


# ------------------------------------------------------------ distinct failure outcomes


def test_unreachable_pair_is_no_route() -> None:
    bundle = _bundle(_cp("ab", "A", "B", 10**6, 10**6), _cp("cd", "C", "D", 10**6, 10**6))
    result = _solve(bundle, Case("c", "A", "D", 1000))
    assert result.status is SolveStatus.NO_ROUTE
    assert result.plan is None and result.candidates_truncated == 0
    assert result.search_stats["min_hops_unbounded"] is None
    assert result.error is not None and "unreachable" in result.error


def test_hop_cap_is_a_distinct_no_route() -> None:
    bundle = fixture("cpmm_graph")
    result = _solve(bundle, bundle.case("a_d_multi_hop"), max_hops=1)
    assert result.status is SolveStatus.NO_ROUTE
    assert result.search_stats["min_hops_unbounded"] == 2
    assert result.error is not None and "hop cap" in result.error
    assert result.search_stats["quotes_executed"] == 0


def test_every_candidate_failing_within_the_cap_is_no_route() -> None:
    bundle = _bundle(
        _cp("ab_dry", "A", "B", 0, 10**6),
        _cp("ac_dry", "A", "C", 0, 10**6),
        _cp("cb", "C", "B", 10**6, 10**6),
    )
    result = _solve(bundle, Case("c", "A", "B", 1000))
    assert result.status is SolveStatus.NO_ROUTE
    assert result.candidates_truncated == 0
    assert result.error is not None and "failed" in result.error
    assert result.search_stats["failed_candidates"] == {"insufficient_liquidity": 2}


def _dry_direct_bundle() -> SnapshotBundle:
    # The direct pool is dry; the only valid route is the 2-hop detour.
    return _bundle(
        _cp("ab_dry", "A", "B", 0, 10**6),
        _cp("ac", "A", "C", 10**6, 10**6),
        _cp("cb", "C", "B", 10**6, 10**6),
    )


def test_candidate_cap_truncation_is_never_no_route() -> None:
    result = _solve(
        _dry_direct_bundle(), Case("c", "A", "B", 1000), budget=Budget(max_candidates=1)
    )
    assert result.status is SolveStatus.TIMEOUT
    assert result.candidates_truncated == 1
    assert result.search_stats["truncated_by"] == "max_candidates"
    assert result.error is not None and "not evidence of no_route" in result.error
    untruncated = _solve(_dry_direct_bundle(), Case("c", "A", "B", 1000))
    assert untruncated.status is SolveStatus.OK and _pools(untruncated) == ["ac", "cb"]


def test_quote_budget_truncation_stops_before_the_hard_meter() -> None:
    bundle, case = _dry_direct_bundle(), Case("c", "A", "B", 1000)
    with metered_quotes(2) as meter:  # the dry pool costs 1; the detour would need 2
        result = _solve(bundle, case, budget=Budget(max_quotes=2))
    assert not meter.exceeded and meter.counted == 1
    assert result.status is SolveStatus.TIMEOUT
    assert result.search_stats["truncated_by"] == "max_quotes"
    assert result.search_stats["quotes_executed"] == meter.counted
    with metered_quotes(3) as meter:
        result = _solve(bundle, case, budget=Budget(max_quotes=3))
    assert result.status is SolveStatus.OK and meter.counted == 3 and not meter.exceeded


def test_truncation_with_a_found_route_is_ok_and_declared() -> None:
    bundle = fixture("cpmm_graph")
    result = _solve(bundle, bundle.case("a_b_large"), budget=Budget(max_candidates=2))
    assert result.status is SolveStatus.OK
    assert _pools(result) == ["ab_1"]  # direct candidates come first and are kept
    assert result.candidates_truncated == 8
    assert result.search_stats["truncated_by"] == "max_candidates"


USDT = "0x201eba5cc46d216ce6dc03f6a759e8e766e956ae"
WMNT = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"
UNI_USDT_WMNT = "0x4cdfc22bf05209de87ee564746dc7e5174631d2b"


def test_only_incomplete_candidates_is_incomplete_snapshot_not_no_route() -> None:
    mixed = fixture("mantle_mixed")
    bundle = _bundle(mixed.pools[UNI_USDT_WMNT])  # type: ignore[arg-type]
    result = _solve(bundle, Case("huge", USDT, WMNT, 10**18))  # beyond the collected ticks
    assert result.status is SolveStatus.INCOMPLETE_SNAPSHOT
    assert result.plan is None
    assert result.search_stats["paths_incomplete"] == 1
    assert result.error is not None and "uncollected" in result.error


def test_incomplete_detours_are_excluded_and_disclosed_not_fatal() -> None:
    bundle = fixture("mantle_mixed")
    case = bundle.case("usdt_wmnt_evidence_split")
    result = _solve(bundle, case)
    assert result.status is SolveStatus.OK
    stats = result.search_stats
    assert stats["paths_incomplete"] > 0
    assert "outside the collected range" in stats["incomplete_example"]
    # Every excluded candidate is accounted for: nothing is silently dropped.
    assert stats["paths_evaluated"] + stats["paths_pruned"] == stats["paths_enumerated"]


# ------------------------------------------------------------ quote accounting


def test_quote_accounting_is_exact_and_prefix_quotes_are_memoized() -> None:
    bundle = fixture("cpmm_graph")
    case = bundle.case("a_d_multi_hop")
    with metered_quotes(None) as meter:
        result = _solve(bundle, case)
    stats = result.search_stats
    assert stats["quotes_executed"] == meter.counted
    naive = sum(len(p) for p in enumerate_paths(build_graph_index(bundle), "TKA", "TKD", 3))
    assert stats["quotes_memoized"] > 0
    assert meter.counted < naive


def test_every_new_best_is_published_as_a_candidate() -> None:
    bundle = fixture("cpmm_graph")
    published: list[Any] = []
    result = _solve(bundle, bundle.case("a_b_large"), sink=published)
    assert published and published[-1] == result.plan


def test_solve_is_repeatable_with_one_prepared_index() -> None:
    bundle = fixture("cpmm_graph")
    prepared = single_path.prepare(bundle, AlgorithmConfig(single_path.NAME, {"max_hops": 3}))
    context = SolveContext(bundle=bundle, objective=gross_only(), prepared=prepared)
    runs = [single_path.solve(c, context, Budget()) for c in bundle.cases * 2]
    assert runs[: len(bundle.cases)] == runs[len(bundle.cases) :]


# ------------------------------------------------------------ registration / config


def test_registered_with_multi_hop_no_split_capability() -> None:
    factory = ALGORITHMS["single_path"]
    assert factory is single_path.FACTORY
    assert factory.capabilities.multi_hop and not factory.capabilities.split
    assert factory.search_params == ("max_hops",)
    assert factory.prepare is single_path.prepare


@pytest.mark.parametrize("value", [None, 0, -1, True, 2.0, "3"])
def test_prepare_rejects_a_missing_or_invalid_hop_bound(value: Any) -> None:
    params = {} if value is None else {"max_hops": value}
    with pytest.raises(single_path.SinglePathConfigError):
        single_path.prepare(fixture("cpmm_graph"), AlgorithmConfig(single_path.NAME, params))


def test_solve_without_prepare_is_an_error() -> None:
    bundle = fixture("cpmm_graph")
    with pytest.raises(TypeError, match="prepare"):
        single_path.solve(
            bundle.case("a_b_large"), SolveContext(bundle=bundle, objective=gross_only()), Budget()
        )


PROFILE: dict[str, Any] = {
    "schema_version": 2,
    "algorithms": ["direct", "single_path"],
    "objective": {"mode": "gross_only"},
    "budget": {"time_limit_seconds": 5, "max_quotes": 1000, "max_candidates": None},
    "measurement": {"warmup": 0, "repeats": 1, "seed": 1, "order": "fixed", "memory_pass": False},
    "worker": {"start_method": "spawn", "scope": "algorithm", "prepare_time_limit_seconds": 30},
}


def test_profile_must_declare_the_hop_bound() -> None:
    with pytest.raises(ProfileError, match=r"search\.max_hops"):
        parse_profile(PROFILE, "p.yaml")
    profile = parse_profile({**PROFILE, "search": {"max_hops": 3}}, "p.yaml")
    assert profile.search == {"max_hops": 3}
    assert profile.algorithm_config(single_path.FACTORY).params == {"max_hops": 3}
    assert profile.algorithm_config(ALGORITHMS["direct"]).params == {}
    resolved = profile.resolved()
    assert resolved["search"] == {"max_hops": 3}
    assert resolved["algorithm_config"]["single_path"] == {
        "capabilities": {"multi_hop": True, "split": False, "shared_pools": False},
        "params": {"max_hops": 3},
    }


@pytest.mark.parametrize(
    ("search", "match"),
    [({"max_hops": 0}, "max_hops"), ({"max_hops": "3"}, "max_hops"), ({"max_hop": 3}, "unknown")],
)
def test_profile_rejects_invalid_search_values(search: Any, match: str) -> None:
    with pytest.raises(ProfileError, match=match):
        parse_profile({**PROFILE, "search": search}, "p.yaml")


# ------------------------------------------------------------ measured runner


def test_isolated_runner_charges_prepare_and_records_the_search(tmp_path: Path) -> None:
    from benchmark.results import load_case_records
    from benchmark.runner import run_experiment

    bundle = fixture("cpmm_graph")
    profile = parse_profile(
        {**PROFILE, "search": {"max_hops": 3}, "budget": {**PROFILE["budget"], "max_quotes": 30}},
        "p.yaml",
    )
    manifest = run_experiment(bundle, profile, results_dir=tmp_path, replay_command="cmd")
    assert manifest.complete
    events = {e["algorithm"]: e for e in manifest.prepare_events}
    assert events["single_path"]["status"] == "ok"
    assert events["single_path"]["prepare_seconds"] is not None
    assert manifest.resolved_profile["algorithm_config"]["single_path"]["params"] == {"max_hops": 3}
    records = {(r["algorithm"], r["case_id"]): r for r in load_case_records(manifest.run_dir)}
    direct_md = records[("direct", "a_d_multi_hop")]
    routed = records[("single_path", "a_d_multi_hop")]
    assert direct_md["status"] == "no_route"
    assert routed["status"] == "ok"  # the runner's own independent evaluation
    assert routed["score"] == "20568"
    assert routed["search"]["best_hops"] == 3
    assert routed["quotes"]["counted"] == routed["search"]["quotes_executed"] <= 30
    large = records[("single_path", "a_b_large")]
    assert large["score"] == "114812154"
    assert int(large["score"]) > int(records[("direct", "a_b_large")]["score"])

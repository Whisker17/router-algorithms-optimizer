"""`uni_sor_fast` (WHI-1508 / L06): the opt-in candidate-shortlist variant of the SOR port.

What is checked here, against `uni_sor_port` as the same-scope reference and against the
independent evaluator -- never against the variant's own claims:

- it is a separately named, opt-in seventh identity: the six reference IDs, their
  factories and every checked-in profile are unchanged, and the new `shortlist.*` profile
  settings have no defaults and are strictly validated;
- a shortlist that retains every route reproduces the reference exactly (a vacuous
  restriction), while a real restriction can lose output: the adversarial thin-pool case
  below shows an actual, asserted loss, and the multi-amount probe that avoids it;
- enumeration statuses, the charged deterministic full-table fallback, budget timeouts
  and `incomplete_snapshot` / `no_route` after the fallback are the reference's own --
  a shortlist miss is never reported as `no_route`;
- quotes executed never exceed the reference's, every phase is charged, the worker meter
  agrees, and every returned plan is protocol-exact and replays independently.

CPMM expectations use the Solidity `getAmountOut` formula computed here.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from benchmark.objective import gross_only
from benchmark.profile import ProfileError, load_profile, parse_profile
from pools.cl_math import get_sqrt_ratio_at_tick
from pools.quote import metered_quotes
from routing.algorithms import uni_sor_fast as fast
from routing.algorithms import uni_sor_port as sor
from routing.algorithms.base import AlgorithmConfig, Budget, SolveContext, SolveResult, SolveStatus
from routing.algorithms.registry import ALGORITHMS
from routing.evaluator import EvalStatus, evaluate
from snapshot.bundle import load_bundle
from snapshot.models import (
    BlockRef,
    Case,
    ConcentratedPoolState,
    ConstantProductPoolState,
    PoolState,
    SnapshotBundle,
    TickInfo,
)

REPO = Path(__file__).resolve().parents[2]
BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)
SEARCH = {"max_hops": 2, "max_splits": 4, "percent_step": 5}
REFERENCE_IDS = (
    "direct",
    "single_path",
    "direct_split",
    "path_split",
    "incremental_graph",
    "uni_sor_port",
)


@cache
def _load(rel: str) -> SnapshotBundle:
    return load_bundle(REPO / rel)


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


def _cp(pool_id: str, t0: str, t1: str, r0: int, r1: int) -> ConstantProductPoolState:
    return ConstantProductPoolState(pool_id, t0, t1, r0, r1, fee_bps=30)


def _out(x: int, r_in: int, r_out: int) -> int:
    fee_in = x * 9970
    return fee_in * r_out // (r_in * 10000 + fee_in)


def _cl(pool_id: str, liquidity: int) -> ConcentratedPoolState:
    """A Uniswap v3 pool A/B at tick 0 with one position on [-600, 600) and bitmap words
    -1..0 collected: a swap that leaves the band needs uncollected state."""
    spacing, lower, upper = 10, -600, 600
    bitmap: dict[int, int] = {}
    for t in (lower, upper):
        c = t // spacing
        bitmap[c >> 8] = bitmap.get(c >> 8, 0) | (1 << (c & 0xFF))
    return ConcentratedPoolState(
        pool_id=pool_id,
        source_key="uniswap_v3",
        token0="A",
        token1="B",
        fee=3000,
        tick_spacing=spacing,
        sqrt_price_x96=get_sqrt_ratio_at_tick(0),
        tick=0,
        liquidity=liquidity,
        fee_protocol=0,
        fee_growth_global0_x128=0,
        fee_growth_global1_x128=0,
        protocol_fees0=0,
        protocol_fees1=0,
        bitmap_word_range=(-1, 0),
        tick_bitmap=bitmap,
        ticks={
            lower: TickInfo(liquidity, liquidity, 0, 0),
            upper: TickInfo(liquidity, -liquidity, 0, 0),
        },
    )


def _params(
    probes: list[int], k: int, d: int, search: dict[str, int] | None = None
) -> dict[str, Any]:
    return {
        **(search or SEARCH),
        "probe_percents": probes,
        "routes_per_probe": k,
        "direct_routes": d,
    }


def _ref(
    bundle: SnapshotBundle,
    case: Case,
    search: dict[str, int] | None = None,
    budget: Budget | None = None,
) -> SolveResult:
    prepared = sor.prepare(bundle, AlgorithmConfig(sor.NAME, search or SEARCH))
    return sor.solve(case, SolveContext(bundle, gross_only(), prepared), budget or Budget())


def _fast(
    bundle: SnapshotBundle, case: Case, params: dict[str, Any], budget: Budget | None = None
) -> SolveResult:
    prepared = fast.prepare(bundle, AlgorithmConfig(fast.NAME, params))
    return fast.solve(case, SolveContext(bundle, gross_only(), prepared), budget or Budget())


def _independently_valid(bundle: SnapshotBundle, case: Case, res: SolveResult) -> None:
    """The plan replays on a fresh evaluator to exactly the solver's own evaluation,
    allocates the whole input and leaves no residual."""
    assert res.plan is not None and res.evaluation is not None
    ev = evaluate(bundle, case, res.plan, gross_only())
    assert ev.status is EvalStatus.OK, ev.error
    assert ev == res.evaluation
    assert not any(ev.residuals.values())
    assert sum(int(a) for a in res.search_stats["allocation"]) == case.amount_in
    assert res.search_stats["evaluated_gross"] == str(ev.gross_output)


# ------------------------------------------------------------- identity / registry


def test_opt_in_seventh_identity_leaves_the_six_references_untouched() -> None:
    assert tuple(ALGORITHMS)[:6] == REFERENCE_IDS
    assert ALGORITHMS["uni_sor_port"] is sor.FACTORY and ALGORITHMS["uni_sor_fast"] is fast.FACTORY
    for name in REFERENCE_IDS:
        assert ALGORITHMS[name].shortlist_params == ()
    factory = ALGORITHMS["uni_sor_fast"]
    assert factory.shortlist_params == ("probe_percents", "routes_per_probe", "direct_routes")
    assert factory.search_params == sor.SEARCH_PARAMS
    assert factory.capabilities == sor.CAPABILITIES
    assert factory.provenance is not None
    assert factory.provenance["experimental"] and factory.provenance["opt_in"]
    assert factory.provenance["reference"] == "uni_sor_port"
    assert "not upstream candidate selection" in factory.provenance["candidate_provider"]


@pytest.mark.parametrize("path", sorted((REPO / "config").glob("*.yaml")), ids=lambda p: p.name)
def test_no_checked_in_profile_selects_the_variant(path: Path) -> None:
    raw = path.read_text()
    if "schema_version" not in raw or "algorithms:" not in raw:
        pytest.skip("not a run profile")
    profile = load_profile(path)
    assert "uni_sor_fast" not in profile.algorithms
    resolved = profile.resolved()
    assert "shortlist" not in resolved and profile.shortlist == {}
    assert all(
        set(cfg["params"]) <= {"max_hops", "max_splits", "percent_step", "chunks"}
        for cfg in resolved["algorithm_config"].values()
    )


# ------------------------------------------------------------- profile settings

PROFILE: dict[str, Any] = {
    "schema_version": 2,
    "algorithms": ["direct", "uni_sor_port", "uni_sor_fast"],
    "objective": {"mode": "gross_only"},
    "search": SEARCH,
    "shortlist": {"probe_percents": [100, 5], "routes_per_probe": 4, "direct_routes": 2},
    "budget": {"time_limit_seconds": 60, "max_quotes": 50000, "max_candidates": None},
    "measurement": {"warmup": 0, "repeats": 1, "seed": 1, "order": "fixed", "memory_pass": False},
    "worker": {"start_method": "spawn", "scope": "algorithm", "prepare_time_limit_seconds": 60},
}


def _with(**changes: Any) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(json.dumps(PROFILE))
    for key, value in changes.items():
        if value is None:
            doc.pop(key)
        else:
            doc[key] = value
    return doc


def test_profile_hands_the_validated_settings_only_to_the_declaring_factory() -> None:
    profile = parse_profile(PROFILE, "p.yaml")
    config = profile.algorithm_config(fast.FACTORY)
    assert dict(config.params) == {
        **SEARCH,
        "probe_percents": (5, 100),
        "routes_per_probe": 4,
        "direct_routes": 2,
    }
    assert set(profile.algorithm_config(sor.FACTORY).params) == set(SEARCH)
    assert profile.algorithm_config(ALGORITHMS["direct"]).params == {}
    resolved = profile.resolved()
    assert resolved["shortlist"] == {"probe_percents": [5, 100], "routes_per_probe": 4,
                                     "direct_routes": 2}  # fmt: skip
    assert resolved["algorithm_config"]["uni_sor_fast"]["params"]["probe_percents"] == [5, 100]
    assert resolved["algorithm_config"]["uni_sor_fast"]["provenance"]["experimental"] is True
    json.dumps(resolved)


@pytest.mark.parametrize(
    ("shortlist", "match"),
    [
        (None, "shortlist.probe_percents"),
        ({"routes_per_probe": 4, "direct_routes": 0}, "shortlist.probe_percents"),
        ({"probe_percents": [100], "routes_per_probe": 4}, "shortlist.direct_routes"),
        ({"probe_percents": [100], "routes_per_probe": 4, "direct_routes": 0, "tvl": 1}, "unknown"),
        ({"probe_percents": [100], "routes_per_probe": 0, "direct_routes": 0}, "routes_per_probe"),
        (
            {"probe_percents": [100], "routes_per_probe": True, "direct_routes": 0},
            "routes_per_probe",
        ),
        ({"probe_percents": [100], "routes_per_probe": 2, "direct_routes": -1}, "direct_routes"),
        ({"probe_percents": [100], "routes_per_probe": 2, "direct_routes": 1.0}, "direct_routes"),
        ({"probe_percents": [], "routes_per_probe": 2, "direct_routes": 0}, "non-empty"),
        ({"probe_percents": 100, "routes_per_probe": 2, "direct_routes": 0}, "non-empty"),
        ({"probe_percents": [5, 5, 100], "routes_per_probe": 2, "direct_routes": 0}, "duplicate"),
        ({"probe_percents": [7, 100], "routes_per_probe": 2, "direct_routes": 0}, "grid"),
        ({"probe_percents": [100, 105], "routes_per_probe": 2, "direct_routes": 0}, "grid"),
        ({"probe_percents": [0, 100], "routes_per_probe": 2, "direct_routes": 0}, "grid"),
        ({"probe_percents": [True, 100], "routes_per_probe": 2, "direct_routes": 0}, "grid"),
        ({"probe_percents": [5, 25], "routes_per_probe": 2, "direct_routes": 0}, "include 100"),
    ],
)
def test_profile_rejects_missing_or_invalid_shortlist_settings(
    shortlist: dict[str, Any] | None, match: str
) -> None:
    with pytest.raises(ProfileError, match=match):
        parse_profile(_with(shortlist=shortlist), "p.yaml")


def test_shortlist_grid_needs_the_declared_percent_step() -> None:
    doc = _with(algorithms=["direct"], search={"max_hops": 2})
    with pytest.raises(ProfileError, match="percent_step"):
        parse_profile(doc, "p.yaml")
    # Declared but unused by the selected algorithms: validated, never handed over.
    ok = parse_profile(_with(algorithms=["direct", "uni_sor_port"]), "p.yaml")
    assert set(ok.algorithm_config(sor.FACTORY).params) == set(SEARCH)


@pytest.mark.parametrize(
    "params",
    [
        SEARCH,
        _params([100], 0, 0),
        _params([100], 2, -1),
        _params([5], 2, 0),
        _params([3, 100], 2, 0),
        _params([100, 100], 2, 0),
        {**_params([100], 2, 0), "max_splits": 64},
    ],
)
def test_prepare_rejects_missing_or_invalid_settings(params: dict[str, Any]) -> None:
    twin = _bundle(_cp("p1", "A", "B", 10**6, 10**6))
    with pytest.raises(fast.UniSorFastConfigError):
        fast.prepare(twin, AlgorithmConfig(fast.NAME, params))


# ------------------------------------------------------------- vacuous restriction


@pytest.mark.parametrize(
    "rel", ["tests/fixtures/routing/mantle_mixed", "tests/fixtures/corpus/bundle"]
)
def test_a_shortlist_that_keeps_every_route_is_the_reference(rel: str) -> None:
    """Restricted-input equality is only claimed where the restriction is vacuous: every
    enumerated route ranked and kept gives the reference's exact result."""
    bundle = _load(rel)
    params = _params([5, 25, 100], 10**6, 10**6)
    whole = 0
    for case in bundle.cases[:: max(1, len(bundle.cases) // 24)]:
        ref, got = _ref(bundle, case), _fast(bundle, case, params)
        s = got.search_stats
        assert s["quotes_executed"] <= ref.search_stats["quotes_executed"]
        _coverage_is_the_completed_search(got)
        if s["search_scope"] != "full_cohort":
            assert s["search_scope"] in ("shortlist", "full_cohort_fallback", None)
            continue
        whole += 1
        assert (got.status, got.plan, got.evaluation, got.score, got.error) == (
            ref.status, ref.plan, ref.evaluation, ref.score, ref.error)  # fmt: skip
        same = ("selection", "allocation", "d1_residual", "cached_quote", "entry_failures",
                "quote_entries", "route_quotes", "coverage_mode", "routes_enumerated")  # fmt: skip
        for key in same:
            assert s[key] == ref.search_stats[key], key
        assert got.candidates_truncated == 0
    assert whole


# ------------------------------------------------------------- adversarial quality

BIG, MID1, MID2, THIN = "p0big", "p1mid", "p2mid", "p3thin"
THIN_BUNDLE = _bundle(
    _cp(BIG, "A", "B", 10**9, 10**9),
    _cp(MID1, "A", "B", 5 * 10**8, 5 * 10**8),
    _cp(MID2, "A", "B", 5 * 10**8, 5 * 10**8),
    # Low TVL but a better price: worst at 100 %, best at small sizes.
    _cp(THIN, "A", "B", 10**7, 12 * 10**6),
)
THIN_CASE = Case("thin", "A", "B", 10**7)


def test_thin_pool_is_worst_at_full_input_and_best_as_a_small_split() -> None:
    """The construction itself, from the formula: ranking by the full-input quote (or by
    depth) puts the thin pool last, while at 5 % it beats every other pool."""
    x, small = THIN_CASE.amount_in, THIN_CASE.amount_in * 5 // 100
    cp = {p: s for p, s in THIN_BUNDLE.pools.items() if isinstance(s, ConstantProductPoolState)}
    full = {p: _out(x, s.reserve0, s.reserve1) for p, s in cp.items()}
    part = {p: _out(small, s.reserve0, s.reserve1) for p, s in cp.items()}
    assert min(full, key=lambda p: full[p]) == THIN
    assert max(part, key=lambda p: part[p]) == THIN
    ref = _ref(THIN_BUNDLE, THIN_CASE)
    assert THIN in {pid for r in ref.search_stats["selection"]["routes"] for pid in r["pool_ids"]}


def test_full_input_ranking_misses_the_thin_split_and_loses_output() -> None:
    """An actual loss, retained: probing only at 100 % with 3 routes skips the thin pool
    the reference uses, and the returned (valid) plan yields strictly less."""
    ref = _ref(THIN_BUNDLE, THIN_CASE)
    got = _fast(THIN_BUNDLE, THIN_CASE, _params([100], 3, 0))
    assert ref.status is got.status is SolveStatus.OK
    s = got.search_stats
    assert s["search_scope"] == "shortlist" and s["sor_fast"]["search_approximation"] is True
    assert s["search_completed"] is True
    assert s["shortlist"]["searched_route_ids"] == [f"V2:{BIG}", f"V2:{MID1}", f"V2:{MID2}"]
    assert s["shortlist"]["skipped_routes"] == {"V3": 0, "V2": 1, "MIXED": 0}
    assert (s["shortlist"]["searched_pools"], s["shortlist"]["skipped_pools"]) == (3, 1)
    # No fallback: the completed coverage is the initial shortlist.
    assert s["shortlist"]["shortlisted"] == {
        "routes": s["shortlist"]["searched_routes"],
        "pools": 3,
        "route_ids": s["shortlist"]["searched_route_ids"],
    }
    assert got.candidates_truncated == 1 and got.candidates_considered == 4
    assert s["shortlist"]["fallback"] == {"triggered": False, "reason": None, "completed": None}
    assert ref.score is not None and got.score is not None
    assert got.score < ref.score  # the heuristic's loss, not hidden
    _independently_valid(THIN_BUNDLE, THIN_CASE, got)
    assert s["quotes_executed"] < ref.search_stats["quotes_executed"]


def test_a_small_probe_catches_the_thin_split() -> None:
    """Ranking at 5 % as well (one route per probe) keeps the thin pool; with three
    routes per probe the shortlist is every route and the result is the reference's."""
    ref = _ref(THIN_BUNDLE, THIN_CASE)
    one = _fast(THIN_BUNDLE, THIN_CASE, _params([5, 100], 1, 0))
    assert one.search_stats["shortlist"]["searched_route_ids"] == [f"V2:{BIG}", f"V2:{THIN}"]
    assert one.search_stats["shortlist"]["routes_by_probe"] == {"5": 1, "100": 1}
    _independently_valid(THIN_BUNDLE, THIN_CASE, one)
    full_rank = _fast(THIN_BUNDLE, THIN_CASE, _params([100], 3, 0))
    assert one.score is not None and full_rank.score is not None and ref.score is not None
    assert one.score > full_rank.score
    three = _fast(THIN_BUNDLE, THIN_CASE, _params([5, 100], 3, 0))
    assert three.search_stats["search_scope"] == "full_cohort"
    assert (three.plan, three.score) == (ref.plan, ref.score)


def test_unusual_intermediate_route_ranks_on_quotes_and_direct_incumbent_is_retained() -> None:
    """No base-token list: a two-hop route through an arbitrary token X that beats the
    direct pool at full input is the top-ranked route. `direct_routes` additionally keeps
    the best direct route, whatever its rank."""
    bundle = _bundle(
        _cp("ab", "A", "B", 10**8, 10**8),
        _cp("ax", "A", "X", 10**10, 2 * 10**10),
        _cp("xb", "X", "B", 10**10, 10**10),
    )
    case = Case("c", "A", "B", 10**7)
    top = _fast(bundle, case, _params([100], 1, 0))
    assert top.search_stats["shortlist"]["searched_route_ids"] == ["V2:ax>xb"]
    assert top.status is SolveStatus.OK
    _independently_valid(bundle, case, top)
    kept = _fast(bundle, case, _params([100], 1, 1))
    assert kept.search_stats["shortlist"]["direct_retained"] == 1
    assert kept.search_stats["shortlist"]["searched_route_ids"] == ["V2:ab", "V2:ax>xb"]
    ref = _ref(bundle, case)
    assert kept.search_stats["search_scope"] == "full_cohort"
    assert (kept.plan, kept.score) == (ref.plan, ref.score)


def test_ties_keep_the_reference_quote_list_order() -> None:
    twin = _bundle(_cp("p2", "A", "B", 10**6, 10**6), _cp("p1", "A", "B", 10**6, 10**6))
    case = Case("c", "A", "B", 10**5)
    a = _fast(twin, case, _params([100], 1, 0))
    b = _fast(twin, case, _params([100], 1, 0))
    assert a.search_stats["shortlist"]["searched_route_ids"] == ["V2:p1"]  # A-1: ascending id
    assert a.search_stats == b.search_stats and a.plan == b.plan


# ------------------------------------------------------------- fallback / statuses

CL_BUNDLE = _bundle(_cl("cl1", 10**12), _cl("cl2", 10**12))
CL_SEARCH = {"max_hops": 1, "max_splits": 2, "percent_step": 50}


def test_no_ranked_route_falls_back_to_the_full_table_and_charges_it() -> None:
    """Every route needs uncollected state at 100 % (the only probe), but a 50/50 split
    fits the collected band: the shortlist is empty, the full table is searched, and the
    result -- status, plan, quotes -- is exactly the reference's."""
    case = Case("c", "A", "B", 4 * 10**10)
    ref = _ref(CL_BUNDLE, case, CL_SEARCH)
    got = _fast(CL_BUNDLE, case, _params([100], 2, 2, CL_SEARCH))
    assert ref.status is SolveStatus.OK and ref.search_stats["entries_incomplete"] == 2
    s = got.search_stats
    assert s["shortlist"]["probe_entries_incomplete"] == 2 and s["shortlist"]["ranked_routes"] == 0
    assert s["shortlist"]["fallback"] == {
        "triggered": True,
        "reason": "no_ranked_route",
        "completed": True,
    }
    assert s["search_scope"] == "full_cohort_fallback" and got.candidates_truncated == 0
    # The initial shortlist was empty; the completed coverage is the whole fallback table.
    sl = s["shortlist"]
    assert sl["shortlisted"] == {"routes": {"V3": 0, "V2": 0, "MIXED": 0}, "pools": 0,
                                 "route_ids": []}  # fmt: skip
    assert s["search_completed"] is True
    assert sl["searched_routes"] == sl["eligible_routes"] == {"V3": 2, "V2": 0, "MIXED": 0}
    assert sl["skipped_routes"] == {"V3": 0, "V2": 0, "MIXED": 0}
    assert (sl["searched_pools"], sl["skipped_pools"]) == (sl["eligible_pools"], 0) == (2, 0)
    assert sl["searched_route_ids"] == ["V3:cl1", "V3:cl2"]
    assert (got.status, got.plan, got.score) == (ref.status, ref.plan, ref.score)
    assert s["selection"] == ref.search_stats["selection"]
    assert s["quotes_executed"] == ref.search_stats["quotes_executed"]
    q = s["shortlist"]["quotes"]
    assert q["probe"] == 2 and q["fallback_table"] == 2 and q["shortlist_table"] == 0
    assert sum(q.values()) == s["quotes_executed"]
    _independently_valid(CL_BUNDLE, case, got)


def test_interrupted_fallback_is_planned_scope_not_completed_coverage() -> None:
    """25 % grid, probes {50, 100}, one route per probe: only `cl1` is shortlisted (both
    routes tie at 50 %, none is valid at 100 %). Its table completes without a selection,
    so the full-table fallback runs. Uninterrupted, the fallback's coverage is recorded
    and the result is the reference's; with a quote budget that runs out inside the
    fallback, the result is `timeout`, the fallback is `completed: false`, and the
    searched coverage stays the completed shortlist table -- never the planned full one."""
    search = {"max_hops": 1, "max_splits": 2, "percent_step": 25}
    case = Case("c", "A", "B", 4 * 10**10)
    params = _params([50, 100], 1, 0, search)
    shortlisted = {"routes": {"V3": 1, "V2": 0, "MIXED": 0}, "pools": 1, "route_ids": ["V3:cl1"]}
    ref = _ref(CL_BUNDLE, case, search)
    done = _fast(CL_BUNDLE, case, params)
    s = done.search_stats
    assert s["shortlist"]["shortlisted"] == shortlisted
    assert s["shortlist"]["fallback"] == {
        "triggered": True,
        "reason": "no_shortlist_selection",
        "completed": True,
    }
    assert s["search_scope"] == "full_cohort_fallback" and s["search_completed"] is True
    assert s["shortlist"]["searched_route_ids"] == ["V3:cl1", "V3:cl2"]
    assert s["shortlist"]["skipped_routes"] == {"V3": 0, "V2": 0, "MIXED": 0}
    assert (done.status, done.plan, done.score) == (ref.status, ref.plan, ref.score)
    assert s["quotes_executed"] == ref.search_stats["quotes_executed"] == 8
    assert s["shortlist"]["quotes"] == {"probe": 4, "shortlist_table": 2, "fallback_table": 2,
                                        "validation": 0}  # fmt: skip

    cut = _fast(CL_BUNDLE, case, params, Budget(max_quotes=7))
    c = cut.search_stats
    assert cut.status is SolveStatus.TIMEOUT and c["truncated_by"] == "max_quotes"
    assert cut.error is not None and "full-table fallback quote table" in cut.error
    assert c["search_scope"] == "full_cohort_fallback" and c["search_completed"] is False
    assert c["shortlist"]["fallback"] == {
        "triggered": True,
        "reason": "no_shortlist_selection",
        "completed": False,
    }
    assert c["shortlist"]["shortlisted"] == shortlisted
    assert c["shortlist"]["searched_route_ids"] == ["V3:cl1"]
    assert c["shortlist"]["searched_routes"] == {"V3": 1, "V2": 0, "MIXED": 0}
    assert c["shortlist"]["skipped_routes"] == {"V3": 1, "V2": 0, "MIXED": 0}
    assert (c["shortlist"]["searched_pools"], c["shortlist"]["skipped_pools"]) == (1, 1)
    assert c["shortlist"]["quotes"]["fallback_table"] == 1 and c["quotes_executed"] == 7
    assert cut.plan is None and c["selection"] is None


def test_restricted_failure_is_never_no_route_after_fallback_statuses_are_the_references() -> None:
    too_big = Case("c", "A", "B", 10**12)  # even 50 % leaves the collected band
    ref = _ref(CL_BUNDLE, too_big, CL_SEARCH)
    got = _fast(CL_BUNDLE, too_big, _params([100], 2, 0, CL_SEARCH))
    assert ref.status is got.status is SolveStatus.INCOMPLETE_SNAPSHOT
    assert got.search_stats["shortlist"]["fallback"]["triggered"]
    assert got.error is not None and "full cohort table" in got.error
    dust = Case("d", "A", "B", 1)  # every entry floors to zero
    twin = _bundle(_cp("p1", "A", "B", 10**6, 10**6), _cp("p2", "A", "B", 10**6, 10**6))
    ref_d = _ref(twin, dust, CL_SEARCH)
    got_d = _fast(twin, dust, _params([100], 1, 0, CL_SEARCH))
    assert ref_d.status is got_d.status is SolveStatus.NO_ROUTE
    assert got_d.search_stats["shortlist"]["fallback"]["reason"] == "no_ranked_route"
    assert got_d.search_stats["entry_failures"] == ref_d.search_stats["entry_failures"]


def test_enumeration_statuses_are_the_references() -> None:
    lb = _load("tests/fixtures/moe_lb/bundle")
    params = _params([5, 100], 2, 0, {"max_hops": 3, "max_splits": 4, "percent_step": 5})
    got = _fast(lb, lb.cases[0], params)
    assert got.status is SolveStatus.UNSUPPORTED and got.search_stats["universe_routes"] > 0
    twin = _bundle(_cp("p1", "A", "B", 10**6, 10**6))
    none = _fast(twin, Case("c", "A", "Z", 10**6), _params([100], 1, 0))
    assert none.status is SolveStatus.NO_ROUTE and none.search_stats["universe_routes"] == 0


# ------------------------------------------------------------- budgets / accounting


def test_quote_budget_is_a_timeout_never_no_route() -> None:
    probes = _fast(THIN_BUNDLE, THIN_CASE, _params([5, 100], 1, 0), Budget(max_quotes=3))
    assert (
        probes.status is SolveStatus.TIMEOUT and probes.search_stats["truncated_by"] == "max_quotes"
    )
    assert probes.error is not None and "shortlist probes" in probes.error
    assert "not evidence of no_route" in probes.error
    ps = probes.search_stats
    assert ps["search_scope"] is None and ps["search_completed"] is False
    assert (
        ps["shortlist"]["searched_routes"] is None and ps["shortlist"]["searched_route_ids"] is None
    )
    assert ps["shortlist"]["shortlisted"] == {"routes": None, "pools": None, "route_ids": None}
    table = _fast(THIN_BUNDLE, THIN_CASE, _params([100], 1, 0), Budget(max_quotes=5))
    assert table.status is SolveStatus.TIMEOUT and "shortlist quote table" in (table.error or "")
    ts = table.search_stats
    assert ts["search_scope"] == "shortlist" and ts["search_completed"] is False
    assert ts["shortlist"]["shortlisted"]["route_ids"] == [f"V2:{BIG}"]
    assert ts["shortlist"]["searched_route_ids"] is None
    capped = _fast(THIN_BUNDLE, THIN_CASE, _params([100], 1, 0), Budget(max_candidates=3))
    assert capped.status is SolveStatus.TIMEOUT and capped.candidates_truncated == 4


def test_quote_accounting_matches_the_meter_and_never_exceeds_the_reference() -> None:
    bundle = _load("tests/fixtures/corpus/bundle")
    params = _params([5, 100], 2, 1)
    for case in bundle.cases[::8]:
        with metered_quotes(None) as meter:
            got = _fast(bundle, case, params)
        s = got.search_stats
        assert s["quotes_executed"] == meter.counted == sum(s["shortlist"]["quotes"].values())
        assert s["quotes_executed"] <= _ref(bundle, case).search_stats["quotes_executed"]
        if got.status is SolveStatus.OK:
            _independently_valid(bundle, case, got)
        _coverage_is_the_completed_search(got)


def _coverage_is_the_completed_search(res: SolveResult) -> None:
    """Invariant: a completed full-cohort search (kept every route, or a completed
    fallback) records every eligible route and pool as searched; a completed shortlist
    records exactly its shortlist."""
    s = res.search_stats
    sl = s["shortlist"]
    if not s["search_completed"]:
        return
    if s["search_scope"] in ("full_cohort", "full_cohort_fallback"):
        assert sl["searched_routes"] == sl["eligible_routes"]
        assert set(sl["skipped_routes"].values()) == {0} and sl["skipped_pools"] == 0
        assert sl["searched_pools"] == sl["eligible_pools"]
        assert len(sl["searched_route_ids"]) == sum(sl["eligible_routes"].values())
    else:
        assert s["search_scope"] == "shortlist"
        assert sl["searched_route_ids"] == sl["shortlisted"]["route_ids"]
        assert sl["searched_pools"] == sl["shortlisted"]["pools"]


def test_isolated_runner_records_the_variant_with_its_provenance(tmp_path: Path) -> None:
    """Spawned workers pickle the factory and the validated settings; every record keeps
    the approximation label and scope metadata, and the runner's own independent replay
    agrees with the solver's."""
    from benchmark.results import load_case_records
    from benchmark.runner import run_experiment

    bundle = _load("tests/fixtures/routing/mantle_mixed")
    profile = parse_profile(_with(algorithms=["uni_sor_port", "uni_sor_fast"]), "p.yaml")
    manifest = run_experiment(bundle, profile, results_dir=tmp_path, replay_command="cmd")
    assert manifest.complete
    run = json.loads((Path(manifest.run_dir) / "manifest.json").read_text())
    assert "L06 shortlist over the reference" in json.dumps(run)
    records = {(r["algorithm"], r["case_id"]): r for r in load_case_records(manifest.run_dir)}
    for case in bundle.cases:
        rec, ref = records[("uni_sor_fast", case.case_id)], records[("uni_sor_port", case.case_id)]
        assert rec["search"]["sor_fast"]["search_approximation"] is True
        assert rec["search"]["shortlist"]["settings"]["probe_percents"] == [5, 100]
        assert rec["quotes"]["counted"] == rec["search"]["quotes_executed"]
        assert rec["quotes"]["counted"] <= ref["quotes"]["counted"]
        if rec["status"] == "ok":
            assert rec["search"]["evaluated_gross"] == rec["evaluation"]["gross_output"]
        else:
            assert rec["status"] == ref["status"]

"""WHI-1556: the registered `uni_sor_cycle_safe` factory (cycle-safe-sor.md §3-§8, §13).

Every behavioural check runs the REAL registered factory (`get_algorithm(NAME)`: its public
`prepare` and `solve`) or, for the hand-built core quote tables, the module's own selector
(`Admission.select`) through the port's unchanged `get_best_swap_route` seam. The production
module patches nothing.

Expected values never come from the implementation. From the WHI-1555 research module
(`test_cycle_safe_sor_contract`) only the fixtures and hand traces, the hand `getAmountOut`
formula, the exhaustive oracle with its own Kahn cycle test, the fixture/random bundle
builders and the unchanged R021-C/1 contract validator are reused; its executable
specification (`spec_solve`, which swaps the port's selector with `mock.patch` inside the
test module only) is used as a differential reference, never as the implementation. The
reference arm is always the actual `uni_sor_port.solve`, and whole plans are replayed by the
plain evaluator.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import random
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import test_cycle_safe_sor_contract as spec  # sibling: fixtures, oracle, builders, validator
import yaml

import benchmark.profile as profile_module
from benchmark.diagnostics import CheckContext, check_diagnostics, pool_protocol
from benchmark.objective import gross_only
from benchmark.profile import ProfileError, parse_profile, preset_options
from benchmark.strategies import R021_ADDITIONS, derive
from pools.quote import QuoteLimitExceeded, metered_quotes
from routing.algorithms import incremental_graph_repair
from routing.algorithms import uni_sor_cycle_safe as ucs
from routing.algorithms import uni_sor_port as sor
from routing.algorithms.base import (
    AlgorithmConfig,
    Budget,
    OptionsError,
    SolveContext,
    SolveStatus,
    settings_sha256,
    validated_options,
)
from routing.algorithms.registry import (
    ALGORITHMS,
    BASE_STRATEGIES,
    OPTIMIZED_STRATEGIES,
    get_algorithm,
)
from routing.evaluator import EvalStatus, evaluate
from routing.plan import RoutePlan
from snapshot.bundle import load_bundle, write_bundle
from snapshot.models import Case, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
NAME = "uni_sor_cycle_safe"
FACTORY = get_algorithm(NAME)
PROFILES = REPO / "config" / NAME
FIXTURES = REPO / "tests" / "fixtures"
CORPUS = FIXTURES / "corpus" / "bundle"
MIXED = FIXTURES / "routing" / "mantle_mixed"
EMPTY_SHA = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
REVISION = "c" * 40
CORE = spec.CORE
ADAPTER = spec.PHASE_FIXTURES
A1, A3, A4, A7 = spec.A1, spec.A3, spec.A4, spec.A7


# ------------------------------------------------------------------ helpers


def _prepare(bundle: SnapshotBundle, params: Mapping[str, int]) -> ucs.PreparedCycleSafe:
    assert FACTORY.prepare is not None
    prepared = FACTORY.prepare(bundle, AlgorithmConfig(NAME, dict(params)))
    assert isinstance(prepared, ucs.PreparedCycleSafe)
    return prepared


def run(
    bundle: SnapshotBundle,
    case: Case,
    params: Mapping[str, int],
    budget: Budget | None = None,
) -> spec.Run:
    """One registered solve under the worker's quote meter, with its publications."""
    prepared = _prepare(bundle, params)
    published: list[RoutePlan] = []
    context = SolveContext(
        bundle, gross_only(), prepared, candidate_sink=published.append,
        run_identity={"git_revision": REVISION},
    )  # fmt: skip
    with metered_quotes(None) as meter:
        result = FACTORY.solve(case, context, budget or Budget())
    return spec.Run(result, published, meter.counted)


def ref(
    bundle: SnapshotBundle, case: Case, params: Mapping[str, int], budget: Budget | None = None
) -> spec.Run:
    """The actual `uni_sor_port.solve` (the reference arm)."""
    return spec.run(sor.solve, bundle, case, params, budget)


def block(r: spec.Run) -> dict[str, Any]:
    out: dict[str, Any] = r.result.search_stats["cycle_safe"]
    return out


def record(r: spec.Run) -> dict[str, Any]:
    out: dict[str, Any] = r.result.search_stats["r021"]
    return out


def core_select(
    fixture: Mapping[str, Any], admission: ucs.Admission | None = None
) -> tuple[sor.SwapSelection | None, dict[int, str], ucs.Admission]:
    """A hand-built core quote table through the port's `get_best_swap_route` with this
    module's own selector (`Admission.select`)."""
    amount, percents, quotes, _, owner = spec.core_inputs(fixture)
    admission = admission or ucs.Admission()
    selection = sor.get_best_swap_route(
        amount, percents, quotes, max_splits=int(fixture["max_splits"]), select=admission.select
    )
    return selection, owner, admission


def counters(a: ucs.Admission) -> dict[str, int]:
    return {
        "admission_checks": a.admission_checks,
        "combinations_rejected_cycle": a.combinations_rejected_cycle,
    }


def assert_acyclic_plan(bundle: SnapshotBundle, case: Case, r: spec.Run) -> None:
    """Theorem S on the returned plan, by the independent Kahn test and the plain evaluator."""
    if r.result.plan is None:
        return
    edges = [(s.token_in.lower(), s.token_out.lower()) for s in r.result.plan.steps]
    assert spec.kahn_acyclic(edges), "the variant returned a cyclic union"
    ev = evaluate(bundle, case, r.result.plan, gross_only())
    assert "economic token cycle" not in (ev.error or "")


def run_context(
    bundle: SnapshotBundle, case: Case, r: spec.Run, *, sha: str = EMPTY_SHA
) -> CheckContext:
    """What the runner supplies independently of the solver (score: an independent replay)."""
    score = None
    if r.result.plan is not None and r.result.status is SolveStatus.OK:
        ev = evaluate(bundle, case, r.result.plan, gross_only())
        score = str(ev.gross_output) if ev.status is EvalStatus.OK else None
    return CheckContext(
        run={"git_revision": REVISION, "bundle_hash": bundle.bundle_hash, "algorithm": NAME,
             "effective_settings_sha256": sha},
        request={"case_id": case.case_id, "token_in": case.token_in,
                 "token_out": case.token_out, "amount_in": str(case.amount_in)},
        status=r.result.status.value, score=score, objective="gross_only",
        quotes_counted=r.metered, hard_killed=False,
        pools={pid: pool_protocol(p) for pid, p in bundle.pools.items()},
    )  # fmt: skip


def contract_violations(r: spec.Run, case: Case) -> set[str]:
    """The unchanged R021-C/1 contract validator against this run's identity."""
    ctx = {
        "status": r.result.status.value,
        "final_score": None if r.result.score is None else str(r.result.score),
        "objective": "gross_only", "quotes_counted": r.metered, "hard_killed": False,
        "run": {"git_revision": REVISION, "bundle_hash": record(r)["domain"]["universe"]["bundle"],
                "algorithm": NAME, "effective_settings_sha256": EMPTY_SHA},
        "request": {"case_id": case.case_id, "token_in": case.token_in,
                    "token_out": case.token_out, "amount_in": str(case.amount_in)},
    }  # fmt: skip
    found: set[str] = spec.VALIDATOR.check_diagnostics(record(r), ctx)
    return found


# ------------------------------------------------------------------ registration and options


def test_registered_once_as_a_custom_identity_after_incremental_graph_repair() -> None:
    assert ALGORITHMS[NAME] is ucs.FACTORY is FACTORY
    names = list(ALGORITHMS)
    assert names.count(NAME) == 1
    assert names.index(incremental_graph_repair.NAME) + 1 == names.index(NAME)
    assert NAME not in BASE_STRATEGIES and NAME not in OPTIMIZED_STRATEGIES
    assert profile_module.strategy_group(NAME) == "custom"
    assert R021_ADDITIONS[-1] == NAME and R021_ADDITIONS.index(NAME) == 3  # contract order 4
    assert FACTORY.options_validator is ucs.validate_options  # module-level (picklable)
    assert FACTORY.capabilities == sor.CAPABILITIES
    assert FACTORY.search_params == sor.SEARCH_PARAMS and FACTORY.graph_params == ()
    prov = dict(FACTORY.provenance or {})
    assert prov["upstream"] == dict(sor.UPSTREAM)
    assert prov["adaptations"] == list(sor.ADAPTATIONS)
    assert prov["deviations"] == list(sor.DEVIATIONS)
    assert set(prov["source_deviations"]) == {"CS-1", "CS-2", "CS-3"}
    assert "upstream parity" in prov["not_claimed"]
    # the reference is untouched: no options, no preset, same factory object and solve
    assert ALGORITHMS[sor.NAME] is sor.FACTORY and sor.FACTORY.solve is sor.solve
    assert sor.FACTORY.options_validator is None and sor.FACTORY.options_preset is None
    for name in ("daily_gross.yaml", "daily.yaml", "full_gross.yaml", "full.yaml"):
        document, profile = derive(
            yaml.safe_load((REPO / "config" / name).read_text()),
            "all", source_path=f"config/{name}", source_sha256="x",
        )  # fmt: skip
        assert list(profile.algorithms)[-2:] == [incremental_graph_repair.NAME, NAME]
        assert len(profile.algorithms) == 13
        assert document["algorithm_options"][NAME] == {}
        assert profile.algorithm_options[NAME]["source"]["kind"] == "preset"
        assert profile.algorithm_options[NAME]["settings_sha256"] == EMPTY_SHA
        assert dict(profile.algorithm_config(FACTORY).options) == {}
        assert (
            profile.algorithm_config(FACTORY).params == profile.algorithm_config(sor.FACTORY).params
        )


def test_preset_is_the_pinned_empty_mapping_and_tampering_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = dict(FACTORY.options_preset or {})
    assert pin == {"path": "config/uni_sor_cycle_safe/preset_v1.yaml",
                   "sha256": ucs.PRESET["sha256"], "key": NAME, "version": 1}  # fmt: skip
    opts = spec.FIX["options"]
    doc = yaml.safe_load((REPO / pin["path"]).read_text())
    assert doc == opts["preset"]  # {key, version, algorithm, options: {}}
    assert preset_options(FACTORY) == {} and settings_sha256({}) == opts["settings_sha256"]
    assert opts["settings_sha256"] == EMPTY_SHA
    (tmp_path / PROFILES.relative_to(REPO)).mkdir(parents=True)
    changed = (REPO / pin["path"]).read_text().replace("options: {}", "options: {x: 1}")
    (tmp_path / pin["path"]).write_text(changed)
    monkeypatch.setattr(profile_module, "REPO_ROOT", tmp_path)
    with pytest.raises(ProfileError, match="differs from the pin"):
        preset_options(FACTORY)


@pytest.mark.parametrize(
    "options",
    [
        {"max_hops": 3},
        {"shortlist": {}},
        {"admission": "off"},
        {"policy": "skip"},
        {"admission": True},
        {1: 2},
        {"seed": 1},
    ],  # fmt: skip
)
def test_every_option_is_refused_by_every_entry_point(options: dict[Any, Any]) -> None:
    with pytest.raises(OptionsError):
        validated_options(FACTORY, options)
    assert FACTORY.prepare is not None
    with pytest.raises(OptionsError):  # before the bundle is touched
        FACTORY.prepare(None, AlgorithmConfig(NAME, {}, options))  # type: ignore[arg-type]
    doc = yaml.safe_load((PROFILES / "matched.yaml").read_text())
    doc["algorithm_options"] = {NAME: options}
    with pytest.raises(ProfileError):
        parse_profile(doc, "<test>")
    assert validated_options(FACTORY, {}) == {} == ucs.validate_options({})


def test_prepare_validates_its_options_then_reuses_the_port_prepare_unchanged() -> None:
    bundle = load_bundle(MIXED)
    params = {"max_hops": 3, "max_splits": 4, "percent_step": 5}
    prepared = _prepare(bundle, params)
    assert prepared.port == sor.prepare(bundle, AlgorithmConfig(sor.NAME, params))
    assert prepared.settings_sha256 == EMPTY_SHA
    assert _prepare(bundle, params) == prepared  # deterministic
    with pytest.raises(ucs.UniSorCycleSafeConfigError, match=NAME):
        _prepare(bundle, {"max_hops": 3, "max_splits": 64, "percent_step": 5})
    # the reference prepare still refuses options: the wrapper clears its own before it
    with pytest.raises(OptionsError):
        sor.prepare(bundle, AlgorithmConfig(sor.NAME, params, {"x": 1}))


# ------------------------------------------------------------------ core fixtures (hand traces)


@pytest.mark.parametrize("fid", sorted(CORE))
def test_core_fixtures_through_the_module_selector_are_the_hand_traces(fid: str) -> None:
    fixture = CORE[fid]
    sel, owner, admission = core_select(fixture)
    exp = fixture["expected_variant"]
    assert spec.shape(sel, owner) == exp["routes"]
    assert (None if sel is None else sel.swap.quote) == exp["value"]
    assert counters(admission) == {
        "admission_checks": exp["admission_checks"],
        "combinations_rejected_cycle": exp["combinations_rejected_cycle"],
    }
    assert admission.started and admission.completed
    _, percents, _, oracle_routes, _ = spec.core_inputs(fixture)
    best_ok, best_any = spec.oracle(oracle_routes, percents, int(fixture["max_splits"]))
    assert {"best_admissible": best_ok, "best_any": best_any} == fixture["expected_oracle"]
    if sel is None:
        assert best_ok is None  # K6: nothing admissible on this grid
    else:
        assert best_ok is not None and sel.swap.quote <= best_ok
        assert spec.kahn_acyclic([e for r in sel.routes for e in ucs.route_edges(r)])
        pools = [p for r in sel.routes for p in r.pool_identifiers]
        assert len(pools) == len(set(pools))
    # the reference arm on the same table is the hand-traced port selection
    ref_sel, ref_owner = spec.port_selection(fixture)
    assert spec.shape(ref_sel, ref_owner) == fixture["expected_port"]["routes"]


class _Pairwise(ucs.Admission):
    """Mutation: a pairwise instead of a full-union admission test."""

    def choose(
        self, used_routes: Sequence[sor.RouteQuote], candidates: Sequence[sor.RouteQuote]
    ) -> sor.RouteQuote | None:
        used = {pid for r in used_routes for pid in r.pool_identifiers}
        for c in candidates:
            if any(pid in used for pid in c.pool_identifiers):
                continue
            self.admission_checks += 1
            if not any(ucs.union_has_cycle((r, c)) for r in used_routes):
                return c
            self.combinations_rejected_cycle += 1
        return None


class _SkipPercent(ucs.Admission):
    """Mutation: give up the percent at its first cyclic entry instead of continuing."""

    def choose(
        self, used_routes: Sequence[sor.RouteQuote], candidates: Sequence[sor.RouteQuote]
    ) -> sor.RouteQuote | None:
        used = {pid for r in used_routes for pid in r.pool_identifiers}
        for c in candidates:
            if any(pid in used for pid in c.pool_identifiers):
                continue
            self.admission_checks += 1
            if not ucs.union_has_cycle((*used_routes, c)):
                return c
            self.combinations_rejected_cycle += 1
            return None
        return None


def test_full_union_and_continue_scan_are_load_bearing_mutations_fail() -> None:
    k1 = CORE["K1_three_route_cycle"]
    sel, owner, _ = core_select(k1, _Pairwise())
    exp = k1["expected_pairwise_mutation"]
    assert spec.shape(sel, owner) == exp["routes"] and sel is not None
    assert sel.swap.quote == exp["value"] != k1["expected_variant"]["value"]
    assert ucs.union_has_cycle(sel.routes)  # the mutant returns the cyclic triple
    assert not any(ucs.union_has_cycle(p) for p in itertools.combinations(sel.routes, 2))
    k2 = CORE["K2_tie_continue_scan"]
    sel, owner, _ = core_select(k2, _SkipPercent())
    exp = k2["expected_skip_percent_mutation"]
    assert spec.shape(sel, owner) == exp["routes"] and sel is not None
    assert sel.swap.quote == exp["value"] < k2["expected_variant"]["value"]


def test_k4_k5_admission_can_change_or_lose_against_a_valid_reference() -> None:
    k4 = CORE["K4_acyclic_reference_but_different_result"]
    ref_sel, ro = spec.port_selection(k4)
    var_sel, vo, admission = core_select(k4)
    assert ref_sel is not None and not ucs.union_has_cycle(ref_sel.routes)
    assert admission.combinations_rejected_cycle > 0 and spec.shape(ref_sel, ro) != spec.shape(
        var_sel, vo
    )
    k5 = CORE["K5_pruning_regression"]
    ref_sel, _ = spec.port_selection(k5)
    var_sel, _, _ = core_select(k5)
    assert ref_sel is not None and var_sel is not None
    assert not ucs.union_has_cycle(ref_sel.routes) and var_sel.swap.quote < ref_sel.swap.quote


def test_admission_test_is_the_independent_kahn_test_including_self_loops() -> None:
    def rq(*tokens: str) -> sor.RouteQuote:
        pools = tuple(
            sor.SorPool(f"p{i}{a}{b}", a, b, sor.V2)
            for i, (a, b) in enumerate(itertools.pairwise(tokens))
        )
        return sor.RouteQuote(0, sor.SorRoute(sor.V2, pools, tokens), 100, sor.Rational(1), 1)

    assert ucs.union_has_cycle([rq("a", "a")])
    routes = [rq("s", "x", "y", "t"), rq("s", "y", "x", "t"), rq("s", "x", "t"), rq("s", "t")]
    for k in range(1, 4):
        for combo in itertools.combinations(routes, k):
            edges = [e for r in combo for e in ucs.route_edges(r)]
            assert ucs.union_has_cycle(combo) is (not spec.kahn_acyclic(edges))


# ------------------------------------------------------------------ original SOR goldens


@pytest.mark.parametrize("case_id", spec.GOLDEN_CASES)
def test_original_goldens_reject_nothing_and_equal_the_reference(case_id: str) -> None:
    """The 35 goldens stay `uni_sor_port`'s (tests/routing/test_uni_sor_parity.py, unchanged).
    Through this module's selector each completes with zero rejections, so its selection is
    the port's and the golden's (Theorem P)."""
    inp = spec._golden(f"{case_id}.input.json")
    gold = spec._golden(f"{case_id}.golden.json")
    amount, percents, quotes, min_splits, max_splits = spec._golden_inputs(inp)
    reference = sor.get_best_swap_route(
        amount, percents, quotes, min_splits=min_splits, max_splits=max_splits
    )
    admission = ucs.Admission()
    variant = sor.get_best_swap_route(
        amount, percents, quotes, min_splits=min_splits, max_splits=max_splits,
        select=admission.select,
    )  # fmt: skip
    assert admission.completed and admission.combinations_rejected_cycle == 0
    assert spec._selection_view(variant) == spec._selection_view(reference)
    if gold["result"] is None:
        assert variant is None
    else:
        assert variant is not None and [list(r.pool_identifiers) for r in variant.routes] == [
            r["pool_ids"] for r in gold["result"]["routes"]
        ]
    assert len(spec.GOLDEN_CASES) == 35


# ------------------------------------------------------------------ adapter fixtures


def test_a1_union_cycle_is_rejected_and_the_admissible_baseline_wins() -> None:
    bundle, case = spec.fixture_bundle(A1), spec.fixture_case(A1)
    reference = ref(bundle, case, A1["params"])
    assert reference.result.status is SolveStatus.INVALID_PLAN
    assert reference.result.error == A1["expected_port"]["error"]
    var = run(bundle, case, A1["params"])
    exp = A1["expected_variant"]
    assert var.result.status is SolveStatus.OK and var.result.algorithm == NAME
    assert spec.selected(var.result) == exp["routes"]
    assert var.result.evaluation is not None and var.result.evaluation.gross_output == exp["gross"]
    assert spec.hand_chain(A1, ["a", "b", "c"], "s", case.amount_in) == exp["gross"]
    b = block(var)
    assert (b["admission_checks"], b["combinations_rejected_cycle"]) == (2, 2)
    assert b["reference_trajectory"] == "diverged" and b["comparable_completed"] is True
    assert var.published == [var.result.plan]  # once, after the replay, the returned plan
    ev = evaluate(bundle, case, var.result.plan, gross_only())  # type: ignore[arg-type]
    assert ev.status is EvalStatus.OK and ev.gross_output == exp["gross"]
    assert_acyclic_plan(bundle, case, var)


def test_same_candidates_grid_table_and_ledger_as_the_reference() -> None:
    bundle, case = spec.fixture_bundle(A1), spec.fixture_case(A1)
    for params in (A1["params"], {"max_hops": 3, "max_splits": 4, "percent_step": 5}):
        r, v = ref(bundle, case, params), run(bundle, case, params)
        for key in ("routes_enumerated", "quote_entries", "quote_entries_null", "route_quotes",
                    "entry_failures", "coverage_mode", "cohort_pools", "sor_port"):  # fmt: skip
            assert v.result.search_stats[key] == r.result.search_stats[key], key
        assert v.metered == v.result.search_stats["quotes_executed"] == r.metered
        assert record(v)["work"]["quotes_executed"] == v.metered
        assert v.result.status is SolveStatus.OK
        assert_acyclic_plan(bundle, case, v)
    # every port search_stats key is kept, plus the two identity blocks
    assert set(v.result.search_stats) == set(r.result.search_stats) | {"cycle_safe", "r021"}


def test_a3_k6_no_admissible_selection_is_no_route_and_nothing_is_published() -> None:
    bundle, case = spec.fixture_bundle(A3), spec.fixture_case(A3)
    assert ref(bundle, case, A3["params"]).result.status is SolveStatus.INVALID_PLAN
    var = run(bundle, case, A3["params"])
    assert var.result.status is SolveStatus.NO_ROUTE and var.result.plan is None
    assert var.published == []
    b = block(var)
    assert b["no_admissible_selection"] is True
    assert (b["admission_checks"], b["combinations_rejected_cycle"]) == (2, 2)
    assert var.result.error == (
        f"no admissible complete selection over {var.result.search_stats['route_quotes']} valid "
        "quote entries: 2 combinations rejected by plan-token-DAG admission (B-S10 under "
        "admission)"
    )
    assert var.result.search_stats["entry_failures"].get("reverted", 0) > 0  # overflow, counted
    # A6: no selection without rejections keeps the port's B-S10 text and flag false
    a6 = ADAPTER["A6_all_entries_null"]
    b6, c6 = spec.fixture_bundle(a6), spec.fixture_case(a6)
    v6, r6 = run(b6, c6, a6["params"]), ref(b6, c6, a6["params"])
    assert v6.result.status is r6.result.status is SolveStatus.NO_ROUTE
    assert v6.result.error == r6.result.error and block(v6)["no_admissible_selection"] is False


def test_a4_replay_budget_and_publication_only_after_the_ok_replay() -> None:
    bundle, case = spec.fixture_bundle(A4), spec.fixture_case(A4)
    capped = Budget(max_quotes=A4["expected_capped"]["max_quotes"])
    r, v = ref(bundle, case, A4["params"], capped), run(bundle, case, A4["params"], capped)
    for x in (r, v):
        assert x.result.status is SolveStatus.TIMEOUT and "replaying" in (x.result.error or "")
        assert x.metered == A4["table_quotes"]
    assert len(r.published) == 1 and v.published == []  # CS-2
    assert block(v)["publication"]["withheld_by_cs2"] is True
    assert record(v)["certificate"] is None
    assert record(v)["work"]["internal_evaluations"] == 1  # the replay started
    full = run(bundle, case, A4["params"], Budget(max_quotes=5))
    assert full.result.status is SolveStatus.OK
    assert full.metered == A4["table_quotes"] + A4["replay_extra_quotes"]
    assert full.result.search_stats["allocation"] == ["50000", "50001"]  # D-1 remainder last
    assert full.result.evaluation is not None and full.result.evaluation.gross_output == (
        spec.cp_out(50_000, 10**6, 10**6) + spec.cp_out(50_001, 10**6, 10**6)
    )
    assert full.published == [full.result.plan]


def test_a7_replay_invalid_is_returned_and_never_published() -> None:
    bundle, case = spec.fixture_bundle(A7), spec.fixture_case(A7)
    v, r = run(bundle, case, A7["params"]), ref(bundle, case, A7["params"])
    assert v.result.status is r.result.status is SolveStatus.INVALID_PLAN
    assert v.result.search_stats["allocation"] == ["50", "51"]
    assert v.result.error == r.result.error and "economic token cycle" not in (v.result.error or "")
    assert v.published == [] and len(r.published) == 1
    assert block(v)["replay"] == {"phase": "completed", "interrupted_by": None,
                                  "status": "invalid_plan"}  # fmt: skip
    assert block(v)["publication"]["withheld_by_cs2"] is True


def test_incomplete_snapshot_keeps_the_port_precedence() -> None:
    full = load_bundle(MIXED)
    pool = full.pools["0x4cdfc22bf05209de87ee564746dc7e5174631d2b"]  # Uniswap v3 USDT/WMNT
    bundle = dataclasses.replace(full, pools={pool.pool_id: pool}, cases=())
    case = Case("huge", pool.token0, pool.token1, 10**30)
    params = {"max_hops": 3, "max_splits": 2, "percent_step": 50}
    v, r = run(bundle, case, params), ref(bundle, case, params)
    assert r.result.status is v.result.status is SolveStatus.INCOMPLETE_SNAPSHOT
    assert v.result.error == r.result.error
    assert block(v)["selector"]["phase"] == "completed" and not block(v)["no_admissible_selection"]


def test_budgets_are_the_references_and_admission_never_resets_them() -> None:
    bundle, case = spec.fixture_bundle(A1), spec.fixture_case(A1)
    n_routes = A1["routes_enumerated"]
    table = ref(bundle, case, A1["params"]).metered
    for budget, truncated_by, metered in (
        (Budget(max_candidates=n_routes - 1), "max_candidates", 0),
        (Budget(max_quotes=table - 1), "max_quotes", table - 1),
        (Budget(max_quotes=0), "max_quotes", 0),
    ):
        r, v = ref(bundle, case, A1["params"], budget), run(bundle, case, A1["params"], budget)
        for x in (r, v):
            assert x.result.status is SolveStatus.TIMEOUT
            assert x.result.search_stats["truncated_by"] == truncated_by
            assert x.metered == metered
        assert v.result.error == r.result.error
        assert v.result.candidates_truncated == r.result.candidates_truncated
        assert v.published == [] and record(v)["certificate_unavailable_reason"] == "not_produced"
        assert block(v)["reference_trajectory"] == "unavailable"
        assert record(v)["work"]["internal_evaluations"] == 0


@pytest.mark.parametrize("row", spec.A5["cases"], ids=[r["id"] for r in spec.A5["cases"]])
def test_phase_availability_and_completion_semantics(row: Mapping[str, Any]) -> None:
    fixture = ADAPTER[row["fixture"]]
    bundle, case = spec.fixture_bundle(fixture), spec.fixture_case(fixture)
    if "token_out" in row:
        case = dataclasses.replace(case, token_out=row["token_out"])
    budget = spec._phase_budget(fixture, row["budget"])
    r, v = (
        ref(bundle, case, fixture["params"], budget),
        run(bundle, case, fixture["params"], budget),
    )
    b = block(v)
    assert v.result.status.value == row["status"]
    assert r.result.status.value == row["reference_status"]
    assert b["selector"]["phase"] == row["selector_phase"]
    assert b["selector"]["not_started_reason"] == row["not_started_reason"]
    assert b["replay"] == {"phase": row["replay_phase"], "interrupted_by": row["interrupted_by"],
                           "status": row["replay_status"]}  # fmt: skip
    assert b["reference_trajectory"] == row["reference_trajectory"]
    assert b["comparable_completed"] is row["comparable_completed"]
    assert len(v.published) == row["published"] == int(b["publication"]["published"])
    assert len(r.published) == row["reference_published"]
    assert b["publication"]["withheld_by_cs2"] is row["withheld_by_cs2"]
    assert (b["admission_checks"], b["combinations_rejected_cycle"]) == (
        row["admission_checks"], row["combinations_rejected_cycle"])  # fmt: skip
    if row["selector_phase"] == "not_started":
        assert b["reference_trajectory"] == "unavailable" and not spec.comparable_identical(b)
        assert b["selector"]["selection"] is None
    if spec.comparable_identical(b):
        spec.assert_end_to_end_identical(r, v)
    if row["id"] == "replay_cut":
        assert v.result.search_stats["selection"] == r.result.search_stats["selection"]
        assert r.published and not v.published and v.result.plan is None


def test_the_registered_factory_equals_the_executable_specification() -> None:
    """Differential: on every adapter fixture, phase budget and seeded bundle the registered
    solve and the research `spec_solve` agree on status, error, plan, score, publications and
    every `search_stats` block (the certificate's revision is the runner's here, a
    placeholder there)."""
    runs: list[tuple[SnapshotBundle, Case, Mapping[str, int], Budget]] = []
    for row in spec.A5["cases"]:
        fixture = ADAPTER[row["fixture"]]
        case = spec.fixture_case(fixture)
        if "token_out" in row:
            case = dataclasses.replace(case, token_out=row["token_out"])
        runs.append((spec.fixture_bundle(fixture), case, fixture["params"],
                     spec._phase_budget(fixture, row["budget"])))  # fmt: skip
    rng = random.Random(1556)
    for hops in (2, 3):
        for _ in range(15):
            bundle = spec.random_bundle(rng, 5, rng.randint(6, 11))
            case = Case("r", "t0", "t4", rng.randint(10**4, 3 * 10**6))
            runs.append((bundle, case, {"max_hops": hops, "max_splits": 3, "percent_step": 20},
                         Budget()))  # fmt: skip
    for bundle, case, params, budget in runs:
        v = run(bundle, case, params, budget)
        s = spec.run(spec.spec_solve, bundle, case, params, budget)
        for key in ("status", "error", "plan", "score", "evaluation", "candidates_considered",
                    "candidates_truncated"):  # fmt: skip
            assert getattr(v.result, key) == getattr(s.result, key), key
        assert v.published == s.published and v.metered == s.metered
        vs, ss = dict(v.result.search_stats), dict(s.result.search_stats)
        vr, sr = vs.pop("r021"), ss.pop("r021")
        assert vs == ss
        for rec in (vr, sr):
            if rec["certificate"] is not None:
                rec["certificate"]["source"].pop("git_revision")
        assert vr == sr


# ------------------------------------------------------------------ Theorems S / P, L4


def _random_pairs(
    seed: int, count: int, hops: int
) -> list[tuple[SnapshotBundle, Case, spec.Run, spec.Run]]:
    rng = random.Random(seed)
    params = {"max_hops": hops, "max_splits": 3, "percent_step": 20}
    out = []
    for _ in range(count):
        bundle = spec.random_bundle(rng, 5, rng.randint(6, 11))
        case = Case("r", "t0", "t4", rng.randint(10**4, 3 * 10**6))
        out.append((bundle, case, ref(bundle, case, params), run(bundle, case, params)))
    return out


def test_theorem_s_and_p_on_seeded_three_hop_bundles() -> None:
    seen = {"identical": 0, "diverged": 0, "unavailable": 0, "reference_cycle": 0}
    for bundle, case, r, v in _random_pairs(20261005, 60, 3):
        b = block(v)
        assert not spec._is_cycle_error(v.result), "the variant produced a token cycle"
        assert_acyclic_plan(bundle, case, v)
        assert v.metered == v.result.search_stats["quotes_executed"]
        seen[b["reference_trajectory"]] += 1
        if b["reference_trajectory"] == "unavailable":
            assert b["selector"]["not_started_reason"] == "enumeration_status"
            assert b["admission_checks"] == b["combinations_rejected_cycle"] == 0
            assert v.result.status is r.result.status
            continue
        assert b["selector"]["phase"] == "completed" and b["comparable_completed"]
        assert v.result.search_stats["route_quotes"] == r.result.search_stats["route_quotes"]
        if spec.comparable_identical(b):
            spec.assert_end_to_end_identical(r, v)
            assert v.metered == r.metered  # admission costs no quote
        if spec._is_cycle_error(r.result):
            seen["reference_cycle"] += 1
            assert b["reference_trajectory"] == "diverged"
    assert all(seen.values()), seen


def test_l4_two_hops_never_reject_and_equal_the_reference() -> None:
    completed = 0
    for _, _, r, v in _random_pairs(20261006, 40, 2):
        b = block(v)
        assert b["combinations_rejected_cycle"] == 0 and b["reference_trajectory"] != "diverged"
        assert v.result.status is r.result.status
        if spec.comparable_identical(b):
            completed += 1
            spec.assert_end_to_end_identical(r, v)
    assert completed >= 20  # non-vacuous


def test_real_state_corpus_fixture_cases_are_completed_zero_rejection_identities() -> None:
    bundle = load_bundle(CORPUS)
    params = {"max_hops": 3, "max_splits": 4, "percent_step": 5}
    for case in bundle.cases:
        r, v = ref(bundle, case, params), run(bundle, case, params)
        b = block(v)
        assert b["selector"]["phase"] == "completed"
        assert b["replay"] == {"phase": "completed", "interrupted_by": None, "status": "ok"}
        assert spec.comparable_identical(b)
        spec.assert_end_to_end_identical(r, v)
        assert v.metered == r.metered == record(v)["work"]["quotes_executed"]
    assert len(bundle.cases) == 96


# ------------------------------------------------------------------ records, ledger, state


def test_diagnostics_pass_the_contract_and_runtime_validators() -> None:
    cases = [(A1, None, SolveStatus.OK), (A3, None, SolveStatus.NO_ROUTE),
             (A1, Budget(max_candidates=1), SolveStatus.TIMEOUT),
             (A4, Budget(max_quotes=4), SolveStatus.TIMEOUT)]  # fmt: skip
    for fixture, budget, status in cases:
        bundle, case = spec.fixture_bundle(fixture), spec.fixture_case(fixture)
        v = run(bundle, case, fixture["params"], budget)
        assert v.result.status is status
        rec = record(v)
        assert check_diagnostics(rec, run_context(bundle, case, v)) == set()
        assert contract_violations(v, case) == set()
        assert rec["domain"] == ucs.sor_domain(bundle, _prepare(bundle, fixture["params"]).port)
        assert rec["work"]["combinations_rejected_cycle"] <= rec["work"]["admission_checks"]
        assert rec["max_candidates_unit"] == "enumerated_routes_threshold"
        assert rec["fallback"] == {"used": False, "source": None, "reason": None}
        if status is SolveStatus.OK:
            cert = rec["certificate"]
            assert (cert["bound_kind"], cert["upper_raw"], cert["termination"]) == (
                "unknown", None, "complete")  # fmt: skip
            assert cert["source"] == {
                "git_revision": REVISION,
                "bundle_hash": bundle.bundle_hash,
                "algorithm": NAME,
                "effective_settings_sha256": EMPTY_SHA,
            }
            assert cert["request"]["amount_in"] == str(case.amount_in)
        else:
            assert rec["certificate"] is None
    bundle, case = spec.fixture_bundle(A1), spec.fixture_case(A1)
    v = run(bundle, case, A1["params"])
    assert check_diagnostics(record(v), run_context(bundle, case, v, sha="0" * 64)) == {
        "C_IDENTITY"
    }
    same = json.loads(json.dumps(record(v)))
    same["work"]["quotes_executed"] += 1
    assert "W_LEDGER" in check_diagnostics(same, run_context(bundle, case, v))


def test_lb_only_request_is_a_visible_unsupported_scope() -> None:
    """D-4: a request reachable only through Liquidity Book pools is the port's
    `unsupported`, recorded as an unsupported scope (not a silent omission)."""
    bundle = load_bundle(FIXTURES / "moe_lb" / "bundle")
    case = bundle.case("usdt_wmnt_small")
    params = {"max_hops": 3, "max_splits": 2, "percent_step": 50}
    r, v = ref(bundle, case, params), run(bundle, case, params)
    assert r.result.status is v.result.status is SolveStatus.UNSUPPORTED
    assert v.result.error == r.result.error and v.published == []
    assert record(v)["scope"] == {"supported": False, "reason": "route_only_through_non_sor_pools"}
    assert block(v)["selector"]["not_started_reason"] == "enumeration_status"
    assert check_diagnostics(record(v), run_context(bundle, case, v)) == set()


def test_a_hard_kill_returns_nothing_and_publishes_nothing() -> None:
    """The worker's hard meter at every quote of a completed solve: `QuoteLimitExceeded`
    escapes (no result, so no phase record), and CS-2 has published nothing yet."""
    bundle, case = spec.fixture_bundle(A4), spec.fixture_case(A4)
    params = A4["params"]
    needed = run(bundle, case, params).metered
    prepared = _prepare(bundle, params)
    for kill_at in range(needed):
        sink: list[RoutePlan] = []
        ctx = SolveContext(bundle, gross_only(), prepared, candidate_sink=sink.append)
        with metered_quotes(kill_at), pytest.raises(QuoteLimitExceeded):
            FACTORY.solve(case, ctx, Budget())
        assert sink == []


def test_case_order_and_a_shared_prepared_object_leak_no_state() -> None:
    bundle = load_bundle(CORPUS)
    params = {"max_hops": 3, "max_splits": 4, "percent_step": 5}
    prepared = _prepare(bundle, params)
    cases = list(bundle.cases[:12]) + [spec.fixture_case(A1)]
    bundles = {c.case_id: bundle for c in cases}
    bundles["a1"] = spec.fixture_bundle(A1)
    preps = {c.case_id: prepared for c in cases}
    preps["a1"] = _prepare(bundles["a1"], params)

    def solve_all(order: list[Case]) -> dict[str, Any]:
        out = {}
        for c in order:
            ctx = SolveContext(bundles[c.case_id], gross_only(), preps[c.case_id])
            out[c.case_id] = FACTORY.solve(c, ctx, Budget()).to_dict()
        return out

    assert solve_all(cases) == solve_all(cases[::-1])
    assert preps["a1"] == _prepare(bundles["a1"], params)


# ------------------------------------------------------------------ CLI: run + quote --details


def _saved_run(out: str) -> Path:
    marker = "(run "
    return Path(out[out.index(marker) + len(marker) :].split(")", 1)[0])


def _a1_bundle(tmp_path: Path) -> Path:
    from snapshot.models import BlockRef, ConstantProductPoolState

    pools = [
        ConstantProductPoolState(pid, t0, t1, int(r0), int(r1), fee_bps=30)
        for pid, (t0, t1, r0, r1) in A1["pools"].items()
    ]
    write_bundle(
        tmp_path / "a1", bundle_id="whi1556-a1", kind="synthetic",
        block=BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0),
        pools=pools, cases=[spec.fixture_case(A1), Case("a1_small", "s", "t", 20_000)],
    )  # fmt: skip
    return tmp_path / "a1"


def test_cli_run_replay_report_and_quote_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both CLI paths through the spawned worker and the real factory next to `uni_sor_port`
    on the matched profile: the options identity, the runtime-validated view, literal
    `--strategies profile` replay, the offline report and one solve per quote."""
    import main
    from benchmark.results import load_case_records, load_manifest
    from benchmark.runner import compare_runs

    profile = PROFILES / "matched.yaml"
    for bundle_dir in (_a1_bundle(tmp_path), CORPUS):
        results = tmp_path / "out" / bundle_dir.name / "runs"
        argv = ["run", "--bundle", str(bundle_dir), "--profile", str(profile),
                "--results-dir", str(results), "--strategies", "profile"]  # fmt: skip
        assert main.main(argv) == 0
        out = capsys.readouterr().out
        assert f"Experimental and other strategies (1): {NAME}" in out
        (run_dir,) = results.iterdir()
        manifest = load_manifest(run_dir)
        assert list(manifest.algorithms) == [sor.NAME, NAME]
        entry = manifest.resolved_profile["algorithm_options"][NAME]
        assert entry == {"options": {}, "settings_sha256": EMPTY_SHA,
                         "source": {"kind": "preset", **ucs.PRESET}}  # fmt: skip
        records = load_case_records(run_dir)
        reference = {r["case_id"]: r for r in records if r["algorithm"] == sor.NAME}
        own = [r for r in records if r["algorithm"] == NAME]
        assert len(own) == len(reference) == len(manifest.measurement["case_order"]) > 0
        for r in own:
            view, base = r["diagnostics"], reference[r["case_id"]]
            assert view["codes"] == [] and view["origin"] == "solver"
            assert view["work"]["quotes_executed"] == view["checked_against"]["quotes_counted"]
            assert view["checked_against"]["run"]["effective_settings_sha256"] == EMPTY_SHA
            cs = r["search"]["cycle_safe"]
            assert "economic token cycle" not in (r.get("error") or "")
            if cs["reference_trajectory"] == "identical" and cs["comparable_completed"]:
                assert (r["status"], r["score"]) == (base["status"], base["score"])
            if r["status"] == "ok":
                assert view["state"] == "unknown"
            else:
                assert view["state"] == "unavailable"
        if bundle_dir.name == "a1":
            by = {r["case_id"]: r for r in own}
            assert reference["a1"]["status"] == "invalid_plan"
            assert "economic token cycle" in reference["a1"]["error"]
            assert by["a1"]["status"] == "ok"
            assert by["a1"]["search"]["cycle_safe"]["reference_trajectory"] == "diverged"
        replay = shlex.split(manifest.replay_command)
        assert replay[-2:] == ["--strategies", "profile"]
        assert main.main(replay[replay.index("main.py") + 1 :]) == 0
        replayed = next(p for p in results.iterdir() if p != run_dir)
        assert load_manifest(replayed).resolved_profile == manifest.resolved_profile
        assert compare_runs(run_dir, replayed) == []
        report = tmp_path / "out" / bundle_dir.name / "report"
        assert main.main(["report", str(run_dir), "--output", str(report)]) == 0
        assert NAME in (report / "report.html").read_text()
        capsys.readouterr()
    quotes = tmp_path / "quotes"
    argv = ["quote", "--bundle", str(CORPUS), "--profile", str(profile), "--token-in", "USDC",
            "--token-out", "USDT0", "--amount", "1500.25", "--details",
            "--quotes-dir", str(quotes), "--strategies", "profile"]  # fmt: skip
    assert main.main(argv) == 0
    out = capsys.readouterr().out
    assert f"[{NAME}] ok" in out and f"[{sor.NAME}] ok" in out
    assert "bound: unknown" in out
    assert "max_candidates unit: enumerated_routes_threshold" in out
    assert "admission_checks" in out and "combinations_rejected_cycle" in out
    quote_run = _saved_run(out)
    quote_records = load_case_records(quote_run)
    assert [r["algorithm"] for r in quote_records] == [sor.NAME, NAME]
    for r in quote_records:
        assert r["measurement"]["attempts_completed"] == 1
        assert len(r["measurement"]["solve_seconds"]) == 1
    own_quote = quote_records[1]
    assert own_quote["search"]["cycle_safe"]["publication"]["published"] is True
    saved = yaml.safe_load((quote_run.parent.parent / "profile.yaml").read_text())
    assert saved["algorithm_options"] == {NAME: {}}


def test_saved_pre_whi_1556_profiles_replay_literally() -> None:
    """A saved effective profile (8 to 12 strategies) never gains the identity under
    `--strategies profile`, and `base` / `optimized` never select it."""
    source = yaml.safe_load((REPO / "config" / "daily_gross.yaml").read_text())
    document, _ = derive(source, "all", source_path="s", source_sha256="x")
    later = ["incremental_graph_repair", "direct_split_certified", "metis_history",
             "metis_inspired"]  # fmt: skip
    for k in range(len(later) + 1):
        drop = [NAME, *later[:k]]
        saved = json.loads(json.dumps(document))
        saved["algorithms"] = [a for a in saved["algorithms"] if a not in drop]
        saved["selection"]["groups"]["custom"] = [
            a for a in saved["selection"]["groups"]["custom"] if a not in drop
        ]
        options = {n: o for n, o in saved["algorithm_options"].items() if n not in drop}
        saved.pop("algorithm_options")
        if options:
            saved["algorithm_options"] = options
        literal, profile = derive(saved, "profile", source_path="s", source_sha256="x")
        assert literal == saved and NAME not in profile.algorithms
        assert len(profile.algorithms) == 12 - k
    for mode in ("base", "optimized"):
        assert NAME not in derive(source, mode, source_path="s", source_sha256="x")[1].algorithms

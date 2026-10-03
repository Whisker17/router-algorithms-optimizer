"""WHI-1599: `single_path_bounded` -- `single_path` with the default-off rule S1 bound pruning
(`docs/references/research-022/pruning-contract.md` §4, §10; helper `pools.bounds`).

Expected values come from the **reference** `single_path.solve` and the exact quote seam, never
from the bounded code under test. The test-local pruning model of `test_pruning_contract.py`
(`run_single_path`, driven by WHI-1597's formulae) is the contract's own model; the production
solver must agree with it counter for counter. Fixtures are tracked, so these run in a
worktree; the frozen tuning split lives only in the primary clone's gitignored `data/` and is
reached through `ROUTER_TUNING_BUNDLE` (a skip is not evidence: the tracked 19-pool real
fixture, 96 cases, is the always-run differential).
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import pickle
import random
import re
import sys
import typing
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, cast

import pytest
import yaml

import main
from benchmark.costs import load_cost_model
from benchmark.objective import (
    ObjectiveContext,
    ObjectiveMode,
    empirical_cost,
    gross_only,
    synthetic_fixed_cost,
)
from benchmark.profile import parse_profile, read_profile_document, strategy_group
from benchmark.results import load_case_records, load_manifest
from benchmark.runner import compare_runs
from benchmark.strategies import R021_ADDITIONS, R022_ADDITIONS, derive
from pools import bounds as bounds_module
from pools.bounds import BoundTable
from routing.algorithms import single_path, single_path_bounded
from routing.algorithms.base import (
    AlgorithmConfig,
    Budget,
    OptionsError,
    SolveContext,
    SolveResult,
    SolveStatus,
)
from routing.algorithms.registry import ALGORITHMS, BASE_STRATEGIES, OPTIMIZED_STRATEGIES
from routing.plan import RoutePlan
from snapshot.bundle import load_bundle
from snapshot.models import Case, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
NAME = single_path_bounded.NAME
TUNING_ENV = "ROUTER_TUNING_BUNDLE"
MIXED = REPO / "tests" / "fixtures" / "routing" / "mantle_mixed"
TRACKED_CORPUS = REPO / "tests" / "fixtures" / "corpus" / "bundle"


def _load_pruning_contract() -> ModuleType:
    path = REPO / "tests" / "routing" / "test_pruning_contract.py"
    spec = importlib.util.spec_from_file_location("_whi1599_pruning_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PC = _load_pruning_contract()

# the counters the contract (§8.1) requires to be identical to the reference's
IDENTICAL_STATS = (
    "max_hops",
    "min_hops_unbounded",
    "paths_enumerated",
    "direct_candidates",
    "paths_truncated",
    "truncated_by",
    "best_hops",
)


# ======================================================================================
# drivers
# ======================================================================================


def reference(
    bundle: SnapshotBundle,
    case: Case,
    hops: int,
    objective: Any = None,
    budget: Budget | None = None,
) -> tuple[SolveResult, list[RoutePlan]]:
    sink: list[RoutePlan] = []
    result = cast(
        SolveResult,
        PC.sp_reference(bundle, case, max_hops=hops, objective=objective, budget=budget, sink=sink),
    )
    return result, sink


def bounded(
    bundle: SnapshotBundle,
    case: Case,
    hops: int,
    objective: Any = None,
    budget: Budget | None = None,
    *,
    prepared: Any = None,
) -> tuple[SolveResult, list[RoutePlan]]:
    sink: list[RoutePlan] = []
    if prepared is None:
        prepared = single_path_bounded.FACTORY.prepare(  # type: ignore[misc]
            bundle, AlgorithmConfig(NAME, {"max_hops": hops})
        )
    context = SolveContext(
        bundle=bundle,
        objective=objective or gross_only(),
        prepared=prepared,
        candidate_sink=sink.append,
    )
    return single_path_bounded.FACTORY.solve(case, context, budget or Budget()), sink


def assert_exact(
    ref: SolveResult,
    got: SolveResult,
    ref_published: list[RoutePlan],
    got_published: list[RoutePlan],
) -> None:
    """Contract §3.1/§8.1: everything that must be identical, `to_dict` and plan included."""
    assert got.algorithm == NAME
    assert got.status is ref.status
    assert got.plan == ref.plan
    assert (got.evaluation is None) == (ref.evaluation is None)
    if got.evaluation is not None and ref.evaluation is not None:
        assert got.evaluation.to_dict() == ref.evaluation.to_dict()
    assert got.score == ref.score
    assert got.error == ref.error
    assert got.candidates_considered == ref.candidates_considered
    assert got.candidates_truncated == ref.candidates_truncated
    for key in IDENTICAL_STATS:
        assert got.search_stats[key] == ref.search_stats[key], key
    assert got_published == ref_published  # the report_candidate sequence


def all_fixtures() -> list[tuple[str, SnapshotBundle, Case]]:
    return cast("list[tuple[str, SnapshotBundle, Case]]", PC.sp_fixtures())


# ======================================================================================
# differential versus the reference
# ======================================================================================


def test_bounded_equals_the_reference_on_every_fixture_and_objective_and_is_not_vacuous() -> None:
    cells = pruned = candidates = quotes_ref = quotes_bnd = 0
    for name, bundle, case in all_fixtures():
        for _label, make in PC.SP_OBJECTIVES:
            for hops in (2, 3):
                ref, ref_pub = reference(bundle, case, hops, make())
                got, got_pub = bounded(bundle, case, hops, make())
                assert_exact(ref, got, ref_pub, got_pub)
                stats = got.search_stats
                block = stats["bound_pruning"]
                # the work counters only ever shrink; pruned candidates are their own counter
                assert stats["quotes_executed"] <= ref.search_stats["quotes_executed"], (
                    name, case.case_id,
                )  # fmt: skip
                assert stats["paths_evaluated"] <= ref.search_stats["paths_evaluated"]
                assert stats["paths_enumerated"] == (
                    stats["paths_evaluated"]
                    + stats["paths_pruned"]
                    + block["pruned_bound"]
                    + stats["paths_truncated"]
                )
                assert block["exactness"] == {"label": "exact", "binding": []}
                assert block["bound_evaluations"] >= block["pruned_bound"] + block["bound_no_bound"]
                cells += 1
                pruned += block["pruned_bound"]
                candidates += stats["paths_enumerated"]
                quotes_ref += ref.search_stats["quotes_executed"]
                quotes_bnd += stats["quotes_executed"]
    print(
        f"differential: cells={cells} candidates={candidates} pruned_bound={pruned} "
        f"quotes {quotes_ref} -> {quotes_bnd}"
    )
    assert cells == len(all_fixtures()) * 8
    assert pruned > 0 and quotes_bnd < quotes_ref  # a bounded run that never prunes proves nothing


def test_production_counters_equal_the_contracts_pruning_model() -> None:
    """The contract's test-local model (WHI-1597 formulae, reference loop with the rule
    inserted) and the production solver (the `pools.bounds` helper) agree counter for counter."""
    compared = 0
    for _name, bundle, case in all_fixtures():
        model = PC.run_single_path(bundle, case, max_hops=3, bounds=PC.Bounds(bundle))
        got, _ = bounded(bundle, case, 3)
        block = got.search_stats["bound_pruning"]
        for key in ("pruned_bound", "bound_evaluations"):
            assert block[key] == model.stats[key], key
        for key in ("paths_evaluated", "paths_pruned", "quotes_executed", "quotes_memoized"):
            assert got.search_stats[key] == model.stats[key], key
        assert got.candidates_considered == model.considered
        compared += 1
    assert compared == len(all_fixtures())


def _differing_cells() -> int:
    count = 0
    for _name, bundle, case in all_fixtures():
        ref, _ = reference(bundle, case, 3)
        got, _ = bounded(bundle, case, 3)
        count += (got.status, got.plan, got.score) != (ref.status, ref.plan, ref.score)
    return count


@pytest.mark.parametrize(
    ("label", "shrink"),
    [("halved", Fraction(1, 2)), ("shrunk by 1%", Fraction(99, 100)),
     ("shrunk by 0.1%", Fraction(999, 1000))],
)  # fmt: skip
def test_an_underestimating_bound_is_caught_by_the_differential(
    label: str, shrink: Fraction, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation check: the differential passes on the real helper and fails on the very same
    cells once every hop's rate is made smaller. (A 1e-6 shrink is below the price impact of
    every fixture quote, so it is caught at the helper-vs-seam level instead, in
    `tests/pools/test_bounds.py`.)"""
    assert _differing_cells() == 0
    original = bounds_module.bound_out
    monkeypatch.setattr(single_path, "bound_out", lambda rate, x: original(rate * shrink, x))
    assert _differing_cells() > 0, f"an underestimating bound ({label}) was not caught"


def test_a_tie_never_replaces_the_incumbent_and_is_pruned_at_the_tie() -> None:
    bundle = PC.synthetic(
        PC.cp("p1", "A", "B", 10**12, 10**12),
        PC.cp("p2", "A", "B", 10**12, 10**12),
        PC.cp("p3", "A", "B", 10**12, 5 * 10**11),
    )
    case = PC.case_of("A", "B", 1001)
    ref, ref_pub = reference(bundle, case, 2)
    got, got_pub = bounded(bundle, case, 2)
    assert got.plan is not None and [s.pool_id for s in got.plan.steps] == ["p1"]  # first wins
    assert_exact(ref, got, ref_pub, got_pub)
    assert got.search_stats["bound_pruning"]["pruned_bound"] >= 1  # p2 ties p1 and is skipped
    assert got.search_stats["paths_pruned"] == 0  # not a dead prefix, not a failure
    assert got.search_stats["failed_candidates"] == {}


def test_nothing_is_pruned_without_an_incumbent() -> None:
    bundle = PC.synthetic(PC.cp("p1", "A", "B", 10**9, 10**9))
    got, _ = bounded(bundle, PC.case_of("A", "B", 1000), 3)
    block = got.search_stats["bound_pruning"]
    assert (block["pruned_bound"], block["bound_evaluations"]) == (0, 0)
    # no route: no incumbent ever exists, so the reference's status and error are reproduced
    empty = PC.synthetic(PC.cp("p1", "A", "C", 10**9, 10**9))
    ref, ref_pub = reference(empty, PC.case_of("A", "B", 1000), 3)
    got, got_pub = bounded(empty, PC.case_of("A", "B", 1000), 3)
    assert ref.status is SolveStatus.NO_ROUTE
    assert_exact(ref, got, ref_pub, got_pub)


def test_no_bound_never_prunes() -> None:
    """An empty table (every direction `None`) prunes nothing and changes nothing; a table
    with one pool's directions removed still prunes through the others, and every hop through
    the missing pool is counted as `bound_no_bound`."""
    for _name, bundle, case in all_fixtures()[:8]:
        prepared = single_path.prepare_bounded(bundle, AlgorithmConfig(NAME, {"max_hops": 3}))
        blank = single_path.PreparedBoundedSinglePath(
            prepared.index, 3, BoundTable(MappingProxyType({}), 0, 0, 0, 0, 0.0)
        )
        ref, ref_pub = reference(bundle, case, 3)
        got, got_pub = bounded(bundle, case, 3, prepared=blank)
        block = got.search_stats["bound_pruning"]
        assert block["pruned_bound"] == 0
        assert block["bound_no_bound"] == block["bound_evaluations"]
        assert_exact(ref, got, ref_pub, got_pub)
        assert got.search_stats["quotes_executed"] == ref.search_stats["quotes_executed"]
        assert got.search_stats["paths_evaluated"] == ref.search_stats["paths_evaluated"]

    bundle = PC.synthetic(
        PC.cp("good", "A", "B", 10**12, 10**12),
        PC.cp("good2", "A", "B", 10**12, 5 * 10**11),
        PC.cp("hole", "A", "B", 10**12, 10**11),
    )
    case = PC.case_of("A", "B", 1001)
    full = single_path.prepare_bounded(bundle, AlgorithmConfig(NAME, {"max_hops": 2}))
    table = {k: v for k, v in full.bound_table.bounds.items() if k[0] != "hole"}
    partial = single_path.PreparedBoundedSinglePath(
        full.index, 2, replace(full.bound_table, bounds=MappingProxyType(table))
    )
    got, got_pub = bounded(bundle, case, 2, prepared=partial)
    ref, ref_pub = reference(bundle, case, 2)
    assert_exact(ref, got, ref_pub, got_pub)
    block = got.search_stats["bound_pruning"]
    assert block["pruned_bound"] == 1 and block["bound_no_bound"] == 1  # good2 skipped, hole kept


# ======================================================================================
# random graphs, budgets and labels
# ======================================================================================


def test_random_graphs_with_and_without_binding_budgets() -> None:
    rng = random.Random(22_1599_10)
    cells = binding = both_free = pruned = 0
    for _ in range(300):
        bundle = PC.random_cpmm_graph(rng, "ABCDEF", (8, 16))
        case = PC.case_of("A", "F", rng.choice([10**4, 10**6, 10**7]))
        free_ref, free_pub = reference(bundle, case, 3)
        free_got, free_got_pub = bounded(bundle, case, 3)
        assert_exact(free_ref, free_got, free_pub, free_got_pub)
        pruned += free_got.search_stats["bound_pruning"]["pruned_bound"]
        prepared = single_path_bounded.FACTORY.prepare(  # type: ignore[misc]
            bundle, AlgorithmConfig(NAME, {"max_hops": 3})
        )
        for budget in (
            Budget(max_quotes=rng.randint(1, 30)),
            Budget(max_candidates=rng.randint(1, 10)),
        ):
            ref, ref_pub = reference(bundle, case, 3, budget=budget)
            got, got_pub = bounded(bundle, case, 3, budget=budget, prepared=prepared)
            block = got.search_stats["bound_pruning"]
            cells += 1
            truncated_by = got.search_stats["truncated_by"]
            if truncated_by is not None:
                binding += 1
                # labelled, never reported as exact; the converse corners are one-sided (§8.2)
                assert block["exactness"] == {
                    "label": "not_exact_budget_binding",
                    "binding": [truncated_by],
                }
                assert ref.search_stats["truncated_by"] is not None  # B truncates => R truncates
            else:
                assert block["exactness"] == {"label": "exact", "binding": []}
                assert_exact(free_ref, got, free_pub, got_pub)  # untruncated B equals R-infinity
            if ref.search_stats["truncated_by"] is None:
                both_free += 1
                assert truncated_by is None
                assert_exact(ref, got, ref_pub, got_pub)
    assert cells == 600 and binding > 60 and both_free > 60 and pruned > 0


# ======================================================================================
# objectives (contract §7)
# ======================================================================================


class _OtherMode:
    mode = "net_of_something_else"
    label = "stub"
    fixed_cost = 0


def test_an_unsupported_objective_is_refused_naming_the_mode() -> None:
    bundle = PC.synthetic(PC.cp("p1", "A", "B", 10**9, 10**9))
    got, _ = bounded(bundle, PC.case_of("A", "B", 1000), 2, objective=_OtherMode())
    assert got.status is SolveStatus.UNSUPPORTED and got.algorithm == NAME
    assert got.error is not None and "net_of_something_else" in got.error
    assert got.plan is None and got.search_stats == {}  # nothing was solved or claimed


def test_a_negative_cost_on_an_evaluated_plan_is_a_hard_error() -> None:
    """Contract §7's defensive check: an evaluated plan with a negative estimated cost (where
    `score <= gross` fails) raises instead of being absorbed. It can only see evaluated plans;
    the guarantee itself is the objective constructors', which reject a negative cost (the
    negative control of `test_pruning_contract.py` shows what an unchecked one would do)."""
    bundle = PC.synthetic(
        PC.cp("d", "A", "C", 10**9, 10**9),
        PC.cp("x", "A", "B", 10**9, 10**9),
        PC.cp("y", "B", "C", 10**9, 10**9),
    )
    case = PC.case_of("A", "C", 10**6)
    negative_direct = PC.ShapeCost({1: -5, 2: 0})  # the first evaluated plan has cost < 0
    ref, _ = reference(bundle, case, 2, negative_direct)
    assert ref.status is SolveStatus.OK  # the reference just ranks it
    with pytest.raises(ValueError, match="negative estimated cost -5"):
        bounded(bundle, case, 2, negative_direct)
    # a non-negative shape cost is exact, and so is every real objective constructor
    ok = PC.ShapeCost({1: 0, 2: 10})
    (ref_ok, ref_pub), (got_ok, got_pub) = (
        reference(bundle, case, 2, ok),
        bounded(bundle, case, 2, ok),
    )
    assert_exact(ref_ok, got_ok, ref_pub, got_pub)
    with pytest.raises(ValueError, match="fixed_cost"):
        synthetic_fixed_cost(-1)  # the constructor-side half of the guarantee


def test_the_supported_objective_modes_are_exactly_the_three_shipped_ones() -> None:
    assert single_path.BOUND_OBJECTIVES == typing.get_args(ObjectiveMode)


# ======================================================================================
# with the parameter off the reference is the reference
# ======================================================================================


def test_the_parameter_is_default_off_keyword_only_and_the_reference_never_sees_it() -> None:
    parameter = inspect.signature(single_path.solve).parameters["bound_pruning"]
    assert parameter.default is False and parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert single_path.FACTORY.solve is single_path.solve
    assert single_path.FACTORY.prepare is single_path.prepare
    bundle = PC.fixture("routing/cpmm_graph")
    prepared = single_path.prepare(bundle, AlgorithmConfig("single_path", {"max_hops": 3}))
    assert type(prepared) is single_path.PreparedSinglePath  # no extra object, no subclass
    assert not hasattr(prepared, "bound_table")
    case = bundle.cases[0]
    result = single_path.solve(case, SolveContext(bundle, gross_only(), prepared), Budget())
    assert "bound_pruning" not in result.search_stats
    assert "bound_pruning" not in json.dumps(result.to_dict())
    with pytest.raises(TypeError, match="PreparedBoundedSinglePath"):
        single_path.solve(
            case, SolveContext(bundle, gross_only(), prepared), Budget(), bound_pruning=True
        )
    with pytest.raises(TypeError, match="PreparedSinglePath"):
        single_path.solve(case, SolveContext(bundle, gross_only(), None), Budget())


def test_the_bounded_result_is_the_reference_plus_only_the_new_block() -> None:
    """Serialized, the bounded result differs from the reference's in the algorithm name, the
    allowed-to-differ work counters and the added `bound_pruning` key -- nothing else."""
    for _name, bundle, case in all_fixtures()[:12]:
        ref, _ = reference(bundle, case, 3)
        got, _ = bounded(bundle, case, 3)
        a, b = ref.to_dict(), got.to_dict()
        assert b["search"].pop("bound_pruning")["reference"] == "single_path"
        for key in ("paths_evaluated", "paths_pruned", "quotes_executed", "quotes_memoized",
                    "failed_candidates", "paths_incomplete", "incomplete_example"):  # fmt: skip
            a["search"].pop(key), b["search"].pop(key)
        assert (a.pop("algorithm"), b.pop("algorithm")) == ("single_path", NAME)
        assert a == b


# ======================================================================================
# registry, provenance, selection
# ======================================================================================


def test_registered_as_a_custom_opt_in_identity_with_the_references_capabilities() -> None:
    factory = ALGORITHMS[NAME]
    assert factory is single_path_bounded.FACTORY and factory.name == NAME
    assert factory.capabilities == single_path.CAPABILITIES
    assert factory.search_params == single_path.SEARCH_PARAMS and factory.graph_params == ()
    assert factory.options_validator is None and factory.options_preset is None
    assert NAME not in BASE_STRATEGIES and NAME not in OPTIMIZED_STRATEGIES
    assert strategy_group(NAME) == "custom"
    assert R022_ADDITIONS == (NAME,) and NAME not in R021_ADDITIONS
    prov = dict(factory.provenance or {})
    assert prov["experimental"] is True and prov["opt_in"] is True
    assert prov["reference"] == "single_path" and prov["issue"] == "WHI-1599"
    assert prov["identity"] == (
        "exact acceleration of single_path; identical plan under non-binding budgets; "
        "not a new heuristic"
    )
    assert "R022-Q02/1" in prov["contract"] and prov["claims"] and prov["not_claimed"]
    assert "any speedup" in prov["not_claimed"]
    json.dumps(prov)  # recorded in the resolved profile
    with pytest.raises(OptionsError, match="accepts no algorithm_options"):
        factory.prepare(  # type: ignore[misc]
            PC.synthetic(PC.cp("p", "A", "B", 10, 10)),
            AlgorithmConfig(NAME, {"max_hops": 2}, {"x": 1}),
        )


def test_the_factory_is_picklable_by_reference_for_the_worker() -> None:
    clone = pickle.loads(pickle.dumps(single_path_bounded.FACTORY))
    assert clone == single_path_bounded.FACTORY
    assert clone.solve is single_path_bounded.solve and clone.prepare is single_path.prepare_bounded
    for fn in (single_path_bounded.solve, single_path.prepare_bounded):
        assert "<lambda>" not in fn.__qualname__ and "<locals>" not in fn.__qualname__


def test_all_appends_it_after_the_021_identities_and_profile_replays_literally() -> None:
    source = yaml.safe_load((REPO / "config" / "daily_gross.yaml").read_text())
    document, profile = derive(source, "all", source_path="s", source_sha256="x")
    names = list(profile.algorithms)
    assert names[-1 - len(R021_ADDITIONS) : -1] == list(R021_ADDITIONS) and names[-1] == NAME
    assert document["selection"]["groups"]["custom"][-1] == NAME
    assert "algorithm_options" in document and NAME not in document["algorithm_options"]
    config = profile.resolved()["algorithm_config"][NAME]
    assert config["provenance"]["reference"] == "single_path"
    assert config["capabilities"] == ALGORITHMS["single_path"].capabilities.to_dict()
    for mode in ("base", "optimized"):
        assert NAME not in derive(source, mode, source_path="s", source_sha256="x")[1].algorithms
    # a saved earlier profile never gains it under `--strategies profile`
    saved = json.loads(json.dumps(document))
    saved["algorithms"].remove(NAME)
    saved["selection"]["groups"]["custom"].remove(NAME)
    literal, replayed = derive(saved, "profile", source_path="s", source_sha256="x")
    assert literal == saved and NAME not in replayed.algorithms


# ======================================================================================
# CLI: run, quote --details, replay, order-check, report
# ======================================================================================


def _pair_profile(tmp_path: Path) -> Path:
    doc = read_profile_document(REPO / "config" / "daily_gross.yaml")
    doc["algorithms"] = ["single_path", NAME]
    doc["search"] = {"max_hops": 3, "max_splits": 4, "percent_step": 5}
    doc["measurement"] = {"warmup": 0, "repeats": 1, "seed": 7, "order": "fixed",
                          "memory_pass": False}  # fmt: skip
    path = tmp_path / "pair.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    parse_profile(doc, str(path))
    return path


def test_cli_run_shows_both_strategies_separately_with_counters_and_replays_exactly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = _pair_profile(tmp_path)
    results = tmp_path / "results"
    argv = ["run", "--bundle", str(MIXED), "--profile", str(profile), "--results-dir",
            str(results), "--strategies", "profile"]  # fmt: skip
    assert main.main(argv) == 0
    (run_dir,) = results.iterdir()
    manifest = load_manifest(run_dir)
    assert list(manifest.algorithms) == ["single_path", NAME]
    records = load_case_records(run_dir)
    by_algorithm: dict[str, list[dict[str, Any]]] = {"single_path": [], NAME: []}
    for record in records:
        by_algorithm[record["algorithm"]].append(record)
    assert (
        len(by_algorithm[NAME])
        == len(by_algorithm["single_path"])
        == len(manifest.measurement["case_order"])
    )
    pruned = 0
    for ref_record, record in zip(by_algorithm["single_path"], by_algorithm[NAME], strict=True):
        assert ref_record["case_id"] == record["case_id"]
        assert "bound_pruning" not in ref_record["search"]
        assert record["status"] == ref_record["status"]
        assert record["score"] == ref_record["score"]
        assert record["evaluation"] == ref_record["evaluation"]
        assert record["candidates_considered"] == ref_record["candidates_considered"]
        if record["solver_reported"] is not None and record["status"] == "ok":
            block = record["search"]["bound_pruning"]
            assert block["contract"] == "R022-Q02/1" and block["rule"] == "S1"
            assert block["exactness"]["label"] == "exact"
            assert record["quotes"]["counted"] <= ref_record["quotes"]["counted"]
            pruned += block["pruned_bound"]
    assert pruned > 0
    assert manifest.resolved_profile["algorithm_config"][NAME]["provenance"]["reference"] == (
        "single_path"
    )
    # a saved record replays literally (the same resolved profile, identical deterministic view)
    capsys.readouterr()
    replay = manifest.replay_command.split("main.py", 1)[1].split()
    assert replay[-2:] == ["--strategies", "profile"]
    assert main.main(replay) == 0
    again = [p for p in results.iterdir() if p != run_dir]
    assert len(again) == 1
    assert load_manifest(again[0]).resolved_profile == manifest.resolved_profile
    assert compare_runs(run_dir, again[0]) == []
    assert main.main(["order-check", str(run_dir), str(again[0])]) == 0
    # the report keeps them as two rows with their own labels
    out = tmp_path / "report"
    assert main.main(["report", str(run_dir), "--output", str(out)]) == 0
    html = (out / "report.html").read_text()
    assert f"<code>{NAME}</code>" in html and "Bound-pruned strategy" in html


def test_cli_quote_details_runs_one_solve_per_strategy_and_prints_the_counters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = _pair_profile(tmp_path)
    args = ["quote", "--bundle", str(TRACKED_CORPUS), "--profile", str(profile), "--token-in",
            "USDC", "--token-out", "USDT0", "--amount", "1500.25", "--quotes-dir",
            str(tmp_path / "q"), "--strategies", "profile", "--details"]  # fmt: skip
    assert main.main(args) == 0
    out = capsys.readouterr().out
    match = re.search(r"\(run (\S+)\)", out)
    assert match
    records = load_case_records(Path(match.group(1)))
    assert [r["algorithm"] for r in records] == ["single_path", NAME]  # one solve each
    assert "[single_path] ok" in out and f"[{NAME}] ok" in out
    section = out.split(f"[{NAME}] ok", 1)[1]
    assert "bound pruning (R022-Q02/1 rule S1: exact acceleration of single_path)" in section
    assert "pruned by bound" in section and "bound table (built in preparation)" in section
    assert "exactness: exact" in section
    assert "bound pruning" not in out.split(f"[{NAME}] ok", 1)[0]  # not on the reference
    block = records[1]["search"]["bound_pruning"]
    assert f"pruned by bound {block['pruned_bound']} (not failures)" in section


# ======================================================================================
# the tuning split and the tracked real fixture: every case, non-binding budgets
# ======================================================================================


def _differential_over(
    bundle: SnapshotBundle, label: str, objective: ObjectiveContext | None = None
) -> int:
    """Every case equals the reference; returns how many plans were net-ranked."""
    objective = objective or gross_only()
    prepared_ref = single_path.prepare(bundle, AlgorithmConfig("single_path", {"max_hops": 3}))
    prepared_bnd = single_path.prepare_bounded(bundle, AlgorithmConfig(NAME, {"max_hops": 3}))
    compared = pruned = no_bound = ok_cases = ranked = 0
    quotes_ref = quotes_bnd = evaluated_ref = evaluated_bnd = 0
    for case in bundle.cases:
        sink_r: list[RoutePlan] = []
        sink_b: list[RoutePlan] = []
        ref = single_path.solve(
            case, SolveContext(bundle, objective, prepared_ref, candidate_sink=sink_r.append),
            Budget(),
        )  # fmt: skip
        got = single_path_bounded.solve(
            case, SolveContext(bundle, objective, prepared_bnd, candidate_sink=sink_b.append),
            Budget(),
        )  # fmt: skip
        assert_exact(ref, got, sink_r, sink_b)
        block = got.search_stats["bound_pruning"]
        assert block["exactness"]["label"] == "exact"
        compared += 1
        ok_cases += ref.status is SolveStatus.OK
        ranked += ref.evaluation is not None and ref.evaluation.estimated_net_output is not None
        pruned += block["pruned_bound"]
        no_bound += block["bound_no_bound"]
        quotes_ref += ref.search_stats["quotes_executed"]
        quotes_bnd += got.search_stats["quotes_executed"]
        evaluated_ref += ref.search_stats["paths_evaluated"]
        evaluated_bnd += got.search_stats["paths_evaluated"]
    print(
        f"DIFFERENTIAL {label}: cases={compared} ok={ok_cases} pruned_bound={pruned} "
        f"no_bound_evals={no_bound} quotes {quotes_ref} -> {quotes_bnd} "
        f"({quotes_bnd - quotes_ref:+d}) paths_evaluated {evaluated_ref} -> {evaluated_bnd}"
    )
    assert compared == len(bundle.cases) > 0 and ok_cases > 0
    assert pruned > 0 and quotes_bnd <= quotes_ref
    return ranked


def test_every_case_of_the_tracked_real_fixture_equals_the_reference() -> None:
    bundle = load_bundle(TRACKED_CORPUS)
    assert len(bundle.cases) == 96
    _differential_over(bundle, "tracked-fixture")


def test_the_real_empirical_cost_objective_is_exact_on_the_tracked_real_fixture() -> None:
    """The shipped net objective (frozen cost model, price context of the bundle): ranked plans
    score `gross - cost`, unranked ones `gross - 2**257`; both are `<= gross`, so S1 stays exact."""
    model = load_cost_model(
        REPO / "config" / "costs" / "mantle-101082044-cost-v1.json",
        expected_sha256="50e89727d6b6ac869c14c837c7cdec8099efc897d201794640912f3f6b4b7e71",
    )
    bundle = load_bundle(TRACKED_CORPUS)
    objective = empirical_cost(model).bind(bundle)
    ranked = _differential_over(bundle, "tracked-fixture empirical_cost", objective)
    assert ranked > 0  # net-ranked plans (score = gross - cost) are really exercised


def test_every_tuning_split_case_equals_the_reference_under_non_binding_budgets() -> None:
    path = os.environ.get(TUNING_ENV)
    if not path:
        pytest.skip(f"set {TUNING_ENV} to the frozen bundle_tuning (data/ is primary-clone only)")
    assert Path(path).is_dir(), f"{TUNING_ENV}={path!r} is not a directory"
    bundle = load_bundle(path)
    assert len(bundle.cases) == 96 and len(bundle.pools) == 143
    _differential_over(bundle, "tuning-split")


def test_the_solver_uses_the_shared_helper_module() -> None:
    """WHI-1600 reuses `pools.bounds`: the solver holds no copy of the formulae."""
    assert vars(single_path)["bound_out"] is bounds_module.bound_out
    assert vars(single_path)["build_bounds"] is bounds_module.build_bounds

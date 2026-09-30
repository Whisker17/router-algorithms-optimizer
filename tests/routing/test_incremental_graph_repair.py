"""WHI-1554: the registered `incremental_graph_repair` factory (suffix-repair.md §4-§9, §12).

Every behavioural check here runs the REAL registered factory (`get_algorithm(NAME)`: its
public `prepare` and `solve`) or the real module's own pieces, never the WHI-1553
specification `repair_solve`. From the research module only the fixture bundles and the
independent oracle are reused (its own adjacency, path enumeration, Kahn cycle test, hand
integer CPMM formula and aggregate-flow accounting; it imports no search, solver or
evaluator code), so expected values come from the oracle, the frozen fixture records, the
unchanged reference `incremental_graph.solve` and the plain evaluator.

Mutation and fault seams are test-only `monkeypatch`es of the module's own names
(`restore`, `evaluate`, `merged_plan`) or of the embedded `path_split.solve`; the
production module patches nothing.
"""

from __future__ import annotations

import dataclasses
import json
import random
import shlex
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import test_suffix_repair_contract as research  # sibling: fixtures + independent oracle only
import yaml

import benchmark.profile as profile_module
from benchmark.diagnostics import CheckContext, check_diagnostics, diagnostics_view
from benchmark.objective import ObjectiveContext, gross_only, synthetic_fixed_cost
from benchmark.profile import ProfileError, load_profile, parse_profile, preset_options
from benchmark.strategies import R021_ADDITIONS, derive
from pools.quote import QuoteLimitExceeded, metered_quotes
from routing.algorithms import incremental_graph, path_split
from routing.algorithms import incremental_graph_repair as igr
from routing.algorithms.base import (
    AlgorithmConfig,
    Budget,
    OptionsError,
    SolveContext,
    SolveResult,
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
from routing.evaluator import EvalStatus, Evaluation
from routing.evaluator import evaluate as reference_evaluate
from routing.plan import RoutePlan
from routing.search import QuoteCache, enumerate_paths
from snapshot.models import Case, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
NAME = "incremental_graph_repair"
FACTORY = get_algorithm(NAME)
PRESET = {
    "repair": True,
    "max_checkpoints": 4,
    "alternatives_per_checkpoint": 2,
    "max_repair_attempts": 8,
}
OFF = {**PRESET, "repair": False}
NON_STRING_KEY: dict[Any, Any] = {1: 2, **PRESET}
STRESS = {
    "repair": True,
    "max_checkpoints": 16,
    "alternatives_per_checkpoint": 4,
    "max_repair_attempts": 64,
}
MIXED_PARAMS = {"max_hops": 2, "max_splits": 4, "percent_step": 5, "chunks": 10}
FIXTURES = ("structural_trap", "twin_pools", "carry_and_zero_flow", "order_metadata")
PROFILES = REPO / "config" / "incremental_graph_repair"
# incremental_graph's own search_stats keys; the repair adds exactly these.
EXTRA_KEYS = {"repair", "evaluations", "consistency_failure", "r021"}


def _context(
    bundle: SnapshotBundle,
    params: Mapping[str, int],
    options: Mapping[str, Any] = PRESET,
    sink: list[RoutePlan] | None = None,
    objective: ObjectiveContext | None = None,
) -> SolveContext:
    assert FACTORY.prepare is not None
    prepared = FACTORY.prepare(bundle, AlgorithmConfig(NAME, dict(params), dict(options)))
    return SolveContext(
        bundle,
        objective or gross_only(),
        prepared,
        candidate_sink=None if sink is None else sink.append,
    )


def _solve(
    bundle: SnapshotBundle,
    case: Case,
    params: Mapping[str, int],
    options: Mapping[str, Any] = PRESET,
    budget: Budget | None = None,
    sink: list[RoutePlan] | None = None,
    objective: ObjectiveContext | None = None,
) -> SolveResult:
    return FACTORY.solve(
        case, _context(bundle, params, options, sink, objective), budget or Budget()
    )


def _fixture(name: str, **kw: Any) -> tuple[SolveResult, SnapshotBundle, Case, dict[str, Any]]:
    bundle, case, spec = research.fixture_case(name)
    return _solve(bundle, case, spec["settings"], **kw), bundle, case, spec


def _outcomes(result: SolveResult) -> list[str]:
    return [a["outcome"] for a in result.search_stats["repair"]["attempts"]]


def _true_gross(bundle: SnapshotBundle, case: Case, plan: RoutePlan) -> int:
    ev = reference_evaluate(bundle, case, plan, gross_only())
    assert ev.status is EvalStatus.OK
    return ev.gross_output


def _search(
    bundle: SnapshotBundle, case: Case, params: Mapping[str, int], budget: Budget | None = None
) -> tuple[igr._Search, QuoteCache]:
    """The module's own reference loop on a fresh cache, built as its stage 2."""
    prepared = FACTORY.prepare(bundle, AlgorithmConfig(NAME, dict(params), PRESET))  # type: ignore[misc]
    index = prepared.graph.path_split.single_path.index
    cache = QuoteCache(bundle)
    paths = list(enumerate_paths(index, case.token_in, case.token_out, params["max_hops"]))
    amounts = incremental_graph.chunk_amounts(case.amount_in, params["chunks"])
    return igr._Search(bundle, paths, amounts, cache, budget or Budget()), cache


def _registered_neighborhood(
    bundle: SnapshotBundle, case: Case, params: Mapping[str, int], budget: Budget
) -> tuple[list[tuple[int, int]], int]:
    """The §5.2 neighborhood restated here (not the module's helpers): structural decisions
    latest first, at most `max_checkpoints`; per checkpoint the rescored non-incumbent paths
    with a POSITIVE marginal by marginal then index, at most `alternatives_per_checkpoint`;
    at most `max_repair_attempts` in total. Returns the (checkpoint, path index) order and
    how many admissible zero-marginal alternatives the rule excluded."""
    search, _ = _search(bundle, case, params, budget)
    run = search.run(igr.EMPTY, igr.Counters(), trace=True)
    structural = [i for i, d in enumerate(run.decisions) if d.new_edges][::-1]
    order: list[tuple[int, int]] = []
    zero = 0
    for i in structural[: PRESET["max_checkpoints"]]:
        flows, edges, _ = igr.restore(run.checkpoints[i])
        d = run.decisions[i]
        scored = search.score(flows, edges, d.amount, igr.Counters())
        others = [s for s in scored if s.index != d.path_index]
        zero += sum(s.marginal == 0 for s in others)
        ranked = sorted((s for s in others if s.marginal > 0), key=lambda s: (-s.marginal, s.index))
        order += [(i, s.index) for s in ranked[: PRESET["alternatives_per_checkpoint"]]]
    return order[: PRESET["max_repair_attempts"]], zero


def _rebuild(
    bundle: SnapshotBundle, case: Case, params: Mapping[str, int], i: int, alt: int
) -> tuple[igr._Search, igr.Run, igr.Run]:
    """The incumbent trace and one logged repair candidate, rebuilt on a fresh search."""
    search, _ = _search(bundle, case, params)
    incumbent = search.run(igr.EMPTY, igr.Counters(), trace=True)
    flows, edges, _ = igr.restore(incumbent.checkpoints[i])
    scored = search.score(flows, edges, incumbent.decisions[i].amount, igr.Counters())
    forced = next(s for s in scored if s.index == alt)
    return search, incumbent, search.run(incumbent.checkpoints[i], igr.Counters(), forced=forced)


def _sequence(search: igr._Search, run: igr.Run) -> list[tuple[str, ...]]:
    """The run in the oracle's format: per nonzero chunk its pool ids or `("carry",)`."""
    by_position = {d.position: d for d in run.decisions}
    return [
        tuple(e.pool_id for e in by_position[k].path) if k in by_position else ("carry",)
        for k, a in enumerate(search.amounts)
        if a > 0
    ]


def _reference_differential(
    bundle: SnapshotBundle, case: Case, params: Mapping[str, int], budget: Budget
) -> SolveResult:
    """`repair: false` IS `incremental_graph.solve`: status, plan, evaluation, score, error,
    candidate counts, every one of its search_stats, publications and the worker meter."""
    ref_sink: list[RoutePlan] = []
    off_sink: list[RoutePlan] = []
    ref_ctx = SolveContext(
        bundle,
        gross_only(),
        incremental_graph.prepare(bundle, AlgorithmConfig(incremental_graph.NAME, dict(params))),
        candidate_sink=ref_sink.append,
    )
    with metered_quotes(None) as ref_meter:
        ref = incremental_graph.solve(case, ref_ctx, budget)
    with metered_quotes(None) as off_meter:
        off = _solve(bundle, case, params, OFF, budget, off_sink)
    for key in (
        "status",
        "plan",
        "evaluation",
        "score",
        "error",
        "candidates_considered",
        "candidates_truncated",
    ):
        assert getattr(off, key) == getattr(ref, key), key
    assert off.algorithm == NAME and ref.algorithm == incremental_graph.NAME
    assert set(off.search_stats) - set(ref.search_stats) == EXTRA_KEYS
    for key, value in ref.search_stats.items():
        assert off.search_stats[key] == value, key
    assert off_sink == ref_sink and off_meter.counted == ref_meter.counted
    assert off.search_stats["repair"]["stop"] == "disabled"
    assert off.search_stats["evaluations"]["repair"] == 0
    return off


# ------------------------------------------------------------------ registration and options


def test_registered_as_a_custom_options_identity_appended_by_all() -> None:
    assert ALGORITHMS[NAME] is igr.FACTORY is FACTORY
    assert NAME not in BASE_STRATEGIES and NAME not in OPTIMIZED_STRATEGIES
    assert profile_module.strategy_group(NAME) == "custom"
    assert R021_ADDITIONS == (NAME,)
    assert FACTORY.options_validator is igr.validate_options  # module-level (picklable)
    assert FACTORY.capabilities == incremental_graph.CAPABILITIES
    assert (FACTORY.search_params, FACTORY.graph_params) == (
        incremental_graph.SEARCH_PARAMS,
        incremental_graph.GRAPH_PARAMS,
    )
    # the reference identity is untouched: no options, no preset, same factory object
    assert ALGORITHMS[incremental_graph.NAME] is incremental_graph.FACTORY
    assert incremental_graph.FACTORY.options_validator is None


def test_preset_file_is_the_pinned_v1_and_tampering_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = dict(FACTORY.options_preset or {})
    assert pin == {
        "path": "config/incremental_graph_repair/preset_v1.yaml",
        "sha256": igr.PRESET["sha256"],
        "key": "R021-P08-incremental_graph_repair",
        "version": 1,
    }
    assert preset_options(FACTORY) == PRESET
    doc = yaml.safe_load((REPO / pin["path"]).read_text())
    assert (doc["key"], doc["version"], doc["algorithm"], doc["options"]) == (
        pin["key"],
        1,
        NAME,
        PRESET,
    )
    (tmp_path / "config" / "incremental_graph_repair").mkdir(parents=True)
    changed = (REPO / pin["path"]).read_text().replace("max_checkpoints: 4", "max_checkpoints: 5")
    (tmp_path / pin["path"]).write_text(changed)
    monkeypatch.setattr(profile_module, "REPO_ROOT", tmp_path)
    with pytest.raises(ProfileError, match="differs from the pin"):
        preset_options(FACTORY)


@pytest.mark.parametrize(
    "options",
    [
        {k: v for k, v in PRESET.items() if k != "max_checkpoints"},  # required, no default
        {},
        {**PRESET, "window": 3},  # unknown
        {**PRESET, "repair": 1},  # int for bool
        {**PRESET, "repair": "true"},
        {**PRESET, "repair": None},
        {**PRESET, "max_checkpoints": True},  # bool for int
        {**PRESET, "max_checkpoints": 4.0},
        {**PRESET, "max_checkpoints": float("nan")},
        {**PRESET, "max_repair_attempts": float("inf")},
        {**PRESET, "max_checkpoints": 0},
        {**PRESET, "max_checkpoints": 65},
        {**PRESET, "alternatives_per_checkpoint": 0},
        {**PRESET, "alternatives_per_checkpoint": 17},
        {**PRESET, "max_repair_attempts": 257},
        {**PRESET, "max_repair_attempts": -1},
        {**PRESET, "max_repair_attempts": 10**400},
        NON_STRING_KEY,
        *({**PRESET, key: 1} for key in sorted(research.RESERVED_KEYS)),
    ],
)
def test_invalid_options_are_refused_by_every_entry_point(options: dict[Any, Any]) -> None:
    """The shared `validated_options`, the factory's public `prepare` and profile loading
    refuse the same values; nothing is defaulted, clamped or coerced."""
    bundle, _, spec = research.fixture_case("structural_trap")
    with pytest.raises(OptionsError):
        validated_options(FACTORY, options)
    with pytest.raises(OptionsError):
        _context(bundle, spec["settings"], options)
    doc = yaml.safe_load((PROFILES / "repair_on.yaml").read_text())
    doc["algorithm_options"] = {NAME: options}
    with pytest.raises(ProfileError):
        parse_profile(doc, "<test>")


def test_valid_options_bounds_and_profiles_resolve_with_content_derived_identity() -> None:
    for options in (
        PRESET,
        OFF,
        STRESS,
        {
            "repair": True,
            "max_checkpoints": 1,
            "alternatives_per_checkpoint": 1,
            "max_repair_attempts": 1,
        },
        {
            "repair": False,
            "max_checkpoints": 64,
            "alternatives_per_checkpoint": 16,
            "max_repair_attempts": 256,
        },
    ):
        assert validated_options(FACTORY, options) == options
    profiles = {
        name: load_profile(PROFILES / f"{name}.yaml")
        for name in ("repair_on", "repair_off", "stress")
    }
    entries = {name: p.algorithm_options[NAME] for name, p in profiles.items()}
    assert [entries[n]["options"] for n in ("repair_on", "repair_off", "stress")] == [
        PRESET,
        OFF,
        STRESS,
    ]
    assert entries["repair_on"]["source"] == {"kind": "preset", **dict(igr.PRESET)}
    assert entries["repair_off"]["source"] == entries["stress"]["source"] == {"kind": "override"}
    for entry in entries.values():
        assert entry["settings_sha256"] == settings_sha256(entry["options"])
    assert len({e["settings_sha256"] for e in entries.values()}) == 3
    for p in profiles.values():  # shared settings identical; the reference gets no options
        assert list(p.algorithms) == [incremental_graph.NAME, NAME]
        assert dict(p.algorithm_config(incremental_graph.FACTORY).options) == {}
        assert (
            p.algorithm_config(FACTORY).params
            == p.algorithm_config(incremental_graph.FACTORY).params
        )
    # the prepared options are read-only and the legacy prepare received none
    bundle, _, _ = research.fixture_case("structural_trap")
    prepared = FACTORY.prepare(bundle, profiles["stress"].algorithm_config(FACTORY))  # type: ignore[misc]
    assert dict(prepared.options) == STRESS
    with pytest.raises(TypeError):
        prepared.options["repair"] = False


def test_stress_values_never_reach_an_ordinary_all_comparison() -> None:
    """`all` over every standard profile appends the identity with the preset only; the
    stress values run only from their own explicitly selected profile."""
    for name in (
        "daily_gross.yaml",
        "daily.yaml",
        "full_gross.yaml",
        "full.yaml",
        "corpus_incremental_graph_smoke.yaml",
    ):
        doc = yaml.safe_load((REPO / "config" / name).read_text())
        document, profile = derive(doc, "all", source_path=name, source_sha256="0" * 64)
        assert document["algorithms"][-1] == NAME
        assert document["algorithm_options"] == {NAME: PRESET}
        assert profile.algorithm_options[NAME]["source"]["kind"] == "preset"
    stress = yaml.safe_load((PROFILES / "stress.yaml").read_text())
    document, _ = derive(stress, "profile", source_path="s", source_sha256="0" * 64)
    assert document == stress


# ------------------------------------------------------------------ fidelity (repair off)


def test_repair_off_is_incremental_graph_on_fixtures_real_state_and_random_graphs() -> None:
    for name in FIXTURES:
        bundle, case, spec = research.fixture_case(name)
        _reference_differential(bundle, case, spec["settings"], Budget())
    real = research.mixed()
    for c in real.cases:
        _reference_differential(real, c, MIXED_PARAMS, Budget())
    rng = random.Random(20261554)
    for _ in range(80):
        pools, case, params, budget = research.random_instance(rng)
        _reference_differential(research.cp_bundle(pools), case, params, budget)


def test_stages_one_and_two_are_unchanged_by_the_repair() -> None:
    """With repair on, every incumbent-stage key is still the reference's; only the whole-
    solve ledger and what an accepted repair replaced (source, topology, truncation) move."""
    moved = {
        "quotes_executed",
        "quotes_memoized",
        "chosen_source",
        "topology",
        "truncated_by",
        "truncated_stages",
    }
    for name in FIXTURES:
        off, *_ = _fixture(name, options=OFF)
        on, *_ = _fixture(name)
        for key in set(off.search_stats) - EXTRA_KEYS - moved:
            assert on.search_stats[key] == off.search_stats[key], (name, key)


# ------------------------------------------------------------------ checkpoints and restore


def _round_trip(bundle: SnapshotBundle, case: Case, params: Mapping[str, int]) -> int:
    search, cache = _search(bundle, case, params)
    incumbent = search.run(igr.EMPTY, igr.Counters(), trace=True)
    final = igr.freeze(
        len(search.amounts), 0, incumbent.flows, incumbent.edges, incumbent.decisions
    )
    quotes = cache.misses
    for i, cp in enumerate(incumbent.checkpoints):
        assert cp.decisions == tuple(incumbent.decisions[:i])
        assert cp.position == incumbent.decisions[i].position
        assert cp.carry == incumbent.decisions[i].amount - search.amounts[cp.position]
        resumed = search.run(cp, igr.Counters())
        assert resumed.status == incumbent.status
        again = igr.freeze(len(search.amounts), 0, resumed.flows, resumed.edges, resumed.decisions)
        assert again == final  # values, order, insertion order, zero-input records, edges
        assert list(resumed.flows) == list(incumbent.flows)
        if incumbent.status == "complete":
            assert incremental_graph.merged_plan(
                case, resumed.flows.values()
            ) == incremental_graph.merged_plan(case, incumbent.flows.values())
    assert cache.misses == quotes  # a restore re-asks only memoized original-state quotes
    return len(incumbent.checkpoints)


def test_checkpoint_round_trip_is_exact_everywhere() -> None:
    total = sum(
        _round_trip(*research.fixture_case(n)[:2], research.fixture_case(n)[2]["settings"])
        for n in FIXTURES
    )
    real = research.mixed()
    total += sum(_round_trip(real, c, MIXED_PARAMS) for c in real.cases)
    rng = random.Random(20261555)
    for _ in range(60):
        pools, case, params, _ = research.random_instance(rng)
        total += _round_trip(research.cp_bundle(pools), case, params)
    assert total > 150


def test_checkpoint_keeps_carry_zero_input_flows_and_order_slots() -> None:
    bundle, case, spec = research.fixture_case("carry_and_zero_flow")
    search, _ = _search(bundle, case, spec["settings"])
    run = search.run(igr.EMPTY, igr.Counters(), trace=True)
    want = spec["repository"]
    assert [[f.edge.pool_id, f.amount_in, f.amount_out, f.order] for f in run.flows.values()] == (
        want["final_flows"]
    )
    assert [[c.position, c.carry] for c in run.checkpoints] == want[
        "checkpoint_positions_and_carry"
    ]
    zero = run.flows["p1"]
    assert zero.amount_in == 0 and (zero.edge.token_in, zero.edge.token_out) in run.edges
    assert "p1" not in {
        s.pool_id for s in incremental_graph.merged_plan(case, run.flows.values()).steps
    }
    final = igr.freeze(len(search.amounts), 0, run.flows, run.edges, run.decisions)
    assert igr.FlowRecord("p1", "A", "D", 0, 0, 3) in final.flows
    restored, edges, decisions = igr.restore(final)
    assert restored == run.flows and edges == run.edges and decisions == run.decisions
    assert all(restored[k] is not run.flows[k] for k in run.flows)  # fresh objects
    # the factory on this fixture: repair on is consistent and never below the control
    on, *_ = _fixture("carry_and_zero_flow")
    off, *_ = _fixture("carry_and_zero_flow", options=OFF)
    assert on.search_stats["repair"]["consistency_failures"] == 0
    assert on.score is not None and off.score is not None and on.score >= off.score


def test_order_metadata_is_part_of_the_restored_state() -> None:
    bundle, case, spec = research.fixture_case("order_metadata")
    search, _ = _search(bundle, case, spec["settings"])
    run = search.run(igr.EMPTY, igr.Counters(), trace=True)
    cp = run.checkpoints[spec["checkpoint"]]
    n = len(cp.flows)
    renumbered = dataclasses.replace(
        cp, flows=tuple(dataclasses.replace(f, order=n - 1 - f.order) for f in cp.flows)
    )
    exact, stale = search.run(cp, igr.Counters()), search.run(renumbered, igr.Counters())
    good = incremental_graph.merged_plan(case, exact.flows.values())
    bad = incremental_graph.merged_plan(case, stale.flows.values())
    assert good == incremental_graph.merged_plan(case, run.flows.values()) and bad != good
    assert (
        _true_gross(bundle, case, good)
        == _true_gross(bundle, case, bad)
        == spec["repository"]["gross"]
    )


def test_subtracting_a_non_suffix_chunk_leaves_stale_aggregates() -> None:
    bundle, case, spec = research.fixture_case("structural_trap")
    search, _ = _search(bundle, case, spec["settings"])
    run = search.run(igr.EMPTY, igr.Counters(), trace=True)
    before = {f.pool_id: f for f in run.checkpoints[0].flows}
    after0 = {f.pool_id: f for f in run.checkpoints[1].flows}
    flows = dict(run.flows)
    for pid, rec in after0.items():
        base = before.get(pid)
        f = flows[pid]
        flows[pid] = incremental_graph.PoolFlow(
            f.edge,
            f.amount_in - (rec.amount_in - (base.amount_in if base else 0)),
            f.amount_out - (rec.amount_out - (base.amount_out if base else 0)),
            f.order,
        )
    assert any(
        f.amount_in > 0
        and f.amount_out != research.oracle_out(spec["pools"][p], f.edge.token_in, f.amount_in, 30)
        for p, f in flows.items()
    )
    try:
        plan = incremental_graph.merged_plan(case, flows.values())
    except ValueError:
        return
    ev = reference_evaluate(bundle, case, plan, gross_only())
    assert ev.status is not EvalStatus.OK or ev.gross_output != igr.accounted_gross(case, flows)


# ------------------------------------------------------------------ stale-state mutations


def _mutate_repair_restores(monkeypatch: pytest.MonkeyPatch, mutant: Callable[..., Any]) -> None:
    """Replace the module's `restore` for every repair-stage restore (checkpoint rescoring
    and suffix rebuild); the incumbent's own start (`EMPTY`) is left exact."""
    real = igr.restore
    monkeypatch.setattr(igr, "restore", lambda cp: real(cp) if cp is igr.EMPTY else mutant(cp))


def test_mutation_stale_flows_is_caught_by_the_round_trip_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restore that keeps the incumbent's final flows: the rescored checkpoint no longer
    reproduces the recorded decision; the repair stops before any attempt and the valid
    incumbent stays (never a candidate built on stale flows)."""
    off, bundle, case, spec = _fixture("structural_trap", options=OFF)
    search, _ = _search(bundle, case, spec["settings"])
    final = search.run(igr.EMPTY, igr.Counters()).flows
    real_restore = igr.restore

    def stale(cp: igr.Checkpoint) -> Any:
        _, edges, decisions = real_restore(cp)
        return {k: dataclasses.replace(v) for k, v in final.items()}, edges, decisions

    _mutate_repair_restores(monkeypatch, stale)
    got, *_ = _fixture("structural_trap")
    r = got.search_stats["repair"]
    assert (r["stop"], r["consistency_failures"], r["repair_attempts"]) == (
        "consistency_failure",
        1,
        0,
    )
    assert got.search_stats["consistency_failure"]["stage"] == "repair_restore"
    assert got.status is SolveStatus.OK and got.plan == off.plan and got.score == off.score


def test_mutation_stale_token_edges_loses_the_valid_improvement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restore that keeps the incumbent's final token edges: the fix path S-E-B-D is
    (wrongly) inadmissible and never built; only the exact restore finds the oracle optimum."""
    bundle, case, spec = research.fixture_case("structural_trap")
    search, _ = _search(bundle, case, spec["settings"])
    run = search.run(igr.EMPTY, igr.Counters(), trace=True)
    fix = next(
        j for j, p in enumerate(search.paths) if [e.pool_id for e in p] == ["es", "be", "bd"]
    )
    assert incremental_graph.creates_cycle(run.edges, search.paths[fix])
    assert not incremental_graph.creates_cycle(
        set(run.checkpoints[0].token_edges), search.paths[fix]
    )
    exact, *_ = _fixture("structural_trap")
    real_restore = igr.restore
    _mutate_repair_restores(
        monkeypatch,
        lambda cp: real_restore(dataclasses.replace(cp, token_edges=frozenset(run.edges))),
    )
    stale, *_ = _fixture("structural_trap")
    assert fix not in {a["alternative"] for a in stale.search_stats["repair"]["attempts"]}
    assert stale.score == 94_153_558 < spec["oracle"]["best_gross"] == exact.score
    assert fix in {a["alternative"] for a in exact.search_stats["repair"]["attempts"]}
    assert stale.plan is not None and stale.score == _true_gross(bundle, case, stale.plan)


def test_mutation_subtracting_restore_never_publishes_an_invalid_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restore that rolls back by subtracting the decision's recorded pool deltas from the
    final state (keeping later chunks): whatever it builds, every published and returned
    plan is a complete valid plan whose score is its independent replay."""
    bundle, case, spec = research.fixture_case("structural_trap")
    search, _ = _search(bundle, case, spec["settings"])
    run = search.run(igr.EMPTY, igr.Counters(), trace=True)
    real_restore = igr.restore

    def subtracting(cp: igr.Checkpoint) -> Any:
        i = len(cp.decisions)
        before = {f.pool_id: f for f in cp.flows}
        after = (
            {f.pool_id: f for f in run.checkpoints[i + 1].flows}
            if i + 1 < len(run.checkpoints)
            else {k: v for k, v in run.flows.items()}
        )
        flows = {k: dataclasses.replace(v) for k, v in run.flows.items()}
        for pid, rec in after.items():
            base = before.get(pid)
            f = flows[pid]
            flows[pid] = incremental_graph.PoolFlow(
                f.edge,
                f.amount_in - (rec.amount_in - (base.amount_in if base else 0)),
                f.amount_out - (rec.amount_out - (base.amount_out if base else 0)),
                f.order,
            )
        _, edges, decisions = real_restore(cp)
        return flows, edges, decisions

    _mutate_repair_restores(monkeypatch, subtracting)
    sink: list[RoutePlan] = []
    got = _solve(bundle, case, spec["settings"], sink=sink)
    assert got.status is SolveStatus.OK and got.plan is not None
    assert got.score == _true_gross(bundle, case, got.plan)
    for plan in sink:
        research.full_fill(case, reference_evaluate(bundle, case, plan, gross_only()))
    assert got.search_stats["repair"]["stop"] == "consistency_failure"


def test_stale_scores_and_reuse_closures_would_be_wrong_so_a_restore_rescores() -> None:
    """A score computed on another committed state carries stale aggregates (conservation
    or replay = accounting catches it); `_ExactReuse`'s monotone rejections survive no
    rollback. The factory never uses either: it rescores on the restored state."""
    bundle, case, spec = research.fixture_case("structural_trap")
    search, _ = _search(bundle, case, spec["settings"])
    run = search.run(igr.EMPTY, igr.Counters(), trace=True)
    d0 = run.decisions[0]
    stale = search.score(run.flows, set(), d0.amount, igr.Counters())
    fix = next(s for s in stale if [e.pool_id for e in s.path] == ["es", "be", "bd"])
    cand = search.run(run.checkpoints[0], igr.Counters(), forced=fix)
    try:
        plan = incremental_graph.merged_plan(case, cand.flows.values())
    except ValueError:
        plan = None
    if plan is not None:
        ev = reference_evaluate(bundle, case, plan, gross_only())
        assert ev.status is not EvalStatus.OK or ev.gross_output != igr.accounted_gross(
            case, cand.flows
        )
    reuse = incremental_graph._ExactReuse(search.paths)
    edges: set[tuple[str, str]] = set()
    for d in run.decisions:
        new = [
            t for t in dict.fromkeys((e.token_in, e.token_out) for e in d.path) if t not in edges
        ]
        reuse.commit(d.path, new)
        edges.update(new)
    j = search.paths.index(fix.path)
    assert reuse.cyclic(j, fix.path)
    assert not incremental_graph._ExactReuse(search.paths).cyclic(j, fix.path)
    assert "_ExactReuse" not in Path(igr.__file__).read_text().split('"""', 2)[2]


# ------------------------------------------------------------------ the greedy trap


def test_structural_trap_is_repaired_to_the_independent_optimum() -> None:
    got, bundle, case, spec = _fixture("structural_trap")
    o, want = spec["oracle"], spec["specification"]
    best, winners, complete = research.oracle_best(spec["pools"], 30, case, 3, 3)
    assert (best, complete) == (o["best_gross"], o["complete_sequences"])
    off, *_ = _fixture("structural_trap", options=OFF)
    assert off.score == o["incumbent_gross"] == spec["repository"]["incremental_graph_score"]
    r = got.search_stats["repair"]
    assert _outcomes(got) == want["outcomes"]
    assert (
        list(dict.fromkeys(a["checkpoint"] for a in r["attempts"])) == want["checkpoints_visited"]
    )
    assert [int(a["score"]) for a in r["accepted_log"]] == want["accepted_scores"]
    assert got.score == best and got.search_stats["chosen_source"] == NAME
    assert (r["stop"], r["consistency_failures"], r["repair_evaluations"]) == ("complete", 0, 4)
    assert got.plan is not None and got.evaluation is not None
    ev = reference_evaluate(bundle, case, got.plan, gross_only())
    research.full_fill(case, ev)
    assert ev.gross_output == best == got.evaluation.gross_output
    last = r["accepted_log"][-1]
    search, _, cand = _rebuild(
        bundle, case, spec["settings"], last["checkpoint"], last["alternative"]
    )
    assert [list(p) for p in _sequence(search, cand)] == [list(p) for p in winners[0]]
    assert incremental_graph.merged_plan(case, cand.flows.values()) == got.plan


def test_accepted_candidates_keep_the_prefix_and_change_the_first_suffix_choice() -> None:
    bundle, case, spec = research.fixture_case("structural_trap")
    checked = kept_prefix = 0
    for chunks in (3, 5, 10):
        params = {**spec["settings"], "chunks": chunks}
        got = _solve(bundle, case, params)
        for a in got.search_stats["repair"]["accepted_log"]:
            i = a["checkpoint"]
            search, run, cand = _rebuild(bundle, case, params, i, a["alternative"])
            assert cand.decisions[:i] == run.decisions[:i]
            assert cand.decisions[i].path != run.decisions[i].path
            assert igr.flow_key(cand.flows) != igr.flow_key(run.flows)
            plan = incremental_graph.merged_plan(case, cand.flows.values())
            ev = reference_evaluate(bundle, case, plan, gross_only())
            research.full_fill(case, ev)
            value = research.oracle_sequence(
                spec["pools"], 30, case, 3, chunks, _sequence(search, cand)
            )
            assert value == ev.gross_output == int(a["score"])
            checked += 1
            kept_prefix += i > 0
    assert checked >= 5 and kept_prefix >= 1


def test_repair_is_not_optimal_even_in_its_own_domain() -> None:
    bundle, case, spec = research.fixture_case("structural_trap")
    for chunks, want in spec["chunk_variants"].items():
        params = {**spec["settings"], "chunks": int(chunks)}
        got = _solve(bundle, case, params)
        best, _, _ = research.oracle_best(spec["pools"], 30, case, 3, int(chunks))
        assert best == want["oracle_best_gross"]
        assert got.search_stats["incremental_score"] == str(want["incremental_graph_score"])
        assert got.score == want["repair_score"] <= best
    assert (
        spec["chunk_variants"]["5"]["repair_score"]
        < spec["chunk_variants"]["5"]["oracle_best_gross"]
    )


def test_whi1549_greedy_trap_is_repaired_to_its_label_plan() -> None:
    x = research.FIX["cross_references"]["history_labels_greedy_trap"]
    g = research.HISTORY_FIX["fixtures"]["greedy_trap"]
    bundle = research.cp_bundle(g["pools"])
    case = Case(
        "greedy_trap", g["case"]["token_in"], g["case"]["token_out"], g["case"]["amount_in"]
    )
    off = _solve(bundle, case, x["settings"], OFF)
    on = _solve(bundle, case, x["settings"])
    assert off.score == x["repository"]["incremental_graph_score"]
    assert off.search_stats["chosen_source"] == x["repository"]["incremental_graph_source"]
    assert on.score == g["expected"]["label_gross"] and on.plan is not None
    research.full_fill(case, reference_evaluate(bundle, case, on.plan, gross_only()))


# ------------------------------------------------------------------ rejection, ties, stops


def test_no_improvement_keeps_the_incumbent_and_its_publications() -> None:
    off_sink: list[RoutePlan] = []
    on_sink: list[RoutePlan] = []
    off, *_ = _fixture("twin_pools", options=OFF, sink=off_sink)
    on, _, _, spec = _fixture("twin_pools", sink=on_sink)
    assert _outcomes(on) == spec["specification"]["outcomes"]
    assert (on.plan, on.score, on_sink) == (off.plan, off.score, off_sink)
    assert on.search_stats["chosen_source"] == spec["specification"]["chosen_source"]
    r = on.search_stats["repair"]
    assert (r["duplicates"], r["rejected_worse"], r["accepted"], r["repair_evaluations"]) == (
        1,
        1,
        0,
        1,
    )
    fb = on.search_stats["r021"]["fallback"]
    assert fb == {"used": True, "source": "direct_split", "reason": "retained_simpler_candidate"}


def test_an_equal_score_candidate_is_a_tie_and_the_earlier_incumbent_stays() -> None:
    x = research.FIX["cross_references"]["history_labels_tie_state"]
    g = research.HISTORY_FIX["fixtures"]["tie_state"]
    bundle = research.cp_bundle(g["pools"])
    case = Case("tie_state", g["case"]["token_in"], g["case"]["token_out"], g["case"]["amount_in"])
    off = _solve(bundle, case, x["settings"], OFF)
    on = _solve(bundle, case, x["settings"])
    assert off.score == g["expected"]["history_gross"]
    assert _outcomes(on) == x["specification"]["outcomes"] == ["tie"]
    assert on.plan == off.plan and on.search_stats["repair"]["ties"] == 1
    assert on.search_stats["chosen_source"] == x["repository"]["incremental_graph_source"]


def test_single_route_attempt_cap_unreachable_pair_and_determinism() -> None:
    bundle = research.cp_bundle({"ab": ["A", "B", 10**9, 10**9]})
    one = _solve(
        bundle,
        Case("one", "A", "B", 10**6),
        {"max_hops": 2, "max_splits": 4, "percent_step": 5, "chunks": 5},
    )
    r = one.search_stats["repair"]
    assert (r["stop"], r["checkpoint_restores"], r["repair_attempts"]) == ("complete", 1, 0)
    capped, *_ = _fixture("structural_trap", options={**PRESET, "max_repair_attempts": 1})
    r = capped.search_stats["repair"]
    assert (r["stop"], r["repair_attempts"]) == ("attempt_cap", 1)
    split = research.cp_bundle({"ab": ["A", "B", 10**6, 10**6], "cd": ["C", "D", 10**6, 10**6]})
    none = _solve(
        split,
        Case("u", "A", "D", 1000),
        {"max_hops": 2, "max_splits": 4, "percent_step": 5, "chunks": 3},
    )
    assert none.status is SolveStatus.NO_ROUTE
    assert none.search_stats["repair"]["stop"] == "no_trace"
    assert none.search_stats["r021"]["fallback"] == {"used": False, "source": None, "reason": None}
    a, *_ = _fixture("structural_trap")
    b, *_ = _fixture("structural_trap")
    assert a == b


def test_case_order_and_a_shared_prepared_object_leak_no_state() -> None:
    """One prepared object (as a worker holds it) across cases in either order: every result
    equals a fresh solve's; the prepared object is not mutated."""
    real = research.mixed()
    ctx = _context(real, MIXED_PARAMS)
    fresh = {c.case_id: _solve(real, c, MIXED_PARAMS) for c in real.cases}
    graph_before = ctx.prepared.graph
    for cases in (list(real.cases), list(reversed(real.cases)), [*real.cases, *real.cases]):
        for c in cases:
            assert FACTORY.solve(c, ctx, Budget()) == fresh[c.case_id], c.case_id
    assert ctx.prepared.graph is graph_before and dict(ctx.prepared.options) == PRESET


# ------------------------------------------------------------------ one ledger, budgets, kills


def test_one_quote_ledger_covers_every_stage() -> None:
    bundle, case, spec = research.fixture_case("structural_trap")
    with metered_quotes(None) as off_meter:
        off = _solve(bundle, case, spec["settings"], OFF)
    with metered_quotes(None) as on_meter:
        on = _solve(bundle, case, spec["settings"])
    assert off.search_stats["quotes_executed"] == off_meter.counted
    assert on.search_stats["quotes_executed"] == on_meter.counted > off_meter.counted
    work = on.search_stats["r021"]["work"]
    assert work["quotes_executed"] == on_meter.counted
    r = on.search_stats["repair"]
    assert work["paths_scored"] == on.search_stats["paths_scored"] + r["paths_scored"]
    assert on.candidates_considered == off.candidates_considered + r["paths_scored"]


def _budget_sweep(
    bundle: SnapshotBundle, case: Case, params: Mapping[str, int], options: Mapping[str, Any]
) -> dict[int, list[int]]:
    """Every declared `max_quotes` from the repair-off solve's executed quotes to the full
    repair-on solve's: the meter never exceeds the limit (no reset), and the returned plan is
    the best so far -- the last publication, scored as its independent replay, equal to the
    best of the control and every accepted candidate, with consistent provenance (an accepted
    candidate names the repair; otherwise the control's source and fallback label stand).
    Returns {number of acceptances before the cut: limits}."""
    off = _solve(bundle, case, params, OFF)
    full_sink: list[RoutePlan] = []
    full = _solve(bundle, case, params, options, sink=full_sink)
    n_off, n_on = off.search_stats["quotes_executed"], full.search_stats["quotes_executed"]
    assert off.score is not None and n_on >= n_off
    levels: dict[int, list[int]] = {}
    for limit in range(n_off, n_on + 1):
        sink: list[RoutePlan] = []
        with metered_quotes(limit) as meter:
            res = _solve(bundle, case, params, options, Budget(max_quotes=limit), sink)
        s, r = res.search_stats, res.search_stats["repair"]
        assert not meter.exceeded and meter.counted == s["quotes_executed"] <= limit
        assert res.status is SolveStatus.OK and res.plan is not None and res.score is not None
        assert sink == full_sink[: len(sink)] and res.plan == sink[-1], limit
        assert res.score == _true_gross(bundle, case, res.plan)
        accepted = [int(a["score"]) for a in r["accepted_log"]]
        assert r["accepted"] == len(accepted) and accepted == sorted(set(accepted))
        assert res.score == max([off.score, *accepted]), limit
        if accepted:
            assert s["chosen_source"] == NAME, limit
            assert s["r021"]["fallback"] == {"used": False, "source": NAME, "reason": None}
        else:
            assert s["chosen_source"] == off.search_stats["chosen_source"]
            assert s["r021"]["fallback"] == off.search_stats["r021"]["fallback"]
            assert res.plan == off.plan
        if limit < n_on:
            assert r["stop"] == "quote_budget" and s["truncated_by"] == "max_quotes", limit
            assert NAME in s["truncated_stages"]
        else:
            assert (res.plan, res.score, r["stop"]) == (
                full.plan,
                full.score,
                full.search_stats["repair"]["stop"],
            )
        levels.setdefault(len(accepted), []).append(limit)
    return levels


def test_the_budget_is_never_reset_and_a_quote_cut_keeps_the_best_so_far() -> None:
    """Cuts before any acceptance, after one and after several (suffix-repair.md §5.7
    `quote_budget`: "best so far"). On structural_trap the limits 282..290 cut the repair
    after its first acceptance: the returned plan is that accepted, published 111,176,933."""
    bundle, case, spec = research.fixture_case("structural_trap")
    levels = _budget_sweep(bundle, case, spec["settings"], PRESET)
    assert levels == {0: list(range(263, 282)), 1: list(range(282, 291)), 2: [291]}
    for limit in range(282, 291):
        res = _solve(bundle, case, spec["settings"], budget=Budget(max_quotes=limit))
        assert res.score == 111_176_933 and res.search_stats["chosen_source"] == NAME
    for chunks in (3, 5):  # the stress caps try further alternatives after two acceptances
        levels = _budget_sweep(bundle, case, {**spec["settings"], "chunks": chunks}, STRESS)
        assert {0, 1, 2} <= set(levels) and len(levels[2]) > 1
    rng = random.Random(20261559)
    swept = 0
    while swept < 6:
        pools, rcase, rparams, _ = research.random_instance(rng)
        rbundle = research.cp_bundle(pools)
        if not _solve(rbundle, rcase, rparams).search_stats["repair"]["accepted"]:
            continue
        levels = _budget_sweep(rbundle, rcase, rparams, PRESET)
        assert 0 in levels and max(levels) >= 1
        swept += 1
    off = _solve(bundle, case, spec["settings"], OFF)
    n_off = off.search_stats["quotes_executed"]
    with metered_quotes(n_off - 1) as meter:  # the incumbent itself is cut: no repair
        cut = _solve(bundle, case, spec["settings"], budget=Budget(max_quotes=n_off - 1))
    assert cut.search_stats["repair"]["stop"] == "quote_budget" and not meter.exceeded
    assert cut.search_stats["repair"]["repair_attempts"] == 0


def test_a_hard_quote_kill_leaves_only_complete_replayed_publications() -> None:
    """The worker's hard meter kills the solve at every executed quote in turn (all stages):
    the publications are always a prefix of the uninterrupted strictly improving sequence,
    the last one a complete full-fill plan; a kill at the first repair quote leaves exactly
    the repair-off control's publications."""
    bundle, case, spec = research.fixture_case("structural_trap")
    full_sink: list[RoutePlan] = []
    full = _solve(bundle, case, spec["settings"], sink=full_sink)
    off_sink: list[RoutePlan] = []
    off = _solve(bundle, case, spec["settings"], OFF, sink=off_sink)
    scores = [_true_gross(bundle, case, p) for p in full_sink]
    assert scores == sorted(set(scores)) and scores[-1] == full.score and len(scores) >= 3
    seen = set()
    for kill_at in range(full.search_stats["quotes_executed"]):
        sink: list[RoutePlan] = []
        with metered_quotes(kill_at), pytest.raises(QuoteLimitExceeded):
            _solve(bundle, case, spec["settings"], sink=sink)
        assert sink == full_sink[: len(sink)]
        if sink:
            research.full_fill(case, reference_evaluate(bundle, case, sink[-1], gross_only()))
        if kill_at == off.search_stats["quotes_executed"]:
            assert sink == off_sink
        seen.add(len(sink))
    assert seen == set(range(len(full_sink)))


def test_candidate_cap_applies_to_every_rebuilt_chunk() -> None:
    got, *_ = _fixture("structural_trap", budget=Budget(max_candidates=2))
    s = got.search_stats
    assert s["truncated_by"] == "max_candidates" and s["repair"]["paths_truncated"] > 0
    assert got.candidates_truncated >= s["paths_truncated"] + s["repair"]["paths_truncated"]


# ------------------------------------------------------------------ real state, random graphs


def test_repair_on_real_state_and_random_graphs_keeps_every_invariant() -> None:
    """Never below the control, no consistency failure, full fill, one ledger, statuses as
    the control's, the attempt order is exactly the registered neighborhood (zero-marginal
    alternatives excluded) and every accepted candidate is valued identically by the oracle."""
    runs: list[tuple[SnapshotBundle, Case, Mapping[str, int], Budget, Any]] = [
        (research.mixed(), c, MIXED_PARAMS, Budget(), None) for c in research.mixed().cases
    ]
    rng = random.Random(20261556)
    for _ in range(200):
        pools, rcase, rparams, rbudget = research.random_instance(rng)
        runs.append((research.cp_bundle(pools), rcase, rparams, rbudget, pools))
    accepted = zero_excluded = 0
    for bundle, case, params, budget, pools in runs:
        off = _solve(bundle, case, params, OFF, budget)
        with metered_quotes(None) as meter:
            on = _solve(bundle, case, params, PRESET, budget)
        r = on.search_stats["repair"]
        if r["stop"] in ("complete", "attempt_cap"):  # the whole registered neighborhood ran
            order, zero = _registered_neighborhood(bundle, case, params, budget)
            assert [(a["checkpoint"], a["alternative"]) for a in r["attempts"]] == order
            zero_excluded += zero
        assert r["consistency_failures"] == 0 and on.search_stats["consistency_failure"] is None
        assert on.search_stats["quotes_executed"] == meter.counted
        assert on.status is off.status
        if off.score is not None:
            assert on.score is not None and on.score >= off.score and on.plan is not None
            research.full_fill(case, reference_evaluate(bundle, case, on.plan, gross_only()))
        if pools is None:
            continue
        for a in r["accepted_log"]:
            search, _, cand = _rebuild(bundle, case, params, a["checkpoint"], a["alternative"])
            value = research.oracle_sequence(
                pools, 30, case, params["max_hops"], params["chunks"], _sequence(search, cand)
            )
            assert value == int(a["score"])
            accepted += 1
    assert accepted > 0 and zero_excluded > 0


def test_objective_is_the_references_and_nothing_is_unsupported() -> None:
    """`incremental_graph_repair` has `incremental_graph`'s objectives (R021-C/1 §2): under a
    per-call cost it runs (no `unsupported` row), accepts on the objective's complete-plan
    score and every returned score is the objective of the independent replay."""
    for fixed in (0, 10**6, 10**7):
        objective = synthetic_fixed_cost(fixed)
        for name in ("structural_trap", "twin_pools"):
            bundle, case, spec = research.fixture_case(name)
            off = _solve(bundle, case, spec["settings"], OFF, objective=objective)
            on = _solve(bundle, case, spec["settings"], objective=objective)
            assert on.status is SolveStatus.OK and on.plan is not None
            ev = reference_evaluate(bundle, case, on.plan, objective)
            assert on.score == objective.score(ev)
            assert off.score is not None and on.score >= off.score
            for a in on.search_stats["repair"]["accepted_log"]:
                assert int(a["score"]) > off.score


# ------------------------------------------------------------------ evaluation total, faults


def _independent_counts(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Independent counting wrappers on every loaded `routing.algorithms` module alias."""
    import routing.algorithms as package

    counts: dict[str, int] = {}
    for name, module in sorted(sys.modules.items()):
        if module is None or not name.startswith(package.__name__ + "."):
            continue
        inner = getattr(module, "evaluate", None)
        if inner is None:
            continue

        def wrapper(*a: Any, _n: str = name, _f: Any = inner, **k: Any) -> Any:
            counts[_n] = counts.get(_n, 0) + 1
            return _f(*a, **k)

        monkeypatch.setattr(module, "evaluate", wrapper)
    return counts


def test_internal_evaluations_is_the_total_of_every_in_solve_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts = _independent_counts(monkeypatch)
    got, bundle, case, spec = _fixture("structural_trap")
    assert counts == {
        "routing.algorithms.single_path": 6,
        "routing.algorithms.path_split": 1,
        "routing.algorithms.incremental_graph_repair": 5,
    }
    assert got.search_stats["evaluations"] == {
        "fallback": 7,
        "incumbent": 1,
        "repair": 4,
        "total": 12,
    }
    assert got.search_stats["repair"]["repair_evaluations"] == 4
    assert got.search_stats["r021"]["work"]["internal_evaluations"] == 12
    counts.clear()
    off, *_ = _fixture("structural_trap", options=OFF)
    assert off.search_stats["evaluations"]["total"] == sum(counts.values()) == 8
    runs: list[tuple[SnapshotBundle, Case, Mapping[str, int], Budget]] = []
    for name in FIXTURES[1:]:
        b, c, sp = research.fixture_case(name)
        runs.append((b, c, sp["settings"], Budget()))
    real = research.mixed()
    runs += [(real, c, MIXED_PARAMS, Budget()) for c in real.cases]
    rng = random.Random(20261557)
    for _ in range(40):
        pools, c, params, budget = research.random_instance(rng)
        runs.append((research.cp_bundle(pools), c, params, budget))
    for b, c, settings, limits in runs:
        for options in (PRESET, OFF):
            counts.clear()
            res = _solve(b, c, settings, options, limits)
            e = res.search_stats["evaluations"]
            assert (
                e["total"] == sum(counts.values()) == e["fallback"] + e["incumbent"] + e["repair"]
            )
            assert e["repair"] == res.search_stats["repair"]["repair_evaluations"]
            assert e["total"] == res.search_stats["r021"]["work"]["internal_evaluations"]
            if options is OFF:
                assert e["repair"] == 0


def _corrupting(mode: str, at: int, calls: list[int]) -> Callable[..., Evaluation]:
    real = reference_evaluate

    def corrupted(*a: Any, **k: Any) -> Evaluation:
        calls[0] += 1
        ev = real(*a, **k)
        if calls[0] != at:
            return ev
        if mode == "invalid":
            return dataclasses.replace(ev, status=EvalStatus.INVALID_PLAN)
        return dataclasses.replace(ev, gross_output=ev.gross_output + (1 if mode == "+1" else -1))

    return corrupted


@pytest.mark.parametrize("mode", ["+1", "-1", "invalid"])
@pytest.mark.parametrize("at", [1, 2, 3, 4, 5])
def test_a_corrupted_in_solve_replay_is_never_accepted_or_published(
    monkeypatch: pytest.MonkeyPatch, mode: str, at: int
) -> None:
    """Fault injection at the factory's own `at`-th replay of structural_trap (1 = the
    stage-2 incumbent, 2..5 = the four repair replays)."""
    clean_sink: list[RoutePlan] = []
    clean, bundle, case, spec = _fixture("structural_trap", sink=clean_sink)
    monkeypatch.setattr(igr, "evaluate", _corrupting(mode, at, [0]))
    sink: list[RoutePlan] = []
    res, *_ = _fixture("structural_trap", sink=sink)
    monkeypatch.undo()
    assert res.status is SolveStatus.OK and res.plan is not None
    assert res.score == _true_gross(bundle, case, res.plan)  # never a false claimed score
    assert sink == clean_sink[: len(sink)] and sink[-1] == res.plan
    s = res.search_stats
    assert s["repair"]["stop"] == "consistency_failure"
    cf = s["consistency_failure"]
    assert cf["stage"] == ("incumbent" if at == 1 else "repair")
    assert cf["replay_index"] == (1 if at == 1 else at - 1)
    if at == 1:
        assert res.plan == clean_sink[0] and res.score == spec["repository"]["path_split_score"]
        assert s["chosen_source"] not in (incremental_graph.NAME, NAME)
        assert s["incremental_status"] == (
            "invalid_plan" if mode == "invalid" else "consistency_failure"
        )
        assert s["incremental_score"] is None and s["repair"]["repair_attempts"] == 0
        assert s["r021"]["fallback"]["reason"] == f"incremental_status {s['incremental_status']}"
    else:
        replayed = [a for a in clean.search_stats["repair"]["attempts"] if "score" in a][: at - 2]
        best = max(
            [spec["oracle"]["incumbent_gross"]]
            + [int(a["score"]) for a in replayed if a["outcome"] == "accepted"]
        )
        assert res.score == best
    assert s["evaluations"]["total"] == s["evaluations"]["fallback"] + at


def test_a_merged_plan_refusal_of_a_candidate_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    clean, bundle, case, spec = _fixture("structural_trap")
    real = incremental_graph.merged_plan
    calls = [0]

    def refusing(c: Case, flows: Any) -> RoutePlan:
        calls[0] += 1
        if calls[0] == 3:  # the first candidate that would be replayed (1 = incumbent)
            raise ValueError("flows are not conserved at token X")
        return real(c, flows)

    monkeypatch.setattr(igr, "merged_plan", refusing)
    sink: list[RoutePlan] = []
    res = _solve(bundle, case, spec["settings"], sink=sink)
    cf = res.search_stats["consistency_failure"]
    assert (cf["stage"], cf["detail"]) == ("repair", "flows are not conserved at token X")
    assert res.search_stats["repair"]["stop"] == "consistency_failure"
    assert res.plan is not None and res.score == _true_gross(bundle, case, res.plan)
    assert res.score == spec["oracle"]["incumbent_gross"] and len(sink) == 2


def _no_simpler_candidate(
    case: Case, context_: SolveContext, budget: Budget, cache: QuoteCache
) -> SolveResult:
    return SolveResult(case_id=case.case_id, algorithm="path_split", status=SolveStatus.NO_ROUTE)


def test_without_a_validated_plan_a_consistency_failure_is_an_algorithm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(path_split, "solve", _no_simpler_candidate)
    for mode in ("+1", "invalid"):
        sink: list[RoutePlan] = []
        with monkeypatch.context() as m:
            m.setattr(igr, "evaluate", _corrupting(mode, 1, [0]))
            res, *_ = _fixture("structural_trap", sink=sink)
        assert res.status is SolveStatus.ALGORITHM_ERROR
        assert res.plan is None and res.score is None and sink == []
        assert res.error is not None and "consistency_failure in incumbent replay 1" in res.error
        assert res.search_stats["consistency_failure"]["accounted_gross"] == "90545314"
        assert res.search_stats["r021"]["fallback"]["used"] is False
    sink = []
    with monkeypatch.context() as m:
        m.setattr(igr, "evaluate", _corrupting("+1", 2, [0]))
        res, bundle, case, spec = _fixture("structural_trap", sink=sink)
    assert res.status is SolveStatus.OK and res.plan is not None and len(sink) == 1
    assert res.score == _true_gross(bundle, case, res.plan) == spec["oracle"]["incumbent_gross"]
    assert res.search_stats["chosen_source"] == incremental_graph.NAME


# ------------------------------------------------------------------ R021 diagnostics


def _check_context(
    bundle: SnapshotBundle, case: Case, res: SolveResult, counted: int, objective: str
) -> CheckContext:
    """What the runner supplies independently of the solver."""
    return CheckContext(
        run={
            "git_revision": "g" * 40,
            "bundle_hash": bundle.bundle_hash,
            "algorithm": NAME,
            "effective_settings_sha256": settings_sha256(PRESET),
        },
        request={
            "case_id": case.case_id,
            "token_in": case.token_in,
            "token_out": case.token_out,
            "amount_in": str(case.amount_in),
        },
        status=res.status.value,
        score=None if res.score is None else str(res.score),
        objective=objective,
        quotes_counted=counted,
        pools={pid: "constant_product" for pid in bundle.pools},
    )


def test_diagnostics_record_validates_under_the_runtime_and_contract_validators() -> None:
    contract = research._shared_validator()
    domains = set()
    for name in ("structural_trap", "twin_pools", "carry_and_zero_flow"):
        bundle, case, spec = research.fixture_case(name)
        for options in (PRESET, OFF):
            with metered_quotes(None) as meter:
                res = _solve(bundle, case, spec["settings"], options)
            rec = res.search_stats["r021"]
            json.dumps(rec, sort_keys=True, allow_nan=False)
            ctx = _check_context(bundle, case, res, meter.counted, "gross_only")
            assert check_diagnostics(rec, ctx) == set(), (name, options)
            view = diagnostics_view(rec, ctx)
            assert (view["state"], view["reason"]) == ("unavailable", "not_produced")
            assert (
                contract.check_diagnostics(
                    rec,
                    {
                        "hard_killed": False,
                        "quotes_counted": meter.counted,
                        "run": {},
                        "request": {},
                        "objective": "gross_only",
                        "status": res.status.value,
                        "final_score": None if res.score is None else str(res.score),
                    },
                )
                == set()
            )
            assert rec["certificate"] is None and rec["max_candidates_unit"] == (
                "paths_scored_per_chunk"
            )
            assert rec["domain"]["universe"]["cohort"] == "fixture"
            assert rec["repair"]["enabled"] is options["repair"]
            assert "attempts" not in rec["repair"] and "paths_scored" not in rec["repair"]
            wrong = {**rec, "work": {**rec["work"], "quotes_executed": meter.counted - 1}}
            assert check_diagnostics(wrong, ctx) == {"W_LEDGER"}
            domains.add((name, rec["candidate_domain_hash"]))
    assert len(domains) == 3  # repair on and off share one domain hash per fixture
    real = research.mixed()
    res = _solve(real, real.cases[0], MIXED_PARAMS)
    rec = res.search_stats["r021"]
    assert rec["domain"]["universe"]["cohort"] == "full_source"
    assert rec["domain"]["universe"]["pools"] == list(real.pools)


# ------------------------------------------------------------------ CLI: run + quote --details


def _saved_run(out: str) -> Path:
    marker = "(run "
    return Path(out[out.index(marker) + len(marker) :].split(")", 1)[0])


def test_cli_run_and_quote_details_with_the_on_off_profiles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both CLI paths through the spawned worker and the real factory: a distinct identity,
    its options identity and repair provenance, the off control equal to the reference in the
    same run, one solve per quote, literal `--strategies profile` replay."""
    import main
    from benchmark.results import load_case_records, load_manifest
    from benchmark.runner import compare_runs

    mixed = REPO / "tests" / "fixtures" / "routing" / "mantle_mixed"
    corpus = REPO / "tests" / "fixtures" / "corpus" / "bundle"
    for arm, options in (("repair_on", PRESET), ("repair_off", OFF)):
        profile = PROFILES / f"{arm}.yaml"
        results = tmp_path / arm / "runs"
        argv = [
            "run",
            "--bundle",
            str(mixed),
            "--profile",
            str(profile),
            "--results-dir",
            str(results),
            "--strategies",
            "profile",
        ]
        assert main.main(argv) == 0
        out = capsys.readouterr().out
        assert f"Experimental and other strategies (1): {NAME}" in out
        (run_dir,) = results.iterdir()
        manifest = load_manifest(run_dir)
        assert list(manifest.algorithms) == [incremental_graph.NAME, NAME]
        entry = manifest.resolved_profile["algorithm_options"][NAME]
        assert entry["options"] == options
        assert entry["source"]["kind"] == ("preset" if options is PRESET else "override")
        records = load_case_records(run_dir)
        ref = {r["case_id"]: r for r in records if r["algorithm"] == incremental_graph.NAME}
        own = [r for r in records if r["algorithm"] == NAME]
        assert len(own) == len(ref) == len(load_manifest(run_dir).measurement["case_order"])
        for r in own:
            base = ref[r["case_id"]]
            assert r["status"] == "ok" and r["score"] is not None
            assert int(r["score"]) >= int(base["score"])
            if options is OFF:
                assert (r["score"], r["evaluation"]) == (base["score"], base["evaluation"])
            assert r["search"]["repair"]["enabled"] is options["repair"]
            assert r["search"]["repair"]["stop"] == ("disabled" if options is OFF else "complete")
            view = r["diagnostics"]
            assert (view["state"], view["reason"], view.get("codes", [])) == (
                "unavailable",
                "not_produced",
                [],
            )
            assert view["work"]["internal_evaluations"] == r["search"]["evaluations"]["total"]
            assert view["work"]["quotes_executed"] == view["checked_against"]["quotes_counted"]
            assert (
                view["checked_against"]["run"]["effective_settings_sha256"]
                == entry["settings_sha256"]
            )
        replay = shlex.split(manifest.replay_command)
        assert replay[-2:] == ["--strategies", "profile"]
        assert main.main(replay[replay.index("main.py") + 1 :]) == 0
        replayed = next(p for p in results.iterdir() if p != run_dir)
        assert load_manifest(replayed).resolved_profile == manifest.resolved_profile
        assert compare_runs(run_dir, replayed) == []
        capsys.readouterr()
        # quote --details: one solve per selected algorithm, the repair provenance shown
        quotes = tmp_path / arm / "quotes"
        assert (
            main.main(
                [
                    "quote",
                    "--bundle",
                    str(corpus),
                    "--profile",
                    str(profile),
                    "--token-in",
                    "USDC",
                    "--token-out",
                    "USDT0",
                    "--amount",
                    "1500.25",
                    "--details",
                    "--quotes-dir",
                    str(quotes),
                    "--strategies",
                    "profile",
                ]
            )
            == 0
        )
        out = capsys.readouterr().out
        assert f"[{NAME}] ok" in out and "bound: unavailable (not_produced)" in out
        assert "max_candidates unit: paths_scored_per_chunk" in out
        assert "internal_evaluations" in out and "repair_evaluations" in out
        assert f"enabled {options['repair']}" in out
        quote_run = _saved_run(out)
        quote_records = load_case_records(quote_run)
        assert [r["algorithm"] for r in quote_records] == [incremental_graph.NAME, NAME]
        for r in quote_records:
            assert r["measurement"]["attempts_completed"] == 1
            assert len(r["measurement"]["solve_seconds"]) == 1
        saved = yaml.safe_load((quote_run.parent.parent / "profile.yaml").read_text())
        assert saved["algorithm_options"] == {NAME: options}

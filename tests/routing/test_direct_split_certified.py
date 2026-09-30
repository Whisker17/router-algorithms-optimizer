"""WHI-1552: the registered `direct_split_certified` factory (integer-allocation.md §2-§8, §11).

Every behavioural check runs the REAL registered factory (`get_algorithm(NAME)`: its public
`prepare` and `solve`). `solve` is exactly `direct_split_certified.certify(...).result`; the
audits call `certify` with a `trace` list to see the open frontier and every created node, and
`_run` asserts that the traced run returns the identical `SolveResult` as `FACTORY.solve` on
the same inputs, so the audited search is the registered one.

Expected values never come from the implementation. From the WHI-1551 research module
(`test_integer_allocation_contract`) only the fixtures, instance generators, the independent
exhaustive oracle (`oracle_values`: its own `itertools` enumeration, amount rule and hand CPMM
formula with the dust, zero-reserve, source/fee and `uint112` rules) and the node-region
definition `in_region` are reused; its executable specification `certify` is compared only for
the published records, which it generated. Whole plans are replayed by the plain, memo-free
evaluator, and records are checked by the runtime validator (`benchmark.diagnostics`) against
an independently built run context, and by the unchanged contract validator.

Mutations and faults are `monkeypatch`es of the module's own names (`state_bound`,
`interval_bound`, `evaluate`); the production module patches nothing.
"""

from __future__ import annotations

import dataclasses
import json
import math
import shlex
from collections.abc import Callable, Iterator, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
import test_integer_allocation_contract as spec  # sibling: fixtures, oracle, generators
import yaml

import benchmark.profile as profile_module
from benchmark.diagnostics import (
    CheckContext,
    check_diagnostics,
    diagnostics_view,
    pool_protocol,
)
from benchmark.objective import gross_only, synthetic_fixed_cost
from benchmark.profile import ProfileError, load_profile, parse_profile, preset_options
from benchmark.strategies import R021_ADDITIONS, derive
from pools.quote import QuoteLimitExceeded, metered_quotes
from routing.algorithms import direct_split, incremental_graph_repair, metis_history
from routing.algorithms import direct_split_certified as dsc
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
from snapshot.bundle import load_bundle, write_bundle
from snapshot.models import Case, ConstantProductPoolState, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
NAME = "direct_split_certified"
FACTORY = get_algorithm(NAME)
PRESET = {"domain": "repository_grid", "max_bound_nodes": 100_000, "max_open_nodes": 100_000}
STRESS = {**PRESET, "domain": "raw_integer", "max_bound_nodes": 1_000_000,
          "max_open_nodes": 250_000, "raw_max_amount_in": 100_000}  # fmt: skip
GRID, RAW = spec.GRID, spec.RAW  # uncapped (10**7) options
NON_STRING_KEY: dict[Any, Any] = {1: 2, **PRESET}
PROFILES = REPO / "config" / NAME
FIXTURES = REPO / "tests" / "fixtures"
MIXED = FIXTURES / "routing" / "mantle_mixed"
CPMM = FIXTURES / "routing" / "cpmm_graph"
SYNTHETIC = FIXTURES / "synthetic"
MOE = FIXTURES / "moe_classic" / "bundle"
CORPUS = FIXTURES / "corpus" / "bundle"
REVISION = "c" * 40  # the run's source revision (runner identity)
Instance = spec.Instance


# ------------------------------------------------------------------ helpers


def _prepare(
    bundle: SnapshotBundle, splits: int, step: int, options: Mapping[str, Any]
) -> dsc.PreparedCertified:
    assert FACTORY.prepare is not None
    prepared = FACTORY.prepare(
        bundle,
        AlgorithmConfig(NAME, {"max_splits": splits, "percent_step": step}, dict(options)),
    )
    assert isinstance(prepared, dsc.PreparedCertified)
    return prepared


def _run(
    bundle: SnapshotBundle,
    case: Case,
    splits: int,
    step: int,
    options: Mapping[str, Any],
    budget: Budget | None = None,
    objective: Any = None,
    sink: list[RoutePlan] | None = None,
    revision: str = REVISION,
) -> dsc.Certified:
    """The registered `solve` and the traced `certify` on the same inputs: identical results
    (the audit sees the registered search). `sink` receives the solve's publications."""
    budget = budget or Budget()
    objective = objective or gross_only()
    prepared = _prepare(bundle, splits, step, options)
    context = SolveContext(
        bundle,
        objective,
        prepared,
        candidate_sink=None if sink is None else sink.append,
        run_identity={"git_revision": revision},
    )
    solved = FACTORY.solve(case, context, budget)
    traced = dsc.certify(
        bundle, case, objective, prepared, budget, git_revision=revision,
        cohort=dsc.cohort_of(bundle), trace=[],
    )  # fmt: skip
    assert traced.result == solved
    return traced


def _inst(inst: Instance, options: Mapping[str, Any] | None = None, **kw: Any) -> dsc.Certified:
    return _run(
        spec.bundle_of(inst.states), inst.case(), inst.splits, inst.step,
        options or (RAW if inst.raw else GRID), **kw,
    )  # fmt: skip


def _record(run: dsc.Certified) -> dict[str, Any]:
    rec: dict[str, Any] = run.result.search_stats["r021"]
    return rec


def _cert(run: dsc.Certified) -> dict[str, Any] | None:
    cert: dict[str, Any] | None = _record(run)["certificate"]
    return cert


def _context(
    bundle: SnapshotBundle,
    case: Case,
    res: SolveResult,
    counted: int | None,
    options: Mapping[str, Any],
    *,
    objective: str = "gross_only",
    revision: str = REVISION,
    hard_killed: bool = False,
) -> CheckContext:
    """What the runner supplies independently of the solver (score: an independent replay)."""
    score = None
    if res.plan is not None:
        ev = reference_evaluate(bundle, case, res.plan, gross_only())
        score = str(ev.gross_output) if ev.status is EvalStatus.OK else None
    return CheckContext(
        run={
            "git_revision": revision,
            "bundle_hash": bundle.bundle_hash,
            "algorithm": NAME,
            "effective_settings_sha256": settings_sha256(validated_options(FACTORY, options)),
        },
        request={
            "case_id": case.case_id,
            "token_in": case.token_in,
            "token_out": case.token_out,
            "amount_in": str(case.amount_in),
        },
        status=res.status.value,
        score=score,
        objective=objective,
        quotes_counted=counted,
        hard_killed=hard_killed,
        pools={pid: pool_protocol(p) for pid, p in bundle.pools.items()},
    )


def _in_region(node: dsc.Node, legs: tuple[tuple[int, int], ...], units: int) -> bool:
    """The research module's region membership, from the node's definition alone (§5.1)."""
    inside: bool = spec.in_region(node, legs, units)  # type: ignore[arg-type]
    return inside


def _audit(inst: Instance, run: dsc.Certified) -> list[str]:
    """The §8 audit against the oracle: the returned plan replays to the lower bound, the
    lower bound is a feasible value, every certified upper bound dominates the optimum, a zero
    gap is the optimum, and every feasible allocation above the incumbent lies in an open node
    whose bound covers it (frontier coverage)."""
    values = inst.values
    opt = spec.optimum(values)
    res, cert = run.result, _cert(run)
    errors = []
    bundle, case = spec.bundle_of(inst.states), inst.case()
    if res.plan is not None:
        ev = reference_evaluate(bundle, case, res.plan, gross_only())
        if ev.status is not EvalStatus.OK or ev.gross_output != res.score or ev.residuals:
            errors.append("returned plan does not replay to its score")
    if opt is None:
        if res.status is SolveStatus.OK:
            errors.append("plan on an infeasible domain")
        return errors
    lows: list[int] = []
    if cert is None:
        if res.status is not SolveStatus.TIMEOUT:
            errors.append(f"no certificate, status {res.status}")
    else:
        lows = [int(cert["lower_raw"])]
        if lows[0] != res.score or lows[0] not in values.values():
            errors.append("lower bound is not the returned feasible value")
        if cert["bound_kind"] == "certified":
            if int(cert["upper_raw"]) < opt:
                errors.append(f"upper {cert['upper_raw']} < optimum {opt}")
            if cert["optimality_proven"] and lows[0] != opt:
                errors.append(f"false zero gap: {lows[0]} < {opt}")
        elif cert["upper_raw"] is not None or cert["gap_raw"] is not None:
            errors.append("an unknown bound carries a value")
    for legs, value in values.items():
        if value is None or (lows and value <= lows[0]):
            continue
        covering = [n for n in run.frontier if _in_region(n, legs, inst.units)]
        if not covering:
            errors.append(f"uncovered allocation {legs} = {value}")
        elif any(n.ub is not None and n.ub < value for n in covering):
            errors.append(f"open node under-bounds {legs} = {value}")
    return errors


def _node_errors(inst: Instance, run: dsc.Certified) -> list[str]:
    """Every created node's bound dominates the best feasible allocation of its region."""
    values = [(legs, v) for legs, v in inst.values.items() if v is not None]
    errors = []
    assert run.trace is not None
    for node in run.trace:
        best = max((v for legs, v in values if _in_region(node, legs, inst.units)), default=None)
        if best is not None and node.ub is not None and node.ub < best:
            errors.append(f"{node.kind} j={node.j} legs={node.legs} ub {node.ub} < {best}")
    return errors


def _valid(bundle: SnapshotBundle, case: Case, run: dsc.Certified, counted: int,
           options: Mapping[str, Any], objective: str = "gross_only") -> None:  # fmt: skip
    ctx = _context(bundle, case, run.result, counted, options, objective=objective)
    assert check_diagnostics(_record(run), ctx) == set()


# ------------------------------------------------------------------ registration and options


def test_registered_once_as_a_custom_identity_between_history_and_repair() -> None:
    assert ALGORITHMS[NAME] is dsc.FACTORY is FACTORY
    names = list(ALGORITHMS)
    assert names.count(NAME) == 1
    assert names.index(metis_history.NAME) + 1 == names.index(NAME)
    assert names.index(NAME) + 1 == names.index(incremental_graph_repair.NAME)
    assert NAME not in BASE_STRATEGIES and NAME not in OPTIMIZED_STRATEGIES
    assert profile_module.strategy_group(NAME) == "custom"
    assert R021_ADDITIONS == (
        metis_history.NAME, NAME, incremental_graph_repair.NAME, "uni_sor_cycle_safe"
    )  # fmt: skip
    assert FACTORY.options_validator is dsc.validate_options  # module-level (picklable)
    assert FACTORY.capabilities == direct_split.CAPABILITIES
    assert FACTORY.search_params == ("max_splits", "percent_step") and FACTORY.graph_params == ()
    assert FACTORY.provenance is not None and "raw_integer" in str(FACTORY.provenance)
    # the reference is untouched: no options, no preset, same factory object
    assert ALGORITHMS[direct_split.NAME] is direct_split.FACTORY
    assert direct_split.FACTORY.options_validator is None
    assert direct_split.FACTORY.options_preset is None
    for name in ("daily_gross.yaml", "daily.yaml", "full_gross.yaml", "full.yaml"):
        document, profile = derive(
            yaml.safe_load((REPO / "config" / name).read_text()),
            "all",
            source_path=f"config/{name}",
            source_sha256="x",
        )
        assert list(profile.algorithms)[-5:] == [
            "metis_inspired", metis_history.NAME, NAME, incremental_graph_repair.NAME,
            "uni_sor_cycle_safe",
        ]  # fmt: skip
        assert len(profile.algorithms) == 13
        assert document["algorithm_options"][NAME] == PRESET  # repository_grid only in `all`
        assert profile.algorithm_options[NAME]["source"]["kind"] == "preset"
        assert dict(profile.algorithm_config(FACTORY).options) == PRESET
        assert profile.algorithm_config(FACTORY).params == {"max_splits": 4, "percent_step": 5}


def test_preset_file_is_the_pinned_v1_and_tampering_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = dict(FACTORY.options_preset or {})
    assert pin == {
        "path": "config/direct_split_certified/preset_v1.yaml",
        "sha256": dsc.PRESET["sha256"],
        "key": "R021-P06-direct_split_certified",
        "version": 1,
    }
    assert preset_options(FACTORY) == PRESET == spec.FIX["preset"]["options"]
    assert settings_sha256(PRESET) == spec.FIX["preset"]["settings_sha256"]  # 03cfe301…8406
    assert validated_options(FACTORY, STRESS) == spec.FIX["stress_raw"]["options"]
    assert settings_sha256(STRESS) == spec.FIX["stress_raw"]["settings_sha256"]
    (tmp_path / PROFILES.relative_to(REPO)).mkdir(parents=True)
    changed = (REPO / pin["path"]).read_text().replace("nodes: 100000", "nodes: 100001", 1)
    (tmp_path / pin["path"]).write_text(changed)
    monkeypatch.setattr(profile_module, "REPO_ROOT", tmp_path)
    with pytest.raises(ProfileError, match="differs from the pin"):
        preset_options(FACTORY)


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"domain": "grid", "max_bound_nodes": 5, "max_open_nodes": 5},
        {"domain": True, "max_bound_nodes": 5, "max_open_nodes": 5},
        {"domain": "repository_grid", "max_bound_nodes": 5},  # required, no default
        {"domain": "repository_grid", "max_open_nodes": 5},
        {"max_bound_nodes": 5, "max_open_nodes": 5},
        {**PRESET, "max_bound_nodes": True},  # bool for int
        {**PRESET, "max_bound_nodes": 0},
        {**PRESET, "max_bound_nodes": 10_000_001},
        {**PRESET, "max_open_nodes": 1.5},
        {**PRESET, "max_open_nodes": 100.0},
        {**PRESET, "max_open_nodes": float("inf")},
        {**PRESET, "max_open_nodes": float("nan")},
        {**PRESET, "max_open_nodes": 10**400},
        {**PRESET, "max_open_nodes": None},  # "uncapped" is not expressible
        {**PRESET, "raw_max_amount_in": 10},  # only with raw_integer
        {**STRESS, "raw_max_amount_in": 1_000_001},
        {**STRESS, "raw_max_amount_in": 0},
        {k: v for k, v in STRESS.items() if k != "raw_max_amount_in"},  # required with raw
        {**PRESET, "tolerance": 0},  # unknown
        NON_STRING_KEY,
        *({**PRESET, key: 1} for key in ("max_splits", "percent_step", "shortlist", "max_hops",
                                           "max_candidates", "max_quotes", "objective", "seed")),
    ],
)  # fmt: skip
def test_invalid_options_are_refused_by_every_entry_point(options: dict[Any, Any]) -> None:
    """The shared `validated_options`, the factory's public `prepare` and profile loading
    refuse the same values; nothing is defaulted, clamped or coerced."""
    bundle = load_bundle(SYNTHETIC)
    with pytest.raises(OptionsError):
        validated_options(FACTORY, options)
    assert FACTORY.prepare is not None
    with pytest.raises(OptionsError):
        FACTORY.prepare(
            bundle, AlgorithmConfig(NAME, {"max_splits": 4, "percent_step": 5}, options)
        )
    doc = yaml.safe_load((PROFILES / "grid.yaml").read_text())
    doc["algorithm_options"] = {NAME: options}
    with pytest.raises(ProfileError):
        parse_profile(doc, "<test>")


def test_valid_options_profiles_and_the_reused_prepare() -> None:
    for options in (
        PRESET,
        STRESS,
        {"domain": "repository_grid", "max_bound_nodes": 1, "max_open_nodes": 10_000_000},
        {**STRESS, "raw_max_amount_in": 1},
        {**STRESS, "raw_max_amount_in": 10**6},
    ):
        assert validated_options(FACTORY, options) == options  # fmt: skip
    grid, raw = load_profile(PROFILES / "grid.yaml"), load_profile(PROFILES / "raw_stress.yaml")
    for profile, options, kind in ((grid, PRESET, "preset"), (raw, STRESS, "override")):
        entry = profile.algorithm_options[NAME]
        assert (entry["options"], entry["source"]["kind"]) == (options, kind)
        assert entry["settings_sha256"] == settings_sha256(options)
        assert list(profile.algorithms) == [direct_split.NAME, NAME]
        assert profile.objective.mode == "gross_only"
        assert profile.algorithm_config(FACTORY).params == {"max_splits": 4, "percent_step": 5}
        assert dict(profile.algorithm_config(direct_split.FACTORY).options) == {}
    # an explicit profile listing the identity must declare its options (no built-in default)
    doc = yaml.safe_load((PROFILES / "grid.yaml").read_text())
    del doc["algorithm_options"]
    with pytest.raises(ProfileError, match=f"requires algorithm_options.{NAME}"):
        parse_profile(doc, "<test>")
    # the shared search values are validated by the reused direct_split prepare
    bundle = load_bundle(SYNTHETIC)
    assert FACTORY.prepare is not None
    for bad in ({"max_splits": 0, "percent_step": 5}, {"max_splits": 2, "percent_step": 3},
                {"max_splits": True, "percent_step": 5}, {"percent_step": 5}):  # fmt: skip
        with pytest.raises(dsc.DirectSplitCertifiedConfigError, match="direct_split requires"):
            FACTORY.prepare(bundle, AlgorithmConfig(NAME, bad, PRESET))
    # own options validated, then cleared for the reused legacy prepare (which refuses them)
    with pytest.raises(OptionsError):
        direct_split.prepare(bundle, AlgorithmConfig(NAME, {"max_splits": 2, "percent_step": 5},
                                                     PRESET))  # fmt: skip
    prepared = _prepare(bundle, 2, 5, PRESET)
    assert dict(prepared.options) == PRESET and prepared.settings_sha256 == settings_sha256(PRESET)
    with pytest.raises(TypeError):
        prepared.options["domain"] = "raw_integer"  # type: ignore[index,unused-ignore]
    with pytest.raises(TypeError, match="PreparedCertified"):
        FACTORY.solve(bundle.cases[0], SolveContext(bundle, gross_only(), None), Budget())


# ------------------------------------------------------------------ same-domain values (P1)


@pytest.mark.parametrize(
    "inst", spec.GRID_SUITE, ids=lambda i: f"grid-n{len(i.states)}-a{i.amount}"
)
def test_complete_grid_equals_the_exhaustive_optimum_and_direct_split(inst: Instance) -> None:
    """Complete small-grid outputs equal the independent exhaustive optimum and
    `direct_split`'s untruncated value on the same domain (P1); the returned full-input plan
    replays exactly; the certificate is a zero gap and every node's bound dominates its
    region; the record passes the runtime validator."""
    bundle, case = spec.bundle_of(inst.states), inst.case()
    with metered_quotes(None) as meter:
        run = _inst(inst)
    counted = meter.counted // 2  # `_run` executes the solve twice (registered + traced)
    opt = spec.optimum(inst.values)
    res, cert = run.result, _cert(run)
    assert _audit(inst, run) == [] and _node_errors(inst, run) == []
    assert res.search_stats["truncated_by"] is None
    assert res.search_stats["consistency_failure"] is None
    ds = direct_split.solve(
        case,
        SolveContext(bundle, gross_only(), direct_split.prepare(
            bundle, AlgorithmConfig(direct_split.NAME, {"max_splits": inst.splits,
                                                        "percent_step": inst.step})),
        ),
        Budget(),
    )  # fmt: skip
    assert res.score == ds.score == opt
    _valid(bundle, case, run, counted, GRID)
    if opt is None:
        assert res.status is SolveStatus.NO_ROUTE and cert is None
        assert ds.status is SolveStatus.NO_ROUTE
        return
    assert cert is not None and cert["optimality_proven"] and cert["termination"] == "complete"
    assert res.plan is not None and res.evaluation is not None
    ev = reference_evaluate(bundle, case, res.plan, gross_only())
    assert ev.status is EvalStatus.OK and ev.gross_output == opt and ev.residuals == {}
    amounts = [int(leg["amount_in"]) for leg in res.search_stats["best_allocation"]]
    assert sum(amounts) == inst.amount and all(a > 0 for a in amounts)  # full input, no dust
    assert len(amounts) <= inst.splits


@pytest.mark.parametrize("inst", spec.RAW_SUITE, ids=lambda i: f"raw-n{len(i.states)}-a{i.amount}")
def test_complete_raw_equals_its_expanded_domain_optimum(inst: Instance) -> None:
    run = _inst(inst)
    assert _audit(inst, run) == [] and _node_errors(inst, run) == []
    opt = spec.optimum(inst.values)
    assert run.result.score == opt
    grid = spec.optimum(spec.oracle_values(inst.states, "S", inst.amount, 20, inst.splits))
    assert grid is None or opt is not None and grid <= opt  # the grid is a subset (§2.3)
    cert = _cert(run)
    if opt is not None:
        assert cert is not None and cert["optimality_proven"]
        assert _record(run)["domain"]["amount_grid"]["kind"] == "raw_integer"
        assert run.result.search_stats["percent_step"] is None


# ------------------------------------------------------------------ forced interruption


def _truncations(inst: Instance) -> Iterator[tuple[dict[str, Any], Budget]]:
    options = RAW if inst.raw else GRID
    for cap in (1, 2, 3, 5, 8, 13):
        yield {**options, "max_bound_nodes": cap}, Budget()
    for cap in (1, 2, 4):
        yield {**options, "max_open_nodes": cap}, Budget()
    for quotes in (0, 1, 2, 3, 5, 9):
        yield dict(options), Budget(max_quotes=quotes)
    for evals in (0, 1, 2):
        yield dict(options), Budget(max_candidates=evals)


TRUNCATION_SUITE = spec.GRID_SUITE[:20] + spec.RAW_SUITE[:10]


def test_forced_caps_keep_the_optimum_inside_and_cover_the_whole_frontier() -> None:
    """Node, open-node, quote and candidate caps: `L <= max <= U`, every unresolved
    feasible allocation above `L` lies in an open node whose bound covers it, the budgets are
    respected, `timeout` only without an incumbent (never `no_route`), and every stop kind
    and an honest nonzero certified gap occur (the suite is not vacuous)."""
    stops, gaps = set(), set()
    for inst in TRUNCATION_SUITE:
        opt = spec.optimum(inst.values)
        bundle, case = spec.bundle_of(inst.states), inst.case()
        for options, budget in _truncations(inst):
            with metered_quotes(None) as meter:
                run = _inst(inst, options, budget=budget)
            res, cert = run.result, _cert(run)
            assert _audit(inst, run) == [], (options, budget)
            stop = res.search_stats["truncated_by"]
            stops.add(stop)
            work = _record(run)["work"]
            if cert is not None:
                assert cert["termination"] == (stop or "complete")
                assert cert["bound_kind"] == "certified"
                gaps.add(int(cert["gap_raw"]) > 0)
            if cert is None and opt is not None:
                assert res.status is SolveStatus.TIMEOUT and stop is not None
            if budget.max_quotes is not None:
                assert work["quotes_executed"] <= budget.max_quotes
            if budget.max_candidates is not None:
                assert work["internal_evaluations"] <= budget.max_candidates
            assert work["bb_nodes_expanded"] <= options["max_bound_nodes"]
            assert work["peak_open_nodes"] <= options["max_open_nodes"]
            assert len(run.frontier) <= options["max_open_nodes"]
            assert res.candidates_truncated == int(stop == "candidate_cap")
            _valid(bundle, case, run, meter.counted // 2, options)
    assert {"node_cap", "state_cap", "quote_budget", "candidate_cap", None} <= stops
    assert True in gaps


def test_a_stop_repushes_the_expanded_node() -> None:
    """R6 at every node cap and quote budget: the certified interval always contains the
    grid optimum 58, and the frontier's union of regions covers every allocation above `L`."""
    r6 = spec.R6
    inst = Instance(spec._pair(r6["pool1"], r6["pool2"]), r6["amount_in"], 5, 2, False)
    full = _inst(inst, GRID)
    needed = _record(full)["work"]
    for cap in range(1, needed["bb_nodes_expanded"] + 2):
        run = _inst(inst, {**GRID, "max_bound_nodes": cap})
        assert _audit(inst, run) == []
        cert = _cert(run)
        assert cert is not None and int(cert["lower_raw"]) <= 58 <= int(cert["upper_raw"])
    for quotes in range(needed["quotes_executed"] + 1):
        run = _inst(inst, GRID, budget=Budget(max_quotes=quotes))
        assert _audit(inst, run) == []


# ------------------------------------------------------------------ bound mutations


GENUINE_STATE, GENUINE_INTERVAL = dsc.state_bound, dsc.interval_bound


def _minus_one(pools: Sequence[dsc.PoolView], total: int, legs_left: int) -> int | None:
    value = GENUINE_STATE(pools, total, legs_left)
    return None if value is None else value - 1


def _price(p: dsc.PoolView) -> Fraction:
    return -Fraction(p.keep * p.r_out, p.r_in)


def _top_two_state(pools: Sequence[dsc.PoolView], total: int, legs_left: int) -> int | None:
    return GENUINE_STATE(sorted(pools, key=_price)[:2], total, legs_left)


def _top_two_interval(
    p: dsc.PoolView, rest: Sequence[dsc.PoolView], total: int, lo: int, hi: int, left: int
) -> int | None:
    return GENUINE_INTERVAL(p, sorted(rest, key=_price)[:1], total, lo, hi, left)


def _hint_value(pools: Sequence[dsc.PoolView], total: int, legs_left: int) -> int | None:
    t = dsc.tangent_hint(pools, total)
    t[-1] += total - sum(t)
    return math.floor(sum((dsc.g(p, x) for p, x in zip(pools, t, strict=True)), Fraction(0)))


def _zero(*_: Any) -> int | None:
    return 0


def _unknown(*_: Any) -> int | None:
    return None


MUTATION_SUITE = spec.MUTATION_SUITE  # 57 multipool instances, max_splits >= 2
MUTANTS: dict[str, tuple[dict[str, Callable[..., int | None]], bool]] = {
    "minus_one": ({"state_bound": _minus_one}, True),
    "top_two": ({"state_bound": _top_two_state, "interval_bound": _top_two_interval}, True),
    "unknown_as_zero": ({"interval_bound": _zero}, True),
    "hint_value": ({"state_bound": _hint_value}, False),
}


@pytest.mark.parametrize("mutant", sorted(MUTANTS))
def test_underestimating_bound_mutations_are_caught(
    mutant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each mutation of the registered bound rules is caught by the independent audit; the
    first three also yield an end-to-end false zero-gap certificate somewhere (§8 item 4)."""
    patches, false_certificate = MUTANTS[mutant]
    for name, fn in patches.items():
        monkeypatch.setattr(dsc, name, fn)
    detected = false_zero = 0
    for inst in MUTATION_SUITE:
        run = _inst(inst)
        errors = _audit(inst, run) + _node_errors(inst, run)
        detected += bool(errors)
        false_zero += any("false zero gap" in e for e in errors)
    assert detected > 0
    assert (false_zero > 0) is false_certificate


def test_genuine_bounds_pass_the_mutation_audit() -> None:
    for inst in MUTATION_SUITE:
        run = _inst(inst)
        assert _audit(inst, run) == [] and _node_errors(inst, run) == []


def test_an_unknown_bound_is_never_zero_never_pruned_and_never_ranked_certified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unknown hook: a truncated search with an unknown open node emits `bound_kind:
    unknown` (null upper and gap, never 0), which the runtime validator accepts and renders as
    an unknown bound; a complete search still resolves every unknown node exhaustively."""
    monkeypatch.setattr(dsc, "interval_bound", _unknown)
    seen_unknown = 0
    for inst in MUTATION_SUITE[:15]:
        options = {**(RAW if inst.raw else GRID), "max_bound_nodes": 3}
        with metered_quotes(None) as meter:
            run = _inst(inst, options)
        cert = _cert(run)
        assert _audit(inst, run) == []
        if cert is not None and any(n.ub is None for n in run.frontier):
            seen_unknown += 1
            assert cert["bound_kind"] == "unknown" and cert["upper_raw"] is None
            assert cert["gap_raw"] is None and cert["upper_source"] is None
            assert not cert["optimality_proven"]
            bundle, case = spec.bundle_of(inst.states), inst.case()
            ctx = _context(bundle, case, run.result, meter.counted // 2, options)
            assert check_diagnostics(_record(run), ctx) == set()
            view = diagnostics_view(_record(run), ctx)
            assert view["state"] == "unknown" and "upper" not in view
            tampered = json.loads(json.dumps(_record(run)))
            tampered["certificate"].update(upper_raw="0", gap_raw="0")
            assert "C_UNCERTIFIED_BOUND" in check_diagnostics(tampered, ctx)
    assert seen_unknown > 0
    for inst in MUTATION_SUITE[:15]:
        run = _inst(inst)
        assert _audit(inst, run) == [] and run.result.score == spec.optimum(inst.values)


# ------------------------------------------------------------------ counterexamples


def test_r2_integer_marginals_one_unit_legs_are_infeasible() -> None:
    state = spec.cp("p", *spec.RECON["R2_integer_marginals"]["reserves"])
    inst = Instance((state, spec.cp("q", 1000, 1000)), 3, 5, 2, raw=True)
    run = _inst(inst)
    cert = _cert(run)
    assert run.result.score == spec.optimum(inst.values) == 2  # 1 + 2 has a dust leg
    assert cert is not None and cert["optimality_proven"]


def test_r3_plateau_incumbent_is_never_certified() -> None:
    r3 = spec.R3
    inst = Instance(spec._pair(r3["pool1"], r3["pool2"]), r3["amount_in"], 5, 2, raw=True)
    run = _inst(inst)
    cert = _cert(run)
    assert run.result.score == r3["raw_integer_max"] == 76
    assert cert is not None and cert["optimality_proven"]
    assert run.trace is not None
    root = run.trace[0]  # the first bound evaluation covers the whole domain
    assert root.kind == "state" and root.ub is not None and root.ub > 70  # above the plateau
    capped = _inst(inst, {**RAW, "max_bound_nodes": 1})
    capped_cert = _cert(capped)
    assert capped_cert is not None and int(capped_cert["upper_raw"]) >= 76


def test_r6_pool_order_is_part_of_the_domain_and_does_not_transfer() -> None:
    r6 = spec.R6
    runs = {}
    for order in ("12", "21"):
        inst = Instance(spec._pair(r6["pool1"], r6["pool2"], order), r6["amount_in"],
                        r6["percent_step"], r6["max_splits"], False)  # fmt: skip
        runs[order] = _inst(inst)
        assert _audit(inst, runs[order]) == []
    raw = _inst(Instance(spec._pair(r6["pool1"], r6["pool2"]), r6["amount_in"], 5, 2, raw=True))
    assert runs["12"].result.score == r6["grid_optimum_order_p1_p2"] == 58
    assert runs["21"].result.score == r6["grid_optimum_order_p2_p1"] == 59
    assert raw.result.score == r6["raw_integer_optimum"] == 59
    hashes = {k: _record(r)["candidate_domain_hash"] for k, r in runs.items()}
    assert len({*hashes.values(), _record(raw)["candidate_domain_hash"]}) == 3
    # on the shared contract's bundle reference they are WHI-1547's grid38/raw38 domains
    shared = spec.VALIDATOR.EXAMPLES["domain_hashes"]
    for order, raw_domain, key in (("12", False, "grid38_p1p2"), ("21", False, "grid38_p2p1"),
                             ("12", True, "raw38")):  # fmt: skip
        bundle = spec.bundle_of(
            spec._pair(r6["pool1"], r6["pool2"], order), "fixture:R021-FX-GRID38"
        )
        case = Case("r6", "S", "T", r6["amount_in"])
        run = _run(bundle, case, 2, 5, RAW if raw_domain else GRID)
        assert _record(run)["candidate_domain_hash"] == shared[key]
    cert = _cert(runs["12"])
    assert cert is not None and int(cert["upper_raw"]) < 59  # the grid proof is not raw's


def test_dead_dust_fee_source_and_overflow_pools_are_exact() -> None:
    """Dead pools are never quoted, dust legs and fee/source refusals are infeasible, and a
    sourced pool a few units below the uint112 revert is exact on both sides of it."""
    top = (1 << 112) - 10
    cases = [
        ((spec.cp("dead", 0, 500), spec.cp("a", 1000, 1000)), 100, 5, 2),
        ((spec.cp("a", 1000, 1000), spec.cp("b", 1000, 1000)), 3, 5, 3),  # dust
        ((spec.cp("fee", 1000, 1000, 25, "moe_classic_v1"), spec.cp("b", 900, 1000)), 50, 10, 2),
        ((spec.cp("unk", 1000, 1000, 30, "other"), spec.cp("b", 900, 1000)), 50, 10, 2),
        ((spec.cp("moe", top, 1 << 111, 30, "moe_classic_v1"), spec.cp("b", 90, 120)), 20, 5, 2),
        ((spec.cp("moe", top, 1 << 111, 30, "moe_classic_v1"), spec.cp("b", 90, 120)), 9, 5, 2),
        ((spec.cp("dead", 0, 1), spec.cp("dead2", 5, 0)), 10, 5, 2),  # nothing live
    ]
    for states, amount, step, splits in cases:
        for raw in (False, True):
            inst = Instance(states, amount, step, splits, raw)
            bundle = spec.bundle_of(states)
            with metered_quotes(None) as meter:
                run = _inst(inst)
            assert _audit(inst, run) == [] and _node_errors(inst, run) == []
            assert run.result.score == spec.optimum(inst.values)
            dead = [p.pool_id for p in states if 0 in (p.reserve0, p.reserve1)]
            assert run.result.search_stats["live_pools"] == len(states) - len(dead)
            _valid(bundle, inst.case(), run, meter.counted // 2, RAW if raw else GRID)
            if len(dead) == len(states):
                assert run.result.status is SolveStatus.NO_ROUTE
                assert _record(run)["work"]["quotes_executed"] == 0


# ------------------------------------------------------------------ scope and unsupported


def test_non_cpmm_pairs_and_net_objectives_are_visible_unsupported_rows() -> None:
    mixed = load_bundle(MIXED)
    for case in mixed.cases:
        pools = mixed.pools_for_pair(case.token_in, case.token_out)
        assert any(not isinstance(p, ConstantProductPoolState) for p in pools)
        with metered_quotes(None) as meter:
            run = _run(mixed, case, 4, 5, PRESET)
        rec = _record(run)
        assert run.result.status is SolveStatus.UNSUPPORTED and run.result.plan is None
        assert rec["scope"] == {"supported": False, "reason": "non_constant_product_direct_pool"}
        assert (
            rec["certificate"] is None and rec["certificate_unavailable_reason"] == "not_produced"
        )
        assert rec["work"]["quotes_executed"] == 0 == meter.counted
        assert rec["domain"]["pool_order"] == [p.pool_id for p in pools]  # no hidden subset
        assert rec["domain"]["universe"]["pools"] == sorted(p.pool_id for p in pools)
        ctx = _context(mixed, case, run.result, 0, PRESET)
        assert check_diagnostics(rec, ctx) == set()
        assert diagnostics_view(rec, ctx)["scope"] == rec["scope"]
    inst = spec.GRID_SUITE[0]
    bundle = spec.bundle_of(inst.states)
    for objective in (synthetic_fixed_cost(1), synthetic_fixed_cost(0)):
        net = _run(bundle, inst.case(), 2, 5, PRESET, objective=objective)
        assert net.result.status is SolveStatus.UNSUPPORTED
        assert _record(net)["scope"]["reason"] == "objective_not_gross_only"
        assert _record(net)["work"]["quotes_executed"] == 0
        _valid(bundle, inst.case(), net, 0, PRESET, objective="synthetic_fixed_cost")
    big = Instance(inst.states, 50, 5, 2, raw=True)
    capped = _inst(big, {**RAW, "raw_max_amount_in": 49})
    assert capped.result.status is SolveStatus.UNSUPPORTED
    assert _record(capped)["scope"]["reason"] == "raw_integer_amount_above_limit"
    assert _inst(big, {**RAW, "raw_max_amount_in": 50}).result.status is SolveStatus.OK


def test_no_direct_pool_is_a_complete_empty_domain_no_route() -> None:
    bundle = load_bundle(SYNTHETIC)
    case = bundle.case("direct_no_route")
    run = _run(bundle, case, 4, 5, PRESET)
    rec = _record(run)
    assert run.result.status is SolveStatus.NO_ROUTE and rec["certificate"] is None
    assert rec["domain"]["pool_order"] == [] and rec["domain"]["universe"]["pools"] == []
    assert rec["scope"] == {"supported": True, "reason": None}
    assert check_diagnostics(rec, _context(bundle, case, run.result, 0, PRESET)) == set()


# ------------------------------------------------------------------ hard kill


def test_a_hard_killed_solve_leaves_only_complete_published_plans() -> None:
    """The worker's hard meter at every executed quote: `QuoteLimitExceeded` escapes the solve
    (no result, so no certificate), and the publications are a prefix of the uninterrupted
    strictly improving sequence, each a complete valid full-input plan."""
    inst = next(i for i in spec.GRID_SUITE
                if _record(_inst(i))["work"]["internal_evaluations"] >= 2)  # fmt: skip
    bundle, case = spec.bundle_of(inst.states), inst.case()
    prepared = _prepare(bundle, inst.splits, inst.step, GRID)
    full: list[RoutePlan] = []
    context = SolveContext(bundle, gross_only(), prepared, candidate_sink=full.append)
    result = FACTORY.solve(case, context, Budget())
    needed = _record_of(result)["work"]["quotes_executed"]
    seen = set()
    for kill_at in range(needed):
        sink: list[RoutePlan] = []
        ctx = dataclasses.replace(context, candidate_sink=sink.append)
        with metered_quotes(kill_at), pytest.raises(QuoteLimitExceeded):
            FACTORY.solve(case, ctx, Budget())
        assert sink == full[: len(sink)]
        for plan in sink:
            ev = reference_evaluate(bundle, case, plan, gross_only())
            assert ev.status is EvalStatus.OK and ev.residuals == {}
        seen.add(len(sink))
    assert 0 in seen and len(seen) > 1
    # a certificate presented for a hard-killed attempt is refused (C_KILLED)
    rec = _record_of(result)
    ctx_killed = _context(bundle, case, result, None, GRID, hard_killed=True)
    assert "C_KILLED" in check_diagnostics(rec, ctx_killed)


def _record_of(result: SolveResult) -> dict[str, Any]:
    rec: dict[str, Any] = result.search_stats["r021"]
    return rec


def test_a_runner_wall_kill_keeps_no_certificate(tmp_path: Path) -> None:
    """Through the real runner and spawned worker: a hard wall kill of a long raw stress solve
    is `timeout` with the runner's `unavailable (hard_timeout)` view and at most its last
    published plan as `last_valid_candidate`; nothing of the solver's certificate survives."""
    from benchmark.results import load_case_records
    from benchmark.runner import run_experiment
    from snapshot.models import BlockRef

    pools = [
        spec.cp("p0", 498_000, 523_000), spec.cp("p1", 877_000, 921_000),
        spec.cp("p2", 331_000, 347_000), spec.cp("p3", 612_000, 640_000),
    ]  # fmt: skip
    bundle = write_bundle(
        tmp_path / "bundle", bundle_id="dsc-heavy", kind="synthetic",
        block=BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0),
        pools=pools, cases=[Case("heavy", "S", "T", 100_000)],
    )  # fmt: skip
    doc = yaml.safe_load((PROFILES / "raw_stress.yaml").read_text())
    doc["algorithms"] = [NAME]
    doc["budget"]["time_limit_seconds"] = 0.2
    profile = parse_profile(doc, "<test>")
    manifest = run_experiment(bundle, profile, results_dir=tmp_path / "runs", replay_command="-")
    (record,) = load_case_records(tmp_path / "runs" / manifest.run_id)
    assert record["status"] == "timeout" and record["limit_hit"] == "time"
    assert record["diagnostics"]["state"] == "unavailable"
    assert record["diagnostics"]["reason"] == "hard_timeout"
    assert record["diagnostics"]["origin"] == "runner"
    assert "r021" not in (record.get("search") or {})
    candidate = record.get("last_valid_candidate")
    if candidate:
        assert candidate["evaluation"]["status"] == "ok"


# ------------------------------------------------------------------ consistency failure (§5.6)


REAL_EVALUATE = reference_evaluate


def _faulty(fault: Callable[[Evaluation], Evaluation], from_call: int) -> tuple[Any, list[int]]:
    calls = [0]

    def fake(*args: Any, **kwargs: Any) -> Evaluation:
        calls[0] += 1
        real = REAL_EVALUATE(*args, **kwargs)
        return fault(real) if calls[0] >= from_call else real

    return fake, calls


FAULTS: dict[str, Callable[[Evaluation], Evaluation]] = {
    "gross_plus_one": lambda e: dataclasses.replace(e, gross_output=e.gross_output + 1),
    "gross_minus_one": lambda e: dataclasses.replace(e, gross_output=e.gross_output - 1),
    "invalid_plan": lambda e: dataclasses.replace(e, status=EvalStatus.INVALID_PLAN),
}


def _solve_only(inst: Instance, sink: list[RoutePlan]) -> SolveResult:
    bundle = spec.bundle_of(inst.states)
    prepared = _prepare(bundle, inst.splits, inst.step, RAW if inst.raw else GRID)
    context = SolveContext(bundle, gross_only(), prepared, candidate_sink=sink.append,
                           run_identity={"git_revision": REVISION})  # fmt: skip
    return FACTORY.solve(inst.case(), context, Budget())


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_an_inconsistent_first_candidate_is_an_algorithm_error(
    fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parent repro: one (1000, 1000) pool, input 100, quoted 90. A disagreeing replay is never
    published or certified and the solve is neither `ok`, `no_route` nor `timeout`."""
    inst = Instance((spec.cp("p", 1000, 1000),), 100, 5, 1, False)
    assert _solve_only(inst, []).score == spec.optimum(inst.values) == 90
    fake, calls = _faulty(FAULTS[fault], 1)
    monkeypatch.setattr(dsc, "evaluate", fake)
    published: list[RoutePlan] = []
    res = _solve_only(inst, published)
    assert calls[0] == 1 and published == []
    assert res.status is SolveStatus.ALGORITHM_ERROR and res.error
    assert res.plan is None and res.score is None
    rec = _record_of(res)
    assert rec["certificate"] is None and rec["certificate_unavailable_reason"] == "not_produced"
    assert res.search_stats["consistency_failure"]["quoted_value"] == "90"
    assert res.search_stats["consistency_failure"]["legs"] == [[0, 20]]
    assert res.search_stats["truncated_by"] == "consistency_failure"
    ctx = _context(spec.bundle_of(inst.states), inst.case(), res, rec["work"]["quotes_executed"],
                   GRID)  # fmt: skip
    assert check_diagnostics(rec, ctx) == set()
    assert diagnostics_view(rec, ctx)["state"] == "unavailable"


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_an_inconsistent_later_candidate_keeps_the_validated_incumbent_uncertified(
    fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    r6 = spec.R6
    inst = Instance(spec._pair(r6["pool1"], r6["pool2"]), r6["amount_in"], 5, 2, False)
    clean = _solve_only(inst, [])
    assert _record_of(clean)["work"]["internal_evaluations"] == 2 and clean.score == 58
    fake, calls = _faulty(FAULTS[fault], 2)
    monkeypatch.setattr(dsc, "evaluate", fake)
    published: list[RoutePlan] = []
    res = _solve_only(inst, published)
    assert calls[0] == 2
    assert res.status is SolveStatus.OK and res.score == r6["single_pool_incumbent"] == 57
    assert published == [res.plan] and res.plan is not None
    replay = reference_evaluate(spec.bundle_of(inst.states), inst.case(), res.plan, gross_only())
    assert replay.status is EvalStatus.OK and replay.gross_output == 57
    rec = _record_of(res)
    assert rec["certificate"] is None and rec["certificate_unavailable_reason"] == "not_produced"
    assert res.search_stats["consistency_failure"]["evaluated_score"] != "58"
    assert res.search_stats["truncated_by"] == "consistency_failure"
    assert res.search_stats["termination"] is None
    assert [a["amount_in"] for a in res.search_stats["best_allocation"]] == ["38"]


def test_consistency_failures_never_yield_a_certificate_across_the_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    later = 0
    for inst in MUTATION_SUITE[:20]:
        evaluations = _record_of(_solve_only(inst, []))["work"]["internal_evaluations"]
        bundle, case = spec.bundle_of(inst.states), inst.case()
        for fault in FAULTS.values():
            for index in range(1, evaluations + 1):
                fake, _ = _faulty(fault, index)
                monkeypatch.setattr(dsc, "evaluate", fake)
                published: list[RoutePlan] = []
                res = _solve_only(inst, published)
                monkeypatch.setattr(dsc, "evaluate", REAL_EVALUATE)
                assert _record_of(res)["certificate"] is None
                assert res.search_stats["consistency_failure"] is not None
                for plan in published:
                    ev = REAL_EVALUATE(bundle, case, plan, gross_only())
                    assert ev.status is EvalStatus.OK
                if index == 1:
                    assert res.status is SolveStatus.ALGORITHM_ERROR and not published
                else:
                    assert res.status is SolveStatus.OK and published[-1] is res.plan
                    later += 1
    assert later > 0


# ------------------------------------------------------------------ work ledger


def test_the_ledger_is_the_meter_and_bound_work_is_a_separate_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`quotes_executed` is the worker meter of the one solve; `internal_evaluations` is the
    exact number of in-solve `evaluate` calls (counted here by an independent wrapper); one
    bound evaluation per created node; expansions and the open peak within their caps."""
    calls = [0]

    def counting(*args: Any, **kwargs: Any) -> Evaluation:
        calls[0] += 1
        return REAL_EVALUATE(*args, **kwargs)

    monkeypatch.setattr(dsc, "evaluate", counting)
    for inst in spec.GRID_SUITE[:12] + spec.RAW_SUITE[:6]:
        bundle = spec.bundle_of(inst.states)
        prepared = _prepare(bundle, inst.splits, inst.step, RAW if inst.raw else GRID)
        calls[0] = 0
        trace: list[dsc.Node] = []
        with metered_quotes(None) as meter:
            run = dsc.certify(bundle, inst.case(), gross_only(), prepared, Budget(),
                              git_revision=REVISION, cohort="fixture", trace=trace)  # fmt: skip
        work = _record(run)["work"]
        assert set(work) == set(dsc.WORK_UNITS)
        assert work["quotes_executed"] == meter.counted
        assert work["internal_evaluations"] == calls[0] == run.result.candidates_considered
        assert work["bound_evaluations"] == len(trace)
        if trace:
            assert work["bound_evaluations"] >= work["bb_nodes_expanded"]
            assert work["peak_open_nodes"] >= 1


def test_case_order_and_a_shared_prepared_object_leak_no_state() -> None:
    bundle = load_bundle(CPMM)
    prepared = _prepare(bundle, 4, 5, PRESET)
    context = SolveContext(bundle, gross_only(), prepared, run_identity={"git_revision": "x"})
    forward = [FACTORY.solve(c, context, Budget()) for c in bundle.cases]
    backward = [FACTORY.solve(c, context, Budget()) for c in reversed(bundle.cases)]
    assert forward == list(reversed(backward))
    fresh = [
        FACTORY.solve(c, dataclasses.replace(context, prepared=_prepare(bundle, 4, 5, PRESET)),
                      Budget())
        for c in bundle.cases
    ]  # fmt: skip
    assert fresh == forward and dict(prepared.options) == PRESET


# ------------------------------------------------------------------ published records, real state


@pytest.mark.parametrize("ex", spec.FIX["examples"], ids=lambda e: e["id"])
def test_published_records_are_regenerated_by_the_registered_search(ex: dict[str, Any]) -> None:
    """The 11 WHI-1551 records (same numbers with Rule T): the registered search with the
    records' cohort label reproduces each byte for byte; through `FACTORY.solve` the record
    differs at most in the runtime cohort of a real (non-synthetic) fixture and its hash."""
    bundle, case = spec._example_inputs(ex)
    prepared = _prepare(bundle, ex["max_splits"], ex["percent_step"], ex["options"])
    budget = Budget(**ex.get("budget", {}))
    run = dsc.certify(bundle, case, gross_only(), prepared, budget,
                      git_revision=spec.BASE_REVISION, cohort="fixture")  # fmt: skip
    assert _record(run) == ex["record"]
    assert run.result.status.value == ex["status"]
    assert (None if run.result.score is None else str(run.result.score)) == ex["final_score"]
    context = SolveContext(bundle, gross_only(), prepared,
                           run_identity={"git_revision": spec.BASE_REVISION})  # fmt: skip
    solved = FACTORY.solve(case, context, budget)
    rec = json.loads(json.dumps(solved.search_stats["r021"]))
    if bundle.kind == "synthetic":
        assert rec == ex["record"]
        return
    assert rec["domain"]["universe"]["cohort"] == "full_source"
    rec["domain"]["universe"]["cohort"] = "fixture"
    for part in (rec, rec["certificate"] or {}):
        if "candidate_domain_hash" in part:
            part["candidate_domain_hash"] = ex["record"]["candidate_domain_hash"]
    assert rec == ex["record"]


def test_real_moe_classic_states_certify_the_direct_split_value() -> None:
    """Admitted real CPMM smoke: every case of the committed Merchant Moe Classic bundle
    (moe_classic_v1, fee 30, uint112) at the profile grid equals direct_split and is proven."""
    bundle = load_bundle(MOE)
    ds_prepared = direct_split.prepare(
        bundle, AlgorithmConfig(direct_split.NAME, {"max_splits": 4, "percent_step": 5})
    )
    proven = 0
    for case in bundle.cases:
        with metered_quotes(None) as meter:
            run = _run(bundle, case, 4, 5, PRESET)
        ds = direct_split.solve(case, SolveContext(bundle, gross_only(), ds_prepared), Budget())
        assert run.result.status is ds.status and run.result.score == ds.score
        cert = _cert(run)
        if cert is not None:
            assert cert["optimality_proven"] and cert["termination"] == "complete"
            proven += 1
        _valid(bundle, case, run, meter.counted // 2, PRESET)
    assert proven > 0


def test_runtime_validator_binds_the_certificate_to_run_request_and_score() -> None:
    """Tampering with any certificate identity, request or bound field is caught by the
    runtime validator against the independently supplied run context."""
    bundle = load_bundle(SYNTHETIC)
    case = bundle.case("direct_best_pool")
    with metered_quotes(None) as meter:
        run = _run(bundle, case, 4, 5, PRESET)
    ctx = _context(bundle, case, run.result, meter.counted // 2, PRESET)
    rec = _record(run)
    assert check_diagnostics(rec, ctx) == set()
    assert diagnostics_view(rec, ctx)["state"] == "certified"

    def tamper(mutate: Callable[[dict[str, Any]], None]) -> set[str]:
        copy: dict[str, Any] = json.loads(json.dumps(rec))
        mutate(copy)
        return check_diagnostics(copy, ctx)

    def source(key: str, value: str) -> Callable[[dict[str, Any]], None]:
        return lambda r: r["certificate"]["source"].__setitem__(key, value)

    def request(key: str, value: str) -> Callable[[dict[str, Any]], None]:
        return lambda r: r["certificate"]["request"].__setitem__(key, value)

    lower = int(rec["certificate"]["lower_raw"])
    assert tamper(source("git_revision", "0" * 40)) == {"C_IDENTITY"}
    assert tamper(source("bundle_hash", "x")) == {"C_IDENTITY"}
    assert tamper(source("effective_settings_sha256", settings_sha256(STRESS))) == {"C_IDENTITY"}
    assert tamper(source("algorithm", "direct_split")) == {"C_IDENTITY"}
    assert tamper(request("amount_in", str(case.amount_in + 1))) == {"C_REQUEST"}
    assert tamper(request("case_id", "other")) == {"C_REQUEST"}
    assert "C_LOWER_EVAL" in tamper(lambda r: r["certificate"].update(
        lower_raw=str(lower - 1), gap_raw=str(int(r["certificate"]["upper_raw"]) - lower + 1),
        optimality_proven=False))  # fmt: skip
    assert "C_GAP" in tamper(lambda r: r["certificate"].update(gap_raw="7"))
    assert "C_OPTIMALITY" in tamper(lambda r: r["certificate"].update(optimality_proven=False))
    assert "C_DOMAIN" in tamper(lambda r: r["certificate"].update(candidate_domain_hash="0" * 64))
    assert "D_HASH" in tamper(lambda r: r["domain"]["splits"].update(max=3))
    assert "W_LEDGER" in tamper(lambda r: r["work"].update(quotes_executed=10**6))
    assert "C_TERMINATION" in tamper(lambda r: r["certificate"].update(
        upper_raw=str(lower + 3), gap_raw="3", optimality_proven=False))  # fmt: skip
    assert "C_IDENTITY" in check_diagnostics(rec, dataclasses.replace(ctx, run={
        **ctx.run, "git_revision": None}))  # fmt: skip
    # a direct call without a runner identity cannot produce a bindable certificate
    blind = _run(bundle, case, 4, 5, PRESET, revision="")
    assert "C_IDENTITY" in check_diagnostics(_record(blind), ctx)


# ------------------------------------------------------------------ CLI: run + quote --details


def _saved_run(out: str) -> Path:
    marker = "(run "
    return Path(out[out.index(marker) + len(marker) :].split(")", 1)[0])


def test_cli_run_replay_report_and_quote_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both CLI paths through the spawned worker and the real factory, next to the same-grid
    `direct_split`: equal values where supported, certified/unavailable views with domain,
    bound, termination and scope, the options identity, literal `--strategies profile` replay,
    the offline report, and one solve per quote."""
    import main
    from benchmark.results import load_case_records, load_manifest
    from benchmark.runner import compare_runs

    profile = PROFILES / "grid.yaml"
    for bundle_dir in (SYNTHETIC, CPMM, MIXED):
        results = tmp_path / bundle_dir.name / "runs"
        argv = ["run", "--bundle", str(bundle_dir), "--profile", str(profile),
                "--results-dir", str(results), "--strategies", "profile"]  # fmt: skip
        assert main.main(argv) == 0
        out = capsys.readouterr().out
        assert f"Experimental and other strategies (1): {NAME}" in out
        (run_dir,) = results.iterdir()
        manifest = load_manifest(run_dir)
        assert list(manifest.algorithms) == [direct_split.NAME, NAME]
        entry = manifest.resolved_profile["algorithm_options"][NAME]
        assert entry["options"] == PRESET and entry["source"]["kind"] == "preset"
        records = load_case_records(run_dir)
        ref = {r["case_id"]: r for r in records if r["algorithm"] == direct_split.NAME}
        own = [r for r in records if r["algorithm"] == NAME]
        assert len(own) == len(ref) == len(manifest.measurement["case_order"]) > 0
        for r in own:
            view, base = r["diagnostics"], ref[r["case_id"]]
            assert view["codes"] == [] and view["origin"] == "solver"
            assert view["work"]["quotes_executed"] == view["checked_against"]["quotes_counted"]
            assert (
                view["checked_against"]["run"]["effective_settings_sha256"]
                == entry["settings_sha256"]
            )
            if r["status"] == "ok":
                assert (r["score"], base["status"]) == (base["score"], "ok")  # P1
                assert view["state"] == "certified" and view["optimality_proven"] is True
                assert view["termination"] == "complete" and view["domain"]["grid"] == (
                    "repository_grid"
                )
            elif r["status"] == "unsupported":
                assert bundle_dir == MIXED and view["state"] == "unavailable"
                assert view["scope"]["reason"] == "non_constant_product_direct_pool"
            else:
                assert r["status"] == base["status"] == "no_route"
        replay = shlex.split(manifest.replay_command)
        assert replay[-2:] == ["--strategies", "profile"]
        assert main.main(replay[replay.index("main.py") + 1 :]) == 0
        replayed = next(p for p in results.iterdir() if p != run_dir)
        assert load_manifest(replayed).resolved_profile == manifest.resolved_profile
        assert compare_runs(run_dir, replayed) == []
        report = tmp_path / bundle_dir.name / "report"
        assert main.main(["report", str(run_dir), "--output", str(report)]) == 0
        html = (report / "report.html").read_text()
        assert NAME in html and ("certified [" in html or "UNSUPPORTED" in html)
        capsys.readouterr()
    # the raw stress profile: a separately identified domain, explicit profile selection only
    results = tmp_path / "raw" / "runs"
    argv = ["run", "--bundle", str(CPMM), "--profile", str(PROFILES / "raw_stress.yaml"),
            "--results-dir", str(results), "--strategies", "profile"]  # fmt: skip
    assert main.main(argv) == 0
    (run_dir,) = results.iterdir()
    stress_rows = {r["case_id"]: r for r in load_case_records(run_dir) if r["algorithm"] == NAME}
    assert stress_rows["a_b_dust"]["diagnostics"]["domain"]["grid"] == "raw_integer"
    assert stress_rows["a_b_dust"]["diagnostics"]["state"] == "certified"
    assert stress_rows["a_b_large"]["status"] == "unsupported"  # 150000000 > raw_max_amount_in
    assert (
        stress_rows["a_b_large"]["diagnostics"]["scope"]["reason"]
        == "raw_integer_amount_above_limit"
    )
    capsys.readouterr()
    # quote --details: one solve each; the corpus fixture pair has CL/LB direct pools
    quotes = tmp_path / "quotes"
    argv = ["quote", "--bundle", str(CORPUS), "--profile", str(profile), "--token-in", "USDC",
            "--token-out", "USDT0", "--amount", "1500.25", "--details",
            "--quotes-dir", str(quotes), "--strategies", "profile"]  # fmt: skip
    assert main.main(argv) == 0
    out = capsys.readouterr().out
    assert f"[{NAME}] unsupported" in out
    assert "scope: UNSUPPORTED (non_constant_product_direct_pool)" in out
    assert "bound: unavailable (not_produced)" in out
    assert "max_candidates unit: finalist_plans_evaluated" in out
    assert "bb_nodes_expanded 0 [search]" in out and "peak_open_nodes 0 [memory]" in out
    quote_run = _saved_run(out)
    quote_records = load_case_records(quote_run)
    assert [r["algorithm"] for r in quote_records] == [direct_split.NAME, NAME]
    for r in quote_records:
        assert r["measurement"]["attempts_completed"] == 1
        assert len(r["measurement"]["solve_seconds"]) == 1
    saved = yaml.safe_load((quote_run.parent.parent / "profile.yaml").read_text())
    assert saved["algorithm_options"] == {NAME: PRESET}


def test_saved_pre_whi_1552_profiles_replay_literally() -> None:
    """A saved effective profile (8/9/10/11 strategies) never gains the new identity under
    `--strategies profile`, and `base` / `optimized` never select it."""
    source = yaml.safe_load((REPO / "config" / "daily_gross.yaml").read_text())
    document, _ = derive(source, "all", source_path="s", source_sha256="x")
    cyc = "uni_sor_cycle_safe"  # WHI-1556, added after this identity
    for drop in (
        [NAME, cyc],
        [NAME, metis_history.NAME, cyc],
        [NAME, metis_history.NAME, incremental_graph_repair.NAME, cyc],
        [NAME, metis_history.NAME, incremental_graph_repair.NAME, cyc, "metis_inspired"],
    ):
        saved = json.loads(json.dumps(document))  # fmt: skip
        saved["algorithms"] = [a for a in saved["algorithms"] if a not in drop]
        saved["selection"]["groups"]["custom"] = [
            a for a in saved["selection"]["groups"]["custom"] if a not in drop
        ]
        options = {k: v for k, v in saved["algorithm_options"].items() if k not in drop}
        saved.pop("algorithm_options")
        if options:
            saved["algorithm_options"] = options
        literal, profile = derive(saved, "profile", source_path="s", source_sha256="x")
        assert literal == saved and NAME not in profile.algorithms
        assert len(profile.algorithms) == 13 - len(drop)
    for mode in ("base", "optimized"):
        assert NAME not in derive(source, mode, source_path="s", source_sha256="x")[1].algorithms

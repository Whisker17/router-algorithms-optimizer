"""WHI-1558 component B: the registered `cfmm_dual` factory and its integer recovery
(`cfmm-dual.md` §§4-9, 11.1 gates G-C3..G-C9; G-C1/G-C2/G-C3b are component A's
`test_cfmm_model.py` / `test_cfmm_optimizer.py`, re-run end to end here through the factory).

Every behavioural check runs the REAL registered factory (`get_algorithm(NAME)`: its public
`prepare` and `solve`) or the real recovery module `routing.cfmm.recovery`. Expected values
never come from the implementation: they come from the committed WHI-1557 fixtures
(`model_reference.json` stored flows/grosses/budgets), a hand `getAmountOut` ledger and brute
force, the plain memo-free evaluator, independent counters (monkeypatched wrappers that only
count), the worker quote meter and the runtime validator (`benchmark.diagnostics`) against an
independently built run context. Faults are `monkeypatch`es of module names in the test; the
production modules patch nothing.
"""

from __future__ import annotations

import dataclasses
import json
import math
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

import cfmm_contract_model as spec  # sibling: the WHI-1557 executable contract (CL fixture)
import pytest
import yaml
from test_cfmm_model import author_network, bundle_of, cp

import benchmark.profile as profile_module
import routing.algorithms.single_path as single_path_module
import routing.cfmm.model as cm
import routing.cfmm.optimizer as co
import routing.cfmm.recovery as recovery_module
from benchmark.diagnostics import CheckContext, check_diagnostics, diagnostics_view, pool_protocol
from benchmark.objective import gross_only, synthetic_fixed_cost
from benchmark.profile import ProfileError, load_profile, parse_profile, preset_options
from benchmark.strategies import R021_ADDITIONS, derive
from pools.constant_product import get_amount_out
from pools.quote import QuoteLimitExceeded, metered_quotes
from pools.result import QuoteStatus
from routing.algorithms import cfmm_dual as cd
from routing.algorithms import uni_sor_cycle_safe
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
from routing.cfmm.model import Trade
from routing.cfmm.recovery import RecoveryOptions, ResolveSkipped, recover
from routing.evaluator import EvalStatus, Evaluation
from routing.evaluator import evaluate as reference_evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, RoutePlan
from routing.search import QuoteCache, build_graph_index, enumerate_paths, path_plan
from snapshot.bundle import load_bundle, write_bundle
from snapshot.models import (
    Case,
    ConstantProductPoolState,
    LiquidityBookPoolState,
    SnapshotBundle,
)

REPO = Path(__file__).resolve().parents[2]
NAME = "cfmm_dual"
FACTORY = get_algorithm(NAME)
DOC = REPO / "docs" / "references" / "research-021" / "cfmm-dual.md"
CONTRACT = json.loads(
    DOC.read_text()
    .split("<!-- cfmm-dual-contract -->", 1)[1]
    .split("```json\n", 1)[1]
    .split("\n```", 1)[0]
)
PRESET: dict[str, Any] = CONTRACT["preset"]
MODEL = json.loads((REPO / "tests" / "fixtures" / "cfmm" / "model_reference.json").read_text())
EXAMPLES = json.loads(
    (REPO / "docs" / "references" / "research-021" / "fixtures" / "examples.json").read_text()
)
FIXTURES = REPO / "tests" / "fixtures"
MIXED = FIXTURES / "routing" / "mantle_mixed"
CPMM_GRAPH = FIXTURES / "routing" / "cpmm_graph"
SYNTHETIC = FIXTURES / "synthetic"
CORPUS = FIXTURES / "corpus" / "bundle"
LB = FIXTURES / "moe_lb" / "bundle"
PROFILES = REPO / "config" / NAME
REVISION = "b" * 40  # the run's source revision (runner identity)


# ------------------------------------------------------------------ helpers


@cache
def _mixed() -> SnapshotBundle:
    return load_bundle(MIXED)


def _prepare(bundle: SnapshotBundle, max_hops: int = 3, **over: Any) -> cd.PreparedCfmm:
    assert FACTORY.prepare is not None
    prepared = FACTORY.prepare(
        bundle, AlgorithmConfig(NAME, {"max_hops": max_hops}, {**PRESET, **over})
    )
    assert isinstance(prepared, cd.PreparedCfmm)
    return prepared


def _solve(
    bundle: SnapshotBundle,
    case: Case,
    budget: Budget | None = None,
    *,
    objective: Any = None,
    sink: list[RoutePlan] | None = None,
    prepared: cd.PreparedCfmm | None = None,
    revision: str = REVISION,
    **over: Any,
) -> SolveResult:
    prepared = prepared or _prepare(bundle, **over)
    context = SolveContext(
        bundle,
        objective or gross_only(),
        prepared,
        candidate_sink=None if sink is None else sink.append,
        run_identity={"git_revision": revision},
    )
    return FACTORY.solve(case, context, budget or Budget())


def _r021(result: SolveResult) -> dict[str, Any]:
    rec: dict[str, Any] = result.search_stats["r021"]
    return rec


def _cfmm(result: SolveResult) -> dict[str, Any]:
    stats: dict[str, Any] = result.search_stats["cfmm"]
    return stats


def _context(
    bundle: SnapshotBundle,
    case: Case,
    result: SolveResult,
    counted: int | None,
    options: Mapping[str, Any] = PRESET,
    *,
    objective: str = "gross_only",
    hard_killed: bool = False,
) -> CheckContext:
    """What the runner supplies independently of the solver (score: a fresh replay)."""
    score = None
    status = result.status.value
    if result.plan is not None:
        ev = reference_evaluate(bundle, case, result.plan, gross_only())
        score = str(ev.gross_output) if ev.status is EvalStatus.OK else None
        status = "ok" if ev.status is EvalStatus.OK else "invalid_plan"
    return CheckContext(
        run={
            "git_revision": REVISION,
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
        status=status,
        score=score,
        objective=objective,
        quotes_counted=counted,
        hard_killed=hard_killed,
        pools={pid: pool_protocol(p) for pid, p in bundle.pools.items()},
    )


def _invariants(bundle: SnapshotBundle, case: Case, plan: RoutePlan | None) -> int:
    """§6 invariants of a returned plan under a FRESH, memo-free evaluator replay: ok, the
    whole request consumed, no residual fund, one merged step per market, integer or
    `ALL_REMAINING` amounts, every executed leg with positive input and output, no token
    cycle (topological order exists), only request/step funds (nothing borrowed)."""
    assert plan is not None
    ev = reference_evaluate(bundle, case, plan, gross_only())
    assert ev.status is EvalStatus.OK, ev.error
    assert ev.residuals == {}
    request = next(f for f in ev.funds if f.fund_id == REQUEST_FUND_ID)
    assert request.consumed == case.amount_in
    pools = [s.pool_id for s in plan.steps]
    assert len(pools) == len(set(pools))
    outputs = {s.output_fund_id for s in plan.steps}
    for step in plan.steps:
        for ref in step.inputs:
            assert ref.fund_id == REQUEST_FUND_ID or ref.fund_id in outputs  # no external fund
            assert (isinstance(ref.amount, int) and not isinstance(ref.amount, bool)) or (
                ref.amount == ALL_REMAINING
            )
    assert all(t.amount_in > 0 and t.amount_out > 0 for t in ev.trace)
    edges = {(s.token_in, s.token_out) for s in plan.steps}
    tokens = {t for e in edges for t in e}
    indeg = {t: sum(1 for e in edges if e[1] == t) for t in tokens}
    ready = [t for t in tokens if indeg[t] == 0]
    seen = 0
    while ready:
        t = ready.pop()
        seen += 1
        for a, b in edges:
            if a == t:
                indeg[b] -= 1
                if indeg[b] == 0:
                    ready.append(b)
    assert seen == len(tokens)  # acyclic plan token graph
    assert case.token_in not in {b for _, b in edges}
    assert case.token_out not in {a for a, _ in edges}
    return ev.gross_output


def _hand_ledger(bundle: SnapshotBundle, case: Case, flows: list[dict[str, Any]]) -> int:
    """An independent primal ledger of the recorded integer flows: every output is the hand
    `getAmountOut` of its input on the pool's ORIGINAL reserves; every intermediate token is
    exactly conserved; the request is exactly spent. Returns the gross into token_out."""
    balance: dict[str, int] = {case.token_in: case.amount_in}
    for f in flows:
        pool = bundle.pools[f["pool_id"]]
        assert isinstance(pool, ConstantProductPoolState)
        r_in, r_out = pool.reserves_for(f["token_in"])
        x, y = int(f["amount_in"]), int(f["amount_out"])
        assert y == get_amount_out(x, r_in, r_out, pool.fee_bps) > 0
        balance[f["token_in"]] = balance.get(f["token_in"], 0) - x
        balance[f["token_out"]] = balance.get(f["token_out"], 0) + y
    gross = balance.pop(case.token_out)
    assert all(v == 0 for v in balance.values()), balance  # full source, zero intermediates
    return gross


def _model_case(cid: str) -> tuple[SnapshotBundle, Case, dict[str, Any]]:
    doc: dict[str, Any] = next(c for c in MODEL["cases"] if c["id"] == cid)
    if doc["universe"] == "mantle_mixed_cpmm_h3":
        bundle = _mixed()
        return bundle, bundle.case(cid), doc
    bundle, case, _ = author_network(cid.removesuffix("-maxiter2"))
    return bundle, case, doc


# ------------------------------------------------------------------ registration


def test_registered_once_after_uni_sor_cycle_safe_and_appended_by_all() -> None:
    assert ALGORITHMS[NAME] is cd.FACTORY is FACTORY
    names = list(ALGORITHMS)
    assert names.count(NAME) == 1 and names[-6:] == [  # the 0.2.2 bounded identities follow
        NAME,
        "single_path_bounded",
        "incremental_graph_bounded",
        "metis_history_bounded",
        "split_polish",  # WHI-1623 (0.2.3)
        "marginal_activation",  # WHI-1624 (0.2.3), registered last
    ]
    assert names.index(uni_sor_cycle_safe.NAME) + 1 == names.index(NAME)
    assert NAME not in BASE_STRATEGIES and NAME not in OPTIMIZED_STRATEGIES
    assert profile_module.strategy_group(NAME) == "custom"
    assert R021_ADDITIONS == (
        "metis_history",
        "direct_split_certified",
        "incremental_graph_repair",
        uni_sor_cycle_safe.NAME,
        NAME,
    )
    assert len(set(R021_ADDITIONS)) == 5  # all five new identities exactly once
    assert FACTORY.options_validator is cd.validate_options  # module-level (picklable)
    assert FACTORY.search_params == ("max_hops",) and FACTORY.graph_params == ()
    caps = FACTORY.capabilities
    assert (caps.multi_hop, caps.split, caps.shared_pools, caps.protocols) == (
        True,
        True,
        True,
        ("constant_product", "concentrated"),  # WHI-1559: the ceiling over both stages
    )
    prov = dict(FACTORY.provenance or {})
    assert prov["author_code"]["git_revision"] == "5932e42e5077ffc7d8e02c3b3ad2e9ed1d441267"
    assert "MIT" in prov["author_code"]["license"] and "2302.04938v1" in prov["method"]
    assert prov["recovery"] == "cfmm_share_projection/1" and "WHI-1559" in prov["stage"]
    notice = (REPO / "routing" / "cfmm" / "NOTICE.md").read_text()
    assert "5932e42e5077ffc7d8e02c3b3ad2e9ed1d441267" in notice and "recovery.py" in notice
    for name in ("daily_gross.yaml", "daily.yaml", "full_gross.yaml", "full.yaml"):
        document, profile = derive(
            yaml.safe_load((REPO / "config" / name).read_text()),
            "all",
            source_path=f"config/{name}",
            source_sha256="x",
        )
        assert list(profile.algorithms)[-7:] == [
            uni_sor_cycle_safe.NAME, NAME, "single_path_bounded", "incremental_graph_bounded",
            "metis_history_bounded", "split_polish", "marginal_activation",
        ]  # fmt: skip
        assert len(profile.algorithms) == 19  # WHI-1599/1600, WHI-1632 append after it
        # WHI-1559: `all` writes out the CURRENT preset, cfmm_dual/2 (the CL stage)
        assert document["algorithm_options"][NAME] == preset_options(FACTORY) != PRESET
        assert profile.algorithm_options[NAME]["source"]["kind"] == "preset"
        assert profile.algorithm_options[NAME]["source"]["version"] == 2
        hops = profile.search["max_hops"]  # the profile's own shared value
        assert profile.algorithm_config(FACTORY).params == {"max_hops": hops}
    for mode in ("base", "optimized"):
        doc = yaml.safe_load((REPO / "config" / "daily_gross.yaml").read_text())
        assert NAME not in derive(doc, mode, source_path="s", source_sha256="x")[1].algorithms


def test_preset_is_the_pinned_cfmm_dual_1_and_tampering_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WHI-1559: cfmm_dual/1 is now the factory's historical pin (the current one is v2)
    (historical,) = FACTORY.historical_presets
    pin = dict(historical)
    assert pin == {
        "path": "config/cfmm_dual/preset_v1.yaml",
        "sha256": cd.PRESET_V1["sha256"],
        "key": "R021-P12-cfmm_dual",
        "version": 1,
    }
    assert preset_options(FACTORY, historical) == PRESET  # = the cfmm-dual.md `preset` block
    example = EXAMPLES["positives"]
    cfmm_example = next(p for p in example if p["id"] == "P-CFMM-EST")
    source = cfmm_example["record"]["certificate"]["source"]
    assert settings_sha256(PRESET) == source["effective_settings_sha256"]  # 63b62554…3ac
    assert CONTRACT["preset_version"] == "cfmm_dual/1"
    (tmp_path / "config" / NAME).mkdir(parents=True)
    changed = (REPO / pin["path"]).read_text().replace("max_iterations: 200", "max_iterations: 201")
    (tmp_path / pin["path"]).write_text(changed)
    monkeypatch.setattr(profile_module, "REPO_ROOT", tmp_path)
    with pytest.raises(ProfileError, match="differs from the pin"):
        preset_options(FACTORY, historical)


_BAD: list[dict[Any, Any]] = [
    {},
    {k: v for k, v in PRESET.items() if k != "fallback"},  # all required, no default
    {k: v for k, v in PRESET.items() if k != "min_split_share"},
    {**PRESET, "market_protocols": "concentrated"},
    {**PRESET, "market_protocols": "liquidity_book"},
    {**PRESET, "fallback": "path_split"},
    {**PRESET, "fallback": None},
    {**PRESET, "cycle_resolve": 1},  # an int is not a bool
    {**PRESET, "cycle_resolve": "true"},
    {**PRESET, "max_iterations": True},  # a bool is not an int
    {**PRESET, "max_iterations": 0},
    {**PRESET, "max_iterations": 1001},
    {**PRESET, "max_iterations": 200.0},
    {**PRESET, "max_function_evaluations": 3001},
    {**PRESET, "lbfgs_memory": 2},
    {**PRESET, "lbfgs_memory": 31},
    {**PRESET, "max_recovery_attempts": 0},
    {**PRESET, "max_recovery_attempts": 65},
    {**PRESET, "pgtol": 1e-2},
    {**PRESET, "pgtol": True},
    {**PRESET, "ftol": 0.0},
    {**PRESET, "residual_tolerance": 1e-13},
    {**PRESET, "residual_tolerance": math.nan},
    {**PRESET, "log_price_bound": math.inf},
    {**PRESET, "log_price_bound": 0.5},
    {**PRESET, "log_price_bound": 201},
    {**PRESET, "min_split_share": -1e-9},
    {**PRESET, "min_split_share": 0.2},
    {**PRESET, "min_split_share": "1e-6"},
    {**PRESET, "max_iterations": 10**400},
    {**PRESET, "tolerance": 1e-5},  # unknown
    {1: 2, **PRESET},  # non-string key
    *(
        {**PRESET, key: 1}
        for key in (
            "max_hops",
            "max_splits",
            "percent_step",
            "max_quotes",
            "max_candidates",
            "objective",
            "seed",
            "time_limit_seconds",
        )
    ),
]


@pytest.mark.parametrize("options", _BAD)
def test_invalid_options_are_refused_by_every_entry_point(options: dict[Any, Any]) -> None:
    """The shared `validated_options`, the factory's public `prepare` and profile loading
    refuse the same values before any worker starts; nothing is defaulted or coerced."""
    bundle = load_bundle(SYNTHETIC)
    with pytest.raises(OptionsError):
        validated_options(FACTORY, options)
    assert FACTORY.prepare is not None
    with pytest.raises(OptionsError):
        FACTORY.prepare(bundle, AlgorithmConfig(NAME, {"max_hops": 3}, options))
    doc = yaml.safe_load((PROFILES / "cpmm.yaml").read_text())
    doc["algorithm_options"] = {NAME: options}
    with pytest.raises(ProfileError):
        parse_profile(doc, "<test>")


def test_valid_options_the_cpmm_profile_and_an_immutable_prepare() -> None:
    edge = {
        **PRESET,
        "max_iterations": 1,
        "max_function_evaluations": 3000,
        "lbfgs_memory": 30,
        "pgtol": 1e-14,
        "ftol": 1e-3,
        "residual_tolerance": 1e-2,
        "log_price_bound": 1,
        "min_split_share": 0,
        "max_recovery_attempts": 64,
        "cycle_resolve": False,
        "fallback": "none",
    }
    normalized = validated_options(FACTORY, edge)
    assert normalized == {**edge, "log_price_bound": 1.0, "min_split_share": 0.0}
    assert isinstance(normalized["log_price_bound"], float)  # an int float option normalizes
    profile = load_profile(PROFILES / "cpmm.yaml")
    entry = profile.algorithm_options[NAME]
    assert (entry["options"], entry["source"]["kind"]) == (PRESET, "preset")
    assert entry["settings_sha256"] == settings_sha256(PRESET)
    assert list(profile.algorithms) == ["path_split", "incremental_graph", NAME]
    assert profile.objective.mode == "gross_only"
    assert profile.algorithm_config(FACTORY).params == {"max_hops": 3}
    assert profile.budget.max_candidates is None
    doc = yaml.safe_load((PROFILES / "cpmm.yaml").read_text())
    del doc["algorithm_options"]
    with pytest.raises(ProfileError, match=f"requires algorithm_options.{NAME}"):
        parse_profile(doc, "<test>")  # an explicit profile must declare its options
    bundle = load_bundle(SYNTHETIC)
    assert FACTORY.prepare is not None
    for bad in ({}, {"max_hops": 0}, {"max_hops": True}, {"max_hops": 2.0}):
        with pytest.raises(cd.CfmmDualConfigError, match="search.max_hops"):
            FACTORY.prepare(bundle, AlgorithmConfig(NAME, bad, PRESET))
    prepared = _prepare(bundle)
    assert dict(prepared.options) == PRESET and prepared.settings_sha256 == settings_sha256(PRESET)
    assert prepared.settings == co.SolverSettings.from_options(PRESET)
    with pytest.raises(TypeError):
        prepared.options["fallback"] = "none"  # type: ignore[index,unused-ignore]
    with pytest.raises(dataclasses.FrozenInstanceError):
        prepared.max_hops = 4  # type: ignore[misc]
    assert "import_seconds" not in prepared.backend  # observational: r021.stages only
    assert prepared.backend["versions"] == dict(co.PINNED_VERSIONS)
    with pytest.raises(TypeError, match="PreparedCfmm"):
        FACTORY.solve(bundle.cases[0], SolveContext(bundle, gross_only(), None), Budget())


def test_registry_import_is_free_and_prepare_pays_the_numeric_import() -> None:
    """A legacy worker importing the registry loads no NumPy/SciPy; the factory's `prepare`
    loads (and pins) them, so the one-off import is charged to the measured preparation."""
    code = """
import json, sys
from routing.algorithms.registry import get_algorithm
from routing.algorithms.base import AlgorithmConfig
from snapshot.bundle import load_bundle
import yaml
before = sorted(m for m in sys.modules if m.split('.')[0] in ('numpy', 'scipy'))
f = get_algorithm('cfmm_dual')
opts = yaml.safe_load(open('config/cfmm_dual/preset_v1.yaml'))['options']
p = f.prepare(load_bundle('tests/fixtures/synthetic'), AlgorithmConfig('cfmm_dual',
              {'max_hops': 3}, opts))
print(json.dumps({"before": before, "after": 'scipy.optimize' in sys.modules,
                  "seconds": p.import_seconds}))
"""
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"},
    )
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["before"] == [] and got["after"] is True and got["seconds"] > 0


# ------------------------------------------------------------------ G-C2/G-C3: recovered plans


@pytest.mark.parametrize("cid", [c["id"] for c in MODEL["cases"]])
def test_the_registered_factory_recovers_the_stored_model_plans(cid: str) -> None:
    """End to end through the factory: the numeric work equals the stored model budget and the
    recovered flows/gross equal the committed WHI-1557 fixture wherever the §4.1 market
    universe is the fixture's market set; every plan satisfies the §6 invariants under a fresh
    replay and a hand getAmountOut ledger. r-deadend: the fixture solved over all pools, the
    universe drops the dead end (never a market), and the same plan results."""
    bundle, case, doc = _model_case(cid)
    over: dict[str, Any] = {"max_iterations": 2} if cid.endswith("-maxiter2") else {}
    sink: list[RoutePlan] = []
    result = _solve(bundle, case, sink=sink, **over)
    stats, rec, stored = _cfmm(result), _r021(result), doc["recovery"]
    assert result.status is SolveStatus.OK and result.score == int(stored["gross"])
    assert _invariants(bundle, case, result.plan) == result.score
    flows = stats["recovery"]["flows"]
    assert _hand_ledger(bundle, case, flows) == result.score
    assert [(f["pool_id"], f["amount_in"], f["amount_out"]) for f in flows] == [
        (f["pool_id"], f["amount_in"], f["amount_out"]) for f in stored["flows"]
    ]
    assert stats["recovery"]["cycle_removed"] == stored["cycle_removed"]
    assert [[p["pool_id"], p["reason"]] for p in stats["recovery"]["pruned"]] == stored["pruned"]
    assert sink == [result.plan]  # published exactly once, after the replay
    assert stats["initial"]["termination"] == doc["solution"]["termination"]
    if tuple(doc["markets"]) == tuple(stats["markets"]):
        work = rec["work"]
        assert work["objective_evaluations"] == doc["budget"]["evaluations_used"]
        assert work["market_oracle_calls"] == doc["budget"]["oracle_calls"]
        cap = over.get("max_iterations", PRESET["max_iterations"])
        assert work["optimizer_iterations"] == cap - doc["budget"]["iterations_left"]
    else:
        assert cid == "r-deadend" and stats["markets"] == ["st"] and "xy" in doc["markets"]


def test_multi_hop_split_plan_on_real_states_beats_every_single_path() -> None:
    """The admitted Moe classic real states (mantle_mixed, three tokens): one fully funded
    plan that splits the input AND routes a hop, strictly above the best exact single path
    over the same markets; grid38 equals the raw brute-force integer optimum; r-triangle's
    converged recovery (137) stays below its brute-force optimum 138 (no optimality claim)."""
    bundle = _mixed()
    case = bundle.case("usdc_usdt_small")
    result = _solve(bundle, case)
    assert result.plan is not None and result.score is not None
    pairs = {(s.token_in, s.token_out) for s in result.plan.steps}
    assert len(result.plan.steps) == 3 and len(pairs) == 3
    assert len({s.token_in for s in result.plan.steps if s.token_in == case.token_in}) == 1
    assert sum(1 for s in result.plan.steps if s.token_in == case.token_in) == 2  # a split
    assert any(s.token_in not in (case.token_in,) for s in result.plan.steps)  # a hop
    markets = _cfmm(result)["markets"]
    sub = dataclasses.replace(bundle, pools={p: bundle.pools[p] for p in markets})
    single = [
        reference_evaluate(bundle, case, path_plan(case, p), gross_only()).gross_output
        for p in enumerate_paths(build_graph_index(sub), case.token_in, case.token_out, 3)
    ]
    assert result.score > max(single)
    b, c, _ = author_network("r-grid38")
    raw = max(
        (get_amount_out(x, 134, 190, 30) if x else 0)
        + (get_amount_out(38 - x, 76, 172, 30) if x < 38 else 0)
        for x in range(39)
    )
    assert _solve(b, c).score == raw == 59
    b, c, _ = author_network("r-triangle")
    best = 0
    for x in range(151):  # S->T direct, S->M, then M->T over two pools (hand formula)
        direct = get_amount_out(x, 1000, 1000, 30) if x else 0
        mid = get_amount_out(150 - x, 1000, 2100, 30) if 150 - x else 0
        for y in range(mid + 1):
            a = get_amount_out(y, 2000, 1000, 30) if y else 0
            z = get_amount_out(mid - y, 500, 260, 5) if mid - y else 0
            best = max(best, direct + a + z)
    assert _solve(b, c).score == 137 < best == 138


# ------------------------------------------------------------------ G-C4: cycles, caps, failures


def test_cycle_break_with_without_and_starved_resolve_share_one_budget() -> None:
    """r-cycle: the full cycle is broken by removing ab2 (the smallest nu_in*x, one
    combinations_rejected_cycle, two admission checks); with `cycle_resolve` the
    fixed-direction DAG is re-solved warm on the SAME budget (26 evaluations / 117 oracle
    calls in total, as the model reference); without it, or starved (13 = what the initial
    solve uses), the cycle-broken support is projected. The estimate is always the initial
    full-network value, never the restricted one."""
    bundle, case, doc = _model_case("r-cycle")
    resolved = _solve(bundle, case)
    stats, work = _cfmm(resolved), _r021(resolved)["work"]
    assert stats["resolve"] == "resolved" and stats["recovery"]["cycle_removed"] == ["ab2"]
    assert (work["combinations_rejected_cycle"], work["admission_checks"]) == (1, 2)
    (again,) = stats["restricted"]
    assert again["markets"] == ["sa", "ab1", "bt", "at"] and again["warm_started"]
    assert again["directions"] == ["S", "A", "B", "A"]
    total = stats["initial"]["evaluations"] + again["evaluations"]
    assert work["objective_evaluations"] == total == doc["budget"]["evaluations_used"] == 26
    assert (
        work["market_oracle_calls"]
        == 117
        == stats["initial"]["oracle_calls"] + again["oracle_calls"]
    )
    assert again["oracle_calls"] == again["evaluations"] * 4  # |M| of the restricted problem
    estimate = _r021(resolved)["certificate"]["estimate"]
    assert float(estimate["value"]) == stats["initial"]["value"] != again["value"]
    assert resolved.score == 511 < float(estimate["value"])  # the loop profit is not routable
    off = _solve(bundle, case, cycle_resolve=False)
    assert _cfmm(off)["resolve"] == "disabled" and _cfmm(off)["restricted"] == []
    assert _invariants(bundle, case, off.plan) == off.score
    starved_doc = MODEL["forced_caps"]["resolve_starved"]
    starved = _solve(bundle, case, max_function_evaluations=starved_doc["cap"])
    s_stats = _cfmm(starved)
    assert s_stats["resolve"] == "skipped" and s_stats["restricted"] == []
    assert _r021(starved)["work"]["objective_evaluations"] == starved_doc["cap"] == 13  # no refund
    assert [
        (f["pool_id"], f["amount_in"], f["amount_out"]) for f in s_stats["recovery"]["flows"]
    ] == [(f["pool_id"], f["amount_in"], f["amount_out"]) for f in starved_doc["recovery"]["flows"]]
    assert str(starved.score) == starved_doc["recovery"]["gross"] == "509"
    # the same budget for iterations: the initial solve takes 9 (as with the preset); a cap
    # of 9 leaves the re-solve nothing (skipped), 10 leaves it exactly one iteration
    initial_iterations = stats["initial"]["iterations"]
    assert initial_iterations == 9
    starved_it = _solve(bundle, case, max_iterations=initial_iterations)
    assert _cfmm(starved_it)["resolve"] == "skipped"
    assert _r021(starved_it)["work"]["optimizer_iterations"] == initial_iterations
    one_more = _solve(bundle, case, max_iterations=initial_iterations + 1)
    (short,) = _cfmm(one_more)["restricted"]
    assert (short["iterations"], short["termination"]) == (1, "iteration_cap")
    assert _r021(one_more)["work"]["optimizer_iterations"] == initial_iterations + 1
    # ... and for evaluations: one left for the re-solve is exactly one, the guard fires
    one_eval = _solve(bundle, case, max_function_evaluations=starved_doc["cap"] + 1)
    (tight,) = _cfmm(one_eval)["restricted"]
    assert (tight["evaluations"], tight["guard_fired"], tight["termination"]) == (
        1,
        True,
        "iteration_cap",
    )
    assert _r021(one_eval)["work"]["objective_evaluations"] == starved_doc["cap"] + 1
    for partial in (starved_it, one_more, one_eval):
        assert _invariants(bundle, case, partial.plan) == partial.score
        assert (
            float(_r021(partial)["certificate"]["estimate"]["value"])
            == _cfmm(partial)["initial"]["value"]
        )


def _dust_network(amount: int = 10) -> tuple[SnapshotBundle, Case]:
    """`big` (worse price, deep) then `dust` (better price, a 1-raw-unit output reserve):
    the continuous optimum trades both; the remainder leg into `dust` floors to 0 output."""
    network: tuple[SnapshotBundle, Case] = bundle_of(
        cp("big", "S", "T", 10**6, 5 * 10**5), cp("dust", "S", "T", 1, 1), amount=amount
    )
    return network


def test_dust_leg_is_pruned_and_retried_then_the_attempt_cap_and_fallbacks() -> None:
    bundle, case = _dust_network()
    ok = _solve(bundle, case)
    rec = _cfmm(ok)["recovery"]
    assert rec["initial_support"] == ["big", "dust"] and rec["attempts"] == 2
    assert rec["pruned"] == [
        {"pool_id": "dust", "reason": "insufficient_output_amount", "attempt": 1}
    ]
    assert _r021(ok)["work"]["recovery_attempts"] == 2
    assert ok.score == get_amount_out(10, 10**6, 5 * 10**5, 30)
    assert _invariants(bundle, case, ok.plan) == ok.score
    capped = _solve(bundle, case, max_recovery_attempts=1)
    stats, r = _cfmm(capped), _r021(capped)
    assert stats["recovery_failure"] == "attempts_exhausted" and stats["termination"] == (
        "recovery_failed"
    )
    assert capped.status is SolveStatus.OK and capped.score == ok.score  # the labeled fallback
    assert r["fallback"] == {"used": True, "source": "single_path", "reason": "attempts_exhausted"}
    assert r["certificate"]["bound_kind"] == "unknown" and r["certificate"]["estimate"] is None
    assert r["certificate"]["termination"] == "recovery_failed"
    assert r["work"]["paths_scored"] == stats["fallback"]["paths_evaluated"] == 2
    none = _solve(bundle, case, max_recovery_attempts=1, fallback="none")
    assert none.status is SolveStatus.MODEL_ERROR and none.plan is None
    assert "attempts_exhausted" in (none.error or "") and _r021(none)["certificate"] is None
    assert _r021(none)["fallback"]["used"] is False


def test_tiny_order_support_exhausted_fallback_no_route_or_model_error() -> None:
    bundle, case = bundle_of(cp("a", "S", "T", 1000, 1000), amount=1)
    result = _solve(bundle, case)
    stats = _cfmm(result)
    assert stats["recovery_failure"] == "support_exhausted"
    assert result.status is SolveStatus.NO_ROUTE and result.plan is None  # fallback's status
    assert stats["fallback"]["status"] == "no_route" and stats["termination"] == "recovery_failed"
    assert _r021(result)["fallback"]["used"] is True and _r021(result)["certificate"] is None
    assert "single_path fallback over the 1 CPMM market(s)" in (result.error or "")
    none = _solve(bundle, case, fallback="none")
    assert none.status is SolveStatus.MODEL_ERROR and "support_exhausted" in (none.error or "")
    # r-tiny (R021 R5): not_converged, still a recovered stepwise plan with no estimate
    b, c, _ = author_network("r-tiny")
    tiny = _solve(b, c)
    assert (tiny.status, tiny.score) == (SolveStatus.OK, 99)
    assert _cfmm(tiny)["initial"]["termination"] == "not_converged"
    assert _r021(tiny)["certificate"]["bound_kind"] == "unknown"
    assert _cfmm(tiny)["estimate_withheld"] == "initial_not_converged"


def test_numeric_failure_falls_back_to_exact_single_path_or_is_a_model_error() -> None:
    bundle, case = bundle_of(cp("a", "S", "T", 10**400, 10**400), amount=10)
    result = _solve(bundle, case)
    stats, rec = _cfmm(result), _r021(result)
    assert stats["initial"]["failure"] == "numeric_failure" and stats["recovery"] is None
    assert stats["recovery_failure"] == "numeric_failure"
    assert result.status is SolveStatus.OK and result.score == get_amount_out(
        10, 10**400, 10**400, 30
    )
    assert rec["fallback"]["reason"] == "numeric_failure"
    assert rec["certificate"]["bound_kind"] == "unknown"
    assert rec["work"]["objective_evaluations"] == 0 and rec["work"]["recovery_attempts"] == 0
    none = _solve(bundle, case, fallback="none")
    assert none.status is SolveStatus.MODEL_ERROR and "numeric_failure" in (none.error or "")


def test_resolve_failure_and_empty_support_run_the_labeled_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, case, _ = _model_case("r-cycle")
    real = co.resolve_restricted

    def failing(*args: Any, **kwargs: Any) -> Any:
        sol = real(*args, **kwargs)
        assert sol is not None
        return dataclasses.replace(sol, failure="numeric_failure", termination=None)

    monkeypatch.setattr(cd, "resolve_restricted", failing)
    result = _solve(bundle, case)
    assert _cfmm(result)["resolve"] == "failed" and _cfmm(result)["recovery_failure"] == (
        "resolve_failed"
    )
    assert _r021(result)["fallback"] == {
        "used": True,
        "source": "single_path",
        "reason": "resolve_failed",
    }
    assert result.status is SolveStatus.OK and _invariants(bundle, case, result.plan) > 0
    monkeypatch.undo()
    # the natural empty support: a 1-raw-unit order of the corpus fixture's dust case
    corpus = load_bundle(CORPUS)
    dust = corpus.case("bnd-cda86a-deadde-dust")
    got = _solve(corpus, dust)
    assert _cfmm(got)["recovery_failure"] == "empty_support"
    assert _cfmm(got)["recovery"]["support"] == [] and _cfmm(got)["recovery"]["attempts"] == 0
    assert got.status is SolveStatus.OK and _r021(got)["fallback"]["reason"] == "empty_support"
    assert _invariants(corpus, dust, got.plan) == got.score


def test_quote_budget_is_timeout_without_certificate_and_the_meter_is_the_ledger() -> None:
    bundle, case, _ = _model_case("r-triangle")
    sink: list[RoutePlan] = []
    with metered_quotes(None) as meter:
        result = _solve(bundle, case, Budget(max_quotes=1), sink=sink)
    stats, rec = _cfmm(result), _r021(result)
    assert result.status is SolveStatus.TIMEOUT and result.plan is None and sink == []
    assert stats["recovery_failure"] == "quote_budget" and stats["termination"] == "quote_budget"
    assert rec["certificate"] is None and rec["fallback"]["used"] is False  # no fallback
    assert rec["work"]["quotes_executed"] == meter.counted == 1
    assert "not evidence of no_route" in (result.error or "")
    # the fallback's Budget.max_candidates unit: fallback_paths_evaluated
    bundle2, case2 = _dust_network()
    capped = _solve(bundle2, case2, Budget(max_candidates=1), max_recovery_attempts=1)
    assert _r021(capped)["max_candidates_unit"] == "fallback_paths_evaluated"
    assert _r021(capped)["work"]["paths_scored"] == 1 and capped.candidates_truncated == 1
    assert _cfmm(capped)["fallback"]["truncated_by"] == "max_candidates"


# ------------------------------------------------------------------ recovery module (§6)


def _options(**over: Any) -> RecoveryOptions:
    base: dict[str, Any] = {
        "min_split_share": 1e-6,
        "max_recovery_attempts": 8,
        "cycle_resolve": True,
    }
    return RecoveryOptions(**{**base, **over})


def _recover(
    bundle: SnapshotBundle, case: Case, trades: list[Trade], nu: dict[str, float], **over: Any
) -> recovery_module.Recovery:
    resolve = over.pop("resolve", None)
    return recover(
        bundle,
        case,
        list(bundle.pools),
        trades,
        nu,
        _options(**over),
        QuoteCache(bundle),
        gross_only(),
        resolve,
    )


def test_recovery_projects_surplus_disconnected_dust_and_zero_output_exactly() -> None:
    # surplus: the continuous point leaves M in surplus (100 in, 60 out): all of M goes on
    bundle, case = bundle_of(
        cp("sm", "S", "M", 10**6, 10**6), cp("mt", "M", "T", 10**6, 10**6), amount=1000
    )
    rec = _recover(
        bundle,
        case,
        [Trade("sm", "S", "M", 1000.0, 100.0), Trade("mt", "M", "T", 60.0, 59.0)],
        {"S": 1.0, "M": 1.0},
    )
    assert rec.flows[1].amount_in == rec.flows[0].amount_out
    assert _invariants(bundle, case, rec.plan) == rec.gross
    # disconnected / dead end (never a path to T) and an edge out of T drop out
    bundle, case = bundle_of(
        cp("st", "S", "T", 10**6, 10**6),
        cp("sx", "S", "X", 10**6, 10**6),
        cp("xy", "X", "Y", 10**6, 10**6),
        cp("ty", "T", "Y", 10**6, 10**6),
        amount=1000,
    )
    rec = _recover(
        bundle,
        case,
        [
            Trade("st", "S", "T", 900.0, 890.0),
            Trade("sx", "S", "X", 100.0, 99.0),
            Trade("xy", "X", "Y", 99.0, 98.0),
            Trade("ty", "T", "Y", 5.0, 4.0),
        ],
        {"S": 1.0, "X": 1.0, "Y": 1.0},
    )
    assert rec.support == ("st",) and rec.gross == get_amount_out(1000, 10**6, 10**6, 30)
    # tiny order: the floored leg is not emitted, a zero-output leg is pruned and retried
    bundle, case = bundle_of(cp("a", "S", "T", 1000, 400), cp("b", "S", "T", 134, 190), amount=3)
    rec = _recover(
        bundle, case, [Trade("a", "S", "T", 2.0, 1.9), Trade("b", "S", "T", 1.0, 1.3)], {"S": 1.0}
    )
    assert rec.pruned == [("a", QuoteStatus.INSUFFICIENT_OUTPUT_AMOUNT.value, 1)]
    assert rec.attempts == 2 and rec.gross == get_amount_out(3, 134, 190, 30)
    # min_split_share: a market below the share of its token's outflow is not in the support
    rec = _recover(
        bundle,
        case,
        [Trade("a", "S", "T", 2.0, 1.9), Trade("b", "S", "T", 1.0, 1.3)],
        {"S": 1.0},
        min_split_share=0.4,
    )
    assert rec.initial_support == ("a",)
    # CL fee-only dust: ok with zero output -> `zero_output`, never a silent donation
    state = spec.synthetic_cl()
    cl_bundle, cl_case = bundle_of(
        state, cp("cp", "T0", "T1", 10**18, 10**18), amount=2, token_in="T0", token_out="T1"
    )
    rec = _recover(
        cl_bundle,
        cl_case,
        [Trade(state.pool_id, "T0", "T1", 1.0, 0.9), Trade("cp", "T0", "T1", 1.0, 0.9)],
        {"T0": 1.0},
    )
    assert rec.pruned == [(state.pool_id, "zero_output", 1)] and rec.gross == 1


def test_recovery_failure_codes_and_the_resolve_protocol() -> None:
    bundle, case = bundle_of(cp("a", "S", "T", 1000, 400), cp("b", "S", "T", 134, 190), amount=3)
    trades = [Trade("a", "S", "T", 2.0, 1.9), Trade("b", "S", "T", 1.0, 1.3)]
    assert _recover(bundle, case, trades, {"S": 1.0}, max_recovery_attempts=1).failure == (
        "attempts_exhausted"
    )
    assert _recover(bundle, case, [], {"S": 1.0}).failure == "empty_support"
    nan = _recover(bundle, case, [Trade("a", "S", "T", math.nan, 1.0)], {"S": 1.0})
    assert nan.failure == "numeric_failure"
    budget = _recover(bundle, case, trades, {"S": 1.0}, max_quotes=0)
    assert budget.failure == "quote_budget" and budget.quotes_executed == 0
    lone, lone_case = bundle_of(cp("a", "S", "T", 1000, 1000), amount=1)
    assert _recover(lone, lone_case, [Trade("a", "S", "T", 1.0, 0.9)], {"S": 1.0}).failure == (
        "support_exhausted"
    )
    # a cycle A -> B -> A beside the S -> T path: removal, then the resolve protocol
    net, net_case, _ = author_network("r-cycle")
    doc = next(c for c in MODEL["cases"] if c["id"] == "r-cycle")
    sol = doc["solution"]
    rows = [
        Trade(t["pool_id"], t["token_in"], t["token_out"], t["amount_in"], t["amount_out"])
        for t in sol["trades"]
    ]
    seen: list[Mapping[str, str]] = []

    def failed(allowed: Mapping[str, str]) -> None:
        seen.append(dict(allowed))
        return None

    def skipped(allowed: Mapping[str, str]) -> None:
        raise ResolveSkipped

    out = _recover(net, net_case, rows, sol["nu"], resolve=failed)
    assert out.failure == "resolve_failed" and out.resolve == "failed"
    assert list(seen[0]) == doc["resolves"][0]["markets"]  # admitted order, directions fixed
    assert _recover(net, net_case, rows, sol["nu"], resolve=lambda a: ()).failure == (
        "resolve_failed"
    )
    skip = _recover(net, net_case, rows, sol["nu"], resolve=skipped)
    assert skip.resolve == "skipped" and skip.plan is not None and skip.cycle_removed == ["ab2"]
    off = _recover(net, net_case, rows, sol["nu"], resolve=failed, cycle_resolve=False)
    assert off.resolve == "disabled" and off.plan is not None and len(seen) == 1  # not called


def _faulty_evaluate(fault: Callable[[Evaluation], Evaluation]) -> Callable[..., Evaluation]:
    def fake(*args: Any, **kwargs: Any) -> Evaluation:
        return fault(reference_evaluate(*args, **kwargs))

    return fake


@pytest.mark.parametrize(
    "fault",
    [
        lambda e: dataclasses.replace(e, gross_output=e.gross_output + 1),
        lambda e: dataclasses.replace(e, gross_output=e.gross_output - 1),
        lambda e: dataclasses.replace(e, status=EvalStatus.INVALID_PLAN),
        lambda e: dataclasses.replace(e, residuals={"HOP1": 1}),
    ],
    ids=["gross+1", "gross-1", "invalid", "residual"],
)
def test_a_replay_mismatch_is_an_algorithm_error_never_published_or_hidden(
    fault: Callable[[Evaluation], Evaluation], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recovery_module, "evaluate", _faulty_evaluate(fault))
    bundle, case, _ = _model_case("r-grid38")
    sink: list[RoutePlan] = []
    result = _solve(bundle, case, sink=sink)
    assert result.status is SolveStatus.ALGORITHM_ERROR and result.plan is None and sink == []
    assert _cfmm(result)["inconsistency"]["stage"] == "replay"
    assert _r021(result)["certificate"] is None and _r021(result)["fallback"]["used"] is False
    assert "disagrees" in (result.error or "")


# ------------------------------------------------------------------ G-C5/G-C6: records


def test_certificates_validate_and_are_estimate_only_when_the_initial_solve_converged() -> None:
    """Runtime validator against an independently built context: grid38 is exactly the
    committed P-CFMM-EST record (domain, hash, estimate, work); converged -> estimate,
    iteration_cap / not_converged / fallback -> unknown; never certified; null upper/gap."""
    cases: dict[str, tuple[dict[str, Any], str]] = {
        "r-grid38": ({}, "estimate"),
        "r-triangle-maxiter2": ({"max_iterations": 2}, "unknown"),
        "r-tiny": ({}, "unknown"),
    }
    for cid, (over, kind) in cases.items():
        bundle, case, doc = _model_case(cid)
        with metered_quotes(None) as meter:
            result = _solve(bundle, case, **over)
        rec = _r021(result)
        ctx = _context(bundle, case, result, meter.counted, {**PRESET, **over})
        assert check_diagnostics(rec, ctx) == set(), cid
        view = diagnostics_view(rec, ctx)
        assert view["state"] == kind and rec["certificate"]["bound_kind"] == kind
        cert = rec["certificate"]
        assert (cert["upper_raw"], cert["gap_raw"], cert["upper_source"]) == (None, None, None)
        assert cert["optimality_proven"] is False
        assert cert["termination"] == doc["solution"]["termination"]
        json.dumps(result.search_stats, allow_nan=False)  # finite, deterministic JSON
    bundle, case, _ = _model_case("r-grid38")
    with metered_quotes(None) as meter:
        result = _solve(bundle, case)
    rec = _r021(result)
    example = next(p for p in EXAMPLES["positives"] if p["id"] == "P-CFMM-EST")["record"]
    assert rec["domain"]["amount_grid"] == example["domain"]["amount_grid"]
    assert {k: v for k, v in rec["domain"].items() if k != "universe"} == {
        k: v for k, v in example["domain"].items() if k != "universe"
    }
    assert rec["certificate"]["estimate"] == example["certificate"]["estimate"]
    assert rec["certificate"]["lower_raw"] == example["certificate"]["lower_raw"] == "59"
    assert {k: rec["work"][k] for k in example["work"]} == example["work"]
    ctx = _context(bundle, case, result, meter.counted)

    def tamper(mutate: Callable[[dict[str, Any]], None]) -> set[str]:
        copy: dict[str, Any] = json.loads(json.dumps(rec))
        mutate(copy)
        return check_diagnostics(copy, ctx)

    assert "C_UNCERTIFIED_BOUND" in tamper(lambda r: r["certificate"].update(upper_raw="60"))
    assert "C_CERTIFIED_SOURCE" in tamper(lambda r: r["certificate"].update(bound_kind="certified"))
    assert "C_REQUEST" in tamper(lambda r: r["certificate"]["request"].update(amount_in="39"))
    assert "W_LEDGER" in tamper(lambda r: r["work"].update(quotes_executed=3))
    assert "C_LOWER_EVAL" in tamper(lambda r: r["certificate"].update(lower_raw="60"))
    killed = dataclasses.replace(ctx, hard_killed=True)
    assert "C_KILLED" in check_diagnostics(rec, killed)


def test_every_work_unit_is_independently_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Oracle calls, objective evaluations and in-solve replays are counted by wrappers that
    only count; quotes by the worker meter; iterations by SciPy's own nit. The fallback's
    replays and quotes are inside the same totals (one ledger)."""
    counts = {"arb": 0, "objective": 0, "evaluate": 0}
    real_arb, real_obj = cm.cpmm_arb, cm.log_objective

    def arb(*a: Any, **k: Any) -> Any:
        counts["arb"] += 1
        return real_arb(*a, **k)

    def obj(*a: Any, **k: Any) -> Any:
        counts["objective"] += 1
        return real_obj(*a, **k)

    def ev(*a: Any, **k: Any) -> Evaluation:
        counts["evaluate"] += 1
        return reference_evaluate(*a, **k)

    monkeypatch.setattr(cm, "cpmm_arb", arb)
    monkeypatch.setattr(co, "log_objective", obj)
    monkeypatch.setattr(recovery_module, "evaluate", ev)
    monkeypatch.setattr(single_path_module, "evaluate", ev)
    runs: list[tuple[SnapshotBundle, Case, dict[str, Any]]] = [
        (*_model_case("r-cycle")[:2], {}),
        (*_model_case("usdc_usdt_small")[:2], {}),
        (*_dust_network(), {"max_recovery_attempts": 1}),  # the fallback path
    ]
    for bundle, case, over in runs:
        counts.update(arb=0, objective=0, evaluate=0)
        with metered_quotes(None) as meter:
            result = _solve(bundle, case, **over)
        work, stats = _r021(result)["work"], _cfmm(result)
        assert work["market_oracle_calls"] == counts["arb"]
        assert work["objective_evaluations"] == work["gradient_evaluations"] == counts["objective"]
        assert work["internal_evaluations"] == counts["evaluate"] == result.candidates_considered
        assert work["quotes_executed"] == meter.counted
        assert work["exact_replay_quotes"] <= work["quotes_executed"]
        phases = [stats["initial"], *stats["restricted"]]
        assert work["optimizer_iterations"] == sum(p["iterations"] for p in phases)
        assert all(p["scipy_nit"] in (None, p["iterations"]) for p in phases)
        assert set(work) == set(cd.WORK_UNITS)
    assert set(_r021(result)["stages"]) >= {"prepare_numeric_import", "initial_solve", "fallback"}


def test_two_solves_case_order_and_a_shared_prepare_are_deterministic() -> None:
    bundle = _mixed()
    prepared = _prepare(bundle)
    before = (dict(prepared.options), dict(prepared.backend), prepared.index)
    cases = list(bundle.cases)

    def comparable(result: SolveResult) -> Any:
        stats = json.loads(json.dumps(result.search_stats))
        stats["r021"].pop("stages")
        return (result.status, result.plan, result.score, result.candidates_considered, stats)

    forward = [comparable(_solve(bundle, c, prepared=prepared)) for c in cases]
    backward = [comparable(_solve(bundle, c, prepared=prepared)) for c in reversed(cases)]
    assert forward == backward[::-1]
    assert forward == [comparable(_solve(bundle, c, prepared=prepared)) for c in cases]
    assert (dict(prepared.options), dict(prepared.backend), prepared.index) == before


# ------------------------------------------------------------------ hard limits (publication)


def test_a_hard_quote_kill_leaves_only_complete_published_plans() -> None:
    """The worker's hard meter at every executed quote: `QuoteLimitExceeded` escapes (no
    result, no certificate); publications happen only after the complete replay."""
    runs: list[tuple[SnapshotBundle, Case, dict[str, Any]]] = [
        (*_model_case("usdc_usdt_small")[:2], {}),
        (*_dust_network(), {"max_recovery_attempts": 1}),
    ]
    for bundle, case, over in runs:
        full: list[RoutePlan] = []
        result = _solve(bundle, case, sink=full, **over)
        needed = _r021(result)["work"]["quotes_executed"]
        assert needed >= 2 and full
        for kill_at in range(needed):
            sink: list[RoutePlan] = []
            with metered_quotes(kill_at), pytest.raises(QuoteLimitExceeded):
                _solve(bundle, case, sink=sink, **over)
            assert sink == full[: len(sink)]
            for plan in sink:
                assert _invariants(bundle, case, plan) > 0


def test_a_runner_wall_kill_keeps_no_certificate(tmp_path: Path) -> None:
    """Through the real runner and a spawned worker: a hard wall kill is `timeout` with the
    runner's `unavailable (hard_timeout)` view; nothing of a solver certificate survives."""
    from benchmark.results import load_case_records
    from benchmark.runner import run_experiment
    from snapshot.models import BlockRef

    pools = [cp(f"p{i}", "S", f"M{i}", 10**9 + i, 10**9) for i in range(6)]
    pools += [cp(f"q{i}", f"M{i}", "T", 10**9, 10**9 + 7 * i) for i in range(6)]
    pools += [cp(f"r{i}", f"M{i}", f"M{(i + 1) % 6}", 10**9, 10**9) for i in range(6)]
    bundle = write_bundle(
        tmp_path / "bundle",
        bundle_id="cfmm-heavy",
        kind="synthetic",
        block=BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0),
        pools=pools,
        cases=[Case("heavy", "S", "T", 10**8)],
    )
    doc = yaml.safe_load((PROFILES / "cpmm.yaml").read_text())
    doc["algorithms"] = [NAME]
    doc["budget"]["time_limit_seconds"] = 0.001
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


# ------------------------------------------------------------------ scope (visible rows)


def test_outside_domain_and_structural_cases_are_visible_distinct_rows() -> None:
    bundle = _mixed()
    case = bundle.case("usdc_usdt_small")
    net = _solve(bundle, case, objective=synthetic_fixed_cost(0))
    assert net.status is SolveStatus.UNSUPPORTED and net.plan is None
    assert _r021(net)["scope"] == {"supported": False, "reason": "objective"}
    assert _cfmm(net)["termination"] == "unsupported_scope"
    assert _r021(net)["work"]["objective_evaluations"] == 0  # refused before numeric work
    # LB/CL-only paths: unsupported protocol_ceiling (never a CPMM substitute)
    lb = load_bundle(LB)
    assert all(isinstance(p, LiquidityBookPoolState) for p in lb.pools.values())
    for c in lb.cases:
        row = _solve(lb, c)
        assert row.status is SolveStatus.UNSUPPORTED
        assert _r021(row)["scope"]["reason"] == "protocol_ceiling"
        assert _r021(row)["domain"]["universe"]["pools"] == []
        assert _r021(row)["domain"]["protocols"] == ["constant_product"]
    # no path at all: no_route (complete); beyond max_hops: no_route (hop cap)
    synthetic = load_bundle(SYNTHETIC)
    none = _solve(synthetic, synthetic.case("direct_no_route"))
    assert none.status is SolveStatus.NO_ROUTE and "unreachable" in (none.error or "")
    far, far_case = bundle_of(
        cp("a", "S", "M", 10**6, 10**6), cp("b", "M", "T", 10**6, 10**6), amount=100
    )
    hop = _solve(far, far_case, prepared=_prepare(far, max_hops=1))
    assert hop.status is SolveStatus.NO_ROUTE and "hop cap" in (hop.error or "")
    assert _r021(hop)["scope"]["supported"] is True
    # mixed real states: every CPMM-reachable case solved over CPMM markets only
    for c in bundle.cases:
        row = _solve(bundle, c)
        assert row.status is SolveStatus.OK
        assert all(
            isinstance(bundle.pools[s.pool_id], ConstantProductPoolState)
            for s in (row.plan.steps if row.plan else ())
        )
    corpus = load_bundle(CORPUS)
    statuses = {_solve(corpus, c).status for c in corpus.cases}
    assert SolveStatus.UNSUPPORTED in statuses and SolveStatus.OK in statuses
    assert statuses <= {SolveStatus.OK, SolveStatus.UNSUPPORTED, SolveStatus.NO_ROUTE}


# ------------------------------------------------------------------ CLI: run + quote --details


def _saved_run(out: str) -> Path:
    marker = "(run "
    return Path(out[out.index(marker) + len(marker) :].split(")", 1)[0])


def test_cli_run_replay_report_and_quote_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both CLI paths through the spawned worker and the real factory with the CPMM profile
    next to its matched controls: validated views, the options identity, literal
    `--strategies profile` replay with no deterministic difference, the offline report, and
    exactly one timed solve per quote with the effective settings."""
    import main
    from benchmark.results import load_case_records, load_manifest
    from benchmark.runner import compare_runs

    profile = PROFILES / "cpmm.yaml"
    for bundle_dir in (MIXED, CPMM_GRAPH):
        results = tmp_path / bundle_dir.name / "runs"
        argv = [
            "run",
            "--bundle",
            str(bundle_dir),
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
        assert list(manifest.algorithms) == ["path_split", "incremental_graph", NAME]
        entry = manifest.resolved_profile["algorithm_options"][NAME]
        assert entry["options"] == PRESET and entry["source"]["kind"] == "preset"
        own = [r for r in load_case_records(run_dir) if r["algorithm"] == NAME]
        assert len(own) == len(manifest.measurement["case_order"]) > 0
        for r in own:
            view = r["diagnostics"]
            assert view["codes"] == [] and view["origin"] == "solver", view
            assert r["status"] == "ok" and r["measurement"]["attempts_consistent"] is True
            assert view["state"] in ("estimate", "unknown")
            assert view["work"]["quotes_executed"] == view["checked_against"]["quotes_counted"]
            assert view["max_candidates_unit"] == "fallback_paths_evaluated"
            assert set(view["stages"]) >= {"prepare_numeric_import", "initial_solve"}
        replay = shlex.split(manifest.replay_command)
        assert replay[-2:] == ["--strategies", "profile"]
        assert main.main(replay[replay.index("main.py") + 1 :]) == 0
        replayed = next(p for p in results.iterdir() if p != run_dir)
        assert load_manifest(replayed).resolved_profile == manifest.resolved_profile
        assert compare_runs(run_dir, replayed) == []
        report = tmp_path / bundle_dir.name / "report"
        assert main.main(["report", str(run_dir), "--output", str(report)]) == 0
        html = (report / "report.html").read_text()
        assert NAME in html and "estimate" in html
        capsys.readouterr()
    quotes = tmp_path / "quotes"
    argv = [
        "quote",
        "--bundle",
        str(CORPUS),
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
    assert main.main(argv) == 0
    out = capsys.readouterr().out
    assert f"[{NAME}] " in out and "max_candidates unit: fallback_paths_evaluated" in out
    assert "market_oracle_calls" in out and "[numeric]" in out
    quote_run = _saved_run(out)
    records = load_case_records(quote_run)
    assert [r["algorithm"] for r in records] == ["path_split", "incremental_graph", NAME]
    for r in records:
        assert r["measurement"]["attempts_completed"] == 1
        assert len(r["measurement"]["solve_seconds"]) == 1
    assert records[-1]["diagnostics"]["codes"] == []
    saved = yaml.safe_load((quote_run.parent.parent / "profile.yaml").read_text())
    assert saved["algorithm_options"] == {NAME: PRESET}


def test_saved_pre_whi_1558_profiles_replay_literally() -> None:
    """A saved effective profile (8 to 13 strategies) never gains `cfmm_dual` under
    `--strategies profile`."""
    source = yaml.safe_load((REPO / "config" / "daily_gross.yaml").read_text())
    document, _ = derive(source, "all", source_path="s", source_sha256="x")
    later = [
        "uni_sor_cycle_safe",
        "incremental_graph_repair",
        "direct_split_certified",
        "metis_history",
        "metis_inspired",
    ]
    for k in range(len(later) + 1):
        drop = [
            NAME,
            "single_path_bounded",
            "incremental_graph_bounded",
            "metis_history_bounded",
            *later[:k],
        ]  # WHI-1599/1600 are added after this identity
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
        assert len(profile.algorithms) == 13 - k

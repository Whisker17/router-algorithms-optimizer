"""The named optimized strategies (WHI-1528): `uni_sor_adaptive` (L08 arm H3) and
`uni_sor_optimized` (L08 arm H4), thin adapters over the unchanged `uni_sor_fast`.

Checked against the registered recipes and the independent evaluator, not against the
adapters' own claims:

- separate identities: the six base factories and the configurable `uni_sor_fast` are the
  same objects as before; each strategy has its own name, recipe, capabilities and
  provenance, and the global `shortlist` / `sampling` sections never reach it;
- on frozen fixtures each strategy's result IS the registered recipe's -- `uni_sor_fast`
  with the arm's settings (H4 additionally under the L08 driver's own control
  installation) -- and every returned plan replays identically in the evaluator;
- `uni_sor_optimized` installs fresh L02-L04 controls for each solve only and restores the
  reference kernels after errors and hard limits too; `uni_sor_adaptive` installs none;
- `prepare` refuses incomplete or foreign settings (no defaults), and the factories cross
  the spawn boundary.
"""

from __future__ import annotations

import hashlib
import json
import pickle
from functools import cache
from pathlib import Path
from typing import Any

import pytest

import benchmark.latency as latency
from benchmark.objective import gross_only
from benchmark.profile import parse_profile, read_profile_document
from benchmark.strategies import derive
from pools import concentrated, exact_controls, liquidity_book
from pools.quote import QuoteLimitExceeded, metered_quotes
from routing.algorithms import direct, uni_sor_fast, uni_sor_port
from routing.algorithms import uni_sor_strategies as strategies
from routing.algorithms.base import AlgorithmConfig, Budget, SolveContext, SolveResult, SolveStatus
from routing.algorithms.registry import ALGORITHMS, BASE_STRATEGIES, OPTIMIZED_STRATEGIES
from routing.evaluator import EvalStatus, evaluate
from snapshot.bundle import load_bundle
from snapshot.models import Case, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
L08 = REPO / "config" / "latency" / "l08.yaml"
DAILY_GROSS = "config/daily_gross.yaml"
FIXTURES = (
    "tests/fixtures/corpus/bundle",
    "tests/fixtures/routing/mantle_mixed",
    "tests/fixtures/routing/cpmm_graph",
    "tests/fixtures/moe_lb/bundle",  # LB-only: SOR capability omission -> unsupported
    "tests/fixtures/synthetic",  # a typed no_route
)
ARMS = {strategies.ADAPTIVE: "H3", strategies.OPTIMIZED: "H4"}


@cache
def _load(rel: str) -> SnapshotBundle:
    return load_bundle(REPO / rel)


@cache
def _profile() -> Any:
    """The CLI's effective `--strategies all` profile of the unchanged daily_gross."""
    _, profile = derive(
        read_profile_document(REPO / DAILY_GROSS), "all", source_path=DAILY_GROSS,
        source_sha256="unused",
    )  # fmt: skip
    return profile


def _arm(name: str) -> latency.Arm:
    return latency.load_arms(L08).arms[ARMS[name]]


def _recipe_config(name: str) -> AlgorithmConfig:
    """What the L08 driver hands the arm's `uni_sor_fast` (`latency.arm_profile`)."""
    arm = _arm(name)
    profile = latency.arm_profile(arm, DAILY_GROSS)
    return profile.algorithm_config(ALGORITHMS[uni_sor_fast.NAME])


def _solve_strategy(name: str, bundle: SnapshotBundle, case: Case, budget: Budget) -> SolveResult:
    factory = ALGORITHMS[name]
    assert factory.prepare is not None
    prepared = factory.prepare(bundle, _profile().algorithm_config(factory))
    return factory.solve(case, SolveContext(bundle, gross_only(), prepared), budget)


def _solve_recipe(name: str, bundle: SnapshotBundle, case: Case, budget: Budget) -> SolveResult:
    arm = _arm(name)
    prepared = uni_sor_fast.prepare(bundle, _recipe_config(name))
    context = SolveContext(bundle, gross_only(), prepared)
    factory = latency.arm_factory(uni_sor_fast.FACTORY, arm)  # H4: the driver's installation
    return factory.solve(case, context, budget)


# ------------------------------------------------------------------ identity


def test_named_identities_leave_the_six_bases_and_uni_sor_fast_untouched() -> None:
    assert tuple(ALGORITHMS)[:6] == BASE_STRATEGIES
    assert OPTIMIZED_STRATEGIES == ("uni_sor_adaptive", "uni_sor_optimized")
    assert (
        ALGORITHMS["direct"] is direct.FACTORY
        and ALGORITHMS["uni_sor_port"] is uni_sor_port.FACTORY
    )
    assert ALGORITHMS["uni_sor_fast"] is uni_sor_fast.FACTORY
    assert uni_sor_fast.FACTORY.strategy_recipe is None
    for name in (*BASE_STRATEGIES, uni_sor_fast.NAME):
        assert ALGORITHMS[name].strategy_recipe is None
    for name, arm in ARMS.items():
        factory = ALGORITHMS[name]
        assert factory.name == name and factory.capabilities == uni_sor_port.CAPABILITIES
        assert factory.search_params == uni_sor_port.SEARCH_PARAMS
        # the global shortlist/sampling sections can never reach a named strategy
        assert factory.shortlist_params == () and factory.sampling_params == ()
        assert dict(factory.strategy_recipe or {}) == {
            "path": "config/latency/l08.yaml", "sha256": strategies.RECIPE_SHA256,
            "key": "L08", "version": 1, "arm": arm,
        }  # fmt: skip
        provenance = factory.provenance or {}
        assert provenance["experimental"] is True and provenance["strategy_group"] == "optimized"
        assert (
            provenance["reference"] == "uni_sor_port" and "not a default" in provenance["adoption"]
        )
        assert provenance["exact_controls"] == (["L02", "L03", "L04"] if arm == "H4" else [])
        json.dumps(dict(provenance))  # persisted verbatim in every resolved profile
        restored = pickle.loads(pickle.dumps(factory))  # crosses the spawn boundary
        assert restored.solve is factory.solve and restored.prepare is factory.prepare
    # the pinned recipe file is the unchanged L08 v1
    assert hashlib.sha256(L08.read_bytes()).hexdigest() == strategies.RECIPE_SHA256


def test_each_strategy_gets_only_its_own_recipe_settings() -> None:
    profile = _profile()
    adaptive = profile.algorithm_config(ALGORITHMS[strategies.ADAPTIVE]).params
    optimized = profile.algorithm_config(ALGORITHMS[strategies.OPTIMIZED]).params
    search = {"max_hops": 2, "max_splits": 4, "percent_step": 5}  # daily_gross's own
    assert dict(adaptive) == {
        **search, "probe_percents": (25, 50, 75, 100), "routes_per_probe": 1_000_000,
        "direct_routes": 0, "coarse_step": 25, "refine_radius": 1, "soft_max_quotes": None,
        "controls": {},
    }  # fmt: skip
    assert dict(optimized) == {
        **search, "probe_percents": (5, 100), "routes_per_probe": 8, "direct_routes": 0,
        "coarse_step": 25, "refine_radius": 1, "soft_max_quotes": None,
        "controls": {
            "L02": {"skip_empty_spans": True},
            "L03": {"tick_capacity": 16384, "bin_capacity": 4096, "lifetime": "per_solve"},
            "L04": {"max_keys": 4096, "max_checkpoints": 262144, "lifetime": "per_solve"},
        },
    }  # fmt: skip
    # identical to the L08 driver's own resolution of the arms (the registered recipes)
    for name in ARMS:
        recipe = dict(_recipe_config(name).params)
        own = dict(profile.algorithm_config(ALGORITHMS[name]).params)
        assert own.pop("controls") == dict(_arm(name).controls)
        assert own == recipe
    # a configured raw uni_sor_fast keeps its own global settings, independent of both
    raw = read_profile_document(REPO / DAILY_GROSS)
    raw["algorithms"] = [*raw["algorithms"], "uni_sor_fast", *OPTIMIZED_STRATEGIES]
    raw["shortlist"] = {"probe_percents": [10, 100], "routes_per_probe": 3, "direct_routes": 1}
    raw["strategies"] = _profile().resolved()["strategies"]
    mixed = parse_profile(raw, "mixed.yaml")
    assert mixed.algorithm_config(uni_sor_fast.FACTORY).params["routes_per_probe"] == 3
    assert mixed.algorithm_config(ALGORITHMS[strategies.OPTIMIZED]).params["routes_per_probe"] == 8
    assert mixed.algorithm_config(ALGORITHMS[strategies.ADAPTIVE]).params["probe_percents"] == (
        25, 50, 75, 100)  # fmt: skip


# ------------------------------------------------------------------ recipe equality


@pytest.mark.parametrize("name", list(ARMS))
@pytest.mark.parametrize("rel", FIXTURES)
def test_strategy_result_is_the_registered_recipe_and_replays_independently(
    name: str, rel: str
) -> None:
    bundle = _load(rel)
    cases = bundle.cases[:: max(1, len(bundle.cases) // 24)]
    statuses = set()
    for case in cases:
        got = _solve_strategy(name, bundle, case, Budget())
        ref = _solve_recipe(name, bundle, case, Budget())
        assert got.algorithm == name and ref.algorithm == uni_sor_fast.NAME
        assert (got.status, got.plan, got.evaluation, got.score, got.error) == (
            ref.status, ref.plan, ref.evaluation, ref.score, ref.error)  # fmt: skip
        assert (got.candidates_considered, got.candidates_truncated) == (
            ref.candidates_considered, ref.candidates_truncated)  # fmt: skip
        mine, theirs = dict(got.search_stats), dict(ref.search_stats)
        strategy = mine.pop(strategies.STRATEGY_STATS_KEY)
        theirs.pop(latency.CONTROL_STATS_KEY, None)
        assert strategy["recipe"]["arm"] == ARMS[name] and strategy["lifetime"] == "per_solve"
        mine["sor_fast"] = {k: v for k, v in mine["sor_fast"].items() if k != "quote_path"}
        theirs["sor_fast"] = {k: v for k, v in theirs["sor_fast"].items() if k != "quote_path"}
        assert mine == theirs
        statuses.add(got.status)
        if got.status is SolveStatus.OK:
            assert got.plan is not None
            replay = evaluate(bundle, case, got.plan, gross_only())
            assert replay.status is EvalStatus.OK and replay == got.evaluation
    if rel.endswith("moe_lb/bundle"):
        assert SolveStatus.UNSUPPORTED in statuses  # capability omission stays visible
    if rel.endswith("synthetic"):
        assert SolveStatus.NO_ROUTE in statuses


def test_quote_path_labels_are_honest() -> None:
    bundle = _load("tests/fixtures/routing/mantle_mixed")
    case = bundle.cases[0]
    adaptive = _solve_strategy(strategies.ADAPTIVE, bundle, case, Budget()).search_stats
    optimized = _solve_strategy(strategies.OPTIMIZED, bundle, case, Budget()).search_stats
    assert adaptive["sor_fast"]["quote_path"] == uni_sor_fast.QUOTE_PATH  # nothing installed
    assert adaptive[strategies.STRATEGY_STATS_KEY]["controls"] == []
    assert "tick_math" not in adaptive[strategies.STRATEGY_STATS_KEY]
    assert optimized["sor_fast"]["quote_path"].startswith("uni_sor_optimized: explicit exact")
    stats = optimized[strategies.STRATEGY_STATS_KEY]
    assert stats["controls"] == ["L02", "L03", "L04"]
    assert stats["tick_math"]["hits"] + stats["tick_math"]["misses"] > 0  # really installed


# ------------------------------------------------------------------ control lifetime


def test_controls_are_fresh_per_solve_and_restored_after_errors_and_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _load("tests/fixtures/routing/mantle_mixed")
    case = bundle.cases[0]
    reference = (concentrated.swap, liquidity_book.swap)
    seen: list[Any] = []
    original = uni_sor_fast.solve

    def probe(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
        seen.append((concentrated.swap, liquidity_book.swap))
        return original(case, context, budget)

    monkeypatch.setattr(uni_sor_fast, "solve", probe)
    for _ in range(2):
        _solve_strategy(strategies.OPTIMIZED, bundle, case, Budget())
        assert (concentrated.swap, liquidity_book.swap) == reference  # restored on return
    (cl1, lb1), (cl2, lb2) = seen
    assert cl1.func is reference[0] and cl1.keywords["skip_empty_spans"] is True
    assert (
        cl1.keywords["math_reuse"].capacity == 16384 and lb1.keywords["math_reuse"].capacity == 4096
    )
    assert cl1.keywords["math_reuse"] is not cl2.keywords["math_reuse"]  # per solve, never shared
    assert cl1.keywords["prefix_reuse"] is not cl2.keywords["prefix_reuse"]
    assert lb1.keywords["math_reuse"] is not lb2.keywords["math_reuse"]
    seen.clear()
    _solve_strategy(strategies.ADAPTIVE, bundle, case, Budget())
    assert seen == [reference]  # the adaptive recipe declares no control

    def boom(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
        assert concentrated.swap is not reference[0]
        raise RuntimeError("boom")

    monkeypatch.setattr(uni_sor_fast, "solve", boom)
    with pytest.raises(RuntimeError, match="boom"):
        _solve_strategy(strategies.OPTIMIZED, bundle, case, Budget())
    assert (concentrated.swap, liquidity_book.swap) == reference  # restored on error
    monkeypatch.setattr(uni_sor_fast, "solve", original)
    # the worker's hard quote meter raises through the installation: restored as well
    with pytest.raises(QuoteLimitExceeded), metered_quotes(1):
        _solve_strategy(strategies.OPTIMIZED, bundle, case, Budget())
    assert (concentrated.swap, liquidity_book.swap) == reference
    # the cooperative quote budget stays a typed timeout without a plan
    cut = _solve_strategy(strategies.OPTIMIZED, bundle, case, Budget(max_quotes=2))
    assert cut.status is SolveStatus.TIMEOUT and cut.plan is None
    assert (concentrated.swap, liquidity_book.swap) == reference
    # a solve never starts from replaced kernels
    monkeypatch.setattr(concentrated, "swap", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="already replaced"):
        _solve_strategy(strategies.OPTIMIZED, bundle, case, Budget())
    assert exact_controls.REFERENCE_CL_SWAP is reference[0]


# ------------------------------------------------------------------ prepare validation


def _params(name: str) -> dict[str, Any]:
    params: dict[str, Any] = json.loads(
        json.dumps(_profile().resolved()["algorithm_config"][name]["params"])
    )
    return params


@pytest.mark.parametrize(
    ("name", "mutate", "match"),
    [
        (strategies.ADAPTIVE, lambda p: p.pop("coarse_step"), "missing recipe settings"),
        (strategies.ADAPTIVE, lambda p: p.pop("controls"), "missing recipe settings"),
        (strategies.ADAPTIVE, lambda p: p.update(controls={"L02": {"skip_empty_spans": True}}),
         "must be exactly"),
        (strategies.OPTIMIZED, lambda p: p["controls"].pop("L04"), "must be exactly"),
        (strategies.OPTIMIZED, lambda p: p["controls"].update(L05={"graph_reuse": True}),
         "must be exactly"),
        (strategies.OPTIMIZED, lambda p: p["controls"]["L03"].update(tick_capacity=0), "invalid"),
        (strategies.OPTIMIZED, lambda p: p["controls"]["L03"].update(lifetime="per_worker"),
         "invalid"),
        (strategies.OPTIMIZED, lambda p: p.update(percent_step=10), "probe_percents"),
    ],
)  # fmt: skip
def test_prepare_refuses_incomplete_or_foreign_settings(name: str, mutate: Any, match: str) -> None:
    params = _params(name)
    mutate(params)
    factory = ALGORITHMS[name]
    assert factory.prepare is not None
    with pytest.raises(strategies.UniSorStrategyError, match=match):
        factory.prepare(_load("tests/fixtures/routing/mantle_mixed"), AlgorithmConfig(name, params))


def test_solve_needs_its_own_prepared_state() -> None:
    bundle = _load("tests/fixtures/routing/mantle_mixed")
    prepared = uni_sor_fast.prepare(bundle, _recipe_config(strategies.ADAPTIVE))
    with pytest.raises(TypeError, match="PreparedStrategy"):
        strategies.solve_adaptive(bundle.cases[0], SolveContext(bundle, gross_only(), prepared),
                                  Budget())  # fmt: skip

"""WHI-1626: `split_polish` (E1, R023-C/1 §3, §4, §6) on the M4 (`metis_inspired`), S4
(`metis_history`) and REP (`incremental_graph_repair`) bases of contract §8 Q1.

The base must run exactly as its own registered identity under the same profile: its `prepare`
gets the `AlgorithmConfig` its own run gets (the loader forwards the base's `graph.*` keys through
`AlgorithmFactory.graph_params_for`; `base_options` are the base's own validated options), and on
every case the recorded base outcome equals the base's own run (plan, evaluation, score, status,
quotes, search counters). The E1 gates of `test_split_polish.py` run here once per new base. The
tuning-split equality is behind `ROUTER_TUNING_BUNDLE` (`data/` is primary-clone only: a skip is
not evidence) and shards the cases over forked workers."""

from __future__ import annotations

import dataclasses
import json
import multiprocessing
import os
import re
import time
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
import yaml

import main
from benchmark.objective import gross_only, synthetic_fixed_cost
from benchmark.profile import (
    ProfileError,
    RunProfile,
    parse_profile,
    preset_options,
    read_profile_document,
)
from benchmark.results import load_case_records, load_manifest
from benchmark.runner import compare_runs
from pools.quote import metered_quotes
from routing.algorithms import marginal_activation as ma
from routing.algorithms import split_polish as sp
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    OptionsError,
    SolveContext,
    SolveResult,
    SolveStatus,
    validated_options,
)
from routing.algorithms.registry import ALGORITHMS
from routing.evaluator import EvalStatus, evaluate
from routing.plan import FundInput, RoutePlan, SwapStep
from snapshot.bundle import load_bundle
from snapshot.models import BlockRef, Case, SnapshotBundle
from snapshot.models import ConcentratedPoolState as CL
from snapshot.models import ConstantProductPoolState as CP

REPO = Path(__file__).resolve().parents[2]
NAME = sp.NAME
O = gross_only()  # noqa: E741
TUNING_ENV = "ROUTER_TUNING_BUNDLE"
MIXED = REPO / "tests" / "fixtures" / "routing" / "mantle_mixed"
TRACKED_CORPUS = REPO / "tests" / "fixtures" / "corpus" / "bundle"
NEW_BASES = ("metis_inspired", "metis_history", "incremental_graph_repair")  # M4, S4, REP
POLISH = {"solver": "brent", "rounds": 2, "tolerance": 0.0001, "grid": 10**9, "maxiter": 60}
# The registered M4 arm's graph section (`label_hops` 4, `label_pruning` true; S4 reads only
# `label_hops`), on top of `config/full_gross.yaml` -- the campaign's profile shape.
M4_GRAPH = read_profile_document(REPO / "config" / "metis_challenge" / "m4.yaml")["graph"]
# A non-preset variant of each options-taking base: its registered disabled-mechanism control.
VARIANT = {
    "metis_history": read_profile_document(REPO / "config" / "metis_history" / "history_off.yaml")[
        "algorithm_options"
    ]["metis_history"],
    "incremental_graph_repair": read_profile_document(
        REPO / "config" / "incremental_graph_repair" / "repair_off.yaml"
    )["algorithm_options"]["incremental_graph_repair"],
}


def options_for(base: str, base_options: dict[str, Any] | None = None) -> dict[str, Any]:
    """`algorithm_options.split_polish` for `base`: the E1 nominee settings plus, for a base that
    takes options, `base_options` (default: the base's pinned preset, written out)."""
    options: dict[str, Any] = {**POLISH, "base": base}
    factory = ALGORITHMS[base]
    if factory.options_validator is not None:
        options["base_options"] = dict(
            preset_options(factory) if base_options is None else base_options
        )
    return options


def document(
    base: str,
    *,
    source: str = "full_gross.yaml",
    graph: dict[str, Any] | None = None,
    base_options: dict[str, Any] | None = None,
    listed: bool = True,
) -> dict[str, Any]:
    """A profile document running `base` (if `listed`) and `split_polish(base)` side by side,
    each base-options value written twice (the base's own entry and `base_options`)."""
    doc = read_profile_document(REPO / "config" / source)
    doc["algorithms"] = [base, NAME] if listed else [NAME]
    doc["graph"] = dict(M4_GRAPH if graph is None else graph)
    options = options_for(base, base_options)
    own = {base: dict(options["base_options"])} if listed and "base_options" in options else {}
    doc["algorithm_options"] = {**own, NAME: options}
    return doc


def profile_for(base: str, **kwargs: Any) -> RunProfile:
    return parse_profile(document(base, **kwargs), "p")


def bundle(pools: list[Any], case: Case) -> SnapshotBundle:
    return SnapshotBundle(
        "fx", "synthetic", 1, BlockRef(1, 1, "h", 1), {p.pool_id: p for p in pools}, (case,), "h",
        "fx",
    )  # fmt: skip


def step(pid: str, tin: str, tout: str, fid: str, amount: Any, out: str) -> SwapStep:
    return SwapStep(pid, tin, tout, (FundInput(fid, amount),), out)


def cl_pool(pid: str, token0: str, token1: str) -> CL:
    return CL(
        pool_id=pid, source_key="uniswap_v3", token0=token0, token1=token1, fee=3000,
        tick_spacing=60, sqrt_price_x96=1 << 96, tick=0, liquidity=10**9, fee_protocol=0,
        fee_growth_global0_x128=0, fee_growth_global1_x128=0, protocol_fees0=0, protocol_fees1=0,
        bitmap_word_range=(-1, 1),
    )  # fmt: skip


# `test_split_polish.py`'s solve fixture: two parallel S -> T CPMMs (a split the polish improves)
CS = Case("ig", "S", "T", 10**6)
BS = bundle([CP("p", "S", "T", 10**6, 3 * 10**6, 30), CP("q", "S", "T", 10**6, 2 * 10**6, 30),
             CP("x", "S", "X", 10**6, 10**6, 30)], CS)  # fmt: skip
SMALL_GRAPH = {"chunks": 4, "label_hops": 2, "label_pruning": True}


def small_profile(base: str, **kwargs: Any) -> RunProfile:
    doc = document(base, source="daily_gross.yaml", graph=SMALL_GRAPH, **kwargs)
    doc["search"] = {"max_hops": 2, "max_splits": 2, "percent_step": 25}
    return parse_profile(doc, "p")


def prepared_pair(base: str, b: SnapshotBundle, profile: RunProfile) -> tuple[Any, Any]:
    """The base's own prepared state and `split_polish(base)`'s, both from `profile`."""
    factory = ALGORITHMS[base]
    assert factory.prepare is not None
    own = factory.prepare(b, profile.algorithm_config(factory))
    return own, sp.prepare(b, profile.algorithm_config(sp.FACTORY))


def solve(
    prep: sp.PreparedSplitPolish,
    case: Case = CS,
    b: SnapshotBundle = BS,
    budget: Budget | None = None,
    objective: Any = O,
) -> tuple[SolveResult, int]:
    context = SolveContext(b, objective, prep)
    with metered_quotes(None if budget is None else budget.max_quotes) as meter:
        result = sp.solve(case, context, budget or Budget())
    return result, meter.counted


def own_solve(base: str, prep: Any, case: Case = CS, b: SnapshotBundle = BS) -> tuple[Any, int]:
    with metered_quotes(None) as meter:
        result = ALGORITHMS[base].solve(case, SolveContext(b, O, prep), Budget())
    return result, meter.counted


# ======================================================================================
# options, profile keys and the declared surface
# ======================================================================================


def test_base_options_are_required_exactly_when_the_base_takes_options() -> None:
    factory = sp.FACTORY
    for base in ("incremental_graph", "path_split", "metis_inspired"):
        assert validated_options(factory, options_for(base)) == options_for(base)
        for extra in ({}, {"repair": True}):
            with pytest.raises(OptionsError, match="accepts no algorithm_options"):
                validated_options(factory, {**options_for(base), "base_options": extra})
    for base in ("metis_history", "incremental_graph_repair"):
        preset = preset_options(ALGORITHMS[base])
        assert validated_options(factory, options_for(base)) == {**options_for(base)}
        assert validated_options(factory, options_for(base))["base_options"] == preset
        assert validated_options(factory, options_for(base, VARIANT[base]))["base_options"] == {
            **VARIANT[base]
        }
        missing = {k: v for k, v in options_for(base).items() if k != "base_options"}
        with pytest.raises(OptionsError, match="requires its own algorithm_options"):
            validated_options(factory, missing)
        first = sorted(preset)[0]
        for bad in (
            {},
            "preset",
            {k: v for k, v in preset.items() if k != first},
            {**preset, "extra": 1},
            {**preset, "chunks": 50},  # a reserved shared setting, refused by the base's check
            {**preset, "label_hops": 4},
        ):
            with pytest.raises(OptionsError, match="base_options"):
                validated_options(factory, {**options_for(base), "base_options": bad})
    for base in ("single_path", NAME, ma.NAME, "metis_history_bounded", "uni_sor_port"):
        with pytest.raises(OptionsError):
            validated_options(factory, {**POLISH, "base": base})


def test_e2_still_accepts_only_the_e1_bases() -> None:
    """`marginal_activation` reuses `validate_options` with its default `E1_BASES`."""
    e2 = {**POLISH, "base": "incremental_graph", "mode": "pf", "activations": 2, "top_k": 3,
          "delta_share": 0.0001, "seed_share": 0.0001, "arm": "treatment"}  # fmt: skip
    assert sp.E1_BASES == ("incremental_graph", "path_split")
    for base in sp.E1_BASES:
        validated_options(ma.FACTORY, {**e2, "base": base})
    for base in NEW_BASES:
        with pytest.raises(OptionsError):
            validated_options(ma.FACTORY, {**e2, "base": base})
    with pytest.raises(OptionsError):
        validated_options(ma.FACTORY, {**e2, "base_options": {}})
    assert ma.FACTORY.graph_params == ("chunks",) and ma.FACTORY.graph_params_for is None


def test_the_declared_surface_is_exact_for_every_new_base() -> None:
    """`CAPABILITIES`/`SEARCH_PARAMS` (what profile validation and the diagnostics view read)
    equal each new base's own; `path_split` keeps WHI-1623's ceiling, which covers it. The graph
    keys are per base through the hook; no other factory sets the hook."""
    ceiling = sp.CAPABILITIES
    assert list(sp.BASES) == ["incremental_graph", "path_split", *NEW_BASES]
    for name, factory in sp.BASES.items():
        assert factory is ALGORITHMS[name]
        caps = factory.capabilities
        assert caps.protocols is None and ceiling.protocols is None
        assert (caps.multi_hop, caps.split, caps.shared_pools) <= (True, True, True)
        assert not caps.shared_pools or ceiling.shared_pools
        assert set(factory.search_params) <= set(sp.SEARCH_PARAMS)
        keys = sp.graph_params_for({"base": name})
        assert keys[: len(sp.GRAPH_PARAMS)] == sp.GRAPH_PARAMS
        assert set(keys) == set(sp.GRAPH_PARAMS) | set(factory.graph_params)
    for name in NEW_BASES:
        factory = ALGORITHMS[name]
        assert factory.capabilities == ceiling and factory.search_params == sp.SEARCH_PARAMS
        assert set(sp.graph_params_for({"base": name})) == set(factory.graph_params)
    assert sp.FACTORY.graph_params == ("chunks",)  # WHI-1623's static key, unchanged
    assert [n for n, f in ALGORITHMS.items() if f.graph_params_for is not None] == [NAME]


@pytest.mark.parametrize(
    ("base", "required"),
    [
        ("metis_inspired", ("label_hops", "label_pruning")),
        ("metis_history", ("label_hops",)),
        ("incremental_graph_repair", ()),
        ("incremental_graph", ()),
        ("path_split", ()),
    ],
)
def test_the_loader_requires_and_forwards_exactly_the_base_graph_keys(
    base: str, required: tuple[str, ...]
) -> None:
    """Only `split_polish` is listed, so every label-key requirement comes from the hook: a
    loader that ignores it accepts these profiles and forwards no label key."""
    for key in ("label_hops", "label_pruning"):
        graph = {k: v for k, v in M4_GRAPH.items() if k != key}
        doc = document(base, graph=graph, listed=False)
        if key in required:
            with pytest.raises(ProfileError, match=rf"'split_polish' .*requires graph\.{key}"):
                parse_profile(doc, "p")
        else:
            parse_profile(doc, "p")
    with pytest.raises(ProfileError, match=r"requires graph\.chunks"):  # WHI-1623's, any base
        parse_profile(document(base, graph={"label_hops": 4, "label_pruning": True}), "p")
    profile = profile_for(base, listed=False)
    params = profile.algorithm_config(sp.FACTORY).params
    assert params == {"max_hops": 3, "max_splits": 4, "percent_step": 5, "chunks": 50,
                      **{k: M4_GRAPH[k] for k in required}}  # fmt: skip
    resolved = profile.resolved()["algorithm_config"][NAME]
    assert resolved["params"] == params and resolved["capabilities"] == sp.CAPABILITIES.to_dict()


@pytest.mark.parametrize("base", NEW_BASES)
def test_the_base_prepare_gets_exactly_its_own_registered_config(
    base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under one profile (non-default graph values, a non-preset `base_options` where the base
    takes options), `split_polish(base)` calls the base's `prepare` with exactly the
    `AlgorithmConfig` the base's own registered run gets. Forwarding other options (e.g. the
    preset, or none) or other graph keys (e.g. `incremental_graph`'s `chunks` only) fails here."""
    graph = {"chunks": 7, "label_hops": 3, "label_pruning": False}
    profile = parse_profile(
        document(base, source="daily_gross.yaml", graph=graph, base_options=VARIANT.get(base)),
        "p",
    )
    factory = ALGORITHMS[base]
    own = profile.algorithm_config(factory)
    seen: list[AlgorithmConfig] = []
    real = factory.prepare
    assert real is not None

    def spy(bundle: SnapshotBundle, config: AlgorithmConfig) -> Any:
        seen.append(config)
        return real(bundle, config)

    spied = dataclasses.replace(factory, prepare=spy)
    monkeypatch.setattr(sp, "BASES", MappingProxyType({**sp.BASES, base: spied}))
    sp.prepare(BS, profile.algorithm_config(sp.FACTORY))
    (got,) = seen
    assert (got.name, dict(got.params), dict(got.options)) == (
        own.name, dict(own.params), dict(own.options)
    )  # fmt: skip
    assert set(own.params) == set(factory.search_params) | set(factory.graph_params)
    assert {k: own.params[k] for k in factory.graph_params} == {
        k: graph[k] for k in factory.graph_params
    }
    if base in VARIANT:
        assert dict(own.options) == VARIANT[base] != preset_options(factory)


def test_the_hook_changes_nothing_for_a_shipped_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every shipped profile resolves identically with `split_polish`'s hook removed (it is unset
    for every other factory), and so do the WHI-1623/1624 profile shapes on their bases."""
    shipped = sorted((REPO / "config").rglob("*.yaml"))
    docs = []
    for path in shipped:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "algorithms" in raw and raw.get("schema_version") == 2:
            if (raw.get("algorithm_options") or {}).get(NAME, {}).get("base") in NEW_BASES:
                continue  # WHI-1627 campaign profiles on M4/S4/REP: they need the hook by design
            docs.append((str(path), raw))
    for base in sp.E1_BASES:
        docs.append((f"e1-{base}", document(base)))
    e2 = document("incremental_graph")
    e2["algorithms"] = ["incremental_graph", ma.NAME]
    e2["algorithm_options"] = {ma.NAME: {**options_for("incremental_graph"), "mode": "pf",
                                         "activations": 2, "top_k": 3, "delta_share": 0.0001,
                                         "seed_share": 0.0001, "arm": "treatment"}}  # fmt: skip
    docs.append(("e2", e2))
    assert len(docs) > 40

    def resolve_all() -> list[str]:
        out = []
        for path, raw in docs:
            try:
                out.append(json.dumps(parse_profile(raw, path).resolved(), sort_keys=True))
            except ProfileError as exc:  # e.g. a sweep template: refused the same either way
                out.append(f"refused: {exc}")
        return out

    with_hook = resolve_all()
    unhooked = dataclasses.replace(sp.FACTORY, graph_params_for=None)
    monkeypatch.setitem(ALGORITHMS, NAME, unhooked)
    assert resolve_all() == with_hook
    assert sum(not r.startswith("refused") for r in with_hook) > 40


# ======================================================================================
# E1 gates per new base (contract §9), on the solve fixture
# ======================================================================================


@pytest.mark.parametrize("base", NEW_BASES)
def test_solve_polishes_the_base_and_the_ledger_equals_the_seam(base: str) -> None:
    own_prep, prep = prepared_pair(base, BS, small_profile(base))
    own, own_quotes = own_solve(base, own_prep)
    assert own.status is SolveStatus.OK and own.score is not None and len(own.plan.steps) == 2
    result, seam = solve(prep)
    record, base_row = result.search_stats[NAME], result.search_stats["base"]
    assert base_row == {"algorithm": base, "status": "ok", "quotes": own_quotes,
                        "search": dict(own.search_stats)}  # fmt: skip
    assert record["base_gross"] == str(own.score) and record["scope"] == "fixed_funding_topology"
    assert seam == own_quotes + record["quotes"] and record["truncated_by"] is None
    assert result.status is SolveStatus.OK and result.plan is not None and result.score is not None
    rep = evaluate(BS, CS, result.plan, O)
    assert rep.status is EvalStatus.OK and rep.gross_output == result.score > own.score
    assert record["gross"] == str(result.score) and record["work"]["accepted"] >= 1
    if base in VARIANT:
        assert record["base_options"] == preset_options(ALGORITHMS[base])
    json.dumps(result.to_dict())


@pytest.mark.parametrize("base", NEW_BASES)
def test_budget_zero_returns_the_base_result_literally(base: str) -> None:
    own_prep, prep = prepared_pair(base, BS, small_profile(base))
    own, own_quotes = own_solve(base, own_prep)
    result, seam = solve(prep, budget=Budget(max_quotes=own_quotes))
    record = result.search_stats[NAME]
    assert record["truncated_by"] == "max_quotes" and record["quotes"] == 0
    assert seam == own_quotes
    assert (result.status, result.plan, result.evaluation, result.score) == (
        own.status, own.plan, own.evaluation, own.score
    )  # fmt: skip


@pytest.mark.parametrize("base", NEW_BASES)
def test_truncation_is_never_no_route_at_any_cap(base: str) -> None:
    own_prep, prep = prepared_pair(base, BS, small_profile(base))
    _, own_quotes = own_solve(base, own_prep)
    full, full_seam = solve(prep)
    grosses = []
    for cap in range(own_quotes, full_seam + 2):
        result, seam = solve(prep, budget=Budget(max_quotes=cap))
        record = result.search_stats[NAME]
        assert result.status is SolveStatus.OK and result.plan is not None and seam <= cap
        assert record["truncated_by"] == (None if cap >= full_seam else "max_quotes")
        rep = evaluate(BS, CS, result.plan, O)
        assert rep.status is EvalStatus.OK and str(rep.gross_output) == record["gross"]
        grosses.append(rep.gross_output)
    assert grosses == sorted(grosses) and grosses[-1] == full.score
    result, _ = solve(prep, budget=Budget(time_limit_seconds=1e-9))
    assert result.status is SolveStatus.OK and result.search_stats[NAME]["truncated_by"] == "time"


@pytest.mark.parametrize("base", NEW_BASES)
def test_rerun_and_fresh_prepare_are_deterministic(base: str) -> None:
    profile = small_profile(base)
    _, prep = prepared_pair(base, BS, profile)
    first, seam = solve(prep)
    again, seam_again = solve(prep)
    _, fresh = prepared_pair(base, BS, profile)
    third, seam_third = solve(fresh)
    assert first.to_dict() == again.to_dict() == third.to_dict()
    assert seam == seam_again == seam_third


@pytest.mark.parametrize("base", NEW_BASES)
def test_base_statuses_and_a_non_gross_objective(base: str) -> None:
    own_prep, prep = prepared_pair(base, BS, small_profile(base))
    unreachable = Case("none", "S", "Z", 10**6)
    result, _ = solve(prep, unreachable)
    own, _ = own_solve(base, own_prep, unreachable)
    assert result.status is own.status is SolveStatus.NO_ROUTE
    assert result.error == own.error and result.search_stats[NAME]["scope"] is None
    result, seam = solve(prep, objective=synthetic_fixed_cost(1))
    assert result.status is SolveStatus.UNSUPPORTED and seam == 0
    assert result.search_stats[NAME]["scope"] == "objective" and "base" not in result.search_stats


# F13's evaluator-valid plan with a real CL zero-output consumer of the target fund: produced 90,
# consumed 1 -> true gross 89 (a recomputed "sum of target outputs" would say 90).
C13 = Case("partial-target", "S", "T", 100)
B13 = bundle([CP("st", "S", "T", 1000, 1000, 30), cl_pool("tx", "T", "X")], C13)
P13 = RoutePlan(
    (step("st", "S", "T", "REQUEST", 100, "out"), step("tx", "T", "X", "out", 1, "dead"))
)


@pytest.mark.parametrize("evaluated", [True, False], ids=["evaluated", "replayed"])
@pytest.mark.parametrize("base", NEW_BASES)
def test_a_refused_base_plan_returns_the_base_with_its_true_gross(
    base: str, evaluated: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tuning split has no plan outside E1's domain for these bases, so the base's `solve`
    is replaced by one returning F13's plan (prepared through the base's real `prepare`): `ok`,
    the base result itself, `scope: unsupported_topology` and the true gross 89 (§3). A plan
    without its evaluation is replayed once, charged to the ledger."""
    ev = evaluate(B13, C13, P13, O)
    assert ev.status is EvalStatus.OK and ev.gross_output == 89
    assert sum(t.amount_out for t in ev.trace if t.token_out == "T") == 90
    base_result = SolveResult(
        C13.case_id, base, SolveStatus.OK, P13, ev if evaluated else None, ev.gross_output
    )

    def fake_solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
        return base_result

    factory: AlgorithmFactory = dataclasses.replace(ALGORITHMS[base], solve=fake_solve)
    monkeypatch.setattr(sp, "BASES", MappingProxyType({**sp.BASES, base: factory}))
    profile = small_profile(base)
    prep = sp.prepare(B13, profile.algorithm_config(sp.FACTORY))
    result, seam = solve(prep, C13, B13)
    record = result.search_stats[NAME]
    assert result.status is SolveStatus.OK and result.plan == P13 and result.score == 89
    assert result.evaluation == base_result.evaluation
    assert record["scope"] == "unsupported_topology"
    assert record["reason"] == "partially_consumed_fund:out"
    assert record["gross"] == record["base_gross"] == "89"
    assert seam == record["quotes"] == (0 if evaluated else 2)  # the replay: one quote per step
    rep = evaluate(B13, C13, result.plan, O)
    assert rep.status is EvalStatus.OK and rep.gross_output == 89


# ======================================================================================
# base row == the base's own registered run, case by case
# ======================================================================================

_SHARED: dict[str, Any] = {}


def _setup(base: str, b: SnapshotBundle, profile: RunProfile, mp: pytest.MonkeyPatch) -> None:
    """Prepare both identities from `profile` and wrap the base's `solve` so the raw base
    result `split_polish` received is captured (module state: inherited by forked workers)."""
    factory = ALGORITHMS[base]
    captured: list[SolveResult] = []

    def spy(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
        captured.append(factory.solve(case, context, budget))
        return captured[-1]

    mp.setattr(sp, "BASES", MappingProxyType({**sp.BASES, base: dataclasses.replace(
        factory, solve=spy)}))  # fmt: skip
    own, polish = prepared_pair(base, b, profile)
    _SHARED.clear()
    _SHARED.update(base=base, bundle=b, budget=profile.budget, own=own, polish=polish,
                   captured=captured)  # fmt: skip


def _compare_case(index: int) -> dict[str, Any]:
    """One case: the base's own run, then `split_polish(base)`, each under its own worker-style
    meter capped at `max_quotes`; the polished plan replayed by the evaluator (uncharged)."""
    base, b, budget = _SHARED["base"], _SHARED["bundle"], _SHARED["budget"]
    case = b.cases[index]
    started = time.monotonic()
    with metered_quotes(budget.max_quotes) as meter:
        own = ALGORITHMS[base].solve(case, SolveContext(b, O, _SHARED["own"]), budget)
    own_quotes = meter.counted
    captured: list[SolveResult] = _SHARED["captured"]
    captured.clear()
    with metered_quotes(budget.max_quotes) as meter:
        result = sp.solve(case, SolveContext(b, O, _SHARED["polish"]), budget)
    seam = meter.counted
    (raw,) = captured
    record, row = result.search_stats[NAME], result.search_stats["base"]
    equal = raw == own and row == {
        "algorithm": base, "status": own.status.value, "quotes": own_quotes,
        "search": dict(own.search_stats),
    }  # fmt: skip
    out = {"case": case.case_id, "equal": equal, "status": own.status.value, "ok": False,
           "ge": False, "improved": False, "scope": record.get("scope"),
           "truncated_by": record.get("truncated_by")}  # fmt: skip
    if own.status is SolveStatus.OK:
        assert own.score is not None and result.plan is not None
        with metered_quotes(None):
            rep = evaluate(b, case, result.plan, O)
        assert rep.status is EvalStatus.OK and str(rep.gross_output) == record["gross"]
        out.update(
            ok=result.status is SolveStatus.OK and seam == own_quotes + record["quotes"],
            ge=rep.gross_output >= own.score == int(record["base_gross"]),
            improved=rep.gross_output > own.score,
        )
    else:
        out.update(ok=result.status is own.status, ge=True)
    out["seconds"] = round(time.monotonic() - started, 2)
    return out


def _compare_all(parallel: bool) -> list[dict[str, Any]]:
    indexes = range(len(_SHARED["bundle"].cases))
    if parallel and "fork" in multiprocessing.get_all_start_methods():
        workers = max(1, min(8, (os.cpu_count() or 2) - 1))
        with ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("fork")
        ) as pool:
            return list(pool.map(_compare_case, indexes, chunksize=1))
    return [_compare_case(i) for i in indexes]


def _summary(label: str, rows: list[dict[str, Any]], seconds: float) -> dict[str, Any]:
    summary = {
        "cases": len(rows),
        "base_equal": sum(r["equal"] for r in rows),
        "status_ok": sum(r["status"] == "ok" for r in rows),
        "polished_ge_base": sum(r["ge"] for r in rows),
        "improved": sum(r["improved"] for r in rows),
        "unsupported_topology": sum(r["scope"] == "unsupported_topology" for r in rows),
        "truncated": sum(r["truncated_by"] is not None for r in rows),
        "ledger_ok": sum(r["ok"] for r in rows),
        "seconds": round(seconds, 1),
    }
    print(f"BASE EQUALITY {label}: {json.dumps(summary)}")
    return summary


@pytest.fixture
def shared() -> Iterator[None]:
    yield
    _SHARED.clear()


@pytest.mark.parametrize("base", NEW_BASES)
def test_every_tracked_corpus_case_records_the_base_own_run(
    base: str, monkeypatch: pytest.MonkeyPatch, shared: None
) -> None:
    """The tracked 19-pool real fixture (96 cases), campaign profile shape."""
    b = load_bundle(TRACKED_CORPUS)
    assert len(b.cases) == 96
    _setup(base, b, profile_for(base), monkeypatch)
    started = time.monotonic()
    rows = _compare_all(parallel=False)
    s = _summary(f"{base} tracked-corpus", rows, time.monotonic() - started)
    assert s["cases"] == s["base_equal"] == s["polished_ge_base"] == s["ledger_ok"] == 96
    assert s["improved"] > 0 and s["truncated"] == 0


# Pinned from the first run, and equal to the pre-implementation probe's independent counts
# (WHI-1626 stop report): improved cases per base; none refused, none truncated.
TUNING_IMPROVED = {"metis_inspired": 80, "metis_history": 78, "incremental_graph_repair": 72}


@pytest.mark.parametrize("base", NEW_BASES)
def test_every_tuning_split_case_records_the_base_own_run(
    base: str, monkeypatch: pytest.MonkeyPatch, shared: None
) -> None:
    """Acceptance 1 on the tuning split (96 cases, `full_gross.yaml` + the M4 graph keys + the
    base's pinned preset): the recorded base outcome equals the base's own run in every case and
    the polished gross, replayed by the evaluator, is never below it. Runtime on the reference
    machine (10 cores, 8 forked workers): about 5 min each for M4 and S4, 11 min for REP."""
    path = os.environ.get(TUNING_ENV)
    if not path:
        pytest.skip(f"set {TUNING_ENV} to the frozen bundle_tuning (data/ is primary-clone only)")
    assert Path(path).is_dir(), f"{TUNING_ENV}={path!r} is not a directory"
    b = load_bundle(path)
    assert len(b.cases) == 96 and len(b.pools) == 143
    _setup(base, b, profile_for(base), monkeypatch)
    started = time.monotonic()
    rows = _compare_all(parallel=True)
    s = _summary(f"{base} tuning-split", rows, time.monotonic() - started)
    assert s["cases"] == s["base_equal"] == s["polished_ge_base"] == s["ledger_ok"] == 96
    assert s["status_ok"] == 96 and s["improved"] == TUNING_IMPROVED[base]
    assert s["unsupported_topology"] == 0 and s["truncated"] == 0


# ======================================================================================
# CLI: run, quote, literal replay
# ======================================================================================


def _cli_profile(tmp_path: Path, base: str) -> Path:
    doc = document(base, source="daily_gross.yaml", graph={"chunks": 20, "label_hops": 3,
                                                            "label_pruning": True})  # fmt: skip
    doc["measurement"] = {"warmup": 0, "repeats": 1, "seed": 7, "order": "fixed",
                          "memory_pass": False}  # fmt: skip
    path = tmp_path / f"{base}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    parse_profile(doc, str(path))
    return path


@pytest.mark.parametrize("base", NEW_BASES)
def test_cli_run_records_the_base_own_row_and_replays_literally(
    base: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results"
    argv = ["run", "--bundle", str(MIXED), "--profile", str(_cli_profile(tmp_path, base)),
            "--results-dir", str(results), "--strategies", "profile"]  # fmt: skip
    assert main.main(argv) == 0
    (run_dir,) = list(results.iterdir())
    manifest = load_manifest(run_dir)
    assert list(manifest.algorithms) == [base, NAME]
    resolved = manifest.resolved_profile
    params = resolved["algorithm_config"][NAME]["params"]
    assert params == resolved["algorithm_config"][base]["params"] | {"chunks": 20}
    by = {(r["algorithm"], r["case_id"]): r for r in load_case_records(run_dir)}
    oks = improved = 0
    for case_id in manifest.measurement["case_order"]:
        own, record = by[(base, case_id)], by[(NAME, case_id)]
        row = record["search"]["base"]
        assert (row["algorithm"], row["status"], row["quotes"], row["search"]) == (
            base, own["status"], own["quotes"]["counted"], own["search"]
        )  # fmt: skip
        assert record["status"] == own["status"]
        if own["status"] != "ok":
            continue
        oks += 1
        polish = record["search"][NAME]
        assert polish["base_gross"] == own["score"] and polish["gross"] == record["score"]
        assert int(record["score"]) >= int(own["score"])  # the runner's own evaluation
        assert record["quotes"]["counted"] == row["quotes"] + polish["quotes"]
        improved += int(record["score"]) > int(own["score"])
    assert oks >= 4 and improved >= 1
    capsys.readouterr()
    replay = manifest.replay_command.split("main.py", 1)[1].split()
    assert main.main(replay) == 0
    (again,) = [p for p in results.iterdir() if p != run_dir]
    assert load_manifest(again).resolved_profile == resolved
    assert compare_runs(run_dir, again) == []
    report = tmp_path / "report"
    assert main.main(["report", str(run_dir), "--output", str(report)]) == 0
    assert "split polishing" in (report / "report.html").read_text()


@pytest.mark.parametrize("base", NEW_BASES)
def test_cli_quote_selects_it(
    base: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["quote", "--bundle", str(TRACKED_CORPUS), "--profile",
            str(_cli_profile(tmp_path, base)), "--token-in", "USDC", "--token-out", "USDT0",
            "--amount", "1500.25", "--quotes-dir", str(tmp_path / "q"), "--strategies",
            "profile", "--details"]  # fmt: skip
    assert main.main(args) == 0
    out = capsys.readouterr().out
    match = re.search(r"\(run (\S+)\)", out)
    assert match
    records = load_case_records(Path(match.group(1)))
    assert [r["algorithm"] for r in records] == [base, NAME]
    assert f"[{NAME}] ok" in out
    assert records[1]["search"]["base"]["quotes"] == records[0]["quotes"]["counted"]
    assert int(records[1]["score"]) >= int(records[0]["score"])

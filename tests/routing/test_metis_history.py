"""WHI-1550: the registered `metis_history` factory (history-labels.md §4-§9, §11).

Every behavioural check runs the REAL registered factory (`get_algorithm(NAME)`: its public
`prepare` and `solve`), its real selector `metis_history.choose_history` or its real
`diagnose_history` -- never the WHI-1549 specification selector as the thing under test.
From the research module (`test_history_labels_contract`) only the fixtures, the
specification (as an independent expected trajectory) and the independent exhaustive
oracle are reused: the oracle has its own adjacency, depth-first simple-path enumeration,
acyclicity test and hand CPMM formula (other families through the pure
`pools.quote.quote_exact_in`), and imports no search, solver or evaluator code. Whole plans
are replayed by the plain, memo-free evaluator.

`real_trajectory` runs the real selector inside the chunk loop so the oracle can audit each
chunk on its own committed state; `test_the_solve_commits_the_audited_trajectory` pins that
this loop is exactly the one `solve` commits (allocation, sequence and every counter).

Test-only seams are `monkeypatch`es of the module's own names (`choose_history`,
`diagnose_history`) or of module-level `evaluate` aliases for independent counting; the
production module patches nothing.
"""

from __future__ import annotations

import dataclasses
import json
import shlex
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import test_history_labels_contract as spec  # sibling: fixtures, specification, oracle
import test_metis_inspired as mi_fixtures  # sibling: the WHI-1449 X1-X7 fixtures
import yaml

import benchmark.profile as profile_module
from benchmark.diagnostics import CheckContext, check_diagnostics, diagnostics_view
from benchmark.objective import gross_only, synthetic_fixed_cost
from benchmark.profile import ProfileError, load_profile, parse_profile, preset_options
from benchmark.strategies import R021_ADDITIONS, derive
from pools.quote import QuoteLimitExceeded, metered_quotes
from routing.algorithms import incremental_graph_repair, metis_inspired
from routing.algorithms import metis_history as mh
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
from routing.algorithms.incremental_graph import chunk_amounts, merged_plan
from routing.algorithms.registry import (
    ALGORITHMS,
    BASE_STRATEGIES,
    OPTIMIZED_STRATEGIES,
    get_algorithm,
)
from routing.evaluator import EvalStatus
from routing.evaluator import evaluate as reference_evaluate
from routing.plan import RoutePlan
from routing.search import QuoteCache, build_graph_index
from snapshot.bundle import load_bundle
from snapshot.models import Case, ConstantProductPoolState, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
NAME = "metis_history"
FACTORY = get_algorithm(NAME)
PRESET = {"dominance": "history", "max_labels_per_signature": 1, "max_frontier_labels": 1024}
OFF = {**PRESET, "dominance": "off"}
WIDE = {"dominance": "history", "max_labels_per_signature": 10**6, "max_frontier_labels": 10**7}
WIDE_OFF = {**WIDE, "dominance": "off"}
NON_STRING_KEY: dict[Any, Any] = {1: 2, **PRESET}
PROFILES = REPO / "config" / "metis_history"
MIXED = REPO / "tests" / "fixtures" / "routing" / "mantle_mixed"
CORPUS = REPO / "tests" / "fixtures" / "corpus" / "bundle"
USDT = "0x201eba5cc46d216ce6dc03f6a759e8e766e956ae"
WMNT = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"
UNI_USDT_WMNT = "0x4cdfc22bf05209de87ee564746dc7e5174631d2b"
# metis_inspired's label-mode keys this identity keeps, and its own additions.
OWN_KEYS = {
    "dominance",
    "max_labels_per_signature",
    "max_frontier_labels",
    "labels_dropped_signature_cap",
    "labels_dropped_frontier_cap",
    "chunks_state_capped",
    "peak_signature_labels",
    "certified_strict_insertions",
    "termination",
    "evaluations",
    "r021",
}


def _params(label_hops: int = 4, chunks: int = 1, max_hops: int = 2) -> dict[str, int]:
    return {
        "max_hops": max_hops,
        "max_splits": 4,
        "percent_step": 5,
        "chunks": chunks,
        "label_hops": label_hops,
    }


def _context(
    bundle: SnapshotBundle,
    params: Mapping[str, int],
    options: Mapping[str, Any] = PRESET,
    sink: list[RoutePlan] | None = None,
) -> SolveContext:
    assert FACTORY.prepare is not None
    prepared = FACTORY.prepare(bundle, AlgorithmConfig(NAME, dict(params), dict(options)))
    return SolveContext(
        bundle, gross_only(), prepared, candidate_sink=None if sink is None else sink.append
    )


def _solve(
    bundle: SnapshotBundle,
    case: Case,
    params: Mapping[str, int],
    options: Mapping[str, Any] = PRESET,
    budget: Budget | None = None,
    sink: list[RoutePlan] | None = None,
) -> SolveResult:
    return FACTORY.solve(case, _context(bundle, params, options, sink), budget or Budget())


def _metis(
    bundle: SnapshotBundle,
    case: Case,
    params: Mapping[str, int],
    pruning: bool,
    budget: Budget | None = None,
    sink: list[RoutePlan] | None = None,
) -> SolveResult:
    config = AlgorithmConfig(metis_inspired.NAME, {**params, "label_pruning": pruning})
    prepared = metis_inspired.prepare(bundle, config)
    ctx = SolveContext(
        bundle, gross_only(), prepared, candidate_sink=None if sink is None else sink.append
    )
    return metis_inspired.solve(case, ctx, budget or Budget())


def _gross(bundle: SnapshotBundle, case: Case, plan: RoutePlan) -> int:
    ev = reference_evaluate(bundle, case, plan, gross_only())
    assert ev.status is EvalStatus.OK, ev.error
    return ev.gross_output


def _chunk_pools(result: SolveResult) -> list[list[str]]:
    """The committed chunk paths of the incremental candidate, as pool-id lists."""
    s = result.search_stats
    labels = [a["path"] for a in s["incremental_allocation"]]
    return [
        [part.split("]")[0] for part in labels[i].split("-[")[1:]]
        for i in s["incremental_chunk_sequence"]
    ]


def _options(options: Mapping[str, Any]) -> spec.SelectorOptions:
    return spec.SelectorOptions(
        str(options["dominance"]),
        int(options["max_labels_per_signature"]),
        int(options["max_frontier_labels"]),
    )


def real_trajectory(
    bundle: SnapshotBundle,
    case: Case,
    options: Mapping[str, Any],
    *,
    chunks: int,
    hops: int,
    budget: Budget | None = None,
) -> tuple[spec.Trajectory, mh.Work]:
    """The real `mh.choose_history` in `solve`'s chunk loop (carry, final chunk, commit),
    recording each chunk's committed state for the oracle; the merged plan is replayed by
    the plain evaluator."""
    budget = budget or Budget()
    index = build_graph_index(bundle)
    cache_ = QuoteCache(bundle)
    alloc = metis_inspired._Allocator(bundle, case, index, cache_)
    dist = metis_inspired.hops_to_target(index, case.token_in, case.token_out)
    safe = mh.upward_safe_edges(bundle)
    region: dict[tuple[str, int], bool] = {}
    work = mh.Work()
    amounts = chunk_amounts(case.amount_in, chunks)
    last = max(k for k, a in enumerate(amounts) if a > 0)
    records: list[spec.ChunkRecord] = []
    chunk_paths: list[Any] = []
    carry, status = 0, "ok"
    for k, chunk in enumerate(amounts):
        if chunk == 0:
            continue
        amount = carry + chunk
        before = (dict(alloc.flows), set(alloc.token_edges))
        was, cut = dataclasses.asdict(work), alloc.label_truncated
        choice = mh.choose_history(
            alloc,
            amount,
            hops,
            dist,
            budget,
            options,
            final=k == last,
            safe=safe,
            region=region,
            work=work,
        )
        now = dataclasses.asdict(work)
        cw = spec.ChunkWork(
            **{
                f.name: now[f.name] - was[f.name]
                for f in dataclasses.fields(spec.ChunkWork)
                if f.name in now and not f.name.startswith("peak")
            },
            truncated_by="max_candidates" if alloc.label_truncated > cut else None,
        )
        work.chunks_state_capped += cw.capped
        records.append(spec.ChunkRecord(k + 1, amount, k == last, *before, choice, cw))
        if k != last and (choice is None or choice[0] == 0):
            carry = amount
            continue
        if choice is None:
            status = f"chunk_{k + 1}_no_admissible_path"
            break
        carry = 0
        alloc.commit(choice[1], choice[2])
        chunk_paths.append(choice[1])
    evaluation = None
    if status == "ok":
        plan = merged_plan(case, alloc.flows.values())
        evaluation = reference_evaluate(bundle, case, plan, gross_only())
        if evaluation.status is not EvalStatus.OK:
            status = "invalid_plan"
    run = spec.Trajectory(
        status, records, chunk_paths, dict(alloc.flows), evaluation, cache_.misses
    )
    return run, work


def _audit(
    bundle: SnapshotBundle, case: Case, options: Mapping[str, Any], **kw: Any
) -> dict[str, int]:
    run, _ = real_trajectory(bundle, case, options, **kw)
    return spec.audit(bundle, case, run, kw["hops"])


@cache
def _mixed() -> SnapshotBundle:
    return load_bundle(MIXED)


def _x2() -> tuple[SnapshotBundle, Case]:
    """X2: real Uniswap v3 USDT/WMNT state; the larger S->USDT label overshoots its
    collected tick range where the smaller one fits (history-labels.md §2.4)."""
    bundle = spec.with_pools(
        spec.cp_bundle({"big": ["S", USDT, 10**12, 10**14], "small": ["S", USDT, 10**9, 10**9]}),
        _mixed().pools[UNI_USDT_WMNT],
        ConstantProductPoolState("weak", "S", WMNT, 10**9, 10**15, 30, None),
    )
    return bundle, Case("x2", "S", WMNT, 10**7)


# ------------------------------------------------------------------ registration and options


def test_registered_once_as_a_custom_identity_right_after_metis_inspired() -> None:
    assert ALGORITHMS[NAME] is mh.FACTORY is FACTORY
    assert NAME not in BASE_STRATEGIES and NAME not in OPTIMIZED_STRATEGIES
    assert profile_module.strategy_group(NAME) == "custom"
    assert R021_ADDITIONS == (  # R021-C/1 §2 order
        NAME,
        "direct_split_certified",
        incremental_graph_repair.NAME,
        "uni_sor_cycle_safe",
        "cfmm_dual",
    )
    assert FACTORY.options_validator is mh.validate_options  # module-level (picklable)
    assert FACTORY.capabilities == metis_inspired.CAPABILITIES
    assert FACTORY.search_params == metis_inspired.SEARCH_PARAMS
    assert FACTORY.graph_params == ("chunks", "label_hops")  # label_pruning is not read
    assert FACTORY.provenance is not None and "NOT Jupiter Metis" in str(FACTORY.provenance)
    # the reference identity is untouched: no options, no preset, same factory object
    assert ALGORITHMS[metis_inspired.NAME] is metis_inspired.FACTORY
    assert metis_inspired.FACTORY.options_validator is None
    assert metis_inspired.FACTORY.options_preset is None
    names = list(ALGORITHMS)
    assert names.count(NAME) == 1
    document, profile = derive(
        yaml.safe_load((REPO / "config" / "daily_gross.yaml").read_text()),
        "all",
        source_path="config/daily_gross.yaml",
        source_sha256="x",
    )
    assert list(profile.algorithms)[-11:] == [
        metis_inspired.NAME, NAME, "direct_split_certified", "incremental_graph_repair",
        "uni_sor_cycle_safe", "cfmm_dual", "single_path_bounded", "incremental_graph_bounded",
        "metis_history_bounded", "split_polish", "marginal_activation",
    ]  # fmt: skip
    assert len(profile.algorithms) == 19  # + 0.2.2 bounded (WHI-1599/1600), 0.2.4 (WHI-1632)
    assert profile.algorithm_options[NAME]["source"]["kind"] == "preset"
    assert dict(profile.algorithm_config(FACTORY).options) == PRESET
    assert profile.algorithm_config(FACTORY).params == {
        "max_hops": 2, "max_splits": 4, "percent_step": 5, "chunks": 200, "label_hops": 4
    }  # fmt: skip


def test_preset_file_is_the_pinned_v1_and_tampering_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = dict(FACTORY.options_preset or {})
    assert pin == {
        "path": "config/metis_history/preset_v1.yaml",
        "sha256": mh.PRESET["sha256"],
        "key": "R021-P04-metis_history",
        "version": 1,
    }
    assert preset_options(FACTORY) == PRESET  # history-labels.md §6 v1
    doc = yaml.safe_load((REPO / pin["path"]).read_text())
    assert (doc["key"], doc["version"], doc["algorithm"], doc["options"]) == (
        pin["key"],
        1,
        NAME,
        PRESET,
    )
    (tmp_path / "config" / "metis_history").mkdir(parents=True)
    changed = (
        (REPO / pin["path"]).read_text().replace("frontier_labels: 1024", "frontier_labels: 4096")
    )
    (tmp_path / pin["path"]).write_text(changed)
    monkeypatch.setattr(profile_module, "REPO_ROOT", tmp_path)
    with pytest.raises(ProfileError, match="differs from the pin"):
        preset_options(FACTORY)


@pytest.mark.parametrize(
    "options",
    [
        {k: v for k, v in PRESET.items() if k != "dominance"},  # required, no default
        {k: v for k, v in PRESET.items() if k != "max_frontier_labels"},
        {},
        {**PRESET, "beam": 2},  # unknown
        {**PRESET, "dominance": "amount"},
        {**PRESET, "dominance": True},
        {**PRESET, "dominance": None},
        {**PRESET, "max_labels_per_signature": True},  # bool for int
        {**PRESET, "max_labels_per_signature": 1.0},
        {**PRESET, "max_labels_per_signature": float("nan")},
        {**PRESET, "max_frontier_labels": float("inf")},
        {**PRESET, "max_labels_per_signature": 0},
        {**PRESET, "max_labels_per_signature": 1_000_001},
        {**PRESET, "max_frontier_labels": 0},
        {**PRESET, "max_frontier_labels": 10_000_001},
        {**PRESET, "max_frontier_labels": -1},
        {**PRESET, "max_frontier_labels": 10**400},
        {**PRESET, "max_labels_per_signature": None},  # "uncapped" is not expressible
        NON_STRING_KEY,
        *({**PRESET, key: 1} for key in ("label_hops", "chunks", "label_pruning", "max_hops",
                                           "max_candidates", "max_quotes", "objective", "seed")),
    ],
)  # fmt: skip
def test_invalid_options_are_refused_by_every_entry_point(options: dict[Any, Any]) -> None:
    """The shared `validated_options`, the factory's public `prepare` and profile loading
    refuse the same values; nothing is defaulted, clamped or coerced."""
    bundle, case = spec._fixture("tie_state")
    with pytest.raises(OptionsError):
        validated_options(FACTORY, options)
    assert FACTORY.prepare is not None
    with pytest.raises(OptionsError):
        FACTORY.prepare(bundle, AlgorithmConfig(NAME, _params(2, 2), options))
    doc = yaml.safe_load((PROFILES / "history_on.yaml").read_text())
    doc["algorithm_options"] = {NAME: options}
    with pytest.raises(ProfileError):
        parse_profile(doc, "<test>")


def test_valid_options_profiles_and_the_reused_prepare() -> None:
    for options in (
        PRESET,
        OFF,
        {"dominance": "history", "max_labels_per_signature": 1_000_000, "max_frontier_labels": 1},
        {"dominance": "off", "max_labels_per_signature": 7, "max_frontier_labels": 10_000_000},
    ):
        assert validated_options(FACTORY, options) == options
    on, off = (
        load_profile(PROFILES / "history_on.yaml"),
        load_profile(PROFILES / "history_off.yaml"),
    )
    for profile, options, kind, pruning in ((on, PRESET, "preset", True),
                                            (off, OFF, "override", False)):  # fmt: skip
        entry = profile.algorithm_options[NAME]
        assert (entry["options"], entry["source"]["kind"]) == (options, kind)
        assert entry["settings_sha256"] == settings_sha256(options)
        assert list(profile.algorithms) == [metis_inspired.NAME, NAME]
        params = profile.algorithm_config(FACTORY).params
        assert "label_pruning" not in params and params["label_hops"] == 4
        assert profile.algorithm_config(metis_inspired.FACTORY).params["label_pruning"] is pruning
        assert dict(profile.algorithm_config(metis_inspired.FACTORY).options) == {}
    # an explicit profile listing metis_history must declare its options (no built-in default)
    doc = yaml.safe_load((PROFILES / "history_on.yaml").read_text())
    del doc["algorithm_options"]
    with pytest.raises(ProfileError, match="requires algorithm_options.metis_history"):
        parse_profile(doc, "<test>")
    doc = yaml.safe_load((PROFILES / "history_on.yaml").read_text())
    del doc["graph"]["label_hops"]
    with pytest.raises(ProfileError, match=r"graph\.label_hops"):
        parse_profile(doc, "<test>")
    # prepare: its own options are validated, then cleared for the reused legacy prepare
    bundle, case = spec._fixture("tie_state")
    assert FACTORY.prepare is not None
    with pytest.raises(OptionsError):
        metis_inspired.prepare(
            bundle, AlgorithmConfig(NAME, {**_params(2, 2), "label_pruning": True}, PRESET)
        )
    prepared = FACTORY.prepare(bundle, AlgorithmConfig(NAME, _params(2, 2), PRESET))
    assert dict(prepared.options) == PRESET and prepared.label_hops == 2
    with pytest.raises(TypeError):
        prepared.options["dominance"] = "off"  # type: ignore[index,unused-ignore]
    for bad, match in (({"label_hops": 1}, r"label_hops >= search\.max_hops"),
                       ({"label_hops": True}, "label_hops"), ({"label_hops": None}, "label_hops"),
                       ({"chunks": 0}, "chunks")):  # fmt: skip
        with pytest.raises(mh.MetisHistoryConfigError, match=match):
            FACTORY.prepare(bundle, AlgorithmConfig(NAME, {**_params(2, 2), **bad}, PRESET))
    # graph.label_pruning is never read: present, absent, true or false give one solve
    results = [
        FACTORY.solve(case, SolveContext(bundle, gross_only(), FACTORY.prepare(
            bundle, AlgorithmConfig(NAME, {**_params(2, 2), **extra}, PRESET))), Budget())
        for extra in ({}, {"label_pruning": True}, {"label_pruning": False})
    ]  # fmt: skip
    assert results[0] == results[1] == results[2]
    with pytest.raises(TypeError, match="PreparedMetisHistory"):
        FACTORY.solve(case, SolveContext(bundle, gross_only(), None), Budget())


# ------------------------------------------------------------------ the selector is the spec


def _random_runs() -> list[tuple[SnapshotBundle, Case, int, int]]:
    runs = []
    for hops in (3, 4, 5):
        for seed in range(30 if hops < 5 else 12):
            bundle, cases = spec._random_bundle(seed, sourced=seed % 5 == 4)
            runs += [(bundle, case, chunks, hops) for case in cases for chunks in (1, 7)]
    return runs


def test_the_real_selector_is_the_normative_specification() -> None:
    """The real `choose_history` reproduces the WHI-1549 specification selector on every
    random CPMM graph (3-5 hops, 1 and 7 chunks, sourced pairs included): identical chunk
    choices, trajectory status, evaluated gross and every counter, for the mechanism
    (uncapped and the preset caps) and the disabled control. The specification's
    certified-edge set is the module's."""
    compared = strict = 0
    for bundle, case, chunks, hops in _random_runs():
        assert mh.upward_safe_edges(bundle) == spec.upward_safe_edges(bundle)
        for options in (WIDE, PRESET, OFF):
            real, _ = real_trajectory(bundle, case, options, chunks=chunks, hops=hops)
            want = spec.trajectory(bundle, case, _options(options), chunks=chunks, hops=hops)
            assert real.status == want.status and real.gross == want.gross, (case, options)
            assert [r.choice for r in real.records] == [r.choice for r in want.records]
            assert [r.flows for r in real.records] == [r.flows for r in want.records]
            got, exp = real.work(), want.work()
            for f in dataclasses.fields(spec.ChunkWork):
                if not f.name.startswith("peak"):
                    assert getattr(got, f.name) == getattr(exp, f.name), (f.name, case, options)
            strict += got.certified_strict_insertions
            compared += 1
    assert compared > 500 and strict > 0


def test_the_solve_commits_the_audited_trajectory() -> None:
    """`real_trajectory` is `solve`'s own chunk loop: the solve's committed allocation, chunk
    sequence and r021 search counters equal it, on random graphs and real CL/LB/CPMM state."""
    runs = [(b, c, ch, h) for b, c, ch, h in _random_runs()[::7]]
    runs += [(_mixed(), c, 7, 4) for c in _mixed().cases]
    for bundle, case, chunks, hops in runs:
        for options in (PRESET, OFF):
            run, work = real_trajectory(bundle, case, options, chunks=chunks, hops=hops)
            res = _solve(bundle, case, _params(hops, chunks, max_hops=2), options)
            s = res.search_stats
            if s["incremental_status"] == "no_paths":  # solve's structural reachability test
                assert all(r.choice is None for r in run.records)
                continue
            if run.status != "ok":
                assert s["incremental_status"] == run.status
                continue
            assert _chunk_pools(res) == [[e.pool_id for e in p] for p in run.chunk_paths]
            assert s["incremental_evaluated_gross"] == str(run.gross)
            r021 = s["r021"]["work"]
            for key in ("label_relaxations", "labels_discarded_dominance",
                        "labels_retained_unknown", "state_comparisons", "admission_checks",
                        "peak_frontier_labels"):  # fmt: skip
                assert r021[key] == getattr(work, key), key
            assert s["chunks_state_capped"] == work.chunks_state_capped
            assert s["labels_dropped_signature_cap"] == work.labels_dropped_signature_cap


# ------------------------------------------------------------------ independent oracle audits


@pytest.mark.parametrize("hops", [3, 4, 5])
def test_every_chunk_of_the_real_selector_matches_the_independent_oracle(hops: int) -> None:
    """history-labels.md §9.3 on the real selector: random CPMM multigraphs, dust to large,
    fee 0-100 bps, some sourced pairs, 1 and 7 chunks -> uncapped every chunk is `agree` or
    an equal-value `tie`, with certified strict pruning exercised; the disabled control
    reproduces the actual enumeration trajectory exactly; every complete plan is valid."""
    classes: dict[str, int] = {}
    strict = compared = 0
    for bundle, case, chunks, h in _random_runs():
        if h != hops:
            continue
        real, work = real_trajectory(bundle, case, WIDE, chunks=chunks, hops=hops)
        for cls, n in spec.audit(bundle, case, real, hops).items():
            classes[cls] = classes.get(cls, 0) + n
        strict += work.certified_strict_insertions
        off, _ = real_trajectory(bundle, case, WIDE_OFF, chunks=chunks, hops=hops)
        enum = spec.trajectory(bundle, case, "enumeration", chunks=chunks, hops=hops)
        assert off.status == enum.status and off.chunk_paths == enum.chunk_paths, case
        assert off.gross == enum.gross
        assert set(spec.audit(bundle, case, off, hops)) <= {"agree"}
        if real.status == "ok":
            assert real.gross is not None
            compared += 1
    assert set(classes) <= {"agree", "tie"}, classes
    assert classes.get("agree", 0) > 100 and strict > 0 and compared > 50


def test_real_cl_lb_cpmm_states_match_the_independent_oracle() -> None:
    """`mantle_mixed` (real Uniswap v3, Merchant Moe LB and Classic states): each chunk of
    the real selector's trajectory is `agree`/`tie` at 3 and 4 hops, uncapped. With the
    preset caps every non-`agree`/`tie` chunk is visibly `miss_capped`."""
    total: dict[str, int] = {}
    capped: dict[str, int] = {}
    for case in _mixed().cases:
        for hops, chunks in ((3, 13), (4, 7)):
            run, work = real_trajectory(_mixed(), case, WIDE, chunks=chunks, hops=hops)
            assert run.status == "ok" and run.gross is not None
            assert work.certified_strict_insertions == 0  # CL/LB present: not certified
            for cls, n in spec.audit(_mixed(), case, run, hops).items():
                total[cls] = total.get(cls, 0) + n
            for cls, n in _audit(_mixed(), case, PRESET, chunks=chunks, hops=hops).items():
                capped[cls] = capped.get(cls, 0) + n
    assert set(total) <= {"agree", "tie"} and total["agree"] > 20, total
    assert set(capped) <= {"agree", "tie", "tie_capped", "miss_capped"}, capped


# ------------------------------------------------------------------ counterexamples (real solve)


def test_r1_legal_prefix_is_recovered_and_the_cyclic_walk_stays_invalid() -> None:
    """External report §2.1: L4 keeps only the via-B label at C and returns 9,938; the real
    metis_history recovers the legal A-D-C-B-T (19,560, = E4, oracle `agree`) under the
    preset. The 23,708 walk is out of domain (evaluator `invalid_plan`, spec test)."""
    r = spec.RECON["R1_prefix_merge_vs_cycle"]
    bundle, case = spec._recon("R1_prefix_merge_vs_cycle")
    for options in (PRESET, WIDE):
        res = _solve(bundle, case, _params(4, 1), options)
        s = res.search_stats
        assert s["incremental_evaluated_gross"] == str(r["best_simple_path"]["gross"])
        assert _chunk_pools(res) == [r["best_simple_path"]["path"]]
        assert res.plan is not None and res.score == r["best_simple_path"]["gross"]
        assert res.score == _gross(bundle, case, res.plan)
        assert s["chosen_source"] == NAME and s["r021"]["fallback"]["used"] is False
    l4 = _metis(bundle, case, _params(4, 1), True)
    assert l4.search_stats["incremental_evaluated_gross"] == str(r["single_label_result"]["gross"])
    assert _audit(bundle, case, PRESET, chunks=1, hops=4) == {"agree": 1}


def test_x4_token_revisit_loss_is_recovered() -> None:
    r = spec.RECON["R7_X4"]
    bundle, case = spec._recon("R7_X4")
    res = _solve(bundle, case, _params(4, 1))
    assert _chunk_pools(res) == [["sa", "ax", "xy", "yd"]]
    assert res.search_stats["incremental_evaluated_gross"] == str(r["best_four_hop"]["gross"])
    l4 = _metis(bundle, case, _params(4, 1), True)
    assert res.score is not None and l4.score is not None and res.score > l4.score
    assert _audit(bundle, case, PRESET, chunks=1, hops=4) == {"agree": 1}


def test_x4b_prefix_admission_loss_is_recovered_on_the_final_multichunk_plan() -> None:
    """Chunk 2's dominated S-B-V (other visited set) continues to S-B-V-X-D; the final
    two-chunk plan equals E4's and is strictly better than L4's."""
    bundle, case = spec._recon("R8_X4b")
    params = _params(4, 2)
    res = _solve(bundle, case, params)
    e4 = _metis(bundle, case, params, False)
    l4 = _metis(bundle, case, params, True)
    assert _chunk_pools(res) == [["sx", "xa", "ad"], ["sb", "bv", "vx", "xd"]]
    assert res.plan == e4.plan and res.score == e4.score
    assert res.score is not None and l4.score is not None and res.score > l4.score
    assert _audit(bundle, case, PRESET, chunks=2, hops=4) == {"agree": 2}
    assert mh.diagnose_history(case, bundle, _context(bundle, params).prepared)["classes"] == {
        "agree": 2
    }


def test_x2_failure_domain_labels_are_retained_and_the_preset_cap_is_visible() -> None:
    """X2 (real CL state): uncapped, both same-signature S->USDT labels are retained
    (`labels_retained_unknown`), the chunk is `agree` and the result beats L3; the preset's
    one-label cap drops the fitting label, visibly: `miss_capped`, `state_cap`, still `ok`."""
    bundle, case = _x2()
    params = _params(3, 1)
    assert (UNI_USDT_WMNT, USDT) not in mh.upward_safe_edges(bundle)
    wide = _solve(bundle, case, params, WIDE)
    l3 = _metis(bundle, case, params, True)
    w = wide.search_stats
    assert w["r021"]["work"]["labels_retained_unknown"] >= 1
    assert w["certified_strict_insertions"] == 0 and w["chunks_state_capped"] == 0
    assert w["termination"] == "complete" and w["truncated_by"] is None
    assert int(w["incremental_evaluated_gross"]) > int(
        l3.search_stats["incremental_evaluated_gross"]
    )
    assert _audit(bundle, case, WIDE, chunks=1, hops=3) == {"agree": 1}
    capped = _solve(bundle, case, params, PRESET)
    c = capped.search_stats
    assert capped.status is SolveStatus.OK and c["labels_dropped_signature_cap"] == 1
    assert (c["chunks_state_capped"], c["termination"], c["truncated_by"]) == (
        1,
        "state_cap",
        "state_cap",
    )
    assert _audit(bundle, case, PRESET, chunks=1, hops=3) == {"miss_capped": 1}
    diag = mh.diagnose_history(case, bundle, _context(bundle, params).prepared)
    assert diag["classes"] == {"miss_capped": 1} and diag["gate"] == "pass"


def test_sourced_cpmm_overflow_is_not_certified_and_both_labels_are_kept() -> None:
    """history-labels.json `overflow`: the sourced pair reverts for the larger X amount; the
    static bound refuses to certify it, so the (uncapped) selector keeps both labels and
    returns E3's gross; L3 falls back to the weak direct pool."""
    want = spec.FIX["fixtures"]["overflow"]["expected"]
    bundle, case = spec._fixture("overflow")
    assert ("xd", "X") not in mh.upward_safe_edges(bundle)
    params = _params(3, 1, max_hops=1)
    res = _solve(bundle, case, params, WIDE)
    assert res.search_stats["incremental_evaluated_gross"] == str(want["history_gross"])
    assert want["history_gross"] == want["enumeration_gross"]
    assert _metis(bundle, case, params, True).search_stats["incremental_evaluated_gross"] == str(
        want["label_gross"]
    )
    assert _audit(bundle, case, WIDE, chunks=1, hops=3) == {"agree": 1}


def test_strict_pruning_only_in_non_final_chunks_of_certified_regions() -> None:
    bundle, case = spec._recon("R1_prefix_merge_vs_cycle")
    run, _ = real_trajectory(bundle, case, WIDE, chunks=3, hops=4)
    works = [r.work for r in run.records]
    assert [r.final for r in run.records] == [False, False, True]
    assert works[0] is not None and works[0].certified_strict_insertions > 0
    assert works[-1] is not None and works[-1].certified_strict_insertions == 0
    assert spec.audit(bundle, case, run, 4) == {"agree": 3}
    # no strict pruning at all when any continuation edge is a CL pool (X2)
    bundle, case = _x2()
    run, work = real_trajectory(bundle, case, WIDE, chunks=3, hops=3)
    assert work.certified_strict_insertions == 0


def test_tie_state_same_chunk_value_different_state_and_final_plan() -> None:
    bundle, case = spec._fixture("tie_state")
    want = spec.FIX["fixtures"]["tie_state"]["expected"]
    params = _params(2, 2)
    res = _solve(bundle, case, params)
    e2 = _metis(bundle, case, params, False)
    assert _chunk_pools(res) == want["history_chunk_paths"]
    assert _chunk_pools(e2) == want["enumeration_chunk_paths"]
    assert res.search_stats["incremental_evaluated_gross"] == str(want["history_gross"])
    assert e2.search_stats["incremental_evaluated_gross"] == str(want["enumeration_gross"])
    assert _audit(bundle, case, PRESET, chunks=2, hops=2) == {"tie": 1, "agree": 1}
    diag = mh.diagnose_history(case, bundle, _context(bundle, params).prepared)
    assert diag["classes"] == {"tie": 1, "agree": 1} and diag["gate"] == "pass"


def test_greedy_trap_per_chunk_exact_is_not_whole_plan_better() -> None:
    """Every chunk agrees with the exhaustive per-chunk maximum, yet L4's final plan is
    higher: no whole-plan monotonicity is implied or claimed."""
    bundle, case = spec._fixture("greedy_trap")
    want = spec.FIX["fixtures"]["greedy_trap"]["expected"]
    params = _params(4, 2)
    res = _solve(bundle, case, params)
    l4 = _metis(bundle, case, params, True)
    assert _chunk_pools(res) == want["history_chunk_paths"]
    assert res.search_stats["incremental_evaluated_gross"] == str(want["history_gross"])
    assert l4.search_stats["incremental_evaluated_gross"] == str(want["label_gross"])
    assert want["label_gross"] > want["history_gross"]
    assert _audit(bundle, case, PRESET, chunks=2, hops=4) == {"agree": 2}
    assert res.score is not None and l4.score is not None and l4.score > res.score


def test_amount_only_top_k_is_not_a_repair_but_the_real_signature_is() -> None:
    base = spec.RECON["R1_prefix_merge_vs_cycle"]
    for k in (1, 2, 4, 8):
        pools = dict(base["pools"])
        for i in range(1, k):
            pools[f"ab{i}"] = ["A", "B", 10**9 + i * 10**6, 10**9]
        bundle = spec.cp_bundle(pools, base["fee_bps"])
        case = Case("topk", "A", "T", base["amount_in"])
        assert spec._top_k_gross(bundle, case, k) == base["single_label_result"]["gross"]
        res = _solve(bundle, case, _params(4, 1))
        assert res.search_stats["incremental_evaluated_gross"] == str(
            base["best_simple_path"]["gross"]
        )
        assert _audit(bundle, case, PRESET, chunks=1, hops=4) == {"agree": 1}


# ------------------------------------------------------------------ caps and budgets


def test_caps_are_visible_approximations_never_silent() -> None:
    r1, r1_case = spec._recon("R1_prefix_merge_vs_cycle")
    narrow = {**WIDE, "max_frontier_labels": 1}
    res = _solve(r1, r1_case, _params(4, 1), narrow)
    s = res.search_stats
    assert s["labels_dropped_frontier_cap"] > 0 and s["chunks_state_capped"] == 1
    assert (s["termination"], s["truncated_by"], res.status) == ("state_cap", "state_cap",
                                                                 SolveStatus.OK)  # fmt: skip
    assert _audit(r1, r1_case, narrow, chunks=1, hops=4) == {"miss_capped": 1}
    assert s["chosen_source"] != NAME  # the lost path leaves the retained fallback in place
    assert s["r021"]["fallback"] == {
        "used": True,
        "source": s["chosen_source"],
        "reason": "retained_simpler_candidate",
    }


def test_a_state_capped_search_without_a_plan_is_timeout_never_no_route() -> None:
    """Only S-C-D reaches D (S-B-D's second pool is empty); no route within the fallback's
    one hop. A one-label frontier keeps S-B and drops S-C: no valid plan, reported as a
    declared cap (`timeout`), never as a complete `no_route`; uncapped it is `ok`."""
    bundle = spec.cp_bundle(
        {"sb": ["S", "B", 10**9, 10**9], "sc": ["S", "C", 10**9, 10**9],
         "bd": ["B", "D", 10**9, 0], "cd": ["C", "D", 10**9, 10**9]}
    )  # fmt: skip
    case = Case("capped", "S", "D", 10**6)
    params = _params(2, 1, max_hops=1)
    assert _solve(bundle, case, params, WIDE).status is SolveStatus.OK
    res = _solve(bundle, case, params, {**WIDE, "max_frontier_labels": 1})
    assert res.status is SolveStatus.TIMEOUT and "not evidence of no_route" in (res.error or "")
    assert res.search_stats["truncated_by"] == "state_cap"
    unreachable = _solve(bundle, Case("u", "S", "Z", 10), params)
    assert unreachable.status is SolveStatus.NO_ROUTE
    assert unreachable.search_stats["incremental_status"] == "no_paths"


def test_relaxation_budget_is_declared_truncation_per_chunk() -> None:
    bundle, case = spec._recon("R1_prefix_merge_vs_cycle")
    free = _solve(bundle, case, _params(4, 3))
    cut = _solve(bundle, case, _params(4, 3), budget=Budget(max_candidates=3))
    c = cut.search_stats
    assert c["label_relaxations"] == 9 < free.search_stats["label_relaxations"]  # 3 per chunk
    assert c["truncated_by"] == "max_candidates" and c["termination"] == "candidate_cap"
    assert c["label_truncated_chunks"] == 3 and NAME in c["truncated_stages"]
    assert cut.candidates_truncated >= 3
    assert c["r021"]["max_candidates_unit"] == "label_relaxations_per_chunk"


def _publications(bundle: SnapshotBundle, case: Case, params: Mapping[str, int]) -> None:
    """Every declared `max_quotes` from 0 to the full solve's: the meter never exceeds it
    (nothing is reset), the publications strictly improve by their independent replay, the
    returned plan is the last one (the best validated so far) with exactly its replayed
    score, and a budget cut never becomes `no_route`."""
    full_sink: list[RoutePlan] = []
    full = _solve(bundle, case, params, sink=full_sink)
    n = full.search_stats["quotes_executed"]
    for limit in range(n + 1):
        sink: list[RoutePlan] = []
        with metered_quotes(limit) as meter:
            res = _solve(bundle, case, params, budget=Budget(max_quotes=limit), sink=sink)
        s = res.search_stats
        assert not meter.exceeded and meter.counted == s["quotes_executed"] <= limit
        scores = [_gross(bundle, case, p) for p in sink]
        assert scores == sorted(set(scores)), limit
        if res.status is SolveStatus.OK:
            assert res.plan == sink[-1] and res.score == scores[-1], limit
        else:
            assert res.status is SolveStatus.TIMEOUT and not sink, limit
        if limit < n:  # someone's declared quote cut; the fallback's may leave ours complete
            assert s["truncated_by"] == "max_quotes", limit
            assert s["termination"] in ("quote_budget", "complete"), limit
    assert full.plan == full_sink[-1]


def test_the_quote_budget_is_never_reset_and_keeps_the_published_best() -> None:
    for name, chunks, hops in (("R1_prefix_merge_vs_cycle", 3, 4), ("R8_X4b", 2, 4)):
        bundle, case = spec._recon(name)
        _publications(bundle, case, _params(hops, chunks))
    bundle, case = spec._fixture("greedy_trap")
    _publications(bundle, case, _params(4, 2))


def test_a_hard_quote_kill_leaves_only_complete_published_plans() -> None:
    """The worker's hard meter (not the cooperative budget) kills the solve at every executed
    quote: `QuoteLimitExceeded` propagates, and the publications are a prefix of the
    uninterrupted strictly improving sequence, each a complete full-fill plan."""
    bundle, case = spec._recon("R8_X4b")
    params = _params(4, 2)
    full_sink: list[RoutePlan] = []
    full = _solve(bundle, case, params, sink=full_sink)
    scores = [_gross(bundle, case, p) for p in full_sink]
    assert scores == sorted(set(scores)) and scores[-1] == full.score and len(scores) == 2
    seen = set()
    for kill_at in range(full.search_stats["quotes_executed"]):
        sink: list[RoutePlan] = []
        with metered_quotes(kill_at), pytest.raises(QuoteLimitExceeded):
            _solve(bundle, case, params, sink=sink)
        assert sink == full_sink[: len(sink)]
        for plan in sink:
            ev = reference_evaluate(bundle, case, plan, gross_only())
            assert ev.status is EvalStatus.OK and ev.residuals == {}
        seen.add(len(sink))
    assert seen == {0, 1}


def test_one_quote_ledger_and_the_total_of_every_in_solve_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`quotes_executed` is the worker meter's count (fallback, label search and in-solve
    replay together); `internal_evaluations` is the total of every `evaluate` call inside the
    solve, counted independently by test-only wrappers of each module alias."""
    import routing.algorithms.direct_split as ds_module
    import routing.algorithms.path_split as ps_module
    import routing.algorithms.single_path as sp_module

    counts: dict[str, int] = {}
    for module in (sp_module, ds_module, ps_module, mh):
        inner = getattr(module, "evaluate", None)
        if inner is None:
            continue

        def wrapper(*a: Any, _n: str = module.__name__, _f: Any = inner, **k: Any) -> Any:
            counts[_n] = counts.get(_n, 0) + 1
            return _f(*a, **k)

        monkeypatch.setattr(module, "evaluate", wrapper)
    runs = [(*spec._recon("R8_X4b"), _params(4, 2)), (*spec._fixture("greedy_trap"), _params(4, 2))]
    runs += [(_mixed(), c, _params(4, 7)) for c in _mixed().cases]
    for bundle, case, params in runs:
        for options in (PRESET, OFF):
            counts.clear()
            with metered_quotes(None) as meter:
                res = _solve(bundle, case, params, options)
            s = res.search_stats
            ev = s["evaluations"]
            assert ev["total"] == sum(counts.values()) == ev["fallback"] + ev["incremental"]
            assert ev["incremental"] == counts.get(mh.__name__, 0) == 1  # one merged replay
            assert s["r021"]["work"]["internal_evaluations"] == ev["total"]
            assert s["quotes_executed"] == meter.counted == s["r021"]["work"]["quotes_executed"]
            assert res.candidates_considered >= s["label_relaxations"]


def test_case_order_and_a_shared_prepared_object_leak_no_state() -> None:
    """One prepared object (as a worker holds it) across cases in any order equals fresh
    solves; the prepared structures are immutable and unchanged."""
    real = _mixed()
    params = _params(4, 7)
    for options in (PRESET, OFF):
        ctx = _context(real, params, options)
        fresh = {c.case_id: _solve(real, c, params, options) for c in real.cases}
        safe_before = ctx.prepared.safe_edges
        for cases in (list(real.cases), list(reversed(real.cases)), [*real.cases, *real.cases]):
            for c in cases:
                assert FACTORY.solve(c, ctx, Budget()) == fresh[c.case_id], c.case_id
        assert ctx.prepared.safe_edges is safe_before and dict(ctx.prepared.options) == options
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.prepared.label_hops = 3


# ------------------------------------------------------------------ disabled control, reference


def _control_runs() -> list[tuple[SnapshotBundle, Case, dict[str, int]]]:
    f = mi_fixtures
    runs = [
        (f.PARALLEL, f.PARALLEL_CASE, _params(3, 20, max_hops=3)),
        (f.PREFIX, f.CASE, _params(3, 20, max_hops=2)),
        (f.SUFFIX, f.CASE, _params(4, 20, max_hops=2)),
        (f.SINGLE, f.CASE, _params(3, 20, max_hops=1)),
        (f.FAILURE, f.FAILURE_CASE, _params(3, 1, max_hops=1)),
        (f.FOUR_HOP, f.FOUR_HOP_CASE, _params(4, 20, max_hops=3)),
        (f.REVISIT, f.REVISIT_CASE, _params(4, 1, max_hops=2)),
        (f.ADMISSION, f.ADMISSION_CASE, _params(4, 2, max_hops=2)),
        (f.TIE, f.TIE_CASE, _params(2, 20, max_hops=1)),
    ]
    for name in ("overflow", "tie_state", "greedy_trap"):
        b, c = spec._fixture(name)
        runs.append((b, c, _params(4 if name != "tie_state" else 2, 2, max_hops=1)))
    graph = load_bundle(REPO / "tests" / "fixtures" / "routing" / "cpmm_graph")
    runs += [(graph, c, _params(3, 10, max_hops=2)) for c in graph.cases]
    runs += [(_mixed(), c, _params(h, 7, max_hops=2)) for c in _mixed().cases for h in (3, 4)]
    return runs


def test_dominance_off_equals_metis_inspired_enumeration_when_untruncated() -> None:
    """The disabled-mechanism control: `dominance: off` with the preset caps is plan-,
    evaluation-, score- and status-identical to `metis_inspired` with `label_pruning: false`
    at the same `label_hops` (the same-depth enumeration E_H), and publishes the same
    sequence, wherever neither side truncates (no cap bound: `chunks_state_capped == 0`)."""
    checked = 0
    for bundle, case, params in _control_runs():
        own_sink: list[RoutePlan] = []
        ref_sink: list[RoutePlan] = []
        off = _solve(bundle, case, params, OFF, sink=own_sink)
        ref = _metis(bundle, case, params, False, sink=ref_sink)
        assert off.search_stats["chunks_state_capped"] == 0, case
        for key in ("status", "plan", "evaluation", "score"):
            assert getattr(off, key) == getattr(ref, key), (case, key)
        assert own_sink == ref_sink
        o, r = off.search_stats, ref.search_stats
        for key in (
            "incremental_status",
            "incremental_allocation",
            "incremental_chunk_sequence",
            "incremental_evaluated_gross",
            "path_split_score",
            "chunks_carried",
        ):
            assert o[key] == r[key], (case, key)
        assert o["r021"]["work"]["labels_discarded_dominance"] == 0
        checked += 1
    assert checked >= 20


def test_metis_inspired_keeps_its_registered_behaviour() -> None:
    """The reference is not modified: its label search still loses X4/X4b/R1 exactly as its
    own frozen records say, and a metis_history solve in between changes nothing."""
    for name, chunks in (("R7_X4", 1), ("R8_X4b", 2), ("R1_prefix_merge_vs_cycle", 1)):
        bundle, case = spec._recon(name)
        before = _metis(bundle, case, _params(4, chunks), True)
        _solve(bundle, case, _params(4, chunks))
        after = _metis(bundle, case, _params(4, chunks), True)
        assert before == after
    r1, case = spec._recon("R1_prefix_merge_vs_cycle")
    s = _metis(r1, case, _params(4, 1), True).search_stats
    assert s["incremental_evaluated_gross"] == "9938" and s["label_skipped_revisit"] > 0
    own = _solve(r1, case, _params(4, 1)).search_stats
    kept = set(mi_fixtures.METIS_KEYS) - {"label_pruning"}
    assert kept <= set(own) and "label_pruning" not in own
    assert set(own) - set(s) == OWN_KEYS


def test_an_invalid_in_solve_replay_is_never_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """A history plan the in-solve evaluator rejects is counted (`invalid_plan`), never
    published or returned; the published fallback stands and the replay is still counted."""
    bundle, case = spec._recon("R8_X4b")
    params = _params(4, 2)
    real = reference_evaluate

    def rejecting(*a: Any, **k: Any) -> Any:
        return dataclasses.replace(real(*a, **k), status=EvalStatus.INVALID_PLAN, error="test")

    monkeypatch.setattr(mh, "evaluate", rejecting)
    sink: list[RoutePlan] = []
    res = _solve(bundle, case, params, sink=sink)
    s = res.search_stats
    assert s["incremental_status"] == "invalid_plan" and s["marginal_failures"]["invalid_plan"] == 1
    assert s["chosen_source"] != NAME and len(sink) == 1 and res.plan == sink[0]
    assert s["evaluations"]["incremental"] == 1
    assert s["r021"]["fallback"]["reason"] == "incremental_status invalid_plan"
    assert res.score == _gross(bundle, case, sink[0])


def test_objective_is_metis_inspireds_and_nothing_is_unsupported() -> None:
    """`metis_history` has `metis_inspired`'s objectives (R021-C/1 §2): under a per-call cost
    it runs (no `unsupported` row), keeps the simpler plan unless the history plan's complete
    objective score is strictly higher, and the disabled control stays `metis_inspired`'s
    enumeration under the same objective."""
    for fixed in (0, 10**6, 10**9):
        objective = synthetic_fixed_cost(fixed)
        for name, chunks in (("R8_X4b", 2), ("R1_prefix_merge_vs_cycle", 3)):
            bundle, case = spec._recon(name)
            params = _params(4, chunks)
            prepared = _context(bundle, params).prepared
            res = FACTORY.solve(case, SolveContext(bundle, objective, prepared), Budget())
            assert res.status is SolveStatus.OK and res.plan is not None
            ev = reference_evaluate(bundle, case, res.plan, objective)
            assert res.score == objective.score(ev)
            s = res.search_stats
            if s["chosen_source"] == NAME:
                assert int(s["incremental_score"]) > int(s["path_split_score"])
            off_prep = _context(bundle, params, OFF).prepared
            off = FACTORY.solve(case, SolveContext(bundle, objective, off_prep), Budget())
            config = AlgorithmConfig(metis_inspired.NAME, {**params, "label_pruning": False})
            ref_prep = metis_inspired.prepare(bundle, config)
            ref = metis_inspired.solve(case, SolveContext(bundle, objective, ref_prep), Budget())
            assert (off.plan, off.score, off.status) == (ref.plan, ref.score, ref.status)


# ------------------------------------------------------------------ the diagnostic pass


def test_diagnose_history_is_a_separate_pass_with_its_own_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, case = spec._recon("R8_X4b")
    params = _params(4, 2)
    ctx = _context(bundle, params)

    def forbidden(*_: Any, **__: Any) -> None:
        raise AssertionError("solve must never run the diagnostic")

    monkeypatch.setattr(mh, "diagnose_history", forbidden)
    with metered_quotes(None) as meter:
        res = FACTORY.solve(case, ctx, Budget())
    monkeypatch.undo()
    with metered_quotes(None) as diag_meter:
        diag = mh.diagnose_history(case, bundle, ctx.prepared)
    assert diag["gate"] == "pass" and diag["classes"] == {"agree": 2}
    assert diag["trajectory_status"] == "ok" and diag["chunks_audited"] == 2
    assert [r["history"]["path"] for r in diag["chunk_records"]] == [
        a["path"] for a in (res.search_stats["incremental_allocation"])
    ]
    c = diag["counters"]
    assert diag_meter.counted == c["history_quotes_executed"] + c["enumeration_quotes_executed"]
    assert c["label_relaxations"] == res.search_stats["label_relaxations"]
    assert res.search_stats["quotes_executed"] == meter.counted  # the solve's own ledger


def test_diagnose_history_flags_what_it_cannot_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    """`dominance: off` shows only `agree` (random graphs, uncapped); an unsound selector
    (mutated to amount-only dominance on every edge) is `unattributed` and fails the gate on
    uncapped X2; no audited chunk is `no_data`, never a vacuous pass."""
    for seed in range(8):
        bundle, cases = spec._random_bundle(seed)
        prepared = _context(bundle, _params(4, 7), OFF).prepared
        for case in cases:
            diag = mh.diagnose_history(case, bundle, prepared)
            if diag["chunks_audited"]:
                assert set(diag["classes"]) == {"agree"} and diag["gate"] == "pass", case
    bundle, case = _x2()
    wide = _context(bundle, _params(3, 1), WIDE).prepared
    assert mh.diagnose_history(case, bundle, wide)["gate"] == "pass"
    real_choose = mh.choose_history  # the unsafe amount-only rule: strict everywhere
    monkeypatch.setattr(mh, "region_certified", lambda *a, **k: True)
    monkeypatch.setattr(
        mh, "choose_history", lambda *a, **k: real_choose(*a, **{**k, "final": False})
    )
    diag = mh.diagnose_history(case, bundle, wide)
    assert diag["classes"] == {"miss": 1} and diag["gate"] == "fail"
    assert diag["unattributed_chunks"] == 1
    monkeypatch.undo()
    empty = mh.diagnose_history(Case("u", "S", "nowhere", 10), bundle, wide)
    assert (empty["gate"], empty["trajectory_status"], empty["chunks_audited"]) == (
        "no_data",
        "no_paths",
        0,
    )


# ------------------------------------------------------------------ r021 diagnostics


def _check_context(
    bundle: SnapshotBundle, case: Case, res: SolveResult, counted: int, options: Mapping[str, Any]
) -> CheckContext:
    """What the runner supplies independently of the solver."""
    return CheckContext(
        run={
            "git_revision": "g" * 40,
            "bundle_hash": bundle.bundle_hash,
            "algorithm": NAME,
            "effective_settings_sha256": settings_sha256(options),
        },
        request={
            "case_id": case.case_id,
            "token_in": case.token_in,
            "token_out": case.token_out,
            "amount_in": str(case.amount_in),
        },
        status=res.status.value,
        score=None if res.score is None else str(res.score),
        objective="gross_only",
        quotes_counted=counted,
        pools={pid: "constant_product" for pid in bundle.pools},
    )


def test_diagnostics_record_validates_under_the_runtime_and_contract_validators() -> None:
    contract = spec_validator()
    domains = set()
    for name in ("R8_X4b", "R1_prefix_merge_vs_cycle"):
        bundle, case = spec._recon(name)
        for options in (PRESET, OFF, WIDE):
            with metered_quotes(None) as meter:
                res = _solve(bundle, case, _params(4, 2), options)
            rec = res.search_stats["r021"]
            json.dumps(rec, sort_keys=True, allow_nan=False)
            ctx = _check_context(bundle, case, res, meter.counted, options)
            assert check_diagnostics(rec, ctx) == set(), (name, options)
            view = diagnostics_view(rec, ctx)
            assert (view["state"], view["reason"]) == ("unavailable", "not_produced")
            assert contract.check_diagnostics(rec, {
                "hard_killed": False, "quotes_counted": meter.counted, "run": {},
                "request": {}, "objective": "gross_only", "status": res.status.value,
                "final_score": None if res.score is None else str(res.score),
            }) == set()  # fmt: skip
            assert rec["certificate"] is None
            assert rec["max_candidates_unit"] == "label_relaxations_per_chunk"
            assert rec["domain"]["hops"] == {"max": 4, "param": "graph.label_hops"}
            assert rec["domain"]["amount_grid"] == {"kind": "chunk_grid", "chunks": 2}
            assert set(rec["work"]) == {
                "quotes_executed", "quotes_memoized", "internal_evaluations",
                "label_relaxations", "labels_discarded_dominance", "labels_retained_unknown",
                "state_comparisons", "peak_frontier_labels", "admission_checks",
            }  # fmt: skip
            wrong = {**rec, "work": {**rec["work"], "quotes_executed": meter.counted + 1}}
            assert check_diagnostics(wrong, ctx) == {"W_LEDGER"}
            domains.add((name, rec["candidate_domain_hash"]))
    assert len(domains) == 2  # one feasible set per request setting, whatever the options
    res = _solve(_mixed(), _mixed().cases[0], _params(4, 7))
    universe = res.search_stats["r021"]["domain"]["universe"]
    assert universe["cohort"] == "full_source" and universe["pools"] == list(_mixed().pools)


def spec_validator() -> Any:
    import importlib.util

    path = REPO / "tests" / "docs" / "test_research_021_contract.py"
    module_spec = importlib.util.spec_from_file_location("r021_contract_validator_1550", path)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ CLI: run + quote --details


def _saved_run(out: str) -> Path:
    marker = "(run "
    return Path(out[out.index(marker) + len(marker) :].split(")", 1)[0])


def test_cli_run_and_quote_details_with_the_on_off_profiles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both CLI paths through the spawned worker and the real factory: a distinct identity
    with its options identity and diagnostics, the off control equal to `metis_inspired`'s
    enumeration in the same run, one solve per quote, literal `--strategies profile` replay."""
    import main
    from benchmark.results import load_case_records, load_manifest
    from benchmark.runner import compare_runs

    for arm, options in (("history_on", PRESET), ("history_off", OFF)):
        profile = PROFILES / f"{arm}.yaml"
        results = tmp_path / arm / "runs"
        argv = ["run", "--bundle", str(MIXED), "--profile", str(profile),
                "--results-dir", str(results), "--strategies", "profile"]  # fmt: skip
        assert main.main(argv) == 0
        out = capsys.readouterr().out
        assert f"Experimental and other strategies (2): {metis_inspired.NAME}, {NAME}" in out
        (run_dir,) = results.iterdir()
        manifest = load_manifest(run_dir)
        assert list(manifest.algorithms) == [metis_inspired.NAME, NAME]
        entry = manifest.resolved_profile["algorithm_options"][NAME]
        assert entry["options"] == options
        assert entry["source"]["kind"] == ("preset" if options is PRESET else "override")
        assert manifest.resolved_profile["algorithm_config"][NAME]["params"]["label_hops"] == 4
        records = load_case_records(run_dir)
        ref = {r["case_id"]: r for r in records if r["algorithm"] == metis_inspired.NAME}
        own = [r for r in records if r["algorithm"] == NAME]
        assert len(own) == len(ref) == len(manifest.measurement["case_order"]) > 0
        for r in own:
            assert r["status"] == "ok" and r["score"] is not None
            if options is OFF:
                base = ref[r["case_id"]]
                assert (r["score"], r["evaluation"]) == (base["score"], base["evaluation"])
            assert r["search"]["dominance"] == options["dominance"]
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
        quotes = tmp_path / arm / "quotes"
        argv = ["quote", "--bundle", str(CORPUS), "--profile", str(profile), "--token-in", "USDC",
                "--token-out", "USDT0", "--amount", "1500.25", "--details",
                "--quotes-dir", str(quotes), "--strategies", "profile"]  # fmt: skip
        assert main.main(argv) == 0
        out = capsys.readouterr().out
        assert f"[{NAME}] ok" in out and "bound: unavailable (not_produced)" in out
        assert "max_candidates unit: label_relaxations_per_chunk" in out
        assert "labels_retained_unknown" in out and "internal_evaluations" in out
        assert "graph.label_pruning not read" in out
        quote_run = _saved_run(out)
        quote_records = load_case_records(quote_run)
        assert [r["algorithm"] for r in quote_records] == [metis_inspired.NAME, NAME]
        for r in quote_records:
            assert r["measurement"]["attempts_completed"] == 1
            assert len(r["measurement"]["solve_seconds"]) == 1
        saved = yaml.safe_load((quote_run.parent.parent / "profile.yaml").read_text())
        assert saved["algorithm_options"] == {NAME: options}


def test_saved_eight_nine_and_ten_strategy_profiles_replay_literally() -> None:
    """A saved effective profile never gains the new identity under `--strategies profile`,
    and `base` / `optimized` never select it."""
    source = yaml.safe_load((REPO / "config" / "daily_gross.yaml").read_text())
    document, _ = derive(source, "all", source_path="s", source_sha256="x")
    dsc = "direct_split_certified"  # WHI-1552, added after this identity
    cyc = [
        "uni_sor_cycle_safe",
        "cfmm_dual",
        "single_path_bounded",
        "incremental_graph_bounded",
        "metis_history_bounded",
        "split_polish",
        "marginal_activation",
    ]  # WHI-1556/1558/1599/1600/1632, later
    for drop in ([NAME, dsc, "incremental_graph_repair", *cyc, metis_inspired.NAME],
                 [NAME, dsc, "incremental_graph_repair", *cyc], [NAME, dsc, *cyc]):  # fmt: skip
        saved = json.loads(json.dumps(document))
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
        assert len(profile.algorithms) == 17 - len(drop)
    for mode in ("base", "optimized"):
        assert NAME not in derive(source, mode, source_path="s", source_sha256="x")[1].algorithms

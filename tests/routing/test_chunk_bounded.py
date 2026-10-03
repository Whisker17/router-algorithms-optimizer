"""WHI-1600: `incremental_graph_bounded` and `metis_history_bounded` -- `incremental_graph` and
`metis_history` with default-off per-chunk bound pruning (`docs/references/research-022/
pruning-contract.md` R022-Q02/1 §5 rule I1, §6 rules M1/M2 and gate `G_M2`, §8, §10).

Expected values come from the **reference** solvers (`incremental_graph.solve`,
`metis_history.solve` with the parameter off) and the exact quote seam, never from the bounded
code under test. The test-local pruning model of `test_pruning_contract.py` is the contract's own
model: the production solvers must agree with it counter for counter. Fixtures are tracked, so
these run in a worktree; the frozen tuning split lives only in the primary clone's gitignored
`data/` and is reached through `ROUTER_TUNING_BUNDLE` (a skip is not evidence: the tracked
real-pool fixtures are the always-run differential).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import json
import multiprocessing
import os
import pickle
import random
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, cast

import pytest
import yaml

import main
from benchmark.objective import ObjectiveContext, gross_only, synthetic_fixed_cost
from benchmark.profile import parse_profile, preset_options, read_profile_document, strategy_group
from benchmark.results import load_case_records, load_manifest
from benchmark.runner import compare_runs
from benchmark.strategies import R021_ADDITIONS, R022_ADDITIONS, derive
from pools import bounds as bounds_module
from pools.bounds import BoundTable, OutputBound
from pools.quote import quote_exact_in
from pools.result import QuoteStatus
from routing.algorithms import (
    chunk_pruning,
    incremental_graph,
    incremental_graph_bounded,
    metis_history,
    metis_history_bounded,
    path_split,
)
from routing.algorithms.base import (
    AlgorithmConfig,
    Budget,
    OptionsError,
    SolveContext,
    SolveResult,
    SolveStatus,
    settings_sha256,
)
from routing.algorithms.registry import ALGORITHMS, BASE_STRATEGIES, OPTIMIZED_STRATEGIES
from routing.plan import RoutePlan
from routing.search import build_graph_index
from snapshot.bundle import load_bundle
from snapshot.models import Case, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
IG, IG_B = incremental_graph.NAME, incremental_graph_bounded.NAME
MH, MH_B = metis_history.NAME, metis_history_bounded.NAME
TUNING_ENV = "ROUTER_TUNING_BUNDLE"
MIXED = REPO / "tests" / "fixtures" / "routing" / "mantle_mixed"
TRACKED_CORPUS = REPO / "tests" / "fixtures" / "corpus" / "bundle"


def _load_pruning_contract() -> ModuleType:
    path = REPO / "tests" / "routing" / "test_pruning_contract.py"
    spec = importlib.util.spec_from_file_location("_whi1600_pruning_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PC = _load_pruning_contract()
PRESET: dict[str, Any] = PC.PRESET
GENEROUS: dict[str, Any] = PC.GENEROUS
OFF: dict[str, Any] = PC.OFF
CAPPED_OFF: dict[str, Any] = PC.CAPPED_OFF
OPTION_SETS = {"preset": PRESET, "generous": GENEROUS, "off": OFF, "capped_off": CAPPED_OFF}

# §8.1: the counters that may differ from the reference's even when nothing is truncated
WORK_ONLY = frozenset(
    {
        "marginal_failures",
        "marginal_incomplete",
        "incomplete_example",
        "quotes_executed",
        "quotes_memoized",
    }  # fmt: skip
)
# the label-population counters of `metis_history_bounded` that rule M2 (and only M2) may change
M2_POPULATION = frozenset(
    {"label_relaxations", "label_rejected_cycle", "label_pruned_distance",
     "label_skipped_revisit", "label_truncated_chunks", "labels_dropped_signature_cap",
     "labels_dropped_frontier_cap", "peak_signature_labels", "certified_strict_insertions"}
)  # fmt: skip
R021_WORK_POPULATION = frozenset(
    {"label_relaxations", "labels_discarded_dominance", "labels_retained_unknown",
     "state_comparisons", "peak_frontier_labels", "admission_checks"}
)  # fmt: skip


# ======================================================================================
# drivers
# ======================================================================================


def run(
    name: str,
    bundle: SnapshotBundle,
    case: Case,
    *,
    chunks: int,
    hops: int = 3,
    label_hops: int | None = None,
    options: Mapping[str, Any] | None = None,
    budget: Budget | None = None,
    objective: Any = None,
    prepared: Any = None,
) -> tuple[SolveResult, list[RoutePlan]]:
    """One solve through the registered factory (the path the runner takes)."""
    factory = ALGORITHMS[name]
    params: dict[str, Any] = {
        "max_hops": hops, "max_splits": 2, "percent_step": 25, "chunks": chunks,
    }  # fmt: skip
    if name.startswith("metis"):
        params["label_hops"] = label_hops if label_hops is not None else max(hops, 3)
        options = PRESET if options is None else options
    if prepared is None:
        assert factory.prepare is not None
        prepared = factory.prepare(bundle, AlgorithmConfig(name, params, dict(options or {})))
    sink: list[RoutePlan] = []
    context = SolveContext(
        bundle=bundle,
        objective=objective or gross_only(),
        prepared=prepared,
        candidate_sink=sink.append,
    )
    return factory.solve(case, context, budget or Budget()), sink


def _r021_view(record: dict[str, Any], *, population: bool) -> dict[str, Any]:
    out = json.loads(json.dumps(record))
    for key in ("quotes_executed", "quotes_memoized"):
        out["work"].pop(key)
    if population:
        for key in R021_WORK_POPULATION:
            out["work"].pop(key)
    return cast("dict[str, Any]", out)


def assert_exact(
    reference: tuple[SolveResult, list[RoutePlan]],
    bounded: tuple[SolveResult, list[RoutePlan]],
    *,
    names: tuple[str, str] = (IG, IG_B),
) -> None:
    """Contract §3.1/§8.1: everything that must be identical -- the plan, the evaluation, the
    score, the status, the error, every counter outside the allowed-to-differ column and the
    `report_candidate` sequence."""
    (ref, ref_pub), (got, got_pub) = reference, bounded
    m2 = bool(block_of(got).get("m2", {}).get("active"))  # rule M2 may change the label population
    assert got.algorithm == names[1] and ref.algorithm == names[0]
    assert got.status is ref.status
    assert got.plan == ref.plan
    assert (got.evaluation is None) == (ref.evaluation is None)
    if got.evaluation is not None and ref.evaluation is not None:
        assert got.evaluation.to_dict() == ref.evaluation.to_dict()
    assert got.score == ref.score
    assert got.error == ref.error
    if not m2:
        assert got.candidates_considered == ref.candidates_considered
        assert got.candidates_truncated == ref.candidates_truncated
    assert got_pub == ref_pub
    allowed = WORK_ONLY | {"bound_pruning", "r021"} | (M2_POPULATION if m2 else frozenset())
    assert set(got.search_stats) == set(ref.search_stats) | {"bound_pruning"}
    for key, value in ref.search_stats.items():
        if key not in allowed:
            assert got.search_stats[key] == value, key
    if "r021" in ref.search_stats:
        got_r021 = _r021_view(got.search_stats["r021"], population=m2)
        ref_r021 = _r021_view(ref.search_stats["r021"], population=m2)
        assert got_r021.pop("algorithm") == names[1] and ref_r021.pop("algorithm") == names[0]
        assert got_r021 == ref_r021


def cells() -> Iterator[tuple[str, SnapshotBundle, Case]]:
    yield from PC.chunk_cases()


def reference_cut(result: SolveResult) -> bool:
    """Did a declared budget truncate this run at any stage (a `state_cap` is not a budget)?"""
    stats = result.search_stats
    return stats["truncated_by"] not in (None, "state_cap") or any(
        stage not in (MH, IG) for stage in stats["truncated_stages"]
    )


def block_of(result: SolveResult) -> dict[str, Any]:
    return cast("dict[str, Any]", result.search_stats["bound_pruning"])


# ======================================================================================
# incremental_graph_bounded: differential
# ======================================================================================

OBJECTIVES: list[tuple[str, Callable[[], Any]]] = [
    ("gross", gross_only),
    ("fixed1e6", lambda: synthetic_fixed_cost(10**6)),
    ("shape", lambda: PC.ShapeCost({1: 5, 2: 40, 3: None})),
    # Chunk choices are gross marginals; the net score enters only the final comparison, so even
    # a (never shipped) negative cost cannot reach a prune: the objective constructors reject it
    # and the chunk stage never reads it (contract §7; WHI-1599 amendment on negative costs).
    ("negative_shape_cost", lambda: PC.ShapeCost({1: -5, 2: 0, 3: None})),
]


def test_incremental_bounded_equals_the_reference_on_every_fixture_and_is_not_vacuous() -> None:
    n = scored = pruned = no_bound = rejected = carried = shared = 0
    quotes_ref = quotes_bnd = 0
    for _name, bundle, case in cells():
        for hops, chunks in ((2, 6), (3, 10)):
            for _label, make in OBJECTIVES:
                ref = run(IG, bundle, case, chunks=chunks, hops=hops, objective=make())
                got = run(IG_B, bundle, case, chunks=chunks, hops=hops, objective=make())
                assert_exact(ref, got)
                stats, block = got[0].search_stats, block_of(got[0])
                assert stats["quotes_executed"] <= ref[0].search_stats["quotes_executed"]
                assert block["exactness"] == {"label": "exact", "binding": []}
                assert block["rule"] == "I1" and block["bound_table_cost"] == 0
                assert block["bound_evaluations"] >= block["pruned_bound"] + block["bound_no_bound"]
                assert "m2" not in block  # an incremental_graph has no label search
                n += 1
                scored += stats["paths_scored"]
                pruned += block["pruned_bound"]
                no_bound += block["bound_no_bound"]
                rejected += stats["paths_rejected_cycle"]  # equal to the reference's (assert_exact)
                carried += stats["chunks_carried"]
                shared += bool(stats["incremental_shared_pools"])
                quotes_ref += ref[0].search_stats["quotes_executed"]
                quotes_bnd += stats["quotes_executed"]
    print(
        f"DIFFERENTIAL incremental_graph_bounded fixtures: cells={n} paths_scored={scored} "
        f"pruned_bound={pruned} no_bound_evals={no_bound} cycle_rejected={rejected} "
        f"chunks_carried={carried} shared_pool_plans={shared} quotes {quotes_ref} -> {quotes_bnd}"
    )
    assert pruned > 0 and quotes_bnd < quotes_ref and rejected > 0 and no_bound > 0
    assert carried > 0 and shared > 0


def test_incremental_production_counters_equal_the_contracts_pruning_model() -> None:
    """The contract's test-local model (WHI-1597 formulae, `_Allocator` with the rule inserted)
    and the production solver (the `pools.bounds` helper) agree counter for counter."""
    compared = 0
    for _name, bundle, case in cells():
        for hops, chunks in ((2, 6), (3, 10)):
            got, _ = run(IG_B, bundle, case, chunks=chunks, hops=hops)
            block = block_of(got)
            if not block["p0"]:
                continue
            model = PC.run_chunks(
                bundle, case, chooser="enum", hops=hops, chunks=chunks, bounds=PC.Bounds(bundle)
            )
            gate = model.alloc.gate
            assert block["pruned_bound"] == gate.pruned_bound
            assert block["bound_evaluations"] == gate.bound_evaluations
            assert block["bound_no_bound"] == gate.no_bound
            assert got.search_stats["paths_scored"] == model.alloc.scored
            compared += 1
    assert compared >= 30


def test_p0_the_pruning_switches_off_when_the_retained_simpler_candidate_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract §5.2: a skipped path might have been the only `incomplete_snapshot` evidence, so
    without an OK `path_split` candidate the bounded strategy runs the reference loop."""
    real = path_split.solve

    def no_retained(*args: Any, **kwargs: Any) -> SolveResult:
        return dataclasses.replace(
            real(*args, **kwargs), status=SolveStatus.NO_ROUTE, plan=None, evaluation=None,
            score=None,
        )  # fmt: skip

    monkeypatch.setattr(path_split, "solve", no_retained)
    for name, (ref_name, bnd_name) in {"ig": (IG, IG_B), "mh": (MH, MH_B)}.items():
        checked = 0
        for _n, bundle, case in list(cells()):
            ref = run(ref_name, bundle, case, chunks=8)
            got = run(bnd_name, bundle, case, chunks=8)
            assert_exact(ref, got, names=(ref_name, bnd_name))
            block = block_of(got[0])
            assert block["p0"] is False and block["pruned_bound"] == block["bound_evaluations"] == 0
            if name == "ig":  # no pruning at all: even the work counters are the reference's
                assert (
                    got[0].search_stats["quotes_executed"]
                    == (ref[0].search_stats["quotes_executed"])
                )
            checked += 1
        assert checked == len(list(cells()))


def test_the_reference_bounded_difference_is_only_the_new_block_and_the_work_counters() -> None:
    for _n, bundle, case in list(cells())[:20]:
        (ref, _), (got, _) = run(IG, bundle, case, chunks=8), run(IG_B, bundle, case, chunks=8)
        a, b = ref.to_dict(), got.to_dict()
        assert b["search"].pop("bound_pruning")["reference"] == IG
        for key in WORK_ONLY:
            a["search"].pop(key), b["search"].pop(key)
        assert (a.pop("algorithm"), b.pop("algorithm")) == (IG, IG_B)
        assert a == b


# ---- tie / carry / cycle / rounding (the named cases of the issue's acceptance) ----------


def test_a_tie_never_replaces_the_choice_and_equality_with_the_choice_is_pruned() -> None:
    """`p2` ties `p1` exactly: its bound (marginal + 1 slack) exceeds the choice, so it is
    evaluated, ties and the first pool keeps the chunk. `p3` and `p4` have a bound equal to /
    below the choice (`<=`: replacement is strict, so equality is skippable) and are pruned."""
    bundle = PC.synthetic(
        PC.cp("p1", "A", "B", 10**12, 10**12),
        PC.cp("p2", "A", "B", 10**12, 10**12),
        PC.cp("p3", "A", "B", 10**12, 998_500_000_000),  # bound == the choice, true marginal below
        PC.cp("p4", "A", "B", 10**12, 990_000_000_000),
    )
    case = PC.case_of("A", "B", 1001)
    ref, got = run(IG, bundle, case, chunks=1, hops=1), run(IG_B, bundle, case, chunks=1, hops=1)
    assert_exact(ref, got)
    assert got[0].plan is not None and [s.pool_id for s in got[0].plan.steps] == ["p1"]
    block = block_of(got[0])
    assert (block["pruned_bound"], block["bound_evaluations"]) == (2, 3)
    assert got[0].search_stats["marginal_failures"] == {}  # a skipped path is never a failure
    ref_mh = run(MH, bundle, case, chunks=1, hops=1)
    got_mh = run(MH_B, bundle, case, chunks=1, hops=1)
    assert_exact(ref_mh, got_mh, names=(MH, MH_B))
    assert block_of(got_mh[0])["pruned_bound"] == 2  # M1: the same two arrivals


def test_a_carried_chunk_keeps_the_chunk_schedule_and_the_result() -> None:
    """Chunks too small to buy any output are carried into the next chunk; the bounded run must
    carry exactly the same amounts, then choose through the same rule on the bigger chunk (the
    carried amount is the chunk's `m_0` for every bound)."""
    bundle = PC.synthetic(
        PC.cp("x1", "A", "B", 10**6, 10**3),
        PC.cp("x2", "A", "B", 1_200_000, 1_000),
        PC.cp("x3", "A", "B", 9 * 10**5, 10**3),
    )
    carried = pruned = 0
    for amount in (6000, 20_000, 90_000):
        for chunks in (10, 20, 40):
            case = PC.case_of("A", "B", amount)
            for names in ((IG, IG_B), (MH, MH_B)):
                ref = run(names[0], bundle, case, chunks=chunks, hops=1, label_hops=1)
                got = run(names[1], bundle, case, chunks=chunks, hops=1, label_hops=1)
                assert_exact(ref, got, names=names)
                carried += got[0].search_stats["chunks_carried"]
                pruned += block_of(got[0])["pruned_bound"]
    assert carried > 30 and pruned > 30


def _small_cpmm_cells(count: int, seed: int) -> Iterator[tuple[SnapshotBundle, Case, int]]:
    """Small parallel CPMM pools and a few units per chunk: integer rounding of the chunk
    marginal is the whole story here (a marginal can exceed `floor(rate * m)` by one)."""
    rng = random.Random(seed)
    for _ in range(count):
        pools = [
            PC.cp(f"p{j}", "A", "B", rng.randint(200, 3000), rng.randint(200, 3000), 30)
            for j in range(rng.randint(2, 3))
        ]
        chunks = rng.choice([5, 8, 12, 20, 40])
        yield PC.synthetic(*pools), PC.case_of("A", "B", rng.randint(chunks, 4 * chunks)), chunks


def _incremental_trajectory(result: SolveResult) -> tuple[Any, ...]:
    s = result.search_stats
    return (
        s["incremental_chunk_sequence"],
        s["incremental_allocation"],
        s["incremental_score"],
        s["chunks_carried"],
    )


def test_rounding_slack_the_small_pool_sweep_is_exact_and_omitting_the_slack_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation (a), issue acceptance "a case where omitting slack would wrongly prune". The
    sweep is exact on the real helper; the very same cells differ once the slack is dropped
    from every hop's bound (a candidate whose rounded marginal is `floor(rate * m) + 1` is
    then pruned against an incumbent equal to `floor(rate * m)`)."""
    sweep = list(_small_cpmm_cells(900, seed=5))
    refs = [run(IG, b, c, chunks=k, hops=1) for b, c, k in sweep]
    for (b, c, k), ref in zip(sweep, refs, strict=True):
        assert_exact(ref, run(IG_B, b, c, chunks=k, hops=1))

    def without_slack(
        bounds: Mapping[tuple[str, str], OutputBound | None], edge: Any, committed: int, m: int
    ) -> int | None:
        bound = bounds.get((edge.pool_id, edge.token_in))
        if bound is None or bound.slack is None or committed + m > bounds_module.CHUNK_DOMAIN_MAX:
            return None
        return (bound.rate * m).__floor__()

    monkeypatch.setattr(chunk_pruning, "hop_bound", without_slack)
    differing = sum(
        _incremental_trajectory(ref[0])
        != _incremental_trajectory(run(IG_B, b, c, chunks=k, hops=1)[0])
        for (b, c, k), ref in zip(sweep, refs, strict=True)
    )
    print(f"MUTATION no-slack: {differing} of {len(sweep)} cells differ from the reference")
    assert differing > 0
    # a named cell of the sweep, as the readable regression
    bundle = PC.synthetic(PC.cp("p0", "A", "B", 1024, 1334), PC.cp("p1", "A", "B", 1023, 1442))
    case = PC.case_of("A", "B", 106)
    bad = run(IG_B, bundle, case, chunks=40, hops=1)[0]
    monkeypatch.undo()
    ref = run(IG, bundle, case, chunks=40, hops=1)
    assert_exact(ref, run(IG_B, bundle, case, chunks=40, hops=1))
    assert _incremental_trajectory(bad) != _incremental_trajectory(ref[0])


def _swapped_state_bound(bundle: SnapshotBundle) -> Callable[..., int | None]:
    """The tempting-but-unsafe bound: recompute rate and slack from the pool's swapped state
    (`new_state` after the committed aggregate) instead of the original frozen state."""

    def swapped(bounds: Any, edge: Any, committed: int, m: int) -> int | None:
        state = bundle.pools[edge.pool_id]
        if committed:
            first = quote_exact_in(state, edge.token_in, committed)
            assert first.status is QuoteStatus.OK and first.new_state is not None
            state = first.new_state
        bound = bounds_module.output_bound(state, edge.token_in)
        if bound is None or bound.slack is None or committed + m > bounds_module.CHUNK_DOMAIN_MAX:
            return None
        return (bound.rate * m + bound.slack).__floor__()

    return swapped


def _swapped_state_scenario() -> tuple[SnapshotBundle, Case, list[int]]:
    """`P` (1e8/1e8) carries 1e8 after chunk 1; the next chunk is 1e5 and `P`'s aggregate
    marginal is 24 321 above... the swapped-state bound by 24 units (contract §3.3). `Q`, scored
    first, is tuned so its fresh marginal lands inside that window: the reference prefers `P`
    (strictly larger), the swapped-state bound prunes `P` and picks `Q`."""
    bundle = PC.synthetic(
        PC.cp("Q", "A", "B", 10**8, 25_064_000), PC.cp("P", "A", "B", 10**8, 10**8)
    )
    schedule = [10**8, 10**5]
    return bundle, PC.case_of("A", "B", sum(schedule)), schedule


@pytest.mark.parametrize("which", ["incremental", "metis"])
def test_recomputing_the_bound_from_the_swapped_state_is_caught(
    which: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation (b), contract §3.3: the real strategies bound the aggregate marginal from the
    ORIGINAL state; a bound recomputed from the swapped state underestimates it and changes the
    chunk choice. The chunk schedule is pinned through `chunk_amounts` (the seam both solvers
    read) so the scenario is exact, not luck."""
    bundle, case, schedule = _swapped_state_scenario()
    ref_name, bnd_name = (IG, IG_B) if which == "incremental" else (MH, MH_B)
    module = incremental_graph if which == "incremental" else metis_history
    monkeypatch.setattr(module, "chunk_amounts", lambda amount, chunks: schedule)
    kw: dict[str, Any] = {"chunks": 2, "hops": 1, "label_hops": 1}
    ref = run(ref_name, bundle, case, **kw)
    good = run(bnd_name, bundle, case, **kw)
    assert_exact(ref, good, names=(ref_name, bnd_name))
    assert ref[0].search_stats["incremental_chunk_sequence"] == [0, 0]  # both chunks go to `P`
    assert block_of(good[0])["bound_evaluations"] >= 1  # the bound is consulted, and is safe
    monkeypatch.setattr(chunk_pruning, "hop_bound", _swapped_state_bound(bundle))
    bad = run(bnd_name, bundle, case, **kw)
    assert (
        bad[0].search_stats["incremental_allocation"]
        != ref[0].search_stats["incremental_allocation"]
    )
    assert bad[0].search_stats["incremental_score"] != ref[0].search_stats["incremental_score"]


def test_an_underestimating_rate_is_caught_by_the_differential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def differing() -> int:
        count = 0
        for _n, bundle, case in cells():
            ref, _ = run(IG, bundle, case, chunks=10)
            got, _ = run(IG_B, bundle, case, chunks=10)
            count += _incremental_trajectory(ref) != _incremental_trajectory(got)
        return count

    assert differing() == 0
    original = chunk_pruning.hop_bound

    def halved(bounds: Any, edge: Any, committed: int, m: int) -> int | None:
        hop = original(bounds, edge, committed, m)
        return None if hop is None else hop // 2

    monkeypatch.setattr(chunk_pruning, "hop_bound", halved)
    assert differing() > 0


def test_a_hop_outside_the_domain_or_without_slack_has_no_bound_and_never_prunes() -> None:
    """`slack is None` (3 of 286 corpus directions) and `x + m > 2**127` mean "no bound"."""
    pool = PC.cp("big", "A", "B", 1 << 140, 1 << 140)
    bundle = PC.synthetic(pool)
    table = bounds_module.build_bounds(bundle)
    edge = build_graph_index(bundle).edges_from("A")[0]
    assert chunk_pruning.hop_bound(table.bounds, edge, 0, 1 << 127) is not None
    assert chunk_pruning.hop_bound(table.bounds, edge, 0, (1 << 127) + 1) is None
    assert chunk_pruning.hop_bound(table.bounds, edge, 1, 1 << 127) is None  # committed counts
    rate_only = {k: OutputBound(v.rate, None) for k, v in table.bounds.items() if v is not None}
    assert chunk_pruning.hop_bound(rate_only, edge, 0, 10) is None  # never a rate-only prune
    # end to end: a table whose every direction is rate-only prunes nothing and counts it
    for _name, bundle, case in list(cells())[:12]:
        for name, bnd in ((IG, IG_B), (MH, MH_B)):
            prepared = ALGORITHMS[bnd].prepare(  # type: ignore[misc]
                bundle,
                AlgorithmConfig(
                    bnd,
                    {
                        "max_hops": 3,
                        "max_splits": 2,
                        "percent_step": 25,
                        "chunks": 8,
                        **({"label_hops": 3} if name == MH else {}),
                    },  # fmt: skip
                    dict(PRESET) if name == MH else {},
                ),
            )
            table = prepared.bound_table
            blank = dataclasses.replace(
                table,
                bounds=MappingProxyType(
                    {
                        k: None if v is None else OutputBound(v.rate, None)
                        for k, v in table.bounds.items()
                    }
                ),
            )
            if name == IG:
                weak = incremental_graph.PreparedBoundedIncrementalGraph(
                    prepared.path_split, prepared.chunks, blank
                )
            else:
                weak = dataclasses.replace(prepared, bound_table=blank)
            ref = run(name, bundle, case, chunks=8)
            got = run(bnd, bundle, case, chunks=8, prepared=weak)
            assert_exact(ref, got, names=(name, bnd))
            block = block_of(got[0])
            assert block["pruned_bound"] == 0
            assert block["bound_no_bound"] == block["bound_evaluations"]
            assert got[0].search_stats["quotes_executed"] == ref[0].search_stats["quotes_executed"]


# ---- random multi-chunk cases, budgets, state leaks --------------------------------------


def test_random_multi_chunk_graphs_are_exact_for_both_chunk_searches() -> None:
    rng = random.Random(22_1600_01)
    n = pruned_ig = pruned_mh = multi = 0
    for _ in range(160):
        bundle = PC.random_cpmm_graph(rng, "ABCDE", (7, 12))
        case = PC.case_of("A", "E", rng.choice([10**5, 10**6, 5 * 10**6, 10**7]))
        chunks = rng.choice([3, 6, 12, 24])
        ref, got = run(IG, bundle, case, chunks=chunks), run(IG_B, bundle, case, chunks=chunks)
        assert_exact(ref, got)
        pruned_ig += block_of(got[0])["pruned_bound"]
        multi += len(got[0].search_stats["incremental_allocation"]) > 1
        options = rng.choice([PRESET, OFF, CAPPED_OFF, GENEROUS])
        ref, got = (
            run(MH, bundle, case, chunks=chunks, options=options),
            run(MH_B, bundle, case, chunks=chunks, options=options),
        )
        block = block_of(got[0])
        assert_exact(ref, got, names=(MH, MH_B))
        pruned_mh += block["pruned_bound"]
        n += 1
    print(
        f"RANDOM {n} graphs: incremental pruned={pruned_ig} metis pruned={pruned_mh} split={multi}"
    )
    assert pruned_ig > 0 and pruned_mh > 0 and multi > 20


@pytest.mark.parametrize("pair", [(IG, IG_B), (MH, MH_B)])
def test_budget_labels_and_truncation_points_on_fixtures_and_random_graphs(
    pair: tuple[str, str],
) -> None:
    """Contract §6.5/§8.2: a bounded run that truncates is labelled `not_exact_budget_binding`
    (never exact) and implies the reference truncates; an untruncated bounded run equals the
    unbudgeted reference; where the reference is untruncated the two are equal. A binding
    `max_candidates` also leaves every chunk-search counter at the reference's value (a skipped
    path / relaxation still counts, so the truncation points do not move)."""
    ref_name, bnd_name = pair
    rng = random.Random(22_1600_02)
    pool = [(b, c) for _n, b, c in cells()]
    pool += [
        (
            PC.random_cpmm_graph(rng, "ABCDE", (7, 12)),
            PC.case_of("A", "E", rng.choice([10**6, 10**7])),
        )
        for _ in range(40)
    ]
    checked = binding = free = 0
    for bundle, case in pool:
        base = run(bnd_name, bundle, case, chunks=6)
        unbudgeted_ref = run(ref_name, bundle, case, chunks=6)
        for budget in (
            *(Budget(max_candidates=n) for n in (1, 2, 4, 8, 16)),
            *(Budget(max_quotes=n) for n in (3, 6, 12, 24, 48)),
        ):
            ref = run(ref_name, bundle, case, chunks=6, budget=budget)
            got = run(bnd_name, bundle, case, chunks=6, budget=budget)
            block = block_of(got[0])
            assert got[0].status in (SolveStatus.OK, SolveStatus.TIMEOUT, SolveStatus.NO_ROUTE,
                                     SolveStatus.INCOMPLETE_SNAPSHOT)  # fmt: skip
            checked += 1
            budget_cut = block["exactness"]["binding"]
            if budget_cut:
                binding += 1
                assert block["exactness"]["label"] == "not_exact_budget_binding"
                assert set(budget_cut) <= {
                    "max_candidates",
                    "max_quotes",
                    "single_path",
                    "direct_split",
                }
                assert reference_cut(ref[0])  # B truncates => R truncates
                if budget.max_candidates is not None:  # the counted unit never moves
                    for key in ("paths_scored", "paths_truncated"):
                        if key in ref[0].search_stats and ref[0].search_stats[key] is not None:
                            assert got[0].search_stats[key] == ref[0].search_stats[key]
                    assert got[0].candidates_considered == ref[0].candidates_considered
                    assert got[0].candidates_truncated == ref[0].candidates_truncated
            else:
                assert block["exactness"] == {"label": "exact", "binding": []}
                assert_exact(unbudgeted_ref, got, names=pair)  # B untruncated == R-infinity
            if not reference_cut(ref[0]):
                free += 1
                assert not budget_cut
                assert_exact(ref, got, names=pair)
        assert block_of(base[0])["exactness"]["label"] == "exact"
    print(f"BUDGETS {bnd_name}: cells={checked} binding={binding} reference-free={free}")
    assert checked and binding > 30 and free > 30


def _prepare_for(name: str, bundle: SnapshotBundle) -> Any:
    params = {"max_hops": 3, "max_splits": 2, "percent_step": 25, "chunks": 6}
    if name.startswith("metis"):
        params["label_hops"] = 3
    options = dict(PRESET) if name.startswith("metis") else {}
    return ALGORITHMS[name].prepare(bundle, AlgorithmConfig(name, params, options))  # type: ignore[misc]


@pytest.mark.parametrize("name", [IG_B, MH_B])
def test_a_shared_prepared_object_never_leaks_state_between_cases_or_orders(name: str) -> None:
    items = [(b, c) for _n, b, c in cells()]
    fresh = {
        (id(b), c.case_id): run(name, b, c, chunks=6)[0].to_dict() for b, c in items
    }  # a fresh prepared object per solve
    for order in (items, items[::-1]):
        prepared: dict[int, Any] = {}
        for bundle, case in order:  # one prepared object per bundle, reused across cases
            prepared.setdefault(id(bundle), _prepare_for(name, bundle))
            got = run(name, bundle, case, chunks=6, prepared=prepared[id(bundle)])[0]
            assert got.to_dict() == fresh[id(bundle), case.case_id]


def test_nothing_in_the_recorded_search_is_wall_clock_so_replay_is_literal() -> None:
    """The runner compares a replayed `search` exactly: no `seconds`, no float anywhere in
    `search_stats["bound_pruning"]` (the prepared object keeps the table's time)."""
    for name in (IG_B, MH_B):
        for _n, bundle, case in list(cells())[:6]:
            first, second = (
                run(name, bundle, case, chunks=6)[0],
                run(name, bundle, case, chunks=6)[0],
            )
            assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
                second.to_dict(), sort_keys=True
            )
            record = json.dumps(block_of(first))
            assert "seconds" not in record and not re.search(r"\d\.\d", record)


# ======================================================================================
# metis_history_bounded
# ======================================================================================


@pytest.mark.parametrize("label", list(OPTION_SETS))
def test_metis_bounded_equals_the_reference_under_every_dominance_and_cap_setting(
    label: str,
) -> None:
    """M1 is population neutral: every label counter, the cap decisions and `termination` are
    the reference's -- on chunks the reference caps too. Where `G_M2` opens, M2 acts and only
    the label-population counters may differ (the plan and score never do)."""
    options = OPTION_SETS[label]
    n = pruned = m2_active = capped = relaxations = 0
    for _name, bundle, case in cells():
        for hops_, chunks in ((2, 8), (3, 24)):
            ref = run(MH, bundle, case, chunks=chunks, hops=hops_, options=options)
            got = run(MH_B, bundle, case, chunks=chunks, hops=hops_, options=options)
            block = block_of(got[0])
            active = block["m2"]["active"]
            assert_exact(ref, got, names=(MH, MH_B))
            stats = got[0].search_stats
            assert stats["quotes_executed"] <= ref[0].search_stats["quotes_executed"]
            assert block["exactness"]["label"] == "exact" and block["rule"] in {"M1", "M1+M2"}
            assert block["rule"] == ("M1+M2" if active else "M1")
            assert (block["bound_table_cost"] > 0) == active
            assert block["m2"]["gate"] in {"open", "closed:frontier", "closed:dominance", "n/a"}
            if not active:  # M1 alone: the label search is byte-for-byte the reference's
                assert stats["label_relaxations"] == ref[0].search_stats["label_relaxations"]
            n += 1
            pruned += block["pruned_bound"]
            m2_active += active
            capped += ref[0].search_stats["chunks_state_capped"] > 0
            relaxations += stats["label_relaxations"]
    print(
        f"DIFFERENTIAL metis_history_bounded fixtures[{label}]: cells={n} pruned_bound={pruned} "
        f"m2_active={m2_active} ref_capped_cells={capped} label_relaxations={relaxations}"
    )
    assert pruned > 0
    if label in ("preset", "capped_off"):
        assert capped > 0  # the equality above was checked on capped chunks too
    if label == "off":
        assert m2_active > 0  # the gate opens where dominance is off and the frontier is ample


def test_metis_production_counters_equal_the_contracts_pruning_model() -> None:
    """Production and the contract's model (its `BoundedAllocator` in label mode, M2 behind its
    gate) skip the same relaxations: `pruned_bound` and the relaxations still counted agree."""
    compared = active = 0
    for _name, bundle, case in cells():
        for options in (PRESET, OFF):
            got, _ = run(MH_B, bundle, case, chunks=8, hops=3, options=options)
            block = block_of(got)
            if not block["p0"]:
                continue
            model = PC.run_chunks(
                bundle, case, chooser="labels", hops=3, chunks=8, bounds=PC.Bounds(bundle),
                options=options,
            )  # fmt: skip
            assert block["pruned_bound"] == model.alloc.gate.pruned_bound
            assert got.search_stats["label_relaxations"] == model.alloc.relaxations
            compared += 1
            active += block["m2"]["active"]
    assert compared >= 30 and active > 0


def test_m2_is_exact_behind_its_gate_and_saves_quotes_beyond_m1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain, chain_case = PC.chain_bundle()
    candidates: list[tuple[SnapshotBundle, Case, Mapping[str, Any]]] = [
        (chain, chain_case, PRESET),
        (chain, chain_case, OFF),
    ]
    candidates += [(b, c, OFF) for _n, b, c in cells()]
    runs = beyond_m1 = cost = 0
    for bundle, case, options in candidates:
        for chunks in (6, 24):
            ref = run(MH, bundle, case, chunks=chunks, options=options)
            m2 = run(MH_B, bundle, case, chunks=chunks, options=options)
            block = block_of(m2[0])
            assert_exact(ref, m2, names=(MH, MH_B))
            with monkeypatch.context() as patch:  # M1 only: the gate forced shut
                patch.setattr(chunk_pruning, "m2_gate", lambda *a, **k: ("closed:dominance", True))
                m1 = run(MH_B, bundle, case, chunks=chunks, options=options)
            assert not block_of(m1[0])["m2"]["active"] and block_of(m1[0])["bound_table_cost"] == 0
            assert_exact(ref, m1, names=(MH, MH_B))
            q = (m2[0].search_stats["quotes_executed"], m1[0].search_stats["quotes_executed"],
                 ref[0].search_stats["quotes_executed"])  # fmt: skip
            assert q[0] <= q[1] <= q[2]
            assert (
                m2[0].search_stats["label_relaxations"] <= ref[0].search_stats["label_relaxations"]
            )
            if block["m2"]["active"]:
                runs += 1
                cost += block["bound_table_cost"]
                beyond_m1 += q[0] < q[1]
    print(f"M2 behind the gate: runs={runs} fewer-quotes-than-M1={beyond_m1} table_cost={cost}")
    assert runs > 10 and beyond_m1 > 0 and cost > 0


def test_the_gate_opens_only_when_no_dominance_event_or_cap_can_occur() -> None:
    def gate(bundle: SnapshotBundle, case: Case, options: Mapping[str, Any]) -> tuple[str, bool]:
        index = build_graph_index(bundle)
        dist = chunk_pruning_dist(index, case)
        return chunk_pruning.m2_gate(index, case.token_in, case.token_out, 3, dist, options)

    def chunk_pruning_dist(index: Any, case: Case) -> dict[str, int]:
        from routing.algorithms import metis_inspired

        return metis_inspired.hops_to_target(index, case.token_in, case.token_out)

    chain, chain_case = PC.chain_bundle()
    assert gate(chain, chain_case, PRESET) == ("open", True)
    assert gate(chain, chain_case, OFF) == ("open", True)
    mixed, corpus = PC.fixture("routing/mantle_mixed"), PC.fixture("corpus/bundle")
    direct = next(c for c in corpus.cases if c.case_id.startswith("emp-09bc4e-779ded"))
    assert gate(corpus, direct, PRESET) == ("open", False)  # open, but no label exists
    parallel = next(c for c in corpus.cases if c.case_id.startswith("nod-09bc4e-c96de2"))
    assert gate(corpus, parallel, PRESET)[0] == "closed:dominance"
    assert gate(corpus, parallel, GENEROUS)[0] == "closed:dominance"
    assert gate(corpus, parallel, OFF)[0] == "open"
    assert gate(mixed, mixed.cases[0], PRESET)[0] == "closed:dominance"
    graph = PC.fixture("routing/cpmm_graph")
    multi = next(c for c in graph.cases if c.case_id == "a_d_multi_hop")
    assert gate(graph, multi, CAPPED_OFF)[0] == "closed:frontier"  # a frontier of 3 can bind


def test_frontier_cap_interaction_m1_is_exact_and_the_gate_keeps_m2_off() -> None:
    """The reference fills the 3-slot frontier on `a_d_multi_hop`; the bounded run reproduces it
    -- same drops, same `state_cap` termination -- because the gate is closed (§6.4)."""
    graph = PC.fixture("routing/cpmm_graph")
    case = next(c for c in graph.cases if c.case_id == "a_d_multi_hop")
    kw: dict[str, Any] = {"chunks": 24, "hops": 2, "label_hops": 3, "options": CAPPED_OFF}
    ref, got = run(MH, graph, case, **kw), run(MH_B, graph, case, **kw)
    assert ref[0].search_stats["labels_dropped_frontier_cap"] > 0
    assert ref[0].search_stats["termination"] == "state_cap"
    assert_exact(ref, got, names=(MH, MH_B))
    block = block_of(got[0])
    assert block["m2"] == {"active": False, "gate": "closed:frontier"} and block["rule"] == "M1"
    for key in ("labels_dropped_frontier_cap", "chunks_state_capped", "termination"):
        assert got[0].search_stats[key] == ref[0].search_stats[key]
    assert block["exactness"] == {"label": "exact", "binding": []}  # a cap is not a budget


def test_negative_control_forcing_m2_past_a_binding_cap_changes_the_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract §6.4 on the production solver: with the gate forced open the label population
    changes, the frontier never fills and a better-than-reference plan appears -- not the
    reference (gross 20 600 vs 20 611). This is why M2 exists only behind `G_M2`."""
    graph = PC.fixture("routing/cpmm_graph")
    case = next(c for c in graph.cases if c.case_id == "a_d_multi_hop")
    kw: dict[str, Any] = {"chunks": 24, "hops": 2, "label_hops": 3, "options": CAPPED_OFF}
    ref = run(MH, graph, case, **kw)
    monkeypatch.setattr(chunk_pruning, "m2_gate", lambda *a, **k: ("open", True))
    forced = run(MH_B, graph, case, **kw)
    assert block_of(forced[0])["m2"]["active"] is True and block_of(forced[0])["pruned_bound"] > 0
    assert ref[0].evaluation is not None and forced[0].evaluation is not None
    assert forced[0].evaluation.gross_output != ref[0].evaluation.gross_output
    print(f"NEGATIVE CONTROL forced M2: {ref[0].evaluation.gross_output} -> "
          f"{forced[0].evaluation.gross_output}")  # fmt: skip


def test_the_u_table_equals_the_contracts_model_and_the_walk_maximum() -> None:
    compared = 0
    for _name, bundle, case in cells():
        index = build_graph_index(bundle)
        table = bounds_module.build_bounds(bundle)
        mine = chunk_pruning.UTable(index, case.token_in, case.token_out, 3, table.bounds)
        model = PC.UTable(index, case.token_in, case.token_out, 3, PC.Bounds(bundle))
        assert mine.cost == model.cost
        assert set(mine.entries) == set(model.entries)
        for key, entry in mine.entries.items():
            other = model.entries[key]
            if entry is None or other is None:
                assert entry is None and other is None, key
            else:
                assert (entry.r, entry.s, entry.p, entry.t, entry.reach) == (
                    other.r, other.s, other.p, other.t, other.reach)  # fmt: skip
        compared += 1
    assert compared == len(list(cells()))


# ======================================================================================
# parameters off, factories, selection
# ======================================================================================


@pytest.mark.parametrize("module", [incremental_graph, metis_history])
def test_the_parameter_is_default_off_keyword_only_and_the_reference_never_sees_it(
    module: ModuleType,
) -> None:
    parameter = inspect.signature(module.solve).parameters["bound_pruning"]
    assert parameter.default is False and parameter.kind is inspect.Parameter.KEYWORD_ONLY
    factory = module.FACTORY
    assert factory.solve is module.solve and factory.prepare is module.prepare
    bundle = PC.fixture("routing/cpmm_graph")
    case = bundle.cases[0]
    name = module.NAME
    params = {"max_hops": 2, "max_splits": 2, "percent_step": 25, "chunks": 6}
    if module is metis_history:
        params["label_hops"] = 3
    options = dict(PRESET) if module is metis_history else {}
    prepared = factory.prepare(bundle, AlgorithmConfig(name, params, options))
    assert type(prepared) in (
        incremental_graph.PreparedIncrementalGraph,
        metis_history.PreparedMetisHistory,
    )
    assert not hasattr(prepared, "bound_table")  # no extra object, no subclass
    result = module.solve(case, SolveContext(bundle, gross_only(), prepared), Budget())
    assert "bound_pruning" not in result.search_stats
    assert "bound_pruning" not in json.dumps(result.to_dict())
    with pytest.raises(TypeError, match=r"PreparedBounded"):
        module.solve(
            case, SolveContext(bundle, gross_only(), prepared), Budget(), bound_pruning=True
        )
    with pytest.raises(TypeError, match=r"Prepared"):
        module.solve(case, SolveContext(bundle, gross_only(), None), Budget())


def test_bound_pruning_together_with_graph_reuse_is_refused() -> None:
    bundle = PC.fixture("routing/cpmm_graph")
    prepared = incremental_graph.prepare_bounded(
        bundle,
        AlgorithmConfig(IG_B, {"max_hops": 2, "max_splits": 2, "percent_step": 25, "chunks": 4}),
    )
    context = SolveContext(bundle, gross_only(), prepared)
    with pytest.raises(ValueError, match="bound_pruning and graph_reuse"):
        incremental_graph.solve(
            bundle.cases[0], context, Budget(), bound_pruning=True, graph_reuse=True
        )
    # the two default-off switches stay independently usable
    assert incremental_graph.solve(bundle.cases[0], context, Budget(), graph_reuse=True).status


def test_registered_as_custom_opt_in_identities_with_the_references_capabilities() -> None:
    assert R022_ADDITIONS == ("single_path_bounded", IG_B, MH_B) and len(R022_ADDITIONS) == 3
    assert not set(R022_ADDITIONS) & set(R021_ADDITIONS)
    for name, ref in ((IG_B, IG), (MH_B, MH)):
        factory, reference = ALGORITHMS[name], ALGORITHMS[ref]
        assert factory.name == name
        assert factory.capabilities == reference.capabilities
        assert (factory.search_params, factory.graph_params) == (
            reference.search_params, reference.graph_params)  # fmt: skip
        assert name not in BASE_STRATEGIES and name not in OPTIMIZED_STRATEGIES
        assert strategy_group(name) == "custom"
        prov = dict(factory.provenance or {})
        assert prov["experimental"] is True and prov["opt_in"] is True
        assert prov["reference"] == ref and prov["issue"] == "WHI-1600"
        assert prov["identity"] == (
            f"exact acceleration of {ref}; identical plan under non-binding budgets; "
            "not a new heuristic"
        )
        assert (
            "R022-Q02/1" in prov["contract"]
            and prov["claims"]
            and "any speedup" in prov["not_claimed"]
        )
        json.dumps(prov)  # recorded in the resolved profile
    assert ALGORITHMS[IG_B].options_validator is None and ALGORITHMS[IG_B].options_preset is None
    with pytest.raises(OptionsError, match="accepts no algorithm_options"):
        ALGORITHMS[IG_B].prepare(  # type: ignore[misc]
            PC.synthetic(PC.cp("p", "A", "B", 10, 10)),
            AlgorithmConfig(
                IG_B, {"max_hops": 2, "max_splits": 2, "percent_step": 25, "chunks": 2}, {"x": 1}
            ),
        )


def test_metis_bounded_takes_exactly_the_references_options_with_the_same_settings_hash() -> None:
    """Contract §10.4 and open question §12.3(1): the preset is the reference's options under the
    bounded strategy's own key (a preset file names the algorithm that pins it), so the
    `settings_sha256` of the options is `metis_history`'s -- `183bb1ff…`."""
    ref, bnd = ALGORITHMS[MH], ALGORITHMS[MH_B]
    assert bnd.options_validator is ref.options_validator
    assert preset_options(bnd) == preset_options(ref) == PRESET
    assert settings_sha256(preset_options(bnd)) == settings_sha256(preset_options(ref))
    assert settings_sha256(preset_options(ref)).startswith("183bb1ff")
    assert dict(bnd.options_preset or {})["path"] == "config/metis_history_bounded/preset_v1.yaml"
    assert dict(bnd.options_preset or {})["key"] != dict(ref.options_preset or {})["key"]
    doc = yaml.safe_load((REPO / "config/metis_history_bounded/preset_v1.yaml").read_text())
    assert (
        doc["algorithm"] == MH_B
        and doc["options"]
        == yaml.safe_load((REPO / "config/metis_history/preset_v1.yaml").read_text())["options"]
    )
    for bad, match in (({**PRESET, "dominance": "weak"}, "dominance"),
                       ({**PRESET, "max_frontier_labels": 0}, "max_frontier_labels"),
                       ({"dominance": "history"}, "missing"),
                       ({**PRESET, "chunks": 3}, "chunks")):  # fmt: skip
        with pytest.raises(OptionsError, match=match):
            bnd.prepare(  # type: ignore[misc]
                PC.synthetic(PC.cp("p", "A", "B", 10, 10)),
                AlgorithmConfig(
                    MH_B,
                    {
                        "max_hops": 2,
                        "max_splits": 2,
                        "percent_step": 25,
                        "chunks": 2,
                        "label_hops": 3,
                    },
                    bad,
                ),
            )


def test_the_factories_are_picklable_by_reference_for_the_worker() -> None:
    for module, prepare in (
        (incremental_graph_bounded, incremental_graph.prepare_bounded),
        (metis_history_bounded, metis_history.prepare_bounded),
    ):
        clone = pickle.loads(pickle.dumps(module.FACTORY))
        assert clone == module.FACTORY
        assert clone.solve is module.solve and clone.prepare is prepare
        for fn in (module.solve, prepare):
            assert "<lambda>" not in fn.__qualname__ and "<locals>" not in fn.__qualname__


def test_all_appends_them_after_single_path_bounded_and_profile_replays_literally() -> None:
    source = yaml.safe_load((REPO / "config" / "daily_gross.yaml").read_text())
    document, profile = derive(source, "all", source_path="s", source_sha256="x")
    names = list(profile.algorithms)
    assert names[-3:] == ["single_path_bounded", IG_B, MH_B] and len(names) == 17
    assert names[-3 - len(R021_ADDITIONS) : -3] == list(R021_ADDITIONS)
    assert document["selection"]["groups"]["custom"][-2:] == [IG_B, MH_B]
    options = document["algorithm_options"]
    assert IG_B not in options and options[MH_B] == options[MH] == PRESET
    entry = profile.algorithm_options[MH_B]
    assert entry["source"]["kind"] == "preset" and entry["source"]["key"] == (
        "R022-Q04-metis_history_bounded"
    )
    assert entry["settings_sha256"] == profile.algorithm_options[MH]["settings_sha256"]
    config = profile.resolved()["algorithm_config"]
    assert config[IG_B]["provenance"]["reference"] == IG
    assert config[MH_B]["params"] == config[MH]["params"]  # same chunks / label_hops / search
    assert config[IG_B]["params"] == config[IG]["params"]
    for mode in ("base", "optimized"):
        got = derive(source, mode, source_path="s", source_sha256="x")[1].algorithms
        assert IG_B not in got and MH_B not in got
    saved = json.loads(json.dumps(document))  # an earlier saved profile never gains them
    for name in (IG_B, MH_B):
        saved["algorithms"].remove(name)
        saved["selection"]["groups"]["custom"].remove(name)
    saved["algorithm_options"].pop(MH_B)
    literal, replayed = derive(saved, "profile", source_path="s", source_sha256="x")
    assert literal == saved and IG_B not in replayed.algorithms and MH_B not in replayed.algorithms


def test_the_solvers_use_the_shared_helper_modules_and_hold_no_copy_of_the_formulae() -> None:
    assert vars(incremental_graph)["build_bounds"] is bounds_module.build_bounds
    assert vars(metis_history)["build_bounds"] is bounds_module.build_bounds
    assert vars(chunk_pruning)["CHUNK_DOMAIN_MAX"] is bounds_module.CHUNK_DOMAIN_MAX
    for module in (chunk_pruning, incremental_graph, metis_history, incremental_graph_bounded,
                   metis_history_bounded):  # fmt: skip
        text = Path(inspect.getfile(module)).read_text()
        code = re.sub(r'""".*?"""', "", text, flags=re.S)
        for formula in ("_Q192", "get_sqrt_ratio_at_tick", "get_price_from_id", "FEE_DENOMINATOR",
                        "liquidity_gross", "PRECISION"):  # fmt: skip
            assert formula not in code, (module.__name__, formula)


# ======================================================================================
# CLI: run, quote --details, replay, order-check, report
# ======================================================================================


def _chunk_profile(tmp_path: Path) -> Path:
    doc = read_profile_document(REPO / "config" / "daily_gross.yaml")
    doc["algorithms"] = [IG, IG_B, MH, MH_B]
    doc["search"] = {"max_hops": 3, "max_splits": 2, "percent_step": 25}
    doc["graph"] = {"chunks": 6, "label_hops": 3}
    doc["algorithm_options"] = {MH: dict(PRESET), MH_B: dict(PRESET)}
    doc["measurement"] = {"warmup": 0, "repeats": 1, "seed": 7, "order": "fixed",
                          "memory_pass": False}  # fmt: skip
    path = tmp_path / "chunks.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    parse_profile(doc, str(path))
    return path


def test_cli_run_shows_each_strategy_separately_with_counters_and_replays_exactly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = _chunk_profile(tmp_path)
    results = tmp_path / "results"
    argv = ["run", "--bundle", str(MIXED), "--profile", str(profile), "--results-dir",
            str(results), "--strategies", "profile"]  # fmt: skip
    assert main.main(argv) == 0
    (run_dir,) = results.iterdir()
    manifest = load_manifest(run_dir)
    assert list(manifest.algorithms) == [IG, IG_B, MH, MH_B]
    by: dict[str, list[dict[str, Any]]] = {name: [] for name in manifest.algorithms}
    for record in load_case_records(run_dir):
        by[record["algorithm"]].append(record)
    assert len({len(v) for v in by.values()}) == 1 and by[IG]
    pruned = {IG_B: 0, MH_B: 0}
    for ref_name, bnd_name in ((IG, IG_B), (MH, MH_B)):
        for ref_record, record in zip(by[ref_name], by[bnd_name], strict=True):
            assert ref_record["case_id"] == record["case_id"]
            assert "bound_pruning" not in ref_record["search"]
            assert record["status"] == ref_record["status"]
            assert record["score"] == ref_record["score"]
            assert record["evaluation"] == ref_record["evaluation"]
            if record["solver_reported"] is not None and record["status"] == "ok":
                block = record["search"]["bound_pruning"]
                assert block["contract"] == "R022-Q02/1" and block["reference"] == ref_name
                assert block["exactness"]["label"] == "exact" and "p0" in block
                assert record["quotes"]["counted"] <= ref_record["quotes"]["counted"]
                pruned[bnd_name] += block["pruned_bound"]
    assert pruned[IG_B] > 0 and pruned[MH_B] > 0
    resolved = manifest.resolved_profile
    assert resolved["algorithm_config"][IG_B]["provenance"]["reference"] == IG
    assert (
        resolved["algorithm_options"][MH_B]["settings_sha256"]
        == (resolved["algorithm_options"][MH]["settings_sha256"])
    )
    capsys.readouterr()
    replay = manifest.replay_command.split("main.py", 1)[1].split()
    assert replay[-2:] == ["--strategies", "profile"]
    assert main.main(replay) == 0
    again = [p for p in results.iterdir() if p != run_dir]
    assert len(again) == 1
    assert load_manifest(again[0]).resolved_profile == manifest.resolved_profile
    assert compare_runs(run_dir, again[0]) == []  # `search` (counters included) replays literally
    assert main.main(["order-check", str(run_dir), str(again[0])]) == 0
    out = tmp_path / "report"
    assert main.main(["report", str(run_dir), "--output", str(out)]) == 0
    html = (out / "report.html").read_text()
    for name in (IG_B, MH_B):
        assert f"<code>{name}</code>" in html
    assert html.count("Bound-pruned strategy") >= 2


def test_cli_quote_details_runs_one_solve_per_strategy_and_prints_the_counters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = _chunk_profile(tmp_path)
    args = ["quote", "--bundle", str(TRACKED_CORPUS), "--profile", str(profile), "--token-in",
            "USDC", "--token-out", "USDT0", "--amount", "1500.25", "--quotes-dir",
            str(tmp_path / "q"), "--strategies", "profile", "--details"]  # fmt: skip
    assert main.main(args) == 0
    out = capsys.readouterr().out
    match = re.search(r"\(run (\S+)\)", out)
    assert match
    records = load_case_records(Path(match.group(1)))
    assert [r["algorithm"] for r in records] == [IG, IG_B, MH, MH_B]  # one solve each
    for name, rule in ((IG_B, "I1"), (MH_B, "M1")):
        assert f"[{name}] ok" in out
        section = out.split(f"[{name}] ok", 1)[1].split("\n[", 1)[0]
        assert f"bound pruning (R022-Q02/1 rule {rule}" in section
        assert "exact acceleration of" in section and "pruned by bound" in section
        assert "retained simpler candidate present (P0): True" in section
        assert "exactness: exact" in section
        block = next(r for r in records if r["algorithm"] == name)["search"]["bound_pruning"]
        assert f"pruned by bound {block['pruned_bound']} (not failures)" in section
    assert "rule M2 active" in out.split(f"[{MH_B}] ok", 1)[1]
    for name in (IG, MH):
        reference = out.split(f"[{name}] ok", 1)[1].split("\n[", 1)[0]
        assert "bound pruning" not in reference


# ======================================================================================
# the tracked real fixture and the tuning split: every case, non-binding budgets
# ======================================================================================

_PARAMS = {"max_hops": 3, "max_splits": 2, "percent_step": 25, "chunks": 8, "label_hops": 3}


_SHARED: dict[str, Any] = {}  # read by forked workers (set before the pool starts, never pickled)


def _compare_case(index: int) -> tuple[int, int, int, int, int, int, int, int]:
    """One case: the bounded strategy equals the reference exactly (raises otherwise)."""
    shared = _SHARED
    pair, bundle, objective = shared["pair"], shared["bundle"], shared["objective"]
    case = bundle.cases[index]
    kw: dict[str, Any] = {"chunks": 8, "objective": objective}
    ref = run(pair[0], bundle, case, prepared=shared["prepared_ref"], **kw)
    got = run(pair[1], bundle, case, prepared=shared["prepared_bnd"], **kw)
    block = block_of(got[0])
    assert_exact(ref, got, names=pair)
    assert block["exactness"] == {"label": "exact", "binding": []}
    key = "paths_scored" if pair[0] == IG else "label_relaxations"
    return (
        ref[0].status is SolveStatus.OK,
        block["pruned_bound"],
        block["bound_no_bound"],
        bool(block.get("m2", {}).get("active")),
        ref[0].search_stats["quotes_executed"],
        got[0].search_stats["quotes_executed"],
        ref[0].search_stats[key],
        got[0].search_stats[key],
    )


def _differential_over(
    pair: tuple[str, str],
    bundle: SnapshotBundle,
    label: str,
    objective: ObjectiveContext | None = None,
    options: Mapping[str, Any] | None = None,
    *,
    parallel: bool = False,
    expect_m2: bool = False,
) -> None:
    """Every case of `bundle` equals the reference. `parallel` shards the cases over forked
    processes (the tuning split is ~10 minutes of pure-Python search single-threaded); an
    assertion in a worker is re-raised here."""
    ref_name, bnd_name = pair
    metis = pair == (MH, MH_B)
    options = dict(options or PRESET) if metis else {}
    params = _PARAMS if metis else {k: v for k, v in _PARAMS.items() if k != "label_hops"}
    prepared_ref = ALGORITHMS[ref_name].prepare(  # type: ignore[misc]
        bundle, AlgorithmConfig(ref_name, params, options)
    )
    prepared_bnd = ALGORITHMS[bnd_name].prepare(  # type: ignore[misc]
        bundle, AlgorithmConfig(bnd_name, params, options)
    )
    _SHARED.update(
        pair=pair, bundle=bundle, objective=objective, prepared_ref=prepared_ref,
        prepared_bnd=prepared_bnd,
    )  # fmt: skip
    indexes = range(len(bundle.cases))
    if parallel and "fork" in multiprocessing.get_all_start_methods():
        workers = max(1, min(8, (os.cpu_count() or 2) - 1))
        with ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("fork")
        ) as pool:
            rows = list(pool.map(_compare_case, indexes, chunksize=2))
    else:
        rows = [_compare_case(i) for i in indexes]
    ok, pruned, no_bound, m2_active, q_ref, q_bnd, w_ref, w_bnd = (
        sum(c) for c in zip(*rows, strict=True)
    )
    unit = "paths_scored" if ref_name == IG else "label_relaxations"
    print(
        f"DIFFERENTIAL {bnd_name} {label}: cases={len(rows)} ok={ok} pruned_bound={pruned} "
        f"no_bound_evals={no_bound} m2_active_cases={m2_active} quotes {q_ref} -> {q_bnd} "
        f"({q_bnd - q_ref:+d}) {unit} {w_ref} -> {w_bnd}"
    )
    assert len(rows) == len(bundle.cases) > 0 and ok > 0
    assert pruned > 0 and q_bnd <= q_ref
    assert (m2_active > 0) == expect_m2


@pytest.mark.parametrize("pair", [(IG, IG_B), (MH, MH_B)], ids=[IG_B, MH_B])
def test_every_case_of_the_tracked_real_fixture_equals_the_reference(pair: tuple[str, str]) -> None:
    bundle = load_bundle(TRACKED_CORPUS)
    assert len(bundle.cases) == 96
    _differential_over(pair, bundle, "tracked-fixture")


@pytest.mark.parametrize("pair", [(IG, IG_B), (MH, MH_B)], ids=[IG_B, MH_B])
def test_every_tuning_split_case_equals_the_reference_under_non_binding_budgets(
    pair: tuple[str, str],
) -> None:
    path = os.environ.get(TUNING_ENV)
    if not path:
        pytest.skip(f"set {TUNING_ENV} to the frozen bundle_tuning (data/ is primary-clone only)")
    assert Path(path).is_dir(), f"{TUNING_ENV}={path!r} is not a directory"
    bundle = load_bundle(path)
    assert len(bundle.cases) == 96 and len(bundle.pools) == 143
    _differential_over(pair, bundle, "tuning-split", parallel=True)


def test_bound_table_is_the_helpers_and_immutable() -> None:
    bundle = PC.fixture("corpus/bundle")
    prepared = incremental_graph.prepare_bounded(
        bundle,
        AlgorithmConfig(IG_B, {"max_hops": 3, "max_splits": 2, "percent_step": 25, "chunks": 4}),
    )
    assert isinstance(prepared.bound_table, BoundTable)
    with pytest.raises(TypeError):
        prepared.bound_table.bounds[("x", "y")] = None  # type: ignore[index]
    got, _ = run(IG_B, bundle, bundle.cases[0], chunks=4, prepared=prepared)
    assert block_of(got)["prepare"] == prepared.bound_table.prepare_record()


def test_rule_m2_is_exact_on_every_case_of_the_tracked_real_fixture_with_dominance_off() -> None:
    """`dominance: off` with an ample frontier is where `G_M2` opens for requests with parallel
    routes (the WHI-1602 arm A4): the label-pruning rule acts on real pool states and the plan
    is still the reference's on every case."""
    bundle = load_bundle(TRACKED_CORPUS)
    _differential_over(
        (MH, MH_B), bundle, "tracked-fixture dominance-off", options=OFF, expect_m2=True
    )


def test_rule_m2_is_exact_on_every_tuning_split_case_with_dominance_off() -> None:
    path = os.environ.get(TUNING_ENV)
    if not path:
        pytest.skip(f"set {TUNING_ENV} to the frozen bundle_tuning (data/ is primary-clone only)")
    bundle = load_bundle(path)
    assert len(bundle.cases) == 96 and len(bundle.pools) == 143
    _differential_over(
        (MH, MH_B), bundle, "tuning-split dominance-off", options=OFF, parallel=True, expect_m2=True
    )

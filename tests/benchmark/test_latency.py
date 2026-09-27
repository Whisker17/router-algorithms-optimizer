"""L01 latency driver and summaries (WHI-1503).

Boundary tests put the same artificial work in `prepare` or in `solve`, as burned CPU or as
sleep, plus a delayed parent-side evaluation, and check that each cost is charged to the
stage that incurred it -- so an optimization cannot make work disappear by moving it across
a timing boundary. An end-to-end run over the checked-in corpus fixture exercises every
stage, including `main.py quote` (still exactly one solve per algorithm per invocation).
"""

from __future__ import annotations

import dataclasses
import json
import multiprocessing
import os
import pickle
import re
import signal
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import FrameType, SimpleNamespace
from typing import Any

import fake_solvers  # sibling module: importable by spawned workers via sys.path
import pytest
import yaml

import benchmark.latency as latency
import benchmark.runner as runner
from benchmark.objective import gross_only
from benchmark.profile import MeasurementSettings, RunProfile, WorkerSettings
from benchmark.results import load_case_records, load_manifest, load_memory_records
from benchmark.runner import compare_runs
from report import latency as report
from routing.algorithms.base import Budget
from routing.algorithms.registry import get_algorithm
from snapshot.bundle import load_bundle, sha256_file
from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "corpus" / "bundle"
D = fake_solvers.DELAY_SECONDS
EVAL_DELAY = 0.2
TEST_ALARM_SECONDS = 240


@pytest.fixture(autouse=True)
def _bounded_test() -> Iterator[None]:
    def _alarm(signum: int, frame: FrameType | None) -> None:
        raise TimeoutError(f"test exceeded {TEST_ALARM_SECONDS}s")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(TEST_ALARM_SECONDS)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        for child in multiprocessing.active_children():
            child.kill()


@pytest.fixture(autouse=True)
def _register_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    import routing.algorithms.registry as registry

    for factory in fake_solvers.ALL:
        monkeypatch.setitem(registry.ALGORITHMS, factory.name, factory)


def _bundle() -> SnapshotBundle:
    pool = ConstantProductPoolState(
        pool_id="pool_a", token0="TKA", token1="TKB", reserve0=10**6, reserve1=3 * 10**6,
        fee_bps=30,
    )  # fmt: skip
    return SnapshotBundle(
        bundle_id="b1",
        kind="synthetic",
        schema_version=1,
        block=BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0),
        pools={"pool_a": pool},
        cases=tuple(Case(f"c{i}", "TKA", "TKB", 100_000 + i) for i in range(2)),
        bundle_hash="deadbeef",
        source_path="<test>",
    )


def _profile(*algorithms: str) -> RunProfile:
    return RunProfile(
        schema_version=2,
        algorithms=algorithms,
        objective=gross_only(),
        source_path="p.yaml",
        budget=Budget(time_limit_seconds=10, max_quotes=None, max_candidates=None),
        measurement=MeasurementSettings(
            warmup=0, repeats=1, seed=7, order="fixed", memory_pass=False
        ),
        worker=WorkerSettings(
            start_method="spawn", scope="algorithm", prepare_time_limit_seconds=10
        ),  # fmt: skip
    )


def _measure(tmp_path: Path, *algorithms: str, run_id: str = "r", **kw: Any) -> Path:
    options: dict[str, Any] = dict(order="fixed", warmup=1, repeats=2, scope="algorithm",
                                   memory=False)  # fmt: skip
    options.update(kw)
    manifest = latency.measure_run(
        _bundle(), _profile(*algorithms), results_dir=tmp_path, run_id=run_id,
        stage="test", replay_command="cmd", **options,
    )  # fmt: skip
    return Path(manifest.run_dir)


# ------------------------------------------------------------ timing boundaries


def test_each_cost_is_charged_to_the_stage_that_incurred_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_evaluate = runner.evaluate  # type: ignore[attr-defined]

    def slow_evaluate(*args: Any, **kwargs: Any) -> Any:
        time.sleep(EVAL_DELAY)
        return real_evaluate(*args, **kwargs)

    monkeypatch.setattr(runner, "evaluate", slow_evaluate)  # parent-side final evaluation
    run_dir = _measure(tmp_path, "delay_in_prepare", "delay_in_solve", "sleep_in_solve")
    manifest = load_manifest(run_dir)
    prepare = {e["algorithm"]: e["prepare_seconds"] for e in manifest.prepare_events}
    records = load_case_records(run_dir)
    assert {r["status"] for r in records} == {"ok"}
    charged: dict[str, float] = {}
    for record in records:
        m, name = record["measurement"], record["algorithm"]
        # The warmup attempt is recorded but excluded from the measured samples.
        assert [a["phase"] for a in m["attempts"]] == ["warmup", "measured", "measured"]
        assert len(m["solve_seconds"]) == len(m["solve_cpu_seconds"]) == 2
        for attempt in m["attempts"]:
            assert attempt["elapsed_seconds"] >= attempt["solve_wall_seconds"]
            assert attempt["transport_seconds"] >= 0
            assert attempt["solve_cpu_seconds"] <= attempt["solve_wall_seconds"] + 0.05
        # The parent's independent evaluation is its own stage, never solve latency.
        assert m["evaluation_seconds"] >= EVAL_DELAY
        walls, cpus = m["solve_seconds"], m["solve_cpu_seconds"]
        if name == "delay_in_prepare":
            assert max(walls) < D / 2
        elif name == "delay_in_solve":
            assert min(walls) >= D and min(cpus) >= D * 0.95  # burned CPU is CPU time
            assert prepare[name] < D / 2
        else:
            assert min(walls) >= D and max(cpus) < D / 2  # sleeping is wall, not CPU
        charged[name] = charged.get(name, prepare[name]) + sum(walls)
    assert prepare["delay_in_prepare"] >= D
    # Moving the work between prepare and solve never makes it vanish from the record.
    assert charged["delay_in_prepare"] >= D and charged["delay_in_solve"] >= D


def test_cold_scope_prepares_per_case_and_memory_is_a_separate_pass(tmp_path: Path) -> None:
    run_dir = _measure(tmp_path, "delay_in_prepare", warmup=0, repeats=1, scope="case",
                       memory=True)  # fmt: skip
    manifest = load_manifest(run_dir)
    passes = [(e["pass"], e["reason"]) for e in manifest.prepare_events]
    assert passes == [("timing", "initial"), ("timing", "per_case")] + [
        ("memory", "initial"),
        ("memory", "per_case"),
    ]
    assert all(e["prepare_seconds"] >= D for e in manifest.prepare_events)
    assert all(r["measurement"]["instrumented"] is False for r in load_case_records(run_dir))
    memory = load_memory_records(run_dir)
    assert [m["status"] for m in memory] == ["measured", "measured"]
    assert all(m["solve_peak_bytes"] is not None for m in memory)


def test_reverse_reverses_the_whole_schedule_without_changing_outputs(tmp_path: Path) -> None:
    fixed = _measure(tmp_path, "direct", "sleep_in_solve", run_id="f", warmup=0, repeats=1)
    reverse = _measure(tmp_path, "direct", "sleep_in_solve", run_id="r", order="reverse",
                       warmup=0, repeats=1)  # fmt: skip
    schedule = load_manifest(fixed).measurement["schedule"]
    assert load_manifest(reverse).measurement["schedule"] == list(reversed(schedule))
    assert [r["algorithm"] for r in load_case_records(reverse)][0] == "sleep_in_solve"
    assert compare_runs(fixed, reverse) == []


# ------------------------------------------------------------ protocol / identity


def _write_profile(tmp_path: Path) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(
        "schema_version: 2\nalgorithms: [direct, single_path]\nobjective: {mode: gross_only}\n"
        "search: {max_hops: 2}\n"
        "budget: {time_limit_seconds: 60, max_quotes: 50000, max_candidates: null}\n"
        "measurement: {warmup: 2, repeats: 3, seed: 1, order: fixed, memory_pass: true}\n"
        "worker: {start_method: spawn, scope: algorithm, prepare_time_limit_seconds: 60}\n"
    )
    return path


def _protocol_doc(tmp_path: Path) -> dict[str, Any]:
    fixture = load_bundle(FIXTURE)
    doc: dict[str, Any] = yaml.safe_load((REPO / "config" / "latency" / "l01.yaml").read_text())
    profile = _write_profile(tmp_path)
    doc["parent_bundle"] = {"bundle_id": fixture.bundle_id, "bundle_hash": fixture.bundle_hash}
    doc["profile"] = {"path": str(profile), "sha256": sha256_file(profile)}
    doc["sentinel"] = {"token_in": "USDC", "token_out": "USDT0", "amount": "1500.25"}
    doc["matrix"] = [
        {"case": "emp-09bc4e-779ded-low-2", "split": "tuning", "covers": ["small"]},
        {"case": "emp-09bc4e-779ded-low-1", "split": "held_out", "covers": ["small"]},
        {"case": "nod-09bc4e-c96de2-medium-1", "split": "held_out", "covers": ["no_route"]},
    ]
    doc["timing"] = {"warmup": 1, "repeats": 2, "orders": ["fixed", "reverse"]}
    doc["quote_cli"] = {"invocations": 1}
    return doc


def _write_protocol(tmp_path: Path, doc: dict[str, Any]) -> Path:
    path = tmp_path / "protocol.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def test_checked_in_protocol_is_valid_and_pins_the_frozen_inputs() -> None:
    protocol = latency.load_protocol(REPO / "config" / "latency" / "l01.yaml")
    assert protocol.key == "L01" and protocol.cohorts == ("full_source", "sor_compatible")
    assert sha256_file(REPO / protocol.profile_path) == protocol.profile_sha256
    assert {m.split for m in protocol.matrix} == {"tuning", "held_out"}
    assert protocol.acceptance["heuristic_default_loss_tolerance"] is None
    assert protocol.warmup == 1 and protocol.repeats == 5 and protocol.orders == ORDERS


ORDERS = ("fixed", "reverse")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(extra=1), "unknown"),
        (lambda d: d["acceptance"].update(heuristic_default_loss_tolerance=5), "must stay null"),
        (lambda d: d["timing"].update(orders=["fixed", "shuffle"]), "not within"),
        (lambda d: d["matrix"][0].update(split="dev"), "split"),
    ],
)
def test_protocol_validation_refuses_malformed_documents(
    tmp_path: Path, mutate: Any, message: str
) -> None:
    doc = _protocol_doc(tmp_path)
    mutate(doc)
    with pytest.raises(latency.LatencyError, match=message):
        latency.load_protocol(_write_protocol(tmp_path, doc))


def test_inputs_must_match_the_protocol_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = _protocol_doc(tmp_path)
    doc["matrix"][1]["split"] = "tuning"  # a held-out corpus case declared as tuning
    with pytest.raises(latency.LatencyError, match="corpus split"):
        latency.build_bundles(
            latency.load_protocol(_write_protocol(tmp_path, doc)), load_bundle(FIXTURE),
            tmp_path / "b1",
        )  # fmt: skip
    doc = _protocol_doc(tmp_path)
    doc["parent_bundle"]["bundle_hash"] = "0" * 64
    with pytest.raises(latency.LatencyError, match="parent bundle hash"):
        latency.build_bundles(
            latency.load_protocol(_write_protocol(tmp_path, doc)), load_bundle(FIXTURE),
            tmp_path / "b2",
        )  # fmt: skip
    doc = _protocol_doc(tmp_path)
    doc["profile"]["sha256"] = "0" * 64
    with pytest.raises(latency.LatencyError, match="sha256 differs"):
        latency.run_latency_experiment(
            _write_protocol(tmp_path, doc), str(FIXTURE), tmp_path / "out", allow_dirty=True
        )
    monkeypatch.setattr(
        latency, "source_identity", lambda: {"git_dirty": True, "dirty_paths": ["?? x"]}
    )
    with pytest.raises(latency.LatencyError, match="dirty"):
        latency.run_latency_experiment(
            _write_protocol(tmp_path, _protocol_doc(tmp_path)), str(FIXTURE), tmp_path / "o2"
        )
    assert not (tmp_path / "out").exists() and not (tmp_path / "o2").exists()


# ------------------------------------------------------------ end to end


def test_experiment_over_the_corpus_fixture_records_every_stage(tmp_path: Path) -> None:
    protocol = _write_protocol(tmp_path, _protocol_doc(tmp_path))
    out = latency.run_latency_experiment(
        protocol, str(FIXTURE), tmp_path / "latency", allow_dirty=True, echo=lambda _: None
    )
    doc = json.loads((out / "experiment.json").read_text())
    assert doc["state"] == "complete" and doc["partial"] is False
    assert doc["protocol"]["sha256"] == sha256_file(protocol)
    assert doc["source"]["git_revision"] and "code_tree_ids" in doc["source"]
    assert doc["load"]["samples"] > 0 and "contaminated" in doc["load"]
    # timing: 2 orders x 2 cohorts x (matrix, sentinel); cold: full-source matrix + sentinel
    assert [(r["stage"], r["order"], r["bundle"]) for r in doc["runs"]] == [
        ("timing", "fixed", "full_source/matrix"),
        ("timing", "fixed", "full_source/sentinel"),
        ("timing", "fixed", "sor_compatible/matrix"),
        ("timing", "fixed", "sor_compatible/sentinel"),
        ("timing", "reverse", "sor_compatible/sentinel"),
        ("timing", "reverse", "sor_compatible/matrix"),
        ("timing", "reverse", "full_source/sentinel"),
        ("timing", "reverse", "full_source/matrix"),
        ("cold", "fixed", "full_source/matrix"),
        ("cold", "fixed", "full_source/sentinel"),
    ]
    assert doc["algorithms"] == ["direct", "single_path"]
    summary = report.summarize(out)
    assert summary["coverage_problems"] == []
    assert summary["internal_checks"] == {
        "cross_order": [], "cold_warm": [], "attempts_inconsistent": []
    }  # fmt: skip
    # The report names its own generating source beside the measured one.
    assert summary["experiment"]["measured_source"] == doc["source"]
    assert summary["report"]["git_revision"] and summary["report"]["generated_at"]
    semantic = summary["semantic"]["full_source/matrix"]
    for run in ("timing fixed", "timing reverse", "cold fixed"):  # failure kept in every run
        counts = semantic["status_counts"][f"{run} full_source/matrix"]
        assert counts["direct"] == {"ok": 2, "no_route": 1}
    assert semantic["reverse_vs_fixed_mismatches"] == []
    assert semantic["cold_vs_warm_mismatches"] == []
    assert semantic["attempts_inconsistent"] == []
    timing = summary["timing"]["full_source/matrix"]["single_path"]
    # 2 orders x 2 repeats pooled per case; warmups excluded
    assert timing["samples_per_case"] == {"n": 3, "min": 4, "median": 4, "max": 4}
    quote = summary["quote_cli"]
    assert quote["one_solve_per_algorithm_per_invocation"] is True
    # The full-source sentinel bundle is exactly the one `main.py quote` derives.
    assert quote["bundle_hashes"] == [doc["bundles"]["full_source/sentinel"]["bundle_hash"]]
    cold = summary["cold"]["full_source/matrix"]["single_path"]
    assert cold["memory_records"] == 3 and cold["solve_peak_bytes"]["n"] == 3
    assert cold["charged_seconds"]["n"] == 3  # every cold case charged, failures included
    held_out = summary["quality"]["full_source/matrix"]["per_algorithm"]["direct"]["held_out"]
    assert held_out["cases"] + held_out["not_applicable"] == 2  # held-out cases only
    rendered = report.render_summary(summary).replace(report.NO_TAIL_CLAIM, "")
    assert not re.search(r"p9\d|percentile|\bSLA\b", rendered, re.IGNORECASE)
    # A/A comparison of the experiment with itself: exact semantics, never an adopt verdict
    # -- inconclusive from a dirty tree or a loaded host, otherwise rejected (no speedup).
    result = report.compare(out, out, lane="exact")
    assert result["semantic_mismatches"] == [] and result["work_differences"] == []
    assert result["coverage_problems"] == {"baseline": [], "candidate": [], "pairing": []}
    unusable = doc["source"]["git_dirty"] or doc["load"]["contaminated"]
    assert result["verdict"] == ("inconclusive" if unusable else "reject"), result["reasons"]
    # Sufficient-budget re-solve of one listed record, same derived bundle, raised budget.
    sufficient = tmp_path / "sufficient.yaml"
    sufficient.write_text(yaml.safe_dump({
        "schema": "latency-sufficient-budget/1", "key": "T-SB", "version": 1,
        "protocol": {"path": str(protocol), "sha256": sha256_file(protocol)},
        "budget": {"time_limit_seconds": 60, "max_quotes": None, "max_candidates": None},
        "measurement": {"warmup": 0, "repeats": 2},
        "cases": [{"cohort": "full_source", "case": "emp-09bc4e-779ded-low-1",
                   "algorithms": ["single_path"], "reason": "test"}],
    }))  # fmt: skip
    sb_dir = latency.run_sufficient_budget(
        sufficient, str(FIXTURE), tmp_path / "sb", allow_dirty=True, echo=lambda _: None
    )
    evidence = report.load_sufficient(sb_dir)
    assert evidence.document["budget"]["max_quotes"] is None
    (record,) = evidence.runs["full_source"].records
    assert record["measurement"]["attempts_completed"] == 2 and not report.budget_bound(record)
    assert load_manifest(evidence.runs["full_source"].path).resolved_profile["budget"][
        "max_quotes"
    ] is None  # fmt: skip
    experiment = report.load_experiment(out)
    assert report.sufficient_problems(evidence, experiment) == []  # same source and bundle
    rows = report.summarize_sufficient(evidence, experiment)["records"]
    assert rows[0]["fixed_budget"]["status"] == rows[0]["sufficient_budget"]["status"]
    assert rows[0]["differs_from_fixed_budget_in"] == ""  # not bound: identical semantics
    paired = report.compare(out, out, lane="exact", sufficient=(sb_dir, sb_dir))
    assert paired["bounded_exactness"]["required"] == 0
    assert paired["bounded_exactness"]["evidence_problems"]["baseline"] == []


def test_sigterm_finalizes_the_experiment_as_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = latency.measure_run
    calls = 0

    def measure_then_terminate(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:  # a supervisor stops the driver after the second run's first case
            inner = kwargs["on_record"]

            def on_record(algorithm: str, case_id: str) -> None:
                inner(algorithm, case_id)
                os.kill(os.getpid(), signal.SIGTERM)

            kwargs["on_record"] = on_record
        return real(*args, **kwargs)

    monkeypatch.setattr(latency, "measure_run", measure_then_terminate)
    protocol = _write_protocol(tmp_path, _protocol_doc(tmp_path))
    code = latency.main(["run", "--protocol", str(protocol), "--bundle", str(FIXTURE),
                         "--out", str(tmp_path / "latency"), "--allow-dirty"])  # fmt: skip
    assert code == 130
    (out,) = (tmp_path / "latency").iterdir()
    doc = json.loads((out / "experiment.json").read_text())
    assert doc["state"] == "interrupted" and len(doc["runs"]) == 1  # completed run kept
    manifests = [
        json.loads((p / "manifest.json").read_text()) for p in sorted((out / "runs").iterdir())
    ]
    assert [m["state"] for m in manifests] == ["complete", "interrupted"]
    (cases,) = (out / "runs").glob("*sentinel/cases.jsonl")
    statuses = [json.loads(line)["status"] for line in cases.read_text().splitlines()]
    assert statuses[0] != "cancelled" and set(statuses[1:]) == {"cancelled"}  # all retained
    with pytest.raises(report.LatencyReportError, match="interrupted"):
        report.summarize(out)  # never read as a complete experiment


# ------------------------------------------------------------ acceptance rules


def _t(verdict: str) -> dict[str, Any]:
    return {"verdict": verdict}


CLEAN_FACTS: dict[str, Any] = dict(
    lane="exact", candidate_internal=0, semantic_mismatches=0, coverage_problems=0,
    baseline_internal=0, unproven_bounded=0, evidence_clean=True, contaminated=False,
    timing={"a": _t("faster")}, charged={"a": _t("not_slower")},
)  # fmt: skip


@pytest.mark.parametrize(
    ("change", "verdict"),
    [
        ({}, "adopt_eligible"),
        (dict(semantic_mismatches=1), "reject"),
        (dict(candidate_internal=1, lane="heuristic"), "reject"),
        (dict(coverage_problems=1), "inconclusive"),
        (dict(baseline_internal=1), "inconclusive"),
        (dict(unproven_bounded=1), "inconclusive"),
        (dict(unproven_bounded=1, lane="heuristic"), "opt_in_only"),
        (dict(charged={"a": _t("lost_samples")}), "reject"),
        (dict(charged={}), "inconclusive"),  # faster without any cold charged evidence
        (dict(charged={"a": _t("insufficient_cases")}), "inconclusive"),
        (dict(timing={"a": _t("faster"), "b": _t("slower")}), "reject"),
        (dict(timing={"a": _t("faster"), "b": _t("lost_samples")}), "reject"),
        (dict(charged={"a": _t("slower")}), "reject"),
        (dict(timing={"a": _t("no_worthwhile_change")}), "reject"),
        (dict(lane="heuristic", semantic_mismatches=3), "opt_in_only"),
        (dict(contaminated=True), "inconclusive"),
        (dict(evidence_clean=False), "inconclusive"),
    ],
)  # fmt: skip
def test_judge_applies_the_preregistered_rules(change: dict[str, Any], verdict: str) -> None:
    assert report.judge(**{**CLEAN_FACTS, **change})[0] == verdict


def test_timing_verdict_needs_wall_and_cpu_beyond_the_threshold() -> None:
    assert report._timing_verdict({"wall": 0.2, "cpu": 0.15}, 0.1) == "faster"
    assert report._timing_verdict({"wall": 0.2, "cpu": 0.05}, 0.1) == "no_worthwhile_change"
    assert report._timing_verdict({"wall": 0.0, "cpu": -0.2}, 0.1) == "slower"
    assert report._timing_verdict({"wall": None, "cpu": 0.5}, 0.1) == "insufficient_cases"


def test_regret_is_against_the_same_scope_best_known_and_unknown_scores_are_na() -> None:
    def rec(algorithm: str, status: str, score: int | None, case: str = "c") -> dict[str, Any]:
        text = None if score is None else str(score)
        return {"algorithm": algorithm, "case_id": case, "status": status, "score": text}

    records = [rec("a", "ok", 1000), rec("b", "ok", 990), rec("c", "ok", None),
               rec("d", "no_route", None), rec("a", "ok", 500, "t"),
               rec("b", "ok", 1000, "t")]  # fmt: skip
    run = SimpleNamespace(records=records)
    block = report._quality_block(run, {"c": "held_out", "t": "tuning"})  # type: ignore[arg-type]
    row = block["cases"]["c"]
    assert row["best_known_score"] == "1000" and row["split"] == "held_out"
    assert row["a"]["regret_bps"] == 0 and row["b"]["regret_bps"] == 100
    assert row["c"]["regret_bps"] is None and row["d"]["regret_bps"] is None
    assert block["per_algorithm"]["c"]["held_out"]["not_applicable"] == 1
    # The tuning-case loss of `a` never reaches its held-out aggregate.
    assert block["per_algorithm"]["a"]["held_out"]["max_regret_bps"] == 0
    assert block["per_algorithm"]["a"]["tuning"]["max_regret_bps"] == 5000


def test_cold_charge_counts_prepare_once() -> None:
    """Worker start-up runs until ready, after prepare: it already contains prepare."""
    events = ({"index": 0, "startup_seconds": 5.0, "prepare_seconds": 3.0},)
    record = _rec("direct", "h1", 2.0)
    record["measurement"].update(transport_seconds=[0.25], evaluation_seconds=1.0)
    run = SimpleNamespace(manifest=SimpleNamespace(prepare_events=events), records=[record],
                          memory=[])  # fmt: skip
    parts = report.cold_parts(run)[("direct", "h1")]  # type: ignore[arg-type]
    assert parts["charged_seconds"] == pytest.approx(8.25)  # not 11.25
    assert parts["spawn_seconds"] == pytest.approx(2.0)
    block = report._cold_block(run)["direct"]  # type: ignore[arg-type]
    assert block["charged_seconds"]["median"] == pytest.approx(8.25)
    failed = _rec("direct", "h2", 2.0)
    failed["measurement"].update(solve_seconds=[], prepare_event=0)  # no solve: not charged
    run.records.append(failed)
    assert report.cold_parts(run)[("direct", "h2")]["charged_seconds"] is None  # type: ignore[arg-type]


# ------------------------------------------------------------ comparator end to end
#
# Complete in-memory experiments (the same shapes the driver writes): identical semantics,
# five 1.0 s baseline vs five 0.5 s candidate samples per case in both orders, clean
# provenance, uncontaminated load. The unmodified pair is adopt_eligible; every defect
# below must stop that.

ALGS = ("direct", "single_path")
CASES = {"full_source/matrix": ["t1", "h1", "h2"], "full_source/sentinel": ["s"]}
STAGE_ORDERS = (("timing", "fixed"), ("timing", "reverse"), ("cold", "fixed"))
RUN_KEYS = [(stage, order, label) for label in CASES for stage, order in STAGE_ORDERS]


def _rec(
    algorithm: str, case: str, wall: float, *, samples: int = 5, warmup: int = 1, **extra: Any
) -> dict[str, Any]:
    """A record as the driver writes it: `warmup` + `samples` attempts all returned."""
    record: dict[str, Any] = {
        "algorithm": algorithm, "case_id": case, "status": "ok", "score": "1000",
        "evaluation": {"gross_output": "1000"}, "error": None, "limit_hit": None,
        "solver_reported": {"status": "ok"}, "quotes": {"attempted": 5, "counted": 5},
        "candidates_considered": 1, "candidates_truncated": 0, "search": {"truncated_by": None},
        "measurement": {"seed": 1, "attempts_consistent": True, "prepare_event": 0,
                        "warmup": warmup, "repeats": samples,
                        "attempts_completed": warmup + samples,
                        "solve_seconds": [wall] * samples, "solve_cpu_seconds": [wall] * samples,
                        "transport_seconds": [0.001] * samples, "evaluation_seconds": 0.001},
    }  # fmt: skip
    record.update(extra)
    return record


def _memory(algorithm: str, case: str, index: int, event: int) -> dict[str, Any]:
    """A measured memory-pass outcome as `benchmark.runner._measure_memory` writes it."""
    return {"pass": "memory", "schedule_index": index, "case_id": case, "algorithm": algorithm,
            "seed": 1, "solve_peak_bytes": 4096, "solver_status": "ok", "error": None,
            "prepare_event": event, "worker_id": event, "status": "measured"}  # fmt: skip


def _memory_event(index: int, algorithm: str) -> dict[str, Any]:
    return {"index": index, "pass": "memory", "algorithm": algorithm, "status": "ok",
            "reason": "per_case", "error": None, "worker_id": index, "startup_seconds": 0.3,
            "prepare_seconds": 0.02, "prepare_peak_bytes": 2048}  # fmt: skip


def _experiment(wall: float, algorithms: Sequence[str] = ALGS) -> report.Experiment:
    protocol: dict[str, Any] = yaml.safe_load((REPO / "config/latency/l01.yaml").read_text())
    protocol.update(
        matrix=[{"case": "t1", "split": "tuning", "covers": []},
                {"case": "h1", "split": "held_out", "covers": []},
                {"case": "h2", "split": "held_out", "covers": []}],
        cohorts=["full_source"], cold={"cohorts": ["full_source"]}, quote_cli={"invocations": 0},
    )  # fmt: skip
    runs = {}
    for stage, order, label in RUN_KEYS:
        schedule = [[a, c] for a in algorithms for c in CASES[label]]
        attempts = {"samples": 1, "warmup": 0} if stage == "cold" else {"samples": 5, "warmup": 1}
        cold = stage == "cold"  # cold runs carry the separate memory pass, as the driver writes
        memory = [_memory(a, c, i, 1 + i) for i, (a, c) in enumerate(schedule)] if cold else []
        manifest = SimpleNamespace(
            algorithms=tuple(algorithms), state="complete", bundle_hash=f"hash-{label}",
            scheduled_count=len(schedule), memory_record_count=len(memory) if cold else None,
            measurement={"schedule": schedule[::-1] if order == "reverse" else schedule,
                         "warmup": attempts["warmup"], "repeats": attempts["samples"],
                         "memory_pass": cold},
            prepare_events=({"index": 0, "algorithm": "direct", "status": "ok",
                             "startup_seconds": 0.2, "prepare_seconds": 0.01},
                            *[_memory_event(m["prepare_event"], m["algorithm"]) for m in memory]),
        )  # fmt: skip
        records = [_rec(a, c, wall, **attempts) for a, c in schedule]
        runs[(stage, order, label)] = report.RunView({}, manifest, records, memory, Path(stage))  # type: ignore[arg-type]
    document = {
        "experiment_id": f"e{wall}", "partial": False, "stages_requested": list(latency.STAGES),
        "protocol": {"sha256": "p", "document": protocol}, "algorithms": list(algorithms),
        "bundles": {label: {"bundle_hash": f"hash-{label}"} for label in CASES},
        "source": {"git_revision": "abc", "git_dirty": False}, "load": {"contaminated": False},
        "quote_cli": [], "profile": {"path": "config/daily_gross.yaml", "sha256": "x"},
    }  # fmt: skip
    return report.Experiment(Path("."), document, runs)


def _record(exp: report.Experiment, key: tuple[str, str, str], alg: str, case: str) -> Any:
    return next(r for r in exp.runs[key].records if (r["algorithm"], r["case_id"]) == (alg, case))


COLD, FIXED, REVERSE = (("cold", "fixed", "full_source/matrix"),
                        ("timing", "fixed", "full_source/matrix"),
                        ("timing", "reverse", "full_source/matrix"))  # fmt: skip


def _failed(record: dict[str, Any], status: str = "algorithm_error") -> None:
    record.update(status=status, score=None, evaluation=None, error="boom")
    m = record["measurement"]  # the first measured attempt failed
    m.update(solve_seconds=[], solve_cpu_seconds=[], transport_seconds=[],
             attempts_completed=m["warmup"])  # fmt: skip


def _cold_error(e: report.Experiment) -> None:
    _failed(_record(e, COLD, "single_path", "h1"))


def _inconsistent(e: report.Experiment) -> None:
    _record(e, FIXED, "single_path", "h1")["measurement"]["attempts_consistent"] = False


def _order_leak(e: report.Experiment) -> None:  # same result in both runs, except reverse
    _record(e, REVERSE, "direct", "t1").update(score="999")


def _missing_record(e: report.Experiment) -> None:
    e.runs[REVERSE].records.pop()


def _missing_run(e: report.Experiment) -> None:
    del e.runs[COLD]


def _budget_bound(e: report.Experiment) -> None:  # truncated by the quote cap, then differs
    for key in (FIXED, REVERSE, COLD):
        _record(e, key, "single_path", "h2").update(
            score="1001", search={"truncated_by": "max_quotes"}
        )


def _one_sample(e: report.Experiment) -> None:  # 1 sample where the protocol schedules 5
    for run in e.runs.values():
        for record in run.records:
            for key in report.SAMPLE_FIELDS:
                record["measurement"][key] = record["measurement"][key][:1]


def _no_cold_cost(e: report.Experiment) -> None:  # cold solves without their charge
    for key, run in e.runs.items():
        if key[0] == "cold":
            for record in run.records:
                record["measurement"].update(solve_seconds=[], solve_cpu_seconds=[])


def _all_bound(e: report.Experiment) -> None:  # identical outputs, all cut by the budget
    for run in e.runs.values():
        for record in run.records:
            record["search"]["truncated_by"] = "max_quotes"


def _prepare_moved(e: report.Experiment) -> None:  # 2 s of work moved into prepare
    for key in (COLD,):
        e.runs[key].manifest.prepare_events[0].update(startup_seconds=2.2, prepare_seconds=2.0)


@pytest.mark.parametrize(
    ("defect", "verdict", "evidence", "symmetric"),
    [
        (_cold_error, "reject", lambda r: r["semantic_mismatches"], True),
        (_inconsistent, "reject",
         lambda r: r["internal_checks"]["candidate"]["attempts_inconsistent"], True),
        (_order_leak, "reject", lambda r: r["internal_checks"]["candidate"]["cross_order"], True),
        (_missing_record, "inconclusive", lambda r: r["coverage_problems"]["candidate"], True),
        (_missing_run, "inconclusive", lambda r: r["coverage_problems"]["candidate"], True),
        (_budget_bound, "inconclusive", lambda r: r["fixed_budget_differences"], True),
        (_one_sample, "inconclusive", lambda r: r["coverage_problems"]["candidate"], True),
        (_no_cold_cost, "inconclusive", lambda r: r["coverage_problems"]["candidate"], True),
        (_all_bound, "inconclusive", lambda r: r["bounded_exactness"]["unproven"], True),
        (_prepare_moved, "reject",  # a slower baseline prepare is fine: not symmetric
         lambda r: r["charged"]["full_source/matrix direct"]["verdict"] == "slower", False),
    ],
)  # fmt: skip
def test_exact_comparison_is_adopt_eligible_only_without_any_defect(
    defect: Any, verdict: str, evidence: Any, symmetric: bool
) -> None:
    base = _experiment(1.0)
    clean = report.compare_experiments(base, _experiment(0.5), lane="exact")
    assert clean["verdict"] == "adopt_eligible", clean["reasons"]
    assert clean["semantic_mismatches"] == [] and clean["fixed_budget_differences"] == []
    candidate = _experiment(0.5)
    defect(candidate)
    result = report.compare_experiments(base, candidate, lane="exact")
    assert result["verdict"] == verdict, result["reasons"]
    assert evidence(result)  # the defect is named, not only reflected in the verdict
    if not symmetric:
        return
    # The same defect in the baseline never yields an adopt verdict either.
    base_defect = _experiment(1.0)
    defect(base_defect)
    flipped = report.compare_experiments(base_defect, _experiment(0.5), lane="exact")
    assert flipped["verdict"] != "adopt_eligible"


# ------------------------------------------------------------ cold memory pass (R1-F3)

SENTINEL_COLD = ("cold", "fixed", "full_source/sentinel")


def _memory_of(e: report.Experiment, alg: str, case: str) -> Any:
    return next(m for m in e.runs[COLD].memory if (m["algorithm"], m["case_id"]) == (alg, case))


def _manifest(e: report.Experiment, key: tuple[str, str, str], **changes: Any) -> None:
    vars(e.runs[key].manifest).update(changes)


def _drop_outcome(e: report.Experiment) -> None:  # one outcome gone, count kept consistent
    e.runs[COLD].memory.remove(_memory_of(e, "single_path", "h1"))
    _manifest(e, COLD, memory_record_count=len(e.runs[COLD].memory))


def _empty_pass(e: report.Experiment) -> None:  # the reviewer's R1-F3 reproduction
    for key in (COLD, SENTINEL_COLD):
        e.runs[key].memory.clear()
        _manifest(e, key, memory_record_count=0)


def _duplicate(e: report.Experiment) -> None:
    e.runs[COLD].memory.append(dict(_memory_of(e, "direct", "t1")))
    _manifest(e, COLD, memory_record_count=len(e.runs[COLD].memory))


def _unscheduled(e: report.Experiment) -> None:
    e.runs[COLD].memory.append({**_memory_of(e, "direct", "t1"), "case_id": "s"})
    _manifest(e, COLD, memory_record_count=len(e.runs[COLD].memory))


def _count_mismatch(e: report.Experiment) -> None:
    _manifest(e, COLD, memory_record_count=len(e.runs[COLD].memory) + 1)


def _undeclared(e: report.Experiment) -> None:
    e.runs[COLD].manifest.measurement["memory_pass"] = False


def _no_peak(e: report.Experiment) -> None:
    _memory_of(e, "single_path", "h1")["solve_peak_bytes"] = None


def _bad_peak(e: report.Experiment) -> None:
    _memory_of(e, "single_path", "h1")["solve_peak_bytes"] = -1


def _no_prepare_peak(e: report.Experiment) -> None:
    event = _memory_of(e, "single_path", "h1")["prepare_event"]
    e.runs[COLD].manifest.prepare_events[event]["prepare_peak_bytes"] = None


def _fabricated_failure(e: report.Experiment) -> None:  # a failure with an invented peak
    _memory_of(e, "single_path", "h1").update(status="timeout", error="solve exceeded")


def _silent_failure(e: report.Experiment) -> None:  # a failure without its error
    _memory_of(e, "single_path", "h1").update(status="error", solve_peak_bytes=None)


def _cancelled_outcome(e: report.Experiment) -> None:
    _memory_of(e, "single_path", "h1").update(status="cancelled", solve_peak_bytes=None)


def _timing_memory(e: report.Experiment) -> None:  # a memory pass the timing stage never has
    _manifest(e, FIXED, memory_record_count=0)


@pytest.mark.parametrize(
    ("defect", "expected"),
    [
        (_drop_outcome, "cold fixed full_source/matrix: single_path/h1: scheduled memory "
                        "outcome missing"),
        (_empty_pass, "cold fixed full_source/sentinel: single_path/s: scheduled memory "
                      "outcome missing"),
        (_duplicate, "direct/t1: unscheduled or duplicate memory outcome"),
        (_unscheduled, "direct/s: unscheduled or duplicate memory outcome"),
        (_count_mismatch, "manifest memory_record_count 7 != 6 memory outcome(s)"),
        (_undeclared, "memory pass not declared"),
        (_no_peak, "single_path/h1: measured memory outcome lacks"),
        (_bad_peak, "single_path/h1: measured memory outcome lacks"),
        (_no_prepare_peak, "single_path/h1: measured memory outcome lacks"),
        (_fabricated_failure, "single_path/h1: failed memory outcome 'timeout'"),
        (_silent_failure, "single_path/h1: failed memory outcome 'error'"),
        (_cancelled_outcome, "single_path/h1: memory outcome is 'cancelled'"),
        (_timing_memory, "timing fixed full_source/matrix: memory pass outside"),
    ],
)  # fmt: skip
def test_incomplete_cold_memory_pass_is_a_coverage_problem(defect: Any, expected: str) -> None:
    exp = _experiment(0.5)
    assert report.coverage_problems(exp) == []
    defect(exp)
    problems = report.coverage_problems(exp)
    assert any(expected in p for p in problems), problems
    result = report.compare_experiments(_experiment(1.0), exp, lane="exact")
    assert result["verdict"] == "inconclusive" and result["coverage_problems"]["candidate"]


def test_explicit_memory_failures_and_no_route_peaks_stay_valid() -> None:
    exp = _experiment(0.5)
    _memory_of(exp, "direct", "t1")["solver_status"] = "no_route"  # a measured solve peak
    _memory_of(exp, "single_path", "h1").update(
        status="timeout", error="solve exceeded the 120s time limit; worker killed",
        solve_peak_bytes=None, worker_id=None)  # fmt: skip
    failed = _memory_of(exp, "single_path", "h2")
    failed.update(status="prepare_failed", error="prepare raised: boom", solve_peak_bytes=None)
    exp.runs[COLD].manifest.prepare_events[failed["prepare_event"]].update(
        status="error", error="prepare raised: boom", prepare_peak_bytes=None)  # fmt: skip
    assert report.coverage_problems(exp) == []
    block = report._cold_block(exp.runs[COLD])
    assert block["single_path"]["memory_failures"] == {"prepare_failed": 1, "timeout": 1}
    assert block["single_path"]["solve_peak_bytes"]["n"] == 1  # no fabricated peak
    assert "memory_failures" not in block["direct"]
    peaks = {(r["algorithm"], r["case_id"]): r["solve_peak_bytes"] for r in report.case_records(exp)
             if r["bundle"] == "full_source/matrix"}  # fmt: skip
    assert peaks[("single_path", "h1")] is None and peaks[("direct", "t1")] == 4096


def test_timing_only_run_needs_no_memory_pass() -> None:
    exp = _experiment(0.5)
    exp.document["stages_requested"] = ["timing"]
    for key in [k for k in exp.runs if k[0] == "cold"]:
        del exp.runs[key]
    assert report.coverage_problems(exp) == []


def test_heuristic_candidate_that_loses_timed_cases_is_rejected() -> None:
    candidate = _experiment(0.5)
    for key in (FIXED, REVERSE, COLD):  # times out on a held-out case in every stage
        _failed(_record(candidate, key, "single_path", "h1"), "timeout")
        _record(candidate, key, "single_path", "h1").update(limit_hit="time")
    result = report.compare_experiments(_experiment(1.0), candidate, lane="heuristic")
    assert result["timing"]["full_source/matrix single_path"]["verdict"] == "lost_samples"
    assert result["verdict"] == "reject"
    assert len(result["status_regressions"]) == 3


def test_heuristic_regret_is_scoped_by_split() -> None:
    candidate = _experiment(0.5)
    for key in (FIXED, REVERSE, COLD):  # a loss on the tuning case only
        _record(candidate, key, "single_path", "t1").update(score="990")
    result = report.compare_experiments(_experiment(1.0), candidate, lane="heuristic")
    regret = result["regret"]["full_source/matrix single_path"]
    assert regret["held_out"]["losses"] == 0 and regret["held_out"]["cases"] == 2
    assert regret["tuning"]["losses"] == 1 and regret["tuning"]["max_regret_bps"] == 100
    assert result["verdict"] == "opt_in_only"
    assert "sentinel" in result["regret"]["full_source/sentinel single_path"]


def test_algorithm_pairing_cannot_drop_algorithms() -> None:
    base, candidate = _experiment(1.0), _experiment(0.5)
    with pytest.raises(report.LatencyReportError, match="exact lane"):
        report.compare_experiments(base, candidate, lane="exact", pairs={"direct": "single_path"})
    candidate.document["algorithms"] = ["direct"]  # a candidate that skipped an algorithm
    with pytest.raises(report.LatencyReportError, match="never compared"):
        report.compare_experiments(base, candidate, lane="exact")


def test_sample_completeness_follows_the_declared_attempts() -> None:
    complete = _rec("direct", "c", 1.0)  # warmup 1 + 5 measured, all returned
    assert report.sample_problem(complete, 1, 5) is None
    short = _rec("direct", "c", 1.0)
    short["measurement"]["transport_seconds"] = [0.001]
    assert "samples" in (report.sample_problem(short, 1, 5) or "")
    assert "declares" in (report.sample_problem(complete, 1, 3) or "")  # a different schedule
    failed = _rec("direct", "c", 1.0)
    _failed(failed, "timeout")  # the first measured attempt was killed: no sample is right
    assert report.sample_problem(failed, 1, 5) is None
    early_ok = _rec("direct", "c", 1.0)
    early_ok["measurement"]["attempts_completed"] = 3  # only a failure may stop early
    assert "attempt(s) completed" in (report.sample_problem(early_ok, 1, 5) or "")


def test_a_lost_cold_charge_is_a_rejection_not_a_skip() -> None:
    base, candidate = _experiment(1.0), _experiment(0.5)
    _record(candidate, COLD, "single_path", "h1")["measurement"]["prepare_event"] = 99
    verdict = report._charged_verdict(
        base.runs[COLD], candidate.runs[COLD], "single_path", "single_path", base.split_of,
        base.acceptance, 0.1,
    )  # fmt: skip
    assert verdict["verdict"] == "lost_samples" and verdict["lost_decision_cases"] == ["h1"]
    result = report.compare_experiments(base, candidate, lane="exact")
    assert result["verdict"] == "inconclusive"
    assert any("cold charge evidence" in m for m in result["coverage_problems"]["candidate"])


# ------------------------------------------------------------ sufficient-budget exactness

BOUND = ("full_source/matrix", "single_path", "h2")


def _bound_pair() -> tuple[report.Experiment, report.Experiment]:
    """Both sides: single_path/h2 cut by the quote cap with IDENTICAL outputs."""
    base, candidate = _experiment(1.0), _experiment(0.5)
    for exp in (base, candidate):
        for key in (FIXED, REVERSE, COLD):
            _record(exp, key, "single_path", "h2")["search"] = {"truncated_by": "max_quotes"}
    return base, candidate


SB_PROTOCOL = REPO / "config/latency/l01-sufficient-budget.yaml"
SB_MEASUREMENT = yaml.safe_load(SB_PROTOCOL.read_text())["measurement"]  # warmup 0, repeats 2


def _evidence(exp: report.Experiment, *records: dict[str, Any]) -> report.SufficientEvidence:
    schedule = [[r["algorithm"], r["case_id"]] for r in records]
    declared = dict(SB_MEASUREMENT)  # as l01-sufficient-budget.yaml and the driver declare
    manifest = SimpleNamespace(bundle_hash="hash-full_source/matrix", state="complete",
                               measurement={"schedule": schedule, **declared,
                                            "stage": "sufficient_budget"})  # fmt: skip
    document = {
        "protocol": {"sha256": exp.document["protocol"]["sha256"]},
        "source": dict(exp.document["source"]), "profile": dict(exp.document["profile"]),
        "bundles": {"full_source/matrix": {"bundle_hash": "hash-full_source/matrix"}},
        "sufficient_budget": {"sha256": "sb", "document": {"measurement": dict(declared)}},
    }  # fmt: skip
    run = report.RunView({}, manifest, list(records), [], Path("sb"))  # type: ignore[arg-type]
    return report.SufficientEvidence(Path("."), document, {"full_source": run})


def _unbounded(**extra: Any) -> dict[str, Any]:  # re-solved with the quote cap raised
    return _rec("single_path", "h2", 2.0, samples=SB_MEASUREMENT["repeats"],
                warmup=SB_MEASUREMENT["warmup"], **extra)  # fmt: skip


def _still_bound() -> dict[str, Any]:
    return _unbounded(search={"truncated_by": "max_quotes"})


def _inconsistent_sb() -> dict[str, Any]:
    record = _unbounded()
    record["measurement"]["attempts_consistent"] = False
    return record


@pytest.mark.parametrize(
    ("base_sb", "cand_sb", "verdict", "outcome"),
    [
        (None, None, "inconclusive", "unproven"),  # identical bounded outputs are not enough
        (_unbounded, _unbounded, "adopt_eligible", "established"),
        (_unbounded, lambda: _unbounded(score="999"), "reject", "mismatches"),
        (_unbounded, _still_bound, "inconclusive", "unproven"),
        (_unbounded, _inconsistent_sb, "inconclusive", "unproven"),
        (_unbounded, lambda: _rec("direct", "h2", 1.0, samples=2, warmup=0), "inconclusive",
         "unproven"),  # the bound record was not re-solved
    ],
)  # fmt: skip
def test_budget_bound_exactness_needs_sufficient_budget_evidence(
    base_sb: Any, cand_sb: Any, verdict: str, outcome: str
) -> None:
    base, candidate = _bound_pair()
    evidence = None
    if base_sb is not None:
        evidence = (_evidence(base, base_sb()), _evidence(candidate, cand_sb()))
    result = report.compare_experiments(base, candidate, lane="exact", sufficient=evidence)
    assert result["verdict"] == verdict, result["reasons"]
    bounded = result["bounded_exactness"]
    assert bounded["required"] == 1 and len(bounded[outcome]) == 1
    assert result["fixed_budget_differences"] == []  # the bounded outputs were identical


def test_sufficient_evidence_is_pinned_to_each_experiments_source_and_bundle() -> None:
    base, candidate = _bound_pair()
    stale = _evidence(candidate, _unbounded())
    stale.document["source"]["git_revision"] = "older"  # measured on another source
    result = report.compare_experiments(
        base, candidate, lane="exact", sufficient=(_evidence(base, _unbounded()), stale)
    )
    assert result["verdict"] == "inconclusive"
    assert result["bounded_exactness"]["evidence_problems"]["candidate"]
    other = _evidence(candidate, _unbounded())
    other.runs["full_source"].manifest.bundle_hash = "another-bundle"  # type: ignore[misc]
    assert report.sufficient_problems(other, candidate)


def test_fixed_budget_completion_is_reported_apart_from_sufficient_exactness() -> None:
    base, candidate = _bound_pair()
    for key in (FIXED, REVERSE, COLD):  # the candidate completes more under the fixed cap
        _record(candidate, key, "single_path", "h2").update(score="1001")
    evidence = (_evidence(base, _unbounded()), _evidence(candidate, _unbounded()))
    result = report.compare_experiments(base, candidate, lane="exact", sufficient=evidence)
    assert result["verdict"] == "adopt_eligible", result["reasons"]
    assert len(result["fixed_budget_differences"]) == 3  # kept, per stage/order
    assert result["bounded_exactness"]["established"]


def _sb_records(ev: report.SufficientEvidence) -> list[dict[str, Any]]:
    return ev.runs["full_source"].records


def _zero_attempts(ev: report.SufficientEvidence) -> None:  # ok/consistent, nothing solved
    for record in _sb_records(ev):
        record["measurement"].update(attempts_completed=0, solve_seconds=[],
                                     solve_cpu_seconds=[], transport_seconds=[])  # fmt: skip


def _one_attempt(ev: report.SufficientEvidence) -> None:  # 1 of the 2 declared attempts
    for record in _sb_records(ev):
        m = record["measurement"]
        m["attempts_completed"] = 1
        for key in report.SAMPLE_FIELDS:
            m[key] = m[key][:1]


def _one_sb_sample(ev: report.SufficientEvidence) -> None:  # 2 attempts, one wall sample lost
    for record in _sb_records(ev):
        record["measurement"]["solve_seconds"] = record["measurement"]["solve_seconds"][:1]


def _record_repeats(ev: report.SufficientEvidence) -> None:  # record claims another schedule
    for record in _sb_records(ev):
        record["measurement"]["repeats"] = 3


def _manifest_undeclared(ev: report.SufficientEvidence) -> None:
    for key in ("warmup", "repeats"):
        del ev.runs["full_source"].manifest.measurement[key]


def _manifest_repeats(ev: report.SufficientEvidence) -> None:
    ev.runs["full_source"].manifest.measurement["repeats"] = 3


def _protocol_undeclared(ev: report.SufficientEvidence) -> None:
    del ev.document["sufficient_budget"]["document"]


def _protocol_repeats(ev: report.SufficientEvidence) -> None:  # protocol != manifest/records
    ev.document["sufficient_budget"]["document"]["measurement"]["repeats"] = 3


@pytest.mark.parametrize("side", ["baseline", "candidate"])
@pytest.mark.parametrize(
    ("defect", "message"),
    [
        (_zero_attempts, "0 attempt\\(s\\) completed"),
        (_one_attempt, "1 attempt\\(s\\) completed"),
        (_one_sb_sample, "samples .*expected 2 each"),
        (_record_repeats, "single_path/h2: declares warmup/repeats 0/3"),
        (_manifest_undeclared, "run declares warmup/repeats None/None"),
        (_manifest_repeats, "run declares warmup/repeats 0/3, sufficient-budget protocol 0/2"),
        (_protocol_undeclared, "declares no valid warmup/repeats"),
        (_protocol_repeats, "run declares warmup/repeats 0/2, sufficient-budget protocol 0/3"),
    ],
)  # fmt: skip
def test_sufficient_exactness_needs_the_declared_attempts_and_samples(
    defect: Any, message: str, side: str
) -> None:
    base, candidate = _bound_pair()
    evidence = (_evidence(base, _unbounded()), _evidence(candidate, _unbounded()))
    valid = report.compare_experiments(base, candidate, lane="exact", sufficient=evidence)
    assert valid["verdict"] == "adopt_eligible" and valid["bounded_exactness"]["established"]
    defect(evidence[side == "candidate"])  # mutated only after the valid construction
    result = report.compare_experiments(base, candidate, lane="exact", sufficient=evidence)
    assert result["verdict"] == "inconclusive", result["reasons"]
    bounded = result["bounded_exactness"]
    assert bounded["established"] == [] and bounded["mismatches"] == []
    assert bounded["unproven"] == [
        f"{BOUND[0]} single_path/h2 (3 bound record(s)): evidence invalid"
    ]
    problems = bounded["evidence_problems"]
    assert any(re.search(message, p) for p in problems[side]), problems
    assert problems["baseline" if side == "candidate" else "candidate"] == []


@pytest.mark.parametrize("status", ["algorithm_error", "timeout"])
def test_failed_sufficient_companions_leave_exactness_unproven(status: str) -> None:
    base, candidate = _bound_pair()
    evidence = (_evidence(base, _unbounded()), _evidence(candidate, _unbounded()))
    for ev in evidence:  # identical failures on both sides: explicit, never a zero or a pass
        _failed(_sb_records(ev)[0], status)
    result = report.compare_experiments(base, candidate, lane="exact", sufficient=evidence)
    assert result["verdict"] == "inconclusive", result["reasons"]
    bounded = result["bounded_exactness"]
    assert bounded["evidence_problems"] == {"baseline": [], "candidate": [], "pairing": []}
    assert bounded["established"] == [] and "failed" in bounded["unproven"][0]


# ------------------------------------------------------------ L08 arms (WHI-1510)

ARMS = REPO / "config" / "latency" / "l08.yaml"


def test_checked_in_arms_file_pins_l01_and_resolves_every_arm() -> None:
    arms = latency.load_arms(ARMS)
    assert arms.protocol_sha256 == sha256_file(REPO / "config/latency/l01.yaml")
    assert arms.sufficient_sha256 == sha256_file(REPO / "config/latency/l01-sufficient-budget.yaml")
    assert list(arms.arms) == ["R", "E1", "E2", "E3", "E4", "S0", "H1", "H2", "H3", "H4"]
    protocol = latency.load_protocol(REPO / arms.protocol_path)
    assert protocol.acceptance["heuristic_default_loss_tolerance"] is None
    for name, arm in arms.arms.items():
        profile = latency.arm_profile(arm, protocol)  # the ordinary loader validates it
        for algorithm in profile.algorithms:  # every wrapped factory crosses into a worker
            pickle.loads(pickle.dumps(latency.arm_factory(get_algorithm(algorithm), arm)))
        assert ("quote_cli" in arm.stages) == (name == "R")  # CLI = default path only
        quote_path = profile.resolved()["algorithm_config"].get("uni_sor_fast", {})
        if arm.controls and quote_path:  # never the static "controls off" label
            assert quote_path["provenance"]["quote_path"].startswith(f"L08 arm {name}")
    decisions = {c["id"] for c in arms.comparisons if c["role"] == "decision"}
    assert decisions == {"L02", "L03", "L04", "L05", "L06", "L07-combined", "L07-adaptive-only"}
    # No default profile, source pin or protocol file is touched by an arm.
    assert sha256_file(REPO / protocol.profile_path) == protocol.profile_sha256


def _arms_doc() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(ARMS.read_text())
    return doc


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["controls"]["L03"].update(tick_capacity=0), "tick_capacity"),
        (lambda d: d["controls"]["L04"].update(lifetime="per_worker"), "per_solve"),
        (lambda d: d["controls"].update(L09={"x": 1}), "unknown control"),
        (lambda d: d["controls"]["L02"].update(skip_empty_spans=False), "must be true"),
        (lambda d: d["arms"][1].update(stages=["timing", "cold", "quote_cli"]), "main.py quote"),
        (lambda d: d["arms"][1].update(stages=["timing"]), "always required"),
        (lambda d: d["arms"][0].update(shortlist={"routes_per_probe": 2}), "only for"),
        (lambda d: d["arms"][1].update(controls=["L09"]), "not all registered"),
        (lambda d: d["comparisons"][0].update(pairs={"a": "b"}), "itself"),
        (lambda d: d["comparisons"][0].update(candidate="Z"), "unknown arm"),
        (lambda d: d["dispositions"]["exact"].pop("reject"), "missing"),
    ],
)
def test_arms_file_validation_refuses_malformed_documents(
    tmp_path: Path, mutate: Any, message: str
) -> None:
    doc = _arms_doc()
    mutate(doc)
    path = tmp_path / "arms.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(latency.LatencyError, match=message):
        latency.load_arms(path)


def _swap_probe(seen: list[Any]) -> Any:
    from pools import concentrated, liquidity_book

    def solve(case: Any, context: Any, budget: Any) -> Any:
        seen.append((concentrated.swap, liquidity_book.swap))
        from routing.algorithms.base import SolveResult, SolveStatus

        return SolveResult(case_id="c", algorithm="uni_sor_fast", status=SolveStatus.NO_ROUTE,
                           search_stats={"sor_fast": {"quote_path": "all off"}})  # fmt: skip

    return solve


def test_controls_are_fresh_per_solve_and_never_outlive_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from pools import concentrated, liquidity_book

    arm = latency.load_arms(ARMS).arms["H4"]
    reference = (concentrated.swap, liquidity_book.swap)
    seen: list[Any] = []
    solve = latency.controlled_solve(dict(arm.controls), arm.name, _swap_probe(seen), None, None,
                                     None)  # fmt: skip
    latency.controlled_solve(dict(arm.controls), arm.name, _swap_probe(seen), None, None, None)
    (cl1, lb1), (cl2, lb2) = seen
    assert cl1.keywords["skip_empty_spans"] is True and cl1.func is reference[0]
    tick, prefix, bins = cl1.keywords["math_reuse"], cl1.keywords["prefix_reuse"], lb1.keywords[
        "math_reuse"
    ]  # fmt: skip
    assert (tick.capacity, bins.capacity) == (16384, 4096)
    assert (prefix.max_keys, prefix.max_checkpoints) == (4096, 262144)
    # a fresh instance per solve: nothing is shared across solves (state isolation)
    assert cl2.keywords["math_reuse"] is not tick and cl2.keywords["prefix_reuse"] is not prefix
    assert lb2.keywords["math_reuse"] is not bins
    assert (concentrated.swap, liquidity_book.swap) == reference  # restored after the solve
    stats = solve.search_stats[latency.CONTROL_STATS_KEY]
    assert stats["controls"] == ["L02", "L03", "L04"] and stats["lifetime"] == "per_solve"
    assert stats["tick_math"]["entries"] == 0  # the probe quoted nothing
    assert solve.search_stats["sor_fast"]["quote_path"].startswith("L08 arm H4")
    # A failing solve restores the reference too; a solve never starts from a replaced kernel.

    def boom(case: Any, context: Any, budget: Any) -> Any:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        latency.controlled_solve(dict(arm.controls), "H4", boom, None, None, None)
    assert (concentrated.swap, liquidity_book.swap) == reference
    monkeypatch.setattr(concentrated, "swap", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="already replaced"):
        latency.controlled_solve(dict(arm.controls), "H4", boom, None, None, None)


def test_control_construction_is_charged_inside_the_solve_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowMemo:  # 0.2 s to build: must land in the solve, never in prepare
        def __init__(self, capacity: int) -> None:
            time.sleep(0.2)

        def stats(self) -> dict[str, int]:
            return {}

    monkeypatch.setattr(latency, "TickMathReuse", SlowMemo)
    arm = latency.load_arms(ARMS).arms["E2"]
    base = dataclasses.replace(fake_solvers.ALL[0], solve=_swap_probe([]))
    factory = latency.arm_factory(base, arm)
    assert factory.prepare is base.prepare and factory.name == base.name  # nothing moved there
    started = time.perf_counter()
    factory.solve(None, None, None)  # type: ignore[arg-type]
    assert time.perf_counter() - started >= 0.2
    graph = latency.arm_factory(get_algorithm("incremental_graph"), latency.load_arms(
        ARMS).arms["E4"])  # fmt: skip
    assert graph.solve.args[2].keywords == {"graph_reuse": True}  # type: ignore[attr-defined]
    other = latency.arm_factory(get_algorithm("path_split"), latency.load_arms(
        ARMS).arms["E4"])  # fmt: skip
    assert not hasattr(other.solve.args[2], "keywords")  # type: ignore[attr-defined]
    assert latency.arm_factory(base, latency.load_arms(ARMS).arms["S0"]) is base


def _l08_arms(tmp_path: Path, protocol: Path, sufficient: Path) -> Path:
    doc = _arms_doc()
    doc["protocol"] = {"path": str(protocol), "sha256": sha256_file(protocol)}
    doc["sufficient_budget"] = {"path": str(sufficient), "sha256": sha256_file(sufficient)}
    doc["arms"] = [a for a in doc["arms"] if a["name"] in ("R", "E3")]
    doc["comparisons"] = [c for c in doc["comparisons"] if c["id"] == "L02-L04-cumulative"]
    path = tmp_path / "arms.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def test_l08_session_measures_controls_in_workers_and_judges_registered_comparisons(
    tmp_path: Path,
) -> None:
    protocol = _write_protocol(tmp_path, _protocol_doc(tmp_path))
    sufficient = tmp_path / "sufficient.yaml"
    sufficient.write_text(yaml.safe_dump({
        "schema": "latency-sufficient-budget/1", "key": "T-SB", "version": 1,
        "protocol": {"path": str(protocol), "sha256": sha256_file(protocol)},
        "budget": {"time_limit_seconds": 60, "max_quotes": None, "max_candidates": None},
        "measurement": {"warmup": 0, "repeats": 2},
        "cases": [{"cohort": "full_source", "case": "emp-09bc4e-779ded-low-1",
                   "algorithms": ["single_path"], "reason": "test"}],
    }))  # fmt: skip
    arms = _l08_arms(tmp_path, protocol, sufficient)
    session = latency.run_session(
        arms, str(FIXTURE), tmp_path / "l08", allow_dirty=True, echo=lambda _: None
    )
    doc = json.loads((session / "session.json").read_text())
    assert doc["state"] == "complete" and [e["arm"] for e in doc["entries"]] == ["R", "E3"]
    assert all(e.get("sufficient") for e in doc["entries"])  # own SB per arm
    r_dir, e3_dir = (session / e["experiment"] for e in doc["entries"])
    e3 = json.loads((e3_dir / "experiment.json").read_text())
    assert e3["partial"] is False and e3["stages_requested"] == ["timing", "cold"]
    assert e3["arm"]["controls"].keys() == {"L02", "L03", "L04"}
    manifest = load_manifest(e3_dir / "runs" / "timing-fixed-full_source-matrix")
    assert manifest.resolved_profile["l08_arm"]["name"] == "E3"
    # The controls really ran inside the spawned workers: per-solve memo traffic recorded.
    records = load_case_records(e3_dir / "runs" / "timing-fixed-full_source-matrix")
    ticks = [r["search"][latency.CONTROL_STATS_KEY]["tick_math"] for r in records]
    assert any(t["hits"] + t["misses"] > 0 for t in ticks)
    # Registered exact comparison: identical semantics and work, controls stats aside.
    result = report.compare(r_dir, e3_dir, lane="exact")
    assert result["arms"]["id"] == "L02-L04-cumulative"
    assert result["semantic_mismatches"] == [] and result["work_differences"] == []
    assert result["coverage_problems"] == {"baseline": [], "candidate": [], "pairing": []}
    assert result["internal_checks"]["candidate"] == {
        "cross_order": [], "cold_warm": [], "attempts_inconsistent": []
    }  # fmt: skip
    with pytest.raises(report.LatencyReportError, match="pre-registered"):
        report.compare(e3_dir, r_dir, lane="exact")  # not a registered direction
    # Sufficient-budget evidence is pinned to its own arm.
    r_sb, e3_sb = (report.load_sufficient(session / e["sufficient"]) for e in doc["entries"])
    assert report.sufficient_problems(e3_sb, report.load_experiment(e3_dir)) == []
    assert any("arm" in p for p in report.sufficient_problems(r_sb, report.load_experiment(e3_dir)))
    # Final aggregation: verdict + disposition per registered comparison, per-case records.
    final, rows = report.final_report(session)
    (row,) = final["comparisons"]
    assert row["verdict"] in ("inconclusive", "reject")  # dirty test tree: never adopt
    assert row["disposition"] == _arms_doc()["dispositions"]["exact"][row["verdict"]]
    assert {r["arm"] for r in rows} == {"R", "E3"} and all(r["samples"] for r in rows
                                                           if r["status"] == "ok")  # fmt: skip
    assert final["arms"]["R"]["quote_cli"]["one_solve_per_algorithm_per_invocation"] is True
    assert final["arms"]["E3"]["quote_cli"] is None  # no CLI total claimed for a controlled arm
    report.render_final(final)
    # Every arm and SB run must carry the session's source pin (l08.yaml
    # every_arm_same_clean_commit), checked against the session, not only pairwise.
    zero = {**doc["source"], "git_revision": "0" * 40}
    _refuses_mixed_sources(session, doc, lambda d: d.update(source=zero))  # root declared only
    arm_docs = [session / e["experiment"] / "experiment.json" for e in doc["entries"]]
    _refuses_mixed_sources(session, doc, None, arm_docs, zero)  # consistent foreign arm group
    sb_docs = [session / doc["entries"][1]["sufficient"] / "experiment.json"]
    _refuses_mixed_sources(session, doc, None, sb_docs, zero)  # own-arm SB from elsewhere
    # A missing arm experiment is `not_measured`, never a verdict.
    doc["entries"][1]["experiment"] = "gone"
    (session / "session.json").write_text(json.dumps(doc))
    final, _ = report.final_report(session)
    assert final["comparisons"][0]["verdict"] == "not_measured"
    assert final["comparisons"][0]["disposition"] == "not adopted (not measured)"


def _refuses_mixed_sources(
    session: Path, doc: dict[str, Any], mutate_root: Any, documents: Any = (),
    source: Any = None,
) -> None:  # fmt: skip
    """Mutate only the session's declared source, or rewrite the listed experiment
    documents to one consistent foreign source; `final_report` must then refuse (no
    comparison, no verdict). Every file is restored afterwards."""
    saved = {p: p.read_text() for p in [session / "session.json", *documents]}
    try:
        if mutate_root is not None:
            root = json.loads(json.dumps(doc))
            mutate_root(root)
            (session / "session.json").write_text(json.dumps(root))
        for path in documents:
            exp = json.loads(path.read_text())
            exp["source"] = source
            path.write_text(json.dumps(exp))
        with pytest.raises(report.LatencyReportError, match="measured sources differ"):
            report.final_report(session)
    finally:
        for path, text in saved.items():
            path.write_text(text)
    assert report.final_report(session)[0]["comparisons"]  # restored: accepted again


PINNED_PROFILE = "config/daily_gross.yaml"  # the L01 pin: `algorithms: reference`


def _bind(manifest: Any, arm: latency.Arm, algorithms: Sequence[str]) -> None:
    """Record `arm` as the driver does in a run manifest: measurement arm + effective
    (resolved) profile. Fresh copies, so mutating one declaration never edits another."""
    record = arm.record()
    overlays = {k: record[k] for k in ("shortlist", "sampling") if record[k]}
    params = {k: v for overlay in overlays.values() for k, v in overlay.items()}
    manifest.algorithms = tuple(algorithms)
    manifest.measurement["arm"] = json.loads(json.dumps(record))
    manifest.resolved_profile = json.loads(json.dumps({
        "algorithms": list(algorithms), "l08_arm": record, **overlays,
        "algorithm_config": {a: {"params": params if a == latency.OVERLAY_ALGORITHM else {},
                                 "provenance": {"quote_path": record["quote_path"]}}
                             for a in algorithms},
    }))  # fmt: skip


def _armed(wall: float, name: str, arms_doc: dict[str, Any] | None = None) -> report.Experiment:
    """A GENUINELY registered L08 arm experiment: declared arm, stages, algorithms and every
    run's recorded effective profile are the canonical settings resolved from the embedded
    arms document. Negative tests must mutate it explicitly after construction."""
    doc = arms_doc or _arms_doc()
    arm = latency.parse_arms(doc, "test arms", "arms").arms[name]
    pinned = yaml.safe_load((REPO / PINNED_PROFILE).read_text())["algorithms"]
    algorithms = list(arm.algorithms or pinned)
    exp = _experiment(wall, algorithms)
    exp.document.update(
        arms={"sha256": "arms", "document": doc}, arm=json.loads(json.dumps(arm.record())),
        stages_requested=list(arm.stages),
        profile={"path": PINNED_PROFILE, "sha256": sha256_file(REPO / PINNED_PROFILE)},
    )  # fmt: skip
    for run in exp.runs.values():
        _bind(run.manifest, arm, algorithms)
    return exp


def test_arm_comparisons_must_be_registered_and_exact_needs_equal_scope() -> None:
    base, cand = _armed(1.0, "E2"), _armed(0.5, "E3")
    result = report.compare_experiments(base, cand, lane="exact")
    assert result["arms"]["id"] == "L04" and result["verdict"] == "adopt_eligible"
    assert result["arms"]["baseline_controls"].keys() == {"L02", "L03"}
    assert result["arms"]["candidate_controls"].keys() == {"L02", "L03", "L04"}
    with pytest.raises(report.LatencyReportError, match="pre-registered"):
        report.compare_experiments(base, cand, lane="heuristic")
    cand.document["arms"] = {"sha256": "other", "document": _arms_doc()}
    with pytest.raises(report.LatencyReportError, match="different arms files"):
        report.compare_experiments(base, cand, lane="exact")
    cand = _armed(0.5, "E3")
    cand.document["source"] = {"git_revision": "def", "git_dirty": False}
    with pytest.raises(report.LatencyReportError, match="share one source"):
        report.compare_experiments(base, cand, lane="exact")
    # An arms file registering an exact comparison between different overlays: each arm
    # matches its registration, but the exact lane still refuses the scope difference.
    doc = _arms_doc()
    next(a for a in doc["arms"] if a["name"] == "H4")["shortlist"]["routes_per_probe"] = 2
    with pytest.raises(report.LatencyReportError, match="heuristic settings"):
        report.compare_experiments(_armed(1.0, "H2", doc), _armed(0.5, "H4", doc), lane="exact")
    with pytest.raises(report.LatencyReportError, match="only compared with another arm"):
        report.compare_experiments(_experiment(1.0), cand, lane="exact")


def test_empty_declared_controls_are_refused_even_when_every_record_agrees() -> None:
    """R1-F1: E2/E3 with explicitly empty controls -- the reviewer's scenario, set AFTER
    valid construction so a helper change can never make this vacuous -- are refused,
    also when the runs' effective profiles were relabelled consistently."""
    base, cand = _armed(1.0, "E2"), _armed(0.5, "E3")
    assert report.compare_experiments(base, cand, lane="exact")["verdict"] == "adopt_eligible"
    for exp in (base, cand):
        exp.document["arm"]["controls"] = {}
    with pytest.raises(
        report.LatencyReportError,
        match=r"baseline arm 'E2' does not match .*declared controls \{\}",
    ):
        report.compare_experiments(base, cand, lane="exact")
    base = _armed(1.0, "E2")
    reference_path = latency.Arm("E3", None, {}, (), None, None).quote_path()
    manifests = [run.manifest for run in cand.runs.values()]
    relabelled = [m.measurement["arm"] for m in manifests]
    relabelled += [m.resolved_profile["l08_arm"] for m in manifests]
    for arm in (cand.document["arm"], *relabelled):
        arm.update(controls={}, quote_path=reference_path)
    with pytest.raises(report.LatencyReportError, match="candidate arm 'E3'.*declared controls"):
        report.compare_experiments(base, cand, lane="exact")


def _first(exp: report.Experiment) -> Any:
    return exp.runs[("timing", "fixed", "full_source/matrix")].manifest


E_PAIR = ("E2", "E3", "exact", None)  # L04
H_PAIR = ("S0", "H4", "heuristic", {"uni_sor_fast": "uni_sor_port"})  # L06-L07-with-exact-controls


@pytest.mark.parametrize(
    ("pair", "mutate", "message"),
    [
        (E_PAIR, lambda e: e.document["arm"]["controls"]["L03"].update(tick_capacity=8192),
         "declared controls"),
        (E_PAIR, lambda e: e.document["arm"]["controls"].pop("L04"), "declared controls"),
        (E_PAIR, lambda e: e.document["arm"].update(algorithms=["direct"]), "declared algorithms"),
        (E_PAIR, lambda e: e.document["arm"].update(stages=["timing", "cold", "quote_cli"]),
         "declared stages"),
        (E_PAIR, lambda e: e.document["arm"].update(name="E9"), "not registered"),
        (E_PAIR, lambda e: e.document.update(stages_requested=["timing"]), "stages_requested"),
        (E_PAIR, lambda e: e.document.update(algorithms=["direct"]), "algorithms"),
        (E_PAIR, lambda e: e.document["arms"]["document"]["controls"]["L03"].update(
            tick_capacity=0), "does not resolve"),
        (E_PAIR, lambda e: e.document.update(profile={"path": PINNED_PROFILE, "sha256": "x"}),
         "reference` unresolvable"),
        # recorded effective profile disagreeing with the (valid) declaration
        (E_PAIR, lambda e: _first(e).measurement["arm"].update(controls={}), "measurement arm"),
        (E_PAIR, lambda e: _first(e).resolved_profile["l08_arm"]["controls"]["L04"].update(
            max_keys=1), "effective profile l08_arm"),
        (E_PAIR, lambda e: _first(e).resolved_profile.update(algorithms=["direct"]),
         "effective profile algorithms"),
        (E_PAIR, lambda e: setattr(_first(e), "algorithms", ("direct",)), "run algorithms"),
        (E_PAIR, lambda e: _first(e).resolved_profile["algorithm_config"]["direct"][
            "provenance"].update(quote_path="default reference"), "quote path"),
        # heuristic overlays (shortlist / sampling) and their effective settings
        (H_PAIR, lambda e: e.document["arm"]["shortlist"].update(routes_per_probe=2),
         "declared shortlist"),
        (H_PAIR, lambda e: e.document["arm"].update(sampling=None), "declared sampling"),
        (H_PAIR, lambda e: _first(e).resolved_profile.pop("sampling"), "effective sampling"),
        (H_PAIR, lambda e: _first(e).resolved_profile["algorithm_config"]["uni_sor_fast"][
            "params"].update(coarse_step=10), "params \\['coarse_step'\\]"),
    ],
)  # fmt: skip
def test_arm_settings_and_effective_profiles_must_match_the_registration(
    pair: tuple[str, str, str, Any], mutate: Any, message: str
) -> None:
    baseline, candidate, lane, pairs = pair
    base, cand = _armed(1.0, baseline), _armed(0.5, candidate)
    report.compare_experiments(base, cand, lane=lane, pairs=pairs)  # valid before the mutation
    mutate(cand)
    with pytest.raises(report.LatencyReportError, match=f"candidate arm .*{message}"):
        report.compare_experiments(base, cand, lane=lane, pairs=pairs)


def test_sufficient_evidence_must_measure_the_registered_arm_settings() -> None:
    exp = _armed(0.5, "E3")
    arm = latency.parse_arms(_arms_doc(), "test arms", "arms").arms["E3"]
    evidence = _evidence(exp, _unbounded())
    evidence.document.update(arm=json.loads(json.dumps(exp.document["arm"])),
                             arms=exp.document["arms"])  # fmt: skip
    _bind(evidence.runs["full_source"].manifest, arm, exp.algorithms)
    assert report.sufficient_problems(evidence, exp) == []
    evidence.document["arm"]["controls"] = {}  # same name, reference-path settings
    assert any("arm E3: declared controls" in p for p in report.sufficient_problems(evidence, exp))
    evidence.document["arm"] = json.loads(json.dumps(exp.document["arm"]))
    evidence.runs["full_source"].manifest.resolved_profile["l08_arm"]["controls"] = {}
    assert any("effective profile l08_arm" in p for p in report.sufficient_problems(evidence, exp))

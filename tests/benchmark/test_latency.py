"""L01 latency driver and summaries (WHI-1503).

Boundary tests put the same artificial work in `prepare` or in `solve`, as burned CPU or as
sleep, plus a delayed parent-side evaluation, and check that each cost is charged to the
stage that incurred it -- so an optimization cannot make work disappear by moving it across
a timing boundary. An end-to-end run over the checked-in corpus fixture exercises every
stage, including `main.py quote` (still exactly one solve per algorithm per invocation).
"""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import signal
import time
from collections.abc import Iterator
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
    # A/A comparison of the experiment with itself: exact semantics, but a dirty test tree
    # can never yield an adopt verdict.
    result = report.compare(out, out, lane="exact")
    assert result["semantic_mismatches"] == [] and result["work_differences"] == []
    assert result["coverage_problems"] == {"baseline": [], "candidate": [], "pairing": []}
    assert result["verdict"] == "inconclusive"
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


def _experiment(wall: float) -> report.Experiment:
    protocol: dict[str, Any] = yaml.safe_load((REPO / "config/latency/l01.yaml").read_text())
    protocol.update(
        matrix=[{"case": "t1", "split": "tuning", "covers": []},
                {"case": "h1", "split": "held_out", "covers": []},
                {"case": "h2", "split": "held_out", "covers": []}],
        cohorts=["full_source"], cold={"cohorts": ["full_source"]}, quote_cli={"invocations": 0},
    )  # fmt: skip
    runs = {}
    for stage, order, label in RUN_KEYS:
        schedule = [[a, c] for a in ALGS for c in CASES[label]]
        attempts = {"samples": 1, "warmup": 0} if stage == "cold" else {"samples": 5, "warmup": 1}
        manifest = SimpleNamespace(
            algorithms=ALGS, state="complete", bundle_hash=f"hash-{label}",
            scheduled_count=len(schedule),
            measurement={"schedule": schedule[::-1] if order == "reverse" else schedule,
                         "warmup": attempts["warmup"], "repeats": attempts["samples"]},
            prepare_events=({"index": 0, "algorithm": "direct", "status": "ok",
                             "startup_seconds": 0.2, "prepare_seconds": 0.01},),
        )  # fmt: skip
        records = [_rec(a, c, wall, **attempts) for a, c in schedule]
        runs[(stage, order, label)] = report.RunView({}, manifest, records, [], Path(stage))  # type: ignore[arg-type]
    document = {
        "experiment_id": f"e{wall}", "partial": False, "stages_requested": list(latency.STAGES),
        "protocol": {"sha256": "p", "document": protocol}, "algorithms": list(ALGS),
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


def _evidence(exp: report.Experiment, *records: dict[str, Any]) -> report.SufficientEvidence:
    schedule = [[r["algorithm"], r["case_id"]] for r in records]
    manifest = SimpleNamespace(bundle_hash="hash-full_source/matrix", state="complete",
                               measurement={"schedule": schedule})  # fmt: skip
    document = {
        "protocol": {"sha256": exp.document["protocol"]["sha256"]},
        "source": dict(exp.document["source"]), "profile": dict(exp.document["profile"]),
        "bundles": {"full_source/matrix": {"bundle_hash": "hash-full_source/matrix"}},
        "sufficient_budget": {"sha256": "sb"},
    }  # fmt: skip
    run = report.RunView({}, manifest, list(records), [], Path("sb"))  # type: ignore[arg-type]
    return report.SufficientEvidence(Path("."), document, {"full_source": run})


def _unbounded(**extra: Any) -> dict[str, Any]:  # re-solved with the quote cap raised
    return _rec("single_path", "h2", 2.0, samples=2, warmup=0, **extra)


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

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
            assert min(walls) >= D and min(cpus) >= D / 4  # burned CPU is CPU time
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
    summary = report.summarize(out)
    semantic = summary["semantic"]["full_source/matrix"]
    assert semantic["status_counts"]["direct"] == {"ok": 2, "no_route": 1}  # failure kept
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
    rendered = report.render_summary(summary).replace(report.NO_TAIL_CLAIM, "")
    assert not re.search(r"p9\d|percentile|\bSLA\b", rendered, re.IGNORECASE)
    # A/A comparison of the experiment with itself: exact semantics, but a dirty test tree
    # can never yield an adopt verdict.
    result = report.compare(out, out, lane="exact")
    assert result["semantic_mismatches"] == [] and result["work_differences"] == []
    assert result["verdict"] == "inconclusive"


# ------------------------------------------------------------ acceptance rules


def _t(verdict: str) -> dict[str, Any]:
    return {"verdict": verdict}


@pytest.mark.parametrize(
    ("kwargs", "verdict"),
    [
        (dict(lane="exact", semantic_mismatches=1, timing={"a": _t("faster")}), "reject"),
        (dict(lane="exact", semantic_mismatches=0, timing={"a": _t("faster")}), "adopt_eligible"),
        (dict(lane="exact", semantic_mismatches=0, timing={"a": _t("faster"),
                                                           "b": _t("slower")}), "reject"),
        (dict(lane="exact", semantic_mismatches=0, timing={"a": _t("no_worthwhile_change")}),
         "reject"),
        (dict(lane="heuristic", semantic_mismatches=3, timing={"a": _t("faster")}),
         "opt_in_only"),
        (dict(lane="exact", semantic_mismatches=0, timing={"a": _t("faster")},
              contaminated=True), "inconclusive"),
        (dict(lane="exact", semantic_mismatches=0, timing={"a": _t("faster")},
              evidence_clean=False), "inconclusive"),
    ],
)  # fmt: skip
def test_judge_applies_the_preregistered_rules(kwargs: dict[str, Any], verdict: str) -> None:
    facts: dict[str, Any] = {"evidence_clean": True, "contaminated": False, **kwargs}
    assert report.judge(**facts)[0] == verdict


def test_timing_verdict_needs_wall_and_cpu_beyond_the_threshold() -> None:
    assert report._timing_verdict({"wall": 0.2, "cpu": 0.15}, 0.1) == "faster"
    assert report._timing_verdict({"wall": 0.2, "cpu": 0.05}, 0.1) == "no_worthwhile_change"
    assert report._timing_verdict({"wall": 0.0, "cpu": -0.2}, 0.1) == "slower"
    assert report._timing_verdict({"wall": None, "cpu": 0.5}, 0.1) == "insufficient_cases"


def test_regret_is_against_the_same_scope_best_known_and_unknown_scores_are_na() -> None:
    def rec(algorithm: str, status: str, score: int | None) -> dict[str, Any]:
        text = None if score is None else str(score)
        return {"algorithm": algorithm, "case_id": "c", "status": status, "score": text}

    run = SimpleNamespace(records=[rec("a", "ok", 1000), rec("b", "ok", 990),
                                   rec("c", "ok", None), rec("d", "no_route", None)])  # fmt: skip
    block = report._quality_block(run)  # type: ignore[arg-type]
    row = block["cases"]["c"]
    assert row["best_known_score"] == "1000"
    assert row["a"]["regret_bps"] == 0 and row["b"]["regret_bps"] == 100
    assert row["c"]["regret_bps"] is None and row["d"]["regret_bps"] is None
    assert block["per_algorithm"]["c"]["not_applicable"] == 1

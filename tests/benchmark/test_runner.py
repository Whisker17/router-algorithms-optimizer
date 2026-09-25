"""Integration tests for `benchmark.runner.run_experiment` (WHI-1427 + WHI-1437).

The recorded result always comes from the runner's own independent re-evaluation
of the submitted plan, never a trusted solver claim (docs/DESIGN.md §2.5, §4.4),
and every scheduled case ends with a terminal or explicit `cancelled` outcome even
when solvers hang, crash, raise, leak state or blow their budgets (docs/DESIGN.md
§2.10, §4.5). Solvers run in real spawned worker processes; every hard limit in
these tests is well under a second and each test is additionally bounded by an
alarm, so a runner regression cannot hang the suite.
"""

from __future__ import annotations

import json
import multiprocessing
import signal
import time
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
from typing import Any

import fake_solvers  # sibling module: importable by spawned workers via sys.path
import pytest

import main
from benchmark.objective import gross_only
from benchmark.profile import CaseOrder, MeasurementSettings, RunProfile, WorkerSettings
from benchmark.results import (
    ResultError,
    load_case_records,
    load_manifest,
    load_memory_records,
)
from benchmark.runner import (
    RunInterrupted,
    case_seed,
    compare_runs,
    ordered_cases,
    run_experiment,
)
from routing.algorithms.base import AlgorithmFactory, Budget
from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle

BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)
TEST_ALARM_SECONDS = 20  # hard per-test ceiling; typical tests finish in < 2 s
TIME_LIMIT = 0.5  # hard per-attempt limit used for hanging solvers

POOL = ConstantProductPoolState(
    pool_id="pool_a",
    token0="TKA",
    token1="TKB",
    reserve0=1_000_000,
    reserve1=3_000_000,
    fee_bps=30,
)


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
        for child in multiprocessing.active_children():  # never leak a worker
            child.kill()


@pytest.fixture(autouse=True)
def _register_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    import routing.algorithms.registry as registry

    for factory in fake_solvers.ALL:
        monkeypatch.setitem(registry.ALGORITHMS, factory.name, factory)


def _bundle(*case_ids: str) -> SnapshotBundle:
    ids = case_ids or ("c_ok", "c_no_route")
    cases = tuple(
        Case(
            case_id=cid,
            token_in="TKA",
            token_out="TKD" if cid == "c_no_route" else "TKB",
            amount_in=100_000 + 1_000 * i,
        )
        for i, cid in enumerate(ids)
    )
    return SnapshotBundle(
        bundle_id="b1",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools={"pool_a": POOL},
        cases=cases,
        bundle_hash="deadbeef",
        source_path="<test>",
    )


def _profile(
    *algorithms: str,
    time_limit: float = 5.0,
    max_quotes: int | None = None,
    max_candidates: int | None = None,
    warmup: int = 0,
    repeats: int = 1,
    order: CaseOrder = "fixed",
    memory_pass: bool = False,
    scope: str = "algorithm",
    prepare_limit: float = 5.0,
) -> RunProfile:
    return RunProfile(
        schema_version=2,
        algorithms=algorithms or ("direct",),
        objective=gross_only(),
        source_path="p.yaml",
        budget=Budget(
            time_limit_seconds=time_limit, max_quotes=max_quotes, max_candidates=max_candidates
        ),
        measurement=MeasurementSettings(
            warmup=warmup, repeats=repeats, seed=7, order=order, memory_pass=memory_pass
        ),
        worker=WorkerSettings(
            start_method="spawn",
            scope=scope,  # type: ignore[arg-type]
            prepare_time_limit_seconds=prepare_limit,
        ),
    )


def _run(tmp_path: Path, bundle: SnapshotBundle, profile: RunProfile, **kwargs: Any) -> Any:
    return run_experiment(
        bundle, profile, results_dir=tmp_path / "results", replay_command="cmd", **kwargs
    )


def _by_case(run_dir: str | Path) -> dict[str, dict[str, Any]]:
    return {r["case_id"]: r for r in load_case_records(run_dir)}


# ------------------------------------------------------------ basic behavior


def test_run_experiment_solves_every_case_with_every_algorithm(tmp_path: Path) -> None:
    manifest = _run(tmp_path, _bundle(), _profile("direct", "seed_echo"))
    reloaded = load_manifest(Path(manifest.run_dir))
    assert reloaded.complete and reloaded.state == "complete"
    assert reloaded.case_count == reloaded.scheduled_count == 4
    records = load_case_records(manifest.run_dir)
    assert {(r["algorithm"], r["case_id"]) for r in records} == {
        (a, c) for a in ("direct", "seed_echo") for c in ("c_ok", "c_no_route")
    }
    by_case = {r["case_id"]: r for r in records if r["algorithm"] == "direct"}
    assert by_case["c_ok"]["status"] == "ok"
    assert by_case["c_ok"]["evaluation"]["gross_output"] == "271983"
    assert by_case["c_no_route"]["status"] == "no_route"


def test_run_experiment_new_run_id_each_call(tmp_path: Path) -> None:
    m1 = _run(tmp_path, _bundle(), _profile())
    m2 = _run(tmp_path, _bundle(), _profile())
    assert m1.run_id != m2.run_id
    assert Path(m1.run_dir).exists() and Path(m2.run_dir).exists()


def test_run_experiment_refuses_to_overwrite_an_existing_run(tmp_path: Path) -> None:
    _run(tmp_path, _bundle(), _profile(), run_id="fixed")
    with pytest.raises(ResultError, match="already exists"):
        _run(tmp_path, _bundle(), _profile(), run_id="fixed")


def test_run_experiment_ignores_a_lying_solver_and_records_the_independent_truth(
    tmp_path: Path,
) -> None:
    manifest = _run(tmp_path, _bundle(), _profile("lying_solver"))
    by_case = _by_case(manifest.run_dir)

    ok_case = by_case["c_ok"]
    # The solver claimed gross_output=999,999,999; the true, independently
    # evaluated output for this plan is 271,983 (tests/pools/test_constant_product.py).
    assert ok_case["status"] == "ok"
    assert ok_case["evaluation"]["gross_output"] == "271983"
    assert ok_case["score"] == "271983"
    assert ok_case["solver_reported"]["gross_output"] == "999999999"
    assert ok_case["solver_reported"]["score"] == "999999999"

    no_route_case = by_case["c_no_route"]
    # It claimed ok for a structurally invalid plan (pool_a does not trade TKD);
    # the runner's independent evaluation overrides that to invalid_plan.
    assert no_route_case["status"] == "invalid_plan"
    assert no_route_case["score"] is None
    assert no_route_case["evaluation"]["status"] == "invalid_plan"
    assert no_route_case["solver_reported"]["status"] == "ok"
    assert no_route_case["solver_reported"]["gross_output"] == "42"


def test_warmup_and_repeats_produce_only_measured_samples(tmp_path: Path) -> None:
    manifest = _run(tmp_path, _bundle(), _profile(warmup=2, repeats=3))
    for record in load_case_records(manifest.run_dir):
        m = record["measurement"]
        assert m["warmup"] == 2 and m["repeats"] == 3
        assert m["attempts_completed"] == 5
        assert len(m["solve_seconds"]) == 3
        assert all(s > 0 for s in m["solve_seconds"])
        assert m["attempts_consistent"] is True
        assert m["instrumented"] is False
        # The runner's own evaluation is timed separately from solver latency.
        assert "evaluation_seconds" in m
    ok = _by_case(manifest.run_dir)["c_ok"]
    assert ok["quotes"] == {"attempted": 1, "counted": 1}
    per_alg = manifest.timing["per_algorithm"]["direct"]
    assert per_alg["cases"] == 2
    assert per_alg["solve_seconds_total"] == pytest.approx(
        sum(sum(r["measurement"]["solve_seconds"]) for r in load_case_records(manifest.run_dir))
    )


def test_manifest_records_environment_and_resolved_profile(tmp_path: Path) -> None:
    manifest = _run(tmp_path, _bundle(), _profile(time_limit=3.0, max_quotes=9))
    env = manifest.environment
    for key in ("cpu_model", "cpu_count", "os", "platform", "python_version", "dependencies"):
        assert key in env
    assert env["clock"]["monotonic"] is True
    assert env["worker"] == {
        "start_method": "spawn",
        "scope": "algorithm",
        "prepare_time_limit_seconds": 5.0,
    }
    assert "pyyaml" in env["dependencies"]
    assert manifest.resolved_profile["budget"] == {
        "time_limit_seconds": 3.0,
        "max_quotes": 9,
        "max_candidates": None,
    }
    assert manifest.measurement["case_order"] == ["c_ok", "c_no_route"]


# ------------------------------------------------------------ hard failures


def test_hanging_solver_is_killed_and_cannot_stall_or_contaminate_the_next_case(
    tmp_path: Path,
) -> None:
    started = time.monotonic()
    profile = _profile("hang", time_limit=TIME_LIMIT)
    manifest = _run(tmp_path, _bundle("c1", "c_hang", "c2"), profile)
    assert time.monotonic() - started < 8

    by_case = _by_case(manifest.run_dir)
    hang = by_case["c_hang"]
    assert hang["status"] == "timeout"
    assert hang["limit_hit"] == "time"
    assert hang["evaluation"] is None and hang["score"] is None
    assert hang["measurement"]["elapsed_seconds"] >= TIME_LIMIT
    assert hang["measurement"]["solve_seconds"] == []
    # The candidate it published before hanging is kept -- independently
    # evaluated and separately labeled, never as the outcome.
    lvc = hang["last_valid_candidate"]
    assert lvc is not None and "not a completed solve" in lvc["label"]
    assert lvc["evaluation"]["status"] == "ok"
    assert int(lvc["score"]) > 0

    # The hang poisoned its process's module state; the next case ran in a
    # brand-new worker and is unaffected.
    assert by_case["c2"]["status"] == "ok"
    assert by_case["c2"]["measurement"]["worker_id"] != hang["measurement"]["worker_id"]
    assert [e["reason"] for e in manifest.prepare_events] == ["initial", "replacement"]
    assert manifest.status_counts == {"ok": 2, "timeout": 1}


def test_a_solver_flooding_candidates_still_hits_the_time_limit(tmp_path: Path) -> None:
    started = time.monotonic()
    manifest = _run(tmp_path, _bundle("c1"), _profile("candidate_spam", time_limit=TIME_LIMIT))
    assert time.monotonic() - started < 8
    record = _by_case(manifest.run_dir)["c1"]
    assert record["status"] == "timeout" and record["limit_hit"] == "time"
    assert record["measurement"]["candidates_reported"] > 0
    assert record["last_valid_candidate"] is not None


def test_crashing_solver_is_recorded_and_its_worker_replaced(tmp_path: Path) -> None:
    manifest = _run(tmp_path, _bundle("c1", "c_crash", "c2"), _profile("crash"))
    by_case = _by_case(manifest.run_dir)
    assert by_case["c_crash"]["status"] == "algorithm_error"
    assert "exit code 17" in by_case["c_crash"]["error"]
    assert by_case["c_crash"]["solver_reported"] is None
    assert by_case["c1"]["status"] == "ok"
    assert by_case["c2"]["status"] == "ok"  # not POISONED
    assert len(manifest.prepare_events) == 2


def test_raising_solver_is_an_algorithm_error_and_the_worker_is_replaced(
    tmp_path: Path,
) -> None:
    manifest = _run(tmp_path, _bundle("c_raise", "c2"), _profile("raise", warmup=1, repeats=2))
    by_case = _by_case(manifest.run_dir)
    assert by_case["c_raise"]["status"] == "algorithm_error"
    assert "RuntimeError: boom" in by_case["c_raise"]["error"]
    assert by_case["c_raise"]["error"].startswith("warmup 1/1")
    assert by_case["c2"]["status"] == "ok"


def test_quote_limit_is_hard_and_cannot_be_swallowed(tmp_path: Path) -> None:
    profile = _profile("quote_hog", "limit_swallower", max_quotes=5)
    manifest = _run(tmp_path, _bundle("c1", "c2"), profile)
    records = load_case_records(manifest.run_dir)
    assert len(records) == 4
    for record in records:
        assert record["status"] == "timeout"
        assert record["limit_hit"] == "quotes"
        assert record["quotes"] == {"attempted": 6, "counted": 5}


def test_direct_declares_budget_truncation_instead_of_quoting_past_the_limit(
    tmp_path: Path,
) -> None:
    bundle = SnapshotBundle(
        **{
            **_bundle("c1").__dict__,
            "pools": {
                "pool_a": POOL,
                "pool_b": ConstantProductPoolState(
                    pool_id="pool_b",
                    token0="TKA",
                    token1="TKB",
                    reserve0=2_000_000,
                    reserve1=3_000_000,
                    fee_bps=30,
                ),
            },
        }
    )
    manifest = _run(tmp_path, bundle, _profile(max_quotes=1))
    record = _by_case(manifest.run_dir)["c1"]
    assert record["status"] == "ok"
    assert record["candidates_considered"] == 1
    assert record["candidates_truncated"] == 1
    assert record["quotes"] == {"attempted": 1, "counted": 1}


def test_garbage_and_misattributed_results_are_algorithm_errors(tmp_path: Path) -> None:
    manifest = _run(tmp_path, _bundle("c1"), _profile("garbage", "wrong_case"))
    records = {r["algorithm"]: r for r in load_case_records(manifest.run_dir)}
    assert records["garbage"]["status"] == "algorithm_error"
    assert "not SolveResult" in records["garbage"]["error"]
    assert records["wrong_case"]["status"] == "algorithm_error"
    assert "someone_else" in records["wrong_case"]["error"]


def test_unpicklable_factory_is_an_algorithm_error_not_a_runner_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import routing.algorithms.registry as registry

    monkeypatch.setitem(
        registry.ALGORITHMS,
        "closure",
        AlgorithmFactory(name="closure", solve=lambda case, context, budget: None),  # type: ignore[arg-type]
    )
    manifest = _run(tmp_path, _bundle("c1", "c2"), _profile("closure"))
    records = load_case_records(manifest.run_dir)
    assert [r["status"] for r in records] == ["algorithm_error", "algorithm_error"]
    assert "could not be started" in records[0]["error"]
    assert len(manifest.prepare_events) == 1  # a failed prepare is not retried per case


# ------------------------------------------------------------ preparation


def test_prepare_cost_is_charged_separately_and_never_hidden(tmp_path: Path) -> None:
    manifest = _run(tmp_path, _bundle("c1", "c2", "c3"), _profile("indexed", repeats=2))
    (event,) = manifest.prepare_events
    assert event["status"] == "ok" and event["reason"] == "initial"
    assert event["prepare_seconds"] >= fake_solvers.PREPARE_SECONDS
    assert event["startup_seconds"] >= event["prepare_seconds"]
    for record in load_case_records(manifest.run_dir):
        assert record["status"] == "ok"
        assert record["measurement"]["prepare_event"] == 0
        assert all(s < fake_solvers.PREPARE_SECONDS for s in record["measurement"]["solve_seconds"])
    totals = manifest.timing["per_algorithm"]["indexed"]
    assert totals["prepare_count"] == 1
    assert totals["prepare_seconds_total"] == pytest.approx(event["prepare_seconds"])
    assert totals["amortized_prepare_seconds_per_case"] == pytest.approx(
        event["prepare_seconds"] / 3
    )


def test_case_scope_prepares_a_fresh_worker_per_case(tmp_path: Path) -> None:
    manifest = _run(tmp_path, _bundle("c1", "c2"), _profile("indexed", scope="case"))
    assert [e["reason"] for e in manifest.prepare_events] == ["initial", "per_case"]
    assert manifest.timing["per_algorithm"]["indexed"]["prepare_count"] == 2
    worker_ids = {r["measurement"]["worker_id"] for r in load_case_records(manifest.run_dir)}
    assert len(worker_ids) == 2


def test_prepare_timeout_ends_every_case_of_that_algorithm_once(tmp_path: Path) -> None:
    started = time.monotonic()
    manifest = _run(
        tmp_path,
        _bundle("c1", "c2", "c3"),
        _profile("slow_prepare", "direct", prepare_limit=TIME_LIMIT),
    )
    assert time.monotonic() - started < 8
    records = load_case_records(manifest.run_dir)
    slow = [r for r in records if r["algorithm"] == "slow_prepare"]
    assert [r["status"] for r in slow] == ["timeout"] * 3
    assert all(r["limit_hit"] == "prepare_time" for r in slow)
    events = [e for e in manifest.prepare_events if e["algorithm"] == "slow_prepare"]
    assert len(events) == 1 and events[0]["status"] == "timeout"
    # Another algorithm in the same run is unaffected.
    assert {r["status"] for r in records if r["algorithm"] == "direct"} == {"ok"}


def test_prepare_error_is_an_algorithm_error(tmp_path: Path) -> None:
    manifest = _run(tmp_path, _bundle("c1", "c2"), _profile("bad_prepare"))
    records = load_case_records(manifest.run_dir)
    assert [r["status"] for r in records] == ["algorithm_error"] * 2
    assert "cannot build index" in records[0]["error"]
    assert len(manifest.prepare_events) == 1


# ------------------------------------------------------------ order / state leaks


def test_order_helpers_are_deterministic() -> None:
    cases = _bundle("a", "b", "c", "d").cases
    assert [c.case_id for c in ordered_cases(cases, "reverse", 1)] == ["d", "c", "b", "a"]
    assert ordered_cases(cases, "shuffle", 3) == ordered_cases(cases, "shuffle", 3)
    assert sorted(ordered_cases(cases, "shuffle", 3), key=lambda c: c.case_id) == list(cases)
    assert case_seed(7, "direct", "a") == case_seed(7, "direct", "a")
    assert case_seed(7, "direct", "a") != case_seed(7, "direct", "b")
    assert case_seed(7, "direct", "a") < 2**53


def test_reversed_and_shuffled_order_preserve_deterministic_outputs(tmp_path: Path) -> None:
    bundle = _bundle("c1", "c_no_route", "c2", "c3")
    profile = _profile("direct", "seed_echo")
    fixed = _run(tmp_path, bundle, profile)
    reverse = _run(tmp_path, bundle, profile, order="reverse")
    shuffle = _run(tmp_path, bundle, profile, order="shuffle")
    assert reverse.measurement["case_order"] == ["c3", "c2", "c_no_route", "c1"]
    assert reverse.measurement["order"] == "reverse"
    assert compare_runs(fixed.run_dir, reverse.run_dir) == []
    assert compare_runs(fixed.run_dir, shuffle.run_dir) == []
    # Seeds are per (algorithm, case), not per position.
    seeds = {
        r["case_id"]: r["error"]
        for r in load_case_records(reverse.run_dir)
        if r["algorithm"] == "seed_echo"
    }
    assert seeds["c1"] == f"seed={case_seed(7, 'seed_echo', 'c1')}"


def test_order_check_detects_a_state_leaking_solver(tmp_path: Path) -> None:
    bundle = _bundle("c1", "c2", "c3")
    fixed = _run(tmp_path, bundle, _profile("stateful"))
    reverse = _run(tmp_path, bundle, _profile("stateful"), order="reverse")
    mismatches = compare_runs(fixed.run_dir, reverse.run_dir)
    assert any("stateful/c1" in m for m in mismatches)
    assert any("stateful/c3" in m for m in mismatches)


def test_repeats_expose_state_leaking_between_attempts(tmp_path: Path) -> None:
    manifest = _run(tmp_path, _bundle("c1"), _profile("stateful", repeats=2))
    record = _by_case(manifest.run_dir)["c1"]
    assert record["measurement"]["attempts_consistent"] is False


def test_case_scope_isolates_even_a_state_leaking_solver(tmp_path: Path) -> None:
    bundle = _bundle("c1", "c2", "c3")
    fixed = _run(tmp_path, bundle, _profile("stateful", scope="case"))
    reverse = _run(tmp_path, bundle, _profile("stateful", scope="case"), order="reverse")
    assert compare_runs(fixed.run_dir, reverse.run_dir) == []
    assert {r["status"] for r in load_case_records(fixed.run_dir)} == {"ok"}


# ------------------------------------------------------------ memory pass


def test_memory_pass_is_separate_from_timing_samples(tmp_path: Path) -> None:
    manifest = _run(tmp_path, _bundle("c1", "c2"), _profile("memory_hog", memory_pass=True))
    timing_records = load_case_records(manifest.run_dir)
    for record in timing_records:
        assert record["measurement"]["instrumented"] is False
        assert "solve_peak_bytes" not in json.dumps(record)
    memory = load_memory_records(manifest.run_dir)
    assert manifest.memory_record_count == 2 == len(memory)
    for record in memory:
        assert record["status"] == "measured"
        assert record["solve_peak_bytes"] >= 8 * 1024 * 1024
    timing_workers = {r["measurement"]["worker_id"] for r in timing_records}
    assert timing_workers.isdisjoint({r["worker_id"] for r in memory})
    assert [e["pass"] for e in manifest.prepare_events] == ["timing", "memory"]
    assert "prepare_peak_bytes" in manifest.prepare_events[1]
    assert "prepare_peak_bytes" not in manifest.prepare_events[0]
    assert manifest.timing["memory_pass_seconds"] > 0


def test_memory_pass_absent_unless_declared(tmp_path: Path) -> None:
    manifest = _run(tmp_path, _bundle("c1"), _profile())
    assert manifest.memory_record_count is None
    assert not (Path(manifest.run_dir) / "memory.jsonl").exists()


# ------------------------------------------------------------ interruption


def test_interrupted_run_keeps_completed_records_and_cancels_the_rest(tmp_path: Path) -> None:
    seen: list[str] = []

    def _interrupt_after_first(record: Any) -> None:
        seen.append(record.case_id)
        # While running, the on-disk manifest is explicitly incomplete.
        run_dir = next((tmp_path / "results").iterdir())
        on_disk = json.loads((run_dir / "manifest.json").read_text())
        assert on_disk["state"] == "running" and on_disk["complete"] is False
        with pytest.raises(ResultError, match="incomplete"):
            load_manifest(run_dir)
        assert len(load_case_records(run_dir)) == len(seen)
        raise KeyboardInterrupt

    profile = _profile("direct", "seed_echo", memory_pass=True)
    with pytest.raises(RunInterrupted) as info:
        _run(tmp_path, _bundle("c1", "c2", "c3"), profile, progress=_interrupt_after_first)

    manifest = info.value.manifest
    assert manifest.state == "interrupted" and not manifest.complete
    assert multiprocessing.active_children() == []
    with pytest.raises(ResultError, match="incomplete"):
        load_manifest(manifest.run_dir)
    reloaded = load_manifest(manifest.run_dir, allow_incomplete=True)  # checksums verified
    assert reloaded.case_count == reloaded.scheduled_count == 6

    records = load_case_records(manifest.run_dir)
    assert [r["measurement"]["schedule_index"] for r in records] == list(range(6))
    assert records[0]["status"] == "ok"
    assert [r["status"] for r in records[1:]] == ["cancelled"] * 5
    assert manifest.status_counts == {"cancelled": 5, "ok": 1}
    memory = load_memory_records(manifest.run_dir)
    assert [r["status"] for r in memory] == ["cancelled"] * 6


def test_interruption_during_a_hanging_solve_cancels_it_promptly(tmp_path: Path) -> None:
    def _interrupt(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGALRM, _interrupt)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        with pytest.raises(RunInterrupted) as info:
            _run(tmp_path, _bundle("c1", "c_hang", "c2"), _profile("hang", time_limit=10.0))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    records = load_case_records(info.value.manifest.run_dir)
    assert [r["status"] for r in records] == ["ok", "cancelled", "cancelled"]
    assert multiprocessing.active_children() == []


# ------------------------------------------------------------ CLI


def test_cli_run_with_reverse_order_and_order_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results"
    base = ["run", "--bundle", "tests/fixtures/synthetic", "--profile", "config/smoke.yaml"]
    assert main.main([*base, "--results-dir", str(results)]) == 0
    assert main.main([*base, "--results-dir", str(results), "--order", "reverse"]) == 0
    run_a, run_b = sorted(results.iterdir())
    manifest_b = load_manifest(run_b)
    assert manifest_b.replay_command.endswith("--order reverse")
    assert manifest_b.measurement["order"] == "reverse"
    capsys.readouterr()
    assert main.main(["order-check", str(run_a), str(run_b)]) == 0
    assert "agree" in capsys.readouterr().out

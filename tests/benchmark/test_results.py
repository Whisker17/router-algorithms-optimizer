"""Unit tests for `benchmark.results`: run ids never collide/overwrite, a saved
manifest round-trips through `load_manifest` with full provenance,
`CaseRecord.to_dict` keeps the solver's own claim clearly separate as a
diagnostic, and the append-only `RunWriter` leaves an explicitly incomplete,
crash-tolerant run when it never finalizes (WHI-1437).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from benchmark.objective import gross_only
from benchmark.profile import MeasurementSettings, RunProfile, WorkerSettings
from benchmark.results import (
    CaseRecord,
    ResultError,
    RunWriter,
    environment_record,
    load_case_records,
    load_manifest,
    load_memory_records,
    new_run_id,
    save_run,
)
from routing.algorithms.base import Budget, SolveStatus
from snapshot.models import BlockRef, SnapshotBundle

BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)


def _bundle() -> SnapshotBundle:
    return SnapshotBundle(
        bundle_id="b1",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools={},
        cases=(),
        bundle_hash="deadbeef",
        source_path="<test>",
    )


def _profile() -> RunProfile:
    return RunProfile(
        schema_version=2,
        algorithms=("direct",),
        objective=gross_only(),
        source_path="p.yaml",
        budget=Budget(time_limit_seconds=1.0, max_quotes=10, max_candidates=None),
        measurement=MeasurementSettings(
            warmup=0, repeats=1, seed=0, order="fixed", memory_pass=False
        ),
        worker=WorkerSettings(
            start_method="spawn", scope="algorithm", prepare_time_limit_seconds=5.0
        ),
    )


def _record() -> CaseRecord:
    return CaseRecord(
        case_id="c1",
        algorithm="direct",
        status=SolveStatus.OK,
        evaluation=None,
        score=None,
        candidates_considered=1,
        error=None,
        solver_reported_status=SolveStatus.OK,
        solver_reported_gross_output=None,
        solver_reported_score=None,
    )


def test_new_run_id_is_unique_across_calls() -> None:
    assert new_run_id() != new_run_id()


def test_save_run_writes_and_round_trips(tmp_path: Path) -> None:
    manifest = save_run(
        tmp_path,
        bundle=_bundle(),
        profile=_profile(),
        results=[_record()],
        replay_command="uv run python main.py run --bundle b --profile p",
    )
    reloaded = load_manifest(Path(manifest.run_dir))
    assert reloaded.run_id == manifest.run_id
    assert reloaded.bundle_id == "b1"
    assert reloaded.case_count == 1
    assert reloaded.cases_sha256 == manifest.cases_sha256
    assert reloaded.resolved_profile == manifest.resolved_profile
    assert reloaded.python_version == manifest.python_version


def test_save_run_records_resolved_profile_values(tmp_path: Path) -> None:
    manifest = save_run(
        tmp_path,
        bundle=_bundle(),
        profile=_profile(),
        results=[_record()],
        replay_command="cmd",
    )
    assert manifest.resolved_profile == {
        "schema_version": 2,
        "algorithms": ["direct"],
        "objective": {"mode": "gross_only", "fixed_cost": 0},
        "budget": {"time_limit_seconds": 1.0, "max_quotes": 10, "max_candidates": None},
        "measurement": {
            "warmup": 0,
            "repeats": 1,
            "seed": 0,
            "order": "fixed",
            "memory_pass": False,
        },
        "worker": {
            "start_method": "spawn",
            "scope": "algorithm",
            "prepare_time_limit_seconds": 5.0,
        },
        "search": {},
        "graph": {},
        "algorithm_config": {
            "direct": {
                "capabilities": {"multi_hop": False, "split": False, "shared_pools": False},
                "params": {},
            }
        },
    }


def test_save_run_records_python_version(tmp_path: Path) -> None:
    manifest = save_run(
        tmp_path, bundle=_bundle(), profile=_profile(), results=[_record()], replay_command="cmd"
    )
    assert manifest.python_version == sys.version.split()[0]


def test_save_run_records_git_revision_in_this_repo_checkout(tmp_path: Path) -> None:
    # This test runs inside the actual repo worktree, which is a real git
    # checkout, so a revision must be recorded (never silently dropped for a
    # checkout that *does* have git available).
    manifest = save_run(
        tmp_path, bundle=_bundle(), profile=_profile(), results=[_record()], replay_command="cmd"
    )
    assert manifest.git_revision is not None
    assert len(manifest.git_revision) == 40  # full SHA-1 hex
    assert manifest.git_dirty in (True, False)


def test_save_run_tolerates_missing_profile_file(tmp_path: Path) -> None:
    # _profile().source_path ("p.yaml") does not exist on disk in this test;
    # save_run must record `profile_sha256=None` rather than raising.
    manifest = save_run(
        tmp_path, bundle=_bundle(), profile=_profile(), results=[_record()], replay_command="cmd"
    )
    assert manifest.profile_sha256 is None


def test_save_run_never_overwrites_existing_run_id(tmp_path: Path) -> None:
    save_run(
        tmp_path,
        bundle=_bundle(),
        profile=_profile(),
        results=[_record()],
        replay_command="cmd",
        run_id="fixed-run-id",
    )
    with pytest.raises(ResultError, match="already exists"):
        save_run(
            tmp_path,
            bundle=_bundle(),
            profile=_profile(),
            results=[_record()],
            replay_command="cmd",
            run_id="fixed-run-id",
        )


def test_load_manifest_detects_tampered_results(tmp_path: Path) -> None:
    manifest = save_run(
        tmp_path, bundle=_bundle(), profile=_profile(), results=[_record()], replay_command="cmd"
    )
    cases_path = Path(manifest.run_dir) / "cases.jsonl"
    cases_path.write_text(cases_path.read_text() + '{"tampered": true}\n')
    with pytest.raises(ResultError, match="checksum"):
        load_manifest(Path(manifest.run_dir))


def test_case_record_to_dict_keeps_solver_reported_separate() -> None:
    record = CaseRecord(
        case_id="c1",
        algorithm="direct",
        status=SolveStatus.INVALID_PLAN,
        evaluation=None,
        score=None,
        candidates_considered=1,
        error="residual balance",
        solver_reported_status=SolveStatus.OK,
        solver_reported_gross_output=999_999,
        solver_reported_score=999_999,
    )
    as_dict = record.to_dict()
    assert as_dict["status"] == "invalid_plan"
    assert as_dict["score"] is None
    assert as_dict["solver_reported"] == {
        "status": "ok",
        "gross_output": "999999",
        "score": "999999",
    }


def test_solver_reported_is_null_when_the_solver_never_answered() -> None:
    record = CaseRecord(
        case_id="c1",
        algorithm="direct",
        status=SolveStatus.TIMEOUT,
        evaluation=None,
        score=None,
        candidates_considered=0,
        error="killed",
        solver_reported_status=None,
        solver_reported_gross_output=None,
        solver_reported_score=None,
        limit_hit="time",
    )
    as_dict = record.to_dict()
    assert as_dict["solver_reported"] is None
    assert as_dict["limit_hit"] == "time"
    assert as_dict["quotes"] == {"attempted": None, "counted": None}


def _writer(tmp_path: Path, *, scheduled: int = 2, memory: bool = False) -> RunWriter:
    return RunWriter.create(
        tmp_path,
        bundle=_bundle(),
        profile=_profile(),
        replay_command="cmd",
        scheduled_count=scheduled,
        memory=memory,
        environment={"python_version": "x"},
    )


def test_an_unfinalized_run_is_explicitly_incomplete_but_readable(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.append(_record())
    with pytest.raises(ResultError, match="incomplete"):
        load_manifest(writer.run_dir)
    manifest = load_manifest(writer.run_dir, allow_incomplete=True)
    assert manifest.state == "running" and not manifest.complete
    assert len(load_case_records(writer.run_dir)) == 1


def test_a_torn_final_line_is_tolerated_only_while_running(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.append(_record())
    cases = writer.run_dir / "cases.jsonl"
    with open(cases, "a") as fh:
        fh.write('{"case_id": "c2", "algo')  # a crash mid-write
    assert [r["case_id"] for r in load_case_records(writer.run_dir)] == ["c1"]


def test_finalize_complete_requires_every_scheduled_record(tmp_path: Path) -> None:
    writer = _writer(tmp_path, scheduled=2)
    writer.append(_record())
    with pytest.raises(ResultError, match="not complete"):
        writer.finalize(state="complete", timing={})
    manifest = writer.finalize(state="interrupted", timing={})
    assert manifest.state == "interrupted"
    assert load_manifest(writer.run_dir, allow_incomplete=True).cases_sha256 == (
        manifest.cases_sha256
    )


def test_prepare_events_are_durable_before_finalize(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.add_prepare_event({"index": 0, "prepare_seconds": 0.5})
    on_disk = load_manifest(writer.run_dir, allow_incomplete=True)
    assert on_disk.prepare_events == ({"index": 0, "prepare_seconds": 0.5},)


def test_memory_records_live_in_their_own_checksummed_file(tmp_path: Path) -> None:
    writer = _writer(tmp_path, scheduled=1, memory=True)
    writer.append(_record())
    writer.append_memory({"case_id": "c1", "solve_peak_bytes": 123})
    manifest = writer.finalize(state="complete", timing={})
    assert manifest.memory_record_count == 1
    assert load_memory_records(writer.run_dir) == [{"case_id": "c1", "solve_peak_bytes": 123}]
    (writer.run_dir / "memory.jsonl").write_text("{}\n")
    with pytest.raises(ResultError, match="memory.jsonl checksum"):
        load_manifest(writer.run_dir)


def test_memory_records_refused_without_a_memory_pass(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    with pytest.raises(ResultError, match="memory pass"):
        writer.append_memory({})


def test_environment_record_names_machine_and_software() -> None:
    env = environment_record({"scope": "algorithm"})
    assert env["worker"] == {"scope": "algorithm"}
    assert env["clock"]["monotonic"] is True
    assert env["python_version"] == sys.version.split()[0]
    assert set(env["dependencies"]) == {"pycryptodome", "pyyaml"}

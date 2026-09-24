"""Unit tests for `benchmark.results`: run ids never collide/overwrite, a saved
manifest round-trips through `load_manifest` with full provenance, and
`CaseRecord.to_dict` keeps the solver's own claim clearly separate as a
diagnostic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from benchmark.objective import gross_only
from benchmark.profile import RunProfile
from benchmark.results import CaseRecord, ResultError, load_manifest, new_run_id, save_run
from routing.algorithms.base import SolveStatus
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
        schema_version=1, algorithms=("direct",), objective=gross_only(), source_path="p.yaml"
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
        "schema_version": 1,
        "algorithms": ["direct"],
        "objective": {"mode": "gross_only", "fixed_cost": 0},
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

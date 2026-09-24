"""Unit tests for `benchmark.results`: run ids never collide/overwrite, and a
saved manifest round-trips through `load_manifest`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmark.objective import gross_only
from benchmark.results import ResultError, load_manifest, new_run_id, save_run
from routing.algorithms.base import SolveResult, SolveStatus
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


def _result() -> SolveResult:
    return SolveResult(case_id="c1", algorithm="direct", status=SolveStatus.OK)


def test_new_run_id_is_unique_across_calls() -> None:
    assert new_run_id() != new_run_id()


def test_save_run_writes_and_round_trips(tmp_path: Path) -> None:
    manifest = save_run(
        tmp_path,
        bundle=_bundle(),
        profile_path="config/smoke.yaml",
        objective=gross_only(),
        algorithms=("direct",),
        results=[_result()],
        replay_command="uv run python main.py run --bundle b --profile p",
    )
    reloaded = load_manifest(Path(manifest.run_dir))
    assert reloaded.run_id == manifest.run_id
    assert reloaded.bundle_id == "b1"
    assert reloaded.case_count == 1
    assert reloaded.cases_sha256 == manifest.cases_sha256


def test_save_run_never_overwrites_existing_run_id(tmp_path: Path) -> None:
    save_run(
        tmp_path,
        bundle=_bundle(),
        profile_path="config/smoke.yaml",
        objective=gross_only(),
        algorithms=("direct",),
        results=[_result()],
        replay_command="cmd",
        run_id="fixed-run-id",
    )
    with pytest.raises(ResultError, match="already exists"):
        save_run(
            tmp_path,
            bundle=_bundle(),
            profile_path="config/smoke.yaml",
            objective=gross_only(),
            algorithms=("direct",),
            results=[_result()],
            replay_command="cmd",
            run_id="fixed-run-id",
        )


def test_load_manifest_detects_tampered_results(tmp_path: Path) -> None:
    manifest = save_run(
        tmp_path,
        bundle=_bundle(),
        profile_path="config/smoke.yaml",
        objective=gross_only(),
        algorithms=("direct",),
        results=[_result()],
        replay_command="cmd",
    )
    cases_path = Path(manifest.run_dir) / "cases.jsonl"
    cases_path.write_text(cases_path.read_text() + '{"tampered": true}\n')
    with pytest.raises(ResultError, match="checksum"):
        load_manifest(Path(manifest.run_dir))

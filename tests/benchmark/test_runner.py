"""Integration test for `benchmark.runner.run_experiment`: every case is solved
by every profiled algorithm, in fixed input order, and saved as one run.
"""

from __future__ import annotations

from pathlib import Path

from benchmark.objective import gross_only
from benchmark.profile import RunProfile
from benchmark.results import load_manifest
from benchmark.runner import run_experiment
from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle

BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)


def _bundle() -> SnapshotBundle:
    pool = ConstantProductPoolState(
        pool_id="pool_a",
        token0="TKA",
        token1="TKB",
        reserve0=1_000_000,
        reserve1=3_000_000,
        fee_bps=30,
    )
    cases = (
        Case(case_id="c_ok", token_in="TKA", token_out="TKB", amount_in=100_000),
        Case(case_id="c_no_route", token_in="TKA", token_out="TKD", amount_in=100_000),
    )
    return SnapshotBundle(
        bundle_id="b1",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools={"pool_a": pool},
        cases=cases,
        bundle_hash="deadbeef",
        source_path="<test>",
    )


def test_run_experiment_solves_every_case_with_every_algorithm(tmp_path: Path) -> None:
    profile = RunProfile(
        schema_version=1, algorithms=("direct",), objective=gross_only(), source_path="p.yaml"
    )
    manifest = run_experiment(
        _bundle(), profile, results_dir=tmp_path, replay_command="replay-cmd"
    )
    reloaded = load_manifest(Path(manifest.run_dir))
    assert reloaded.case_count == 2

    lines = (Path(manifest.run_dir) / "cases.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_run_experiment_new_run_id_each_call(tmp_path: Path) -> None:
    profile = RunProfile(
        schema_version=1, algorithms=("direct",), objective=gross_only(), source_path="p.yaml"
    )
    m1 = run_experiment(_bundle(), profile, results_dir=tmp_path, replay_command="cmd")
    m2 = run_experiment(_bundle(), profile, results_dir=tmp_path, replay_command="cmd")
    assert m1.run_id != m2.run_id
    assert Path(m1.run_dir).exists()
    assert Path(m2.run_dir).exists()

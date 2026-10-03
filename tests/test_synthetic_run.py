"""End-to-end CLI test: synthetic prepare -> validate -> run -> saved result
(docs/DESIGN.md §4.4 core flows). This is the offline command documented in the
issue's Testing/Verification section:

    uv run python main.py run --bundle tests/fixtures/synthetic --profile config/smoke.yaml \
        --strategies profile

(`--strategies profile`: the smoke profile's exact one-algorithm selection; the default
`all` would add the optimized strategies, which need `search.*` values smoke.yaml does not
declare -- WHI-1528.)
Also proves the WHI-1427 acceptance criteria: a documented offline command
produces the expected integer output and identical deterministic replay, and no
credentials/network are needed for synthetic prepare/validate/run.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

import main
from benchmark.results import load_manifest
from snapshot.bundle import load_bundle

FIXTURE_BUNDLE = Path("tests/fixtures/synthetic")
SMOKE_PROFILE = Path("config/smoke.yaml")

EXPECTED_DIRECT_BEST_OUTPUT = "271983"  # see tests/pools/test_constant_product.py


def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted during an offline command")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)


def _read_case_results(run_dir: Path) -> dict[str, dict[str, Any]]:
    lines = (run_dir / "cases.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    return {r["case_id"]: r for r in records}


def test_checked_in_fixture_is_valid() -> None:
    bundle = load_bundle(FIXTURE_BUNDLE)
    assert bundle.bundle_id == "synthetic-direct-v1"
    assert len(bundle.pools) == 2
    assert len(bundle.cases) == 2


def test_prepare_synthetic_reproduces_checked_in_fixture(tmp_path: Path) -> None:
    out = tmp_path / "synthetic"
    rc = main.main(["prepare", "--source", "synthetic", "--output", str(out)])
    assert rc == 0
    for name in ("manifest.json", "pools.json", "cases.jsonl"):
        assert (out / name).read_text() == (FIXTURE_BUNDLE / name).read_text()


def test_validate_checked_in_fixture(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main.main(["validate", "--bundle", str(FIXTURE_BUNDLE)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "is valid" in out


def test_validate_reports_clear_error_for_missing_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main.main(["validate", "--bundle", str(tmp_path / "does_not_exist")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "validate failed" in err


def test_run_produces_expected_integer_output_and_typed_no_route(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    rc = main.main(
        [
            "run",
            "--bundle",
            str(FIXTURE_BUNDLE),
            "--profile",
            str(SMOKE_PROFILE),
            "--results-dir",
            str(results_dir),
            "--strategies",
            "profile",
        ]
    )
    assert rc == 0

    run_dirs = list(results_dir.iterdir())
    assert len(run_dirs) == 1
    manifest = load_manifest(run_dirs[0])
    assert manifest.bundle_id == "synthetic-direct-v1"
    assert manifest.case_count == 2

    by_case = _read_case_results(run_dirs[0])
    best = by_case["direct_best_pool"]
    assert best["status"] == "ok"
    assert best["evaluation"]["gross_output"] == EXPECTED_DIRECT_BEST_OUTPUT
    assert best["evaluation"]["status"] == "ok"

    no_route = by_case["direct_no_route"]
    assert no_route["status"] == "no_route"
    assert no_route["evaluation"] is None


def test_run_is_deterministic_across_repeated_replays(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    for _ in range(2):
        rc = main.main(
            [
                "run",
                "--bundle",
                str(FIXTURE_BUNDLE),
                "--profile",
                str(SMOKE_PROFILE),
                "--results-dir",
                str(results_dir),
                "--strategies",
                "profile",
            ]
        )
        assert rc == 0

    run_dirs = sorted(results_dir.iterdir())
    assert len(run_dirs) == 2  # never overwrites: two distinct run ids
    first = _read_case_results(run_dirs[0])
    second = _read_case_results(run_dirs[1])
    # Deterministic replay: dropping run-identity fields, every case result
    # (status, evaluation, score) is byte-identical across independent runs.
    for case_id in first:
        assert first[case_id]["status"] == second[case_id]["status"]
        assert first[case_id]["evaluation"] == second[case_id]["evaluation"]
        assert first[case_id]["score"] == second[case_id]["score"]


def test_offline_guarantee_no_network_for_prepare_validate_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _block_network(monkeypatch)
    bundle_dir = tmp_path / "synthetic"
    results_dir = tmp_path / "results"

    assert main.main(["prepare", "--source", "synthetic", "--output", str(bundle_dir)]) == 0
    assert main.main(["validate", "--bundle", str(bundle_dir)]) == 0
    assert (
        main.main(
            [
                "run",
                "--bundle",
                str(bundle_dir),
                "--profile",
                str(SMOKE_PROFILE),
                "--results-dir",
                str(results_dir),
                "--strategies",
                "profile",
            ]
        )
        == 0
    )

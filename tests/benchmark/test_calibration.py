"""WHI-1447: calibration sweep profiles and run summaries (benchmark/calibration.py).

The sweep generator must emit ordinary, loader-validated profiles (one per grid point,
nothing varied at run time); the summary reads saved runs only and compares variants
on one bundle against the best known gross among them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import main
from benchmark import calibration as cal
from benchmark.profile import load_profile

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "corpus" / "bundle"

BASE = """\
schema_version: 2
algorithms: [direct, direct_split]
objective: {mode: gross_only}
search: {max_splits: 2, percent_step: 50}
budget: {time_limit_seconds: 60, max_quotes: null, max_candidates: null}
measurement: {warmup: 0, repeats: 1, seed: 7, order: fixed, memory_pass: false}
worker: {start_method: spawn, scope: algorithm, prepare_time_limit_seconds: 60}
"""


def test_grid_parsing_rejects_unknown_keys_and_malformed_axes() -> None:
    assert cal.parse_grid(["percent_step=10,5", "chunks=20"]) == {
        "percent_step": [10, 5],
        "chunks": [20],
    }
    for bad in (["warmup=1"], ["percent_step"], ["percent_step=5,5"], ["chunks=a"], []):
        with pytest.raises(cal.CalibrationError):
            cal.parse_grid(bad)
    with pytest.raises(cal.CalibrationError, match="twice"):
        cal.parse_grid(["chunks=1", "chunks=2"])


def test_sweep_profiles_are_validated_profiles_one_per_grid_point(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    base.write_text(BASE)
    grid = cal.parse_grid(["max_splits=1,2", "percent_step=50,25"])
    profiles = cal.sweep_profiles(base, grid, algorithms=["direct_split"], prefix="t")
    assert sorted(profiles) == sorted(
        f"t-maxsplits{m}-percentstep{p}.yaml" for m in (1, 2) for p in (50, 25)
    )
    for name, text in profiles.items():
        assert text.startswith("# GENERATED")
        path = tmp_path / name
        path.write_text(text)
        profile = load_profile(path)
        assert profile.algorithms == ("direct_split",)
        assert profile.budget.max_quotes is None and profile.measurement.seed == 7
    point = yaml.safe_load(profiles["t-maxsplits1-percentstep25.yaml"])
    assert point["search"] == {"max_splits": 1, "percent_step": 25}
    # A grid point the loader rejects is refused, not written.
    with pytest.raises(cal.CalibrationError, match="divisor of 100"):
        cal.sweep_profiles(base, cal.parse_grid(["percent_step=30"]))


def test_summary_compares_variants_against_the_best_known_on_one_bundle(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.yaml"
    base.write_text(BASE)
    out = tmp_path / "profiles"
    argv = ["calibrate", "profiles", "--base", str(base), "--output", str(out)]
    assert main.main([*argv, "--grid", "max_splits=1,2", "--prefix", "t"]) == 0
    results = tmp_path / "results"
    for name in ("t-maxsplits1.yaml", "t-maxsplits2.yaml"):
        run = ["run", "--bundle", str(FIXTURE), "--profile", str(out / name)]
        run += ["--strategies", "profile"]  # a calibration grid point exactly
        assert main.main([*run, "--results-dir", str(results)]) == 0
    runs = sorted(results.iterdir())
    summaries = cal.summarize_runs(runs)
    by_key = {(s.algorithm, s.params.get("max_splits")): s for s in summaries}
    one, two = by_key[("direct_split", 1)], by_key[("direct_split", 2)]
    scheduled = len(json.loads((FIXTURE / "corpus.json").read_text())["case_metadata"])
    assert one.scheduled == two.scheduled == scheduled
    assert sum(one.statuses.values()) == scheduled
    # A 2-way split grid contains every 1-way plan, so it is never worse than 1-way here
    # and the 1-way variant can only fall short of the best known.
    one_mean, two_mean = one.shortfall_bps["mean"], two.shortfall_bps["mean"]
    assert one_mean is not None and two_mean is not None
    assert one_mean >= two_mean >= 0
    assert two.at_best >= one.at_best
    one_quotes, two_quotes = one.quotes["max"], two.quotes["max"]
    assert one_quotes is not None and two_quotes is not None and two_quotes >= one_quotes
    table = cal.summary_table(summaries)
    assert len(table) == 2 + len(summaries) and table[0].startswith("| run |")
    json_path = tmp_path / "summary.json"
    assert main.main(["calibrate", "summarize", *map(str, runs), "--json", str(json_path)]) == 0
    assert len(json.loads(json_path.read_text())) == len(summaries)


def test_summary_refuses_runs_of_different_bundles(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    base.write_text(BASE.replace("[direct, direct_split]", "[direct]"))
    results = tmp_path / "results"
    cohort = tmp_path / "cohort"
    assert main.main(["corpus", "cohort", "--bundle", str(FIXTURE), "--output", str(cohort)]) == 0
    for bundle in (FIXTURE, cohort):
        run = ["run", "--bundle", str(bundle), "--profile", str(base)]
        run += ["--strategies", "profile"]  # a calibration grid point exactly
        assert main.main([*run, "--results-dir", str(results)]) == 0
    with pytest.raises(cal.CalibrationError, match="bundles"):
        cal.summarize_runs(sorted(results.iterdir()))
    assert main.main(["calibrate", "summarize", *map(str, sorted(results.iterdir()))]) == 1


# --------------------------------------------------------------- acceptance manifest


def test_acceptance_manifest_binds_runs_order_checks_and_reports(tmp_path: Path) -> None:
    """The final experiment manifest records each run's bundle/profile/experiment
    identity from saved records, re-runs the declared order check and is write-once."""
    from benchmark import acceptance as acc
    from snapshot.bundle import load_bundle

    base = tmp_path / "base.yaml"
    base.write_text(BASE)
    results = tmp_path / "results"
    for order in ("fixed", "reverse"):
        run = ["run", "--bundle", str(FIXTURE), "--profile", str(base), "--order", order]
        run += ["--strategies", "profile"]  # a calibration grid point exactly
        assert main.main([*run, "--results-dir", str(results)]) == 0
    fixed, reverse = sorted(results.iterdir(), key=lambda p: load_manifest_order(p))
    fixture = load_bundle(FIXTURE)
    record = {
        "source": {"bundle_hash": "c" * 64},
        "bundles": {"fixture": {"bundle_hash": fixture.bundle_hash, "cases": 1, "pools": 1}},
    }
    bundles = tmp_path / "bundles.json"
    bundles.write_text(json.dumps(record))
    report = tmp_path / "report"
    assert main.main(["report", str(fixed), "--output", str(report)]) == 0
    out = tmp_path / "acceptance.json"
    argv = [
        "acceptance",
        "--run",
        f"A={fixed}",
        "--run",
        f"B={reverse}",
        "--order-check",
        "AB=A,B",
        "--report",
        str(report),
        "--bundles",
        str(bundles),
        "--output",
        str(out),
    ]
    assert main.main(argv) == 0
    doc = json.loads(out.read_text())
    assert doc["schema"] == acc.ACCEPTANCE_SCHEMA
    assert [r["label"] for r in doc["runs"]] == ["A", "B"]
    entry = doc["runs"][0]
    assert entry["bundle"]["name"] == "fixture"
    assert entry["bundle"]["corpus_bundle_hash"] == "c" * 64
    assert entry["experiment"]["bundle_hash"] == fixture.bundle_hash
    assert entry["profile"]["path"] == str(base) and entry["wall_seconds"]["total"] > 0
    (check,) = doc["order_checks"]
    assert check["mismatches"] == 0 and check["orders"] == ["fixed", "reverse"]
    assert {Path(r["path"]).name for r in doc["reports"]} >= {"report.html", "cases.csv"}
    # Write-once: the same bytes are accepted, different bytes refused.
    assert main.main(argv) == 0
    out.write_text("{}")
    assert main.main(argv) == 1
    with pytest.raises(acc.AcceptanceError, match="not a recorded acceptance bundle"):
        acc.run_entry("X", fixed, {"bundles": {}})
    with pytest.raises(acc.AcceptanceError, match="duplicate"):
        acc.parse_labeled(["A=x", "A=y"], what="--run")


def load_manifest_order(run_dir: Path) -> int:
    order = json.loads((run_dir / "manifest.json").read_text())["measurement"]["order"]
    return ["fixed", "reverse"].index(order)

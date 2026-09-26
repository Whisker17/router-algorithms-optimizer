"""`main.py quote` end to end (WHI-1498): one exploratory request, every selected algorithm
solved exactly once in an isolated worker through the ordinary runner, results saved with
request and parent provenance, and rendered from those records.

Runs against the checked-in corpus fixture (real frozen pools, offline, no credentials).
The source profiles below deliberately declare warmups, repeats and a memory pass; the
command must override them to one solve attempt without touching the source file."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import re
import signal
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from types import FrameType

import fake_solvers  # sibling module: importable by spawned workers via sys.path
import pytest

import main
from benchmark.profile import load_profile
from benchmark.results import load_case_records, load_manifest
from benchmark.runner import compare_runs
from snapshot.bundle import load_bundle

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "corpus" / "bundle"
SIX = ["direct", "single_path", "direct_split", "path_split", "incremental_graph", "uni_sor_port"]
STATISTICS = re.compile(r"median|mean|p50|p95|percentile|samples|stdev", re.IGNORECASE)
TEST_ALARM_SECONDS = 120


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


def _profile(tmp_path: Path, algorithms: list[str], *, time_limit: float = 60) -> Path:
    path = tmp_path / "source_profile.yaml"
    path.write_text(
        "\n".join(
            [
                "schema_version: 2",
                f"algorithms: {json.dumps(algorithms)}",
                "objective: {mode: gross_only}",
                "search: {max_hops: 2, max_splits: 2, percent_step: 25}",
                "graph: {chunks: 10}",
                f"budget: {{time_limit_seconds: {time_limit}, max_quotes: 50000, "
                "max_candidates: null}",
                # the single-run override must beat all three of these
                "measurement: {warmup: 2, repeats: 3, seed: 1, order: fixed, memory_pass: true}",
                "worker: {start_method: spawn, scope: algorithm, prepare_time_limit_seconds: 60}",
                "",
            ]
        )
    )
    return path


def _digests(directory: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())}


def _quote(tmp_path: Path, profile: Path, *extra: str, pair: tuple[str, str] = ("USDC", "USDT0"),
           amount: str = "1500.25") -> list[str]:  # fmt: skip
    return [
        "quote",
        "--bundle", str(FIXTURE),
        "--profile", str(profile),
        "--token-in", pair[0],
        "--token-out", pair[1],
        "--amount", amount,
        "--quotes-dir", str(tmp_path / "quotes"),
        *extra,
    ]  # fmt: skip


def _run_dir(stdout: str) -> Path:
    match = re.search(r"\(run (\S+)\)", stdout)
    assert match, stdout
    return Path(match.group(1))


def test_six_algorithms_each_solve_once_with_a_forced_single_run_measurement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = _profile(tmp_path, SIX)
    source_bytes = profile.read_bytes()
    parent_before = _digests(FIXTURE)

    assert main.main(_quote(tmp_path, profile)) == 0
    out = capsys.readouterr().out
    run_dir = _run_dir(out)

    table = out.split("algorithm", 1)[1]
    for name in SIX:
        assert re.search(rf"^{name}\s+ok\s+", table, re.M), name
    assert "raw input 1500250000" in out  # 1500.25 USDC, 6 decimals, exact

    manifest = load_manifest(run_dir)
    assert {k: manifest.measurement[k] for k in ("warmup", "repeats", "memory_pass")} == {
        "warmup": 0,
        "repeats": 1,
        "memory_pass": False,
    }
    assert manifest.memory_record_count is None and not (run_dir / "memory.jsonl").exists()
    assert [e["algorithm"] for e in manifest.prepare_events] == SIX  # one worker each
    records = load_case_records(run_dir)
    assert [r["algorithm"] for r in records] == SIX
    for record in records:
        assert record["measurement"]["attempts_completed"] == 1
        assert len(record["measurement"]["solve_seconds"]) == 1

    # source profile and parent bundle untouched; the effective profile is saved beside it
    assert profile.read_bytes() == source_bytes
    assert _digests(FIXTURE) == parent_before
    effective = load_profile(manifest.profile_path)
    assert (effective.measurement.warmup, effective.measurement.repeats) == (0, 1)
    assert effective.measurement.memory_pass is False
    assert effective.algorithms == tuple(SIX)

    # provenance: exploratory, derived from the parent by hash, replayable offline
    quote_dir = run_dir.parent.parent
    record = json.loads((quote_dir / "quote.json").read_text())
    assert record["exploratory"] is True
    assert record["source_profile"]["sha256"] == hashlib.sha256(source_bytes).hexdigest()
    assert record["parent_bundle"]["bundle_hash"] == load_bundle(FIXTURE).bundle_hash
    derived = load_bundle(quote_dir / "bundle")
    assert derived.bundle_hash == manifest.bundle_hash != record["parent_bundle"]["bundle_hash"]
    provenance = json.loads((quote_dir / "bundle" / "provenance.json").read_text())
    assert provenance["exploratory"] is True
    assert provenance["derived_from"]["bundle_hash"] == record["parent_bundle"]["bundle_hash"]
    assert manifest.replay_command.startswith("uv run python main.py run --bundle ")


def test_details_change_presentation_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = _profile(tmp_path, SIX)
    assert main.main(_quote(tmp_path, profile)) == 0
    plain = capsys.readouterr().out
    assert main.main(_quote(tmp_path, profile, "--details")) == 0
    detailed = capsys.readouterr().out
    run_plain, run_detailed = _run_dir(plain), _run_dir(detailed)

    # identical request + parent => identical derived bundle; deterministic outputs agree
    assert compare_runs(run_plain, run_detailed) == []
    for run_dir in (run_plain, run_detailed):
        for record in load_case_records(run_dir):
            assert record["measurement"]["attempts_completed"] == 1

    assert "[direct]" not in plain
    for name in SIX:
        assert f"[{name}] ok" in detailed
    assert detailed.count("reconcile exactly") == len(SIX)
    for text in (plain, detailed):
        assert not STATISTICS.search(text), STATISTICS.search(text)
    assert "preparation" in detailed and "final independent evaluation" in detailed


def test_exactly_one_solve_attempt_per_algorithm_even_when_solvers_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import routing.algorithms.registry as registry

    for factory in fake_solvers.ALL:
        monkeypatch.setitem(registry.ALGORITHMS, factory.name, factory)
    log = tmp_path / "solves.log"
    monkeypatch.setenv("FAKE_SOLVE_LOG", str(log))
    names = ["counted_ok", "counted_raise", "counted_hang"]
    profile = _profile(tmp_path, names, time_limit=0.5)

    assert main.main(_quote(tmp_path, profile, "--details")) == 0
    out = capsys.readouterr().out

    # warmup 2 + repeats 3 + memory pass in the source => still exactly one call each
    assert Counter(log.read_text().split()) == {name: 1 for name in names}
    statuses = {r["algorithm"]: r for r in load_case_records(_run_dir(out))}
    assert statuses["counted_ok"]["status"] == "ok"
    assert statuses["counted_raise"]["status"] == "algorithm_error"
    assert statuses["counted_hang"]["status"] == "timeout"
    assert statuses["counted_hang"]["limit_hit"] == "time"
    assert statuses["counted_hang"]["last_valid_candidate"] is not None
    hang = out.split("[counted_hang]", 1)[1]
    assert "PARTIAL DIAGNOSTIC" in hang and "not a completed solve" in hang
    raised = out.split("[counted_raise]", 1)[1].split("[counted_hang]")[0]
    assert "no completed route" in raised
    # PR-F2: what the killed/raising solver never reported is N/A, not a measured zero, and
    # an evaluated-but-untimed partial candidate is not "nothing evaluated"
    assert "final independent evaluation not recorded (the partial candidate" in hang
    assert "final independent evaluation N/A (no plan to evaluate)" in raised
    for block in (hang, raised):
        assert "candidates considered/truncated N/A (the solver returned no counters)" in block
    assert not re.search(r"\bNone\b", out)


@pytest.mark.parametrize(
    ("pair", "amount", "message"),
    [
        (("USDC", "USDC"), "1", "same token"),
        (("DOGE", "USDC"), "1", "unknown token 'DOGE'"),
        (("MNT", "USDC"), "1", "use WMNT"),
        (("0x" + "00" * 19 + "01", "USDC"), "1", "not in the frozen token universe"),
        (("USDC", "USDT0"), "1.0000001", "rounded"),
        (("USDC", "USDT0"), "0", "positive"),
        (("USDC", "USDT0"), "nan", "plain positive decimal"),
        (("USDC", "USDT0"), "-5", "plain positive decimal"),
    ],
)
def test_invalid_requests_fail_before_any_write_or_solve(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    pair: tuple[str, str],
    amount: str,
    message: str,
) -> None:
    profile = _profile(tmp_path, ["direct"])
    assert main.main(_quote(tmp_path, profile, pair=pair, amount=amount)) == 1
    assert message in capsys.readouterr().err
    assert not (tmp_path / "quotes").exists()


def test_empirical_cost_profile_is_refused_before_any_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO)  # the profile names its cost-model artifact repo-relatively
    args = _quote(tmp_path, REPO / "config" / "daily.yaml")
    assert main.main(args) == 1
    assert "empirical_cost is not supported by quote" in capsys.readouterr().err
    assert not (tmp_path / "quotes").exists()


def test_failed_direct_baseline_and_envelope_warning_are_explicit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = _profile(tmp_path, ["direct", "single_path"])
    # USDC -> USDT has no path in the fixture's pool subset; 20000 USDC exceeds its envelope
    assert main.main(_quote(tmp_path, profile, pair=("USDC", "USDT"), amount="20000")) == 0
    out = capsys.readouterr().out
    assert re.search(r"^direct\s+no_route\s+N/A", out, re.M)
    assert "vs direct: N/A -- the direct baseline has no valid output (no_route)" in out
    assert "WARNING: input exceeds the parent corpus envelope" in out


def test_report_command_renders_a_quote_run_as_a_single_case_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, ["direct", "path_split"])
    assert main.main(_quote(tmp_path, profile)) == 0
    run_dir = _run_dir(capsys.readouterr().out)

    out_dir = tmp_path / "report"
    assert main.main(["report", str(run_dir), "--output", str(out_dir)]) == 0
    text = (out_dir / "single_request.txt").read_text()
    assert "EXPLORATORY single-request comparison" in text and "[path_split] ok" in text
    assert not STATISTICS.search(text)
    assert not (out_dir / "report.html").exists()

    # PR-F1: relocated artifacts (request bundle not found) are refused, never reported
    # through the corpus report's distribution tables; --bundle recovers the report
    moved = tmp_path / "moved"
    import shutil

    shutil.copytree(run_dir, moved)
    manifest_path = moved / "manifest.json"
    raw = json.loads(manifest_path.read_text())
    raw["replay_command"] = raw["replay_command"].replace(str(tmp_path), "/nonexistent")
    manifest_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
    capsys.readouterr()
    assert main.main(["report", str(moved), "--output", str(tmp_path / "lost")]) == 1
    assert "request bundle is unavailable" in capsys.readouterr().err
    assert not (tmp_path / "lost").exists()
    bundle = str(run_dir.parent.parent / "bundle")
    found = tmp_path / "found"
    assert main.main(["report", str(moved), "--bundle", bundle, "--output", str(found)]) == 0
    assert "EXPLORATORY" in (found / "single_request.txt").read_text()

    # never pooled with a corpus run
    monkeypatch.chdir(REPO)
    assert main.main(
        ["run", "--bundle", "tests/fixtures/synthetic", "--profile", "config/smoke.yaml",
         "--results-dir", str(tmp_path / "batch")]
    ) == 0  # fmt: skip
    batch = next((tmp_path / "batch").iterdir())
    capsys.readouterr()
    assert main.main(["report", str(run_dir), str(batch), "--output", str(tmp_path / "x")]) == 1
    assert "reported on their own" in capsys.readouterr().err

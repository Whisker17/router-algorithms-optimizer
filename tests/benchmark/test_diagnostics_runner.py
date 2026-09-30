"""WHI-1548 Stage B end to end: research diagnostics through real isolated workers, the
runner, `main.py quote --details`, the offline report (text and HTML/CSV) and literal replay.

The test-only factories of `diag_solvers` are registered per test (`register`), so the
ordinary roster stays nine and no future contract identity is pre-registered. The runner's
recorded source revision is pinned (`environment_record` patched) so identity checks are
deterministic; everything else is the ordinary code path.
"""

from __future__ import annotations

import json
import multiprocessing
import re
import shlex
import signal
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
from typing import Any

import diag_solvers  # sibling module: importable by spawned workers via sys.path
import pytest

import benchmark.runner as runner_module
import main
from benchmark.diagnostics import unavailable_view
from benchmark.profile import load_profile
from benchmark.results import environment_record, load_case_records, load_manifest
from benchmark.runner import compare_runs, run_experiment
from report.render import render_report
from routing.algorithms.base import settings_sha256
from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "tests" / "fixtures" / "corpus" / "bundle"
REV = "c0ffee" + "0" * 34
TEST_ALARM_SECONDS = 150
TIME_LIMIT = 1.0
STATISTICS = re.compile(r"median|mean|p50|p95|percentile|samples|stdev", re.IGNORECASE)
FX = diag_solvers.PREFIX
LEGACY_RECORD_KEYS = {
    "case_id",
    "algorithm",
    "status",
    "evaluation",
    "score",
    "candidates_considered",
    "candidates_truncated",
    "search",
    "quotes",
    "limit_hit",
    "error",
    "solver_reported",
    "last_valid_candidate",
    "measurement",
}
# mode -> (record status, view state, codes)
EXPECTED: dict[str, tuple[str, str, list[str]]] = {
    "certified": ("ok", "certified", []),
    "estimate": ("ok", "estimate", []),
    "unknown": ("ok", "unknown", []),
    "not_produced": ("ok", "unavailable", []),
    "no_diag": ("ok", "unavailable", []),
    "hang": ("timeout", "unavailable", []),
    "crash": ("algorithm_error", "unavailable", []),
    "unsupported": ("unsupported", "unavailable", []),
    "wrong_request": ("ok", "invalid", ["C_REQUEST"]),
    "wrong_revision": ("ok", "invalid", ["C_IDENTITY"]),
    "stale_settings": ("ok", "invalid", ["C_IDENTITY"]),
    "lying_score": ("ok", "invalid", ["C_LOWER_EVAL"]),
    "estimate_gap": ("ok", "invalid", ["C_UNCERTIFIED_BOUND"]),
    "ledger": ("ok", "invalid", ["W_LEDGER"]),
    "malformed": ("ok", "invalid", sorted(["C_AMOUNT_TYPE", "D_HASH", "D_MISSING_FIELD",
                                           "W_TYPE", "W_UNIT"])),
    "garbage": ("ok", "invalid", ["S_SHAPE"]),
}  # fmt: skip


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
def _fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    diag_solvers.register(monkeypatch)

    def _pinned(worker: Any = None) -> dict[str, Any]:
        return {**environment_record(worker), "git_revision": REV}

    monkeypatch.setattr(runner_module, "environment_record", _pinned)


BUNDLE = SnapshotBundle(
    bundle_id="b1",
    kind="synthetic",
    schema_version=1,
    block=BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0),
    pools={
        "pool_a": ConstantProductPoolState(
            pool_id="pool_a",
            token0="TKA",
            token1="TKB",
            reserve0=1_000_000,
            reserve1=3_000_000,
            fee_bps=30,
        )
    },
    cases=(
        Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100_000),
        Case(case_id="c2", token_in="TKB", token_out="TKA", amount_in=250_000),
    ),
    bundle_hash="deadbeef",
    source_path="<test>",
)


def _profile_yaml(
    tmp_path: Path, algorithms: list[str], *, options: dict[str, Any] | None = None
) -> Path:
    path = tmp_path / "profile.yaml"
    lines = [
        "schema_version: 2",
        f"algorithms: {json.dumps(algorithms)}",
        "objective: {mode: gross_only}",
        "search: {max_hops: 2, max_splits: 2, percent_step: 25}",
        "graph: {chunks: 10}",
        f"budget: {{time_limit_seconds: {TIME_LIMIT}, max_quotes: 50000, max_candidates: null}}",
        "measurement: {warmup: 1, repeats: 2, seed: 1, order: fixed, memory_pass: false}",
        "worker: {start_method: spawn, scope: algorithm, prepare_time_limit_seconds: 60}",
    ]
    if options:
        lines.append(f"algorithm_options: {json.dumps(options)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _records(run_dir: str | Path) -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["algorithm"], r["case_id"]): r for r in load_case_records(run_dir)}


def _run(tmp_path: Path, algorithms: list[str], **kwargs: Any) -> Any:
    profile = load_profile(_profile_yaml(tmp_path, algorithms, **kwargs))
    return run_experiment(BUNDLE, profile, results_dir=tmp_path / "results", replay_command="cmd")


# ------------------------------------------------------------------ runner


def test_every_outcome_through_real_workers(tmp_path: Path) -> None:
    names = ["direct", *(FX + m for m in EXPECTED)]
    manifest = _run(tmp_path, names)
    records = _records(manifest.run_dir)
    for case in BUNDLE.cases:
        baseline = records[("direct", case.case_id)]
        assert set(baseline) == LEGACY_RECORD_KEYS  # an existing algorithm: no new key at all
        request = {
            "case_id": case.case_id,
            "token_in": case.token_in,
            "token_out": case.token_out,
            "amount_in": str(case.amount_in),
        }
        for mode, (status, state, codes) in EXPECTED.items():
            record = records[(FX + mode, case.case_id)]
            view = record["diagnostics"]
            assert (record["status"], view["state"], view["codes"]) == (status, state, codes), (
                mode,
                view,
            )
            if status == "ok":  # diagnostics never change the independent outcome
                assert record["evaluation"] == baseline["evaluation"], mode
                assert record["score"] == baseline["score"], mode
            if view["origin"] == "solver":
                assert view["checked_against"]["run"] == {
                    "git_revision": REV,
                    "bundle_hash": "deadbeef",
                    "algorithm": FX + mode,
                    "effective_settings_sha256": settings_sha256({}),
                }
                assert view["checked_against"]["request"] == request
                assert view["checked_against"]["score"] == record["score"]
                assert view["checked_against"]["quotes_counted"] == record["quotes"]["counted"]
        certified = records[(FX + "certified", case.case_id)]["diagnostics"]
        assert certified["lower"] == certified["upper"] == baseline["score"]
        assert certified["gap"] == "0" and certified["optimality_proven"] is True
        assert certified["domain"]["grid"] == "single_leg"
        assert certified["work"]["quotes_executed"] == baseline["quotes"]["counted"]
        # a proof of another request carrying this request's own evaluated score still fails
        wrong = records[(FX + "wrong_request", case.case_id)]["diagnostics"]
        assert any("case_id" in d for d in wrong["details"])
        # runner-observed views: nothing of the solver's output is read or reconstructed
        hang = records[(FX + "hang", case.case_id)]
        assert hang["diagnostics"] == unavailable_view("hard_timeout")
        assert hang["limit_hit"] == "time" and hang["search"] == {}
        assert hang["last_valid_candidate"] is not None
        assert hang["last_valid_candidate"]["score"] == baseline["score"]
        assert records[(FX + "crash", case.case_id)]["diagnostics"] == unavailable_view(
            "worker_error"
        )
        assert records[(FX + "no_diag", case.case_id)]["diagnostics"] == unavailable_view(
            "not_produced"
        )
        unsupported = records[(FX + "unsupported", case.case_id)]["diagnostics"]
        assert unsupported["origin"] == "solver" and unsupported["reason"] == "not_produced"
        assert unsupported["scope"] == {"supported": False, "reason": "fixture: declared scope"}
        garbage = records[(FX + "garbage", case.case_id)]
        assert garbage["search"]["r021"].startswith("<r021 diagnostics not JSON-serializable")
    assert manifest.complete and manifest.case_count == len(names) * len(BUNDLE.cases)


def test_declared_options_are_the_certificates_settings_identity(tmp_path: Path) -> None:
    names = [FX + "certified", FX + "stale_settings"]
    options = {FX + "certified": {"tag": "b"}, FX + "stale_settings": {"tag": "b"}}
    manifest = _run(tmp_path, names, options=options)
    records = _records(manifest.run_dir)
    ours = records[(FX + "certified", "c1")]["diagnostics"]
    assert ours["state"] == "certified"
    assert ours["checked_against"]["run"]["effective_settings_sha256"] == settings_sha256(
        {"tag": "b"}
    )
    assert manifest.resolved_profile["algorithm_options"][FX + "certified"][
        "settings_sha256"
    ] == settings_sha256({"tag": "b"})
    stale = records[(FX + "stale_settings", "c1")]["diagnostics"]
    assert stale["codes"] == ["C_IDENTITY"]
    assert any("effective_settings_sha256" in d for d in stale["details"])


def test_a_run_without_a_source_revision_certifies_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_git(worker: Any = None) -> dict[str, Any]:
        return {**environment_record(worker), "git_revision": None}

    monkeypatch.setattr(runner_module, "environment_record", _no_git)
    manifest = _run(tmp_path, [FX + "certified"])
    view = _records(manifest.run_dir)[(FX + "certified", "c1")]["diagnostics"]
    assert view["state"] == "invalid" and view["codes"] == ["C_IDENTITY"]


def test_order_check_covers_diagnostics_and_old_views_stay_identical(tmp_path: Path) -> None:
    names = ["direct", FX + "certified", FX + "wrong_request"]
    profile = load_profile(_profile_yaml(tmp_path, names))
    kwargs: dict[str, Any] = {"results_dir": tmp_path / "results", "replay_command": "cmd"}
    first = run_experiment(BUNDLE, profile, **kwargs)
    second = run_experiment(BUNDLE, profile, order="reverse", **kwargs)
    assert compare_runs(first.run_dir, second.run_dir) == []
    view = runner_module._deterministic_view(_records(first.run_dir)[("direct", "c1")])
    assert "diagnostics" not in view  # the legacy leak-check view is unchanged
    view = runner_module._deterministic_view(_records(first.run_dir)[(FX + "certified", "c1")])
    assert view["diagnostics"]["state"] == "certified"


def test_observational_stage_seconds_never_count_as_nondeterminism(tmp_path: Path) -> None:
    import copy

    manifest = _run(tmp_path, [FX + "unknown"])
    record = _records(manifest.run_dir)[(FX + "unknown", "c1")]
    assert set(record["diagnostics"]["stages"]) == {"incumbent"}  # measured, attempt-varying
    assert record["measurement"]["attempts_consistent"] is True  # warmup 1 + repeats 2
    slower = copy.deepcopy(record)
    slower["diagnostics"]["stages"] = {"incumbent": 9.5}
    slower["search"]["r021"]["stages"] = {"incumbent": 9.5}
    view = runner_module._deterministic_view
    assert view(slower) == view(record)
    slower["diagnostics"]["state"] = "certified"
    assert view(slower) != view(record)


# ------------------------------------------------------------------ offline report


def test_html_and_csv_render_every_state_and_regenerate_identically(tmp_path: Path) -> None:
    names = ["direct", *(FX + m for m in EXPECTED)]
    manifest = _run(tmp_path, names)
    first = render_report(manifest, tmp_path / "r1")
    second = render_report(manifest, tmp_path / "r2")
    assert first.html.read_bytes() == second.html.read_bytes()
    assert "diagnostics" in first.csv
    assert first.csv["diagnostics"].read_bytes() == second.csv["diagnostics"].read_bytes()
    page = first.html.read_text()
    section = page.split("<h2>Research diagnostics (R021-C/1)</h2>", 1)[1].split("<h2>", 1)[0]
    for text in (
        "certified [",
        "gap 0",
        "estimate ",
        "(not a bound;",
        "unknown (no bound;",
        "unavailable (hard_timeout)",
        "unavailable (worker_error)",
        "unavailable (not_produced)",
        "invalid certificate (C_REQUEST) -- counted as unknown",
        "invalid certificate (C_IDENTITY)",
        "invalid certificate (C_LOWER_EVAL)",
        "invalid diagnostics (S_SHAPE)",
        "UNSUPPORTED (fixture: declared scope)",
        "fallback used (source direct; reason fixture fallback); repair attempts 0",
        "quotes_executed ",
        "[quote]",
        "(single_leg)",
    ):
        assert text in section, text
    assert "<code>direct</code>" not in section  # an existing algorithm has no row
    rows = first.csv["diagnostics"].read_text().splitlines()
    assert len(rows) == 1 + (len(names) - 1) * len(BUNDLE.cases)


def test_runs_without_diagnostics_render_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import report.html as html_module

    manifest = _run(tmp_path, ["direct"])
    new = render_report(manifest, tmp_path / "new")
    assert "diagnostics" not in new.csv
    page = new.html.read_text()
    assert "R021" not in page and "Research diagnostics" not in page
    # with the Stage B renderer switched off entirely, the page is byte-identical
    monkeypatch.setattr(html_module, "_diagnostics", lambda run: "")
    old = render_report(manifest, tmp_path / "old")
    assert old.html.read_bytes() == new.html.read_bytes()
    assert sorted(old.csv) == sorted(new.csv)


def test_a_tampered_saved_view_is_refused_by_the_checksum(tmp_path: Path) -> None:
    manifest = _run(tmp_path, [FX + "wrong_request"])
    cases = Path(manifest.run_dir) / "cases.jsonl"
    text = cases.read_text()
    forged = text.replace('"state": "invalid"', '"state": "certified"')
    assert forged != text
    cases.write_text(forged)
    with pytest.raises(Exception, match="checksum"):
        render_report(load_manifest(manifest.run_dir), tmp_path / "forged")


# ------------------------------------------------------------------ quote CLI


def _quote_args(tmp_path: Path, profile: Path, *extra: str) -> list[str]:
    return [
        "quote",
        "--bundle", str(CORPUS),
        "--profile", str(profile),
        "--token-in", "USDC",
        "--token-out", "USDT0",
        "--amount", "1500.25",
        "--quotes-dir", str(tmp_path / "quotes"),
        "--strategies", "profile",
        *extra,
    ]  # fmt: skip


def test_quote_details_report_and_literal_replay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "solves.log"
    monkeypatch.setenv("FAKE_SOLVE_LOG", str(log))
    modes = ["certified", "estimate", "unknown", "wrong_request", "hang", "no_diag", "garbage"]
    names = ["direct", *(FX + m for m in modes)]
    profile = _profile_yaml(tmp_path, names, options={FX + "certified": {"tag": "a"}})

    assert main.main(_quote_args(tmp_path, profile, "--details")) == 0
    out = capsys.readouterr().out
    # one solve per identity: no warmup, retry, repeat or memory pass (source: 1 + 2)
    assert Counter(log.read_text().split()) == {FX + m: 1 for m in modes}
    run_dir = Path(re.search(r"\(run (\S+)\)", out).group(1))  # type: ignore[union-attr]
    records = {r["algorithm"]: r for r in load_case_records(run_dir)}
    for record in records.values():
        assert record["measurement"]["attempts_completed"] == (
            0 if record["algorithm"] == FX + "hang" else 1
        )
    score = records["direct"]["score"]
    assert "diagnostics" not in records["direct"]

    compact, details = out.split("[direct]", 1)
    assert "research diagnostics (R021-C/1; checked by the runner" in compact
    assert f"  {FX}certified: certified [{score}, {score}] gap 0" in compact
    block = details.split(f"[{FX}certified]", 1)[1].split(f"[{FX}estimate]", 1)[0]
    assert (
        f"    bound: certified [{score}, {score}] gap 0 (exhaustive; termination complete" in block
    )
    assert "    domain: " in block and "(single_leg)" in block
    assert "    max_candidates unit: pools_evaluated" in block
    assert "    work (named units; different units are never divided): quotes_executed" in block
    assert f"bound: estimate {score}.25 (not a bound;" in details
    assert "bound: unknown (no bound; termination complete)" in details
    assert "fallback/repair: fallback used (source direct; reason fixture fallback)" in details
    assert re.search(r"stages \(observed seconds, not budgets\): incumbent \d\.\d{6} s", details)
    wrong = details.split(f"[{FX}wrong_request]", 1)[1].split(f"[{FX}hang]", 1)[0]
    assert "bound: invalid certificate (C_REQUEST) -- counted as unknown (no bound)" in wrong
    assert "C_REQUEST: certificate.request.case_id" in wrong
    hang = details.split(f"[{FX}hang]", 1)[1].split(f"[{FX}no_diag]", 1)[0]
    assert "PARTIAL DIAGNOSTIC" in hang
    assert "bound: unavailable (hard_timeout) (observed by the runner" in hang
    assert "bound: unavailable (not_produced) (observed by the runner" in details
    assert "bound: invalid diagnostics (S_SHAPE)" in details
    assert not STATISTICS.search(out), STATISTICS.search(out)
    assert not re.search(r"\bNone\b", out)

    # literal replay of the saved effective profile: same identities, options and outcomes
    quote_record = json.loads((run_dir.parent.parent / "quote.json").read_text())
    replay = shlex.split(quote_record["replay_command"])
    assert replay[:4] == ["uv", "run", "python", "main.py"]
    capsys.readouterr()
    assert main.main(replay[4:]) == 0
    replay_out = capsys.readouterr().out
    replay_dir = Path(re.search(r"run '\S+' saved to (\S+)", replay_out).group(1))  # type: ignore[union-attr]
    first, again = load_manifest(run_dir), load_manifest(replay_dir)
    assert again.resolved_profile == first.resolved_profile
    assert again.algorithms == first.algorithms
    assert compare_runs(run_dir, replay_dir) == []
    assert _records(replay_dir)[(FX + "certified", records["direct"]["case_id"])]["diagnostics"][
        "checked_against"
    ]["run"]["effective_settings_sha256"] == settings_sha256({"tag": "a"})

    # the offline text report of the saved quote run regenerates identically
    capsys.readouterr()
    for name in ("rep1", "rep2"):
        assert main.main(["report", str(run_dir), "--output", str(tmp_path / name)]) == 0
    text = (tmp_path / "rep1" / "single_request.txt").read_text()
    assert text == (tmp_path / "rep2" / "single_request.txt").read_text()
    assert f"bound: certified [{score}, {score}] gap 0" in text
    assert "invalid certificate (C_REQUEST)" in text
    assert not STATISTICS.search(text)

"""`tools/metis_challenge.py` (WHI-1449 gate-S2 diagnostic driver): case selection is never
empty, a verdict never reads missing evidence as zero or a timeout as success, and measured
runs are accepted only when their manifest, bundle, arm, effective settings, coverage and
clean code revision all match. Every refusal is proven by mutating a valid captured run.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from benchmark.profile import load_profile
from routing.algorithms import incremental_graph, metis_inspired
from routing.algorithms.base import AlgorithmConfig, Budget, SolveContext
from snapshot.bundle import load_bundle, sha256_file
from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "routing"
MIXED = FIXTURES / "mantle_mixed"
M3 = REPO / "config" / "metis_challenge" / "m3.yaml"
M4 = REPO / "config" / "metis_challenge" / "m4.yaml"
A0 = REPO / "config" / "metis_challenge" / "a0.yaml"
CLEAN = {"git_revision": "f" * 40, "git_dirty": False, "git_diff_sha256": None}


@cache
def _tool() -> Any:
    spec = importlib.util.spec_from_file_location(
        "metis_challenge", REPO / "tools" / "metis_challenge.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _argv(out: Path, *extra: str, profile: Path = M3, bundle: Path = MIXED) -> list[str]:
    return [
        "diagnose",
        "--bundle", str(bundle),
        "--profile", str(profile),
        "--reference-profile", str(A0),
        "--output", str(out),
        *extra,
    ]  # fmt: skip


def _refused(capsys: pytest.CaptureFixture[str], out: Path, argv: list[str], match: str) -> None:
    assert _tool().main(argv) == 2
    assert not out.exists()  # refused before any diagnostic work or write
    assert match in capsys.readouterr().err


# ------------------------------------------------------------ selection


def test_first_zero_is_refused_not_an_empty_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reproduction: `--first 0 --x7` on a valid bundle and the M3/A0 profiles used to
    write `cases_selected=[]`, `verdicts={}`, `x7_identical=true` and exit 0."""
    out = tmp_path / "s2.json"
    _refused(capsys, out, _argv(out, "--first", "0", "--x7"), "--first must be >= 1")
    _refused(capsys, out, _argv(out, "--first", "-3"), "--first must be >= 1")


def test_duplicate_and_unknown_cases_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "s2.json"
    case = "usdc_usdt_small"
    _refused(capsys, out, _argv(out, "--case", case, "--case", case), "duplicate --case")
    _refused(capsys, out, _argv(out, "--case", "nope"), "unknown case ids")


def test_an_empty_bundle_is_refused() -> None:
    bundle = load_bundle(MIXED)
    empty = SnapshotBundle(
        bundle_id="empty",
        kind=bundle.kind,
        schema_version=bundle.schema_version,
        block=bundle.block,
        pools=bundle.pools,
        cases=(),
        bundle_hash="0" * 64,
        source_path="<test>",
    )
    with pytest.raises(_tool().ChallengeInputError, match="has no cases"):
        _tool().select_cases(empty, [], None)
    assert [c.case_id for c in _tool().select_cases(bundle, [], 2)] == [
        c.case_id for c in bundle.cases[:2]
    ]


# ------------------------------------------------------------ verdicts

BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)
HUB = SnapshotBundle(
    bundle_id="t",
    kind="synthetic",
    schema_version=1,
    block=BLOCK,
    pools={
        p.pool_id: p
        for p in (
            ConstantProductPoolState("hub", "A", "C", 10**12, 10**12, 1, None),
            ConstantProductPoolState("cb1", "C", "B", 10**9, 10**9, 30, None),
            ConstantProductPoolState("cb2", "C", "B", 10**9, 10**9, 30, None),
            ConstantProductPoolState("ab", "A", "B", 10**8, 10**8, 30, None),
        )
    },
    cases=(),
    bundle_hash="deadbeef",
    source_path="<test>",
)
PARAMS = {"max_hops": 3, "max_splits": 4, "percent_step": 5, "chunks": 10}


def _sides(case: Case, budget: Budget | None = None) -> tuple[Any, Any, Any]:
    tool = _tool()
    budget = budget or Budget()
    prepared = metis_inspired.prepare(
        HUB,
        AlgorithmConfig(metis_inspired.NAME, {**PARAMS, "label_hops": 3, "label_pruning": True}),
    )
    ref_prepared = incremental_graph.prepare(HUB, AlgorithmConfig(incremental_graph.NAME, PARAMS))
    from benchmark.objective import gross_only

    got = metis_inspired.solve(case, SolveContext(HUB, gross_only(), prepared), budget)
    ref = incremental_graph.solve(case, SolveContext(HUB, gross_only(), ref_prepared), budget)
    diag = metis_inspired.diagnose_case(case, HUB, prepared)
    return diag, tool.side_from_result(got), tool.side_from_result(ref)


def _verdict(diag: Any, metis: Any, ref: Any) -> dict[str, Any]:
    result: dict[str, Any] = _tool().verdict(diag, metis, ref, "test")
    return result


def test_the_timeout_reproduction_is_not_identical() -> None:
    """The reproduction: two empty `timeout` sides with an empty diagnostic used to give
    `identical`, `truncated=false`, `quotes_within_reference=true`."""
    tool = _tool()
    diag = {
        "incremental_allocation": [],
        "incremental_chunk_sequence": [],
        "unexplained_chunks": 0,
        "quote_subset_violations": 0,
        "divergent_chunks": 0,
    }
    sides: list[dict[str, Any]] = [
        {"status": "timeout", "score": None, "trace": [], "limit_hit": None, "search": {}},
        tool.side_from_record({"status": "timeout", "score": None, "search": {}}),
        tool.side_from_record({"status": "timeout", "score": None}),
    ]
    for side in sides:
        got = _verdict(diag, side, copy.deepcopy(side))
        assert got["verdict"] == "evidence_unavailable"
        assert got["quotes_within_reference"] is None and got["same_plan"] is None
        assert got["budgets"] == [] and "search" in got["evidence_missing"]["metis"]


def test_verdicts_on_real_solves_and_their_mutations() -> None:
    case = Case("c", "A", "B", 4 * 10**8)
    diag, metis, ref = _sides(case)
    assert _verdict(diag, metis, ref)["verdict"] == "identical"
    # A missing counter is unavailable evidence, never zero.
    for key in ("quotes_executed", "incremental_allocation", "label_relaxations"):
        broken = copy.deepcopy(metis)
        del broken["search"][key]
        got = _verdict(diag, broken, ref)
        assert got["verdict"] == "evidence_unavailable"
        assert f"search.{key}" in got["evidence_missing"]["metis"]
    broken = copy.deepcopy(ref)
    del broken["search"]["paths_scored"]
    assert _verdict(diag, metis, broken)["verdict"] == "evidence_unavailable"
    # An `ok` without its executed plan is not a successful trace.
    for field, value in (("trace", None), ("trace", []), ("score", None)):
        broken = copy.deepcopy(metis)
        broken[field] = value
        assert _verdict(diag, broken, ref)["verdict"] == "evidence_unavailable"
    # More quotes than the reference with every chunk agreeing is unexplained.
    greedy = copy.deepcopy(metis)
    greedy["search"]["quotes_executed"] = ref["search"]["quotes_executed"] + 1
    got = _verdict(diag, greedy, ref)
    assert got["verdict"] == "unexplained" and got["quotes_within_reference"] is False
    # A different plan without a divergent chunk is unexplained; with one it is attributed.
    other = copy.deepcopy(metis)
    other["trace"] = other["trace"][:-1]
    assert _verdict(diag, other, ref)["verdict"] == "unexplained"
    assert _verdict({**diag, "divergent_chunks": 1}, other, ref)["verdict"] == "attributed"
    # A diagnostic trajectory that is not the solve's is unexplained.
    moved = copy.deepcopy(metis)
    moved["search"]["incremental_chunk_sequence"] = [
        1,
        *moved["search"]["incremental_chunk_sequence"],
    ]
    assert _verdict(diag, moved, ref)["verdict"] == "unexplained"
    # Differing statuses are unexplained, not budget order.
    lost = {**copy.deepcopy(metis), "status": "no_route", "score": None, "trace": None}
    assert _verdict(diag, lost, ref)["verdict"] == "unexplained"


def test_budget_order_needs_declared_budget_evidence_and_never_hides_failures() -> None:
    case = Case("c", "A", "B", 4 * 10**8)
    diag, metis, ref = _sides(case)
    # The runner's hard limit (no returned search) is an explicit budget outcome.
    runner: dict[str, Any] = {
        "status": "timeout",
        "score": None,
        "trace": None,
        "limit_hit": "time",
        "search": {},
    }
    got = _verdict(diag, runner, ref)
    assert got["verdict"] == "budget_order" and got["budgets"] == ["runner:time"]
    assert got["metis"]["status"] == "timeout" and got["quotes_within_reference"] is None
    # A solver-declared truncation (here a real quote-capped solve).
    capped_diag, capped, capped_ref = _sides(case, Budget(max_quotes=40))
    assert capped["search"]["truncated_by"] == "max_quotes"
    assert _verdict(capped_diag, capped, capped_ref)["verdict"] == "budget_order"
    # A timeout with an unknown limit carries no budget evidence.
    odd = {**runner, "limit_hit": "memory"}
    assert _verdict(diag, odd, ref)["verdict"] == "evidence_unavailable"
    # Genuine failures stay failures, even beside a budget-truncated arm.
    for status in ("algorithm_error", "invalid_plan", "cancelled"):
        failed: dict[str, Any] = {
            "status": status,
            "score": None,
            "trace": None,
            "limit_hit": None,
            "search": {},
        }
        assert _verdict(diag, failed, runner)["verdict"] == "solver_failure"
        assert _verdict(diag, metis, failed)["verdict"] == "solver_failure"
    # The diagnostic's own findings are never absorbed into budget order.
    assert _verdict({**diag, "unexplained_chunks": 1}, runner, ref)["verdict"] == "unexplained"


def test_valid_no_route_and_incomplete_pairs_keep_their_domain_semantics() -> None:
    tool = _tool()
    unreachable = SnapshotBundle(
        bundle_id="u",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools={
            "ab": ConstantProductPoolState("ab", "A", "B", 10**6, 10**6, 30, None),
            "cd": ConstantProductPoolState("cd", "C", "D", 10**6, 10**6, 30, None),
        },
        cases=(),
        bundle_hash="u",
        source_path="<test>",
    )
    usdt, wmnt = (
        "0x201eba5cc46d216ce6dc03f6a759e8e766e956ae",
        "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8",
    )
    cl = load_bundle(MIXED).pools["0x4cdfc22bf05209de87ee564746dc7e5174631d2b"]
    incomplete = SnapshotBundle(
        bundle_id="i",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools={cl.pool_id: cl},
        cases=(),
        bundle_hash="i",
        source_path="<test>",
    )
    from benchmark.objective import gross_only

    for bundle, case, status in (
        (unreachable, Case("c", "A", "D", 1000), "no_route"),
        (incomplete, Case("huge", usdt, wmnt, 10**18), "incomplete_snapshot"),
    ):
        prepared = metis_inspired.prepare(
            bundle,
            AlgorithmConfig(
                metis_inspired.NAME, {**PARAMS, "label_hops": 3, "label_pruning": True}
            ),
        )
        ref_prepared = incremental_graph.prepare(
            bundle, AlgorithmConfig(incremental_graph.NAME, PARAMS)
        )
        got = tool.side_from_result(
            metis_inspired.solve(case, SolveContext(bundle, gross_only(), prepared), Budget())
        )
        ref = tool.side_from_result(
            incremental_graph.solve(
                case, SolveContext(bundle, gross_only(), ref_prepared), Budget()
            )
        )
        diag = metis_inspired.diagnose_case(case, bundle, prepared)
        result = _verdict(diag, got, ref)
        assert got["status"] == ref["status"] == status
        assert result["verdict"] == "identical" and result["quotes_within_reference"] is True


# ------------------------------------------------------------ measured evidence


@pytest.fixture(scope="module")
def captured_runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """One valid M3 and one valid A0 run over the whole fixture bundle, through the
    ordinary runner (spawned workers), stamped with a fixed clean code identity."""
    from benchmark.runner import run_experiment

    bundle = load_bundle(MIXED)
    base = tmp_path_factory.mktemp("runs")
    runs = {}
    for arm, path in (("metis", M3), ("reference", A0)):
        manifest = run_experiment(
            bundle, load_profile(path), results_dir=base / arm, replay_command="cmd"
        )
        assert manifest.complete
        run_dir = Path(manifest.run_dir)
        _edit_manifest(run_dir, lambda m: m["environment"].update(CLEAN))
        runs[arm] = run_dir
    return runs


def _edit_manifest(run_dir: Path, edit: Callable[[dict[str, Any]], Any]) -> None:
    path = run_dir / "manifest.json"
    manifest = json.loads(path.read_text())
    edit(manifest)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _edit_records(run_dir: Path, edit: Callable[[list[dict[str, Any]]], Any]) -> None:
    """Rewrite the records and re-seal the checksum, so only the content check can object."""
    path = run_dir / "cases.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines() if line]
    edit(records)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
    _edit_manifest(run_dir, lambda m: m.update(cases_sha256=sha256_file(path)))


@pytest.fixture
def runs(
    captured_runs: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    monkeypatch.setattr(_tool(), "_source_identity", lambda: dict(CLEAN))
    copies = {}
    for arm, run_dir in captured_runs.items():
        copies[arm] = tmp_path / arm
        shutil.copytree(run_dir, copies[arm])
    return copies


def _measured(out: Path, runs: dict[str, Path], *extra: str, **kw: Any) -> list[str]:
    return _argv(
        out, "--metis-run", str(runs["metis"]), "--reference-run", str(runs["reference"]),
        *extra, **kw,
    )  # fmt: skip


def test_valid_measured_runs_are_ingested_with_their_identities(
    runs: dict[str, Path], tmp_path: Path
) -> None:
    out = tmp_path / "s2.json"
    assert _tool().main(_measured(out, runs, "--x7")) == 0
    document = json.loads(out.read_text())
    assert document["results_from"] == "measured run records"
    assert document["selection"]["scope"] == "full"
    assert document["s2_gate"] == {"established": True, "not_established_because": []}
    assert document["source"] == CLEAN and document["x7_identical"] is True
    metis_run, ref_run = document["measured_runs"]
    manifest = json.loads((runs["metis"] / "manifest.json").read_text())
    assert metis_run["run_id"] == manifest["run_id"]
    assert metis_run["cases_sha256"] == manifest["cases_sha256"]
    assert metis_run["manifest_sha256"] == sha256_file(runs["metis"] / "manifest.json")
    assert metis_run["profile_path"] == str(M3) and metis_run["algorithms"] == ["metis_inspired"]
    assert ref_run["algorithms"] == ["incremental_graph"] and ref_run["git_dirty"] is False
    for row in document["cases"]:
        assert row["results_from"] == "measured run records"
        assert row["verdict"] == "identical" and row["quotes_within_reference"] is True
    # A subset of the same valid evidence is only a smoke.
    subset = tmp_path / "subset.json"
    assert _tool().main(_measured(subset, runs, "--first", "1")) == 0
    gate = json.loads(subset.read_text())["s2_gate"]
    assert gate["established"] is False and "subset" in gate["not_established_because"][0]


def test_in_process_evidence_never_establishes_the_gate(tmp_path: Path) -> None:
    out = tmp_path / "s2.json"
    assert _tool().main(_argv(out, "--case", "usdt_wmnt_evidence_split", "--x7")) == 0
    document = json.loads(out.read_text())
    assert document["results_from"] == "diagnostic in-process re-solves"
    assert document["verdicts"] == {"identical": 1} and document["x7_identical"] is True
    assert document["label_hops"] == 3 and document["allowed_classes"] == ["quote_failure", "tie"]
    assert document["s2_gate"]["established"] is False and document["measured_runs"] == []
    assert _tool().main(_argv(out, "--first", "1")) == 2  # never overwrites an artifact


def test_mixed_evidence_is_refused(
    runs: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "s2.json"
    for extra in (["--metis-run", str(runs["metis"])], ["--reference-run", str(runs["reference"])]):
        _refused(capsys, out, _argv(out, *extra), "must be given together")


def _state(m: dict[str, Any]) -> None:
    m["state"] = "interrupted"


def _revision(m: dict[str, Any]) -> None:
    m["environment"]["git_revision"] = "e" * 40


def _dirty(m: dict[str, Any]) -> None:
    m["environment"]["git_dirty"] = True


def _no_revision(m: dict[str, Any]) -> None:
    m["environment"]["git_revision"] = None


def _settings(m: dict[str, Any]) -> None:
    m["resolved_profile"]["graph"]["label_hops"] = 4


def _budget(m: dict[str, Any]) -> None:
    m["resolved_profile"]["budget"]["max_quotes"] = 1
    m["measurement"]["budget"]["max_quotes"] = 1


def _order(m: dict[str, Any]) -> None:
    m["measurement"]["case_order"] = m["measurement"]["case_order"][:-1]


def _duplicate(records: list[dict[str, Any]]) -> None:
    records.append(copy.deepcopy(records[0]))


def _drop(records: list[dict[str, Any]]) -> None:
    records.pop()


def _wrong_arm(records: list[dict[str, Any]]) -> None:
    records[0]["algorithm"] = "incremental_graph"


def _unknown(records: list[dict[str, Any]]) -> None:
    records[0]["case_id"] = "not-in-bundle"


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (_state, "run is incomplete"),
        (_revision, "git_revision"),
        (_no_revision, "git_revision"),
        (_dirty, "not clean"),
        (_settings, "effective settings differ"),
        (_budget, "effective settings differ"),
        (_order, "case_order"),
    ],
    ids=["incomplete", "revision", "no-revision", "dirty", "settings", "budget", "order"],
)
@pytest.mark.parametrize("arm", ["metis", "reference"])
def test_manifest_mismatches_are_refused(
    runs: dict[str, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutate: Callable[[dict[str, Any]], None],
    match: str,
    arm: str,
) -> None:
    if mutate is _settings and arm == "reference":
        mutate = _budget  # incremental_graph has no graph.label_hops
    _edit_manifest(runs[arm], mutate)
    out = tmp_path / "s2.json"
    _refused(capsys, out, _measured(out, runs), match)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (_duplicate, "duplicate record"),
        (_drop, "no record for 1 case"),
        (_wrong_arm, "is algorithm 'incremental_graph'"),
        (_unknown, "unknown case"),
    ],
    ids=["duplicate", "missing", "wrong-arm", "unknown"],
)
def test_record_mismatches_are_refused_not_last_write_wins(
    runs: dict[str, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutate: Callable[[list[dict[str, Any]]], None],
    match: str,
) -> None:
    _edit_records(runs["metis"], mutate)
    out = tmp_path / "s2.json"
    _refused(capsys, out, _measured(out, runs), match)


def test_tampered_records_wrong_bundle_arms_profile_and_source_are_refused(
    runs: dict[str, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "s2.json"
    with (runs["metis"] / "cases.jsonl").open("a") as fh:  # checksum no longer matches
        fh.write("\n")
    _refused(capsys, out, _measured(out, runs), "checksum does not match")
    shutil.rmtree(runs["metis"])
    shutil.copytree(runs["reference"], runs["metis"])  # the A0 run offered as the M3 arm
    _refused(capsys, out, _measured(out, runs), "algorithms ['incremental_graph']")


def test_runs_over_another_bundle_or_profile_or_revision_are_refused(
    runs: dict[str, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "s2.json"
    cpmm = FIXTURES / "cpmm_graph"
    _refused(capsys, out, _measured(out, runs, bundle=cpmm), "bundle_hash")
    _refused(capsys, out, _measured(out, runs, profile=M4), "effective settings differ")
    monkeypatch.setattr(_tool(), "_source_identity", lambda: {**CLEAN, "git_dirty": True})
    _refused(capsys, out, _measured(out, runs), "does not run on a clean checkout")
    monkeypatch.setattr(_tool(), "_source_identity", lambda: {**CLEAN, "git_revision": "a" * 40})
    _refused(capsys, out, _measured(out, runs), "git_revision")

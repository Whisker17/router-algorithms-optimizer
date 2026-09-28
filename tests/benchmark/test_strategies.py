"""`main.py run|quote --strategies` (WHI-1528): base and optimized strategy groups.

End to end through the ordinary CLI, spawned workers and saved records (offline, frozen
fixtures), plus the profile-derivation and validation rules behind it:

- the default `all` runs the source profile's base strategies, then the two named
  optimized strategies, sequentially; `base` / `optimized` / `profile` select as documented,
  intentional subsets and custom algorithms are kept, nothing is duplicated;
- invalid, empty or incompatible selections are refused before anything is written;
- the effective profile (algorithms, recipe settings, groups, source identity) is saved,
  replays with `--strategies profile` into identical deterministic records and resolves
  identically; quote keeps one attempt per algorithm, run keeps the profile's measurement;
- terminal, HTML and CSV output show the groups (failures included) from the records only;
  runs without the new metadata keep their previous presentation;
- source profiles and the L01 / L01-SB / L08 protocol files are unchanged.
"""

from __future__ import annotations

import csv
import hashlib
import json
import multiprocessing
import re
import shlex
import signal
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
from typing import Any

import pytest
import yaml

import benchmark.latency as latency
import benchmark.profile as profile_module
import main
from benchmark.profile import ProfileError, load_profile, parse_profile, read_profile_document
from benchmark.results import load_case_records, load_manifest
from benchmark.runner import compare_runs
from benchmark.strategies import StrategySelectionError, derive
from routing.algorithms.registry import ALGORITHMS, BASE_STRATEGIES, OPTIMIZED_STRATEGIES
from snapshot.bundle import load_bundle

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "tests" / "fixtures" / "corpus" / "bundle"
MIXED = REPO / "tests" / "fixtures" / "routing" / "mantle_mixed"
LB = REPO / "tests" / "fixtures" / "moe_lb" / "bundle"
SIX = list(BASE_STRATEGIES)
EIGHT = [*SIX, *OPTIMIZED_STRATEGIES]
STANDARD = ("daily_gross.yaml", "daily.yaml", "full_gross.yaml", "full.yaml")
TEST_ALARM_SECONDS = 240

# Pins that must never move (L08 v1 pins L01 v1 and L01-SB v1; L01 pins daily_gross).
PINNED = {
    "config/latency/l08.yaml": "e7add86add573033f04c6790f705cb2951712ffc272a4498d03f01251101beaa",
    "config/latency/l01.yaml": "961fb52208c7baac3d0ffe492cef89818498543f1f00f56217d2f99113e27f1b",
    "config/latency/l01-sufficient-budget.yaml": (
        "4059506da09280cdb0e8d4a94ac6ecef1c786113016e8eee430bef38089db43b"
    ),
}


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


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _doc(name: str = "daily_gross.yaml") -> dict[str, Any]:
    return read_profile_document(REPO / "config" / name)


def _derive(doc: dict[str, Any], mode: str) -> tuple[dict[str, Any], Any]:
    return derive(doc, mode, source_path="src.yaml", source_sha256="s" * 64)


def _write(tmp_path: Path, doc: dict[str, Any], name: str = "source.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _small(doc: dict[str, Any], **measurement: Any) -> dict[str, Any]:
    """daily_gross's objective/search/budget with a smaller graph and measurement."""
    out: dict[str, Any] = json.loads(json.dumps(doc))
    out["graph"] = {"chunks": 20}
    out["measurement"] = {"warmup": 0, "repeats": 1, "seed": 7, "order": "fixed",
                          "memory_pass": False, **measurement}  # fmt: skip
    return out


# ------------------------------------------------------------------ derivation


@pytest.mark.parametrize("name", STANDARD)
def test_standard_profiles_default_to_six_base_then_two_optimized(name: str) -> None:
    source = REPO / "config" / name
    before = source.read_bytes()
    document, profile = _derive(_doc(name), "all")
    assert list(profile.algorithms) == EIGHT == document["algorithms"]
    assert document["selection"]["groups"] == {
        "base": SIX, "optimized": list(OPTIMIZED_STRATEGIES), "custom": []
    }  # fmt: skip
    raw = _doc(name)
    # everything but the selection is the source's own (objective, search, budget, ...)
    for key in ("objective", "search", "graph", "budget", "measurement", "worker"):
        assert document[key] == raw[key]
    assert source.read_bytes() == before


def test_group_and_profile_selectors() -> None:
    doc = _doc()
    assert list(_derive(doc, "base")[1].algorithms) == SIX
    base_doc = _derive(doc, "base")[0]
    assert "strategies" not in base_doc and base_doc["selection"]["mode"] == "base"
    assert list(_derive(doc, "optimized")[1].algorithms) == list(OPTIMIZED_STRATEGIES)
    exact, profile = _derive(doc, "profile")
    assert exact == doc and profile.selection == {} and profile.strategies == {}
    assert profile.resolved() == load_profile(REPO / "config" / "daily_gross.yaml").resolved()
    with pytest.raises(StrategySelectionError, match="expected one of"):
        _derive(doc, "everything")


def test_custom_subsets_are_kept_and_nothing_is_duplicated() -> None:
    doc = _doc()
    doc["algorithms"] = ["uni_sor_optimized", "direct", "uni_sor_fast", "path_split"]
    doc["shortlist"] = {"probe_percents": [10, 100], "routes_per_probe": 3, "direct_routes": 1}
    doc["strategies"] = {"uni_sor_optimized": profile_module.strategy_entry("uni_sor_optimized")}
    all_doc, profile = _derive(doc, "all")
    assert list(profile.algorithms) == [
        "direct", "uni_sor_fast", "path_split", "uni_sor_adaptive", "uni_sor_optimized"
    ]  # fmt: skip
    assert all_doc["selection"]["groups"] == {
        "base": ["direct", "path_split"], "optimized": list(OPTIMIZED_STRATEGIES),
        "custom": ["uni_sor_fast"],
    }  # fmt: skip
    # the raw configurable variant keeps its own global settings, never a preset's
    assert profile.algorithm_config(ALGORITHMS["uni_sor_fast"]).params[
        "routes_per_probe"] == 3  # fmt: skip
    assert list(_derive(doc, "base")[1].algorithms) == ["direct", "path_split"]
    assert list(_derive(doc, "optimized")[1].algorithms) == list(OPTIMIZED_STRATEGIES)
    # applying `all` to an already derived profile changes nothing but the source record
    again, repeated = _derive(all_doc, "all")
    assert list(repeated.algorithms) == list(profile.algorithms)
    assert again["strategies"] == all_doc["strategies"]


@pytest.mark.parametrize(
    ("change", "mode", "match"),
    [
        ({"algorithms": ["uni_sor_fast"], "shortlist": {"probe_percents": [100],
          "routes_per_probe": 1, "direct_routes": 0}}, "base", "selects no base strategy"),
        ({"search": {"max_hops": 2}}, "all", r"requires search\.max_splits"),
        ({"search": {"max_hops": 2, "max_splits": 4, "percent_step": 10}}, "all",
         "probe_percents"),
        ({"search": {"max_hops": 2, "max_splits": 4, "percent_step": 25}}, "optimized",
         "probe_percents"),
        ({"surprise": 1}, "all", "unknown key"),
        ({"algorithms": []}, "all", "non-empty list"),
    ],
)  # fmt: skip
def test_invalid_or_incompatible_selections_are_refused(
    change: dict[str, Any], mode: str, match: str
) -> None:
    doc = {**_doc(), **change}
    with pytest.raises(ProfileError, match=match) as info:
        _derive(doc, mode)
    if isinstance(info.value, StrategySelectionError) and "cannot run" in str(info.value):
        assert "--strategies base" in str(info.value) and "--strategies profile" in str(info.value)


# ------------------------------------------------------------------ loader


def _effective() -> dict[str, Any]:
    return _derive(_doc(), "all")[0]


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d["strategies"]["uni_sor_optimized"]["shortlist"].update(routes_per_probe=9),
         "differs from recipe L08 arm H4"),
        (lambda d: d["strategies"]["uni_sor_adaptive"]["sampling"].update(refine_radius=2),
         "differs from recipe L08 arm H3"),
        (lambda d: d["strategies"]["uni_sor_optimized"]["controls"].pop("L04"),
         "differs from recipe"),
        (lambda d: d["strategies"]["uni_sor_adaptive"]["recipe"].update(arm="H4"),
         "registered recipe"),
        (lambda d: d["strategies"].pop("uni_sor_adaptive"), r"strategies\.uni_sor_adaptive"),
        (lambda d: d["strategies"].update(uni_sor_fast={}), "not a named optimized strategy"),
        (lambda d: d.update(algorithms=d["algorithms"][:-1],
                            selection={**d["selection"], "groups": {**d["selection"]["groups"],
                                       "optimized": ["uni_sor_adaptive"]}}),
         "not in algorithms"),
        (lambda d: d["selection"]["groups"].update(base=SIX[:-1], custom=["uni_sor_port"]),
         "do not belong to the custom group"),
        (lambda d: d["selection"]["groups"].update(base=SIX[::-1]), "order"),
        (lambda d: d["selection"]["groups"].update(optimized=["uni_sor_adaptive"]),
         "exactly once"),
        (lambda d: d["selection"].update(mode="profile"), "mode"),
    ],
)  # fmt: skip
def test_loader_binds_named_strategies_to_their_recipes(mutate: Any, match: str) -> None:
    doc = _effective()
    mutate(doc)
    with pytest.raises(ProfileError, match=match):
        parse_profile(doc, "effective.yaml")


def test_changed_recipe_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config" / "latency").mkdir(parents=True)
    for name in ("l08.yaml", "l01.yaml", "l01-sufficient-budget.yaml"):
        text = (REPO / "config" / "latency" / name).read_text()
        (tmp_path / "config" / "latency" / name).write_text(text + ("\n# edited\n" * (
            name == "l08.yaml")))  # fmt: skip
    from routing.algorithms import uni_sor_strategies

    effective = _effective()  # derived from the real, unchanged file
    factory = ALGORITHMS["uni_sor_adaptive"]
    config = parse_profile(effective, "e.yaml").algorithm_config(factory)
    monkeypatch.setattr(uni_sor_strategies, "REPO_ROOT", tmp_path)  # the one shared reader
    with pytest.raises(ProfileError, match="differs from the recipe pin"):
        parse_profile(effective, "effective.yaml")
    assert factory.prepare is not None  # the direct factory path refuses it as well
    with pytest.raises(uni_sor_strategies.UniSorStrategyError, match="recipe pin"):
        factory.prepare(load_bundle(MIXED), config)


def test_effective_profile_round_trips_and_legacy_profiles_resolve_unchanged(
    tmp_path: Path,
) -> None:
    document, profile = _derive(_doc(), "all")
    reloaded = load_profile(_write(tmp_path, document))
    assert reloaded.resolved() == profile.resolved()
    resolved = profile.resolved()
    assert resolved["selection"]["mode"] == "all"
    assert set(resolved["strategies"]) == set(OPTIMIZED_STRATEGIES)
    for name in OPTIMIZED_STRATEGIES:
        provenance = resolved["algorithm_config"][name]["provenance"]
        assert provenance["recipe"] == resolved["strategies"][name]["recipe"]
    # a replayed effective profile under `profile` mode is itself, byte for byte
    assert _derive(document, "profile")[0] == document
    for path in sorted((REPO / "config").glob("*.yaml")):
        text = path.read_text()
        if "schema_version" not in text or "algorithms:" not in text:
            continue
        legacy = load_profile(path).resolved()
        assert "strategies" not in legacy and "selection" not in legacy, path.name


def test_profiles_and_protocol_pins_are_unchanged() -> None:
    for rel, digest in PINNED.items():
        assert _sha(REPO / rel) == digest, rel
    arms = latency.load_arms(REPO / "config" / "latency" / "l08.yaml")
    assert (arms.protocol_sha256, arms.sufficient_sha256) == (
        PINNED["config/latency/l01.yaml"],
        PINNED["config/latency/l01-sufficient-budget.yaml"],
    )
    protocol = latency.load_protocol(REPO / "config" / "latency" / "l01.yaml")
    assert _sha(REPO / protocol.profile_path) == protocol.profile_sha256
    # L08 keeps its registered scopes: R / E* the pinned six, H* uni_sor_fast only
    for name, arm in arms.arms.items():
        algorithms = latency.arm_profile(arm, protocol).algorithms
        assert set(algorithms).isdisjoint(OPTIMIZED_STRATEGIES), name


# ------------------------------------------------------------------ CLI: early refusal


def test_incompatible_cli_selections_write_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results, quotes = tmp_path / "results", tmp_path / "quotes"
    smoke = str(REPO / "config" / "smoke.yaml")
    assert main.main(["run", "--bundle", str(CORPUS), "--profile", smoke,
                      "--results-dir", str(results)]) == 1  # fmt: skip
    err = capsys.readouterr().err
    assert "cannot run the optimized strategies" in err and "--strategies profile" in err
    coarse = _write(tmp_path, {**_small(_doc()), "search": {"max_hops": 2, "max_splits": 2,
                                                            "percent_step": 25}})  # fmt: skip
    assert main.main(["quote", "--bundle", str(CORPUS), "--profile", str(coarse),
                      "--token-in", "USDC", "--token-out", "USDT0", "--amount", "1",
                      "--quotes-dir", str(quotes)]) == 1  # fmt: skip
    assert "probe_percents" in capsys.readouterr().err
    only_custom = _write(tmp_path, {**_small(_doc()), "algorithms": ["uni_sor_fast"],
                                    "shortlist": {"probe_percents": [100], "routes_per_probe": 1,
                                                  "direct_routes": 0}}, "custom.yaml")  # fmt: skip
    assert main.main(["run", "--bundle", str(CORPUS), "--profile", str(only_custom),
                      "--results-dir", str(results), "--strategies", "base"]) == 1  # fmt: skip
    assert "selects no base strategy" in capsys.readouterr().err
    assert not results.exists() and not quotes.exists()


# ------------------------------------------------------------------ CLI: quote


def _saved_run(stdout: str) -> Path:
    match = re.search(r"\(run (\S+)\)", stdout)
    assert match, stdout
    return Path(match.group(1))


def _replay(command: str) -> list[str]:
    parts = shlex.split(command)
    assert parts[:4] == ["uv", "run", "python", "main.py"]
    return parts[4:]


def test_quote_defaults_to_both_groups_once_each_and_replays_exactly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = REPO / "config" / "daily_gross.yaml"
    before = source.read_bytes()
    args = ["quote", "--bundle", str(CORPUS), "--profile", str(source), "--token-in", "USDC",
            "--token-out", "USDT0", "--amount", "1500.25", "--quotes-dir", str(tmp_path / "q"),
            "--details"]  # fmt: skip
    assert main.main(args) == 0
    out = capsys.readouterr().out
    run_dir = _saved_run(out)
    manifest = load_manifest(run_dir)
    assert list(manifest.algorithms) == EIGHT
    assert [e["algorithm"] for e in manifest.prepare_events] == EIGHT  # one worker each, in order
    assert {k: manifest.measurement[k] for k in ("warmup", "repeats", "memory_pass")} == {
        "warmup": 0, "repeats": 1, "memory_pass": False}  # fmt: skip
    records = load_case_records(run_dir)
    assert [r["algorithm"] for r in records] == EIGHT
    for record in records:
        assert record["measurement"]["attempts_completed"] == 1
        assert len(record["measurement"]["solve_seconds"]) == 1
    assert source.read_bytes() == before
    quote = json.loads((run_dir.parent.parent / "quote.json").read_text())
    assert quote["strategies"] == {"mode": "all", "groups": {
        "base": SIX, "optimized": list(OPTIMIZED_STRATEGIES)}}  # fmt: skip
    # compact rows and details are grouped; failures would sit in their group's rows
    table = out.split("Base strategies:", 1)[1]
    base_rows, optimized_rows = table.split("Optimized strategies:", 1)
    for name in SIX:
        assert re.search(rf"^{name}\s+ok\s", base_rows, re.M), name
    for name in OPTIMIZED_STRATEGIES:
        assert re.search(rf"^{name}\s+ok\s", optimized_rows.split("\n\n")[0], re.M), name
        assert f"{name}: experimental optimized strategy, recipe L08" in out
    assert out.index("== Base strategies ==") < out.index("[direct] ok")
    assert out.index("== Optimized strategies ==") < out.index("[uni_sor_adaptive] ok")
    # replay: the saved effective profile under --strategies profile, never re-expanded
    assert manifest.replay_command.endswith("--strategies profile")
    capsys.readouterr()
    assert main.main(_replay(manifest.replay_command)) == 0
    replayed = [p for p in run_dir.parent.iterdir() if p != run_dir]
    assert len(replayed) == 1
    again = load_manifest(replayed[0])
    assert again.resolved_profile == manifest.resolved_profile
    assert again.profile_sha256 == manifest.profile_sha256
    assert compare_runs(run_dir, replayed[0]) == []
    report_dir = tmp_path / "report"
    assert main.main(["report", str(run_dir), "--output", str(report_dir)]) == 0
    text = (report_dir / "single_request.txt").read_text()
    assert "Optimized strategies:" in text and "== Optimized strategies ==" in text


# ------------------------------------------------------------------ CLI: batch run + report


def test_run_keeps_measurement_saves_the_selection_and_replays_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write(tmp_path, _small(_doc(), warmup=1, repeats=2, seed=11, order="reverse",
                                     memory_pass=True))  # fmt: skip
    results = tmp_path / "results"
    argv = ["run", "--bundle", str(MIXED), "--profile", str(source), "--results-dir", str(results)]
    assert main.main(argv) == 0
    out = capsys.readouterr().out
    assert "strategies: all -- Base strategies (6): direct" in out
    assert "Optimized strategies (2): uni_sor_adaptive, uni_sor_optimized" in out
    (run_dir,) = results.iterdir()  # the effective profile lives inside the run directory
    manifest = load_manifest(run_dir)
    assert manifest.profile_path == str(run_dir / "profile.yaml")
    assert manifest.profile_sha256 == _sha(run_dir / "profile.yaml")
    assert list(manifest.algorithms) == EIGHT
    m = manifest.measurement
    assert (m["warmup"], m["repeats"], m["seed"], m["order"], m["memory_pass"]) == (
        1, 2, 11, "reverse", True)  # fmt: skip
    assert manifest.memory_record_count == 8 * 4
    selection = manifest.resolved_profile["selection"]
    assert selection["source_profile"] == {"path": str(source), "sha256": _sha(source)}
    records = load_case_records(run_dir)
    assert [r["algorithm"] for r in records[::4]] == EIGHT  # base workers, then optimized
    for record in records:
        assert record["measurement"]["attempts_completed"] == 3
        if record["algorithm"] in OPTIMIZED_STRATEGIES:
            assert record["search"]["strategy"]["name"] == record["algorithm"]
    capsys.readouterr()
    assert main.main(_replay(manifest.replay_command)) == 0
    replayed = next(p for p in results.iterdir() if p != run_dir)
    assert load_manifest(replayed).resolved_profile == manifest.resolved_profile
    assert compare_runs(run_dir, replayed) == []
    # --strategies profile of the source: its exact six, no selection, no saved profile
    capsys.readouterr()
    exact = tmp_path / "exact"
    assert main.main([*argv[:-1], str(exact), "--strategies", "profile"]) == 0
    (exact_run,) = exact.iterdir()
    legacy = load_manifest(exact_run)
    assert list(legacy.algorithms) == SIX and "selection" not in legacy.resolved_profile
    assert legacy.profile_path == str(source) and not (exact_run / "profile.yaml").exists()


def test_reports_group_strategies_with_failures_and_escape_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write(tmp_path, _small(_doc()), "src<b>&'x'.yaml")
    results = tmp_path / "results"
    argv = ["run", "--bundle", str(LB), "--profile", str(source), "--results-dir", str(results)]
    assert main.main(argv) == 0
    assert main.main([*argv[:-1], str(tmp_path / "legacy"), "--strategies", "profile"]) == 0
    (grouped,) = results.iterdir()
    (legacy,) = (tmp_path / "legacy").iterdir()
    out = tmp_path / "report"
    assert main.main(["report", str(grouped), str(legacy), "--output", str(out)]) == 0
    html = (out / "report.html").read_text()
    grouped_html, legacy_html = html.split(f"id='run-{legacy.name}'")
    assert "<h2>Strategy groups</h2>" in grouped_html and "Strategy groups" not in legacy_html
    assert "<h3>Base strategies (6)</h3>" in grouped_html
    assert "<h3>Optimized strategies (2)</h3>" in grouped_html
    assert "Optimized strategy — experimental heuristic" in grouped_html
    assert "src&lt;b&gt;&amp;&#x27;x&#x27;.yaml" in grouped_html and "src<b>" not in html
    rows = list(csv.DictReader((out / "strategy_groups.csv").open()))
    assert [r["run_id"] for r in rows] == [grouped.name] * 8  # only the grouped run
    assert [(r["group"], r["algorithm"]) for r in rows] == [
        *(("base", a) for a in SIX),
        *(("optimized", a) for a in OPTIMIZED_STRATEGIES),
    ]
    by_name = {r["algorithm"]: r for r in rows}
    lb_cases = len(load_manifest(grouped).measurement["case_order"])
    for name in ("uni_sor_port", *OPTIMIZED_STRATEGIES):  # LB-only fixture: every row kept
        assert by_name[name]["unsupported"] == str(lb_cases) and by_name[name]["ok"] == "0"
    assert int(by_name["direct"]["no_route"]) > 0  # base failures visible too
    assert json.loads(by_name["uni_sor_optimized"]["recipe"])["arm"] == "H4"
    # cross-group pairs are in the ordinary pairwise table
    pairs = list(csv.DictReader((out / "paired_gross.csv").open()))
    assert any(p["algorithm"] == "uni_sor_adaptive" and p["baseline"] == "path_split"
               for p in pairs if p["run_id"] == grouped.name)  # fmt: skip


def test_custom_profile_reports_the_recorded_execution_order_not_the_group_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """PR-F2: groups are a presentation. A custom algorithm listed between base ones runs
    there; the report says so instead of claiming the group order is the run order."""
    custom = {**_small(_doc()), "algorithms": ["direct", "uni_sor_fast", "path_split"],
              "shortlist": {"probe_percents": [10, 100], "routes_per_probe": 3,
                            "direct_routes": 1}}  # fmt: skip
    source = _write(tmp_path, custom)
    order = ["direct", "uni_sor_fast", "path_split", *OPTIMIZED_STRATEGIES]
    assert main.main(["quote", "--bundle", str(CORPUS), "--profile", str(source),
                      "--token-in", "USDC", "--token-out", "USDT0", "--amount", "1500.25",
                      "--quotes-dir", str(tmp_path / "q"), "--details"]) == 0  # fmt: skip
    out = capsys.readouterr().out
    run_dir = _saved_run(out)
    manifest = load_manifest(run_dir)
    assert list(manifest.algorithms) == order  # the schedule keeps the profile's order
    assert [r["algorithm"] for r in load_case_records(run_dir)] == order
    assert [e["algorithm"] for e in manifest.prepare_events] == order
    assert f"execution order (sequential, as recorded): {', '.join(order)}" in out
    assert "sequentially in that order" not in out
    # the grouped presentation is unchanged: base, optimized, then custom
    base = out.index("Base strategies:")
    optimized = out.index("Optimized strategies:")
    other = out.index("Other profile-selected strategies:")
    assert base < optimized < other
    assert re.search(r"^uni_sor_fast\s+ok\s", out[other:], re.M)
    assert out.index("== Base strategies ==") < out.index("== Optimized strategies ==") < out.index(
        "== Other profile-selected strategies ==")  # fmt: skip
    # the HTML report of a batch run of the same source states the recorded order too
    results = tmp_path / "results"
    assert main.main(["run", "--bundle", str(MIXED), "--profile", str(source),
                      "--results-dir", str(results)]) == 0  # fmt: skip
    (batch,) = results.iterdir()
    assert list(load_manifest(batch).algorithms) == order
    assert main.main(["report", str(batch), "--output", str(tmp_path / "report")]) == 0
    html = (tmp_path / "report" / "report.html").read_text()
    assert f"the recorded order <code>{', '.join(order)}</code>" in html
    assert "base strategies first" not in html
    assert "<h3>Other profile-selected strategies (1)</h3>" in html

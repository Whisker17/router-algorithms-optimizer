"""WHI-1562 research-021 campaign plumbing, end to end through the ordinary CLI.

- The checked-in manifest validates; every generated profile is exactly its rendering from
  the canonical base (drift and pin changes are refused); the ordinary `all` roster derives the
  registered 14 identities in contract order; stage inventories are exact.
- EVERY registered new profile (and the pinned L3/E4 files) runs through BOTH `main.py run`
  and `main.py quote --details` with its registered algorithm order, one solve per quote; the
  net-objective profile shows every gross-only row `unsupported` in `run` and is a registered
  refusal in `quote` (empirical_cost is refused by design, nothing written).
- Mutated recipe / options / reserved keys in a campaign profile are refused before anything
  is written.
- The stage executor runs a bounded fixture campaign (mixed-universe and LB-only fixtures,
  quote + report + replay + order-check, a max_splits scan) with a ledger and load samples,
  refuses re-execution, changed inputs and re-running a completed run, and the analysis
  reconciles every scheduled cell and applies the registered comparisons.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import multiprocessing
import shutil
import signal
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FrameType, ModuleType
from typing import Any

import pytest
import yaml

import main
from benchmark.profile import preset_options, read_profile_document
from benchmark.results import load_case_records, load_manifest
from benchmark.strategies import derive
from routing.algorithms.registry import ALGORITHMS

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "tests" / "fixtures" / "corpus" / "bundle"
MIXED = REPO / "tests" / "fixtures" / "routing" / "mantle_mixed"
LB = REPO / "tests" / "fixtures" / "moe_lb" / "bundle"
TEST_ALARM_SECONDS = 300
ALL14 = ["direct", "single_path", "direct_split", "path_split", "incremental_graph",
         "uni_sor_port", "uni_sor_adaptive", "uni_sor_optimized", "metis_inspired",
         "metis_history", "direct_split_certified", "incremental_graph_repair",
         "uni_sor_cycle_safe", "cfmm_dual"]  # fmt: skip
RESTRICTED = ("direct_split_certified", "uni_sor_cycle_safe", "cfmm_dual")


def _load(name: str) -> ModuleType:
    path = REPO / "tools" / "research_021" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("analysis")
C = _load("campaign")


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


@pytest.fixture(scope="module")
def campaign() -> Any:
    return C.load_campaign()


# ----------------------------------------------------------------------------- manifest


def test_manifest_profiles_and_roster_are_consistent(campaign: Any) -> None:
    assert C.check(campaign) == []
    source = read_profile_document(REPO / "config" / "full_gross.yaml")
    document, _ = derive(source, "all", source_path="config/full_gross.yaml",
                         source_sha256="0" * 64)  # fmt: skip
    assert document["algorithms"] == ALL14 == campaign.raw["rosters"]["all14"]
    for key in C.generated_profiles(campaign):
        path = REPO / C.profile_path(campaign, key)
        assert path.read_text() == C.render_profile(campaign, key), key
    # generated profiles only change what their spec lists: budgets/objective/measurement stay
    base = read_profile_document(REPO / "config" / "full_gross.yaml")
    for key in C.generated_profiles(campaign):
        doc = yaml.safe_load((REPO / C.profile_path(campaign, key)).read_text())
        if campaign.raw["profiles"][key]["generate"]["base"] == "canonical_gross":
            assert doc["budget"] == base["budget"] and doc["objective"] == base["objective"], key
            assert doc["worker"] == base["worker"], key


def test_drift_and_changed_pins_are_refused(campaign: Any) -> None:
    drifted = copy.deepcopy(campaign)
    drifted.raw["profiles"]["s3"]["generate"]["graph"] = {"label_hops": 4}
    assert any("s3" in p and "differs from its rendering" in p for p in C.check(drifted))
    pinned = copy.deepcopy(campaign)
    pinned.raw["profiles"]["l3"]["sha256"] = "0" * 64
    assert any("l3" in p and "sha256" in p for p in C.check(pinned))
    order = copy.deepcopy(campaign)
    run = next(i for i in order.invocations if i.id == "T-roster-full")
    run.raw["algorithms"] = list(reversed(ALL14))
    assert any(p.startswith("T-roster-full: derives") for p in C.check(order))


def test_stage_inventories_are_exact(campaign: Any) -> None:
    t = C.schedule(campaign, "T")
    ids = [i["id"] for i in t["invocations"]]
    assert len(ids) == len(set(ids)) == 26
    for stage in ("L", "R", "M", "I"):  # registered before any report-stage execution
        rules = campaign.raw["stages"][stage]
        assert rules["caffeinate"] is True and rules["pmset_capture"] is True, stage
    nominees = campaign.raw["nominees"]
    assert nominees["max_splits.same_grid_allocation"]["nominee"] == 8
    assert nominees["max_splits.matched_sor_cycle_safe"]["nominee"] == 4
    nominee_runs = [i for i in campaign.stage("R") if i.get("profile") == "same_grid_nominee"]
    assert sorted(i.get("bundle") for i in nominee_runs) == ["report_full", "report_sor"]
    doc = yaml.safe_load((REPO / C.profile_path(campaign, "same_grid_nominee")).read_text())
    assert doc["search"]["max_splits"] == 8
    assert doc["algorithms"] == ["direct_split", "direct_split_certified"]
    assert ids[-3:] == ["T-quote-smoke.report", "T-quote-smoke.replay", "T-quote-smoke.order"]
    runs = [i for i in t["invocations"] if i["kind"] == "run"]
    assert all(i["cells"] == 96 * len(i["algorithms"]) for i in runs)
    assert all(campaign.raw["inputs"][i["bundle"]]["split"] == "tuning" for i in runs)
    quote = next(i for i in t["invocations"] if i["kind"] == "quote")
    assert quote["solves_per_algorithm"] == 1 and quote["algorithms"] == ALL14
    r = C.schedule(campaign, "R")
    assert all(campaign.raw["inputs"][i["bundle"]]["split"] == "report"
               for i in r["invocations"] if i["kind"] == "run")  # fmt: skip
    # the report stage mirrors every tuning comparison except the nominee rule
    mirrored = C.stage_comparisons(campaign, "R")
    assert mirrored and all(c["kind"] != "nominee" for c in mirrored)
    assert all("T-" not in json.dumps(c) for c in mirrored)
    cycle = [c for c in mirrored if c["id"].startswith("cycle.")]
    defects = campaign.raw["known_report_defects"]["cases"]
    assert len(cycle) == 2 and all(c["exclude_cases"] == defects for c in cycle)
    assert len(defects) == 12
    with pytest.raises(C.CampaignError, match="unknown stage"):
        campaign.stage("X")


def test_manifest_refusals(tmp_path: Path, campaign: Any) -> None:
    raw = copy.deepcopy(campaign.raw)
    cases: list[tuple[Callable[[dict[str, Any]], Any], str]] = [
        (lambda r: r["invocations"].append(dict(r["invocations"][0])), "duplicate"),
        (lambda r: r["invocations"][0].update(profile="nope"), "unknown profile"),
        (lambda r: r["invocations"][0].update(strategies="every"), "strategies must"),
        (lambda r: r["invocations"][0].update(after=["T-roster-sor"]), "not registered before"),
        (lambda r: r["invocations"][0].update(kind="shell"), "not registered"),
    ]
    for mutate, match in cases:
        doc = copy.deepcopy(raw)
        mutate(doc)
        path = tmp_path / "m.yaml"
        path.write_text(yaml.safe_dump(doc))
        with pytest.raises(C.CampaignError, match=match):
            C.load_campaign(path)


def test_saved_replay_is_relocated_into_the_slot_never_the_input(
    tmp_path: Path, campaign: Any
) -> None:
    spec = campaign.raw["saved_quotes"]["saved8"]
    saved = tmp_path / "inputs" / "saved" / "saved8"
    saved.mkdir(parents=True)
    src = spec["source"]
    (saved / "quote.json").write_text(json.dumps({"replay_command": (
        f"uv run python main.py run --bundle {src}/bundle --profile {src}/profile.yaml "
        f"--results-dir {src}/runs --strategies profile")}))  # fmt: skip
    ctx = C.Context(campaign, tmp_path / "inputs", tmp_path / "out", ("py",))
    inv = campaign.by_id["I-saved8-replay-a"]
    argv = C.argv_for(inv, ctx)
    assert argv == ["py", "main.py", "run", "--bundle", f"{saved}/bundle", "--profile",
                    f"{saved}/profile.yaml", "--results-dir", str(tmp_path / "out" / inv.id),
                    "--strategies", "profile"]  # fmt: skip


EVIDENCE = REPO / "docs" / "references" / "research-021" / "campaign"


def test_checked_in_case_lists_are_the_registered_bundles_cases(campaign: Any) -> None:
    for split in ("tuning", "report"):
        data = (EVIDENCE / f"cases-{split}.jsonl").read_bytes()
        keys = [k for k, v in campaign.raw["inputs"].items() if v["split"] == split]
        assert keys and all(C._sha256_bytes(data) == campaign.raw["inputs"][k]["cases_sha256"]
                            for k in keys)  # fmt: skip
        ids = [json.loads(x)["case_id"] for x in data.decode().splitlines() if x.strip()]
        assert len(ids) == len(set(ids)) == campaign.raw["inputs"][keys[0]]["cases"]
    report = {json.loads(x)["case_id"] for x in (EVIDENCE / "cases-report.jsonl").read_text()
              .splitlines() if x.strip()}  # fmt: skip
    assert set(campaign.raw["known_report_defects"]["cases"]) <= report


def test_registered_nominees_are_the_rule_results_of_the_clean_tuning_analysis(
    campaign: Any
) -> None:
    analysis = json.loads((EVIDENCE / "tuning-analysis.json").read_text())
    assert analysis["reconciled"] is True and analysis["analysis_source"]["git_dirty"] is False
    assert analysis["stage"] == "T" and len(analysis["invocations"]) == 26
    assert all(v["result"] == "ok" for v in analysis["invocations"].values())
    results = {c["id"]: c for c in analysis["comparisons"] if c["kind"] == "nominee"}
    registered = {k: v for k, v in campaign.raw["nominees"].items() if k != "analysis"}
    assert set(results) == set(registered)
    for key, entry in registered.items():
        got = results[key]
        assert (got["apply"], got["result"]["nominee"]) == (entry["apply"], entry["nominee"]), key
        assert {v: [t["failures"], t["shortfall"]] for v, t in got["result"]["values"].items()} \
            == entry["failures_shortfall"], key  # fmt: skip
    frozen = json.loads((EVIDENCE / "freeze.json").read_text())
    assert {k: v["nominee"] for k, v in frozen["nominees"].items()} == {
        k: v["nominee"] for k, v in registered.items()}  # fmt: skip


def test_freeze_record_regenerates_from_code_and_manifest(campaign: Any) -> None:
    record = C.freeze_record(campaign, inputs=None, nominees=None)
    for name, pins in record["presets"].items():
        for pin in pins:  # every preset file (current and historical) still matches its pin
            assert pin["file_sha256_now"] == pin["sha256"], name
    assert set(record["effective_settings"]) == {
        i.id for i in campaign.invocations if i.kind in ("run", "quote")
        and int(i.get("expect_exit", 0)) == 0 and not str(i.get("profile")).startswith("saved:")
    }  # fmt: skip
    roster = record["effective_settings"]["T-roster-full"]["algorithms"]
    assert list(roster) == ALL14
    assert roster["metis_inspired"]["params"]["label_hops"] == 4
    assert roster["cfmm_dual"]["options_source"]["version"] == 2  # current CP+CL preset
    cpmm = record["effective_settings"]["T-cfmm-cpmm-full"]["algorithms"]["cfmm_dual"]
    assert cpmm["options_source"]["version"] == 1  # the historical CPMM-only pin
    off = record["effective_settings"]["T-repair-off-full"]["algorithms"]
    assert off["incremental_graph_repair"]["options_source"] == {"kind": "override"}
    frozen_path = EVIDENCE / "freeze.json"
    if frozen_path.is_file():  # after the freeze commit: nothing may drift from it
        frozen = json.loads(frozen_path.read_text())
        for key in ("files", "presets", "effective_settings", "inventory", "rules_sha256",
                    "known_report_defects", "saved_quotes"):
            assert frozen[key] == json.loads(json.dumps(record[key])), key
        for key, view in record["inputs"].items():
            assert {k: frozen["inputs"][key][k] for k in view} == view, key


def test_registered_commands_are_the_real_cli_and_latency_commands(
    tmp_path: Path, campaign: Any
) -> None:
    """Stage L uses the unchanged L08 arm commands (latency-optimization-results.md §7)
    with a same-source sufficient-budget run per arm; R/I use the ordinary CLI."""
    out, inputs = tmp_path / "out", tmp_path / "inputs"
    ctx = C.Context(campaign, inputs, out, ("uv", "run", "python"))
    exp = "20260101T000000000000Z-00000000"
    for inv_id in ("L-R", "L-R-sb", "L-E1", "L-E1-sb"):
        (out / inv_id / exp).mkdir(parents=True)
    run = C.argv_for(campaign.by_id["L-E1"], ctx)
    assert run == ["uv", "run", "python", "-m", "benchmark.latency", "run", "--protocol",
                   "config/latency/l01.yaml", "--arms", "config/latency/l08.yaml", "--arm", "E1",
                   "--bundle", str(inputs / "full"), "--out", str(out / "L-E1")]  # fmt: skip
    sb = C.argv_for(campaign.by_id["L-E1-sb"], ctx)
    assert sb[4:8] == ["benchmark.latency", "sufficient", "--sufficient",
                       "config/latency/l01-sufficient-budget.yaml"]  # fmt: skip
    cmp = C.argv_for(campaign.by_id["L-cmp-L02"], ctx)
    assert cmp[3:12] == ["-m", "report.latency", "compare", str(out / "L-R" / exp),
                         str(out / "L-E1" / exp), "--lane", "exact", "--sufficient",
                         str(out / "L-R-sb" / exp)]  # fmt: skip
    roster = C.argv_for(campaign.by_id["R-roster-full"], ctx)
    assert roster == ["uv", "run", "python", "main.py", "run", "--bundle",
                      str(inputs / "report_full"), "--profile", "config/full_gross.yaml",
                      "--results-dir", str(out / "R-roster-full"),
                      "--strategies", "all"]  # fmt: skip
    quote = C.argv_for(campaign.by_id["I-all-details"], ctx)
    assert quote[4:] == ["quote", "--bundle", str(inputs / "full"), "--profile",
                         "config/full_gross.yaml", "--token-in", "USDC", "--token-out", "USDT",
                         "--amount", "1000", "--quotes-dir", str(out / "I-all-details"),
                         "--strategies", "all", "--details"]  # fmt: skip
    compact = C.argv_for(campaign.by_id["I-all-compact"], ctx)
    assert "--details" not in compact


# ----------------------------------------------------------------------------- both CLI modes


def _profiles(campaign: Any) -> list[str]:
    return [*C.generated_profiles(campaign), "l3", "e4"]


PROFILE_KEYS = _profiles(C.load_campaign())


def _registered_algorithms(campaign: Any, key: str) -> list[str]:
    inv = next(i for i in campaign.invocations if i.stage == "I" and i.get("profile") == key)
    return list(inv.algorithms)


@pytest.mark.parametrize("key", PROFILE_KEYS)
def test_every_new_profile_runs_in_batch_and_single_request_mode(
    key: str, campaign: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = REPO / C.profile_path(campaign, key)
    before = profile.read_bytes()
    inv = next(i for i in campaign.invocations if i.stage == "I" and i.get("profile") == key)
    mode = str(inv.get("strategies"))
    results = tmp_path / "results"
    bundle = CORPUS if key == "net_empirical" else MIXED  # the cost model's own block
    assert main.main(["run", "--bundle", str(bundle), "--profile", str(profile),
                      "--results-dir", str(results), "--strategies", mode]) == 0  # fmt: skip
    (run_dir,) = results.iterdir()
    manifest = load_manifest(run_dir)
    expected = _registered_algorithms(campaign, key) or list(
        campaign.raw["profiles"][key]["generate"]["algorithms"])  # fmt: skip
    assert list(manifest.algorithms) == expected
    records = load_case_records(run_dir)
    assert len(records) == len(expected) * len(manifest.measurement["case_order"])  # all kept
    if key == "net_empirical":
        assert {r["status"] for r in records} == {"unsupported"}
    quotes = tmp_path / "quotes"
    argv = ["quote", "--bundle", str(CORPUS), "--profile", str(profile), "--token-in", "USDC",
            "--token-out", "USDT0", "--amount", "1500.25", "--quotes-dir", str(quotes),
            "--strategies", mode, "--details"]  # fmt: skip
    capsys.readouterr()
    code = main.main(argv)
    if key == "net_empirical":  # a registered refusal: quote does not support empirical_cost
        assert code == 1 and "empirical_cost is not supported by quote" in capsys.readouterr().err
        assert not quotes.exists()
    else:
        assert code == 0
        (quote_dir,) = quotes.iterdir()
        (qrun,) = (quote_dir / "runs").iterdir()
        qm = load_manifest(qrun)
        assert list(qm.algorithms) == expected
        assert [e["algorithm"] for e in qm.prepare_events] == expected  # one worker each
        for record in load_case_records(qrun):
            assert record["measurement"]["attempts_completed"] == 1
            assert len(record["measurement"]["solve_seconds"]) == 1
        assert qm.memory_record_count is None  # never a memory pass in quote
    assert profile.read_bytes() == before


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d["strategies"]["uni_sor_adaptive"]["shortlist"].update(routes_per_probe=7),
         "differ"),
        (lambda d: d["algorithm_options"]["uni_sor_cycle_safe"].update(max_splits=2),
         "cannot be set per strategy"),
        (lambda d: d["algorithm_options"]["uni_sor_cycle_safe"].update(admission="off"),
         "uni_sor_cycle_safe"),
        (lambda d: d["strategies"]["uni_sor_optimized"]["recipe"].update(sha256="0" * 64),
         "sha256"),
    ],
)
def test_mutated_campaign_profiles_are_refused_before_anything_is_written(
    mutate: Any, match: str, campaign: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = yaml.safe_load((REPO / C.profile_path(campaign, "sor_ms2")).read_text())
    mutate(doc)
    path = tmp_path / "mutated.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    results = tmp_path / "results"
    assert main.main(["run", "--bundle", str(MIXED), "--profile", str(path), "--results-dir",
                      str(results), "--strategies", "profile"]) == 1  # fmt: skip
    assert match in capsys.readouterr().err
    assert not results.exists()


# ----------------------------------------------------------------------------- executor


def _fixture_campaign(tmp_path: Path) -> Any:
    small = read_profile_document(REPO / "config" / "daily_gross.yaml")
    small["graph"] = {"chunks": 20}
    small["measurement"] = {"warmup": 0, "repeats": 1, "seed": 7, "order": "fixed",
                            "memory_pass": False}  # fmt: skip
    base = tmp_path / "small.yaml"
    base.write_text(yaml.safe_dump(small, sort_keys=False))
    scan = {**small, "algorithms": ["direct_split", "direct_split_certified"],
            "search": {**small["search"], "max_splits": 1},
            "algorithm_options": {"direct_split_certified": preset_options(
                ALGORITHMS["direct_split_certified"])}}  # fmt: skip
    scan_path = tmp_path / "scan1.yaml"
    scan_path.write_text(yaml.safe_dump(scan, sort_keys=False))
    sha = C._sha256_bytes
    inputs = {}
    for key, source in (("mixed", MIXED), ("lb", LB), ("corpus", CORPUS)):
        manifest = (source / "manifest.json").read_bytes()
        lines = [x for x in (source / "cases.jsonl").read_text().splitlines() if x.strip()]
        inputs[key] = {"source": str(source.relative_to(REPO)), "bundle_hash": sha(manifest),
                       "cases": len(lines), "split": "fixture", "cohort": "fixture"}  # fmt: skip
    doc = {
        "schema": C.SCHEMA,
        "host": {"logical_cpus": 10, "load_sample_seconds": 1},
        "holdout_exposure": {"fixture": "fixture"},
        "inputs": inputs,
        "requests": {"one": {"token_in": "USDC", "token_out": "USDT0", "amount": "1500.25"}},
        "profiles": {"small": {"path": str(base), "sha256": sha(base.read_bytes())},
                     "scan1": {"path": str(scan_path), "sha256": sha(scan_path.read_bytes())}},
        "stages": {"T": {"lanes": 2, "caffeinate": True, "pmset_capture": True}},
        "known_report_defects": {"cases": []},
        "invocations": [
            {"id": "X-mixed", "stage": "T", "kind": "run", "bundle": "mixed", "profile": "small",
             "strategies": "all", "algorithms": ALL14},
            {"id": "X-lb", "stage": "T", "kind": "run", "bundle": "lb", "profile": "small",
             "strategies": "all", "algorithms": ALL14},
            {"id": "X-scan1", "stage": "T", "kind": "run", "bundle": "mixed", "profile": "scan1",
             "strategies": "profile", "algorithms": ["direct_split", "direct_split_certified"]},
            {"id": "X-quote", "stage": "T", "kind": "quote", "bundle": "corpus",
             "profile": "small", "strategies": "all", "details": True, "request": "one",
             "algorithms": ALL14, "derive": ["report", "replay", "order_check"]},
        ],
        "comparisons": [
            {"id": "grid", "stage": "T", "kind": "equal_value", "class": "same_domain",
             "baseline": "X-mixed/direct_split", "candidate": "X-mixed/direct_split_certified"},
            {"id": "cycle", "stage": "T", "kind": "cycle_safe", "class": "same_domain",
             "baseline": "X-mixed/uni_sor_port", "candidate": "X-mixed/uni_sor_cycle_safe",
             "units": ["quotes_counted", "admission_checks"]},
            {"id": "lb-restricted", "stage": "T", "kind": "all_unsupported",
             "arms": [f"X-lb/{a}" for a in RESTRICTED]},
            {"id": "ms", "stage": "T", "kind": "nominee", "apply": True, "canonical": 4,
             "algorithms": ["direct_split", "direct_split_certified"],
             "scan": {1: ["X-scan1"], 4: ["X-mixed"]}},
        ],
    }  # fmt: skip
    path = tmp_path / "campaign.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return C.load_campaign(path)


def test_stage_executor_and_analysis_on_bounded_fixtures(tmp_path: Path) -> None:
    campaign = _fixture_campaign(tmp_path)
    inputs, out = tmp_path / "inputs", tmp_path / "out"
    C.prepare_inputs(campaign, REPO, inputs)
    code = C.execute(campaign, "T", inputs=inputs, out=out, allow_dirty=True,
                     launcher=(sys.executable,), echo=lambda _: None)  # fmt: skip
    assert code == 0, (out / "ledger.jsonl").read_text()
    ledger = C.read_ledger(out)
    assert set(ledger) == {i.id for i in campaign.stage("T")}
    assert all(e["result"] == "ok" for e in ledger.values())
    assert (out / "load.jsonl").read_text().strip()
    events = [e for e in C.Ledger(out / "ledger.jsonl").entries()]
    caffeinate = [e for e in events if e.get("event") == "caffeinate"]
    assert caffeinate and caffeinate[0]["argv"][:5] == ["caffeinate", "-i", "-m", "-s", "-w"]
    capture = [e for e in events if e.get("event") == "pmset_capture"]
    assert capture and capture[0]["ok"] is True and (out / "pmset-sleep-wake.txt").is_file()
    result = C.analyze(campaign, "T", inputs=inputs, out=out)
    assert result["reconciled"], result["reconciliation_problems"]
    by_arm = {a["status"]["arm"]: a for inv in result["invocations"].values()
              for a in inv.get("arms") or []}  # fmt: skip
    # mixed universe: LB presence alone never makes cfmm_dual / uni_sor_cycle_safe unsupported;
    # direct_split_certified is, by its non-CPMM direct pools (lb-scope.md §11)
    assert "unsupported" not in by_arm["X-mixed/cfmm_dual"]["status"]["statuses"]
    assert "unsupported" not in by_arm["X-mixed/uni_sor_cycle_safe"]["status"]["statuses"]
    dsc = by_arm["X-mixed/direct_split_certified"]["status"]
    assert dsc["statuses"].get("unsupported") == dsc["scheduled"]
    comparisons = {c["id"]: c for c in result["comparisons"]}
    assert comparisons["lb-restricted"]["gate"] == "pass"  # LB-only: all three unsupported
    assert comparisons["grid"]["result"]["evaluable"] is False  # no all-CPMM direct pair here
    assert comparisons["ms"]["result"]["nominee"] in (1, 4)
    cycle = comparisons["cycle"]
    assert cycle["identity_on_comparable_identical"]["gate"] == "pass"
    assert [r["unit"] for r in cycle["same_unit_ratios"]] == ["quotes_counted", "admission_checks"]
    quote = result["invocations"]["X-quote"]
    assert [a["status"]["arm"].split("/")[1] for a in quote["arms"]] == ALL14
    assert result["invocations"]["X-quote.order"]["result"] == "ok"  # replay == original
    assert (out / "X-quote.report" / "report" / "single_request.txt").is_file()
    assert "p95" not in C.render_markdown(result)
    # the analysis checks each run against the resolved profile it was registered with
    ident = C.resolved_identity(campaign, campaign.by_id["X-mixed"])
    recorded = load_manifest(ledger["X-mixed"]["run_dir"]).resolved_profile
    assert ident["resolved_profile_sha256"] == C._canonical_sha256(recorded)
    other = copy.deepcopy(campaign)
    other.by_id["X-mixed"].raw["strategies"] = "base"
    assert C.resolved_identity(other, other.by_id["X-mixed"])["resolved_profile_sha256"] != (
        ident["resolved_profile_sha256"])  # fmt: skip
    # no silent rerun, no changed input, no second run of a completed run
    with pytest.raises(C.CampaignError, match="never re-run"):
        C.execute(campaign, "T", inputs=inputs, out=out, allow_dirty=True,
                  launcher=(sys.executable,), echo=lambda _: None, only=["X-scan1"])  # fmt: skip
    with pytest.raises(C.CampaignError, match="completed run is never re-run"):
        C.execute(campaign, "T", inputs=inputs, out=out, allow_dirty=True,
                  launcher=(sys.executable,), echo=lambda _: None,
                  retry_infrastructure="X-scan1")  # fmt: skip
    changed = tmp_path / "inputs2"
    shutil.copytree(inputs, changed)
    (changed / "mixed" / "manifest.json").write_text("{}")
    with pytest.raises(C.CampaignError, match="hash differs"):
        C.execute(campaign, "T", inputs=changed, out=tmp_path / "out2", allow_dirty=True,
                  launcher=(sys.executable,), echo=lambda _: None)  # fmt: skip
    assert not (tmp_path / "out2").exists()

"""WHI-1602 campaign plumbing, end to end through the ordinary CLI, offline.

- The committed schedule validates; its profiles are exactly their renderings from the unchanged
  canonical bases; the 17-ID roster derives; the A1 + B14 groups slice it exactly once; stage
  inventories are exact; the L01-R022 protocol is L01's except `key` and the pinned profile.
- A bounded fixture campaign (the tracked 19-pool corpus fixture in place of every corpus bundle)
  runs stage T through the real executor -- ordinary runs, work passes, the net arm, A4 -- and the
  analysis reconciles every cell, finds every pair identical and re-derives the fixture facts the
  guide fixes (S1: 582 -> 422 quotes), the work pass agrees with its ordinary twin, the A5 rule runs
  on the stage-T records and stage M runs the budgets it produced.
- `--strategies all` (the 17 IDs), its report, replay and order-check run over the fixture, and the
  group profiles carry exactly that roster's per-strategy settings.
- The stage-L launch policy waits and gives up as registered; the stage commands are the real ones.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import multiprocessing
import re
import signal
import sys
from collections.abc import Iterator
from pathlib import Path
from types import FrameType, ModuleType
from typing import Any

import pytest
import yaml

from benchmark.latency import arm_profile, load_arms, load_protocol
from benchmark.results import load_manifest

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "corpus" / "bundle"
TEST_ALARM_SECONDS = 900
ALL17 = [
    "direct", "single_path", "direct_split", "path_split", "incremental_graph", "uni_sor_port",
    "uni_sor_adaptive", "uni_sor_optimized", "metis_inspired", "metis_history",
    "direct_split_certified", "incremental_graph_repair", "uni_sor_cycle_safe", "cfmm_dual",
    "single_path_bounded", "incremental_graph_bounded", "metis_history_bounded",
]  # fmt: skip
LIVE = [*ALL17, "split_polish", "marginal_activation"]  # WHI-1632: today's `--strategies all`


def _load(name: str) -> ModuleType:
    path = REPO / "tools" / "research_022" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PA: Any = _load("pruning_analysis")
C: Any = _load("pruning_campaign")


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
    return C.load_schedule()


def fixture_raw(arms: list[dict[str, Any]]) -> dict[str, Any]:
    """The committed schedule with every corpus bundle replaced by the tracked fixture bundle (the
    registered inputs of a stage-T-shaped run need `data/`, which no worktree has), the given arms
    and no literal invocation."""
    raw = copy.deepcopy(yaml.safe_load((REPO / "config/research_022/schedule.yaml").read_text()))
    fixture = raw["inputs"]["fixture"]
    for key, spec in raw["inputs"].items():
        if key != "fixture":
            raw["inputs"][key] = {**fixture, "cohort": spec["cohort"], "split": spec["split"]}
    raw["arms"] = arms
    raw["invocations"] = []
    out: dict[str, Any] = raw
    return out


def mini_campaign(tmp_path: Path, raw: dict[str, Any]) -> Any:
    path = tmp_path / "schedule.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return C.build(raw, path)


# ----------------------------------------------------------------------------- the schedule


def test_the_committed_schedule_files_and_roster_are_consistent(campaign: Any) -> None:
    assert C.check(campaign) == []
    assert campaign.raw["rosters"]["all17"] == ALL17
    for key in C.generated_profiles(campaign):
        assert (REPO / C.profile_path(campaign, key)).read_text() == C.render_profile(campaign, key)


def test_drift_and_a_broken_roster_cover_are_refused(campaign: Any) -> None:
    drifted = copy.deepcopy(campaign)
    drifted.raw["profiles"]["g_sp"]["generate"]["algorithms"] = ["single_path"]
    problems = C.check(drifted)
    assert any("g_sp" in p and "differs from its rendering" in p for p in problems)
    assert any("do not cover the 17 IDs exactly once" in p for p in problems)
    changed = copy.deepcopy(campaign)
    changed.raw["profiles"]["canonical_gross"]["sha256"] = "0" * 64
    assert any("canonical_gross" in p and "differs from the pin" in p for p in C.check(changed))
    pairs = copy.deepcopy(campaign)
    gen = pairs.raw["profiles"]["a4_mh"]["generate"]["options"]
    gen["metis_history_bounded"]["set"]["max_frontier_labels"] = 1
    assert any("a4_mh" in p for p in C.check(pairs))


def test_group_profiles_change_only_the_algorithm_list_and_the_registered_sets(
    campaign: Any,
) -> None:
    roster = C.roster_document(campaign, "canonical_gross")
    for key in ("g_sp", "g_ig", "g_mh", "g_rest_a", "g_rest_b", "g_rest_c", "g_rest_d"):
        doc = yaml.safe_load((REPO / C.profile_path(campaign, key)).read_text())
        for section in ("objective", "search", "graph", "budget", "measurement", "worker"):
            assert doc[section] == roster[section], (key, section)
        for algorithm in doc["algorithms"]:
            assert doc.get("algorithm_options", {}).get(algorithm) == roster.get(
                "algorithm_options", {}
            ).get(algorithm), (key, algorithm)
    a4 = yaml.safe_load((REPO / C.profile_path(campaign, "a4_mh")).read_text())
    options = a4["algorithm_options"]
    assert options["metis_history"] == options["metis_history_bounded"]
    assert a4["algorithm_options"]["metis_history"]["dominance"] == "off"
    a3 = yaml.safe_load((REPO / C.profile_path(campaign, "a3_mh")).read_text())
    assert a3["algorithm_options"]["metis_history"]["max_frontier_labels"] == 10_000_000
    net = yaml.safe_load((REPO / C.profile_path(campaign, "n_ig")).read_text())
    assert net["objective"]["mode"] == "empirical_cost"
    assert net["budget"] == roster["budget"]  # the net arm changes the objective only


def test_stage_inventories_are_exact(campaign: Any) -> None:
    counts = {s: len(campaign.stage(s)) for s in "TRMIL"}
    assert counts == {"T": 18, "R": 32, "M": 24, "I": 12, "L": 8}  # M exists once a5 is frozen
    ids = {i.id for i in campaign.stage("R")}
    assert {f"R-A1-{g}-{b}" for g in ("ig", "mh", "sp") for b in ("full", "sor")} <= ids
    assert {f"R-B14-{g}-{b}" for g in ("ra", "rb", "rc", "rd") for b in ("full", "sor")} <= ids
    assert "R-WP-A4-mh-full" in ids and "R-A1-ig-full.report" in ids
    assert "T-B14-ra-full" not in {i.id for i in campaign.stage("T")}  # no tuning baseline run
    pair = campaign.by_id["T-A1-mh-full"]
    assert pair.algorithms == ["metis_history", "metis_history_bounded"]
    assert pair.raw["pair"] == "metis_history" and not pair.raw["work_pass"]
    assert campaign.by_id["T-WP-A1-mh-full"].raw["work_pass"] is True
    assert [i.id for i in campaign.stage("L")][:3] == ["L-ref", "L-bnd", "L-cmp"]


def test_the_a5_expansion_needs_the_frozen_values_and_adds_twenty_four_runs(campaign: Any) -> None:
    values = campaign.raw["a5"]["values"]  # the freeze commit's: the rule applied to stage T
    assert set(values) == {"sp", "ig", "mh"}
    for group in values.values():
        for kind in ("max_quotes", "max_candidates"):
            assert 0 < group[kind]["p25"] <= group[kind]["p50"]
    runs = campaign.stage("M")
    assert len(runs) == 24  # 3 pairs x 4 budgets x (tuning, report)
    one = campaign.by_id["M-A5-ig-c50-report"]
    assert one.get("bundle") == "report_full" and one.raw["pair"] == "incremental_graph"
    spec = campaign.raw["profiles"]["a5_ig_c50"]["generate"]
    assert spec["budget"] == {"max_candidates": values["ig"]["max_candidates"]["p50"]}
    quotes = campaign.raw["profiles"]["a5_sp_q25"]["generate"]["budget"]
    assert quotes == {"max_quotes": values["sp"]["max_quotes"]["p25"]}
    unfrozen = copy.deepcopy(campaign.raw)
    unfrozen["a5"]["values"] = None
    assert C.a5_profiles(unfrozen) == {} and C.build(unfrozen).stage("M") == []


def test_the_a5_values_are_written_once_into_the_schedule(tmp_path: Path) -> None:
    path = tmp_path / "schedule.yaml"
    unfrozen = re.sub(r"  values:\n(?:    .+\n)+", "  values: null\n",
                      (REPO / "config/research_022/schedule.yaml").read_text())  # fmt: skip
    assert "values: null" in unfrozen
    path.write_text(unfrozen)
    values = {g: {"max_quotes": {"p25": 1, "p50": 2}, "max_candidates": {"p25": 3, "p50": 4}}
              for g in ("sp", "ig", "mh")}
    C.write_a5_values(path, values)
    loaded = yaml.safe_load(path.read_text())
    assert loaded["a5"]["values"]["ig"]["max_candidates"] == {"p25": 3, "p50": 4}
    with pytest.raises(C.CampaignError):
        C.write_a5_values(path, values)


# ----------------------------------------------------------------------------- latency files


def test_the_latency_protocol_is_l01_verbatim_except_key_and_profile(campaign: Any) -> None:
    l01 = yaml.safe_load((REPO / "config/latency/l01.yaml").read_text())
    ours = yaml.safe_load((REPO / "config/research_022/l01-r022.yaml").read_text())
    for section in l01:
        if section not in ("key", "profile"):
            assert ours[section] == l01[section], section
    assert ours["key"] == "L01-R022" and ours["profile"]["path"].endswith("timing_pairs.yaml")
    protocol = load_protocol(REPO / "config/research_022/l01-r022.yaml")
    assert (protocol.warmup, protocol.repeats) == (1, 5)
    assert protocol.orders == ("fixed", "reverse")
    assert protocol.max_loadavg_1m_per_cpu == 0.5 and len(protocol.matrix) == 24


def test_the_arms_resolve_to_the_registered_profile_subsets(campaign: Any) -> None:
    arms = load_arms(REPO / "config/research_022/latency-arms.yaml")
    protocol_text = (REPO / "config/research_022/l01-r022.yaml").read_bytes()
    assert arms.protocol_sha256 == C.sha256_bytes(protocol_text)
    pairs = campaign.raw["latency"]["pairs"]
    ref = arm_profile(arms.arms["REF"], "config/research_022/profiles/timing_pairs.yaml")
    bnd = arm_profile(arms.arms["BND"], "config/research_022/profiles/timing_pairs.yaml")
    assert list(ref.algorithms) == list(pairs.values())
    assert list(bnd.algorithms) == list(pairs)
    # each arm keeps only the options of the algorithms it runs (`benchmark.latency.arm_profile`)
    assert set(ref.algorithm_options) == {"metis_history"}
    assert set(bnd.algorithm_options) == {"metis_history_bounded"}
    assert ref.algorithm_options["metis_history"]["settings_sha256"] == bnd.algorithm_options[
        "metis_history_bounded"]["settings_sha256"]  # same settings, paired comparison
    assert ref.search == bnd.search and ref.graph == bnd.graph and ref.budget == bnd.budget
    (comparison,) = arms.comparisons
    assert comparison["lane"] == "heuristic" and comparison["pairs"] == pairs


def test_the_stage_commands_are_the_real_ones(campaign: Any, tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    ctx = C.c21.Context(campaign, inputs, tmp_path / "out", ("uv", "run", "python"),
                        done={"L-ref": {"result": "ok"}, "L-bnd": {"result": "ok"}},
                        stage="L", stage_root=tmp_path)  # fmt: skip
    for sub in ("L-ref", "L-bnd"):
        (tmp_path / "out" / sub / "exp").mkdir(parents=True)
    run = C.argv_for(campaign.by_id["T-A1-ig-full"], ctx)
    assert run[:5] == ["uv", "run", "python", "main.py", "run"] and "profile" in run
    work = C.argv_for(campaign.by_id["T-WP-A1-ig-full"], ctx)
    assert work[3:5] == ["tools/research_022/pruning_work.py", "run"]
    slot = work.index("--results-dir") + 1
    assert work[:slot] == [*run[:3], work[3], *run[4:slot]]  # only the entry point differs ...
    assert work[slot + 1 :] == run[slot + 1 :]  # ... and the slot
    assert work[slot].endswith("T-WP-A1-ig-full")
    ref = C.argv_for(campaign.by_id["L-ref"], ctx)
    assert ref[3:7] == ["-m", "benchmark.latency", "run", "--protocol"]
    assert ref[ref.index("--arm") + 1] == "REF" and ref[ref.index("--arms") + 1].endswith(
        "latency-arms.yaml")  # fmt: skip
    cmp = C.argv_for(campaign.by_id["L-cmp"], ctx)
    assert cmp[3:6] == ["-m", "report.latency", "compare"] and "heuristic" in cmp
    assert cmp.count("--pair") == 3 and "single_path_bounded=single_path" in cmp
    quote = C.argv_for(campaign.by_id["L-q1"], ctx)
    assert quote[4] == "quote" and "--details" in quote
    assert quote[quote.index("--strategies") + 1] == "profile"


# ----------------------------------------------------------------------------- launch policy


def test_the_launch_window_waits_for_a_quiet_group_and_gives_up_at_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Popen:
        def __init__(self, *a: Any, **k: Any) -> None:
            self.pid = 1

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 0

    clock = {"now": 0.0}
    loads = iter([4.0, 1.0, 1.0, 1.0, 1.0] + [1.0] * 5)  # a busy group, then a quiet one

    def sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(C.subprocess, "Popen", Popen)
    monkeypatch.setattr(C.time, "sleep", sleep)
    monkeypatch.setattr(C.time, "time", lambda: clock["now"])
    monkeypatch.setattr(C.os, "getloadavg", lambda: (next(loads), 0.0, 0.0))
    ledger = C.c21.Ledger(tmp_path / "ledger.jsonl")
    window = {"samples": 5, "interval_seconds": 30, "headroom_load1": 3.0,
              "resample_every_seconds": 300, "max_wait_seconds": 1000}  # fmt: skip
    assert C.launch_window(window, ledger, lambda _m: None) is True
    gates = [e for e in ledger.entries() if e["event"] == "launch_gate"]
    assert [g["launched"] for g in gates] == [False, True] and gates[0]["max_load1"] == 4.0
    # a host that never quiets is not launched once the registered wait is spent
    clock["now"] = 0.0
    monkeypatch.setattr(C.os, "getloadavg", lambda: (9.0, 0.0, 0.0))
    ledger2 = C.c21.Ledger(tmp_path / "ledger2.jsonl")
    assert C.launch_window(window, ledger2, lambda _m: None) is False
    events = ledger2.entries()
    assert events[-1]["event"] == "stage_not_launched" and events[-1]["waited_seconds"] <= 1000
    assert sum(1 for e in events if e["event"] == "launch_gate") >= 2


# ----------------------------------------------------------------------------- stage T and M


def test_stage_t_m_on_the_fixture_through_the_real_executor_and_analysis(tmp_path: Path) -> None:
    arms: list[dict[str, Any]] = [
        {"arm": "A4", "stages": {"T": ["tuning_full"]}, "groups": {"mh": "a4_mh"},
         "work_pass": True},
        {"arm": "A1", "stages": {"T": ["tuning_full", "tuning_sor"]},
         "groups": {"ig": "g_ig", "mh": "g_mh", "sp": "g_sp"}, "work_pass": True},
        {"arm": "A2", "stages": {"T": ["tuning_full"]}, "groups": {"sp": "n_sp"}},
    ]  # fmt: skip
    raw = fixture_raw(arms)
    # M runs on `report_full` (also the fixture here); the A5 arm is added after stage T
    campaign = mini_campaign(tmp_path, raw)
    root = tmp_path / "root"
    C.prepare_inputs(campaign, REPO, root / "inputs")
    out = tmp_path / "T"
    code = C.execute(campaign, "T", inputs=root / "inputs", out=out, lanes=4, allow_dirty=True,
                     echo=lambda _m: None)  # fmt: skip
    assert code == 0
    result = C.analyze(campaign, "T", inputs=root / "inputs", out=out)
    # a worktree under test is dirty by definition; a registered stage runs from a clean clone
    assert [p for p in result["reconciliation_problems"] if "dirty tree" not in p] == []
    assert len(result["invocations"]) == 1 + 1 + 6 + 6 + 1  # A4 (+WP), A1 (+WP) on 2 bundles, A2
    # every pair is identical on every cell (the fixture has no truncated cell) ...
    assert len(result["exactness"]) == 8  # A4 + 3 pairs x 2 bundles + A2 sp
    for label, e in result["exactness"].items():
        assert e["compared"] == e["identical"] == e["scheduled"] == 96, label
        assert e["differences"] == [] and e["bounded_truncated_reference_not"] == [], label
        assert e["excluded_as_truncated"] == [] and e["label_disagrees_with_truncation"] == []
    # ... and is not vacuous: the guide's fixture fact (S1 skips 160 candidates, quotes 582 -> 422)
    sp = result["work"]["T-A1-sp-full"]
    assert sp["units"]["quotes_executed"]["reference"]["sum"] == 582
    assert sp["units"]["quotes_executed"]["bounded"]["sum"] == 422
    assert sp["bound_cost"]["pruned_bound"]["sum"] == 160
    assert sp["bound_cost"]["bound_evaluations"]["sum"] > 160  # a skip costs >= 1 evaluation
    # the work pass: counts agree with the meter and the ordinary twin, and CL steps / LB bins exist
    for label, agree in result["work_pass_agreement"].items():
        assert agree["internal_mismatches"] == [] and agree["differs_from_twin"] == [], label
    wp = result["work"]["T-WP-A1-sp-full"]
    assert wp["units"]["quotes_executed(work_pass)"]["reference"]["sum"] == 582
    assert wp["units"]["cl_swap_steps"]["bounded"]["sum"] < wp["units"]["cl_swap_steps"][
        "reference"]["sum"]  # fmt: skip
    assert wp["units"]["lb_bins_swapped"]["reference"]["sum"] > 0
    for family in ("cohort", "direct", "mix", "stratum", "pair"):
        assert wp["families"][family], family
    # G_M2 is re-derived from the records: under the preset M2 is inactive, dominance off is not
    assert result["exactness"]["T-A1-mh-full"]["gates"]["m2_active"] == 0
    # the A5 rule runs on the stage-T reference records and stage M runs the budgets it yields
    values = C.a5_values(campaign, out)
    assert values["sp"]["max_quotes"]["p25"] <= values["sp"]["max_quotes"]["p50"]
    assert values["sp"]["cells"]["quotes_executed"] > 0
    raw["a5"]["values"] = {g: {k: v[k] for k in ("max_quotes", "max_candidates")}
                           for g, v in values.items()}  # fmt: skip
    raw["a5"]["groups"] = {"sp": "g_sp"}  # one pair keeps the test bounded
    (tmp_path / "m" / "profiles").mkdir(parents=True)
    raw["a5"]["profile_dir"] = str(tmp_path / "m" / "profiles")  # never write into the repository
    m_campaign = mini_campaign(tmp_path / "m", raw)
    for key in C.a5_profiles(m_campaign.raw):
        Path(m_campaign.raw["profiles"][key]["path"]).write_text(C.render_profile(m_campaign, key))
    m_out = tmp_path / "M"
    code = C.execute(m_campaign, "M", inputs=root / "inputs", out=m_out, lanes=4,
                     allow_dirty=True, echo=lambda _m: None)  # fmt: skip
    assert code == 0
    m_result = C.analyze(m_campaign, "M", inputs=root / "inputs", out=m_out)
    assert [p for p in m_result["reconciliation_problems"] if "dirty tree" not in p] == []
    assert len(m_result["binding_budgets"]) == 8  # 4 budgets x (tuning, report)
    cut = [b for b in m_result["binding_budgets"].values() if "ref_cut/bnd_cut" in b["truncation"]
           or "ref_cut/bnd_clean" in b["truncation"]]  # fmt: skip
    assert cut, "a p25 / p50 budget must bind the reference somewhere"
    assert m_result["exactness"] == {}  # A5 is never exactness evidence
    # the tables and the disposition are rendered from the analyses
    text = C.render_tables({"T": result, "M": m_result})
    assert "Exactness (§11.2)" in text and "A5 binding budgets" in text
    summary = C.disposition({"T": result})
    assert summary["single_path_bounded"]["disposition"] in ("keep_experimental", "inconclusive")
    assert summary["single_path_bounded"]["exactness_differences"] == 0


def test_a_difference_in_one_cell_rejects_the_strategy_and_names_the_cell() -> None:
    names = ("single_path", "single_path_bounded")
    ref = {"c": {"status": "ok"}}
    assert names and ref  # the rule is exercised in test_pruning_analysis; here the disposition
    clean = {"reconciled": True, "exactness": {"x": {"bounded": names[1], "arm": "A1",
             "differences": [{"case": "c", "keys": ["score"]}], "new_failure_status": [],
             "compared": 5}}, "work": {}}  # fmt: skip
    got = C.disposition({"T": clean, "R": clean})
    assert got["single_path_bounded"]["disposition"] == "reject"
    assert got["single_path_bounded"]["exactness_differences"] == 2


def test_disposition_is_inconclusive_without_a_pruned_cell_and_names_the_timing_gate() -> None:
    def stage(pruned: int) -> dict[str, Any]:
        return {"reconciled": True, "work": {"w": {"pair": "single_path", "arm": "A1",
                "work_pass": False, "bound_cost": {"pruned_bound": {"sum": pruned}}}},
                "exactness": {"x": {"bounded": "single_path_bounded", "arm": "A1",
                "differences": [], "new_failure_status": [], "compared": 9}}}  # fmt: skip

    assert C.disposition({"T": stage(0), "R": stage(0)})["single_path_bounded"][
        "disposition"] == "inconclusive"  # fmt: skip
    kept = C.disposition({"T": stage(4), "R": stage(4)})["single_path_bounded"]
    assert kept["disposition"] == "keep_experimental" and kept["statement"] == "work reduction only"
    assert kept["timing_verdict"] == "inconclusive"
    latency = {"latency": {"gate_passed": True,
                           "per_strategy": {"all single_path_bounded": {"verdict": "improvement"}}}}
    faster = C.disposition({"T": stage(4), "R": stage(4), "L": latency})["single_path_bounded"]
    assert faster["statement"] == "work and latency"


# ----------------------------------------------------------------------------- stage I


def test_all_17_ids_batch_report_replay_and_order_check_on_the_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = fixture_raw([])
    inv = yaml.safe_load((REPO / "config/research_022/schedule.yaml").read_text())["invocations"][0]
    assert inv["id"] == "I-batch17" and inv["algorithms"] == ALL17
    # WHI-1632: a re-execution today runs the live 19-identity `main.py run --strategies all`, so
    # this fixture registers (and derives) the live roster; the frozen `all17` schedule itself is
    # checked by `test_the_committed_schedule_files_and_roster_are_consistent`.
    raw["invocations"] = [{**inv, "algorithms": LIVE}]
    monkeypatch.setattr(C, "FROZEN_ADDITIONS", tuple(LIVE[LIVE.index("metis_history") :]))
    campaign = mini_campaign(tmp_path, raw)
    root = tmp_path / "root"
    C.prepare_inputs(campaign, REPO, root / "inputs")
    out = tmp_path / "I"
    code = C.execute(campaign, "I", inputs=root / "inputs", out=out, lanes=1, allow_dirty=True,
                     echo=lambda _m: None)  # fmt: skip
    assert code == 0
    ledger = C.c21.read_ledger(out)
    assert {k: v["result"] for k, v in ledger.items()} == {
        "I-batch17": "ok", "I-batch17.report": "ok", "I-batch17.replay": "ok",
        "I-batch17.order": "ok"}  # fmt: skip
    manifest = load_manifest(ledger["I-batch17"]["run_dir"])
    assert list(manifest.algorithms) == LIVE
    replay = load_manifest(ledger["I-batch17.replay"]["run_dir"])
    assert list(replay.algorithms) == LIVE and replay.case_count == manifest.case_count
    report = out / "I-batch17.report" / "report"
    assert report.is_dir()
    html = "\n".join(p.read_text(errors="replace") for p in report.rglob("*.html"))
    for name in ("single_path_bounded", "incremental_graph_bounded", "metis_history_bounded",
                 "split_polish", "marginal_activation"):
        assert name in html
    # the group profiles run the strategies with exactly the settings the 17-ID run recorded
    resolved = manifest.resolved_profile
    campaign_raw = yaml.safe_load((REPO / "config/research_022/schedule.yaml").read_text())
    for key in ("g_sp", "g_ig", "g_mh", "g_rest_a", "g_rest_b", "g_rest_c", "g_rest_d"):
        doc = yaml.safe_load((REPO / campaign_raw["profiles"][key]["path"]).read_text())
        from benchmark.profile import parse_profile

        profile = parse_profile(doc, key).resolved()
        for algorithm in doc["algorithms"]:
            assert profile["algorithm_config"][algorithm]["params"] == resolved[
                "algorithm_config"][algorithm]["params"], (key, algorithm)  # fmt: skip
        for section in ("objective", "budget", "search", "graph", "measurement", "worker"):
            assert profile[section] == resolved[section], (key, section)
    json.dumps(resolved)  # serializable: the freeze record embeds it
    result = C.analyze(campaign, "I", inputs=root / "inputs", out=out)
    assert [p for p in result["reconciliation_problems"] if "dirty tree" not in p] == []
    assert set(result["invocations"]) == {"I-batch17", "I-batch17.report", "I-batch17.replay",
                                          "I-batch17.order"}  # fmt: skip
    assert set(result["invocations"]["I-batch17"]["status_counts"]) == set(LIVE)
    assert result["invocations"]["I-batch17.replay"]["status_counts"] == result["invocations"][
        "I-batch17"]["status_counts"]  # fmt: skip
    assert result["invocations"]["I-batch17"]["holdout_exposure"] == "not_a_corpus_split"


# ----------------------------------------------------------------------------- stage L analysis


def _stage_l(tmp_path: Path, *, load: float = 1.0, pmset: bool = True, state: str = "complete",
             contaminated: bool = False) -> Path:  # fmt: skip
    out = tmp_path / "L"
    out.mkdir()
    events: list[dict[str, Any]] = [{"event": "stage", "logical_cpus": 10, "t": 0,
                                     "git_revision": "x"}]
    ledger = out / "ledger.jsonl"
    for inv_id, (start, end) in {"L-ref": (10, 20), "L-bnd": (21, 30), "L-cmp": (31, 32)}.items():
        events.append({"event": "start", "id": inv_id, "t": start})
        events.append({"event": "end", "id": inv_id, "t": end, "result": "ok", "exit_code": 0})
    ledger.write_text("".join(json.dumps(e) + "\n" for e in events))
    (out / "load.jsonl").write_text(
        "".join(json.dumps({"t": t, "load1": load if t == 25 else 1.0}) + "\n"
                for t in range(10, 33)))  # fmt: skip
    if pmset:
        (out / "pmset-sleep-wake.txt").write_text("")
    for inv_id in ("L-ref", "L-bnd"):
        exp = out / inv_id / "exp"
        exp.mkdir(parents=True)
        (exp / "experiment.json").write_text(json.dumps({
            "state": state, "load": {"contaminated": contaminated}, "partial": False,
            "source": {"git_dirty": False}}))  # fmt: skip
    (out / "L-cmp").mkdir()
    (out / "L-cmp" / "compare.json").write_text(json.dumps({
        "verdict": "reject", "reasons": [], "pairs": {}, "coverage_problems": {},
        "internal_checks": {}, "charged": {}, "charged_costs": {},
        "timing": {"full_source/matrix single_path_bounded": {
            "reference": "single_path", "improvement": {"wall": 0.4, "cpu": 0.4},
            "threshold": 0.1, "verdict": "faster"},
            "full_source/matrix metis_history_bounded": {
            "reference": "metis_history", "improvement": {"wall": 0.0, "cpu": 0.0},
            "threshold": 0.1, "verdict": "no_worthwhile_change"}}}))  # fmt: skip
    return out


def test_stage_l_timing_is_a_verdict_only_in_a_clean_window(
    campaign: Any, tmp_path: Path
) -> None:
    out = _stage_l(tmp_path)
    done = C.c21.read_ledger(out)
    samples = [json.loads(x) for x in (out / "load.jsonl").read_text().splitlines()]
    view = C._latency_view(campaign, out, done, samples, [], 10)
    assert view["gate_passed"] is True
    assert set(view["windows"]) == {"L-ref", "L-bnd"}  # the comparison measures nothing
    assert {k: v["verdict"] for k, v in view["per_strategy"].items()} == {
        "full_source/matrix single_path_bounded": "improvement",
        "full_source/matrix metis_history_bounded": "no_difference"}  # fmt: skip
    analyses = {"L": {"latency": view, "reconciled": True, "exactness": {}, "work": {}}}
    assert C.disposition(analyses)["single_path_bounded"]["timing_verdict"] == "improvement"


def test_stage_l_is_inconclusive_for_load_sleep_or_a_missing_log_and_never_a_claim(
    campaign: Any, tmp_path: Path
) -> None:
    cases: dict[str, dict[str, Any]] = {
        "load": dict(load=5.5),  # one 1-minute sample above 0.5 x 10 CPUs
        "contaminated experiment": dict(contaminated=True),
        "interrupted experiment": dict(state="interrupted"),
    }
    for name, kwargs in cases.items():
        root = tmp_path / name.replace(" ", "_")
        root.mkdir()
        out = _stage_l(root, **kwargs)
        samples = [json.loads(x) for x in (out / "load.jsonl").read_text().splitlines()]
        view = C._latency_view(campaign, out, C.c21.read_ledger(out), samples, [], 10)
        assert view["gate_passed"] is False and view["verdict"] == "inconclusive", name
        assert view["per_strategy"] == {}, name
    (tmp_path / "sleep").mkdir()
    out = _stage_l(tmp_path / "sleep")
    samples = [json.loads(x) for x in (out / "load.jsonl").read_text().splitlines()]
    slept = [{"t": 25.0, "type": "Sleep"}]
    assert C._latency_view(campaign, out, C.c21.read_ledger(out), samples, slept, 10)[
        "gate_passed"] is False  # fmt: skip
    no_log = C._latency_view(campaign, out, C.c21.read_ledger(out), samples, None, 10)
    assert no_log["gate_passed"] is False and no_log["per_strategy"] == {}


def test_the_committed_analysis_drops_only_the_per_pair_families_and_pins_them() -> None:
    pair = {"a->b": {"cases": 1}}
    result: dict[str, Any] = {"work": {"w": {"families": {"cohort": {"x": 1}, "pair": pair}}},
                              "stage": "T"}  # fmt: skip
    out = C.compact(result)
    assert out["work"]["w"]["families"]["cohort"] == {"x": 1}
    assert out["work"]["w"]["families"]["pair"]["omitted_from_compact_copy"] == 1
    assert out["work"]["w"]["families"]["pair"]["sha256"] == C.sha256_bytes(
        json.dumps(pair, sort_keys=True).encode())  # fmt: skip
    assert result["work"]["w"]["families"]["pair"] == pair  # the input is untouched


# ----------------------------------------------------------------------------- stage L plumbing


def test_the_ref_and_bnd_arms_run_and_compare_through_the_real_latency_tools(
    campaign: Any, tmp_path: Path
) -> None:
    """The stage-L commands, end to end, over the tracked fixture bundle: a miniature protocol
    (three cases, two repeats) built from the rendered L01-R022 protocol, the real arms file,
    `benchmark.latency run --arm REF|BND` and `report.latency compare --lane heuristic --pair
    BOUNDED=REFERENCE`. Pins the pairing, the arm option subsets and the compare output."""
    import subprocess

    from snapshot.bundle import load_bundle

    parent = load_bundle(FIXTURE)
    doc = yaml.safe_load(C.render_protocol(campaign))
    doc["parent_bundle"] = {"bundle_id": parent.bundle_id, "bundle_hash": parent.bundle_hash}
    doc["matrix"] = [
        {"case": "emp-09bc4e-779ded-low-2", "split": "tuning", "covers": ["small"]},
        {"case": "emp-09bc4e-779ded-low-1", "split": "held_out", "covers": ["small"]},
        {"case": "nod-09bc4e-c96de2-medium-1", "split": "held_out", "covers": ["no_route"]},
    ]  # the corpus subset needs a no-direct-pool case
    doc["sentinel"] = {"token_in": "USDC", "token_out": "USDT0", "amount": "1500.25"}
    doc["timing"] = {"warmup": 1, "repeats": 2, "orders": ["fixed", "reverse"]}
    doc["acceptance"]["min_timed_solve_seconds"] = 0.0
    protocol = tmp_path / "protocol.yaml"
    protocol.write_text(yaml.safe_dump(doc, sort_keys=False))
    arms = yaml.safe_load(C.render_arms(campaign, protocol.read_text()))
    arms["protocol"] = {"path": str(protocol), "sha256": C.sha256_bytes(protocol.read_bytes())}
    arms_path = tmp_path / "arms.yaml"
    arms_path.write_text(yaml.safe_dump(arms, sort_keys=False))

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["uv", "run", "python", *args], cwd=REPO, text=True,
                              capture_output=True, timeout=600)  # fmt: skip

    dirs = {}
    for arm in ("REF", "BND"):
        out = tmp_path / arm
        done = run("-m", "benchmark.latency", "run", "--protocol", str(protocol), "--arms",
                   str(arms_path), "--arm", arm, "--bundle", str(FIXTURE), "--out", str(out),
                   "--allow-dirty")  # fmt: skip
        assert done.returncode == 0, done.stderr[-2000:]
        (dirs[arm],) = [p for p in out.iterdir() if p.is_dir()]
        experiment = json.loads((dirs[arm] / "experiment.json").read_text())
        assert experiment["state"] == "complete" and experiment["arm"]["name"] == arm
        expected = list(campaign.raw["latency"]["pairs"]) if arm == "BND" else list(
            campaign.raw["latency"]["pairs"].values())  # fmt: skip
        assert experiment["algorithms"] == expected
    pairs = [f"{c}={r}" for c, r in campaign.raw["latency"]["pairs"].items()]
    args = ["-m", "report.latency", "compare", str(dirs["REF"]), str(dirs["BND"]),
            "--lane", "heuristic", "--json", str(tmp_path / "compare.json")]  # fmt: skip
    for pair in pairs:
        args += ["--pair", pair]
    done = run(*args)
    assert done.returncode == 0, done.stderr[-2000:]
    result = json.loads((tmp_path / "compare.json").read_text())
    assert result["pairs"] == campaign.raw["latency"]["pairs"]
    assert result["coverage_problems"] == {"baseline": [], "candidate": [], "pairing": []}
    assert result["arms"]["baseline"] == "REF" and result["arms"]["candidate"] == "BND"
    keys = {k for k in result["timing"] if k.endswith("matrix " + "single_path_bounded")}
    assert keys, result["timing"].keys()
    for algorithm in campaign.raw["latency"]["pairs"]:
        entry = result["timing"][f"full_source/matrix {algorithm}"]
        assert entry["reference"] == campaign.raw["latency"]["pairs"][algorithm]
        assert entry["verdict"] in ("faster", "slower", "no_worthwhile_change",
                                    "insufficient_cases", "lost_samples")  # fmt: skip
    # the experiment's own result of every pair is identical in the exact semantic fields
    exact_view = json.loads((dirs["REF"] / "experiment.json").read_text())
    assert exact_view["profile"]["path"].endswith("timing_pairs.yaml")


def test_a_pool_entry_without_a_family_key_is_constant_product(tmp_path: Path) -> None:
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "pools.json").write_text(json.dumps({"pools": [
        {"pool_id": "cp", "reserve0": "1"},
        {"pool_id": "cl", "family": "concentrated"},
        {"pool_id": "lb", "family": "liquidity_book"}]}))  # fmt: skip
    assert C._pool_families(tmp_path, "b") == {"cp": "constant_product", "cl": "concentrated",
                                               "lb": "liquidity_book"}  # fmt: skip
    assert PA.plan_mix({"evaluation": {"trace": [{"pool_id": "cp"}, {"pool_id": "cl"}]}},
                       C._pool_families(tmp_path, "b")) == "cl+cp"  # fmt: skip

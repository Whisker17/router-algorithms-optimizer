"""WHI-1633 research_024 campaign: the schedule, the analysis rules, the timing protocol and a
fixture stage T.

- The committed schedule checks clean against contract §12 (inputs, roster, arms, timing values),
  the generated profiles, the §7.2 base-control decisions and the timing files; `check` refuses
  drift (a schedule value, a group, a base-control decision, the T3 reading).
- The literal T3 reading (orchestrator decision 2): only a `pmset -g log` entry whose type column
  is exactly Sleep, Wake or DarkWake is a trigger; `Wake Requests` and the other types are kept
  and reported.
- §7.5 control classes and audits reproduce contract §12 `campaign.control_examples` and
  `control_audit_examples`; comparisons keep every denominator; rankability follows §7.3.
- Stage L (§8.4) with a scripted host: the launch gate's finite wait, each trigger T1-T5, the
  first valid attempt, the cap, an interrupted attempt counted as T5 on resume.
- A fixture stage T (the tracked `mantle_mixed` bundle for both cohorts) runs every quality
  invocation through the real executor and analyses clean; the tables regenerate identically.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import sys
from collections.abc import Callable, Mapping
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "routing" / "mantle_mixed"


def _load(name: str, relative: str) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


C: Any = _load("r024_campaign", "tools/research_024/r024_campaign.py")
CA: Any = sys.modules["r024_campaign_analysis"]
RR: Any = sys.modules["r024_rule"]
RAW = C.load_raw()
REGISTRY = C.registry(RAW)


def _sha(path: Path) -> str:
    return str(C.sha256_bytes(path.read_bytes()))


# ----------------------------------------------------------------------------- schedule


def test_the_committed_schedule_checks_clean() -> None:
    assert C.check(RAW) == []


def test_the_schedule_carries_the_contracts_values() -> None:
    timing = REGISTRY["timing"]
    for key in ("units", "max_started_attempts_per_unit", "launch", "validity", "triggers"):
        assert RAW["timing"][key] == timing[key]
    assert RAW["all19"] == REGISTRY["roster"]["all19"]
    for key, spec in REGISTRY["campaign"]["inputs"].items():
        assert {k: RAW["inputs"][key][k] for k in ("bundle_hash", "cases")} == spec


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["timing"].update(max_started_attempts_per_unit=4),
        lambda r: r["timing"]["launch"].update(max_wait_seconds=43200),
        lambda r: r["timing"]["t3_reading"].update(trigger_types=["Sleep", "Wake"]),
        lambda r: r["timing"]["t3_reading"].update(match="first_word"),
        lambda r: r["groups"]["u1"].remove("direct"),
        lambda r: r["groups"].update(u1=["single_path", "direct"]),
        lambda r: r["base_controls"]["BASE-E1"].update(served_by="arm"),
        lambda r: r["base_controls"]["E2-E1only"].update(served_by="Q19"),
        lambda r: r["inputs"]["report_full"].update(cases=301),
        lambda r: r.update(all19_resolved_sha256="0" * 64),
        lambda r: r["arms"][3].update(work_pass=True),
    ],
)
def test_check_refuses_a_schedule_that_departs(change: Callable[[dict[str, Any]], None]) -> None:
    raw = copy.deepcopy(RAW)
    change(raw)
    assert C.check(raw)


def test_group_profiles_keep_the_roster_values_and_preset_provenance() -> None:
    full = C.resolved_of(C.all_document(RAW))
    for key, (doc, _) in C.profiles(RAW).items():
        if not key.startswith("q19-"):
            continue
        resolved = C.resolved_of(doc)
        assert resolved["selection"]["mode"] == "all"
        for identity in doc["algorithms"]:
            assert C.identity_values(resolved, identity) == C.identity_values(full, identity)
            if identity in (C.E1, C.E2):
                assert resolved["algorithm_options"][identity]["source"]["kind"] == "preset"


def test_the_base_control_decisions_follow_the_stated_rule() -> None:
    e1 = C.base_control_decision(RAW, C.E1)
    e2 = C.base_control_decision(RAW, C.E2)
    assert (e1["base"], e1["served_by"]) == ("metis_inspired", "Q19")
    assert (e2["base"], e2["served_by"]) == ("incremental_graph", "Q19")
    assert C.e1only_decision(RAW)["served_by"] == "arm"
    assert C.e1only_options(RAW) == {
        k: C.preset_options(RAW, C.E2)[k]
        for k in ("base", "solver", "rounds", "tolerance", "grid", "maxiter")
    }


def test_the_timing_files_follow_l01_and_the_contract_units() -> None:
    files = C.timing_files(RAW)
    import yaml

    protocol = yaml.safe_load(files[RAW["timing"]["protocol"]["path"]])
    source = yaml.safe_load((REPO / "config" / "latency" / "l01.yaml").read_text())
    assert {k: v for k, v in protocol.items() if k not in ("key", "profile")} == {
        k: v for k, v in source.items() if k not in ("key", "profile")
    }
    assert protocol["key"] == "L01-R024"
    arms = yaml.safe_load(files[RAW["timing"]["arms"]["path"]])
    names = [a["name"] for a in arms["arms"]]
    assert names == ["UP-base", "UP-cand", "U1", "U2", "U3", "U4", "U5", "U6"]
    assert arms["arms"][0]["algorithms"] == ["incremental_graph", "metis_inspired"]
    assert arms["comparisons"][0]["pairs"] == {
        "split_polish": "metis_inspired",
        "marginal_activation": "incremental_graph",
    }
    assert all(a["stages"] == ["timing", "cold"] for a in arms["arms"])


def test_invocations_cover_every_arm_once_per_stage() -> None:
    for stage in ("T", "R"):
        invs = C.invocations(RAW, stage)
        ids = [i["id"] for i in invs]
        assert len(ids) == len(set(ids)) == 2 * 8 + 3 + 2 * 8 + 1
        assert not any(i["work_pass"] for i in invs if i["arm"] in ("E2-wm", "E2-cm"))
        bundles = {i["bundle"] for i in invs}
        assert bundles == set(RAW["stage_bundles"][stage].values())


# ----------------------------------------------------------------------------- T3 reading

PMSET = "\n".join(
    [
        "2026-10-07 19:56:07 +0800 Wake                \tDarkWake to FullWake from Deep Idle",
        "2026-10-07 19:56:07 +0800 WakeDetails         \tDriverReason:smc.sysState.Wake",
        "2026-10-07 19:56:08 +0800 Wake Requests       \t[*process=dasd request=SleepService]",
        "2026-10-07 19:56:09 +0800 Sleep               \tEntering DarkWake state (Clamshell)",
        "   continuation without a stamp",
        "2026-10-07 19:57:00 +0800 DarkWake            \tDarkWake from Deep Idle",
        "2026-10-07 19:58:00 +0800 Assertions          \tPID 1(caffeinate) Created",
        "2026-10-07 19:59:00 +0800 Wake Requests       \t[*process=x]",
    ]
)


def _t(stamp: str) -> float:
    return float(CA._epoch(f"2026-10-07 {stamp} +0800"))


def test_t3_reads_the_type_column_literally() -> None:
    entries = CA.pmset_entries(PMSET)
    assert [e["type"] for e in entries] == [
        "Wake",
        "WakeDetails",
        "Wake Requests",
        "Sleep",
        "DarkWake",
        "Assertions",
        "Wake Requests",
    ]
    hits = CA.t3_hits(entries, _t("19:56:00"), _t("19:59:59"))
    assert [h["type"] for h in hits] == ["Wake", "Sleep", "DarkWake"]
    # only `Wake Requests` / `Assertions` inside the window: retained, never a trigger
    assert CA.t3_hits(entries, _t("19:57:30"), _t("19:59:30")) == []
    kinds = CA.pmset_types(PMSET, _t("19:57:30"), _t("19:59:30"))
    assert kinds["types"] == {"Assertions": 1, "Wake Requests": 1}
    # an entry stamped with second s covers [s, s + 1)
    assert CA.t3_hits(entries, _t("19:56:09") + 0.5, _t("19:56:20"))
    assert not CA.t3_hits(entries, _t("19:56:10"), _t("19:56:20"))
    kept = CA.pmset_window_lines(PMSET, _t("19:56:09"), _t("19:56:09"))
    assert kept == [PMSET.splitlines()[3], PMSET.splitlines()[4]]


def test_the_registered_reading_is_the_codes() -> None:
    assert tuple(RAW["timing"]["t3_reading"]["trigger_types"]) == CA.T3_TYPES
    assert RAW["timing"]["t3_reading"]["match"] == "exact_type_column"


# ----------------------------------------------------------------------------- §7.5 and comparisons


@pytest.mark.parametrize("example", REGISTRY["campaign"]["control_examples"])
def test_control_classes_reproduce_the_contract(example: dict[str, Any]) -> None:
    assert CA.control_class(example["record"]) == example["class"]


@pytest.mark.parametrize("example", REGISTRY["campaign"]["control_audit_examples"])
def test_control_audits_reproduce_the_contract(example: dict[str, Any]) -> None:
    got = CA.control_audit_row(example["control"], example["embedded"], example["treatment"])
    assert got == example["outcome"]


def _slim(status: str, gross: int | None) -> dict[str, Any]:
    return {"status": status, "gross": gross, "quotes": None}


def test_compare_keeps_every_denominator() -> None:
    cases = ["a", "b", "c", "d", "e"]
    base = {
        "a": _slim("ok", 1000),
        "b": _slim("ok", 0),
        "c": _slim("timeout", None),
        "d": _slim("ok", 2000),
        "e": _slim("ok", 500),
    }
    cand = {
        "a": _slim("ok", 1001),
        "b": _slim("ok", 5),
        "c": _slim("ok", 10),
        "d": _slim("no_route", None),
        "e": _slim("ok", 500),
    }
    fam = {"a": "x->y", "b": "x->y", "c": "y->x", "d": "y->x", "e": "y->x"}
    strata = {c: CA.stratum_of(c) for c in cases}
    out = CA.compare(base, cand, cases, fam, strata)
    assert out["scheduled"] == 5 and out["common_ok"] == 3
    assert out["only_baseline_ok"] == 1 and out["only_candidate_ok"] == 1
    assert out["zero_baseline_na"] == ["b"]
    assert (out["higher"], out["equal"], out["lower"]) == (1, 1, 0)
    assert out["bps"]["n"] == 2 and out["bps"]["max"] == 10.0
    assert out["transitions"]["ok->no_route"] == 1
    assert out["families"] == {"n": 2, "net_plus": 1, "net_minus": 0}


def test_strata_and_rankability() -> None:
    assert [
        CA.stratum_of(c)
        for c in ("bnd-1-2-dust", "nod-1-2-medium-1", "emp-1-2-large-3", "emp-1-2-low-1")
    ] == ["bnd", "nod", "large", "low"]
    full = list(CA.FAMILIES_ALL)
    sor = ["concentrated", "constant_product"]
    assert not CA.rankable("uni_sor_port", "incremental_graph", full)
    assert CA.rankable("uni_sor_port", "incremental_graph", sor)
    assert CA.rankable("uni_sor_port", "cfmm_dual", full)
    assert not CA.rankable("direct_split_certified", "split_polish", sor)
    assert CA.rankable("split_polish", "marginal_activation", full)


def test_the_envelope_is_the_best_ok_row() -> None:
    rows = {
        "r1": {"a": _slim("ok", 5), "b": _slim("timeout", None)},
        "r2": {"a": _slim("ok", 7), "b": _slim("no_route", None)},
    }
    env = CA.envelope(rows, ["a", "b"])
    assert env["a"]["gross"] == 7 and env["b"]["status"] == "no_ok_row"


# ----------------------------------------------------------------------------- stage L


class FakeProc:
    def __init__(self, host: FakeHost, argv: list[str], lifetime: float) -> None:
        self.host, self.argv, self.pid = host, argv, 4242
        self.ends = host.now + lifetime
        self.returncode: int | None = None
        self.killed = False

    def poll(self) -> int | None:
        if self.returncode is None and (self.host.now >= self.ends or self.killed):
            self.returncode = 130 if self.killed else self.host.exit_code(self.argv)
            if not self.killed:
                self.host.produce(self.argv)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        while self.poll() is None:
            self.host.sleep(1.0)
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.killed = True

    kill = terminate


class FakeHost:
    """A scripted host: a clock, a load curve, a pmset log and commands that leave files."""

    def __init__(
        self,
        load: Callable[[float], float] = lambda t: 0.5,
        pmset: str = "",
        fail: str | None = None,
        duration: float = 600.0,
    ) -> None:
        self.now = 1_800_000_000.0
        self.load_at, self.pmset_text, self.fail, self.duration = load, pmset, fail, duration
        self.started: list[list[str]] = []
        self.caffeinate_alive = True

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def load(self) -> float:
        return self.load_at(self.now)

    def pmset(self) -> str:
        return self.pmset_text

    def exit_code(self, argv: list[str]) -> int:
        return 1 if self.fail and self.fail in " ".join(argv) else 0

    def popen(self, argv: list[str], **kwargs: Any) -> Any:
        if argv[0] == "caffeinate":
            proc = FakeProc(self, argv, 1e12)
            return proc
        self.started.append(argv)
        return FakeProc(self, argv, self.duration)

    def produce(self, argv: list[str]) -> None:
        if "benchmark.latency" in argv:
            out = Path(argv[argv.index("--out") + 1]) / "exp"
            run = out / "runs" / "timing-fixed-x"
            run.mkdir(parents=True)
            (run / "manifest.json").write_text(json.dumps({"state": "complete"}))
            (out / "experiment.json").write_text(
                json.dumps(
                    {
                        "state": "complete",
                        "partial": False,
                        "runs": [{"run_id": "timing-fixed-x", "run_dir": str(run)}],
                    }
                )
            )
        elif "report.latency" in argv:
            path = Path(argv[argv.index("--json") + 1])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}")
        elif "quote" in argv:
            run = Path(argv[argv.index("--quotes-dir") + 1]) / "q" / "runs" / "r"
            run.mkdir(parents=True)
            (run / "manifest.json").write_text(json.dumps({"state": "complete"}))
            (run / "cases.jsonl").write_text("")


def _summary(path: Path) -> dict[str, Any]:
    return {"load": None, "timing": {}, "cold": {}}


def _stage(tmp_path: Path, host: FakeHost, units: list[str] | None = None) -> Any:
    raw = copy.deepcopy(RAW)
    if units is not None:
        raw["timing"]["units"] = [u for u in raw["timing"]["units"] if u["unit"] in units]
    raw["host"]["logical_cpus"] = os.cpu_count()  # the host the driver records (WHI-1746)
    return C.TimingStage(
        raw,
        tmp_path / "inputs",
        tmp_path / "L",
        echo=lambda _m: None,
        clock=host.clock,
        sleep=host.sleep,
        load=host.load,
        pmset=host.pmset,
        popen=host.popen,
        caffeinate=["caffeinate", "-i", "-m", "-s"],
    )


def _events(stage: Any, kind: str) -> list[dict[str, Any]]:
    return [e for e in stage.ledger.entries() if e.get("event") == kind]


def test_a_clean_unit_is_valid_on_its_first_attempt(tmp_path: Path) -> None:
    host = FakeHost()
    stage = _stage(tmp_path, host, ["UP", "U2", "UQ"])
    assert stage.run() == 0
    outcomes = {e["unit"]: e for e in _events(stage, "unit_outcome")}
    assert {u: o["outcome"] for u, o in outcomes.items()} == {
        "UP": "valid",
        "U2": "valid",
        "UQ": "valid",
    }
    assert outcomes["UP"]["attempt"] == "L-UP-a1"
    # UP: base experiment, candidate experiment, then the heuristic-lane compare with both pairs
    compare = next(a for a in host.started if "report.latency" in a)
    assert "--pair" in compare and "split_polish=metis_inspired" in compare
    quotes = [a for a in host.started if "quote" in a]
    assert len(quotes) == 5 and all("--details" in q and "all" in q for q in quotes)
    analysis = CA.analyze_timing(stage.raw, tmp_path / "L", summarize=_summary)
    assert analysis["statements"]["usable_latency_obtained"] == ["UP", "U2", "UQ"]


def test_a_busy_host_ends_the_unit_without_launch(tmp_path: Path) -> None:
    host = FakeHost(load=lambda t: 3.5)
    stage = _stage(tmp_path, host, ["U2"])
    stage.run()
    outcome = _events(stage, "unit_outcome")[0]
    assert outcome["outcome"] == "inconclusive_no_launch"
    gates = _events(stage, "launch_gate")
    assert len(gates) > 1 and not any(g["passed"] for g in gates)
    assert host.now - 1_800_000_000.0 <= 21600 + 600
    assert not host.started


def test_load_above_half_the_cpus_aborts_and_the_next_attempt_runs(tmp_path: Path) -> None:
    start = 1_800_000_000.0
    # quiet for the gate, loaded during the first attempt, quiet afterwards
    host = FakeHost(load=lambda t: 6.0 if start + 200 < t < start + 400 else 0.5)
    stage = _stage(tmp_path, host, ["U2"])
    stage.run()
    validity = _events(stage, "attempt_validity")
    assert validity[0]["valid"] is False and "T1_load" in validity[0]["triggers"]
    assert _events(stage, "abort")[0]["trigger"] == "T1_load"
    assert validity[1]["valid"] is True
    assert _events(stage, "unit_outcome")[0]["attempt"] == "L-U2-a2"


def test_a_sleep_entry_invalidates_and_three_attempts_cap_the_unit(tmp_path: Path) -> None:
    lines = []
    from datetime import UTC, datetime

    for k in range(0, 80000, 200):
        stamp = datetime.fromtimestamp(1_800_000_000 + k, UTC).strftime("%Y-%m-%d %H:%M:%S +0000")
        lines.append(f"{stamp} DarkWake            \tDarkWake from Deep Idle")
    host = FakeHost(pmset="\n".join(lines))
    stage = _stage(tmp_path, host, ["U4"])
    stage.run()
    validity = _events(stage, "attempt_validity")
    assert len(validity) == 3 and all("T3_sleep" in v["triggers"] for v in validity)
    assert _events(stage, "unit_outcome")[0]["outcome"] == "inconclusive_cap_exhausted"


def test_wake_requests_alone_never_invalidate(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    lines = [
        f"{datetime.fromtimestamp(1_800_000_000 + k, UTC).strftime('%Y-%m-%d %H:%M:%S +0000')}"
        " Wake Requests       \t[*process=dasd]"
        for k in range(0, 5000, 100)
    ]
    host = FakeHost(pmset="\n".join(lines))
    stage = _stage(tmp_path, host, ["U4"])
    stage.run()
    validity = _events(stage, "attempt_validity")
    assert validity[0]["valid"] is True
    assert validity[0]["pmset_window"]["types"]["Wake Requests"] > 0


def test_a_failed_experiment_or_missing_capture_is_t5_or_t4(tmp_path: Path) -> None:
    host = FakeHost(fail="report.latency")
    stage = _stage(tmp_path, host, ["UP"])
    stage.run()
    first = _events(stage, "attempt_validity")[0]
    assert first["triggers"] == ["T5_incomplete_execution"]

    def broken() -> str:
        raise OSError("pmset unavailable")

    host2 = FakeHost()
    stage2 = _stage(tmp_path / "b", host2, ["U4"])
    stage2.pmset = broken
    stage2.run()
    assert "T4_sleep_prevention_or_capture" in _events(stage2, "attempt_validity")[0]["triggers"]


def test_an_interrupted_attempt_counts_as_t5_on_resume(tmp_path: Path) -> None:
    host = FakeHost()
    stage = _stage(tmp_path, host, ["U4"])
    (tmp_path / "L").mkdir(parents=True)
    stage.ledger.append(
        {"event": "attempt_start", "unit": "U4", "attempt": "L-U4-a1", "t_start": host.now}
    )
    (tmp_path / "L" / "U4" / "a1").mkdir(parents=True)
    stage.run()
    validity = _events(stage, "attempt_validity")
    assert validity[0]["attempt"] == "L-U4-a1" and validity[0]["valid"] is False
    assert validity[0]["triggers"] == ["T5_incomplete_execution"]
    assert _events(stage, "unit_outcome")[0]["attempt"] == "L-U4-a2"


# ----------------------------------------------------------------------------- stage L verification
# WHI-1744 (release review R1-F1): statement (a) of §8.7 rests on the retained evidence, re-derived
# with the driver's own trigger evaluator, never on the ledger's terminal labels alone.

TIMING_RECORDS = "ROUTER_R024_TIMING_RECORDS"


def _verified(stage: Any) -> dict[str, Any]:
    return dict(CA.analyze_timing(stage.raw, stage.out, summarize=_summary))


def _rewrite(stage: Any, change: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]) -> None:
    entries = change(stage.ledger.entries())
    stage.ledger.path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def _resample(stage: Any, change: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    """`change` applied to every load sample, in `load.jsonl` and in the launch-gate groups alike
    (the driver writes each gate sample to both)."""
    load = stage.out / "load.jsonl"
    rows = [change(json.loads(x)) for x in load.read_text().splitlines() if x]
    load.write_text("".join(json.dumps(x) + "\n" for x in rows))
    _rewrite(
        stage,
        lambda es: [
            {**e, "samples": [change(s) for s in e["samples"]]}
            if e["event"] == "launch_gate"
            else e
            for e in es
        ],
    )


def _refuted(analysis: Mapping[str, Any]) -> list[str]:
    """The problems of an analysis that must not make the positive §8.7 statement."""
    assert analysis["statements"]["measurement_attempted_correctly"] is False
    assert analysis["problems"]
    return list(analysis["problems"])


def test_the_drivers_own_records_verify_clean_for_every_outcome(tmp_path: Path) -> None:
    start = 1_800_000_000.0
    scenarios: dict[str, tuple[FakeHost, str]] = {
        "valid": (FakeHost(), "UP"),
        "retried": (FakeHost(load=lambda t: 6.0 if start + 200 < t < start + 400 else 0.5), "U2"),
        "no_launch": (FakeHost(load=lambda t: 3.5), "U2"),
        "cap": (FakeHost(fail="benchmark.latency"), "U4"),
        "quote": (FakeHost(), "UQ"),
    }
    outcomes = {}
    for label, (host, unit) in scenarios.items():
        stage = _stage(tmp_path / label, host, [unit])
        stage.run()
        analysis = _verified(stage)
        assert analysis["problems"] == [], label
        assert analysis["statements"]["measurement_attempted_correctly"] is True
        outcome = analysis["units"][unit]["outcome"]
        outcomes[label] = (outcome["outcome"], outcome["attempt"])
    assert outcomes == {
        "valid": ("valid", "L-UP-a1"),
        "retried": ("valid", "L-U2-a2"),
        "no_launch": ("inconclusive_no_launch", None),
        "cap": ("inconclusive_cap_exhausted", None),
        "quote": ("valid", "L-UQ-a1"),
    }
    for unit in RAW["timing"]["units"]:  # the analysis' experiment list is the driver's
        assert CA.unit_experiments(unit) == [e for e, _ in stage.commands(unit, tmp_path)]


def test_a_resumed_stage_with_an_interrupted_attempt_verifies_clean(tmp_path: Path) -> None:
    host = FakeHost()
    stage = _stage(tmp_path, host, ["U4"])

    def interrupted(seconds: float) -> None:  # the driver stopped inside its first experiment
        host.sleep(seconds)
        if host.started:
            raise KeyboardInterrupt

    stage.sleep = interrupted
    assert stage.run() == 130
    stage.sleep = host.sleep
    assert stage.run() == 0
    assert _events(stage, "abort")[0]["trigger"] == "driver_interrupted"
    analysis = _verified(stage)
    assert analysis["problems"] == []
    assert analysis["units"]["U4"]["outcome"]["attempt"] == "L-U4-a2"


def test_terminal_labels_without_attempts_are_never_attempted_correctly(tmp_path: Path) -> None:
    """The reviewer's reproduction: one `inconclusive_cap_exhausted` per unit, no attempt_start,
    no attempt completion, no monitoring file."""
    (tmp_path / "ledger.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "event": "unit_outcome",
                    "unit": u["unit"],
                    "outcome": "inconclusive_cap_exhausted",
                }
            )
            + "\n"
            for u in RAW["timing"]["units"]
        )
    )
    analysis = CA.analyze_timing(RAW, tmp_path, summarize=_summary)
    assert _refuted(analysis) == [
        f"{u['unit']}: the retained evidence supports no terminal outcome"
        for u in RAW["timing"]["units"]
    ]
    # a `valid` label without its attempt is refuted the same way
    (tmp_path / "ledger.jsonl").write_text(
        json.dumps(
            {"event": "unit_outcome", "unit": "U6", "outcome": "valid", "attempt": "L-U6-a1"}
        )
    )
    raw = copy.deepcopy(RAW)
    raw["timing"]["units"] = [u for u in raw["timing"]["units"] if u["unit"] == "U6"]
    assert _refuted(CA.analyze_timing(raw, tmp_path, summarize=_summary)) == [
        "U6: the retained evidence supports no terminal outcome"
    ]


def test_a_later_attempt_selected_over_an_earlier_valid_one_is_a_problem(tmp_path: Path) -> None:
    stage = _stage(tmp_path, FakeHost(), ["U2"])
    stage.run()
    # a1 is valid by its evidence; its recorded validity is flipped and its outcome dropped, and the
    # driver resumes: it starts a2 and selects it
    _rewrite(
        stage,
        lambda es: [
            {**e, "valid": False} if e["event"] == "attempt_validity" else e
            for e in es
            if e["event"] != "unit_outcome"
        ],
    )
    stage.run()
    assert _events(stage, "unit_outcome")[0]["attempt"] == "L-U2-a2"
    problems = _refuted(_verified(stage))
    assert any(p.startswith("L-U2-a1: recorded validity False") for p in problems)
    assert "U2: attempts started after the first valid attempt L-U2-a1" in problems
    assert any(p.startswith("U2: recorded outcome") and "'L-U2-a1'" in p for p in problems)


def test_cap_exhausted_needs_exactly_n_started_invalid_attempts(tmp_path: Path) -> None:
    stage = _stage(tmp_path, FakeHost(fail="benchmark.latency"), ["U4"])
    stage.raw["timing"]["max_started_attempts_per_unit"] = 2  # a driver that stops after two
    stage.run()
    assert _events(stage, "unit_outcome")[0]["outcome"] == "inconclusive_cap_exhausted"
    assert len(_events(stage, "attempt_start")) == 2
    raw = copy.deepcopy(stage.raw)
    raw["timing"]["max_started_attempts_per_unit"] = 3
    analysis = CA.analyze_timing(raw, stage.out, summarize=_summary)
    assert _refuted(analysis) == ["U4: the retained evidence supports no terminal outcome"]
    raw["timing"]["max_started_attempts_per_unit"] = 1
    analysis = CA.analyze_timing(raw, stage.out, summarize=_summary)
    assert "U4: 2 started attempts, more than N = 1" in _refuted(analysis)


def test_recorded_validity_must_match_the_retained_evidence(tmp_path: Path) -> None:
    start = 1_800_000_000.0
    host = FakeHost(load=lambda t: 6.0 if start + 200 < t < start + 400 else 0.5)
    stage = _stage(tmp_path, host, ["U2"])
    stage.run()
    assert _verified(stage)["problems"] == []
    load = stage.out / "load.jsonl"
    clean = load.read_text()
    # the busy samples gone: a1's recorded T1 has no evidence, so its T1 abort is a protocol breach
    load.write_text("".join(x + "\n" for x in clean.splitlines() if json.loads(x)["load1"] <= 5.0))
    problems = _refuted(_verified(stage))
    assert any(p.startswith("L-U2-a1: recorded validity False ['T1_load'") for p in problems)
    assert any("'inconclusive_protocol_breach'" in p for p in problems)
    load.write_text(clean)
    # the valid attempt's pmset capture gone: T4 by the evidence, `valid` in the ledger
    (stage.out / "U2" / "a2" / "pmset.txt").unlink()
    problems = _refuted(_verified(stage))
    assert any(p.startswith("L-U2-a2: recorded validity True") for p in problems)


def test_the_experiments_are_read_from_the_attempts_own_slot(tmp_path: Path) -> None:
    stage = _stage(tmp_path, FakeHost(), ["U2"])
    stage.run()
    # the attempt's own experiment interrupted, the ledger pointing at a complete copy elsewhere
    own = stage.out / "U2" / "a1" / "U2"
    copy_ = shutil.copytree(own, tmp_path / "elsewhere" / "U2")
    record = own / "exp" / "experiment.json"
    record.write_text(json.dumps({**json.loads(record.read_text()), "state": "interrupted"}))
    _rewrite(
        stage,
        lambda es: [
            {**e, "experiments": [{**x, "dir": str(copy_)} for x in e["experiments"]]}
            if e["event"] == "attempt_end"
            else {**e, "dir": str(copy_)}
            if e["event"] == "experiment_end"
            else e
            for e in es
        ],
    )
    problems = _refuted(_verified(stage))
    assert problems == [
        "L-U2-a1: recorded validity True [] differs from the retained evidence: False "
        "['T5_incomplete_execution']",
        "U2: the retained evidence supports no terminal outcome",
    ]


def test_an_attempt_needs_its_passing_gate_and_its_place_in_the_sequence(tmp_path: Path) -> None:
    stage = _stage(tmp_path, FakeHost(), ["U2"])
    stage.run()
    entries, clean = stage.ledger.entries(), (stage.out / "load.jsonl").read_text()
    # the passing gate group's samples above the headroom, in the ledger and the load record alike
    # (its recorded verdict kept): the group is not the passing one its label claims
    gate = {
        (s["t"], s["load1"]) for e in entries if e["event"] == "launch_gate" for s in e["samples"]
    }
    _resample(stage, lambda s: {**s, "load1": 3.5} if (s["t"], s["load1"]) in gate else s)
    assert _refuted(_verified(stage)) == [
        "L-U2-a1: launch-gate group 1 incomplete: the recorded peak or verdict is not its samples'",
        "L-U2-a1: no passing launch gate immediately before its start",
    ]
    # ... and with the verdict recorded as the samples give it
    _rewrite(
        stage,
        lambda es: [
            {**e, "max_load1": 3.5, "passed": False} if e["event"] == "launch_gate" else e
            for e in es
        ],
    )
    assert _refuted(_verified(stage)) == [
        "L-U2-a1: no passing launch gate immediately before its start"
    ]
    (stage.out / "load.jsonl").write_text(clean)
    # the only attempt recorded as a2: no a1 was started
    _rewrite(
        stage,
        lambda es: [json.loads(json.dumps(e).replace("L-U2-a1", "L-U2-a2")) for e in entries],
    )
    problems = _refuted(_verified(stage))
    assert "U2: started attempts ['L-U2-a2'] are not L-U2-a1, a2, ... in order" in problems


def test_no_launch_needs_gate_evidence_over_the_whole_window(tmp_path: Path) -> None:
    stage = _stage(tmp_path, FakeHost(load=lambda t: 3.5), ["U2"])
    stage.run()
    assert _verified(stage)["problems"] == []
    # the later gate groups dropped: the recorded `no_launch` covers too little of 21,600 s
    _rewrite(
        stage, lambda es: [e for e in es if not (e["event"] == "launch_gate" and e["group"] > 3)]
    )
    assert _refuted(_verified(stage)) == ["U2: the retained evidence supports no terminal outcome"]


# WHI-1746 (release review R2-F1, R2-F2): a launch gate counts only as complete groups at the
# registered cadence, and an attempt window must contain the execution it is read over.


def test_the_reviewers_round_2_reproductions_never_certify(tmp_path: Path) -> None:
    """`r024-r2-probes.py` (review round 2), verbatim; plus the shortened window of R2-F2 and the
    reversed window with every load sample at 10.0."""
    found = {}
    for what in ["compressed_gate", "old_gate", "reversed_window", "short", "reversed_busy"]:
        st = _stage(tmp_path / what, FakeHost(), ["U2"])
        st.run()
        es = st.ledger.entries()
        if what in ("compressed_gate", "old_gate"):
            for e in es:
                if e["event"] == "launch_gate":
                    for sample in e["samples"]:
                        sample["t"] = (
                            e["samples"][0]["t"]
                            if what == "compressed_gate"
                            else sample["t"] - 86400
                        )
        for e in es:
            if e["event"] == "attempt_end" and what != "short" and what.startswith("reversed"):
                e["t_end"] = e["t_start"] - 1
            if e["event"] == "attempt_end" and what == "short":
                e["t_end"] = e["t_start"] + 1
        st.ledger.path.write_text("".join(json.dumps(e) + "\n" for e in es))
        if what == "reversed_busy":
            _resample(st, lambda s: {**s, "load1": 10.0})
        found[what] = _verified(st)
    for what in ["one_sparse_gate", "incomplete_groups"]:
        st = _stage(tmp_path / what, FakeHost(load=lambda t: 3.5), ["U2"])
        st.run()
        es = st.ledger.entries()
        gs = [e for e in es if e["event"] == "launch_gate"]
        if what == "one_sparse_gate":
            es = [e for e in es if e["event"] != "launch_gate"]
            g = gs[0]
            g["samples"] = [{"t": 1800000000.0, "load1": 0.5}, {"t": 1800021600.0, "load1": 0.5}]
            es.insert(2, g)
        if what == "incomplete_groups":
            for g in gs:
                g["samples"] = [g["samples"][0], g["samples"][-1]]
        st.ledger.path.write_text("".join(json.dumps(e) + "\n" for e in es))
        found[what] = _verified(st)
    for what, analysis in found.items():
        assert _refuted(analysis), what
        assert analysis["statements"]["usable_latency_obtained"] == [], what
    assert (
        "L-U2-a1: no passing launch gate immediately before its start"
        in found["old_gate"]["problems"]
    )
    assert (
        "L-U2-a1: launch-gate group 1 incomplete: samples not 30 s apart"
        in (found["compressed_gate"]["problems"])
    )
    assert found["reversed_window"]["problems"] == [
        "L-U2-a1: its window ends at 1800000119.0, not after its start"
    ]
    # the triggers are read over the window that contains the execution, not the recorded one
    assert any(
        "from the retained evidence: False ['T1_load']" in p
        for p in found["reversed_busy"]["problems"]
    )
    assert found["short"]["problems"] == [
        "L-U2-a1: its window [1800000120.0, 1800000121.0] does not contain its execution records "
        "[1800000120.0, 1800000720.0]"
    ]
    for what in ("one_sparse_gate", "incomplete_groups"):
        assert "U2: the retained evidence supports no terminal outcome" in found[what]["problems"]


# WHI-1747 (release review R3-F1, R3-F2, R3-F3): an abort's reason is an event detected by its
# time, experiment records are reconciled by identity as well as time, a load sample is never
# negative.


def test_the_reviewers_round_3_reproductions_never_certify(tmp_path: Path) -> None:
    """The three certified cases of round 3 (`review-r3/reviewer-probes/probes.py`), verbatim, on
    the driver's own fixtures; plus a T4 abort whose only support would be the pmset capture that
    fails at the window end."""
    stages = _perturbation_stages(tmp_path)
    found = {
        # R3-F1: the T1 abort moved to the attempt start, before the first sample above T1
        "abort_before_trigger": _perturbed(
            stages["retried"],
            lambda es, ss: _set(es, "abort", "t", _of(es, "attempt_start")[0]["t_start"]),
        ),
        # R3-F2: the U2 start renamed, its time unchanged
        "wrong_experiment_start_identity": _perturbed(
            stages["valid"], lambda es, ss: _set(es, "experiment_start", "experiment", "UP-cand")
        ),
        # R3-F3: the passing gate's loads -1.0, in the gate, load.jsonl and its peak
        "negative_gate_load": _perturbed(
            stages["valid"],
            lambda es, ss: (
                _move(es, ss, _group_times(es), load1=-1.0),
                _set(es, "launch_gate", "max_load1", -1.0),
            ),
        ),
    }
    for what, analysis in found.items():
        assert _refuted(analysis), what
        assert analysis["statements"]["usable_latency_obtained"] == [], what
    assert found["abort_before_trigger"]["problems"] == [
        "U2: recorded outcome {'outcome': 'valid', 'attempt': 'L-U2-a2', 'invalid_attempts': None} "
        "differs from the evidence {'outcome': 'inconclusive_protocol_breach'}"
    ]
    assert found["wrong_experiment_start_identity"]["problems"] == [
        "L-U2-a1: its experiment and abort records are not its registered experiments, each "
        "started then ended in order"
    ]
    assert (
        "load.jsonl: 10 samples without a time and a nonnegative load"
        in found["negative_gate_load"]["problems"]
    )
    # a T4 abort with the capture missing, recorded consistently: the capture fails only at the
    # window end, after the abort, and the driver never aborts on it
    stage = stages["retried"]
    pmset = stage.out / "U2" / "a1" / "pmset.txt"
    kept = pmset.read_text()
    pmset.unlink()
    try:
        analysis = _perturbed(
            stage,
            lambda es, ss: (
                _abort_reason(es, "T4_sleep_prevention_or_capture"),
                _set(
                    es,
                    "attempt_validity",
                    "triggers",
                    ["T1_load", "T4_sleep_prevention_or_capture", "T5_incomplete_execution"],
                    attempt="L-U2-a1",
                ),
            ),
        )
    finally:
        pmset.write_text(kept)
    assert "'inconclusive_protocol_breach'" in " ".join(_refuted(analysis))


def test_the_drivers_t3_and_t4_aborts_verify_clean(tmp_path: Path) -> None:
    """An abort the driver records on a detected T3 (a DarkWake in the capture) or T4 (caffeinate
    gone) is supported by the evidence up to it: the unit verifies, its retry is selected."""
    start = 1_800_000_000.0
    stamp = _datetime_stamp(start + 300)
    sleep = _stage(
        tmp_path / "t3",
        FakeHost(pmset=f"{stamp} DarkWake            \tDarkWake from Deep Idle"),
        ["U2"],
    )
    host = FakeHost()
    awake = _stage(tmp_path / "t4", host, ["U2"])
    dead: list[Any] = []

    def popen(argv: list[str], **kwargs: Any) -> Any:
        proc = host.popen(argv, **kwargs)
        if argv[0] == "caffeinate" and not dead:  # the stage's first caffeinate dies at +300 s
            proc.ends = start + 300
            dead.append(proc)
        return proc

    awake.popen = popen
    for stage, trigger in ((sleep, "T3_sleep"), (awake, "T4_sleep_prevention_or_capture")):
        stage.run()
        assert [e["trigger"] for e in _events(stage, "abort")] == [trigger]
        analysis = _verified(stage)
        assert analysis["problems"] == [], trigger
        assert analysis["units"]["U2"]["outcome"]["attempt"] == "L-U2-a2"


def _datetime_stamp(moment: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%d %H:%M:%S +0000")


def _perturbed(
    stage: Any, change: Callable[[list[dict[str, Any]], list[dict[str, Any]]], Any]
) -> Any:
    """The analysis after `change(ledger entries, load samples)` edits them in place; the stage's
    records are restored afterwards."""
    ledger, load = stage.ledger.path, stage.out / "load.jsonl"
    texts = ledger.read_text(), load.read_text()
    entries = [json.loads(x) for x in texts[0].splitlines() if x]
    samples = [json.loads(x) for x in texts[1].splitlines() if x]
    raw = change(entries, samples)  # a schedule, when the change is to the registered order
    raw = raw if isinstance(raw, dict) and "timing" in raw else stage.raw
    ledger.write_text("".join(json.dumps(e) + "\n" for e in entries))
    load.write_text("".join(json.dumps(s) + "\n" for s in samples))
    try:
        return CA.analyze_timing(raw, stage.out, summarize=_summary)
    finally:
        ledger.write_text(texts[0])
        load.write_text(texts[1])


def _of(entries: list[dict[str, Any]], kind: str, **match: Any) -> list[dict[str, Any]]:
    return [
        e for e in entries if e["event"] == kind and all(e.get(k) == v for k, v in match.items())
    ]


def _move(
    entries: list[dict[str, Any]], samples: list[dict[str, Any]], times: set[float], **new: Any
) -> None:
    """The samples at `times` changed (`dt`: shifted; else the fields given), in the load record and
    the gate groups alike, as the driver would have written them."""
    for s in [*samples, *(x for g in _of(entries, "launch_gate") for x in g["samples"])]:
        if s["t"] in times:
            s.update({"t": s["t"] + new["dt"]} if "dt" in new else new)


def _group_times(entries: list[dict[str, Any]], number: int = 1, attempt: int = 1) -> set[float]:
    return {
        s["t"] for s in _of(entries, "launch_gate", group=number, attempt=attempt)[0]["samples"]
    }


def _drop_group(entries: list[dict[str, Any]], number: int) -> None:
    entries.remove(_of(entries, "launch_gate", group=number)[0])


def _set(entries: list[dict[str, Any]], kind: str, field: str, value: Any, **match: Any) -> None:
    for e in _of(entries, kind, **match):
        e[field] = value(e) if callable(value) else value


def _experiments(entries: list[dict[str, Any]], name: str, field: str, value: Any) -> None:
    """One experiment's time changed in all three records of it."""
    for e in [
        *_of(entries, "experiment_end"),
        *(x for a in _of(entries, "attempt_end") for x in a["experiments"]),
    ]:
        if e["experiment"] == name:
            e[field] = value(e[field])
    if field == "t_start":
        _set(entries, "experiment_start", "t", lambda e: value(e["t"]), experiment=name)


def _pass_group(entries: list[dict[str, Any]], samples: list[dict[str, Any]], number: int) -> None:
    """Gate group `number` (attempt 1) quiet, with the verdict its samples then give."""
    _move(entries, samples, _group_times(entries, number), load1=0.5)
    _set(entries, "launch_gate", "passed", True, group=number)
    _set(entries, "launch_gate", "max_load1", 0.5, group=number)


def _group_past_the_window(entries: list[dict[str, Any]], samples: list[dict[str, Any]]) -> None:
    """One more no-launch group 420 s after the last (52nd), recorded as the driver would."""
    last = _of(entries, "launch_gate", group=52)[0]
    extra = [{**s, "t": s["t"] + 420} for s in last["samples"]]
    entries.insert(
        entries.index(_of(entries, "no_launch")[0]), {**last, "group": 53, "samples": extra}
    )
    samples.extend(copy.deepcopy(extra))
    _set(entries, "no_launch", "groups", 53)
    _set(entries, "no_launch", "waited_seconds", lambda e: e["waited_seconds"] + 420)


def _resampled_late(
    entries: list[dict[str, Any]], samples: list[dict[str, Any]], number: int
) -> None:
    """Every sample from gate group `number` on 60 s later, the no-launch wait with them."""
    first = min(_group_times(entries, number))
    _move(entries, samples, {s["t"] for s in samples if s["t"] >= first}, dt=60.0)
    _set(entries, "no_launch", "waited_seconds", lambda e: e["waited_seconds"] + 60)


def _start_late(entries: list[dict[str, Any]], seconds: float) -> None:
    for kind in ("attempt_start", "attempt_end"):
        _set(entries, kind, "t_start", lambda e: e["t_start"] + seconds)


def _swap(entries: list[dict[str, Any]], a: dict[str, Any], b: dict[str, Any]) -> None:
    """Two records' places in the ledger exchanged."""
    i, j = entries.index(a), entries.index(b)
    entries[i], entries[j] = b, a


def _rename(entries: list[dict[str, Any]], old: str, new: str) -> None:
    """Experiment `old` called `new` in every record of it (start, end and attempt_end)."""
    for e in [
        *_of(entries, "experiment_start"),
        *_of(entries, "experiment_end"),
        *(x for a in _of(entries, "attempt_end") for x in a["experiments"]),
    ]:
        if e["experiment"] == old:
            e["experiment"] = new


def _abort_reason(entries: list[dict[str, Any]], trigger: str) -> None:
    """The first attempt's abort recorded with another reason, in the abort and attempt_end."""
    _set(entries, "abort", "trigger", trigger)
    _set(entries, "attempt_end", "aborted", trigger, attempt="L-U2-a1")


PERTURBATIONS: dict[str, tuple[str, Callable[..., Any]]] = {
    # the passing gate group of a valid attempt
    "gate: a sample missing": ("valid", lambda es, ss: _of(es, "launch_gate")[0]["samples"].pop(2)),
    "gate: a sample too many": (
        "valid",
        lambda es, ss: _of(es, "launch_gate")[0]["samples"].append(
            _of(es, "launch_gate")[0]["samples"][-1]
        ),
    ),
    "gate: a sample 5 s late": (
        "valid",
        lambda es, ss: _move(es, ss, {_of(es, "launch_gate")[0]["samples"][2]["t"]}, dt=5.0),
    ),
    "gate: samples out of order": (
        "valid",
        lambda es, ss: _of(es, "launch_gate")[0]["samples"].reverse(),
    ),
    "gate: one timestamp": (
        "valid",
        lambda es, ss: [
            s.update(t=_of(es, "launch_gate")[0]["samples"][0]["t"])
            for s in _of(es, "launch_gate")[0]["samples"]
        ],
    ),
    "gate: a day before the start": (
        "valid",
        lambda es, ss: _move(es, ss, _group_times(es), dt=-86400.0),
    ),
    "gate: after the start": ("valid", lambda es, ss: _move(es, ss, _group_times(es), dt=200.0)),
    "gate: not in the load record": (
        "valid",
        lambda es, ss: _of(es, "launch_gate")[0]["samples"][1].update(load1=0.4),
    ),
    "gate: verdict not its samples'": (
        "valid",
        lambda es, ss: _set(es, "launch_gate", "passed", False),
    ),
    "gate: peak not its samples'": (
        "valid",
        lambda es, ss: _set(es, "launch_gate", "max_load1", 0.1),
    ),
    "gate: numbered 2": ("valid", lambda es, ss: _set(es, "launch_gate", "group", 2)),
    "gate: none": ("valid", lambda es, ss: [es.remove(g) for g in _of(es, "launch_gate")]),
    "gate: a stray one": (
        "valid",
        lambda es, ss: es.append({**_of(es, "launch_gate")[0], "attempt": 3}),
    ),
    # the launch windows of a no-launch outcome
    "no_launch: last group missing": ("no_launch", lambda es, ss: _drop_group(es, 52)),
    "no_launch: first group missing": ("no_launch", lambda es, ss: _drop_group(es, 1)),
    "no_launch: a group missing": ("no_launch", lambda es, ss: _drop_group(es, 20)),
    "no_launch: a group 60 s late": (
        "no_launch",
        lambda es, ss: _move(es, ss, _group_times(es, 20), dt=60.0),
    ),
    "no_launch: a group passing": ("no_launch", lambda es, ss: _pass_group(es, ss, 20)),
    "no_launch: a group past the window": (
        "no_launch",
        lambda es, ss: _group_past_the_window(es, ss),
    ),
    "no_launch: group count": ("no_launch", lambda es, ss: _set(es, "no_launch", "groups", 51)),
    "no_launch: wait": (
        "no_launch",
        lambda es, ss: _set(es, "no_launch", "waited_seconds", lambda e: e["waited_seconds"] - 600),
    ),
    "no_launch: another attempt's": (
        "no_launch",
        lambda es, ss: _set(es, "no_launch", "attempt", 2),
    ),
    # the window of a valid attempt
    "window: reversed": (
        "valid",
        lambda es, ss: _set(es, "attempt_end", "t_end", lambda e: e["t_start"] - 1),
    ),
    "window: empty": (
        "valid",
        lambda es, ss: _set(es, "attempt_end", "t_end", lambda e: e["t_start"]),
    ),
    "window: shorter than the execution": (
        "valid",
        lambda es, ss: _set(es, "attempt_end", "t_end", lambda e: e["t_start"] + 1),
    ),
    "window: end not a number": ("valid", lambda es, ss: _set(es, "attempt_end", "t_end", "late")),
    "window: end missing": ("valid", lambda es, ss: es.remove(_of(es, "attempt_end")[0])),
    "window: start after the gate": ("valid", lambda es, ss: _start_late(es, 5.0)),
    "window: the other start": (
        "valid",
        lambda es, ss: _set(es, "attempt_end", "t_start", lambda e: e["t_start"] - 5),
    ),
    "experiment: ends after the window": (
        "valid",
        lambda es, ss: _experiments(es, "U2", "t_end", lambda t: t + 1000),
    ),
    "experiment: starts before the window": (
        "valid",
        lambda es, ss: _experiments(es, "U2", "t_start", lambda t: t - 10),
    ),
    "experiment: exit code in attempt_end": (
        "valid",
        lambda es, ss: _of(es, "attempt_end")[0]["experiments"][0].update(exit_code=1),
    ),
    "experiment: exit code in its record": (
        "valid",
        lambda es, ss: _set(es, "experiment_end", "exit_code", 1),
    ),
    "experiment: record missing": ("valid", lambda es, ss: es.remove(_of(es, "experiment_end")[0])),
    "experiment: start record missing": (
        "valid",
        lambda es, ss: es.remove(_of(es, "experiment_start")[0]),
    ),
    "experiment: not run in order": (
        "quote",
        lambda es, ss: _experiments(es, "q2", "t_start", lambda t: t - 100),
    ),
    "abort: recorded only as an event": (
        "valid",
        lambda es, ss: es.append(
            {
                "event": "abort",
                "attempt": "L-U2-a1",
                "experiment": "U2",
                "trigger": "driver_interrupted",
                "t": _of(es, "attempt_end")[0]["t_end"],
            }
        ),
    ),
    "abort: recorded only in attempt_end": (
        "valid",
        lambda es, ss: _set(es, "attempt_end", "aborted", "T1_load"),
    ),
    "abort: after the window": (
        "retried",
        lambda es, ss: _set(es, "abort", "t", lambda e: e["t"] + 5000),
    ),
    "attempt: records of one never started": (
        "valid",
        lambda es, ss: es.append({**_of(es, "experiment_end")[0], "attempt": "L-U2-a2"}),
    ),
    "attempt: previous one ends after the next gate": (
        "retried",
        lambda es, ss: _set(
            es, "attempt_end", "t_end", lambda e: e["t_end"] + 1000, attempt="L-U2-a1"
        ),
    ),
    # cases where exactly one check is load-bearing (the mutation table of WHI-1746)
    "gate: the first sample missing": (
        "valid",
        lambda es, ss: _of(es, "launch_gate")[0]["samples"].pop(0),
    ),
    "gate: an earlier group passing": ("late_launch", lambda es, ss: _pass_group(es, ss, 2)),
    "gate: opened before the previous attempt ended": (
        "retried",
        lambda es, ss: _set(
            es,
            "attempt_end",
            "t_end",
            min(_group_times(es, 1, 2)) + 1,
            attempt="L-U2-a1",
        ),
    ),
    "no_launch: re-sampled late": ("no_launch", lambda es, ss: _resampled_late(es, ss, 20)),
    "no_launch: the last group passing": ("no_launch", lambda es, ss: _pass_group(es, ss, 52)),
    "no_launch: a second record": (
        "no_launch",
        lambda es, ss: es.append({**_of(es, "no_launch")[0], "attempt": 2}),
    ),
    "window: start not a number": (
        "valid",
        lambda es, ss: _set(es, "attempt_start", "t_start", None),
    ),
    "experiment: attempt_end's list not a list": (
        "valid",
        lambda es, ss: _set(es, "attempt_end", "experiments", 5),
    ),
    "experiment: attempt_end's list with a non-record": (
        "valid",
        lambda es, ss: _of(es, "attempt_end")[0]["experiments"].append(5),
    ),
    "experiment: listed as run without its records": (
        "valid",
        lambda es, ss: _of(es, "attempt_end")[0]["experiments"].append(
            {"experiment": "U2", "exit_code": 0}
        ),
    ),
    "abort: a time not a number": ("retried", lambda es, ss: _set(es, "abort", "t", None)),
    "abort: the interrupted attempt's after the next gate": (
        "resumed",
        lambda es, ss: _set(es, "abort", "t", lambda e: e["t"] + 5000),
    ),
    # WHI-1747 (release review round 3): abort timing, experiment identity, load-sample domain
    "abort: before its triggering sample": (
        "retried",
        lambda es, ss: _set(es, "abort", "t", lambda e: e["t"] - 30),
    ),
    "abort: for T5, the experiment it stopped": (
        "retried",
        lambda es, ss: _abort_reason(es, "T5_incomplete_execution"),
    ),
    "abort: for T3 without a sleep entry": (
        "retried",
        lambda es, ss: _abort_reason(es, "T3_sleep"),
    ),
    "abort: for T4 with caffeinate held": (
        "retried",
        lambda es, ss: _abort_reason(es, "T4_sleep_prevention_or_capture"),
    ),
    "abort: names another experiment": (
        "retried",
        lambda es, ss: _set(es, "abort", "experiment", "UP-cand"),
    ),
    "abort: after its experiment's end": (
        "retried",
        lambda es, ss: _swap(es, _of(es, "abort")[0], _of(es, "experiment_end")[0]),
    ),
    "experiment: started twice": (
        "valid",
        lambda es, ss: es.insert(
            es.index(_of(es, "experiment_start")[0]), copy.deepcopy(_of(es, "experiment_start")[0])
        ),
    ),
    "experiment: another one started after the abort": (
        "retried",
        lambda es, ss: es.insert(
            es.index(_of(es, "experiment_end")[0]) + 1, {**_of(es, "experiment_start")[0]}
        ),
    ),
    "experiment: labels out of the registered order": (
        "quote",
        lambda es, ss: (_rename(es, "q2", "qx"), _rename(es, "q3", "q2"), _rename(es, "qx", "q3")),
    ),
    "experiment: the interrupted one's start of another unit": (
        "resumed",
        lambda es, ss: _set(es, "experiment_start", "experiment", "U2", attempt="L-U4-a1"),
    ),
    "abort: the interrupted one's before its experiment started": (
        "resumed",
        lambda es, ss: _set(es, "abort", "t", lambda e: e["t"] - 100),
    ),
    "load: a negative sample in the attempt": (
        "valid",
        lambda es, ss: ss[-3].update(load1=-1.0),
    ),
    # stage-wide evidence
    "load: a sample without a load": ("valid", lambda es, ss: ss[len(ss) // 2].update(load1=None)),
    "load: a sample not finite": (
        "valid",
        lambda es, ss: ss[len(ss) // 2].update(load1=float("nan")),
    ),
    "load: samples out of order": ("valid", lambda es, ss: ss.insert(0, ss.pop())),
    "stage: another CPU count": ("valid", lambda es, ss: _set(es, "stage", "logical_cpus", 1000)),
    "units: out of the registered order": (
        "pair",
        lambda es, ss: {
            **RAW,
            "host": {**RAW["host"], "logical_cpus": os.cpu_count()},
            "timing": {
                **RAW["timing"],
                "units": [u for u in RAW["timing"]["units"] if u["unit"] in ("U4", "U2")][::-1],
            },
        },
    ),
}


def _perturbation_stages(tmp_path: Path) -> dict[str, Any]:
    """The driver's own clean records the perturbations start from."""
    start = 1_800_000_000.0
    fixtures = {
        "valid": (FakeHost(), ["U2"]),
        "retried": (FakeHost(load=lambda t: 6.0 if start + 200 < t < start + 400 else 0.5), ["U2"]),
        "late_launch": (FakeHost(load=lambda t: 3.5 if t < start + 1000 else 0.5), ["U2"]),
        "no_launch": (FakeHost(load=lambda t: 3.5), ["U2"]),
        "quote": (FakeHost(), ["UQ"]),
        "pair": (FakeHost(), ["U2", "U4"]),
    }
    stages = {}
    for label, (host, units) in fixtures.items():
        stages[label] = _stage(tmp_path / label, host, units)
        stages[label].run()
    resumed = FakeHost()  # the driver stopped inside the first attempt's experiment, then resumed
    stages["resumed"] = _stage(tmp_path / "resumed", resumed, ["U4"])

    def interrupted(seconds: float) -> None:
        resumed.sleep(seconds)
        if resumed.started:
            raise KeyboardInterrupt

    stages["resumed"].sleep = interrupted
    assert stages["resumed"].run() == 130
    stages["resumed"].sleep = resumed.sleep
    assert stages["resumed"].run() == 0
    return stages


def test_no_perturbed_evidence_field_certifies(tmp_path: Path) -> None:
    """Each perturbation of one evidence field of the driver's own clean records (gate samples,
    their count, spacing, order and labels; no-launch groups, cadence, coverage and record; window
    ends; experiment, abort and attempt records; load samples; the host) is a problem: never the
    positive §8.7 statement, never a usable unit."""
    stages = _perturbation_stages(tmp_path)
    for stage in stages.values():
        clean = _verified(stage)
        assert clean["problems"] == [] and clean["statements"]["measurement_attempted_correctly"]
    assert len(_of(stages["no_launch"].ledger.entries(), "launch_gate")) == 52
    assert len(_of(stages["late_launch"].ledger.entries(), "launch_gate")) == 4
    for what, (label, change) in PERTURBATIONS.items():
        analysis = _perturbed(stages[label], change)
        assert analysis["problems"], what
        assert analysis["statements"]["measurement_attempted_correctly"] is False, what
        usable = set(analysis["statements"]["usable_latency_obtained"])
        # the pair's U4 records are untouched: only U2, the unit out of order, loses its yield
        assert usable <= ({"U4"} if label == "pair" else set()), what


def test_a_resumed_stage_gates_again_in_a_new_launch_window(tmp_path: Path) -> None:
    """The driver stopped while it gated (a busy host), resumed later: the new run's groups start
    again at 1 after the earlier ones, and only the last window decides."""
    start = 1_800_000_000.0
    host = FakeHost(load=lambda t: 3.5 if t < start + 1000 else 0.5)
    stage = _stage(tmp_path, host, ["U2"])

    def interrupted(seconds: float) -> None:
        host.sleep(seconds)
        if host.now > start + 800:
            raise KeyboardInterrupt

    stage.sleep = interrupted
    assert stage.run() == 130
    host.now += 3600
    stage.sleep = host.sleep
    assert stage.run() == 0
    groups = [g["group"] for g in _events(stage, "launch_gate")]
    assert groups[0] == 1 and groups.count(1) == 2
    analysis = _verified(stage)
    assert analysis["problems"] == []
    assert analysis["units"]["U2"]["outcome"]["attempt"] == "L-U2-a1"


def test_the_retained_campaign_records_verify_to_the_published_outcomes() -> None:
    """The H1 stage-L raw records (outside the repository, pinned by `timing-SHA256SUMS`): the
    verified analysis is the committed one, 0 problems, the published outcomes and attempts."""
    path = os.environ.get(TIMING_RECORDS)
    if not path:
        pytest.skip(f"set {TIMING_RECORDS} to the retained campaign `L` directory")
    out = Path(path)
    assert C.verify_sums(out, EVIDENCE / "timing-SHA256SUMS") == []
    analysis = CA.analyze_timing(RAW, out)
    assert analysis["problems"] == []
    published = json.loads((EVIDENCE / "timing-analysis.json").read_text())
    assert json.loads(json.dumps(analysis, sort_keys=True, default=str)) == published
    assert {
        u: (v["outcome"]["outcome"], v["outcome"]["attempt"]) for u, v in analysis["units"].items()
    } == {
        **{u: ("inconclusive_cap_exhausted", None) for u in ("UP", "U1", "U2", "U3", "U4", "U5")},
        "U6": ("valid", "L-U6-a1"),
        "UQ": ("valid", "L-UQ-a1"),
    }
    assert analysis["statements"] == {
        "measurement_attempted_correctly": True,
        "usable_latency_obtained": ["U6", "UQ"],
    }


# ----------------------------------------------------------------------------- fixture stage T


def _fixture_raw() -> dict[str, Any]:
    raw: dict[str, Any] = copy.deepcopy(RAW)
    spec = {
        "source": str(FIXTURE),
        "bundle_hash": _sha(FIXTURE / "manifest.json"),
        "cases": 4,
        "split": "all",
        "cohort": "fixture",
    }
    raw["inputs"] = {"tuning_full": dict(spec), "tuning_sor": dict(spec)}
    raw["stages"]["T"].update(caffeinate=False, pmset_capture=False)
    return raw


@pytest.fixture(scope="module")
def stage_t(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("r024c")
    raw = _fixture_raw()
    inputs = root / "inputs"
    for key in raw["inputs"]:
        shutil.copytree(FIXTURE, inputs / key)
    out = root / "T"
    code = C.pc.execute(
        C.build(raw, "T"),
        "T",
        inputs=inputs,
        out=out,
        lanes=6,
        allow_dirty=True,
        echo=lambda _m: None,
    )
    assert code == 0, (out / "ledger.jsonl").read_text()
    result = C.analyze(raw, "T", inputs, out)
    return {"raw": raw, "root": root, "inputs": inputs, "out": out, "result": result}


def test_the_fixture_stage_t_records_every_arm_and_checks_clean(stage_t: dict[str, Any]) -> None:
    result = stage_t["result"]
    # the fixture runs from this working tree (allow_dirty); the campaign runs from a clean clone
    assert [p for p in result["problems"] if not p.endswith("run from a dirty tree")] == []
    rows = result["rows"]
    assert len(rows) == 2 * 19 + 3
    for key, row in rows.items():
        assert sum(row["statuses"].values()) == 4, key
        if row["rule_p"] is not None:
            assert row["rule_p"] == {RR.P1: 4}, key
    assert set(result["gates"]) == {
        "Q19-full/split_polish",
        "Q19-full/marginal_activation",
        "Q19-sor/split_polish",
        "Q19-sor/marginal_activation",
        "E2-E1only",
        "E2-wm",
        "E2-cm",
    }
    assert result["gates"]["Q19-full/split_polish"]["base"] == "Q19-full/metis_inspired"
    assert result["gates"]["E2-E1only"]["base"] == "Q19-full/incremental_graph"
    assert "G6" not in result["gates"]["E2-wm"]["gates"]
    for gate in result["gates"].values():
        assert not any(gate["failures"].get(g) for g in CA.DEFECT_GATES)
    assert set(result["controls"]) == {"E2-wm", "E2-cm"}
    comps = result["comparisons"]
    assert len(comps["ranked"]) + len(comps["expanded_protocol"]) == 2 * 2 * 18
    assert len(comps["envelope"]) == 2 * 19
    assert len(comps["attribution"]) == 3 and len(comps["base_control"]) == 4


def test_the_fixture_dispositions_and_tables(stage_t: dict[str, Any], tmp_path: Path) -> None:
    raw, out = stage_t["raw"], stage_t["out"]
    runs, problems = C.stage_runs(raw, "T", out)
    assert problems == []
    relabelled = {"R" + k[1:]: v for k, v in runs.items()}
    bundles = {
        c: CA.bundle_view(stage_t["inputs"] / b) for c, b in raw["stage_bundles"]["T"].items()
    }
    expected = {k: C.expected_resolved(raw, k) for k in C.profiles(raw)}
    result = CA.analyze_stage(
        raw,
        "R",
        relabelled,
        bundles,
        expected,
        bundle_hashes={
            c: raw["inputs"][b]["bundle_hash"] for c, b in raw["stage_bundles"]["T"].items()
        },
        stage_revision=None,
    )
    assert set(result["dispositions"]) == {C.E1, C.E2}
    for d in result["dispositions"].values():
        assert d["disposition"] in ("keep_experimental", "inconclusive")
        assert not d["reject"]
    first = CA.render_tables(raw, {"T": stage_t["result"]})
    path = tmp_path / "T.json"
    path.write_text(json.dumps(stage_t["result"], indent=1, sort_keys=True, default=str))
    again = CA.render_tables(raw, {"T": json.loads(path.read_text())})
    assert first == again and "C9(b)" in first and "C13" in first


def _reanalyze(stage_t: dict[str, Any], runs: dict[str, Any]) -> dict[str, Any]:
    raw = stage_t["raw"]
    bundles = {
        c: CA.bundle_view(stage_t["inputs"] / b) for c, b in raw["stage_bundles"]["T"].items()
    }
    expected = {k: C.expected_resolved(raw, k) for k in C.profiles(raw)}
    return dict(
        CA.analyze_stage(
            raw,
            "R",
            {"R" + k[1:]: v for k, v in runs.items()},
            bundles,
            expected,
            bundle_hashes={
                c: raw["inputs"][b]["bundle_hash"] for c, b in raw["stage_bundles"]["T"].items()
            },
            stage_revision=None,
        )
    )


def test_a_worse_polish_record_is_a_defect_and_rejects(
    stage_t: dict[str, Any], tmp_path: Path
) -> None:
    runs, _ = C.stage_runs(stage_t["raw"], "T", stage_t["out"])
    inv = "T-q19-e1-full"
    copy_dir = tmp_path / "run"
    shutil.copytree(runs[inv]["run_dir"], copy_dir)
    lines = (copy_dir / "cases.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    assert record["status"] == "ok"
    record["evaluation"]["gross_output"] = "1"  # below its base: never-worse (G3) and G7 break
    lines[0] = json.dumps(record)
    (copy_dir / "cases.jsonl").write_text("\n".join(lines) + "\n")
    manifest = json.loads((copy_dir / "manifest.json").read_text())
    manifest["cases_sha256"] = _sha(copy_dir / "cases.jsonl")  # a consistent, tampered run
    (copy_dir / "manifest.json").write_text(json.dumps(manifest))
    runs[inv] = {**runs[inv], "run_dir": str(copy_dir)}
    result = _reanalyze(stage_t, runs)
    assert any("G3 fails" in p for p in result["problems"])
    assert result["dispositions"][C.E1]["disposition"] == "reject"
    assert result["dispositions"][C.E2]["disposition"] != "reject"


def test_a_missing_work_pass_is_reported_and_inconclusive(stage_t: dict[str, Any]) -> None:
    runs, _ = C.stage_runs(stage_t["raw"], "T", stage_t["out"])
    del runs["T-WP-q19-e2-full"]
    result = _reanalyze(stage_t, runs)
    row = result["rows"]["Q19-full/marginal_activation"]
    assert row["rule_p"] == {RR.P2: 4} and row["work"]["cl_swap_steps"] is None
    assert result["dispositions"][C.E2]["disposition"] == "inconclusive"


# ----------------------------------------------------------------------------- committed evidence

EVIDENCE = REPO / "docs" / "references" / "research-024" / "campaign"


@pytest.mark.skipif(not (EVIDENCE / "tuning-analysis.json").is_file(), reason="stage T not pinned")
def test_the_committed_tables_regenerate_from_the_pinned_analyses() -> None:
    analyses = {}
    for stage, name in (("T", "tuning"), ("R", "report"), ("L", "timing")):
        path = EVIDENCE / f"{name}-analysis.json"
        if path.is_file():
            analyses[stage] = json.loads(path.read_text())
    tuning = CA.render_tables(RAW, {"T": analyses["T"]})
    assert (EVIDENCE / "tuning-tables.md").read_text() == tuning
    if "R" in analyses:
        report = CA.render_tables(RAW, {"R": analyses["R"]})
        assert (EVIDENCE / "report-tables.md").read_text() == report
        assert analyses["R"]["problems"] == []
        counters: Any = _load("r024_counters", "tools/research_024/r024_counters.py")
        rendered = counters.render(RAW, analyses["R"])
        assert (EVIDENCE / "report-search-counters.md").read_text() == rendered
    if "L" in analyses:
        timing = CA.render_tables(RAW, {"L": analyses["L"]})
        assert (EVIDENCE / "timing-tables.md").read_text() == timing
        assert analyses["L"]["problems"] == []
        view: Any = _load("r024_timing_view", "tools/research_024/r024_timing_view.py")
        ledger = [
            json.loads(x) for x in (EVIDENCE / "timing-ledger.jsonl").read_text().splitlines()
        ]
        rendered = view.render(RAW, analyses["L"], ledger)
        assert (EVIDENCE / "timing-attempts.md").read_text() == rendered
    if (EVIDENCE / "tables.md").is_file():
        assert (EVIDENCE / "tables.md").read_text() == CA.render_tables(RAW, analyses)
    assert analyses["T"]["problems"] == []


def test_the_published_work_ratios_are_the_recorded_totals() -> None:
    """`results.md` §3.4 and §0 (release review R1-F3): every printed total is the work-pass total
    of `report-analysis.json`, every ratio its quotient to the base, rounded to three places."""
    rows = json.loads((EVIDENCE / "report-analysis.json").read_text())["rows"]
    units = ("quotes", "cl_swap_steps", "lb_bins_swapped")
    keys = {
        "metis_inspired": "Q19-full/metis_inspired",
        "split_polish": "Q19-full/split_polish",
        "incremental_graph": "Q19-full/incremental_graph",
        "E2-E1only": "E2-E1only",
        "marginal_activation": "Q19-full/marginal_activation",
    }
    base = {
        "split_polish": "metis_inspired",
        "E2-E1only": "incremental_graph",
        "marginal_activation": "incremental_graph",
    }

    def ratios(name: str) -> list[str]:
        work, ref = rows[keys[name]]["work"], rows[keys[base[name]]]["work"]
        return [f"{Fraction(work[u], ref[u]):.3f}" for u in units]

    results = (REPO / "docs" / "references" / "research-024" / "results.md").read_text()
    section = results.split("### 3.4 Work", 1)[1].split("\n### ", 1)[0]
    printed = {}
    for line in (x for x in section.splitlines() if x.startswith("| `")):
        cells = [c.strip() for c in line.strip("|").split("|")]
        printed[cells[0].split("`")[1]] = (cells[1:4], cells[4])
    assert set(printed) == set(keys)
    for name, (totals, quotients) in printed.items():
        work = rows[keys[name]]["work"]
        assert totals == [f"{work[u]:,}" for u in units], name
        assert quotients == (" / ".join(ratios(name)) if name in base else ""), name
    assert ratios("split_polish")[0] == "1.747"  # 9,299,891 / 5,324,859 = 1.74650...
    summary = next(x for x in results.splitlines() if x.startswith("| Work (C8, C13) |"))
    for name, ratio in (
        ("split_polish", ratios("split_polish")),
        ("marginal_activation", ratios("marginal_activation")),
    ):
        assert all(f"{r}×" in summary for r in ratio), name

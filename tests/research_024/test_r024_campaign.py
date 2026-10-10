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

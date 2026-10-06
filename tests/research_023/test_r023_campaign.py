"""WHI-1627 research_023 campaign driver: schedule check, end-to-end fixture campaign, audits.

- The committed schedule checks clean: profiles equal their renderings, pins hold, every E1/E2
  arm's base configuration equals its reference arm's, every E2 treatment has both controls.
- `check` refuses a profile whose base drifts from its reference arm, and profile drift itself.
- A fixture campaign (the tracked `mantle_mixed` bundle in place of the corpus split) runs every
  registered arm and work pass of stage T through the real executor; the analysis finds every
  arm x case recorded, every audit clean, the tables regenerate byte-identically.
- Each audit rule fails when its rule is broken in a record (base row, ledger, never-worse,
  refusal, gate status, work target, call count, embedded activation, completeness), and a
  "not reached: E1 truncated" row stays in the denominators but is never a matched comparison.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

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


C: Any = _load("r023_campaign", "tools/research_023/campaign.py")
RA: Any = sys.modules["r023_analysis"]


def _sha(path: Path) -> str:
    return str(C.sha256_bytes(path.read_bytes()))


def test_the_committed_schedule_checks_clean() -> None:
    raw = C.load_raw()
    assert C.check(raw) == []
    arms = C.arms(raw)
    assert len(arms) == 23 and set(raw["bases"]) == {"A0", "C100", "C200", "M4", "S4", "REP", "PS"}
    e1 = {s["base"] for s in arms.values() if s["kind"] == "e1" and s["solver"] == "brent"}
    assert e1 == {"A0", "C100", "M4", "S4", "REP", "PS"}  # contract §8 E1-b
    assert [k for k, s in arms.items() if s["kind"] == "e1" and s["solver"] == "golden"] == [
        "E1g-A0"
    ]
    e2 = {(s["base"], s["mode"], s["e2_arm"]) for s in arms.values() if s["kind"] == "e2"}
    assert e2 == {(b, m, a) for b, m in (("A0", "pf"), ("C100", "pf"), ("A0", "full"))
                  for a in ("treatment", "work_matched", "call_matched")}  # fmt: skip
    ids = [i["id"] for i in C.invocations(raw, "R")]
    assert len(ids) == 23 + 17 and len(set(ids)) == len(ids)  # no work pass for the 6 controls
    assert raw["polish"] == {"solver": "brent", "rounds": 2, "tolerance": 0.0001,
                             "grid": 10**9, "maxiter": 60}  # contract §4.7  # fmt: skip


def test_check_refuses_a_base_that_differs_from_its_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = C.load_raw()
    raw["profile_dir"] = str(tmp_path)
    real = C.arm_document

    def drifted(raw_: Any, key: str) -> dict[str, Any]:
        doc = real(raw_, key)
        if key == "E1b-S4":  # a renderer bug: the base runs a non-preset option
            doc["algorithm_options"]["split_polish"]["base_options"]["max_frontier_labels"] = 7
        if key == "E1b-M4":
            doc["graph"]["label_hops"] = 3
        return dict(doc)

    monkeypatch.setattr(C, "arm_document", drifted)
    for key in C.arms(raw):
        (tmp_path / Path(C.profile_path(raw, key)).name).write_text(C.render_profile(raw, key))
    problems = C.check(raw)
    assert any(p.startswith("arm E1b-S4: base options") for p in problems), problems
    assert any(p.startswith("arm E1b-M4: base params") for p in problems), problems
    assert len(problems) == 2
    (tmp_path / "a0.yaml").write_text((tmp_path / "a0.yaml").read_text() + "# edited\n")
    assert any("a0.yaml differs from its rendering" in p for p in C.check(raw))


# ----------------------------------------------------------------------------- fixture campaign


@pytest.fixture(scope="module")
def stage(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Stage T of the committed schedule over the fixture bundle, every arm and work pass."""
    root = tmp_path_factory.mktemp("r023")
    raw = copy.deepcopy(C.load_raw())
    raw["inputs"] = {"fixture": {
        "source": str(FIXTURE), "bundle_hash": _sha(FIXTURE / "manifest.json"),
        "cases_sha256": _sha(FIXTURE / "cases.jsonl"), "cases": 4, "split": "all",
        "cohort": "fixture",
    }}  # fmt: skip
    raw["stages"]["T"].update(split="fixture", caffeinate=False, pmset_capture=False)
    inputs = root / "inputs"
    shutil.copytree(FIXTURE, inputs / "fixture")
    out = root / "T"
    code = C.pc.execute(C.build(raw), "T", inputs=inputs, out=out, lanes=6, allow_dirty=True,
                        echo=lambda _m: None)  # fmt: skip
    assert code == 0, (out / "ledger.jsonl").read_text()
    result = C.analyze(raw, "T", inputs, out)
    return {"raw": raw, "inputs": inputs, "out": out, "result": result}


def test_the_fixture_campaign_records_every_arm_and_audits_clean(stage: dict[str, Any]) -> None:
    result = stage["result"]
    assert result["problems"] == []
    assert len(result["statuses"]) == 23 and len(result["physical"]) == 17
    for counts in result["statuses"].values():
        assert counts["scheduled"] == 4 and sum(counts["statuses"].values()) == 4
    for key, audit in result["base_rows"].items():
        assert audit["compared"] == 4 and audit["differences"] == [], key
    for key, gate in result["gates"].items():
        assert gate["checked"]["ledger"] == gate["checked"]["never_worse"] == 4, key
    for audit in result["controls"].values():
        assert audit["rows"] == audit["matched"] == 4 and audit["not_reached"] == 0
    assert {d["disposition"] for d in result["dispositions"].values()} == {"keep_experimental"}
    assert sum(v["improved"] for v in result["identity"].values()) > 0
    assert len(result["raw_sha256"]) == 2 * 40 and len(result["git_revisions"]) == 1


def test_the_tables_regenerate_identically_from_the_analysis(
    stage: dict[str, Any], tmp_path: Path
) -> None:
    path = tmp_path / "analysis.json"
    path.write_text(json.dumps(stage["result"], indent=1, sort_keys=True) + "\n")
    tables = RA.render_tables(json.loads(path.read_text()))
    assert tables == RA.render_tables(json.loads(path.read_text()))
    for needle in ("`E1b-CFMM`", "not_implemented", "Q1", "Q3", "Q4", "Q5", "`E2pf-A0-wm`"):
        assert needle in tables
    sums = tmp_path / "SHA256SUMS"
    sums.write_text("".join(f"{d}  {n}\n" for n, d in stage["result"]["raw_sha256"].items()))
    assert C.verify_sums(stage["out"], sums) == []


# ----------------------------------------------------------------------------- audit rules


def _arms(stage: dict[str, Any]) -> dict[str, Any]:
    from report.aggregate import load_run

    runs, _ = C.stage_runs(stage["raw"], "T", stage["out"])
    out = {}
    for key, spec in C.arms(stage["raw"]).items():
        run = load_run(runs[f"T-{key}"]["run_dir"], bundle_dirs=[stage["inputs"] / "fixture"])
        out[key] = RA.r021.arm_from_run(run, spec["algorithm"], key)
    return out


def _edit(arm: Any, case_id: str, change: Any) -> Any:
    """`arm` with one record replaced by `change(copy of the record)`."""
    cell = arm.cell(case_id)
    record = copy.deepcopy(dict(cell.record))
    change(record)
    cells = dict(arm.cells)
    gross = RA.gross(record)
    cells[case_id] = replace(cell, record=record, status=record["status"], gross=gross)
    return replace(arm, cells=cells)


def test_each_audit_fails_when_its_rule_is_broken(stage: dict[str, Any]) -> None:
    arms = _arms(stage)
    ref, e1 = arms["A0"], arms["E1b-A0"]
    case = e1.case_ids[0]
    assert RA.base_audit(ref, e1)["differences"] == []

    def base_quotes(r: dict[str, Any]) -> None:
        r["search"]["base"]["quotes"] += 1

    def base_search(r: dict[str, Any]) -> None:
        r["search"]["base"]["search"]["chunks"] = -1

    def base_gross(r: dict[str, Any]) -> None:
        r["search"]["split_polish"]["base_gross"] = "1"

    for change, field in ((base_quotes, "quotes"), (base_search, "search"),
                          (base_gross, "base_gross")):  # fmt: skip
        found = RA.base_audit(ref, _edit(e1, case, change))["differences"]
        assert found == [{"case_id": case, "field": field}]

    def killed(r: dict[str, Any]) -> None:
        r["search"], r["status"] = {}, "timeout"

    assert RA.base_audit(ref, _edit(e1, case, killed))["differences"][0]["field"].startswith(
        "status"
    )

    def seam(r: dict[str, Any]) -> None:
        r["quotes"]["counted"] += 1

    def worse(r: dict[str, Any]) -> None:
        r["evaluation"]["gross_output"] = str(int(r["search"]["split_polish"]["base_gross"]) - 1)

    def refused_but_changed(r: dict[str, Any]) -> None:
        r["search"]["split_polish"]["scope"] = "unsupported_topology"
        r["evaluation"]["gross_output"] = str(int(r["search"]["split_polish"]["base_gross"]) + 1)

    def error(r: dict[str, Any]) -> None:
        r["status"] = "invalid_plan"

    for change, name in ((seam, "ledger"), (worse, "never_worse"),
                         (refused_but_changed, "refusal"), (error, "gate_status")):  # fmt: skip
        failures = RA.gate_audit(ref, _edit(e1, case, change), RA.E1)["failures"]
        assert failures[name] == [case], (name, failures)
        assert sum(len(v) for v in failures.values()) == 1, (name, failures)

    treatment, wm, cm = arms["E2pf-A0"], arms["E2pf-A0-wm"], arms["E2pf-A0-cm"]
    clean = RA.control_audit(treatment, wm)
    assert clean["over_work_target"] == clean["embedded_activation_differs"] == []

    def over(r: dict[str, Any]) -> None:
        block = r["search"]["marginal_activation"]["control"]
        block["quotes"] = int(block["target_quotes"]) + 1

    def calls(r: dict[str, Any]) -> None:
        block = r["search"]["marginal_activation"]["control"]
        block["calls_started"], block["stop"] = block["target_calls"] + 1, None

    def embedded(r: dict[str, Any]) -> None:
        r["search"]["marginal_activation"]["activation"]["quotes"] += 1

    assert RA.control_audit(treatment, _edit(wm, case, over))["over_work_target"] == [case]
    assert RA.control_audit(treatment, _edit(cm, case, calls))["call_count_mismatch"] == [case]

    def target(r: dict[str, Any]) -> None:
        r["search"]["marginal_activation"]["control"]["target_calls"] += 1

    audit = RA.control_audit(treatment, _edit(cm, case, target))
    assert audit["target_differs_from_treatment"] == [case]
    audit = RA.control_audit(treatment, _edit(wm, case, embedded))
    assert audit["embedded_activation_differs"] == [case]
    disposition = RA.disposition(["E2pf-A0-wm"], {"E2pf-A0-wm": {"failures": {}}},
                                 {"E2pf-A0-wm": {"differences": []}},
                                 {"E2pf-A0-wm": RA.control_audit(treatment, _edit(wm, case, over))},
                                 {"E2pf-A0-wm": {"scheduled": 4, "truncated_by": {}, "scope": {}}},
                                 {"E2pf-A0-wm": {"statuses": {"ok": 4}}})  # fmt: skip
    assert disposition["disposition"] == "reject"


def test_a_not_reached_row_stays_in_the_denominators_and_is_never_matched(
    stage: dict[str, Any],
) -> None:
    arms = _arms(stage)
    treatment, control = arms["E2pf-A0"], arms["E2pf-A0-wm"]
    case = control.case_ids[1]

    def truncated(r: dict[str, Any]) -> None:
        block = r["search"]["marginal_activation"]
        block["not_reached"] = RA.NOT_REACHED
        block.pop("control"), block.pop("activation")

    def killed(r: dict[str, Any]) -> None:
        r["status"], r["search"], r["evaluation"] = "timeout", {}, None

    hard = RA.control_audit(treatment, _edit(control, case, killed))
    assert (hard["matched"], hard["unmatched_status"], hard["no_control_block"]) == (
        3,
        {"timeout": 1},
        [],
    )  # an outcome, kept and never matched; not an audit problem
    cut = _edit(control, case, truncated)
    audit = RA.control_audit(treatment, cut)
    assert (audit["rows"], audit["not_reached"], audit["matched"]) == (4, 1, 3)
    before = RA.comparison("Q3", control, treatment)
    after = RA.comparison("Q3", cut, treatment)
    assert after["scheduled"] == before["scheduled"] == 4
    assert after["common_ok"] == before["common_ok"] - 1
    assert after["transitions"].get("not_reached->ok") == 1


def test_completeness_reports_a_missing_case(stage: dict[str, Any]) -> None:
    from report.aggregate import load_run

    runs, problems = C.stage_runs(stage["raw"], "T", stage["out"])
    assert problems == []
    run = load_run(runs["T-A0"]["run_dir"])
    expected = list(run.case_ids)
    assert RA.completeness("T-A0", run, "incremental_graph", expected) == []
    assert RA.completeness("T-A0", run, "incremental_graph", [*expected, "extra"])
    del run.rows[(expected[0], "incremental_graph")]
    run.rows[(expected[0], "incremental_graph")] = replace(
        run.row(expected[1], "incremental_graph"), case_id=expected[0], status="missing"
    )
    assert any("without a record" in p for p in RA.completeness("T-A0", run, "incremental_graph",
                                                                expected))  # fmt: skip


def test_the_schedule_yaml_is_the_only_source_of_the_arm_list() -> None:
    raw = yaml.safe_load((REPO / "config" / "research_023" / "schedule.yaml").read_text())
    names = {Path(C.profile_path(raw, k)).name for k in C.arms(raw)}
    assert names == {p.name for p in (REPO / "config" / "research_023" / "profiles").glob("*.yaml")}


PUBLISHED = REPO / "docs" / "references" / "research-023" / "campaign"


@pytest.mark.parametrize("split", ["tuning", "report"])
def test_published_tables_regenerate_from_the_published_analysis(split: str) -> None:
    """`<split>-tables.md` is exactly `tables` over `<split>-analysis.json`, and the analysis pins
    the raw records exactly as `<split>-SHA256SUMS` lists them (raw records: artifacts root)."""
    analysis_path = PUBLISHED / f"{split}-analysis.json"
    if split == "report" and not analysis_path.exists():
        pytest.skip("the report stage is published by the results commit")
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    assert RA.render_tables(analysis) == (PUBLISHED / f"{split}-tables.md").read_text()
    sums = (PUBLISHED / f"{split}-SHA256SUMS").read_text(encoding="utf-8")
    assert sums == "".join(f"{d}  {n}\n" for n, d in analysis["raw_sha256"].items())
    assert analysis["problems"] == [] and len(analysis["git_revisions"]) == 1
    assert analysis["stage"] == {"tuning": "T", "report": "R"}[split]

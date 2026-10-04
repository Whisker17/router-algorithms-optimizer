"""The published numbers of `docs/references/research-022/results.md`, against the evidence.

Offline and unskipped: every figure the results page states is recomputed from the compact analyses
under `docs/references/research-022/campaign/` (the committed copies of the analyses of the
registered stages), the schedule and the freeze record. A page that drifts from its evidence, an
evidence file that changed, or a schedule edited after the freeze fails here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs" / "references" / "research-022"
CAMPAIGN = DOCS / "campaign"
RESULTS = (DOCS / "results.md").read_text(encoding="utf-8")
STRAT = {"single_path": "single_path_bounded", "incremental_graph": "incremental_graph_bounded",
         "metis_history": "metis_history_bounded"}  # fmt: skip
GROUP = {"single_path": "sp", "incremental_graph": "ig", "metis_history": "mh"}


def _load(name: str) -> ModuleType:
    path = REPO / "tools" / "research_022" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


C: Any = _load("pruning_campaign")


def analysis(name: str) -> dict[str, Any]:
    out: dict[str, Any] = json.loads((CAMPAIGN / name).read_text(encoding="utf-8"))
    return out


T, R, M = analysis("tuning-analysis.json"), analysis("report-analysis.json"), analysis(
    "binding-budget-analysis.json")  # fmt: skip
I, L = analysis("cli-analysis.json"), analysis("timing-analysis.json")  # noqa: E741
SCHEDULE = yaml.safe_load((REPO / "config/research_022/schedule.yaml").read_text())


def f(x: int) -> str:
    return f"{x:,}"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ----------------------------------------------------------------------------- evidence integrity


def test_every_registered_stage_reconciled_with_no_problem() -> None:
    for name, stage in (("T", T), ("R", R), ("M", M), ("I", I), ("L", L)):
        assert stage["stage"] == name and stage["reconciled"] is True, name
        assert stage["reconciliation_problems"] == [], name
    assert (len(T["invocations"]), len(R["invocations"]), len(M["invocations"])) == (18, 32, 24)
    assert len(I["invocations"]) == 12 and len(L["invocations"]) == 8
    for stage in (T, R, M, I, L):
        for inv_id, view in stage["invocations"].items():
            assert view["result"] == "ok", inv_id
            if "git_revision" in view:  # every run is from the clean commit its stage names
                assert view["git_revision"] == stage["stage_revision"], inv_id


def test_the_schedule_is_the_frozen_one_and_stages_ran_from_the_registered_commits() -> None:
    freeze = json.loads((CAMPAIGN / "freeze.json").read_text())
    schedule_path = REPO / "config/research_022/schedule.yaml"
    assert freeze["files"]["schedule"]["sha256"] == sha256(schedule_path)
    for key, pin in freeze["files"].items():
        if key.startswith(("profile:", "latency:")):
            assert sha256(REPO / pin["path"]) == pin["sha256"], key
    assert T["stage_revision"] == "93a9db52f128594b0411e07a3fb1e23fbc1ef955"
    s2 = "46d6e2e628b2768caab124986c3ce50d3c2f891d"
    stages = (R, M, I, L)
    assert {stage["stage_revision"] for stage in stages} == {s2}
    for sha in ("93a9db52f128594b0411e07a3fb1e23fbc1ef955", s2):
        assert sha in RESULTS
    assert freeze["a5"]["values"] == SCHEDULE["a5"]["values"]  # the freeze commit's A5 values


def test_the_repro_table_pins_the_committed_files() -> None:
    names = ("report-analysis.json", "tuning-analysis.json", "binding-budget-analysis.json",
             "cli-analysis.json", "timing-analysis.json", "tables.md", "freeze.json")  # fmt: skip
    for name in names:
        row = f"`docs/references/research-022/campaign/{name}` | `{sha256(CAMPAIGN / name)}`"
        assert row in RESULTS, name
    for rel in ("config/research_022/schedule.yaml", "config/research_022/l01-r022.yaml",
                "config/research_022/latency-arms.yaml",
                "config/research_022/profiles/timing_pairs.yaml"):  # fmt: skip
        assert f"`{rel}` | `{sha256(REPO / rel)}`" in RESULTS


# ----------------------------------------------------------------------------- exactness


def totals(stage: dict[str, Any], bounded: str) -> dict[str, int]:
    t = {"scheduled": 0, "compared": 0, "identical": 0, "excluded": 0, "bad": 0, "diff": 0}
    for e in stage["exactness"].values():
        if e["bounded"] == bounded:
            t["scheduled"] += e["scheduled"]
            t["compared"] += e["compared"]
            t["identical"] += e["identical"]
            t["excluded"] += len(e["excluded_as_truncated"])
            t["bad"] += len(e["bounded_truncated_reference_not"])
            t["diff"] += len(e["differences"])
    return t


def test_exactness_numbers_of_the_page_are_the_evidence_and_there_is_no_difference() -> None:
    for bnd in STRAT.values():
        both = [totals(T, bnd), totals(R, bnd)]
        assert sum(t["diff"] for t in both) == 0 and sum(t["bad"] for t in both) == 0
        assert sum(t["compared"] - t["identical"] for t in both) == 0
        summary = (f"| `{bnd}` | {f(sum(t['compared'] for t in both))} | "
                   f"{f(sum(t['identical'] for t in both))} | "
                   f"{sum(t['excluded'] for t in both)} | 0 | 0 |")
        assert summary in RESULTS, summary
        for split, stage in (("tuning", T), ("report", R)):
            t = totals(stage, bnd)
            runs = sum(1 for e in stage["exactness"].values() if e["bounded"] == bnd)
            row = (f"| `{bnd}` | {split} | {runs} | {f(t['scheduled'])} | {f(t['compared'])} | "
                   f"{f(t['identical'])} | {t['excluded']} | 0 | 0 | 0 | 0 |")
            assert row in RESULTS, row
    for stage in (T, R):  # nothing hides behind a label or a new failure
        for label, e in stage["exactness"].items():
            assert e["label_disagrees_with_truncation"] == [], label
            assert e["new_failure_status"] == [], label
            assert e["compared"] + len(e["excluded_as_truncated"]) == e["scheduled"], label
    excluded = {label: [x["case"] for x in e["excluded_as_truncated"]]
                for label, e in R["exactness"].items() if e["excluded_as_truncated"]}  # fmt: skip
    assert set(excluded) == {"R-A3-mh-full", "R-A4-mh-full"}
    assert all(len(v) == 6 for v in excluded.values())
    assert all(case in RESULTS for cases in excluded.values() for case in cases)
    assert not any(e["excluded_as_truncated"] for e in T["exactness"].values())


def test_the_gate_states_and_m2_activity_are_the_ones_the_page_states() -> None:
    gates = {k: e["gates"] for k, e in T["exactness"].items()}
    assert gates["T-A1-mh-full"]["m2_active"] == 0 and gates["T-A1-mh-sor"]["m2_active"] == 0
    assert gates["T-A1-mh-full"]["m2_gate"] == {"closed:dominance": 3, "closed:frontier": 93}
    assert gates["T-A4-mh-full"]["m2_active"] == 96
    gates_r = {k: e["gates"] for k, e in R["exactness"].items()}
    assert gates_r["R-A1-mh-full"]["m2_active"] == 0 and gates_r["R-A3-mh-full"]["m2_active"] == 0
    assert gates_r["R-A4-mh-full"]["m2_active"] == 301
    assert "**0** cells" in RESULTS and "301/302" in RESULTS and "96/96" in RESULTS


# ----------------------------------------------------------------------------- baseline


def test_the_fourteen_references_are_unchanged_against_the_021_baseline() -> None:
    baseline = R["baseline_0_2_1"]
    total = same = 0
    for label in ("report_full", "report_sor"):
        assert len(baseline[label]) == 14
        for algorithm, v in baseline[label].items():
            assert v["cells"] == 302 and v["missing"] == [], (label, algorithm)
            assert v["outcome_differing"] == [] and v["search_differing"] == [], (label, algorithm)
            total += v["cells"]
            same += v["outcome_and_search_identical"]
    assert same == total == 8456 and f"**{f(same)} of {f(total)} cells" in RESULTS
    sor = baseline["report_sor"]
    assert {a for a, v in sor.items() if v["research_block_differing_cells"]} == {
        "metis_history", "incremental_graph_repair"}  # fmt: skip
    assert all(v["research_block_differing_cells"] == 0 for v in baseline["report_full"].values())
    for a in ("metis_history", "incremental_graph_repair"):
        assert set(sor[a]["research_block_paths"]) == {
            "/diagnostics/domain/hash", "/r021/candidate_domain_hash",
            "/r021/domain/universe/cohort"}  # fmt: skip


# ----------------------------------------------------------------------------- work


def test_the_work_numbers_of_the_page_are_the_evidence() -> None:
    for ref, bnd in STRAT.items():
        g = GROUP[ref]
        for unit in ("quotes_executed", "cl_swap_steps", "lb_bins_swapped"):
            row = R["work"][f"R-WP-A1-{g}-full"]["units"][unit]
            expected = f"{f(row['reference']['sum'])} → {f(row['bounded']['sum'])}"
            assert expected in RESULTS, (bnd, unit, expected)
            assert row["reference"]["sum"] - row["bounded"]["sum"] == row["saved"]
        cost = R["work"][f"R-A1-{g}-full"]["bound_cost"]
        for unit in ("pruned_bound", "bound_evaluations"):
            assert f(cost[unit]["sum"]) in RESULTS, (bnd, unit)
        # the bound's cost is its own column, never a savings unit
        assert "bound_evaluations" not in R["work"][f"R-WP-A1-{g}-full"]["units"]
    for pair in R["work_pass_agreement"].values():
        assert pair["internal_mismatches"] == [] and pair["differs_from_twin"] == []
    # M2 active (A4): the table cost exists only there
    assert R["work"]["R-A4-mh-full"]["bound_cost"]["bound_table_cost"]["sum"] == 142774
    assert all(w["bound_cost"]["bound_table_cost"]["sum"] == 0
               for k, w in R["work"].items() if k != "R-A4-mh-full" and not w["work_pass"]
               and k != "R-WP-A4-mh-full")  # fmt: skip
    for k in ("R-A4-mh-full", "R-A3-mh-full"):
        assert f(R["work"][k]["units"]["quotes_executed"]["bounded"]["sum"]) in RESULTS


def test_a_unit_is_never_zero_when_missing_and_work_units_stay_apart() -> None:
    for stage in (T, R):
        for label, w in stage["work"].items():
            for unit, row in w["units"].items():
                for side in ("reference", "bounded"):
                    assert row[side]["n"] + row[side]["missing"] == w["cases"], (label, unit)
                assert unit in {"quotes_executed", "paths_evaluated", "paths_scored",
                                "label_relaxations", "cl_swap_steps", "lb_bins_swapped",
                                "quotes_executed(work_pass)"}, (label, unit)  # fmt: skip


# ----------------------------------------------------------------------------- A5, CLI, timing


def test_the_binding_budget_runs_are_apart_and_the_target_zero_holds() -> None:
    assert len(M["binding_budgets"]) == 24 and M["exactness"] == {}
    for label, b in M["binding_budgets"].items():
        assert b["bounded_truncated_reference_not"] == [], label
        assert b["score_vs_reference"]["lower"] == 0, label
        assert f"| `{label}` |" in RESULTS
    values = SCHEDULE["a5"]["values"]
    for g, v in values.items():
        assert f(v["max_quotes"]["p25"]) in RESULTS and f(v["max_candidates"]["p50"]) in RESULTS, g
    for g in ("ig", "mh"):  # the registered rule's max_candidates did not bind the chunk strategies
        for p in (25, 50):
            for split in ("tuning", "report"):
                assert set(M["binding_budgets"][f"M-A5-{g}-c{p}-{split}"]["truncation"]) == {
                    "ref_clean/bnd_clean"}  # fmt: skip


def test_the_cli_matrix_covers_all_17_ids() -> None:
    roster = SCHEDULE["rosters"]["all17"]
    for inv in ("I-batch17", "I-batch17.replay", "I-quote-details", "I-quote-compact"):
        assert sorted(I["invocations"][inv]["status_counts"]) == sorted(roster), inv
    assert all(v["result"] == "ok" for v in I["invocations"].values())


def test_timing_is_inconclusive_and_the_gate_evidence_is_what_the_page_states() -> None:
    lat = L["latency"]
    assert lat["gate_passed"] is False and lat["verdict"] == "inconclusive"
    windows = lat["windows"]
    assert windows["L-ref"]["state"] == "contaminated"
    assert windows["L-bnd"]["state"] == "contaminated"
    for ref_id in ("L-ref", "L-bnd"):
        assert windows[ref_id]["max_load1"] > 5.0  # 0.5 x 10 CPUs
        assert f"{windows[ref_id]['max_load1']:.2f}" in RESULTS
    assert all(w["state"] == "clean" for k, w in windows.items() if k.startswith("L-q"))
    assert L["host_sleep_transitions"] == 0
    assert (CAMPAIGN / "timing-pmset-sleep-wake.txt").read_text() == ""
    gate = [g for g in lat["gate"] if g["event"] == "launch_gate"]
    assert gate[-1]["launched"] is True and all(not g["launched"] for g in gate[:-1])
    assert max(s["load1"] for s in gate[-1]["samples"]) <= 3.0
    assert f"passed on attempt {len(gate)}" in RESULTS
    # no strategy carries a timing verdict: the per-strategy map is empty under a failed gate
    assert lat["per_strategy"] == {}
    disposition = json.loads((CAMPAIGN / "disposition.json").read_text())
    assert all(d["timing_verdict"] == "inconclusive" for d in disposition.values())
    assert all(d["statement"] == "work reduction only" for d in disposition.values())


def test_the_disposition_is_the_rule_applied_to_the_committed_analyses() -> None:
    got = C.disposition({"T": T, "R": R, "L": L})
    assert got == json.loads((CAMPAIGN / "disposition.json").read_text())
    for bnd, d in got.items():
        assert d["disposition"] == "keep_experimental" and d["exactness_differences"] == 0
        row = f"| `{bnd}` | **keep_experimental** | work reduction only | inconclusive |"
        assert row in RESULTS, bnd
    # the rule rejects as soon as one non-truncated cell differs (the committed evidence has none)
    broken = json.loads(json.dumps(R))
    first = next(iter(broken["exactness"].values()))
    first["differences"].append({"case": "x", "keys": ["score"]})
    assert C.disposition({"T": T, "R": broken})[first["bounded"]]["disposition"] == "reject"


def test_no_placeholder_survives_in_the_page() -> None:
    assert re.search(r"__[A-Z0-9_]+__", RESULTS) is None
    assert "TBD" not in RESULTS

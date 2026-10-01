"""WHI-1562 research-021 campaign analysis rules on bounded synthetic rows (no solver).

Each test states one pre-registered rule of `tools/research_021/analysis.py` and fails if the
rule broke: unconditional statuses, common-OK pairing (None is never 0), refusal of unlike
schedules, gains and losses per family and stratum, identity/P1/equal-value gates that never
pass a budget-cut cell, ratio decomposition, the max_splits nominee rule, the host-load rule,
bounds never shown as zero, CS-2 attribution and inventory reconciliation.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    path = REPO / "tools" / "research_021" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


A = _load("analysis")


def cell(case: str, status: str = "ok", gross: int | None = None, **record: Any) -> Any:
    rec: dict[str, Any] = {"status": status, "score": None if gross is None else str(gross),
                           "evaluation": None if gross is None else {"gross_output": str(gross)},
                           **record}  # fmt: skip
    return A.Cell(case, status, gross if status == "ok" else None, rec,
                  record.get("solve"), record.get("limit_hit"))  # fmt: skip


def arm(label: str, cells: list[Any], algorithm: str = "x",
        families: Mapping[str, str] | None = None,
        strata: Mapping[str, str] | None = None) -> Any:  # fmt: skip
    ids = [c.case_id for c in cells]
    return A.Arm(label, algorithm, ids, {c.case_id: c for c in cells},
                 dict(families or {c: "p" for c in ids}), dict(strata or {c: "s" for c in ids}),
                 {c: "tuning" for c in ids})  # fmt: skip


def test_status_counts_keep_every_scheduled_outcome_and_reason() -> None:
    a = arm("r/x", [cell("a", "ok", 10), cell("b", "unsupported",
                    search={"r021": {"scope": {"supported": False, "reason": "protocol_ceiling"}}}),
                    cell("c", "timeout", limit_hit="time"), cell("d", "missing"),
                    cell("e", "no_route")])  # fmt: skip
    s = A.status_counts(a)
    assert s["scheduled"] == 5
    assert s["statuses"] == {"missing": 1, "no_route": 1, "ok": 1, "timeout": 1, "unsupported": 1}
    assert s["unsupported_reasons"] == {"protocol_ceiling": 1}
    assert s["timeout_by_limit"] == {"time": 1}
    assert s["failures"] == 2  # timeout + missing; unsupported/no_route are not failures


def test_paired_uses_common_ok_only_and_never_turns_a_missing_score_into_zero() -> None:
    base = arm("b", [cell("a", "ok", 100), cell("b", "ok", 100), cell("c", "timeout"),
                     cell("d", "ok", 0), cell("e", "ok", 200)])  # fmt: skip
    cand = arm("c", [cell("a", "ok", 101), cell("b", "unsupported"), cell("c", "ok", 5),
                     cell("d", "ok", 3), cell("e", "ok", 190)])  # fmt: skip
    p = A.paired(base, cand, comparison_class="same_domain")
    assert p["common_ok"] == 3 and p["zero_baseline_na"] == ["d"]  # d: N/A, not +inf, not 0
    assert (p["only_baseline_ok"], p["only_candidate_ok"]) == (1, 1)
    pooled = p["pooled"]
    assert (pooled["higher"], pooled["equal"], pooled["lower"]) == (1, 0, 1)  # loss is kept
    assert pooled["min"] == pytest.approx(-500.0) and pooled["max"] == pytest.approx(100.0)
    assert p["worst_losses"] == [{"case_id": "e", "bps": pytest.approx(-500.0)}]
    assert p["transitions"]["ok->unsupported"] == 1 and p["transitions"]["timeout->ok"] == 1
    assert p["ranked"] is True
    assert A.paired(base, cand, comparison_class="expanded_protocol")["ranked"] is False


def test_paired_reports_gains_and_losses_per_family_and_stratum() -> None:
    cells_b = [cell(c, "ok", 100) for c in "abcd"]
    cells_c = [cell("a", "ok", 110), cell("b", "ok", 90), cell("c", "ok", 100), cell("d", "ok", 99)]
    fam = {"a": "USDC->USDT", "b": "USDC->USDT", "c": "WMNT->WETH", "d": "WMNT->WETH"}
    strata = {"a": "low", "b": "large", "c": "low", "d": "large"}
    p = A.paired(arm("b", cells_b, families=fam, strata=strata),
                 arm("c", cells_c, families=fam, strata=strata), comparison_class="same_domain")
    assert p["per_family"]["USDC->USDT"] | {} == {"n": 2, "higher": 1, "equal": 0, "lower": 1,
                                                  "min": -1000.0, "median": 0.0, "max": 1000.0}
    assert p["per_family"]["WMNT->WETH"]["lower"] == 1
    assert p["per_stratum"]["large"]["lower"] == 2 and p["per_stratum"]["low"]["higher"] == 1


def test_unlike_schedules_are_refused_not_intersected() -> None:
    with pytest.raises(A.AnalysisError, match="unlike schedules"):
        A.paired(arm("b", [cell("a", "ok", 1), cell("b", "ok", 1)]),
                 arm("c", [cell("a", "ok", 1)]), comparison_class="same_domain")  # fmt: skip
    with pytest.raises(A.AnalysisError, match="unlike schedules"):  # same set, other order
        A.identity(arm("b", [cell("a", "ok", 1), cell("b", "ok", 1)]),
                   arm("c", [cell("b", "ok", 1), cell("a", "ok", 1)]))  # fmt: skip


def test_identity_gate_excludes_budget_bound_cells_and_fails_on_a_difference() -> None:
    ref = arm("e", [cell("a", "ok", 5), cell("b", "ok", 7, search={"truncated_by": "max_quotes"}),
                    cell("c", "ok", 9)])  # fmt: skip
    same = arm("s", [cell("a", "ok", 5), cell("b", "ok", 6), cell("c", "ok", 9)])
    result = A.identity(ref, same)
    assert result["gate"] == "pass" and result["checked"] == 2
    assert result["budget_bound_excluded"] == ["b"]  # listed, never counted as passing
    other = arm("s", [cell("a", "ok", 5), cell("b", "ok", 6), cell("c", "ok", 8)])
    failed = A.identity(ref, other)
    assert failed["gate"] == "fail" and failed["differing"][0]["case_id"] == "c"
    missing = A.identity(ref, arm("s", [cell("a", "ok", 5), cell("b", "ok", 6),
                                        cell("c", "missing")]))  # fmt: skip
    assert missing["gate"] == "fail" and missing["missing"] == ["c"]


def test_not_below_and_equal_value_gates() -> None:
    control = arm("off", [cell("a", "ok", 10), cell("b", "ok", 10), cell("c", "ok", 10)])
    good = arm("on", [cell("a", "ok", 11), cell("b", "ok", 10),
                      cell("c", "timeout", limit_hit="time")])  # fmt: skip
    r = A.not_below(control, good)
    assert r["gate"] == "pass" and r["budget_cut_listed_apart"] == ["c"]
    bad = arm("on", [cell("a", "ok", 9), cell("b", "no_route"), cell("c", "ok", 10)])
    r = A.not_below(control, bad)
    assert r["gate"] == "fail"
    assert (r["below_without_budget_cut"], r["ok_to_failure_without_budget_cut"]) == (["a"], ["b"])
    ev = A.equal_value(control, arm("dsc", [cell("a", "ok", 10), cell("b", "unsupported"),
                                            cell("c", "ok", 11)]))  # fmt: skip
    assert ev["gate"] == "fail" and ev["compared"] == 2 and ev["not_compared"] == {
        "ok->unsupported": 1}  # fmt: skip
    none = A.equal_value(control, arm("dsc", [cell(c, "unsupported") for c in "abc"]))
    assert none["gate"] == "pass" and none["evaluable"] is False  # nothing to certify


def test_depth_decomposition_multiplies_ratios_on_one_common_case_set() -> None:
    e3 = arm("E3", [cell("a", "ok", 100), cell("b", "ok", 200), cell("c", "ok", 50)])
    e4 = arm("E4", [cell("a", "ok", 110), cell("b", "ok", 200), cell("c", "timeout")])
    l4 = arm("L4", [cell("a", "ok", 121), cell("b", "ok", 180), cell("c", "ok", 60)])
    d = A.depth_decomposition({"E3": e3, "E4": e4, "L4": l4}, "E3", [["E4", "L4"]])
    assert d["common_ok_cases"] == 2 and d["excluded_by_arm_status"]["E4"] == {"timeout": 1}
    chain = d["chains"][0]
    steps = {s["ratio"]: s["geometric_mean"] for s in chain["steps"]}
    assert steps["E4/E3"] == pytest.approx(math.sqrt(1.1 * 1.0))
    assert steps["L4/E4"] == pytest.approx(math.sqrt(1.1 * 0.9))
    assert chain["total"]["geometric_mean"] == pytest.approx(steps["E4/E3"] * steps["L4/E4"])
    assert abs(chain["log_identity_residual"]) < 1e-12


def _scan(values: dict[int, list[tuple[str, int | None]]]) -> dict[int, list[tuple[str, Any]]]:
    out = {}
    for v, rows in values.items():
        cells = [cell(c, "ok" if g is not None else "timeout", g) for c, g in rows]
        out[v] = [("tuning_full", arm(f"ms{v}/x", cells))]
    return out


def test_nominee_is_the_smallest_saturating_value_else_canonical() -> None:
    saturating = _scan({1: [("a", 5), ("b", 5)], 2: [("a", 6), ("b", 7)],
                        4: [("a", 6), ("b", 7)], 8: [("a", 6), ("b", 7)]})  # fmt: skip
    assert A.nominee(saturating, canonical=4, algorithms=["x"])["nominee"] == 2
    non_monotone = _scan({1: [("a", 5), ("b", 5)], 2: [("a", 7), ("b", 6)],
                          4: [("a", 6), ("b", 7)], 8: [("a", 6), ("b", 7)]})  # fmt: skip
    r = A.nominee(non_monotone, canonical=4, algorithms=["x"])
    assert r["nominee"] == 4 and r["eligible"] == [] and "canonical 4 retained" in r["reason"]
    timeout = _scan({1: [("a", 5), ("b", 5)], 2: [("a", 6), ("b", 7)],
                     4: [("a", 6), ("b", 7)], 8: [("a", 6), ("b", None)]})  # fmt: skip
    r = A.nominee(timeout, canonical=4, algorithms=["x"])
    assert r["nominee"] == 2 and r["values"]["8"]["failures"] == 1
    assert r["values"]["8"]["shortfall"] == 1  # ok elsewhere, a failure at 8
    with pytest.raises(A.AnalysisError, match="canonical"):
        A.nominee({1: timeout[1]}, canonical=4, algorithms=["x"])


def test_host_rule_and_timing_verdict_never_invent_a_speedup() -> None:
    samples = [{"t": 1.0, "load1": 2.0}, {"t": 2.0, "load1": 5.01}, {"t": 9.0, "load1": 1.0}]
    assert A.host_window(samples, 0, 1.5, 10)["state"] == "clean"
    dirty = A.host_window(samples, 0, 3, 10)
    assert dirty["state"] == "contaminated" and dirty["threshold"] == 5.0
    assert A.host_window(samples, 3, 4, 10)["state"] == "unknown"
    assert A.timing_verdict([dirty], noise_floor_available=True).startswith("inconclusive")
    assert A.timing_verdict([{"state": "unknown"}], noise_floor_available=True).startswith(
        "inconclusive")  # fmt: skip
    clean = A.host_window(samples, 0, 1.5, 10)
    assert A.timing_verdict([clean], noise_floor_available=False).startswith("descriptive only")
    t = A.timing(arm("r", [cell("a", "ok", 1, solve=0.5), cell("b", "timeout")]))
    assert set(t["solve_seconds"]) == {"n", "min", "median", "mean", "max", "sum"}  # no p95
    assert t["solve_seconds"]["n"] == 1


def test_bounds_and_estimates_are_never_shown_as_zero_or_certified() -> None:
    def r021(certificate: Any, reason: str | None = None) -> dict[str, Any]:
        return {"r021": {"certificate": certificate, "certificate_unavailable_reason": reason}}

    view = A.certificate_view(arm("c", [
        cell("a", "ok", 5, search=r021({"bound_kind": "estimate", "termination": "converged"})),
        cell("b", "ok", 5, search=r021({"bound_kind": "unknown", "termination": "complete"})),
        cell("c", "unsupported", search=r021(None, "not_produced")),
        cell("d", "timeout"),
    ]))  # fmt: skip
    assert view["bound_kinds"] == {"estimate": 1, "none (not_produced)": 1,
                                   "no r021 record": 1, "unknown": 1}  # fmt: skip
    assert view["certified_claims"] == 0


def test_work_units_are_side_by_side_and_missing_is_not_zero() -> None:
    a = arm("r", [cell("a", "ok", 1, quotes={"counted": 10},
                       search={"r021": {"work": {"quotes_executed": 10, "paths_scored": 4}}}),
                  cell("b", "timeout", quotes={"counted": 3})])  # fmt: skip
    w = A.work_units(a)
    assert w["units"]["paths_scored"]["n"] == 1 and w["units"]["paths_scored"]["missing"] == 1
    assert w["runner_quotes_counted"]["sum"] == 13
    b = arm("s", [cell("a", "ok", 1, quotes={"counted": 20},
                       search={"r021": {"work": {"quotes_executed": 20, "paths_scored": 8}}}),
                  cell("b", "timeout", quotes={"counted": 3})])  # fmt: skip
    ratio = A.same_unit_ratio(a, b, "paths_scored")
    assert ratio["unit"] == "paths_scored" and ratio["ratio"]["median"] == 2.0


def test_cycle_safe_view_attributes_cs2_and_never_calls_unavailable_identical() -> None:
    def cs(trajectory: str, completed: bool, withheld: bool, rejected: int) -> dict[str, Any]:
        return {"cycle_safe": {"reference_trajectory": trajectory,
                               "comparable_completed": completed,
                               "combinations_rejected_cycle": rejected,
                               "replay": {"phase": "completed" if completed else "interrupted"},
                               "publication": {"published": not withheld,
                                               "withheld_by_cs2": withheld}}}  # fmt: skip

    ref = arm("port", [cell("a", "ok", 5), cell("b", "timeout", limit_hit="time",
                                                 last_valid_candidate={"x": 1}),
                       cell("c", "invalid_plan")])  # fmt: skip
    var = arm("safe", [cell("a", "ok", 5, search=cs("identical", True, False, 0)),
                       cell("b", "timeout", limit_hit="time",
                            search=cs("unavailable", False, True, 0)),
                       cell("c", "ok", 4, search=cs("diverged", True, False, 3))])  # fmt: skip
    v = A.cycle_safe_view(ref, var)
    assert v["comparable_identical_cases"] == ["a"]  # b (no-start/cut) is never an identity
    assert v["publication"] == {"published": 2, "withheld_by_cs2": 1}
    assert v["hard_kills"] == {"reference": 1, "variant": 1}
    assert v["reference_last_valid_candidate_without_variant"] == 1
    assert v["cases_with_rejections"] == 1 and v["variant_invalid_plan"] == 0


def test_reconcile_detects_every_inventory_mismatch() -> None:
    good = A.reconcile("r", algorithms=["a", "b"], case_ids=["x", "y"],
                       expected_algorithms=["a", "b"], expected_case_ids=["x", "y"],
                       cells={(c, a): "ok" for c in "xy" for a in "ab"}, complete=True)  # fmt: skip
    assert good == []
    bad = A.reconcile("r", algorithms=["b", "a"], case_ids=["y", "x"],
                      expected_algorithms=["a", "b"], expected_case_ids=["x", "y"],
                      cells={("x", "a"): "missing"}, complete=False)  # fmt: skip
    assert len(bad) == 5


def test_cfmm_estimate_only_from_the_initial_converged_solve_without_fallback() -> None:
    def cf(termination: str, estimate: bool, fallback: bool) -> dict[str, Any]:
        return {"cfmm": {"stage": "constant_product+concentrated", "termination": termination,
                         "initial": {"termination": termination},
                         "estimate": {"value": "100.0"} if estimate else None,
                         "fallback": {"reason": "support_exhausted"} if fallback else None,
                         "recovery_failure": "support_exhausted" if fallback else None,
                         "estimate_withheld": None if estimate else "initial_not_converged"}}

    good = A.cfmm_view(arm("cf", [cell("a", "ok", 99, search=cf("converged", True, False)),
                                  cell("b", "ok", 50, search=cf("not_converged", False, True))]))
    assert good["gate"] == "pass" and good["estimate_present"] == 1
    assert good["recovered_over_estimate"]["median"] == pytest.approx(0.99)
    assert good["fallbacks"] == {"support_exhausted": 1}
    bad = A.cfmm_view(arm("cf", [cell("a", "ok", 99, search=cf("not_converged", True, False)),
                                 cell("b", "ok", 50, search=cf("converged", True, True))]))
    assert bad["gate"] == "fail" and bad["estimate_rule_violations"] == ["a", "b"]

"""WHI-1602 analysis rules (`tools/research_022/pruning_analysis.py`), on synthetic records.

Each test has a case that would fail if the rule it names were dropped: a difference is found, a
truncated cell is excluded and listed, an unlisted counter cannot differ, a missing value is never
0, units are never mixed, the A5 percentile rule is nearest-rank.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    path = REPO / "tools" / "research_022" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PA: Any = _load("pruning_analysis")


def record(
    algorithm: str, case: str = "emp-aaaaaa-bbbbbb-low-1", *, score: str = "100",
    quotes: int = 50, status: str = "ok", search: dict[str, Any] | None = None,
    block: dict[str, Any] | None = None, **extra: Any,
) -> dict[str, Any]:  # fmt: skip
    out: dict[str, Any] = {
        "algorithm": algorithm, "case_id": case, "status": status, "error": None, "score": score,
        "evaluation": {"gross_output": score, "trace": [{"pool_id": "p1", "step": 0}]},
        "solver_reported": {"status": status, "score": score}, "limit_hit": None,
        "candidates_considered": 10, "candidates_truncated": 0,
        "quotes": {"counted": quotes}, "measurement": {"seed": 1, "attempts_consistent": True},
        "search": {"truncated_by": None, "paths_enumerated": 4, "paths_evaluated": 8,
                   "paths_pruned": 0, "quotes_executed": quotes, "quotes_memoized": 0,
                   "failed_candidates": {}, "paths_incomplete": 0, "incomplete_example": None,
                   "best_hops": 1, **(search or {})},
    }  # fmt: skip
    if block is not None:
        out["search"]["bound_pruning"] = block
    out.update(extra)
    return out


def bounded_block(**kw: Any) -> dict[str, Any]:
    return {"pruned_bound": 3, "bound_evaluations": 9, "bound_no_bound": 0, "bound_table_cost": 0,
            "exactness": {"label": "exact", "binding": []}, "rule": "S1", **kw}  # fmt: skip


def pair(**kw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    ref = record("single_path")
    bnd = record("single_path_bounded", quotes=20, block=bounded_block(),
                 search={"paths_evaluated": 5, "paths_pruned": 0})  # fmt: skip
    bnd["search"].update(kw)
    return ref, bnd


def run(ref: dict[str, Any], bnd: dict[str, Any]) -> dict[str, Any]:
    case = ref["case_id"]
    out = PA.exactness({case: ref}, {case: bnd}, "single_path_bounded", [case])
    assert isinstance(out, dict)
    return out


# ----------------------------------------------------------------------------- exactness


def test_a_pruned_run_with_only_allowed_counters_changed_is_identical() -> None:
    result = run(*pair())
    assert (result["compared"], result["identical"]) == (1, 1)
    assert result["differences"] == [] and result["excluded_as_truncated"] == []


def test_every_outcome_field_is_compared() -> None:
    for key, value in (("score", "99"), ("status", "no_route"), ("error", "boom"),
                       ("evaluation", {"gross_output": "99", "trace": []}),
                       ("solver_reported", {"status": "ok", "score": "1"}),
                       ("candidates_considered", 11), ("candidates_truncated", 1)):  # fmt: skip
        ref, bnd = pair()
        bnd[key] = value
        result = run(ref, bnd)
        assert result["identical"] == 0, key
        assert key in result["differences"][0]["keys"], key


def test_a_different_plan_with_the_same_score_is_still_a_difference() -> None:
    ref, bnd = pair()
    bnd["evaluation"] = {"gross_output": "100", "trace": [{"pool_id": "p2", "step": 0}]}
    assert run(ref, bnd)["differences"][0]["keys"] == ["evaluation"]


def test_a_counter_outside_the_allowed_column_cannot_differ() -> None:
    for key in ("paths_enumerated", "best_hops", "truncated_by"):
        ref, bnd = pair()
        bnd["search"][key] = 99 if key != "truncated_by" else "state_cap"
        assert f"search.{key}" in run(ref, bnd)["differences"][0]["keys"], key


def test_the_added_key_is_required_and_no_other_key_may_appear() -> None:
    ref, bnd = pair()
    del bnd["search"]["bound_pruning"]
    assert run(ref, bnd)["differences"][0]["keys"][0].startswith("search_keys:")
    ref, bnd = pair()
    bnd["search"]["surprise"] = 1
    assert run(ref, bnd)["differences"][0]["keys"][0].startswith("search_keys:")


def test_a_truncated_side_is_excluded_and_listed_never_compared() -> None:
    ref, bnd = pair()
    ref["search"]["truncated_by"] = "max_quotes"
    bnd["search"]["truncated_by"] = "max_quotes"
    bnd["search"]["bound_pruning"]["exactness"] = {"label": "not_exact_budget_binding",
                                                   "binding": ["max_quotes"]}
    bnd["score"] = "999"  # would be a difference if it were compared
    result = run(ref, bnd)
    assert result["compared"] == 0 and result["differences"] == []
    assert result["excluded_as_truncated"] == [{"case": ref["case_id"],
                                                "reference": "truncated_by:max_quotes",
                                                "bounded": "truncated_by:max_quotes"}]


def test_bounded_truncated_with_a_clean_reference_is_its_own_list() -> None:
    ref, bnd = pair()
    bnd["search"]["truncated_by"] = "max_candidates"
    result = run(ref, bnd)
    assert result["bounded_truncated_reference_not"] == [
        {"case": ref["case_id"], "bounded": "truncated_by:max_candidates"}]
    assert result["compared"] == 0 and result["excluded_as_truncated"] == []


def test_a_state_cap_alone_is_not_truncation_but_a_hard_stop_and_a_timeout_are() -> None:
    names = ("metis_history", "metis_history_bounded")
    capped = record("metis_history", search={"truncated_by": "state_cap",
                                             "truncated_stages": ["metis_history"]})
    assert PA.cut_reason(capped, names) is None
    stage = record("metis_history", search={"truncated_by": None,
                                            "truncated_stages": ["single_path"]})
    assert PA.cut_reason(stage, names) == "truncated_stages:single_path"
    stopped = record("metis_history", status="timeout")
    stopped["limit_hit"] = "quotes"
    assert PA.cut_reason(stopped, names) == "status:timeout/quotes"
    assert PA.cut_reason(None, names) == "missing"


def test_the_label_must_agree_with_a_non_truncated_cell() -> None:
    ref, bnd = pair()
    bnd["search"]["bound_pruning"]["exactness"] = {"label": "not_exact_budget_binding",
                                                   "binding": ["max_quotes"]}
    result = run(ref, bnd)
    assert result["label_disagrees_with_truncation"] == [ref["case_id"]]


def test_a_new_invalid_plan_is_flagged_even_though_it_also_differs() -> None:
    ref, bnd = pair()
    bnd["status"] = "invalid_plan"
    result = run(ref, bnd)
    assert result["new_failure_status"] == [ref["case_id"]] and result["identical"] == 0


def test_an_active_m2_may_change_the_label_population_and_nothing_else() -> None:
    def chunk(name: str, **kw: Any) -> dict[str, Any]:
        return record(name, search={"label_relaxations": 10, "evaluations": 5, "paths_scored": 7,
                                    "marginal_failures": {}, "marginal_incomplete": 0, **kw})

    ref = chunk("metis_history")
    bnd = chunk("metis_history_bounded", label_relaxations=4)
    bnd["search"]["bound_pruning"] = bounded_block(rule="M1", m2={"active": False, "gate": "open"})
    m1 = PA.exactness({"c": ref | {"case_id": "c"}}, {"c": bnd | {"case_id": "c"}},
                      "metis_history_bounded", ["c"])
    assert "search.label_relaxations" in m1["differences"][0]["keys"]  # M1 is population neutral
    bnd["search"]["bound_pruning"]["m2"] = {"active": True, "gate": "open"}
    bnd["candidates_considered"] = 3  # allowed under an active M2 ...
    m2 = PA.exactness({"c": ref | {"case_id": "c"}}, {"c": bnd | {"case_id": "c"}},
                      "metis_history_bounded", ["c"])
    assert m2["differences"] == []
    bnd["search"]["evaluations"] = 6  # ... but `evaluations` is unchanged by M2
    again = PA.exactness({"c": ref | {"case_id": "c"}}, {"c": bnd | {"case_id": "c"}},
                         "metis_history_bounded", ["c"])
    assert again["differences"][0]["keys"] == ["search.evaluations"]


# ----------------------------------------------------------------------------- work


def test_units_are_summed_apart_missing_is_never_zero_and_ratio_is_same_unit() -> None:
    row = PA.unit_row([10, 20, None], [4, None, 5])
    assert row["cases_with_both"] == 1
    assert row["ratio_bounded_over_reference"] == 4 / 10  # only the case with both sides
    assert row["reference"]["missing"] == 1 and row["bounded"]["missing"] == 1
    assert row["reference"]["sum"] == 30 and row["bounded"]["sum"] == 9
    assert PA.unit_row([None], [None])["ratio_bounded_over_reference"] is None


def test_work_table_keeps_savings_and_the_bounds_cost_in_separate_columns() -> None:
    cases = ["c1", "c2"]
    refs = {c: record("single_path", c, quotes=100) for c in cases}
    bnds = {c: record("single_path_bounded", c, quotes=40, block=bounded_block(
        pruned_bound=5, bound_evaluations=12)) for c in cases}
    table = PA.work_table(refs, bnds, "single_path_bounded", cases)
    assert table["units"]["quotes_executed"]["saved"] == 120
    assert table["bound_cost"]["bound_evaluations"]["sum"] == 24
    assert table["bound_cost"]["bound_table_cost"]["sum"] == 0
    assert "bound_evaluations" not in table["units"]  # never netted into a savings unit
    assert table["skipped_fraction_of_candidates_considered"]["sum_over_sum"] == 10 / 20


def test_a_unit_that_no_record_carries_is_absent_not_zero() -> None:
    refs = {"c": record("single_path", "c")}
    bnds = {"c": record("single_path_bounded", "c", block=bounded_block())}
    units = PA.work_table(refs, bnds, "single_path_bounded", ["c"])["units"]
    assert "cl_swap_steps" not in units and "lb_bins_swapped" not in units
    for rec in (refs["c"], bnds["c"]):
        rec["search"]["r022_work"] = {"cl_swap_steps": 7, "lb_bins_swapped": 0,
                                      "quotes_executed": 50}
    units = PA.work_table(refs, bnds, "single_path_bounded", ["c"])["units"]
    assert units["cl_swap_steps"]["reference"]["sum"] == 7
    assert units["lb_bins_swapped"]["reference"]["sum"] == 0  # a real zero stays a zero


def test_case_families_follow_the_case_id_and_the_plan_pools() -> None:
    pools = {"p1": "concentrated", "p2": "liquidity_book"}
    ref = record("single_path")
    ref["evaluation"]["trace"] = [{"pool_id": "p1"}, {"pool_id": "p2"}]
    keys = PA.family_keys("nod-09bc4e-c96de2-medium-2", "full_source", ref, pools)
    assert keys == {"cohort": "full_source", "direct": "no_direct", "mix": "cl+lb",
                    "stratum": "medium", "pair": "09bc4e->c96de2"}
    assert PA.family_keys("bnd-09bc4e-201eba-liq_at", "x", None, pools)["stratum"] == "boundary"
    assert PA.family_keys("bnd-09bc4e-201eba-liq_at", "x", None, pools)["mix"] == "none"


def test_gate_states_count_a_missing_block_apart() -> None:
    bnd = record("metis_history_bounded", block=bounded_block(
        rule="M1", p0=True, m2={"active": True, "gate": "open"}))
    states = PA.gate_states({"a": bnd, "b": record("metis_history_bounded", "b")}, ["a", "b"])
    assert states["m2_active"] == 1 and states["missing_block"] == 1
    assert states["m2_gate"] == {"open": 1} and states["p0_true"] == 1


# ----------------------------------------------------------------------------- A5 and baseline


def test_budget_values_are_nearest_rank_over_ok_positive_cells() -> None:
    records = [record("single_path", str(i), quotes=q) for i, q in enumerate(
        [10, 20, 30, 40, 50, 60, 70, 80])]
    records.append(record("single_path", "x", status="no_route", quotes=999))
    records.append(record("single_path", "z", quotes=0))
    for r in records:
        r["candidates_considered"] = r["search"]["quotes_executed"] // 10
    got = PA.budget_values(records)
    assert got["max_quotes"] == {"p25": 20, "p50": 40}  # ceil(.25*8)=2nd, ceil(.5*8)=4th
    assert got["max_candidates"] == {"p25": 2, "p50": 4}
    assert got["cells"]["quotes_executed"] == 8  # the no_route and the zero are not counted


def test_binding_budget_view_reports_quadrants_and_what_the_bounded_run_did() -> None:
    names = ("single_path", "single_path_bounded")
    ref = {c: record("single_path", c, score="100") for c in "abcd"}
    bnd = {c: record("single_path_bounded", c, score="100", block=bounded_block()) for c in "abcd"}
    for c in "ab":
        ref[c]["search"]["truncated_by"] = "max_quotes"
    bnd["a"]["search"]["truncated_by"] = "max_quotes"
    bnd["b"]["score"] = "120"  # the bounded run goes further
    bnd["c"]["search"]["truncated_by"] = "max_quotes"  # bounded cut, reference not
    view = PA.binding_budget_view(ref, bnd, names[1], list("abcd"))
    assert view["truncation"] == {"ref_cut/bnd_cut": 1, "ref_cut/bnd_clean": 1,
                                  "ref_clean/bnd_cut": 1, "ref_clean/bnd_clean": 1}
    assert view["bounded_truncated_reference_not"] == ["c"]
    assert view["score_vs_reference"] == {"higher": 1, "equal": 3, "lower": 0}
    assert view["higher_cases"] == ["b"]


def test_baseline_compare_has_three_tiers_and_stamps_are_not_behaviour() -> None:
    old = {("direct", c): record("direct", c) for c in ("a", "b", "c", "d", "e")}
    for r in old.values():
        r["diagnostics"] = {"checked_against": {"run": {"git_revision": "aaa"}},
                            "domain": {"hash": "h"}, "stages": {"solve": 1.0}}
        r["search"]["r021"] = {"certificate": {"source": {"git_revision": "aaa"}},
                               "stages": {"solve": 1.0}, "domain": {"universe": {"cohort": "x"}}}
    new = copy.deepcopy(old)
    for r in new.values():  # a new commit and new wall seconds: identical behaviour
        r["diagnostics"]["checked_against"]["run"]["git_revision"] = "bbb"
        r["diagnostics"]["stages"]["solve"] = 2.0
        r["search"]["r021"]["certificate"]["source"]["git_revision"] = "bbb"
        r["search"]["r021"]["stages"]["solve"] = 2.0
    new[("direct", "b")]["score"] = "101"  # tier 1: the outcome
    new[("direct", "c")]["search"]["paths_enumerated"] = 99  # tier 2: the strategy's counters
    new[("direct", "d")]["search"]["r021"]["domain"]["universe"]["cohort"] = "y"  # tier 3
    del new[("direct", "e")]
    out = PA.baseline_compare(new, old, ["direct"])["direct"]
    assert out["cells"] == 5 and out["missing"] == ["e"]
    assert [d["case"] for d in out["outcome_differing"]] == ["b"]
    assert out["outcome_differing"][0]["keys"] == ["score"]
    assert out["search_differing"] == [{"case": "c", "keys": ["search/paths_enumerated"]}]
    assert out["research_block_differing_cells"] == 1
    assert list(out["research_block_paths"]) == ["/r021/domain/universe/cohort"]
    assert out["outcome_and_search_identical"] == 2  # a and d: the research block is apart
    timed = {("direct", "a"): record("direct", "a", status="timeout")}
    timed[("direct", "a")]["limit_hit"] = "time"
    same = copy.deepcopy(timed)
    same[("direct", "a")]["search"]["paths_enumerated"] = 5  # how far a killed search got
    assert PA.baseline_compare(same, timed, ["direct"])["direct"]["outcome_differing"] == []

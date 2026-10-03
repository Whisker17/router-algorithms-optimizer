"""WHI-1561: the executable worked examples of the five 0.2.1 strategies
(`docs/examples/routing-algorithms/r021_examples.py`).

The example module already checks every published value against an independent expectation
(its own hand CPMM formula / fund ledger / exhaustive oracles, pinned research fixtures, the
pinned CFMMRouter.jl author run, exact protocol quotes). These tests re-state the headline
numbers the guide publishes as literals taken from those independent sources, so a changed
example (or a changed factory) fails here too, and check that a wrong expectation is caught.
"""

from __future__ import annotations

import importlib.util
import json
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "docs" / "examples" / "routing-algorithms" / "r021_examples.py"
R021 = ROOT / "docs" / "references" / "research-021" / "fixtures"


@cache
def examples() -> ModuleType:
    spec = importlib.util.spec_from_file_location("r021_examples", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@cache
def data(name: str) -> Any:
    return examples().EXAMPLES[name]()


def _fixture(name: str) -> Any:
    return json.loads((R021 / name).read_text(encoding="utf-8"))


def test_hand_formula_is_the_published_teaching_formula() -> None:
    m = examples()
    pool = m.cp("P_AB1", "TKA", "TKB", 100_000, 100_000)
    assert m.hand_out(pool, "TKA", 10_000) == 9066  # guide §1.7 / run_examples section 1
    moe = m.cp("m", "A", "B", (1 << 112) - 10, 1000, 30, "moe_classic_v1")
    assert m.hand_out(moe, "A", 10) is None  # uint112 post-swap balance reverts
    assert m.hand_out(m.cp("d", "A", "B", 1000, 1000), "A", 1) is None  # zero output


def test_metis_history_example() -> None:
    d = data("metis_history")
    c = d["competing_prefixes"]
    assert {tuple(x["pools"]): x["amount"] for x in c["layer2_at_C"]} == {
        ("ab", "bc1"): 11926,
        ("ab", "cb2"): 4969,
        ("ad", "dc"): 9840,
    }
    assert c["exhaustive_best"] == {"pools": ["ad", "dc", "cb2", "bt"], "gross": 19560}
    assert c["metis_history"]["score"] == c["metis_history"]["hand_gross"] == 19560
    assert c["metis_inspired"]["score"] == 9938
    assert c["repeated_token_walk"]["evaluator"] == "invalid_plan"
    assert c["options"]["source"]["kind"] == "preset" and c["options"]["source"]["version"] == 1
    x = d["admission_x4b"]
    assert [c["pools"] for c in x["independent_trajectory"]] == [
        ["sx", "xa", "ad"],
        ["sb", "bv", "vx", "xd"],
    ]
    assert x["metis_history"]["score"] > x["metis_inspired"]["score"]
    u = d["unsafe_deletion"]
    assert (u["wide_caps"]["score"], u["preset"]["score"]) == (4920982, 9871)
    assert u["preset"]["diagnostics"]["termination"] == "state_cap"
    assert d["tie_state"]["metis_history"]["diagnostics"]["incremental_evaluated_gross"] == "9"
    assert d["tie_state"]["enumeration"]["diagnostics"]["incremental_evaluated_gross"] == "8"
    g = d["greedy_trap"]
    assert g["metis_history"]["diagnostics"]["incremental_evaluated_gross"] == "9943929"
    assert g["metis_inspired"]["diagnostics"]["incremental_evaluated_gross"] == "12757712"
    assert d["caps"]["full_fill_capped"]["status"] == "timeout"
    assert d["caps"]["full_fill_uncapped"]["score"] == 992032


def test_direct_split_certified_example() -> None:
    d = data("direct_split_certified")
    g = d["grid38"]
    r6 = _fixture("reconstructions.json")["R6_grid_order_and_bounds"]
    cert = g["complete"]["certificate"]
    assert (cert["lower_raw"], cert["upper_raw"], cert["gap_raw"]) == ("58", "58", "0")
    assert g["search_trace"]["root"]["ub"] == r6["tangent_upper_floor"] == 59
    assert g["pool_order_p2p1"]["score"] == g["raw_integer"]["score"] == 59
    nc, qc = g["node_cap_1"]["certificate"], g["quote_budget_2"]["certificate"]
    assert (nc["lower_raw"], nc["upper_raw"], nc["gap_raw"], nc["termination"]) == (
        "58",
        "59",
        "1",
        "node_cap",
    )
    assert (qc["lower_raw"], qc["upper_raw"], qc["gap_raw"], qc["termination"]) == (
        "57",
        "59",
        "2",
        "quote_budget",
    )
    assert g["quote_budget_0"]["status"] == "timeout" and g["quote_budget_0"]["certificate"] is None
    assert d["plateau_r3"]["G"] == {"0": 70, "1": 70, "2": 70, "3": 72, "15": 76}
    assert d["plateau_r3"]["raw_integer"]["score"] == 76
    assert (d["dust"]["row"]["score"], d["real_moe_single_pool"]["row"]["score"]) == (2, 73242137)
    assert d["unsupported_mixed"]["row"]["status"] == "unsupported"
    assert g["raw_integer"]["options_source"] == {"kind": "override"}


def test_incremental_graph_repair_example() -> None:
    d = data("incremental_graph_repair")
    st = d["structural_trap"]
    oracle = _fixture("suffix-repair.json")["fixtures"]["structural_trap"]["oracle"]
    assert [s["pools"] for s in st["independent_greedy"]] == oracle["incumbent_sequence"]
    assert st["repair_off"]["score"] == oracle["incumbent_gross"] == 90545314
    assert st["repair_on"]["score"] == oracle["best_gross"] == 111178819
    assert st["exhaustive"]["complete_sequences"] == 120
    assert [a["outcome"] for a in st["repair_on"]["repair"]["attempts"]][-1] == "accepted"
    assert d["twin_pools_rejected"]["repair_on"]["diagnostics"]["chosen_source"] == "direct_split"
    assert d["attempt_cap_1"]["repair"]["stop"] == "attempt_cap"
    assert d["quote_budget_cut"]["row"]["score"] == 83270629
    assert d["greedy_trap_cross_reference"]["row"]["score"] == 12757712


def test_uni_sor_cycle_safe_example() -> None:
    d = data("uni_sor_cycle_safe")
    a1 = d["union_cycle_A1"]
    assert a1["uni_sor_port"]["status"] == "invalid_plan"
    assert a1["uni_sor_cycle_safe"]["score"] == 2231431 == a1["hand_quotes"]["abc"][1]
    assert a1["uni_sor_cycle_safe"]["admission"] == {
        "admission_checks": 2,
        "combinations_rejected_cycle": 2,
    }
    assert d["no_admissible_A3"]["uni_sor_cycle_safe"]["status"] == "no_route"
    assert d["teaching_graph"]["uni_sor_cycle_safe"]["score"] == 12581


def test_cfmm_dual_example() -> None:
    d = data("cfmm_dual")
    assert d["presets"]["v1"]["source"]["version"] == 1
    assert d["presets"]["v2"]["source"]["version"] == 2
    assert d["presets"]["v2"]["options"]["market_protocols"] == "constant_product+concentrated"
    t = d["triangle"]
    for key in ("v1_historical", "v2_current"):
        assert t[key]["score"] == 137 and t[key]["certificate"]["bound_kind"] == "estimate"
        assert [(f["pool_id"], f["amount_in"], f["amount_out"]) for f in t[key]["flows"]] == [
            ("st", 86, 78),
            ("sm", 64, 125),
            ("mt", 91, 43),
            ("mt2", 34, 16),
        ]
    assert t["brute_force_integer_optimum"] == t["max_iterations_2"]["score"] == 138
    assert t["max_iterations_2"]["certificate"]["bound_kind"] == "unknown"
    assert (d["cycle"]["removed"], d["cycle"]["resolved"]["score"]) == ("ab2", 511)
    assert d["cycle"]["resolve_starved"]["score"] == 509
    assert d["nonconvergence"]["row"]["score"] == 99
    rf = d["recovery_failures"]
    assert rf["attempts_exhausted"]["fallback"]["source"] == "single_path"
    assert rf["fallback_none"]["status"] == "model_error"
    assert rf["tiny_order"]["status"] == "no_route"
    cl = d["cl"]
    assert max(p["max_rel_diff"] for p in cl["author_probes"]) < 5e-14
    assert cl["missing_tick_landing"]["landing"]["status"] == "incomplete_snapshot"
    assert [s["row"]["status"] for s in cl["single_market"]] == ["ok", "ok", "incomplete_snapshot"]
    rc = d["real_cp_cl"]
    assert (rc["v2_cl_stage"]["score"], rc["v1_cpmm_stage"]["score"]) == (993469, 990975)
    assert rc["best_exact_single_path"] < rc["v2_cl_stage"]["score"]


def test_real_state_walkthrough_has_every_row_of_the_all_roster() -> None:
    """The fourteen 0.2.1 rows plus the three 0.2.2 bounded rows (WHI-1599, WHI-1600; `all`)."""
    d = data("real_state")
    rows = {r["algorithm"]: r for r in d["rows"]}
    assert list(rows) == [
        "direct",
        "single_path",
        "direct_split",
        "path_split",
        "incremental_graph",
        "uni_sor_port",
        "uni_sor_adaptive",
        "uni_sor_optimized",
        "metis_inspired",
        "metis_history",
        "direct_split_certified",
        "incremental_graph_repair",
        "uni_sor_cycle_safe",
        "cfmm_dual",
        "single_path_bounded",
        "incremental_graph_bounded",
        "metis_history_bounded",
    ]
    assert d["bundle"]["pools"] == 19 and d["bundle"]["block"]["number"] == 101082044
    for name in ("metis_history", "incremental_graph_repair", "incremental_graph"):
        assert rows[name]["score"] == 10000663447
    for name in ("incremental_graph_bounded", "metis_history_bounded"):  # exact accelerations
        assert rows[name]["score"] == rows[name.removesuffix("_bounded")]["score"]
    for name in ("direct", "uni_sor_cycle_safe", "cfmm_dual"):
        assert rows[name]["score"] == 10000660449
    assert rows["direct_split_certified"]["status"] == "unsupported"
    assert rows["direct_split_certified"]["scope"]["reason"] == "non_constant_product_direct_pool"
    assert rows["cfmm_dual"]["options"]["source"]["version"] == 2
    assert rows["cfmm_dual"]["cfmm"]["stage"] == "constant_product+concentrated"
    ablation = d["cpmm_stage_ablation"]
    assert ablation["row"]["score"] == ablation["best_hand_cpmm_single_path"] == 14409
    assert ablation["row"]["options_source"]["version"] == 1


def test_a_wrong_expectation_is_reported() -> None:
    m = examples()
    with pytest.raises(m.ExampleError, match="got 1, expected 2"):
        m.equal(1, 2, "demo")

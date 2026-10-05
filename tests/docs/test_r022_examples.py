"""WHI-1601: the executable worked examples of the three 0.2.2 bound-pruned strategies and the
bound theory (`docs/examples/routing-algorithms/r022_examples.py`; guide §§19-22).

The example module already checks every published value against an expectation that does not
come from the bounded code under test (the pinned hand-rate fixture, its own integer
constant-product quote and fund ledger, the exact protocol seam, the reference solvers and its own
oracles for rules S1, I1, M1 and M2). These tests restate the numbers the guide publishes as
literals taken from those independent sources, so a changed example *or a changed factory* fails
here too, and they show that the checks have teeth:

* an underestimating bound helper makes the examples fail (`test_an_underestimating_*`);
* a refusal turned into a prune -- the slack dropped from the chunk bound, or the structural gate
  `G_M2` forced open -- makes the examples fail (`test_a_refusal_turned_into_a_prune_*`);
* every number of the guide's chapters 19-22 is backed by a literal of this file or by a value the
  example module computed and asserted (`test_every_number_of_the_guide_chapters_is_asserted`).

Everything is offline: tracked fixtures only (`tests/fixtures/...`, `docs/references/research-022/
fixtures/`), no RPC, Dune, credentials or `data/`. Nothing here asserts or implies a timing.
"""

from __future__ import annotations

import importlib.util
import json
import re
from fractions import Fraction
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from pools import bounds as bounds_module
from pools.bounds import OutputBound
from routing.algorithms import chunk_pruning

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "docs" / "examples" / "routing-algorithms" / "r022_examples.py"
GUIDE = ROOT / "docs" / "references" / "routing-algorithms.md"


@cache
def examples() -> ModuleType:
    spec = importlib.util.spec_from_file_location("r022_examples", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@cache
def everything() -> dict[str, Any]:
    collected: dict[str, Any] = examples().collect()  # each example asserts as it runs
    return collected


def data(name: str) -> Any:
    return everything()[name]


# ======================================================================================
# 19. bounds
# ======================================================================================


def test_hand_derived_rates_are_the_pinned_fixture_values() -> None:
    cells = {(c["name"], c["token_in"]): c["hand_rate"] for c in data("bounds")["hand_rates"]}
    assert len(cells) == 14
    assert cells[("cpmm 2000/500 fee 30bps", "A")] == "997/4000"
    assert cells[("cpmm 2000/500 fee 30bps", "B")] == "997/250"
    assert (
        cells[("cl tick 0 fee 3000 pips", "A")]
        == cells[("cl tick 0 fee 3000 pips", "B")]
        == "997/1000"
    )
    assert cells[("lb id 2**23 step 10 baseFactor 5000", "A")] == "1999/2000"
    assert cells[("lb id 2**23 step 25 baseFactor 800", "B")] == "4999/5000"
    # those are the values derived on paper before any bound code existed
    fixture = json.loads(
        (ROOT / "docs/references/research-022/fixtures/hand_cases.json").read_text(encoding="utf-8")
    )
    assert fixture["rate_cases"][0]["rates"] == {"A": "997/4000", "B": "997/250"}


def test_r2_the_chunk_slack_is_needed() -> None:
    r2 = data("bounds")["r2"]
    assert r2["reserves"] == [1000, 1000] and r2["fee_bps"] == 30 and r2["rate"] == "997/1000"
    assert r2["q"] == [0, 0, 1, 2, 3, 4, 5]  # the guide's R2 outputs, x = 0..6
    assert r2["floor_rate_x"] == [0, 0, 1, 2, 3, 4, 5]
    # q(2) - q(1) = 1 exceeds r * 1 = 0.997: a slack-free chunk bound floor(0.997) = 0 is false
    assert r2["marginal_x1_m1"] == 1 > Fraction(997, 1000)
    assert (r2["slack_free_bound_m1"], r2["bound_with_slack_m1"], r2["slack"]) == (0, 1, 1)


def test_real_pools_never_exceed_the_rate_bound() -> None:
    pools = {row["pool"]: row for row in data("bounds")["real_pools"]}
    assert [row["rate_6dp"] for row in pools.values()] == ["1.008548", "1.000121", "1.000099"]
    table = {
        name: [(t["x"], t["q"], t["floor_rate_x"]) for t in row["table"]]
        for name, row in pools.items()
    }
    assert table["0x69a707d8"] == [
        (10_000, 5_933, 10_085),
        (1_000_000, 14_207, 1_008_548),
        (100_000_000, 14_407, 100_854_826),
        (10_000_000_000, 14_409, 10_085_482_625),
        (100_000_000_000, 14_409, 100_854_826_254),
    ]
    assert table["0x36f66548"] == [
        (10_000, 10_001, 10_001),
        (1_000_000, 1_000_121, 1_000_121),
        (100_000_000, 100_012_068, 100_012_123),
        (10_000_000_000, 10_000_660_449, 10_001_212_335),
        (100_000_000_000, 99_956_962_148, 100_012_123_358),
    ]
    assert table["0x368b1480"] == [
        (10_000, 10_000, 10_000),
        (1_000_000, 1_000_099, 1_000_099),
        (100_000_000, 100_003_452, 100_009_996),
        (10_000_000_000, 9_999_983_351, 10_000_999_699),
        (100_000_000_000, 99_985_554_791, 100_009_996_999),
    ]
    assert all(q <= bound for rows in table.values() for _x, q, bound in rows)
    assert pools["0x69a707d8"]["output_reserve"] == 14_410  # the CPMM pool is nearly drained


def test_multi_hop_composition_and_the_chunk_chain() -> None:
    m = data("bounds")["multi_hop"]
    assert m["pools"] == {"p1": [1_000_000, 1_500_000], "p2": [2_000_000, 1_000_000]}
    assert m["rates"] == ["2991/2000", "997/2000"] and m["hop_outputs"] == [14_807, 7_327]
    # nested: floor(997/2000 * floor(2991/2000 * 10000)) = floor(997/2000 * 14955) = 7455
    assert (14_955 * 997) // 2000 == m["nested_bound"] == m["product_bound"] == 7_455
    assert m["hop_outputs"][1] <= m["nested_bound"]
    chunk = m["chunk"]
    assert chunk["committed"] == [40_000, 30_000] and chunk["m"] == 10_000
    assert chunk["marginals"] == [13_699, 6_585] and chunk["chain_u"] == [10_000, 14_956, 7_456]
    # u_1 = floor(2991/2000 * 10000 + 1) = 14956; u_2 = floor(997/2000 * 14956 + 1) = 7456
    assert (2991 * 10_000) // 2000 + 1 == 14_956 and (997 * 14_956 + 2000) // 2000 == 7_456
    assert chunk["product_form"] == 7_456 and all(
        mg <= u for mg, u in zip(chunk["marginals"], chunk["chain_u"][1:], strict=True)
    )


def test_same_direction_opposite_direction_and_the_evaluator() -> None:
    d = data("bounds")["directions"]
    assert d["first_swap"] == {"in": 200_000, "out": 166_249}
    assert d["same_direction"] == {
        "in": 100_000,
        "out": 63_957,
        "bound_from_original_state": 99_700,
    }
    assert d["opposite_direction"] == {
        "in": 100_000,
        "out": 128_169,
        "bound_from_original_state": 99_700,
    }
    assert d["opposite_direction"]["out"] > d["opposite_direction"]["bound_from_original_state"]
    assert d["cyclic_plan"] == {
        "status": "invalid_plan",
        "error": "economic token cycle: A -> B -> A",
    }
    assert d["reuse_replay"] == [166_249, 63_957]  # the second leg sees the state the first left


def test_the_swapped_state_bound_underestimates_by_24() -> None:
    s = data("bounds")["swapped_state"]
    assert s == {
        "committed": 100_000_000,
        "m": 100_000,
        "marginal": 24_987,
        "bound_original": 99_701,
        "bound_swapped": 24_963,
        "excess": 24,
    }


def test_no_bound_states_and_the_rate_only_direction() -> None:
    rows = {r["state"]: r for r in data("bounds")["no_bound"]}
    assert len(rows) == 10
    quotes = {name: r["exact_quote_status_at_100"] for name, r in rows.items()}
    assert (
        quotes["cl: frozen tick one above the price (guard G_CL)"] == "ok"
    )  # quotes, but no bound
    assert quotes["cpmm: a zero reserve"] == "insufficient_liquidity"
    assert quotes["lb: static fee parameters not collected"] == "incomplete_snapshot"
    assert quotes["cpmm: unknown source key"] == "unsupported"
    assert all(r["bound"] is None for n, r in rows.items() if "sqrt_price_x96 >=" not in n)
    assert rows["cl: sqrt_price_x96 >= 2**128, token0 -> token1"]["bound"] == "rate only"


def test_the_bound_table_of_the_tracked_fixture() -> None:
    t = data("bounds")["fixture_table"]
    assert (t["pool_directions"], t["bounded"], t["rate_only"], t["no_bound"]) == (38, 37, 1, 0)
    assert [list(x) for x in t["rate_only_directions"]] == [
        ["0x283fdeea", "0xcda86a27"]
    ]  # FusionX at its ceiling


# ======================================================================================
# 20. single_path_bounded
# ======================================================================================


def test_single_path_teaching_graph() -> None:
    t = data("single_path_bounded")["teaching_graph"]
    assert t["request"]["amount_in"] == 10_000_000
    assert t["ordered_candidates"] == ["ab1", "ab2", "ab3", "ac>cb1", "ac>cb2", "ad>db", "ae>eb"]
    verdict = [(x["path"], x["decision"], x.get("output"), x["bound"]) for x in t["log"]]
    assert verdict == [
        ("ab1", "evaluated", 9_871_580, None),
        ("ab2", "pruned", None, 8_973_000),
        ("ab3", "evaluated", 9_871_580, 9_970_000),  # an exact twin: the spot-rate bound is above
        ("ac>cb1", "evaluated", 9_651_976, 9_940_090),
        ("ac>cb2", "pruned", None, 3_936_786),
        ("ad>db", "evaluated", 10_233_347, 10_437_094),
        ("ae>eb", "pruned", None, 7_952_072),
    ]
    assert [x["from_prefix"] for x in t["log"] if x["decision"] == "pruned"] == [0, 1, 0]
    assert [x["incumbent"] for x in t["log"] if x["decision"] == "pruned"] == [
        9_871_580,
        9_871_580,
        10_233_347,
    ]
    assert t["best"] == {"score": 10_233_347, "path": "ad>db"} and t["winner_pools"] == ["ad", "db"]
    assert t["reference"] == {"quotes": 10, "evaluated": 7}
    assert t["bounded"] == {
        "quotes": 6,
        "evaluated": 4,
        "pruned_bound": 3,
        "bound_evaluations": 6,
        "bound_no_bound": 0,
    }


def test_single_path_equal_bound_tie_is_pruned_and_safe() -> None:
    e = data("single_path_bounded")["equal_bound_tie"]
    assert e["pools"] == {"first": ["A", "B", 10**9, 10**9], "tie": ["A", "B", 10**9, 999_500_000]}
    assert (e["request"], e["incumbent"], e["tie_bound"], e["tie_exact"]) == (1000, 996, 996, 996)
    assert e["reference_winner"] == e["bounded_winner"] == ["first"]
    assert (e["pruned_bound"], e["bound_evaluations"], e["bound_no_bound"]) == (1, 1, 0)


def test_single_path_no_bound_on_the_real_fixture() -> None:
    n = data("single_path_bounded")["no_bound"]
    assert 10_000 * 10**6 == 10_000_000_000  # the request's raw amount: 10 000 USDC (6 decimals)
    for label in ("as_is", "stale_tick"):
        assert n[label]["score"] == 10_000_660_449 and n[label]["winner"] == "0x36f66548"
        assert (
            n[label]["enumerated"],
            n[label]["pruned_bound"],
            n[label]["bound_evaluations"],
        ) == (4, 0, 2)
    assert n["as_is"]["bound_no_bound"] == 0 and n["stale_tick"]["bound_no_bound"] == 1
    assert [list(x) for x in n["as_is"]["order"]] == [
        ["0x1bc31bd8", "failed"],
        ["0x368b1480", "evaluated"],
        ["0x36f66548", "evaluated"],
        ["0x69a707d8", "evaluated"],
    ]
    assert list(n["stale_tick"]["order"][2]) == ["0x36f66548", "evaluated, no bound"]


def test_single_path_tracked_fixture_equals_the_reference_and_the_oracle() -> None:
    f = data("single_path_bounded")["tracked_fixture"]
    assert f == {
        "cases": 96,
        "identical": 96,
        "pruned_bound": 160,
        "quotes_reference": 582,
        "quotes_bounded": 422,
        "cases_with_a_prune": 68,
    }


# ======================================================================================
# 21. incremental_graph_bounded
# ======================================================================================


def test_chunk_teaching_graph() -> None:
    t = data("incremental_graph_bounded")["teaching"]
    assert t["request"]["amount_in"] == 400_000 and t["chunks"] == 4
    assert [(s["pools"], s["marginal"]) for s in t["sequence"]] == [
        (["ac", "cb"], 91_860),
        (["ab1"], 90_661),
        (["ab2"], 79_799),
        (["ab1"], 75_588),
    ]
    assert t["gross"] == 91_860 + 90_661 + 79_799 + 75_588 == 337_908
    assert t["counts"] == {"scored": 20, "bound_evaluations": 16, "pruned_bound": 6}
    assert t["bounded"] == {"pruned_bound": 6, "bound_evaluations": 16, "bound_no_bound": 0}
    table = {
        d["chunk"]: {
            c["path"]: (c["marginal"] if c["decision"] == "evaluated" else -c["bound"])
            for c in d["candidates"]
        }
        for d in t["decisions"]
    }
    assert table == {
        1: {"ab1": 90_661, "ab2": 79_799, "ac>cb": 91_860, "ac>cb2": -54_234, "ad>db": -86_977},
        2: {"ab1": 90_661, "ab2": 79_799, "ac>cb": 57_520, "ac>cb2": -45_218, "ad>db": -86_977},
        3: {"ab1": 75_588, "ab2": 79_799, "ac>cb": 57_520, "ac>cb2": -45_218, "ad>db": 69_642},
        4: {"ab1": 75_588, "ab2": 57_049, "ac>cb": 57_520, "ac>cb2": -45_218, "ad>db": 69_642},
    }
    prefixes = [
        c["from_prefix"]
        for d in t["decisions"]
        for c in d["candidates"]
        if c["decision"] == "pruned"
    ]
    assert prefixes == [1, 0, 1, 0, 1, 1]  # `ac>cb2` bounds start from the memoized prefix `ac`


def test_chunk_slack_is_required() -> None:
    s = data("incremental_graph_bounded")["slack"]
    assert s["pools"] == {"p0": ["A", "B", 1024, 1334], "p1": ["A", "B", 1023, 1442]}
    assert (s["request"]["amount_in"], s["chunks"], s["gross_exact"]) == (106, 40, 134)
    assert s["with_slack"] == {
        "scored": 80,
        "bound_evaluations": 40,
        "pruned_bound": 1,
        "gross": 134,
    }
    assert s["without_slack"] == {
        "scored": 80,
        "bound_evaluations": 40,
        "pruned_bound": 28,
        "gross": 133,
    }
    assert s["first_wrong_chunk"] == {"chunk": 6, "exact": ["p1"], "slack_free": ["p0"]}
    assert s["factory"] == {"pruned_bound": 1, "bound_evaluations": 40, "bound_no_bound": 0}


def test_chunk_tie_and_equality() -> None:
    t = data("incremental_graph_bounded")["tie"]
    assert t["pools"]["p3"] == ["A", "B", 10**12, 998_500_000_000] and t["request"] == 1001
    assert [(c["path"], c["decision"], c.get("marginal"), c["bound"]) for c in t["candidates"]] == [
        ("p1", "evaluated", 997, None),
        ("p2", "evaluated", 997, 998),  # tie: bound above the choice
        ("p3", "pruned", None, 997),
        ("p4", "pruned", None, 989),  # equality is skipped
    ]
    assert (t["choice"], t["pruned_bound"], t["bound_evaluations"]) == (997, 2, 3)


def test_chunk_failed_prefix_is_not_bounded() -> None:
    f = data("incremental_graph_bounded")["failed_prefix"]
    assert [(c["path"], c["decision"]) for c in f["decisions"]] == [
        ("ab", "evaluated"),
        ("ovf>cb1", "failed"),
        ("ovf>cb2", "failed"),
    ]
    assert f["decisions"][0]["marginal"] == 996 and f["request"] == 1000
    assert f["counts"] == {
        "scored": 3,
        "bound_evaluations": 1,
    }  # not 2: the memoized failure is free
    assert f["marginal_failures"] == {"reverted": 1}


def test_chunk_p0_without_the_retained_candidate_pruning_is_off() -> None:
    p = data("incremental_graph_bounded")["p0"]
    assert (p["request"], p["per_leg"]) == (150, 75)
    one, two = p["max_splits_1"], p["max_splits_2"]
    assert (one["path_split_status"], one["p0"], one["bound_evaluations"]) == ("no_route", False, 0)
    assert (two["path_split_status"], two["p0"], two["bound_evaluations"]) == ("ok", True, 1)
    assert one["gross"] == two["gross"] == 288_022_822 and one["plan"] == two["plan"] == [
        "m1",
        "m2",
    ]
    assert (one["chosen_source"], two["chosen_source"]) == ("incremental_graph", "direct_split")
    assert one["pruned_bound"] == two["pruned_bound"] == 0


def test_chunk_domain_and_rate_only() -> None:
    d = data("incremental_graph_bounded")["domain"]
    assert 17 * 10**37 <= 2**127 < 18 * 10**37  # "about 1.7e38"
    assert d["below"] == {
        "amount": 10**37,
        "amount_over_2_127": False,
        "pruned_bound": 1,
        "bound_evaluations": 1,
        "bound_no_bound": 0,
    }
    assert d["above"] == {
        "amount": 10**39,
        "amount_over_2_127": True,
        "pruned_bound": 0,
        "bound_evaluations": 1,
        "bound_no_bound": 1,
    }
    r = data("incremental_graph_bounded")["rate_only"]
    assert r["chunk"] == {"pruned_bound": 0, "bound_evaluations": 2, "bound_no_bound": 2}
    assert r["single_path"] == {"pruned_bound": 0, "bound_evaluations": 1, "bound_no_bound": 0}
    assert (r["rate_mantissa_1dp"], r["rate_exponent"]) == ("3.4", 38)


def test_chunk_tracked_fixture() -> None:
    assert data("incremental_graph_bounded")["tracked_fixture"] == {
        "cases": 96,
        "identical": 96,
        "paths_scored": 4418,
        "pruned_bound": 1233,
        "bound_evaluations": 2922,
        "p0": 96,
    }


# ======================================================================================
# 22. metis_history_bounded
# ======================================================================================


def test_metis_chain_m2_acts_under_the_preset() -> None:
    c = data("metis_history_bounded")["chain"]
    assert c["gate"] == ["open", True] and c["rule"] == "M1+M2"
    assert c["m2"] == {"active": True, "gate": "open"} and c["bound_table_cost"] == 12
    assert c["relaxations"] == {"reference": 12, "bounded": 9}
    assert c["counters"] == {"pruned_bound": 3, "bound_evaluations": 3, "bound_no_bound": 0}
    assert [(k["rule"], k["label_path"], k["bound"], k["best"]) for k in c["m2_skips"]] == [
        ("M2", "ab>bc", 2_194_989, 3_216_439),
        ("M2", "ab>bc", 2_194_989, 3_015_978),
        ("M2", "ab>bc", 2_194_989, 2_833_691),
    ]
    assert [(k["rule"], k["label_path"], k["bound"]) for k in c["m1_only_oracle"]["skipped"]] == [
        ("M1", "ab>bc>cd", 2_190_165),
        ("M1", "ab>bc>cd", 2_190_165),
        ("M1", "ab>bc>cd", 2_190_166),
    ]
    assert c["m1_only_oracle"]["relaxations"] == 12
    assert [(s["pools"], s["marginal"]) for s in c["sequence"]] == [
        (["ad"], 3_216_439),
        (["ad"], 3_015_978),
        (["ad"], 2_833_691),
    ]
    assert c["gross"] == 3_216_439 + 3_015_978 + 2_833_691 == 9_066_108


def test_metis_frontier_cap_the_gate_refuses_and_forcing_it_changes_the_plan() -> None:
    f = data("metis_history_bounded")["frontier_cap"]
    assert f["options"] == {
        "dominance": "off",
        "max_labels_per_signature": 1,
        "max_frontier_labels": 3,
    }
    assert (f["gate"], f["chunks"], f["reference_gross"]) == ("closed:frontier", 24, 20_600)
    assert f["bounded"] == {
        "pruned_bound": 118,
        "bound_evaluations": 212,
        "bound_no_bound": 0,
        "rule": "M1",
        "frontier_refusals": 28,
    }
    assert f["relaxations"] == 432
    assert f["forced_m2"] == {
        "gross": 20_611,
        "frontier_refusals": 24,
        "pruned_bound": 124,
        "bound_table_cost": 30,
    }
    assert f["forced_m2"]["gross"] > f["reference_gross"]  # a better plan: not the reference
    assert (f["first_chunk"]["pools"], f["first_chunk"]["marginal"]) == (["ab_1", "db_2"], 862)


def test_metis_gate_sweep_on_the_tracked_fixture() -> None:
    s = data("metis_history_bounded")["gate_sweep"]
    assert s["preset"] == {
        "cases": 96,
        "identical": 96,
        "m2_active": 0,
        "gates": {"open (no label can exist)": 84, "closed:dominance": 12},
    }
    assert s["dominance_off"] == {
        "cases": 96,
        "identical": 96,
        "m2_active": 12,
        "gates": {"open (no label can exist)": 84, "open": 12},
    }


def test_metis_p0() -> None:
    p = data("metis_history_bounded")["p0"]
    assert p == {
        "p0": False,
        "m2": {"active": False, "gate": "n/a"},
        "plan": ["m1", "m2"],
        "gross": 288_022_822,
    }


# ======================================================================================
# the roster, the presets and the section 11 request
# ======================================================================================


def test_roster_is_seventeen_and_the_presets_are_option_identical() -> None:
    r = data("roster")
    assert len(r["all_roster"]) == 17
    assert r["all_roster"][-3:] == [
        "single_path_bounded",
        "incremental_graph_bounded",
        "metis_history_bounded",
    ]
    assert r["bounded_additions"] == r["all_roster"][-3:]
    assert len(r["profile_mode_roster"]) == 6 and r["saved_profile_equal_to_source"]
    assert r["settings_sha256_prefix"] == "183bb1ff"
    files = r["preset_files"]
    expected = {"dominance": "history", "max_labels_per_signature": 1, "max_frontier_labels": 1024}
    assert (
        files["metis_history"]["options"] == files["metis_history_bounded"]["options"] == expected
    )
    assert files["metis_history"]["algorithm"] == "metis_history"
    assert files["metis_history_bounded"]["algorithm"] == "metis_history_bounded"
    assert files["metis_history"]["key"] != files["metis_history_bounded"]["key"]


def test_section_11_request_through_the_bounded_rows() -> None:
    import yaml

    profile = yaml.safe_load((ROOT / "config" / "daily_gross.yaml").read_text(encoding="utf-8"))
    assert profile["graph"]["chunks"] == 200 and profile["search"]["max_hops"] == 2
    assert 10_000 - 30 == 9970
    w = data("roster")["walkthrough"]
    assert w == {
        "single_path_bounded": {
            "score": 10_000_660_449,
            "quotes_counted": 4,
            "pruned_bound": 0,
            "bound_evaluations": 2,
            "bound_no_bound": 0,
        },
        "incremental_graph_bounded": {
            "score": 10_000_663_447,
            "quotes_counted": 264,
            "pruned_bound": 0,
            "bound_evaluations": 400,
            "bound_no_bound": 0,
        },
        "metis_history_bounded": {
            "score": 10_000_663_447,
            "quotes_counted": 264,
            "pruned_bound": 0,
            "bound_evaluations": 400,
            "bound_no_bound": 0,
        },
    }


# ======================================================================================
# the checks have teeth
# ======================================================================================


def test_a_wrong_expectation_is_reported() -> None:
    m = examples()
    with pytest.raises(m.ExampleError, match="got 1, expected 2"):
        m.equal(1, 2, "demo")


def test_an_underestimating_bound_helper_fails_the_examples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: the CPMM rate shrunk to a quarter (an underestimate). The bound examples and the
    single-path examples, whose expectations are the pinned hand rates, the module's own rates
    and the reference solver, must fail."""
    m = examples()
    real = bounds_module._cpmm

    def quarter(state: Any, token_in: str) -> OutputBound | None:
        got = real(state, token_in)
        return None if got is None else OutputBound(got.rate / 4, got.slack)

    monkeypatch.setattr(bounds_module, "_cpmm", quarter)
    with pytest.raises(m.ExampleError):
        m.example_bounds()
    with pytest.raises(m.ExampleError):
        m.example_single_path_bounded()
    with pytest.raises(m.ExampleError):
        m.example_incremental_graph_bounded()


def test_a_refusal_turned_into_a_prune_fails_the_examples(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutations: (a) the chunk slack dropped from the hop bound -- the slack, tie and teaching
    refusals become prunes; (b) the structural gate forced open -- the `closed:frontier` refusal
    becomes an M2 prune that changes the plan."""
    m = examples()

    def without_slack(table: Any, edge: Any, committed: int, amount: int) -> int | None:
        bound = table.get((edge.pool_id, edge.token_in))
        if (
            bound is None
            or bound.slack is None
            or committed + amount > bounds_module.CHUNK_DOMAIN_MAX
        ):
            return None
        return int((bound.rate * amount).__floor__())

    with monkeypatch.context() as patch:
        patch.setattr(chunk_pruning, "hop_bound", without_slack)
        with pytest.raises(m.ExampleError):
            m.example_incremental_graph_bounded()
    with monkeypatch.context() as patch:
        patch.setattr(chunk_pruning, "m2_gate", lambda *a, **k: ("open", True))
        with pytest.raises(m.ExampleError):
            m.example_metis_history_bounded()


# ======================================================================================
# offline, and every number of the guide is backed
# ======================================================================================


def test_the_examples_are_offline_and_need_no_data_directory() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "data/corpus" not in source and "ROUTER_" not in source and "os.environ" not in source
    for needed in (
        "tests/fixtures/corpus/bundle",
        "tests/fixtures/routing/cpmm_graph",
        "docs/references/research-022/fixtures/hand_cases.json",
    ):
        assert (ROOT / needed).exists(), needed
    imported = set(re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, flags=re.M))
    assert not imported & {"requests", "urllib", "httpx", "socket", "web3", "dune_client"}


# structural constants of the proofs and the identifiers the chapters cite; none is an output.
STRUCTURAL = {
    # exponents of the proofs (output-bounds.md): the CL / LB fixed-point scales and the guards
    "112",
    "127",
    "128",
    "192",
    "218",
    "256",
    # tracker issues and pull requests the chapters name (WHI-xxxx)
    "1449",
    "1561",
    "1562",
    "1597",
    "1598",
    "1599",
    "1600",
    "1601",
    "1602",
    # corpus facts cited from other documents (history-labels.md section 1, corpus.md section 3,
    # jupiter-metis-challenge.md section 6): 143 pools; the cProfile attribution of
    # latency-optimization-research.md section 3.1: 98.9 % and 0.05 %
    "143",
    "98.9",
    "0.05",
    # the human unit of a raw amount asserted in `test_single_path_no_bound_on_the_real_fixture`
    # (10 000 USDC = 10_000 * 10**6 raw) and of the section 11 request
    "10000",
    # 10**4 - 30 bps = 9970, the keep factor of the pinned derivation 9970 * 500 / (10**4 * 2000);
    # `daily_gross.yaml` runs 200 chunks (asserted in the section 11 test below)
    "9970",
    "200",
    # decimals asserted in this file: 2**127 is about 1.7e38 (the domain test), the tie
    # pool's rate is 0.99650... (module check), the R2 rate 0.997 = 997/1000, and 0.9965
    "1.7",
    "0.997",
    "0.99650",
}


def _numbers(text: str) -> set[str]:
    """Numeric tokens of at least three digits or with a decimal point, thousands separators
    removed (`9,871,580`, `9\\,871\\,580` and `9871580` are one number). Hex words and
    identifiers fused to letters are not numbers."""
    text = re.sub(r"^\s*#+ .*$", " ", text, flags=re.M)  # headings (section numbers)
    text = re.sub(
        r"§§?\s*\d+(?:\.\d+)?(?:\s*(?:,|and|–|-)\s*\d+(?:\.\d+)?)*", " ", text
    )  # section refs
    text = re.sub(r"research-0\d\d", " ", text)  # directory names
    text = re.sub(r"0x[0-9a-fA-F]+", " ", text)
    text = re.sub(r"\b[0-9a-f]{7,}\b", " ", text)
    found = set()
    for token in re.findall(
        r"(?<![\w.])\d+(?:(?:\\,|,)\d{3})+(?![\w])|(?<![\w.])\d+(?:\.\d+)?(?![\w])", text
    ):
        token = token.replace("\\,", "").replace(",", "")
        if len(token.replace(".", "")) >= 3 or "." in token:
            found.add(token)
    return found


def _guide_chapters() -> str:
    text = GUIDE.read_text(encoding="utf-8")
    end = text.find("\n## 23. ")  # WHI-1623's chapter 23 is checked by test_split_polish.py
    body = text[text.index("## 19. Upper-Bound Pruning") : end if end >= 0 else len(text)]
    body = re.sub(r"```.*?```", " ", body, flags=re.S)  # pseudocode and diagrams
    # the implementation maps hold source line numbers, which are anchors rather than results
    return re.sub(r"### \d+\.6 Implementation Map.*?(?=### \d+\.7 )", " ", body, flags=re.S)


def test_every_number_of_the_guide_chapters_is_asserted() -> None:
    """Chapters 19-22 publish only numbers that appear in the data the example module computed
    after asserting its independent expectation, in a literal of this file, or in the short
    structural list above. A number added to the guide without a check fails here."""
    asserted = _numbers(json.dumps(everything(), default=str))
    asserted |= _numbers(Path(__file__).read_text(encoding="utf-8"))
    asserted |= STRUCTURAL
    published = _numbers(_guide_chapters())
    unbacked = sorted(published - asserted)
    assert not unbacked, f"numbers in the guide with no check: {unbacked}"


def test_the_guide_never_claims_a_speedup() -> None:
    """Chapters 19-22 mention speed only to deny the claim or to defer timing to WHI-1602."""
    prose = re.sub(r"`[^`]*`|\]\([^)]*\)", " ", _guide_chapters())  # file names are not claims
    for banned in ("speedup", "speed-up", "faster", "slower", "latency", "x faster"):
        for line in prose.splitlines():
            if banned in line.lower():
                assert re.search(
                    r"\bno\b|\bnot\b|never|nothing|neither|WHI-1602|without|separate|claim",
                    line,
                    re.I,
                ), (banned, line)
    assert "WHI-1602" in GUIDE.read_text(encoding="utf-8")

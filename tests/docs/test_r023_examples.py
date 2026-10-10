"""WHI-1628: the executable worked examples of the 0.2.3 post-processors and the research-023
campaign figures of the routing guide (`docs/examples/routing-algorithms/r023_examples.py`;
guide §§11.5, 23, 24 and the 0.2.3 passages of the header, §1.2, §12 and §13).

The example module already checks every published value against an expectation that does not come
from the code under test (its own fee-free constant-product formula and exhaustive split oracles,
exact protocol quotes on the real fixture, the committed `report-analysis.json` against its
regenerated `report-tables.md` rows). These tests restate the numbers the guide publishes as
literals taken from those independent sources (`research-023/results.md` for the campaign), so a
changed example, factory or analysis fails here too, and they show that the checks have teeth:

* a polish that never accepts an exchange, or a campaign figure changed in the analysis, makes the
  examples fail (`test_a_*_fails_the_examples`);
* every number of the 0.2.3 chapters and of the 0.2.3 passages of the shared sections is backed by
  the example data, a literal of this file, the contract or the committed campaign evidence
  (`test_every_number_of_the_0_2_3_text_is_asserted`), and changing one published figure fails
  (`test_a_changed_published_number_is_caught`);
* every campaign table row of §§23.9 and 24.9 is the row the example module renders from the
  analysis (`test_the_guide_tables_are_the_rendered_rows`).

Since Release 0.2.4 (WHI-1634) the 0.2.3 text is history; the 0.2.4 passages next to it (the
header's 0.2.4 bullet, the inspected-commit 0.2.4 bullet, §13 item 17 and §§23.10 / 24.10) are
checked, with the research-024 evidence, by `tests/docs/test_r024_examples.py`.

Everything is offline: tracked files only, no RPC, Dune, credentials or `data/`. Nothing here
asserts or implies a timing.
"""

from __future__ import annotations

import importlib.util
import json
import re
from decimal import Decimal
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

import main
from benchmark.results import load_case_records
from routing.algorithms import split_polish as sp

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "docs" / "examples" / "routing-algorithms" / "r023_examples.py"
GUIDE = ROOT / "docs" / "references" / "routing-algorithms.md"
R023 = ROOT / "docs" / "references" / "research-023"


@cache
def examples() -> ModuleType:
    spec = importlib.util.spec_from_file_location("r023_examples", SCRIPT)
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


def guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


# ======================================================================================
# 23. split_polish (§23.5)
# ======================================================================================


def test_split_polish_worked_example() -> None:
    s = data("split_polish")
    assert s["pools"] == {"a": [1000, 2000], "b": [1000, 100], "c": [1000, 100]}
    assert s["base"] == {"outputs": [181, 9, 9], "gross": 199}
    assert s["oracle"] == {"gross": 461, "argmax": [[300, 0, 0]]}  # the best split is a corner
    for solver in ("brent", "golden"):
        sequence = s["solvers"][solver]["sequence"]
        assert sequence == [
            {"legs": [["a", 200, 333], ["c", 100, 9]], "gross": 342},
            {"legs": [["a", 300, 461]], "gross": 461},
        ]
    assert s["solvers"]["brent"]["factory_counters"] == {
        "simulations": 92,
        "quotes": 197,
        "accepted": 2,
        "polish_calls": 1,
    }
    assert s["solvers"]["golden"]["factory_counters"] == {
        "simulations": 99,
        "quotes": 210,
        "accepted": 2,
        "polish_calls": 1,
    }
    assert s["caps_golden"] == {50: 199, 80: 342, 140: 461}
    assert s["nominee"] == {
        "base": "incremental_graph",
        "solver": "brent",
        "rounds": 2,
        "tolerance": 0.0001,
        "grid": 10**9,
        "maxiter": 60,
    }


# ======================================================================================
# 24. marginal_activation (§24.5)
# ======================================================================================


def test_marginal_activation_worked_example() -> None:
    m = data("marginal_activation")
    assert (m["request"], m["base_gross"], m["delta"]) == (10**6, 909090, 100)
    assert m["post_plan_proposals"] == {"p": 82, "q": 99}
    assert m["seed"] == {"p": 999900, "q": 100, "gross": 909107}
    assert (m["oracle_best"], m["half_output"], m["second_iteration_proposal"]) == (
        952380,
        476190,
        90,
    )
    for mode, quotes, simulations in (("pf", 203, 72), ("full", 861, 305)):
        run = m["runs"][mode]
        assert run["legs"] == [["p", 500000, 476190], ["q", 500000, 476190]]
        assert run["gross"] == 952380 and run["e1"] == {"gross": 909090, "quotes": 1, "accepted": 0}
        assert run["log"] == [
            [0, 0, "accepted novel_pools=1 reuse=none flow=500000"],
            [1, 0, "no_gain_or_zero_flow"],
            [1, 1, "no_gain_or_zero_flow"],
            [1, -1, "stop_no_candidate"],
        ]
        assert run["factory_counters"] == {
            "activation_quotes": quotes,
            "simulations": simulations,
            "invocations": 3,
        }
    assert m["controls"] == {
        "work_matched": {"gross": 909090, "quotes": 0, "calls_started": 1},
        "call_matched": {"gross": 909090, "quotes": 0, "calls_started": 3},
    }
    assert m["control_target"] == {"invocations": 3, "quotes": 203}


# ======================================================================================
# 25. the §11.5 request, the roster
# ======================================================================================


WALK_ROWS = {
    "incremental_graph": {
        "score": 10000663447,
        "usdt0": "10000.663447",
        "quotes_counted": 264,
        "legs": [["0x36f6", 9950000000, 9950659893], ["0x368b", 50000000, 50003554]],
    },
    "split_polish": {
        "score": 10000663636,
        "usdt0": "10000.663636",
        "quotes_counted": 360,
        "legs": [["0x36f6", 9964149090, 9964809143], ["0x368b", 35850910, 35854493]],
    },
    "marginal_activation": {
        "score": 10000663636,
        "usdt0": "10000.663636",
        "quotes_counted": 588,
        "legs": [["0x36f6", 9964149090, 9964809143], ["0x368b", 35850910, 35854493]],
    },
}


def test_section_11_5_request_through_the_explicit_profile() -> None:
    w = data("walkthrough")
    assert w["all_roster_size"] == 17
    r = w["walkthrough"]
    assert r["bundle"] == {"block": 101082044, "pools": 19}
    assert r["request"] == {"token_in": "USDC", "token_out": "USDT0", "amount_in": 10**10}
    assert r["rows"] == WALK_ROWS
    assert r["base_legs"] == [9950659893, 50003554]  # the §11.1 exact protocol quotes
    assert 9964809143 + 35854493 == 10000663636 and 9964149090 + 35850910 == 10**10
    assert r["gain_raw"] == 10000663636 - 10000663447 == 189 and r["gain_bps_4dp"] == "0.0002"
    assert r["moved_raw"] == 50000000 - 35850910 == 14149090
    assert r["grid_oracle"] == {
        "step_usdc": "0.1",
        "gross": 10000663634,
        "lb_in": 35900000,
        "below_e1": 2,
    }
    assert r["e1_counters"] == {
        "quotes": 96,
        "scope": "fixed_funding_topology",
        "truncated_by": None,
        "polish_calls": 1,
        "simulations": 49,
        "accepted": 1,
        "brent_calls": 2,
    }
    assert r["e2_counters"] == {
        "e1_quotes": 96,
        "activation_quotes": 228,
        "simulations": 74,
        "invocations": 3,
        "log": [
            [0, 0, "no_gain_or_zero_flow"],
            [0, 1, "no_gain_or_zero_flow"],
            [0, 2, "no_gain_or_zero_flow"],
            [0, -1, "stop_no_candidate"],
        ],
        "not_reached": None,
    }
    assert 264 + 96 == 360 and 264 + 96 + 228 == 588
    assert r["delta"] == 10**10 // 10**4 == 1000000
    assert r["terminals"] == [[1000011, ["0x36f6"]], [999996, ["0x368b"]], [14207, ["0x69a7"]]]
    assert r["moe_output_reserve"] == 14410 and r["dead_pool_status"] != "ok"
    assert [s["inputs"] for s in r["plan_split_polish"]] == [
        [["REQUEST", 9964149090]],
        [["REQUEST", 35850910]],
    ]
    assert [s["output"] for s in r["plan_split_polish"]] == ["F1", "F2"]


def test_the_guide_profile_is_the_checked_profile() -> None:
    """The YAML block of §11.5 is exactly the profile the example runs: `daily_gross.yaml`'s
    values, only `algorithms` replaced and the two nominees written out."""
    section = guide().split("### 11.5 ", 1)[1].split("\n---\n", 1)[0]
    (block,) = re.findall(r"```yaml\n(.*?)```", section, flags=re.S)
    shown = yaml.safe_load(block)
    assert (
        shown == data("walkthrough")["walkthrough"]["profile"] == examples().walkthrough_profile()
    )
    source = yaml.safe_load((ROOT / "config" / "daily_gross.yaml").read_text(encoding="utf-8"))
    assert {k: v for k, v in shown.items() if k not in ("algorithms", "algorithm_options")} == {
        k: v for k, v in source.items() if k != "algorithms"
    }
    assert "--strategies profile --details" in section and "tests/fixtures/corpus/bundle" in section


def test_the_quote_command_of_section_11_5(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The §11.5 `quote --strategies profile` command, run as published (worker processes, the
    tracked fixture): its saved records are the rows the example computed in process."""
    profile = tmp_path / "r023-walkthrough.yaml"
    profile.write_text(yaml.safe_dump(examples().walkthrough_profile(), sort_keys=False))
    argv = [
        "quote",
        "--bundle",
        str(ROOT / "tests/fixtures/corpus/bundle"),
        "--profile",
        str(profile),
        "--token-in",
        "USDC",
        "--token-out",
        "USDT0",
        "--amount",
        "10000",
        "--strategies",
        "profile",
        "--details",
        "--quotes-dir",
        str(tmp_path / "q"),
    ]
    assert main.main(argv) == 0
    out = capsys.readouterr().out
    match = re.search(r"\(run (\S+)\)", out)
    assert match
    records = load_case_records(Path(match.group(1)))
    got = {r["algorithm"]: (r["status"], int(r["score"]), r["quotes"]["counted"]) for r in records}
    assert got == {n: ("ok", row["score"], row["quotes_counted"]) for n, row in WALK_ROWS.items()}
    for name in ("split_polish", "marginal_activation"):
        assert f"[{name}] ok" in out
        record = next(r for r in records if r["algorithm"] == name)
        assert record["search"]["base"]["quotes"] == 264
        assert record["search"][name]["base_gross"] == "10000663447"


# ======================================================================================
# 26. campaign figures (report-analysis.json; the literals are research-023/results.md's)
# ======================================================================================


def _hel(c: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return c["hel"], c["mean"], c["p50"], c["p95"], c["families"]


def test_campaign_denominators_statuses_and_dispositions() -> None:
    c = data("campaign")
    assert (c["arms"], c["cases"]) == (23, 302)
    assert c["dispositions"] == {
        "split_polish": "keep_experimental",
        "marginal_activation": "keep_experimental",
    }
    assert c["not_reached"] == {"E2pf-C100": 2, "E2pf-C100-cm": 2, "E2pf-C100-wm": 2}
    assert c["truncated_by"]["E1b-C100"] == {"max_quotes": 2}
    assert max(t.get("max_quotes", 0) for t in c["truncated_by"].values()) == 6
    assert {a for a, t in c["truncated_by"].items() if t.get("max_quotes") == 6} == {
        "E2full-A0",
        "E2pf-C100-cm",
    }
    for q in ("q1", "q2", "q3"):
        for row in c[q].values():
            assert (row["scheduled"], row["zero_baseline"]) == (302, 10)
    c100 = c["q3"]["E2pf-C100 vs E2pf-C100-wm"]
    assert (c100["common_ok"], c100["scored"]) == (299, 289)  # the 2 not_reached rows excluded
    assert c100["transitions"]["not_reached->not_reached"] == 2
    assert c["e2pf_c100_vs_own_base"]["transitions"]["ok->not_reached"] == 2


def test_campaign_q1_q2_q5() -> None:
    c = data("campaign")
    assert {k: _hel(v) for k, v in c["q1"].items()} == {
        "E1b-A0": ("199/92/0", "0.447", "0.072", "1.915", "26 / 0"),
        "E1b-C100": ("205/86/0", "0.329", "0.074", "1.608", "27 / 0"),
        "E1b-M4": ("214/77/0", "0.494", "0.129", "2.383", "29 / 0"),
        "E1b-S4": ("214/77/0", "0.529", "0.112", "2.422", "29 / 0"),
        "E1b-REP": ("199/92/0", "0.454", "0.071", "2.336", "26 / 0"),
        "E1b-PS": ("181/110/0", "0.224", "0.018", "0.865", "22 / 0"),
    }
    assert {k: _hel(v) + (v["p5"],) for k, v in c["q2"].items()} == {
        "E1b-A0 vs C100": ("84/85/122", "0.472", "0.000", "3.856", "8 / 18", "-1.422"),
        "E1b-A0 vs C200": ("63/79/149", "0.682", "-0.004", "9.015", "7 / 19", "-1.768"),
        "E1g-A0 vs C100": ("83/86/122", "0.468", "0.000", "3.854", "8 / 18", "-1.418"),
        "E1g-A0 vs C200": ("61/80/150", "0.678", "-0.006", "9.019", "6 / 20", "-1.775"),
        "E1b-A0 vs E1g-A0": ("107/110/74", "0.004", "0.000", "0.019", "15 / 8", "-0.010"),
    }
    assert c["q5"] == {
        "golden_over_brent_p50": "1.207",
        "golden_over_brent_p95": "1.432",
        "polish_quotes": {"brent": 2367236, "golden": 2983922},
    }
    assert c["physical"]["E1b-A0"] == {
        "quotes_executed": "1.276",
        "cl_swap_steps": "1.014",
        "lb_bins_swapped": "1.072",
        "cl_initialized_ticks_crossed": "1.070",
    }


def test_campaign_q3_q4_and_the_activation() -> None:
    c = data("campaign")
    assert {k: _hel(v) for k, v in c["q3"].items()} == {
        "E2pf-A0 vs E2pf-A0-wm": ("209/78/4", "0.210", "0.060", "0.948", "28 / 1"),
        "E2pf-A0 vs E2pf-A0-cm": ("202/79/10", "0.195", "0.053", "0.945", "28 / 0"),
        "E2full-A0 vs E2full-A0-wm": ("208/76/7", "0.257", "0.059", "1.027", "28 / 1"),
        "E2full-A0 vs E2full-A0-cm": ("208/78/5", "0.258", "0.059", "1.027", "28 / 0"),
        "E2pf-C100 vs E2pf-C100-wm": ("195/88/6", "0.146", "0.044", "0.564", "28 / 0"),
        "E2pf-C100 vs E2pf-C100-cm": ("186/87/16", "0.130", "0.040", "0.559", "27 / 0"),
    }
    q4 = {t: {r: (v["hel"], v["mean"]) for r, v in row.items()} for t, row in c["q4"].items()}
    assert q4 == {
        "E2pf-A0": {
            "C100": ("170/56/65", "0.685"),
            "C200": ("116/56/119", "0.895"),
            "M4": ("143/52/96", "-0.747"),
            "S4": ("141/51/99", "-1.986"),
            "REP": ("233/56/2", "0.622"),
        },
        "E2pf-C100": {
            "C100": ("235/54/0", "0.479"),
            "C200": ("175/54/60", "0.691"),
            "M4": ("156/50/83", "-0.962"),
            "S4": ("150/49/90", "-2.210"),
            "REP": ("212/54/23", "0.416"),
        },
        "E2full-A0": {
            "C100": ("183/56/52", "0.748"),
            "C200": ("134/56/101", "0.958"),
            "M4": ("146/52/93", "-0.684"),
            "S4": ("145/51/95", "-1.923"),
            "REP": ("233/56/2", "0.685"),
        },
    }
    assert c["q4"]["E2pf-C100"]["C200"]["families"] == "26 / 3"
    assert sorted(
        float(v["p5"]) for row in c["q4"].values() for r, v in row.items() if r in ("M4", "S4")
    ) == [-10.672, -10.664, -9.758, -9.738, -9.635, -9.635]
    assert sorted(c["control_gain_over_e1"].values()) == [
        "0.002",
        "0.003",
        "0.018",
        "0.018",
        "0.018",
        "0.019",
    ]
    assert c["pf_a0_activation"] == {
        "cases_activated": 210,
        "accepted": 395,
        "dag_cycle": 111,
        "no_gain_or_zero_flow": 159,
        "stop_no_candidate": 116,
        "budget_before_validation": 0,
    }
    assert c["physical"]["E2pf-A0"]["quotes_executed"] == "1.305"
    assert (
        c["physical"]["E2pf-A0"]["cl_swap_steps"],
        c["physical"]["E2pf-A0"]["lb_bins_swapped"],
    ) == (
        "1.046",
        "1.079",
    )
    assert [
        c["physical"]["E2full-A0"][k]
        for k in ("quotes_executed", "cl_swap_steps", "lb_bins_swapped")
    ] == ["2.260", "1.448", "1.283"]


def _table_problems(text: str) -> list[str]:
    """The campaign rows of §§23.9 / 24.9 that are not, verbatim, the rendered rows, in the
    chapter that owns them, plus any other campaign row."""
    ch23 = _between(text, "## 23. ", "### 23.10 ")  # §23.10 / §24.10: test_r024_examples.py
    ch24 = _between(text, "## 24. ", "### 24.10 ")
    rows = data("campaign")["guide_rows"]
    missing = [r for r in rows["q1"] + rows["q2"] if r not in ch23]
    missing += [r for r in rows["q3"] + rows["q4"] if r not in ch24]
    published = [
        line
        for line in (ch23 + ch24).splitlines()
        if re.match(r"\| [^|]+ \| (\d+ / 302|\d+/\d+/\d+, )", line)
    ]
    return missing + [line for line in published if line not in sum(rows.values(), [])]


def test_the_guide_tables_are_the_rendered_rows() -> None:
    """Each campaign table row of §§23.9 and 24.9 is, verbatim, the row the example module renders
    from `report-analysis.json`, in the chapter that owns it, and there is no other."""
    assert _table_problems(guide()) == []
    assert len(sum(data("campaign")["guide_rows"].values(), [])) == 20


# ======================================================================================
# the checks have teeth
# ======================================================================================


def test_a_wrong_expectation_is_reported() -> None:
    m = examples()
    with pytest.raises(m.ExampleError, match="got 1, expected 2"):
        m.equal(1, 2, "demo")


def test_a_polish_that_never_accepts_fails_the_examples(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: the line search never finds a better point (E1 keeps every base). The §23.5,
    §24.5 and §11.5 examples, whose expectations are the hand formula, the exhaustive oracle and
    exact protocol quotes, must fail."""
    m = examples()
    monkeypatch.setattr(sp, "line_search", lambda *a, **k: None)
    with pytest.raises(m.ExampleError):
        m.example_split_polish()
    with pytest.raises(m.ExampleError):
        m.example_marginal_activation()
    with pytest.raises(m.ExampleError):
        m.example_walkthrough()


def test_a_changed_campaign_figure_fails_the_examples(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: one mean of the analysis moved by 0.001 bps; it no longer matches its
    regenerated `report-tables.md` row."""
    m = examples()
    analysis = json.loads(json.dumps(m.ANALYSIS))
    (c,) = [
        x for x in analysis["comparisons"] if (x["candidate"], x["baseline"]) == ("E1b-A0", "A0")
    ]
    c["bps"]["mean"] += 0.001
    monkeypatch.setattr(m, "ANALYSIS", analysis)
    with pytest.raises(m.ExampleError, match="report-tables.md row"):
        m.example_campaign()


# ======================================================================================
# offline, and every number of the 0.2.3 text is backed
# ======================================================================================


def test_the_examples_are_offline_and_need_no_data_directory() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "data/corpus" not in source and "ROUTER_" not in source and "os.environ" not in source
    for needed in ("tests/fixtures/corpus/bundle", "docs/references/research-023/campaign"):
        assert (ROOT / needed).exists(), needed
    imported = set(re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, flags=re.M))
    assert not imported & {"requests", "urllib", "httpx", "socket", "web3", "dune_client"}


# identifiers and request constants the 0.2.3 text cites; none is a result.
STRUCTURAL = {
    # the tracker issues (WHI-xxxx) the text names
    "1562",
    "1623",
    "1624",
    "1626",
    "1627",
    # the frozen block of the fixture and the human request amount (10 000 USDC = 10**10 raw),
    # both asserted in the section 11.5 test above
    "101082044",
    "10000",
}


def _numbers(text: str) -> set[str]:
    """Numeric tokens of at least three digits or with a decimal point, thousands separators
    removed. Hex words, commit hashes, section references and identifiers fused to letters are
    not numbers. Unlike `test_r022_examples._numbers`, a long run of decimal digits (a raw amount
    such as 10000663636) is a number, not a hex word, so the raw amounts are checked too."""
    text = re.sub(r"^\s*#+ .*$", " ", text, flags=re.M)  # headings (section numbers)
    text = re.sub(r"§§?\s*\d+(?:\.\d+)?(?:\s*(?:,|and|–|-)\s*\d+(?:\.\d+)?)*", " ", text)
    text = re.sub(r"[Ss]ections?\s+\d+(?:\s*(?:,|and|–|-)\s*\d+)*", " ", text)
    text = re.sub(r"research-0\d\d", " ", text)
    text = re.sub(r"0x[0-9a-fA-F]+", " ", text)
    text = re.sub(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,}\b", " ", text)  # pure digits stay numbers
    found = set()
    for token in re.findall(
        r"(?<![\w.])\d+(?:(?:\\,|,)\d{3})+(?![\w])|(?<![\w.])\d+(?:\.\d+)?(?![\w])", text
    ):
        token = token.replace("\\,", "").replace(",", "")
        if len(token.replace(".", "")) >= 3 or "." in token:
            found.add(token)
    return found


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i : text.index(end, i + len(start))]


def _r023_text(text: str) -> str:
    """The 0.2.3 chapters and every 0.2.3 passage of the shared sections."""
    parts = [
        _between(text, "- the **0.2.3 post-processors**", "- the **0.2.4 roster**"),
        _between(text, "- §§23–24, §11.5 and the 0.2.3 parts", "- The 0.2.4 parts"),
        _between(text, "All numeric traces", "\n---\n"),
        *(
            line
            for line in _between(text, "### 1.2 ", "### 1.3 ").splitlines()
            if re.match(r"- \*\*Section (11|12|23|24):", line)
        ),
        _between(text, "The five 0.2.1 chapters, the four 0.2.2", "### 1.3 "),
        re.sub(r"```yaml\n.*?```", " ", _between(text, "### 11.5 ", "\n---\n"), flags=re.S),
        _between(text, "The two 0.2.3 post-processors (§§23–24)", "### 12.2 "),
        *(
            line
            for line in _between(text, "### 12.2 ", "## 13. ").splitlines()
            if "split_polish" in line or "marginal_activation" in line or "0.2.3" in line
        ),
        _between(text, "The two 0.2.3 rows count", "### 12.3 "),
        _between(text, "14. **Literal Replay:**", "15. **"),
        _between(text, "16. **The 0.2.3 Post-Processors", "17. **Release 0.2.4"),
        _between(text, "## 23. ", "### 23.10 "),  # chapters 23 and 24 up to their 0.2.4 sections
        _between(text, "## 24. ", "### 24.10 "),
    ]
    return re.sub(r"```(?:python|mermaid)\n.*?```", " ", "\n".join(parts), flags=re.S)


def _backed() -> set[str]:
    asserted = _numbers(json.dumps(everything(), default=str))
    asserted |= _numbers(Path(__file__).read_text(encoding="utf-8"))
    for source in ("contract.md", "campaign/report-analysis.json", "campaign/report-tables.md"):
        asserted |= _numbers((R023 / source).read_text(encoding="utf-8"))
    return asserted | STRUCTURAL


def test_every_number_of_the_0_2_3_text_is_asserted() -> None:
    """The 0.2.3 chapters (§§23-24, pseudocode and diagrams excepted) and the 0.2.3 passages of
    the header, §1.2, §11.5 (its trace block included), §12 and §13 publish only numbers that the
    example module computed after checking its independent expectation, that a literal of this
    file states, or that the contract or the committed campaign evidence states."""
    published = _numbers(_r023_text(guide()))
    assert len(published) > 150  # not vacuous
    unbacked = sorted(published - _backed())
    assert not unbacked, f"numbers in the 0.2.3 guide text with no check: {unbacked}"


def test_a_changed_published_number_is_caught() -> None:
    """Mutation: one published figure of each kind, changed in the guide, is reported by the
    number check or the table-row check."""
    text = guide()
    assert not _numbers(_r023_text(text)) - _backed() and not _table_problems(text)
    # (the guide text, the figure in it); the changed figure is computed, so that it is never a
    # literal of this file (which backs the number check)
    for old, figure in (
        ("| 199/92/0 | 0.447 |", "0.447"),  # a campaign table mean
        ("| 63/79/149 |", "149"),  # a campaign H/E/L count
        ("**10000663636**", "10000663636"),  # the §11.5 gross
        ("228 quotes", "228"),  # a factory counter
        ("1.276×", "1.276"),
    ):  # a work-pass ratio
        assert old in text, old
        step = "0.001" if "." in figure else "1"
        changed = str(Decimal(figure) + Decimal(step))
        mutated = text.replace(old, old.replace(figure, changed))
        assert _numbers(_r023_text(mutated)) - _backed() or _table_problems(mutated), old


def test_the_0_2_3_text_never_claims_a_speedup() -> None:
    prose = re.sub(r"`[^`]*`|\]\([^)]*\)", " ", _r023_text(guide()))
    for banned in ("speedup", "speed-up", "faster", "slower", "latency"):
        for line in prose.splitlines():
            if banned in line.lower():
                assert re.search(r"\bno\b|\bnot\b|never|nothing|neither|claim", line, re.I), line

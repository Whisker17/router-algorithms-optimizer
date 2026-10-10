"""WHI-1634: the 0.2.4 parts of the routing guide and of the documents that list the roster.

`docs/examples/routing-algorithms/r024_examples.py` checks every published 0.2.4 value against an
expectation that does not come from the code under test (exact protocol quotes and a grid oracle on
the tracked fixture, the presets' own files, the pinned selection and campaign analyses against
their generated tables). These tests restate the numbers the guide publishes as literals taken from
those independent sources (`research-024/selection.md` and `research-024/results.md` for the
evidence), so a changed example, factory or analysis fails here too, and they show that the checks
have teeth:

* the §11 rows: the two 0.2.4 rows of the 19-row table are the rendered rows, the published `quote`
  command prints them with their preset provenance, and their plans are §11.2's;
* §§23.10 / 24.10: every campaign table row is the row rendered from `report-analysis.json`, and
  every number of the 0.2.4 text is backed (`test_every_number_of_the_0_2_4_text_is_asserted`); a
  changed published figure fails (`test_a_changed_published_number_is_caught`), one per section;
* timing appears only as `results.md` §4.3 states it (`test_the_timing_statement_is_verbatim`,
  `test_the_0_2_4_text_never_claims_a_speedup`);
* the guide, `strategy-groups.md`, `single-request.md` and `DESIGN.md` describe the same 19-row
  roster, and `config/README.md` describes every campaign and preset directory.

Everything is offline: tracked files only, no RPC, Dune, credentials or `data/`.
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

import main
from benchmark.results import load_case_records
from routing.algorithms import split_polish as sp

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "docs" / "examples" / "routing-algorithms" / "r024_examples.py"
GUIDE = ROOT / "docs" / "references" / "routing-algorithms.md"
R023 = ROOT / "docs" / "references" / "research-023"
R024 = ROOT / "docs" / "references" / "research-024"


@cache
def examples() -> ModuleType:
    spec = importlib.util.spec_from_file_location("r024_examples", SCRIPT)
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


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i : text.index(end, i + len(start))]


# ======================================================================================
# 27. the 19-row roster and the two 0.2.4 rows of §11 (the fixture)
# ======================================================================================


def test_roster_presets_and_provenance() -> None:
    w = data("walkthrough")
    r = w["roster"]
    assert (r["size"], r["frozen_size"], r["last_two"]) == (
        19,
        17,
        ["split_polish", "marginal_activation"],
    )
    pins = w["presets"]
    assert {n: (p["key"], p["version"], p["sha256"][:8]) for n, p in pins.items()} == {
        "split_polish": ("R024-P01-split_polish", 1, "bdaba97b"),
        "marginal_activation": ("R024-P02-marginal_activation", 1, "553ba71c"),
    }  # the file sha256 of selection.md §5
    assert pins["split_polish"]["options"] == {
        "base": "metis_inspired",
        "solver": "golden",
        "rounds": 2,
        "tolerance": 1e-05,
        "grid": 1000000000,
        "maxiter": 60,
    }
    assert pins["split_polish"]["decimals"] == {"tolerance": "0.00001"}
    assert pins["marginal_activation"]["options"] == {
        "base": "incremental_graph",
        "solver": "brent",
        "rounds": 2,
        "tolerance": 0.0001,
        "grid": 1000000000,
        "maxiter": 60,
        "mode": "pf",
        "activations": 4,
        "top_k": 9,
        "delta_share": 0.001,
        "seed_share": 0.0001,
        "arm": "treatment",
    }
    assert {n: o["settings"][:8] for n, o in r["options"].items()} == {
        "split_polish": "04b363a7",
        "marginal_activation": "6aec619d",
    }
    assert r["params"]["split_polish"] == {
        "max_hops": 2,
        "max_splits": 4,
        "percent_step": 5,
        "chunks": 200,
        "label_hops": 4,
        "label_pruning": True,
    }
    assert r["params"]["marginal_activation"] == {
        "max_hops": 2,
        "max_splits": 4,
        "percent_step": 5,
        "chunks": 200,
    }


def test_the_two_0_2_4_rows_on_the_fixture() -> None:
    rows = data("walkthrough")["rows"]
    assert (rows["base_gross"], rows["grid_oracle"]) == (10000663447, 10000663634)
    e1, e2 = rows["split_polish"], rows["marginal_activation"]
    assert (e1["score"], e1["usdt0"], e1["quotes_counted"], e1["own_quotes"]) == (
        10000663635,
        "10000.663635",
        376,
        112,
    )
    assert e1["legs"] == [["0x36f6", 9964137490, 9964797543], ["0x368b", 35862510, 35866092]]
    assert 9964797543 + 35866092 == 10000663635 and 9964137490 + 35862510 == 10**10
    assert (e1["gain_raw"], e1["moved_raw"]) == (188, 14137490)
    assert e1["factory_counters"] == {
        "polish_calls": 1,
        "simulations": 57,
        "golden_iters": 48,
        "accepted": 1,
        "scope": "fixed_funding_topology",
        "truncated_by": None,
    }
    assert (e2["score"], e2["usdt0"], e2["quotes_counted"], e2["own_quotes"]) == (
        10000663636,
        "10000.663636",
        588,
        324,
    )
    assert e2["legs"] == [["0x36f6", 9964149090, 9964809143], ["0x368b", 35850910, 35854493]]
    assert 264 + 112 == 376 and 264 + 96 + 228 == 588 and e2["gain_raw"] == 189
    assert e2["factory_counters"] == {
        "e1_quotes": 96,
        "e1_accepted": 1,
        "e1_simulations": 49,
        "activation_quotes": 228,
        "activation_simulations": 74,
        "invocations": 3,
        "log": [
            [0, 0, "no_gain_or_zero_flow"],
            [0, 1, "no_gain_or_zero_flow"],
            [0, 2, "no_gain_or_zero_flow"],
            [0, -1, "stop_no_candidate"],
        ],
        "not_reached": None,
    }
    assert e2["delta"] == 10**10 // 1000 == 10000000
    assert e2["terminals"] == [[10000111, ["0x36f6"]], [9999978, ["0x368b"]], [14389, ["0x69a7"]]]
    assert e2["dead_pool_status"] != "ok" and rows["moe_output_reserve"] == 14410
    assert rows["split_polish_vs_marginal_activation_raw"] == 1
    assert (e1["base_quotes"], e2["base_quotes"]) == (264, 264)


def test_the_section_11_rows_and_plans_are_the_checked_ones() -> None:
    text = guide()
    table = _between(text, "### 11.1 ", "### 11.2 ")
    rows = data("walkthrough")["rows"]
    for name in ("split_polish", "marginal_activation"):
        (line,) = [x for x in table.splitlines() if x.startswith(f"| `{name}` |")]
        assert line.startswith(rows[name]["table_row"]), line
        steps = [s["inputs"][0][1] for s in rows[name]["plan"]]
        assert [s["output"] for s in rows[name]["plan"]] == ["F1", "F2"]
        block = _between(text, "#### The Polished Plans", "#### The Unsupported Row")
        legs = rows[name]["legs"]
        assert (
            f"Input:     REQUEST {steps[0]} raw\n  Output:    F1 {legs[0][2]} raw\n"
            f"Step 1: moe_lb_v2_2 pool 0x368b148052a1a775dbe70e56d04474e54c694cac\n"
            f"  Input:     REQUEST {steps[1]} raw\n  Output:    F2 {legs[1][2]} raw\n"
            f"Terminal Total: {legs[0][2]} + {legs[1][2]} = {rows[name]['score']} raw"
        ) in block
    assert "all nineteen\n`--strategies all` strategies of Release 0.2.4" in text


def test_the_quote_command_of_section_11(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The §11 `quote` command as published (worker processes, `--strategies all` by default):
    19 rows, and the two 0.2.4 rows' saved records and `--details` blocks are the checked ones."""
    argv = [
        "quote",
        "--bundle",
        str(ROOT / "tests/fixtures/corpus/bundle"),
        "--profile",
        str(ROOT / "config/daily_gross.yaml"),
        "--token-in",
        "USDC",
        "--token-out",
        "USDT0",
        "--amount",
        "10000",
        "--details",
        "--quotes-dir",
        str(tmp_path / "q"),
    ]
    assert main.main(argv) == 0
    out = capsys.readouterr().out
    match = re.search(r"\(run (\S+)\)", out)
    assert match
    records = load_case_records(Path(match.group(1)))
    assert len(records) == 19 and [r["algorithm"] for r in records][-2:] == [
        "split_polish",
        "marginal_activation",
    ]
    rows = data("walkthrough")["rows"]
    for name, base in (
        ("split_polish", "metis_inspired"),
        ("marginal_activation", "incremental_graph"),
    ):
        record = next(r for r in records if r["algorithm"] == name)
        assert (record["status"], int(record["score"]), record["quotes"]["counted"]) == (
            "ok",
            rows[name]["score"],
            rows[name]["quotes_counted"],
        )
        assert record["search"]["base"]["algorithm"] == base
        assert f"options preset {rows[name]['options_key']} v1 (config/{name}/preset_v1.yaml" in out
        assert f"base {base}: status ok, quotes 264, gross 10000663447" in out
    assert "E1 polish: gross 10000663636, quotes 96" in out
    assert "activation: gross 10000663636, quotes 228, invocations 3" in out


# ======================================================================================
# 28. the selection (research-024/selection.md)
# ======================================================================================


def test_selection_figures() -> None:
    s = data("selection")
    assert {n: {k: v for k, v in f.items() if k != "sensitivity"} for n, f in s.items()} == {
        "split_polish": {
            "candidates": 90,
            "eligible": 88,
            "ineligible": ["work_limit"],
            "ineligible_count": 2,
            "band": 3,
            "q_star": "+0.1249",
            "q_winner": "+0.1172",
            "margin": "+0.0025",
            "rank_of_winner": 3,
            "own_base": {
                "base": "metis_inspired",
                "mean": "+0.791",
                "hel": "81/15/0",
                "families": "29 / 0",
            },
            "nominee_rank": 59,
        },
        "marginal_activation": {
            "candidates": 108,
            "eligible": 93,
            "ineligible": ["work_limit"],
            "ineligible_count": 15,
            "band": 1,
            "q_star": "-2.7808",
            "q_winner": "-2.7808",
            "margin": "+0.0995",
            "rank_of_winner": 1,
            "own_base": {
                "base": "incremental_graph",
                "mean": "+1.038",
                "hel": "82/14/0",
                "families": "28 / 0",
            },
            "nominee_rank": 21,
        },
    }
    sens = {
        n: {c: (v["own_base"], v["hel"]) for c, v in f["sensitivity"].items()} for n, f in s.items()
    }
    assert sens == {
        "split_polish": {"c100": ("+0.5688", "85/11/0"), "c200": ("+0.3575", "85/11/0")},
        "marginal_activation": {"c100": ("+0.7627", "82/14/0"), "c200": ("+0.6178", "82/14/0")},
    }


# ======================================================================================
# 29. the campaign (research-024/results.md) and the timing outcomes
# ======================================================================================


def _hel(c: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return c["hel"], c["mean"], c["p50"], c["p95"], c["families"]


def test_campaign_own_base_attribution_and_dispositions() -> None:
    c = data("campaign")
    assert c["dispositions"] == {
        "split_polish": "keep_experimental",
        "marginal_activation": "keep_experimental",
    }
    assert c["statuses"] == {n: {"no_route": 1, "ok": 301} for n in c["dispositions"]}
    assert c["branches"] == {
        n: {"B1_base_passthrough": 1, "B3_polished": 301} for n in c["dispositions"]
    }
    assert {k: _hel(v) + (v["scored"],) for k, v in c["own_base"].items()} == {
        "Q19-full/split_polish vs Q19-full/metis_inspired": (
            "214/77/0", "0.496", "0.129", "2.388", "29 / 0", 291),
        "Q19-sor/split_polish vs Q19-sor/metis_inspired": (
            "194/94/0", "0.532", "0.064", "2.509", "28 / 0", 288),
        "Q19-full/marginal_activation vs Q19-full/incremental_graph": (
            "224/67/0", "0.950", "0.505", "3.252", "28 / 0", 291),
        "Q19-sor/marginal_activation vs Q19-sor/incremental_graph": (
            "229/59/0", "0.892", "0.463", "3.001", "28 / 0", 288),
    }  # fmt: skip
    assert {k: _hel(v) for k, v in c["attribution"].items()} == {
        "E2-E1only": ("213/78/0", "0.503", "0.216", "1.753", "28 / 0"),
        "E2-wm": ("212/74/5", "0.499", "0.215", "1.753", "28 / 0"),
        "E2-cm": ("208/76/7", "0.484", "0.165", "1.753", "28 / 0"),
    }
    assert c["controls"] == {
        k: {
            "classes": {"K2_non_ok": 1, "K5_matched": 301},
            "defects": {},
            "audits": {"ok": 301},
            "charged": charged,
            "embedded_uncharged": 622672,
        }
        for k, charged in (("E2-cm", 18470093), ("E2-wm", 11373837))
    }
    assert c["determinism"] == {"cells": 10268, "differing": 0}


def test_campaign_other_rows_envelope_and_work() -> None:
    c = data("campaign")
    others = {
        n: {o: (v["hel"], v["mean"]) for o, v in rows.items()} for n, rows in c["others"].items()
    }
    assert others == {
        "split_polish": {
            "incremental_graph": ("182/74/35", "1.908"),
            "incremental_graph_repair": ("180/74/37", "1.870"),
            "metis_history": ("205/76/10", "-0.743"),
            "marginal_activation": ("99/58/134", "0.958"),
        },
        "marginal_activation": {
            "incremental_graph_repair": ("223/67/1", "0.912"),
            "metis_inspired": ("153/58/80", "-0.457"),
            "metis_history": ("150/57/84", "-1.696"),
            "split_polish": ("134/58/99", "-0.953"),
        },
    }
    assert c["others"]["split_polish"]["metis_history"]["min"] == "-351.107"
    assert c["others"]["marginal_activation"]["metis_history"]["min"] == "-351.107"
    assert c["others"]["split_polish"]["marginal_activation"]["families"] == "13 / 16"
    assert c["envelope"] == {
        "split_polish": {"equal": 154, "scored": 291, "mean": "-2.106"},
        "marginal_activation": {"equal": 190, "scored": 291, "mean": "-3.060"},
    }
    assert c["work"]["split_polish"]["quotes"] == 9299891
    assert c["work"]["metis_inspired"]["quotes"] == 5324859
    assert (
        c["work"]["marginal_activation"]["quotes"],
        c["work"]["incremental_graph"]["quotes"],
    ) == (
        11581715,
        8591807,
    )
    # results.md §3.4 prints 1.746 for the first ratio; 9299891 / 5324859 = 1.74650... -> 1.747
    assert c["ratios"] == {
        "split_polish": {"quotes": "1.747", "cl_swap_steps": "1.043", "lb_bins_swapped": "1.127"},
        "marginal_activation": {
            "quotes": "1.348",
            "cl_swap_steps": "1.063",
            "lb_bins_swapped": "1.097",
        },
        "E2-E1only": {"quotes": "1.276", "cl_swap_steps": "1.014", "lb_bins_swapped": "1.072"},
    }


def test_timing_outcomes_and_statements() -> None:
    t = data("campaign")["timing"]
    assert t["statements"] == {
        "measurement_attempted_correctly": True,
        "usable_latency_obtained": ["U6", "UQ"],
    }
    assert t["outcomes"] == {
        **{u: "inconclusive_cap_exhausted" for u in ("UP", "U1", "U2", "U3", "U4", "U5")},
        "U6": "valid",
        "UQ": "valid",
    }


def test_the_timing_statement_is_verbatim() -> None:
    """§13 item 17 quotes statement (b) of `results.md` §4.3 word for word."""
    item = _between(guide(), "17. **Release 0.2.4", "\n---\n")
    quoted = " ".join(
        line.strip()[1:].strip() for line in item.splitlines() if line.strip().startswith(">")
    )
    assert quoted == examples().statement_b()
    assert "not measured validly" in quoted and "`U6` and `UQ`" in quoted


def _sections_0_2_4(text: str) -> tuple[str, str]:
    return _between(text, "### 23.10 ", "## 24. "), text[text.index("### 24.10 ") :]


def _table_problems(text: str) -> list[str]:
    """The campaign rows of §§23.10 / 24.10 that are not, verbatim, the rendered rows, in the
    section that owns them, plus any other campaign row there."""
    s23, s24 = _sections_0_2_4(text)
    rows = data("campaign")["guide_rows"]
    own = rows["own_base"]
    missing = [r for r in own[:2] if r not in s23]
    missing += [r for r in own[2:] + rows["attribution"] if r not in s24]
    published = [
        line for line in (s23 + s24).splitlines() if re.match(r"\| [^|]+ \| \d+ / 302 \|", line)
    ]
    return missing + [line for line in published if line not in sum(rows.values(), [])]


def test_the_guide_tables_are_the_rendered_rows() -> None:
    assert _table_problems(guide()) == []
    assert len(sum(data("campaign")["guide_rows"].values(), [])) == 7


# ======================================================================================
# the checks have teeth
# ======================================================================================


def test_a_polish_that_never_accepts_fails_the_walkthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: E1 keeps every base plan; the §11 expectations (strictly above the base, at least
    the grid oracle, exact legs) must fail."""
    m = examples()
    monkeypatch.setattr(sp, "line_search", lambda *a, **k: None)
    with pytest.raises(m.ExampleError):
        m.example_walkthrough()


def test_a_changed_campaign_figure_fails_the_examples(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: one mean of the analysis moved by 0.001 bps no longer matches its
    `report-tables.md` row."""
    m = examples()
    analysis = json.loads(json.dumps(m.ANALYSIS))
    (c,) = [x for x in analysis["comparisons"]["base_control"]
            if x["candidate"] == "Q19-full/split_polish"]  # fmt: skip
    c["bps"]["mean"] += 0.001
    monkeypatch.setattr(m, "ANALYSIS", analysis)
    with pytest.raises(m.ExampleError, match="report-tables.md row"):
        m.example_campaign()


def test_a_changed_selection_figure_fails_the_examples(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: the selection table's Q(winner) of `split_polish` changed."""
    m = examples()
    changed = m.SELECTION_TABLES.replace("+0.1172", f"+{_decremented('0.1172')}")
    monkeypatch.setattr(m, "SELECTION_TABLES", changed)
    with pytest.raises(m.ExampleError, match="outcome row"):
        m.example_selection()


def test_the_examples_are_offline_and_need_no_data_directory() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "data/corpus" not in source and "ROUTER_" not in source and "os.environ" not in source
    imported = set(re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, flags=re.M))
    assert not imported & {"requests", "urllib", "httpx", "socket", "web3", "dune_client"}


# identifiers and request constants the 0.2.4 text cites; none is a result.
STRUCTURAL = {
    # the tracker issues (WHI-xxxx) the text names
    "1599",
    "1600",
    "1623",
    "1624",
    "1627",
    "1631",
    "1632",
    "1633",
    # the fixture's block and the human request amount (10 000 USDC), asserted above
    "101082044",
    "10000",
}


def _numbers(text: str) -> set[str]:
    """`test_r023_examples._numbers`: numeric tokens of at least three digits or with a decimal
    point, thousands separators removed; hex words, commit hashes and section references are not
    numbers; a long run of decimal digits (a raw amount) is a number."""
    text = re.sub(r"^\s*#+ .*$", " ", text, flags=re.M)
    text = re.sub(r"§§?\s*\d+(?:\.\d+)?(?:\s*(?:,|and|–|-)\s*\d+(?:\.\d+)?)*", " ", text)
    text = re.sub(r"[Ss]ections?\s+\d+(?:\s*(?:,|and|–|-)\s*\d+)*", " ", text)
    text = re.sub(r"research-0\d\d", " ", text)
    text = re.sub(r"0x[0-9a-fA-F]+", " ", text)
    text = re.sub(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,}\b", " ", text)
    found = set()
    for token in re.findall(
        r"(?<![\w.])\d+(?:(?:\\,|,)\d{3})+(?![\w])|(?<![\w.])\d+(?:\.\d+)?(?![\w])", text
    ):
        token = token.replace("\\,", "").replace(",", "")
        if len(token.replace(".", "")) >= 3 or "." in token:
            found.add(token)
    return found


def _r024_text(text: str) -> str:
    """Every 0.2.4 passage of the guide: the header and inspected-commit bullets, the §11 settings,
    rows, plans and item 12, §13 item 17 and §§23.10 / 24.10."""
    table = _between(text, "### 11.1 ", "### 11.2 ")
    parts = [
        _between(text, "- the **0.2.4 roster**", "For each strategy"),
        _between(text, "- The 0.2.4 parts", "All numeric traces"),
        _between(text, "- **0.2.4 settings:**", "*Classification:*"),
        _between(text, "same rows in process on the fixture itself", "### 11.1 "),
        *(
            x
            for x in table.splitlines()
            if x.startswith(("| `split_polish` |", "| `marginal_activation` |"))
        ),
        _between(text, "The two post-processor rows count", "### 11.2 "),
        _between(text, "#### The Polished Plans", "#### The Unsupported Row"),
        _between(text, "12. **The two post-processors re-split", "### 11.4 "),
        _between(text, "17. **Release 0.2.4", "\n---\n"),
        *_sections_0_2_4(text),
    ]  # fmt: skip
    return "\n".join(parts)


def _backed() -> set[str]:
    asserted = _numbers(json.dumps(everything(), default=str))
    asserted |= _numbers(Path(__file__).read_text(encoding="utf-8"))
    for source in (
        "contract.md",
        "selection.md",
        "selection/tables.md",
        "results.md",
        "campaign/report-tables.md",
        "campaign/timing-tables.md",
    ):
        asserted |= _numbers((R024 / source).read_text(encoding="utf-8"))
    # the 0.2.3 figures the 0.2.4 sections cite as history (research-023/results.md, §§23.9, 24.9)
    for source in ("campaign/report-tables.md",):
        asserted |= _numbers((R023 / source).read_text(encoding="utf-8"))
    return asserted | STRUCTURAL


def test_every_number_of_the_0_2_4_text_is_asserted() -> None:
    """The 0.2.4 passages publish only numbers that the example module computed after checking its
    independent expectation, that a literal of this file states, or that the committed research-024
    evidence (or, for 0.2.3 history, the research-023 tables) states."""
    published = _numbers(_r024_text(guide()))
    assert len(published) > 100  # not vacuous
    unbacked = sorted(published - _backed())
    assert not unbacked, f"numbers in the 0.2.4 guide text with no check: {unbacked}"


def _decremented(figure: str) -> str:
    """`figure` minus one unit of its last digit, computed (so never a literal of this file, which
    backs the number check); thousands separators are kept."""
    digits = figure.replace(",", "")
    places = len(digits.split(".")[1]) if "." in digits else 0
    value = Decimal(digits) - Decimal(1).scaleb(-places)
    return f"{value:,}" if "," in figure else str(value)


def test_a_changed_published_number_is_caught() -> None:
    """Mutation: one published figure per 0.2.4 section, changed in the guide, is reported by the
    number check, the §11 row check or the table-row check."""
    text = guide()
    rows = data("walkthrough")["rows"]
    assert not _numbers(_r024_text(text)) - _backed() and not _table_problems(text)
    for old, figure in (
        ("| **10000663635** | 376 |", "376"),  # §11.1: a row's counted quotes
        ("returns 10000111", "10000111"),  # §11.3 item 12: an activation proposal
        ("Output:    F1 9964797543", "9964797543"),  # §11.2: a polished leg
        ("| 214/77/0 | +0.496 |", "0.496"),  # §23.10: a campaign table mean
        ("Q +0.1172", "0.1172"),  # §23.10: a selection figure
        ("9,299,891 quotes", "9,299,891"),  # §23.10: a work total
        ("| 213/78/0 | +0.503 |", "0.503"),  # §24.10: an attribution mean
        ("150/57/84, -1.696", "1.696"),  # §24.10: a comparison with another row
        ("+0.7627 and +0.6178", "0.6178"),  # §24.10: a sensitivity figure
    ):
        assert old in text, old
        mutated = text.replace(old, old.replace(figure, _decremented(figure)))
        rows_ok = all(
            line.startswith(rows[n]["table_row"])
            for n in ("split_polish", "marginal_activation")
            for line in _between(mutated, "### 11.1 ", "### 11.2 ").splitlines()
            if line.startswith(f"| `{n}` |")
        )
        caught = (
            _numbers(_r024_text(mutated)) - _backed() or _table_problems(mutated) or not rows_ok
        )
        assert caught, old
    # §13 item 17: a changed word of the quoted timing statement
    mutated = text.replace("was **not measured validly**", "was **measured validly**", 1)
    item = _between(mutated, "17. **Release 0.2.4", "\n---\n")
    quoted = " ".join(x.strip()[1:].strip() for x in item.splitlines() if x.strip().startswith(">"))
    assert quoted != examples().statement_b()


def test_the_0_2_4_text_never_claims_a_speedup() -> None:
    """A 0.2.4 line mentioning speed or latency denies a claim, or is the quoted statement (b)."""
    prose = re.sub(r"`[^`]*`|\]\([^)]*\)", " ", _r024_text(guide()))
    for banned in ("speedup", "speed-up", "faster", "slower", "latency", "overhead"):
        for line in prose.splitlines():
            if banned in line.lower() and not line.strip().startswith(">"):
                assert re.search(r"\bno\b|\bnot\b|never|nothing|neither|claim", line, re.I), line


# ======================================================================================
# the roster in the other documents, and config/README.md
# ======================================================================================


def test_every_roster_document_describes_the_19_rows() -> None:
    refs = ROOT / "docs" / "references"
    groups = (refs / "strategy-groups.md").read_text(encoding="utf-8")
    single = (refs / "single-request.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    text = guide()
    assert "therefore runs nineteen\nstrategies" in groups
    assert "`split_polish` and\n`marginal_activation` (`benchmark/strategies.py`" in groups
    assert "these are nineteen for a six-algorithm profile" in single
    assert "Its 19 rows are the walkthrough" in single
    assert "**19 strategies** for a six-algorithm profile" in design
    assert "compares **19** strategies" in text and "all 19 `--strategies all` rows" in text
    for doc in (groups, single, design, text):
        assert "R024-P01-split_polish" in doc and "R024-P02-marginal_activation" in doc
        assert "not adoption" in doc
    # no document states the current roster as 17 rows without marking it as history
    for doc in (groups, single, text):
        for match in re.finditer(r"[^.]*\b(17|seventeen)\b[^.]*\.", doc):
            sentence = match.group(0)
            if re.search(r"`--strategies all`|`all`", sentence) and re.search(
                r"\bis\b|runs", sentence
            ):
                history = r"0\.2\.[23]|was|Through|history|saved|frozen|registered"
                assert re.search(history, sentence), sentence


def test_config_readme_describes_every_campaign_and_preset_directory() -> None:
    readme = (ROOT / "config" / "README.md").read_text(encoding="utf-8")
    campaigns = sorted(p.name for p in (ROOT / "config").glob("research_0*") if p.is_dir())
    assert campaigns == ["research_021", "research_022", "research_023", "research_024"]
    for name in campaigns:
        assert f"`{name}/" in readme, name
    for name in (
        "research_022/schedule.yaml",
        "research_022/profiles/",
        "research_022/l01-r022.yaml",
        "research_022/latency-arms.yaml",
        "research_023/schedule.yaml",
        "research_023/profiles/",
    ):
        assert f"`{name}" in readme, name
    presets = sorted({p.parent.name for p in (ROOT / "config").glob("*/preset_v*.yaml")})
    for name in presets:
        assert f"`{name}/`" in readme, name
    assert "nothing\n  reads it before then" not in readme

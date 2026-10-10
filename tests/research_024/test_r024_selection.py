"""WHI-1631 research_024 preset selection: the rule, the gates, the schedule and a fixture run.

- The committed schedule checks clean against contract §12 (rule values, grids, reuse list,
  P*, base CECs, renderings, the report-bundle ban), and `check` refuses drift.
- The rule (`r024_rule.apply_rule`, with the schedule's values) reproduces every §12 worked
  example WE1-WE8, and each pinned value is load-bearing: changing epsilon, a limit or the
  tie-break order changes an example's outcome; so does changing the rule's comparisons.
- The record branches, Rule P / Rule M and every gate G2-G7 fail exactly when their rule is
  broken in a record; §5.8 precedence holds.
- A fixture selection (the tracked `mantle_mixed` bundle in place of the tuning split, a reduced
  grid) runs stage T and stage I through the real executor and analyses clean: every arm x case
  recorded, the rule reproduced from the written rule inputs, presets rendered options-only, the
  preset runs equal their candidates, the tables regenerate identically.
- Once the stage-T analysis is pinned, the rule reproduces the committed selection from it.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
from fractions import Fraction
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


S: Any = _load("r024_selection", "tools/research_024/selection.py")
RR: Any = sys.modules["r024_rule"]
RAW = S.load_raw()
REGISTRY = S.registry(RAW)
SELECTION = REGISTRY["selection"]
PARAMS = S.rule_parameters(RAW)


def _sha(path: Path) -> str:
    return str(S.sha256_bytes(path.read_bytes()))


# ----------------------------------------------------------------------------- schedule


def test_the_committed_schedule_checks_clean() -> None:
    assert S.check(RAW) == []
    table = S.candidates(RAW)
    kinds = [s["identity"] for s in table.values() if s["kind"] == "candidate"]
    assert kinds.count("split_polish") == 90 and kinds.count("marginal_activation") == 108
    assert [c for c in table if c.startswith("ref|")] == [
        f"ref|{b}" for b in SELECTION["quality"]["reference_set"]
    ]
    ids = [i["id"] for i in S.invocations(RAW, "T")]
    assert len(ids) == 2 * 190 == len(set(ids))  # 13 registered reuses run nothing new
    assert {i["profile"] for i in S.invocations(RAW, "T")} <= set(S.all_profiles(RAW))
    assert S.invocations(RAW, "T")[0]["profile"].startswith("sp-incremental_graph_repair")


def test_the_schedule_carries_the_contracts_rule_values() -> None:
    for key in S.CONTRACT_KEYS:
        assert RAW[key] == SELECTION[key], key
    assert PARAMS == {
        "epsilon": Fraction(1, 100),
        "limits": {"quotes": 2, "cl_swap_steps": 2, "lb_bins_swapped": 2},
        "tie_break": ["quotes", "cl_swap_steps", "lb_bins_swapped", "candidate_id"],
        "failure_gross": 0,
    }


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["rule"].update(epsilon_bps="1/50"),
        lambda r: r["work"]["limits"].update(quotes=3),
        lambda r: r["rule"]["tie_break"].reverse(),
        lambda r: r["identities"]["split_polish"]["grid"]["rounds"].append(8),
        lambda r: r["reuse"]["configurations"].pop(),
        lambda r: r["forbidden"]["bundle_hashes"].pop(),
        lambda r: r["inputs"].update(report_full={"source": "data/x/bundle_report"}),
    ],
)
def test_check_refuses_a_schedule_that_departs_from_the_contract(change: Any) -> None:
    raw = copy.deepcopy(RAW)
    change(raw)
    assert S.check(raw) != []


def test_check_refuses_profile_drift_and_a_report_bundle_in_a_profile(
    tmp_path: Path,
) -> None:
    raw = copy.deepcopy(RAW)
    raw["profile_dir"] = str(tmp_path)
    for key, (doc, what) in S.all_profiles(raw).items():
        (tmp_path / f"{key}.yaml").write_text(S.render_text(raw, key, doc, what))
    assert S.check(raw) == []
    path = tmp_path / "ref-incremental_graph.yaml"
    path.write_text(path.read_text() + "# bundle_report\n")
    problems = S.check(raw)
    assert any("ref-incremental_graph.yaml differs from its rendering" in p for p in problems)
    (tmp_path / "extra.yaml").write_text("x: 1\n")
    assert any("unregistered profiles" in p for p in S.check(raw))


def test_every_candidate_renders_under_pstar_with_its_registered_options() -> None:
    for cid, spec in S.candidates(RAW).items():
        doc = S.render_document(RAW, spec["identity"], spec["options"])
        assert doc["graph"] == {"chunks": 50, "label_hops": 4, "label_pruning": True}
        resolved = S.resolved_of(doc)
        cec = RR.cec(resolved, spec["identity"])
        assert cec["options"] == spec["options"], cid
        assert cec["budget"] == {
            "time_limit_seconds": 900.0,
            "max_quotes": 300000,
            "max_candidates": None,
        }


# ----------------------------------------------------------------------------- rule (§5.5-§5.7)


@pytest.mark.parametrize("example", SELECTION["worked_examples"], ids=lambda e: e["id"])
def test_worked_examples_reproduce(example: dict[str, Any]) -> None:
    result = RR.apply_rule(example, **PARAMS)
    expected = example["expected"]
    assert (result["outcome"], result["winner"], result["eligible"]) == (
        expected["outcome"],
        expected["winner"],
        expected["eligible"],
    )


def _winner(name: str, **change: Any) -> str | None:
    example = next(e for e in SELECTION["worked_examples"] if e["id"].startswith(name))
    return RR.apply_rule(example, **{**PARAMS, **change})["winner"]  # type: ignore[no-any-return]


def test_each_rule_value_is_load_bearing() -> None:
    """A test fails if epsilon, a limit or the tie-break order is altered."""
    assert _winner("WE1", epsilon=Fraction(0)) != "we1|b"
    assert _winner("WE1", epsilon=Fraction(1, 1000)) != "we1|b"
    assert _winner("WE2", limits={**PARAMS["limits"], "quotes": Fraction(5, 2)}) != "we2|d"
    assert _winner("WE2", limits={**PARAMS["limits"], "quotes": Fraction(199, 100)}) is None
    assert _winner("WE7", limits={**PARAMS["limits"], "cl_swap_steps": 1}) == "we7|p"
    swapped = ["cl_swap_steps", "quotes", "lb_bins_swapped", "candidate_id"]
    assert _winner("WE3", tie_break=swapped) != "we3|a"
    assert _winner("WE3", tie_break=["candidate_id"]) == "we3|a"  # id last: a, b, c
    four = next(e for e in SELECTION["worked_examples"] if e["id"].startswith("WE4"))
    assert RR.apply_rule(four, **PARAMS)["q"]["we4|h"] == "-5000"  # the failed case scores 0


def test_the_rule_reports_ranking_frontier_margin_and_concession() -> None:
    example = next(e for e in SELECTION["worked_examples"] if e["id"].startswith("WE1"))
    result = RR.apply_rule(example, **PARAMS)
    q_a = Fraction(result["q"]["we1|a"])
    q_b = Fraction(result["q"]["we1|b"])
    assert q_a - q_b == Fraction(10**4, 10**6) / 2  # 0.005 bps: inside the band
    assert result["band"] == ["we1|a", "we1|b"] and result["margin"] is None
    assert Fraction(result["concession"]) == q_a - q_b <= PARAMS["epsilon"]
    assert result["ranking"] == ["we1|a", "we1|b"]
    assert result["frontier"] == ["we1|a", "we1|b"]  # higher Q vs fewer quotes
    two = next(e for e in SELECTION["worked_examples"] if e["id"].startswith("WE2"))
    result = RR.apply_rule(two, **PARAMS)
    assert result["ranking"] == ["we2|d", "we2|c"] and result["ineligible"] == {
        "we2|c": "work_limit"
    }


def test_ratios_follow_rule_m_and_the_zero_conventions() -> None:
    assert RR.ratio(0, 0) == 1 and RR.ratio(1, 0) == RR.INFINITE and RR.ratio(None, 5) is None
    assert RR.ratio(4, 2) == 2
    limits = PARAMS["limits"]
    base = {"quotes": 10, "cl_swap_steps": 0, "lb_bins_swapped": 3}
    assert RR.within_limits({"quotes": 20, "cl_swap_steps": 0, "lb_bins_swapped": 6}, base, limits)
    assert not RR.within_limits(
        {"quotes": 21, "cl_swap_steps": 0, "lb_bins_swapped": 6}, base, limits
    )
    assert not RR.within_limits(
        {"quotes": 1, "cl_swap_steps": 1, "lb_bins_swapped": 1}, base, limits
    )
    assert RR.totals([{"quotes": 1, "cl_swap_steps": 2, "lb_bins_swapped": None}]) == {
        "quotes": 1,
        "cl_swap_steps": 2,
        "lb_bins_swapped": None,
    }


def test_outcome_precedence() -> None:
    selected = {"outcome": "selected", "winner": "x", "cause": None}
    empty = {"outcome": "no_selection", "winner": None, "cause": "no_eligible_candidate"}
    assert RR.identity_outcome(defects=[], reference_defects=[], reference_unavailable=[],
                               rule=selected)["outcome"] == "selected"  # fmt: skip
    assert RR.identity_outcome(defects=["y"], reference_defects=[], reference_unavailable=[],
                               rule=selected)["outcome"] == "blocked_defect"  # fmt: skip
    assert RR.identity_outcome(defects=[], reference_defects=["ref|path_split"],
                               reference_unavailable=["ref|x"], rule=selected)[
        "outcome"] == "blocked_defect"  # fmt: skip
    assert RR.identity_outcome(defects=[], reference_defects=[], reference_unavailable=["r"],
                               rule=selected)["outcome"] == "no_selection"  # fmt: skip
    assert RR.identity_outcome(defects=[], reference_defects=[], reference_unavailable=[],
                               rule=empty)["cause"] == "no_eligible_candidate"  # fmt: skip


# ----------------------------------------------------------------------------- branches, pairs


@pytest.mark.parametrize("example", SELECTION["record_branches"], ids=lambda e: e["branch"])
def test_record_branches_classify(example: dict[str, Any]) -> None:
    assert RR.branch(example["record"], example["identity"]) == example["branch"]
    assert (
        list(RR.GATE_APPLICABILITY[example["branch"]])
        == SELECTION["gate_applicability"][example["branch"]]
    )


@pytest.mark.parametrize("example", SELECTION["pair_examples"], ids=lambda e: e["case"])
def test_rule_p_and_rule_m(example: dict[str, Any]) -> None:
    case, available = RR.reconcile(example["ordinary"], example["work_pass"])
    assert case == example["case"]
    if case != RR.P5:
        assert list(available) == example["available"]
        cells = RR.cell_work(example["ordinary"], example["work_pass"])
        assert [u for u in RR.UNITS if cells[u] is not None] == example["available"]


# ----------------------------------------------------------------------------- fixture selection


def _fixture_raw(root: Path) -> dict[str, Any]:
    """The committed schedule over the fixture bundle with a reduced grid (two E1 bases with
    options, one E2 candidate), no registered reuse, stage-I files under `root`."""
    raw: dict[str, Any] = copy.deepcopy(RAW)
    raw["inputs"] = {
        "tuning_full": {
            "source": str(FIXTURE),
            "bundle_hash": _sha(FIXTURE / "manifest.json"),
            "cases_sha256": _sha(FIXTURE / "cases.jsonl"),
            "cases": 4,
            "split": "all",
            "cohort": "fixture",
        }
    }
    sp = raw["identities"]["split_polish"]
    sp["grid"].update(base=["incremental_graph", "metis_history"], solver=["brent"], rounds=[1, 2],
                      tolerance={"1e-4": 0.0001})  # fmt: skip
    sp["candidates"] = 4
    ma = raw["identities"]["marginal_activation"]
    ma["grid"].update(base=["incremental_graph"], mode=["pf"], activations=[1], top_k=[1],
                      delta_share={"1e-4": 0.0001})  # fmt: skip
    ma["candidates"] = 1
    for identity in ("split_polish", "marginal_activation"):
        raw["identities"][identity]["preset"]["path"] = str(root / f"{identity}-preset_v1.yaml")
    raw["reuse"]["configurations"] = []
    raw["reuse"]["sensitivity_sources"] = []
    # on four fixture cases polishing costs several times the base's work, so the fixture run
    # widens the limit to reach the `selected` path (the registered limit gives `no_selection`,
    # asserted below); the committed schedule never changes
    raw["work"]["limits"] = dict.fromkeys(raw["work"]["limits"], 10)
    raw["selection_analysis"] = str(root / "T-analysis.json")
    raw["profile_dir"] = str(root / "profiles")
    for stage in ("T", "I"):
        raw["stages"][stage].update(caffeinate=False, pmset_capture=False)
    return raw


@pytest.fixture(scope="module")
def selection(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("r024")
    raw = _fixture_raw(root)
    inputs = root / "inputs"
    shutil.copytree(FIXTURE, inputs / "tuning_full")
    (root / "profiles").mkdir()
    for key, (doc, what) in S.all_profiles(raw).items():
        (root / "profiles" / f"{key}.yaml").write_text(S.render_text(raw, key, doc, what))
    out = root / "selection" / "T"
    code = S.pc.execute(S.build(raw, "T"), "T", inputs=inputs, out=out, lanes=6,
                        allow_dirty=True, echo=lambda _m: None)  # fmt: skip
    assert code == 0, (out / "ledger.jsonl").read_text()
    result = S.analyze_t(raw, inputs, out, root)
    rule_inputs = result.pop("_rule_inputs")
    (root / "T-analysis.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    S.write_rule_inputs(root / "T-rule-inputs.json", rule_inputs)
    for identity in raw["identities"]:
        if result["identities"][identity]["outcome"] == "selected":
            preset = Path(raw["identities"][identity]["preset"]["path"])
            preset.write_text(S.render_preset(raw, identity, result))
    for key, (doc, what) in S.all_profiles(raw).items():  # now with the stage-I profiles
        (root / "profiles" / f"{key}.yaml").write_text(S.render_text(raw, key, doc, what))
    stage_i = root / "selection" / "I"
    view = S.with_stage_i_reuse(raw, root, inputs)
    code = S.pc.execute(S.build(view, "I"), "I", inputs=inputs, out=stage_i, lanes=6,
                        allow_dirty=True, echo=lambda _m: None)  # fmt: skip
    assert code == 0, (stage_i / "ledger.jsonl").read_text()
    result_i = S.analyze_i(raw, inputs, stage_i, root)
    return {"raw": raw, "root": root, "inputs": inputs, "out": out, "result": result,
            "rule_inputs": rule_inputs, "result_i": result_i}  # fmt: skip


def test_the_fixture_selection_records_every_arm_and_checks_clean(
    selection: dict[str, Any],
) -> None:
    result = selection["result"]
    assert result["problems"] == []
    assert len(result["git_revisions"]) == 1 and len(result["environments"]) == 1
    assert len(result["raw_sha256"]) == 2 * 2 * 10  # (5 refs + 5 candidates) x (run + work pass)
    for cid, row in result["candidates"].items():
        assert row["g1"] == [], cid
        if row["kind"] == "candidate":
            assert sum(row["counters"]["statuses"].values()) == 4
            assert sum(row["branches"].values()) == 4 and sum(row["pairs"].values()) == 4
            assert row["pairs"] == {RR.P1: 4}, cid
            assert row["eligibility"] in ("eligible", "ineligible: work_limit"), cid
    assert result["reference_work"]["quotes"] > 0
    for identity, outcome in result["identities"].items():
        assert outcome["outcome"] == "selected", identity
        assert outcome["winner"] in outcome["band"]
        assert outcome["concession"] is not None


def test_the_rule_reproduces_the_selection_from_the_written_rule_inputs(
    selection: dict[str, Any],
) -> None:
    result = selection["result"]
    params = S.rule_parameters(selection["raw"])
    loaded = json.loads((selection["root"] / "T-rule-inputs.json").read_text())
    assert loaded == json.loads(json.dumps(selection["rule_inputs"]))
    for identity, problem in loaded.items():
        again = RR.apply_rule(problem, **params)
        pinned = result["identities"][identity]
        for key in ("outcome", "winner", "eligible", "q_star", "band", "ranking", "margin"):
            assert again[key] == pinned[key], (identity, key)


def test_the_registered_limit_gives_no_selection_on_the_fixture(selection: dict[str, Any]) -> None:
    for identity, problem in selection["rule_inputs"].items():
        result = RR.apply_rule(json.loads(json.dumps(problem)), **PARAMS)
        assert (result["outcome"], result["cause"]) == ("no_selection", "no_eligible_candidate")
        assert set(result["ineligible"].values()) == {"work_limit"}, identity


def test_presets_are_options_only_and_stage_i_reproduces_the_candidates(
    selection: dict[str, Any],
) -> None:
    raw, result, result_i = selection["raw"], selection["result"], selection["result_i"]
    assert result_i["problems"] == []
    for identity in raw["identities"]:
        doc = yaml.safe_load(Path(raw["identities"][identity]["preset"]["path"]).read_text())
        assert set(doc) == {"key", "version", "algorithm", "options"}
        winner = result["identities"][identity]["winner"]
        assert doc["options"] == result["candidates"][winner]["options"]
        entry = result_i["identities"][identity]
        assert entry["final_outcome"] == "selected"
        assert entry["preset"]["cec_equal"] and entry["preset"]["differences"] == []
        assert entry["preset"]["cases_compared"] == 4
        for k in ("c100", "c200"):
            arm = entry["sensitivity"][k]
            assert arm["winner_arm"]["g1"] == arm["base_arm"]["g1"] == []
            assert arm["winner_arm"]["failures"] == {}
            assert arm["winner_arm"]["q"] is not None


def test_the_tables_regenerate_identically(selection: dict[str, Any], tmp_path: Path) -> None:
    path = tmp_path / "T.json"
    path.write_text(json.dumps(selection["result"], indent=1, sort_keys=True) + "\n")
    stage_i = json.loads(json.dumps(selection["result_i"]))
    tables = S.render_tables(json.loads(path.read_text()), stage_i)
    assert tables == S.render_tables(json.loads(path.read_text()), stage_i)
    for needle in ("## Outcomes", "full ranking", "## Reuse", "Stage I", "**winner"):
        assert needle in tables
    sums = tmp_path / "SHA256SUMS"
    sums.write_text("".join(f"{d}  {n}\n" for n, d in selection["result"]["raw_sha256"].items()))
    assert S.verify_sums(selection["out"], sums) == []


def test_a_report_bundle_in_a_ledger_or_inputs_is_a_problem(
    selection: dict[str, Any], tmp_path: Path
) -> None:
    raw = selection["raw"]
    out = tmp_path / "T"
    out.mkdir()
    (out / "ledger.jsonl").write_text('{"argv": ["--bundle", "/x/bundle_report"]}\n')
    inputs = tmp_path / "inputs"
    (inputs / "tuning_full").mkdir(parents=True)
    (inputs / "sor_cohort_report").mkdir()
    problems = S._forbidden_in_runs(raw, out, {}, inputs)
    assert len(problems) == 2


# ----------------------------------------------------------------------------- gates (§5.4)


def _records(selection: dict[str, Any], cid: str) -> tuple[Any, Any, Any]:
    root = selection["root"]
    runs = selection["result"]["run_dirs"]
    spec = S.candidates(selection["raw"])[cid]

    def load(key: str) -> Any:
        return RR.by_case(S.load_view(root / runs[key])["records"])

    return load(f"T-{S.slug(cid)}"), load(f"T-WP-{S.slug(cid)}"), load(f"T-ref-{spec['base']}")


def test_each_gate_fails_when_its_rule_is_broken(selection: dict[str, Any]) -> None:
    cid = "sp|incremental_graph|brent|r2|t1e-4"
    ordinary, work, reference = _records(selection, cid)
    case = next(c for c, r in ordinary.items() if RR.branch(r, "split_polish") == RR.B3)
    rec, wp, ref = ordinary[case], work[case], reference[case]
    pair, _ = RR.reconcile(rec, wp)
    assert pair == RR.P1
    assert RR.cell_gates(rec, ref, "split_polish", pair)["failed"] == []

    def broken(change: Any) -> list[str]:
        r = copy.deepcopy(dict(rec))
        change(r)
        return list(RR.cell_gates(r, ref, "split_polish", pair)["failed"])

    def status(r: dict[str, Any]) -> None:
        r["status"] = "timeout"

    def worse(r: dict[str, Any]) -> None:
        g = int(r["search"]["split_polish"]["base_gross"]) - 1
        r["evaluation"]["gross_output"] = r["search"]["split_polish"]["gross"] = str(g)

    def base_quotes(r: dict[str, Any]) -> None:
        r["search"]["base"]["quotes"] += 1
        r["quotes"]["counted"] += 1

    def base_gross(r: dict[str, Any]) -> None:
        r["search"]["split_polish"]["base_gross"] = "1"

    def ledger(r: dict[str, Any]) -> None:
        r["quotes"]["counted"] += 1

    def evaluation(r: dict[str, Any]) -> None:
        r["search"]["split_polish"]["gross"] = "1"

    def refusal(r: dict[str, Any]) -> None:
        r["search"]["split_polish"]["scope"] = "unsupported_topology"
        r["status"] = "invalid_plan"  # G7's second clause alone (G2 fails too)

    assert broken(status) == ["G2", "G3"]
    assert broken(worse) == ["G3"]
    assert broken(base_quotes) == ["G4"]
    assert "G4" in broken(base_gross)
    assert broken(ledger) == ["G5"]
    assert broken(evaluation) == ["G7"]
    assert "G7" in broken(refusal)
    differs = copy.deepcopy(dict(wp))
    differs["search"][RR.WORK_KEY]["quotes_executed"] += 1
    assert RR.reconcile(rec, differs)[0] == RR.P5
    assert RR.cell_gates(rec, ref, "split_polish", RR.P5)["failed"] == ["G6"]


def test_a_preset_run_that_differs_is_found(selection: dict[str, Any]) -> None:
    cid = "sp|incremental_graph|brent|r2|t1e-4"
    ordinary, _, _ = _records(selection, cid)
    case = next(iter(ordinary))
    changed = copy.deepcopy(dict(ordinary))
    changed[case] = {**changed[case], "score": "0"}
    assert RR.preset_equality(ordinary, changed, "split_polish", list(ordinary)) == [
        {"case_id": case, "field": "score"}
    ]


# ----------------------------------------------------------------------------- pinned selection


PINNED = REPO / str(RAW["selection_analysis"])


@pytest.mark.skipif(not PINNED.is_file(), reason="the stage-T analysis is not pinned yet")
def test_the_rule_reproduces_the_pinned_selection() -> None:
    analysis = json.loads(PINNED.read_text())
    path = S.rule_inputs_path(RAW)
    assert _sha(path) == analysis["rule_inputs_sha256"]
    loaded = json.loads(path.read_text())
    assert analysis["problems"] == []
    for identity, problem in loaded.items():
        again = RR.apply_rule(problem, **PARAMS)
        pinned = analysis["identities"][identity]
        for key in ("outcome", "winner", "eligible", "q", "q_star", "band", "ranking",
                    "frontier", "margin", "concession"):  # fmt: skip
            assert again[key] == pinned[key], (identity, key)
        assert len(problem["candidates"]) == RAW["identities"][identity]["candidates"]
        assert all(len(c["gross"]) == analysis["cases"] == 96 for c in problem["candidates"])

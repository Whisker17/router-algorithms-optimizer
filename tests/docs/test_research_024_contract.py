"""R024-C/1 contract checks (docs/references/research-024/contract.md, WHI-1630).

The contract is only useful if every value a later issue applies is pinned and mechanical, so this
module re-derives what can be re-derived and executes what is a rule:

- P* is recomputed with the loader (`derive(config/full_gross.yaml, "all")`) and must equal the
  registry's shared sections and hashes; so must the five base configurations (CEC, presets);
- both candidate grids are expanded; every candidate passes the real option validator and is
  rendered under P* (an options-only preset needs no other profile key);
- every reusable R023 configuration has the CEC of its P*-rendered candidate, and its records are
  pinned in the committed R023 SHA256SUMS;
- the selection rule (section 5.7) runs on the registry's worked examples, and each example still
  discriminates (changing epsilon, the limit or the tie-break order changes its outcome);
- the earlier campaigns' `check`s: research_022/023 0 problems, research_021 exactly the 9
  registered finding identities;
- the timing units partition the roster, and the re-attempt policy and its triggers are the
  registered ones; RELEASE_PLAN.md carries the 0.2.4 gates and the six shipped 0.2.3 issues;
  every review finding has one disposition.
"""

from __future__ import annotations

import ast
import hashlib
import itertools
import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
import yaml

from benchmark import strategies
from benchmark.profile import RunProfile, load_profile, parse_profile, preset_options
from benchmark.strategies import derive
from routing.algorithms.base import validated_options
from routing.algorithms.registry import ALGORITHMS

ROOT = Path(__file__).resolve().parents[2]
R024 = ROOT / "docs" / "references" / "research-024"
CONTRACT = (R024 / "contract.md").read_text(encoding="utf-8")
RELEASE_PLAN = (ROOT / "docs" / "RELEASE_PLAN.md").read_text(encoding="utf-8")
FULL_GROSS = "config/full_gross.yaml"
FINDING_ID = re.compile(r"\bC[1-3]-F\d+\b")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_sha(value: Any) -> str:
    return _sha(json.dumps(value, sort_keys=True).encode())


def _section(number: int) -> str:
    match = re.search(rf"^## {number}\. .*?(?=^## {number + 1}\. |\Z)", CONTRACT, re.M | re.S)
    assert match, f"section {number} missing"
    return match.group(0)


def _registry() -> dict[str, Any]:
    match = re.search(r"^```yaml\n(.*?)\n```", _section(12), re.M | re.S)
    assert match, "§12 holds no YAML registry"
    data = yaml.safe_load(match.group(1))
    assert isinstance(data, dict)
    return data


REGISTRY = _registry()
PSTAR = REGISTRY["pstar"]
SELECTION = REGISTRY["selection"]


def _derive_all() -> tuple[dict[str, Any], RunProfile]:
    source = yaml.safe_load((ROOT / FULL_GROSS).read_text(encoding="utf-8"))
    sha = _sha((ROOT / FULL_GROSS).read_bytes())
    return derive(source, "all", source_path=FULL_GROSS, source_sha256=sha)


def _render(name: str, options: dict[str, Any] | None) -> RunProfile:
    """The P*-rendered single-identity profile of contract §4.4."""
    source = yaml.safe_load((ROOT / FULL_GROSS).read_text(encoding="utf-8"))
    document: dict[str, Any] = {"schema_version": 2, "algorithms": [name]}
    for key in ("objective", "search", "budget", "measurement", "worker"):
        document[key] = json.loads(json.dumps(source[key]))
    document["graph"] = dict(PSTAR["shared"]["graph"])
    if options is not None:
        document["algorithm_options"] = {name: options}
    return parse_profile(document, f"<P* {name}>")


def _cec(profile: RunProfile, name: str) -> dict[str, Any]:
    """The candidate effective configuration of contract §4.3."""
    resolved = profile.resolved()
    entry = profile.algorithm_options.get(name)
    return {
        "algorithm": name,
        **{k: resolved[k] for k in ("objective", "budget", "measurement", "worker", "search")},
        "params": resolved["algorithm_config"][name]["params"],
        "options": None if entry is None else entry["options"],
        "settings_sha256": None if entry is None else entry["settings_sha256"],
    }


def _base_options(name: str) -> dict[str, Any] | None:
    factory = ALGORITHMS[name]
    return preset_options(factory) if factory.options_preset is not None else None


def _candidates(identity: str) -> dict[str, dict[str, Any]]:
    """Candidate id -> options, the Cartesian grid of contract §5.2 (labels fill the id)."""
    spec = SELECTION["identities"][identity]
    grid = spec["grid"]
    axes = list(grid)
    values = [
        list(grid[a].items()) if isinstance(grid[a], dict) else [(v, v) for v in grid[a]]
        for a in axes
    ]
    out: dict[str, dict[str, Any]] = {}
    for combo in itertools.product(*values):
        labels = {a: label for a, (label, _) in zip(axes, combo, strict=True)}
        options = {a: value for a, (_, value) in zip(axes, combo, strict=True)}
        options.update(spec["fixed"])
        if options["base"] in spec.get("base_options_from_preset", []):
            options["base_options"] = _base_options(options["base"])
        out[spec["id_format"].format(**labels)] = options
    return out


ALL_CANDIDATES = {**_candidates("split_polish"), **_candidates("marginal_activation")}


# ----------------------------------------------------------------------------- structure


def test_contract_has_every_required_section() -> None:
    headings = re.findall(r"^## (\d+)\. ", CONTRACT, re.M)
    assert headings == [str(n) for n in range(1, 15)]
    required = {
        2: ["P1", "P8", "D1-F2", "11.815", "10.191", "9d20d2aa"],
        3: ["E2full-A0 2.458", "previously_exposed", "never read"],
        4: ["CEC", "Non-transfer", "daily_gross.yaml"],
        5: ["no_selection", "blocked_defect", "ε = 1/100 bps", "§5.12", "never loosened"],
        6: ["R024_ADDITIONS", "I1", "I4", "(invocation id, registered roster)"],
        7: ["Q19-sor", "E2-wm", "C15", "rankable", "Not scheduled"],
        8: [
            "Departure from R022",
            "N = 3 started attempts per",
            "First valid attempt",
            "Whole attempts only",
            "Never a trigger",
            "measurement attempted correctly",
            "usable latency obtained",
        ],
        9: ["`reject`", "`inconclusive`", "`keep_experimental`", "not adoption"],
        10: ["not a runtime ceiling", "Resume", "6 lanes"],
        11: ["held-out block", "Change control"],
    }
    for number, phrases in required.items():
        body = " ".join(_section(number).split())  # prose is hard-wrapped
        missing = [p for p in phrases if p not in body]
        assert missing == [], (number, missing)


def test_contract_does_not_point_at_local_drafts() -> None:
    assert "/tmp" not in CONTRACT


def test_relative_links_resolve() -> None:
    for doc in sorted(R024.rglob("*.md")):
        for target in re.findall(r"\]\(([^)#:]+)\)", doc.read_text(encoding="utf-8")):
            assert (doc.parent / target).exists(), (doc, target)


# ----------------------------------------------------------------------------- P* (§4)


def test_pstar_is_the_loaders_all_derivation_of_full_gross() -> None:
    assert _sha((ROOT / FULL_GROSS).read_bytes()) == PSTAR["source"]["sha256"]
    m4 = PSTAR["m4_settings"]
    assert (
        _sha((ROOT / m4["path"]).read_bytes())
        == m4["sha256"]
        == strategies.METIS_SETTINGS["sha256"]
    )
    _, profile = _derive_all()
    resolved = profile.resolved()
    shared = {k: resolved[k] for k in PSTAR["shared"]}
    assert shared == PSTAR["shared"]
    assert _canonical_sha(shared) == _canonical_sha(PSTAR["shared"]) == PSTAR["shared_sha256"]


def test_the_17_identity_derivation_keeps_its_resolved_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant I1 for P*: with the 0.2.4 additions removed, `all` resolves as on dbc9432."""
    monkeypatch.setattr(strategies, "R024_ADDITIONS", (), raising=False)
    _, profile = _derive_all()
    assert list(profile.algorithms) == REGISTRY["roster"]["all19"][:17]
    assert _canonical_sha(profile.resolved()) == PSTAR["all17_resolved_profile_sha256"]


@pytest.mark.parametrize("base", list(PSTAR["bases"]))
def test_base_configurations_are_pinned(base: str) -> None:
    pin = PSTAR["bases"][base]
    rendered = _cec(_render(base, _base_options(base)), base)
    _, all_profile = _derive_all()
    assert rendered == _cec(all_profile, base)  # the ordinary row is the reference arm
    assert _canonical_sha(rendered) == pin["cec_sha256"]
    if "preset" in pin:
        assert _sha((ROOT / pin["preset"]["path"]).read_bytes()) == pin["preset"]["sha256"]
        assert all_profile.algorithm_options[base]["settings_sha256"] == pin["settings_sha256"]
    assert SELECTION["quality"]["reference_set"] == list(PSTAR["bases"])


# ----------------------------------------------------------------------------- grid (§5.2)


def test_candidate_grids_are_the_registered_size_and_representable_under_pstar() -> None:
    sizes = {"split_polish": 90, "marginal_activation": 108}
    pstar_graph = set(PSTAR["shared"]["graph"])
    for identity, size in sizes.items():
        candidates = _candidates(identity)
        assert len(candidates) == size == SELECTION["identities"][identity]["candidates"]
        factory = ALGORITHMS[identity]
        for cid, options in candidates.items():
            normalized = validated_options(factory, options)
            assert normalized == options, cid  # already in normalized (preset) form
            keys = factory.graph_params_for(normalized) if factory.graph_params_for else ()
            assert set(keys) | set(factory.graph_params) <= pstar_graph, cid
            assert _render(identity, options).algorithm_options[identity]["options"] == options
    assert len(ALL_CANDIDATES) == 198


def test_fixed_values_and_ladders_are_the_registered_ones() -> None:
    sp = SELECTION["identities"]["split_polish"]
    ma = SELECTION["identities"]["marginal_activation"]
    assert sp["grid"]["rounds"] == [1, 2, 4]
    assert sp["grid"]["tolerance"] == {"1e-3": 0.001, "1e-4": 0.0001, "1e-5": 0.00001}
    assert sp["fixed"] == {"grid": 10**9, "maxiter": 60}
    assert ma["grid"]["activations"] == [1, 2, 4] and ma["grid"]["top_k"] == [1, 3, 9]
    assert ma["fixed"] == {
        "solver": "brent",
        "rounds": 2,
        "tolerance": 0.0001,
        "grid": 10**9,
        "maxiter": 60,
        "seed_share": 0.0001,
        "arm": "treatment",
    }


def test_reused_r023_configurations_are_equivalent_under_pstar() -> None:
    reuse = SELECTION["reuse"]
    sums_path = ROOT / reuse["sums"]["path"]
    assert _sha(sums_path.read_bytes()) == reuse["sums"]["sha256"]
    pinned = {
        name for _, name in (line.split("  ", 1) for line in sums_path.read_text().splitlines())
    }
    assert len(reuse["configurations"]) == 13
    for entry in reuse["configurations"]:
        old = load_profile(
            ROOT / "config" / "research_023" / "profiles" / f"{entry['r023_profile']}.yaml"
        )
        name = old.algorithms[0]
        cid = entry["candidate"]
        options = _base_options(name) if cid.startswith("ref|") else ALL_CANDIDATES[cid]
        assert _cec(old, name) == _cec(_render(name, options), name), cid
        for run in entry["runs"]:
            for leaf in ("cases.jsonl", "manifest.json"):
                assert any(re.fullmatch(rf"{run}/[^/]+/{leaf}", p) for p in pinned), (run, leaf)


# ----------------------------------------------------------------------------- rule (§5.5-§5.7)


def _select(
    example: dict[str, Any], *, epsilon: Fraction, limits: dict[str, Any], order: list[str]
) -> tuple[str, str | None, list[str]]:
    reference = example["reference"]["gross"]
    reference_work = example["reference"]["work"]
    scored = [i for i, r in enumerate(reference) if r > 0]
    failure = SELECTION["quality"]["failure_gross"]

    def quality(c: dict[str, Any]) -> Fraction:
        total = sum(
            (
                Fraction(
                    10**4 * ((failure if c["gross"][i] is None else c["gross"][i]) - reference[i]),
                    reference[i],
                )
                for i in scored
            ),
            Fraction(0),
        )
        return total / len(scored)

    def within_limits(c: dict[str, Any]) -> bool:
        for unit, limit in limits.items():
            num, den = c["work"][unit], reference_work[unit]
            if den == 0 and num > 0:
                return False  # positive / 0 = infinite
            ratio = Fraction(1) if den == 0 else Fraction(num, den)  # 0 / 0 = 1
            if ratio > Fraction(str(limit)):
                return False
        return True

    if not scored:
        return "no_selection", None, []
    eligible = [c for c in example["candidates"] if c["gates"] == "pass" and within_limits(c)]
    if not eligible:
        return "no_selection", None, []
    best = max(quality(c) for c in eligible)
    band = [c for c in eligible if quality(c) >= best - epsilon]
    key = [k for k in order]
    winner = min(
        band, key=lambda c: tuple(c["id"] if k == "candidate_id" else c["work"][k] for k in key)
    )
    return "selected", winner["id"], [c["id"] for c in eligible]


RULE = {
    "epsilon": Fraction(SELECTION["rule"]["epsilon_bps"]),
    "limits": SELECTION["work"]["limits"],
    "order": SELECTION["rule"]["tie_break"],
}


def test_rule_constants_are_the_registered_ones() -> None:
    assert SELECTION["rule"] == {
        "epsilon_bps": "1/100",
        "tie_break": ["quotes", "cl_swap_steps", "lb_bins_swapped", "candidate_id"],
    }
    work = SELECTION["work"]
    assert work["reference"] == "incremental_graph"
    assert work["limits"] == {"quotes": 2, "cl_swap_steps": 2, "lb_bins_swapped": 2}
    assert (work["zero_over_zero"], work["positive_over_zero"]) == (1, "infinite")
    assert SELECTION["quality"]["failure_gross"] == 0
    assert (
        SELECTION["split"]["bundle_hash"]
        == REGISTRY["campaign"]["inputs"]["tuning_full"]["bundle_hash"]
    )
    report = {
        REGISTRY["campaign"]["inputs"][k]["bundle_hash"] for k in ("report_full", "report_sor")
    }
    assert set(SELECTION["forbidden_bundle_hashes"]) == report


@pytest.mark.parametrize("example", SELECTION["worked_examples"], ids=lambda e: e["id"])
def test_worked_examples_reproduce(example: dict[str, Any]) -> None:
    outcome, winner, eligible = _select(example, **RULE)
    expected = example["expected"]
    assert (outcome, winner, eligible) == (
        expected["outcome"],
        expected["winner"],
        expected["eligible"],
    )


def test_worked_examples_bind_epsilon_limit_and_tie_break() -> None:
    """Each pinned rule value is load-bearing: changing it changes an example's outcome."""
    examples = {e["id"].split("-")[0]: e for e in SELECTION["worked_examples"]}

    def winner(name: str, **change: Any) -> str | None:
        return _select(examples[name], **{**RULE, **change})[1]

    assert winner("WE1", epsilon=Fraction(0)) != examples["WE1"]["expected"]["winner"]
    looser = {**RULE["limits"], "quotes": Fraction(5, 2)}
    assert winner("WE2", limits=looser) != examples["WE2"]["expected"]["winner"]
    swapped = ["cl_swap_steps", "quotes", "lb_bins_swapped", "candidate_id"]
    assert winner("WE3", order=swapped) != examples["WE3"]["expected"]["winner"]


# ----------------------------------------------------------------------------- roster (§6)


def _check_output(tool: str) -> list[str]:
    done = subprocess.run(
        [sys.executable, tool, "check"], cwd=ROOT, capture_output=True, text=True, timeout=300
    )
    return (done.stderr + done.stdout).strip().splitlines()  # problems on stderr, summary on stdout


def test_roster_order_is_the_registered_one() -> None:
    roster = REGISTRY["roster"]
    assert roster["all19"][-2:] == roster["additions"] == ["split_polish", "marginal_activation"]
    assert len(roster["all19"]) == len(set(roster["all19"])) == 19
    _, profile = _derive_all()
    derived = list(profile.algorithms)
    assert derived[:17] == roster["all19"][:17]
    assert derived == [a for a in roster["all19"] if a in derived]  # additions in contract order


def test_research_021_reports_exactly_the_registered_finding_identities() -> None:
    lines = _check_output("tools/research_021/campaign.py")
    assert lines[-1] == "check: 9 problem(s)"
    pattern = re.compile(r"^(\S+): derives (\[.*?\]), registered (\[.*\])$")
    found = set()
    for line in lines[:-1]:
        match = pattern.match(line)
        assert match, line
        found.add((match.group(1), tuple(ast.literal_eval(match.group(3)))))
    known = {
        (f["invocation"], tuple(f["registered"]))
        for f in REGISTRY["roster"]["research_021_known_findings"]
    }
    assert len(known) == 9 and found == known


@pytest.mark.parametrize(
    "tool", ["tools/research_022/pruning_campaign.py", "tools/research_023/campaign.py"]
)
def test_later_campaign_checks_stay_clean(tool: str) -> None:
    assert _check_output(tool)[-1] == "check: 0 problem(s)"


# ----------------------------------------------------------------------------- timing (§8)


def test_timing_units_and_reattempt_policy_are_the_registered_ones() -> None:
    timing = REGISTRY["timing"]
    protocol = timing["protocol"]
    assert _sha((ROOT / protocol["source"]).read_bytes()) == protocol["source_sha256"]
    assert protocol["replaced"] == ["key", "profile"] and protocol["key"] == "L01-R024"
    units = timing["units"]
    assert [u["unit"] for u in units] == ["UP", "U1", "U2", "U3", "U4", "U5", "U6", "UQ"]
    descriptive = [a for u in units if u["kind"] == "descriptive" for a in u["algorithms"]]
    assert descriptive == REGISTRY["roster"]["all19"][:17]  # each earlier identity once, in order
    assert units[0]["candidate"] == REGISTRY["roster"]["additions"]
    assert units[-1] == {"unit": "UQ", "kind": "quote_cli", "invocations": 5, "strategies": "all"}
    assert timing["max_started_attempts_per_unit"] == 3
    assert timing["launch"] == {
        "samples": 5,
        "interval_seconds": 30,
        "headroom_load1": 3.0,
        "resample_every_seconds": 300,
        "max_wait_seconds": 21600,
    }
    assert timing["validity"]["max_load1_per_cpu"] == 0.5
    assert timing["validity"]["max_sample_gap_seconds"] == 90
    assert timing["validity"]["sleep_entries_allowed"] == 0
    assert timing["triggers"] == [
        "T1_load",
        "T2_sampling_coverage",
        "T3_sleep",
        "T4_sleep_prevention_or_capture",
        "T5_incomplete_execution",
    ]
    assert {"solver_timeout", "regression", "insufficient_speedup", "noisy_comparison"} <= set(
        timing["never_triggers"]
    )
    assert not set(timing["triggers"]) & set(timing["never_triggers"])


# ----------------------------------------------------------------------------- release plan, review


def _plan_section(title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}.*?(?=^## |\Z)", RELEASE_PLAN, re.M | re.S)
    assert match, title
    return " ".join(match.group(0).split())


def test_release_plan_has_the_024_gates_and_the_six_023_issues() -> None:
    section = _plan_section("0.2.4 — ")
    for phrase in [
        "appears once in the ordinary comparison",
        "old saved profiles replay literally",
        "no case or status row disappears",
        "unlike domains are not ranked as like-for-like",
        "pre-registered raw records, hashes, commands and environment",
        "deterministic work is the primary evidence",
        "L01 quiet-host gate",
        "`inconclusive`",
        "independently checked worked examples",
        "full suite",
        "whole-release review gate",
        "research-024/contract.md",
        *(f"WHI-{n}" for n in range(1629, 1635)),
    ]:
        assert phrase in section, phrase
    previous = _plan_section("0.2.3 — ")
    assert "Planned issues: 3" not in previous
    for issue in ("WHI-1622", "WHI-1623", "WHI-1624", "WHI-1626", "WHI-1627", "WHI-1628"):
        assert issue in previous, issue


def test_every_review_finding_has_one_disposition() -> None:
    raised: set[str] = set()
    for report in sorted((R024 / "reviews").glob("round-*.md")):
        raised |= set(FINDING_ID.findall(report.read_text(encoding="utf-8")))
    rows = re.findall(r"^\| (C[1-3]-F\d+) \| ([^|]+) \| ([^|]+) \|", _section(13), re.M)
    ids = [row[0] for row in rows]
    assert len(ids) == len(set(ids))
    assert set(ids) == raised
    assert all(sev.strip() in {"blocking", "suggestion"} and disp.strip() for _, sev, disp in rows)

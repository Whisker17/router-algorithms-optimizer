"""Offline ingestion checks for the Uniswap SOR goldens (WHI-1443 / I22).

`tests/fixtures/uni_sor/` holds frozen inputs and the goldens that the actual pinned
`@uniswap/smart-order-router@4.31.10` produced from them (`tools/upstream/uni_sor`).
These tests need neither Node nor network. They check that the fixtures are

- well-formed (input/golden schema, canonical JSON, manifest hashes);
- complete over the contract's golden categories G-1…G-14
  (`docs/references/uni-sor-port-contract.md` §8), each case pinning what it claims and
  differing from the naive alternative it names;
- provenance-consistent with the pinned inventory (commit, npm integrity, lockfile,
  Node/V8, overrides, harness file revisions, upstream calls);
- internally consistent with the contract's reporting rules (B-A1, B-Q1/B-Q2, B-Q4,
  B-S9, B-S11, B-F2) — structural cross-checks, not a re-implementation of SOR;
- reproducible on the input side (`author_inputs.build_inputs()` is byte-identical).

WHI-1444's parity test consumes the same files.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from fractions import Fraction
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "uni_sor"
HARNESS = REPO / "tools" / "upstream" / "uni_sor"
INVENTORY = REPO / "docs" / "references" / "uni-sor-source-inventory.json"
CONTRACT = "docs/references/uni-sor-port-contract.md"

FAMILIES = ("V3", "V2", "MIXED")
CATEGORIES = [f"G-{i}" for i in range(1, 15)]
UPSTREAM_CALLS = [
    "AlphaRouter.prototype.getAmountDistribution",
    "computeAllV3Routes",
    "computeAllV2Routes",
    "computeAllMixedRoutes",
    "mixedRouteFilterOutV4Pools",
    "V3Quoter.getQuotes",
    "V2Quoter.getQuotes",
    "MixedQuoter.getQuotes",
    "getBestSwapRoute",
]
DIAGNOSTIC_CALLS = [*UPSTREAM_CALLS[:-1], "getBestSwapRouteBy"]
UINT = re.compile(r"^(0|[1-9][0-9]*)$")
INT = re.compile(r"^-?(0|[1-9][0-9]*)$")

Json = dict[str, Any]


def _canonical(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@cache
def _inventory() -> Json:
    data: Json = json.loads(INVENTORY.read_text(encoding="utf-8"))
    return data


@cache
def _manifest() -> Json:
    data: Json = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
    return data


@cache
def _load(name: str) -> Json:
    data: Json = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def _case_ids() -> list[str]:
    return [str(c["case_id"]) for c in _manifest()["cases"]]


def _pair(case_id: str) -> tuple[Json, Json]:
    return _load(f"{case_id}.input.json"), _load(f"{case_id}.golden.json")


def _selection(golden: Json) -> list[Json] | None:
    result = golden["result"]
    if result is None:
        return None
    return [{"pool_ids": r["pool_ids"], "percent": r["percent"]} for r in result["routes"]]


def _frac(obj: Json) -> Fraction:
    return Fraction(int(obj["numerator"]), int(obj["denominator"]))


def _author_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "uni_sor_author_inputs", HARNESS / "author_inputs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- manifest, file set, canonical form -------------------------------------------


def test_manifest_lists_exactly_the_fixture_files_with_matching_hashes() -> None:
    manifest = _manifest()
    assert manifest["schema"] == "uni-sor-golden-manifest/1"
    assert manifest["command"] == "tools/upstream/uni_sor/regen.sh"
    assert manifest["contract"] == CONTRACT
    listed = {"MANIFEST.json", "README.md"}
    for entry in manifest["cases"]:
        for kind in ("input", "golden"):
            data = (FIXTURES / entry[kind]).read_bytes()
            assert _sha256(data) == entry[f"{kind}_sha256"], entry[kind]
            listed.add(entry[kind])
        assert entry["input"] == f"{entry['case_id']}.input.json"
        assert entry["golden"] == f"{entry['case_id']}.golden.json"
    assert {p.name for p in FIXTURES.iterdir()} == listed
    assert _case_ids() == sorted(_case_ids())


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.json")))
def test_every_fixture_is_canonical_json(name: str) -> None:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    assert text == _canonical(json.loads(text)), f"{name} is not canonical JSON"


def test_inputs_are_reproduced_byte_for_byte_by_the_author_script() -> None:
    built: dict[str, str] = _author_module().build_inputs()
    assert sorted(built) == _case_ids()
    for case_id, text in built.items():
        assert (FIXTURES / f"{case_id}.input.json").read_text(encoding="utf-8") == text, case_id


# --- input schema -------------------------------------------------------------------


@pytest.mark.parametrize("case_id", _case_ids())
def test_input_is_well_formed(case_id: str) -> None:
    inp = _load(f"{case_id}.input.json")
    assert inp["schema"] == "uni-sor-golden-input/1"
    assert inp["case_id"] == case_id
    assert inp["chain_id"] == 1  # A-4
    assert set(inp["categories"]) <= set(CATEGORIES) and inp["categories"]
    assert all(re.fullmatch(r"B-[A-Z]+\d+", b) for b in inp["behaviours"])
    assert inp["description"] and inp["naive_alternative"]
    assert UINT.match(inp["amount_in_raw"]) and int(inp["amount_in_raw"]) > 0
    routing = inp["routing"]
    assert set(routing) == {"max_hops", "percent_step", "min_splits", "max_splits"}
    assert all(isinstance(v, int) and v > 0 for v in routing.values())
    assert routing["max_splits"] < 64  # B-F1
    tokens = {t["address"] for t in inp["tokens"]}
    assert len(tokens) == len(inp["tokens"])
    assert all(re.fullmatch(r"0x[0-9a-f]{40}", t) for t in tokens)
    assert "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2" not in tokens  # A-4
    assert {inp["token_in"], inp["token_out"]} <= tokens
    assert set(inp["pools"]) == {"V3", "V2"}
    ids = [p["pool_id"] for fam in ("V3", "V2") for p in inp["pools"][fam]]
    assert len(ids) == len(set(ids))
    for p in inp["pools"]["V3"]:
        assert set(p) == {"pool_id", "token0", "token1", "fee"}
    for p in inp["pools"]["V2"]:
        assert set(p) == {"pool_id", "token0", "token1"}
    for fam in ("V3", "V2"):
        for p in inp["pools"][fam]:
            assert {p["token0"], p["token1"]} <= tokens and p["token0"] != p["token1"]
    keys = set()
    for q in inp["quotes"]:
        assert q["family"] in FAMILIES
        assert set(q["route_pool_ids"]) <= set(ids)
        key = (q["family"], tuple(q["route_pool_ids"]), q["percent"])
        assert key not in keys
        keys.add(key)
        gas = ("gas_estimate", "gas_cost_in_quote_token", "gas_cost_usd_raw")
        if q["raw_quote"] is None:
            assert all(q[k] is None for k in gas)
        else:
            assert all(UINT.match(q[k]) for k in ("raw_quote", *gas))
    assert inp["adaptations"] == sorted(set(inp["adaptations"]))
    assert {"A-2", "A-3", "A-4", "A-5", "A-8"} <= set(inp["adaptations"])


def test_corpus_inputs_use_the_frozen_cohort_in_pool_id_order() -> None:
    corpus = json.loads((REPO / "tests/fixtures/corpus/bundle/corpus.json").read_text())
    cohort = set(corpus["cohorts"]["sor_compatible"]["pools"])
    corpus_cases = [
        c for c in _case_ids() if _load(f"{c}.input.json")["origin"]["kind"] == "corpus"
    ]
    assert corpus_cases, "no corpus-shaped case"
    for case_id in corpus_cases:
        inp = _load(f"{case_id}.input.json")
        origin = inp["origin"]
        assert origin["bundle"] == "tests/fixtures/corpus/bundle"
        assert origin["cohort"] == "sor_compatible"
        assert "A-1" in inp["adaptations"]
        for fam in ("V3", "V2"):
            ids = [p["pool_id"] for p in inp["pools"][fam]]
            assert ids == sorted(ids), f"{case_id}: {fam} list not in A-1 order"
        assert {p["pool_id"] for fam in ("V3", "V2") for p in inp["pools"][fam]} == cohort


# --- golden schema and provenance -----------------------------------------------------


@pytest.mark.parametrize("case_id", _case_ids())
def test_golden_provenance_matches_the_pinned_inventory(case_id: str) -> None:
    inp, gold = _pair(case_id)
    inv = _inventory()
    assert gold["schema"] == "uni-sor-golden/1"
    assert gold["case_id"] == case_id and gold["categories"] == inp["categories"]
    assert gold["input_file"] == f"tests/fixtures/uni_sor/{case_id}.input.json"
    assert gold["input_sha256"] == _sha256((FIXTURES / f"{case_id}.input.json").read_bytes())
    assert gold["routing"] == inp["routing"]
    assert gold["routing_config"] == {
        "minSplits": inp["routing"]["min_splits"],
        "maxSplits": inp["routing"]["max_splits"],
        "maxSwapsPerPath": inp["routing"]["max_hops"],
        "distributionPercent": inp["routing"]["percent_step"],
        "forceCrossProtocol": False,
        "forceMixedRoutes": False,
    }
    assert gold["adaptations"] == inp["adaptations"]
    prov = gold["provenance"]
    assert prov["contract"] == CONTRACT and prov["chain_id"] == 1
    up = prov["upstream"]
    assert up["commit"] == inv["upstream"]["commit"]
    assert up["repository"] == inv["upstream"]["repository"]
    assert up["package"] == inv["upstream"]["package_name"]
    assert up["version"] == inv["upstream"]["package_version"]
    assert up["npm_integrity"] == inv["npm_artifact"]["integrity"]
    assert up["npm_git_head"] == inv["npm_artifact"]["git_head"]
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in up["build_files_sha256"].values())
    node = inv["harness_dependency_pins"]["node"]
    versions = prov["toolchain"]["process_versions"]
    assert "v" + versions["node"] == node["version"] and versions["v8"] == node["v8"]
    assert UINT.match(prov["toolchain"]["npm"].replace(".", "")) is not None
    assert prov["toolchain"]["npm"] == _manifest()["npm"]
    packages = prov["packages"]
    assert packages[inv["upstream"]["package_name"]] == {
        "version": inv["upstream"]["package_version"],
        "integrity": inv["npm_artifact"]["integrity"],
    }
    lock = json.loads((HARNESS / "package-lock.json").read_text())
    for dep in inv["behavior_relevant_dependencies"]:
        entry = lock["packages"][f"node_modules/{dep['name']}"]
        assert packages[dep["name"]] == {"version": dep["version"], "integrity": entry["integrity"]}
    harness = prov["harness"]
    assert harness["command"] == "tools/upstream/uni_sor/regen.sh"
    assert harness["install"] == "npm ci --ignore-scripts"
    assert harness["package_lock_sha256"] == _sha256((HARNESS / "package-lock.json").read_bytes())
    for rel, blob in harness["git_blob_sha1"].items():
        assert _git_blob((REPO / rel).read_bytes()) == blob, f"{rel} changed; regenerate"
    assert prov["upstream_calls"] == UPSTREAM_CALLS
    assert prov["diagnostic_calls"] == DIAGNOSTIC_CALLS


def test_all_goldens_share_one_toolchain_and_harness_revision() -> None:
    shared = {
        _canonical({k: v for k, v in _pair(c)[1]["provenance"].items() if "calls" not in k})
        for c in _case_ids()
    }
    assert len(shared) == 1


def test_harness_lock_pins_the_inventory() -> None:
    inv = _inventory()
    pkg = json.loads((HARNESS / "package.json").read_text())
    assert pkg["overrides"] == inv["harness_dependency_pins"]["direct_overrides"]
    assert pkg["dependencies"] == {
        inv["upstream"]["package_name"]: inv["upstream"]["package_version"]
    }
    assert pkg["engines"]["node"] == inv["harness_dependency_pins"]["node"]["version"].lstrip("v")
    assert pkg["license"] == "GPL-3.0-only" and pkg["private"] is True
    assert (HARNESS / ".nvmrc").read_text().strip() == inv["harness_dependency_pins"]["node"][
        "version"
    ]
    lock = json.loads((HARNESS / "package-lock.json").read_text())
    sor = lock["packages"][f"node_modules/{inv['upstream']['package_name']}"]
    assert sor["version"] == inv["upstream"]["package_version"]
    assert sor["integrity"] == inv["npm_artifact"]["integrity"]
    for name, version in inv["harness_dependency_pins"]["direct_overrides"].items():
        copies = [
            v["version"]
            for k, v in lock["packages"].items()
            if k == f"node_modules/{name}" or k.endswith(f"/node_modules/{name}")
        ]
        assert copies and set(copies) == {version}, name  # one resolved version per pin
    assert _manifest()["package_lock_sha256"] == _sha256(
        (HARNESS / "package-lock.json").read_bytes()
    )


# --- golden structure and contract cross-checks -----------------------------------------


@pytest.mark.parametrize("case_id", _case_ids())
def test_golden_is_consistent_with_its_input_and_the_contract(case_id: str) -> None:
    inp, gold = _pair(case_id)
    amount = int(inp["amount_in_raw"])
    step = inp["routing"]["percent_step"]
    # B-A1: grid i*d for i <= 100/d, exact rational amounts, quotient = floor.
    assert gold["percents"] == [i * step for i in range(1, int(100 / step) + 1)]
    for a in gold["amounts"]:
        assert _frac(a) == Fraction(amount * a["percent"], 100)
        assert int(a["quotient"]) == amount * a["percent"] // 100
    # Enumerated routes cover exactly the table's routes (contract §7.4), pool ids resolve.
    table_routes = {(q["family"], tuple(q["route_pool_ids"])) for q in inp["quotes"]}
    enumerated = {(fam, tuple(r["pool_ids"])) for fam in FAMILIES for r in gold["routes"][fam]}
    assert set(gold["routes"]) == set(FAMILIES)
    assert enumerated == table_routes
    pools = {p["pool_id"]: p for fam in ("V3", "V2") for p in inp["pools"][fam]}
    fam_of = {p["pool_id"]: fam for fam in ("V3", "V2") for p in inp["pools"][fam]}
    for fam in FAMILIES:
        for r in gold["routes"][fam]:
            assert 1 <= len(r["pool_ids"]) <= inp["routing"]["max_hops"]  # B-R2
            assert r["token_path"][0] == inp["token_in"] and r["token_path"][-1] == inp["token_out"]
            assert len(r["token_path"]) == len(r["pool_ids"]) + 1
            assert len(set(r["token_path"])) == len(r["token_path"])  # B-R3/B-R4
            for hop, pid in enumerate(r["pool_ids"]):
                assert {r["token_path"][hop], r["token_path"][hop + 1]} == {
                    pools[pid]["token0"],
                    pools[pid]["token1"],
                }
            kinds = {fam_of[pid] for pid in r["pool_ids"]}
            assert kinds == ({"V3", "V2"} if fam == "MIXED" else {fam})  # B-R7/B-R8
    # B-Q1/B-Q2: V3, V2, MIXED blocks; route order, then percent; null rows dropped.
    table = {(q["family"], tuple(q["route_pool_ids"]), q["percent"]): q for q in inp["quotes"]}
    expected_list = [
        (fam, tuple(r["pool_ids"]), p)
        for fam in FAMILIES
        for r in gold["routes"][fam]
        for p in gold["percents"]
        if table[(fam, tuple(r["pool_ids"]), p)]["raw_quote"] is not None
    ]
    listed = gold["routes_with_valid_quotes"]
    assert [(e["protocol"], tuple(e["pool_ids"]), e["percent"]) for e in listed] == expected_list
    for i, e in enumerate(listed):
        q = table[(e["protocol"], tuple(e["pool_ids"]), e["percent"])]
        assert e["list_index"] == i
        assert e["raw_quote"] == e["quote"] == q["raw_quote"]
        assert e["gas_estimate"] == q["gas_estimate"]
        assert e["gas_cost_in_token"] == q["gas_cost_in_quote_token"]
        assert e["gas_cost_in_usd"] == q["gas_cost_usd_raw"]
        assert INT.match(e["quote_adjusted_for_gas"])
        assert int(e["quote_adjusted_for_gas"]) == int(e["quote"]) - int(e["gas_cost_in_token"])
    # Diagnostic: every list entry appears once in its percent group (B-S1).
    groups = gold["diagnostic"]["sorted_by_percent"]
    assert gold["diagnostic"]["selection_matches_primary"] is True
    assert [g["percent"] for g in groups] == sorted({e["percent"] for e in listed})
    assert sorted(i for g in groups for i in g["list_indices"]) == list(range(len(listed)))
    for g in groups:
        idx = g["list_indices"]
        assert all(listed[i]["percent"] == g["percent"] for i in idx)
        values = [int(listed[i]["quote_adjusted_for_gas"]) for i in idx]
        assert values == sorted(values, reverse=True)  # B-S2 descending
    result = gold["result"]
    if result is None:
        return
    routes = result["routes"]
    assert inp["routing"]["min_splits"] <= len(routes) <= inp["routing"]["max_splits"]
    assert sum(r["percent"] for r in routes) == 100
    used: set[str] = set()
    for r in routes:
        assert listed[r["list_index"]] == {
            k: v for k, v in r.items() if k not in ("token_path", "amount")
        }
        assert set(r["pool_ids"]).isdisjoint(used)  # B-S9
        used |= set(r["pool_ids"])
        assert _frac(r["amount"]) == Fraction(amount * r["percent"], 100)
        route = next(x for x in gold["routes"][r["protocol"]] if x["pool_ids"] == r["pool_ids"])
        assert r["token_path"] == route["token_path"]
    # B-F1: non-increasing amounts; B-F2: upstream never adds a remainder on this grid.
    assert [r["percent"] for r in routes] == sorted((r["percent"] for r in routes), reverse=True)
    assert _frac(result["missing_amount"]) == 0 and result["remainder_added"] is False
    assert int(result["sum_of_quotients"]) == sum(int(r["amount"]["quotient"]) for r in routes)
    assert 0 <= amount - int(result["sum_of_quotients"]) <= len(routes) - 1
    # B-S11: cached totals.
    total = {k: sum(int(r[k]) for r in routes) for k in ("quote", "quote_adjusted_for_gas")}
    assert int(result["quote"]) == total["quote"]
    assert int(result["quote_gas_adjusted"]) == total["quote_adjusted_for_gas"]
    assert int(result["estimated_gas_used"]) == sum(int(r["gas_estimate"]) for r in routes)
    assert int(result["estimated_gas_used_quote_token"]) == sum(
        int(r["gas_cost_in_token"]) for r in routes
    )
    assert int(result["estimated_gas_used_usd"]) == sum(int(r["gas_cost_in_usd"]) for r in routes)


# --- category completeness and per-case claims ---------------------------------------


def test_every_contract_category_is_represented() -> None:
    contract = (REPO / CONTRACT).read_text(encoding="utf-8")
    assert re.findall(r"^\| (G-\d+) \|", contract, flags=re.MULTILINE) == CATEGORIES
    covered = {cat for c in _manifest()["cases"] for cat in c["categories"]}
    assert covered == set(CATEGORIES)


DISCRIMINATORS = (
    "naive_selection",
    "naive_min_routes",
    "properties",
    "absent_percents",
    "absent_routes",
    "routes",
    "percents",
    "sorted_by_percent",
)


def test_every_category_has_a_case_that_defeats_its_naive_alternative() -> None:
    """Contract §8: each category needs a case whose expected outcome differs from the
    named naive alternative. A case counts when it pins an outcome (selection, null
    result or exact route lists) *and* carries a machine-checked discriminator."""
    decisive: dict[str, list[str]] = {cat: [] for cat in CATEGORIES}
    for case_id in _case_ids():
        inp = _load(f"{case_id}.input.json")
        expect = inp["expect"]
        pinned = "selection" in expect or "routes" in expect or "result" in expect
        discriminated = any(k in expect for k in DISCRIMINATORS) or "result" in expect
        if pinned and discriminated:
            for cat in inp["categories"]:
                decisive[cat].append(case_id)
    assert [cat for cat, cases in decisive.items() if not cases] == []


@pytest.mark.parametrize("case_id", _case_ids())
def test_golden_meets_the_case_expectations(case_id: str) -> None:
    inp, gold = _pair(case_id)
    expect = inp["expect"]
    selection = _selection(gold)
    result = gold["result"]
    listed = gold["routes_with_valid_quotes"]
    assert set(expect) <= {
        "result",
        "routes",
        "selection",
        "naive_selection",
        "naive_min_routes",
        "properties",
        "list_length",
        "sorted_by_percent",
        "sum_of_quotients",
        "missing_amount_zero",
        "absent_percents",
        "absent_routes",
        "same_key_selected",
        "quote",
        "quote_gas_adjusted",
        "percents",
    }
    if "result" in expect:
        assert expect["result"] is None and result is None
    if "routes" in expect:
        assert {f: [r["pool_ids"] for r in gold["routes"][f]] for f in FAMILIES} == expect["routes"]
    if "selection" in expect:
        assert selection == expect["selection"]
    if "naive_selection" in expect:
        assert selection != expect["naive_selection"]
    if "naive_min_routes" in expect:
        assert selection is not None and len(selection) < expect["naive_min_routes"]
    if "list_length" in expect:
        assert len(listed) == expect["list_length"]
    if "sorted_by_percent" in expect:
        assert gold["diagnostic"]["sorted_by_percent"] == expect["sorted_by_percent"]
    if "percents" in expect:
        assert gold["percents"] == expect["percents"]
    if "absent_percents" in expect:
        present = {e["percent"] for e in listed}
        assert present.isdisjoint(expect["absent_percents"])
        assert set(expect["absent_percents"]) <= set(gold["percents"])
    if "absent_routes" in expect:
        present_routes = {tuple(e["pool_ids"]) for e in listed}
        enumerated = {tuple(r["pool_ids"]) for f in FAMILIES for r in gold["routes"][f]}
        for rt in expect["absent_routes"]:
            assert tuple(rt) in enumerated and tuple(rt) not in present_routes
    for key in ("sum_of_quotients", "quote", "quote_gas_adjusted"):
        if key in expect:
            assert result is not None and result[key] == expect[key]
    if expect.get("missing_amount_zero"):
        assert result is not None and _frac(result["missing_amount"]) == 0
        assert int(result["sum_of_quotients"]) < int(inp["amount_in_raw"])
    if expect.get("same_key_selected"):
        assert result is not None
        pools = {p["pool_id"]: p for p in inp["pools"]["V3"]}
        keys = [
            (
                pools[r["pool_ids"][0]]["token0"],
                pools[r["pool_ids"][0]]["token1"],
                pools[r["pool_ids"][0]]["fee"],
            )
            for r in result["routes"]
            if len(r["pool_ids"]) == 1 and r["pool_ids"][0] in pools
        ]
        assert len(keys) >= 2 and len(set(keys)) < len(keys)  # D-2 / A-5
    props = expect.get("properties", {})
    if props:
        assert selection is not None
    if "min_routes" in props:
        assert selection is not None and len(selection) >= props["min_routes"]
    if "routes" in props:
        assert selection is not None and len(selection) == props["routes"]
    if props.get("uneven"):
        assert selection is not None and len({s["percent"] for s in selection}) > 1
    if props.get("beats_best_single"):
        assert result is not None
        best_single = max(int(e["quote_adjusted_for_gas"]) for e in listed if e["percent"] == 100)
        assert int(result["quote_gas_adjusted"]) > best_single
        best_single_route = next(
            e["pool_ids"]
            for e in listed
            if e["percent"] == 100 and int(e["quote_adjusted_for_gas"]) == best_single
        )
        # G-5: at least one selected route is not the best 100 % route.
        assert any(r["pool_ids"] != best_single_route for r in result["routes"])
    if "residual" in props:
        assert result is not None
        assert int(inp["amount_in_raw"]) - int(result["sum_of_quotients"]) == props["residual"]


# --- notices and runtime isolation --------------------------------------------------------


def test_required_notices_are_retained() -> None:
    inv = _inventory()
    notice = (HARNESS / "NOTICE.md").read_text(encoding="utf-8")
    assert inv["upstream"]["commit"] in notice
    assert inv["npm_artifact"]["integrity"] in notice
    assert CONTRACT in notice
    assert inv["upstream"]["license_file"]["verbatim_copy"] in notice
    assert (REPO / inv["upstream"]["license_file"]["verbatim_copy"]).is_file()
    generate = (HARNESS / "generate.js").read_text(encoding="utf-8")
    assert generate.startswith("// SPDX-License-Identifier: GPL-3.0-only\n")
    readme = (FIXTURES / "README.md").read_text(encoding="utf-8")
    assert (
        "generated by the pinned GPL-3.0 SOR via tools/upstream/uni_sor; data, not code" in readme
    )
    assert (HARNESS / ".gitignore").read_text(encoding="utf-8").splitlines() == ["node_modules/"]


def test_no_node_harness_enters_the_python_runtime() -> None:
    runtime = [REPO / "main.py"] + [
        p
        for d in ("snapshot", "pools", "routing", "benchmark", "config")
        for p in (REPO / d).rglob("*.py")
    ]
    for path in runtime:
        text = path.read_text(encoding="utf-8")
        assert "tools/upstream" not in text and "tools.upstream" not in text, path
        assert "uni_sor" not in text or "uni_sor_port" in text, path


def test_fixture_readme_category_table_matches_the_manifest() -> None:
    readme = (FIXTURES / "README.md").read_text(encoding="utf-8")
    table = dict(re.findall(r"^\| (G-\d+) \| (.+) \|$", readme, flags=re.MULTILINE))
    expected: dict[str, list[str]] = {cat: [] for cat in CATEGORIES}
    for c in _manifest()["cases"]:
        for cat in c["categories"]:
            expected[cat].append(c["case_id"])
    assert {cat: re.findall(r"`([^`]+)`", cell) for cat, cell in table.items()} == expected

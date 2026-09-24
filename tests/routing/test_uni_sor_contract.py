"""Drift guard for the Uniswap SOR source/parity contract (WHI-1442).

`docs/references/uni-sor-port-contract.md` (prose), `uni-sor-source-inventory.json`
(machine-readable pins) and the verbatim notice files under
`docs/references/licenses/` must agree. WHI-1443 (harness) and WHI-1444 (port) build
against these pins, so a silent edit to one of them is a provenance break
(docs/DESIGN.md §2.7: "Refreshing this pin requires an explicit provenance update").
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
REFS = REPO / "docs" / "references"
CONTRACT = REFS / "uni-sor-port-contract.md"
INVENTORY = REFS / "uni-sor-source-inventory.json"

PIN = "04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647"


def _inventory() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(INVENTORY.read_text(encoding="utf-8"))
    return data


def _contract() -> str:
    return CONTRACT.read_text(encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_pin_agrees_across_design_contract_and_inventory() -> None:
    inv = _inventory()
    assert inv["upstream"]["commit"] == PIN
    assert inv["upstream"]["package_version"] == "4.31.10"
    assert PIN in _contract()
    assert PIN in (REPO / "docs" / "DESIGN.md").read_text(encoding="utf-8")


def test_npm_artifact_is_source_equivalent_and_quoted_in_contract() -> None:
    inv = _inventory()
    npm = inv["npm_artifact"]
    assert npm["git_head_is_ancestor_of_pin"] is True
    assert npm["git_head_src_tree"] == inv["upstream"]["src_tree"]
    text = _contract()
    assert npm["git_head"] in text
    # The contract abbreviates the integrity; its prefix and suffix must match.
    integrity = npm["integrity"]
    assert integrity.startswith("sha512-Bmg46K") and integrity.endswith("H74Q==")
    assert "sha512-Bmg46K" in text and "H74Q==" in text


def test_verbatim_notice_files_match_recorded_hashes() -> None:
    inv = _inventory()
    lic = inv["upstream"]["license_file"]
    sor_copy = REPO / lic["verbatim_copy"]
    assert _sha256(sor_copy) == lic["sha256"]
    assert sor_copy.read_text(encoding="utf-8").lstrip().startswith("GNU GENERAL PUBLIC LICENSE")
    assert "Version 3, 29 June 2007" in sor_copy.read_text(encoding="utf-8")

    v8 = inv["v8_sort_reference"]
    v8_copy = REPO / v8["verbatim_copy"]
    assert _sha256(v8_copy) == v8["license_file_sha256"]
    assert "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2" in v8_copy.read_text(encoding="utf-8")


def test_harness_overrides_pin_the_behavior_relevant_dependencies() -> None:
    inv = _inventory()
    overrides = inv["harness_dependency_pins"]["direct_overrides"]
    for dep in inv["behavior_relevant_dependencies"]:
        assert overrides.get(dep["name"]) == dep["version"], dep["name"]


def test_every_golden_category_and_behavior_id_is_defined_once() -> None:
    text = _contract()
    golden_rows = re.findall(r"^\| (G-\d+) \|", text, flags=re.MULTILINE)
    assert golden_rows == [f"G-{i}" for i in range(1, 15)]
    # Every behaviour/adaptation/deviation ID cited anywhere must have a definition.
    defined = set(re.findall(r"\*\*((?:B-[A-Z]+\d+|D-\d+))\b", text))
    defined |= set(re.findall(r"^\| (A-\d+) \|", text, flags=re.MULTILINE))
    cited = set(re.findall(r"\b((?:B-[A-Z]+\d+)|(?:A-\d+)|(?:D-\d+))\b", text))
    assert cited <= defined, sorted(cited - defined)


def test_no_concrete_restriction_is_recorded_without_contract_text() -> None:
    # If a concrete source restriction is ever recorded, the contract must name it.
    restrictions = _inventory()["concrete_restrictions_preventing_current_use"]
    text = _contract()
    for item in restrictions:
        assert item in text
    if not restrictions:
        assert "Concrete restriction" in text and "none" in text

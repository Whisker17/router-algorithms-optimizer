"""WHI-1686: a latency arm may select a subset of a pinned profile that declares `strategies` and a
`selection` record -- the effective 19-identity `--strategies all` document of
`config/full_gross.yaml`, timed as the R024-C/1 §8.2 timing units (`L01-R024`).

- every timing unit resolves, and each identity's resolved configuration, options and their
  provenance equal the full document's (the two R024 presets stay `preset`: `selection.mode: all`
  is kept, WHI-1632);
- every arm of the arms files checked in at `b17e119` keeps its resolved-profile hash;
- an invalid record is still refused (a selected or a dropped identity's `strategies` entry, the
  selection groups) and so is a tampered preset.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
import yaml

from benchmark.latency import Arm, LatencyError, arm_profile, load_arms, load_protocol
from benchmark.profile import read_profile_document
from benchmark.strategies import derive
from routing.algorithms import registry

REPO = Path(__file__).resolve().parents[2]
FULL = "config/full_gross.yaml"
# R024-C/1 §8.2: UP = the distinct bases of the two selected presets / the two new rows; U1-U6
UNITS = {
    "UP-base": ("incremental_graph", "metis_inspired"),
    "UP-cand": ("split_polish", "marginal_activation"),
    "U1": ("direct", "single_path", "direct_split", "path_split", "incremental_graph"),
    "U2": ("uni_sor_port",),
    "U3": (
        "uni_sor_adaptive",
        "uni_sor_optimized",
        "metis_inspired",
        "metis_history",
        "direct_split_certified",
    ),  # fmt: skip
    "U4": ("incremental_graph_repair",),
    "U5": ("uni_sor_cycle_safe",),
    "U6": (
        "cfmm_dual",
        "single_path_bounded",
        "incremental_graph_bounded",
        "metis_history_bounded",
    ),  # fmt: skip
}
SHARED = ("schema_version", "objective", "budget", "measurement", "worker", "search", "graph")
# The canonical resolved-profile sha256 of every arm of the arms files at b17e119 (protocol's
# pinned profile), recorded before the change (artifacts research-024/whi-1686/identity-*.json).
ARM_IDENTITY = {
    "config/latency/l08.yaml": {
        "R": "c32e10f70105a8a54a2291b66934e03e795693146c5bc616921fbf3d3065f985",
        "E1": "1526318f805ee9115ebe1e0221cfbef6407e451ae8100ad81acb38cd0d08937e",
        "E2": "11d307e40a0a7f2f31dc4c99a74e6cfdab1bd651668cae02bb589cf0f51aaade",
        "E3": "a75efc437f0e0a00f2f7b61eff2d5504484d498359644752b2bb8bbed3ef6ee3",
        "E4": "37e51cd86ff632cea25f14aa59ed5ba6c45bd96e0bf9e7413bbb9b95d164cde9",
        "S0": "13f3f014588c6a2e77cc8247a0cbbfced810205c5e80cad18c4580adba414169",
        "H1": "d9e807c926e772fca635161dd0e338d921103ba50d846a32d3e868fa4458d789",
        "H2": "7b74bba659e2fcf242a96dc112c31b6d069cb4ceecebaeb90e47d567f873f2fe",
        "H3": "ed7698cc9da390b08fb5ca13c69e944eda34d6be195a32ae911e6d20c610fa9b",
        "H4": "2fcb8bdf62d39d8c5b34e7e313d1111d5c164806f36542f6189ebc1aedff8465",
    },
    "config/research_022/latency-arms.yaml": {
        "REF": "88771d4291442bb11edbb8e8010150c16fbe830baf6097f8b29033bc23b144bc",
        "BND": "74b73824cf8529db0f32080430ca53acf39977c5ac8c8dc6d6089cef04ecf304",
    },
}


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _arm(name: str, algorithms: tuple[str, ...]) -> Arm:
    return Arm(name, algorithms, {}, ("timing", "cold"), None, None)


@pytest.fixture(scope="module")
def full() -> tuple[dict[str, Any], dict[str, Any]]:
    """The effective 19-row `all` document of full_gross and its resolved profile."""
    document, profile = derive(read_profile_document(REPO / FULL), "all", source_path=FULL,
                               source_sha256="x")  # fmt: skip
    assert "strategies" in document and document["selection"]["mode"] == "all"
    return document, profile.resolved()


def _pinned(tmp_path: Path, document: dict[str, Any]) -> str:
    path = tmp_path / "all19.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return str(path)  # absolute: `REPO_ROOT / path` is the path itself


def test_the_units_cover_the_19_identities(full: tuple[dict[str, Any], Any]) -> None:
    earlier = [a for unit, names in UNITS.items() if not unit.startswith("UP") for a in names]
    assert len(earlier) == len(set(earlier)) == 17
    assert sorted([*earlier, *UNITS["UP-cand"]]) == sorted(full[0]["algorithms"])


@pytest.mark.parametrize("unit", UNITS)
def test_every_timing_unit_resolves_like_the_full_document(
    unit: str, full: tuple[dict[str, Any], dict[str, Any]], tmp_path: Path
) -> None:
    document, whole = full
    names = tuple(a for a in document["algorithms"] if a in UNITS[unit])  # roster order
    mine = arm_profile(_arm(unit, names), _pinned(tmp_path, document)).resolved()
    assert mine["algorithms"] == list(names)
    for key in SHARED:
        assert mine[key] == whole[key], key
    for name in names:  # configuration, options with source and settings_sha256, recipe
        assert mine["algorithm_config"][name] == whole["algorithm_config"][name], name
        for section in ("algorithm_options", "strategies"):
            assert mine.get(section, {}).get(name) == whole.get(section, {}).get(name), name
    for section in ("algorithm_options", "strategies"):
        assert set(mine.get(section, {})) == set(whole.get(section, {})) & set(names)
    selection = mine["selection"]
    assert selection["mode"] == "all"
    assert selection["source_profile"] == whole["selection"]["source_profile"]
    assert selection["groups"] == {
        g: [a for a in m if a in names] for g, m in whole["selection"]["groups"].items()
    }
    if unit == "UP-cand":  # WHI-1632: the timed rows are the presets, as in the 19-row run
        for name in names:
            source = mine["algorithm_options"][name]["source"]
            assert source["kind"] == "preset"
            pin = registry.ALGORITHMS[name].options_preset or {}
            assert source == {"kind": "preset", **dict(pin)}


def test_an_arm_order_other_than_the_roster_order_resolves(
    full: tuple[dict[str, Any], Any], tmp_path: Path
) -> None:
    mine = arm_profile(_arm("rev", ("marginal_activation", "uni_sor_optimized", "direct")),
                       _pinned(tmp_path, full[0])).resolved()  # fmt: skip
    assert mine["selection"]["groups"] == {
        "base": ["direct"], "optimized": ["uni_sor_optimized"], "custom": ["marginal_activation"]
    }  # fmt: skip
    assert mine["algorithm_options"]["marginal_activation"]["source"]["kind"] == "preset"


@pytest.mark.parametrize("path", sorted(ARM_IDENTITY))
def test_existing_arms_keep_their_resolved_identity(path: str) -> None:
    arms = load_arms(REPO / path)
    protocol = load_protocol(REPO / arms.protocol_path)
    got = {name: _sha(arm_profile(arm, protocol).resolved()) for name, arm in arms.arms.items()}
    assert got == ARM_IDENTITY[path]


def test_invalid_records_are_still_refused(
    full: tuple[dict[str, Any], Any], tmp_path: Path
) -> None:
    document = json.loads(json.dumps(full[0]))
    entry = document["strategies"]["uni_sor_adaptive"]
    entry["sampling"] = {**entry["sampling"], "coarse_step": 50}  # not the registered recipe
    pinned = _pinned(tmp_path, document)
    # the invalid entry of a selected identity, and of one the arm drops (never filtered away)
    for names in (UNITS["U3"], UNITS["U1"]):
        with pytest.raises(LatencyError, match=r"strategies\.uni_sor_adaptive"):
            arm_profile(_arm("x", names), pinned)
    document = json.loads(json.dumps(full[0]))
    groups = document["selection"]["groups"]
    groups["base"], groups["custom"] = groups["base"][1:], [groups["base"][0], *groups["custom"]]
    with pytest.raises(LatencyError, match="do not belong to the custom group"):
        arm_profile(_arm("x", UNITS["U2"]), _pinned(tmp_path, document))


def test_a_tampered_preset_is_refused_and_other_options_stay_override(
    full: tuple[dict[str, Any], Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = json.loads(json.dumps(full[0]))
    options = document["algorithm_options"]["split_polish"]
    options["rounds"] += 1  # an `all` document whose options are not the preset's
    mine = arm_profile(_arm("x", UNITS["UP-cand"]), _pinned(tmp_path, document)).resolved()
    assert mine["algorithm_options"]["split_polish"]["source"] == {"kind": "override"}
    assert mine["algorithm_options"]["marginal_activation"]["source"]["kind"] == "preset"
    factory = registry.ALGORITHMS["split_polish"]
    pin = {**dict(factory.options_preset or {}), "sha256": "0" * 64}  # the file differs from it
    tampered = dataclasses.replace(factory, options_preset=MappingProxyType(pin))
    monkeypatch.setitem(registry.ALGORITHMS, "split_polish", tampered)
    with pytest.raises(LatencyError, match="differs from the pin"):
        arm_profile(_arm("x", UNITS["UP-cand"]), _pinned(tmp_path, full[0]))

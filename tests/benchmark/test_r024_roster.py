"""WHI-1632 (R024-C/1 §6): `split_polish` and `marginal_activation` join `--strategies all` with
their selected presets (WHI-1631), and every earlier document keeps its resolved identity.

- §6.1/§6.2: the two rows follow the 0.2.2 accelerations once each, with the pinned preset written
  out, and their CEC under P* is the selected candidate's (`d09fafeb…`, `633b5de0…`);
- I1/I4: the explicit 0.2.3 and 0.2.4 profiles keep `{kind: override}` and their resolved hash, for
  the real presets (b: different from every R023 nominee) and for a constructed preset equal to
  the R023 nominee of `e1b-a0.yaml` (a: the D1-F2 seam, contract §2 P8);
- I3: a changed preset file and false preset metadata are refused, an `all` document whose options
  differ from the preset resolves as an override, a document cannot claim a preset identity,
  explicit overrides stay allowed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
import yaml

import benchmark.profile as profile_module
from benchmark.profile import (
    ALL_SCOPED_PRESETS,
    ProfileError,
    load_profile,
    parse_profile,
    preset_options,
    read_profile_document,
    single_run_document,
)
from benchmark.strategies import R022_ADDITIONS, R024_ADDITIONS, derive
from routing.algorithms import registry

REPO = Path(__file__).resolve().parents[2]
_RULE = REPO / "tools" / "research_024" / "r024_rule.py"
_SPEC = importlib.util.spec_from_file_location("r024_rule", _RULE)
assert _SPEC and _SPEC.loader
rr: Any = sys.modules.setdefault("r024_rule", importlib.util.module_from_spec(_SPEC))
if not hasattr(rr, "cec"):
    _SPEC.loader.exec_module(rr)

FULL = "config/full_gross.yaml"
R023 = REPO / "config" / "research_023" / "profiles"
R024 = REPO / "config" / "research_024" / "profiles"
# WHI-1631's frozen presets and the selected candidates' CEC hashes (R024-C/1 §5.11, §6.2)
PINS = {
    "split_polish": (
        "config/split_polish/preset_v1.yaml",
        "bdaba97b702c205fe6fae0b6ce2a439f550f8b0f263713c702789358f92dc533",
        "R024-P01-split_polish",
        "sp-metis_inspired-golden-r2-t1e-5",
        "d09fafeb72b02645f934ca5bdda2a1e7ee6b92ad80ca21079c31689da07b8871",
    ),
    "marginal_activation": (
        "config/marginal_activation/preset_v1.yaml",
        "553ba71c8569eee67710587dda4a7e24e7dccee1f44ead6107944abc3db76cba",
        "R024-P02-marginal_activation",
        "ma-incremental_graph-pf-k4-top9-d1e-3",
        "633b5de0b18f0f883912ba1ec51deaf64f46e1185182159827ee0f3dca0b5bf6",
    ),
}
# contract §2 P8: the canonical resolved-profile sha256 of e1b-a0.yaml at dbc9432
E1B_A0_RESOLVED = "9d20d2aa48f3854630e31d5ce62b859a2923d2c797fb584ce770e6200cc25148"


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _all(name: str = FULL) -> tuple[dict[str, Any], Any]:
    return derive(read_profile_document(REPO / name), "all", source_path=name, source_sha256="x")


def _pin(identity: str) -> dict[str, Any]:
    path, digest, key, _, _ = PINS[identity]
    return {"path": path, "sha256": digest, "key": key, "version": 1}


def _explicit_profiles() -> list[Path]:
    """Every committed profile that lists one of the two identities (no `selection` record)."""
    out = []
    for path in sorted([*R023.glob("*.yaml"), *R024.glob("*.yaml")]):
        doc = read_profile_document(path)
        if set(doc["algorithms"]) & set(R024_ADDITIONS):
            assert "selection" not in doc, path
            out.append(path)
    return out


def test_all_appends_both_once_after_the_022_rows_with_their_pinned_presets() -> None:
    assert R024_ADDITIONS == ("split_polish", "marginal_activation")
    assert set(ALL_SCOPED_PRESETS) == set(R024_ADDITIONS)
    for name in (FULL, "config/daily_gross.yaml"):
        document, profile = _all(name)
        names = list(profile.algorithms)
        assert len(names) == len(set(names)) == 19
        assert names[-5:] == [*R022_ADDITIONS, *R024_ADDITIONS]
        assert document["selection"]["groups"]["custom"][-2:] == list(R024_ADDITIONS)
        for identity, (path, digest, _, _, _) in PINS.items():
            assert hashlib.sha256((REPO / path).read_bytes()).hexdigest() == digest
            factory = registry.ALGORITHMS[identity]
            assert dict(factory.options_preset or {}) == _pin(identity)
            written = yaml.safe_load((REPO / path).read_text())["options"]
            assert document["algorithm_options"][identity] == written == preset_options(factory)
            entry = profile.algorithm_options[identity]
            assert entry["source"] == {"kind": "preset", **dict(factory.options_preset or {})}
            assert entry["settings_sha256"] == _sha(written)


def test_the_all_rows_are_the_selected_candidates_under_pstar() -> None:
    """§6.2: CEC(all-derived profile, id) == CEC(P*-rendered winner) == the pinned hash; the
    stage-I preset profile renders the same configuration."""
    _, profile = _all()
    for identity, (_, _, _, candidate, cec_sha) in PINS.items():
        mine = rr.cec(profile.resolved(), identity)
        winner = rr.cec(load_profile(R024 / f"{candidate}.yaml").resolved(), identity)
        preset_run = rr.cec(load_profile(R024 / f"i-preset-{identity}.yaml").resolved(), identity)
        assert mine == winner == preset_run
        assert rr.canonical_sha256(mine) == cec_sha


def test_explicit_profiles_keep_override_and_differ_from_the_selected_presets() -> None:
    """I1 and I4 (b): no explicit 0.2.3/0.2.4 profile turns into a preset, although the winner
    profiles carry exactly the preset options; no R023 nominee equals a selected preset."""
    profiles = _explicit_profiles()
    assert len(profiles) > 200  # every R023 E1/E2 arm and every R024 selection profile
    for path in profiles:
        profile = load_profile(path)
        for identity in set(profile.algorithms) & set(R024_ADDITIONS):
            assert profile.algorithm_options[identity]["source"] == {"kind": "override"}, path
            if path.parent == R023:
                options = profile.algorithm_options[identity]["options"]
                assert options != preset_options(registry.ALGORITHMS[identity]), path
    assert _sha(load_profile(R023 / "e1b-a0.yaml").resolved()) == E1B_A0_RESOLVED


@pytest.fixture
def nominee_preset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """I4 (a): `split_polish` registered with a preset equal to the R023 nominee of e1b-a0."""
    options = read_profile_document(R023 / "e1b-a0.yaml")["algorithm_options"]["split_polish"]
    path = tmp_path / "nominee_preset.yaml"
    path.write_text(yaml.safe_dump(
        {"key": "TEST-e1b-a0", "version": 1, "algorithm": "split_polish", "options": options}
    ))  # fmt: skip
    pin = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
           "key": "TEST-e1b-a0", "version": 1}  # fmt: skip
    factory = dataclasses.replace(
        registry.ALGORITHMS["split_polish"], options_preset=MappingProxyType(pin)
    )
    monkeypatch.setitem(registry.ALGORITHMS, "split_polish", factory)
    return {"pin": pin, "options": options}


def test_a_preset_equal_to_an_r023_nominee_leaves_the_old_profiles_unchanged(
    nominee_preset: dict[str, Any],
) -> None:
    """The D1-F2 reproduction (contract §2 P8) no longer flips: the explicit profile stays an
    override with its dbc9432 hash, while the `all` row records the preset."""
    old = load_profile(R023 / "e1b-a0.yaml")
    assert old.algorithm_options["split_polish"]["source"] == {"kind": "override"}
    assert _sha(old.resolved()) == E1B_A0_RESOLVED
    document, profile = _all()
    assert document["algorithm_options"]["split_polish"] == nominee_preset["options"]
    entry = profile.algorithm_options["split_polish"]
    assert entry["source"] == {"kind": "preset", **nominee_preset["pin"]}
    assert entry["settings_sha256"] == old.algorithm_options["split_polish"]["settings_sha256"]
    replayed = parse_profile(yaml.safe_load(yaml.safe_dump(document)), "saved")  # literal replay
    assert _sha(replayed.resolved()) == _sha(profile.resolved())


def test_saved_all_documents_replay_and_quote_with_the_preset_provenance() -> None:
    document, profile = _all()
    replayed = parse_profile(yaml.safe_load(yaml.safe_dump(document, sort_keys=False)), "saved")
    assert replayed.resolved() == profile.resolved()
    quote = parse_profile(single_run_document(document), "quote")
    for identity in R024_ADDITIONS:
        assert quote.algorithm_options[identity] == profile.algorithm_options[identity]


def test_tampering_and_false_metadata_are_refused_and_overrides_stay_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, _ = _all()
    # an `all` document with other options: a legitimate override, never the preset identity
    edited = json.loads(json.dumps(document))
    edited["algorithm_options"]["split_polish"]["tolerance"] = 1e-4
    entry = parse_profile(edited, "edited").algorithm_options["split_polish"]
    assert entry["source"] == {"kind": "override"}
    # an explicit profile may use any valid options, the preset's included (an override there)
    explicit = load_profile(R024 / "sp-metis_inspired-golden-r2-t1e-5.yaml")
    assert explicit.algorithm_options["split_polish"]["source"] == {"kind": "override"}
    # a document cannot write its own provenance
    forged = json.loads(json.dumps(document))
    forged["algorithm_options"]["marginal_activation"]["source"] = {"kind": "preset"}
    with pytest.raises(ProfileError, match="source"):
        parse_profile(forged, "forged")
    # nor claim a selection it does not have: groups must list exactly its algorithms
    claimed = read_profile_document(R024 / "sp-metis_inspired-golden-r2-t1e-5.yaml")
    claimed["selection"] = {**document["selection"]}
    with pytest.raises(ProfileError, match="groups"):
        parse_profile(claimed, "claimed")
    # a changed preset file is refused everywhere it is read, explicit profiles included
    path = PINS["split_polish"][0]
    shutil.copytree(REPO / "config", tmp_path / "config", dirs_exist_ok=True)
    (tmp_path / path).write_text((REPO / path).read_text().replace("rounds: 2", "rounds: 4"))
    monkeypatch.setattr(profile_module, "REPO_ROOT", tmp_path)
    with pytest.raises(ProfileError, match="differs from the pin"):
        _all()
    with pytest.raises(ProfileError, match="differs from the pin"):
        load_profile(R023 / "e1b-a0.yaml")
    # false metadata: the pinned bytes, but not the pinned key / version / algorithm
    (tmp_path / path).write_text((REPO / path).read_text().replace("key: R024-P01", "key: X-P01"))
    false_pin = {**_pin("split_polish"),
                 "sha256": hashlib.sha256((tmp_path / path).read_bytes()).hexdigest()}  # fmt: skip
    factory = dataclasses.replace(
        registry.ALGORITHMS["split_polish"], options_preset=MappingProxyType(false_pin)
    )
    monkeypatch.setitem(registry.ALGORITHMS, "split_polish", factory)
    with pytest.raises(ProfileError, match="is not R024-P01-split_polish v1"):
        _all()

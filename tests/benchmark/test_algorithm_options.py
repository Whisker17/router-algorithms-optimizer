"""WHI-1548 Stage A: validated per-strategy `algorithm_options` (contract R021-C/1 §9.1-§9.3).

Exercised with the test-only factories of `option_solvers` (inserted into the registry by
`monkeypatch`; they never join the roster) and the registered strategies:

- invalid, unknown, reserved, boolean-as-integer, non-finite and out-of-range options are
  refused by profile loading AND by the factory's public `prepare` (same strict checks),
  and by `main.py run` / `main.py quote` before any worker starts or anything is written;
- each algorithm receives only its own read-only options; siblings, `params` and the shared
  `search`/`graph`/budget values are untouched;
- identity is derived from content: the preset label only for the pinned preset's exact
  options, an override gets its own `settings_sha256`, a changed preset file is refused and a
  tampered effective document can never claim the preset;
- batch and quote persist the resolved options, transport them into the worker and replay
  them literally with `--strategies profile` (one solve per quote);
- without the section every legacy resolved profile, derived effective document and their
  hashes equal the pins computed at the pre-WHI-1548 base (under `all`, after removing the
  implemented 0.2.1 identities it now appends with their pinned presets, WHI-1554), the
  `--strategies all` roster is the nine plus exactly those identities, and the SOR recipe
  checks are not weakened.
"""

from __future__ import annotations

import glob
import hashlib
import json
import math
import multiprocessing
import shlex
import signal
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from types import FrameType, MappingProxyType
from typing import Any

import option_solvers  # sibling module: importable by spawned workers via sys.path
import pytest
import yaml

import benchmark.profile as profile_module
import benchmark.strategies as strategies_module
import main
import routing.algorithms.registry as registry
from benchmark.profile import (
    ProfileError,
    load_profile,
    parse_profile,
    preset_options,
    read_profile_document,
)
from benchmark.results import load_case_records, load_manifest
from benchmark.runner import compare_runs
from benchmark.strategies import METIS, MODES, R021_ADDITIONS, derive
from routing.algorithms import direct
from routing.algorithms.base import (
    RESERVED_OPTION_KEYS,
    AlgorithmConfig,
    OptionsError,
    option_int,
    option_number,
    settings_sha256,
    validated_options,
)
from routing.algorithms.registry import ALGORITHMS, BASE_STRATEGIES, OPTIMIZED_STRATEGIES
from snapshot.bundle import load_bundle

REPO = Path(__file__).resolve().parents[2]
MIXED = REPO / "tests" / "fixtures" / "routing" / "mantle_mixed"
CORPUS = REPO / "tests" / "fixtures" / "corpus" / "bundle"
PINS = REPO / "tests" / "fixtures" / "r021_options" / "legacy_identity_pins.json"
A, B = option_solvers.A, option_solvers.B
PRESET = {"width": 3, "ratio": 0.5, "label": "bounded"}  # the fixture preset file's values
NON_STRING_KEY: dict[Any, Any] = {1: 2, **PRESET}
NINE = [*BASE_STRATEGIES, *OPTIMIZED_STRATEGIES, METIS]
# The implemented 0.2.1 identities (R021-C/1 §2), in contract order, and the profiles they added.
# WHI-1550, WHI-1552, WHI-1554, WHI-1556, WHI-1558
IMPLEMENTED = (
    "metis_history",
    "direct_split_certified",
    "incremental_graph_repair",
    "uni_sor_cycle_safe",
    "cfmm_dual",
)
NEW_PROFILES = (
    {
        f"config/incremental_graph_repair/{name}.yaml"
        for name in ("repair_on", "repair_off", "stress")
    }
    | {f"config/metis_history/{name}.yaml" for name in ("history_on", "history_off")}
    | {f"config/direct_split_certified/{name}.yaml" for name in ("grid", "raw_stress")}
    | {"config/uni_sor_cycle_safe/matched.yaml"}
    | {"config/cfmm_dual/cpmm.yaml"}
)
TEST_ALARM_SECONDS = 240


@pytest.fixture(autouse=True)
def _bounded_test() -> Iterator[None]:
    def _alarm(signum: int, frame: FrameType | None) -> None:
        raise TimeoutError(f"test exceeded {TEST_ALARM_SECONDS}s")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(TEST_ALARM_SECONDS)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        for child in multiprocessing.active_children():
            child.kill()


@pytest.fixture
def fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    for factory in option_solvers.ALL:
        monkeypatch.setitem(registry.ALGORITHMS, factory.name, factory)


@pytest.fixture
def added_a(fixtures: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """`opt_fixture_a` as if it were an implemented identity `--strategies all` appends."""
    monkeypatch.setattr(strategies_module, "R021_ADDITIONS", (A,))


def _main_argv(command: str) -> list[str]:
    """A printed `uv run python main.py ...` command as `main.main` arguments."""
    argv = shlex.split(command)
    return argv[argv.index("main.py") + 1 :]


def _sha(obj: Any) -> str:
    """Independent restatement of the R021-C/1 §3.1 hash rule."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def _doc(algorithms: list[str], options: dict[str, Any] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": 2,
        "algorithms": algorithms,
        "objective": {"mode": "gross_only"},
        "search": {"max_hops": 2, "max_splits": 2, "percent_step": 5},
        "graph": {"chunks": 10},
        "budget": {"time_limit_seconds": 60, "max_quotes": 50000, "max_candidates": None},
        "measurement": {
            "warmup": 0,
            "repeats": 1,
            "seed": 7,
            "order": "fixed",
            "memory_pass": False,
        },  # fmt: skip
        "worker": {"start_method": "spawn", "scope": "algorithm", "prepare_time_limit_seconds": 60},
    }
    if options is not None:
        doc["algorithm_options"] = options
    return doc


def _write(tmp_path: Path, doc: dict[str, Any], name: str = "source.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _derive(doc: dict[str, Any], mode: str) -> tuple[dict[str, Any], Any]:
    return derive(doc, mode, source_path="src.yaml", source_sha256="s" * 64)


# Invalid option values of `opt_fixture_a` (every other key valid) -> expected message.
INVALID_A: list[tuple[dict[str, Any], str]] = [
    ({**PRESET, "width": True}, "width: expected an integer"),  # bool is not an int
    ({**PRESET, "width": 1.0}, "width: expected an integer"),
    ({**PRESET, "width": "3"}, "width: expected an integer"),
    ({**PRESET, "width": 0}, r"width: expected an integer in \[1, 8\]"),
    ({**PRESET, "width": 9}, r"width: expected an integer in \[1, 8\]"),
    ({**PRESET, "ratio": math.nan}, "finite number"),
    ({**PRESET, "ratio": math.inf}, "finite number"),
    ({**PRESET, "ratio": -math.inf}, "finite number"),
    ({**PRESET, "ratio": False}, "ratio: expected a finite number"),
    ({**PRESET, "ratio": 1.5}, r"ratio: expected a finite number in \[0.0, 1.0\]"),
    ({**PRESET, "ratio": 10**400}, "ratio: expected a finite number"),  # overflows a float
    ({**PRESET, "ratio": -(10**400)}, "ratio: expected a finite number"),
    (
        {**PRESET, "width": 10**5000},
        r"width: integer too large to serialize, got <int of \d+ bits>",
    ),
    ({**PRESET, "label": "other"}, "label: expected one of"),
    ({**PRESET, "extra": 1}, r"unknown key\(s\) \['extra'\]"),
    ({"width": 3, "ratio": 0.5}, r"missing required key\(s\) \['label'\]"),
    ({**PRESET, "nested": {"x": math.nan}}, "finite number"),
    ({**PRESET, "source": {"kind": "preset"}}, r"unknown key\(s\) \['source'\]"),
    ({**PRESET, "settings_sha256": "0" * 64}, r"unknown key\(s\) \['settings_sha256'\]"),
    *[
        ({**PRESET, key: 1}, rf"\['{key}'\] name shared profile settings")
        for key in sorted(RESERVED_OPTION_KEYS)
    ],  # fmt: skip
]


# ------------------------------------------------------------------ validation: both entrypoints


@pytest.mark.parametrize(
    ("options", "match"), INVALID_A, ids=[f"case{i}" for i in range(len(INVALID_A))]
)
def test_invalid_options_are_refused_by_the_profile_and_by_public_prepare(
    fixtures: None, options: dict[str, Any], match: str
) -> None:
    with pytest.raises(ProfileError, match=match):
        parse_profile(_doc(["direct", A], {A: options}), "<test>")
    # the factory's own public prepare applies exactly the same strict checks
    with pytest.raises(OptionsError, match=match):
        option_solvers.prepare_a(None, AlgorithmConfig(A, options=options))  # type: ignore[arg-type]


def test_reserved_keys_list_is_the_contract_list() -> None:
    assert RESERVED_OPTION_KEYS == {
        "max_hops", "max_splits", "percent_step", "chunks", "label_hops", "label_pruning",
        "time_limit_seconds", "max_quotes", "max_candidates", "shortlist", "sampling",
        "controls", "recipe", "objective", "seed",
    }  # fmt: skip


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"zzz_unknown": {}}, r"zzz_unknown: unknown algorithm"),
        ({B: {}}, rf"{B}: declared but '{B}' is not in algorithms"),  # registered, unselected
        ({"direct": {}}, r"'direct' accepts no algorithm_options \(no validator\)"),
        ({"uni_sor_port": {"max_hops": 3}}, "accepts no algorithm_options"),
        ({}, "expected a non-empty mapping"),
        ({A: [1]}, "expected a mapping"),
        ({A: NON_STRING_KEY}, "option keys must be strings"),
    ],
)
def test_section_level_errors(fixtures: None, options: Any, match: str) -> None:
    with pytest.raises(ProfileError, match=match):
        parse_profile(_doc(["direct", "uni_sor_port", A], options), "<test>")


def test_undeclared_required_options_are_not_defaulted(fixtures: None) -> None:
    with pytest.raises(ProfileError, match=rf"'{A}' requires algorithm_options.{A}"):
        parse_profile(_doc(["direct", A]), "<test>")
    # an all-optional validator accepts an undeclared entry as exactly `{}`
    profile = parse_profile(_doc(["direct", B]), "<test>")
    assert profile.algorithm_options == {
        B: {"options": {}, "source": {"kind": "override"}, "settings_sha256": _sha({})}
    }


def test_factories_without_a_validator_refuse_direct_options() -> None:
    assert validated_options(direct.FACTORY, {}) == {}
    with pytest.raises(OptionsError, match="accepts no algorithm_options"):
        validated_options(direct.FACTORY, {"width": 1})


def test_every_options_factory_refuses_reserved_and_unknown_keys_in_prepare(
    fixtures: None,
) -> None:
    """Applies to every registered options factory (the implemented 0.2.1 identities; none of
    the nine) and the fixtures."""
    factories = [f for f in ALGORITHMS.values() if f.options_validator is not None]
    assert {f.name for f in factories} == {A, B, *IMPLEMENTED}
    for factory in factories:
        assert factory.prepare is not None
        for bad in ({"max_hops": 1}, {"__unknown__": 1}):
            with pytest.raises(OptionsError):
                factory.prepare(None, AlgorithmConfig(factory.name, options=bad))  # type: ignore[arg-type]


def test_valid_options_are_normalized_by_both_entrypoints(fixtures: None) -> None:
    declared = {"width": 8, "ratio": 1, "label": "stress"}  # an int ratio normalizes to 1.0
    expected = {"width": 8, "ratio": 1.0, "label": "stress"}
    profile = parse_profile(_doc(["direct", A], {A: declared}), "<test>")
    assert profile.algorithm_options[A]["options"] == expected
    prepared = option_solvers.prepare_a(None, AlgorithmConfig(A, options=declared))  # type: ignore[arg-type]
    assert dict(prepared) == expected


# ------------------------------------------------------------------ isolation


def test_siblings_never_see_each_others_options_or_the_shared_settings(fixtures: None) -> None:
    doc = _doc(["direct", A, B, "path_split"], {A: {**PRESET, "width": 5}, B: {"width": 7}})
    profile = parse_profile(doc, "<test>")
    baseline = parse_profile(_doc(["direct", A, B, "path_split"],
                                  {A: {**PRESET, "width": 5}}), "<test>")  # fmt: skip
    config_a = profile.algorithm_config(ALGORITHMS[A])
    config_b = profile.algorithm_config(ALGORITHMS[B])
    assert dict(config_a.options) == {**PRESET, "width": 5}
    assert dict(config_b.options) == {"width": 7}  # same key name, its own value
    for name in ("direct", "path_split"):
        assert dict(profile.algorithm_config(ALGORITHMS[name]).options) == {}
        # B's options changed nothing for anyone else
        assert profile.algorithm_config(ALGORITHMS[name]) == baseline.algorithm_config(
            ALGORITHMS[name]
        )
    assert config_a == baseline.algorithm_config(ALGORITHMS[A])
    # options never leak into params, and the shared settings are the profile's
    assert "width" not in config_a.params and "width" not in config_b.params
    assert profile.search == baseline.search and profile.graph == baseline.graph
    assert profile.budget == baseline.budget
    # read-only, and a fresh copy per config: nothing can be mutated through a handoff
    with pytest.raises(TypeError):
        config_a.options["width"] = 1  # type: ignore[index]
    assert profile.algorithm_options[A]["options"]["width"] == 5
    assert profile.algorithm_config(ALGORITHMS[A]).options is not config_a.options


# ------------------------------------------------------------------ identity


def test_preset_versus_override_identity(fixtures: None) -> None:
    pin = dict(option_solvers.PRESET_A)
    preset = parse_profile(_doc([A], {A: dict(PRESET)}), "<test>").algorithm_options[A]
    assert preset == {"options": PRESET, "source": {"kind": "preset", **pin},
                      "settings_sha256": _sha(PRESET)}  # fmt: skip
    changed = {**PRESET, "width": 4}
    override = parse_profile(_doc([A], {A: changed}), "<test>").algorithm_options[A]
    assert override == {"options": changed, "source": {"kind": "override"},
                        "settings_sha256": _sha(changed)}  # fmt: skip
    assert override["settings_sha256"] != preset["settings_sha256"]
    # the resolved record carries the entries; the hash is the canonical-JSON rule
    resolved = parse_profile(_doc([A], {A: changed}), "<test>").resolved()
    assert resolved["algorithm_options"][A] == override


def test_changed_or_mismatching_preset_files_are_refused(
    added_a: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative = option_solvers.PRESET_A["path"]
    source, copy = REPO / relative, tmp_path / relative
    copy.parent.mkdir(parents=True)
    monkeypatch.setattr(profile_module, "REPO_ROOT", tmp_path)
    copy.write_bytes(source.read_bytes().replace(b"width: 3", b"width: 4"))
    for doc in (_doc([A], {A: PRESET}), _doc(["direct"])):
        with pytest.raises(ProfileError, match="differs from the pin"):
            derive(doc, "all", source_path="s.yaml", source_sha256="s" * 64)
    with pytest.raises(ProfileError, match="differs from the pin"):  # a declared override too
        parse_profile(_doc([A], {A: {**PRESET, "width": 4}}), "<test>")
    copy.unlink()
    with pytest.raises(ProfileError, match="unreadable"):
        parse_profile(_doc([A], {A: PRESET}), "<test>")


def test_tampered_effective_document_never_claims_the_preset(added_a: None) -> None:
    document, profile = _derive(_doc(["direct"]), "all")
    assert document["algorithm_options"] == {A: PRESET}  # the preset, written out
    assert profile.algorithm_options[A]["source"]["kind"] == "preset"
    tampered = json.loads(json.dumps(document))
    tampered["algorithm_options"][A]["width"] = 6
    entry = parse_profile(tampered, "<replay>").algorithm_options[A]
    assert entry["source"] == {"kind": "override"}
    assert entry["settings_sha256"] == _sha({**PRESET, "width": 6})
    # identity fields cannot be smuggled into a document
    for key, value in (("source", {"kind": "preset"}), ("settings_sha256", _sha(PRESET))):
        forged = json.loads(json.dumps(tampered))
        forged["algorithm_options"][A][key] = value
        with pytest.raises(ProfileError, match="unknown key"):
            parse_profile(forged, "<replay>")


# ------------------------------------------------------------------ derivation / selection


def test_derivation_keeps_declared_fills_presets_and_drops_unselected(added_a: None) -> None:
    declared = {A: {**PRESET, "width": 2}, B: {"width": 9}}
    document, profile = _derive(_doc(["direct", A, B], declared), "all")
    # a listed identity keeps its place (not appended twice)
    assert document["algorithms"] == ["direct", A, B, *OPTIMIZED_STRATEGIES, METIS]
    assert document["algorithm_options"] == declared  # a declared entry wins over the preset
    assert profile.algorithm_options[A]["source"] == {"kind": "override"}
    assert _derive(document, "all")[0] == document  # idempotent re-derivation
    # not listed: `all` appends A after metis_inspired with its pinned preset
    document, profile = _derive(_doc(["direct", B], {B: {"width": 9}}), "all")
    assert document["algorithms"] == ["direct", B, *OPTIMIZED_STRATEGIES, METIS, A]
    assert document["algorithm_options"] == {B: {"width": 9}, A: PRESET}
    assert profile.algorithm_options[A]["source"]["kind"] == "preset"
    assert _derive(document, "all")[0] == document
    # a listed options identity is a required setting of the source, never defaulted
    with pytest.raises(ProfileError, match=f"requires algorithm_options.{A}"):
        _derive(_doc(["direct", A]), "all")
    # base / optimized drop the unselected custom algorithms and their options entirely
    for mode in ("base", "optimized"):
        document, profile = _derive(_doc(["direct", A, B], declared), mode)
        assert "algorithm_options" not in document and profile.algorithm_options == {}
        assert "algorithm_options" not in profile.resolved()
    # profile: literal copy
    document, profile = _derive(_doc(["direct", A, B], declared), "profile")
    assert document["algorithm_options"] == declared


def test_options_do_not_weaken_sor_recipe_pins(added_a: None) -> None:
    document, _ = _derive(_doc(["direct", A], {A: PRESET}), "all")
    tampered = json.loads(json.dumps(document))
    tampered["strategies"]["uni_sor_adaptive"]["sampling"]["refine_radius"] += 1
    with pytest.raises(ProfileError, match="differs from recipe"):
        parse_profile(tampered, "<test>")
    for name in OPTIMIZED_STRATEGIES:  # options are no side door into a named recipe
        side_door = json.loads(json.dumps(document))
        side_door["algorithm_options"][name] = {"refine_radius": 1}
        with pytest.raises(ProfileError, match="accepts no algorithm_options"):
            parse_profile(side_door, "<test>")


# ------------------------------------------------------------------ legacy identity


def _legacy_projection(effective: dict[str, Any]) -> dict[str, Any]:
    """An `all` document without the implemented 0.2.1 identities `all` now appends (WHI-1550
    `metis_history`, WHI-1552 `direct_split_certified`, WHI-1554 `incremental_graph_repair`,
    WHI-1556 `uni_sor_cycle_safe`, WHI-1558 `cfmm_dual`, after `metis_inspired`, with exactly
    their pinned presets):
    the document the pre-WHI-1548 base derived. Anything else that changed stays visible."""
    doc: dict[str, Any] = json.loads(json.dumps(effective))
    added = list(IMPLEMENTED)
    assert doc["algorithms"][-len(added) :] == added  # appended once, last, in contract order
    doc["algorithms"] = doc["algorithms"][: -len(added)]
    assert doc["selection"]["groups"]["custom"][-len(added) :] == added
    doc["selection"]["groups"]["custom"] = doc["selection"]["groups"]["custom"][: -len(added)]
    assert doc.pop("algorithm_options") == {
        name: preset_options(ALGORITHMS[name]) for name in added
    }
    return doc


def test_legacy_profiles_derivations_and_hashes_match_the_pre_whi_1548_pins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every parseable legacy `config/**/*.yaml`, every `--strategies` mode: resolved profile,
    effective document bytes and their literal replay hash exactly as at base 42cbf59. Under
    `all` the only change is the appended implemented 0.2.1 identity with its pinned preset
    (WHI-1554): its legacy projection keeps the pinned bytes and hashes. The profiles WHI-1554
    added (`NEW_PROFILES`) are not legacy and carry options by design."""
    monkeypatch.chdir(REPO)  # empirical-cost profiles name their artifact repo-relatively
    pins = json.loads(PINS.read_text())["pins"]
    observed: dict[str, str] = {}
    new_seen: set[str] = set()
    for path in sorted(glob.glob("config/**/*.yaml", recursive=True)):
        if path in NEW_PROFILES:
            new_seen.add(path)
            continue
        try:
            doc = read_profile_document(path)
            profile = parse_profile(doc, path)
        except ProfileError:
            continue
        observed[f"{path}|resolved"] = _sha(profile.resolved())
        for mode in MODES:
            try:
                effective, derived = derive(doc, mode, source_path=path, source_sha256="0" * 64)
            except ProfileError:
                continue
            text = yaml.safe_dump(effective, sort_keys=False)
            # literal replay of the saved document: the same resolved identity
            replayed = parse_profile(yaml.safe_load(text), path)
            assert _sha(replayed.resolved()) == _sha(derived.resolved()), (path, mode)
            if mode == "all":
                effective = _legacy_projection(effective)
                derived = parse_profile(effective, path)
                text = yaml.safe_dump(effective, sort_keys=False)
            observed[f"{path}|{mode}|document"] = hashlib.sha256(text.encode()).hexdigest()
            observed[f"{path}|{mode}|resolved"] = _sha(derived.resolved())
            assert "algorithm_options" not in effective
            assert "algorithm_options" not in derived.resolved()
    assert observed == pins
    assert len(pins) == 289
    assert new_seen == NEW_PROFILES


def test_the_all_roster_is_the_nine_plus_the_implemented_0_2_1_identities() -> None:
    """The contract-stage snapshot "nine, no new ID" (WHI-1548) with the roster as implemented
    so far: `--strategies all` appends exactly `IMPLEMENTED` after the nine (R021-C/1 §2), each
    with its pinned preset; every other factory still accepts no options."""
    document, profile = _derive(read_profile_document(REPO / "config" / "daily_gross.yaml"), "all")
    assert R021_ADDITIONS == IMPLEMENTED
    assert list(profile.algorithms) == [*NINE, *IMPLEMENTED] == document["algorithms"]
    assert len(ALGORITHMS) == 15  # the nine + profile-selected uni_sor_fast + IMPLEMENTED
    with_options = {n for n, f in ALGORITHMS.items() if f.options_validator is not None}
    assert with_options == set(IMPLEMENTED)
    assert all(ALGORITHMS[n].options_preset is not None for n in IMPLEMENTED)
    assert all(f.options_preset is None for n, f in ALGORITHMS.items() if n not in IMPLEMENTED)
    for name in NINE:
        assert dict(profile.algorithm_config(ALGORITHMS[name]).options) == {}
    for name in IMPLEMENTED:
        assert profile.algorithm_options[name]["source"]["kind"] == "preset"
        assert dict(profile.algorithm_config(ALGORITHMS[name]).options) == preset_options(
            ALGORITHMS[name]
        )


# ------------------------------------------------------------------ CLI: refused before any write


@pytest.mark.parametrize("strategies", ["profile", "all"])
def test_invalid_options_fail_run_and_quote_before_any_write_or_worker(
    fixtures: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    strategies: str,
) -> None:
    log = tmp_path / "solves.log"
    monkeypatch.setenv("FAKE_SOLVE_LOG", str(log))
    cases: list[tuple[dict[str, Any], str]] = [
        ({A: {**PRESET, "width": True}}, "width: expected an integer"),
        ({A: {**PRESET, "max_hops": 3}}, "name shared profile settings"),
        ({A: PRESET, "direct": {"x": 1}}, "accepts no algorithm_options"),
        ({A: PRESET, B: {"width": 1}}, "is not in algorithms"),
    ]
    for index, (options, match) in enumerate(cases):
        source = _write(tmp_path, _doc(["direct", A], options), f"bad{index}.yaml")
        results, quotes = tmp_path / f"results{index}", tmp_path / f"quotes{index}"
        run = ["run", "--bundle", str(MIXED), "--profile", str(source),
               "--results-dir", str(results), "--strategies", strategies]  # fmt: skip
        assert main.main(run) == 1
        quote = ["quote", "--bundle", str(CORPUS), "--profile", str(source), "--token-in",
                 "USDC", "--token-out", "USDT0", "--amount", "1500.25", "--quotes-dir",
                 str(quotes), "--strategies", strategies]  # fmt: skip
        assert main.main(quote) == 1
        err = capsys.readouterr().err
        assert err.count(match.split(":")[0]) >= 2, err
        assert not results.exists() and not quotes.exists()
    assert not log.exists()  # no solve ever ran
    assert not multiprocessing.active_children()


# ------------------------------------------------------------------ CLI: persist, transport, replay


def _records(run_dir: Path) -> dict[str, dict[str, Any]]:
    return {r["algorithm"]: r for r in load_case_records(run_dir)}


def test_batch_run_persists_transports_and_replays_exact_options(
    added_a: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write(tmp_path, _doc(["direct", B], {B: {"width": 9}}))
    before = source.read_bytes()
    results = tmp_path / "results"
    argv = ["run", "--bundle", str(MIXED), "--profile", str(source), "--results-dir",
            str(results), "--strategies", "all"]  # fmt: skip
    assert main.main(argv) == 0
    out = capsys.readouterr().out
    assert source.read_bytes() == before
    (run_dir,) = results.iterdir()
    manifest = load_manifest(run_dir)
    saved = read_profile_document(run_dir / "profile.yaml")
    assert saved["algorithm_options"] == {B: {"width": 9}, A: PRESET}
    entries = manifest.resolved_profile["algorithm_options"]
    assert entries[A]["source"]["kind"] == "preset"
    assert entries[B] == {"options": {"width": 9}, "source": {"kind": "override"},
                          "settings_sha256": _sha({"width": 9})}  # fmt: skip
    records = [r for r in load_case_records(run_dir) if r["algorithm"] in (A, B)]
    assert records
    for record in records:  # each worker received exactly its own options
        expected = PRESET if record["algorithm"] == A else {"width": 9}
        assert record["search"]["options"] == expected
        assert record["search"]["options_sha256"] == entries[record["algorithm"]]["settings_sha256"]
    for record in load_case_records(run_dir):
        if record["algorithm"] not in (A, B):
            assert "options" not in (record["search"] or {})

    # the printed replay command reads the saved effective profile literally
    assert list(manifest.algorithms) == ["direct", B, *OPTIMIZED_STRATEGIES, METIS, A]
    replay = next(line for line in out.splitlines() if line.startswith("replay: "))
    command = _main_argv(replay.removeprefix("replay: "))
    assert command[-2:] == ["--strategies", "profile"]
    replay_results = tmp_path / "replay"
    command[command.index("--results-dir") + 1] = str(replay_results)
    assert main.main(command) == 0
    capsys.readouterr()
    (replayed,) = replay_results.iterdir()
    again = load_manifest(replayed)
    assert again.resolved_profile == manifest.resolved_profile
    assert list(again.algorithms) == list(manifest.algorithms)
    assert compare_runs(run_dir, replayed) == []
    assert _records(replayed)[A]["search"]["options"] == PRESET


def test_quote_persists_options_one_solve_each_and_replays_them(
    fixtures: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "solves.log"
    monkeypatch.setenv("FAKE_SOLVE_LOG", str(log))
    override = {**PRESET, "width": 6, "label": "stress"}
    doc = _doc(["direct", A, B], {A: override})
    doc["measurement"] = {"warmup": 2, "repeats": 3, "seed": 1, "order": "fixed",
                          "memory_pass": True}  # fmt: skip
    source = _write(tmp_path, doc)
    quotes = tmp_path / "quotes"
    argv = ["quote", "--bundle", str(CORPUS), "--profile", str(source), "--token-in", "USDC",
            "--token-out", "USDT0", "--amount", "1500.25", "--quotes-dir", str(quotes),
            "--strategies", "profile", "--details"]  # fmt: skip
    assert main.main(argv) == 0
    capsys.readouterr()
    assert Counter(log.read_text().split()) == {A: 1, B: 1}  # warmup/repeats/memory ignored
    (quote_dir,) = quotes.iterdir()
    record = json.loads((quote_dir / "quote.json").read_text())
    saved = read_profile_document(quote_dir / "profile.yaml")
    assert saved["algorithm_options"] == {A: override}
    (run_dir,) = (quote_dir / "runs").iterdir()
    manifest = load_manifest(run_dir)
    entries = manifest.resolved_profile["algorithm_options"]
    assert entries[A] == {"options": override, "source": {"kind": "override"},
                          "settings_sha256": _sha(override)}  # fmt: skip
    assert entries[B]["options"] == {}
    records = _records(run_dir)
    assert records[A]["search"]["options"] == override
    assert records[B]["search"]["options"] == {}
    for name in (A, B):
        assert records[name]["measurement"]["attempts_completed"] == 1

    command = _main_argv(record["replay_command"])
    assert command[-2:] == ["--strategies", "profile"]
    replay_results = tmp_path / "replay"
    command[command.index("--results-dir") + 1] = str(replay_results)
    assert main.main(command) == 0
    capsys.readouterr()
    (replayed,) = replay_results.iterdir()
    assert load_manifest(replayed).resolved_profile == manifest.resolved_profile
    assert compare_runs(run_dir, replayed) == []
    assert _records(replayed)[A]["search"]["options"] == override
    assert load_profile(quote_dir / "profile.yaml").algorithm_options == entries


# ------------------------------------------------------------------ parent Stage A gate regressions


def test_settings_hash_accepts_the_public_read_only_options_type(fixtures: None) -> None:
    """`AlgorithmConfig.options` is a `MappingProxyType`: it hashes like the plain dict."""
    assert settings_sha256(MappingProxyType({"limit": 3})) == _sha({"limit": 3})
    nested = MappingProxyType({"a": MappingProxyType({"b": [1, 2]})})
    assert settings_sha256(nested) == _sha({"a": {"b": [1, 2]}})
    profile = parse_profile(_doc([A], {A: {**PRESET, "width": 4}}), "<test>")
    public = profile.algorithm_config(ALGORITHMS[A]).options
    assert isinstance(public, MappingProxyType)
    assert settings_sha256(public) == profile.algorithm_options[A]["settings_sha256"]
    with pytest.raises(OptionsError, match="integer too large to serialize"):
        settings_sha256({"n": 10**5000})
    with pytest.raises(OptionsError, match="expected a JSON value"):
        settings_sha256({"n": {1, 2}})


@pytest.mark.parametrize("value", [10**400, -(10**400), 10**5000], ids=["e400", "-e400", "e5000"])
def test_non_representable_numbers_are_contextual_option_errors(value: int) -> None:
    with pytest.raises(OptionsError, match="tolerance: expected a finite number >= 0"):
        option_number(value, "tolerance", 0)
    assert option_number(10**20, "tolerance", 0) == 1e20  # large but representable
    with pytest.raises(OptionsError, match=r"n: expected an integer in \[0, 5\]"):
        option_int(value, "n", 0, 5)


def test_existing_prepares_refuse_explicit_options_and_keep_absent_parity() -> None:
    """Every registered factory without a validator: its public `prepare` refuses any
    explicit (unsupported, reserved or unknown) option instead of ignoring it; with options
    absent or empty it prepares exactly as before."""
    bundle = load_bundle(MIXED)
    _, profile = _derive(read_profile_document(REPO / "config" / "daily_gross.yaml"), "all")
    guarded = [f for f in ALGORITHMS.values() if f.options_validator is None]
    assert {f.name for f in guarded} == set(ALGORITHMS) - set(IMPLEMENTED)
    for factory in guarded:
        if factory.prepare is None:  # `direct`: no public prepare, nothing to bypass
            assert factory.name == "direct"
            continue
        params = (
            profile.algorithm_config(factory).params if factory.name in profile.algorithms else {}
        )
        for bad in ({"max_quotes": 1}, {"__unknown__": 1}, MappingProxyType({"width": 2})):
            config = AlgorithmConfig(factory.name, params=params, options=bad)
            with pytest.raises(
                OptionsError, match=f"'{factory.name}' accepts no algorithm_options"
            ):
                factory.prepare(bundle, config)
        if factory.name in profile.algorithms:  # absent / empty options: unchanged
            factory.prepare(bundle, AlgorithmConfig(factory.name, params=params))
            factory.prepare(bundle, profile.algorithm_config(factory))
    # the parent's exact reproduction
    with pytest.raises(OptionsError, match="accepts no algorithm_options"):
        ALGORITHMS["single_path"].prepare(  # type: ignore[misc]
            bundle,
            AlgorithmConfig("single_path", params={"max_hops": 3}, options={"max_quotes": 1}),
        )

"""Unit tests for `benchmark.profile`: schema validation, unknown-key rejection,
objective-mode dispatch, and the explicit (default-free) measured-run sections of
schema 2 (WHI-1437).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from benchmark.profile import ProfileError, load_profile, parse_profile
from routing.algorithms.base import Budget

BASE: dict[str, Any] = {
    "schema_version": 2,
    "algorithms": ["direct"],
    "objective": {"mode": "gross_only"},
    "budget": {"time_limit_seconds": 2.5, "max_quotes": 100, "max_candidates": None},
    "measurement": {
        "warmup": 1,
        "repeats": 3,
        "seed": 11,
        "order": "fixed",
        "memory_pass": False,
    },
    "worker": {"start_method": "spawn", "scope": "algorithm", "prepare_time_limit_seconds": 30},
}


def _raw(**changes: Any) -> dict[str, Any]:
    raw = copy.deepcopy(BASE)
    for dotted, value in changes.items():
        *path, leaf = dotted.split("__")
        target = raw
        for key in path:
            target = target[key]
        if value is _DELETE:
            del target[leaf]
        else:
            target[leaf] = value
    return raw


_DELETE = object()


def test_load_profile_gross_only(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(BASE))
    profile = load_profile(path)
    assert profile.algorithms == ("direct",)
    assert profile.objective.mode == "gross_only"
    assert profile.budget == Budget(time_limit_seconds=2.5, max_quotes=100, max_candidates=None)
    assert profile.measurement.repeats == 3 and profile.measurement.warmup == 1
    assert profile.worker.prepare_time_limit_seconds == 30.0
    assert profile.resolved()["worker"]["scope"] == "algorithm"


def test_load_profile_synthetic_fixed_cost() -> None:
    profile = parse_profile(
        _raw(objective={"mode": "synthetic_fixed_cost", "fixed_cost": 42}), "<test>"
    )
    assert profile.objective.mode == "synthetic_fixed_cost"
    assert profile.objective.fixed_cost == 42


def test_checked_in_smoke_profile_is_valid() -> None:
    profile = load_profile("config/smoke.yaml")
    assert profile.schema_version == 2
    assert profile.budget.time_limit_seconds is not None


def test_schema_1_is_rejected_with_a_pointer_to_the_new_sections() -> None:
    raw = {"schema_version": 1, "algorithms": ["direct"], "objective": {"mode": "gross_only"}}
    with pytest.raises(ProfileError, match="predates measured runs"):
        parse_profile(raw, "<test>")


@pytest.mark.parametrize("section", ["budget", "measurement", "worker"])
def test_measured_sections_are_required(section: str) -> None:
    with pytest.raises(ProfileError, match="missing required key"):
        parse_profile(_raw(**{section: _DELETE}), "<test>")


@pytest.mark.parametrize(
    "key",
    [
        "budget__time_limit_seconds",
        "budget__max_quotes",
        "budget__max_candidates",
        "measurement__warmup",
        "measurement__repeats",
        "measurement__seed",
        "measurement__order",
        "measurement__memory_pass",
        "worker__start_method",
        "worker__scope",
        "worker__prepare_time_limit_seconds",
    ],
)
def test_every_measured_value_must_be_declared(key: str) -> None:
    # No invented universal defaults (docs/DESIGN.md §2.12): omitting any key fails,
    # even the ones whose value may legitimately be null.
    with pytest.raises(ProfileError, match="missing required key"):
        parse_profile(_raw(**{key: _DELETE}), "<test>")


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("budget__time_limit_seconds", 0, "positive number"),
        ("budget__time_limit_seconds", None, "positive number"),
        ("budget__time_limit_seconds", True, "positive number"),
        ("budget__max_quotes", 0, "integer >= 1"),
        ("budget__max_candidates", 1.5, "integer >= 1"),
        ("measurement__warmup", -1, "integer >= 0"),
        ("measurement__repeats", 0, "integer >= 1"),
        ("measurement__seed", "x", "integer >= 0"),
        ("measurement__order", "random", "expected one of"),
        ("measurement__memory_pass", "yes", "expected a bool"),
        ("worker__start_method", "fork", "expected one of"),
        ("worker__scope", "global", "expected one of"),
        ("worker__prepare_time_limit_seconds", -3, "positive number"),
        ("budget__extra", 1, "unknown key"),
        ("worker__extra", 1, "unknown key"),
    ],
)
def test_invalid_measured_values_rejected(key: str, value: Any, match: str) -> None:
    with pytest.raises(ProfileError, match=match):
        parse_profile(_raw(**{key: value}), "<test>")


def test_explicit_null_count_limits_are_allowed() -> None:
    profile = parse_profile(_raw(budget__max_quotes=None), "<test>")
    assert profile.budget.max_quotes is None


def test_unknown_top_level_key_rejected() -> None:
    with pytest.raises(ProfileError, match="unknown key"):
        parse_profile(_raw(unexpected=True), "<test>")


def test_unsupported_schema_version_rejected() -> None:
    with pytest.raises(ProfileError, match="unsupported schema"):
        parse_profile(_raw(schema_version=99), "<test>")


def test_unknown_algorithm_rejected() -> None:
    with pytest.raises(ProfileError, match="unknown algorithm"):
        parse_profile(_raw(algorithms=["not_a_real_algorithm"]), "<test>")


def test_duplicate_algorithm_rejected() -> None:
    with pytest.raises(ProfileError, match="duplicate"):
        parse_profile(_raw(algorithms=["direct", "direct"]), "<test>")


def test_empty_algorithms_rejected() -> None:
    with pytest.raises(ProfileError, match="non-empty"):
        parse_profile(_raw(algorithms=[]), "<test>")


def test_gross_only_with_fixed_cost_rejected() -> None:
    with pytest.raises(ProfileError, match="must not set fixed_cost"):
        parse_profile(_raw(objective={"mode": "gross_only", "fixed_cost": 5}), "<test>")


def test_unknown_objective_key_rejected() -> None:
    with pytest.raises(ProfileError, match="unknown key"):
        parse_profile(_raw(objective={"mode": "gross_only", "bogus": True}), "<test>")


def test_load_profile_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_profile(tmp_path / "does_not_exist.yaml")


def test_load_profile_not_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text("- just\n- a\n- list\n")
    with pytest.raises(ProfileError, match="must be a mapping"):
        load_profile(path)


# ------------------------------------------------------------ calibrated profiles (WHI-1447)

REPO_ROOT = Path(__file__).resolve().parents[2]
SIX = (
    "direct",
    "single_path",
    "direct_split",
    "path_split",
    "incremental_graph",
    "uni_sor_port",
)


@pytest.mark.parametrize("name", ["daily", "full"])
def test_calibrated_profiles_and_their_gross_twins(name: str) -> None:
    """daily/full run all six mandatory algorithms under the pinned empirical cost model;
    each `_gross` twin differs only in its objective, so the two objectives compare the
    same search, budget and measurement."""
    net_path = REPO_ROOT / "config" / f"{name}.yaml"
    gross_path = REPO_ROOT / "config" / f"{name}_gross.yaml"
    net, gross = load_profile(net_path), load_profile(gross_path)
    assert net.algorithms == gross.algorithms == SIX
    assert net.objective.mode == "empirical_cost" and gross.objective.mode == "gross_only"
    assert net.objective.cost_model is not None
    assert net.objective.cost_model.sha256 == (
        "50e89727d6b6ac869c14c837c7cdec8099efc897d201794640912f3f6b4b7e71"
    )
    raw_net, raw_gross = (yaml.safe_load(p.read_text()) for p in (net_path, gross_path))
    raw_net.pop("objective")
    raw_gross.pop("objective")
    assert raw_net == raw_gross
    # Honest per-case limits are always declared (never uncapped in a measured profile).
    assert net.budget.max_quotes is not None and net.budget.time_limit_seconds is not None
    assert net.search["max_splits"] == 4 and net.search["percent_step"] == 5


def test_full_profile_keeps_the_design_trial_hop_bound_and_daily_declares_less() -> None:
    full = load_profile(REPO_ROOT / "config" / "full.yaml")
    daily = load_profile(REPO_ROOT / "config" / "daily.yaml")
    assert full.search["max_hops"] == 3  # DESIGN §2.12 trial value
    assert daily.search["max_hops"] == 2  # declared reduction (config/daily.yaml header)
    assert daily.measurement.memory_pass and daily.measurement.repeats >= 3

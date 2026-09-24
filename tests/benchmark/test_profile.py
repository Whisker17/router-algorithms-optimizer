"""Unit tests for `benchmark.profile`: schema validation, unknown-key rejection,
and objective-mode dispatch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmark.profile import ProfileError, load_profile, parse_profile


def test_load_profile_gross_only(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text("schema_version: 1\nalgorithms: [direct]\nobjective:\n  mode: gross_only\n")
    profile = load_profile(path)
    assert profile.algorithms == ("direct",)
    assert profile.objective.mode == "gross_only"


def test_load_profile_synthetic_fixed_cost(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text(
        "schema_version: 1\nalgorithms: [direct]\nobjective:\n"
        "  mode: synthetic_fixed_cost\n  fixed_cost: 42\n"
    )
    profile = load_profile(path)
    assert profile.objective.mode == "synthetic_fixed_cost"
    assert profile.objective.fixed_cost == 42


def test_unknown_top_level_key_rejected() -> None:
    raw = {
        "schema_version": 1,
        "algorithms": ["direct"],
        "objective": {"mode": "gross_only"},
        "unexpected": True,
    }
    with pytest.raises(ProfileError, match="unknown key"):
        parse_profile(raw, "<test>")


def test_unsupported_schema_version_rejected() -> None:
    raw = {"schema_version": 99, "algorithms": ["direct"], "objective": {"mode": "gross_only"}}
    with pytest.raises(ProfileError, match="unsupported schema"):
        parse_profile(raw, "<test>")


def test_unknown_algorithm_rejected() -> None:
    raw = {
        "schema_version": 1,
        "algorithms": ["not_a_real_algorithm"],
        "objective": {"mode": "gross_only"},
    }
    with pytest.raises(ProfileError, match="unknown algorithm"):
        parse_profile(raw, "<test>")


def test_empty_algorithms_rejected() -> None:
    raw = {"schema_version": 1, "algorithms": [], "objective": {"mode": "gross_only"}}
    with pytest.raises(ProfileError, match="non-empty"):
        parse_profile(raw, "<test>")


def test_gross_only_with_fixed_cost_rejected() -> None:
    raw = {
        "schema_version": 1,
        "algorithms": ["direct"],
        "objective": {"mode": "gross_only", "fixed_cost": 5},
    }
    with pytest.raises(ProfileError, match="must not set fixed_cost"):
        parse_profile(raw, "<test>")


def test_unknown_objective_key_rejected() -> None:
    raw = {
        "schema_version": 1,
        "algorithms": ["direct"],
        "objective": {"mode": "gross_only", "bogus": True},
    }
    with pytest.raises(ProfileError, match="unknown key"):
        parse_profile(raw, "<test>")


def test_load_profile_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_profile(tmp_path / "does_not_exist.yaml")


def test_load_profile_not_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text("- just\n- a\n- list\n")
    with pytest.raises(ProfileError, match="must be a mapping"):
        load_profile(path)

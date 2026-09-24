"""Typed, validated loader for run profiles under `config/*.yaml` (docs/DESIGN.md
§2.12: non-secret parameters live in validated YAML; unknown keys are rejected,
never silently ignored, mirroring `snapshot.config`'s conventions).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from benchmark.objective import ObjectiveContext, gross_only, synthetic_fixed_cost
from routing.algorithms.registry import ALGORITHMS

SUPPORTED_SCHEMA_VERSION = 1


class ProfileError(ValueError):
    """The run profile YAML is missing, malformed, or fails schema validation."""


def _require_keys(obj: Any, required: set[str], optional: set[str], where: str) -> None:
    if not isinstance(obj, dict):
        raise ProfileError(f"{where}: expected a mapping, got {type(obj).__name__}")
    missing = required - obj.keys()
    if missing:
        raise ProfileError(f"{where}: missing required key(s) {sorted(missing)}")
    unknown = obj.keys() - required - optional
    if unknown:
        raise ProfileError(f"{where}: unknown key(s) {sorted(unknown)}")


@dataclass(frozen=True)
class RunProfile:
    schema_version: int
    algorithms: tuple[str, ...]
    objective: ObjectiveContext
    source_path: str


def _parse_objective(obj: Any, where: str) -> ObjectiveContext:
    _require_keys(obj, {"mode"}, {"fixed_cost"}, where)
    mode = obj["mode"]
    if mode == "gross_only":
        if "fixed_cost" in obj:
            raise ProfileError(f"{where}: gross_only mode must not set fixed_cost")
        return gross_only()
    if mode == "synthetic_fixed_cost":
        if "fixed_cost" not in obj:
            raise ProfileError(f"{where}: synthetic_fixed_cost mode requires fixed_cost")
        fixed_cost = obj["fixed_cost"]
        if not isinstance(fixed_cost, int) or isinstance(fixed_cost, bool):
            raise ProfileError(f"{where}.fixed_cost: expected int, got {fixed_cost!r}")
        return synthetic_fixed_cost(fixed_cost)
    raise ProfileError(f"{where}.mode: unknown objective mode {mode!r}")


def parse_profile(raw: Any, source_path: str) -> RunProfile:
    _require_keys(raw, {"schema_version", "algorithms", "objective"}, set(), "<root>")
    if raw["schema_version"] != SUPPORTED_SCHEMA_VERSION:
        raise ProfileError(
            f"schema_version: unsupported schema {raw['schema_version']!r} "
            f"(expected {SUPPORTED_SCHEMA_VERSION})"
        )
    algorithms_obj = raw["algorithms"]
    if not isinstance(algorithms_obj, list) or not algorithms_obj:
        raise ProfileError("algorithms: expected a non-empty list")
    for name in algorithms_obj:
        if not isinstance(name, str):
            raise ProfileError(f"algorithms: expected strings, got {name!r}")
        if name not in ALGORITHMS:
            raise ProfileError(
                f"algorithms: unknown algorithm {name!r}; registered: {sorted(ALGORITHMS)}"
            )
    objective = _parse_objective(raw["objective"], "objective")
    return RunProfile(
        schema_version=raw["schema_version"],
        algorithms=tuple(algorithms_obj),
        objective=objective,
        source_path=source_path,
    )


def load_profile(path: str | Path) -> RunProfile:
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"{path}: top-level YAML document must be a mapping")
    try:
        return parse_profile(raw, str(path))
    except ProfileError as exc:
        raise ProfileError(f"{path}: {exc}") from exc

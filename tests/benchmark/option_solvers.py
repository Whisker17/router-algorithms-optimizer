"""Test-only options-accepting factories for the WHI-1548 `algorithm_options` seam.

They are NOT registered strategies: tests insert them into `registry.ALGORITHMS` with
`monkeypatch`, so the ordinary roster stays nine. They live in an importable module
because spawned workers unpickle a factory's functions by module + qualified name.

- `opt_fixture_a`: required `width` (int 1..8), `ratio` (finite number in [0, 1]) and
  `label` (choice), with a sha256-pinned preset
  (`tests/fixtures/r021_options/opt_fixture_a.yaml`);
- `opt_fixture_b`: one optional `width` (int >= 1; the same key name as A's, to show that
  siblings never see each other's values) and no preset.

Both `prepare` through the shared `validated_options` (the public entrypoint cannot bypass
it) and solve like `direct`, echoing the options their worker received in
`search_stats["options"]` so a test can prove the options crossed the worker boundary.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from routing.algorithms import direct
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    SolveContext,
    SolveResult,
    option_choice,
    option_int,
    option_number,
    require_option_keys,
    settings_sha256,
    validated_options,
)
from snapshot.models import Case, SnapshotBundle

A = "opt_fixture_a"
B = "opt_fixture_b"
LABELS = ("bounded", "stress")
PRESET_A: dict[str, Any] = {
    "path": "tests/fixtures/r021_options/opt_fixture_a.yaml",
    "sha256": "b05190d402e03a407b1a1c81c4e362891c02221646e728b8665925bbea21a5c9",
    "key": "R021-TEST-OPTIONS-A",
    "version": 1,
}


def validate_a(options: Mapping[str, Any]) -> dict[str, Any]:
    require_option_keys(options, {"width", "ratio", "label"})
    return {
        "width": option_int(options["width"], "width", 1, 8),
        "ratio": option_number(options["ratio"], "ratio", 0.0, 1.0),
        "label": option_choice(options["label"], "label", LABELS),
    }


def validate_b(options: Mapping[str, Any]) -> dict[str, Any]:
    require_option_keys(options, set(), {"width"})
    return {"width": option_int(options["width"], "width", 1)} if "width" in options else {}


def _log_attempt(name: str) -> None:
    path = os.environ.get("FAKE_SOLVE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}\n")


def prepare_a(bundle: SnapshotBundle, config: AlgorithmConfig) -> Any:
    return MappingProxyType(validated_options(FACTORY_A, config.options))


def prepare_b(bundle: SnapshotBundle, config: AlgorithmConfig) -> Any:
    return MappingProxyType(validated_options(FACTORY_B, config.options))


def _solve(name: str, case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    _log_attempt(name)
    options = dict(context.prepared)
    result = direct.solve(case, dataclasses.replace(context, prepared=None), budget)
    stats = {**result.search_stats, "options": options, "options_sha256": settings_sha256(options)}
    return dataclasses.replace(result, algorithm=name, search_stats=stats)


def solve_a(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    return _solve(A, case, context, budget)


def solve_b(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    return _solve(B, case, context, budget)


FACTORY_A = AlgorithmFactory(
    name=A,
    solve=solve_a,
    prepare=prepare_a,
    options_validator=validate_a,
    options_preset=MappingProxyType(PRESET_A),
)
FACTORY_B = AlgorithmFactory(name=B, solve=solve_b, prepare=prepare_b, options_validator=validate_b)
ALL = (FACTORY_A, FACTORY_B)

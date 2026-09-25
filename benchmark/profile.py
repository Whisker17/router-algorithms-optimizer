"""Typed, validated loader for run profiles under `config/*.yaml` (docs/DESIGN.md
§2.12: non-secret parameters live in validated YAML; unknown keys are rejected,
never silently ignored, mirroring `snapshot.config`'s conventions).

Schema 2 (WHI-1437) adds the measured-run sections. Every value is required and
explicit -- `budget`, `measurement` and `worker` have no built-in defaults
(docs/DESIGN.md §2.12: "Quote/time/candidate caps; warmup/repeats: explicit profile
values calibrated on the reference machine -- no invented universal defaults"). A
count limit may be `null` only by writing `null` explicitly ("no cap declared"); the
per-case time limit is always a positive number. Schema 1 profiles (no measurement
sections) are rejected with a pointer to the new keys.

The optional `search` section (WHI-1438) holds the docs/DESIGN.md §2.12 search
parameters (`search.max_hops`, `search.max_splits`, `search.percent_step` -- the last
a positive divisor of 100). It has no defaults either: a profile listing an algorithm
must declare every `search.*` key that algorithm requires
(`AlgorithmFactory.search_params`), and those values reach the algorithm's `prepare`
as `AlgorithmConfig.params`.

The optional `graph` section (WHI-1441) holds `graph.chunks` (docs/DESIGN.md §2.12:
explicit per profile, swept alongside percentage granularity), required by
`incremental_graph` through `AlgorithmFactory.graph_params` under the same no-default
rule; its values join the `search.*` values in `AlgorithmConfig.params`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from benchmark.objective import ObjectiveContext, gross_only, synthetic_fixed_cost
from routing.algorithms.base import AlgorithmConfig, AlgorithmFactory, Budget
from routing.algorithms.registry import ALGORITHMS

SUPPORTED_SCHEMA_VERSION = 2

CaseOrder = Literal["fixed", "reverse", "shuffle"]
CASE_ORDERS: tuple[CaseOrder, ...] = ("fixed", "reverse", "shuffle")
StartMethod = Literal["spawn", "forkserver"]
# `fork` is deliberately not offered: a forked worker inherits the parent's live
# memory, which defeats the clean-process isolation the runner promises.
START_METHODS: tuple[StartMethod, ...] = ("spawn", "forkserver")
WorkerScope = Literal["algorithm", "case"]
WORKER_SCOPES: tuple[WorkerScope, ...] = ("algorithm", "case")


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
class MeasurementSettings:
    """`warmup` discarded + `repeats` measured solve attempts per (case,
    algorithm), all in the same worker; `seed` drives the per-case solver seeds
    and the `shuffle` case order; `order` is the case order (`fixed` = bundle
    order; `reverse`/`shuffle` exist for state-leak checks); `memory_pass` adds a
    separate tracemalloc pass whose numbers never enter timing samples."""

    warmup: int
    repeats: int
    seed: int
    order: CaseOrder
    memory_pass: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "warmup": self.warmup,
            "repeats": self.repeats,
            "seed": self.seed,
            "order": self.order,
            "memory_pass": self.memory_pass,
        }


@dataclass(frozen=True)
class WorkerSettings:
    """`scope`: `algorithm` = one prepared worker per algorithm, replaced after any
    crash/timeout/error; `case` = a brand-new worker (and a new, separately
    charged `prepare`) for every case. `prepare_time_limit_seconds` bounds worker
    start-up plus `prepare`."""

    start_method: StartMethod
    scope: WorkerScope
    prepare_time_limit_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_method": self.start_method,
            "scope": self.scope,
            "prepare_time_limit_seconds": self.prepare_time_limit_seconds,
        }


@dataclass(frozen=True)
class RunProfile:
    schema_version: int
    algorithms: tuple[str, ...]
    objective: ObjectiveContext
    source_path: str
    budget: Budget
    measurement: MeasurementSettings
    worker: WorkerSettings
    search: dict[str, int] = field(default_factory=dict)
    graph: dict[str, int] = field(default_factory=dict)

    def algorithm_config(self, factory: AlgorithmFactory) -> AlgorithmConfig:
        """The `prepare` configuration for `factory`: exactly the `search.*` and
        `graph.*` values it declares it needs (the loader already guaranteed they are
        present)."""
        params = {key: self.search[key] for key in factory.search_params if key in self.search}
        params.update({key: self.graph[key] for key in factory.graph_params if key in self.graph})
        return AlgorithmConfig(name=factory.name, params=params)

    def resolved(self) -> dict[str, Any]:
        """Every profile value, including inherited defaults (docs/DESIGN.md §2.12:
        "All profile values appear in output even if inherited from defaults.")."""
        return {
            "schema_version": self.schema_version,
            "algorithms": list(self.algorithms),
            "objective": {"mode": self.objective.mode, "fixed_cost": self.objective.fixed_cost},
            "budget": self.budget.to_dict(),
            "measurement": self.measurement.to_dict(),
            "worker": self.worker.to_dict(),
            "search": dict(self.search),
            "graph": dict(self.graph),
            "algorithm_config": {
                name: {
                    "capabilities": ALGORITHMS[name].capabilities.to_dict(),
                    "params": dict(self.algorithm_config(ALGORITHMS[name]).params),
                    **(
                        {
                            "provenance": json.loads(
                                json.dumps(dict(ALGORITHMS[name].provenance or {}))
                            )
                        }
                        if ALGORITHMS[name].provenance is not None
                        else {}
                    ),
                }
                for name in self.algorithms
                if name in ALGORITHMS
            },
        }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_number(value: Any, where: str) -> float:
    if not (_is_int(value) or isinstance(value, float)) or not value > 0:
        raise ProfileError(f"{where}: expected a positive number, got {value!r}")
    return float(value)


def _int_at_least(value: Any, minimum: int, where: str) -> int:
    if not _is_int(value) or value < minimum:
        raise ProfileError(f"{where}: expected an integer >= {minimum}, got {value!r}")
    return int(value)


def _optional_positive_int(value: Any, where: str) -> int | None:
    if value is None:
        return None
    return _int_at_least(value, 1, where)


def _choice(value: Any, options: tuple[str, ...], where: str) -> Any:
    if value not in options:
        raise ProfileError(f"{where}: expected one of {list(options)}, got {value!r}")
    return value


def _parse_budget(obj: Any, where: str) -> Budget:
    _require_keys(obj, {"time_limit_seconds", "max_quotes", "max_candidates"}, set(), where)
    return Budget(
        time_limit_seconds=_positive_number(
            obj["time_limit_seconds"], f"{where}.time_limit_seconds"
        ),
        max_quotes=_optional_positive_int(obj["max_quotes"], f"{where}.max_quotes"),
        max_candidates=_optional_positive_int(obj["max_candidates"], f"{where}.max_candidates"),
    )


def _parse_measurement(obj: Any, where: str) -> MeasurementSettings:
    _require_keys(obj, {"warmup", "repeats", "seed", "order", "memory_pass"}, set(), where)
    if not isinstance(obj["memory_pass"], bool):
        raise ProfileError(f"{where}.memory_pass: expected a bool, got {obj['memory_pass']!r}")
    return MeasurementSettings(
        warmup=_int_at_least(obj["warmup"], 0, f"{where}.warmup"),
        repeats=_int_at_least(obj["repeats"], 1, f"{where}.repeats"),
        seed=_int_at_least(obj["seed"], 0, f"{where}.seed"),
        order=_choice(obj["order"], CASE_ORDERS, f"{where}.order"),
        memory_pass=obj["memory_pass"],
    )


def _parse_worker(obj: Any, where: str) -> WorkerSettings:
    _require_keys(obj, {"start_method", "scope", "prepare_time_limit_seconds"}, set(), where)
    return WorkerSettings(
        start_method=_choice(obj["start_method"], START_METHODS, f"{where}.start_method"),
        scope=_choice(obj["scope"], WORKER_SCOPES, f"{where}.scope"),
        prepare_time_limit_seconds=_positive_number(
            obj["prepare_time_limit_seconds"], f"{where}.prepare_time_limit_seconds"
        ),
    )


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


# Every `search.*` key the loader knows (docs/DESIGN.md §2.12), with its minimum.
SEARCH_KEYS: dict[str, int] = {"max_hops": 1, "max_splits": 1, "percent_step": 1}


def _parse_search(obj: Any, where: str) -> dict[str, int]:
    _require_keys(obj, set(), set(SEARCH_KEYS), where)
    search = {
        key: _int_at_least(obj[key], minimum, f"{where}.{key}")
        for key, minimum in SEARCH_KEYS.items()
        if key in obj
    }
    step = search.get("percent_step")
    if step is not None and (step > 100 or 100 % step):
        raise ProfileError(f"{where}.percent_step: must be a positive divisor of 100, got {step}")
    return search


# Every `graph.*` key the loader knows (docs/DESIGN.md §2.12), with its minimum.
GRAPH_KEYS: dict[str, int] = {"chunks": 1}


def _parse_graph(obj: Any, where: str) -> dict[str, int]:
    _require_keys(obj, set(), set(GRAPH_KEYS), where)
    return {
        key: _int_at_least(obj[key], minimum, f"{where}.{key}")
        for key, minimum in GRAPH_KEYS.items()
        if key in obj
    }


def parse_profile(raw: Any, source_path: str) -> RunProfile:
    if isinstance(raw, dict) and raw.get("schema_version") == 1:
        raise ProfileError(
            "schema_version: 1 predates measured runs (WHI-1437); use schema_version 2 and "
            "declare the budget, measurement and worker sections explicitly"
        )
    _require_keys(
        raw,
        {"schema_version", "algorithms", "objective", "budget", "measurement", "worker"},
        {"search", "graph"},
        "<root>",
    )
    if raw["schema_version"] != SUPPORTED_SCHEMA_VERSION:
        raise ProfileError(
            f"schema_version: unsupported schema {raw['schema_version']!r} "
            f"(expected {SUPPORTED_SCHEMA_VERSION})"
        )
    algorithms_obj = raw["algorithms"]
    if not isinstance(algorithms_obj, list) or not algorithms_obj:
        raise ProfileError("algorithms: expected a non-empty list")
    if len(set(map(str, algorithms_obj))) != len(algorithms_obj):
        raise ProfileError(f"algorithms: duplicate entries in {algorithms_obj!r}")
    for name in algorithms_obj:
        if not isinstance(name, str):
            raise ProfileError(f"algorithms: expected strings, got {name!r}")
        if name not in ALGORITHMS:
            raise ProfileError(
                f"algorithms: unknown algorithm {name!r}; registered: {sorted(ALGORITHMS)}"
            )
    objective = _parse_objective(raw["objective"], "objective")
    search = _parse_search(raw["search"], "search") if "search" in raw else {}
    graph = _parse_graph(raw["graph"], "graph") if "graph" in raw else {}
    for name in algorithms_obj:
        factory = ALGORITHMS[name]
        missing = [f"search.{key}" for key in factory.search_params if key not in search]
        missing += [f"graph.{key}" for key in factory.graph_params if key not in graph]
        if missing:
            raise ProfileError(
                f"algorithms: {name!r} requires "
                + ", ".join(missing)
                + " to be declared explicitly (no built-in default)"
            )
    return RunProfile(
        schema_version=raw["schema_version"],
        algorithms=tuple(algorithms_obj),
        objective=objective,
        source_path=source_path,
        budget=_parse_budget(raw["budget"], "budget"),
        measurement=_parse_measurement(raw["measurement"], "measurement"),
        worker=_parse_worker(raw["worker"], "worker"),
        search=search,
        graph=graph,
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

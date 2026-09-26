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

The optional `shortlist` section (WHI-1508) holds the pre-registered candidate-shortlist
settings of the opt-in experimental `uni_sor_fast` variant of `uni_sor_port`
(`shortlist.probe_percents`, `shortlist.routes_per_probe`, `shortlist.direct_routes`),
required through `AlgorithmFactory.shortlist_params` under the same no-default rule. No reference
algorithm declares them, and a profile without the section resolves exactly as before.

The optional `sampling` section (WHI-1509) holds the adaptive percentage-sampling settings
of the same opt-in `uni_sor_fast` (`sampling.coarse_step`, `sampling.refine_radius`,
`sampling.soft_max_quotes`), declared through `AlgorithmFactory.sampling_params`. Unlike
the sections above it is an **optional all-or-none group**: without the section
`uni_sor_fast` runs exactly as the L06 shortlist variant (nothing is handed over, nothing
is defaulted); with it, every key must be written explicitly (`soft_max_quotes` may be an
explicit `null`: no soft cap declared). A profile without the section resolves exactly as
before.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from benchmark.objective import ObjectiveContext, empirical_cost, gross_only, synthetic_fixed_cost
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
    shortlist: dict[str, Any] = field(default_factory=dict)
    sampling: dict[str, Any] = field(default_factory=dict)

    def algorithm_config(self, factory: AlgorithmFactory) -> AlgorithmConfig:
        """The `prepare` configuration for `factory`: exactly the `search.*`, `graph.*`
        and `shortlist.*` values it declares it needs (the loader already guaranteed they
        are present; every value is an int or a tuple of ints, so nothing mutable is
        shared with the prepared state)."""
        params = {key: self.search[key] for key in factory.search_params if key in self.search}
        params.update({key: self.graph[key] for key in factory.graph_params if key in self.graph})
        params.update(
            {key: self.shortlist[key] for key in factory.shortlist_params if key in self.shortlist}
        )
        params.update(
            {key: self.sampling[key] for key in factory.sampling_params if key in self.sampling}
        )
        return AlgorithmConfig(name=factory.name, params=params)

    def resolved(self) -> dict[str, Any]:
        """Every profile value, including inherited defaults (docs/DESIGN.md §2.12:
        "All profile values appear in output even if inherited from defaults.")."""
        return {
            "schema_version": self.schema_version,
            "algorithms": list(self.algorithms),
            "objective": self.objective.to_dict(),
            "budget": self.budget.to_dict(),
            "measurement": self.measurement.to_dict(),
            "worker": self.worker.to_dict(),
            "search": dict(self.search),
            "graph": dict(self.graph),
            # Only a profile that declares the experimental section records it, so every
            # existing profile resolves byte-identically (WHI-1508).
            **({"shortlist": _plain(self.shortlist)} if self.shortlist else {}),
            **({"sampling": dict(self.sampling)} if self.sampling else {}),
            "algorithm_config": {
                name: {
                    "capabilities": ALGORITHMS[name].capabilities.to_dict(),
                    "params": _plain(self.algorithm_config(ALGORITHMS[name]).params),
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


def _plain(values: Any) -> dict[str, Any]:
    """JSON-shaped copy (tuples become lists) of a parameter mapping."""
    return {k: list(v) if isinstance(v, tuple) else v for k, v in dict(values).items()}


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
    _require_keys(obj, {"mode"}, {"fixed_cost", "cost_model", "cost_model_sha256"}, where)
    mode = obj["mode"]
    if mode != "empirical_cost" and ("cost_model" in obj or "cost_model_sha256" in obj):
        raise ProfileError(f"{where}: cost_model/cost_model_sha256 belong to empirical_cost")
    if mode == "empirical_cost":
        from benchmark.costs import CostModelError, load_cost_model

        if "fixed_cost" in obj:
            raise ProfileError(f"{where}: empirical_cost mode must not set fixed_cost")
        missing = [k for k in ("cost_model", "cost_model_sha256") if k not in obj]
        if missing:
            raise ProfileError(
                f"{where}: empirical_cost requires {missing} (the frozen artifact and its "
                "declared identity)"
            )
        try:
            model = load_cost_model(
                str(obj["cost_model"]), expected_sha256=str(obj["cost_model_sha256"])
            )
        except (OSError, CostModelError) as exc:
            raise ProfileError(f"{where}.cost_model: {exc}") from exc
        return empirical_cost(model)
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


# Every `shortlist.*` key the loader knows (WHI-1508, opt-in `uni_sor_fast` only).
SHORTLIST_KEYS = ("probe_percents", "routes_per_probe", "direct_routes")


def _parse_shortlist(obj: Any, where: str, percent_step: int | None) -> dict[str, Any]:
    """`probe_percents`: a non-empty list of distinct grid percents (multiples of
    `search.percent_step`, at most 100) that must include 100 -- the full-input
    incumbent is always ranked -- stored ascending as a tuple; `routes_per_probe` >= 1;
    `direct_routes` >= 0."""
    _require_keys(obj, set(), set(SHORTLIST_KEYS), where)
    out: dict[str, Any] = {}
    if "routes_per_probe" in obj:
        out["routes_per_probe"] = _int_at_least(
            obj["routes_per_probe"], 1, f"{where}.routes_per_probe"
        )
    if "direct_routes" in obj:
        out["direct_routes"] = _int_at_least(obj["direct_routes"], 0, f"{where}.direct_routes")
    if "probe_percents" in obj:
        probes = obj["probe_percents"]
        at = f"{where}.probe_percents"
        if not isinstance(probes, list) or not probes:
            raise ProfileError(f"{at}: expected a non-empty list, got {probes!r}")
        if percent_step is None:
            raise ProfileError(f"{at}: needs search.percent_step to define the grid")
        for p in probes:
            if not _is_int(p) or not 1 <= p <= 100 or p % percent_step:
                raise ProfileError(
                    f"{at}: {p!r} is not a percent of the {percent_step}% grid (1..100)"
                )
        if len(set(probes)) != len(probes):
            raise ProfileError(f"{at}: duplicate entries in {probes!r}")
        if 100 not in probes:
            raise ProfileError(f"{at}: must include 100 (the full-input incumbent)")
        out["probe_percents"] = tuple(sorted(probes))
    return out


# Every `sampling.*` key (WHI-1509, opt-in `uni_sor_fast` only): all or none.
SAMPLING_KEYS = ("coarse_step", "refine_radius", "soft_max_quotes")


def _parse_sampling(obj: Any, where: str, percent_step: int | None) -> dict[str, Any]:
    """`coarse_step`: a multiple of `search.percent_step` that divides 100 (so the coarse
    grid contains the 100 % full-input entry); `refine_radius` >= 1 fine grid steps;
    `soft_max_quotes` >= 1, or an explicit `null` (no soft cap declared)."""
    _require_keys(obj, set(SAMPLING_KEYS), set(), where)
    if percent_step is None:
        raise ProfileError(f"{where}.coarse_step: needs search.percent_step to define the grid")
    coarse = _int_at_least(obj["coarse_step"], 1, f"{where}.coarse_step")
    if coarse % percent_step or 100 % coarse:
        raise ProfileError(
            f"{where}.coarse_step: {coarse} must be a multiple of search.percent_step "
            f"({percent_step}) that divides 100"
        )
    return {
        "coarse_step": coarse,
        "refine_radius": _int_at_least(obj["refine_radius"], 1, f"{where}.refine_radius"),
        "soft_max_quotes": _optional_positive_int(
            obj["soft_max_quotes"], f"{where}.soft_max_quotes"
        ),
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
        {"search", "graph", "shortlist", "sampling"},
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
    shortlist = (
        _parse_shortlist(raw["shortlist"], "shortlist", search.get("percent_step"))
        if "shortlist" in raw
        else {}
    )
    sampling = (
        _parse_sampling(raw["sampling"], "sampling", search.get("percent_step"))
        if "sampling" in raw
        else {}
    )
    for name in algorithms_obj:
        factory = ALGORITHMS[name]
        missing = [f"search.{key}" for key in factory.search_params if key not in search]
        missing += [f"graph.{key}" for key in factory.graph_params if key not in graph]
        missing += [f"shortlist.{key}" for key in factory.shortlist_params if key not in shortlist]
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
        shortlist=shortlist,
        sampling=sampling,
    )


# WHI-1498 single-request comparison: exactly one solve attempt per algorithm and no
# separate memory pass, whatever the source profile's measurement counts say. The owner's
# requirement for that command, not a search or budget default.
SINGLE_RUN_MEASUREMENT: dict[str, Any] = {"warmup": 0, "repeats": 1, "memory_pass": False}


def single_run_document(raw: dict[str, Any]) -> dict[str, Any]:
    """A copy of a profile document with `SINGLE_RUN_MEASUREMENT` applied; every other
    value (algorithms, objective, search, budget, seed, worker) is the source's."""
    document: dict[str, Any] = json.loads(json.dumps(raw))
    document["measurement"] = {**document["measurement"], **SINGLE_RUN_MEASUREMENT}
    return document


def read_profile_document(path: str | Path) -> dict[str, Any]:
    """The raw YAML mapping of a profile file (not yet validated)."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"{path}: top-level YAML document must be a mapping")
    return raw


def load_profile(path: str | Path) -> RunProfile:
    raw = read_profile_document(path)
    try:
        return parse_profile(raw, str(path))
    except ProfileError as exc:
        raise ProfileError(f"{path}: {exc}") from exc

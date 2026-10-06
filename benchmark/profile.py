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
rule; its values join the `search.*` values in `AlgorithmConfig.params`. The same section
holds `graph.label_hops` (an integer >= `search.max_hops`) and `graph.label_pruning` (an
explicit bool), the settings of the opt-in experimental `metis_inspired` (WHI-1449), which
alone declares them; no other factory receives them and a profile without them resolves
exactly as before. A factory with an `AlgorithmFactory.graph_params_for` hook
(WHI-1626: `split_polish`, whose graph keys are its declared base's) requires and receives the
keys that hook returns for its normalized `algorithm_options` instead; no other factory sets it.

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

The optional `strategies` section (WHI-1528) holds the settings of the **named optimized
strategies** (`uni_sor_adaptive`, `uni_sor_optimized`; `AlgorithmFactory.strategy_recipe`),
one entry per selected strategy: its `recipe` identity (a frozen, sha256-pinned arms file
and arm) and that arm's `shortlist`, `sampling` and exact quote `controls`, written out in
full. The loader re-reads the pinned arms file (the shared, sha256-verified
`uni_sor_strategies.registered_settings`, which the factory's `prepare` also checks) and
refuses any entry whose settings differ from the registered arm, so a strategy name always
means exactly its recipe; each strategy gets only its own entry (never the global
`shortlist` / `sampling` sections). A profile listing such a strategy must declare its
entry. The optional `selection` section records how `main.py run|quote --strategies`
derived an effective profile (mode, source profile identity, the `base` / `optimized` /
`custom` groups); it is validated against `algorithms` and persisted for offline
reporting. Profiles without either section resolve exactly as before.

The optional `algorithm_options` section (WHI-1548, contract R021-C/1 §9) maps a selected
algorithm ID to that algorithm's own options. Only a factory with an
`AlgorithmFactory.options_validator` accepts an entry; the shared `validated_options` check
(the same one its `prepare` applies) refuses unknown IDs/keys, reserved shared-setting keys,
booleans as integers, non-finite and out-of-range values before any worker or result exists.
Each entry is handed only to its own algorithm (`AlgorithmConfig.options`), and its resolved
form records the complete normalized `options`, their `settings_sha256` and a `source` derived
from content alone: `{kind: preset, ...pin}` only when the options equal the factory's
sha256-pinned preset (`preset_options`), otherwise `{kind: override}` -- a document can never
claim the preset identity for other settings. A factory that replaced its preset (WHI-1559
`cfmm_dual`) also registers the earlier pins as `historical_presets`: options equal to one of
them resolve to that same `{kind: preset, ...pin}` (verified like the current one; equal to
several pins is refused as ambiguous), so a saved profile keeps its original source identity;
only the current pin is ever written out as a default. A profile without the section resolves
exactly as before (the key is omitted).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import yaml

from benchmark.objective import ObjectiveContext, empirical_cost, gross_only, synthetic_fixed_cost
from routing.algorithms import uni_sor_strategies
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    OptionsError,
    settings_sha256,
    validated_options,
)
from routing.algorithms.registry import ALGORITHMS, BASE_STRATEGIES, OPTIMIZED_STRATEGIES

SUPPORTED_SCHEMA_VERSION = 2
REPO_ROOT = Path(__file__).resolve().parents[1]

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
    strategies: dict[str, dict[str, Any]] = field(default_factory=dict)
    selection: dict[str, Any] = field(default_factory=dict)
    # WHI-1548: resolved `{options, source, settings_sha256}` per options-accepting algorithm.
    algorithm_options: dict[str, dict[str, Any]] = field(default_factory=dict)

    def algorithm_config(self, factory: AlgorithmFactory) -> AlgorithmConfig:
        """The `prepare` configuration for `factory`: exactly the `search.*`, `graph.*`
        and `shortlist.*` values it declares it needs (the loader already guaranteed they
        are present; every value is an int or a tuple of ints, so nothing mutable is
        shared with the prepared state). A named strategy (WHI-1528) gets its own
        `strategies.<name>` recipe settings (plus a fresh copy of its `controls`) instead."""
        resolved = self.algorithm_options.get(factory.name)
        graph_keys = factory.graph_params
        if factory.graph_params_for is not None and resolved is not None:  # WHI-1626
            graph_keys = factory.graph_params_for(resolved["options"])
        params: dict[str, Any] = {
            key: self.search[key] for key in factory.search_params if key in self.search
        }
        params.update({key: self.graph[key] for key in graph_keys if key in self.graph})
        params.update(
            {key: self.shortlist[key] for key in factory.shortlist_params if key in self.shortlist}
        )
        params.update(
            {key: self.sampling[key] for key in factory.sampling_params if key in self.sampling}
        )
        entry = self.strategies.get(factory.name) if factory.strategy_recipe is not None else None
        if entry is not None:  # a named strategy: its own recipe settings only
            params.update(entry["shortlist"])
            params.update(entry["sampling"])
            params["controls"] = {k: dict(v) for k, v in entry["controls"].items()}
        options = (  # a fresh read-only copy of this algorithm's own options only
            MappingProxyType(json.loads(json.dumps(resolved["options"])))
            if resolved is not None
            else MappingProxyType({})
        )
        return AlgorithmConfig(name=factory.name, params=params, options=options)

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
            # WHI-1528: only a profile that declares them records these sections.
            **({"strategies": json.loads(json.dumps(self.strategies))} if self.strategies else {}),
            **({"selection": json.loads(json.dumps(self.selection))} if self.selection else {}),
            # WHI-1548: only a profile selecting an options-accepting algorithm records it.
            **(
                {"algorithm_options": json.loads(json.dumps(self.algorithm_options))}
                if self.algorithm_options
                else {}
            ),
            "algorithm_config": {
                name: {
                    "capabilities": ALGORITHMS[name].capabilities.to_dict(),
                    "params": json.loads(
                        json.dumps(_plain(self.algorithm_config(ALGORITHMS[name]).params))
                    ),
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


# Every integer `graph.*` key the loader knows (docs/DESIGN.md §2.12), with its minimum.
# `label_hops` (WHI-1449, opt-in `metis_inspired` only) must also be >= `search.max_hops`.
GRAPH_KEYS: dict[str, int] = {"chunks": 1, "label_hops": 1}
# Boolean `graph.*` keys (WHI-1449 `label_pruning`: the mechanism ablation switch).
GRAPH_FLAGS = ("label_pruning",)


def _parse_graph(obj: Any, where: str) -> dict[str, int]:
    _require_keys(obj, set(), set(GRAPH_KEYS) | set(GRAPH_FLAGS), where)
    graph = {
        key: _int_at_least(obj[key], minimum, f"{where}.{key}")
        for key, minimum in GRAPH_KEYS.items()
        if key in obj
    }
    for key in GRAPH_FLAGS:
        if key in obj:
            if not isinstance(obj[key], bool):
                raise ProfileError(f"{where}.{key}: expected a bool, got {obj[key]!r}")
            graph[key] = obj[key]
    return graph


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


# ------------------------------------------------------------------ WHI-1528 strategies

STRATEGY_KEYS = ("recipe", "shortlist", "sampling", "controls")


def recipe_settings(recipe: Mapping[str, Any], where: str) -> dict[str, Any]:
    """The registered settings of `recipe`'s arm (`shortlist`, `sampling`, `controls`): the
    one shared, sha256-verified reader (`uni_sor_strategies.registered_settings`) the
    factory's `prepare` uses too."""
    try:
        return uni_sor_strategies.registered_settings(recipe)
    except uni_sor_strategies.UniSorStrategyError as exc:
        raise ProfileError(f"{where}: {exc}") from exc


def strategy_entry(name: str) -> dict[str, Any]:
    """The complete `strategies.<name>` entry of a named strategy, from its recipe."""
    factory = ALGORITHMS[name]
    if factory.strategy_recipe is None:
        raise ProfileError(f"{name!r} is not a named optimized strategy")
    recipe = dict(factory.strategy_recipe)
    return {"recipe": recipe, **recipe_settings(recipe, f"strategies.{name}")}


def _parse_strategies(
    obj: Any, where: str, algorithms: list[str], percent_step: int | None
) -> dict[str, dict[str, Any]]:
    if not isinstance(obj, dict) or not obj:
        raise ProfileError(f"{where}: expected a non-empty mapping")
    out: dict[str, dict[str, Any]] = {}
    for name, entry in obj.items():
        at = f"{where}.{name}"
        factory = ALGORITHMS.get(name) if isinstance(name, str) else None
        if factory is None or factory.strategy_recipe is None:
            raise ProfileError(
                f"{at}: not a named optimized strategy (known: {list(OPTIMIZED_STRATEGIES)})"
            )
        if name not in algorithms:
            raise ProfileError(f"{at}: declared but {name!r} is not in algorithms")
        _require_keys(entry, set(STRATEGY_KEYS), set(), at)
        recipe = dict(factory.strategy_recipe)
        if entry["recipe"] != recipe:
            raise ProfileError(
                f"{at}.recipe: expected the registered recipe {recipe}, got {entry['recipe']!r}"
            )
        shortlist = _parse_shortlist(entry["shortlist"], f"{at}.shortlist", percent_step)
        missing = [k for k in SHORTLIST_KEYS if k not in shortlist]
        if missing:
            raise ProfileError(f"{at}.shortlist: missing required key(s) {missing}")
        sampling = _parse_sampling(entry["sampling"], f"{at}.sampling", percent_step)
        if not isinstance(entry["controls"], dict):
            raise ProfileError(f"{at}.controls: expected a mapping")
        declared = {"shortlist": _plain(shortlist), "sampling": sampling,
                    "controls": entry["controls"]}  # fmt: skip
        registered = recipe_settings(recipe, at)  # a changed recipe file is refused first
        try:  # the same check the factory's `prepare` applies
            uni_sor_strategies.check_recipe(recipe, declared)
        except uni_sor_strategies.UniSorStrategyError as exc:
            raise ProfileError(f"{at}.{exc}") from exc
        out[name] = {
            "recipe": recipe,
            "shortlist": shortlist,
            "sampling": sampling,
            "controls": {k: dict(v) for k, v in registered["controls"].items()},
        }
    return out


# ------------------------------------------------------------------ WHI-1548 algorithm_options


def preset_options(
    factory: AlgorithmFactory, pin: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The validated options of `factory`'s pinned bounded comparison preset (R021-C/1 §7.1):
    a YAML file `{key, version, algorithm, options}` read only after its bytes hash to the
    pin; a changed, unreadable or mismatching file is refused, never reinterpreted. `pin`
    (only ever one of the factory's own registered `historical_presets`) reads an earlier
    version instead of the current `options_preset`."""
    pin = factory.options_preset if pin is None else pin
    where = f"algorithm_options.{factory.name}: preset"
    if pin is None:
        raise ProfileError(f"{where}: {factory.name!r} has no registered preset")
    try:
        data = (REPO_ROOT / str(pin["path"])).read_bytes()
    except OSError as exc:
        raise ProfileError(f"{where} {pin['path']} unreadable: {exc}") from exc
    digest = hashlib.sha256(data).hexdigest()
    if digest != pin["sha256"]:
        raise ProfileError(
            f"{where} {pin['path']} sha256 {digest} differs from the pin {pin['sha256']} "
            "(a registered preset is frozen; a changed file is refused)"
        )
    doc = yaml.safe_load(data)
    if not isinstance(doc, dict) or (doc.get("key"), doc.get("version"), doc.get("algorithm")) != (
        pin["key"], pin["version"], factory.name
    ):  # fmt: skip
        raise ProfileError(f"{where} {pin['path']} is not {pin['key']} v{pin['version']} of it")
    try:
        return validated_options(factory, doc.get("options"))
    except OptionsError as exc:
        raise ProfileError(f"{where} {pin['path']}: {exc}") from exc


def options_entry(factory: AlgorithmFactory, options: Any) -> dict[str, Any]:
    """The resolved `{options, source, settings_sha256}` of `factory`'s (declared) options.
    `source` is a preset pin only when the normalized options equal that pinned preset's:
    the current `options_preset` or one of the factory's registered `historical_presets`
    (WHI-1559; each verified like the current one). Options equal to more than one pin are
    refused as ambiguous, never resolved by order."""
    try:
        normalized = validated_options(factory, options)
    except OptionsError as exc:
        raise ProfileError(str(exc)) from exc
    pins = [factory.options_preset] if factory.options_preset is not None else []
    pins += factory.historical_presets
    matched = [pin for pin in pins if normalized == preset_options(factory, pin)]
    if len(matched) > 1:
        names = ", ".join(f"{pin['key']} v{pin['version']}" for pin in matched)
        raise ProfileError(
            f"algorithm_options.{factory.name}: the options equal {len(matched)} registered "
            f"presets ({names}); a preset identity must be unambiguous"
        )
    source: dict[str, Any] = {"kind": "override"}
    if matched:
        source = {"kind": "preset", **json.loads(json.dumps(dict(matched[0])))}
    return {
        "options": normalized,
        "source": source,
        "settings_sha256": settings_sha256(normalized),
    }


def _parse_algorithm_options(
    obj: Any, where: str, algorithms: list[str]
) -> dict[str, dict[str, Any]]:
    """`obj` is the declared section, or `None` when the profile omits it."""
    if obj is not None and (not isinstance(obj, dict) or not obj):
        raise ProfileError(f"{where}: expected a non-empty mapping (omit the section instead)")
    declared: dict[Any, Any] = obj or {}
    for name in declared:
        at = f"{where}.{name}"
        if not isinstance(name, str) or name not in ALGORITHMS:
            raise ProfileError(f"{at}: unknown algorithm; registered: {sorted(ALGORITHMS)}")
        if name not in algorithms:
            raise ProfileError(f"{at}: declared but {name!r} is not in algorithms")
        if ALGORITHMS[name].options_validator is None:
            raise ProfileError(f"{at}: {name!r} accepts no algorithm_options (no validator)")
    out: dict[str, dict[str, Any]] = {}
    for name in algorithms:
        factory = ALGORITHMS[name]
        if factory.options_validator is None:
            continue
        if name not in declared:
            try:
                validated_options(factory, {})
            except OptionsError as exc:
                raise ProfileError(
                    f"algorithms: {name!r} requires algorithm_options.{name} to be declared "
                    "explicitly (no built-in default; only an identity --strategies all adds "
                    f"itself receives its pinned preset): {exc}"
                ) from exc
        out[name] = options_entry(factory, declared.get(name, {}))
    return out


SELECTION_MODES = ("all", "base", "optimized")
GROUPS = ("base", "optimized", "custom")


def strategy_group(name: str) -> str:
    """`base` (the six mandatory references), `optimized` (the named strategies) or `custom`
    (any other profile-selected algorithm, e.g. the configurable `uni_sor_fast`)."""
    if name in BASE_STRATEGIES:
        return "base"
    if name in OPTIMIZED_STRATEGIES:
        return "optimized"
    return "custom"


def _parse_selection(obj: Any, where: str, algorithms: list[str]) -> dict[str, Any]:
    _require_keys(obj, {"mode", "source_profile", "groups"}, set(), where)
    if obj["mode"] not in SELECTION_MODES:
        raise ProfileError(f"{where}.mode: expected one of {list(SELECTION_MODES)}")
    source = obj["source_profile"]
    _require_keys(source, {"path", "sha256"}, set(), f"{where}.source_profile")
    if not all(isinstance(source[k], str) and source[k] for k in ("path", "sha256")):
        raise ProfileError(f"{where}.source_profile: path and sha256 must be non-empty strings")
    groups = obj["groups"]
    _require_keys(groups, set(GROUPS), set(), f"{where}.groups")
    for group in GROUPS:
        members = groups[group]
        at = f"{where}.groups.{group}"
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            raise ProfileError(f"{at}: expected a list of algorithm names")
        wrong = [m for m in members if strategy_group(m) != group]
        if wrong:
            raise ProfileError(f"{at}: {wrong} do not belong to the {group} group")
        if members != [a for a in algorithms if a in members]:
            raise ProfileError(f"{at}: {members} is not in the algorithms' order")
    listed = [m for g in GROUPS for m in groups[g]]
    if sorted(listed) != sorted(algorithms):
        raise ProfileError(f"{where}.groups: must list every algorithm exactly once")
    return {
        "mode": obj["mode"],
        "source_profile": {"path": source["path"], "sha256": source["sha256"]},
        "groups": {g: list(groups[g]) for g in GROUPS},
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
        {"search", "graph", "shortlist", "sampling", "strategies", "selection"}
        | {"algorithm_options"},
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
    if "label_hops" in graph:
        if "max_hops" not in search:
            raise ProfileError("graph.label_hops: needs search.max_hops to be declared")
        if graph["label_hops"] < search["max_hops"]:
            raise ProfileError(
                f"graph.label_hops: must be >= search.max_hops ({search['max_hops']}), "
                f"got {graph['label_hops']}"
            )
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
    declared_strategies = raw.get("strategies")
    for name in algorithms_obj:
        factory = ALGORITHMS[name]
        missing = [f"search.{key}" for key in factory.search_params if key not in search]
        if factory.strategy_recipe is not None and not (
            isinstance(declared_strategies, dict) and name in declared_strategies
        ):
            missing.append(f"strategies.{name}")
        missing += [f"graph.{key}" for key in factory.graph_params if key not in graph]
        missing += [f"shortlist.{key}" for key in factory.shortlist_params if key not in shortlist]
        if missing:
            raise ProfileError(
                f"algorithms: {name!r} requires "
                + ", ".join(missing)
                + " to be declared explicitly (no built-in default)"
            )
    strategies = (
        _parse_strategies(
            raw["strategies"], "strategies", algorithms_obj, search.get("percent_step")
        )
        if "strategies" in raw
        else {}
    )
    selection = (
        _parse_selection(raw["selection"], "selection", algorithms_obj)
        if "selection" in raw
        else {}
    )
    algorithm_options = _parse_algorithm_options(
        raw["algorithm_options"] if "algorithm_options" in raw else None,
        "algorithm_options",
        algorithms_obj,
    )
    for name, entry in algorithm_options.items():  # WHI-1626: graph keys given the options
        hook = ALGORITHMS[name].graph_params_for
        keys = hook(entry["options"]) if hook is not None else ()
        missing = [f"graph.{key}" for key in keys if key not in graph]
        if missing:
            raise ProfileError(
                f"algorithms: {name!r} with its algorithm_options requires "
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
        strategies=strategies,
        selection=selection,
        algorithm_options=algorithm_options,
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

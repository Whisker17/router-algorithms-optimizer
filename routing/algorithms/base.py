"""Shared solver-side types (docs/DESIGN.md §4.3, §2.10): `SolveContext`, `Budget`,
`SolveResult`, the full solver status vocabulary, and the `AlgorithmFactory` that
pairs an algorithm's optional `prepare(bundle, config)` with its
`solve(case, context, budget)`. Every mandatory algorithm (docs/DESIGN.md §2.6)
implements these same types.

Factories hold ordinary module-level Python functions: the benchmark runner
(WHI-1437) ships a factory to an isolated worker process by pickling it, which
serializes the functions *by reference* (module + qualified name). Lambdas and
closures therefore cannot be registered -- by design, an algorithm is importable
code, not a live object smuggled across the process boundary.

Per-strategy options (WHI-1548, contract R021-C/1 §9.2): a factory that accepts
`algorithm_options.<id>` names a module-level `options_validator`; `validated_options` is
the one shared entry that both the profile loader and that factory's own `prepare` call,
so a direct caller of the public `prepare` cannot bypass the checks. It refuses options for
a factory without a validator, reserved shared-setting keys, non-string keys and non-finite
or non-JSON values, then applies the factory's validator (the `option_*` helpers below keep
its integer/bool/finite checks strict). The canonical hash of the normalized options is
`settings_sha256`.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from benchmark.objective import ObjectiveContext
from routing.evaluator import Evaluation
from routing.plan import RoutePlan
from snapshot.models import Case, SnapshotBundle


class SolveStatus(StrEnum):
    """The full runner status vocabulary (docs/DESIGN.md §2.10). Not every
    algorithm produces every value; `direct` returns `OK`, `NO_ROUTE` or
    `INCOMPLETE_SNAPSHOT` (a candidate needed uncollected state, WHI-1429). The
    runner itself assigns `TIMEOUT` (a hard time/quote limit cut the search off),
    and a solver may return `TIMEOUT` itself when its own cooperative budget
    accounting stopped the search before any complete route was found (e.g.
    `single_path`: a truncated search is never evidence of `NO_ROUTE`);
    `ALGORITHM_ERROR` (the solver raised, crashed its worker or returned garbage),
    `INVALID_PLAN` (the independent evaluation rejected a submitted plan) and
    `CANCELLED` (the run was interrupted before the case finished)."""

    OK = "ok"
    UNSUPPORTED = "unsupported"
    NO_ROUTE = "no_route"
    TIMEOUT = "timeout"
    INVALID_PLAN = "invalid_plan"
    INCOMPLETE_SNAPSHOT = "incomplete_snapshot"
    MODEL_ERROR = "model_error"
    ALGORITHM_ERROR = "algorithm_error"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class SolveContext:
    """Everything a `solve()` call needs beyond the case itself: the immutable
    bundle, the shared objective (docs/DESIGN.md §2.9), the algorithm's immutable
    `prepared` index (whatever its `prepare()` returned; charged separately), the
    deterministic per-case `seed`, and an optional sink for intermediate
    candidates.

    The runner builds a fresh context for every solve attempt (docs/DESIGN.md
    §2.10: "fresh per-case scratch state"); a solver must keep its mutable search
    state local to the call."""

    bundle: SnapshotBundle
    objective: ObjectiveContext
    prepared: Any = None
    seed: int = 0
    candidate_sink: Callable[[RoutePlan], None] | None = field(
        default=None, compare=False, repr=False
    )
    # WHI-1548 (R021-C/1 §4.1 `source`): the runner's own identity of this run --
    # `git_revision`, `bundle_hash`, `algorithm`, `effective_settings_sha256` -- so a solver
    # that emits `search_stats["r021"]` can name the run its certificate belongs to. The
    # runner validates a certificate against its own copy, never against this echo; empty
    # for direct unit-level calls.
    run_identity: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}), compare=False
    )

    def report_candidate(self, plan: RoutePlan) -> None:
        """Publish the current best complete plan. If the solve is later cut off
        by a hard limit, the runner independently evaluates the last reported plan
        and keeps it as a separately labeled `last_valid_candidate` -- never as a
        normal completed solve (docs/DESIGN.md §2.10)."""
        if self.candidate_sink is not None:
            self.candidate_sink(plan)


@dataclass(frozen=True)
class Budget:
    """Declared solve limits, always taken from the validated run profile
    (docs/DESIGN.md §2.12: "Quote/time/candidate caps ... No invented universal
    defaults"). `None` means "no limit declared" and is only used by direct
    unit-level calls; the measured runner requires a profile to declare every
    value explicitly.

    - `time_limit_seconds`: hard wall-clock limit per solve attempt; the runner
      kills the worker when it elapses.
    - `max_quotes`: hard cap on `pools.quote.quote_exact_in` calls per attempt,
      metered by the worker; exceeding it ends the attempt.
    - `max_candidates`: a cooperative cap the algorithm applies itself and reports
      as declared truncation (`SolveResult.candidates_truncated`)."""

    time_limit_seconds: float | None = None
    max_quotes: int | None = None
    max_candidates: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "time_limit_seconds": self.time_limit_seconds,
            "max_quotes": self.max_quotes,
            "max_candidates": self.max_candidates,
        }


@dataclass(frozen=True)
class SolveResult:
    case_id: str
    algorithm: str
    status: SolveStatus
    plan: RoutePlan | None = None
    evaluation: Evaluation | None = None
    score: int | None = None
    candidates_considered: int = 0
    # Candidates the algorithm deliberately skipped because of a declared limit
    # (e.g. `Budget.max_candidates`) -- declared truncation, visible in results.
    candidates_truncated: int = 0
    error: str | None = None
    # Algorithm-specific, deterministic search counters (e.g. `single_path`: hop
    # bound, paths enumerated/evaluated/pruned/truncated, which limit truncated the
    # search, quotes executed vs. memoized). JSON-serializable values only; recorded
    # verbatim as `search` in the run's per-case record.
    search_stats: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "algorithm": self.algorithm,
            "status": self.status.value,
            "evaluation": self.evaluation.to_dict() if self.evaluation is not None else None,
            "score": None if self.score is None else str(self.score),
            "candidates_considered": self.candidates_considered,
            "candidates_truncated": self.candidates_truncated,
            "error": self.error,
            "search": dict(self.search_stats),
        }


@dataclass(frozen=True)
class AlgorithmConfig:
    """The per-algorithm configuration handed to `prepare()`: the validated profile
    `search.*` values the algorithm declared in `AlgorithmFactory.search_params`
    (e.g. `{"max_hops": 3}` for `single_path`) plus the `graph.*` values it declared in
    `AlgorithmFactory.graph_params` (e.g. `{"chunks": 20}`) and the `shortlist.*` values
    it declared in `AlgorithmFactory.shortlist_params` (the opt-in `uni_sor_fast`
    experiment, WHI-1508) and, only when the profile declares them, the `sampling.*` values
    of `AlgorithmFactory.sampling_params` (WHI-1509); empty for algorithms that declare
    none.

    `options` (WHI-1548) holds the validated `algorithm_options.<name>` of this algorithm
    alone -- separate from `params`, never a sibling's -- and is empty for every factory
    without an `AlgorithmFactory.options_validator`."""

    name: str
    params: Mapping[str, Any] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Capabilities:
    """What plan shapes an algorithm can produce (docs/DESIGN.md §2.6 "Capability"
    column; §2.10: capability declarations determine `unsupported`). `multi_hop`: a
    plan may chain several pools; `split`: a plan may divide the input across
    several routes; `shared_pools`: several routes of one plan may share a physical
    pool and split/merge at intermediate tokens (`incremental_graph`, WHI-1441) -- an
    expanded topology, so its results are reported apart from the capability-matched
    pool-disjoint comparison (docs/DESIGN.md §2.11). `protocols`: the SOR route protocols
    whose pools the algorithm can route through (`uni_sor_port`: `("V2", "V3")`, WHI-1444;
    other pools never enter its candidates), or `None` for every admitted pool."""

    multi_hop: bool
    split: bool
    shared_pools: bool = False
    protocols: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "multi_hop": self.multi_hop,
            "split": self.split,
            "shared_pools": self.shared_pools,
        }
        if self.protocols is not None:
            out["protocols"] = list(self.protocols)
        return out


SINGLE_POOL = Capabilities(multi_hop=False, split=False)


class SolveFn(Protocol):
    def __call__(self, case: Case, context: SolveContext, budget: Budget) -> SolveResult: ...


class PrepareFn(Protocol):
    def __call__(self, bundle: SnapshotBundle, config: AlgorithmConfig) -> Any: ...


# `(options) -> complete normalized options`, raising `OptionsError` naming the key.
OptionsValidator = Callable[[Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class AlgorithmFactory:
    """`prepare(bundle, config) -> PreparedAlgorithm` (docs/DESIGN.md §4.3) plus
    `solve`. `prepare` is optional; without it the worker still measures and
    reports a (near-zero) preparation step so the cost is never hidden. Whatever
    `prepare` returns must be treated as immutable by `solve`."""

    name: str
    solve: SolveFn
    prepare: PrepareFn | None = None
    capabilities: Capabilities = SINGLE_POOL
    # Profile `search.*` keys this algorithm requires. The profile loader rejects a
    # profile that lists the algorithm without declaring every one of them
    # (docs/DESIGN.md §2.12: no invented defaults) and hands them to `prepare` as
    # `AlgorithmConfig.params`.
    search_params: tuple[str, ...] = ()
    # Profile `graph.*` keys this algorithm requires (docs/DESIGN.md §2.12
    # `graph.chunks`; WHI-1441), validated and handed over exactly like `search_params`.
    graph_params: tuple[str, ...] = ()
    # Source pin / scope / adapted-provider record of a ported algorithm (docs/DESIGN.md
    # §2.11: "algorithm/source pins and all deviations"; `uni_sor_port`, WHI-1444),
    # copied into the run's resolved profile. JSON-serializable; `None` for native ones.
    provenance: Mapping[str, Any] | None = None
    # Profile `shortlist.*` keys (the pre-registered candidate-shortlist settings of the
    # opt-in `uni_sor_fast` experiment, WHI-1508), validated and handed over exactly like
    # `search_params`. Empty for every reference algorithm.
    shortlist_params: tuple[str, ...] = ()
    # Profile `sampling.*` keys (the adaptive percentage-sampling settings of the same
    # opt-in experiment, WHI-1509). An optional all-or-none group: handed over only when
    # the profile declares the section (the loader then requires every key); without it
    # nothing is passed and the algorithm keeps its non-sampling behaviour. Empty for
    # every reference algorithm.
    sampling_params: tuple[str, ...] = ()
    # A named optimized strategy (WHI-1528): the identity of the registered recipe its
    # settings come from (`path`, `sha256`, `key`, `version`, `arm` of a frozen arms file).
    # The profile loader requires `strategies.<name>` to declare exactly that recipe's
    # settings, and hands them over as `AlgorithmConfig.params`; the global `shortlist` /
    # `sampling` sections never reach such a factory. `None` for every other algorithm.
    strategy_recipe: Mapping[str, Any] | None = None
    # WHI-1548 (R021-C/1 §9.2): the module-level validator of this algorithm's
    # `algorithm_options.<name>` keys/types/ranges. `None` = the algorithm accepts no
    # options (every pre-0.2.1 identity). Its `prepare` must call `validated_options`.
    options_validator: OptionsValidator | None = None
    # The pinned bounded comparison preset (`path`, `sha256`, `key`, `version` of a frozen
    # file holding only these options, §7.1), read only when the algorithm is selected;
    # `None` = no preset.
    options_preset: Mapping[str, Any] | None = None


# ------------------------------------------------------------------ WHI-1548 options

# Keys naming a shared profile setting (R021-C/1 §9.1): the profile's `search`, `graph`,
# `budget`, SOR-recipe, objective and seed values stay authoritative, so an option can never
# shadow one of them.
RESERVED_OPTION_KEYS = frozenset(
    {
        "max_hops", "max_splits", "percent_step", "chunks", "label_hops", "label_pruning",
        "time_limit_seconds", "max_quotes", "max_candidates", "shortlist", "sampling",
        "controls", "recipe", "objective", "seed",
    }
)  # fmt: skip


class OptionsError(ValueError):
    """`algorithm_options` that are unknown, reserved, mistyped, out of range or non-finite."""


def settings_sha256(options: Mapping[str, Any]) -> str:
    """The R021-C/1 §3.1 canonical hash (`json.dumps(sort_keys=True)`) of normalized options.
    Any mapping (e.g. the read-only `AlgorithmConfig.options`) hashes as its plain JSON copy;
    a non-JSON value is an `OptionsError`."""
    plain = _json_copy(options, "options")
    return hashlib.sha256(json.dumps(plain, sort_keys=True).encode()).hexdigest()


def _shown(value: Any) -> str:
    """`repr(value)` for an error message; an int too long to print is summarized."""
    try:
        return repr(value)
    except ValueError:  # int exceeds sys.get_int_max_str_digits()
        return f"<int of {value.bit_length()} bits>"


def _json_copy(value: Any, where: str) -> Any:
    """A plain JSON-shaped copy (mappings -> dict, lists -> list) of `value`, refusing
    non-string keys, non-finite numbers and anything that is not a JSON value."""
    if isinstance(value, float) and not math.isfinite(value):
        raise OptionsError(f"{where}: expected a finite number, got {value!r}")
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise OptionsError(f"{where}: option keys must be strings, got {key!r}")
            out[key] = _json_copy(item, f"{where}.{key}")
        return out
    if isinstance(value, list):
        return [_json_copy(item, f"{where}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, int) and _shown(value).startswith("<int"):
        raise OptionsError(f"{where}: integer too large to serialize, got {_shown(value)}")
    if value is None or isinstance(value, str | int | float):
        return value
    raise OptionsError(f"{where}: expected a JSON value, got {type(value).__name__}")


def validated_options(factory: AlgorithmFactory, options: Any) -> dict[str, Any]:
    """The complete normalized `options` of `factory` (a fresh, JSON-shaped dict), or
    `OptionsError`. Called by the profile loader and by the factory's own `prepare`."""
    where = f"algorithm_options.{factory.name}"
    if not isinstance(options, Mapping):
        raise OptionsError(f"{where}: expected a mapping, got {type(options).__name__}")
    if factory.options_validator is None:
        refuse_options(AlgorithmConfig(factory.name, options=options))
        return {}
    plain = _json_copy(options, where)
    reserved = sorted(set(plain) & RESERVED_OPTION_KEYS)
    if reserved:
        raise OptionsError(
            f"{where}: {reserved} name shared profile settings (search/graph/budget/recipe/"
            "objective/seed), which stay authoritative and cannot be set per strategy"
        )
    try:
        out = factory.options_validator(plain)
    except (ValueError, TypeError, KeyError) as exc:  # OptionsError is a ValueError
        raise OptionsError(f"{where}: {exc}") from exc
    if not isinstance(out, dict):
        raise OptionsError(f"{where}: the validator must return a dict")
    copy: dict[str, Any] = _json_copy(out, where)
    return copy


def refuse_options(config: AlgorithmConfig) -> None:
    """The `prepare` guard of a factory without an `options_validator` (every pre-0.2.1
    identity): explicit options are refused, never silently ignored. Empty options -- the
    only ones the profile loader ever hands such a factory -- change nothing."""
    if config.options:
        raise OptionsError(
            f"algorithm_options.{config.name}: {config.name!r} accepts no algorithm_options"
        )


def require_option_keys(
    options: Mapping[str, Any],
    required: frozenset[str] | set[str],
    optional: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Refuse missing `required` and unknown keys (validator helper)."""
    missing = sorted(required - options.keys())
    if missing:
        raise OptionsError(f"missing required key(s) {missing}")
    unknown = sorted(options.keys() - required - optional)
    if unknown:
        raise OptionsError(f"unknown key(s) {unknown}")


def option_int(value: Any, key: str, minimum: int, maximum: int | None = None) -> int:
    """An integer in `[minimum, maximum]`; a bool is never an integer."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        bound = f"in [{minimum}, {maximum}]" if maximum is not None else f">= {minimum}"
        raise OptionsError(f"{key}: expected an integer {bound}, got {_shown(value)}")
    return value


def option_number(value: Any, key: str, minimum: float, maximum: float | None = None) -> float:
    """A finite number in `[minimum, maximum]` (an int is accepted, a bool is not; an int too
    large for a float is not representable and refused like any other invalid value)."""
    number: float | None = None
    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            number = float(value)
        except OverflowError:
            number = None
    if (
        number is None
        or not math.isfinite(number)
        or number < minimum
        or (maximum is not None and number > maximum)
    ):
        bound = f"in [{minimum}, {maximum}]" if maximum is not None else f">= {minimum}"
        raise OptionsError(f"{key}: expected a finite number {bound}, got {_shown(value)}")
    return number


def option_choice(value: Any, key: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise OptionsError(f"{key}: expected one of {list(choices)}, got {_shown(value)}")
    return value

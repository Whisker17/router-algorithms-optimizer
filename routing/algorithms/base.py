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
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
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
    `AlgorithmFactory.graph_params` (e.g. `{"chunks": 20}`), empty for algorithms that
    declare none."""

    name: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Capabilities:
    """What plan shapes an algorithm can produce (docs/DESIGN.md §2.6 "Capability"
    column; §2.10: capability declarations determine `unsupported`). `multi_hop`: a
    plan may chain several pools; `split`: a plan may divide the input across
    several routes; `shared_pools`: several routes of one plan may share a physical
    pool and split/merge at intermediate tokens (`incremental_graph`, WHI-1441) -- an
    expanded topology, so its results are reported apart from the capability-matched
    pool-disjoint comparison (docs/DESIGN.md §2.11)."""

    multi_hop: bool
    split: bool
    shared_pools: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {"multi_hop": self.multi_hop, "split": self.split, "shared_pools": self.shared_pools}


SINGLE_POOL = Capabilities(multi_hop=False, split=False)


class SolveFn(Protocol):
    def __call__(self, case: Case, context: SolveContext, budget: Budget) -> SolveResult: ...


class PrepareFn(Protocol):
    def __call__(self, bundle: SnapshotBundle, config: AlgorithmConfig) -> Any: ...


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

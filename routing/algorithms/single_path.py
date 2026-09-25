"""`single_path` (docs/DESIGN.md §2.6): enumerate cycle-free paths up to the
configured hop bound and choose the objective-best **full-input** route. Multi-hop,
no split -- the whole order input flows through one chain of pools.

**Candidates.** `routing.search.enumerate_paths` over the structural token graph
(`prepare` builds the `GraphIndex` once per worker; the runner charges that cost as
the algorithm's preparation step). Order is hop-major, so every direct (1-hop) pool
is a candidate before any 2-hop path, and a declared cap only ever truncates the
longest paths: the best valid simpler route is always retained (§2.6). Parallel pools
on one pair are distinct candidates. The hop bound is the profile's `search.max_hops`
(§2.12: trial 3, profile-declared, no built-in default).

**Scoring.** Every candidate is a complete exact-input `RoutePlan`
(`routing.search.path_plan`) replayed by `routing.evaluator.evaluate` and scored with
`ObjectiveContext.score` -- real integer quotes only, never a spot-rate estimate. The
returned plan/evaluation is therefore the evaluator's own replay of exactly the
returned path, with its integer intermediate amounts in `evaluation.trace`/`funds`.
Candidates that share a prefix reuse the prefix's (pure, immutable) quote results
through a per-solve `routing.search.QuoteCache`; memoized calls never reach the
metered quote seam. A candidate whose evaluation failed at step `k` proves every
other candidate sharing its first `k+1` edges fails identically (same pools, same
original states, same amounts), so those are resolved as `pruned` without quoting.
Ties keep the first candidate found (fewer hops, then adjacency order).

**Budgets.** `Budget.max_candidates` caps the number of evaluated candidates;
`Budget.max_quotes` is the worker's hard meter, so the search keeps its own count of
executed quotes and stops *before* a candidate could exceed it (a candidate needing
`h` new quotes is only evaluated when at least `h` remain). Every candidate left
unevaluated by either limit is reported in `candidates_truncated`, and
`search_stats["truncated_by"]` names the limit. The time limit is enforced by the
runner; every new best plan is published through `SolveContext.report_candidate` so a
killed search keeps a labeled last valid candidate.

**Statuses** (never conflated):

- `ok` -- the best evaluated full-input route (possibly with declared truncation).
  Not claimed optimal: only the best of the *evaluated* bounded set.
- `no_route` -- the bounded search was complete and no candidate produced a valid
  plan, with a distinct reason: `token_out` is structurally unreachable, reachable
  only beyond `max_hops` (the hop cap), or every candidate within the cap failed.
- `timeout` -- a declared candidate/quote budget truncated the search before any
  valid route was found. A truncated search is not evidence of `no_route`.
- `incomplete_snapshot` -- no candidate produced a valid plan and at least one
  failed only because it needed state outside the collected range (§2.2: state-range
  exhaustion is not exhausted real liquidity, so this is never `no_route`).

**Incomplete candidates are excluded, not fatal** -- a deliberate difference from
`direct`, which makes the whole case `incomplete_snapshot`. `direct`'s candidates
all carry the case's own amount, which the corpus envelope is built to cover; a
multi-hop candidate carries arbitrary *intermediate* amounts that no finite envelope
can cover for every path, so one unevaluable detour would otherwise erase every
route of the case. Such candidates stay visible: `search_stats["paths_incomplete"]`
counts them (evaluated, or pruned behind an incomplete shared prefix) and
`search_stats["incomplete_example"]` names the first; an `ok` result is then the best
of the candidates that *could* be evaluated.
"""

from __future__ import annotations

from typing import Any

from pools.result import QuoteStatus
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    Capabilities,
    SolveContext,
    SolveResult,
    SolveStatus,
)
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import RoutePlan
from routing.search import (
    GraphIndex,
    Path,
    QuoteCache,
    build_graph_index,
    enumerate_paths,
    min_hops,
    path_label,
    path_plan,
)
from snapshot.models import Case, SnapshotBundle

NAME = "single_path"

CAPABILITIES = Capabilities(multi_hop=True, split=False)
SEARCH_PARAMS = ("max_hops",)


class SinglePathConfigError(ValueError):
    """`prepare` received an invalid or missing `search.max_hops`."""


class PreparedSinglePath:
    """Immutable per-worker preparation: the structural graph index and the hop
    bound. `solve` must never mutate it."""

    __slots__ = ("index", "max_hops")

    def __init__(self, index: GraphIndex, max_hops: int) -> None:
        self.index = index
        self.max_hops = max_hops


def prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> PreparedSinglePath:
    max_hops = config.params.get("max_hops")
    if not isinstance(max_hops, int) or isinstance(max_hops, bool) or max_hops < 1:
        raise SinglePathConfigError(
            f"{NAME} requires search.max_hops as an integer >= 1, got {max_hops!r}"
        )
    return PreparedSinglePath(index=build_graph_index(bundle), max_hops=max_hops)


def _new_quotes_needed(
    case: Case, path: Path, cache: QuoteCache, prefix_out: dict[Path, int]
) -> int:
    """Upper bound on the quotes evaluating `path` would execute: the steps after its
    longest already-evaluated prefix, less one if the next step's call is memoized.
    A zero intermediate amount makes every later step zero-input (no quote)."""
    k, amount = 0, case.amount_in
    for j in range(len(path) - 1, 0, -1):
        if path[:j] in prefix_out:
            k, amount = j, prefix_out[path[:j]]
            break
    if amount == 0:
        return 0
    nxt = path[k]
    return len(path) - k - (1 if cache.cached(nxt.pool_id, nxt.token_in, amount) else 0)


def solve(
    case: Case, context: SolveContext, budget: Budget, *, cache: QuoteCache | None = None
) -> SolveResult:
    """`cache` lets a composing solver (`path_split`) share one per-solve quote memo;
    the quote budget then counts every miss of that shared cache."""
    prepared = context.prepared
    if not isinstance(prepared, PreparedSinglePath):
        raise TypeError(f"{NAME}.solve needs the PreparedSinglePath returned by prepare()")
    objective = context.objective
    bundle = context.bundle
    max_hops = prepared.max_hops

    cache = QuoteCache(bundle) if cache is None else cache
    prefix_out: dict[Path, int] = {}  # evaluated prefix -> its integer output amount
    dead: dict[Path, str] = {}  # prefix whose last quote failed -> its quote status

    best: tuple[RoutePlan, Evaluation, int, Path] | None = None
    enumerated = evaluated = pruned = pruned_incomplete = truncated = 0
    truncated_by: str | None = None
    direct_candidates = 0
    failures: dict[str, int] = {}
    incomplete: list[str] = []

    for path in enumerate_paths(prepared.index, case.token_in, case.token_out, max_hops):
        enumerated += 1
        if len(path) == 1:
            direct_candidates += 1
        if truncated_by is not None:
            truncated += 1
            continue
        dead_reason = next((dead[path[:k]] for k in range(1, len(path)) if path[:k] in dead), None)
        if dead_reason is not None:
            pruned += 1
            if dead_reason == QuoteStatus.INCOMPLETE_SNAPSHOT.value:
                pruned_incomplete += 1
            continue
        if budget.max_candidates is not None and evaluated >= budget.max_candidates:
            truncated_by, truncated = "max_candidates", truncated + 1
            continue
        if budget.max_quotes is not None:
            if cache.misses + _new_quotes_needed(case, path, cache, prefix_out) > budget.max_quotes:
                truncated_by, truncated = "max_quotes", truncated + 1
                continue

        plan = path_plan(case, path)
        evaluation = evaluate(bundle, case, plan, objective, quote=cache)
        evaluated += 1
        for step in evaluation.trace:
            if step.status in (QuoteStatus.OK.value, "zero_input"):
                prefix_out[path[: step.step + 1]] = step.amount_out
            else:
                dead[path[: step.step + 1]] = step.status
        if evaluation.status is not EvalStatus.OK:
            last = (
                evaluation.trace[-1].status
                if evaluation.trace and evaluation.trace[-1].status != QuoteStatus.OK.value
                else "invalid_plan"
            )
            failures[last] = failures.get(last, 0) + 1
            if last == QuoteStatus.INCOMPLETE_SNAPSHOT.value:
                incomplete.append(f"{path_label(path)}: {evaluation.error}")
            continue
        score = objective.score(evaluation)
        if best is None or score > best[2]:
            best = (plan, evaluation, score, path)
            context.report_candidate(plan)

    stats: dict[str, Any] = {
        "max_hops": max_hops,
        "min_hops_unbounded": None,
        "paths_enumerated": enumerated,
        "direct_candidates": direct_candidates,
        "paths_evaluated": evaluated,
        "paths_pruned": pruned,
        "paths_truncated": truncated,
        "truncated_by": truncated_by,
        "quotes_executed": cache.misses,
        "quotes_memoized": cache.hits,
        "failed_candidates": dict(sorted(failures.items())),
        "paths_incomplete": len(incomplete) + pruned_incomplete,
        "incomplete_example": incomplete[0] if incomplete else None,
        "best_hops": None if best is None else len(best[3]),
    }
    common: dict[str, Any] = {
        "case_id": case.case_id,
        "algorithm": NAME,
        "candidates_considered": evaluated + pruned,
        "candidates_truncated": truncated,
        "search_stats": stats,
    }

    if best is not None:
        plan, evaluation, score, _ = best
        return SolveResult(
            status=SolveStatus.OK, plan=plan, evaluation=evaluation, score=score, **common
        )

    if truncated:
        return SolveResult(
            status=SolveStatus.TIMEOUT,
            error=(
                f"declared {truncated_by} budget truncated the search after {evaluated} "
                f"evaluated candidate(s) with no valid route; {truncated} candidate path(s) "
                "left unevaluated -- not evidence of no_route"
            ),
            **common,
        )

    if incomplete:
        return SolveResult(
            status=SolveStatus.INCOMPLETE_SNAPSHOT,
            error=(
                f"no valid route; {len(incomplete)} candidate path(s) need uncollected pool "
                f"state, e.g. {incomplete[0]}"
            ),
            **common,
        )

    if enumerated == 0:
        shortest = min_hops(prepared.index, case.token_in, case.token_out)
        stats["min_hops_unbounded"] = shortest
        if shortest is None:
            reason = f"{case.token_out} is unreachable from {case.token_in} in the pool graph"
        else:
            reason = (
                f"hop cap: the shortest path {case.token_in} -> {case.token_out} needs "
                f"{shortest} pools, above search.max_hops={max_hops}"
            )
        return SolveResult(status=SolveStatus.NO_ROUTE, error=reason, **common)

    return SolveResult(
        status=SolveStatus.NO_ROUTE,
        error=(
            f"every one of the {enumerated} cycle-free path(s) within max_hops={max_hops} "
            f"failed ({', '.join(f'{k}: {v}' for k, v in sorted(failures.items()))}; "
            f"{pruned} pruned by a failed shared prefix)"
        ),
        **common,
    )


FACTORY = AlgorithmFactory(
    name=NAME,
    solve=solve,
    prepare=prepare,
    capabilities=CAPABILITIES,
    search_params=SEARCH_PARAMS,
)

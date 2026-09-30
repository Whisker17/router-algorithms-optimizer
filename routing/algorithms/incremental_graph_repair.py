"""`incremental_graph_repair` (WHI-1554, R021-P08; contract R021-C/1 §2 row 3): the
complete `incremental_graph` incumbent plus a bounded checkpoint-and-suffix repair of its
own greedy chunk trace. Normative specification: `docs/references/research-021/
suffix-repair.md` §4-§8 (r2); its executable form is `repair_solve` in
`tests/routing/test_suffix_repair_contract.py`. Experimental and opt-in (`custom` group);
no optimality, improvement or latency claim.

**Stages** (one solve, one `QuoteCache`, one guarded quote check, one worker meter, one
wall clock; nothing is reset between stages or attempts, and there is no retry):

1. *Retained simpler candidate*: `path_split.solve` on the per-solve cache, exactly as
   `incremental_graph` (it keeps `single_path` / `direct_split` itself); published.
2. *Incumbent*: `incremental_graph`'s reference chunk loop (`graph_reuse` off) on its public
   `PoolFlow`, `chunk_amounts`, `creates_cycle` and `merged_plan`, recording a complete
   checkpoint before every committed decision. Chunks are search allocations on
   original-state aggregate quotes (`f(x + m) - f(x)`), never executed as sequential swaps.
   The merged plan is replayed in solve; it is a candidate only if the replay is `ok` **and**
   its gross equals the accounted gross (otherwise `incremental_status` is `invalid_plan` or
   `consistency_failure`, nothing is published and the repair never starts). It replaces the
   stage-1 plan only if strictly better (published). With `repair: false` the result is
   `incremental_graph`'s: plan, evaluation, status, counters, publications, metered quotes.
3. *Repair* (`repair: true`): structural checkpoints (decisions that added a token edge),
   latest first, at most `max_checkpoints`. Each is restored into **fresh** objects (never by
   subtracting chunks) and its decision's chunk is rescored with the reference rule and
   per-chunk `max_candidates` cap; the rescored first maximum must be the recorded decision
   (round-trip guard). Alternatives: every other admissible path with a positive marginal,
   by marginal descending then enumeration index, at most `alternatives_per_checkpoint`. Each
   attempt (at most `max_repair_attempts` per solve) forces one alternative as the first
   suffix choice and rebuilds the rest of the suffix with the unchanged reference greedy
   rule on the state the suffix itself builds. A candidate whose flow key (positive pool
   inputs and directions) equals the incumbent's or an earlier candidate's is a duplicate and
   is not replayed. Every other complete candidate is merged, replayed from the original
   snapshot on the reference evaluator (metered, on the guarded cache), checked `ok` and
   evaluated gross = accounted gross, and scored with the objective: strictly better
   replaces the best and is published; equal is a tie (the earlier incumbent stays); lower
   is `rejected_worse`.

**What a restore keeps and drops** (§4): kept -- the immutable inputs (bundle, case, chunk
grid, path enumeration, objective), the pure original-state quote memo and every monotone
counter; dropped -- the per-chunk prefix memo, every chunk score/ranking computed on another
committed state, any reuse closure (`incremental_graph._ExactReuse` is never used here). A
checkpoint holds the carry, every flow record in insertion order (zero-input records and
their `order` slots included), the committed token edges and the committed decisions.

**Fail closed** (§5.7, Proposition P2): a replay that is not `ok` or whose gross differs from
the accounted gross, a `merged_plan` refusal of a candidate or a failed round-trip guard is a
consistency failure: the repair stops, `search_stats["consistency_failure"]` records the
stage/replay index/amounts, and the result is the best plan validated before the fault, or
`algorithm_error` (never `ok`, `no_route` or `timeout`) when there is none. A corrupted score
is never published or returned.

**Accounting.** The whole solve runs inside `routing.evaluator.counted_evaluations()`:
`search_stats["evaluations"]` = `{fallback, incumbent, repair, total}` read at the stage
boundaries (every complete-plan `evaluate` call inside this solve, the embedded
`path_split`/`single_path`/`direct_split` replays included); the R021 unit
`internal_evaluations` is `total`; `repair.repair_evaluations` is the separately named
stage extra. The runner's own final evaluation is outside the solve. `quotes_executed` is
the cache's executed count (the worker meter). `search_stats["r021"]` is the
`r021.diagnostics/1` record: the §6 domain (identical with repair on and off), certificate
`null` / `not_produced`, unit `paths_scored_per_chunk`, the fallback label and the repair
object. A hard kill by the runner keeps only the last published (complete, replayed,
accepted) plan as `last_valid_candidate` with status `timeout`.

**Options** (`algorithm_options.incremental_graph_repair`, WHI-1548 seam, §7): `repair`
(bool), `max_checkpoints` (1..64), `alternatives_per_checkpoint` (1..16),
`max_repair_attempts` (1..256), all required after preset resolution, no solver defaults.
Bounded preset v1 (`PRESET`): `true / 4 / 2 / 8`; the repair-off control is the same values
with `repair: false`; the stress profile (`16 / 4 / 64`) runs only by explicit profile.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from pools.result import QuoteStatus, SwapResult
from routing.algorithms import incremental_graph, path_split
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
    option_int,
    require_option_keys,
    validated_options,
)
from routing.algorithms.incremental_graph import (
    PoolFlow,
    PreparedIncrementalGraph,
    chunk_amounts,
    creates_cycle,
    merged_plan,
    topology,
)
from routing.evaluator import (
    EvalStatus,
    Evaluation,
    EvaluationCounter,
    counted_evaluations,
    evaluate,
)
from routing.plan import RoutePlan
from routing.search import Edge, Path, QuoteCache, enumerate_paths, path_label
from snapshot.models import Case, PoolState, SnapshotBundle

NAME = "incremental_graph_repair"
REFERENCE = incremental_graph.NAME

CAPABILITIES = incremental_graph.CAPABILITIES
SEARCH_PARAMS = incremental_graph.SEARCH_PARAMS
GRAPH_PARAMS = incremental_graph.GRAPH_PARAMS

OPTION_RANGES: dict[str, tuple[int, int]] = {
    "max_checkpoints": (1, 64),
    "alternatives_per_checkpoint": (1, 16),
    "max_repair_attempts": (1, 256),
}
OPTION_KEYS = frozenset(("repair", *OPTION_RANGES))
# The bounded comparison preset (R021-C/1 §7.1, suffix-repair.md §7), frozen by its bytes.
PRESET: dict[str, Any] = {
    "path": "config/incremental_graph_repair/preset_v1.yaml",
    "sha256": "13774bcd2225ee17d3635bae509ac2a5a888d5c2801006a200e01788ca35786a",
    "key": "R021-P08-incremental_graph_repair",
    "version": 1,
}


class IncrementalGraphRepairConfigError(ValueError):
    """`prepare` received an invalid or missing `search.*`/`graph.*` value."""


def validate_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """The `options_validator` (suffix-repair.md §7): all four keys required, unknown keys
    refused (reserved keys are refused by `validated_options` first), `repair` a real bool,
    the caps integers (never bools) in range."""
    require_option_keys(options, set(OPTION_KEYS))
    repair = options["repair"]
    if not isinstance(repair, bool):
        raise ValueError(f"repair: expected a boolean, got {repair!r}")
    out: dict[str, Any] = {"repair": repair}
    for key, (lo, hi) in OPTION_RANGES.items():
        out[key] = option_int(options[key], key, lo, hi)
    return out


class PreparedRepair:
    """Immutable per-worker preparation: `incremental_graph`'s prepared object and the
    validated, read-only options."""

    __slots__ = ("graph", "options")

    def __init__(self, graph: PreparedIncrementalGraph, options: Mapping[str, Any]) -> None:
        self.graph = graph
        self.options = options


def prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> PreparedRepair:
    options = validated_options(FACTORY, config.options)  # the public entry validates too
    try:  # the reused legacy prepare refuses options: it gets the same config without them
        graph = incremental_graph.prepare(bundle, dataclasses.replace(config, options={}))
    except incremental_graph.IncrementalGraphConfigError as exc:
        raise IncrementalGraphRepairConfigError(f"{NAME}: {exc}") from exc
    return PreparedRepair(graph, MappingProxyType(options))


# ------------------------------------------------------------------ checkpoint state


@dataclasses.dataclass(frozen=True)
class FlowRecord:
    """One committed `PoolFlow`, frozen (pool, used direction, aggregate in/out, order)."""

    pool_id: str
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int
    order: int


@dataclasses.dataclass(frozen=True)
class Decision:
    """One committed chunk: position, amount (carry included), path index and path, its
    marginal and the token edges it added (non-empty = structural)."""

    position: int
    amount: int
    path_index: int
    path: Path
    marginal: int
    new_edges: tuple[tuple[str, str], ...]


@dataclasses.dataclass(frozen=True)
class Checkpoint:
    """The complete committed prefix immediately before decision `len(decisions)`."""

    position: int
    carry: int
    flows: tuple[FlowRecord, ...]
    token_edges: frozenset[tuple[str, str]]
    decisions: tuple[Decision, ...]


EMPTY = Checkpoint(0, 0, (), frozenset(), ())


def freeze(
    position: int,
    carry: int,
    flows: Mapping[str, PoolFlow],
    edges: set[tuple[str, str]],
    decisions: Sequence[Decision],
) -> Checkpoint:
    records = tuple(
        FlowRecord(
            f.edge.pool_id, f.edge.token_in, f.edge.token_out, f.amount_in, f.amount_out, f.order
        )
        for f in flows.values()
    )
    return Checkpoint(position, carry, records, frozenset(edges), tuple(decisions))


def restore(cp: Checkpoint) -> tuple[dict[str, PoolFlow], set[tuple[str, str]], list[Decision]]:
    """Fresh mutable state equal to `cp`: new `PoolFlow` objects in the recorded insertion
    order, a new edge set, a new decision list. Nothing is subtracted."""
    flows = {
        r.pool_id: PoolFlow(
            Edge(r.pool_id, r.token_in, r.token_out), r.amount_in, r.amount_out, r.order
        )
        for r in cp.flows
    }
    return flows, set(cp.token_edges), list(cp.decisions)


def flow_key(flows: Mapping[str, PoolFlow]) -> frozenset[tuple[str, str, str, int]]:
    """A candidate's identity (§5.4): its positive pool inputs and directions."""
    return frozenset(
        (f.edge.pool_id, f.edge.token_in, f.edge.token_out, f.amount_in)
        for f in flows.values()
        if f.amount_in > 0
    )


def accounted_gross(case: Case, flows: Mapping[str, PoolFlow]) -> int:
    return sum(f.amount_out for f in flows.values() if f.edge.token_out == case.token_out)


# ------------------------------------------------------------------ the reference loop


class _BudgetExhausted(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class _Failure:
    reason: str
    prefix: int
    note: str | None = None


class _ChunkFailed(Exception):
    def __init__(self, failure: _Failure) -> None:
        super().__init__(failure.reason)
        self.failure = failure


@dataclasses.dataclass(frozen=True)
class Scored:
    """One admissible, successfully scored path of a chunk on one committed state."""

    marginal: int
    index: int
    path: Path
    updates: tuple[PoolFlow, ...]


@dataclasses.dataclass
class Counters:
    """One stage's search counters (the incumbent stage's are `incremental_graph`'s)."""

    scored: int = 0
    rejected_cycle: int = 0
    truncated: int = 0
    carried: int = 0
    failures: dict[str, int] = dataclasses.field(default_factory=dict)
    incomplete: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Run:
    """The reference loop from one checkpoint: final state, decisions, the per-decision
    checkpoints (incumbent trace only) and `complete` / `chunk_<k>_no_admissible_path`."""

    status: str
    flows: dict[str, PoolFlow]
    edges: set[tuple[str, str]]
    decisions: list[Decision]
    checkpoints: list[Checkpoint]


class _Search:
    """`incremental_graph.solve`'s reference marginal rule and chunk loop (default
    `graph_reuse=False`), parameterized by the committed state it runs on."""

    def __init__(
        self,
        bundle: SnapshotBundle,
        paths: list[Path],
        amounts: list[int],
        guarded: Callable[[PoolState, str, int], SwapResult[PoolState]],
        budget: Budget,
    ) -> None:
        self.bundle, self.paths, self.amounts = bundle, paths, amounts
        self.guarded, self.budget = guarded, budget
        self.last = max((k for k, a in enumerate(amounts) if a > 0), default=-1)
        self.cap_hit = False
        self.live: list[Decision] = []  # the running loop's decisions (read after a cut)

    def marginal(
        self,
        flows: Mapping[str, PoolFlow],
        path: Path,
        amount: int,
        memo: dict[Path, Any],
        counters: Counters,
    ) -> tuple[int, list[PoolFlow]]:
        m, updates = amount, list[PoolFlow]()
        for i, edge in enumerate(path):
            key = path[: i + 1]
            if key in memo:
                hit = memo[key]
                if isinstance(hit, _Failure):
                    raise _ChunkFailed(hit)
                m, update = hit
                updates.append(update)
                continue
            prev = flows.get(edge.pool_id)
            x, out = (prev.amount_in, prev.amount_out) if prev else (0, 0)
            order = prev.order if prev else len(flows)
            if m == 0:  # zero marginal input: nothing changes, no pool call
                update = PoolFlow(edge, x, out, order)
            else:
                r = self.guarded(self.bundle.pools[edge.pool_id], edge.token_in, x + m)
                reason: str | None = None
                note: str | None = None
                if r.status is not QuoteStatus.OK or r.amount_in_consumed != x + m:
                    reason = r.status.value if r.status is not QuoteStatus.OK else "partial_fill"
                    if r.status is QuoteStatus.INCOMPLETE_SNAPSHOT:
                        note = f" @ {x + m}: {r.detail}"
                elif r.amount_out < out:
                    reason = "nonmonotone"
                if reason is not None:
                    failure = _Failure(reason, i + 1, note)
                    memo[key] = failure
                    counters.failures[reason] = counters.failures.get(reason, 0) + 1
                    if note is not None:
                        counters.incomplete.append(path_label(path) + note)
                    raise _ChunkFailed(failure)
                update = PoolFlow(edge, x + m, r.amount_out, order)
                m = r.amount_out - out
            memo[key] = (m, update)
            updates.append(update)
        return m, updates

    def score(
        self,
        flows: Mapping[str, PoolFlow],
        edges: set[tuple[str, str]],
        amount: int,
        counters: Counters,
    ) -> list[Scored]:
        """Every admissible successfully scored path of one chunk on `(flows, edges)`, in
        enumeration order, under the per-chunk `max_candidates` cap, with a fresh memo."""
        memo: dict[Path, Any] = {}
        out: list[Scored] = []
        chunk_scored = 0
        for j, path in enumerate(self.paths):
            cap = self.budget.max_candidates
            if cap is not None and chunk_scored >= cap:
                self.cap_hit = True
                counters.truncated += len(self.paths) - j
                break
            if creates_cycle(edges, path):
                counters.rejected_cycle += 1
                continue
            chunk_scored += 1
            try:
                m, updates = self.marginal(flows, path, amount, memo, counters)
            except _ChunkFailed:
                continue
            out.append(Scored(m, j, path, tuple(updates)))
        counters.scored += chunk_scored
        return out

    def run(
        self,
        start: Checkpoint,
        counters: Counters,
        forced: Scored | None = None,
        trace: bool = False,
    ) -> Run:
        """The reference loop from `start`; `forced` replaces the first decision and every
        later one is the reference's first maximum on the state the suffix builds."""
        flows, edges, decisions = restore(start)
        self.live = decisions
        carry = start.carry
        checkpoints: list[Checkpoint] = []
        for k in range(start.position, len(self.amounts)):
            chunk = self.amounts[k]
            if chunk == 0:
                continue
            amount = carry + chunk
            choice: Scored | None
            if forced is not None and k == start.position:
                choice = forced
            else:
                choice = first_max(self.score(flows, edges, amount, counters))
            if k != self.last and (choice is None or choice.marginal == 0):
                carry = amount
                counters.carried += 1
                continue
            if choice is None:
                return Run(
                    f"chunk_{k + 1}_no_admissible_path", flows, edges, decisions, checkpoints
                )
            if trace:
                checkpoints.append(freeze(k, carry, flows, edges, decisions))
            carry = 0
            new_edges = tuple(
                dict.fromkeys(
                    (e.token_in, e.token_out)
                    for e in choice.path
                    if (e.token_in, e.token_out) not in edges
                )
            )
            for update in choice.updates:
                flows[update.edge.pool_id] = update
            edges.update((e.token_in, e.token_out) for e in choice.path)
            decisions.append(
                Decision(k, amount, choice.index, choice.path, choice.marginal, new_edges)
            )
        return Run("complete", flows, edges, decisions, checkpoints)


def first_max(scored: Sequence[Scored]) -> Scored | None:
    """The reference choice: the first maximal marginal (ties keep the earlier path)."""
    return min(scored, key=lambda s: (-s.marginal, s.index), default=None)


def structural_checkpoints(run: Run, limit: int) -> list[int]:
    """Decisions that added a token edge, latest first, at most `limit`."""
    return [i for i in reversed(range(len(run.decisions))) if run.decisions[i].new_edges][:limit]


def alternatives(decision: Decision, scored: Sequence[Scored], limit: int) -> list[Scored]:
    """Other admissible paths with a positive marginal, by marginal then index."""
    ranked = sorted(scored, key=lambda s: (-s.marginal, s.index))
    return [s for s in ranked if s.index != decision.path_index and s.marginal > 0][:limit]


def _consistency(
    stage: str, index: int, accounted: int, ev: Evaluation | None, detail: str | None = None
) -> dict[str, Any]:
    return {
        "stage": stage,
        "replay_index": index,
        "accounted_gross": str(accounted),
        "evaluation_status": None if ev is None else ev.status.value,
        "evaluated_gross": None if ev is None else str(ev.gross_output),
        "detail": detail,
    }


# ------------------------------------------------------------------ solve

# The current best validated candidate: source, plan, evaluation, score, its route paths.
_Best = tuple[str, RoutePlan, Evaluation, int, list[Path]]


def solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    prepared = context.prepared
    if not isinstance(prepared, PreparedRepair):
        raise TypeError(f"{NAME}.solve needs the PreparedRepair returned by prepare()")
    with counted_evaluations() as evaluations:  # the whole solve: every stage's replays
        return _solve(case, context, budget, prepared, evaluations)


def _solve(
    case: Case,
    context: SolveContext,
    budget: Budget,
    prepared: PreparedRepair,
    evaluations: EvaluationCounter,
) -> SolveResult:
    graph, opts = prepared.graph, prepared.options
    bundle, objective = context.bundle, context.objective
    ps_prepared = graph.path_split
    max_hops = ps_prepared.single_path.max_hops
    cache = QuoteCache(bundle)
    quiet = dataclasses.replace(context, candidate_sink=None)

    # ---- stage 1: the retained simpler candidates (path_split on the shared cache).
    ps = path_split.solve(
        case, dataclasses.replace(quiet, prepared=ps_prepared), budget, cache=cache
    )
    best: _Best | None = None
    if ps.status is SolveStatus.OK and ps.plan and ps.evaluation and ps.score is not None:
        best = (
            str(ps.search_stats.get("chosen_source") or path_split.NAME),
            ps.plan,
            ps.evaluation,
            ps.score,
            incremental_graph._source_paths(ps.evaluation),
        )
        context.report_candidate(ps.plan)
    after_fallback = evaluations.count

    # ---- stage 2: the incumbent (the reference loop, traced).
    truncated_by: str | None = None

    def guarded(state: PoolState, token_in: str, amount: int) -> SwapResult[PoolState]:
        nonlocal truncated_by
        memo_hit = bundle.pools.get(state.pool_id) is state and cache.cached(
            state.pool_id, token_in, amount
        )
        if budget.max_quotes is not None and cache.misses >= budget.max_quotes and not memo_hit:
            truncated_by = "max_quotes"
            raise _BudgetExhausted
        return cache(state, token_in, amount)

    paths = list(
        enumerate_paths(ps_prepared.single_path.index, case.token_in, case.token_out, max_hops)
    )
    amounts = chunk_amounts(case.amount_in, graph.chunks)
    search = _Search(bundle, paths, amounts, guarded, budget)
    inc = Counters()
    incremental_status = "not_run"
    incremental: tuple[RoutePlan, Evaluation, int] | None = None
    accounted: int | None = None
    run: Run | None = None
    budget_cut = 0
    consistency: dict[str, Any] | None = None
    try:
        if not paths:
            incremental_status = "no_paths"
        else:
            run = search.run(EMPTY, inc, trace=True)
            if run.status != "complete":
                incremental_status = run.status
            else:
                accounted = accounted_gross(case, run.flows)
                plan = merged_plan(case, run.flows.values())
                ev = evaluate(bundle, case, plan, objective, quote=guarded)
                if ev.status is EvalStatus.OK and ev.gross_output == accounted:
                    incremental_status = "ok"
                    incremental = (plan, ev, objective.score(ev))
                else:  # never replaced or published: the replay contradicts the accounting
                    if ev.status is EvalStatus.OK:
                        incremental_status = "consistency_failure"
                    else:
                        incremental_status = "invalid_plan"
                        inc.failures["invalid_plan"] = inc.failures.get("invalid_plan", 0) + 1
                    consistency = _consistency("incumbent", 1, accounted, ev)
    except _BudgetExhausted:
        incremental_status = "truncated"
        budget_cut = 1
    if search.cap_hit:
        truncated_by = truncated_by or "max_candidates"
    stage2_truncated = truncated_by is not None
    incumbent_decisions = list(search.live)  # committed incumbent chunks (also after a cut)
    after_incumbent = evaluations.count
    if incremental is not None and (best is None or incremental[2] > best[3]):
        plan, ev, score = incremental
        best = (REFERENCE, plan, ev, score, [d.path for d in incumbent_decisions])
        context.report_candidate(incremental[0])

    # ---- stage 3: bounded structural suffix repair (same cache, meter and budget).
    ledger = _Ledger(opts)
    rep = ledger.counters
    repair_truncated = False
    if not opts["repair"]:
        ledger.stop = "disabled"
    elif incremental_status == "truncated":
        ledger.stop = "quote_budget"
    elif consistency is not None:
        ledger.consistency_failures += 1
        ledger.stop = "consistency_failure"
    elif run is None or not run.decisions:
        ledger.stop = "no_trace"
    else:
        search.cap_hit = False
        best, consistency = _repair(case, context, search, run, ledger, best, guarded)
        repair_truncated = ledger.stop == "quote_budget"
        if search.cap_hit:
            repair_truncated = True
            truncated_by = truncated_by or "max_candidates"
    total = evaluations.count

    # ---- the record (stage 1-2 keys exactly as incremental_graph's).
    by_path: dict[Path, list[int]] = {}
    for d in incumbent_decisions:
        entry = by_path.setdefault(d.path, [0, 0])
        entry[0] += 1
        entry[1] += d.amount
    shared: dict[str, int] = {}
    for path in by_path:
        for e in path:
            shared[e.pool_id] = shared.get(e.pool_id, 0) + 1
    ps_truncated = ps.search_stats.get("truncated_by")
    stats: dict[str, Any] = {
        "chunks": graph.chunks,
        "chunks_empty": sum(a == 0 for a in amounts),
        "chunks_carried": inc.carried,
        "chunks_allocated": len(incumbent_decisions),
        "max_hops": max_hops,
        "max_splits": ps_prepared.direct_split.max_splits,
        "percent_step": ps_prepared.direct_split.percent_step,
        "paths_enumerated": len(paths),
        "paths_scored": inc.scored,
        "paths_rejected_cycle": inc.rejected_cycle,
        "paths_truncated": inc.truncated + budget_cut,
        "marginal_failures": dict(sorted(inc.failures.items())),
        "marginal_incomplete": len(inc.incomplete),
        "incomplete_example": inc.incomplete[0] if inc.incomplete else None,
        "incremental_status": incremental_status,
        "incremental_allocation": [
            {"path": path_label(p), "chunks": n, "amount_in": str(a)}
            for p, (n, a) in by_path.items()
        ],
        "incremental_chunk_sequence": [list(by_path).index(d.path) for d in incumbent_decisions],
        "incremental_shared_pools": sorted(p for p, n in shared.items() if n > 1),
        "incremental_topology": topology(by_path) if by_path else None,
        "incremental_accounted_gross": None if accounted is None else str(accounted),
        "incremental_evaluated_gross": (
            None if incremental is None else str(incremental[1].gross_output)
        ),
        "incremental_score": None if incremental is None else str(incremental[2]),
        "accounting_matches_evaluation": (
            None if incremental is None else accounted == incremental[1].gross_output
        ),
        "path_split_status": ps.status.value,
        "path_split_score": None if ps.score is None else str(ps.score),
        "path_split_source": ps.search_stats.get("chosen_source"),
        "single_path_score": ps.search_stats.get("single_path_score"),
        "direct_split_score": ps.search_stats.get("direct_split_score"),
        "truncated_by": truncated_by or ps_truncated,
        "truncated_stages": ps.search_stats.get("truncated_stages", [])
        + ([REFERENCE] if stage2_truncated else [])
        + ([NAME] if repair_truncated else []),
        "quotes_executed": cache.misses,
        "quotes_memoized": cache.hits,
        "chosen_source": None,
        "topology": None,
        "repair": ledger.to_dict(),
        "evaluations": {
            "fallback": after_fallback,
            "incumbent": after_incumbent - after_fallback,
            "repair": total - after_incumbent,
            "total": total,
        },
        "consistency_failure": consistency,
    }
    common: dict[str, Any] = {
        "case_id": case.case_id,
        "algorithm": NAME,
        "candidates_considered": ps.candidates_considered + inc.scored + rep.scored,
        "candidates_truncated": ps.candidates_truncated
        + inc.truncated
        + budget_cut
        + rep.truncated,
        "search_stats": stats,
    }
    if best is not None:
        source, plan, ev, score, route_paths = best
        stats["chosen_source"] = source
        stats["topology"] = topology(route_paths)
        stats["r021"] = _diagnostics(bundle, graph, stats, source)
        return SolveResult(status=SolveStatus.OK, plan=plan, evaluation=ev, score=score, **common)
    stats["r021"] = _diagnostics(bundle, graph, stats, None)
    if consistency is not None:  # no plan was validated before the fault
        return SolveResult(
            status=SolveStatus.ALGORITHM_ERROR,
            error=(
                f"consistency_failure in {consistency['stage']} replay "
                f"{consistency['replay_index']}: evaluated {consistency['evaluated_gross']} "
                f"!= accounted {consistency['accounted_gross']}; no validated plan"
            ),
            **common,
        )
    if truncated_by is not None or ps.status is SolveStatus.TIMEOUT:
        return SolveResult(
            status=SolveStatus.TIMEOUT,
            error=(
                f"declared {stats['truncated_by']} budget truncated the search with no valid "
                "route -- not evidence of no_route"
            ),
            **common,
        )
    if inc.incomplete or ps.status is SolveStatus.INCOMPLETE_SNAPSHOT:
        example = inc.incomplete[0] if inc.incomplete else ps.error
        return SolveResult(
            status=SolveStatus.INCOMPLETE_SNAPSHOT,
            error=f"no valid route; candidates need uncollected pool state, e.g. {example}",
            **common,
        )
    return SolveResult(status=SolveStatus.NO_ROUTE, error=ps.error, **common)


class _Ledger:
    """The repair stage's counters (§8), all monotone within the solve."""

    def __init__(self, opts: Mapping[str, Any]) -> None:
        self.opts = opts
        self.stop = "not_run"
        self.checkpoint_restores = 0
        self.repair_attempts = 0
        self.candidates_complete = 0
        self.candidates_failed = 0
        self.duplicates = 0
        self.rejected_worse = 0
        self.ties = 0
        self.accepted = 0
        self.consistency_failures = 0
        self.repair_evaluations = 0  # candidate replays only: a stage extra, not the total
        self.counters = Counters()
        self.accepted_log: list[dict[str, Any]] = []
        # Per attempt, in order: checkpoint, alternative path index, outcome (not emitted in
        # the record; read by tests).
        self.attempts: list[dict[str, Any]] = []

    def to_dict(self) -> dict[str, Any]:
        rep = self.counters
        return {
            "enabled": self.opts["repair"],
            "stop": self.stop,
            "checkpoint_restores": self.checkpoint_restores,
            "repair_attempts": self.repair_attempts,
            "candidates_complete": self.candidates_complete,
            "candidates_failed": self.candidates_failed,
            "duplicates": self.duplicates,
            "rejected_worse": self.rejected_worse,
            "ties": self.ties,
            "accepted": self.accepted,
            "consistency_failures": self.consistency_failures,
            "repair_evaluations": self.repair_evaluations,
            "paths_scored": rep.scored,
            "paths_rejected_cycle": rep.rejected_cycle,
            "paths_truncated": rep.truncated,
            "marginal_failures": dict(sorted(rep.failures.items())),
            "accepted_log": [dict(a) for a in self.accepted_log],
            "attempts": [dict(a) for a in self.attempts],
        }


def _repair(
    case: Case,
    context: SolveContext,
    search: _Search,
    run: Run,
    ledger: _Ledger,
    best: _Best | None,
    guarded: Callable[[PoolState, str, int], SwapResult[PoolState]],
) -> tuple[_Best | None, dict[str, Any] | None]:
    """Stage 3 (§5.2-§5.5). Returns the (possibly replaced) best and the consistency
    provenance, if a fault stopped it. The declared quote budget (`_BudgetExhausted`) stops it
    with `quote_budget` and still returns the best so far, including a candidate accepted
    (and published) before the cut. The worker's hard meter (`QuoteLimitExceeded`) is not
    caught: a hard kill keeps only the runner's `last_valid_candidate`."""
    bundle, objective, opts = context.bundle, context.objective, ledger.opts
    rep = ledger.counters
    seen = {flow_key(run.flows)} if run.status == "complete" else set()
    ledger.stop = "complete"
    try:  # a declared quote cut keeps the best so far (it may be an accepted candidate)
        for i in structural_checkpoints(run, opts["max_checkpoints"]):
            cp = run.checkpoints[i]
            ledger.checkpoint_restores += 1
            decision = run.decisions[i]
            flows, edges, _ = restore(cp)
            scored = search.score(flows, edges, decision.amount, rep)
            top = first_max(scored)
            if top is None or (top.index, top.marginal) != (decision.path_index, decision.marginal):
                ledger.consistency_failures += 1
                ledger.stop = "consistency_failure"
                detail = f"checkpoint {i} rescored to {None if top is None else top.index}"
                return best, _consistency(
                    "repair_restore",
                    ledger.checkpoint_restores,
                    accounted_gross(case, run.flows),
                    None,
                    detail,
                )
            for alt in alternatives(decision, scored, opts["alternatives_per_checkpoint"]):
                if ledger.repair_attempts >= opts["max_repair_attempts"]:
                    ledger.stop = "attempt_cap"
                    return best, None
                ledger.repair_attempts += 1
                record: dict[str, Any] = {"checkpoint": i, "alternative": alt.index}
                ledger.attempts.append(record)
                record["outcome"] = "budget_cut"  # replaced below unless the quote budget ends it
                cand = search.run(cp, rep, forced=alt)
                if cand.status != "complete":
                    record["outcome"] = "failed"
                    ledger.candidates_failed += 1
                    continue
                ledger.candidates_complete += 1
                key = flow_key(cand.flows)
                if key in seen:
                    record["outcome"] = "duplicate"
                    ledger.duplicates += 1
                    continue
                seen.add(key)
                accounted = accounted_gross(case, cand.flows)
                try:
                    plan = merged_plan(case, cand.flows.values())
                except ValueError as exc:
                    record["outcome"] = "consistency_failure"
                    ledger.consistency_failures += 1
                    ledger.stop = "consistency_failure"
                    return best, _consistency(
                        "repair", ledger.repair_evaluations + 1, accounted, None, str(exc)
                    )
                ledger.repair_evaluations += 1
                ev = evaluate(bundle, case, plan, objective, quote=guarded)
                if ev.status is not EvalStatus.OK or ev.gross_output != accounted:
                    record["outcome"] = "consistency_failure"
                    ledger.consistency_failures += 1
                    ledger.stop = "consistency_failure"
                    return best, _consistency("repair", ledger.repair_evaluations, accounted, ev)
                score = objective.score(ev)
                record["score"] = str(score)
                if best is not None and score < best[3]:
                    record["outcome"] = "rejected_worse"
                    ledger.rejected_worse += 1
                elif best is not None and score == best[3]:
                    record["outcome"] = "tie"
                    ledger.ties += 1
                else:
                    record["outcome"] = "accepted"
                    ledger.accepted += 1
                    best = (NAME, plan, ev, score, [d.path for d in cand.decisions])
                    ledger.accepted_log.append(
                        {"checkpoint": i, "alternative": alt.index, "score": str(score)}
                    )
                    context.report_candidate(plan)
    except _BudgetExhausted:  # the cut candidate is abandoned; no retry, nothing reset
        ledger.stop = "quote_budget"
    return best, None


# ------------------------------------------------------------------ R021 diagnostics


def domain(bundle: SnapshotBundle, graph: PreparedIncrementalGraph) -> dict[str, Any]:
    """suffix-repair.md §6: the incremental incumbent's and every repair candidate's
    feasible set (`incremental_graph`'s); identical with repair on and off."""
    pools = list(bundle.pools)
    max_hops = graph.path_split.single_path.max_hops
    return {
        "schema": "r021.domain/1",
        "universe": {
            "bundle": bundle.bundle_hash,
            "cohort": "fixture" if bundle.kind == "synthetic" else "full_source",
            "pools": pools,
        },
        "protocols": ["constant_product", "concentrated", "liquidity_book"],
        "pool_order": pools,
        "hops": {"max": max_hops, "param": "search.max_hops"},
        "splits": {"max": graph.chunks, "param": "graph.chunks", "governs": "allocation"},
        "amount_grid": {"kind": "chunk_grid", "chunks": graph.chunks, "remainder": "floor_chunks"},
        "zero_output_leg": "infeasible",
        "token_reuse": "simple_path",
        "pool_reuse": "shared_merged",
        "dag_admission": "plan_token_dag",
        "full_fill": "v1_full_fill",
    }


_PATH_COUNTERS = ("paths_scored", "paths_rejected_cycle", "paths_truncated", "marginal_failures")


def _diagnostics(
    bundle: SnapshotBundle,
    graph: PreparedIncrementalGraph,
    stats: Mapping[str, Any],
    source: str | None,
) -> dict[str, Any]:
    """The `r021.diagnostics/1` record (suffix-repair.md §8)."""
    dom = domain(bundle, graph)
    r = stats["repair"]
    used = source is not None and source not in (REFERENCE, NAME)
    reason = None
    if used:
        status = stats["incremental_status"]
        reason = "retained_simpler_candidate" if status == "ok" else f"incremental_status {status}"
    return {
        "schema": "r021.diagnostics/1",
        "contract": "R021-C/1",
        "algorithm": NAME,
        "domain": dom,
        "candidate_domain_hash": hashlib.sha256(
            json.dumps(dom, sort_keys=True).encode()
        ).hexdigest(),
        "certificate": None,
        "certificate_unavailable_reason": "not_produced",
        "max_candidates_unit": "paths_scored_per_chunk",
        "work": {
            "quotes_executed": stats["quotes_executed"],
            "quotes_memoized": stats["quotes_memoized"],
            "internal_evaluations": stats["evaluations"]["total"],
            "paths_scored": stats["paths_scored"] + r["paths_scored"],
            "admission_checks": stats["paths_scored"]
            + stats["paths_rejected_cycle"]
            + r["paths_scored"]
            + r["paths_rejected_cycle"],
            "repair_attempts": r["repair_attempts"],
            "checkpoint_restores": r["checkpoint_restores"],
        },
        "fallback": {"used": used, "source": source, "reason": reason},
        "repair": {k: v for k, v in r.items() if k not in (*_PATH_COUNTERS, "attempts")},
    }


FACTORY = AlgorithmFactory(
    name=NAME,
    solve=solve,
    prepare=prepare,
    capabilities=CAPABILITIES,
    search_params=SEARCH_PARAMS,
    graph_params=GRAPH_PARAMS,
    options_validator=validate_options,
    options_preset=MappingProxyType(PRESET),
)

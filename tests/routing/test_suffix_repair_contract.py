"""R021-P07 (WHI-1553): the checkpoint-and-suffix repair contract for the future
`incremental_graph_repair` (docs/references/research-021/suffix-repair.md).

This module is research evidence, not a strategy. It holds three things, kept apart:

1. **The executable specification** (`repair_solve`): `incremental_graph`'s reference
   chunk loop re-expressed on the *actual* public pieces of
   `routing/algorithms/incremental_graph.py` (`PoolFlow`, `chunk_amounts`,
   `creates_cycle`, `merged_plan`), the embedded `path_split.solve`, the per-solve
   `QuoteCache` behind the same guarded meter, and the unchanged evaluator; plus the
   complete prefix checkpoint (`Checkpoint`), its restore, the registered structural
   suffix neighborhood and the one attempt ledger. With `repair: false` it must equal
   `incremental_graph.solve` exactly (plan, evaluation, statuses, counters, quotes). It is
   never registered, profiled or timed.
2. **An independent oracle** (`oracle_*`): its own adjacency, simple-path enumeration,
   token-cycle test, hand integer CPMM formula and aggregate-flow accounting, and an
   exhaustive enumeration of every chunk-path sequence of a small case. It shares no
   search code with the specification or with `incremental_graph`.
3. **Actual-solver reproductions**: `incremental_graph.solve` and the common
   `routing.evaluator.evaluate` on the fixtures of
   `docs/references/research-021/fixtures/suffix-repair.json`.

Chunks are search allocations on original-state aggregate quotes (`f(x+m) - f(x)`), never
sequential executed swaps; only the merged complete plan is ever replayed or scored.

`PYTHONPATH=. uv run python tests/routing/test_suffix_repair_contract.py probe <bundle>
<out.json> [--hops N] [--chunks N] [--rule structural|trailing] [--checkpoints N]
[--alternatives N] [--attempts N] [--max-quotes N] [case ids...]` is the bounded tuning
probe of `suffix-repair.md` §10 (a separate diagnostic pass; nothing here is a measured
solve).
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import random
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from benchmark.objective import ObjectiveContext, gross_only
from pools.quote import QuoteLimitExceeded, metered_quotes
from pools.result import QuoteStatus, SwapResult
from routing.algorithms import incremental_graph, path_split
from routing.algorithms.base import (
    AlgorithmConfig,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
)
from routing.algorithms.incremental_graph import (
    PoolFlow,
    chunk_amounts,
    creates_cycle,
    merged_plan,
)
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import REQUEST_FUND_ID, RoutePlan
from routing.search import Edge, QuoteCache, enumerate_paths
from snapshot.bundle import load_bundle
from snapshot.models import BlockRef, Case, ConstantProductPoolState, PoolState, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
R021 = REPO / "docs" / "references" / "research-021"
FIX = json.loads((R021 / "fixtures" / "suffix-repair.json").read_text(encoding="utf-8"))
HISTORY_FIX = json.loads((R021 / "fixtures" / "history-labels.json").read_text(encoding="utf-8"))
MEMO = (R021 / "suffix-repair.md").read_text(encoding="utf-8")
ROUTING_FIXTURES = REPO / "tests" / "fixtures" / "routing"
BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)
ALGORITHM = "incremental_graph_repair"

PathT = tuple[Edge, ...]
Pools = Mapping[str, Sequence[Any]]


# ================================================================ bundles


def cp_bundle(pools: Pools, fee_bps: int = 30) -> SnapshotBundle:
    """A synthetic bundle from `{pool_id: [token0, token1, reserve0, reserve1, (fee)]}` in
    the given (insertion = adjacency) order."""
    states: dict[str, PoolState] = {}
    for pid, spec in pools.items():
        t0, t1, r0, r1 = spec[:4]
        states[pid] = ConstantProductPoolState(
            pool_id=pid,
            token0=t0,
            token1=t1,
            reserve0=int(r0),
            reserve1=int(r1),
            fee_bps=int(spec[4]) if len(spec) > 4 else fee_bps,
            source_key=None,
        )
    return SnapshotBundle(
        bundle_id="whi1553",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools=states,
        cases=(),
        bundle_hash="whi1553",
        source_path="<test>",
    )


def fixture_case(name: str) -> tuple[SnapshotBundle, Case, dict[str, Any]]:
    spec = FIX["fixtures"][name]
    c = spec["case"]
    case = Case(name, c["token_in"], c["token_out"], int(c["amount_in"]))
    return cp_bundle(spec["pools"], spec.get("fee_bps", 30)), case, spec


# ================================================================ 1. the specification


RESERVED_KEYS = frozenset(
    (
        "max_hops",
        "max_splits",
        "percent_step",
        "chunks",
        "label_hops",
        "label_pruning",
        "time_limit_seconds",
        "max_quotes",
        "max_candidates",
        "shortlist",
        "sampling",
        "controls",
        "recipe",
        "objective",
        "seed",
    )
)
OPTION_RANGES: dict[str, tuple[int, int]] = {
    "max_checkpoints": (1, 64),
    "alternatives_per_checkpoint": (1, 16),
    "max_repair_attempts": (1, 256),
}
PRESET = {
    "repair": True,
    "max_checkpoints": 4,
    "alternatives_per_checkpoint": 2,
    "max_repair_attempts": 8,
}
REPAIR_OFF = {**PRESET, "repair": False}


class RepairOptionsError(ValueError):
    """An `algorithm_options.incremental_graph_repair` value is invalid (names the key)."""


def validate_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """The WHI-1548 `options_validator` of suffix-repair.md §7: all four keys required,
    no defaults, reserved and unknown keys refused, booleans only for `repair`."""
    for key in options:
        if key in RESERVED_KEYS:
            raise RepairOptionsError(f"{key}: reserved shared setting, not an algorithm option")
        if key not in PRESET:
            raise RepairOptionsError(f"{key}: unknown incremental_graph_repair option")
    for key in PRESET:
        if key not in options:
            raise RepairOptionsError(f"{key}: required")
    if not isinstance(options["repair"], bool):
        raise RepairOptionsError("repair: must be a boolean")
    out: dict[str, Any] = {"repair": options["repair"]}
    for key, (lo, hi) in OPTION_RANGES.items():
        value = options[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise RepairOptionsError(f"{key}: must be an integer")
        if not lo <= value <= hi:
            raise RepairOptionsError(f"{key}: must be in {lo}..{hi}, got {value}")
        out[key] = value
    return out


@dataclass(frozen=True)
class FlowRecord:
    """One committed `PoolFlow`, frozen: pool, used direction, aggregate input, exact
    aggregate output on the pool's original state, first-use order."""

    pool_id: str
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int
    order: int


@dataclass(frozen=True)
class Decision:
    """One committed chunk: its chunk position, the committed amount (carry included), the
    path (and its index in the enumeration), its marginal output and the token edges it
    added to the committed DAG (non-empty = a structural decision)."""

    position: int
    amount: int
    path_index: int
    path: PathT
    marginal: int
    new_edges: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Checkpoint:
    """The complete committed search prefix immediately before decision `len(decisions)`:
    the next chunk position, the carry entering it, the aggregate flows in insertion order
    (zero-input flows included), the committed token edges and the committed decisions.
    Everything else the loop holds is either immutable (bundle, case, chunk grid, path
    enumeration), a per-solve pure memo (`QuoteCache`), a monotone ledger counter or a
    state-dependent cache that a restore drops (suffix-repair.md §4)."""

    position: int
    carry: int
    flows: tuple[FlowRecord, ...]
    token_edges: frozenset[tuple[str, str]]
    decisions: tuple[Decision, ...]


def freeze(
    position: int,
    carry: int,
    flows: Mapping[str, PoolFlow],
    edges: set[tuple[str, str]],
    decisions: Sequence[Decision],
) -> Checkpoint:
    return Checkpoint(
        position,
        carry,
        tuple(
            FlowRecord(
                f.edge.pool_id,
                f.edge.token_in,
                f.edge.token_out,
                f.amount_in,
                f.amount_out,
                f.order,
            )
            for f in flows.values()
        ),
        frozenset(edges),
        tuple(decisions),
    )


def restore(cp: Checkpoint) -> tuple[dict[str, PoolFlow], set[tuple[str, str]], list[Decision]]:
    """Fresh mutable state equal to the checkpoint: new `PoolFlow` objects in the recorded
    insertion order, a new edge set and a new decision list. Nothing is subtracted."""
    flows = {
        r.pool_id: PoolFlow(
            Edge(r.pool_id, r.token_in, r.token_out), r.amount_in, r.amount_out, r.order
        )
        for r in cp.flows
    }
    return flows, set(cp.token_edges), list(cp.decisions)


class BudgetExhausted(Exception):
    pass


@dataclass(frozen=True)
class Failure:
    reason: str
    prefix: int
    note: str | None = None


class ChunkFailed(Exception):
    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.reason)
        self.failure = failure


@dataclass(frozen=True)
class Scored:
    """One admissible, successfully scored path of a chunk on a given committed state."""

    marginal: int
    index: int
    path: PathT
    updates: tuple[PoolFlow, ...]


@dataclass
class Counters:
    """Search counters of one stage. The incumbent stage's values are
    `incremental_graph`'s; the repair stage keeps its own (never reset, never merged into
    the reference keys)."""

    scored: int = 0
    rejected_cycle: int = 0
    truncated: int = 0
    carried: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    incomplete: list[str] = field(default_factory=list)


@dataclass
class Run:
    """The result of the reference loop from one checkpoint (optionally with a forced
    first choice): final state, decisions, per-decision checkpoints (incumbent trace only)
    and the loop status (`complete`, `chunk_<k>_no_admissible_path`)."""

    status: str
    flows: dict[str, PoolFlow]
    edges: set[tuple[str, str]]
    decisions: list[Decision]
    checkpoints: list[Checkpoint]


class Engine:
    """The mutable per-solve search of one `repair_solve` call: the reference marginal
    rule and chunk loop of `incremental_graph.solve` (lines of `marginal` and the chunk
    loop, verbatim semantics, default `graph_reuse=False`), parameterized by the state it
    runs on so that a restored checkpoint is used instead of the live one."""

    def __init__(
        self,
        bundle: SnapshotBundle,
        case: Case,
        paths: list[PathT],
        amounts: list[int],
        guarded: Callable[[PoolState, str, int], SwapResult[PoolState]],
        budget: Budget,
    ) -> None:
        self.bundle, self.case, self.paths, self.amounts = bundle, case, paths, amounts
        self.guarded, self.budget = guarded, budget
        self.last = max((k for k, a in enumerate(amounts) if a > 0), default=-1)
        self.cap_hit = False
        self.live: list[Decision] = []  # the running loop's decisions (read after a budget cut)

    def marginal(
        self,
        flows: Mapping[str, PoolFlow],
        path: PathT,
        amount: int,
        memo: dict[PathT, Any],
        counters: Counters,
    ) -> tuple[int, list[PoolFlow]]:
        m, updates = amount, list[PoolFlow]()
        for i, edge in enumerate(path):
            key = path[: i + 1]
            if key in memo:
                hit = memo[key]
                if isinstance(hit, Failure):
                    raise ChunkFailed(hit)
                m, update = hit
                updates.append(update)
                continue
            prev = flows.get(edge.pool_id)
            x, out = (prev.amount_in, prev.amount_out) if prev else (0, 0)
            order = prev.order if prev else len(flows)
            if m == 0:
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
                    failure = Failure(reason, i + 1, note)
                    memo[key] = failure
                    counters.failures[reason] = counters.failures.get(reason, 0) + 1
                    if note is not None:
                        counters.incomplete.append(note)
                    raise ChunkFailed(failure)
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
        enumeration order, with the reference's per-chunk `max_candidates` cap and a fresh
        per-chunk memo. `ranked(...)[0]` is the reference choice (first maximum)."""
        memo: dict[PathT, Any] = {}
        out: list[Scored] = []
        chunk_scored = 0
        for j, path in enumerate(self.paths):
            if (
                self.budget.max_candidates is not None
                and chunk_scored >= self.budget.max_candidates
            ):
                self.cap_hit = True
                counters.truncated += len(self.paths) - j
                break
            if creates_cycle(edges, path):
                counters.rejected_cycle += 1
                continue
            chunk_scored += 1
            try:
                m, updates = self.marginal(flows, path, amount, memo, counters)
            except ChunkFailed:
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
        """The reference chunk loop from `start` (the empty checkpoint for the incumbent).
        A `forced` first choice replaces the first decision; every later decision is the
        reference's deterministic first maximum on the state the suffix itself built."""
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
                scored = self.score(flows, edges, amount, counters)
                choice = min(scored, key=lambda s: (-s.marginal, s.index), default=None)
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


EMPTY = Checkpoint(0, 0, (), frozenset(), ())


def accounted_gross(case: Case, flows: Mapping[str, PoolFlow]) -> int:
    return sum(f.amount_out for f in flows.values() if f.edge.token_out == case.token_out)


def flow_key(flows: Mapping[str, PoolFlow]) -> frozenset[tuple[str, str, str, int]]:
    """A candidate's identity for "actual change" (suffix-repair.md §5.4): its positive
    aggregate pool inputs and directions. Equal keys give the same merged steps up to
    order, hence the same replay output; a candidate equal to the incumbent or to an
    earlier candidate is a duplicate and is not replayed."""
    return frozenset(
        (f.edge.pool_id, f.edge.token_in, f.edge.token_out, f.amount_in)
        for f in flows.values()
        if f.amount_in > 0
    )


def structural_checkpoints(run: Run, max_checkpoints: int) -> list[int]:
    """The registered neighborhood's checkpoint indices: decisions that added at least one
    token edge (structural), latest first, at most `max_checkpoints`."""
    return [i for i in reversed(range(len(run.decisions))) if run.decisions[i].new_edges][
        :max_checkpoints
    ]


def alternatives(decision: Decision, scored: list[Scored], limit: int) -> list[Scored]:
    """Alternative first suffix choices at a restored checkpoint: every other admissible
    path with a positive marginal, by marginal descending then enumeration index."""
    ranked = sorted(scored, key=lambda s: (-s.marginal, s.index))
    return [s for s in ranked if s.index != decision.path_index and s.marginal > 0][:limit]


@dataclass
class RepairLedger:
    """The repair stage's counters (suffix-repair.md §8). All monotone within the solve."""

    checkpoint_restores: int = 0
    repair_attempts: int = 0
    internal_evaluations: int = 0
    candidates_complete: int = 0
    candidates_failed: int = 0
    duplicates: int = 0
    rejected_worse: int = 0
    ties: int = 0
    accepted: int = 0
    consistency_failures: int = 0
    stop: str = "not_run"
    counters: Counters = field(default_factory=Counters)
    accepted_log: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RepairResult:
    """`repair_solve`'s output: the ordinary `SolveResult` fields plus the specification's
    internal views used by the checks (incumbent trace, candidate log)."""

    result: SolveResult
    incumbent_run: Run | None
    ledger: RepairLedger
    candidates: list[dict[str, Any]]


def prepare_ig(bundle: SnapshotBundle, params: Mapping[str, int]) -> Any:
    return incremental_graph.prepare(bundle, AlgorithmConfig(incremental_graph.NAME, dict(params)))


def repair_solve(
    case: Case,
    context: SolveContext,
    budget: Budget,
    options: Mapping[str, Any],
    *,
    restore_fn: Callable[[Checkpoint], Checkpoint] | None = None,
    quote_hook: Callable[[int], None] | None = None,
    rule: str = "structural",
) -> RepairResult:
    """The normative algorithm of suffix-repair.md §5. `restore_fn` is a test-only seam for
    the stale-state mutations (identity in the specification); `quote_hook(n)` is called
    before the n-th executed quote (interruption tests). `rule="trailing"` (every decision,
    latest first) is a probe-only ablation of the registered structural rule, not an
    option."""
    opts = validate_options(options)
    prepared = context.prepared
    bundle, objective = context.bundle, context.objective
    ps_prepared = prepared.path_split
    max_hops = ps_prepared.single_path.max_hops
    cache = QuoteCache(bundle)
    quiet = dataclasses.replace(context, candidate_sink=None)

    # ---- stage 1: the retained simpler candidates (path_split on the shared cache).
    ps = path_split.solve(
        case, dataclasses.replace(quiet, prepared=ps_prepared), budget, cache=cache
    )
    best: tuple[str, RoutePlan, Evaluation, int] | None = None
    if ps.status is SolveStatus.OK and ps.plan and ps.evaluation and ps.score is not None:
        best = (
            str(ps.search_stats.get("chosen_source") or path_split.NAME),
            ps.plan,
            ps.evaluation,
            ps.score,
        )
        context.report_candidate(ps.plan)

    truncated_by: str | None = None

    def guarded(state: PoolState, token_in: str, amount: int) -> SwapResult[PoolState]:
        nonlocal truncated_by
        memo_hit = bundle.pools.get(state.pool_id) is state and cache.cached(
            state.pool_id, token_in, amount
        )
        if budget.max_quotes is not None and cache.misses >= budget.max_quotes and not memo_hit:
            truncated_by = "max_quotes"
            raise BudgetExhausted
        if quote_hook is not None and not memo_hit:
            quote_hook(cache.misses + 1)
        return cache(state, token_in, amount)

    paths = list(
        enumerate_paths(ps_prepared.single_path.index, case.token_in, case.token_out, max_hops)
    )
    amounts = chunk_amounts(case.amount_in, prepared.chunks)
    engine = Engine(bundle, case, paths, amounts, guarded, budget)
    inc = Counters()
    incremental_status = "not_run"
    incremental: tuple[RoutePlan, Evaluation, int] | None = None
    run: Run | None = None
    own_truncated_budget = 0

    # ---- stage 2: the incumbent incremental plan (the reference loop, traced).
    try:
        if not paths:
            incremental_status = "no_paths"
        else:
            run = engine.run(EMPTY, inc, trace=True)
            if run.status != "complete":
                incremental_status = run.status
            else:
                plan = merged_plan(case, run.flows.values())
                ev = evaluate(bundle, case, plan, objective, quote=guarded)
                if ev.status is EvalStatus.OK:
                    incremental_status = "ok"
                    incremental = (plan, ev, objective.score(ev))
                else:
                    incremental_status = "invalid_plan"
                    inc.failures["invalid_plan"] = inc.failures.get("invalid_plan", 0) + 1
    except BudgetExhausted:
        incremental_status = "truncated"
        own_truncated_budget = 1
    allocated = len(engine.live)  # committed incumbent chunks (also after a budget cut)
    if engine.cap_hit:
        truncated_by = truncated_by or "max_candidates"
    if incremental is not None and (best is None or incremental[2] > best[3]):
        best = (incremental_graph.NAME, incremental[0], incremental[1], incremental[2])
        context.report_candidate(incremental[0])

    # ---- stage 3: bounded structural suffix repair (same cache, meter and budget).
    ledger = RepairLedger()
    candidates: list[dict[str, Any]] = []
    rep = ledger.counters
    if not opts["repair"]:
        ledger.stop = "disabled"
    elif incremental_status == "truncated":
        ledger.stop = "quote_budget"
    elif incremental_status == "invalid_plan":  # accounting disagrees with replay: a defect
        ledger.consistency_failures += 1
        ledger.stop = "consistency_failure"
    elif run is None or not run.decisions:
        ledger.stop = "no_trace"
    else:
        seen = {flow_key(run.flows)} if run.status == "complete" else set()
        ledger.stop = "complete"
        try:
            registered = (
                structural_checkpoints(run, opts["max_checkpoints"])
                if rule == "structural"
                else list(reversed(range(len(run.decisions))))[: opts["max_checkpoints"]]
            )
            for i in registered:
                if ledger.stop != "complete":
                    break
                cp = run.checkpoints[i]
                if restore_fn is not None:
                    cp = restore_fn(cp)
                ledger.checkpoint_restores += 1
                decision = run.decisions[i]
                flows, edges, _ = restore(cp)
                scored = engine.score(flows, edges, decision.amount, rep)
                top = min(scored, key=lambda s: (-s.marginal, s.index), default=None)
                if top is None or (top.index, top.marginal) != (
                    decision.path_index,
                    decision.marginal,
                ):
                    ledger.consistency_failures += 1
                    ledger.stop = "consistency_failure"
                    break
                for alt in alternatives(decision, scored, opts["alternatives_per_checkpoint"]):
                    if ledger.repair_attempts >= opts["max_repair_attempts"]:
                        ledger.stop = "attempt_cap"
                        break
                    ledger.repair_attempts += 1
                    record: dict[str, Any] = {"checkpoint": i, "alternative": alt.index}
                    candidates.append(record)
                    cand = engine.run(cp, rep, forced=alt)
                    record["run_status"] = cand.status
                    if cand.status != "complete":
                        ledger.candidates_failed += 1
                        continue
                    ledger.candidates_complete += 1
                    key = flow_key(cand.flows)
                    if key in seen:
                        record["outcome"] = "duplicate"
                        ledger.duplicates += 1
                        continue
                    seen.add(key)
                    try:
                        plan = merged_plan(case, cand.flows.values())
                    except ValueError as exc:
                        record["outcome"] = f"consistency_failure: {exc}"
                        ledger.consistency_failures += 1
                        ledger.stop = "consistency_failure"
                        break
                    ev = evaluate(bundle, case, plan, objective, quote=guarded)
                    ledger.internal_evaluations += 1
                    accounted = accounted_gross(case, cand.flows)
                    record["accounted"] = accounted
                    record["evaluated"] = ev.gross_output if ev.status is EvalStatus.OK else None
                    if ev.status is not EvalStatus.OK or ev.gross_output != accounted:
                        record["outcome"] = "consistency_failure"
                        ledger.consistency_failures += 1
                        ledger.stop = "consistency_failure"
                        break
                    score = objective.score(ev)
                    record["score"] = score
                    if best is not None and score < best[3]:
                        record["outcome"] = "rejected_worse"
                        ledger.rejected_worse += 1
                    elif best is not None and score == best[3]:
                        record["outcome"] = "tie"
                        ledger.ties += 1
                    else:
                        record["outcome"] = "accepted"
                        ledger.accepted += 1
                        best = (ALGORITHM, plan, ev, score)
                        ledger.accepted_log.append(
                            {"checkpoint": i, "alternative": alt.index, "score": str(score)}
                        )
                        context.report_candidate(plan)
        except BudgetExhausted:
            ledger.stop = "quote_budget"
            if candidates and "outcome" not in candidates[-1]:
                candidates[-1]["outcome"] = "budget_cut"
        if engine.cap_hit:
            truncated_by = truncated_by or "max_candidates"

    ps_truncated = ps.search_stats.get("truncated_by")
    stats: dict[str, Any] = {
        "chunks": prepared.chunks,
        "chunks_empty": sum(a == 0 for a in amounts),
        "chunks_carried": inc.carried,
        "chunks_allocated": allocated,
        "paths_enumerated": len(paths),
        "paths_scored": inc.scored,
        "paths_rejected_cycle": inc.rejected_cycle,
        "paths_truncated": inc.truncated + own_truncated_budget,
        "marginal_failures": dict(sorted(inc.failures.items())),
        "incremental_status": incremental_status,
        "incremental_score": None if incremental is None else str(incremental[2]),
        "truncated_by": truncated_by or ps_truncated,
        "quotes_executed": cache.misses,
        "quotes_memoized": cache.hits,
        "chosen_source": None,
        "repair": {
            "enabled": opts["repair"],
            "stop": ledger.stop,
            "checkpoint_restores": ledger.checkpoint_restores,
            "repair_attempts": ledger.repair_attempts,
            "candidates_complete": ledger.candidates_complete,
            "candidates_failed": ledger.candidates_failed,
            "duplicates": ledger.duplicates,
            "rejected_worse": ledger.rejected_worse,
            "ties": ledger.ties,
            "accepted": ledger.accepted,
            "consistency_failures": ledger.consistency_failures,
            "internal_evaluations": ledger.internal_evaluations,
            "paths_scored": rep.scored,
            "paths_rejected_cycle": rep.rejected_cycle,
            "paths_truncated": rep.truncated,
            "marginal_failures": dict(sorted(rep.failures.items())),
            "accepted_log": ledger.accepted_log,
        },
    }
    common: dict[str, Any] = {
        "case_id": case.case_id,
        "algorithm": ALGORITHM,
        "candidates_considered": ps.candidates_considered + inc.scored + rep.scored,
        "candidates_truncated": ps.candidates_truncated
        + inc.truncated
        + own_truncated_budget
        + rep.truncated,
        "search_stats": stats,
    }
    if best is not None:
        source, plan, ev, score = best
        stats["chosen_source"] = source
        result = SolveResult(status=SolveStatus.OK, plan=plan, evaluation=ev, score=score, **common)
    elif truncated_by is not None or ps.status is SolveStatus.TIMEOUT:
        result = SolveResult(
            status=SolveStatus.TIMEOUT, error="declared budget truncated the search", **common
        )
    elif inc.incomplete or ps.status is SolveStatus.INCOMPLETE_SNAPSHOT:
        result = SolveResult(
            status=SolveStatus.INCOMPLETE_SNAPSHOT, error="uncollected state", **common
        )
    else:
        result = SolveResult(status=SolveStatus.NO_ROUTE, error=ps.error, **common)
    return RepairResult(result, run, ledger, candidates)


# ================================================================ 2. independent oracle
#
# Own adjacency, own simple-path enumeration, own token-cycle test, the hand integer
# UniswapV2 `getAmountOut` (generalized to `fee_bps`, R2 dust rule), own aggregate-flow
# accounting. Nothing below imports or calls the specification, `incremental_graph`,
# `routing.search` or the evaluator.

OPath = tuple[tuple[str, str, str], ...]  # ((pool, token_in, token_out), ...)


def oracle_out(spec: Sequence[Any], token_in: str, amount: int, fee_bps: int) -> int | None:
    """Hand CPMM output of `amount` into a pool on its original reserves; None = the
    quote fails (non-positive output: `insufficient_output_amount`)."""
    t0, r0, r1 = spec[0], int(spec[2]), int(spec[3])
    fee = int(spec[4]) if len(spec) > 4 else fee_bps
    r_in, r_out = (r0, r1) if token_in == t0 else (r1, r0)
    with_fee = amount * (10_000 - fee)
    out = with_fee * r_out // (r_in * 10_000 + with_fee)
    return out if 0 < out < r_out else None


def oracle_paths(pools: Pools, source: str, target: str, hops: int) -> list[OPath]:
    """Every token-simple path source -> target with 1..hops pools, never through target."""
    adj: dict[str, list[tuple[str, str]]] = {}
    for pid, spec in pools.items():
        adj.setdefault(spec[0], []).append((pid, spec[1]))
        adj.setdefault(spec[1], []).append((pid, spec[0]))
    found: list[OPath] = []

    def walk(token: str, visited: frozenset[str], acc: OPath) -> None:
        for pid, nxt in adj.get(token, []):
            if nxt in visited:
                continue
            step = (*acc, (pid, token, nxt))
            if nxt == target:
                found.append(step)
            elif len(step) < hops:
                walk(nxt, visited | {nxt}, step)

    walk(source, frozenset((source,)), ())
    return found


def oracle_acyclic(edges: set[tuple[str, str]]) -> bool:
    """Kahn's algorithm on the directed token graph."""
    nodes = {t for e in edges for t in e}
    indeg = dict.fromkeys(nodes, 0)
    for _, v in edges:
        indeg[v] += 1
    ready = [t for t in nodes if indeg[t] == 0]
    seen = 0
    while ready:
        t = ready.pop()
        seen += 1
        for u, v in edges:
            if u == t:
                indeg[v] -= 1
                if indeg[v] == 0:
                    ready.append(v)
    return seen == len(nodes)


@dataclass(frozen=True)
class OState:
    """Oracle aggregate state: per pool (direction token_in, aggregate input), edges."""

    x: tuple[tuple[str, str, int], ...] = ()
    edges: frozenset[tuple[str, str]] = frozenset()

    def get(self, pid: str) -> tuple[str, int] | None:
        for p, t, a in self.x:
            if p == pid:
                return t, a
        return None


def oracle_chunk(
    pools: Pools, fee: int, state: OState, path: OPath, amount: int
) -> tuple[int, OState] | None:
    """The marginal of one chunk on `path` against the aggregate state, or None if the
    path is inadmissible (cyclic union) or a quote fails."""
    edges = set(state.edges) | {(a, b) for _, a, b in path}
    if not oracle_acyclic(edges):
        return None
    x = dict((p, (t, a)) for p, t, a in state.x)
    m = amount
    for pid, a, _ in path:
        prev = x.get(pid)
        base = prev[1] if prev else 0
        if m == 0:
            continue
        before = 0 if base == 0 else oracle_out(pools[pid], a, base, fee)
        after = oracle_out(pools[pid], a, base + m, fee)
        if after is None or before is None:
            return None
        x[pid] = (a, base + m)
        m = after - before
    return m, OState(tuple((p, t, v) for p, (t, v) in sorted(x.items())), frozenset(edges))


def oracle_gross(pools: Pools, fee: int, state: OState, target: str) -> int:
    total = 0
    for pid, t, a in state.x:
        spec = pools[pid]
        other = spec[1] if t == spec[0] else spec[0]
        if other == target and a > 0:
            out = oracle_out(spec, t, a, fee)
            assert out is not None
            total += out
    return total


def oracle_amounts(amount: int, chunks: int) -> list[int]:
    """Own chunk grid: amount*k//K - amount*(k-1)//K, k = 1..K."""
    return [amount * k // chunks - amount * (k - 1) // chunks for k in range(1, chunks + 1)]


def oracle_sequence(
    pools: Pools, fee: int, case: Case, hops: int, chunks: int, sequence: Sequence[Sequence[str]]
) -> int | None:
    """Whole-plan gross of one explicit chunk-path sequence: one entry per nonzero chunk,
    the pool ids of its path or `("carry",)`; None = infeasible."""
    paths = {
        tuple(p for p, _, _ in op): op
        for op in oracle_paths(pools, case.token_in, case.token_out, hops)
    }
    state, carry = OState(), 0
    chunks_nonzero = [a for a in oracle_amounts(case.amount_in, chunks) if a > 0]
    assert len(sequence) == len(chunks_nonzero)
    for chunk, pids in zip(chunks_nonzero, sequence, strict=True):
        if tuple(pids) == ("carry",):
            carry += chunk
            continue
        res = oracle_chunk(pools, fee, state, paths[tuple(pids)], carry + chunk)
        if res is None:
            return None
        carry, state = 0, res[1]
    return oracle_gross(pools, fee, state, case.token_out)


def oracle_best(
    pools: Pools, fee: int, case: Case, hops: int, chunks: int
) -> tuple[int, list[tuple[tuple[str, ...], ...]], int]:
    """Exhaustive optimum of the chunk-sequence domain (suffix-repair.md §3.1): every nonzero
    chunk commits to an admissible path with a positive marginal (the final chunk: any
    admissible successful path), a non-final chunk with none is carried. Returns the best
    gross, every sequence reaching it and the number of complete sequences."""
    paths = oracle_paths(pools, case.token_in, case.token_out, hops)
    amounts = oracle_amounts(case.amount_in, chunks)
    last = max(k for k, a in enumerate(amounts) if a > 0)
    top: list[int] = [-1, 0]  # best gross, complete sequences
    winners: list[tuple[tuple[str, ...], ...]] = []

    def dfs(k: int, state: OState, carry: int, seq: tuple[tuple[str, ...], ...]) -> None:
        if k == len(amounts):
            top[1] += 1
            g = oracle_gross(pools, fee, state, case.token_out)
            if g > top[0]:
                top[0] = g
                winners[:] = [seq]
            elif g == top[0]:
                winners.append(seq)
            return
        if amounts[k] == 0:
            dfs(k + 1, state, carry, seq)
            return
        amount = carry + amounts[k]
        options = []
        for op in paths:
            res = oracle_chunk(pools, fee, state, op, amount)
            if res is not None and (res[0] > 0 or k == last):
                options.append((op, res[1]))
        if not options:
            if k != last:
                dfs(k + 1, state, amount, (*seq, ("carry",)))
            return
        for op, nxt in options:
            dfs(k + 1, nxt, 0, (*seq, tuple(p for p, _, _ in op)))

    dfs(0, OState(), 0, ())
    return top[0], winners, top[1]


# ================================================================ 3. helpers for the checks


def context(
    bundle: SnapshotBundle,
    params: Mapping[str, int],
    sink: list[RoutePlan] | None = None,
    objective: ObjectiveContext | None = None,
) -> SolveContext:
    return SolveContext(
        bundle,
        objective or gross_only(),
        prepare_ig(bundle, params),
        candidate_sink=None if sink is None else sink.append,
    )


def fixture_run(
    name: str, options: Mapping[str, Any] = PRESET, **kw: Any
) -> tuple[RepairResult, SnapshotBundle, Case, dict[str, Any]]:
    bundle, case, spec = fixture_case(name)
    return (
        repair_solve(case, context(bundle, spec["settings"]), Budget(), options, **kw),
        bundle,
        case,
        spec,
    )


def engine_for(
    bundle: SnapshotBundle, case: Case, params: Mapping[str, int], budget: Budget | None = None
) -> tuple[Engine, QuoteCache]:
    """A fresh engine on a fresh cache, built exactly as `repair_solve`'s stage 2."""
    prepared = prepare_ig(bundle, params)
    cache = QuoteCache(bundle)
    index = prepared.path_split.single_path.index
    paths = list(enumerate_paths(index, case.token_in, case.token_out, params["max_hops"]))
    amounts = chunk_amounts(case.amount_in, params["chunks"])
    return Engine(bundle, case, paths, amounts, cache, budget or Budget()), cache


def pool_sequence(engine: Engine, run: Run) -> list[tuple[str, ...]]:
    """The run's chunk-path sequence in the oracle's format: one entry per nonzero chunk."""
    by_position = {d.position: d for d in run.decisions}
    return [
        tuple(e.pool_id for e in by_position[k].path) if k in by_position else ("carry",)
        for k, a in enumerate(engine.amounts)
        if a > 0
    ]


def replay_candidate(
    bundle: SnapshotBundle, case: Case, params: Mapping[str, int], incumbent: Run, i: int, alt: int
) -> tuple[Engine, Run]:
    """Rebuild one logged repair candidate on a fresh engine: restore checkpoint `i`,
    rescore, force the logged alternative, rebuild the suffix."""
    engine, _ = engine_for(bundle, case, params)
    cp = incumbent.checkpoints[i]
    flows, edges, _ = restore(cp)
    forced = next(
        s
        for s in engine.score(flows, edges, incumbent.decisions[i].amount, Counters())
        if s.index == alt
    )
    return engine, engine.run(cp, Counters(), forced=forced)


def full_fill(case: Case, ev: Evaluation) -> None:
    """The v1 full-fill rule on the evaluator's own ledger: the request fund fully consumed,
    no residual, only target-token terminal balances, summing to the gross output."""
    assert ev.status is EvalStatus.OK and ev.residuals == {}
    funds = {f.fund_id: f for f in ev.funds}
    assert funds[REQUEST_FUND_ID].consumed == case.amount_in
    terminal = 0
    for f in ev.funds:
        if f.token == case.token_out:
            terminal += f.remaining
        else:
            assert f.remaining == 0, f
    assert terminal == ev.gross_output


REFERENCE_KEYS = (
    "chunks_carried",
    "chunks_allocated",
    "paths_enumerated",
    "paths_scored",
    "paths_rejected_cycle",
    "paths_truncated",
    "marginal_failures",
    "incremental_status",
    "incremental_score",
    "truncated_by",
    "quotes_executed",
    "quotes_memoized",
    "chosen_source",
)


def assert_same_as_reference(
    bundle: SnapshotBundle, case: Case, params: Mapping[str, int], budget: Budget
) -> RepairResult:
    """`repair: false` reproduces `incremental_graph.solve` exactly: status, plan, score,
    candidate counts, every compared search counter, the published candidate sequence and
    the worker quote meter."""
    ref_sink: list[RoutePlan] = []
    spec_sink: list[RoutePlan] = []
    with metered_quotes(None) as ref_meter:
        ref = incremental_graph.solve(case, context(bundle, params, ref_sink), budget)
    with metered_quotes(None) as spec_meter:
        got = repair_solve(case, context(bundle, params, spec_sink), budget, REPAIR_OFF)
    out = got.result
    for key in ("status", "plan", "score", "candidates_considered", "candidates_truncated"):
        assert getattr(out, key) == getattr(ref, key), key
    for key in REFERENCE_KEYS:
        assert out.search_stats[key] == ref.search_stats[key], key
    assert spec_sink == ref_sink and spec_meter.counted == ref_meter.counted
    assert out.search_stats["repair"]["stop"] == "disabled"
    return got


def random_instance(
    rng: random.Random,
) -> tuple[dict[str, list[Any]], Case, dict[str, int], Budget]:
    tokens = ["S", "A", "B", "C", "D"]
    pools: dict[str, list[Any]] = {}
    for i in range(rng.randint(3, 9)):
        a, b = rng.sample(tokens, 2)
        pools[f"p{i}"] = [
            a,
            b,
            rng.choice([10**4, 10**6, 10**8, 10**10]),
            rng.choice([10**4, 10**6, 10**8, 10**10]),
            rng.choice([0, 5, 30, 100]),
        ]
    case = Case("rnd", "S", "D", rng.choice([1, 7, 10**3, 10**6, 10**9]))
    params = {
        "max_hops": rng.choice([2, 3, 4]),
        "max_splits": rng.choice([1, 2, 4]),
        "percent_step": rng.choice([5, 10, 25]),
        "chunks": rng.choice([1, 2, 5, 13]),
    }
    budget = rng.choice(
        [Budget(), Budget(max_quotes=rng.randint(1, 300)), Budget(max_candidates=rng.randint(1, 4))]
    )
    return pools, case, params, budget


def mixed() -> SnapshotBundle:
    return load_bundle(ROUTING_FIXTURES / "mantle_mixed")


# ================================================================ 4. checks: specification fidelity


def test_repair_off_is_incremental_graph_on_fixtures_real_state_and_random_graphs() -> None:
    """The specification's incumbent stage *is* the reference: identical plans, counters,
    publications and metered quotes, including declared quote and candidate truncation."""
    for name in ("structural_trap", "twin_pools", "carry_and_zero_flow", "order_metadata"):
        bundle, case, spec = fixture_case(name)
        assert_same_as_reference(bundle, case, spec["settings"], Budget())
    real = mixed()
    for c in real.cases:
        assert_same_as_reference(
            real, c, {"max_hops": 2, "max_splits": 4, "percent_step": 5, "chunks": 10}, Budget()
        )
    rng = random.Random(20260930)
    for _ in range(80):
        pools, case, params, budget = random_instance(rng)
        assert_same_as_reference(
            cp_bundle({k: v[:4] + v[4:] for k, v in pools.items()}), case, params, budget
        )


def test_options_schema_refuses_every_invalid_value_before_solving() -> None:
    assert validate_options(PRESET) == PRESET and validate_options(REPAIR_OFF) == REPAIR_OFF
    bad: list[dict[str, Any]] = [
        {k: v for k, v in PRESET.items() if k != "max_checkpoints"},  # required, no default
        {**PRESET, "window": 3},  # unknown
        {**PRESET, "repair": 1},  # int for bool
        {**PRESET, "max_checkpoints": True},  # bool for int
        {**PRESET, "max_checkpoints": 4.0},  # float
        {**PRESET, "max_checkpoints": 0},
        {**PRESET, "alternatives_per_checkpoint": 17},
        {**PRESET, "max_repair_attempts": 257},
    ]
    bad += [{**PRESET, key: 1} for key in sorted(RESERVED_KEYS)]
    bundle, case, spec = fixture_case("structural_trap")
    for options in bad:
        with pytest.raises(RepairOptionsError):
            repair_solve(case, context(bundle, spec["settings"]), Budget(), options)


# ================================================================ 5. checks: checkpoint and restore


def _round_trip(bundle: SnapshotBundle, case: Case, params: Mapping[str, int]) -> int:
    """Resume the unforced reference loop from every checkpoint of the incumbent trace:
    byte-identical flows (values, order, insertion order, zero-input records), edges,
    decisions and merged plan, with zero new executed quotes. Returns #checkpoints."""
    engine, cache = engine_for(bundle, case, params)
    incumbent = engine.run(EMPTY, Counters(), trace=True)
    final = freeze(len(engine.amounts), 0, incumbent.flows, incumbent.edges, incumbent.decisions)
    quotes = cache.misses
    for i, cp in enumerate(incumbent.checkpoints):
        assert cp.decisions == tuple(incumbent.decisions[:i])
        assert cp.position == incumbent.decisions[i].position
        assert cp.carry == incumbent.decisions[i].amount - engine.amounts[cp.position]
        resumed = engine.run(cp, Counters())
        assert resumed.status == incumbent.status
        again = freeze(len(engine.amounts), 0, resumed.flows, resumed.edges, resumed.decisions)
        assert again == final
        if incumbent.status == "complete":
            assert merged_plan(case, resumed.flows.values()) == merged_plan(
                case, incumbent.flows.values()
            )
    assert cache.misses == quotes  # a restore re-asks only memoized original-state quotes
    return len(incumbent.checkpoints)


def test_checkpoint_round_trip_is_exact_everywhere() -> None:
    total = 0
    for name in ("structural_trap", "twin_pools", "carry_and_zero_flow", "order_metadata"):
        bundle, case, spec = fixture_case(name)
        total += _round_trip(bundle, case, spec["settings"])
    real = mixed()
    for c in real.cases:
        total += _round_trip(
            real, c, {"max_hops": 2, "max_splits": 4, "percent_step": 5, "chunks": 10}
        )
    rng = random.Random(20261002)
    for _ in range(60):
        pools, case, params, _ = random_instance(rng)
        total += _round_trip(cp_bundle(pools), case, params)
    assert total > 150


def test_checkpoint_keeps_carry_zero_input_flows_and_order_slots() -> None:
    """`carry_and_zero_flow`: carried dust enters the next decision's checkpoint; a
    zero-input PoolFlow is recorded (its edge is committed and its slot shifts later
    `order` values) although the merged plan omits it."""
    bundle, case, spec = fixture_case("carry_and_zero_flow")
    engine, _ = engine_for(bundle, case, spec["settings"])
    run = engine.run(EMPTY, Counters(), trace=True)
    want = spec["repository"]
    assert [
        [f.edge.pool_id, f.amount_in, f.amount_out, f.order] for f in run.flows.values()
    ] == want["final_flows"]
    assert [[c.position, c.carry] for c in run.checkpoints] == want[
        "checkpoint_positions_and_carry"
    ]
    zero = run.flows["p1"]
    assert zero.amount_in == 0 and (zero.edge.token_in, zero.edge.token_out) in run.edges
    plan = merged_plan(case, run.flows.values())
    assert "p1" not in {s.pool_id for s in plan.steps}
    full_fill(case, evaluate(bundle, case, plan, gross_only()))
    # The frozen state keeps the zero-input record (a restore must not drop it).
    final = freeze(len(engine.amounts), 0, run.flows, run.edges, run.decisions)
    assert FlowRecord("p1", "A", "D", 0, 0, 3) in final.flows
    assert restore(final)[0] == run.flows


def test_order_metadata_is_part_of_the_restored_state() -> None:
    bundle, case, spec = fixture_case("order_metadata")
    engine, _ = engine_for(bundle, case, spec["settings"])
    run = engine.run(EMPTY, Counters(), trace=True)
    cp = run.checkpoints[spec["checkpoint"]]
    n = len(cp.flows)
    renumbered = dataclasses.replace(
        cp, flows=tuple(dataclasses.replace(f, order=n - 1 - f.order) for f in cp.flows)
    )
    exact, stale = engine.run(cp, Counters()), engine.run(renumbered, Counters())
    good, bad = merged_plan(case, exact.flows.values()), merged_plan(case, stale.flows.values())
    assert good == merged_plan(case, run.flows.values()) and bad != good
    g, b = evaluate(bundle, case, good, gross_only()), evaluate(bundle, case, bad, gross_only())
    assert g.gross_output == b.gross_output == spec["repository"]["gross"]


def test_subtracting_a_non_suffix_chunk_leaves_stale_aggregates() -> None:
    """Why a restore copies a checkpoint instead of subtracting: removing chunk 1 of the
    trap's incumbent by subtracting its recorded pool deltas while keeping chunks 2-3
    leaves pools whose aggregate output is no longer f(aggregate input); only the real
    evaluator's replay exposes it. The checkpoint restore of the same prefix is exact."""
    bundle, case, spec = fixture_case("structural_trap")
    engine, _ = engine_for(bundle, case, spec["settings"])
    run = engine.run(EMPTY, Counters(), trace=True)
    before = {f.pool_id: f for f in run.checkpoints[0].flows}
    after0 = {f.pool_id: f for f in run.checkpoints[1].flows}
    flows = {k: dataclasses.replace(v) for k, v in run.flows.items()}
    for pid, rec in after0.items():  # subtract chunk 1's (delta in, delta out) per pool
        base = before.get(pid)
        d_in = rec.amount_in - (base.amount_in if base else 0)
        d_out = rec.amount_out - (base.amount_out if base else 0)
        f = flows[pid]
        flows[pid] = PoolFlow(f.edge, f.amount_in - d_in, f.amount_out - d_out, f.order)
    stale = {
        pid: f
        for pid, f in flows.items()
        if f.amount_in > 0
        and f.amount_out != oracle_out(spec["pools"][pid], f.edge.token_in, f.amount_in, 30)
    }
    assert stale  # some pool's recorded output is not its exact quote any more
    try:
        plan = merged_plan(case, flows.values())
    except ValueError:
        return  # non-conserved: rejected before any replay
    ev = evaluate(bundle, case, plan, gross_only())
    assert ev.status is not EvalStatus.OK or ev.gross_output != accounted_gross(case, flows)


# ================================================================ 6. checks: stale-state failures


def test_stale_flows_are_detected_before_any_candidate() -> None:
    """A restore that keeps the incumbent's final flows (not rolled back): the rescored
    checkpoint no longer reproduces the recorded decision, so the repair stops with
    `consistency_failure`, builds nothing and keeps the valid incumbent."""
    bundle, case, spec = fixture_case("structural_trap")
    off, *_ = fixture_run("structural_trap", REPAIR_OFF)
    final: list[Checkpoint] = []

    def stale(cp: Checkpoint) -> Checkpoint:
        if not final:
            engine, _ = engine_for(bundle, case, spec["settings"])
            run = engine.run(EMPTY, Counters())
            final.append(freeze(0, 0, run.flows, run.edges, ()))
        return dataclasses.replace(cp, flows=final[0].flows)

    got, *_ = fixture_run("structural_trap", restore_fn=stale)
    r = got.result.search_stats["repair"]
    assert r["stop"] == "consistency_failure" and r["consistency_failures"] == 1
    assert r["repair_attempts"] == 0 and got.result.plan == off.result.plan
    # Unguarded, the same stale flows double-count the rolled-back input: not conserved.
    engine, _ = engine_for(bundle, case, spec["settings"])
    run = engine.run(EMPTY, Counters(), trace=True)
    cp = dataclasses.replace(run.checkpoints[0], flows=final[0].flows)
    doubled = engine.run(cp, Counters())
    with pytest.raises(ValueError):  # a token cycle or non-conserved flows
        merged_plan(case, doubled.flows.values())


def test_stale_token_edges_lose_the_valid_improvement() -> None:
    """A restore that rolls back flows but keeps the incumbent's final token edges: the fix
    path S-E-B-D is (wrongly) inadmissible because the rolled-back B->E edge survives, the
    repair finds nothing and returns the incumbent. The exact restore finds the oracle
    optimum. `creates_cycle` decides both on the actual edge sets."""
    bundle, case, spec = fixture_case("structural_trap")
    engine, _ = engine_for(bundle, case, spec["settings"])
    run = engine.run(EMPTY, Counters(), trace=True)
    fix = next(p for p in engine.paths if [e.pool_id for e in p] == ["es", "be", "bd"])
    assert creates_cycle(run.edges, fix) and not creates_cycle(
        set(run.checkpoints[0].token_edges), fix
    )
    got, *_ = fixture_run(
        "structural_trap",
        restore_fn=lambda cp: dataclasses.replace(cp, token_edges=frozenset(run.edges)),
    )
    exact, *_ = fixture_run("structural_trap")
    fix_index = engine.paths.index(fix)
    assert fix_index not in {c["alternative"] for c in got.candidates}  # never even built
    assert got.result.score == 94_153_558 < spec["oracle"]["best_gross"]  # valid, fix lost
    assert exact.result.score == spec["oracle"]["best_gross"]
    assert fix_index in {c["alternative"] for c in exact.candidates}


def test_reused_exact_closure_must_not_survive_a_rollback() -> None:
    """`incremental_graph._ExactReuse` (L05, read-only here) keeps monotone cycle
    rejections because committed edges only grow. After committing the trap's incumbent
    it rejects the fix path; on the restored empty prefix the fix is admissible. A
    rollback therefore rebuilds admission from the restored edges (suffix-repair.md §4)."""
    bundle, case, spec = fixture_case("structural_trap")
    engine, _ = engine_for(bundle, case, spec["settings"])
    run = engine.run(EMPTY, Counters(), trace=True)
    reuse = incremental_graph._ExactReuse(engine.paths)
    edges: set[tuple[str, str]] = set()
    for d in run.decisions:
        new = [
            t for t in dict.fromkeys((e.token_in, e.token_out) for e in d.path) if t not in edges
        ]
        reuse.commit(d.path, new)
        edges.update(new)
    j = next(i for i, p in enumerate(engine.paths) if [e.pool_id for e in p] == ["es", "be", "bd"])
    assert reuse.cyclic(j, engine.paths[j])
    fresh = incremental_graph._ExactReuse(engine.paths)
    assert not fresh.cyclic(j, engine.paths[j])


def test_a_stale_score_as_first_choice_is_rejected_by_the_replay_check() -> None:
    """Reusing a chunk score computed on another committed state (the final incumbent
    state) as the forced first choice at checkpoint 0 carries stale aggregates into the
    candidate: its flows are not conserved or its accounted gross differs from the replay.
    The specification's checks (merged_plan conservation, evaluated == accounted) catch it."""
    bundle, case, spec = fixture_case("structural_trap")
    engine, _ = engine_for(bundle, case, spec["settings"])
    run = engine.run(EMPTY, Counters(), trace=True)
    d0 = run.decisions[0]
    stale_scores = engine.score(run.flows, set(), d0.amount, Counters())
    fix = next(s for s in stale_scores if [e.pool_id for e in s.path] == ["es", "be", "bd"])
    cand = engine.run(run.checkpoints[0], Counters(), forced=fix)
    assert cand.status == "complete"
    try:
        plan = merged_plan(case, cand.flows.values())
    except ValueError:
        return
    ev = evaluate(bundle, case, plan, gross_only())
    assert ev.status is not EvalStatus.OK or ev.gross_output != accounted_gross(case, cand.flows)


# ================================================================ 7. checks: the greedy trap


def test_structural_trap_incumbent_and_optimum_are_independently_known() -> None:
    """The oracle (own paths, cycle test, CPMM formula, exhaustive sequences) reproduces the
    actual incremental_graph plan's evaluated gross and finds a unique better sequence whose
    first chunk uses the E->B direction the incumbent's B->E edge forbids."""
    bundle, case, spec = fixture_case("structural_trap")
    o, params = spec["oracle"], spec["settings"]
    ref = incremental_graph.solve(case, context(bundle, params), Budget())
    assert ref.search_stats["chosen_source"] == spec["repository"]["incremental_graph_source"]
    assert ref.score == spec["repository"]["incremental_graph_score"] == o["incumbent_gross"]
    assert ref.search_stats["path_split_score"] == str(spec["repository"]["path_split_score"])
    engine, _ = engine_for(bundle, case, params)
    inc = engine.run(EMPTY, Counters())
    assert [list(s) for s in pool_sequence(engine, inc)] == o["incumbent_sequence"]
    assert (
        oracle_sequence(spec["pools"], 30, case, 3, 3, o["incumbent_sequence"])
        == o["incumbent_gross"]
    )
    best, winners, complete = oracle_best(
        spec["pools"], 30, case, params["max_hops"], params["chunks"]
    )
    assert (best, complete) == (o["best_gross"], o["complete_sequences"])
    assert [[list(p) for p in w] for w in winners] == o["best_sequences"]
    assert tuple(o["blocking_edge"]) in inc.edges
    fix_path = next(p for p in engine.paths if [e.pool_id for e in p] == list(winners[0][0]))
    assert tuple(o["fix_edge"]) in {(e.token_in, e.token_out) for e in fix_path}
    assert creates_cycle(inc.edges, fix_path)  # forbidden after the incumbent's chunk 1
    assert not creates_cycle(set(), fix_path)


def test_repair_finds_the_independent_optimum_with_a_changed_valid_suffix() -> None:
    got, bundle, case, spec = fixture_run("structural_trap")
    want, o = spec["specification"], spec["oracle"]
    r = got.result.search_stats["repair"]
    assert [c["outcome"] for c in got.candidates] == want["outcomes"]
    assert (
        list(dict.fromkeys(c["checkpoint"] for c in got.candidates)) == want["checkpoints_visited"]
    )
    assert [int(a["score"]) for a in r["accepted_log"]] == want["accepted_scores"]
    assert got.result.score == want["repair_score"] == o["best_gross"]
    assert got.result.search_stats["chosen_source"] == ALGORITHM
    assert r["stop"] == "complete" and r["consistency_failures"] == 0
    # Independent replay of the returned plan: plain evaluator, full fill, oracle value.
    assert got.result.plan is not None
    ev = evaluate(bundle, case, got.result.plan, gross_only())
    full_fill(case, ev)
    assert ev.gross_output == o["best_gross"]
    # The accepted candidate, rebuilt from its checkpoint, is the oracle's best sequence.
    last = r["accepted_log"][-1]
    assert got.incumbent_run is not None
    engine, cand = replay_candidate(
        bundle, case, spec["settings"], got.incumbent_run, last["checkpoint"], last["alternative"]
    )
    assert [list(p) for p in pool_sequence(engine, cand)] == o["best_sequences"][0]
    assert merged_plan(case, cand.flows.values()) == got.result.plan


def test_accepted_candidates_keep_the_prefix_and_change_the_first_suffix_choice() -> None:
    """For every accepted candidate on the trap at 3, 5 and 10 chunks and the WHI-1549 trap:
    decisions before the checkpoint are the incumbent's, the first suffix choice differs,
    the flows differ, and the oracle's value of the rebuilt sequence equals the evaluator's."""
    cases = [("structural_trap", k) for k in (3, 5, 10)]
    checked = kept_prefix = 0
    for name, chunks in cases:
        bundle, case, spec = fixture_case(name)
        params = {**spec["settings"], "chunks": chunks}
        got = repair_solve(case, context(bundle, params), Budget(), PRESET)
        run = got.incumbent_run
        assert run is not None
        for a in got.result.search_stats["repair"]["accepted_log"]:
            i = a["checkpoint"]
            engine, cand = replay_candidate(bundle, case, params, run, i, a["alternative"])
            assert cand.decisions[:i] == run.decisions[:i]
            assert cand.decisions[i].path != run.decisions[i].path
            assert flow_key(cand.flows) != flow_key(run.flows)
            plan = merged_plan(case, cand.flows.values())
            ev = evaluate(bundle, case, plan, gross_only())
            full_fill(case, ev)
            seq = pool_sequence(engine, cand)
            assert (
                oracle_sequence(spec["pools"], 30, case, params["max_hops"], chunks, seq)
                == ev.gross_output
            )
            assert ev.gross_output == int(a["score"])
            checked += 1
            kept_prefix += i > 0
    assert checked >= 5 and kept_prefix >= 1


def test_repair_is_not_optimal_even_in_its_own_domain() -> None:
    """At 5 chunks the repair improves the incumbent but stays below the exhaustive
    chunk-sequence optimum: no global (or domain) optimality claim."""
    bundle, case, spec = fixture_case("structural_trap")
    for chunks, want in spec["chunk_variants"].items():
        params = {**spec["settings"], "chunks": int(chunks)}
        got = repair_solve(case, context(bundle, params), Budget(), PRESET)
        best, _, _ = oracle_best(spec["pools"], 30, case, 3, int(chunks))
        assert best == want["oracle_best_gross"]
        assert got.result.search_stats["incremental_score"] == str(want["incremental_graph_score"])
        assert got.result.score == want["repair_score"] <= best
    assert (
        spec["chunk_variants"]["5"]["repair_score"]
        < spec["chunk_variants"]["5"]["oracle_best_gross"]
    )


def test_history_labels_greedy_trap_is_repaired_to_the_whi1549_label_plan() -> None:
    """Cross-check with WHI-1549's independent evidence: at 4 hops and 2 chunks the
    incumbent is path_split's plan (the incremental plan 9,943,929 loses to it); the repair
    replays to exactly WHI-1549's metis_inspired L4 gross."""
    x = FIX["cross_references"]["history_labels_greedy_trap"]
    g = HISTORY_FIX["fixtures"]["greedy_trap"]
    bundle = cp_bundle(g["pools"])
    case = Case(
        "greedy_trap", g["case"]["token_in"], g["case"]["token_out"], g["case"]["amount_in"]
    )
    off = repair_solve(case, context(bundle, x["settings"]), Budget(), REPAIR_OFF).result
    on = repair_solve(case, context(bundle, x["settings"]), Budget(), PRESET).result
    assert off.score == x["repository"]["incremental_graph_score"]
    assert off.search_stats["chosen_source"] == x["repository"]["incremental_graph_source"]
    assert off.search_stats["incremental_score"] == str(g["expected"]["enumeration_gross"])
    assert on.score == x["specification"]["repair_score"] == g["expected"]["label_gross"]
    assert on.plan is not None
    full_fill(case, evaluate(bundle, case, on.plan, gross_only()))


# ================================================================ 8. checks: rejection, ties, stops


def test_no_improvement_keeps_the_incumbent_and_its_publications() -> None:
    bundle, case, spec = fixture_case("twin_pools")
    off_sink: list[RoutePlan] = []
    on_sink: list[RoutePlan] = []
    off = repair_solve(case, context(bundle, spec["settings"], off_sink), Budget(), REPAIR_OFF)
    on = repair_solve(case, context(bundle, spec["settings"], on_sink), Budget(), PRESET)
    assert [c["outcome"] for c in on.candidates] == spec["specification"]["outcomes"]
    assert on.result.plan == off.result.plan and on.result.score == off.result.score
    assert on.result.search_stats["chosen_source"] == spec["specification"]["chosen_source"]
    assert on_sink == off_sink  # nothing new published
    r = on.result.search_stats["repair"]
    assert (r["duplicates"], r["rejected_worse"], r["accepted"], r["internal_evaluations"]) == (
        1,
        1,
        0,
        1,
    )


def test_an_equal_score_candidate_is_a_tie_and_the_earlier_incumbent_stays() -> None:
    x = FIX["cross_references"]["history_labels_tie_state"]
    g = HISTORY_FIX["fixtures"]["tie_state"]
    bundle = cp_bundle(g["pools"])
    case = Case("tie_state", g["case"]["token_in"], g["case"]["token_out"], g["case"]["amount_in"])
    off = repair_solve(case, context(bundle, x["settings"]), Budget(), REPAIR_OFF)
    on = repair_solve(case, context(bundle, x["settings"]), Budget(), PRESET)
    assert off.result.search_stats["incremental_score"] == str(x["repository"]["incremental_score"])
    assert (
        off.result.score
        == x["repository"]["incremental_graph_score"]
        == g["expected"]["history_gross"]
    )
    assert [c["outcome"] for c in on.candidates] == x["specification"]["outcomes"]
    assert on.result.plan == off.result.plan
    assert on.result.search_stats["chosen_source"] == x["repository"]["incremental_graph_source"]
    assert on.result.search_stats["repair"]["ties"] == 1


def test_single_route_has_a_checkpoint_but_no_alternative() -> None:
    bundle = cp_bundle({"ab": ["A", "B", 10**9, 10**9]})
    case = Case("one", "A", "B", 10**6)
    got = repair_solve(
        case,
        context(bundle, {"max_hops": 2, "max_splits": 4, "percent_step": 5, "chunks": 5}),
        Budget(),
        PRESET,
    )
    r = got.result.search_stats["repair"]
    assert (r["stop"], r["checkpoint_restores"], r["repair_attempts"]) == ("complete", 1, 0)


def test_attempt_cap_and_unreachable_pairs_stop_visibly() -> None:
    got, *_ = fixture_run("structural_trap", {**PRESET, "max_repair_attempts": 1})
    r = got.result.search_stats["repair"]
    assert (r["stop"], r["repair_attempts"]) == ("attempt_cap", 1)
    bundle = cp_bundle({"ab": ["A", "B", 10**6, 10**6], "cd": ["C", "D", 10**6, 10**6]})
    none = repair_solve(
        Case("u", "A", "D", 1000),
        context(bundle, {"max_hops": 2, "max_splits": 4, "percent_step": 5, "chunks": 3}),
        Budget(),
        PRESET,
    )
    assert none.result.status is SolveStatus.NO_ROUTE
    assert none.result.search_stats["repair"]["stop"] == "no_trace"


def test_repair_is_deterministic() -> None:
    a, *_ = fixture_run("structural_trap")
    b, *_ = fixture_run("structural_trap")
    assert a.result == b.result and a.candidates == b.candidates


# ================================ 9. checks: one ledger, budgets, interruption


def test_one_quote_ledger_covers_every_stage() -> None:
    """quotes_executed is the worker meter over path_split, the incumbent, every restore,
    rebuild and replay; the incumbent stage's own work equals the repair-off control."""
    bundle, case, spec = fixture_case("structural_trap")
    with metered_quotes(None) as off_meter:
        off = repair_solve(case, context(bundle, spec["settings"]), Budget(), REPAIR_OFF).result
    with metered_quotes(None) as on_meter:
        on = repair_solve(case, context(bundle, spec["settings"]), Budget(), PRESET).result
    assert off.search_stats["quotes_executed"] == off_meter.counted
    assert on.search_stats["quotes_executed"] == on_meter.counted > off_meter.counted
    for key in ("paths_scored", "paths_rejected_cycle", "chunks_allocated", "incremental_score"):
        assert on.search_stats[key] == off.search_stats[key], key
    assert on.search_stats["repair"]["paths_scored"] > 0
    assert (
        on.candidates_considered
        == off.candidates_considered + on.search_stats["repair"]["paths_scored"]
    )


def test_the_budget_is_never_reset_for_the_repair() -> None:
    """With max_quotes equal to the repair-off solve's executed quotes, the repair can only
    rescore memoized checkpoints: its first new quote stops it (`quote_budget`), the meter
    never exceeds the limit and the repair-off plan is returned unchanged. Every limit in
    between keeps a valid plan at least as good as the incumbent."""
    bundle, case, spec = fixture_case("structural_trap")
    off = repair_solve(case, context(bundle, spec["settings"]), Budget(), REPAIR_OFF).result
    full = repair_solve(case, context(bundle, spec["settings"]), Budget(), PRESET).result
    n_off, n_on = off.search_stats["quotes_executed"], full.search_stats["quotes_executed"]
    for limit in range(n_off, n_on + 1):
        with metered_quotes(limit) as meter:
            got = repair_solve(
                case, context(bundle, spec["settings"]), Budget(max_quotes=limit), PRESET
            )
        res = got.result
        assert not meter.exceeded and meter.counted == res.search_stats["quotes_executed"] <= limit
        assert res.status is SolveStatus.OK and res.score is not None and off.score is not None
        assert res.score >= off.score
        if limit < n_on:
            assert res.search_stats["repair"]["stop"] == "quote_budget"
            assert res.search_stats["truncated_by"] == "max_quotes"
        if limit == n_off:
            assert res.plan == off.plan
    with metered_quotes(n_off - 1) as meter:  # the incumbent itself cut: repair never starts
        cut = repair_solve(
            case, context(bundle, spec["settings"]), Budget(max_quotes=n_off - 1), PRESET
        )
    assert cut.result.search_stats["repair"]["stop"] == "quote_budget" and not meter.exceeded


def test_interruption_leaves_only_complete_replayed_publications() -> None:
    """The runner's hard quote meter kills the solve at every possible executed quote in
    turn (all stages): the publications so far are a prefix of the uninterrupted,
    strictly improving sequence and the last one is a complete full-fill plan, so a kill
    during repair never exposes a partial suffix."""
    bundle, case, spec = fixture_case("structural_trap")
    full_sink: list[RoutePlan] = []
    full = repair_solve(case, context(bundle, spec["settings"], full_sink), Budget(), PRESET).result
    n = full.search_stats["quotes_executed"]
    scores = [evaluate(bundle, case, p, gross_only()).gross_output for p in full_sink]
    assert scores == sorted(set(scores)) and scores[-1] == full.score and len(scores) >= 3
    seen_lengths = set()
    for kill_at in range(n):
        sink: list[RoutePlan] = []
        with metered_quotes(kill_at), pytest.raises(QuoteLimitExceeded):
            repair_solve(case, context(bundle, spec["settings"], sink), Budget(), PRESET)
        assert sink == full_sink[: len(sink)]
        if sink:
            full_fill(case, evaluate(bundle, case, sink[-1], gross_only()))
        seen_lengths.add(len(sink))
    assert seen_lengths == set(range(len(full_sink)))  # killed before each publication


class Killed(Exception):
    """A simulated hard kill raised from the specification's quote seam."""


def test_a_kill_inside_the_repair_stage_keeps_the_published_incumbent() -> None:
    """Killing at the first executed quote after the repair-off solve's last one (the first
    repair rebuild quote) leaves exactly the repair-off control's publications."""
    bundle, case, spec = fixture_case("structural_trap")
    off_sink: list[RoutePlan] = []
    off = repair_solve(case, context(bundle, spec["settings"], off_sink), Budget(), REPAIR_OFF)
    first_repair_quote = off.result.search_stats["quotes_executed"] + 1

    def hook(n: int) -> None:
        if n == first_repair_quote:
            raise Killed

    sink: list[RoutePlan] = []
    with pytest.raises(Killed):
        repair_solve(
            case, context(bundle, spec["settings"], sink), Budget(), PRESET, quote_hook=hook
        )
    assert sink == off_sink and sink[-1] == off.result.plan


def test_candidate_cap_applies_to_every_rebuilt_chunk() -> None:
    bundle, case, spec = fixture_case("structural_trap")
    got = repair_solve(case, context(bundle, spec["settings"]), Budget(max_candidates=2), PRESET)
    s = got.result.search_stats
    assert s["truncated_by"] == "max_candidates" and s["repair"]["paths_truncated"] > 0


# ================================ 10. checks: real state and random graphs


def test_repair_on_real_mixed_family_state_is_consistent() -> None:
    real = mixed()
    for c in real.cases:
        params = {"max_hops": 2, "max_splits": 4, "percent_step": 5, "chunks": 10}
        off = repair_solve(c, context(real, params), Budget(), REPAIR_OFF).result
        with metered_quotes(None) as meter:
            on = repair_solve(c, context(real, params), Budget(), PRESET)
        r = on.result.search_stats["repair"]
        assert r["consistency_failures"] == 0 and r["stop"] in (
            "complete",
            "attempt_cap",
            "no_trace",
        )
        assert on.result.search_stats["quotes_executed"] == meter.counted
        if off.score is not None:
            assert on.result.score is not None and on.result.score >= off.score
            assert on.result.plan is not None
            full_fill(c, evaluate(real, c, on.result.plan, gross_only()))
        for rec in on.candidates:
            if "evaluated" in rec:
                assert rec["evaluated"] == rec["accounted"]


def test_repair_invariants_on_random_graphs() -> None:
    """Random CPMM multigraphs (dust to large, fee 0-100 bps, 2-4 hops, 1-13 chunks, quote
    and candidate budgets): never below the repair-off control, no consistency failure,
    full-fill replays, one quote ledger, and every accepted candidate's rebuilt sequence
    valued identically by the independent oracle."""
    rng = random.Random(20261003)
    accepted = 0
    for _ in range(200):
        pools, case, params, budget = random_instance(rng)
        bundle = cp_bundle(pools)
        off = repair_solve(case, context(bundle, params), budget, REPAIR_OFF).result
        with metered_quotes(None) as meter:
            on = repair_solve(case, context(bundle, params), budget, PRESET)
        res, r = on.result, on.result.search_stats["repair"]
        assert r["consistency_failures"] == 0
        assert res.search_stats["quotes_executed"] == meter.counted
        assert res.status is off.status
        if off.score is not None:
            assert res.score is not None and res.score >= off.score and res.plan is not None
            full_fill(case, evaluate(bundle, case, res.plan, gross_only()))
        for a in r["accepted_log"]:
            assert on.incumbent_run is not None
            engine, cand = replay_candidate(
                bundle, case, params, on.incumbent_run, a["checkpoint"], a["alternative"]
            )
            value = oracle_sequence(
                pools, 30, case, params["max_hops"], params["chunks"], pool_sequence(engine, cand)
            )
            assert value == int(a["score"])
            accepted += 1
    assert accepted > 0


# ================================================================ 11. checks: contract records


def _shared_validator() -> ModuleType:
    """The unchanged R021-C/1 validator, loaded read-only by path."""
    path = REPO / "tests" / "docs" / "test_research_021_contract.py"
    spec = importlib.util.spec_from_file_location("r021_contract_validator_1553", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def domain_record(
    bundle: SnapshotBundle, params: Mapping[str, int], cohort: str = "fixture"
) -> dict[str, Any]:
    """suffix-repair.md §6: the incremental candidate's (and every repair candidate's)
    feasible set; identical with repair on and off."""
    return {
        "schema": "r021.domain/1",
        "universe": {"bundle": bundle.bundle_hash, "cohort": cohort, "pools": list(bundle.pools)},
        "protocols": ["constant_product", "concentrated", "liquidity_book"],
        "pool_order": list(bundle.pools),
        "hops": {"max": params["max_hops"], "param": "search.max_hops"},
        "splits": {"max": params["chunks"], "param": "graph.chunks", "governs": "allocation"},
        "amount_grid": {
            "kind": "chunk_grid",
            "chunks": params["chunks"],
            "remainder": "floor_chunks",
        },
        "zero_output_leg": "infeasible",
        "token_reuse": "simple_path",
        "pool_reuse": "shared_merged",
        "dag_admission": "plan_token_dag",
        "full_fill": "v1_full_fill",
    }


def diagnostics_record(
    got: RepairResult, bundle: SnapshotBundle, params: Mapping[str, int]
) -> dict[str, Any]:
    s = got.result.search_stats
    r = s["repair"]
    domain = domain_record(bundle, params)
    source = s["chosen_source"]
    evaluated_incumbent = int(s["incremental_status"] in ("ok", "invalid_plan"))
    return {
        "schema": "r021.diagnostics/1",
        "contract": "R021-C/1",
        "algorithm": ALGORITHM,
        "domain": domain,
        "candidate_domain_hash": hashlib.sha256(
            json.dumps(domain, sort_keys=True).encode()
        ).hexdigest(),
        "certificate": None,
        "certificate_unavailable_reason": "not_produced",
        "max_candidates_unit": "paths_scored_per_chunk",
        "work": {
            "quotes_executed": s["quotes_executed"],
            "quotes_memoized": s["quotes_memoized"],
            "internal_evaluations": evaluated_incumbent + r["internal_evaluations"],
            "paths_scored": s["paths_scored"] + r["paths_scored"],
            "admission_checks": s["paths_scored"]
            + s["paths_rejected_cycle"]
            + r["paths_scored"]
            + r["paths_rejected_cycle"],
            "repair_attempts": r["repair_attempts"],
            "checkpoint_restores": r["checkpoint_restores"],
        },
        "fallback": {
            "used": source not in (incremental_graph.NAME, ALGORITHM),
            "source": source,
            "reason": None
            if source in (incremental_graph.NAME, ALGORITHM)
            else "retained_simpler_candidate",
        },
        "repair": {
            k: v
            for k, v in r.items()
            if k
            not in ("paths_scored", "paths_rejected_cycle", "paths_truncated", "marginal_failures")
        },
    }


def test_diagnostics_record_satisfies_the_unchanged_r021_validator() -> None:
    validator = _shared_validator()
    for name in ("structural_trap", "twin_pools"):
        bundle, case, spec = fixture_case(name)
        for options in (PRESET, REPAIR_OFF):
            with metered_quotes(None) as meter:
                got = repair_solve(case, context(bundle, spec["settings"]), Budget(), options)
            rec = diagnostics_record(got, bundle, spec["settings"])
            ctx = {
                "hard_killed": False,
                "quotes_counted": meter.counted,
                "run": {},
                "request": {},
                "objective": "gross_only",
                "status": got.result.status.value,
                "final_score": None if got.result.score is None else str(got.result.score),
            }
            assert validator.check_diagnostics(rec, ctx) == set(), (name, options)
            wrong = {**rec, "work": {**rec["work"], "quotes_executed": meter.counted - 1}}
            assert validator.check_diagnostics(wrong, ctx) == {"W_LEDGER"}
            bogus = {**rec, "max_candidates_unit": "repair_attempts"}
            assert validator.check_diagnostics(bogus, ctx) == {"W_MAX_CANDIDATES"}
    # repair on/off share one feasible set: same_domain comparison class.
    bundle, _, spec = fixture_case("structural_trap")
    assert domain_record(bundle, spec["settings"]) == domain_record(bundle, spec["settings"])


def test_memo_pins_the_preset_fixtures_and_probe_evidence() -> None:
    for key, value in PRESET.items():
        assert f"`{key}: {str(value).lower()}`" in MEMO, key
    for number in ("111,178,819", "90,545,314", "12,757,712", "111,193,897"):
        assert number in MEMO, number
    probe = FIX.get("probe")
    assert probe is not None and probe["spec_sha256"] == spec_sha256()
    for name, run in probe["runs"].items():
        assert run["worse"] == 0 and run["consistency_failures"] == 0, name
        assert run["improved"] + run["equal"] == run["cases"]
        assert run["cases"] == (24 if name.endswith("subset") else 96)
    preset = probe["runs"]["h3c50_structural_preset"]
    assert preset["settings"]["options"] == PRESET and preset["settings"]["rule"] == "structural"
    assert (preset["improved"], preset["cases_gain_ge_0_1_bps"]) == (23, 1)
    assert "23 / 73 / **0**" in MEMO and "9.776 bps" in MEMO


# ================================================================ bounded tuning probe (not a test)


def _probe(argv: Sequence[str]) -> None:
    """`probe <bundle> <out.json> [--hops N] [--chunks N] [--rule R] [--attempts N]
    [--checkpoints N] [--alternatives N] [--max-quotes N] [case ids...]`: per case, the
    repair-off control and the repair-on specification on the same bundle, settings and
    budget (a separate diagnostic pass; no timing is recorded or claimed)."""
    args = list(argv)
    bundle_dir, out = args.pop(0), args.pop(0)
    hops, chunks, rule, max_quotes = 3, 50, "structural", 300_000
    options = dict(PRESET)
    ids: list[str] = []
    while args:
        a = args.pop(0)
        if a == "--hops":
            hops = int(args.pop(0))
        elif a == "--chunks":
            chunks = int(args.pop(0))
        elif a == "--rule":
            rule = args.pop(0)
        elif a == "--max-quotes":
            max_quotes = int(args.pop(0))
        elif a == "--attempts":
            options["max_repair_attempts"] = int(args.pop(0))
        elif a == "--checkpoints":
            options["max_checkpoints"] = int(args.pop(0))
        elif a == "--alternatives":
            options["alternatives_per_checkpoint"] = int(args.pop(0))
        else:
            ids.append(a)
    bundle = load_bundle(Path(bundle_dir))
    params = {"max_hops": hops, "max_splits": 4, "percent_step": 5, "chunks": chunks}
    prepared = prepare_ig(bundle, params)
    budget = Budget(max_quotes=max_quotes)
    rows: list[dict[str, Any]] = []
    for case in [c for c in bundle.cases if not ids or c.case_id in ids]:
        ctx = SolveContext(bundle, gross_only(), prepared)
        off = repair_solve(case, ctx, budget, {**options, "repair": False})
        on = repair_solve(case, ctx, budget, options, rule=rule)
        so, sn = off.result.search_stats, on.result.search_stats
        r = sn["repair"]
        run = on.incumbent_run
        row: dict[str, Any] = {
            "case_id": case.case_id,
            "status_off": off.result.status.value,
            "status_on": on.result.status.value,
            "score_off": None if off.result.score is None else str(off.result.score),
            "score_on": None if on.result.score is None else str(on.result.score),
            "source_off": so["chosen_source"],
            "source_on": sn["chosen_source"],
            "incremental_status": sn["incremental_status"],
            "decisions": None if run is None else len(run.decisions),
            "structural_decisions": None
            if run is None
            else sum(bool(d.new_edges) for d in run.decisions),
            "quotes_off": so["quotes_executed"],
            "quotes_on": sn["quotes_executed"],
            "paths_scored_off": so["paths_scored"],
            "repair": {k: v for k, v in r.items() if k != "accepted_log"},
            "accepted_log": r["accepted_log"],
            "outcomes": [c.get("outcome", c.get("run_status")) for c in on.candidates],
        }
        rows.append(row)
        print(
            json.dumps(
                {k: row[k] for k in ("case_id", "score_off", "score_on", "quotes_off", "quotes_on")}
            ),
            flush=True,
        )
    doc = {
        "pass": "WHI-1553 bounded tuning probe (separate diagnostic pass; not a measured solve)",
        "bundle_hash": bundle.bundle_hash,
        "settings": {**params, "rule": rule, "options": options, "budget": budget.to_dict()},
        "spec_sha256": spec_sha256(),
        "rows": rows,
    }
    Path(out).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def spec_sha256() -> str:
    """SHA-256 of the specification section of this module (binds probe evidence)."""
    text = Path(__file__).read_text(encoding="utf-8")
    start = text.index(
        "# ================================================================ 1. the specification"
    )
    end = text.index(
        "# ================================================================ 2. independent oracle"
    )
    return hashlib.sha256(text[start:end].encode()).hexdigest()


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "probe":
        _probe(sys.argv[2:])
    else:
        sys.exit(
            "usage: PYTHONPATH=. python test_suffix_repair_contract.py probe <bundle> <out> ..."
        )

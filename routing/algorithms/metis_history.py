"""`metis_history` (WHI-1550, R021-P04; contract R021-C/1 §2 row 1): `metis_inspired`'s
aggregate-flow chunk allocation with a **history/admission-aware** per-chunk label search.
Normative specification: `docs/references/research-021/history-labels.md` §4-§8 (WHI-1549,
`narrow_go`); its executable form is `choose_history` in
`tests/routing/test_history_labels_contract.py`. An inferred experimental extension of this
repository's Metis-inspired variant -- NOT Jupiter Metis, no production equivalence, no
optimality, whole-plan improvement or latency claim. `custom` group, opt-in.

**What changes against `metis_inspired`** (only what a layer retains). Labels are grouped by
the exact signature `(layer, token, visited set)` (a frozenset key compared by equality;
lemma L2: with the committed token DAG fixed, a prefix's admissible continuations depend
only on these). Inside a group a new label is discarded when an existing one has the **same
amount** (identical futures in every protocol, lemma L1), or -- only in a **non-final**
chunk whose continuation region is **certified upward safe** (every edge a constant-product
pool passing the static `uint112` inflow bound, lemma L3) -- a strictly larger amount (the
smaller label is then removed). Every other same-signature label is **retained**
(`labels_retained_unknown`): concentrated/liquidity-book and overflow-exposed CPMM regions are
never assumed monotone. Two resource caps drop labels **visibly** (never silently):
`max_labels_per_signature` (the lowest amount, then latest generation, of the group plus the
new label goes) and `max_frontier_labels` (a full layer refuses the new label; nothing is
evicted across tokens). A chunk with any cap drop is *state-capped*: the per-chunk exactness
of T1 (history-labels.md §3.5) does not apply to it. With the bounded preset (one label per
signature) the selector is therefore a **declared approximation** on uncertified regions.
`dominance: off` gives every label its own key (the disabled-mechanism control; caps still
apply) and chooses exactly the same-depth enumeration's first maximum when untruncated.

**Everything else is `metis_inspired`'s** (reused, unchanged): expansion order (labels in
generation order, edges in bundle adjacency order), the source/distance/token-simple filters,
`creates_cycle` admission before every quote, one relaxation = one guarded edge quote on the
pool's original state at `x_pool + amount` (`_Allocator.step`), the strict first-found
target choice, chunk carry, `commit`, `merged_plan`, the in-solve `evaluate`, the retained
`path_split` candidate at `search.max_hops` (published first, replaced only by a strictly
better complete, independently evaluated plan) and the statuses. Committed aggregate flows
`F` and edges `E` stay fixed during a chunk's choice (a continuation's pools are disjoint
from its prefix's, lemma L0); prefix flows matter only after commit.

**One solve, one ledger.** The whole solve runs inside `routing.evaluator.counted_evaluations`
(the embedded `path_split` / `single_path` / `direct_split` replays included):
`search_stats["evaluations"] = {fallback, incremental, total}` and the R021 unit
`internal_evaluations` is `total`. One `QuoteCache` and one guarded quote check (the worker
meter) serve the fallback, the label search and the in-solve replay; nothing is reset or
retried. `Budget.max_candidates` caps label relaxations per chunk (declared truncation, the
chunk keeps its best); `Budget.max_quotes` abandons the incremental plan (the published
fallback stands). A hard kill by the runner keeps only the last published plan as
`last_valid_candidate`; no certificate exists (`certificate: null`, `not_produced`).
Statuses are `metis_inspired`'s; `truncated_by` names `max_quotes`, then `max_candidates`,
then `state_cap` (a cap never turns a valid plan into anything but `ok`). A search that found
no valid plan after a cap dropped labels is `timeout` (a declared limit), never `no_route`,
which stays reserved for a complete search (R021-C/1 §10).

**Settings.** `graph.chunks`, `graph.label_hops` (>= `search.max_hops`) and `search.*` from
the shared profile; `graph.label_pruning` is **not read**. `algorithm_options.metis_history`
(WHI-1548 seam, history-labels.md §6): `dominance` (`history` | `off`),
`max_labels_per_signature` (1..1,000,000), `max_frontier_labels` (1..10,000,000), all
required, no solver defaults. Bounded preset v1 (`PRESET`): `history / 1 / 1024`.

**Bound pruning** (WHI-1600, `docs/references/research-022/pruning-contract.md` §6, rules M1/M2;
default **off**). `solve(..., bound_pruning=True)` -- reached only through the registered
strategy `metis_history_bounded`, never by a profile or the reference factory -- skips a
relaxation whose arrival bound (`routing.algorithms.chunk_pruning`) is at most the chunk's best
marginal so far. **M1** (a relaxation into the target) is always on: an arrival creates no label, so
dominance, both caps, the layer order and the label counters are untouched, even on a chunk the
reference caps. **M2** (a relaxation into another token, bounded by the per-solve `U_h` table) runs
only behind the structural gate `G_M2` (§6.3): outside it a removed label could change dominance or
a cap for the others (§6.4 reproduces a better-than-reference plan), so it is never enabled. A
skipped relaxation is still a relaxation (`max_candidates` points stay put), makes no quote and is
never a failure. Pruning needs the retained `path_split` candidate (P0). With the parameter off
there is no `bound_pruning` key and no extra prepared object.

**Diagnostics are not the solve.** `diagnose_history` (history-labels.md §9.2) is a separate,
unbudgeted correctness pass with its own caches and counters; the registry, runner and
profiles call `solve` only.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pools.bounds import BoundTable, build_bounds
from pools.constant_product import SOURCES
from pools.result import SwapResult
from routing.algorithms import chunk_pruning, incremental_graph, metis_inspired, path_split
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
    option_choice,
    option_int,
    require_option_keys,
    validated_options,
)
from routing.algorithms.direct_split_certified import cohort_of
from routing.algorithms.incremental_graph import (
    PreparedIncrementalGraph,
    chunk_amounts,
    creates_cycle,
    merged_plan,
    topology,
)
from routing.algorithms.metis_inspired import Choice, Label, hops_to_target
from routing.evaluator import (
    EvalStatus,
    Evaluation,
    EvaluationCounter,
    counted_evaluations,
    evaluate,
)
from routing.plan import RoutePlan
from routing.search import GraphIndex, Path, QuoteCache, enumerate_paths, path_label
from snapshot.models import Case, ConstantProductPoolState, PoolState, SnapshotBundle

NAME = "metis_history"
BOUNDED_NAME = "metis_history_bounded"  # the strategy that runs `solve(bound_pruning=True)`
BOUND_CONTRACT = "R022-Q02/1"  # the pruning contract rules M1/M2 come from
REFERENCE = metis_inspired.NAME

CAPABILITIES = metis_inspired.CAPABILITIES
SEARCH_PARAMS = metis_inspired.SEARCH_PARAMS
GRAPH_PARAMS = ("chunks", "label_hops")  # graph.label_pruning is not read

DOMINANCE = ("history", "off")
OPTION_RANGES: dict[str, tuple[int, int]] = {
    "max_labels_per_signature": (1, 1_000_000),
    "max_frontier_labels": (1, 10_000_000),
}
OPTION_KEYS = frozenset(("dominance", *OPTION_RANGES))
# The bounded comparison preset (R021-C/1 §7.1, history-labels.md §6), frozen by its bytes.
PRESET: dict[str, Any] = {
    "path": "config/metis_history/preset_v1.yaml",
    "sha256": "f4510b5181b1152786b0a637cd462734e31988b7890487c6ee6d396d37965799",
    "key": "R021-P04-metis_history",
    "version": 1,
}

PROVENANCE: Mapping[str, Any] = MappingProxyType(
    {
        "experimental": True,
        "opt_in": True,
        "issue": "WHI-1550",
        "identity": (
            "history/admission-aware label-search extension of the Metis-inspired "
            "experimental Python variant; inferred experimental extension, NOT Jupiter Metis; "
            "no production equivalence, global-optimality, whole-plan or latency claim"
        ),
        "contract": (
            "docs/references/research-021/history-labels.md §4-§8 (WHI-1549, R021-P03, "
            "narrow_go; PR #59 b9e7310ea73b2cfa4a3388b80644199e5188e1d9)"
        ),
        "reference": REFERENCE,
        "inferred": [
            "label signature (layer, token, visited set), exact equality",
            "equal-amount discard in every protocol and chunk (lemma L1)",
            "strictly-larger-amount discard only in non-final chunks on statically certified "
            "upward-safe constant-product continuation regions (lemma L3)",
            "every other same-signature label retained (unknown => retain)",
        ],
        "approximations": [
            "max_labels_per_signature / max_frontier_labels drop labels visibly; a capped "
            "chunk carries no exactness claim (the bounded preset caps one label per signature)",
        ],
        "not_claimed": [
            "whole-plan optimality or improvement over metis_inspired / incremental_graph",
            "identical tie choices or trajectories with the same-depth enumeration",
            "concentrated / liquidity-book monotonicity (not certified)",
            "any speedup",
        ],
        "control": "algorithm_options dominance: off (every label kept; caps still apply)",
    }
)


class MetisHistoryConfigError(ValueError):
    """`prepare` received an invalid or missing `search.*` / `graph.*` value."""


def validate_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """The `options_validator` (history-labels.md §6): all three keys required, unknown keys
    refused (reserved keys are refused by `validated_options` first), `dominance` one of
    `DOMINANCE`, the caps integers (never bools or floats) in range."""
    require_option_keys(options, set(OPTION_KEYS))
    if options["dominance"] is False:
        raise ValueError('dominance: got false (YAML reads a bare off as false; write "off")')
    out: dict[str, Any] = {"dominance": option_choice(options["dominance"], "dominance", DOMINANCE)}
    for key, (lo, hi) in OPTION_RANGES.items():
        out[key] = option_int(options[key], key, lo, hi)
    return out


def upward_safe_edges(bundle: SnapshotBundle) -> frozenset[tuple[str, str]]:
    """Directed edges `(pool_id, token_in)` whose chunk marginal is certified upward safe
    (history-labels.md §3.4): constant product, and either no overflow rule (no source), an
    input-independent failure (unknown source, fee mismatch, an empty side), or a sourced
    pair whose own `token_in` reserve plus every other pool's reserve of that token stays
    below `2^reserve_bits`, every other holder being constant product (a CPMM's aggregate
    output of a token is below its reserve; a CL/LB holder has no such bound: unknown => not
    certified)."""
    held: dict[str, dict[str, int | None]] = {}  # token -> pool -> its reserve of the token
    for pool in bundle.pools.values():
        for token in (pool.token0, pool.token1):
            reserve = (
                pool.reserves_for(token)[0] if isinstance(pool, ConstantProductPoolState) else None
            )
            held.setdefault(token, {})[pool.pool_id] = reserve
    safe: set[tuple[str, str]] = set()
    for pool in bundle.pools.values():
        if not isinstance(pool, ConstantProductPoolState):
            continue
        for u in (pool.token0, pool.token1):
            r_in, r_out = pool.reserves_for(u)
            source = SOURCES.get(pool.source_key) if pool.source_key is not None else None
            if source is None or pool.fee_bps != source.fee_bps or r_in <= 0 or r_out <= 0:
                safe.add((pool.pool_id, u))
                continue
            others = [r for pid, r in held[u].items() if pid != pool.pool_id]
            if all(r is not None for r in others) and r_in + sum(
                r for r in others if r is not None
            ) < (1 << source.reserve_bits):
                safe.add((pool.pool_id, u))
    return frozenset(safe)


@dataclasses.dataclass(frozen=True)
class PreparedMetisHistory:
    """Immutable per-worker preparation: `incremental_graph`'s prepared object (graph index,
    fallback hop bound, grid, chunks), the label hop bound, the certified upward-safe edges
    and the validated read-only options."""

    graph: PreparedIncrementalGraph
    label_hops: int
    safe_edges: frozenset[tuple[str, str]]
    options: Mapping[str, Any]

    @property
    def index(self) -> GraphIndex:
        return self.graph.path_split.single_path.index

    @property
    def max_hops(self) -> int:
        return self.graph.path_split.single_path.max_hops


@dataclasses.dataclass(frozen=True)
class PreparedBoundedMetisHistory(PreparedMetisHistory):
    """`PreparedMetisHistory` plus the eagerly built, immutable output-bound table of the frozen
    bundle (`pools.bounds`). Only `prepare_bounded` returns it; the reference `prepare` never
    does (contract §10.2: no new object in the prepared result with the parameter off)."""

    bound_table: BoundTable


def prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> PreparedMetisHistory:
    options = validated_options(FACTORY, config.options)  # the public entry validates too
    hops = config.params.get("label_hops")
    if not isinstance(hops, int) or isinstance(hops, bool) or hops < 1:
        raise MetisHistoryConfigError(
            f"{NAME} requires graph.label_hops as an integer >= 1, got {hops!r}"
        )
    try:  # the reused legacy prepare refuses options: it gets the same config without them
        graph = incremental_graph.prepare(bundle, dataclasses.replace(config, options={}))
    except incremental_graph.IncrementalGraphConfigError as exc:
        raise MetisHistoryConfigError(f"{NAME}: {exc}") from exc
    max_hops = graph.path_split.single_path.max_hops
    if hops < max_hops:
        raise MetisHistoryConfigError(
            f"{NAME} requires graph.label_hops >= search.max_hops ({max_hops}), got {hops}"
        )
    return PreparedMetisHistory(
        graph, hops, upward_safe_edges(bundle), MappingProxyType(dict(options))
    )


def prepare_bounded(bundle: SnapshotBundle, config: AlgorithmConfig) -> PreparedBoundedMetisHistory:
    """The reference preparation plus every pool direction's output bound (charged to the
    preparation step, contract §10.3)."""
    base = prepare(bundle, config)
    return PreparedBoundedMetisHistory(
        base.graph, base.label_hops, base.safe_edges, base.options, build_bounds(bundle)
    )


# ------------------------------------------------------------------ the selector (§4)


@dataclasses.dataclass
class Work:
    """The selector's monotone counters: R021-C/1 §5.2 units (`label_relaxations`,
    `admission_checks`, `state_comparisons`, `labels_discarded_dominance`,
    `labels_retained_unknown`, `peak_frontier_labels`) and plain extras (cap drops, capped
    chunks, `peak_signature_labels`, `certified_strict_insertions`)."""

    label_relaxations: int = 0
    admission_checks: int = 0
    state_comparisons: int = 0
    labels_discarded_dominance: int = 0
    labels_retained_unknown: int = 0
    peak_frontier_labels: int = 0
    labels_dropped_signature_cap: int = 0
    labels_dropped_frontier_cap: int = 0
    certified_strict_insertions: int = 0
    peak_signature_labels: int = 0
    chunks_state_capped: int = 0

    def drops(self) -> int:
        return self.labels_dropped_signature_cap + self.labels_dropped_frontier_cap


def region_certified(
    index: GraphIndex,
    safe: frozenset[tuple[str, str]],
    v: str,
    remaining: int,
    source: str,
    target: str,
    memo: dict[tuple[str, int], bool],
) -> bool:
    """Every directed edge on a walk of at most `remaining` pools from `v` that never enters
    `source` and never leaves `target` (a superset of the token-simple continuations) is in
    `safe`. `memo` is per solve (the region depends on the request's source and target)."""
    key = (v, remaining)
    if key not in memo:
        ok, frontier = True, {v}
        for _ in range(remaining):
            nxt: set[str] = set()
            for t in frontier:
                if t == target:
                    continue
                for e in index.edges_from(t):
                    if e.token_out == source:
                        continue
                    if (e.pool_id, e.token_in) not in safe:
                        ok = False
                        break
                    nxt.add(e.token_out)
                if not ok:
                    break
            if not ok:
                break
            frontier = nxt
        memo[key] = ok
    return memo[key]


def choose_history(
    alloc: metis_inspired._Allocator,
    amount: int,
    hops: int,
    dist: Mapping[str, int],
    budget: Budget,
    options: Mapping[str, Any],
    *,
    final: bool,
    safe: frozenset[tuple[str, str]],
    region: dict[tuple[str, int], bool],
    work: Work,
    pruner: chunk_pruning.LabelPruner | None = None,
) -> Choice | None:
    """One chunk of the selector (history-labels.md §4, rules R1-R8), on `alloc`'s committed
    flows and token edges, with `metis_inspired`'s counters (`relaxations`, `label_cycle`,
    `label_distance`, `label_revisit`, `label_truncated`, failures) kept in their meaning."""
    source, target = alloc.case.token_in, alloc.case.token_out
    off = options["dominance"] == "off"
    sig_cap, frontier_cap = options["max_labels_per_signature"], options["max_frontier_labels"]
    layer: list[Label] = [Label(amount, (), (), frozenset((source,)))]
    best: Choice | None = None
    relaxed = 0
    for k in range(1, hops + 1):
        groups: dict[Any, dict[int, tuple[int, Label]]] = {}  # key -> amount -> (order, label)
        order = size = 0
        for lab in layer:  # generation order == enumeration order
            t = lab.path[-1].token_out if lab.path else source
            for e in alloc.index.edges_from(t):  # adjacency order
                v = e.token_out
                if v == source:
                    continue
                if v != target and dist.get(v, hops + 1) > hops - k:
                    alloc.label_distance += 1
                    continue
                if v in lab.tokens:
                    alloc.label_revisit += 1
                    continue
                p = (*lab.path, e)
                work.admission_checks += 1
                if creates_cycle(alloc.token_edges, p):
                    alloc.label_cycle += 1
                    continue
                if budget.max_candidates is not None and relaxed >= budget.max_candidates:
                    alloc.truncated_by = alloc.truncated_by or "max_candidates"
                    alloc.label_truncated += 1
                    return best
                relaxed += 1
                alloc.relaxations += 1
                work.label_relaxations += 1
                if (  # rules M1/M2: every arrival below this relaxation is <= the chunk's best
                    pruner is not None
                    and best is not None
                    and lab.amount > 0  # a zero input makes no pool call: nothing to save
                    and pruner.skip(e, lab.amount, hops - k, best[0])
                ):
                    continue
                result = alloc.step(e, lab.amount, k)
                if isinstance(result, metis_inspired._Failure):
                    alloc._count(result, p)
                    continue
                m, update = result
                if v == target:
                    if best is None or m > best[0]:  # strict: ties keep the earlier
                        best = (m, p, [*lab.updates, update])
                    continue
                new = Label(m, p, (*lab.updates, update), lab.tokens | {v})
                order += 1
                key: Any
                if off:
                    key, strict = order, False
                else:
                    key = (v, new.tokens)
                    strict = not final and region_certified(
                        alloc.index, safe, v, hops - k, source, target, region
                    )
                group = groups.setdefault(key, {})
                if group:  # R1
                    work.state_comparisons += 1
                    if m in group:  # R2: same signature, same amount -> identical futures
                        work.labels_discarded_dominance += 1
                        continue
                    if strict:  # R3: a certified strict group holds at most one label
                        ((_, old),) = group.values()
                        work.labels_discarded_dominance += 1
                        if old.amount > m:
                            continue
                        group.clear()
                        size -= 1
                if strict:
                    work.certified_strict_insertions += 1
                if len(group) >= sig_cap:  # R6: drop the lowest (amount, latest generation)
                    worst = min(group.values(), key=lambda g: (g[1].amount, -g[0]))
                    work.labels_dropped_signature_cap += 1
                    if worst[1].amount > m:
                        continue
                    del group[worst[1].amount]
                    size -= 1
                if size >= frontier_cap:  # R7: a full layer refuses the new label
                    work.labels_dropped_frontier_cap += 1
                    continue
                work.labels_retained_unknown += bool(group)  # R4: beside an unprovable label
                group[m] = (order, new)
                size += 1
                work.peak_signature_labels = max(work.peak_signature_labels, len(group))
        layer = [lab for _, lab in sorted(g for group in groups.values() for g in group.values())]
        work.peak_frontier_labels = max(work.peak_frontier_labels, len(layer))
    return best


# ------------------------------------------------------------------ solve


def _termination(truncated_by: str | None, capped: int) -> str:
    """The per-chunk mechanism's termination (history-labels.md §7), by precedence."""
    if truncated_by == "max_quotes":
        return "quote_budget"
    if truncated_by == "max_candidates":
        return "candidate_cap"
    return "state_cap" if capped else "complete"


def solve(
    case: Case, context: SolveContext, budget: Budget, *, bound_pruning: bool = False
) -> SolveResult:
    """`bound_pruning=True` enables rules M1/M2 (module docstring); it needs the
    `PreparedBoundedMetisHistory` of `prepare_bounded`."""
    prepared = context.prepared
    if not isinstance(prepared, PreparedMetisHistory):
        raise TypeError(f"{NAME}.solve needs the PreparedMetisHistory returned by prepare()")
    if bound_pruning and not isinstance(prepared, PreparedBoundedMetisHistory):
        raise TypeError(
            f"{NAME}.solve(bound_pruning=True) needs the PreparedBoundedMetisHistory "
            "returned by prepare_bounded()"
        )
    with counted_evaluations() as evaluations:  # the whole solve, fallback replays included
        return _solve(case, context, budget, prepared, evaluations, bound_pruning)


def _solve(
    case: Case,
    context: SolveContext,
    budget: Budget,
    prepared: PreparedMetisHistory,
    evaluations: EvaluationCounter,
    bound_pruning: bool = False,
) -> SolveResult:
    bundle, objective, opts = context.bundle, context.objective, prepared.options
    ps_prepared = prepared.graph.path_split
    max_hops, hops = prepared.max_hops, prepared.label_hops
    cache = QuoteCache(bundle)
    quiet = dataclasses.replace(context, candidate_sink=None)

    # ---- 1. the retained simpler candidate: path_split at search.max_hops (published).
    ps = path_split.solve(
        case, dataclasses.replace(quiet, prepared=ps_prepared), budget, cache=cache
    )
    best: tuple[str, RoutePlan, Evaluation, int, list[Path]] | None = None
    if ps.status is SolveStatus.OK and ps.plan and ps.evaluation and ps.score is not None:
        best = (
            str(ps.search_stats.get("chosen_source") or path_split.NAME),
            ps.plan,
            ps.evaluation,
            ps.score,
            metis_inspired._source_paths(ps.evaluation),
        )
        context.report_candidate(ps.plan)
    after_fallback = evaluations.count

    # ---- 2. incremental chunk allocation with the history selector (same cache and meter).
    def guarded(state: PoolState, token_in: str, amount: int) -> SwapResult[PoolState]:
        memo_hit = bundle.pools.get(state.pool_id) is state and cache.cached(
            state.pool_id, token_in, amount
        )
        if budget.max_quotes is not None and cache.misses >= budget.max_quotes and not memo_hit:
            alloc.truncated_by = "max_quotes"
            raise metis_inspired._BudgetExhausted
        return cache(state, token_in, amount)

    index = prepared.index
    alloc = metis_inspired._Allocator(bundle, case, index, guarded)
    work = Work()
    region: dict[tuple[str, int], bool] = {}  # per solve: depends on the request
    dist = hops_to_target(index, case.token_in, case.token_out)
    reachable = case.token_in != case.token_out and dist.get(case.token_in, hops + 1) <= hops
    pruner: chunk_pruning.LabelPruner | None = None
    table: BoundTable | None = None
    m2_gate, m2_table = "n/a", None
    if bound_pruning:
        assert isinstance(prepared, PreparedBoundedMetisHistory)
        table = prepared.bound_table
        if best is not None:  # P0 (contract §5.2): the retained simpler candidate exists
            if reachable:
                m2_gate, labels_exist = chunk_pruning.m2_gate(
                    index, case.token_in, case.token_out, hops, dist, opts
                )
                if m2_gate == "open" and labels_exist:  # M2 only behind G_M2, and only if useful
                    m2_table = chunk_pruning.UTable(
                        index, case.token_in, case.token_out, hops, table.bounds
                    )
            pruner = chunk_pruning.LabelPruner(table.bounds, alloc.flows, case.token_out, m2_table)
    amounts = chunk_amounts(case.amount_in, prepared.graph.chunks)
    chunk_paths: list[Path] = []
    chunk_inputs: list[int] = []
    carried = 0
    incremental_status = "not_run"
    incremental: tuple[RoutePlan, Evaluation, int] | None = None
    accounted_gross: int | None = None
    try:
        if not reachable:
            incremental_status = "no_paths"
            raise metis_inspired._ChunkFailed(metis_inspired._Failure("no_paths", 0))
        last = max(k for k, a in enumerate(amounts) if a > 0)
        carry = 0
        for k, chunk in enumerate(amounts):
            if chunk == 0:
                continue
            amount = carry + chunk
            drops = work.drops()
            choice = choose_history(
                alloc,
                amount,
                hops,
                dist,
                budget,
                opts,
                final=k == last,
                safe=prepared.safe_edges,
                region=region,
                work=work,
                pruner=pruner,
            )
            work.chunks_state_capped += work.drops() > drops  # R8
            if k != last and (choice is None or choice[0] == 0):
                carry, carried = amount, carried + 1
                continue
            if choice is None:
                incremental_status = f"chunk_{k + 1}_no_admissible_path"
                raise metis_inspired._ChunkFailed(metis_inspired._Failure(incremental_status, 0))
            carry = 0
            _, path, updates = choice
            alloc.commit(path, updates)
            chunk_paths.append(path)
            chunk_inputs.append(amount)

        # ---- 3. normalize (one merged step per pool) and re-evaluate the complete plan.
        accounted_gross = sum(
            f.amount_out for f in alloc.flows.values() if f.edge.token_out == case.token_out
        )
        plan = merged_plan(case, alloc.flows.values())
        ev = evaluate(bundle, case, plan, objective, quote=guarded)
        if ev.status is EvalStatus.OK:
            incremental_status = "ok"
            incremental = (plan, ev, objective.score(ev))
        else:
            incremental_status = "invalid_plan"
            alloc.failures["invalid_plan"] = alloc.failures.get("invalid_plan", 0) + 1
    except metis_inspired._BudgetExhausted:  # abandons the incremental plan; best stays
        incremental_status = "truncated"
        alloc.label_truncated += 1
    except metis_inspired._ChunkFailed:
        pass

    if incremental is not None and (best is None or incremental[2] > best[3]):
        plan, ev, score = incremental
        best = (NAME, plan, ev, score, chunk_paths)
        context.report_candidate(plan)
    total = evaluations.count

    by_path: dict[Path, list[int]] = {}
    for path, amount in zip(chunk_paths, chunk_inputs, strict=True):
        entry = by_path.setdefault(path, [0, 0])
        entry[0] += 1
        entry[1] += amount
    shared: dict[str, int] = {}
    for path in by_path:
        for e in path:
            shared[e.pool_id] = shared.get(e.pool_id, 0) + 1
    hop_histogram: dict[str, int] = {}
    for path in chunk_paths:
        hop_histogram[str(len(path))] = hop_histogram.get(str(len(path)), 0) + 1
    own_cut = alloc.truncated_by  # max_quotes (precedence) or max_candidates
    capped = work.chunks_state_capped
    truncated_by = own_cut or ("state_cap" if capped else None)
    stats: dict[str, Any] = {
        "chunks": prepared.graph.chunks,
        "chunks_empty": sum(a == 0 for a in amounts),
        "chunks_carried": carried,
        "chunks_allocated": len(chunk_paths),
        "max_hops": max_hops,
        "max_splits": ps_prepared.direct_split.max_splits,
        "percent_step": ps_prepared.direct_split.percent_step,
        "paths_enumerated": None,  # path units: not this label search's (metis_inspired rule)
        "paths_scored": None,
        "paths_rejected_cycle": None,
        "paths_truncated": None,
        "marginal_failures": dict(sorted(alloc.failures.items())),
        "marginal_incomplete": len(alloc.incomplete),
        "incomplete_example": alloc.incomplete[0] if alloc.incomplete else None,
        "incremental_status": incremental_status,
        "incremental_allocation": [
            {"path": path_label(p), "chunks": n, "amount_in": str(a)}
            for p, (n, a) in by_path.items()
        ],
        "incremental_chunk_sequence": [list(by_path).index(p) for p in chunk_paths],
        "incremental_shared_pools": sorted(p for p, n in shared.items() if n > 1),
        "incremental_topology": topology(by_path) if by_path else None,
        "incremental_accounted_gross": None if accounted_gross is None else str(accounted_gross),
        "incremental_evaluated_gross": (
            None if incremental is None else str(incremental[1].gross_output)
        ),
        "incremental_score": None if incremental is None else str(incremental[2]),
        "accounting_matches_evaluation": (
            None if incremental is None else accounted_gross == incremental[1].gross_output
        ),
        "path_split_status": ps.status.value,
        "path_split_score": None if ps.score is None else str(ps.score),
        "path_split_source": ps.search_stats.get("chosen_source"),
        "single_path_score": ps.search_stats.get("single_path_score"),
        "direct_split_score": ps.search_stats.get("direct_split_score"),
        "truncated_by": truncated_by or ps.search_stats.get("truncated_by"),
        "truncated_stages": ps.search_stats.get("truncated_stages", [])
        + ([NAME] if truncated_by else []),
        "quotes_executed": cache.misses,
        "quotes_memoized": cache.hits,
        "chosen_source": None,
        "topology": None,
        # ---- the label search (metis_inspired's keys and meanings, label mode)
        "label_hops": hops,
        "chunk_search": "history_label",
        "candidate_unit": "label_relaxation",
        "label_relaxations": alloc.relaxations,
        "label_rejected_cycle": alloc.label_cycle,
        "label_pruned_distance": alloc.label_distance,
        "label_skipped_revisit": alloc.label_revisit,
        "label_truncated_chunks": alloc.label_truncated,
        "chunk_path_hops": dict(sorted(hop_histogram.items())),
        # ---- metis_history's own (plain extras beside the r021 units)
        "dominance": opts["dominance"],
        "max_labels_per_signature": opts["max_labels_per_signature"],
        "max_frontier_labels": opts["max_frontier_labels"],
        "labels_dropped_signature_cap": work.labels_dropped_signature_cap,
        "labels_dropped_frontier_cap": work.labels_dropped_frontier_cap,
        "chunks_state_capped": capped,
        "peak_signature_labels": work.peak_signature_labels,
        "certified_strict_insertions": work.certified_strict_insertions,
        "termination": _termination(own_cut, capped),
        "evaluations": {
            "fallback": after_fallback,
            "incremental": total - after_fallback,
            "total": total,
        },
    }
    if table is not None:
        stats["bound_pruning"] = {
            "contract": BOUND_CONTRACT,
            "reference": NAME,
            "rule": "M1+M2" if m2_table is not None else "M1",
            "pruned_bound": pruner.pruned_bound if pruner else 0,
            "bound_evaluations": pruner.bound_evaluations if pruner else 0,
            "bound_no_bound": pruner.bound_no_bound if pruner else 0,
            "bound_table_cost": m2_table.cost if m2_table is not None else 0,
            "prepare": table.prepare_record(),
            "p0": pruner is not None,
            "m2": {"active": m2_table is not None, "gate": m2_gate},
            "exactness": incremental_graph.budget_exactness(
                own_cut, ps.search_stats.get("truncated_by")
            ),
        }
    common: dict[str, Any] = {
        "case_id": case.case_id,
        "algorithm": NAME,
        "candidates_considered": ps.candidates_considered + alloc.relaxations,
        "candidates_truncated": ps.candidates_truncated + alloc.label_truncated,
        "search_stats": stats,
    }
    source = best[0] if best is not None else None
    stats["r021"] = _diagnostics(
        bundle, prepared, stats, work, source, BOUNDED_NAME if bound_pruning else NAME
    )
    if best is not None:
        _, plan, ev, score, route_paths = best
        stats["chosen_source"] = source
        stats["topology"] = topology(route_paths)
        return SolveResult(status=SolveStatus.OK, plan=plan, evaluation=ev, score=score, **common)
    if truncated_by is not None or ps.status is SolveStatus.TIMEOUT:
        return SolveResult(
            status=SolveStatus.TIMEOUT,
            error=(
                f"declared {stats['truncated_by']} limit truncated the search with no valid "
                "route -- not evidence of no_route"
            ),
            **common,
        )
    if alloc.incomplete or ps.status is SolveStatus.INCOMPLETE_SNAPSHOT:
        example = alloc.incomplete[0] if alloc.incomplete else ps.error
        return SolveResult(
            status=SolveStatus.INCOMPLETE_SNAPSHOT,
            error=f"no valid route; candidates need uncollected pool state, e.g. {example}",
            **common,
        )
    return SolveResult(status=SolveStatus.NO_ROUTE, error=ps.error, **common)


# ------------------------------------------------------------------ R021 diagnostics


def domain(bundle: SnapshotBundle, prepared: PreparedMetisHistory) -> dict[str, Any]:
    """history-labels.md §5: the incremental candidate's feasible set (identical for
    `dominance` history and off, and for every cap value)."""
    pools = list(bundle.pools)
    chunks = prepared.graph.chunks
    return {
        "schema": "r021.domain/1",
        "universe": {
            "bundle": bundle.bundle_hash,
            "cohort": cohort_of(bundle),
            "pools": pools,
        },
        "protocols": ["constant_product", "concentrated", "liquidity_book"],
        "pool_order": pools,
        "hops": {"max": prepared.label_hops, "param": "graph.label_hops"},
        "splits": {"max": chunks, "param": "graph.chunks", "governs": "allocation"},
        "amount_grid": {"kind": "chunk_grid", "chunks": chunks},
        "zero_output_leg": "infeasible",
        "token_reuse": "simple_path",
        "pool_reuse": "shared_merged",
        "dag_admission": "plan_token_dag",
        "full_fill": "v1_full_fill",
    }


def _diagnostics(
    bundle: SnapshotBundle,
    prepared: PreparedMetisHistory,
    stats: Mapping[str, Any],
    work: Work,
    source: str | None,
    algorithm: str = NAME,
) -> dict[str, Any]:
    """The `r021.diagnostics/1` record (history-labels.md §5, §7, §8): no bound is claimed."""
    dom = domain(bundle, prepared)
    used = source is not None and source != NAME
    reason = None
    if used:
        status = stats["incremental_status"]
        reason = "retained_simpler_candidate" if status == "ok" else f"incremental_status {status}"
    return {
        "schema": "r021.diagnostics/1",
        "contract": "R021-C/1",
        "algorithm": algorithm,
        "domain": dom,
        "candidate_domain_hash": hashlib.sha256(
            json.dumps(dom, sort_keys=True).encode()
        ).hexdigest(),
        "certificate": None,
        "certificate_unavailable_reason": "not_produced",
        "max_candidates_unit": "label_relaxations_per_chunk",
        "work": {
            "quotes_executed": stats["quotes_executed"],
            "quotes_memoized": stats["quotes_memoized"],
            "internal_evaluations": stats["evaluations"]["total"],
            "label_relaxations": work.label_relaxations,
            "labels_discarded_dominance": work.labels_discarded_dominance,
            "labels_retained_unknown": work.labels_retained_unknown,
            "state_comparisons": work.state_comparisons,
            "peak_frontier_labels": work.peak_frontier_labels,
            "admission_checks": work.admission_checks,
        },
        "fallback": {"used": used, "source": source, "reason": reason},
    }


# ===================================================================================
# §9.2 diagnostic pass: NOT part of `solve`; never timed, metered or budgeted.
# ===================================================================================

UNATTRIBUTED = frozenset({"miss", "above", "extra"})


def _choice_dict(choice: Choice | None) -> dict[str, Any]:
    if choice is None:
        return {"marginal": None, "path": None, "hops": None}
    return {"marginal": str(choice[0]), "path": path_label(choice[1]), "hops": len(choice[1])}


def _classify(got: Choice | None, want: Choice | None) -> str:
    if want is None:
        return "agree" if got is None else "extra"
    if got is None or got[0] < want[0]:
        return "miss"
    if got[0] > want[0]:
        return "above"
    return "agree" if got[1] == want[1] else "tie"


def diagnose_history(
    case: Case, bundle: SnapshotBundle, prepared: PreparedMetisHistory
) -> dict[str, Any]:
    """history-labels.md §9.2: a **separate correctness pass**, never called by `solve`.

    Replays `metis_history`'s own chunk trajectory (the prepared options, unbudgeted) on its
    own `QuoteCache` and, at every non-empty chunk, re-scores the exhaustive same-depth
    enumeration (`metis_inspired`'s ablation loop over `enumerate_paths(..., label_hops)`)
    on the **identical** committed flows, token edges and carried amount with a second
    cache and its own counters. Classes: `agree`; `tie` (equal marginal, other path);
    `miss_capped` (below the maximum on a state-capped chunk: the visible approximation);
    `miss`, `above`, `extra` on an uncapped chunk are `unattributed`, and so is any
    non-`agree` uncapped chunk under `dominance: off`. `gate` is `pass` only when chunks
    were audited and none is unattributed (`no_data` otherwise, never a vacuous pass)."""
    index, hops, opts = prepared.index, prepared.label_hops, prepared.options
    source, target = case.token_in, case.token_out
    own_cache, enum_cache = QuoteCache(bundle), QuoteCache(bundle)
    alloc = metis_inspired._Allocator(bundle, case, index, own_cache)
    enum = metis_inspired._Allocator(bundle, case, index, enum_cache)
    unbudgeted = Budget()
    work = Work()
    region: dict[tuple[str, int], bool] = {}
    dist = hops_to_target(index, source, target)
    paths = list(enumerate_paths(index, source, target, hops)) if source != target else []
    amounts = chunk_amounts(case.amount_in, prepared.graph.chunks)
    records: list[dict[str, Any]] = []
    status = "ok"
    reachable = source != target and dist.get(source, hops + 1) <= hops
    if not reachable:
        status = "no_paths"
    else:
        last = max(k for k, a in enumerate(amounts) if a > 0)
        carry = 0
        for k, chunk in enumerate(amounts):
            if chunk == 0:
                continue
            amount = carry + chunk
            enum.flows, enum.token_edges = dict(alloc.flows), set(alloc.token_edges)
            before = (work.label_relaxations, enum.scored, own_cache.misses, enum_cache.misses)
            drops = work.drops()
            got = choose_history(
                alloc,
                amount,
                hops,
                dist,
                unbudgeted,
                opts,
                final=k == last,
                safe=prepared.safe_edges,
                region=region,
                work=work,
            )
            capped = work.drops() > drops
            work.chunks_state_capped += capped
            want = enum.choose_enumeration(amount, paths, unbudgeted)
            cls = _classify(got, want)
            if capped and cls == "miss":
                cls = "miss_capped"
            unattributed = not capped and (
                cls in UNATTRIBUTED or (opts["dominance"] == "off" and cls != "agree")
            )
            records.append(
                {
                    "chunk": k + 1,
                    "amount": str(amount),
                    "final": k == last,
                    "capped": capped,
                    "history": _choice_dict(got),
                    "enumeration": _choice_dict(want),
                    "class": cls,
                    "unattributed": unattributed,
                    "label_relaxations": work.label_relaxations - before[0],
                    "enumeration_paths_scored": enum.scored - before[1],
                    "history_quotes_executed": own_cache.misses - before[2],
                    "enumeration_quotes_executed": enum_cache.misses - before[3],
                }
            )
            if k != last and (got is None or got[0] == 0):
                carry = amount
                continue
            if got is None:
                status = f"chunk_{k + 1}_no_admissible_path"
                break
            carry = 0
            alloc.commit(got[1], got[2])
    classes: dict[str, int] = {}
    for r in records:
        classes[r["class"]] = classes.get(r["class"], 0) + 1
    unattributed = sum(r["unattributed"] for r in records)
    return {
        "pass": "history-labels.md §9.2 diagnostic (separate correctness pass; not a measured "
        "solve)",
        "case_id": case.case_id,
        "algorithm": NAME,
        "options": dict(opts),
        "label_hops": hops,
        "chunks": prepared.graph.chunks,
        "trajectory_status": status,
        "classes": dict(sorted(classes.items())),
        "chunks_audited": len(records),
        "chunks_state_capped": work.chunks_state_capped,
        "unattributed_chunks": unattributed,
        "gate": "fail" if unattributed else ("pass" if records else "no_data"),
        "counters": {
            **{f.name: getattr(work, f.name) for f in dataclasses.fields(Work)},
            "history_quotes_executed": own_cache.misses,
            "enumeration_paths": len(paths),
            "enumeration_paths_scored": enum.scored,
            "enumeration_quotes_executed": enum_cache.misses,
        },
        "chunk_records": records,
    }


FACTORY = AlgorithmFactory(
    name=NAME,
    solve=solve,
    prepare=prepare,
    capabilities=CAPABILITIES,
    search_params=SEARCH_PARAMS,
    graph_params=GRAPH_PARAMS,
    provenance=PROVENANCE,
    options_validator=validate_options,
    options_preset=MappingProxyType(PRESET),
)

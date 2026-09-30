"""R021-P03 (WHI-1549): the history/admission-aware label contract for the future
`metis_history` (docs/references/research-021/history-labels.md).

This module is research evidence, not a strategy. It holds three things, kept apart:

1. **The executable selector specification** (`choose_history`): the per-chunk label
   search of `history-labels.md` §4, run on the *actual* `metis_inspired._Allocator`
   step/commit semantics (committed aggregate `PoolFlow`s on original pool states, the
   guarded `QuoteCache`, `creates_cycle` admission). It is the reference WHI-1550 must
   reproduce; it is never registered, profiled or timed.
2. **An independent exhaustive oracle** (`oracle_chunk`): its own adjacency, its own
   depth-first simple-path enumeration, its own token-cycle test and a hand integer CPMM
   formula (other pool families use the pure `pools.quote.quote_exact_in`), on the
   identical committed state of each chunk. It shares no search code with the selector
   or with `metis_inspired`.
3. **Actual-solver reproductions**: `metis_inspired.solve` / `diagnose_case` and the
   common evaluator on R1 (external report §2.1), X4, X4b, X2 and the new counterexamples
   of `docs/references/research-021/fixtures/history-labels.json`.

Whole plans are always replayed by the unchanged `routing.evaluator.evaluate`; a
per-chunk agreement is never read as a whole-plan claim.

`uv run python tests/routing/test_history_labels_contract.py probe <bundle> <out.json>
[case ids...]` is the bounded tuning probe of `history-labels.md` §9 (a separate
diagnostic pass; nothing here is a measured solve).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from benchmark.objective import gross_only
from pools.constant_product import SOURCES
from pools.quote import quote_exact_in
from pools.result import QuoteStatus
from routing.algorithms import metis_inspired
from routing.algorithms.base import AlgorithmConfig, Budget, SolveContext, SolveResult
from routing.algorithms.incremental_graph import (
    PoolFlow,
    chunk_amounts,
    creates_cycle,
    merged_plan,
)
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.search import (
    Edge,
    GraphIndex,
    QuoteCache,
    build_graph_index,
    enumerate_paths,
    path_plan,
)
from snapshot.bundle import load_bundle
from snapshot.models import BlockRef, Case, ConstantProductPoolState, PoolState, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
R021 = REPO / "docs" / "references" / "research-021"
RECON = json.loads((R021 / "fixtures" / "reconstructions.json").read_text(encoding="utf-8"))
FIX = json.loads((R021 / "fixtures" / "history-labels.json").read_text(encoding="utf-8"))
MEMO = (R021 / "history-labels.md").read_text(encoding="utf-8")
ROUTING_FIXTURES = REPO / "tests" / "fixtures" / "routing"
BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)

Path_ = tuple[Edge, ...]
Choice = tuple[int, Path_, list[PoolFlow]]
Pools = Mapping[str, Sequence[Any]]


# ================================================================ bundles


def cp_bundle(pools: Pools, fee_bps: int = 30, source_key: str | None = None) -> SnapshotBundle:
    """A synthetic bundle from `{pool_id: [token0, token1, reserve0, reserve1, (fee)]}`
    in the given (insertion = adjacency) order."""
    states: dict[str, PoolState] = {}
    for pid, spec in pools.items():
        t0, t1, r0, r1 = spec[:4]
        fee = int(spec[4]) if len(spec) > 4 else fee_bps
        states[pid] = ConstantProductPoolState(
            pool_id=pid,
            token0=t0,
            token1=t1,
            reserve0=int(r0),
            reserve1=int(r1),
            fee_bps=fee,
            source_key=source_key,
        )
    return SnapshotBundle(
        bundle_id="whi1549",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools=states,
        cases=(),
        bundle_hash="whi1549",
        source_path="<test>",
    )


def with_pools(bundle: SnapshotBundle, *extra: PoolState) -> SnapshotBundle:
    return dataclasses.replace(bundle, pools={**bundle.pools, **{p.pool_id: p for p in extra}})


# ================================================================ 1. selector specification


@dataclass(frozen=True)
class SelectorOptions:
    """The `metis_history` option schema of history-labels.md §6 (validated there).

    `dominance`: `"history"` (the mechanism) or `"off"` (the disabled-mechanism control:
    every admissible label is kept). `max_labels_per_signature` / `max_frontier_labels`:
    the two resource caps (None = uncapped, allowed only in specification checks and
    stress profiles, never in the bounded preset)."""

    dominance: str = "history"
    max_labels_per_signature: int | None = None
    max_frontier_labels: int | None = None


@dataclass
class ChunkWork:
    """Per-chunk work of the selector in the registered R021-C/1 §5.2 units, plus the
    cap counters (search_stats keys, not §5.2 units)."""

    label_relaxations: int = 0
    admission_checks: int = 0
    state_comparisons: int = 0
    labels_discarded_dominance: int = 0
    labels_retained_unknown: int = 0
    peak_frontier_labels: int = 0
    labels_dropped_signature_cap: int = 0
    labels_dropped_frontier_cap: int = 0
    certified_strict_insertions: int = 0
    truncated_by: str | None = None
    peak_signature_labels: int = 0

    @property
    def capped(self) -> bool:
        return bool(self.labels_dropped_signature_cap or self.labels_dropped_frontier_cap)


@dataclass(frozen=True)
class HLabel:
    amount: int
    path: Path_
    updates: tuple[PoolFlow, ...]
    tokens: frozenset[str]

    @property
    def token(self) -> str:
        return self.path[-1].token_out


def upward_safe_edges(bundle: SnapshotBundle) -> frozenset[tuple[str, str]]:
    """Directed edges `(pool_id, token_in)` whose chunk marginal is *certified upward
    safe* (history-labels.md §3.4, lemma L3): for every committed aggregate `x` and inputs
    `1 <= m_b <= m_a`, a successful quote at `x + m_b` implies a successful quote at
    `x + m_a` with a marginal at least as large.

    Only constant-product pools qualify: the floored `getAmountOut` is nondecreasing, its
    only input-dependent failures are the dust floor (downward) and, for a sourced pair,
    the `uint112` overflow revert (upward). The overflow is excluded statically: every
    unit entering the pool from `u` is some pool's output of `u`, and a CPMM's aggregate
    output of `u` is below its `u` reserve, so the pair is safe when its own `u` reserve
    plus every other pool's `u` reserve stays below the limit. A non-CPMM pool producing
    `u` has no such bound (unknown -> not certified). Input-independent failures
    (unsupported source/fee, an empty side) fail for both amounts and are safe."""
    held: dict[str, dict[str, int | None]] = {}  # token -> pool -> its reserve of token
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
                safe.add((pool.pool_id, u))  # no overflow rule, or input-independent failure
                continue
            others = [r for pid, r in held[u].items() if pid != pool.pool_id]
            if any(r is None for r in others):
                continue  # a non-CPMM producer of u: no static inflow bound
            if r_in + sum(r for r in others if r is not None) < 1 << source.reserve_bits:
                safe.add((pool.pool_id, u))
    return frozenset(safe)


def region_certified(
    index: GraphIndex,
    safe: frozenset[tuple[str, str]],
    v: str,
    remaining: int,
    source: str,
    target: str,
    memo: dict[tuple[str, int], bool],
) -> bool:
    """Every directed edge any continuation from `v` with at most `remaining` pools can
    use (walks not entering the source and not leaving the target; a superset of the
    token-simple continuations) is certified upward safe."""
    key = (v, remaining)
    if key in memo:
        return memo[key]
    ok = True
    frontier = {v}
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
    return ok


def choose_history(
    alloc: metis_inspired._Allocator,
    amount: int,
    hops: int,
    dist: Mapping[str, int],
    budget: Budget,
    options: SelectorOptions,
    *,
    final: bool,
    safe: frozenset[tuple[str, str]],
    work: ChunkWork,
    mutation: str | None = None,
) -> Choice | None:
    """One chunk of the `metis_history` selector (history-labels.md §4, normative).

    Identical to `metis_inspired._Allocator.choose_labels` in expansion order, filters
    (source, distance, token-simple, `creates_cycle` before the quote), the budgeted
    relaxation unit and the strict first-found target choice. It differs only in what a
    layer retains: labels are grouped by the exact signature `(token, visited set)` of
    their layer (lemma L2: with the committed token DAG fixed, the admissible
    continuations of a prefix depend only on these); inside a group a new label is
    discarded when an existing one has the **same amount** (identical futures, lemma L1),
    or -- only in a non-final chunk whose continuation region is certified upward safe
    (lemma L3) -- a **strictly larger** amount (a strictly smaller existing label is then
    removed). Every other same-signature label is retained (`labels_retained_unknown`).
    Caps drop labels visibly (never silently).

    `mutation` exists only to show that each rule is needed (tests): `"strict_everywhere"`
    is the unsafe amount-only rule (strict dominance on every edge and in the final chunk);
    `"token_only"` drops the visited set from the signature (metis_inspired's grouping);
    both together (`"token_only+strict_everywhere"`) are metis_inspired's own rule."""
    source, target = alloc.case.token_in, alloc.case.token_out
    region: dict[tuple[str, int], bool] = {}
    layer: list[HLabel] = [HLabel(amount, (), (), frozenset((source,)))]
    best: Choice | None = None
    relaxed = 0
    for k in range(1, hops + 1):
        groups: dict[Any, dict[int, tuple[int, HLabel]]] = {}  # key -> amount -> label
        order = 0
        size = 0
        for lab in layer:  # generation order == enumeration (lexicographic) order
            t = lab.token if lab.path else source
            for e in alloc.index.edges_from(t):
                v = e.token_out
                if v == source:
                    continue
                if v != target and dist.get(v, hops + 1) > hops - k:
                    continue
                if v in lab.tokens:
                    continue
                p = (*lab.path, e)
                work.admission_checks += 1
                if creates_cycle(alloc.token_edges, p):
                    continue
                if budget.max_candidates is not None and relaxed >= budget.max_candidates:
                    work.truncated_by = "max_candidates"
                    return best
                relaxed += 1
                work.label_relaxations += 1
                result = alloc.step(e, lab.amount, k)
                if isinstance(result, metis_inspired._Failure):
                    alloc._count(result, p)
                    continue
                m, update = result
                if v == target:
                    if best is None or m > best[0]:
                        best = (m, p, [*lab.updates, update])
                    continue
                new = HLabel(m, p, (*lab.updates, update), lab.tokens | {v})
                order += 1
                if options.dominance == "off":
                    key: Any = order
                    strict = False
                else:
                    key = v if "token_only" in (mutation or "") else (v, new.tokens)
                    strict = "strict_everywhere" in (mutation or "") or (
                        not final
                        and region_certified(alloc.index, safe, v, hops - k, source, target, region)
                    )
                group = groups.setdefault(key, {})
                if group:
                    work.state_comparisons += 1  # one dominance decision per occupied group
                    if new.amount in group:  # equal amount, equal signature: same futures
                        work.labels_discarded_dominance += 1
                        continue
                    if strict:  # a strict group holds at most one label
                        (old,) = group.values()
                        if old[1].amount > new.amount:
                            work.labels_discarded_dominance += 1
                            continue
                        group.clear()
                        size -= 1
                        work.labels_discarded_dominance += 1
                if strict:
                    work.certified_strict_insertions += 1
                cap = options.max_labels_per_signature
                if cap is not None and len(group) >= cap:
                    worst = min(group.values(), key=lambda g: (g[1].amount, -g[0]))
                    work.labels_dropped_signature_cap += 1
                    if worst[1].amount > new.amount:
                        continue  # the new label ranks last: dropped
                    del group[worst[1].amount]
                    size -= 1
                if options.max_frontier_labels is not None and size >= options.max_frontier_labels:
                    work.labels_dropped_frontier_cap += 1
                    continue
                work.labels_retained_unknown += bool(group)  # beside an unprovable label
                group[new.amount] = (order, new)
                size += 1
                work.peak_signature_labels = max(work.peak_signature_labels, len(group))
        layer = [lab for _, lab in sorted(g for group in groups.values() for g in group.values())]
        work.peak_frontier_labels = max(work.peak_frontier_labels, len(layer))
    return best


# ---------------------------------------------------------------- trajectories


@dataclass
class ChunkRecord:
    index: int
    amount: int
    final: bool
    flows: dict[str, PoolFlow]
    token_edges: set[tuple[str, str]]
    choice: Choice | None
    work: ChunkWork | None


@dataclass
class Trajectory:
    status: str
    records: list[ChunkRecord]
    chunk_paths: list[Path_]
    flows: dict[str, PoolFlow]
    evaluation: Evaluation | None
    quotes_executed: int

    @property
    def gross(self) -> int | None:
        if self.evaluation is None or self.evaluation.status is not EvalStatus.OK:
            return None
        return self.evaluation.gross_output

    def work(self) -> ChunkWork:
        total = ChunkWork()
        for r in self.records:
            if r.work is None:
                continue
            for f in dataclasses.fields(ChunkWork):
                if f.name == "truncated_by":
                    total.truncated_by = total.truncated_by or r.work.truncated_by
                elif f.name.startswith("peak"):
                    setattr(total, f.name, max(getattr(total, f.name), getattr(r.work, f.name)))
                else:
                    setattr(total, f.name, getattr(total, f.name) + getattr(r.work, f.name))
        return total


def trajectory(
    bundle: SnapshotBundle,
    case: Case,
    chooser: str | SelectorOptions,
    *,
    chunks: int,
    hops: int,
    budget: Budget | None = None,
    mutation: str | None = None,
) -> Trajectory:
    """The incremental chunk loop of `metis_inspired.solve` (carry, final chunk, commit)
    with a chunk chooser: `"labels"` (the actual `metis_inspired` label search, L_H),
    `"enumeration"` (the actual ablation loop, E_H) or `SelectorOptions` (the
    specification, S_H). The merged plan is replayed by the unchanged evaluator."""
    budget = budget or Budget()
    index = build_graph_index(bundle)
    cache_ = QuoteCache(bundle)
    alloc = metis_inspired._Allocator(bundle, case, index, cache_)
    dist = metis_inspired.hops_to_target(index, case.token_in, case.token_out)
    safe = upward_safe_edges(bundle)
    paths = list(enumerate_paths(index, case.token_in, case.token_out, hops))
    amounts = chunk_amounts(case.amount_in, chunks)
    last = max(k for k, a in enumerate(amounts) if a > 0)
    records: list[ChunkRecord] = []
    chunk_paths: list[Path_] = []
    carry = 0
    status = "ok"
    for k, chunk in enumerate(amounts):
        if chunk == 0:
            continue
        amount = carry + chunk
        work: ChunkWork | None = None
        before = (dict(alloc.flows), set(alloc.token_edges))
        if chooser == "labels":
            choice = alloc.choose_labels(amount, hops, dist, budget)
        elif chooser == "enumeration":
            choice = alloc.choose_enumeration(amount, paths, budget)
        else:
            assert isinstance(chooser, SelectorOptions)
            work = ChunkWork()
            choice = choose_history(
                alloc,
                amount,
                hops,
                dist,
                budget,
                chooser,
                final=k == last,
                safe=safe,
                work=work,
                mutation=mutation,
            )
        records.append(ChunkRecord(k + 1, amount, k == last, *before, choice, work))
        if k != last and (choice is None or choice[0] == 0):
            carry = amount
            continue
        if choice is None:
            status = f"chunk_{k + 1}_no_admissible_path"
            break
        carry = 0
        alloc.commit(choice[1], choice[2])
        chunk_paths.append(choice[1])
    evaluation = None
    if status == "ok":
        plan = merged_plan(case, alloc.flows.values())
        evaluation = evaluate(bundle, case, plan, gross_only())
        if evaluation.status is not EvalStatus.OK:
            status = "invalid_plan"
    return Trajectory(status, records, chunk_paths, dict(alloc.flows), evaluation, cache_.misses)


# ================================================================ 2. independent oracle


def hand_cpmm(pool: ConstantProductPoolState, token_in: str, z: int) -> int | None:
    """Hand exact-input CPMM output of an aggregate input `z` on the original reserves,
    with the repository's failure rules restated independently: dust (output 0), empty
    side, an unsupported source/fee, and a sourced pair's reserve overflow."""
    r_in, r_out = (
        (pool.reserve0, pool.reserve1)
        if token_in == pool.token0
        else (pool.reserve1, pool.reserve0)
    )
    if pool.source_key is not None:
        src = SOURCES.get(pool.source_key)
        if src is None or src.fee_bps != pool.fee_bps:
            return None
        if r_in + z >= 1 << src.reserve_bits:
            return None
    if r_in <= 0 or r_out <= 0:
        return None
    keep = 10_000 - pool.fee_bps
    out = z * keep * r_out // (r_in * 10_000 + z * keep)
    return out if out > 0 else None


@dataclass(frozen=True)
class OracleChunk:
    values: tuple[tuple[tuple[str, ...], int], ...]  # (pool ids, marginal), enumeration order

    @property
    def best(self) -> tuple[tuple[str, ...], int] | None:
        top: tuple[tuple[str, ...], int] | None = None
        for pools, value in self.values:
            if top is None or value > top[1]:
                top = (pools, value)
        return top


def _acyclic(edges: set[tuple[str, str]]) -> bool:
    nodes = {t for e in edges for t in e}
    indeg = dict.fromkeys(nodes, 0)
    for _, v in edges:
        indeg[v] += 1
    ready = [t for t, d in indeg.items() if d == 0]
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


def oracle_chunk(
    bundle: SnapshotBundle,
    case: Case,
    flows: Mapping[str, PoolFlow],
    token_edges: set[tuple[str, str]],
    amount: int,
    hops: int,
    memo: dict[tuple[str, str, int], int | None] | None = None,
) -> OracleChunk:
    """Every admissible simple path's chunk marginal on the given committed state, in the
    documented enumeration order (hop-major, then depth-first in bundle adjacency order),
    computed without any repository search code."""
    memo = {} if memo is None else memo
    adj: dict[str, list[tuple[str, str, str]]] = {}
    for pool in bundle.pools.values():
        adj.setdefault(pool.token0, []).append((pool.pool_id, pool.token0, pool.token1))
        adj.setdefault(pool.token1, []).append((pool.pool_id, pool.token1, pool.token0))
    source, target = case.token_in, case.token_out

    def out_of(pid: str, token: str, z: int) -> int | None:
        key = (pid, token, z)
        if key not in memo:
            pool = bundle.pools[pid]
            if isinstance(pool, ConstantProductPoolState):
                memo[key] = hand_cpmm(pool, token, z)
            else:
                r = quote_exact_in(pool, token, z)
                ok = r.status is QuoteStatus.OK and r.amount_in_consumed == z
                memo[key] = r.amount_out if ok else None
        return memo[key]

    def paths(length: int) -> Iterator[list[tuple[str, str, str]]]:
        def go(token: str, path: list[tuple[str, str, str]], seen: set[str]) -> Iterator[Any]:
            for edge in adj.get(token, ()):
                nxt = edge[2]
                if nxt == target:
                    if len(path) + 1 == length:
                        yield [*path, edge]
                    continue
                if nxt in seen or len(path) + 1 >= length:
                    continue
                yield from go(nxt, [*path, edge], seen | {nxt})

        yield from go(source, [], {source})

    values: list[tuple[tuple[str, ...], int]] = []
    if source == target:
        return OracleChunk(())
    for length in range(1, hops + 1):
        for path in paths(length):
            if not _acyclic(token_edges | {(u, v) for _, u, v in path}):
                continue
            m: int | None = amount
            for pid, u, _ in path:
                assert m is not None
                prev = flows.get(pid)
                x, base = (prev.amount_in, prev.amount_out) if prev else (0, 0)
                if m == 0:
                    continue
                out = out_of(pid, u, x + m)
                if out is None or out < base:
                    m = None
                    break
                m = out - base
            if m is not None:
                values.append((tuple(pid for pid, _, _ in path), m))
    return OracleChunk(tuple(values))


def classify(choice: Choice | None, oracle: OracleChunk) -> str:
    best = oracle.best
    if choice is None:
        return "agree" if best is None else "miss"
    pools = tuple(e.pool_id for e in choice[1])
    if best is None:
        return "extra"
    if choice[0] > best[1]:
        return "above"
    if choice[0] < best[1]:
        return "miss"
    return "agree" if pools == best[0] else "tie"


def audit(bundle: SnapshotBundle, case: Case, run: Trajectory, hops: int) -> dict[str, int]:
    """Classify every chunk of `run` against the oracle on that chunk's committed state."""
    counts: dict[str, int] = {}
    memo: dict[tuple[str, str, int], int | None] = {}
    for r in run.records:
        oracle = oracle_chunk(bundle, case, r.flows, r.token_edges, r.amount, hops, memo)
        cls = classify(r.choice, oracle)
        if cls != "agree" and r.work is not None and (r.work.capped or r.work.truncated_by):
            cls = f"{cls}_capped"
        counts[cls] = counts.get(cls, 0) + 1
    return counts


def spec_sha256() -> str:
    """SHA-256 of the specification and oracle source (binds recorded probe evidence to
    this code; edits elsewhere in the module do not change it)."""
    import inspect

    parts: list[Any] = [
        SelectorOptions, ChunkWork, HLabel, upward_safe_edges,
        region_certified, choose_history, ChunkRecord, Trajectory, trajectory, hand_cpmm,
        OracleChunk, _acyclic, oracle_chunk, classify, audit,
    ]  # fmt: skip
    text = "\n".join(inspect.getsource(obj) for obj in parts)
    return hashlib.sha256(text.encode()).hexdigest()


# ================================================================ helpers for tests


def _metis(bundle: SnapshotBundle, case: Case, **params: Any) -> SolveResult:
    config = {"max_hops": 2, "max_splits": 4, "percent_step": 5, "chunks": 1}
    config.update({"label_hops": 4, "label_pruning": True, **params})
    prepared = metis_inspired.prepare(bundle, AlgorithmConfig(metis_inspired.NAME, config))
    return metis_inspired.solve(case, SolveContext(bundle, gross_only(), prepared), Budget())


def _diagnose(bundle: SnapshotBundle, case: Case, **params: Any) -> dict[str, Any]:
    config = {"max_hops": 2, "max_splits": 4, "percent_step": 5, "chunks": 1}
    config.update({"label_hops": 4, "label_pruning": True, **params})
    prepared = metis_inspired.prepare(bundle, AlgorithmConfig(metis_inspired.NAME, config))
    return metis_inspired.diagnose_case(case, bundle, prepared)


def _pools(run: Trajectory) -> list[list[str]]:
    return [[e.pool_id for e in p] for p in run.chunk_paths]


HISTORY = SelectorOptions()
OFF = SelectorOptions(dominance="off")


@cache
def _fixture(name: str) -> tuple[SnapshotBundle, Case]:
    spec = FIX["fixtures"][name]
    bundle = cp_bundle(spec["pools"], int(spec.get("fee_bps", 30)), spec.get("source_key"))
    c = spec["case"]
    return bundle, Case(name, c["token_in"], c["token_out"], int(c["amount_in"]))


def _recon(name: str) -> tuple[SnapshotBundle, Case]:
    r = RECON[name]
    bundle = cp_bundle(r["pools"], r["fee_bps"])
    if name == "R1_prefix_merge_vs_cycle":
        return bundle, Case("r1", r["token_in"], r["token_out"], r["amount_in"])
    source, target = ("S", "D")
    return bundle, Case(name, source, target, r["amount_in"])


# ================================================================ 3a. actual-solver counterexamples


def test_r1_actual_label_search_discards_the_legal_prefix_and_the_walk_is_invalid() -> None:
    """External report §2.1 on the actual solver: L4's single label at C (via B, 11926)
    drops A-D-C (9840); A-D-C-B-T (19560) repeats no token. E4, the history signature and
    the evaluator agree on 19560; the 23708 walk is an evaluator `invalid_plan`."""
    r = RECON["R1_prefix_merge_vs_cycle"]
    bundle, case = _recon("R1_prefix_merge_vs_cycle")
    l4 = _metis(bundle, case)
    e4 = _metis(bundle, case, label_pruning=False)
    assert l4.search_stats["incremental_evaluated_gross"] == str(r["single_label_result"]["gross"])
    assert e4.search_stats["incremental_evaluated_gross"] == str(r["best_simple_path"]["gross"])
    assert l4.search_stats["label_skipped_revisit"] > 0
    diag = _diagnose(bundle, case)
    assert diag["classes"] == {"token_revisit": 1} and diag["unexplained_chunks"] == 0
    s4 = trajectory(bundle, case, HISTORY, chunks=1, hops=4)
    assert s4.gross == r["best_simple_path"]["gross"]
    assert _pools(s4) == [r["best_simple_path"]["path"]]
    assert audit(bundle, case, s4, 4) == {"agree": 1}
    walk = r["repeated_token_walk"]
    steps = [bundle.pools[p] for p in walk["path"]]
    tokens = ["A"]
    for pool in steps:
        tokens.append(pool.other_token(tokens[-1]))
    assert tokens == walk["tokens"] and len(set(tokens)) < len(tokens)
    edges = tuple(
        Edge(p.pool_id, a, b) for p, a, b in zip(steps, tokens[:-1], tokens[1:], strict=True)
    )
    bad = evaluate(bundle, case, path_plan(case, edges), gross_only())
    assert bad.status is EvalStatus.INVALID_PLAN and "cycle" in (bad.error or "")


def test_x4_token_revisit_is_a_legal_prefix_loss_the_signature_recovers() -> None:
    r = RECON["R7_X4"]
    bundle, case = _recon("R7_X4")
    diag = _diagnose(bundle, case)
    assert diag["classes"] == {"token_revisit": 1}
    record = diag["chunk_records"][0]
    assert record["enumeration"]["path"] == "S -[sa]-> A -[ax]-> X -[xy]-> Y -[yd]-> D"
    assert record["label"]["path"] == "S -[sy]-> Y -[yd]-> D"
    s4 = trajectory(bundle, case, HISTORY, chunks=1, hops=4)
    assert s4.gross == r["best_four_hop"]["gross"] and _pools(s4) == [["sa", "ax", "xy", "yd"]]
    assert audit(bundle, case, s4, 4) == {"agree": 1}


def test_x4b_prefix_admission_is_a_legal_prefix_loss_the_visited_set_recovers() -> None:
    """Chunk 2 of X4b: the dominant S-A-V label and the dominated S-B-V label reach V with
    different visited sets, so they are different signatures. The history selector keeps
    S-B-V and reaches the enumeration's S-B-V-X-D; the evaluator accepts that union (the
    S-A-V-X-D union is the evaluator-rejected cycle of R8)."""
    bundle, case = _recon("R8_X4b")
    diag = _diagnose(bundle, case, chunks=2)
    assert diag["classes"] == {"agree": 1, "prefix_admission": 1}
    s4 = trajectory(bundle, case, HISTORY, chunks=2, hops=4)
    e4 = trajectory(bundle, case, "enumeration", chunks=2, hops=4)
    l4 = trajectory(bundle, case, "labels", chunks=2, hops=4)
    assert _pools(s4) == _pools(e4) == [["sx", "xa", "ad"], ["sb", "bv", "vx", "xd"]]
    assert _pools(l4) == [["sx", "xa", "ad"], ["sx", "xa", "ad"]]
    assert s4.gross == e4.gross and l4.gross is not None and s4.gross is not None
    assert s4.gross > l4.gross
    assert audit(bundle, case, s4, 4) == {"agree": 2}
    assert audit(bundle, case, l4, 4) == {"agree": 1, "miss": 1}
    # Mutation: one label per token with amount-only dominance is metis_inspired's rule and
    # reproduces L4's trajectory and loss.
    rule = "token_only+strict_everywhere"
    token_only = trajectory(bundle, case, HISTORY, chunks=2, hops=4, mutation=rule)
    assert _pools(token_only) == _pools(l4) and token_only.gross == l4.gross


def _walks(
    bundle: SnapshotBundle, start: str, stop: str, avoid: frozenset[str], max_len: int
) -> Iterator[list[tuple[str, str, str]]]:
    """Every token-simple walk from `start` of 1..`max_len` pools that never enters
    `avoid` and never passes through `stop` (independent of the repository search)."""
    adj: dict[str, list[tuple[str, str, str]]] = {}
    for pool in bundle.pools.values():
        adj.setdefault(pool.token0, []).append((pool.pool_id, pool.token0, pool.token1))
        adj.setdefault(pool.token1, []).append((pool.pool_id, pool.token1, pool.token0))

    def go(token: str, path: list[tuple[str, str, str]], seen: set[str]) -> Iterator[Any]:
        if path:
            yield path
        if len(path) == max_len or token == stop:
            return
        for edge in adj.get(token, ()):
            if edge[2] not in seen and edge[2] not in avoid:
                yield from go(edge[2], [*path, edge], seen | {edge[2]})

    yield from go(start, [], {start})


def test_lemma_l2_admission_depends_only_on_the_visited_set() -> None:
    """Lemma L2, checked exhaustively with an independent acyclicity test: on the committed
    token DAG of every chunk of random multi-chunk trajectories, any two admissible
    prefixes with the same end token and visited set -- in any token order, over any
    pools -- admit exactly the same continuations to the target. This is why X4b-style
    prefix-dependent admission needs no signature beyond the visited set."""
    checked = reordered = 0
    for seed in range(200):
        bundle, cases = _random_bundle(100 + seed, dense=True)
        run = trajectory(bundle, cases[-1], "enumeration", chunks=6, hops=5)
        for record in run.records:
            edges = record.token_edges
            groups: dict[tuple[str, frozenset[str]], list[tuple[str, ...]]] = {}
            for p in _walks(bundle, "A", "B", frozenset("A"), 3):
                chain = {(u, v) for _, u, v in p}
                if p[-1][2] == "B" or not _acyclic(edges | chain):
                    continue  # a target relaxation or an inadmissible prefix: no label
                order = ("A", *(v for _, _, v in p))
                groups.setdefault((p[-1][2], frozenset(order)), []).append(order)
            for (v, tokens), orders in groups.items():
                if len(orders) < 2:
                    continue
                distinct = set(orders)
                reordered += bool(edges) and len(distinct) > 1
                for q in _walks(bundle, v, "B", tokens, 5 - (len(tokens) - 1)):
                    if q[-1][2] != "B":
                        continue
                    cont = {(a, b) for _, a, b in q}
                    verdicts = {
                        _acyclic(edges | set(zip(o, o[1:], strict=False)) | cont) for o in distinct
                    }
                    assert len(verdicts) == 1, (seed, v, sorted(tokens), q)
                    checked += 1
    assert checked > 1000 and reordered > 20, (checked, reordered)


# ================================================================ 3b. failure domains


def test_x2_larger_amount_fails_on_real_cl_state_and_the_smaller_label_is_retained() -> None:
    """X2 (real Uniswap v3 USDT/WMNT state): the larger S->USDT label overshoots the
    collected tick range (`incomplete_snapshot`) where the smaller one fits. CL is not
    certified, so the selector keeps both same-signature labels (`labels_retained_unknown`)
    and matches the oracle; forcing strict dominance there reproduces L3's loss."""
    mixed = load_bundle(ROUTING_FIXTURES / "mantle_mixed")
    usdt, wmnt = (
        "0x201eba5cc46d216ce6dc03f6a759e8e766e956ae",
        "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8",
    )
    cl = mixed.pools["0x4cdfc22bf05209de87ee564746dc7e5174631d2b"]
    bundle = with_pools(
        cp_bundle({"big": ["S", usdt, 10**12, 10**14], "small": ["S", usdt, 10**9, 10**9]}),
        cl,
        ConstantProductPoolState("weak", "S", wmnt, 10**9, 10**15, 30, None),
    )
    case = Case("x2", "S", wmnt, 10**7)
    assert (cl.pool_id, usdt) not in upward_safe_edges(bundle)
    s3 = trajectory(bundle, case, HISTORY, chunks=1, hops=3)
    l3 = trajectory(bundle, case, "labels", chunks=1, hops=3)
    assert audit(bundle, case, s3, 3) == {"agree": 1}
    assert audit(bundle, case, l3, 3) == {"miss": 1}
    work = s3.work()
    assert work.labels_retained_unknown >= 1 and work.certified_strict_insertions == 0
    assert s3.gross is not None and l3.gross is not None and s3.gross > l3.gross
    forced = _forced_strict_run(bundle, case, chunks=1, hops=3)
    assert audit(bundle, case, forced, 3) == {"miss": 1}


def _forced_strict_run(bundle: SnapshotBundle, case: Case, *, chunks: int, hops: int) -> Trajectory:
    return trajectory(bundle, case, HISTORY, chunks=chunks, hops=hops, mutation="strict_everywhere")


def test_sourced_cpmm_overflow_makes_a_larger_amount_fail() -> None:
    """history-labels.json `overflow`: a Moe Classic v1 pair near the uint112 limit. The
    actual quote succeeds for the smaller label's X amount and reverts for the larger one,
    so even constant product is not upward closed; the static bound refuses to certify
    that edge, the selector retains both labels and matches the oracle. The unsafe
    amount-only rule and metis_inspired's L3 both fall back to the weak direct pool."""
    spec = FIX["fixtures"]["overflow"]
    bundle, case = _fixture("overflow")
    pool = bundle.pools["xd"]
    assert isinstance(pool, ConstantProductPoolState)
    small, large = spec["xd_inputs"]["small"], spec["xd_inputs"]["large"]
    assert quote_exact_in(pool, "X", small).status is QuoteStatus.OK
    assert quote_exact_in(pool, "X", large).status is QuoteStatus.REVERTED
    assert hand_cpmm(pool, "X", small) is not None and hand_cpmm(pool, "X", large) is None
    assert ("xd", "X") not in upward_safe_edges(bundle)
    assert ("xd", "X") in upward_safe_edges(cp_bundle(spec["pools"]))  # no overflow rule
    want = spec["expected"]
    s = trajectory(bundle, case, HISTORY, chunks=1, hops=3)
    e = trajectory(bundle, case, "enumeration", chunks=1, hops=3)
    l3 = trajectory(bundle, case, "labels", chunks=1, hops=3)
    forced = _forced_strict_run(bundle, case, chunks=1, hops=3)
    assert (s.gross, e.gross) == (want["history_gross"], want["enumeration_gross"])
    assert (l3.gross, forced.gross) == (want["label_gross"], want["strict_everywhere_gross"])
    assert audit(bundle, case, s, 3) == {"agree": 1}
    assert audit(bundle, case, forced, 3) == audit(bundle, case, l3, 3) == {"miss": 1}


def test_final_chunk_uses_equal_amount_dominance_only() -> None:
    """The final non-empty chunk never applies strict amount dominance (history-labels.md
    §4.3 rule R5): lemma L3 covers continuation values >= 1 only, and in the final chunk a
    zero-valued commit and an abandoned incremental plan differ. Earlier chunks of the same
    all-CPMM request do use it."""
    bundle, case = _recon("R1_prefix_merge_vs_cycle")
    s = trajectory(bundle, case, HISTORY, chunks=3, hops=4)
    works = [r.work for r in s.records]
    assert all(w is not None for w in works)
    assert [r.final for r in s.records] == [False, False, True]
    assert works[0] is not None and works[0].certified_strict_insertions > 0
    assert works[-1] is not None and works[-1].certified_strict_insertions == 0
    assert audit(bundle, case, s, 4) == {"agree": 3}


# ================================================================ 3c. ties, state and whole plans


def test_equal_chunk_value_different_state_changes_the_next_chunk() -> None:
    """history-labels.json `tie_state`: chunk 1 has two maximal paths of equal marginal
    through different first pools (floor plateau on the thin last pool). Certified strict
    dominance keeps the larger-amount prefix; enumeration keeps the first path. Same
    chunk-1 value, different committed state, different chunk 2 and final gross."""
    bundle, case = _fixture("tie_state")
    want = FIX["fixtures"]["tie_state"]["expected"]
    s = trajectory(bundle, case, HISTORY, chunks=2, hops=2)
    e = trajectory(bundle, case, "enumeration", chunks=2, hops=2)
    first_s, first_e = s.records[0].choice, e.records[0].choice
    assert first_s is not None and first_e is not None and first_s[0] == first_e[0]
    assert _pools(s) == want["history_chunk_paths"]
    assert _pools(e) == want["enumeration_chunk_paths"]
    assert audit(bundle, case, s, 2) == {"tie": 1, "agree": 1}
    assert audit(bundle, case, e, 2) == {"agree": 2}
    assert (s.gross, e.gross) == (want["history_gross"], want["enumeration_gross"])
    for run in (s, e):  # both whole plans replay exactly; only their states differ
        assert run.evaluation is not None and run.evaluation.status is EvalStatus.OK


def test_per_chunk_exact_choice_is_not_whole_plan_optimal_or_better() -> None:
    """history-labels.json `greedy_trap`: every S4 chunk agrees with the exhaustive
    per-chunk maximum, yet L4 (which misses chunk 1's maximum) ends with a higher
    evaluated gross. A per-chunk guarantee is not a whole-plan guarantee."""
    bundle, case = _fixture("greedy_trap")
    want = FIX["fixtures"]["greedy_trap"]["expected"]
    s = trajectory(bundle, case, HISTORY, chunks=2, hops=4)
    e = trajectory(bundle, case, "enumeration", chunks=2, hops=4)
    l4 = trajectory(bundle, case, "labels", chunks=2, hops=4)
    assert audit(bundle, case, s, 4) == audit(bundle, case, e, 4) == {"agree": 2}
    assert audit(bundle, case, l4, 4) == {"miss": 1, "agree": 1}
    assert _diagnose(bundle, case, chunks=2)["classes"] == {"agree": 1, "token_revisit": 1}
    assert _pools(s) == _pools(e) == want["history_chunk_paths"]
    assert _pools(l4) == want["label_chunk_paths"]
    assert (s.gross, e.gross, l4.gross) == (
        want["history_gross"],
        want["enumeration_gross"],
        want["label_gross"],
    )
    assert s.gross is not None and l4.gross is not None and l4.gross > s.gross


def test_amount_only_top_k_is_not_a_repair_but_the_signature_is() -> None:
    """External report §2.2 reconstructed (NEW; its construction was unspecified): R1
    plus k parallel A->B pools whose via-B labels at C all outrank via-D. Amount-only
    top-k per (layer, token) keeps k redundant via-B labels and returns 9938 for k = 1, 2,
    4, 8; the history signature returns 19560 with one label per signature."""
    base = RECON["R1_prefix_merge_vs_cycle"]
    for k in (1, 2, 4, 8):
        pools = dict(base["pools"])
        for i in range(1, k):
            pools[f"ab{i}"] = ["A", "B", 10**9 + i * 10**6, 10**9]
        bundle = cp_bundle(pools, base["fee_bps"])
        case = Case("topk", "A", "T", base["amount_in"])
        assert _top_k_gross(bundle, case, k) == base["single_label_result"]["gross"]
        s = trajectory(bundle, case, HISTORY, chunks=1, hops=4)
        assert s.gross == base["best_simple_path"]["gross"]
        assert audit(bundle, case, s, 4) == {"agree": 1}


def _top_k_gross(bundle: SnapshotBundle, case: Case, k: int) -> int | None:
    """Amount-only top-k beam (per layer and token, ties by generation order), one chunk,
    token-simple, no signature -- the rule the report shows is not a repair."""
    index = build_graph_index(bundle)
    layer: list[tuple[int, list[str], list[str]]] = [(case.amount_in, [case.token_in], [])]
    best: int | None = None
    for _ in range(4):
        cands: dict[str, list[tuple[int, list[str], list[str]]]] = {}
        for amount, tokens, pools in layer:
            for e in index.edges_from(tokens[-1]):
                if e.token_out in tokens:
                    continue
                pool = bundle.pools[e.pool_id]
                assert isinstance(pool, ConstantProductPoolState)
                out = hand_cpmm(pool, e.token_in, amount)
                if out is None:
                    continue
                if e.token_out == case.token_out:
                    best = out if best is None else max(best, out)
                    continue
                cands.setdefault(e.token_out, []).append(
                    (out, [*tokens, e.token_out], [*pools, e.pool_id])
                )
        layer = [lab for group in cands.values() for lab in sorted(group, key=lambda g: -g[0])[:k]]
    return best


# ================================================================ 3d. independent exhaustive checks


def _random_bundle(
    seed: int, *, sourced: bool = False, dense: bool = False
) -> tuple[SnapshotBundle, list[Case]]:
    rng = random.Random(seed)
    tokens = ["A", "B", "C", "D", "E", "F"][: rng.randint(4, 6)]
    pools: dict[str, list[Any]] = {}
    for n in range(rng.randint(12, 18) if dense else rng.randint(5, 11)):
        t0, t1 = rng.sample(tokens, 2)
        r0, r1 = (10 ** rng.randint(4, 10) * rng.randint(1, 9) for _ in range(2))
        pools[f"p{n}"] = [t0, t1, r0, r1, rng.choice([0, 1, 5, 30, 100])]
    bundle = cp_bundle(pools, source_key="moe_classic_v1" if sourced else None)
    if sourced:  # sourced pairs need fee 30
        bundle = cp_bundle({k: [*v[:4], 30] for k, v in pools.items()}, source_key="moe_classic_v1")
    amounts = [rng.randint(1, 40), rng.randint(10**3, 10**6), 10 ** rng.randint(5, 9) + 3]
    return bundle, [Case(f"c{a}", "A", "B", a) for a in amounts]


@pytest.mark.parametrize("hops", [3, 4, 5])
def test_every_chunk_matches_the_exhaustive_oracle_on_random_cpmm_graphs(hops: int) -> None:
    """On random small CPMM multigraphs (parallel pools, both directions, dust to large,
    fee 0..100 bps; some sourced), every chunk of the selector's own trajectory reaches
    the exhaustive same-state maximum (`agree` or an equal-value `tie`), uncapped. The
    disabled-mechanism control (`dominance: off`) reproduces the actual enumeration
    trajectory exactly (plan and gross)."""
    classes: dict[str, int] = {}
    strict = compared = 0
    for seed in range(30 if hops < 5 else 12):
        bundle, cases = _random_bundle(seed, sourced=seed % 5 == 4)
        for case in cases:
            for chunks in (1, 7):
                s = trajectory(bundle, case, HISTORY, chunks=chunks, hops=hops)
                for cls, n in audit(bundle, case, s, hops).items():
                    classes[cls] = classes.get(cls, 0) + n
                strict += s.work().certified_strict_insertions
                off = trajectory(bundle, case, OFF, chunks=chunks, hops=hops)
                e = trajectory(bundle, case, "enumeration", chunks=chunks, hops=hops)
                assert off.status == e.status and _pools(off) == _pools(e), (seed, case)
                assert off.gross == e.gross
                if s.status == "ok":
                    assert s.gross is not None  # the evaluator accepted the merged plan
                    compared += 1
    assert set(classes) <= {"agree", "tie"}, classes
    assert classes.get("agree", 0) > 100 and strict > 0 and compared > 50


def test_every_chunk_matches_the_oracle_on_real_cl_lb_cpmm_states() -> None:
    """The `mantle_mixed` fixture (real Uniswap v3, Merchant Moe LB and Classic states,
    partial fills and collected-range limits): each chunk of the selector's trajectory
    matches the oracle's exact-quote maximum at 3 and 4 hops, uncapped."""
    bundle = load_bundle(ROUTING_FIXTURES / "mantle_mixed")
    total: dict[str, int] = {}
    for case in bundle.cases:
        for hops, chunks in ((3, 13), (4, 7)):
            s = trajectory(bundle, case, HISTORY, chunks=chunks, hops=hops)
            assert s.status == "ok" and s.gross is not None
            for cls, n in audit(bundle, case, s, hops).items():
                total[cls] = total.get(cls, 0) + n
    assert set(total) <= {"agree", "tie"} and total["agree"] > 20, total


# ================================================================ 3e. caps and budgets


def test_caps_are_visible_approximations_never_silent() -> None:
    """Capping X2's retained labels to one per signature reintroduces the failure-domain
    loss, and the chunk says so (`labels_dropped_signature_cap`); a frontier cap of one
    label per layer loses R1's path the same visible way."""
    mixed = load_bundle(ROUTING_FIXTURES / "mantle_mixed")
    usdt, wmnt = (
        "0x201eba5cc46d216ce6dc03f6a759e8e766e956ae",
        "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8",
    )
    bundle = with_pools(
        cp_bundle({"big": ["S", usdt, 10**12, 10**14], "small": ["S", usdt, 10**9, 10**9]}),
        mixed.pools["0x4cdfc22bf05209de87ee564746dc7e5174631d2b"],
        ConstantProductPoolState("weak", "S", wmnt, 10**9, 10**15, 30, None),
    )
    case = Case("x2", "S", wmnt, 10**7)
    capped = trajectory(bundle, case, SelectorOptions(max_labels_per_signature=1), chunks=1, hops=3)
    assert capped.work().labels_dropped_signature_cap == 1
    assert audit(bundle, case, capped, 3) == {"miss_capped": 1}
    r1, r1_case = _recon("R1_prefix_merge_vs_cycle")
    narrow = trajectory(r1, r1_case, SelectorOptions(max_frontier_labels=1), chunks=1, hops=4)
    assert narrow.work().labels_dropped_frontier_cap > 0
    assert audit(r1, r1_case, narrow, 4) == {"miss_capped": 1}


def test_relaxation_budget_is_declared_truncation_in_the_registered_unit() -> None:
    bundle, case = _recon("R1_prefix_merge_vs_cycle")
    free = trajectory(bundle, case, HISTORY, chunks=3, hops=4)
    cut = trajectory(bundle, case, HISTORY, chunks=3, hops=4, budget=Budget(max_candidates=3))
    assert cut.work().truncated_by == "max_candidates"
    assert cut.work().label_relaxations == 3 * 3 < free.work().label_relaxations


# ================================================================ 3f. memo bindings


def test_memo_records_every_fixture_the_h4_classification_and_the_probe() -> None:
    for name in FIX["fixtures"]:
        assert f"`{name}`" in MEMO, name
    h4 = FIX["h4_real_corpus"]
    assert h4["sha256"].startswith("a0306f02") and h4["unexplained_chunks"] == 0
    totals = {"agree": 398, "prefix_admission": 6, "tie": 174, "token_revisit": 22}
    assert h4["class_totals"] == totals and len(h4["cases"]) == 12
    per_case: dict[str, int] = {}
    for row in h4["cases"]:
        for cls, n in row["classes"].items():
            per_case[cls] = per_case.get(cls, 0) + n
    assert per_case == totals
    assert sum(int(r["l4_minus_e4_gross"]) > 0 for r in h4["cases"]) == 7
    probe = FIX["probe"]
    assert probe["spec_sha256"] == spec_sha256()  # the evidence was produced by this spec
    assert probe["chunk_classes"] == {"agree": 4595, "tie_capped": 205}
    assert probe["cases"] == 96 and probe["labels_dropped_frontier_cap"] == 0
    assert probe["peak_frontier_labels_max"] == 27 and "max_frontier_labels: 1024" in MEMO
    for word in ("narrow_go", "labels_retained_unknown", "max_labels_per_signature", "state_cap"):
        assert word in MEMO


# ================================================================ bounded tuning probe (not a test)


def _probe(argv: Sequence[str]) -> None:
    """`probe <bundle> <out.json> [--cap N] [--frontier N] [--oracle] [case ids...]`"""
    args = list(argv)
    bundle_dir, out = args.pop(0), args.pop(0)
    cap = frontier = None
    oracle = False
    ids: list[str] = []
    while args:
        a = args.pop(0)
        if a == "--cap":
            cap = int(args.pop(0))
        elif a == "--frontier":
            frontier = int(args.pop(0))
        elif a == "--oracle":
            oracle = True
        else:
            ids.append(a)
    options = SelectorOptions("history", cap, frontier)
    bundle = load_bundle(Path(bundle_dir))
    rows: list[dict[str, Any]] = []
    for case in [c for c in bundle.cases if not ids or c.case_id in ids]:
        t0 = time.perf_counter()
        s = trajectory(bundle, case, options, chunks=50, hops=4)
        row: dict[str, Any] = {
            "case_id": case.case_id,
            "status": s.status,
            "gross": None if s.gross is None else str(s.gross),
            "seconds": round(time.perf_counter() - t0, 3),
            "quotes_executed": s.quotes_executed,
            "chunks_capped": sum(bool(r.work and r.work.capped) for r in s.records),
            "work": dataclasses.asdict(s.work()),
        }
        if oracle:
            t1 = time.perf_counter()
            row["audit"] = audit(bundle, case, s, 4)
            row["audit_seconds"] = round(time.perf_counter() - t1, 3)
        rows.append(row)
        print(json.dumps({k: row[k] for k in ("case_id", "status", "seconds")}), flush=True)
    doc = {
        "pass": "WHI-1549 bounded tuning probe (separate diagnostic pass; not a measured solve)",
        "bundle_hash": bundle.bundle_hash,
        "settings": {"chunks": 50, "label_hops": 4, "options": dataclasses.asdict(options)},
        "spec_sha256": spec_sha256(),
        "rows": rows,
    }
    Path(out).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "probe":
        _probe(sys.argv[2:])
    else:
        sys.exit(
            "usage: PYTHONPATH=. python test_history_labels_contract.py probe <bundle> <out> ..."
        )

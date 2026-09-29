"""`metis_inspired` (WHI-1449): a **Metis-inspired experimental Python variant** of
`incremental_graph` -- NOT Jupiter Metis, no production equivalence, no source parity (no
Metis routing source is public). It implements the WHI-1448 decision section,
docs/references/jupiter-metis-challenge.md §9.2 (mechanism) and §§10.3-10.6 (settings,
arms, fixtures, gates), and nothing else.

**What is borrowed, and how far.** J1 (the archived Jupiter v3 Metis post) names a
"heavily modified" Bellman-Ford variant that combines route generation and quoting; the
modifications are not described. The chunk loop that already realizes J1's incremental
split/merge (C2/C3) is `incremental_graph`'s, unchanged in behaviour: integer chunks with
carry, aggregate `f(x + m) - f(x)` accounting on the pools' original states, atomic
`creates_cycle` admission, one merged step per pool (`merged_plan`), the independent
`evaluate` replay, the retained simpler `path_split` candidate at `search.max_hops`
(strictly beaten or kept), the guarded quote meter and the statuses. Only the per-chunk
choice changes (`graph.label_pruning: true`): instead of scoring every enumerated path, a
hop-layered, quote-driven label search in the textbook Bellman-Ford shape (W1) -- every
choice below is this repository's inference (*[inferred]* in the memo), not a Metis fact:

- layer `k` (1..`graph.label_hops`) keeps, per token, the one label (amount, path) reached
  in exactly `k` hops with the **strictly** largest chunk marginal (ties keep the label
  found first; layers are relaxed from the previous layer only, never in place);
- tokens of a layer are expanded in insertion order and their edges in the bundle's
  adjacency order (deterministic across processes);
- a relaxation never returns to the source, never passes through the target and never
  revisits a token of its own label's path (token-simple), and is skipped when the target
  is structurally out of reach in the remaining hops (`dist`: breadth-first hops to the
  target on the pool graph without edges into the source or out of the target -- an exact
  prune: it removes only prefixes no enumerated path extends);
- a relaxation's prefix must pass `creates_cycle` against the committed token edges
  **before** it is quoted (atomic over the prefix's new edges);
- one relaxation = one edge marginal quote on the pool's original state at
  `x_pool + label amount`, through the same guarded `QuoteCache`; a failing quote
  (`incomplete_snapshot`, insufficient liquidity, a partial fill, `nonmonotone`) is counted
  and yields no label;
- a relaxation into the target competes, strictly, for the chunk's choice.

`graph.label_pruning: false` is the disabled-mechanism ablation: the chunk choice is
`incremental_graph`'s own exhaustive loop over `enumerate_paths(..., graph.label_hops)`.
With `graph.label_hops == search.max_hops` the whole solve then equals `incremental_graph`
(plan, evaluation, statuses and every logical counter; X7). `graph.label_hops` may exceed
`search.max_hops` (arm M4): only the chunk search goes deeper; the embedded simpler
candidate stays at `search.max_hops`.

**Settings** (`graph.*`, explicit, no default): `chunks` (as `incremental_graph`),
`label_hops` (integer >= `search.max_hops`) and `label_pruning` (bool). No k-best label
count or other knob exists.

**Divergence from exhaustive scoring** (memo §9.3; derived, checked empirically by gate
S2 with `diagnose_case`). With identical committed state the label search can choose a
different chunk path only by: (1) a **tie** in the maximal marginal resolved in another
order; (2) a **non-downward-closed quote failure** -- the maximal label's larger amount
makes a later edge fail where a dominated smaller amount succeeds; (3) **budget order** --
`max_quotes` / `max_candidates` truncate at a different point; and, only for
`label_hops >= 4`, (4) **token-revisit pruning** -- the best prefix to a token visits a
token the best continuation needs -- and (4b, this implementation's note beyond the
memo) **prefix-dependent admission** -- the best prefix's token edges close a cycle with
the committed edges on a continuation that a dominated prefix could take. For
`label_hops <= 3` neither (4) nor (4b) can occur (a layer-1 prefix is `{S, t}`; the last
hop enters the target, which is never on a prefix and has no committed outgoing edge), and
every relaxation is a prefix `incremental_graph` also quotes at the same amount.

**Counters (declared units).** Label mode reports relaxations, not paths:
`label_relaxations` (every quote-backed relaxation attempted, including a chunk a quote
budget aborted), `label_rejected_cycle`, `label_pruned_distance`, `label_skipped_revisit`,
`label_truncated_chunks`; the path-unit keys (`paths_enumerated`, `paths_scored`,
`paths_rejected_cycle`, `paths_truncated`) are `None` there, and set exactly as
`incremental_graph` sets them in ablation mode. `candidate_unit` names the unit of
`candidates_considered` / `candidates_truncated` beyond the embedded `path_split`'s.
`Budget.max_candidates` caps relaxations per chunk in label mode (the search of that chunk
stops, declared truncation) and paths per chunk in ablation mode. `chunk_path_hops` is the
hop histogram of the committed chunk paths.

**Diagnostics are not the solve.** `diagnose_case` is a separate, explicitly labeled
correctness pass (gate S2): it replays the label trajectory on its own `QuoteCache` and, at
every chunk, re-scores the exhaustive enumeration maximum on the identical committed flows
and carried amount with a second, separate cache and counters. The registry, the runner
and every profile call `solve` only; nothing of the diagnostic reaches measured latency,
quote counts or budgets.
"""

from __future__ import annotations

import dataclasses
from collections import deque
from collections.abc import Callable, Mapping
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
)
from routing.algorithms.incremental_graph import (
    PoolFlow,
    chunk_amounts,
    creates_cycle,
    merged_plan,
    topology,
)
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import REQUEST_FUND_ID, RoutePlan
from routing.search import Edge, GraphIndex, Path, QuoteCache, enumerate_paths, path_label
from snapshot.models import Case, PoolState, SnapshotBundle

NAME = "metis_inspired"

CAPABILITIES = incremental_graph.CAPABILITIES
SEARCH_PARAMS = incremental_graph.SEARCH_PARAMS
GRAPH_PARAMS = ("chunks", "label_hops", "label_pruning")

PROVENANCE: Mapping[str, Any] = MappingProxyType(
    {
        "experimental": True,
        "opt_in": True,
        "issue": "WHI-1449",
        "identity": (
            "Metis-inspired experimental Python variant; NOT Jupiter Metis; no production "
            "equivalence, global-optimality or source-parity claim"
        ),
        "contract": (
            "docs/references/jupiter-metis-challenge.md §9.2 and §§10.3-10.6 (WHI-1448, "
            "PR #50, c5b56369155b4beddef8de4df64e63dd0118662c)"
        ),
        "reference": incremental_graph.NAME,
        "sources": {
            "J1": {
                "title": "Archived: Jupiter v3: The Metis Routing Algo (historical)",
                "url": "https://discuss.jup.ag/raw/21712",
                "sha256": "825598a5af7e5457721433f11d0e008304a7a1b92a91fa403efe50fb66492169",
                "retrieved": "2026-09-28",
                "used_for": "C1 Bellman-Ford variant; C4 route generation and quoting combined",
            },
            "J2": {
                "title": "Jupiter docs: Reduce Latency (current mode=fast beta)",
                "url": "https://developers.jup.ag/docs/swap/advanced/reduce-latency.md",
                "sha256": "d7ab87af2f137ff94c3ded402df6b62d152cf01f66f990d0925ea81acc113e53",
                "retrieved": "2026-09-28",
                "used_for": "C8 Bellman-Ford path engine with splitting as a separate layer",
            },
            "W1": {
                "title": "Bellman-Ford algorithm (public textbook algorithm)",
                "url": "https://en.wikipedia.org/wiki/Bellman%E2%80%93Ford_algorithm",
                "used_for": "hop-layered label relaxation shape only; no code reused",
            },
        },
        "source_gaps": (
            "no public Metis routing source; J1's modifications, the current splitter, "
            "Metis's accounting for reused DEXs and its budgets are unknown"
        ),
        "inferred": [
            "one label per (hop layer, token), strict greater-than dominance, ties keep the "
            "earlier label",
            "layers relaxed from the previous layer only (no in-place relaxation)",
            "deterministic token insertion order and bundle adjacency order",
            "token-simple relaxations; never back to the source or through the target",
            "structural hops-to-target prune",
            "atomic creates_cycle admission of each relaxation prefix before its quote",
            "chunk marginals f(x+m)-f(x) on original pool states (incremental_graph rule)",
        ],
        "deviations": [
            "no k-best labels: token-revisit pruning (label_hops >= 4) is accepted, not repaired",
            "Budget.max_candidates caps relaxations per chunk in label mode (unit change)",
            "label_hops may exceed search.max_hops; the retained path_split candidate stays "
            "at search.max_hops",
        ],
        "divergence_classes": [
            "tie",
            "quote_failure (non-downward-closed)",
            "budget_order",
            "token_revisit (label_hops >= 4)",
            "prefix_admission (label_hops >= 4)",
        ],
        "ablation": "graph.label_pruning false: incremental_graph's per-chunk enumeration",
    }
)


class MetisInspiredConfigError(ValueError):
    """`prepare` received an invalid or missing `search.*` / `graph.*` value."""


@dataclasses.dataclass(frozen=True)
class PreparedMetisInspired:
    """Immutable per-worker preparation: `incremental_graph`'s prepared object (graph
    index, hop bound, grid, chunks) plus the label settings."""

    graph: incremental_graph.PreparedIncrementalGraph
    label_hops: int
    label_pruning: bool

    @property
    def index(self) -> GraphIndex:
        return self.graph.path_split.single_path.index

    @property
    def max_hops(self) -> int:
        return self.graph.path_split.single_path.max_hops


def prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> PreparedMetisInspired:
    params = config.params
    hops, pruning = params.get("label_hops"), params.get("label_pruning")
    if not isinstance(pruning, bool):
        raise MetisInspiredConfigError(
            f"{NAME} requires graph.label_pruning as an explicit bool, got {pruning!r}"
        )
    if not isinstance(hops, int) or isinstance(hops, bool) or hops < 1:
        raise MetisInspiredConfigError(
            f"{NAME} requires graph.label_hops as an integer >= 1, got {hops!r}"
        )
    try:
        graph = incremental_graph.prepare(bundle, config)
    except incremental_graph.IncrementalGraphConfigError as exc:
        raise MetisInspiredConfigError(f"{NAME}: {exc}") from exc
    max_hops = graph.path_split.single_path.max_hops
    if hops < max_hops:
        raise MetisInspiredConfigError(
            f"{NAME} requires graph.label_hops >= search.max_hops ({max_hops}), got {hops}"
        )
    return PreparedMetisInspired(graph, hops, pruning)


def hops_to_target(index: GraphIndex, source: str, target: str) -> dict[str, int]:
    """Fewest pools from each token to `target` on the structural graph without edges
    into `source` or out of `target` (breadth-first from the target; pools are
    bidirectional, so a token's predecessors are its neighbours). A lower bound on every
    token-simple completion, so pruning by it is exact."""
    dist = {target: 0}
    queue = deque([target])
    while queue:
        w = queue.popleft()
        if w == source:
            continue  # edges into the source are ignored
        for e in index.edges_from(w):
            u = e.token_out
            if u != target and u not in dist:
                dist[u] = dist[w] + 1
                queue.append(u)
    return dist


class _BudgetExhausted(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class _Failure:
    """A path prefix whose marginal quote is unusable in one chunk (as
    `incremental_graph._Failure`: counted reason, prefix length, incomplete-state note)."""

    reason: str
    prefix: int
    note: str | None = None


class _ChunkFailed(Exception):
    def __init__(self, failure: _Failure) -> None:
        super().__init__(failure.reason)
        self.failure = failure


@dataclasses.dataclass(frozen=True)
class Label:
    """The best way found to reach a token in exactly `len(path)` hops in one chunk:
    the chunk's marginal `amount` there, the path and its pools' tentative aggregates."""

    amount: int
    path: Path
    updates: tuple[PoolFlow, ...]
    tokens: frozenset[str]


Choice = tuple[int, Path, list[PoolFlow]]
QuoteFn = Callable[[PoolState, str, int], SwapResult[PoolState]]


class _Allocator:
    """Mutable per-solve chunk-allocation state and the two chunk choosers. Created
    fresh for every solve (and separately for each side of a diagnostic pass)."""

    def __init__(
        self, bundle: SnapshotBundle, case: Case, index: GraphIndex, quote: QuoteFn
    ) -> None:
        self.bundle, self.case, self.index, self.quote = bundle, case, index, quote
        self.flows: dict[str, PoolFlow] = {}
        self.token_edges: set[tuple[str, str]] = set()
        self.failures: dict[str, int] = {}
        self.incomplete: list[str] = []
        self.truncated_by: str | None = None
        # enumeration (path units, incremental_graph's semantics)
        self.scored = self.rejected_cycle = self.own_truncated = 0
        # label search (relaxation units)
        self.relaxations = self.label_cycle = self.label_distance = self.label_revisit = 0
        self.label_truncated = 0
        # diagnostic hooks (None in a measured solve)
        self.relaxed: list[Path] | None = None
        self.layers: list[dict[str, Label]] | None = None

    # ---- shared marginal rule (incremental_graph.marginal, edge by edge)

    def _count(self, failure: _Failure, path: Path) -> None:
        self.failures[failure.reason] = self.failures.get(failure.reason, 0) + 1
        if failure.note is not None:
            self.incomplete.append(path_label(path) + failure.note)

    def step(self, edge: Edge, m: int, depth: int) -> tuple[int, PoolFlow] | _Failure:
        """The marginal output of `m` more input into `edge`'s pool given the committed
        aggregate, and the pool's new aggregate; a `_Failure` (uncounted) otherwise."""
        prev = self.flows.get(edge.pool_id)
        x, out = (prev.amount_in, prev.amount_out) if prev else (0, 0)
        order = prev.order if prev else len(self.flows)
        if m == 0:  # zero marginal input: nothing changes, no pool call
            return 0, PoolFlow(edge, x, out, order)
        r = self.quote(self.bundle.pools[edge.pool_id], edge.token_in, x + m)
        if r.status is not QuoteStatus.OK or r.amount_in_consumed != x + m:
            reason = r.status.value if r.status is not QuoteStatus.OK else "partial_fill"
            note = (
                f" @ {x + m}: {r.detail}" if r.status is QuoteStatus.INCOMPLETE_SNAPSHOT else None
            )
            return _Failure(reason, depth, note)
        if r.amount_out < out:
            return _Failure("nonmonotone", depth)
        return r.amount_out - out, PoolFlow(edge, x + m, r.amount_out, order)

    # ---- ablation: incremental_graph's per-chunk loop, verbatim semantics

    def _marginal(
        self, path: Path, amount: int, memo: dict[Path, Any]
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
            result = self.step(edge, m, i + 1)
            if isinstance(result, _Failure):
                memo[key] = result
                self._count(result, path)
                raise _ChunkFailed(result)
            m, update = result
            memo[key] = (m, update)
            updates.append(update)
        return m, updates

    def choose_enumeration(
        self, amount: int, paths: list[Path], budget: Budget, memo: dict[Path, Any] | None = None
    ) -> Choice | None:
        memo = {} if memo is None else memo
        choice: Choice | None = None
        chunk_scored = 0
        for j, path in enumerate(paths):
            if budget.max_candidates is not None and chunk_scored >= budget.max_candidates:
                self.truncated_by = self.truncated_by or "max_candidates"
                self.own_truncated += len(paths) - j
                break
            if creates_cycle(self.token_edges, path):
                self.rejected_cycle += 1
                continue
            chunk_scored += 1
            try:
                m, updates = self._marginal(path, amount, memo)
            except _ChunkFailed:
                continue
            if choice is None or m > choice[0]:
                choice = (m, path, updates)
        self.scored += chunk_scored
        return choice

    # ---- the mechanism: hop-layered, quote-driven label search (memo §9.2)

    def choose_labels(
        self, amount: int, hops: int, dist: Mapping[str, int], budget: Budget
    ) -> Choice | None:
        source, target = self.case.token_in, self.case.token_out
        layers: list[dict[str, Label]] = [{source: Label(amount, (), (), frozenset((source,)))}]
        self.layers = layers if self.relaxed is not None else None
        best: Choice | None = None
        chunk_relaxed = 0
        for k in range(1, hops + 1):
            layer: dict[str, Label] = {}
            layers.append(layer)
            for t, lab in layers[k - 1].items():  # insertion order; never the target
                for e in self.index.edges_from(t):  # adjacency order
                    v = e.token_out
                    if v == source:
                        continue
                    # Out of reach in the remaining hops (for k == hops this is v != target).
                    if v != target and dist.get(v, hops + 1) > hops - k:
                        self.label_distance += 1
                        continue
                    if v in lab.tokens:  # token-simple
                        self.label_revisit += 1
                        continue
                    p = (*lab.path, e)
                    if creates_cycle(self.token_edges, p):
                        self.label_cycle += 1
                        continue
                    if budget.max_candidates is not None and chunk_relaxed >= budget.max_candidates:
                        self.truncated_by = self.truncated_by or "max_candidates"
                        self.label_truncated += 1
                        return best
                    chunk_relaxed += 1
                    self.relaxations += 1
                    if self.relaxed is not None:
                        self.relaxed.append(p)
                    result = self.step(e, lab.amount, k)
                    if isinstance(result, _Failure):
                        self._count(result, p)
                        continue
                    m, update = result
                    if v == target:
                        if best is None or m > best[0]:  # strict: ties keep the earlier
                            best = (m, p, [*lab.updates, update])
                    elif v not in layer or m > layer[v].amount:  # strict dominance
                        layer[v] = Label(m, p, (*lab.updates, update), lab.tokens | {v})
        return best

    def commit(self, path: Path, updates: list[PoolFlow]) -> None:
        for update in updates:
            self.flows[update.edge.pool_id] = update
        self.token_edges.update((e.token_in, e.token_out) for e in path)


def _source_paths(ev: Evaluation) -> list[Path]:
    """The legs of a `path_split`-family plan, read from the evaluator's trace (the same
    rule as `incremental_graph`'s, for the `topology` of a retained simpler plan)."""
    legs: list[list[Edge]] = []
    for t in ev.trace:
        if t.inputs[0][0] == REQUEST_FUND_ID or not legs:
            legs.append([])
        legs[-1].append(Edge(t.pool_id, t.token_in, t.token_out))
    return [tuple(leg) for leg in legs]


def solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    prepared = context.prepared
    if not isinstance(prepared, PreparedMetisInspired):
        raise TypeError(f"{NAME}.solve needs the PreparedMetisInspired returned by prepare()")
    bundle, objective = context.bundle, context.objective
    ps_prepared = prepared.graph.path_split
    max_hops, hops, pruning = prepared.max_hops, prepared.label_hops, prepared.label_pruning
    cache = QuoteCache(bundle)
    quiet = dataclasses.replace(context, candidate_sink=None)

    # ---- 1. simpler candidates: path_split at search.max_hops (unchanged).
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
            _source_paths(ps.evaluation),
        )
        context.report_candidate(ps.plan)

    # ---- 2. incremental chunk allocation (the chunk choice is the only change).
    def guarded(state: PoolState, token_in: str, amount: int) -> SwapResult[PoolState]:
        memo_hit = bundle.pools.get(state.pool_id) is state and cache.cached(
            state.pool_id, token_in, amount
        )
        if budget.max_quotes is not None and cache.misses >= budget.max_quotes and not memo_hit:
            alloc.truncated_by = "max_quotes"
            raise _BudgetExhausted
        return cache(state, token_in, amount)

    index = prepared.index
    alloc = _Allocator(bundle, case, index, guarded)
    paths = [] if pruning else list(enumerate_paths(index, case.token_in, case.token_out, hops))
    dist = hops_to_target(index, case.token_in, case.token_out) if pruning else {}
    reachable = (
        case.token_in != case.token_out and dist.get(case.token_in, hops + 1) <= hops
        if pruning
        else bool(paths)
    )
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
            raise _ChunkFailed(_Failure("no_paths", 0))
        last = max(k for k, a in enumerate(amounts) if a > 0)
        carry = 0
        for k, chunk in enumerate(amounts):
            if chunk == 0:
                continue
            amount = carry + chunk
            choice = (
                alloc.choose_labels(amount, hops, dist, budget)
                if pruning
                else alloc.choose_enumeration(amount, paths, budget)
            )
            if k != last and (choice is None or choice[0] == 0):
                carry, carried = amount, carried + 1
                continue
            if choice is None:
                incremental_status = f"chunk_{k + 1}_no_admissible_path"
                raise _ChunkFailed(_Failure(incremental_status, 0))
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
    except _BudgetExhausted:
        incremental_status = "truncated"
        if pruning:
            alloc.label_truncated += 1
        else:
            alloc.own_truncated += 1
    except _ChunkFailed:
        pass

    if incremental is not None:
        plan, ev, score = incremental
        if best is None or score > best[3]:
            best = (NAME, plan, ev, score, chunk_paths)
            context.report_candidate(plan)

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
    ps_truncated = ps.search_stats.get("truncated_by")
    truncated_by = alloc.truncated_by
    own_work = alloc.relaxations if pruning else alloc.scored
    own_truncated = alloc.label_truncated if pruning else alloc.own_truncated

    def paths_only(value: int) -> int | None:
        return None if pruning else value

    def labels_only(value: int) -> int | None:
        return value if pruning else None

    stats: dict[str, Any] = {
        "chunks": prepared.graph.chunks,
        "chunks_empty": sum(a == 0 for a in amounts),
        "chunks_carried": carried,
        "chunks_allocated": len(chunk_paths),
        "max_hops": max_hops,
        "max_splits": ps_prepared.direct_split.max_splits,
        "percent_step": ps_prepared.direct_split.percent_step,
        "paths_enumerated": paths_only(len(paths)),
        "paths_scored": paths_only(alloc.scored),
        "paths_rejected_cycle": paths_only(alloc.rejected_cycle),
        "paths_truncated": paths_only(alloc.own_truncated),
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
        "truncated_by": truncated_by or ps_truncated,
        "truncated_stages": ps.search_stats.get("truncated_stages", [])
        + ([NAME] if truncated_by else []),
        "quotes_executed": cache.misses,
        "quotes_memoized": cache.hits,
        "chosen_source": None,
        "topology": None,
        # ---- metis_inspired's own (declared units)
        "label_hops": hops,
        "label_pruning": pruning,
        "chunk_search": "label" if pruning else "enumeration",
        "candidate_unit": "label_relaxation" if pruning else "path",
        "label_relaxations": labels_only(alloc.relaxations),
        "label_rejected_cycle": labels_only(alloc.label_cycle),
        "label_pruned_distance": labels_only(alloc.label_distance),
        "label_skipped_revisit": labels_only(alloc.label_revisit),
        "label_truncated_chunks": labels_only(alloc.label_truncated),
        "chunk_path_hops": dict(sorted(hop_histogram.items())),
    }
    common: dict[str, Any] = {
        "case_id": case.case_id,
        "algorithm": NAME,
        "candidates_considered": ps.candidates_considered + own_work,
        "candidates_truncated": ps.candidates_truncated + own_truncated,
        "search_stats": stats,
    }
    if best is not None:
        source, plan, ev, score, route_paths = best
        stats["chosen_source"] = source
        stats["topology"] = topology(route_paths)
        return SolveResult(status=SolveStatus.OK, plan=plan, evaluation=ev, score=score, **common)
    if truncated_by is not None or ps.status is SolveStatus.TIMEOUT:
        return SolveResult(
            status=SolveStatus.TIMEOUT,
            error=(
                f"declared {stats['truncated_by']} budget truncated the search with no valid "
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


# ===================================================================================
# S2 diagnostic pass (memo §10.6): NOT part of `solve`; never timed, metered or budgeted.
# ===================================================================================

# Classes a disagreeing chunk may be attributed to, by hop bound (memo §9.3). `budget_order`
# (class 3) is decided per case from the measured records' truncation, not per chunk: the
# diagnostic itself runs unbudgeted.
ALLOWED_CLASSES_UP_TO_3 = frozenset({"tie", "quote_failure"})
ALLOWED_CLASSES_FROM_4 = ALLOWED_CLASSES_UP_TO_3 | {"token_revisit", "prefix_admission"}


def allowed_classes(label_hops: int) -> frozenset[str]:
    return ALLOWED_CLASSES_UP_TO_3 if label_hops <= 3 else ALLOWED_CLASSES_FROM_4


def _choice_dict(choice: Choice | None) -> dict[str, Any]:
    if choice is None:
        return {"marginal": None, "path": None, "hops": None}
    return {"marginal": str(choice[0]), "path": path_label(choice[1]), "hops": len(choice[1])}


def _attribute(label: _Allocator, target_path: Path, memo: Mapping[Path, Any]) -> tuple[str, str]:
    """Why the label search missed the enumeration maximum `target_path` in this chunk:
    walk the maximal path's tokens through the label table. By induction the label at
    each of its tokens holds at least the maximal path's prefix marginal, until one of the
    enumerated reasons breaks the chain; anything else is `unattributed` (a bug or an
    unsound dominance assumption)."""
    layers = label.layers
    assert layers is not None
    target = label.case.token_out
    lab = layers[0][label.case.token_in]
    for i, e in enumerate(target_path):
        hit = memo[target_path[: i + 1]]
        want = hit[0]
        v = e.token_out
        at = f"layer {i}: label {path_label(lab.path)} (amount {lab.amount})"
        if v in lab.tokens:
            return "token_revisit", f"{at} already visits {v}, needed by {path_label(target_path)}"
        if creates_cycle(label.token_edges, (*lab.path, e)):
            return "prefix_admission", f"{at} + {e.pool_id} closes a committed token cycle"
        result = label.step(e, lab.amount, i + 1)  # the relaxation the search made (cached)
        if isinstance(result, _Failure):
            prefix = memo[target_path[:i]][0] if i else layers[0][label.case.token_in].amount
            return (
                "quote_failure",
                f"{at} -[{e.pool_id}]-> {v} fails ({result.reason}) where the maximal path's "
                f"prefix amount {prefix} succeeds",
            )
        m = result[0]
        if m < want:
            return "unattributed", f"{at} -[{e.pool_id}]-> {v}: {m} < prefix marginal {want}"
        if v == target:
            return "unattributed", f"{at} reaches the target with {m} >= {want}, not chosen"
        nxt = layers[i + 1].get(v)
        if nxt is None or nxt.amount < m:
            return "unattributed", f"layer {i + 1} lost a relaxation of {m} into {v}"
        lab = nxt
    return "unattributed", "the walk ended without a reason"


def diagnose_case(
    case: Case, bundle: SnapshotBundle, prepared: PreparedMetisInspired
) -> dict[str, Any]:
    """The S2 per-chunk enumeration-maximum diagnostic, a **separate correctness pass**.

    Replays the label-search chunk trajectory (the one `solve` commits, unbudgeted) on
    its own `QuoteCache`, and at every non-empty chunk re-scores `incremental_graph`'s
    exhaustive per-chunk loop over `enumerate_paths(..., label_hops)` on the **identical**
    committed flows, committed token edges and actual (carried) amount, with a second,
    separate cache. Each chunk is `agree` or attributed to a memo §9.3 class (`tie`,
    `quote_failure`, `token_revisit`, `prefix_admission`) or `unattributed`. It also
    checks the quote-subset claim: every label relaxation must be a prefix the enumeration
    quoted in that chunk (`relaxations_outside_enumeration`). Returns a JSON-shaped record
    with its own counters; nothing here is measured solver work."""
    index, hops = prepared.index, prepared.label_hops
    source, target = case.token_in, case.token_out
    label_cache, enum_cache = QuoteCache(bundle), QuoteCache(bundle)
    label = _Allocator(bundle, case, index, label_cache)
    enum = _Allocator(bundle, case, index, enum_cache)
    unbudgeted = Budget()
    paths = list(enumerate_paths(index, source, target, hops)) if source != target else []
    dist = hops_to_target(index, source, target)
    amounts = chunk_amounts(case.amount_in, prepared.graph.chunks)
    records: list[dict[str, Any]] = []
    chunk_paths: list[Path] = []
    chunk_inputs: list[int] = []
    status = "no_paths" if not paths else "ok"
    if paths:
        last = max(k for k, a in enumerate(amounts) if a > 0)
        carry = 0
        for k, chunk in enumerate(amounts):
            if chunk == 0:
                continue
            amount = carry + chunk
            enum.flows, enum.token_edges = dict(label.flows), set(label.token_edges)
            label.relaxed = []
            before = (label.relaxations, enum.scored, label_cache.misses, enum_cache.misses)
            got = label.choose_labels(amount, hops, dist, unbudgeted)
            memo: dict[Path, Any] = {}
            want = enum.choose_enumeration(amount, paths, unbudgeted, memo)
            if got is None and want is None:
                cls, detail = "agree", "no admissible path on either side"
            elif want is None:
                cls, detail = "unattributed", "label found a path the enumeration did not"
            elif got is not None and got[0] == want[0]:
                cls = "agree" if got[1] == want[1] else "tie"
                detail = "" if cls == "agree" else "equal maximal marginal, other order"
            elif got is not None and got[0] > want[0]:
                cls, detail = "unattributed", "label marginal above the enumeration maximum"
            else:
                cls, detail = _attribute(label, want[1], memo)
            outside = sum(p not in memo for p in label.relaxed)
            records.append(
                {
                    "chunk": k + 1,
                    "amount": str(amount),
                    "label": _choice_dict(got),
                    "enumeration": _choice_dict(want),
                    "class": cls,
                    "detail": detail,
                    "label_relaxations": label.relaxations - before[0],
                    "enumeration_paths_scored": enum.scored - before[1],
                    "label_quotes_executed": label_cache.misses - before[2],
                    "enumeration_quotes_executed": enum_cache.misses - before[3],
                    "relaxations_outside_enumeration": outside,
                }
            )
            if k != last and (got is None or got[0] == 0):
                carry = amount
                continue
            if got is None:
                status = f"chunk_{k + 1}_no_admissible_path"
                break
            carry = 0
            label.commit(got[1], got[2])
            chunk_paths.append(got[1])
            chunk_inputs.append(amount)
    by_path: dict[Path, list[int]] = {}
    for path, amount in zip(chunk_paths, chunk_inputs, strict=True):
        entry = by_path.setdefault(path, [0, 0])
        entry[0] += 1
        entry[1] += amount
    classes: dict[str, int] = {}
    for r in records:
        classes[r["class"]] = classes.get(r["class"], 0) + 1
    allowed = allowed_classes(hops)
    return {
        "pass": "S2 diagnostic (separate correctness pass; not a measured solve)",
        "case_id": case.case_id,
        "label_hops": hops,
        "chunks": prepared.graph.chunks,
        "trajectory_status": status,
        "incremental_allocation": [
            {"path": path_label(p), "chunks": n, "amount_in": str(a)}
            for p, (n, a) in by_path.items()
        ],
        "incremental_chunk_sequence": [list(by_path).index(p) for p in chunk_paths],
        "classes": dict(sorted(classes.items())),
        "divergent_chunks": sum(r["class"] != "agree" for r in records),
        "unexplained_chunks": sum(
            r["class"] != "agree" and r["class"] not in allowed for r in records
        ),
        # memo §9.3 quote subset, derived for label_hops <= 3 only
        "quote_subset_violations": (
            sum(r["relaxations_outside_enumeration"] > 0 for r in records) if hops <= 3 else None
        ),
        "relaxations_outside_enumeration": sum(
            r["relaxations_outside_enumeration"] for r in records
        ),
        "counters": {
            "label_relaxations": label.relaxations,
            "label_quotes_executed": label_cache.misses,
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
)

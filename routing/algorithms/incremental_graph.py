"""`incremental_graph` (docs/DESIGN.md §2.6): a shared-pool incremental graph
heuristic. The order input is allocated in chunks; each chunk takes the path with the
best **marginal** output given the tentative state every earlier chunk left, so
different paths may share physical pools and split/merge at intermediate tokens --
what `path_split`'s pool-disjointness forbids. Heuristic only: greedy chunk order on a
finite chunk grid, no optimality claim (§2.6).

**Parameters.** Profile `graph.chunks` (§2.12: explicit per profile, swept alongside
percentage granularity at calibration) and, for the retained simpler candidates and
the candidate path set, `search.max_hops`, `search.max_splits` and
`search.percent_step` (validated by `path_split.prepare`). No built-in defaults.
`search.max_splits` bounds only the embedded `path_split`; the incremental plan's
number of distinct routes is bounded by `graph.chunks`.

**Chunks.** Chunk `k` of `K` is `floor(A*k/K) - floor(A*(k-1)/K)`, so the chunks sum
to the input exactly for every amount (a nondivisible input fully allocates); a
zero-size chunk (`A < K`) carries nothing and is skipped (`chunks_empty`). A chunk
whose best admissible marginal output is zero, or that has no admissible path at all
(e.g. dust that rounds to zero output on an unused pool), is **carried** into the next
chunk (`chunks_carried`); only the final chunk is committed with a zero marginal, and a
final chunk without any admissible path abandons the incremental plan.

**State accounting = merged execution** (the §2.6 warning: `f(x+delta) - f(x)` is not
two sequential fee-bearing swaps). The tentative state of a physical pool is its
aggregate input `x_p` and output `f_p(x_p)`, where `f_p` is the pool's exact integer
quote on its **original** bundle state in the pool's (single) used direction. A chunk
of `d` on path `e1..eh` is charged `m_1 = f_1(x_1 + d) - f_1(x_1)` on `e1`, then
`m_i = f_i(x_i + m_{i-1}) - f_i(x_i)` on each later edge; the chunk's marginal output is
`m_h`. Pools used by earlier chunks therefore show their updated state (a larger `x_p`
on a concave curve), and nothing is ever charged against duplicated initial liquidity.
This aggregate marginal is **not** what the same chunks would get as separate
sequential swaps: on a fee-bearing CPMM the fee stays in the reserves, so sequential
chunks yield strictly less than one swap of their sum -- marginal accounting executed
as sequential steps would over-claim (the §2.6 warning), which is why the chunks are
merged.

**Normalized plan: one merged step per physical pool** -- the chunks that traverse the
same pool are merged into one swap of the pool's aggregate input, which is how an
aggregating router executes it (chunks are *not* kept as sequential steps: repeated
fee-bearing calls on one pool are a different, not-accounted-for execution and would
also pay per-call cost). `merged_plan` orders the used tokens topologically; at each
token the incoming funds (the request fund, or the outputs of the pools into it) are
split across the outgoing pools by explicit amounts in first-use order, and the last
outgoing pool takes `ALL_REMAINING` of every fund still holding a balance (merged
funding when several funds enter; the explicit final allocation of any remainder).
Because the flows telescope -- a pool's aggregate output is exactly the sum of its
chunks' marginals -- the evaluated gross of this plan equals the accounted gross
(`search_stats["accounting_matches_evaluation"]`). Whatever the accounting says, only
`routing.evaluator.evaluate`'s replay of the complete plan is scored or returned; an
accounted figure the evaluated plan does not reach is never reported as output.

**Unsupported topology (explicit rules).** A chunk path is admissible only if the union
of the plan's token edges stays acyclic (`creates_cycle`): no economic token cycle
(the evaluator rejects them) and therefore no pool used in both directions. A path
whose marginal quote fails (insufficient liquidity, uncollected state, a partial fill)
or decreases a pool's aggregate output is skipped for that chunk and counted.
If some chunk has no admissible path, the incremental plan is abandoned
(`incremental_status`) and the simpler candidates stand.

**Simpler candidates are kept.** `path_split` runs first on the same per-solve
`QuoteCache` (it itself keeps `single_path` and `direct_split` as finalists), so its
best result -- the best of the three simpler algorithms -- is a candidate; the
incremental plan must strictly beat it on `ObjectiveContext.score` (ties keep the
simpler route). Chunk choices use gross marginals; a per-call cost is only applied by
the final comparison. `search_stats["topology"]` says whether the returned plan is
`single_route`, `disjoint_split` (capability-matched with `path_split`) or
`shared_pool` (an expanded-topology result: some pool feeds more than one distinct
path), and `chosen_source` names where it came from.

**Budgets.** `Budget.max_quotes` is the worker's hard meter: every new quote is checked
against the shared cache's executed count *before* it is made, and an exhausted
budget abandons the incremental plan (declared truncation, `truncated_by`).
`Budget.max_candidates` caps the paths scored per chunk (hop-major, so the longest are
truncated first; `paths_truncated`), besides `path_split`'s own use of it. The time
limit is the runner's; every new best plan goes to `SolveContext.report_candidate`.

**Experimental exact reuse (WHI-1507 / L05), default off.** `solve(...,
graph_reuse=True)` -- selected only by an explicit caller; the registry, runner and every
profile run the reference loop -- keeps each path's chunk result per actual amount until a
committed chunk touches one of its pools, and decides cycle admission from a transitive
closure of the committed edges, keeping monotone rejections (`_ExactReuse`). Plans,
evaluations, statuses, budgets and every logical counter are the reference's; only the
physical `quotes_memoized` is lower, and `search_stats["graph_reuse"]` reports the
physical reuse/recomputation counters (including a chunk a budget aborted). No adoption
or speedup claim: the performance decision is WHI-1510's.

**Statuses** as `path_split`: `ok`; `timeout` (a declared budget truncated the search
and nothing valid was found -- never evidence of `no_route`); `incomplete_snapshot`;
`no_route` (`path_split`'s reason).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any

from pools.result import QuoteStatus, SwapResult
from routing.algorithms import path_split
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
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from routing.search import Edge, Path, QuoteCache, enumerate_paths, path_label
from snapshot.models import Case, PoolState, SnapshotBundle

NAME = "incremental_graph"

CAPABILITIES = Capabilities(multi_hop=True, split=True, shared_pools=True)
SEARCH_PARAMS = path_split.SEARCH_PARAMS
GRAPH_PARAMS = ("chunks",)


class IncrementalGraphConfigError(ValueError):
    """`prepare` received an invalid or missing `search.*`/`graph.*` value."""


class PreparedIncrementalGraph:
    """Immutable per-worker preparation: `path_split`'s prepared object (graph index,
    hop bound, grid) and the chunk count."""

    __slots__ = ("chunks", "path_split")

    def __init__(self, split: path_split.PreparedPathSplit, chunks: int) -> None:
        self.path_split = split
        self.chunks = chunks


def prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> PreparedIncrementalGraph:
    chunks = config.params.get("chunks")
    if not isinstance(chunks, int) or isinstance(chunks, bool) or chunks < 1:
        raise IncrementalGraphConfigError(
            f"{NAME} requires graph.chunks as an integer >= 1, got {chunks!r}"
        )
    try:
        split = path_split.prepare(bundle, config)
    except path_split.PathSplitConfigError as exc:
        raise IncrementalGraphConfigError(f"{NAME}: {exc}") from exc
    return PreparedIncrementalGraph(split, chunks)


def chunk_amounts(amount_in: int, chunks: int) -> list[int]:
    """The `chunks` integer chunk sizes (see the module docstring); they sum to
    `amount_in` exactly. Zero-size chunks are included (callers skip them)."""
    return [amount_in * k // chunks - amount_in * (k - 1) // chunks for k in range(1, chunks + 1)]


def creates_cycle(edges: set[tuple[str, str]], path: Path) -> bool:
    """Whether adding `path`'s token edges to the directed token graph `edges` makes it
    cyclic (an economic token cycle, including one pool used in both directions)."""
    new = [(e.token_in, e.token_out) for e in path if (e.token_in, e.token_out) not in edges]
    if not new:
        return False
    graph: dict[str, set[str]] = {}
    for u, v in (*edges, *new):
        graph.setdefault(u, set()).add(v)
    # A cycle must use a new edge u -> v, i.e. v reaches u.
    for u, v in new:
        seen, stack = {v}, [v]
        while stack:
            t = stack.pop()
            if t == u:
                return True
            for w in graph.get(t, ()):
                if w not in seen:
                    seen.add(w)
                    stack.append(w)
    return False


@dataclasses.dataclass
class PoolFlow:
    """One physical pool's tentative aggregate: its (single) used direction, total
    input `amount_in` and the exact output `amount_out = f(amount_in)` on the pool's
    original state. `order` is its first use (plan ordering)."""

    edge: Edge
    amount_in: int
    amount_out: int
    order: int


def merged_plan(case: Case, flows: Iterable[PoolFlow]) -> RoutePlan:
    """The normalized complete plan of aggregate pool flows: one step per physical
    pool with a positive input, tokens in topological order (ties by first use), each
    token's incoming funds split across its outgoing pools by explicit amounts in
    first-use order and the last outgoing pool taking `ALL_REMAINING` of every fund
    still holding a balance. Requires conserved flows: at every intermediate token the
    incoming pool outputs equal the outgoing pool inputs, and the request amount equals
    the input of the pools out of `case.token_in`."""
    used = sorted((f for f in flows if f.amount_in > 0), key=lambda f: f.order)
    if not used:
        raise ValueError("a merged plan needs at least one pool with a positive input")
    out_edges: dict[str, list[PoolFlow]] = {}
    in_edges: dict[str, list[PoolFlow]] = {}
    first: dict[str, int] = {case.token_in: -1}
    for f in used:
        out_edges.setdefault(f.edge.token_in, []).append(f)
        in_edges.setdefault(f.edge.token_out, []).append(f)
        for t in (f.edge.token_in, f.edge.token_out):
            first.setdefault(t, f.order)
    indegree = {t: len(in_edges.get(t, ())) for t in first}
    ready = sorted((t for t, d in indegree.items() if d == 0), key=first.__getitem__)
    order: list[str] = []
    while ready:
        t = ready.pop(0)
        order.append(t)
        for f in out_edges.get(t, ()):
            indegree[f.edge.token_out] -= 1
            if indegree[f.edge.token_out] == 0:
                ready.append(f.edge.token_out)
                ready.sort(key=first.__getitem__)
    if len(order) != len(first):
        raise ValueError("the pool flows form a token cycle")
    if order[0] != case.token_in or case.token_in in in_edges:
        raise ValueError(f"the pool flows must start at {case.token_in} and never re-enter it")

    steps: list[SwapStep] = []
    fund_of: dict[str, str] = {}  # pool id -> its output fund id
    for token in order:
        outs = out_edges.get(token, [])
        if not outs:
            continue
        if token == case.token_in:
            funds = [(REQUEST_FUND_ID, case.amount_in)]
        else:
            funds = [(fund_of[f.edge.pool_id], f.amount_out) for f in in_edges.get(token, [])]
        if sum(f.amount_in for f in outs) != sum(b for _, b in funds):
            raise ValueError(f"flows are not conserved at token {token}")
        balance = [b for _, b in funds]
        for n, f in enumerate(outs):
            refs: list[FundInput] = []
            if n == len(outs) - 1:
                refs = [
                    FundInput(fid, ALL_REMAINING)
                    for (fid, _), b in zip(funds, balance, strict=True)
                    if b > 0
                ]
            else:
                need = f.amount_in
                for i, (fid, _) in enumerate(funds):
                    if need == 0:
                        break
                    take = min(need, balance[i])
                    if take > 0:
                        refs.append(FundInput(fid, take))
                        balance[i] -= take
                        need -= take
            out = f"F{len(steps) + 1}"
            fund_of[f.edge.pool_id] = out
            steps.append(
                SwapStep(
                    pool_id=f.edge.pool_id,
                    token_in=f.edge.token_in,
                    token_out=f.edge.token_out,
                    inputs=tuple(refs),
                    output_fund_id=out,
                )
            )
    return RoutePlan(steps=tuple(steps))


def topology(route_paths: Iterable[Path]) -> str:
    """`single_route`, `disjoint_split` (distinct paths sharing no pool: the
    `path_split` capability) or `shared_pool` (some pool on more than one distinct
    path: an expanded topology)."""
    distinct = list(dict.fromkeys(route_paths))
    if len(distinct) <= 1:
        return "single_route"
    pools = [p.pool_id for path in distinct for p in path]
    return "shared_pool" if len(pools) != len(set(pools)) else "disjoint_split"


def _source_paths(ev: Evaluation) -> list[Path]:
    """The legs of a `path_split`-family plan (one per request-fund consumer, each
    followed along its hops), read from the evaluator's own trace."""
    legs: list[list[Edge]] = []
    for t in ev.trace:
        if t.inputs[0][0] == REQUEST_FUND_ID or not legs:
            legs.append([])
        legs[-1].append(Edge(t.pool_id, t.token_in, t.token_out))
    return [tuple(leg) for leg in legs]


_NO_TOKENS: frozenset[str] = frozenset()


class _BudgetExhausted(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class _Failure:
    """A path prefix whose marginal quote is unusable in one chunk: the counted
    `reason`, the prefix length and, for `incomplete_snapshot`, the example suffix."""

    reason: str
    prefix: int
    note: str | None = None


class _ChunkFailed(Exception):
    def __init__(self, failure: _Failure) -> None:
        super().__init__(failure.reason)
        self.failure = failure


class _ExactReuse:
    """Explicit, default-off exact reuse for one solve (WHI-1507 / research key L05;
    `solve(..., graph_reuse=True)`). Mutable per-solve search state, never shared.

    **Scores.** A path's chunk result (marginal and pool updates, or its first failing
    prefix) is a pure function of the chunk's actual amount (carry included) and the
    committed aggregate `(amount_in, amount_out)` of the pools on the path: quotes are
    on the pools' original states through the solve's `QuoteCache`. It is kept per
    (amount, path) and dropped for every path touching a pool of a committed chunk
    (`pool_paths`); another amount is another key. A reused result makes no quote call:
    the reference would have answered every one of its quotes from the `QuoteCache`
    (same keys), so the quote meter, budget checks and `quotes_executed` are unchanged
    and only `quotes_memoized` (physical cache lookups) is lower.

    **Admission.** Committed token edges only grow, so a path once found to close a
    cycle stays rejected; an admitted path is re-checked whenever an edge was added.
    The check is atomic over the whole proposed path against the transitive closure of
    the committed (acyclic) edges: the union is cyclic iff some later path token reaches
    an earlier one (a path is itself cycle-free), which is exactly `creates_cycle`,
    including cycles formed only by several new edges together. The check never
    mutates anything; only a committed chunk extends the closure."""

    def __init__(self, paths: list[Path]) -> None:
        self.pool_paths: dict[str, list[int]] = {}
        for j, path in enumerate(paths):
            for e in path:
                self.pool_paths.setdefault(e.pool_id, []).append(j)
        self.scores: dict[int, dict[int, tuple[int, list[PoolFlow]] | _Failure]] = {}
        self.entries = 0
        self.reach: dict[str, set[str]] = {}
        self.version = 0
        self.rejected: set[int] = set()
        self.admitted: dict[int, int] = {}
        self.stats = dict.fromkeys(
            (
                "scores_reused",
                "scores_recomputed",
                "score_invalidations",
                "score_entries_peak",
                "cycle_decisions_reused",
                "cycle_checks_executed",
                "closure_edges_added",
            ),
            0,
        )

    def cyclic(self, j: int, path: Path) -> bool:
        if j in self.rejected or self.admitted.get(j) == self.version:
            self.stats["cycle_decisions_reused"] += 1
            return j in self.rejected
        self.stats["cycle_checks_executed"] += 1
        seen = {path[0].token_in}
        for e in path:
            if not self.reach.get(e.token_out, _NO_TOKENS).isdisjoint(seen):
                self.rejected.add(j)
                return True
            seen.add(e.token_out)
        self.admitted[j] = self.version
        return False

    def commit(self, path: Path, new_edges: list[tuple[str, str]]) -> None:
        for pool in dict.fromkeys(e.pool_id for e in path):
            for j in self.pool_paths.get(pool, ()):
                for entries in self.scores.values():
                    if entries.pop(j, None) is not None:
                        self.entries -= 1
                        self.stats["score_invalidations"] += 1
        for u, v in new_edges:
            add = {v} | self.reach.get(v, set())
            self.reach.setdefault(u, set())
            for w, r in self.reach.items():
                if w == u or u in r:
                    r |= add
            self.stats["closure_edges_added"] += 1
        if new_edges:
            self.version += 1


def solve(
    case: Case, context: SolveContext, budget: Budget, *, graph_reuse: bool = False
) -> SolveResult:
    """`graph_reuse=True` explicitly selects the exact score/admission reuse of
    `_ExactReuse` (default off: the reference loop). It returns the same plan,
    evaluation and search counters, except the physical `quotes_memoized`, and adds
    its own physical counters as `search_stats["graph_reuse"]`."""
    prepared = context.prepared
    if not isinstance(prepared, PreparedIncrementalGraph):
        raise TypeError(f"{NAME}.solve needs the PreparedIncrementalGraph returned by prepare()")
    bundle, objective = context.bundle, context.objective
    ps_prepared = prepared.path_split
    max_hops = ps_prepared.single_path.max_hops
    cache = QuoteCache(bundle)
    quiet = dataclasses.replace(context, candidate_sink=None)

    # ---- 1. simpler candidates: path_split (itself keeping single_path/direct_split).
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

    # ---- 2. incremental chunk allocation on tentative aggregate pool state.
    truncated_by: str | None = None
    failures: dict[str, int] = {}
    incomplete: list[str] = []

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
    amounts = chunk_amounts(case.amount_in, prepared.chunks)
    flows: dict[str, PoolFlow] = {}
    token_edges: set[tuple[str, str]] = set()
    chunk_paths: list[Path] = []
    chunk_inputs: list[int] = []  # per committed chunk, its amount (with any carry)
    scored = rejected_cycle = own_truncated = carried = 0
    incremental_status = "not_run"
    incremental: tuple[RoutePlan, Evaluation, int] | None = None
    accounted_gross: int | None = None

    def count(failure: _Failure, path: Path) -> None:
        failures[failure.reason] = failures.get(failure.reason, 0) + 1
        if failure.note is not None:
            incomplete.append(path_label(path) + failure.note)

    def marginal(path: Path, amount: int, memo: dict[Path, Any]) -> tuple[int, list[PoolFlow]]:
        """The chunk's marginal output along `path` and the pools' new aggregates;
        raises `_ChunkFailed` (after counting the reason) if the path is not usable."""
        m, updates = amount, []
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
                r = guarded(bundle.pools[edge.pool_id], edge.token_in, x + m)
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
                    count(failure, path)
                    raise _ChunkFailed(failure)
                update = PoolFlow(edge, x + m, r.amount_out, order)
                m = r.amount_out - out
            memo[key] = (m, update)
            updates.append(update)
        return m, updates

    reuse = _ExactReuse(paths) if graph_reuse else None

    def reused_marginal(
        j: int, path: Path, amount: int, memo: dict[Path, Any]
    ) -> tuple[int, list[PoolFlow]]:
        """`marginal` through `reuse`: a kept result stands in for the recomputation.
        A kept failure is counted exactly where the reference counts it -- by the first
        path of the chunk that reaches the failing prefix -- and marks it in `memo`."""
        assert reuse is not None
        entries = reuse.scores.setdefault(amount, {})
        hit = entries.get(j)
        if hit is None:
            reuse.stats["scores_recomputed"] += 1
            try:
                hit = marginal(path, amount, memo)
            except _ChunkFailed as exc:
                hit = exc.failure
            entries[j] = hit
            reuse.entries += 1
            peak = reuse.stats["score_entries_peak"]
            reuse.stats["score_entries_peak"] = max(peak, reuse.entries)
        else:
            reuse.stats["scores_reused"] += 1
            if isinstance(hit, _Failure) and path[: hit.prefix] not in memo:
                memo[path[: hit.prefix]] = hit
                count(hit, path)
        if isinstance(hit, _Failure):
            raise _ChunkFailed(hit)
        return hit

    try:
        if not paths:
            incremental_status = "no_paths"
            raise _ChunkFailed(_Failure("no_paths", 0))
        last = max(k for k, a in enumerate(amounts) if a > 0)
        carry = 0
        for k, chunk in enumerate(amounts):
            if chunk == 0:
                continue
            amount = carry + chunk
            memo: dict[Path, Any] = {}
            choice: tuple[int, Path, list[PoolFlow]] | None = None
            chunk_scored = 0
            for j, path in enumerate(paths):
                if budget.max_candidates is not None and chunk_scored >= budget.max_candidates:
                    truncated_by = truncated_by or "max_candidates"
                    own_truncated += len(paths) - j
                    break
                if creates_cycle(token_edges, path) if reuse is None else reuse.cyclic(j, path):
                    rejected_cycle += 1
                    continue
                chunk_scored += 1
                try:
                    if reuse is None:
                        m, updates = marginal(path, amount, memo)
                    else:
                        m, updates = reused_marginal(j, path, amount, memo)
                except _ChunkFailed:
                    continue
                if choice is None or m > choice[0]:
                    choice = (m, path, updates)
            scored += chunk_scored
            if k != last and (choice is None or choice[0] == 0):
                carry, carried = amount, carried + 1
                continue
            if choice is None:
                incremental_status = f"chunk_{k + 1}_no_admissible_path"
                raise _ChunkFailed(_Failure(incremental_status, 0))
            carry = 0
            _, path, updates = choice
            if reuse is not None:
                # A kept result may predate other pools' first use: take the order the
                # reference assigns at scoring time, on fresh objects (the kept ones stay).
                n = len(flows)
                updates = [
                    PoolFlow(u.edge, u.amount_in, u.amount_out, flows[p].order if p in flows else n)
                    for u in updates
                    for p in (u.edge.pool_id,)
                ]
                new_edges = [(e.token_in, e.token_out) for e in path]
                reuse.commit(path, [t for t in dict.fromkeys(new_edges) if t not in token_edges])
            for update in updates:
                flows[update.edge.pool_id] = update
            token_edges.update((e.token_in, e.token_out) for e in path)
            chunk_paths.append(path)
            chunk_inputs.append(amount)

        # ---- 3. normalize (one merged step per pool) and re-evaluate the complete plan.
        accounted_gross = sum(
            f.amount_out for f in flows.values() if f.edge.token_out == case.token_out
        )
        plan = merged_plan(case, flows.values())
        ev = evaluate(bundle, case, plan, objective, quote=guarded)
        if ev.status is EvalStatus.OK:
            incremental_status = "ok"
            incremental = (plan, ev, objective.score(ev))
        else:
            incremental_status = "invalid_plan"
            failures["invalid_plan"] = failures.get("invalid_plan", 0) + 1
    except _BudgetExhausted:
        incremental_status = "truncated"
        own_truncated += 1
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
    ps_truncated = ps.search_stats.get("truncated_by")
    stats: dict[str, Any] = {
        "chunks": prepared.chunks,
        "chunks_empty": sum(a == 0 for a in amounts),
        "chunks_carried": carried,
        "chunks_allocated": len(chunk_paths),
        "max_hops": max_hops,
        "max_splits": ps_prepared.direct_split.max_splits,
        "percent_step": ps_prepared.direct_split.percent_step,
        "paths_enumerated": len(paths),
        "paths_scored": scored,
        "paths_rejected_cycle": rejected_cycle,
        "paths_truncated": own_truncated,
        "marginal_failures": dict(sorted(failures.items())),
        "marginal_incomplete": len(incomplete),
        "incomplete_example": incomplete[0] if incomplete else None,
        "incremental_status": incremental_status,
        "incremental_allocation": [
            {"path": path_label(p), "chunks": n, "amount_in": str(a)}
            for p, (n, a) in by_path.items()
        ],
        # Per allocated chunk, in allocation order: its index in `incremental_allocation`.
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
    }
    if reuse is not None:
        stats["graph_reuse"] = dict(reuse.stats)
    common: dict[str, Any] = {
        "case_id": case.case_id,
        "algorithm": NAME,
        "candidates_considered": ps.candidates_considered + scored,
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
    if incomplete or ps.status is SolveStatus.INCOMPLETE_SNAPSHOT:
        example = incomplete[0] if incomplete else ps.error
        return SolveResult(
            status=SolveStatus.INCOMPLETE_SNAPSHOT,
            error=f"no valid route; candidates need uncollected pool state, e.g. {example}",
            **common,
        )
    return SolveResult(status=SolveStatus.NO_ROUTE, error=ps.error, **common)


FACTORY = AlgorithmFactory(
    name=NAME,
    solve=solve,
    prepare=prepare,
    capabilities=CAPABILITIES,
    search_params=SEARCH_PARAMS,
    graph_params=GRAPH_PARAMS,
)

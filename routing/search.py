"""Reusable bounded path traversal over a bundle's pool graph (docs/DESIGN.md §2.6;
WHI-1438).

The graph is purely structural: tokens are nodes and every admitted pool is an
undirected edge between its two tokens (parallel pools are parallel edges). Nothing
here reads a price, a reserve ratio or any other static spot-rate figure -- the
search only *proposes* candidate paths; each candidate is scored by real integer
quotes through `routing.evaluator.evaluate` (the issue's "no static spot-rate
shortest-path approximation may replace real quotes").

`enumerate_paths` yields every **cycle-free** path (no token visited twice, so no pool
is used twice either) from `token_in` to `token_out` with at most `max_hops` pools,
in a fixed, documented order:

1. **hop-major** -- every 1-hop (direct) path first, then every 2-hop path, ...;
   a declared candidate/quote cap therefore always truncates the *longest* paths
   and never drops a direct candidate while a longer one is still being scored;
2. within one hop count, depth-first in the index's adjacency order, which is the
   bundle's pool insertion order (deterministic across runs and processes).

A path never passes *through* `token_out` (it ends the first time it reaches it) and
never returns to `token_in`. The traversal is a generator: callers that stop early
(a budget) pay nothing for the rest, and callers that must *count* the remainder
(declared truncation) can keep iterating without quoting.

`GraphIndex` is immutable and safe to share across solves; it is what
`single_path.prepare` builds once per worker (its cost is charged by the runner as the
algorithm's preparation step).

`QuoteCache` is a per-solve memo for the evaluator's quote seam: candidates sharing a
prefix (`A -[p]-> C -> ...`) replay the same first quote on the same original pool
state with the same amount, and `quote_exact_in` is a pure function of immutable
inputs, so the earlier result is returned instead of quoting again. Only calls on a
pool's *original* bundle state are memoized (identity check), so a later use of an
already-swapped pool inside one plan always quotes fresh. A cache hit never reaches the
metered seam (`pools.quote`), so the worker's quote meter counts exactly the quotes
that were executed (`QuoteCache.misses`).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from pools.quote import quote_exact_in
from pools.result import SwapResult
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from snapshot.models import Case, PoolState, SnapshotBundle


@dataclass(frozen=True)
class Edge:
    """One directed use of a pool: swap `token_in` for `token_out` in `pool_id`."""

    pool_id: str
    token_in: str
    token_out: str


Path = tuple[Edge, ...]


@dataclass(frozen=True)
class GraphIndex:
    """Token adjacency of a bundle: `adjacency[token]` is every directed edge out of
    `token`, in bundle pool-insertion order."""

    adjacency: Mapping[str, tuple[Edge, ...]]
    pool_count: int

    @property
    def token_count(self) -> int:
        return len(self.adjacency)

    def edges_from(self, token: str) -> tuple[Edge, ...]:
        return self.adjacency.get(token, ())


def build_graph_index(bundle: SnapshotBundle) -> GraphIndex:
    adjacency: dict[str, list[Edge]] = {}
    for pool in bundle.pools.values():
        adjacency.setdefault(pool.token0, []).append(Edge(pool.pool_id, pool.token0, pool.token1))
        adjacency.setdefault(pool.token1, []).append(Edge(pool.pool_id, pool.token1, pool.token0))
    return GraphIndex(
        adjacency=MappingProxyType({t: tuple(edges) for t, edges in adjacency.items()}),
        pool_count=len(bundle.pools),
    )


def min_hops(index: GraphIndex, token_in: str, token_out: str) -> int | None:
    """Fewest pools on any path from `token_in` to `token_out`, ignoring every hop
    cap (breadth-first over the structural graph), or `None` if `token_out` is not
    reachable at all. Used to tell "unreachable" apart from "reachable only beyond the
    hop cap" -- both are `no_route`, for different reasons."""
    if token_in == token_out:
        return 0
    seen = {token_in}
    frontier: deque[tuple[str, int]] = deque([(token_in, 0)])
    while frontier:
        token, depth = frontier.popleft()
        for edge in index.edges_from(token):
            if edge.token_out == token_out:
                return depth + 1
            if edge.token_out not in seen:
                seen.add(edge.token_out)
                frontier.append((edge.token_out, depth + 1))
    return None


def _paths_of_length(index: GraphIndex, token_in: str, token_out: str, hops: int) -> Iterator[Path]:
    """Every cycle-free path of exactly `hops` edges, depth-first in adjacency order.
    Iterative (explicit stack) so a large hop bound cannot hit the recursion limit."""
    visited = {token_in}
    path: list[Edge] = []
    stack: list[Iterator[Edge]] = [iter(index.edges_from(token_in))]
    while stack:
        edge = next(stack[-1], None)
        if edge is None:
            stack.pop()
            if path:
                visited.discard(path.pop().token_out)
            continue
        if edge.token_out == token_out:
            if len(path) + 1 == hops:
                yield (*path, edge)
            continue  # never pass through the target
        if edge.token_out in visited or len(path) + 1 >= hops:
            continue
        visited.add(edge.token_out)
        path.append(edge)
        stack.append(iter(index.edges_from(edge.token_out)))


def enumerate_paths(
    index: GraphIndex, token_in: str, token_out: str, max_hops: int
) -> Iterator[Path]:
    """Every cycle-free `token_in -> token_out` path with `1..max_hops` pools,
    hop-major then depth-first (see the module docstring)."""
    if max_hops < 1:
        raise ValueError(f"max_hops must be >= 1, got {max_hops}")
    if token_in == token_out:
        return
    for hops in range(1, max_hops + 1):
        yield from _paths_of_length(index, token_in, token_out, hops)


def path_plan(case: Case, path: Path, *, output_fund_id: str = "OUT") -> RoutePlan:
    """The complete exact-input plan for one path (`routing.plan` semantics): step
    `i` consumes all of the previous step's output (`ALL_REMAINING`; the request fund
    for step 0) and the last step's output fund holds the target token."""
    if not path:
        raise ValueError("a path needs at least one edge")
    if path[0].token_in != case.token_in or path[-1].token_out != case.token_out:
        raise ValueError(
            f"path {path_label(path)} does not connect {case.token_in} -> {case.token_out}"
        )
    steps: list[SwapStep] = []
    source = REQUEST_FUND_ID
    for i, edge in enumerate(path):
        out = output_fund_id if i == len(path) - 1 else f"HOP{i + 1}"
        steps.append(
            SwapStep(
                pool_id=edge.pool_id,
                token_in=edge.token_in,
                token_out=edge.token_out,
                inputs=(FundInput(fund_id=source, amount=ALL_REMAINING),),
                output_fund_id=out,
            )
        )
        source = out
    return RoutePlan(steps=tuple(steps))


def path_label(path: Path) -> str:
    if not path:
        return "<empty>"
    return path[0].token_in + "".join(f" -[{e.pool_id}]-> {e.token_out}" for e in path)


class QuoteCache:
    """Per-solve memo of `quote_exact_in` on original bundle states (see the module
    docstring). Must be created fresh for every solve: it is mutable search state."""

    def __init__(self, bundle: SnapshotBundle) -> None:
        self._pools = bundle.pools
        self._memo: dict[tuple[str, str, int], SwapResult[PoolState]] = {}
        self.hits = 0
        self.misses = 0

    def cached(self, pool_id: str, token_in: str, amount_in: int) -> bool:
        return (pool_id, token_in, amount_in) in self._memo

    def __call__(self, state: PoolState, token_in: str, amount_in: int) -> SwapResult[PoolState]:
        if self._pools.get(state.pool_id) is not state:
            self.misses += 1
            return quote_exact_in(state, token_in, amount_in)
        key = (state.pool_id, token_in, amount_in)
        hit = self._memo.get(key)
        if hit is not None:
            self.hits += 1
            return hit
        self.misses += 1
        result = quote_exact_in(state, token_in, amount_in)
        self._memo[key] = result
        return result

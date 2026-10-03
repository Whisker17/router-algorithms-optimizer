"""Shared pieces of the exact chunk-level bound pruning of `incremental_graph_bounded` and
`metis_history_bounded` (WHI-1600; `docs/references/research-022/pruning-contract.md` R022-Q02/1
§3, §5 rule I1, §6 rules M1/M2 and the gate `G_M2`).

Everything here is a **bound on the aggregate-flow chunk marginal** `q(x + m) - q(x)` of a pool
quoted on its **original frozen state** at the committed aggregate input `x` (§3.2-§3.3: never
a bound recomputed from a swapped state). A hop's bound is `floor(rate * m + slack)` from
`pools.bounds.OutputBound` (the helper owns the rate and slack formulae; this module holds none
of them), valid only for `x + m <= CHUNK_DOMAIN_MAX`. A direction whose rate or slack is
missing, or whose domain check fails, has **no bound** and is never pruned on; it is counted
(`bound_no_bound`), never replaced by `0` or a guess. Nothing here makes a quote or touches the
meter: preparing the bounds is charged to `prepare`, the `U_h` table to the solve
(`bound_table_cost`, a deterministic count of edge relaxations -- never wall time, which would
break literal replay of the recorded `search`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Any

from pools.bounds import CHUNK_DOMAIN_MAX, OutputBound
from routing.search import Edge, GraphIndex

if TYPE_CHECKING:
    from routing.algorithms.incremental_graph import PoolFlow

Bounds = Mapping[tuple[str, str], OutputBound | None]


def hop_bound(bounds: Bounds, edge: Edge, committed: int, m: int) -> int | None:
    """`floor(rate * m + slack)`: the most the marginal output of `m` more input into `edge`'s
    pool can be on top of its committed aggregate input `committed` (output-bounds §5.3);
    `None` (no bound) if the direction has no rate or no slack, or `committed + m` leaves
    the proved domain."""
    bound = bounds.get((edge.pool_id, edge.token_in))
    if bound is None or bound.slack is None or committed + m > CHUNK_DOMAIN_MAX:
        return None
    total = bound.rate * m + bound.slack
    return total.numerator // total.denominator


def committed_input(flows: Mapping[str, PoolFlow], edge: Edge) -> int:
    flow = flows.get(edge.pool_id)
    return flow.amount_in if flow is not None else 0


def chain_bound(
    bounds: Bounds, flows: Mapping[str, PoolFlow], edges: Iterable[Edge], m: int
) -> int | None:
    """Rule I1's chain bound (§3.5): `u_0 = m`, `u_j = floor(rate_j * u_{j-1} + slack_j)` along
    `edges`, each hop gated on `committed_j + u_{j-1} <= 2**127`; `m` is the exact marginal
    entering the first edge. `None` as soon as one hop has no bound."""
    upper = m
    for edge in edges:
        hop = hop_bound(bounds, edge, committed_input(flows, edge), upper)
        if hop is None:
            return None
        upper = hop
    return upper


# ------------------------------------------------------------------ U_h (rule M2)


@dataclass(frozen=True)
class Entry:
    """`U_h(v)` (§6.3): every arrival from a label of amount `a` at `v` with `h` hops left is
    `<= r * a + s`; every amount entering a pool on the way is `<= p * a + t`. `reach` False:
    no walk to the target, so no arrival is possible."""

    r: Fraction
    s: Fraction
    p: Fraction
    t: Fraction
    reach: bool = True


ARRIVED = Entry(Fraction(1), Fraction(0), Fraction(1), Fraction(0))
NO_WALK = Entry(Fraction(0), Fraction(0), Fraction(1), Fraction(0), reach=False)


class UTable:
    """The backward bounded-hop max-product table over the **relaxed** constraints (a token may
    repeat, committed-cycle admission is ignored; never enters `source`, never leaves `target`),
    so every walk the label search can take is a walk of the table. `entry(v, h)` is `None` when
    any walk that can reach the target uses an edge without a (rate and slack) bound: no label
    at `v` with `h` hops left may then be pruned. `cost` counts the edge relaxations spent."""

    def __init__(
        self, index: GraphIndex, source: str, target: str, hops: int, bounds: Bounds
    ) -> None:
        self.target = target
        self.entries: dict[tuple[str, int], Entry | None] = {}
        self.cost = 0
        for h in range(hops):
            for v in index.adjacency:
                if v != target:
                    self.entries[(v, h)] = self._build(index, source, v, h, bounds)

    def _sub(self, w: str, h: int) -> Entry | None:
        return ARRIVED if w == self.target else self.entries.get((w, h), NO_WALK)

    def _build(
        self, index: GraphIndex, source: str, v: str, h: int, bounds: Bounds
    ) -> Entry | None:
        if h == 0:
            return NO_WALK
        best: Entry | None = None
        for e in index.edges_from(v):
            self.cost += 1
            if e.token_out == source:
                continue
            sub = self._sub(e.token_out, h - 1)
            if sub is not None and not sub.reach:
                continue
            bound = bounds.get((e.pool_id, e.token_in))
            if sub is None or bound is None or bound.slack is None:
                return None
            r, s = bound.rate, bound.slack
            cand = Entry(r * sub.r, s * sub.r + sub.s, r * sub.p, s * sub.p + sub.t)
            best = (
                cand
                if best is None
                else Entry(
                    max(best.r, cand.r),
                    max(best.s, cand.s),
                    max(best.p, cand.p),
                    max(best.t, cand.t),
                )
            )
        if best is None:
            return NO_WALK
        return Entry(best.r, best.s, max(Fraction(1), best.p), max(Fraction(0), best.t))

    def entry(self, v: str, h: int) -> Entry | None:
        return ARRIVED if v == self.target else self.entries.get((v, h), NO_WALK)


# ------------------------------------------------------------------ the gate G_M2


def walk_counts(
    index: GraphIndex, source: str, target: str, hops: int, dist: Mapping[str, int]
) -> list[dict[str, int]]:
    """`W[k][v]`: the walks of exactly `k` edges from `source` to `v` the label search could
    create (never into `source`, never out of `target`, inside the distance filter; a token may
    repeat, so this only over-counts). Layer `k` holds at most `sum_v W[k][v]` labels and a
    `(v, visited set)` group at most `W[k][v]`."""
    layers: list[dict[str, int]] = [{source: 1}]
    for k in range(1, hops + 1):
        nxt: dict[str, int] = {}
        for t, n in layers[-1].items():
            if t == target:
                continue
            for e in index.edges_from(t):
                v = e.token_out
                if v == source or (v != target and dist.get(v, hops + 1) > hops - k):
                    continue
                nxt[v] = nxt.get(v, 0) + n
        layers.append(nxt)
    return layers


def m2_gate(
    index: GraphIndex,
    source: str,
    target: str,
    hops: int,
    dist: Mapping[str, int],
    options: Mapping[str, Any],
) -> tuple[str, bool]:
    """`G_M2` (§6.3): `("open" | "closed:frontier" | "closed:dominance", labels_exist)`.

    Open iff no dominance event and no cap can occur in either run: the largest layer fits the
    frontier cap (no R7 refusal) and either `dominance` is off (every label is its own group, so
    R2/R3/R6 never fire) or every `(layer, token)` is reached by at most one walk (every group is
    a singleton). `labels_exist` is False when no non-target label can exist at all, in which
    case M2 has nothing to skip (the caller then builds no table)."""
    layers = walk_counts(index, source, target, hops, dist)
    labels = [{v: n for v, n in layer.items() if v != target} for layer in layers[1:]]
    exist = any(layer for layer in labels)
    frontier = max((sum(layer.values()) for layer in labels), default=0)
    if frontier > options["max_frontier_labels"]:
        return "closed:frontier", exist
    if options["dominance"] != "off" and any(n > 1 for layer in labels for n in layer.values()):
        return "closed:dominance", exist
    return "open", exist


# ------------------------------------------------------------------ the label pruner (M1, M2)


class LabelPruner:
    """Rules M1 and M2 for one `metis_history` solve. `skip` decides, for one relaxation of a
    label of amount `m > 0` along `edge` with `hops_left` hops after it, whether every arrival
    below it is `<= best`, the chunk's best marginal so far (strict replacement: a tie never
    wins, so equality is skippable). M1 is a relaxation into the target (an arrival creates no
    label, so it is population neutral); a relaxation into any other token is M2 and exists
    only when the solve built a `UTable` behind `G_M2`."""

    def __init__(
        self,
        bounds: Bounds,
        flows: Mapping[str, PoolFlow],
        target: str,
        table: UTable | None = None,
    ) -> None:
        self.bounds, self.flows, self.target, self.table = bounds, flows, target, table
        self.pruned_bound = self.bound_evaluations = self.bound_no_bound = 0

    def skip(self, edge: Edge, m: int, hops_left: int, best: int) -> bool:
        arrival = edge.token_out == self.target
        if not arrival and self.table is None:
            return False  # M2 is off: nothing is computed, nothing is counted
        self.bound_evaluations += 1
        upper = self._upper(edge, m, hops_left, arrival)
        if upper is None:
            self.bound_no_bound += 1
            return False
        if upper <= best:
            self.pruned_bound += 1
            return True
        return False

    def _upper(self, edge: Edge, m: int, hops_left: int, arrival: bool) -> int | None:
        first = hop_bound(self.bounds, edge, committed_input(self.flows, edge), m)
        if first is None or arrival:
            return first
        assert self.table is not None
        entry = self.table.entry(edge.token_out, hops_left)
        if entry is None:
            return None
        bound = self.bounds[(edge.pool_id, edge.token_in)]
        assert bound is not None and bound.slack is not None
        entering = bound.rate * m + bound.slack  # the (unfloored) bound on the label's amount
        largest = max((f.amount_in for f in self.flows.values()), default=0)
        if largest + max(Fraction(m), entry.p * entering + entry.t) > CHUNK_DOMAIN_MAX:
            return None  # a deeper hop could leave the proved domain
        total = entry.r * entering + entry.s
        return total.numerator // total.denominator

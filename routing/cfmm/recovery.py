"""Integer plan recovery `cfmm_share_projection/1` of `cfmm_dual` (WHI-1558; `cfmm-dual.md` §6).

Ported from the validated WHI-1557 executable contract model
(`tests/routing/cfmm_contract_model.py` `recover`, sha256
`7fb979394cbcba243058fe38ebdc37b56602735ab3887475c6386851006ac387`; not imported). It turns
the continuous oracle trades of a solve into ONE ordered, fully funded integer `RoutePlan`, or
a named failure. The continuous solution only chooses the support and the split shares; every
amount that reaches the plan is an exact integer decided by exact quotes on the pools'
ORIGINAL states:

1. **Support**: markets with a positive continuous input that is at least `min_split_share`
   of their input token's total continuous outflow.
2. **Relevance**: only markets on a directed `token_in -> token_out` path of the support and
   never an edge out of `token_out` (dead ends, disconnected parts and surplus-only flows drop).
3. **Cycles**: while the token digraph has a cycle (DFS in sorted token order, adjacency in
   admitted order) remove the cycle market with the smallest continuous input value
   `nu_in * amount_in` (ties: the later admitted market); every check is one
   `admission_checks`, every removal one `combinations_rejected_cycle`. After a removal and
   with `cycle_resolve`, the caller's `resolve` re-solves the fixed-direction DAG on the SAME
   numeric budget (`ResolveSkipped` = nothing left; a result without trades =
   `resolve_failed`); support + relevance are then taken from the re-solve.
4. **Share projection**: tokens in topological order (Kahn; ties by first use, then token
   id); a token's exact integer inflow is split over its out-markets in admitted order,
   every leg but the last `floor(I * x_p / sum x)` with exact `Fraction` shares of the float
   inputs, the last the exact remainder; a zero leg is not emitted. Each emitted leg is
   quoted once through the per-solve `QuoteCache` (the worker's metered seam on a miss).
5. **Prune and retry**: the first failing leg (a non-`ok` quote status, or `ok` with a zero
   output: `zero_output`) is removed, relevance is re-applied and step 4 restarts, at most
   `max_recovery_attempts` projections. A miss beyond the remaining quote budget stops with
   `quote_budget`. Missing or partial state is a quote failure (`incomplete_snapshot`), never
   assumed empty liquidity.
6. **Plan and replay**: `incremental_graph.merged_plan` (one merged step per market, the last
   out-leg of a token takes `ALL_REMAINING`) replayed by `routing.evaluator.evaluate` with the
   same cache (memoized, no new quote). The replay must be `ok`, consume the whole request,
   leave no residual fund and equal the accounted gross; anything else is an `inconsistency`
   (an algorithm error, never a recovery outcome or a hidden fallback).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from benchmark.objective import ObjectiveContext
from pools.result import QuoteStatus
from routing.algorithms.incremental_graph import PoolFlow, merged_plan
from routing.cfmm.model import Trade
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import REQUEST_FUND_ID, RoutePlan
from routing.search import Edge, QuoteCache
from snapshot.models import Case, SnapshotBundle

RECOVERY = "cfmm_share_projection/1"
FAILURES = (
    "empty_support",
    "support_exhausted",
    "attempts_exhausted",
    "resolve_failed",
    "numeric_failure",
    "quote_budget",
)
PRUNE_REASONS = (
    "insufficient_output_amount",
    "insufficient_liquidity",
    "incomplete_snapshot",
    "reverted",
    "unsupported",
    "zero_output",
)


@dataclass(frozen=True)
class RecoveryOptions:
    min_split_share: float
    max_recovery_attempts: int
    cycle_resolve: bool
    max_quotes: int | None = None  # the attempt's quote cap (the cache's misses count)


class ResolveSkipped(Exception):  # noqa: N818 -- a declared budget outcome, not an error
    """The shared numeric budget has no evaluation or iteration left for the re-solve."""


# `allowed` (pool id -> its kept input token, admitted order) -> the re-solve's trades, or
# `None` when the re-solve failed or produced no trade (`resolve_failed`).
Resolve = Callable[[Mapping[str, str]], Sequence[Trade] | None]


@dataclass
class Recovery:
    """Outcome of `recover`: `plan`/`evaluation`/`gross` only on success; `failure` a §6
    code; `inconsistency` a detail when the replay disagrees with the accounting (a bug)."""

    plan: RoutePlan | None = None
    evaluation: Evaluation | None = None
    gross: int | None = None
    failure: str | None = None
    inconsistency: dict[str, Any] | None = None
    attempts: int = 0
    admission_checks: int = 0
    initial_support: tuple[str, ...] = ()
    cycle_removed: list[str] = field(default_factory=list)
    resolve: str | None = None  # None (no cycle) | disabled | resolved | skipped | failed
    support: tuple[str, ...] = ()
    pruned: list[tuple[str, str, int]] = field(default_factory=list)  # (pool, reason, attempt)
    flows: tuple[PoolFlow, ...] = ()
    quotes_executed: int = 0  # cache misses during recovery (exact_replay_quotes)


def _support(trades: Sequence[Trade], min_share: float) -> dict[str, Trade]:
    out_total: dict[str, list[float]] = {}
    for t in trades:
        out_total.setdefault(t.token_in, []).append(t.amount_in)
    totals = {k: math.fsum(v) for k, v in out_total.items()}
    return {
        t.pool_id: t
        for t in trades
        if t.amount_in > 0.0 and t.amount_in >= min_share * totals[t.token_in]
    }


def _relevant(edges: Mapping[str, Trade], source: str, target: str) -> dict[str, Trade]:
    fwd: dict[str, set[str]] = {}
    back: dict[str, set[str]] = {}
    for t in edges.values():
        fwd.setdefault(t.token_in, set()).add(t.token_out)
        back.setdefault(t.token_out, set()).add(t.token_in)

    def reach(start: str, adj: Mapping[str, set[str]]) -> set[str]:
        seen, stack = {start}, [start]
        while stack:
            for nxt in adj.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    from_s, to_t = reach(source, fwd), reach(target, back)
    return {
        pid: t
        for pid, t in edges.items()
        if t.token_in in from_s and t.token_out in to_t and t.token_in != target
    }


def _find_cycle(edges: Mapping[str, Trade]) -> list[str] | None:
    """The market ids of one directed token cycle (DFS from tokens in sorted order, edges in
    the mapping's admitted order), or None."""
    adj: dict[str, list[Trade]] = {}
    for t in edges.values():
        adj.setdefault(t.token_in, []).append(t)
    state: dict[str, int] = {}
    for root in sorted(adj):
        if root in state:
            continue
        path: list[Trade] = []
        tokens = [root]
        stack = [iter(adj.get(root, []))]
        state[root] = 1
        while stack:
            nxt = next(stack[-1], None)
            if nxt is None:
                state[tokens.pop()] = 2
                stack.pop()
                if path:
                    path.pop()
                continue
            if state.get(nxt.token_out) == 1:
                start = tokens.index(nxt.token_out)
                return [t.pool_id for t in path[start:]] + [nxt.pool_id]
            if nxt.token_out not in state:
                state[nxt.token_out] = 1
                tokens.append(nxt.token_out)
                path.append(nxt)
                stack.append(iter(adj.get(nxt.token_out, [])))
    return None


def _break_cycles(
    edges: dict[str, Trade], nu: Mapping[str, float], order: Mapping[str, int], rec: Recovery
) -> None:
    while True:
        rec.admission_checks += 1
        cycle = _find_cycle(edges)
        if cycle is None:
            return
        victim = min(
            cycle,
            key=lambda pid: (nu[edges[pid].token_in] * edges[pid].amount_in, -order[pid]),
        )
        rec.cycle_removed.append(victim)
        del edges[victim]


def _cascade(edges: dict[str, Trade], source: str, target: str) -> None:
    keep = _relevant(edges, source, target)
    for pid in list(edges):
        if pid not in keep:
            del edges[pid]


def _topological(edges: Mapping[str, Trade], source: str, order: Mapping[str, int]) -> list[str]:
    first: dict[str, int] = {source: -1}
    for pid in sorted(edges, key=order.__getitem__):
        t = edges[pid]
        first.setdefault(t.token_in, order[pid])
        first.setdefault(t.token_out, order[pid])
    indeg = dict.fromkeys(first, 0)
    for t in edges.values():
        indeg[t.token_out] += 1
    ready = sorted((t for t, d in indeg.items() if d == 0), key=lambda t: (first[t], t))
    out: list[str] = []
    while ready:
        tok = ready.pop(0)
        out.append(tok)
        for t in edges.values():
            if t.token_in == tok:
                indeg[t.token_out] -= 1
                if indeg[t.token_out] == 0:
                    ready.append(t.token_out)
        ready.sort(key=lambda t: (first[t], t))
    return out


def _project(
    bundle: SnapshotBundle,
    case: Case,
    edges: Mapping[str, Trade],
    order: Mapping[str, int],
    cache: QuoteCache,
    max_quotes: int | None,
) -> tuple[list[PoolFlow], tuple[str, str] | None]:
    """One share projection (step 4): the exact integer flows, or the first failing
    (market, reason)."""
    inflow: dict[str, int] = {case.token_in: case.amount_in}
    flows: list[PoolFlow] = []
    for token in _topological(edges, case.token_in, order):
        outs = sorted(
            (t for t in edges.values() if t.token_in == token), key=lambda t: order[t.pool_id]
        )
        amount = inflow.get(token, 0)
        if not outs or amount == 0:
            continue
        weights = [Fraction(t.amount_in) for t in outs]
        total = sum(weights, Fraction(0))
        legs = [math.floor(amount * w / total) for w in weights[:-1]]
        legs.append(amount - sum(legs))
        for t, leg in zip(outs, legs, strict=True):
            if leg == 0:
                continue
            if (
                max_quotes is not None
                and not cache.cached(t.pool_id, token, leg)
                and cache.misses >= max_quotes
            ):
                return flows, (t.pool_id, "quote_budget")
            result = cache(bundle.pools[t.pool_id], token, leg)
            if result.status is not QuoteStatus.OK:
                return flows, (t.pool_id, result.status.value)
            if result.amount_out == 0:
                return flows, (t.pool_id, "zero_output")
            flows.append(
                PoolFlow(
                    Edge(t.pool_id, token, t.token_out), leg, result.amount_out, order[t.pool_id]
                )
            )
            inflow[t.token_out] = inflow.get(t.token_out, 0) + result.amount_out
    return flows, None


def _replay(
    bundle: SnapshotBundle,
    case: Case,
    flows: Sequence[PoolFlow],
    objective: ObjectiveContext,
    cache: QuoteCache,
    rec: Recovery,
) -> None:
    """Step 6: plan + independent in-solve replay; sets the plan or the inconsistency."""
    gross = sum(f.amount_out for f in flows if f.edge.token_out == case.token_out)
    try:
        plan = merged_plan(case, flows)
    except ValueError as exc:
        rec.inconsistency = {"stage": "merged_plan", "error": str(exc), "accounted": str(gross)}
        return
    ev = evaluate(bundle, case, plan, objective, quote=cache)
    request = next((f for f in ev.funds if f.fund_id == REQUEST_FUND_ID), None)
    consumed = request is not None and request.consumed == case.amount_in
    if ev.status is not EvalStatus.OK or ev.gross_output != gross or ev.residuals or not consumed:
        rec.inconsistency = {
            "stage": "replay",
            "evaluation_status": ev.status.value,
            "evaluated_gross": str(ev.gross_output),
            "accounted": str(gross),
            "error": ev.error,
        }
        return
    rec.plan, rec.evaluation, rec.gross, rec.flows = plan, ev, gross, tuple(flows)


def recover(
    bundle: SnapshotBundle,
    case: Case,
    markets: Sequence[str],
    trades: Sequence[Trade],
    nu: Mapping[str, float],
    options: RecoveryOptions,
    cache: QuoteCache,
    objective: ObjectiveContext,
    resolve: Resolve | None = None,
) -> Recovery:
    """§6 steps 1-6 on the trades/prices `nu` (every variable; `nu_out` = 1 implied) of the
    initial full-network solve over `markets` (admitted order)."""
    rec = Recovery()
    order = {pid: i for i, pid in enumerate(markets)}
    nu_full = {**nu, case.token_out: 1.0}
    if any(not (math.isfinite(t.amount_in) and math.isfinite(t.amount_out)) for t in trades):
        rec.failure = "numeric_failure"
        return rec
    edges = _relevant(_support(trades, options.min_split_share), case.token_in, case.token_out)
    rec.initial_support = tuple(sorted(edges, key=order.__getitem__))
    _break_cycles(edges, nu_full, order, rec)
    resolved = False
    if rec.cycle_removed:
        rec.resolve = "disabled"
        if options.cycle_resolve and resolve is not None and edges:
            allowed = {pid: edges[pid].token_in for pid in sorted(edges, key=order.__getitem__)}
            try:
                again = resolve(allowed)
            except ResolveSkipped:
                rec.resolve = "skipped"
            else:
                if not again:
                    rec.resolve = "failed"
                    rec.failure = "resolve_failed"
                    return rec
                rec.resolve, resolved = "resolved", True
                kept = [t for t in again if t.pool_id in allowed]
                edges = _relevant(
                    _support(kept, options.min_split_share), case.token_in, case.token_out
                )
    if not resolved:
        _cascade(edges, case.token_in, case.token_out)
    rec.support = tuple(sorted(edges, key=order.__getitem__))
    if not edges:
        rec.failure = "empty_support"
        return rec
    while rec.plan is None and rec.inconsistency is None:
        if rec.attempts >= options.max_recovery_attempts:
            rec.failure = "attempts_exhausted"
            break
        rec.attempts += 1
        flows, failed = _project(bundle, case, edges, order, cache, options.max_quotes)
        if failed is None:
            _replay(bundle, case, flows, objective, cache, rec)
            break
        pid, reason = failed
        if reason == "quote_budget":
            rec.failure = "quote_budget"
            break
        rec.pruned.append((pid, reason, rec.attempts))
        del edges[pid]
        _cascade(edges, case.token_in, case.token_out)
        if not edges:
            rec.failure = "support_exhausted"
            break
    rec.quotes_executed = cache.misses
    return rec

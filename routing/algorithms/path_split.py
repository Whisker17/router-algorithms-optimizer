"""`path_split` (docs/DESIGN.md §2.6): discrete allocation of the **full** order input
across candidate multi-hop paths, with physical-pool conflicts excluded. Multi-hop
disjoint split: two selected paths never reference the same physical pool (pool-id
identity -- Agni, FusionX and Uniswap pools on the same tokens and fee are distinct
pools), so the legs never share pool state. Shared-pool plans are `incremental_graph`'s
job (WHI-1441), not this one's.

**Parameters.** Profile `search.max_hops`, `search.max_splits` and
`search.percent_step` (validated by `single_path.prepare` / `direct_split.prepare`;
no built-in defaults, §2.12). The grid is `direct_split`'s: `N = 100 / percent_step`
units, at most `max_splits` nonzero legs, integer amounts and the remainder by
`direct_split.leg_amounts` (floored shares, the last leg in step order takes
`ALL_REMAINING` of the request fund), so the input is always fully allocated.

**Search** (one per-solve `routing.search.QuoteCache` shared by every stage, so a
quote is executed at most once per solve and the final replays are memo hits):

1. **Simpler routes are candidates.** `single_path` runs first (every cycle-free
   path within `max_hops` quoted at the full input and scored as a complete plan),
   then `direct_split` (every direct pool on the grid). Both results are finalists,
   so the best single path and the best direct split can never be lost.
2. **Discovery at multiple sizes.** Every enumerated path is also sampled at the
   smallest positive grid size `u0` (the full-size sample is memoized from stage 1).
   A path that is poor or failing at the full input keeps its small-size sample:
   ranking never uses the full-input quote alone.
3. **Per-size candidate pruning** (conflict-aware, sound under the assumption
   below). Let `B = (max_splits - 1) * max_hops`, the most pools the other legs of an
   allocation can hold. At grid size `u`, if more than `B` pairwise pool-disjoint
   paths are each strictly better than path `P`, then in any allocation using `P` at
   `u`, one of them avoids every other leg's pools and replaces `P` for a strictly
   better allocation with the same leg count -- so `P` at `u` is never needed. The
   pruning takes the pool-disjoint family greedily in value order (a valid lower
   bound on how many pools must be excluded). Paths are sampled at `u` lazily in
   descending order of an **upper bound** `min(out(N), ceil((out(u0) + 1) * a_u / a_u0))`
   (checked every `B + 1` samples); sampling stops once the family beats the next
   bound. A path that fails at `u0` for a reason that also holds for every larger input
   (insufficient liquidity, uncollected state, unsupported) is not sampled larger. The
   bound and that rule assume a path's output is nondecreasing and its average rate
   nonincreasing in the input, and that a larger swap only traverses more pool state
   (true for the admitted CPMM/CL/LB curves up to integer rounding; the `+ 1` absorbs
   one unit of output rounding). Every skipped or pruned (path, size) pair is counted
   in `search_stats` (`samples_dead_skipped`, `samples_bound_pruned`,
   `entries_rank_pruned`).
4. **Conflict-aware selection: exact branch-and-bound** over the kept (path, size)
   table (`_branch_and_bound`). Legs are chosen with nonincreasing grid size, and
   within one size in value-descending order, so no leg set is visited twice; a path
   is only added if its pool set is disjoint from every chosen leg (`paths_conflict`);
   each size's list is scanned best-first and cut as soon as gross plus a knapsack
   bound (the best kept value per size, pool conflicts ignored) cannot beat the
   incumbent of any reachable leg count. It returns the gross-best allocation of every
   leg count `m = 2..max_splits` over the kept table, valued at the floored grid
   amounts; the final leg's integer remainder (< `m` raw units) is added by the
   re-evaluation, so a finalist's evaluated gross is never below its search value.
   Because each pruned entry is strictly beaten by a feasible swap, the kept table
   loses no gross-best allocation of the grid (floored values) under the assumption
   above.
5. **Complete-plan re-evaluation.** Every finalist is built as a fund-referenced
   `RoutePlan` (`split_path_plan`) and replayed by `routing.evaluator.evaluate`; the
   best `ObjectiveContext.score` wins, ties keeping the simpler route (the single path,
   then the direct split, then fewer legs). This is where a non-additive complete-plan
   cost is rechecked (§2.9).

The result is the best evaluated allocation of the declared finite grid over the
bounded path set under the stated pruning -- not claimed optimal (§2.6).

**Budgets.** `Budget.max_quotes` is the worker's hard meter: every stage checks the
shared cache's executed-quote count before each new quote and stops *before* the meter
(declared truncation, `truncated_by`). `Budget.max_candidates` is applied by each
stage to its own evaluated candidates (the sub-solvers' own rules; this stage's
finalists). The time limit is the runner's; every new best plan is published through
`SolveContext.report_candidate`.

**Statuses** (never conflated): `ok`; `timeout` (a declared budget truncated some
stage and no valid route was found -- never evidence of `no_route`);
`incomplete_snapshot` (nothing valid, and some candidate needed uncollected state);
`no_route` (the bounded search was complete and nothing was valid; `single_path`'s
reason -- unreachable, hop cap, or every path failed -- is reported).
"""

from __future__ import annotations

import bisect
import dataclasses
from typing import Any, Literal

from pools.result import QuoteStatus, SwapResult
from routing.algorithms import direct_split, single_path
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

NAME = "path_split"

CAPABILITIES = Capabilities(multi_hop=True, split=True)
SEARCH_PARAMS = ("max_hops", "max_splits", "percent_step")


class PathSplitConfigError(ValueError):
    """`prepare` received an invalid or missing `search.*` value."""


class PreparedPathSplit:
    """Immutable per-worker preparation: the two sub-solvers' prepared objects (the
    graph index and hop bound; the grid settings)."""

    __slots__ = ("direct_split", "single_path")

    def __init__(
        self,
        single: single_path.PreparedSinglePath,
        split: direct_split.PreparedDirectSplit,
    ) -> None:
        self.single_path = single
        self.direct_split = split


def prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> PreparedPathSplit:
    try:
        single = single_path.prepare(bundle, config)
        split = direct_split.prepare(bundle, config)
    except (single_path.SinglePathConfigError, direct_split.DirectSplitConfigError) as exc:
        raise PathSplitConfigError(f"{NAME}: {exc}") from exc
    return PreparedPathSplit(single, split)


def paths_conflict(a: Path, b: Path) -> bool:
    """Whether two paths reference a common physical pool (pool-id identity)."""
    return not {e.pool_id for e in a}.isdisjoint(e.pool_id for e in b)


def split_path_plan(case: Case, paths: tuple[Path, ...], units: tuple[int, ...]) -> RoutePlan:
    """The fund-referenced plan of a multi-path allocation whose legs are in step
    order: leg `n`'s first step draws `direct_split.leg_amounts`' explicit amount from
    the request fund (the final leg draws `ALL_REMAINING`, its share plus the integer
    remainder), every later hop consumes all of the previous hop's output, and each
    leg ends in its own terminal target-token fund `OUT{n}`. Refuses pool-sharing
    legs: this algorithm's plans are pool-disjoint by construction."""
    if len(paths) != len(units) or not paths:
        raise ValueError("an allocation needs one unit count per path and at least one leg")
    for i, a in enumerate(paths):
        for b in paths[i + 1 :]:
            if paths_conflict(a, b):
                raise ValueError(f"paths {path_label(a)} and {path_label(b)} share a physical pool")
    legs = tuple(enumerate(units))
    amounts = direct_split.leg_amounts(case.amount_in, legs, sum(units))
    steps: list[SwapStep] = []
    for n, path in enumerate(paths):
        if path[0].token_in != case.token_in or path[-1].token_out != case.token_out:
            raise ValueError(
                f"path {path_label(path)} does not connect {case.token_in} -> {case.token_out}"
            )
        first: int | Literal["ALL_REMAINING"] = ALL_REMAINING if n == len(paths) - 1 else amounts[n]
        source = REQUEST_FUND_ID
        for i, edge in enumerate(path):
            out = f"OUT{n + 1}" if i == len(path) - 1 else f"L{n + 1}H{i + 1}"
            amount = first if i == 0 else ALL_REMAINING
            steps.append(
                SwapStep(
                    pool_id=edge.pool_id,
                    token_in=edge.token_in,
                    token_out=edge.token_out,
                    inputs=(FundInput(fund_id=source, amount=amount),),
                    output_fund_id=out,
                )
            )
            source = out
    return RoutePlan(steps=tuple(steps))


def _branch_and_bound(
    kept: list[Entry],
    pools: list[frozenset[str]],
    n_units: int,
    max_splits: int,
    best: dict[int, tuple[int, tuple[tuple[int, int], ...]]],
    counters: dict[str, int],
) -> None:
    """Exact search over pool-disjoint allocations of the kept table: fills `best[m]`
    with the gross-best `m`-leg allocation (floored grid values) for every reachable
    `m`. Legs are chosen with nonincreasing grid size, and within one size in the
    size's value-descending list order, so each leg set is visited once. Each size's
    list is scanned best-first and the scan stops as soon as gross plus a knapsack
    bound (the best kept value per size, sizes capped by the current one, pool
    conflicts ignored) cannot beat any reachable leg count's incumbent. A path whose
    pools intersect a chosen leg's is skipped (`counters["conflicts"]`)."""
    lists: dict[int, list[tuple[int, int]]] = {}
    for j, u, v in kept:
        lists.setdefault(u, []).append((v, j))
    for lst in lists.values():
        lst.sort(key=lambda t: (-t[0], t[1]))
    # ub[c][k][r]: best sum of exactly k per-size maxima, sizes <= c, summing to r.
    ub: list[list[list[int | None]]] = [
        [[None] * (n_units + 1) for _ in range(max_splits + 1)] for _ in range(n_units + 1)
    ]
    for c in range(n_units + 1):
        ub[c][0][0] = 0
        for k in range(1, max_splits + 1):
            for r in range(1, n_units + 1):
                vals = [
                    lists[u][0][0] + prev
                    for u in range(1, min(c, r) + 1)
                    if u in lists and (prev := ub[c][k - 1][r - u]) is not None
                ]
                ub[c][k][r] = max(vals) if vals else None

    def promising(gross: int, k: int, rest: int, cap: int) -> bool:
        if rest == 0:
            return k not in best or gross > best[k][0]
        for m in range(k + 1, max_splits + 1):
            bound = ub[cap][m - k][rest]
            if bound is not None and (m not in best or gross + bound > best[m][0]):
                return True
        return False

    def dfs(
        used: int,
        cap: int,
        min_idx: int,
        taken: frozenset[str],
        gross: int,
        legs: tuple[tuple[int, int], ...],
    ) -> None:
        counters["nodes"] += 1
        r = n_units - used
        if r == 0:
            if len(legs) not in best or gross > best[len(legs)][0]:
                best[len(legs)] = (gross, legs)
            return
        legs_left = max_splits - len(legs)
        for u in range(min(cap, r), 0, -1):
            if u * legs_left < r:
                break  # nonincreasing sizes can no longer cover the rest
            if u not in lists or (legs_left == 1 and u != r):
                continue
            lst = lists[u]
            for idx in range(min_idx if u == cap else 0, len(lst)):
                v, j = lst[idx]
                if not promising(gross + v, len(legs) + 1, r - u, u):
                    break  # the list is value-descending: nothing later can do better
                if not taken.isdisjoint(pools[j]):
                    counters["conflicts"] += 1
                    continue
                dfs(used + u, u, idx + 1, taken | pools[j], gross + v, (*legs, (j, u)))

    dfs(0, n_units, 0, frozenset(), 0, ())


def _allocation(evaluation: Evaluation) -> list[dict[str, Any]]:
    """The legs of an evaluated plan -- one per request-fund consumer, each followed
    along its hops -- as [{path, amount_in}], read from the evaluator's own trace."""
    legs: list[dict[str, Any]] = []
    edges: list[Edge] = []
    for t in evaluation.trace:
        if t.inputs[0][0] == REQUEST_FUND_ID:
            edges = []
            legs.append({"path": "", "amount_in": str(t.amount_in)})
        edges.append(Edge(t.pool_id, t.token_in, t.token_out))
        legs[-1]["path"] = path_label(tuple(edges))
    return legs


# Failures that also hold for every larger input (the swap only traverses more state):
# a path failing so at the smallest grid size is not sampled at larger sizes.
_FAILS_ABOVE = frozenset(
    {
        QuoteStatus.INSUFFICIENT_LIQUIDITY,
        QuoteStatus.INCOMPLETE_SNAPSHOT,
        QuoteStatus.UNSUPPORTED,
        QuoteStatus.UNSUPPORTED_TOKEN,
    }
)


class _BudgetExhausted(Exception):
    pass


# One kept table entry: (path index, grid units, gross output at the floored amount).
Entry = tuple[int, int, int]


def _family_counts(
    ranked: list[tuple[int, int]], pools: list[frozenset[str]]
) -> tuple[list[int], list[int]]:
    """Greedy pool-disjoint family over `ranked` = sorted [(-value, path)] (value
    descending): returns the negated values (ascending, for `bisect`) and, per
    position, how many family members lie at or before it."""
    union: set[str] = set()
    counts: list[int] = []
    size = 0
    for _, j in ranked:
        if union.isdisjoint(pools[j]):
            union |= pools[j]
            size += 1
        counts.append(size)
    return [negv for negv, _ in ranked], counts


def _family_exceeds(
    ranked: list[tuple[int, int]], pools: list[frozenset[str]], threshold: int, bound: int
) -> bool:
    """Whether more than `bound` greedy pool-disjoint paths are worth > `threshold`."""
    union: set[str] = set()
    size = 0
    for negv, j in ranked:
        if -negv <= threshold:
            return False
        if union.isdisjoint(pools[j]):
            union |= pools[j]
            size += 1
            if size > bound:
                return True
    return False


def _members_above(neg: list[int], counts: list[int], threshold: int) -> int:
    """Family members with value strictly greater than `threshold`."""
    k = bisect.bisect_left(neg, -threshold)  # entries with -value < -threshold
    return counts[k - 1] if k else 0


def solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    prepared = context.prepared
    if not isinstance(prepared, PreparedPathSplit):
        raise TypeError(f"{NAME}.solve needs the PreparedPathSplit returned by prepare()")
    bundle, objective = context.bundle, context.objective
    amount_in = case.amount_in
    max_hops = prepared.single_path.max_hops
    max_splits = prepared.direct_split.max_splits
    step_pct = prepared.direct_split.percent_step
    n_units = 100 // step_pct
    cache = QuoteCache(bundle)
    quiet = dataclasses.replace(context, candidate_sink=None)

    # ---- 1. simpler routes: single_path, then direct_split, on the shared cache.
    sp = single_path.solve(
        case, dataclasses.replace(quiet, prepared=prepared.single_path), budget, cache=cache
    )
    ds = direct_split.solve(
        case, dataclasses.replace(quiet, prepared=prepared.direct_split), budget, cache=cache
    )
    finalists: list[tuple[str, RoutePlan, Evaluation, int, list[dict[str, Any]]]] = []
    best_idx: int | None = None

    def adopt(source: str, plan: RoutePlan, ev: Evaluation, score: int, alloc: Any) -> None:
        nonlocal best_idx
        finalists.append((source, plan, ev, score, alloc))
        if best_idx is None or score > finalists[best_idx][3]:
            best_idx = len(finalists) - 1
            context.report_candidate(plan)

    for source, sub in (("single_path", sp), ("direct_split", ds)):
        if sub.status is SolveStatus.OK and sub.plan and sub.evaluation and sub.score is not None:
            adopt(source, sub.plan, sub.evaluation, sub.score, _allocation(sub.evaluation))

    # ---- quote seam for this algorithm's own samples and finalists.
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
        enumerate_paths(prepared.single_path.index, case.token_in, case.token_out, max_hops)
    )
    pools = [frozenset(e.pool_id for e in p) for p in paths]
    samples: dict[tuple[int, int], int | None] = {}  # (path, amount) -> output
    fail_status: dict[tuple[int, int], QuoteStatus] = {}

    def path_out(j: int, amount: int) -> int | None:
        """Chain-quote path `j` at `amount` (evaluator semantics); `None` = failed."""
        key = (j, amount)
        if key in samples:
            return samples[key]
        out: int | None = amount
        for edge in paths[j]:
            assert out is not None
            if out == 0:
                break  # zero-input steps: zero output, no pool call
            r = guarded(bundle.pools[edge.pool_id], edge.token_in, out)
            if r.status is not QuoteStatus.OK or r.amount_in_consumed != out:
                fail_status[key] = r.status
                failures[r.status.value] = failures.get(r.status.value, 0) + 1
                if r.status is QuoteStatus.INCOMPLETE_SNAPSHOT:
                    incomplete.append(f"{path_label(paths[j])} @ {amount}: {r.detail}")
                out = None
                break
            out = r.amount_out
        samples[key] = out
        return out

    sizes = [u for u in range(1, n_units + 1) if amount_in * u // n_units > 0]
    split_possible = max_splits > 1 and len(sizes) > 1 and len(paths) > 1
    bound = (max_splits - 1) * max_hops
    kept: list[Entry] = []
    bound_pruned = rank_pruned = dead_skipped = own_truncated = 0
    bnb = {"nodes": 0, "conflicts": 0}
    bb_best: dict[int, tuple[int, tuple[tuple[int, int], ...]]] = {}

    try:
        if split_possible:
            # ---- 2. discovery: every path at the full input (memoized) and at u0.
            u0 = sizes[0]
            a0 = amount_in * u0 // n_units
            for j in range(len(paths)):
                path_out(j, amount_in)
            for j in range(len(paths)):
                path_out(j, a0)

            def upper(j: int, a: int) -> float | int:
                full, small = samples[(j, amount_in)], samples[(j, a0)]
                cap: float | int = float("inf") if full is None else full
                if small is not None:
                    cap = min(cap, ((small + 1) * a + a0 - 1) // a0)
                return cap

            # ---- 3. per-size lazy sampling and conflict-aware rank pruning.
            dead = [fail_status.get((j, a0)) in _FAILS_ABOVE for j in range(len(paths))]
            for u in sizes:
                a = amount_in * u // n_units
                ranked: list[tuple[int, int]] = []  # (-value, path): value descending
                if u in (u0, n_units):  # sampled for every path in discovery
                    ranked = sorted(
                        (-v, j) for j in range(len(paths)) if (v := samples[(j, a)]) is not None
                    )
                    todo: list[int] = []
                else:
                    live = [j for j in range(len(paths)) if not dead[j]]
                    dead_skipped += len(paths) - len(live)
                    todo = sorted(live, key=lambda j: (-upper(j, a), j))
                for pos, j in enumerate(todo):
                    if pos and pos % (bound + 1) == 0:
                        ub = upper(j, a)
                        if ub != float("inf") and _family_exceeds(ranked, pools, int(ub), bound):
                            bound_pruned += len(todo) - pos
                            break
                    v = path_out(j, a)
                    if v is not None:
                        bisect.insort(ranked, (-v, j))
                neg, counts = _family_counts(ranked, pools)
                for negv, j in ranked:
                    if _members_above(neg, counts, -negv) <= bound:
                        kept.append((j, u, -negv))
                    else:
                        rank_pruned += 1

            # ---- 4. exact branch-and-bound over the kept table.
            _branch_and_bound(kept, pools, n_units, max_splits, bb_best, bnb)

            # ---- 5. re-evaluate the multi-leg finalists as complete plans.
            evaluated = 0
            for m in sorted(bb_best):
                if m < 2:
                    continue
                if budget.max_candidates is not None and evaluated >= budget.max_candidates:
                    truncated_by = truncated_by or "max_candidates"
                    own_truncated += 1
                    continue
                legs = tuple(sorted(bb_best[m][1]))  # step order = path enumeration order
                plan = split_path_plan(
                    case, tuple(paths[j] for j, _ in legs), tuple(u for _, u in legs)
                )
                ev = evaluate(bundle, case, plan, objective, quote=guarded)
                evaluated += 1
                if ev.status is not EvalStatus.OK:
                    continue
                adopt("path_split", plan, ev, objective.score(ev), _allocation(ev))
    except _BudgetExhausted:
        own_truncated += 1

    sub_truncated = [
        name
        for name, sub in (("single_path", sp), ("direct_split", ds))
        if sub.search_stats.get("truncated_by") is not None
    ]
    stats: dict[str, Any] = {
        "max_hops": max_hops,
        "max_splits": max_splits,
        "percent_step": step_pct,
        "grid_units": n_units,
        "paths_enumerated": len(paths),
        "single_path_status": sp.status.value,
        "single_path_score": None if sp.score is None else str(sp.score),
        "direct_split_status": ds.status.value,
        "direct_split_score": None if ds.score is None else str(ds.score),
        "conflict_bound_pools": bound,
        "samples_quoted": len(samples),
        "samples_failed": dict(sorted(failures.items())),
        "samples_incomplete": len(incomplete),
        "incomplete_example": incomplete[0] if incomplete else None,
        "samples_bound_pruned": bound_pruned,
        "samples_dead_skipped": dead_skipped,
        "entries_kept": len(kept),
        "entries_rank_pruned": rank_pruned,
        "bnb_nodes": bnb["nodes"],
        "bnb_conflicts_excluded": bnb["conflicts"],
        "bnb_best_gross": {str(m): str(g) for m, (g, _) in sorted(bb_best.items())},
        "finalists": [
            {"source": src, "splits": len(alloc), "score": str(score)}
            for src, _, _, score, alloc in finalists
        ],
        "truncated_by": truncated_by or (sub_truncated[0] if sub_truncated else None),
        "truncated_stages": sub_truncated + (["path_split"] if truncated_by else []),
        "quotes_executed": cache.misses,
        "quotes_memoized": cache.hits,
        "chosen_source": None,
        "best_splits": None,
        "best_allocation": None,
    }
    truncated = sp.candidates_truncated + ds.candidates_truncated + own_truncated
    common: dict[str, Any] = {
        "case_id": case.case_id,
        "algorithm": NAME,
        "candidates_considered": sp.candidates_considered + ds.candidates_considered + len(kept),
        "candidates_truncated": truncated,
        "search_stats": stats,
    }

    if best_idx is not None:
        source, plan, ev, score, alloc = finalists[best_idx]
        stats["chosen_source"] = source
        stats["best_splits"] = len(alloc)
        stats["best_allocation"] = alloc
        return SolveResult(status=SolveStatus.OK, plan=plan, evaluation=ev, score=score, **common)
    if truncated_by is not None or sub_truncated:
        return SolveResult(
            status=SolveStatus.TIMEOUT,
            error=(
                f"declared {stats['truncated_by']} budget truncated the search with no valid "
                "route -- not evidence of no_route"
            ),
            **common,
        )
    if incomplete or SolveStatus.INCOMPLETE_SNAPSHOT in (sp.status, ds.status):
        example = incomplete[0] if incomplete else (sp.error or ds.error)
        return SolveResult(
            status=SolveStatus.INCOMPLETE_SNAPSHOT,
            error=f"no valid route; candidates need uncollected pool state, e.g. {example}",
            **common,
        )
    return SolveResult(status=SolveStatus.NO_ROUTE, error=sp.error, **common)


FACTORY = AlgorithmFactory(
    name=NAME,
    solve=solve,
    prepare=prepare,
    capabilities=CAPABILITIES,
    search_params=SEARCH_PARAMS,
)

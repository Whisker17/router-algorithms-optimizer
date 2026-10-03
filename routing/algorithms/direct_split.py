"""`direct_split` (docs/DESIGN.md §2.6): discrete allocation of the **full** order
input across the admitted pools that directly connect the case's two tokens.
Direct split: no multi-hop, each physical pool used at most once, so the legs never
share pool state (shared-pool conflicts are out of scope, WHI-1439).

**Grid.** The profile declares `search.percent_step` (a positive divisor of 100) and
`search.max_splits` (>= 1); there are no built-in defaults (§2.12). The input is cut
into `N = 100 / percent_step` units. An allocation gives pool `i` (admitted order)
`u_i >= 0` units with `sum(u_i) = N` and at most `max_splits` nonzero legs.

**Integer amounts and the remainder.** Legs are ordered by admitted pool order. Every
leg except the last gets the explicit amount `floor(amount_in * u_i / N)`; the last
leg is the explicit final allocation and takes `ALL_REMAINING` of the request fund,
i.e. `amount_in - sum(earlier legs)`, which is its own grid share plus the integer
remainder (at most `legs - 1` raw units). The whole input is therefore allocated --
no overspend, no residual (docs/DESIGN.md §2.5) -- for every amount, including tiny
and nondivisible ones. A non-final leg whose floor amount is 0 is never emitted: the
allocation it would describe has exactly the same amounts as the one with that leg's
units moved to the final leg, which the search already covers.

**Search.** Because the legs use distinct pools with no shared state, a plan's gross
output is exactly the sum of its legs' independent quotes. The solver therefore

1. quotes every direct pool at the **full input** first (the single-pool candidates:
   the best valid simpler route is always sampled before anything else, §2.6);
2. samples every pool at every grid size `floor(amount_in * u / N)`, `u = N-1 .. 1`
   ("a pool bad at 100% can be useful for a small split", §2.6 -- a pool that failed
   at full size is still sampled smaller);
3. runs an exact dynamic program over the sampled table (state: legs used, units
   used, accumulated `amount_in * u mod N` residue, which fixes the final leg's
   remainder exactly) to find, for every split count `m = 1..max_splits`, the
   gross-best allocation on the grid; a final leg's non-grid amount is quoted on
   demand;
4. re-evaluates each of those finalists as a complete fund-referenced `RoutePlan`
   through `routing.evaluator.evaluate` and keeps the best by
   `ObjectiveContext.score` (fewer splits win ties). Step 4 is where a non-additive
   complete-plan cost is rechecked (§2.9): an objective that charges per pool call
   can prefer a single pool over a gross-better split.

Every quote goes through one per-solve `routing.search.QuoteCache` (the metered seam
on a miss, a memo hit otherwise), so step 4 costs no new quotes. The result is the
best allocation **of the declared finite grid** under an additive-gross search with
per-split-count finalists -- not claimed optimal (§2.6).

**Budgets.** `Budget.max_quotes` is the worker's hard meter: the solver counts its own
executed quotes and stops sampling *before* it (a skipped sample is declared
truncation). `Budget.max_candidates` caps the number of complete finalist plans
evaluated in step 4, in split-count order, so the single-pool finalist is always
evaluated first. `search_stats["truncated_by"]` names the limit that cut the search.
The time limit is the runner's; every new best plan is published through
`SolveContext.report_candidate` -- the best single pool right after step 1.

**Statuses** (as `single_path`, never conflated): `ok` (best evaluated allocation);
`no_route` (no admitted direct pool, or every sample/finalist failed with the search
complete); `timeout` (a declared budget truncated the search before any valid route
-- never evidence of `no_route`); `incomplete_snapshot` (nothing valid and at least
one sample needed uncollected state). A sample needing uncollected state is excluded
and disclosed (`samples_incomplete`, `incomplete_example`) like `single_path`'s
detours: a smaller split of the same pool may be fully covered. This deliberately
differs from `direct`, which makes the whole case `incomplete_snapshot` when any pool
is incomplete at full size; here the best single pool is then the best *complete*
one, and the disclosure says so.
"""

from __future__ import annotations

from typing import Any

from pools.result import QuoteStatus
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    Capabilities,
    SolveContext,
    SolveResult,
    SolveStatus,
    refuse_options,
)
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from routing.search import QuoteCache
from snapshot.models import Case, SnapshotBundle

NAME = "direct_split"

CAPABILITIES = Capabilities(multi_hop=False, split=True)
SEARCH_PARAMS = ("max_splits", "percent_step")

# One leg of an allocation: (index into the admitted direct pools, grid units).
Leg = tuple[int, int]


class DirectSplitConfigError(ValueError):
    """`prepare` received an invalid or missing `search.max_splits`/`percent_step`."""


class PreparedDirectSplit:
    """Immutable per-worker preparation: the validated grid settings."""

    __slots__ = ("max_splits", "percent_step")

    def __init__(self, max_splits: int, percent_step: int) -> None:
        self.max_splits = max_splits
        self.percent_step = percent_step


def prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> PreparedDirectSplit:
    refuse_options(config)  # WHI-1548: explicit options are refused, never ignored
    max_splits = config.params.get("max_splits")
    step = config.params.get("percent_step")
    if not isinstance(max_splits, int) or isinstance(max_splits, bool) or max_splits < 1:
        raise DirectSplitConfigError(
            f"{NAME} requires search.max_splits as an integer >= 1, got {max_splits!r}"
        )
    if not isinstance(step, int) or isinstance(step, bool) or not 1 <= step <= 100 or 100 % step:
        raise DirectSplitConfigError(
            f"{NAME} requires search.percent_step as a positive divisor of 100, got {step!r}"
        )
    return PreparedDirectSplit(max_splits=max_splits, percent_step=step)


def leg_amounts(amount_in: int, legs: tuple[Leg, ...], units: int) -> list[int]:
    """The integer amount of every leg (see the module docstring): floored grid shares
    for every leg but the last, which takes the rest of the input."""
    amounts = [amount_in * u // units for _, u in legs[:-1]]
    return [*amounts, amount_in - sum(amounts)]


def allocation_plan(case: Case, pool_ids: tuple[str, ...], legs: tuple[Leg, ...]) -> RoutePlan:
    """The fund-referenced plan of an allocation whose legs are in step order: each
    non-final leg draws an explicit amount from the request fund, the final leg draws
    `ALL_REMAINING` (the explicit final allocation of the integer remainder), and every
    leg's output is a separate terminal target-token fund."""
    units = sum(u for _, u in legs)
    amounts = leg_amounts(case.amount_in, legs, units)
    steps = tuple(
        SwapStep(
            pool_id=pool_ids[i],
            token_in=case.token_in,
            token_out=case.token_out,
            inputs=(
                FundInput(
                    fund_id=REQUEST_FUND_ID,
                    amount=ALL_REMAINING if n == len(legs) - 1 else amounts[n],
                ),
            ),
            output_fund_id=f"OUT{n + 1}",
        )
        for n, (i, _) in enumerate(legs)
    )
    return RoutePlan(steps=steps)


class _BudgetExhausted(Exception):
    pass


def solve(
    case: Case, context: SolveContext, budget: Budget, *, cache: QuoteCache | None = None
) -> SolveResult:
    """`cache` lets a composing solver (`path_split`) share one per-solve quote memo;
    the quote budget then counts every miss of that shared cache."""
    prepared = context.prepared
    if not isinstance(prepared, PreparedDirectSplit):
        raise TypeError(f"{NAME}.solve needs the PreparedDirectSplit returned by prepare()")
    bundle, objective = context.bundle, context.objective
    amount_in = case.amount_in
    units = 100 // prepared.percent_step
    pools = bundle.pools_for_pair(case.token_in, case.token_out)
    pool_ids = tuple(p.pool_id for p in pools)
    max_legs = min(prepared.max_splits, len(pools))

    cache = QuoteCache(bundle) if cache is None else cache
    outputs: dict[tuple[int, int], int | None] = {}  # (pool, amount) -> output, None = failed
    failures: dict[str, int] = {}
    incomplete: list[str] = []
    truncated_by: str | None = None
    skipped: set[tuple[int, int]] = set()  # samples the quote budget left unquoted

    def sample(i: int, amount: int) -> int | None:
        """The output of pool `i` for `amount`, or `None` if its quote failed. Raises
        `_BudgetExhausted` instead of exceeding the declared quote budget."""
        nonlocal truncated_by
        key = (i, amount)
        if key in outputs:
            return outputs[key]
        if budget.max_quotes is not None and cache.misses >= budget.max_quotes:
            truncated_by = "max_quotes"
            raise _BudgetExhausted
        result = cache(pools[i], case.token_in, amount)
        ok = result.status is QuoteStatus.OK and result.amount_in_consumed == amount
        outputs[key] = result.amount_out if ok else None
        if not ok:
            failures[result.status.value] = failures.get(result.status.value, 0) + 1
            if result.status is QuoteStatus.INCOMPLETE_SNAPSHOT:
                incomplete.append(f"{pool_ids[i]} @ {amount}: {result.detail}")
        return outputs[key]

    def try_sample(i: int, amount: int) -> int | None:
        try:
            return sample(i, amount)
        except _BudgetExhausted:
            skipped.add((i, amount))
            return None

    best: tuple[RoutePlan, Evaluation, int, tuple[Leg, ...]] | None = None
    evaluated = finalists_truncated = 0

    def consider(legs: tuple[Leg, ...]) -> None:
        nonlocal best, evaluated, finalists_truncated, truncated_by
        if budget.max_candidates is not None and evaluated >= budget.max_candidates:
            finalists_truncated += 1
            truncated_by = truncated_by or "max_candidates"
            return
        plan = allocation_plan(case, pool_ids, legs)
        evaluation = evaluate(bundle, case, plan, objective, quote=cache)
        evaluated += 1
        if evaluation.status is not EvalStatus.OK:
            return
        score = objective.score(evaluation)
        if best is None or score > best[2]:
            best = (plan, evaluation, score, legs)
            context.report_candidate(plan)

    # 1. Every direct pool at the full input: the single-pool candidates.
    singles = [try_sample(i, amount_in) for i in range(len(pools))]
    best_single = max(
        (i for i, out in enumerate(singles) if out is not None),
        key=lambda i: (singles[i], -i),
        default=None,
    )
    if best_single is not None:
        consider(((best_single, units),))

    # 2. Every pool at every smaller grid size, largest first. Non-final legs only
    #    ever use these floored amounts; a zero amount is never a leg.
    if max_legs > 1:
        for u in range(units - 1, 0, -1):
            if amount_in * u // units > 0:
                for i in range(len(pools)):
                    try_sample(i, amount_in * u // units)

    # 3. Exact DP over the table. A state (non-final legs, units used, residue) is
    #    reached from pools before the current one; the current pool either closes the
    #    allocation as its final leg (taking every remaining unit and the remainder) or
    #    joins as another non-final leg.
    states: dict[tuple[int, int, int], tuple[int, tuple[Leg, ...]]] = {(0, 0, 0): (0, ())}
    finalists: dict[int, tuple[int, tuple[Leg, ...]]] = {}
    transitions = 0
    for j in range(len(pools)):
        for (legs_used, used, residue), (gross, legs) in states.items():
            final_amount = amount_in - (amount_in * used - residue) // units
            out = singles[j] if legs_used == 0 else try_sample(j, final_amount)
            transitions += 1
            if out is None:
                continue
            m, total = legs_used + 1, gross + out
            if m not in finalists or total > finalists[m][0]:
                finalists[m] = (total, (*legs, (j, units - used)))
        if j == len(pools) - 1:
            break
        grown = dict(states)
        for (legs_used, used, residue), (gross, legs) in states.items():
            if legs_used + 1 >= max_legs:
                continue
            for u in range(1, units - used):
                out = outputs.get((j, amount_in * u // units))
                transitions += 1
                if out is None:
                    continue
                key = (legs_used + 1, used + u, residue + amount_in * u % units)
                if key not in grown or gross + out > grown[key][0]:
                    grown[key] = (gross + out, (*legs, (j, u)))
        states = grown

    # 4. Re-evaluate the finalists as complete plans under the declared objective,
    #    simplest first (the single-pool finalist was already evaluated in step 1).
    for m in sorted(finalists):
        if m > 1:
            consider(finalists[m][1])

    skipped_samples = len(skipped)
    truncated = skipped_samples + finalists_truncated
    stats: dict[str, Any] = {
        "percent_step": prepared.percent_step,
        "max_splits": prepared.max_splits,
        "grid_units": units,
        "direct_pools": len(pools),
        "samples_quoted": len(outputs),
        "samples_skipped": skipped_samples,
        "samples_failed": dict(sorted(failures.items())),
        "samples_incomplete": len(incomplete),
        "incomplete_example": incomplete[0] if incomplete else None,
        "dp_transitions": transitions,
        "finalists": len(finalists),
        "finalist_gross": {str(m): str(g) for m, (g, _) in sorted(finalists.items())},
        "finalists_evaluated": evaluated,
        "finalists_truncated": finalists_truncated,
        "truncated_by": truncated_by,
        "quotes_executed": cache.misses,
        "quotes_memoized": cache.hits,
        "best_single_pool": None if best_single is None else pool_ids[best_single],
        "best_single_gross": None if best_single is None else str(singles[best_single]),
        "best_splits": None,
        "best_allocation": None,
    }
    common: dict[str, Any] = {
        "case_id": case.case_id,
        "algorithm": NAME,
        "candidates_considered": evaluated,
        "candidates_truncated": truncated,
        "search_stats": stats,
    }

    if best is not None:
        plan, evaluation, score, legs = best
        stats["best_splits"] = len(legs)
        stats["best_allocation"] = [
            {"pool_id": pool_ids[i], "percent": u * prepared.percent_step, "amount_in": str(a)}
            for (i, u), a in zip(legs, leg_amounts(amount_in, legs, units), strict=True)
        ]
        return SolveResult(
            status=SolveStatus.OK, plan=plan, evaluation=evaluation, score=score, **common
        )
    if not pools:
        return SolveResult(
            status=SolveStatus.NO_ROUTE,
            error=f"no admitted direct pool for {case.token_in}/{case.token_out}",
            **common,
        )
    if truncated:
        return SolveResult(
            status=SolveStatus.TIMEOUT,
            error=(
                f"declared {truncated_by} budget truncated the search with no valid route "
                f"({skipped_samples} sample(s), {finalists_truncated} finalist(s) skipped) "
                "-- not evidence of no_route"
            ),
            **common,
        )
    if incomplete:
        return SolveResult(
            status=SolveStatus.INCOMPLETE_SNAPSHOT,
            error=(
                f"no valid allocation; {len(incomplete)} sample(s) need uncollected pool "
                f"state, e.g. {incomplete[0]}"
            ),
            **common,
        )
    return SolveResult(
        status=SolveStatus.NO_ROUTE,
        error=(
            f"no allocation over the {len(pools)} direct pool(s) produced a valid route "
            f"({', '.join(f'{k}: {v}' for k, v in sorted(failures.items()))})"
        ),
        **common,
    )


FACTORY = AlgorithmFactory(
    name=NAME,
    solve=solve,
    prepare=prepare,
    capabilities=CAPABILITIES,
    search_params=SEARCH_PARAMS,
)

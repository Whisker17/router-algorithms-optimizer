"""Plan/fund-ledger evaluator: `evaluate(bundle, case, plan, objective) ->
Evaluation` (docs/DESIGN.md §2.5, the `evaluate` load-bearing interface in
§4.3: `evaluate(bundle, case, plan, objective: ObjectiveContext) -> Evaluation`).

The evaluator is a fresh, from-scratch replay of a submitted plan against the
*original* bundle state: it never trusts a solver's own claimed output, and every
call starts from a new fund ledger and a new physical-pool state map, so evaluating
one plan can never influence another plan or the bundle (states are immutable and
`quote_exact_in` returns a new state instead of mutating).

It runs in two phases, and never repairs, reorders or normalizes the plan -- the
algorithm's submitted step order *is* the execution order:

1. **Static checks** (before any pool call; `_check_plan`): a positive order input;
   identifier/amount types; every output fund id distinct (and never the request
   fund); every step on a known pool in a direction that pool supports; non-empty
   inputs, no fund referenced twice by one step; every referenced fund is the request
   fund or the output of an *earlier* step (producer-before-consumer -- a fund no
   step produces is external funding and is rejected, never read as zero); every
   input fund carries the step's `token_in` (so merged inputs share one token); and
   the plan's token graph is acyclic (no economic token cycle).
2. **Replay** in submitted order against one transaction-local state per physical
   pool (`pool_id`): each step resolves its input references against the ledger
   (explicit integer amounts, or `ALL_REMAINING` = the fund's balance at that point),
   merges them into one swap and records the output as a new fund. A later use of the
   same pool sees the state left by the earlier use (CPMM reserves, CL price/tick
   state, LB bins and volatility accumulator). A fund drained to zero by a consumer is
   no longer available (a double spend fails). A step whose resolved input is zero is
   deterministic: zero output and no pool call. A quote that fails, or that does not
   consume exactly the merged input, makes the plan `invalid_plan`.

After the last step every fund balance must be either zero or a target-token terminal
balance (the v1 "full-fill" policy): target-token terminal balances are summed into
`gross_output`; any other leftover -- unallocated input, including integer dust, or an
unconsumed intermediate -- is reported in `residuals` and makes the plan
`invalid_plan` (docs/DESIGN.md §2.5: integer remainder belongs to an explicit final
allocation, no implicit dust donation). `funds` is the full ledger (who produced and
consumed how much of every fund), `trace` the per-step record, and `route_features`
integer plan/execution features for later cost models.

The `objective` parameter is used only to *label* and, for an `ok` plan,
attach an estimated cost/net output to the returned `Evaluation` -- evaluation
correctness itself never depends on it. `benchmark.objective.ObjectiveContext`
is imported only under `TYPE_CHECKING`: `routing` stays independent of
`benchmark` at runtime (docs/DESIGN.md §4.2: `benchmark/` depends on
`routing`/pool interfaces, never the reverse), while `evaluate`'s signature
still matches §4.3 exactly. The value passed in only needs `.mode`,
`.fixed_cost`, `.label` and (for `empirical_cost`) `.plan_cost(route_features,
token_out)` at runtime (see `benchmark.objective.ObjectiveContext` for the concrete
implementation actually used everywhere). The empirical cost is a function of the
*complete* plan's `route_features`, computed once per plan after the replay -- never a
per-step sum -- and its detail (status, cohort, scenarios, flags) is kept in `cost`.

Only two evaluator-level statuses are produced here: `ok` and `invalid_plan`. The
richer status vocabulary in docs/DESIGN.md §2.10 (`no_route`, `unsupported`,
`timeout`, ...) belongs to `solve()` (see `routing.algorithms`), which decides
*whether* to submit a plan at all; a plan that *is* submitted either evaluates
cleanly or is invalid.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pools.quote import quote_exact_in
from pools.result import QuoteStatus, SwapResult
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, RoutePlan, SwapStep
from snapshot.models import (
    Case,
    ConcentratedPoolState,
    ConstantProductPoolState,
    LiquidityBookPoolState,
    PoolState,
    SnapshotBundle,
)

if TYPE_CHECKING:
    from benchmark.objective import ObjectiveContext


# The pool-quote seam the replay calls. Always `pools.quote.quote_exact_in` unless a
# caller passes a *pure* equivalent (e.g. `routing.search.QuoteCache`, which returns an
# earlier identical call's immutable result) -- see `evaluate`.
QuoteFn = Callable[[PoolState, str, int], SwapResult[PoolState]]


class EvalStatus(StrEnum):
    OK = "ok"
    INVALID_PLAN = "invalid_plan"


# Per-family pool-call counts in `route_features` (`pool_calls_<family>`): the swap
# cost of a CL/LB step differs from a CPMM step, so later cost models need them.
_FAMILY: dict[type, str] = {
    ConstantProductPoolState: "constant_product",
    ConcentratedPoolState: "concentrated",
    LiquidityBookPoolState: "liquidity_book",
}


@dataclass(frozen=True)
class StepTrace:
    """One replayed step, in submitted order. `inputs` are the resolved
    `(fund_id, amount)` references (an `ALL_REMAINING` reference shows the amount it
    resolved to); `amount_in` is their merged total. `status` is the pool quote
    status, or `zero_input` for a step that made no pool call. `features` are the
    pool's execution features for this swap (e.g. CL initialized ticks crossed)."""

    step: int
    pool_id: str
    token_in: str
    token_out: str
    inputs: tuple[tuple[str, int], ...]
    output_fund_id: str
    amount_in: int
    amount_out: int
    status: str
    features: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "pool_id": self.pool_id,
            "token_in": self.token_in,
            "token_out": self.token_out,
            "inputs": [{"fund_id": f, "amount": str(a)} for f, a in self.inputs],
            "output_fund_id": self.output_fund_id,
            "amount_in": str(self.amount_in),
            "amount_out": str(self.amount_out),
            "status": self.status,
            "features": dict(self.features),
        }


@dataclass(frozen=True)
class FundRecord:
    """One fund of the ledger: its token, the step that produced it (`None` for the
    request fund), how much was produced and how much later steps consumed. The
    remaining balance is a terminal target-token balance or a residual."""

    fund_id: str
    token: str
    producer_step: int | None
    produced: int
    consumed: int

    @property
    def remaining(self) -> int:
        return self.produced - self.consumed

    def to_dict(self) -> dict[str, Any]:
        return {
            "fund_id": self.fund_id,
            "token": self.token,
            "producer_step": self.producer_step,
            "produced": str(self.produced),
            "consumed": str(self.consumed),
            "remaining": str(self.remaining),
        }


@dataclass(frozen=True)
class Evaluation:
    status: EvalStatus
    gross_output: int
    trace: tuple[StepTrace, ...] = ()
    residuals: dict[str, int] = field(default_factory=dict)
    next_states: dict[str, PoolState] = field(default_factory=dict)
    route_features: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    # docs/DESIGN.md §2.9: every Evaluation carries the objective it was scored
    # under, visibly. `estimated_cost`/`estimated_net_output` are `None` --
    # never fabricated as `0` -- whenever the objective does not supply a cost
    # (gross-only mode, or any non-`ok` plan).
    objective_label: str = ""
    estimated_cost: int | None = None
    estimated_net_output: int | None = None
    funds: tuple[FundRecord, ...] = ()
    # empirical_cost only: the complete plan's cost detail (benchmark.costs); a plan
    # without a reliable net score keeps `estimated_*` None and says why here.
    cost: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out = self._to_dict()
        if self.cost is not None:
            out["cost"] = dict(self.cost)
        return out

    def _to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "gross_output": str(self.gross_output),
            "trace": [t.to_dict() for t in self.trace],
            "residuals": {k: str(v) for k, v in self.residuals.items()},
            "funds": [f.to_dict() for f in self.funds],
            "route_features": dict(self.route_features),
            "error": self.error,
            "objective_label": self.objective_label,
            "estimated_cost": None if self.estimated_cost is None else str(self.estimated_cost),
            "estimated_net_output": (
                None if self.estimated_net_output is None else str(self.estimated_net_output)
            ),
        }


def _cost_and_net(
    objective: ObjectiveContext,
    gross_output: int,
    route_features: Mapping[str, int],
    token_out: str,
) -> tuple[int | None, int | None, Mapping[str, Any] | None]:
    """(estimated cost, estimated net output, cost detail). Gross-only stays
    `(None, None, None)` rather than a fabricated `0`; `empirical_cost` yields a net
    output only for a net-rankable complete plan (docs/DESIGN.md §2.9: "Unknown
    price/cost still permits gross-output comparison but produces no reliable net
    score.")."""
    if objective.mode == "gross_only":
        return None, None, None
    if objective.mode == "empirical_cost":
        detail = objective.plan_cost(dict(route_features), token_out)
        if not detail.get("net_rankable"):
            return None, None, detail
        cost = int(detail["nominal_out_raw"])
        return cost, gross_output - cost, detail
    cost = objective.fixed_cost
    return cost, gross_output - cost, None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_id(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _token_cycle(edges: dict[str, set[str]]) -> list[str] | None:
    """A directed cycle in the plan's token graph (token_in -> token_out per step),
    as a closed token path, or `None`. Iterative three-colour DFS in sorted order so
    the reported cycle is deterministic."""
    state: dict[str, int] = {}  # 1 = on the current path, 2 = finished
    for root in sorted(edges):
        if root in state:
            continue
        path = [root]
        stack = [iter(sorted(edges.get(root, ())))]
        state[root] = 1
        while stack:
            nxt = next(stack[-1], None)
            if nxt is None:
                state[path.pop()] = 2
                stack.pop()
            elif state.get(nxt) == 1:
                return path[path.index(nxt) :] + [nxt]
            elif nxt not in state:
                state[nxt] = 1
                path.append(nxt)
                stack.append(iter(sorted(edges.get(nxt, ()))))
    return None


def _check_plan(bundle: SnapshotBundle, case: Case, plan: RoutePlan) -> str | None:
    """Every docs/DESIGN.md §2.5 rule that does not depend on balances or pool math.
    Returns the first violation, or `None` for a structurally valid plan."""
    if not _is_int(case.amount_in) or case.amount_in <= 0:
        return (
            f"case {case.case_id!r}: order input must be a positive integer, got {case.amount_in!r}"
        )
    if case.token_in == case.token_out:
        return f"case {case.case_id!r}: token_in and token_out are both {case.token_in!r}"

    producer: dict[str, int] = {}
    for idx, step in enumerate(plan.steps):
        out = step.output_fund_id
        if not _is_id(out):
            return f"step {idx}: output fund id must be a non-empty string, got {out!r}"
        if out == REQUEST_FUND_ID or out in producer:
            owner = f"output of step {producer[out]}" if out in producer else "the request fund"
            return f"step {idx}: output fund id {out!r} is not distinct (already {owner})"
        producer[out] = idx
    fund_token = {REQUEST_FUND_ID: case.token_in}
    fund_token.update({s.output_fund_id: s.token_out for s in plan.steps})

    edges: dict[str, set[str]] = {}
    for idx, step in enumerate(plan.steps):
        pool = bundle.pools.get(step.pool_id) if _is_id(step.pool_id) else None
        if pool is None:
            return f"step {idx}: unknown pool {step.pool_id!r}"
        if step.token_in not in (pool.token0, pool.token1):
            return (
                f"step {idx}: declared token_in {step.token_in!r} is not a token of pool "
                f"{pool.pool_id!r}"
            )
        if pool.other_token(step.token_in) != step.token_out:
            return (
                f"step {idx}: declared token_out {step.token_out!r} does not match pool's "
                f"other token {pool.other_token(step.token_in)!r}"
            )
        if not step.inputs:
            return f"step {idx}: no input-fund references"
        seen: set[str] = set()
        for finput in step.inputs:
            fid, amount = finput.fund_id, finput.amount
            if not _is_id(fid):
                return f"step {idx}: input fund id must be a non-empty string, got {fid!r}"
            if fid in seen:
                return f"step {idx}: fund {fid!r} is referenced more than once by one step"
            seen.add(fid)
            if amount != ALL_REMAINING and not (_is_int(amount) and amount >= 0):
                return (
                    f"step {idx}: input amount must be a non-negative int or {ALL_REMAINING!r}, "
                    f"got {amount!r}"
                )
            if fid != REQUEST_FUND_ID and fid not in producer:
                return (
                    f"step {idx}: input fund {fid!r} is not available: external funding "
                    "(neither the request fund nor produced by any step)"
                )
            if fid != REQUEST_FUND_ID and producer[fid] >= idx:
                return (
                    f"step {idx}: input fund {fid!r} is not available: not yet produced "
                    f"(its producer is step {producer[fid]}; producers must come first)"
                )
            if fund_token[fid] != step.token_in:
                return (
                    f"step {idx}: input fund {fid!r} holds {fund_token[fid]!r}, not the step's "
                    f"token_in {step.token_in!r} (merged inputs must share one token)"
                )
        edges.setdefault(step.token_in, set()).add(step.token_out)

    cycle = _token_cycle(edges)
    if cycle is not None:
        return "economic token cycle: " + " -> ".join(cycle)
    return None


def _trace(
    idx: int,
    step: SwapStep,
    resolved: list[tuple[str, int]],
    amount_in: int,
    amount_out: int,
    status: str,
    features: Mapping[str, int],
) -> StepTrace:
    return StepTrace(
        step=idx,
        pool_id=step.pool_id,
        token_in=step.token_in,
        token_out=step.token_out,
        inputs=tuple(resolved),
        output_fund_id=step.output_fund_id,
        amount_in=amount_in,
        amount_out=amount_out,
        status=status,
        features=dict(features),
    )


def evaluate(
    bundle: SnapshotBundle,
    case: Case,
    plan: RoutePlan,
    objective: ObjectiveContext,
    *,
    quote: QuoteFn = quote_exact_in,
) -> Evaluation:
    """Replay `plan` from the bundle's original state (see the module docstring).

    `quote` is the pool-quote seam. It must behave exactly like
    `pools.quote.quote_exact_in` -- a pure function of `(state, token_in, amount)` --
    and exists only so a solver scoring many candidates that share prefixes can
    memoize identical calls (WHI-1438). The runner's independent evaluation always
    uses the default."""
    route_features: dict[str, int] = {"hops": len(plan.steps)}
    ledger: dict[str, FundRecord] = {}
    next_states: dict[str, PoolState] = {}
    trace: list[StepTrace] = []

    def invalid(error: str, residuals: dict[str, int] | None = None, gross: int = 0) -> Evaluation:
        return Evaluation(
            status=EvalStatus.INVALID_PLAN,
            gross_output=gross,
            trace=tuple(trace),
            residuals=residuals or {},
            next_states=next_states,
            route_features=route_features,
            error=error,
            objective_label=objective.label,
            funds=tuple(ledger.values()),
        )

    error = _check_plan(bundle, case, plan)
    if error is not None:
        return invalid(error)

    consumers: dict[str, int] = {}
    for step in plan.steps:
        for finput in step.inputs:
            consumers[finput.fund_id] = consumers.get(finput.fund_id, 0) + 1
    route_features.update(
        distinct_pools=len({s.pool_id for s in plan.steps}),
        merge_steps=sum(len(s.inputs) > 1 for s in plan.steps),
        split_funds=sum(n > 1 for n in consumers.values()),
        pool_calls=0,
        zero_input_steps=0,
    )

    ledger[REQUEST_FUND_ID] = FundRecord(REQUEST_FUND_ID, case.token_in, None, case.amount_in, 0)
    drained: set[str] = set()

    for idx, step in enumerate(plan.steps):
        resolved: list[tuple[str, int]] = []
        for finput in step.inputs:
            fund = ledger[finput.fund_id]  # produced earlier: guaranteed by _check_plan
            if fund.fund_id in drained:
                return invalid(
                    f"step {idx}: input fund {fund.fund_id!r} is not available "
                    "(already fully consumed by an earlier step)"
                )
            amount = fund.remaining if finput.amount == ALL_REMAINING else finput.amount
            assert isinstance(amount, int)
            if amount > fund.remaining:
                return invalid(
                    f"step {idx}: fund {fund.fund_id!r} balance {fund.remaining} is less than "
                    f"requested {amount}"
                )
            resolved.append((fund.fund_id, amount))
        for fund_id, amount in resolved:
            ledger[fund_id] = replace(ledger[fund_id], consumed=ledger[fund_id].consumed + amount)
            if ledger[fund_id].remaining == 0:
                drained.add(fund_id)
        total_input = sum(amount for _, amount in resolved)

        if total_input == 0:
            route_features["zero_input_steps"] += 1
            trace.append(_trace(idx, step, resolved, 0, 0, "zero_input", {}))
            ledger[step.output_fund_id] = FundRecord(step.output_fund_id, step.token_out, idx, 0, 0)
            continue

        pool_state = next_states.get(step.pool_id, bundle.pools[step.pool_id])
        result = quote(pool_state, step.token_in, total_input)
        trace.append(
            _trace(
                idx,
                step,
                resolved,
                total_input,
                result.amount_out,
                result.status.value,
                result.features,
            )
        )
        if result.status is not QuoteStatus.OK or result.new_state is None:
            return invalid(
                f"step {idx}: pool {step.pool_id!r} quote failed "
                f"({result.status.value}): {result.detail}"
            )
        if result.amount_in_consumed != total_input:
            return invalid(
                f"step {idx}: pool {step.pool_id!r} consumed {result.amount_in_consumed} of the "
                f"{total_input} exact input"
            )
        next_states[step.pool_id] = result.new_state
        family = _FAMILY[type(pool_state)]
        route_features["pool_calls"] += 1
        route_features[f"pool_calls_{family}"] = route_features.get(f"pool_calls_{family}", 0) + 1
        for feature, value in result.features.items():  # e.g. CL initialized ticks crossed
            route_features[feature] = route_features.get(feature, 0) + value
        ledger[step.output_fund_id] = FundRecord(
            step.output_fund_id, step.token_out, idx, result.amount_out, 0
        )
    route_features["repeated_pool_calls"] = route_features["pool_calls"] - len(next_states)

    gross_output = 0
    residuals: dict[str, int] = {}
    for fund in ledger.values():
        if fund.remaining <= 0:
            continue
        if fund.token == case.token_out:
            gross_output += fund.remaining
        else:
            residuals[fund.fund_id] = fund.remaining

    if residuals:
        detail = ", ".join(
            f"{fid!r} ({ledger[fid].token}, "
            + (
                "unallocated order input"
                if fid == REQUEST_FUND_ID
                else f"unconsumed output of step {ledger[fid].producer_step}"
            )
            + f"): {amount}"
            for fid, amount in residuals.items()
        )
        return invalid(
            f"unconsumed residual balances (v1 full-fill policy): {detail}",
            residuals=residuals,
            gross=gross_output,
        )

    estimated_cost, estimated_net_output, cost_detail = _cost_and_net(
        objective, gross_output, route_features, case.token_out
    )
    return Evaluation(
        status=EvalStatus.OK,
        gross_output=gross_output,
        trace=tuple(trace),
        residuals={},
        next_states=next_states,
        route_features=route_features,
        error=None,
        objective_label=objective.label,
        estimated_cost=estimated_cost,
        estimated_net_output=estimated_net_output,
        funds=tuple(ledger.values()),
        cost=cost_detail,
    )

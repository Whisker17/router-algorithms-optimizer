"""Plan/fund-ledger evaluator: `evaluate(bundle, case, plan) -> Evaluation`
(docs/DESIGN.md §2.5, the `evaluate` load-bearing interface in §4.3 specialized to
`ConstantProductPoolState`/no explicit `ObjectiveContext` -- the objective only
scores an already-evaluated plan, it never changes evaluation itself).

The evaluator is a fresh, from-scratch replay of a submitted plan against the
*original* bundle state: it never trusts a solver's own claimed output. It
enforces, in order: known pools, non-empty inputs, distinct output fund ids,
producer-before-consumer availability, a single shared input token per step, the
step's declared direction matching the pool's tokens, and -- after every step has
run -- that every fund balance is either fully consumed or is a target-token
terminal balance (the v1 "full-fill" policy: any other leftover makes the plan
`invalid_plan`, docs/DESIGN.md §2.5). A zero-input step is deterministic (zero
output, no pool call) rather than an error.

Only two evaluator-level statuses are produced here: `ok` and `invalid_plan`. The
richer status vocabulary in docs/DESIGN.md §2.10 (`no_route`, `unsupported`,
`timeout`, ...) belongs to `solve()` (see `routing.algorithms`), which decides
*whether* to submit a plan at all; a plan that *is* submitted either evaluates
cleanly or is invalid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pools.constant_product import QuoteStatus, quote_exact_in
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, RoutePlan
from snapshot.models import Case, ConstantProductPoolState, SnapshotBundle


class EvalStatus(StrEnum):
    OK = "ok"
    INVALID_PLAN = "invalid_plan"


@dataclass(frozen=True)
class StepTrace:
    pool_id: str
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id,
            "token_in": self.token_in,
            "token_out": self.token_out,
            "amount_in": str(self.amount_in),
            "amount_out": str(self.amount_out),
            "status": self.status,
        }


@dataclass(frozen=True)
class Evaluation:
    status: EvalStatus
    gross_output: int
    trace: tuple[StepTrace, ...] = ()
    residuals: dict[str, int] = field(default_factory=dict)
    next_states: dict[str, ConstantProductPoolState] = field(default_factory=dict)
    route_features: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "gross_output": str(self.gross_output),
            "trace": [t.to_dict() for t in self.trace],
            "residuals": {k: str(v) for k, v in self.residuals.items()},
            "route_features": dict(self.route_features),
            "error": self.error,
        }


def _invalid(
    trace: list[StepTrace], route_features: dict[str, int], error: str
) -> Evaluation:
    return Evaluation(
        status=EvalStatus.INVALID_PLAN,
        gross_output=0,
        trace=tuple(trace),
        residuals={},
        next_states={},
        route_features=route_features,
        error=error,
    )


def evaluate(bundle: SnapshotBundle, case: Case, plan: RoutePlan) -> Evaluation:
    funds: dict[str, int] = {REQUEST_FUND_ID: case.amount_in}
    fund_token: dict[str, str] = {REQUEST_FUND_ID: case.token_in}
    produced: set[str] = {REQUEST_FUND_ID}
    next_states: dict[str, ConstantProductPoolState] = {}
    trace: list[StepTrace] = []
    route_features = {"hops": len(plan.steps)}

    for idx, step in enumerate(plan.steps):
        pool_state = next_states.get(step.pool_id, bundle.pools.get(step.pool_id))
        if pool_state is None:
            return _invalid(trace, route_features, f"step {idx}: unknown pool {step.pool_id!r}")
        if not step.inputs:
            return _invalid(trace, route_features, f"step {idx}: no input-fund references")
        if step.output_fund_id in produced:
            return _invalid(
                trace,
                route_features,
                f"step {idx}: output fund id {step.output_fund_id!r} is not distinct "
                "(already produced)",
            )

        total_input = 0
        input_token: str | None = None
        consumed: list[tuple[str, int]] = []
        for finput in step.inputs:
            if finput.fund_id not in funds:
                return _invalid(
                    trace,
                    route_features,
                    f"step {idx}: input fund {finput.fund_id!r} is not available "
                    "(not yet produced, or already fully consumed)",
                )
            available = funds[finput.fund_id]
            if finput.amount == ALL_REMAINING:
                amount = available
            else:
                if not isinstance(finput.amount, int) or isinstance(finput.amount, bool):
                    return _invalid(
                        trace, route_features, f"step {idx}: input amount must be int or "
                        f"{ALL_REMAINING!r}, got {finput.amount!r}"
                    )
                if finput.amount < 0:
                    return _invalid(
                        trace,
                        route_features,
                        f"step {idx}: input amount must be non-negative, got {finput.amount}",
                    )
                amount = finput.amount
                if amount > available:
                    return _invalid(
                        trace,
                        route_features,
                        f"step {idx}: fund {finput.fund_id!r} balance {available} is less "
                        f"than requested {amount}",
                    )
            token = fund_token[finput.fund_id]
            if input_token is None:
                input_token = token
            elif token != input_token:
                return _invalid(
                    trace,
                    route_features,
                    f"step {idx}: merged inputs must share one token, got "
                    f"{input_token!r} and {token!r}",
                )
            total_input += amount
            consumed.append((finput.fund_id, amount))

        if input_token != step.token_in:
            return _invalid(
                trace,
                route_features,
                f"step {idx}: declared token_in {step.token_in!r} does not match input "
                f"funds' token {input_token!r}",
            )
        try:
            expected_out = pool_state.other_token(step.token_in)
        except ValueError as exc:
            return _invalid(trace, route_features, f"step {idx}: {exc}")
        if expected_out != step.token_out:
            return _invalid(
                trace,
                route_features,
                f"step {idx}: declared token_out {step.token_out!r} does not match pool's "
                f"other token {expected_out!r}",
            )

        for fund_id, amount in consumed:
            funds[fund_id] -= amount
            if funds[fund_id] == 0:
                del funds[fund_id]
                del fund_token[fund_id]

        if total_input == 0:
            trace.append(
                StepTrace(
                    pool_id=step.pool_id,
                    token_in=step.token_in,
                    token_out=step.token_out,
                    amount_in=0,
                    amount_out=0,
                    status="zero_input",
                )
            )
            funds[step.output_fund_id] = 0
            fund_token[step.output_fund_id] = step.token_out
            produced.add(step.output_fund_id)
            continue

        result = quote_exact_in(pool_state, step.token_in, total_input)
        trace.append(
            StepTrace(
                pool_id=step.pool_id,
                token_in=step.token_in,
                token_out=step.token_out,
                amount_in=total_input,
                amount_out=result.amount_out,
                status=result.status.value,
            )
        )
        if result.status is not QuoteStatus.OK or result.new_state is None:
            return _invalid(
                trace,
                route_features,
                f"step {idx}: pool {step.pool_id!r} quote failed "
                f"({result.status.value}): {result.detail}",
            )
        next_states[step.pool_id] = result.new_state
        funds[step.output_fund_id] = result.amount_out
        fund_token[step.output_fund_id] = step.token_out
        produced.add(step.output_fund_id)

    gross_output = 0
    residuals: dict[str, int] = {}
    for fund_id, amount in funds.items():
        if amount <= 0:
            continue
        if fund_token[fund_id] == case.token_out:
            gross_output += amount
        else:
            residuals[fund_id] = amount

    if residuals:
        return Evaluation(
            status=EvalStatus.INVALID_PLAN,
            gross_output=gross_output,
            trace=tuple(trace),
            residuals=residuals,
            next_states=next_states,
            route_features=route_features,
            error=f"unconsumed residual balances: {residuals}",
        )

    return Evaluation(
        status=EvalStatus.OK,
        gross_output=gross_output,
        trace=tuple(trace),
        residuals={},
        next_states=next_states,
        route_features=route_features,
        error=None,
    )

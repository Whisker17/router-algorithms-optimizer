"""`direct` (docs/DESIGN.md §2.6): evaluate every admitted pool directly
connecting the case's two tokens and keep the objective-best one. Single pool,
no multi-hop, no split -- exactly the "Single pool" capability in the mandatory
algorithm table.

A candidate whose quote needs state outside the bundle's collected range
(`incomplete_snapshot`, e.g. a concentrated-liquidity swap past the collected tick
bitmap words) makes the whole solve `incomplete_snapshot`, never a silent skip: that
pool might have been the best one, so no "best direct pool" claim is possible
(docs/DESIGN.md §2.2: state-range exhaustion "is not equivalent to exhausted real
liquidity").

Budgets (WHI-1437): every candidate costs at most one quote (a one-step plan), so
`direct` evaluates at most `min(max_candidates, max_quotes)` pools in admitted order
and reports the rest as declared truncation (`candidates_truncated`). A truncated
`no_route` only means no *evaluated* pool worked. Each new best plan is published via
`SolveContext.report_candidate`.
"""

from __future__ import annotations

from pools.result import QuoteStatus
from routing.algorithms.base import (
    AlgorithmFactory,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
)
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from snapshot.models import Case

NAME = "direct"

OUTPUT_FUND_ID = "OUT"


def _plan_for_pool(pool_id: str, case: Case) -> RoutePlan:
    return RoutePlan(
        steps=(
            SwapStep(
                pool_id=pool_id,
                token_in=case.token_in,
                token_out=case.token_out,
                inputs=(FundInput(fund_id=REQUEST_FUND_ID, amount=ALL_REMAINING),),
                output_fund_id=OUTPUT_FUND_ID,
            ),
        )
    )


def solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    admitted = context.bundle.pools_for_pair(case.token_in, case.token_out)
    caps = [c for c in (budget.max_candidates, budget.max_quotes) if c is not None]
    candidates = admitted[: min(caps)] if caps else admitted
    truncated = len(admitted) - len(candidates)

    best_plan: RoutePlan | None = None
    best_evaluation: Evaluation | None = None
    best_score: int | None = None
    incomplete: list[str] = []

    for pool in candidates:
        plan = _plan_for_pool(pool.pool_id, case)
        evaluation = evaluate(context.bundle, case, plan, context.objective)
        if evaluation.status is not EvalStatus.OK:
            if evaluation.trace and evaluation.trace[-1].status == QuoteStatus.INCOMPLETE_SNAPSHOT:
                incomplete.append(f"{pool.pool_id}: {evaluation.error}")
            continue
        score = context.objective.score(evaluation)
        if best_score is None or score > best_score:
            best_plan, best_evaluation, best_score = plan, evaluation, score
            context.report_candidate(plan)

    if incomplete:
        return SolveResult(
            case_id=case.case_id,
            algorithm=NAME,
            status=SolveStatus.INCOMPLETE_SNAPSHOT,
            candidates_considered=len(candidates),
            candidates_truncated=truncated,
            error="candidate pool state is incomplete for this amount: " + "; ".join(incomplete),
        )

    if best_plan is None or best_evaluation is None:
        return SolveResult(
            case_id=case.case_id,
            algorithm=NAME,
            status=SolveStatus.NO_ROUTE,
            candidates_considered=len(candidates),
            candidates_truncated=truncated,
            error=(
                f"no admitted direct pool for {case.token_in}/{case.token_out}"
                if not admitted
                else "no admitted direct pool produced a valid route"
                + (f" ({truncated} candidate(s) truncated by the budget)" if truncated else "")
            ),
        )

    return SolveResult(
        case_id=case.case_id,
        algorithm=NAME,
        status=SolveStatus.OK,
        plan=best_plan,
        evaluation=best_evaluation,
        score=best_score,
        candidates_considered=len(candidates),
        candidates_truncated=truncated,
    )


FACTORY = AlgorithmFactory(name=NAME, solve=solve)

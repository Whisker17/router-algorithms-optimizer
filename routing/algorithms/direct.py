"""`direct` (docs/DESIGN.md §2.6): evaluate every admitted pool directly
connecting the case's two tokens and keep the objective-best one. Single pool,
no multi-hop, no split -- exactly the "Single pool" capability in the mandatory
algorithm table.
"""

from __future__ import annotations

from routing.algorithms.base import Budget, SolveContext, SolveResult, SolveStatus
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
    candidates = context.bundle.pools_for_pair(case.token_in, case.token_out)
    if budget.max_candidates is not None:
        candidates = candidates[: budget.max_candidates]

    best_plan: RoutePlan | None = None
    best_evaluation: Evaluation | None = None
    best_score: int | None = None

    for pool in candidates:
        plan = _plan_for_pool(pool.pool_id, case)
        evaluation = evaluate(context.bundle, case, plan, context.objective)
        if evaluation.status is not EvalStatus.OK:
            continue
        score = context.objective.score(evaluation)
        if best_score is None or score > best_score:
            best_plan, best_evaluation, best_score = plan, evaluation, score

    if best_plan is None or best_evaluation is None:
        return SolveResult(
            case_id=case.case_id,
            algorithm=NAME,
            status=SolveStatus.NO_ROUTE,
            candidates_considered=len(candidates),
            error=(
                f"no admitted direct pool for {case.token_in}/{case.token_out}"
                if not candidates
                else "no admitted direct pool produced a valid route"
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
    )

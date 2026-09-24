"""`run_experiment(bundle, profile, algorithms) -> RunManifest` (docs/DESIGN.md
§4.3). Runs every case in the bundle through every algorithm the profile names,
in fixed input order, and saves a versioned result record. Deliberately simple:
sequential in-process execution, no worker isolation or hard timeout -- a later
measurement-isolation ticket owns those (docs/DESIGN.md §2.10: "Keep the runner
initially simple").

Critically, this module owns the independent-evaluation step from
docs/DESIGN.md §4.4's Run flow ("solve each case under limits -> independently
evaluate complete plans -> append atomic result records"): every submitted plan
is re-evaluated from scratch against the original bundle here, and *that*
evaluation -- never the solver's own self-reported one -- becomes the recorded
`CaseRecord`. The solver's own claim is kept only as a `solver_reported_*`
diagnostic (docs/DESIGN.md §2.5).
"""

from __future__ import annotations

from pathlib import Path

from benchmark.objective import ObjectiveContext
from benchmark.profile import RunProfile
from benchmark.results import CaseRecord, RunManifest, save_run
from routing.algorithms.base import Budget, SolveContext, SolveResult, SolveStatus
from routing.algorithms.registry import get_algorithm
from routing.evaluator import EvalStatus, evaluate
from snapshot.models import Case, SnapshotBundle


def _independent_record(
    bundle: SnapshotBundle, case: Case, objective: ObjectiveContext, solved: SolveResult
) -> CaseRecord:
    solver_reported_gross_output = (
        solved.evaluation.gross_output if solved.evaluation is not None else None
    )

    if solved.plan is None:
        # Nothing to independently verify: a solver's "no route found" (or
        # unsupported/timeout/...) claim has no submitted plan to re-evaluate.
        # docs/DESIGN.md §2.5's independence guarantee is specifically about
        # plans; the search-process claim itself is recorded as-is.
        return CaseRecord(
            case_id=solved.case_id,
            algorithm=solved.algorithm,
            status=solved.status,
            evaluation=None,
            score=None,
            candidates_considered=solved.candidates_considered,
            error=solved.error,
            solver_reported_status=solved.status,
            solver_reported_gross_output=solver_reported_gross_output,
            solver_reported_score=solved.score,
        )

    independent_evaluation = evaluate(bundle, case, solved.plan, objective)
    if independent_evaluation.status is EvalStatus.OK:
        status = SolveStatus.OK
        score: int | None = objective.score(independent_evaluation)
        error = None
    else:
        # The solver may have claimed `ok` for this exact plan; the runner's own
        # fresh replay disagrees, and the fresh replay always wins.
        status = SolveStatus.INVALID_PLAN
        score = None
        error = independent_evaluation.error

    return CaseRecord(
        case_id=solved.case_id,
        algorithm=solved.algorithm,
        status=status,
        evaluation=independent_evaluation,
        score=score,
        candidates_considered=solved.candidates_considered,
        error=error,
        solver_reported_status=solved.status,
        solver_reported_gross_output=solver_reported_gross_output,
        solver_reported_score=solved.score,
    )


def run_experiment(
    bundle: SnapshotBundle,
    profile: RunProfile,
    *,
    results_dir: str | Path,
    replay_command: str,
    budget: Budget | None = None,
    run_id: str | None = None,
) -> RunManifest:
    budget = budget or Budget()
    context = SolveContext(bundle=bundle, objective=profile.objective)

    records: list[CaseRecord] = []
    for case in bundle.cases:  # fixed input order (docs/DESIGN.md §2.10)
        for algorithm_name in profile.algorithms:
            solve_fn = get_algorithm(algorithm_name)
            solved = solve_fn(case, context, budget)
            records.append(_independent_record(bundle, case, profile.objective, solved))

    return save_run(
        results_dir,
        bundle=bundle,
        profile=profile,
        results=records,
        replay_command=replay_command,
        run_id=run_id,
    )

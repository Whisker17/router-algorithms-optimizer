"""`run_experiment(bundle, profile, algorithms) -> RunManifest` (docs/DESIGN.md
§4.3). Runs every case in the bundle through every algorithm the profile names,
in fixed input order, and saves a versioned result record. Deliberately simple:
sequential in-process execution, no worker isolation or hard timeout -- a later
measurement-isolation ticket owns those (docs/DESIGN.md §2.10: "Keep the runner
initially simple").
"""

from __future__ import annotations

from pathlib import Path

from benchmark.profile import RunProfile
from benchmark.results import RunManifest, save_run
from routing.algorithms.base import Budget, SolveContext, SolveResult
from routing.algorithms.registry import get_algorithm
from snapshot.models import SnapshotBundle


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

    results: list[SolveResult] = []
    for case in bundle.cases:  # fixed input order (docs/DESIGN.md §2.10)
        for algorithm_name in profile.algorithms:
            solve_fn = get_algorithm(algorithm_name)
            results.append(solve_fn(case, context, budget))

    return save_run(
        results_dir,
        bundle=bundle,
        profile_path=profile.source_path,
        objective=profile.objective,
        algorithms=profile.algorithms,
        results=results,
        replay_command=replay_command,
        run_id=run_id,
    )

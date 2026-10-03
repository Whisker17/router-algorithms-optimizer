"""`single_path_bounded` (WHI-1599; frozen interface `docs/references/research-022/
pruning-contract.md` R022-Q02/1 §10): the unchanged `single_path` search with rule S1
upper-bound pruning switched on -- an **exact acceleration**, not a new heuristic.

It calls `single_path.solve(..., bound_pruning=True)` on the `single_path.prepare_bounded`
result and relabels the result with its own name (the runner refuses any other name). The
bound table is built eagerly in `prepare` (charged to the preparation step) and reported under
`search_stats["bound_pruning"]`, next to -- never folded into -- the reference counters. It
accepts no `algorithm_options`, has `single_path`'s capabilities and `search.*` keys, and is a
`custom`-group opt-in identity: it is not in `BASE_STRATEGIES`/`OPTIMIZED_STRATEGIES`, and
`--strategies all` appends it (`benchmark.strategies.R022_ADDITIONS`).

The factory functions are module-level: workers receive a factory by pickling it, which
serializes functions by reference.
"""

from __future__ import annotations

from dataclasses import replace

from routing.algorithms import single_path
from routing.algorithms.base import AlgorithmFactory, Budget, SolveContext, SolveResult
from snapshot.models import Case

NAME = "single_path_bounded"
REFERENCE = single_path.NAME
ISSUE = "WHI-1599"
CONTRACT_DOC = "docs/references/research-022/pruning-contract.md (R022-Q02/1)"

PROVENANCE = {
    "experimental": True,
    "opt_in": True,
    "issue": ISSUE,
    "reference": REFERENCE,
    "contract": CONTRACT_DOC,
    "identity": (
        f"exact acceleration of {REFERENCE}; identical plan under non-binding budgets; "
        "not a new heuristic"
    ),
    "claims": [
        "status, plan, evaluation, score and error equal the reference's whenever the bounded "
        "run is not budget-truncated (pruning-contract §8.2)",
        "candidates_considered, candidates_truncated, paths_enumerated, direct_candidates, "
        "paths_truncated, truncated_by, best_hops and the report_candidate sequence equal the "
        "reference's (§8.1)",
    ],
    "not_claimed": [
        "any speedup",
        "identical work counters (paths_evaluated, paths_pruned, quotes_executed, "
        "quotes_memoized, failed_candidates, paths_incomplete)",
        "exactness under a binding budget (labelled not_exact_budget_binding)",
        "any objective other than gross_only, synthetic_fixed_cost, empirical_cost",
        "optimality",
    ],
}


def solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    return replace(single_path.solve(case, context, budget, bound_pruning=True), algorithm=NAME)


FACTORY = AlgorithmFactory(
    name=NAME,
    solve=solve,
    prepare=single_path.prepare_bounded,
    capabilities=single_path.CAPABILITIES,
    search_params=single_path.SEARCH_PARAMS,
    provenance=PROVENANCE,
)

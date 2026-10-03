"""`incremental_graph_bounded` (WHI-1600; frozen interface `docs/references/research-022/
pruning-contract.md` R022-Q02/1 §5, §10): the unchanged `incremental_graph` chunk search with rule
I1 upper-bound pruning switched on -- an **exact acceleration**, not a new heuristic.

It calls `incremental_graph.solve(..., bound_pruning=True)` on the
`incremental_graph.prepare_bounded` result and relabels the result with its own name (the runner
refuses any other name). Every other field -- `chosen_source`, `truncated_stages`, the allocation
-- keeps the reference's value. The bound table is built eagerly in `prepare` (charged to the
preparation step) and reported under `search_stats["bound_pruning"]`, next to -- never folded
into -- the reference counters. It accepts no `algorithm_options`, has `incremental_graph`'s
capabilities and `search.*` / `graph.*` keys, and is a `custom`-group opt-in identity: it is not
in `BASE_STRATEGIES`/`OPTIMIZED_STRATEGIES`, and `--strategies all` appends it
(`benchmark.strategies.R022_ADDITIONS`).

The factory functions are module-level: workers receive a factory by pickling it, which
serializes functions by reference.
"""

from __future__ import annotations

from dataclasses import replace

from routing.algorithms import incremental_graph
from routing.algorithms.base import AlgorithmFactory, Budget, SolveContext, SolveResult
from snapshot.models import Case

NAME = "incremental_graph_bounded"
REFERENCE = incremental_graph.NAME
ISSUE = "WHI-1600"
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
        "run is not budget-truncated (pruning-contract §8.2); chunk choices, carry and the "
        "final comparison are the reference's (rule I1, original-state bounds only)",
        "candidates_considered, candidates_truncated, paths_enumerated, paths_scored, "
        "paths_rejected_cycle, paths_truncated, the chunks_* and incremental_* counters, "
        "path_split_*, chosen_source, topology, truncated_by and truncated_stages equal the "
        "reference's (§8.1)",
    ],
    "not_claimed": [
        "any speedup",
        "identical work counters (marginal_failures, marginal_incomplete, incomplete_example, "
        "quotes_executed, quotes_memoized)",
        "exactness under a binding budget (labelled not_exact_budget_binding)",
        "pruning when the retained simpler candidate is absent (P0) or with graph_reuse",
        "optimality",
    ],
}


def solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    return replace(
        incremental_graph.solve(case, context, budget, bound_pruning=True), algorithm=NAME
    )


FACTORY = AlgorithmFactory(
    name=NAME,
    solve=solve,
    prepare=incremental_graph.prepare_bounded,
    capabilities=incremental_graph.CAPABILITIES,
    search_params=incremental_graph.SEARCH_PARAMS,
    graph_params=incremental_graph.GRAPH_PARAMS,
    provenance=PROVENANCE,
)

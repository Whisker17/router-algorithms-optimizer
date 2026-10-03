"""`metis_history_bounded` (WHI-1600; frozen interface `docs/references/research-022/
pruning-contract.md` R022-Q02/1 §6, §10): the unchanged `metis_history` label search with rule M1
(arrival) and, behind the structural gate `G_M2`, rule M2 (label) upper-bound pruning switched
on -- an **exact acceleration**, not a new heuristic.

It calls `metis_history.solve(..., bound_pruning=True)` on the `metis_history.prepare_bounded`
result and relabels the result with its own name (the runner refuses any other name; the `r021`
diagnostics carry it too). Every other field keeps the reference's value. The bound table is built
eagerly in `prepare` (charged to the preparation step); the per-solve `U_h` table, built only
behind an open gate, is charged as `bound_table_cost`. All of it is reported under
`search_stats["bound_pruning"]`, never folded into the reference counters.

It takes **exactly `metis_history`'s `algorithm_options`** (the same validator and ranges; the
pruning is not an option, so `settings_sha256` of the options is the reference's). The preset
file `config/metis_history_bounded/preset_v1.yaml` is a copy under the bounded strategy's own key
with identical options (contract §12.3(1): `benchmark.profile.preset_options` checks a preset's
`algorithm` against its factory). It has `metis_history`'s capabilities and `search.*` / `graph.*`
keys and is a `custom`-group opt-in identity: it is not in `BASE_STRATEGIES`/
`OPTIMIZED_STRATEGIES`, and `--strategies all` appends it (`benchmark.strategies.R022_ADDITIONS`).

The factory functions are module-level: workers receive a factory by pickling it, which
serializes functions by reference.
"""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

from routing.algorithms import metis_history
from routing.algorithms.base import AlgorithmFactory, Budget, SolveContext, SolveResult
from snapshot.models import Case

NAME = metis_history.BOUNDED_NAME
REFERENCE = metis_history.NAME
ISSUE = "WHI-1600"
CONTRACT_DOC = "docs/references/research-022/pruning-contract.md (R022-Q02/1)"

# The bounded strategy's own pin of the same options (a copy of `metis_history`'s preset file).
PRESET = {
    "path": "config/metis_history_bounded/preset_v1.yaml",
    "sha256": "242d5b81ebb7a8c3da74751ae162721e674f4bfe3b046f8cb5a208eaba90b6ce",
    "key": "R022-Q04-metis_history_bounded",
    "version": 1,
}

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
        "run is not budget-truncated (pruning-contract §8.2); rule M1 (arrival prune) is exact "
        "even on a chunk the reference caps (state_cap is a cap, not a budget)",
        "chunk-search counters (paths_*, chunks_*, incremental_*, path_split_*, chosen_source, "
        "topology, truncated_by, truncated_stages), candidates_considered, candidates_truncated, "
        "label_relaxations, label_rejected_cycle, label_pruned_distance, label_skipped_revisit, "
        "label_truncated_chunks, labels_*, chunks_state_capped, peak_*, "
        "certified_strict_insertions, termination and evaluations equal the reference's under "
        "M1 alone (§8.1)",
    ],
    "not_claimed": [
        "any speedup",
        "identical work counters (marginal_failures, marginal_incomplete, incomplete_example, "
        "quotes_executed, quotes_memoized)",
        "identical label-population counters when rule M2 is active (bound_pruning.m2.active)",
        "rule M2 outside its gate G_M2 (unproved; unsafe under a binding frontier cap, §6.4)",
        "exactness under a binding budget (labelled not_exact_budget_binding)",
        "pruning when the retained simpler candidate is absent (P0)",
        "optimality",
    ],
}


def solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    return replace(metis_history.solve(case, context, budget, bound_pruning=True), algorithm=NAME)


FACTORY = AlgorithmFactory(
    name=NAME,
    solve=solve,
    prepare=metis_history.prepare_bounded,
    capabilities=metis_history.CAPABILITIES,
    search_params=metis_history.SEARCH_PARAMS,
    graph_params=metis_history.GRAPH_PARAMS,
    provenance=PROVENANCE,
    options_validator=metis_history.validate_options,
    options_preset=MappingProxyType(PRESET),
)

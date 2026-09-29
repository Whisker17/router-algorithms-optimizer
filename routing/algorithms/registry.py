"""Explicit algorithm registry (docs/DESIGN.md §4.3: "Do not build dynamic plugin
discovery: an explicit name-to-factory mapping is sufficient."). Registered so far:
`direct` (WHI-1427; single pool), `single_path` (WHI-1438; multi-hop, no split,
requires profile `search.max_hops`) and `direct_split` (WHI-1439; direct split,
requires `search.max_splits` and `search.percent_step`) and `path_split` (WHI-1440;
multi-hop pool-disjoint split, requires `search.max_hops`, `search.max_splits` and
`search.percent_step`) and `incremental_graph` (WHI-1441; shared-pool chunked graph
heuristic, requires those three plus profile `graph.chunks`) and `uni_sor_port`
(WHI-1444; the parity-tested Uniswap SOR routing-core port, V2/V3 cohort only, requires
`search.max_hops`, `search.max_splits` and `search.percent_step`). Each further mandatory
algorithm (docs/DESIGN.md §2.6) adds one `AlgorithmFactory` entry here, with its
`Capabilities` and the `search.*` keys it requires.

`uni_sor_fast` (WHI-1508) is registered as a seventh, **opt-in experimental** identity: a
candidate-shortlist heuristic over `uni_sor_port`'s core that also requires the
`shortlist.*` settings. Registration only makes it selectable by a profile that names it;
no existing profile lists it and it never replaces `uni_sor_port`.

The two **named optimized strategies** (WHI-1528) follow: `uni_sor_adaptive` (L08 arm H3)
and `uni_sor_optimized` (L08 arm H4), thin adapters that run the unchanged `uni_sor_fast`
solve with their registered recipe settings (`routing/algorithms/uni_sor_strategies.py`).
`BASE_STRATEGIES` / `OPTIMIZED_STRATEGIES` name the two comparison groups the CLI selects
with `--strategies`; `uni_sor_fast` is in neither (a profile-selected custom experiment).

`metis_inspired` (WHI-1449) is a further **opt-in experimental** identity: the Metis-inspired
(NOT Jupiter Metis) hop-layered label search over `incremental_graph`'s chunk allocation
(docs/references/jupiter-metis-challenge.md §9.2). It requires `incremental_graph`'s keys
plus `graph.label_hops` and `graph.label_pruning`, carries its source/inference provenance,
and is in neither comparison group: only a profile that names it runs it.
"""

from __future__ import annotations

from routing.algorithms import (
    direct,
    direct_split,
    incremental_graph,
    metis_inspired,
    path_split,
    single_path,
    uni_sor_fast,
    uni_sor_port,
    uni_sor_strategies,
)
from routing.algorithms.base import AlgorithmFactory, SolveFn

__all__ = ["ALGORITHMS", "BASE_STRATEGIES", "OPTIMIZED_STRATEGIES", "SolveFn", "get_algorithm"]

ALGORITHMS: dict[str, AlgorithmFactory] = {
    direct.NAME: direct.FACTORY,
    single_path.NAME: single_path.FACTORY,
    direct_split.NAME: direct_split.FACTORY,
    path_split.NAME: path_split.FACTORY,
    incremental_graph.NAME: incremental_graph.FACTORY,
    uni_sor_port.NAME: uni_sor_port.FACTORY,
    uni_sor_fast.NAME: uni_sor_fast.FACTORY,  # opt-in experiment, not a reference
    metis_inspired.NAME: metis_inspired.FACTORY,  # opt-in experiment (WHI-1449), not a reference
    # Named optimized strategies (WHI-1528): registered recipes, not references or defaults.
    uni_sor_strategies.ADAPTIVE: uni_sor_strategies.ADAPTIVE_FACTORY,
    uni_sor_strategies.OPTIMIZED: uni_sor_strategies.OPTIMIZED_FACTORY,
}

# The comparison groups, in their deterministic run order (base first, then optimized).
BASE_STRATEGIES: tuple[str, ...] = (
    direct.NAME,
    single_path.NAME,
    direct_split.NAME,
    path_split.NAME,
    incremental_graph.NAME,
    uni_sor_port.NAME,
)
OPTIMIZED_STRATEGIES: tuple[str, ...] = (uni_sor_strategies.ADAPTIVE, uni_sor_strategies.OPTIMIZED)


def get_algorithm(name: str) -> AlgorithmFactory:
    try:
        return ALGORITHMS[name]
    except KeyError as exc:
        raise KeyError(f"unknown algorithm {name!r}; registered: {sorted(ALGORITHMS)}") from exc

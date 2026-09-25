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
"""

from __future__ import annotations

from routing.algorithms import (
    direct,
    direct_split,
    incremental_graph,
    path_split,
    single_path,
    uni_sor_port,
)
from routing.algorithms.base import AlgorithmFactory, SolveFn

__all__ = ["ALGORITHMS", "SolveFn", "get_algorithm"]

ALGORITHMS: dict[str, AlgorithmFactory] = {
    direct.NAME: direct.FACTORY,
    single_path.NAME: single_path.FACTORY,
    direct_split.NAME: direct_split.FACTORY,
    path_split.NAME: path_split.FACTORY,
    incremental_graph.NAME: incremental_graph.FACTORY,
    uni_sor_port.NAME: uni_sor_port.FACTORY,
}


def get_algorithm(name: str) -> AlgorithmFactory:
    try:
        return ALGORITHMS[name]
    except KeyError as exc:
        raise KeyError(f"unknown algorithm {name!r}; registered: {sorted(ALGORITHMS)}") from exc

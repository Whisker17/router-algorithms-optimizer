"""Plan types, fund/state evaluator, algorithm registry and implementations
(docs/DESIGN.md §4.2). `evaluate()` (`routing.evaluator`) replays split, merged and
multi-hop plans with shared physical-pool state (WHI-1427, WHI-1435). Algorithms so far:
single-pool `direct` (`routing.algorithms.direct`), multi-hop, no-split
`single_path` (`routing.algorithms.single_path`, WHI-1438), which uses the reusable
bounded path traversal in `routing.search`, and `direct_split`
(`routing.algorithms.direct_split`, WHI-1439), a discrete percentage-grid allocation
across direct pools, and `path_split` (`routing.algorithms.path_split`, WHI-1440), the
same grid over bounded multi-hop paths with physical-pool conflicts excluded, and
`incremental_graph` (`routing.algorithms.incremental_graph`, WHI-1441), a chunked
shared-pool graph heuristic whose merged plan is re-evaluated. Further
mandatory algorithms (docs/DESIGN.md §2.6) register alongside them in `routing.algorithms.registry`.
"""

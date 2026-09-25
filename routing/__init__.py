"""Plan types, fund/state evaluator, algorithm registry and implementations
(docs/DESIGN.md §4.2). `evaluate()` (`routing.evaluator`) replays split, merged and
multi-hop plans with shared physical-pool state (WHI-1427, WHI-1435). Algorithms so far:
single-pool `direct` (`routing.algorithms.direct`) and multi-hop, no-split
`single_path` (`routing.algorithms.single_path`, WHI-1438), which uses the reusable
bounded path traversal in `routing.search`. Further mandatory algorithms
(docs/DESIGN.md §2.6) register alongside them in `routing.algorithms.registry`.
"""

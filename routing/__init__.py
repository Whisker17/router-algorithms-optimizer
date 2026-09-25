"""Plan types, fund/state evaluator, algorithm registry and implementations
(docs/DESIGN.md §4.2). `evaluate()` (`routing.evaluator`) replays split, merged and
multi-hop plans with shared physical-pool state (WHI-1427, WHI-1435); only the
single-step `direct` algorithm (`routing.algorithms.direct`) exists so far, and further
mandatory algorithms (docs/DESIGN.md §2.6) register alongside it in
`routing.algorithms.registry`.
"""

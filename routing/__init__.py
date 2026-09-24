"""Plan types, fund/state evaluator, algorithm registry and implementations
(docs/DESIGN.md §4.2). Only `evaluate()` (`routing.evaluator`) and the single-step
`direct` algorithm (`routing.algorithms.direct`) exist so far (WHI-1427); further
mandatory algorithms (docs/DESIGN.md §2.6) register alongside `direct` in
`routing.algorithms.registry`.
"""

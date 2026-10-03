"""Objective/cost context, worker lifecycle, budgets, measurements and result
records (docs/DESIGN.md §4.2). `benchmark.objective.ObjectiveContext` is the
shared gross-only/synthetic-fixed-cost scoring seam (WHI-1427);
`benchmark.runner` measures every algorithm in isolated `benchmark.worker`
processes under the profile's declared budget and `benchmark.results` writes the
append-only, crash-tolerant versioned run records (WHI-1437, docs/DESIGN.md
§2.10, §4.5).
"""

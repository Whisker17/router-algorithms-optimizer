"""Objective/cost context, worker lifecycle, budgets, measurements and result
records (docs/DESIGN.md §4.2). `benchmark.objective.ObjectiveContext` is the
shared gross-only/synthetic-fixed-cost scoring seam; `benchmark.runner` and
`benchmark.results` are the simple sequential runner and versioned result writer
(WHI-1427). Hard timeout/measurement isolation belong to a later ticket
(docs/DESIGN.md §2.10).
"""

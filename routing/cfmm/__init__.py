"""Runtime helpers of the `cfmm_dual` CPMM stage (WHI-1558; contract
`docs/references/research-021/cfmm-dual.md` §§4-5).

- `routing.cfmm.model` -- admitted CPMM market universe (`simple_path_union`), immutable
  dual-problem inputs, the per-market optimal-arbitrage oracle, the dual function, the
  fee-free maximum-depth normalization and the log-price objective. Standard library only.
- `routing.cfmm.optimizer` -- the hard per-attempt `SolveBudget`, the `GuardedObjective`,
  SciPy L-BFGS-B `solve` / `resolve_restricted` with our own projected-residual
  termination, and the recorded numerical-backend provenance. NumPy/SciPy are imported
  only when a numeric solve actually needs them (`numeric_backend()`), never by importing
  this package or either module.

Numbers produced here are float64 continuous-model estimates, never money and never a
certified bound; only exact integer quotes decide a plan (recovery is not in this package).
"""

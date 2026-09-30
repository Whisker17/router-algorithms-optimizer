"""Guarded numerical solve of the `cfmm_dual` CPMM dual (WHI-1558; `cfmm-dual.md` §§5.2-5.4).

Ported from the validated WHI-1557 model (`tests/routing/cfmm_contract_model.py`
`EvaluationBudget`/`GuardedObjective`, sha256 `7fb97939...c387`) and its SciPy driver (the
offline reference harness `python_reference.py` `solve`, sha256 `d7657c56...9148`; attribution
in `routing/cfmm/NOTICE.md`), with the same
float path, so the committed `model_reference.json` points reproduce. The established
optimizer is SciPy's L-BFGS-B (`scipy.optimize.minimize(method="L-BFGS-B")`, the C
translation of the same L-BFGS-B 3.0 the paper's author code wraps); nothing here is a
hand-written optimizer.

**Hard budget (§5.3).** SciPy checks `maxfun` only between iterations and finishes a line
search past it, so `maxfun` alone overruns. One `SolveBudget` per solve attempt owns
`max_function_evaluations` and `max_iterations`; the initial solve and the restricted
re-solve (`resolve_restricted`) both draw on it and nothing is ever reset. Every fresh
Phi/gradient evaluation (|M| market-oracle calls) is charged *before* it is computed, the
final point included; the evaluation beyond the cap is refused (`EvaluationCapReached`),
which stops SciPy. An identical point is served from the per-guard cache (pure function):
counted as a cache hit, not as fresh math, but still inside the measured seconds. The
reported point is SciPy's `x` when it was evaluated (reused) or is still affordable, else
the lowest-Phi evaluated point (ties: earlier); guard state lives on the guard and the
caller-owned budget, so an exception inside SciPy cannot lose an evaluated point or a count.

**Termination (§5.4)** is our own projected residual at the reported point, never SciPy's
`success`: `converged` iff residual <= `residual_tolerance`; else `iteration_cap` if the
guard fired or SciPy stopped on `maxiter`/`maxfun` (status 1); else `not_converged`. A
non-finite or undefined evaluation is `failure="numeric_failure"` (charged, counted).

**Lazy backend.** NumPy/SciPy are imported only by `numeric_backend()` -- called by
`solve`, or explicitly by a caller that wants to charge the load to its own measured
preparation -- never at module import, so importing this module (or the registry) costs a
legacy worker nothing. The backend refuses versions other than the approved pins and records
BLAS/LAPACK vendor, thread-limit environment and platform provenance. The loop is
sequential in the calling thread; no process or thread pool is started here.
"""

from __future__ import annotations

import math
import os
import platform
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from types import MappingProxyType
from typing import Any, Literal

from routing.cfmm.model import (
    DualEval,
    DualProblem,
    NumericFailure,
    Trade,
    log_objective,
    projected_residual,
    restricted,
    scales,
)

Termination = Literal["converged", "not_converged", "iteration_cap"]
PointSource = Literal["optimizer_cached", "optimizer_charged", "best_evaluated"]

# The approved optimizer contract (`cfmm-dual.md` §2 "Port optimizer"); pinned in
# pyproject.toml/uv.lock and checked again at load time (never silently swapped).
PINNED_VERSIONS: Mapping[str, str] = MappingProxyType({"numpy": "2.5.3", "scipy": "1.18.1"})
# Native BLAS thread-pool limits applied (setdefault, before the first NumPy import) so the
# worker never starts a nested BLAS pool; an explicit environment value is kept as is.
THREAD_ENV = (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)


class NumericBackendError(RuntimeError):
    """The numerical dependencies are missing or not the approved pinned versions."""


class EvaluationCapReached(Exception):  # noqa: N818 -- a declared cap, not an error
    """Raised instead of any objective evaluation beyond `max_function_evaluations`."""


# --------------------------------------------------------------------------- settings/budget


@dataclass(frozen=True)
class SolverSettings:
    """The §9.3 numerical options (range validation is the factory's options validator;
    here only the finite/positive checks that keep the solve well defined)."""

    max_iterations: int
    max_function_evaluations: int
    lbfgs_memory: int
    pgtol: float
    ftol: float
    residual_tolerance: float
    log_price_bound: float

    def __post_init__(self) -> None:
        for name in ("max_iterations", "max_function_evaluations", "lbfgs_memory"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name}: expected an integer >= 1, got {value!r}")
        for name in ("pgtol", "ftol", "residual_tolerance", "log_price_bound"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{name}: expected a number, got {value!r}")
            if not (math.isfinite(value) and value > 0):
                raise ValueError(f"{name}: expected a finite number > 0, got {value!r}")

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> SolverSettings:
        """The numerical subset of validated `algorithm_options.cfmm_dual`."""
        return cls(
            max_iterations=options["max_iterations"],
            max_function_evaluations=options["max_function_evaluations"],
            lbfgs_memory=options["lbfgs_memory"],
            pgtol=float(options["pgtol"]),
            ftol=float(options["ftol"]),
            residual_tolerance=float(options["residual_tolerance"]),
            log_price_bound=float(options["log_price_bound"]),
        )


@dataclass
class SolveBudget:
    """The one numeric budget of a solve attempt (§5.3), shared by the initial solve and
    the restricted re-solve. `evaluations` counts charged fresh Phi/gradient evaluations
    (each = one objective and one gradient), `oracle_calls` the market-oracle calls they
    made, `cache_hits` the identical points served without new math, `iterations` the
    L-BFGS-B iterations; the solves only ever add to them."""

    max_function_evaluations: int
    max_iterations: int
    evaluations: int = 0
    oracle_calls: int = 0
    cache_hits: int = 0
    iterations: int = 0

    @classmethod
    def for_settings(cls, settings: SolverSettings) -> SolveBudget:
        return cls(settings.max_function_evaluations, settings.max_iterations)

    @property
    def remaining_evaluations(self) -> int:
        return self.max_function_evaluations - self.evaluations

    @property
    def remaining_iterations(self) -> int:
        return self.max_iterations - self.iterations


# --------------------------------------------------------------------------- guard


class GuardedObjective:
    """Phi and its gradient with the hard evaluation guard (§5.3). The per-guard cache
    serves identical points uncharged; the guard owns the best evaluated point, so it
    survives any exception that unwinds the optimizer."""

    def __init__(self, problem: DualProblem, sigma: Mapping[str, float], budget: SolveBudget):
        self.problem, self.sigma, self.budget = problem, sigma, budget
        self.cache: dict[tuple[float, ...], tuple[float, list[float], DualEval]] = {}
        self.best: tuple[float, ...] | None = None
        self.evaluations = 0
        self.oracle_calls = 0
        self.cache_hits = 0
        self.fired = False

    def __call__(self, x: Sequence[float]) -> tuple[float, list[float]]:
        key = tuple(float(t) for t in x)
        hit = self.cache.get(key)
        if hit is not None:
            self.cache_hits += 1
            self.budget.cache_hits += 1
            return hit[0], hit[1]
        if self.budget.remaining_evaluations <= 0:
            self.fired = True
            raise EvaluationCapReached(
                f"max_function_evaluations {self.budget.max_function_evaluations} reached"
            )
        self.budget.evaluations += 1  # charged before the math is done
        self.evaluations += 1
        try:
            phi, grad, ev = log_objective(self.problem, self.sigma, key)
        except NumericFailure as exc:
            self._calls(exc.oracle_calls)
            raise
        self._calls(ev.oracle_calls)
        if not math.isfinite(phi) or not all(math.isfinite(g) for g in grad):
            raise NumericFailure("non-finite dual value or gradient", ev.oracle_calls)
        self.cache[key] = (phi, grad, ev)
        if self.best is None or phi < self.cache[self.best][0]:
            self.best = key  # ties keep the earlier point
        return phi, grad

    def _calls(self, n: int) -> None:
        self.oracle_calls += n
        self.budget.oracle_calls += n

    def final(
        self, x: Sequence[float] | None
    ) -> tuple[tuple[float, ...], float, list[float], DualEval, PointSource]:
        """The reported point: the optimizer's `x` when given and already evaluated
        (reused) or still affordable (charged once); otherwise the lowest-Phi evaluated
        point. Raises `EvaluationCapReached` when nothing was ever evaluated."""
        if x is not None:
            key = tuple(float(t) for t in x)
            cached = key in self.cache
            if cached or self.budget.remaining_evaluations > 0:
                self(key)
                phi, grad, ev = self.cache[key]
                return key, phi, grad, ev, "optimizer_cached" if cached else "optimizer_charged"
        if self.best is None:
            raise EvaluationCapReached("no evaluation was affordable")
        phi, grad, ev = self.cache[self.best]
        return self.best, phi, grad, ev, "best_evaluated"


# --------------------------------------------------------------------------- backend


@dataclass(frozen=True)
class NumericBackend:
    """The loaded optimizer and its recorded provenance (JSON-serializable)."""

    minimize: Callable[..., Any]
    array: Callable[..., Any]
    provenance: Mapping[str, Any]


@cache
def numeric_backend() -> NumericBackend:
    """Import NumPy/SciPy once per process (the first call pays and records the import
    seconds; later calls are free), refuse unpinned versions, and record provenance."""
    already = sorted(m for m in ("numpy", "scipy") if m in sys.modules)
    thread_env_set = []
    if "numpy" not in sys.modules:
        for name in THREAD_ENV:
            if name not in os.environ:
                os.environ[name] = "1"
                thread_env_set.append(name)
    threads_before = threading.active_count()
    t0 = time.perf_counter()
    try:
        import numpy
        import scipy  # type: ignore[import-untyped]
        from scipy.optimize import minimize  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover -- an environment blocker, not a fallback
        raise NumericBackendError(f"numerical dependencies unavailable: {exc}") from exc
    seconds = time.perf_counter() - t0
    versions = {"numpy": numpy.__version__, "scipy": scipy.__version__}
    if versions != dict(PINNED_VERSIONS):
        raise NumericBackendError(f"expected {dict(PINNED_VERSIONS)}, found {versions}")
    deps = {
        "numpy": numpy.show_config(mode="dicts")["Build Dependencies"],
        "scipy": scipy.show_config(mode="dicts")["Build Dependencies"],
    }
    provenance = {
        "optimizer": "scipy.optimize.minimize(method='L-BFGS-B')",
        "versions": versions,
        "blas": {
            k: {"blas": v["blas"]["name"], "lapack": v["lapack"]["name"]} for k, v in deps.items()
        },
        "thread_env": {name: os.environ.get(name) for name in THREAD_ENV},
        "thread_env_set_by_cfmm": thread_env_set,
        "already_imported": already,
        "python_threads": {"before": threads_before, "after": threading.active_count()},
        "import_seconds": seconds,
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "system": platform.system(),
        "machine": platform.machine(),
        "platform": platform.platform(),
    }
    return NumericBackend(minimize, numpy.asarray, MappingProxyType(provenance))


# --------------------------------------------------------------------------- solve


@dataclass(frozen=True)
class EvaluatedPoint:
    """A point the guard actually evaluated: x (log-price offsets), nu (every variable;
    nu_out = 1 is implicit), Phi, g(nu), dPhi/dx, the projected residual on the box and
    the oracle trades there. Float estimates, never money."""

    x: tuple[float, ...]
    nu: Mapping[str, float]
    phi: float
    value: float
    gradient: tuple[float, ...]
    residual: float
    trades: tuple[Trade, ...]
    source: PointSource


@dataclass(frozen=True)
class NumericSolution:
    """One guarded L-BFGS-B solve (initial or restricted). `failure` is `numeric_failure`
    (then `termination` is `None`; `point`, if any, is the best finite point evaluated
    before the failure, for diagnostics only). `evaluations`/`oracle_calls`/`cache_hits`/
    `iterations` are this solve's own share of the shared `SolveBudget`; `scipy_*` are
    SciPy's own reports (its `nfev` also counts cache-served repeats; `None` when our
    guard or a numeric failure stopped SciPy). `seconds` covers the whole solve, cache
    hits included."""

    problem: DualProblem
    sigma: Mapping[str, float]
    point: EvaluatedPoint | None
    termination: Termination | None
    failure: Literal["numeric_failure"] | None
    evaluations: int
    oracle_calls: int
    cache_hits: int
    iterations: int
    guard_fired: bool
    scipy_status: int | None
    scipy_message: str
    scipy_nit: int | None
    scipy_nfev: int | None
    warm_started: bool
    seconds: float
    error: str | None = None
    budget_after: Mapping[str, int] = field(default_factory=dict)

    @property
    def trades(self) -> tuple[Trade, ...] | None:
        """The trades recovery may use: `None` on a numeric failure."""
        return None if self.failure is not None or self.point is None else self.point.trades

    @property
    def estimate(self) -> float | None:
        """g(nu) of the reported point iff `converged` (§7; the caller still drops it for a
        restricted re-solve or when a fallback was used). Float, never a bound."""
        if self.termination != "converged" or self.point is None:
            return None
        return self.point.value


def _start(
    problem: DualProblem, sigma: Mapping[str, float], bound: float, warm: Mapping[str, float] | None
) -> list[float]:
    """x0: 0 (spot) for a variable without a warm price, else log(warm/sigma) clamped into
    the box. A warm price must be a finite positive real number (not a bool)."""
    x0 = []
    for v in problem.variables:
        if warm is None or v not in warm:
            x0.append(0.0)
            continue
        w = warm[v]
        try:
            ok = not isinstance(w, bool) and isinstance(w, int | float) and 0 < float(w) < math.inf
        except OverflowError:  # an int beyond float range
            ok = False
        if not ok:
            raise ValueError(f"warm price of {v!r} must be finite and > 0, got {w!r}")
        x0.append(min(max(_log_ratio(float(w), sigma[v]), -bound), bound))
    return x0


def _log_ratio(w: float, s: float) -> float:
    """log(w/s) for finite w, s > 0. The ordinary quotient is used whenever it is a finite
    positive float (the path the pinned reference points took); when it under- or
    overflows (e.g. a subnormal warm price over a scale > 1) the difference of logs is
    exact enough and always finite."""
    ratio = w / s
    if 0.0 < ratio < math.inf:
        return math.log(ratio)
    return math.log(w) - math.log(s)


def solve(
    problem: DualProblem,
    settings: SolverSettings,
    budget: SolveBudget,
    warm: Mapping[str, float] | None = None,
) -> NumericSolution:
    """§5: x_j = log(nu_j/sigma_j), x0 = 0 (spot prices) or the clamped `warm` start, box
    |x_j| <= `log_price_bound`, L-BFGS-B on Phi under the shared hard `budget` (at least
    one evaluation and one iteration must remain). Termination per §5.4 from our own
    projected residual; never inferred from SciPy's success."""
    if budget.remaining_evaluations < 1 or budget.remaining_iterations < 1:
        raise ValueError("solve needs at least one evaluation and one iteration left")
    backend = numeric_backend()
    t0 = time.perf_counter()
    bound = settings.log_price_bound
    iterations = 0
    status: int | None = None
    message, nit, nfev = "", None, None
    x_ret: list[float] | None = None
    failed: NumericFailure | None = None
    try:
        sigma = scales(problem)
        if not all(math.isfinite(s) and s > 0 for s in sigma.values()):
            raise NumericFailure(f"non-finite or zero scale: {dict(sigma)}", 0)
    except NumericFailure as exc:
        sigma, failed, message = MappingProxyType({}), exc, f"NUMERIC: {exc}"
    guard = GuardedObjective(problem, sigma, budget)

    def fun(x: Any) -> tuple[float, Any]:
        phi, grad = guard(x.tolist())
        return phi, backend.array(grad, dtype=float)

    def count(intermediate_result: Any) -> None:  # SciPy: one call per iteration
        nonlocal iterations
        iterations += 1

    if failed is None:
        try:
            x0 = _start(problem, sigma, bound, warm)
            res = backend.minimize(
                fun,
                backend.array(x0, dtype=float),
                jac=True,
                method="L-BFGS-B",
                bounds=[(-bound, bound)] * len(x0),
                callback=count,
                options={
                    "maxcor": settings.lbfgs_memory,
                    "ftol": settings.ftol,
                    "gtol": settings.pgtol,
                    "maxiter": budget.remaining_iterations,
                    "maxfun": budget.remaining_evaluations,
                },
            )
            x_ret = [float(t) for t in res.x]
            status, message = int(res.status), str(res.message)
            nit, nfev = int(res.nit), int(res.nfev)
            iterations = nit
        except EvaluationCapReached:
            message = "GUARD: evaluation cap"
        except NumericFailure as exc:
            failed, message = exc, f"NUMERIC: {exc}"
    budget.iterations += iterations
    if failed is None:
        try:
            x, phi, grad, ev, source = guard.final(x_ret)
        except NumericFailure as exc:
            failed, message = exc, f"NUMERIC: {exc}"
    if failed is not None:
        point = _best(guard, problem, sigma, bound)
        return _solution(problem, sigma, point, None, guard, iterations, status, message,
                         nit, nfev, warm, t0, budget, str(failed))  # fmt: skip
    residual = projected_residual(x, grad, -bound, bound)
    termination: Termination
    if residual <= settings.residual_tolerance:
        termination = "converged"
    elif guard.fired or status == 1:
        termination = "iteration_cap"
    else:
        termination = "not_converged"
    point = _point(problem, sigma, x, phi, grad, ev, residual, source)
    return _solution(problem, sigma, point, termination, guard, iterations, status, message,
                     nit, nfev, warm, t0, budget, None)  # fmt: skip


def _point(
    problem: DualProblem,
    sigma: Mapping[str, float],
    x: tuple[float, ...],
    phi: float,
    grad: Sequence[float],
    ev: DualEval,
    residual: float,
    source: PointSource,
) -> EvaluatedPoint:
    nu = {v: sigma[v] * math.exp(t) for v, t in zip(problem.variables, x, strict=True)}
    return EvaluatedPoint(
        x, MappingProxyType(nu), phi, ev.value, tuple(grad), residual, ev.trades, source
    )


def _best(
    guard: GuardedObjective, problem: DualProblem, sigma: Mapping[str, float], bound: float
) -> EvaluatedPoint | None:
    if guard.best is None:
        return None
    phi, grad, ev = guard.cache[guard.best]
    residual = projected_residual(guard.best, grad, -bound, bound)
    return _point(problem, sigma, guard.best, phi, grad, ev, residual, "best_evaluated")


def _solution(
    problem: DualProblem,
    sigma: Mapping[str, float],
    point: EvaluatedPoint | None,
    termination: Termination | None,
    guard: GuardedObjective,
    iterations: int,
    status: int | None,
    message: str,
    nit: int | None,
    nfev: int | None,
    warm: Mapping[str, float] | None,
    t0: float,
    budget: SolveBudget,
    error: str | None,
) -> NumericSolution:
    return NumericSolution(
        problem=problem,
        sigma=sigma,
        point=point,
        termination=termination,
        failure=None if error is None else "numeric_failure",
        evaluations=guard.evaluations,
        oracle_calls=guard.oracle_calls,
        cache_hits=guard.cache_hits,
        iterations=iterations,
        guard_fired=guard.fired,
        scipy_status=status,
        scipy_message=message,
        scipy_nit=nit,
        scipy_nfev=nfev,
        warm_started=warm is not None,
        seconds=time.perf_counter() - t0,
        error=error,
        budget_after=MappingProxyType(
            {
                "evaluations": budget.evaluations,
                "oracle_calls": budget.oracle_calls,
                "cache_hits": budget.cache_hits,
                "iterations": budget.iterations,
                "remaining_evaluations": budget.remaining_evaluations,
                "remaining_iterations": budget.remaining_iterations,
            }
        ),
    )


def resolve_restricted(
    problem: DualProblem,
    allowed: Mapping[str, str],
    settings: SolverSettings,
    budget: SolveBudget,
    warm: Mapping[str, float],
) -> NumericSolution | None:
    """§6 step 3 bounded re-solve: the markets of `allowed` with fixed directions,
    warm-started from `warm` (the initial solve's nu), on the **remaining** evaluations
    and iterations of the same `budget`. `None` = skipped because nothing is left
    (`resolve_skipped`; the budget is never re-funded)."""
    if budget.remaining_evaluations < 1 or budget.remaining_iterations < 1:
        return None
    return solve(restricted(problem, allowed), settings, budget, warm)

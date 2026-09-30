"""WHI-1558 component A: guarded L-BFGS-B solve and hard per-attempt budget of `cfmm_dual`
(`routing.cfmm.optimizer`, `cfmm-dual.md` §§5.3-5.4, G-C2/G-C3b).

Independent expectations: the committed Python model reference
(`tests/fixtures/cfmm/model_reference.json`, WHI-1557, SciPy 1.18.1 / NumPy 2.5.3, NOT author
execution), the pinned author run (`author_reference.json`) and counters taken by wrappers
around the real oracle and the real SciPy callback -- never the implementation's own counts
alone. The WHI-1557 research model is not imported.
"""

from __future__ import annotations

import dataclasses
import json
import math
import platform
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from test_cfmm_model import author_network, bundle_of, cp, rel

import routing.cfmm.model as cm
import routing.cfmm.optimizer as co
from snapshot.bundle import load_bundle

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "cfmm"
MODEL = json.loads((FIX / "model_reference.json").read_text())
AUTHOR = json.loads((FIX / "author_reference.json").read_text())
INPUTS = json.loads((FIX / "author_inputs.json").read_text())
PRESET: dict[str, Any] = MODEL["preset"]
SETTINGS = co.SolverSettings.from_options(PRESET)
ENV = MODEL["environment"]
# Float paths are bitwise reproducible only on the fixture's platform with the pinned
# wheels (§9.4); elsewhere the stated tolerances and invariants still apply.
SAME_PLATFORM = (platform.system(), platform.machine(), platform.python_version()) == (
    ENV["system"],
    ENV["machine"],
    ENV["python"],
)
TERMINATIONS = {"converged", "not_converged", "iteration_cap"}


def settings(**over: Any) -> co.SolverSettings:
    return dataclasses.replace(SETTINGS, **over)


def reference_problem(doc: dict[str, Any]) -> cm.DualProblem:
    if doc.get("universe") == "mantle_mixed_cpmm_h3":
        bundle = load_bundle(ROOT / "tests" / "fixtures" / "routing" / "mantle_mixed")
        case = bundle.case(doc["id"])
        assert cm.market_universe(bundle, case, 3) == tuple(doc["markets"])
    else:
        cid = (
            (doc.get("case") or doc["id"])
            .removesuffix("-maxiter2")
            .removesuffix("-resolve-starved")
        )
        bundle, case, _ = author_network(cid)
    return cm.dual_problem(bundle, case, doc["markets"])


class Counters:
    """Independent counts: every real `cpmm_arb` call and every SciPy objective callback."""

    def __init__(self) -> None:
        self.oracle = 0
        self.callbacks = 0


@pytest.fixture
def counters(monkeypatch: pytest.MonkeyPatch) -> Iterator[Counters]:
    seen = Counters()
    real_arb = cm.cpmm_arb

    def arb(*args: Any) -> Any:
        seen.oracle += 1
        return real_arb(*args)

    backend = co.numeric_backend()

    def minimize(fun: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        def counted(x: Any) -> Any:
            seen.callbacks += 1
            return fun(x)

        return backend.minimize(counted, *args, **kwargs)

    monkeypatch.setattr(cm, "cpmm_arb", arb)
    monkeypatch.setattr(
        co, "numeric_backend", lambda: dataclasses.replace(backend, minimize=minimize)
    )
    yield seen


def assert_consistent(sol: co.NumericSolution, tolerance: float, bound: float) -> None:
    """§5.4 classification re-derived from the reported point, never from SciPy."""
    assert sol.point is not None and sol.failure is None
    problem = sol.problem
    _, grad, ev = cm.log_objective(problem, sol.sigma, sol.point.x)
    residual = cm.projected_residual(sol.point.x, grad, -bound, bound)
    assert residual == sol.point.residual and ev.value == sol.point.value
    if residual <= tolerance:
        assert sol.termination == "converged" and sol.estimate == sol.point.value
    else:
        assert sol.estimate is None
        expected = "iteration_cap" if sol.guard_fired or sol.scipy_status == 1 else "not_converged"
        assert sol.termination == expected


# ------------------------------------------------------------------ G-C2 model reference


@pytest.mark.parametrize("doc", MODEL["cases"], ids=lambda c: c["id"])
def test_initial_solve_reproduces_the_model_reference(
    doc: dict[str, Any], counters: Counters
) -> None:
    """Each stored dual point: value 1e-9, sigma, termination class, plus (pinned platform)
    the identical point and work counts. The evaluations are the actual oracle work
    (oracle calls counted independently = evaluations x |M|), not SciPy's `nfev`."""
    problem = reference_problem(doc)
    ref = doc["solution"]
    cap = 2 if doc["id"].endswith("-maxiter2") else PRESET["max_iterations"]
    budget = co.SolveBudget.for_settings(settings(max_iterations=cap))
    sol = co.solve(problem, settings(max_iterations=cap), budget)
    assert sol.point is not None and sol.termination == ref["termination"]
    assert rel(sol.point.value, ref["value"]) < 1e-9
    assert dict(sol.sigma) == pytest.approx(ref["sigma"], rel=1e-15)
    assert counters.oracle == sol.oracle_calls == sol.evaluations * len(problem.markets)
    assert counters.callbacks == sol.scipy_nfev  # SciPy's count includes cache hits ...
    assert sol.evaluations + sol.cache_hits == counters.callbacks + 1  # ... + final reuse
    assert_consistent(sol, PRESET["residual_tolerance"], PRESET["log_price_bound"])
    assert budget.evaluations == sol.evaluations <= PRESET["max_function_evaluations"]
    assert sol.scipy_nit is not None
    assert budget.iterations == sol.iterations == sol.scipy_nit <= cap
    if SAME_PLATFORM:
        assert list(sol.point.x) == ref["u"] and sol.point.value == ref["value"]
        assert (sol.iterations, sol.evaluations, sol.scipy_nfev) == (
            ref["nit"],
            ref["evaluations"],
            ref["scipy_nfev"],
        )


@pytest.mark.parametrize("cid", ["r-grid38", "r-triangle", "r-cycle", "r-deadend"])
def test_optimum_matches_the_author_where_the_author_converged(cid: str) -> None:
    bundle, case, doc = author_network(cid)
    sol = co.solve(
        cm.dual_problem(bundle, case, tuple(bundle.pools)),
        SETTINGS,
        co.SolveBudget.for_settings(SETTINGS),
    )
    ref = next(c for c in AUTHOR["router"] if c["id"] == cid)
    v_out = ref["v"][doc["tokens"].index(case.token_out)]
    assert sol.termination == "converged" and sol.estimate is not None
    assert rel(sol.estimate, ref["dual_value"] / v_out) < 1e-6


def test_real_moe_decimals_converge_and_dust_is_not_converged() -> None:
    """The real 6/6/18-decimal Moe triangle converges in log-price space (the author's
    raw-unit run did not); a 1-raw-unit order against the same deep pools meets the float
    noise floor and is reported `not_converged` (no estimate), never as success."""
    bundle, case, _ = author_network("r-moe-usdc-usdt")
    problem = cm.dual_problem(bundle, case, tuple(bundle.pools))
    assert co.solve(problem, SETTINGS, co.SolveBudget.for_settings(SETTINGS)).termination == (
        "converged"
    )
    dust = cm.dual_problem(bundle, dataclasses.replace(case, amount_in=1), tuple(bundle.pools))
    sol = co.solve(dust, SETTINGS, co.SolveBudget.for_settings(SETTINGS))
    assert sol.termination == "not_converged" and sol.estimate is None and sol.failure is None
    assert_consistent(sol, PRESET["residual_tolerance"], PRESET["log_price_bound"])


@pytest.mark.parametrize("amount", [1, 2, 10**6, 10**30])
@pytest.mark.parametrize("cid", ["r-grid38", "r-triangle", "r-cycle", "r-deadend", "r-tiny"])
def test_tiny_and_large_orders_stay_finite_and_classified(cid: str, amount: int) -> None:
    bundle, case, _ = author_network(cid)
    problem = cm.dual_problem(
        bundle, dataclasses.replace(case, amount_in=amount), tuple(bundle.pools)
    )
    budget = co.SolveBudget.for_settings(SETTINGS)
    sol = co.solve(problem, SETTINGS, budget)
    assert sol.termination in TERMINATIONS and sol.point is not None
    assert all(math.isfinite(v) for v in (sol.point.value, sol.point.residual, *sol.point.x))
    assert all(abs(x) <= PRESET["log_price_bound"] for x in sol.point.x)
    assert_consistent(sol, PRESET["residual_tolerance"], PRESET["log_price_bound"])
    assert budget.evaluations <= PRESET["max_function_evaluations"]


def test_solves_are_deterministic() -> None:
    bundle, case, _ = author_network("r-cycle")
    problem = cm.dual_problem(bundle, case, tuple(bundle.pools))
    runs = [co.solve(problem, SETTINGS, co.SolveBudget.for_settings(SETTINGS)) for _ in range(2)]
    first, second = (dataclasses.replace(r, seconds=0.0) for r in runs)
    assert first == second


# ------------------------------------------------------------------ G-C3b hard budget


@pytest.mark.parametrize("cid", ["r-grid38", "r-triangle", "r-cycle", "r-deadend"])
def test_scipy_maxfun_alone_overruns_and_the_guard_does_not(cid: str, counters: Counters) -> None:
    """The trap: SciPy with `maxfun=1` finishes its line search and calls the objective
    3-4 times. The guarded solve with `max_function_evaluations` 1 does exactly one
    evaluation (|M| oracle calls) and reports that evaluated point."""
    bundle, case, _ = author_network(cid)
    problem = cm.dual_problem(bundle, case, tuple(bundle.pools))
    sigma = cm.scales(problem)
    backend = co.numeric_backend()
    unguarded = 0

    def plain(x: Any) -> tuple[float, Any]:
        nonlocal unguarded
        unguarded += 1
        phi, grad, _ = cm.log_objective(problem, sigma, x.tolist())
        return phi, backend.array(grad)

    n = len(problem.variables)
    backend.minimize(
        plain, backend.array([0.0] * n), jac=True, method="L-BFGS-B",
        bounds=[(-50.0, 50.0)] * n, options={"maxfun": 1, "maxcor": 10, "ftol": 1e-15},
    )  # fmt: skip
    assert unguarded > 1
    counters.oracle = 0
    budget = co.SolveBudget.for_settings(settings(max_function_evaluations=1))
    sol = co.solve(problem, settings(max_function_evaluations=1), budget)
    assert sol.guard_fired and sol.evaluations == budget.evaluations == 1
    assert counters.oracle == sol.oracle_calls == len(problem.markets)
    assert sol.point is not None and sol.point.source == "best_evaluated"
    assert sol.point.x == (0.0,) * n and sol.termination == "iteration_cap"
    assert sol.scipy_nfev is None and sol.scipy_message == "GUARD: evaluation cap"


@pytest.mark.parametrize(
    "run", MODEL["forced_caps"]["runs"], ids=lambda r: f"{r['case']}-{r['cap_key']}-{r['cap']}"
)
def test_forced_caps_never_overrun(run: dict[str, Any], counters: Counters) -> None:
    """Caps 1/2/3/5 on `max_function_evaluations` and 1/2 on `max_iterations`, four
    networks: charged evaluations (final point included) <= cap, oracle calls counted by
    the wrapper = evaluations x |M|, iterations <= cap, the stored termination and
    (pinned platform) the stored point and counts."""
    problem = reference_problem(run)
    s = settings(**{run["cap_key"]: run["cap"]})
    budget = co.SolveBudget.for_settings(s)
    sol = co.solve(problem, s, budget)
    assert sol.evaluations == budget.evaluations <= s.max_function_evaluations
    assert counters.oracle == sol.oracle_calls == sol.evaluations * len(problem.markets)
    assert budget.iterations == sol.iterations <= s.max_iterations
    assert sol.termination == run["termination"] and sol.guard_fired == run["guard_fired"]
    assert sol.scipy_status == run["scipy_status"]
    if run["cap_key"] == "max_function_evaluations":
        # SciPy asked for one more point than the cap; the guard refused it (not charged)
        assert sol.guard_fired and sol.evaluations == run["cap"]
        assert counters.callbacks == sol.evaluations + sol.cache_hits + 1
    else:
        assert sol.scipy_status == 1 and sol.iterations == run["cap"]
        assert counters.callbacks == sol.scipy_nfev
    assert_consistent(sol, s.residual_tolerance, s.log_price_bound)
    if SAME_PLATFORM:
        assert list(sol.point.x) == run["u"]  # type: ignore[union-attr]
        assert (sol.evaluations, sol.iterations) == (run["evaluations"], run["nit"])


def test_guard_charges_before_math_caches_repeats_and_keeps_the_best_point() -> None:
    bundle, case, _ = author_network("r-grid38")
    problem = cm.dual_problem(bundle, case, tuple(bundle.pools))
    budget = co.SolveBudget(max_function_evaluations=3, max_iterations=10)
    guard = co.GuardedObjective(problem, cm.scales(problem), budget)
    seen = [guard([0.0])[0], guard([0.0])[0], guard([0.1])[0], guard([0.05])[0]]
    assert (budget.evaluations, budget.cache_hits, budget.oracle_calls) == (3, 1, 6)
    with pytest.raises(co.EvaluationCapReached):
        guard([0.02])
    assert guard.fired and budget.evaluations == 3  # refused, not charged
    x, phi, _, _, source = guard.final([0.02])  # unaffordable -> the best evaluated point
    assert source == "best_evaluated" and phi == min(seen) and x in ((0.0,), (0.1,), (0.05,))
    x, _, _, _, source = guard.final([0.1])  # evaluated -> reused, uncharged
    assert (x, source, budget.evaluations) == ((0.1,), "optimizer_cached", 3)
    roomy = co.SolveBudget(max_function_evaluations=5, max_iterations=10)
    fresh = co.GuardedObjective(problem, cm.scales(problem), roomy)
    fresh([0.0])
    _, _, _, _, source = fresh.final([0.3])  # affordable and new -> charged exactly once
    assert source == "optimizer_charged" and roomy.evaluations == 2
    empty = co.GuardedObjective(problem, cm.scales(problem), budget)
    with pytest.raises(co.EvaluationCapReached):
        empty.final(None)


def test_best_point_ties_keep_the_earlier_point() -> None:
    """Two trade-free points of a two-token network have the same Phi (it depends on
    x_in only): the earlier stays the best."""
    bundle, case = bundle_of(
        cp("sm", "S", "M", 10**6, 10**6), cp("mt", "M", "T", 10**6, 10**6), amount=10
    )
    problem = cm.dual_problem(bundle, case, ["sm", "mt"])
    guard = co.GuardedObjective(problem, cm.scales(problem), co.SolveBudget(5, 5))
    a, b = guard([0.0, 0.0001]), guard([0.0, -0.0001])
    assert a[0] == b[0] and guard.best == (0.0, 0.0001)


# ------------------------------------------------------------------ shared re-solve budget


def _cycle() -> tuple[cm.DualProblem, dict[str, str], dict[str, Any]]:
    doc = next(c for c in MODEL["cases"] if c["id"] == "r-cycle")
    problem = reference_problem(doc)
    trades = {t["pool_id"]: t["token_in"] for t in doc["solution"]["trades"]}
    allowed = {pid: trades[pid] for pid in doc["resolves"][0]["markets"]}  # ab2 removed
    return problem, allowed, doc


def test_resolve_shares_the_one_budget_and_reproduces_the_reference(counters: Counters) -> None:
    problem, allowed, doc = _cycle()
    budget = co.SolveBudget.for_settings(SETTINGS)
    first = co.solve(problem, SETTINGS, budget)
    assert first.point is not None
    second = co.resolve_restricted(problem, allowed, SETTINGS, budget, first.point.nu)
    assert second is not None and second.point is not None and second.warm_started
    ref = doc["resolves"][0]
    assert second.problem.market_ids == tuple(ref["markets"])
    assert second.termination == ref["termination"] and rel(second.point.value, ref["value"]) < 1e-9
    assert all(t.token_in == allowed[t.pool_id] for t in second.point.trades)
    assert budget.evaluations == first.evaluations + second.evaluations
    assert budget.oracle_calls == counters.oracle == doc["budget"]["oracle_calls"]
    assert budget.evaluations == doc["budget"]["evaluations_used"]
    assert budget.remaining_iterations == doc["budget"]["iterations_left"]


def test_starved_resolve_is_skipped_never_refunded() -> None:
    """Evaluation cap = what the initial solve used (the stored `resolve_starved` run),
    or iteration cap = its iterations: the re-solve is skipped and nothing is reset."""
    problem, allowed, _ = _cycle()
    starved = MODEL["forced_caps"]["resolve_starved"]
    s = settings(max_function_evaluations=starved["cap"])
    budget = co.SolveBudget.for_settings(s)
    first = co.solve(problem, s, budget)
    assert first.point is not None and budget.remaining_evaluations == 0
    before = dataclasses.asdict(budget)
    assert co.resolve_restricted(problem, allowed, s, budget, first.point.nu) is None
    assert dataclasses.asdict(budget) == before
    assert budget.evaluations == starved["budget_used"]
    s = settings(max_iterations=first.iterations)
    budget = co.SolveBudget.for_settings(s)
    first = co.solve(problem, s, budget)
    assert first.point is not None and budget.remaining_iterations == 0
    assert co.resolve_restricted(problem, allowed, s, budget, first.point.nu) is None
    with pytest.raises(ValueError, match="at least one evaluation"):
        co.solve(problem, s, budget)


@pytest.mark.parametrize("left", [1, 2, 3])
def test_resolve_gets_only_the_remaining_evaluations(left: int, counters: Counters) -> None:
    problem, allowed, _ = _cycle()
    full = co.solve(problem, SETTINGS, co.SolveBudget.for_settings(SETTINGS))
    s = settings(max_function_evaluations=full.evaluations + left)
    budget = co.SolveBudget.for_settings(s)
    first = co.solve(problem, s, budget)
    assert first.point is not None
    counters.oracle = 0
    second = co.resolve_restricted(problem, allowed, s, budget, first.point.nu)
    assert second is not None and second.guard_fired and second.evaluations == left
    assert budget.evaluations == s.max_function_evaluations  # exactly the cap, never more
    assert counters.oracle == second.oracle_calls == left * len(allowed)
    assert second.termination in TERMINATIONS and second.point is not None


def test_resolve_gets_only_the_remaining_iterations() -> None:
    problem, allowed, _ = _cycle()
    full = co.solve(problem, SETTINGS, co.SolveBudget.for_settings(SETTINGS))
    s = settings(max_iterations=full.iterations + 1)
    budget = co.SolveBudget.for_settings(s)
    first = co.solve(problem, s, budget)
    assert first.point is not None
    second = co.resolve_restricted(problem, allowed, s, budget, first.point.nu)
    assert second is not None and second.iterations == 1 and budget.remaining_iterations == 0
    assert second.termination == "iteration_cap" and second.scipy_status == 1


# ------------------------------------------------------------------ numeric failure


def _poisoned(monkeypatch: pytest.MonkeyPatch, at: int, mode: str) -> Counters:
    seen = Counters()
    real_arb = cm.cpmm_arb

    def arb(*args: Any) -> Any:
        seen.oracle += 1
        if seen.oracle >= at:
            if mode == "raise":
                raise ZeroDivisionError("poisoned oracle")
            return cm.Trade(args[0].pool_id, args[0].token0, args[0].token1, math.nan, 1.0)
        return real_arb(*args)

    monkeypatch.setattr(cm, "cpmm_arb", arb)
    return seen


@pytest.mark.parametrize("mode", ["nan", "raise"])
def test_numeric_failure_is_charged_counted_and_keeps_the_evaluated_point(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 3rd evaluation of a two-market problem fails (a NaN trade or an arithmetic
    error in its first oracle call): the failure is `numeric_failure`, the failing
    evaluation is charged, the oracle calls are exactly those made, and the best of the
    two finite points survives for diagnostics (no trades for recovery)."""
    seen = _poisoned(monkeypatch, 5, mode)
    bundle, case, _ = author_network("r-grid38")
    problem = cm.dual_problem(bundle, case, tuple(bundle.pools))
    budget = co.SolveBudget.for_settings(SETTINGS)
    sol = co.solve(problem, SETTINGS, budget)
    assert sol.failure == "numeric_failure" and sol.termination is None and sol.error
    assert sol.trades is None and sol.estimate is None
    assert sol.evaluations == budget.evaluations == 3
    assert seen.oracle == sol.oracle_calls == budget.oracle_calls == (6 if mode == "nan" else 5)
    assert sol.point is not None and sol.point.source == "best_evaluated"
    monkeypatch.undo()
    phi0, _, _ = cm.log_objective(problem, sol.sigma, [0.0])  # the first evaluated point
    phi, _, ev = cm.log_objective(problem, sol.sigma, sol.point.x)
    assert (phi, ev.value) == (sol.point.phi, sol.point.value) and phi <= phi0
    assert sol.scipy_nfev is None and sol.scipy_message.startswith("NUMERIC:")


def test_unrepresentable_state_is_a_numeric_failure_without_any_evaluation() -> None:
    bundle, case = bundle_of(cp("a", "S", "T", 10**400, 10**400), amount=10)
    budget = co.SolveBudget.for_settings(SETTINGS)
    sol = co.solve(cm.dual_problem(bundle, case, ["a"]), SETTINGS, budget)
    assert sol.failure == "numeric_failure" and sol.point is None and sol.trades is None
    assert (budget.evaluations, budget.oracle_calls, budget.iterations) == (0, 0, 0)


# ------------------------------------------------------------------ settings, pins, imports


def test_settings_refuse_non_finite_and_non_integer_values() -> None:
    assert SETTINGS.max_function_evaluations == 600 and SETTINGS.ftol == 1e-15
    for over in (
        {"max_iterations": 0},
        {"max_iterations": True},
        {"lbfgs_memory": 2.0},
        {"pgtol": math.nan},
        {"residual_tolerance": 0.0},
        {"log_price_bound": math.inf},
        {"ftol": False},
    ):
        with pytest.raises(ValueError):
            settings(**over)
    with pytest.raises(ValueError, match="finite and > 0"):
        bundle, case, _ = author_network("r-grid38")
        co.solve(
            cm.dual_problem(bundle, case, tuple(bundle.pools)),
            SETTINGS,
            co.SolveBudget.for_settings(SETTINGS),
            warm={"S": -1.0},
        )


def test_dependency_pins_are_the_approved_contract() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    deps = set(project["project"]["dependencies"])
    assert {"numpy==2.5.3", "scipy==1.18.1"} <= deps
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {p["name"]: p["version"] for p in lock["package"]}
    assert {k: locked[k] for k in co.PINNED_VERSIONS} == dict(co.PINNED_VERSIONS)
    assert {k: ENV[k] for k in co.PINNED_VERSIONS} == dict(co.PINNED_VERSIONS)
    prov = co.numeric_backend().provenance
    assert prov["versions"] == dict(co.PINNED_VERSIONS)
    assert set(prov["blas"]) == {"numpy", "scipy"} and all(
        v["blas"] and v["lapack"] for v in prov["blas"].values()
    )
    assert set(prov["thread_env"]) == set(co.THREAD_ENV)
    json.dumps(dict(prov))  # JSON-serializable provenance


def test_backend_refuses_an_unpinned_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.modules["numpy"], "__version__", "0.0.0")
    with pytest.raises(co.NumericBackendError, match="expected"):
        co.numeric_backend.__wrapped__()


def test_importing_the_helpers_and_the_registry_loads_no_numerical_library() -> None:
    """Legacy workers import the registry; neither it nor the cfmm helpers may pull in
    NumPy/SciPy. The first numeric solve loads them, records provenance and limits the
    native BLAS pools; the ordinary roster is unchanged (no cfmm_dual yet)."""
    code = """
import json, sys
import main, benchmark.worker, benchmark.strategies as st
import routing.cfmm, routing.cfmm.model as cm, routing.cfmm.optimizer as co
from routing.algorithms.registry import ALGORITHMS, BASE_STRATEGIES, OPTIMIZED_STRATEGIES
loaded = sorted(m for m in sys.modules if m.split('.')[0] in ('numpy', 'scipy'))
roster = [*BASE_STRATEGIES, *OPTIMIZED_STRATEGIES, st.METIS, *st.R021_ADDITIONS]
before = {"loaded": loaded, "cfmm_registered": "cfmm_dual" in ALGORITHMS, "roster": len(roster)}
from test_cfmm_model import author_network
b, c, _ = author_network("r-grid38")
s = co.SolverSettings(200, 600, 10, 1e-9, 1e-15, 1e-5, 50.0)
sol = co.solve(cm.dual_problem(b, c, tuple(b.pools)), s, co.SolveBudget.for_settings(s))
prov = co.numeric_backend().provenance
print(json.dumps({**before, "after": "scipy.optimize" in sys.modules,
                  "termination": sol.termination, "set": prov["thread_env_set_by_cfmm"],
                  "already": prov["already_imported"]}))
"""
    env = {k: v for k, v in __import__("os").environ.items() if k not in co.THREAD_ENV}
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**env, "PYTHONPATH": f"{ROOT}:{ROOT / 'tests' / 'routing'}"},
        capture_output=True,
        text=True,
        check=True,
    )
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["loaded"] == [] and got["cfmm_registered"] is False and got["roster"] == 13
    assert got["after"] is True and got["termination"] == "converged"
    assert got["set"] == list(co.THREAD_ENV) and got["already"] == []


def test_restricted_problem_is_built_only_from_the_initial_markets() -> None:
    bundle, case = bundle_of(cp("st", "S", "T", 1000, 1000), amount=10)
    problem = cm.dual_problem(bundle, case, ["st"])
    with pytest.raises(ValueError):
        co.resolve_restricted(problem, {"xx": "S"}, SETTINGS, co.SolveBudget(5, 5), {"S": 1.0})

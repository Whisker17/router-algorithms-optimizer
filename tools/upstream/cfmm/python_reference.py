"""Python-model reference runs for WHI-1557 (validation-only; NOT author execution).

Runs this repository's executable contract model (`tests/routing/cfmm_contract_model.py`)
with the established optimizer the contract names for WHI-1558 -- SciPy's L-BFGS-B
(`scipy.optimize.minimize(method="L-BFGS-B")`) -- in an ephemeral environment. SciPy is
**not** a project dependency; nothing here is imported by the benchmark or the tests:

    uv run --with scipy==1.18.1 --with numpy==2.5.3 \
        python tools/upstream/cfmm/python_reference.py fixtures
    uv run --with scipy==1.18.1 --with numpy==2.5.3 \
        python tools/upstream/cfmm/python_reference.py tuning|sweep <corpus_dir> <out_dir>

`fixtures` writes `tests/fixtures/cfmm/model_reference.json` (dual points the offline
tests re-verify for stationarity without SciPy). `tuning` is the bounded preset probe on
the 96-case tuning split only (contract §10); it writes per-case JSON outside the repo.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import platform
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import scipy
from scipy.optimize import minimize

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "routing"))

import cfmm_contract_model as m  # noqa: E402

from benchmark.objective import gross_only  # noqa: E402
from routing.evaluator import EvalStatus, evaluate  # noqa: E402
from routing.search import QuoteCache, build_graph_index, enumerate_paths, path_plan  # noqa: E402
from snapshot.bundle import load_bundle  # noqa: E402
from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle  # noqa: E402

# The bounded preset under test (contract §9). Values are what the tuning probe checks.
PRESET: dict[str, Any] = {
    "max_iterations": 200,
    "max_function_evaluations": 600,
    "lbfgs_memory": 10,
    "pgtol": 1e-9,
    "ftol": 1e-15,
    "residual_tolerance": 1e-5,
    "log_price_bound": 50.0,
    "min_split_share": 1e-6,
    "max_recovery_attempts": 8,
    "cycle_resolve": True,
}


@dataclasses.dataclass
class Solved:
    markets: tuple[str, ...]
    variables: tuple[str, ...]
    sigma: dict[str, float]
    u: list[float]
    nu: dict[str, float]
    value: float
    residual: float
    status: int | None  # scipy warnflag; None when our guard stopped the solve
    message: str
    nit: int
    evaluations: int  # Phi/gradient evaluations actually made (guard count, incl. final)
    scipy_nfev: int | None
    guard_fired: bool
    termination: str
    trades: tuple[m.Trade, ...]
    oracle_calls: int
    seconds: float


@dataclasses.dataclass
class Budgets:
    """One numeric budget per solve attempt (§5.3), shared by the re-solve: evaluations
    (hard guard) and L-BFGS-B iterations (SciPy enforces `maxiter` exactly)."""

    evaluations: m.EvaluationBudget
    iterations_left: int


def solve(
    problem: m.DualProblem,
    opts: Mapping[str, Any],
    budgets: Budgets,
    warm: Mapping[str, float] | None = None,
) -> Solved:
    """§5: x_j = log(nu_j / sigma_j), x0 = 0 (spot prices) or a warm start, box
    |x_j| <= log_price_bound, L-BFGS-B on Phi = log g under the shared budgets, the
    evaluation cap enforced by `GuardedObjective` (SciPy's `maxfun` alone overruns);
    termination per §5.4 from our own projected residual, never from SciPy's success."""
    sigma = m.scales(problem)
    n = len(problem.variables)
    bound = opts["log_price_bound"]
    x0 = [
        0.0
        if warm is None or v not in warm
        else min(max(math.log(warm[v] / sigma[v]), -bound), bound)
        for v in problem.variables
    ]
    guard = m.GuardedObjective(problem, sigma, budgets.evaluations)
    calls_before = budgets.evaluations.oracle_calls
    iterations = 0

    def fun(x: np.ndarray) -> tuple[float, np.ndarray]:
        phi, grad = guard([float(t) for t in x])
        return phi, np.array(grad)

    def count(intermediate_result: Any) -> None:
        nonlocal iterations
        iterations += 1

    t0 = time.perf_counter()
    x_ret: list[float] | None
    try:
        res = minimize(
            fun,
            np.array(x0),
            jac=True,
            method="L-BFGS-B",
            bounds=[(-bound, bound)] * n,
            callback=count,
            options={
                "maxcor": opts["lbfgs_memory"],
                "ftol": opts["ftol"],
                "gtol": opts["pgtol"],
                "maxiter": budgets.iterations_left,
                "maxfun": budgets.evaluations.remaining,
            },
        )
        x_ret = [float(t) for t in res.x]
        status: int | None = int(res.status)
        message, nit, scipy_nfev = str(res.message), int(res.nit), int(res.nfev)
    except m.EvaluationCapReached:
        x_ret, status, message, nit, scipy_nfev = (
            None,
            None,
            "GUARD: evaluation cap",
            iterations,
            None,
        )
    budgets.iterations_left -= nit
    x, _, grad, ev = guard.final(x_ret)
    seconds = time.perf_counter() - t0
    residual = m.projected_residual(x, grad, -bound, bound)
    # §5.4: acceptance is our own KKT residual at the final point, never scipy "success".
    if residual <= opts["residual_tolerance"]:
        termination = "converged"
    elif guard.fired or status == 1:
        termination = "iteration_cap"
    else:
        termination = "not_converged"
    nu = {v: sigma[v] * math.exp(t) for v, t in zip(problem.variables, x, strict=True)}
    return Solved(
        problem.markets,
        problem.variables,
        sigma,
        x,
        nu,
        ev.value,
        residual,
        status,
        message,
        nit,
        guard.evaluations,
        scipy_nfev,
        guard.fired,
        termination,
        ev.trades,
        budgets.evaluations.oracle_calls - calls_before,
        seconds,
    )


def new_budgets(opts: Mapping[str, Any]) -> Budgets:
    return Budgets(m.EvaluationBudget(opts["max_function_evaluations"]), opts["max_iterations"])


def cfmm_solve(
    bundle: SnapshotBundle,
    case: Case,
    markets: Sequence[str],
    opts: Mapping[str, Any],
    max_quotes: int | None = None,
) -> dict[str, Any]:
    """Dual solve + §6 recovery with the restricted re-solve wired to `solve`; both draw
    on one `Budgets` (the re-solve is skipped, never re-funded, when nothing is left)."""
    problem = m.dual_problem(bundle, case, markets)
    budgets = new_budgets(opts)
    sol = solve(problem, opts, budgets)
    resolves: list[Solved] = []

    def resolve(allowed: Mapping[str, str]) -> Sequence[m.Trade] | None:
        if budgets.evaluations.remaining < 1 or budgets.iterations_left < 1:
            raise m.ResolveBudgetExhausted
        restricted = m.dual_problem(bundle, case, list(allowed), allowed)
        r = solve(restricted, opts, budgets, warm=sol.nu)
        resolves.append(r)
        return r.trades

    cache = QuoteCache(bundle)
    rec = m.recover(
        bundle,
        case,
        markets,
        sol.trades,
        sol.nu,
        m.RecoveryOptions(
            opts["min_split_share"],
            opts["max_recovery_attempts"],
            opts["cycle_resolve"],
            max_quotes,
        ),
        cache,
        resolve,
    )
    return {"solution": sol, "resolves": resolves, "recovery": rec, "budgets": budgets}


def trades_json(trades: Sequence[m.Trade]) -> list[dict[str, Any]]:
    return [dataclasses.asdict(t) for t in trades]


def solved_json(s: Solved) -> dict[str, Any]:
    return {
        "markets": list(s.markets),
        "variables": list(s.variables),
        "sigma": s.sigma,
        "u": s.u,
        "nu": s.nu,
        "value": s.value,
        "residual": s.residual,
        "scipy_status": s.status,
        "scipy_message": s.message,
        "nit": s.nit,
        "evaluations": s.evaluations,
        "scipy_nfev": s.scipy_nfev,
        "guard_fired": s.guard_fired,
        "oracle_calls": s.oracle_calls,
        "termination": s.termination,
        "trades": trades_json(s.trades),
    }


BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)


def network_bundle(case_doc: Mapping[str, Any]) -> tuple[SnapshotBundle, Case]:
    pools = {
        p["pool_id"]: ConstantProductPoolState(
            p["pool_id"], p["token0"], p["token1"], p["reserve0"], p["reserve1"], p["fee_bps"]
        )
        for p in case_doc["pools"]
    }
    case = Case(case_doc["id"], case_doc["token_in"], case_doc["token_out"], case_doc["amount_in"])
    return SnapshotBundle(
        "cfmm-" + case_doc["id"], "synthetic", 1, BLOCK, pools, (case,), "fixture", "<cfmm>"
    ), case


SOURCE_FILES = (
    "tools/upstream/cfmm/python_reference.py",
    "tests/routing/cfmm_contract_model.py",
    "tests/fixtures/cfmm/author_inputs.json",
)


def environment() -> dict[str, Any]:
    return {
        "sources_sha256": {
            f: hashlib.sha256((REPO / f).read_bytes()).hexdigest() for f in SOURCE_FILES
        },
        "python": platform.python_version(),
        "scipy": scipy.__version__,
        "numpy": np.__version__,
        "machine": platform.machine(),
        "system": platform.system(),
        "command": "uv run --with scipy==1.18.1 --with numpy==2.5.3 python "
        "tools/upstream/cfmm/python_reference.py fixtures",
    }


def fixtures() -> None:
    inputs = json.loads((REPO / "tests/fixtures/cfmm/author_inputs.json").read_text())
    out: dict[str, Any] = {
        "_comment": "Python contract model + SciPy L-BFGS-B (NOT author execution). Written by "
        "tools/upstream/cfmm/python_reference.py fixtures; tests re-verify each dual point.",
        "environment": environment(),
        "preset": PRESET,
        "cases": [],
    }
    for doc in inputs["router"]:
        if doc["route_kwargs"]:
            continue  # the maxiter=1 author case is an audit case, not a model case
        bundle, case = network_bundle(doc)
        markets = tuple(bundle.pools)  # the author runs use every pool; so do these
        r = cfmm_solve(bundle, case, markets, PRESET)
        out["cases"].append(case_json(doc["id"], "author_network_all_pools", markets, r))
        if doc["id"] == "r-triangle":  # a declared iteration cap still yields a valid plan
            capped = cfmm_solve(bundle, case, markets, PRESET | {"max_iterations": 2})
            out["cases"].append(
                case_json("r-triangle-maxiter2", "author_network_all_pools", markets, capped)
            )
    out["forced_caps"] = forced_caps(inputs)
    mantle = load_bundle(REPO / "tests/fixtures/routing/mantle_mixed")
    for case in mantle.cases:
        markets = m.market_universe(mantle, case, 3, [m.CPMM])
        r = cfmm_solve(mantle, case, markets, PRESET)
        out["cases"].append(case_json(case.case_id, "mantle_mixed_cpmm_h3", markets, r))
    path = REPO / "tests/fixtures/cfmm/model_reference.json"
    path.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(f"wrote {path.relative_to(REPO)}")


FORCED_CAPS = (1, 2, 3, 5)


def forced_caps(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """§5.3 regression evidence with the real pinned SciPy: the first four author router
    cases under `max_function_evaluations` in FORCED_CAPS (the cap SciPy's `maxfun` alone
    overran: 4/4/5/5 evaluations for a cap of 1 at 0c5890f), a `max_iterations` cap, and
    r-cycle with the shared budget exhausted by the initial solve (re-solve skipped)."""
    runs: list[dict[str, Any]] = []
    for doc in inputs["router"][:4]:
        bundle, case = network_bundle(doc)
        problem = m.dual_problem(bundle, case, tuple(bundle.pools))
        for key, caps in (("max_function_evaluations", FORCED_CAPS), ("max_iterations", (1, 2))):
            for cap in caps:
                opts = PRESET | {key: cap}
                budgets = new_budgets(opts)
                sol = solve(problem, opts, budgets)
                runs.append(
                    {
                        "case": doc["id"],
                        "cap_key": key,
                        "cap": cap,
                        "markets": list(problem.markets),
                    }
                    | solved_json(sol)
                    | {
                        "budget_used": budgets.evaluations.used,
                        "iterations_left": budgets.iterations_left,
                    }
                )
    cycle = next(d for d in inputs["router"] if d["id"] == "r-cycle")
    bundle, case = network_bundle(cycle)
    full = solve(m.dual_problem(bundle, case, tuple(bundle.pools)), PRESET, new_budgets(PRESET))
    starved = PRESET | {"max_function_evaluations": full.evaluations}
    r = cfmm_solve(bundle, case, tuple(bundle.pools), starved)
    shared = case_json(
        "r-cycle-resolve-starved", "author_network_all_pools", tuple(bundle.pools), r
    )
    shared["cap"] = full.evaluations
    shared["budget_used"] = r["budgets"].evaluations.used
    return {"runs": runs, "resolve_starved": shared}


def case_json(
    cid: str, universe: str, markets: Sequence[str], r: Mapping[str, Any]
) -> dict[str, Any]:
    rec: m.Recovery = r["recovery"]
    return {
        "id": cid,
        "universe": universe,
        "markets": list(markets),
        "solution": solved_json(r["solution"]),
        "resolves": [solved_json(x) for x in r["resolves"]],
        "budget": {
            "evaluations_used": r["budgets"].evaluations.used,
            "oracle_calls": r["budgets"].evaluations.oracle_calls,
            "iterations_left": r["budgets"].iterations_left,
        },
        "recovery": {
            "failure": rec.failure,
            "attempts": rec.attempts,
            "cycle_removed": rec.cycle_removed,
            "pruned": [list(p) for p in rec.pruned],
            "resolved": rec.resolved,
            "resolve_skipped": rec.resolve_skipped,
            "support": list(rec.support),
            "gross": None if rec.evaluation is None else str(rec.evaluation.gross_output),
            "flows": [
                {
                    "pool_id": f.edge.pool_id,
                    "token_in": f.edge.token_in,
                    "amount_in": str(f.amount_in),
                    "amount_out": str(f.amount_out),
                }
                for f in rec.flows
            ],
        },
    }


def best_single_path(
    bundle: SnapshotBundle, case: Case, markets: Sequence[str], max_hops: int
) -> tuple[int | None, int]:
    sub = dataclasses.replace(bundle, pools={p: bundle.pools[p] for p in markets})
    cache = QuoteCache(bundle)
    best: int | None = None
    n = 0
    for path in enumerate_paths(build_graph_index(sub), case.token_in, case.token_out, max_hops):
        n += 1
        ev = evaluate(bundle, case, path_plan(case, path), gross_only(), quote=cache)
        if ev.status is EvalStatus.OK and (best is None or ev.gross_output > best):
            best = ev.gross_output
    return best, n


def tuning(corpus: Path, out_dir: Path) -> None:
    bundle = load_bundle(corpus)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for case in bundle.cases:
        row: dict[str, Any] = {"case_id": case.case_id, "amount_in": str(case.amount_in)}
        markets = m.market_universe(bundle, case, 3, [m.CPMM])
        row["markets"] = len(markets)
        if not markets:
            anyp = m.market_universe(bundle, case, 3, [m.CPMM, m.CL, "liquidity_book"])
            full = dataclasses.replace(bundle, pools=dict(bundle.pools))
            reach = next(
                enumerate_paths(build_graph_index(full), case.token_in, case.token_out, 3), None
            )
            row["scope"] = "unsupported_protocol_ceiling" if (anyp or reach) else "no_route"
            rows.append(row)
            continue
        t0 = time.perf_counter()
        r = cfmm_solve(bundle, case, markets, PRESET)
        row["seconds"] = time.perf_counter() - t0
        sol: Solved = r["solution"]
        rec: m.Recovery = r["recovery"]
        row.update(
            tokens=len(sol.variables) + 1,
            termination=sol.termination,
            scipy_message=sol.message,
            nit=sol.nit,
            evaluations=sol.evaluations,
            evaluations_total=r["budgets"].evaluations.used,
            oracle_calls_total=r["budgets"].evaluations.oracle_calls,
            residual=sol.residual,
            estimate=sol.value,
            resolves=[
                {"termination": x.termination, "nit": x.nit, "residual": x.residual}
                for x in r["resolves"]
            ],
            recovery_failure=rec.failure,
            attempts=rec.attempts,
            cycle_removed=rec.cycle_removed,
            pruned=[list(p) for p in rec.pruned],
            support=len(rec.support),
            steps=0 if rec.plan is None else len(rec.plan.steps),
            gross=None if rec.evaluation is None else str(rec.evaluation.gross_output),
            quotes=rec.quotes_executed,
        )
        best, npaths = best_single_path(bundle, case, markets, 3)
        row["best_single_path"] = None if best is None else str(best)
        row["paths"] = npaths
        rows.append(row)
        print(
            case.case_id,
            row.get("termination"),
            row.get("nit"),
            f"{row.get('residual', 0):.2e}",
            row.get("recovery_failure"),
            row.get("gross"),
            row.get("best_single_path"),
            flush=True,
        )
    summary = {
        "environment": environment() | {"command": "python_reference.py tuning"},
        "corpus": str(corpus),
        "bundle_hash": bundle.bundle_hash,
        "preset": PRESET,
        "rows": rows,
    }
    (out_dir / "tuning_probe.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out_dir / 'tuning_probe.json'}")


SWEEP_SOLVER: dict[str, dict[str, Any]] = {
    "preset": {},
    "lbfgs_memory_5": {"lbfgs_memory": 5},
    "lbfgs_memory_20": {"lbfgs_memory": 20},
    "ftol_scipy_default": {"ftol": 2.220446049250313e-09},
    "pgtol_1e-7": {"pgtol": 1e-7},
    "max_iterations_50": {"max_iterations": 50},
    "max_iterations_25": {"max_iterations": 25},
    "log_price_bound_20": {"log_price_bound": 20.0},
}
SWEEP_SHARE = (0.0, 1e-6, 1e-4, 1e-3, 1e-2)


def sweep(corpus: Path, out_dir: Path) -> None:
    """One-factor ablations around the preset on the tuning split (§10): solver caps and
    tolerances (dual solve only), then `min_split_share` (full recovery, exact gross)."""
    bundle = load_bundle(corpus)
    problems = [
        m.dual_problem(bundle, c, m.market_universe(bundle, c, 3, [m.CPMM])) for c in bundle.cases
    ]
    solver: dict[str, Any] = {}
    for name, delta in SWEEP_SOLVER.items():
        runs = [solve(p, PRESET | delta, new_budgets(PRESET | delta)) for p in problems]
        res = sorted(r.residual for r in runs)
        solver[name] = {
            "delta": delta,
            "converged_at_1e-6": sum(r <= 1e-6 for r in res),
            "converged_at_1e-5": sum(r <= 1e-5 for r in res),
            "converged_at_1e-4": sum(r <= 1e-4 for r in res),
            "iteration_cap_hits": sum(r.termination == "iteration_cap" for r in runs),
            "nit_max": max(r.nit for r in runs),
            "evaluations_max": max(r.evaluations for r in runs),
            "residual_p50": res[len(res) // 2],
            "residual_p90": res[int(0.9 * len(res))],
            "residual_max": res[-1],
            "oracle_calls_max": max(r.oracle_calls for r in runs),
        }
        print(name, solver[name], flush=True)
    gross: dict[str, list[int | None]] = {}
    for share in SWEEP_SHARE:
        gross[repr(share)] = []
        for c in bundle.cases:
            markets = m.market_universe(bundle, c, 3, [m.CPMM])
            rec = cfmm_solve(bundle, c, markets, PRESET | {"min_split_share": share})["recovery"]
            gross[repr(share)].append(
                None if rec.evaluation is None else rec.evaluation.gross_output
            )
    base = gross[repr(PRESET["min_split_share"])]
    shares: dict[str, Any] = {}
    for key, values in gross.items():
        cmp = {">": 0, "=": 0, "<": 0, "failed": 0}
        for got, ref in zip(values, base, strict=True):
            if got is None or ref is None:
                cmp["failed"] += 1
            else:
                cmp[">" if got > ref else "=" if got == ref else "<"] += 1
        shares[key] = {"vs_preset": cmp}
        print("min_split_share", key, cmp, flush=True)
    doc = {
        "environment": environment() | {"command": "python_reference.py sweep"},
        "bundle_hash": bundle.bundle_hash,
        "preset": PRESET,
        "solver": solver,
        "min_split_share": shares,
    }
    (out_dir / "sweep.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out_dir / 'sweep.json'}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["fixtures"]:
        fixtures()
    elif sys.argv[1:2] == ["tuning"]:
        tuning(Path(sys.argv[2]), Path(sys.argv[3]))
    elif sys.argv[1:2] == ["sweep"]:
        sweep(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        raise SystemExit(__doc__)

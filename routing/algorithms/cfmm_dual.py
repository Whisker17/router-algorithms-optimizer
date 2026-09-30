"""`cfmm_dual` (WHI-1558, R021-P12; contract R021-C/1 §2 row 5): the CFMM dual-decomposition
router (Diamandis, Resnick, Chitra, Angeris, arXiv:2302.04938v1) on the admitted
constant-product markets, turned into ONE exact, fully funded integer `RoutePlan`. Normative
specification: `docs/references/research-021/cfmm-dual.md` §§4-9, 11 (WHI-1557, R021-P11,
`narrow_go`). Experimental, `custom` group, CPMM stage only (`concentrated` is WHI-1559,
Liquidity Book is excluded). Gross-only, single-source exact input, estimate-only: no
certified bound, no global or integer optimality claim, no performance claim.

**Scope** (§6.1, before any numeric work). The market universe is `simple_path_union`
(`routing.cfmm.model.market_universe`): every admitted CPMM pool on a simple
`token_in -> token_out` path of at most `search.max_hops` admitted CPMM pools, in bundle
order -- no shortlist. Objective not `gross_only` -> `unsupported` (scope `objective`); an
empty universe while a <= `max_hops` path exists through pools outside the stage ->
`unsupported` (scope `protocol_ceiling`; concentrated/LB pools are never CPMM substitutes);
no such path at all -> `no_route` (the structural search is complete).

**Numeric solve** (§5; `routing.cfmm.optimizer`). ONE `SolveBudget` per attempt covers the
initial full-network solve (spot start, x0 = 0) and the optional restricted re-solve
(warm-started from the initial prices): evaluations, oracle calls and iterations only ever
accumulate, nothing is re-funded. `converged` means only that the §5.4 projected residual is
below `residual_tolerance` (a saturated box edge can satisfy it far from the optimum); the
residual, tolerance, active bounds and clamped warm variables are recorded per phase.

**Recovery** (§6; `routing.cfmm.recovery`, `cfmm_share_projection/1`): support, relevance,
deterministic full cycle removal, optional fixed-direction re-solve, exact share projection
with prune-and-retry, `merged_plan` and an in-solve replay that must equal the accounted
gross (else `algorithm_error`, never a hidden fallback). The plan is published once, after
that replay. A recovery failure (`empty_support`, `support_exhausted`, `attempts_exhausted`,
`resolve_failed`, `numeric_failure`) runs the declared fallback: `single_path` = the best
exact single path over the SAME market universe (`single_path.solve` on the same per-solve
`QuoteCache`, so the remaining quote budget and `Budget.max_candidates` =
`fallback_paths_evaluated` apply; its status is kept, `termination: recovery_failed`,
visible `fallback`); `none` = `model_error`. A recovery stopped by the cooperative quote
budget is `timeout` (`quote_budget`); the runner's hard wall/quote kill keeps only its
published candidates (no certificate survives).

**Certificate** (§7): on `ok`, `lower_raw` = evaluated score, `upper_raw`/`gap_raw` null,
`optimality_proven` false; `bound_kind: estimate` with g of the INITIAL full-network solve,
its residual and tolerance iff that solve `converged` and no fallback was used, else
`unknown`. A restricted re-solve or fallback value is never an estimate.

**Accounting** (§9.4). The whole solve runs inside `counted_evaluations()`
(`internal_evaluations`). Numeric units are the shared budget's charged counts
(`objective_evaluations` = `gradient_evaluations` = charged evaluations, never SciPy's
`nfev`); `quotes_executed` / `quotes_memoized` are the one cache's misses / hits (the worker
meter), `exact_replay_quotes` the recovery's quotes, `paths_scored` the fallback's evaluated
paths. Observational seconds (the worker's one-off NumPy/SciPy import, per-phase solve
times) are only in `r021.stages`; every other diagnostic is deterministic and JSON-finite.

**Preparation** validates the options (§9.3, all required, no defaults) and
`search.max_hops`, builds the immutable structural graph index and calls
`numeric_backend()` so the NumPy/SciPy import is charged to `prepare`. Importing this
module never imports NumPy or SciPy.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from routing.algorithms import single_path
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    Capabilities,
    OptionsError,
    SolveContext,
    SolveResult,
    SolveStatus,
    option_choice,
    option_int,
    option_number,
    require_option_keys,
    settings_sha256,
    validated_options,
)
from routing.algorithms.direct_split_certified import cohort_of
from routing.cfmm.model import CPMM, dual_problem, market_universe
from routing.cfmm.optimizer import (
    NumericSolution,
    SolveBudget,
    SolverSettings,
    numeric_backend,
    resolve_restricted,
)
from routing.cfmm.optimizer import (
    solve as numeric_solve,
)
from routing.cfmm.recovery import (
    RECOVERY,
    Recovery,
    RecoveryOptions,
    ResolveSkipped,
    recover,
)
from routing.evaluator import counted_evaluations
from routing.search import GraphIndex, QuoteCache, build_graph_index, enumerate_paths, min_hops
from snapshot.models import Case, SnapshotBundle

NAME = "cfmm_dual"
CONTRACT = "docs/references/research-021/cfmm-dual.md"
CAPABILITIES = Capabilities(
    multi_hop=True, split=True, shared_pools=True, protocols=(CPMM,)
)  # the CPMM stage: constant-product markets only
SEARCH_PARAMS = ("max_hops",)
MAX_CANDIDATES_UNIT = "fallback_paths_evaluated"
STAGE_PROTOCOLS = (CPMM,)
MARKET_PROTOCOLS = ("constant_product", "constant_product+concentrated")
FALLBACKS = ("none", "single_path")
INT_OPTIONS: dict[str, tuple[int, int]] = {
    "max_iterations": (1, 1000),
    "max_function_evaluations": (1, 3000),
    "lbfgs_memory": (3, 30),
    "max_recovery_attempts": (1, 64),
}
FLOAT_OPTIONS: dict[str, tuple[float, float]] = {
    "pgtol": (1e-14, 1e-3),
    "ftol": (1e-16, 1e-3),
    "residual_tolerance": (1e-12, 1e-2),
    "log_price_bound": (1.0, 200.0),
    "min_split_share": (0.0, 0.1),
}
OPTION_KEYS = frozenset(
    {"market_protocols", "cycle_resolve", "fallback", *INT_OPTIONS, *FLOAT_OPTIONS}
)
# The bounded comparison preset `cfmm_dual/1` (cfmm-dual.md §9.3), frozen by its bytes. The
# CL stage (WHI-1559) adds its own version; this file is never rewritten.
PRESET: dict[str, Any] = {
    "path": "config/cfmm_dual/preset_v1.yaml",
    "sha256": "1526133cd3bf61a5493ee68f2704875ecf225637c1c7858aa30d3be464dbf605",
    "key": "R021-P12-cfmm_dual",
    "version": 1,
}
WORK_UNITS = (
    "market_oracle_calls",
    "objective_evaluations",
    "gradient_evaluations",
    "optimizer_iterations",
    "recovery_attempts",
    "admission_checks",
    "combinations_rejected_cycle",
    "quotes_executed",
    "quotes_memoized",
    "exact_replay_quotes",
    "internal_evaluations",
    "paths_scored",
)
FALLBACK_REASONS = (
    "empty_support",
    "support_exhausted",
    "attempts_exhausted",
    "resolve_failed",
    "numeric_failure",
)

PROVENANCE: Mapping[str, Any] = MappingProxyType(
    {
        "experimental": True,
        "opt_in": True,
        "issue": "WHI-1558",
        "identity": (
            "CFMM dual decomposition (L-BFGS-B on the normalized log-price dual) over the "
            "simple_path_union of admitted constant-product markets, recovered into one exact "
            "integer plan by cfmm_share_projection/1; estimate-only"
        ),
        "contract": (
            f"{CONTRACT} §4-§9, §11.1 (WHI-1557, R021-P11, narrow_go; merged "
            "91d7f4b056e04dbfe7de0a0b867618875c2efd27)"
        ),
        "stage": "constant_product (CPMM); concentrated is WHI-1559; liquidity_book excluded",
        "market_universe": "simple_path_union",
        "recovery": RECOVERY,
        "method": "arXiv:2302.04938v1 eqs. (5)-(9), App. A (Diamandis, Resnick, Chitra, Angeris)",
        "author_code": {
            "repo": "https://github.com/bcc-research/CFMMRouter.jl",
            "git_revision": "5932e42e5077ffc7d8e02c3b3ad2e9ed1d441267",
            "license": "MIT (Copyright (c) 2021 Guillermo Angeris, Theo Diamandis)",
            "notice": "routing/cfmm/NOTICE.md (links the verbatim MIT licence)",
        },
        "optimizer": "scipy 1.18.1 minimize(method='L-BFGS-B'), numpy 2.5.3 (pinned)",
        "model": "routing/cfmm (ported from tests/routing/cfmm_contract_model.py, WHI-1557)",
        "not_claimed": [
            "a certified bound or gap (the continuous value is a float estimate only)",
            "global or integer optimality of the recovered plan",
            "that `converged` implies an accurate optimum (a residual criterion only)",
            "concentrated / liquidity-book coverage (unsupported rows) or net objectives",
            "any speedup or quality gain",
        ],
    }
)


class CfmmDualConfigError(ValueError):
    """`prepare` received an invalid or missing `search.max_hops`."""


def validate_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """The `options_validator` (cfmm-dual.md §9.3): exactly the twelve keys, all required;
    integers never bools or floats, floats finite (bools refused), every value in range;
    `market_protocols` `constant_product+concentrated` is refused until the CL stage."""
    require_option_keys(options, set(OPTION_KEYS))
    out: dict[str, Any] = {
        "market_protocols": option_choice(
            options["market_protocols"], "market_protocols", MARKET_PROTOCOLS
        )
    }
    if out["market_protocols"] != CPMM:
        raise OptionsError(
            "market_protocols: 'constant_product+concentrated' is the CL stage (WHI-1559), "
            "not implemented by this CPMM stage; only 'constant_product' is accepted"
        )
    for key, (lo, hi) in INT_OPTIONS.items():
        out[key] = option_int(options[key], key, lo, hi)
    for key, (flo, fhi) in FLOAT_OPTIONS.items():
        out[key] = option_number(options[key], key, flo, fhi)
    if not isinstance(options["cycle_resolve"], bool):
        raise OptionsError(f"cycle_resolve: expected a bool, got {options['cycle_resolve']!r}")
    out["cycle_resolve"] = options["cycle_resolve"]
    out["fallback"] = option_choice(options["fallback"], "fallback", FALLBACKS)
    return out


@dataclass(frozen=True)
class PreparedCfmm:
    """Immutable per-worker preparation: the validated options, their settings hash, the
    numeric settings, the hop bound, the structural graph index of the whole bundle (scope
    rule of §6.1) and the loaded backend's deterministic provenance."""

    options: Mapping[str, Any]
    settings_sha256: str
    settings: SolverSettings
    max_hops: int
    index: GraphIndex
    backend: Mapping[str, Any]
    import_seconds: float


def prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> PreparedCfmm:
    options = validated_options(FACTORY, config.options)  # the public entry validates too
    max_hops = config.params.get("max_hops")
    if not isinstance(max_hops, int) or isinstance(max_hops, bool) or max_hops < 1:
        raise CfmmDualConfigError(
            f"{NAME} requires search.max_hops as an integer >= 1, got {max_hops!r}"
        )
    provenance = dict(numeric_backend().provenance)  # charged here: the one-off import
    seconds = float(provenance.pop("import_seconds"))
    return PreparedCfmm(
        options=MappingProxyType(dict(options)),
        settings_sha256=settings_sha256(options),
        settings=SolverSettings.from_options(options),
        max_hops=max_hops,
        index=build_graph_index(bundle),
        backend=MappingProxyType(json.loads(json.dumps(provenance))),
        import_seconds=seconds,
    )


# ------------------------------------------------------------------ records


def _finite(value: float) -> float | str:
    """A JSON-safe float: the value when finite, else its repr (never NaN/inf in JSON)."""
    return value if math.isfinite(value) else repr(value)


def _decimal(value: float) -> str:
    return str(Decimal(repr(value)))


def domain_record(
    bundle: SnapshotBundle, markets: tuple[str, ...], max_hops: int, min_split_share: float
) -> dict[str, Any]:
    """`r021.domain/1` of cfmm-dual.md §9.1 over the case's market universe."""
    return {
        "schema": "r021.domain/1",
        "universe": {
            "bundle": bundle.bundle_hash,
            "cohort": cohort_of(bundle),
            "pools": sorted(markets),
        },  # fmt: skip
        "protocols": list(STAGE_PROTOCOLS),
        "pool_order": list(markets),
        "hops": {"max": max_hops, "param": "search.max_hops"},
        "splits": {"max": None, "param": None, "governs": "none"},
        "amount_grid": {
            "kind": "recovered_continuous",
            "recovery": RECOVERY,
            "min_split_share": _decimal(min_split_share),
            "remainder": "last_leg_all_remaining",
        },
        "zero_output_leg": "infeasible",
        "token_reuse": "simple_path",
        "pool_reuse": "shared_merged",
        "dag_admission": "plan_token_dag",
        "full_fill": "v1_full_fill",
    }


def _solve_view(
    sol: NumericSolution, settings: SolverSettings, warm: Mapping[str, float] | None
) -> dict[str, Any]:
    """The deterministic record of one numeric phase (no seconds)."""
    bound = settings.log_price_bound
    point = sol.point
    view: dict[str, Any] = {
        "markets": list(sol.problem.market_ids),
        "directions": [a for a in sol.problem.allowed] if any(sol.problem.allowed) else None,
        "variables": list(sol.problem.variables),
        "termination": sol.termination,
        "failure": sol.failure,
        "error": sol.error,
        "residual": None if point is None else _finite(point.residual),
        "residual_tolerance": settings.residual_tolerance,
        "value": None if point is None else _finite(point.value),
        "point_source": None if point is None else point.source,
        "nu": None if point is None else {v: _finite(p) for v, p in point.nu.items()},
        "active_bounds": (
            None
            if point is None
            else [v for v, x in zip(sol.problem.variables, point.x, strict=True) if abs(x) >= bound]
        ),
        "trades": None if point is None else len(point.trades),
        "evaluations": sol.evaluations,
        "oracle_calls": sol.oracle_calls,
        "cache_hits": sol.cache_hits,
        "iterations": sol.iterations,
        "guard_fired": sol.guard_fired,
        "scipy_status": sol.scipy_status,
        "scipy_message": sol.scipy_message,
        "scipy_nit": sol.scipy_nit,
        "scipy_nfev": sol.scipy_nfev,
        "warm_started": sol.warm_started,
        "warm_clamped": None,
        "budget_after": dict(sol.budget_after),
    }
    if warm is not None and sol.sigma:
        view["warm_clamped"] = [
            v
            for v in sol.problem.variables
            if v in warm and abs(math.log(warm[v]) - math.log(sol.sigma[v])) > bound
        ]
    return view


# ------------------------------------------------------------------ solve


def solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    prepared = context.prepared
    if not isinstance(prepared, PreparedCfmm):
        raise TypeError(f"{NAME}.solve needs the PreparedCfmm returned by prepare()")
    with counted_evaluations() as evaluations:
        return _solve(case, context, budget, prepared, lambda: evaluations.count)


def _fallback(
    case: Case,
    context: SolveContext,
    budget: Budget,
    markets: tuple[str, ...],
    max_hops: int,
    cache: QuoteCache,
) -> SolveResult:
    """The declared `single_path` fallback over the same market universe, on the same
    cache (remaining quote budget) and `Budget.max_candidates` (paths evaluated)."""
    bundle = context.bundle
    sub = dataclasses.replace(bundle, pools={pid: bundle.pools[pid] for pid in markets})
    ctx = dataclasses.replace(
        context, prepared=single_path.PreparedSinglePath(build_graph_index(sub), max_hops)
    )
    return single_path.solve(case, ctx, budget, cache=cache)


def _solve(
    case: Case,
    context: SolveContext,
    budget: Budget,
    prepared: PreparedCfmm,
    evaluated: Callable[[], int],
) -> SolveResult:
    bundle, objective = context.bundle, context.objective
    opts, settings = prepared.options, prepared.settings
    max_hops = prepared.max_hops
    markets = market_universe(bundle, case, max_hops)
    domain = domain_record(bundle, markets, max_hops, opts["min_split_share"])
    work = dict.fromkeys(WORK_UNITS, 0)
    stages: dict[str, float] = {"prepare_numeric_import": prepared.import_seconds}
    record: dict[str, Any] = {
        "schema": "r021.diagnostics/1",
        "contract": "R021-C/1",
        "algorithm": NAME,
        "domain": domain,
        "candidate_domain_hash": hashlib.sha256(
            json.dumps(domain, sort_keys=True).encode()
        ).hexdigest(),
        "certificate": None,
        "certificate_unavailable_reason": "not_produced",
        "max_candidates_unit": MAX_CANDIDATES_UNIT,
        "work": work,
        "scope": {"supported": True, "reason": None},
        "fallback": {"used": False, "source": None, "reason": None},
        "stages": stages,
    }
    cfmm: dict[str, Any] = {
        "stage": CPMM,
        "market_protocols": opts["market_protocols"],
        "market_universe": "simple_path_union",
        "max_hops": max_hops,
        "markets": list(markets),
        "termination": None,
        "recovery_failure": None,
        "initial": None,
        "restricted": [],
        "resolve": None,
        "recovery": None,
        "fallback": None,
        "estimate": None,
        "estimate_withheld": None,
        "numeric_budget": None,
        "inconsistency": None,
        "backend": dict(prepared.backend),
    }
    stats: dict[str, Any] = {"cfmm": cfmm, "r021": record}
    cache = QuoteCache(bundle)

    def finish(
        status: SolveStatus,
        *,
        error: str | None = None,
        best: SolveResult | None = None,
        truncated: int = 0,
    ) -> SolveResult:
        work["quotes_executed"], work["quotes_memoized"] = cache.misses, cache.hits
        work["internal_evaluations"] = evaluated()
        return SolveResult(
            case_id=case.case_id,
            algorithm=NAME,
            status=status,
            plan=None if best is None else best.plan,
            evaluation=None if best is None else best.evaluation,
            score=None if best is None else best.score,
            candidates_considered=work["internal_evaluations"],
            candidates_truncated=truncated,
            error=error,
            search_stats=stats,
        )

    def out_of_scope(status: SolveStatus, reason: str | None, error: str) -> SolveResult:
        cfmm["termination"] = "unsupported_scope" if reason is not None else None
        if reason is not None:
            record["scope"] = {"supported": False, "reason": reason}
        return finish(status, error=error)

    pair = f"{case.token_in} -> {case.token_out}"
    if objective.mode != "gross_only":
        return out_of_scope(
            SolveStatus.UNSUPPORTED,
            "objective",
            f"objective {objective.mode} is outside {NAME}'s gross_only ceiling (the dual "
            "models gross output only; no net estimate)",
        )
    if not markets:
        if next(enumerate_paths(prepared.index, case.token_in, case.token_out, max_hops), None):
            return out_of_scope(
                SolveStatus.UNSUPPORTED,
                "protocol_ceiling",
                f"no admitted constant_product market lies on a <= {max_hops}-pool path "
                f"{pair}, but such paths exist through pools outside the CPMM stage "
                "(concentrated is WHI-1559, liquidity_book is excluded)",
            )
        shortest = min_hops(prepared.index, case.token_in, case.token_out)
        why = (
            f"{case.token_out} is unreachable from {case.token_in} in the pool graph"
            if shortest is None
            else f"hop cap: the shortest path {pair} needs {shortest} pools, above "
            f"search.max_hops={max_hops}"
        )
        return out_of_scope(SolveStatus.NO_ROUTE, None, f"complete structural search: {why}")

    # ---- numeric solve: ONE budget for the initial solve and the restricted re-solve
    problem = dual_problem(bundle, case, markets)
    numeric = SolveBudget.for_settings(settings)
    initial = numeric_solve(problem, settings, numeric)
    stages["initial_solve"] = initial.seconds
    cfmm["initial"] = _solve_view(initial, settings, None)
    resolves: list[NumericSolution] = []
    rec: Recovery | None = None
    failure: str | None = None
    if initial.failure is not None or initial.point is None or initial.trades is None:
        failure = "numeric_failure"
    else:
        warm = initial.point.nu
        nu_initial = dict(warm)

        def resolve(allowed: Mapping[str, str]) -> Any:
            again = resolve_restricted(problem, allowed, settings, numeric, warm)
            if again is None:
                raise ResolveSkipped
            resolves.append(again)
            stages["restricted_resolve"] = again.seconds
            cfmm["restricted"].append(_solve_view(again, settings, nu_initial))
            return again.trades

        t0 = time.perf_counter()
        rec = recover(
            bundle,
            case,
            markets,
            initial.trades,
            warm,
            RecoveryOptions(
                opts["min_split_share"],
                opts["max_recovery_attempts"],
                opts["cycle_resolve"],
                budget.max_quotes,
            ),
            cache,
            objective,
            resolve,
        )
        stages["recovery"] = max(0.0, time.perf_counter() - t0 - sum(s.seconds for s in resolves))
        failure = rec.failure
        cfmm["resolve"] = rec.resolve
        cfmm["recovery"] = {
            "recovery": RECOVERY,
            "min_split_share": opts["min_split_share"],
            "max_recovery_attempts": opts["max_recovery_attempts"],
            "cycle_resolve": opts["cycle_resolve"],
            "initial_support": list(rec.initial_support),
            "cycle_removed": list(rec.cycle_removed),
            "support": list(rec.support),
            "attempts": rec.attempts,
            "pruned": [{"pool_id": p, "reason": r, "attempt": a} for p, r, a in rec.pruned],
            "flows": [
                {
                    "pool_id": f.edge.pool_id,
                    "token_in": f.edge.token_in,
                    "token_out": f.edge.token_out,
                    "amount_in": str(f.amount_in),
                    "amount_out": str(f.amount_out),
                }
                for f in rec.flows
            ],
            "gross": None if rec.gross is None else str(rec.gross),
            "failure": rec.failure,
        }
        work["recovery_attempts"] = rec.attempts
        work["admission_checks"] = rec.admission_checks
        work["combinations_rejected_cycle"] = len(rec.cycle_removed)
        work["exact_replay_quotes"] = rec.quotes_executed
    work["market_oracle_calls"] = numeric.oracle_calls
    work["objective_evaluations"] = work["gradient_evaluations"] = numeric.evaluations
    work["optimizer_iterations"] = numeric.iterations
    cfmm["numeric_budget"] = {
        "max_function_evaluations": numeric.max_function_evaluations,
        "max_iterations": numeric.max_iterations,
        "evaluations": numeric.evaluations,
        "oracle_calls": numeric.oracle_calls,
        "cache_hits": numeric.cache_hits,
        "iterations": numeric.iterations,
        "restricted_solves": len(resolves),
    }
    cfmm["recovery_failure"] = failure

    if rec is not None and rec.inconsistency is not None:  # a bug, never a recovery outcome
        cfmm["inconsistency"] = rec.inconsistency
        cfmm["termination"] = initial.termination
        return finish(
            SolveStatus.ALGORITHM_ERROR,
            error=f"recovered plan's replay disagrees with its accounting: {rec.inconsistency}",
        )

    result: SolveResult | None = None
    truncated = 0
    termination: str | None
    if rec is not None and rec.plan is not None:
        context.report_candidate(rec.plan)  # once, after the ok replay
        assert rec.evaluation is not None and rec.gross is not None
        score = objective.score(rec.evaluation)
        result = SolveResult(case.case_id, NAME, SolveStatus.OK, rec.plan, rec.evaluation, score)
        status, termination, error = SolveStatus.OK, initial.termination, None
    elif failure == "quote_budget":
        status, termination = SolveStatus.TIMEOUT, "quote_budget"
        error = (
            f"declared quote budget (max_quotes {budget.max_quotes}) stopped the integer "
            "recovery before a complete plan -- not evidence of no_route"
        )
    else:
        assert failure in FALLBACK_REASONS
        termination = "recovery_failed"
        if opts["fallback"] == "none":
            status = SolveStatus.MODEL_ERROR
            error = (
                f"integer recovery failed ({failure}) and fallback is none: the continuous "
                "flow is not a valid route"
            )
        else:
            t0 = time.perf_counter()
            fb = _fallback(case, context, budget, markets, max_hops, cache)
            stages["fallback"] = time.perf_counter() - t0
            record["fallback"] = {"used": True, "source": "single_path", "reason": failure}
            fb_stats = fb.search_stats
            work["paths_scored"] = fb_stats["paths_evaluated"]
            truncated = fb.candidates_truncated
            cfmm["fallback"] = {
                "source": "single_path",
                "reason": failure,
                "status": fb.status.value,
                "error": fb.error,
                "gross": None if fb.evaluation is None else str(fb.evaluation.gross_output),
                **{
                    k: fb_stats[k]
                    for k in (
                        "paths_enumerated",
                        "paths_evaluated",
                        "paths_pruned",
                        "paths_truncated",
                        "truncated_by",
                        "failed_candidates",
                        "paths_incomplete",
                        "best_hops",
                    )
                },  # fmt: skip
            }
            status = fb.status
            error = (
                None
                if fb.status is SolveStatus.OK
                else (
                    f"integer recovery failed ({failure}); single_path fallback over the "
                    f"{len(markets)} CPMM market(s) of this stage (other protocols are "
                    f"outside it): {fb.error}"
                )
            )
            if fb.status is SolveStatus.OK:
                result = fb
    cfmm["termination"] = termination

    if status is SolveStatus.OK and result is not None:
        assert result.score is not None
        estimate = None
        if record["fallback"]["used"]:
            cfmm["estimate_withheld"] = "fallback_used"
        elif initial.termination != "converged" or initial.point is None:
            cfmm["estimate_withheld"] = f"initial_{initial.termination}"
        else:
            estimate = {
                "value": _decimal(initial.point.value),
                "residual": _decimal(initial.point.residual),
                "tolerance": _decimal(settings.residual_tolerance),
            }
        cfmm["estimate"] = estimate
        revision = context.run_identity.get("git_revision")
        record["certificate"] = {
            "schema": "r021.certificate/1",
            "candidate_domain_hash": record["candidate_domain_hash"],
            "objective": objective.mode,
            "source": {
                "git_revision": revision if isinstance(revision, str) else None,
                "bundle_hash": bundle.bundle_hash,
                "algorithm": NAME,
                "effective_settings_sha256": prepared.settings_sha256,
            },
            "request": {
                "case_id": case.case_id,
                "token_in": case.token_in,
                "token_out": case.token_out,
                "amount_in": str(case.amount_in),
            },
            "lower_raw": str(result.score),
            "upper_raw": None,
            "gap_raw": None,
            "bound_kind": "unknown" if estimate is None else "estimate",
            "upper_source": None,
            "estimate": estimate,
            "optimality_proven": False,
            "termination": termination,
        }
        record["certificate_unavailable_reason"] = None
    return finish(status, error=error, best=result, truncated=truncated)


FACTORY = AlgorithmFactory(
    name=NAME,
    solve=solve,
    prepare=prepare,
    capabilities=CAPABILITIES,
    search_params=SEARCH_PARAMS,
    provenance=PROVENANCE,
    options_validator=validate_options,
    options_preset=MappingProxyType(PRESET),
)

__all__ = [
    "FACTORY",
    "NAME",
    "PRESET",
    "CfmmDualConfigError",
    "PreparedCfmm",
    "domain_record",
    "prepare",
    "solve",
    "validate_options",
]

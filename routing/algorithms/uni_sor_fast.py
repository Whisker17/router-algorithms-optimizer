# SPDX-License-Identifier: GPL-3.0-only
#
# Builds on routing/algorithms/uni_sor_port.py, the translation of
# Uniswap/smart-order-router@04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647 (4.31.10) (GPL-3.0;
# docs/references/licenses/uniswap-smart-order-router-04c7c0b4-LICENSE.txt). This file
# calls that port's translated routing core unchanged. The route shortlist below is
# original to this repository: it is *informed by* the candidate budgets of the pinned
# src/routers/alpha-router/functions/get-candidate-pools.ts (read from the pinned npm
# build, tarball sha256 647fb12b64fe6b73b803aa03a804dc32e0bab48530cb6220b37d68a080cf24e4),
# not translated from it, and it is not upstream candidate-selection parity. Private
# internal research use; not conveyed (uni-sor-port-contract.md §9.3).
"""`uni_sor_fast` (WHI-1508 / research key L06): an **opt-in experimental** variant of
`uni_sor_port` that searches a bounded, amount-aware shortlist of the SOR routes instead
of every route. It is a heuristic: its search is an approximation of the reference's,
declared as such in every result (`search_approximation: true`). It is never a default,
never a replacement for `uni_sor_port`, and its trade-off is not accepted (WHI-1510 owns
the performance and adoption decision; no loss tolerance is assumed).

Unchanged from `uni_sor_port` (same functions, same inputs): the frozen V2/V3 cohort and
its A-1 order, route enumeration (B-R*, so `unsupported` / `no_route` from enumeration are
the reference's), the percentage grid (B-A1), the A-2 quote rules and null reasons, the
zero A-3 gas scores, the SOR combination (B-S*, B-F*), the D-1 integer fill and the D-3
independent replay. What changes is only **which enumerated routes enter the quote table**:

1. **Probe.** Every enumerated route is quoted at each pre-registered probe percent (grid
   percents; 100 is mandatory). Probe entries are ordinary table entries, memoized per
   solve, so a shortlisted route's probe entries are not quoted twice.
2. **Rank.** Per probe percent, routes are ranked by `quoteAdjustedForGas` descending,
   ties by the reference's quote-list order (B-Q1). A route with no valid probe entry is
   unranked. Several amounts are ranked because a route that is poor at 100 % (a thin
   pool) can be the best small split; nothing is ranked by TVL or spot price (the frozen
   bundle has no pool TVL, and TVL can miss profitable small splits).
3. **Shortlist.** The union of the `routes_per_probe` best routes of every probe, plus
   the `direct_routes` best-ranked (by their best rank over the probes) one-hop routes.
   Because 100 is always probed, the best full-input single route (the simplest
   incumbent) is always shortlisted, and SOR's 100 % baseline (B-S3) keeps it.
4. **Search.** The unchanged SOR core runs on the shortlisted routes, in the reference's
   quote-list order, over the full grid.
5. **Fallback.** If no route was ranked, or the shortlist yields no complete selection,
   the full reference table is built and searched (deterministically; its quotes are
   charged in the same solve). A shortlist failure is never reported as `no_route`.

**Work bound.** Every probe, shortlist and fallback entry is an entry of the reference's
own table, quoted through the same per-solve memo, so the quotes executed are never more
than `uni_sor_port`'s on the same case; with a fallback they are equal. The ranking and
fallback CPU and the in-solve replay are inside the solve; nothing moves into `prepare`
(which is `uni_sor_port.prepare` plus a settings check).

**What a result means.** A `uni_sor_fast` plan is protocol-exact and independently
evaluated like any other, but it is the best SOR selection **over the searched routes**,
not over the cohort. When the shortlist retains every enumerated route it is identical to
`uni_sor_port`; otherwise equality with the reference is not implied, and restricted-input
agreement is not full-universe parity. Quality is measured as paired regret against the
same-scope `uni_sor_port` result (docs/references/latency-l06-sor-shortlist.md).

**Exact controls.** The quote path is the ordinary default (`pools.quote.quote_exact_in`,
L02/L03/L04 off). Those exact controls reduce work without changing the search scope; this
variant changes the scope. Composing the two is WHI-1510's decision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pools.result import QuoteStatus, SwapResult
from routing.algorithms import uni_sor_port as sor
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
)
from routing.algorithms.path_split import split_path_plan
from routing.evaluator import EvalStatus, evaluate
from routing.search import Edge, QuoteCache
from snapshot.models import Case, PoolState, SnapshotBundle

NAME = "uni_sor_fast"
REFERENCE = sor.NAME
SHORTLIST_PARAMS = ("probe_percents", "routes_per_probe", "direct_routes")
SEARCH_APPROXIMATION = (
    "candidate-route shortlist: the SOR core searches only the routes ranked into the "
    "shortlist by exact multi-amount probe quotes (with a full-table fallback); the result "
    "is the best SOR selection over the searched routes, not over the whole cohort"
)
QUOTE_PATH = (
    "default reference quote_exact_in (L02 empty-span skip, L03 math reuse and L04 prefix "
    "reuse all off)"
)
CANDIDATE_LEAD = (
    "Uniswap/smart-order-router@04c7c0b4 get-candidate-pools.ts getV3CandidatePools/"
    "getV2CandidatePools (topNDirectSwaps/topNTokenInOut/topNSecondHop/base-token TVL "
    "budgets), read from the pinned npm build (tarball sha256 "
    "647fb12b64fe6b73b803aa03a804dc32e0bab48530cb6220b37d68a080cf24e4): a lead, not "
    "translated and not reproduced -- the frozen bundle has no pool TVL, so ranking uses "
    "exact probe quotes at several amounts instead"
)
PROVENANCE: Mapping[str, Any] = MappingProxyType(
    {
        "experimental": True,
        "opt_in": True,
        "research_key": "L06",
        "issue": "WHI-1508",
        "reference": REFERENCE,
        "search_approximation": SEARCH_APPROXIMATION,
        "routing_core": {
            "module": "routing/algorithms/uni_sor_port.py",
            "upstream": dict(sor.UPSTREAM),
            "contract": sor.CONTRACT,
        },
        "candidate_provider": (
            "L06 shortlist over the reference's enumerated routes (replaces the full A-1 "
            "route set in the quote table; not upstream candidate selection)"
        ),
        "candidate_lead": CANDIDATE_LEAD,
        "quote_provider": sor.QUOTE_PROVIDER,
        "gas_score_provider": sor.GAS_SCORE_PROVIDER,
        "quote_path": QUOTE_PATH,
        "fallback": (
            "the full reference quote table, when no route is ranked or the shortlist "
            "has no complete selection"
        ),
        "min_splits": sor.MIN_SPLITS,
        "evidence": [
            "tests/routing/test_uni_sor_fast.py",
            "docs/references/latency-l06-sor-shortlist.md",
        ],
    }
)


class UniSorFastConfigError(ValueError):
    """`prepare` received invalid or missing `search.*` / `shortlist.*` settings."""


@dataclass(frozen=True)
class ShortlistSettings:
    probe_percents: tuple[int, ...]
    routes_per_probe: int
    direct_routes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_percents": list(self.probe_percents),
            "routes_per_probe": self.routes_per_probe,
            "direct_routes": self.direct_routes,
        }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _settings(params: Mapping[str, Any], percent_step: int) -> ShortlistSettings:
    """The same rules as the profile loader (`benchmark.profile._parse_shortlist`),
    re-checked because `prepare` may be called without a profile."""
    missing = [k for k in SHORTLIST_PARAMS if k not in params]
    if missing:
        raise UniSorFastConfigError(f"{NAME}: missing shortlist settings {missing} (no default)")
    probes, k, d = params["probe_percents"], params["routes_per_probe"], params["direct_routes"]
    if not _is_int(k) or k < 1:
        raise UniSorFastConfigError(f"{NAME}: routes_per_probe must be an int >= 1, got {k!r}")
    if not _is_int(d) or d < 0:
        raise UniSorFastConfigError(f"{NAME}: direct_routes must be an int >= 0, got {d!r}")
    if not isinstance(probes, (list, tuple)) or not probes:
        raise UniSorFastConfigError(f"{NAME}: probe_percents must be a non-empty list")
    grid = set(range(percent_step, 101, percent_step))
    if any(not _is_int(p) or p not in grid for p in probes) or len(set(probes)) != len(probes):
        raise UniSorFastConfigError(
            f"{NAME}: probe_percents {list(probes)!r} must be distinct percents of the "
            f"{percent_step}% grid"
        )
    if 100 not in probes:
        raise UniSorFastConfigError(f"{NAME}: probe_percents must include 100")
    return ShortlistSettings(tuple(sorted(probes)), k, d)


@dataclass(frozen=True)
class PreparedUniSorFast:
    """`uni_sor_port`'s own preparation (cohort lists, universe, parameters) plus the
    validated shortlist settings."""

    port: sor.PreparedUniSorPort
    settings: ShortlistSettings


def prepare(
    bundle: SnapshotBundle, config: AlgorithmConfig, *, catalog: Any = sor.DEFAULT_CATALOG
) -> PreparedUniSorFast:
    search = {k: config.params[k] for k in sor.SEARCH_PARAMS if k in config.params}
    try:
        port = sor.prepare(bundle, AlgorithmConfig(sor.NAME, search), catalog=catalog)
    except sor.UniSorPortConfigError as exc:
        raise UniSorFastConfigError(f"{NAME}: {exc}") from exc
    return PreparedUniSorFast(port, _settings(config.params, port.percent_step))


@dataclass(frozen=True)
class Shortlist:
    """The searched routes per family (in enumeration order) and how they were chosen."""

    routes: Mapping[str, tuple[sor.SorRoute, ...]]
    ranked: int  # routes with at least one valid probe entry
    by_probe: Mapping[int, int]  # probe percent -> routes it contributed (top-K)
    direct_retained: int  # one-hop routes added by `direct_routes` beyond the top-K

    @property
    def size(self) -> int:
        return sum(len(rs) for rs in self.routes.values())


def shortlist_routes(
    routes: Mapping[str, Sequence[sor.SorRoute]],
    probe_quotes: Sequence[sor.RouteQuote],
    settings: ShortlistSettings,
) -> Shortlist:
    """Deterministic amount-aware shortlist (module docstring steps 2-3). Ties are broken
    by the reference quote-list order (B-Q1: family V3, V2, MIXED, then DFS order)."""
    order = {r: i for i, r in enumerate(r for fam in sor.FAMILIES for r in routes.get(fam, ()))}
    keep: set[sor.SorRoute] = set()
    best_rank: dict[sor.SorRoute, int] = {}
    by_probe: dict[int, int] = {}
    for percent in settings.probe_percents:
        ranked = sorted(
            (rq for rq in probe_quotes if rq.percent == percent),
            key=lambda rq: (-rq.quote_adjusted_for_gas, order[rq.route]),
        )
        for rank, rq in enumerate(ranked):
            best_rank[rq.route] = min(best_rank.get(rq.route, rank), rank)
        top = [rq.route for rq in ranked[: settings.routes_per_probe]]
        by_probe[percent] = len(top)
        keep.update(top)
    directs = sorted(
        (r for r in best_rank if len(r.pools) == 1), key=lambda r: (best_rank[r], order[r])
    )
    extra = [r for r in directs[: settings.direct_routes] if r not in keep]
    keep.update(extra)
    return Shortlist(
        routes=MappingProxyType(
            {fam: tuple(r for r in routes.get(fam, ()) if r in keep) for fam in sor.FAMILIES}
        ),
        ranked=len(best_rank),
        by_probe=MappingProxyType(by_probe),
        direct_retained=len(extra),
    )


class _BudgetExhausted(Exception):
    pass


def _route_label(route: sor.SorRoute) -> str:
    return f"{route.protocol}:{'>'.join(route.pool_ids)}"


def solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    prepared = context.prepared
    if not isinstance(prepared, PreparedUniSorFast):
        raise TypeError(f"{NAME}.solve needs the PreparedUniSorFast returned by prepare()")
    port, settings = prepared.port, prepared.settings
    bundle = context.bundle
    cache = QuoteCache(bundle)
    params = {
        "max_hops": port.max_hops,
        "max_splits": port.max_splits,
        "min_splits": sor.MIN_SPLITS,
        "percent_step": port.percent_step,
    }
    shortlist_stats: dict[str, Any] = {
        "settings": settings.to_dict(),
        "eligible_routes": {},
        "eligible_pools": None,
        "probe_entries": 0,
        "probe_entries_null": 0,
        "probe_entries_incomplete": 0,
        "ranked_routes": None,
        "routes_by_probe": None,
        "direct_retained": None,
        # The initial shortlist chosen by the probes (planned, before any table search).
        "shortlisted": {"routes": None, "pools": None, "route_ids": None},
        # Coverage of the last COMPLETED table search (shortlist, or the full-table
        # fallback once it completed); None while no table search has completed. A
        # budget-interrupted table never counts as searched.
        "searched_routes": None,
        "skipped_routes": None,
        "searched_pools": None,
        "skipped_pools": None,
        "searched_route_ids": None,
        "fallback": {"triggered": False, "reason": None, "completed": None},
        "quotes": {"probe": 0, "shortlist_table": 0, "fallback_table": 0, "validation": 0},
    }
    stats: dict[str, Any] = {
        "sor_fast": {
            "reference": REFERENCE,
            "experimental": True,
            "search_approximation": True,
            "upstream_commit": sor.UPSTREAM["commit"],
            "contract": sor.CONTRACT,
            "gas_score_provider": sor.GAS_SCORE_PROVIDER,
            "quote_path": QUOTE_PATH,
            "params": params,
        },
        # Planned scope of the last table search started, and whether it completed
        # (quoted in full and combined). `search_scope` alone is not completed coverage.
        "search_scope": None,
        "search_completed": False,
        "shortlist": shortlist_stats,
        "coverage_mode": port.coverage_mode,
        "cohort_pools": {sor.V3: len(port.v3_pools), sor.V2: len(port.v2_pools)},
        "excluded_pools": dict(port.excluded_by_source),
        "routes_enumerated": {},
        "quote_entries": 0,
        "quote_entries_null": 0,
        "entry_failures": {},
        "entries_incomplete": 0,
        "incomplete_example": None,
        "route_quotes": 0,
        "selection": None,
        "allocation": None,
        "d1_residual": None,
        "cached_quote": None,
        "evaluated_gross": None,
        "requote_delta": None,
        "truncated_by": None,
        "quotes_executed": 0,
        "quotes_memoized": 0,
    }
    phase_start = 0

    def charge(phase: str) -> None:
        nonlocal phase_start
        shortlist_stats["quotes"][phase] = cache.misses - phase_start
        phase_start = cache.misses

    def result(status: SolveStatus, **kw: Any) -> SolveResult:
        stats["quotes_executed"] = cache.misses
        stats["quotes_memoized"] = cache.hits
        return SolveResult(
            case_id=case.case_id, algorithm=NAME, status=status, search_stats=stats, **kw
        )

    # ---- Enumeration: the reference's (B-R*, A-1), so enumeration statuses are its own.
    routes = {
        fam: sor.compute_family_routes(
            fam, case.token_in, case.token_out, port.v3_pools, port.v2_pools, port.max_hops
        )
        for fam in sor.FAMILIES
    }
    stats["routes_enumerated"] = {fam: len(rs) for fam, rs in routes.items()}
    shortlist_stats["eligible_routes"] = dict(stats["routes_enumerated"])
    eligible_pools = {pid for rs in routes.values() for r in rs for pid in r.pool_ids}
    shortlist_stats["eligible_pools"] = len(eligible_pools)
    n_routes = sum(len(rs) for rs in routes.values())
    if n_routes == 0:
        full = sor.compute_all_routes(case.token_in, case.token_out, port.universe, port.max_hops)
        stats["universe_routes"] = len(full)
        if full:
            return result(
                SolveStatus.UNSUPPORTED,
                error=(
                    f"no route within {port.max_hops} hops over the SOR-compatible cohort, "
                    f"but {len(full)} over all admitted pools: reachable only through pools "
                    f"without an SOR protocol ({dict(port.excluded_by_source)}; D-4)"
                ),
            )
        return result(
            SolveStatus.NO_ROUTE,
            error=f"no route within {port.max_hops} hops over any admitted pool",
        )

    percents, amounts = sor.amount_distribution(case.amount_in, port.percent_step)
    if budget.max_candidates is not None and n_routes > budget.max_candidates:
        stats["truncated_by"] = "max_candidates"
        return result(
            SolveStatus.TIMEOUT,
            candidates_considered=0,
            candidates_truncated=n_routes,
            error=(
                f"declared max_candidates={budget.max_candidates} is below the {n_routes} "
                "enumerated routes the shortlist must rank -- not evidence of no_route"
            ),
        )

    # ---- A-2 quote rules, identical to uni_sor_port's (null reasons counted per phase).
    def guarded(state: PoolState, token_in: str, amount: int) -> SwapResult[PoolState]:
        memo_hit = bundle.pools.get(state.pool_id) is state and cache.cached(
            state.pool_id, token_in, amount
        )
        if budget.max_quotes is not None and cache.misses >= budget.max_quotes and not memo_hit:
            raise _BudgetExhausted
        return cache(state, token_in, amount)

    legs: dict[sor.SorRoute, tuple[Edge, ...]] = {}

    def quoter(failures: dict[str, int], incomplete: list[str]) -> sor.QuoteFn:
        def fail(status: str) -> None:
            failures[status] = failures.get(status, 0) + 1

        def quote(route: sor.SorRoute, percent: int, amount: sor.Rational) -> int | None:
            x = amount.quotient
            if x == 0:
                fail("zero_input")
                return None
            pure_v2 = all(p.protocol == sor.V2 for p in route.pools)
            if route not in legs:
                legs[route] = sor._plan_legs(route, bundle)
            for edge in legs[route]:
                if x == 0:
                    fail("zero_intermediate")
                    return None
                res = guarded(bundle.pools[edge.pool_id], edge.token_in, x)
                if res.status is not QuoteStatus.OK:
                    fail(res.status.value)
                    if res.status is QuoteStatus.INCOMPLETE_SNAPSHOT:
                        incomplete.append(f"{'>'.join(route.pool_ids)} @ {percent}%: {res.detail}")
                    return None
                if res.amount_in_consumed != x:
                    fail("partial_fill")
                    return None
                x = res.amount_out
                if pure_v2 and x == 0:
                    fail("v2_zero_output")
                    return None
            return x

        return quote

    def timeout(where: str) -> SolveResult:
        stats["truncated_by"] = "max_quotes"
        return result(
            SolveStatus.TIMEOUT,
            candidates_considered=n_routes,
            error=(
                f"declared max_quotes={budget.max_quotes} reached during the {where} -- a "
                "partial table is not SOR's input, not evidence of no_route"
            ),
        )

    # ---- 1-3: probe every enumerated route, rank, shortlist.
    probe_amounts = [amounts[percents.index(p)] for p in settings.probe_percents]
    probe_failures: dict[str, int] = {}
    probe_incomplete: list[str] = []
    try:
        probe_quotes = sor.build_route_quotes(
            routes, settings.probe_percents, probe_amounts, quoter(probe_failures, probe_incomplete)
        )
    except _BudgetExhausted:
        charge("probe")
        return timeout("shortlist probes")
    charge("probe")
    n_probe = n_routes * len(settings.probe_percents)
    shortlist_stats["probe_entries"] = n_probe
    shortlist_stats["probe_entries_null"] = n_probe - len(probe_quotes)
    shortlist_stats["probe_entries_incomplete"] = len(probe_incomplete)
    short = shortlist_routes(routes, probe_quotes, settings)

    def pools_of(table_routes: Mapping[str, Sequence[sor.SorRoute]]) -> set[str]:
        return {pid for rs in table_routes.values() for r in rs for pid in r.pool_ids}

    def route_ids(table_routes: Mapping[str, Sequence[sor.SorRoute]]) -> list[str]:
        return [_route_label(r) for fam in sor.FAMILIES for r in table_routes.get(fam, ())]

    shortlist_stats.update(
        ranked_routes=short.ranked,
        routes_by_probe={str(p): n for p, n in short.by_probe.items()},
        direct_retained=short.direct_retained,
        shortlisted={
            "routes": {fam: len(rs) for fam, rs in short.routes.items()},
            "pools": len(pools_of(short.routes)),
            "route_ids": route_ids(short.routes),
        },
    )
    whole = short.size == n_routes

    # ---- 4: the unchanged SOR core on the shortlisted routes.
    def search(
        table_routes: Mapping[str, Sequence[sor.SorRoute]], phase: str
    ) -> sor.SwapSelection | None:
        failures: dict[str, int] = {}
        incomplete: list[str] = []
        route_quotes = sor.build_route_quotes(
            table_routes, percents, amounts, quoter(failures, incomplete)
        )
        n_entries = sum(len(rs) for rs in table_routes.values()) * len(percents)
        stats["quote_entries"] = n_entries
        stats["quote_entries_null"] = n_entries - len(route_quotes)
        stats["entry_failures"] = dict(sorted(failures.items()))
        stats["entries_incomplete"] = len(incomplete)
        stats["incomplete_example"] = incomplete[0] if incomplete else None
        stats["route_quotes"] = len(route_quotes)
        selection = sor.get_best_swap_route(
            case.amount_in, percents, route_quotes, max_splits=port.max_splits
        )
        charge(phase)
        searched = pools_of(table_routes)
        shortlist_stats.update(
            searched_routes={fam: len(table_routes.get(fam, ())) for fam in sor.FAMILIES},
            skipped_routes={
                fam: len(routes[fam]) - len(table_routes.get(fam, ())) for fam in sor.FAMILIES
            },
            searched_pools=len(searched),
            skipped_pools=len(eligible_pools - searched),
            searched_route_ids=route_ids(table_routes),
        )
        stats["search_completed"] = True
        return selection

    selection: sor.SwapSelection | None = None
    try:
        if short.size:
            stats["search_scope"] = "full_cohort" if whole else "shortlist"
            selection = search(short.routes, "shortlist_table")
        if selection is None and not whole:
            # ---- 5: deterministic full-table fallback, charged in this solve.
            shortlist_stats["fallback"] = {
                "triggered": True,
                "reason": "no_ranked_route" if not short.size else "no_shortlist_selection",
                "completed": False,
            }
            stats["search_scope"] = "full_cohort_fallback"
            stats["search_completed"] = False
            selection = search(routes, "fallback_table")
            shortlist_stats["fallback"]["completed"] = True
    except _BudgetExhausted:
        in_fallback = shortlist_stats["fallback"]["triggered"]
        charge("fallback_table" if in_fallback else "shortlist_table")
        return timeout(
            "full-table fallback quote table" if in_fallback else "shortlist quote table"
        )
    candidates_truncated = 0 if stats["search_scope"] != "shortlist" else n_routes - short.size

    if selection is None:
        if stats["entries_incomplete"]:
            return result(
                SolveStatus.INCOMPLETE_SNAPSHOT,
                candidates_considered=n_routes,
                error=(
                    "no complete selection over the full cohort table; entries need "
                    f"uncollected state, e.g. {stats['incomplete_example']}"
                ),
            )
        return result(
            SolveStatus.NO_ROUTE,
            candidates_considered=n_routes,
            error=(
                f"no complete selection over the full cohort table ({stats['route_quotes']} "
                "valid quote entries; B-S10)"
            ),
        )
    stats["selection"] = selection.to_dict()

    # ---- D-1 integer fill; D-3 in-solve replay (charged), then the runner's own replay.
    allocation = sor.integer_fill(case.amount_in, selection)
    stats["allocation"] = [str(a) for a in allocation]
    stats["d1_residual"] = str(allocation[-1] - selection.amounts[-1].quotient)
    plan = split_path_plan(
        case,
        tuple(legs[r.route] for r in selection.routes),
        tuple(r.percent for r in selection.routes),
    )
    context.report_candidate(plan)
    try:
        evaluation = evaluate(bundle, case, plan, context.objective, quote=guarded)
    except _BudgetExhausted:
        charge("validation")
        return timeout("plan replay")
    charge("validation")
    stats["cached_quote"] = str(selection.swap.quote)
    if evaluation.status is not EvalStatus.OK:
        return result(
            SolveStatus.INVALID_PLAN,
            plan=plan,
            evaluation=evaluation,
            candidates_considered=n_routes,
            candidates_truncated=candidates_truncated,
            error=evaluation.error,
        )
    stats["evaluated_gross"] = str(evaluation.gross_output)
    stats["requote_delta"] = str(evaluation.gross_output - selection.swap.quote)
    return result(
        SolveStatus.OK,
        plan=plan,
        evaluation=evaluation,
        score=context.objective.score(evaluation),
        candidates_considered=n_routes,
        candidates_truncated=candidates_truncated,
    )


FACTORY = AlgorithmFactory(
    name=NAME,
    solve=solve,
    prepare=prepare,
    capabilities=sor.CAPABILITIES,
    search_params=sor.SEARCH_PARAMS,
    provenance=PROVENANCE,
    shortlist_params=SHORTLIST_PARAMS,
)

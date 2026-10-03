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

**Adaptive percentage sampling (WHI-1509 / research key L07), explicit opt-in.** Only when
the profile declares the `sampling` section (`coarse_step`, `refine_radius`,
`soft_max_quotes`, all explicit; `PreparedUniSorFast.sampling`) does a table search stop
quoting every grid percent. Without it the solve above runs unchanged, with identical
metadata. With it, each table search (shortlist, or full-cohort fallback) is:

a. **Coarse.** Every searched route at the grid percents that are multiples of
   `coarse_step` (100 always among them). The unchanged SOR core combines this partial
   table (a missing percent group is simply absent, as B-S4/B-S6 already allow), so its
   B-S3 baseline makes the best full-input single route the first incumbent.
b. **Incumbent.** First the simpler full-input incumbent is seeded: the sampled 100 %
   entries in B-S2 order, each as the core's single-route selection, until one replays
   valid. Then every new combined selection is considered: a combinatorial selection is
   not an incumbent until it is D-1 integer-filled, built as a pool-disjoint plan that
   funds the whole input (`split_path_plan` refuses shared pools) and replayed by the
   evaluator (charged as `validation`) -- its fill can fail where the full-input route
   does not. Only a valid plan is published through `report_candidate`, and only when it
   scores strictly better (a combined selection also replaces an equal-scoring seed).
c. **Refine.** Around the percents of the incumbent and of the latest selection, the fine
   percents `p +/- j*percent_step` and the freed shares `j*percent_step`
   (`j <= refine_radius`) are added for every searched route, and the core re-combines
   the grown table (canonical B-Q1 order, so ties resolve as in the reference). This
   repeats until no new percent is proposed (`converged`: a local fixed point, not a
   global optimum) or, before a round, the solve's executed quotes (all phases) reach
   `soft_max_quotes` (`soft_limit`: status `ok` with the valid incumbent, labelled
   `truncated_by: soft_max_quotes`). The soft cap only stops with a **valid** incumbent,
   never with a mere (possibly rejected) selection.
d. **Grid completion.** If the sampled table yields no complete selection, or refinement
   has converged while every selection was rejected, the rest of the full grid is quoted
   (charged, same phase) before anything is concluded. No selection over the full grid:
   the L06 fallback and `no_route` / `incomplete_snapshot` rules above apply unchanged.
   Selections but no valid incumbent over the full grid (`no_valid_incumbent_full_grid`):
   `invalid_plan` for the last rejected plan, which is never published.
e. **Hard limits** stay truthful: `max_quotes` gives `timeout` with no plan (the last valid
   incumbent is kept only as labelled metadata and through the candidate sink).

With `coarse_step == percent_step` the first table is the full grid and the result is the
L06 result whenever the combined selection replays valid and scores at least the seed
(always under a gross objective; a net objective with gas costs may prefer the seed).
The L06 work bound above does not hold in general here: sampled entries are a subset of
the reference table, but several incumbents may be replayed, so the evidence
counts quotes against the reference per case instead of assuming the bound. Otherwise the
search is a second, declared approximation (`search.sor_fast.sampling_approximation:
true`; per-table entry coverage and rounds in `search.sampling`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
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
    refuse_options,
)
from routing.algorithms.path_split import split_path_plan
from routing.evaluator import EvalStatus, evaluate
from routing.search import Edge, QuoteCache
from snapshot.models import Case, PoolState, SnapshotBundle

NAME = "uni_sor_fast"
REFERENCE = sor.NAME
SHORTLIST_PARAMS = ("probe_percents", "routes_per_probe", "direct_routes")
SAMPLING_PARAMS = ("coarse_step", "refine_radius", "soft_max_quotes")
SAMPLING_APPROXIMATION = (
    "coarse-to-fine percentage sampling (L07): the SOR core combines only the sampled "
    "(route, percent) entries -- a coarse grid, then fine entries next to the incumbent's "
    "allocation -- so an optimum between samples can be missed; a fixed point or a soft-cap "
    "stop is a local result over the sampled table, not the full-grid search"
)
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
class SamplingSettings:
    coarse_step: int
    refine_radius: int
    soft_max_quotes: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "coarse_step": self.coarse_step,
            "refine_radius": self.refine_radius,
            "soft_max_quotes": self.soft_max_quotes,
        }


def _sampling_settings(params: Mapping[str, Any], percent_step: int) -> SamplingSettings | None:
    """None when no `sampling.*` key is given (the L06 behaviour); otherwise all keys, by
    the profile loader's rules (`benchmark.profile._parse_sampling`)."""
    given = [k for k in SAMPLING_PARAMS if k in params]
    if not given:
        return None
    missing = [k for k in SAMPLING_PARAMS if k not in params]
    if missing:
        raise UniSorFastConfigError(f"{NAME}: sampling settings are all-or-none; missing {missing}")
    coarse, radius, soft = (params[k] for k in SAMPLING_PARAMS)
    if not _is_int(coarse) or coarse < 1 or coarse % percent_step or 100 % coarse:
        raise UniSorFastConfigError(
            f"{NAME}: coarse_step {coarse!r} must be a multiple of percent_step "
            f"({percent_step}) that divides 100"
        )
    if not _is_int(radius) or radius < 1:
        raise UniSorFastConfigError(f"{NAME}: refine_radius must be an int >= 1, got {radius!r}")
    if soft is not None and (not _is_int(soft) or soft < 1):
        raise UniSorFastConfigError(
            f"{NAME}: soft_max_quotes must be an int >= 1 or null, got {soft!r}"
        )
    return SamplingSettings(coarse, radius, soft)


def refine_percents(selected: Iterable[int], step: int, radius: int) -> set[int]:
    """Step c's proposal: for each selected percent `p`, the grid percents `p +/- j*step`
    and the freed share `j*step` (another route may take it), `1 <= j <= radius`, kept
    inside `[step, 100]`."""
    out: set[int] = set()
    for p in selected:
        for j in range(1, radius + 1):
            out.update(q for q in (p - j * step, p + j * step, j * step) if step <= q <= 100)
    return out


@dataclass(frozen=True)
class PreparedUniSorFast:
    """`uni_sor_port`'s own preparation (cohort lists, universe, parameters) plus the
    validated shortlist settings and, only when declared, the sampling settings."""

    port: sor.PreparedUniSorPort
    settings: ShortlistSettings
    sampling: SamplingSettings | None = None


def prepare(
    bundle: SnapshotBundle, config: AlgorithmConfig, *, catalog: Any = sor.DEFAULT_CATALOG
) -> PreparedUniSorFast:
    refuse_options(config)  # WHI-1548: explicit options are refused, never ignored
    search = {k: config.params[k] for k in sor.SEARCH_PARAMS if k in config.params}
    try:
        port = sor.prepare(bundle, AlgorithmConfig(sor.NAME, search), catalog=catalog)
    except sor.UniSorPortConfigError as exc:
        raise UniSorFastConfigError(f"{NAME}: {exc}") from exc
    return PreparedUniSorFast(
        port,
        _settings(config.params, port.percent_step),
        _sampling_settings(config.params, port.percent_step),
    )


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
    port, settings, sampling = prepared.port, prepared.settings, prepared.sampling
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
    # L07: present only when the profile declared the `sampling` section, so an L06 solve's
    # metadata is unchanged. Describes the last table search started (its `completed` flag
    # says whether it finished); entry coverage counts completed rounds only.
    sampling_stats: dict[str, Any] = {}
    if sampling is not None:
        stats["sor_fast"]["sampling_approximation"] = True
        sampling_stats = {
            "research_key": "L07",
            "issue": "WHI-1509",
            "approximation": SAMPLING_APPROXIMATION,
            "settings": sampling.to_dict(),
        }
        stats["sampling"] = sampling_stats
    phase_start = 0

    def charge(phase: str) -> None:
        nonlocal phase_start
        shortlist_stats["quotes"][phase] += cache.misses - phase_start
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

    # ---- L07 (only with `sampling`): coarse-to-fine table, validated anytime incumbent.
    incumbent: dict[str, Any] | None = None  # the best validated, published plan
    rejected: dict[str, Any] | None = None  # the last selection whose plan failed validation

    def validate(selection: sor.SwapSelection, phase: str) -> dict[str, Any]:
        """Step b: build and replay the selection's plan. Returns the candidate with
        `error` None only when it is complete, funded, pool-disjoint and replays ok."""
        allocation = sor.integer_fill(case.amount_in, selection)
        out: dict[str, Any] = {"selection": selection, "allocation": allocation, "plan": None,
                               "evaluation": None, "score": None, "error": None}  # fmt: skip
        if sum(allocation) != case.amount_in or any(a <= 0 for a in allocation):
            out["error"] = f"allocation {list(allocation)} does not fund the input"
            return out
        try:
            out["plan"] = split_path_plan(
                case,
                tuple(legs[r.route] for r in selection.routes),
                tuple(r.percent for r in selection.routes),
            )
        except ValueError as exc:  # shared physical pool or disconnected leg
            out["error"] = f"plan construction refused: {exc}"
            return out
        charge(phase)
        sampling_stats["validations"] += 1
        try:
            evaluation = evaluate(bundle, case, out["plan"], context.objective, quote=guarded)
        finally:
            charge("validation")
        out["evaluation"] = evaluation
        if evaluation.status is not EvalStatus.OK:
            out["error"] = evaluation.error or evaluation.status.value
            return out
        out["score"] = context.objective.score(evaluation)
        return out

    def summary(cand: dict[str, Any], round_index: int, source: str) -> dict[str, Any]:
        return {
            "round": round_index,
            "source": source,
            "selection": [
                {"route_id": _route_label(r.route), "percent": r.percent}
                for r in cand["selection"].routes
            ],
            "allocation": [str(a) for a in cand["allocation"]],
            "evaluated_gross": str(cand["evaluation"].gross_output),
            "score": str(cand["score"]),
            "validated": True,
        }

    def sampled_search(
        table_routes: Mapping[str, Sequence[sor.SorRoute]], phase: str, quote: sor.QuoteFn
    ) -> tuple[sor.SwapSelection | None, list[sor.RouteQuote], int]:
        """Steps a-d over one table scope; returns the incumbent's selection (or the last
        selection when none validated), the final sampled table and its entry count."""
        assert sampling is not None
        step = port.percent_step
        memo: dict[tuple[sor.SorRoute, int], int | None] = {}

        def entry(route: sor.SorRoute, percent: int, amount: sor.Rational) -> int | None:
            if (route, percent) not in memo:
                memo[(route, percent)] = quote(route, percent, amount)
            return memo[(route, percent)]

        n_scope = sum(len(rs) for rs in table_routes.values())
        coarse = [p for p in percents if p % sampling.coarse_step == 0]
        sampling_stats.update(
            scope=stats["search_scope"],
            completed=False,
            grid_percents=len(percents),
            grid_entries=n_scope * len(percents),
            coarse_percents=coarse,
            sampled_percents=[],
            sampled_entries=0,
            skipped_entries=n_scope * len(percents),
            rounds=[],
            stop_reason=None,
            soft_limit={
                "max_quotes": sampling.soft_max_quotes,
                "reached": False,
                "quotes_at_stop": None,
            },
            grid_completion={"triggered": False, "completed": None, "reason": None},
            seed=None,
            validations=0,
            rejected_incumbents=0,
            rejection_errors=[],
            incumbent=None,
        )
        rounds: list[dict[str, Any]] = sampling_stats["rounds"]
        sampled: set[int] = set()
        table: list[sor.RouteQuote] = []
        seen: set[tuple[tuple[sor.SorRoute, int], ...]] = set()

        def run_round(new: set[int], kind: str) -> sor.SwapSelection | None:
            nonlocal table
            start = cache.misses
            grid = sorted(sampled | new)
            table = sor.build_route_quotes(
                table_routes, grid, [amounts[percents.index(p)] for p in grid], entry
            )
            sampled.update(new)  # only now: an interrupted round's entries never count
            sel = sor.get_best_swap_route(
                case.amount_in, percents, table, max_splits=port.max_splits
            )
            sampling_stats.update(
                sampled_percents=grid,
                sampled_entries=n_scope * len(grid),
                skipped_entries=n_scope * (len(percents) - len(grid)),
            )
            rounds.append({
                "round": len(rounds),
                "kind": kind,
                "added_percents": sorted(new),
                "added_entries": n_scope * len(new),
                "table_quotes": cache.misses - start,
                "selection": None if sel is None else [
                    f"{_route_label(r.route)}@{r.percent}" for r in sel.routes],
                "incumbent": None,
            })  # fmt: skip
            return sel

        def consider(sel: sor.SwapSelection, source: str = "sor_selection") -> str:
            """Validate `sel` and keep it if it beats the incumbent. A combined SOR
            selection also replaces an equal-scoring full-input seed (the seed is only
            the safety net). Returns the outcome recorded for the round."""
            nonlocal incumbent, rejected
            key = tuple((r.route, r.percent) for r in sel.routes)
            if key in seen:
                return "unchanged"
            seen.add(key)
            cand = validate(sel, phase)
            if cand["error"] is not None:
                rejected = cand
                sampling_stats["rejected_incumbents"] += 1
                sampling_stats["rejection_errors"].append(cand["error"])
                return "rejected"
            tie_over_seed = (
                incumbent is not None
                and source == "sor_selection"
                and sampling_stats["incumbent"]["source"] == "full_input_seed"
                and cand["score"] == incumbent["score"]
            )
            if incumbent is None or cand["score"] > incumbent["score"] or tie_over_seed:
                incumbent = cand
                sampling_stats["incumbent"] = summary(cand, len(rounds) - 1, source)
                context.report_candidate(cand["plan"])  # published only after validation
                return "improved"
            return "not_better"

        def seed() -> None:
            """The simpler full-input incumbent, validated before any refinement: the
            sampled 100 % entries in B-S2 order (quote desc, stable), each as the core's
            single-route selection, until one replays valid. The combined selection's
            integer fill can fail where a full-input route does not."""
            full = [rq for rq in table if rq.percent == 100]
            for rq in sorted(full, key=lambda r: -r.quote_adjusted_for_gas):
                one = sor.get_best_swap_route(
                    case.amount_in, percents, [rq], max_splits=port.max_splits
                )
                assert one is not None  # a lone 100 % entry is the B-S3 baseline
                outcome = consider(one, "full_input_seed")
                sampling_stats["seed"] = {"route_id": _route_label(rq.route), "outcome": outcome}
                if outcome != "rejected":
                    return

        def complete_grid(reason: str) -> sor.SwapSelection | None:
            sampling_stats["grid_completion"].update(triggered=True, completed=False, reason=reason)
            sel = run_round(set(percents) - sampled, "grid_completion")
            sampling_stats["grid_completion"]["completed"] = True
            return sel

        latest = run_round(set(coarse), "coarse")
        seed()
        if latest is not None:
            rounds[-1]["incumbent"] = consider(latest)
        while True:
            if incumbent is None and latest is None:
                if sampled == set(percents):
                    sampling_stats["stop_reason"] = "no_selection_full_grid"
                    break
                latest = complete_grid("no_selection")
                if latest is not None:
                    rounds[-1]["incumbent"] = consider(latest)
                continue
            basis = {r.percent for r in latest.routes} if latest is not None else set()
            if incumbent is not None:
                basis |= {r.percent for r in incumbent["selection"].routes}
            new = refine_percents(basis, step, sampling.refine_radius) - sampled
            if not new:
                if incumbent is None and sampled != set(percents):
                    # Selections exist but none replayed valid: no stop without a valid
                    # incumbent; the rest of the grid is searched first (charged).
                    latest = complete_grid("no_valid_incumbent")
                    if latest is not None:
                        rounds[-1]["incumbent"] = consider(latest)
                    continue
                sampling_stats["stop_reason"] = (
                    "converged" if incumbent is not None else "no_valid_incumbent_full_grid"
                )
                break
            soft = sampling.soft_max_quotes
            if soft is not None and cache.misses >= soft and incumbent is not None:
                # The soft cap only ever stops with a VALID incumbent in hand.
                sampling_stats["stop_reason"] = "soft_limit"
                sampling_stats["soft_limit"]["reached"] = True
                break
            latest = run_round(new, "refine")
            if latest is not None:
                rounds[-1]["incumbent"] = consider(latest)
        sampling_stats["soft_limit"]["quotes_at_stop"] = cache.misses
        sampling_stats["completed"] = True
        best = incumbent["selection"] if incumbent is not None else latest
        return best, table, n_scope * len(sampled)

    # ---- 4: the unchanged SOR core on the shortlisted routes.
    def search(
        table_routes: Mapping[str, Sequence[sor.SorRoute]], phase: str
    ) -> sor.SwapSelection | None:
        failures: dict[str, int] = {}
        incomplete: list[str] = []
        if sampling is None:
            route_quotes = sor.build_route_quotes(
                table_routes, percents, amounts, quoter(failures, incomplete)
            )
            n_entries = sum(len(rs) for rs in table_routes.values()) * len(percents)
            selection = sor.get_best_swap_route(
                case.amount_in, percents, route_quotes, max_splits=port.max_splits
            )
        else:
            selection, route_quotes, n_entries = sampled_search(
                table_routes, phase, quoter(failures, incomplete)
            )
        stats["quote_entries"] = n_entries
        stats["quote_entries_null"] = n_entries - len(route_quotes)
        stats["entry_failures"] = dict(sorted(failures.items()))
        stats["entries_incomplete"] = len(incomplete)
        stats["incomplete_example"] = incomplete[0] if incomplete else None
        stats["route_quotes"] = len(route_quotes)
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
        where = "full-table fallback quote table" if in_fallback else "shortlist quote table"
        if sampling is not None:
            sampling_stats["stop_reason"] = "hard_limit"
            sampling_stats["soft_limit"]["quotes_at_stop"] = cache.misses
            where = f"sampled {where}" + (
                " (the last valid incumbent is kept as search.sampling.incumbent, not as "
                "a completed solve)"
                if incumbent is not None
                else " (no valid incumbent yet)"
            )
        return timeout(where)
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

    if sampling is not None:
        # The incumbent was replayed (charged) before it was published; nothing is
        # re-quoted here. No valid incumbent: the last rejected plan, never published.
        cand = incumbent or rejected
        assert cand is not None
        stats["allocation"] = [str(a) for a in cand["allocation"]]
        stats["d1_residual"] = str(cand["allocation"][-1] - selection.amounts[-1].quotient)
        stats["cached_quote"] = str(selection.swap.quote)
        if incumbent is None:
            return result(
                SolveStatus.INVALID_PLAN,
                plan=cand["plan"],
                evaluation=cand["evaluation"],
                candidates_considered=n_routes,
                candidates_truncated=candidates_truncated,
                error=cand["error"],
            )
        evaluation = incumbent["evaluation"]
        stats["evaluated_gross"] = str(evaluation.gross_output)
        stats["requote_delta"] = str(evaluation.gross_output - selection.swap.quote)
        if sampling_stats["soft_limit"]["reached"]:
            stats["truncated_by"] = "soft_max_quotes"
        return result(
            SolveStatus.OK,
            plan=incumbent["plan"],
            evaluation=evaluation,
            score=incumbent["score"],
            candidates_considered=n_routes,
            candidates_truncated=candidates_truncated,
        )

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
    sampling_params=SAMPLING_PARAMS,
)

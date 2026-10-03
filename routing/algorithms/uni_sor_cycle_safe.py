# SPDX-License-Identifier: GPL-3.0-only
#
# `Admission.choose` is adapted from routing/algorithms/uni_sor_port.py
# `find_first_route_not_using_used_pools`, the translation of
# Uniswap/smart-order-router@04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647 (4.31.10)
# src/routers/alpha-router/functions/best-swap-route.ts:821-881 (the B-S7/B-S9 chooser of
# `getBestSwapRouteBy`, best-swap-route.ts:174-817). Copyright (C) Uniswap Labs. Licensed
# under the GNU General Public License v3; the GPL text at the pin is copied verbatim to
# docs/references/licenses/uniswap-smart-order-router-04c7c0b4-LICENSE.txt.
# MODIFIED (WHI-1556): the chooser additionally requires the plan token graph of the
# combination to stay acyclic and continues the scan past a rejected entry (CS-1); every
# other part of the pipeline is the unchanged `uni_sor_port` translation, imported, not
# copied. Private internal research use; not conveyed (uni-sor-port-contract.md §9.3).
"""`uni_sor_cycle_safe` (WHI-1556, R021-C/1 §2 row 4): `uni_sor_port`'s pipeline with one
added rule at SOR's only combination point -- a combination of routes is admitted only if the
union of their token edges is acyclic (the evaluator's plan-token-DAG rule). Contract of
record: `docs/references/research-021/cycle-safe-sor.md` (R021-P09, WHI-1555, outcome `go`),
§3-§8 and §13; its executable specification is `select_cycle_safe` / `spec_solve` in
`tests/routing/test_cycle_safe_sor_contract.py`. Experimental, `custom` group. **Not**
upstream parity; no quality, dominance (K5), optimality (K1) or timing claim.

**Pipeline.** `prepare` validates this identity's options (exactly `{}`) and then calls
`uni_sor_port.prepare` with the options cleared. `solve` runs `uni_sor_port.solve` itself --
enumeration, the `unsupported`/`no_route` rules, the `max_candidates` threshold, the complete
A-2 quote table through the guarded per-solve memo, B-S1 grouping, B-F1, B-F2, D-1 and the
D-3 replay with the guarded quote function -- with one explicit, default-preserving seam: its
`select` receives `uni_sor_port.get_best_swap_route_by` with `choose=Admission.choose`. So the
only new selector code is the chooser; baseline, seeds, layers, B-S5 pruning, split cap,
strict improvement and the cached totals are the port's own lines.

**Chooser (CS-1, memo §4.2).** Scan the B-S2-sorted percent group in order: an entry sharing
a pool identifier with the node's routes is skipped first and costs nothing (B-S9); every
pool-disjoint entry costs one `admission_checks` and is chosen iff the node's routes plus the
entry have an acyclic union of lowercase SOR token edges; otherwise it costs one
`combinations_rejected_cycle` and the scan continues. The B-S3 baseline and B-S4 seeds are
single simple routes (memo L1) and are not checked. No fallback, retry, shortlist or knob.

**Publication (CS-2, memo §4.4).** The port publishes its plan before the replay; here that
publication goes to a local buffer and the plan is published to the runner exactly once, only
after the in-solve replay returned `ok`. A replay cut or a non-`ok` replay publishes nothing
(`withheld_by_cs2`).

**Statuses (memo §4.5).** The port's, plus CS-3: no selection after at least one rejection is
`no_route` naming the admission, with `no_admissible_selection: true` -- the pinned search
ended by its own rules, not a proof that the V2/V3 domain has no valid plan.

**Phases (memo §4.7).** `search_stats["cycle_safe"]` records what actually ran: `selector`
(`completed` only when the adapted selector returned; otherwise `not_started` with the reason),
`replay`, `reference_trajectory` (`identical`: completed with zero rejections; `diverged`: a
rejection; `unavailable`: no selector ran -- never derived from the counter alone),
`comparable_completed` and `publication`. A hard kill returns no result at all (the runner
reports `unavailable (hard_timeout)`).

**Accounting.** The whole solve runs inside `routing.evaluator.counted_evaluations()`:
`internal_evaluations` is the exact number of in-solve replays; `quotes_executed` is the
port's `QuoteCache` miss count (the worker meter), `quotes_memoized` its hits;
`admission_checks` / `combinations_rejected_cycle` are CPU-only search units (admission costs
no quote). `search_stats["r021"]` is the `r021.diagnostics/1` record over the `r021.domain/1`
record that `uni_sor_port` shares (`same_domain`), with an `unknown`-kind certificate on `ok`
and none (`not_produced`) otherwise. Every `uni_sor_port` `search_stats` key is kept.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from routing.algorithms import uni_sor_port as sor
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
    settings_sha256,
    validated_options,
)
from routing.evaluator import counted_evaluations
from routing.plan import RoutePlan
from snapshot.models import Case, SnapshotBundle

NAME = "uni_sor_cycle_safe"
REFERENCE = sor.NAME

CAPABILITIES = sor.CAPABILITIES
SEARCH_PARAMS = sor.SEARCH_PARAMS  # ("max_hops", "max_splits", "percent_step")
MAX_CANDIDATES_UNIT = "enumerated_routes_threshold"
CONTRACT = "docs/references/research-021/cycle-safe-sor.md"
# The preset of memo §7 (R021-C/1 §7.1): the empty options, frozen by the file's bytes.
PRESET: dict[str, Any] = {
    "path": "config/uni_sor_cycle_safe/preset_v1.yaml",
    "sha256": "668e9d42d99b9ecce09fb044f827d4f0a4eec281cab401fb0cd74917557e739a",
    "key": NAME,
    "version": 1,
}
SOURCE_DEVIATIONS: Mapping[str, str] = MappingProxyType(
    {
        "CS-1": (
            "the B-S7/B-S9 chooser also requires the plan token union to be acyclic and "
            "continues the scan past a rejected entry (getBestSwapRouteBy, "
            "best-swap-route.ts:174-881; port get_best_swap_route_by)"
        ),
        "CS-2": (
            "the candidate is published only after the in-solve replay is ok; the reference "
            "publishes before its replay"
        ),
        "CS-3": (
            "no_route after rejections carries its own error text and no_admissible_selection"
        ),
    }
)
ADMISSION = "plan_token_dag at the B-S7/B-S9 chooser (continue scan)"
PUBLICATION_RULE = "after a completed ok replay (CS-2)"

PROVENANCE: Mapping[str, Any] = MappingProxyType(
    {
        "experimental": True,
        "opt_in": True,
        "issue": "WHI-1556",
        "identity": (
            "uni_sor_port's pipeline with plan-token-DAG admission at the B-S7/B-S9 chooser "
            "(pool conflicts skipped first, full token-edge union, continue scan)"
        ),
        "contract": (
            f"{CONTRACT} §3-§8, §13 (WHI-1555, R021-P09, go; PR #62 "
            "7f127e2876d541fd2c5794abcc7774ea981e11f0)"
        ),
        "reference": REFERENCE,
        "upstream": dict(sor.UPSTREAM),
        "port_contract": sor.CONTRACT,
        "adaptations": list(sor.ADAPTATIONS),
        "deviations": list(sor.DEVIATIONS),
        "source_deviations": dict(SOURCE_DEVIATIONS),
        "candidate_provider": sor.CANDIDATE_PROVIDER,
        "quote_provider": sor.QUOTE_PROVIDER,
        "gas_score_provider": sor.GAS_SCORE_PROVIDER,
        "min_splits": sor.MIN_SPLITS,
        "parity_evidence": (
            "none for this identity: the upstream goldens (tests/fixtures/uni_sor/) stay "
            "authoritative for uni_sor_port only; this identity follows the cycle-safe-sor.md "
            "fixtures and Theorems S/P (tests/routing/test_uni_sor_cycle_safe.py)"
        ),
        "not_claimed": [
            "upstream parity",
            "no worse than uni_sor_port (K5: admission changes B-S5 pruning)",
            "the best admissible selection (K1)",
            "end-to-end parity under wall-clock or replay-budget cuts (CS-2, admission CPU)",
            "any speedup or timing effect",
        ],
    }
)


class UniSorCycleSafeConfigError(sor.UniSorPortConfigError):
    """`prepare` received an invalid or missing `search.*` value (the port's check)."""


def validate_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """The `options_validator` (memo §7): no option exists, so exactly `{}` is accepted and
    every key -- reserved (refused by `validated_options` first) or not -- is refused."""
    if options:
        raise ValueError(
            f"{sorted(map(str, options))}: {NAME} has no algorithm_options (admission and its "
            "scan policy define the identity; candidates, grid, splits and hops are the shared "
            "search.* values)"
        )
    return {}


@dataclass(frozen=True)
class PreparedCycleSafe:
    """Immutable per-worker preparation: `uni_sor_port`'s own prepared object (A-1 cohort
    lists, parameters) and the settings hash of the validated (empty) options."""

    port: sor.PreparedUniSorPort
    settings_sha256: str


def prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> PreparedCycleSafe:
    options = validated_options(FACTORY, config.options)  # the public entry validates too
    try:  # the reused legacy prepare refuses options: it gets the same config without them
        port = sor.prepare(bundle, dataclasses.replace(config, options={}))
    except sor.UniSorPortConfigError as exc:
        raise UniSorCycleSafeConfigError(f"{NAME}: {exc}") from exc
    return PreparedCycleSafe(port, settings_sha256(options))


# ------------------------------------------------------------------ admission (memo §4.1-§4.2)


def route_edges(rq: sor.RouteQuote) -> tuple[tuple[str, str], ...]:
    """The directed token edges one route adds to the plan token graph: consecutive pairs of
    its lowercase SOR `token_path` (each plan step's `token_in -> token_out`, memo L3)."""
    path = rq.route.token_path
    return tuple(zip(path, path[1:], strict=False))


def union_has_cycle(routes: Sequence[sor.RouteQuote]) -> bool:
    """Does the union of the routes' token edges contain a directed cycle (a self-loop
    counts)? Iterative three-colour DFS over sorted nodes (deterministic)."""
    succ: dict[str, set[str]] = {}
    for rq in routes:
        for a, b in route_edges(rq):
            succ.setdefault(a, set()).add(b)
    colour: dict[str, int] = {}  # 1 = on the current DFS path, 2 = finished
    for root in sorted(succ):
        if root in colour:
            continue
        colour[root] = 1
        path = [root]
        stack = [iter(sorted(succ[root]))]
        while stack:
            nxt = next(stack[-1], None)
            if nxt is None:
                colour[path.pop()] = 2
                stack.pop()
            elif colour.get(nxt) == 1:
                return True
            elif nxt not in colour:
                colour[nxt] = 1
                path.append(nxt)
                stack.append(iter(sorted(succ.get(nxt, ()))))
    return False


@dataclass
class Admission:
    """One solve's chooser and selector state. `choose` is the §4.2 chooser handed to the
    port's `get_best_swap_route_by`; `select` is the selector handed to the port's pipeline
    and records whether it started and ran to its own termination (§4.7)."""

    admission_checks: int = 0
    combinations_rejected_cycle: int = 0
    started: bool = False
    completed: bool = False

    def choose(
        self, used_routes: Sequence[sor.RouteQuote], candidates: Sequence[sor.RouteQuote]
    ) -> sor.RouteQuote | None:
        used = {pid for r in used_routes for pid in r.pool_identifiers}
        for candidate in candidates:
            if any(pid in used for pid in candidate.pool_identifiers):
                continue  # B-S9 first: a pool conflict is not an admission check
            self.admission_checks += 1
            if not union_has_cycle((*used_routes, candidate)):
                return candidate
            self.combinations_rejected_cycle += 1  # a token cycle is one more conflict
        return None

    def select(
        self,
        percent_to_quotes: Mapping[int, Sequence[sor.RouteQuote]],
        percents: Sequence[int],
        *,
        min_splits: int,
        max_splits: int,
    ) -> sor.BestSwap | None:
        self.started = True
        swap = sor.get_best_swap_route_by(
            percent_to_quotes,
            percents,
            min_splits=min_splits,
            max_splits=max_splits,
            choose=self.choose,
        )
        self.completed = True
        return swap


# ------------------------------------------------------------------ records (memo §7-§8)


def sor_domain(bundle: SnapshotBundle, prepared: sor.PreparedUniSorPort) -> dict[str, Any]:
    """The `r021.domain/1` record (memo §7), identical for `uni_sor_port` and this identity:
    the same A-1 candidates, grid, hop and split bounds and the evaluator's feasible set."""
    order = [p.pool_id for p in (*prepared.v3_pools, *prepared.v2_pools)]
    return {
        "schema": "r021.domain/1",
        "universe": {"bundle": bundle.bundle_hash, "cohort": "sor_compatible", "pools": order},
        "protocols": ["constant_product", "concentrated"],
        "pool_order": order,
        "hops": {"max": prepared.max_hops, "param": "search.max_hops"},
        "splits": {
            "max": prepared.max_splits,
            "param": "search.max_splits",
            "governs": "allocation",
        },
        "amount_grid": {
            "kind": "repository_grid",
            "percent_step": prepared.percent_step,
            "remainder": "sor_d1_last_b_f1_route_all_remaining",
            "route_order": "sor_a1_b_r1_b_q1",
        },
        "zero_output_leg": "infeasible",
        "token_reuse": "simple_path",
        "pool_reuse": "disjoint",
        "dag_admission": "plan_token_dag",
        "full_fill": "v1_full_fill",
    }


def _canonical_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def _diagnostics(
    case: Case,
    context: SolveContext,
    prepared: PreparedCycleSafe,
    res: SolveResult,
    work: Mapping[str, int],
) -> dict[str, Any]:
    domain = sor_domain(context.bundle, prepared.port)
    domain_hash = _canonical_hash(domain)
    certificate: dict[str, Any] | None = None
    if res.status is SolveStatus.OK and res.score is not None:
        revision = context.run_identity.get("git_revision")
        certificate = {
            "schema": "r021.certificate/1",
            "candidate_domain_hash": domain_hash,
            "objective": context.objective.mode,
            "source": {
                "git_revision": revision if isinstance(revision, str) else None,
                "bundle_hash": context.bundle.bundle_hash,
                "algorithm": NAME,
                "effective_settings_sha256": prepared.settings_sha256,
            },
            "request": {
                "case_id": case.case_id,
                "token_in": case.token_in,
                "token_out": case.token_out,
                "amount_in": str(case.amount_in),
            },
            "lower_raw": str(res.score),
            "upper_raw": None,
            "gap_raw": None,
            "bound_kind": "unknown",
            "upper_source": None,
            "estimate": None,
            "optimality_proven": False,
            # the SOR search ended by its own rules; no domain resolution, no bound claimed
            "termination": "complete",
        }
    unsupported = res.status is SolveStatus.UNSUPPORTED  # D-4: routes only via non-SOR pools
    return {
        "schema": "r021.diagnostics/1",
        "contract": "R021-C/1",
        "algorithm": NAME,
        "domain": domain,
        "candidate_domain_hash": domain_hash,
        "certificate": certificate,
        "certificate_unavailable_reason": None if certificate is not None else "not_produced",
        "max_candidates_unit": MAX_CANDIDATES_UNIT,
        "work": dict(work),
        "fallback": {"used": False, "source": None, "reason": None},
        "scope": {
            "supported": not unsupported,
            "reason": "route_only_through_non_sor_pools" if unsupported else None,
        },
    }


def _not_started_reason(stats: Mapping[str, Any]) -> str:
    """Why the port's pipeline returned before its selector (memo §4.7): its own stop label."""
    return {"max_candidates": "max_candidates", "max_quotes": "max_quotes_table"}.get(
        str(stats.get("truncated_by")), "enumeration_status"
    )


# ------------------------------------------------------------------ solve


def solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    prepared = context.prepared
    if not isinstance(prepared, PreparedCycleSafe):
        raise TypeError(f"{NAME}.solve needs the PreparedCycleSafe returned by prepare()")
    admission = Admission()
    built: list[RoutePlan] = []  # the port publishes a built plan once, right before replay
    inner = dataclasses.replace(context, prepared=prepared.port, candidate_sink=built.append)
    with counted_evaluations() as evaluations:
        res = sor.solve(case, inner, budget, select=admission.select)
    assert admission.started == admission.completed, "the selector has no cooperative stop"
    assert len(built) <= 1
    stats: dict[str, Any] = dict(res.search_stats)
    ran = admission.completed
    rejected = admission.combinations_rejected_cycle

    published = res.status is SolveStatus.OK and res.plan is not None
    if published:
        assert res.plan is not None and built == [res.plan]
        context.report_candidate(res.plan)  # CS-2: once, after the ok replay

    error = res.error
    no_admissible = ran and not built and res.status is SolveStatus.NO_ROUTE and rejected > 0
    if no_admissible:  # CS-3
        error = (
            f"no admissible complete selection over {stats['route_quotes']} valid quote "
            f"entries: {rejected} combinations rejected by plan-token-DAG admission "
            "(B-S10 under admission)"
        )

    if not ran:
        selector: dict[str, Any] = {
            "phase": "not_started",
            "not_started_reason": _not_started_reason(stats),
            "selection": None,
        }
        replay: dict[str, Any] = {"phase": "not_reached", "interrupted_by": None, "status": None}
    else:
        selector = {
            "phase": "completed",
            "not_started_reason": None,
            "selection": stats.get("selection") is not None,
        }
        if not built:
            replay = {"phase": "not_needed", "interrupted_by": None, "status": None}
        elif res.evaluation is None:  # the port's replay stopped before one more quote
            replay = {
                "phase": "interrupted",
                "interrupted_by": stats.get("truncated_by"),
                "status": None,
            }
        else:
            replay = {"phase": "completed", "interrupted_by": None, "status": res.status.value}
    trajectory = "unavailable" if not ran else "identical" if rejected == 0 else "diverged"
    stats["cycle_safe"] = {
        "admission": ADMISSION,
        "admission_checks": admission.admission_checks,
        "combinations_rejected_cycle": rejected,
        "no_admissible_selection": no_admissible,
        "fallback": {"used": False},
        "selector": selector,
        "replay": replay,
        "reference_trajectory": trajectory,
        "comparable_completed": ran and replay["phase"] != "interrupted",
        "publication": {
            "rule": PUBLICATION_RULE,
            "published": published,
            "reference_publishes_before_replay": True,
            "withheld_by_cs2": bool(built) and not published,
        },
    }
    work = {
        "quotes_executed": int(stats["quotes_executed"]),
        "quotes_memoized": int(stats["quotes_memoized"]),
        "internal_evaluations": evaluations.count,
        "admission_checks": admission.admission_checks,
        "combinations_rejected_cycle": rejected,
    }
    out = dataclasses.replace(res, algorithm=NAME, error=error, search_stats=stats)
    stats["r021"] = _diagnostics(case, context, prepared, out, work)
    return out


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
    "Admission",
    "PreparedCycleSafe",
    "UniSorCycleSafeConfigError",
    "prepare",
    "route_edges",
    "solve",
    "sor_domain",
    "union_has_cycle",
    "validate_options",
]

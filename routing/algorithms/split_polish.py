"""`split_polish` (WHI-1623; research contract R023-C/1 §3, §4, §6, published by WHI-1622):
a gross-only, never-worse post-processor that re-optimises the split shares of a
declared base strategy's finished plan with exact integer replay (E1). Jupiter-inspired
(Ultra V3 / Metis v7 Brent splitting), **NOT Jupiter Metis**; not SCO, not an optimum, a local
heuristic. Experimental, `custom` group. A profile that names it runs it; since WHI-1632 (R024-C/1
§6) `--strategies all` also appends it with its selected preset (`PRESET`, below).

**Domain** (§3): the base plan's evaluated trace, canonicalised (zero references and zero-input
steps dropped). Every consumed fund must be fully consumed, otherwise the case is refused
(`scope: unsupported_topology`) and the base result is returned unchanged, with its true gross.
The variables are exact `Fraction` shares of every multiply-consumed fund; pools, directions,
step order and fund wiring stay fixed.

**Mechanism** (§4, verbatim): reconstruction check (shares `amount / produced`, simulated
allocations and gross must equal the base); evaluator-equivalent `simulate` (every consumer
but the last positive-share one gets `floor(total * share)`, that one the remainder, zero
shares exactly 0, zero-input steps skipped, physical pools sequential, a positive unconsumed
non-target fund infeasible); `rounds` rounds over split funds in first-consumption order and
consumer pairs `i < j`, stopping after a round with no acceptance; `line_search` over the
transfer `t in [-s_j, s_i]` with exact rational cache keys, `t = 0` and both endpoints always
evaluated, interior points on the `grid`, golden section (infeasible = -inf) or SciPy bounded
Brent (objective `-(v - v0) / v0`, infeasible = +1.0, `xatol = max(1, tol / 2)`), `maxiter`;
the best exact cached output wins (higher gross, then `t = 0`, smaller `|t|`, smaller `t`).
Only a strictly higher gross is accepted: the canonical plan is checked by the evaluator's
structural `_check_plan` (no quotes), stored in the `Incumbent` at once and published with
`report_candidate`. After each polish call the topology is rebuilt from the canonical plan.

**Budget** (§4.6): one `Ledger` shared with the base solve -- pre-charged with the quotes the
base executed under the worker meter, capped at `Budget.max_quotes`, with the cooperative wall
deadline `time_limit_seconds` after this solve started. Every simulation (the reconstruction
included) is charged; nothing is re-quoted to materialise the result. A cooperative stop keeps
the last validated incumbent: status `ok`, `search.split_polish.truncated_by` in
{`max_quotes`, `time`}. The runner's hard limits keep their `timeout`.

**Statuses** (§6): objective not `gross_only` -> `unsupported` (`scope: objective`, the base
never runs); a base status other than `ok` passes through unchanged (relabelled); a result
equal to the base returns the base's own plan and evaluation. A polished plan is returned with
`evaluation=None` (the runner evaluates it independently) and `score` = its simulated gross.

**Options** (`algorithm_options.split_polish`, all required, no defaults): `base` (one of
`BASES`: `incremental_graph`, `path_split` (WHI-1623), `metis_inspired`, `metis_history`,
`incremental_graph_repair` (WHI-1626)), `solver` (`brent` / `golden`), `rounds`, `tolerance`
(share of a fund, converted exactly to grid units), `grid` (D), `maxiter`. The E1 nominee is
`incremental_graph`, `brent`, 2, 0.0001, 10**9, 60. The selected preset v1 (`PRESET`,
`R024-P01-split_polish`, WHI-1631: selected by the registered rule R024-C/1 §5.7 among 90
candidates under P*, on the already exposed tuning split) is `metis_inspired`, `golden`, 2,
0.00001, 10**9, 60; `--strategies all` writes it out. It is recognised as `{kind: preset}` only in
a `--strategies all` document (`benchmark.profile.ALL_SCOPED_PRESETS`): equal options in an
explicit profile stay an override, so the 0.2.3/0.2.4 explicit profiles keep their identity.

**The base runs as its own registered identity** (WHI-1626). Its configuration has one source
each, so there is nothing to reconcile: (a) the profile's `search.*` and `graph.chunks` plus the
base's own `graph.*` keys (`graph_params_for`: `label_hops`, `label_pruning` for
`metis_inspired`, `label_hops` for `metis_history`), which the loader requires and hands to the
base's `prepare` exactly as to the base's own run; (b) `base_options`, the base's own
`algorithm_options`, validated by the base's own validator (reserved keys refused). It is
required when the base takes options (`metis_history`, `incremental_graph_repair`: no default,
write its pinned preset out to use it) and refused when it takes none. The base's own profile
entry, if the profile also lists the base, is never read. `CAPABILITIES` and `SEARCH_PARAMS` are
`incremental_graph`'s: exactly those of every base except `path_split`, whose narrower ones they
cover (WHI-1623). `marginal_activation` (E2) accepts only `E1_BASES` through `validate_options`.

`Ledger`, `Topology`, `canonical_from_evaluation`, `shares_or_refusal`, `simulate`,
`canonical_plan`, `Incumbent`, `rebuild`, `line_search`, `polish` and `polish_plan` are the
reusable seams of E2 (`marginal_activation`, WHI-1624). Importing this module never imports SciPy.
"""

from __future__ import annotations

import dataclasses
import math
import time
from collections.abc import Callable, Mapping
from fractions import Fraction
from types import MappingProxyType
from typing import Any

from pools.quote import _ACTIVE_METER, QuoteMeter, metered_quotes, quote_exact_in
from pools.result import QuoteStatus, SwapResult
from routing.algorithms import (
    incremental_graph,
    incremental_graph_repair,
    metis_history,
    metis_inspired,
    path_split,
)
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    OptionsError,
    SolveContext,
    SolveResult,
    SolveStatus,
    option_choice,
    option_int,
    option_number,
    require_option_keys,
    validated_options,
)
from routing.evaluator import EvalStatus, Evaluation, _check_plan, evaluate
from routing.plan import REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from snapshot.models import Case, PoolState, SnapshotBundle

NAME = "split_polish"
ISSUE = "WHI-1623"
CONTRACT = "R023-C/1"
CONTRACT_DOC = "R023-C/1 (WHI-1622 research contract), E1"

E1_BASES = (incremental_graph.NAME, path_split.NAME)  # WHI-1623's, the only ones E2 accepts
BASES: Mapping[str, AlgorithmFactory] = MappingProxyType(
    {
        f.name: f
        for f in (
            incremental_graph.FACTORY,
            path_split.FACTORY,
            metis_inspired.FACTORY,  # WHI-1626: M4
            metis_history.FACTORY,  # S4
            incremental_graph_repair.FACTORY,  # REP
        )
    }
)
SOLVERS = ("brent", "golden")
OPTION_KEYS = frozenset({"base", "solver", "rounds", "tolerance", "grid", "maxiter"})
BASE_OPTIONS = "base_options"  # the base's own algorithm_options (WHI-1626)
CAPABILITIES = incremental_graph.CAPABILITIES  # a ceiling: every base's plans are a subset
SEARCH_PARAMS = incremental_graph.SEARCH_PARAMS
GRAPH_PARAMS = incremental_graph.GRAPH_PARAMS

# The selected preset v1 (R024-C/1 §5.1, WHI-1631), frozen by its bytes (WHI-1632).
PRESET: dict[str, Any] = {
    "path": "config/split_polish/preset_v1.yaml",
    "sha256": "bdaba97b702c205fe6fae0b6ce2a439f550f8b0f263713c702789358f92dc533",
    "key": "R024-P01-split_polish",
    "version": 1,
}

GOLDEN = (math.sqrt(5) - 1) / 2
PENALTY = 1.0  # Brent: finite relative penalty of an infeasible point (losing 100 %), §4.4

PROVENANCE = {
    "experimental": True,
    "opt_in": True,
    "issue": ISSUE,
    "contract": CONTRACT_DOC,
    "label": "Jupiter-inspired (Ultra V3 / Metis v7 Brent splitting), NOT Jupiter Metis",
    "identity": (
        "post-processor of a declared base strategy: re-optimises the exact rational split "
        "shares of the base's canonical plan over its fixed funding topology"
    ),
    "claims": [
        "never worse than the base plan it ran on (strictly higher gross is the only change)",
        "every returned plan is the evaluator-validated incumbent of exact integer replay",
    ],
    "not_claimed": [
        "optimality (a local pairwise heuristic over a fixed topology; not SCO)",
        "Jupiter Metis equivalence",
        "any objective other than gross_only",
        "any speedup",
    ],
}


def validate_options(
    options: Mapping[str, Any], bases: tuple[str, ...] = E1_BASES
) -> dict[str, Any]:
    """The six polish keys, all required, each typed and in range; `base` one of `bases` (by
    default `E1_BASES`, which is what `marginal_activation` reuses this for)."""
    require_option_keys(options, set(OPTION_KEYS))
    return {
        "base": option_choice(options["base"], "base", bases),
        "solver": option_choice(options["solver"], "solver", SOLVERS),
        "rounds": option_int(options["rounds"], "rounds", 1, 16),
        "tolerance": option_number(options["tolerance"], "tolerance", 1e-12, 1.0),
        "grid": option_int(options["grid"], "grid", 2, 10**18),
        "maxiter": option_int(options["maxiter"], "maxiter", 1, 1000),
    }


def validate_split_polish_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """The `options_validator`: the six polish keys over every base in `BASES`, plus
    `base_options` exactly when the base takes options (its own validator's normalized form)."""
    require_option_keys(options, set(OPTION_KEYS), {BASE_OPTIONS})
    out = validate_options({k: options[k] for k in OPTION_KEYS}, tuple(BASES))
    base = BASES[out["base"]]
    if base.options_validator is None:
        if BASE_OPTIONS in options:
            raise OptionsError(f"{BASE_OPTIONS}: {base.name!r} accepts no algorithm_options")
        return out
    if BASE_OPTIONS not in options:
        raise OptionsError(
            f"{BASE_OPTIONS}: {base.name!r} requires its own algorithm_options (no default)"
        )
    try:
        out[BASE_OPTIONS] = validated_options(base, options[BASE_OPTIONS])
    except OptionsError as exc:
        raise OptionsError(f"{BASE_OPTIONS}: {exc}") from exc
    return out


def graph_params_for(options: Mapping[str, Any]) -> tuple[str, ...]:
    """The `graph.*` keys of `split_polish` with these (normalized) options: `GRAPH_PARAMS`
    (WHI-1623, whatever the base) plus the declared base's own."""
    own = BASES[options["base"]].graph_params
    return GRAPH_PARAMS + tuple(key for key in own if key not in GRAPH_PARAMS)


@dataclasses.dataclass(frozen=True)
class Settings:
    """The polish settings of one solve; `tol_k` is the tolerance in grid units (exact)."""

    solver: str
    rounds: int
    grid: int
    tol_k: int
    maxiter: int

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Settings:
        tolerance = Fraction(repr(float(options["tolerance"])))  # the written decimal, exactly
        grid = int(options["grid"])
        return cls(
            solver=str(options["solver"]),
            rounds=int(options["rounds"]),
            grid=grid,
            tol_k=math.floor(tolerance * grid),
            maxiter=int(options["maxiter"]),
        )


@dataclasses.dataclass(frozen=True)
class PreparedSplitPolish:
    base: str
    base_prepared: Any
    options: Mapping[str, Any]
    settings: Settings


def prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> PreparedSplitPolish:
    options = validated_options(FACTORY, config.options)  # the public entry validates too
    base = BASES[options["base"]]
    assert base.prepare is not None
    base_prepared = base.prepare(
        bundle, AlgorithmConfig(base.name, config.params, options.get(BASE_OPTIONS, {}))
    )
    return PreparedSplitPolish(
        options["base"], base_prepared, MappingProxyType(options), Settings.from_options(options)
    )


# ------------------------------------------------------------------ ledger


class PolishStop(Exception):
    """The shared ledger is exhausted; `reason` is `max_quotes` or `time`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclasses.dataclass
class Ledger:
    """One attempt ledger (§4.6): `used` quotes already charged (the base's), the cap and a
    monotonic deadline. Every quote passes the checks first, then the metered seam."""

    cap: int | None
    deadline: float | None = None
    used: int = 0

    def quote(self, state: PoolState, token_in: str, amount: int) -> SwapResult[PoolState]:
        if self.cap is not None and self.used >= self.cap:
            raise PolishStop("max_quotes")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise PolishStop("time")
        self.used += 1
        return quote_exact_in(state, token_in, amount)


QuoteFn = Callable[[PoolState, str, int], SwapResult[PoolState]]
Slot = tuple[int, int]  # (step index, input index)
Shares = dict[str, list[Fraction]]


# ------------------------------------------------------------------ topology


class Topology:
    """The fixed funding topology of a canonical plan: steps (amounts ignored), the token of
    every fund, every fund's consumer slots in step order, the multiply-consumed funds in
    order of first consumption."""

    def __init__(self, steps: tuple[SwapStep, ...], token_in: str, token_out: str) -> None:
        self.steps = steps
        self.token_out = token_out
        self.fund_token = {REQUEST_FUND_ID: token_in}
        self.fund_token.update({s.output_fund_id: s.token_out for s in steps})
        self.consumers: dict[str, list[Slot]] = {}
        for si, step in enumerate(steps):
            for ii, finput in enumerate(step.inputs):
                self.consumers.setdefault(finput.fund_id, []).append((si, ii))
        self.split_funds = [f for f, slots in self.consumers.items() if len(slots) > 1]


def topology_and_alloc(plan: RoutePlan, case: Case) -> tuple[Topology, dict[Slot, int]]:
    """A canonical plan (explicit integer amounts) as its topology and slot allocation."""
    alloc = {
        (si, ii): int(finput.amount)
        for si, step in enumerate(plan.steps)
        for ii, finput in enumerate(step.inputs)
    }
    return Topology(plan.steps, case.token_in, case.token_out), alloc


def canonical_from_evaluation(evaluation: Evaluation) -> tuple[RoutePlan, dict[str, int]]:
    """The canonical plan of an evaluated plan (zero references and zero-input steps dropped,
    `ALL_REMAINING` resolved) and the produced amount of every step's output fund."""
    steps = []
    for t in evaluation.trace:
        inputs = tuple(FundInput(f, a) for f, a in t.inputs if a > 0)
        if inputs:
            steps.append(SwapStep(t.pool_id, t.token_in, t.token_out, inputs, t.output_fund_id))
    return RoutePlan(tuple(steps)), {t.output_fund_id: t.amount_out for t in evaluation.trace}


def shares_or_refusal(
    topo: Topology, alloc: Mapping[Slot, int], produced: Mapping[str, int]
) -> tuple[Shares | None, str | None]:
    """Exact rational shares, or `(None, reason)` when a consumed fund is only partially
    consumed (outside the E1 domain, §3)."""
    shares: Shares = {}
    for fund, slots in topo.consumers.items():
        total = produced[fund]
        if sum(alloc[c] for c in slots) != total:
            return None, f"partially_consumed_fund:{fund}"
        shares[fund] = [Fraction(alloc[c], total) for c in slots]
    return shares, None


@dataclasses.dataclass(frozen=True)
class Simulation:
    """`gross` is `None` for an infeasible candidate; `states` are the post-plan pool states."""

    gross: int | None
    alloc: dict[Slot, int]
    produced: dict[str, int]
    states: dict[str, PoolState]


def simulate(
    bundle: SnapshotBundle, topo: Topology, shares: Shares, amount: int, quote: QuoteFn
) -> Simulation:
    """The evaluator-equivalent forward pass of §4.2 over `topo` with `shares`."""
    produced = {REQUEST_FUND_ID: amount}
    alloc: dict[Slot, int] = {}
    states: dict[str, PoolState] = {}
    for si, step in enumerate(topo.steps):
        total_in = 0
        for ii, finput in enumerate(step.inputs):
            fund = finput.fund_id
            if (si, ii) not in alloc:  # first consumer of this fund: allocate it whole
                total = produced.get(fund, 0)
                slots, weights = topo.consumers[fund], shares[fund]
                last = max(k for k, w in enumerate(weights) if w > 0)
                given = 0
                for k, slot in enumerate(slots):
                    if k != last:
                        value = math.floor(total * weights[k])
                        if not 0 <= value <= total - given:
                            raise ValueError(f"share invariant violated on fund {fund!r}")
                        alloc[slot] = value
                        given += value
                alloc[slots[last]] = total - given
            total_in += alloc[(si, ii)]
        if total_in == 0:
            produced[step.output_fund_id] = 0
            continue
        state = states.get(step.pool_id, bundle.pools[step.pool_id])
        result = quote(state, step.token_in, total_in)
        if (
            result.status is not QuoteStatus.OK
            or result.new_state is None
            or result.amount_in_consumed != total_in
        ):
            return Simulation(None, alloc, produced, states)
        states[step.pool_id] = result.new_state
        produced[step.output_fund_id] = result.amount_out
    gross = 0
    for fund, value in produced.items():
        if fund in topo.consumers or value == 0:
            continue
        if topo.fund_token[fund] != topo.token_out:
            return Simulation(None, alloc, produced, states)  # a dead non-target terminal
        gross += value
    return Simulation(gross, alloc, produced, states)


def canonical_plan(topo: Topology, alloc: Mapping[Slot, int]) -> RoutePlan:
    steps = []
    for si, step in enumerate(topo.steps):
        inputs = tuple(
            FundInput(f.fund_id, alloc[(si, ii)])
            for ii, f in enumerate(step.inputs)
            if alloc.get((si, ii), 0) > 0
        )
        if inputs:
            steps.append(dataclasses.replace(step, inputs=inputs))
    return RoutePlan(tuple(steps))


class Incumbent:
    """The last validated incumbent (§4.5): a complete canonical plan, its gross and the
    rebuilt variables. `offer` accepts only a strictly better, structurally valid plan and
    publishes it at once."""

    def __init__(
        self,
        plan: RoutePlan,
        gross: int,
        topo: Topology,
        shares: Shares,
        publish: Callable[[RoutePlan], None] | None = None,
    ) -> None:
        self.plan, self.gross, self.topo, self.shares = plan, gross, topo, shares
        self.publish = publish
        self.source = "base"

    def offer(
        self,
        bundle: SnapshotBundle,
        case: Case,
        topo: Topology,
        shares: Shares,
        sim: Simulation,
        source: str,
    ) -> bool:
        if sim.gross is None or sim.gross <= self.gross:
            return False
        plan = canonical_plan(topo, sim.alloc)
        if _check_plan(bundle, case, plan) is not None:  # structural only, no quotes
            return False
        self.plan, self.gross, self.topo, self.shares = plan, sim.gross, topo, shares
        self.source = source
        if self.publish is not None:
            self.publish(plan)
        return True


def rebuild(incumbent: Incumbent, case: Case) -> None:
    """Drop zero steps for good: the topology and exact shares of the canonical plan."""
    topo, alloc = topology_and_alloc(incumbent.plan, case)
    produced = {REQUEST_FUND_ID: case.amount_in}
    produced.update({f: sum(alloc[c] for c in slots) for f, slots in topo.consumers.items()})
    shares, why = shares_or_refusal(topo, alloc, produced)
    if shares is None:
        raise ValueError(f"a canonical incumbent cannot be rebuilt: {why}")
    incumbent.topo, incumbent.shares = topo, shares


# ------------------------------------------------------------------ 1-D search

Point = tuple[int | None, Any]  # (gross or None, payload)


def line_search(
    eval_t: Callable[[Fraction], Point],
    si: Fraction,
    sj: Fraction,
    settings: Settings,
    stats: dict[str, Any],
) -> tuple[int, Fraction, Any] | None:
    """Maximise the gross over the transfer `t in [-sj, si]` (§4.4). Returns the best exact
    cached feasible point `(gross, t, payload)`, or `None` when none is feasible."""
    grid = settings.grid
    lo, hi = -math.floor(sj * grid), math.floor(si * grid)
    cache: dict[Fraction, Point] = {}

    def value_at(t: Fraction) -> Point:
        if t not in cache:
            cache[t] = eval_t(t)
        return cache[t]

    def t_of(k: float) -> Fraction:
        index = max(lo, min(hi, int(round(k))))
        if index == 0:
            return Fraction(0)
        if index == hi:
            return si
        if index == lo:
            return -sj
        return Fraction(index, grid)

    value_at(Fraction(0))
    value_at(si)
    value_at(-sj)
    if hi - lo > settings.tol_k:
        if settings.solver == "golden":
            _golden(value_at, t_of, lo, hi, settings, stats)
        else:
            _brent(value_at, t_of, lo, hi, settings, stats)
    feasible = [(v, t, payload) for t, (v, payload) in cache.items() if v is not None]
    if not feasible:
        return None
    return max(feasible, key=lambda p: (p[0], p[1] == 0, -abs(p[1]), -p[1]))


def _golden(
    value_at: Callable[[Fraction], Point],
    t_of: Callable[[float], Fraction],
    lo: int,
    hi: int,
    settings: Settings,
    stats: dict[str, Any],
) -> None:
    def val(x: float) -> int | float:  # the exact gross; -inf when infeasible
        v = value_at(t_of(x))[0]
        return -math.inf if v is None else v

    a: float = lo
    b: float = hi
    x1, x2 = b - GOLDEN * (b - a), a + GOLDEN * (b - a)
    f1, f2 = val(x1), val(x2)
    it = 0
    while b - a > settings.tol_k and it < settings.maxiter:
        it += 1
        if f1 >= f2:
            b, x2, f2 = x2, x1, f1
            x1 = b - GOLDEN * (b - a)
            f1 = val(x1)
        else:
            a, x1, f1 = x1, x2, f2
            x2 = a + GOLDEN * (b - a)
            f2 = val(x2)
    stats["golden_iters"] = stats.get("golden_iters", 0) + it


def _brent(
    value_at: Callable[[Fraction], Point],
    t_of: Callable[[float], Fraction],
    lo: int,
    hi: int,
    settings: Settings,
    stats: dict[str, Any],
) -> None:
    from scipy.optimize import minimize_scalar  # type: ignore[import-untyped]

    ref = value_at(Fraction(0))[0] or 1

    def objective(k: float) -> float:
        v = value_at(t_of(k))[0]
        return PENALTY if v is None else -float(Fraction(v - ref, ref))

    res = minimize_scalar(
        objective,
        bounds=(lo, hi),
        method="bounded",
        options={"xatol": max(1.0, settings.tol_k / 2), "maxiter": settings.maxiter},
    )
    status = stats.setdefault("brent_status", {})
    status[str(int(res.status))] = status.get(str(int(res.status)), 0) + 1
    stats["brent_nfev"] = stats.get("brent_nfev", 0) + int(res.nfev)


def polish(
    bundle: SnapshotBundle,
    case: Case,
    incumbent: Incumbent,
    quote: QuoteFn,
    settings: Settings,
    stats: dict[str, Any],
) -> None:
    """One polish call (§4.3-§4.5) over `incumbent.topo`. Every accepted exchange is held by
    `incumbent` immediately, so a `PolishStop` raised by `quote` keeps it."""
    topo, shares = incumbent.topo, dict(incumbent.shares)
    stats["polish_calls"] = stats.get("polish_calls", 0) + 1
    for _ in range(settings.rounds):
        improved = False
        for fund in topo.split_funds:
            n = len(topo.consumers[fund])
            for i in range(n):
                for j in range(i + 1, n):
                    si, sj = shares[fund][i], shares[fund][j]

                    def eval_t(
                        t: Fraction, fund: str = fund, i: int = i, j: int = j,
                        si: Fraction = si, sj: Fraction = sj, current: Shares = shares,
                    ) -> Point:  # fmt: skip
                        weights = list(current[fund])
                        weights[i], weights[j] = si - t, sj + t
                        candidate = {**current, fund: weights}
                        stats["simulations"] = stats.get("simulations", 0) + 1
                        sim = simulate(bundle, topo, candidate, case.amount_in, quote)
                        return sim.gross, (candidate, sim)

                    best = line_search(eval_t, si, sj, settings, stats)
                    if best is not None and best[0] > incumbent.gross:
                        candidate, sim = best[2]
                        if incumbent.offer(bundle, case, topo, candidate, sim, "polish"):
                            shares = candidate
                            improved = True
                            stats["accepted"] = stats.get("accepted", 0) + 1
        if not improved:
            break
    rebuild(incumbent, case)


# ------------------------------------------------------------------ E1 driver


@dataclasses.dataclass
class Outcome:
    """What `polish_plan` did: `scope` is `fixed_funding_topology` or `unsupported_topology`
    (`reason` says why), `incumbent` the final holder (`None` when refused), `truncated_by`
    the caught cooperative stop, `work` the search counters."""

    scope: str
    reason: str | None = None
    incumbent: Incumbent | None = None
    truncated_by: str | None = None
    work: dict[str, Any] = dataclasses.field(default_factory=dict)


def polish_plan(
    bundle: SnapshotBundle,
    case: Case,
    evaluation: Evaluation,
    ledger: Ledger,
    settings: Settings,
    publish: Callable[[RoutePlan], None] | None = None,
) -> Outcome:
    """E1 on an evaluator-valid plan (its `ok` `evaluation`): canonicalise, refuse outside the
    domain, check the reconstruction, polish. Quotes go through `ledger`; a `PolishStop` is
    caught and recorded, the incumbent kept."""
    plan, produced = canonical_from_evaluation(evaluation)
    produced[REQUEST_FUND_ID] = case.amount_in
    topo, alloc = topology_and_alloc(plan, case)
    shares, why = shares_or_refusal(topo, alloc, produced)
    if shares is None:
        return Outcome("unsupported_topology", reason=why)
    incumbent = Incumbent(plan, evaluation.gross_output, topo, shares, publish)
    outcome = Outcome("fixed_funding_topology", incumbent=incumbent)
    try:
        sim = simulate(bundle, topo, shares, case.amount_in, ledger.quote)
        if sim.gross != evaluation.gross_output or any(sim.alloc[c] != a for c, a in alloc.items()):
            raise ValueError("reconstruction mismatch: the simulated base differs from its replay")
        polish(bundle, case, incumbent, ledger.quote, settings, outcome.work)
    except PolishStop as stop:
        outcome.truncated_by = stop.reason
    return outcome


# ------------------------------------------------------------------ solve


def _active_meter() -> QuoteMeter | None:
    return _ACTIVE_METER.get()


def solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    meter = _active_meter()
    if meter is None:  # a direct (unmetered) call: meter it here so the base's quotes count
        with metered_quotes(None) as own:
            return _solve(case, context, budget, own)
    return _solve(case, context, budget, meter)


def _solve(case: Case, context: SolveContext, budget: Budget, meter: QuoteMeter) -> SolveResult:
    prepared: PreparedSplitPolish = context.prepared
    record: dict[str, Any] = {
        "contract": CONTRACT,
        **dict(prepared.options),
        "scope": None,
        "truncated_by": None,
    }
    if context.objective.mode != "gross_only":
        record["scope"] = "objective"
        return SolveResult(
            case.case_id, NAME, SolveStatus.UNSUPPORTED,
            error=f"objective {context.objective.mode} is outside {NAME}'s gross_only ceiling",
            search_stats={NAME: record},
        )  # fmt: skip
    started = time.monotonic()
    counted_before = meter.counted
    base = BASES[prepared.base].solve(
        case, dataclasses.replace(context, prepared=prepared.base_prepared), budget
    )
    base_quotes = meter.counted - counted_before
    stats: dict[str, Any] = {
        "base": {
            "algorithm": prepared.base,
            "status": base.status.value,
            "quotes": base_quotes,
            "search": dict(base.search_stats),
        },
        NAME: record,
    }
    unchanged = dataclasses.replace(base, algorithm=NAME, search_stats=stats)
    if base.status is not SolveStatus.OK or base.plan is None:
        return unchanged  # base statuses pass through (§6)
    deadline = None if budget.time_limit_seconds is None else started + budget.time_limit_seconds
    ledger = Ledger(budget.max_quotes, deadline, used=base_quotes)
    evaluation = base.evaluation
    try:
        if evaluation is None or evaluation.status is not EvalStatus.OK:  # replay it, charged
            evaluation = evaluate(context.bundle, case, base.plan, context.objective,
                                  quote=ledger.quote)  # fmt: skip
    except PolishStop as stop:
        record.update(scope="not_reached", truncated_by=stop.reason)
        return unchanged
    if evaluation.status is not EvalStatus.OK:
        raise ValueError(f"the base's ok plan does not evaluate: {evaluation.error}")
    outcome = polish_plan(
        context.bundle, case, evaluation, ledger, prepared.settings, context.report_candidate
    )
    incumbent = outcome.incumbent
    record.update(
        scope=outcome.scope,
        reason=outcome.reason,
        truncated_by=outcome.truncated_by,
        base_gross=str(evaluation.gross_output),
        gross=str(evaluation.gross_output if incumbent is None else incumbent.gross),
        quotes=ledger.used - base_quotes,
        work=outcome.work,
    )
    if incumbent is None or incumbent.source == "base":
        return unchanged  # refused, or nothing strictly better: the base result itself
    return SolveResult(
        case_id=case.case_id,
        algorithm=NAME,
        status=SolveStatus.OK,
        plan=incumbent.plan,
        evaluation=None,
        score=incumbent.gross,
        candidates_considered=base.candidates_considered + outcome.work.get("simulations", 0),
        candidates_truncated=base.candidates_truncated,
        search_stats=stats,
    )


FACTORY = AlgorithmFactory(
    name=NAME,
    solve=solve,
    prepare=prepare,
    capabilities=CAPABILITIES,
    search_params=SEARCH_PARAMS,
    graph_params=GRAPH_PARAMS,
    provenance=PROVENANCE,
    options_validator=validate_split_polish_options,
    options_preset=MappingProxyType(PRESET),
    graph_params_for=graph_params_for,
)

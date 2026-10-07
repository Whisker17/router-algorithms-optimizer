"""`marginal_activation` (WHI-1624; research contract R023-C/1 §3, §5, §6, published by WHI-1622):
E2 of the contract. After the E1 polisher (`split_polish`) has finished on a declared base
strategy's plan, it appends up to K new branches found by a delta-amount label search on the
post-plan pool states, re-splits them and keeps one only if an independent replay confirms a
strict gain with positive flow on the new branch. Jupiter-inspired, **NOT Jupiter Metis**;
PRIME-Flow-like (overlapping paths, a split between the current flow and a new path). Not an
optimum and not a certificate: a local heuristic. Experimental, `custom` group, selected only by
a profile that names it (`--strategies all` does not add it).

**Domain** (§3): E1's fixed funding topology plus appended branches. A reused pool executes as a
new sequential call (`shared_sequential`). A plan E1 refuses (`unsupported_topology`) is returned
as the base result; nothing is activated.

**Iteration** (§5.1, verbatim, at most `activations` = K times):

1. Post-plan pool states: `simulate` of the incumbent (charged), i.e. the evaluator's sequential
   state of every pool the plan touches.
2. `top_paths`: hop-layered, token-simple label search from `token_in` with the fresh amount
   `delta = max(1, floor(A * delta_share))`: one label per (layer, token) (strictly larger output
   replaces, ties keep the first in sorted order), `H = search.max_hops` layers, every path that
   reaches `token_out` is a terminal; the `top_k` best terminals (larger output, then the path).
3. For each terminal in rank order: `union` of the incumbent's canonical steps and the new path
   under a fresh fund prefix that no existing fund id starts with; **admitted only if the token
   graph over all union steps is acyclic** (`dag_cycle` otherwise). Seed: `pf` = proportional
   (`w_orig * (1 - s0) + [s0]`); `full` = donor (`s0` taken from the largest REQUEST consumer,
   lowest index on ties). Optimise: `pf` = one grouped 1-D search (`pf_split`) over the new
   branch's share `tt in [0, 1]` of `w_orig * (1 - tt) + [tt]` (`tt = 0` is the original plan
   exactly; intermediate funds fixed); `full` = one E1 `polish` call over every fund of the union.
4. Accept iff the new branch's first hop has positive integer input, the gross strictly improves,
   and an evaluator replay **charged to the attempt ledger** equals the simulated gross; the
   topology is then rebuilt. Otherwise the next terminal is tried. Stop when K is reached, no
   terminal is accepted (`stop_no_candidate`) or the budget ends (its reason is preserved).

**Controls** (§5.3; `arm` option, for the campaign): `work_matched` (primary) runs E1 polish calls
from the E1 incumbent until the treatment's activation-stage quote spend -- a hard `work_target`
stop inside the control's own ledger -- or until a call improves nothing; `call_matched` runs
exactly the treatment's number of activation-stage optimiser invocations, each a full E1 polish
call (invocations started and calls completed are reported separately). A control arm runs base
and E1 on its own ledger (they are the shared stages), then the treatment's activation stage as an
**uncharged reference** under a nested quote meter (its numbers are recorded under `activation`,
never in this record's quote count), then the control on a ledger pre-charged with the shared
spend, under the same global cap and its own wall allowance (the treatment's allowance after base
+ E1). If the budget ends during the base replay or E1, every arm is labelled `not_reached =
"activation/control not reached: E1 truncated"` and returns the last E1 incumbent.

**Budget** (§4.6, §5.1): one `split_polish.Ledger` shared with the base solve, as E1. A
cooperative stop is `ok` with `search.marginal_activation.truncated_by` in {`max_quotes`, `time`}
(and `work_target` for the work-matched control), never `no_route`. **Statuses** (§6): as
`split_polish`.

**Options** (`algorithm_options.marginal_activation`, all required, no defaults): E1's six
(`base`, `solver`, `rounds`, `tolerance`, `grid`, `maxiter`), `mode` (`pf` / `full`),
`activations` (K), `top_k`, `delta_share`, `seed_share` (both exact decimals of the written value)
and `arm` (`treatment` / `work_matched` / `call_matched`). The E2 nominee is the E1 nominee with
`pf`, 2, 3, 0.0001, 0.0001, `treatment`. Importing this module never imports SciPy.
"""

from __future__ import annotations

import dataclasses
import math
import time
from collections.abc import Callable, Mapping
from fractions import Fraction
from types import MappingProxyType
from typing import Any

from pools.quote import QuoteMeter, metered_quotes
from pools.result import QuoteStatus, SwapResult
from routing.algorithms import split_polish as sp
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
    option_choice,
    option_int,
    option_number,
    require_option_keys,
    validated_options,
)
from routing.evaluator import EvalStatus, _check_plan, _token_cycle, evaluate
from routing.plan import REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from snapshot.models import Case, PoolState, SnapshotBundle

NAME = "marginal_activation"
ISSUE = "WHI-1624"
CONTRACT = sp.CONTRACT
CONTRACT_DOC = "R023-C/1 (WHI-1622 research contract), E2"

MODES = ("pf", "full")
ARMS = ("treatment", "work_matched", "call_matched")
OPTION_KEYS = sp.OPTION_KEYS | {"mode", "activations", "top_k", "delta_share", "seed_share", "arm"}
NOT_REACHED = "activation/control not reached: E1 truncated"
CAPABILITIES = sp.CAPABILITIES
SEARCH_PARAMS = sp.SEARCH_PARAMS
GRAPH_PARAMS = sp.GRAPH_PARAMS

PROVENANCE = {
    "experimental": True,
    "opt_in": True,
    "issue": ISSUE,
    "contract": CONTRACT_DOC,
    "mechanism": "marginal activation",
    "label": "Jupiter-inspired (Ultra V3 / Metis v7 Brent splitting), NOT Jupiter Metis",
    "identity": (
        "post-processor of a declared base strategy: split_polish (E1), then up to K new branches "
        "from a delta-amount label search on post-plan states, admitted under the all-steps "
        "token-DAG invariant and kept only on a strictly higher, replay-confirmed gross"
    ),
    "claims": [
        "never worse than the base plan it ran on (strictly higher gross is the only change)",
        "every returned plan is the evaluator-validated incumbent of exact integer replay",
    ],
    "not_claimed": [
        "optimality (a heuristic label oracle and local line searches; not SCO, not FVO)",
        "Jupiter Metis equivalence",
        "any objective other than gross_only",
        "any speedup",
    ],
}

Edge = tuple[str, str, str]  # (pool id, token in, token out)
Path = tuple[Edge, ...]


def validate_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """The `options_validator`: the twelve keys, all required, each typed and in range."""
    require_option_keys(options, set(OPTION_KEYS))
    return {
        **sp.validate_options({k: options[k] for k in sp.OPTION_KEYS}),
        "mode": option_choice(options["mode"], "mode", MODES),
        "activations": option_int(options["activations"], "activations", 1, 16),
        "top_k": option_int(options["top_k"], "top_k", 1, 16),
        "delta_share": option_number(options["delta_share"], "delta_share", 1e-12, 1.0),
        "seed_share": option_number(options["seed_share"], "seed_share", 1e-12, 0.5),
        "arm": option_choice(options["arm"], "arm", ARMS),
    }


def _exact(value: Any) -> Fraction:
    return Fraction(repr(float(value)))  # the written decimal, exactly (as E1's tolerance)


@dataclasses.dataclass(frozen=True)
class Settings:
    """The E2 settings of one solve: E1's `polish` settings plus the activation settings."""

    polish: sp.Settings
    mode: str
    activations: int
    top_k: int
    delta_share: Fraction
    seed: Fraction
    max_hops: int

    @classmethod
    def from_options(cls, options: Mapping[str, Any], max_hops: int) -> Settings:
        return cls(
            polish=sp.Settings.from_options(options),
            mode=str(options["mode"]),
            activations=int(options["activations"]),
            top_k=int(options["top_k"]),
            delta_share=_exact(options["delta_share"]),
            seed=_exact(options["seed_share"]),
            max_hops=int(max_hops),
        )

    def delta(self, amount: int) -> int:
        return max(1, math.floor(amount * self.delta_share))


@dataclasses.dataclass(frozen=True)
class PreparedMarginalActivation:
    base: str
    base_prepared: Any
    options: Mapping[str, Any]
    settings: Settings
    adjacency: Mapping[str, tuple[Edge, ...]]


def adjacency(bundle: SnapshotBundle) -> dict[str, tuple[Edge, ...]]:
    """Both directions of every bundle pool, per input token, sorted."""
    edges: dict[str, list[Edge]] = {}
    for pid, state in bundle.pools.items():
        edges.setdefault(state.token0, []).append((pid, state.token0, state.token1))
        edges.setdefault(state.token1, []).append((pid, state.token1, state.token0))
    return {token: tuple(sorted(out)) for token, out in edges.items()}


def prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> PreparedMarginalActivation:
    options = validated_options(FACTORY, config.options)  # the public entry validates too
    base = sp.BASES[options["base"]]
    assert base.prepare is not None
    base_prepared = base.prepare(bundle, AlgorithmConfig(base.name, params=config.params))
    return PreparedMarginalActivation(
        options["base"],
        base_prepared,
        MappingProxyType(options),
        Settings.from_options(options, config.params["max_hops"]),
        MappingProxyType(adjacency(bundle)),
    )


# ------------------------------------------------------------------ label search, union


def top_paths(
    bundle: SnapshotBundle,
    edges: Mapping[str, tuple[Edge, ...]],
    states: Mapping[str, PoolState],
    case: Case,
    delta: int,
    quote: sp.QuoteFn,
    hops: int,
    k: int,
) -> list[tuple[int, Path]]:
    """The `k` best terminal paths of the hop-layered, token-simple label search (§5.1) with
    fresh `delta` quotes on the post-plan `states`."""
    src, dst = case.token_in, case.token_out
    layer: dict[str, tuple[int, Path]] = {src: (delta, ())}
    finals: list[tuple[int, Path]] = []
    for depth in range(1, hops + 1):
        nxt: dict[str, tuple[int, Path]] = {}
        for token, (amount, path) in sorted(layer.items()):
            visited = {src, *(edge[1] for edge in path)}
            for pid, a, b in edges.get(token, ()):
                if b in visited or (depth == hops and b != dst):
                    continue
                r = quote(states.get(pid, bundle.pools[pid]), a, amount)
                if r.status is not QuoteStatus.OK or r.amount_in_consumed != amount:
                    continue
                if r.amount_out <= 0:
                    continue
                extended = (*path, (pid, a, b))
                if b == dst:
                    finals.append((r.amount_out, extended))
                elif b not in nxt or r.amount_out > nxt[b][0]:
                    nxt[b] = (r.amount_out, extended)
        layer = nxt
    finals.sort(key=lambda f: (-f[0], f[1]))
    return finals[:k]


def fresh_prefix(topo: sp.Topology, iteration: int) -> str:
    """A fund-id prefix no existing fund id starts with (collision-checked, F12)."""
    n = 0
    while any(f.startswith(f"__ACT{iteration}_{n}") for f in topo.fund_token):
        n += 1
    return f"__ACT{iteration}_{n}"


def union(
    incumbent: sp.Incumbent, path: Path, prefix: str, seed: Fraction, mode: str
) -> tuple[sp.Topology, sp.Shares] | str:
    """The incumbent's canonical steps plus the branch `path` (appended last, funds under
    `prefix`) with the seeded shares, or the refusal reason (`dag_cycle`, `seed_impossible`)."""
    topo = incumbent.topo
    branch = tuple(
        SwapStep(
            pid, a, b,
            (FundInput(REQUEST_FUND_ID if h == 0 else f"{prefix}H{h - 1}", 0),),
            f"{prefix}OUT" if h == len(path) - 1 else f"{prefix}H{h}",
        )
        for h, (pid, a, b) in enumerate(path)
    )  # fmt: skip
    steps = topo.steps + branch
    edges: dict[str, set[str]] = {}
    for step in steps:
        edges.setdefault(step.token_in, set()).add(step.token_out)
    if _token_cycle(edges) is not None:  # all union steps: no reallocation can make a cycle
        return "dag_cycle"
    joined = sp.Topology(steps, topo.fund_token[REQUEST_FUND_ID], topo.token_out)
    shares = {
        f: list(incumbent.shares[f]) if f in incumbent.shares else [Fraction(1)]
        for f in joined.consumers
    }
    request = shares[REQUEST_FUND_ID]
    if mode == "pf":  # proportional: the original REQUEST vector keeps its proportions
        shares[REQUEST_FUND_ID] = [x * (1 - seed) for x in request] + [seed]
    else:  # full: the declared donor (largest REQUEST consumer, lowest index on ties)
        donor = max(range(len(request)), key=lambda j: (request[j], -j))
        if request[donor] <= seed:
            return "seed_impossible"
        request[donor] -= seed
        request.append(seed)
    return joined, shares


def pf_split(
    bundle: SnapshotBundle,
    case: Case,
    candidate: sp.Incumbent,
    original: list[Fraction],
    quote: sp.QuoteFn,
    settings: sp.Settings,
    stats: dict[str, Any],
) -> None:
    """PF: one grouped 1-D search over the new branch's share `tt` of `original * (1 - tt) +
    [tt]` on the union topology (`tt = 0` is the original plan exactly). The line search's transfer
    `t in [-s0, 1 - s0]` is `tt - s0`, so its `t = 0` is the seed and `tt = 0`, `tt = 1` are its
    exact endpoints."""
    topo, seeded = candidate.topo, candidate.shares
    s0 = seeded[REQUEST_FUND_ID][-1]

    def eval_t(t: Fraction) -> sp.Point:
        tt = s0 + t
        shares = {**seeded, REQUEST_FUND_ID: [x * (1 - tt) for x in original] + [tt]}
        stats["simulations"] = stats.get("simulations", 0) + 1
        sim = sp.simulate(bundle, topo, shares, case.amount_in, quote)
        return sim.gross, (shares, sim)

    best = sp.line_search(eval_t, 1 - s0, s0, settings, stats)
    if best is not None:
        shares, sim = best[2]
        candidate.offer(bundle, case, topo, shares, sim, "pf")
    sp.rebuild(candidate, case)


# ------------------------------------------------------------------ the activation stage


def accepts(
    bundle: SnapshotBundle,
    case: Case,
    incumbent: sp.Incumbent,
    candidate: sp.Incumbent,
    prefix: str,
    quote: sp.QuoteFn,
    objective: Any,
) -> tuple[str, int]:
    """The §5.1 acceptance rule: `(verdict, new-branch first-hop flow)`. The replay is charged to
    `quote`; a `PolishStop` it raises propagates."""
    flow = sum(
        int(i.amount)
        for s in candidate.plan.steps
        if s.output_fund_id.startswith(prefix)
        for i in s.inputs
        if i.fund_id == REQUEST_FUND_ID
    )
    if not (candidate.gross > incumbent.gross and flow > 0):
        return "no_gain_or_zero_flow", flow
    replay = evaluate(bundle, case, candidate.plan, objective, quote=quote)
    if replay.status is EvalStatus.OK and replay.gross_output == candidate.gross:
        return "accept", flow
    return "replay_mismatch", flow


def activate(
    bundle: SnapshotBundle,
    case: Case,
    incumbent: sp.Incumbent,
    quote: sp.QuoteFn,
    settings: Settings,
    edges: Mapping[str, tuple[Edge, ...]],
    objective: Any,
    log: list[list[Any]],
    work: dict[str, Any],
) -> None:
    """The §5.1 iteration on `incumbent` (an E1 result). Every outcome is appended to `log` as
    `[iteration, rank, verdict]` (rank -1: `stop_no_candidate`). A `PolishStop` propagates with
    its reason; an accepted activation is already held and published."""
    for it in range(settings.activations):
        post = sp.simulate(bundle, incumbent.topo, incumbent.shares, case.amount_in, quote)
        found = top_paths(
            bundle, edges, post.states, case, settings.delta(case.amount_in), quote,
            settings.max_hops, settings.top_k,
        )  # fmt: skip
        plan_pools = {s.pool_id for s in incumbent.plan.steps}
        accepted = False
        for rank, (_, path) in enumerate(found):
            prefix = fresh_prefix(incumbent.topo, it)
            built = union(incumbent, path, prefix, settings.seed, settings.mode)
            if isinstance(built, str):
                log.append([it, rank, built])
                continue
            topo, shares = built
            seeded = sp.simulate(bundle, topo, shares, case.amount_in, quote)
            if seeded.gross is None:
                log.append([it, rank, "seed_infeasible"])
                continue
            candidate = sp.Incumbent(
                sp.canonical_plan(topo, seeded.alloc), seeded.gross, topo, shares
            )
            if _check_plan(bundle, case, candidate.plan) is not None:
                log.append([it, rank, "seed_structure"])
                continue
            work["invocations"] = work.get("invocations", 0) + 1  # optimiser invocations started
            stop: sp.PolishStop | None = None
            try:
                if settings.mode == "full":
                    sp.polish(bundle, case, candidate, quote, settings.polish, work)
                else:
                    pf_split(bundle, case, candidate, incumbent.shares[REQUEST_FUND_ID], quote,
                             settings.polish, work)  # fmt: skip
                work["completed"] = work.get("completed", 0) + 1
            except sp.PolishStop as caught:
                stop = caught
            try:
                verdict, flow = accepts(
                    bundle, case, incumbent, candidate, prefix, quote, objective
                )
            except sp.PolishStop as caught:  # the replay itself ran out: its reason wins
                verdict, stop = "budget_before_validation", caught
            if verdict == "accept":
                reuse = any(p[0] in plan_pools for p in path)
                novel = sum(p[0] not in plan_pools for p in path)
                incumbent.plan, incumbent.gross = candidate.plan, candidate.gross
                incumbent.source = "activation"
                sp.rebuild(incumbent, case)
                if incumbent.publish is not None:
                    incumbent.publish(incumbent.plan)
                work["activations"] = work.get("activations", 0) + 1
                reused = "shared_sequential" if reuse else "none"
                log.append([it, rank, f"accepted novel_pools={novel} reuse={reused} flow={flow}"])
                accepted = True
            else:
                log.append([it, rank, verdict])
            if stop is not None:
                raise stop
            if accepted:
                break
        if not accepted:
            log.append([it, -1, "stop_no_candidate"])
            break


def run_activation(
    bundle: SnapshotBundle,
    case: Case,
    incumbent: sp.Incumbent,
    ledger: sp.Ledger,
    settings: Settings,
    edges: Mapping[str, tuple[Edge, ...]],
    objective: Any,
) -> dict[str, Any]:
    """`activate` on `ledger`; returns the stage record (the caught stop included)."""
    used = ledger.used
    record: dict[str, Any] = {"log": [], "work": {}, "truncated_by": None}
    try:
        activate(bundle, case, incumbent, ledger.quote, settings, edges, objective,
                 record["log"], record["work"])  # fmt: skip
    except sp.PolishStop as stop:
        record["truncated_by"] = stop.reason
    record.update(
        quotes=ledger.used - used,
        invocations=record["work"].get("invocations", 0),
        gross=str(incumbent.gross),
    )
    return record


# ------------------------------------------------------------------ controls (§5.3)


@dataclasses.dataclass
class ControlLedger(sp.Ledger):
    """A control's own ledger: `split_polish.Ledger` plus the hard `target` (the work-matched
    control's `work_target`), checked before the cap."""

    target: int | None = None

    def quote(self, state: PoolState, token_in: str, amount: int) -> SwapResult[PoolState]:
        if self.target is not None and self.used >= self.target:
            raise sp.PolishStop("work_target")
        return super().quote(state, token_in, amount)


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """The E1 incumbent at the end of E1 (the shared stages), the ledger's `used` count then and
    the time E1 ended."""

    plan: RoutePlan
    gross: int
    topo: sp.Topology
    shares: sp.Shares
    source: str
    used: int
    at: float

    @classmethod
    def of(cls, incumbent: sp.Incumbent, ledger: sp.Ledger) -> Snapshot:
        return cls(
            incumbent.plan, incumbent.gross, incumbent.topo, dict(incumbent.shares),
            incumbent.source, ledger.used, time.monotonic(),
        )  # fmt: skip


def run_control(
    bundle: SnapshotBundle,
    case: Case,
    snapshot: Snapshot,
    kind: str,
    treatment: Mapping[str, Any],
    cap: int | None,
    allowance: float | None,
    settings: sp.Settings,
    publish: Callable[[RoutePlan], None] | None = None,
) -> tuple[sp.Incumbent, dict[str, Any]]:
    """One control from `snapshot` on its own `ControlLedger` (pre-charged with the shared spend,
    the global `cap`, a deadline `allowance` seconds from now). `work_matched`: polish calls until
    the treatment's activation quotes (`work_target`) or a call improves nothing; `call_matched`:
    exactly the treatment's invocations."""
    incumbent = sp.Incumbent(snapshot.plan, snapshot.gross, snapshot.topo,
                             dict(snapshot.shares), publish)  # fmt: skip
    incumbent.source = snapshot.source
    work_matched = kind == "work_matched"
    target = snapshot.used + int(treatment["quotes"]) if work_matched else None
    deadline = None if allowance is None else time.monotonic() + allowance
    ledger = ControlLedger(cap, deadline, used=snapshot.used, target=target)
    record: dict[str, Any] = {
        "kind": kind,
        "target_quotes": treatment["quotes"] if work_matched else None,
        "target_calls": None if work_matched else treatment["invocations"],
        "calls_started": 0,
        "calls_completed": 0,
        "stop": None,
        "converged": False,
        "work": {},
    }
    try:
        while work_matched or record["calls_started"] < treatment["invocations"]:
            before = incumbent.gross
            record["calls_started"] += 1
            sp.polish(bundle, case, incumbent, ledger.quote, settings, record["work"])
            record["calls_completed"] += 1
            if work_matched and incumbent.gross == before:
                record["converged"] = True
                break
    except sp.PolishStop as stop:
        record["stop"] = stop.reason
    record.update(quotes=ledger.used - snapshot.used, gross=str(incumbent.gross),
                  allowance_seconds=allowance)  # fmt: skip
    return incumbent, record


# ------------------------------------------------------------------ solve


def solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    meter = sp._active_meter()
    if meter is None:  # a direct (unmetered) call: meter it here so the base's quotes count
        with metered_quotes(None) as own:
            return _solve(case, context, budget, own)
    return _solve(case, context, budget, meter)


def _solve(case: Case, context: SolveContext, budget: Budget, meter: QuoteMeter) -> SolveResult:
    prepared: PreparedMarginalActivation = context.prepared
    settings = prepared.settings
    arm = prepared.options["arm"]
    record: dict[str, Any] = {
        "contract": CONTRACT,
        **dict(prepared.options),
        "scope": None,
        "truncated_by": None,
        "not_reached": None,
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
    base = sp.BASES[prepared.base].solve(
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
    ledger = sp.Ledger(budget.max_quotes, deadline, used=base_quotes)
    evaluation = base.evaluation
    try:
        if evaluation is None or evaluation.status is not EvalStatus.OK:  # replay it, charged
            evaluation = evaluate(context.bundle, case, base.plan, context.objective,
                                  quote=ledger.quote)  # fmt: skip
    except sp.PolishStop as stop:
        record.update(scope="not_reached", truncated_by=stop.reason, not_reached=NOT_REACHED)
        return unchanged
    if evaluation.status is not EvalStatus.OK:
        raise ValueError(f"the base's ok plan does not evaluate: {evaluation.error}")
    e1 = sp.polish_plan(
        context.bundle, case, evaluation, ledger, settings.polish, context.report_candidate
    )
    final = e1.incumbent
    used = ledger.used  # the returned arm's ledger (a control replaces it with its own)
    record.update(
        scope=e1.scope,
        reason=e1.reason,
        truncated_by=e1.truncated_by,
        base_gross=str(evaluation.gross_output),
        e1={
            "gross": str(evaluation.gross_output if final is None else final.gross),
            "quotes": ledger.used - base_quotes,
            "truncated_by": e1.truncated_by,
            "work": e1.work,
        },
    )
    if final is not None and e1.truncated_by is not None:
        record["not_reached"] = NOT_REACHED  # kept in every denominator, never matched
    elif final is not None:
        snapshot = Snapshot.of(final, ledger)
        if arm == "treatment":
            activation = run_activation(context.bundle, case, final, ledger, settings,
                                        prepared.adjacency, context.objective)  # fmt: skip
            used = ledger.used
            record.update(activation=activation, truncated_by=activation["truncated_by"])
        else:  # the treatment's activation stage as an uncharged reference, then the control
            final.publish = None
            with metered_quotes(None):
                activation = run_activation(context.bundle, case, final, ledger, settings,
                                            prepared.adjacency, context.objective)  # fmt: skip
            activation["charged"] = False
            allowance = None if deadline is None else max(0.0, deadline - snapshot.at)
            final, control = run_control(
                context.bundle, case, snapshot, arm, activation, budget.max_quotes, allowance,
                settings.polish, context.report_candidate,
            )  # fmt: skip
            used = snapshot.used + control["quotes"]
            record.update(activation=activation, control=control, truncated_by=control["stop"])
    record.update(
        gross=str(evaluation.gross_output if final is None else final.gross),
        quotes=used - base_quotes,
    )
    if final is None or final.source == "base":
        return unchanged  # refused, or nothing strictly better: the base result itself
    stage = record.get("control", record.get("activation", {}))  # the returned arm's own work
    simulations = e1.work.get("simulations", 0) + stage.get("work", {}).get("simulations", 0)
    return SolveResult(
        case_id=case.case_id,
        algorithm=NAME,
        status=SolveStatus.OK,
        plan=final.plan,
        evaluation=None,
        score=final.gross,
        candidates_considered=base.candidates_considered + simulations,
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
    options_validator=validate_options,
)

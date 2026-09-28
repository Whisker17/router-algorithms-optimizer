"""Colleague fixed-plan adapter (WHI-1537): the SWAP-only M3 funding-graph subset of
docs/references/colleague-routing-contract.md §§5.1-5.5 recovered as an ordinary
ordered `RoutePlan` and replayed by the unchanged `routing.evaluator.evaluate`.

Not a solver, compiler or second plan standard. `adapt` takes an already chosen plan
(slots, shares, call order) and, for one frozen bundle and request only:

1. **Validates the whole source graph before quoting or eliding anything**: known pool
   and direction per slot tokens, fresh single producers, producer-before-consumer (an
   unproduced slot is never read as zero), exclusive merge members closed by their
   merge, BPS groups within 10000 and closed by a final REMAINDER, ALL only on an
   exclusively owned slot, every non-target slot consumed, and an acyclic token graph.
   These hold even where every amount is zero.
2. **Resolves** each instruction in the selected order: BPS takes
   `floor(base * bps / 10000)` of the slot's frozen produced balance (never a flattened
   product of ratios), REMAINDER takes `base - consumed`, a merge freezes its members'
   sum as a new base. A zero input makes no quote and produces a ready zero output.
   Every non-zero swap is quoted on one evolving state per physical pool; a failed,
   incomplete or partial quote fails the whole plan with its reason (no fallback).
3. **Recovers** the `RoutePlan` (contract §5.3): one `SwapStep` per SWAP in instruction
   order, explicit integer draws, `ALL_REMAINING` for ALL/REMAINDER, merged slots
   referenced through their members (greedy positive takes in ascending slot order).
   Approved G-1 elision: a zero reference to a fund the evaluator already treats as
   drained (or to the output of an elided step) is omitted, and a zero step with no
   representable input left is omitted. Each omission stays visible in `SourceRow`.
4. **Replays** the recovered plan independently through `evaluate` and requires every
   real call's pool/amounts/zero status, the final per-pool states and the terminal
   balances to match the source resolution.

Nonempty JIT/candidate sets are `unsupported` (contract §6: D-P1..D-P4 undecided);
`policy_off=True` replays the baseline pool as an ordinary SWAP and labels it an
adaptation. Every quote -- resolution and replay -- goes through `quote` (default the
metered `pools.quote.quote_exact_in`), so a solve that calls `adapt` pays for both
passes under its own budget; nothing is cached between calls.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pools.quote import quote_exact_in
from pools.result import QuoteStatus
from routing.algorithms.base import SolveStatus
from routing.evaluator import (
    EvalStatus,
    Evaluation,
    QuoteFn,
    _is_id,
    _is_int,
    _token_cycle,
    evaluate,
)
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from snapshot.models import Case, PoolState, SnapshotBundle

if TYPE_CHECKING:
    from benchmark.objective import ObjectiveContext

BPS_DENOMINATOR = 10_000


class Mode(StrEnum):
    ALL = "ALL"
    BPS = "BPS"
    REMAINDER = "REMAINDER"


@dataclass(frozen=True)
class Single:
    """Draw from one slot: `ALL` (its sole consumer), `BPS` of its frozen base, or the
    final `REMAINDER`. `bps` is only meaningful for `BPS` and must be 0 otherwise."""

    slot: int
    mode: Mode
    bps: int = 0


@dataclass(frozen=True)
class MergeInto:
    """Consume and close every member, freeze their sum as `merged_slot`, then take the
    first branch (`ALL` or `BPS`) from it. Later branches are `Single` draws on
    `merged_slot` ending with `REMAINDER`."""

    merged_slot: int
    mode: Mode
    bps: int
    members: tuple[int, ...]


@dataclass(frozen=True)
class Swap:
    """One M3 SWAP. A nonempty `candidates` set makes it a source JIT_SWAP hop whose
    baseline is `pool_id`."""

    pool_id: str
    input: Single | MergeInto
    out: int
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class ColleaguePlan:
    """`slot_tokens[k]` is slot k's token; slot 0 is the order input."""

    slot_tokens: tuple[str, ...]
    program: tuple[Swap, ...]


@dataclass(frozen=True)
class SourceRow:
    """One source instruction as resolved. `draws` is its consumption relation
    `(slot, amount)`, kept even at zero: merge members at their full balance, then the
    draw from the input (or merged) slot. `plan_step` is the recovered step's index, or
    `None` for a G-1 elided zero row; `elided_inputs` are the fund references omitted."""

    n: int
    pool_id: str
    token_in: str
    token_out: str
    draws: tuple[tuple[int, int], ...]
    merged_base: int | None
    amount_in: int
    amount_out: int
    plan_step: int | None
    elided_inputs: tuple[str, ...] = ()

    @property
    def zero_input(self) -> bool:
        return self.amount_in == 0


@dataclass(frozen=True)
class Adaptation:
    """`terminals` maps every never-consumed target-token slot to its balance (zero ones
    included). `quote_status` keeps the underlying status of a failed/partial quote."""

    status: SolveStatus
    plan: RoutePlan | None = None
    evaluation: Evaluation | None = None
    source_trace: tuple[SourceRow, ...] = ()
    terminals: Mapping[int, int] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()
    error: str | None = None
    quote_status: QuoteStatus | None = None


def fund_id(slot: int) -> str:
    return REQUEST_FUND_ID if slot == 0 else f"s{slot}"


def _check_source(bundle: SnapshotBundle, case: Case, plan: ColleaguePlan) -> str | None:
    """The first structural violation of the complete source plan, or `None`. Depends
    on no amount, so nothing zero-valued can hide a violation."""
    if not _is_int(case.amount_in) or case.amount_in <= 0:
        return f"order input must be a positive integer, got {case.amount_in!r}"
    if case.token_in == case.token_out:
        return f"token_in and token_out are both {case.token_in!r}"
    tokens = plan.slot_tokens
    if not tokens or not all(_is_id(t) for t in tokens):
        return "slot_tokens must be a non-empty list of token ids"
    if tokens[0] != case.token_in:
        return f"slot 0 holds {tokens[0]!r}, not the order input token {case.token_in!r}"

    ready = {0}
    closed: set[int] = set()
    consumers: dict[int, int] = {}
    bps_used: dict[int, int] = {}
    edges: dict[str, set[str]] = {}

    def slot_error(n: int, slot: object) -> str | None:
        if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot < len(tokens):
            return f"#{n}: {slot!r} is not a declared slot"
        return None

    def draw(n: int, slot: int, mode: object, bps: object) -> str | None:
        if not isinstance(mode, Mode):
            return f"#{n}: unknown mode {mode!r}"
        if slot not in ready:
            return f"#{n}: slot {slot} is consumed before it is produced (never read as zero)"
        if slot in closed:
            return f"#{n}: slot {slot} is already closed by an earlier consumer (reuse)"
        if mode is Mode.BPS:
            if isinstance(bps, bool) or not isinstance(bps, int) or not 1 <= bps <= BPS_DENOMINATOR:
                return f"#{n}: BPS share {bps!r} is outside 1..{BPS_DENOMINATOR}"
            used = bps_used.get(slot, 0) + bps
            if used > BPS_DENOMINATOR:
                return f"#{n}: BPS shares of slot {slot} total {used} > {BPS_DENOMINATOR}"
            bps_used[slot] = used
        elif bps != 0:
            return f"#{n}: {mode.value} carries bps {bps!r}; only BPS takes a share"
        elif mode is Mode.ALL and consumers.get(slot):
            return f"#{n}: ALL on slot {slot}, which already has a consumer"
        else:
            closed.add(slot)
        consumers[slot] = consumers.get(slot, 0) + 1
        return None

    mode: object
    for n, ins in enumerate(plan.program):
        pool = bundle.pools.get(ins.pool_id) if _is_id(ins.pool_id) else None
        if pool is None:
            return f"#{n}: unknown pool {ins.pool_id!r} (no state in the frozen bundle)"
        inp = ins.input
        if isinstance(inp, MergeInto):
            merged = inp.merged_slot
            for s in (*inp.members, merged):
                if (err := slot_error(n, s)) is not None:
                    return err
            if len(inp.members) < 2:
                return f"#{n}: a merge needs at least two members, got {list(inp.members)}"
            if len(set(inp.members)) != len(inp.members):
                return f"#{n}: merge member listed twice (member reuse): {list(inp.members)}"
            for m in inp.members:
                if tokens[m] != tokens[merged]:
                    return (
                        f"#{n}: member slot {m} holds {tokens[m]!r}, merged slot {merged} "
                        f"holds {tokens[merged]!r}"
                    )
                if (err := draw(n, m, Mode.ALL, 0)) is not None:
                    return err
            if merged in ready:
                return f"#{n}: merged slot {merged} is not fresh (duplicate producer)"
            ready.add(merged)
            if inp.mode is Mode.REMAINDER:
                return f"#{n}: a merge's first branch must be ALL or BPS"
            src, mode, bps = merged, inp.mode, inp.bps
        elif isinstance(inp, Single):
            if (err := slot_error(n, inp.slot)) is not None:
                return err
            src, mode, bps = inp.slot, inp.mode, inp.bps
        else:
            return f"#{n}: unknown input form {inp!r}"
        if (err := draw(n, src, mode, bps)) is not None:
            return err
        if (err := slot_error(n, ins.out)) is not None:
            return err
        token_in, token_out = tokens[src], tokens[ins.out]
        if token_in not in (pool.token0, pool.token1):
            return f"#{n}: slot {src} holds {token_in!r}, not a token of pool {ins.pool_id!r}"
        if pool.other_token(token_in) != token_out:
            return (
                f"#{n}: output slot {ins.out} holds {token_out!r}, but pool {ins.pool_id!r} "
                f"yields {pool.other_token(token_in)!r}"
            )
        if ins.out in ready:
            return f"#{n}: output slot {ins.out} is not fresh (duplicate producer)"
        ready.add(ins.out)
        edges.setdefault(token_in, set()).add(token_out)

    cycle = _token_cycle(edges)
    if cycle is not None:
        return "economic token cycle: " + " -> ".join(cycle)
    for s in sorted(ready - closed):
        if s in bps_used:
            return f"slot {s}: BPS group has no final REMAINDER"
        if tokens[s] != case.token_out:
            return f"slot {s} ({tokens[s]!r}) is never consumed"
    return None


def adapt(
    bundle: SnapshotBundle,
    case: Case,
    plan: ColleaguePlan,
    objective: ObjectiveContext,
    *,
    policy_off: bool = False,
    quote: QuoteFn = quote_exact_in,
) -> Adaptation:
    """Validate, resolve, recover and replay `plan` (see the module docstring)."""
    error = _check_source(bundle, case, plan)
    if error is not None:
        return Adaptation(SolveStatus.INVALID_PLAN, error=error)
    jit = [n for n, ins in enumerate(plan.program) if ins.candidates]
    if jit and not policy_off:
        return Adaptation(
            SolveStatus.UNSUPPORTED,
            error=(
                f"instructions {jit} carry JIT/candidate sets; candidate policy is "
                "unsupported (contract §6: D-P1..D-P4 undecided)"
            ),
        )
    diagnostics = [
        f"#{n}: policy_off adaptation: candidates {list(plan.program[n].candidates)} "
        f"ignored, baseline pool {plan.program[n].pool_id!r} replayed as an ordinary SWAP "
        "(not the source policy)"
        for n in jit
    ]

    tokens = plan.slot_tokens
    base: dict[int, int] = {0: case.amount_in}  # source ledger: frozen produced balances
    taken: dict[int, int] = {}
    members_of: dict[int, tuple[int, ...]] = {}
    left: dict[str, int] = {REQUEST_FUND_ID: case.amount_in}  # recovered-plan ledger
    drained: set[str] = set()  # as the evaluator marks them: balance 0 after a consumer
    elided: set[str] = set()  # output funds of elided zero steps
    states: dict[str, PoolState] = {}
    steps: list[SwapStep] = []
    rows: list[SourceRow] = []

    def share(slot: int, mode: Mode, bps: int) -> int:
        if mode is Mode.BPS:
            amount = base[slot] * bps // BPS_DENOMINATOR
        else:
            amount = base[slot] - taken.get(slot, 0)
        taken[slot] = taken.get(slot, 0) + amount
        return amount

    for n, ins in enumerate(plan.program):
        inp = ins.input
        draws: list[tuple[int, int]] = []
        merged_base = None
        if isinstance(inp, MergeInto):
            members = tuple(sorted(inp.members))
            draws = [(m, share(m, Mode.ALL, 0)) for m in members]
            merged_base = base[inp.merged_slot] = sum(a for _, a in draws)
            members_of[inp.merged_slot] = members
            src = inp.merged_slot
        else:
            src = inp.slot
        mode, bps = inp.mode, inp.bps
        amount = share(src, mode, bps)
        draws.append((src, amount))

        refs: list[tuple[str, int | Literal["ALL_REMAINING"]]] = []
        if src not in members_of:
            refs = [(fund_id(src), amount if mode is Mode.BPS else ALL_REMAINING)]
        elif mode is Mode.ALL:
            refs = [(fund_id(m), ALL_REMAINING) for m in members_of[src]]
        else:
            live = [
                fund_id(m)
                for m in members_of[src]
                if fund_id(m) not in drained and fund_id(m) not in elided
            ]
            if mode is Mode.REMAINDER:
                refs = [(f, ALL_REMAINING) for f in live if left[f] > 0]
            else:
                need = amount
                for f in live:
                    take = min(need, left[f])
                    if take > 0:
                        refs.append((f, take))
                        need -= take
            if not refs:  # a zero draw keeps its lowest still-referenceable member
                zero: int | Literal["ALL_REMAINING"] = (
                    ALL_REMAINING if mode is Mode.REMAINDER else 0
                )
                refs = [(live[0] if live else fund_id(members_of[src][0]), zero)]

        kept: list[tuple[str, int | Literal["ALL_REMAINING"]]] = []
        dropped: list[str] = []
        for f, a in refs:
            if (f in elided or f in drained) and a in (0, ALL_REMAINING):
                dropped.append(f)  # G-1: a zero reference the evaluator cannot represent
            else:
                kept.append((f, a))  # a positive one stays and fails replay if invalid
        for f, a in kept:
            left[f] = left.get(f, 0) - (left.get(f, 0) if a == ALL_REMAINING else a)
            if left[f] == 0:
                drained.add(f)
        token_in, token_out = tokens[src], tokens[ins.out]
        plan_step = None
        if kept:
            inputs = tuple(FundInput(f, a) for f, a in kept)
            steps.append(SwapStep(ins.pool_id, token_in, token_out, inputs, fund_id(ins.out)))
            plan_step = len(steps) - 1
        else:
            elided.add(fund_id(ins.out))
        if dropped:
            what = "zero-input step" if plan_step is None else "zero reference(s)"
            diagnostics.append(f"#{n}: G-1 elided {what} to drained/elided {dropped}")

        amount_out = 0
        if amount:
            state = states.get(ins.pool_id, bundle.pools[ins.pool_id])
            result = quote(state, token_in, amount)
            if (
                result.status is not QuoteStatus.OK
                or result.new_state is None
                or result.amount_in_consumed != amount
            ):
                return Adaptation(
                    SolveStatus.INVALID_PLAN,
                    source_trace=tuple(rows),
                    diagnostics=tuple(diagnostics),
                    error=(
                        f"#{n}: pool {ins.pool_id!r} quote {result.status.value} consumed "
                        f"{result.amount_in_consumed} of {amount}: {result.detail}"
                    ),
                    quote_status=result.status,
                )
            states[ins.pool_id] = result.new_state
            amount_out = result.amount_out
        base[ins.out] = amount_out
        if plan_step is not None:
            left[fund_id(ins.out)] = amount_out
        rows.append(
            SourceRow(
                n,
                ins.pool_id,
                token_in,
                token_out,
                tuple(draws),
                merged_base,
                amount,
                amount_out,
                plan_step,
                tuple(dropped),
            )
        )

    terminals = {s: v for s, v in base.items() if s not in taken and tokens[s] == case.token_out}
    route = RoutePlan(tuple(steps))
    evaluation = evaluate(bundle, case, route, objective, quote=quote)
    mismatch = _mismatch(rows, terminals, states, evaluation)
    return Adaptation(
        SolveStatus.OK if mismatch is None else SolveStatus.INVALID_PLAN,
        plan=route if mismatch is None else None,
        evaluation=evaluation,
        source_trace=tuple(rows),
        terminals=terminals,
        diagnostics=tuple(diagnostics),
        error=mismatch,
    )


def _mismatch(
    rows: list[SourceRow],
    terminals: Mapping[int, int],
    states: Mapping[str, PoolState],
    ev: Evaluation,
) -> str | None:
    """Where the recovered plan's replay departs from the source resolution: every
    real call and kept zero row, the final pool states and the terminal balances."""
    if ev.status is not EvalStatus.OK:
        return f"recovered plan is {ev.status.value}: {ev.error}"
    if len(ev.trace) != sum(r.plan_step is not None for r in rows):
        return f"replay has {len(ev.trace)} steps for {len(rows)} source rows"
    for r in rows:
        if r.plan_step is None:
            if r.amount_in:
                return f"#{r.n}: an elided row carries input {r.amount_in}"
            continue
        t = ev.trace[r.plan_step]
        got = (t.pool_id, t.amount_in, t.amount_out, t.status == "zero_input")
        if got != (r.pool_id, r.amount_in, r.amount_out, r.zero_input):
            return f"#{r.n}: replay {got} differs from source row {r}"
    if dict(ev.next_states) != dict(states):
        return "replayed pool states differ from the source resolution"
    balances = {f.fund_id: f.remaining for f in ev.funds if f.remaining}
    if balances != {fund_id(s): v for s, v in terminals.items() if v}:
        return f"replay balances {balances} differ from source terminals {dict(terminals)}"
    return None

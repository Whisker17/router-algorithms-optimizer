"""WHI-1537: colleague fixed-plan adapter (docs/references/colleague-routing-contract.md §5).

Expected values never come from the adapter under test:

- `docs/references/colleague-design/fixtures.json` (WHI-1536): hand-derived traces,
  recovered plans and evaluations; pool outputs are a scripted pure `(pool, amount_in)`
  table fed through the evaluator's quote seam. The research checker is not imported.
- Plans written here carry their derivation beside each literal.
- Real CPMM state (`tests/fixtures/routing/cpmm_graph`): `_v2_out` restates
  `UniswapV2Library.getAmountOut`, applied by hand to the fixture reserves and pinned as
  literals.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from benchmark.objective import gross_only
from pools.quote import QuoteLimitExceeded, metered_quotes, quote_exact_in
from pools.result import QuoteStatus, SwapResult
from routing.algorithms.base import SolveStatus
from routing.colleague_plan import (
    Adaptation,
    ColleaguePlan,
    MergeInto,
    Mode,
    Single,
    Swap,
    adapt,
)
from routing.evaluator import EvalStatus, QuoteFn, evaluate
from routing.plan import ALL_REMAINING, FundInput, RoutePlan, SwapStep
from snapshot.bundle import load_bundle
from snapshot.models import BlockRef, Case, ConstantProductPoolState, PoolState, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
FIXTURES = json.loads(
    (REPO / "docs/references/colleague-design/fixtures.json").read_text(encoding="utf-8")
)
PLANS = {fx["id"].split("-")[0]: fx for fx in FIXTURES["plans"]}
OBJ = gross_only()
ALL, BPS, REM = Mode.ALL, Mode.BPS, Mode.REMAINDER


# --- helpers --------------------------------------------------------------------------


def synthetic(pools: dict[str, tuple[str, str]]) -> SnapshotBundle:
    """Pools whose reserves are never used: every output comes from a scripted table."""
    return SnapshotBundle(
        bundle_id="colleague",
        kind="synthetic",
        schema_version=1,
        block=BlockRef(chain_id=5000, number=1, hash="0x" + "00" * 32, timestamp=0),
        pools={
            p: ConstantProductPoolState(p, a, b, 10**30, 10**30, 0) for p, (a, b) in pools.items()
        },
        cases=(),
        bundle_hash="-",
        source_path="<test>",
    )


def scripted(table: dict[tuple[str, int], int], calls: list[str] | None = None) -> QuoteFn:
    def quote(state: PoolState, token_in: str, amount: int) -> SwapResult[PoolState]:
        if calls is not None:
            calls.append(state.pool_id)
        return SwapResult(QuoteStatus.OK, amount, table[(state.pool_id, amount)], state)

    return quote


def no_quote(state: PoolState, token_in: str, amount: int) -> SwapResult[PoolState]:
    raise AssertionError(f"quoted {state.pool_id} before the source plan was validated")


def table(rows: list[dict[str, str]]) -> dict[tuple[str, int], int]:
    return {(r["pool"], int(r["amount_in"])): int(r["amount_out"]) for r in rows}


def source_plan(fx: dict[str, Any]) -> ColleaguePlan:
    program = []
    for ins in fx["program"]:
        kind, *rest = ins["input"]
        if kind == "SINGLE":
            inp: Single | MergeInto = Single(rest[0], Mode(rest[1]), rest[2])
        else:
            inp = MergeInto(rest[0], Mode(rest[1]), rest[2], tuple(rest[3]))
        program.append(Swap(ins["pool"], inp, ins["out"]))
    return ColleaguePlan(tuple(fx["slot_tokens"]), tuple(program))


def fixture_case(fx: dict[str, Any]) -> Case:
    c = fx["case"]
    return Case("fixture", c["token_in"], c["token_out"], int(c["amount_in"]))


def as_json(plan: RoutePlan | None) -> list[dict[str, Any]]:
    assert plan is not None
    return [
        {
            "pool": s.pool_id,
            "token_in": s.token_in,
            "token_out": s.token_out,
            "inputs": [
                [i.fund_id, i.amount if i.amount == ALL_REMAINING else str(i.amount)]
                for i in s.inputs
            ],
            "out": s.output_fund_id,
        }
        for s in plan.steps
    ]


def from_json(steps: list[dict[str, Any]]) -> RoutePlan:
    return RoutePlan(
        tuple(
            SwapStep(
                s["pool"],
                s["token_in"],
                s["token_out"],
                tuple(
                    FundInput(f, ALL_REMAINING if a == ALL_REMAINING else int(a))
                    for f, a in s["inputs"]
                ),
                s["out"],
            )
            for s in steps
        )
    )


def run_fixture(fx: dict[str, Any], **kw: Any) -> Adaptation:
    quote = kw.pop("quote", scripted(table(fx["scripted_outputs"])))
    return adapt(
        synthetic(fx["pools"]),
        fixture_case(fx),
        kw.pop("plan", source_plan(fx)),
        OBJ,
        quote=quote,
        **kw,
    )


def assert_ok(result: Adaptation) -> RoutePlan:
    assert result.status is SolveStatus.OK, result.error
    assert result.evaluation is not None and result.evaluation.status is EvalStatus.OK
    assert result.plan is not None
    return result.plan


# --- F1-F4, F6: committed hand-derived goldens ----------------------------------------


@pytest.mark.parametrize("key", ["F1", "F2", "F3", "F4", "F6"])
def test_fixture_goldens_through_the_adapter(key: str) -> None:
    fx = PLANS[key]
    result = run_fixture(fx)
    assert_ok(result)
    ev = result.evaluation
    assert ev is not None

    rows = [
        {
            "n": r.n,
            "amount_in": str(r.amount_in),
            "amount_out": str(r.amount_out),
            **({"zero_input": True} if r.zero_input else {}),
            **({"merged_base": str(r.merged_base)} if r.merged_base is not None else {}),
        }
        for r in result.source_trace
    ]
    keys = ("n", "amount_in", "amount_out", "zero_input", "merged_base")
    assert rows == [{k: e[k] for k in keys if k in e} for e in fx["expected_trace"]]

    # F6 is the G-1 gap: its fixture golden only replays after the approved elision.
    want = fx.get("elided_resolution_candidate", fx)
    assert as_json(result.plan) == want["route_plan" if key == "F6" else "expected_route_plan"]
    ev_want = want["expected_evaluation"]
    assert (ev.status.value, str(ev.gross_output)) == (ev_want["status"], ev_want["gross_output"])
    assert ev.route_features["pool_calls"] == ev_want["pool_calls"]
    assert ev.route_features["zero_input_steps"] == ev_want["zero_input_steps"]
    assert ev.gross_output == sum(result.terminals.values())

    # Trace equality over real calls and kept zero rows; elided rows are mapped, not counted.
    kept = [r for r in result.source_trace if r.plan_step is not None]
    assert [(t.pool_id, t.amount_in, t.amount_out) for t in ev.trace] == [
        (r.pool_id, r.amount_in, r.amount_out) for r in kept
    ]
    assert [r.n for r in result.source_trace if r.plan_step is None] == ([1] if key == "F6" else [])

    if key == "F6":  # without elision the unchanged evaluator rejects the zero REMAINDER
        gap = evaluate(
            synthetic(fx["pools"]),
            fixture_case(fx),
            from_json(fx["expected_route_plan"]),
            OBJ,
            quote=scripted(table(fx["scripted_outputs"])),
        )
        assert gap.status is EvalStatus.INVALID_PLAN
        assert fx["expected_evaluation"]["error_contains"] in (gap.error or "")
        assert result.source_trace[1].elided_inputs == ("REQUEST",)
        assert result.diagnostics == (
            "#1: G-1 elided zero-input step to drained/elided ['REQUEST']",
        )

    # Negative variants of the recovered plan still fail in the unchanged evaluator.
    base = as_json(result.plan)
    for neg in fx.get("negative_variants", []):
        variant = [dict(s) for s in base]
        variant[neg["replace_step"]]["inputs"] = neg["inputs"]
        quote = scripted({**table(fx["scripted_outputs"]), **table(neg["scripted_outputs"])})
        nev = evaluate(
            synthetic(fx["pools"]), fixture_case(fx), from_json(variant), OBJ, quote=quote
        )
        assert nev.status is EvalStatus.INVALID_PLAN
        assert neg["expected_error_contains"] in (nev.error or "")


def test_integer_rules_main_split_and_merge_then_split() -> None:
    """Acceptance 1 with the source numbers (IR-1, IR-4) read off the adapter's rows."""
    f1 = run_fixture(PLANS["F1"])
    assert [r.amount_in for r in f1.source_trace[:3]] == [500_000_000, 350_000_000, 150_000_001]
    assert f1.source_trace[4].merged_base == 298_000_000_000_000_000  # 0.150+0.104+0.044 WETH
    f3 = run_fixture(PLANS["F3"])
    merge, rest = f3.source_trace[2], f3.source_trace[3]
    assert merge.draws == ((1, 101), (2, 199), (3, 180))  # 101 + 199 = 300; floor(300*0.6)
    assert (merge.merged_base, merge.amount_in, rest.amount_in) == (300, 180, 120)
    # Member allocation is deterministic: ascending slots whatever the listed order.
    listed = source_plan(PLANS["F3"])
    program = list(listed.program)
    program[2] = replace(program[2], input=MergeInto(3, BPS, 6000, (2, 1)))
    reordered = run_fixture(PLANS["F3"], plan=replace(listed, program=tuple(program)))
    assert reordered.plan == f3.plan
    assert assert_ok(f3).steps[2].inputs == (FundInput("s1", 101), FundInput("s2", 79))
    # Flattening the remainder to its nominal 1500 bps is a source error, found before quoting.
    flat = source_plan(PLANS["F1"])
    program = list(flat.program)
    program[2] = replace(program[2], input=Single(0, BPS, 1500))
    bad = run_fixture(PLANS["F1"], plan=replace(flat, program=tuple(program)), quote=no_quote)
    assert bad.status is SolveStatus.INVALID_PLAN
    assert bad.error == "slot 0: BPS group has no final REMAINDER"


def test_nested_rounding_on_a_realized_balance_through_a_supported_shape() -> None:
    """IR-2 / F5's integer rule, 3 -> 50% -> 80%, through an admitted shape: the second
    layer splits a swap output, never a same-token child of a split. F5's own shape (a
    split of a split child with no swap between) has no SWAP-only instruction form here:
    slots come only from swaps and merges, so it stays unsupported (contract §5.4).

    Scripted 1:1 pools. X = floor(3*5000/10000) = 1 -> AB -> 1 B; Y = 3 - 1 = 2 -> AC;
    X1 = floor(1*8000/10000) = 0 (flattened floor(3*4000/10000) = 1 is invalid);
    X2 = 1 - 0 = 1. Terminals 2 + 0 + 1 = 3."""
    pools = {"AB": ("A", "B"), "AC": ("A", "C"), "BC1": ("B", "C"), "BC2": ("B", "C")}
    plan = ColleaguePlan(
        ("A", "B", "C", "C", "C"),
        (
            Swap("AB", Single(0, BPS, 5000), 1),
            Swap("AC", Single(0, REM), 2),
            Swap("BC1", Single(1, BPS, 8000), 3),
            Swap("BC2", Single(1, REM), 4),
        ),
    )
    quote = scripted({("AB", 1): 1, ("AC", 2): 2, ("BC2", 1): 1})
    result = adapt(synthetic(pools), Case("f5", "A", "C", 3), plan, OBJ, quote=quote)
    assert_ok(result)
    allocations = {
        k: r.amount_in for k, r in zip(("X", "Y", "X1", "X2"), result.source_trace, strict=True)
    }
    want = PLANS["F5"]["expected_allocations"]
    assert allocations == {k: int(v) for k, v in want.items()} == {"X": 1, "Y": 2, "X1": 0, "X2": 1}
    assert allocations["X1"] != 3 * 4000 // 10_000 == 1
    assert as_json(result.plan) == [
        {
            "pool": "AB",
            "token_in": "A",
            "token_out": "B",
            "inputs": [["REQUEST", "1"]],
            "out": "s1",
        },
        {
            "pool": "AC",
            "token_in": "A",
            "token_out": "C",
            "inputs": [["REQUEST", ALL_REMAINING]],
            "out": "s2",
        },
        {"pool": "BC1", "token_in": "B", "token_out": "C", "inputs": [["s1", "0"]], "out": "s3"},
        {
            "pool": "BC2",
            "token_in": "B",
            "token_out": "C",
            "inputs": [["s1", ALL_REMAINING]],
            "out": "s4",
        },
    ]
    assert result.evaluation is not None and result.evaluation.gross_output == 3
    assert result.evaluation.route_features["zero_input_steps"] == 1


# --- zero allocation, closure and G-1 propagation -------------------------------------


def test_zero_first_allocation_keeps_merge_closure_and_terminals() -> None:
    """Acceptance 2 on F4: members s1 (0) and s2 (1) are consumed by the merge even though
    its first branch is 0; they are not terminals and cannot be reused."""
    fx = PLANS["F4"]
    result = run_fixture(fx)
    assert_ok(result)
    merge = result.source_trace[2]
    assert merge.draws == ((1, 0), (2, 1), (3, 0)) and merge.merged_base == 1
    assert merge.zero_input and merge.plan_step == 2  # kept as a zero step, not elided
    assert dict(result.terminals) == {4: 0, 5: 1}  # never s1, s2 or the merged slot 3
    ev = result.evaluation
    assert ev is not None
    assert {f.fund_id: f.remaining for f in ev.funds if f.remaining} == {"s5": 1}

    plan = source_plan(fx)
    for reuse in (Single(1, ALL), Single(2, REM)):
        program = list(plan.program)
        program[3] = replace(program[3], input=reuse)
        bad = run_fixture(fx, plan=replace(plan, program=tuple(program)), quote=no_quote)
        assert bad.status is SolveStatus.INVALID_PLAN and bad.plan is None
        assert f"slot {reuse.slot} is already closed" in (bad.error or "")


def test_g1_elides_zero_propagation_without_hiding_a_call() -> None:
    """Scripted: 5 A; #0 BPS 10000 takes 5 -> AB1 -> 4 B and drains REQUEST; #1 REMAINDER
    = 0 has no representable reference (G-1) -> elided, s2 is a ready zero; #2/#3 split
    that zero parent (floor(0*0.5) = 0, 0 - 0 = 0) -> elided; #4 ALL of s1 = 4 -> 3 C."""
    pools = {p: ("A", "B") for p in ("AB1", "AB2")} | {p: ("B", "C") for p in ("BC1", "BC2", "BC3")}
    plan = ColleaguePlan(
        ("A", "B", "B", "C", "C", "C"),
        (
            Swap("AB1", Single(0, BPS, 10_000), 1),
            Swap("AB2", Single(0, REM), 2),
            Swap("BC1", Single(2, BPS, 5000), 3),
            Swap("BC2", Single(2, REM), 4),
            Swap("BC3", Single(1, ALL), 5),
        ),
    )
    calls: list[str] = []
    quote = scripted({("AB1", 5): 4, ("BC3", 4): 3}, calls)
    result = adapt(synthetic(pools), Case("zero", "A", "C", 5), plan, OBJ, quote=quote)
    assert_ok(result)
    rows = result.source_trace
    assert [r.plan_step for r in rows] == [0, None, None, None, 1]
    assert [r.elided_inputs for r in rows] == [(), ("REQUEST",), ("s2",), ("s2",), ()]
    assert [r.draws for r in rows] == [((0, 5),), ((0, 0),), ((2, 0),), ((2, 0),), ((1, 4),)]
    assert sum("G-1 elided zero-input step" in d for d in result.diagnostics) == 3
    assert as_json(result.plan) == [
        {
            "pool": "AB1",
            "token_in": "A",
            "token_out": "B",
            "inputs": [["REQUEST", "5"]],
            "out": "s1",
        },
        {
            "pool": "BC3",
            "token_in": "B",
            "token_out": "C",
            "inputs": [["s1", ALL_REMAINING]],
            "out": "s5",
        },
    ]
    assert dict(result.terminals) == {3: 0, 4: 0, 5: 3}
    assert result.evaluation is not None and result.evaluation.gross_output == 3
    assert calls == ["AB1", "BC3", "AB1", "BC3"]  # resolution, then independent replay


def test_g1_drops_a_zero_merge_member_but_keeps_the_step() -> None:
    """5 A; #0 BPS 10000 -> AB1 -> 4 B drains REQUEST; #1 REMAINDER 0 is elided (s2 a
    ready zero); #2 merges s1 (4) and s2 (0): the merge still closes both members, but
    only s1 is representable in the RoutePlan. 4 -> BC1 -> 3."""
    pools = {p: ("A", "B") for p in ("AB1", "AB2")} | {"BC1": ("B", "C")}
    plan = ColleaguePlan(
        ("A", "B", "B", "B", "C"),
        (
            Swap("AB1", Single(0, BPS, 10_000), 1),
            Swap("AB2", Single(0, REM), 2),
            Swap("BC1", MergeInto(3, ALL, 0, (1, 2)), 4),
        ),
    )
    quote = scripted({("AB1", 5): 4, ("BC1", 4): 3})
    result = adapt(synthetic(pools), Case("zm", "A", "C", 5), plan, OBJ, quote=quote)
    steps = assert_ok(result).steps
    assert [s.inputs for s in steps] == [
        (FundInput("REQUEST", 5),),
        (FundInput("s1", ALL_REMAINING),),
    ]
    rows = result.source_trace
    assert [r.plan_step for r in rows] == [0, None, 1]
    assert rows[2].draws == ((1, 4), (2, 0), (3, 4)) and rows[2].elided_inputs == ("s2",)
    assert result.diagnostics[-1] == "#2: G-1 elided zero reference(s) to drained/elided ['s2']"
    assert result.evaluation is not None and result.evaluation.gross_output == 3


def test_zero_draw_from_a_merge_keeps_its_first_referenceable_member() -> None:
    """A zero BPS draw after s1 was drained references s2 at 0 instead of eliding a step
    that still has a representable input. 10 A -> 5/5 -> 5 B/5 B (scripted 1:1); merged
    10; #2 floor(10*0.5) = 5 greedy from s1 (drains it); #3 floor(10*1/10000) = 0;
    #4 10 - 5 - 0 = 5 = ALL of s2. Outputs 4 + 0 + 4 = 8."""
    pools = {p: ("A", "B") for p in ("AB1", "AB2")} | {p: ("B", "C") for p in ("BC1", "BC2", "BC3")}
    plan = ColleaguePlan(
        ("A", "B", "B", "B", "C", "C", "C"),
        (
            Swap("AB1", Single(0, BPS, 5000), 1),
            Swap("AB2", Single(0, REM), 2),
            Swap("BC1", MergeInto(3, BPS, 5000, (1, 2)), 4),
            Swap("BC2", Single(3, BPS, 1), 5),
            Swap("BC3", Single(3, REM), 6),
        ),
    )
    quote = scripted({("AB1", 5): 5, ("AB2", 5): 5, ("BC1", 5): 4, ("BC3", 5): 4})
    result = adapt(synthetic(pools), Case("zdraw", "A", "C", 10), plan, OBJ, quote=quote)
    assert_ok(result)
    assert [s.inputs for s in assert_ok(result).steps[2:]] == [
        (FundInput("s1", 5),),
        (FundInput("s2", 0),),
        (FundInput("s2", ALL_REMAINING),),
    ]
    assert [r.plan_step for r in result.source_trace] == [0, 1, 2, 3, 4]
    assert result.evaluation is not None and result.evaluation.gross_output == 8


# --- invalid source plans fail before any quote or elision ----------------------------

F3_POOLS = {
    "AB1": ("A", "B"),
    "AB2": ("A", "B"),
    "AB3": ("A", "B"),
    "BC1": ("B", "C"),
    "BC2": ("B", "C"),
    "AC1": ("A", "C"),
}
F3 = source_plan(PLANS["F3"])  # A -> 50%/remainder -> merge [1, 2] -> 60%/remainder -> C


def with_ins(n: int, **changes: Any) -> ColleaguePlan:
    program = list(F3.program)
    program[n] = replace(program[n], **changes)
    return replace(F3, program=tuple(program))


def with_tokens(k: int, token: str) -> ColleaguePlan:
    tokens = list(F3.slot_tokens)
    tokens[k] = token
    return replace(F3, slot_tokens=tuple(tokens))


INVALID: list[tuple[str, ColleaguePlan, Case | None, str]] = [
    ("duplicate producer", with_ins(1, out=1), None, "#1: output slot 1 is not fresh"),
    (
        "merged slot already produced",
        with_ins(2, input=MergeInto(2, BPS, 6000, (1, 2))),
        None,
        "#2: merged slot 2 is not fresh",
    ),
    (
        "forward reference",
        replace(F3, program=(*F3.program[:2], F3.program[3], F3.program[2])),
        None,
        "#2: slot 3 is consumed before it is produced",
    ),
    (
        "unproduced slot is not zero",
        with_ins(0, input=Single(5, ALL)),
        None,
        "#0: slot 5 is consumed before it is produced",
    ),
    ("output token mismatch", with_tokens(4, "B"), None, "#2: output slot 4 holds 'B'"),
    ("wrong pool direction", with_ins(2, pool_id="AB1"), None, "yields 'A'"),
    ("input token not in pool", with_ins(0, pool_id="BC1"), None, "not a token of pool 'BC1'"),
    ("merge member token mismatch", with_tokens(3, "C"), None, "member slot 1 holds 'B'"),
    ("slot 0 is not the order token", F3, Case("x", "B", "C", 1000), "slot 0 holds 'A'"),
    ("bps zero", with_ins(0, input=Single(0, BPS, 0)), None, "BPS share 0 is outside"),
    ("bps over 10000", with_ins(0, input=Single(0, BPS, 10_001)), None, "BPS share 10001"),
    ("bps group over 10000", with_ins(1, input=Single(0, BPS, 5001)), None, "total 10001"),
    (
        "group without remainder",
        with_ins(1, input=Single(0, BPS, 5000)),
        None,
        "slot 0: BPS group has no final REMAINDER",
    ),
    (
        "merged group without remainder",
        with_ins(3, input=Single(3, BPS, 4000)),
        None,
        "slot 3: BPS group has no final REMAINDER",
    ),
    (
        "remainder carries bps",
        with_ins(1, input=Single(0, REM, 5000)),
        None,
        "REMAINDER carries bps",
    ),
    (
        "merge first branch remainder",
        with_ins(2, input=MergeInto(3, REM, 0, (1, 2))),
        None,
        "first branch must be ALL or BPS",
    ),
    (
        "ALL on a shared slot",
        with_ins(1, input=Single(0, ALL)),
        None,
        "ALL on slot 0, which already has a consumer",
    ),
    (
        "second consumer of a merged ALL",
        with_ins(2, input=MergeInto(3, ALL, 0, (1, 2))),
        None,
        "#3: slot 3 is already closed",
    ),
    (
        "member listed twice",
        with_ins(2, input=MergeInto(3, BPS, 6000, (1, 1))),
        None,
        "member listed twice",
    ),
    ("single member", with_ins(2, input=MergeInto(3, BPS, 6000, (1,))), None, "at least two"),
    ("closed member reused", with_ins(3, input=Single(1, REM)), None, "slot 1 is already closed"),
    ("unknown pool (missing state)", with_ins(0, pool_id="nope"), None, "unknown pool 'nope'"),
    ("undeclared slot", with_ins(0, out=9), None, "9 is not a declared slot"),
    (
        "mode is not a Mode",
        with_ins(0, input=Single(0, "BPS", 5000)),  # type: ignore[arg-type]
        None,
        "unknown mode 'BPS'",
    ),
    ("empty program", replace(F3, program=()), None, "slot 0 ('A') is never consumed"),
    ("non-positive order", F3, Case("x", "A", "C", 0), "order input must be a positive"),
    # Zero-valued structure elision would otherwise hide: 5 A; BPS 10000 drains REQUEST, so
    # the remainder chain below is all zero and would be elided from the RoutePlan.
    (
        "cycle through zero steps",
        ColleaguePlan(
            ("A", "B", "B", "A", "C", "C"),
            (
                Swap("AB1", Single(0, BPS, 10_000), 1),
                Swap("AB2", Single(0, REM), 2),
                Swap("AB3", Single(2, ALL), 3),  # B -> A: the cycle edge
                Swap("AC1", Single(3, ALL), 4),
                Swap("BC1", Single(1, ALL), 5),
            ),
        ),
        Case("x", "A", "C", 5),
        "economic token cycle: A -> B -> A",
    ),
    (
        "zero intermediate never consumed",
        ColleaguePlan(
            ("A", "C", "B"),
            (
                Swap("AC1", Single(0, BPS, 10_000), 1),
                Swap("AB1", Single(0, REM), 2),
            ),
        ),
        Case("x", "A", "C", 5),
        "slot 2 ('B') is never consumed",
    ),
]


@pytest.mark.parametrize(
    ("plan", "case", "error"), [c[1:] for c in INVALID], ids=[c[0] for c in INVALID]
)
def test_invalid_source_plans_fail_before_quoting(
    plan: ColleaguePlan, case: Case | None, error: str
) -> None:
    case = case or Case("x", "A", "C", 1000)
    result = adapt(synthetic(F3_POOLS), case, plan, OBJ, quote=no_quote)
    assert result.status is SolveStatus.INVALID_PLAN
    assert result.plan is None and result.source_trace == ()
    assert error in (result.error or ""), result.error


def test_display_order_f7_is_a_forward_reference() -> None:
    """F7: N6 (the Z swap on F140) displayed before N5 (the merge producing F140). As an
    instruction that is Z drawing the merged slot 5 before any merge produced it."""
    fx = PLANS["F1"]
    plan = source_plan(fx)
    displayed = replace(
        plan, program=(*plan.program[:4], replace(plan.program[4], input=Single(5, ALL)))
    )
    bad = run_fixture(fx, plan=displayed, quote=no_quote)
    assert bad.status is SolveStatus.INVALID_PLAN
    assert bad.error == "#4: slot 5 is consumed before it is produced (never read as zero)"
    assert_ok(run_fixture(fx))  # the SEM_NODES / INSTRS order


# --- real pool state: selected order, one state per pool, fresh snapshot, charging ---


def _v2_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int = 30) -> int:
    keep = 10_000 - fee_bps
    return amount_in * keep * reserve_out // (reserve_in * 10_000 + amount_in * keep)


CPMM = load_bundle(REPO / "tests/fixtures/routing/cpmm_graph")
CASE = Case("cpmm", "TKA", "TKB", 200_000_000)
REPEATED = ColleaguePlan(
    ("TKA", "TKC", "TKC", "TKC", "TKB", "TKB"),
    (
        Swap("ac_1", Single(0, BPS, 5000), 1),
        Swap("ac_1", Single(0, REM), 2),
        Swap("cb_1", MergeInto(3, BPS, 6000, (1, 2)), 4),
        Swap("cb_1", Single(3, REM), 5),
    ),
)


def test_repeated_pools_keep_selected_order_and_one_state_per_pool() -> None:
    c1 = _v2_out(100_000_000, 1_000_000_000, 1_000_000)
    c2 = _v2_out(100_000_000, 1_100_000_000, 1_000_000 - c1)  # ac_1 after the first use
    merged = c1 + c2
    first = merged * 6000 // 10_000
    b1 = _v2_out(first, 1_000_000, 1_000_000_000)
    b2 = _v2_out(merged - first, 1_000_000 + first, 1_000_000_000 - b1)  # cb_1 after #2
    assert (c1, c2, merged, first, b1, b2) == (
        90_661,
        75_569,
        166_230,
        99_738,
        90_445_040,
        51_711_076,
    )
    assert quote_exact_in(CPMM.pools["ac_1"], "TKA", 100_000_000).amount_out == c1 > c2

    result = adapt(CPMM, CASE, REPEATED, OBJ)
    assert_ok(result)
    assert [(r.pool_id, r.amount_in, r.amount_out) for r in result.source_trace] == [
        ("ac_1", 100_000_000, c1),
        ("ac_1", 100_000_000, c2),
        ("cb_1", first, b1),
        ("cb_1", merged - first, b2),
    ]
    steps = assert_ok(result).steps
    assert [s.pool_id for s in steps] == ["ac_1", "ac_1", "cb_1", "cb_1"]  # never merged per pool
    assert steps[2].inputs == (FundInput("s1", 90_661), FundInput("s2", 9_077))  # greedy 99_738
    assert steps[3].inputs == (FundInput("s2", ALL_REMAINING),)  # 75_569 - 9_077 = 66_492
    ev = result.evaluation
    assert ev is not None
    assert ev.trace[3].inputs == (("s2", 66_492),)
    assert ev.gross_output == b1 + b2 == 142_156_116
    assert ev.route_features["repeated_pool_calls"] == 2
    ac_1, cb_1 = ev.next_states["ac_1"], ev.next_states["cb_1"]
    assert isinstance(ac_1, ConstantProductPoolState) and isinstance(cb_1, ConstantProductPoolState)
    assert (ac_1.reserve0, ac_1.reserve1) == (1_200_000_000, 1_000_000 - c1 - c2)
    assert (cb_1.reserve0, cb_1.reserve1) == (1_000_000 + merged, 1_000_000_000 - b1 - b2)


def test_selected_order_of_repeated_suffix_calls_is_retained() -> None:
    """Two valid orders of the same two cb_1 swaps give different, hand-derived results."""
    d1 = _v2_out(100_000_000, 1_000_000_000, 1_000_000)  # ac_1
    d2 = _v2_out(100_000_000, 500_000_000, 500_000)  # ac_2
    head = (Swap("ac_1", Single(0, BPS, 5000), 1), Swap("ac_2", Single(0, REM), 2))
    tokens = ("TKA", "TKC", "TKC", "TKB", "TKB")
    s1_first = ColleaguePlan(
        tokens, (*head, Swap("cb_1", Single(1, ALL), 3), Swap("cb_1", Single(2, ALL), 4))
    )
    s2_first = ColleaguePlan(
        tokens, (*head, Swap("cb_1", Single(2, ALL), 3), Swap("cb_1", Single(1, ALL), 4))
    )
    x1 = _v2_out(d1, 1_000_000, 1_000_000_000)
    x2 = _v2_out(d2, 1_000_000 + d1, 1_000_000_000 - x1)
    y2 = _v2_out(d2, 1_000_000, 1_000_000_000)
    y1 = _v2_out(d1, 1_000_000 + d2, 1_000_000_000 - y2)
    a, b = adapt(CPMM, CASE, s1_first, OBJ), adapt(CPMM, CASE, s2_first, OBJ)
    assert_ok(a)
    assert_ok(b)
    assert [r.amount_out for r in a.source_trace[2:]] == [x1, x2] == [82_896_118, 64_765_518]
    assert [r.amount_out for r in b.source_trace[2:]] == [y2, y1] == [76_532_061, 71_129_470]
    assert [s.inputs[0].fund_id for s in assert_ok(b).steps[2:]] == ["s2", "s1"]
    assert a.evaluation is not None and b.evaluation is not None
    assert (a.evaluation.gross_output, b.evaluation.gross_output) == (147_661_636, 147_661_531)


def test_fresh_snapshot_and_charged_quotes() -> None:
    pools_before = dict(CPMM.pools)
    plan_before = REPEATED
    with metered_quotes(None) as meter:
        first = adapt(CPMM, CASE, REPEATED, OBJ)
    assert meter.counted == 8  # 4 resolution quotes + 4 independent replay quotes, no cache
    assert dict(CPMM.pools) == pools_before and REPEATED == plan_before
    second = adapt(CPMM, CASE, REPEATED, OBJ)
    assert (second.plan, second.source_trace, second.terminals) == (
        first.plan,
        first.source_trace,
        first.terminals,
    )
    with pytest.raises(QuoteLimitExceeded), metered_quotes(5):
        adapt(CPMM, CASE, REPEATED, OBJ)


# --- policy: candidates unsupported, policy_off baseline only, no silent success ------


def with_candidates(plan: ColleaguePlan, hops: dict[int, tuple[str, ...]]) -> ColleaguePlan:
    program = tuple(replace(ins, candidates=hops.get(n, ())) for n, ins in enumerate(plan.program))
    return replace(plan, program=program)


def test_candidate_sets_are_unsupported_unless_policy_off() -> None:
    fx = PLANS["F1"]  # source: X + [Fermi] and Z + [Kipseli]
    jit = with_candidates(source_plan(fx), {0: ("Fermi",), 4: ("Kipseli",)})
    unsupported = run_fixture(fx, plan=jit, quote=no_quote)
    assert unsupported.status is SolveStatus.UNSUPPORTED and unsupported.plan is None
    assert "D-P1..D-P4 undecided" in (unsupported.error or "")

    calls: list[str] = []
    off = run_fixture(
        fx, plan=jit, policy_off=True, quote=scripted(table(fx["scripted_outputs"]), calls)
    )
    assert_ok(off)
    plain = run_fixture(fx)
    assert off.plan == plain.plan and off.source_trace == plain.source_trace
    assert off.evaluation is not None and plain.evaluation is not None
    assert off.evaluation.next_states == plain.evaluation.next_states  # no probe changed state
    assert set(calls) == {"X", "Y", "C", "V", "Z"}  # only baseline pools were ever quoted
    labels = [d for d in off.diagnostics if "policy_off adaptation" in d]
    assert [d.split(":")[0] for d in labels] == ["#0", "#4"]
    assert "'Fermi'" in labels[0] and "'Kipseli'" in labels[1]
    assert plain.diagnostics == ()


def test_failed_or_partial_baseline_quotes_never_succeed() -> None:
    # Real insufficient liquidity on the baseline; its candidate is never tried as a fallback.
    calls: list[str] = []

    def recording(state: PoolState, token_in: str, amount: int) -> SwapResult[PoolState]:
        calls.append(state.pool_id)
        return quote_exact_in(state, token_in, amount)

    dry = ColleaguePlan(("TKA", "TKB"), (Swap("ab_dry", Single(0, ALL), 1, ("ab_1",)),))
    for policy_off in (True, False):
        result = adapt(CPMM, CASE, dry, OBJ, policy_off=policy_off, quote=recording)
        assert result.plan is None and result.evaluation is None
        if policy_off:
            assert result.status is SolveStatus.INVALID_PLAN
            assert result.quote_status is QuoteStatus.INSUFFICIENT_LIQUIDITY
            assert "zero reserve" in (result.error or "")
        else:
            assert result.status is SolveStatus.UNSUPPORTED
    assert calls == ["ab_dry"]

    fx = PLANS["F2"]
    outputs = table(fx["scripted_outputs"])

    def incomplete(state: PoolState, token_in: str, amount: int) -> SwapResult[PoolState]:
        if state.pool_id == "BC":
            return SwapResult(QuoteStatus.INCOMPLETE_SNAPSHOT, 0, 0, None, "tick word missing")
        return SwapResult(QuoteStatus.OK, amount, outputs[(state.pool_id, amount)], state)

    def partial(state: PoolState, token_in: str, amount: int) -> SwapResult[PoolState]:
        consumed = amount - 1 if state.pool_id == "AB2" else amount
        return SwapResult(QuoteStatus.OK, consumed, outputs[(state.pool_id, amount)], state)

    bad = run_fixture(fx, quote=incomplete)
    assert (bad.status, bad.quote_status, bad.plan) == (
        SolveStatus.INVALID_PLAN,
        QuoteStatus.INCOMPLETE_SNAPSHOT,
        None,
    )
    assert "tick word missing" in (bad.error or "") and len(bad.source_trace) == 2
    short = run_fixture(fx, quote=partial)
    assert (short.status, short.quote_status) == (SolveStatus.INVALID_PLAN, QuoteStatus.OK)
    assert "consumed 400 of 401" in (short.error or "")


def test_no_threshold_or_tie_policy_is_exposed() -> None:
    assert list(inspect.signature(adapt).parameters) == [
        "bundle",
        "case",
        "plan",
        "objective",
        "policy_off",
        "quote",
    ]


def test_independent_replay_rejects_a_divergent_recovery() -> None:
    """The replay is compared, not trusted: a quote seam that answers the replay
    differently from the resolution (impure, which `evaluate` forbids) is caught."""
    fx = PLANS["F2"]
    outputs = table(fx["scripted_outputs"])
    seen: set[tuple[str, int]] = set()

    def drifting(state: PoolState, token_in: str, amount: int) -> SwapResult[PoolState]:
        key = (state.pool_id, amount)
        out = outputs.get(key, 0) + (key in seen and state.pool_id == "BC")
        seen.add(key)
        return SwapResult(QuoteStatus.OK, amount, out, state)

    result = run_fixture(fx, quote=drifting)
    assert result.status is SolveStatus.INVALID_PLAN and result.plan is None
    assert "#2: replay ('BC', 985, 971, False) differs from source row" in (result.error or "")

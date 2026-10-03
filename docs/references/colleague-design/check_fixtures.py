"""Check the WHI-1536 colleague-design fixtures (docs/references/colleague-routing-contract.md).

Run from the repository root:

    uv run python docs/references/colleague-design/check_fixtures.py [--attachment PATH]

The script does four things. None of them computes a fixture's expected value:

1. It recomputes every integer rule with plain floor arithmetic (contract §5.1).
2. It interprets each M3 program with a small slot ledger written from the attachment's
   rules (baseAmount/consumed/ready/closed, BPS_OF_PARENT on a frozen base, REMAINDER,
   MERGE_INTO phases, zero input without a quote). It then compares the result with the
   hand-derived `expected_trace`.
3. It recovers a `RoutePlan` with the canonical rules of contract §5.3 and compares it
   with the hand-written `expected_route_plan`.
4. It replays that plan, and every negative variant, through the unchanged
   `routing.evaluator.evaluate`. Pool outputs come from the fixture's scripted table
   through the evaluator's pure quote seam, so no expectation depends on pool math.

With `--attachment` it also checks the byte length and SHA-256 of a locally downloaded
`plan-and-compilation.html`. The attachment is never stored in the repository.

Research artifact only: this is not the WHI-1537 adapter and is not imported by product code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from benchmark.objective import gross_only  # noqa: E402
from pools.result import QuoteStatus, SwapResult  # noqa: E402
from routing.evaluator import Evaluation, evaluate  # noqa: E402
from routing.plan import (  # noqa: E402
    ALL_REMAINING,
    REQUEST_FUND_ID,
    FundInput,
    RoutePlan,
    SwapStep,
)
from snapshot.models import (  # noqa: E402
    BlockRef,
    Case,
    ConstantProductPoolState,
    PoolState,
    SnapshotBundle,
)

HERE = Path(__file__).resolve().parent
FAILURES: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        FAILURES.append(what)


# --- 1. integer rules ---------------------------------------------------------------


def bps_of(base: int, bps: int) -> int:
    return base * bps // 10_000


def check_integer_rules(rules: list[dict[str, Any]]) -> None:
    for r in rules:
        rid = r["id"]
        if "branches" in r:
            base = int(r["base"])
            got: list[int] = []
            for mode, bps in r["branches"]:
                got.append(bps_of(base, bps) if mode == "BPS" else base - sum(got))
            check(got == [int(x) for x in r["expected"]], f"{rid}: {got}")
            flat = [bps_of(base, bps) for _, bps in r["flattened_invalid"]["branches"]]
            left = base - sum(flat)
            unallocated = int(r["flattened_invalid"]["unallocated"])
            check(left == unallocated, f"{rid}: flattened leaves {left}")
        elif "layers" in r:
            v = int(r["base"])
            for bps in r["layers"]:
                v = bps_of(v, bps)
            flat_v = bps_of(int(r["base"]), r["flattened_bps"])
            check(v == int(r["expected_nested"]), f"{rid}: nested = {v}")
            check(
                flat_v == int(r["flattened_invalid_value"]) != v,
                f"{rid}: flattened {flat_v} != {v}",
            )
        elif "groups" in r:
            for g in r["groups"]:
                first = bps_of(int(g["base"]), g["bps"])
                got = [first, int(g["base"]) - first]
                check(got == [int(x) for x in g["expected"]], f"{rid}: {g['base']} -> {got}")
        else:
            base = sum(int(m) for m in r["members"])
            first = bps_of(base, r["bps"])
            e = r["expected"]
            check(
                (base, first, base - first) == (int(e["base"]), int(e["first"]), int(e["rest"])),
                f"{rid}: {base} -> {first}/{base - first}",
            )


# --- 2. independent M3 slot ledger (attachment §6.2, §6.3, §9.2, §10.4) ------------


@dataclass
class Slot:
    base: int | None = None  # frozen when produced; None = not ready
    consumed: int = 0
    closed: bool = False
    bps_used: int = 0  # BPS share group on this slot
    group_open: bool = False


class M3Error(Exception):
    pass


Scripted = dict[tuple[str, int], int]


def run_m3(
    program: list[dict[str, Any]], n_slots: int, amount_in: int, outputs: Scripted
) -> list[dict[str, Any]]:
    slots = [Slot() for _ in range(n_slots)]
    slots[0].base = amount_in
    trace: list[dict[str, Any]] = []

    def ready(s: int) -> Slot:
        slot = slots[s]
        if slot.base is None:
            raise M3Error(f"slot{s} consumed before it is produced")
        if slot.closed:
            raise M3Error(f"slot{s} already closed")
        return slot

    def take_share(s: int, mode: str, bps: int) -> int:
        slot = ready(s)
        assert slot.base is not None
        if mode == "BPS":
            if not 1 <= bps <= 10_000 or slot.bps_used + bps > 10_000:
                raise M3Error(f"slot{s}: bad bps {bps}")
            slot.bps_used += bps
            slot.group_open = True
            amount = bps_of(slot.base, bps)  # always on the frozen base
        elif mode == "REMAINDER":
            amount = slot.base - slot.consumed
            slot.closed = True
        else:  # ALL: exclusive single consumer
            if slot.consumed or slot.group_open:
                raise M3Error(f"slot{s}: ALL on a shared slot")
            amount = slot.base
            slot.closed = True
        slot.consumed += amount
        return amount

    for ins in program:
        n, inp = ins["n"], ins["input"]
        merged_base: int | None = None
        if inp[0] == "SINGLE":
            _, s, mode, bps = inp
            amount = take_share(s, mode, bps)
        else:
            _, merged, mode, bps, members = inp
            if members != sorted(set(members)) or not 2 <= len(members) <= 4:
                raise M3Error(f"#{n}: members must be 2..4, strictly ascending")
            total = 0
            for m in members:  # (1) consume and close members exclusively
                total += take_share(m, "ALL", 0)
            if slots[merged].base is not None:
                raise M3Error(f"#{n}: merged slot{merged} is not fresh")
            slots[merged].base = merged_base = total  # (2) produce and freeze
            if mode not in ("ALL", "BPS"):
                raise M3Error(f"#{n}: MERGE_INTO first branch must be ALL or BPS")
            amount = take_share(merged, mode, bps)  # (3) reserve the first branch
        out = ins["out"]
        if slots[out].base is not None:
            raise M3Error(f"#{n}: output slot{out} is not fresh")
        if amount == 0:  # zero input: no quote, ready zero output
            got = 0
        else:
            got = outputs[(ins["pool"], amount)]  # (4) swap, (5) produce the output
        slots[out].base = got
        row: dict[str, Any] = {"n": n, "amount_in": amount, "amount_out": got}
        if amount == 0:
            row["zero_input"] = True
        if merged_base is not None:
            row["merged_base"] = merged_base
        trace.append(row)
    return trace


def check_display_order(d: dict[str, Any]) -> None:
    nodes = {n: (ins, outs) for n, ins, outs in d["sem_nodes_order"]}

    def first_violation(order: list[str]) -> str | None:
        produced = set(d["initial_flows"])
        for n in order:
            ins, outs = nodes[n]
            missing = [f for f in ins if f not in produced]
            if missing:
                return f"{n} consumes {missing} before any producer"
            produced.update(outs)
        return None

    sem = first_violation([n for n, _, _ in d["sem_nodes_order"]])
    disp = first_violation(d["display_order"])
    check(sem is None, f"{d['id']}: SEM_NODES order is producer-before-consumer")
    check(disp is not None and disp.startswith("N6"), f"{d['id']}: display order fails ({disp})")


# --- 3. canonical recovery (contract §5.3) ------------------------------------------


def fund(slot: int) -> str:
    return REQUEST_FUND_ID if slot == 0 else f"s{slot}"


def recover(fx: dict[str, Any], trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """RoutePlan steps from an M3 program and its resolved amounts. Merged slots have no
    fund: their members' funds are referenced directly."""
    tokens: list[str] = fx["slot_tokens"]
    left = {0: int(fx["case"]["amount_in"])}  # RoutePlan-ledger balance per real slot
    members_of: dict[int, list[int]] = {}
    steps: list[dict[str, Any]] = []
    for ins, row in zip(fx["program"], trace, strict=True):
        inp, amount = ins["input"], row["amount_in"]
        refs: list[list[str]] = []
        src, mode = inp[1], inp[2]
        if inp[0] == "MERGE_INTO":
            members_of[src] = list(inp[4])
        if src in members_of:  # draw from a merged slot through its members
            members = members_of[src]
            if mode in ("ALL", "REMAINDER"):
                live = [m for m in members if left[m] > 0] if mode == "REMAINDER" else members
                refs = [[fund(m), ALL_REMAINING] for m in live]
                for m in live:
                    left[m] = 0
            else:
                need = amount
                for m in members:
                    take = min(need, left[m])
                    if take > 0:
                        refs.append([fund(m), str(take)])
                        left[m] -= take
                        need -= take
                if not refs:
                    refs = [[fund(members[0]), "0"]]
        elif mode == "BPS":
            refs = [[fund(src), str(amount)]]
            left[src] -= amount
        else:
            refs = [[fund(src), ALL_REMAINING]]
            left[src] = 0
        out = ins["out"]
        left[out] = row["amount_out"]
        a, b = fx["pools"][ins["pool"]]
        token_in = a if tokens[out] == b else b
        steps.append(
            {
                "pool": ins["pool"],
                "token_in": token_in,
                "token_out": tokens[out],
                "inputs": refs,
                "out": fund(out),
            }
        )
    return steps


# --- 4. evaluator replay with a scripted pure quote seam ----------------------------


def bundle_for(fx: dict[str, Any]) -> SnapshotBundle:
    pools: dict[str, PoolState] = {
        pid: ConstantProductPoolState(pid, t0, t1, 10**30, 10**30, 0)  # reserves unused
        for pid, (t0, t1) in fx["pools"].items()
    }
    return SnapshotBundle(
        bundle_id=fx["id"],
        kind="synthetic",
        schema_version=1,
        block=BlockRef(chain_id=5000, number=1, hash="0x" + "00" * 32, timestamp=0),
        pools=pools,
        cases=(),
        bundle_hash="-",
        source_path="<fixture>",
    )


def scripted(rows: list[dict[str, Any]]) -> Scripted:
    return {(r["pool"], int(r["amount_in"])): int(r["amount_out"]) for r in rows}


def replay(fx: dict[str, Any], plan: list[dict[str, Any]], outputs: Scripted) -> Evaluation:
    def quote(state: PoolState, token_in: str, amount: int) -> SwapResult[PoolState]:
        out = outputs[(state.pool_id, amount)]  # KeyError = the plan asked for an unscripted amount
        return SwapResult(QuoteStatus.OK, amount, out, state)

    steps = tuple(
        SwapStep(
            pool_id=s["pool"],
            token_in=s["token_in"],
            token_out=s["token_out"],
            inputs=tuple(
                FundInput(f, ALL_REMAINING if a == ALL_REMAINING else int(a))
                for f, a in s["inputs"]
            ),
            output_fund_id=s["out"],
        )
        for s in plan
    )
    c = fx["case"]
    case = Case("fixture", c["token_in"], c["token_out"], int(c["amount_in"]))
    return evaluate(bundle_for(fx), case, RoutePlan(steps), gross_only(), quote=quote)


def check_evaluation(label: str, ev: Evaluation, want: dict[str, Any]) -> None:
    check(ev.status.value == want["status"], f"{label}: status {ev.status.value}")
    if "gross_output" in want:
        check(ev.gross_output == int(want["gross_output"]), f"{label}: gross {ev.gross_output}")
    for key in ("pool_calls", "zero_input_steps"):
        if key in want:
            check(ev.route_features.get(key) == want[key], f"{label}: {key}")
    if "error_contains" in want:
        check(want["error_contains"] in (ev.error or ""), f"{label}: error {ev.error!r}")


def normal(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ("n", "amount_in", "amount_out", "zero_input", "merged_base")
    return [
        {
            k: (str(r[k]) if k in ("amount_in", "amount_out", "merged_base") else r[k])
            for k in keys
            if k in r
        }
        for r in trace
    ]


def check_plan(fx: dict[str, Any]) -> None:
    fid = fx["id"]
    outputs = scripted(fx["scripted_outputs"])
    if "program" in fx:
        trace = run_m3(fx["program"], len(fx["slot_tokens"]), int(fx["case"]["amount_in"]), outputs)
        check(normal(trace) == normal(fx["expected_trace"]), f"{fid}: M3 ledger = expected_trace")
        plan = recover(fx, trace)
        check(plan == fx["expected_route_plan"], f"{fid}: recovery = expected_route_plan")
    else:  # F5: nested same-token split, allocations by the layered rule
        a = int(fx["case"]["amount_in"])
        x = bps_of(a, 5000)
        x1 = bps_of(x, 8000)
        got = {"X": x, "Y": a - x, "X1": x1, "X2": x - x1}
        want = {k: int(v) for k, v in fx["expected_allocations"].items()}
        check(got == want, f"{fid}: layered allocations {got}")
        flat = {k: int(v) for k, v in fx["flattened_invalid"].items() if k != "derivation"}
        check(flat["X1"] == bps_of(a, 4000) != got["X1"], f"{fid}: flattened X1 differs")
        amounts = [s["inputs"][0][1] for s in fx["expected_route_plan"]]
        check(amounts == [str(x1), str(x - x1), ALL_REMAINING], f"{fid}: plan amounts {amounts}")
        plan = fx["expected_route_plan"]
    ev = replay(fx, plan, outputs)
    check_evaluation(f"{fid}: evaluator", ev, fx["expected_evaluation"])
    if "program" in fx:
        steps = [(t.amount_in, t.amount_out) for t in ev.trace]
        m3 = [(r["amount_in"], r["amount_out"]) for r in trace]
        if ev.status.value == "ok":
            check(steps == m3, f"{fid}: evaluator trace = M3 ledger trace")
    for neg in fx.get("negative_variants", []):
        variant = [dict(s) for s in plan]
        variant[neg["replace_step"]]["inputs"] = neg["inputs"]
        nev = replay(fx, variant, {**outputs, **scripted(neg["scripted_outputs"])})
        check_evaluation(
            f"{neg['id']}: evaluator",
            nev,
            {"status": neg["expected_status"], "error_contains": neg["expected_error_contains"]},
        )
    if "elided_resolution_candidate" in fx:
        alt = fx["elided_resolution_candidate"]
        check_evaluation(
            f"{fid}: elided candidate",
            replay(fx, alt["route_plan"], outputs),
            alt["expected_evaluation"],
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--attachment", type=Path, help="local plan-and-compilation.html to verify")
    args = ap.parse_args()
    fixtures = json.loads((HERE / "fixtures.json").read_text(encoding="utf-8"))
    prov = json.loads((HERE / "provenance.json").read_text(encoding="utf-8"))
    check(fixtures["source_sha256"] == prov["attachment"]["sha256"], "fixture/provenance sha agree")
    if args.attachment is not None:
        data = args.attachment.read_bytes()
        check(len(data) == prov["attachment"]["bytes"], f"attachment bytes {len(data)}")
        digest = hashlib.sha256(data).hexdigest()
        check(digest == prov["attachment"]["sha256"], f"attachment sha256 {digest}")
    check_integer_rules(fixtures["integer_rules"])
    for fx in fixtures["plans"]:
        check_plan(fx)
    check_display_order(fixtures["display_order"])
    print(f"\n{'FAILED ' + str(len(FAILURES)) if FAILURES else 'all checks passed'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())

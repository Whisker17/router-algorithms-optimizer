"""`report.quote` (WHI-1498): the single-request presentation renders every algorithm's
final plan from recorded evaluator traces -- splits, integer remainders, merges and
shared-pool reuse in execution order -- with raw amounts that reconcile, and never prints
distribution statistics for one execution.

Plans are hand-built on the hand-derived CPMM graph fixture and replayed by the real
`routing.evaluator.evaluate`; the renderer only sees the resulting records."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from benchmark.objective import gross_only
from benchmark.results import CaseRecord, RunManifest
from report.quote import QuoteView, render_compact, render_details
from routing.algorithms.base import SolveStatus
from routing.evaluator import evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from snapshot.bundle import load_bundle
from snapshot.models import Case
from snapshot.request import REQUEST_SCHEMA

REPO = Path(__file__).resolve().parents[2]
CPMM = load_bundle(REPO / "tests" / "fixtures" / "routing" / "cpmm_graph")
R = REQUEST_FUND_ID
ALL: Literal["ALL_REMAINING"] = ALL_REMAINING
STATISTICS = re.compile(r"median|mean|p50|p95|percentile|samples|stdev", re.IGNORECASE)
AMOUNT = 100_000_001  # odd on purpose: a split leaves an integer remainder


def step(pool: str, tin: str, tout: str, out: str, *inputs: tuple[str, Any]) -> SwapStep:
    return SwapStep(pool, tin, tout, tuple(FundInput(f, a) for f, a in inputs), out)


CASE = Case(case_id="quote-test", token_in="TKA", token_out="TKB", amount_in=AMOUNT)
PLANS = {
    "direct": (step("ab_1", "TKA", "TKB", "OUT", (R, ALL)),),
    # disjoint split: a third over A->C->B, the rest (incl. the remainder) direct
    "path_split": (
        step("ac_1", "TKA", "TKC", "c1", (R, AMOUNT // 3)),
        step("cb_1", "TKC", "TKB", "b1", ("c1", ALL)),
        step("ab_1", "TKA", "TKB", "b2", (R, ALL)),
    ),
    # split over two A->C pools, merge both C funds into one cb_1 swap
    "incremental_graph": (
        step("ac_1", "TKA", "TKC", "c1", (R, 60_000_000)),
        step("ac_2", "TKA", "TKC", "c2", (R, ALL)),
        step("cb_1", "TKC", "TKB", "out", ("c1", ALL), ("c2", ALL)),
    ),
    # the same physical pool twice, unmerged: the second use sees the first's state
    "single_path": (
        step("ac_1", "TKA", "TKC", "c1", (R, 50_000_000)),
        step("cb_1", "TKC", "TKB", "b1", ("c1", ALL)),
        step("ac_2", "TKA", "TKC", "c2", (R, ALL)),
        step("cb_1", "TKC", "TKB", "b2", ("c2", ALL)),
    ),
}


def ok_record(name: str, steps: tuple[SwapStep, ...], objective: Any = None) -> dict[str, Any]:
    evaluation = evaluate(CPMM, CASE, RoutePlan(steps), objective or gross_only())
    assert evaluation.status.value == "ok", evaluation.error
    return CaseRecord(
        case_id=CASE.case_id,
        algorithm=name,
        status=SolveStatus.OK,
        evaluation=evaluation,
        score=evaluation.gross_output,
        candidates_considered=3,
        error=None,
        solver_reported_status=SolveStatus.OK,
        solver_reported_gross_output=evaluation.gross_output,
        solver_reported_score=evaluation.gross_output,
        quotes_attempted=5,
        quotes_counted=5,
        measurement={
            "solve_seconds": [0.0125],
            "evaluation_seconds": 0.0003,
            "prepare_event": 0,
            "attempts_completed": 1,
            "candidates_reported": 1,
        },
    ).to_dict()


def failed_record(name: str, status: SolveStatus, **extra: Any) -> dict[str, Any]:
    return CaseRecord(
        case_id=CASE.case_id,
        algorithm=name,
        status=status,
        evaluation=None,
        score=None,
        candidates_considered=0,
        error=extra.pop("error", "deliberate"),
        solver_reported_status=None,
        solver_reported_gross_output=None,
        solver_reported_score=None,
        measurement={"solve_seconds": [], "prepare_event": 0, "attempts_completed": 0}
        | extra.pop("measurement", {}),
        **extra,
    ).to_dict()


def view(records: list[dict[str, Any]], *, mode: str = "gross_only") -> QuoteView:
    tokens = {
        t.lower(): {"address": t, "symbol": t.removeprefix("TK"), "decimals": 6}
        for t in ("TKA", "TKB", "TKC", "TKD")
    }
    manifest = RunManifest.from_dict(
        {
            "run_id": "run-1",
            "schema_version": 2,
            "state": "complete",
            "bundle_id": "cpmm-exploratory-test",
            "bundle_hash": "b" * 64,
            "profile_path": "profile.yaml",
            "profile_sha256": "c" * 64,
            "resolved_profile": {"objective": {"mode": mode}, "budget": {}},
            "objective_label": mode,
            "algorithms": [r["algorithm"] for r in records],
            "created_at": "t",
            "replay_command": "cmd",
            "scheduled_count": len(records),
            "case_count": len(records),
            "measurement": {"warmup": 0, "repeats": 1, "memory_pass": False},
            "prepare_events": [{"prepare_seconds": 0.001, "startup_seconds": 0.2}],
            "environment": {"git_revision": "abc", "python_version": "3.13"},
        },
        "run-dir",
    )
    provenance = {
        "schema": REQUEST_SCHEMA,
        "label": "exploratory single request; not a held-out corpus result",
        "block": {"chain_id": 0, "number": 0, "hash": "0x0", "timestamp": 0},
        "derived_from": {"bundle_id": CPMM.bundle_id, "bundle_hash": "p" * 64},
        "request": {
            "case_id": CASE.case_id,
            "token_in": tokens["tka"],
            "token_out": tokens["tkb"],
            "amount_in_raw": str(AMOUNT),
            "amount_in": "100.000001",
        },
        "envelope": {"token_in_max_raw": None, "within": None},
        "pool_scope": {"pools": len(CPMM.pools), "sources": {"generic": len(CPMM.pools)}},
        "tokens": tokens,
    }
    sources = {pid: "generic_cpmm" for pid in CPMM.pools}
    return QuoteView(manifest, records, provenance, sources, None)


def section(text: str, name: str) -> str:
    return text.split(f"[{name}]", 1)[1].split("\n[", 1)[0]


def allocations(block: str, fund: str) -> list[tuple[int, int]]:
    """(step, raw amount) allocations listed under `fund` in a details section."""
    lines = block.split(f"    {fund}:", 1)[1].splitlines()[1:]
    out = []
    for line in lines:
        m = re.match(r"\s+-> step (\d+): (\d+) raw", line)
        if not m:
            break
        out.append((int(m.group(1)), int(m.group(2))))
    return out


def test_every_plan_is_explained_and_reconciles_in_raw_units() -> None:
    records = [ok_record(name, steps) for name, steps in PLANS.items()]
    text = render_details(view(records))
    assert text.count("reconciliation: allocations and terminal totals reconcile exactly") == 4
    for record in records:
        block = section(text, record["algorithm"])
        trace = record["evaluation"]["trace"]
        # execution order is the evaluator's, verbatim
        shown = [int(n) for n in re.findall(r"^\s+(\d+)\. generic_cpmm pool", block, re.M)]
        assert shown == [t["step"] for t in trace]
        request = allocations(block, "REQUEST")
        assert sum(a for _, a in request) == AMOUNT  # the whole input, exactly
        gross = int(record["evaluation"]["gross_output"])
        assert f"evaluated gross output {gross} raw" in block
        assert f"= {gross} raw; evaluated" in block  # terminal outputs sum to gross


def test_integer_remainder_goes_to_the_final_allocation_with_explicit_denominators() -> None:
    block = section(
        render_details(view([ok_record("path_split", PLANS["path_split"])])), "path_split"
    )
    first, last = allocations(block, "REQUEST")
    assert first == (0, AMOUNT // 3) and last == (2, AMOUNT - AMOUNT // 3)
    assert f"{AMOUNT // 3} raw = ≈33.333333% of {AMOUNT} raw" in block
    assert "drains the remaining balance (final allocation, incl. any integer remainder)" in block
    assert "residuals: none" in block


def test_merge_and_shared_pool_reuse_are_shown_in_execution_order() -> None:
    text = render_details(
        view([ok_record(n, PLANS[n]) for n in ("incremental_graph", "single_path")])
    )
    merged = section(text, "incremental_graph")
    assert re.search(r"in : c1 \d+ raw \+ c2 \d+ raw \(merge\)", merged)
    reused = section(text, "single_path")
    assert "shared pool cb_1: used by steps 1, 3 in that order" in reused
    assert "(merge)" not in reused  # reuse is not flattened into a merge
    # both uses of the one physical pool are listed, not deduplicated into one step
    trace = ok_record("single_path", PLANS["single_path"])["evaluation"]["trace"]
    assert trace[1]["pool_id"] == trace[3]["pool_id"] == "cb_1"
    assert reused.count("generic_cpmm pool cb_1") == 2


def test_failures_show_status_and_labelled_evidence_never_a_fabricated_route() -> None:
    candidate_eval = evaluate(CPMM, CASE, RoutePlan(PLANS["direct"]), gross_only())
    records = [
        failed_record("direct", SolveStatus.NO_ROUTE, error="no route found"),
        failed_record(
            "path_split",
            SolveStatus.TIMEOUT,
            limit_hit="time",
            error="repeat 1/1: time limit",
            last_valid_candidate={
                "label": "last valid candidate before the solve was cut off "
                "(not a completed solve)",
                "evaluation": candidate_eval.to_dict(),
                "score": str(candidate_eval.gross_output),
            },
            measurement={"elapsed_seconds": 0.5},
        ),
        failed_record("single_path", SolveStatus.ALGORITHM_ERROR, error="boom"),
        ok_record("incremental_graph", PLANS["incremental_graph"]),
    ]
    v = view(records)
    compact, details = render_compact(v), render_details(v)
    rows = {line.split()[0]: line for line in compact.splitlines() if line.split()[:1]}
    assert "no_route" in rows["direct"] and "timeout" in rows["path_split"]
    assert "algorithm_error" in rows["single_path"]
    # a failed direct baseline leaves every relative gain N/A, with the reason
    assert "vs direct: N/A -- the direct baseline has no valid output (no_route)" in compact
    assert "bps" not in rows["incremental_graph"]
    timeout = section(details, "path_split")
    assert "limit hit: time" in timeout and "PARTIAL DIAGNOSTIC -- last valid candidate" in timeout
    assert "not completed (cut off after 0.500000 s)" in timeout
    assert "gross output" not in timeout.split("PARTIAL DIAGNOSTIC")[0]
    crashed = section(details, "single_path")
    assert "no completed route" in crashed and "execution order" not in crashed


def test_relative_gain_and_net_column_follow_the_recorded_objective() -> None:
    records = [ok_record(n, PLANS[n]) for n in ("direct", "incremental_graph")]
    gross_text = render_compact(view(records))
    assert "net raw" not in gross_text
    direct = int(records[0]["evaluation"]["gross_output"])
    other = int(records[1]["evaluation"]["gross_output"])
    from decimal import Decimal

    expected = (Decimal((other - direct) * 10_000) / Decimal(direct)).quantize(Decimal("0.01"))
    assert f"{expected:+} bps" in gross_text
    from benchmark.objective import synthetic_fixed_cost

    fixed = [ok_record(n, PLANS[n], synthetic_fixed_cost(1_000)) for n in ("direct",)]
    net_text = render_compact(view(fixed, mode="synthetic_fixed_cost"))
    assert (
        "net raw" in net_text
        and str(int(fixed[0]["evaluation"]["gross_output"]) - 1_000) in net_text
    )


def test_single_execution_presentation_has_no_distribution_statistics() -> None:
    records = [ok_record(n, s) for n, s in PLANS.items()]
    v = view(records)
    for text in (render_compact(v), render_details(v)):
        assert not STATISTICS.search(text), STATISTICS.search(text)
    assert "latency is one observation of this execution" in render_compact(v)

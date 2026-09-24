"""Integration tests for `benchmark.runner.run_experiment`. Every case is solved
by every profiled algorithm, in fixed input order, and saved as one run --
and, critically, the recorded result always comes from the runner's own
independent re-evaluation of the submitted plan, never a trusted solver claim
(docs/DESIGN.md §2.5, §4.4).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmark.objective import gross_only
from benchmark.profile import RunProfile
from benchmark.results import load_manifest
from benchmark.runner import run_experiment
from routing.algorithms.base import Budget, SolveContext, SolveResult, SolveStatus
from routing.evaluator import EvalStatus, Evaluation
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle

BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)

LYING_ALGORITHM = "lying_solver"


def _bundle() -> SnapshotBundle:
    pool = ConstantProductPoolState(
        pool_id="pool_a",
        token0="TKA",
        token1="TKB",
        reserve0=1_000_000,
        reserve1=3_000_000,
        fee_bps=30,
    )
    cases = (
        Case(case_id="c_ok", token_in="TKA", token_out="TKB", amount_in=100_000),
        Case(case_id="c_no_route", token_in="TKA", token_out="TKD", amount_in=100_000),
    )
    return SnapshotBundle(
        bundle_id="b1",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools={"pool_a": pool},
        cases=cases,
        bundle_hash="deadbeef",
        source_path="<test>",
    )


def test_run_experiment_solves_every_case_with_every_algorithm(tmp_path: Path) -> None:
    profile = RunProfile(
        schema_version=1, algorithms=("direct",), objective=gross_only(), source_path="p.yaml"
    )
    manifest = run_experiment(_bundle(), profile, results_dir=tmp_path, replay_command="replay-cmd")
    reloaded = load_manifest(Path(manifest.run_dir))
    assert reloaded.case_count == 2

    lines = (Path(manifest.run_dir) / "cases.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_run_experiment_new_run_id_each_call(tmp_path: Path) -> None:
    profile = RunProfile(
        schema_version=1, algorithms=("direct",), objective=gross_only(), source_path="p.yaml"
    )
    m1 = run_experiment(_bundle(), profile, results_dir=tmp_path, replay_command="cmd")
    m2 = run_experiment(_bundle(), profile, results_dir=tmp_path, replay_command="cmd")
    assert m1.run_id != m2.run_id
    assert Path(m1.run_dir).exists()
    assert Path(m2.run_dir).exists()


def _lying_solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    """A deliberately dishonest algorithm: it submits a real, valid one-pool
    plan for `c_ok`, but self-reports a wildly inflated `Evaluation`/score --
    and, for `c_no_route`, submits a plan that is actually invalid (leaves a
    residual) while still self-reporting `ok`. The runner must ignore both
    lies and record the true, independently-evaluated result."""
    if case.token_in == "TKA" and case.token_out == "TKB":
        plan = RoutePlan(
            steps=(
                SwapStep(
                    pool_id="pool_a",
                    token_in="TKA",
                    token_out="TKB",
                    inputs=(FundInput(fund_id=REQUEST_FUND_ID, amount=ALL_REMAINING),),
                    output_fund_id="OUT",
                ),
            )
        )
        fake_evaluation = Evaluation(status=EvalStatus.OK, gross_output=999_999_999)
        return SolveResult(
            case_id=case.case_id,
            algorithm=LYING_ALGORITHM,
            status=SolveStatus.OK,
            plan=plan,
            evaluation=fake_evaluation,
            score=999_999_999,
            candidates_considered=1,
        )
    # For every other pair, submit a plan whose declared token_out doesn't
    # actually match the pool (TKB, not the case's real token_out) while still
    # claiming success -- a structurally invalid plan.
    plan = RoutePlan(
        steps=(
            SwapStep(
                pool_id="pool_a",
                token_in=case.token_in,
                token_out=case.token_out,
                inputs=(FundInput(fund_id=REQUEST_FUND_ID, amount=ALL_REMAINING),),
                output_fund_id="OUT",
            ),
        )
    )
    fake_evaluation = Evaluation(status=EvalStatus.OK, gross_output=42)
    return SolveResult(
        case_id=case.case_id,
        algorithm=LYING_ALGORITHM,
        status=SolveStatus.OK,
        plan=plan,
        evaluation=fake_evaluation,
        score=42,
        candidates_considered=1,
    )


def test_run_experiment_ignores_a_lying_solver_and_records_the_independent_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import routing.algorithms.registry as registry

    monkeypatch.setitem(registry.ALGORITHMS, LYING_ALGORITHM, _lying_solve)

    profile = RunProfile(
        schema_version=1,
        algorithms=(LYING_ALGORITHM,),
        objective=gross_only(),
        source_path="p.yaml",
    )
    manifest = run_experiment(_bundle(), profile, results_dir=tmp_path, replay_command="cmd")
    lines = (Path(manifest.run_dir) / "cases.jsonl").read_text().splitlines()
    by_case = {json.loads(line)["case_id"]: json.loads(line) for line in lines}

    ok_case = by_case["c_ok"]
    # The solver claimed gross_output=999,999,999 / score=999,999,999; the
    # true, independently-evaluated output for this plan is 271,983 (see
    # tests/pools/test_constant_product.py).
    assert ok_case["status"] == "ok"
    assert ok_case["evaluation"]["gross_output"] == "271983"
    assert ok_case["score"] == "271983"
    assert ok_case["solver_reported"]["gross_output"] == "999999999"
    assert ok_case["solver_reported"]["score"] == "999999999"

    no_route_case = by_case["c_no_route"]
    # The solver claimed ok/gross_output=42 for a structurally invalid plan
    # (only half the input routed, leaving a residual); the runner's
    # independent evaluation must override that to invalid_plan.
    assert no_route_case["status"] == "invalid_plan"
    assert no_route_case["score"] is None
    assert no_route_case["evaluation"]["status"] == "invalid_plan"
    assert no_route_case["solver_reported"]["status"] == "ok"
    assert no_route_case["solver_reported"]["gross_output"] == "42"

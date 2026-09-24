"""Unit tests for `routing.evaluator.evaluate`: fund-ledger conservation, the
full-fill residual policy, and next-state threading (docs/DESIGN.md §2.5).
"""

from __future__ import annotations

from routing.evaluator import EvalStatus, evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle

BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)


def _bundle(pools: dict[str, ConstantProductPoolState]) -> SnapshotBundle:
    return SnapshotBundle(
        bundle_id="b",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools=pools,
        cases=(),
        bundle_hash="deadbeef",
        source_path="<test>",
    )


POOL = ConstantProductPoolState(
    pool_id="pool_a", token0="TKA", token1="TKB", reserve0=1000, reserve1=2000, fee_bps=30
)


def test_evaluate_single_step_ok() -> None:
    bundle = _bundle({"pool_a": POOL})
    case = Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100)
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
    evaluation = evaluate(bundle, case, plan)
    assert evaluation.status is EvalStatus.OK
    assert evaluation.gross_output == 181  # see tests/pools/test_constant_product.py
    assert evaluation.residuals == {}
    assert evaluation.route_features == {"hops": 1}
    assert evaluation.next_states["pool_a"].reserve0 == 1100
    assert evaluation.next_states["pool_a"].reserve1 == 2000 - 181
    assert len(evaluation.trace) == 1
    assert evaluation.trace[0].amount_out == 181


def test_evaluate_explicit_partial_amount_leaves_residual() -> None:
    bundle = _bundle({"pool_a": POOL})
    case = Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100)
    plan = RoutePlan(
        steps=(
            SwapStep(
                pool_id="pool_a",
                token_in="TKA",
                token_out="TKB",
                inputs=(FundInput(fund_id=REQUEST_FUND_ID, amount=40),),
                output_fund_id="OUT",
            ),
        )
    )
    evaluation = evaluate(bundle, case, plan)
    # Only 40 of the 100 requested input was routed; the remaining 60 stays in
    # the REQUEST (input-token) fund and is a residual under the v1 full-fill
    # policy (docs/DESIGN.md §2.5).
    assert evaluation.status is EvalStatus.INVALID_PLAN
    assert evaluation.residuals == {REQUEST_FUND_ID: 60}
    assert evaluation.error is not None


def test_evaluate_unknown_pool_is_invalid() -> None:
    bundle = _bundle({"pool_a": POOL})
    case = Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100)
    plan = RoutePlan(
        steps=(
            SwapStep(
                pool_id="does_not_exist",
                token_in="TKA",
                token_out="TKB",
                inputs=(FundInput(fund_id=REQUEST_FUND_ID, amount=ALL_REMAINING),),
                output_fund_id="OUT",
            ),
        )
    )
    evaluation = evaluate(bundle, case, plan)
    assert evaluation.status is EvalStatus.INVALID_PLAN
    assert "unknown pool" in (evaluation.error or "")


def test_evaluate_wrong_token_out_is_invalid() -> None:
    bundle = _bundle({"pool_a": POOL})
    case = Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100)
    plan = RoutePlan(
        steps=(
            SwapStep(
                pool_id="pool_a",
                token_in="TKA",
                token_out="TKZ",  # not this pool's other token
                inputs=(FundInput(fund_id=REQUEST_FUND_ID, amount=ALL_REMAINING),),
                output_fund_id="OUT",
            ),
        )
    )
    evaluation = evaluate(bundle, case, plan)
    assert evaluation.status is EvalStatus.INVALID_PLAN
    assert "token_out" in (evaluation.error or "")


def test_evaluate_double_spend_input_fund_is_invalid() -> None:
    bundle = _bundle(
        {
            "pool_a": POOL,
            "pool_b": ConstantProductPoolState(
                pool_id="pool_b",
                token0="TKA",
                token1="TKB",
                reserve0=1000,
                reserve1=2000,
                fee_bps=30,
            ),
        }
    )
    case = Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100)
    # Both steps try to consume the entire REQUEST fund via ALL_REMAINING; the
    # second step must fail because the fund has already been fully consumed.
    plan = RoutePlan(
        steps=(
            SwapStep(
                pool_id="pool_a",
                token_in="TKA",
                token_out="TKB",
                inputs=(FundInput(fund_id=REQUEST_FUND_ID, amount=ALL_REMAINING),),
                output_fund_id="OUT1",
            ),
            SwapStep(
                pool_id="pool_b",
                token_in="TKA",
                token_out="TKB",
                inputs=(FundInput(fund_id=REQUEST_FUND_ID, amount=ALL_REMAINING),),
                output_fund_id="OUT2",
            ),
        )
    )
    evaluation = evaluate(bundle, case, plan)
    assert evaluation.status is EvalStatus.INVALID_PLAN
    assert "not available" in (evaluation.error or "")


def test_evaluate_duplicate_output_fund_id_is_invalid() -> None:
    bundle = _bundle({"pool_a": POOL})
    case = Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100)
    plan = RoutePlan(
        steps=(
            SwapStep(
                pool_id="pool_a",
                token_in="TKA",
                token_out="TKB",
                inputs=(FundInput(fund_id=REQUEST_FUND_ID, amount=ALL_REMAINING),),
                output_fund_id=REQUEST_FUND_ID,  # REQUEST is already "produced"
            ),
        )
    )
    evaluation = evaluate(bundle, case, plan)
    assert evaluation.status is EvalStatus.INVALID_PLAN
    assert "not distinct" in (evaluation.error or "")


def test_evaluate_insufficient_liquidity_is_invalid() -> None:
    empty_pool = ConstantProductPoolState(
        pool_id="pool_a", token0="TKA", token1="TKB", reserve0=0, reserve1=2000, fee_bps=30
    )
    bundle = _bundle({"pool_a": empty_pool})
    case = Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100)
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
    evaluation = evaluate(bundle, case, plan)
    assert evaluation.status is EvalStatus.INVALID_PLAN
    assert "insufficient_liquidity" in (evaluation.error or "")


def test_evaluate_two_step_chain_threads_pool_state() -> None:
    # TKA -> TKB via pool_a, then TKB -> TKC via pool_b, exercising
    # producer-before-consumer funding order across two steps.
    pool_b = ConstantProductPoolState(
        pool_id="pool_b", token0="TKB", token1="TKC", reserve0=5000, reserve1=5000, fee_bps=30
    )
    bundle = _bundle({"pool_a": POOL, "pool_b": pool_b})
    case = Case(case_id="c1", token_in="TKA", token_out="TKC", amount_in=100)
    plan = RoutePlan(
        steps=(
            SwapStep(
                pool_id="pool_a",
                token_in="TKA",
                token_out="TKB",
                inputs=(FundInput(fund_id=REQUEST_FUND_ID, amount=ALL_REMAINING),),
                output_fund_id="MID",
            ),
            SwapStep(
                pool_id="pool_b",
                token_in="TKB",
                token_out="TKC",
                inputs=(FundInput(fund_id="MID", amount=ALL_REMAINING),),
                output_fund_id="OUT",
            ),
        )
    )
    evaluation = evaluate(bundle, case, plan)
    assert evaluation.status is EvalStatus.OK
    assert evaluation.route_features == {"hops": 2}
    assert evaluation.gross_output > 0
    assert set(evaluation.next_states) == {"pool_a", "pool_b"}


def test_evaluate_explicit_zero_input_step_is_deterministic_and_no_pool_call() -> None:
    # docs/DESIGN.md §2.5: "A zero-input step has a deterministic zero output
    # and no pool call." An explicit amount=0 reference to an already-available
    # fund is valid (distinct from referencing an unproduced fund, which stays
    # rejected) and leaves that fund's balance untouched for later steps.
    bundle = _bundle({"pool_a": POOL})
    case = Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100)
    plan = RoutePlan(
        steps=(
            SwapStep(
                pool_id="pool_a",
                token_in="TKA",
                token_out="TKB",
                inputs=(FundInput(fund_id=REQUEST_FUND_ID, amount=0),),
                output_fund_id="SPLIT_ZERO",
            ),
            SwapStep(
                pool_id="pool_a",
                token_in="TKA",
                token_out="TKB",
                inputs=(FundInput(fund_id=REQUEST_FUND_ID, amount=ALL_REMAINING),),
                output_fund_id="OUT",
            ),
        )
    )
    evaluation = evaluate(bundle, case, plan)
    assert evaluation.status is EvalStatus.OK
    assert evaluation.trace[0].status == "zero_input"
    assert evaluation.trace[0].amount_out == 0
    # The zero-input step made no pool call: pool_a's state only reflects the
    # second (real) step.
    assert evaluation.next_states["pool_a"].reserve0 == 1000 + 100
    assert evaluation.gross_output == 181  # see tests/pools/test_constant_product.py


def test_evaluate_zero_amount_from_unproduced_fund_is_still_rejected() -> None:
    # "unproduced funds are never interpreted as zero" (docs/DESIGN.md §2.5):
    # referencing a fund id that was never produced is invalid even with
    # amount=0 -- it is not silently treated as an available zero balance.
    bundle = _bundle({"pool_a": POOL})
    case = Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100)
    plan = RoutePlan(
        steps=(
            SwapStep(
                pool_id="pool_a",
                token_in="TKA",
                token_out="TKB",
                inputs=(FundInput(fund_id="NEVER_PRODUCED", amount=0),),
                output_fund_id="OUT",
            ),
        )
    )
    evaluation = evaluate(bundle, case, plan)
    assert evaluation.status is EvalStatus.INVALID_PLAN
    assert "not available" in (evaluation.error or "")

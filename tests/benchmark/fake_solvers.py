"""Deliberately misbehaving test algorithms for `benchmark.runner` (WHI-1437).

They must live in an importable module (not a test function body): the runner
ships an `AlgorithmFactory` to a spawned worker by pickling, which references the
functions by module + qualified name. Module-level globals here are the "mutable
solver state" that must never leak from one worker into the next.

Case-id conventions in the shared test bundle: `c_hang`, `c_crash`, `c_raise`
trigger the misbehavior; every other case behaves like `direct`.
"""

from __future__ import annotations

import dataclasses
import os
import time
from typing import Any

from pools.quote import QuoteLimitExceeded
from routing.algorithms import direct
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
)
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from snapshot.models import Case, SnapshotBundle

_POISONED = False
_CALLS = 0


def _like_direct(name: str, case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    return dataclasses.replace(direct.solve(case, context, budget), algorithm=name)


def _poison_check(name: str, case: Case) -> SolveResult | None:
    """A solver whose previous (failed) case corrupted its module state answers
    wrongly -- the runner must never let that happen, by replacing the worker."""
    if _POISONED:
        return SolveResult(
            case_id=case.case_id,
            algorithm=name,
            status=SolveStatus.NO_ROUTE,
            error="POISONED by a previous case's leftover state",
        )
    return None


def _plan(case: Case, pool_id: str = "pool_a") -> RoutePlan:
    return RoutePlan(
        steps=(
            SwapStep(
                pool_id=pool_id,
                token_in=case.token_in,
                token_out=case.token_out,
                inputs=(FundInput(fund_id=REQUEST_FUND_ID, amount=ALL_REMAINING),),
                output_fund_id="OUT",
            ),
        )
    )


def hang_solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    global _POISONED
    if case.case_id == "c_hang":
        _POISONED = True
        context.report_candidate(_plan(case))  # a valid candidate, then never returns
        while True:
            time.sleep(0.05)
    return _poison_check("hang", case) or _like_direct("hang", case, context, budget)


def candidate_spam_solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    """Never returns, but floods the parent with candidate messages."""
    plan = _plan(case)
    while True:
        context.report_candidate(plan)


def crash_solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    global _POISONED
    if case.case_id == "c_crash":
        _POISONED = True
        os._exit(17)
    return _poison_check("crash", case) or _like_direct("crash", case, context, budget)


def raise_solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    global _POISONED
    if case.case_id == "c_raise":
        _POISONED = True
        raise RuntimeError("boom")
    return _poison_check("raise", case) or _like_direct("raise", case, context, budget)


def stateful_solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    """Leaks state across calls in one process: only its first call ever succeeds."""
    global _CALLS
    _CALLS += 1
    if _CALLS > 1:
        return SolveResult(
            case_id=case.case_id,
            algorithm="stateful",
            status=SolveStatus.NO_ROUTE,
            error=f"stale state from {_CALLS - 1} earlier call(s)",
        )
    return _like_direct("stateful", case, context, budget)


def quote_hog_solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    """Quotes forever and even tries to swallow ordinary exceptions."""
    while True:
        try:
            evaluate(context.bundle, case, _plan(case), context.objective)
        except Exception:  # noqa: BLE001 - deliberately broad: must not catch the limit
            pass


def limit_swallower_solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    """Catches the hard quote limit itself and pretends to have finished."""
    try:
        while True:
            evaluate(context.bundle, case, _plan(case), context.objective)
    except QuoteLimitExceeded:
        pass
    return SolveResult(
        case_id=case.case_id,
        algorithm="limit_swallower",
        status=SolveStatus.OK,
        plan=_plan(case),
        candidates_considered=1,
    )


def memory_hog_solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    ballast = bytearray(8 * 1024 * 1024)
    result = _like_direct("memory_hog", case, context, budget)
    del ballast
    return result


def slow_prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> Any:
    while True:
        time.sleep(0.05)


def bad_prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> Any:
    raise ValueError("cannot build index")


PREPARE_SECONDS = 0.2


def indexed_prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> Any:
    time.sleep(PREPARE_SECONDS)
    return {"pool_ids": tuple(sorted(bundle.pools)), "config_name": config.name}


def indexed_solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    assert context.prepared["config_name"] == "indexed"
    return _like_direct("indexed", case, context, budget)


def seed_echo_solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    return SolveResult(
        case_id=case.case_id,
        algorithm="seed_echo",
        status=SolveStatus.NO_ROUTE,
        error=f"seed={context.seed}",
    )


def garbage_solve(case: Case, context: SolveContext, budget: Budget) -> Any:
    return "not a SolveResult"


def wrong_case_solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    return dataclasses.replace(
        _like_direct("wrong_case", case, context, budget), case_id="someone_else"
    )


def lying_solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    """Submits a real plan for TKA->TKB with an inflated self-report, and a
    structurally invalid plan (wrong pool for the pair) that it claims is ok."""
    fake = Evaluation(status=EvalStatus.OK, gross_output=999_999_999)
    if case.token_in == "TKA" and case.token_out == "TKB":
        return SolveResult(
            case_id=case.case_id,
            algorithm="lying_solver",
            status=SolveStatus.OK,
            plan=_plan(case),
            evaluation=fake,
            score=999_999_999,
            candidates_considered=1,
        )
    return SolveResult(
        case_id=case.case_id,
        algorithm="lying_solver",
        status=SolveStatus.OK,
        plan=_plan(case),
        evaluation=Evaluation(status=EvalStatus.OK, gross_output=42),
        score=42,
        candidates_considered=1,
    )


HANG = AlgorithmFactory(name="hang", solve=hang_solve)
CANDIDATE_SPAM = AlgorithmFactory(name="candidate_spam", solve=candidate_spam_solve)
CRASH = AlgorithmFactory(name="crash", solve=crash_solve)
RAISE = AlgorithmFactory(name="raise", solve=raise_solve)
STATEFUL = AlgorithmFactory(name="stateful", solve=stateful_solve)
QUOTE_HOG = AlgorithmFactory(name="quote_hog", solve=quote_hog_solve)
LIMIT_SWALLOWER = AlgorithmFactory(name="limit_swallower", solve=limit_swallower_solve)
MEMORY_HOG = AlgorithmFactory(name="memory_hog", solve=memory_hog_solve)
SLOW_PREPARE = AlgorithmFactory(name="slow_prepare", solve=seed_echo_solve, prepare=slow_prepare)
BAD_PREPARE = AlgorithmFactory(name="bad_prepare", solve=seed_echo_solve, prepare=bad_prepare)
INDEXED = AlgorithmFactory(name="indexed", solve=indexed_solve, prepare=indexed_prepare)
SEED_ECHO = AlgorithmFactory(name="seed_echo", solve=seed_echo_solve)
GARBAGE = AlgorithmFactory(name="garbage", solve=garbage_solve)
WRONG_CASE = AlgorithmFactory(name="wrong_case", solve=wrong_case_solve)
LYING = AlgorithmFactory(name="lying_solver", solve=lying_solve)

ALL = (
    HANG,
    CANDIDATE_SPAM,
    CRASH,
    RAISE,
    STATEFUL,
    QUOTE_HOG,
    LIMIT_SWALLOWER,
    MEMORY_HOG,
    SLOW_PREPARE,
    BAD_PREPARE,
    INDEXED,
    SEED_ECHO,
    GARBAGE,
    WRONG_CASE,
    LYING,
)

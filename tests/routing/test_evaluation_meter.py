"""WHI-1554 component A: the default-off `counted_evaluations()` seam of
`routing.evaluator` (docs/references/research-021/suffix-repair.md §8.1).

Checked here: nothing is counted and nothing changes with no active counter; every
`evaluate` call (ok, invalid, raising) is counted once inside a block; nesting, reset and
exception restore mirror `pools.quote.metered_quotes`; independent threads/tasks count only
their own calls; and the seam's count of a real solve equals independent counting wrappers
on every `routing.algorithms` `evaluate` alias -- the embedded `path_split` fallback with its
`single_path`/`direct_split` replays, the `incremental_graph` incumbent replay and, through
WHI-1553's merged executable specification, the repair replays (`structural_trap`: 12 =
6 + 1 + 1 + 4). The seam itself adds no strategy; the repair runtime is WHI-1554 component B.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
from pathlib import Path
from typing import Any, Literal

import pytest

from benchmark.objective import gross_only
from pools.result import SwapResult
from routing.algorithms import incremental_graph, path_split
from routing.algorithms.base import AlgorithmConfig, Budget, SolveContext, SolveStatus
from routing.evaluator import EvalStatus, counted_evaluations, evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from snapshot.bundle import load_bundle
from snapshot.models import BlockRef, Case, ConstantProductPoolState, PoolState, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)
POOL = ConstantProductPoolState(
    pool_id="pool_a", token0="TKA", token1="TKB", reserve0=1000, reserve1=2000, fee_bps=30
)
BUNDLE = SnapshotBundle(
    bundle_id="b",
    kind="synthetic",
    schema_version=1,
    block=BLOCK,
    pools={"pool_a": POOL},
    cases=(),
    bundle_hash="deadbeef",
    source_path="<test>",
)
CASE = Case(case_id="c1", token_in="TKA", token_out="TKB", amount_in=100)


def _plan(
    amount: int | Literal["ALL_REMAINING"] = ALL_REMAINING, pool_id: str = "pool_a"
) -> RoutePlan:
    return RoutePlan(
        steps=(
            SwapStep(
                pool_id=pool_id,
                token_in="TKA",
                token_out="TKB",
                inputs=(FundInput(fund_id=REQUEST_FUND_ID, amount=amount),),
                output_fund_id="OUT",
            ),
        )
    )


OK_PLAN = _plan()
RESIDUAL_PLAN = _plan(40)  # replay-time invalid: unallocated input
UNKNOWN_POOL_PLAN = _plan(pool_id="nope")  # static-check invalid: no pool call


def _once() -> None:
    evaluate(BUNDLE, CASE, OK_PLAN, gross_only())


def _harness() -> Any:
    """WHI-1553's merged executable specification, loaded by path under a private name so
    none of its tests is collected twice (as tests/benchmark/test_diagnostics.py does)."""
    path = REPO / "tests" / "routing" / "test_suffix_repair_contract.py"
    spec = importlib.util.spec_from_file_location("_whi1554_suffix_repair", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


H = _harness()


def _alias_counts(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Independent counting wrappers (test-only) on the `evaluate` alias of every loaded
    `routing.algorithms` module and of the WHI-1553 specification module."""
    import routing.algorithms as package

    counts: dict[str, int] = {}
    modules = [H] + [
        m
        for n, m in sorted(sys.modules.items())
        if m is not None and n.startswith(package.__name__ + ".") and hasattr(m, "evaluate")
    ]
    for module in modules:
        inner = module.evaluate

        def wrapper(*a: Any, _n: str = module.__name__, _f: Any = inner, **k: Any) -> Any:
            counts[_n] = counts.get(_n, 0) + 1
            return _f(*a, **k)

        monkeypatch.setattr(module, "evaluate", wrapper)
    return counts


# ================================================================ the counter itself


def test_default_inactive_counts_nothing_and_outputs_are_identical() -> None:
    before = evaluate(BUNDLE, CASE, OK_PLAN, gross_only())
    with counted_evaluations() as n:
        during = evaluate(BUNDLE, CASE, OK_PLAN, gross_only())
    _once()  # after the block: the closed counter is no longer charged
    after = evaluate(BUNDLE, CASE, OK_PLAN, gross_only())
    assert n.count == 1
    assert before == during == after
    dumped = [json.dumps(e.to_dict(), sort_keys=True) for e in (before, during, after)]
    assert dumped[0] == dumped[1] == dumped[2]
    assert "count" not in dumped[0] and "evaluations" not in dumped[0]


def test_every_call_is_counted_ok_invalid_and_raising() -> None:
    class Boom(Exception):
        pass

    def raising(state: PoolState, token_in: str, amount: int) -> SwapResult[PoolState]:
        raise Boom

    with counted_evaluations() as n:
        assert evaluate(BUNDLE, CASE, OK_PLAN, gross_only()).status is EvalStatus.OK
        assert evaluate(BUNDLE, CASE, RESIDUAL_PLAN, gross_only()).status is EvalStatus.INVALID_PLAN
        assert (
            evaluate(BUNDLE, CASE, UNKNOWN_POOL_PLAN, gross_only()).status
            is EvalStatus.INVALID_PLAN
        )
        with pytest.raises(Boom):
            evaluate(BUNDLE, CASE, OK_PLAN, gross_only(), quote=raising)
        assert n.count == 4


def test_nested_block_replaces_the_outer_counter_until_it_exits() -> None:
    with counted_evaluations() as outer:
        _once()
        with counted_evaluations() as inner:
            _once()
            _once()
        _once()
    assert (outer.count, inner.count) == (2, 2)


def test_previous_counter_is_restored_on_exception() -> None:
    with counted_evaluations() as outer:
        with pytest.raises(RuntimeError), counted_evaluations() as inner:
            _once()
            raise RuntimeError
        _once()
    assert (outer.count, inner.count) == (1, 1)
    with counted_evaluations() as fresh:
        pass
    _once()
    assert fresh.count == 0  # no counter leaked past its block


def test_independent_threads_count_only_their_own_calls() -> None:
    barrier = threading.Barrier(2)
    got: dict[int, int] = {}

    def worker(calls: int) -> None:
        with counted_evaluations() as n:
            barrier.wait()
            for _ in range(calls):
                _once()
            barrier.wait()
        got[calls] = n.count

    with counted_evaluations() as main:
        threads = [threading.Thread(target=worker, args=(k,)) for k in (3, 5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert got == {3: 3, 5: 5}
    assert main.count == 0


def test_independent_asyncio_tasks_count_only_their_own_calls() -> None:
    async def task(calls: int) -> int:
        with counted_evaluations() as n:
            for _ in range(calls):
                _once()
                await asyncio.sleep(0)  # interleave with the sibling task
        return n.count

    async def run() -> tuple[int, int, int]:
        with counted_evaluations() as parent:
            a, b = await asyncio.gather(task(2), task(4))
        return a, b, parent.count

    assert asyncio.run(run()) == (2, 4, 0)


# ================================================================ real solves (actual aliases)


def _ig_context(bundle: SnapshotBundle, params: dict[str, int]) -> SolveContext:
    config = AlgorithmConfig(incremental_graph.NAME, params)
    return SolveContext(bundle, gross_only(), incremental_graph.prepare(bundle, config))


def _runs() -> list[tuple[SnapshotBundle, Case, dict[str, int]]]:
    runs = []
    for name in ("structural_trap", "twin_pools", "carry_and_zero_flow", "order_metadata"):
        bundle, case, spec = H.fixture_case(name)
        runs.append((bundle, case, dict(spec["settings"])))
    mixed = load_bundle(REPO / "tests" / "fixtures" / "routing" / "mantle_mixed")
    params = {"max_hops": 2, "max_splits": 4, "percent_step": 5, "chunks": 10}
    runs += [(mixed, c, params) for c in mixed.cases]
    return runs


def test_fallback_and_incumbent_replays_are_counted_through_every_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The embedded `path_split` fallback (with its `single_path`/`direct_split` replays)
    and `incremental_graph`'s incumbent replay call `evaluate` through their own module
    aliases; the seam counts each exactly once, equal to independent alias wrappers. The
    runner's independent evaluation after the block is not charged."""
    counts = _alias_counts(monkeypatch)
    seen: dict[str, int] = {}
    for bundle, case, params in _runs():
        ctx = _ig_context(bundle, params)
        ps_ctx = SolveContext(bundle, gross_only(), ctx.prepared.path_split)
        for solve, solve_ctx in ((path_split.solve, ps_ctx), (incremental_graph.solve, ctx)):
            counts.clear()
            with counted_evaluations() as n:
                result = solve(case, solve_ctx, Budget())
            assert n.count == sum(counts.values()), (case.case_id, counts)
            for module, k in counts.items():
                seen[module] = seen.get(module, 0) + k
            charged = n.count
            if result.plan is not None:  # the runner's final evaluation: outside the solve
                evaluate(bundle, case, result.plan, gross_only())
            assert n.count == charged
    assert {"single_path", "direct_split", "path_split", "incremental_graph"} <= {
        m.rsplit(".", 1)[-1] for m, k in seen.items() if k
    }


def test_structural_trap_counts_match_the_traced_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """suffix-repair.md §8/§9.7: `structural_trap` = 6 `single_path` + 1 `path_split`
    fallback replays, + 1 incumbent replay in `incremental_graph`, + 4 repair replays in
    WHI-1553's executable specification (total 12; repair-off total 8). The seam equals its
    research emulation (`EvaluationCounter`), the specification's own stage totals and
    independent alias wrappers."""
    bundle, case, spec = H.fixture_case("structural_trap")
    settings = dict(spec["settings"])

    ps_ctx = SolveContext(bundle, gross_only(), _ig_context(bundle, settings).prepared.path_split)
    counts = _alias_counts(monkeypatch)
    with counted_evaluations() as n:
        ps = path_split.solve(case, ps_ctx, Budget())
    assert ps.status is SolveStatus.OK
    assert counts == {"routing.algorithms.single_path": 6, "routing.algorithms.path_split": 1}
    assert n.count == 7

    counts.clear()
    with counted_evaluations() as n:
        ig = incremental_graph.solve(case, _ig_context(bundle, settings), Budget())
    assert ig.status is SolveStatus.OK
    assert n.count == 8 == sum(counts.values())
    assert counts["routing.algorithms.incremental_graph"] == 1

    for options, total, repair in ((H.PRESET, 12, 4), (H.REPAIR_OFF, 8, 0)):
        counts.clear()
        with counted_evaluations() as n:
            got = H.repair_solve(case, H.context(bundle, settings), Budget(), options)
        e = got.result.search_stats["evaluations"]
        assert e["total"] == e["fallback"] + e["incumbent"] + e["repair"]
        assert (e["fallback"], e["incumbent"], e["repair"]) == (7, 1, repair)
        assert n.count == total == e["total"] == sum(counts.values())
        assert got.result.search_stats["repair"]["repair_evaluations"] == repair

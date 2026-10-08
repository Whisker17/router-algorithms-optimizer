"""WHI-1623: `split_polish` (R023-C/1 §3, §4, §6, E1). The contract's probe fixtures F1-F9 and
F13 (`docs/references/research-023/probe/fixtures4.py.txt`) ported to the runtime module, the §9
implementation gates, the CLI wiring and -- through `ROUTER_TUNING_BUNDLE` -- the tuning
reproduction of the pinned probe outputs (`probe/results/ig_b2.json.gz`). `data/` exists only in
the primary clone: a skip of that test is not evidence."""

from __future__ import annotations

import gzip
import json
import math
import os
import re
import time
import warnings
from collections import Counter
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
import yaml

import main
from benchmark.objective import gross_only, synthetic_fixed_cost
from benchmark.profile import parse_profile, read_profile_document, strategy_group
from benchmark.results import load_case_records, load_manifest
from benchmark.runner import compare_runs
from benchmark.strategies import derive
from pools.quote import metered_quotes
from routing.algorithms import split_polish as sp
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    OptionsError,
    SolveContext,
    SolveResult,
    SolveStatus,
    validated_options,
)
from routing.algorithms.registry import ALGORITHMS, BASE_STRATEGIES, OPTIMIZED_STRATEGIES
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import FundInput, RoutePlan, SwapStep
from snapshot.bundle import load_bundle
from snapshot.models import BlockRef, Case, SnapshotBundle
from snapshot.models import ConcentratedPoolState as CL
from snapshot.models import ConstantProductPoolState as CP
from snapshot.models import LBStaticFeeParameters as LBStatic
from snapshot.models import LBVariableFeeParameters as LBVariable
from snapshot.models import LiquidityBookPoolState as LB

REPO = Path(__file__).resolve().parents[2]
NAME = sp.NAME
O = gross_only()  # noqa: E741 -- the probe's name
TUNING_ENV = "ROUTER_TUNING_BUNDLE"
MIXED = REPO / "tests" / "fixtures" / "routing" / "mantle_mixed"
TRACKED_CORPUS = REPO / "tests" / "fixtures" / "corpus" / "bundle"
PROBE_RESULTS = REPO / "docs" / "references" / "research-023" / "probe" / "results"
NOMINEE = {"base": "incremental_graph", "solver": "brent", "rounds": 2, "tolerance": 0.0001,
           "grid": 10**9, "maxiter": 60}  # fmt: skip
AID = 2**23  # LB price-1 bin


def settings(solver: str = "golden", rounds: int = 2) -> sp.Settings:
    """The probe's fixture settings: tolerance 1 bps of a fund (10**5 grid units), D = 10**9."""
    return sp.Settings(solver, rounds, 10**9, 10**5, 60)


def bundle(pools: list[Any], case: Case) -> SnapshotBundle:
    return SnapshotBundle(
        "fx", "synthetic", 1, BlockRef(1, 1, "h", 1), {p.pool_id: p for p in pools}, (case,), "h",
        "fx",
    )  # fmt: skip


def step(pid: str, tin: str, tout: str, fid: str, amount: Any, out: str) -> SwapStep:
    return SwapStep(pid, tin, tout, (FundInput(fid, amount),), out)


def ok_eval(b: SnapshotBundle, c: Case, plan: RoutePlan) -> Evaluation:
    ev = evaluate(b, c, plan, O)
    assert ev.status is EvalStatus.OK, ev.error
    return ev


def lb_pool(pid: str, bins: dict[int, tuple[int, int]], base_factor: int = 5000) -> LB:
    return LB(
        pid, "moe_lb_v2_2", "S", "T", 25, 1_000_000, AID, 0, sum(y for _, y in bins.values()), 0, 0,
        LBStatic(base_factor, 30, 600, 5000, 40000, 1000, 350_000),
        LBVariable(0, 0, AID, 1_000_000), (0, (1 << 24) - 1), bins,
    )  # fmt: skip


def cl_pool(pid: str, token0: str, token1: str, liquidity: int = 10**9) -> CL:
    return CL(
        pool_id=pid, source_key="uniswap_v3", token0=token0, token1=token1, fee=3000,
        tick_spacing=60, sqrt_price_x96=1 << 96, tick=0, liquidity=liquidity, fee_protocol=0,
        fee_growth_global0_x128=0, fee_growth_global1_x128=0, protocol_fees0=0, protocol_fees1=0,
        bitmap_word_range=(-1, 1),
    )  # fmt: skip


def run(
    b: SnapshotBundle,
    c: Case,
    plan: RoutePlan,
    *,
    solver: str = "golden",
    rounds: int = 2,
    cap: int | None = None,
    deadline: float | None = None,
) -> tuple[Evaluation, sp.Outcome, sp.Ledger, Evaluation, int]:
    """The probe's `run`: polish an evaluated plan on a fresh ledger inside a quote meter;
    returns (base evaluation, outcome, ledger, replay of the final plan, metered seam count)."""
    ev = ok_eval(b, c, plan)
    ledger = sp.Ledger(cap, deadline)
    with metered_quotes(None) as meter:
        outcome = sp.polish_plan(b, c, ev, ledger, settings(solver, rounds))
    final = plan if outcome.incumbent is None else outcome.incumbent.plan
    return ev, outcome, ledger, evaluate(b, c, final, O), meter.counted


def final_gross(ev: Evaluation, outcome: sp.Outcome) -> int:
    return ev.gross_output if outcome.incumbent is None else outcome.incumbent.gross


@pytest.fixture
def equivalence(monkeypatch: pytest.MonkeyPatch) -> Iterator[Counter[str]]:
    """Every `simulate` call of the polish is checked against the evaluator (outside the
    caller's meter): a feasible candidate's canonical plan replays `ok` with the same gross; a
    fully allocated infeasible one does not replay `ok`."""
    seen: Counter[str] = Counter()
    real = sp.simulate

    def checked(
        b: SnapshotBundle, topo: sp.Topology, shares: sp.Shares, amount: int, quote: Any
    ) -> sp.Simulation:
        sim = real(b, topo, shares, amount, quote)
        case = b.cases[0]
        with metered_quotes(None):  # the check's own replays are not charged to the polish
            if sim.gross is not None:
                rep = evaluate(b, case, sp.canonical_plan(topo, sim.alloc), O)
                assert rep.status is EvalStatus.OK and rep.gross_output == sim.gross, rep.error
                seen["feasible"] += 1
            else:
                seen["infeasible"] += 1
                slots = sum(len(s.inputs) for s in topo.steps)
                if len(sim.alloc) == slots:
                    rep = evaluate(b, case, sp.canonical_plan(topo, sim.alloc), O)
                    assert rep.status is not EvalStatus.OK
        return sim

    monkeypatch.setattr(sp, "simulate", checked)
    yield seen


# ======================================================================================
# probe fixtures F1-F9, F13 (contract §9), ported
# ======================================================================================

C1 = Case("c", "S", "T", 100)
B1 = bundle(
    [CP("a", "S", "T", 1000, 1000, 30), CP("b", "S", "T", 1000, 100, 30),
     CP("x", "S", "X", 1000, 2000, 30), CP("tx", "T", "X", 1000, 1000, 30)], C1,
)  # fmt: skip


@pytest.mark.parametrize("weights", [(1, 0), (0, 1)])
def test_f1_f2_endpoint_zeroing_in_both_orders_replays_exactly(weights: tuple[int, int]) -> None:
    base = RoutePlan(
        (step("a", "S", "T", "REQUEST", 50, "A"), step("b", "S", "T", "REQUEST", 50, "B"))
    )
    plan, _ = sp.canonical_from_evaluation(ok_eval(B1, C1, base))
    topo, _ = sp.topology_and_alloc(plan, C1)
    shares = {"REQUEST": [Fraction(w) for w in weights]}
    sim = sp.simulate(B1, topo, shares, 100, sp.Ledger(None).quote)
    rep = evaluate(B1, C1, sp.canonical_plan(topo, sim.alloc), O)
    assert rep.status is EvalStatus.OK and rep.gross_output == sim.gross
    assert sim.gross == {(1, 0): 90, (0, 1): 9}[weights]  # the probe log's values
    assert len(sp.canonical_plan(topo, sim.alloc).steps) == 1  # the zero step is dropped


def test_f3_a_dead_non_target_terminal_is_infeasible() -> None:
    topo = sp.Topology(
        (step("x", "S", "X", "REQUEST", 0, "dead"), step("a", "S", "T", "REQUEST", 0, "out")),
        "S", "T",
    )  # fmt: skip
    shares = {"REQUEST": [Fraction(1, 2), Fraction(1, 2)]}
    assert sp.simulate(B1, topo, shares, 100, sp.Ledger(None).quote).gross is None


def test_f4_a_target_fund_beside_a_zero_consumer_is_canonicalised_and_kept() -> None:
    base = RoutePlan(
        (step("a", "S", "T", "REQUEST", 100, "out"), step("tx", "T", "X", "out", 0, "dead"))
    )
    ev, outcome, _, rep, _ = run(B1, C1, base)
    assert outcome.scope == "fixed_funding_topology" and outcome.incumbent is not None
    assert outcome.incumbent.source == "base" and final_gross(ev, outcome) == ev.gross_output == 90
    assert outcome.incumbent.plan == RoutePlan((step("a", "S", "T", "REQUEST", 100, "out"),))
    assert rep.status is EvalStatus.OK


def test_f5_a_partially_consumed_target_fund_is_refused() -> None:
    plan = RoutePlan(
        (step("a", "S", "T", "REQUEST", 100, "out"), step("tx", "T", "X", "out", 1, "x1"))
    )
    topo, alloc = sp.topology_and_alloc(plan, C1)
    shares, why = sp.shares_or_refusal(topo, alloc, {"REQUEST": 100, "out": 90, "x1": 0})
    assert shares is None and why == "partially_consumed_fund:out"


@pytest.mark.parametrize("cap", [0, 1, 2, 3])
def test_f6_cap_0_to_3_identity_and_ledger_equals_seam(cap: int) -> None:
    base = RoutePlan((step("a", "S", "T", "REQUEST", 100, "out"),))
    ev, outcome, ledger, rep, seam = run(B1, C1, base, cap=cap)
    assert rep.status is EvalStatus.OK and final_gross(ev, outcome) == ev.gross_output
    assert seam == ledger.used == (0 if cap == 0 else 1)  # the reconstruction is charged
    assert outcome.truncated_by == ("max_quotes" if cap == 0 else None)


C7 = Case("retain", "S", "T", 300)
B7 = bundle(
    [CP("a", "S", "T", 1000, 2000, 0), CP("b", "S", "T", 1000, 100, 0),
     CP("c", "S", "T", 1000, 100, 0)], C7,
)  # fmt: skip
P7 = RoutePlan(tuple(step(p, "S", "T", "REQUEST", 100, p.upper()) for p in ("a", "b", "c")))


@pytest.mark.parametrize(("cap", "expect"), [(50, 199), (80, 342), (100, 342), (140, 461),
                                              (180, 461)])  # fmt: skip
def test_f7_completed_improvements_survive_truncation(cap: int, expect: int) -> None:
    ev, outcome, ledger, rep, seam = run(B7, C7, P7, cap=cap)
    assert ev.gross_output == 199
    assert rep.status is EvalStatus.OK and rep.gross_output == final_gross(ev, outcome) == expect
    assert seam == ledger.used <= cap and outcome.truncated_by == "max_quotes"
    _, full, *_ = run(B7, C7, P7)
    assert full.truncated_by is None and full.incumbent is not None and full.incumbent.gross == 461


def test_f8_the_incumbent_and_the_endpoints_are_evaluated_distinctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = Case("small", "S", "T", 10**12)
    b = bundle([CP("a", "S", "T", 10**15, 10**18, 0), CP("b", "S", "T", 10**15, 10**18, 0)], c)
    seen: list[tuple[Fraction, ...]] = []
    real = sp.simulate

    def spy(b_: SnapshotBundle, topo: sp.Topology, shares: sp.Shares, a: int, q: Any) -> Any:
        seen.append(tuple(shares["REQUEST"]))
        return real(b_, topo, shares, a, q)

    monkeypatch.setattr(sp, "simulate", spy)
    base = RoutePlan(
        (step("a", "S", "T", "REQUEST", 1, "A"), step("b", "S", "T", "REQUEST", 10**12 - 1, "B"))
    )
    run(b, c, base, rounds=1)
    incumbent = (Fraction(1, 10**12), Fraction(10**12 - 1, 10**12))
    assert seen.count(incumbent) >= 2  # the reconstruction and t = 0 inside the line search
    assert (Fraction(0), Fraction(1)) in seen and (Fraction(1), Fraction(0)) in seen
    assert len(set(seen)) > 3


def test_f9_brent_penalty_raises_no_numeric_warning_and_the_winner_is_exact() -> None:
    def eval_t(t: Fraction) -> sp.Point:
        k = t * 10**9
        return ((10**30 - (int(k) - 123456789) ** 2) if 0 <= k <= 2 * 10**8 else None), None

    stats: dict[str, Any] = {}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        best = sp.line_search(
            eval_t, Fraction(1, 2), Fraction(1, 2), sp.Settings("brent", 2, 10**9, 10**5, 60),
            stats,
        )  # fmt: skip
    assert caught == []
    assert best is not None and abs(int(best[1] * 10**9) - 123456789) <= 10**5
    assert stats["brent_status"] == {"0": 1}


def test_f13_an_evaluator_valid_cl_zero_output_consumer_is_refused_with_the_true_gross() -> None:
    c = Case("partial-target", "S", "T", 100)
    b = bundle([CP("st", "S", "T", 1000, 1000, 30), cl_pool("tx", "T", "X")], c)
    plan = RoutePlan(
        (step("st", "S", "T", "REQUEST", 100, "out"), step("tx", "T", "X", "out", 1, "dead"))
    )
    ev, outcome, ledger, rep, seam = run(b, c, plan)
    assert outcome.scope == "unsupported_topology" and outcome.incumbent is None
    assert outcome.reason == "partially_consumed_fund:out"
    assert ev.gross_output == rep.gross_output == 89 and seam == ledger.used == 0


def test_the_documented_worked_example() -> None:
    """`docs/references/routing-algorithms.md` §23.5: the published sequence, simulations and
    quotes of both solvers on the F7 fixture."""
    ev = ok_eval(B7, C7, P7)
    assert [t.amount_out for t in ev.trace] == [181, 9, 9]
    for solver, simulations, quotes in (("brent", 92, 197), ("golden", 99, 210)):
        published: list[RoutePlan] = []
        ledger = sp.Ledger(None)
        outcome = sp.polish_plan(B7, C7, ev, ledger, settings(solver), published.append)
        steps = [[(s.pool_id, s.inputs[0].amount) for s in p.steps] for p in published]
        assert steps == [[("a", 200), ("c", 100)], [("a", 300)]]
        replays = [evaluate(B7, C7, p, O) for p in published]
        assert [r.gross_output for r in replays] == [342, 461]
        assert [[t.amount_out for t in r.trace] for r in replays] == [[333, 9], [461]]
        assert outcome.work["simulations"] == simulations and ledger.used == quotes
        assert outcome.work["accepted"] == 2 and outcome.work["polish_calls"] == 1


# ======================================================================================
# implementation gates (contract §9)
# ======================================================================================


@pytest.mark.parametrize("solver", sp.SOLVERS)
def test_analytic_two_cpmm_optimum(solver: str) -> None:
    """Fee-free CPMMs: the continuous optimum equalises the marginal rates,
    `(x1 + a*) / sqrt(x1 y1) = (x2 + b*) / sqrt(x2 y2)`; the polish lands within its share
    tolerance of it, and its gross loss against the exact integer optimum is within the
    second-order bound of that tolerance (`|f''| / 2 * delta**2`, plus two units of rounding)."""
    x1 = y1 = x2 = 10**12
    y2 = 4 * 10**12  # sqrt(x1 y1) = 10**12, sqrt(x2 y2) = 2 * 10**12
    amount = 3 * 10**12
    a_star = Fraction(x1 + x2 + amount, 3) - x1  # 2/3 * 10**12
    c = Case("analytic", "S", "T", amount)
    b = bundle([CP("p", "S", "T", x1, y1, 0), CP("q", "S", "T", x2, y2, 0)], c)

    def gross(a: int) -> int:
        return y1 * a // (x1 + a) + y2 * (amount - a) // (x2 + amount - a)

    exact = max(gross(a) for a in range(math.floor(a_star) - 3, math.ceil(a_star) + 4))
    base = RoutePlan(
        (step("p", "S", "T", "REQUEST", amount // 2, "P"),
         step("q", "S", "T", "REQUEST", amount - amount // 2, "Q"))
    )  # fmt: skip
    ev, outcome, _, rep, _ = run(b, c, base, solver=solver)
    assert outcome.incumbent is not None
    got = outcome.incumbent.plan.steps[0].inputs[0].amount
    assert isinstance(got, int) and abs(got - a_star) <= Fraction(1, 10**4) * amount
    assert rep.gross_output == outcome.incumbent.gross == gross(got) > ev.gross_output
    delta = Fraction(1, 10**4) * amount
    curvature = (
        Fraction(y1 * x1) / (x1 + a_star - delta) ** 3
        + Fraction(y2 * x2) / (x2 + amount - a_star - delta) ** 3
    )  # |f''| / 2 anywhere within delta of the optimum
    assert exact - (curvature * delta**2 + 2) <= outcome.incumbent.gross <= exact


def _two(c: Case, a: int) -> RoutePlan:
    steps = []
    if a:
        steps.append(step("p", "S", "T", "REQUEST", a, "P"))
    if c.amount_in - a:
        steps.append(step("q", "S", "T", "REQUEST", c.amount_in - a, "Q"))
    return RoutePlan(tuple(steps))


ORACLE_INSTANCES: dict[str, tuple[list[Any], int]] = {
    # integer floors make these non-unimodal on the integers: several scattered optima
    "cpmm_a": ([CP("p", "S", "T", 300, 900, 30), CP("q", "S", "T", 400, 900, 30)], 200),
    "cpmm_b": ([CP("p", "S", "T", 1000, 3000, 30), CP("q", "S", "T", 400, 900, 30)], 200),
    # p needs >= 35 in for one unit out, the 3-bin LB pool q runs out of liquidity above 15
    "island": ([CP("p", "S", "T", 1000, 30, 0), lb_pool("q", {AID - k: (0, 4) for k in range(3)})],
               50),
    # zero-fee LB bins at price 1: every split and both endpoints give exactly the input
    "plateau": ([lb_pool("p", {AID: (0, 10**6)}, 0), lb_pool("q", {AID: (0, 10**6)}, 0)], 40),
}  # fmt: skip


@pytest.mark.parametrize("solver", sp.SOLVERS)
@pytest.mark.parametrize("instance", sorted(ORACLE_INSTANCES))
def test_exhaustive_small_integer_oracle(
    instance: str, solver: str, equivalence: Counter[str]
) -> None:
    """Every feasible two-consumer base of a small instance against the exhaustive integer
    optimum: never worse, never above the oracle, every simulated point equal to the evaluator,
    and within one raw unit of the oracle -- except Brent on the island, where the optimum is
    the island's edge next to infeasible points (a local search with a finite penalty: it is
    never worse, it does not always reach the edge; golden does). The plateau keeps the base
    (tie rule: t = 0)."""
    pools, amount = ORACLE_INSTANCES[instance]
    c = Case(instance, "S", "T", amount)
    b = bundle(pools, c)
    with metered_quotes(None):
        oracle = {a: evaluate(b, c, _two(c, a), O) for a in range(amount + 1)}
    feasible = {a: e.gross_output for a, e in oracle.items() if e.status is EvalStatus.OK}
    best = max(feasible.values())
    gaps: Counter[int] = Counter()
    for a in range(1, amount):
        if a not in feasible:
            continue
        ev, outcome, _, rep, _ = run(b, c, _two(c, a), solver=solver)
        got = final_gross(ev, outcome)
        assert rep.status is EvalStatus.OK and rep.gross_output == got
        assert feasible[a] <= got <= best
        gaps[best - got] += 1
        if instance == "plateau":
            assert outcome.incumbent is not None and outcome.incumbent.source == "base"
    if instance == "island":
        assert len([a for a in feasible if 0 < a < amount]) <= 14  # narrow
        assert equivalence["infeasible"] > 0
    if instance == "island" and solver == "brent":
        assert gaps[0] >= 1 and sum(gaps.values()) == len(feasible) - 1
    else:
        assert max(gaps) <= 1, gaps
    assert equivalence["feasible"] > 0


def test_lb_variable_fee_bin_crossing_and_shared_pool_sequential_state(
    equivalence: Counter[str],
) -> None:
    """An LB pool used by two steps of one plan (sequential state, variable fee growing with
    every bin crossed) beside a CPMM: every simulated point equals the evaluator and the
    polished plan crosses bins."""
    c = Case("lb", "S", "T", 8 * 10**6)
    b = bundle([lb_pool("lb", {AID - k: (0, 10**6) for k in range(10)}),
                CP("cp", "S", "T", 10**7, 10**7, 30)], c)  # fmt: skip
    base = RoutePlan(
        (step("lb", "S", "T", "REQUEST", 10**6, "L1"),
         step("cp", "S", "T", "REQUEST", 6 * 10**6, "C"),
         step("lb", "S", "T", "REQUEST", 10**6, "L2"))
    )  # fmt: skip
    for solver in sp.SOLVERS:
        ev, outcome, _, rep, _ = run(b, c, base, solver=solver)
        assert outcome.incumbent is not None and outcome.incumbent.gross > ev.gross_output
        assert rep.status is EvalStatus.OK and rep.gross_output == outcome.incumbent.gross
        assert rep.route_features["lb_bins_swapped"] > 2
    assert equivalence["feasible"] > 0


def test_cl_incomplete_coverage_points_are_infeasible_and_skipped(
    equivalence: Counter[str],
) -> None:
    """A CL pool whose collected bitmap ends inside the search range: points beyond it are
    `incomplete_snapshot` (infeasible), never returned. Both golden probes of the first bracket
    fall there (-inf), so golden keeps the base; Brent (finite penalty) improves it."""
    c = Case("cl", "S", "T", 6 * 10**9)
    b = bundle([cl_pool("cl", "S", "T"), CP("cp", "S", "T", 10**10, 10**10, 30)], c)
    base = RoutePlan(
        (step("cl", "S", "T", "REQUEST", 10**9, "A"),
         step("cp", "S", "T", "REQUEST", 5 * 10**9, "B"))
    )  # fmt: skip
    all_in_cl = RoutePlan((step("cl", "S", "T", "REQUEST", c.amount_in, "A"),))
    assert evaluate(b, c, all_in_cl, O).status is not EvalStatus.OK  # beyond the bitmap range
    for solver in sp.SOLVERS:
        ev, outcome, _, rep, _ = run(b, c, base, solver=solver)
        assert rep.status is EvalStatus.OK and rep.gross_output == final_gross(ev, outcome)
        assert (final_gross(ev, outcome) > ev.gross_output) == (solver == "brent")
    assert equivalence["infeasible"] > 0 and equivalence["feasible"] > 0


def test_an_amount_below_the_consumer_count(equivalence: Counter[str]) -> None:
    """A = 3 against four raw consumers (one zero reference, dropped by canonicalisation): the
    floors give 0 to most consumers, the last positive share takes the remainder, zero-input
    steps are skipped, and every point replays exactly."""
    c = Case("tiny", "S", "T", 3)
    b = bundle([CP(p, "S", "T", 10, 10**6, 0) for p in "abcd"], c)
    zero = step("d", "S", "T", "REQUEST", 0, "D")
    base = RoutePlan((zero, *(step(p, "S", "T", "REQUEST", 1, p.upper()) for p in "abc")))
    for solver in sp.SOLVERS:
        ev, outcome, _, rep, _ = run(b, c, base, solver=solver)
        assert outcome.incumbent is not None and outcome.scope == "fixed_funding_topology"
        assert "d" not in {s.pool_id for s in outcome.incumbent.plan.steps}
        assert rep.status is EvalStatus.OK and rep.gross_output == final_gross(ev, outcome)
        assert final_gross(ev, outcome) >= ev.gross_output
    assert equivalence["feasible"] > 0


def test_an_all_zero_plan_polishes_from_a_zero_baseline() -> None:
    """A valid plan of zero gross (CL dust swaps that output 0): the Brent relative objective
    uses the reference 1, nothing divides by zero, and a positive split is found."""
    c = Case("zero", "S", "T", 4)
    b = bundle([cl_pool("p", "S", "T"), cl_pool("q", "S", "T")], c)
    base = RoutePlan(
        (step("p", "S", "T", "REQUEST", 2, "P"), step("q", "S", "T", "REQUEST", 2, "Q"))
    )
    for solver in sp.SOLVERS:
        ev, outcome, _, rep, _ = run(b, c, base, solver=solver)
        assert ev.gross_output == 0
        assert rep.status is EvalStatus.OK and rep.gross_output == final_gross(ev, outcome) > 0


def test_a_past_deadline_stops_with_time_and_keeps_the_base() -> None:
    ev, outcome, ledger, rep, seam = run(B7, C7, P7, deadline=time.monotonic() - 1)
    assert outcome.truncated_by == "time" and ledger.used == seam == 0
    assert final_gross(ev, outcome) == ev.gross_output == rep.gross_output


# ======================================================================================
# solve(): the base strategy, the shared ledger, statuses
# ======================================================================================

CS = Case("ig", "S", "T", 10**6)
BS = bundle([CP("p", "S", "T", 10**6, 3 * 10**6, 30), CP("q", "S", "T", 10**6, 2 * 10**6, 30),
             CP("x", "S", "X", 10**6, 10**6, 30)], CS)  # fmt: skip
PARAMS = {"max_hops": 2, "max_splits": 2, "percent_step": 25, "chunks": 4}


def prepared(b: SnapshotBundle = BS, **options: Any) -> sp.PreparedSplitPolish:
    return sp.prepare(b, AlgorithmConfig(NAME, PARAMS, {**NOMINEE, **options}))


def solve(
    case: Case = CS,
    budget: Budget | None = None,
    prep: sp.PreparedSplitPolish | None = None,
    b: SnapshotBundle = BS,
    objective: Any = O,
) -> tuple[SolveResult, list[RoutePlan], int]:
    sink: list[RoutePlan] = []
    context = SolveContext(b, objective, prep or prepared(b), candidate_sink=sink.append)
    with metered_quotes(None if budget is None else budget.max_quotes) as meter:
        result = sp.solve(case, context, budget or Budget())
    return result, sink, meter.counted


def base_solve(case: Case = CS, b: SnapshotBundle = BS) -> tuple[SolveResult, int]:
    from routing.algorithms import incremental_graph

    prep = incremental_graph.prepare(b, AlgorithmConfig("incremental_graph", PARAMS))
    with metered_quotes(None) as meter:
        result = incremental_graph.solve(case, SolveContext(b, O, prep), Budget())
    return result, meter.counted


def test_solve_polishes_the_base_and_the_ledger_equals_the_seam() -> None:
    base, base_quotes = base_solve()
    assert base.status is SolveStatus.OK and base.score is not None
    result, published, seam = solve()
    record = result.search_stats[NAME]
    assert result.algorithm == NAME and result.status is SolveStatus.OK
    assert record["scope"] == "fixed_funding_topology" and record["truncated_by"] is None
    assert result.search_stats["base"]["quotes"] == base_quotes
    assert seam == base_quotes + record["quotes"]  # one ledger, every quote charged once
    assert result.plan is not None and result.score is not None and result.score > base.score
    rep = evaluate(BS, CS, result.plan, O)
    assert rep.status is EvalStatus.OK and rep.gross_output == result.score
    assert record["base_gross"] == str(base.score) and record["gross"] == str(result.score)
    assert published[-1] == result.plan and base.plan in published  # incumbents published
    assert record["work"]["accepted"] >= 1 and result.evaluation is None
    json.dumps(result.to_dict())  # JSON-serialisable record


def test_budget_zero_returns_the_base_result_literally() -> None:
    base, base_quotes = base_solve()
    result, _, seam = solve(budget=Budget(max_quotes=base_quotes))
    record = result.search_stats[NAME]
    assert record["truncated_by"] == "max_quotes" and record["quotes"] == 0
    assert seam == base_quotes
    assert (result.status, result.plan, result.evaluation, result.score) == (
        base.status, base.plan, base.evaluation, base.score
    )  # fmt: skip


def test_truncation_is_never_no_route_at_any_cap() -> None:
    _, base_quotes = base_solve()
    full, _, full_seam = solve()
    grosses = []
    for cap in range(base_quotes, full_seam + 2):
        result, _, seam = solve(budget=Budget(max_quotes=cap))
        record = result.search_stats[NAME]
        assert result.status is SolveStatus.OK and result.plan is not None and seam <= cap
        assert record["truncated_by"] == (None if cap >= full_seam else "max_quotes")
        rep = evaluate(BS, CS, result.plan, O)
        assert rep.status is EvalStatus.OK and str(rep.gross_output) == record["gross"]
        grosses.append(rep.gross_output)
    assert grosses == sorted(grosses) and grosses[-1] == full.score  # retention is monotone
    # time: a zero allowance stops at the first polish quote, status ok, never no_route
    result, _, _ = solve(budget=Budget(time_limit_seconds=1e-9))
    assert result.status is SolveStatus.OK and result.search_stats[NAME]["truncated_by"] == "time"


def test_a_non_gross_objective_is_unsupported_and_the_base_never_runs() -> None:
    objective = synthetic_fixed_cost(1)
    result, published, seam = solve(objective=objective)
    assert result.status is SolveStatus.UNSUPPORTED and result.plan is None
    assert result.search_stats[NAME]["scope"] == "objective" and seam == 0 and published == []
    assert "gross_only" in str(result.error)


def test_base_statuses_pass_through() -> None:
    unreachable = Case("none", "S", "Z", 10**6)
    result, _, _ = solve(unreachable)
    base, _ = base_solve(unreachable)
    assert result.status is base.status is SolveStatus.NO_ROUTE
    assert result.error == base.error and result.search_stats[NAME]["scope"] is None


def test_an_unsupported_topology_returns_the_base_result_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F13 through `solve`: a base whose plan is outside the domain -> `ok`, base plan, scope."""
    c = Case("partial-target", "S", "T", 100)
    b = bundle([CP("st", "S", "T", 1000, 1000, 30), cl_pool("tx", "T", "X")], c)
    plan = RoutePlan(
        (step("st", "S", "T", "REQUEST", 100, "out"), step("tx", "T", "X", "out", 1, "dead"))
    )
    ev = ok_eval(b, c, plan)
    base = SolveResult(c.case_id, "incremental_graph", SolveStatus.OK, plan, ev, ev.gross_output)

    def fake_solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
        return base

    monkeypatch.setattr(sp, "BASES", {"incremental_graph": AlgorithmFactory("x", fake_solve)})
    prep = sp.PreparedSplitPolish(
        "incremental_graph", None, NOMINEE, sp.Settings.from_options(NOMINEE)
    )
    result, _, seam = solve(c, prep=prep, b=b)
    assert result.status is SolveStatus.OK and result.plan == plan and result.evaluation == ev
    assert result.search_stats[NAME]["scope"] == "unsupported_topology" and seam == 0


def test_options_are_validated_all_required_and_exact() -> None:
    factory = ALGORITHMS[NAME]
    assert validated_options(factory, NOMINEE) == NOMINEE
    assert sp.Settings.from_options(NOMINEE) == sp.Settings("brent", 2, 10**9, 10**5, 60)
    for bad in (
        {},
        {k: v for k, v in NOMINEE.items() if k != "maxiter"},
        {**NOMINEE, "extra": 1},
        {**NOMINEE, "base": "single_path"},  # WHI-1626: a still-unregistered base
        {**NOMINEE, "base": NAME},
        {**NOMINEE, "solver": "nelder"},
        {**NOMINEE, "rounds": 0},
        {**NOMINEE, "rounds": True},
        {**NOMINEE, "tolerance": 0},
        {**NOMINEE, "tolerance": float("nan")},
        {**NOMINEE, "grid": 1},
        {**NOMINEE, "maxiter": 2.0},
        {**NOMINEE, "max_quotes": 1},
    ):
        with pytest.raises(OptionsError):
            validated_options(factory, bad)
    with pytest.raises(OptionsError):  # the public prepare validates too
        sp.prepare(BS, AlgorithmConfig(NAME, PARAMS, {}))


def test_registered_as_a_custom_profile_selected_identity() -> None:
    factory = ALGORITHMS[NAME]
    assert factory is sp.FACTORY and list(ALGORITHMS).count(NAME) == 1
    assert NAME not in BASE_STRATEGIES and NAME not in OPTIMIZED_STRATEGIES
    assert strategy_group(NAME) == "custom"
    assert (factory.options_preset or {}).get("key") == "R024-P01-split_polish"
    assert factory.search_params == ("max_hops", "max_splits", "percent_step")
    assert factory.graph_params == ("chunks",)
    assert "NOT Jupiter Metis" in str(factory.provenance)
    # WHI-1632: `--strategies all` appends it once, with its selected preset; the other modes
    # and every saved/default profile are unchanged
    for name in ("daily_gross.yaml", "full_gross.yaml", "daily.yaml", "full.yaml"):
        source = read_profile_document(REPO / "config" / name)
        for mode in ("all", "base", "optimized", "profile"):
            document, profile = derive(source, mode, source_path=name, source_sha256="x")
            if mode != "all":
                assert NAME not in profile.algorithms and NAME not in json.dumps(document)
                continue
            assert list(profile.algorithms).count(NAME) == 1
            assert profile.algorithm_options[NAME]["source"]["kind"] == "preset"
    # a profile without its options is refused (no built-in default)
    doc = read_profile_document(REPO / "config" / "full_gross.yaml")
    doc["algorithms"] = [NAME]
    with pytest.raises(Exception, match="requires algorithm_options.split_polish"):
        parse_profile(doc, "p")


def test_importing_the_module_never_imports_scipy() -> None:
    import subprocess
    import sys

    code = "import sys, routing.algorithms.split_polish; print('scipy' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=REPO, check=True)  # fmt: skip
    assert out.stdout.strip() == "False"


# ======================================================================================
# CLI: run, quote, replay, order checks
# ======================================================================================


def _profile(tmp_path: Path, order: str = "fixed", repeats: int = 1, name: str = "p") -> Path:
    doc = read_profile_document(REPO / "config" / "daily_gross.yaml")
    doc["algorithms"] = ["incremental_graph", NAME]
    doc["search"] = {"max_hops": 2, "max_splits": 4, "percent_step": 5}
    doc["graph"] = {"chunks": 20}
    doc["algorithm_options"] = {NAME: NOMINEE}
    doc["measurement"] = {"warmup": 0, "repeats": repeats, "seed": 7, "order": order,
                          "memory_pass": False}  # fmt: skip
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    parse_profile(doc, str(path))
    return path


def _run(tmp_path: Path, profile: Path, results: Path) -> Path:
    before = set(results.iterdir()) if results.exists() else set()
    argv = ["run", "--bundle", str(MIXED), "--profile", str(profile), "--results-dir",
            str(results), "--strategies", "profile"]  # fmt: skip
    assert main.main(argv) == 0
    (run_dir,) = set(results.iterdir()) - before
    return run_dir


def test_cli_run_selects_it_never_worse_and_replays_literally(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results"
    run_dir = _run(tmp_path, _profile(tmp_path, repeats=2), results)  # 2 attempts: consistent
    manifest = load_manifest(run_dir)
    assert list(manifest.algorithms) == ["incremental_graph", NAME]
    resolved = manifest.resolved_profile
    assert resolved["algorithm_options"][NAME]["options"] == NOMINEE
    assert resolved["algorithm_options"][NAME]["source"] == {"kind": "override"}
    assert "NOT Jupiter Metis" in resolved["algorithm_config"][NAME]["provenance"]["label"]
    records = load_case_records(run_dir)
    by = {(r["algorithm"], r["case_id"]): r for r in records}
    improved = 0
    for case_id in manifest.measurement["case_order"]:
        base, record = by[("incremental_graph", case_id)], by[(NAME, case_id)]
        assert record["status"] == base["status"]
        if base["status"] != "ok":
            continue
        assert int(record["score"]) >= int(base["score"])
        assert record["search"]["base"]["status"] == "ok"
        assert record["search"][NAME]["gross"] == record["score"]
        assert record["quotes"]["counted"] == (
            record["search"]["base"]["quotes"] + record["search"][NAME]["quotes"]
        )  # the worker meter is the shared ledger
        assert record["measurement"]["attempts_consistent"] is True
        improved += int(record["score"]) > int(base["score"])
    assert improved >= 1
    # the saved effective profile replays literally, with identical deterministic records
    capsys.readouterr()
    replay = manifest.replay_command.split("main.py", 1)[1].split()
    assert replay[-2:] == ["--strategies", "profile"]
    assert main.main(replay) == 0
    (again,) = [p for p in results.iterdir() if p != run_dir]
    assert load_manifest(again).resolved_profile == resolved
    assert compare_runs(run_dir, again) == []
    out = tmp_path / "report"
    assert main.main(["report", str(run_dir), "--output", str(out)]) == 0
    assert "split polishing" in (out / "report.html").read_text()


@pytest.mark.parametrize("order", ["reverse", "shuffle"])
def test_reverse_and_shuffle_orders_give_identical_records(tmp_path: Path, order: str) -> None:
    results = tmp_path / "results"
    fixed = _run(tmp_path, _profile(tmp_path, name="fixed"), results)
    other = _run(tmp_path, _profile(tmp_path, order=order, name=order), results)
    assert load_manifest(other).measurement["order"] == order
    assert main.main(["order-check", str(fixed), str(other)]) == 0


def test_cli_quote_selects_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["quote", "--bundle", str(TRACKED_CORPUS), "--profile", str(_profile(tmp_path)),
            "--token-in", "USDC", "--token-out", "USDT0", "--amount", "1500.25", "--quotes-dir",
            str(tmp_path / "q"), "--strategies", "profile", "--details"]  # fmt: skip
    assert main.main(args) == 0
    out = capsys.readouterr().out
    match = re.search(r"\(run (\S+)\)", out)
    assert match
    records = load_case_records(Path(match.group(1)))
    assert [r["algorithm"] for r in records] == ["incremental_graph", NAME]
    assert f"[{NAME}] ok" in out
    assert int(records[1]["score"]) >= int(records[0]["score"])


# ======================================================================================
# tuning reproduction of the pinned probe outputs (acceptance 2)
# ======================================================================================


def _plan_json(plan: RoutePlan) -> list[dict[str, Any]]:
    return [{"pool": s.pool_id, "tin": s.token_in, "tout": s.token_out,
             "inputs": [[i.fund_id, i.amount] for i in s.inputs], "out": s.output_fund_id}
            for s in plan.steps]  # fmt: skip


# (arm, options, improved cases in the contract's §7 table). `ig_b2` is the E1 nominee, `ig_g2` the
# golden solver control, `ps_b2` the nominee settings on `path_split`.
ARMS = [
    ("ig_b2", NOMINEE, 72),
    ("ig_g2", {**NOMINEE, "solver": "golden"}, 73),
    ("ps_b2", {**NOMINEE, "base": "path_split"}, 58),
]


@pytest.mark.parametrize(("arm", "options", "improved_cases"), ARMS, ids=[a[0] for a in ARMS])
def test_the_pinned_probe_outputs_are_reproduced_on_the_tuning_split(
    arm: str, options: dict[str, Any], improved_cases: int
) -> None:
    """The base (`config/full_gross.yaml` search/graph/budget, `incremental_graph` c50 or
    `path_split`) + the polish on all 96 tuning cases equals `probe/results/<arm>.json.gz` case by
    case: canonical plan, gross, polish quotes, base quotes and gross, truncation and the search
    counters. The probe charges the polish to `CAP_RESIDUAL - base quotes`, i.e. the one ledger of
    §4.6 shared with the base."""
    path = os.environ.get(TUNING_ENV)
    if not path:
        pytest.skip(f"set {TUNING_ENV} to the frozen bundle_tuning (data/ is primary-clone only)")
    assert Path(path).is_dir(), f"{TUNING_ENV}={path!r} is not a directory"
    b = load_bundle(path)
    assert len(b.cases) == 96
    with gzip.open(PROBE_RESULTS / f"{arm}.json.gz") as fh:
        pinned = {r["case"]: r for r in json.load(fh)["rows"]}
    profile = read_profile_document(REPO / "config" / "full_gross.yaml")
    params = {**profile["search"], **profile["graph"]}
    budget = Budget(profile["budget"]["time_limit_seconds"], profile["budget"]["max_quotes"])
    prep = sp.prepare(b, AlgorithmConfig(NAME, params, options))
    differences: list[tuple[str, str]] = []
    improved = 0
    for case in b.cases:
        result, _, seam = solve(case, budget, prep, b)
        record, base = result.search_stats[NAME], result.search_stats["base"]
        assert result.status is SolveStatus.OK and result.plan is not None
        rep = evaluate(b, case, result.plan, O)
        assert rep.status is EvalStatus.OK and str(rep.gross_output) == record["gross"]
        assert seam == base["quotes"] + record["quotes"] <= profile["budget"]["max_quotes"]
        p = pinned[case.case_id]
        work = record["work"]
        got = {
            "plan": _plan_json(sp.canonical_from_evaluation(rep)[0]), "final": rep.gross_output,
            "q_total": record["quotes"], "base_quotes": base["quotes"],
            "recorded": int(record["base_gross"]), "truncated_by": record["truncated_by"],
            **{k: work.get(k) for k in ("polish_calls", "accepted", "brent_nfev", "brent_status",
                                        "golden_iters")},
        }  # fmt: skip
        want = {**{k: p[k] for k in ("plan", "final", "q_total", "base_quotes", "recorded",
                                       "truncated_by")}, **p["stats"]}  # fmt: skip
        differences += [(case.case_id, k) for k in got if got[k] != want.get(k)]
        improved += rep.gross_output > int(record["base_gross"])
    print(f"TUNING REPRODUCTION {arm}: 96 cases, {improved} improved, differences {differences}")
    assert differences == [] and improved == improved_cases


def test_every_number_of_chapter_23_is_asserted() -> None:
    """`routing-algorithms.md` §23 publishes only numbers that this file asserts, that the
    contract states (tuning evidence), that the committed research-023 campaign evidence states
    (report-split results: `report-analysis.json` and `report-tables.md`, which `tests/research_023`
    regenerates byte-identically from it; `tests/docs/test_r023_examples.py` also pins each cited
    row), or the chapter's own option ranges and ids. Section references (§23.9) are anchors."""
    guide = (REPO / "docs" / "references" / "routing-algorithms.md").read_text(encoding="utf-8")
    end = guide.find("\n## 24. ")  # WHI-1624's chapter 24 is checked by test_marginal_activation.py
    chapter = guide[guide.index("## 23. ") : end if end >= 0 else len(guide)]
    chapter = re.sub(r"```.*?```", " ", chapter, flags=re.S)  # pseudocode and diagram
    chapter = re.sub(r"^#+ .*$", " ", chapter, flags=re.M)  # headings
    chapter = re.sub(r"§§?\s*\d+(?:\.\d+)?(?:\s*(?:,|and|–|-)\s*\d+(?:\.\d+)?)*", " ", chapter)
    r023 = REPO / "docs" / "references" / "research-023"
    contract = (r023 / "contract.md").read_text()
    campaign = "".join((r023 / "campaign" / f).read_text()
                       for f in ("report-analysis.json", "report-tables.md"))  # fmt: skip
    backed = (Path(__file__).read_text(encoding="utf-8") + contract + campaign
              + " 1000000000 1623 1624 1627 1.0 ")  # fmt: skip
    number = r"(?<![\w.])\d+(?:\.\d+)?(?![\w])"
    published = {n for n in re.findall(number, chapter) if len(n) >= 3 or "." in n}
    assert len(published) >= 20  # not vacuous
    assert sorted(n for n in published if n not in set(re.findall(number, backed))) == []

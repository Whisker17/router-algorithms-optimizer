"""WHI-1624: `marginal_activation` (R023-C/1 §3, §5, §6, E2). The contract's probe fixtures F10-F12
and F14-F16 (`docs/references/research-023/probe/fixtures4.py.txt`) ported to the runtime module,
the §9 E2 gates (invalid-best / valid-second-best admission, quote-cap sweeps for both modes), one
test per acceptance rule that fails when that rule alone is removed, the controls (§5.3), the CLI
wiring and -- through `ROUTER_TUNING_BUNDLE` -- the tuning reproduction of the pinned PF nominee
(`probe/results/ig_actpf.json.gz`, controls included). `data/` exists only in the primary clone: a
skip of that test is not evidence."""

from __future__ import annotations

import gzip
import json
import os
import re
import time
from collections import Counter
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
from routing.algorithms import marginal_activation as ma
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
from routing.evaluator import EvalStatus, Evaluation, _token_cycle, evaluate
from routing.plan import FundInput, RoutePlan, SwapStep
from snapshot.bundle import load_bundle
from snapshot.models import BlockRef, Case, SnapshotBundle
from snapshot.models import ConcentratedPoolState as CL
from snapshot.models import ConstantProductPoolState as CP
from snapshot.models import LBStaticFeeParameters as LBStatic
from snapshot.models import LBVariableFeeParameters as LBVariable
from snapshot.models import LiquidityBookPoolState as LB

REPO = Path(__file__).resolve().parents[2]
NAME = ma.NAME
O = gross_only()  # noqa: E741 -- the probe's name
TUNING_ENV = "ROUTER_TUNING_BUNDLE"
MIXED = REPO / "tests" / "fixtures" / "routing" / "mantle_mixed"
TRACKED_CORPUS = REPO / "tests" / "fixtures" / "corpus" / "bundle"
PROBE_RESULTS = REPO / "docs" / "references" / "research-023" / "probe" / "results"
E1_NOMINEE = {"base": "incremental_graph", "solver": "brent", "rounds": 2, "tolerance": 0.0001,
              "grid": 10**9, "maxiter": 60}  # fmt: skip
NOMINEE = {**E1_NOMINEE, "mode": "pf", "activations": 2, "top_k": 3, "delta_share": 0.0001,
           "seed_share": 0.0001, "arm": "treatment"}  # fmt: skip
AID = 2**23  # LB price-1 bin


def settings(mode: str = "pf", solver: str = "golden", k: int = 2, hops: int = 3) -> ma.Settings:
    """The probe's fixture settings: tolerance 1 bps (10**5 grid units), D = 10**9, R = 2, top-3,
    delta = max(1, A / 10**4), seed 10**-4."""
    return ma.Settings(
        sp.Settings(solver, 2, 10**9, 10**5, 60), mode, k, 3, Fraction(1, 10**4),
        Fraction(1, 10**4), hops,
    )  # fmt: skip


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


def cl_pool(pid: str, token0: str, token1: str) -> CL:
    return CL(
        pool_id=pid, source_key="uniswap_v3", token0=token0, token1=token1, fee=3000,
        tick_spacing=60, sqrt_price_x96=1 << 96, tick=0, liquidity=10**9, fee_protocol=0,
        fee_growth_global0_x128=0, fee_growth_global1_x128=0, protocol_fees0=0, protocol_fees1=0,
        bitmap_word_range=(-1, 1),
    )  # fmt: skip


class Run:
    """The probe's `run_case` on the runtime seams: E1 (`polish_plan`) then the activation stage
    on one ledger, inside a quote meter; `replay` is the independent evaluation of the final
    plan."""

    def __init__(
        self,
        b: SnapshotBundle,
        c: Case,
        plan: RoutePlan,
        st: ma.Settings,
        ledger: sp.Ledger | None = None,
    ) -> None:
        self.base = ok_eval(b, c, plan)
        self.ledger = ledger or sp.Ledger(None)
        self.published: list[RoutePlan] = []
        self.activation: dict[str, Any] | None = None
        with metered_quotes(None) as meter:
            self.e1 = sp.polish_plan(b, c, self.base, self.ledger, st.polish, self.published.append)
            self.incumbent = self.e1.incumbent
            if self.incumbent is not None and self.e1.truncated_by is None:
                self.e1_gross = self.incumbent.gross
                self.activation = ma.run_activation(
                    b, c, self.incumbent, self.ledger, st, ma.adjacency(b), O
                )
        self.seam = meter.counted
        self.final = plan if self.incumbent is None else self.incumbent.plan
        self.gross = self.base.gross_output if self.incumbent is None else self.incumbent.gross
        self.replay = evaluate(b, c, self.final, O)

    @property
    def log(self) -> list[list[Any]]:
        return [] if self.activation is None else self.activation["log"]

    @property
    def truncated_by(self) -> str | None:
        if self.activation is None:
            return self.e1.truncated_by
        reason: str | None = self.activation["truncated_by"]
        return reason


# ======================================================================================
# probe fixtures F10-F12, F14-F16 (contract §9), ported
# ======================================================================================

C4 = Case("cyc", "S", "T", 10000)
B4 = bundle(
    [CP("sx", "S", "X", 100000, 100000, 0), CP("xy", "X", "Y", 1000000, 10000, 0),
     CP("yt", "Y", "T", 1000, 100000, 0), CP("sy", "S", "Y", 1000, 1000000, 0),
     CP("xt", "X", "T", 10000, 1000000, 0)], C4,
)  # fmt: skip
PLAN_ACTIVE = RoutePlan(
    (step("sx", "S", "X", "REQUEST", 3000, "A"), step("xy", "X", "Y", "A", "ALL_REMAINING", "B"),
     step("yt", "Y", "T", "B", "ALL_REMAINING", "C"), step("sy", "S", "Y", "REQUEST", 7000, "D"),
     step("yt", "Y", "T", "D", "ALL_REMAINING", "E"))
)  # fmt: skip
PLAN_DORMANT = RoutePlan(
    (step("sx", "S", "X", "REQUEST", 0, "A"), step("xy", "X", "Y", "A", 0, "B"),
     step("yt", "Y", "T", "B", 0, "C"), step("sy", "S", "Y", "REQUEST", 10000, "D"),
     step("yt", "Y", "T", "D", "ALL_REMAINING", "E"))
)  # fmt: skip


def incumbent_of(b: SnapshotBundle, c: Case, plan: RoutePlan) -> sp.Incumbent:
    """The probe's `Holder` + `rebuild` of a plan's canonical form."""
    ev = ok_eval(b, c, plan)
    canon, _ = sp.canonical_from_evaluation(ev)
    incumbent = sp.Incumbent(canon, ev.gross_output, None, None)  # type: ignore[arg-type]
    sp.rebuild(incumbent, c)
    return incumbent


@pytest.mark.parametrize("mode", ma.MODES)
def test_f10_a_union_with_a_reverse_edge_is_rejected_as_dag_cycle(mode: str) -> None:
    incumbent = incumbent_of(B4, C4, PLAN_ACTIVE)
    path = (("sy", "S", "Y"), ("xy", "Y", "X"), ("xt", "X", "T"))  # Y -> X beside X -> Y
    assert ma.union(incumbent, path, "__T", Fraction(1, 10**4), mode) == "dag_cycle"
    assert ma.union(incumbent, (("sy", "S", "Y"), ("yt", "Y", "T")), "__T", Fraction(1, 10**4),
                    mode) != "dag_cycle"  # fmt: skip


# (mode, solver) -> (final gross, first-hop flow): the probe's values (fixtures4.log for full +
# golden; the other three from the probe's `run_case` on the same fixture).
F11 = {("full", "golden"): (1089662, 9059), ("pf", "golden"): (1089662, 9059),
       ("full", "brent"): (1089663, 9101), ("pf", "brent"): (1089663, 9101)}  # fmt: skip


@pytest.mark.parametrize(("mode", "solver"), sorted(F11))
def test_f11_the_former_cycle_scenario_is_valid_end_to_end(mode: str, solver: str) -> None:
    """The dormant S->X->Y->T branch is dropped by canonicalisation, so S->Y->X->T is admissible;
    the activation replays exactly, never worse, calling `sy` a second time (sequentially)."""
    run = Run(B4, C4, PLAN_DORMANT, settings(mode, solver))
    final, flow = F11[(mode, solver)]
    assert run.base.gross_output == 99890 and run.e1_gross == 99890
    assert run.replay.status is EvalStatus.OK and run.replay.gross_output == run.gross == final
    assert run.log == [
        [0, 0, f"accepted novel_pools=2 reuse=shared_sequential flow={flow}"],
        [1, -1, "stop_no_candidate"],
    ]
    assert run.replay.route_features["repeated_pool_calls"] == 1  # sy: shared_sequential
    assert run.seam == run.ledger.used and run.published[-1] == run.final


def test_f12_fresh_fund_prefixes_do_not_collide_with_existing_ids() -> None:
    plan = RoutePlan(
        (step("sy", "S", "Y", "REQUEST", 10000, "__ACT0_0OUT"),
         step("yt", "Y", "T", "__ACT0_0OUT", "ALL_REMAINING", "E"))
    )  # fmt: skip
    incumbent = incumbent_of(B4, C4, plan)
    prefix = ma.fresh_prefix(incumbent.topo, 0)
    assert prefix == "__ACT0_1"
    assert not any(f.startswith(prefix) for f in incumbent.topo.fund_token)
    built = ma.union(incumbent, (("xt", "X", "T"),), prefix, Fraction(1, 10**4), "pf")
    assert not isinstance(built, str) and len(set(built[0].fund_token)) == 4  # REQUEST + 3 outs


@pytest.mark.parametrize(
    ("amount", "parts"),
    [(100000, (50000, 50000)), (100003, (33335, 33334, 33334)), (99991, (7, 49992, 49992))],
)
def test_f14_the_pf_zero_endpoint_reproduces_the_original_plan_exactly(
    amount: int, parts: tuple[int, ...]
) -> None:
    c = Case("pf", "S", "T", amount)
    b = bundle([CP(p, "S", "T", 10**9, 10**9, 0) for p in "abcd"], c)
    pools = "abc"[: len(parts)]
    plan = RoutePlan(
        tuple(step(p, "S", "T", "REQUEST", a, p.upper()) for p, a in zip(pools, parts, strict=True))
    )
    ev = ok_eval(b, c, plan)
    incumbent = incumbent_of(b, c, plan)
    built = ma.union(incumbent, (("d", "S", "T"),), "__N", Fraction(1, 10**4), "pf")
    assert not isinstance(built, str)
    topo, seeded = built
    original = incumbent.shares["REQUEST"]
    assert seeded["REQUEST"] == [x * (1 - Fraction(1, 10**4)) for x in original] + [
        Fraction(1, 10**4)
    ]
    end = {**seeded, "REQUEST": [x * 1 for x in original] + [Fraction(0)]}  # tt = 0
    sim = sp.simulate(b, topo, end, amount, sp.Ledger(None).quote)
    assert sim.gross == ev.gross_output and sp.canonical_plan(topo, sim.alloc) == incumbent.plan


C7 = Case("retain", "S", "T", 300)
B7 = bundle(
    [CP("a", "S", "T", 1000, 2000, 0), CP("b", "S", "T", 1000, 100, 0),
     CP("c", "S", "T", 1000, 100, 0)], C7,
)  # fmt: skip
P7 = RoutePlan(tuple(step(p, "S", "T", "REQUEST", 100, p.upper()) for p in ("a", "b", "c")))


def test_f15_the_work_target_ledger_never_exceeds_its_target() -> None:
    """Mutation (e): without the `work_target` check the polish spends past 11."""
    ledger = ma.ControlLedger(None, None, used=10, target=11)
    incumbent = incumbent_of(B7, C7, P7)
    with pytest.raises(sp.PolishStop) as stop:
        sp.polish(B7, C7, incumbent, ledger.quote, settings().polish, {})
    assert stop.value.reason == "work_target" and ledger.used == 11


class TimeAt(sp.Ledger):
    """The probe's `TimeAt`: a time stop at the `at`-th quote."""

    def __init__(self, at: int) -> None:
        super().__init__(None)
        self.at = at

    def quote(self, state: Any, token_in: str, amount: int) -> Any:
        if self.used >= self.at:
            raise sp.PolishStop("time")
        return super().quote(state, token_in, amount)


@pytest.mark.parametrize("mode", ma.MODES)
def test_f16_a_time_stop_is_reported_as_time_and_the_incumbent_is_valid_at_133_cuts(
    mode: str,
) -> None:
    full = Run(B4, C4, PLAN_DORMANT, settings(mode))
    bad = []
    for at in range(1, 400, 3):  # 133 cut points, through E1, activation and its validation
        run = Run(B4, C4, PLAN_DORMANT, settings(mode), TimeAt(at))
        if (
            run.truncated_by not in (None, "time")
            or (run.truncated_by is None) != (at >= full.ledger.used)
            or run.replay.status is not EvalStatus.OK
            or run.replay.gross_output != run.gross
            or run.gross < run.base.gross_output
            or run.seam != run.ledger.used
        ):
            bad.append((at, run.truncated_by, run.replay.status))
    assert bad == []
    outcomes = Counter(entry[2] for at in (60, 100, 140) for entry in
                       Run(B4, C4, PLAN_DORMANT, settings(mode), TimeAt(at)).log)  # fmt: skip
    assert full.ledger.used < 400 and full.log[0][2].startswith("accepted")
    assert outcomes  # cuts inside the activation stage keep their log


# ======================================================================================
# admission and acceptance: one test per rule (each fails when that rule alone is removed)
# ======================================================================================

CA = Case("adm", "S", "T", 10**6)
BA = bundle(
    [CP("sx", "S", "X", 10**8, 10**8, 30), CP("xy", "X", "Y", 10**8, 10**8, 30),
     CP("yt", "Y", "T", 10**8, 10**8, 30), CP("sy", "S", "Y", 10**7, 4 * 10**7, 30),
     CP("xt", "X", "T", 10**7, 3 * 10**7, 30), CP("st", "S", "T", 10**7, 10**7, 30)], CA,
)  # fmt: skip
PA = RoutePlan(
    (step("sx", "S", "X", "REQUEST", 10**6, "A"), step("xy", "X", "Y", "A", "ALL_REMAINING", "B"),
     step("yt", "Y", "T", "B", "ALL_REMAINING", "C"))
)  # fmt: skip


@pytest.mark.parametrize("mode", ma.MODES)
def test_the_invalid_best_candidate_is_refused_and_the_valid_second_best_is_activated(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation (a): the best terminal S->Y->X->T reverses the plan's X->Y, so the union's token
    graph has a cycle: it is refused before any seed or quote (no simulation of a cyclic union),
    and the second-best S->Y->T is activated."""
    cyclic: list[int] = []
    real = sp.simulate

    def spy(b: SnapshotBundle, topo: sp.Topology, shares: sp.Shares, a: int, q: Any) -> Any:
        edges: dict[str, set[str]] = {}
        for s in topo.steps:
            edges.setdefault(s.token_in, set()).add(s.token_out)
        cyclic.append(_token_cycle(edges) is not None)
        return real(b, topo, shares, a, q)

    monkeypatch.setattr(sp, "simulate", spy)
    run = Run(BA, CA, PA, settings(mode, "brent", k=1))
    incumbent = run.incumbent
    assert incumbent is not None
    post = real(BA, incumbent.topo, incumbent.shares, CA.amount_in, sp.Ledger(None).quote)
    assert run.log == [
        [0, 0, "dag_cycle"],
        [0, 1, "accepted novel_pools=1 reuse=shared_sequential flow=1000000"],
    ]
    assert [(s.pool_id, s.token_in, s.token_out) for s in run.final.steps] == [
        ("sy", "S", "Y"), ("yt", "Y", "T")
    ]  # fmt: skip
    assert run.replay.status is EvalStatus.OK and run.replay.gross_output == run.gross
    assert run.gross > run.e1_gross == run.base.gross_output == 962329
    assert not any(cyclic)
    assert post.gross == run.gross  # the rebuilt incumbent simulates to its own gross


def test_the_ranked_terminals_of_the_admission_fixture() -> None:
    """The label search behind the admission fixture: the cyclic path ranks first."""
    incumbent = incumbent_of(BA, CA, PA)
    ledger = sp.Ledger(None)
    post = sp.simulate(BA, incumbent.topo, incumbent.shares, CA.amount_in, ledger.quote)
    found = ma.top_paths(BA, ma.adjacency(BA), post.states, CA, 100, ledger.quote, 3, 3)
    assert found == [
        (1208, (("sy", "S", "Y"), ("xy", "Y", "X"), ("xt", "X", "T"))),
        (389, (("sy", "S", "Y"), ("yt", "Y", "T"))),
        (290, (("sx", "S", "X"), ("xt", "X", "T"))),
    ]


def test_an_equal_gross_activation_with_positive_flow_is_not_accepted() -> None:
    """Mutation (b): zero-fee LB bins at price 1 make every split of the input give exactly the
    input, so the PF seed (positive flow on the new branch) ties the incumbent. Only a strictly
    higher gross is accepted, so the plan stays the base's."""
    c = Case("tie", "S", "T", 40000)
    b = bundle([lb_pool("p", {AID: (0, 10**6)}, 0), lb_pool("q", {AID: (0, 10**6)}, 0)], c)
    plan = RoutePlan((step("p", "S", "T", "REQUEST", 40000, "P"),))
    for mode in ma.MODES:
        run = Run(b, c, plan, settings(mode))
        assert run.log == [[0, 0, "no_gain_or_zero_flow"], [0, 1, "no_gain_or_zero_flow"],
                           [0, -1, "stop_no_candidate"]]  # fmt: skip
        assert run.final == plan and run.gross == run.base.gross_output == 40000
        assert run.incumbent is not None and run.incumbent.source == "base"


def test_a_replay_that_differs_from_the_simulated_gross_is_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation (c): a simulator that overstates every union by one raw unit. The charged
    evaluator replay disagrees, the candidate is `replay_mismatch` and the E1 plan is kept."""
    real = sp.simulate

    def inflated(b: SnapshotBundle, topo: sp.Topology, shares: sp.Shares, a: int, q: Any) -> Any:
        sim = real(b, topo, shares, a, q)
        branch = any(s.output_fund_id.startswith("__ACT") for s in topo.steps)
        if sim.gross is None or not branch:
            return sim
        return sp.Simulation(sim.gross + 1, sim.alloc, sim.produced, sim.states)

    monkeypatch.setattr(sp, "simulate", inflated)
    run = Run(B4, C4, PLAN_DORMANT, settings("pf"), sp.Ledger(None))
    assert run.log == [[0, 0, "replay_mismatch"], [0, -1, "stop_no_candidate"]]
    assert run.final == run.e1.incumbent.plan and run.gross == run.e1_gross == 99890  # type: ignore[union-attr]
    assert run.replay.status is EvalStatus.OK and run.seam == run.ledger.used


def test_the_acceptance_replay_is_charged_to_the_attempt_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replays: list[Any] = []
    real = evaluate  # the evaluator `marginal_activation` calls

    def spy(b: SnapshotBundle, c: Case, plan: RoutePlan, objective: Any, **kw: Any) -> Any:
        replays.append(kw.get("quote"))
        return real(b, c, plan, objective, **kw)

    monkeypatch.setattr(ma, "evaluate", spy)
    ledger = sp.Ledger(None)
    run = Run(B4, C4, PLAN_DORMANT, settings("pf"), ledger)
    assert len(replays) == 1 and replays[0] == ledger.quote and run.seam == ledger.used


def test_a_gain_without_positive_first_hop_flow_is_not_an_activation() -> None:
    """Mutation (d): a candidate whose gross is higher (here: a strictly better complete plan) but
    whose new branch carries no first-hop input is refused without a replay; the same candidate
    with flow on the branch is accepted."""
    incumbent = incumbent_of(B7, C7, P7)  # gross 199
    better = RoutePlan((step("a", "S", "T", "REQUEST", 300, "A"),))  # gross 461, no branch
    candidate = sp.Incumbent(better, 461, None, None)  # type: ignore[arg-type]
    ledger = sp.Ledger(None)
    assert ma.accepts(B7, C7, incumbent, candidate, "__ACT0_0", ledger.quote, O) == (
        "no_gain_or_zero_flow", 0,
    )  # fmt: skip
    assert ledger.used == 0  # refused before the replay
    branch = RoutePlan((step("a", "S", "T", "REQUEST", 300, "__ACT0_0OUT"),))
    candidate = sp.Incumbent(branch, 461, None, None)  # type: ignore[arg-type]
    assert ma.accepts(B7, C7, incumbent, candidate, "__ACT0_0", ledger.quote, O) == ("accept", 300)
    assert ledger.used == 1  # the charged replay


def test_post_plan_states_are_the_evaluator_sequential_states() -> None:
    """The label search quotes on `simulate`'s post-plan states: for a plan that uses one pool
    twice (`shared_sequential`) they are exactly the evaluator's `next_states`."""
    c = Case("seq", "S", "T", 8 * 10**6)
    b = bundle([lb_pool("lb", {AID - k: (0, 10**6) for k in range(10)}),
                CP("cp", "S", "T", 10**7, 10**7, 30)], c)  # fmt: skip
    plan = RoutePlan(
        (step("lb", "S", "T", "REQUEST", 10**6, "L1"),
         step("cp", "S", "T", "REQUEST", 6 * 10**6, "C"),
         step("lb", "S", "T", "REQUEST", 10**6, "L2"))
    )  # fmt: skip
    ev = ok_eval(b, c, plan)
    assert ev.route_features["repeated_pool_calls"] == 1
    incumbent = incumbent_of(b, c, plan)
    sim = sp.simulate(b, incumbent.topo, incumbent.shares, c.amount_in, sp.Ledger(None).quote)
    assert sim.gross == ev.gross_output and sim.states == ev.next_states


# ======================================================================================
# controls (§5.3)
# ======================================================================================


@pytest.mark.parametrize("invocations", [0, 1, 2, 3, 5])
def test_the_call_matched_control_runs_exactly_the_treatment_invocations(
    invocations: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation (f): exactly `invocations` full E1 polish calls, started and completed, even when
    a call improves nothing (no convergence stop), and no quote cap of its own."""
    calls: list[int] = []
    real = sp.polish

    def spy(*args: Any) -> None:
        calls.append(1)
        real(*args)

    snapshot = ma.Snapshot.of(incumbent_of(B7, C7, P7), sp.Ledger(None, used=5))
    monkeypatch.setattr(sp, "polish", spy)
    treatment = {"invocations": invocations, "quotes": 1}
    incumbent, record = ma.run_control(
        B7, C7, snapshot, "call_matched", treatment, None, None, settings().polish
    )
    assert len(calls) == record["calls_started"] == record["calls_completed"] == invocations
    assert record["target_calls"] == invocations and record["stop"] is None
    if invocations:  # the first call reaches 461, later calls improve nothing and still run
        assert record["quotes"] > invocations and int(record["gross"]) == 461
    assert evaluate(B7, C7, incumbent.plan, O).gross_output == int(record["gross"])


@pytest.mark.parametrize("target", [0, 1, 7, 40, 155, 10**6])
def test_the_work_matched_control_never_exceeds_its_target(target: int) -> None:
    """Mutation (e) through `run_control`: the control's ledger stops at `work_target` (or the
    control converges first); `quotes <= target` always, and the incumbent stays valid."""
    c, b = C7, B7
    ev = ok_eval(b, c, P7)
    snap_ledger = sp.Ledger(None, used=17)
    canon, _ = sp.canonical_from_evaluation(ev)
    snapshot = ma.Snapshot.of(incumbent_of(b, c, P7), snap_ledger)
    assert snapshot.plan == canon
    incumbent, record = ma.run_control(
        b, c, snapshot, "work_matched", {"invocations": 99, "quotes": target}, None, None,
        settings().polish,
    )  # fmt: skip
    assert record["quotes"] <= target and record["target_quotes"] == target
    assert (record["stop"] == "work_target") == (not record["converged"])
    rep = evaluate(b, c, incumbent.plan, O)
    assert rep.status is EvalStatus.OK and rep.gross_output == int(record["gross"]) >= 199
    if target == 10**6:
        assert record["converged"] and int(record["gross"]) == 461


def test_a_control_respects_the_global_cap_and_its_own_wall_allowance() -> None:
    snapshot = ma.Snapshot.of(incumbent_of(B7, C7, P7), sp.Ledger(None, used=50))
    _, capped = ma.run_control(B7, C7, snapshot, "call_matched", {"invocations": 3, "quotes": 0},
                               60, None, settings().polish)  # fmt: skip
    assert capped["stop"] == "max_quotes" and capped["quotes"] == 10
    assert capped["calls_started"] == 1 and capped["calls_completed"] == 0
    _, timed = ma.run_control(B7, C7, snapshot, "work_matched", {"invocations": 0, "quotes": 99},
                              None, 0.0, settings().polish)  # fmt: skip
    assert timed["stop"] == "time" and timed["quotes"] == 0 and timed["allowance_seconds"] == 0.0


# ======================================================================================
# solve(): the base strategy, the shared ledger, arms, statuses, quote-cap sweeps
# ======================================================================================

CS = Case("ig", "S", "T", 10**6)
BS = bundle(
    [CP("p", "S", "T", 10**6, 3 * 10**6, 30), CP("q", "S", "T", 10**6, 2 * 10**6, 30),
     CP("x", "S", "X", 10**6, 10**6, 30), CP("y", "X", "T", 10**6, 3 * 10**6, 30),
     CP("r", "S", "T", 10**5, 4 * 10**5, 30)], CS,
)  # fmt: skip
PARAMS = {"max_hops": 2, "max_splits": 2, "percent_step": 25, "chunks": 4}


def prepared(b: SnapshotBundle = BS, **options: Any) -> ma.PreparedMarginalActivation:
    return ma.prepare(b, AlgorithmConfig(NAME, PARAMS, {**NOMINEE, **options}))


def solve(
    case: Case = CS,
    budget: Budget | None = None,
    prep: ma.PreparedMarginalActivation | None = None,
    b: SnapshotBundle = BS,
    objective: Any = O,
) -> tuple[SolveResult, list[RoutePlan], int]:
    sink: list[RoutePlan] = []
    context = SolveContext(b, objective, prep or prepared(b), candidate_sink=sink.append)
    with metered_quotes(None if budget is None else budget.max_quotes) as meter:
        result = ma.solve(case, context, budget or Budget())
    return result, sink, meter.counted


def base_solve(case: Case = CS, b: SnapshotBundle = BS) -> tuple[SolveResult, int]:
    from routing.algorithms import incremental_graph

    prep = incremental_graph.prepare(b, AlgorithmConfig("incremental_graph", PARAMS))
    with metered_quotes(None) as meter:
        result = incremental_graph.solve(case, SolveContext(b, O, prep), Budget())
    return result, meter.counted


@pytest.mark.parametrize("mode", ma.MODES)
def test_solve_activates_after_e1_and_the_ledger_equals_the_seam(mode: str) -> None:
    base, base_quotes = base_solve()
    result, published, seam = solve(prep=prepared(mode=mode))
    record = result.search_stats[NAME]
    assert result.algorithm == NAME and result.status is SolveStatus.OK
    assert record["scope"] == "fixed_funding_topology" and record["truncated_by"] is None
    assert record["not_reached"] is None and result.search_stats["base"]["quotes"] == base_quotes
    assert seam == base_quotes + record["quotes"]  # one ledger, every quote charged once
    assert record["quotes"] == record["e1"]["quotes"] + record["activation"]["quotes"]
    rep = evaluate(BS, CS, result.plan, O)  # type: ignore[arg-type]
    assert rep.status is EvalStatus.OK and str(rep.gross_output) == record["gross"]
    assert int(record["gross"]) > int(record["e1"]["gross"]) > base.score  # type: ignore[operator]
    assert [e[2].split()[0] for e in record["activation"]["log"]] == ["accepted", "accepted"]
    assert published[-1] == result.plan and result.evaluation is None
    assert record["activation"]["invocations"] == 2
    json.dumps(result.to_dict())  # JSON-serialisable record


@pytest.mark.parametrize("mode", ma.MODES)
def test_quote_cap_sweep_every_cut_is_valid_never_worse_and_ledger_equals_seam(mode: str) -> None:
    """§9: every cap from the base's own quotes to past the full run: status `ok` (never
    `no_route`), an evaluator-valid plan whose gross is the recorded one, never worse than the
    base or a smaller cap's E1 stage, the ledger equal to the seam, and the E1-truncated rows
    labelled and never activated."""
    base, base_quotes = base_solve()
    prep = prepared(mode=mode)
    full, _, full_seam = solve(prep=prep)
    e1_end = base_quotes + full.search_stats[NAME]["e1"]["quotes"]
    reasons: Counter[str | None] = Counter()
    for cap in range(base_quotes, full_seam + 2):
        result, published, seam = solve(budget=Budget(max_quotes=cap), prep=prep)
        record = result.search_stats[NAME]
        assert result.status is SolveStatus.OK and result.plan is not None and seam <= cap
        assert seam == base_quotes + record["quotes"]
        rep = evaluate(BS, CS, result.plan, O)
        assert rep.status is EvalStatus.OK and str(rep.gross_output) == record["gross"]
        assert rep.gross_output >= base.score  # type: ignore[operator]
        truncated_e1 = cap < e1_end
        assert (record["not_reached"] == ma.NOT_REACHED) == truncated_e1
        assert ("activation" in record) == (not truncated_e1)
        if not truncated_e1:
            assert rep.gross_output >= int(record["e1"]["gross"])
        assert record["truncated_by"] == (None if cap >= full_seam else "max_quotes")
        reasons[record["truncated_by"]] += 1
        if published:
            assert published[-1] == result.plan or result.plan == base.plan
    assert reasons["max_quotes"] == full_seam - base_quotes and reasons[None] == 2


@pytest.mark.parametrize("mode", ma.MODES)
def test_control_arms_match_the_treatment_at_every_cap(mode: str) -> None:
    """§5.3 at every cap: each control arm's embedded (uncharged) treatment equals the treatment
    arm's own record, the work-matched control never spends more than the treatment's activation
    stage, the call-matched control starts exactly the treatment's invocations unless its budget
    ends first, its record counts only base + E1 + its own quotes, and E1-truncated rows are
    labelled in every arm."""
    _, base_quotes = base_solve()
    treat = prepared(mode=mode)
    controls = {arm: prepared(mode=mode, arm=arm) for arm in ("work_matched", "call_matched")}
    _, _, full_seam = solve(prep=treat)
    stride = 1 if mode == "pf" else 7
    checked = Counter[str]()
    for cap in [*range(base_quotes, full_seam + 2, stride), full_seam + 1, None]:
        budget = Budget(max_quotes=cap)
        t_result, _, _ = solve(budget=budget, prep=treat)
        t = t_result.search_stats[NAME]
        for arm, prep in controls.items():
            result, published, seam = solve(budget=budget, prep=prep)
            record = result.search_stats[NAME]
            assert result.status is SolveStatus.OK and result.plan is not None
            assert seam == base_quotes + record["quotes"] and (cap is None or seam <= cap)
            rep = evaluate(BS, CS, result.plan, O)
            assert rep.status is EvalStatus.OK and str(rep.gross_output) == record["gross"]
            assert record["not_reached"] == t["not_reached"] and record["e1"] == t["e1"]
            if t["not_reached"] is not None:
                assert "control" not in record and "activation" not in record
                checked["not_reached"] += 1
                continue
            reference, control = record["activation"], record["control"]
            assert reference["charged"] is False
            assert {k: v for k, v in reference.items() if k != "charged"} == t["activation"]
            assert record["quotes"] == record["e1"]["quotes"] + control["quotes"]
            assert record["truncated_by"] == control["stop"]
            if arm == "work_matched":
                assert control["quotes"] <= t["activation"]["quotes"] == control["target_quotes"]
                assert control["stop"] in (None, "work_target")  # the target binds first
                assert (control["stop"] is None) == control["converged"]
            else:
                started = control["calls_started"]
                assert control["target_calls"] == t["activation"]["invocations"]
                if control["stop"] is None:
                    assert started == control["calls_completed"] == control["target_calls"]
                else:
                    assert control["stop"] == "max_quotes" and started <= control["target_calls"]
                    assert control["calls_completed"] == started - 1
            checked[arm] += 1
    assert checked["work_matched"] > 10 and checked["call_matched"] > 10
    assert checked["not_reached"] > 0


def test_budget_zero_returns_the_base_result_literally_in_every_arm() -> None:
    base, base_quotes = base_solve()
    for arm in ma.ARMS:
        result, _, seam = solve(budget=Budget(max_quotes=base_quotes), prep=prepared(arm=arm))
        record = result.search_stats[NAME]
        assert record["truncated_by"] == "max_quotes" and record["quotes"] == 0
        assert record["not_reached"] == ma.NOT_REACHED and seam == base_quotes
        assert (result.status, result.plan, result.evaluation, result.score) == (
            base.status, base.plan, base.evaluation, base.score
        )  # fmt: skip


def test_a_time_stop_is_ok_and_never_no_route() -> None:
    for arm in ma.ARMS:
        result, _, _ = solve(budget=Budget(time_limit_seconds=1e-9), prep=prepared(arm=arm))
        record = result.search_stats[NAME]
        assert result.status is SolveStatus.OK and record["truncated_by"] == "time"
        assert record["not_reached"] == ma.NOT_REACHED


def test_a_non_gross_objective_is_unsupported_and_the_base_never_runs() -> None:
    result, published, seam = solve(objective=synthetic_fixed_cost(1))
    assert result.status is SolveStatus.UNSUPPORTED and result.plan is None
    assert result.search_stats[NAME]["scope"] == "objective" and seam == 0 and published == []


def test_base_statuses_pass_through() -> None:
    unreachable = Case("none", "S", "Z", 10**6)
    result, _, _ = solve(unreachable)
    base, _ = base_solve(unreachable)
    assert result.status is base.status is SolveStatus.NO_ROUTE
    assert result.error == base.error and result.search_stats[NAME]["scope"] is None


def test_an_unsupported_topology_returns_the_base_result_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    for arm in ma.ARMS:
        options = {**NOMINEE, "arm": arm}
        prep = ma.PreparedMarginalActivation(
            "incremental_graph", None, options, ma.Settings.from_options(options, 3), {}
        )
        result, _, seam = solve(c, prep=prep, b=b)
        record = result.search_stats[NAME]
        assert result.status is SolveStatus.OK and result.plan == plan and result.evaluation == ev
        assert record["scope"] == "unsupported_topology" and seam == 0
        assert "activation" not in record and record["not_reached"] is None


def test_options_are_validated_all_required_and_exact() -> None:
    factory = ALGORITHMS[NAME]
    assert validated_options(factory, NOMINEE) == NOMINEE
    st = ma.Settings.from_options(NOMINEE, 3)
    assert st == settings("pf", "brent", 2, 3)
    assert st.delta(10**6) == 100 and st.delta(19999) == 1 and st.delta(3) == 1
    for bad in (
        {},
        {k: v for k, v in NOMINEE.items() if k != "arm"},
        {k: v for k, v in NOMINEE.items() if k != "maxiter"},
        {**NOMINEE, "extra": 1},
        {**NOMINEE, "mode": "both"},
        {**NOMINEE, "activations": 0},
        {**NOMINEE, "activations": True},
        {**NOMINEE, "top_k": 0},
        {**NOMINEE, "delta_share": 0},
        {**NOMINEE, "delta_share": float("inf")},
        {**NOMINEE, "seed_share": 0.6},
        {**NOMINEE, "arm": "control"},
        {**NOMINEE, "base": "metis_inspired"},
        {**NOMINEE, "seed": 1},
        {**NOMINEE, "controls": "work"},
    ):
        with pytest.raises(OptionsError):
            validated_options(factory, bad)
    with pytest.raises(OptionsError):  # the public prepare validates too
        ma.prepare(BS, AlgorithmConfig(NAME, PARAMS, {}))


def test_registered_as_a_custom_profile_selected_identity() -> None:
    factory = ALGORITHMS[NAME]
    assert factory is ma.FACTORY and list(ALGORITHMS).count(NAME) == 1
    assert list(ALGORITHMS)[-2:] == ["split_polish", NAME]
    assert NAME not in BASE_STRATEGIES and NAME not in OPTIMIZED_STRATEGIES
    assert strategy_group(NAME) == "custom" and factory.options_preset is None
    assert factory.search_params == ("max_hops", "max_splits", "percent_step")
    assert factory.graph_params == ("chunks",)
    assert "NOT Jupiter Metis" in str(factory.provenance)
    for name in ("daily_gross.yaml", "full_gross.yaml", "daily.yaml", "full.yaml"):
        source = read_profile_document(REPO / "config" / name)
        for mode in ("all", "base", "optimized", "profile"):
            document, profile = derive(source, mode, source_path=name, source_sha256="x")
            assert NAME not in profile.algorithms and NAME not in json.dumps(document)
    doc = read_profile_document(REPO / "config" / "full_gross.yaml")
    doc["algorithms"] = [NAME]
    with pytest.raises(Exception, match="requires algorithm_options.marginal_activation"):
        parse_profile(doc, "p")


def test_importing_the_module_never_imports_scipy() -> None:
    import subprocess
    import sys

    code = "import sys, routing.algorithms.marginal_activation; print('scipy' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=REPO, check=True)  # fmt: skip
    assert out.stdout.strip() == "False"


# ======================================================================================
# CLI: run, quote, replay, order checks, report label
# ======================================================================================


def _profile(tmp_path: Path, order: str = "fixed", repeats: int = 1, name: str = "p",
             **options: Any) -> Path:  # fmt: skip
    doc = read_profile_document(REPO / "config" / "daily_gross.yaml")
    doc["algorithms"] = ["incremental_graph", NAME]
    doc["search"] = {"max_hops": 2, "max_splits": 4, "percent_step": 5}
    doc["graph"] = {"chunks": 20}
    doc["algorithm_options"] = {NAME: {**NOMINEE, **options}}
    doc["measurement"] = {"warmup": 0, "repeats": repeats, "seed": 7, "order": order,
                          "memory_pass": False}  # fmt: skip
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    parse_profile(doc, str(path))
    return path


def _run(profile: Path, results: Path) -> Path:
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
    run_dir = _run(_profile(tmp_path, repeats=2), results)  # 2 attempts: consistent
    manifest = load_manifest(run_dir)
    assert list(manifest.algorithms) == ["incremental_graph", NAME]
    resolved = manifest.resolved_profile
    assert resolved["algorithm_options"][NAME]["options"] == NOMINEE
    assert resolved["algorithm_options"][NAME]["source"] == {"kind": "override"}
    assert "NOT Jupiter Metis" in resolved["algorithm_config"][NAME]["provenance"]["label"]
    by = {(r["algorithm"], r["case_id"]): r for r in load_case_records(run_dir)}
    activated = 0
    for case_id in manifest.measurement["case_order"]:
        base, record = by[("incremental_graph", case_id)], by[(NAME, case_id)]
        assert record["status"] == base["status"]
        if base["status"] != "ok":
            continue
        assert int(record["score"]) >= int(base["score"])
        search = record["search"][NAME]
        assert search["gross"] == record["score"] and search["not_reached"] is None
        assert record["quotes"]["counted"] == record["search"]["base"]["quotes"] + search["quotes"]
        assert record["measurement"]["attempts_consistent"] is True
        activated += any(e[2].startswith("accepted") for e in search["activation"]["log"])
    assert activated >= 1
    capsys.readouterr()
    replay = manifest.replay_command.split("main.py", 1)[1].split()
    assert replay[-2:] == ["--strategies", "profile"]
    assert main.main(replay) == 0
    (again,) = [p for p in results.iterdir() if p != run_dir]
    assert load_manifest(again).resolved_profile == resolved
    assert compare_runs(run_dir, again) == []
    out = tmp_path / "report"
    assert main.main(["report", str(run_dir), "--output", str(out)]) == 0
    html = (out / "report.html").read_text()
    assert "Experimental marginal activation" in html and "NOT Jupiter Metis" in html


def test_cli_runs_a_control_arm_with_its_own_ledger(tmp_path: Path) -> None:
    """The campaign's control arm through the CLI: the record's quote count is base + E1 + the
    control's own spend; the treatment reference is recorded uncharged."""
    results = tmp_path / "results"
    treat = load_case_records(_run(_profile(tmp_path, name="t"), results))
    work = load_case_records(_run(_profile(tmp_path, name="w", arm="work_matched"), results))
    t_by = {r["case_id"]: r for r in treat if r["algorithm"] == NAME}
    matched = 0
    for record in (r for r in work if r["algorithm"] == NAME and r["status"] == "ok"):
        search, t = record["search"][NAME], t_by[record["case_id"]]["search"][NAME]
        if search["not_reached"] is not None:
            continue
        control = search["control"]
        assert control["quotes"] <= t["activation"]["quotes"] == control["target_quotes"]
        assert record["quotes"]["counted"] == (
            record["search"]["base"]["quotes"] + search["e1"]["quotes"] + control["quotes"]
        )
        assert search["activation"]["log"] == t["activation"]["log"]
        matched += 1
    assert matched >= 1


@pytest.mark.parametrize("order", ["reverse", "shuffle"])
def test_reverse_and_shuffle_orders_give_identical_records(tmp_path: Path, order: str) -> None:
    results = tmp_path / "results"
    fixed = _run(_profile(tmp_path, name="fixed", mode="full"), results)
    other = _run(_profile(tmp_path, order=order, name=order, mode="full"), results)
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
# the worked example (docs/references/routing-algorithms.md §24.5)
# ======================================================================================

CD = Case("doc", "S", "T", 10**6)
BD = bundle([CP("p", "S", "T", 10**7, 10**7, 0), CP("q", "S", "T", 10**7, 10**7, 0)], CD)
PD = RoutePlan((step("p", "S", "T", "REQUEST", 10**6, "P"),))


def _cp(reserve: int, amount: int) -> int:
    return reserve * amount // (reserve + amount)  # fee-free CPMM, equal reserves


def test_the_documented_worked_example(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ok_eval(BD, CD, PD).gross_output == _cp(10**7, 10**6) == 909090
    # iteration 1: post-plan p = (10**7 + 10**6, 10**7 - 909090); delta = 10**6 // 10**4 = 100
    p_after = (10**7 + 10**6, 10**7 - 909090)
    assert p_after[1] * 100 // (p_after[0] + 100) == 82 and _cp(10**7, 100) == 99
    # the PF seed: p gets floor(10**6 * 9999/10000) = 999900, q the remainder 100
    assert _cp(10**7, 999900) + _cp(10**7, 100) == 909107
    seen: list[tuple[int, ...]] = []
    real_top = ma.top_paths

    def spy(*args: Any) -> Any:
        found = real_top(*args)
        seen.append(tuple(amount for amount, _ in found))
        return found

    monkeypatch.setattr(ma, "top_paths", spy)
    for mode, quotes, simulations in (("pf", 203, 72), ("full", 861, 305)):
        seen.clear()
        run = Run(BD, CD, PD, settings(mode, "brent"))
        assert run.ledger.used - run.activation["quotes"] == 1  # type: ignore[index]
        assert seen == [(99, 82), (90, 90)]  # q first; then both reuse a pool (shared_sequential)
        assert run.log == [
            [0, 0, "accepted novel_pools=1 reuse=none flow=500000"],
            [1, 0, "no_gain_or_zero_flow"], [1, 1, "no_gain_or_zero_flow"],
            [1, -1, "stop_no_candidate"],
        ]  # fmt: skip
        assert [(s.pool_id, s.inputs[0].amount) for s in run.final.steps] == [
            ("p", 500000), ("q", 500000)
        ]  # fmt: skip
        assert _cp(10**7, 500000) == 476190
        assert run.replay.gross_output == run.gross == 2 * _cp(10**7, 500000) == 952380
        assert run.activation["quotes"] == quotes  # type: ignore[index]
        assert run.activation["work"]["simulations"] == simulations  # type: ignore[index]
        assert run.activation["invocations"] == 3  # type: ignore[index]
        assert run.published == [run.final]
    # the controls from the E1 incumbent (the base plan: one consumer, nothing to split)
    snapshot = ma.Snapshot.of(incumbent_of(BD, CD, PD), sp.Ledger(None, used=1))
    for kind, calls in (("work_matched", 1), ("call_matched", 3)):
        _, control = ma.run_control(BD, CD, snapshot, kind, {"invocations": 3, "quotes": 203},
                                    None, None, settings("pf", "brent").polish)  # fmt: skip
        assert control["gross"] == "909090" and control["quotes"] == 0
        assert control["calls_started"] == calls and control["stop"] is None


def test_every_number_of_chapter_24_is_asserted() -> None:
    """`routing-algorithms.md` §24 publishes only numbers that this file asserts, that the
    contract states (tuning evidence), or the chapter's own option ranges and ids."""
    guide = (REPO / "docs" / "references" / "routing-algorithms.md").read_text(encoding="utf-8")
    chapter = guide[guide.index("## 24. ") :]
    chapter = re.sub(r"```.*?```", " ", chapter, flags=re.S)  # pseudocode and diagram
    chapter = re.sub(r"^#+ .*$", " ", chapter, flags=re.M)  # headings
    contract = (REPO / "docs" / "references" / "research-023" / "contract.md").read_text()
    backed = Path(__file__).read_text(encoding="utf-8") + contract + " 1000000000 1623 1624 1.0 "
    number = r"(?<![\w.])\d+(?:\.\d+)?(?![\w])"
    published = {n for n in re.findall(number, chapter) if len(n) >= 3 or "." in n}
    assert len(published) >= 15  # not vacuous
    assert sorted(n for n in published if n not in set(re.findall(number, backed))) == []


# ======================================================================================
# tuning reproduction of the pinned PF nominee (acceptance 4), controls included
# ======================================================================================


def _plan_json(plan: RoutePlan) -> list[dict[str, Any]]:
    return [{"pool": s.pool_id, "tin": s.token_in, "tout": s.token_out,
             "inputs": [[i.fund_id, i.amount] for i in s.inputs], "out": s.output_fund_id}
            for s in plan.steps]  # fmt: skip


def _merged(*works: dict[str, Any]) -> dict[str, Any]:
    """The probe's one `stats` dict of E1 + activation (E1's polish call + the invocations)."""
    out: dict[str, Any] = {}
    for work in works:
        for key in ("accepted", "brent_nfev", "golden_iters"):
            if key in work:
                out[key] = out.get(key, 0) + work[key]
        for status, n in work.get("brent_status", {}).items():
            statuses = out.setdefault("brent_status", {})
            statuses[status] = statuses.get(status, 0) + n
    out["polish_calls"] = works[0].get("polish_calls", 0) + works[1].get("invocations", 0)
    return out


def test_the_pinned_pf_nominee_is_reproduced_on_the_tuning_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`incremental_graph` c50 (`config/full_gross.yaml`) + the PF nominee on all 96 tuning cases
    equals `probe/results/ig_actpf.json.gz` case by case: canonical plan, gross, E1 gross and
    quotes, total quotes, base quotes and gross, truncation, the activation log, the search
    counters, and both controls run from the same E1 incumbent (gross, quotes, calls, stop,
    convergence, target). The probe charges everything to `CAP_RESIDUAL - base quotes`, i.e. the
    one ledger shared with the base."""
    path = os.environ.get(TUNING_ENV)
    if not path:
        pytest.skip(f"set {TUNING_ENV} to the frozen bundle_tuning (data/ is primary-clone only)")
    assert Path(path).is_dir(), f"{TUNING_ENV}={path!r} is not a directory"
    b = load_bundle(path)
    assert len(b.cases) == 96
    with gzip.open(PROBE_RESULTS / "ig_actpf.json.gz") as fh:
        pinned = {r["case"]: r for r in json.load(fh)["rows"]}
    profile = read_profile_document(REPO / "config" / "full_gross.yaml")
    params = {**profile["search"], **profile["graph"]}
    cap = profile["budget"]["max_quotes"]
    budget = Budget(profile["budget"]["time_limit_seconds"], cap)
    prep = ma.prepare(b, AlgorithmConfig(NAME, params, NOMINEE))
    snapshots: list[ma.Snapshot] = []
    real = ma.run_activation

    def spy(bb: Any, case: Case, incumbent: sp.Incumbent, ledger: sp.Ledger, *a: Any) -> Any:
        snapshots.append(ma.Snapshot.of(incumbent, ledger))
        return real(bb, case, incumbent, ledger, *a)

    monkeypatch.setattr(ma, "run_activation", spy)
    differences: list[tuple[str, str]] = []
    improved = activated = 0
    started = time.monotonic()
    for case in b.cases:
        snapshots.clear()
        result, _, seam = solve(case, budget, prep, b)
        record, base = result.search_stats[NAME], result.search_stats["base"]
        assert result.status is SolveStatus.OK and result.plan is not None
        rep = evaluate(b, case, result.plan, O)
        assert rep.status is EvalStatus.OK and str(rep.gross_output) == record["gross"]
        assert seam == base["quotes"] + record["quotes"] <= cap
        p = pinned[case.case_id]
        act = record["activation"]
        got: dict[str, Any] = {
            "plan": _plan_json(sp.canonical_from_evaluation(rep)[0]), "final": rep.gross_output,
            "q_total": record["quotes"], "q_e1": record["e1"]["quotes"],
            "e1_gross": int(record["e1"]["gross"]), "base_quotes": base["quotes"],
            "recorded": int(record["base_gross"]), "truncated_by": record["truncated_by"],
            "act_log": act["log"], "stats": _merged(record["e1"]["work"], act["work"]),
        }  # fmt: skip
        (snapshot,) = snapshots
        controls = {}
        for kind, name in (("call_matched", "calls"), ("work_matched", "work")):
            _, c = ma.run_control(b, case, snapshot, kind, act, cap, None, prep.settings.polish)
            controls[name] = {
                "gross": int(c["gross"]), "q": c["quotes"], "calls": c["calls_started"],
                "stop": c["stop"], "converged": c["converged"], "target": c["target_quotes"],
            }  # fmt: skip
        got["controls"] = controls
        want = {k: p[k] for k in got}
        want["stats"] = {k: v for k, v in p["stats"].items()}
        differences += [(case.case_id, k) for k in got if got[k] != want[k]]
        improved += rep.gross_output > int(record["base_gross"])
        activated += any(e[2].startswith("accepted") for e in act["log"])
    print(f"TUNING REPRODUCTION ig_actpf: 96 cases, {improved} improved, {activated} activated, "
          f"{time.monotonic() - started:.0f} s, differences {differences}")  # fmt: skip
    assert differences == [] and improved == 85

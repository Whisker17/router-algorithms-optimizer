"""WHI-1557 `cfmm_dual` contract checks (docs/references/research-021/cfmm-dual.md).

Evidence classes are kept apart and labeled:

1. **Author reference** -- `tests/fixtures/cfmm/author_reference.json`, produced OFFLINE by
   the pinned, unmodified CFMMRouter.jl (`tools/upstream/cfmm/generate.jl`). The tests
   check that this repository's executable model (`cfmm_contract_model.py`) reproduces the
   author's per-market oracle (CPMM and aggregate Uniswap v3) and dual function, and
   record what the author code does and does not return.
2. **Python model reference** -- `tests/fixtures/cfmm/model_reference.json`, our model plus
   SciPy L-BFGS-B run in an ephemeral environment. It is NOT author execution; every
   stored dual point is re-verified here (value, residual, termination) without SciPy.
3. **Independent integer expectations** -- hand CPMM formula brute force, the real
   evaluator's replay and hand-built continuous flows for the recovery counterexamples.

Nothing here certifies a bound: continuous convergence and optimizer success are shown
not to imply integer optimality.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import importlib.util
import json
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from functools import cache
from pathlib import Path
from typing import Any

import cfmm_contract_model as m
import pytest

from benchmark.objective import gross_only
from pools.constant_product import get_amount_out
from pools.quote import quote_exact_in
from pools.result import QuoteStatus
from routing.evaluator import EvalStatus, evaluate
from routing.plan import REQUEST_FUND_ID
from routing.search import QuoteCache, build_graph_index, enumerate_paths, path_plan
from snapshot.bundle import load_bundle
from snapshot.models import (
    BlockRef,
    Case,
    ConcentratedPoolState,
    ConstantProductPoolState,
    LiquidityBookPoolState,
    PoolState,
    SnapshotBundle,
)

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "cfmm"
UPSTREAM = ROOT / "tools" / "upstream" / "cfmm"
DOC = ROOT / "docs" / "references" / "research-021" / "cfmm-dual.md"
R021 = json.loads((ROOT / "docs/references/research-021/contract-v1.json").read_text())
R021_PROSE = (ROOT / "docs/references/research-021/contract.md").read_text()
INPUTS = json.loads((FIX / "author_inputs.json").read_text())
AUTHOR = json.loads((FIX / "author_reference.json").read_text())
MODEL = json.loads((FIX / "model_reference.json").read_text())
MANTLE = ROOT / "tests" / "fixtures" / "routing" / "mantle_mixed"
BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)

PIN = "5932e42e5077ffc7d8e02c3b3ad2e9ed1d441267"
PIN_TREE = "dbf991b18897abdcca08647dd753a955f7a477f6"
LICENSE_SHA256 = "99056ed306835ccac7874395818f302f264d91d6f13f28ca6b88440e8cbd042e"
PAPER_PDF_SHA256 = "8b26956fb768240791081189506448a70c646aa6bc23bdfa7d5fc700899a5ed0"
RESERVED = (
    "max_hops max_splits percent_step chunks label_hops label_pruning time_limit_seconds "
    "max_quotes max_candidates shortlist sampling controls recipe objective seed"
).split()


def _doc_contract() -> dict[str, Any]:
    text = DOC.read_text()
    match = re.search(r"<!-- cfmm-dual-contract -->\s*```json\n(.*?)\n```", text, re.S)
    assert match, "cfmm-dual.md must embed the machine-readable contract block"
    parsed: dict[str, Any] = json.loads(match.group(1))
    return parsed


CONTRACT = _doc_contract()
PRESET: dict[str, Any] = CONTRACT["preset"]


def _rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(a), abs(b), 1e-300)


def _net_bundle(doc: Mapping[str, Any]) -> tuple[SnapshotBundle, Case]:
    pools: dict[str, PoolState] = {
        p["pool_id"]: ConstantProductPoolState(
            p["pool_id"], p["token0"], p["token1"], p["reserve0"], p["reserve1"], p["fee_bps"]
        )
        for p in doc["pools"]
    }
    case = Case(doc["id"], doc["token_in"], doc["token_out"], doc["amount_in"])
    bundle = SnapshotBundle("cfmm", "synthetic", 1, BLOCK, pools, (case,), "fixture", "<t>")
    return bundle, case


def _router_input(cid: str) -> dict[str, Any]:
    found: dict[str, Any] = next(c for c in INPUTS["router"] if c["id"] == cid)
    return found


def _author(section: str, cid: str) -> dict[str, Any]:
    found: dict[str, Any] = next(c for c in AUTHOR[section] if c["id"] == cid)
    return found


def _model(cid: str) -> dict[str, Any]:
    found: dict[str, Any] = next(c for c in MODEL["cases"] if c["id"] == cid)
    return found


@cache
def _mantle() -> SnapshotBundle:
    return load_bundle(MANTLE)


def _trades(rows: Sequence[Mapping[str, Any]]) -> list[m.Trade]:
    return [
        m.Trade(r["pool_id"], r["token_in"], r["token_out"], r["amount_in"], r["amount_out"])
        for r in rows
    ]


def _options(**over: Any) -> m.RecoveryOptions:
    base: dict[str, Any] = {
        "min_split_share": PRESET["min_split_share"],
        "max_recovery_attempts": PRESET["max_recovery_attempts"],
        "cycle_resolve": PRESET["cycle_resolve"],
    }
    base.update(over)
    return m.RecoveryOptions(**base)


# ------------------------------------------------------------------ 1. pins and notices


def test_author_reference_is_the_pinned_offline_run() -> None:
    prov = AUTHOR["provenance"]
    assert prov["cfmmrouter"]["git_revision"] == PIN
    assert prov["cfmmrouter"]["repo"] == "https://github.com/bcc-research/CFMMRouter.jl"
    assert prov["threads"] == 1 and prov["julia"] == "1.10.10"
    assert (prov["lbfgsb"], prov["l_bfgs_b_jll"]) == ("0.4.1", "3.0.1+0")
    manifest = (UPSTREAM / "Manifest.toml").read_text()
    block = manifest.split("[[deps.CFMMRouter]]", 1)[1].split("[[", 1)[0]
    assert f'repo-rev = "{PIN}"' in block and f'git-tree-sha1 = "{PIN_TREE}"' in block
    lic = (UPSTREAM / "LICENSE-CFMMRouter.jl.txt").read_bytes()
    assert hashlib.sha256(lic).hexdigest() == LICENSE_SHA256
    assert b"Copyright (c) 2021 Guillermo Angeris, Theo Diamandis" in lic
    notice = (UPSTREAM / "NOTICE.md").read_text()
    doc = DOC.read_text()
    for text in (notice, doc):
        assert PIN in text and "2302.04938v1" in text and PAPER_PDF_SHA256 in text


def test_model_reference_is_labeled_python_not_author() -> None:
    assert "NOT author execution" in MODEL["_comment"]
    for path, digest in MODEL["environment"]["sources_sha256"].items():
        # bound to the exact generator/model/input bytes: regenerate after any change
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path
    env = MODEL["environment"]
    assert (env["scipy"], env["numpy"]) == ("1.18.1", "2.5.3")
    assert MODEL["preset"] == {k: v for k, v in PRESET.items() if k in MODEL["preset"]}


# ------------------------------------------------------- 2. author oracle and dual function


@pytest.mark.parametrize("case", INPUTS["cpmm_oracle"], ids=lambda c: c["id"])
def test_cpmm_oracle_matches_author(case: dict[str, Any]) -> None:
    pool = ConstantProductPoolState("p", "A", "B", case["R"][0], case["R"][1], case["fee_bps"])
    trade = m.cpmm_arb(pool, {"A": case["v"][0], "B": case["v"][1]})
    ref = _author("cpmm_oracle", case["id"])
    ours = [0.0, 0.0, 0.0, 0.0]  # delta_A, delta_B, lambda_A, lambda_B
    if trade is not None:
        i = 0 if trade.token_in == "A" else 1
        ours[i], ours[3 - i] = trade.amount_in, trade.amount_out
    for got, want in zip(ours, ref["delta"] + ref["lambda"], strict=True):
        assert (got == want == 0) or _rel(got, want) < 1e-12


def _cl_states() -> dict[str, ConcentratedPoolState]:
    real = _mantle().pools["0x4cdfc22bf05209de87ee564746dc7e5174631d2b"]
    assert isinstance(real, ConcentratedPoolState)
    return {
        "synthetic": m.synthetic_cl(),
        "synthetic_missing_tick": m.synthetic_cl(missing_tick_data=True),
        "real_uniswap_v3_usdt_wmnt": real,
    }


@pytest.mark.parametrize("case", INPUTS["univ3_oracle"], ids=lambda c: c["id"])
def test_cl_aggregate_oracle_matches_author_univ3(case: dict[str, Any]) -> None:
    state = _cl_states()[case["state"]]
    trade = m.cl_arb(
        state, m.cl_ladder(state), {state.token0: case["v"][0], state.token1: case["v"][1]}
    )
    ref = _author("univ3_oracle", case["id"])
    ours = [0.0, 0.0, 0.0, 0.0]
    if trade is not None:
        i = 0 if trade.token_in == state.token0 else 1
        ours[i], ours[3 - i] = trade.amount_in, trade.amount_out
    for got, want in zip(ours, ref["delta"] + ref["lambda"], strict=True):
        assert (got == want == 0) or _rel(got, want) < 1e-9
    assert (trade is None) == case["id"].endswith("-band")


@pytest.mark.parametrize("case", INPUTS["router"], ids=lambda c: c["id"])
def test_dual_function_matches_author_at_the_author_point(case: dict[str, Any]) -> None:
    """At the author's own final nu (normalized so nu_out = 1: g is homogeneous of degree
    one), our g, trades and gradient equal the author's dual value, trades and net flows."""
    bundle, c = _net_bundle(case)
    ref = _author("router", case["id"])
    tokens = case["tokens"]
    v_out = ref["v"][tokens.index(c.token_out)]
    nu = {t: v / v_out for t, v in zip(tokens, ref["v"], strict=True) if t != c.token_out}
    problem = m.dual_problem(bundle, c, tuple(bundle.pools))
    ev = m.dual_value(problem, nu)
    assert _rel(ev.value * v_out, ref["dual_value"]) < 1e-9
    trade_by_pool = {t.pool_id: t for t in ev.trades}
    for row, pool in zip(ref["trades"], case["pools"], strict=True):
        got = trade_by_pool.get(pool["pool_id"])
        tendered = max(row["delta"]) if max(row["delta"]) > 0 else 0.0
        assert (got is None and tendered == 0.0) or (
            got is not None and _rel(got.amount_in, tendered) < 1e-9
        )
    for t, flow in zip(tokens, ref["netflows"], strict=True):
        if t == c.token_out:
            continue
        want = flow + (c.amount_in if t == c.token_in else 0.0)
        terms = [c.amount_in] + [
            x.amount_in if x.token_in == t else x.amount_out
            for x in ev.trades
            if t in (x.token_in, x.token_out)
        ]
        assert abs(ev.gradient[t] - want) <= 1e-12 * max(terms)  # cancellation-aware


def test_author_route_returns_no_status_and_its_point_can_be_infeasible() -> None:
    """Audit of the author interface: `route!` returns `nothing` (no convergence status,
    no iteration count); with its defaults the returned point may spend more input than
    exists or borrow an intermediate token, so its completion proves nothing."""
    assert {r["route_returned"] for r in AUTHOR["router"]} == {"nothing"}
    tiny, moe = _author("router", "r-tiny"), _author("router", "r-moe-usdc-usdt")
    assert tiny["netflows"][0] < -10 * _router_input("r-tiny")["amount_in"]  # spends > 10x A
    assert moe["netflows"][2] < -1e11  # WMNT net outflow: borrowed intermediate
    capped = _author("router", "r-grid38-maxiter1")
    assert abs(capped["netflows"][0] + 38) > 5  # one iteration: far from conservation
    assert 59.36 < _author("router", "r-grid38")["dual_value"] < 59.37


# ------------------------------------------------ 3. Python model reference, re-verified


@pytest.mark.parametrize("case", MODEL["cases"], ids=lambda c: c["id"])
def test_model_reference_points_reverify_offline(case: dict[str, Any]) -> None:
    if case["universe"] == "mantle_mixed_cpmm_h3":
        bundle = _mantle()
        c = bundle.case(case["id"])
        assert tuple(case["markets"]) == m.market_universe(bundle, c, 3, [m.CPMM])
    else:
        bundle, c = _net_bundle(_router_input(case["id"].removesuffix("-maxiter2")))
    sol = case["solution"]
    problem = m.dual_problem(bundle, c, case["markets"])
    sigma = m.scales(problem)
    assert sigma == pytest.approx(sol["sigma"], rel=1e-15)
    phi, grad, ev = m.log_objective(problem, sigma, sol["u"])
    bound = PRESET["log_price_bound"]
    residual = m.projected_residual(sol["u"], grad, -bound, bound)
    assert residual == pytest.approx(sol["residual"], rel=1e-9, abs=1e-15)
    assert ev.value == pytest.approx(sol["value"], rel=1e-12)
    converged = residual <= PRESET["residual_tolerance"]
    assert (sol["termination"] == "converged") == converged
    if not converged:
        assert sol["termination"] in ("not_converged", "iteration_cap")


@pytest.mark.parametrize("cid", ["r-grid38", "r-triangle", "r-cycle", "r-deadend"])
def test_model_optimum_agrees_with_author_where_author_converged(cid: str) -> None:
    ours, ref = _model(cid)["solution"], _author("router", cid)
    v_out = ref["v"][_router_input(cid)["tokens"].index(_router_input(cid)["token_out"])]
    assert ours["termination"] == "converged"
    assert _rel(ours["value"], ref["dual_value"] / v_out) < 1e-6


def test_model_converges_where_author_defaults_did_not() -> None:
    """Scale-free log-price parametrization (§5.2): on the real 6/6/18-decimal Moe
    triangle our point is stationary to the preset tolerance, while the author's raw-unit
    run of the same network stops with a unit-free residual above 10% (its WMNT net flow
    is a borrowed 2e11 raw units)."""
    doc = _router_input("r-moe-usdc-usdt")
    bundle, c = _net_bundle(doc)
    ours = _model("r-moe-usdc-usdt")["solution"]
    problem = m.dual_problem(bundle, c, tuple(bundle.pools))
    sigma = m.scales(problem)
    ref = _author("router", "r-moe-usdc-usdt")
    v_out = ref["v"][doc["tokens"].index(c.token_out)]
    x_author = [
        math.log(ref["v"][doc["tokens"].index(v)] / v_out / sigma[v]) for v in problem.variables
    ]
    author_residual = max(abs(g) for g in m.log_objective(problem, sigma, x_author)[1])
    assert ours["termination"] == "converged" and ours["residual"] <= PRESET["residual_tolerance"]
    assert author_residual > 0.1


# ----------------------------------------------------------- 4. recovery: exact integer plans


def _check_plan_invariants(bundle: SnapshotBundle, case: Case, rec: m.Recovery) -> int:
    """Independent replay by the real evaluator plus the §6.5 invariants."""
    assert rec.plan is not None and rec.failure is None
    ev = evaluate(bundle, case, rec.plan, gross_only())  # fresh, unmemoized replay
    assert ev.status is EvalStatus.OK, ev.error
    assert not ev.residuals  # full source allocation, zero intermediate residual
    request = next(f for f in ev.funds if f.fund_id == REQUEST_FUND_ID)
    assert request.consumed == case.amount_in
    pools = [s.pool_id for s in rec.plan.steps]
    assert len(pools) == len(set(pools))  # one merged step per market
    assert all(t.amount_out > 0 and t.amount_in > 0 for t in ev.trace)  # no dust legs
    assert all(
        isinstance(r.amount, int) or r.amount == "ALL_REMAINING"
        for s in rec.plan.steps
        for r in s.inputs
    )
    return ev.gross_output


def _stored_recovery(
    cid: str, **over: Any
) -> tuple[SnapshotBundle, Case, m.Recovery, dict[str, Any]]:
    case_doc = _model(cid)
    if case_doc["universe"] == "mantle_mixed_cpmm_h3":
        bundle = _mantle()
        c = bundle.case(cid)
    else:
        bundle, c = _net_bundle(_router_input(cid.removesuffix("-maxiter2")))
    sol = case_doc["solution"]
    resolved = case_doc["resolves"]

    def resolve(allowed: Mapping[str, str]) -> list[m.Trade]:
        assert resolved and list(allowed) == resolved[0]["markets"]
        return _trades(resolved[0]["trades"])

    rec = m.recover(
        bundle,
        c,
        case_doc["markets"],
        _trades(sol["trades"]),
        sol["nu"],
        _options(**over),
        QuoteCache(bundle),
        resolve,
    )
    return bundle, c, rec, case_doc


@pytest.mark.parametrize("cid", [c["id"] for c in MODEL["cases"]])
def test_recovery_reproduces_the_stored_plan_and_replays(cid: str) -> None:
    bundle, c, rec, doc = _stored_recovery(cid)
    stored = doc["recovery"]
    assert rec.failure == stored["failure"] is None
    assert rec.cycle_removed == stored["cycle_removed"]
    assert [list(p) for p in rec.pruned] == stored["pruned"]
    assert [(f.edge.pool_id, str(f.amount_in), str(f.amount_out)) for f in rec.flows] == [
        (f["pool_id"], f["amount_in"], f["amount_out"]) for f in stored["flows"]
    ]
    assert str(_check_plan_invariants(bundle, c, rec)) == stored["gross"]


def _brute_triangle(amount: int) -> int:
    """Every integer plan of the r-triangle DAG (S->T direct, S->M, M->T over two pools),
    scored with the hand `getAmountOut` formula: independent of the model and evaluator."""
    best = 0
    for x in range(amount + 1):
        direct = get_amount_out(x, 1000, 1000, 30) if x else 0
        m_in = get_amount_out(amount - x, 1000, 2100, 30) if amount - x else 0
        if (x and direct == 0) or (amount - x and m_in == 0):
            continue
        for y in range(m_in + 1):
            a = get_amount_out(y, 2000, 1000, 30) if y else 0
            b = get_amount_out(m_in - y, 500, 260, 5) if m_in - y else 0
            if (y and a == 0) or (m_in - y and b == 0):
                continue
            best = max(best, direct + a + b)
    return best


def test_continuous_convergence_does_not_imply_integer_optimality() -> None:
    """r-triangle: the converged continuous point recovers 137, a two-iteration capped
    point recovers 138 = the integer optimum (hand brute force), and all stay below the
    continuous estimate. Neither the optimizer's success nor the estimate certifies
    anything."""
    optimum = _brute_triangle(150)
    converged = int(_model("r-triangle")["recovery"]["gross"])
    capped = int(_model("r-triangle-maxiter2")["recovery"]["gross"])
    estimate = _model("r-triangle")["solution"]["value"]
    assert (converged, capped) == (137, 138)
    assert converged < capped == optimum < math.floor(estimate) == 139
    assert _model("r-triangle-maxiter2")["solution"]["termination"] == "iteration_cap"


def test_grid38_recovers_the_raw_integer_optimum_of_r6() -> None:
    raw = max(
        (get_amount_out(x, 134, 190, 30) if x else 0)
        + (get_amount_out(38 - x, 76, 172, 30) if x < 38 else 0)
        for x in range(39)
    )
    assert raw == 59 == int(_model("r-grid38")["recovery"]["gross"])
    assert 59.36 < _model("r-grid38")["solution"]["value"] < 59.37


def test_tiny_order_is_not_converged_but_recovers_the_stepwise_plan() -> None:
    doc = _model("r-tiny")
    assert doc["solution"]["termination"] == "not_converged"  # estimate must be omitted
    assert int(doc["recovery"]["gross"]) == 99  # R021 R5 stepwise integer value
    assert 198.40 < doc["solution"]["value"]  # the dual value is not an integer bound claim


def test_cycle_is_broken_then_resolved_on_the_restricted_dag() -> None:
    doc = _model("r-cycle")
    assert doc["recovery"]["cycle_removed"] == ["ab2"] and doc["recovery"]["resolved"]
    assert doc["resolves"][0]["markets"] == ["sa", "ab1", "bt", "at"]
    # the estimate (full network, arbitrage loop included) exceeds the acyclic plan
    assert int(doc["recovery"]["gross"]) < doc["solution"]["value"]


def test_cycle_break_without_resolve_is_a_valid_projection() -> None:
    bundle, c, rec, _ = _stored_recovery("r-cycle", cycle_resolve=False)
    assert rec.cycle_removed == ["ab2"] and not rec.resolved
    assert _check_plan_invariants(bundle, c, rec) > 0


def _synthetic(
    *pools: PoolState, amount: int, token_in: str = "S", token_out: str = "T"
) -> tuple[SnapshotBundle, Case]:
    case = Case("x", token_in, token_out, amount)
    return SnapshotBundle(
        "x", "synthetic", 1, BLOCK, {p.pool_id: p for p in pools}, (case,), "h", "<t>"
    ), case


def _cp(pid: str, t0: str, t1: str, r0: int, r1: int, fee: int = 30) -> ConstantProductPoolState:
    return ConstantProductPoolState(pid, t0, t1, r0, r1, fee)


def test_surplus_intermediate_is_routed_on_not_disposed() -> None:
    """The continuous point leaves M in surplus (100 in, 60 out). Share projection sends
    M's whole exact inflow onward; nothing is stranded or donated."""
    bundle, case = _synthetic(
        _cp("sm", "S", "M", 10**6, 10**6), _cp("mt", "M", "T", 10**6, 10**6), amount=1000
    )
    trades = [m.Trade("sm", "S", "M", 1000.0, 100.0), m.Trade("mt", "M", "T", 60.0, 59.0)]
    rec = m.recover(
        bundle, case, ["sm", "mt"], trades, {"S": 1.0, "M": 1.0}, _options(), QuoteCache(bundle)
    )
    gross = _check_plan_invariants(bundle, case, rec)
    assert rec.flows[1].amount_in == rec.flows[0].amount_out and gross > 0


def test_disconnected_and_dead_end_markets_are_excluded() -> None:
    bundle, case = _synthetic(
        _cp("st", "S", "T", 10**6, 10**6),
        _cp("sx", "S", "X", 10**6, 10**6),
        _cp("xy", "X", "Y", 10**6, 10**6),
        amount=1000,
    )
    trades = [
        m.Trade("st", "S", "T", 900.0, 890.0),
        m.Trade("sx", "S", "X", 100.0, 99.0),
        m.Trade("xy", "X", "Y", 99.0, 98.0),
    ]
    rec = m.recover(
        bundle,
        case,
        ["st", "sx", "xy"],
        trades,
        {"S": 1.0, "X": 1.0, "Y": 1.0},
        _options(),
        QuoteCache(bundle),
    )
    assert rec.support == ("st",)
    assert _check_plan_invariants(bundle, case, rec) == get_amount_out(1000, 10**6, 10**6, 30)
    assert m.market_universe(bundle, case, 3, [m.CPMM]) == ("st",)


def test_tiny_amount_dust_leg_is_pruned_then_retried() -> None:
    """A 1-unit order split over two pools: the floored share leg is 0 (not emitted); a
    leg whose exact output floors to 0 is pruned and the projection retried."""
    bundle, case = _synthetic(_cp("a", "S", "T", 1000, 400), _cp("b", "S", "T", 134, 190), amount=3)
    trades = [m.Trade("a", "S", "T", 2.0, 1.9), m.Trade("b", "S", "T", 1.0, 1.3)]
    rec = m.recover(bundle, case, ["a", "b"], trades, {"S": 1.0}, _options(), QuoteCache(bundle))
    assert rec.pruned == [("a", QuoteStatus.INSUFFICIENT_OUTPUT_AMOUNT.value)] and rec.attempts == 2
    assert _check_plan_invariants(bundle, case, rec) == get_amount_out(3, 134, 190, 30)
    lone, lone_case = _synthetic(_cp("a", "S", "T", 1000, 1000), amount=1)
    failed = m.recover(
        lone,
        lone_case,
        ["a"],
        [m.Trade("a", "S", "T", 1.0, 0.9)],
        {"S": 1.0},
        _options(),
        QuoteCache(lone),
    )
    assert failed.failure == "support_exhausted" and failed.plan is None


def test_recovery_caps_and_failure_categories() -> None:
    bundle, case, _, doc = _stored_recovery("r-triangle")
    trades, nu = _trades(doc["solution"]["trades"]), doc["solution"]["nu"]
    budget = m.recover(
        bundle, case, doc["markets"], trades, nu, _options(max_quotes=1), QuoteCache(bundle)
    )
    assert budget.failure == "quote_budget" and budget.plan is None
    b2, c2 = _synthetic(_cp("a", "S", "T", 1000, 400), _cp("b", "S", "T", 134, 190), amount=3)
    t2 = [m.Trade("a", "S", "T", 2.0, 1.9), m.Trade("b", "S", "T", 1.0, 1.3)]
    capped = m.recover(
        b2, c2, ["a", "b"], t2, {"S": 1.0}, _options(max_recovery_attempts=1), QuoteCache(b2)
    )
    assert capped.failure == "attempts_exhausted" and capped.attempts == 1
    empty = m.recover(b2, c2, ["a", "b"], [], {"S": 1.0}, _options(), QuoteCache(b2))
    assert empty.failure == "empty_support" and empty.attempts == 0
    nan = m.recover(
        b2,
        c2,
        ["a", "b"],
        [m.Trade("a", "S", "T", math.nan, 1.0)],
        {"S": 1.0},
        _options(),
        QuoteCache(b2),
    )
    assert nan.failure == "numeric_failure"
    no_resolve = m.recover(
        *_net_bundle(_router_input("r-cycle")),
        _model("r-cycle")["markets"],
        _trades(_model("r-cycle")["solution"]["trades"]),
        _model("r-cycle")["solution"]["nu"],
        _options(),
        QuoteCache(_net_bundle(_router_input("r-cycle"))[0]),
        lambda _a: None,
    )
    assert no_resolve.failure == "resolve_failed"


def test_recovery_is_deterministic() -> None:
    first = _stored_recovery("usdc_usdt_small")[2]
    second = _stored_recovery("usdc_usdt_small")[2]
    assert first.plan == second.plan and first.flows == second.flows
    assert len({f.edge.token_in for f in first.flows}) == 2  # a real split and a real hop


def test_real_moe_triangle_uses_multi_hop_and_split() -> None:
    bundle, c, rec, _ = _stored_recovery("usdc_usdt_small")
    assert rec.plan is not None
    tokens = {(s.token_in, s.token_out) for s in rec.plan.steps}
    assert len(rec.plan.steps) == 3 and len(tokens) == 3
    single = [
        evaluate(bundle, c, path_plan(c, p), gross_only()).gross_output
        for p in enumerate_paths(build_graph_index(bundle), c.token_in, c.token_out, 3)
        if all(isinstance(bundle.pools[e.pool_id], ConstantProductPoolState) for e in p)
    ]
    assert _check_plan_invariants(bundle, c, rec) > max(single)


# ------------------------------------------------------ 4b. evaluation budget (§5.3)


def _grid38_problem() -> m.DualProblem:
    bundle, c = _net_bundle(_router_input("r-grid38"))
    return m.dual_problem(bundle, c, tuple(bundle.pools))


def test_guard_refuses_the_evaluation_beyond_the_cap_mid_line_search() -> None:
    """An optimizer that keeps evaluating inside one line search (what SciPy does past
    `maxfun`) is stopped at exactly the cap; repeats are cached, the final point costs
    nothing extra, and the lowest-Phi point is reported."""
    problem = _grid38_problem()
    sigma = m.scales(problem)
    budget = m.EvaluationBudget(3)
    guard = m.GuardedObjective(problem, sigma, budget)
    trial = [[0.0], [0.0], [0.1], [0.05], [0.02], [0.01]]  # x0 twice, then a line search
    seen: list[float] = []
    with pytest.raises(m.EvaluationCapReached):
        for x in trial:
            seen.append(guard(x)[0])
    assert (budget.used, guard.evaluations, len(seen)) == (3, 3, 4) and guard.fired
    assert budget.oracle_calls == 3 * len(problem.markets)
    x, phi, _, _ = guard.final([0.02])  # not evaluated and unaffordable -> best point
    assert phi == min(seen) and x in ([0.0], [0.1], [0.05]) and budget.used == 3
    x_cached, _, _, _ = guard.final([0.1])  # evaluated -> reused, uncharged
    assert x_cached == [0.1] and budget.used == 3


def test_guard_budget_is_shared_with_the_resolve_and_final_is_charged_once() -> None:
    problem = _grid38_problem()
    sigma = m.scales(problem)
    budget = m.EvaluationBudget(4)
    first = m.GuardedObjective(problem, sigma, budget)
    first([0.0])
    first([0.2])
    _, _, _, _ = first.final([0.3])  # affordable and new -> charged exactly once
    assert budget.used == 3 and first.evaluations == 3
    second = m.GuardedObjective(problem, sigma, budget)  # the re-solve: remainder only
    second([0.0])  # a new guard has its own cache: charged again, no reset
    assert budget.remaining == 0
    with pytest.raises(m.EvaluationCapReached):
        second([0.4])
    with pytest.raises(m.EvaluationCapReached):
        m.GuardedObjective(problem, sigma, budget).final(None)


@pytest.mark.parametrize(
    "run", MODEL["forced_caps"]["runs"], ids=lambda r: f"{r['case']}-{r['cap_key']}-{r['cap']}"
)
def test_forced_caps_with_the_pinned_scipy_hold(run: dict[str, Any]) -> None:
    """Real SciPy 1.18.1 runs (model reference, not author): the guarded evaluations never
    exceed the cap (0c5890f made 4/4/5/5 for a cap of 1), oracle calls are exactly
    evaluations x |M|, iterations never exceed `max_iterations`, and the stored point
    re-verifies offline with a termination consistent with §5.4."""
    cap_evals = (
        run["cap"]
        if run["cap_key"] == "max_function_evaluations"
        else PRESET["max_function_evaluations"]
    )
    cap_iters = run["cap"] if run["cap_key"] == "max_iterations" else PRESET["max_iterations"]
    assert run["evaluations"] == run["budget_used"] <= cap_evals
    assert run["oracle_calls"] == run["evaluations"] * len(run["markets"])
    assert run["nit"] <= cap_iters and run["iterations_left"] == cap_iters - run["nit"]
    bundle, c = _net_bundle(_router_input(run["case"]))
    problem = m.dual_problem(bundle, c, run["markets"])
    _, grad, ev = m.log_objective(problem, m.scales(problem), run["u"])
    bound = PRESET["log_price_bound"]
    residual = m.projected_residual(run["u"], grad, -bound, bound)
    assert residual == pytest.approx(run["residual"], rel=1e-9, abs=1e-15)
    assert ev.value == pytest.approx(run["value"], rel=1e-12)
    if residual <= PRESET["residual_tolerance"]:
        assert run["termination"] == "converged"
    else:
        assert run["termination"] == "iteration_cap" and (
            run["guard_fired"] or run["scipy_status"] == 1
        )
    if run["cap_key"] == "max_function_evaluations" and run["cap"] == 1:
        assert run["guard_fired"] and run["evaluations"] == 1


def test_starved_resolve_is_skipped_not_refunded() -> None:
    """r-cycle with `max_function_evaluations` equal to what its initial solve uses: the
    cycle is broken, the re-solve is skipped (budget exhausted), and the projection of the
    cycle-broken support still yields a valid plan."""
    doc = MODEL["forced_caps"]["resolve_starved"]
    assert doc["budget_used"] == doc["cap"] == doc["budget"]["evaluations_used"]
    assert doc["resolves"] == [] and doc["recovery"]["resolve_skipped"]
    assert not doc["recovery"]["resolved"] and doc["recovery"]["cycle_removed"] == ["ab2"]
    bundle, c = _net_bundle(_router_input("r-cycle"))
    sol = doc["solution"]

    def starved(_allowed: Mapping[str, str]) -> list[m.Trade]:
        raise m.ResolveBudgetExhausted

    rec = m.recover(
        bundle,
        c,
        doc["markets"],
        _trades(sol["trades"]),
        sol["nu"],
        _options(),
        QuoteCache(bundle),
        starved,
    )
    assert (
        rec.resolve_skipped
        and str(_check_plan_invariants(bundle, c, rec)) == doc["recovery"]["gross"]
    )


# ----------------------------------------------------------------- 5. CL stage boundary


def test_cl_current_word_outside_the_collected_range_is_empty_in_both_directions() -> None:
    """Parent repro (comment 2104a98d): the current tick 100 (spacing 60) lies in word 0.
    Collected words (1, 1): going up reads word 0 -> unknown; (-2, -2): going down reads
    word 0 -> unknown. The exact quote is incomplete_snapshot, so the model must be empty."""
    base = m.synthetic_cl()
    above = dataclasses.replace(base, bitmap_word_range=(1, 1), tick_bitmap={}, ticks={})
    below = dataclasses.replace(base, bitmap_word_range=(-2, -2), tick_bitmap={}, ticks={})
    for state, token, zero_for_one in ((above, "T1", False), (below, "T0", True)):
        ladder = m.cl_ladder(state)
        assert (ladder.down if zero_for_one else ladder.up) == ()
        assert m.cl_forward(ladder, zero_for_one, 1_000_000.0) is None
        assert quote_exact_in(state, token, 1_000_000).status is QuoteStatus.INCOMPLETE_SNAPSHOT
        nu = {"T0": 0.5, "T1": 1.0} if zero_for_one else {"T0": 2.0, "T1": 1.0}
        assert m.cl_arb(state, ladder, nu) is None


@pytest.mark.parametrize("tick", [-15360, -60, 0, 100, 15240, 15300, 15359])
@pytest.mark.parametrize("words", [(-1, 0), (0, 0), (-1, -1), (1, 1), (-2, -2), (0, 1), (-2, -1)])
def test_cl_direction_known_iff_the_exact_swap_can_start(tick: int, words: tuple[int, int]) -> None:
    """Over word ranges around the current word and ticks at word edges (bit 0 and bit 255
    of a word), the continuous model answers (no `None`) exactly when the exact swap of a
    price-moving input does not fail with incomplete_snapshot; an empty direction never
    extrapolates the active liquidity."""
    from pools.cl_math import get_sqrt_ratio_at_tick

    state = dataclasses.replace(
        m.synthetic_cl(),
        tick=tick,
        sqrt_price_x96=get_sqrt_ratio_at_tick(tick) + 1,
        bitmap_word_range=words,
        tick_bitmap={},
        ticks={},
        liquidity=10**16,
    )
    ladder = m.cl_ladder(state)
    for token, zero_for_one in (("T0", True), ("T1", False)):
        exact = quote_exact_in(state, token, 10**6)
        model = m.cl_forward(ladder, zero_for_one, 1e6)
        assert (exact.status is QuoteStatus.INCOMPLETE_SNAPSHOT) == (model is None), (token, exact)
        if model is not None:
            assert exact.status is QuoteStatus.OK and exact.amount_out <= model + 1


def test_cl_known_range_boundaries() -> None:
    full, missing = (
        m.cl_ladder(m.synthetic_cl()),
        m.cl_ladder(m.synthetic_cl(missing_tick_data=True)),
    )
    assert (full.down_boundary, full.up_boundary) == ("collected_range", "collected_range")
    assert missing.down_boundary == "missing_tick_data"
    assert [s.liquidity for s in full.down] == [1e16, 0.0, 4e15, 0.0]  # empty range between
    assert [s.liquidity for s in missing.down] == [1e16, 0.0, 4e15]
    assert full.down[-1].end == pytest.approx(1.0001 ** (-15360 / 2), rel=1e-12)
    assert full.up[-1].end == pytest.approx(1.0001 ** (15300 / 2), rel=1e-12)


@pytest.mark.parametrize("source", ["uniswap_v3", "agni_v3", "fusionx_v3"])
@pytest.mark.parametrize("zero_for_one", [True, False])
def test_cl_exact_replay_never_exceeds_the_continuous_aggregate(
    source: str, zero_for_one: bool
) -> None:
    """Continuous interval math is an estimate; the exact source-specific swap (rounding,
    per-step fees, empty-range crossings) stays the only execution: within the known
    range exact <= continuous and within a relative 1e-9 (+2 raw units); beyond it the
    exact replay is INCOMPLETE_SNAPSHOT and the continuous model refuses to extrapolate."""
    state = m.synthetic_cl(source_key=source)
    ladder = m.cl_ladder(state)
    token_in = state.token0 if zero_for_one else state.token1
    for amount in (10**6, 10**12, 3 * 10**14, 4 * 10**14, 9 * 10**14):
        cont = m.cl_forward(ladder, zero_for_one, float(amount))
        exact = quote_exact_in(state, token_in, amount)
        if cont is None:
            assert exact.status is QuoteStatus.INCOMPLETE_SNAPSHOT
            continue
        assert exact.status is QuoteStatus.OK
        assert exact.amount_out <= cont * (1 + 1e-12) + 1
        assert exact.amount_out >= cont * (1 - 1e-9) - 2
    missing = m.synthetic_cl(missing_tick_data=True, source_key=source)
    assert m.cl_forward(m.cl_ladder(missing), True, 7 * 10**14) is None
    assert (
        quote_exact_in(missing, missing.token0, 7 * 10**14).status
        is QuoteStatus.INCOMPLETE_SNAPSHOT
    )


def test_cl_zero_output_leg_is_pruned() -> None:
    """A CL pool returns OK with amount_out 0 for a fee-only dust input; the recovery
    treats it as a zero-output leg (never a silent donation)."""
    state = m.synthetic_cl()
    assert quote_exact_in(state, state.token0, 1).amount_out == 0
    cp = _cp("cp", "T0", "T1", 10**18, 10**18)
    bundle, case = _synthetic(state, cp, amount=2, token_in="T0", token_out="T1")
    trades = [m.Trade(state.pool_id, "T0", "T1", 1.0, 0.9), m.Trade("cp", "T0", "T1", 1.0, 0.9)]
    rec = m.recover(
        bundle, case, [state.pool_id, "cp"], trades, {"T0": 1.0}, _options(), QuoteCache(bundle)
    )
    assert rec.pruned == [(state.pool_id, "zero_output")]
    assert _check_plan_invariants(bundle, case, rec) == 1


def test_liquidity_book_never_enters_a_cfmm_market_set() -> None:
    bundle = _mantle()
    for case in bundle.cases:
        for protocols in ([m.CPMM], [m.CPMM, m.CL]):
            markets = m.market_universe(bundle, case, 3, protocols)
            assert not any(isinstance(bundle.pools[p], LiquidityBookPoolState) for p in markets)
    lb = next(p for p in bundle.pools.values() if isinstance(p, LiquidityBookPoolState))
    assert not m.admitted(lb, [m.CPMM, m.CL, "liquidity_book"])


# ---------------------------------------------------------------- 6. the published contract


def _domain_hash(domain: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(domain, sort_keys=True).encode()).hexdigest()


def test_contract_block_uses_only_registered_vocabulary() -> None:
    assert CONTRACT["contract"] == "R021-C/1" and CONTRACT["identity"] == "cfmm_dual"
    assert CONTRACT["outcome"] in R021["research_outcomes"]
    assert set(CONTRACT["work_units"]) <= set(R021["work_units"])
    assert set(CONTRACT["terminations"]) <= set(R021["terminations"])
    assert set(CONTRACT["bound_kinds"]) == {"estimate", "unknown"}  # never certified
    domain = CONTRACT["example_domain"]
    assert set(domain) == set(R021["domain_fields"])
    assert domain["amount_grid"]["kind"] == "recovered_continuous"
    assert (
        domain["pool_reuse"] in R021["pool_reuse"]
        and domain["hops"]["param"] in R021["hops_params"]
    )
    assert domain["splits"]["governs"] in R021["splits_governs"]
    assert CONTRACT["example_domain_hash"] == _domain_hash(domain)


def test_option_schema_is_bounded_and_avoids_reserved_keys() -> None:
    for key in RESERVED:
        assert f"`{key}`" in R021_PROSE  # the list mirrors contract.md §9.1
    options = CONTRACT["options"]
    assert set(options) == set(PRESET)
    for key, spec in options.items():
        assert key not in RESERVED
        value = PRESET[key]
        if spec["type"] == "int":
            assert isinstance(value, int) and not isinstance(value, bool)
            assert spec["min"] <= value <= spec["max"] and isinstance(spec["max"], int)
        elif spec["type"] == "float":
            assert isinstance(value, float) and math.isfinite(value)
            assert spec["min"] <= value <= spec["max"] and math.isfinite(spec["max"])
        elif spec["type"] == "bool":
            assert isinstance(value, bool)
        else:
            assert spec["type"] == "enum" and value in spec["values"]


def test_estimate_record_shape() -> None:
    """The certificate a converged solve emits for r-grid38 (decimal strings, estimate
    only, no upper bound or gap)."""
    sol = _model("r-grid38")["solution"]
    est = {
        "value": str(Decimal(repr(sol["value"]))),
        "residual": str(Decimal(repr(sol["residual"]))),
        "tolerance": str(Decimal(repr(PRESET["residual_tolerance"]))),
    }
    assert Fraction(est["value"]) > 59 and Fraction(est["residual"]) <= Fraction(est["tolerance"])
    example = CONTRACT["example_diagnostics"]["record"]["certificate"]
    assert example["bound_kind"] == "estimate" and example["upper_raw"] is None
    assert example["gap_raw"] is None and example["optimality_proven"] is False
    assert (
        example["estimate"] == est
        and example["lower_raw"] == _model("r-grid38")["recovery"]["gross"]
    )
    assert example["candidate_domain_hash"] == CONTRACT["example_domain_hash"]


def _r021_validator() -> Any:
    path = ROOT / "tests" / "docs" / "test_research_021_contract.py"
    spec = importlib.util.spec_from_file_location("r021_contract_checks", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_diagnostics_is_a_complete_record_accepted_by_the_r021_validator() -> None:
    """The memo's example is the shared P-CFMM-EST positive (record + the runner's
    independent run/request context); the R021 validator accepts it, rejects a wrong run
    identity or request, and its numbers are the r-grid38 model reference."""
    r021 = _r021_validator()
    examples = json.loads(
        (ROOT / "docs/references/research-021/fixtures/examples.json").read_text()
    )
    positive = next(p for p in examples["positives"] if p["id"] == "P-CFMM-EST")
    doc = CONTRACT["example_diagnostics"]
    assert doc == {"record": positive["record"], "context": positive["context"]}
    record, context = doc["record"], doc["context"]
    assert r021.check_diagnostics(copy.deepcopy(record), context) == set()
    assert record["domain"] == CONTRACT["example_domain"] == examples["domains"]["cfmm38_recovered"]
    assert record["max_candidates_unit"] == CONTRACT["row"]["max_candidates_unit"]
    wrong_run = {**context, "run": {**context["run"], "git_revision": "0" * 40}}
    assert r021.check_diagnostics(copy.deepcopy(record), wrong_run) == {"C_IDENTITY"}
    wrong_request = {**context, "request": {**context["request"], "amount_in": "39"}}
    assert r021.check_diagnostics(copy.deepcopy(record), wrong_request) == {"C_REQUEST"}
    settings = hashlib.sha256(json.dumps(PRESET, sort_keys=True).encode()).hexdigest()
    assert record["certificate"]["source"]["effective_settings_sha256"] == settings
    sol, stored = _model("r-grid38")["solution"], _model("r-grid38")["recovery"]
    work = record["work"]
    assert work["objective_evaluations"] == work["gradient_evaluations"] == sol["evaluations"]
    assert work["market_oracle_calls"] == sol["oracle_calls"] == sol["evaluations"] * 2
    assert work["optimizer_iterations"] == sol["nit"]
    assert work["quotes_executed"] == context["quotes_counted"] == len(stored["flows"])
    assert record["certificate"]["lower_raw"] == context["final_score"] == stored["gross"]
    history = positive["history"]["previous"]
    assert history["max_candidates_unit"] == "declared_by_research"
    assert examples["domain_hashes"]["cfmm38"] == history["candidate_domain_hash"]


def test_shared_contract_row_is_filled_with_history() -> None:
    row = next(i for i in R021["identities"] if i["id"] == "cfmm_dual")
    assert row["max_candidates_unit"] == CONTRACT["row"]["max_candidates_unit"]
    assert "declared_by_research" not in json.dumps(row["governing"])
    assert (
        row["row_fill"]["issue"] == "WHI-1557"
        and "declared_by_research" in row["row_fill"]["replaces"]
    )
    line = next(x for x in R021_PROSE.splitlines() if x.startswith("| `cfmm_dual` |"))
    assert "`fallback_paths_evaluated`" in line and "declared by WHI-1557" not in line


def test_fallback_single_path_is_inside_the_domain() -> None:
    """When recovery fails (r-tiny from an empty support would), the declared fallback is
    the best exact single path over the same markets: 99 for R021 R5."""
    bundle, c = _net_bundle(_router_input("r-tiny"))
    best = max(
        evaluate(bundle, c, path_plan(c, p), gross_only()).gross_output
        for p in enumerate_paths(build_graph_index(bundle), c.token_in, c.token_out, 3)
    )
    assert best == 99

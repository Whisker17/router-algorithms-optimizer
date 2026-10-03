"""WHI-1558 component A: the runtime CPMM model of `cfmm_dual` (`routing.cfmm.model`).

Independent expectations only: the pinned author fixtures (`tests/fixtures/cfmm/
author_reference.json`, CFMMRouter.jl run offline), hand-built networks and structural
facts. Nothing here imports the WHI-1557 research model or SciPy.
"""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path
from typing import Any

import pytest

from routing.cfmm import model as cm
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
INPUTS = json.loads((FIX / "author_inputs.json").read_text())
AUTHOR = json.loads((FIX / "author_reference.json").read_text())
MANTLE = ROOT / "tests" / "fixtures" / "routing" / "mantle_mixed"
BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)


def rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(a), abs(b), 1e-300)


def cp(pid: str, t0: str, t1: str, r0: int, r1: int, fee: int = 30) -> ConstantProductPoolState:
    return ConstantProductPoolState(pid, t0, t1, r0, r1, fee)


def bundle_of(*pools: PoolState, amount: int, token_in: str = "S", token_out: str = "T") -> Any:
    case = Case("x", token_in, token_out, amount)
    bundle = SnapshotBundle(
        "x", "synthetic", 1, BLOCK, {p.pool_id: p for p in pools}, (case,), "h", "<t>"
    )
    return bundle, case


def author_network(cid: str) -> tuple[SnapshotBundle, Case, dict[str, Any]]:
    doc: dict[str, Any] = next(c for c in INPUTS["router"] if c["id"] == cid)
    pools = [
        cp(p["pool_id"], p["token0"], p["token1"], p["reserve0"], p["reserve1"], p["fee_bps"])
        for p in doc["pools"]
    ]
    bundle, case = bundle_of(
        *pools, amount=doc["amount_in"], token_in=doc["token_in"], token_out=doc["token_out"]
    )
    return bundle, case, doc


# ------------------------------------------------------------------ G-C1 author oracle/dual


@pytest.mark.parametrize("case", INPUTS["cpmm_oracle"], ids=lambda c: c["id"])
def test_cpmm_oracle_matches_the_pinned_author_run(case: dict[str, Any]) -> None:
    pool = cp("p", "A", "B", case["R"][0], case["R"][1], case["fee_bps"])
    trade = cm.cpmm_arb(pool, {"A": case["v"][0], "B": case["v"][1]})
    ref = next(c for c in AUTHOR["cpmm_oracle"] if c["id"] == case["id"])
    ours = [0.0, 0.0, 0.0, 0.0]  # delta_A, delta_B, lambda_A, lambda_B
    if trade is not None:
        i = 0 if trade.token_in == "A" else 1
        ours[i], ours[3 - i] = trade.amount_in, trade.amount_out
    for got, want in zip(ours, ref["delta"] + ref["lambda"], strict=True):
        assert (got == want == 0) or rel(got, want) < 1e-12


@pytest.mark.parametrize("cid", [c["id"] for c in INPUTS["router"]])
def test_dual_value_matches_the_author_at_the_author_point(cid: str) -> None:
    """At the author's own final nu (normalized to nu_out = 1; g is homogeneous of degree
    one) the runtime g and trades equal the author's dual value and trades (1e-9)."""
    bundle, case, doc = author_network(cid)
    ref = next(c for c in AUTHOR["router"] if c["id"] == cid)
    tokens = doc["tokens"]
    v_out = ref["v"][tokens.index(case.token_out)]
    nu = {t: v / v_out for t, v in zip(tokens, ref["v"], strict=True) if t != case.token_out}
    ev = cm.dual_value(cm.dual_problem(bundle, case, tuple(bundle.pools)), nu)
    assert rel(ev.value * v_out, ref["dual_value"]) < 1e-9
    assert ev.oracle_calls == len(bundle.pools)
    by_pool = {t.pool_id: t for t in ev.trades}
    for row, pool in zip(ref["trades"], doc["pools"], strict=True):
        got, tendered = by_pool.get(pool["pool_id"]), max(row["delta"])
        assert (got is None and tendered == 0) or (
            got is not None and rel(got.amount_in, tendered) < 1e-9
        )


def test_cpmm_fee_is_the_actual_pool_fee_and_one_direction_at_most() -> None:
    """The no-trade band is exactly [gamma, 1/gamma] around the fee-free spot price, with
    gamma = (10000 - fee_bps)/10000 of the pool (5 bp and 30 bp differ)."""
    for fee in (5, 30, 100):
        pool = cp("p", "A", "B", 1000, 1000, fee)
        gamma = (10_000 - fee) / 10_000
        assert cm.cpmm_arb(pool, {"A": gamma * 1.000001, "B": 1.0}) is None
        assert cm.cpmm_arb(pool, {"A": 1 / gamma * 0.999999, "B": 1.0}) is None
        sell_a = cm.cpmm_arb(pool, {"A": gamma * 0.9, "B": 1.0})
        sell_b = cm.cpmm_arb(pool, {"A": 1 / gamma * 1.1, "B": 1.0})
        assert sell_a is not None and (sell_a.token_in, sell_a.token_out) == ("A", "B")
        assert sell_b is not None and (sell_b.token_in, sell_b.token_out) == ("B", "A")
        # the continuous output is the floor-free getAmountOut of the gross input
        out = gamma * sell_a.amount_in * 1000 / (1000 + gamma * sell_a.amount_in)
        assert rel(sell_a.amount_out, out) < 1e-12
    restricted = cm.cpmm_arb(cp("p", "A", "B", 1000, 1000), {"A": 0.5, "B": 1.0}, "B")
    assert restricted is None  # the profitable direction is A -> B; B -> A is not allowed


# ------------------------------------------------------------------ market universe (§4.1)


def test_market_universe_is_the_admitted_cpmm_simple_path_union_in_bundle_order() -> None:
    bundle, case = bundle_of(
        cp("mt", "M", "T", 10**6, 10**6),
        cp("xy", "X", "Y", 10**6, 10**6),  # disconnected
        cp("st", "S", "T", 10**6, 10**6),
        cp("sd", "S", "D", 10**6, 10**6),  # dead end
        cp("sm", "S", "M", 10**6, 10**6),
        cp("empty", "S", "T", 0, 10**6),  # no liquidity: not admitted
        dataclasses.replace(cp("badfee", "S", "T", 10**6, 10**6, 25), source_key="moe_classic_v1"),
        amount=1000,
    )
    assert cm.market_universe(bundle, case, 3) == ("mt", "st", "sm")
    assert cm.market_universe(bundle, case, 1) == ("st",)
    moe = dataclasses.replace(cp("moe", "S", "T", 10**6, 10**6, 30), source_key="moe_classic_v1")
    assert cm.cpmm_admitted(moe)


def test_liquidity_book_and_cl_pools_never_enter_the_cpmm_market_set() -> None:
    bundle = load_bundle(MANTLE)
    kinds: set[type] = set()
    for case in bundle.cases:
        markets = cm.market_universe(bundle, case, 3)
        kinds.update(type(bundle.pools[p]) for p in markets)
        assert list(markets) == [p for p in bundle.pools if p in markets]  # bundle order
    assert kinds == {ConstantProductPoolState}
    assert any(isinstance(p, LiquidityBookPoolState) for p in bundle.pools.values())
    assert any(isinstance(p, ConcentratedPoolState) for p in bundle.pools.values())
    case = bundle.cases[0]
    lb = next(pid for pid, p in bundle.pools.items() if isinstance(p, LiquidityBookPoolState))
    with pytest.raises(ValueError, match="not an admitted CPMM market"):
        cm.dual_problem(bundle, case, [lb])


def test_dual_problem_refuses_invalid_inputs() -> None:
    bundle, case = bundle_of(cp("st", "S", "T", 1000, 1000), amount=10)
    for markets in ([], ["st", "st"], ["nope"]):
        with pytest.raises(ValueError):
            cm.dual_problem(bundle, case, markets)
    for bad in (Case("x", "S", "T", 0), Case("x", "S", "S", 5), Case("x", "S", "T", True)):
        with pytest.raises(ValueError):
            cm.dual_problem(bundle, bad, ["st"])
    problem = cm.dual_problem(bundle, case, ["st"])
    with pytest.raises(dataclasses.FrozenInstanceError):
        problem.variables = ()  # type: ignore[misc]


def test_three_token_problem_variables_and_restriction() -> None:
    bundle, case, _ = author_network("r-triangle")
    problem = cm.dual_problem(bundle, case, tuple(bundle.pools))
    assert problem.variables == ("S", "M") and problem.market_ids == ("st", "sm", "mt", "mt2")
    # the allowed mapping's own order does not matter: admitted order is kept
    sub = cm.restricted(problem, {"mt": "M", "sm": "S"})
    assert sub.market_ids == ("sm", "mt") and sub.allowed == ("S", "M")
    assert sub.variables == ("S", "M")
    for bad in ({}, {"zz": "S"}, {"sm": "T"}):
        with pytest.raises(ValueError):
            cm.restricted(problem, bad)


# ------------------------------------------------------------------ normalization (§5.1-5.2)


def test_scale_uses_the_deepest_market_not_the_first_found() -> None:
    """A pool holding 1 raw unit of token_out, with an extreme price, comes first; the
    maximum-depth tree prices S from the deep pool (first-found would give 1e-8)."""
    bundle, case = bundle_of(
        cp("shallow", "S", "T", 10**8, 1), cp("deep", "S", "T", 10**6, 10**6), amount=1000
    )
    sigma = cm.scales(cm.dual_problem(bundle, case, ["shallow", "deep"]))
    assert dict(sigma) == {"T": 1.0, "S": 1.0}


def test_scale_ties_follow_admitted_market_order() -> None:
    a, b = cp("a", "S", "T", 1000, 5000), cp("b", "S", "T", 2000, 5000)  # equal T depth
    bundle, case = bundle_of(a, b, amount=10)
    assert cm.scales(cm.dual_problem(bundle, case, ["a", "b"]))["S"] == 5.0
    assert cm.scales(cm.dual_problem(bundle, case, ["b", "a"]))["S"] == 2.5


def test_log_objective_is_invariant_to_token_decimals() -> None:
    """Re-denominating the intermediate M by 10**12 raw units (a decimals change) leaves
    Phi and dPhi/dx unchanged at the same x: the objective is unit-free (§5.2)."""
    bundle, case, doc = author_network("r-triangle")
    scaled = {
        pid: dataclasses.replace(
            p,
            reserve0=p.reserve0 * 10**12 if p.token0 == "M" else p.reserve0,
            reserve1=p.reserve1 * 10**12 if p.token1 == "M" else p.reserve1,
        )
        for pid, p in bundle.pools.items()
        if isinstance(p, ConstantProductPoolState)
    }
    other = dataclasses.replace(bundle, pools=scaled)
    base = cm.dual_problem(bundle, case, tuple(bundle.pools))
    big = cm.dual_problem(other, case, tuple(other.pools))
    s_base, s_big = cm.scales(base), cm.scales(big)
    assert rel(s_big["M"] * 10**12, s_base["M"]) < 1e-15
    for x in ([0.0, 0.0], [0.3, -0.2], [-1.0, 2.0]):
        phi, grad, ev = cm.log_objective(base, s_base, x)
        phi2, grad2, ev2 = cm.log_objective(big, s_big, x)
        assert rel(phi, phi2) < 1e-12 and rel(ev.value, ev2.value) < 1e-12
        assert all(abs(g - h) < 1e-12 for g, h in zip(grad, grad2, strict=True))


def test_spot_start_of_a_single_market_is_trade_free() -> None:
    """x = 0 prices every spanning-tree market at its own fee-free spot, inside its fee
    band: a lone market does not trade, g = A*sigma_in and dPhi/dx_in is exactly +1."""
    bundle, case = bundle_of(cp("st", "S", "T", 1000, 3000), amount=10)
    problem = cm.dual_problem(bundle, case, ["st"])
    sigma = cm.scales(problem)
    phi, grad, ev = cm.log_objective(problem, sigma, [0.0])
    assert sigma["S"] == 3.0 and ev.trades == () and phi == math.log(10 * 3.0)
    assert grad == [1.0]
    assert cm.projected_residual([0.0], grad, -50.0, 50.0) == 1.0
    assert cm.projected_residual([-50.0], [1.0], -50.0, 50.0) == 0.0  # active bound


def test_arithmetic_failures_are_numeric_failures_with_actual_oracle_calls() -> None:
    bundle, case = bundle_of(cp("a", "S", "T", 10**400, 10**400), amount=10)
    problem = cm.dual_problem(bundle, case, ["a"])
    with pytest.raises(cm.NumericFailure) as scale:
        cm.scales(problem)
    assert scale.value.oracle_calls == 0
    ok, _ = bundle_of(cp("a", "S", "T", 1000, 1000), cp("b", "S", "T", 900, 1000), amount=10)
    two = cm.dual_problem(ok, case, ["a", "b"])
    with pytest.raises(cm.NumericFailure) as zero:
        cm.dual_value(two, {"S": 0.0})  # nu_a = 0 divides by zero in the first oracle
    assert zero.value.oracle_calls == 1
    phi, _, _ = cm.log_objective(two, {"S": 1.0}, [float("nan")])
    assert math.isnan(phi)  # returned as is: the guard classifies it (never clamped)

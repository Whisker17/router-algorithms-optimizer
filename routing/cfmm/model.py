"""CPMM continuous model of `cfmm_dual` (WHI-1558; `cfmm-dual.md` §§4.1-4.2, 5.1-5.2).

Ported, CPMM part only, from the validated executable contract model
`tests/routing/cfmm_contract_model.py` of WHI-1557 (merged `91d7f4b`, sha256
`7fb979394cbcba243058fe38ebdc37b56602735ab3887475c6386851006ac387`), keeping its float
operation order so the committed `tests/fixtures/cfmm/model_reference.json` dual points
reproduce. The oracle is the closed form of the paper (Diamandis, Resnick, Chitra, Angeris,
arXiv:2302.04938v1, App. A, eta = 1) as implemented by the pinned author code
(`bcc-research/CFMMRouter.jl` `5932e42e5077ffc7d8e02c3b3ad2e9ed1d441267`, `ProductTwoCoin`
`find_arb!`; MIT, notice in `routing/cfmm/NOTICE.md`). No test or research module is
imported. The CL oracle is WHI-1559; Liquidity Book pools never become markets.

Units are raw integer token units; prices nu are "raw token_out units per raw unit" with
nu_out fixed to 1. Everything here is float64 and deterministic (sequential market loop in
admitted order, `math.fsum`); none of it is money. Arithmetic failures of the oracle or
objective surface as `NumericFailure` with the market-oracle calls actually made, never as a
clamped value.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from pools.constant_product import SOURCES as CP_SOURCES
from routing.search import build_graph_index, enumerate_paths
from snapshot.models import Case, ConstantProductPoolState, SnapshotBundle

CPMM = "constant_product"


class NumericFailure(ArithmeticError):
    """A non-finite or undefined oracle/dual/objective value (§5.4 `numeric_failure`).
    `oracle_calls` is the number of market-oracle calls made by the failing evaluation."""

    def __init__(self, message: str, oracle_calls: int) -> None:
        super().__init__(message)
        self.oracle_calls = oracle_calls


# --------------------------------------------------------------------------- markets


def cpmm_admitted(pool: ConstantProductPoolState) -> bool:
    """A CPMM pool is a market iff its exact quote semantics are admitted and it has
    liquidity: source-free (synthetic) or a known source with that source's fee, and both
    reserves positive (otherwise every quote fails, so it cannot carry flow)."""
    if pool.source_key is not None:
        src = CP_SOURCES.get(pool.source_key)
        if src is None or src.fee_bps != pool.fee_bps:
            return False
    return pool.reserve0 > 0 and pool.reserve1 > 0 and 0 <= pool.fee_bps < 10_000


def market_universe(bundle: SnapshotBundle, case: Case, max_hops: int) -> tuple[str, ...]:
    """§4.1 `simple_path_union` (CPMM stage): the admitted CPMM pools, in bundle insertion
    order, that lie on at least one simple `token_in -> token_out` path of at most
    `max_hops` admitted pools (`routing.search.enumerate_paths`)."""
    pools = {
        pid: p
        for pid, p in bundle.pools.items()
        if isinstance(p, ConstantProductPoolState) and cpmm_admitted(p)
    }
    index = build_graph_index(dataclasses.replace(bundle, pools=pools))
    used: set[str] = set()
    for path in enumerate_paths(index, case.token_in, case.token_out, max_hops):
        used.update(e.pool_id for e in path)
    return tuple(pid for pid in bundle.pools if pid in used)


# --------------------------------------------------------------------------- oracle


@dataclass(frozen=True)
class Trade:
    """One market's continuous trade at given prices: tender `amount_in` of `token_in`
    (gross, fee included), receive `amount_out` of `token_out`. Floats, not money."""

    pool_id: str
    token_in: str
    token_out: str
    amount_in: float
    amount_out: float


def cpmm_gamma(pool: ConstantProductPoolState) -> float:
    return (10_000 - pool.fee_bps) / 10_000


def cpmm_arb(
    pool: ConstantProductPoolState, nu: Mapping[str, float], allowed_in: str | None = None
) -> Trade | None:
    """Closed-form optimal arbitrage of one CPMM with the actual input fee: trade a for b
    iff r^2 = gamma*nu_b*R_b/(nu_a*R_a) > 1; then delta = R_a*(r-1)/gamma and
    lambda = R_b*(1-1/r) (at most one direction). `allowed_in` restricts the market to one
    direction (restricted re-solve)."""
    g = cpmm_gamma(pool)
    for a, b, ra, rb in (
        (pool.token0, pool.token1, pool.reserve0, pool.reserve1),
        (pool.token1, pool.token0, pool.reserve1, pool.reserve0),
    ):
        if allowed_in is not None and a != allowed_in:
            continue
        ratio = g * nu[b] * rb / (nu[a] * ra)
        if ratio > 1.0:
            r = math.sqrt(ratio)
            return Trade(pool.pool_id, a, b, ra * (r - 1.0) / g, rb * (1.0 - 1.0 / r))
    return None


# --------------------------------------------------------------------------- dual


@dataclass(frozen=True)
class DualProblem:
    """Immutable inputs of g(nu) for one exact-input case: the admitted CPMM `markets`
    (admitted order), the priced `variables` (every market token except `token_out`, in
    first-seen order starting with `token_in`) and, for a restricted re-solve, the one
    allowed input token per market (`allowed[i]` for `markets[i]`; `None` = both ways)."""

    case: Case
    markets: tuple[ConstantProductPoolState, ...]
    variables: tuple[str, ...]
    allowed: tuple[str | None, ...]

    @property
    def market_ids(self) -> tuple[str, ...]:
        return tuple(p.pool_id for p in self.markets)


def _problem(
    case: Case, markets: Sequence[ConstantProductPoolState], allowed: Sequence[str | None]
) -> DualProblem:
    seen: dict[str, None] = {case.token_in: None}
    for p in markets:
        seen.setdefault(p.token0, None)
        seen.setdefault(p.token1, None)
    variables = tuple(t for t in seen if t != case.token_out)
    return DualProblem(case, tuple(markets), variables, tuple(allowed))


def dual_problem(bundle: SnapshotBundle, case: Case, markets: Sequence[str]) -> DualProblem:
    """The full-network problem over `markets` (normally `market_universe`, in admitted
    order). Every id must name a distinct admitted CPMM pool of `bundle`; the case must be
    a positive exact-input order between two different tokens."""
    if isinstance(case.amount_in, bool) or not isinstance(case.amount_in, int):
        raise ValueError(f"amount_in must be an int, got {case.amount_in!r}")
    if case.amount_in <= 0 or case.token_in == case.token_out:
        raise ValueError(f"case {case.case_id!r}: not a positive exact-input order")
    if not markets or len(set(markets)) != len(markets):
        raise ValueError(f"markets must be a non-empty list of distinct ids: {list(markets)}")
    pools: list[ConstantProductPoolState] = []
    for pid in markets:
        pool = bundle.pools.get(pid)
        if not isinstance(pool, ConstantProductPoolState) or not cpmm_admitted(pool):
            raise ValueError(f"{pid!r}: not an admitted CPMM market")
        pools.append(pool)
    return _problem(case, pools, [None] * len(pools))


def restricted(problem: DualProblem, allowed: Mapping[str, str]) -> DualProblem:
    """The §6 step 3 restricted problem: only the markets named in `allowed` (kept in the
    problem's admitted order), each one-directional with `allowed[pool_id]` as its only
    input token; variables are recomputed from the kept markets."""
    by_id = {p.pool_id: p for p in problem.markets}
    unknown = sorted(set(allowed) - set(by_id))
    if unknown or not allowed:
        raise ValueError(f"restricted markets must be a non-empty subset, unknown {unknown}")
    kept = [p for p in problem.markets if p.pool_id in allowed]
    for p in kept:
        if allowed[p.pool_id] not in (p.token0, p.token1):
            raise ValueError(f"{p.pool_id!r}: {allowed[p.pool_id]!r} is not one of its tokens")
    return _problem(problem.case, kept, [allowed[p.pool_id] for p in kept])


@dataclass(frozen=True)
class DualEval:
    """g(nu), dg/dnu over the problem's variables, the non-zero trades (admitted order)
    and the market-oracle calls this evaluation made."""

    value: float
    gradient: Mapping[str, float]
    trades: tuple[Trade, ...]
    oracle_calls: int


def dual_value(problem: DualProblem, prices: Mapping[str, float]) -> DualEval:
    """g(nu) = A*nu_in + sum_i arb_i(nu) with nu_out = 1 and dg/dnu_j = A*[j = in] +
    sum_i (received_ij - tendered_ij), summed with `math.fsum` in admitted market order.
    An arithmetic error in an oracle call is a `NumericFailure`; a non-finite value is
    returned as is (the caller decides)."""
    case = problem.case
    nu = {**prices, case.token_out: 1.0}
    trades: list[Trade] = []
    for i, (pool, allowed_in) in enumerate(zip(problem.markets, problem.allowed, strict=True)):
        try:
            t = cpmm_arb(pool, nu, allowed_in)
        except (ArithmeticError, ValueError) as exc:
            raise NumericFailure(f"{pool.pool_id}: oracle failed: {exc}", i + 1) from exc
        if t is not None:
            trades.append(t)
    calls = len(problem.markets)
    a = float(case.amount_in)
    try:
        value = math.fsum(
            [a * nu[case.token_in]]
            + [nu[t.token_out] * t.amount_out - nu[t.token_in] * t.amount_in for t in trades]
        )
        parts: dict[str, list[float]] = {v: [] for v in problem.variables}
        parts[case.token_in].append(a)
        for t in trades:
            if t.token_out in parts:
                parts[t.token_out].append(t.amount_out)
            if t.token_in in parts:
                parts[t.token_in].append(-t.amount_in)
        grad = {v: math.fsum(xs) for v, xs in parts.items()}
    except (ArithmeticError, ValueError) as exc:
        raise NumericFailure(f"dual sum failed: {exc}", calls) from exc
    return DualEval(value, MappingProxyType(grad), tuple(trades), calls)


# --------------------------------------------------------------------------- normalization


def scales(problem: DualProblem) -> Mapping[str, float]:
    """§5.1 sigma (sigma_out = 1): a maximum-depth spanning tree grown from token_out
    (Prim): repeatedly price the unpriced token reachable through the market with the
    largest priced-side depth valued in token_out units, sigma_k * reserve_k (ties: admitted
    market order); sigma_j = sigma_k * fee-free spot value of j in k (R_k / R_j). A shallow
    pool with an extreme price therefore never sets a token's scale when a deeper one
    exists. A scale that cannot be formed in float64 is a `NumericFailure`."""
    try:
        return _scales(problem)
    except (ArithmeticError, ValueError) as exc:
        raise NumericFailure(f"scale undefined: {exc}", 0) from exc


def _scales(problem: DualProblem) -> Mapping[str, float]:
    sigma = {problem.case.token_out: 1.0}
    while True:
        best: tuple[float, int, str, str] | None = None
        for i, pool in enumerate(problem.markets):
            for known, other in ((pool.token0, pool.token1), (pool.token1, pool.token0)):
                if known in sigma and other not in sigma:
                    value = sigma[known] * float(pool.reserves_for(known)[0])
                    if best is None or value > best[0]:
                        best = (value, i, known, other)
        if best is None:
            return MappingProxyType(sigma)
        _, i, known, other = best
        r_in, r_out = problem.markets[i].reserves_for(other)
        sigma[other] = sigma[known] * (r_out / r_in)


def log_objective(
    problem: DualProblem, sigma: Mapping[str, float], x: Sequence[float]
) -> tuple[float, list[float], DualEval]:
    """§5.2 Phi(x) = log g(nu) with nu_j = sigma_j*exp(x_j) and nu_out = 1, and
    dPhi/dx_j = nu_j*(dg/dnu_j)/g: the net-flow imbalance of token j valued at the point's
    own prices as a fraction of g (unit-free, invariant to decimals and price scale).
    Arithmetic errors are a `NumericFailure`; finiteness is checked by the caller."""
    nu = {v: sigma[v] * math.exp(xi) for v, xi in zip(problem.variables, x, strict=True)}
    ev = dual_value(problem, nu)
    try:
        grad = [nu[v] * ev.gradient[v] / ev.value for v in problem.variables]
        phi = math.log(ev.value)
    except (ArithmeticError, ValueError) as exc:
        raise NumericFailure(f"log objective undefined: {exc}", ev.oracle_calls) from exc
    return phi, grad, ev


def projected_residual(
    x: Sequence[float], grad: Sequence[float], lower: float, upper: float
) -> float:
    """L-BFGS-B's stationarity measure on the box [lower, upper]: max_j |P(x - g) - x|_j
    (§5.4)."""
    worst = 0.0
    for xi, g in zip(x, grad, strict=True):
        worst = max(worst, abs(min(max(xi - g, lower), upper) - xi))
    return worst

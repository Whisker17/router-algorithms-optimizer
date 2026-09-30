"""Executable reference of the `cfmm_dual` contract (WHI-1557,
`docs/references/research-021/cfmm-dual.md`). Validation-only.

This module is **not** a solver and is never registered: it holds no optimizer. It pins,
as runnable code, the pieces of the contract that WHI-1558 (CPMM) and WHI-1559 (CL) must
reproduce, so that the specification and its checks cannot drift apart:

- `market_universe`: the admitted market set (contract §3.1, rule `simple_path_union`).
- `cpmm_arb` / `cl_arb`: the per-market optimal-arbitrage oracle (paper eq. (5)/(6),
  author `find_arb!`), in raw token units, with the actual pool fee.
- `dual_value`: the dual function g(nu) with nu_out fixed to 1 and its gradient (paper
  eqs. (7)-(9)), summed with `math.fsum` in admitted market order.
- `scales` / `log_objective`: the normalization and optimizer function of §5.
- `cl_ladder`: the continuous Uniswap-v3 aggregate over the *known* price range (§8).
- `recover`: the restricted-domain projection that turns a continuous flow into an
  ordered, fully funded integer `RoutePlan` (§6), replayed by the real evaluator.

The continuous side is float64 (numerical); every amount that reaches a plan is an exact
integer and every quote goes through `pools.quote.quote_exact_in` (via `QuoteCache`).
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction

from benchmark.objective import gross_only
from pools.cl_math import MAX_TICK, MIN_TICK, get_sqrt_ratio_at_tick
from pools.concentrated import SOURCES as CL_SOURCES
from pools.concentrated import ZERO_ADDRESS
from pools.constant_product import SOURCES as CP_SOURCES
from pools.result import QuoteStatus
from routing.algorithms.incremental_graph import PoolFlow, merged_plan
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import RoutePlan
from routing.search import Edge, QuoteCache, build_graph_index, enumerate_paths
from snapshot.models import (
    Case,
    ConcentratedPoolState,
    ConstantProductPoolState,
    PoolState,
    SnapshotBundle,
    TickInfo,
)

Q96 = float(1 << 96)
CPMM = "constant_product"
CL = "concentrated"


# --------------------------------------------------------------------------- markets


def cpmm_admitted(pool: ConstantProductPoolState) -> bool:
    """A CPMM pool is a market iff its exact quote semantics are admitted and it has
    liquidity: source-free (synthetic) or a known source with that source's fee, and
    both reserves positive (otherwise every quote fails, so it cannot carry flow)."""
    if pool.source_key is not None:
        src = CP_SOURCES.get(pool.source_key)
        if src is None or src.fee_bps != pool.fee_bps:
            return False
    return pool.reserve0 > 0 and pool.reserve1 > 0 and 0 <= pool.fee_bps < 10_000


def cl_admitted(pool: ConcentratedPoolState) -> bool:
    """A CL pool is a market iff `pools.concentrated` admits its source (the same
    predicate as `_source`) and its fee leaves a positive input share."""
    src = CL_SOURCES.get(pool.source_key)
    if src is None or not 0 <= pool.fee < 1_000_000:
        return False
    has_lm = pool.lm_pool is not None and pool.lm_pool.lower() != ZERO_ADDRESS
    return src.has_lm_pool_hook or not has_lm


def admitted(pool: PoolState, protocols: Sequence[str]) -> bool:
    if isinstance(pool, ConstantProductPoolState):
        return CPMM in protocols and cpmm_admitted(pool)
    if isinstance(pool, ConcentratedPoolState):
        return CL in protocols and cl_admitted(pool)
    return False  # liquidity_book is outside every cfmm_dual stage


def market_universe(
    bundle: SnapshotBundle, case: Case, max_hops: int, protocols: Sequence[str]
) -> tuple[str, ...]:
    """Pools (bundle insertion order) that lie on at least one simple
    `token_in -> token_out` path of at most `max_hops` admitted pools."""
    pools = {pid: p for pid, p in bundle.pools.items() if admitted(p, protocols)}
    index = build_graph_index(dataclasses.replace(bundle, pools=pools))
    used: set[str] = set()
    for path in enumerate_paths(index, case.token_in, case.token_out, max_hops):
        used.update(e.pool_id for e in path)
    return tuple(pid for pid in bundle.pools if pid in used)


# --------------------------------------------------------------------------- oracle


@dataclass(frozen=True)
class Trade:
    """One market's continuous trade at the current prices: tender `amount_in` of
    `token_in` (gross, fee included), receive `amount_out` of `token_out`."""

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
    """Closed-form optimal arbitrage of one CPMM (paper App. A with eta = 1; author
    `ProductTwoCoin` `find_arb!`): trade token a for b iff gamma*R_b/R_a > nu_a/nu_b; then
    r = sqrt(gamma*nu_b*R_b / (nu_a*R_a)), delta = R_a*(r-1)/gamma, lambda = R_b*(1-1/r).
    `allowed_in` restricts the market to one direction (restricted re-solve)."""
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


# -------------------------------------------------------------- continuous V3 aggregate


@dataclass(frozen=True)
class Segment:
    """One constant-liquidity piece of the known price range, in sqrt-price space
    (`sqrt_price_x96 / 2**96`): from `start` (nearer the current price) to `end`."""

    start: float
    end: float
    liquidity: float


@dataclass(frozen=True)
class ClLadder:
    """The continuous aggregate of one CL pool (contract §8): current sqrt price, fee
    factor and, per direction, the segments up to the collected boundary. `down` is
    token0 -> token1 (price falls), `up` is token1 -> token0."""

    sqrt_price: float
    gamma: float
    down: tuple[Segment, ...]
    up: tuple[Segment, ...]
    down_boundary: str
    up_boundary: str


def _initialized(state: ConcentratedPoolState) -> list[int]:
    """Every initialized tick the collected bitmap words declare, ascending."""
    ticks: list[int] = []
    for word_pos in range(state.bitmap_word_range[0], state.bitmap_word_range[1] + 1):
        word = state.tick_bitmap.get(word_pos, 0)
        bit = 0
        while word:
            if word & 1:
                ticks.append((word_pos * 256 + bit) * state.tick_spacing)
            word >>= 1
            bit += 1
    return ticks


def _sqrt_at(tick: int) -> float:
    return get_sqrt_ratio_at_tick(tick) / Q96


def cl_ladder(state: ConcentratedPoolState) -> ClLadder:
    """The known-range continuous aggregate. A direction ends at the first of: the
    collected bitmap boundary (the swap would read an uncollected word), an initialized
    tick without `TickInfo` (it could not be crossed), or MIN_TICK/MAX_TICK. Liquidity
    changes by `liquidity_net` at every initialized tick crossed (minus going down).

    A direction is **empty** when the first word the exact swap reads lies outside the
    collected interval, on either side: going down that is the word of the current
    compressed tick, going up the word of `compressed + 1`
    (`TickBitmap.nextInitializedTickWithinOneWord`). The swap would fail at once with
    `incomplete_snapshot`, so no liquidity is assumed."""
    s = state.tick_spacing
    w_lo, w_hi = state.bitmap_word_range
    compressed = state.tick // s
    init = _initialized(state)
    cur = state.sqrt_price_x96 / Q96

    down: list[Segment] = []
    down_boundary = "collected_range"
    if not w_lo <= compressed >> 8 <= w_hi:
        lo_tick = None
    else:
        lo_tick = max(w_lo * 256 * s, MIN_TICK)
    if lo_tick is not None:
        liquidity = state.liquidity
        start = cur
        for t in sorted((t for t in init if lo_tick <= t <= state.tick), reverse=True):
            end = _sqrt_at(t)
            if end < start:
                down.append(Segment(start, end, float(liquidity)))
                start = end
            info = state.ticks.get(t)
            if info is None:
                down_boundary = "missing_tick_data"
                break
            liquidity -= info.liquidity_net
        else:
            end = _sqrt_at(lo_tick)
            if end < start:
                down.append(Segment(start, end, float(liquidity)))
            if lo_tick == MIN_TICK:
                down_boundary = "min_tick"

    up: list[Segment] = []
    up_boundary = "collected_range"
    if not w_lo <= (compressed + 1) >> 8 <= w_hi:
        hi_tick = None
    else:
        hi_tick = min((w_hi * 256 + 255) * s, MAX_TICK)
    if hi_tick is not None:
        liquidity = state.liquidity
        start = cur
        for t in sorted(t for t in init if state.tick < t <= hi_tick):
            end = _sqrt_at(t)
            if end > start:
                up.append(Segment(start, end, float(liquidity)))
                start = end
            info = state.ticks.get(t)
            if info is None:
                up_boundary = "missing_tick_data"
                break
            liquidity += info.liquidity_net
        else:
            end = _sqrt_at(hi_tick)
            if end > start:
                up.append(Segment(start, end, float(liquidity)))
            if hi_tick == MAX_TICK:
                up_boundary = "max_tick"

    gamma = (1_000_000 - state.fee) / 1_000_000
    return ClLadder(cur, gamma, tuple(down), tuple(up), down_boundary, up_boundary)


def cl_arb(
    state: ConcentratedPoolState,
    ladder: ClLadder,
    nu: Mapping[str, float],
    allowed_in: str | None = None,
) -> Trade | None:
    """Optimal arbitrage of the continuous aggregate (paper §3 "bounded liquidity" and
    the aggregate trick; author `UniV3` `find_arb!`): sell token0 while
    gamma*P > nu0/nu1, i.e. down to sqrt(nu0/(gamma*nu1)); sell token1 while
    P < gamma*nu0/nu1, i.e. up to sqrt(gamma*nu0/nu1). Both stop at the known boundary;
    the fee divides the net input once (V3 charges it on the input)."""
    g, n0, n1 = ladder.gamma, nu[state.token0], nu[state.token1]
    if allowed_in in (None, state.token0) and g * ladder.sqrt_price**2 > n0 / n1:
        target = math.sqrt(n0 / (g * n1))
        net = out = 0.0
        for seg in ladder.down:
            lo = max(seg.end, target)
            if lo >= seg.start:
                break
            net += seg.liquidity * (1.0 / lo - 1.0 / seg.start)
            out += seg.liquidity * (seg.start - lo)
            if target > seg.end:
                break
        if net > 0.0:
            return Trade(state.pool_id, state.token0, state.token1, net / g, out)
        return None
    if allowed_in in (None, state.token1) and ladder.sqrt_price**2 < g * n0 / n1:
        target = math.sqrt(g * n0 / n1)
        net = out = 0.0
        for seg in ladder.up:
            hi = min(seg.end, target)
            if hi <= seg.start:
                break
            net += seg.liquidity * (hi - seg.start)
            out += seg.liquidity * (1.0 / seg.start - 1.0 / hi)
            if target < seg.end:
                break
        if net > 0.0:
            return Trade(state.pool_id, state.token1, state.token0, net / g, out)
    return None


def cl_forward(ladder: ClLadder, zero_for_one: bool, amount_in: float) -> float | None:
    """Continuous output for a gross input, or None when the input would leave the
    known range (never extrapolated)."""
    net = amount_in * ladder.gamma
    out = 0.0
    for seg in ladder.down if zero_for_one else ladder.up:
        if zero_for_one:
            cap = seg.liquidity * (1.0 / seg.end - 1.0 / seg.start)
        else:
            cap = seg.liquidity * (seg.end - seg.start)
        if net <= cap:
            if seg.liquidity == 0.0:
                return out
            # cancellation-free forms: L(sa - sb) = sa*sb*net and L(1/sa - 1/sb) = net/(sa*sb)
            if zero_for_one:
                nxt = 1.0 / (1.0 / seg.start + net / seg.liquidity)
                return out + seg.start * nxt * net
            nxt = seg.start + net / seg.liquidity
            return out + net / (seg.start * nxt)
        net -= cap
        if zero_for_one:
            out += seg.liquidity * (seg.start - seg.end)
        else:
            out += seg.liquidity * (1.0 / seg.start - 1.0 / seg.end)
    return None


def synthetic_cl(
    missing_tick_data: bool = False, source_key: str = "uniswap_v3"
) -> ConcentratedPoolState:
    """Two positions with an empty range between them: [-1800, -1200) with L1 and
    [-600, 1200) with L2 (current tick 100); ticks -1200..-600 carry no liquidity. The
    collected words are -1 and 0, i.e. ticks [-15360, 15300] at spacing 60."""
    l1, l2 = 4 * 10**15, 10**16
    ticks = {
        -1800: TickInfo(l1, l1, 0, 0),
        -1200: TickInfo(l1, -l1, 0, 0),
        -600: TickInfo(l2, l2, 0, 0),
        1200: TickInfo(l2, -l2, 0, 0),
    }
    if missing_tick_data:
        del ticks[-1800]
    bitmap = {-1: (1 << 226) | (1 << 236) | (1 << 246), 0: 1 << 20}
    return ConcentratedPoolState(
        pool_id=f"cl_synth_{source_key}{'_missing' if missing_tick_data else ''}",
        source_key=source_key,
        token0="T0",
        token1="T1",
        fee=3000,
        tick_spacing=60,
        sqrt_price_x96=get_sqrt_ratio_at_tick(100) + 12345,
        tick=100,
        liquidity=l2,
        fee_protocol=0,
        fee_growth_global0_x128=0,
        fee_growth_global1_x128=0,
        protocol_fees0=0,
        protocol_fees1=0,
        bitmap_word_range=(-1, 0),
        tick_bitmap=bitmap,
        ticks=ticks,
        lm_pool="0x" + "11" * 20 if source_key != "uniswap_v3" else None,
    )


# --------------------------------------------------------------------------- dual


@dataclass(frozen=True)
class DualProblem:
    """g(nu) for one exact-input case over `markets` (admitted order). `variables` are
    the priced tokens (all market tokens except `case.token_out`, whose price is fixed
    to 1). `allowed` fixes market directions for a restricted re-solve."""

    bundle: SnapshotBundle
    case: Case
    markets: tuple[str, ...]
    variables: tuple[str, ...]
    ladders: Mapping[str, ClLadder]
    allowed: Mapping[str, str] | None = None


def dual_problem(
    bundle: SnapshotBundle,
    case: Case,
    markets: Sequence[str],
    allowed: Mapping[str, str] | None = None,
) -> DualProblem:
    seen: dict[str, None] = {case.token_in: None}
    ladders: dict[str, ClLadder] = {}
    for pid in markets:
        p = bundle.pools[pid]
        seen.setdefault(p.token0, None)
        seen.setdefault(p.token1, None)
        if isinstance(p, ConcentratedPoolState):
            ladders[pid] = cl_ladder(p)
    variables = tuple(t for t in seen if t != case.token_out)
    return DualProblem(bundle, case, tuple(markets), variables, ladders, allowed)


@dataclass(frozen=True)
class DualEval:
    value: float
    gradient: dict[str, float]
    trades: tuple[Trade, ...]
    oracle_calls: int


def market_trade(problem: DualProblem, pid: str, nu: Mapping[str, float]) -> Trade | None:
    pool = problem.bundle.pools[pid]
    allowed = None if problem.allowed is None else problem.allowed[pid]
    if isinstance(pool, ConstantProductPoolState):
        return cpmm_arb(pool, nu, allowed)
    if isinstance(pool, ConcentratedPoolState):
        return cl_arb(pool, problem.ladders[pid], nu, allowed)
    raise TypeError(f"{pid}: not a cfmm_dual market")


def dual_value(problem: DualProblem, prices: Mapping[str, float]) -> DualEval:
    """g(nu) = A*nu_in + sum_i arb_i(nu) with nu_out = 1; dg/dnu_j = A*[j = in] +
    sum_i (received_ij - tendered_ij). Positive prices are required (the oracle divides
    by them); a non-finite value is returned as is and treated as a numeric failure."""
    nu = {**prices, problem.case.token_out: 1.0}
    trades: list[Trade] = []
    for pid in problem.markets:
        t = market_trade(problem, pid, nu)
        if t is not None:
            trades.append(t)
    a = float(problem.case.amount_in)
    value = math.fsum(
        [a * nu[problem.case.token_in]]
        + [nu[t.token_out] * t.amount_out - nu[t.token_in] * t.amount_in for t in trades]
    )
    parts: dict[str, list[float]] = {v: [] for v in problem.variables}
    parts[problem.case.token_in].append(a)
    for t in trades:
        if t.token_out in parts:
            parts[t.token_out].append(t.amount_out)
        if t.token_in in parts:
            parts[t.token_in].append(-t.amount_in)
    grad = {v: math.fsum(xs) for v, xs in parts.items()}
    return DualEval(value, grad, tuple(trades), len(problem.markets))


def _mid_value(pool: PoolState, token: str) -> float:
    """Fee-free spot value of one unit of `token` in units of the pool's other token."""
    if isinstance(pool, ConstantProductPoolState):
        r_in, r_out = pool.reserves_for(token)
        return r_out / r_in
    if isinstance(pool, ConcentratedPoolState):
        p = (pool.sqrt_price_x96 / Q96) ** 2  # token1 per token0
        return p if token == pool.token0 else 1.0 / p
    raise TypeError(type(pool).__name__)


def _depth(pool: PoolState, token: str) -> float:
    """Raw-unit depth of `token` in the pool: its reserve (CPMM) or the active
    liquidity's virtual reserve L/sqrtP (token0) or L*sqrtP (token1) (CL)."""
    if isinstance(pool, ConstantProductPoolState):
        return float(pool.reserves_for(token)[0])
    if isinstance(pool, ConcentratedPoolState):
        sp = pool.sqrt_price_x96 / Q96
        return pool.liquidity / sp if token == pool.token0 else pool.liquidity * sp
    raise TypeError(type(pool).__name__)


def scales(problem: DualProblem) -> dict[str, float]:
    """sigma_j (sigma_out = 1): a maximum-depth spanning tree grown from token_out
    (Prim): repeatedly price the unpriced token reachable through the market with the
    largest priced-side depth valued in token_out units, sigma_k * depth_k (ties:
    admitted market order); sigma_j = sigma_k * spot value of j in k. A shallow pool with
    an extreme price therefore never sets a token's scale when a deeper one exists."""
    sigma = {problem.case.token_out: 1.0}
    while True:
        best: tuple[float, int, str, str] | None = None
        for i, pid in enumerate(problem.markets):
            pool = problem.bundle.pools[pid]
            for known, other in ((pool.token0, pool.token1), (pool.token1, pool.token0)):
                if known in sigma and other not in sigma:
                    value = sigma[known] * _depth(pool, known)
                    if best is None or value > best[0]:
                        best = (value, i, known, other)
        if best is None:
            return sigma
        _, i, known, other = best
        sigma[other] = sigma[known] * _mid_value(problem.bundle.pools[problem.markets[i]], other)


def log_objective(
    problem: DualProblem, sigma: Mapping[str, float], x: Sequence[float]
) -> tuple[float, list[float], DualEval]:
    """The optimizer's function (§5.2): Phi(x) = log g(nu) with nu_j = sigma_j*exp(x_j)
    and nu_out = 1. g >= A*nu_in > 0, and log and exp are monotone, so Phi's stationary
    points are exactly g's interior minimizers. dPhi/dx_j = nu_j*(dg/dnu_j)/g: the net
    flow imbalance of token j valued at the point's own prices, as a fraction of g
    (unit-free and invariant to token decimals and to the price scale)."""
    nu = {v: sigma[v] * math.exp(xi) for v, xi in zip(problem.variables, x, strict=True)}
    ev = dual_value(problem, nu)
    grad = [nu[v] * ev.gradient[v] / ev.value for v in problem.variables]
    return math.log(ev.value), grad, ev


def projected_residual(
    x: Sequence[float], grad: Sequence[float], lower: float, upper: float
) -> float:
    """L-BFGS-B's stationarity measure on the box [lower, upper]: max_j |proj g_j|."""
    worst = 0.0
    for xi, g in zip(x, grad, strict=True):
        worst = max(worst, abs(min(max(xi - g, lower), upper) - xi))
    return worst


class EvaluationCapReached(Exception):  # noqa: N818 -- a declared cap, not an error
    """Raised instead of any objective evaluation beyond `max_function_evaluations`."""


@dataclass
class EvaluationBudget:
    """`max_function_evaluations` of one solve attempt (§5.3): every Phi/gradient
    evaluation (|M| oracle calls each) of the initial solve, the restricted re-solve and
    the final point is charged here; the re-solve gets only the remainder (no reset)."""

    cap: int
    used: int = 0
    oracle_calls: int = 0

    @property
    def remaining(self) -> int:
        return self.cap - self.used


class GuardedObjective:
    """The optimizer's function with a hard evaluation guard (§5.3). SciPy checks `maxfun`
    only between iterations and finishes line searches past it, so the guard itself
    refuses the evaluation beyond the cap by raising `EvaluationCapReached`. Identical
    points are served from a cache (pure function, not charged). `final` returns the
    reported point without a hidden extra evaluation."""

    def __init__(
        self, problem: DualProblem, sigma: Mapping[str, float], budget: EvaluationBudget
    ) -> None:
        self.problem, self.sigma, self.budget = problem, sigma, budget
        self.cache: dict[tuple[float, ...], tuple[float, list[float], DualEval]] = {}
        self.best: tuple[float, ...] | None = None
        self.evaluations = 0
        self.fired = False

    def __call__(self, x: Sequence[float]) -> tuple[float, list[float]]:
        key = tuple(float(t) for t in x)
        hit = self.cache.get(key)
        if hit is not None:
            return hit[0], hit[1]
        if self.budget.remaining <= 0:
            self.fired = True
            raise EvaluationCapReached(f"max_function_evaluations {self.budget.cap} reached")
        self.budget.used += 1
        self.evaluations += 1
        phi, grad, ev = log_objective(self.problem, self.sigma, key)
        self.budget.oracle_calls += ev.oracle_calls
        if not math.isfinite(phi) or not all(math.isfinite(g) for g in grad):
            raise FloatingPointError("non-finite dual value")
        self.cache[key] = (phi, grad, ev)
        if self.best is None or phi < self.cache[self.best][0]:
            self.best = key  # ties keep the earlier point
        return phi, grad

    def final(self, x: Sequence[float] | None) -> tuple[list[float], float, list[float], DualEval]:
        """The reported point: the optimizer's `x` when given and already evaluated
        (reused) or still affordable (charged); otherwise the lowest-Phi evaluated point.
        Requires at least one evaluation."""
        if x is not None:
            key = tuple(float(t) for t in x)
            if key in self.cache or self.budget.remaining > 0:
                phi, grad = self(key)
                return list(key), phi, grad, self.cache[key][2]
        if self.best is None:
            raise EvaluationCapReached("no evaluation was affordable")
        phi, grad, ev = self.cache[self.best]
        return list(self.best), phi, grad, ev


# --------------------------------------------------------------------------- recovery


@dataclass(frozen=True)
class RecoveryOptions:
    min_split_share: float
    max_recovery_attempts: int
    cycle_resolve: bool
    max_quotes: int | None = None  # remaining quote budget of the attempt


@dataclass
class Recovery:
    """Outcome of `recover`. `plan`/`evaluation` are set only on success; `failure` is a
    §6.6 code otherwise. Counters are the §5.2 units this stage produces."""

    plan: RoutePlan | None = None
    evaluation: Evaluation | None = None
    failure: str | None = None
    attempts: int = 0
    cycle_removed: list[str] = field(default_factory=list)
    pruned: list[tuple[str, str]] = field(default_factory=list)
    resolved: bool = False
    resolve_skipped: bool = False
    support: tuple[str, ...] = ()
    flows: tuple[PoolFlow, ...] = ()
    quotes_executed: int = 0
    quotes_memoized: int = 0
    admission_checks: int = 0


Resolve = Callable[[Mapping[str, str]], Sequence[Trade] | None]


class ResolveBudgetExhausted(Exception):  # noqa: N818 -- a declared budget outcome
    """The shared numeric budget has no evaluation or iteration left for the re-solve;
    recovery then projects the cycle-broken support without re-solving (§6 step 3)."""


def _support(trades: Sequence[Trade], min_share: float) -> dict[str, Trade]:
    """Markets whose continuous input is positive and at least `min_share` of their
    input token's total continuous outflow."""
    out_total: dict[str, list[float]] = {}
    for t in trades:
        out_total.setdefault(t.token_in, []).append(t.amount_in)
    totals = {k: math.fsum(v) for k, v in out_total.items()}
    return {
        t.pool_id: t
        for t in trades
        if t.amount_in > 0.0 and t.amount_in >= min_share * totals[t.token_in]
    }


def _relevant(edges: Mapping[str, Trade], source: str, target: str) -> dict[str, Trade]:
    """Keep only markets on some directed source -> target path of `edges`."""
    fwd: dict[str, set[str]] = {}
    back: dict[str, set[str]] = {}
    for t in edges.values():
        fwd.setdefault(t.token_in, set()).add(t.token_out)
        back.setdefault(t.token_out, set()).add(t.token_in)

    def reach(start: str, adj: Mapping[str, set[str]]) -> set[str]:
        seen, stack = {start}, [start]
        while stack:
            for nxt in adj.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    from_s, to_t = reach(source, fwd), reach(target, back)
    return {
        pid: t
        for pid, t in edges.items()
        if t.token_in in from_s and t.token_out in to_t and t.token_in != target
    }


def _find_cycle(edges: Mapping[str, Trade]) -> list[str] | None:
    """The market ids of one directed token cycle, found by DFS in sorted token order
    (ties: admitted market order), or None."""
    adj: dict[str, list[Trade]] = {}
    for t in edges.values():
        adj.setdefault(t.token_in, []).append(t)
    state: dict[str, int] = {}
    for root in sorted(adj):
        if root in state:
            continue
        path: list[Trade] = []
        tokens = [root]
        stack = [iter(adj.get(root, []))]
        state[root] = 1
        while stack:
            nxt = next(stack[-1], None)
            if nxt is None:
                state[tokens.pop()] = 2
                stack.pop()
                if path:
                    path.pop()
                continue
            if state.get(nxt.token_out) == 1:
                start = tokens.index(nxt.token_out)
                return [t.pool_id for t in path[start:]] + [nxt.pool_id]
            if nxt.token_out not in state:
                state[nxt.token_out] = 1
                tokens.append(nxt.token_out)
                path.append(nxt)
                stack.append(iter(adj.get(nxt.token_out, [])))
    return None


def _break_cycles(
    edges: dict[str, Trade], nu: Mapping[str, float], order: Mapping[str, int], rec: Recovery
) -> dict[str, Trade]:
    """Remove, per found cycle, the market with the smallest continuous input value
    nu_in * amount_in (ties: the later admitted market) until the digraph is acyclic."""
    while True:
        rec.admission_checks += 1
        cycle = _find_cycle(edges)
        if cycle is None:
            return edges
        victim = min(
            cycle,
            key=lambda pid: (nu[edges[pid].token_in] * edges[pid].amount_in, -order[pid]),
        )
        rec.cycle_removed.append(victim)
        del edges[victim]


def _cascade(edges: dict[str, Trade], source: str, target: str) -> None:
    """After pruning, drop markets that no longer lie on a source -> target path."""
    keep = _relevant(edges, source, target)
    for pid in list(edges):
        if pid not in keep:
            del edges[pid]


def _topological(edges: Mapping[str, Trade], source: str, order: Mapping[str, int]) -> list[str]:
    first: dict[str, int] = {source: -1}
    for pid in sorted(edges, key=order.__getitem__):
        t = edges[pid]
        first.setdefault(t.token_in, order[pid])
        first.setdefault(t.token_out, order[pid])
    indeg = dict.fromkeys(first, 0)
    for t in edges.values():
        indeg[t.token_out] += 1
    ready = sorted((t for t, d in indeg.items() if d == 0), key=lambda t: (first[t], t))
    out: list[str] = []
    while ready:
        tok = ready.pop(0)
        out.append(tok)
        for t in edges.values():
            if t.token_in == tok:
                indeg[t.token_out] -= 1
                if indeg[t.token_out] == 0:
                    ready.append(t.token_out)
        ready.sort(key=lambda t: (first[t], t))
    return out


def _project(
    bundle: SnapshotBundle,
    case: Case,
    edges: Mapping[str, Trade],
    order: Mapping[str, int],
    cache: QuoteCache,
    max_quotes: int | None,
) -> tuple[list[PoolFlow], tuple[str, str] | None]:
    """One share-projection attempt: tokens in topological order; each token's exact
    integer inflow is split over its out-markets (admitted order) by the continuous
    shares, floor for all but the last, the last taking the exact remainder. Every
    emitted leg is quoted exactly. Returns the integer flows, or the first failing
    (market, reason)."""
    inflow: dict[str, int] = {case.token_in: case.amount_in}
    flows: list[PoolFlow] = []
    for token in _topological(edges, case.token_in, order):
        outs = sorted(
            (t for t in edges.values() if t.token_in == token), key=lambda t: order[t.pool_id]
        )
        amount = inflow.get(token, 0)
        if not outs or amount == 0:
            continue
        weights = [Fraction(t.amount_in) for t in outs]
        total = sum(weights, Fraction(0))
        legs: list[int] = []
        for w in weights[:-1]:
            legs.append(math.floor(amount * w / total))
        legs.append(amount - sum(legs))
        for t, leg in zip(outs, legs, strict=True):
            if leg == 0:
                continue
            if max_quotes is not None and not cache.cached(t.pool_id, token, leg):
                if cache.misses >= max_quotes:
                    return flows, (t.pool_id, "quote_budget")
            result = cache(bundle.pools[t.pool_id], token, leg)
            if result.status is not QuoteStatus.OK:
                return flows, (t.pool_id, result.status.value)
            if result.amount_out == 0:
                return flows, (t.pool_id, "zero_output")
            flows.append(
                PoolFlow(
                    Edge(t.pool_id, token, t.token_out), leg, result.amount_out, order[t.pool_id]
                )
            )
            inflow[t.token_out] = inflow.get(t.token_out, 0) + result.amount_out
    return flows, None


def recover(
    bundle: SnapshotBundle,
    case: Case,
    markets: Sequence[str],
    trades: Sequence[Trade],
    nu: Mapping[str, float],
    options: RecoveryOptions,
    cache: QuoteCache,
    resolve: Resolve | None = None,
) -> Recovery:
    """§6: support -> relevance -> cycle breaking (+ optional restricted re-solve) ->
    share projection with deterministic prune-and-retry -> `merged_plan` -> independent
    replay by `routing.evaluator.evaluate` (memoized quotes)."""
    rec = Recovery()
    order = {pid: i for i, pid in enumerate(markets)}
    nu_full = {**nu, case.token_out: 1.0}
    if any(not (math.isfinite(t.amount_in) and math.isfinite(t.amount_out)) for t in trades):
        rec.failure = "numeric_failure"
        return rec
    edges = _relevant(_support(trades, options.min_split_share), case.token_in, case.token_out)
    edges = _break_cycles(edges, nu_full, order, rec)
    resolved: Sequence[Trade] | None = None
    if rec.cycle_removed and options.cycle_resolve and resolve is not None:
        allowed = {pid: edges[pid].token_in for pid in sorted(edges, key=order.__getitem__)}
        try:
            resolved = resolve(allowed)
        except ResolveBudgetExhausted:
            rec.resolve_skipped = True
        else:
            rec.resolved = True
            if resolved is None:
                rec.failure = "resolve_failed"
                return rec
            edges = _relevant(
                _support([t for t in resolved if t.pool_id in allowed], options.min_split_share),
                case.token_in,
                case.token_out,
            )
    if not rec.resolved:
        _cascade(edges, case.token_in, case.token_out)
    rec.support = tuple(sorted(edges, key=order.__getitem__))
    if not edges:
        rec.failure = "empty_support"
        return rec
    while True:
        if rec.attempts >= options.max_recovery_attempts:
            rec.failure = "attempts_exhausted"
            break
        rec.attempts += 1
        flows, failed = _project(bundle, case, edges, order, cache, options.max_quotes)
        if failed is None:
            plan = merged_plan(case, flows)
            ev = evaluate(bundle, case, plan, gross_only(), quote=cache)
            gross = sum(f.amount_out for f in flows if f.edge.token_out == case.token_out)
            if ev.status is not EvalStatus.OK or ev.gross_output != gross:
                raise AssertionError(f"recovered plan failed replay: {ev.error}")
            rec.plan, rec.evaluation, rec.flows = plan, ev, tuple(flows)
            break
        pid, reason = failed
        if reason == "quote_budget":
            rec.failure = "quote_budget"
            break
        rec.pruned.append(failed)
        del edges[pid]
        _cascade(edges, case.token_in, case.token_out)
        if not edges:
            rec.failure = "support_exhausted"
            break
    rec.quotes_executed, rec.quotes_memoized = cache.misses, cache.hits
    return rec

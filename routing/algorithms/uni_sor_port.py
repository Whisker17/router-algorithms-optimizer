# SPDX-License-Identifier: GPL-3.0-only
#
# Translated from Uniswap/smart-order-router@04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647 (4.31.10):
#   src/routers/alpha-router/functions/compute-all-routes.ts
#   src/routers/alpha-router/functions/best-swap-route.ts
#   src/routers/alpha-router/alpha-router.ts (getAmountDistribution)
#   plus the quote-list assembly order of quoters/{v3,v2,mixed}-quoter.ts and the
#   V2/V3/MixedRouteWithValidQuote constructors (entities/route-with-valid-quote.ts).
# Copyright (C) Uniswap Labs. Licensed under the GNU General Public License v3; the GPL
# text at the pin is copied verbatim to
# docs/references/licenses/uniswap-smart-order-router-04c7c0b4-LICENSE.txt.
# MODIFIED: translated from TypeScript to Python, restricted to the exact-input V2/V3
# routing core, with the A-1...A-8 adaptations and D-1...D-4 deviations declared in
# docs/references/uni-sor-port-contract.md (§§4-5). Private internal research use; not
# conveyed (contract §9.3).
#
# `v8_small_array_sort` is translated from V8 13.6.233.17
# third_party/v8/builtins/array-sort.tq (macros CountAndMakeRun, BinaryInsertionSort,
# ReverseRange); Copyright Python Software Foundation; PSF-2.0 -- verbatim license:
# docs/references/licenses/v8-13.6.233.17-third_party-builtins-LICENSE.txt.
"""`uni_sor_port` (docs/DESIGN.md §2.7): the mandatory, parity-tested Python port of the
pinned Uniswap Smart Order Router's exact-input V2/V3 routing core.

Contract of record: `docs/references/uni-sor-port-contract.md` (WHI-1442). Behaviour ids
(`B-*`), adaptations (`A-*`) and deviations (`D-*`) below are the contract's. Parity is
proven by `tests/routing/test_uni_sor_parity.py` against every golden the actual pinned
upstream functions produced (WHI-1443, `tests/fixtures/uni_sor/`).

The module has two layers:

1. **The translated routing core** (pure; no benchmark types): `compute_all_routes`
   (B-R1...B-R8), `amount_distribution` (B-A1), `build_route_quotes` (B-Q1...B-Q5),
   `get_best_swap_route` / `get_best_swap_route_by` / `find_first_route_not_using_used_pools`
   (B-S1...B-S12, B-F1, B-F2) and `v8_small_array_sort` (B-F1). Given identical ordered
   candidate pools, percentage grid, (route, percent) quote table and abstract gas
   scores, it returns upstream's enumerated routes, quote list and selection -- routes,
   percents, exact rational amounts, final order and cached totals -- bit for bit.
2. **The benchmark adapter** (`prepare` / `solve`, registered as `uni_sor_port`), which
   supplies those inputs from the frozen bundle and turns the selection into a plan:

   - **A-1 candidates:** the bundle's SOR-compatible pools -- every pool whose catalog
     source has a `sor_protocol` (`config/protocols.yaml`: Agni/FusionX/Uniswap v3 =
     `V3`, Merchant Moe Classic = `V2`; a source-free generic constant-product pool of
     the synthetic suite = `V2`), per family in ascending lowercase `pool_id` order.
     Liquidity Book pools never enter (D-4). Pool identity is the verified `pool_id`
     (A-5 / D-2).
   - **A-2 quotes:** every `(route, percent)` entry chains `pools.quote.quote_exact_in`
     through the route's pools on the frozen state, independently per entry, at
     `quotient(amount x p / 100)`; `null` when that input is 0, when a hop fails (an
     incomplete-snapshot failure is counted and reported, never dropped silently), when
     a hop only partially fills, when an intermediate hop outputs 0 (the next on-chain
     hop would revert), or, on a pure-V2 route, when any hop outputs 0.
   - **A-3 gas scores:** the declared `ObjectiveContext`. Neither objective mode has a
     per-route cost -- `gross_only` has none and `synthetic_fixed_cost` charges one
     constant per plan, which cannot change the ranking of complete plans -- so every
     score is `(0, 0, 0)` and SOR selects on raw quotes (named in results as the gas
     score provider). A-4 (no L1 fee branch), A-6 (no portion), A-7 (no native routes)
     and A-8 (no logging) hold by construction.
   - **D-1 integer fill:** routes in B-F1 order; route `j < k` draws its quoted
     `quotient_j` from the request fund and the last route draws `ALL_REMAINING`
     (`quotient_k` plus the residual `amount - sum(quotient_j)`, at most `k - 1` raw
     units), so the whole input is allocated. Selection parity is judged before D-1.
   - **D-3 re-quote:** the plan is replayed by `routing.evaluator.evaluate` (and again
     independently by the runner); the difference between that gross output and
     upstream's cached `quote` (B-S11) is reported per case (`requote_delta`), next to
     -- never instead of -- the upstream-shaped selection.

**Coverage modes** (DESIGN §2.7/§2.11). On a bundle holding only SOR-compatible pools
(the matched V2/V3 cohort, `main.py corpus cohort`) every algorithm shares SOR's
candidates: `coverage_mode = "matched_cohort"`. On a bundle that also holds LB pools
(the full five-source universe) SOR still sees only its cohort, `coverage_mode =
"full_universe"`, and the excluded LB pools are counted per case.

**Statuses** (contract §6): `ok`; `unsupported` -- the cohort DFS yields no route but the
same DFS over *every* admitted pool (LB included) does (an LB-only case; never
`no_route`); `no_route` -- both DFS are empty, or no complete selection exists (B-S10);
`incomplete_snapshot` -- no selection and some entry needed uncollected state; `timeout`
-- a declared quote/candidate budget would cut the quote table short (a partial table is
not SOR's input, so no selection is made); `invalid_plan` -- only from the evaluator.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pools.result import QuoteStatus, SwapResult
from routing.algorithms import direct_split, single_path
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    Capabilities,
    SolveContext,
    SolveResult,
    SolveStatus,
)
from routing.algorithms.path_split import split_path_plan
from routing.evaluator import EvalStatus, evaluate
from routing.search import Edge, QuoteCache
from snapshot.models import (
    Case,
    ConcentratedPoolState,
    ConstantProductPoolState,
    PoolState,
    SnapshotBundle,
)

NAME = "uni_sor_port"

V3 = "V3"
V2 = "V2"
MIXED = "MIXED"
FAMILIES = (V3, V2, MIXED)  # B-Q1: upstream quoter order (V4 absent)

CAPABILITIES = Capabilities(multi_hop=True, split=True, protocols=(V2, V3))
SEARCH_PARAMS = ("max_hops", "max_splits", "percent_step")

# B-S12: upstream's default and the port's; not a profile key (recorded per case).
MIN_SPLITS = 1
# B-F1: `v8_small_array_sort` reproduces only V8's small-array (< 64) TimSort path.
MAX_SPLITS_LIMIT = 63

CONTRACT = "docs/references/uni-sor-port-contract.md"
UPSTREAM: Mapping[str, str] = MappingProxyType(
    {
        "repository": "https://github.com/Uniswap/smart-order-router",
        "commit": "04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647",
        "package": "@uniswap/smart-order-router",
        "version": "4.31.10",
        "npm_integrity": (
            "sha512-Bmg46KXDSfE1AAf1tPg+vDzMngVoGfzdohekhjJ1hIQG13tXcjEykqapQy+CrJUqXLL3lQvZj2uVEKfzCNH74Q=="
        ),
        "license": "GPL-3.0-only",
        "v8_sort_reference": "V8 13.6.233.17 third_party/v8/builtins/array-sort.tq (PSF-2.0)",
    }
)
ADAPTATIONS = ("A-1", "A-2", "A-3", "A-4", "A-5", "A-6", "A-7", "A-8")
DEVIATIONS = ("D-1", "D-2", "D-3", "D-4")
PORT_SCOPE = (
    "exact-input V2/V3 routing core: bounded route enumeration (V3, V2, mixed V2+V3), "
    "percentage grid, quote-list order, per-percent sort, BFS seeds/expansion, pool-"
    "disjointness, split limits and pruning, V8 final route order, remainder step. "
    "Not reproduced: candidate/quote/gas providers (replaced: A-1/A-2/A-3), V4/hooks, "
    "native routes, exact output, L1 fees, calldata, caching, Liquidity Book."
)
CANDIDATE_PROVIDER = (
    "frozen bundle's SOR-compatible pools (catalog sor_protocol; V3 and V2 lists each in "
    "ascending lowercase pool_id; mixed = V3 ++ V2), pool identity = pool_id (A-1, A-5)"
)
QUOTE_PROVIDER = (
    "benchmark simulator: pools.quote.quote_exact_in chained per route on the frozen state, "
    "independently per (route, percent) at quotient(amount*p/100) (A-2)"
)
GAS_SCORE_PROVIDER = (
    "zero: the declared ObjectiveContext has no per-route cost (gross_only: none; "
    "synthetic_fixed_cost: one plan-level constant) (A-3)"
)
PROVENANCE: Mapping[str, Any] = MappingProxyType(
    {
        "upstream": dict(UPSTREAM),
        "contract": CONTRACT,
        "port_scope": PORT_SCOPE,
        "adaptations": list(ADAPTATIONS),
        "deviations": list(DEVIATIONS),
        "candidate_provider": CANDIDATE_PROVIDER,
        "quote_provider": QUOTE_PROVIDER,
        "gas_score_provider": GAS_SCORE_PROVIDER,
        "min_splits": MIN_SPLITS,
        "parity_evidence": "tests/routing/test_uni_sor_parity.py over tests/fixtures/uni_sor/",
    }
)

_REPO = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = _REPO / "config" / "protocols.yaml"


# =====================================================================================
# 1. Translated routing core
# =====================================================================================


@dataclass(frozen=True)
class Rational:
    """sdk-core 7.10.1 `Fraction`/`CurrencyAmount` semantics: an *unreduced*
    numerator/denominator pair (upstream keeps e.g. `5050/100`), exact comparison by
    cross multiplication, and `quotient` = JSBI division (truncation toward zero; the
    same as floor for the non-negative amounts in scope)."""

    numerator: int
    denominator: int = 1

    def __post_init__(self) -> None:
        if self.denominator <= 0:
            raise ValueError(f"denominator must be positive, got {self.denominator}")

    @property
    def quotient(self) -> int:
        q = abs(self.numerator) // self.denominator
        return q if self.numerator >= 0 else -q

    def add(self, other: Rational) -> Rational:
        if self.denominator == other.denominator:
            return Rational(self.numerator + other.numerator, self.denominator)
        return Rational(
            self.numerator * other.denominator + other.numerator * self.denominator,
            self.denominator * other.denominator,
        )

    def subtract(self, other: Rational) -> Rational:
        if self.denominator == other.denominator:
            return Rational(self.numerator - other.numerator, self.denominator)
        return Rational(
            self.numerator * other.denominator - other.numerator * self.denominator,
            self.denominator * other.denominator,
        )

    def greater_than(self, other: Rational) -> bool:
        return self.numerator * other.denominator > other.numerator * self.denominator

    def to_dict(self) -> dict[str, str]:
        return {
            "numerator": str(self.numerator),
            "denominator": str(self.denominator),
            "quotient": str(self.quotient),
        }


@dataclass(frozen=True)
class SorPool:
    """One candidate pool as the routing core sees it: identity (A-5: the verified
    `pool_id`), lowercase token addresses, route protocol and (V3) fee."""

    pool_id: str
    token0: str
    token1: str
    protocol: str  # V3 | V2
    fee: int | None = None

    def involves(self, token: str) -> bool:
        return token in (self.token0, self.token1)

    def other(self, token: str) -> str:
        # compute-all-routes.ts: `curPool.token0.equals(previousTokenOut) ? token1 : token0`
        return self.token1 if self.token0 == token else self.token0


@dataclass(frozen=True)
class SorRoute:
    protocol: str  # V3 | V2 | MIXED
    pools: tuple[SorPool, ...]
    token_path: tuple[str, ...]

    @property
    def pool_ids(self) -> tuple[str, ...]:
        return tuple(p.pool_id for p in self.pools)


def compute_all_routes(
    token_in: str, token_out: str, pools: Sequence[SorPool], max_hops: int
) -> list[tuple[SorPool, ...]]:
    """`computeAllRoutes` (compute-all-routes.ts:161-271; B-R1...B-R6): recursive DFS over
    the candidate list in index order, skipping pools already on the current path
    (identity = list index), with `tokensVisited` seeded with lowercase `tokenIn`; the hop
    check (`length > maxHops`) runs before the terminal check (the last pool involves
    `tokenOut`), which emits the path and stops extending it. Routes are returned in DFS
    pre-order.

    Two equivalence-preserving shortcuts, neither of which changes the emitted sequence:
    candidate pools at each depth are drawn from a per-token index list (in the same
    ascending index order) instead of scanning every pool and rejecting those that do not
    involve the previous token, and a path already `max_hops` long is not extended (every
    extension would return immediately at the hop check)."""
    t_in, t_out = token_in.lower(), token_out.lower()
    by_token: dict[str, list[int]] = {}
    for i, pool in enumerate(pools):
        for token in dict.fromkeys((pool.token0, pool.token1)):
            by_token.setdefault(token, []).append(i)
    used = [False] * len(pools)
    visited = {t_in}
    current: list[SorPool] = []
    routes: list[tuple[SorPool, ...]] = []

    def compute(previous_token_out: str) -> None:
        if len(current) > max_hops:
            return
        if current and current[-1].involves(t_out):
            routes.append(tuple(current))
            return
        if len(current) == max_hops:
            return  # shortcut: every extension fails the hop check above
        for i in by_token.get(previous_token_out, ()):
            if used[i]:
                continue
            pool = pools[i]
            current_token_out = pool.other(previous_token_out)
            if current_token_out in visited:
                continue
            visited.add(current_token_out)
            current.append(pool)
            used[i] = True
            compute(current_token_out)
            used[i] = False
            current.pop()
            visited.discard(current_token_out)

    compute(t_in)
    return routes


def _token_path(token_in: str, pools: Sequence[SorPool]) -> tuple[str, ...]:
    path = [token_in.lower()]
    for pool in pools:
        path.append(pool.other(path[-1]))
    return tuple(path)


def compute_family_routes(
    family: str,
    token_in: str,
    token_out: str,
    v3_pools: Sequence[SorPool],
    v2_pools: Sequence[SorPool],
    max_hops: int,
) -> list[SorRoute]:
    """`computeAllV3Routes` / `computeAllV2Routes` (compute-all-routes.ts:52-86; B-R7:
    each family only from its own list) and the V2+V3 part of `computeAllMixedRoutes`
    (:88-159; B-R8: the DFS over `V3 ++ V2` in that order, then routes whose pools are
    all V3 or all V2 are filtered out, keeping DFS order)."""
    if family == V3:
        raw = compute_all_routes(token_in, token_out, v3_pools, max_hops)
    elif family == V2:
        raw = compute_all_routes(token_in, token_out, v2_pools, max_hops)
    elif family == MIXED:
        raw = [
            r
            for r in compute_all_routes(token_in, token_out, [*v3_pools, *v2_pools], max_hops)
            if not all(p.protocol == V3 for p in r) and not all(p.protocol == V2 for p in r)
        ]
    else:
        raise ValueError(f"unknown route family {family!r}")
    return [SorRoute(family, r, _token_path(token_in, r)) for r in raw]


def amount_distribution(amount: int, distribution_percent: int) -> tuple[list[int], list[Rational]]:
    """`AlphaRouter.getAmountDistribution` (alpha-router.ts:3348-3362; B-A1):
    `for (i = 1; i <= 100 / d; i++)` with JS float division (Python `/` on IEEE doubles
    is the same operation), each amount the exact unreduced rational
    `amount x (i*d) / 100`. A step that does not divide 100 has no 100 % entry."""
    percents: list[int] = []
    amounts: list[Rational] = []
    i = 1
    while i <= 100 / distribution_percent:
        percents.append(i * distribution_percent)
        amounts.append(Rational(amount * (i * distribution_percent), 100))
        i += 1
    return percents, amounts


@dataclass(frozen=True)
class GasScore:
    """A-3 abstract gas score of one `(route, percent)` entry: `gas_estimate`,
    `gas_cost_in_token` (output-token raw units) and a reporting-only USD figure."""

    gas_estimate: int = 0
    gas_cost_in_token: int = 0
    gas_cost_in_usd: int = 0


ZERO_GAS = GasScore()


@dataclass(frozen=True)
class RouteQuote:
    """`V2/V3/MixedRouteWithValidQuote` (route-with-valid-quote.ts:89-160, 183-262,
    389-...; B-Q3...B-Q5): the entry's percent and grid amount, the raw quote and gas score,
    `quoteAdjustedForGas = quote - gasCostInToken` for exact input (may be negative),
    and one pool identifier per hop. `list_index` is its position in the quote list."""

    list_index: int
    route: SorRoute
    percent: int
    amount: Rational
    raw_quote: int
    gas: GasScore = ZERO_GAS

    @property
    def protocol(self) -> str:
        return self.route.protocol

    @property
    def quote(self) -> int:
        return self.raw_quote  # A-6: no portion is taken for exact input

    @property
    def quote_adjusted_for_gas(self) -> int:
        return self.raw_quote - self.gas.gas_cost_in_token

    @property
    def pool_identifiers(self) -> tuple[str, ...]:
        return self.route.pool_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "list_index": self.list_index,
            "protocol": self.protocol,
            "pool_ids": list(self.pool_identifiers),
            "percent": self.percent,
            "raw_quote": str(self.raw_quote),
            "quote": str(self.quote),
            "quote_adjusted_for_gas": str(self.quote_adjusted_for_gas),
            "gas_estimate": str(self.gas.gas_estimate),
            "gas_cost_in_token": str(self.gas.gas_cost_in_token),
            "gas_cost_in_usd": str(self.gas.gas_cost_in_usd),
        }


QuoteFn = Callable[[SorRoute, int, Rational], int | None]
GasFn = Callable[[SorRoute, int, Rational, int], GasScore]


def build_route_quotes(
    routes: Mapping[str, Sequence[SorRoute]],
    percents: Sequence[int],
    amounts: Sequence[Rational],
    quote: QuoteFn,
    gas: GasFn | None = None,
) -> list[RouteQuote]:
    """The `routesWithValidQuotes` list (B-Q1...B-Q3): family blocks V3, V2, MIXED in the
    `quotePromises.push` order of alpha-router.ts:2916-3058 (flattened in order by
    `Promise.all`); within a block, route order then percent ascending
    (v3-quoter.ts:203-248, v2-quoter.ts:224-255, mixed-quoter.ts:271-...). An entry whose
    quote is `None` is dropped; a numeric zero is kept (a `BigNumber` is truthy)."""
    out: list[RouteQuote] = []
    for family in FAMILIES:
        for route in routes.get(family, ()):
            for percent, amount in zip(percents, amounts, strict=True):
                raw = quote(route, percent, amount)
                if raw is None:
                    continue
                score = gas(route, percent, amount, raw) if gas is not None else ZERO_GAS
                out.append(RouteQuote(len(out), route, percent, amount, raw, score))
    return out


def v8_small_array_sort[T](items: Sequence[T], compare: Callable[[T, T], int]) -> list[T]:
    """V8's `Array.prototype.sort` for arrays shorter than 64 (array-sort.tq at
    13.6.233.17; B-F1): `ComputeMinRunLength(n) == n` below 64, so TimSort performs one
    `CountAndMakeRun` from index 0 -- a leading strictly-descending run (`order < 0`
    between neighbours, so a comparator that never returns >= 0 makes equal elements
    "descending") is reversed in place -- and then `BinaryInsertionSort`s the rest.
    Only `order < 0` is ever tested. Returns the sorted copy."""
    work = list(items)
    n = len(work)
    if n >= 64:
        raise ValueError(f"v8_small_array_sort covers only arrays shorter than 64, got {n}")
    if n < 2:
        return work

    # CountAndMakeRun(lowArg = 0, high = n)
    run_length = 2
    order = compare(work[1], work[0])
    is_descending = order < 0
    previous = work[1]
    for idx in range(2, n):
        current = work[idx]
        order = compare(current, previous)
        if is_descending:
            if order >= 0:
                break
        elif order < 0:
            break
        previous = current
        run_length += 1
    if is_descending:
        work[0:run_length] = work[0:run_length][::-1]  # ReverseRange(0, runLength)

    # BinaryInsertionSort(low = 0, startArg = runLength, high = n)
    for start in range(run_length, n):
        left, right = 0, start
        pivot = work[start]
        while left < right:
            mid = left + ((right - left) >> 1)
            if compare(pivot, work[mid]) < 0:
                right = mid
            else:
                left = mid + 1
        for p in range(start, left, -1):
            work[p] = work[p - 1]
        work[left] = pivot
    return work


def find_first_route_not_using_used_pools(
    used_routes: Sequence[RouteQuote], candidates: Sequence[RouteQuote]
) -> RouteQuote | None:
    """`findFirstRouteNotUsingUsedPools` (best-swap-route.ts:821-881; B-S9): the first
    candidate none of whose pool identifiers is used by any route so far. The
    native/wrapped-native exclusion cannot trigger without native routes (A-7) and the
    `forceCrossProtocol` branch is disabled (rejected by `get_best_swap_route`)."""
    used = {pid for r in used_routes for pid in r.pool_identifiers}
    for candidate in candidates:
        if any(pid in used for pid in candidate.pool_identifiers):
            continue
        return candidate
    return None


@dataclass(frozen=True)
class _Node:
    cur_routes: tuple[RouteQuote, ...]
    percent_index: int
    remaining_percent: int
    special: bool


@dataclass(frozen=True)
class BestSwap:
    """What `getBestSwapRouteBy` returns: the selected routes in B-F1 order, the
    cached totals (B-S11) and, for diagnostics, the per-percent sorted groups."""

    routes: tuple[RouteQuote, ...]
    quote: int
    quote_gas_adjusted: int
    estimated_gas_used: int
    estimated_gas_used_quote_token: int
    estimated_gas_used_usd: int
    sorted_by_percent: Mapping[int, tuple[RouteQuote, ...]]


def get_best_swap_route_by(
    percent_to_quotes: Mapping[int, Sequence[RouteQuote]],
    percents: Sequence[int],
    *,
    min_splits: int,
    max_splits: int,
    by: Callable[[RouteQuote], int] = lambda r: r.quote_adjusted_for_gas,
) -> BestSwap | None:
    """`getBestSwapRouteBy` (best-swap-route.ts:174-817) for exact input."""
    # B-S2: `(a, b) => by(a).greaterThan(by(b)) ? -1 : 1` under V8 TimSort, which only
    # ever tests `order < 0`: a stable sort by `by` descending (contract §3.3 permits a
    # stable sort here; ties keep B-Q1 order).
    sorted_groups = {
        percent: tuple(sorted(quotes, key=lambda r: -by(r)))
        for percent, quotes in percent_to_quotes.items()
    }
    best_quote: int | None = None
    best_swap: tuple[RouteQuote, ...] | None = None

    # B-S3: the 100 % baseline, unless there is no 100 % group or minSplits > 1.
    if 100 in sorted_groups and min_splits <= 1:
        best_quote = by(sorted_groups[100][0])
        best_swap = (sorted_groups[100][0],)

    # B-S4: seeds, `for (i = percents.length; i >= 0; i--)`; index `percents.length` is
    # undefined and skipped. The best entry, then (`special`) the second best.
    queue: deque[_Node] = deque()
    for i in range(len(percents), -1, -1):
        if i >= len(percents) or percents[i] not in sorted_groups:
            continue
        group = sorted_groups[percents[i]]
        queue.append(_Node((group[0],), i, 100 - percents[i], False))
        if len(group) < 2:
            continue
        queue.append(_Node((group[1],), i, 100 - percents[i], True))

    splits = 1
    while queue:
        layer = len(queue)  # B-S5: only nodes queued when the layer starts
        splits += 1
        # B-S5 pruning, then the split cap, in this order.
        if splits >= 3 and best_swap is not None and len(best_swap) < splits - 1:
            break
        if splits > max_splits:
            break
        while layer > 0:
            layer -= 1
            node = queue.popleft()
            # B-S6: percents from the node's index down; skip too-large or empty ones.
            for i in range(node.percent_index, -1, -1):
                percent_a = percents[i]
                if percent_a > node.remaining_percent:
                    continue
                if percent_a not in sorted_groups:
                    continue
                # B-S7 / B-S9: only the first non-overlapping entry of this percent.
                route_a = find_first_route_not_using_used_pools(
                    node.cur_routes, sorted_groups[percent_a]
                )
                if route_a is None:
                    continue
                remaining_new = node.remaining_percent - percent_a
                routes_new = (*node.cur_routes, route_a)
                # B-S8: complete with >= minSplits routes -> strictly-better check
                # (gasCostL1QuoteToken is 0 under A-4); otherwise enqueue.
                if remaining_new == 0 and splits >= min_splits:
                    quote_new = sum(by(r) for r in routes_new)
                    if best_quote is None or quote_new > best_quote:
                        best_quote = quote_new
                        best_swap = routes_new
                else:
                    queue.append(_Node(routes_new, i, remaining_new, node.special))

    if best_swap is None:
        return None  # B-S10

    # B-F1: `bestSwap.sort((A, B) => B.amount.greaterThan(A.amount) ? 1 : -1)`.
    final = v8_small_array_sort(
        best_swap, lambda a, b: 1 if b.amount.greater_than(a.amount) else -1
    )
    return BestSwap(
        routes=tuple(final),
        # B-S11: cached, not re-quoted.
        quote=sum(r.quote for r in best_swap),
        quote_gas_adjusted=sum(r.quote_adjusted_for_gas for r in best_swap),
        estimated_gas_used=sum(r.gas.gas_estimate for r in best_swap),
        estimated_gas_used_quote_token=sum(r.gas.gas_cost_in_token for r in best_swap),
        estimated_gas_used_usd=sum(r.gas.gas_cost_in_usd for r in best_swap),
        sorted_by_percent=MappingProxyType(sorted_groups),
    )


@dataclass(frozen=True)
class SwapSelection:
    """`getBestSwapRoute`'s result: the B-F1-ordered routes with their (possibly
    remainder-adjusted, B-F2) exact amounts, the missing amount and the cached totals."""

    swap: BestSwap
    amounts: tuple[Rational, ...]
    missing_amount: Rational
    remainder_added: bool

    @property
    def routes(self) -> tuple[RouteQuote, ...]:
        return self.swap.routes

    @property
    def sum_of_quotients(self) -> int:
        return sum(a.quotient for a in self.amounts)

    def to_dict(self) -> dict[str, Any]:
        """The upstream-shaped selection (contract §6 result metadata)."""
        return {
            "routes": [
                {
                    **r.to_dict(),
                    "token_path": list(r.route.token_path),
                    "amount": a.to_dict(),
                }
                for r, a in zip(self.routes, self.amounts, strict=True)
            ],
            "missing_amount": self.missing_amount.to_dict(),
            "remainder_added": self.remainder_added,
            "sum_of_quotients": str(self.sum_of_quotients),
            "quote": str(self.swap.quote),
            "quote_gas_adjusted": str(self.swap.quote_gas_adjusted),
            "estimated_gas_used": str(self.swap.estimated_gas_used),
            "estimated_gas_used_quote_token": str(self.swap.estimated_gas_used_quote_token),
            "estimated_gas_used_usd": str(self.swap.estimated_gas_used_usd),
        }


def get_best_swap_route(
    amount: int,
    percents: Sequence[int],
    route_quotes: Sequence[RouteQuote],
    *,
    min_splits: int = MIN_SPLITS,
    max_splits: int,
    force_cross_protocol: bool = False,
    force_mixed_routes: bool = False,
) -> SwapSelection | None:
    """`getBestSwapRoute` (best-swap-route.ts:42-172) for exact input: B-S1 grouping by
    percent in list order, `getBestSwapRouteBy` on `quoteAdjustedForGas`, then the B-F2
    remainder step on exact rationals -- `missingAmount = amount - sum(route.amount)` is
    added to the last route of the B-F1 array only if positive (on an exact grid it is
    always 0; D-1 is the benchmark's integer fill, applied separately)."""
    if force_cross_protocol or force_mixed_routes:
        raise ValueError("forceCrossProtocol/forceMixedRoutes are outside the parity boundary")
    if not 1 <= max_splits <= MAX_SPLITS_LIMIT:
        raise ValueError(
            f"max_splits must be in 1..{MAX_SPLITS_LIMIT} (B-F1 reproduces V8's small-array "
            f"sort only), got {max_splits}"
        )
    groups: dict[int, list[RouteQuote]] = {}
    for rq in route_quotes:
        groups.setdefault(rq.percent, []).append(rq)
    swap = get_best_swap_route_by(groups, percents, min_splits=min_splits, max_splits=max_splits)
    if swap is None:
        return None
    amounts = [r.amount for r in swap.routes]
    total = Rational(0)
    for a in amounts:
        total = total.add(a)
    missing = Rational(amount).subtract(total)
    remainder_added = missing.greater_than(Rational(0))
    if remainder_added:
        amounts[-1] = amounts[-1].add(missing)
    return SwapSelection(swap, tuple(amounts), missing, remainder_added)


def integer_fill(amount: int, selection: SwapSelection) -> tuple[int, ...]:
    """D-1: routes `j < k` receive `quotient_j`, the last route the rest of the input
    (`quotient_k + residual`, `residual = amount - sum(quotient_j)` in `[0, k-1]`)."""
    head = [a.quotient for a in selection.amounts[:-1]]
    return (*head, amount - sum(head))


# =====================================================================================
# 2. Benchmark adapter
# =====================================================================================


class UniSorPortConfigError(ValueError):
    """`prepare` received an invalid or missing `search.*` value, or the bundle's pools
    cannot be mapped to SOR protocols consistently."""


def sor_protocol_of(state: PoolState, sor_by_source: Mapping[str, str]) -> str | None:
    """The SOR route protocol a pool enters the cohort as (A-1), or `None` (never SOR-
    routable: Liquidity Book, or any source without a catalog `sor_protocol`)."""
    source = getattr(state, "source_key", None)
    if source is None:
        # The synthetic correctness suite's source-free generic constant-product pool
        # uses the Uniswap V2 formula itself.
        return V2 if isinstance(state, ConstantProductPoolState) else None
    protocol = sor_by_source.get(source)
    if protocol == V3 and not isinstance(state, ConcentratedPoolState):
        raise UniSorPortConfigError(f"{state.pool_id}: source {source} is V3 but not a CL pool")
    if protocol == V2 and not isinstance(state, ConstantProductPoolState):
        raise UniSorPortConfigError(f"{state.pool_id}: source {source} is V2 but not a CPMM")
    return protocol


def _sor_pool(state: PoolState, protocol: str) -> SorPool:
    fee = state.fee if isinstance(state, ConcentratedPoolState) else None
    return SorPool(state.pool_id, state.token0.lower(), state.token1.lower(), protocol, fee)


@dataclass(frozen=True)
class PreparedUniSorPort:
    """Immutable per-worker preparation: routing parameters, the per-family cohort
    lists in A-1 order, the full admitted universe (for the `unsupported` check) and the
    excluded (non-SOR) pools by source."""

    max_hops: int
    max_splits: int
    percent_step: int
    v3_pools: tuple[SorPool, ...]
    v2_pools: tuple[SorPool, ...]
    universe: tuple[SorPool, ...]
    excluded_by_source: Mapping[str, int] = field(default_factory=dict)

    @property
    def coverage_mode(self) -> str:
        return "full_universe" if self.excluded_by_source else "matched_cohort"


def _sor_by_source(catalog_path: Path) -> dict[str, str]:
    from snapshot.config import load_catalog

    return load_catalog(catalog_path).sor_protocols()


def prepare(
    bundle: SnapshotBundle, config: AlgorithmConfig, *, catalog: Path = DEFAULT_CATALOG
) -> PreparedUniSorPort:
    try:
        max_hops = single_path.prepare(bundle, config).max_hops
        grid = direct_split.prepare(bundle, config)
    except (single_path.SinglePathConfigError, direct_split.DirectSplitConfigError) as exc:
        raise UniSorPortConfigError(f"{NAME}: {exc}") from exc
    if grid.max_splits > MAX_SPLITS_LIMIT:
        raise UniSorPortConfigError(
            f"{NAME}: search.max_splits must be <= {MAX_SPLITS_LIMIT} (B-F1 reproduces V8's "
            f"small-array sort only), got {grid.max_splits}"
        )
    sor_by_source = _sor_by_source(catalog)
    v3: list[SorPool] = []
    v2: list[SorPool] = []
    universe: list[SorPool] = []
    excluded: dict[str, int] = {}
    for state in sorted(bundle.pools.values(), key=lambda s: s.pool_id.lower()):
        protocol = sor_protocol_of(state, sor_by_source)
        if protocol is None:
            key = str(getattr(state, "source_key", None) or type(state).__name__)
            excluded[key] = excluded.get(key, 0) + 1
            universe.append(_sor_pool(state, "NONE"))
            continue
        pool = _sor_pool(state, protocol)
        (v3 if protocol == V3 else v2).append(pool)
        universe.append(pool)
    if bundle.corpus is not None:
        declared = list(bundle.corpus["cohorts"]["sor_compatible"]["pools"])
        derived = sorted(p.pool_id for p in (*v3, *v2))
        if sorted(declared) != derived:
            raise UniSorPortConfigError(
                f"{NAME}: the corpus sor_compatible cohort disagrees with the catalog's "
                f"sor_protocol mapping ({len(declared)} vs {len(derived)} pools)"
            )
    return PreparedUniSorPort(
        max_hops=max_hops,
        max_splits=grid.max_splits,
        percent_step=grid.percent_step,
        v3_pools=tuple(v3),
        v2_pools=tuple(v2),
        universe=tuple(universe),
        excluded_by_source=MappingProxyType(dict(sorted(excluded.items()))),
    )


class _BudgetExhausted(Exception):
    pass


def _route_edges(route: SorRoute) -> tuple[Edge, ...]:
    return tuple(
        Edge(p.pool_id, route.token_path[i], route.token_path[i + 1])
        for i, p in enumerate(route.pools)
    )


def _plan_legs(route: SorRoute, bundle: SnapshotBundle) -> tuple[Edge, ...]:
    """The route's edges in the bundle's own token spelling (the port lowercases)."""
    edges = []
    for edge in _route_edges(route):
        state = bundle.pools[edge.pool_id]
        t_in = state.token0 if state.token0.lower() == edge.token_in else state.token1
        edges.append(Edge(edge.pool_id, t_in, state.other_token(t_in)))
    return tuple(edges)


def solve(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    prepared = context.prepared
    if not isinstance(prepared, PreparedUniSorPort):
        raise TypeError(f"{NAME}.solve needs the PreparedUniSorPort returned by prepare()")
    bundle = context.bundle
    cache = QuoteCache(bundle)
    params = {
        "max_hops": prepared.max_hops,  # maxSwapsPerPath
        "max_splits": prepared.max_splits,  # maxSplits
        "min_splits": MIN_SPLITS,  # minSplits
        "percent_step": prepared.percent_step,  # distributionPercent
    }
    stats: dict[str, Any] = {
        "sor_port": {
            "upstream_commit": UPSTREAM["commit"],
            "upstream_version": UPSTREAM["version"],
            "contract": CONTRACT,
            "adaptations": list(ADAPTATIONS),
            "deviations": list(DEVIATIONS),
            "gas_score_provider": GAS_SCORE_PROVIDER,
            "params": params,
        },
        "coverage_mode": prepared.coverage_mode,
        "cohort_pools": {V3: len(prepared.v3_pools), V2: len(prepared.v2_pools)},
        "excluded_pools": dict(prepared.excluded_by_source),
        "routes_enumerated": {},
        "quote_entries": 0,
        "quote_entries_null": 0,
        "entry_failures": {},
        "entries_incomplete": 0,
        "incomplete_example": None,
        "route_quotes": 0,
        "selection": None,
        "allocation": None,
        "d1_residual": None,
        "cached_quote": None,
        "evaluated_gross": None,
        "requote_delta": None,
        "truncated_by": None,
        "quotes_executed": 0,
        "quotes_memoized": 0,
    }

    def result(status: SolveStatus, **kw: Any) -> SolveResult:
        stats["quotes_executed"] = cache.misses
        stats["quotes_memoized"] = cache.hits
        return SolveResult(
            case_id=case.case_id, algorithm=NAME, status=status, search_stats=stats, **kw
        )

    # ---- B-R*: enumeration over the cohort (A-1).
    routes = {
        fam: compute_family_routes(
            fam,
            case.token_in,
            case.token_out,
            prepared.v3_pools,
            prepared.v2_pools,
            prepared.max_hops,
        )
        for fam in FAMILIES
    }
    stats["routes_enumerated"] = {fam: len(rs) for fam, rs in routes.items()}
    n_routes = sum(len(rs) for rs in routes.values())
    if n_routes == 0:
        full = compute_all_routes(
            case.token_in, case.token_out, prepared.universe, prepared.max_hops
        )
        stats["universe_routes"] = len(full)
        if full:
            return result(
                SolveStatus.UNSUPPORTED,
                error=(
                    f"no route within {prepared.max_hops} hops over the SOR-compatible cohort, "
                    f"but {len(full)} over all admitted pools: reachable only through pools "
                    f"without an SOR protocol ({dict(prepared.excluded_by_source)}; D-4)"
                ),
            )
        return result(
            SolveStatus.NO_ROUTE,
            error=f"no route within {prepared.max_hops} hops over any admitted pool",
        )

    percents, amounts = amount_distribution(case.amount_in, prepared.percent_step)
    n_entries = n_routes * len(percents)
    if budget.max_candidates is not None and n_routes > budget.max_candidates:
        stats["truncated_by"] = "max_candidates"
        return result(
            SolveStatus.TIMEOUT,
            candidates_considered=0,
            candidates_truncated=n_routes,
            error=(
                f"declared max_candidates={budget.max_candidates} is below the {n_routes} "
                "enumerated routes; a truncated route set is not SOR's input -- not evidence "
                "of no_route"
            ),
        )

    # ---- A-2: the (route, percent) quote table from the simulator.
    failures: dict[str, int] = {}
    incomplete: list[str] = []

    def guarded(state: PoolState, token_in: str, amount: int) -> SwapResult[PoolState]:
        memo_hit = bundle.pools.get(state.pool_id) is state and cache.cached(
            state.pool_id, token_in, amount
        )
        if budget.max_quotes is not None and cache.misses >= budget.max_quotes and not memo_hit:
            raise _BudgetExhausted
        return cache(state, token_in, amount)

    def fail(status: str) -> None:
        failures[status] = failures.get(status, 0) + 1

    legs: dict[SorRoute, tuple[Edge, ...]] = {}

    def quote(route: SorRoute, percent: int, amount: Rational) -> int | None:
        x = amount.quotient
        if x == 0:
            fail("zero_input")
            return None
        pure_v2 = all(p.protocol == V2 for p in route.pools)
        if route not in legs:
            legs[route] = _plan_legs(route, bundle)
        for edge in legs[route]:
            if x == 0:
                fail("zero_intermediate")
                return None
            res = guarded(bundle.pools[edge.pool_id], edge.token_in, x)
            if res.status is not QuoteStatus.OK:
                fail(res.status.value)
                if res.status is QuoteStatus.INCOMPLETE_SNAPSHOT:
                    incomplete.append(f"{'>'.join(route.pool_ids)} @ {percent}%: {res.detail}")
                return None
            if res.amount_in_consumed != x:
                fail("partial_fill")
                return None
            x = res.amount_out
            if pure_v2 and x == 0:
                fail("v2_zero_output")
                return None
        return x

    try:
        route_quotes = build_route_quotes(routes, percents, amounts, quote)
    except _BudgetExhausted:
        stats["truncated_by"] = "max_quotes"
        return result(
            SolveStatus.TIMEOUT,
            candidates_considered=n_routes,
            error=(
                f"declared max_quotes={budget.max_quotes} would cut the {n_entries}-entry "
                "quote table short; a partial table is not SOR's input -- not evidence of "
                "no_route"
            ),
        )
    stats["quote_entries"] = n_entries
    stats["quote_entries_null"] = n_entries - len(route_quotes)
    stats["entry_failures"] = dict(sorted(failures.items()))
    stats["entries_incomplete"] = len(incomplete)
    stats["incomplete_example"] = incomplete[0] if incomplete else None
    stats["route_quotes"] = len(route_quotes)

    # ---- B-S*, B-F*: the upstream selection (A-3: zero gas scores).
    selection = get_best_swap_route(
        case.amount_in, percents, route_quotes, max_splits=prepared.max_splits
    )
    if selection is None:
        if incomplete:
            return result(
                SolveStatus.INCOMPLETE_SNAPSHOT,
                candidates_considered=n_routes,
                error=(
                    f"no complete selection; entries need uncollected state, e.g. {incomplete[0]}"
                ),
            )
        return result(
            SolveStatus.NO_ROUTE,
            candidates_considered=n_routes,
            error=f"no complete selection over {len(route_quotes)} valid quote entries (B-S10)",
        )
    stats["selection"] = selection.to_dict()

    # ---- D-1: integer fill; D-3: independent re-quote of the final plan.
    allocation = integer_fill(case.amount_in, selection)
    stats["allocation"] = [str(a) for a in allocation]
    stats["d1_residual"] = str(allocation[-1] - selection.amounts[-1].quotient)
    plan = split_path_plan(
        case,
        tuple(legs[r.route] for r in selection.routes),
        tuple(r.percent for r in selection.routes),
    )
    context.report_candidate(plan)
    try:
        evaluation = evaluate(bundle, case, plan, context.objective, quote=guarded)
    except _BudgetExhausted:
        stats["truncated_by"] = "max_quotes"
        return result(
            SolveStatus.TIMEOUT,
            candidates_considered=n_routes,
            error=f"declared max_quotes={budget.max_quotes} reached while replaying the plan",
        )
    stats["cached_quote"] = str(selection.swap.quote)
    if evaluation.status is not EvalStatus.OK:
        return result(
            SolveStatus.INVALID_PLAN,
            plan=plan,
            evaluation=evaluation,
            candidates_considered=n_routes,
            error=evaluation.error,
        )
    stats["evaluated_gross"] = str(evaluation.gross_output)
    stats["requote_delta"] = str(evaluation.gross_output - selection.swap.quote)
    return result(
        SolveStatus.OK,
        plan=plan,
        evaluation=evaluation,
        score=context.objective.score(evaluation),
        candidates_considered=n_routes,
    )


FACTORY = AlgorithmFactory(
    name=NAME,
    solve=solve,
    prepare=prepare,
    capabilities=CAPABILITIES,
    search_params=SEARCH_PARAMS,
    provenance=PROVENANCE,
)

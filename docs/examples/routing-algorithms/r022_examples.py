"""Executable worked examples for the 0.2.2 upper-bound pruning guide (WHI-1601).

Every example calls the REAL registered factories (`single_path_bounded`,
`incremental_graph_bounded`, `metis_history_bounded` and their references) through the same
plumbing as `r021_examples.py`, replays every emitted plan with a FRESH
`routing.evaluator.evaluate`, and checks each published number against an expectation that does
not come from the bounded code under test:

- the hand rates of `docs/references/research-022/fixtures/hand_cases.json` (derived on paper
  before any bound code existed) and this module's own `rate_of` / `slack_of`, written from
  `output-bounds.md` §1 / §5.3 and sharing no code with `pools.bounds`;
- this module's own integer constant-product quote (`r021_examples.hand_out`), fund ledger and
  exhaustive oracles (`simple_paths`, `greedy_chunks`), and the exact protocol quote seam
  (`pools.quote.quote_exact_in`) for concentrated-liquidity and Liquidity Book legs;
- three small *pruning oracles* written here from the contract's rules (`s1_oracle` for S1,
  `i1_oracle` for I1, `label_oracle` for M1/M2 on graphs where no two labels merge): they
  decide, candidate by candidate, what the contract says may be skipped, from this module's own
  rates. The factories' `pruned_bound` / `bound_evaluations` counters must equal the oracles';
- the reference solvers (`single_path`, `incremental_graph`, `metis_history`): a bounded run must
  return the reference's status, plan and score.

Counters that only the factory can report (the `bound_pruning.prepare` record of a real bundle
is the one exception, derived here from the formulae) are not published. Work counts
(`pruned_bound`, quotes avoided) are deterministic facts of these toy and fixture inputs;
**no timing is asserted or claimed**: timing belongs to the WHI-1602 measurement.

Run: `uv run python docs/examples/routing-algorithms/r022_examples.py` (also called by
`run_examples.py`). `collect()` returns every example as plain JSON-shaped data.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for _path in (ROOT, HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import r021_examples as r21  # noqa: E402  (the shared independent toolkit)

from benchmark.objective import gross_only  # noqa: E402
from pools.bounds import CHUNK_DOMAIN_MAX, build_bounds, output_bound  # noqa: E402
from pools.cl_math import (  # noqa: E402
    SolidityRevert,
    get_sqrt_ratio_at_tick,
)
from pools.liquidity_book import LBRevert, get_price_from_id  # noqa: E402
from pools.quote import quote_exact_in  # noqa: E402
from pools.result import QuoteStatus  # noqa: E402
from routing.algorithms import chunk_pruning  # noqa: E402
from routing.algorithms.registry import ALGORITHMS  # noqa: E402
from routing.evaluator import evaluate  # noqa: E402
from routing.plan import (  # noqa: E402
    ALL_REMAINING,
    REQUEST_FUND_ID,
    FundInput,
    RoutePlan,
    SwapStep,
)
from snapshot.bundle import load_bundle  # noqa: E402
from snapshot.models import (  # noqa: E402
    Case,
    ConcentratedPoolState,
    ConstantProductPoolState,
    LBStaticFeeParameters,
    LBVariableFeeParameters,
    LiquidityBookPoolState,
    PoolState,
    SnapshotBundle,
)

R022 = ROOT / "docs" / "references" / "research-022" / "fixtures"
CORPUS = ROOT / "tests" / "fixtures" / "corpus" / "bundle"
CPMM_GRAPH = ROOT / "tests" / "fixtures" / "routing" / "cpmm_graph"
HAND_CASES = json.loads((R022 / "hand_cases.json").read_text(encoding="utf-8"))

ExampleError = r21.ExampleError
check = r21.check
equal = r21.equal

D = 10_000  # basis-point denominator of the constant-product fee
TWO127 = 1 << 127
# the sources `output-bounds.md` §1 admits, written out (not read from the bound helper)
CPMM_FIXED_FEE = {"moe_classic_v1": 30}
CL_LM_HOOK = {"uniswap_v3": False, "agni_v3": True, "fusionx_v3": True}
LB_SOURCES = ("moe_lb_v2_2",)

PRESET = {"dominance": "history", "max_labels_per_signature": 1, "max_frontier_labels": 1024}
OFF = {"dominance": "off", "max_labels_per_signature": 1, "max_frontier_labels": 10_000_000}
CAPPED_OFF = {"dominance": "off", "max_labels_per_signature": 1, "max_frontier_labels": 3}


def floor_frac(value: Fraction) -> int:
    return value.numerator // value.denominator


# ====================================================================== independent bounds


def rate_of(pool: PoolState, token_in: str) -> Fraction | None:
    """`r̄` of `output-bounds.md` §1, written from the document; `None` = "no bound"."""
    if token_in not in (pool.token0, pool.token1):
        return None
    if isinstance(pool, ConstantProductPoolState):
        fixed = CPMM_FIXED_FEE.get(pool.source_key) if pool.source_key is not None else None
        if pool.source_key is not None and (fixed is None or pool.fee_bps != fixed):
            return None
        if not 0 <= pool.fee_bps < D:
            return None
        r_in, r_out = pool.reserves_for(token_in)
        if r_in <= 0 or r_out <= 0:
            return None
        return Fraction((D - pool.fee_bps) * r_out, D * r_in)
    if isinstance(pool, ConcentratedPoolState):
        if pool.source_key not in CL_LM_HOOK:
            return None
        if (
            pool.lm_pool is not None
            and int(pool.lm_pool, 16) != 0
            and not CL_LM_HOOK[pool.source_key]
        ):
            return None
        if not 0 <= pool.fee < 10**6:
            return None
        psi = pool.sqrt_price_x96
        try:
            low = get_sqrt_ratio_at_tick(pool.tick)
            high = get_sqrt_ratio_at_tick(pool.tick + 1) if token_in == pool.token1 else None
        except SolidityRevert:
            return None
        if psi < low:  # guard G_CL: the frozen tick may not sit above the price ...
            return None
        if token_in == pool.token0:
            return Fraction((10**6 - pool.fee) * psi * psi, 10**6 * (1 << 192))
        if high is None or psi > high:  # ... and for 1->0 may not lag by more than one tick
            return None
        return Fraction((10**6 - pool.fee) * (1 << 192), 10**6 * psi * psi)
    if isinstance(pool, LiquidityBookPoolState):
        if pool.source_key not in LB_SOURCES or pool.static_fee is None or pool.bin_step < 1:
            return None
        beta = pool.static_fee.base_factor * pool.bin_step * 10**10  # the base fee, not the total
        if beta > 10**17:
            return None
        try:
            price = get_price_from_id(pool.active_id, pool.bin_step)
        except LBRevert:
            return None
        if not (1 << 38) <= price <= (1 << 218):
            return None
        if token_in == pool.token0:
            return Fraction((10**18 - beta) * price, 10**18 * (1 << 128))
        return Fraction((10**18 - beta) * (1 << 128), 10**18 * price)
    return None


def slack_of(pool: PoolState, token_in: str) -> Fraction | None:
    """The chunk slack `s` of `output-bounds.md` §1 / §5.3 (`None` when the rate or the slack
    domain is missing)."""
    if rate_of(pool, token_in) is None:
        return None
    if isinstance(pool, ConstantProductPoolState):
        return Fraction(1)
    if isinstance(pool, ConcentratedPoolState):
        psi = pool.sqrt_price_x96
        l_hat = min(
            (1 << 128) - 1, pool.liquidity + sum(t.liquidity_gross for t in pool.ticks.values())
        )
        if token_in == pool.token0:
            return (
                None
                if psi >= 1 << 128
                else Fraction(psi * psi, 1 << 192) + 1 + Fraction(l_hat, 1 << 96)
            )
        spot = Fraction(1 << 192, psi * psi)
        return spot * (1 + Fraction(l_hat, 1 << 96)) + 1
    assert isinstance(pool, LiquidityBookPoolState)
    price = get_price_from_id(pool.active_id, pool.bin_step)
    if token_in == pool.token0:
        return Fraction(price, 1 << 128) + 1
    return Fraction(1 << 128, price) + 1


def ub_nested(rates: Sequence[Fraction], amount: int) -> int:
    """`output-bounds.md` §5.1: `⌊r̄_n ⌊ … ⌊r̄_1 x⌋ … ⌋⌋`."""
    for rate in rates:
        amount = floor_frac(rate * amount)
    return amount


def chain_u(hops: Sequence[tuple[Fraction, Fraction]], m: int) -> list[int]:
    """`pruning-contract.md` §3.5: `u_0 = m`, `u_j = ⌊r̄_j u_{j-1} + s_j⌋`."""
    out = [m]
    for rate, slack in hops:
        out.append(floor_frac(rate * out[-1] + slack))
    return out


def pool_rate(bundle: SnapshotBundle, edge: r21.Edge) -> Fraction:
    rate = rate_of(bundle.pools[edge[0]], edge[1])
    check(rate is not None, f"{edge[0]}: no bound")
    assert rate is not None
    return rate


def helper(pool: PoolState, token_in: str) -> Any:
    return output_bound(pool, token_in)


def first_pool(bundle: SnapshotBundle, prefix: str) -> Any:
    return bundle.pools[next(k for k in bundle.pools if k.startswith(prefix))]


def real_bundle() -> SnapshotBundle:
    return load_bundle(CORPUS)


def hand_or_zero(pool: ConstantProductPoolState, token: str, amount: int, **kw: Any) -> int:
    """`q̃` of `output-bounds.md` §2: a dust leg (zero output) counts as 0."""
    return 0 if amount == 0 else (r21.hand_out(pool, token, amount, **kw) or 0)


# ====================================================================== pruning oracles


def step_out(pool: PoolState, token_in: str, amount: int) -> int | None:
    """One exact hop: this module's own CPMM quote, the protocol seam for CL / LB; `None` for
    every quote the evaluator would reject (non-OK status, partial fill, dust)."""
    if isinstance(pool, ConstantProductPoolState):
        return r21.hand_out(pool, token_in, amount)
    res = quote_exact_in(pool, token_in, amount)
    if res.status is not QuoteStatus.OK or res.amount_in_consumed != amount:
        return None
    return res.amount_out


def path_label(path: Sequence[r21.Edge]) -> str:
    return ">".join(e[0] for e in path)


def s1_oracle(
    bundle: SnapshotBundle, case: Case, hops: int, *, prune: bool = True
) -> dict[str, Any]:
    """Rule S1 (`pruning-contract.md` §4) on `single_path`'s enumeration, from this module's
    rates and exact hops. With `prune=False` it is the reference loop (the quote count of the
    reference). A candidate is skipped when it is not dead-pruned, an incumbent exists and the
    nested-floor bound from its longest evaluated prefix is `<=` the incumbent."""
    incumbent: tuple[int, str] | None = None
    prefix_out: dict[tuple[r21.Edge, ...], int] = {}
    dead: set[tuple[r21.Edge, ...]] = set()
    quote_keys: set[tuple[str, str, int]] = set()
    log: list[dict[str, Any]] = []
    c: Counter[str] = Counter()
    for path in r21.simple_paths(bundle, case.token_in, case.token_out, hops):
        label = path_label(path)
        if any(path[:k] in dead for k in range(1, len(path))):
            c["dead"] += 1
            log.append({"path": label, "decision": "dead_prefix"})
            continue
        upper: int | None = None
        unbounded = False
        if prune and incumbent is not None:
            c["bound_evaluations"] += 1
            k, amount = 0, case.amount_in
            for j in range(len(path) - 1, 0, -1):
                if path[:j] in prefix_out:
                    k, amount = j, prefix_out[path[:j]]
                    break
            bounds = [rate_of(bundle.pools[e[0]], e[1]) for e in path[k:]]
            if any(b is None for b in bounds):
                c["no_bound"] += 1
                unbounded = True  # evaluated below, never pruned
            else:
                upper = ub_nested([b for b in bounds if b is not None], amount)
                if upper <= incumbent[0]:
                    c["pruned_bound"] += 1
                    log.append(
                        {
                            "path": label,
                            "decision": "pruned",
                            "bound": upper,
                            "from_prefix": k,
                            "incumbent": incumbent[0],
                        }
                    )
                    continue
        c["evaluated"] += 1
        amount, ok = case.amount_in, True
        for i, e in enumerate(path):
            if amount == 0:
                prefix_out[path[: i + 1]] = 0
                continue
            quote_keys.add((e[0], e[1], amount))
            out = step_out(bundle.pools[e[0]], e[1], amount)
            if out is None:
                dead.add(path[: i + 1])
                c["failed"] += 1
                ok = False
                break
            prefix_out[path[: i + 1]] = amount = out
        if ok:
            log.append(
                {
                    "path": label,
                    "decision": "evaluated",
                    "output": amount,
                    "bound": upper,
                    "no_bound": unbounded,
                }
            )
            if incumbent is None or amount > incumbent[0]:
                incumbent = (amount, label)
        else:
            log.append({"path": label, "decision": "failed"})
    return {
        "best": None if incumbent is None else {"score": incumbent[0], "path": incumbent[1]},
        "log": log,
        "counts": dict(c),
        "quotes": len(quote_keys),
    }


Memo = dict[tuple[r21.Edge, ...], Any]


def hop_ub(
    bundle: SnapshotBundle,
    edge: r21.Edge,
    committed: int,
    m: int,
    *,
    slack_scale: Fraction = Fraction(1),
) -> int | None:
    """`⌊r̄ m + s⌋` of one hop, `None` for "no bound" (missing rate or slack, or the proved
    domain `committed + m <= 2**127` left): `pruning-contract.md` §3.4."""
    pool = bundle.pools[edge[0]]
    rate, slack = rate_of(pool, edge[1]), slack_of(pool, edge[1])
    if rate is None or slack is None or committed + m > CHUNK_DOMAIN_MAX:
        return None
    return floor_frac(rate * m + slack * slack_scale)


def i1_oracle(
    bundle: SnapshotBundle,
    case: Case,
    chunks: int,
    hops: int,
    *,
    prune: bool = True,
    retained: bool = True,
    slack_scale: Fraction = Fraction(1),
) -> dict[str, Any]:
    """The `incremental_graph` chunk loop (`routing-algorithms.md` §6.3) with rule I1
    (`pruning-contract.md` §5.1) from this module's own CPMM quotes and rates. `retained` is P0
    (the simpler `path_split` candidate exists); `slack_scale=0` is the unsafe slack-free bound
    §3.5 forbids. A skipped candidate is still *scored*; the longest memoized prefix starts the
    chain; a memoized failure is never bounded."""
    paths = r21.simple_paths(bundle, case.token_in, case.token_out, hops)
    flows: dict[str, tuple[str, int, int]] = {}  # pool -> (token_in, aggregate input, output)
    token_edges: set[tuple[str, str]] = set()
    sizes = r21.chunk_amounts(case.amount_in, chunks)
    last = max(k for k, a in enumerate(sizes) if a > 0)
    carry = 0
    c: Counter[str] = Counter()
    sequence: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for k, size in enumerate(sizes):
        if size == 0:
            continue
        amount = carry + size
        memo: Memo = {}
        choice: tuple[int, tuple[r21.Edge, ...]] | None = None
        chunk_log: list[dict[str, Any]] = []
        for path in paths:
            if not r21._admissible(token_edges, path):
                c["rejected_cycle"] += 1
                chunk_log.append({"path": path_label(path), "decision": "rejected_cycle"})
                continue
            c["scored"] += 1
            u: int | None = None
            if prune and retained and choice is not None:
                start = _bound_start(path, amount, memo)
                if start is not None:
                    c["bound_evaluations"] += 1
                    i, u = start
                    for edge in path[i:]:
                        committed = flows[edge[0]][1] if edge[0] in flows else 0
                        hop = hop_ub(bundle, edge, committed, u, slack_scale=slack_scale)
                        if hop is None:
                            u = None
                            break
                        u = hop
                    if u is None:
                        c["no_bound"] += 1
                    elif u <= choice[0]:
                        c["pruned_bound"] += 1
                        chunk_log.append(
                            {
                                "path": path_label(path),
                                "decision": "pruned",
                                "bound": u,
                                "from_prefix": i,
                                "choice": choice[0],
                            }
                        )
                        continue
            m = _marginal_with_memo(bundle, flows, path, amount, memo)
            if m is None:
                chunk_log.append({"path": path_label(path), "decision": "failed"})
                continue
            chunk_log.append(
                {"path": path_label(path), "decision": "evaluated", "marginal": m, "bound": u}
            )
            if choice is None or m > choice[0]:
                choice = (m, path)
        decisions.append({"chunk": k + 1, "amount": amount, "candidates": chunk_log})
        if k != last and (choice is None or choice[0] == 0):
            carry = amount
            c["carried"] += 1
            continue
        check(choice is not None, f"chunk {k + 1}: no admissible path")
        assert choice is not None
        carry = 0
        sequence.append(
            {
                "chunk": k + 1,
                "amount": amount,
                "pools": [e[0] for e in choice[1]],
                "marginal": choice[0],
            }
        )
        for i, edge in enumerate(choice[1]):  # commit: the memoized updates of the chosen path
            flows[edge[0]] = memo[choice[1][: i + 1]][1]
        token_edges.update((e[1], e[2]) for e in choice[1])
    gross = sum(
        out
        for pid, (tin, x, out) in flows.items()
        if (
            bundle.pools[pid].token1
            if tin == bundle.pools[pid].token0
            else bundle.pools[pid].token0
        )
        == case.token_out
    )
    return {"sequence": sequence, "decisions": decisions, "counts": dict(c), "gross": gross}


def _bound_start(path: tuple[r21.Edge, ...], amount: int, memo: Memo) -> tuple[int, int] | None:
    """Rule I1 starts its chain after the longest successfully memoized proper prefix; a
    memoized failure there means the reference answers the candidate for free: no bound."""
    for i in range(len(path) - 1, 0, -1):
        hit = memo.get(path[:i])
        if hit is not None:
            return None if hit == "fail" else (i, hit[0])
    return 0, amount


def _marginal_with_memo(
    bundle: SnapshotBundle,
    flows: Mapping[str, tuple[str, int, int]],
    path: tuple[r21.Edge, ...],
    amount: int,
    memo: Memo,
) -> int | None:
    """Marginal of `path` for `amount` on the committed aggregates (each hop on the pool's
    ORIGINAL state at `x + m` minus the committed output), memoized per prefix like the
    reference; a zero marginal makes no further quote."""
    m = amount
    for i, edge in enumerate(path):
        key = path[: i + 1]
        if key in memo:
            if memo[key] == "fail":
                return None
            m = memo[key][0]
            continue
        tin, x, out = flows.get(edge[0], (edge[1], 0, 0))
        if m == 0:
            memo[key] = (0, (tin, x, out))
            continue
        total = step_out(bundle.pools[edge[0]], edge[1], x + m)
        if total is None or total < out:
            memo[key] = "fail"
            return None
        entering, m = m, total - out
        memo[key] = (m, (edge[1], x + entering, total))
    return m


def hop_distance(bundle: SnapshotBundle, source: str, target: str) -> dict[str, int]:
    """Fewest pools from each token to `target`, never entering `source` and never leaving
    `target` (the label search's distance filter; breadth first from the target)."""
    dist = {target: 0}
    queue = [target]
    while queue:
        w = queue.pop(0)
        if w == source:
            continue
        for _pid, _tin, u in r21.adjacency(bundle, w):
            if u != target and u not in dist:
                dist[u] = dist[w] + 1
                queue.append(u)
    return dist


def walk_layers(
    bundle: SnapshotBundle, source: str, target: str, hops: int
) -> list[dict[str, int]]:
    """`W[k][v]` of `pruning-contract.md` §6.3: the walks of exactly `k` pools from `source` to
    `v` the label search could create (never into `source`, never out of `target`, inside the
    distance filter; a token may repeat, so this only over-counts)."""
    dist = hop_distance(bundle, source, target)
    layers: list[dict[str, int]] = [{source: 1}]
    for k in range(1, hops + 1):
        nxt: dict[str, int] = {}
        for token, n in layers[-1].items():
            if token == target:
                continue
            for _pid, _tin, v in r21.adjacency(bundle, token):
                if v == source or (v != target and dist.get(v, hops + 1) > hops - k):
                    continue
                nxt[v] = nxt.get(v, 0) + n
        layers.append(nxt)
    return layers


def gate_oracle(
    bundle: SnapshotBundle, case: Case, hops: int, options: Mapping[str, Any]
) -> tuple[str, bool]:
    """`G_M2` of `pruning-contract.md` §6.3 and whether a non-target label can exist at all."""
    labels = [
        {v: n for v, n in layer.items() if v != case.token_out}
        for layer in walk_layers(bundle, case.token_in, case.token_out, hops)[1:]
    ]
    exist = any(layer for layer in labels)
    if max((sum(layer.values()) for layer in labels), default=0) > options["max_frontier_labels"]:
        return "closed:frontier", exist
    if options["dominance"] != "off" and any(n > 1 for layer in labels for n in layer.values()):
        return "closed:dominance", exist
    return "open", exist


def u_bound(
    bundle: SnapshotBundle, source: str, target: str, token: str, h: int
) -> tuple[Fraction, Fraction] | None:
    """`U_h(token)` of `pruning-contract.md` §6.3: every arrival from a label of amount `a` at
    `token` with `h` hops left is `<= r a + s` (`r`, `s` maximized separately over the relaxed
    walks); `None` = no walk reaches the target."""
    if token == target:
        return Fraction(1), Fraction(0)
    if h == 0:
        return None
    best: tuple[Fraction, Fraction] | None = None
    for pid, tin, w in r21.adjacency(bundle, token):
        if w == source:
            continue
        sub = u_bound(bundle, source, target, w, h - 1)
        if sub is None:
            continue
        pool = bundle.pools[pid]
        rate, slack = rate_of(pool, tin), slack_of(pool, tin)
        check(rate is not None and slack is not None, f"{pid}: no bound")
        assert rate is not None and slack is not None
        cand = (rate * sub[0], slack * sub[0] + sub[1])
        best = cand if best is None else (max(best[0], cand[0]), max(best[1], cand[1]))
    return best


def label_oracle(
    bundle: SnapshotBundle,
    case: Case,
    chunks: int,
    hops: int,
    *,
    m2: bool,
    frontier_cap: int = 10**9,
) -> dict[str, Any]:
    """The `metis_history` chunk search where **no two labels share a (token, visited-set)
    group**: a graph whose walks never merge, or `dominance: "off"` (every label is its own
    group, so R2, R3 and R6 never fire). `frontier_cap` is R7: a layer already holding that many
    labels refuses the new one (`capped` counts the refusals). Rule M1 (a relaxation into the
    target whose one-hop bound is
    `<=` the chunk's best) and, if `m2`, rule M2 (a relaxation into another token whose `U_h`
    bound is `<=` the best). Everything comes from this module's CPMM quotes and rates;
    `relaxations` is the reference's unit (`label_relaxations`: a skipped relaxation still
    counts)."""
    source, target = case.token_in, case.token_out
    dist = hop_distance(bundle, source, target)
    flows: dict[str, tuple[str, int, int]] = {}
    token_edges: set[tuple[str, str]] = set()
    sizes = r21.chunk_amounts(case.amount_in, chunks)
    last = max(k for k, a in enumerate(sizes) if a > 0)
    carry = 0
    c: Counter[str] = Counter()
    sequence: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for k0, size in enumerate(sizes):
        if size == 0:
            continue
        amount = carry + size
        layer: list[tuple[int, tuple[r21.Edge, ...], frozenset[str], list[Any]]] = [
            (amount, (), frozenset((source,)), [])
        ]
        best: tuple[int, tuple[r21.Edge, ...], list[Any]] | None = None
        for k in range(1, hops + 1):
            nxt: list[tuple[int, tuple[r21.Edge, ...], frozenset[str], list[Any]]] = []
            for lab_amount, lab_path, lab_tokens, lab_updates in layer:
                token = lab_path[-1][2] if lab_path else source
                for e in r21.adjacency(bundle, token):
                    v = e[2]
                    if v == source:
                        continue
                    if v != target and dist.get(v, hops + 1) > hops - k:
                        continue
                    if v in lab_tokens:
                        continue
                    p = (*lab_path, e)
                    if not r21._admissible(token_edges, p):
                        continue
                    c["relaxations"] += 1
                    arrival = v == target
                    if best is not None and lab_amount > 0 and (arrival or m2):
                        c["bound_evaluations"] += 1
                        committed = flows[e[0]][1] if e[0] in flows else 0
                        first = hop_ub(bundle, e, committed, lab_amount)
                        check(first is not None, f"{e[0]}: no bound")
                        assert first is not None
                        if arrival:
                            upper = first
                        else:
                            u = u_bound(bundle, source, target, v, hops - k)
                            pool = bundle.pools[e[0]]
                            rate, slack = rate_of(pool, e[1]), slack_of(pool, e[1])
                            assert rate is not None and slack is not None
                            if u is None:  # no walk reaches the target: nothing below can arrive
                                upper = 0
                            else:
                                upper = floor_frac(u[0] * (rate * lab_amount + slack) + u[1])
                        if upper <= best[0]:
                            c["pruned_bound"] += 1
                            skipped.append(
                                {
                                    "chunk": k0 + 1,
                                    "rule": "M1" if arrival else "M2",
                                    "relaxation": p[-1][0],
                                    "label_path": path_label(p),
                                    "bound": upper,
                                    "best": best[0],
                                }
                            )
                            continue
                    tin, x, out = flows.get(e[0], (e[1], 0, 0))
                    if lab_amount == 0:
                        m, update = 0, (tin, x, out)
                    else:
                        total = step_out(bundle.pools[e[0]], e[1], x + lab_amount)
                        if total is None or total < out:
                            continue
                        m, update = total - out, (e[1], x + lab_amount, total)
                    if arrival:
                        if best is None or m > best[0]:
                            best = (m, p, [*lab_updates, update])
                        continue
                    if len(nxt) >= frontier_cap:  # R7: a full layer refuses the new label
                        c["capped"] += 1
                        continue
                    nxt.append((m, p, lab_tokens | {v}, [*lab_updates, update]))
            layer = nxt
        if k0 != last and (best is None or best[0] == 0):
            carry = amount
            continue
        check(best is not None, f"chunk {k0 + 1}: no admissible label path")
        assert best is not None
        carry = 0
        sequence.append(
            {
                "chunk": k0 + 1,
                "amount": amount,
                "pools": [e[0] for e in best[1]],
                "marginal": best[0],
            }
        )
        for e, update in zip(best[1], best[2], strict=True):
            flows[e[0]] = update
        token_edges.update((e[1], e[2]) for e in best[1])
    gross = sum(
        out
        for pid, (tin, _x, out) in flows.items()
        if (
            bundle.pools[pid].token1
            if tin == bundle.pools[pid].token0
            else bundle.pools[pid].token0
        )
        == target
    )
    cost = 0
    if m2:  # edge relaxations of the U_h table: h = 1..hops-1 over every non-target token
        tokens = {t for p in bundle.pools.values() for t in (p.token0, p.token1)} - {target}
        cost = (hops - 1) * sum(len(r21.adjacency(bundle, t)) for t in tokens)
    return {
        "sequence": sequence,
        "skipped": skipped,
        "counts": dict(c),
        "gross": gross,
        "bound_table_cost": cost,
    }


# ====================================================================== factory plumbing


def run(
    name: str,
    bundle: SnapshotBundle,
    case: Case,
    *,
    hops: int,
    chunks: int | None = None,
    label_hops: int | None = None,
    splits: int = 2,
    step: int = 25,
    options: Mapping[str, Any] | None = None,
    hand: bool = True,
) -> dict[str, Any]:
    """One solve of a registered factory through `r021_examples.run_factory` (fresh evaluator
    replay of the plan, plus the hand fund ledger for all-CPMM plans)."""
    params: dict[str, Any] = {"max_hops": hops}
    if chunks is not None:
        params |= {"max_splits": splits, "percent_step": step, "chunks": chunks}
    if label_hops is not None:
        params["label_hops"] = label_hops
    return r21.run_factory(name, bundle, case, params, options, hand=hand)


def same_result(ref: Mapping[str, Any], got: Mapping[str, Any], what: str) -> None:
    """The bounded run returns the reference's status, plan (every step and fund) and score."""
    for key in ("status", "score", "plan"):
        equal(got[key], ref[key], f"{what}: {key}")
    equal(got["replay"], ref["replay"], f"{what}: fresh replay")


def block(row: Mapping[str, Any]) -> dict[str, Any]:
    b: dict[str, Any] = row["stats"]["bound_pruning"]
    return b


def counters(row: Mapping[str, Any]) -> dict[str, int]:
    b = block(row)
    return {k: b[k] for k in ("pruned_bound", "bound_evaluations", "bound_no_bound")}


def dec6(value: Fraction) -> str:
    """`value` truncated to six decimals, in integer arithmetic."""
    n = floor_frac(value * 10**6)
    return f"{n // 10**6}.{n % 10**6:06d}"


def cpmm_bundle(name: str, pools: Mapping[str, Sequence[Any]], **kw: Any) -> SnapshotBundle:
    return r21.fixture_bundle(name, pools, **kw)


def request_case(token_in: str, token_out: str, amount: int, case_id: str = "ex") -> Case:
    return Case(case_id, token_in, token_out, amount)


# ====================================================================== 1. the bound theory


def _hand_state(case: Mapping[str, Any]) -> PoolState:
    kind = case["family"]
    if kind == "cpmm":
        reserve0, reserve1 = case["reserves"]
        return ConstantProductPoolState("h", "A", "B", reserve0, reserve1, case["fee_bps"])
    if kind == "cl":
        tick = case["tick"]
        return ConcentratedPoolState(
            "h",
            "uniswap_v3",
            "A",
            "B",
            case["fee"],
            60,
            get_sqrt_ratio_at_tick(tick),
            tick,
            10**18,
            0,
            0,
            0,
            0,
            0,
            (-4, 4),
        )
    return LiquidityBookPoolState(
        "h",
        "moe_lb_v2_2",
        "A",
        "B",
        case["bin_step"],
        1_000_000,
        case["active_id"],
        0,
        0,
        0,
        0,
        LBStaticFeeParameters(case["base_factor"], 30, 600, 5000, 0, 1000, 350_000),
        LBVariableFeeParameters(0, 0, case["active_id"], 1_000_000),
        (0, (1 << 24) - 1),
    )


def _hand_rates() -> list[dict[str, Any]]:
    """The pinned hand-derived rates (fixture written before any bound code) against the helper
    and this module's `rate_of`."""
    rows = []
    for case in HAND_CASES["rate_cases"]:
        state = _hand_state(case)
        for token_in, text in case["rates"].items():
            want, got = Fraction(text), helper(state, token_in)
            mine = rate_of(state, token_in)
            check(got is not None and mine is not None, f"{case['name']}: no bound")
            assert got is not None and mine is not None
            if case.get("approx"):  # ids away from 2**23: hand-derived to 1e-30
                check(abs(got.rate - want) < Fraction(1, 10**30), f"{case['name']}: {token_in}")
            else:
                equal(got.rate, want, f"{case['name']} token {token_in}")
            equal(got.rate, mine, f"{case['name']}: helper vs rate_of")
            equal(got.slack, slack_of(state, token_in), f"{case['name']}: helper slack")
            rows.append({"name": case["name"], "token_in": token_in, "hand_rate": text})
    return rows


def _r2() -> dict[str, Any]:
    """The R2 counterexample: `1000/1000`, 30 bps, outputs `0*, 1, 2, 3, 4, 5` for `x = 1..6`."""
    pool = r21.cp("r2", "A", "B", 1000, 1000, 30)
    hand = HAND_CASES["r2_cpmm"]
    q = [hand_or_zero(pool, "A", x) for x in range(7)]
    equal(q, hand["outputs_x0_to_x6"], "R2 outputs of the pinned fixture")
    equal(r21.hand_out(pool, "A", 1), None, "R2: x = 1 is a dust quote (zero output)")
    bound = helper(pool, "A")
    rate = rate_of(pool, "A")
    assert bound is not None and rate is not None
    equal(rate, Fraction(hand["rate"]), "R2 hand rate")
    equal(bound.rate, rate, "R2 helper rate")
    floors = [floor_frac(rate * x) for x in range(7)]
    check(all(qx <= fx for qx, fx in zip(q, floors, strict=True)), "R2: q(x) <= floor(r x)")
    marginal = q[2] - q[1]
    check(marginal > rate * 1, "R2: the marginal of one unit exceeds r*m")
    equal(bound.slack, Fraction(1), "R2 CPMM slack")
    slack_free, with_slack = floor_frac(rate * 1), floor_frac(rate * 1 + 1)
    equal((slack_free, with_slack, marginal), (0, 1, 1), "R2 slack-free vs slack bound")
    return {
        "reserves": [1000, 1000],
        "fee_bps": 30,
        "q": q,
        "floor_rate_x": floors,
        "rate": str(rate),
        "marginal_x1_m1": marginal,
        "slack": 1,
        "slack_free_bound_m1": slack_free,
        "bound_with_slack_m1": with_slack,
    }


def _real_pools() -> list[dict[str, Any]]:
    """`q(x) <= floor(r̄ x)` on three real fixture pools, every `q` from the exact seam."""
    bundle = real_bundle()
    rows = []
    for prefix, family in (
        ("0x69a707d8", "CPMM moe_classic_v1"),
        ("0x36f66548", "CL agni_v3"),
        ("0x368b1480", "LB moe_lb_v2_2"),
    ):
        pool = first_pool(bundle, prefix)
        rate, slack, got = rate_of(pool, r21.USDC), slack_of(pool, r21.USDC), helper(pool, r21.USDC)
        check(rate is not None and slack is not None and got is not None, f"{prefix}: no bound")
        assert rate is not None and slack is not None and got is not None
        equal((got.rate, got.slack), (rate, slack), f"{prefix}: helper vs rate_of/slack_of")
        table = []
        for x in (10**4, 10**6, 10**8, 10**10, 10**11):
            res = quote_exact_in(pool, r21.USDC, x)
            check(res.status is QuoteStatus.OK, f"{prefix} x={x}: {res.status}")
            bound = floor_frac(rate * x)
            check(res.amount_out <= bound, f"{prefix} x={x}: q={res.amount_out} > {bound}")
            table.append({"x": x, "q": res.amount_out, "floor_rate_x": bound})
        reserve_out = (
            pool.reserves_for(r21.USDC)[1] if isinstance(pool, ConstantProductPoolState) else None
        )
        rows.append(
            {
                "pool": prefix,
                "family": family,
                "rate_6dp": dec6(rate),
                "table": table,
                "output_reserve": reserve_out,
            }
        )
    return rows


def _multi_hop() -> dict[str, Any]:
    """Composition (`output-bounds.md` §5.1, §5.4) and the chunk chain (`pruning-contract.md`
    §3.5) on a two-hop CPMM path, every exact value from the hand quote."""
    p1 = r21.cp("p1", "A", "B", 1_000_000, 1_500_000, 30)
    p2 = r21.cp("p2", "B", "C", 2_000_000, 1_000_000, 30)
    x = 10_000
    y = r21.hand_out(p1, "A", x)
    z = r21.hand_out(p2, "B", y or 0)
    assert y is not None and z is not None
    r1, r2 = rate_of(p1, "A"), rate_of(p2, "B")
    assert r1 is not None and r2 is not None
    nested = ub_nested([r1, r2], x)
    product = floor_frac(r1 * r2 * x)
    check(z <= nested <= product, "multi-hop: exact <= nested <= product form")
    # the chunk chain: committed aggregates 40 000 into p1 and 30 000 into p2, then m = 10 000
    c1, c2, m = 40_000, 30_000, 10_000
    m1 = hand_or_zero(p1, "A", c1 + m) - hand_or_zero(p1, "A", c1)
    m2 = hand_or_zero(p2, "B", c2 + m1) - hand_or_zero(p2, "B", c2)
    u = chain_u([(r1, Fraction(1)), (r2, Fraction(1))], m)
    check(m1 <= u[1] and m2 <= u[2], "chunk chain: marginals <= chain bound")
    s_path = Fraction(1) * r2 + 1
    check(u[2] <= floor_frac(r1 * r2 * m + s_path), "chain bound never looser than product form")
    return {
        "pools": {"p1": [1_000_000, 1_500_000], "p2": [2_000_000, 1_000_000]},
        "request": x,
        "hop_outputs": [y, z],
        "rates": [str(r1), str(r2)],
        "nested_bound": nested,
        "product_bound": product,
        "chunk": {
            "committed": [c1, c2],
            "m": m,
            "marginals": [m1, m2],
            "chain_u": u,
            "product_form": floor_frac(r1 * r2 * m + s_path),
        },
    }


def _directions() -> dict[str, Any]:
    """Same direction is cumulative; the opposite direction improves and is excluded by the
    evaluator before any quote."""
    pool = r21.cp("p", "A", "B", 10**6, 10**6, 30)
    bundle = r21.bundle_of("directions", pool)
    r_ab, r_ba = rate_of(pool, "A"), rate_of(pool, "B")
    assert r_ab is not None and r_ba is not None
    x1, x2, y = 200_000, 100_000, 100_000
    out1 = r21.hand_out(pool, "A", x1)
    assert out1 is not None
    after = (pool.reserve0 + x1, pool.reserve1 - out1)  # (A, B) after the swap
    same = r21.hand_out(pool, "A", x2, reserves=after)
    opposite = r21.hand_out(pool, "B", y, reserves=(after[1], after[0]))
    assert same is not None and opposite is not None
    same_bound, opposite_bound = floor_frac(r_ab * x2), floor_frac(r_ba * y)
    check(same <= same_bound, "same direction: later swap <= original-state bound")
    check(opposite > opposite_bound, "opposite direction: the original bound is exceeded")
    # the evaluator never executes the opposite direction: a plan using `p` both ways is cyclic
    case = request_case("A", "B", 1000)
    cyclic = RoutePlan(
        (
            SwapStep("p", "A", "B", (FundInput(REQUEST_FUND_ID, ALL_REMAINING),), "F1"),
            SwapStep("p", "B", "A", (FundInput("F1", ALL_REMAINING),), "OUT"),
        )
    )
    ev = evaluate(bundle, case, cyclic, gross_only())
    equal(
        (ev.status.value, ev.error),
        ("invalid_plan", "economic token cycle: A -> B -> A"),
        "cyclic plan",
    )
    # same-direction reuse is admitted and the second leg sees the state the first left
    reuse = RoutePlan(
        (
            SwapStep("p", "A", "B", (FundInput(REQUEST_FUND_ID, x1),), "F1"),
            SwapStep("p", "A", "B", (FundInput(REQUEST_FUND_ID, ALL_REMAINING),), "F2"),
        )
    )
    ev2 = evaluate(bundle, request_case("A", "B", x1 + x2), reuse, gross_only())
    equal([t.amount_out for t in ev2.trace], [out1, same], "same-direction reuse replay")
    return {
        "reserves": [10**6, 10**6],
        "first_swap": {"in": x1, "out": out1},
        "same_direction": {"in": x2, "out": same, "bound_from_original_state": same_bound},
        "opposite_direction": {
            "in": y,
            "out": opposite,
            "bound_from_original_state": opposite_bound,
        },
        "cyclic_plan": {"status": ev.status.value, "error": ev.error},
        "reuse_replay": [t.amount_out for t in ev2.trace],
    }


def _swapped_state() -> dict[str, Any]:
    """Why bounds come from the original state (`pruning-contract.md` §3.3)."""
    pool = r21.cp("p", "A", "B", 10**8, 10**8, 30)
    x, m = 10**8, 10**5
    fx, fxm = r21.hand_out(pool, "A", x), r21.hand_out(pool, "A", x + m)
    assert fx is not None and fxm is not None
    marginal = fxm - fx
    original, new = (
        rate_of(pool, "A"),
        Fraction((D - 30) * (pool.reserve1 - fx), D * (pool.reserve0 + x)),
    )
    assert original is not None
    from_original, from_swapped = floor_frac(original * m + 1), floor_frac(new * m + 1)
    check(marginal <= from_original, "original-state chunk bound is sound")
    check(marginal > from_swapped, "the swapped-state bound underestimates the marginal")
    return {
        "committed": x,
        "m": m,
        "marginal": marginal,
        "bound_original": from_original,
        "bound_swapped": from_swapped,
        "excess": marginal - from_swapped,
    }


PROBE = 100  # the input of the exact quote shown beside each "no bound" state


def _no_bound() -> list[dict[str, Any]]:
    """Each state outside the proved domain has no bound; the helper and this module agree."""
    cpmm = r21.cp("c", "A", "B", 1000, 1000, 30)
    cl = _hand_state({"family": "cl", "tick": 0, "fee": 3000})
    lb = _hand_state({"family": "lb", "bin_step": 10, "active_id": 8388608, "base_factor": 5000})
    assert isinstance(cl, ConcentratedPoolState) and isinstance(lb, LiquidityBookPoolState)
    cases: list[tuple[str, PoolState, str]] = [
        ("cpmm: a zero reserve", dataclasses.replace(cpmm, reserve1=0), "A"),
        ("cpmm: unknown source key", dataclasses.replace(cpmm, source_key="mystery_amm"), "A"),
        (
            "cpmm: moe_classic_v1 with a fee other than its fixed 30 bps",
            dataclasses.replace(cpmm, source_key="moe_classic_v1", fee_bps=25),
            "A",
        ),
        ("cpmm: fee_bps = 10000", dataclasses.replace(cpmm, fee_bps=D), "A"),
        (
            "cl: frozen tick one above the price (guard G_CL)",
            dataclasses.replace(cl, tick=cl.tick + 1, sqrt_price_x96=get_sqrt_ratio_at_tick(0)),
            "A",
        ),
        (
            "cl: lm_pool set on uniswap_v3 (no such hook)",
            dataclasses.replace(cl, lm_pool="0x" + "11" * 20),
            "A",
        ),
        ("lb: static fee parameters not collected", dataclasses.replace(lb, static_fee=None), "A"),
        (
            "lb: base fee above the 10 % MAX_FEE",
            dataclasses.replace(
                lb,
                bin_step=200,
                static_fee=LBStaticFeeParameters(65_535, 30, 600, 5000, 0, 1000, 350_000),
            ),
            "A",
        ),
        (
            "lb: active bin so far from 2**23 that the price is outside [2**38, 2**218]",
            dataclasses.replace(lb, active_id=lb.active_id + 700_000),
            "A",
        ),
    ]
    rows = []
    for why, state, token in cases:
        check(helper(state, token) is None, f"{why}: the helper returned a bound")
        check(rate_of(state, token) is None, f"{why}: rate_of returned a bound")
        try:
            status = quote_exact_in(state, token, PROBE).status.value
        except ValueError:  # an impossible fee: the seam refuses the state outright
            status = "raises ValueError"
        rows.append({"state": why, "bound": None, "exact_quote_status_at_100": status})
    # a rate without a slack: CL token0 -> token1 with sqrt_price_x96 >= 2**128
    high = dataclasses.replace(cl, tick=500_000, sqrt_price_x96=get_sqrt_ratio_at_tick(500_000))
    got = helper(high, "A")
    check(got is not None and got.slack is None, "rate-only direction")
    assert got is not None
    equal(got.rate, rate_of(high, "A"), "rate-only rate")
    equal(slack_of(high, "A"), None, "rate-only slack (module)")
    rows.append(
        {
            "state": "cl: sqrt_price_x96 >= 2**128, token0 -> token1",
            "bound": "rate only",
            "exact_quote_status_at_100": None,
        }
    )
    return rows


def _prepare_record() -> dict[str, Any]:
    """The bound table of the tracked 19-pool fixture, counted from this module's formulae."""
    bundle = real_bundle()
    counts: Counter[str] = Counter()
    rate_only = []
    for pid, p in bundle.pools.items():
        for token in (p.token0, p.token1):
            rate, slack = rate_of(p, token), slack_of(p, token)
            counts["pool_directions"] += 1
            if rate is None:
                counts["no_bound"] += 1
            elif slack is None:
                counts["rate_only"] += 1
                rate_only.append([pid, token])
            else:
                counts["bounded"] += 1
    expected = {
        k: counts.get(k, 0) for k in ("pool_directions", "bounded", "rate_only", "no_bound")
    }
    equal(build_bounds(bundle).prepare_record(), expected, "bound table of the tracked fixture")
    return {**expected, "rate_only_directions": [[a[:10], b[:10]] for a, b in rate_only]}


def example_bounds() -> dict[str, Any]:
    return {
        "no_bound_probe_amount": PROBE,
        "hand_rates": _hand_rates(),
        "r2": _r2(),
        "real_pools": _real_pools(),
        "multi_hop": _multi_hop(),
        "directions": _directions(),
        "swapped_state": _swapped_state(),
        "no_bound": _no_bound(),
        "fixture_table": _prepare_record(),
    }


# ====================================================================== 2. single_path_bounded

SP_REF, SP_B = "single_path", "single_path_bounded"


def _compare_s1(
    bundle: SnapshotBundle, case: Case, hops: int, what: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Reference and bounded factory runs next to the S1 oracle: same result, and the
    factory's counters and quote counts equal what the contract's rule says."""
    ref, got = run(SP_REF, bundle, case, hops=hops), run(SP_B, bundle, case, hops=hops)
    oracle = s1_oracle(bundle, case, hops)
    plain = s1_oracle(bundle, case, hops, prune=False)
    same_result(ref, got, what)
    equal(
        got["score"],
        None if oracle["best"] is None else oracle["best"]["score"],
        f"{what}: oracle best",
    )
    c = oracle["counts"]
    equal(
        counters(got),
        {
            "pruned_bound": c.get("pruned_bound", 0),
            "bound_evaluations": c.get("bound_evaluations", 0),
            "bound_no_bound": c.get("no_bound", 0),
        },
        f"{what}: counters vs the S1 oracle",
    )
    equal(got["stats"]["paths_evaluated"], c.get("evaluated", 0), f"{what}: paths evaluated")
    equal(
        ref["stats"]["paths_evaluated"],
        plain["counts"].get("evaluated", 0),
        f"{what}: reference evaluated",
    )
    equal(ref["stats"]["quotes_executed"], plain["quotes"], f"{what}: reference quotes")
    equal(got["stats"]["quotes_executed"], oracle["quotes"], f"{what}: bounded quotes")
    equal(block(got)["exactness"], {"label": "exact", "binding": []}, f"{what}: label")
    equal(got["stats"]["paths_enumerated"], ref["stats"]["paths_enumerated"], f"{what}: enumerated")
    return ref, got, oracle


def _teaching_graph() -> dict[str, Any]:
    """A safe prune, a tie that is *not* pruned (the bound is a spot rate), a prefix bound and
    an incumbent change. 10 000 000 A -> B through 1e9-deep pools."""
    pools = {
        "ab1": ["A", "B", 10**9, 10**9],
        "ab2": ["A", "B", 10**9, 900_000_000],
        "ab3": ["A", "B", 10**9, 10**9],  # an exact twin of ab1
        "ac": ["A", "C", 10**9, 2 * 10**9],
        "cb1": ["C", "B", 10**9, 500_000_000],
        "cb2": ["C", "B", 10**9, 200_000_000],
        "ad": ["A", "D", 10**9, 10**9],
        "db": ["D", "B", 10**9, 1_050_000_000],
        "ae": ["A", "E", 10**9, 10**9],
        "eb": ["E", "B", 10**9, 800_000_000],
    }
    bundle = cpmm_bundle("sp-teaching", pools)
    case = request_case("A", "B", 10_000_000)
    ref, got, oracle = _compare_s1(bundle, case, 2, "teaching graph")
    paths = r21.simple_paths(bundle, "A", "B", 2)
    equal(len(paths), 7, "teaching graph: candidates enumerated")
    best = oracle["best"]
    equal(best, {"score": 10_233_347, "path": "ad>db"}, "teaching graph: best")
    decisions = {d["path"]: d for d in oracle["log"]}
    equal(decisions["ab1"]["output"], 9_871_580, "ab1 hand output")
    equal(
        decisions["ab2"],
        {
            "path": "ab2",
            "decision": "pruned",
            "bound": 8_973_000,
            "from_prefix": 0,
            "incumbent": 9_871_580,
        },
        "ab2 pruned",
    )
    equal(
        (decisions["ab3"]["decision"], decisions["ab3"]["output"], decisions["ab3"]["bound"]),
        ("evaluated", 9_871_580, 9_970_000),
        "ab3: tie, evaluated",
    )
    equal(got["plan"][0]["pool_id"], "ad", "the incumbent changed to ad>db")
    return {
        "request": r21.request(case),
        "pools": pools,
        "ordered_candidates": [d["path"] for d in oracle["log"]],
        "log": oracle["log"],
        "best": best,
        "reference": {
            "quotes": ref["stats"]["quotes_executed"],
            "evaluated": ref["stats"]["paths_evaluated"],
        },
        "bounded": {
            "quotes": got["stats"]["quotes_executed"],
            "evaluated": got["stats"]["paths_evaluated"],
            **counters(got),
        },
        "winner_pools": r21.pools_of(got),
    }


def _equal_bound_tie() -> dict[str, Any]:
    """`UB == incumbent`: pruned. The reference would have evaluated the candidate, found an exact
    tie and kept the first pool (replacement is strict), so skipping on equality is safe."""
    pools = {"first": ["A", "B", 10**9, 10**9], "tie": ["A", "B", 10**9, 999_500_000]}
    bundle = cpmm_bundle("sp-tie", pools)
    case = request_case("A", "B", 1000)
    ref, got, oracle = _compare_s1(bundle, case, 1, "equal-bound tie")
    d = {x["path"]: x for x in oracle["log"]}
    equal(d["tie"]["decision"], "pruned", "tie pool pruned")
    equal((d["tie"]["bound"], d["tie"]["incumbent"]), (996, 996), "bound equals the incumbent")
    tie_pool = bundle.pools["tie"]
    assert isinstance(tie_pool, ConstantProductPoolState)
    equal(r21.hand_out(tie_pool, "A", 1000), 996, "the tie pool's exact output is also 996")
    tie_rate = rate_of(tie_pool, "A")
    assert tie_rate is not None
    check(
        Fraction(99650, 100000) <= tie_rate < Fraction(99651, 100000), "tie pool rate is 0.99650..."
    )
    equal(r21.pools_of(ref), ["first"], "the reference keeps the first pool on a tie")
    equal(ref["stats"]["paths_evaluated"], 2, "the reference evaluates the tie pool")
    equal(got["stats"]["paths_evaluated"], 1, "the bounded run does not")
    return {
        "pools": pools,
        "request": 1000,
        "incumbent": 996,
        "tie_bound": d["tie"]["bound"],
        "tie_exact": 996,
        "reference_winner": r21.pools_of(ref),
        "bounded_winner": r21.pools_of(got),
        **counters(got),
    }


def _no_bound_real_state() -> dict[str, Any]:
    """The 19-pool real fixture, 10 000 USDC -> USDT0. As is, then with the Agni pool's frozen
    tick one above its price (guard `G_CL` fails: no bound for that pool)."""
    bundle = real_bundle()
    case = request_case(r21.USDC, r21.USDT0, 10_000_000_000, "real_usdc_usdt0_10k")
    agni = first_pool(bundle, "0x36f66548")
    stale = dataclasses.replace(agni, tick=agni.tick + 1)
    check(
        helper(stale, r21.USDC) is None and rate_of(stale, r21.USDC) is None, "stale tick: no bound"
    )
    stale_bundle = dataclasses.replace(bundle, pools={**bundle.pools, agni.pool_id: stale})
    out: dict[str, Any] = {}
    paths = r21.simple_paths(bundle, case.token_in, case.token_out, 3)
    for label, b in (("as_is", bundle), ("stale_tick", stale_bundle)):
        ref, got, oracle = _compare_s1(b, case, 3, f"real state {label}")
        exact_best = max(
            (
                v
                for v in (r21._exact_chain(b, case.token_in, case.amount_in, p) for p in paths)
                if v is not None
            ),
            default=None,
        )
        equal(got["score"], exact_best, f"real state {label}: best exact chain")
        out[label] = {
            "score": got["score"],
            "winner": r21.pools_of(got)[0][:10],
            "enumerated": got["stats"]["paths_enumerated"],
            **counters(got),
            "order": [
                (x["path"][:10], "evaluated, no bound" if x.get("no_bound") else x["decision"])
                for x in oracle["log"]
            ],
        }
    equal(out["as_is"]["bound_no_bound"], 0, "as is: every hop has a bound")
    equal(out["stale_tick"]["bound_no_bound"], 1, "stale tick: one evaluation without a bound")
    equal(out["as_is"]["score"], out["stale_tick"]["score"], "same winner with and without bound")
    return out


def _tracked_fixture_s1() -> dict[str, Any]:
    """Every case of the tracked 96-case fixture: the bounded result equals the reference and the
    factory's counters and quote counts equal the S1 oracle's (CL and LB rates included)."""
    bundle = real_bundle()
    total: Counter[str] = Counter()
    for case in bundle.cases:
        ref, got, oracle = _compare_s1(bundle, case, 3, case.case_id)
        total["cases"] += 1
        total["identical"] += 1
        total["pruned_bound"] += oracle["counts"].get("pruned_bound", 0)
        total["quotes_reference"] += ref["stats"]["quotes_executed"]
        total["quotes_bounded"] += got["stats"]["quotes_executed"]
        total["cases_with_a_prune"] += oracle["counts"].get("pruned_bound", 0) > 0
    return dict(total)


def example_single_path_bounded() -> dict[str, Any]:
    return {
        "teaching_graph": _teaching_graph(),
        "equal_bound_tie": _equal_bound_tie(),
        "no_bound": _no_bound_real_state(),
        "tracked_fixture": _tracked_fixture_s1(),
    }


# ================================================================= 3. incremental_graph_bounded

IG_REF, IG_B = "incremental_graph", "incremental_graph_bounded"


def _chunk_params(hops: int, chunks: int, splits: int = 2, step: int = 25) -> dict[str, int]:
    return {"max_hops": hops, "max_splits": splits, "percent_step": step, "chunks": chunks}


def _compare_i1(
    bundle: SnapshotBundle,
    case: Case,
    hops: int,
    chunks: int,
    what: str,
    *,
    splits: int = 2,
    step: int = 25,
    retained: bool = True,
    hand: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Reference and bounded runs next to the I1 oracle: the same plan, the factory's counters
    equal the oracle's and the incremental stage's accounted gross is the oracle's."""
    ref = run(IG_REF, bundle, case, hops=hops, chunks=chunks, splits=splits, step=step, hand=hand)
    got = run(IG_B, bundle, case, hops=hops, chunks=chunks, splits=splits, step=step, hand=hand)
    oracle = i1_oracle(bundle, case, chunks, hops, retained=retained)
    same_result(ref, got, what)
    c = oracle["counts"]
    equal(
        counters(got),
        {
            "pruned_bound": c.get("pruned_bound", 0),
            "bound_evaluations": c.get("bound_evaluations", 0),
            "bound_no_bound": c.get("no_bound", 0),
        },
        f"{what}: counters vs the I1 oracle",
    )
    equal(got["stats"]["paths_scored"], c["scored"], f"{what}: paths scored")
    equal(ref["stats"]["paths_scored"], c["scored"], f"{what}: reference paths scored")
    equal(got["stats"]["paths_rejected_cycle"], c.get("rejected_cycle", 0), f"{what}: cycles")
    equal(
        got["stats"]["incremental_accounted_gross"],
        str(oracle["gross"]),
        f"{what}: accounted gross",
    )
    equal(block(got)["p0"], retained, f"{what}: P0")
    equal(block(got)["exactness"], {"label": "exact", "binding": []}, f"{what}: label")
    return ref, got, oracle


def _sequence_pools(oracle: Mapping[str, Any]) -> list[list[str]]:
    return [s["pools"] for s in oracle["sequence"]]


def _chunk_teaching() -> dict[str, Any]:
    """Four chunks of 100 000 into a graph of two direct pools and three two-hop routes."""
    pools = {
        "ab1": ["A", "B", 1_000_000, 1_000_000],
        "ab2": ["A", "B", 500_000, 480_000],
        "ac": ["A", "C", 1_000_000, 2_000_000],
        "cb": ["C", "B", 1_000_000, 600_000],
        "cb2": ["C", "B", 1_000_000, 300_000],
        "ad": ["A", "D", 800_000, 800_000],
        "db": ["D", "B", 800_000, 700_000],
    }
    bundle = cpmm_bundle("ig-teaching", pools)
    case = request_case("A", "B", 400_000)
    ref, got, oracle = _compare_i1(bundle, case, 2, 4, "chunk teaching graph")
    greedy = r21.greedy_chunks(bundle, case, 4, 2)
    equal(
        _sequence_pools(oracle),
        [s["pools"] for s in greedy["sequence"]],
        "chunk sequence vs greedy oracle",
    )
    equal(
        [s["marginal"] for s in oracle["sequence"]],
        [s["marginal"] for s in greedy["sequence"]],
        "chunk marginals vs greedy oracle",
    )
    equal(oracle["gross"], greedy["gross"], "accounted gross vs greedy oracle")
    equal(
        oracle["counts"],
        {"scored": 20, "bound_evaluations": 16, "pruned_bound": 6},
        "teaching counts",
    )
    equal(got["stats"]["chosen_source"], IG_REF, "the incremental plan is the final plan")
    return {
        "request": r21.request(case),
        "pools": pools,
        "chunks": 4,
        "sequence": oracle["sequence"],
        "gross": oracle["gross"],
        "counts": oracle["counts"],
        "decisions": oracle["decisions"],
        "bounded": counters(got),
    }


def _chunk_slack() -> dict[str, Any]:
    """Why the slack is in the bound: two pools, 106 units, 40 chunks. Without the slack the
    unsafe rule skips a candidate whose rounded marginal exceeds `floor(r m)`."""
    pools = {"p0": ["A", "B", 1024, 1334], "p1": ["A", "B", 1023, 1442]}
    bundle = cpmm_bundle("ig-slack", pools)
    case = request_case("A", "B", 106)
    ref, got, oracle = _compare_i1(bundle, case, 1, 40, "slack cell", splits=2)
    unsafe = i1_oracle(bundle, case, 40, 1, slack_scale=Fraction(0))
    greedy = r21.greedy_chunks(bundle, case, 40, 1)
    equal(oracle["gross"], greedy["gross"], "slack cell: gross vs greedy oracle")
    equal((greedy["gross"], unsafe["gross"]), (134, 133), "slack cell: with vs without slack")
    first_diff = next(
        (a["chunk"], a["pools"], b["pools"])
        for a, b in zip(greedy["sequence"], unsafe["sequence"], strict=True)
        if a["pools"] != b["pools"] or a["marginal"] != b["marginal"]
    )
    equal(first_diff, (6, ["p1"], ["p0"]), "first chunk the slack-free rule gets wrong")
    return {
        "pools": pools,
        "request": r21.request(case),
        "chunks": 40,
        "gross_exact": greedy["gross"],
        "with_slack": {**oracle["counts"], "gross": oracle["gross"]},
        "without_slack": {**unsafe["counts"], "gross": unsafe["gross"]},
        "first_wrong_chunk": {
            "chunk": first_diff[0],
            "exact": first_diff[1],
            "slack_free": first_diff[2],
        },
        "factory": counters(got),
    }


def _chunk_tie() -> dict[str, Any]:
    """A tie is evaluated (the bound is above the choice), equality with the choice is pruned."""
    pools = {
        "p1": ["A", "B", 10**12, 10**12],
        "p2": ["A", "B", 10**12, 10**12],
        "p3": ["A", "B", 10**12, 998_500_000_000],
        "p4": ["A", "B", 10**12, 990_000_000_000],
    }
    bundle = cpmm_bundle("ig-tie", pools)
    case = request_case("A", "B", 1001)
    ref, got, oracle = _compare_i1(bundle, case, 1, 1, "tie cell")
    d = {x["path"]: x for x in oracle["decisions"][0]["candidates"]}
    p1 = bundle.pools["p1"]
    assert isinstance(p1, ConstantProductPoolState)
    exact = r21.hand_out(p1, "A", 1001)
    equal(
        (d["p1"]["marginal"], d["p2"]["marginal"], exact),
        (exact, exact, 997),
        "p1 and p2 tie at 997",
    )
    equal(
        d["p3"],
        {"path": "p3", "decision": "pruned", "bound": 997, "from_prefix": 0, "choice": 997},
        "p3: bound equals the choice",
    )
    equal(d["p4"]["decision"], "pruned", "p4 pruned")
    equal(r21.pools_of(got), ["p1"], "the first pool keeps the chunk")
    return {
        "pools": pools,
        "request": 1001,
        "choice": 997,
        "candidates": oracle["decisions"][0]["candidates"],
        **counters(got),
    }


def _chunk_failed_prefix() -> dict[str, Any]:
    """A memoized failure is not bounded: the reference answers it for free. `ovf` is a Moe
    Classic pool whose reserve sits 10 below the `uint112` ceiling, so 1000 more input reverts."""
    ceiling = 1 << 112
    bundle = r21.bundle_of(
        "ig-failed",
        r21.cp("ab", "A", "B", 10**9, 10**9, 30),
        r21.cp("ovf", "A", "C", ceiling - 10, 10**40, 30, "moe_classic_v1"),
        r21.cp("cb1", "C", "B", 10**9, 10**9, 30),
        r21.cp("cb2", "C", "B", 10**9, 9 * 10**8, 30),
    )
    case = request_case("A", "B", 1000)
    ovf = bundle.pools["ovf"]
    assert isinstance(ovf, ConstantProductPoolState)
    equal(r21.hand_out(ovf, "A", 1000), None, "ovf: the post-swap reserve reaches 2**112")
    ref, got, oracle = _compare_i1(bundle, case, 2, 1, "failed prefix", hand=False)
    log = {x["path"]: x for x in oracle["decisions"][0]["candidates"]}
    equal(
        [log[p]["decision"] for p in ("ab", "ovf>cb1", "ovf>cb2")],
        ["evaluated", "failed", "failed"],
        "failed-prefix decisions",
    )
    equal(
        oracle["counts"],
        {"scored": 3, "bound_evaluations": 1},
        "bound evaluated once, not for ovf>cb2",
    )
    equal(
        ref["stats"]["marginal_failures"],
        got["stats"]["marginal_failures"],
        "failures disclosed alike",
    )
    return {
        "request": 1000,
        "decisions": oracle["decisions"][0]["candidates"],
        "counts": oracle["counts"],
        "marginal_failures": got["stats"]["marginal_failures"],
    }


def _chunk_p0() -> dict[str, Any]:
    """P0: pruning is off unless the retained simpler candidate exists. Two Moe Classic pools
    whose reserve is 100 below `uint112`: the full 150 reverts on either, 75 fits. With
    `max_splits 1` `path_split` has no plan; with `max_splits 2` it has one."""
    ceiling = 1 << 112
    bundle = r21.bundle_of(
        "ig-p0",
        r21.cp("m1", "A", "B", ceiling - 100, 10**40, 30, "moe_classic_v1"),
        r21.cp("m2", "A", "B", ceiling - 100, 10**40, 30, "moe_classic_v1"),
    )
    case = request_case("A", "B", 150)
    m1_pool = bundle.pools["m1"]
    assert isinstance(m1_pool, ConstantProductPoolState)
    margin = ceiling - m1_pool.reserve0
    out: dict[str, Any] = {"request": 150, "per_leg": 75, "reserve_below_ceiling": margin}
    equal(margin, 100, "the Moe reserves sit 100 below 2**112")
    for label, splits, retained in (("max_splits_1", 1, False), ("max_splits_2", 2, True)):
        ref, got, oracle = _compare_i1(
            bundle, case, 1, 2, f"P0 {label}", splits=splits, retained=retained
        )
        ps = got["stats"]["path_split_status"]
        equal(ps, "ok" if retained else "no_route", f"P0 {label}: path_split status")
        equal(r21.pools_of(got), ["m1", "m2"], f"P0 {label}: plan")
        out[label] = {
            "path_split_status": ps,
            "p0": block(got)["p0"],
            "plan": r21.pools_of(got),
            "gross": got["score"],
            "chosen_source": got["stats"]["chosen_source"],
            **counters(got),
        }
    equal(out["max_splits_1"]["gross"], out["max_splits_2"]["gross"], "same gross either way")
    equal(out["per_leg"] * 2, case.amount_in, "two legs of 75 spend the 150")
    m1 = bundle.pools["m1"]
    assert isinstance(m1, ConstantProductPoolState)
    equal(
        out["max_splits_1"]["gross"],
        2 * (r21.hand_out(m1, "A", 75) or 0),
        "gross = two hand quotes at 75",
    )
    return out


def _chunk_domain() -> dict[str, Any]:
    """The proved domain `committed + m <= 2**127`: above it a hop has no bound."""
    pools = {"ab1": ["A", "B", 10**45, 10**45], "ab2": ["A", "B", 10**45, 8 * 10**44]}
    bundle = cpmm_bundle("ig-domain", pools)
    out = {}
    for label, amount in (("below", 10**37), ("above", 10**39)):
        case = request_case("A", "B", amount)
        _ref, got, oracle = _compare_i1(bundle, case, 1, 1, f"domain {label}")
        out[label] = {"amount": amount, "amount_over_2_127": amount > TWO127, **counters(got)}
    equal((out["below"]["pruned_bound"], out["above"]["pruned_bound"]), (1, 0), "domain: prunes")
    equal(out["above"]["bound_no_bound"], 1, "domain: the hop above 2**127 has no bound")
    return out


def _chunk_rate_only() -> dict[str, Any]:
    """A real rate-only direction (the fixture's FusionX pool at its price ceiling, rate 3.4e38,
    no slack) next to a CPMM pool: single_path_bounded can use the rate, the chunk strategies
    never prune on it."""
    bundle = real_bundle()
    fx = first_pool(bundle, "0x283fdeea")
    meth, weth = fx.token0, fx.token1
    cpmm = r21.cp("meth_weth", meth, weth, 10**21, 10**21, 30)
    mini = r21.bundle_of("ig-rate-only", cpmm, fx)
    case = request_case(meth, weth, 10**12)
    got_bound = helper(fx, meth)
    check(got_bound is not None and got_bound.slack is None, "fx: rate only")
    ref, got, oracle = _compare_i1(mini, case, 1, 2, "rate-only chunk", hand=False)
    sp_ref, sp_got, sp_oracle = _compare_s1(mini, case, 1, "rate-only S1")
    equal(counters(got)["pruned_bound"], 0, "rate-only: nothing pruned")
    check(counters(got)["bound_no_bound"] > 0, "rate-only: counted as no bound")
    rate = rate_of(fx, meth)
    assert rate is not None
    mantissa = (rate * 10) // 10**38  # the rate is about 3.4e38
    equal(mantissa, 34, "the ceiling pool's rate is about 3.4e38")
    return {
        "chunk": counters(got),
        "single_path": counters(sp_got),
        "winner": r21.pools_of(got),
        "single_path_winner": r21.pools_of(sp_got),
        "rate_mantissa_1dp": f"{mantissa // 10}.{mantissa % 10}",
        "rate_exponent": 38,
    }


def _tracked_fixture_i1() -> dict[str, Any]:
    bundle = real_bundle()
    total: Counter[str] = Counter()
    for case in bundle.cases:
        _ref, got, oracle = _compare_i1(bundle, case, 3, 8, case.case_id, hand=False)
        total["cases"] += 1
        total["identical"] += 1
        total["paths_scored"] += oracle["counts"]["scored"]
        total["pruned_bound"] += oracle["counts"].get("pruned_bound", 0)
        total["bound_evaluations"] += oracle["counts"].get("bound_evaluations", 0)
        total["p0"] += block(got)["p0"]
    return dict(total)


def example_incremental_graph_bounded() -> dict[str, Any]:
    return {
        "teaching": _chunk_teaching(),
        "slack": _chunk_slack(),
        "tie": _chunk_tie(),
        "failed_prefix": _chunk_failed_prefix(),
        "p0": _chunk_p0(),
        "domain": _chunk_domain(),
        "rate_only": _chunk_rate_only(),
        "tracked_fixture": _tracked_fixture_i1(),
    }


# ====================================================================== 4. metis_history_bounded

MH_REF, MH_B = "metis_history", "metis_history_bounded"
MH_DIFFERS = ("label_relaxations", "quotes_executed")


def _mh_run(
    name: str,
    bundle: SnapshotBundle,
    case: Case,
    *,
    hops: int,
    chunks: int,
    label_hops: int,
    options: Mapping[str, Any],
    splits: int = 2,
    step: int = 25,
    hand: bool = True,
) -> dict[str, Any]:
    return run(
        name,
        bundle,
        case,
        hops=hops,
        chunks=chunks,
        label_hops=label_hops,
        splits=splits,
        step=step,
        options=options,
        hand=hand,
    )


def _mh_pair(
    bundle: SnapshotBundle,
    case: Case,
    what: str,
    *,
    hops: int,
    chunks: int,
    label_hops: int,
    options: Mapping[str, Any],
    splits: int = 2,
    step: int = 25,
) -> tuple[dict[str, Any], dict[str, Any]]:
    kw: dict[str, Any] = {
        "hops": hops,
        "chunks": chunks,
        "label_hops": label_hops,
        "options": options,
        "splits": splits,
        "step": step,
    }
    ref = _mh_run(MH_REF, bundle, case, **kw)
    got = _mh_run(MH_B, bundle, case, **kw)
    same_result(ref, got, what)
    equal(block(got)["exactness"], {"label": "exact", "binding": []}, f"{what}: label")
    return ref, got


def _chain() -> dict[str, Any]:
    """A chain with no merging walks, the preset options: `G_M2` opens and both rules act."""
    pools = {
        "ab": ["A", "B", 10**9, 2 * 10**9],
        "bc": ["B", "C", 3 * 10**9, 10**9],
        "cd": ["C", "D", 10**9, 10**9],
        "ad": ["A", "D", 10**8, 10**8],  # a poor direct pool, so there is something to skip
    }
    bundle = cpmm_bundle("mh-chain", pools)
    case = request_case("A", "D", 10**7)
    equal(gate_oracle(bundle, case, 3, PRESET), ("open", True), "chain: G_M2 under the preset")
    ref, got = _mh_pair(
        bundle, case, "chain", hops=3, chunks=3, label_hops=3, options=PRESET, step=50
    )
    m2 = label_oracle(bundle, case, 3, 3, m2=True)
    m1 = label_oracle(bundle, case, 3, 3, m2=False)
    greedy = r21.greedy_chunks(bundle, case, 3, 3)
    equal(m2["gross"], greedy["gross"], "chain: gross vs the greedy chunk oracle")
    equal(_sequence_pools(m2), [s["pools"] for s in greedy["sequence"]], "chain: chunk paths")
    b = block(got)
    equal(
        (b["rule"], b["m2"], b["pruned_bound"], b["bound_evaluations"], b["bound_table_cost"]),
        (
            "M1+M2",
            {"active": True, "gate": "open"},
            m2["counts"]["pruned_bound"],
            m2["counts"]["bound_evaluations"],
            m2["bound_table_cost"],
        ),
        "chain: M1+M2 counters",
    )
    equal(got["stats"]["label_relaxations"], m2["counts"]["relaxations"], "chain: relaxations (M2)")
    equal(
        ref["stats"]["label_relaxations"],
        m1["counts"]["relaxations"],
        "chain: relaxations (reference)",
    )
    equal(got["stats"]["incremental_accounted_gross"], str(m2["gross"]), "chain: accounted gross")
    return {
        "pools": pools,
        "request": r21.request(case),
        "chunks": 3,
        "gate": ["open", True],
        "rule": b["rule"],
        "m2": b["m2"],
        "m2_skips": m2["skipped"],
        "m1_only_oracle": {**m1["counts"], "skipped": m1["skipped"]},
        "relaxations": {
            "reference": ref["stats"]["label_relaxations"],
            "bounded": got["stats"]["label_relaxations"],
        },
        "counters": counters(got),
        "bound_table_cost": b["bound_table_cost"],
        "sequence": m2["sequence"],
        "gross": m2["gross"],
    }


def _frontier_cap() -> dict[str, Any]:
    """`pruning-contract.md` §6.4: `dominance: off`, a 3-label frontier. The gate is closed
    (`closed:frontier`), M1 alone reproduces the reference; forcing M2 open finds a *better*
    plan than the reference, which is not the reference."""
    graph = load_bundle(CPMM_GRAPH)
    case = next(c for c in graph.cases if c.case_id == "a_d_multi_hop")
    equal(gate_oracle(graph, case, 3, CAPPED_OFF), ("closed:frontier", True), "§6.4 gate")
    equal(gate_oracle(graph, case, 3, OFF), ("open", True), "§6.4 gate with an ample frontier")
    ref, got = _mh_pair(
        graph, case, "frontier cap", hops=2, chunks=24, label_hops=3, options=CAPPED_OFF
    )
    m1 = label_oracle(graph, case, 24, 3, m2=False, frontier_cap=3)
    forced_oracle = label_oracle(graph, case, 24, 3, m2=True, frontier_cap=3)
    b = block(got)
    equal(m1["gross"], ref["score"], "§6.4: M1-only oracle gross vs the reference")
    equal((b["rule"], b["m2"]), ("M1", {"active": False, "gate": "closed:frontier"}), "§6.4: rule")
    equal(
        (b["pruned_bound"], b["bound_evaluations"]),
        (m1["counts"]["pruned_bound"], m1["counts"]["bound_evaluations"]),
        "§6.4: M1 counters",
    )
    for key in (
        "labels_dropped_frontier_cap",
        "chunks_state_capped",
        "termination",
        "label_relaxations",
    ):
        equal(got["stats"][key], ref["stats"][key], f"§6.4: {key} equals the reference's")
    equal(got["stats"]["labels_dropped_frontier_cap"], m1["counts"]["capped"], "§6.4: R7 refusals")
    # the negative control: the gate forced open (what `G_M2` exists to prevent)
    real_gate = chunk_pruning.m2_gate
    chunk_pruning.m2_gate = lambda *a, **k: ("open", True)
    try:
        forced = _mh_run(MH_B, graph, case, hops=2, chunks=24, label_hops=3, options=CAPPED_OFF)
    finally:
        chunk_pruning.m2_gate = real_gate
    fb = block(forced)
    equal(fb["m2"]["active"], True, "forced: M2 ran")
    equal(forced["score"], forced_oracle["gross"], "forced: gross vs the M2 oracle")
    equal(
        (fb["pruned_bound"], fb["bound_evaluations"], fb["bound_table_cost"]),
        (
            forced_oracle["counts"]["pruned_bound"],
            forced_oracle["counts"]["bound_evaluations"],
            forced_oracle["bound_table_cost"],
        ),
        "forced: M2 counters vs the oracle",
    )
    check(forced["score"] != ref["score"], "forced M2 changed the plan")
    return {
        "request": r21.request(case),
        "options": dict(CAPPED_OFF),
        "chunks": 24,
        "gate": "closed:frontier",
        "reference_gross": ref["score"],
        "relaxations": got["stats"]["label_relaxations"],
        "bounded": {
            **counters(got),
            "rule": b["rule"],
            "frontier_refusals": m1["counts"]["capped"],
        },
        "forced_m2": {
            "gross": forced["score"],
            "frontier_refusals": forced_oracle["counts"]["capped"],
            "pruned_bound": fb["pruned_bound"],
            "bound_table_cost": fb["bound_table_cost"],
        },
        "first_chunk": m1["sequence"][0],
    }


def _gate_sweep() -> dict[str, Any]:
    """The tracked 96-case fixture under the shipped preset and under `dominance: off`."""
    bundle = real_bundle()
    out: dict[str, Any] = {}
    for label, options in (("preset", PRESET), ("dominance_off", OFF)):
        gates: Counter[str] = Counter()
        active = 0
        for case in bundle.cases:
            _ref, got = _mh_pair(
                bundle,
                case,
                f"{label} {case.case_id}",
                hops=3,
                chunks=8,
                label_hops=3,
                options=options,
            )
            gate, labels = gate_oracle(bundle, case, 3, options)
            b = block(got)
            equal(
                b["m2"],
                {"active": gate == "open" and labels, "gate": gate},
                f"{label} {case.case_id}: gate and activity vs the oracle",
            )
            gates[f"{gate}{'' if labels else ' (no label can exist)'}"] += 1
            active += b["m2"]["active"]
        out[label] = {
            "cases": len(bundle.cases),
            "identical": len(bundle.cases),
            "gates": dict(gates),
            "m2_active": active,
        }
    equal(out["preset"]["m2_active"], 0, "the preset never activates M2 on the fixture")
    return out


def _p0_metis() -> dict[str, Any]:
    """P0 for the label search: the same two Moe pools as the chunk example, `max_splits 1`."""
    ceiling = 1 << 112
    bundle = r21.bundle_of(
        "mh-p0",
        r21.cp("m1", "A", "B", ceiling - 100, 10**40, 30, "moe_classic_v1"),
        r21.cp("m2", "A", "B", ceiling - 100, 10**40, 30, "moe_classic_v1"),
    )
    case = request_case("A", "B", 150)
    ref, got = _mh_pair(
        bundle, case, "metis P0", hops=1, chunks=2, label_hops=2, options=PRESET, splits=1
    )
    b = block(got)
    equal(
        (b["p0"], b["m2"], b["pruned_bound"], b["bound_evaluations"]),
        (False, {"active": False, "gate": "n/a"}, 0, 0),
        "metis P0 block",
    )
    equal(got["stats"]["path_split_status"], "no_route", "metis P0: path_split has no plan")
    return {"p0": b["p0"], "m2": b["m2"], "plan": r21.pools_of(got), "gross": got["score"]}


def example_metis_history_bounded() -> dict[str, Any]:
    return {
        "chain": _chain(),
        "frontier_cap": _frontier_cap(),
        "gate_sweep": _gate_sweep(),
        "p0": _p0_metis(),
    }


# ====================================================================== 5. roster and presets

ALL_ROSTER = [
    "direct",
    "single_path",
    "direct_split",
    "path_split",
    "incremental_graph",
    "uni_sor_port",
    "uni_sor_adaptive",
    "uni_sor_optimized",
    "metis_inspired",
    "metis_history",
    "direct_split_certified",
    "incremental_graph_repair",
    "uni_sor_cycle_safe",
    "cfmm_dual",
    "single_path_bounded",
    "incremental_graph_bounded",
    "metis_history_bounded",
]
BOUNDED = ("single_path_bounded", "incremental_graph_bounded", "metis_history_bounded")


def _walkthrough() -> dict[str, Any]:
    """The §11 request (10 000 USDC -> USDT0 on the 19-pool fixture, `daily_gross` values) through
    the three bounded strategies: the reference's plan and counted quotes, and what the rules
    say."""
    bundle = real_bundle()
    case = request_case(r21.USDC, r21.USDT0, 10_000_000_000, "real_usdc_usdt0_10k")
    out: dict[str, Any] = {}
    sp_ref, sp_got, sp_oracle = _compare_s1(bundle, case, 2, "walkthrough single_path")
    ig_ref, ig_got, ig_oracle = _compare_i1(
        bundle, case, 2, 200, "walkthrough incremental_graph", splits=4, step=5, hand=False
    )
    mh_ref, mh_got = _mh_pair(
        bundle,
        case,
        "walkthrough metis_history",
        hops=2,
        chunks=200,
        label_hops=4,
        options=PRESET,
        splits=4,
        step=5,
    )
    mh_oracle = label_oracle(bundle, case, 200, 4, m2=False)
    b = block(mh_got)
    equal(
        (b["pruned_bound"], b["bound_evaluations"]),
        (mh_oracle["counts"].get("pruned_bound", 0), mh_oracle["counts"]["bound_evaluations"]),
        "walkthrough metis_history: M1 counters vs the label oracle",
    )
    equal(
        gate_oracle(bundle, case, 4, PRESET),
        ("open", False),
        "walkthrough: gate, no label can exist",
    )
    equal(b["m2"], {"active": False, "gate": "open"}, "walkthrough: M2 inactive")
    for ref, got in ((sp_ref, sp_got), (ig_ref, ig_got), (mh_ref, mh_got)):
        equal(
            got["stats"]["quotes_executed"], ref["stats"]["quotes_executed"], "walkthrough quotes"
        )
    rows = {
        "single_path_bounded": (sp_got, sp_ref),
        "incremental_graph_bounded": (ig_got, ig_ref),
        "metis_history_bounded": (mh_got, mh_ref),
    }
    for name, (got, _ref) in rows.items():
        out[name] = {
            "score": got["score"],
            "quotes_counted": got["stats"]["quotes_executed"],
            **counters(got),
        }
    equal(
        [out[n]["score"] for n in rows],
        [10_000_660_449, 10_000_663_447, 10_000_663_447],
        "walkthrough scores",
    )
    return out


def example_roster() -> dict[str, Any]:
    """`--strategies all` is 17 after the three bounded strategies; `profile` replays literally;
    the bounded Metis strategy carries the reference's options under its own preset file."""
    import yaml

    from benchmark.profile import options_entry, strategy_group
    from benchmark.strategies import R022_ADDITIONS, derive

    _document, profile, _sha = r21.all_profile()
    equal(list(profile.algorithms), ALL_ROSTER, "`all` roster of daily_gross.yaml")
    equal(tuple(R022_ADDITIONS), BOUNDED, "R022_ADDITIONS order")
    equal([strategy_group(n) for n in BOUNDED], ["custom"] * 3, "bounded strategies are `custom`")
    source = (ROOT / "config/daily_gross.yaml").read_bytes()
    saved, replayed = derive(yaml.safe_load(source), "profile", source_path="s", source_sha256="x")
    check(not set(BOUNDED) & set(replayed.algorithms), "`profile` never gains a bounded strategy")
    ref_entry = options_entry(ALGORITHMS["metis_history"], dict(PRESET))
    bnd_entry = options_entry(ALGORITHMS["metis_history_bounded"], dict(PRESET))
    equal(
        bnd_entry["settings_sha256"],
        ref_entry["settings_sha256"],
        "settings_sha256 of the two presets",
    )
    check(str(ref_entry["settings_sha256"]).startswith("183bb1ff"), "pinned settings hash")
    files = {
        n: yaml.safe_load((ROOT / "config" / n / "preset_v1.yaml").read_text(encoding="utf-8"))
        for n in ("metis_history", "metis_history_bounded")
    }
    equal(
        files["metis_history"]["options"],
        files["metis_history_bounded"]["options"],
        "preset options",
    )
    equal(files["metis_history"]["options"], PRESET, "preset values")
    equal(
        (files["metis_history"]["algorithm"], files["metis_history_bounded"]["algorithm"]),
        ("metis_history", "metis_history_bounded"),
        "preset algorithm keys",
    )
    return {
        "all_roster": list(profile.algorithms),
        "bounded_additions": list(R022_ADDITIONS),
        "profile_mode_roster": list(replayed.algorithms),
        "settings_sha256_prefix": str(ref_entry["settings_sha256"])[:8],
        "preset_files": {
            n: {"algorithm": d["algorithm"], "key": d["key"], "options": d["options"]}
            for n, d in files.items()
        },
        "saved_profile_equal_to_source": list(saved["algorithms"]) == list(replayed.algorithms),
        "walkthrough": _walkthrough(),
    }


# ====================================================================== output

EXAMPLES: dict[str, Callable[[], dict[str, Any]]] = {
    "bounds": example_bounds,
    "single_path_bounded": example_single_path_bounded,
    "incremental_graph_bounded": example_incremental_graph_bounded,
    "metis_history_bounded": example_metis_history_bounded,
    "roster": example_roster,
}


def collect() -> dict[str, Any]:
    """Every example as plain data (each has already checked its independent expectations)."""
    return {name: fn() for name, fn in EXAMPLES.items()}


def _p(*parts: Any) -> None:
    print("".join(str(p) for p in parts))


def _print_bounds(d: Mapping[str, Any]) -> None:
    _p("--- 18. Upper-bound pruning: per-pool bounds, composition, slack ---")
    _p(f"hand-derived rates match the helper and this module: {len(d['hand_rates'])} cells")
    r2 = d["r2"]
    _p(
        f"R2 1000/1000 30 bps: q(0..6) = {r2['q']}, floor(997/1000 x) = {r2['floor_rate_x']}; "
        f"marginal q(2)-q(1) = {r2['marginal_x1_m1']} > 0.997; slack-free bound "
        f"{r2['slack_free_bound_m1']} vs {r2['bound_with_slack_m1']} with slack 1"
    )
    for row in d["real_pools"]:
        cells = ", ".join(f"{t['x']}: {t['q']} <= {t['floor_rate_x']}" for t in row["table"][:3])
        _p(f"  {row['pool']} {row['family']} rate {row['rate_6dp']}: {cells}, ...")
    m, c = d["multi_hop"], d["multi_hop"]["chunk"]
    _p(
        f"two hops {m['request']} -> {m['hop_outputs']}: nested bound {m['nested_bound']}, "
        f"product form {m['product_bound']}; chunk chain u = {c['chain_u']} over "
        f"marginals {c['marginals']}"
    )
    dr = d["directions"]
    same, opp = dr["same_direction"], dr["opposite_direction"]
    _p(
        f"same direction: {same['out']} <= {same['bound_from_original_state']}; opposite: "
        f"{opp['out']} > {opp['bound_from_original_state']} (evaluator: "
        f"{dr['cyclic_plan']['status']}: {dr['cyclic_plan']['error']})"
    )
    s = d["swapped_state"]
    _p(
        f"swapped-state bound {s['bound_swapped']} < marginal {s['marginal']} (excess "
        f"{s['excess']}); original-state bound {s['bound_original']}"
    )
    probe = d["no_bound_probe_amount"]
    for row in d["no_bound"]:
        _p(
            f"  no bound: {row['state']} -> {row['bound']} "
            f"(exact quote at {probe}: {row['exact_quote_status_at_100']})"
        )
    ft = d["fixture_table"]
    _p(
        f"tracked 19-pool fixture bound table: {ft['pool_directions']} directions, "
        f"{ft['bounded']} bounded, {ft['rate_only']} rate only {ft['rate_only_directions']}, "
        f"{ft['no_bound']} no bound [OK]\n"
    )


def _print_single_path(d: Mapping[str, Any]) -> None:
    _p("--- 19. single_path_bounded (rule S1) ---")
    t = d["teaching_graph"]
    _p(f"teaching graph, {t['request']['amount_in']} A->B, candidates in enumeration order:")
    for x in t["log"]:
        if x["decision"] == "pruned":
            extra = f"bound {x['bound']} <= incumbent {x['incumbent']}"
        else:
            extra = f"output {x['output']}, bound {x['bound']}"
        _p(f"  {x['path']:9s} {x['decision']:9s} {extra}")
    _p(f"  best {t['best']}; reference {t['reference']}; bounded {t['bounded']}")
    e = d["equal_bound_tie"]
    _p(
        f"equal-bound tie: incumbent {e['incumbent']}, tie pool bound {e['tie_bound']} "
        f"(exact {e['tie_exact']}) -> pruned; both keep {e['bounded_winner']}"
    )
    for label, row in d["no_bound"].items():
        _p(
            f"real USDC->USDT0 10 000 ({label}): {row['score']} via {row['winner']}, "
            f"no_bound {row['bound_no_bound']}, order {row['order']}"
        )
    f = d["tracked_fixture"]
    _p(
        f"tracked fixture: {f['identical']}/{f['cases']} cases identical to single_path; "
        f"S1 oracle = factory: {f['pruned_bound']} candidates skipped in "
        f"{f['cases_with_a_prune']} cases, quotes {f['quotes_reference']} -> "
        f"{f['quotes_bounded']} [OK]\n"
    )


def _print_incremental(d: Mapping[str, Any]) -> None:
    _p("--- 20. incremental_graph_bounded (rule I1) ---")
    t = d["teaching"]
    _p(
        f"teaching graph, {t['request']['amount_in']} A->B in {t['chunks']} chunks, "
        f"gross {t['gross']}:"
    )
    for dec in t["decisions"]:
        cells = ", ".join(
            f"{c['path']} {c['marginal']}"
            if c["decision"] == "evaluated"
            else f"{c['path']} skip<={c['bound']}"
            for c in dec["candidates"]
        )
        _p(f"  chunk {dec['chunk']}: {cells}")
    chosen = [(s["pools"], s["marginal"]) for s in t["sequence"]]
    _p(f"  chosen {chosen}; counts {t['counts']}")
    s = d["slack"]
    _p(
        f"slack cell: gross {s['gross_exact']}; with slack {s['with_slack']}; without slack "
        f"{s['without_slack']}; first wrong chunk {s['first_wrong_chunk']}"
    )
    tie = d["tie"]
    _p(
        f"tie cell: choice {tie['choice']}, pruned {tie['pruned_bound']} of "
        f"{tie['bound_evaluations']} bounds"
    )
    fp = d["failed_prefix"]
    _p(
        f"failed prefix: {[(c['path'], c['decision']) for c in fp['decisions']]}, "
        f"counts {fp['counts']}"
    )
    _p(f"P0: {d['p0']}")
    _p(f"domain: {d['domain']}")
    _p(f"rate only: {d['rate_only']}")
    f = d["tracked_fixture"]
    _p(
        f"tracked fixture: {f['identical']}/{f['cases']} identical; I1 oracle = factory: "
        f"{f['pruned_bound']} of {f['paths_scored']} scored paths skipped, "
        f"{f['bound_evaluations']} bound evaluations [OK]\n"
    )


def _print_metis(d: Mapping[str, Any]) -> None:
    _p("--- 21. metis_history_bounded (rules M1, M2 behind G_M2) ---")
    c = d["chain"]
    _p(
        f"chain, preset options: gate {c['gate']}, rule {c['rule']}, relaxations "
        f"{c['relaxations']}, counters {c['counters']}, table cost {c['bound_table_cost']}, "
        f"gross {c['gross']}"
    )
    for k in c["m2_skips"]:
        _p(
            f"  chunk {k['chunk']}: {k['rule']} skips {k['label_path']} "
            f"(bound {k['bound']} <= best {k['best']})"
        )
    f = d["frontier_cap"]
    _p(
        f"frontier cap 3, dominance off: gate {f['gate']}; reference {f['reference_gross']}, "
        f"bounded {f['bounded']}; forcing M2: {f['forced_m2']}"
    )
    for label, row in d["gate_sweep"].items():
        _p(f"tracked fixture ({label}): {row}")
    _p(f"P0: {d['p0']} [OK]\n")


def print_report(data: Mapping[str, Any]) -> None:
    """The inspectable transcript; every number comes from the checked example data."""
    _print_bounds(data["bounds"])
    _print_single_path(data["single_path_bounded"])
    _print_incremental(data["incremental_graph_bounded"])
    _print_metis(data["metis_history_bounded"])
    ro = data["roster"]
    _p("--- 22. Roster, presets and the section 11 request ---")
    _p(f"section 11 request through the bounded strategies: {ro['walkthrough']}")
    _p(
        f"--strategies all: {len(ro['all_roster'])} strategies; added after the 0.2.1 "
        f"identities: {ro['bounded_additions']}; profile mode: {len(ro['profile_mode_roster'])}; "
        f"metis_history_bounded settings sha256 {ro['settings_sha256_prefix']}... [OK]"
    )
    _p("All R022 worked-example suites passed (independent expectations; no timing claimed)")


def run_all() -> dict[str, Any]:
    data = collect()
    print_report(data)
    return data


if __name__ == "__main__":
    run_all()

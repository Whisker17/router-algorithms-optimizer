"""Executable worked examples for the five 0.2.1 experimental strategies (WHI-1561).

Every example calls the REAL registered factory (`routing.algorithms.registry.ALGORITHMS`)
with options resolved exactly like a profile resolves them (`benchmark.profile.options_entry`:
the pinned preset identity, a historical preset pin, or a visible `override`), replays every
emitted plan with a FRESH `routing.evaluator.evaluate` call, and checks each published amount,
allocation, state transition and bound against an expectation that does not come from the
implementation under test:

- `hand_out` / `hand_replay`: this module's own integer constant-product formula and fund
  ledger (the Solidity `getAmountOut` rule, the Moe Classic uint112 revert), sharing no code
  with `pools/` or `routing/`;
- this module's own exhaustive oracles (`simple_paths`, `greedy_chunks`,
  `chunk_sequences`, `grid_values`, `raw_values`, `label_layers`), written from the
  published contracts, not imported from the solvers;
- pinned research fixtures committed before the implementations
  (`docs/references/research-021/fixtures/*.json`, whose `oracle`/`expected`/`hand` values were
  derived by independent oracles) and the pinned upstream CFMMRouter.jl author run
  (`tests/fixtures/cfmm/author_reference.json`) plus the pre-implementation WHI-1557 Python
  contract model (`tests/fixtures/cfmm/model_reference.json`);
- for concentrated-liquidity legs, the exact protocol quote (`pools.quote.quote_exact_in`,
  the fork-evidence-validated money authority), never the continuous CFMM model.

Counters that no independent source derives (quotes executed, label relaxations, bound
evaluations, ...) are reported under a `factory_counters` key: they are what the factory
reported on this run, regression-pinned only, not independently derived teaching numbers.
Timings are never asserted.

Run: `uv run python docs/examples/routing-algorithms/r021_examples.py` (also called by
`run_examples.py`). `collect()` returns every example as plain JSON-shaped data.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from benchmark.objective import gross_only  # noqa: E402
from benchmark.profile import options_entry, preset_options  # noqa: E402
from benchmark.strategies import derive  # noqa: E402
from pools.cl_math import get_sqrt_ratio_at_tick  # noqa: E402
from pools.quote import quote_exact_in  # noqa: E402
from pools.result import QuoteStatus  # noqa: E402
from routing.algorithms.base import (  # noqa: E402
    AlgorithmConfig,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
)
from routing.algorithms.registry import ALGORITHMS  # noqa: E402
from routing.cfmm import cl as cfmm_cl  # noqa: E402
from routing.cfmm import model as cfmm_model  # noqa: E402
from routing.evaluator import Evaluation, evaluate  # noqa: E402
from routing.plan import (  # noqa: E402
    ALL_REMAINING,
    REQUEST_FUND_ID,
    FundInput,
    RoutePlan,
    SwapStep,
)
from snapshot.bundle import load_bundle  # noqa: E402
from snapshot.models import (  # noqa: E402
    BlockRef,
    Case,
    ConcentratedPoolState,
    ConstantProductPoolState,
    LiquidityBookPoolState,
    PoolState,
    SnapshotBundle,
    TickInfo,
)

R021 = ROOT / "docs" / "references" / "research-021" / "fixtures"
CFMM_FIX = ROOT / "tests" / "fixtures" / "cfmm"
FIXTURES = ROOT / "tests" / "fixtures"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


RECON = _json(R021 / "reconstructions.json")
HISTORY = _json(R021 / "history-labels.json")
INTEGER = _json(R021 / "integer-allocation.json")
SUFFIX = _json(R021 / "suffix-repair.json")
CYCLE = _json(R021 / "cycle-safe-sor.json")
AUTHOR_IN = _json(CFMM_FIX / "author_inputs.json")
AUTHOR = _json(CFMM_FIX / "author_reference.json")
MODEL = _json(CFMM_FIX / "model_reference.json")

BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)
UINT112 = 1 << 112
FEE_DEN = 10_000


class ExampleError(AssertionError):
    """A published worked-example value disagrees with its independent expectation."""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise ExampleError(message)


def equal(got: Any, want: Any, what: str) -> None:
    check(got == want, f"{what}: got {got!r}, expected {want!r}")


# ====================================================================== independent toolkit


def cp(
    pid: str, t0: str, t1: str, r0: int, r1: int, fee: int = 30, source: str | None = None
) -> ConstantProductPoolState:
    return ConstantProductPoolState(pid, t0, t1, int(r0), int(r1), fee, source)


def bundle_of(bundle_id: str, *pools: PoolState, cases: tuple[Case, ...] = ()) -> SnapshotBundle:
    return SnapshotBundle(
        bundle_id,
        "synthetic",
        1,
        BLOCK,
        {p.pool_id: p for p in pools},
        cases,
        bundle_id,
        "<r021_examples>",
    )


def fixture_bundle(
    name: str, pools: Mapping[str, Sequence[Any]], fee: int = 30, source: str | None = None
) -> SnapshotBundle:
    """`{pool_id: [token0, token1, reserve0, reserve1, (fee)]}` in insertion (= adjacency) order."""
    states = [
        cp(pid, s[0], s[1], s[2], s[3], int(s[4]) if len(s) > 4 else fee, source)
        for pid, s in pools.items()
    ]
    return bundle_of(name, *states)


def hand_out(
    pool: ConstantProductPoolState,
    token_in: str,
    amount: int,
    *,
    reserves: tuple[int, int] | None = None,
) -> int | None:
    """This module's own exact-input constant-product quote: `getAmountOut` with the pool fee,
    `None` for every failing swap (dead side, zero output, output >= reserve, the Moe Classic
    uint112 post-swap balance revert)."""
    if reserves is None:
        reserves = (
            (pool.reserve0, pool.reserve1)
            if token_in == pool.token0
            else (pool.reserve1, pool.reserve0)
        )
    r_in, r_out = reserves
    if amount <= 0 or r_in <= 0 or r_out <= 0:
        return None
    keep = FEE_DEN - pool.fee_bps
    out = amount * keep * r_out // (r_in * FEE_DEN + amount * keep)
    if out <= 0 or out >= r_out:
        return None
    if pool.source_key == "moe_classic_v1" and (pool.fee_bps != 30 or r_in + amount >= UINT112):
        return None
    return out


def chain_out(
    bundle: SnapshotBundle, token: str, amount: int, pool_ids: Sequence[str]
) -> int | None:
    """Hand quote along a path of pools on their ORIGINAL states."""
    for pid in pool_ids:
        pool = bundle.pools[pid]
        assert isinstance(pool, ConstantProductPoolState)
        out = hand_out(pool, token, amount)
        if out is None:
            return None
        amount, token = out, pool.token1 if token == pool.token0 else pool.token0
    return amount


def hand_replay(bundle: SnapshotBundle, case: Case, plan: RoutePlan) -> dict[str, Any]:
    """An independent fund ledger for an all-constant-product plan: every step's output from
    `hand_out` on the pool's CURRENT reserves (a reused pool sees the earlier leg), every fund
    drained or a target terminal balance (v1 full fill), the request fully spent and the plan
    token graph acyclic. Returns the gross and the per-step trace."""
    funds: dict[str, tuple[str, int]] = {REQUEST_FUND_ID: (case.token_in, case.amount_in)}
    reserves: dict[str, dict[str, int]] = {}
    trace: list[dict[str, Any]] = []
    edges: set[tuple[str, str]] = set()
    for step in plan.steps:
        pool = bundle.pools[step.pool_id]
        check(isinstance(pool, ConstantProductPoolState), f"{step.pool_id} is not CPMM")
        assert isinstance(pool, ConstantProductPoolState)
        total = 0
        for ref in step.inputs:
            token, balance = funds[ref.fund_id]
            check(token == step.token_in, f"fund {ref.fund_id} holds {token}")
            amount = balance if ref.amount == ALL_REMAINING else int(ref.amount)
            check(0 <= amount <= balance, f"fund {ref.fund_id} overdrawn")
            funds[ref.fund_id] = (token, balance - amount)
            total += amount
        res = reserves.setdefault(
            pool.pool_id, {pool.token0: pool.reserve0, pool.token1: pool.reserve1}
        )
        out = 0
        if total:
            got = hand_out(
                pool, step.token_in, total, reserves=(res[step.token_in], res[step.token_out])
            )
            check(got is not None, f"hand quote of {step.pool_id} failed for {total}")
            assert got is not None
            out = got
            res[step.token_in] += total
            res[step.token_out] -= out
            edges.add((step.token_in, step.token_out))
        funds[step.output_fund_id] = (step.token_out, out)
        trace.append(
            {
                "pool_id": step.pool_id,
                "token_in": step.token_in,
                "token_out": step.token_out,
                "amount_in": total,
                "amount_out": out,
            }
        )
    check(_acyclic(edges), "plan token graph has a cycle")
    gross = 0
    for fid, (token, balance) in funds.items():
        if token == case.token_out:
            gross += balance
        else:
            equal(balance, 0, f"residual in fund {fid}")
    return {"gross": gross, "trace": trace}


def _acyclic(edges: set[tuple[str, str]]) -> bool:
    nodes = {t for e in edges for t in e}
    indeg = {t: sum(1 for _, b in edges if b == t) for t in nodes}
    ready, seen = [t for t in nodes if indeg[t] == 0], 0
    while ready:
        t = ready.pop()
        seen += 1
        for a, b in edges:
            if a == t:
                indeg[b] -= 1
                if indeg[b] == 0:
                    ready.append(b)
    return seen == len(nodes)


def cycle_of(edges: set[tuple[str, str]]) -> bool:
    return not _acyclic(edges)


Edge = tuple[str, str, str]  # (pool_id, token_in, token_out)


def adjacency(bundle: SnapshotBundle, token: str) -> list[Edge]:
    """Edges out of `token` in bundle (pool insertion) order."""
    out = []
    for pid, p in bundle.pools.items():
        if token == p.token0:
            out.append((pid, p.token0, p.token1))
        elif token == p.token1:
            out.append((pid, p.token1, p.token0))
    return out


def simple_paths(bundle: SnapshotBundle, src: str, dst: str, hops: int) -> list[tuple[Edge, ...]]:
    """Every token-simple `src -> dst` path of 1..`hops` pools, hop-major then depth first."""
    found: list[tuple[Edge, ...]] = []
    for length in range(1, hops + 1):

        def walk(path: tuple[Edge, ...], seen: frozenset[str], length: int = length) -> None:
            t = path[-1][2] if path else src
            if len(path) == length:
                if t == dst:
                    found.append(path)
                return
            if t == dst:
                return
            for e in adjacency(bundle, t):
                if e[2] not in seen:
                    walk((*path, e), seen | {e[2]})

        walk((), frozenset((src,)))
    return found


def label_layers(
    bundle: SnapshotBundle, src: str, amount: int, hops: int
) -> list[list[dict[str, Any]]]:
    """Every token-simple prefix from `src` per depth with its hand amount and exact
    `(token, visited set)` signature -- the state a history label keeps apart."""
    layers: list[list[dict[str, Any]]] = []
    frontier: list[tuple[tuple[Edge, ...], int]] = [((), amount)]
    for _ in range(hops):
        nxt = []
        for path, amt in frontier:
            t = path[-1][2] if path else src
            visited = {src, *(e[2] for e in path)}
            for e in adjacency(bundle, t):
                if e[2] in visited:
                    continue
                pool = bundle.pools[e[0]]
                assert isinstance(pool, ConstantProductPoolState)
                out = hand_out(pool, e[1], amt)
                if out is not None:
                    nxt.append(((*path, e), out))
        layers.append(
            [
                {
                    "token": p[-1][2],
                    "visited": sorted({src, *(e[2] for e in p)}),
                    "pools": [e[0] for e in p],
                    "amount": a,
                }
                for p, a in nxt
            ]
        )
        frontier = nxt
    return layers


def _flows_key(flows: Mapping[str, tuple[str, int]]) -> tuple[Any, ...]:
    return tuple(sorted((p, t, x) for p, (t, x) in flows.items() if x > 0))


def _marginal(
    bundle: SnapshotBundle,
    flows: Mapping[str, tuple[str, int]],
    path: tuple[Edge, ...],
    amount: int,
) -> int | None:
    """Marginal output of `path` for `amount` on the committed aggregate inputs `flows` (each
    edge quoted on the pool's ORIGINAL state at `x + m` minus at `x`)."""
    for pid, tin, _tout in path:
        pool = bundle.pools[pid]
        assert isinstance(pool, ConstantProductPoolState)
        x = flows[pid][1] if pid in flows else 0
        before = hand_out(pool, tin, x) if x else 0
        after = hand_out(pool, tin, x + amount)
        if before is None or after is None:
            return None
        amount = after - before
        if amount <= 0:
            return None
    return amount


def _admissible(edges: set[tuple[str, str]], path: tuple[Edge, ...]) -> bool:
    return _acyclic(edges | {(e[1], e[2]) for e in path})


def chunk_amounts(amount: int, chunks: int) -> list[int]:
    """The published chunk grid (routing-algorithms.md §6): `floor(A*k/K) - floor(A*(k-1)/K)`."""
    return [amount * k // chunks - amount * (k - 1) // chunks for k in range(1, chunks + 1)]


def _commit(
    bundle: SnapshotBundle,
    flows: dict[str, tuple[str, int]],
    edges: set[tuple[str, str]],
    path: tuple[Edge, ...],
    amount: int,
) -> None:
    for pid, tin, tout in path:
        pool = bundle.pools[pid]
        assert isinstance(pool, ConstantProductPoolState)
        x = flows[pid][1] if pid in flows else 0
        out = (hand_out(pool, tin, x + amount) or 0) - ((hand_out(pool, tin, x) or 0) if x else 0)
        flows[pid] = (tin, x + amount)
        edges.add((tin, tout))
        amount = out


def accounted_gross(
    bundle: SnapshotBundle, case: Case, flows: Mapping[str, tuple[str, int]]
) -> int:
    """Sum of every aggregate pool output into the target, on original states."""
    total = 0
    for pid, (tin, x) in flows.items():
        pool = bundle.pools[pid]
        assert isinstance(pool, ConstantProductPoolState)
        tout = pool.token1 if tin == pool.token0 else pool.token0
        if tout == case.token_out and x:
            total += hand_out(pool, tin, x) or 0
    return total


def greedy_chunks(
    bundle: SnapshotBundle,
    case: Case,
    chunks: int,
    hops: int,
    choose: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """The published incremental chunk rule (routing-algorithms.md §6) with this module's own
    paths/admission/quotes: each non-empty chunk (plus carry) takes the first path of maximal
    positive marginal on the committed aggregate state that keeps the plan token graph
    acyclic; per-chunk state (flows, token edges) is recorded after every commit."""
    paths = simple_paths(bundle, case.token_in, case.token_out, hops)
    flows: dict[str, tuple[str, int]] = {}
    edges: set[tuple[str, str]] = set()
    seq, states, carry = [], [], 0
    sizes = chunk_amounts(case.amount_in, chunks)
    last = max(k for k, a in enumerate(sizes) if a > 0)
    for k, size in enumerate(sizes):
        if size == 0:
            continue
        amount = carry + size
        best = None
        for p in paths:
            if not _admissible(edges, p):
                continue
            m = _marginal(bundle, flows, p, amount)
            if m is not None and (best is None or m > best[0]):
                best = (m, p)
        if best is None and k != last:
            carry = amount
            continue
        check(best is not None, f"chunk {k + 1}: no admissible path")
        assert best is not None
        carry = 0
        _commit(bundle, flows, edges, best[1], amount)
        seq.append(
            {
                "chunk": k + 1,
                "amount": amount,
                "pools": [e[0] for e in best[1]],
                "marginal": best[0],
            }
        )
        states.append(
            {
                "flows": {p: {"token_in": t, "amount_in": x} for p, (t, x) in flows.items()},
                "token_edges": sorted(edges),
            }
        )
    return {
        "sequence": seq,
        "states": states,
        "flows": flows,
        "gross": accounted_gross(bundle, case, flows),
    }


def chunk_sequences(bundle: SnapshotBundle, case: Case, chunks: int, hops: int) -> dict[str, Any]:
    """Exhaustive enumeration of every complete chunk sequence (any admissible path with a
    positive marginal per chunk): the best accounted gross and all distinct values."""
    paths = simple_paths(bundle, case.token_in, case.token_out, hops)
    sizes = [a for a in chunk_amounts(case.amount_in, chunks) if a > 0]
    values: dict[tuple[Any, ...], int] = {}
    best: tuple[int, list[list[str]]] = (-1, [])
    count = 0

    def rec(
        i: int, flows: dict[str, tuple[str, int]], edges: set[tuple[str, str]], seq: list[list[str]]
    ) -> None:
        nonlocal best, count
        if i == len(sizes):
            count += 1
            g = accounted_gross(bundle, case, flows)
            values[_flows_key(flows)] = g
            if g > best[0]:
                best = (g, [list(s) for s in seq])
            return
        for p in paths:
            if _admissible(edges, p) and _marginal(bundle, flows, p, sizes[i]) is not None:
                f2, e2 = dict(flows), set(edges)
                _commit(bundle, f2, e2, p, sizes[i])
                rec(i + 1, f2, e2, [*seq, [e[0] for e in p]])

    rec(0, {}, set(), [])
    return {
        "best_gross": best[0],
        "best_sequence": best[1],
        "complete_sequences": count,
        "values": sorted(set(values.values())),
    }


def grid_values(
    pools: Sequence[ConstantProductPoolState],
    token_in: str,
    amount: int,
    step: int,
    max_splits: int,
) -> dict[tuple[tuple[str, int], ...], int | None]:
    """`direct_split`'s repository grid, enumerated independently: N = 100/step units over
    at most `max_splits` strictly increasing admitted pools, every non-final leg
    floor(A*u/N) (a non-final leg flooring to 0 is not a member), the last leg the remainder;
    value = sum of hand outputs, `None` when any leg's quote fails."""
    units = 100 // step
    out: dict[tuple[tuple[str, int], ...], int | None] = {}
    for k in range(1, max_splits + 1):
        for combo in itertools.combinations(range(len(pools)), k):
            for cut in itertools.combinations(range(1, units), k - 1):
                parts = [b - a for a, b in zip((0, *cut), (*cut, units), strict=True)]
                legs = [amount * u // units for u in parts[:-1]]
                if any(x == 0 for x in legs):
                    continue
                legs.append(amount - sum(legs))
                vals = [hand_out(pools[i], token_in, x) for i, x in zip(combo, legs, strict=True)]
                key = tuple((pools[i].pool_id, u) for i, u in zip(combo, parts, strict=True))
                out[key] = None if any(v is None for v in vals) else sum(v or 0 for v in vals)
    return out


def raw_values(
    pools: Sequence[ConstantProductPoolState], token_in: str, amount: int, max_splits: int
) -> dict[tuple[tuple[str, int], ...], int | None]:
    """The raw-integer domain: any positive integer legs summing to the input over at most
    `max_splits` strictly increasing pools."""
    out: dict[tuple[tuple[str, int], ...], int | None] = {}
    for k in range(1, max_splits + 1):
        for combo in itertools.combinations(range(len(pools)), k):
            for cut in itertools.combinations(range(1, amount), k - 1):
                legs = [b - a for a, b in zip((0, *cut), (*cut, amount), strict=True)]
                vals = [hand_out(pools[i], token_in, x) for i, x in zip(combo, legs, strict=True)]
                key = tuple((pools[i].pool_id, x) for i, x in zip(combo, legs, strict=True))
                out[key] = None if any(v is None for v in vals) else sum(v or 0 for v in vals)
    return out


def best_of(values: Mapping[Any, int | None]) -> tuple[int, list[Any]]:
    top = max(v for v in values.values() if v is not None)
    return top, [k for k, v in values.items() if v == top]


# ====================================================================== factory plumbing


def resolved_options(name: str, options: Mapping[str, Any]) -> dict[str, Any]:
    """The options exactly as a profile resolves them: normalized options, `source` (a
    preset pin -- current or historical -- or `override`) and `settings_sha256`."""
    return options_entry(ALGORITHMS[name], dict(options))


def fresh_replay(
    bundle: SnapshotBundle, case: Case, plan: RoutePlan | None
) -> dict[str, Any] | None:
    """A fresh, memo-free `routing.evaluator.evaluate` of an emitted plan (never the solver's
    own evaluation), with its fund ledger."""
    if plan is None:
        return None
    ev: Evaluation = evaluate(bundle, case, plan, gross_only())
    return {
        "status": ev.status.value,
        "gross": ev.gross_output,
        "error": ev.error,
        "residuals": {k: v for k, v in ev.residuals.items()},
        "trace": [
            {
                "pool_id": t.pool_id,
                "token_in": t.token_in,
                "token_out": t.token_out,
                "amount_in": t.amount_in,
                "amount_out": t.amount_out,
            }
            for t in ev.trace
        ],
        "funds": [
            {
                "fund_id": f.fund_id,
                "token": f.token,
                "produced": f.produced,
                "consumed": f.consumed,
                "remaining": f.remaining,
            }
            for f in ev.funds
        ],
    }


def plan_view(plan: RoutePlan | None) -> list[dict[str, Any]] | None:
    if plan is None:
        return None
    return [
        {
            "pool_id": s.pool_id,
            "token_in": s.token_in,
            "token_out": s.token_out,
            "inputs": [
                [i.fund_id, i.amount if i.amount == ALL_REMAINING else int(i.amount)]
                for i in s.inputs
            ],
            "output": s.output_fund_id,
        }
        for s in plan.steps
    ]


def run_factory(
    name: str,
    bundle: SnapshotBundle,
    case: Case,
    params: Mapping[str, Any],
    options: Mapping[str, Any] | None = None,
    budget: Budget | None = None,
    *,
    hand: bool = True,
) -> dict[str, Any]:
    """Prepare + solve through the registered factory, then replay the plan independently
    (fresh evaluator; plus `hand_replay` for all-CPMM plans). Every `ok` plan must replay
    `ok` to the reported score with no residual; a non-`ok` status carries no plan claim."""
    factory = ALGORITHMS[name]
    opts = dict(options or {})
    config = AlgorithmConfig(name, dict(params), opts)
    prepared = factory.prepare(bundle, config) if factory.prepare is not None else None
    published: list[RoutePlan] = []
    result: SolveResult = factory.solve(
        case,
        SolveContext(bundle, gross_only(), prepared, candidate_sink=published.append),
        budget or Budget(),
    )
    replay = fresh_replay(bundle, case, result.plan)
    if result.status is SolveStatus.OK:
        check(replay is not None and replay["status"] == "ok", f"{name}: plan does not replay ok")
        assert replay is not None
        equal(replay["gross"], result.score, f"{name}: fresh replay vs reported score")
        equal(replay["residuals"], {}, f"{name}: residual funds")
    elif result.status is SolveStatus.INVALID_PLAN:  # the fresh replay must reject it too
        check(replay is not None and replay["status"] == "invalid_plan", f"{name}: invalid plan")
    else:
        check(result.plan is None, f"{name}: a non-ok result carries a plan")
    independent = None
    if (
        hand
        and result.status is SolveStatus.OK
        and result.plan is not None
        and all(
            isinstance(bundle.pools[s.pool_id], ConstantProductPoolState) for s in result.plan.steps
        )
    ):
        independent = hand_replay(bundle, case, result.plan)
        equal(independent["gross"], result.score, f"{name}: hand ledger vs reported score")
    return {
        "algorithm": name,
        "options": resolved_options(name, opts) if factory.options_validator else None,
        "params": dict(params),
        "budget": (budget or Budget()).to_dict(),
        "status": result.status.value,
        "score": result.score,
        "error": result.error,
        "plan": plan_view(result.plan),
        "replay": replay,
        "hand_replay": independent,
        "published": len(published),
        "stats": json.loads(json.dumps(dict(result.search_stats), default=str)),
    }


def pools_of(row: Mapping[str, Any]) -> list[str]:
    return [s["pool_id"] for s in row["plan"] or []]


def request(case: Case) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "token_in": case.token_in,
        "token_out": case.token_out,
        "amount_in": case.amount_in,
    }


# ====================================================================== 1. metis_history

MH_PRESET = {"dominance": "history", "max_labels_per_signature": 1, "max_frontier_labels": 1024}
MH_WIDE = {"dominance": "history", "max_labels_per_signature": 10**6, "max_frontier_labels": 10**7}


def _mh_params(label_hops: int, chunks: int, max_hops: int = 2) -> dict[str, int]:
    return {
        "max_hops": max_hops,
        "max_splits": 4,
        "percent_step": 5,
        "chunks": chunks,
        "label_hops": label_hops,
    }


def _chunk_pools(row: Mapping[str, Any]) -> list[list[str]]:
    s = row["stats"]
    labels = [a["path"] for a in s["incremental_allocation"]]
    return [
        [part.split("]")[0] for part in labels[i].split("-[")[1:]]
        for i in s["incremental_chunk_sequence"]
    ]


def _metis_inspired(
    bundle: SnapshotBundle, case: Case, params: Mapping[str, int], pruning: bool
) -> dict[str, Any]:
    return run_factory("metis_inspired", bundle, case, {**params, "label_pruning": pruning})


def example_metis_history() -> dict[str, Any]:
    r1 = RECON["R1_prefix_merge_vs_cycle"]
    b = fixture_bundle("R1_prefix_merge_vs_cycle", r1["pools"], r1["fee_bps"])
    case = Case("r1", r1["token_in"], r1["token_out"], r1["amount_in"])
    params = _mh_params(4, 1)

    # --- competing prefixes to C: two signatures, both retained (independent layers)
    layers = label_layers(b, case.token_in, case.amount_in, 4)
    at_c = [lab for lab in layers[1] if lab["token"] == "C"]
    # three prefixes reach C: two share the signature (C, {A,B,C}) (pools bc1 and cb2 in
    # parallel), the third has another visited set (C, {A,C,D}) and is never compared to them.
    via_cb2 = chain_out(b, "A", case.amount_in, ["ab", "cb2"])
    equal(
        {tuple(lab["pools"]): lab["amount"] for lab in at_c},
        {
            ("ab", "bc1"): r1["layer2_amount_at_C"]["via_B"],
            ("ab", "cb2"): via_cb2,
            ("ad", "dc"): r1["layer2_amount_at_C"]["via_D"],
        },
        "R1 layer-2 labels at C",
    )
    equal(
        [tuple(lab["visited"]) for lab in at_c],
        [("A", "B", "C"), ("A", "B", "C"), ("A", "C", "D")],
        "R1 C signatures",
    )
    oracle = {
        tuple(e[0] for e in p): chain_out(b, "A", case.amount_in, [e[0] for e in p])
        for p in simple_paths(b, "A", "T", 4)
    }
    best = max(v for v in oracle.values() if v is not None)
    equal(best, r1["best_simple_path"]["gross"], "R1 exhaustive best simple path")
    equal(
        [list(k) for k, v in oracle.items() if v == best],
        [r1["best_simple_path"]["path"]],
        "R1 argmax path",
    )
    walk = chain_out(b, "A", case.amount_in, r1["repeated_token_walk"]["path"])
    equal(walk, r1["repeated_token_walk"]["gross_if_allowed"], "R1 repeated-token walk (hand)")
    walk_plan = RoutePlan(
        tuple(
            SwapStep(
                pid,
                ti,
                to,
                (FundInput(REQUEST_FUND_ID if i == 0 else f"H{i}", ALL_REMAINING),),
                "OUT" if i == 3 else f"H{i + 1}",
            )
            for i, (pid, ti, to) in enumerate(
                zip(
                    r1["repeated_token_walk"]["path"],
                    r1["repeated_token_walk"]["tokens"],
                    r1["repeated_token_walk"]["tokens"][1:],
                    strict=False,
                )
            )
        )
    )
    walk_eval = fresh_replay(b, case, walk_plan)
    assert walk_eval is not None
    equal(walk_eval["status"], "invalid_plan", "R1 cyclic walk under the evaluator")

    hist = run_factory("metis_history", b, case, params, MH_PRESET)
    equal(hist["status"], "ok", "R1 metis_history status")
    equal(_chunk_pools(hist), [r1["best_simple_path"]["path"]], "R1 metis_history chunk path")
    equal(hist["score"], r1["best_simple_path"]["gross"], "R1 metis_history gross")
    equal(hist["stats"]["chosen_source"], "metis_history", "R1 chosen source")
    base = _metis_inspired(b, case, params, True)
    equal(base["score"], r1["single_label_result"]["gross"], "R1 metis_inspired (token labels)")

    # --- admission: the dominated prefix of another visited set is the one whose continuation
    # stays admissible against chunk 1's committed token edges (X4b, final two-chunk plan)
    x4b = RECON["R8_X4b"]
    xb = fixture_bundle("R8_X4b", x4b["pools"], x4b["fee_bps"])
    xcase = Case("R8_X4b", "S", "D", x4b["amount_in"])
    x_ref = greedy_chunks(xb, xcase, 2, 4)
    equal([s["pools"] for s in x_ref["sequence"]], x4b["accepted_union"], "X4b per-chunk maxima")
    committed = set(map(tuple, x_ref["states"][0]["token_edges"]))
    rejected = x4b["rejected_union"][1]
    rejected_edges, tok = set(), "S"
    for pid in rejected:
        pool = xb.pools[pid]
        nxt = pool.token1 if tok == pool.token0 else pool.token0
        rejected_edges.add((tok, nxt))
        tok = nxt
    equal(tok, "D", "X4b rejected continuation reaches D")
    check(cycle_of(committed | rejected_edges), "X4b: S-A-V-X-D closes X->A->V->X")
    x_hist = run_factory("metis_history", xb, xcase, _mh_params(4, 2), MH_PRESET)
    x_label = _metis_inspired(xb, xcase, _mh_params(4, 2), True)
    equal(_chunk_pools(x_hist), x4b["accepted_union"], "X4b metis_history chunk paths")
    equal(x_hist["score"], x_ref["gross"], "X4b metis_history = independent trajectory gross")
    check(
        x_label["score"] is not None and x_label["score"] < (x_hist["score"] or 0),
        "X4b metis_inspired loses the admissible continuation",
    )

    # --- unsafe vs safe same-signature deletion
    ov = HISTORY["fixtures"]["overflow"]
    ob = fixture_bundle("overflow", ov["pools"], ov["fee_bps"], ov["source_key"])
    ocase = Case(
        "overflow", ov["case"]["token_in"], ov["case"]["token_out"], ov["case"]["amount_in"]
    )
    big_x = chain_out(ob, "S", ocase.amount_in, ["big"])
    small_x = chain_out(ob, "S", ocase.amount_in, ["small"])
    equal(
        (small_x, big_x), (ov["xd_inputs"]["small"], ov["xd_inputs"]["large"]), "overflow X labels"
    )
    xd = ob.pools["xd"]
    assert isinstance(xd, ConstantProductPoolState)
    x_held = sum(
        p.reserve0 if p.token0 == "X" else p.reserve1
        for p in ob.pools.values()
        if isinstance(p, ConstantProductPoolState) and "X" in (p.token0, p.token1)
    )
    check(x_held >= UINT112, "overflow: X holdings reach uint112 (region not certifiable)")
    equal(hand_out(xd, "X", big_x or 0), None, "overflow: larger X label reverts in xd")
    via_small = chain_out(ob, "S", ocase.amount_in, ["small", "xd"])
    weak = chain_out(ob, "S", ocase.amount_in, ["weak"])
    equal(via_small, ov["expected"]["history_gross"], "overflow: smaller label continues (hand)")
    equal(weak, ov["expected"]["label_gross"], "overflow: direct fallback (hand)")
    oparams = _mh_params(3, 1, max_hops=1)
    unsafe_wide = run_factory("metis_history", ob, ocase, oparams, MH_WIDE)
    equal(unsafe_wide["score"], ov["expected"]["history_gross"], "overflow: history (wide caps)")
    equal(unsafe_wide["stats"]["certified_strict_insertions"], 0, "overflow: no strict pruning")
    unsafe_preset = run_factory("metis_history", ob, ocase, oparams, MH_PRESET)
    # one label per signature keeps the LARGER amount (rule R6): it reverts, only the direct
    # pool remains -> the visible state-capped approximation.
    equal(unsafe_preset["score"], weak, "overflow: preset (1 label) keeps the reverting label")
    equal(unsafe_preset["stats"]["termination"], "state_cap", "overflow preset termination")
    check(unsafe_preset["stats"]["labels_dropped_signature_cap"] >= 1, "overflow preset drop")
    label_only = _metis_inspired(ob, ocase, oparams, True)
    equal(label_only["score"], ov["expected"]["label_gross"], "overflow: metis_inspired")

    safe = run_factory("metis_history", b, case, _mh_params(4, 3), MH_WIDE)
    # every R1 pool is constant product without a source key: an input-independent success
    # domain, so every edge is certified upward safe (history-labels.md §3.4)
    check(all(p.source_key is None for p in b.pools.values()), "R1 pools carry no source key")
    certified = sorted(b.pools)
    check(safe["stats"]["certified_strict_insertions"] > 0, "R1 chunks=3: strict pruning used")
    safe_ref = greedy_chunks(b, case, 3, 4)
    equal(
        safe["stats"]["incremental_evaluated_gross"],
        str(safe_ref["gross"]),
        "R1 chunks=3 gross vs independent per-chunk exhaustive trajectory",
    )
    equal(_chunk_pools(safe), [s["pools"] for s in safe_ref["sequence"]], "R1 chunks=3 paths")

    # --- tie: equal chunk value, different committed state, different final plans
    ts = HISTORY["fixtures"]["tie_state"]
    tb = fixture_bundle("tie_state", ts["pools"])
    tcase = Case(
        "tie_state", ts["case"]["token_in"], ts["case"]["token_out"], ts["case"]["amount_in"]
    )
    half = tcase.amount_in // 2
    p1x, p3x = chain_out(tb, "S", half, ["p1"]), chain_out(tb, "S", half, ["p3"])
    check(p3x is not None and p1x is not None and p3x > p1x, "tie: p3 gives more X")
    equal(
        chain_out(tb, "S", half, ["p1", "q"]),
        chain_out(tb, "S", half, ["p3", "q"]),
        "tie: equal chunk-1 output",
    )
    tparams = _mh_params(2, 2)
    tie = run_factory("metis_history", tb, tcase, tparams, MH_PRESET)
    enum = _metis_inspired(tb, tcase, tparams, False)
    equal(_chunk_pools(tie), ts["expected"]["history_chunk_paths"], "tie: history chunk paths")
    equal(_chunk_pools(enum), ts["expected"]["enumeration_chunk_paths"], "tie: enumeration paths")
    equal(
        tie["stats"]["incremental_evaluated_gross"],
        str(ts["expected"]["history_gross"]),
        "tie: history final gross",
    )
    equal(
        enum["stats"]["incremental_evaluated_gross"],
        str(ts["expected"]["enumeration_gross"]),
        "tie: enumeration final gross",
    )

    # --- per-chunk exact is not whole-plan better (greedy trap)
    gt = HISTORY["fixtures"]["greedy_trap"]
    gb = fixture_bundle("greedy_trap", gt["pools"])
    gcase = Case(
        "greedy_trap", gt["case"]["token_in"], gt["case"]["token_out"], gt["case"]["amount_in"]
    )
    gparams = _mh_params(4, 2)
    g_ref = greedy_chunks(gb, gcase, 2, 4)
    equal(
        [s["pools"] for s in g_ref["sequence"]],
        gt["expected"]["history_chunk_paths"],
        "greedy trap: independent per-chunk maxima",
    )
    equal(g_ref["gross"], gt["expected"]["history_gross"], "greedy trap: independent gross")
    g_hist = run_factory("metis_history", gb, gcase, gparams, MH_PRESET)
    g_label = _metis_inspired(gb, gcase, gparams, True)
    equal(_chunk_pools(g_hist), gt["expected"]["history_chunk_paths"], "greedy trap: history paths")
    equal(
        g_hist["stats"]["incremental_evaluated_gross"],
        str(gt["expected"]["history_gross"]),
        "greedy trap: history incremental gross",
    )
    equal(
        g_label["stats"]["incremental_evaluated_gross"],
        str(gt["expected"]["label_gross"]),
        "greedy trap: metis_inspired incremental gross",
    )

    # --- caps: visible approximation (fallback) and a no-plan cap is timeout, never no_route
    narrow = {**MH_WIDE, "max_frontier_labels": 1}
    capped = run_factory("metis_history", b, case, params, narrow)
    equal(capped["stats"]["termination"], "state_cap", "R1 frontier cap termination")
    equal(
        capped["stats"]["r021"]["fallback"]["reason"], "retained_simpler_candidate", "cap fallback"
    )
    equal(
        capped["score"],
        chain_out(b, "A", case.amount_in, r1["single_label_result"]["path"]),
        "cap fallback = the retained A-B-T path (hand)",
    )
    fb = fixture_bundle(
        "capped",
        {
            "sb": ["S", "B", 10**9, 10**9],
            "sc": ["S", "C", 10**9, 10**9],
            "bd": ["B", "D", 10**9, 0],
            "cd": ["C", "D", 10**9, 10**9],
        },
    )
    fcase = Case("capped", "S", "D", 10**6)
    fparams = _mh_params(2, 1, max_hops=1)
    full_ok = run_factory("metis_history", fb, fcase, fparams, MH_WIDE)
    equal(
        full_ok["score"], chain_out(fb, "S", 10**6, ["sc", "cd"]), "capped: uncapped S-C-D (hand)"
    )
    no_plan = run_factory(
        "metis_history", fb, fcase, fparams, {**MH_WIDE, "max_frontier_labels": 1}
    )
    equal(no_plan["status"], "timeout", "capped: frontier cap without a plan")
    equal(no_plan["stats"]["truncated_by"], "state_cap", "capped: truncated_by")

    return {
        "strategy": "metis_history",
        "competing_prefixes": {
            "fixture": "reconstructions.json R1_prefix_merge_vs_cycle (external report §2.1)",
            "request": request(case),
            "params": params,
            "options": hist["options"],
            "layer2_at_C": at_c,
            "exhaustive_best": {"pools": r1["best_simple_path"]["path"], "gross": best},
            "repeated_token_walk": {
                "pools": r1["repeated_token_walk"]["path"],
                "gross_if_allowed": walk,
                "evaluator": walk_eval["status"],
                "error": walk_eval["error"],
            },
            "metis_history": _summary(hist),
            "metis_inspired": _summary(base),
        },
        "admission_x4b": {
            "fixture": "reconstructions.json R8_X4b",
            "request": request(xcase),
            "independent_trajectory": x_ref["sequence"],
            "committed_after_chunk_1": sorted(committed),
            "rejected_continuation": {"pools": rejected, "token_edges": sorted(rejected_edges)},
            "metis_history": _summary(x_hist),
            "metis_inspired": _summary(x_label),
        },
        "unsafe_deletion": {
            "fixture": "history-labels.json overflow (moe_classic_v1 sourced)",
            "request": request(ocase),
            "x_labels": {"small": small_x, "big": big_x},
            "x_reserves_held": x_held,
            "uint112": UINT112,
            "hand": {"S-small-X-xd-D": via_small, "S-weak-D": weak},
            "wide_caps": _summary(unsafe_wide),
            "preset": _summary(unsafe_preset),
            "metis_inspired": _summary(label_only),
        },
        "safe_deletion": {
            "request": request(case),
            "params": _mh_params(4, 3),
            "independent_trajectory": safe_ref["sequence"],
            "metis_history": _summary(safe),
            "certified_pools": certified,
        },
        "tie_state": {
            "request": request(tcase),
            "hand_chunk1_X": {"p1": p1x, "p3": p3x},
            "metis_history": _summary(tie),
            "enumeration": _summary(enum),
        },
        "greedy_trap": {
            "request": request(gcase),
            "independent": g_ref["sequence"],
            "metis_history": _summary(g_hist),
            "metis_inspired": _summary(g_label),
        },
        "caps": {
            "frontier_cap_R1": _summary(capped),
            "full_fill_uncapped": _summary(full_ok),
            "full_fill_capped": _summary(no_plan),
        },
    }


def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """The inspectable fields of one factory run (status, plan, replay, diagnostics)."""
    s = row["stats"]
    keep = (
        "chosen_source",
        "incremental_status",
        "incremental_evaluated_gross",
        "incremental_chunk_sequence",
        "incremental_allocation",
        "termination",
        "truncated_by",
        "labels_dropped_signature_cap",
        "labels_dropped_frontier_cap",
        "chunks_state_capped",
        "certified_strict_insertions",
        "label_relaxations",
        "quotes_executed",
        "path_split_score",
        "evaluations",
    )
    r021 = s.get("r021") or {}
    return {
        "algorithm": row["algorithm"],
        "status": row["status"],
        "score": row["score"],
        "error": row["error"],
        "options_source": (row["options"] or {}).get("source"),
        "settings_sha256": (row["options"] or {}).get("settings_sha256"),
        "plan": row["plan"],
        "replay_gross": (row["replay"] or {}).get("gross"),
        "hand_gross": (row["hand_replay"] or {}).get("gross"),
        "published": row["published"],
        "diagnostics": {k: s[k] for k in keep if k in s},
        "fallback": r021.get("fallback"),
        "certificate_kind": (r021.get("certificate") or {}).get("bound_kind"),
        "factory_counters": r021.get("work"),
    }


# ====================================================================== 2. direct_split_certified

DSC_PRESET = {"domain": "repository_grid", "max_bound_nodes": 100_000, "max_open_nodes": 100_000}
DSC_RAW = {
    "domain": "raw_integer",
    "max_bound_nodes": 1_000_000,
    "max_open_nodes": 250_000,
    "raw_max_amount_in": 100_000,
}  # (= config/direct_split_certified/raw_stress.yaml)


def _cert_view(row: Mapping[str, Any]) -> dict[str, Any] | None:
    cert = row["stats"]["r021"]["certificate"]
    if cert is None:
        return None
    return {
        k: cert[k]
        for k in (
            "lower_raw",
            "upper_raw",
            "gap_raw",
            "bound_kind",
            "upper_source",
            "estimate",
            "optimality_proven",
            "termination",
        )
    }


def _dsc_trace(
    bundle: SnapshotBundle,
    case: Case,
    params: Mapping[str, int],
    options: Mapping[str, Any],
    budget: Budget | None = None,
) -> dict[str, Any]:
    """The factory's own search (`certify`, which `solve` calls unchanged) with its node trace:
    root bound, first nodes and the open frontier at the stop (inspectable transitions)."""
    from routing.algorithms import direct_split_certified as dsc

    factory = ALGORITHMS["direct_split_certified"]
    assert factory.prepare is not None
    prepared = factory.prepare(bundle, AlgorithmConfig(factory.name, dict(params), dict(options)))
    trace: list[Any] = []
    run = dsc.certify(
        bundle,
        case,
        gross_only(),
        prepared,
        budget or Budget(),
        git_revision=None,
        cohort=dsc.cohort_of(bundle),
        trace=trace,
    )
    solved = factory.solve(case, SolveContext(bundle, gross_only(), prepared), budget or Budget())
    equal(run.result, solved, "certify() is the registered solve")

    def node(n: Any) -> dict[str, Any]:
        return {
            "kind": n.kind,
            "pool_index": n.j,
            "fixed_legs": [list(x) for x in n.legs],
            "units_used": n.used,
            "prefix_output": n.gp,
            "ub": n.ub,
            "interval": [n.lo, n.hi] if n.kind == "interval" else None,
        }

    return {
        "root": node(trace[0]),
        "first_nodes": [node(n) for n in trace[:6]],
        "open_frontier": [node(n) for n in run.frontier],
        "nodes_created": len(trace),
    }


def example_direct_split_certified() -> dict[str, Any]:
    r6 = RECON["R6_grid_order_and_bounds"]
    p1 = cp("p1", "S", "T", *r6["pool1"])
    p2 = cp("p2", "S", "T", *r6["pool2"])
    ref = "fixture:R021-FX-GRID38"
    b12, b21 = bundle_of(ref, p1, p2), bundle_of(ref, p2, p1)
    case = Case("r021-grid38", "S", "T", r6["amount_in"])
    params = {"max_splits": r6["max_splits"], "percent_step": r6["percent_step"]}

    grid12 = grid_values([p1, p2], "S", case.amount_in, r6["percent_step"], r6["max_splits"])
    opt12, arg12 = best_of(grid12)
    equal(opt12, r6["grid_optimum_order_p1_p2"], "R6 grid optimum (p1, p2)")
    opt21, _ = best_of(grid_values([p2, p1], "S", case.amount_in, 5, 2))
    equal(opt21, r6["grid_optimum_order_p2_p1"], "R6 grid optimum (p2, p1)")
    raw = raw_values([p1, p2], "S", case.amount_in, 2)
    opt_raw, arg_raw = best_of(raw)
    equal(opt_raw, r6["raw_integer_optimum"], "R6 raw optimum")
    equal([dict(k)["p1"] for k in arg_raw], r6["raw_integer_argmax_pool1"], "R6 raw argmax")
    singles = max(v for k, v in grid12.items() if len(k) == 1 and v is not None)
    equal(singles, r6["single_pool_incumbent"], "R6 best single pool")

    complete = run_factory("direct_split_certified", b12, case, params, DSC_PRESET)
    equal(complete["score"], opt12, "R6 certified value")
    alloc = tuple((a["pool_id"], a["units"]) for a in complete["stats"]["best_allocation"])
    check(alloc in arg12, f"R6 allocation {alloc} is a grid argmax")
    cert = _cert_view(complete)
    equal((cert or {}).get("lower_raw"), str(opt12), "R6 lower")
    equal(
        ((cert or {})["upper_raw"], (cert or {})["gap_raw"], (cert or {})["termination"]),
        (str(opt12), "0", "complete"),
        "R6 complete certificate",
    )
    trace = _dsc_trace(b12, case, params, DSC_PRESET)
    equal(trace["root"]["ub"], r6["tangent_upper_floor"], "R6 root (tangent) bound")
    ref_direct = run_factory("direct_split", b12, case, params)
    equal(ref_direct["score"], opt12, "Proposition P1: direct_split's value on the same grid")

    reordered = run_factory("direct_split_certified", b21, case, params, DSC_PRESET)
    equal(reordered["score"], opt21, "R6 (p2, p1) certified value")
    raw_row = run_factory("direct_split_certified", b12, case, params, DSC_RAW)
    equal(raw_row["score"], opt_raw, "R6 raw_integer certified value")
    hashes = {
        k: r["stats"]["r021"]["candidate_domain_hash"]
        for k, r in (("grid_p1p2", complete), ("grid_p2p1", reordered), ("raw", raw_row))
    }
    equal(len(set(hashes.values())), 3, "three distinct candidate domains")

    node_cap = run_factory(
        "direct_split_certified", b12, case, params, {**DSC_PRESET, "max_bound_nodes": 1}
    )
    nc = _cert_view(node_cap) or {}
    check(
        int(nc["lower_raw"]) <= opt12 <= int(nc["upper_raw"]), "node cap bounds contain the optimum"
    )
    equal(
        (nc["lower_raw"], nc["upper_raw"], nc["gap_raw"], nc["termination"]),
        ("58", str(r6["tangent_upper_floor"]), "1", "node_cap"),
        "R6 node-cap certificate",
    )
    quote_cut = run_factory(
        "direct_split_certified", b12, case, params, DSC_PRESET, Budget(max_quotes=2)
    )
    qc = _cert_view(quote_cut) or {}
    equal(int(qc["lower_raw"]), singles, "quote cut keeps the best single pool")
    check(int(qc["lower_raw"]) <= opt12 <= int(qc["upper_raw"]), "quote cut bounds contain optimum")
    equal((qc["gap_raw"], qc["termination"]), ("2", "quote_budget"), "R6 quote-budget certificate")
    no_quote = run_factory(
        "direct_split_certified", b12, case, params, DSC_PRESET, Budget(max_quotes=0)
    )
    equal(
        (no_quote["status"], no_quote["stats"]["r021"]["certificate"]),
        ("timeout", None),
        "no incumbent: timeout, no certificate",
    )

    # --- integer plateau (R3, raw domain)
    r3 = RECON["R3_plateau"]
    q1, q2 = cp("p1", "S", "T", *r3["pool1"]), cp("p2", "S", "T", *r3["pool2"])
    pb = bundle_of(ref, q1, q2)
    pcase = Case("r3", "S", "T", r3["amount_in"])

    def g_of(x: int) -> int:
        return (hand_out(q1, "S", x) or 0) + (hand_out(q2, "S", r3["amount_in"] - x) or 0)

    g = {str(x): g_of(x) for x in (0, 1, 2, 3, 15)}
    equal(g, r3["G"], "R3 G values")
    praw = raw_values([q1, q2], "S", r3["amount_in"], 2)
    p_opt, p_arg = best_of(praw)
    equal(p_opt, r3["raw_integer_max"], "R3 raw max")
    plateau = run_factory("direct_split_certified", pb, pcase, params, DSC_RAW)
    equal(plateau["score"], p_opt, "R3 certified raw value")
    palloc = tuple((a["pool_id"], int(a["amount_in"])) for a in plateau["stats"]["best_allocation"])
    check(palloc in p_arg, "R3 allocation is a raw argmax")
    ptrace = _dsc_trace(pb, pcase, params, DSC_RAW)
    check(ptrace["root"]["ub"] > g["0"], "R3 root bound lies above the plateau value 70")

    # --- dust grid, real single pool, no direct pool and unsupported mixed direct pools
    graph = load_bundle(FIXTURES / "routing" / "cpmm_graph")
    dust_case = next(c for c in graph.cases if c.case_id == "a_b_dust")
    direct = [
        p
        for p in graph.pools_for_pair(dust_case.token_in, dust_case.token_out)
        if isinstance(p, ConstantProductPoolState)
    ]
    dust_grid = grid_values(direct, dust_case.token_in, dust_case.amount_in, 5, 4)
    dust_opt, _ = best_of(dust_grid)
    ex = {e["id"]: e for e in INTEGER["examples"]}
    equal(str(dust_opt), ex["P-IA-DUST"]["oracle_optimum"], "dust grid optimum")
    dust = run_factory(
        "direct_split_certified", graph, dust_case, {"max_splits": 4, "percent_step": 5}, DSC_PRESET
    )
    equal(dust["score"], dust_opt, "dust certified value")
    moe = load_bundle(FIXTURES / "moe_classic" / "bundle")
    moe_case = next(c for c in moe.cases if c.case_id == "wmnt_usdt_large")
    (moe_pool,) = moe.pools_for_pair(moe_case.token_in, moe_case.token_out)
    assert isinstance(moe_pool, ConstantProductPoolState)
    moe_hand = hand_out(moe_pool, moe_case.token_in, moe_case.amount_in)
    equal(str(moe_hand), ex["P-IA-REAL-MOE"]["oracle_optimum"], "real Moe single pool (hand)")
    moe_row = run_factory(
        "direct_split_certified", moe, moe_case, {"max_splits": 4, "percent_step": 5}, DSC_PRESET
    )
    equal(moe_row["score"], moe_hand, "real Moe certified value")
    mixed = load_bundle(FIXTURES / "routing" / "mantle_mixed")
    mcase = next(c for c in mixed.cases if c.case_id == "usdc_usdt_small")
    kinds = sorted(type(p).__name__ for p in mixed.pools_for_pair(mcase.token_in, mcase.token_out))
    check(any(k != "ConstantProductPoolState" for k in kinds), "mixed pair holds a non-CPMM pool")
    unsupported = run_factory(
        "direct_split_certified", mixed, mcase, {"max_splits": 4, "percent_step": 5}, DSC_PRESET
    )
    equal(
        (unsupported["status"], unsupported["stats"]["r021"]["scope"]["reason"]),
        ("unsupported", "non_constant_product_direct_pool"),
        "mixed direct pools",
    )

    def full(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **_summary(row),
            "certificate": _cert_view(row),
            "best_allocation": row["stats"].get("best_allocation"),
            "scope": row["stats"]["r021"].get("scope"),
            "candidate_domain_hash": row["stats"]["r021"]["candidate_domain_hash"],
        }

    return {
        "strategy": "direct_split_certified",
        "grid38": {
            "fixture": "reconstructions.json R6 (pools of external report §2.3)",
            "request": request(case),
            "params": params,
            "independent": {
                "grid_optimum_p1p2": opt12,
                "grid_argmax_p1p2": [list(a) for a in arg12],
                "grid_optimum_p2p1": opt21,
                "raw_optimum": opt_raw,
                "raw_argmax": [list(a) for a in arg_raw],
                "best_single": singles,
                "tangent_upper_floor": r6["tangent_upper_floor"],
                "continuous_optimum_bounds": r6["continuous_optimum_bounds"],
            },
            "complete": full(complete),
            "search_trace": trace,
            "direct_split_same_grid": _summary(ref_direct),
            "pool_order_p2p1": full(reordered),
            "raw_integer": full(raw_row),
            "node_cap_1": full(node_cap),
            "quote_budget_2": full(quote_cut),
            "quote_budget_0": full(no_quote),
        },
        "plateau_r3": {
            "request": request(pcase),
            "G": g,
            "raw_argmax": [list(a) for a in p_arg],
            "raw_integer": full(plateau),
            "root": ptrace["root"],
        },
        "dust": {"request": request(dust_case), "independent_optimum": dust_opt, "row": full(dust)},
        "real_moe_single_pool": {
            "request": request(moe_case),
            "bundle_hash": moe.bundle_hash,
            "hand": moe_hand,
            "row": full(moe_row),
        },
        "unsupported_mixed": {
            "request": request(mcase),
            "direct_pool_kinds": kinds,
            "row": full(unsupported),
        },
    }


# ====================================================================== 3. incremental_graph_repair

IGR_PRESET = {
    "repair": True,
    "max_checkpoints": 4,
    "alternatives_per_checkpoint": 2,
    "max_repair_attempts": 8,
}
IGR_OFF = {**IGR_PRESET, "repair": False}


def _repair_view(row: Mapping[str, Any]) -> dict[str, Any]:
    r = row["stats"].get("repair") or {}
    keep = (
        "stop",
        "checkpoint_restores",
        "repair_attempts",
        "accepted",
        "duplicates",
        "rejected_worse",
        "ties",
        "consistency_failures",
        "repair_evaluations",
    )
    return {
        **{k: r.get(k) for k in keep},
        "attempts": [
            {k: a.get(k) for k in ("checkpoint", "alternative", "outcome", "score")}
            for a in r.get("attempts", [])
        ],
        "accepted_log": r.get("accepted_log"),
    }


def example_incremental_graph_repair() -> dict[str, Any]:
    fx = SUFFIX["fixtures"]["structural_trap"]
    b = fixture_bundle("structural_trap", fx["pools"])
    case = Case(
        "structural_trap", fx["case"]["token_in"], fx["case"]["token_out"], fx["case"]["amount_in"]
    )
    params = fx["settings"]
    oracle = fx["oracle"]

    greedy = greedy_chunks(b, case, params["chunks"], params["max_hops"])
    equal(
        [s["pools"] for s in greedy["sequence"]],
        oracle["incumbent_sequence"],
        "trap: greedy chunks",
    )
    equal(greedy["gross"], oracle["incumbent_gross"], "trap: greedy incumbent gross")
    first_edges = greedy["states"][0]["token_edges"]
    check(tuple(oracle["blocking_edge"]) in {tuple(e) for e in first_edges}, "chunk 1 commits B->E")
    exhaustive = chunk_sequences(b, case, params["chunks"], params["max_hops"])
    equal(exhaustive["best_gross"], oracle["best_gross"], "trap: exhaustive best")
    equal(exhaustive["complete_sequences"], oracle["complete_sequences"], "trap: sequences counted")
    equal([exhaustive["best_sequence"]], oracle["best_sequences"], "trap: best sequence")
    # non-additivity: the aggregate pool quote is not the sum of per-chunk quotes, so a restore
    # must rebuild the aggregate from the original state (never subtract a chunk's output)
    ed = b.pools["ed"]
    assert isinstance(ed, ConstantProductPoolState)
    ed_in = greedy["flows"]["ed"][1]
    ed_chunks = [s["amount"] for s in greedy["sequence"] if "ed" in s["pools"]]
    split = [
        chain_out(b, "S", a, s["pools"][:-1])
        for a, s in zip(
            ed_chunks, [s for s in greedy["sequence"] if "ed" in s["pools"]], strict=True
        )
    ]
    equal(ed_in, sum(x or 0 for x in split), "pool ed aggregate input = its chunks' E inputs")
    separately = sum(hand_out(ed, "E", x or 0) or 0 for x in split)
    together = hand_out(ed, "E", ed_in)

    off = run_factory("incremental_graph_repair", b, case, params, IGR_OFF)
    reference = run_factory("incremental_graph", b, case, params)
    equal(off["score"], oracle["incumbent_gross"], "repair off = incumbent")
    equal(off["plan"], reference["plan"], "repair off = incremental_graph plan")
    on = run_factory("incremental_graph_repair", b, case, params, IGR_PRESET)
    equal(on["score"], oracle["best_gross"], "repair on reaches the exhaustive best")
    equal(on["stats"]["chosen_source"], "incremental_graph_repair", "accepted repair source")
    rv = _repair_view(on)
    equal(
        [a["outcome"] for a in rv["attempts"]], fx["specification"]["outcomes"], "attempt outcomes"
    )
    for a in rv["accepted_log"] or []:
        check(
            int(a["score"]) in exhaustive["values"], f"accepted {a['score']} is a feasible sequence"
        )
    equal(
        [int(a["score"]) for a in rv["accepted_log"] or []],
        fx["specification"]["accepted_scores"],
        "accepted scores",
    )

    # --- rejected: twin pools (duplicate / worse), the retained direct_split stands
    tw = SUFFIX["fixtures"]["twin_pools"]
    tb = fixture_bundle("twin_pools", tw["pools"])
    tcase = Case("twin", tw["case"]["token_in"], tw["case"]["token_out"], tw["case"]["amount_in"])
    tp = tw["settings"]
    twin_grid, _ = best_of(
        grid_values(
            [p for p in tb.pools.values() if isinstance(p, ConstantProductPoolState)],
            "S",
            tcase.amount_in,
            tp["percent_step"],
            tp["max_splits"],
        )
    )
    twin_on = run_factory("incremental_graph_repair", tb, tcase, tp, IGR_PRESET)
    twin_off = run_factory("incremental_graph_repair", tb, tcase, tp, IGR_OFF)
    equal(twin_on["score"], twin_grid, "twin pools: the direct_split grid optimum stands")
    equal(
        (twin_on["plan"], twin_on["score"]), (twin_off["plan"], twin_off["score"]), "twin on = off"
    )
    equal(
        [a["outcome"] for a in _repair_view(twin_on)["attempts"]],
        tw["specification"]["outcomes"],
        "twin outcomes",
    )
    equal(twin_on["stats"]["chosen_source"], tw["specification"]["chosen_source"], "twin source")

    # --- no budget: one attempt allowed; and a quote budget that cuts the incumbent itself
    one = run_factory(
        "incremental_graph_repair", b, case, params, {**IGR_PRESET, "max_repair_attempts": 1}
    )
    equal(
        (_repair_view(one)["stop"], _repair_view(one)["repair_attempts"]),
        ("attempt_cap", 1),
        "attempt cap",
    )
    equal(
        one["score"],
        oracle["incumbent_gross"],
        "attempt cap 1: the first attempt is rejected_worse",
    )
    cut_quotes = off["stats"]["quotes_executed"] - 1
    cut = run_factory(
        "incremental_graph_repair", b, case, params, IGR_PRESET, Budget(max_quotes=cut_quotes)
    )
    equal(
        (_repair_view(cut)["stop"], _repair_view(cut)["repair_attempts"]),
        ("quote_budget", 0),
        "quote budget before any repair",
    )
    equal(
        cut["score"],
        fx["repository"]["path_split_score"],
        "cut keeps the published path_split plan",
    )

    # --- the WHI-1549 greedy trap is repaired to the metis_inspired label plan
    gt = HISTORY["fixtures"]["greedy_trap"]
    gb = fixture_bundle("greedy_trap", gt["pools"])
    gcase = Case(
        "greedy_trap", gt["case"]["token_in"], gt["case"]["token_out"], gt["case"]["amount_in"]
    )
    gp = SUFFIX["cross_references"]["history_labels_greedy_trap"]["settings"]
    g_on = run_factory("incremental_graph_repair", gb, gcase, gp, IGR_PRESET)
    equal(g_on["score"], gt["expected"]["label_gross"], "greedy trap repaired to the label plan")

    def full(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **_summary(row),
            "repair": _repair_view(row),
            "evaluations": row["stats"].get("evaluations"),
            "incremental_score": row["stats"].get("incremental_score"),
        }

    return {
        "strategy": "incremental_graph_repair",
        "structural_trap": {
            "fixture": "suffix-repair.json structural_trap (independent oracle values)",
            "request": request(case),
            "params": params,
            "independent_greedy": greedy["sequence"],
            "independent_states": greedy["states"],
            "blocking_edge": oracle["blocking_edge"],
            "fix_edge": oracle["fix_edge"],
            "exhaustive": {
                k: exhaustive[k] for k in ("best_gross", "best_sequence", "complete_sequences")
            },
            "non_additive_pool_ed": {
                "aggregate_input": ed_in,
                "aggregate_output": together,
                "sum_of_separate_chunk_quotes": separately,
            },
            "repair_off": full(off),
            "incremental_graph": _summary(reference),
            "repair_on": full(on),
        },
        "twin_pools_rejected": {
            "request": request(tcase),
            "grid_optimum": twin_grid,
            "repair_on": full(twin_on),
            "repair_off": full(twin_off),
        },
        "attempt_cap_1": full(one),
        "quote_budget_cut": {"max_quotes": cut_quotes, "row": full(cut)},
        "greedy_trap_cross_reference": {
            "request": request(gcase),
            "row": full(g_on),
            "label_gross": gt["expected"]["label_gross"],
        },
    }


# ====================================================================== 4. uni_sor_cycle_safe


def _adapter(name: str) -> tuple[SnapshotBundle, Case, dict[str, Any], dict[str, Any]]:
    fx = CYCLE["adapter"][name]
    big = (1 << 112) - 1
    pools = {
        pid: [s[0], s[1], *(big - int(v[2:]) if isinstance(v, str) else v for v in s[2:4])]
        for pid, s in fx["pools"].items()
    }  # "M-k" = 2**112 - 1 - k
    b = fixture_bundle(name, pools, fx["fee_bps"], fx.get("source_key"))
    c = fx["case"]
    return b, Case(c["case_id"], c["token_in"], c["token_out"], c["amount_in"]), fx["params"], fx


def _cs_view(row: Mapping[str, Any]) -> dict[str, Any]:
    cs = row["stats"].get("cycle_safe") or {}
    return {
        **_summary(row),
        "cycle_safe": cs,
        "admission": {
            k: ((row["stats"].get("r021") or {}).get("work") or {}).get(k)
            for k in ("admission_checks", "combinations_rejected_cycle")
        },
    }


def example_uni_sor_cycle_safe() -> dict[str, Any]:
    b, case, params, fx = _adapter("A1_union_cycle")
    half = case.amount_in // 2
    legs = {"abc": ["a", "b", "c"], "def": ["d", "e", "f"]}
    hand = {
        k: [chain_out(b, "s", half, p), chain_out(b, "s", case.amount_in, p)]
        for k, p in legs.items()
    }
    equal(hand["abc"], fx["hand_quotes"]["abc"], "A1 abc hand quotes")
    equal(hand["def"], fx["hand_quotes"]["def"], "A1 def hand quotes")
    union = {("s", "x"), ("x", "y"), ("y", "t"), ("s", "y"), ("y", "x"), ("x", "t")}
    check(cycle_of(union), "A1: the two routes' union has the cycle x->y->x")
    check(
        not cycle_of({("s", "x"), ("x", "y"), ("y", "t")})
        and not cycle_of({("s", "y"), ("y", "x"), ("x", "t")}),
        "A1: each route alone is acyclic",
    )
    check(not set(legs["abc"]) & set(legs["def"]), "A1: the routes are pool-disjoint")
    port = run_factory("uni_sor_port", b, case, params, hand=False)
    equal(port["status"], fx["expected_port"]["status"], "A1 uni_sor_port status")
    equal(port["error"] and fx["expected_port"]["error"] in port["error"], True, "A1 port error")
    safe = run_factory("uni_sor_cycle_safe", b, case, params, {})
    equal((safe["status"], safe["score"]), ("ok", fx["expected_variant"]["gross"]), "A1 cycle_safe")
    equal(pools_of(safe), fx["expected_variant"]["routes"][0][0], "A1 cycle_safe route")
    w = safe["stats"]["r021"]["work"]
    equal(
        (w["admission_checks"], w["combinations_rejected_cycle"]),
        (
            fx["expected_variant"]["admission_checks"],
            fx["expected_variant"]["combinations_rejected_cycle"],
        ),
        "A1 admission counters (hand-traced)",
    )
    equal(safe["stats"]["cycle_safe"]["reference_trajectory"], "diverged", "A1 trajectory")

    b3, c3, p3, fx3 = _adapter("A3_no_admissible_overflow")
    port3 = run_factory("uni_sor_port", b3, c3, p3, hand=False)
    safe3 = run_factory("uni_sor_cycle_safe", b3, c3, p3, {})
    equal(port3["status"], fx3["expected_port"]["status"], "A3 port")
    equal(safe3["status"], fx3["expected_variant"]["status"], "A3 cycle_safe")
    equal(
        safe3["stats"]["cycle_safe"]["no_admissible_selection"], True, "A3 no admissible selection"
    )

    teach = bundle_of("synthetic_teaching_bundle", *_teaching_pools())
    tcase = Case("ex_sor", "TKA", "TKB", 10_000)
    tparams = {"max_hops": 2, "max_splits": 2, "percent_step": 10}
    tport = run_factory("uni_sor_port", teach, tcase, tparams)
    tsafe = run_factory("uni_sor_cycle_safe", teach, tcase, tparams, {})
    equal(tsafe["score"], 12581, "teaching graph: the guide's uni_sor_port value")
    equal(tsafe["plan"], tport["plan"], "teaching graph: identical plan")
    equal(tsafe["stats"]["cycle_safe"]["reference_trajectory"], "identical", "teaching trajectory")
    return {
        "strategy": "uni_sor_cycle_safe",
        "union_cycle_A1": {
            "fixture": "cycle-safe-sor.json adapter A1_union_cycle",
            "request": request(case),
            "params": params,
            "hand_quotes": hand,
            "union_edges": sorted(union),
            "oracle_step50": fx["expected_oracle_step50"],
            "uni_sor_port": _cs_view(port),
            "uni_sor_cycle_safe": _cs_view(safe),
        },
        "no_admissible_A3": {
            "request": request(c3),
            "uni_sor_port": _cs_view(port3),
            "uni_sor_cycle_safe": _cs_view(safe3),
        },
        "teaching_graph": {
            "request": request(tcase),
            "params": tparams,
            "uni_sor_port": _summary(tport),
            "uni_sor_cycle_safe": _cs_view(tsafe),
        },
    }


def _teaching_pools() -> list[ConstantProductPoolState]:
    """The guide's shared teaching graph (run_examples.make_teaching_bundle), unchanged."""

    def tp(pid: str, t0: str, t1: str, r0: int, r1: int) -> ConstantProductPoolState:
        return cp(
            pid,
            min(t0, t1),
            max(t0, t1),
            r0 if t0 < t1 else r1,
            r1 if t0 < t1 else r0,
            30,
            "moe_classic_v1",
        )

    return [
        tp("P_AB1", "TKA", "TKB", 100_000, 100_000),
        tp("P_AB2", "TKA", "TKB", 200_000, 180_000),
        tp("P_AC", "TKA", "TKC", 100_000, 200_000),
        tp("P_CB", "TKC", "TKB", 200_000, 150_000),
        tp("P_AD", "TKA", "TKD", 100_000, 150_000),
        tp("P_DB", "TKD", "TKB", 150_000, 120_000),
        tp("P_CD", "TKC", "TKD", 100_000, 100_000),
    ]


# ====================================================================== 5. cfmm_dual

CFMM = "cfmm_dual"
CPMM_ORACLE_TOL = 1e-12  # closed-form CPMM oracle vs the pinned author run (float64)
CL_ORACLE_TOL = 5e-14  # CL oracle vs the 13 pinned author UniV3 probes (cfmm-dual.md §8.3)
PRICE_TOL = 1e-8  # the factory's final dual prices vs the author's (numerical, not money)


def cfmm_presets() -> dict[str, dict[str, Any]]:
    """`cfmm_dual/2` (current, CL stage) and `cfmm_dual/1` (historical CPMM-stage pin), read
    through the pinned preset files."""
    factory = ALGORITHMS[CFMM]
    return {
        "v2": preset_options(factory),
        "v1": preset_options(factory, factory.historical_presets[0]),
    }


def author_network(cid: str) -> tuple[SnapshotBundle, Case, dict[str, Any]]:
    doc = next(x for x in AUTHOR_IN["router"] if x["id"] == cid)
    pools = [
        cp(p["pool_id"], p["token0"], p["token1"], p["reserve0"], p["reserve1"], p["fee_bps"])
        for p in doc["pools"]
    ]
    case = Case(cid, doc["token_in"], doc["token_out"], doc["amount_in"])
    return bundle_of(cid, *pools, cases=(case,)), case, doc


def arb(pool: ConstantProductPoolState, nu: Mapping[str, float]) -> tuple[str, float, float] | None:
    """This module's closed-form optimal CPMM arbitrage at prices `nu` (paper eq. (9), fee on
    the input): trade a->b iff r^2 = gamma*nu_b*R_b/(nu_a*R_a) > 1, tendering
    R_a*(r-1)/gamma and receiving R_b*(1-1/r)."""
    gamma = (FEE_DEN - pool.fee_bps) / FEE_DEN
    for a, b, ra, rb in (
        (pool.token0, pool.token1, pool.reserve0, pool.reserve1),
        (pool.token1, pool.token0, pool.reserve1, pool.reserve0),
    ):
        ratio = gamma * nu[b] * rb / (nu[a] * ra)
        if ratio > 1.0:
            r = ratio**0.5
            return a, ra * (r - 1.0) / gamma, rb * (1.0 - 1.0 / r)
    return None


def imbalance(bundle: SnapshotBundle, case: Case, nu: Mapping[str, float]) -> dict[str, Any]:
    """All market trades at prices `nu` and each token's net flow: the dual gradient
    components (tender of the source vs its request, zero for intermediates at an optimum)."""
    trades, net = [], {t: 0.0 for p in bundle.pools.values() for t in (p.token0, p.token1)}
    for pool in bundle.pools.values():
        assert isinstance(pool, ConstantProductPoolState)
        t = arb(pool, nu)
        if t is None:
            continue
        tin, dx, dy = t
        tout = pool.token1 if tin == pool.token0 else pool.token0
        trades.append(
            {
                "pool_id": pool.pool_id,
                "token_in": tin,
                "token_out": tout,
                "amount_in": dx,
                "amount_out": dy,
            }
        )
        net[tin] -= dx
        net[tout] += dy
    return {
        "prices": dict(nu),
        "trades": trades,
        "net_flow": net,
        "source_excess": net[case.token_in] + case.amount_in,
    }


def _trade3(t: Any) -> tuple[str, float, float] | None:
    return None if t is None else (t.token_in, t.amount_in, t.amount_out)


def rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(a), abs(b), 1e-300)


def project(
    bundle: SnapshotBundle, case: Case, trades: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """`cfmm_share_projection/1` written from cfmm-dual.md §6 step 4 alone: tokens in
    topological order; a token's exact integer inflow split over its out-markets in admitted
    order, every leg but the last floor(I*x_p/sum x) (exact rational shares of the float
    inputs), the last the remainder; each leg quoted with `hand_out` on the original state."""
    order = list(bundle.pools)
    edges = {(t["token_in"], t["token_out"]) for t in trades}
    check(not cycle_of(edges), "projection needs an acyclic support")
    tokens: list[str] = []
    seen: set[str] = set()
    while len(seen) < len({x for e in edges for x in e}):
        tok = next(
            t
            for t in sorted({x for e in edges for x in e} - seen)
            if all(a in seen for a, b in edges if b == t)
        )
        tokens.append(tok)
        seen.add(tok)
    inflow: dict[str, int] = {case.token_in: case.amount_in}
    flows = []
    for tok in tokens:
        outs = sorted(
            (t for t in trades if t["token_in"] == tok), key=lambda t: order.index(t["pool_id"])
        )
        amount = inflow.get(tok, 0)
        if not outs or amount == 0:
            continue
        w = [Fraction(t["amount_in"]) for t in outs]
        legs = [amount * x // sum(w) for x in w[:-1]]
        legs.append(amount - sum(legs))
        for t, leg in zip(outs, legs, strict=True):
            if leg == 0:
                continue
            pool = bundle.pools[t["pool_id"]]
            assert isinstance(pool, ConstantProductPoolState)
            out = hand_out(pool, tok, int(leg))
            check(out is not None, f"projection leg {t['pool_id']} fails")
            flows.append(
                {"pool_id": t["pool_id"], "token_in": tok, "amount_in": int(leg), "amount_out": out}
            )
            inflow[t["token_out"]] = inflow.get(t["token_out"], 0) + (out or 0)
    return {"flows": flows, "gross": inflow.get(case.token_out, 0)}


def _author_trades(cid: str, bundle: SnapshotBundle) -> list[dict[str, Any]]:
    ref = next(x for x in AUTHOR["router"] if x["id"] == cid)
    out = []
    for t in ref["trades"]:
        pool = bundle.pools[t["pool_id"]]
        d0, d1 = t["delta"]
        l0, l1 = t["lambda"]
        if d0 == d1 == 0:
            continue
        tin, tout, dx, dy = (
            (pool.token0, pool.token1, d0, l1) if d0 else (pool.token1, pool.token0, d1, l0)
        )
        out.append(
            {
                "pool_id": t["pool_id"],
                "token_in": tin,
                "token_out": tout,
                "amount_in": dx,
                "amount_out": dy,
            }
        )
    return out


def _flows(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    rec = row["stats"]["cfmm"]["recovery"] or {}
    return [
        {
            "pool_id": f["pool_id"],
            "token_in": f["token_in"],
            "amount_in": int(f["amount_in"]),
            "amount_out": int(f["amount_out"]),
        }
        for f in rec.get("flows", [])
    ]


def _cfmm_view(row: Mapping[str, Any]) -> dict[str, Any]:
    c = row["stats"]["cfmm"]
    ini = c.get("initial") or {}
    return {
        **_summary(row),
        "stage": c["stage"],
        "markets": c["markets"],
        "termination": c["termination"],
        "recovery_failure": c["recovery_failure"],
        "resolve": c.get("resolve"),
        "estimate": c.get("estimate"),
        "estimate_withheld": c.get("estimate_withheld"),
        "initial": {
            k: ini.get(k)
            for k in (
                "termination",
                "failure",
                "nu",
                "residual",
                "residual_tolerance",
                "value",
                "active_bounds",
                "evaluations",
                "oracle_calls",
                "iterations",
            )
        },
        "restricted": [
            {
                k: r.get(k)
                for k in (
                    "markets",
                    "directions",
                    "termination",
                    "value",
                    "evaluations",
                    "iterations",
                    "warm_started",
                )
            }
            for r in c.get("restricted") or []
        ],
        "recovery": {
            k: (c.get("recovery") or {}).get(k)
            for k in ("initial_support", "cycle_removed", "support", "attempts", "pruned", "gross")
        },
        "flows": _flows(row),
        "fallback_detail": c.get("fallback"),
        "certificate": (row["stats"]["r021"].get("certificate") or None),
        "cl_markets": c.get("cl_markets"),
    }


def _brute_triangle(bundle: SnapshotBundle) -> int:
    st, sm, mt, mt2 = (bundle.pools[p] for p in ("st", "sm", "mt", "mt2"))
    best = 0
    for x in range(151):
        direct = (hand_out(st, "S", x) or 0) if x else 0  # type: ignore[arg-type]
        mid = (hand_out(sm, "S", 150 - x) or 0) if 150 - x else 0  # type: ignore[arg-type]
        for y in range(mid + 1):
            a = (hand_out(mt, "M", y) or 0) if y else 0  # type: ignore[arg-type]
            z = (hand_out(mt2, "M", mid - y) or 0) if mid - y else 0  # type: ignore[arg-type]
            best = max(best, direct + a + z)
    return best


def synthetic_cl(missing_tick_data: bool = False) -> ConcentratedPoolState:
    """The WHI-1557 two-position CL fixture the pinned author UniV3 inputs were produced from:
    [-1800, -1200) L 4e15, empty [-1200, -600), [-600, 1200) L 1e16; current tick 100,
    spacing 60, collected bitmap words (-1, 0). Its identity with the author inputs is
    re-checked below (`author_cl_mapping`)."""
    l1, l2 = 4 * 10**15, 10**16
    ticks = {
        -1800: TickInfo(l1, l1, 0, 0),
        -1200: TickInfo(l1, -l1, 0, 0),
        -600: TickInfo(l2, l2, 0, 0),
        1200: TickInfo(l2, -l2, 0, 0),
    }
    if missing_tick_data:
        del ticks[-1800]
    return ConcentratedPoolState(
        pool_id=f"cl_synth_uniswap_v3{'_missing' if missing_tick_data else ''}",
        source_key="uniswap_v3",
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
        tick_bitmap={-1: (1 << 226) | (1 << 236) | (1 << 246), 0: 1 << 20},
        ticks=ticks,
        lm_pool=None,
    )


def _exact_landing(state: ConcentratedPoolState, token: str) -> int:
    """Smallest exact input whose EXACT quote is not ok (bisection on the exact quote only)."""
    lo, hi = 1, 1
    while quote_exact_in(state, token, hi).status is QuoteStatus.OK:
        hi *= 2
    while lo < hi:
        mid = (lo + hi) // 2
        if quote_exact_in(state, token, mid).status is QuoteStatus.OK:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _exact_chain(
    bundle: SnapshotBundle, token: str, amount: int, path: Sequence[Edge]
) -> int | None:
    for pid, tin, _ in path:
        res = quote_exact_in(bundle.pools[pid], tin, amount)
        if res.status is not QuoteStatus.OK or res.amount_out <= 0:
            return None
        amount = res.amount_out
    return amount


def example_cfmm_dual() -> dict[str, Any]:
    presets = cfmm_presets()
    out: dict[str, Any] = {
        "strategy": CFMM,
        "presets": {k: resolved_options(CFMM, v) for k, v in presets.items()},
    }

    # --- A. small multi-hop CPMM network: prices, oracles, imbalance, iterations, recovery
    b, case, doc = author_network("r-triangle")
    author = next(x for x in AUTHOR["router"] if x["id"] == "r-triangle")
    v_t = author["v"][-1]
    v = {tok: x / v_t for tok, x in zip(doc["tokens"], author["v"], strict=True)}
    at_author = imbalance(b, case, v)
    for mine in at_author["trades"]:
        theirs = next(t for t in _author_trades("r-triangle", b) if t["pool_id"] == mine["pool_id"])
        check(
            rel(mine["amount_in"], theirs["amount_in"]) < CPMM_ORACLE_TOL
            and rel(mine["amount_out"], theirs["amount_out"]) < CPMM_ORACLE_TOL,
            f"author trade {mine['pool_id']}",
        )
    check(abs(at_author["net_flow"]["M"]) < 1e-6, "M balances at the author prices")
    check(
        abs(at_author["source_excess"]) < 1e-6,
        "the source tenders its request at the author prices",
    )
    oracle_probes = []
    for probe in AUTHOR_IN["cpmm_oracle"]:  # pool-level oracle: 7 pinned author probes
        pool = cp("p", "A", "B", probe["R"][0], probe["R"][1], probe["fee_bps"])
        nu = {"A": probe["v"][0], "B": probe["v"][1]}
        ref = next(x for x in AUTHOR["cpmm_oracle"] if x["id"] == probe["id"])
        want = ref["delta"] + ref["lambda"]
        for label, got in (
            ("hand", arb(pool, nu)),
            ("factory_model", _trade3(cfmm_model.cpmm_arb(pool, nu))),
        ):
            vec = [0.0, 0.0, 0.0, 0.0]
            if got is not None:
                i = 0 if got[0] == "A" else 1
                vec[i], vec[3 - i] = got[1], got[2]
            worst = max(0.0 if g == w == 0 else rel(g, w) for g, w in zip(vec, want, strict=True))
            check(worst < CPMM_ORACLE_TOL, f"{probe['id']} {label} CPMM oracle vs author")
        oracle_probes.append(
            {
                "id": probe["id"],
                "reserves": probe["R"],
                "fee_bps": probe["fee_bps"],
                "prices": probe["v"],
                "delta": ref["delta"],
                "lambda": ref["lambda"],
            }
        )
    model = next(x for x in MODEL["cases"] if x["id"] == "r-triangle")
    spot = model["solution"]["sigma"]
    at_spot = imbalance(b, case, spot)
    check(abs(at_spot["net_flow"]["M"]) > 1.0, "spot prices leave M unbalanced")

    rows = {k: run_factory(CFMM, b, case, {"max_hops": 3}, presets[k]) for k in ("v1", "v2")}
    for k, row in rows.items():
        ini = row["stats"]["cfmm"]["initial"]
        equal(ini["termination"], "converged", f"r-triangle {k} termination")
        check(
            ini["residual"] < ini["residual_tolerance"], f"r-triangle {k} residual below tolerance"
        )
        for tok, x in ini["nu"].items():
            check(rel(x, v[tok]) < PRICE_TOL, f"r-triangle {k} price {tok} vs author")
        check(
            rel(ini["value"], author["dual_value"]) < 1e-6, f"r-triangle {k} dual value vs author"
        )
        work = row["stats"]["r021"]["work"]
        equal(
            (
                work["objective_evaluations"],
                work["market_oracle_calls"],
                work["optimizer_iterations"],
            ),
            (
                model["budget"]["evaluations_used"],
                model["budget"]["oracle_calls"],
                presets[k]["max_iterations"] - model["budget"]["iterations_left"],
            ),
            f"r-triangle {k} numeric work = pinned contract model",
        )
    projected = project(b, case, _author_trades("r-triangle", b))
    for k, row in rows.items():
        equal(
            _flows(row), projected["flows"], f"r-triangle {k} integer flows from the author trades"
        )
        equal(row["score"], projected["gross"], f"r-triangle {k} integer gross")
    brute = _brute_triangle(b)
    check(
        rows["v1"]["score"] is not None and rows["v1"]["score"] < brute,
        "recovery below the integer optimum",
    )
    capped2 = run_factory(CFMM, b, case, {"max_hops": 3}, {**presets["v1"], "max_iterations": 2})
    m2 = next(x for x in MODEL["cases"] if x["id"] == "r-triangle-maxiter2")
    equal(
        capped2["stats"]["cfmm"]["initial"]["termination"], "iteration_cap", "maxiter 2 termination"
    )
    equal(
        [(f["pool_id"], str(f["amount_in"]), str(f["amount_out"])) for f in _flows(capped2)],
        [(f["pool_id"], f["amount_in"], f["amount_out"]) for f in m2["recovery"]["flows"]],
        "maxiter 2 flows = pinned contract model",
    )
    equal(capped2["score"], brute, "maxiter 2 recovers the brute-force integer optimum")
    equal(capped2["stats"]["cfmm"]["estimate_withheld"], "initial_iteration_cap", "no estimate")
    out["triangle"] = {
        "cpmm_oracle_probes": oracle_probes,
        "network": doc,
        "request": request(case),
        "author_prices_normalized": v,
        "author_dual_value": author["dual_value"],
        "author_netflows": author["netflows"],
        "at_author_prices": at_author,
        "at_spot_start": at_spot,
        "independent_projection": projected,
        "brute_force_integer_optimum": brute,
        "v1_historical": _cfmm_view(rows["v1"]),
        "v2_current": _cfmm_view(rows["v2"]),
        "max_iterations_2": _cfmm_view(capped2),
        "tolerances": {"cpmm_oracle": CPMM_ORACLE_TOL, "prices": PRICE_TOL},
    }

    # --- B. cycle: the continuous optimum trades a loop, recovery breaks it and re-solves
    cb, ccase, _ = author_network("r-cycle")
    ca = next(x for x in AUTHOR["router"] if x["id"] == "r-cycle")
    cdoc = next(x for x in AUTHOR_IN["router"] if x["id"] == "r-cycle")
    cv = {tok: x for tok, x in zip(cdoc["tokens"], ca["v"], strict=True)}
    ctrades = _author_trades("r-cycle", cb)
    loop = {
        t["pool_id"]: cv[t["token_in"]] * t["amount_in"]
        for t in ctrades
        if t["pool_id"] in ("ab1", "ab2")
    }
    check(
        cycle_of({(t["token_in"], t["token_out"]) for t in ctrades}), "r-cycle author trades loop"
    )
    removed = min(loop, key=lambda p: loop[p])
    equal(removed, "ab2", "smallest nu_in * x on the cycle")
    broken = project(cb, ccase, [t for t in ctrades if t["pool_id"] != removed])
    cyc = run_factory(CFMM, cb, ccase, {"max_hops": 3}, presets["v2"])
    cm_ = next(x for x in MODEL["cases"] if x["id"] == "r-cycle")
    equal(cyc["stats"]["cfmm"]["recovery"]["cycle_removed"], [removed], "cycle removed")
    equal(cyc["stats"]["cfmm"]["resolve"], "resolved", "restricted re-solve")
    equal(cyc["score"], int(cm_["recovery"]["gross"]), "r-cycle recovered gross = contract model")
    starved = run_factory(
        CFMM,
        cb,
        ccase,
        {"max_hops": 3},
        {
            **presets["v2"],
            "max_function_evaluations": MODEL["forced_caps"]["resolve_starved"]["cap"],
        },
    )
    equal(starved["stats"]["cfmm"]["resolve"], "skipped", "starved re-solve skipped (no refund)")
    equal(_flows(starved), broken["flows"], "starved: the cycle-broken author support projected")
    equal(starved["score"], broken["gross"], "starved gross")
    check(
        float(cyc["stats"]["cfmm"]["estimate"]["value"]) > (cyc["score"] or 0),
        "loop profit not routable",
    )
    out["cycle"] = {
        "request": request(ccase),
        "author_loop_values": loop,
        "removed": removed,
        "author_dual_value": ca["dual_value"],
        "independent_broken_projection": broken,
        "resolved": _cfmm_view(cyc),
        "resolve_starved": _cfmm_view(starved),
    }

    # --- C. nonconvergence: the residual criterion fails, the exact plan is still recovered
    tb, tcase, _ = author_network("r-tiny")
    r5 = RECON["R5_multihop_rounding"]
    tiny = run_factory(CFMM, tb, tcase, {"max_hops": 3}, presets["v2"])
    ti = tiny["stats"]["cfmm"]["initial"]
    equal(ti["termination"], "not_converged", "r-tiny termination")
    check(ti["residual"] > ti["residual_tolerance"], "r-tiny residual above tolerance")
    equal(tiny["score"], r5["stepwise_integer"], "r-tiny exact integer (R5)")
    equal(chain_out(tb, "S", 2, ["h1", "h2"]), r5["stepwise_integer"], "r-tiny hand chain")
    lo, hi = (float(x) for x in r5["continuous_bounds"])
    check(lo <= ti["value"] <= hi + 0.01, "r-tiny continuous value within R5 bounds")
    equal(
        (
            tiny["stats"]["cfmm"]["estimate_withheld"],
            tiny["stats"]["r021"]["certificate"]["bound_kind"],
        ),
        ("initial_not_converged", "unknown"),
        "r-tiny: no estimate",
    )
    out["nonconvergence"] = {"request": request(tcase), "R5": r5, "row": _cfmm_view(tiny)}

    # --- D. recovery failures and the declared fallback (option overrides, visibly labelled)
    db = bundle_of("dust", cp("big", "S", "T", 10**6, 5 * 10**5), cp("dust", "S", "T", 1, 1))
    dcase = Case("dust", "S", "T", 10)
    big_hand = hand_out(db.pools["big"], "S", 10)  # type: ignore[arg-type]
    pruned = run_factory(CFMM, db, dcase, {"max_hops": 3}, presets["v2"])
    equal(pruned["score"], big_hand, "dust: pruned and retried onto `big`")
    equal(
        pruned["stats"]["cfmm"]["recovery"]["pruned"],
        [{"pool_id": "dust", "reason": "insufficient_output_amount", "attempt": 1}],
        "dust prune",
    )
    exhausted = run_factory(
        CFMM, db, dcase, {"max_hops": 3}, {**presets["v2"], "max_recovery_attempts": 1}
    )
    equal(exhausted["stats"]["cfmm"]["recovery_failure"], "attempts_exhausted", "attempt cap")
    equal(
        exhausted["stats"]["r021"]["fallback"],
        {"used": True, "source": "single_path", "reason": "attempts_exhausted"},
        "fallback",
    )
    equal(exhausted["score"], big_hand, "fallback = best exact single path")
    none = run_factory(
        CFMM,
        db,
        dcase,
        {"max_hops": 3},
        {**presets["v2"], "max_recovery_attempts": 1, "fallback": "none"},
    )
    equal(none["status"], "model_error", "no fallback: model_error")
    ob = bundle_of("tiny_order", cp("a", "S", "T", 1000, 1000))
    one = run_factory(CFMM, ob, Case("one", "S", "T", 1), {"max_hops": 3}, presets["v2"])
    equal(hand_out(ob.pools["a"], "S", 1), None, "a 1-unit order has zero output")  # type: ignore[arg-type]
    equal(
        (one["status"], one["stats"]["cfmm"]["recovery_failure"]),
        ("no_route", "support_exhausted"),
        "tiny order: support exhausted, fallback no_route",
    )
    from routing.cfmm.recovery import RecoveryOptions, recover
    from routing.search import QuoteCache

    sb = bundle_of("surplus", cp("sm", "S", "M", 10**6, 10**6), cp("mt", "M", "T", 10**6, 10**6))
    scase = Case("surplus", "S", "T", 1000)
    srec = recover(
        sb,
        scase,
        ["sm", "mt"],
        [
            cfmm_model.Trade("sm", "S", "M", 1000.0, 100.0),
            cfmm_model.Trade("mt", "M", "T", 60.0, 59.0),
        ],
        {"S": 1.0, "M": 1.0},
        RecoveryOptions(1e-6, 8, True),
        QuoteCache(sb),
        gross_only(),
        None,
    )
    s_hand = chain_out(sb, "S", 1000, ["sm", "mt"])
    equal(srec.gross, s_hand, "surplus: all of M goes on (hand chain)")
    out["recovery_failures"] = {
        "dust_pruned": _cfmm_view(pruned),
        "attempts_exhausted": _cfmm_view(exhausted),
        "fallback_none": _cfmm_view(none),
        "tiny_order": _cfmm_view(one),
        "surplus_internal": {
            "note": "routing.cfmm.recovery.recover on hand-given continuous "
            "trades (sm 1000->100 M, mt 60->59): the numerical internal the "
            "factory calls, not a factory solve",
            "gross": srec.gross,
            "flows": [
                {"pool_id": f.edge.pool_id, "amount_in": f.amount_in, "amount_out": f.amount_out}
                for f in srec.flows
            ],
        },
    }

    # --- E. CL stage: interval index, boundaries, author probes, exact money, mixed cycle
    state = synthetic_cl()
    idx = cfmm_cl.build_cl_index(state)
    bounds = list(reversed(idx.up.edges[1:])) + list(idx.down.edges[1:])
    liq = (
        list(reversed(idx.up.liquidity[1:])) + [idx.up.liquidity[0]] + list(idx.down.liquidity[1:])
    )
    probes = [c for c in AUTHOR_IN["univ3_oracle"] if c["state"] == "synthetic"]
    for c in probes:  # the pinned author inputs ARE this state's index (cfmm-dual.md §8.3)
        equal(c["lower_ticks"], [x * x for x in bounds], f"{c['id']} boundaries")
        equal(c["liquidity"], [x * x for x in liq] + [0.0], f"{c['id']} liquidity")
    probe_rows = []
    for c in probes:
        trade = cfmm_model.cl_arb(idx, {"T0": c["v"][0], "T1": c["v"][1]})
        ref = next(x for x in AUTHOR["univ3_oracle"] if x["id"] == c["id"])
        vec = [0.0, 0.0, 0.0, 0.0]
        if trade is not None:
            i = 0 if trade.token_in == "T0" else 1
            vec[i], vec[3 - i] = trade.amount_in, trade.amount_out
        worst = max(
            (0.0 if g == w == 0 else rel(g, w))
            for g, w in zip(vec, ref["delta"] + ref["lambda"], strict=True)
        )
        check(worst < CL_ORACLE_TOL, f"{c['id']} CL oracle vs author")
        probe_rows.append(
            {
                "id": c["id"],
                "prices": c["v"],
                "delta": vec[:2],
                "lambda": vec[2:],
                "max_rel_diff": worst,
            }
        )
    across = int(idx.down.net[2] / idx.gamma) + 10**12
    beyond = int(idx.down.net[-1] / idx.gamma) * 2
    singles = []
    for amount in (10**6, across, beyond):
        sbd = bundle_of("cl_single", state)
        scase2 = Case("cl_single", "T0", "T1", amount)
        exact = quote_exact_in(state, "T0", amount)
        row = run_factory(CFMM, sbd, scase2, {"max_hops": 3}, presets["v2"])
        if exact.status is QuoteStatus.OK and exact.amount_out > 0:
            equal(row["score"], exact.amount_out, f"CL single market {amount}: exact quote")
        else:
            equal(
                (row["status"], exact.status.value),
                ("incomplete_snapshot", "incomplete_snapshot"),
                f"CL single market {amount}: beyond the collected range",
            )
        singles.append(
            {
                "amount_in": amount,
                "exact_status": exact.status.value,
                "exact_out": exact.amount_out,
                "ticks_crossed": dict(exact.features),
                "row": _cfmm_view(row),
            }
        )
    missing = synthetic_cl(missing_tick_data=True)
    midx = cfmm_cl.build_cl_index(missing)
    x = _exact_landing(missing, "T0")
    mb = bundle_of("cl_missing", missing)
    before = run_factory(CFMM, mb, Case("m1", "T0", "T1", x - 1), {"max_hops": 3}, presets["v2"])
    equal(before["score"], quote_exact_in(missing, "T0", x - 1).amount_out, "one below the landing")
    landed = run_factory(CFMM, mb, Case("m2", "T0", "T1", x), {"max_hops": 3}, presets["v2"])
    equal(
        landed["stats"]["cfmm"]["recovery"]["pruned"],
        [{"pool_id": missing.pool_id, "reason": "incomplete_snapshot", "attempt": 1}],
        "landing pruned",
    )
    equal(landed["status"], "incomplete_snapshot", "landing on missing TickInfo")
    cyc_b = bundle_of(
        "cl_cycle",
        cp("s0", "S", "T0", 10**16, 10**16),
        state,
        cp("x01", "T0", "T1", 2 * 10**15, 10**15),
        cp("t1", "T1", "T", 10**16, 10**16),
    )
    cyc_case = Case("cl_cycle", "S", "T", 10**13)
    cl_cycle = run_factory(CFMM, cyc_b, cyc_case, {"max_hops": 3}, presets["v2"])
    check(
        bool(cl_cycle["stats"]["cfmm"]["recovery"]["cycle_removed"]), "mixed CL/CPMM cycle removed"
    )
    equal(cl_cycle["stats"]["cfmm"]["resolve"], "resolved", "mixed cycle re-solved")
    out["cl"] = {
        "state": {
            "pool_id": state.pool_id,
            "source_key": state.source_key,
            "fee": state.fee,
            "tick": state.tick,
            "tick_spacing": state.tick_spacing,
            "liquidity": state.liquidity,
            "positions": [[-1800, -1200, 4 * 10**15], [-600, 1200, 10**16]],
            "bitmap_word_range": list(state.bitmap_word_range),
        },
        "index": {
            "down": {
                "liquidity": list(idx.down.liquidity),
                "boundary": idx.down.boundary,
                "boundary_tick": idx.down.boundary_tick,
            },
            "up": {
                "liquidity": list(idx.up.liquidity),
                "boundary": idx.up.boundary,
                "boundary_tick": idx.up.boundary_tick,
            },
            "gamma": idx.gamma,
        },
        "missing_index": {"boundary": midx.down.boundary, "boundary_tick": midx.down.boundary_tick},
        "author_probes": probe_rows,
        "tolerance": CL_ORACLE_TOL,
        "single_market": singles,
        "missing_tick_landing": {
            "landing_input": x,
            "one_below": _cfmm_view(before),
            "landing": _cfmm_view(landed),
        },
        "mixed_cycle": {"request": request(cyc_case), "row": _cfmm_view(cl_cycle)},
    }

    # --- F. real admitted CPMM + CL states (mantle_mixed, block 101,057,678): stage comparison
    mixed = load_bundle(FIXTURES / "routing" / "mantle_mixed")
    mcase = next(c for c in mixed.cases if c.case_id == "usdc_usdt_small")
    v2 = run_factory(CFMM, mixed, mcase, {"max_hops": 3}, presets["v2"], Budget(max_quotes=50_000))
    v1 = run_factory(CFMM, mixed, mcase, {"max_hops": 3}, presets["v1"], Budget(max_quotes=50_000))
    mref = next(x for x in MODEL["cases"] if x["id"] == "usdc_usdt_small")
    equal(
        [(f["pool_id"], str(f["amount_in"]), str(f["amount_out"])) for f in _flows(v1)],
        [(f["pool_id"], f["amount_in"], f["amount_out"]) for f in mref["recovery"]["flows"]],
        "mantle_mixed CPMM stage (v1) = pinned contract model flows",
    )
    equal(v1["score"], int(mref["recovery"]["gross"]), "mantle_mixed v1 gross")
    kinds = {s["pool_id"]: type(mixed.pools[s["pool_id"]]).__name__ for s in v2["plan"] or []}
    check(
        "ConcentratedPoolState" in kinds.values() and "ConstantProductPoolState" in kinds.values(),
        "v2 plan routes CL and CPMM legs",
    )
    for flow in _flows(v2):  # one merged step per pool: each leg is its original-state quote
        exact_leg = quote_exact_in(
            mixed.pools[flow["pool_id"]], flow["token_in"], flow["amount_in"]
        )
        equal(exact_leg.amount_out, flow["amount_out"], f"exact quote of leg {flow['pool_id']}")
    lb = [p for p, s in mixed.pools.items() if isinstance(s, LiquidityBookPoolState)]
    check(not set(v2["stats"]["cfmm"]["markets"]) & set(lb), "LB never a CFMM market")
    sub = bundle_of("markets", *(mixed.pools[p] for p in v2["stats"]["cfmm"]["markets"]))
    singles_exact = {
        "->".join(e[0][:10] for e in p): _exact_chain(mixed, mcase.token_in, mcase.amount_in, p)
        for p in simple_paths(sub, mcase.token_in, mcase.token_out, 3)
    }
    best_single = max(v for v in singles_exact.values() if v is not None)
    check(v2["score"] is not None and v2["score"] > best_single, "v2 beats every exact single path")
    out["real_cp_cl"] = {
        "fixture": "tests/fixtures/routing/mantle_mixed (8 real pools, Mantle block 101,057,678; "
        "NOT the 19-pool corpus fixture nor the 143-pool corpus)",
        "bundle_hash": mixed.bundle_hash,
        "request": request(mcase),
        "leg_kinds": kinds,
        "best_exact_single_path": best_single,
        "single_paths": singles_exact,
        "v2_cl_stage": _cfmm_view(v2),
        "v1_cpmm_stage": _cfmm_view(v1),
    }
    return out


# ====================================================================== 6. fixed-block real state

USDC = "0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9"
USDT0 = "0x779ded0c9e1022225f8e0630b35a9b54be713736"
SOURCE_PROFILE = "config/daily_gross.yaml"


def all_profile(source: str = SOURCE_PROFILE) -> tuple[dict[str, Any], Any, str]:
    """The effective `--strategies all` profile of `source` (the CLI's own derivation)."""
    data = (ROOT / source).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    document, profile = derive(
        yaml.safe_load(data), "all", source_path=source, source_sha256=digest
    )
    return document, profile, digest


def _protocol(pool: PoolState) -> str:
    return f"{type(pool).__name__.removesuffix('PoolState')}:{getattr(pool, 'source_key', None)}"


def real_state_walkthrough() -> dict[str, Any]:
    bundle = load_bundle(FIXTURES / "corpus" / "bundle")
    document, profile, source_sha = all_profile()
    case = Case("real_usdc_usdt0_10k", USDC, USDT0, 10_000_000_000)
    direct_pools = bundle.pools_for_pair(USDC, USDT0)
    agni = next(p for p in direct_pools if isinstance(p, ConcentratedPoolState))
    lb = next(p for p in direct_pools if p.pool_id.startswith("0x368b"))
    one_leg = quote_exact_in(agni, USDC, case.amount_in).amount_out
    split = (
        quote_exact_in(agni, USDC, 9_950_000_000).amount_out,
        quote_exact_in(lb, USDC, 50_000_000).amount_out,
    )
    expected: dict[str, int | None] = {name: one_leg for name in profile.algorithms}
    for name in (
        "incremental_graph",
        "metis_inspired",
        "metis_history",
        "incremental_graph_repair",
        # WHI-1600: the exact accelerations return their reference's plan (pruning contract §8)
        "incremental_graph_bounded",
        "metis_history_bounded",
    ):
        expected[name] = sum(split)
    expected["direct_split_certified"] = None  # unsupported: the pair's direct pools are CL/LB
    rows = []
    for name in profile.algorithms:
        factory = ALGORITHMS[name]
        config = profile.algorithm_config(factory)
        prepared = factory.prepare(bundle, config) if factory.prepare is not None else None
        identity = {
            "bundle_hash": bundle.bundle_hash,
            "algorithm": name,
            "effective_settings_sha256": (profile.algorithm_options.get(name) or {}).get(
                "settings_sha256"
            ),
        }
        result = factory.solve(
            case,
            SolveContext(bundle, profile.objective, prepared, run_identity=identity),
            profile.budget,
        )
        replay = fresh_replay(bundle, case, result.plan)
        equal(result.score, expected[name], f"real state {name}")
        if result.status is SolveStatus.OK:
            assert replay is not None
            equal(
                (replay["status"], replay["gross"]),
                ("ok", result.score),
                f"real state {name} replay",
            )
        s = dict(result.search_stats)
        r021 = s.get("r021") or {}
        row = {
            "algorithm": name,
            "status": result.status.value,
            "score": result.score,
            "error": result.error,
            "legs": [
                {
                    "pool_id": st.pool_id,
                    "protocol": _protocol(bundle.pools[st.pool_id]),
                    "amount_in": (replay or {"trace": []})["trace"][i]["amount_in"]
                    if replay
                    else None,
                    "amount_out": (replay or {"trace": []})["trace"][i]["amount_out"]
                    if replay
                    else None,
                }
                for i, st in enumerate(result.plan.steps)
            ]
            if result.plan
            else None,
            "replay_status": (replay or {}).get("status"),
            "replay_gross": (replay or {}).get("gross"),
            "params": json.loads(json.dumps(dict(config.params), default=list)),
            "options": profile.algorithm_options.get(name),
            "recipe": (profile.strategies.get(name) or {}).get("recipe"),
            "chosen_source": s.get("chosen_source"),
            "fallback": r021.get("fallback"),
            "scope": r021.get("scope"),
            "certificate": r021.get("certificate"),
            "termination": s.get("termination") or (s.get("cfmm") or {}).get("termination"),
            "factory_counters": {
                "quotes_executed": s.get("quotes_executed"),
                **({"r021_work": r021.get("work")} if r021 else {}),
            },
        }
        if name == "cfmm_dual":
            c = s["cfmm"]
            row["cfmm"] = {
                "stage": c["stage"],
                "markets": c["markets"],
                "initial_termination": c["initial"]["termination"],
                "recovery_failure": c["recovery_failure"],
                "estimate": c.get("estimate"),
            }
            check(
                not {
                    p for p in c["markets"] if isinstance(bundle.pools[p], LiquidityBookPoolState)
                },
                "LB never a cfmm market",
            )
        if name == "uni_sor_cycle_safe":
            row["cycle_safe"] = {
                k: s["cycle_safe"][k]
                for k in ("admission_checks", "combinations_rejected_cycle", "reference_trajectory")
            }
        if name == "incremental_graph_repair":
            row["repair"] = _repair_view({"stats": s})
        if name == "metis_history":
            row["history"] = {
                k: s.get(k)
                for k in (
                    "termination",
                    "labels_dropped_signature_cap",
                    "chunks_state_capped",
                    "incremental_evaluated_gross",
                )
            }
        rows.append(row)
    kinds = sorted(_protocol(p) for p in direct_pools)
    by = {r["algorithm"]: r for r in rows}
    equal(
        by["direct_split_certified"]["scope"],
        {"supported": False, "reason": "non_constant_product_direct_pool"},
        "certified row is a visible unsupported row",
    )
    equal(
        by["uni_sor_cycle_safe"]["cycle_safe"]["reference_trajectory"],
        "identical",
        "cycle-safe = port",
    )
    two_leg = fresh_replay(bundle, case, _plan_of(bundle, case, "incremental_graph", profile))

    # the CPMM-stage ablation (historical cfmm_dual/1 via config/cfmm_dual/cpmm.yaml, explicit
    # `--strategies profile` only; never one of the 14 `all` rows): the same request over the
    # stage's CPMM markets alone
    cpmm_path = "config/cfmm_dual/cpmm.yaml"
    cpmm_bytes = (ROOT / cpmm_path).read_bytes()
    _, cpmm_profile = derive(
        yaml.safe_load(cpmm_bytes),
        "profile",
        source_path=cpmm_path,
        source_sha256=hashlib.sha256(cpmm_bytes).hexdigest(),
    )
    ablation = run_factory(
        CFMM,
        bundle,
        case,
        dict(cpmm_profile.algorithm_config(ALGORITHMS[CFMM]).params),
        cpmm_profile.algorithm_options[CFMM]["options"],
        cpmm_profile.budget,
    )
    markets = ablation["stats"]["cfmm"]["markets"]
    check(
        all(isinstance(bundle.pools[m], ConstantProductPoolState) for m in markets),
        "CPMM stage: CPMM markets only",
    )
    cpmm_sub = bundle_of("cpmm_markets", *(bundle.pools[m] for m in markets))
    cpmm_best_single = max(
        v
        for v in (
            chain_out(cpmm_sub, USDC, case.amount_in, [e[0] for e in p])
            for p in simple_paths(cpmm_sub, USDC, USDT0, 3)
        )
        if v is not None
    )
    check(
        ablation["score"] is not None and ablation["score"] >= cpmm_best_single,
        "CPMM stage: at least the best exact CPMM single path (hand)",
    )
    return {
        "bundle": {
            "path": "tests/fixtures/corpus/bundle",
            "bundle_id": bundle.bundle_id,
            "bundle_hash": bundle.bundle_hash,
            "kind": bundle.kind,
            "block": {
                "chain_id": bundle.block.chain_id,
                "number": bundle.block.number,
                "hash": bundle.block.hash,
            },
            "pools": len(bundle.pools),
            "pool_protocols": dict(
                sorted(_count(_protocol(p) for p in bundle.pools.values()).items())
            ),
            "note": "19-pool checked-in real-state FIXTURE of the five-source corpus at this "
            "block, not the full corpus (whose bundle_tuning split has 143 pools, "
            "integer-allocation.json probe)",
        },
        "request": {
            **request(case),
            "symbols": "10000 USDC -> USDT0 (6 decimals each)",
            "direct_pools": kinds,
        },
        "source_profile": {"path": SOURCE_PROFILE, "sha256": source_sha},
        "effective_profile": {
            "selection": document["selection"],
            "algorithms": list(profile.algorithms),
            "search": dict(profile.search),
            "graph": dict(profile.graph),
            "budget": profile.budget.to_dict(),
            "objective": profile.objective.mode,
            "algorithm_options": profile.algorithm_options,
        },
        "independent": {
            "agni_full_input_exact_quote": one_leg,
            "agni_9950000000_plus_lb_50000000": list(split),
        },
        "rows": rows,
        "representative_two_leg_funds": two_leg,
        "cpmm_stage_ablation": {
            "profile": {"path": cpmm_path, "sha256": hashlib.sha256(cpmm_bytes).hexdigest()},
            "note": "explicit --strategies profile only; the historical cfmm_dual/1 CPMM stage "
            "over this fixture's CPMM markets; not one of the 14 all rows",
            "markets": markets,
            "best_hand_cpmm_single_path": cpmm_best_single,
            "row": _cfmm_view(ablation),
        },
    }


def _count(items: Iterator[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return out


def _plan_of(bundle: SnapshotBundle, case: Case, name: str, profile: Any) -> RoutePlan | None:
    factory = ALGORITHMS[name]
    prepared = (
        factory.prepare(bundle, profile.algorithm_config(factory)) if factory.prepare else None
    )
    return factory.solve(
        case, SolveContext(bundle, profile.objective, prepared), profile.budget
    ).plan


# ====================================================================== output

EXAMPLES: dict[str, Callable[[], dict[str, Any]]] = {
    "metis_history": example_metis_history,
    "direct_split_certified": example_direct_split_certified,
    "incremental_graph_repair": example_incremental_graph_repair,
    "uni_sor_cycle_safe": example_uni_sor_cycle_safe,
    "cfmm_dual": example_cfmm_dual,
    "real_state": real_state_walkthrough,
}


def collect() -> dict[str, Any]:
    """Every example as plain data (each has already checked its independent expectations)."""
    return {name: fn() for name, fn in EXAMPLES.items()}


def _p(*parts: Any) -> None:
    print("".join(str(p) for p in parts))


def _alloc(row: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [(a["pool_id"], a["amount_in"]) for a in row.get("best_allocation") or []]


def _print_metis_history(mh: Mapping[str, Any]) -> None:
    c = mh["competing_prefixes"]
    src = c["options"]["source"]
    _p("--- 12. Experimental strategy: metis_history (history labels; NOT Jupiter Metis) ---")
    _p(
        f"R1 {c['request']['amount_in']} A->T, options {src['kind']} v{src.get('version')} ",
        f"(settings {c['options']['settings_sha256'][:12]})",
    )
    for lab in c["layer2_at_C"]:
        sig = ",".join(lab["visited"])
        _p(f"  layer-2 label at C via {lab['pools']}: signature (C, {{{sig}}}) = {lab['amount']}")
    best, walk = c["exhaustive_best"], c["repeated_token_walk"]
    _p(
        f"  exhaustive best simple path {best['pools']} = {best['gross']}; repeated-token walk ",
        f"{walk['gross_if_allowed']} is {walk['evaluator']}",
    )
    h = c["metis_history"]
    _p(
        f"  metis_history {h['score']} (fresh replay {h['replay_gross']}, hand {h['hand_gross']}); "
        "",
        f"metis_inspired token labels {c['metis_inspired']['score']}",
    )
    x = mh["admission_x4b"]
    _p(
        f"Admission (X4b, 2 chunks): chunk-1 edges {x['committed_after_chunk_1']}; ",
        f"{x['rejected_continuation']['pools']} would close X->A->V->X; history ",
        f"{[c['pools'] for c in x['independent_trajectory']]} -> {x['metis_history']['score']} ",
        f"vs metis_inspired {x['metis_inspired']['score']}",
    )
    u = mh["unsafe_deletion"]
    _p(
        f"Unsafe deletion (overflow): X labels {u['x_labels']}; X held {u['x_reserves_held']} ",
        ">= 2^112, region not certified",
    )
    _p(
        f"  wide caps keep both -> {u['wide_caps']['score']}; preset (1 label/signature) keeps the "
        "",
        f"reverting big label -> {u['preset']['score']} "
        f"({u['preset']['diagnostics']['termination']});",
        f" metis_inspired {u['metis_inspired']['score']}",
    )
    s = mh["safe_deletion"]
    sd = s["metis_history"]["diagnostics"]
    _p(
        "Safe deletion (R1, 3 chunks, certified CPMM region): strict insertions ",
        f"{sd['certified_strict_insertions']}, gross {s['metis_history']['score']}, chunk paths = ",
        f"independent per-chunk maxima {[x['pools'] for x in s['independent_trajectory']]}",
    )
    t = mh["tie_state"]
    _p(
        f"Tie: chunk-1 X via p1/p3 {t['hand_chunk1_X']} (equal q output); final history ",
        f"{t['metis_history']['diagnostics']['incremental_evaluated_gross']} vs enumeration ",
        f"{t['enumeration']['diagnostics']['incremental_evaluated_gross']}",
    )
    g = mh["greedy_trap"]
    _p(
        f"Greedy trap: per-chunk exact {[x['pools'] for x in g['independent']]} -> ",
        f"{g['metis_history']['diagnostics']['incremental_evaluated_gross']}; metis_inspired ",
        f"{g['metis_inspired']['diagnostics']['incremental_evaluated_gross']} (no whole-plan "
        "claim)",
    )
    k = mh["caps"]
    fc = k["frontier_cap_R1"]
    _p(
        f"Caps: R1 frontier cap 1 -> {fc['diagnostics']['termination']}, fallback ",
        f"{fc['fallback']['source']} {fc['score']}; full-fill uncapped "
        f"{k['full_fill_uncapped']['score']}, ",
        f"capped {k['full_fill_capped']['status']} [OK]\n",
    )


def _print_certified(ds: Mapping[str, Any]) -> None:
    gr, ind = ds["grid38"], ds["grid38"]["independent"]
    _p(
        "--- 13. Experimental strategy: direct_split_certified (certified grid branch and bound) "
        "---"
    )
    _p(
        f"R6 38 S->T, 5 % grid, 2 splits: grid optimum {ind['grid_optimum_p1p2']} ",
        f"({len(ind['grid_argmax_p1p2'])} tied allocations), raw optimum {ind['raw_optimum']}, ",
        f"best single {ind['best_single']}, continuous {ind['continuous_optimum_bounds']}",
    )
    for key in (
        "complete",
        "pool_order_p2p1",
        "raw_integer",
        "node_cap_1",
        "quote_budget_2",
        "quote_budget_0",
    ):
        r = gr[key]
        cert = r["certificate"] or {}
        _p(
            f"  {key:16s} {r['status']:8s} {r['score']} [{cert.get('lower_raw')}, ",
            f"{cert.get('upper_raw')}] gap {cert.get('gap_raw')} {cert.get('termination')} "
            f"{_alloc(r)}",
        )
    _p(
        f"  root bound {gr['search_trace']['root']['ub']} = tangent floor "
        f"{ind['tangent_upper_floor']}"
    )
    pl = ds["plateau_r3"]
    _p(
        f"R3 plateau (raw, 53): G {pl['G']}; certified {pl['raw_integer']['score']} ",
        f"{_alloc(pl['raw_integer'])}, root bound {pl['root']['ub']} > 70",
    )
    mixed = ds["unsupported_mixed"]["row"]
    _p(
        f"Dust {ds['dust']['row']['score']}; real Moe single pool ",
        f"{ds['real_moe_single_pool']['row']['score']}; mantle_mixed {mixed['status']} ",
        f"({mixed['scope']['reason']}) [OK]\n",
    )


def _print_repair(ig: Mapping[str, Any]) -> None:
    st = ig["structural_trap"]
    _p("--- 14. Experimental strategy: incremental_graph_repair (checkpoint + suffix repair) ---")
    for seq, state in zip(st["independent_greedy"], st["independent_states"], strict=True):
        _p(
            f"  greedy chunk {seq['chunk']} ({seq['amount']}): {seq['pools']} marginal ",
            f"{seq['marginal']}; token edges {state['token_edges']}",
        )
    ex = st["exhaustive"]
    _p(
        f"  incumbent {st['repair_off']['score']} (= incremental_graph); exhaustive best ",
        f"{ex['best_gross']} of {ex['complete_sequences']} sequences {ex['best_sequence']}",
    )
    na = st["non_additive_pool_ed"]
    _p(
        f"  pool ed aggregate {na['aggregate_input']} -> {na['aggregate_output']} vs separate "
        "chunk ",
        f"quotes {na['sum_of_separate_chunk_quotes']} (rebuild, never subtract)",
    )
    on = st["repair_on"]
    tries = [(a["checkpoint"], a["outcome"], a["score"]) for a in on["repair"]["attempts"]]
    _p(
        f"  repair on: {tries} -> {on['score']} ({on['repair']['stop']}); evaluations ",
        f"{on['evaluations']}",
    )
    tw = ig["twin_pools_rejected"]["repair_on"]
    _p(
        f"Twin pools: {[a['outcome'] for a in tw['repair']['attempts']]}, kept ",
        f"{tw['diagnostics']['chosen_source']} {tw['score']}",
    )
    cut = ig["quote_budget_cut"]
    _p(
        f"Attempt cap 1: {ig['attempt_cap_1']['repair']['stop']} -> "
        f"{ig['attempt_cap_1']['score']}; ",
        f"max_quotes {cut['max_quotes']}: {cut['row']['repair']['stop']} -> {cut['row']['score']}; "
        "",
        f"WHI-1549 greedy trap -> {ig['greedy_trap_cross_reference']['row']['score']} [OK]\n",
    )


def _print_cycle_safe(cs: Mapping[str, Any]) -> None:
    a1 = cs["union_cycle_A1"]
    safe = a1["uni_sor_cycle_safe"]
    _p(
        "--- 15. Experimental strategy: uni_sor_cycle_safe (token-DAG admission; not SOR parity) "
        "---"
    )
    _p(
        f"A1 2000000 s->t: abc {a1['hand_quotes']['abc']}, def {a1['hand_quotes']['def']} ",
        f"(at 50 % / 100 %); union edges {a1['union_edges']} close x->y->x",
    )
    _p(f"  uni_sor_port: {a1['uni_sor_port']['status']} ({a1['uni_sor_port']['error']})")
    _p(
        f"  uni_sor_cycle_safe: {safe['status']} {safe['score']} via ",
        f"{[x['pool_id'] for x in safe['plan']]} at 100 %; {safe['admission']}; oracle best ",
        f"admissible {a1['oracle_step50']['best_admissible']} vs any "
        f"{a1['oracle_step50']['best_any']}",
    )
    a3, teach = cs["no_admissible_A3"], cs["teaching_graph"]["uni_sor_cycle_safe"]
    _p(
        f"A3: port {a3['uni_sor_port']['status']}, cycle_safe "
        f"{a3['uni_sor_cycle_safe']['status']}; ",
        f"teaching graph {teach['score']} ({teach['cycle_safe']['reference_trajectory']}) [OK]\n",
    )


def _print_cfmm(cf: Mapping[str, Any]) -> None:
    tr = cf["triangle"]
    spot, auth = tr["at_spot_start"], tr["at_author_prices"]
    _p("--- 16. Experimental strategy: cfmm_dual (dual decomposition + integer recovery) ---")
    _p(
        f"r-triangle 150 S->T: spot start {spot['prices']} -> S excess "
        f"{spot['source_excess']:.4f}, ",
        f"M net {spot['net_flow']['M']:.4f}",
    )
    prices = {k: round(v, 10) for k, v in tr["author_prices_normalized"].items()}
    _p(
        f"  author prices {prices}: S excess {auth['source_excess']:.2e}, M net ",
        f"{auth['net_flow']['M']:.2e}, T {auth['net_flow']['T']:.6f}",
    )
    for key in ("v1_historical", "v2_current", "max_iterations_2"):
        r = tr[key]
        ini = r["initial"]
        flows = [(f["pool_id"], f["amount_in"], f["amount_out"]) for f in r["flows"]]
        _p(
            f"  {key:16s} {ini['termination']:13s} residual {ini['residual']:.3e} value ",
            f"{ini['value']:.6f} -> {r['score']} {flows} ({r['certificate']['bound_kind']})",
        )
    _p(
        f"  projection of the author trades {tr['independent_projection']['gross']}; brute-force ",
        f"integer optimum {tr['brute_force_integer_optimum']} (no optimality claim)",
    )
    cy = cf["cycle"]
    loop = {k: round(v, 3) for k, v in cy["author_loop_values"].items()}
    _p(
        f"r-cycle: loop values {loop} -> remove {cy['removed']}; re-solved "
        f"{cy['resolved']['score']} ",
        f"vs continuous {float(cy['resolved']['estimate']['value']):.4f}; starved ",
        f"{cy['resolve_starved']['score']} = projection "
        f"{cy['independent_broken_projection']['gross']}",
    )
    nc = cf["nonconvergence"]["row"]
    _p(
        f"r-tiny: {nc['initial']['termination']} residual {nc['initial']['residual']:.4f} > ",
        f"{nc['initial']['residual_tolerance']} -> {nc['score']}, {nc['estimate_withheld']}",
    )
    rf = cf["recovery_failures"]
    _p(
        f"Recovery: dust pruned -> {rf['dust_pruned']['score']}; attempts_exhausted -> ",
        f"{rf['attempts_exhausted']['fallback']['source']} {rf['attempts_exhausted']['score']}; ",
        f"fallback none -> {rf['fallback_none']['status']}; 1-unit order -> ",
        f"{rf['tiny_order']['status']}; surplus internal {rf['surplus_internal']['gross']}",
    )
    cl = cf["cl"]
    worst = max(p["max_rel_diff"] for p in cl["author_probes"])
    _p(
        f"CL synthetic: down liquidity {cl['index']['down']['liquidity']} to ",
        f"{cl['index']['down']['boundary']}; author probes max rel diff {worst:.2e} < "
        f"{cl['tolerance']}",
    )
    for sm in cl["single_market"]:
        r = sm["row"]
        ticks = sm["ticks_crossed"].get("initialized_ticks_crossed")
        _p(
            f"  single market {sm['amount_in']}: exact {sm['exact_status']} {sm['exact_out']} ",
            f"(ticks {ticks}) -> {r['status']} {r['score']} (recovery_failure ",
            f"{r['recovery_failure']}, fallback {r['fallback']['used']})",
        )
    ml = cl["missing_tick_landing"]
    _p(
        f"  missing TickInfo: landing {ml['landing_input']} -> {ml['landing']['status']}; ",
        f"one below -> {ml['one_below']['score']}",
    )
    mc = cl["mixed_cycle"]["row"]
    _p(f"  mixed CL/CPMM cycle: removed {mc['recovery']['cycle_removed']} -> {mc['score']}")
    rc = cf["real_cp_cl"]
    _p(
        "mantle_mixed usdc_usdt_small (real, block 101,057,678): CL stage ",
        f"{rc['v2_cl_stage']['score']} ({sorted(set(rc['leg_kinds'].values()))}); CPMM stage ",
        f"{rc['v1_cpmm_stage']['score']}; best exact single path {rc['best_exact_single_path']} "
        "[OK]\n",
    )


def _print_real_state(rs: Mapping[str, Any]) -> None:
    b = rs["bundle"]
    _p("--- 17. Real-state fixed-block walkthrough: 17 rows (--strategies all, daily_gross) ---")
    _p(
        f"bundle {b['bundle_id']} ({b['bundle_hash'][:12]}), block {b['block']['number']}, ",
        f"{b['pools']} pools {b['pool_protocols']}",
    )
    _p(f"  {b['note']}")
    src = rs["source_profile"]
    _p(
        f"request {rs['request']['symbols']}; source {src['path']} ({src['sha256'][:12]}), ",
        f"selection {rs['effective_profile']['selection']['mode']}",
    )
    for r in rs["rows"]:
        opt = r["options"]
        tag = f" [{opt['source']['kind']} v{opt['source'].get('version')}]" if opt else ""
        if r["scope"] and not r["scope"]["supported"]:
            tag = f" ({r['scope']['reason']}){tag}"
        elif r["certificate"]:
            tag = f" (bound {r['certificate']['bound_kind']}){tag}"
        legs = ", ".join(
            f"{x['pool_id'][:10]} {x['protocol']} {x['amount_in']}" for x in r["legs"] or []
        )
        _p(f"  {r['algorithm']:25s} {r['status']:12s} {str(r['score']):12s} {legs}{tag}")
    ab = rs["cpmm_stage_ablation"]
    _p(
        f"  (ablation) cfmm_dual {ab['profile']['path']} [{ab['row']['options_source']['kind']} ",
        f"v{ab['row']['options_source'].get('version')}]: ",
        f"{ab['row']['status']} {ab['row']['score']} ",
        f"over CPMM markets {[m[:10] for m in ab['markets']]} (hand best CPMM path ",
        f"{ab['best_hand_cpmm_single_path']}) -- stage, not search quality",
    )


def print_report(data: Mapping[str, Any]) -> None:
    """The inspectable transcript; every number comes from the checked example data."""
    _print_metis_history(data["metis_history"])
    _print_certified(data["direct_split_certified"])
    _print_repair(data["incremental_graph_repair"])
    _print_cycle_safe(data["uni_sor_cycle_safe"])
    _print_cfmm(data["cfmm_dual"])
    _print_real_state(data["real_state"])
    _p("All R021 worked-example suites passed (independent expectations; timings not asserted)")


def run_all() -> dict[str, Any]:
    data = collect()
    print_report(data)
    return data


if __name__ == "__main__":
    run_all()

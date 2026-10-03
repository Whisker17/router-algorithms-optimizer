"""Author the frozen *inputs* of the Uniswap SOR goldens (WHI-1443 / I22).

This script only writes `tests/fixtures/uni_sor/<case>.input.json`. It never decides
what upstream selects: `generate.js` feeds each input to the actual pinned
`@uniswap/smart-order-router@4.31.10` functions and writes `<case>.golden.json`.
Contract: `docs/references/uni-sor-port-contract.md` (§7.4 input format, §8 golden
categories G-1…G-14).

Two input kinds:

- **synthetic** -- hand-built pool graphs with authored quote/gas tables. Each case
  names the contract categories it pins, the naive alternative it must defeat and
  a hand-derived `expect` block that `tests/routing/test_sor_fixture_schema.py`
  checks against the upstream golden.
- **corpus** -- the matched V2/V3 cohort (`sor_compatible`) of the checked-in frozen
  corpus fixture `tests/fixtures/corpus/bundle` (Mantle block 101,082,044), ordered by
  ascending lowercase `pool_id` per family (A-1). Quotes come from the benchmark
  simulator per A-2 (`pools.quote.quote_exact_in`, chained per route, independently
  per entry, at `quotient(amount x p / 100)`); gas scores are `gross_only` zeros
  (A-3). Only the route list is hand-written; the harness fails if it disagrees with
  upstream enumeration.

Run from the repository root (the regeneration wrapper `regen.sh` does this):

    uv run python tools/upstream/uni_sor/author_inputs.py

Output is canonical JSON (sorted keys, 2-space indent, trailing newline), so the test
suite re-runs `build_inputs()` and requires byte-identical files.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:  # direct `python tools/upstream/uni_sor/author_inputs.py`
    sys.path.insert(0, str(REPO))

from pools.quote import quote_exact_in  # noqa: E402
from pools.result import QuoteStatus  # noqa: E402
from snapshot.bundle import load_bundle  # noqa: E402
from snapshot.models import (  # noqa: E402
    ConcentratedPoolState,
    ConstantProductPoolState,
    SnapshotBundle,
)

FIXTURES = REPO / "tests" / "fixtures" / "uni_sor"
CORPUS_BUNDLE = "tests/fixtures/corpus/bundle"
INPUT_SCHEMA = "uni-sor-golden-input/1"
INPUT_SUFFIX = ".input.json"

# Benchmark trial profile (docs/DESIGN.md §2.12): 5 % grid, 4 splits, 3 hops.
TRIAL_PROFILE = {"max_hops": 3, "percent_step": 5, "min_splits": 1, "max_splits": 4}

# A-4: the harness impersonates chain 1 with synthetic ERC-20 addresses (never mainnet
# WETH). Five lowercase-distinct synthetic tokens.
TOKENS = {
    sym: {"address": "0x" + ch * 40, "decimals": 18, "symbol": f"TK{sym}"}
    for sym, ch in (("A", "a"), ("B", "b"), ("C", "c"), ("D", "d"), ("E", "e"))
}

QuoteSpec = Mapping[int, int | None] | Callable[[int, int], int | None]
Json = dict[str, Any]


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def percent_grid(step: int) -> list[int]:
    """Only for authoring the table rows; the harness takes the grid from the actual
    `AlphaRouter.getAmountDistribution` and rejects rows that do not match it."""
    return [i * step for i in range(1, int(100 / step) + 1)]


# --- synthetic case builders -------------------------------------------------------


def v3(pool_id: str, a: str, b: str, fee: int = 500) -> Json:
    return {
        "pool_id": pool_id,
        "token0": TOKENS[a]["address"],
        "token1": TOKENS[b]["address"],
        "fee": fee,
    }


def v2(pool_id: str, a: str, b: str) -> Json:
    return {"pool_id": pool_id, "token0": TOKENS[a]["address"], "token1": TOKENS[b]["address"]}


def row(
    family: str,
    route: Sequence[str],
    percent: int,
    raw_quote: int | None,
    gas: tuple[int, int, int] = (0, 0, 0),
) -> Json:
    if raw_quote is None:
        return {
            "family": family,
            "route_pool_ids": list(route),
            "percent": percent,
            "raw_quote": None,
            "gas_estimate": None,
            "gas_cost_in_quote_token": None,
            "gas_cost_usd_raw": None,
        }
    return {
        "family": family,
        "route_pool_ids": list(route),
        "percent": percent,
        "raw_quote": str(raw_quote),
        "gas_estimate": str(gas[0]),
        "gas_cost_in_quote_token": str(gas[1]),
        "gas_cost_usd_raw": str(gas[2]),
    }


def table(
    family: str,
    route: Sequence[str],
    step: int,
    amount: int,
    quotes: QuoteSpec,
    gas: Callable[[int], tuple[int, int, int]] | None = None,
) -> list[Json]:
    """One row per grid percent. `quotes` maps percent -> raw quote, or is a function
    of (percent, integer input quotient). A zero integer input is always `null` (A-2)."""
    rows = []
    for p in percent_grid(step):
        q_in = amount * p // 100
        if q_in == 0:
            raw: int | None = None
        elif callable(quotes):
            raw = quotes(p, q_in)
        else:
            raw = quotes[p]
        rows.append(row(family, route, p, raw, gas(p) if gas and raw is not None else (0, 0, 0)))
    return rows


def cpmm(reserve_in: int, reserve_out: int, fee_ppm: int) -> Callable[[int, int], int]:
    """Integer constant-product output on the quoted integer input (synthetic depth)."""

    def f(_percent: int, x: int) -> int:
        x_fee = x * (1_000_000 - fee_ppm)
        return (reserve_out * x_fee) // (reserve_in * 1_000_000 + x_fee)

    return f


def case(
    case_id: str,
    *,
    categories: list[str],
    behaviours: list[str],
    description: str,
    naive_alternative: str,
    amount: int,
    token_in: str = "A",
    token_out: str = "B",
    v3_pools: list[Json] | None = None,
    v2_pools: list[Json] | None = None,
    routing: Mapping[str, int] | None = None,
    quotes: list[Json],
    expect: Json,
    gas_score_provider: str = "zero (gross_only)",
) -> Json:
    params = {"max_hops": 3, "percent_step": 50, "min_splits": 1, "max_splits": 2}
    params.update(routing or {})
    return {
        "schema": INPUT_SCHEMA,
        "case_id": case_id,
        "categories": categories,
        "behaviours": behaviours,
        "description": description,
        "naive_alternative": naive_alternative,
        "origin": {"kind": "synthetic"},
        "adaptations": ["A-2", "A-3", "A-4", "A-5", "A-6", "A-7", "A-8"],
        "chain_id": 1,
        "amount_in_raw": str(amount),
        "token_in": TOKENS[token_in]["address"],
        "token_out": TOKENS[token_out]["address"],
        "tokens": sorted(TOKENS.values(), key=lambda t: str(t["address"])),
        "pools": {"V3": v3_pools or [], "V2": v2_pools or []},
        "routing": params,
        "gas_score_provider": gas_score_provider,
        "quotes": quotes,
        "expect": expect,
    }


def sel(*legs: tuple[Sequence[str], int]) -> list[Json]:
    return [{"pool_ids": list(ids), "percent": p} for ids, p in legs]


def synthetic_cases() -> list[Json]:
    cases: list[Json] = []

    # ---- G-1 no route -------------------------------------------------------------
    cases.append(
        case(
            "g01_disconnected_tokens",
            categories=["G-1"],
            behaviours=["B-R1", "B-R7", "B-R8", "B-S10"],
            description="tokenIn and tokenOut are in different components; no family "
            "enumerates a route, the quote list is empty and upstream returns null.",
            naive_alternative="returning an empty plan or a partial route instead of null",
            amount=1_000,
            v3_pools=[v3("v3_ac", "A", "C"), v3("v3_db", "D", "B")],
            v2_pools=[v2("v2_ce", "C", "E")],
            quotes=[],
            expect={"result": None, "routes": {"V3": [], "V2": [], "MIXED": []}},
        )
    )
    null_routes = [("V3", ["v3_ab_500"]), ("V3", ["v3_ab_3000"]), ("V2", ["v2_ab"])]
    cases.append(
        case(
            "g01_all_quotes_null",
            categories=["G-1"],
            behaviours=["B-Q2", "B-S10"],
            description="Routes exist in two families but every (route, percent) quote is "
            "null; every entry is dropped and upstream returns null.",
            naive_alternative="treating a null quote as 0 and selecting a route",
            amount=1_000,
            v3_pools=[v3("v3_ab_500", "A", "B", 500), v3("v3_ab_3000", "A", "B", 3000)],
            v2_pools=[v2("v2_ab", "A", "B")],
            routing={"percent_step": 25},
            quotes=[
                r
                for fam, rt in null_routes
                for r in table(fam, rt, 25, 1_000, {p: None for p in percent_grid(25)})
            ],
            expect={
                "result": None,
                "routes": {"V3": [["v3_ab_500"], ["v3_ab_3000"]], "V2": [["v2_ab"]], "MIXED": []},
                "list_length": 0,
            },
        )
    )

    # ---- G-2 enumeration order and hop bound --------------------------------------
    g2_pools = [
        v3("ab500", "A", "B", 500),
        v3("ac", "A", "C"),
        v3("cd", "C", "D"),
        v3("de", "D", "E"),
        v3("eb", "E", "B"),
        v3("db", "D", "B"),
        v3("cb", "C", "B"),
        v3("ab3000", "A", "B", 3000),
    ]
    g2_quotes = {
        ("ab500",): {50: 60, 100: 100},
        ("ac", "cd", "db"): {50: 55, 100: 90},
        ("ac", "cb"): {50: 50, 100: 90},
        ("ab3000",): {50: 60, 100: 100},
    }
    cases.append(
        case(
            "g02_dfs_order_and_hop_bound",
            categories=["G-2", "G-12"],
            behaviours=["B-R1", "B-R2", "B-R5", "B-S2", "B-F1"],
            description="Pool-index DFS pre-order: the 3-hop A-C-D-B route precedes the "
            "2-hop A-C-B route, parallel A/B pools are separate routes in index order, "
            "and the 4-hop A-C-D-E-B path is cut by max_hops=3. The tied 50 % entries of "
            "the two parallel pools pair up and the final V8 sort reverses them.",
            naive_alternative="BFS/length-ordered enumeration ([ab500, ab3000, ac>cb, "
            "ac>cd>db]) or emitting the 4-hop path",
            amount=1_000,
            v3_pools=g2_pools,
            routing={"max_hops": 3, "percent_step": 50, "max_splits": 2},
            quotes=[r for rt, q in g2_quotes.items() for r in table("V3", rt, 50, 1_000, q)],
            expect={
                "routes": {
                    "V3": [["ab500"], ["ac", "cd", "db"], ["ac", "cb"], ["ab3000"]],
                    "V2": [],
                    "MIXED": [],
                },
                "selection": sel((["ab3000"], 50), (["ab500"], 50)),
                "naive_selection": sel((["ab500"], 50), (["ab3000"], 50)),
            },
        )
    )
    cases.append(
        case(
            "g02_hop_bound_two",
            categories=["G-2"],
            behaviours=["B-R1", "B-R2"],
            description="The same graph at max_hops=2: the 3-hop A-C-D-B route is no "
            "longer emitted (the bound check runs before the terminal check).",
            naive_alternative="an off-by-one hop bound that still emits ac>cd>db",
            amount=1_000,
            v3_pools=g2_pools,
            routing={"max_hops": 2, "percent_step": 100, "max_splits": 1},
            quotes=[
                r
                for rt, q in {
                    ("ab500",): {100: 100},
                    ("ac", "cb"): {100: 120},
                    ("ab3000",): {100: 99},
                }.items()
                for r in table("V3", rt, 100, 1_000, q)
            ],
            expect={
                "routes": {"V3": [["ab500"], ["ac", "cb"], ["ab3000"]], "V2": [], "MIXED": []},
                "selection": sel((["ac", "cb"], 100)),
            },
        )
    )

    # ---- G-3 terminal and cycle rules ---------------------------------------------
    g3_pools = [
        v3("ac", "A", "C"),
        v3("ca_alt", "C", "A", 3000),
        v3("cd", "C", "D"),
        v3("dc_alt", "D", "C", 3000),
        v3("db", "D", "B"),
        v3("ab", "A", "B"),
        v3("bc", "B", "C"),
        v3("cb", "C", "B", 3000),
        v3("be", "B", "E"),
    ]
    g3_routes = [
        ["ac", "cd", "db"],
        ["ac", "dc_alt", "db"],
        ["ac", "bc"],
        ["ac", "cb"],
        ["ca_alt", "cd", "db"],
        ["ca_alt", "dc_alt", "db"],
        ["ca_alt", "bc"],
        ["ca_alt", "cb"],
        ["ab"],
    ]
    g3_q = {tuple(r): {50: 40 + i, 100: 70 + i} for i, r in enumerate(g3_routes)}
    cases.append(
        case(
            "g03_terminal_and_cycle_rules",
            categories=["G-3", "G-2"],
            behaviours=["B-R1", "B-R3", "B-R4", "B-R5"],
            description="max_hops=4 so only the token rules bound the DFS: a path stops at "
            "the first pool touching tokenOut (no ab>be 'route' ending past B), tokenIn "
            "is never revisited (no ac>ca_alt>...), and an intermediate token is never "
            "revisited (no ac>cd>dc_alt>...).",
            naive_alternative="continuing the DFS past tokenOut (adds ab>be) or a visited "
            "set that omits tokenIn/intermediates (adds cyclic routes)",
            amount=1_000,
            v3_pools=g3_pools,
            routing={"max_hops": 4, "percent_step": 50, "max_splits": 2},
            quotes=[r for rt, q in g3_q.items() for r in table("V3", rt, 50, 1_000, q)],
            expect={
                "routes": {"V3": g3_routes, "V2": [], "MIXED": []},
                "selection": sel((["ca_alt", "cb"], 50), (["ab"], 50)),
            },
        )
    )

    # ---- G-4 mixed family ---------------------------------------------------------
    g4_v3 = [v3("v3_ac", "A", "C"), v3("v3_cb", "C", "B"), v3("v3_ab", "A", "B")]
    g4_v2 = [v2("v2_ac", "A", "C"), v2("v2_cb", "C", "B"), v2("v2_ab", "A", "B")]
    g4_routes = {
        "V3": [["v3_ac", "v3_cb"], ["v3_ab"]],
        "V2": [["v2_ac", "v2_cb"], ["v2_ab"]],
        "MIXED": [["v3_ac", "v2_cb"], ["v2_ac", "v3_cb"]],
    }

    def g4_table(values: Mapping[str, Sequence[int]]) -> list[Json]:
        out = []
        for fam, routes in g4_routes.items():
            for i, rt in enumerate(routes):
                out += table(fam, rt, 100, 1_000, {100: values[fam][i]})
        return out

    cases.append(
        case(
            "g04_list_order_v3_v2_mixed",
            categories=["G-4", "G-12"],
            behaviours=["B-R7", "B-R8", "B-Q1", "B-S2", "B-S3"],
            description="A V2 route and both mixed routes tie for the best 100 % quote; "
            "the quote list is V3 block, V2 block, MIXED block, and the stable "
            "per-percent sort keeps the V2 entry first.",
            naive_alternative="MIXED before V2 (e.g. alphabetical family order) selects a "
            "mixed route",
            amount=1_000,
            v3_pools=g4_v3,
            v2_pools=g4_v2,
            routing={"percent_step": 100, "max_splits": 1},
            quotes=g4_table({"V3": [90, 90], "V2": [90, 100], "MIXED": [100, 100]}),
            expect={
                "routes": g4_routes,
                "selection": sel((["v2_ab"], 100)),
                "naive_selection": sel((["v3_ac", "v2_cb"], 100)),
            },
        )
    )
    cases.append(
        case(
            "g04_mixed_candidate_order",
            categories=["G-4", "G-12"],
            behaviours=["B-R8", "B-Q1", "B-S2"],
            description="Both mixed routes tie for the best quote; mixed enumeration runs "
            "over V3 pools ++ V2 pools, so v3_ac>v2_cb is enumerated (and listed) first "
            "and wins the tie. Pure-V3/V2 paths are filtered out of the mixed list.",
            naive_alternative="V2 ++ V3 candidate order (v2_ac>v3_cb first), or keeping "
            "pure paths in the mixed list",
            amount=1_000,
            v3_pools=g4_v3,
            v2_pools=g4_v2,
            routing={"percent_step": 100, "max_splits": 1},
            quotes=g4_table({"V3": [90, 90], "V2": [90, 90], "MIXED": [100, 100]}),
            expect={
                "routes": g4_routes,
                "selection": sel((["v3_ac", "v2_cb"], 100)),
                "naive_selection": sel((["v2_ac", "v3_cb"], 100)),
            },
        )
    )

    # ---- G-5 competing percentages ------------------------------------------------
    g5_amount = 1_000_000
    g5 = {
        ("deep_1pct",): cpmm(4_000_000, 4_000_000, 10_000),
        ("mid_5bp",): cpmm(1_500_000, 1_500_000, 500),
        ("thin_1bp",): cpmm(700_000, 700_000, 100),
    }
    cases.append(
        case(
            "g05_competing_percentages",
            categories=["G-5"],
            behaviours=["B-A1", "B-S3", "B-S4", "B-S5", "B-S6", "B-S7", "B-S8"],
            description="Three direct pools of different depth and fee on a 10 % grid: the "
            "best single route, even splits and uneven splits compete and an uneven "
            "multi-way split wins, using a pool that is not the best at 100 %.",
            naive_alternative="the best 100 % route, or an even split",
            amount=g5_amount,
            v3_pools=[
                v3("deep_1pct", "A", "B", 10_000),
                v3("mid_5bp", "A", "B", 500),
                v3("thin_1bp", "A", "B", 100),
            ],
            routing={"percent_step": 10, "max_splits": 3},
            quotes=[r for rt, f in g5.items() for r in table("V3", rt, 10, g5_amount, f)],
            expect={
                # Pinned as observed from upstream at the pin; the properties are the
                # hand-checkable claims (uneven multi-way split beating every single route).
                "selection": sel((["deep_1pct"], 60), (["mid_5bp"], 30), (["thin_1bp"], 10)),
                "properties": {"min_routes": 2, "uneven": True, "beats_best_single": True},
            },
        )
    )

    # ---- two-hop overlap graph shared by G-6 / G-7 ---------------------------------
    ovl_pools = [
        v3("ab", "A", "B"),
        v3("ac1", "A", "C"),
        v3("ac2", "A", "C", 3000),
        v3("cb1", "C", "B"),
        v3("cb2", "C", "B", 3000),
    ]
    ovl_routes = [["ab"], ["ac1", "cb1"], ["ac1", "cb2"], ["ac2", "cb1"], ["ac2", "cb2"]]

    def ovl_quotes(values: Mapping[tuple[str, ...], Mapping[int, int]]) -> list[Json]:
        return [r for rt in ovl_routes for r in table("V3", rt, 50, 1_000, values[tuple(rt)])]

    cases.append(
        case(
            "g06_second_best_seed",
            categories=["G-6", "G-12"],
            behaviours=["B-S4", "B-S7", "B-S8", "B-S9", "B-F1"],
            description="At 50 % the best entry ac1>cb1 overlaps both other strong entries, "
            "so expanding it only reaches ac1>cb1 + ab (160 < 170 baseline). The optimum "
            "ac1>cb2 + ac2>cb1 (197) is only reachable from the second-best 50 % seed.",
            naive_alternative="seeding only the best entry per percent (keeps the 170 "
            "single-route baseline)",
            amount=1_000,
            v3_pools=ovl_pools,
            routing={"percent_step": 50, "max_splits": 3},
            quotes=ovl_quotes(
                {
                    ("ab",): {50: 60, 100: 100},
                    ("ac1", "cb1"): {50: 100, 100: 170},
                    ("ac1", "cb2"): {50: 99, 100: 160},
                    ("ac2", "cb1"): {50: 98, 100: 150},
                    ("ac2", "cb2"): {50: 50, 100: 90},
                }
            ),
            expect={
                "selection": sel((["ac2", "cb1"], 50), (["ac1", "cb2"], 50)),
                "naive_selection": sel((["ac1", "cb1"], 100)),
            },
        )
    )
    cases.append(
        case(
            "g07_greedy_first_non_overlapping",
            categories=["G-7", "G-12"],
            behaviours=["B-S7", "B-S9", "B-F1"],
            description="Expanding ac1>cb1 at 50 %: the next-best entry ac1>cb2 overlaps "
            "(shared ac1), so the first non-overlapping entry ac2>cb2 is taken (190). The "
            "better overlapping pair ac1>cb1 + ac1>cb2 (195) is excluded.",
            naive_alternative="allowing pool reuse across splits (ac1>cb1 + ac1>cb2)",
            amount=1_000,
            v3_pools=ovl_pools,
            routing={"percent_step": 50, "max_splits": 2},
            quotes=ovl_quotes(
                {
                    ("ab",): {50: 60, 100: 100},
                    ("ac1", "cb1"): {50: 100, 100: 150},
                    ("ac1", "cb2"): {50: 95, 100: 140},
                    ("ac2", "cb1"): {50: 20, 100: 40},
                    ("ac2", "cb2"): {50: 90, 100: 130},
                }
            ),
            expect={
                "selection": sel((["ac2", "cb2"], 50), (["ac1", "cb1"], 50)),
                "naive_selection": sel((["ac1", "cb2"], 50), (["ac1", "cb1"], 50)),
            },
        )
    )

    # ---- G-8 pool overlap ---------------------------------------------------------
    mid_routes = [
        ["ab"],
        ["ac1", "cd", "db1"],
        ["ac1", "cd", "db2"],
        ["ac2", "cd", "db1"],
        ["ac2", "cd", "db2"],
    ]
    mid_q = {
        ("ab",): {50: 40, 100: 70},
        ("ac1", "cd", "db1"): {50: 100, 100: 150},
        ("ac1", "cd", "db2"): {50: 97, 100: 130},
        ("ac2", "cd", "db1"): {50: 96, 100: 130},
        ("ac2", "cd", "db2"): {50: 99, 100: 140},
    }
    cases.append(
        case(
            "g08_shared_intermediate_pool",
            categories=["G-8"],
            behaviours=["B-Q5", "B-S9"],
            description="Every 3-hop path shares the middle pool cd, so no two of them may "
            "be combined; the best disjoint split (with ab) loses to the single route.",
            naive_alternative="checking only first/last hops for overlap (combines "
            "ac1>cd>db1 + ac2>cd>db2 = 199)",
            amount=1_000,
            v3_pools=[
                v3("ab", "A", "B"),
                v3("ac1", "A", "C"),
                v3("ac2", "A", "C", 3000),
                v3("cd", "C", "D"),
                v3("db1", "D", "B"),
                v3("db2", "D", "B", 3000),
            ],
            routing={"percent_step": 50, "max_splits": 2},
            quotes=[r for rt, q in mid_q.items() for r in table("V3", rt, 50, 1_000, q)],
            expect={
                "routes": {"V3": mid_routes, "V2": [], "MIXED": []},
                "selection": sel((["ac1", "cd", "db1"], 100)),
                "naive_selection": sel((["ac2", "cd", "db2"], 50), (["ac1", "cd", "db1"], 50)),
            },
        )
    )
    fh_pools = [
        v3("ac1", "A", "C"),
        v3("ac2", "A", "C", 3000),
        v3("cb1", "C", "B"),
        v3("cb2", "C", "B", 3000),
    ]
    fh_q = {
        ("ac1", "cb1"): {50: 100, 100: 150},
        ("ac1", "cb2"): {50: 99, 100: 140},
        ("ac2", "cb1"): {50: 98, 100: 140},
        ("ac2", "cb2"): {50: 97, 100: 130},
    }
    cases.append(
        case(
            "g08_shared_first_hop_vs_disjoint_same_token",
            categories=["G-8", "G-12"],
            behaviours=["B-S8", "B-S9", "B-F1"],
            description="ac1>cb2 shares the first hop and ac2>cb1 the last hop with the "
            "best entry ac1>cb1; both are excluded. ac2>cb2 goes through the same "
            "intermediate token C on disjoint pools and is allowed (197). The second seed "
            "finds an equal 197 later, which does not replace the first.",
            naive_alternative="token-level conflict (nothing through C twice: keeps the "
            "150 baseline) or first-hop reuse (ac1>cb1 + ac1>cb2 = 199)",
            amount=1_000,
            v3_pools=fh_pools,
            routing={"percent_step": 50, "max_splits": 2},
            quotes=[r for rt, q in fh_q.items() for r in table("V3", rt, 50, 1_000, q)],
            expect={
                "selection": sel((["ac2", "cb2"], 50), (["ac1", "cb1"], 50)),
                "naive_selection": sel((["ac1", "cb1"], 100)),
            },
        )
    )

    # ---- G-9 same (token0, token1, fee) on distinct physical pools -----------------
    same_key = [
        ("V3", ["agni_ab_500"], {50: 100, 100: 150}),
        ("V3", ["fusionx_ab_500"], {50: 99, 100: 149}),
        ("V2", ["moe_ab_1"], {50: 90, 100: 140}),
        ("V2", ["moe_ab_2"], {50: 89, 100: 139}),
    ]
    cases.append(
        case(
            "g09_same_key_physical_pools",
            categories=["G-9", "G-12"],
            behaviours=["B-Q5", "B-S9", "B-F1"],
            description="Two V3 pools share (token0, token1, fee=500) and two V2 pairs share "
            "a token pair, as Agni/FusionX/Uniswap v3 and Moe Classic can on Mantle. "
            "Under the route-scoped pool-id provider (A-5, D-2) they are distinct, so "
            "the two 500 pools are combined.",
            naive_alternative="upstream single-factory identity (CREATE2 address of "
            "(token0, token1, fee)) conflates them: agni + moe_ab_1 = 190",
            amount=1_000,
            v3_pools=[v3("agni_ab_500", "A", "B", 500), v3("fusionx_ab_500", "A", "B", 500)],
            v2_pools=[v2("moe_ab_1", "A", "B"), v2("moe_ab_2", "A", "B")],
            routing={"percent_step": 50, "max_splits": 2},
            quotes=[r for fam, rt, q in same_key for r in table(fam, rt, 50, 1_000, q)],
            expect={
                "selection": sel((["fusionx_ab_500"], 50), (["agni_ab_500"], 50)),
                "naive_selection": sel((["moe_ab_1"], 50), (["agni_ab_500"], 50)),
                "same_key_selected": True,
            },
        )
    )

    # ---- G-10 split bounds and pruning --------------------------------------------
    three = [v3("r1", "A", "B", 100), v3("r2", "A", "B", 500), v3("r3", "A", "B", 3000)]

    def three_table(step: int, values: Mapping[str, Mapping[int, int]], amount: int) -> list[Json]:
        return [
            r for pid in ("r1", "r2", "r3") for r in table("V3", [pid], step, amount, values[pid])
        ]

    q25 = {
        "r1": {25: 38, 50: 70, 75: 85, 100: 100},
        "r2": {25: 40, 50: 60, 75: 80, 100: 95},
        "r3": {25: 39, 50: 59, 75: 79, 100: 94},
    }
    cases.append(
        case(
            "g10_max_splits_one",
            categories=["G-10"],
            behaviours=["B-S3", "B-S5"],
            description="max_splits=1: the first layer check stops the BFS, so only the "
            "100 % baseline can win although a 2-way split (130) is better.",
            naive_alternative="treating max_splits as 'additional' splits (selects r1+r2)",
            amount=1_000,
            v3_pools=three,
            routing={"percent_step": 25, "max_splits": 1},
            quotes=three_table(25, q25, 1_000),
            expect={
                "selection": sel((["r1"], 100)),
                "naive_selection": sel((["r2"], 50), (["r1"], 50)),
            },
        )
    )
    cases.append(
        case(
            "g10_max_splits_two_blocks_three_way",
            categories=["G-10", "G-12"],
            behaviours=["B-S5", "B-S8", "B-F1"],
            description="The same quotes at max_splits=2: the best 2-way r1@50+r2@50 (130) "
            "is selected; the better 3-way (149, see g12_v8_three_way_trailing_equal) "
            "is cut by the split cap.",
            naive_alternative="ignoring the cap (3-way r1@50+r2@25+r3@25)",
            amount=1_000,
            v3_pools=three,
            routing={"percent_step": 25, "max_splits": 2},
            quotes=three_table(25, q25, 1_000),
            expect={
                "selection": sel((["r2"], 50), (["r1"], 50)),
                "naive_selection": sel((["r1"], 50), (["r3"], 25), (["r2"], 25)),
            },
        )
    )
    q20_prune = {
        "r1": {20: 30, 40: 40, 60: 50, 80: 55, 100: 100},
        "r2": {20: 29, 40: 39, 60: 49, 80: 54, 100: 99},
        "r3": {20: 28, 40: 38, 60: 48, 80: 53, 100: 98},
    }
    cases.append(
        case(
            "g10_pruning_blocks_better_three_way",
            categories=["G-10"],
            behaviours=["B-S5"],
            description="No 2-way split beats the 100 % baseline (best 89 < 100), so at "
            "splits=3 the rule 'bestSwap.length < splits - 1' stops the search although "
            "3-way splits reach 107. max_splits=4 is not the binding limit.",
            naive_alternative="searching every layer up to max_splits (a 107 3-way)",
            amount=1_000,
            v3_pools=three,
            routing={"percent_step": 20, "max_splits": 4},
            quotes=three_table(20, q20_prune, 1_000),
            expect={"selection": sel((["r1"], 100)), "naive_min_routes": 3},
        )
    )
    cases.append(
        case(
            "g10_min_splits_two_suppresses_better_single",
            categories=["G-10", "G-12"],
            behaviours=["B-S3", "B-S8", "B-F1"],
            description="min_splits=2 removes the 100 % baseline although r1@100 (200) is "
            "far better than any split; the best 2-way is selected.",
            naive_alternative="keeping the baseline (r1@100)",
            amount=1_000,
            v3_pools=three[:2],
            routing={"percent_step": 50, "min_splits": 2, "max_splits": 2},
            quotes=[
                r
                for pid, q in (("r1", {50: 60, 100: 200}), ("r2", {50: 59, 100: 90}))
                for r in table("V3", [pid], 50, 1_000, q)
            ],
            expect={
                "selection": sel((["r2"], 50), (["r1"], 50)),
                "naive_selection": sel((["r1"], 100)),
            },
        )
    )
    q25_min3 = {
        "r1": {25: 30, 50: 70, 75: 80, 100: 100},
        "r2": {25: 29, 50: 69, 75: 79, 100: 99},
        "r3": {25: 28, 50: 68, 75: 78, 100: 98},
    }
    cases.append(
        case(
            "g10_min_splits_three",
            categories=["G-10", "G-12"],
            behaviours=["B-S3", "B-S5", "B-S8", "B-F1"],
            description="min_splits=3: complete 2-way candidates (best 139) are enqueued "
            "instead of accepted, pruning is inactive while no best exists, and the best "
            "3-way (127) is selected.",
            naive_alternative="accepting 2-way completions (r1@50+r2@50 = 139)",
            amount=1_000,
            v3_pools=three,
            routing={"percent_step": 25, "min_splits": 3, "max_splits": 4},
            quotes=three_table(25, q25_min3, 1_000),
            expect={
                "selection": sel((["r1"], 50), (["r3"], 25), (["r2"], 25)),
                "naive_selection": sel((["r2"], 50), (["r1"], 50)),
            },
        )
    )
    cases.append(
        case(
            "g10_min_splits_unsatisfiable",
            categories=["G-10", "G-1"],
            behaviours=["B-S3", "B-S10"],
            description="min_splits=2 with a single route: no split can be pool-disjoint, "
            "no baseline exists, and upstream returns null.",
            naive_alternative="falling back to the single route",
            amount=1_000,
            v3_pools=[v3("r1", "A", "B")],
            routing={"percent_step": 50, "min_splits": 2, "max_splits": 3},
            quotes=table("V3", ["r1"], 50, 1_000, {50: 60, 100: 100}),
            expect={"result": None, "naive_selection": sel((["r1"], 100))},
        )
    )

    # ---- G-11 nondivisible integer inputs -----------------------------------------
    cases.append(
        case(
            "g11_101_wei_even_split",
            categories=["G-11", "G-12"],
            behaviours=["B-A1", "B-F1", "B-F2"],
            description="101 wei at 50/50: each route keeps the exact rational 5050/100; "
            "upstream's missing amount is 0 and the integer quotients sum to 100 < 101.",
            naive_alternative="flooring the grid amounts (missing 1 added to a route) or "
            "reporting a filled input",
            amount=101,
            v3_pools=three[:2],
            routing={"percent_step": 50, "max_splits": 2},
            quotes=[
                r
                for pid, q in (("r1", {50: 60, 100: 100}), ("r2", {50: 59, 100: 99}))
                for r in table("V3", [pid], 50, 101, q)
            ],
            expect={
                "selection": sel((["r2"], 50), (["r1"], 50)),
                "sum_of_quotients": "100",
                "missing_amount_zero": True,
            },
        )
    )
    tiny = cpmm(10, 1_000, 3_000)
    cases.append(
        case(
            "g11_7_wei_5pct_grid",
            categories=["G-11", "G-14", "G-12"],
            behaviours=["B-A1", "B-Q2", "B-S8", "B-F1", "B-F2"],
            description="7 wei on the 5 % grid: 5 % and 10 % have integer input 0 and are "
            "null (A-2). Two identical pools make many splits tie at 460 (45/55, 50/50, "
            "55/45); seeds run from the highest percent down, so 55/45 is found first and "
            "kept. Quotients 3 + 3 = 6 < 7, missing amount 0.",
            naive_alternative="preferring the even 50/50 split among ties, or filling the "
            "residual upstream",
            amount=7,
            v3_pools=three[:2],
            routing=TRIAL_PROFILE,
            quotes=[r for pid in ("r1", "r2") for r in table("V3", [pid], 5, 7, tiny)],
            expect={
                "selection": sel((["r1"], 55), (["r2"], 45)),
                "naive_selection": sel((["r2"], 50), (["r1"], 50)),
                "sum_of_quotients": "6",
                "missing_amount_zero": True,
                "absent_percents": [5, 10],
            },
        )
    )
    res_curve = {10: 20, 20: 38, 30: 54, 40: 68, 50: 80, 60: 90, 70: 98, 80: 104, 90: 108, 100: 110}
    cases.append(
        case(
            "g11_three_way_residual_two",
            categories=["G-11"],
            behaviours=["B-A1", "B-F2"],
            description="1,000,003 on a 10 % grid split three ways: the per-route integer "
            "quotients leave a residual of 2 that upstream never assigns (its rational "
            "amounts sum exactly to the input).",
            naive_alternative="adding the residual to the last route upstream",
            amount=1_000_003,
            v3_pools=three,
            routing={"percent_step": 10, "max_splits": 3},
            quotes=[
                r
                for i, pid in enumerate(("r1", "r2", "r3"))
                for r in table("V3", [pid], 10, 1_000_003, {p: v - i for p, v in res_curve.items()})
            ],
            expect={"properties": {"routes": 3, "residual": 2}, "missing_amount_zero": True},
        )
    )

    # ---- G-12 ties ----------------------------------------------------------------
    cases.append(
        case(
            "g12_stable_tie_within_percent",
            categories=["G-12"],
            behaviours=["B-S2", "B-S3"],
            description="Three parallel pools, listed z, y, x, tie at 100 %. The "
            "per-percent sort is stable, so the list-first entry (z) is the baseline.",
            naive_alternative="an unstable sort or ordering by pool id (selects x)",
            amount=1_000,
            v3_pools=[v3("ab_z", "A", "B", 100), v3("ab_y", "A", "B", 500), v3("ab_x", "A", "B")],
            routing={"percent_step": 100, "max_splits": 1},
            quotes=[
                r
                for pid in ("ab_z", "ab_y", "ab_x")
                for r in table("V3", [pid], 100, 1_000, {100: 7})
            ],
            expect={
                "selection": sel((["ab_z"], 100)),
                "naive_selection": sel((["ab_x"], 100)),
                "sorted_by_percent": [{"percent": 100, "list_indices": [0, 1, 2]}],
            },
        )
    )
    cases.append(
        case(
            "g12_baseline_wins_equal_split",
            categories=["G-12"],
            behaviours=["B-S3", "B-S8"],
            description="The best 50/50 split totals exactly the 100 % baseline (200). "
            "Completion requires a strictly greater total, so the baseline stays.",
            naive_alternative="'>=' replacement (selects the split)",
            amount=1_000,
            v3_pools=three[:2],
            routing={"percent_step": 50, "max_splits": 2},
            quotes=[
                r
                for pid, q in (("r1", {50: 100, 100: 200}), ("r2", {50: 100, 100: 150}))
                for r in table("V3", [pid], 50, 1_000, q)
            ],
            expect={
                "selection": sel((["r1"], 100)),
                "naive_selection": sel((["r2"], 50), (["r1"], 50)),
            },
        )
    )
    cases.append(
        case(
            "g12_first_found_equal_totals",
            categories=["G-12"],
            behaviours=["B-S4", "B-S8", "B-F1"],
            description="r1@50 + r2@50 (203) is found from the best seed as [r1, r2] and "
            "again from the second seed as [r2, r1]; the first stays, and the final V8 "
            "sort reverses its equal amounts to [r2, r1].",
            naive_alternative="last-found wins, or a stable final sort (both give [r1, r2])",
            amount=1_000,
            v3_pools=three[:2],
            routing={"percent_step": 50, "max_splits": 2},
            quotes=[
                r
                for pid, q in (("r1", {50: 102, 100: 150}), ("r2", {50: 101, 100: 140}))
                for r in table("V3", [pid], 50, 1_000, q)
            ],
            expect={
                "selection": sel((["r2"], 50), (["r1"], 50)),
                "naive_selection": sel((["r1"], 50), (["r2"], 50)),
            },
        )
    )
    cases.append(
        case(
            "g12_v8_three_way_trailing_equal",
            categories=["G-12", "G-10"],
            behaviours=["B-S5", "B-S8", "B-F1"],
            description="Selection found as [r1@50, r2@25, r3@25] (149). V8's run detection "
            "stops at the equal trailing pair and binary insertion places r3 before r2: "
            "final order [r1, r3, r2].",
            naive_alternative="a stable descending sort ([r1, r2, r3])",
            amount=1_000,
            v3_pools=three,
            routing={"percent_step": 25, "max_splits": 3},
            quotes=three_table(25, q25, 1_000),
            expect={
                "selection": sel((["r1"], 50), (["r3"], 25), (["r2"], 25)),
                "naive_selection": sel((["r1"], 50), (["r2"], 25), (["r3"], 25)),
            },
        )
    )
    q20_lead = {
        "r1": {20: 25, 40: 50, 60: 62, 80: 70, 100: 75},
        "r2": {20: 24, 40: 49, 60: 61, 80: 69, 100: 74},
        "r3": {20: 30, 40: 40, 60: 55, 80: 60, 100: 65},
    }
    cases.append(
        case(
            "g12_v8_three_way_leading_equal",
            categories=["G-12"],
            behaviours=["B-S8", "B-F1"],
            description="Selection found as [r1@40, r2@40, r3@20] (129). The equal leading "
            "pair is a 'strictly descending' run for the inconsistent comparator and is "
            "reversed: final order [r2, r1, r3].",
            naive_alternative="a stable descending sort ([r1, r2, r3])",
            amount=1_000,
            v3_pools=three,
            routing={"percent_step": 20, "max_splits": 3},
            quotes=three_table(20, q20_lead, 1_000),
            expect={
                "selection": sel((["r2"], 40), (["r1"], 40), (["r3"], 20)),
                "naive_selection": sel((["r1"], 40), (["r2"], 40), (["r3"], 20)),
            },
        )
    )
    four = three + [v3("r4", "A", "B", 10_000)]
    q25_four = {
        "r1": {25: 40, 50: 65, 75: 80, 100: 90},
        "r2": {25: 39, 50: 64, 75: 79, 100: 89},
        "r3": {25: 38, 50: 63, 75: 78, 100: 88},
        "r4": {25: 37, 50: 62, 75: 77, 100: 87},
    }
    cases.append(
        case(
            "g12_v8_four_way_all_equal",
            categories=["G-12", "G-10"],
            behaviours=["B-S5", "B-S8", "B-F1"],
            description="Each layer improves (129 -> 142 -> 154), so a 4-way 25 % split "
            "found as [r1, r2, r3, r4] wins; four equal amounts form one descending run "
            "and are fully reversed.",
            naive_alternative="a stable descending sort ([r1, r2, r3, r4])",
            amount=1_000,
            v3_pools=four,
            routing={"percent_step": 25, "max_splits": 4},
            quotes=[
                r
                for pid in ("r1", "r2", "r3", "r4")
                for r in table("V3", [pid], 25, 1_000, q25_four[pid])
            ],
            expect={
                "selection": sel((["r4"], 25), (["r3"], 25), (["r2"], 25), (["r1"], 25)),
                "naive_selection": sel((["r1"], 25), (["r2"], 25), (["r3"], 25), (["r4"], 25)),
            },
        )
    )

    # ---- G-13 gas score effects ---------------------------------------------------
    cases.append(
        case(
            "g13_gas_flips_winner",
            categories=["G-13", "G-4"],
            behaviours=["B-Q4", "B-S2", "B-S11"],
            description="The V3 route has the higher raw quote (1000) but a gas cost of 300 "
            "output units; the V2 route (900 - 100 = 800) wins on quoteAdjustedForGas.",
            naive_alternative="selecting on raw quote (the V3 route)",
            amount=1_000,
            v3_pools=[v3("v3_ab", "A", "B")],
            v2_pools=[v2("v2_ab", "A", "B")],
            routing={"percent_step": 50, "max_splits": 2},
            gas_score_provider="synthetic table (abstract A-3 scores)",
            quotes=table(
                "V3", ["v3_ab"], 50, 1_000, {50: 600, 100: 1_000}, lambda p: (150_000, 300, 10**15)
            )
            + table(
                "V2", ["v2_ab"], 50, 1_000, {50: 520, 100: 900}, lambda p: (90_000, 100, 10**14)
            ),
            expect={
                "selection": sel((["v2_ab"], 100)),
                "naive_selection": sel((["v3_ab"], 100)),
            },
        )
    )
    cases.append(
        case(
            "g13_negative_adjusted_quote",
            categories=["G-13"],
            behaviours=["B-Q4", "B-S2", "B-S8", "B-S11"],
            description="Every gas-adjusted quote is negative; they still compare as exact "
            "integers and the least negative single route (-10) wins over a -84 split.",
            naive_alternative="dropping non-positive adjusted quotes (null) or selecting on "
            "raw quote (p1)",
            amount=1_000,
            v3_pools=[v3("p1", "A", "B"), v3("p2", "A", "B", 3000)],
            routing={"percent_step": 50, "max_splits": 2},
            gas_score_provider="synthetic table (abstract A-3 scores)",
            quotes=table("V3", ["p1"], 50, 1_000, {50: 30, 100: 50}, lambda p: (200_000, 100, 0))
            + table("V3", ["p2"], 50, 1_000, {50: 6, 100: 10}, lambda p: (60_000, 20, 0)),
            expect={
                "selection": sel((["p2"], 100)),
                "naive_selection": sel((["p1"], 100)),
                "quote_gas_adjusted": "-10",
            },
        )
    )
    cases.append(
        case(
            "g13_zero_quote_kept",
            categories=["G-13"],
            behaviours=["B-Q2", "B-S3"],
            description="The only quotes are raw 0 (a V3 route and a V2 route). A BigNumber "
            "zero is truthy, so both entries stay in the list and the V3 one is selected "
            "with quote 0.",
            naive_alternative="treating a zero quote as no quote (null result)",
            amount=1_000,
            v3_pools=[v3("v3_ab", "A", "B")],
            v2_pools=[v2("v2_ab", "A", "B")],
            routing={"percent_step": 100, "max_splits": 1},
            quotes=table("V3", ["v3_ab"], 100, 1_000, {100: 0})
            + table("V2", ["v2_ab"], 100, 1_000, {100: 0}),
            expect={"selection": sel((["v3_ab"], 100)), "list_length": 2, "quote": "0"},
        )
    )

    # ---- G-14 grid edges ----------------------------------------------------------
    edge = cpmm(50, 5_000, 3_000)
    cases.append(
        case(
            "g14_zero_quotient_percent_dropped",
            categories=["G-14", "G-11"],
            behaviours=["B-A1", "B-Q2", "B-Q3"],
            description="19 wei on the 5 % grid: only the 5 % entry has integer input 0; it "
            "is null and absent from the quote list, while 10 % (input 1) stays.",
            naive_alternative="quoting the 0 input (a kept zero-quote entry at 5 %)",
            amount=19,
            v3_pools=[v3("r1", "A", "B"), v3("r2", "A", "B", 3000)],
            routing=TRIAL_PROFILE,
            quotes=[r for pid in ("r1", "r2") for r in table("V3", [pid], 5, 19, edge)],
            expect={"absent_percents": [5], "properties": {"min_routes": 1}},
        )
    )
    cases.append(
        case(
            "g14_step_not_dividing_100",
            categories=["G-14", "G-1"],
            behaviours=["B-A1", "B-S3", "B-S6", "B-S8", "B-S10"],
            description="percent_step=30 gives the grid 30/60/90 with no 100 % key: no "
            "baseline, and no multiset of multiples of 30 sums to 100, so no candidate "
            "ever completes and upstream returns null despite valid quotes.",
            naive_alternative="appending a 100 % entry or rescaling the grid",
            amount=1_000,
            v3_pools=[v3("r1", "A", "B"), v3("r2", "A", "B", 3000)],
            routing={"percent_step": 30, "max_splits": 4},
            quotes=[
                r
                for pid in ("r1", "r2")
                for r in table("V3", [pid], 30, 1_000, {30: 40, 60: 70, 90: 90})
            ],
            expect={"result": None, "percents": [30, 60, 90], "list_length": 6},
        )
    )
    return cases


# --- corpus-shaped cases -----------------------------------------------------------


def _family(state: object) -> str:
    if isinstance(state, ConcentratedPoolState):
        return "V3"
    if isinstance(state, ConstantProductPoolState):
        return "V2"
    raise ValueError(f"not a V2/V3 pool: {type(state).__name__}")


def simulate_route(
    bundle: SnapshotBundle, pool_ids: Sequence[str], token_in: str, amount: int
) -> int | None:
    """A-2 quote-table entry: chain `quote_exact_in` through the route's pools from the
    frozen state, independently per entry. `null` when the integer input is 0, when a
    hop fails, or (pure-V2 routes) when a hop outputs 0. A hop that needs state the
    snapshot lacks is an error, never a silent `null`."""
    if amount == 0:
        return None
    pure_v2 = all(_family(bundle.pools[pid]) == "V2" for pid in pool_ids)
    token, x = token_in, amount
    for pid in pool_ids:
        state = bundle.pools[pid]
        res = quote_exact_in(state, token, x)
        if res.status is QuoteStatus.INCOMPLETE_SNAPSHOT:
            raise RuntimeError(f"{pid}: incomplete snapshot for {x} {token}: {res.detail}")
        if res.status is not QuoteStatus.OK:
            return None
        if res.amount_in_consumed != x:
            raise RuntimeError(f"{pid}: partial fill {res.amount_in_consumed} of {x}")
        token = state.token1 if token == state.token0 else state.token0
        x = res.amount_out
        if pure_v2 and x == 0:
            return None
    return x


def corpus_case(
    bundle: SnapshotBundle,
    case_id: str,
    corpus_case_id: str,
    *,
    categories: list[str],
    behaviours: list[str],
    description: str,
    naive_alternative: str,
    routes: Mapping[str, list[list[str]]],
    expect: Json,
) -> Json:
    corpus = bundle.corpus
    assert corpus is not None and bundle.prices is not None
    (req,) = [c for c in bundle.cases if c.case_id == corpus_case_id]
    cohort = list(corpus["cohorts"]["sor_compatible"]["pools"])
    pools: dict[str, list[Json]] = {"V3": [], "V2": []}
    for pid in sorted(cohort):  # A-1: ascending lowercase pool_id per family
        st = bundle.pools[pid]
        fam = _family(st)
        entry: Json = {"pool_id": pid, "token0": st.token0, "token1": st.token1}
        if isinstance(st, ConcentratedPoolState):
            entry["fee"] = st.fee
        pools[fam].append(entry)
    tokens = sorted(
        {
            addr
            for fam in pools.values()
            for p in fam
            for addr in (str(p["token0"]), str(p["token1"]))
        }
    )
    price_tokens = bundle.prices.tokens
    token_table = [
        {
            "address": t,
            "decimals": price_tokens[t].decimals,
            "symbol": price_tokens[t].symbol or t[:8],
        }
        for t in tokens
    ]
    params = dict(TRIAL_PROFILE)
    quotes: list[Json] = []
    for fam in ("V3", "V2", "MIXED"):
        for rt in routes[fam]:
            for p in percent_grid(params["percent_step"]):
                raw = simulate_route(bundle, rt, req.token_in, req.amount_in * p // 100)
                quotes.append(row(fam, rt, p, raw))
    return {
        "schema": INPUT_SCHEMA,
        "case_id": case_id,
        "categories": categories,
        "behaviours": behaviours,
        "description": description,
        "naive_alternative": naive_alternative,
        "origin": {
            "kind": "corpus",
            "bundle": CORPUS_BUNDLE,
            "bundle_id": bundle.bundle_id,
            "bundle_hash": bundle.bundle_hash,
            "block": {
                "chain_id": bundle.block.chain_id,
                "number": bundle.block.number,
                "hash": bundle.block.hash,
            },
            "corpus_case_id": corpus_case_id,
            "cohort": "sor_compatible",
            "quote_source": "pools.quote.quote_exact_in chained per route on the frozen "
            "bundle state, independently per (route, percent) at quotient(amount*p/100) "
            "(contract A-2)",
            "note": "chain id and addresses are Mantle facts; the harness still runs as "
            "chain 1 (A-4)",
        },
        "adaptations": ["A-1", "A-2", "A-3", "A-4", "A-5", "A-6", "A-7", "A-8"],
        "chain_id": 1,
        "amount_in_raw": str(req.amount_in),
        "token_in": req.token_in,
        "token_out": req.token_out,
        "tokens": token_table,
        "pools": pools,
        "routing": params,
        "gas_score_provider": "zero (gross_only)",
        "quotes": quotes,
        "expect": {"routes": dict(routes), **expect},
    }


def corpus_cases() -> list[Json]:
    bundle = load_bundle(REPO / CORPUS_BUNDLE)
    return [
        corpus_case(
            bundle,
            "c01_corpus_usdc_fbtc_mixed",
            "nod-09bc4e-c96de2-medium-2",
            categories=["G-4", "G-2"],
            behaviours=["B-R1", "B-R7", "B-R8", "B-Q1", "B-Q2"],
            description="USDC -> FBTC (no direct pool) over the full 12-pool sor_compatible "
            "cohort at the trial profile: one V3 path via USDT0, no V2 path (no V2 FBTC "
            "pool), one mixed V2+V3 path, dead-end branches through WMNT pools pruned.",
            naive_alternative="treating a missing direct pool as no route, or dropping the "
            "mixed family",
            routes={
                "V3": [
                    [
                        "0x36f66548cda219c6fc037037cee063b9f28b13ef",
                        "0x9608fecddbeea59cff57f61353f6654962500566",
                    ]
                ],
                "V2": [],
                "MIXED": [
                    [
                        "0x69a707d8deb5423865d84b46b2915bb72f2844d0",
                        "0x9608fecddbeea59cff57f61353f6654962500566",
                    ]
                ],
            },
            expect={
                "selection": [
                    {
                        "pool_ids": [
                            "0x36f66548cda219c6fc037037cee063b9f28b13ef",
                            "0x9608fecddbeea59cff57f61353f6654962500566",
                        ],
                        "percent": 100,
                    }
                ]
            },
        ),
        corpus_case(
            bundle,
            "c02_corpus_meth_weth_parallel_pools",
            "emp-cda86a-deadde-large-4",
            categories=["G-2", "G-1"],
            behaviours=["B-R1", "B-R5", "B-Q1", "B-Q2", "B-S3", "B-S8"],
            description="mETH -> WETH over the full cohort at the trial profile: six "
            "single-hop V3 pools, three sharing (mETH, WETH, fee 100) and two sharing fee "
            "500 across Agni/FusionX/Uniswap, enumerated as distinct routes in pool-id "
            "order, plus one Moe Classic pair. Two V3 pools have no usable liquidity and "
            "every one of their entries is null-dropped. At this block the deepest pool "
            "0x94d6 beats every split, so the 100 % baseline stands.",
            naive_alternative="merging same-(token0, token1, fee) pools into one route, or "
            "keeping null entries in the list",
            routes={
                "V3": [
                    ["0x283fdeeac018e403a9274fbdec3c570dd3157e68"],
                    ["0x48ef5640e71001cac842f5627a0bfec1ef09deb7"],
                    ["0x4f9e3683a523b66da89d82bba0a9caa1c3243df4"],
                    ["0x78b54c2bc2c5ad8f744915d481e5caaff0c6c8c2"],
                    ["0x8a6a1ed01989ff1c5ac6361c34cad9d7d0015ab4"],
                    ["0x94d692afff21e045691971a20df4a9adb482ddf6"],
                ],
                "V2": [["0x86e3a987187fed135d6d9c114f1857d8144f01e1"]],
                "MIXED": [],
            },
            expect={
                "selection": [
                    {"pool_ids": ["0x94d692afff21e045691971a20df4a9adb482ddf6"], "percent": 100}
                ],
                "absent_routes": [
                    ["0x283fdeeac018e403a9274fbdec3c570dd3157e68"],
                    ["0x78b54c2bc2c5ad8f744915d481e5caaff0c6c8c2"],
                ],
            },
        ),
    ]


def build_inputs() -> dict[str, str]:
    """case_id -> canonical input JSON text."""
    out: dict[str, str] = {}
    for c in synthetic_cases() + corpus_cases():
        cid = str(c["case_id"])
        if cid in out:
            raise ValueError(f"duplicate case id {cid}")
        out[cid] = canonical_json(c)
    return out


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    inputs = build_inputs()
    for stale in FIXTURES.glob("*" + INPUT_SUFFIX):
        if stale.name.removesuffix(INPUT_SUFFIX) not in inputs:
            stale.unlink()
    for cid, text in inputs.items():
        (FIXTURES / f"{cid}{INPUT_SUFFIX}").write_text(text, encoding="utf-8")
    print(f"wrote {len(inputs)} inputs to {FIXTURES.relative_to(REPO)}")


if __name__ == "__main__":
    main()

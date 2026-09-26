"""Selection parity of `uni_sor_port` against the actual pinned Uniswap SOR (WHI-1444).

Every golden in `tests/fixtures/uni_sor/` was produced by the real
`@uniswap/smart-order-router@4.31.10` functions (WHI-1443). For **every** case -- none is
skipped; the upstream runs were byte-identical twice, so no nondeterminism needed
canonicalizing (contract §7.6/§7.7) -- the port is fed the identical ordered candidate
pools, percentage grid, (route, percent) quote table and abstract gas scores, and must
reproduce (contract §8, I18 checks 1-3):

1. the enumerated routes per family, in upstream order, with token paths (B-R*);
2. the grid amounts as unreduced rationals (B-A1);
3. the ordered quote list with list indices, quotes, gas and adjusted quotes (B-Q*);
4. the per-percent sorted groups of the `getBestSwapRouteBy` diagnostic (B-S2);
5. the selection *before* D-1: the same routes in the same final order, percents,
   rational amounts, cached quotes, missing amount, remainder flag, sum of quotients
   and totals (B-S*, B-F1, B-F2), or `null` (B-S10).

Separately (check 4): the D-1 integer fill conserves the input on every golden
selection, and on the corpus-derived goldens the benchmark adapter itself rebuilds the
identical quote table from the frozen bundle, reproduces the golden selection end to
end, and its final plan passes the independent evaluator (D-3), with the re-quote
difference reported apart from selection parity.
"""

from __future__ import annotations

import functools
import json
import random
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from benchmark.objective import gross_only
from pools import concentrated
from pools.cl_math import TickMathReuse
from routing.algorithms import uni_sor_port as sor
from routing.algorithms.base import AlgorithmConfig, Budget, SolveContext, SolveStatus
from routing.evaluator import EvalStatus, evaluate
from snapshot.bundle import load_bundle

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "uni_sor"
CORPUS_BUNDLE = REPO / "tests" / "fixtures" / "corpus" / "bundle"

Json = dict[str, Any]


@cache
def _load(name: str) -> Json:
    data: Json = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def _case_ids() -> list[str]:
    return [str(c["case_id"]) for c in _load("MANIFEST.json")["cases"]]


CASES = _case_ids()


def _pools(inp: Json, family: str) -> list[sor.SorPool]:
    return [
        sor.SorPool(
            str(p["pool_id"]),
            str(p["token0"]).lower(),
            str(p["token1"]).lower(),
            family,
            p.get("fee"),
        )
        for p in inp["pools"][family]
    ]


class _Table:
    """The input's frozen (route, percent) table as the port's quote/gas providers, with
    the harness's own completeness rules: every enumerated (route, percent) has a row and
    every row is consumed (contract §7.4)."""

    def __init__(self, inp: Json) -> None:
        self.rows: dict[tuple[str, tuple[str, ...], int], Json] = {}
        for row in inp["quotes"]:
            key = (str(row["family"]), tuple(row["route_pool_ids"]), int(row["percent"]))
            assert key not in self.rows, key
            self.rows[key] = row
        self.used: set[tuple[str, tuple[str, ...], int]] = set()

    def _row(self, route: sor.SorRoute, percent: int) -> Json:
        key = (route.protocol, route.pool_ids, percent)
        assert key in self.rows, f"enumerated entry {key} missing from the table"
        self.used.add(key)
        return self.rows[key]

    def quote(self, route: sor.SorRoute, percent: int, _amount: sor.Rational) -> int | None:
        raw = self._row(route, percent)["raw_quote"]
        return None if raw is None else int(raw)

    def gas(self, route: sor.SorRoute, percent: int, _a: sor.Rational, _q: int) -> sor.GasScore:
        row = self._row(route, percent)
        return sor.GasScore(
            int(row["gas_estimate"]),
            int(row["gas_cost_in_quote_token"]),
            int(row["gas_cost_usd_raw"]),
        )


def _port(inp: Json) -> dict[str, Any]:
    """Run the translated routing core on a golden input exactly as the harness ran
    upstream (contract §7.2)."""
    r = inp["routing"]
    amount = int(inp["amount_in_raw"])
    v3, v2 = _pools(inp, "V3"), _pools(inp, "V2")
    routes = {
        fam: sor.compute_family_routes(
            fam, inp["token_in"], inp["token_out"], v3, v2, int(r["max_hops"])
        )
        for fam in sor.FAMILIES
    }
    percents, amounts = sor.amount_distribution(amount, int(r["percent_step"]))
    table = _Table(inp)
    quotes = sor.build_route_quotes(routes, percents, amounts, table.quote, table.gas)
    assert table.used == set(table.rows), "a table row was never consumed"
    selection = sor.get_best_swap_route(
        amount,
        percents,
        quotes,
        min_splits=int(r["min_splits"]),
        max_splits=int(r["max_splits"]),
    )
    return {
        "amount": amount,
        "routes": routes,
        "percents": percents,
        "amounts": amounts,
        "quotes": quotes,
        "selection": selection,
    }


# --- 1-5: exact selection parity on every golden ---------------------------------------


def test_every_manifest_case_is_exercised() -> None:
    goldens = sorted(p.name.removesuffix(".golden.json") for p in FIXTURES.glob("*.golden.json"))
    assert CASES == goldens and len(CASES) == 35
    categories = {c for e in _load("MANIFEST.json")["cases"] for c in e["categories"]}
    assert categories == {f"G-{i}" for i in range(1, 15)}


@pytest.mark.parametrize("case_id", CASES)
def test_enumerated_routes_match_upstream(case_id: str) -> None:
    golden = _load(f"{case_id}.golden.json")
    got = _port(_load(f"{case_id}.input.json"))["routes"]
    for fam in sor.FAMILIES:
        assert [
            {"pool_ids": list(rt.pool_ids), "token_path": list(rt.token_path)} for rt in got[fam]
        ] == golden["routes"][fam], fam


@pytest.mark.parametrize("case_id", CASES)
def test_amount_grid_matches_upstream(case_id: str) -> None:
    golden = _load(f"{case_id}.golden.json")
    run = _port(_load(f"{case_id}.input.json"))
    assert run["percents"] == golden["percents"]
    assert [
        {"percent": p, **a.to_dict()} for p, a in zip(run["percents"], run["amounts"], strict=True)
    ] == golden["amounts"]


@pytest.mark.parametrize("case_id", CASES)
def test_quote_list_matches_upstream(case_id: str) -> None:
    golden = _load(f"{case_id}.golden.json")
    run = _port(_load(f"{case_id}.input.json"))
    assert [q.to_dict() for q in run["quotes"]] == golden["routes_with_valid_quotes"]


@pytest.mark.parametrize("case_id", CASES)
def test_per_percent_sorted_groups_match_upstream(case_id: str) -> None:
    golden = _load(f"{case_id}.golden.json")
    run = _port(_load(f"{case_id}.input.json"))
    groups: dict[int, list[sor.RouteQuote]] = {}
    for q in run["quotes"]:
        groups.setdefault(q.percent, []).append(q)
    r = golden["routing"]
    swap = sor.get_best_swap_route_by(
        groups,
        run["percents"],
        min_splits=int(r["min_splits"]),
        max_splits=int(r["max_splits"]),
    )
    got_groups = (
        swap.sorted_by_percent
        if swap is not None
        else {
            p: tuple(sorted(g, key=lambda q: -q.quote_adjusted_for_gas)) for p, g in groups.items()
        }
    )
    assert [
        {"percent": p, "list_indices": [q.list_index for q in got_groups[p]]}
        for p in run["percents"]
        if p in got_groups
    ] == golden["diagnostic"]["sorted_by_percent"]


@pytest.mark.parametrize("case_id", CASES)
def test_selection_before_integer_fill_matches_upstream(case_id: str) -> None:
    golden = _load(f"{case_id}.golden.json")
    selection = _port(_load(f"{case_id}.input.json"))["selection"]
    if golden["result"] is None:
        assert selection is None
        return
    assert selection is not None
    assert selection.to_dict() == golden["result"]


# --- D-1: integer fill conservation on every golden selection --------------------------


@pytest.mark.parametrize("case_id", CASES)
def test_integer_fill_allocates_the_whole_input(case_id: str) -> None:
    run = _port(_load(f"{case_id}.input.json"))
    selection = run["selection"]
    if selection is None:
        return
    amount = run["amount"]
    alloc = sor.integer_fill(amount, selection)
    k = len(alloc)
    assert sum(alloc) == amount and all(a >= 0 for a in alloc)
    assert list(alloc[:-1]) == [a.quotient for a in selection.amounts[:-1]]
    residual = alloc[-1] - selection.amounts[-1].quotient
    assert 0 <= residual <= k - 1
    assert residual == amount - selection.sum_of_quotients
    # Upstream never fills the input on an exact grid (B-F2); D-1 is the port's own step.
    assert not selection.remainder_added and selection.missing_amount.numerator == 0


def test_nondivisible_inputs_exercise_the_d1_residual() -> None:
    """G-11: 101 wei at 50/50 -> upstream quotients 50 + 50 = 100; D-1 gives the last
    route (the B-F1-final one, `r1` after V8's reversal) the missing unit; a 3-way
    selection has residual 2."""
    run = _port(_load("g11_101_wei_even_split.input.json"))
    sel = run["selection"]
    assert [q.pool_identifiers for q in sel.routes] == [("r2",), ("r1",)]
    assert sor.integer_fill(101, sel) == (50, 51)
    three = _port(_load("g11_three_way_residual_two.input.json"))
    alloc = sor.integer_fill(three["amount"], three["selection"])
    assert alloc[-1] - three["selection"].amounts[-1].quotient == 2


# --- behaviours whose naive alternative must fail --------------------------------------


def test_v8_final_order_is_not_pythons_sort_contract() -> None:
    """B-F1: an inconsistent comparator on equal amounts reverses a leading equal run;
    a naive "keep discovery order" or `sorted(key=-amount)` would keep `[X, Y]`."""
    a = sor.Rational(50, 1)

    def cmp(x: tuple[str, sor.Rational], y: tuple[str, sor.Rational]) -> int:
        return 1 if y[1].greater_than(x[1]) else -1

    items = [("X", a), ("Y", a)]
    assert [n for n, _ in sor.v8_small_array_sort(items, cmp)] == ["Y", "X"]
    assert [n for n, _ in sorted(items, key=lambda t: -t[1].numerator)] == ["X", "Y"]
    with pytest.raises(ValueError, match="shorter than 64"):
        sor.v8_small_array_sort([("x", a)] * 64, cmp)


def test_v8_sort_matches_a_reference_model_on_random_arrays() -> None:
    """Independent check of the translation: for a *consistent* comparator TimSort is a
    stable sort; for upstream's tie-inconsistent comparator the result is still a
    permutation ordered by amount descending."""
    rng = random.Random(1444)
    for _ in range(2000):
        n = rng.randint(0, 63)
        items = [(i, rng.randint(0, 4)) for i in range(n)]
        stable = sor.v8_small_array_sort(items, lambda x, y: -1 if x[1] > y[1] else 1)
        assert stable == sorted(items, key=lambda t: -t[1])
        consistent = sor.v8_small_array_sort(items, lambda x, y: (y[1] > x[1]) - (y[1] < x[1]))
        assert consistent == sorted(items, key=lambda t: -t[1])
        upstream = sor.v8_small_array_sort(items, lambda x, y: 1 if y[1] > x[1] else -1)
        assert sorted(upstream) == sorted(items)
        assert [v for _, v in upstream] == sorted((v for _, v in items), reverse=True)


def test_boundary_flags_outside_the_parity_boundary_are_rejected() -> None:
    with pytest.raises(ValueError, match="parity boundary"):
        sor.get_best_swap_route(1, [100], [], max_splits=1, force_cross_protocol=True)
    with pytest.raises(ValueError, match="1..63"):
        sor.get_best_swap_route(1, [100], [], max_splits=64)


def _exhaustive_best(
    groups: Any, percents: Any, *, min_splits: int, max_splits: int, **_: Any
) -> sor.BestSwap | None:
    """A generic "SOR-inspired" optimizer: the gross-best pool-disjoint combination of
    the same quote list (no seeds, no greedy first-non-overlap, no pruning)."""
    quotes = sorted((q for g in groups.values() for q in g), key=lambda q: q.list_index)
    best: tuple[int, tuple[sor.RouteQuote, ...]] | None = None

    def search(chosen: tuple[sor.RouteQuote, ...], rem: int, start: int) -> None:
        nonlocal best
        if rem == 0:
            total = sum(q.quote_adjusted_for_gas for q in chosen)
            if len(chosen) >= min_splits and (best is None or total > best[0]):
                best = (total, chosen)
            return
        if len(chosen) == max_splits:
            return
        used = {p for q in chosen for p in q.pool_identifiers}
        for i in range(start, len(quotes)):
            q = quotes[i]
            if q.percent <= rem and used.isdisjoint(q.pool_identifiers):
                search((*chosen, q), rem - q.percent, i + 1)

    search((), 100, 0)
    if best is None:
        return None
    routes = tuple(sorted(best[1], key=lambda q: -q.percent))
    return sor.BestSwap(routes, 0, best[0], 0, 0, 0, {})


def _stable_final_order(items: Any, _cmp: Any) -> list[Any]:
    return sorted(items, key=lambda r: -r.percent)


def _selection_shape(selection: sor.SwapSelection | None) -> Any:
    if selection is None:
        return None
    return [(list(r.pool_identifiers), r.percent) for r in selection.routes]


@pytest.mark.parametrize(
    ("mutation", "target", "replacement"),
    [
        ("generic exhaustive split optimizer", "get_best_swap_route_by", _exhaustive_best),
        ("stable final route order instead of V8", "v8_small_array_sort", _stable_final_order),
    ],
)
def test_a_generic_or_mutated_port_fails_parity(
    mutation: str, target: str, replacement: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parity is evidence only if a plausible non-SOR implementation fails it: each
    mutation reproduces at least one golden selection wrongly, while the real port
    passes every golden (above)."""
    monkeypatch.setattr(sor, target, replacement)
    failing = []
    for case_id in CASES:
        golden = _load(f"{case_id}.golden.json")["result"]
        got = _selection_shape(_port(_load(f"{case_id}.input.json"))["selection"])
        want = None if golden is None else [(r["pool_ids"], r["percent"]) for r in golden["routes"]]
        if got != want:
            failing.append(case_id)
    assert failing, f"{mutation} passed every golden"


def test_same_key_physical_pools_are_disjoint_under_pool_id_identity() -> None:
    """D-2 / G-9: the golden selects two physical pools sharing `(token0, token1, fee)`;
    upstream's single-factory CREATE2 identity would have conflated them into one."""
    inp = _load("g09_same_key_physical_pools.input.json")
    port = _port(inp)["selection"]
    ids = [r.pool_identifiers for r in port.routes]
    assert len(ids) == 2 and set(ids[0]).isdisjoint(ids[1])
    keyed = {
        p["pool_id"]: f"{p['token0']}/{p['token1']}/{p.get('fee')}"
        for fam in ("V3", "V2")
        for p in inp["pools"][fam]
    }
    assert keyed[ids[0][0]] == keyed[ids[1][0]], "the golden selects two same-key pools"


# --- D-3 and the adapter, end to end on the corpus-derived goldens ---------------------


def _corpus_context() -> tuple[Any, sor.PreparedUniSorPort]:
    bundle = load_bundle(CORPUS_BUNDLE)
    prepared = sor.prepare(
        bundle,
        AlgorithmConfig(sor.NAME, {"max_hops": 3, "max_splits": 4, "percent_step": 5}),
    )
    return bundle, prepared


@pytest.mark.parametrize(
    "case_id", ["c01_corpus_usdc_fbtc_mixed", "c02_corpus_meth_weth_parallel_pools"]
)
def test_adapter_rebuilds_the_golden_table_and_selection_from_the_bundle(case_id: str) -> None:
    inp = _load(f"{case_id}.input.json")
    golden = _load(f"{case_id}.golden.json")
    bundle, prepared = _corpus_context()
    assert inp["routing"] == {"max_hops": 3, "max_splits": 4, "min_splits": 1, "percent_step": 5}
    # A-1: the adapter's cohort lists are the golden input's candidate lists.
    for fam, pools in (("V3", prepared.v3_pools), ("V2", prepared.v2_pools)):
        assert [p.pool_id for p in pools] == [p["pool_id"] for p in inp["pools"][fam]]
    case = bundle.case(inp["origin"]["corpus_case_id"])
    context = SolveContext(bundle=bundle, objective=gross_only(), prepared=prepared)
    res = sor.solve(case, context, Budget())
    assert res.status is SolveStatus.OK, res.error
    stats = res.search_stats
    # A-2: the adapter's simulator table equals the frozen table, entry for entry.
    assert stats["routes_enumerated"] == {f: len(golden["routes"][f]) for f in sor.FAMILIES}
    assert stats["route_quotes"] == len(golden["routes_with_valid_quotes"])
    assert stats["quote_entries"] == len(inp["quotes"])
    assert stats["quote_entries_null"] == sum(1 for q in inp["quotes"] if q["raw_quote"] is None)
    # Selection parity end to end (zero gas scores == the golden's gross-only table).
    assert stats["selection"] == golden["result"]
    assert stats["coverage_mode"] == "full_universe"
    assert stats["excluded_pools"] == {"moe_lb_v2_2": 7}
    # D-1 + D-3: the executable plan and its independent re-quote.
    assert res.plan is not None and res.evaluation is not None
    independent = evaluate(bundle, case, res.plan, gross_only())
    assert independent.status is EvalStatus.OK
    assert independent.gross_output == res.evaluation.gross_output
    alloc = [int(a) for a in stats["allocation"]]
    assert sum(alloc) == case.amount_in
    assert int(stats["requote_delta"]) == independent.gross_output - int(golden["result"]["quote"])
    # Single-route selections carry no residual: the re-quote equals the cached quote.
    assert stats["d1_residual"] == "0" and stats["requote_delta"] == "0"


def test_adapter_quote_rows_equal_the_frozen_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A-2 row by row (not only counts): the adapter's quote provider on the corpus
    bundle returns each frozen `raw_quote` of both corpus inputs."""
    bundle, prepared = _corpus_context()
    captured: dict[tuple[str, tuple[str, ...], int], int | None] = {}
    original = sor.build_route_quotes

    def spy(routes: Any, percents: Any, amounts: Any, quote: sor.QuoteFn, gas: Any = None) -> Any:
        def wrapped(route: sor.SorRoute, percent: int, amount: sor.Rational) -> int | None:
            raw = quote(route, percent, amount)
            captured[(route.protocol, route.pool_ids, percent)] = raw
            return raw

        return original(routes, percents, amounts, wrapped, gas)

    monkeypatch.setattr(sor, "build_route_quotes", spy)
    for case_id in ("c01_corpus_usdc_fbtc_mixed", "c02_corpus_meth_weth_parallel_pools"):
        inp = _load(f"{case_id}.input.json")
        case = bundle.case(inp["origin"]["corpus_case_id"])
        captured.clear()
        context = SolveContext(bundle=bundle, objective=gross_only(), prepared=prepared)
        assert sor.solve(case, context, Budget()).status is SolveStatus.OK
        expected = {
            (q["family"], tuple(q["route_pool_ids"]), int(q["percent"])): (
                None if q["raw_quote"] is None else int(q["raw_quote"])
            )
            for q in inp["quotes"]
        }
        assert captured == expected, case_id


@pytest.mark.parametrize("l02_l03", [False, True], ids=["prefix_only", "l02_l03_prefix"])
def test_cl_prefix_reuse_rebuilds_the_golden_rows_and_selection(
    l02_l03: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WHI-1506 / L04 (explicit, default-off): with CL traversal-prefix reuse selected --
    alone, and on top of explicitly selected L02+L03 -- the port still produces every
    frozen `raw_quote`, the golden selection and a plan whose independent evaluation
    (after the variant is removed: the ordinary reference path) matches its own."""
    bundle, prepared = _corpus_context()
    captured: dict[tuple[str, tuple[str, ...], int], int | None] = {}
    original = sor.build_route_quotes

    def spy(routes: Any, percents: Any, amounts: Any, quote: sor.QuoteFn, gas: Any = None) -> Any:
        def wrapped(route: sor.SorRoute, percent: int, amount: sor.Rational) -> int | None:
            raw = quote(route, percent, amount)
            captured[(route.protocol, route.pool_ids, percent)] = raw
            return raw

        return original(routes, percents, amounts, wrapped, gas)

    resumed = 0
    for case_id in ("c01_corpus_usdc_fbtc_mixed", "c02_corpus_meth_weth_parallel_pools"):
        inp, golden = _load(f"{case_id}.input.json"), _load(f"{case_id}.golden.json")
        case = bundle.case(inp["origin"]["corpus_case_id"])
        captured.clear()
        reuse = concentrated.CLPrefixReuse(max_keys=4096, max_checkpoints=1_000_000)
        variant = functools.partial(
            concentrated.swap,
            skip_empty_spans=l02_l03,
            math_reuse=TickMathReuse(16384) if l02_l03 else None,
            prefix_reuse=reuse,
        )
        context = SolveContext(bundle=bundle, objective=gross_only(), prepared=prepared)
        with monkeypatch.context() as patch:
            patch.setattr(concentrated, "swap", variant)
            patch.setattr(sor, "build_route_quotes", spy)
            res = sor.solve(case, context, Budget())
        assert res.status is SolveStatus.OK, res.error
        expected = {
            (q["family"], tuple(q["route_pool_ids"]), int(q["percent"])): (
                None if q["raw_quote"] is None else int(q["raw_quote"])
            )
            for q in inp["quotes"]
        }
        assert captured == expected, case_id
        assert res.search_stats["selection"] == golden["result"]
        assert res.plan is not None and res.evaluation is not None
        independent = evaluate(bundle, case, res.plan, gross_only())
        assert independent.status is EvalStatus.OK
        assert independent.gross_output == res.evaluation.gross_output
        resumed += reuse.stats()["resumed"]
    # c01's CL quotes all end inside their first step; c02's cross many boundaries.
    assert resumed > 50

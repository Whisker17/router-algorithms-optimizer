"""`uni_sor_port` as a benchmark participant (WHI-1444): cohort selection, statuses,
D-1 integer fill, D-3 re-quote, budgets, registration and the isolated runner.

Selection parity against the actual upstream goldens is `test_uni_sor_parity.py`; this
file checks the adapter around the translated core. Expected CPMM outputs are the
Solidity `getAmountOut` formula computed here, independent of the code under test.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import pytest

from benchmark.objective import gross_only, synthetic_fixed_cost
from benchmark.profile import parse_profile
from pools.quote import metered_quotes
from routing.algorithms import uni_sor_port as sor
from routing.algorithms.base import AlgorithmConfig, Budget, SolveContext, SolveResult, SolveStatus
from routing.algorithms.registry import ALGORITHMS
from routing.evaluator import EvalStatus, evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID
from snapshot.bundle import load_bundle
from snapshot.models import BlockRef, Case, ConstantProductPoolState, PoolState, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)
CONFIG = {"max_hops": 3, "max_splits": 4, "percent_step": 5}


@cache
def _load(rel: str) -> SnapshotBundle:
    return load_bundle(REPO / rel)


def _bundle(*pools: PoolState) -> SnapshotBundle:
    return SnapshotBundle(
        bundle_id="t",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools={p.pool_id: p for p in pools},
        cases=(),
        bundle_hash="deadbeef",
        source_path="<test>",
    )


def _cp(pool_id: str, t0: str, t1: str, r0: int, r1: int) -> ConstantProductPoolState:
    return ConstantProductPoolState(pool_id, t0, t1, r0, r1, fee_bps=30)


def _out(x: int, r_in: int, r_out: int) -> int:
    fee_in = x * 9970
    return fee_in * r_out // (r_in * 10000 + fee_in)


def _solve(
    bundle: SnapshotBundle,
    case: Case,
    params: dict[str, int] | None = None,
    *,
    budget: Budget | None = None,
    objective: Any = None,
) -> SolveResult:
    prepared = sor.prepare(bundle, AlgorithmConfig(sor.NAME, params or CONFIG))
    context = SolveContext(bundle=bundle, objective=objective or gross_only(), prepared=prepared)
    return sor.solve(case, context, budget or Budget())


TWIN = _bundle(_cp("p1", "A", "B", 10**6, 10**6), _cp("p2", "A", "B", 10**6, 10**6))


# ------------------------------------------------------------------- D-1 / D-3


def test_nondivisible_split_fills_the_input_and_reports_the_requote_delta() -> None:
    """101 wei, 50 % grid, two identical pools: SOR splits 50/50 (quotients 50 + 50),
    V8 orders the tied routes `[p2, p1]`, and D-1 gives the last route (`p1`) the
    missing unit as `ALL_REMAINING`. The evaluator's re-quote exceeds the cached quote
    by exactly the residual's output."""
    case = Case("c", "A", "B", 101)
    twin = _bundle(_cp("p1", "A", "B", 1000, 1000), _cp("p2", "A", "B", 1000, 1000))
    res = _solve(twin, case, {"max_hops": 1, "max_splits": 2, "percent_step": 50})
    assert res.status is SolveStatus.OK, res.error
    s = res.search_stats
    sel = s["selection"]
    assert [(r["pool_ids"], r["percent"]) for r in sel["routes"]] == [(["p2"], 50), (["p1"], 50)]
    assert [r["amount"] for r in sel["routes"]] == [
        {"numerator": "5050", "denominator": "100", "quotient": "50"}
    ] * 2
    assert sel["missing_amount"]["numerator"] == "0" and sel["sum_of_quotients"] == "100"
    assert s["allocation"] == ["50", "51"] and s["d1_residual"] == "1"
    assert res.plan is not None
    first, last = res.plan.steps
    assert (first.pool_id, first.inputs[0].fund_id, first.inputs[0].amount) == (
        "p2",
        REQUEST_FUND_ID,
        50,
    )
    assert (last.pool_id, last.inputs[0].amount) == ("p1", ALL_REMAINING)
    ev = evaluate(twin, case, res.plan, gross_only())
    assert ev.status is EvalStatus.OK
    cached = 2 * _out(50, 1000, 1000)
    assert cached > _out(101, 1000, 1000)  # why SOR splits
    assert int(sel["quote"]) == cached
    assert ev.gross_output == _out(50, 1000, 1000) + _out(51, 1000, 1000)
    assert int(s["requote_delta"]) == ev.gross_output - cached == 1
    assert res.score == ev.gross_output


def test_multi_hop_plan_chains_all_remaining_and_conserves_the_input() -> None:
    bundle = _bundle(
        _cp("ab", "A", "B", 10**9, 10**9),
        _cp("ac", "A", "C", 10**9, 10**9),
        _cp("cb", "C", "B", 10**9, 10**9),
    )
    case = Case("c", "A", "B", 3 * 10**8 + 7)
    res = _solve(bundle, case)
    assert res.status is SolveStatus.OK and res.evaluation is not None
    s = res.search_stats
    assert s["routes_enumerated"] == {"V3": 0, "V2": 2, "MIXED": 0}
    assert sum(int(a) for a in s["allocation"]) == case.amount_in
    for step in res.plan.steps if res.plan else ():
        if step.inputs[0].fund_id != REQUEST_FUND_ID:
            assert step.inputs[0].amount == ALL_REMAINING
    request_spent = sum(
        t.amount_in for t in res.evaluation.trace if t.inputs[0][0] == REQUEST_FUND_ID
    )
    assert request_spent == case.amount_in and not any(res.evaluation.residuals.values())


# ------------------------------------------------------------------- statuses


def test_lb_only_case_is_unsupported_never_no_route() -> None:
    """D-4 / contract §6: every pool of the Merchant Moe LB bundle is outside the SOR
    cohort, so the cohort DFS is empty while the full-universe DFS is not."""
    bundle = _load("tests/fixtures/moe_lb/bundle")
    res = _solve(bundle, bundle.cases[0])
    assert res.status is SolveStatus.UNSUPPORTED
    assert res.plan is None and res.error is not None and "SOR protocol" in res.error
    assert res.search_stats["coverage_mode"] == "full_universe"
    assert res.search_stats["cohort_pools"] == {"V3": 0, "V2": 0}
    assert res.search_stats["universe_routes"] > 0


def test_disconnected_tokens_are_no_route() -> None:
    res = _solve(TWIN, Case("c", "A", "Z", 10**6))
    assert res.status is SolveStatus.NO_ROUTE
    assert res.search_stats["universe_routes"] == 0


def test_all_null_quotes_are_no_route_b_s10() -> None:
    """Every entry is null (a dust input floors to zero at every percent)."""
    res = _solve(TWIN, Case("c", "A", "B", 1), {"max_hops": 1, "max_splits": 2, "percent_step": 50})
    assert res.status is SolveStatus.NO_ROUTE
    assert res.search_stats["route_quotes"] == 0
    assert res.search_stats["entry_failures"]


def test_matched_cohort_mode_on_a_pure_v2_v3_bundle() -> None:
    res = _solve(TWIN, Case("c", "A", "B", 10**6))
    assert res.search_stats["coverage_mode"] == "matched_cohort"
    assert res.search_stats["excluded_pools"] == {}


def test_full_universe_mixed_bundle_excludes_lb_pools_and_matches_the_cohort_bundle() -> None:
    """On a bundle with LB pools SOR's candidates are exactly the V2/V3 pools: the same
    case on the bundle with the LB pools removed gives the identical plan."""
    full = _load("tests/fixtures/routing/mantle_mixed")
    sor_by_source = sor._sor_by_source(sor.DEFAULT_CATALOG)
    lb = {pid for pid, p in full.pools.items() if sor.sor_protocol_of(p, sor_by_source) is None}
    assert lb, "the mixed fixture holds LB pools"
    cohort = _bundle(*(p for pid, p in full.pools.items() if pid not in lb))
    for case in full.cases:
        a, b = _solve(full, case), _solve(cohort, case)
        assert a.search_stats["coverage_mode"] == "full_universe"
        assert b.search_stats["coverage_mode"] == "matched_cohort"
        assert a.status == b.status and a.plan == b.plan
        assert a.search_stats["selection"] == b.search_stats["selection"]


def test_synthetic_fixed_cost_uses_zero_route_gas_and_the_same_selection() -> None:
    case = Case("c", "A", "B", 10**6)
    gross = _solve(TWIN, case)
    fixed = _solve(TWIN, case, objective=synthetic_fixed_cost(1000))
    assert gross.search_stats["selection"] == fixed.search_stats["selection"]
    assert fixed.score is not None and gross.score is not None
    assert fixed.score == gross.score - 1000


# ------------------------------------------------------------------- budgets


def test_quote_budget_below_the_table_is_timeout_before_the_meter() -> None:
    case = Case("c", "A", "B", 10**6)
    full = _solve(TWIN, case)
    limit = full.search_stats["quotes_executed"] - 1
    with metered_quotes(limit) as meter:
        res = _solve(TWIN, case, budget=Budget(max_quotes=limit))
    assert not meter.exceeded
    assert res.status is SolveStatus.TIMEOUT and res.search_stats["truncated_by"] == "max_quotes"
    assert res.error is not None and "not evidence of no_route" in res.error


def test_candidate_cap_below_the_route_count_is_declared_timeout() -> None:
    res = _solve(TWIN, Case("c", "A", "B", 10**6), budget=Budget(max_candidates=1))
    assert res.status is SolveStatus.TIMEOUT
    assert res.search_stats["truncated_by"] == "max_candidates"
    assert res.candidates_truncated == 2


def test_quote_accounting_matches_the_meter() -> None:
    with metered_quotes(None) as meter:
        res = _solve(TWIN, Case("c", "A", "B", 10**6))
    assert res.search_stats["quotes_executed"] == meter.counted


# ------------------------------------------------------------------- config / registry


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"max_hops": 3, "max_splits": 4},
        {"max_hops": 3, "max_splits": 64, "percent_step": 5},
        {"max_hops": 3, "max_splits": 4, "percent_step": 3},
        {"max_hops": 0, "max_splits": 4, "percent_step": 5},
    ],
)
def test_prepare_rejects_missing_or_invalid_settings(params: dict[str, Any]) -> None:
    with pytest.raises(sor.UniSorPortConfigError):
        sor.prepare(TWIN, AlgorithmConfig(sor.NAME, params))


def test_solve_without_prepare_is_an_error() -> None:
    with pytest.raises(TypeError, match="prepare"):
        sor.solve(Case("c", "A", "B", 1), SolveContext(TWIN, gross_only()), Budget())


def test_corpus_cohort_is_the_catalog_sor_protocol_cohort() -> None:
    bundle = _load("tests/fixtures/corpus/bundle")
    prepared = sor.prepare(bundle, AlgorithmConfig(sor.NAME, CONFIG))
    ids = [p.pool_id for p in (*prepared.v3_pools, *prepared.v2_pools)]
    assert sorted(ids) == bundle.corpus["cohorts"]["sor_compatible"]["pools"]  # type: ignore[index]
    for pools in (prepared.v3_pools, prepared.v2_pools):
        assert [p.pool_id for p in pools] == sorted(p.pool_id for p in pools)


def test_registered_with_v2_v3_capability_and_provenance() -> None:
    factory = ALGORITHMS["uni_sor_port"]
    assert factory is sor.FACTORY
    assert factory.capabilities.to_dict() == {
        "multi_hop": True,
        "split": True,
        "shared_pools": False,
        "protocols": ["V2", "V3"],
    }
    assert factory.search_params == ("max_hops", "max_splits", "percent_step")
    assert factory.provenance is not None
    assert factory.provenance["upstream"]["commit"] == "04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647"


def test_port_file_retains_the_required_notices() -> None:
    """Contract §9.3: SPDX, translation pin, GPL copy, modification statement and the
    V8/PSF notice, plus per-function source mapping comments."""
    text = (REPO / "routing" / "algorithms" / "uni_sor_port.py").read_text()
    head = text[:2500]
    for needle in (
        "SPDX-License-Identifier: GPL-3.0-only",
        "Translated from Uniswap/smart-order-router@"
        "04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647 (4.31.10)",
        "docs/references/licenses/uniswap-smart-order-router-04c7c0b4-LICENSE.txt",
        "MODIFIED: translated from TypeScript to Python",
        "V8 13.6.233.17",
        "third_party/v8/builtins/array-sort.tq",
        "Copyright Python Software Foundation; PSF-2.0",
        "docs/references/licenses/v8-13.6.233.17-third_party-builtins-LICENSE.txt",
    ):
        assert needle in head, needle
    for mapping in (
        "compute-all-routes.ts:161-271",
        "compute-all-routes.ts:52-86",
        "alpha-router.ts:3348-3362",
        "best-swap-route.ts:174-817",
        "best-swap-route.ts:821-881",
        "best-swap-route.ts:42-172",
    ):
        assert mapping in text, mapping


PROFILE: dict[str, Any] = {
    "schema_version": 2,
    "algorithms": ["direct", "path_split", "uni_sor_port"],
    "objective": {"mode": "gross_only"},
    "search": CONFIG,
    "budget": {"time_limit_seconds": 60, "max_quotes": 50000, "max_candidates": None},
    "measurement": {"warmup": 0, "repeats": 1, "seed": 1, "order": "fixed", "memory_pass": False},
    "worker": {"start_method": "spawn", "scope": "algorithm", "prepare_time_limit_seconds": 60},
}


def test_isolated_runner_records_the_port_with_its_provenance(tmp_path: Path) -> None:
    import json

    from benchmark.results import load_case_records
    from benchmark.runner import run_experiment

    bundle = _load("tests/fixtures/routing/mantle_mixed")
    profile = parse_profile(PROFILE, "p.yaml")
    manifest = run_experiment(bundle, profile, results_dir=tmp_path, replay_command="cmd")
    assert manifest.complete
    run = json.loads((Path(manifest.run_dir) / "manifest.json").read_text())
    text = json.dumps(run)
    assert "04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647" in text and "A-5" in text
    records = {(r["algorithm"], r["case_id"]): r for r in load_case_records(manifest.run_dir)}
    for case in bundle.cases:
        rec = records[("uni_sor_port", case.case_id)]
        assert rec["status"] in ("ok", "no_route", "unsupported"), rec["error"]
        assert rec["search"]["sor_port"]["upstream_commit"].startswith("04c7c0b4")
        if rec["status"] == "ok":
            assert rec["quotes"]["counted"] == rec["search"]["quotes_executed"]
            assert rec["search"]["evaluated_gross"] == rec["evaluation"]["gross_output"]


# ------------------------------------------------------------------- matched cohort cut


def test_matched_cohort_bundle_shares_sor_candidates_for_every_algorithm(tmp_path: Path) -> None:
    """`main.py corpus cohort`: the corpus's sor_compatible pools and every case; the
    port's selection is identical on the full and the cut bundle (its candidates never
    included LB), and the cut declares itself a cohort subset."""
    from snapshot.corpus import CorpusError, sor_cohort_bundle, validate_corpus_bundle

    full = _load("tests/fixtures/corpus/bundle")
    cut = sor_cohort_bundle(full, tmp_path / "cohort")
    validate_corpus_bundle(load_bundle(tmp_path / "cohort"))
    assert full.corpus is not None and cut.corpus is not None
    assert sorted(cut.pools) == full.corpus["cohorts"]["sor_compatible"]["pools"]
    assert [c.case_id for c in cut.cases] == [c.case_id for c in full.cases]
    assert cut.corpus["cohort"] == "sor_compatible"
    assert cut.corpus["subset_of"]["bundle_hash"] == full.bundle_hash
    for case in full.cases[::12]:
        a, b = _solve(full, case), _solve(cut, case)
        assert (a.status, a.plan) == (b.status, b.plan)
        assert b.search_stats["coverage_mode"] == "matched_cohort"
    # A cohort marker cannot hide a missing SOR source.
    import json

    doc = json.loads((tmp_path / "cohort" / "corpus.json").read_text())
    from snapshot.corpus import parse_corpus_document

    doc["cohorts"]["sor_compatible"]["sources"] = ["agni_v3", "fusionx_v3", "uniswap_v3"]
    with pytest.raises(CorpusError, match="cohort"):
        parse_corpus_document(
            doc, block=cut.block, pools=cut.pools, cases=cut.cases, prices=cut.prices
        )


def test_recorded_pin_agrees_with_the_source_inventory() -> None:
    import json

    inv = json.loads((REPO / "docs/references/uni-sor-source-inventory.json").read_text())
    assert sor.UPSTREAM["commit"] == inv["upstream"]["commit"]
    assert sor.UPSTREAM["version"] == inv["upstream"]["package_version"]
    assert sor.UPSTREAM["npm_integrity"] == inv["npm_artifact"]["integrity"]
    assert sor.CONTRACT == inv["contract"]

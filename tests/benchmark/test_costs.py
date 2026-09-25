"""Empirical execution-cost model (WHI-1445; docs/DESIGN.md §2.9,
docs/references/cost-model.md). Offline: every input is a checked-in file
(`tests/fixtures/costs`, `config/costs`, the corpus fixture bundle).

Expected values come from sources independent of the code under test: the public RPC's
receipts and sender balance drops captured in `fee_evidence.json` (the chain's own
accounting), hand-built transaction rows, and literal pinned identities.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from benchmark.costs import (
    LOW_CONFIDENCE,
    SUPPORTED,
    UNKNOWN_PRICE,
    UNSUPPORTED,
    CostModel,
    CostModelError,
    assign_split,
    canonical_model_bytes,
    fit_cost_model,
    load_cost_model,
    parse_cost_model,
    plan_cohort,
    receipts_per_cohort,
    tx_cohort,
    write_cost_model,
)
from benchmark.objective import UNRANKED_OFFSET, empirical_cost, gross_only
from benchmark.profile import ProfileError, parse_profile
from benchmark.results import experiment_identity
from routing.algorithms import direct, single_path
from routing.algorithms.base import AlgorithmConfig, Budget, SolveContext
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from snapshot import cost_evidence as ce
from snapshot.bundle import load_bundle
from snapshot.corpus import load_export
from snapshot.models import Case, SnapshotBundle
from snapshot.prices import STATUS_MISSING, TokenPrice

REPO = Path(__file__).resolve().parents[2]
EXPORTS = REPO / "tests" / "fixtures" / "costs"
MODEL_PATH = REPO / "config" / "costs" / "mantle-101082044-cost-v1.json"
MODEL_SHA256 = "50e89727d6b6ac869c14c837c7cdec8099efc897d201794640912f3f6b4b7e71"
CORPUS = REPO / "tests" / "fixtures" / "corpus" / "bundle"
FBTC = "0xc96de26018a54d51c097160568752c4e3bd6c364"


@cache
def config() -> ce.CostConfig:
    return ce.load_cost_config(REPO / "config" / "cost_calibration.yaml")


@cache
def samples() -> ce.SampleSet:
    return ce.load_sample_set(EXPORTS, config())


@cache
def evidence() -> dict[str, Any]:
    doc, _ = ce.load_fee_evidence(EXPORTS / ce.EVIDENCE_FILE)
    return doc


@cache
def model() -> CostModel:
    return load_cost_model(MODEL_PATH, expected_sha256=MODEL_SHA256)


@cache
def corpus() -> SnapshotBundle:
    return load_bundle(CORPUS)


def _hex(value: str) -> int:
    return int(value, 16)


# ------------------------------------------------------------------ exports / SQL


def test_checked_in_sql_is_generated_and_bounded() -> None:
    cfg = config()
    ce.check_sql_files(cfg, EXPORTS)  # byte-identical to what the config generates
    for name in ce.EXPORTS:
        sql = ce.generate_sql(cfg, name)
        assert "blockchain = 'mantle'" in sql
        assert "block_month IN (DATE '2026-09-01')" in sql
        assert "block_date >= DATE '2026-09-18' AND tx.block_date < DATE '2026-09-25'" in sql
        assert "block_time < TIMESTAMP '2026-09-25 00:00:00'" in sql
        # legs are grouped by transaction before the fee row is joined (once)
        assert sql.index("GROUP BY tx_hash") < sql.index("LEFT JOIN mantle.transactions")
    assert cfg.window_end_ts <= cfg.block_timestamp == 1790294400


def test_config_rejects_a_window_after_the_snapshot() -> None:
    import yaml

    raw = yaml.safe_load((REPO / "config" / "cost_calibration.yaml").read_text())
    raw["dune"]["window_end"] = "2026-09-25 00:00:01"
    with pytest.raises(ce.CostEvidenceError, match="no later than the snapshot"):
        ce.parse_cost_config(raw, source_path="<t>", sha256="0")


def test_exports_are_identified_and_the_sample_is_one_row_per_transaction() -> None:
    s = samples()
    assert (s.census.query_id, s.samples.query_id) == (8835037, 8835020)
    assert s.samples.sha256 == "de91abedf0f36a822b3ad1426ddc8c7a35a4b1f92a4152afac19fc40d7cd0389"
    assert s.census.sha256 == "2e8789bb452161695a927d833e43cd6027b5966c6b10876911e239f03bfbe862"
    assert len(s.txs) == 3151 == len({t.tx_hash for t in s.txs})
    shapes = [r for r in s.census.rows if r["kind"] == "shape"]
    window_txs = sum(r["txs"] for r in shapes)
    assert window_txs == 25427
    assert sum(r["duplicate_leg_rows"] for r in shapes) == 0
    # the seeded 1/8 sample is close to its expected size
    assert abs(len(s.txs) - window_txs / 8) < 4 * (window_txs / 8) ** 0.5


def test_ingest_round_trips_a_saved_dune_result(tmp_path: Path) -> None:
    for name, (sql_file, export_file, _) in ce.EXPORTS.items():
        export = ce.load_cost_export(EXPORTS / export_file)
        doc = {
            "query": {"query_id": export.query_id, "query": (EXPORTS / sql_file).read_text()},
            "result_preview": {
                "executionId": export.execution_id,
                "state": "COMPLETED",
                "resultMetadata": {
                    "columns": [{"name": c} for c in export.columns],
                    "totalRowCount": len(export.rows),
                },
                "data": {"rows": list(export.rows)},
            },
        }
        raw = tmp_path / f"{name}.json"
        raw.write_text(json.dumps(doc))
        (tmp_path / sql_file).write_text((EXPORTS / sql_file).read_text())
        again = ce.ingest(config(), name, raw, tmp_path)
        assert again.sha256 == export.sha256
        assert (tmp_path / export_file).read_bytes() == (EXPORTS / export_file).read_bytes()
        doc["result_preview"]["resultMetadata"]["totalRowCount"] += 1  # type: ignore[index]
        raw.write_text(json.dumps(doc))
        with pytest.raises(ce.CostEvidenceError, match="partial"):
            ce.ingest(config(), name, raw, tmp_path)


def _row(**overrides: Any) -> dict[str, Any]:
    row = dict(ce.load_cost_export(EXPORTS / "q2_samples.jsonl.gz").rows[0])
    row.update(overrides)
    return row


def _export(rows: list[dict[str, Any]]) -> Any:
    base = ce.load_cost_export(EXPORTS / "q2_samples.jsonl.gz")
    return replace(base, rows=tuple(rows))


# ------------------------------------------------------------------ AC1: no per-leg totals


def test_a_per_leg_export_is_rejected_so_a_fee_is_never_counted_per_leg() -> None:
    two_leg = _row(legs=2, evts=2, c=2, pc=2, pools=2, tokens=3)
    with pytest.raises(ce.CostEvidenceError, match="one row per transaction"):
        ce.tx_samples(_export([two_leg, dict(two_leg)]))  # the same tx once per leg
    with pytest.raises(ce.CostEvidenceError, match="duplicated leg rows"):
        ce.tx_samples(_export([_row(legs=2, evts=1)]))


def test_receipt_evidence_fixes_the_fee_components_without_double_counting() -> None:
    rules, summary = ce.check_fee_evidence(evidence(), config())
    assert (rules.operator_fee_scalar, rules.operator_fee_constant) == (100_000_000, 0)
    assert summary["balance_drop_equals_total"] >= 10
    assert summary["gasfees_short_by_exactly_the_operator_fee"] == summary["receipts"] == 12
    assert summary["base_fee_per_gas_observed"] == [50_000_000_000]
    explained = set(summary["explained_tx_hashes"])
    for rec in evidence()["receipts"]:
        if rec["tx_hash"] not in explained:
            continue
        r = rec["receipt"]
        gas, price, l1 = _hex(r["gasUsed"]), _hex(r["effectiveGasPrice"]), _hex(r["l1Fee"])
        drop = _hex(rec["sender_balance_before"]) - _hex(rec["sender_balance_after"])
        drop -= _hex(rec["tx_value"])
        gasfees = int(rec["dune"]["gasfees_tx_fee_raw"])
        # The chain's own accounting: exactly one of these interpretations is the total.
        assert rules.total_fee_wei(gas, price, l1) == drop
        assert gas * price != drop  # gas_used * gas_price is not the total
        assert gasfees != drop  # Dune gas.fees omits the operator fee ...
        assert gasfees + l1 != drop  # ... and already contains l1Fee: never add it again
        assert gasfees == gas * price + l1


def test_evidence_that_disagrees_with_dune_or_changes_parameters_is_rejected() -> None:
    bad = copy.deepcopy(evidence())
    bad["receipts"][0]["dune"]["l1_fee"] = str(int(bad["receipts"][0]["dune"]["l1_fee"]) + 1)
    with pytest.raises(ce.CostEvidenceError, match="differ from the receipt"):
        ce.check_fee_evidence(bad, config())
    bad = copy.deepcopy(evidence())
    bad["param_checkpoints"][3]["operator_fee_scalar"] = hex(1)
    with pytest.raises(ce.CostEvidenceError, match="changed within the window"):
        ce.check_fee_evidence(bad, config())


def _swap_plan(case: Case, *pools: str) -> RoutePlan:
    """A single-chain plan through `pools` (each step consumes all of the previous)."""
    bundle = corpus()
    token, steps, fund = case.token_in, [], REQUEST_FUND_ID
    for i, pool_id in enumerate(pools):
        out = bundle.pools[pool_id].other_token(token)
        steps.append(SwapStep(pool_id, token, out, (FundInput(fund, ALL_REMAINING),), f"f{i}"))
        token, fund = out, f"f{i}"
    return RoutePlan(tuple(steps))


def _bound() -> Any:
    return empirical_cost(model()).bind(corpus())


def _chain_case() -> tuple[Case, tuple[str, str]]:
    """A no-direct-pool USDC->FBTC case and its best CL+CL route (single_path)."""
    bundle = corpus()
    case = bundle.case("nod-09bc4e-c96de2-large-1")
    prepared = single_path.prepare(bundle, AlgorithmConfig(single_path.NAME, {"max_hops": 2}))
    result = single_path.solve(
        case, SolveContext(bundle=bundle, objective=_bound(), prepared=prepared), Budget()
    )
    assert result.plan is not None
    first, second = (s.pool_id for s in result.plan.steps)
    return case, (first, second)


def test_a_multi_hop_plan_is_charged_one_whole_transaction_fee() -> None:
    case, (first, second) = _chain_case()
    ev = evaluate(corpus(), case, _swap_plan(case, first, second), _bound())
    assert ev.status is EvalStatus.OK and ev.cost is not None
    assert ev.cost["cohort"] == "c2.l0.p0.chain" and ev.cost["status"] == SUPPORTED
    two_hop = model().cohorts["c2.l0.p0.chain"].nominal_fee_wei
    one_hop = model().cohorts["c1.l0.p0.chain"].nominal_fee_wei
    assert int(ev.cost["native_nominal_wei"]) == two_hop
    # the plan's own two-pool cohort, measured whole -- not a sum of per-hop totals
    assert two_hop != 2 * one_hop
    prices = corpus().prices
    assert prices is not None
    usd = prices.usd_value(prices.native["via_token"], two_hop)
    assert ev.estimated_cost == prices.raw_amount_for_usd(FBTC, usd)
    assert ev.estimated_net_output == ev.gross_output - ev.estimated_cost


# ------------------------------------------------------------------ AC2: reproducible artifacts


def test_refitting_from_checked_in_inputs_reproduces_the_artifact_byte_for_byte() -> None:
    rules, summary = ce.check_fee_evidence(evidence(), config())
    raw_evidence = (EXPORTS / ce.EVIDENCE_FILE).read_bytes()
    provenance = model().document["provenance"]
    assert provenance["fee_evidence"]["sha256"] == hashlib.sha256(raw_evidence).hexdigest()
    document = fit_cost_model(
        config(),
        samples(),
        rules,
        provenance=provenance,
        validated_receipts=receipts_per_cohort(summary["explained_tx_hashes"], samples()),
    )
    data = canonical_model_bytes(document)
    assert data == MODEL_PATH.read_bytes()
    assert hashlib.sha256(data).hexdigest() == MODEL_SHA256


def test_loading_rederives_predictions_and_holdout_statistics() -> None:
    doc = json.loads(MODEL_PATH.read_bytes())
    for field, mutate in (
        ("nominal_fee_wei", lambda c: str(int(c["nominal_fee_wei"]) + 1)),
        ("holdout_statistics", lambda c: c["holdout_statistics"] | {"rel_error_median": "0"}),
        ("holdout_rows", lambda c: c["holdout_rows"][1:] + c["holdout_rows"][:1][:0]),
        ("high_multiplier", lambda c: "9"),
    ):
        bad = copy.deepcopy(doc)
        cohort = bad["cohorts"]["c1.l0.p0.chain"]
        cohort[field] = mutate(cohort)
        with pytest.raises(CostModelError, match="reproduce|holdout rows"):
            parse_cost_model(bad, sha256="x")


def test_holdout_statistics_match_an_independent_recomputation() -> None:
    m = model()
    cohort_doc = m.document["cohorts"]["c1.l0.p0.chain"]
    ref = m.reference_gas_price_wei
    nominal = m.cohorts["c1.l0.p0.chain"].nominal_fee_wei
    # nominal: medians priced at the reference, operator fee added once
    assert nominal == int(cohort_doc["gas_used_median"]) * (ref + 10_000_000_000) + int(
        cohort_doc["l1_fee_median_wei"]
    )
    errors = sorted(
        (int(r["gas_used"]) * (ref + 10_000_000_000) + int(r["l1_fee_wei"])) / nominal - 1
        for r in cohort_doc["holdout_rows"]
    )
    n = len(errors)
    assert n == cohort_doc["n_holdout"] >= config().min_holdout
    mid = errors[(n - 1) // 2] if n % 2 else (errors[n // 2 - 1] + errors[n // 2]) / 2
    assert Decimal(cohort_doc["holdout_statistics"]["rel_error_median"]) == Decimal(f"{mid:.6f}")
    # training and holdout are disjoint and follow the declared key rule
    train = set(cohort_doc["train_keys"])
    hold = {r["key"] for r in cohort_doc["holdout_rows"]}
    assert not train & hold
    assert all(assign_split(k, 3) == "train" for k in train)
    assert all(assign_split(k, 3) == "holdout" for k in hold)


def test_an_artifact_is_immutable_and_pinned_by_hash(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    doc = json.loads(MODEL_PATH.read_bytes())
    assert write_cost_model(doc, path) == MODEL_SHA256
    assert write_cost_model(doc, path) == MODEL_SHA256  # same bytes: no-op
    doc["model_id"] = "other"
    with pytest.raises(CostModelError, match="immutable"):
        write_cost_model(doc, path)
    with pytest.raises(CostModelError, match="sha256"):
        load_cost_model(MODEL_PATH, expected_sha256="0" * 64)


# ------------------------------------------------------------------ AC3: missing price/cost


def _direct_plan(case: Case) -> RoutePlan:
    result = direct.solve(case, SolveContext(bundle=corpus(), objective=gross_only()), Budget())
    assert result.plan is not None
    return result.plan


def test_missing_price_context_keeps_gross_and_withholds_net() -> None:
    bundle = corpus()
    no_prices = replace(bundle, prices=None)
    objective = empirical_cost(model()).bind(no_prices)
    assert objective.price_context_sha256 is None and "NO price context" in objective.label
    case = bundle.cases[0]
    plan = _direct_plan(case)
    gross = evaluate(bundle, case, plan, gross_only())
    ev = evaluate(no_prices, case, plan, objective)
    assert ev.gross_output == gross.gross_output
    assert ev.estimated_cost is None and ev.estimated_net_output is None
    assert ev.cost is not None and ev.cost["status"] == UNKNOWN_PRICE
    assert objective.score(ev) == ev.gross_output - UNRANKED_OFFSET


def test_a_missing_output_token_price_withholds_net() -> None:
    bundle = corpus()
    assert bundle.prices is not None
    case = next(
        c
        for c in bundle.cases
        if bundle.pools_for_pair(c.token_in, c.token_out) and c.amount_in > 10**6
    )
    entry = bundle.prices.tokens[case.token_out]
    missing = TokenPrice(
        entry.address, entry.symbol, entry.decimals, STATUS_MISSING, None, None, None
    )
    prices = replace(bundle.prices, tokens=bundle.prices.tokens | {case.token_out: missing})
    objective = replace(_bound(), prices=prices)
    ev = evaluate(bundle, case, _direct_plan(case), objective)
    assert ev.status is EvalStatus.OK and ev.gross_output > 0
    assert ev.estimated_net_output is None
    assert ev.cost is not None and ev.cost["status"] == UNKNOWN_PRICE
    assert "no frozen price" in ev.cost["reason"]


def test_unbound_or_foreign_block_objectives_are_refused() -> None:
    objective = empirical_cost(model())
    with pytest.raises(ValueError, match="before bind"):
        objective.plan_cost({"pool_calls": 1, "pool_calls_concentrated": 1}, FBTC)
    other = replace(corpus(), block=replace(corpus().block, number=1))
    with pytest.raises(ValueError, match="calibrated for block"):
        objective.bind(other)


# ------------------------------------------------------------------ AC4: applicability


def test_shared_pool_parallel_and_out_of_range_shapes_are_flagged() -> None:
    m = model()
    shared = {"pool_calls": 2, "pool_calls_concentrated": 2, "repeated_pool_calls": 1}
    assert m.native_cost(shared)["status"] == UNSUPPORTED
    split = {"pool_calls": 2, "pool_calls_concentrated": 2, "split_funds": 1}
    assert plan_cohort(split) == ("c2.l0.p0.parallel", None)
    assert m.native_cost(split)["status"] in (UNSUPPORTED, LOW_CONFIDENCE)
    never_seen = {"pool_calls": 9, "pool_calls_constant_product": 9}
    assert m.native_cost(never_seen)["status"] == UNSUPPORTED
    lb = m.cohorts["c0.l1.p0.chain"]
    assert lb.lb_bins_max is not None
    wide = {"pool_calls": 1, "pool_calls_liquidity_book": 1, "lb_bins_swapped": lb.lb_bins_max + 1}
    assert m.native_cost(wide)["status"] == LOW_CONFIDENCE
    for features in (shared, split, never_seen, wide):
        assert m.plan_cost(features, FBTC, corpus().prices)["net_rankable"] is False


def test_supported_cohorts_carry_holdout_and_independent_receipt_evidence() -> None:
    supported = {k: c for k, c in model().cohorts.items() if c.status == SUPPORTED}
    assert set(supported) == {
        "c1.l0.p0.chain",
        "c0.l1.p0.chain",
        "c2.l0.p0.chain",
        "c0.l0.p1.chain",
        "c1.l1.p0.chain",
    }
    for cohort in supported.values():
        assert cohort.n_train >= 30 and cohort.n_holdout >= 10
        assert cohort.low_multiplier is not None and cohort.high_multiplier is not None
        assert cohort.low_multiplier <= 1 <= cohort.high_multiplier
    assert all(c.validated_receipts >= 1 for c in supported.values())
    assert all(not k.endswith("parallel") for k in supported)


def test_historical_shapes_map_to_cohorts_like_plans_do() -> None:
    by_key = {t.key: t for t in samples().txs}
    counts: dict[str, int] = {}
    for tx in by_key.values():
        key, why = tx_cohort(tx.shape)
        counts[key or f"excluded:{why}"] = counts.get(key or f"excluded:{why}", 0) + 1
    assert counts["c1.l0.p0.chain"] == 2002
    assert counts["excluded:cyclic_or_multi_io"] == 367  # arbitrage cycles never priced


# ------------------------------------------------------------------ selection / reversal


def test_fee_driven_ranking_reversal_in_final_selection() -> None:
    bundle = corpus()
    objective = _bound()
    # direct: gross prefers the LB pool (1 raw unit vs 0); the costlier LB execution
    # makes the concentrated pool the better net choice.
    case = bundle.case("bnd-78c1b0-779ded-round_at")
    g = direct.solve(case, SolveContext(bundle=bundle, objective=gross_only()), Budget())
    e = direct.solve(case, SolveContext(bundle=bundle, objective=objective), Budget())
    assert g.evaluation is not None and e.evaluation is not None
    assert g.evaluation.gross_output > e.evaluation.gross_output
    g_net = evaluate(bundle, case, g.plan, objective)  # type: ignore[arg-type]
    assert g_net.estimated_net_output is not None and e.evaluation.estimated_net_output is not None
    assert e.evaluation.estimated_net_output > g_net.estimated_net_output
    assert (g_net.cost or {})["cohort"] == "c0.l1.p0.chain"
    assert (e.evaluation.cost or {})["cohort"] == "c1.l0.p0.chain"
    # single_path: equal gross; the cheaper whole-plan shape (CL+CL) wins on net.
    case, _ = _chain_case()
    prepared = single_path.prepare(bundle, AlgorithmConfig(single_path.NAME, {"max_hops": 2}))
    g = single_path.solve(
        case, SolveContext(bundle=bundle, objective=gross_only(), prepared=prepared), Budget()
    )
    assert g.plan is not None
    g_net = evaluate(bundle, case, g.plan, objective)
    e_ev = evaluate(bundle, case, _swap_plan(case, *_chain_case()[1]), objective)
    assert g_net.gross_output == e_ev.gross_output
    assert (g_net.cost or {})["cohort"] == "c1.l1.p0.chain"
    assert (e_ev.estimated_net_output or 0) > (g_net.estimated_net_output or 0)


def test_rankable_plans_outscore_unrankable_ones() -> None:
    objective = _bound()
    worst = Evaluation(status=EvalStatus.OK, gross_output=0, estimated_net_output=1 - (1 << 256))
    unranked = Evaluation(status=EvalStatus.OK, gross_output=(1 << 256) - 1)
    small = Evaluation(status=EvalStatus.OK, gross_output=1)
    assert objective.score(worst) > objective.score(unranked) > objective.score(small)
    assert gross_only().score(unranked) == (1 << 256) - 1


# ------------------------------------------------------------------ profile / identity


def _profile(objective: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "algorithms": ["direct"],
        "objective": objective,
        "budget": {"time_limit_seconds": 10, "max_quotes": None, "max_candidates": None},
        "measurement": {
            "warmup": 0,
            "repeats": 1,
            "seed": 0,
            "order": "fixed",
            "memory_pass": False,
        },
        "worker": {"start_method": "spawn", "scope": "algorithm", "prepare_time_limit_seconds": 10},
    }


def test_profile_pins_the_cost_model_and_the_experiment_identity_binds_it() -> None:
    empirical = {
        "mode": "empirical_cost",
        "cost_model": str(MODEL_PATH),
        "cost_model_sha256": MODEL_SHA256,
    }
    profile = parse_profile(_profile(empirical), "<t>")
    resolved = profile.resolved()["objective"]
    assert resolved["cost_model"]["sha256"] == MODEL_SHA256
    with pytest.raises(ProfileError, match="sha256"):
        parse_profile(_profile(empirical | {"cost_model_sha256": "0" * 64}), "<t>")
    with pytest.raises(ProfileError, match="requires"):
        parse_profile(_profile({"mode": "empirical_cost", "cost_model": str(MODEL_PATH)}), "<t>")
    with pytest.raises(ProfileError, match="belong to empirical_cost"):
        parse_profile(_profile({"mode": "gross_only", "cost_model": "x"}), "<t>")
    bound = profile.objective.bind(corpus())
    ids = {
        experiment_identity(corpus().bundle_hash, o.to_dict())["experiment_id"]
        for o in (gross_only(), bound, replace(bound, price_context_sha256="0" * 64))
    }
    assert len(ids) == 3  # objective, cost model and price context are all identity
    moved = bound.to_dict()
    moved["cost_model"] = moved["cost_model"] | {"path": "elsewhere.json"}
    same = experiment_identity(corpus().bundle_hash, moved)["experiment_id"]
    assert same == experiment_identity(corpus().bundle_hash, bound.to_dict())["experiment_id"]


def test_cost_export_gzip_identity_is_the_canonical_text() -> None:
    path = EXPORTS / "q2_samples.jsonl.gz"
    text = gzip.decompress(path.read_bytes())
    assert ce.load_cost_export(path).sha256 == hashlib.sha256(text).hexdigest()
    census = load_export(EXPORTS / "q1_census.jsonl")
    assert census.sha256 == samples().census.sha256


def test_empirical_mode_requires_its_model_and_nothing_else() -> None:
    from benchmark.objective import ObjectiveContext

    with pytest.raises(ValueError, match="cost_model"):
        ObjectiveContext(mode="empirical_cost")
    with pytest.raises(ValueError, match="cost_model"):
        ObjectiveContext(mode="gross_only", cost_model=model())
    with pytest.raises(ValueError, match="fixed_cost"):
        ObjectiveContext(mode="empirical_cost", cost_model=model(), fixed_cost=1)
    assert gross_only().bind(corpus()) == gross_only()

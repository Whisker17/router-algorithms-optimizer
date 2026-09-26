"""WHI-1446: offline report from synthetic run records (docs/DESIGN.md §2.11).

Every run here is hand-written in the schema-2 result format (`benchmark.results`), so
each test controls exactly which statuses, outputs, costs and labels the report sees.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import socket
from pathlib import Path
from typing import Any

import pytest

import main
from benchmark.results import ResultError, load_manifest
from report import aggregate as agg
from report.render import render_report

MIN = 3  # min_samples used by most tests
T_IN, T_OUT, T_MID = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40

SOR_CONFIG = {
    "capabilities": {
        "multi_hop": True,
        "split": True,
        "shared_pools": False,
        "protocols": ["V2", "V3"],
    },
    "params": {},
    "provenance": {
        "upstream": {
            "package": "@uniswap/smart-order-router",
            "version": "4.31.10",
            "commit": "04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647",
            "license": "GPL-3.0-only",
            "repository": "https://github.com/Uniswap/smart-order-router",
        },
        "port_scope": "exact-input V2/V3 routing core",
        "adaptations": ["A-1", "A-2"],
        "deviations": ["D-1"],
        "contract": "docs/references/uni-sor-port-contract.md",
    },
}
CAPS = {
    "direct": {"multi_hop": False, "split": False, "shared_pools": False},
    "single_path": {"multi_hop": True, "split": False, "shared_pools": False},
    "path_split": {"multi_hop": True, "split": True, "shared_pools": False},
    "incremental_graph": {"multi_hop": True, "split": True, "shared_pools": True},
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rec(
    case_id: str,
    algorithm: str,
    status: str = "ok",
    gross: int | None = None,
    *,
    net: int | None = None,
    cost: dict[str, Any] | None = None,
    features: dict[str, int] | None = None,
    error: str | None = None,
    limit_hit: str | None = None,
    solve: float = 0.01,
    quotes: int = 10,
    score: str | None = None,
    search: dict[str, Any] | None = None,
    last_valid: bool = False,
) -> dict[str, Any]:
    evaluation = None
    if status == "ok":
        evaluation = {
            "status": "ok",
            "gross_output": str(gross),
            "estimated_net_output": None if net is None else str(net),
            "estimated_cost": None,
            "route_features": features or {"split_funds": 0, "repeated_pool_calls": 0},
            "trace": [
                {
                    "step": 0,
                    "pool_id": "pool-1",
                    "token_in": T_IN,
                    "token_out": T_OUT,
                    "inputs": [{"fund_id": "REQUEST", "amount": "100"}],
                    "output_fund_id": "OUT",
                    "amount_in": "100",
                    "amount_out": str(gross),
                    "status": "ok",
                    "features": {},
                }
            ],
            "funds": [],
            "residuals": {},
            "error": None,
            "objective_label": "test",
        }
        if cost is not None:
            evaluation["cost"] = cost
    return {
        "case_id": case_id,
        "algorithm": algorithm,
        "status": status,
        "evaluation": evaluation,
        "score": score if score is not None else (None if gross is None else str(gross)),
        "candidates_considered": 1,
        "candidates_truncated": 0,
        "search": search or {},
        "quotes": {"attempted": quotes, "counted": quotes},
        "limit_hit": limit_hit,
        "error": error,
        "solver_reported": None,
        "last_valid_candidate": {"status": "ok"} if last_valid else None,
        "measurement": {"solve_seconds": [solve]},
    }


def write_bundle(
    root: Path,
    *,
    symbol_out: str = "USDT",
    cohort: str | None = None,
    splits: dict[str, str] | None = None,
    pools: dict[str, str] | None = None,
) -> Path:
    bundle = root / "bundle"
    bundle.mkdir(parents=True)
    cases = "".join(
        json.dumps({"case_id": c, "token_in": T_IN, "token_out": T_OUT, "amount_in": "100"}) + "\n"
        for c in ("c1", "c2", "c3", "c4")
    )
    meta = {
        c: {
            "stratum": "large" if c in ("c1", "c2") else "low",
            "split": (splits or {}).get(c, "report"),
        }
        for c in ("c1", "c2", "c3", "c4")
    }
    corpus = {"case_metadata": meta, **({"cohort": cohort} if cohort else {})}
    prices = {
        "tokens": {
            T_IN: {"symbol": "USDC", "decimals": 6},
            T_OUT: {"symbol": symbol_out, "decimals": 6},
        }
    }
    files = {
        "cases.jsonl": cases,
        "corpus.json": json.dumps(corpus),
        "prices.json": json.dumps(prices),
    }
    if pools is not None:
        files["pools.json"] = json.dumps(
            {"pools": [{"pool_id": p, "source_key": s} for p, s in pools.items()]}
        )
    for name, text in files.items():
        (bundle / name).write_text(text)
    manifest = {"bundle_id": "b", "checksums": {n: _sha(t.encode()) for n, t in files.items()}}
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    return bundle


def write_run(
    root: Path,
    records: list[dict[str, Any]],
    *,
    algorithms: list[str],
    run_id: str = "run-1",
    objective: dict[str, Any] | None = None,
    state: str = "complete",
    bundle: Path | None = None,
    configs: dict[str, dict[str, Any]] | None = None,
    case_order: list[str] | None = None,
    memory: list[dict[str, Any]] | None = None,
) -> Path:
    run_dir = root / "results" / run_id
    run_dir.mkdir(parents=True)
    cases_text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
    (run_dir / "cases.jsonl").write_text(cases_text)
    memory_sha = None
    if memory is not None:
        memory_text = "".join(json.dumps(m) + "\n" for m in memory)
        (run_dir / "memory.jsonl").write_text(memory_text)
        memory_sha = _sha(memory_text.encode())
    order = case_order or sorted({r["case_id"] for r in records})
    config = {a: {"capabilities": CAPS.get(a, {}), "params": {}} for a in algorithms}
    config.update(configs or {})
    bundle_hash = _sha((bundle / "manifest.json").read_bytes()) if bundle else "f" * 64
    manifest = {
        "run_id": run_id,
        "schema_version": 2,
        "state": state,
        "bundle_id": "bundle-id",
        "bundle_hash": bundle_hash,
        "profile_path": "config/p.yaml",
        "profile_sha256": "e" * 64,
        "resolved_profile": {
            "objective": objective or {"mode": "gross_only", "fixed_cost": 0},
            "algorithm_config": config,
        },
        "objective_label": "objective-label",
        "algorithms": algorithms,
        "created_at": "2026-09-25T00:00:00+00:00",
        "finished_at": "2026-09-25T00:01:00+00:00",
        "replay_command": f"uv run python main.py run --bundle {bundle or 'nowhere'} "
        "--profile config/p.yaml",
        "scheduled_count": len(order) * len(algorithms),
        "case_count": len(records),
        "status_counts": {},
        "cases_sha256": _sha(cases_text.encode()),
        "memory_record_count": None if memory is None else len(memory),
        "memory_sha256": memory_sha,
        "measurement": {"case_order": order},
        "prepare_events": [{"algorithm": a, "prepare_seconds": 0.5} for a in algorithms],
        "timing": {},
        "environment": {"git_revision": "abc123", "git_dirty": True, "git_diff_sha256": "d" * 64},
        "experiment": {"experiment_id": "exp-1"},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    return run_dir


def load(run_dir: Path, **kw: Any) -> agg.RunData:
    return agg.load_run(run_dir, **kw)


def by_algorithm(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["algorithm"]: r for r in rows}


# ------------------------------------------------------------------ denominators


def test_every_failure_status_stays_in_the_denominator(tmp_path: Path) -> None:
    records = [
        rec("c1", "direct", gross=1000),
        rec("c2", "direct", "unsupported", error="LB only"),
        rec("c3", "direct", "no_route", error="no pool"),
        rec("c4", "direct", "timeout", limit_hit="quotes", error="quote budget", last_valid=True),
        rec("c5", "direct", "algorithm_error", error="boom"),
        rec("c6", "direct", "cancelled", error="interrupted"),
        rec("c7", "direct", "incomplete_snapshot", error="missing tick"),
    ]
    run = load(write_run(tmp_path, records, algorithms=["direct"]))
    (row,) = agg.status_table(run)
    assert row["scheduled"] == 7
    assert sum(row[s] for s in agg.STATUS_ORDER) == 7
    assert row["ok"] == 1 and row["ok_share"] == pytest.approx(1 / 7)
    assert row["timeout_by_limit"] == {"quotes": 1}
    assert row["last_valid_candidates"] == 1
    groups = agg.error_groups(run)
    assert {g["status"] for g in groups} == {
        "unsupported",
        "no_route",
        "timeout",
        "algorithm_error",
        "cancelled",
        "incomplete_snapshot",
    }


def test_incomplete_run_needs_opt_in_and_counts_missing_cases(tmp_path: Path) -> None:
    run_dir = write_run(
        tmp_path,
        [rec("c1", "direct", gross=5)],
        algorithms=["direct"],
        state="interrupted",
        case_order=["c1", "c2"],
    )
    with pytest.raises(ResultError):
        load(run_dir)
    run = load(run_dir, allow_incomplete=True)
    (row,) = agg.status_table(run)
    assert (row["scheduled"], row["ok"], row["missing"]) == (2, 1, 1)
    paths = render_report(load_manifest(run_dir, allow_incomplete=True), tmp_path / "out")
    assert "interrupted" in paths.html.read_text()


# ------------------------------------------------------------------ paired gross


def _paired_run(tmp_path: Path) -> agg.RunData:
    records = [
        rec("c1", "direct", gross=1000),
        rec("c1", "path_split", gross=1010),
        rec("c1", "single_path", gross=1000),
        rec("c2", "direct", gross=2000),
        rec("c2", "path_split", gross=1990),
        rec("c2", "single_path", gross=2000),
        rec("c3", "direct", "no_route", error="no direct pool"),
        rec("c3", "path_split", gross=500),
        rec("c3", "single_path", gross=400),
        rec("c4", "direct", gross=0),
        rec("c4", "path_split", gross=7),
        rec("c4", "single_path", "timeout", limit_hit="time", error="slow"),
    ]
    return load(write_run(tmp_path, records, algorithms=["direct", "single_path", "path_split"]))


def test_paired_comparisons_use_common_success_relative_improvements(tmp_path: Path) -> None:
    run = _paired_run(tmp_path)
    vs = by_algorithm(agg.vs_direct(run, MIN))
    ps = vs["path_split"]
    # c1: +100 bps, c2: -50 bps; c3 has no direct baseline (N/A); c4 direct output 0.
    assert ps["n"] == 2 and (ps["better"], ps["worse"]) == (1, 1)
    assert ps["p50"] == pytest.approx(-50.0) and ps["mean"] == pytest.approx(25.0)
    assert ps["na_no_direct"] == 1 and ps["zero_baseline"] == 1
    assert ps["underpowered"] is True
    # An alternative baseline still compares the case direct could not solve.
    pair = {(p["algorithm"], p["baseline"]): p for p in agg.pairwise(run, MIN)}
    alt = pair[("path_split", "single_path")]
    assert alt["n"] == 3 and alt["only_algorithm"] == 1  # c4: single_path timed out
    assert alt["p50"] == pytest.approx(100.0)  # c1 +100, c2 -50, c3 +2500 bps


def test_relative_improvements_are_per_case_not_raw_amount_averages(tmp_path: Path) -> None:
    # Two assets of wildly different raw scale: an average of raw amounts would be
    # dominated by the large one; the per-case bps are symmetric.
    records = [
        rec("c1", "direct", gross=10**24),
        rec("c1", "path_split", gross=10**24 + 10**20),
        rec("c2", "direct", gross=100),
        rec("c2", "path_split", gross=99),
    ]
    run = load(write_run(tmp_path, records, algorithms=["direct", "path_split"]))
    (ps,) = agg.vs_direct(run, MIN)
    assert ps["mean"] == pytest.approx((1.0 - 100.0) / 2)


def test_missing_direct_baseline_renders_na(tmp_path: Path) -> None:
    records = [rec("c1", "single_path", gross=5), rec("c1", "path_split", gross=6)]
    run_dir = write_run(tmp_path, records, algorithms=["single_path", "path_split"])
    run = load(run_dir)
    assert all(s["absent"] for s in agg.vs_direct(run, MIN))
    html = render_report(load_manifest(run_dir), tmp_path / "out").html.read_text()
    assert "no <code>direct</code> baseline" in html
    assert '<span class="na">N/A</span>' in html


def test_na_column_for_cases_without_direct_baseline_is_rendered(tmp_path: Path) -> None:
    run = _paired_run(tmp_path)
    paths = render_report(run.manifest, tmp_path / "out", min_samples=MIN)
    html = paths.html.read_text()
    assert "N/A (no direct)" in html and "underpowered" in html
    rows = list(csv.DictReader((paths.csv["paired_gross"]).open()))
    row = next(r for r in rows if r["algorithm"] == "path_split" and r["baseline"] == "direct")
    assert row["only_algorithm_ok"] == "1" and row["baseline_output_zero"] == "1"
    assert row["underpowered"] == "True"


def test_topology_separates_like_for_like_from_capability_gains(tmp_path: Path) -> None:
    split = {"split_funds": 1, "repeated_pool_calls": 0}
    shared = {"split_funds": 1, "repeated_pool_calls": 1}
    records = [
        rec("c1", "direct", gross=100),
        rec("c1", "incremental_graph", gross=101),
        rec("c2", "direct", gross=100),
        rec("c2", "incremental_graph", gross=110, features=split),
        rec("c3", "direct", gross=100),
        rec(
            "c3",
            "incremental_graph",
            gross=120,
            features=shared,
            search={"topology": "shared_pool"},
        ),
    ]
    run = load(write_run(tmp_path, records, algorithms=["direct", "incremental_graph"]))
    (ig,) = agg.vs_direct(run, 1)
    assert ig["like_for_like"]["n"] == 1 and ig["like_for_like"]["p50"] == pytest.approx(100.0)
    assert ig["capability"]["n"] == 2
    topo = by_algorithm(agg.topology_table(run))["incremental_graph"]
    assert (topo["single_route"], topo["disjoint_split"], topo["shared_pool"]) == (1, 1, 1)


def test_multi_hop_plans_are_capability_gains_over_a_single_hop_baseline(
    tmp_path: Path,
) -> None:
    hop = rec("c1", "single_path", gross=150)
    hop["evaluation"]["trace"] = [
        {
            "step": 0,
            "token_in": T_IN,
            "token_out": T_MID,
            "output_fund_id": "HOP1",
            "inputs": [{"fund_id": "REQUEST", "amount": "100"}],
        },
        {
            "step": 1,
            "token_in": T_MID,
            "token_out": T_OUT,
            "output_fund_id": "OUT",
            "inputs": [{"fund_id": "HOP1", "amount": "90"}],
        },
    ]
    records = [
        rec("c1", "direct", gross=100),
        hop,
        rec("c2", "direct", gross=100),
        rec("c2", "single_path", gross=100),
    ]
    run = load(write_run(tmp_path, records, algorithms=["direct", "single_path"]))
    assert run.row("c1", "single_path").multi_hop is True
    (sp,) = agg.vs_direct(run, 1)
    assert sp["capability"]["n"] == 1 and sp["capability"]["p50"] == pytest.approx(5000.0)
    assert sp["like_for_like"]["n"] == 1
    traces = agg.representative_traces(run)
    assert {"algorithm": "single_path", "case_id": "c1"}.items() <= next(
        t for t in traces if t["algorithm"] == "single_path"
    ).items()


# ------------------------------------------------------------------ cohorts / coverage


def test_differing_coverage_and_matched_vs_full_cohorts(tmp_path: Path) -> None:
    def records(mode: str) -> list[dict[str, Any]]:
        sor = {"coverage_mode": mode}
        return [
            rec("c1", "path_split", gross=1010),
            rec("c1", "uni_sor_port", gross=1000, search=sor),
            rec("c2", "path_split", gross=900),
            rec("c2", "uni_sor_port", "unsupported", error="LB-only pair", search=sor),
        ]

    algos = ["path_split", "uni_sor_port"]
    full = load(
        write_run(
            tmp_path / "f",
            records("full_universe"),
            algorithms=algos,
            configs={"uni_sor_port": SOR_CONFIG},
        )
    )
    matched = load(
        write_run(
            tmp_path / "m",
            records("matched_cohort"),
            algorithms=algos,
            configs={"uni_sor_port": SOR_CONFIG},
        )
    )
    assert (full.cohort, matched.cohort) == (agg.COHORT_FULL, agg.COHORT_MATCHED)
    pair_full = agg.paired_gross(full, "path_split", "uni_sor_port", full.case_ids, 1)
    pair_matched = agg.paired_gross(matched, "path_split", "uni_sor_port", matched.case_ids, 1)
    assert pair_full["kind"] == "coverage" and pair_matched["kind"] == "matched"
    assert pair_full["only_algorithm"] == 1  # the unsupported case: coverage, not quality
    statuses = by_algorithm(agg.status_table(full))
    assert statuses["uni_sor_port"]["unsupported"] == 1
    assert statuses["path_split"]["ok_share"] == 1.0 and statuses["uni_sor_port"]["ok_share"] == 0.5
    # Both cohorts render as separate, labeled sections of one report.
    html = render_report([matched.manifest, full.manifest], tmp_path / "out").html.read_text()
    assert html.count("<section class='run'") == 2
    assert agg.COHORT_TITLES[agg.COHORT_MATCHED].split(" (")[0] in html
    assert "Full five-source coverage" in html and "⚑" in html


def test_bundle_labels_are_used_only_when_the_hash_matches(tmp_path: Path) -> None:
    bundle = write_bundle(tmp_path, cohort="sor_compatible")
    records = [rec(c, "direct", gross=10) for c in ("c1", "c2", "c3", "c4")]
    good = load(write_run(tmp_path / "g", records, algorithms=["direct"], bundle=bundle))
    assert good.cohort == agg.COHORT_MATCHED
    assert good.cases["c1"].stratum == "large" and agg.pair_label(good, "c1") == "USDC→USDT"
    assert "hash verified" in good.bundle_note
    other = write_bundle(tmp_path / "other")
    run_dir = write_run(tmp_path / "b", records, algorithms=["direct"], bundle=bundle)
    unlabeled = load(run_dir, bundle_dirs=[other])  # replay path still points at `bundle`
    assert unlabeled.cases["c1"].stratum == "large"
    (bundle / "manifest.json").write_text("{}")  # the named bundle no longer matches
    stale = load(run_dir, bundle_dirs=[other])
    assert stale.cases["c1"].stratum == agg.UNLABELED
    assert "bundle_hash mismatch" in stale.bundle_note


def test_held_out_and_exploratory_scope_labels(tmp_path: Path) -> None:
    """WHI-1447: a run is labeled held-out only when every case is a report-split case;
    tuning-only, mixed and unlabeled runs are labeled exploratory in the HTML."""
    records = [rec(c, "direct", gross=10) for c in ("c1", "c2", "c3", "c4")]
    cases = {
        "held": {},
        "tune": dict.fromkeys(("c1", "c2", "c3", "c4"), "tuning"),
        "mixed": {"c1": "tuning"},
    }
    expected = {
        "held": agg.SCOPE_HELD_OUT,
        "tune": agg.SCOPE_TUNING,
        "mixed": agg.SCOPE_MIXED,
    }
    for name, splits in cases.items():
        bundle = write_bundle(tmp_path / name, splits=splits)
        run_dir = write_run(tmp_path / name, records, algorithms=["direct"], bundle=bundle)
        run = load(run_dir)
        assert run.evaluation_scope == expected[name]
        paths = render_report(load_manifest(run_dir), tmp_path / name / "out", min_samples=MIN)
        html = paths.html.read_text()
        assert agg.SCOPE_TITLES[expected[name]].split(":")[0] in html
    no_labels = load(write_run(tmp_path / "none", records, algorithms=["direct"]))
    assert no_labels.evaluation_scope == agg.SCOPE_UNLABELED


def test_source_coverage_counts_solved_plans_per_source(tmp_path: Path) -> None:
    """WHI-1447 (DESIGN §2.11 "source coverage"): per algorithm, how many solved plans
    use each admitted source; failed cases never count, and a bundle source no plan uses
    is shown with 0."""
    bundle = write_bundle(
        tmp_path, pools={"pool-1": "agni_v3", "pool-2": "moe_lb_v2_2", "pool-3": "uniswap_v3"}
    )
    two_sources = rec("c2", "single_path", gross=10)
    two_sources["evaluation"]["trace"].append(dict(two_sources["evaluation"]["trace"][0]))
    two_sources["evaluation"]["trace"][1]["pool_id"] = "pool-2"
    records = [
        rec("c1", "direct", gross=10),
        rec("c2", "direct", "no_route", error="none"),
        rec("c1", "single_path", gross=10),
        two_sources,
    ]
    run_dir = write_run(tmp_path, records, algorithms=["direct", "single_path"], bundle=bundle)
    cov = agg.source_coverage(load(run_dir))
    assert cov["sources"] == ["agni_v3", "moe_lb_v2_2", "uniswap_v3"]
    assert cov["bundle_pools"] == {"agni_v3": 1, "moe_lb_v2_2": 1, "uniswap_v3": 1}
    rows = by_algorithm(cov["rows"])
    assert rows["direct"] == {
        "algorithm": "direct",
        "ok": 1,
        "agni_v3": 1,
        "moe_lb_v2_2": 0,
        "uniswap_v3": 0,
        "other": 0,
    }
    assert rows["single_path"]["ok"] == 2
    assert rows["single_path"]["agni_v3"] == 2 and rows["single_path"]["moe_lb_v2_2"] == 1
    paths = render_report(load_manifest(run_dir), tmp_path / "out", min_samples=MIN)
    assert "Source coverage of solved plans" in paths.html.read_text()
    with (tmp_path / "out" / "source_coverage.csv").open() as fh:
        csv_rows = list(csv.DictReader(fh))
    assert len(csv_rows) == 2 * 3
    # Without a verified bundle there is nothing to attribute.
    unlabeled = write_run(tmp_path / "u", records, algorithms=["direct", "single_path"])
    assert agg.source_coverage(load(unlabeled))["rows"] == []


# ------------------------------------------------------------------ net / fee uncertainty


def _cost(nominal: int, low: int, high: int) -> dict[str, Any]:
    return {
        "status": "supported",
        "net_rankable": True,
        "nominal_out_raw": str(nominal),
        "low_out_raw": str(low),
        "high_out_raw": str(high),
        "reason": None,
    }


def _net_run(tmp_path: Path) -> agg.RunData:
    unsupported = {
        "status": "unsupported",
        "net_rankable": False,
        "reason": "no historical transaction of this shape",
    }
    sentinel = str(1000 - 2**257)
    records = [
        # c1: A wins at nominal and low cost, loses at high cost -> scenario reversal.
        rec("c1", "direct", gross=1000, net=990, cost=_cost(10, 5, 30)),
        rec("c1", "single_path", gross=990, net=988, cost=_cost(2, 1, 3)),
        # c2: A has the better gross but the worse net -> gross-vs-net order reversal.
        rec("c2", "direct", gross=1000, net=900, cost=_cost(100, 90, 110)),
        rec("c2", "single_path", gross=995, net=990, cost=_cost(5, 4, 6)),
        # c3: A's plan is unsupported by the cost model: unranked on net, gross kept;
        # its sentinel score must never be read as a net result.
        rec("c3", "direct", gross=1000, cost=unsupported, score=sentinel),
        rec("c3", "single_path", gross=990, net=980, cost=_cost(10, 9, 11)),
        # c4: no plan at all for A.
        rec("c4", "direct", "no_route", error="none"),
        rec("c4", "single_path", gross=10, net=9, cost=_cost(1, 1, 1)),
    ]
    objective = {
        "mode": "empirical_cost",
        "fixed_cost": 0,
        "cost_model": {"model_id": "m1", "sha256": "1" * 64},
        "price_context_sha256": "2" * 64,
    }
    return load(
        write_run(tmp_path, records, algorithms=["direct", "single_path"], objective=objective)
    )


def test_fee_uncertainty_scenarios_rank_reversals_and_unranked_cases(tmp_path: Path) -> None:
    run = _net_run(tmp_path)
    assert run.row("c1", "direct").net == {"nominal": 990, "low": 995, "high": 970}
    assert run.row("c3", "direct").net["nominal"] is None
    assert run.row("c3", "direct").gross == 1000
    coverage = by_algorithm(agg.net_coverage(run))
    assert coverage["direct"]["net_rankable"] == 2
    assert coverage["direct"]["unranked"] == {"no plan (no_route)": 1, "unsupported": 1}
    pair = next(
        p
        for p in agg.paired_net(run, 1)
        if (p["algorithm"], p["baseline"]) == ("direct", "single_path")
    )
    assert pair["scenario_reversals"] == ["c1"]
    assert pair["gross_vs_net_reversals"] == ["c2"]
    assert pair["both_ok_unranked"] == 1
    assert pair["nonpositive_baseline"] == 0
    assert pair["scenarios"]["nominal"]["n"] == 2
    nominal = sorted([relative_bps(990, 988), relative_bps(900, 990)])
    assert pair["scenarios"]["nominal"]["min"] == pytest.approx(nominal[0])
    assert pair["scenarios"]["high"]["max"] == pytest.approx(relative_bps(970, 987))
    paths = render_report(run.manifest, tmp_path / "out", min_samples=1)
    html = paths.html.read_text()
    assert "unranked on net" in html and "scenario rank reversals" in html
    assert "net_coverage" in paths.csv and "paired_net" in paths.csv
    rows = list(csv.DictReader(paths.csv["cases"].open()))
    c3 = next(r for r in rows if r["case_id"] == "c3" and r["algorithm"] == "direct")
    assert c3["net_nominal_raw"] == "N/A" and c3["cost_status"] == "unsupported"
    assert c3["gross_output_raw"] == "1000"


def relative_bps(a: int, b: int) -> float:
    value = agg.relative_bps(a, b)
    assert value is not None
    return value


def test_gross_only_run_shows_no_net_ranking(tmp_path: Path) -> None:
    run = _paired_run(tmp_path)
    assert not agg.net_available(run)
    paths = render_report(run.manifest, tmp_path / "out")
    assert "no net scores" in paths.html.read_text()
    assert "paired_net" not in paths.csv


def test_synthetic_fixed_cost_is_labeled_synthetic(tmp_path: Path) -> None:
    records = [rec("c1", "direct", gross=100, net=90), rec("c1", "single_path", gross=101, net=91)]
    run_dir = write_run(
        tmp_path,
        records,
        algorithms=["direct", "single_path"],
        objective={"mode": "synthetic_fixed_cost", "fixed_cost": 10},
    )
    run = load(run_dir)
    assert run.row("c1", "direct").cost_status == "synthetic"
    assert run.row("c1", "direct").net == {"nominal": 90, "low": None, "high": None}
    html = render_report(run.manifest, tmp_path / "out").html.read_text()
    assert "SYNTHETIC" in html


# ------------------------------------------------------------------ labels


def test_sor_and_metis_labels_cannot_pass_as_full_upstream_products(tmp_path: Path) -> None:
    records = [rec("c1", "uni_sor_port", gross=5), rec("c1", "direct", gross=5)]
    run_dir = write_run(
        tmp_path,
        records,
        algorithms=["direct", "uni_sor_port"],
        configs={"uni_sor_port": SOR_CONFIG},
    )
    run = load(run_dir)
    label = agg.algorithm_label(run, "uni_sor_port")
    assert label["kind"] == "scoped_port"
    assert "scoped routing-core port" in label["title"]
    assert "NOT the full upstream product" in label["title"]
    assert "04c7c0b4d85a" in label["title"]
    html = render_report(run.manifest, tmp_path / "out").html.read_text()
    assert "04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647" in html and "A-1, A-2" in html
    assert "label is reserved" in html
    metis = load(
        write_run(
            tmp_path / "x", [rec("c1", "metis_inspired", gross=5)], algorithms=["metis_inspired"]
        )
    )
    title = agg.algorithm_label(metis, "metis_inspired")["title"]
    assert "NOT Jupiter Metis" in title and "experimental" in title


# ------------------------------------------------------------------ escaping / offline

EVIL_SYMBOL = "<script>alert('sym')</script>"
EVIL_ERROR = '<img src=x onerror="alert(1)">'
EVIL_FORMULA = '=HYPERLINK("http://evil","x")'


def test_external_text_is_escaped_and_cannot_inject_html(tmp_path: Path) -> None:
    bundle = write_bundle(tmp_path, symbol_out=EVIL_SYMBOL)
    records = [
        rec("c1", "direct", gross=10),
        rec("c1", "single_path", gross=11),
        rec("c2", "direct", "no_route", error=EVIL_ERROR),
        rec("c2", "single_path", "algorithm_error", error=EVIL_FORMULA),
    ]
    run_dir = write_run(tmp_path, records, algorithms=["direct", "single_path"], bundle=bundle)
    paths = render_report(load_manifest(run_dir), tmp_path / "out")
    html = paths.html.read_text()
    assert EVIL_SYMBOL not in html and EVIL_ERROR not in html
    assert "&lt;script&gt;alert(&#x27;sym&#x27;)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    # No raw tag survives; `onerror=` only appears inside escaped, inert text.
    assert "<script" not in html.lower() and "<img" not in html.lower()
    assert "Content-Security-Policy" in html and "default-src 'none'" in html
    rows = list(csv.DictReader(paths.csv["cases"].open()))
    formula = next(r for r in rows if r["algorithm"] == "single_path" and r["case_id"] == "c2")
    assert formula["error"] == "'" + EVIL_FORMULA


def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted during an offline report")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def test_cli_report_is_offline_self_contained_and_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _block_network(monkeypatch)
    bundle = write_bundle(tmp_path)
    records = [
        rec(c, a, gross=100 + i)
        for c in ("c1", "c2")
        for i, a in enumerate(["direct", "single_path"])
    ]
    memory = [{"case_id": "c1", "algorithm": "direct", "solve_peak_bytes": 2048}]
    run_dir = write_run(
        tmp_path, records, algorithms=["direct", "single_path"], bundle=bundle, memory=memory
    )
    out = tmp_path / "report"
    assert main.main(["report", str(run_dir), "--output", str(out)]) == 0
    first = (out / "report.html").read_bytes()
    assert main.main(["report", str(run_dir), "--output", str(out)]) == 0
    assert (out / "report.html").read_bytes() == first  # deterministic
    html = first.decode()
    assert not re.search(r"""(src|href)\s*=\s*['"]?\s*(https?:|//)""", html)
    assert "<script" not in html and "<link" not in html
    for name in ("status_counts", "paired_gross", "latency", "pareto", "cases"):
        assert (out / f"{name}.csv").is_file()
        assert f"href='{name}.csv'" in html
    assert "2048" in html  # memory when present
    assert "uv run python main.py run --bundle" in html  # replay command
    assert "exp-1" in html and "abc123" in html and "d" * 64 in html
    assert f"uv run python main.py report {run_dir}" in html
    assert "wrote" in capsys.readouterr().out


def test_cli_report_refuses_incomplete_run_without_opt_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = write_run(
        tmp_path, [rec("c1", "direct", gross=1)], algorithms=["direct"], state="running"
    )
    assert main.main(["report", str(run_dir), "--output", str(tmp_path / "o")]) == 1
    assert "incomplete" in capsys.readouterr().err
    assert (
        main.main(["report", str(run_dir), "--output", str(tmp_path / "o"), "--allow-incomplete"])
        == 0
    )


# ------------------------------------------------------------------ statistics / pareto


def test_percentiles_are_nearest_rank_and_underpowered_is_marked() -> None:
    values = [float(v) for v in range(1, 101)]
    assert agg.percentile(values, 0.5) == 50.0 and agg.percentile(values, 0.95) == 95.0
    assert agg.percentile([], 0.5) is None
    assert agg.distribution([1.0, 2.0], 3)["underpowered"] is True
    assert agg.distribution([1.0, 2.0, 3.0], 3)["underpowered"] is False


def test_pareto_view_marks_dominated_algorithms(tmp_path: Path) -> None:
    records = []
    for c in ("c1", "c2"):
        records += [
            rec(c, "direct", gross=100, solve=0.001),
            rec(c, "single_path", gross=110, solve=0.01),
            rec(c, "path_split", gross=105, solve=0.1),  # slower and worse: dominated
        ]
    run = load(write_run(tmp_path, records, algorithms=["direct", "single_path", "path_split"]))
    points = by_algorithm(agg.pareto(run, 1)["points"])
    assert points["single_path"]["quality_mean"] == 0.0
    assert points["direct"]["efficient"] and points["single_path"]["efficient"]
    assert not points["path_split"]["efficient"]

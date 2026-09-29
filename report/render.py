"""`render_report(run_manifest) -> ReportPaths` (docs/DESIGN.md §4.3): write the offline
`report.html` plus CSV summaries for one or more runs, from their versioned records only.
Output is deterministic for the same inputs (no timestamps), so a report can be diffed.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmark.results import RunManifest
from report import aggregate as agg
from report.aggregate import DEFAULT_MIN_SAMPLES, RunData, load_run
from report.html import render_html

REPORT_HTML = "report.html"
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class ReportPaths:
    output_dir: Path
    html: Path
    csv: dict[str, Path] = field(default_factory=dict)


def _cell(value: Any) -> Any:
    """CSV cell. External text that a spreadsheet would read as a formula is quoted
    with a leading apostrophe; numbers pass through unchanged."""
    if value is None:
        return "N/A"
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, dict | list | tuple):
        value = json.dumps(value, sort_keys=True)
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        try:
            float(text)
        except ValueError:
            return "'" + text
    return text


def _write_csv(path: Path, headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(headers)
        for row in rows:
            writer.writerow([_cell(v) for v in row])


_DIST = ("n", "p5", "p25", "p50", "p75", "p95", "mean", "underpowered")


def _dist(d: Mapping[str, Any]) -> list[Any]:
    return [d[k] for k in _DIST]


def _csvs(runs: Sequence[RunData], out: Path, min_samples: int) -> dict[str, Path]:
    paths: dict[str, Path] = {}

    def emit(name: str, headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
        paths[name] = out / f"{name}.csv"
        _write_csv(paths[name], headers, rows)

    emit(
        "status_counts",
        [
            "run_id",
            "cohort",
            "algorithm",
            "scheduled",
            *agg.STATUS_ORDER,
            "other",
            "ok_share",
            "timeout_by_limit",
            "last_valid_candidates",
        ],
        (
            [
                r.manifest.run_id,
                r.cohort,
                e["algorithm"],
                e["scheduled"],
                *[e[s] for s in agg.STATUS_ORDER],
                e["other"],
                e["ok_share"],
                e["timeout_by_limit"],
                e["last_valid_candidates"],
            ]
            for r in runs
            for e in agg.status_table(r)
        ),
    )
    grouped = [(r, g) for r in runs if (g := agg.strategy_groups(r.manifest)) is not None]
    if grouped:  # WHI-1528: only runs that persisted their base / optimized selection

        def versus(r: RunData, a: str, baseline: str) -> list[Any]:
            if baseline not in r.algorithms or a == baseline:
                return [None, None]
            s = agg.paired_gross(r, a, baseline, r.case_ids, min_samples)
            return [s["n"], s["p50"]]

        emit(
            "strategy_groups",
            [
                "run_id",
                "cohort",
                "group",
                "algorithm",
                "recipe",
                "settings",
                "scheduled",
                *agg.STATUS_ORDER,
                "other",
                "ok_share",
                "vs_direct_n",
                "vs_direct_p50_bps",
                "vs_uni_sor_port_n",
                "vs_uni_sor_port_p50_bps",
            ],
            (
                [
                    r.manifest.run_id,
                    r.cohort,
                    group,
                    e["algorithm"],
                    (agg.strategy_recipe(r.manifest, e["algorithm"]) or {}).get("recipe"),
                    agg.algorithm_params(r.manifest, e["algorithm"]),
                    e["scheduled"],
                    *[e[st] for st in agg.STATUS_ORDER],
                    e["other"],
                    e["ok_share"],
                    *versus(r, e["algorithm"], "direct"),
                    *versus(r, e["algorithm"], "uni_sor_port"),
                ]
                for r, groups in grouped
                for table in (agg.status_table(r),)
                for group, members in groups
                for e in table
                if e["algorithm"] in members
            ),
        )
    emit(
        "paired_gross",
        [
            "run_id",
            "cohort",
            "algorithm",
            "baseline",
            "comparison",
            *_DIST,
            "better",
            "equal",
            "worse",
            "only_algorithm_ok",
            "only_baseline_ok",
            "baseline_output_zero",
            "like_for_like_n",
            "like_for_like_p50",
            "capability_n",
            "capability_p50",
        ],
        (
            [
                r.manifest.run_id,
                r.cohort,
                s["algorithm"],
                s["baseline"],
                s["kind"],
                *_dist(s),
                s["better"],
                s["equal"],
                s["worse"],
                s["only_algorithm"],
                s["only_baseline"],
                s["zero_baseline"],
                s["like_for_like"]["n"],
                s["like_for_like"]["p50"],
                s["capability"]["n"],
                s["capability"]["p50"],
            ]
            for r in runs
            for s in agg.pairwise(r, min_samples)
        ),
    )
    emit(
        "vs_direct_by_stratum",
        [
            "run_id",
            "cohort",
            "split",
            "stratum",
            "algorithm",
            "scheduled",
            "ok",
            "paired_n",
            "p5",
            "p50",
            "p95",
            "better",
            "worse",
            "na_no_direct",
            "underpowered",
        ],
        (
            [
                r.manifest.run_id,
                r.cohort,
                *e["group"],
                e["algorithm"],
                e["scheduled"],
                e["ok"],
                e["n"],
                e.get("p5"),
                e.get("p50"),
                e.get("p95"),
                e.get("better"),
                e.get("worse"),
                e["na_no_direct"],
                e["underpowered"],
            ]
            for r in runs
            for e in agg.grouped_vs_direct(r, "stratum", min_samples)
        ),
    )
    emit(
        "vs_direct_by_pair",
        [
            "run_id",
            "cohort",
            "pair",
            "algorithm",
            "scheduled",
            "ok",
            "paired_n",
            "p5",
            "p50",
            "p95",
            "better",
            "worse",
            "na_no_direct",
            "underpowered",
        ],
        (
            [
                r.manifest.run_id,
                r.cohort,
                *e["group"],
                e["algorithm"],
                e["scheduled"],
                e["ok"],
                e["n"],
                e.get("p5"),
                e.get("p50"),
                e.get("p95"),
                e.get("better"),
                e.get("worse"),
                e["na_no_direct"],
                e["underpowered"],
            ]
            for r in runs
            for e in agg.grouped_vs_direct(r, "pair", min_samples)
        ),
    )
    net_runs = [r for r in runs if agg.net_available(r)]
    if net_runs:
        emit(
            "net_coverage",
            ["run_id", "cohort", "algorithm", "scheduled", "net_rankable", "unranked"],
            (
                [
                    r.manifest.run_id,
                    r.cohort,
                    c["algorithm"],
                    c["scheduled"],
                    c["net_rankable"],
                    c["unranked"],
                ]
                for r in net_runs
                for c in agg.net_coverage(r)
            ),
        )
        emit(
            "paired_net",
            [
                "run_id",
                "cohort",
                "algorithm",
                "baseline",
                "scenario",
                *_DIST,
                "both_ok_unranked",
                "nonpositive_baseline",
                "scenario_reversals",
                "gross_vs_net_reversals",
            ],
            (
                [
                    r.manifest.run_id,
                    r.cohort,
                    p["algorithm"],
                    p["baseline"],
                    scenario,
                    *_dist(p["scenarios"][scenario]),
                    p["both_ok_unranked"],
                    p["nonpositive_baseline"],
                    len(p["scenario_reversals"]),
                    len(p["gross_vs_net_reversals"]),
                ]
                for r in net_runs
                for p in agg.paired_net(r, min_samples)
                for scenario in agg.SCENARIOS
            ),
        )
    emit(
        "latency",
        [
            "run_id",
            "cohort",
            "algorithm",
            "timed_cases",
            "solve_p50_s",
            "solve_p95_s",
            "solve_max_s",
            "quotes_p50",
            "quotes_p95",
            "quotes_max",
            "quotes_total",
            "memory_peak_bytes_p50",
            "memory_peak_bytes_max",
            "prepare_seconds_total",
            "solve_seconds_total",
            "evaluation_seconds_total",
        ],
        (
            [
                r.manifest.run_id,
                r.cohort,
                e["algorithm"],
                e["solve_all"]["n"],
                e["solve_all"]["p50"],
                e["solve_all"]["p95"],
                e["solve_all"]["max"],
                e["quotes_attempted"]["p50"],
                e["quotes_attempted"]["p95"],
                e["quotes_attempted"]["max"],
                e["quotes_total"],
                e["memory_peak_bytes"]["p50"],
                e["memory_peak_bytes"]["max"],
                e["prepare_seconds_total"],
                e["solve_seconds_total"],
                e["evaluation_seconds_total"],
            ]
            for r in runs
            for e in agg.latency_table(r, min_samples)
        ),
    )
    emit(
        "pareto",
        [
            "run_id",
            "cohort",
            "algorithm",
            "common_cases",
            "quality_mean_bps",
            "quality_p50_bps",
            "best_known_share",
            "time_p50_s",
            "time_p95_s",
            "coverage",
            "pareto_efficient",
            "underpowered",
        ],
        (
            [
                r.manifest.run_id,
                r.cohort,
                p["algorithm"],
                p["n"],
                p["quality_mean"],
                p["quality_p50"],
                p["best_known_share"],
                p["time_p50"],
                p["time_p95"],
                p["coverage"],
                p["efficient"],
                p["underpowered"],
            ]
            for r in runs
            for p in agg.pareto(r, min_samples)["points"]
        ),
    )
    emit(
        "source_coverage",
        ["run_id", "cohort", "algorithm", "ok_plans", "source", "plans_using", "bundle_pools"],
        (
            [
                r.manifest.run_id,
                r.cohort,
                row["algorithm"],
                row["ok"],
                source,
                row[source],
                cov["bundle_pools"][source],
            ]
            for r in runs
            for cov in (agg.source_coverage(r),)
            for row in cov["rows"]
            for source in cov["sources"]
        ),
    )
    emit(
        "cases",
        [
            "run_id",
            "cohort",
            "case_id",
            "split",
            "stratum",
            "pair",
            "algorithm",
            "status",
            "gross_output_raw",
            "net_nominal_raw",
            "net_low_cost_raw",
            "net_high_cost_raw",
            "cost_status",
            "topology",
            "solve_seconds",
            "quotes_attempted",
            "quotes_counted",
            "limit_hit",
            "last_valid_candidate",
            "error",
        ],
        (
            [
                r.manifest.run_id,
                r.cohort,
                c,
                r.cases[c].split,
                r.cases[c].stratum,
                agg.pair_label(r, c),
                a,
                row.status,
                None if row.gross is None else str(row.gross),
                *[None if row.net[s] is None else str(row.net[s]) for s in agg.SCENARIOS],
                row.cost_status,
                row.topology,
                row.solve_seconds,
                row.quotes_attempted,
                row.quotes_counted,
                row.limit_hit,
                row.has_last_valid_candidate,
                row.error,
            ]
            for r in runs
            for c in r.case_ids
            for a in r.algorithms
            for row in (r.row(c, a),)
        ),
    )
    return paths


def render_report(
    run_manifest: RunManifest | Sequence[RunManifest],
    output_dir: str | Path | None = None,
    *,
    bundle_dirs: Sequence[str | Path] = (),
    min_samples: int = DEFAULT_MIN_SAMPLES,
    report_command: str = "",
    repo_root: Path | None = None,
) -> ReportPaths:
    """Render one or more loaded runs (each its own cohort section). Default output is
    `<first run dir>/../../reports/<run id>`, i.e. `data/reports/<run id>` for runs under
    `data/results`; a run directory itself is never written to."""
    manifests = [run_manifest] if isinstance(run_manifest, RunManifest) else list(run_manifest)
    if not manifests:
        raise agg.ReportInputError("no runs to report")
    runs = [
        load_run(
            m.run_dir,
            bundle_dirs=bundle_dirs,
            allow_incomplete=not m.complete,
            repo_root=repo_root,
        )
        for m in manifests
    ]
    if output_dir is None:
        first = Path(manifests[0].run_dir)
        output_dir = first.parent.parent / "reports" / first.name
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_paths = _csvs(runs, out, min_samples)
    html_path = out / REPORT_HTML
    html_path.write_text(
        render_html(
            runs,
            min_samples=min_samples,
            csv_files=[p.name for p in csv_paths.values()],
            report_command=report_command,
        ),
        encoding="utf-8",
    )
    return ReportPaths(output_dir=out, html=html_path, csv=csv_paths)

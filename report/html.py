"""Self-contained, static `report.html` (docs/DESIGN.md §2.11).

No server, CDN, script or external resource: CSS is inline, the Pareto chart is inline
SVG, long lists sit in `<details>` (usable with JavaScript disabled). A restrictive
Content-Security-Policy forbids scripts outright, and *every* value that reaches the
page goes through `esc()` -- token symbols, error strings, case ids and manifest fields
are external text and are never trusted as markup.
"""

from __future__ import annotations

import html
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from report import aggregate as agg
from report.aggregate import RunData

CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"

STYLE = """
body{font:14px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:24px;color:#1b1f24}
h1{font-size:22px}h2{font-size:18px;margin-top:32px;border-bottom:2px solid #d0d7de}
h3{font-size:15px;margin-top:22px}
table{border-collapse:collapse;margin:8px 0 14px;font-size:13px}
th,td{border:1px solid #d0d7de;padding:3px 7px;text-align:right;vertical-align:top}
th{background:#f3f5f7}td.l,th.l{text-align:left}
code,.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;word-break:break-all}
.banner{padding:8px 12px;margin:10px 0;border-left:4px solid #bf8700;background:#fff8c5}
.bad{border-left-color:#cf222e;background:#ffebe9}.note{color:#57606a;font-size:12px}
.under{color:#9a6700;font-weight:600}.na{color:#6e7781}.eff{font-weight:700}
section.run{margin-bottom:48px}dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px}
dt{font-weight:600}dd{margin:0}
"""


NOT_MEASURED = "<span class='na'>not measured</span>"


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def na(value: Any, fmt: str = "{}") -> str:
    """Formatted, escaped cell text; `None` renders as a visible N/A."""
    if value is None:
        return '<span class="na">N/A</span>'
    return esc(fmt.format(value))


def bps(value: float | None) -> str:
    return na(value, "{:+.2f}")


def secs(value: float | None) -> str:
    return na(value, "{:.4g}")


def under(flag: bool) -> str:
    if not flag:
        return ""
    return (
        ' <span class="under" title="fewer samples than the declared minimum">⚠ underpowered</span>'
    )


def table(headers: Sequence[str], rows: Iterable[Sequence[str]], left: int = 1) -> str:
    """`rows` cells are already-escaped HTML fragments; headers are escaped here."""
    head = "".join(
        f'<th class="{"l" if i < left else ""}">{esc(h)}</th>' for i, h in enumerate(headers)
    )
    body = "".join(
        "<tr>"
        + "".join(f'<td class="{"l" if i < left else ""}">{c}</td>' for i, c in enumerate(row))
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _dist_cells(d: Mapping[str, Any], fmt: Any = bps) -> list[str]:
    return [
        esc(d["n"]) + under(d["underpowered"]),
        fmt(d["p5"]),
        fmt(d["p25"]),
        fmt(d["p50"]),
        fmt(d["p75"]),
        fmt(d["p95"]),
        fmt(d["mean"]),
    ]


DIST_HEADERS = ["n", "p5", "p25", "p50", "p75", "p95", "mean"]


# ---------------------------------------------------------------------------- sections


def _labels(run: RunData) -> str:
    rows = []
    pins = []
    for algorithm in run.algorithms:
        label = agg.algorithm_label(run, algorithm)
        caps = run.capabilities.get(algorithm, {})
        rows.append(
            [
                f"<code>{esc(algorithm)}</code>",
                esc(label["title"]),
                f"<code>{esc(json.dumps(caps, sort_keys=True))}</code>",
            ]
        )
        prov = label["provenance"]
        if label["kind"] == "scoped_port" and isinstance(prov, dict):
            upstream = prov.get("upstream", {})
            items = [
                ("upstream repository", upstream.get("repository")),
                ("package / version", f"{upstream.get('package')} {upstream.get('version')}"),
                ("commit pin", upstream.get("commit")),
                ("npm integrity", upstream.get("npm_integrity")),
                ("license", upstream.get("license")),
                ("port scope", prov.get("port_scope")),
                ("candidate provider", prov.get("candidate_provider")),
                ("quote provider", prov.get("quote_provider")),
                ("gas-score provider", prov.get("gas_score_provider")),
                ("adaptations", ", ".join(map(str, prov.get("adaptations", [])))),
                ("deviations", ", ".join(map(str, prov.get("deviations", [])))),
                ("contract", prov.get("contract")),
                ("parity evidence", prov.get("parity_evidence")),
            ]
            pins.append(
                f"<h3>Source pin: <code>{esc(algorithm)}</code></h3><dl>"
                + "".join(f"<dt>{esc(k)}</dt><dd class='mono'>{esc(v)}</dd>" for k, v in items)
                + "</dl>"
            )
    metis = [a for a in run.algorithms if "metis" in a.lower()]
    metis_note = (
        ""
        if metis
        else "<p class='note'>The <code>metis_inspired</code> label is reserved for a future "
        "(0.2.0) experimental variant; no Metis-inspired algorithm is part of this run, and "
        "nothing here is Jupiter Metis.</p>"
    )
    return (
        "<h3>Algorithms and what their names claim</h3>"
        + table(["algorithm", "label", "declared capabilities"], rows)
        + metis_note
        + "".join(pins)
    )


def _status(run: RunData) -> str:
    statuses = [s for s in agg.STATUS_ORDER]
    rows = []
    for entry in agg.status_table(run):
        timeout_detail = ", ".join(f"{k}: {v}" for k, v in entry["timeout_by_limit"].items())
        rows.append(
            [
                f"<code>{esc(entry['algorithm'])}</code>",
                esc(entry["scheduled"]),
                *[esc(entry[s]) for s in statuses],
                na(entry["ok_share"], "{:.1%}"),
                esc(timeout_detail) if timeout_detail else "",
                esc(entry["last_valid_candidates"]),
            ]
        )
    return (
        "<h2>Coverage and status over the full schedule</h2>"
        "<p>Every scheduled case is counted for every algorithm: failures are never removed "
        "from the denominator. A <code>timeout</code> with limit <code>quotes</code> or "
        "<code>time</code> is a <b>budget cut-off</b> by the runner (not evidence that no route "
        "exists); <code>solver_budget</code> is the solver's own cooperative budget stop. "
        "Last valid candidates of cut-off solves are kept apart and never counted as solved.</p>"
        + table(
            [
                "algorithm",
                "scheduled",
                *statuses,
                "ok share",
                "timeouts by limit",
                "last valid cand.",
            ],
            rows,
        )
    )


def _vs_direct(run: RunData, min_samples: int) -> str:
    summaries = agg.vs_direct(run, min_samples)
    if summaries and summaries[0].get("absent"):
        return (
            "<h2>Gross quality vs the direct baseline</h2><p><b>N/A</b>: this run has no "
            "<code>direct</code> baseline; see the pairwise table for alternative baselines.</p>"
        )
    rows = [
        [
            f"<code>{esc(s['algorithm'])}</code>",
            esc(s["kind"]),
            *_dist_cells(s),
            esc(s["better"]),
            esc(s["equal"]),
            esc(s["worse"]),
            esc(s["na_no_direct"]),
            esc(s["zero_baseline"]),
            esc(s["only_baseline"]),
        ]
        for s in summaries
    ]
    return (
        "<h2>Gross quality vs the direct baseline (common-success cases)</h2>"
        "<p>Per-case relative improvement <code>(algorithm − direct) / direct</code> in basis "
        "points, summarized over cases both solved; raw amounts of different assets are never "
        "averaged. <b>N/A (no direct)</b> counts cases the algorithm solved where no direct "
        "baseline exists — their ratio is N/A, not zero; so is a case whose direct plan outputs 0 "
        "(dust).</p>"
        + table(
            [
                "algorithm",
                "comparison",
                *DIST_HEADERS,
                "better",
                "equal",
                "worse",
                "N/A (no direct)",
                "N/A (direct output 0)",
                "only direct ok",
            ],
            rows,
        )
    )


def _pairwise(run: RunData, min_samples: int) -> str:
    summaries = agg.pairwise(run, min_samples)
    by = {(s["algorithm"], s["baseline"]): s for s in summaries}
    matrix = []
    for a in run.algorithms:
        cells = [f"<code>{esc(a)}</code>"]
        for b in run.algorithms:
            if a == b:
                cells.append("—")
                continue
            s = by[(a, b)]
            flag = " ⚑" if s["kind"] == "coverage" else ""
            cells.append(
                f"{bps(s['p50'])} <span class='note'>(n={esc(s['n'])}; "
                f"+{esc(s['better'])}/={esc(s['equal'])}/−{esc(s['worse'])})</span>"
                f"{esc(flag)}{under(s['underpowered'])}"
            )
        matrix.append(cells)
    detail = [
        [
            f"<code>{esc(s['algorithm'])}</code>",
            f"<code>{esc(s['baseline'])}</code>",
            esc(s["kind"]),
            *_dist_cells(s),
            esc(s["like_for_like"]["n"]),
            bps(s["like_for_like"]["p50"]),
            esc(s["capability"]["n"]),
            bps(s["capability"]["p50"]),
            esc(s["zero_baseline"]),
            esc(s["only_algorithm"]),
            esc(s["only_baseline"]),
        ]
        for s in summaries
    ]
    return (
        "<h2>Pairwise gross comparisons (row vs column baseline)</h2>"
        "<p>Median per-case bps of the row algorithm over the column baseline on their common "
        "successes. ⚑ marks a <b>coverage</b> comparison: in a non-matched universe one side "
        "is protocol-restricted (e.g. SOR: V2/V3 only) and the other may use sources it "
        "cannot, so the difference includes coverage gains, not only search quality. "
        "<b>Like-for-like</b> splits keep cases whose row-plan topology (single route / "
        "disjoint split / shared pool, and multi-hop) the baseline's declared capabilities "
        "admit; <b>capability</b> cases use a plan shape the baseline cannot produce.</p>"
        + table(["row \\ baseline", *run.algorithms], matrix)
        + "<details><summary>All pairs with distributions and topology split</summary>"
        + table(
            [
                "algorithm",
                "baseline",
                "comparison",
                *DIST_HEADERS,
                "like n",
                "like p50",
                "capability n",
                "capability p50",
                "baseline output 0 (N/A)",
                "only algorithm ok",
                "only baseline ok",
            ],
            detail,
            left=3,
        )
        + "</details>"
    )


def _topology(run: RunData) -> str:
    rows = [
        [
            f"<code>{esc(t['algorithm'])}</code>",
            esc(", ".join(t["declared"])),
            *[esc(t[k]) for k in agg.TOPOLOGIES],
            esc(t["unknown"]),
        ]
        for t in agg.topology_table(run)
    ]
    return "<h3>Plan topology of solved cases</h3>" + table(
        ["algorithm", "declared-capable", *agg.TOPOLOGIES, "unknown"], rows, left=2
    )


def _grouped(run: RunData, min_samples: int) -> str:
    if "direct" not in run.algorithms:
        return ""

    def rows(key: str) -> list[list[str]]:
        return [
            [
                *[esc(g) for g in e["group"]],
                f"<code>{esc(e['algorithm'])}</code>",
                esc(e["scheduled"]),
                esc(e["ok"]),
                esc(e["n"]) + under(e["underpowered"]),
                bps(e.get("p5")),
                bps(e.get("p50")),
                bps(e.get("p95")),
                esc(e.get("better")),
                esc(e.get("worse")),
                esc(e["na_no_direct"]),
            ]
            for e in agg.grouped_vs_direct(run, key, min_samples)
        ]

    tail = [
        "algorithm",
        "scheduled",
        "ok",
        "paired n",
        "p5",
        "p50",
        "p95",
        "better",
        "worse",
        "N/A (no direct)",
    ]
    return (
        "<h2>Per-stratum and per-pair relative improvement vs direct</h2>"
        "<p>Amount strata come from the frozen corpus descriptor; <code>tuning</code> cases "
        "are shown apart from <code>report</code> cases, and state-derived boundary cases are "
        "their own stratum.</p>"
        + table(["split", "stratum", *tail], rows("stratum"), left=3)
        + "<details><summary>Per token pair</summary>"
        + table(["pair", *tail], rows("pair"), left=2)
        + "</details>"
    )


def _net(run: RunData, min_samples: int) -> str:
    if not agg.net_available(run):
        return (
            "<h2>Estimated net output</h2><div class='banner'>This run maximized "
            "<b>gross output</b> (development mode): there are no net scores, so no net "
            "comparison or ranking is shown. Gross and net are never compared directly.</div>"
        )
    banner = ""
    if run.objective_mode == "synthetic_fixed_cost":
        banner = (
            "<div class='banner bad'><b>SYNTHETIC</b> fixed cost: test-only, NOT an empirical "
            "net-output claim; no low/high scenarios exist.</div>"
        )
    coverage = [
        [
            f"<code>{esc(c['algorithm'])}</code>",
            esc(c["scheduled"]),
            esc(c["net_rankable"]),
            esc("; ".join(f"{k}: {v}" for k, v in c["unranked"].items())),
        ]
        for c in agg.net_coverage(run)
    ]
    pairs = agg.paired_net(run, min_samples)
    rows = []
    reversal_rows = []
    for p in pairs:
        cells = [f"<code>{esc(p['algorithm'])}</code>", f"<code>{esc(p['baseline'])}</code>"]
        for scenario in agg.SCENARIOS:
            d = p["scenarios"][scenario]
            cells.append(
                f"{bps(d['p50'])} <span class='note'>(n={esc(d['n'])})</span>"
                + under(d["underpowered"])
            )
        cells += [
            esc(p["both_ok_unranked"]),
            esc(p["nonpositive_baseline"]),
            esc(len(p["scenario_reversals"])),
            esc(len(p["gross_vs_net_reversals"])),
        ]
        rows.append(cells)
        if p["scenario_reversals"] or p["gross_vs_net_reversals"]:
            reversal_rows.append(
                [
                    f"<code>{esc(p['algorithm'])}</code>",
                    f"<code>{esc(p['baseline'])}</code>",
                    esc(", ".join(p["scenario_reversals"][:10])),
                    esc(", ".join(p["gross_vs_net_reversals"][:10])),
                ]
            )
    return (
        "<h2>Estimated net output (nominal and low/high cost scenarios)</h2>"
        + banner
        + "<p>Net output is <code>evaluation.estimated_net_output</code> (gross minus the "
        "complete plan's nominal execution cost in output units); low/high use the cost "
        "model's scenario costs. Scenarios are not confidence intervals. A plan whose cost is "
        "<code>unsupported</code>, <code>low_confidence</code> or <code>unknown_price</code> "
        "is <b>unranked on net</b> and listed below — it keeps its gross result.</p>"
        + table(
            ["algorithm", "scheduled", "net-rankable", "unranked on net (reason: count)"], coverage
        )
        + "<h3>Paired net comparisons (both plans net-rankable; scenario vs same scenario)</h3>"
        + table(
            [
                "algorithm",
                "baseline",
                "nominal p50 bps",
                "low-cost p50 bps",
                "high-cost p50 bps",
                "both ok, unranked",
                "baseline net ≤ 0 (N/A)",
                "scenario rank reversals",
                "gross-vs-net order reversals",
            ],
            rows,
            left=2,
        )
        + "<p class='note'>A scenario rank reversal: the pair's ordering flips between cost "
        "scenarios. A gross-vs-net order reversal compares only orderings; gross and net "
        "magnitudes are never compared.</p>"
        + (
            "<details><summary>Rank-reversal cases (first 10 per pair)</summary>"
            + table(
                ["algorithm", "baseline", "scenario reversals", "gross-vs-net reversals"],
                reversal_rows,
                left=4,
            )
            + "</details>"
            if reversal_rows
            else ""
        )
    )


def _svg(points: Sequence[Mapping[str, Any]]) -> str:
    usable = [p for p in points if p["time_p50"] and p["quality_mean"] is not None]
    if not usable:
        return ""
    w, h, pad = 640, 320, 56
    xs = [math.log10(max(p["time_p50"], 1e-9)) for p in usable]
    ys = [float(p["quality_mean"]) for p in usable]
    x0, x1 = min(xs) - 0.2, max(xs) + 0.2
    y0, y1 = min(ys + [0.0]) - 0.5, max(ys + [0.0]) + 0.5

    def sx(x: float) -> float:
        return pad + (x - x0) / (x1 - x0) * (w - 2 * pad)

    def sy(y: float) -> float:
        return h - pad - (y - y0) / (y1 - y0) * (h - 2 * pad)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" role="img" '
        'aria-label="quality versus measured time">',
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="#fff" stroke="#d0d7de"/>',
        f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" stroke="#57606a"/>',
        f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{h - pad}" stroke="#57606a"/>',
        f'<text x="{w / 2}" y="{h - 16}" text-anchor="middle" font-size="12">'
        "median solve time (s, log scale)</text>",
        f'<text x="14" y="{h / 2}" font-size="12" transform="rotate(-90 14 {h / 2})" '
        'text-anchor="middle">mean bps vs best known</text>',
    ]
    for x, y, p in zip(xs, ys, usable, strict=True):
        color = "#0969da" if p["efficient"] else "#8c959f"
        parts.append(
            f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="5" fill="{color}">'
            f"<title>{esc(p['algorithm'])}</title></circle>"
            f'<text x="{sx(x) + 7:.1f}" y="{sy(y) - 6:.1f}" font-size="11">'
            f"{esc(p['algorithm'])}</text>"
        )
    for tick in range(math.floor(x0), math.ceil(x1) + 1):
        if x0 <= tick <= x1:
            parts.append(
                f'<text x="{sx(tick):.1f}" y="{h - pad + 14}" font-size="10" '
                f'text-anchor="middle">{esc(f"{10.0**tick:g}")}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _pareto(run: RunData, min_samples: int) -> str:
    result = agg.pareto(run, min_samples)
    rows = [
        [
            f"<code>{esc(p['algorithm'])}</code>"
            + (" <span class='eff'>★</span>" if p["efficient"] else ""),
            esc(p["n"]) + under(p["underpowered"]),
            bps(p["quality_mean"]),
            bps(p["quality_p50"]),
            bps(p["quality_p5"]),
            na(p["best_known_share"], "{:.1%}"),
            secs(p["time_p50"]),
            secs(p["time_p95"]),
            na(p["coverage"], "{:.1%}"),
        ]
        for p in result["points"]
    ]
    return (
        "<h2>Quality versus measured time (Pareto view)</h2>"
        f"<p>Over the {esc(result['common_cases'])} case(s) every algorithm solved: per-case "
        "shortfall from the best known gross output among this run's algorithms (0 = best "
        "known) against median measured solve time. ★ marks Pareto-efficient algorithms. "
        "There is deliberately no single weighted score; coverage is shown alongside because "
        "the common-success set hides each algorithm's failures.</p>"
        + table(
            [
                "algorithm",
                "cases",
                "mean bps",
                "p50 bps",
                "p5 bps",
                "best-known share",
                "solve p50 s",
                "solve p95 s",
                "coverage (ok share)",
            ],
            rows,
        )
        + _svg(result["points"])
    )


def _latency(run: RunData, min_samples: int) -> str:
    rows = []
    for entry in agg.latency_table(run, min_samples):
        s, q, m = entry["solve_all"], entry["quotes_attempted"], entry["memory_peak_bytes"]
        rows.append(
            [
                f"<code>{esc(entry['algorithm'])}</code>",
                esc(s["n"]) + under(s["underpowered"]),
                secs(s["p50"]),
                secs(s["p95"]),
                secs(s["max"]),
                secs(entry["solve_ok"]["p50"]),
                na(q["p50"], "{:.0f}"),
                na(q["p95"], "{:.0f}"),
                na(q["max"], "{:.0f}"),
                esc(entry["quotes_total"]),
                na(m["p50"], "{:.0f}") if entry["memory_measured"] else NOT_MEASURED,
                na(m["max"], "{:.0f}") if entry["memory_measured"] else "",
                secs(entry["prepare_seconds_total"]),
                secs(entry["solve_seconds_total"]),
                secs(entry["evaluation_seconds_total"]),
            ]
        )
    return (
        "<h2>Latency, quote counts and memory</h2>"
        "<p>Solve time is the median of each case's measured repeats (monotonic clock), over "
        "every recorded case; preparation and external evaluation are charged separately. "
        "Wall-clock timing is measurement, not a reproducibility guarantee. Memory comes only "
        "from a separate memory pass.</p>"
        + table(
            [
                "algorithm",
                "timed cases",
                "solve p50 s",
                "solve p95 s",
                "solve max s",
                "ok-only p50 s",
                "quotes p50",
                "quotes p95",
                "quotes max",
                "quotes total",
                "peak bytes p50",
                "peak bytes max",
                "prepare s",
                "solve s total",
                "evaluation s total",
            ],
            rows,
        )
    )


def _trace(run: RunData, algorithm: str, case_id: str, why: str) -> str:
    row = run.row(case_id, algorithm)
    context = run.cases[case_id]
    head = (
        f"<h3><code>{esc(algorithm)}</code> · <code>{esc(case_id)}</code> "
        f"<span class='note'>{esc(why)}</span></h3>"
        f"<p>{esc(agg.pair_label(run, case_id))}, amount_in {na(context.amount_in)} (raw), "
        f"stratum {esc(context.stratum)}; status <b>{esc(row.status)}</b>"
        + (f", gross {esc(row.gross)} (raw)" if row.gross is not None else "")
        + (f", topology {esc(row.topology)}" if row.topology else "")
        + (f", cost {esc(row.cost_status)}" if row.cost_status else "")
        + "</p>"
    )
    record = row.record or {}
    if row.error:
        head += f"<p>error: <code>{esc(row.error)}</code></p>"
    evaluation = record.get("evaluation") or {}
    steps = evaluation.get("trace") if isinstance(evaluation, dict) else None
    if not steps:
        return head
    rows = [
        [
            esc(step.get("step")),
            f"<code>{esc(step.get('pool_id'))}</code>",
            esc(agg.token_label(run, step.get("token_in"))),
            esc(agg.token_label(run, step.get("token_out"))),
            esc(", ".join(f"{i.get('fund_id')}:{i.get('amount')}" for i in step.get("inputs", []))),
            esc(step.get("amount_in")),
            esc(step.get("amount_out")),
            esc(step.get("output_fund_id")),
        ]
        for step in steps
    ]
    return head + table(
        ["step", "pool", "in", "out", "inputs (fund:raw)", "amount in", "amount out", "fund"],
        rows,
        left=5,
    )


def _traces(run: RunData) -> str:
    return "<h2>Representative plan traces</h2>" + "".join(
        _trace(run, t["algorithm"], t["case_id"], t["why"]) for t in agg.representative_traces(run)
    )


def _errors(run: RunData) -> str:
    groups = agg.error_groups(run)
    rows = [
        [
            f"<code>{esc(g['algorithm'])}</code>",
            esc(g["status"]),
            na(g["limit_hit"]),
            f"<code>{esc(g['error'])}</code>" if g["error"] else "",
            esc(g["count"]),
            f"<code>{esc(', '.join(g['examples']))}</code>",
        ]
        for g in groups
    ]
    return f"<h2>All non-ok outcomes ({esc(sum(g['count'] for g in groups))})</h2>" + (
        table(["algorithm", "status", "limit", "error", "count", "example cases"], rows, left=4)
        if rows
        else "<p>None.</p>"
    )


def _provenance(run: RunData) -> str:
    p = agg.provenance(run)
    order = [
        ("replay command", p["replay_command"]),
        ("run id / state", f"{p['run_id']} / {p['state']}"),
        ("bundle", f"{p['bundle_id']} sha256 {p['bundle_hash']}"),
        ("profile", f"{p['profile_path']} sha256 {p['profile_sha256']}"),
        ("objective", p["objective_label"]),
        ("cost model sha256", p["cost_model_sha256"]),
        ("price context sha256", p["price_context_sha256"]),
        ("experiment id", p["experiment_id"]),
        ("git revision", p["git_revision"]),
        ("git dirty / diff sha256", f"{p['git_dirty']} / {p['git_diff_sha256']}"),
        ("cases.jsonl sha256", p["cases_sha256"]),
        ("memory.jsonl sha256", p["memory_sha256"]),
        ("created / finished", f"{p['created_at']} / {p['finished_at']}"),
        ("measurement", json.dumps(p["measurement"], sort_keys=True)),
        ("environment", json.dumps(p["environment"], sort_keys=True)),
        ("case labels", p["case_labels"]),
    ]
    return (
        "<h2>Provenance and replay</h2><dl>"
        + "".join(f"<dt>{esc(k)}</dt><dd class='mono'>{na(v)}</dd>" for k, v in order)
        + "</dl>"
    )


def _run_section(run: RunData, min_samples: int) -> str:
    m = run.manifest
    banners = []
    if not m.complete:
        banners.append(
            f"<div class='banner bad'>Run state <b>{esc(m.state)}</b>: records do not cover the "
            "whole schedule; unrecorded cases are counted as <code>missing</code>.</div>"
        )
    scope = run.evaluation_scope
    banners.append(
        f"<div class='banner{'' if scope == agg.SCOPE_HELD_OUT else ' bad'}'>"
        f"Evaluation scope: <b>{esc(agg.SCOPE_TITLES[scope])}</b></div>"
    )
    banners.append(f"<div class='banner'>Objective: {esc(m.objective_label)}</div>")
    return (
        f"<section class='run' id='run-{esc(m.run_id)}'>"
        f"<h1>Run <code>{esc(m.run_id)}</code></h1>"
        f"<p><b>{esc(agg.COHORT_TITLES[run.cohort])}</b> · bundle <code>{esc(m.bundle_id)}</code> "
        f"· {esc(len(run.case_ids))} case(s) × {esc(len(run.algorithms))} algorithm(s)</p>"
        + "".join(banners)
        + _labels(run)
        + _status(run)
        + _vs_direct(run, min_samples)
        + _pairwise(run, min_samples)
        + _topology(run)
        + _grouped(run, min_samples)
        + _net(run, min_samples)
        + _pareto(run, min_samples)
        + _latency(run, min_samples)
        + _traces(run)
        + _errors(run)
        + _provenance(run)
        + "</section>"
    )


def render_html(
    runs: Sequence[RunData],
    *,
    min_samples: int,
    csv_files: Sequence[str],
    report_command: str,
) -> str:
    cohorts = [
        f"<li><a href='#run-{esc(r.manifest.run_id)}'>{esc(r.manifest.run_id)}</a> — "
        f"{esc(agg.COHORT_TITLES[r.cohort])} · <b>{esc(r.evaluation_scope)}</b> · "
        f"{esc(r.objective_mode)}</li>"
        for r in runs
    ]
    links = "".join(f"<li><a href='{esc(name)}'>{esc(name)}</a></li>" for name in csv_files)
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta http-equiv='Content-Security-Policy' content=\"{CSP}\">"
        "<title>Mantle router benchmark report</title>"
        f"<style>{STYLE}</style></head><body>"
        "<h1>Mantle router benchmark — offline report</h1>"
        "<p>Built only from versioned run records (and the hash-verified frozen bundle "
        "descriptor for case labels); no live chain data. Each run is its own cohort: matched "
        "V2/V3 results and full-coverage results are never pooled.</p>"
        f"<ul>{''.join(cohorts)}</ul>"
        f"<p class='note'>Samples with fewer than {esc(min_samples)} paired cases are marked "
        "⚠ underpowered. Regenerate with: "
        f"<code>{esc(report_command)}</code></p>"
        f"<p>CSV summaries:</p><ul>{links}</ul>"
        + "".join(_run_section(run, min_samples) for run in runs)
        + "</body></html>\n"
    )

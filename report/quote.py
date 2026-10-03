"""Text presentation of one exploratory single-request run (WHI-1498), from saved records
only: the run manifest and `cases.jsonl` plus the hash-verified request bundle (its
provenance carries the request, the parent bundle identity and the token metadata; its
pools file the source of every pool).

`render_compact` prints one row per selected algorithm, failures included; a run that
persisted its strategy groups (`main.py quote --strategies`, WHI-1528) shows them as
separate "Base strategies" / "Optimized strategies" / "Experimental and other strategies"
blocks, older runs keep one ungrouped table (and `render_details` the same group headings).
A Metis-inspired algorithm's recorded label settings (WHI-1540) are printed in the header,
so its hop domain is never presented as the shared `search.max_hops`. Groups are a presentation: the
header prints the recorded execution order separately, since a profile's custom algorithm
may run between its base ones. `render_details` adds, for EVERY
algorithm, the final plan exactly as the independent evaluation replayed it -- steps in
execution order, the fund ledger (how each fund was split between later steps, which steps
merged funds, which physical pools were reused) with exact raw amounts that reconcile --
and this one execution's timings. Nothing here re-runs a solver or
re-evaluates a plan.

A single execution has one latency observation per algorithm, reported as that: this
module never prints sample lists or distribution statistics.

Research diagnostics (WHI-1548, R021-C/1 §9.5): a record carrying the runner's validated
`diagnostics` view gets one bound line in the compact output and, in the details, its bound
(`certified [lower, upper] gap g`, `estimate v (not a bound)`, `unknown (no bound)`,
`unavailable (<reason>)` or `invalid certificate (<codes>)`), domain hash prefix and grid
kind, `max_candidates` unit, named work counters, fallback/repair and scope. A record without
one (every existing algorithm, every older run) prints exactly as before.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from benchmark.diagnostics import (
    bound_text,
    domain_text,
    fallback_text,
    read_view,
    scope_text,
    stages_text,
    work_text,
)
from benchmark.results import RunManifest, load_case_records, load_manifest
from report.aggregate import (
    STRATEGY_GROUP_TITLES,
    ReportInputError,
    _pool_sources,
    _read_checked,
    _verified_bundle,
    bundle_path_from_replay,
    metis_settings,
    strategy_groups,
    strategy_recipe,
)
from snapshot.request import EXPLORATORY_MARK, REQUEST_SCHEMA, format_amount

REQUEST_FUND = "REQUEST"
SOR = "uni_sor_port"


@dataclass(frozen=True)
class QuoteView:
    manifest: RunManifest
    records: list[dict[str, Any]]
    provenance: dict[str, Any]  # the request bundle's provenance record
    pool_sources: dict[str, str]
    quote_record: dict[str, Any] | None  # quote.json next to the effective profile, if verified

    @property
    def tokens(self) -> dict[str, dict[str, Any]]:
        return dict(self.provenance["tokens"])

    @property
    def request(self) -> dict[str, Any]:
        return dict(self.provenance["request"])


def load_quote_view(
    run_dir: str | Path,
    *,
    bundle_dirs: list[str] | tuple[str, ...] = (),
    repo_root: Path | None = None,
    allow_incomplete: bool = False,
) -> QuoteView | None:
    """The view of an exploratory single-request run, or `None` when `run_dir` is an
    ordinary run. A run whose bundle id marks it exploratory but whose request bundle cannot
    be found and hash-verified is refused (`ReportInputError`), never reported as an
    ordinary run with distribution statistics."""
    manifest = load_manifest(run_dir, allow_incomplete=allow_incomplete)
    candidates = [Path(p) for p in bundle_dirs]
    replay_bundle = bundle_path_from_replay(manifest.replay_command)
    if replay_bundle is not None:
        candidates.append(replay_bundle)
        if not replay_bundle.is_absolute() and repo_root is not None:
            candidates.append(repo_root / replay_bundle)
    bundle_dir, checksums, note = _verified_bundle(manifest, candidates)
    provenance: Any = {}
    if bundle_dir is not None:
        provenance_text = _read_checked(bundle_dir, checksums, "provenance.json")
        provenance = json.loads(provenance_text) if provenance_text else {}
    if not isinstance(provenance, dict) or provenance.get("schema") != REQUEST_SCHEMA:
        if EXPLORATORY_MARK in manifest.bundle_id:
            raise ReportInputError(
                f"{run_dir}: exploratory single-request run (bundle {manifest.bundle_id}) whose "
                f"request bundle is unavailable ({note}); pass --bundle <quote dir>/bundle"
            )
        return None
    pool_sources = _pool_sources(bundle_dir, checksums)
    quote_record = None
    quote_path = Path(manifest.profile_path).parent / "quote.json"
    if quote_path.is_file():
        candidate = json.loads(quote_path.read_text(encoding="utf-8"))
        if (
            candidate.get("effective_profile", {}).get("sha256") == manifest.profile_sha256
            and candidate.get("request_bundle", {}).get("bundle_hash") == manifest.bundle_hash
        ):
            quote_record = candidate
    return QuoteView(
        manifest=manifest,
        records=load_case_records(run_dir),
        provenance=provenance,
        pool_sources=pool_sources,
        quote_record=quote_record,
    )


# ------------------------------------------------------------------ formatting


def _short(address: str) -> str:
    return (
        f"{address[:6]}…{address[-4:]}"
        if address.startswith("0x") and len(address) > 12
        else address
    )


def _token(view: QuoteView, address: str) -> tuple[str, int | None]:
    meta = view.tokens.get(address.lower())
    if meta is None:
        return address, None
    return str(meta.get("symbol") or address), int(meta["decimals"])


def _amount(view: QuoteView, address: str, raw: int) -> str:
    symbol, decimals = _token(view, address)
    if decimals is None:
        return f"{raw} raw {symbol}"
    return f"{format_amount(raw, decimals)} {symbol} ({raw} raw)"


def _percent(part: int, whole: int) -> str:
    if whole == 0:
        return "N/A"
    exact = Fraction(part * 100, whole)
    shown = Decimal(exact.numerator) / Decimal(exact.denominator)
    rounded = shown.quantize(Decimal("0.000001"))
    text = format(rounded.normalize(), "f")
    return f"{text}%" if Fraction(rounded) == exact else f"≈{text}%"


def _na(value: Any) -> str:
    return "N/A" if value is None else str(value)


def _seconds(value: Any) -> str:
    return "N/A" if value is None else f"{float(value):.6f} s"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]

    def line(cells: list[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip()

    return [line(header), line(["-" * w for w in widths]), *(line(r) for r in rows)]


def _records_by_algorithm(view: QuoteView) -> list[tuple[str, dict[str, Any] | None]]:
    by_name = {str(r["algorithm"]): r for r in view.records}
    return [(name, by_name.get(name)) for name in view.manifest.algorithms]


def _gross(record: dict[str, Any] | None) -> int | None:
    if record is None or record["status"] != "ok" or record.get("evaluation") is None:
        return None
    return int(record["evaluation"]["gross_output"])


def _solve_latency(record: dict[str, Any]) -> float | None:
    samples = record.get("measurement", {}).get("solve_seconds") or []
    return float(samples[0]) if samples else None


def _net_applicable(view: QuoteView) -> bool:
    mode = view.manifest.resolved_profile.get("objective", {}).get("mode")
    return mode not in (None, "gross_only")


# ------------------------------------------------------------------ header


def render_header(view: QuoteView) -> list[str]:
    m, prov, req = view.manifest, view.provenance, view.request
    tin, tout = req["token_in"], req["token_out"]
    block = prov["block"]
    measurement = m.measurement
    scope = prov["pool_scope"]
    env = m.environment
    lines = [
        f"EXPLORATORY single-request comparison ({prov['label']})",
        f"request: {req['amount_in']} {tin['symbol']} -> {tout['symbol']} (exact input)",
        f"  token in : {tin['symbol']} {tin['address']} decimals {tin['decimals']}; "
        f"raw input {req['amount_in_raw']}",
        f"  token out: {tout['symbol']} {tout['address']} decimals {tout['decimals']}",
        f"snapshot: chain {block['chain_id']} block {block['number']} hash {block['hash']}",
        f"  parent bundle {prov['derived_from']['bundle_id']} "
        f"(bundle_hash {prov['derived_from']['bundle_hash']}), unchanged",
        f"  request bundle {m.bundle_id} (bundle_hash {m.bundle_hash})",
        f"  pools: {scope['pools']} across "
        + ", ".join(f"{k} {v}" for k, v in scope["sources"].items()),
    ]
    envelope = prov["envelope"]
    if envelope["within"] is False:
        lines.append(
            f"  WARNING: input exceeds the parent corpus envelope for {tin['symbol']} "
            f"({envelope['token_in_max_raw']} raw); collected pool state (CL ticks, LB bins) "
            "may not cover it -- an incomplete_snapshot outcome means uncollected state, "
            "not missing liquidity"
        )
    source = (view.quote_record or {}).get("source_profile")
    source_text = f"{source['path']} (sha256 {source['sha256'][:12]}) -> " if source else ""
    lines += [
        f"profile: {source_text}{m.profile_path} (sha256 {(m.profile_sha256 or '?')[:12]})",
        f"  objective: {m.objective_label}",
        f"  measurement: warmup {measurement.get('warmup')}, repeats {measurement.get('repeats')}, "
        f"memory_pass {str(measurement.get('memory_pass')).lower()} -- one solve attempt per "
        "algorithm",
        f"  budget: {json.dumps(m.resolved_profile.get('budget', {}), sort_keys=True)}",
        f"environment: git {env.get('git_revision') or '?'}"
        + (" (dirty)" if env.get("git_dirty") else "")
        + f", Python {env.get('python_version') or '?'}, "
        f"{env.get('cpu_model') or env.get('machine') or 'CPU unknown'}",
    ]
    groups = strategy_groups(m)
    metis = {n: note for n in m.algorithms if (note := metis_settings(m, n)) is not None}
    if groups is not None:
        mode = m.resolved_profile.get("selection", {}).get("mode")
        lines.append(
            f"strategies: --strategies {mode} -- "
            + "; ".join(f"{STRATEGY_GROUP_TITLES[g]} ({len(n)})" for g, n in groups)
            + " (groups are a presentation, not the schedule), under the same objective, "
            + (
                "budget and search"
                if not metis
                else "budget and search.* values; the Metis-inspired label search adds its "
                "own recorded hop/chunk/pruning settings (below)"
            )
        )
        lines.append(f"  execution order (sequential, as recorded): {', '.join(m.algorithms)}")
        for group, names in groups:
            for name in names:
                entry = strategy_recipe(m, name)
                if group == "optimized" and entry is not None:
                    recipe = entry.get("recipe", {})
                    lines.append(
                        f"  {name}: experimental optimized strategy, recipe {recipe.get('key')} "
                        f"arm {recipe.get('arm')} ({recipe.get('path')}); exact controls "
                        f"{', '.join(entry.get('controls') or {}) or 'none'}; not a default"
                    )
    for name, note in metis.items():
        lines.append(
            f"  {name}: experimental Metis-inspired Python variant, NOT Jupiter Metis (no "
            f"production equivalence); {note}; included for comparison only, not adopted as a "
            "production routing default"
        )
    if SOR in m.algorithms:
        lb = sum(v for k, v in scope["sources"].items() if k.startswith("moe_lb"))
        lines.append(
            f"scope: {SOR} is a scoped Uniswap SOR V2/V3 port -- it does not see the {lb} "
            "Liquidity Book pool(s) the other algorithms may use"
        )
    lines.append(
        "latency is one observation of this execution, not a stable performance "
        "distribution or a production latency claim"
    )
    return lines


# ------------------------------------------------------------------ compact


def render_compact(view: QuoteView) -> str:
    tout = view.request["token_out"]
    records = _records_by_algorithm(view)
    direct = dict(records).get("direct")
    direct_gross = _gross(direct)
    net = _net_applicable(view)
    header = ["algorithm", "status", f"gross out ({tout['symbol']})", "gross raw", "vs direct"]
    header += ["net raw"] if net else []
    header += ["solve latency", "quotes counted"]
    rows: list[list[str]] = []
    notes: list[str] = []
    for name, record in records:
        if record is None:
            rows.append(
                [name, "missing", "N/A", "N/A", "N/A", *(["N/A"] if net else []), "N/A", "N/A"]
            )
            continue
        gross = _gross(record)
        if gross is None or direct_gross is None or direct_gross == 0:
            gain = "N/A"
        else:
            bps = Decimal((gross - direct_gross) * 10_000) / Decimal(direct_gross)
            gain = f"{bps.quantize(Decimal('0.01')):+} bps"
        row = [
            name,
            record["status"],
            "N/A" if gross is None else format_amount(gross, int(tout["decimals"])),
            "N/A" if gross is None else str(gross),
            gain,
        ]
        if net:
            value = (record.get("evaluation") or {}).get("estimated_net_output")
            row.append("N/A" if value is None or gross is None else str(value))
        latency = _solve_latency(record)
        quotes = record.get("quotes", {}).get("counted")
        row += [
            "N/A" if latency is None else _seconds(latency),
            "N/A" if quotes is None else str(quotes),
        ]
        rows.append(row)
        if record["status"] != "ok":
            limit = f" (limit: {record['limit_hit']})" if record.get("limit_hit") else ""
            notes.append(
                f"  {name}: {record['status']}{limit}: {record.get('error') or 'no detail'}"
            )
    table = _table(header, rows)
    groups = strategy_groups(view.manifest)
    # Each group's rows under its own heading, in the recorded order within the group (the
    # execution sequence itself is printed in the header; a custom algorithm may run between
    # base ones).
    if groups is not None:
        by_name = dict(zip((n for n, _ in records), table[2:], strict=True))
        table = table[:2] + [
            line
            for group, names in groups
            for line in (f"{STRATEGY_GROUP_TITLES[group]}:", *(by_name[n] for n in names))
        ]
    lines = [*render_header(view), "", *table]
    if direct_gross is None:
        status = "not selected" if direct is None else direct["status"]
        lines.append(f"vs direct: N/A -- the direct baseline has no valid output ({status})")
    elif direct_gross == 0:
        lines.append("vs direct: N/A -- the direct baseline output is zero")
    bounds = [
        f"  {name}: {bound_text(diagnostics)}"
        for name, record in records
        if record is not None and (diagnostics := read_view(record)) is not None
    ]
    if bounds:
        lines += [
            "",
            "research diagnostics (R021-C/1; checked by the runner against this run and request; "
            "--details for domain and work):",
            *bounds,
        ]
    if notes:
        lines += ["", "non-ok outcomes:", *notes]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ details


def _plan_lines(view: QuoteView, evaluation: dict[str, Any], indent: str) -> list[str]:
    """Steps in execution order, then the fund ledger, then reconciliation."""
    trace = evaluation.get("trace") or []
    funds = evaluation.get("funds") or []
    lines = [
        f"{indent}execution order (as replayed; a reused pool sees the state earlier steps left):"
    ]
    pool_uses: dict[str, list[int]] = {}
    for step in trace:
        n = int(step["step"])
        pool = str(step["pool_id"])
        pool_uses.setdefault(pool.lower(), []).append(n)
        tin, tout = str(step["token_in"]), str(step["token_out"])
        sin, _ = _token(view, tin)
        sout, _ = _token(view, tout)
        source = view.pool_sources.get(pool.lower(), "unknown source")
        inputs = " + ".join(f"{i['fund_id']} {int(i['amount'])} raw" for i in step["inputs"])
        merged = " (merge)" if len(step["inputs"]) > 1 else ""
        lines += [
            f"{indent}  {n}. {source} pool {pool}: {sin} ({tin}) -> {sout} ({tout})",
            f"{indent}     in : {inputs}{merged} = {_amount(view, tin, int(step['amount_in']))}",
            f"{indent}     out: {_amount(view, tout, int(step['amount_out']))} -> fund "
            f"{step['output_fund_id']} [{step['status']}]",
        ]
    shared = {p: s for p, s in pool_uses.items() if len(s) > 1}
    for pool, steps in shared.items():
        lines.append(
            f"{indent}  shared pool {pool}: used by steps "
            f"{', '.join(map(str, steps))} in that order"
        )

    consumers: dict[str, list[tuple[int, int]]] = {}
    for step in trace:
        for ref in step["inputs"]:
            consumers.setdefault(str(ref["fund_id"]), []).append(
                (int(step["step"]), int(ref["amount"]))
            )
    residuals = {str(k): int(v) for k, v in (evaluation.get("residuals") or {}).items()}
    token_out = view.request["token_out"]["address"]
    problems: list[str] = []
    terminal: list[tuple[str, int]] = []
    lines.append(f"{indent}fund ledger (allocation of every fund, exact raw units):")
    for fund in funds:
        fid, token = str(fund["fund_id"]), str(fund["token"])
        produced, consumed, remaining = (
            int(fund[k]) for k in ("produced", "consumed", "remaining")
        )
        origin = (
            "request input"
            if fund["producer_step"] is None
            else f"output of step {fund['producer_step']}"
        )
        lines.append(f"{indent}  {fid}: {_amount(view, token, produced)}, {origin}")
        uses = consumers.get(fid, [])
        for index, (n, amount) in enumerate(uses):
            drains = index == len(uses) - 1 and remaining == 0 and len(uses) > 1
            tail = (
                " -- drains the remaining balance (final allocation, incl. any integer remainder)"
                if drains
                else ""
            )
            lines.append(
                f"{indent}    -> step {n}: {amount} raw = "
                f"{_percent(amount, produced)} of {produced} raw{tail}"
            )
        if sum(a for _, a in uses) != consumed:
            problems.append(f"{fid}: step inputs {sum(a for _, a in uses)} != consumed {consumed}")
        if remaining:
            if token.lower() == token_out.lower() and fid not in residuals:
                terminal.append((fid, remaining))
                lines.append(f"{indent}    terminal output: {remaining} raw")
            else:
                lines.append(f"{indent}    RESIDUAL (unallocated): {remaining} raw")
    gross = int(evaluation["gross_output"])
    total = sum(v for _, v in terminal)
    lines.append(
        f"{indent}terminal outputs: "
        + (" + ".join(f"{f} {v} raw" for f, v in terminal) or "none")
        + f" = {total} raw; evaluated gross output {gross} raw"
    )
    if total != gross:
        problems.append(f"terminal outputs {total} != gross output {gross}")
    request = next((f for f in funds if f["fund_id"] == REQUEST_FUND), None)
    if request is not None and int(request["produced"]) != int(view.request["amount_in_raw"]):
        problems.append("request fund differs from the request amount")
    lines.append(
        f"{indent}residuals: " + (", ".join(f"{k} {v} raw" for k, v in residuals.items()) or "none")
    )
    lines.append(
        f"{indent}reconciliation: "
        + (
            "allocations and terminal totals reconcile exactly"
            if not problems
            else "FAILED: " + "; ".join(problems)
        )
    )
    return lines


def _performance_lines(view: QuoteView, record: dict[str, Any]) -> list[str]:
    m = record.get("measurement", {})
    events = view.manifest.prepare_events
    index = m.get("prepare_event")
    event = events[index] if isinstance(index, int) and 0 <= index < len(events) else {}
    latency = _solve_latency(record)
    solve = (
        _seconds(latency)
        if latency is not None
        else (
            f"not completed (cut off after {_seconds(m.get('elapsed_seconds'))})"
            if m.get("elapsed_seconds") is not None
            else "N/A (no solve attempt)"
        )
    )
    quotes = record.get("quotes") or {}
    if record.get("evaluation") is not None:
        evaluation = _seconds(m.get("evaluation_seconds"))
    elif record.get("last_valid_candidate"):
        evaluation = "not recorded (the partial candidate was evaluated; its duration is not)"
    else:
        evaluation = "N/A (no plan to evaluate)"
    # A solver that never returned (killed, crashed, prepare failed) reported no search
    # counters; the record's zero defaults are not measurements.
    if record.get("solver_reported") is not None:
        search = (
            f"candidates considered {record.get('candidates_considered')}, "
            f"truncated {record.get('candidates_truncated')}"
        )
    else:
        search = "candidates considered/truncated N/A (the solver returned no counters)"
    return [
        "  performance (this single execution):",
        f"    preparation {_seconds(event.get('prepare_seconds'))}, worker start-up "
        f"{_seconds(event.get('startup_seconds'))}",
        f"    solve {solve}",
        f"    final independent evaluation {evaluation}",
        f"    quotes counted {_na(quotes.get('counted'))}, "
        f"attempted {_na(quotes.get('attempted'))}",
        f"    {search}, candidates reported {_na(m.get('candidates_reported'))}",
        f"    limit hit: {record.get('limit_hit') or 'none'}; solve attempts completed "
        f"{m.get('attempts_completed', 0)}",
    ]


def _bound_pruning_lines(record: dict[str, Any]) -> list[str]:
    """WHI-1599 (R022-Q02/1 §10.4): the recorded `search.bound_pruning` block of a bounded
    strategy -- its own counters, never folded into the reference's; empty for any other."""
    search = record.get("search")
    block = search.get("bound_pruning") if isinstance(search, dict) else None
    if not isinstance(block, dict):
        return []
    prepare: dict[str, Any] = block["prepare"] if isinstance(block.get("prepare"), dict) else {}
    exactness: dict[str, Any] = (
        block["exactness"] if isinstance(block.get("exactness"), dict) else {}
    )
    binding = exactness.get("binding") or []
    chunk: list[str] = []
    if "p0" in block:  # WHI-1600: the chunk searches (incremental_graph, metis_history)
        chunk.append(
            f"    retained simpler candidate present (P0): {block['p0']}"
            + (
                f"; rule M2 active: {block['m2'].get('active')} (gate {block['m2'].get('gate')})"
                if isinstance(block.get("m2"), dict)
                else ""
            )
        )
    return [
        f"  bound pruning ({block.get('contract')} rule {block.get('rule')}: exact acceleration "
        f"of {block.get('reference')}):",
        f"    pruned by bound {block.get('pruned_bound')} (not failures), bound evaluations "
        f"{block.get('bound_evaluations')}, evaluations without a bound "
        f"{block.get('bound_no_bound')}, bound table cost {block.get('bound_table_cost')}",
        f"    bound table (built in preparation): {prepare.get('pool_directions')} pool "
        f"directions, {prepare.get('bounded')} bounded, {prepare.get('rate_only')} rate only, "
        f"{prepare.get('no_bound')} no bound",
        *chunk,
        f"    exactness: {exactness.get('label')}"
        + (f" (binding: {', '.join(map(str, binding))})" if binding else ""),
    ]


def _diagnostics_lines(record: dict[str, Any]) -> list[str]:
    """The record's research-diagnostics block; empty for a record without one."""
    view = read_view(record)
    if view is None:
        return []
    origin = ""
    if view.get("origin") == "runner":
        origin = (
            " (observed by the runner: the solve returned without diagnostics)"
            if view.get("reason") == "not_produced"
            else " (observed by the runner: no certificate survives a cut-off or failed solve)"
        )
    lines = [
        "  research diagnostics (R021-C/1; checked by the runner against its own run identity, "
        "request and evaluated score, never the solver's claims):",
        f"    bound: {bound_text(view)}{origin}",
    ]
    if view["state"] == "invalid" or view.get("unvalidated"):
        lines += [f"      - {detail}" for detail in view.get("details") or []]
        return lines
    unit = view.get("max_candidates_unit")
    for label, text in (
        ("domain", domain_text(view)),
        ("max_candidates unit", unit if isinstance(unit, str) else None),
        ("work (named units; different units are never divided)", work_text(view)),
        ("fallback/repair", fallback_text(view)),
        ("scope", scope_text(view)),
        ("stages (observed seconds, not budgets)", stages_text(view)),
    ):
        if text is not None:
            lines.append(f"    {label}: {text}")
    return lines


def render_details(view: QuoteView) -> str:
    lines: list[str] = []
    records = dict(_records_by_algorithm(view))
    groups = strategy_groups(view.manifest)
    heading = {names[0]: STRATEGY_GROUP_TITLES[g] for g, names in groups or [] if names}
    order = [n for _, names in groups for n in names] if groups else list(records)
    for name in order:
        record = records[name]
        if name in heading:
            lines += ["", f"== {heading[name]} =="]
        lines.append("")
        if record is None:
            lines.append(f"[{name}] missing: no record (incomplete run)")
            continue
        lines.append(f"[{name}] {record['status']}")
        evaluation = record.get("evaluation")
        if record["status"] == "ok" and evaluation is not None:
            token_out = view.request["token_out"]["address"]
            gross = int(evaluation["gross_output"])
            lines.append(f"  gross output {_amount(view, token_out, gross)}")
            if evaluation.get("estimated_net_output") is not None:
                lines.append(f"  estimated net output {evaluation['estimated_net_output']} raw")
            lines += _plan_lines(view, evaluation, "  ")
        else:
            if record.get("limit_hit"):
                lines.append(f"  limit hit: {record['limit_hit']}")
            lines.append(f"  error: {record.get('error') or 'none recorded'}")
            lines.append("  no completed route (nothing is fabricated for a failed solve)")
            if evaluation is not None:  # invalid_plan: the rejected plan's replay, labelled
                lines.append(
                    "  rejected plan as replayed by the independent evaluation (NOT a valid route):"
                )
                lines += _plan_lines(view, evaluation, "    ")
            candidate = record.get("last_valid_candidate")
            if candidate:
                lines.append(f"  PARTIAL DIAGNOSTIC -- {candidate['label']}:")
                lines += _plan_lines(view, candidate["evaluation"], "    ")
        lines += _performance_lines(view, record)
        lines += _bound_pruning_lines(record)
        lines += _diagnostics_lines(record)
    return "\n".join(lines) + "\n"


__all__ = ["QuoteView", "ReportInputError", "load_quote_view", "render_compact", "render_details"]

"""Aggregate versioned run records into report tables (docs/DESIGN.md §2.11, §4.4 flow 3).

Input is only what a run persisted: the schema-2 `manifest.json` + `cases.jsonl` (+
`memory.jsonl`) of `benchmark.results`. Case *labels* -- pair, token symbols, amount
stratum, tuning/report split -- are not in the run records; they are read from the
frozen bundle the run names, and only when that bundle's `manifest.json` hashes to the
run's `bundle_hash` and each read file matches the bundle's checksum table. Nothing here
touches the network or live chain data; without a verified bundle the report still
renders, with raw token addresses and a single `unlabeled` stratum.

Comparison rules implemented here (§2.11):

- **Denominators are the full schedule.** Every algorithm's status counts cover every
  scheduled case; `unsupported`/`no_route`/`timeout`/... and (for an incomplete run)
  unrecorded cases stay in the totals. Timeouts are split by `limit_hit` so a quote/time
  budget cut-off is not read as a missing route.
- **Quality is paired on common-success cases** and expressed per case as a relative
  improvement in basis points, `(a - b) / b * 1e4`; raw amounts of different assets are
  never averaged. A case where the direct baseline did not succeed has an `N/A` ratio
  (counted, never dropped silently); other baselines can still compare it.
- **Samples below `min_samples` are marked underpowered.**
- **Topology** (`single_route`/`disjoint_split`/`shared_pool`, WHI-1441) separates
  like-for-like comparisons (the baseline's declared capabilities admit the plan's
  topology) from capability gains.
- **Net output** reads `evaluation.estimated_net_output` and `evaluation.cost`
  (WHI-1445) -- never the ranking sentinel `score`. Nominal/low/high cost scenarios are
  compared scenario-to-scenario; rank reversals are counted; a plan without a reliable
  net score stays visibly unranked. Gross and net magnitudes are never compared.
- **Quality vs time** is a Pareto view over cases every algorithm solved, not a
  weighted score.
"""

from __future__ import annotations

import hashlib
import json
import math
import shlex
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from benchmark.results import (
    RunManifest,
    load_case_records,
    load_manifest,
    load_memory_records,
)

DEFAULT_MIN_SAMPLES = 30
STATUS_ORDER = (
    "ok",
    "unsupported",
    "no_route",
    "timeout",
    "invalid_plan",
    "incomplete_snapshot",
    "model_error",
    "algorithm_error",
    "cancelled",
    "missing",  # scheduled but never recorded (an incomplete run only)
)
TOPOLOGIES = ("single_route", "disjoint_split", "shared_pool")
SCENARIOS = ("nominal", "low", "high")
UNLABELED = "unlabeled"

COHORT_MATCHED = "matched_sor_v2_v3"
COHORT_FULL = "full_source"
COHORT_UNKNOWN = "unknown"
COHORT_TITLES = {
    COHORT_MATCHED: "Matched V2/V3 cohort (SOR-compatible pools only; like-for-like with SOR)",
    COHORT_FULL: "Full five-source coverage (includes Liquidity Book; coverage/capability gains)",
    COHORT_UNKNOWN: "Unlabeled universe (no verified bundle descriptor)",
}

SCOPE_HELD_OUT = "held_out"
SCOPE_TUNING = "exploratory_tuning"
SCOPE_MIXED = "exploratory_mixed"
SCOPE_UNLABELED = "exploratory_unlabeled"
SCOPE_TITLES = {
    SCOPE_HELD_OUT: "HELD-OUT: every case is a declared report-split case (disjoint from "
    "the tuning cases profiles were calibrated on)",
    SCOPE_TUNING: "EXPLORATORY: tuning-split cases only (the cases profiles were calibrated "
    "on); not a held-out result",
    SCOPE_MIXED: "EXPLORATORY: mixes tuning-split and report-split cases; not a held-out "
    "result (the per-stratum table shows the splits apart)",
    SCOPE_UNLABELED: "EXPLORATORY: case splits unknown (no verified bundle descriptor); not "
    "a held-out result",
}


class ReportInputError(ValueError):
    """The run records cannot be reported as asked."""


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class CaseContext:
    case_id: str
    token_in: str | None
    token_out: str | None
    amount_in: str | None
    stratum: str
    split: str


@dataclass(frozen=True)
class Row:
    """One scheduled (case, algorithm) cell, normalized from its record."""

    case_id: str
    algorithm: str
    status: str
    gross: int | None
    net: dict[str, int | None]  # scenario -> estimated net output (None: unranked)
    cost_status: str | None
    cost_reason: str | None
    topology: str | None
    multi_hop: bool | None  # some step spends an intermediate (non-request) fund
    solve_seconds: float | None
    quotes_attempted: int | None
    quotes_counted: int | None
    limit_hit: str | None
    error: str | None
    has_last_valid_candidate: bool
    record: Mapping[str, Any] | None


@dataclass
class RunData:
    manifest: RunManifest
    rows: dict[tuple[str, str], Row]
    case_ids: list[str]
    algorithms: list[str]
    cases: dict[str, CaseContext]
    symbols: dict[str, str | None]
    memory: list[dict[str, Any]]
    cohort: str
    bundle_note: str
    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    # pool id -> source key, from the hash-verified bundle's pools.json (WHI-1447 source
    # coverage); empty without a verified bundle.
    pool_sources: dict[str, str] = field(default_factory=dict)

    def row(self, case_id: str, algorithm: str) -> Row:
        return self.rows[(case_id, algorithm)]

    @property
    def evaluation_scope(self) -> str:
        """`held_out` iff every scheduled case is a declared `report`-split case,
        `tuning` iff every case is a `tuning`-split case, else `mixed`/`unlabeled`
        (docs/DESIGN.md §2.4: tuning and final reporting use disjoint declared cases, or
        the report is clearly labeled exploratory)."""
        splits = {self.cases[c].split for c in self.case_ids}
        if not splits or UNLABELED in splits:
            return SCOPE_UNLABELED
        if splits == {"report"}:
            return SCOPE_HELD_OUT
        if splits == {"tuning"}:
            return SCOPE_TUNING
        return SCOPE_MIXED

    @property
    def objective_mode(self) -> str:
        objective = self.manifest.resolved_profile.get("objective", {})
        return str(objective.get("mode", "unknown")) if isinstance(objective, dict) else "unknown"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bundle_path_from_replay(replay_command: str) -> Path | None:
    try:
        argv = shlex.split(replay_command)
    except ValueError:
        return None
    for index, token in enumerate(argv[:-1]):
        if token == "--bundle":
            return Path(argv[index + 1])
    return None


def _verified_bundle(
    manifest: RunManifest, candidates: Sequence[Path]
) -> tuple[Path | None, dict[str, str], str]:
    """(bundle dir, its checksum table, note). Only a bundle whose manifest bytes hash to
    the run's `bundle_hash` is used."""
    tried: list[str] = []
    for candidate in candidates:
        manifest_path = candidate / "manifest.json"
        if not manifest_path.is_file():
            tried.append(f"{candidate} (absent)")
            continue
        if _sha256(manifest_path) != manifest.bundle_hash:
            tried.append(f"{candidate} (bundle_hash mismatch)")
            continue
        checksums = json.loads(manifest_path.read_text(encoding="utf-8")).get("checksums", {})
        return candidate, dict(checksums), f"case labels from bundle {candidate} (hash verified)"
    detail = "; ".join(tried) if tried else "no bundle path known"
    return None, {}, f"case labels unavailable: {detail}"


def _read_checked(bundle_dir: Path, checksums: Mapping[str, str], name: str) -> str | None:
    path = bundle_dir / name
    if name not in checksums or not path.is_file():
        return None
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != checksums[name]:
        raise ReportInputError(f"{path}: checksum does not match its bundle manifest")
    return data.decode("utf-8")


def _case_contexts(
    bundle_dir: Path | None, checksums: Mapping[str, str]
) -> tuple[dict[str, CaseContext], dict[str, str | None], str | None]:
    """(case contexts, token symbols, corpus cohort name) from a verified bundle."""
    if bundle_dir is None:
        return {}, {}, None
    cases_text = _read_checked(bundle_dir, checksums, "cases.jsonl") or ""
    corpus_text = _read_checked(bundle_dir, checksums, "corpus.json")
    prices_text = _read_checked(bundle_dir, checksums, "prices.json")
    corpus = json.loads(corpus_text) if corpus_text else {}
    metadata = corpus.get("case_metadata", {}) if isinstance(corpus, dict) else {}
    contexts: dict[str, CaseContext] = {}
    for line in cases_text.splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        meta = metadata.get(raw["case_id"], {})
        contexts[raw["case_id"]] = CaseContext(
            case_id=raw["case_id"],
            token_in=str(raw.get("token_in", "")).lower() or None,
            token_out=str(raw.get("token_out", "")).lower() or None,
            amount_in=str(raw["amount_in"]) if "amount_in" in raw else None,
            stratum=str(meta.get("stratum", UNLABELED)),
            split=str(meta.get("split", UNLABELED)),
        )
    symbols: dict[str, str | None] = {}
    if prices_text:
        for address, entry in json.loads(prices_text).get("tokens", {}).items():
            symbols[address.lower()] = entry.get("symbol") if isinstance(entry, dict) else None
    cohort = corpus.get("cohort") if isinstance(corpus, dict) and corpus else None
    return contexts, symbols, (cohort if cohort else ("full_source" if corpus else None))


def _pool_sources(bundle_dir: Path | None, checksums: Mapping[str, str]) -> dict[str, str]:
    """pool id -> source key of a verified bundle (empty when unavailable)."""
    if bundle_dir is None:
        return {}
    text = _read_checked(bundle_dir, checksums, "pools.json")
    if not text:
        return {}
    pools = json.loads(text).get("pools", [])
    return {
        str(p["pool_id"]).lower(): str(p.get("source_key") or p.get("family") or UNLABELED)
        for p in pools
        if isinstance(p, dict) and "pool_id" in p
    }


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def plan_topology(record: Mapping[str, Any]) -> str | None:
    """The solver's own `search.topology` when it declares one (incremental_graph),
    else derived from the evaluated plan's `route_features`."""
    search = record.get("search") or {}
    declared = search.get("topology") if isinstance(search, dict) else None
    if isinstance(declared, str) and declared in TOPOLOGIES:
        return declared
    evaluation = record.get("evaluation") or {}
    features = evaluation.get("route_features") if isinstance(evaluation, dict) else None
    if not isinstance(features, dict):
        return None
    if int(features.get("repeated_pool_calls", 0)) > 0:
        return "shared_pool"
    if int(features.get("split_funds", 0)) > 0 or int(features.get("merge_steps", 0)) > 0:
        return "disjoint_split"
    return "single_route"


def plan_multi_hop(record: Mapping[str, Any]) -> bool | None:
    evaluation = record.get("evaluation") or {}
    trace = evaluation.get("trace") if isinstance(evaluation, dict) else None
    if not isinstance(trace, list) or not trace:
        return None
    return any(
        inp.get("fund_id") != "REQUEST"
        for step in trace
        if isinstance(step, dict)
        for inp in step.get("inputs", [])
    )


def _normalize(record: Mapping[str, Any], objective_mode: str) -> Row:
    status = str(record.get("status"))
    evaluation = record.get("evaluation") if status == "ok" else None
    evaluation = evaluation if isinstance(evaluation, dict) else None
    gross = _int_or_none(evaluation.get("gross_output")) if evaluation else None
    net: dict[str, int | None] = dict.fromkeys(SCENARIOS)
    cost_status: str | None = None
    cost_reason: str | None = None
    if evaluation is not None:
        nominal = _int_or_none(evaluation.get("estimated_net_output"))
        cost = evaluation.get("cost")
        if isinstance(cost, dict):
            cost_status = str(cost.get("status"))
            cost_reason = cost.get("reason")
            if cost.get("net_rankable") and nominal is not None and gross is not None:
                net["nominal"] = nominal
                for scenario in ("low", "high"):
                    raw = _int_or_none(cost.get(f"{scenario}_out_raw"))
                    net[scenario] = None if raw is None else gross - raw
        elif objective_mode == "synthetic_fixed_cost" and nominal is not None:
            cost_status = "synthetic"
            net["nominal"] = nominal
    measurement = record.get("measurement") or {}
    samples = measurement.get("solve_seconds") if isinstance(measurement, dict) else None
    solve = None
    if isinstance(samples, list) and samples:
        ordered = sorted(float(s) for s in samples)
        solve = ordered[(len(ordered) - 1) // 2]
    quotes = record.get("quotes") or {}
    return Row(
        case_id=str(record["case_id"]),
        algorithm=str(record["algorithm"]),
        status=status,
        gross=gross,
        net=net,
        cost_status=cost_status,
        cost_reason=cost_reason,
        topology=plan_topology(record) if status == "ok" else None,
        multi_hop=plan_multi_hop(record) if status == "ok" else None,
        solve_seconds=solve,
        quotes_attempted=_int_or_none(quotes.get("attempted")),
        quotes_counted=_int_or_none(quotes.get("counted")),
        limit_hit=record.get("limit_hit"),
        error=record.get("error"),
        has_last_valid_candidate=record.get("last_valid_candidate") is not None,
        record=record,
    )


def _missing_row(case_id: str, algorithm: str) -> Row:
    return Row(
        case_id=case_id,
        algorithm=algorithm,
        status="missing",
        gross=None,
        net=dict.fromkeys(SCENARIOS),
        cost_status=None,
        cost_reason=None,
        topology=None,
        multi_hop=None,
        solve_seconds=None,
        quotes_attempted=None,
        quotes_counted=None,
        limit_hit=None,
        error="scheduled but not recorded (incomplete run)",
        has_last_valid_candidate=False,
        record=None,
    )


def _cohort_of(corpus_cohort: str | None, records: Iterable[Mapping[str, Any]]) -> str:
    if corpus_cohort == "sor_compatible":
        return COHORT_MATCHED
    if corpus_cohort == "full_source":
        return COHORT_FULL
    for record in records:  # uni_sor_port records its own coverage mode per case
        search = record.get("search")
        if isinstance(search, dict) and search.get("coverage_mode") == "matched_cohort":
            return COHORT_MATCHED
        if isinstance(search, dict) and search.get("coverage_mode") == "full_universe":
            return COHORT_FULL
    return COHORT_UNKNOWN


def load_run(
    run_dir: str | Path,
    *,
    bundle_dirs: Sequence[str | Path] = (),
    allow_incomplete: bool = False,
    repo_root: Path | None = None,
) -> RunData:
    """Load and normalize one run. `bundle_dirs` are candidate frozen bundles for case
    labels (the run's own `--bundle` from its replay command is tried too); only the
    one hashing to the run's `bundle_hash` is read."""
    manifest = load_manifest(run_dir, allow_incomplete=allow_incomplete)
    records = load_case_records(run_dir)
    memory = load_memory_records(run_dir) if manifest.memory_record_count is not None else []
    algorithms = list(manifest.algorithms)
    order = manifest.measurement.get("case_order")
    case_ids = [str(c) for c in order] if isinstance(order, list) and order else []
    if not case_ids:
        seen: dict[str, None] = {}
        for record in records:
            seen.setdefault(str(record["case_id"]), None)
        case_ids = list(seen)
    candidates = [Path(p) for p in bundle_dirs]
    replay_bundle = bundle_path_from_replay(manifest.replay_command)
    if replay_bundle is not None:
        candidates.append(replay_bundle)
        if not replay_bundle.is_absolute() and repo_root is not None:
            candidates.append(repo_root / replay_bundle)
    bundle_dir, checksums, note = _verified_bundle(manifest, candidates)
    contexts, symbols, corpus_cohort = _case_contexts(bundle_dir, checksums)
    pool_sources = _pool_sources(bundle_dir, checksums)
    rows: dict[tuple[str, str], Row] = {}
    mode = str(manifest.resolved_profile.get("objective", {}).get("mode", "unknown"))
    for record in records:
        row = _normalize(record, mode)
        key = (row.case_id, row.algorithm)
        if key in rows:
            raise ReportInputError(f"{run_dir}: duplicate record for {key}")
        rows[key] = row
    for case_id in case_ids:
        for algorithm in algorithms:
            rows.setdefault((case_id, algorithm), _missing_row(case_id, algorithm))
    for case_id in case_ids:
        contexts.setdefault(
            case_id,
            CaseContext(case_id, None, None, None, UNLABELED, UNLABELED),
        )
    config = manifest.resolved_profile.get("algorithm_config", {})
    capabilities = {name: dict(config.get(name, {}).get("capabilities", {})) for name in algorithms}
    return RunData(
        manifest=manifest,
        rows=rows,
        case_ids=case_ids,
        algorithms=algorithms,
        cases=contexts,
        symbols=symbols,
        memory=memory,
        cohort=_cohort_of(corpus_cohort, records),
        bundle_note=note,
        capabilities=capabilities,
        pool_sources=pool_sources,
    )


# --------------------------------------------------------------------------- statistics


def percentile(sorted_values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (deterministic, no interpolation)."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(q * len(sorted_values)))
    return float(sorted_values[min(rank, len(sorted_values)) - 1])


def relative_bps(value: int, baseline: int) -> float | None:
    if baseline <= 0:
        return None
    return float(Fraction(value - baseline, baseline) * 10_000)


def distribution(values: Iterable[float], min_samples: int) -> dict[str, Any]:
    ordered = sorted(values)
    n = len(ordered)
    return {
        "n": n,
        "p5": percentile(ordered, 0.05),
        "p25": percentile(ordered, 0.25),
        "p50": percentile(ordered, 0.50),
        "p75": percentile(ordered, 0.75),
        "p95": percentile(ordered, 0.95),
        "min": ordered[0] if ordered else None,
        "max": ordered[-1] if ordered else None,
        "mean": (sum(ordered) / n) if n else None,
        "underpowered": n < min_samples,
    }


def token_label(run: RunData, address: str | None) -> str:
    if not address:
        return "?"
    symbol = run.symbols.get(address.lower())
    return symbol if symbol else f"{address[:8]}…"


def pair_label(run: RunData, case_id: str) -> str:
    context = run.cases[case_id]
    if context.token_in is None and context.token_out is None:
        return UNLABELED
    return f"{token_label(run, context.token_in)}→{token_label(run, context.token_out)}"


# --------------------------------------------------------------------------- tables


def status_table(run: RunData) -> list[dict[str, Any]]:
    """Per algorithm: every status over the full schedule (denominator = scheduled)."""
    out = []
    for algorithm in run.algorithms:
        counts: Counter[str] = Counter()
        timeout_by_limit: Counter[str] = Counter()
        last_valid = 0
        for case_id in run.case_ids:
            row = run.row(case_id, algorithm)
            counts[row.status] += 1
            if row.status == "timeout":
                timeout_by_limit[row.limit_hit or "solver_budget"] += 1
            last_valid += row.has_last_valid_candidate
        scheduled = len(run.case_ids)
        out.append(
            {
                "algorithm": algorithm,
                "scheduled": scheduled,
                **{status: counts.get(status, 0) for status in STATUS_ORDER},
                "other": sum(v for k, v in counts.items() if k not in STATUS_ORDER),
                "ok_share": counts.get("ok", 0) / scheduled if scheduled else None,
                "timeout_by_limit": dict(sorted(timeout_by_limit.items())),
                "last_valid_candidates": last_valid,
            }
        )
    return out


def _allowed_topologies(capabilities: Mapping[str, Any]) -> set[str]:
    allowed = {"single_route"}
    if capabilities.get("split"):
        allowed.add("disjoint_split")
    if capabilities.get("shared_pools"):
        allowed.add("shared_pool")
    return allowed


def comparison_kind(run: RunData, algorithm: str, baseline: str) -> str:
    """How to read `algorithm` vs `baseline` in this run's universe."""
    restricted = [
        name
        for name in (algorithm, baseline)
        if run.capabilities.get(name, {}).get("protocols") is not None
    ]
    if restricted and run.cohort != COHORT_MATCHED:
        return "coverage"  # the unrestricted side may use sources the other cannot
    return "matched"


def paired_gross(
    run: RunData, algorithm: str, baseline: str, case_ids: Iterable[str], min_samples: int
) -> dict[str, Any]:
    """`algorithm` vs `baseline` on the common-success cases among `case_ids`."""
    bps: list[float] = []
    like: list[float] = []
    capability: list[float] = []
    only_algorithm = only_baseline = zero_baseline = 0
    baseline_caps = run.capabilities.get(baseline, {})
    allowed = _allowed_topologies(baseline_caps)
    for case_id in case_ids:
        a, b = run.row(case_id, algorithm), run.row(case_id, baseline)
        if a.gross is None and b.gross is None:
            continue
        if b.gross is None:
            only_algorithm += 1
            continue
        if a.gross is None:
            only_baseline += 1
            continue
        value = relative_bps(a.gross, b.gross)
        if value is None:
            zero_baseline += 1
            continue
        bps.append(value)
        within = a.topology in allowed and (not a.multi_hop or bool(baseline_caps.get("multi_hop")))
        (like if within else capability).append(value)
    dist = distribution(bps, min_samples)
    return {
        "algorithm": algorithm,
        "baseline": baseline,
        "kind": comparison_kind(run, algorithm, baseline),
        "better": sum(1 for v in bps if v > 0),
        "equal": sum(1 for v in bps if v == 0),
        "worse": sum(1 for v in bps if v < 0),
        "only_algorithm": only_algorithm,
        "only_baseline": only_baseline,
        "zero_baseline": zero_baseline,
        "like_for_like": distribution(like, min_samples),
        "capability": distribution(capability, min_samples),
        **dist,
    }


def vs_direct(run: RunData, min_samples: int) -> list[dict[str, Any]]:
    """Every algorithm against `direct`. Cases the algorithm solved but `direct` did
    not are `na_no_direct` -- their ratio is N/A, never zero and never dropped."""
    if "direct" not in run.algorithms:
        return [
            {"algorithm": a, "baseline": "direct", "n": 0, "na_no_direct": None, "absent": True}
            for a in run.algorithms
        ]
    out = []
    for algorithm in run.algorithms:
        if algorithm == "direct":
            continue
        summary = paired_gross(run, algorithm, "direct", run.case_ids, min_samples)
        summary["na_no_direct"] = summary["only_algorithm"]
        summary["absent"] = False
        out.append(summary)
    return out


def pairwise(run: RunData, min_samples: int) -> list[dict[str, Any]]:
    return [
        paired_gross(run, a, b, run.case_ids, min_samples)
        for a in run.algorithms
        for b in run.algorithms
        if a != b
    ]


def grouped_vs_direct(run: RunData, key: str, min_samples: int) -> list[dict[str, Any]]:
    """Per stratum (`key="stratum"`, with its tuning/report split) or per pair."""
    groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for case_id in run.case_ids:
        context = run.cases[case_id]
        group: tuple[str, ...] = (
            (context.split, context.stratum) if key == "stratum" else (pair_label(run, case_id),)
        )
        groups[group].append(case_id)
    out = []
    has_direct = "direct" in run.algorithms
    for group in sorted(groups):
        members = groups[group]
        for algorithm in run.algorithms:
            if algorithm == "direct":
                continue
            ok = sum(1 for c in members if run.row(c, algorithm).status == "ok")
            entry: dict[str, Any] = {"group": group, "algorithm": algorithm}
            entry["scheduled"] = len(members)
            entry["ok"] = ok
            if has_direct:
                summary = paired_gross(run, algorithm, "direct", members, min_samples)
                entry |= {k: summary[k] for k in ("n", "p5", "p50", "p95", "better", "worse")}
                entry["underpowered"] = summary["underpowered"]
                entry["na_no_direct"] = summary["only_algorithm"]
            else:
                entry |= {"n": 0, "na_no_direct": None, "underpowered": True}
            out.append(entry)
    return out


def topology_table(run: RunData) -> list[dict[str, Any]]:
    out = []
    for algorithm in run.algorithms:
        counts = Counter(
            run.row(c, algorithm).topology
            for c in run.case_ids
            if run.row(c, algorithm).status == "ok"
        )
        out.append(
            {
                "algorithm": algorithm,
                "declared": sorted(_allowed_topologies(run.capabilities.get(algorithm, {}))),
                **{t: counts.get(t, 0) for t in TOPOLOGIES},
                "unknown": counts.get(None, 0),
            }
        )
    return out


def plan_pools(record: Mapping[str, Any] | None) -> list[str]:
    evaluation = (record or {}).get("evaluation")
    trace = evaluation.get("trace") if isinstance(evaluation, dict) else None
    if not isinstance(trace, list):
        return []
    return [str(step.get("pool_id", "")).lower() for step in trace if isinstance(step, dict)]


def source_coverage(run: RunData) -> dict[str, Any]:
    """Which liquidity sources the solved plans actually use (docs/DESIGN.md §2.11
    "source coverage"): per algorithm, the number of `ok` plans with at least one step
    through a pool of each source, next to the bundle's admitted pools per source. Needs
    the hash-verified bundle's pool records; empty otherwise."""
    if not run.pool_sources:
        return {"sources": [], "bundle_pools": {}, "rows": []}
    bundle_pools = Counter(run.pool_sources.values())
    sources = sorted(bundle_pools)
    rows = []
    for algorithm in run.algorithms:
        used: Counter[str] = Counter()
        solved = 0
        for case_id in run.case_ids:
            row = run.row(case_id, algorithm)
            if row.status != "ok":
                continue
            solved += 1
            for source in {run.pool_sources.get(p, UNLABELED) for p in plan_pools(row.record)}:
                used[source] += 1
        rows.append(
            {
                "algorithm": algorithm,
                "ok": solved,
                **{src: used.get(src, 0) for src in sources},
                "other": sum(v for k, v in used.items() if k not in bundle_pools),
            }
        )
    return {"sources": sources, "bundle_pools": dict(bundle_pools), "rows": rows}


def _sign(x: int) -> int:
    return (x > 0) - (x < 0)


def net_available(run: RunData) -> bool:
    return any(row.net["nominal"] is not None for row in run.rows.values()) or (
        run.objective_mode == "empirical_cost"
    )


def net_coverage(run: RunData) -> list[dict[str, Any]]:
    """Per algorithm: net-rankable plans vs every reason a scheduled case is unranked
    on net (no plan at all, or a plan whose cost is unsupported/low-confidence/...)."""
    out = []
    for algorithm in run.algorithms:
        reasons: Counter[str] = Counter()
        rankable = 0
        for case_id in run.case_ids:
            row = run.row(case_id, algorithm)
            if row.net["nominal"] is not None:
                rankable += 1
            elif row.status != "ok":
                reasons[f"no plan ({row.status})"] += 1
            else:
                reasons[row.cost_status or "no cost"] += 1
        out.append(
            {
                "algorithm": algorithm,
                "scheduled": len(run.case_ids),
                "net_rankable": rankable,
                "unranked": dict(sorted(reasons.items())),
            }
        )
    return out


def paired_net(run: RunData, min_samples: int) -> list[dict[str, Any]]:
    """Scenario-to-scenario net comparisons on cases where both plans are net-rankable,
    plus rank reversals: the ordering flips between cost scenarios (`scenario`), or the
    net ordering differs from the gross ordering (`gross_vs_net`; orderings only --
    magnitudes of gross and net are never compared)."""
    out = []
    for a in run.algorithms:
        for b in run.algorithms:
            if a == b:
                continue
            per: dict[str, list[float]] = {s: [] for s in SCENARIOS}
            scenario_reversals: list[str] = []
            gross_net_reversals: list[str] = []
            unranked = 0
            nonpositive: set[str] = set()
            for case_id in run.case_ids:
                ra, rb = run.row(case_id, a), run.row(case_id, b)
                if ra.gross is None or rb.gross is None:
                    continue
                if ra.net["nominal"] is None or rb.net["nominal"] is None:
                    unranked += 1
                    continue
                signs = set()
                for scenario in SCENARIOS:
                    na, nb = ra.net[scenario], rb.net[scenario]
                    if na is None or nb is None:
                        continue
                    value = relative_bps(na, nb)
                    if value is not None:
                        per[scenario].append(value)
                    else:
                        nonpositive.add(case_id)
                    signs.add(_sign(na - nb))
                if 1 in signs and -1 in signs:
                    scenario_reversals.append(case_id)
                gross_sign = _sign(ra.gross - rb.gross)
                net_sign = _sign(ra.net["nominal"] - rb.net["nominal"])
                if gross_sign * net_sign < 0:
                    gross_net_reversals.append(case_id)
            out.append(
                {
                    "algorithm": a,
                    "baseline": b,
                    "both_ok_unranked": unranked,
                    # net baseline <= 0 in some scenario (cost >= gross): ratio N/A there
                    "nonpositive_baseline": len(nonpositive),
                    "scenarios": {s: distribution(per[s], min_samples) for s in SCENARIOS},
                    "scenario_reversals": scenario_reversals,
                    "gross_vs_net_reversals": gross_net_reversals,
                }
            )
    return out


def latency_table(run: RunData, min_samples: int) -> list[dict[str, Any]]:
    timing = run.manifest.timing.get("per_algorithm", {})
    peak: dict[str, list[float]] = defaultdict(list)
    for record in run.memory:
        if record.get("solve_peak_bytes") is not None:
            peak[str(record.get("algorithm"))].append(float(record["solve_peak_bytes"]))
    prepare: dict[str, float] = defaultdict(float)
    for event in run.manifest.prepare_events:
        if isinstance(event.get("prepare_seconds"), int | float):
            prepare[str(event.get("algorithm"))] += float(event["prepare_seconds"])
    out = []
    for algorithm in run.algorithms:
        rows = [run.row(c, algorithm) for c in run.case_ids]
        solve_all = [r.solve_seconds for r in rows if r.solve_seconds is not None]
        solve_ok = [
            r.solve_seconds for r in rows if r.solve_seconds is not None and r.gross is not None
        ]
        quotes = [float(r.quotes_attempted) for r in rows if r.quotes_attempted is not None]
        per = timing.get(algorithm, {}) if isinstance(timing, dict) else {}
        out.append(
            {
                "algorithm": algorithm,
                "solve_all": distribution(solve_all, min_samples),
                "solve_ok": distribution(solve_ok, min_samples),
                "quotes_attempted": distribution(quotes, min_samples),
                "quotes_total": int(sum(quotes)),
                "memory_peak_bytes": distribution(peak.get(algorithm, []), min_samples),
                "memory_measured": algorithm in peak,
                "prepare_seconds_total": prepare.get(algorithm),
                "solve_seconds_total": per.get("solve_seconds_total"),
                "evaluation_seconds_total": per.get("evaluation_seconds_total"),
            }
        )
    return out


def pareto(run: RunData, min_samples: int) -> dict[str, Any]:
    """Quality vs measured time over the cases *every* algorithm solved. Quality is the
    per-case shortfall from the best-known gross output among the run's algorithms
    (bps, <= 0); time is the median solve time on the same cases. A point is Pareto-
    efficient when no other is at least as good on both axes and better on one."""
    common = [
        c
        for c in run.case_ids
        if all(run.row(c, a).gross is not None for a in run.algorithms)
        and all(run.row(c, a).solve_seconds is not None for a in run.algorithms)
    ]
    points = []
    for algorithm in run.algorithms:
        shortfalls: list[float] = []
        times: list[float] = []
        for case_id in common:
            best = max(run.row(case_id, a).gross or 0 for a in run.algorithms)
            row = run.row(case_id, algorithm)
            assert row.gross is not None and row.solve_seconds is not None
            value = relative_bps(row.gross, best)
            if value is not None:
                shortfalls.append(value)
            times.append(row.solve_seconds)
        quality = distribution(shortfalls, min_samples)
        speed = distribution(times, min_samples)
        points.append(
            {
                "algorithm": algorithm,
                "n": len(common),
                "quality_p50": quality["p50"],
                "quality_mean": quality["mean"],
                "quality_p5": quality["p5"],
                "best_known_share": (
                    sum(1 for v in shortfalls if v == 0) / len(shortfalls) if shortfalls else None
                ),
                "time_p50": speed["p50"],
                "time_p95": speed["p95"],
                "coverage": sum(1 for c in run.case_ids if run.row(c, algorithm).status == "ok")
                / max(1, len(run.case_ids)),
                "underpowered": len(common) < min_samples,
            }
        )
    for point in points:
        point["efficient"] = point["n"] > 0 and not any(
            other is not point
            and other["quality_mean"] is not None
            and point["quality_mean"] is not None
            and other["quality_mean"] >= point["quality_mean"]
            and other["time_p50"] <= point["time_p50"]
            and (
                other["quality_mean"] > point["quality_mean"]
                or other["time_p50"] < point["time_p50"]
            )
            for other in points
        )
    return {"common_cases": len(common), "points": points}


def error_groups(run: RunData, examples: int = 5) -> list[dict[str, Any]]:
    """Every non-ok outcome, grouped by (algorithm, status, limit, error text)."""
    groups: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    for case_id in run.case_ids:
        for algorithm in run.algorithms:
            row = run.row(case_id, algorithm)
            if row.status == "ok":
                continue
            key = (algorithm, row.status, row.limit_hit or "", row.error or "")
            groups[key].append(case_id)
    return [
        {
            "algorithm": key[0],
            "status": key[1],
            "limit_hit": key[2] or None,
            "error": key[3] or None,
            "count": len(cases),
            "examples": cases[:examples],
        }
        for key, cases in sorted(groups.items(), key=lambda kv: (kv[0][0], -len(kv[1]), kv[0]))
    ]


def representative_traces(run: RunData) -> list[dict[str, Any]]:
    """Per algorithm: a *typical* improvement over `direct` -- the median-gain case among
    improved, non-boundary cases (boundary dust cases give extreme ratios) -- or its
    first solved case when there is none, plus one example of each failure status."""
    out = []
    for algorithm in run.algorithms:
        gains: list[tuple[float, str]] = []
        first_ok: str | None = None
        failures: dict[str, str] = {}
        for case_id in run.case_ids:
            row = run.row(case_id, algorithm)
            if row.gross is None:
                failures.setdefault(row.status, case_id)
                continue
            first_ok = first_ok or case_id
            if algorithm == "direct" or "direct" not in run.algorithms:
                continue
            base = run.row(case_id, "direct").gross
            value = None if base is None else relative_bps(row.gross, base)
            if value is not None and value > 0 and run.cases[case_id].stratum != "boundary":
                gains.append((value, case_id))
        if gains:
            gains.sort()
            value, chosen = gains[(len(gains) - 1) // 2]
            out.append(
                {
                    "algorithm": algorithm,
                    "case_id": chosen,
                    "why": f"median gain vs direct among {len(gains)} improved non-boundary "
                    f"case(s): {value:+.2f} bps",
                }
            )
        elif first_ok is not None:
            out.append({"algorithm": algorithm, "case_id": first_ok, "why": "first solved case"})
        for status, case_id in sorted(failures.items()):
            out.append({"algorithm": algorithm, "case_id": case_id, "why": f"example {status}"})
    return out


# --------------------------------------------------------------------------- groups

# WHI-1528: the persisted strategy groups of a run (`resolved_profile.selection.groups`).
STRATEGY_GROUPS = ("base", "optimized", "custom")
STRATEGY_GROUP_TITLES = {
    "base": "Base strategies",
    "optimized": "Optimized strategies",
    "custom": "Other profile-selected strategies",
}


def strategy_groups(manifest: RunManifest) -> list[tuple[str, list[str]]] | None:
    """(group, algorithms) in run order, read only from the run's own persisted selection
    record -- never from the current registry -- or `None` for a run without one (older
    records and `--strategies profile` runs keep their ungrouped presentation). A recorded
    algorithm missing from every group is still shown, under `custom`."""
    selection = manifest.resolved_profile.get("selection")
    groups = selection.get("groups") if isinstance(selection, dict) else None
    if not isinstance(groups, dict):
        return None
    algorithms = list(manifest.algorithms)
    out: list[tuple[str, list[str]]] = []
    placed: set[str] = set()
    for group in STRATEGY_GROUPS:
        listed = groups.get(group)
        members = [a for a in algorithms if isinstance(listed, list) and a in listed]
        if group == "custom":
            members += [a for a in algorithms if a not in placed and a not in members]
        placed.update(members)
        if members:
            out.append((group, members))
    return out


def strategy_recipe(manifest: RunManifest, algorithm: str) -> dict[str, Any] | None:
    """The persisted `strategies.<algorithm>` entry (recipe identity and settings)."""
    entry = manifest.resolved_profile.get("strategies", {}).get(algorithm)
    return entry if isinstance(entry, dict) else None


# --------------------------------------------------------------------------- labels


def algorithm_label(run: RunData, algorithm: str) -> dict[str, Any]:
    """What an algorithm name does -- and does not -- claim to be."""
    config = run.manifest.resolved_profile.get("algorithm_config", {}).get(algorithm, {})
    provenance = config.get("provenance") if isinstance(config, dict) else None
    if isinstance(provenance, dict) and isinstance(provenance.get("upstream"), dict):
        upstream = provenance["upstream"]
        return {
            "kind": "scoped_port",
            "title": (
                f"Uniswap SOR — scoped routing-core port of {upstream.get('package')} "
                f"{upstream.get('version')} @ {str(upstream.get('commit', ''))[:12]}; "
                "NOT the full upstream product"
            ),
            "provenance": provenance,
        }
    if isinstance(provenance, dict) and provenance.get("strategy_group") == "optimized":
        recipe = provenance.get("recipe") or {}
        return {
            "kind": "optimized_strategy",
            "title": (
                "Optimized strategy — experimental heuristic over the "
                f"{provenance.get('reference')} routing core, recipe {recipe.get('key')} "
                f"arm {recipe.get('arm')}: "
                f"{provenance.get('recipe_summary')}; NOT a default router, not adopted"
            ),
            "provenance": provenance,
        }
    if "metis" in algorithm.lower():
        return {
            "kind": "experimental",
            "title": (
                "Metis-inspired experimental Python variant — NOT Jupiter Metis; "
                "no production equivalence claimed"
            ),
            "provenance": provenance,
        }
    return {"kind": "local", "title": "local benchmark algorithm", "provenance": provenance}


def provenance(run: RunData) -> dict[str, Any]:
    m = run.manifest
    objective = m.resolved_profile.get("objective", {})
    cost_model = objective.get("cost_model") if isinstance(objective, dict) else None
    return {
        "run_id": m.run_id,
        "state": m.state,
        "replay_command": m.replay_command,
        "bundle_id": m.bundle_id,
        "bundle_hash": m.bundle_hash,
        "profile_path": m.profile_path,
        "profile_sha256": m.profile_sha256,
        "objective_label": m.objective_label,
        "objective_mode": run.objective_mode,
        "cost_model_sha256": cost_model.get("sha256") if isinstance(cost_model, dict) else None,
        "price_context_sha256": (
            objective.get("price_context_sha256") if isinstance(objective, dict) else None
        ),
        "experiment_id": m.experiment.get("experiment_id"),
        "cases_sha256": m.cases_sha256,
        "memory_sha256": m.memory_sha256,
        "git_revision": m.environment.get("git_revision"),
        "git_dirty": m.environment.get("git_dirty"),
        "git_diff_sha256": m.environment.get("git_diff_sha256"),
        "created_at": m.created_at,
        "finished_at": m.finished_at,
        "environment": {
            k: m.environment.get(k)
            for k in ("python_version", "platform", "cpu_model", "cpu_count", "dependencies")
        },
        "measurement": {k: v for k, v in m.measurement.items() if k != "case_order"},
        "case_labels": run.bundle_note,
    }

"""Offline summaries and paired comparisons of L01 latency experiments (WHI-1503).

    uv run python -m report.latency summarize EXPERIMENT_DIR [--json F] [--markdown F]
    uv run python -m report.latency compare BASELINE_DIR CANDIDATE_DIR --lane exact|heuristic
        [--pair candidate_algorithm=reference_algorithm ...] [--sufficient BASE_SB CAND_SB]
        [--json F] [--markdown F]
    uv run python -m report.latency sufficient SB_DIR [--against EXPERIMENT_DIR] [...]

Everything is read from saved, checksum-verified run records (`benchmark.results`); nothing
re-runs a solver. Every summary/comparison records its own report provenance (the source
that generated it) beside the measured source of the raw records, so a regenerated report
never relabels old measurements as new-source performance. The rules applied by `compare`
are the protocol's pre-registered `acceptance` section (config/latency/l01.yaml,
docs/references/latency-baseline.md):

- coverage: every required run of the protocol schedule is present and complete, over the
  pinned derived bundle, with exactly one record per scheduled (algorithm, case) and the
  experiment's full algorithm set; a missing run or record is never read as agreement;
- internal gates, per experiment: fixed == reverse order, cold == warm process, and every
  case's attempts consistent (`benchmark.runner`'s deterministic view);
- per (bundle, algorithm, case): the median of the measured solve samples pooled over both
  schedule orders; wall and process CPU separately;
- decisions on the protocol's decision split only (held-out), and only for cases whose
  baseline median reaches `min_timed_solve_seconds`;
- improvement = 1 - geometric mean of candidate/baseline per-case medians; it must reach
  both the minimum worthwhile improvement and `noise_multiplier` x the larger A/A noise
  floor (exp|ln geomean(reverse/fixed)| - 1) of the two experiments, for wall AND CPU; the
  cold charged time (start-up incl. prepare + solve + transport + evaluation) must not be
  slower by that threshold, so work moved out of the solve window is still charged;
- exact lane: every scheduled record's semantic fields identical in every stage (timing
  in both orders, cold), cohort and bundle; work counters may differ and are listed; a
  budget-bound record (limit hit, timeout or declared budget truncation) on either side,
  even with identical outputs, is inconclusive until sufficient-budget evidence
  (`--sufficient`, config/latency/l01-sufficient-budget.yaml) shows it unbounded and
  identical on both sides; fixed-budget completion differences are reported separately;
- coverage includes sampling completeness: each case holds the protocol's declared samples
  and each cold case its charge evidence;
- heuristic lane: paired regret against the same-scope reference per split (held-out
  decides), N/A where a score is unknown; there is no default loss tolerance, so the
  verdict is at most "opt-in".

Latency is never summarized as a percentile: each case has 2 x repeats samples, which does
not support a tail or SLA claim.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmark.latency import (
    CONTROL_STATS_KEY,
    SESSION_FILE,
    STAGES,
    SUFFICIENT_EXPERIMENT_SCHEMA,
    source_identity,
)
from benchmark.results import RunManifest, load_case_records, load_manifest, load_memory_records
from benchmark.runner import _deterministic_view

SUMMARY_SCHEMA = "latency-summary/2"
COMPARISON_SCHEMA = "latency-comparison/2"
KILLED = {"timeout", "cancelled"}  # how far a killed search got is timing-dependent
SENTINEL_SPLIT = "sentinel"  # the sentinel case is in neither matrix split
NO_TAIL_CLAIM = (
    "No percentile, p95 or SLA is derived: each case has 2 x repeats samples; the figures "
    "are medians/min/max of observations on the recorded host."
)


class LatencyReportError(ValueError):
    """An experiment directory is missing, incomplete or not comparable."""


@dataclass(frozen=True)
class RunView:
    entry: dict[str, Any]
    manifest: RunManifest
    records: list[dict[str, Any]]
    memory: list[dict[str, Any]]
    path: Path  # relative to the experiment directory, so experiments stay relocatable

    @property
    def run_dir(self) -> str:
        return str(self.path)


@dataclass(frozen=True)
class Experiment:
    path: Path
    document: dict[str, Any]
    runs: dict[tuple[str, str, str], RunView]  # (stage, order, bundle label) -> run

    @property
    def protocol(self) -> dict[str, Any]:
        return dict(self.document["protocol"]["document"])

    @property
    def acceptance(self) -> dict[str, Any]:
        return dict(self.protocol["acceptance"])

    @property
    def split_of(self) -> dict[str, str]:
        return {m["case"]: m["split"] for m in self.protocol["matrix"]}

    def split(self, case_id: str) -> str:
        return self.split_of.get(case_id, SENTINEL_SPLIT)

    @property
    def algorithms(self) -> list[str]:
        """The experiment's scheduled algorithm set: recorded by the driver (older
        experiments: the first run's manifest)."""
        if self.document.get("algorithms"):
            return list(self.document["algorithms"])
        first = next(iter(self.runs.values()), None)
        return list(first.manifest.algorithms) if first is not None else []

    def labels(self, stage: str) -> list[str]:
        return sorted({label for (s, _, label) in self.runs if s == stage})

    @property
    def arm(self) -> dict[str, Any] | None:
        """The L08 arm this experiment measured (None: a plain L01 experiment)."""
        return self.document.get("arm")


def load_experiment(path: str | Path) -> Experiment:
    path = Path(path)
    doc_path = path / "experiment.json"
    if not doc_path.is_file():
        raise LatencyReportError(f"{path}: no experiment.json")
    document = json.loads(doc_path.read_text(encoding="utf-8"))
    if document.get("schema") != "latency-experiment/1":
        raise LatencyReportError(f"{path}: unsupported schema {document.get('schema')!r}")
    if document.get("state") != "complete":
        raise LatencyReportError(f"{path}: experiment is {document.get('state')!r}")
    runs = {}
    for entry in document["runs"]:
        run_dir = path / "runs" / entry["run_id"]  # relocatable: relative to the experiment
        manifest = load_manifest(run_dir)  # verifies cases/memory checksums
        memory = load_memory_records(run_dir) if manifest.memory_record_count else []
        runs[(entry["stage"], entry["order"], entry["bundle"])] = RunView(
            entry, manifest, load_case_records(run_dir), memory, run_dir
        )
    return Experiment(path, document, runs)


def report_provenance() -> dict[str, Any]:
    """Which source generated this report, and when (not the measured source)."""
    source = source_identity()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "git_revision": source["git_revision"],
        "git_dirty": source["git_dirty"],
        "dirty_patch_sha256": source["dirty_patch_sha256"],
        "report_tree_id": source["code_tree_ids"].get("report"),
    }


# ------------------------------------------------------------------ helpers


def _median(values: Iterable[float]) -> float | None:
    items = list(values)
    return statistics.median(items) if items else None


def _spread(values: Iterable[float]) -> dict[str, Any]:
    items = sorted(values)
    if not items:
        return {"n": 0, "min": None, "median": None, "max": None}
    return {"n": len(items), "min": items[0], "median": statistics.median(items), "max": items[-1]}


def _geomean(ratios: Sequence[float]) -> float | None:
    return math.exp(statistics.fmean(math.log(r) for r in ratios)) if ratios else None


def _key(record: Mapping[str, Any]) -> tuple[str, str]:
    return (record["algorithm"], record["case_id"])


def _samples(record: Mapping[str, Any], kind: str) -> list[float]:
    field = "solve_seconds" if kind == "wall" else "solve_cpu_seconds"
    return [float(v) for v in record.get("measurement", {}).get(field) or [] if v is not None]


def _score(record: Mapping[str, Any]) -> int | None:
    return None if record.get("score") is None else int(record["score"])


def semantic_view(record: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    if record["status"] in KILLED:
        return {"status": record["status"], "limit_hit": record.get("limit_hit")}
    return {field: record.get(field) for field in fields}


def work_view(record: Mapping[str, Any]) -> dict[str, Any]:
    """Work counters. An L08 arm's per-solve control stats and its effective quote-path
    label are arm labels, not search work: they are reported with the arm, not here."""
    search = record.get("search")
    if isinstance(search, Mapping):
        search = {k: v for k, v in search.items() if k != CONTROL_STATS_KEY}
        if isinstance(search.get("sor_fast"), Mapping):
            search["sor_fast"] = {k: v for k, v in search["sor_fast"].items() if k != "quote_path"}
    return {
        "quotes_counted": (record.get("quotes") or {}).get("counted"),
        "candidates_considered": record.get("candidates_considered"),
        "candidates_truncated": record.get("candidates_truncated"),
        "search": search,
    }


def budget_bound(record: Mapping[str, Any]) -> bool:
    """The fixed budget ended or cut this search (a hard limit, or a declared solver
    truncation): what it completed depends on the budget, not only on the algorithm."""
    return (
        record["status"] == "timeout"
        or record.get("limit_hit") is not None
        or (record.get("search") or {}).get("truncated_by") is not None
    )


def _differing(a: Mapping[str, Any], b: Mapping[str, Any]) -> str:
    return ", ".join(sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k)))


def record_mismatches(
    a: Sequence[Mapping[str, Any]], b: Sequence[Mapping[str, Any]], *, one_sided: bool = True
) -> list[str]:
    """`benchmark.runner.compare_runs` on in-memory records: the deterministic outputs
    (incl. work counters, seed and attempt consistency) of the same schedule must agree.
    A record present on one side only is listed (`one_sided`), never silently skipped; the
    gates leave it to `coverage_problems`, which always reports it."""
    by_a, by_b = {_key(r): r for r in a}, {_key(r): r for r in b}
    out: list[str] = []
    for key in sorted(by_a.keys() | by_b.keys()):
        if key not in by_a or key not in by_b:
            if one_sided:
                out.append(f"{key[0]}/{key[1]}: scheduled in only one run")
            continue
        view_a, view_b = _deterministic_view(by_a[key]), _deterministic_view(by_b[key])
        if view_a != view_b:
            out.append(f"{key[0]}/{key[1]}: differs in {_differing(view_a, view_b)}")
    return out


def _required_runs(exp: Experiment) -> set[tuple[str, str, str]]:
    proto = exp.protocol
    stages = exp.document.get("stages_requested") or list(STAGES)
    required: set[tuple[str, str, str]] = set()
    for kind in ("matrix", "sentinel"):
        if "timing" in stages:
            required |= {
                ("timing", order, f"{cohort}/{kind}")
                for cohort in proto["cohorts"]
                for order in proto["timing"]["orders"]
            }
        if "cold" in stages:
            required |= {
                ("cold", "fixed", f"{cohort}/{kind}") for cohort in proto["cold"]["cohorts"]
            }
    return required


ATTEMPT_FAILURES = {"timeout", "algorithm_error"}  # may end a case before its last attempt
SAMPLE_FIELDS = ("solve_seconds", "solve_cpu_seconds", "transport_seconds")


def declared_attempts(exp: Experiment, stage: str) -> tuple[int, int]:
    """(warmup, repeats) the protocol schedules per case in a stage."""
    timing = exp.protocol["timing"]
    return (timing["warmup"], timing["repeats"]) if stage == "timing" else (0, 1)


def sample_problem(record: Mapping[str, Any], warmup: int, repeats: int) -> str | None:
    """Why a record does not hold the declared samples: every case whose attempts all
    returned has exactly `repeats` wall, CPU and transport samples; only an attempt
    failure (timeout / algorithm_error, incl. a failed prepare) may end it early, with one
    sample per measured attempt that returned before it."""
    m = record.get("measurement", {})
    if (m.get("warmup"), m.get("repeats")) != (warmup, repeats):
        return (f"declares warmup/repeats {m.get('warmup')}/{m.get('repeats')}, protocol "
                f"{warmup}/{repeats}")  # fmt: skip
    completed = m.get("attempts_completed")
    if completed == warmup + repeats:
        expected = repeats
    elif record["status"] in ATTEMPT_FAILURES and (completed or 0) < warmup + repeats:
        expected = max(0, (completed or 0) - warmup)
    else:
        return (f"{completed} attempt(s) completed with status {record['status']!r}; "
                f"protocol schedules {warmup} + {repeats}")  # fmt: skip
    counts = {f: len(m.get(f) or []) for f in SAMPLE_FIELDS}
    if any(n != expected for n in counts.values()) or any(
        v is None for f in SAMPLE_FIELDS for v in m.get(f) or []
    ):
        return f"samples {counts}, expected {expected} each"
    return None


def coverage_problems(exp: Experiment) -> list[str]:
    """Everything the protocol schedule requires but the experiment does not hold exactly
    once. Empty means complete coverage; anything else can never support a verdict."""
    problems: list[str] = []
    required = _required_runs(exp)
    for key in sorted(required - exp.runs.keys()):
        problems.append(f"{' '.join(key)}: required run missing")
    for key in sorted(exp.runs.keys() - required):
        problems.append(f"{' '.join(key)}: run outside the protocol schedule")
    algorithms = exp.algorithms
    if not algorithms:
        problems.append("no algorithm set recorded")
    matrix_cases = {m["case"] for m in exp.protocol["matrix"]}
    bundles = exp.document.get("bundles") or {}
    for key, run in sorted(exp.runs.items()):
        where = " ".join(key)
        manifest = run.manifest
        if manifest.state != "complete":
            problems.append(f"{where}: run is {manifest.state!r}")
        if list(manifest.algorithms) != algorithms:
            problems.append(f"{where}: algorithms {list(manifest.algorithms)} != {algorithms}")
        bundle = bundles.get(key[2])
        if bundle is None or manifest.bundle_hash != bundle.get("bundle_hash"):
            problems.append(f"{where}: bundle differs from the experiment's derived bundle")
        schedule = Counter(tuple(p) for p in manifest.measurement.get("schedule") or [])
        cases = {c for (_, c) in schedule}
        if key[2].endswith("/matrix") and cases != matrix_cases:
            problems.append(f"{where}: schedule cases differ from the protocol matrix")
        if key[2].endswith("/sentinel") and len(cases) != 1:
            problems.append(f"{where}: sentinel schedule is not exactly one case")
        full = Counter((a, c) for a in algorithms for c in cases)
        if schedule != full or manifest.scheduled_count != sum(full.values()):
            problems.append(f"{where}: schedule is not every algorithm x case exactly once")
        records = Counter(_key(r) for r in run.records)
        missing, extra = full - records, records - full
        for a, c in sorted(missing):
            problems.append(f"{where}: {a}/{c}: scheduled record missing")
        for a, c in sorted(extra):
            problems.append(f"{where}: {a}/{c}: unscheduled or duplicate record")
        warmup, repeats = declared_attempts(exp, key[0])
        declared = (manifest.measurement.get("warmup"), manifest.measurement.get("repeats"))
        if declared != (warmup, repeats):
            problems.append(f"{where}: declares warmup/repeats {declared}, protocol "
                            f"{(warmup, repeats)}")  # fmt: skip
        events = {e["index"]: e for e in manifest.prepare_events}
        for r in run.records:
            pair = f"{where}: {r['algorithm']}/{r['case_id']}"
            if r["status"] == "cancelled":
                problems.append(f"{pair}: cancelled")
                continue
            problem = sample_problem(r, warmup, repeats)
            if problem is not None:
                problems.append(f"{pair}: {problem}")
            m = r.get("measurement", {})
            if key[0] == "cold" and m.get("attempts_completed") == warmup + repeats:
                event = events.get(m.get("prepare_event")) or {}
                if (event.get("startup_seconds") is None or event.get("prepare_seconds") is None
                        or m.get("evaluation_seconds") is None):  # fmt: skip
                    problems.append(f"{pair}: cold charge evidence (start-up, prepare, "
                                    "evaluation) missing")  # fmt: skip
    stages = exp.document.get("stages_requested") or list(STAGES)
    wanted = exp.protocol["quote_cli"]["invocations"] if "quote_cli" in stages else 0
    if len(exp.document.get("quote_cli") or []) != wanted:
        problems.append(f"quote_cli: {len(exp.document.get('quote_cli') or [])} invocation(s),"
                        f" protocol requires {wanted}")  # fmt: skip
    return problems


def internal_checks(exp: Experiment) -> dict[str, list[str]]:
    """The experiment's own determinism gates: fixed == reverse order, cold == warm
    process, and every case's attempts consistent."""
    cross_order: list[str] = []
    cold_warm: list[str] = []
    for label in sorted(set(exp.labels("timing")) | set(exp.labels("cold"))):
        fixed = exp.runs.get(("timing", "fixed", label))
        reverse = exp.runs.get(("timing", "reverse", label))
        cold = exp.runs.get(("cold", "fixed", label))
        if fixed and reverse:
            mismatches = record_mismatches(fixed.records, reverse.records, one_sided=False)
            cross_order += [f"{label} {m}" for m in mismatches]
        if fixed and cold:
            mismatches = record_mismatches(fixed.records, cold.records, one_sided=False)
            cold_warm += [f"{label} {m}" for m in mismatches]
    repeat = sorted(
        f"{' '.join(key)} {r['algorithm']}/{r['case_id']}"
        for key, run in exp.runs.items()
        for r in run.records
        if r.get("measurement", {}).get("attempts_consistent") is False
    )
    return {"cross_order": cross_order, "cold_warm": cold_warm, "attempts_inconsistent": repeat}


def _pooled_medians(exp: Experiment, label: str, kind: str) -> dict[tuple[str, str], float]:
    """(algorithm, case) -> median of the measured samples of every timing order."""
    pooled: dict[tuple[str, str], list[float]] = defaultdict(list)
    for (stage, _, run_label), run in exp.runs.items():
        if stage == "timing" and run_label == label:
            for record in run.records:
                pooled[_key(record)].extend(_samples(record, kind))
    return {k: statistics.median(v) for k, v in pooled.items() if v}


def _order_medians(run: RunView, kind: str) -> dict[tuple[str, str], float]:
    return {_key(r): statistics.median(s) for r in run.records if (s := _samples(r, kind))}


def noise_floor(exp: Experiment, label: str, algorithm: str, kind: str) -> dict[str, Any]:
    """A/A noise: geometric mean over decision-split cases (both medians >= floor) of
    the reverse-order / fixed-order per-case median ratio; noise = exp|ln g| - 1."""
    fixed, reverse = (
        exp.runs.get(("timing", "fixed", label)),
        exp.runs.get(("timing", "reverse", label)),
    )
    if fixed is None or reverse is None:
        return {"cases": 0, "geomean_ratio": None, "noise": None}
    acc = exp.acceptance
    a, b = _order_medians(fixed, kind), _order_medians(reverse, kind)
    ratios = [
        b[k] / a[k]
        for k in a.keys() & b.keys()
        if k[0] == algorithm
        and exp.split(k[1]) == acc["decision_split"]
        and min(a[k], b[k]) >= acc["min_timed_solve_seconds"]
    ]
    g = _geomean(ratios)
    return {
        "cases": len(ratios),
        "geomean_ratio": g,
        "noise": None if g is None else math.exp(abs(math.log(g))) - 1,
    }


# ------------------------------------------------------------------ summary


def _timing_block(exp: Experiment, label: str) -> dict[str, Any]:
    runs = [r for (s, _, lb), r in exp.runs.items() if s == "timing" and lb == label]
    walls, cpus = _pooled_medians(exp, label, "wall"), _pooled_medians(exp, label, "cpu")
    block: dict[str, Any] = {}
    for algorithm in exp.algorithms:
        records = [r for run in runs for r in run.records if r["algorithm"] == algorithm]
        keys = sorted(k for k in walls if k[0] == algorithm)
        attempts = [a for r in records for a in r["measurement"].get("attempts", [])]
        warm_first = [
            a["solve_wall_seconds"] / walls[_key(r)]
            for r in records
            for a in r["measurement"].get("attempts", [])[:1]
            if a["phase"] == "warmup" and a["solve_wall_seconds"] and walls.get(_key(r))
        ]
        events = [
            e
            for run in runs
            for e in run.manifest.prepare_events
            if e["algorithm"] == algorithm and e.get("status") == "ok"
        ]
        pooled_counts = Counter(k for r in records for k in [_key(r)] * len(_samples(r, "wall")))
        sum_wall = sum(walls[k] for k in keys)
        sum_cpu = sum(cpus.get(k, 0.0) for k in keys)
        block[algorithm] = {
            "cases_with_samples": len(keys),
            "samples_per_case": _spread(pooled_counts[k] for k in keys),
            "case_median_wall_seconds": _spread(walls[k] for k in keys),
            "case_median_cpu_seconds": _spread(cpus[k] for k in keys if k in cpus),
            "sum_of_case_medians_wall_seconds": sum_wall,
            "sum_of_case_medians_cpu_seconds": sum_cpu,
            "cpu_over_wall": sum_cpu / sum_wall if sum_wall else None,
            "transport_seconds": _spread(
                a["transport_seconds"]
                for a in attempts
                if a["phase"] == "measured" and a["transport_seconds"] is not None
            ),
            "evaluation_seconds": _spread(
                r["measurement"]["evaluation_seconds"]
                for r in records
                if r["measurement"].get("evaluation_seconds") is not None
            ),
            "first_attempt_over_case_median": _median(warm_first),
            "prepare_seconds": _spread(e["prepare_seconds"] for e in events),
            # `startup_seconds` is spawn -> ready and INCLUDES prepare (benchmark.worker).
            "startup_including_prepare_seconds": _spread(e["startup_seconds"] for e in events),
            "noise_wall": noise_floor(exp, label, algorithm, "wall"),
            "noise_cpu": noise_floor(exp, label, algorithm, "cpu"),
        }
    return block


def _sentinel_block(exp: Experiment, label: str) -> dict[str, Any]:
    runs = [r for (s, _, lb), r in exp.runs.items() if s == "timing" and lb == label]
    out: dict[str, Any] = {}
    for record in (r for run in runs for r in run.records):
        entry = out.setdefault(
            record["algorithm"], {"status": set(), "wall": [], "cpu": [], "evaluation": []}
        )
        entry["status"].add(record["status"])
        entry["wall"] += _samples(record, "wall")
        entry["cpu"] += _samples(record, "cpu")
        if record["measurement"].get("evaluation_seconds") is not None:
            entry["evaluation"].append(record["measurement"]["evaluation_seconds"])
    return {
        algorithm: {
            "status": sorted(e["status"]),
            "solve_wall_seconds": _spread(e["wall"]),
            "solve_cpu_seconds": _spread(e["cpu"]),
            "evaluation_seconds": _spread(e["evaluation"]),
        }
        for algorithm, e in out.items()
    }


CHARGED_PARTS = (
    "startup_including_prepare_seconds",
    "solve_wall_seconds",
    "transport_seconds",
    "evaluation_seconds",
)


def cold_parts(run: RunView) -> dict[tuple[str, str], dict[str, float | None]]:
    """Per (algorithm, case) of a cold run: the fresh worker's disjoint cost parts.

    The worker's `startup_seconds` runs from process start until ready, which is AFTER
    `prepare()`, so it already contains `prepare_seconds`; spawn-only time is the
    difference. `charged_seconds` = start-up (incl. prepare) + solve + transport +
    evaluation, each cost counted once (None if any part is missing, e.g. a failure)."""
    events = {e["index"]: e for e in run.manifest.prepare_events}
    out: dict[tuple[str, str], dict[str, float | None]] = {}
    for record in run.records:
        m = record["measurement"]
        event = events.get(m.get("prepare_event"), {})
        startup, prepare = event.get("startup_seconds"), event.get("prepare_seconds")
        parts: dict[str, float | None] = {
            "startup_including_prepare_seconds": startup,
            "prepare_seconds": prepare,
            "spawn_seconds": None if startup is None or prepare is None else startup - prepare,
            "solve_wall_seconds": (m.get("solve_seconds") or [None])[0],
            "solve_cpu_seconds": (m.get("solve_cpu_seconds") or [None])[0],
            "transport_seconds": (m.get("transport_seconds") or [None])[0],
            "evaluation_seconds": m.get("evaluation_seconds"),
        }
        charged = [parts[n] for n in CHARGED_PARTS]
        parts["charged_seconds"] = (
            None if any(v is None for v in charged) else sum(v for v in charged if v is not None)
        )
        out[_key(record)] = parts
    return out


def _cold_block(run: RunView) -> dict[str, Any]:
    entries: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for (algorithm, _), parts in cold_parts(run).items():
        for name, value in parts.items():
            if value is not None:
                entries[algorithm][name].append(value)
    memory: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for record in run.memory:
        if record.get("solve_peak_bytes") is not None:
            memory[record["algorithm"]]["solve_peak_bytes"].append(record["solve_peak_bytes"])
    for event in run.manifest.prepare_events:
        if event.get("pass") == "memory" and event.get("prepare_peak_bytes") is not None:
            memory[event["algorithm"]]["prepare_peak_bytes"].append(event["prepare_peak_bytes"])
    return {
        algorithm: {
            **{name: _spread(values) for name, values in entry.items()},
            **{name: _spread(values) for name, values in memory.get(algorithm, {}).items()},
            "records": sum(1 for r in run.records if r["algorithm"] == algorithm),
            "memory_records": sum(1 for r in run.memory if r["algorithm"] == algorithm),
        }
        for algorithm, entry in entries.items()
    }


def _regret_stats(values: list[float], not_applicable: int) -> dict[str, Any]:
    return {
        "cases": len(values),
        "not_applicable": not_applicable,
        "at_zero_regret": sum(1 for x in values if x == 0),
        "losses": sum(1 for x in values if x > 0),
        "gains": sum(1 for x in values if x < 0),
        "max_regret_bps": max(values, default=None),
        "mean_regret_bps": statistics.fmean(values) if values else None,
    }


def _quality_block(run: RunView, split_of: Mapping[str, str]) -> dict[str, Any]:
    """Per case: the same-scope best known score among the recorded algorithms and each
    algorithm's regret against it (bps); N/A (None) where a score is unknown. Aggregates
    are per split (tuning / held_out / sentinel), never pooled across them."""
    by_case: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for record in run.records:
        by_case[record["case_id"]][record["algorithm"]] = record
    cases: dict[str, Any] = {}
    acc: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"values": [], "na": 0})
    for case_id, records in sorted(by_case.items()):
        split = split_of.get(case_id, SENTINEL_SPLIT)
        scores = {a: _score(r) for a, r in records.items() if r["status"] == "ok"}
        known = [s for s in scores.values() if s is not None]
        best = max(known) if known else None
        row: dict[str, Any] = {"split": split,
                               "best_known_score": None if best is None else str(best)}  # fmt: skip
        for algorithm, record in records.items():
            score = scores.get(algorithm)
            regret = (
                (best - score) * 10_000 / best
                if best is not None and best > 0 and score is not None
                else None
            )
            row[algorithm] = {
                "status": record["status"],
                "score": record.get("score"),
                "regret_bps": regret,
                "quotes_counted": (record.get("quotes") or {}).get("counted"),
                "candidates_considered": record.get("candidates_considered"),
                "candidates_truncated": record.get("candidates_truncated"),
                "truncated_by": (record.get("search") or {}).get("truncated_by"),
            }
            stats = acc[(algorithm, split)]
            if regret is None:
                stats["na"] += 1
            else:
                stats["values"].append(regret)
        cases[case_id] = row
    per_algorithm: dict[str, dict[str, Any]] = defaultdict(dict)
    for (algorithm, split), stats in sorted(acc.items()):
        per_algorithm[algorithm][split] = _regret_stats(stats["values"], stats["na"])
    return {"cases": cases, "per_algorithm": dict(per_algorithm)}


def _quote_cli_block(exp: Experiment, algorithms: Sequence[str]) -> dict[str, Any]:
    entries = exp.document.get("quote_cli") or []
    one_solve = bool(entries) and all(
        e.get("exit_code") == 0
        and e.get("measurement") == {"warmup": 0, "repeats": 1, "memory_pass": False}
        and sorted(r["algorithm"] for r in e.get("records", [])) == sorted(algorithms)
        and all((r["attempts_completed"] or 0) <= 1 for r in e["records"])
        and len(e.get("prepare_events", [])) == len(algorithms)
        for e in entries
    )
    return {
        "invocations": len(entries),
        "exit_codes": [e.get("exit_code") for e in entries],
        "cli_wall_seconds": _spread(e["cli_wall_seconds"] for e in entries),
        "sum_of_solves_seconds": _spread(
            sum(s for r in e.get("records", []) for s in r.get("solve_seconds") or [])
            for e in entries
        ),
        "one_solve_per_algorithm_per_invocation": one_solve,
        "statuses": sorted(
            {(r["algorithm"], r["status"]) for e in entries for r in e.get("records", [])}
        ),  # fmt: skip
        "bundle_hashes": sorted({e.get("bundle_hash") for e in entries if e.get("bundle_hash")}),
    }


def summarize_experiment(exp: Experiment) -> dict[str, Any]:
    doc = exp.document
    algorithms = exp.algorithms
    internal = internal_checks(exp)
    semantic: dict[str, Any] = {}
    for label in sorted(set(exp.labels("timing")) | set(exp.labels("cold"))):
        fixed = exp.runs.get(("timing", "fixed", label))
        reverse = exp.runs.get(("timing", "reverse", label))
        cold = exp.runs.get(("cold", "fixed", label))
        semantic[label] = {
            "status_counts": {
                " ".join(key): {
                    a: dict(Counter(r["status"] for r in run.records if r["algorithm"] == a))
                    for a in algorithms
                }
                for key, run in sorted(exp.runs.items())
                if key[2] == label
            },
            "reverse_vs_fixed_mismatches": (
                record_mismatches(fixed.records, reverse.records) if fixed and reverse else None
            ),
            "cold_vs_warm_mismatches": (
                record_mismatches(fixed.records, cold.records) if fixed and cold else None
            ),
            "attempts_inconsistent": sorted(
                f"{key[0]} {key[1]} {r['algorithm']}/{r['case_id']}"
                for key, run in exp.runs.items()
                if key[2] == label
                for r in run.records
                if r.get("measurement", {}).get("attempts_consistent") is False
            ),
            "budget_bound": sorted(
                {
                    f"{r['algorithm']}/{r['case_id']}"
                    for key, run in exp.runs.items()
                    if key[2] == label
                    for r in run.records
                    if budget_bound(r)
                }
            ),
            "scheduled": {
                " ".join(key): run.manifest.scheduled_count
                for key, run in sorted(exp.runs.items())
                if key[2] == label
            },
        }
    split_of = exp.split_of
    return {
        "schema": SUMMARY_SCHEMA,
        "report": report_provenance(),
        "experiment": {
            "experiment_id": doc["experiment_id"],
            "created_at": doc["created_at"],
            "finished_at": doc["finished_at"],
            "partial": doc["partial"],
            "replay_command": doc["replay_command"],
            "protocol": {k: doc["protocol"][k] for k in ("path", "sha256", "key", "version")},
            "measured_source": doc["source"],
            "parent_bundle": doc["parent_bundle"],
            "profile": doc["profile"],
            "arm": doc.get("arm"),
            "bundles": doc["bundles"],
            "runs": {
                r["run_id"]: {
                    k: r[k]
                    for k in ("status_counts", "total_seconds", "loadavg_before", "loadavg_after")
                }
                for r in doc["runs"]
            },  # fmt: skip
        },
        "environment": {
            k: doc["environment"].get(k)
            for k in ("cpu_model", "cpu_count", "platform", "python_version", "dependencies")
        },
        "load": doc["load"],
        "algorithms": algorithms,
        "coverage_problems": coverage_problems(exp),
        "internal_checks": internal,
        "semantic": semantic,
        "timing": {lb: _timing_block(exp, lb) for lb in exp.labels("timing") if "matrix" in lb},
        "sentinel": {
            lb: _sentinel_block(exp, lb) for lb in exp.labels("timing") if "sentinel" in lb
        },
        "cold": {lb: _cold_block(exp.runs[("cold", "fixed", lb)]) for lb in exp.labels("cold")},
        "quality": {
            lb: _quality_block(exp.runs[("timing", "fixed", lb)], split_of)
            for lb in exp.labels("timing")
            if ("timing", "fixed", lb) in exp.runs
        },
        "quote_cli": _quote_cli_block(exp, algorithms),
        "claims": NO_TAIL_CLAIM,
    }


def summarize(path: str | Path) -> dict[str, Any]:
    return summarize_experiment(load_experiment(path))


# ------------------------------------------------------------------ comparison


def judge(
    *,
    lane: str,
    candidate_internal: int,
    semantic_mismatches: int,
    coverage_problems: int,
    baseline_internal: int,
    unproven_bounded: int,
    evidence_clean: bool,
    contaminated: bool,
    timing: Mapping[str, Mapping[str, Any]],
    charged: Mapping[str, Mapping[str, Any]],
) -> tuple[str, list[str]]:
    """The pre-registered verdict from already-computed facts, in order. `timing` maps
    "<label> <algorithm>" to a dict with `verdict` in {faster, slower, no_worthwhile_change,
    insufficient_cases, lost_samples}; `charged` to one in {slower, not_slower,
    insufficient_cases, lost_samples}. `unproven_bounded` counts exact-lane budget-bound
    records without valid identical sufficient-budget evidence."""
    if candidate_internal:
        return "reject", [f"{candidate_internal} candidate order/cold/repeat inconsistency(ies):"
                          " state leaks or nondeterminism"]  # fmt: skip
    if lane == "exact" and semantic_mismatches:
        return "reject", [f"{semantic_mismatches} semantic mismatch(es): not an exact variant"]
    if coverage_problems:
        return "inconclusive", [f"{coverage_problems} coverage problem(s): a required run or "
                                "record is missing, so failures could be hidden"]  # fmt: skip
    if baseline_internal:
        return "inconclusive", [
            f"{baseline_internal} baseline order/cold/repeat inconsistency(ies): the "
            "reference is not deterministic"
        ]
    if lane == "exact" and unproven_bounded:
        return "inconclusive", [
            f"{unproven_bounded} budget-bound record(s) without identical sufficient-budget "
            "evidence: identity under a fixed budget does not establish exactness "
            "(compare --sufficient BASE_SB CAND_SB, config/latency/l01-sufficient-budget.yaml)"
        ]
    if not evidence_clean:
        return "inconclusive", ["an experiment is partial, or ran from a dirty source tree"]
    if contaminated:
        return "inconclusive", ["host load exceeded the protocol threshold in an experiment"]
    lost = sorted(k for k, t in timing.items() if t["verdict"] == "lost_samples")
    if lost:
        return "reject", ["candidate lost timed decision cases (failures): " + ", ".join(lost)]
    slower = sorted(k for k, t in timing.items() if t["verdict"] == "slower")
    if slower:
        return "reject", ["slower: " + ", ".join(slower)]
    moved = sorted(k for k, t in charged.items() if t["verdict"] in ("slower", "lost_samples"))
    if moved:
        return "reject", ["cold charged time slower or lost (cost moved out of the solve): "
                          + ", ".join(moved)]  # fmt: skip
    faster = sorted(k for k, t in timing.items() if t["verdict"] == "faster")
    if not faster:
        return "reject", ["no algorithm reached the minimum worthwhile improvement"]
    # Cold charge is measured on the protocol's cold cohorts: every faster algorithm needs a
    # `not_slower` charged verdict there (a sor_compatible speedup is charged on full_source).
    by_alg: dict[str, list[str]] = defaultdict(list)
    for k, t in charged.items():
        by_alg[k.rsplit(" ", 1)[-1]].append(t["verdict"])
    uncharged = sorted({
        k.rsplit(" ", 1)[-1] for k in faster
        if not by_alg.get(k.rsplit(" ", 1)[-1])
        or any(v != "not_slower" for v in by_alg[k.rsplit(" ", 1)[-1]])
    })  # fmt: skip
    if uncharged:
        return "inconclusive", ["no cold charged-time evidence for faster: "
                                + ", ".join(uncharged)]  # fmt: skip
    reasons = ["faster: " + ", ".join(faster)]
    if lane == "heuristic":
        reasons.append("no default loss tolerance: owner must accept the reported regret")
        return "opt_in_only", reasons
    return "adopt_eligible", reasons


def _timing_verdict(improvements: Mapping[str, float | None], threshold: float) -> str:
    wall, cpu = improvements["wall"], improvements["cpu"]
    if wall is None or cpu is None:
        return "insufficient_cases"
    if wall >= threshold and cpu >= threshold:
        return "faster"
    if wall <= -threshold or cpu <= -threshold:
        return "slower"
    return "no_worthwhile_change"


def _mapping(
    base: Experiment, cand: Experiment, lane: str, pairs: Mapping[str, str] | None
) -> dict[str, str]:
    """candidate algorithm -> reference algorithm. Every candidate algorithm is compared
    and every baseline algorithm is a reference: nothing is dropped by intersection."""
    base_algs, cand_algs = base.algorithms, cand.algorithms
    extra = dict(pairs or {})
    if lane == "exact" and any(c != r for c, r in extra.items()):
        raise LatencyReportError("the exact lane compares each algorithm with itself; --pair "
                                 "names a different reference (use --lane heuristic)")  # fmt: skip
    mapping = {a: a for a in cand_algs if a in base_algs}
    mapping.update(extra)
    unknown = sorted({c for c in mapping if c not in cand_algs}
                     | {r for r in mapping.values() if r not in base_algs})  # fmt: skip
    unpaired = sorted(set(cand_algs) - mapping.keys())
    unused = sorted(set(base_algs) - set(mapping.values()))
    if unknown or unpaired or unused:
        raise LatencyReportError(
            f"algorithm pairing is incomplete: unknown {unknown}, candidate algorithms "
            f"without a reference {unpaired}, baseline algorithms never compared {unused}"
        )
    return mapping


def _charged_verdict(
    base_run: RunView, cand_run: RunView, b_alg: str, c_alg: str, split_of: Mapping[str, str],
    acc: Mapping[str, Any], threshold: float,
) -> dict[str, Any]:  # fmt: skip
    b_parts, c_parts = cold_parts(base_run), cold_parts(cand_run)
    ratios = []
    lost = []
    for (alg, case), parts in b_parts.items():
        b_charged = parts["charged_seconds"]
        if (alg != b_alg or split_of.get(case) != acc["decision_split"] or b_charged is None
                or b_charged < acc["min_timed_solve_seconds"]):  # fmt: skip
            continue
        c_charged = (c_parts.get((c_alg, case)) or {}).get("charged_seconds")
        if c_charged is None:
            lost.append(case)  # a charged baseline case the candidate has no charge for
        else:
            ratios.append(c_charged / b_charged)
    g = _geomean(ratios)
    improvement = None if g is None else 1 - g
    if lost:
        verdict = "lost_samples"
    elif improvement is None:
        verdict = "insufficient_cases"
    else:
        verdict = "slower" if improvement <= -threshold else "not_slower"
    return {"improvement": improvement, "decision_cases": len(ratios), "threshold": threshold,
            "lost_decision_cases": sorted(lost), "verdict": verdict}  # fmt: skip


@dataclass(frozen=True)
class SufficientEvidence:
    """A `benchmark.latency sufficient` experiment: listed records re-solved under the
    L01-SB raised budget on the main protocol's derived matrix bundles."""

    path: Path
    document: dict[str, Any]
    runs: dict[str, RunView]  # cohort -> run


def load_sufficient(path: str | Path) -> SufficientEvidence:
    path = Path(path)
    doc_path = path / "experiment.json"
    if not doc_path.is_file():
        raise LatencyReportError(f"{path}: no experiment.json")
    document = json.loads(doc_path.read_text(encoding="utf-8"))
    if document.get("schema") != SUFFICIENT_EXPERIMENT_SCHEMA:
        raise LatencyReportError(f"{path}: not a sufficient-budget experiment")
    if document.get("state") != "complete":
        raise LatencyReportError(f"{path}: experiment is {document.get('state')!r}")
    runs = {}
    for entry in document["runs"]:
        run_dir = path / "runs" / entry["run_id"]
        manifest = load_manifest(run_dir)
        runs[entry["cohort"]] = RunView(entry, manifest, load_case_records(run_dir), [], run_dir)
    return SufficientEvidence(path, document, runs)


SOURCE_PIN = ("git_revision", "git_dirty", "dirty_patch_sha256")


def sufficient_problems(evidence: SufficientEvidence, exp: Experiment) -> list[str]:
    """Why `evidence` cannot stand in for `exp`'s budget-bound records: it must be for the
    same main protocol, from the same measured source, on the same derived bundles, with
    every scheduled record present once, consistent, and actually unbounded."""
    doc, problems = evidence.document, []
    if doc["protocol"]["sha256"] != exp.document["protocol"]["sha256"]:
        problems.append("different main protocol document")
    if {k: doc["source"].get(k) for k in SOURCE_PIN} != {
        k: exp.document["source"].get(k) for k in SOURCE_PIN
    }:
        problems.append(f"measured source {doc['source'].get('git_revision')} (dirty "
                        f"{doc['source'].get('git_dirty')}) is not the experiment's "
                        f"{exp.document['source'].get('git_revision')}")  # fmt: skip
    if doc["profile"] != exp.document["profile"]:
        problems.append("different pinned profile")
    arm_pin = [(d.get("arm") or {}).get("name") for d in (doc, exp.document)]
    arms_pin = [(d.get("arms") or {}).get("sha256") for d in (doc, exp.document)]
    if arm_pin[0] != arm_pin[1] or arms_pin[0] != arms_pin[1]:
        problems.append(f"measured under arm {arm_pin[0]!r}, the experiment under {arm_pin[1]!r}")
    for cohort, run in sorted(evidence.runs.items()):
        label = f"{cohort}/matrix"
        wanted = (exp.document.get("bundles") or {}).get(label, {}).get("bundle_hash")
        if run.manifest.bundle_hash != wanted or doc["bundles"].get(label, {}).get(
            "bundle_hash"
        ) != wanted:  # fmt: skip
            problems.append(f"{label}: bundle differs from the experiment's derived bundle")
        schedule = Counter(tuple(p) for p in run.manifest.measurement.get("schedule") or [])
        if Counter(_key(r) for r in run.records) != schedule or run.manifest.state != "complete":
            problems.append(f"{label}: records do not match the complete schedule")
    return problems


def _sufficient_record(evidence: SufficientEvidence, cohort: str, alg: str, case: str) -> Any:
    run = evidence.runs.get(cohort)
    found = [r for r in (run.records if run else []) if _key(r) == (alg, case)]
    return found[0] if len(found) == 1 else None


def bounded_exactness(
    bound: Mapping[tuple[str, str, str, str], list[str]],
    base: Experiment,
    cand: Experiment,
    sufficient: tuple[SufficientEvidence, SufficientEvidence] | None,
    fields: Sequence[str],
) -> dict[str, Any]:
    """Map every budget-bound (label, candidate alg, reference alg, case) of an exact
    comparison to sufficient-budget evidence: `established` only if both experiments'
    own evidence holds the record unbounded, repeat-consistent and semantically identical;
    a difference there is a real semantic mismatch; anything else is `unproven`."""
    out: dict[str, Any] = {"required": len(bound), "established": [], "mismatches": [],
                           "unproven": [], "evidence_problems": {}}  # fmt: skip
    problems: list[str] = []
    if sufficient is not None:
        b_sb, c_sb = sufficient
        out["evidence_problems"] = {
            "baseline": sufficient_problems(b_sb, base),
            "candidate": sufficient_problems(c_sb, cand),
            "pairing": [] if b_sb.document["sufficient_budget"]["sha256"]
            == c_sb.document["sufficient_budget"]["sha256"]
            else ["the two evidence experiments used different sufficient-budget protocols"],
        }  # fmt: skip
        problems = [m for v in out["evidence_problems"].values() for m in v]
    for (label, c_alg, b_alg, case), wheres in sorted(bound.items()):
        what = f"{label} {c_alg}/{case} ({len(wheres)} bound record(s))"
        cohort, _, kind = label.partition("/")
        if sufficient is None or problems or kind != "matrix":
            reason = (
                "no sufficient-budget evidence supplied"
                if sufficient is None
                else "evidence invalid"
                if problems
                else "no evidence for a sentinel"
            )
            out["unproven"].append(f"{what}: {reason}")
            continue
        b_r = _sufficient_record(sufficient[0], cohort, b_alg, case)
        c_r = _sufficient_record(sufficient[1], cohort, c_alg, case)
        missing = [side for side, r in (("baseline", b_r), ("candidate", c_r)) if r is None]
        if missing:
            out["unproven"].append(f"{what}: not re-solved in the {'/'.join(missing)} evidence")
            continue
        still = [side for side, r in (("baseline", b_r), ("candidate", c_r))
                 if budget_bound(r) or r["status"] in KILLED
                 or r["measurement"].get("attempts_consistent") is not True]  # fmt: skip
        if still:
            out["unproven"].append(f"{what}: still budget-bound or inconsistent under the "
                                   f"sufficient budget ({'/'.join(still)})")  # fmt: skip
        elif semantic_view(b_r, fields) != semantic_view(c_r, fields):
            diff = _differing(semantic_view(b_r, fields), semantic_view(c_r, fields))
            out["mismatches"].append(f"{what}: sufficient-budget results differ in {diff}")
        else:
            out["established"].append(what)
    return out


def compare_experiments(
    base: Experiment,
    cand: Experiment,
    *,
    lane: str,
    pairs: Mapping[str, str] | None = None,
    sufficient: tuple[SufficientEvidence, SufficientEvidence] | None = None,
) -> dict[str, Any]:
    if lane not in ("exact", "heuristic"):
        raise LatencyReportError(f"unknown lane {lane!r}")
    if base.document["protocol"]["sha256"] != cand.document["protocol"]["sha256"]:
        raise LatencyReportError("the experiments used different protocol documents")
    registered = registered_comparison(base, cand, lane, pairs)
    base_bundles = {k: v["bundle_hash"] for k, v in base.document["bundles"].items()}
    cand_bundles = {k: v["bundle_hash"] for k, v in cand.document["bundles"].items()}
    if base_bundles != cand_bundles:
        raise LatencyReportError("the experiments measured different derived bundles")
    acc = base.acceptance
    fields = list(acc["exact_semantic_fields"])
    mapping = _mapping(base, cand, lane, pairs)
    split_of = base.split_of
    coverage = {"baseline": coverage_problems(base), "candidate": coverage_problems(cand),
                "pairing": []}  # fmt: skip
    for key in sorted(base.runs.keys() ^ cand.runs.keys()):
        side = "baseline" if key in cand.runs else "candidate"
        coverage["pairing"].append(f"{' '.join(key)}: run missing in the {side}")
    internal = {"baseline": internal_checks(base), "candidate": internal_checks(cand)}
    semantic: list[str] = []
    fixed_budget: list[str] = []
    bound: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    work: list[str] = []
    transitions: dict[str, Counter[str]] = defaultdict(Counter)
    status_regressions: list[str] = []
    regret: dict[str, dict[str, dict[str, Any]]] = defaultdict(
        lambda: defaultdict(lambda: {"values": [], "na": 0})
    )
    for key in sorted(base.runs.keys() & cand.runs.keys()):
        stage, order, label = key
        b_rec = {_key(r): r for r in base.runs[key].records}
        c_rec = {_key(r): r for r in cand.runs[key].records}
        for c_alg, b_alg in mapping.items():
            case_ids = {c for a, c in b_rec if a == b_alg} | {c for a, c in c_rec if a == c_alg}
            for case_id in sorted(case_ids):
                b_r, c_r = b_rec.get((b_alg, case_id)), c_rec.get((c_alg, case_id))
                where = f"{stage} {order} {label} {c_alg}/{case_id}"
                if b_r is None or c_r is None:
                    side = "baseline" if b_r is None else "candidate"
                    coverage["pairing"].append(f"{where}: record missing in the {side}")
                    continue
                transitions[f"{stage} {order} {label} {c_alg}"][
                    f"{b_r['status']}->{c_r['status']}"
                ] += 1
                if b_r["status"] == "ok" and c_r["status"] != "ok":
                    status_regressions.append(f"{where}: ok -> {c_r['status']}")
                if lane == "exact":
                    b_view, c_view = semantic_view(b_r, fields), semantic_view(c_r, fields)
                    is_bound = budget_bound(b_r) or budget_bound(c_r)
                    if is_bound:  # identical or not: exactness needs sufficient evidence
                        bound[(label, c_alg, b_alg, case_id)].append(where)
                    if b_view != c_view:
                        line = f"{where}: differs in {_differing(b_view, c_view)}"
                        (fixed_budget if is_bound else semantic).append(line)
                    if work_view(b_r) != work_view(c_r):
                        work.append(f"{where}: work counters differ")
                if stage == "timing" and order == "fixed":
                    b_s, c_s = _score(b_r), _score(c_r)
                    entry = regret[f"{label} {c_alg}"][split_of.get(case_id, SENTINEL_SPLIT)]
                    if b_r["status"] == "ok" and b_s is not None and b_s > 0 and c_s is not None:
                        entry["values"].append((b_s - c_s) * 10_000 / b_s)
                    else:
                        entry["na"] += 1
    timing: dict[str, Any] = {}
    for label in base.labels("timing"):
        if "matrix" not in label:
            continue
        medians = {
            (who, kind): _pooled_medians(exp, label, kind)
            for who, exp in (("base", base), ("cand", cand))
            for kind in ("wall", "cpu")
        }
        for c_alg, b_alg in mapping.items():
            improvements: dict[str, float | None] = {}
            cases: dict[str, int] = {}
            lost: set[str] = set()
            for kind in ("wall", "cpu"):
                b_med, c_med = medians[("base", kind)], medians[("cand", kind)]
                ratios = []
                for (alg, case), value in b_med.items():
                    if (alg != b_alg or split_of.get(case) != acc["decision_split"]
                            or medians[("base", "wall")].get((alg, case), 0.0)
                            < acc["min_timed_solve_seconds"]):  # fmt: skip
                        continue
                    if (c_alg, case) in c_med:
                        ratios.append(c_med[(c_alg, case)] / value)
                    else:
                        lost.add(case)  # a timed baseline case the candidate did not time
                g = _geomean(ratios)
                improvements[kind] = None if g is None else 1 - g
                cases[kind] = len(ratios)
            noise = {
                f"{who}_{kind}": noise_floor(exp, label, alg, kind)["noise"]
                for who, exp, alg in (("base", base, b_alg), ("cand", cand, c_alg))
                for kind in ("wall", "cpu")
            }
            known = [n for n in noise.values() if n is not None]
            threshold = max(
                acc["minimum_worthwhile_improvement"],
                acc["noise_multiplier"] * max(known, default=0.0),
            )
            timing[f"{label} {c_alg}"] = {
                "reference": b_alg,
                "improvement": improvements,
                "decision_cases": cases,
                "lost_decision_cases": sorted(lost),
                "noise": noise,
                "threshold": threshold,
                "verdict": "lost_samples" if lost else _timing_verdict(improvements, threshold),
            }
    charged: dict[str, Any] = {}
    charged_costs: dict[str, Any] = {}
    for label in sorted(set(base.labels("cold")) & set(cand.labels("cold"))):
        b_run, c_run = base.runs[("cold", "fixed", label)], cand.runs[("cold", "fixed", label)]
        b_cold, c_cold = _cold_block(b_run), _cold_block(c_run)
        for c_alg, b_alg in mapping.items():
            charged_costs[f"{label} {c_alg}"] = {
                name: {
                    "baseline_median": b_cold.get(b_alg, {}).get(name, {}).get("median"),
                    "candidate_median": c_cold.get(c_alg, {}).get(name, {}).get("median"),
                }
                for name in ("prepare_seconds", "startup_including_prepare_seconds",
                             "charged_seconds", "solve_peak_bytes", "prepare_peak_bytes")
            }  # fmt: skip
            if "matrix" in label:
                threshold = (timing.get(f"{label} {c_alg}") or {}).get(
                    "threshold", acc["minimum_worthwhile_improvement"]
                )
                charged[f"{label} {c_alg}"] = _charged_verdict(
                    b_run, c_run, b_alg, c_alg, split_of, acc, threshold
                )
    clean = all(
        not e.document["partial"] and e.document["source"]["git_dirty"] is False
        for e in (base, cand)
    )
    contaminated = any((e.document["load"] or {}).get("contaminated") for e in (base, cand))
    n_coverage = sum(len(v) for v in coverage.values())
    bounded: dict[str, Any] = (
        bounded_exactness(bound, base, cand, sufficient, fields)
        if lane == "exact"
        else {"required": 0, "established": [], "mismatches": [], "unproven": [],
              "evidence_problems": {}}
    )  # fmt: skip
    verdict, reasons = judge(
        lane=lane,
        candidate_internal=sum(len(v) for v in internal["candidate"].values()),
        semantic_mismatches=len(semantic) + len(bounded["mismatches"]),
        coverage_problems=n_coverage,
        baseline_internal=sum(len(v) for v in internal["baseline"].values()),
        unproven_bounded=len(bounded["unproven"]),
        evidence_clean=clean,
        contaminated=contaminated,
        timing=timing,
        charged=charged,
    )
    if status_regressions:
        reasons.append(f"{len(status_regressions)} status regression(s) ok -> failure")
    return {
        "schema": COMPARISON_SCHEMA,
        "report": report_provenance(),
        "lane": lane,
        "baseline": {"experiment_id": base.document["experiment_id"],
                     "measured_source": base.document["source"], "load": base.document["load"]},
        "candidate": {"experiment_id": cand.document["experiment_id"],
                      "measured_source": cand.document["source"], "load": cand.document["load"]},
        "pairs": mapping,
        "arms": registered,
        "protocol_sha256": base.document["protocol"]["sha256"],
        "acceptance": acc,
        "coverage_problems": coverage,
        "internal_checks": internal,
        "semantic_mismatches": semantic,
        "fixed_budget_differences": fixed_budget,
        "bounded_exactness": bounded,
        "work_differences": work,
        "status_transitions": {k: dict(v) for k, v in sorted(transitions.items())},
        "status_regressions": status_regressions,
        "regret": {
            k: {split: _regret_stats(v["values"], v["na"]) for split, v in sorted(by.items())}
            for k, by in sorted(regret.items())
        },
        "timing": timing,
        "charged": charged,
        "charged_costs": charged_costs,
        "verdict": verdict,
        "reasons": reasons,
        "claims": NO_TAIL_CLAIM,
    }  # fmt: skip


def registered_comparison(
    base: Experiment, cand: Experiment, lane: str, pairs: Mapping[str, str] | None
) -> dict[str, Any] | None:
    """For L08 arm experiments: the pre-registered comparison this is, else refuse. Both
    must come from the same arms file and the same measured source; the exact lane also
    refuses different algorithm scope or heuristic (shortlist/sampling) settings, which no
    identity of outputs could make an exact variant."""
    if base.arm is None and cand.arm is None:
        return None
    if base.arm is None or cand.arm is None:
        raise LatencyReportError("an L08 arm experiment is only compared with another arm")
    if base.document["arms"]["sha256"] != cand.document["arms"]["sha256"]:
        raise LatencyReportError("the experiments used different arms files")
    pin = {k: base.document["source"].get(k) for k in SOURCE_PIN}
    if pin != {k: cand.document["source"].get(k) for k in SOURCE_PIN}:
        raise LatencyReportError("arm experiments of one comparison must share one source")
    names = (base.arm["name"], cand.arm["name"])
    for item in base.document["arms"]["document"]["comparisons"]:
        if (
            (item["baseline"], item["candidate"]) == names
            and item["lane"] == lane
            and dict(item.get("pairs") or {}) == dict(pairs or {})
        ):
            break
    else:
        raise LatencyReportError(
            f"{names[0]} -> {names[1]} ({lane}, pairs {dict(pairs or {})}) is not a "
            "pre-registered comparison of the arms file"
        )
    if lane == "exact":
        scope = ("algorithms", "shortlist", "sampling")
        if {k: base.arm.get(k) for k in scope} != {k: cand.arm.get(k) for k in scope}:
            raise LatencyReportError("the exact lane refuses arms with different algorithm "
                                     "scope or heuristic settings")  # fmt: skip
    return {"id": item["id"], "role": item["role"], "baseline": names[0],
            "candidate": names[1], "arms_sha256": base.document["arms"]["sha256"],
            "baseline_controls": base.arm["controls"],
            "candidate_controls": cand.arm["controls"]}  # fmt: skip


def compare(
    baseline: str | Path,
    candidate: str | Path,
    *,
    lane: str,
    pairs: Mapping[str, str] | None = None,
    sufficient: tuple[str | Path, str | Path] | None = None,
) -> dict[str, Any]:
    evidence = None if sufficient is None else (
        load_sufficient(sufficient[0]), load_sufficient(sufficient[1])
    )  # fmt: skip
    return compare_experiments(
        load_experiment(baseline), load_experiment(candidate), lane=lane, pairs=pairs,
        sufficient=evidence,
    )  # fmt: skip


def _brief(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": record["status"],
        "score": record.get("score"),
        "quotes_counted": (record.get("quotes") or {}).get("counted"),
        "limit_hit": record.get("limit_hit"),
        "truncated_by": (record.get("search") or {}).get("truncated_by"),
        "budget_bound": budget_bound(record),
        "attempts_consistent": record.get("measurement", {}).get("attempts_consistent"),
        "solve_seconds": record.get("measurement", {}).get("solve_seconds"),
    }


def summarize_sufficient(
    evidence: SufficientEvidence, against: Experiment | None = None
) -> dict[str, Any]:
    """Each re-solved record under the raised budget, beside its fixed-budget counterpart
    (the `against` experiment's fixed-order timing record): fixed-budget completion is
    reported separately from sufficient-budget semantics, never merged."""
    doc = evidence.document
    fields = list(against.acceptance["exact_semantic_fields"]) if against else []
    rows = []
    for cohort, run in sorted(evidence.runs.items()):
        fixed_run = against.runs.get(("timing", "fixed", f"{cohort}/matrix")) if against else None
        fixed = {_key(r): r for r in fixed_run.records} if fixed_run else {}
        for record in run.records:
            f = fixed.get(_key(record))
            rows.append({
                "cohort": cohort, "algorithm": record["algorithm"], "case_id": record["case_id"],
                "sufficient_budget": _brief(record),
                "fixed_budget": None if f is None else _brief(f),
                "differs_from_fixed_budget_in": None if f is None else _differing(
                    semantic_view(f, fields), semantic_view(record, fields)
                ),
            })  # fmt: skip
    return {
        "schema": "latency-sufficient-summary/1",
        "report": report_provenance(),
        "experiment": {k: doc[k] for k in ("experiment_id", "created_at", "finished_at",
                                            "replay_command", "protocol", "source", "profile",
                                            "budget", "fixed_budget", "bundles")},
        "sufficient_budget": {k: doc["sufficient_budget"][k]
                              for k in ("path", "sha256", "key", "version")},
        "load": doc["load"],
        "records": rows,
        "against": None if against is None else {
            "experiment_id": against.document["experiment_id"],
            "measured_source": against.document["source"],
            "pin_problems": sufficient_problems(evidence, against),
        },
    }  # fmt: skip


# ------------------------------------------------------------------ rendering


def _f(value: Any, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _agree(mismatches: list[str] | None, algorithm: str) -> str:
    if mismatches is None:
        return "N/A"
    count = sum(1 for m in mismatches if m.startswith(algorithm + "/"))
    return f"**{count} mismatch(es)**" if count else "yes"


def _source(src: Mapping[str, Any]) -> str:
    patch = src.get("dirty_patch_sha256")
    return f"`{src.get('git_revision')}` dirty={src.get('git_dirty')}" + (
        f" patch `{patch}`" if src.get("git_dirty") else ""
    )


def _problems(title: str, items: Sequence[str], limit: int = 20) -> list[str]:
    if not items:
        return [f"- {title}: none"]
    shown = [f"  - {m}" for m in items[:limit]]
    more = [f"  - … {len(items) - limit} more (see JSON)"] if len(items) > limit else []
    return [f"- **{title}: {len(items)}**", *shown, *more]


def render_summary(summary: Mapping[str, Any]) -> str:
    e, load = summary["experiment"], summary["load"] or {}
    rep = summary["report"]
    lines = [
        f"# L01 latency baseline — experiment `{e['experiment_id']}`",
        "",
        f"- Protocol: `{e['protocol']['path']}` {e['protocol']['key']} v{e['protocol']['version']}"
        f" (sha256 `{e['protocol']['sha256']}`)",
        f"- Measured source (raw records): {_source(e['measured_source'])}; measured "
        f"{e['created_at']} → {e['finished_at']}",
        f"- Report generated {rep['generated_at']} from {_source(rep)} (report tree "
        f"`{rep['report_tree_id']}`); regenerating a report never changes what was measured",
        f"- Parent bundle: `{e['parent_bundle']['bundle_id']}` "
        f"(`{e['parent_bundle']['bundle_hash']}`); profile `{e['profile']['path']}` "
        f"(`{e['profile']['sha256']}`)",
        f"- Host: {summary['environment']['cpu_model']}, {summary['environment']['cpu_count']} "
        f"CPUs, {summary['environment']['platform']}, Python "
        f"{summary['environment']['python_version']}",
        f"- Load: max 1-min load {_f(load.get('max_loadavg_1m'), 2)} over "
        f"{load.get('samples')} samples; threshold {_f(load.get('threshold_loadavg_1m'), 2)}; "
        f"**contaminated: {load.get('contaminated')}**",
        f"- Partial: {e['partial']}. Replay: `{e['replay_command']}`",
        *([f"- L08 arm `{e['arm']['name']}`: {e['arm']['quote_path']}; algorithms "
           f"{e['arm']['algorithms'] or 'reference'}; shortlist {e['arm']['shortlist']}; "
           f"sampling {e['arm']['sampling']}; stages {e['arm']['stages']}"]
          if e.get("arm") else []),
        "",
        f"_{summary['claims']}_",
        "",
        "## Coverage and determinism gates",
        "",
        *_problems("Coverage problems", summary["coverage_problems"]),
        *_problems("Fixed vs reverse order mismatches",
                   summary["internal_checks"]["cross_order"]),
        *_problems("Cold vs warm process mismatches", summary["internal_checks"]["cold_warm"]),
        *_problems("Records with inconsistent attempts",
                   summary["internal_checks"]["attempts_inconsistent"]),
        "",
        "## Statuses and deterministic checks",
        "",
        "Statuses of the fixed-order timing run (every run's counts are in the JSON). "
        "`budget-bound` = a limit hit or declared budget truncation (fixed-budget scope).",
        "",
        "| Bundle | Algorithm | Statuses | reverse≡fixed | cold≡warm | budget-bound |",
        "| --- | --- | --- | --- | --- | --- |",
    ]  # fmt: skip
    for label, block in summary["semantic"].items():
        rev, cold = block["reverse_vs_fixed_mismatches"], block["cold_vs_warm_mismatches"]
        counts_by_run = block["status_counts"]
        first = counts_by_run.get(f"timing fixed {label}") or next(iter(counts_by_run.values()))
        for algorithm, counts in first.items():
            status = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            bound = [b for b in block["budget_bound"] if b.startswith(algorithm + "/")]
            lines.append(
                f"| {label} | {algorithm} | {status} | {_agree(rev, algorithm)} | "
                f"{_agree(cold, algorithm)} | "
                f"{', '.join(b.split('/', 1)[1] for b in bound) or '—'} |"
            )
    lines += ["", "## Warm-process solve time over the matrix (headline, uninstrumented)", ""]
    lines += [
        "Per-case medians pooled over both orders. Seconds. `A/A` = exp|ln geomean"
        "(reverse/fixed)| − 1 over held-out cases above the timing floor.",
        "",
        "| Bundle | Algorithm | cases | median case wall | max case wall | Σ case wall | "
        "Σ case CPU | CPU/wall | transport median | evaluation median | prepare median | "
        "A/A wall | A/A CPU |",
        "| --- | --- " + "| ---: " * 11 + "|",
    ]
    for label, block in summary["timing"].items():
        for algorithm, t in block.items():
            lines.append(
                f"| {label} | {algorithm} | {t['cases_with_samples']} | "
                f"{_f(t['case_median_wall_seconds']['median'])} | "
                f"{_f(t['case_median_wall_seconds']['max'])} | "
                f"{_f(t['sum_of_case_medians_wall_seconds'])} | "
                f"{_f(t['sum_of_case_medians_cpu_seconds'])} | {_f(t['cpu_over_wall'], 2)} | "
                f"{_f(t['transport_seconds']['median'], 4)} | "
                f"{_f(t['evaluation_seconds']['median'], 4)} | "
                f"{_f(t['prepare_seconds']['median'], 4)} | "
                f"{_f(t['noise_wall']['noise'])} (n={t['noise_wall']['cases']}) | "
                f"{_f(t['noise_cpu']['noise'])} (n={t['noise_cpu']['cases']}) |"
            )
    lines += ["", "## Sentinel (USDC → USDT 1000), warm process", ""]
    lines += [
        "| Cohort | Algorithm | status | n | wall min / median / max | CPU min / median / max |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for label, block in summary["sentinel"].items():
        for algorithm, s in block.items():
            w, c = s["solve_wall_seconds"], s["solve_cpu_seconds"]
            lines.append(
                f"| {label} | {algorithm} | {', '.join(s['status'])} | {w['n']} | "
                f"{_f(w['min'])} / {_f(w['median'])} / {_f(w['max'])} | "
                f"{_f(c['min'])} / {_f(c['median'])} / {_f(c['max'])} |"
            )
    lines += ["", "## Cold process (fresh worker per case) and separate memory pass", ""]
    lines += [
        "Start-up is process start → ready and **includes** prepare; spawn = start-up − "
        "prepare. Charged = start-up (incl. prepare) + solve + transport + evaluation, each "
        "cost once. Medians over the cases (n = cases with every part).",
        "",
        "| Bundle | Algorithm | start-up (incl. prepare) | prepare | spawn | cold solve | "
        "transport | evaluation | charged (n) | solve peak max (MiB) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, block in summary["cold"].items():
        for algorithm, c in block.items():
            peak = (c.get("solve_peak_bytes") or {}).get("max")

            def med(name: str, digits: int = 3, c: Mapping[str, Any] = c) -> str:
                return _f((c.get(name) or {}).get("median"), digits)

            lines.append(
                f"| {label} | {algorithm} | {med('startup_including_prepare_seconds')} | "
                f"{med('prepare_seconds', 4)} | {med('spawn_seconds')} | "
                f"{med('solve_wall_seconds')} | {med('transport_seconds', 4)} | "
                f"{med('evaluation_seconds', 4)} | {med('charged_seconds')} "
                f"({(c.get('charged_seconds') or {}).get('n', 0)}/{c['records']}) | "
                f"{_f(None if peak is None else peak / 2**20, 1)} |"
            )
    q = summary["quote_cli"]
    lines += [
        "",
        "## Complete single-request response (`main.py quote`, sentinel)",
        "",
        f"- Invocations: {q['invocations']}, exit codes {q['exit_codes']}; one solve per "
        f"algorithm per invocation: **{q['one_solve_per_algorithm_per_invocation']}**",
        f"- CLI wall (process start → exit) min / median / max: "
        f"{_f(q['cli_wall_seconds']['min'])} / {_f(q['cli_wall_seconds']['median'])} / "
        f"{_f(q['cli_wall_seconds']['max'])} s; sum of the six solves in those runs: "
        f"{_f(q['sum_of_solves_seconds']['min'])} / {_f(q['sum_of_solves_seconds']['median'])}"
        f" / {_f(q['sum_of_solves_seconds']['max'])} s",
        "",
        "## Quality against the same-scope best known (fixed-order records, per split)",
        "",
        "Regret = (best known − score) × 10,000 / best known among the six algorithms on "
        "the same bundle, cohort, objective and budget — not a mathematical optimum. N/A = "
        "unknown score or failure, never zero. Tuning and held-out cases are never pooled.",
        "",
        "| Bundle | Algorithm | split | cases | at best known | N/A | max regret bps | "
        "mean regret bps |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, block in summary["quality"].items():
        if "matrix" not in label:
            continue
        for algorithm, by_split in block["per_algorithm"].items():
            for split in ("held_out", "tuning"):
                if split not in by_split:
                    continue
                s = by_split[split]
                lines.append(
                    f"| {label} | {algorithm} | {split} | {s['cases']} | {s['at_zero_regret']} "
                    f"| {s['not_applicable']} | {_f(s['max_regret_bps'], 2)} | "
                    f"{_f(s['mean_regret_bps'], 2)} |"
                )
    return "\n".join(lines) + "\n"


def render_comparison(result: Mapping[str, Any]) -> str:
    coverage = [m for side in ("baseline", "candidate", "pairing")
                for m in (f"{side}: {x}" for x in result["coverage_problems"][side])]  # fmt: skip
    internal = [
        f"{side} {gate}: {m}"
        for side in ("candidate", "baseline")
        for gate, items in result["internal_checks"][side].items()
        for m in items
    ]
    rep = result["report"]
    lines = [
        f"# L01 comparison ({result['lane']} lane): **{result['verdict']}**",
        "",
        *[f"- {r}" for r in result["reasons"]],
        f"- Baseline `{result['baseline']['experiment_id']}` measured on "
        f"{_source(result['baseline']['measured_source'])}; candidate "
        f"`{result['candidate']['experiment_id']}` measured on "
        f"{_source(result['candidate']['measured_source'])}",
        f"- Report generated {rep['generated_at']} from {_source(rep)}",
        *_problems("Coverage problems", coverage),
        *_problems("Order/cold/repeat inconsistencies", internal),
        *_problems("Semantic mismatches", result["semantic_mismatches"]),
        *_problems("Fixed-budget completion differences (reported apart from exactness)",
                   result["fixed_budget_differences"]),
        f"- Budget-bound records needing sufficient-budget exactness: "
        f"{result['bounded_exactness']['required']}; established "
        f"{len(result['bounded_exactness']['established'])}",
        *_problems("Unproven budget-bound exactness", result["bounded_exactness"]["unproven"]),
        *_problems("Sufficient-budget mismatches", result["bounded_exactness"]["mismatches"]),
        f"- Work-counter differences: {len(result['work_differences'])}; status "
        f"regressions ok → failure: {len(result['status_regressions'])}",
        "",
        "| Bundle algorithm | reference | wall improvement | CPU improvement | cases | "
        "threshold | verdict | cold charged improvement | charged verdict |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- | ---: | --- |",
    ]  # fmt: skip
    for key, t in result["timing"].items():
        c = result["charged"].get(key) or {}
        lines.append(
            f"| {key} | {t['reference']} | {_f(t['improvement']['wall'])} | "
            f"{_f(t['improvement']['cpu'])} | {t['decision_cases']['wall']} | "
            f"{_f(t['threshold'])} | {t['verdict']} | {_f(c.get('improvement'))} | "
            f"{c.get('verdict', 'N/A')} |"
        )
    lines += [
        "",
        "Held-out paired regret (fixed order) against the same-scope reference:",
        "",
        "| Bundle algorithm | cases | N/A | losses | gains | max regret bps | mean regret bps |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key, by_split in result["regret"].items():
        s = by_split.get("held_out")
        if s is not None:
            lines.append(
                f"| {key} | {s['cases']} | {s['not_applicable']} | {s['losses']} | "
                f"{s['gains']} | {_f(s['max_regret_bps'], 2)} | {_f(s['mean_regret_bps'], 2)} |"
            )
    lines += ["", f"_{result['claims']}_"]
    return "\n".join(lines) + "\n"


def render_sufficient(summary: Mapping[str, Any]) -> str:
    e, sb, rep = summary["experiment"], summary["sufficient_budget"], summary["report"]
    load = summary["load"] or {}
    lines = [
        f"# L01 sufficient-budget evidence — experiment `{e['experiment_id']}`",
        "",
        f"- Sufficient-budget protocol: `{sb['path']}` {sb['key']} v{sb['version']} (sha256 "
        f"`{sb['sha256']}`); main protocol sha256 `{e['protocol']['sha256']}`",
        f"- Budget: {e['budget']} (fixed budget replaced: {e['fixed_budget']})",
        f"- Measured source: {_source(e['source'])}; measured {e['created_at']} → "
        f"{e['finished_at']}; bundles {e['bundles']}",
        f"- Report generated {rep['generated_at']} from {_source(rep)}",
        f"- Load: max 1-min load {_f(load.get('max_loadavg_1m'), 2)}; **contaminated: "
        f"{load.get('contaminated')}** (semantic evidence only; no timing claim)",
        f"- Replay: `{e['replay_command']}`",
    ]
    against = summary["against"]
    if against is not None:
        lines += [
            f"- Fixed-budget counterpart: experiment `{against['experiment_id']}` measured on "
            f"{_source(against['measured_source'])}",
            *_problems("Pin problems (this evidence cannot stand in for that experiment in "
                       "`compare --sufficient`)", against["pin_problems"]),
        ]  # fmt: skip
    lines += [
        "",
        "| Cohort | Algorithm | Case | budget | status | score | quotes counted | "
        "truncated_by / limit | budget-bound | attempts consistent | solve s |",
        "| --- | --- | --- | --- | --- | --- | ---: | --- | --- | --- | --- |",
    ]
    for row in summary["records"]:
        for name in ("fixed_budget", "sufficient_budget"):
            b = row[name]
            if b is None:
                continue
            solve = ", ".join(f"{v:.3f}" for v in b["solve_seconds"] or [])
            lines.append(
                f"| {row['cohort']} | {row['algorithm']} | {row['case_id']} | {name} | "
                f"{b['status']} | {b['score']} | {b['quotes_counted']} | "
                f"{b['truncated_by'] or b['limit_hit'] or '—'} | {b['budget_bound']} | "
                f"{b['attempts_consistent']} | {solve} |"
            )
        if row["differs_from_fixed_budget_in"] is not None:
            differs = row["differs_from_fixed_budget_in"] or "none"
            lines.append(f"| | | | semantic fields differing from the fixed budget: {differs} "
                         "| | | | | | | |")  # fmt: skip
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ L08 final aggregation

FINAL_SCHEMA = "latency-final/1"
LISTED = 50  # list entries kept per problem list in the final JSON (full lists: `compare`)


def _listed(items: Sequence[Any]) -> dict[str, Any]:
    return {"count": len(items), "first": list(items[:LISTED])}


def _compact(result: Mapping[str, Any]) -> dict[str, Any]:
    """The decision facts of one `compare_experiments` result; every problem list keeps
    its full count (the complete lists are regenerated by `report.latency compare`)."""
    coverage = [f"{side}: {m}" for side, items in result["coverage_problems"].items()
                for m in items]  # fmt: skip
    internal = [f"{side} {gate}: {m}" for side, gates in result["internal_checks"].items()
                for gate, items in gates.items() for m in items]  # fmt: skip
    bounded = result["bounded_exactness"]
    return {
        "baseline": result["baseline"], "candidate": result["candidate"],
        "pairs": result["pairs"], "arms": result["arms"],
        "coverage_problems": _listed(coverage), "internal_checks": _listed(internal),
        "semantic_mismatches": _listed(result["semantic_mismatches"]),
        "fixed_budget_differences": _listed(result["fixed_budget_differences"]),
        "bounded_exactness": {"required": bounded["required"],
                              "established": len(bounded["established"]),
                              "mismatches": bounded["mismatches"],
                              "unproven": bounded["unproven"],
                              "evidence_problems": bounded["evidence_problems"]},
        "work_differences": _listed(result["work_differences"]),
        "status_transitions": result["status_transitions"],
        "status_regressions": _listed(result["status_regressions"]),
        "regret": result["regret"], "timing": result["timing"], "charged": result["charged"],
        "charged_costs": result["charged_costs"],
    }  # fmt: skip


def _heuristic_view(search: Mapping[str, Any]) -> dict[str, Any] | None:
    """`uni_sor_fast` scope, fallback, sampling-stop and quote-phase counters (route ids,
    selections and per-round detail stay in the raw records)."""
    if "sor_fast" not in search:
        return None
    shortlist = search.get("shortlist") or {}
    sampling = search.get("sampling") or {}
    incumbent = sampling.get("incumbent") or {}
    return {
        "search_scope": search.get("search_scope"),
        "search_completed": search.get("search_completed"),
        "eligible_pools": shortlist.get("eligible_pools"),
        "searched_pools": shortlist.get("searched_pools"),
        "searched_routes": shortlist.get("searched_routes"),
        "fallback": shortlist.get("fallback"),
        "quotes_by_phase": shortlist.get("quotes"),
        "sampling": None if not sampling else {
            "stop_reason": sampling.get("stop_reason"), "rounds": sampling.get("rounds"),
            "sampled_entries": sampling.get("sampled_entries"),
            "grid_entries": sampling.get("grid_entries"),
            "validations": sampling.get("validations"),
            "rejected_incumbents": sampling.get("rejected_incumbents"),
            "seed": sampling.get("seed"), "grid_completion": sampling.get("grid_completion"),
            "incumbent_source": incumbent.get("source"),
            "soft_limit_reached": (sampling.get("soft_limit") or {}).get("reached"),
        },
    }  # fmt: skip


def case_records(exp: Experiment) -> list[dict[str, Any]]:
    """One machine-readable row per (bundle, algorithm, case) of an experiment: status and
    score of the fixed-order timing record, pooled solve medians, the cold charge parts and
    the memory pass's solve peak, plus control and heuristic scope counters."""
    name = (exp.arm or {}).get("name")
    rows: list[dict[str, Any]] = []
    for label in exp.labels("timing"):
        fixed = exp.runs.get(("timing", "fixed", label))
        cold = exp.runs.get(("cold", "fixed", label))
        parts = cold_parts(cold) if cold else {}
        peaks = {_key(m): m.get("solve_peak_bytes") for m in (cold.memory if cold else [])}
        walls, cpus = _pooled_medians(exp, label, "wall"), _pooled_medians(exp, label, "cpu")
        pooled: Counter[tuple[str, str]] = Counter()
        for (stage, _, run_label), run in exp.runs.items():
            if stage == "timing" and run_label == label:
                for r in run.records:
                    pooled[_key(r)] += len(_samples(r, "wall"))
        for r in fixed.records if fixed else []:
            key, search = _key(r), r.get("search") or {}
            controls = search.get(CONTROL_STATS_KEY)
            rows.append({
                "arm": name, "bundle": label, "algorithm": key[0], "case_id": key[1],
                "split": exp.split(key[1]), "status": r["status"], "score": r.get("score"),
                "error": r.get("error"),
                "quotes_counted": (r.get("quotes") or {}).get("counted"),
                "budget_bound": budget_bound(r), "truncated_by": search.get("truncated_by"),
                "solve_wall_median_seconds": walls.get(key),
                "solve_cpu_median_seconds": cpus.get(key), "samples": pooled[key],
                "evaluation_seconds": r["measurement"].get("evaluation_seconds"),
                "cold": parts.get(key), "solve_peak_bytes": peaks.get(key),
                "controls": None if controls is None else {
                    k: controls.get(k) for k in ("tick_math", "bin_math", "prefix",
                                                 "graph_reuse_bound")},
                "graph_reuse": search.get("graph_reuse"),
                "heuristic": _heuristic_view(search),
            })  # fmt: skip
    return rows


def final_report(session_dir: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Every pre-registered comparison of one L08 session, judged by `compare_experiments`
    exactly as registered (exact lane with both arms' own sufficient-budget evidence),
    with its disposition, plus per-arm facts and per-case records. An arm the session did
    not complete is `not_measured` in every comparison that needs it -- never a verdict."""
    session_dir = Path(session_dir)
    path = session_dir / SESSION_FILE
    if not path.is_file():
        raise LatencyReportError(f"{session_dir}: no {SESSION_FILE}")
    session = json.loads(path.read_text(encoding="utf-8"))
    if session.get("schema") != "latency-session/1":
        raise LatencyReportError(f"{session_dir}: unsupported schema {session.get('schema')!r}")
    arms_doc, arms_sha = session["arms"]["document"], session["arms"]["sha256"]
    entries = {e["arm"]: e for e in session["entries"]}
    experiments: dict[str, Experiment] = {}
    evidence: dict[str, SufficientEvidence] = {}
    missing: dict[str, str] = {}
    for name in session["order"]:
        entry = entries.get(name) or {}
        if not entry.get("experiment"):
            missing[name] = f"arm not measured (session {session['state']})"
            continue
        try:
            exp = load_experiment(session_dir / entry["experiment"])
        except LatencyReportError as exc:
            missing[name] = str(exc)
            continue
        if (exp.document.get("arms") or {}).get("sha256") != arms_sha or (
            (exp.arm or {}).get("name") != name
        ):  # fmt: skip
            missing[name] = "experiment is not this session's arm"
            continue
        experiments[name] = exp
        if entry.get("sufficient"):
            evidence[name] = load_sufficient(session_dir / entry["sufficient"])
    # l08.yaml `source: every_arm_same_clean_commit`: every experiment and sufficient-budget
    # run must carry the session's own source pin -- checked against the session, not only
    # pairwise, so comparisons that share no arm cannot come from different sources either.
    pin = {k: (session.get("source") or {}).get(k) for k in SOURCE_PIN}
    foreign = sorted(
        f"{kind} {name}: {doc['source'].get('git_revision')} (dirty "
        f"{doc['source'].get('git_dirty')})"
        for kind, docs in (("arm", {n: e.document for n, e in experiments.items()}),
                           ("sufficient-budget", {n: e.document for n, e in evidence.items()}))
        for name, doc in docs.items()
        if {k: (doc.get("source") or {}).get(k) for k in SOURCE_PIN} != pin
    )  # fmt: skip
    if foreign:
        raise LatencyReportError(
            f"{session_dir}: measured sources differ from the session's "
            f"{pin['git_revision']} (dirty {pin['git_dirty']}): " + "; ".join(foreign)
        )
    comparisons: list[dict[str, Any]] = []
    for item in arms_doc["comparisons"]:
        lane, base, cand = item["lane"], item["baseline"], item["candidate"]
        row: dict[str, Any] = {k: item.get(k) for k in ("id", "lane", "role", "baseline",
                                                        "candidate", "pairs")}  # fmt: skip
        absent = [n for n in (base, cand) if n not in experiments]
        if absent:
            row.update(verdict="not_measured",
                       reasons=[f"{n}: {missing.get(n)}" for n in absent],
                       disposition="not adopted (not measured)", comparison=None)  # fmt: skip
        else:
            sufficient = (
                (evidence[base], evidence[cand])
                if lane == "exact" and base in evidence and cand in evidence
                else None
            )
            result = compare_experiments(
                experiments[base], experiments[cand], lane=lane, pairs=item.get("pairs"),
                sufficient=sufficient,
            )  # fmt: skip
            row.update(verdict=result["verdict"], reasons=result["reasons"],
                       disposition=arms_doc["dispositions"][lane][result["verdict"]],
                       comparison=_compact(result))  # fmt: skip
        comparisons.append(row)
    arms: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    for name, exp in experiments.items():
        internal = internal_checks(exp)
        arms[name] = {
            "experiment_id": exp.document["experiment_id"],
            "sufficient_id": (entries[name].get("sufficient")),
            "created_at": exp.document["created_at"], "finished_at": exp.document["finished_at"],
            "arm": exp.arm, "load": exp.document["load"],
            "measured_source": exp.document["source"],
            "coverage_problems": len(coverage_problems(exp)),
            "internal_problems": sum(len(v) for v in internal.values()),
            "quote_cli": _quote_cli_block(exp, exp.algorithms) if "quote_cli" in (
                exp.document.get("stages_requested") or []) else None,
        }  # fmt: skip
        records += case_records(exp)
    result = {
        "schema": FINAL_SCHEMA,
        "report": report_provenance(),
        "session": {k: session.get(k) for k in ("session_id", "state", "created_at",
                                                "finished_at", "stop_reasons", "source",
                                                "replay_command", "order")},
        "arms_file": {k: session["arms"][k] for k in ("path", "sha256", "key", "version")},
        "arms": arms,
        "not_measured": missing,
        "comparisons": comparisons,
        "claims": NO_TAIL_CLAIM,
    }  # fmt: skip
    return result, records


def render_final(result: Mapping[str, Any]) -> str:
    s, rep = result["session"], result["report"]
    lines = [
        f"# L08 final latency comparisons — session `{s['session_id']}` ({s['state']})",
        "",
        f"- Arms file `{result['arms_file']['path']}` {result['arms_file']['key']} "
        f"v{result['arms_file']['version']} (sha256 `{result['arms_file']['sha256']}`)",
        f"- Measured source: {_source(s['source'])}; {s['created_at']} → {s['finished_at']}",
        f"- Report generated {rep['generated_at']} from {_source(rep)}",
        f"- Replay: `{s['replay_command']}`",
        *_problems("Session stop reasons", s.get("stop_reasons") or []),
        *_problems("Arms not measured", [f"{k}: {v}" for k, v in result["not_measured"].items()]),
        "",
        "| Arm | experiment | max 1-min load | contaminated | coverage problems | "
        "order/cold/repeat problems |",
        "| --- | --- | ---: | --- | ---: | ---: |",
    ]
    for name, a in result["arms"].items():
        load = a["load"] or {}
        lines.append(f"| {name} | `{a['experiment_id']}` | {_f(load.get('max_loadavg_1m'), 2)} "
                     f"| {load.get('contaminated')} | {a['coverage_problems']} | "
                     f"{a['internal_problems']} |")  # fmt: skip
    lines += [
        "",
        "| Comparison | lane | role | baseline → candidate | verdict | disposition | reason |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in result["comparisons"]:
        lines.append(f"| {c['id']} | {c['lane']} | {c['role']} | {c['baseline']} → "
                     f"{c['candidate']} | **{c['verdict']}** | {c['disposition']} | "
                     f"{'; '.join(c['reasons'])} |")  # fmt: skip
    for c in result["comparisons"]:
        comp = c["comparison"]
        if comp is None:
            continue
        lines += ["", f"## {c['id']} ({c['baseline']} → {c['candidate']}, {c['lane']})", "",
                  f"- Semantic mismatches {comp['semantic_mismatches']['count']}; coverage "
                  f"problems {comp['coverage_problems']['count']}; order/cold/repeat "
                  f"{comp['internal_checks']['count']}; budget-bound established "
                  f"{comp['bounded_exactness']['established']}/"
                  f"{comp['bounded_exactness']['required']}; work differences "
                  f"{comp['work_differences']['count']}; status regressions "
                  f"{comp['status_regressions']['count']}",
                  "",
                  "| Bundle algorithm | wall impr. | CPU impr. | cases | threshold | verdict | "
                  "cold charged impr. | charged verdict |",
                  "| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |"]  # fmt: skip
        for key, t in comp["timing"].items():
            ch = comp["charged"].get(key) or {}
            lines.append(f"| {key} | {_f(t['improvement']['wall'])} | "
                         f"{_f(t['improvement']['cpu'])} | {t['decision_cases']['wall']} | "
                         f"{_f(t['threshold'])} | {t['verdict']} | {_f(ch.get('improvement'))} "
                         f"| {ch.get('verdict', 'N/A')} |")  # fmt: skip
        if c["lane"] == "heuristic":
            lines += ["", "| Bundle algorithm | split | cases | N/A | losses | gains | "
                      "max regret bps | mean regret bps |",
                      "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]  # fmt: skip
            for key, by_split in comp["regret"].items():
                for split, r in sorted(by_split.items()):
                    lines.append(f"| {key} | {split} | {r['cases']} | {r['not_applicable']} | "
                                 f"{r['losses']} | {r['gains']} | {_f(r['max_regret_bps'], 3)} "
                                 f"| {_f(r['mean_regret_bps'], 3)} |")  # fmt: skip
    lines += ["", f"_{result['claims']}_"]
    return "\n".join(lines) + "\n"


def _write(text: str, path: str | None) -> None:
    if path:
        Path(path).write_text(text, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m report.latency")
    sub = parser.add_subparsers(dest="command", required=True)
    s_p = sub.add_parser("summarize")
    s_p.add_argument("experiment")
    c_p = sub.add_parser("compare")
    c_p.add_argument("baseline")
    c_p.add_argument("candidate")
    c_p.add_argument("--lane", required=True, choices=("exact", "heuristic"))
    c_p.add_argument("--pair", action="append", default=[], metavar="CANDIDATE=REFERENCE")
    c_p.add_argument(
        "--sufficient",
        nargs=2,
        metavar=("BASELINE_SB", "CANDIDATE_SB"),
        help="sufficient-budget evidence of each experiment (exact lane)",
    )
    sb_p = sub.add_parser("sufficient")
    sb_p.add_argument("evidence")
    sb_p.add_argument("--against", help="experiment whose fixed-budget records to show")
    f_p = sub.add_parser("final", help="Every pre-registered comparison of an L08 session")
    f_p.add_argument("session")
    f_p.add_argument("--records", help="write per-case records as JSON lines here")
    for p in (s_p, c_p, sb_p, f_p):
        p.add_argument("--json")
        p.add_argument("--markdown")
    args = parser.parse_args(argv)
    try:
        if args.command == "final":
            result, records = final_report(args.session)
            text = render_final(result)
            _write("".join(json.dumps(r, sort_keys=True) + "\n" for r in records), args.records)
        elif args.command == "summarize":
            result = summarize(args.experiment)
            text = render_summary(result)
        elif args.command == "sufficient":
            against = load_experiment(args.against) if args.against else None
            result = summarize_sufficient(load_sufficient(args.evidence), against)
            text = render_sufficient(result)
        else:
            pairs = dict(item.split("=", 1) for item in args.pair) or None
            sufficient = (args.sufficient[0], args.sufficient[1]) if args.sufficient else None
            result = compare(args.baseline, args.candidate, lane=args.lane, pairs=pairs,
                             sufficient=sufficient)  # fmt: skip
            text = render_comparison(result)
    except LatencyReportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _write(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", args.json)
    _write(text, args.markdown)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

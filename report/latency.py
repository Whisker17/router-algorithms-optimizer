"""Offline summaries and paired comparisons of L01 latency experiments (WHI-1503).

    uv run python -m report.latency summarize EXPERIMENT_DIR [--json F] [--markdown F]
    uv run python -m report.latency compare BASELINE_DIR CANDIDATE_DIR --lane exact|heuristic
        [--pair candidate_algorithm=reference_algorithm ...] [--json F] [--markdown F]

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
  difference on a budget-bound record (limit hit, timeout or declared budget truncation) is
  a fixed-budget completion difference, reported separately and inconclusive until a
  sufficient-budget comparison establishes exactness;
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

from benchmark.latency import STAGES, source_identity
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
    return {
        "quotes_counted": (record.get("quotes") or {}).get("counted"),
        "candidates_considered": record.get("candidates_considered"),
        "candidates_truncated": record.get("candidates_truncated"),
        "search": record.get("search"),
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
        for r in run.records:
            if r["status"] == "cancelled":
                problems.append(f"{where}: {r['algorithm']}/{r['case_id']}: cancelled")
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
    fixed_budget_differences: int,
    evidence_clean: bool,
    contaminated: bool,
    timing: Mapping[str, Mapping[str, Any]],
    charged: Mapping[str, Mapping[str, Any]],
) -> tuple[str, list[str]]:
    """The pre-registered verdict from already-computed facts, in order. `timing` maps
    "<label> <algorithm>" to a dict with `verdict` in {faster, slower, no_worthwhile_change,
    insufficient_cases, lost_samples}; `charged` to one in {slower, not_slower,
    insufficient_cases}."""
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
    if lane == "exact" and fixed_budget_differences:
        return "inconclusive", [
            f"{fixed_budget_differences} fixed-budget completion difference(s): exactness "
            "needs a sufficient-budget comparison (quote cap raised on those cases)"
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
    moved = sorted(k for k, t in charged.items() if t["verdict"] == "slower")
    if moved:
        return "reject", ["cold charged time slower (cost moved out of the solve): "
                          + ", ".join(moved)]  # fmt: skip
    faster = sorted(k for k, t in timing.items() if t["verdict"] == "faster")
    if not faster:
        return "reject", ["no algorithm reached the minimum worthwhile improvement"]
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
    for (alg, case), parts in b_parts.items():
        b_charged = parts["charged_seconds"]
        c_charged = (c_parts.get((c_alg, case)) or {}).get("charged_seconds")
        if (alg == b_alg and split_of.get(case) == acc["decision_split"]
                and b_charged is not None and c_charged is not None
                and b_charged >= acc["min_timed_solve_seconds"]):  # fmt: skip
            ratios.append(c_charged / b_charged)
    g = _geomean(ratios)
    improvement = None if g is None else 1 - g
    verdict = (
        "insufficient_cases"
        if improvement is None
        else "slower"
        if improvement <= -threshold
        else "not_slower"
    )
    return {"improvement": improvement, "decision_cases": len(ratios), "threshold": threshold,
            "verdict": verdict}  # fmt: skip


def compare_experiments(
    base: Experiment,
    cand: Experiment,
    *,
    lane: str,
    pairs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if lane not in ("exact", "heuristic"):
        raise LatencyReportError(f"unknown lane {lane!r}")
    if base.document["protocol"]["sha256"] != cand.document["protocol"]["sha256"]:
        raise LatencyReportError("the experiments used different protocol documents")
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
                    if b_view != c_view:
                        line = f"{where}: differs in {_differing(b_view, c_view)}"
                        bound = budget_bound(b_r) or budget_bound(c_r)
                        (fixed_budget if bound else semantic).append(line)
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
    verdict, reasons = judge(
        lane=lane,
        candidate_internal=sum(len(v) for v in internal["candidate"].values()),
        semantic_mismatches=len(semantic),
        coverage_problems=n_coverage,
        baseline_internal=sum(len(v) for v in internal["baseline"].values()),
        fixed_budget_differences=len(fixed_budget),
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
        "protocol_sha256": base.document["protocol"]["sha256"],
        "acceptance": acc,
        "coverage_problems": coverage,
        "internal_checks": internal,
        "semantic_mismatches": semantic,
        "fixed_budget_differences": fixed_budget,
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


def compare(
    baseline: str | Path,
    candidate: str | Path,
    *,
    lane: str,
    pairs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return compare_experiments(
        load_experiment(baseline), load_experiment(candidate), lane=lane, pairs=pairs
    )


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
        *_problems("Fixed-budget completion differences", result["fixed_budget_differences"]),
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
    for p in (s_p, c_p):
        p.add_argument("--json")
        p.add_argument("--markdown")
    args = parser.parse_args(argv)
    try:
        if args.command == "summarize":
            result = summarize(args.experiment)
            text = render_summary(result)
        else:
            pairs = dict(item.split("=", 1) for item in args.pair) or None
            result = compare(args.baseline, args.candidate, lane=args.lane, pairs=pairs)
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

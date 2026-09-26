"""Offline summaries and paired comparisons of L01 latency experiments (WHI-1503).

    uv run python -m report.latency summarize EXPERIMENT_DIR [--json F] [--markdown F]
    uv run python -m report.latency compare BASELINE_DIR CANDIDATE_DIR --lane exact|heuristic
        [--pair candidate_algorithm=reference_algorithm ...] [--json F] [--markdown F]

Everything is read from saved, checksum-verified run records (`benchmark.results`); nothing
re-runs a solver. The rules applied by `compare` are the protocol's pre-registered
`acceptance` section (config/latency/l01.yaml, docs/references/latency-baseline.md):

- per (bundle, algorithm, case): the median of the measured solve samples pooled over both
  schedule orders; wall and process CPU separately;
- decisions on the protocol's decision split only (held-out), and only for cases whose
  baseline median reaches `min_timed_solve_seconds`;
- improvement = 1 - geometric mean of candidate/baseline per-case medians; it must reach
  both the minimum worthwhile improvement and `noise_multiplier` x the larger A/A noise
  floor (exp|ln geomean(reverse/fixed)| - 1) of the two experiments, for wall AND CPU;
- exact lane: every scheduled record's semantic fields identical in every order and
  cohort; work counters may differ and are listed;
- heuristic lane: paired regret against the same-scope reference, N/A where a score is
  unknown; there is no default loss tolerance, so the verdict is at most "opt-in".

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
from pathlib import Path
from typing import Any

from benchmark.results import RunManifest, load_case_records, load_manifest, load_memory_records
from benchmark.runner import compare_runs

SUMMARY_SCHEMA = "latency-summary/1"
COMPARISON_SCHEMA = "latency-comparison/1"
KILLED = {"timeout", "cancelled"}  # how far a killed search got is timing-dependent
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
    """A/A noise: geometric mean over decision-split cases (baseline median >= floor) of
    the reverse-order / fixed-order per-case median ratio; noise = exp|ln g| - 1."""
    fixed, reverse = (
        exp.runs.get(("timing", "fixed", label)),
        exp.runs.get(("timing", "reverse", label)),
    )
    if fixed is None or reverse is None:
        return {"cases": 0, "geomean_ratio": None, "noise": None}
    acc, split_of = exp.acceptance, exp.split_of
    a, b = _order_medians(fixed, kind), _order_medians(reverse, kind)
    ratios = [
        b[k] / a[k]
        for k in a.keys() & b.keys()
        if k[0] == algorithm
        and split_of.get(k[1], acc["decision_split"]) == acc["decision_split"]
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
    algorithms = list(runs[0].manifest.algorithms)
    block: dict[str, Any] = {}
    for algorithm in algorithms:
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
            "startup_seconds": _spread(e["startup_seconds"] for e in events),
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


def _cold_block(run: RunView) -> dict[str, Any]:
    events = {e["index"]: e for e in run.manifest.prepare_events}
    out: dict[str, Any] = {}
    for record in run.records:
        m = record["measurement"]
        event = events.get(m.get("prepare_event"), {})
        entry = out.setdefault(record["algorithm"], defaultdict(list))
        parts = {
            "startup_seconds": event.get("startup_seconds"),
            "prepare_seconds": event.get("prepare_seconds"),
            "solve_wall_seconds": (m.get("solve_seconds") or [None])[0],
            "solve_cpu_seconds": (m.get("solve_cpu_seconds") or [None])[0],
            "transport_seconds": (m.get("transport_seconds") or [None])[0],
            "evaluation_seconds": m.get("evaluation_seconds"),
        }
        for name, value in parts.items():
            if value is not None:
                entry[name].append(value)
        if all(parts[n] is not None for n in parts if n != "solve_cpu_seconds"):
            entry["charged_seconds"].append(
                sum(v for n, v in parts.items() if n != "solve_cpu_seconds" and v is not None)
            )
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
            "memory_records": sum(1 for r in run.memory if r["algorithm"] == algorithm),
        }
        for algorithm, entry in out.items()
    }


def _quality_block(run: RunView) -> dict[str, Any]:
    """Per case: the same-scope best known score among the recorded algorithms and each
    algorithm's regret against it (bps); N/A (None) where a score is unknown."""
    by_case: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for record in run.records:
        by_case[record["case_id"]][record["algorithm"]] = record
    cases: dict[str, Any] = {}
    per_algorithm: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"at_best_known": 0, "regret_bps": [], "not_applicable": 0}
    )
    for case_id, records in sorted(by_case.items()):
        scores = {a: _score(r) for a, r in records.items() if r["status"] == "ok"}
        known = [s for s in scores.values() if s is not None]
        best = max(known) if known else None
        row: dict[str, Any] = {"best_known_score": None if best is None else str(best)}
        for algorithm, record in records.items():
            score = scores.get(algorithm)
            regret = (best - score) * 10_000 / best if best and score is not None else None
            row[algorithm] = {
                "status": record["status"],
                "score": record.get("score"),
                "regret_bps": regret,
                "quotes_counted": (record.get("quotes") or {}).get("counted"),
                "candidates_considered": record.get("candidates_considered"),
                "candidates_truncated": record.get("candidates_truncated"),
                "truncated_by": (record.get("search") or {}).get("truncated_by"),
            }
            stats = per_algorithm[algorithm]
            if regret is None:
                stats["not_applicable"] += 1
            else:
                stats["regret_bps"].append(regret)
                stats["at_best_known"] += regret == 0
        cases[case_id] = row
    return {
        "cases": cases,
        "per_algorithm": {
            a: {
                "at_best_known": s["at_best_known"],
                "not_applicable": s["not_applicable"],
                "max_regret_bps": max(s["regret_bps"], default=None),
                "mean_regret_bps": statistics.fmean(s["regret_bps"]) if s["regret_bps"] else None,
            }
            for a, s in per_algorithm.items()
        },
    }


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


def summarize(path: str | Path) -> dict[str, Any]:
    exp = load_experiment(path)
    doc = exp.document
    semantic: dict[str, Any] = {}
    for label in exp.labels("timing"):
        fixed = exp.runs.get(("timing", "fixed", label))
        reverse = exp.runs.get(("timing", "reverse", label))
        cold = exp.runs.get(("cold", "fixed", label))
        runs = [r for r in (fixed, reverse, cold) if r is not None]
        semantic[label] = {
            "status_counts": {
                algorithm: dict(
                    Counter(r["status"] for r in runs[0].records if r["algorithm"] == algorithm)
                )
                for algorithm in runs[0].manifest.algorithms
            },
            "reverse_vs_fixed_mismatches": (
                compare_runs(fixed.run_dir, reverse.run_dir) if fixed and reverse else None
            ),
            "cold_vs_warm_mismatches": (
                compare_runs(fixed.run_dir, cold.run_dir) if fixed and cold else None
            ),
            "attempts_inconsistent": sorted(
                f"{r['algorithm']}/{r['case_id']}"
                for run in runs
                for r in run.records
                if r["measurement"].get("attempts_consistent") is False
            ),
            "scheduled": {run.entry["run_id"]: run.manifest.scheduled_count for run in runs},
        }
    first = next(iter(exp.runs.values()))
    algorithms = list(first.manifest.algorithms)
    return {
        "schema": SUMMARY_SCHEMA,
        "experiment": {
            "experiment_id": doc["experiment_id"],
            "created_at": doc["created_at"],
            "finished_at": doc["finished_at"],
            "partial": doc["partial"],
            "replay_command": doc["replay_command"],
            "protocol": {k: doc["protocol"][k] for k in ("path", "sha256", "key", "version")},
            "source": doc["source"],
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
        "semantic": semantic,
        "timing": {lb: _timing_block(exp, lb) for lb in exp.labels("timing") if "matrix" in lb},
        "sentinel": {
            lb: _sentinel_block(exp, lb) for lb in exp.labels("timing") if "sentinel" in lb
        },
        "cold": {lb: _cold_block(exp.runs[("cold", "fixed", lb)]) for lb in exp.labels("cold")},
        "quality": {
            lb: _quality_block(exp.runs[("timing", "fixed", lb)])
            for lb in exp.labels("timing")
            if ("timing", "fixed", lb) in exp.runs
        },
        "quote_cli": _quote_cli_block(exp, algorithms),
        "claims": NO_TAIL_CLAIM,
    }


# ------------------------------------------------------------------ comparison


def judge(
    *,
    lane: str,
    semantic_mismatches: int,
    timing: Mapping[str, Mapping[str, Any]],
    evidence_clean: bool,
    contaminated: bool,
) -> tuple[str, list[str]]:
    """The pre-registered verdict from already-computed facts. `timing` maps
    "<label> <algorithm>" to a dict with `verdict` in {faster, slower, no_worthwhile_change,
    insufficient_cases}."""
    reasons: list[str] = []
    faster = sorted(k for k, t in timing.items() if t["verdict"] == "faster")
    slower = sorted(k for k, t in timing.items() if t["verdict"] == "slower")
    if lane == "exact" and semantic_mismatches:
        return "reject", [f"{semantic_mismatches} semantic mismatch(es): not an exact variant"]
    if not evidence_clean:
        return "inconclusive", ["an experiment is partial, or ran from a dirty source tree"]
    if contaminated:
        return "inconclusive", ["host load exceeded the protocol threshold in an experiment"]
    if slower:
        reasons.append("slower: " + ", ".join(slower))
        return "reject", reasons
    if not faster:
        return "reject", ["no algorithm reached the minimum worthwhile improvement"]
    reasons.append("faster: " + ", ".join(faster))
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


def compare(
    baseline: str | Path,
    candidate: str | Path,
    *,
    lane: str,
    pairs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if lane not in ("exact", "heuristic"):
        raise LatencyReportError(f"unknown lane {lane!r}")
    base, cand = load_experiment(baseline), load_experiment(candidate)
    if base.document["protocol"]["sha256"] != cand.document["protocol"]["sha256"]:
        raise LatencyReportError("the experiments used different protocol documents")
    base_bundles = {k: v["bundle_hash"] for k, v in base.document["bundles"].items()}
    cand_bundles = {k: v["bundle_hash"] for k, v in cand.document["bundles"].items()}
    if base_bundles != cand_bundles:
        raise LatencyReportError("the experiments measured different derived bundles")
    acc = base.acceptance
    fields = list(acc["exact_semantic_fields"])
    base_algorithms = list(next(iter(base.runs.values())).manifest.algorithms)
    cand_algorithms = list(next(iter(cand.runs.values())).manifest.algorithms)
    mapping = dict(pairs or {a: a for a in base_algorithms if a in cand_algorithms})
    semantic: list[str] = []
    work: list[str] = []
    regret: dict[str, Any] = {}
    timing: dict[str, Any] = {}
    for label in base.labels("timing"):
        for order in ("fixed", "reverse"):
            b_run, c_run = (
                base.runs.get(("timing", order, label)),
                cand.runs.get(("timing", order, label)),
            )
            if b_run is None or c_run is None:
                continue
            b_rec = {_key(r): r for r in b_run.records}
            c_rec = {_key(r): r for r in c_run.records}
            for c_alg, b_alg in mapping.items():
                for (alg, case_id), c_r in sorted(c_rec.items()):
                    if alg != c_alg:
                        continue
                    b_r = b_rec.get((b_alg, case_id))
                    where = f"{label} {order} {c_alg}/{case_id}"
                    if b_r is None:
                        semantic.append(f"{where}: no baseline record for {b_alg}")
                        continue
                    if semantic_view(b_r, fields) != semantic_view(c_r, fields):
                        semantic.append(f"{where}: semantic fields differ")
                    if work_view(b_r) != work_view(c_r):
                        work.append(f"{where}: work counters differ")
                    if order == "fixed":
                        b_s, c_s = _score(b_r), _score(c_r)
                        entry = regret.setdefault(
                            f"{label} {c_alg}",
                            {"regret_bps": [], "not_applicable": 0, "transitions": Counter()},
                        )
                        entry["transitions"][f"{b_r['status']}->{c_r['status']}"] += 1
                        if b_s and b_s > 0 and c_s is not None:
                            entry["regret_bps"].append((b_s - c_s) * 10_000 / b_s)
                        else:
                            entry["not_applicable"] += 1
        if "matrix" not in label:
            continue
        medians = {
            (who, kind): _pooled_medians(exp, label, kind)
            for who, exp in (("base", base), ("cand", cand))
            for kind in ("wall", "cpu")
        }
        split_of = base.split_of
        for c_alg, b_alg in mapping.items():
            improvements: dict[str, float | None] = {}
            cases: dict[str, int] = {}
            for kind in ("wall", "cpu"):
                b_med, c_med = medians[("base", kind)], medians[("cand", kind)]
                ratios = [
                    c_med[(c_alg, case)] / value
                    for (alg, case), value in b_med.items()
                    if alg == b_alg
                    and split_of.get(case) == acc["decision_split"]
                    and medians[("base", "wall")].get((alg, case), 0.0)
                    >= acc["min_timed_solve_seconds"]
                    and (c_alg, case) in c_med
                ]
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
                "noise": noise,
                "threshold": threshold,
                "verdict": _timing_verdict(improvements, threshold),
            }
    charged = {}
    for label in base.labels("cold"):
        b_cold = _cold_block(base.runs[("cold", "fixed", label)])
        c_cold = _cold_block(cand.runs[("cold", "fixed", label)])
        for c_alg, b_alg in mapping.items():
            charged[f"{label} {c_alg}"] = {
                name: {
                    "baseline_median": b_cold.get(b_alg, {}).get(name, {}).get("median"),
                    "candidate_median": c_cold.get(c_alg, {}).get(name, {}).get("median"),
                }
                for name in (
                    "prepare_seconds",
                    "charged_seconds",
                    "solve_peak_bytes",
                    "prepare_peak_bytes",
                )  # fmt: skip
            }
    clean = all(
        not e.document["partial"] and e.document["source"]["git_dirty"] is False
        for e in (base, cand)
    )
    contaminated = any((e.document["load"] or {}).get("contaminated") for e in (base, cand))
    verdict, reasons = judge(
        lane=lane,
        semantic_mismatches=len(semantic),
        timing=timing,
        evidence_clean=clean,
        contaminated=contaminated,
    )
    return {
        "schema": COMPARISON_SCHEMA,
        "lane": lane,
        "baseline": {"experiment_id": base.document["experiment_id"],
                     "source": base.document["source"], "load": base.document["load"]},
        "candidate": {"experiment_id": cand.document["experiment_id"],
                      "source": cand.document["source"], "load": cand.document["load"]},
        "pairs": mapping,
        "protocol_sha256": base.document["protocol"]["sha256"],
        "acceptance": acc,
        "semantic_mismatches": semantic,
        "work_differences": work,
        "regret": {
            k: {
                "cases": len(v["regret_bps"]),
                "not_applicable": v["not_applicable"],
                "max_regret_bps": max(v["regret_bps"], default=None),
                "mean_regret_bps": statistics.fmean(v["regret_bps"]) if v["regret_bps"] else None,
                "losses": sum(1 for x in v["regret_bps"] if x > 0),
                "gains": sum(1 for x in v["regret_bps"] if x < 0),
                "status_transitions": dict(v["transitions"]),
            }
            for k, v in regret.items()
        },
        "timing": timing,
        "charged_costs": charged,
        "verdict": verdict,
        "reasons": reasons,
        "claims": NO_TAIL_CLAIM,
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


def render_summary(summary: Mapping[str, Any]) -> str:
    e, load = summary["experiment"], summary["load"] or {}
    src = e["source"]
    lines = [
        f"# L01 latency baseline — experiment `{e['experiment_id']}`",
        "",
        f"- Protocol: `{e['protocol']['path']}` {e['protocol']['key']} v{e['protocol']['version']}"
        f" (sha256 `{e['protocol']['sha256']}`)",
        f"- Source: `{src['git_revision']}` dirty={src['git_dirty']}"
        + (f" patch `{src['dirty_patch_sha256']}`" if src["git_dirty"] else ""),
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
        "## Statuses and deterministic checks",
        "",
        "| Bundle | Algorithm | Statuses | reverse≡fixed | cold≡warm |",
        "| --- | --- | --- | --- | --- |",
    ]
    for label, block in summary["semantic"].items():
        rev, cold = block["reverse_vs_fixed_mismatches"], block["cold_vs_warm_mismatches"]
        for algorithm, counts in block["status_counts"].items():
            status = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            lines.append(
                f"| {label} | {algorithm} | {status} | {_agree(rev, algorithm)} | "
                f"{_agree(cold, algorithm)} |"
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
        "| Bundle | Algorithm | startup median | prepare median | cold solve median | "
        "evaluation median | charged median | solve peak max (MiB) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, block in summary["cold"].items():
        for algorithm, c in block.items():
            peak = (c.get("solve_peak_bytes") or {}).get("max")
            lines.append(
                f"| {label} | {algorithm} | {_f(c['startup_seconds']['median'])} | "
                f"{_f(c['prepare_seconds']['median'], 4)} | "
                f"{_f((c.get('solve_wall_seconds') or {}).get('median'))} | "
                f"{_f((c.get('evaluation_seconds') or {}).get('median'), 4)} | "
                f"{_f((c.get('charged_seconds') or {}).get('median'))} | "
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
        "## Quality against the same-scope best known (fixed-order records)",
        "",
        "| Bundle | Algorithm | at best known | N/A | max regret bps | mean regret bps |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for label, block in summary["quality"].items():
        if "matrix" not in label:
            continue
        for algorithm, s in block["per_algorithm"].items():
            lines.append(
                f"| {label} | {algorithm} | {s['at_best_known']} | {s['not_applicable']} | "
                f"{_f(s['max_regret_bps'], 2)} | {_f(s['mean_regret_bps'], 2)} |"
            )
    return "\n".join(lines) + "\n"


def render_comparison(result: Mapping[str, Any]) -> str:
    lines = [
        f"# L01 comparison ({result['lane']} lane): **{result['verdict']}**",
        "",
        *[f"- {r}" for r in result["reasons"]],
        f"- Baseline `{result['baseline']['experiment_id']}` "
        f"(`{result['baseline']['source']['git_revision']}`), candidate "
        f"`{result['candidate']['experiment_id']}` "
        f"(`{result['candidate']['source']['git_revision']}`)",
        f"- Semantic mismatches: {len(result['semantic_mismatches'])}; work-counter "
        f"differences: {len(result['work_differences'])}",
        "",
        "| Bundle algorithm | reference | wall improvement | CPU improvement | cases | "
        "threshold | verdict |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for key, t in result["timing"].items():
        lines.append(
            f"| {key} | {t['reference']} | {_f(t['improvement']['wall'])} | "
            f"{_f(t['improvement']['cpu'])} | {t['decision_cases']['wall']} | "
            f"{_f(t['threshold'])} | {t['verdict']} |"
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

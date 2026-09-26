"""L01 latency / quality experiment driver (WHI-1503; docs/references/latency-baseline.md).

    uv run python -m benchmark.latency run --protocol config/latency/l01.yaml \\
        --bundle data/corpus/mantle-5src-101082044/bundle --out data/latency

One experiment directory holds everything a later comparison needs:

    experiment.json   protocol (document + sha256), source identity (git revision, dirty
                      patch identity, committed code-tree ids), environment, parent bundle
                      and profile identity, derived bundles, stages, quote-CLI runs, load
    load.jsonl        1-minute load-average samples (before/after every stage and case)
    bundles/          derived matrix and sentinel bundles per cohort (deterministic)
    runs/<stage>/     ordinary schema-2 run directories (manifest.json + cases.jsonl
                      [+ memory.jsonl]), readable by `benchmark.results` / `compare_runs`
    quote_cli/        the sentinel's `main.py quote` invocations and their saved runs

Stages, strictly sequential (`timing` -> `cold` -> `quote_cli`):

- **timing** (headline, uninstrumented): every cohort's matrix and sentinel bundle, once
  per protocol order; `fixed` walks bundles and the algorithm x case schedule forward,
  `reverse` walks both backwards. One warm worker per algorithm per run; per case the
  protocol's warmup attempts are recorded but excluded, then its measured repeats.
- **cold**: a fresh spawned worker per (algorithm, case) and one attempt -- start-up and
  prepare charged every time -- followed in the same run by the separate tracemalloc pass.
- **quote_cli**: `python main.py quote` for the sentinel as separate processes. The quote
  command itself is untouched: one solve per algorithm per invocation.

The measured loop reuses `benchmark.runner`'s worker slot, per-case measurement,
independent evaluation, failure mapping and memory pass unchanged. It only observes each
attempt as well, to record what the ordinary runner discards: warmup samples, the solve's
process CPU time (`benchmark.worker`) and the parent-observed transport overhead
(request -> answer minus the child's solve time: request/result pickling and transfer,
the child's per-attempt context/meter setup; candidate-sink sends happen inside the solve
window). Solve time excludes worker start-up (process start -> ready, which includes
prepare; prepare is also reported alone), IPC and the parent's independent final
evaluation, each recorded separately.

`python -m benchmark.latency sufficient --sufficient config/latency/l01-sufficient-budget.yaml
--bundle ... --out ...` re-solves only the listed budget-bound records on the same derived
matrix bundles and pinned profile with the budget raised (the sufficient-budget evidence
exact comparisons need; docs/references/latency-baseline.md §7).

SIGTERM is handled like Ctrl-C: the current run's unfinished cases are recorded as
`cancelled`, its manifest and the experiment are finalized as `interrupted` (never
`complete`), and the command exits 130.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmark.profile import ProfileError, RunProfile, _parse_budget, load_profile
from benchmark.results import (
    REPO_ROOT,
    STATE_COMPLETE,
    STATE_INTERRUPTED,
    CaseRecord,
    RunManifest,
    RunWriter,
    environment_record,
    git_provenance,
    load_case_records,
    load_manifest,
    new_run_id,
)
from benchmark.runner import (
    NS_PER_SECOND,
    _cancelled_record,
    _measure_case,
    _measure_memory,
    _PrepareFailure,
    _Run,
    _WorkerSlot,
)
from benchmark.worker import AttemptOutcome, SolveRequest, Worker
from routing.algorithms.base import Budget
from routing.algorithms.registry import get_algorithm
from snapshot.bundle import load_bundle, sha256_bytes, sha256_file
from snapshot.corpus import subset_corpus_bundle
from snapshot.models import SnapshotBundle
from snapshot.request import derive_request_bundle, parse_amount, request_case, resolve_token

PROTOCOL_SCHEMA = "latency-protocol/1"
EXPERIMENT_SCHEMA = "latency-experiment/1"
EXPERIMENT_FILE = "experiment.json"
LOAD_FILE = "load.jsonl"
COHORTS = ("full_source", "sor_compatible")
ORDERS = ("fixed", "reverse")
STAGES = ("timing", "cold", "quote_cli")
SPLITS = {"tuning": "tuning", "held_out": "report"}  # protocol split -> corpus split
# Committed paths whose content defines what was measured (ids from `git rev-parse HEAD:p`).
CODE_PATHS = ("benchmark", "pools", "routing", "snapshot", "report", "config", "main.py",
              "pyproject.toml", "uv.lock")  # fmt: skip


class LatencyError(ValueError):
    """The protocol, inputs or source tree cannot support a protocol experiment."""


# ------------------------------------------------------------------ protocol


@dataclass(frozen=True)
class MatrixCase:
    case_id: str
    split: str  # "tuning" | "held_out"
    covers: tuple[str, ...]


@dataclass(frozen=True)
class Protocol:
    path: str
    sha256: str
    document: dict[str, Any]
    key: str
    version: int
    parent_bundle_id: str
    parent_bundle_hash: str
    profile_path: str
    profile_sha256: str
    sentinel: dict[str, str]
    matrix: tuple[MatrixCase, ...]
    cohorts: tuple[str, ...]
    warmup: int
    repeats: int
    orders: tuple[str, ...]
    cold_cohorts: tuple[str, ...]
    quote_cli_invocations: int
    max_loadavg_1m_per_cpu: float
    acceptance: dict[str, Any]


def _keys(obj: Any, keys: set[str], where: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise LatencyError(f"{where}: expected a mapping, got {type(obj).__name__}")
    if obj.keys() != keys:
        raise LatencyError(
            f"{where}: missing {sorted(keys - obj.keys())}, unknown {sorted(obj.keys() - keys)}"
        )
    return obj


def _int(value: Any, minimum: int, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise LatencyError(f"{where}: expected an integer >= {minimum}, got {value!r}")
    return value


def _subset(value: Any, allowed: tuple[str, ...], where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(set(value)) != len(value):
        raise LatencyError(f"{where}: expected a non-empty list without duplicates")
    if not set(value) <= set(allowed):
        raise LatencyError(f"{where}: {value!r} not within {list(allowed)}")
    return tuple(value)


ACCEPTANCE_KEYS = {
    "decision_split", "min_timed_solve_seconds", "minimum_worthwhile_improvement",
    "noise_multiplier", "exact_semantic_fields", "work_fields",
    "heuristic_default_loss_tolerance",
}  # fmt: skip


def parse_protocol(raw: Any, *, path: str, sha256: str) -> Protocol:
    top = _keys(raw, {"schema", "key", "version", "parent_bundle", "profile", "sentinel",
                      "matrix", "cohorts", "timing", "cold", "quote_cli", "load",
                      "acceptance"}, "<root>")  # fmt: skip
    if top["schema"] != PROTOCOL_SCHEMA:
        raise LatencyError(f"schema: expected {PROTOCOL_SCHEMA!r}, got {top['schema']!r}")
    parent = _keys(top["parent_bundle"], {"bundle_id", "bundle_hash"}, "parent_bundle")
    profile = _keys(top["profile"], {"path", "sha256"}, "profile")
    sentinel = _keys(top["sentinel"], {"token_in", "token_out", "amount"}, "sentinel")
    if not isinstance(top["matrix"], list) or not top["matrix"]:
        raise LatencyError("matrix: expected a non-empty list")
    matrix: list[MatrixCase] = []
    for i, entry in enumerate(top["matrix"]):
        item = _keys(entry, {"case", "split", "covers"}, f"matrix[{i}]")
        if item["split"] not in SPLITS:
            raise LatencyError(f"matrix[{i}].split: expected one of {list(SPLITS)}")
        matrix.append(MatrixCase(str(item["case"]), item["split"], tuple(item["covers"])))
    if len({m.case_id for m in matrix}) != len(matrix):
        raise LatencyError("matrix: duplicate case")
    timing = _keys(top["timing"], {"warmup", "repeats", "orders"}, "timing")
    cold = _keys(top["cold"], {"cohorts"}, "cold")
    quote_cli = _keys(top["quote_cli"], {"invocations"}, "quote_cli")
    load = _keys(top["load"], {"max_loadavg_1m_per_cpu"}, "load")
    threshold = load["max_loadavg_1m_per_cpu"]
    if not isinstance(threshold, int | float) or isinstance(threshold, bool) or threshold <= 0:
        raise LatencyError("load.max_loadavg_1m_per_cpu: expected a positive number")
    acceptance = _keys(top["acceptance"], ACCEPTANCE_KEYS, "acceptance")
    if acceptance["decision_split"] not in SPLITS:
        raise LatencyError(f"acceptance.decision_split: expected one of {list(SPLITS)}")
    if acceptance["heuristic_default_loss_tolerance"] is not None:
        raise LatencyError(
            "acceptance.heuristic_default_loss_tolerance: no default quality-loss tolerance "
            "was supplied by the owner; it must stay null"
        )
    cohorts = _subset(top["cohorts"], COHORTS, "cohorts")
    return Protocol(
        path=path,
        sha256=sha256,
        document=raw,
        key=str(top["key"]),
        version=_int(top["version"], 1, "version"),
        parent_bundle_id=str(parent["bundle_id"]),
        parent_bundle_hash=str(parent["bundle_hash"]),
        profile_path=str(profile["path"]),
        profile_sha256=str(profile["sha256"]),
        sentinel={k: str(v) for k, v in sentinel.items()},
        matrix=tuple(matrix),
        cohorts=cohorts,
        warmup=_int(timing["warmup"], 0, "timing.warmup"),
        repeats=_int(timing["repeats"], 1, "timing.repeats"),
        orders=_subset(timing["orders"], ORDERS, "timing.orders"),
        cold_cohorts=_subset(cold["cohorts"], cohorts, "cold.cohorts"),
        quote_cli_invocations=_int(quote_cli["invocations"], 0, "quote_cli.invocations"),
        max_loadavg_1m_per_cpu=float(threshold),
        acceptance=acceptance,
    )


def load_protocol(path: str | Path) -> Protocol:
    import yaml

    path = Path(path)
    data = path.read_bytes()
    try:
        return parse_protocol(yaml.safe_load(data), path=str(path), sha256=sha256_bytes(data))
    except LatencyError as exc:
        raise LatencyError(f"{path}: {exc}") from exc


# ------------------------------------------------------------------ provenance


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(  # fixed local git subcommands, no shell/network
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
            check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def source_identity() -> dict[str, Any]:
    """Which source was measured. Clean tree: the revision plus the committed tree/blob id
    of every code path (a later docs/artifact-only commit keeps them, a code change does
    not). Dirty tree: the changed paths and a patch identity covering tracked diffs AND
    untracked file contents (`git diff HEAD` alone would miss new files)."""
    revision, dirty, diff_sha = git_provenance()
    identity: dict[str, Any] = {
        "git_revision": revision,
        "git_dirty": dirty,
        "git_diff_sha256": diff_sha,
        "code_tree_ids": {p: (_git("rev-parse", f"HEAD:{p}") or "").strip() or None
                          for p in CODE_PATHS} if revision else {},
        "dirty_paths": [],
        "dirty_patch_sha256": None,
    }  # fmt: skip
    if dirty:
        status = (_git("status", "--porcelain", "--untracked-files=all") or "").splitlines()
        digest = hashlib.sha256((_git("diff", "HEAD", "--binary") or "").encode())
        for line in sorted(status):
            if line.startswith("?? "):
                rel = line[3:]
                digest.update(f"\0{rel}\0{sha256_file(REPO_ROOT / rel)}".encode())
        identity["dirty_paths"] = sorted(status)
        identity["dirty_patch_sha256"] = digest.hexdigest()
    return identity


# ------------------------------------------------------------------ bundles


def build_bundles(
    protocol: Protocol, parent: SnapshotBundle, out: Path
) -> dict[str, SnapshotBundle]:
    """`{"<cohort>/matrix": ..., "<cohort>/sentinel": ...}`, all written under `out`."""
    if parent.bundle_hash != protocol.parent_bundle_hash:
        raise LatencyError(
            f"parent bundle hash {parent.bundle_hash} differs from the protocol's "
            f"{protocol.parent_bundle_hash}"
        )
    if parent.corpus is None:
        raise LatencyError("the parent bundle is not a corpus bundle")
    meta = parent.corpus["case_metadata"]
    for m in protocol.matrix:
        if m.case_id not in meta:
            raise LatencyError(f"matrix case {m.case_id!r} is not in the parent corpus")
        if meta[m.case_id]["split"] != SPLITS[m.split]:
            raise LatencyError(
                f"matrix case {m.case_id!r} is declared {m.split} but the corpus split is "
                f"{meta[m.case_id]['split']!r}"
            )
    case_ids = [m.case_id for m in protocol.matrix]
    bundles: dict[str, SnapshotBundle] = {}
    for cohort in protocol.cohorts:
        full = cohort == "full_source"
        matrix = subset_corpus_bundle(
            parent,
            parent.pools if full else parent.corpus["cohorts"]["sor_compatible"]["pools"],
            case_ids,
            out / f"{cohort}-matrix",
            id_suffix=f"{protocol.key.lower()}-{cohort}",
            cohort=None if full else cohort,
        )
        # The full-source sentinel is exactly `main.py quote`'s derived bundle (same bundle
        # hash); the matched one sees only the matched V2/V3 pool universe.
        source = parent if full else matrix
        s = protocol.sentinel
        tin = resolve_token(source, s["token_in"], "sentinel.token_in")
        tout = resolve_token(source, s["token_out"], "sentinel.token_out")
        case = request_case(source, tin, tout, parse_amount(s["amount"], tin))
        bundles[f"{cohort}/matrix"] = matrix
        bundles[f"{cohort}/sentinel"] = derive_request_bundle(
            source, case, out / f"{cohort}-sentinel"
        )
    return bundles


# ------------------------------------------------------------------ measured runs


def _seconds(ns: int | None) -> float | None:
    return None if ns is None else ns / NS_PER_SECOND


class _ObservedWorker:
    """Forwards to a real `Worker`, keeping every attempt outcome it returns."""

    def __init__(self, worker: Worker, sink: list[AttemptOutcome]) -> None:
        self._worker, self._sink = worker, sink
        self.worker_id = worker.worker_id

    def attempt(self, request: SolveRequest) -> AttemptOutcome:
        outcome = self._worker.attempt(request)
        self._sink.append(outcome)
        return outcome


class _ObservedSlot(_WorkerSlot):
    def __init__(self, run: _Run, factory: Any) -> None:
        super().__init__(run, factory, memory=False)
        self.outcomes: list[AttemptOutcome] = []

    def acquire(self) -> Any:
        acquired = super().acquire()
        if isinstance(acquired, _PrepareFailure):
            return acquired
        return _ObservedWorker(acquired, self.outcomes)


def _attempt_view(outcome: AttemptOutcome, phase: str) -> dict[str, Any]:
    transport = None if outcome.solve_ns is None else outcome.elapsed_ns - outcome.solve_ns
    return {
        "phase": phase,
        "kind": outcome.kind,
        "solve_wall_seconds": _seconds(outcome.solve_ns),
        "solve_cpu_seconds": _seconds(outcome.solve_cpu_ns),
        "elapsed_seconds": outcome.elapsed_ns / NS_PER_SECOND,
        "transport_seconds": _seconds(transport),
    }


def _observed_record(record: CaseRecord, outcomes: list[AttemptOutcome], warmup: int) -> CaseRecord:
    attempts = [
        _attempt_view(o, "warmup" if i < warmup else "measured") for i, o in enumerate(outcomes)
    ]
    measured = [a for a in attempts if a["phase"] == "measured" and a["kind"] == "returned"]
    return replace(
        record,
        measurement={
            **record.measurement,
            "attempts": attempts,
            "solve_cpu_seconds": [a["solve_cpu_seconds"] for a in measured],
            "transport_seconds": [a["transport_seconds"] for a in measured],
        },
    )


def measure_run(
    bundle: SnapshotBundle,
    profile: RunProfile,
    *,
    results_dir: Path,
    run_id: str,
    stage: str,
    order: str,
    warmup: int,
    repeats: int,
    scope: str,
    memory: bool,
    replay_command: str,
    environment: Mapping[str, Any] | None = None,
    on_record: Callable[[str, str], None] | None = None,
    pairs: frozenset[tuple[str, str]] | None = None,
) -> RunManifest:
    """One ordinary schema-2 run of `profile.algorithms` over `bundle` with the given
    schedule order, attempts and worker scope (see module docstring). `pairs`, if given,
    restricts the schedule to those (algorithm, case id) pairs."""
    if order not in ORDERS:
        raise LatencyError(f"unknown order {order!r}")
    profile = replace(
        profile,
        objective=profile.objective.bind(bundle),
        measurement=replace(
            profile.measurement, warmup=warmup, repeats=repeats, order="fixed",
            memory_pass=memory,
        ),
        worker=replace(profile.worker, scope=scope),  # type: ignore[arg-type]
    )  # fmt: skip
    factories = [get_algorithm(name) for name in profile.algorithms]
    schedule = [
        (factory, case)
        for factory in factories
        for case in bundle.cases
        if pairs is None or (factory.name, case.case_id) in pairs
    ]
    if order == "reverse":
        schedule.reverse()
    writer = RunWriter.create(
        results_dir,
        bundle=bundle,
        profile=profile,
        replay_command=replay_command,
        scheduled_count=len(schedule),
        measurement={
            **profile.measurement.to_dict(),
            "driver": "benchmark.latency",
            "stage": stage,
            "order": order,
            "order_semantics": "reverse = the whole algorithm x case schedule reversed",
            "worker_scope": scope,
            "schedule": [[f.name, c.case_id] for f, c in schedule],
            "budget": profile.budget.to_dict(),
        },
        environment=environment or environment_record(profile.worker.to_dict()),
        memory=memory,
        run_id=run_id,
    )
    run = _Run(bundle=bundle, profile=profile, writer=writer)
    slot: Any = None
    done = memory_done = 0
    started = time.perf_counter_ns()

    def _open(factory: Any, *, instrumented: bool) -> Any:
        nonlocal slot
        if slot is None or slot.factory is not factory:
            if slot is not None:
                slot.close()
            slot = _WorkerSlot(run, factory, memory=True) if instrumented else None
            slot = slot or _ObservedSlot(run, factory)
        return slot

    try:
        for index, (factory, case) in enumerate(schedule):
            current = _open(factory, instrumented=False)
            current.outcomes.clear()
            record = _measure_case(run, current, case, index)
            writer.append(_observed_record(record, current.outcomes, warmup))
            done += 1
            if on_record is not None:
                on_record(factory.name, case.case_id)
        if slot is not None:
            slot.close()
            slot = None
        if memory:
            for index, (factory, case) in enumerate(schedule):
                writer.append_memory(
                    _measure_memory(run, _open(factory, instrumented=True), case, index)
                )
                memory_done += 1
            if slot is not None:
                slot.close()
                slot = None
    except BaseException:
        if slot is not None:
            slot.kill()
        for index in range(done, len(schedule)):
            factory, case = schedule[index]
            writer.append(_cancelled_record(case, factory.name, index))
        if memory:
            for index in range(memory_done, len(schedule)):
                factory, case = schedule[index]
                writer.append_memory({"pass": "memory", "schedule_index": index,
                                      "case_id": case.case_id, "algorithm": factory.name,
                                      "status": "cancelled"})  # fmt: skip
        writer.finalize(state=STATE_INTERRUPTED, timing=_timing(run, started))
        raise
    return writer.finalize(state=STATE_COMPLETE, timing=_timing(run, started))


def _timing(run: _Run, started: int) -> dict[str, Any]:
    return {
        "clock": "perf_counter_ns",
        "total_seconds": (time.perf_counter_ns() - started) / NS_PER_SECOND,
        "per_algorithm": {n: t.to_dict() for n, t in sorted(run.totals.items())},
    }


# ------------------------------------------------------------------ experiment


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_json(path: Path, obj: Mapping[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class _LoadLog:
    def __init__(self, path: Path, threshold: float) -> None:
        self.path, self.threshold = path, threshold
        self.max_1m = 0.0
        self.samples = 0

    def sample(self, **context: Any) -> tuple[float, float, float]:
        load = os.getloadavg()
        self.max_1m = max(self.max_1m, load[0])
        self.samples += 1
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": _now(), "loadavg": list(load), **context}) + "\n")
        return load

    def summary(self) -> dict[str, Any]:
        cpus = os.cpu_count() or 1
        limit = self.threshold * cpus
        return {
            "samples": self.samples,
            "max_loadavg_1m": self.max_1m,
            "cpu_count": cpus,
            "threshold_loadavg_1m": limit,
            "contaminated": self.max_1m > limit,
        }


def _replay(protocol: Protocol, bundle_path: str, out: Path, stages: Sequence[str]) -> str:
    cmd = ["uv", "run", "python", "-m", "benchmark.latency", "run", "--protocol",
           protocol.path, "--bundle", bundle_path, "--out", str(out)]  # fmt: skip
    if tuple(stages) != STAGES:
        cmd += ["--stages", ",".join(stages)]
    return shlex.join(cmd)


def _quote_cli(
    protocol: Protocol, profile: RunProfile, bundle_path: str, out: Path, log: _LoadLog
) -> list[dict[str, Any]]:
    # Hard wrapper bound: every algorithm's prepare and solve limits, plus fixed slack.
    solve_limit = profile.budget.time_limit_seconds or 0.0  # always set by the profile loader
    per_algorithm = profile.worker.prepare_time_limit_seconds + solve_limit
    bound = len(profile.algorithms) * per_algorithm + 120
    results = []
    for index in range(1, protocol.quote_cli_invocations + 1):
        quotes_dir = out / f"{index:02d}"
        s = protocol.sentinel
        command = [sys.executable, str(REPO_ROOT / "main.py"), "quote", "--bundle", bundle_path,
                   "--profile", profile.source_path, "--token-in", s["token_in"],
                   "--token-out", s["token_out"], "--amount", s["amount"],
                   "--quotes-dir", str(quotes_dir)]  # fmt: skip
        log.sample(stage="quote_cli", invocation=index, point="before")
        started = time.perf_counter_ns()
        proc = subprocess.run(
            command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=bound, check=False
        )
        wall = (time.perf_counter_ns() - started) / NS_PER_SECOND
        log.sample(stage="quote_cli", invocation=index, point="after")
        quotes_dir.mkdir(parents=True, exist_ok=True)
        (quotes_dir / "stdout.txt").write_text(proc.stdout, encoding="utf-8")
        (quotes_dir / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
        run_dirs = sorted(p.parent for p in quotes_dir.glob("*/runs/*/manifest.json"))
        entry: dict[str, Any] = {
            "invocation": index,
            "command": shlex.join(command),
            "exit_code": proc.returncode,
            "cli_wall_seconds": wall,
            "run_dir": str(run_dirs[0]) if len(run_dirs) == 1 else None,
        }
        if proc.returncode == 0 and len(run_dirs) == 1:
            manifest = load_manifest(run_dirs[0])
            entry["bundle_hash"] = manifest.bundle_hash
            entry["measurement"] = {k: manifest.measurement.get(k) for k in
                                    ("warmup", "repeats", "memory_pass")}  # fmt: skip
            entry["records"] = [
                {
                    "algorithm": r["algorithm"],
                    "status": r["status"],
                    "attempts_completed": r["measurement"].get("attempts_completed"),
                    "solve_seconds": r["measurement"].get("solve_seconds"),
                    "evaluation_seconds": r["measurement"].get("evaluation_seconds"),
                }
                for r in load_case_records(run_dirs[0])
            ]
            entry["prepare_events"] = [
                {k: e.get(k) for k in ("algorithm", "prepare_seconds", "startup_seconds")}
                for e in manifest.prepare_events
            ]
            entry["run_total_seconds"] = manifest.timing.get("total_seconds")
        results.append(entry)
    return results


def _checked_inputs(protocol: Protocol, allow_dirty: bool) -> tuple[dict[str, Any], RunProfile]:
    """The measured source identity (refusing a dirty tree unless allowed) and the pinned
    profile (refusing a different sha256)."""
    source = source_identity()
    if source["git_dirty"] and not allow_dirty:
        raise LatencyError(
            "the source tree is dirty; commit first (or pass --allow-dirty to record an "
            f"honest dirty-patch identity): {source['dirty_paths']}"
        )
    profile_path = REPO_ROOT / protocol.profile_path
    if sha256_file(profile_path) != protocol.profile_sha256:
        raise LatencyError(f"{protocol.profile_path}: sha256 differs from the protocol's")
    return source, load_profile(profile_path)


def run_latency_experiment(
    protocol_path: str | Path,
    bundle_path: str,
    out_dir: str | Path,
    *,
    stages: Sequence[str] = STAGES,
    allow_dirty: bool = False,
    experiment_id: str | None = None,
    echo: Callable[[str], None] = print,
) -> Path:
    """Run the protocol's stages sequentially; return the experiment directory."""
    if not stages or not set(stages) <= set(STAGES):
        raise LatencyError(f"stages must be a non-empty subset of {list(STAGES)}")
    protocol = load_protocol(protocol_path)
    source, profile = _checked_inputs(protocol, allow_dirty)
    parent = load_bundle(bundle_path)
    out = Path(out_dir) / (experiment_id or new_run_id())
    out.mkdir(parents=True)  # exclusive: an experiment is never overwritten
    log = _LoadLog(out / LOAD_FILE, protocol.max_loadavg_1m_per_cpu)
    environment = environment_record(profile.worker.to_dict())
    replay = _replay(protocol, bundle_path, Path(out_dir), stages)
    bundles = build_bundles(protocol, parent, out / "bundles")
    document: dict[str, Any] = {
        "schema": EXPERIMENT_SCHEMA,
        "experiment_id": out.name,
        "state": "running",
        "created_at": _now(),
        "finished_at": None,
        "replay_command": replay,
        "stages_requested": list(stages),
        "partial": tuple(stages) != STAGES,
        "protocol": {"path": protocol.path, "sha256": protocol.sha256, "key": protocol.key,
                     "version": protocol.version, "document": protocol.document},
        "source": source,
        "environment": environment,
        "parent_bundle": {"path": bundle_path, "bundle_id": parent.bundle_id,
                          "bundle_hash": parent.bundle_hash},
        "profile": {"path": protocol.profile_path, "sha256": protocol.profile_sha256},
        "algorithms": list(profile.algorithms),
        "bundles": {label: {"path": b.source_path, "bundle_id": b.bundle_id,
                            "bundle_hash": b.bundle_hash, "cases": len(b.cases)}
                    for label, b in bundles.items()},
        "runs": [],
        "quote_cli": [],
        "load": None,
    }  # fmt: skip
    _atomic_json(out / EXPERIMENT_FILE, document)

    def _stage_run(label: str, stage: str, order: str, **kwargs: Any) -> None:
        run_id = f"{stage}-{order}-{label.replace('/', '-')}"
        before = log.sample(stage=stage, run_id=run_id, point="before")

        def on_record(algorithm: str, case_id: str) -> None:
            log.sample(stage=stage, run_id=run_id, algorithm=algorithm, case=case_id)

        echo(f"[{_now()}] {run_id}: start (load {before[0]:.2f})")
        manifest = measure_run(
            bundles[label], profile, results_dir=out / "runs", run_id=run_id, stage=stage,
            order=order, replay_command=replay, environment=environment,
            on_record=on_record,
            **kwargs,
        )  # fmt: skip
        after = log.sample(stage=stage, run_id=run_id, point="after")
        document["runs"].append({
            "run_id": run_id, "run_dir": manifest.run_dir, "stage": stage, "order": order,
            "bundle": label, "status_counts": manifest.status_counts,
            "total_seconds": manifest.timing.get("total_seconds"),
            "loadavg_before": list(before), "loadavg_after": list(after),
        })  # fmt: skip
        _atomic_json(out / EXPERIMENT_FILE, document)
        echo(f"[{_now()}] {run_id}: {manifest.status_counts}")

    try:
        if "timing" in stages:
            labels = [f"{c}/{k}" for c in protocol.cohorts for k in ("matrix", "sentinel")]
            for order in protocol.orders:
                for label in labels if order == "fixed" else list(reversed(labels)):
                    _stage_run(
                        label,
                        "timing",
                        order,
                        warmup=protocol.warmup,
                        repeats=protocol.repeats,
                        scope="algorithm",
                        memory=False,
                    )
        if "cold" in stages:
            for cohort in protocol.cold_cohorts:
                for kind in ("matrix", "sentinel"):
                    _stage_run(
                        f"{cohort}/{kind}",
                        "cold",
                        "fixed",
                        warmup=0,
                        repeats=1,
                        scope="case",
                        memory=True,
                    )
        if "quote_cli" in stages:
            document["quote_cli"] = _quote_cli(
                protocol, profile, str(Path(bundle_path).resolve()), out / "quote_cli", log
            )
    except BaseException:
        document.update(state="interrupted", finished_at=_now(), load=log.summary())
        _atomic_json(out / EXPERIMENT_FILE, document)
        raise
    document.update(state="complete", finished_at=_now(), load=log.summary())
    _atomic_json(out / EXPERIMENT_FILE, document)
    return out


# ------------------------------------------------------------------ sufficient budget

SUFFICIENT_SCHEMA = "latency-sufficient-budget/1"
SUFFICIENT_EXPERIMENT_SCHEMA = "latency-sufficient-budget-experiment/1"


@dataclass(frozen=True)
class SufficientBudget:
    """Which (cohort, matrix case, algorithm) records are re-solved under a raised budget,
    so exactness is established on the whole search scope rather than on what a fixed
    budget happened to complete."""

    path: str
    sha256: str
    document: dict[str, Any]
    key: str
    version: int
    protocol_path: str
    protocol_sha256: str
    budget: Budget
    warmup: int
    repeats: int
    pairs: tuple[tuple[str, str, str], ...]  # (cohort, case id, algorithm)


def load_sufficient_budget(path: str | Path) -> SufficientBudget:
    import yaml

    path = Path(path)
    data = path.read_bytes()
    raw = yaml.safe_load(data)
    try:
        top = _keys(raw, {"schema", "key", "version", "protocol", "budget", "measurement",
                          "cases"}, "<root>")  # fmt: skip
        if top["schema"] != SUFFICIENT_SCHEMA:
            raise LatencyError(f"schema: expected {SUFFICIENT_SCHEMA!r}, got {top['schema']!r}")
        protocol = _keys(top["protocol"], {"path", "sha256"}, "protocol")
        try:
            budget = _parse_budget(top["budget"], "budget")
        except ProfileError as exc:
            raise LatencyError(str(exc)) from exc
        measurement = _keys(top["measurement"], {"warmup", "repeats"}, "measurement")
        if not isinstance(top["cases"], list) or not top["cases"]:
            raise LatencyError("cases: expected a non-empty list")
        pairs: list[tuple[str, str, str]] = []
        for i, entry in enumerate(top["cases"]):
            item = _keys(entry, {"cohort", "case", "algorithms", "reason"}, f"cases[{i}]")
            algorithms = item["algorithms"]
            if not isinstance(algorithms, list) or not algorithms:
                raise LatencyError(f"cases[{i}].algorithms: expected a non-empty list")
            pairs += [(str(item["cohort"]), str(item["case"]), str(a)) for a in algorithms]
        if len(set(pairs)) != len(pairs):
            raise LatencyError("cases: duplicate (cohort, case, algorithm)")
        return SufficientBudget(
            path=str(path), sha256=sha256_bytes(data), document=raw, key=str(top["key"]),
            version=_int(top["version"], 1, "version"), protocol_path=str(protocol["path"]),
            protocol_sha256=str(protocol["sha256"]), budget=budget,
            warmup=_int(measurement["warmup"], 0, "measurement.warmup"),
            repeats=_int(measurement["repeats"], 2, "measurement.repeats"),
            pairs=tuple(pairs),
        )  # fmt: skip
    except LatencyError as exc:
        raise LatencyError(f"{path}: {exc}") from exc


def run_sufficient_budget(
    sufficient_path: str | Path,
    bundle_path: str,
    out_dir: str | Path,
    *,
    allow_dirty: bool = False,
    experiment_id: str | None = None,
    echo: Callable[[str], None] = print,
) -> Path:
    """Re-solve the listed records on the main protocol's derived matrix bundles (same
    bundle hashes) and pinned profile, with only the budget replaced; fixed order, one warm
    worker per algorithm, `repeats` attempts (so attempt consistency is checked)."""
    sufficient = load_sufficient_budget(sufficient_path)
    protocol = load_protocol(REPO_ROOT / sufficient.protocol_path)
    if protocol.sha256 != sufficient.protocol_sha256:
        raise LatencyError(f"{sufficient.protocol_path}: sha256 differs from the pinned one")
    source, profile = _checked_inputs(protocol, allow_dirty)
    matrix = {m.case_id for m in protocol.matrix}
    for cohort, case_id, algorithm in sufficient.pairs:
        if cohort not in protocol.cohorts or case_id not in matrix:
            raise LatencyError(f"{cohort}/{case_id}: not a protocol cohort/matrix case")
        if algorithm not in profile.algorithms:
            raise LatencyError(f"{algorithm}: not an algorithm of the pinned profile")
    parent = load_bundle(bundle_path)
    out = Path(out_dir) / (experiment_id or new_run_id())
    out.mkdir(parents=True)
    log = _LoadLog(out / LOAD_FILE, protocol.max_loadavg_1m_per_cpu)
    environment = environment_record(profile.worker.to_dict())
    replay = shlex.join(["uv", "run", "python", "-m", "benchmark.latency", "sufficient",
                         "--sufficient", sufficient.path, "--bundle", bundle_path,
                         "--out", str(out_dir)])  # fmt: skip
    bundles = build_bundles(protocol, parent, out / "bundles")
    measured = replace(profile, budget=sufficient.budget)
    document: dict[str, Any] = {
        "schema": SUFFICIENT_EXPERIMENT_SCHEMA,
        "experiment_id": out.name,
        "state": "running",
        "created_at": _now(),
        "finished_at": None,
        "replay_command": replay,
        "sufficient_budget": {"path": sufficient.path, "sha256": sufficient.sha256,
                              "key": sufficient.key, "version": sufficient.version,
                              "document": sufficient.document},
        "protocol": {"path": protocol.path, "sha256": protocol.sha256},
        "source": source,
        "environment": environment,
        "parent_bundle": {"path": bundle_path, "bundle_id": parent.bundle_id,
                          "bundle_hash": parent.bundle_hash},
        "profile": {"path": protocol.profile_path, "sha256": protocol.profile_sha256},
        "budget": sufficient.budget.to_dict(),
        "fixed_budget": profile.budget.to_dict(),
        "bundles": {f"{c}/matrix": {"bundle_hash": bundles[f"{c}/matrix"].bundle_hash}
                    for c in protocol.cohorts},
        "runs": [],
        "load": None,
    }  # fmt: skip
    _atomic_json(out / EXPERIMENT_FILE, document)
    try:
        for cohort in sorted({c for c, _, _ in sufficient.pairs}):
            run_id = f"sufficient-{cohort}"
            pairs = frozenset((a, k) for c, k, a in sufficient.pairs if c == cohort)
            log.sample(stage="sufficient_budget", run_id=run_id, point="before")
            echo(f"[{_now()}] {run_id}: start ({len(pairs)} record(s))")
            manifest = measure_run(
                bundles[f"{cohort}/matrix"], measured, results_dir=out / "runs",
                run_id=run_id, stage="sufficient_budget", order="fixed",
                warmup=sufficient.warmup, repeats=sufficient.repeats, scope="algorithm",
                memory=False, replay_command=replay, environment=environment, pairs=pairs,
            )  # fmt: skip
            log.sample(stage="sufficient_budget", run_id=run_id, point="after")
            document["runs"].append({"run_id": run_id, "cohort": cohort,
                                     "status_counts": manifest.status_counts})  # fmt: skip
            _atomic_json(out / EXPERIMENT_FILE, document)
            echo(f"[{_now()}] {run_id}: {manifest.status_counts}")
    except BaseException:
        document.update(state="interrupted", finished_at=_now(), load=log.summary())
        _atomic_json(out / EXPERIMENT_FILE, document)
        raise
    document.update(state="complete", finished_at=_now(), load=log.summary())
    _atomic_json(out / EXPERIMENT_FILE, document)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m benchmark.latency")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="Run a protocol experiment (sequential stages)")
    run_p.add_argument("--protocol", required=True)
    run_p.add_argument("--bundle", required=True, help="Frozen parent corpus bundle")
    run_p.add_argument("--out", default="data/latency")
    run_p.add_argument("--stages", default=",".join(STAGES))
    run_p.add_argument("--allow-dirty", action="store_true")
    sb_p = sub.add_parser("sufficient", help="Re-solve listed records under a raised budget")
    sb_p.add_argument("--sufficient", required=True, help="Sufficient-budget protocol")
    sb_p.add_argument("--bundle", required=True, help="Frozen parent corpus bundle")
    sb_p.add_argument("--out", default="data/latency")
    sb_p.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)

    def _terminate(signum: int, frame: object) -> None:
        raise KeyboardInterrupt(f"signal {signum}")

    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        if args.command == "sufficient":
            out = run_sufficient_budget(
                args.sufficient, args.bundle, args.out, allow_dirty=args.allow_dirty
            )
        else:
            out = run_latency_experiment(
                args.protocol, args.bundle, args.out,
                stages=[s for s in args.stages.split(",") if s], allow_dirty=args.allow_dirty,
            )  # fmt: skip
    except LatencyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt as exc:
        print(f"experiment INTERRUPTED ({exc or 'interrupt'}); recorded as interrupted "
              f"under {args.out}", file=sys.stderr)  # fmt: skip
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(f"experiment: {out}\nsummarize:  uv run python -m report.latency summarize {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

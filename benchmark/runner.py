"""`run_experiment(bundle, profile) -> RunManifest` (docs/DESIGN.md §4.3, §2.10,
§4.4 Run flow; measured isolation by WHI-1437).

Every scheduled (algorithm, case) pair ends with exactly one per-case record:

1. **Isolated workers.** Each algorithm's `AlgorithmFactory` runs in a fresh
   worker process (`benchmark.worker`). The worker's `prepare(bundle, config)` is
   timed and charged separately as a *prepare event* in the manifest -- per
   worker start, including every replacement -- so preparation is never hidden
   inside solve latency. With `worker.scope: case` every case gets its own worker
   (and its own charged prepare).
2. **Measured solves under hard limits.** Per case: `measurement.warmup`
   discarded attempts, then `measurement.repeats` measured attempts, each with a
   fresh `SolveContext`, the deterministic per-case seed, the profile's `Budget`,
   a hard wall-clock kill at `budget.time_limit_seconds` and a hard quote meter at
   `budget.max_quotes`. Latency samples are the worker's own `perf_counter_ns`
   measurement of the `solve()` call alone.
3. **Failures are outcomes, not holes.** A timeout, quote-limit breach, raised
   exception or crashed process ends the case with `timeout` / `algorithm_error`
   (with `limit_hit` saying which limit), the worker is discarded, and the next
   case gets a brand-new worker with pristine state. A candidate the solver had
   published before being cut off is independently evaluated and kept as a
   separately labeled `last_valid_candidate`; the case never counts as a completed
   solve.
4. **Independent evaluation** of the first measured attempt's plan (never the
   solver's own claim; docs/DESIGN.md §2.5) happens here in the parent, timed
   separately (`evaluation_seconds`) and excluded from solver latency. All
   attempts of a case must agree; disagreement is recorded as
   `attempts_consistent: false` (state leaking between attempts).
5. **Crash-tolerant records.** Records are appended atomically as each case
   finishes (`benchmark.results.RunWriter`). On interruption every unfinished
   scheduled pair is recorded as `cancelled`, the manifest is finalized as
   `interrupted` and `RunInterrupted` is raised; a run killed outright keeps its
   `running` (incomplete) manifest.
6. **Separate memory pass.** With `measurement.memory_pass`, a second pass in
   separate `tracemalloc` workers records peak allocation per case into
   `memory.jsonl`; timing workers never trace (`instrumented: false`).

Cases run in `measurement.order` (`fixed` = bundle order); `reverse`/`shuffle`
(seeded) plus `compare_runs` form the state-leak check (docs/DESIGN.md §2.10:
"Shuffle/reverse order checks detect accidental state leakage"). Algorithms run
sequentially, in profile order, on the same machine.
"""

from __future__ import annotations

import hashlib
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmark.objective import ObjectiveContext
from benchmark.profile import CASE_ORDERS, CaseOrder, RunProfile
from benchmark.results import (
    STATE_COMPLETE,
    STATE_INTERRUPTED,
    CaseRecord,
    ResultError,
    RunManifest,
    RunWriter,
    environment_record,
    load_case_records,
    load_manifest,
)
from benchmark.worker import AttemptOutcome, SolveRequest, Worker, WorkerSpec
from routing.algorithms.base import AlgorithmFactory, SolveResult, SolveStatus
from routing.algorithms.registry import get_algorithm
from routing.evaluator import EvalStatus, evaluate
from routing.plan import RoutePlan
from snapshot.models import Case, SnapshotBundle

NS_PER_SECOND = 1_000_000_000


class RunInterrupted(KeyboardInterrupt):  # noqa: N818 -- it *is* a KeyboardInterrupt
    """The run was interrupted; `manifest` is the finalized `interrupted` manifest
    (every unfinished scheduled case recorded as `cancelled`)."""

    def __init__(self, manifest: RunManifest) -> None:
        super().__init__(f"run {manifest.run_id} interrupted")
        self.manifest = manifest


def _seconds(ns: int | None) -> float | None:
    return None if ns is None else ns / NS_PER_SECOND


def ordered_cases(cases: Sequence[Case], order: CaseOrder, seed: int) -> tuple[Case, ...]:
    if order == "fixed":
        return tuple(cases)
    if order == "reverse":
        return tuple(reversed(cases))
    if order == "shuffle":
        shuffled = list(cases)
        random.Random(seed).shuffle(shuffled)
        return tuple(shuffled)
    raise ValueError(f"unknown case order {order!r}; expected one of {CASE_ORDERS}")


def case_seed(seed: int, algorithm: str, case_id: str) -> int:
    """Deterministic per-(algorithm, case) solver seed, independent of case order
    (so reverse/shuffle runs hand every case the same seed)."""
    digest = hashlib.sha256(f"{seed}:{algorithm}:{case_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") >> 11  # 53 bits: exact in any JSON reader


# ------------------------------------------------------------------ records


def _independent_record(
    bundle: SnapshotBundle,
    case: Case,
    objective: ObjectiveContext,
    solved: SolveResult,
) -> tuple[CaseRecord, int]:
    """The recorded outcome of a returned solve, from the runner's own fresh
    evaluation. Returns the record and the evaluation time in ns."""
    solver_reported_gross_output = (
        solved.evaluation.gross_output if solved.evaluation is not None else None
    )
    common: dict[str, Any] = {
        "case_id": case.case_id,
        "algorithm": solved.algorithm,
        "candidates_considered": solved.candidates_considered,
        "candidates_truncated": solved.candidates_truncated,
        "search": dict(solved.search_stats),
        "solver_reported_status": solved.status,
        "solver_reported_gross_output": solver_reported_gross_output,
        "solver_reported_score": solved.score,
    }

    if solved.plan is None:
        # Nothing to independently verify: a solver's "no route found" (or
        # unsupported/timeout/...) claim has no submitted plan to re-evaluate.
        # docs/DESIGN.md §2.5's independence guarantee is specifically about
        # plans; the search-process claim itself is recorded as-is -- except that
        # a claimed `ok` without a plan is not a result at all.
        if solved.status is SolveStatus.OK:
            return (
                CaseRecord(
                    status=SolveStatus.ALGORITHM_ERROR,
                    evaluation=None,
                    score=None,
                    error="solver claimed ok without submitting a plan",
                    **common,
                ),
                0,
            )
        return (
            CaseRecord(
                status=solved.status, evaluation=None, score=None, error=solved.error, **common
            ),
            0,
        )

    start = time.perf_counter_ns()
    independent_evaluation = evaluate(bundle, case, solved.plan, objective)
    evaluation_ns = time.perf_counter_ns() - start
    if independent_evaluation.status is EvalStatus.OK:
        status = SolveStatus.OK
        score: int | None = objective.score(independent_evaluation)
        error = None
    else:
        # The solver may have claimed `ok` for this exact plan; the runner's own
        # fresh replay disagrees, and the fresh replay always wins.
        status = SolveStatus.INVALID_PLAN
        score = None
        error = independent_evaluation.error

    return (
        CaseRecord(
            status=status,
            evaluation=independent_evaluation,
            score=score,
            error=error,
            **common,
        ),
        evaluation_ns,
    )


def _candidate_record(
    bundle: SnapshotBundle, case: Case, objective: ObjectiveContext, plan: RoutePlan | None
) -> dict[str, Any] | None:
    """Independent evaluation of the last candidate a cut-off solve published.
    Kept only when it is valid; it is a labeled diagnostic, never the outcome."""
    if plan is None:
        return None
    evaluation = evaluate(bundle, case, plan, objective)
    if evaluation.status is not EvalStatus.OK:
        return None
    return {
        "label": "last valid candidate before the solve was cut off (not a completed solve)",
        "evaluation": evaluation.to_dict(),
        "score": str(objective.score(evaluation)),
    }


def _fingerprint(result: SolveResult | None) -> Any:
    if result is None:
        return None
    return (
        result.case_id,
        result.status,
        result.plan,
        result.evaluation,
        result.score,
        result.candidates_considered,
        result.candidates_truncated,
        result.error,
        dict(result.search_stats),
    )


# ------------------------------------------------------------------ workers


@dataclass
class _PrepareFailure:
    status: SolveStatus
    limit_hit: str | None
    error: str
    event_index: int


@dataclass
class _AlgorithmTotals:
    cases: int = 0
    prepare_count: int = 0
    prepare_ns: int = 0
    startup_ns: int = 0
    solve_ns: int = 0
    evaluation_ns: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "cases": self.cases,
            "prepare_count": self.prepare_count,
            "prepare_seconds_total": self.prepare_ns / NS_PER_SECOND,
            "startup_seconds_total": self.startup_ns / NS_PER_SECOND,
            "solve_seconds_total": self.solve_ns / NS_PER_SECOND,
            "evaluation_seconds_total": self.evaluation_ns / NS_PER_SECOND,
            "amortized_prepare_seconds_per_case": (
                self.prepare_ns / NS_PER_SECOND / self.cases if self.cases else None
            ),
        }


@dataclass
class _Run:
    bundle: SnapshotBundle
    profile: RunProfile
    writer: RunWriter
    next_worker_id: int = 0
    event_count: int = 0
    totals: dict[str, _AlgorithmTotals] = field(default_factory=dict)


class _WorkerSlot:
    """The (at most one) live worker of one algorithm in one pass. Replaces a
    discarded worker lazily, charges every start as a prepare event, and makes a
    failed `prepare` sticky so a deterministic failure is not retried per case."""

    def __init__(self, run: _Run, factory: AlgorithmFactory, *, memory: bool) -> None:
        self.run = run
        self.factory = factory
        self.memory = memory
        self.pass_name = "memory" if memory else "timing"
        self.worker: Worker | None = None
        self.failure: _PrepareFailure | None = None
        self._next_reason = "initial"
        self._last_event_index: int | None = None

    @property
    def event_index(self) -> int | None:
        return self._last_event_index

    def acquire(self) -> Worker | _PrepareFailure:
        if self.worker is not None and self.worker.usable:
            return self.worker
        if self.failure is not None:
            return self.failure
        run = self.run
        spec = WorkerSpec(
            factory=self.factory,
            config=run.profile.algorithm_config(self.factory),
            bundle=run.bundle,
            objective=run.profile.objective,
            memory=self.memory,
        )
        worker = Worker(
            spec, start_method=run.profile.worker.start_method, worker_id=run.next_worker_id
        )
        run.next_worker_id += 1
        reason = self._next_reason
        outcome = worker.start(run.profile.worker.prepare_time_limit_seconds)
        event: dict[str, Any] = {
            "index": run.event_count,
            "pass": self.pass_name,
            "algorithm": self.factory.name,
            "worker_id": worker.worker_id,
            "reason": reason,
            "status": outcome.kind,
            "prepare_seconds": _seconds(outcome.prepare_ns),
            "startup_seconds": _seconds(outcome.startup_ns),
            "error": outcome.error,
        }
        if self.memory:
            event["prepare_peak_bytes"] = outcome.peak_bytes
        run.writer.add_prepare_event(event)
        self._last_event_index = run.event_count
        run.event_count += 1
        if not self.memory:
            totals = run.totals.setdefault(self.factory.name, _AlgorithmTotals())
            totals.prepare_count += 1
            totals.prepare_ns += outcome.prepare_ns or 0
            totals.startup_ns += outcome.startup_ns
        if not outcome.ok:
            timed_out = outcome.kind == "timeout"
            self.failure = _PrepareFailure(
                status=SolveStatus.TIMEOUT if timed_out else SolveStatus.ALGORITHM_ERROR,
                limit_hit="prepare_time" if timed_out else None,
                error=f"prepare failed ({outcome.kind}): {outcome.error}",
                event_index=event["index"],
            )
            return self.failure
        self.worker = worker
        self._next_reason = "replacement"
        return worker

    def after_case(self) -> None:
        if self.run.profile.worker.scope == "case":
            if self.worker is not None:  # healthy: retired by scope, not replaced
                self._next_reason = "per_case"
            self.close()

    def discard(self) -> None:
        """Kill a worker whose state can no longer be trusted (timeout, crash,
        error); the next case starts a replacement from the pristine bundle."""
        if self.worker is not None:
            self.worker.kill()
        self.worker = None
        self._next_reason = "replacement"

    def close(self) -> None:
        if self.worker is not None:
            self.worker.close()
        self.worker = None

    def kill(self) -> None:
        if self.worker is not None:
            self.worker.kill()
        self.worker = None


# ------------------------------------------------------------------ passes


def _failure_status(outcome: AttemptOutcome) -> tuple[SolveStatus, str | None]:
    if outcome.kind == "timeout":
        return SolveStatus.TIMEOUT, "time"
    if outcome.kind == "quote_limit":
        return SolveStatus.TIMEOUT, "quotes"
    return SolveStatus.ALGORITHM_ERROR, None


def _measure_case(run: _Run, slot: _WorkerSlot, case: Case, schedule_index: int) -> CaseRecord:
    profile = run.profile
    settings = profile.measurement
    algorithm = slot.factory.name
    seed = case_seed(settings.seed, algorithm, case.case_id)
    measurement: dict[str, Any] = {
        "pass": "timing",
        "schedule_index": schedule_index,
        "seed": seed,
        "warmup": settings.warmup,
        "repeats": settings.repeats,
        "solve_seconds": [],
        "instrumented": False,
    }
    totals = run.totals.setdefault(algorithm, _AlgorithmTotals())
    totals.cases += 1

    acquired = slot.acquire()
    measurement["prepare_event"] = slot.event_index
    if isinstance(acquired, _PrepareFailure):
        return CaseRecord(
            case_id=case.case_id,
            algorithm=algorithm,
            status=acquired.status,
            evaluation=None,
            score=None,
            candidates_considered=0,
            error=acquired.error,
            solver_reported_status=None,
            solver_reported_gross_output=None,
            solver_reported_score=None,
            limit_hit=acquired.limit_hit,
            measurement=measurement,
        )
    worker = acquired
    measurement["worker_id"] = worker.worker_id

    request = SolveRequest(case=case, budget=profile.budget, seed=seed)
    total_attempts = settings.warmup + settings.repeats
    returned: list[AttemptOutcome] = []
    failure: AttemptOutcome | None = None
    failure_label = ""
    for attempt_index in range(total_attempts):
        outcome = worker.attempt(request)
        if outcome.kind != "returned":
            failure = outcome
            failure_label = (
                f"warmup {attempt_index + 1}/{settings.warmup}"
                if attempt_index < settings.warmup
                else f"repeat {attempt_index - settings.warmup + 1}/{settings.repeats}"
            )
            slot.discard()
            break
        returned.append(outcome)
        if attempt_index >= settings.warmup:
            assert outcome.solve_ns is not None
            measurement["solve_seconds"].append(outcome.solve_ns / NS_PER_SECOND)
            totals.solve_ns += outcome.solve_ns
        if outcome.instrumented:  # pragma: no cover - guarded by construction
            measurement["instrumented"] = True
    measurement["attempts_completed"] = len(returned)
    measurement["attempts_consistent"] = (
        len({repr(_fingerprint(o.result)) for o in returned}) <= 1 if returned else None
    )
    slot.after_case()

    if failure is not None:
        status, limit_hit = _failure_status(failure)
        measurement["elapsed_seconds"] = failure.elapsed_ns / NS_PER_SECOND
        measurement["candidates_reported"] = failure.candidates_reported
        return CaseRecord(
            case_id=case.case_id,
            algorithm=algorithm,
            status=status,
            evaluation=None,
            score=None,
            candidates_considered=0,
            error=f"{failure_label}: {failure.error}",
            solver_reported_status=None,
            solver_reported_gross_output=None,
            solver_reported_score=None,
            quotes_attempted=failure.quotes_attempted,
            quotes_counted=failure.quotes_counted,
            limit_hit=limit_hit,
            last_valid_candidate=_candidate_record(
                run.bundle, case, profile.objective, failure.last_candidate
            ),
            measurement=measurement,
        )

    measured = returned[settings.warmup]
    assert measured.result is not None
    measurement["elapsed_seconds"] = measured.elapsed_ns / NS_PER_SECOND
    measurement["candidates_reported"] = measured.candidates_reported
    result = measured.result
    if result.case_id != case.case_id or result.algorithm != algorithm:
        record = CaseRecord(
            case_id=case.case_id,
            algorithm=algorithm,
            status=SolveStatus.ALGORITHM_ERROR,
            evaluation=None,
            score=None,
            candidates_considered=result.candidates_considered,
            error=(
                f"solver answered for ({result.algorithm!r}, {result.case_id!r}) instead of "
                f"({algorithm!r}, {case.case_id!r})"
            ),
            solver_reported_status=result.status,
            solver_reported_gross_output=None,
            solver_reported_score=result.score,
        )
        evaluation_ns = 0
    else:
        record, evaluation_ns = _independent_record(run.bundle, case, profile.objective, result)
    measurement["evaluation_seconds"] = evaluation_ns / NS_PER_SECOND
    totals.evaluation_ns += evaluation_ns
    return CaseRecord(
        **{
            **record.__dict__,
            "quotes_attempted": measured.quotes_attempted,
            "quotes_counted": measured.quotes_counted,
            "measurement": measurement,
        }
    )


def _measure_memory(
    run: _Run, slot: _WorkerSlot, case: Case, schedule_index: int
) -> dict[str, Any]:
    profile = run.profile
    algorithm = slot.factory.name
    seed = case_seed(profile.measurement.seed, algorithm, case.case_id)
    record: dict[str, Any] = {
        "pass": "memory",
        "schedule_index": schedule_index,
        "case_id": case.case_id,
        "algorithm": algorithm,
        "seed": seed,
        "solve_peak_bytes": None,
        "solver_status": None,
        "error": None,
    }
    acquired = slot.acquire()
    record["prepare_event"] = slot.event_index
    if isinstance(acquired, _PrepareFailure):
        record.update(status="prepare_failed", error=acquired.error)
        return record
    record["worker_id"] = acquired.worker_id
    outcome = acquired.attempt(SolveRequest(case=case, budget=profile.budget, seed=seed))
    if outcome.kind == "returned":
        assert outcome.result is not None
        record.update(
            status="measured",
            solve_peak_bytes=outcome.peak_bytes,
            solver_status=outcome.result.status.value,
        )
    else:
        slot.discard()
        record.update(status=outcome.kind, error=outcome.error)
    slot.after_case()
    return record


def _cancelled_record(case: Case, algorithm: str, schedule_index: int) -> CaseRecord:
    return CaseRecord(
        case_id=case.case_id,
        algorithm=algorithm,
        status=SolveStatus.CANCELLED,
        evaluation=None,
        score=None,
        candidates_considered=0,
        error="run interrupted before this case finished",
        solver_reported_status=None,
        solver_reported_gross_output=None,
        solver_reported_score=None,
        measurement={"pass": "timing", "schedule_index": schedule_index},
    )


def run_experiment(
    bundle: SnapshotBundle,
    profile: RunProfile,
    *,
    results_dir: str | Path,
    replay_command: str,
    run_id: str | None = None,
    order: CaseOrder | None = None,
    progress: Callable[[CaseRecord], None] | None = None,
) -> RunManifest:
    """Measure every profiled algorithm on every bundle case (see module
    docstring). `order` overrides `profile.measurement.order` (and is recorded);
    `progress` is called after each case record is durably appended."""
    settings = profile.measurement
    effective_order: CaseOrder = order or settings.order
    cases = ordered_cases(bundle.cases, effective_order, settings.seed)
    factories = [get_algorithm(name) for name in profile.algorithms]
    schedule = [(factory, case) for factory in factories for case in cases]

    writer = RunWriter.create(
        results_dir,
        bundle=bundle,
        profile=profile,
        replay_command=replay_command,
        scheduled_count=len(schedule),
        measurement={
            **settings.to_dict(),
            "order": effective_order,
            "profile_order": settings.order,
            "case_order": [case.case_id for case in cases],
            "budget": profile.budget.to_dict(),
        },
        environment=environment_record(profile.worker.to_dict()),
        memory=settings.memory_pass,
        run_id=run_id,
    )
    run = _Run(bundle=bundle, profile=profile, writer=writer)
    timing: dict[str, Any] = {"clock": "perf_counter_ns"}
    done = 0
    memory_done = 0
    slot: _WorkerSlot | None = None
    run_start = time.perf_counter_ns()
    phase_start = run_start

    def _timing_summary() -> dict[str, Any]:
        now = time.perf_counter_ns()
        summary = dict(timing)
        summary.setdefault("timing_pass_seconds", (now - phase_start) / NS_PER_SECOND)
        summary["total_seconds"] = (now - run_start) / NS_PER_SECOND
        summary["per_algorithm"] = {
            name: totals.to_dict() for name, totals in sorted(run.totals.items())
        }
        return summary

    try:
        for index, (factory, case) in enumerate(schedule):
            if slot is None or slot.factory is not factory:
                if slot is not None:
                    slot.close()
                slot = _WorkerSlot(run, factory, memory=False)
            record = _measure_case(run, slot, case, index)
            writer.append(record)
            done += 1
            if progress is not None:
                progress(record)
        if slot is not None:
            slot.close()
            slot = None
        timing["timing_pass_seconds"] = (time.perf_counter_ns() - phase_start) / NS_PER_SECOND

        if settings.memory_pass:
            memory_start = time.perf_counter_ns()
            for index, (factory, case) in enumerate(schedule):
                if slot is None or slot.factory is not factory:
                    if slot is not None:
                        slot.close()
                    slot = _WorkerSlot(run, factory, memory=True)
                writer.append_memory(_measure_memory(run, slot, case, index))
                memory_done += 1
            if slot is not None:
                slot.close()
                slot = None
            timing["memory_pass_seconds"] = (time.perf_counter_ns() - memory_start) / NS_PER_SECOND
    except BaseException as exc:
        if slot is not None:
            slot.kill()  # an in-flight attempt would never read a graceful stop
        for index in range(done, len(schedule)):
            factory, case = schedule[index]
            writer.append(_cancelled_record(case, factory.name, index))
        if settings.memory_pass:
            for index in range(memory_done, len(schedule)):
                factory, case = schedule[index]
                writer.append_memory(
                    {
                        "pass": "memory",
                        "schedule_index": index,
                        "case_id": case.case_id,
                        "algorithm": factory.name,
                        "status": "cancelled",
                        "error": "run interrupted before this case finished",
                    }
                )
        manifest = writer.finalize(state=STATE_INTERRUPTED, timing=_timing_summary())
        if isinstance(exc, KeyboardInterrupt) and not isinstance(exc, RunInterrupted):
            raise RunInterrupted(manifest) from exc
        raise

    return writer.finalize(state=STATE_COMPLETE, timing=_timing_summary())


# ------------------------------------------------------------------ leak check

_TIMEOUT_STATUSES = {SolveStatus.TIMEOUT.value, SolveStatus.CANCELLED.value}


def _deterministic_view(record: Mapping[str, Any]) -> Any:
    if record["status"] in _TIMEOUT_STATUSES:
        # How far a killed search got is timing-dependent; only its status and
        # which limit ended it are deterministic.
        return {"status": record["status"], "limit_hit": record.get("limit_hit")}
    return {
        "status": record["status"],
        "evaluation": record["evaluation"],
        "score": record["score"],
        "error": record["error"],
        "candidates_considered": record["candidates_considered"],
        "candidates_truncated": record["candidates_truncated"],
        "search": record.get("search"),
        "quotes_counted": record["quotes"]["counted"],
        "solver_reported": record["solver_reported"],
        "seed": record["measurement"].get("seed"),
        "attempts_consistent": record["measurement"].get("attempts_consistent"),
    }


def compare_runs(run_a: str | Path, run_b: str | Path) -> list[str]:
    """Order-leak check: the deterministic outputs of two complete runs over the
    same bundle (typically `fixed` vs `reverse`/`shuffle` order) must agree per
    (algorithm, case). Returns human-readable mismatches; empty means no leak was
    detected."""
    manifest_a, manifest_b = load_manifest(run_a), load_manifest(run_b)
    if manifest_a.bundle_hash != manifest_b.bundle_hash:
        raise ResultError(
            f"runs are over different bundles ({manifest_a.bundle_hash} vs "
            f"{manifest_b.bundle_hash}); order comparison is meaningless"
        )
    by_key_a = {(r["algorithm"], r["case_id"]): r for r in load_case_records(run_a)}
    by_key_b = {(r["algorithm"], r["case_id"]): r for r in load_case_records(run_b)}
    mismatches: list[str] = []
    for key in sorted(by_key_a.keys() | by_key_b.keys()):
        if key not in by_key_a or key not in by_key_b:
            mismatches.append(f"{key[0]}/{key[1]}: scheduled in only one run")
            continue
        view_a, view_b = _deterministic_view(by_key_a[key]), _deterministic_view(by_key_b[key])
        if view_a != view_b:
            diff = sorted(
                k for k in view_a.keys() | view_b.keys() if view_a.get(k) != view_b.get(k)
            )
            mismatches.append(f"{key[0]}/{key[1]}: differs in {', '.join(diff)}")
    return mismatches

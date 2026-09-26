"""Isolated solver worker processes (docs/DESIGN.md §2.10, §4.5; WHI-1437).

One `Worker` is one fresh Python process (multiprocessing `spawn`/`forkserver`,
never `fork`) that receives a pickled copy of the pristine bundle, the algorithm's
`AlgorithmFactory` (ordinary module-level functions, pickled by reference) and its
`AlgorithmConfig`. In the child it:

1. runs and times `prepare(bundle, config)` once -- the preparation cost is sent
   back and charged separately, never folded into solve latency;
2. serves solve attempts: for each request it builds a **fresh** `SolveContext`,
   opens a `pools.quote.metered_quotes(max_quotes)` meter and times only the
   `solve()` call with `time.perf_counter_ns` (monotonic). The same window's process
   CPU time (`time.process_time_ns`) is reported beside it as an explicit metric;
   the ordinary runner does not record it (WHI-1503: `benchmark.latency` does).
   Candidates the solver publishes through `SolveContext.report_candidate` are
   streamed to the parent as they happen, so they survive a later kill.

The parent enforces the hard wall-clock limit: it waits on the pipe and the process
sentinel with a monotonic deadline and kills the process when the deadline passes.
A killed, crashed or erroring worker is never reused -- the runner starts a new one
from the parent's untouched bundle (docs/DESIGN.md §4.5: "failed solver workers are
replaced without reusing their pool state").

In `memory` mode the child runs under `tracemalloc` and reports peak allocation for
prepare and each solve; timing workers never enable it and report
`instrumented: false`, so instrumentation can never leak into latency samples.
"""

from __future__ import annotations

import copyreg
import multiprocessing
import signal
import time
import traceback
import tracemalloc
from collections.abc import Mapping
from dataclasses import dataclass
from multiprocessing.connection import Connection, wait
from types import MappingProxyType
from typing import Any, Literal

from benchmark.objective import ObjectiveContext
from pools.quote import QuoteLimitExceeded, metered_quotes
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    SolveContext,
    SolveResult,
)
from routing.plan import RoutePlan
from snapshot.models import Case, SnapshotBundle


def _frozen_mapping(items: dict[Any, Any]) -> Mapping[Any, Any]:
    return MappingProxyType(items)


def _reduce_frozen_mapping(mapping: MappingProxyType[Any, Any]) -> tuple[Any, ...]:
    return (_frozen_mapping, (dict(mapping),))


# Snapshot states and quote results freeze their mappings as `MappingProxyType`,
# which the standard pickler refuses. Crossing the worker boundary copies them as a
# fresh read-only proxy over a private dict: the child never shares the parent's
# objects, which is exactly the isolation wanted.
copyreg.pickle(MappingProxyType, _reduce_frozen_mapping)

# Mechanism constants for tearing a process down; not measurement budgets.
_STOP_JOIN_SECONDS = 2.0
_KILL_JOIN_SECONDS = 5.0


@dataclass(frozen=True)
class WorkerSpec:
    factory: AlgorithmFactory
    config: AlgorithmConfig
    bundle: SnapshotBundle
    objective: ObjectiveContext
    memory: bool = False


@dataclass(frozen=True)
class SolveRequest:
    case: Case
    budget: Budget
    seed: int


AttemptKind = Literal["returned", "error", "quote_limit", "timeout", "crashed"]


@dataclass(frozen=True)
class AttemptOutcome:
    """What one solve attempt produced, as observed by the parent.

    `returned`: the solver returned a `SolveResult` within every limit.
    `error`: it raised (or returned something that is not a `SolveResult`).
    `quote_limit`: it exceeded `Budget.max_quotes`.
    `timeout`: the parent killed it at `Budget.time_limit_seconds`.
    `crashed`: the process died without answering."""

    kind: AttemptKind
    result: SolveResult | None = None
    error: str | None = None
    solve_ns: int | None = None  # child-measured solve() time; None if killed/crashed
    solve_cpu_ns: int | None = None  # child process CPU over the same window (WHI-1503)
    elapsed_ns: int = 0  # parent-measured request->answer (or ->kill) time
    quotes_attempted: int | None = None
    quotes_counted: int | None = None
    last_candidate: RoutePlan | None = None
    candidates_reported: int = 0
    instrumented: bool = False
    peak_bytes: int | None = None


@dataclass(frozen=True)
class PrepareOutcome:
    ok: bool
    kind: Literal["ok", "error", "timeout", "crashed"]
    prepare_ns: int | None
    startup_ns: int
    error: str | None = None
    peak_bytes: int | None = None


# --------------------------------------------------------------------------- child


def _format_exception(exc: BaseException) -> str:
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()


def _worker_main(  # pragma: no cover - child
    requests: Connection, conn: Connection, spec: WorkerSpec
) -> None:
    # Ctrl-C belongs to the parent, which records the interruption and kills us.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if spec.memory:
        tracemalloc.start()
    start = time.perf_counter_ns()
    try:
        prepared = (
            spec.factory.prepare(spec.bundle, spec.config)
            if spec.factory.prepare is not None
            else None
        )
    except BaseException as exc:  # noqa: BLE001 - reported, never swallowed silently
        conn.send(("prepare_error", time.perf_counter_ns() - start, _format_exception(exc)))
        return
    prepare_ns = time.perf_counter_ns() - start
    prepare_peak = tracemalloc.get_traced_memory()[1] if spec.memory else None
    conn.send(("ready", prepare_ns, prepare_peak))

    while True:
        try:
            message = requests.recv()
        except EOFError:
            return
        if message[0] == "stop":
            return
        request: SolveRequest = message[1]
        _serve_attempt(conn, spec, prepared, request)


def _serve_attempt(  # pragma: no cover - child
    conn: Connection, spec: WorkerSpec, prepared: Any, request: SolveRequest
) -> None:
    def sink(plan: RoutePlan) -> None:
        conn.send(("candidate", plan))

    context = SolveContext(
        bundle=spec.bundle,
        objective=spec.objective,
        prepared=prepared,
        seed=request.seed,
        candidate_sink=sink,
    )
    instrumented = tracemalloc.is_tracing()
    baseline = 0
    if instrumented:
        tracemalloc.reset_peak()
        baseline = tracemalloc.get_traced_memory()[0]
    kind: AttemptKind = "returned"
    result: Any = None
    error: str | None = None
    with metered_quotes(request.budget.max_quotes) as meter:
        cpu_start = time.process_time_ns()
        start = time.perf_counter_ns()
        try:
            result = spec.factory.solve(request.case, context, request.budget)
        except QuoteLimitExceeded as exc:
            kind, error = "quote_limit", _format_exception(exc)
        except Exception as exc:  # noqa: BLE001 - becomes algorithm_error
            kind, error = "error", _format_exception(exc)
        solve_ns = time.perf_counter_ns() - start
        solve_cpu_ns = time.process_time_ns() - cpu_start
    peak = tracemalloc.get_traced_memory()[1] - baseline if instrumented else None
    if kind == "returned" and meter.exceeded:
        # The solver swallowed the limit and kept going: still a limit breach.
        kind, error, result = "quote_limit", f"quote limit {meter.limit} exhausted", None
    if kind == "returned" and not isinstance(result, SolveResult):
        kind, error = "error", f"solve() returned {type(result).__name__}, not SolveResult"
        result = None
    payload = {
        "kind": kind,
        "result": result if kind == "returned" else None,
        "error": error,
        "solve_ns": solve_ns,
        "solve_cpu_ns": solve_cpu_ns,
        "quotes_attempted": meter.attempted,
        "quotes_counted": meter.counted,
        "instrumented": instrumented,
        "peak_bytes": peak,
    }
    try:
        conn.send(("done", payload))
    except Exception as exc:  # noqa: BLE001 - e.g. an unpicklable result
        payload.update(kind="error", result=None, error=f"result not transferable: {exc!r}")
        conn.send(("done", payload))


# -------------------------------------------------------------------------- parent


class Worker:
    """Parent-side handle of one worker process (see module docstring)."""

    def __init__(self, spec: WorkerSpec, *, start_method: str, worker_id: int) -> None:
        self.spec = spec
        self.worker_id = worker_id
        self._ctx: Any = multiprocessing.get_context(start_method)
        self._conn: Connection | None = None  # child -> parent answers
        self._requests: Connection | None = None  # parent -> child requests
        self._process: Any = None
        self.usable = False

    @property
    def pid(self) -> int | None:
        return None if self._process is None else int(self._process.pid)

    def start(self, prepare_time_limit_seconds: float) -> PrepareOutcome:
        # Two one-way OS pipes rather than a duplex pipe: a duplex multiprocessing
        # pipe is a socketpair on POSIX, and an offline run must open no sockets.
        parent_conn, child_conn = self._ctx.Pipe(duplex=False)
        child_requests, parent_requests = self._ctx.Pipe(duplex=False)
        process = self._ctx.Process(
            target=_worker_main,
            args=(child_requests, child_conn, self.spec),
            name=f"solver-worker-{self.spec.factory.name}-{self.worker_id}",
            daemon=True,
        )
        started = time.perf_counter_ns()
        deadline = time.monotonic() + prepare_time_limit_seconds
        try:
            process.start()
        except Exception as exc:  # noqa: BLE001 - e.g. an unpicklable factory
            for end in (child_conn, parent_conn, child_requests, parent_requests):
                end.close()
            return PrepareOutcome(
                ok=False,
                kind="error",
                prepare_ns=None,
                startup_ns=time.perf_counter_ns() - started,
                error=f"worker could not be started: {_format_exception(exc)}",
            )
        child_conn.close()
        child_requests.close()
        self._conn, self._requests, self._process = parent_conn, parent_requests, process
        message, status = self._receive(deadline)
        startup_ns = time.perf_counter_ns() - started
        if status == "timeout":
            self.kill()
            return PrepareOutcome(
                ok=False,
                kind="timeout",
                prepare_ns=None,
                startup_ns=startup_ns,
                error=f"worker start-up + prepare exceeded {prepare_time_limit_seconds}s",
            )
        if status == "crashed" or message is None:
            code = self._exitcode()
            self.kill()
            return PrepareOutcome(
                ok=False,
                kind="crashed",
                prepare_ns=None,
                startup_ns=startup_ns,
                error=f"worker process exited during prepare (exit code {code})",
            )
        if message[0] == "prepare_error":
            self.kill()
            return PrepareOutcome(
                ok=False,
                kind="error",
                prepare_ns=int(message[1]),
                startup_ns=startup_ns,
                error=f"prepare raised: {message[2]}",
            )
        self.usable = True
        return PrepareOutcome(
            ok=True,
            kind="ok",
            prepare_ns=int(message[1]),
            startup_ns=startup_ns,
            peak_bytes=message[2],
        )

    def _exitcode(self) -> int | None:
        if self._process is None:
            return None
        self._process.join(timeout=_KILL_JOIN_SECONDS)
        code = self._process.exitcode
        return None if code is None else int(code)

    def _receive(self, deadline: float | None) -> tuple[Any, str]:
        """Next message, or `(None, "timeout"|"crashed")`."""
        assert self._conn is not None and self._process is not None
        while True:
            if self._conn.poll(0):
                try:
                    return self._conn.recv(), "ok"
                except (EOFError, OSError):
                    return None, "crashed"
            if not self._process.is_alive():
                if self._conn.poll(0):
                    continue
                return None, "crashed"
            timeout = None if deadline is None else deadline - time.monotonic()
            if timeout is not None and timeout <= 0:
                return None, "timeout"
            wait([self._conn, self._process.sentinel], timeout=timeout)

    def attempt(self, request: SolveRequest) -> AttemptOutcome:
        if not self.usable or self._conn is None or self._requests is None:
            raise RuntimeError("worker is not usable; start a new one")
        limit = request.budget.time_limit_seconds
        started = time.perf_counter_ns()
        deadline = None if limit is None else time.monotonic() + limit
        self._requests.send(("solve", request))
        last_candidate: RoutePlan | None = None
        reported = 0
        while True:
            message, status = self._receive(deadline)
            if status != "ok":
                elapsed = time.perf_counter_ns() - started
                if status == "timeout":
                    self.kill()
                    return AttemptOutcome(
                        kind="timeout",
                        error=f"solve exceeded the {limit}s time limit; worker killed",
                        elapsed_ns=elapsed,
                        last_candidate=last_candidate,
                        candidates_reported=reported,
                    )
                code = self._exitcode()
                self.kill()
                return AttemptOutcome(
                    kind="crashed",
                    error=f"worker process died during solve (exit code {code})",
                    elapsed_ns=elapsed,
                    last_candidate=last_candidate,
                    candidates_reported=reported,
                )
            if message[0] == "candidate":
                last_candidate = message[1]
                reported += 1
                if deadline is not None and time.monotonic() >= deadline:
                    # A solver streaming candidates must not starve the deadline check.
                    self.kill()
                    return AttemptOutcome(
                        kind="timeout",
                        error=f"solve exceeded the {limit}s time limit; worker killed",
                        elapsed_ns=time.perf_counter_ns() - started,
                        last_candidate=last_candidate,
                        candidates_reported=reported,
                    )
                continue
            elapsed = time.perf_counter_ns() - started
            payload = message[1]
            kind: AttemptKind = payload["kind"]
            if kind != "returned":
                self.usable = False  # possibly corrupted solver state: replace
            return AttemptOutcome(
                kind=kind,
                result=payload["result"],
                error=payload["error"],
                solve_ns=payload["solve_ns"],
                solve_cpu_ns=payload["solve_cpu_ns"],
                elapsed_ns=elapsed,
                quotes_attempted=payload["quotes_attempted"],
                quotes_counted=payload["quotes_counted"],
                last_candidate=last_candidate,
                candidates_reported=reported,
                instrumented=payload["instrumented"],
                peak_bytes=payload["peak_bytes"],
            )

    def kill(self) -> None:
        self.usable = False
        if self._process is not None and self._process.is_alive():
            self._process.kill()
        if self._process is not None:
            self._process.join(timeout=_KILL_JOIN_SECONDS)
        for end in (self._conn, self._requests):
            if end is not None:
                end.close()
        self._conn = self._requests = None

    def close(self) -> None:
        """Graceful stop for a healthy worker; kill otherwise."""
        if self.usable and self._requests is not None:
            try:
                self._requests.send(("stop",))
            except (OSError, ValueError):
                pass
            if self._process is not None:
                self._process.join(timeout=_STOP_JOIN_SECONDS)
        self.kill()

"""Versioned result records (docs/DESIGN.md §2.10/§4.5): a run directory holds a
`manifest.json` (run id, bundle/profile identity and hashes, replay command,
environment/provenance, measurement settings, preparation events, timing totals,
counts), a `cases.jsonl` per-case result file and -- when the profile asks for a
memory pass -- a separate `memory.jsonl`.

Result schema 2 (WHI-1437) makes a run an append-only, crash-tolerant artifact:

- `RunWriter.create` claims the run directory exclusively (`mkdir` without
  `exist_ok`), so a run is never silently overwritten, and immediately writes a
  manifest with `state: "running"` / `complete: false`.
- Every finished case is appended to `cases.jsonl` as one line in a single
  `write()` on an `O_APPEND` descriptor followed by `fsync`, so a crash leaves only
  whole, completed records (at worst one torn final line, which the loader
  detects).
- The manifest is rewritten atomically (temp file + `os.replace`) and finalized as
  `complete` or `interrupted`. A run killed outright keeps its `running` manifest --
  still explicitly incomplete.

Every `CaseRecord` here is the runner's own **independent** re-evaluation of a
solver's submitted plan (docs/DESIGN.md §4.4 Run flow: "independently evaluate
complete plans"; §2.5: "Solver-reported output is retained only as a
diagnostic."). `benchmark.runner` is responsible for producing `CaseRecord`s
this way; nothing here trusts a solver's self-reported status/output beyond the
`solver_reported_*` diagnostic fields.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import subprocess
import sys
import time
import tomllib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from benchmark.profile import RunProfile
from routing.algorithms.base import SolveStatus
from routing.evaluator import Evaluation
from snapshot.bundle import sha256_bytes, sha256_file
from snapshot.models import SnapshotBundle

RESULT_SCHEMA_VERSION = 2
MANIFEST_FILE = "manifest.json"
CASES_FILE = "cases.jsonl"
MEMORY_FILE = "memory.jsonl"
PROFILE_FILE = "profile.yaml"  # the CLI-derived effective profile of a run (WHI-1528)

STATE_RUNNING = "running"
STATE_COMPLETE = "complete"
STATE_INTERRUPTED = "interrupted"

REPO_ROOT = Path(__file__).resolve().parent.parent


class ResultError(ValueError):
    """A run directory could not be created or read as expected."""


def new_run_id(*, now: datetime | None = None) -> str:
    """`<UTC timestamp>-<random suffix>`: collision-resistant even across two
    runs started in the same second (docs/DESIGN.md §2.10: "Re-running with the
    same bundle/config cannot silently overwrite an existing run"; §4.5:
    "explicit new run IDs are sufficient")."""
    ts = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{ts}-{secrets.token_hex(4)}"


def _opt_str(value: int | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True)
class CaseRecord:
    """The canonical, independently-evaluated per-case record persisted by a
    run. `status`/`evaluation`/`score` always come from a fresh `evaluate()`
    call the runner made against the original bundle -- never from the
    solver's own claims -- or from the runner's own observation of a hard
    limit, crash or interruption. `solver_reported_*` fields are retained purely
    as a diagnostic (docs/DESIGN.md §2.5) and are `None` when the solver never
    returned a result.

    `last_valid_candidate` is only set for a solve cut off by a hard limit (or a
    crash) after it had reported a candidate: the runner's independent evaluation
    of that plan, labeled separately and never counted as a completed solve
    (docs/DESIGN.md §2.10). `measurement` holds the per-case timing samples and
    settings (see `benchmark.runner`)."""

    case_id: str
    algorithm: str
    status: SolveStatus
    evaluation: Evaluation | None
    score: int | None
    candidates_considered: int
    error: str | None
    solver_reported_status: SolveStatus | None
    solver_reported_gross_output: int | None
    solver_reported_score: int | None
    candidates_truncated: int = 0
    quotes_attempted: int | None = None
    quotes_counted: int | None = None
    limit_hit: str | None = None
    last_valid_candidate: Mapping[str, Any] | None = None
    measurement: Mapping[str, Any] = field(default_factory=dict)
    # The solver's own deterministic search counters (`SolveResult.search_stats`,
    # e.g. `single_path` hop bound / truncation / memoized quotes); empty when the
    # solver declares none or never returned.
    search: Mapping[str, Any] = field(default_factory=dict)
    # WHI-1548 (R021-C/1 §9.4): the runner's validated view of `search["r021"]` (or its own
    # `unavailable` view of an options-accepting identity's failed solve); `None` -- and no
    # `diagnostics` key at all in the saved record -- for every other record.
    diagnostics: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "case_id": self.case_id,
            "algorithm": self.algorithm,
            "status": self.status.value,
            "evaluation": self.evaluation.to_dict() if self.evaluation is not None else None,
            "score": _opt_str(self.score),
            "candidates_considered": self.candidates_considered,
            "candidates_truncated": self.candidates_truncated,
            "search": dict(self.search),
            "quotes": {"attempted": self.quotes_attempted, "counted": self.quotes_counted},
            "limit_hit": self.limit_hit,
            "error": self.error,
            # Diagnostic only -- never the source of `status`/`evaluation`/`score`
            # above (docs/DESIGN.md §2.5: "Solver-reported output is retained
            # only as a diagnostic.").
            "solver_reported": (
                None
                if self.solver_reported_status is None
                else {
                    "status": self.solver_reported_status.value,
                    "gross_output": _opt_str(self.solver_reported_gross_output),
                    "score": _opt_str(self.solver_reported_score),
                }
            ),
            "last_valid_candidate": (
                None if self.last_valid_candidate is None else dict(self.last_valid_candidate)
            ),
            "measurement": dict(self.measurement),
        }
        if self.diagnostics is not None:
            out["diagnostics"] = dict(self.diagnostics)
        return out


def _run_git(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(  # fixed local git subcommands only, no shell/network
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def git_provenance(repo_root: Path | None = None) -> tuple[str | None, bool | None, str | None]:
    """`(revision, dirty, diff_sha256)`. Tolerates a non-git checkout (all three
    `None`) rather than failing an otherwise-successful offline run."""
    root = repo_root or REPO_ROOT
    revision_out = _run_git(["rev-parse", "HEAD"], root)
    if revision_out is None:
        return None, None, None
    revision = revision_out.strip()
    status_out = _run_git(["status", "--porcelain"], root)
    if status_out is None:
        return revision, None, None
    dirty = bool(status_out.strip())
    diff_sha256 = None
    if dirty:
        diff_out = _run_git(["diff", "HEAD"], root)
        if diff_out is not None:
            diff_sha256 = sha256_bytes(diff_out.encode("utf-8"))
    return revision, dirty, diff_sha256


def cpu_model() -> str | None:
    """Best-effort CPU model string (docs/DESIGN.md §2.10: "CPU/model ... are
    recorded"); `None` rather than a guess when the platform does not expose it."""
    system = platform.system()
    if system == "Darwin":
        try:
            out = subprocess.run(  # fixed local sysctl read, no shell
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            out = None
        if out is not None and out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    elif system == "Linux":
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or None


def dependency_versions(pyproject: Path | None = None) -> dict[str, str | None]:
    """Installed versions of the project's declared runtime dependencies."""
    path = pyproject or (REPO_ROOT / "pyproject.toml")
    try:
        declared = tomllib.loads(path.read_text(encoding="utf-8"))["project"]["dependencies"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return {}
    versions: dict[str, str | None] = {}
    for spec in declared:
        name = spec.split(";")[0].strip()
        for sep in ("[", "<", ">", "=", "!", "~", " "):
            name = name.split(sep)[0]
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return dict(sorted(versions.items()))


def environment_record(worker: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Machine/software provenance for a run (docs/DESIGN.md §2.10: "CPU/model, OS,
    Python/dependency versions and worker settings are recorded")."""
    git_revision, git_dirty, git_diff_sha256 = git_provenance()
    clock = time.get_clock_info("perf_counter")
    return {
        "git_revision": git_revision,
        "git_dirty": git_dirty,
        "git_diff_sha256": git_diff_sha256,
        "python_version": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "os": platform.system(),
        "os_release": platform.release(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_model": cpu_model(),
        "cpu_count": os.cpu_count(),
        "dependencies": dependency_versions(),
        "clock": {
            "name": "perf_counter_ns",
            "monotonic": clock.monotonic,
            "resolution_seconds": clock.resolution,
        },
        "worker": dict(worker) if worker is not None else None,
    }


def _profile_sha256(profile_path: str) -> str | None:
    path = Path(profile_path)
    if not path.is_file():
        return None
    return sha256_file(path)


def experiment_identity(bundle_hash: str, objective: Mapping[str, Any]) -> dict[str, Any]:
    """`experiment_id` = SHA-256 over the canonical JSON of the corpus bundle hash and the
    resolved objective (mode plus, for `empirical_cost`, the cost-model and
    price-context hashes). Content-addressed: the artifact's file path is not identity."""
    identity = dict(objective)
    if isinstance(identity.get("cost_model"), Mapping):
        identity["cost_model"] = {k: v for k, v in identity["cost_model"].items() if k != "path"}
    body = {"bundle_hash": bundle_hash, "objective": identity}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return body | {"experiment_id": digest}


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    schema_version: int
    state: str
    bundle_id: str
    bundle_hash: str
    profile_path: str
    profile_sha256: str | None
    resolved_profile: dict[str, Any]
    objective_label: str
    algorithms: tuple[str, ...]
    created_at: str
    finished_at: str | None
    replay_command: str
    scheduled_count: int
    case_count: int
    status_counts: dict[str, int]
    cases_sha256: str | None
    memory_record_count: int | None
    memory_sha256: str | None
    measurement: dict[str, Any]
    prepare_events: tuple[dict[str, Any], ...]
    timing: dict[str, Any]
    environment: dict[str, Any]
    run_dir: str
    # WHI-1445: the experiment identity -- corpus bundle hash + objective (incl. the
    # cost-model and price-context hashes). A different model or price context is a
    # different experiment id; the bundle itself is never rewritten.
    experiment: dict[str, Any] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.state == STATE_COMPLETE

    @property
    def git_revision(self) -> str | None:
        value = self.environment.get("git_revision")
        return value if isinstance(value, str) else None

    @property
    def git_dirty(self) -> bool | None:
        value = self.environment.get("git_dirty")
        return value if isinstance(value, bool) else None

    @property
    def python_version(self) -> str:
        return str(self.environment.get("python_version", ""))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "state": self.state,
            "complete": self.complete,
            "bundle_id": self.bundle_id,
            "bundle_hash": self.bundle_hash,
            "profile_path": self.profile_path,
            "profile_sha256": self.profile_sha256,
            "resolved_profile": self.resolved_profile,
            "objective_label": self.objective_label,
            "algorithms": list(self.algorithms),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "replay_command": self.replay_command,
            "scheduled_count": self.scheduled_count,
            "case_count": self.case_count,
            "status_counts": self.status_counts,
            "cases_file": CASES_FILE,
            "cases_sha256": self.cases_sha256,
            "memory_file": MEMORY_FILE if self.memory_record_count is not None else None,
            "memory_record_count": self.memory_record_count,
            "memory_sha256": self.memory_sha256,
            "measurement": self.measurement,
            "prepare_events": list(self.prepare_events),
            "timing": self.timing,
            "environment": self.environment,
            "experiment": self.experiment,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], run_dir: str) -> RunManifest:
        return cls(
            run_id=raw["run_id"],
            schema_version=raw["schema_version"],
            state=raw["state"],
            bundle_id=raw["bundle_id"],
            bundle_hash=raw["bundle_hash"],
            profile_path=raw["profile_path"],
            profile_sha256=raw.get("profile_sha256"),
            resolved_profile=raw.get("resolved_profile", {}),
            objective_label=raw["objective_label"],
            algorithms=tuple(raw["algorithms"]),
            created_at=raw["created_at"],
            finished_at=raw.get("finished_at"),
            replay_command=raw["replay_command"],
            scheduled_count=raw["scheduled_count"],
            case_count=raw["case_count"],
            status_counts=dict(raw.get("status_counts", {})),
            cases_sha256=raw.get("cases_sha256"),
            memory_record_count=raw.get("memory_record_count"),
            memory_sha256=raw.get("memory_sha256"),
            measurement=dict(raw.get("measurement", {})),
            prepare_events=tuple(raw.get("prepare_events", [])),
            timing=dict(raw.get("timing", {})),
            environment=dict(raw.get("environment", {})),
            run_dir=run_dir,
            experiment=dict(raw.get("experiment", {})),
        )


def _append_line(path: Path, obj: Mapping[str, Any]) -> None:
    """One JSON line in one `write()` on an `O_APPEND` descriptor, then `fsync`:
    a crash leaves only whole records (at worst a torn final line)."""
    data = (json.dumps(obj, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        written = os.write(fd, data)
        if written != len(data):  # pragma: no cover - local-disk short write
            raise ResultError(f"{path}: short write ({written}/{len(data)} bytes)")
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, obj: Mapping[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class RunWriter:
    """Owns one run directory for the duration of a run (see module docstring)."""

    def __init__(self, run_dir: Path, manifest: RunManifest, *, memory: bool) -> None:
        self.run_dir = run_dir
        self._manifest = manifest
        self._memory = memory
        self._status_counts: Counter[str] = Counter()
        self._case_count = 0
        self._memory_count = 0
        self._prepare_events: list[dict[str, Any]] = []

    @classmethod
    def create(
        cls,
        results_dir: str | Path,
        *,
        bundle: SnapshotBundle,
        profile: RunProfile,
        replay_command: str,
        scheduled_count: int,
        measurement: Mapping[str, Any] | None = None,
        environment: Mapping[str, Any] | None = None,
        memory: bool = False,
        run_id: str | None = None,
        profile_text: str | None = None,
    ) -> RunWriter:
        """`profile_text` (WHI-1528, `main.py run --strategies`): the effective profile the
        CLI derived, written as `<run dir>/profile.yaml` before the manifest -- `profile`
        must have been parsed from exactly that text at exactly that path."""
        results_dir = Path(results_dir)
        run_id = run_id or new_run_id()
        run_dir = results_dir / run_id
        results_dir.mkdir(parents=True, exist_ok=True)
        try:
            run_dir.mkdir()  # exclusive claim: never reuse/overwrite an existing run
        except FileExistsError as exc:
            raise ResultError(f"{run_dir}: a run with id {run_id!r} already exists") from exc
        if profile_text is not None:
            saved = run_dir / PROFILE_FILE
            if profile.source_path != str(saved):
                raise ResultError(f"{saved}: the effective profile must be read from here")
            saved.write_text(profile_text, encoding="utf-8")
        (run_dir / CASES_FILE).touch()
        if memory:
            (run_dir / MEMORY_FILE).touch()
        manifest = RunManifest(
            run_id=run_id,
            schema_version=RESULT_SCHEMA_VERSION,
            state=STATE_RUNNING,
            bundle_id=bundle.bundle_id,
            bundle_hash=bundle.bundle_hash,
            profile_path=profile.source_path,
            profile_sha256=_profile_sha256(profile.source_path),
            resolved_profile=profile.resolved(),
            objective_label=profile.objective.label,
            algorithms=profile.algorithms,
            created_at=datetime.now(UTC).isoformat(),
            finished_at=None,
            replay_command=replay_command,
            scheduled_count=scheduled_count,
            case_count=0,
            status_counts={},
            cases_sha256=None,
            memory_record_count=0 if memory else None,
            memory_sha256=None,
            measurement=dict(measurement or {}),
            prepare_events=(),
            timing={},
            environment=dict(environment) if environment is not None else environment_record(),
            run_dir=str(run_dir),
            experiment=experiment_identity(bundle.bundle_hash, profile.objective.to_dict()),
        )
        writer = cls(run_dir, manifest, memory=memory)
        writer._write_manifest(manifest)
        return writer

    @property
    def manifest(self) -> RunManifest:
        return self._manifest

    def _write_manifest(self, manifest: RunManifest) -> None:
        self._manifest = manifest
        _atomic_write_json(self.run_dir / MANIFEST_FILE, manifest.to_dict())

    def _snapshot(self, **changes: Any) -> RunManifest:
        values = {
            **self._manifest.__dict__,
            "case_count": self._case_count,
            "status_counts": dict(sorted(self._status_counts.items())),
            "prepare_events": tuple(self._prepare_events),
            "memory_record_count": self._memory_count if self._memory else None,
        }
        values.update(changes)
        return RunManifest(**values)

    def append(self, record: CaseRecord) -> None:
        _append_line(self.run_dir / CASES_FILE, record.to_dict())
        self._case_count += 1
        self._status_counts[record.status.value] += 1

    def append_memory(self, record: Mapping[str, Any]) -> None:
        if not self._memory:
            raise ResultError("this run was not created with a memory pass")
        _append_line(self.run_dir / MEMORY_FILE, record)
        self._memory_count += 1

    def add_prepare_event(self, event: Mapping[str, Any]) -> None:
        """Recorded immediately (manifest rewritten atomically) so preparation cost
        is visible even if the run dies before finalizing."""
        self._prepare_events.append(dict(event))
        self._write_manifest(self._snapshot())

    def finalize(self, *, state: str, timing: Mapping[str, Any]) -> RunManifest:
        if state not in (STATE_COMPLETE, STATE_INTERRUPTED):
            raise ResultError(f"cannot finalize a run as {state!r}")
        if state == STATE_COMPLETE and self._case_count != self._manifest.scheduled_count:
            raise ResultError(
                f"{self.run_dir}: {self._case_count} record(s) for "
                f"{self._manifest.scheduled_count} scheduled case(s); not complete"
            )
        memory_sha = sha256_file(self.run_dir / MEMORY_FILE) if self._memory else None
        manifest = self._snapshot(
            state=state,
            finished_at=datetime.now(UTC).isoformat(),
            cases_sha256=sha256_file(self.run_dir / CASES_FILE),
            memory_sha256=memory_sha,
            timing=dict(timing),
        )
        self._write_manifest(manifest)
        return manifest


def save_run(
    results_dir: str | Path,
    *,
    bundle: SnapshotBundle,
    profile: RunProfile,
    results: list[CaseRecord],
    replay_command: str,
    run_id: str | None = None,
) -> RunManifest:
    """Write an already-computed list of records as one complete run."""
    writer = RunWriter.create(
        results_dir,
        bundle=bundle,
        profile=profile,
        replay_command=replay_command,
        scheduled_count=len(results),
        run_id=run_id,
    )
    for record in results:
        writer.append(record)
    return writer.finalize(state=STATE_COMPLETE, timing={})


def load_manifest(run_dir: str | Path, *, allow_incomplete: bool = False) -> RunManifest:
    """Load and verify a run manifest. A run that is not `complete` (still
    running, killed, or interrupted) is refused unless `allow_incomplete` -- its
    records are genuine but do not cover the whole schedule."""
    run_dir = Path(run_dir)
    manifest_path = run_dir / MANIFEST_FILE
    if not manifest_path.is_file():
        raise ResultError(f"{run_dir}: no {MANIFEST_FILE} found")
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise ResultError(
            f"{run_dir}: unsupported result schema {raw.get('schema_version')!r} "
            f"(expected {RESULT_SCHEMA_VERSION})"
        )
    manifest = RunManifest.from_dict(raw, str(run_dir))
    if not manifest.complete and not allow_incomplete:
        raise ResultError(f"{run_dir}: run is incomplete (state={manifest.state!r})")
    if manifest.state != STATE_RUNNING:
        cases_path = run_dir / CASES_FILE
        observed = sha256_file(cases_path) if cases_path.is_file() else None
        if observed != manifest.cases_sha256:
            raise ResultError(f"{run_dir}: {CASES_FILE} checksum does not match manifest")
        if manifest.memory_record_count is not None:
            memory_path = run_dir / MEMORY_FILE
            observed_memory = sha256_file(memory_path) if memory_path.is_file() else None
            if observed_memory != manifest.memory_sha256:
                raise ResultError(f"{run_dir}: {MEMORY_FILE} checksum does not match manifest")
    return manifest


def _read_jsonl(path: Path, *, tolerate_torn_tail: bool) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    records: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            is_tail = index == len(lines) - 1  # no trailing newline: interrupted write
            if tolerate_torn_tail and is_tail:
                break
            raise ResultError(f"{path}: line {index + 1} is not valid JSON") from exc
    return records


def load_case_records(run_dir: str | Path) -> list[dict[str, Any]]:
    """Every whole per-case record of a run, in execution order. A torn final line
    is tolerated only for a run that never finalized (`state: running`)."""
    manifest = load_manifest(run_dir, allow_incomplete=True)
    return _read_jsonl(
        Path(run_dir) / CASES_FILE, tolerate_torn_tail=manifest.state == STATE_RUNNING
    )


def load_memory_records(run_dir: str | Path) -> list[dict[str, Any]]:
    manifest = load_manifest(run_dir, allow_incomplete=True)
    return _read_jsonl(
        Path(run_dir) / MEMORY_FILE, tolerate_torn_tail=manifest.state == STATE_RUNNING
    )

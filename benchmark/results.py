"""Versioned result records (docs/DESIGN.md §2.10/§4.5): a run directory holds a
`manifest.json` (run id, bundle/profile identity and hashes, replay command,
environment/provenance, counts) and a `cases.jsonl` per-case result file. Runs
are append-only artifacts and are never silently overwritten: `save_run` always
writes under a freshly generated run id and refuses if that directory already
exists.

Every `CaseRecord` here is the runner's own **independent** re-evaluation of a
solver's submitted plan (docs/DESIGN.md §4.4 Run flow: "independently evaluate
complete plans"; §2.5: "Solver-reported output is retained only as a
diagnostic."). `benchmark.runner` is responsible for producing `CaseRecord`s
this way; nothing here trusts a solver's self-reported status/output beyond the
`solver_reported_*` diagnostic fields.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmark.profile import RunProfile
from routing.algorithms.base import SolveStatus
from routing.evaluator import Evaluation
from snapshot.bundle import sha256_bytes, sha256_file
from snapshot.models import SnapshotBundle

RESULT_SCHEMA_VERSION = 1
MANIFEST_FILE = "manifest.json"
CASES_FILE = "cases.jsonl"


class ResultError(ValueError):
    """A run directory could not be created or read as expected."""


def new_run_id(*, now: datetime | None = None) -> str:
    """`<UTC timestamp>-<random suffix>`: collision-resistant even across two
    runs started in the same second (docs/DESIGN.md §2.10: "Re-running with the
    same bundle/config cannot silently overwrite an existing run"; §4.5:
    "explicit new run IDs are sufficient")."""
    ts = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{ts}-{secrets.token_hex(4)}"


@dataclass(frozen=True)
class CaseRecord:
    """The canonical, independently-evaluated per-case record persisted by a
    run. `status`/`evaluation`/`score` always come from a fresh `evaluate()`
    call the runner made against the original bundle -- never from the
    solver's own claims. `solver_reported_*` fields are retained purely as a
    diagnostic (docs/DESIGN.md §2.5)."""

    case_id: str
    algorithm: str
    status: SolveStatus
    evaluation: Evaluation | None
    score: int | None
    candidates_considered: int
    error: str | None
    solver_reported_status: SolveStatus
    solver_reported_gross_output: int | None
    solver_reported_score: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "algorithm": self.algorithm,
            "status": self.status.value,
            "evaluation": self.evaluation.to_dict() if self.evaluation is not None else None,
            "score": None if self.score is None else str(self.score),
            "candidates_considered": self.candidates_considered,
            "error": self.error,
            # Diagnostic only -- never the source of `status`/`evaluation`/`score`
            # above (docs/DESIGN.md §2.5: "Solver-reported output is retained
            # only as a diagnostic.").
            "solver_reported": {
                "status": self.solver_reported_status.value,
                "gross_output": (
                    None
                    if self.solver_reported_gross_output is None
                    else str(self.solver_reported_gross_output)
                ),
                "score": (
                    None if self.solver_reported_score is None else str(self.solver_reported_score)
                ),
            },
        }


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
    root = repo_root or Path(__file__).resolve().parent.parent
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


def _profile_sha256(profile_path: str) -> str | None:
    path = Path(profile_path)
    if not path.is_file():
        return None
    return sha256_file(path)


def _resolved_profile(profile: RunProfile) -> dict[str, Any]:
    """Every profile value, including inherited defaults (docs/DESIGN.md §2.12:
    "All profile values appear in output even if inherited from defaults.")."""
    return {
        "schema_version": profile.schema_version,
        "algorithms": list(profile.algorithms),
        "objective": {"mode": profile.objective.mode, "fixed_cost": profile.objective.fixed_cost},
    }


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    schema_version: int
    bundle_id: str
    bundle_hash: str
    profile_path: str
    profile_sha256: str | None
    resolved_profile: dict[str, Any]
    objective_label: str
    algorithms: tuple[str, ...]
    created_at: str
    replay_command: str
    case_count: int
    cases_sha256: str
    run_dir: str
    git_revision: str | None
    git_dirty: bool | None
    git_diff_sha256: str | None
    python_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "bundle_hash": self.bundle_hash,
            "profile_path": self.profile_path,
            "profile_sha256": self.profile_sha256,
            "resolved_profile": self.resolved_profile,
            "objective_label": self.objective_label,
            "algorithms": list(self.algorithms),
            "created_at": self.created_at,
            "replay_command": self.replay_command,
            "case_count": self.case_count,
            "cases_file": CASES_FILE,
            "cases_sha256": self.cases_sha256,
            "environment": {
                "git_revision": self.git_revision,
                "git_dirty": self.git_dirty,
                "git_diff_sha256": self.git_diff_sha256,
                "python_version": self.python_version,
            },
        }


def save_run(
    results_dir: str | Path,
    *,
    bundle: SnapshotBundle,
    profile: RunProfile,
    results: list[CaseRecord],
    replay_command: str,
    run_id: str | None = None,
) -> RunManifest:
    results_dir = Path(results_dir)
    run_id = run_id or new_run_id()
    run_dir = results_dir / run_id
    if run_dir.exists():
        raise ResultError(f"{run_dir}: a run with id {run_id!r} already exists")

    run_dir.mkdir(parents=True)
    cases_path = run_dir / CASES_FILE
    lines = [json.dumps(r.to_dict(), sort_keys=True) for r in results]
    cases_text = "\n".join(lines) + ("\n" if lines else "")
    cases_path.write_text(cases_text, encoding="utf-8")

    git_revision, git_dirty, git_diff_sha256 = git_provenance()

    manifest = RunManifest(
        run_id=run_id,
        schema_version=RESULT_SCHEMA_VERSION,
        bundle_id=bundle.bundle_id,
        bundle_hash=bundle.bundle_hash,
        profile_path=profile.source_path,
        profile_sha256=_profile_sha256(profile.source_path),
        resolved_profile=_resolved_profile(profile),
        objective_label=profile.objective.label,
        algorithms=profile.algorithms,
        created_at=datetime.now(UTC).isoformat(),
        replay_command=replay_command,
        case_count=len(results),
        cases_sha256=sha256_bytes(cases_text.encode("utf-8")),
        run_dir=str(run_dir),
        git_revision=git_revision,
        git_dirty=git_dirty,
        git_diff_sha256=git_diff_sha256,
        python_version=sys.version.split()[0],
    )
    manifest_path = run_dir / MANIFEST_FILE
    manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n")
    return manifest


def load_manifest(run_dir: str | Path) -> RunManifest:
    run_dir = Path(run_dir)
    manifest_path = run_dir / MANIFEST_FILE
    if not manifest_path.is_file():
        raise ResultError(f"{run_dir}: no {MANIFEST_FILE} found")
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases_path = run_dir / CASES_FILE
    observed = sha256_file(cases_path) if cases_path.is_file() else None
    if observed != raw.get("cases_sha256"):
        raise ResultError(f"{run_dir}: {CASES_FILE} checksum does not match manifest")
    env = raw.get("environment", {})
    return RunManifest(
        run_id=raw["run_id"],
        schema_version=raw["schema_version"],
        bundle_id=raw["bundle_id"],
        bundle_hash=raw["bundle_hash"],
        profile_path=raw["profile_path"],
        profile_sha256=raw.get("profile_sha256"),
        resolved_profile=raw.get("resolved_profile", {}),
        objective_label=raw["objective_label"],
        algorithms=tuple(raw["algorithms"]),
        created_at=raw["created_at"],
        replay_command=raw["replay_command"],
        case_count=raw["case_count"],
        cases_sha256=raw["cases_sha256"],
        run_dir=str(run_dir),
        git_revision=env.get("git_revision"),
        git_dirty=env.get("git_dirty"),
        git_diff_sha256=env.get("git_diff_sha256"),
        python_version=env.get("python_version", ""),
    )

"""Versioned result records (docs/DESIGN.md §2.10/§4.5): a run directory holds a
`manifest.json` (run id, bundle/profile identity and hashes, replay command,
counts) and a `cases.jsonl` per-case result file. Runs are append-only artifacts
and are never silently overwritten: `save_run` always writes under a freshly
generated run id and refuses if that directory already exists.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmark.objective import ObjectiveContext
from routing.algorithms.base import SolveResult
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
class RunManifest:
    run_id: str
    schema_version: int
    bundle_id: str
    bundle_hash: str
    profile_path: str
    objective_label: str
    algorithms: tuple[str, ...]
    created_at: str
    replay_command: str
    case_count: int
    cases_sha256: str
    run_dir: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "bundle_hash": self.bundle_hash,
            "profile_path": self.profile_path,
            "objective_label": self.objective_label,
            "algorithms": list(self.algorithms),
            "created_at": self.created_at,
            "replay_command": self.replay_command,
            "case_count": self.case_count,
            "cases_file": CASES_FILE,
            "cases_sha256": self.cases_sha256,
        }


def save_run(
    results_dir: str | Path,
    *,
    bundle: SnapshotBundle,
    profile_path: str,
    objective: ObjectiveContext,
    algorithms: tuple[str, ...],
    results: list[SolveResult],
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

    manifest = RunManifest(
        run_id=run_id,
        schema_version=RESULT_SCHEMA_VERSION,
        bundle_id=bundle.bundle_id,
        bundle_hash=bundle.bundle_hash,
        profile_path=profile_path,
        objective_label=objective.label,
        algorithms=algorithms,
        created_at=datetime.now(UTC).isoformat(),
        replay_command=replay_command,
        case_count=len(results),
        cases_sha256=sha256_bytes(cases_text.encode("utf-8")),
        run_dir=str(run_dir),
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
    return RunManifest(
        run_id=raw["run_id"],
        schema_version=raw["schema_version"],
        bundle_id=raw["bundle_id"],
        bundle_hash=raw["bundle_hash"],
        profile_path=raw["profile_path"],
        objective_label=raw["objective_label"],
        algorithms=tuple(raw["algorithms"]),
        created_at=raw["created_at"],
        replay_command=raw["replay_command"],
        case_count=raw["case_count"],
        cases_sha256=raw["cases_sha256"],
        run_dir=str(run_dir),
    )

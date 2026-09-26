"""The immutable final experiment manifest of the acceptance runs (WHI-1447 / I21;
docs/DESIGN.md §2.2: "The final experiment manifest binds the unchanged corpus hash,
objective, price-context hash and cost-model hash"; §2.10, §2.11).

`compose_manifest` reads only saved, complete runs (plus the checked-in bundle record
and rendered reports) and binds, per labeled run: its run/bundle/profile identities and
hashes, the parent corpus hash of its declared cut, the experiment identity (bundle hash
+ objective with cost-model and price-context hashes), the git revision it ran at, its
full-schedule status counts, the measured wall time and the recorded environment. The
declared order checks (`benchmark.runner.compare_runs`) are re-run and their mismatch
counts recorded; every rendered report file is hashed.

`write_manifest` is write-once: an existing file with different bytes is refused, so a
later experiment is a new file, never an in-place rewrite.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from benchmark.results import MANIFEST_FILE, ResultError, load_manifest
from benchmark.runner import compare_runs

ACCEPTANCE_SCHEMA = "acceptance-manifest/1"


class AcceptanceError(ValueError):
    """The acceptance manifest cannot be composed as asked."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_labeled(items: Sequence[str], *, what: str) -> list[tuple[str, str]]:
    """`["F1=data/results/x", ...]` -> `[("F1", "data/results/x"), ...]`; labels unique."""
    out: list[tuple[str, str]] = []
    for item in items:
        label, sep, value = item.partition("=")
        if not sep or not label.strip() or not value.strip():
            raise AcceptanceError(f"{what} {item!r}: expected LABEL=VALUE")
        out.append((label.strip(), value.strip()))
    labels = [label for label, _ in out]
    if len(set(labels)) != len(labels):
        raise AcceptanceError(f"{what}: duplicate labels in {labels}")
    return out


def run_entry(label: str, run_dir: str | Path, bundles: Mapping[str, Any]) -> dict[str, Any]:
    manifest = load_manifest(run_dir)  # refuses an incomplete run
    by_hash = {b["bundle_hash"]: (name, b) for name, b in bundles.get("bundles", {}).items()}
    if manifest.bundle_hash not in by_hash:
        raise AcceptanceError(
            f"{label}: bundle {manifest.bundle_hash} is not a recorded acceptance bundle"
        )
    bundle_name, bundle = by_hash[manifest.bundle_hash]
    profile = manifest.resolved_profile
    timing = manifest.timing
    return {
        "label": label,
        "run_id": manifest.run_id,
        "state": manifest.state,
        "manifest_sha256": _sha256_file(Path(run_dir) / MANIFEST_FILE),
        "cases_sha256": manifest.cases_sha256,
        "memory_sha256": manifest.memory_sha256,
        "replay_command": manifest.replay_command,
        "bundle": {
            "name": bundle_name,
            "bundle_id": manifest.bundle_id,
            "bundle_hash": manifest.bundle_hash,
            "cohort": bundle.get("cohort"),
            "split": bundle.get("split"),
            "cases": bundle.get("cases"),
            "pools": bundle.get("pools"),
            "subset_of": bundle.get("subset_of"),
            "corpus_bundle_hash": bundles.get("source", {}).get("bundle_hash"),
        },
        "profile": {"path": manifest.profile_path, "sha256": manifest.profile_sha256},
        "objective": profile.get("objective"),
        "experiment": manifest.experiment,
        "search": profile.get("search"),
        "graph": profile.get("graph"),
        "budget": profile.get("budget"),
        "measurement": {k: v for k, v in manifest.measurement.items() if k not in ("case_order",)},
        "algorithms": list(manifest.algorithms),
        "scheduled_count": manifest.scheduled_count,
        "case_count": manifest.case_count,
        "status_counts": manifest.status_counts,
        "created_at": manifest.created_at,
        "finished_at": manifest.finished_at,
        "wall_seconds": {
            "total": timing.get("total_seconds"),
            "timing_pass": timing.get("timing_pass_seconds"),
            "memory_pass": timing.get("memory_pass_seconds"),
        },
        "solve_seconds_total": {
            name: entry.get("solve_seconds_total")
            for name, entry in sorted(timing.get("per_algorithm", {}).items())
        },
        "git": {
            "revision": manifest.environment.get("git_revision"),
            "dirty": manifest.environment.get("git_dirty"),
            "diff_sha256": manifest.environment.get("git_diff_sha256"),
        },
        "environment": manifest.environment,
    }


def compose_manifest(
    runs: Sequence[tuple[str, str]],
    *,
    order_checks: Sequence[tuple[str, str]] = (),
    reports: Sequence[str | Path] = (),
    bundles_record: str | Path,
    notes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """`runs`: (label, run dir); `order_checks`: (label, "RUN_LABEL_A,RUN_LABEL_B")."""
    bundles_path = Path(bundles_record)
    bundles = json.loads(bundles_path.read_text(encoding="utf-8"))
    entries = [run_entry(label, run_dir, bundles) for label, run_dir in runs]
    by_label = dict(runs)
    revisions = {e["git"]["revision"] for e in entries}
    dirty = [e["label"] for e in entries if e["git"]["dirty"]]
    checks = []
    for label, pair in order_checks:
        names = [p.strip() for p in pair.split(",")]
        if len(names) != 2 or any(n not in by_label for n in names):
            raise AcceptanceError(f"order check {label}: expected two run labels, got {pair!r}")
        mismatches = compare_runs(by_label[names[0]], by_label[names[1]])
        a, b = (next(e for e in entries if e["label"] == n) for n in names)
        checks.append(
            {
                "label": label,
                "runs": names,
                "orders": [a["measurement"].get("order"), b["measurement"].get("order")],
                "compared_records": a["case_count"],
                "mismatches": len(mismatches),
                "mismatch_examples": mismatches[:10],
            }
        )
    report_files = []
    for report_dir in reports:
        for path in sorted(Path(report_dir).iterdir()):
            if path.is_file():
                report_files.append({"path": path.as_posix(), "sha256": _sha256_file(path)})
    return {
        "schema": ACCEPTANCE_SCHEMA,
        "corpus": bundles.get("source"),
        "bundles_record": {"path": bundles_path.as_posix(), "sha256": _sha256_file(bundles_path)},
        "git_revisions": sorted(r for r in revisions if r),
        "dirty_runs": dirty,
        "runs": entries,
        "order_checks": checks,
        "reports": report_files,
        "notes": dict(notes or {}),
    }


def write_manifest(document: Mapping[str, Any], path: str | Path) -> str:
    """Write once; returns the SHA-256. Refuses to replace different existing bytes."""
    data = (json.dumps(document, indent=1, sort_keys=True) + "\n").encode("utf-8")
    target = Path(path)
    if target.exists() and target.read_bytes() != data:
        raise AcceptanceError(f"{target}: exists with different content (write a new file)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


__all__ = [
    "ACCEPTANCE_SCHEMA",
    "AcceptanceError",
    "ResultError",
    "compose_manifest",
    "parse_labeled",
    "run_entry",
    "write_manifest",
]

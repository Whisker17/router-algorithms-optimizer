"""WHI-1449 Metis-inspired challenge: the gate-S2 diagnostic pass and the X7 ablation check
(docs/references/jupiter-metis-challenge.md §10.5-10.6). Not a measured run and not part
of the runner: the timed arms run through `main.py run --strategies profile` with the
profiles under `config/metis_challenge/`. Everything here is a separate, explicitly labeled
correctness pass with its own caches and counters; nothing it does enters a measured
solve's latency, quote count or budget, and no value is tuned on its output.

**Case selection.** Every case of the bundle in bundle order, or an explicit subset
(`--case`, repeatable and duplicate-free, then `--first N` with N >= 1). An empty bundle,
an empty selection, an unknown or duplicate case id is refused before any work: there is
no empty pass. A subset is recorded as `subset_smoke` and can never establish gate S2.

**Evidence.** Either both arms are re-solved in-process under the profile budget
(**diagnostic re-solves, not measurements**) or both come from measured runs
(`--metis-run` and `--reference-run`, always together). A measured run is accepted only if
its manifest loads strictly (`benchmark.results.load_manifest`: complete, cases checksum),
it is over the selected bundle (`bundle_hash`), it ran exactly the arm's algorithm, its
persisted effective settings (`resolved_profile`: objective, budget, search, graph,
measurement, algorithm config and provenance) equal the arm profile's as loaded here, its
measurement budget and case order cover every bundle case exactly once, it holds exactly
one record per bundle case for that algorithm (no duplicate, missing, unknown or wrong-arm
record) and it was produced at the same clean git revision this pass runs at (which must be
clean too). The run ids, manifest and cases hashes and the runs' own profile identities are
recorded in the output.

For each selected case:

1. `metis_inspired.diagnose_case` replays the label trajectory of the metis profile
   (normally M3) and re-scores the exhaustive per-chunk enumeration maximum on the
   identical committed state and carried amount; every disagreeing chunk is attributed to
   a memo §9.3 class or reported `unattributed`.
2. The case verdict, from the two arms' statuses, plans and counters:
   - `solver_failure`: either arm is `algorithm_error`, `invalid_plan`, `model_error`,
     `unsupported` or `cancelled` -- a genuine failure, never relabeled;
   - `evidence_unavailable`: a required status, score, trace, search counter or trajectory
     field is missing or malformed, or a `timeout` carries no declared budget evidence --
     never read as zero or as an empty plan;
   - `unexplained`: an unattributed chunk, a broken quote subset, a diagnostic trajectory
     that differs from the metis solve, differing statuses or plans without an attributed
     divergent chunk, or more executed quotes than the reference with every chunk agreeing;
   - `budget_order` (class 3): a declared budget truncated either arm (`truncated_by`
     `max_quotes` / `max_candidates`, or the runner's hard `limit_hit` `time` / `quotes`);
     the diagnostic's own findings still take precedence;
   - `identical`: same status and plan (a valid `no_route` / `incomplete_snapshot` pair
     included); with every chunk agreeing the quote bound is checked on actual counters;
   - `attributed`: different plans, every divergent chunk in an allowed class.
3. `--x7`: `metis_inspired` with `label_pruning: false` and `label_hops == max_hops` must
   equal `incremental_graph` (plan, evaluation, statuses, every logical counter).

`s2_gate.established` is true only for a full pass over every bundle case with measured
evidence and no `unexplained`, `evidence_unavailable` or `solver_failure` verdict at
`label_hops <= 3`. Exit status: 2 for refused inputs (nothing written), 1 for any such
verdict or an X7 difference, else 0.

    uv run python tools/metis_challenge.py diagnose \\
        --bundle <bundle_tuning> --profile config/metis_challenge/m3.yaml \\
        --reference-profile config/metis_challenge/a0.yaml \\
        --metis-run <M3 run dir> --reference-run <A0 run dir> \\
        --output data/metis_challenge/<id>/s2.json
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:  # direct `python tools/metis_challenge.py`
    sys.path.insert(0, str(REPO))

from benchmark.profile import RunProfile, load_profile  # noqa: E402
from benchmark.results import (  # noqa: E402
    MANIFEST_FILE,
    ResultError,
    git_provenance,
    load_case_records,
    load_manifest,
)
from routing.algorithms import incremental_graph, metis_inspired  # noqa: E402
from routing.algorithms.base import AlgorithmConfig, SolveContext, SolveResult  # noqa: E402
from routing.algorithms.registry import get_algorithm  # noqa: E402
from snapshot.bundle import load_bundle, sha256_file  # noqa: E402
from snapshot.models import Case, SnapshotBundle  # noqa: E402

DOMAIN_STATUSES = frozenset({"ok", "no_route", "incomplete_snapshot"})
FAILURE_STATUSES = frozenset(
    {"algorithm_error", "invalid_plan", "model_error", "unsupported", "cancelled"}
)
DECLARED_BUDGETS = frozenset({"max_quotes", "max_candidates"})
RUNNER_LIMITS = frozenset({"time", "quotes"})
NOT_PROVEN = frozenset({"unexplained", "evidence_unavailable", "solver_failure"})


class ChallengeInputError(ValueError):
    """A refused input: nothing is diagnosed or written."""


def _source_identity() -> dict[str, Any]:
    """The code this pass runs (the project's own git provenance seam)."""
    revision, dirty, diff_sha256 = git_provenance(REPO)
    return {"git_revision": revision, "git_dirty": dirty, "git_diff_sha256": diff_sha256}


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _prepare(
    bundle: SnapshotBundle, profile: RunProfile, params: dict[str, Any] | None = None
) -> Any:
    factory = get_algorithm(profile.algorithms[0])
    config = profile.algorithm_config(factory)
    if params:
        config = AlgorithmConfig(config.name, {**config.params, **params})
    assert factory.prepare is not None
    return factory, factory.prepare(bundle, config)


# ------------------------------------------------------------------ selection


def select_cases(bundle: SnapshotBundle, case_ids: list[str], first: int | None) -> list[Case]:
    if not bundle.cases:
        raise ChallengeInputError(f"bundle {bundle.bundle_id!r} has no cases")
    if first is not None and first < 1:
        raise ChallengeInputError(f"--first must be >= 1, got {first}")
    duplicates = sorted(c for c, n in Counter(case_ids).items() if n > 1)
    if duplicates:
        raise ChallengeInputError(f"duplicate --case ids: {duplicates}")
    known = {c.case_id for c in bundle.cases}
    unknown = [c for c in case_ids if c not in known]
    if unknown:
        raise ChallengeInputError(f"unknown case ids for bundle {bundle.bundle_id!r}: {unknown}")
    cases = [bundle.case(c) for c in case_ids] if case_ids else list(bundle.cases)
    if first is not None:
        cases = cases[:first]
    if not cases:  # pragma: no cover - excluded by the checks above
        raise ChallengeInputError("empty case selection")
    return cases


# ------------------------------------------------------------------ measured evidence


def measured_run(
    run_dir: str | Path,
    *,
    arm: str,
    algorithm: str,
    profile: RunProfile,
    bundle: SnapshotBundle,
    source: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """The validated identity of one measured arm run and its records by case id (see
    the module docstring); any mismatch is a `ChallengeInputError`."""
    where = f"--{arm}-run {run_dir}"
    try:
        manifest = load_manifest(run_dir)  # strict: complete, cases checksum verified
        records = load_case_records(run_dir)
    except (ResultError, OSError, ValueError) as exc:
        raise ChallengeInputError(f"{where}: {exc}") from exc
    problems: list[str] = []
    if manifest.bundle_hash != bundle.bundle_hash:
        problems.append(f"bundle_hash {manifest.bundle_hash} != selected {bundle.bundle_hash}")
    if manifest.algorithms != (algorithm,):
        problems.append(f"algorithms {list(manifest.algorithms)} != [{algorithm!r}]")
    bound = dataclasses.replace(profile, objective=profile.objective.bind(bundle))
    expected = json.loads(json.dumps(bound.resolved()))
    if manifest.resolved_profile != expected:
        keys = sorted(
            k
            for k in set(expected) | set(manifest.resolved_profile)
            if expected.get(k) != manifest.resolved_profile.get(k)
        )
        problems.append(f"effective settings differ from {profile.source_path}: {keys}")
    if manifest.measurement.get("budget") != profile.budget.to_dict():
        problems.append("measurement budget differs from the profile budget")
    bundle_ids = [c.case_id for c in bundle.cases]
    order = manifest.measurement.get("case_order")
    if not isinstance(order, list) or sorted(order) != sorted(bundle_ids):
        problems.append("case_order does not cover every bundle case exactly once")
    if manifest.scheduled_count != len(bundle_ids) or manifest.case_count != len(bundle_ids):
        problems.append(
            f"scheduled/recorded {manifest.scheduled_count}/{manifest.case_count} "
            f"!= {len(bundle_ids)} bundle cases"
        )
    if manifest.git_revision is None or manifest.git_revision != source["git_revision"]:
        problems.append(
            f"git_revision {manifest.git_revision} != this pass's {source['git_revision']}"
        )
    if manifest.git_dirty is not False:
        problems.append(f"run code was not clean (git_dirty={manifest.git_dirty})")
    if source["git_dirty"] is not False:
        problems.append("this diagnostic pass does not run on a clean checkout")
    by_case: dict[str, dict[str, Any]] = {}
    known = set(bundle_ids)
    for record in records:
        case_id, name = record.get("case_id"), record.get("algorithm")
        if name != algorithm:
            problems.append(f"record for {case_id!r} is algorithm {name!r}, not {algorithm!r}")
        elif case_id not in known:
            problems.append(f"record for unknown case {case_id!r}")
        elif case_id in by_case:
            problems.append(f"duplicate record for case {case_id!r}")
        else:
            by_case[str(case_id)] = record
    missing = [c for c in bundle_ids if c not in by_case]
    if missing:
        problems.append(f"no record for {len(missing)} case(s), e.g. {missing[:3]}")
    if problems:
        raise ChallengeInputError(f"{where}: " + "; ".join(problems))
    identity = {
        "arm": arm,
        "run_id": manifest.run_id,
        "run_dir": str(run_dir),
        "manifest_sha256": sha256_file(Path(run_dir) / MANIFEST_FILE),
        "cases_sha256": manifest.cases_sha256,
        "state": manifest.state,
        "bundle_hash": manifest.bundle_hash,
        "algorithms": list(manifest.algorithms),
        "profile_path": manifest.profile_path,
        "profile_sha256": manifest.profile_sha256,
        "resolved_profile_sha256": _json_sha256(manifest.resolved_profile),
        "replay_command": manifest.replay_command,
        "git_revision": manifest.git_revision,
        "git_dirty": manifest.git_dirty,
        "case_count": manifest.case_count,
        "status_counts": dict(manifest.status_counts),
        "order": manifest.measurement.get("order"),
    }
    return identity, by_case


# ------------------------------------------------------------------ one arm's outcome


def _trace(evaluation: Any) -> list[list[str]] | None:
    """The executed plan (per step: pool, direction, amounts), or `None` if the
    evaluation or its trace is missing or malformed."""
    if not isinstance(evaluation, dict) or not isinstance(evaluation.get("trace"), list):
        return None
    try:
        return [
            [t["pool_id"], t["token_in"], t["token_out"], str(t["amount_in"]), str(t["amount_out"])]
            for t in evaluation["trace"]
        ]
    except (KeyError, TypeError):
        return None


def side_from_result(result: SolveResult) -> dict[str, Any]:
    evaluation = result.evaluation.to_dict() if result.evaluation is not None else None
    return {
        "status": result.status.value,
        "score": None if result.score is None else str(result.score),
        "trace": _trace(evaluation),
        "limit_hit": None,
        "search": json.loads(json.dumps(dict(result.search_stats))),
    }


def side_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": record.get("status"),
        "score": record.get("score"),
        "trace": _trace(record.get("evaluation")),
        "limit_hit": record.get("limit_hit"),
        "search": record.get("search"),
    }


def _budget(side: dict[str, Any]) -> str | None:
    """The declared budget that truncated this arm, if any."""
    if side["status"] == "timeout" and side["limit_hit"] in RUNNER_LIMITS:
        return f"runner:{side['limit_hit']}"
    search = side["search"]
    truncated = search.get("truncated_by") if isinstance(search, dict) else None
    return f"declared:{truncated}" if truncated in DECLARED_BUDGETS else None


def _missing(side: dict[str, Any], *, metis: bool) -> list[str]:
    """The evidence a returned solve must carry that this arm lacks."""
    status = side["status"]
    if not isinstance(status, str):
        return ["status"]
    if status in FAILURE_STATUSES or (status == "timeout" and side["limit_hit"] in RUNNER_LIMITS):
        return []  # an outcome without a returned search (or a genuine failure)
    missing: list[str] = []
    if status == "ok":
        if side["score"] is None:
            missing.append("score")
        if not side["trace"]:
            missing.append("evaluation.trace")
    elif status not in DOMAIN_STATUSES and status != "timeout":
        missing.append(f"status {status!r} is not a known outcome")
    search = side["search"]
    if not isinstance(search, dict) or not search:
        return [*missing, "search"]
    if not (isinstance(search.get("quotes_executed"), int)):
        missing.append("search.quotes_executed")
    if "truncated_by" not in search:
        missing.append("search.truncated_by")
    if not isinstance(search.get("incremental_status"), str):
        missing.append("search.incremental_status")
    if metis:
        for key in ("incremental_allocation", "incremental_chunk_sequence"):
            if not isinstance(search.get(key), list):
                missing.append(f"search.{key}")
        if not isinstance(search.get("label_relaxations"), int):
            missing.append("search.label_relaxations")
    elif not isinstance(search.get("paths_scored"), int):
        missing.append("search.paths_scored")
    if status == "timeout" and _budget(side) is None:
        missing.append("timeout without declared budget evidence (truncated_by / limit_hit)")
    return missing


def _summary(side: dict[str, Any], *, metis: bool) -> dict[str, Any]:
    search = side["search"] if isinstance(side["search"], dict) else {}
    work = "label_relaxations" if metis else "paths_scored"
    return {
        "status": side["status"],
        "score": side["score"],
        "trace": side["trace"],
        "limit_hit": side["limit_hit"],
        "budget": _budget(side),
        "quotes_executed": search.get("quotes_executed"),
        work: search.get(work),
        "truncated_by": search.get("truncated_by"),
        "incremental_status": search.get("incremental_status"),
    }


def _trajectory_finding(diag: dict[str, Any], search: dict[str, Any]) -> str | None:
    """Whether the diagnostic replayed the trajectory the metis solve committed."""
    status = search["incremental_status"]
    if status in ("ok", "invalid_plan"):  # the chunk loop completed
        if diag["trajectory_status"] != "ok" or (
            diag["incremental_allocation"] != search["incremental_allocation"]
            or diag["incremental_chunk_sequence"] != search["incremental_chunk_sequence"]
        ):
            return "diagnostic trajectory differs from the metis solve"
    elif status == "no_paths" or status.startswith("chunk_"):
        if diag["trajectory_status"] != status:
            return f"diagnostic trajectory {diag['trajectory_status']!r} != solve {status!r}"
    elif status != "truncated":
        return f"unknown incremental_status {status!r}"
    return None


def verdict(
    diag: dict[str, Any], metis: dict[str, Any], ref: dict[str, Any], results_from: str
) -> dict[str, Any]:
    """The case verdict (module docstring, step 2)."""
    findings: list[str] = []
    missing = {"metis": _missing(metis, metis=True), "reference": _missing(ref, metis=False)}
    budgets = [b for b in (_budget(metis), _budget(ref)) if b]
    failed = [s["status"] for s in (metis, ref) if s["status"] in FAILURE_STATUSES]
    quotes_ok: bool | None = None
    same_plan: bool | None = None
    if diag["unexplained_chunks"]:
        findings.append(f"{diag['unexplained_chunks']} unattributed chunk(s)")
    if diag["quote_subset_violations"]:
        findings.append(f"{diag['quote_subset_violations']} chunk(s) break the quote subset")
    if failed:
        result = "solver_failure"
        findings.append(f"failed statuses: {failed}")
    elif missing["metis"] or missing["reference"]:
        result = "evidence_unavailable"
    elif findings:
        result = "unexplained"
    elif budgets:  # class 3; the unbudgeted diagnostic trajectory is not comparable
        result = "budget_order"
    else:
        trajectory = _trajectory_finding(diag, metis["search"])
        if trajectory:
            findings.append(trajectory)
        same_plan = (metis["status"], metis["score"], metis["trace"]) == (
            ref["status"],
            ref["score"],
            ref["trace"],
        )
        if metis["status"] != ref["status"]:
            findings.append(f"statuses differ: {metis['status']} vs {ref['status']}")
        if diag["divergent_chunks"] == 0:
            quotes_ok = metis["search"]["quotes_executed"] <= ref["search"]["quotes_executed"]
            if not quotes_ok:
                findings.append("quotes_executed(metis) > quotes_executed(reference)")
            if not same_plan:
                findings.append("every chunk agrees but the plans differ")
        if findings:
            result = "unexplained"
        elif same_plan:
            result = "identical"
        else:
            result = "attributed"
    return {
        "verdict": result,
        "findings": findings,
        "evidence_missing": missing,
        "budgets": budgets,
        "same_plan": same_plan,
        "quotes_within_reference": quotes_ok,
        "results_from": results_from,
        "metis": _summary(metis, metis=True),
        "reference": _summary(ref, metis=False),
    }


# ------------------------------------------------------------------ X7


def _normalized(result: SolveResult) -> dict[str, Any]:
    own = {"chunk_search", "candidate_unit", "chunk_path_hops"}
    stats = {
        k: v for k, v in result.search_stats.items() if not k.startswith("label_") and k not in own
    }
    if stats.get("chosen_source") == metis_inspired.NAME:
        stats["chosen_source"] = incremental_graph.NAME
    stats["truncated_stages"] = [
        incremental_graph.NAME if s == metis_inspired.NAME else s
        for s in stats.get("truncated_stages", [])
    ]
    fields = {f.name: getattr(result, f.name) for f in dataclasses.fields(SolveResult)}
    return {**fields, "algorithm": incremental_graph.NAME, "search_stats": stats}


def _x7(
    bundle: SnapshotBundle, case: Case, profile: RunProfile, reference: RunProfile
) -> list[str]:
    """Differences between the disabled mechanism at label_hops == max_hops and
    `incremental_graph` (empty = identical)."""
    max_hops = profile.search["max_hops"]
    factory, prepared = _prepare(bundle, profile, {"label_hops": max_hops, "label_pruning": False})
    ref_factory, ref_prepared = _prepare(bundle, reference)
    got = factory.solve(case, SolveContext(bundle, profile.objective, prepared), profile.budget)
    ref = ref_factory.solve(
        case, SolveContext(bundle, reference.objective, ref_prepared), reference.budget
    )
    a = _normalized(got)
    b = {f.name: getattr(ref, f.name) for f in dataclasses.fields(SolveResult)}
    b["search_stats"] = dict(ref.search_stats)
    diffs = [k for k in a if a[k] != b[k] and k != "search_stats"]
    diffs += [
        f"search.{k}"
        for k in set(a["search_stats"]) | set(b["search_stats"])
        if a["search_stats"].get(k) != b["search_stats"].get(k)
    ]
    return sorted(diffs)


# ------------------------------------------------------------------ command


def _profile_identity(path: str, profile: RunProfile) -> dict[str, Any]:
    return {
        "path": path,
        "sha256": sha256_file(Path(path)),
        "resolved_profile_sha256": _json_sha256(json.loads(json.dumps(profile.resolved()))),
    }


def cmd_diagnose(args: argparse.Namespace) -> int:
    try:
        return _diagnose(args)
    except ChallengeInputError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


def _diagnose(args: argparse.Namespace) -> int:
    out_path = Path(args.output)
    if out_path.exists():
        raise ChallengeInputError(f"refusing to overwrite {out_path}")
    if bool(args.metis_run) != bool(args.reference_run):
        raise ChallengeInputError(
            "--metis-run and --reference-run must be given together (no mixed evidence)"
        )
    bundle = load_bundle(args.bundle)
    profile, reference = load_profile(args.profile), load_profile(args.reference_profile)
    if profile.algorithms != (metis_inspired.NAME,):
        raise ChallengeInputError(f"{args.profile}: expected a profile running only metis")
    if reference.algorithms != (incremental_graph.NAME,):
        raise ChallengeInputError(f"{args.reference_profile}: expected only incremental_graph")
    cases = select_cases(bundle, list(args.case), args.first)
    source = _source_identity()
    runs: list[dict[str, Any]] = []
    measured: tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]] | None = None
    if args.metis_run:
        metis_id, metis_records = measured_run(
            args.metis_run,
            arm="metis",
            algorithm=metis_inspired.NAME,
            profile=profile,
            bundle=bundle,
            source=source,
        )
        ref_id, ref_records = measured_run(
            args.reference_run,
            arm="reference",
            algorithm=incremental_graph.NAME,
            profile=reference,
            bundle=bundle,
            source=source,
        )
        runs = [metis_id, ref_id]
        measured = (metis_records, ref_records)
    results_from = "measured run records" if measured else "diagnostic in-process re-solves"
    factory, prepared = _prepare(bundle, profile)
    ref_factory, ref_prepared = _prepare(bundle, reference)
    started = time.perf_counter()
    rows = []
    for case in cases:
        diag = metis_inspired.diagnose_case(case, bundle, prepared)
        if measured is not None:
            metis = side_from_record(measured[0][case.case_id])
            ref = side_from_record(measured[1][case.case_id])
        else:
            ctx = SolveContext(bundle, profile.objective, prepared)
            metis = side_from_result(factory.solve(case, ctx, profile.budget))
            ctx = SolveContext(bundle, reference.objective, ref_prepared)
            ref = side_from_result(ref_factory.solve(case, ctx, reference.budget))
        compact = {k: v for k, v in diag.items() if k != "chunk_records"}
        compact["divergent_chunk_records"] = [
            r for r in diag["chunk_records"] if r["class"] != "agree"
        ]
        row: dict[str, Any] = {
            "case_id": case.case_id,
            **verdict(diag, metis, ref, results_from),
            "diagnostic": diag if args.chunk_records else compact,
        }
        if args.x7:
            row["x7_differences"] = _x7(bundle, case, profile, reference)
        rows.append(row)
        x7 = f" x7={row['x7_differences'] or 'identical'}" if args.x7 else ""
        print(f"{case.case_id}: {row['verdict']} {diag['classes']}{x7}", flush=True)
    verdicts = Counter(row["verdict"] for row in rows)
    full = [c.case_id for c in cases] == [c.case_id for c in bundle.cases]
    reasons = []
    if not full:
        reasons.append("subset selection (smoke), not every bundle case")
    if measured is None:
        reasons.append("in-process re-solves, not measured runs")
    if prepared.label_hops > 3:
        reasons.append("S2 is defined for label_hops <= 3")
    bad = sorted(v for v in verdicts if v in NOT_PROVEN)
    if bad:
        reasons.append(f"verdicts {bad}")
    x7_identical = None if not args.x7 else all(not r["x7_differences"] for r in rows)
    document = {
        "pass": "WHI-1449 S2 diagnostic / X7 check -- separate correctness pass, NOT a measurement",
        "source": source,
        "bundle": {
            "path": str(args.bundle),
            "bundle_id": bundle.bundle_id,
            "bundle_hash": bundle.bundle_hash,
        },
        "profile": _profile_identity(args.profile, profile),
        "reference_profile": _profile_identity(args.reference_profile, reference),
        "results_from": results_from,
        "measured_runs": runs,
        "selection": {
            "scope": "full" if full else "subset_smoke",
            "case_arg": list(args.case),
            "first": args.first,
            "cases_selected": [c.case_id for c in cases],
            "cases_in_bundle": len(bundle.cases),
        },
        "label_hops": prepared.label_hops,
        "allowed_classes": sorted(metis_inspired.allowed_classes(prepared.label_hops)),
        "verdicts": dict(sorted(verdicts.items())),
        "s2_gate": {"established": not reasons, "not_established_because": reasons},
        "x7_identical": x7_identical,
        "diagnostic_wall_seconds": round(time.perf_counter() - started, 3),
        "cases": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: document[k] for k in ("verdicts", "s2_gate", "x7_identical")}))
    return 1 if bad or x7_identical is False else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("diagnose", help="S2 per-chunk diagnostic (+ optional X7 check)")
    d.add_argument("--bundle", required=True, help="frozen bundle directory (read only)")
    d.add_argument("--profile", required=True, help="metis_inspired arm profile (e.g. M3)")
    d.add_argument("--reference-profile", required=True, help="incremental_graph arm (A0)")
    d.add_argument("--case", action="append", default=[], help="case id (repeatable)")
    d.add_argument("--first", type=int, default=None, help="only the first N (>= 1) cases")
    d.add_argument("--metis-run", default=None, help="measured metis run dir (with the next)")
    d.add_argument("--reference-run", default=None, help="measured reference run dir")
    d.add_argument("--x7", action="store_true", help="also check the X7 ablation identity")
    d.add_argument("--chunk-records", action="store_true", help="keep every chunk record")
    d.add_argument("--output", required=True, help="JSON artifact path (write-once)")
    args = parser.parse_args(argv)
    return cmd_diagnose(args)


if __name__ == "__main__":
    raise SystemExit(main())

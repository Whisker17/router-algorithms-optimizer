"""WHI-1449 Metis-inspired challenge: the gate-S2 diagnostic pass and the X7 ablation check
(docs/references/jupiter-metis-challenge.md §10.5-10.6). Not a measured run and not part
of the runner: the timed arms run through `main.py run --strategies profile` with the
profiles under `config/metis_challenge/`. Everything here is a separate, explicitly labeled
correctness pass with its own caches and counters; nothing it does enters a measured
solve's latency, quote count or budget, and no value is tuned on its output.

For each selected case of a bundle (bundle order; `--case` / `--first` bound the pass):

1. `metis_inspired.diagnose_case` replays the label trajectory of the metis profile
   (normally M3) and re-scores the exhaustive per-chunk enumeration maximum on the
   identical committed state and carried amount; every disagreeing chunk is attributed to
   a memo §9.3 class or reported `unattributed`.
2. The metis profile's and the reference profile's (normally A0) solvers are re-solved
   in-process under the profile budget (**diagnostic re-solves, not measurements**), or,
   with `--metis-run` / `--reference-run`, their records are read from the measured runs
   (and the diagnostic trajectory is checked against the measured metis record).
3. The case verdict: `identical` (same plan), `attributed` (different plan, every
   divergent chunk in an allowed class), `budget_order` (a declared budget truncated either
   side: class 3), or `unexplained` (anything else -- stop and investigate). With every
   chunk agreeing, `quotes_executed(metis) <= quotes_executed(reference)` is also checked.
4. `--x7`: `metis_inspired` with `label_pruning: false` and `label_hops == max_hops` must
   equal `incremental_graph` (plan, evaluation, statuses, every logical counter).

    uv run python tools/metis_challenge.py diagnose \\
        --bundle <bundle_tuning> --profile config/metis_challenge/m3.yaml \\
        --reference-profile config/metis_challenge/a0.yaml --first 4 --x7 \\
        --output data/metis_challenge/<id>/s2-smoke.json
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:  # direct `python tools/metis_challenge.py`
    sys.path.insert(0, str(REPO))

from benchmark.profile import RunProfile, load_profile  # noqa: E402
from benchmark.results import load_case_records  # noqa: E402
from routing.algorithms import incremental_graph, metis_inspired  # noqa: E402
from routing.algorithms.base import AlgorithmConfig, SolveContext, SolveResult  # noqa: E402
from routing.algorithms.registry import get_algorithm  # noqa: E402
from snapshot.bundle import load_bundle  # noqa: E402
from snapshot.models import Case, SnapshotBundle  # noqa: E402


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)
    return out.stdout.strip()


def _prepare(
    bundle: SnapshotBundle, profile: RunProfile, params: dict[str, Any] | None = None
) -> Any:
    factory = get_algorithm(profile.algorithms[0])
    config = profile.algorithm_config(factory)
    if params:
        config = AlgorithmConfig(config.name, {**config.params, **params})
    assert factory.prepare is not None
    return factory, factory.prepare(bundle, config)


def _signature(status: str, score: Any, evaluation: Any) -> dict[str, Any]:
    """Status, score and the executed plan (per step: pool, direction, amounts)."""
    trace = []
    if isinstance(evaluation, dict):
        trace = [
            [t["pool_id"], t["token_in"], t["token_out"], str(t["amount_in"]), str(t["amount_out"])]
            for t in evaluation.get("trace", [])
        ]
    return {"status": status, "score": None if score is None else str(score), "trace": trace}


def _from_result(result: SolveResult) -> dict[str, Any]:
    evaluation = result.evaluation.to_dict() if result.evaluation is not None else None
    return {
        "signature": _signature(result.status.value, result.score, evaluation),
        "search": json.loads(json.dumps(dict(result.search_stats))),
    }


def _from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "signature": _signature(record["status"], record["score"], record.get("evaluation")),
        "search": record.get("search") or {},
    }


def _normalized(result: SolveResult) -> dict[str, Any]:
    stats = {
        k: v
        for k, v in result.search_stats.items()
        if not k.startswith("label_")
        and k not in {"chunk_search", "candidate_unit", "chunk_path_hops"}
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


def _verdict(
    diag: dict[str, Any], metis: dict[str, Any], ref: dict[str, Any], measured: bool
) -> dict[str, Any]:
    ms, rs = metis["search"], ref["search"]
    same_plan = metis["signature"] == ref["signature"]
    truncated = bool(ms.get("truncated_by") or rs.get("truncated_by"))
    findings: list[str] = []
    trajectory_checked = ms.get("incremental_status") == "ok"
    if trajectory_checked and (
        diag["incremental_allocation"] != ms.get("incremental_allocation")
        or diag["incremental_chunk_sequence"] != ms.get("incremental_chunk_sequence")
    ):
        findings.append("diagnostic trajectory differs from the metis solve")
    if diag["unexplained_chunks"]:
        findings.append(f"{diag['unexplained_chunks']} unattributed chunk(s)")
    if diag["quote_subset_violations"]:
        findings.append(f"{diag['quote_subset_violations']} chunk(s) break the quote subset")
    quotes_ok: bool | None = None
    if diag["divergent_chunks"] == 0 and not truncated:
        if not same_plan:
            findings.append("every chunk agrees but the plans differ")
        quotes_ok = ms.get("quotes_executed", 0) <= rs.get("quotes_executed", 0)
        if not quotes_ok:
            findings.append("quotes_executed(metis) > quotes_executed(reference)")
    if findings:
        verdict = "unexplained"
    elif same_plan:
        verdict = "identical"
    elif truncated:
        verdict = "budget_order"
    elif diag["divergent_chunks"]:
        verdict = "attributed"
    else:
        verdict = "unexplained"
        findings.append("plans differ without a divergent chunk")
    return {
        "verdict": verdict,
        "findings": findings,
        "same_plan": same_plan,
        "truncated": truncated,
        "trajectory_checked": trajectory_checked,
        "quotes_within_reference": quotes_ok,
        "results_from": "measured run records" if measured else "diagnostic in-process re-solves",
        "metis": {
            **metis["signature"],
            "quotes_executed": ms.get("quotes_executed"),
            "label_relaxations": ms.get("label_relaxations"),
            "truncated_by": ms.get("truncated_by"),
        },
        "reference": {
            **ref["signature"],
            "quotes_executed": rs.get("quotes_executed"),
            "paths_scored": rs.get("paths_scored"),
            "truncated_by": rs.get("truncated_by"),
        },
    }


def cmd_diagnose(args: argparse.Namespace) -> int:
    out_path = Path(args.output)
    if out_path.exists():
        print(f"refusing to overwrite {out_path}", file=sys.stderr)
        return 2
    bundle = load_bundle(args.bundle)
    profile, reference = load_profile(args.profile), load_profile(args.reference_profile)
    if profile.algorithms != (metis_inspired.NAME,):
        raise SystemExit(f"{args.profile}: expected a profile running only {metis_inspired.NAME}")
    if reference.algorithms != (incremental_graph.NAME,):
        raise SystemExit(f"{args.reference_profile}: expected only {incremental_graph.NAME}")
    cases = list(bundle.cases)
    if args.case:
        cases = [bundle.case(c) for c in args.case]
    if args.first is not None:
        cases = cases[: args.first]
    factory, prepared = _prepare(bundle, profile)
    ref_factory, ref_prepared = _prepare(bundle, reference)
    measured_metis = measured_ref = None
    if args.metis_run:
        measured_metis = {
            r["case_id"]: r
            for r in load_case_records(args.metis_run)
            if r["algorithm"] == metis_inspired.NAME
        }
    if args.reference_run:
        measured_ref = {
            r["case_id"]: r
            for r in load_case_records(args.reference_run)
            if r["algorithm"] == incremental_graph.NAME
        }
    started = time.perf_counter()
    rows = []
    for case in cases:
        diag = metis_inspired.diagnose_case(case, bundle, prepared)
        if measured_metis is not None:
            metis = _from_record(measured_metis[case.case_id])
        else:
            ctx = SolveContext(bundle, profile.objective, prepared)
            metis = _from_result(factory.solve(case, ctx, profile.budget))
        if measured_ref is not None:
            ref = _from_record(measured_ref[case.case_id])
        else:
            ctx = SolveContext(bundle, reference.objective, ref_prepared)
            ref = _from_result(ref_factory.solve(case, ctx, reference.budget))
        row: dict[str, Any] = {
            "case_id": case.case_id,
            **_verdict(diag, metis, ref, measured_metis is not None),
            "diagnostic": diag
            if args.chunk_records
            else {k: v for k, v in diag.items() if k != "chunk_records"}
            | {
                "divergent_chunk_records": [
                    r for r in diag["chunk_records"] if r["class"] != "agree"
                ]
            },
        }
        if args.x7:
            row["x7_differences"] = _x7(bundle, case, profile, reference)
        rows.append(row)
        print(
            f"{case.case_id}: {row['verdict']} {row['diagnostic']['classes']}"
            + (f" x7={row['x7_differences'] or 'identical'}" if args.x7 else ""),
            flush=True,
        )
    verdicts: dict[str, int] = {}
    for row in rows:
        verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1
    document = {
        "pass": "WHI-1449 S2 diagnostic / X7 check -- separate correctness pass, NOT a measurement",
        "revision": _git("rev-parse", "HEAD"),
        "dirty": bool(_git("status", "--porcelain")),
        "bundle": {"path": str(args.bundle), "bundle_hash": bundle.bundle_hash},
        "profile": {"path": args.profile, "sha256": _sha256(args.profile)},
        "reference_profile": {
            "path": args.reference_profile,
            "sha256": _sha256(args.reference_profile),
        },
        "cases_selected": [c.case_id for c in cases],
        "cases_in_bundle": len(bundle.cases),
        "label_hops": prepared.label_hops,
        "allowed_classes": sorted(metis_inspired.allowed_classes(prepared.label_hops)),
        "verdicts": dict(sorted(verdicts.items())),
        "x7_identical": None if not args.x7 else all(not r["x7_differences"] for r in rows),
        "diagnostic_wall_seconds": round(time.perf_counter() - started, 3),
        "cases": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(document, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    print(json.dumps({k: document[k] for k in ("verdicts", "x7_identical", "revision")}))
    return 1 if verdicts.get("unexplained") or document["x7_identical"] is False else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("diagnose", help="S2 per-chunk diagnostic (+ optional X7 check)")
    d.add_argument("--bundle", required=True, help="frozen bundle directory (read only)")
    d.add_argument("--profile", required=True, help="metis_inspired arm profile (e.g. M3)")
    d.add_argument("--reference-profile", required=True, help="incremental_graph arm (A0)")
    d.add_argument("--case", action="append", default=[], help="case id (repeatable)")
    d.add_argument("--first", type=int, default=None, help="only the first N selected cases")
    d.add_argument("--metis-run", default=None, help="measured metis run dir (records)")
    d.add_argument("--reference-run", default=None, help="measured reference run dir")
    d.add_argument("--x7", action="store_true", help="also check the X7 ablation identity")
    d.add_argument("--chunk-records", action="store_true", help="keep every chunk record")
    d.add_argument("--output", required=True, help="JSON artifact path (write-once)")
    args = parser.parse_args(argv)
    return cmd_diagnose(args)


if __name__ == "__main__":
    raise SystemExit(main())

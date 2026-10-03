"""WHI-1509 / L07 evidence: bounded, pre-registered quality/work diagnostics of the opt-in
adaptive percentage sampling of `uni_sor_fast`, with its ablation arms.

Not a production module, not part of the L01 driver and not an adopt verdict. It reads the
pre-registration `config/latency/l07.yaml` (refusing an L01 protocol, profile or derived
bundle whose identity differs) and solves L01's derived bundles in-process:

    tuning    the reference `uni_sor_port`, the L06-only arm and every sampling setting of
              the combined and adaptive-only arms on the L01 tuning split of both cohorts,
              plus the sentinel (reported apart); then the pre-registered nominations,
              computed from the tuning split only.
    held_out  the reference, l06_only, both nominations (read from a tuning output of the
              same l07.yaml and code trees) and the fixed aggressive setting, on the L01
              held-out split of both cohorts, once.

Per (bundle, case, setting) it keeps the runner record (the runner's own independent
evaluation, `benchmark.runner._independent_record`), paired regret against the reference
and, for sampling arms, against l06_only (N/A never coerced to zero), status transitions,
counted quotes, the scope/fallback/sampling metadata (stop reason, sampled vs grid entries,
rounds, validations, grid completion, soft/hard limit, incumbent), and single-shot solve
and independent-evaluation times (diagnostic only; load recorded).

    uv run python tools/latency/l07_adaptive_sampling.py tuning \\
        --experiment <L01 experiment dir> --out data/latency-l07/tuning.json
    uv run python tools/latency/l07_adaptive_sampling.py held_out --experiment <L01 dir> \\
        --nomination data/latency-l07/tuning.json --out data/latency-l07/held_out.json
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l02_empty_spans as l02  # noqa: E402
import l06_sor_shortlist as l06  # noqa: E402
import yaml  # noqa: E402

from benchmark.profile import parse_profile, read_profile_document  # noqa: E402
from benchmark.results import load_case_records  # noqa: E402
from benchmark.runner import _independent_record, case_seed  # noqa: E402
from pools.quote import QuoteLimitExceeded, metered_quotes  # noqa: E402
from routing.algorithms.base import SolveContext  # noqa: E402
from routing.algorithms.registry import get_algorithm  # noqa: E402

REPO = l02.REPO
L07 = REPO / "config" / "latency" / "l07.yaml"
CODE_TREES = ("benchmark", "pools", "routing", "snapshot", "tools/latency/l07_adaptive_sampling.py")
SAMPLING_ARMS = ("combined", "adaptive_only")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _registration() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(L07.read_text())
    for key in ("l01_protocol", "profile"):
        path = REPO / doc[key]["path"]
        if _sha(path) != doc[key]["sha256"]:
            raise SystemExit(f"{path}: sha256 differs from the L07 pre-registration")
    if doc["heuristic_default_loss_tolerance"] is not None:
        raise SystemExit("L07 registers no loss tolerance")
    return doc


def _setting(arm: str, doc: dict[str, Any], sampling: dict[str, Any] | None) -> dict[str, Any]:
    if arm == "adaptive_only":
        assert sampling is not None
        coarse = sampling["coarse_step"]
        shortlist = {"probe_percents": list(range(coarse, 101, coarse)),
                     **doc["adaptive_only_shortlist"]}  # fmt: skip
    else:
        shortlist = dict(doc["l06_shortlist"])
    sid = arm
    if sampling is not None:
        soft = sampling["soft_max_quotes"]
        sid += f"-c{sampling['coarse_step']}-r{sampling['refine_radius']}-s{soft or 'none'}"
    return {"id": sid, "arm": arm, "shortlist": shortlist, "sampling": sampling}


def _settings(doc: dict[str, Any]) -> list[dict[str, Any]]:
    out = [_setting("l06_only", doc, None)]
    for arm in SAMPLING_ARMS:
        s = doc["sweep"][arm]
        for c, r, q in itertools.product(s["coarse_step"], s["refine_radius"],
                                         s["soft_max_quotes"]):  # fmt: skip
            out.append(_setting(arm, doc, {"coarse_step": c, "refine_radius": r,
                                           "soft_max_quotes": q}))  # fmt: skip
    return out


def _trees() -> dict[str, str]:
    def rev(spec: str) -> str:
        return subprocess.run(["git", "rev-parse", spec], cwd=REPO, capture_output=True,
                              text=True, check=True).stdout.strip()  # fmt: skip

    return {path: rev(f"HEAD:{path}") for path in CODE_TREES}


def _profile(doc: dict[str, Any], setting: dict[str, Any] | None) -> Any:
    raw = read_profile_document(REPO / doc["profile"]["path"])
    raw["algorithms"] = [doc["reference"]] if setting is None else [doc["candidate"]]
    if setting is not None:
        raw["shortlist"] = dict(setting["shortlist"])
        if setting["sampling"] is not None:
            raw["sampling"] = dict(setting["sampling"])
    return parse_profile(raw, f"{doc['profile']['path']} + L07 {setting and setting['id']}")


def _solve(factory: Any, prepared: Any, bundle: Any, profile: Any, case: Any) -> dict[str, Any]:
    """`l02._solve` plus the runner's independent evaluation time, reported apart."""
    context = SolveContext(
        bundle=bundle,
        objective=profile.objective.bind(bundle),
        prepared=prepared,
        seed=case_seed(profile.measurement.seed, factory.name, case.case_id),
    )
    with metered_quotes(profile.budget.max_quotes) as meter:
        cpu, wall = time.process_time_ns(), time.perf_counter_ns()
        try:
            solved = factory.solve(case, context, profile.budget)
        except QuoteLimitExceeded as exc:
            return {"quote_limit": str(exc), "quotes": {"counted": meter.counted}}
        wall, cpu = time.perf_counter_ns() - wall, time.process_time_ns() - cpu
    record, eval_ns = _independent_record(bundle, case, profile.objective, solved)
    out: dict[str, Any] = json.loads(json.dumps(record.to_dict()))
    out["quotes"] = {"attempted": meter.attempted, "counted": meter.counted}
    out["_cpu_seconds"], out["_wall_seconds"] = cpu / 1e9, wall / 1e9
    out["_independent_eval_seconds"] = eval_ns / 1e9
    return out


def _slim(result: dict[str, Any]) -> dict[str, Any]:
    out = l06._slim(result)
    out["independent_eval_seconds"] = result.get("_independent_eval_seconds")
    search = result.get("search") or {}
    out["truncated_by"] = search.get("truncated_by")
    samp = search.get("sampling")
    if samp is not None:
        out["sampling"] = {k: v for k, v in samp.items() if k not in ("approximation", "rounds")}
        out["sampling"]["rounds"] = [
            {k: r[k] for k in ("kind", "added_percents", "table_quotes", "incumbent")}
            for r in samp.get("rounds") or []
        ]
    return out


def _cases(doc: dict[str, Any], split: str) -> set[str]:
    protocol = yaml.safe_load((REPO / doc["l01_protocol"]["path"]).read_text())
    return {m["case"] for m in protocol["matrix"] if m["split"] == split}


def _run(args: argparse.Namespace, doc: dict[str, Any], settings: list[dict[str, Any]],
         split: str, sentinel: bool) -> list[dict[str, Any]]:  # fmt: skip
    exp_dir = Path(args.experiment)
    experiment = l02._experiment(exp_dir)
    if experiment["experiment_id"] != doc["l01_experiment"]:
        raise SystemExit(f"{exp_dir}: not the pre-registered L01 experiment")
    wanted = _cases(doc, split)
    ref_profile = _profile(doc, None)
    ref_factory = get_algorithm(doc["reference"])
    cand_factory = get_algorithm(doc["candidate"])
    profiles = {s["id"]: _profile(doc, s) for s in settings}
    rows: list[dict[str, Any]] = []
    for key in doc["bundles"]:
        is_sentinel = key.endswith("/sentinel")
        if is_sentinel and not sentinel:
            continue
        bundle = l02._bundle(exp_dir, experiment, key)
        baseline = {
            (r["algorithm"], r["case_id"]): r
            for r in load_case_records(exp_dir / "runs" / f"timing-fixed-{key.replace('/', '-')}")
        }
        ref_prepared = l02._prepare(ref_factory, bundle, ref_profile)
        prepared, prepare_s = {}, {}
        for sid, p in profiles.items():
            t0 = time.perf_counter_ns()
            prepared[sid] = l02._prepare(cand_factory, bundle, p)
            prepare_s[sid] = (time.perf_counter_ns() - t0) / 1e9
        for case in bundle.cases:
            if not is_sentinel and case.case_id not in wanted:
                continue
            load = os.getloadavg()[0]
            ref = _solve(ref_factory, ref_prepared, bundle, ref_profile, case)
            base = baseline[(ref_factory.name, case.case_id)]
            ref_equal = all(ref.get(f) == base.get(f) for f in l02.SEMANTIC)
            l06_rec: dict[str, Any] | None = None
            for s in settings:
                cand = _solve(cand_factory, prepared[s["id"]], bundle, profiles[s["id"]], case)
                if s["arm"] == "l06_only":
                    l06_rec = cand
                vs_l06 = l06._pair(l06_rec, cand) if l06_rec is not None and s["sampling"] else None
                row = {
                    "bundle": key,
                    "split": "sentinel" if is_sentinel else split,
                    "case": case.case_id,
                    "setting": s["id"],
                    "arm": s["arm"],
                    "reference": _slim(ref),
                    "candidate": _slim(cand),
                    "prepare_seconds": prepare_s[s["id"]],
                    "reference_equals_l01_baseline": ref_equal,
                    "loadavg_1m_before": load,
                    **l06._pair(ref, cand),
                    "regret_vs_l06_bps": None if vs_l06 is None else vs_l06["regret_bps"],
                    "transition_vs_l06": None if vs_l06 is None else vs_l06["transition"],
                }
                row["quotes_ratio"] = (
                    row["candidate"]["quotes_counted"] / row["reference"]["quotes_counted"]
                    if row["reference"]["quotes_counted"]
                    else None
                )
                rows.append(row)
                print(json.dumps({k: row[k] for k in ("bundle", "case", "setting", "transition",
                      "regret_bps", "regret_vs_l06_bps", "quotes_ratio")}), flush=True)  # fmt: skip
    return rows


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """`l06._summary` per (setting, split, bundle), plus the sampling-specific counts."""
    out = l06._summary(rows)
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((r["setting"], r["split"], r["bundle"]), []).append(r)
        groups.setdefault((r["setting"], r["split"], "*"), []).append(r)
    for (sid, split, bundle), rs in groups.items():
        g = out[sid][split][bundle]
        vs = [r["regret_vs_l06_bps"] for r in rs if r["regret_vs_l06_bps"] is not None]
        g["vs_l06"] = {
            "paired_n": len(vs),
            "loss_cases": sum(1 for v in vs if v > 0),
            "gain_cases": sum(1 for v in vs if v < 0),
            "max_regret_bps": max(vs) if vs else None,
            "sum_regret_bps": round(sum(vs), 6) if vs else None,
        }
        samp = [r["candidate"].get("sampling") for r in rs if r["candidate"].get("sampling")]
        stops: dict[str, int] = {}
        for s in samp:
            stops[str(s.get("stop_reason"))] = stops.get(str(s.get("stop_reason")), 0) + 1
        g["sampling"] = {
            "stop_reasons": dict(sorted(stops.items())),
            "grid_completions": sum(1 for s in samp if s["grid_completion"]["triggered"]),
            "sampled_entries": sum(s["sampled_entries"] for s in samp),
            "grid_entries": sum(s["grid_entries"] for s in samp),
            "validations": sum(s["validations"] for s in samp),
            "rejected_incumbents": sum(s["rejected_incumbents"] for s in samp),
            "validation_quotes": sum(
                (r["candidate"]["shortlist"].get("quotes") or {}).get("validation", 0)
                for r in rs
            ),
        } if samp else None  # fmt: skip
    return out


def _nominate(summary: dict[str, Any], settings: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    order = {s["id"]: i for i, s in enumerate(settings) if s["arm"] == arm}
    keys = {}
    for sid in order:
        t = summary[sid]["tuning"]["*"]
        keys[sid] = (
            t["status_regressions"],
            t["max_regret_bps"] if t["max_regret_bps"] is not None else float("inf"),
            t["sum_regret_bps"] if t["sum_regret_bps"] is not None else float("inf"),
            t["quotes_counted"]["candidate"],
            order[sid],
        )
    ranked = sorted(order, key=lambda sid: keys[sid])
    return {"nominated": ranked[0], "ranking": [[sid, list(keys[sid])] for sid in ranked]}


def _header(args: argparse.Namespace) -> dict[str, Any]:
    env = l02._environment()
    if env["git_dirty"] and not args.allow_dirty:
        raise SystemExit("refusing a dirty tree (L07 evidence is bound to a committed source)")
    return {
        "kind": "L07 bounded quality/work diagnostic (in-process; not an L01 run or verdict)",
        "registration": {"path": str(L07.relative_to(REPO)), "sha256": _sha(L07)},
        "experiment": str(args.experiment),
        "environment": env,
        "code_trees": _trees(),
        "load_threshold": 0.5 * (os.cpu_count() or 1),
    }


def tuning(args: argparse.Namespace) -> dict[str, Any]:
    doc = _registration()
    header = _header(args)
    settings = _settings(doc)
    phase = doc["phases"]["tuning"]
    rows = _run(args, doc, settings, phase["split"], phase["sentinel"])
    summary = _summary(rows)
    return {**header, "phase": "tuning", "settings": settings, "summary": summary,
            "nominations": {arm: _nominate(summary, settings, arm) for arm in SAMPLING_ARMS},
            "rows": rows}  # fmt: skip


def held_out(args: argparse.Namespace) -> dict[str, Any]:
    doc = _registration()
    header = _header(args)
    tuned = json.loads(Path(args.nomination).read_text())
    if tuned.get("phase") != "tuning" or tuned["registration"] != header["registration"]:
        raise SystemExit("the nominations must be a tuning output of this same l07.yaml")
    if tuned["code_trees"] != header["code_trees"]:
        raise SystemExit("the code changed since the tuning run; the nominations do not carry")
    by_id = {s["id"]: s for s in _settings(doc)}
    agg = {k: v for k, v in doc["aggressive"].items() if k != "arm"}
    aggressive = _setting(doc["aggressive"]["arm"], doc, agg)
    roles: dict[str, list[str]] = {}
    chosen: list[dict[str, Any]] = []
    for role, sid in [("l06_only", "l06_only"),
                      *((f"{arm} nomination", tuned["nominations"][arm]["nominated"])
                        for arm in SAMPLING_ARMS),
                      ("fixed aggressive", aggressive["id"])]:  # fmt: skip
        if sid not in roles:
            chosen.append(by_id[sid])
        roles.setdefault(sid, []).append(role)
    phase = doc["phases"]["held_out"]
    rows = _run(args, doc, chosen, phase["split"], phase["sentinel"])
    return {**header, "phase": "held_out", "settings": chosen, "roles": roles,
            "nomination_source": {"path": args.nomination, "sha256": _sha(Path(args.nomination))},
            "summary": _summary(rows), "rows": rows}  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("tuning", "held_out"):
        p = sub.add_parser(name)
        p.add_argument("--experiment", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--allow-dirty", action="store_true")
        if name == "held_out":
            p.add_argument("--nomination", required=True)
    args = parser.parse_args()
    result = tuning(args) if args.command == "tuning" else held_out(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

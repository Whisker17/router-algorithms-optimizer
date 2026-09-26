"""WHI-1508 / L06 evidence: bounded, pre-registered quality diagnostics of `uni_sor_fast`.

Not a production module, not part of the L01 driver and not an adopt verdict. It reads the
pre-registration `config/latency/l06.yaml` (refusing an L01 protocol, profile or derived
bundle whose identity differs) and solves L01's derived bundles in-process:

    tuning    every sweep setting of `uni_sor_fast` and the `uni_sor_port` reference on the
              L01 tuning split of both cohorts, plus the sentinel (reported apart); then
              the pre-registered nomination, computed from the tuning split only.
    held_out  exactly two settings -- the nomination read from a tuning output (which must
              come from the same l06.yaml and the same code trees) and the fixed
              aggressive setting -- on the L01 held-out split of both cohorts, once.

Per (bundle, case, setting) it keeps the full runner record (the runner's own independent
evaluation, `benchmark.runner._independent_record`, on the ordinary default path), paired
regret against the same-scope reference with N/A never coerced to zero, status
transitions, counted quotes of both sides, the shortlist scope/fallback metadata and
single-shot solve times (diagnostic only; load recorded). The reference is re-solved at
the current source and its semantic fields are compared with the L01 baseline record.

    uv run python tools/latency/l06_sor_shortlist.py tuning \\
        --experiment <L01 experiment dir> --out data/latency-l06/tuning.json
    uv run python tools/latency/l06_sor_shortlist.py held_out --experiment <L01 dir> \\
        --nomination data/latency-l06/tuning.json --out data/latency-l06/held_out.json
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l02_empty_spans as l02  # noqa: E402
import yaml  # noqa: E402

from benchmark.profile import parse_profile, read_profile_document  # noqa: E402
from benchmark.results import load_case_records  # noqa: E402
from routing.algorithms.registry import get_algorithm  # noqa: E402

REPO = l02.REPO
L06 = REPO / "config" / "latency" / "l06.yaml"
CODE_TREES = ("benchmark", "pools", "routing", "snapshot", "tools/latency/l06_sor_shortlist.py")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _registration() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(L06.read_text())
    for key in ("l01_protocol", "profile"):
        path = REPO / doc[key]["path"]
        if _sha(path) != doc[key]["sha256"]:
            raise SystemExit(f"{path}: sha256 differs from the L06 pre-registration")
    if doc["heuristic_default_loss_tolerance"] is not None:
        raise SystemExit("L06 registers no loss tolerance")
    return doc


def _settings(doc: dict[str, Any]) -> list[dict[str, Any]]:
    s = doc["sweep"]
    out = []
    for probes, k, d in itertools.product(
        s["probe_percents"], s["routes_per_probe"], s["direct_routes"]
    ):
        setting = {"probe_percents": probes, "routes_per_probe": k, "direct_routes": d}
        out.append({"id": _setting_id(setting), **setting})
    return out


def _setting_id(setting: dict[str, Any]) -> str:
    probes = ".".join(map(str, setting["probe_percents"]))
    return f"p{probes}-k{setting['routes_per_probe']}-d{setting['direct_routes']}"


def _trees() -> dict[str, str]:
    def rev(spec: str) -> str:
        return subprocess.run(["git", "rev-parse", spec], cwd=REPO, capture_output=True,
                              text=True, check=True).stdout.strip()  # fmt: skip

    return {path: rev(f"HEAD:{path}") for path in CODE_TREES}


def _profile(doc: dict[str, Any], setting: dict[str, Any] | None) -> Any:
    raw = read_profile_document(REPO / doc["profile"]["path"])
    raw["algorithms"] = [doc["reference"]] if setting is None else [doc["candidate"]]
    if setting is not None:
        raw["shortlist"] = {k: setting[k] for k in ("probe_percents", "routes_per_probe",
                                                   "direct_routes")}  # fmt: skip
    return parse_profile(raw, f"{doc['profile']['path']} + L06 {setting and setting['id']}")


def _gross(record: dict[str, Any]) -> int | None:
    if record.get("status") != "ok" or record.get("score") is None:
        return None
    return int(record["score"])


def _pair(ref: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any]:
    r, c = _gross(ref), _gross(cand)
    out: dict[str, Any] = {
        "transition": f"{ref.get('status', 'quote_limit')}->{cand.get('status', 'quote_limit')}",
        "status_regression": r is not None and cand.get("status") != "ok",
        "regret_bps": None,
        "loss_raw": None,
    }
    if r is not None and r > 0 and c is not None:
        out["regret_bps"] = round((r - c) * 10_000 / r, 6)
        out["loss_raw"] = str(r - c)
    return out


def _slim(result: dict[str, Any]) -> dict[str, Any]:
    search = result.get("search") or {}
    shortlist = search.get("shortlist") or {}
    return {
        "status": result.get("status", "quote_limit"),
        "score": result.get("score"),
        "error": result.get("error"),
        "quotes_counted": (result.get("quotes") or {}).get("counted"),
        "candidates_considered": result.get("candidates_considered"),
        "candidates_truncated": result.get("candidates_truncated"),
        "search_scope": search.get("search_scope"),
        "routes_enumerated": search.get("routes_enumerated"),
        "quote_entries": search.get("quote_entries"),
        "shortlist": {k: v for k, v in shortlist.items() if k != "searched_route_ids"},
        "searched_route_ids": shortlist.get("searched_route_ids"),
        "selection_routes": [
            {"pool_ids": r["pool_ids"], "percent": r["percent"]}
            for r in ((search.get("selection") or {}).get("routes") or [])
        ],
        "independent_matches_solver": (
            result.get("status") != "ok"
            or (result.get("evaluation") or {}).get("gross_output") == search.get("evaluated_gross")
        ),
        "solve_cpu_seconds": result.get("_cpu_seconds"),
        "solve_wall_seconds": result.get("_wall_seconds"),
    }


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
        prepared = {sid: l02._prepare(cand_factory, bundle, p) for sid, p in profiles.items()}
        for case in bundle.cases:
            if not is_sentinel and case.case_id not in wanted:
                continue
            load = os.getloadavg()[0]
            ref = l02._solve(ref_factory, ref_prepared, bundle, ref_profile, case)
            base = baseline[(ref_factory.name, case.case_id)]
            ref_equal = all(ref.get(f) == base.get(f) for f in l02.SEMANTIC)
            for s in settings:
                cand = l02._solve(cand_factory, prepared[s["id"]], bundle, profiles[s["id"]], case)
                row = {
                    "bundle": key,
                    "split": "sentinel" if is_sentinel else split,
                    "case": case.case_id,
                    "setting": s["id"],
                    "reference": _slim(ref),
                    "candidate": _slim(cand),
                    "reference_equals_l01_baseline": ref_equal,
                    "loadavg_1m_before": load,
                    **_pair(ref, cand),
                }
                row["quotes_ratio"] = (
                    row["candidate"]["quotes_counted"] / row["reference"]["quotes_counted"]
                    if row["reference"]["quotes_counted"]
                    else None
                )
                rows.append(row)
                print(json.dumps({k: row[k] for k in ("bundle", "case", "setting", "transition",
                      "regret_bps", "quotes_ratio")} | {"scope": row["candidate"]["search_scope"]}),
                      flush=True)  # fmt: skip
    return rows


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per (setting, split, bundle) and per (setting, split): nothing pooled across splits."""
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((r["setting"], r["split"], r["bundle"]), []).append(r)
        groups.setdefault((r["setting"], r["split"], "*"), []).append(r)
    out: dict[str, Any] = {}
    for (sid, split, bundle), rs in sorted(groups.items()):
        regrets = [r["regret_bps"] for r in rs if r["regret_bps"] is not None]
        transitions: dict[str, int] = {}
        scopes: dict[str, int] = {}
        for r in rs:
            transitions[r["transition"]] = transitions.get(r["transition"], 0) + 1
            scope = str(r["candidate"]["search_scope"])
            scopes[scope] = scopes.get(scope, 0) + 1
        ref_q = sum(r["reference"]["quotes_counted"] or 0 for r in rs)
        cand_q = sum(r["candidate"]["quotes_counted"] or 0 for r in rs)
        out.setdefault(sid, {}).setdefault(split, {})[bundle] = {
            "scheduled": len(rs),
            "paired_regret_n": len(regrets),
            "regret_na": len(rs) - len(regrets),
            "loss_cases": sum(1 for v in regrets if v > 0),
            "equal_cases": sum(1 for v in regrets if v == 0),
            "gain_cases": sum(1 for v in regrets if v < 0),
            "max_regret_bps": max(regrets) if regrets else None,
            "sum_regret_bps": round(sum(regrets), 6) if regrets else None,
            "worst": sorted(
                ({"bundle": r["bundle"], "case": r["case"], "regret_bps": r["regret_bps"]}
                 for r in rs if r["regret_bps"] is not None),
                key=lambda x: -x["regret_bps"])[:3],  # fmt: skip
            "status_regressions": sum(1 for r in rs if r["status_regression"]),
            "transitions": dict(sorted(transitions.items())),
            "search_scope": dict(sorted(scopes.items())),
            "fallbacks": sum(1 for r in rs if (r["candidate"]["shortlist"].get("fallback") or {})
                             .get("triggered")),  # fmt: skip
            "quotes_counted": {"reference": ref_q, "candidate": cand_q,
                               "ratio": cand_q / ref_q if ref_q else None},  # fmt: skip
            "quotes_exceed_reference": sum(
                1 for r in rs
                if (r["candidate"]["quotes_counted"] or 0) > (r["reference"]["quotes_counted"] or 0)
            ),  # fmt: skip
            "independent_mismatch": sum(1 for r in rs if not r["candidate"]
                                        ["independent_matches_solver"]),  # fmt: skip
            "reference_not_equal_l01_baseline": sum(
                1 for r in rs if not r["reference_equals_l01_baseline"]),  # fmt: skip
        }
    return out


def _nominate(summary: dict[str, Any], settings: list[dict[str, Any]]) -> dict[str, Any]:
    order = {s["id"]: i for i, s in enumerate(settings)}
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


def _header(doc: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    env = l02._environment()
    if env["git_dirty"] and not args.allow_dirty:
        raise SystemExit("refusing a dirty tree (L06 evidence is bound to a committed source)")
    return {
        "kind": "L06 bounded quality diagnostic (in-process; not an L01 experiment or verdict)",
        "registration": {"path": str(L06.relative_to(REPO)), "sha256": _sha(L06)},
        "experiment": str(args.experiment),
        "environment": env,
        "code_trees": _trees(),
        "load_threshold": 0.5 * (os.cpu_count() or 1),
    }


def tuning(args: argparse.Namespace) -> dict[str, Any]:
    doc = _registration()
    header = _header(doc, args)
    settings = _settings(doc)
    rows = _run(args, doc, settings, doc["phases"]["tuning"]["split"],
                doc["phases"]["tuning"]["sentinel"])  # fmt: skip
    summary = _summary(rows)
    return {**header, "phase": "tuning", "settings": settings, "summary": summary,
            "nomination": _nominate(summary, settings), "rows": rows}  # fmt: skip


def held_out(args: argparse.Namespace) -> dict[str, Any]:
    doc = _registration()
    header = _header(doc, args)
    tuned = json.loads(Path(args.nomination).read_text())
    if tuned.get("phase") != "tuning" or tuned["registration"] != header["registration"]:
        raise SystemExit("the nomination must be a tuning output of this same l06.yaml")
    if tuned["code_trees"] != header["code_trees"]:
        raise SystemExit("the code changed since the tuning run; the nomination does not carry")
    by_id = {s["id"]: s for s in _settings(doc)}
    aggressive = {"id": _setting_id(doc["aggressive"]), **doc["aggressive"]}
    chosen = [by_id[tuned["nomination"]["nominated"]]]
    if aggressive["id"] != chosen[0]["id"]:
        chosen.append(aggressive)
    phase = doc["phases"]["held_out"]
    rows = _run(args, doc, chosen, phase["split"], phase["sentinel"])
    return {**header, "phase": "held_out", "settings": chosen,
            "roles": {chosen[0]["id"]: "tuning nomination", aggressive["id"]: "fixed aggressive"},
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

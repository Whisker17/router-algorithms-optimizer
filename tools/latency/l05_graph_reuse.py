"""WHI-1507 / L05 experiment evidence: exact graph marginal-score and admission reuse.

Not a production module and not part of the L01 driver. It solves `incremental_graph` on
L01's derived bundles in-process under explicitly selected variants:

    ref          everything default (the reference at this HEAD), re-run as a check.
    ref_l05      default quote path (L02/L03/L04 off) + `solve(..., graph_reuse=True)`.
    l04          post-L04 control: CL `swap(..., skip_empty_spans=True,
                 math_reuse=TickMathReuse, prefix_reuse=CLPrefixReuse)`, LB `math_reuse`
                 (WHI-1506's `l02_l03_l04`, fresh per solve), `graph_reuse` off.
    l04_l05      the same plus `graph_reuse=True`.

Every variant is also compared with the L01 baseline records of the default reference.
Only `solve` runs under a variant; the final evaluation
(`benchmark.runner._independent_record`) runs afterwards on the ordinary default path.

    work    per (bundle, case), instrumented, in `--order fixed|reverse` (cases solved on
            one prepared object in that order; compared with the L01 `timing-<order>`
            baseline records): every CL swap the solver executed (arguments and outcome
            or exception) digested in call order and compared between l04 and l04_l05;
            semantic and work fields compared with the control and the baseline, except
            the two physical fields `search.quotes_memoized` and `search.graph_reuse`,
            reported separately; `creates_cycle` calls counted. CPU is diagnostic only.
    paired  per target: rotated l04 / l04_l05 solves, CPU and wall around `solve` only,
            load around each; then one tracemalloc pass per variant (solve peak).

Neither is an L01 experiment or an adopt verdict; WHI-1510 owns the performance decision.

    uv run python tools/latency/l05_graph_reuse.py work --experiment <L01 dir> --out o.json
    uv run python tools/latency/l05_graph_reuse.py paired --experiment <L01 dir> \\
        --target full_source/sentinel:<case> --pairs 3 --out p.json
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import gc
import json
import os
import sys
import tracemalloc
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l02_empty_spans as l02  # noqa: E402
import l04_prefix_reuse as l04  # noqa: E402

from benchmark.profile import load_profile  # noqa: E402
from benchmark.results import load_case_records  # noqa: E402
from routing.algorithms import incremental_graph as ig  # noqa: E402
from routing.algorithms.registry import get_algorithm  # noqa: E402

REPO = l02.REPO
SEMANTIC, WORK = l02.SEMANTIC, l02.WORK
PHYSICAL = ("quotes_memoized", "graph_reuse")
LABELS = ("ref", "ref_l05", "l04", "l04_l05")
ORIGINAL_CYCLE = ig.creates_cycle


class Variant:
    def __init__(self, label: str, args: argparse.Namespace) -> None:
        self.label = label
        self.quotes = None if label.startswith("ref") else l04.Variant("l02_l03_l04", args)
        self.factory = dataclasses.replace(
            get_algorithm(ig.NAME),
            solve=functools.partial(ig.solve, graph_reuse=label.endswith("l05")),
        )
        self.cycle_calls = 0

    @contextmanager
    def installed(self, instrument: bool) -> Iterator[None]:
        def counted(*a: Any) -> bool:
            self.cycle_calls += 1
            return ORIGINAL_CYCLE(*a)

        if instrument:
            ig.creates_cycle = counted
        try:
            yield
        finally:
            ig.creates_cycle = ORIGINAL_CYCLE


def _solve(v: Variant, prepared: Any, bundle: Any, profile: Any, case: Any,
           instrument: bool) -> dict[str, Any]:  # fmt: skip
    with v.installed(instrument):
        if v.quotes is not None:
            return l04._solve(v.factory, prepared, bundle, profile, case, v.quotes, instrument)
        return l02._solve(v.factory, prepared, bundle, profile, case)  # default quote path


def _split(result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Comparable fields, and the physical search fields that are expected to differ."""
    fields = {f: result.get(f) for f in SEMANTIC + WORK}
    search = dict(fields.get("search") or {})
    physical = {k: search.pop(k, None) for k in PHYSICAL}
    fields["search"] = search
    return fields, physical


def work(args: argparse.Namespace) -> dict[str, Any]:
    exp_dir = Path(args.experiment)
    experiment = l02._experiment(exp_dir)
    profile = load_profile(REPO / experiment["profile"]["path"])
    rows: list[dict[str, Any]] = []
    for key in args.bundles.split(","):
        bundle = l02._bundle(exp_dir, experiment, key)
        run = exp_dir / "runs" / f"timing-{args.order}-{key.replace('/', '-')}"
        baseline = {r["case_id"]: r for r in load_case_records(run) if r["algorithm"] == ig.NAME}
        prepared = l02._prepare(get_algorithm(ig.NAME), bundle, profile)
        cases = list(bundle.cases)
        for case in cases if args.order == "fixed" else cases[::-1]:
            res, var, phys, cmp = {}, {}, {}, {}
            for label in LABELS:
                var[label] = Variant(label, args)
                res[label] = _solve(var[label], prepared, bundle, profile, case, True)
                cmp[label], phys[label] = _split(res[label])
            base, _ = _split(baseline[case.case_id])
            quotes = {x: var[x].quotes for x in LABELS[2:]}
            row = {
                "bundle": key, "case": case.case_id, "order": args.order,
                "status": res["l04_l05"].get("status", "quote_limit"),
                "cl_calls_equal": len({q.digest.digest() for q in quotes.values()}) == 1,
                "control_equal": cmp["l04"] == cmp["l04_l05"],
                "baseline_equal": all(cmp[x] == base for x in LABELS),
                "semantic_baseline_equal": all(cmp[x][f] == base[f] for x in LABELS
                                               for f in SEMANTIC),
                "paths_scored": cmp["l04"]["search"].get("paths_scored"),
                "paths_rejected_cycle": cmp["l04"]["search"].get("paths_rejected_cycle"),
                "quotes_executed": cmp["l04"]["search"].get("quotes_executed"),
                "physical": phys,
                "creates_cycle_calls": {x: var[x].cycle_calls for x in LABELS},
                "cl_counts": {x: q.counts for x, q in quotes.items()},
                "cpu_seconds_instrumented": {x: res[x].get("_cpu_seconds") for x in LABELS},
            }  # fmt: skip
            rows.append(row)
            print(json.dumps({k: row[k] for k in ("bundle", "case", "status", "cl_calls_equal",
                  "control_equal", "baseline_equal", "paths_scored")}), flush=True)  # fmt: skip

    def total(pick: Callable[[dict[str, Any]], Any]) -> int:
        return sum(pick(r) or 0 for r in rows)

    reuse = [r["physical"]["l04_l05"]["graph_reuse"] or {} for r in rows]
    return {
        "kind": "L05 work diagnostic (instrumented; ref / ref_l05 / l04 / l04_l05, cold per solve)",
        "experiment": str(exp_dir),
        "experiment_source": experiment["source"].get("git_revision"),
        "environment": l02._environment(),
        "order": args.order,
        "settings": {
            k: getattr(args, k)
            for k in ("tick_capacity", "bin_capacity", "max_keys", "max_checkpoints")
        },
        "records": len(rows),
        **{
            f"all_{k}": all(r[k] for r in rows)
            for k in (
                "cl_calls_equal",
                "control_equal",
                "baseline_equal",
                "semantic_baseline_equal",
            )
        },
        "totals": {
            "paths_scored_logical": total(lambda r: r["paths_scored"]),
            "paths_rejected_cycle_logical": total(lambda r: r["paths_rejected_cycle"]),
            "quotes_executed": total(lambda r: r["quotes_executed"]),
            **{
                f"quotes_memoized_{x}": total(lambda r, x=x: r["physical"][x]["quotes_memoized"])
                for x in LABELS
            },
            **{
                f"creates_cycle_calls_{x}": total(lambda r, x=x: r["creates_cycle_calls"][x])
                for x in LABELS
            },
            **{
                f"l04_l05_{k}": sum(s.get(k, 0) for s in reuse)
                for k in (
                    "scores_reused",
                    "scores_recomputed",
                    "score_invalidations",
                    "cycle_decisions_reused",
                    "cycle_checks_executed",
                    "closure_edges_added",
                )
            },
            "l04_l05_score_entries_peak_max": max(s.get("score_entries_peak", 0) for s in reuse),
            **{
                f"cpu_seconds_instrumented_{x}": sum(
                    r["cpu_seconds_instrumented"][x] or 0.0 for r in rows
                )
                for x in LABELS
            },
        },  # fmt: skip
        "rows": rows,
    }


def _memory(v: Variant, prepared: Any, bundle: Any, profile: Any, case: Any) -> dict[str, Any]:
    gc.collect()
    tracemalloc.start()
    start = tracemalloc.get_traced_memory()[0]
    tracemalloc.reset_peak()
    result = _solve(v, prepared, bundle, profile, case, False)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return {"solve_and_evaluation_peak_traced_bytes_over_start": peak - start,
            "fields": _split(result)[0]}  # fmt: skip


def paired(args: argparse.Namespace) -> dict[str, Any]:
    exp_dir = Path(args.experiment)
    experiment = l02._experiment(exp_dir)
    profile = load_profile(REPO / experiment["profile"]["path"])
    labels = LABELS[2:]
    targets = []
    for target in args.target:
        key, case_id = target.split(":")
        bundle = l02._bundle(exp_dir, experiment, key)
        case = next(c for c in bundle.cases if c.case_id == case_id)
        prepared = l02._prepare(get_algorithm(ig.NAME), bundle, profile)
        samples: dict[str, list[dict[str, Any]]] = {x: [] for x in labels}
        fields: dict[str, Any] = {}
        for pair in range(args.pairs):
            for label in labels if pair % 2 == 0 else labels[::-1]:
                load_before = os.getloadavg()[0]
                result = _solve(Variant(label, args), prepared, bundle, profile, case, False)
                if fields.setdefault(label, _split(result)[0]) != _split(result)[0]:
                    raise SystemExit(f"{target}: {label} attempts are not consistent")
                samples[label].append({"cpu": result["_cpu_seconds"],
                                       "wall": result["_wall_seconds"],
                                       "load_before": load_before,
                                       "load_after": os.getloadavg()[0]})  # fmt: skip
        med = {x: {m: sorted(s[m] for s in v)[len(v) // 2] for m in ("cpu", "wall")}
               for x, v in samples.items()}  # fmt: skip
        memory = {x: _memory(Variant(x, args), prepared, bundle, profile, case) for x in labels}
        equal = fields[labels[0]] == fields[labels[1]]
        equal &= all(m.pop("fields") == fields[labels[0]] for m in memory.values())
        targets.append({
            "target": target, "fields_equal": equal, "samples": samples, "median": med,
            "median_ratio": {m: med[labels[1]][m] / med[labels[0]][m] for m in ("cpu", "wall")},
            "memory_instrumented": memory,
            "max_load": max(max(s["load_before"], s["load_after"])
                            for v in samples.values() for s in v),
        })  # fmt: skip
        print(json.dumps({k: targets[-1][k] for k in ("target", "fields_equal", "median",
                          "median_ratio", "max_load")}), flush=True)  # fmt: skip
    return {
        "kind": "L05 in-process paired timing (l04 control vs l04_l05, cold per solve; "
        "uninstrumented solve window; not an L01 verdict)",
        "experiment": str(exp_dir),
        "environment": l02._environment(),
        "settings": {k: getattr(args, k)
                     for k in ("tick_capacity", "bin_capacity", "max_keys", "max_checkpoints")},
        "pairs": args.pairs,
        "load_threshold": 0.5 * (os.cpu_count() or 1),
        "targets": targets,
    }  # fmt: skip


def profile(args: argparse.Namespace) -> dict[str, Any]:
    """cProfile (instrumented) of one solve per variant: cumulative seconds of the
    embedded `path_split`, the chunk scoring (`marginal`, `reused_marginal`,
    `creates_cycle`, `_ExactReuse.cyclic`) and the final `evaluate`."""
    import cProfile
    import pstats

    exp_dir = Path(args.experiment)
    experiment = l02._experiment(exp_dir)
    prof = load_profile(REPO / experiment["profile"]["path"])
    key, case_id = args.target.split(":")
    bundle = l02._bundle(exp_dir, experiment, key)
    case = next(c for c in bundle.cases if c.case_id == case_id)
    prepared = l02._prepare(get_algorithm(ig.NAME), bundle, prof)
    wanted = {"solve", "marginal", "reused_marginal", "creates_cycle", "cyclic", "evaluate"}
    out: dict[str, Any] = {}
    for label in LABELS:
        profiler = cProfile.Profile()
        profiler.enable()
        _solve(Variant(label, args), prepared, bundle, prof, case, False)
        profiler.disable()
        rows = pstats.Stats(profiler).stats  # type: ignore[attr-defined]
        out[label] = {
            f"{Path(f).parent.name}/{Path(f).name}:{fn}": {"calls": nc, "cumulative_s": ct}
            for (f, _, fn), (_, nc, _, ct, _) in rows.items()
            if fn in wanted and ("routing" in f or "benchmark" in f)
        }
    return {"kind": "L05 cProfile (instrumented, one solve per variant, not a timing)",
            "environment": l02._environment(), "target": args.target, "profiles": out}  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("work")
    w.add_argument("--experiment", required=True)
    w.add_argument("--bundles", default="full_source/sentinel,full_source/matrix,"
                   "sor_compatible/sentinel,sor_compatible/matrix")  # fmt: skip
    w.add_argument("--order", choices=("fixed", "reverse"), default="fixed")
    w.add_argument("--out", required=True)
    p = sub.add_parser("paired")
    p.add_argument("--experiment", required=True)
    p.add_argument("--target", action="append", required=True, help="bundle:case")
    p.add_argument("--pairs", type=int, default=3)
    p.add_argument("--out", required=True)
    pr = sub.add_parser("profile")
    pr.add_argument("--experiment", required=True)
    pr.add_argument("--target", required=True, help="bundle:case")
    pr.add_argument("--out", required=True)
    for s in (w, p, pr):  # declared experiment settings (WHI-1506's), not runtime defaults
        s.add_argument("--tick-capacity", type=int, default=16384)
        s.add_argument("--bin-capacity", type=int, default=4096)
        s.add_argument("--max-keys", type=int, default=4096)
        s.add_argument("--max-checkpoints", type=int, default=262144)
    args = parser.parse_args()
    run: Callable[[argparse.Namespace], dict[str, Any]] = {
        "work": work,
        "paired": paired,
        "profile": profile,
    }[args.command]
    result = run(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

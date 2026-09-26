"""WHI-1505 / L03 experiment evidence: bounded exact reuse of tick and bin price math.

Not a production module and not part of the L01 driver. It solves L01's derived bundles
in-process under two explicitly selected **post-L02** variants:

    l02_only  CL `swap(..., skip_empty_spans=True, math_reuse=None)`, LB
              `swap(..., math_reuse=None)`: the WHI-1504 fast path, no reuse (control).
    l02_l03   the same with `math_reuse=` a `TickMathReuse` / `BinMathReuse` instance.

Both variants enable L02 explicitly; neither relies on a default. The default reference
(L02 off, no reuse) is *not* re-run here: its outputs are the L01 baseline records the
work rows are compared with. Only `factory.solve` runs under a variant. The independent
final evaluation (`benchmark.runner._independent_record`) runs afterwards on the ordinary
default path (reference loop, no reuse), so it independently checks every solved plan.

    work    per (bundle, algorithm, case): both variants, a fresh (cold) reuse instance per
            solve; semantic and work fields compared with each other and with the L01
            baseline record; memo capacity/entries/hits/misses after the solve. Solve times
            are single-shot and diagnostic.
    paired  per chosen (bundle, algorithm, case): one prepare, one charged warm-population
            solve, then rotated l02_only / l02_l03 cold (fresh instances each solve) /
            l02_l03 warm (instances kept across solves) solves, CPU and wall around
            `solve` only, 1-minute load around each. Then one tracemalloc pass per variant
            (instrumented): constructor bytes/time, solve peak, retained memo bytes.

Neither is an L01 experiment or an adopt verdict; WHI-1510 owns the performance decision.
Capacities are declared experiment settings, not runtime defaults.

    uv run python tools/latency/l03_math_reuse.py work \\
        --experiment <L01 experiment dir> --out data/latency-l03/work.json
    uv run python tools/latency/l03_math_reuse.py paired \\
        --experiment <L01 experiment dir> --target full_source/sentinel:uni_sor_port:<case> \\
        --pairs 3 --out data/latency-l03/paired.json
"""

from __future__ import annotations

import argparse
import functools
import gc
import json
import os
import sys
import time
import tracemalloc
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l02_empty_spans as l02  # noqa: E402

from benchmark.profile import load_profile  # noqa: E402
from benchmark.results import load_case_records  # noqa: E402
from benchmark.runner import _independent_record, case_seed  # noqa: E402
from pools import concentrated as cl  # noqa: E402
from pools import liquidity_book as lb  # noqa: E402
from pools.cl_math import TickMathReuse  # noqa: E402
from pools.quote import QuoteLimitExceeded, metered_quotes  # noqa: E402
from routing.algorithms.base import SolveContext  # noqa: E402
from routing.algorithms.registry import get_algorithm  # noqa: E402

REPO = l02.REPO
SEMANTIC, WORK = l02.SEMANTIC, l02.WORK
ORIGINAL_CL, ORIGINAL_LB = cl.swap, lb.swap
Reuse = tuple[TickMathReuse, lb.BinMathReuse] | None


@contextmanager
def variant(reuse: Reuse) -> Iterator[None]:
    """Post-L02 in both variants; `reuse=None` is the l02_only control. Restored on exit."""
    tick, bins = reuse if reuse is not None else (None, None)
    cl.swap = functools.partial(ORIGINAL_CL, skip_empty_spans=True, math_reuse=tick)
    lb.swap = functools.partial(ORIGINAL_LB, math_reuse=bins)
    try:
        yield
    finally:
        cl.swap, lb.swap = ORIGINAL_CL, ORIGINAL_LB


def _new_reuse(args: argparse.Namespace) -> tuple[TickMathReuse, lb.BinMathReuse]:
    return TickMathReuse(args.tick_capacity), lb.BinMathReuse(args.bin_capacity)


def _stats(reuse: Reuse) -> dict[str, Any] | None:
    return None if reuse is None else {"tick": reuse[0].stats(), "bin": reuse[1].stats()}


def _solve(
    factory: Any, prepared: Any, bundle: Any, profile: Any, case: Any, reuse: Reuse
) -> dict[str, Any]:
    context = SolveContext(
        bundle=bundle,
        objective=profile.objective.bind(bundle),
        prepared=prepared,
        seed=case_seed(profile.measurement.seed, factory.name, case.case_id),
    )
    with variant(reuse), metered_quotes(profile.budget.max_quotes) as meter:
        cpu, wall = time.process_time_ns(), time.perf_counter_ns()
        try:
            solved = factory.solve(case, context, profile.budget)
        except QuoteLimitExceeded as exc:
            return {"quote_limit": str(exc), "quotes": {"counted": meter.counted}}
        wall, cpu = time.perf_counter_ns() - wall, time.process_time_ns() - cpu
    stats = _stats(reuse)  # solve only: taken before the reference-path evaluation
    record, _ = _independent_record(bundle, case, profile.objective, solved)
    out: dict[str, Any] = json.loads(json.dumps(record.to_dict()))
    out["quotes"] = {"attempted": meter.attempted, "counted": meter.counted}
    out["_cpu_seconds"], out["_wall_seconds"], out["_reuse"] = cpu / 1e9, wall / 1e9, stats
    return out


def _fields(result: dict[str, Any]) -> dict[str, Any]:
    return {f: result.get(f) for f in SEMANTIC + WORK}


def work(args: argparse.Namespace) -> dict[str, Any]:
    exp_dir = Path(args.experiment)
    experiment = l02._experiment(exp_dir)
    profile = load_profile(REPO / experiment["profile"]["path"])
    rows: list[dict[str, Any]] = []
    for key in args.bundles.split(","):
        bundle = l02._bundle(exp_dir, experiment, key)
        baseline = {
            (r["algorithm"], r["case_id"]): r
            for r in load_case_records(exp_dir / "runs" / f"timing-fixed-{key.replace('/', '-')}")
        }
        for name in args.algorithms.split(","):
            factory = get_algorithm(name)
            prepared = l02._prepare(factory, bundle, profile)
            for case in bundle.cases:
                only = _solve(factory, prepared, bundle, profile, case, None)
                cand = _solve(factory, prepared, bundle, profile, case, _new_reuse(args))
                base = baseline[(name, case.case_id)]
                row = {
                    "bundle": key, "algorithm": name, "case": case.case_id,
                    "status": cand.get("status", "quote_limit"),
                    "semantic_equal": all(only.get(f) == cand.get(f) for f in SEMANTIC),
                    "work_equal": all(only.get(f) == cand.get(f) for f in WORK),
                    "baseline_semantic_equal": all(cand.get(f) == base.get(f) for f in SEMANTIC),
                    "baseline_work_equal": all(cand.get(f) == base.get(f) for f in WORK),
                    "reuse": cand["_reuse"],
                    "cpu_seconds_single_shot": {
                        "l02_only": only.get("_cpu_seconds"), "l02_l03": cand.get("_cpu_seconds")
                    },
                }  # fmt: skip
                rows.append(row)
                print(json.dumps({k: row[k] for k in ("bundle", "algorithm", "case",
                      "semantic_equal", "work_equal", "baseline_semantic_equal",
                      "baseline_work_equal", "reuse")}), flush=True)  # fmt: skip
    totals = {
        memo: {
            k: sum((r["reuse"] or {}).get(memo, {}).get(k, 0) for r in rows)
            for k in ("hits", "misses")
        }
        | {"max_entries": max((r["reuse"] or {}).get(memo, {}).get("entries", 0) for r in rows)}
        | {
            "records_with_eviction": sum(
                (r["reuse"] or {}).get(memo, {}).get("misses", 0)
                > (r["reuse"] or {}).get(memo, {}).get("entries", 0)
                for r in rows
            )
        }
        for memo in ("tick", "bin")
    }
    return {
        "kind": "L03 work diagnostic (post-L02 l02_only vs l02_l03 cold; single-shot times)",
        "experiment": str(exp_dir),
        "experiment_source": experiment["source"].get("git_revision"),
        "environment": l02._environment(),
        "capacities": {"tick": args.tick_capacity, "bin": args.bin_capacity},
        "all_semantic_equal": all(r["semantic_equal"] for r in rows),
        "all_work_equal": all(r["work_equal"] for r in rows),
        "all_baseline_semantic_equal": all(r["baseline_semantic_equal"] for r in rows),
        "all_baseline_work_equal": all(r["baseline_work_equal"] for r in rows),
        "records": len(rows),
        "reuse_totals": totals,
        "rows": rows,
    }


def _memory(factory: Any, prepared: Any, bundle: Any, profile: Any, case: Any,
            args: argparse.Namespace, with_reuse: bool) -> dict[str, Any]:  # fmt: skip
    """Instrumented tracemalloc pass (solve + evaluation), memo retained bytes."""
    gc.collect()
    tracemalloc.start()
    start = time.perf_counter_ns()
    reuse = _new_reuse(args) if with_reuse else None
    construct_ns = time.perf_counter_ns() - start
    construct_bytes = tracemalloc.get_traced_memory()[0]
    tracemalloc.reset_peak()
    base_current = tracemalloc.get_traced_memory()[0]
    result = _solve(factory, prepared, bundle, profile, case, reuse)
    current, peak = tracemalloc.get_traced_memory()
    del reuse
    gc.collect()
    released = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    return {
        "constructor_seconds": construct_ns / 1e9,
        "constructor_traced_bytes": construct_bytes,
        "solve_peak_traced_bytes_over_start": peak - base_current,
        "retained_memo_bytes": current - released,
        "reuse": result["_reuse"],
        "fields": _fields(result),
    }


def paired(args: argparse.Namespace) -> dict[str, Any]:
    exp_dir = Path(args.experiment)
    experiment = l02._experiment(exp_dir)
    profile = load_profile(REPO / experiment["profile"]["path"])
    targets = []
    for target in args.target:
        key, name, case_id = target.split(":")
        bundle = l02._bundle(exp_dir, experiment, key)
        case = next(c for c in bundle.cases if c.case_id == case_id)
        factory = get_algorithm(name)
        prepared = l02._prepare(factory, bundle, profile)
        warm = _new_reuse(args)
        population = _solve(factory, prepared, bundle, profile, case, warm)
        semantic = {"population": _fields(population)}
        samples: dict[str, list[dict[str, Any]]] = {"l02_only": [], "cold": [], "warm": []}
        for pair in range(args.pairs):
            order = ("l02_only", "cold", "warm") if pair % 2 == 0 else ("warm", "cold", "l02_only")
            for label in order:
                reuse = {"l02_only": None, "cold": _new_reuse(args), "warm": warm}[label]
                load_before = os.getloadavg()[0]
                result = _solve(factory, prepared, bundle, profile, case, reuse)
                if semantic.setdefault(label, _fields(result)) != _fields(result):
                    raise SystemExit(f"{target}: {label} attempts are not consistent")
                samples[label].append({"cpu": result["_cpu_seconds"],
                                       "wall": result["_wall_seconds"],
                                       "load_before": load_before,
                                       "load_after": os.getloadavg()[0],
                                       "reuse": result["_reuse"]})  # fmt: skip
        med = {
            label: {m: sorted(s[m] for s in v)[len(v) // 2] for m in ("cpu", "wall")}
            for label, v in samples.items()
        }
        memory = {
            label: _memory(factory, prepared, bundle, profile, case, args, label == "l02_l03")
            for label in ("l02_only", "l02_l03")
        }
        consistent = len({json.dumps(v, sort_keys=True) for v in semantic.values()}) == 1
        consistent &= memory["l02_only"]["fields"] == memory["l02_l03"]["fields"]
        consistent &= memory["l02_only"]["fields"] == semantic["l02_only"]
        targets.append({
            "target": target, "semantic_equal": consistent,
            "warm_population_solve": {"cpu": population["_cpu_seconds"],
                                      "wall": population["_wall_seconds"],
                                      "reuse": population["_reuse"]},
            "samples": samples, "median": med,
            "median_ratio_vs_l02_only": {
                label: {m: med[label][m] / med["l02_only"][m] for m in ("cpu", "wall")}
                for label in ("cold", "warm")
            },
            "memory_instrumented": {k: {f: v for f, v in m.items() if f != "fields"}
                                    for k, m in memory.items()},
            "max_load": max(max(s["load_before"], s["load_after"])
                            for v in samples.values() for s in v),
        })  # fmt: skip
        print(json.dumps({k: targets[-1][k] for k in ("target", "semantic_equal", "median",
                          "median_ratio_vs_l02_only", "max_load")}), flush=True)  # fmt: skip
    return {
        "kind": "L03 in-process paired timing (post-L02 control vs L02+L03 cold/warm; "
        "uninstrumented solve window; not an L01 verdict)",
        "experiment": str(exp_dir),
        "environment": l02._environment(),
        "capacities": {"tick": args.tick_capacity, "bin": args.bin_capacity},
        "pairs": args.pairs,
        "load_threshold": 0.5 * (os.cpu_count() or 1),
        "targets": targets,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("work")
    w.add_argument("--experiment", required=True)
    w.add_argument("--bundles", default="full_source/sentinel,full_source/matrix,"
                   "sor_compatible/sentinel,sor_compatible/matrix")  # fmt: skip
    w.add_argument("--algorithms", default="direct,single_path,direct_split,path_split,"
                   "incremental_graph,uni_sor_port")  # fmt: skip
    w.add_argument("--out", required=True)
    p = sub.add_parser("paired")
    p.add_argument("--experiment", required=True)
    p.add_argument("--target", action="append", required=True, help="bundle:algorithm:case")
    p.add_argument("--pairs", type=int, default=3)
    p.add_argument("--out", required=True)
    for s in (w, p):  # declared experiment settings, not runtime defaults
        s.add_argument("--tick-capacity", type=int, default=16384)
        s.add_argument("--bin-capacity", type=int, default=4096)
    args = parser.parse_args()
    run: Callable[[argparse.Namespace], dict[str, Any]] = work if args.command == "work" else paired
    result = run(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

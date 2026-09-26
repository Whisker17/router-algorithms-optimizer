"""WHI-1504 / L02 experiment evidence: exact skipping of empty zero-liquidity CL spans.

Not a production module and not part of the L01 driver. It solves L01's derived bundles
in-process, once with the unmodified reference CL loop (`swap(..., skip_empty_spans=False)`)
and once with the default fast path, and records:

    work    per (bundle, algorithm, case): the full runner record of both variants
            (`benchmark.runner._independent_record`, the same independent evaluation the
            runner uses) compared field by field with each other AND with the L01 baseline
            record of the given experiment; plus CL work counters -- CL swaps, logical
            loop iterations (`SwapOutcome.steps`), executed `computeSwapStep` calls,
            `getSqrtRatioAtTick` calls, bitmap word searches and executed
            zero-liquidity/uninitialized iterations. Counters wrap the functions, so these
            solves are instrumented: their CPU times are diagnostic only.
    paired  uninstrumented, in-process paired timing of chosen (bundle, algorithm, case)
            solves: one prepare, then ABBA-alternated reference/candidate solves with a
            fresh SolveContext and quote meter each, CPU and wall time around `solve` only,
            1-minute load sampled around every solve. A focused controlled comparison of
            the quote kernel -- not an L01 experiment, no adopt verdict by itself.

Both refuse a derived bundle whose hash differs from the L01 experiment's record.

    uv run python tools/latency/l02_empty_spans.py work \\
        --experiment <L01 experiment dir> --out data/latency-l02/work.json
    uv run python tools/latency/l02_empty_spans.py paired \\
        --experiment <L01 experiment dir> --target full_source/sentinel:uni_sor_port:<case> \\
        --pairs 5 --out data/latency-l02/paired.json
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from benchmark.profile import load_profile  # noqa: E402
from benchmark.results import load_case_records  # noqa: E402
from benchmark.runner import _independent_record, case_seed  # noqa: E402
from pools import cl_math  # noqa: E402
from pools import concentrated as cl  # noqa: E402
from pools.quote import QuoteLimitExceeded, metered_quotes  # noqa: E402
from routing.algorithms.base import SolveContext  # noqa: E402
from routing.algorithms.registry import get_algorithm  # noqa: E402
from snapshot.bundle import load_bundle  # noqa: E402

# Keys of `CaseRecord.to_dict()` (the L01 exact-lane semantic and work fields).
SEMANTIC = ("status", "evaluation", "score", "error", "limit_hit", "solver_reported")
WORK = ("quotes", "candidates_considered", "candidates_truncated", "search")
ORIGINAL_SWAP = cl.swap


class Counters:
    def __init__(self) -> None:
        self.c = dict.fromkeys(
            ("cl_swaps", "logical_steps", "executed_steps", "sqrt_ratio_calls",
             "word_searches", "executed_zero_liquidity_uninitialized"), 0)  # fmt: skip
        self._initialized = False

    @contextmanager
    def installed(self, reference: bool) -> Iterator[None]:
        c = self.c
        swap = (
            functools.partial(ORIGINAL_SWAP, skip_empty_spans=False)
            if reference
            else (ORIGINAL_SWAP)
        )
        step, ratio = cl_math.compute_swap_step_exact_in, cl_math.get_sqrt_ratio_at_tick
        search = cl_math.next_initialized_tick_within_one_word

        def counted_swap(*args: Any) -> Any:
            c["cl_swaps"] += 1
            out = swap(*args)
            c["logical_steps"] += out.steps
            return out

        def counted_search(*args: Any) -> tuple[int, bool]:
            c["word_searches"] += 1
            result = search(*args)
            self._initialized = result[1]
            return result

        def counted_step(*args: int) -> tuple[int, int, int, int]:
            c["executed_steps"] += 1
            if args[2] == 0 and not self._initialized:
                c["executed_zero_liquidity_uninitialized"] += 1
            return step(*args)

        def counted_ratio(tick: int) -> int:
            c["sqrt_ratio_calls"] += 1
            return ratio(tick)

        patches = {"swap": counted_swap, "compute_swap_step_exact_in": counted_step,
                   "get_sqrt_ratio_at_tick": counted_ratio,
                   "next_initialized_tick_within_one_word": counted_search}  # fmt: skip
        saved = {name: getattr(cl, name) for name in patches}
        for name, fn in patches.items():
            setattr(cl, name, fn)
        try:
            yield
        finally:
            for name, fn in saved.items():
                setattr(cl, name, fn)


@contextmanager
def variant(reference: bool) -> Iterator[None]:
    """Uninstrumented: only the reference toggle is patched."""
    if not reference:
        yield
        return
    cl.swap = functools.partial(ORIGINAL_SWAP, skip_empty_spans=False)  # type: ignore[assignment]
    try:
        yield
    finally:
        cl.swap = ORIGINAL_SWAP


def _experiment(path: Path) -> dict[str, Any]:
    return json.loads((path / "experiment.json").read_text())


def _bundle(experiment_dir: Path, experiment: dict[str, Any], key: str) -> Any:
    bundle_path = experiment_dir / "bundles" / key.replace("/", "-")
    bundle = load_bundle(bundle_path)
    expected = experiment["bundles"][key]["bundle_hash"]
    if bundle.bundle_hash != expected:
        raise SystemExit(f"{bundle_path}: bundle_hash {bundle.bundle_hash} != {expected}")
    return bundle


def _prepare(factory: Any, bundle: Any, profile: Any) -> Any:
    if factory.prepare is None:
        return None
    return factory.prepare(bundle, profile.algorithm_config(factory))


def _solve(factory: Any, prepared: Any, bundle: Any, profile: Any, case: Any) -> dict[str, Any]:
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
    record, _ = _independent_record(bundle, case, profile.objective, solved)
    out = json.loads(json.dumps(record.to_dict()))
    out["quotes"] = {"attempted": meter.attempted, "counted": meter.counted}
    out["_cpu_seconds"], out["_wall_seconds"] = cpu / 1e9, wall / 1e9
    return out


def _environment() -> dict[str, Any]:
    rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                         text=True, check=True).stdout.strip()  # fmt: skip
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, capture_output=True,
                           text=True, check=True).stdout.strip()  # fmt: skip
    return {"git_revision": rev, "git_dirty": bool(dirty), "python": sys.version,
            "cpu_count": os.cpu_count(), "loadavg_start": os.getloadavg()}  # fmt: skip


def work(args: argparse.Namespace) -> dict[str, Any]:
    exp_dir = Path(args.experiment)
    experiment = _experiment(exp_dir)
    profile = load_profile(REPO / experiment["profile"]["path"])
    rows: list[dict[str, Any]] = []
    for key in args.bundles.split(","):
        bundle = _bundle(exp_dir, experiment, key)
        baseline = {
            (r["algorithm"], r["case_id"]): r
            for r in load_case_records(exp_dir / "runs" / f"timing-fixed-{key.replace('/', '-')}")
        }
        for name in args.algorithms.split(","):
            factory = get_algorithm(name)
            prepared = _prepare(factory, bundle, profile)
            for case in bundle.cases:
                row: dict[str, Any] = {"bundle": key, "algorithm": name, "case": case.case_id}
                results = {}
                for label, reference in (("reference", True), ("candidate", False)):
                    counters = Counters()
                    with counters.installed(reference):
                        results[label] = _solve(factory, prepared, bundle, profile, case)
                    row[label] = counters.c | {
                        "cpu_seconds_instrumented": results[label].get("_cpu_seconds")
                    }
                ref, cand = results["reference"], results["candidate"]
                base = baseline[(name, case.case_id)]
                row["semantic_equal"] = all(ref.get(f) == cand.get(f) for f in SEMANTIC)
                row["work_equal"] = all(ref.get(f) == cand.get(f) for f in WORK)
                row["baseline_semantic_equal"] = all(cand.get(f) == base.get(f) for f in SEMANTIC)
                row["baseline_work_equal"] = all(cand.get(f) == base.get(f) for f in WORK)
                row["status"] = cand.get("status", "quote_limit")
                row["truncated_by"] = (cand.get("search") or {}).get("truncated_by")
                rows.append(row)
                print(json.dumps({k: row[k] for k in ("bundle", "algorithm", "case",
                      "semantic_equal", "work_equal", "baseline_semantic_equal",
                      "baseline_work_equal")} | {"steps": row["reference"]["logical_steps"],
                      "executed": row["candidate"]["executed_steps"]}), flush=True)  # fmt: skip
    totals = {
        label: {k: sum(r[label][k] for r in rows) for k in Counters().c}
        for label in ("reference", "candidate")
    }
    return {
        "kind": "L02 work diagnostic (instrumented; counters, not latency)",
        "experiment": str(exp_dir),
        "experiment_source": experiment["source"].get("git_revision"),
        "environment": _environment(),
        "all_semantic_equal": all(r["semantic_equal"] for r in rows),
        "all_work_equal": all(r["work_equal"] for r in rows),
        "all_baseline_semantic_equal": all(r["baseline_semantic_equal"] for r in rows),
        "all_baseline_work_equal": all(r["baseline_work_equal"] for r in rows),
        "records": len(rows),
        "totals": totals,
        "rows": rows,
    }


def paired(args: argparse.Namespace) -> dict[str, Any]:
    exp_dir = Path(args.experiment)
    experiment = _experiment(exp_dir)
    profile = load_profile(REPO / experiment["profile"]["path"])
    targets = []
    for target in args.target:
        key, name, case_id = target.split(":")
        bundle = _bundle(exp_dir, experiment, key)
        case = next(c for c in bundle.cases if c.case_id == case_id)
        factory = get_algorithm(name)
        prep_start = time.perf_counter_ns()
        prepared = _prepare(factory, bundle, profile)
        prepare_seconds = (time.perf_counter_ns() - prep_start) / 1e9
        samples: dict[str, list[dict[str, float]]] = {"reference": [], "candidate": []}
        semantic: dict[str, Any] = {}
        for pair in range(args.pairs):
            order = (True, False) if pair % 2 == 0 else (False, True)  # ABBA
            for reference in order:
                label = "reference" if reference else "candidate"
                load_before = os.getloadavg()[0]
                with variant(reference):
                    result = _solve(factory, prepared, bundle, profile, case)
                fields = {f: result.get(f) for f in SEMANTIC + WORK}
                if semantic.setdefault(label, fields) != fields:
                    raise SystemExit(f"{target}: {label} attempts are not consistent")
                samples[label].append({"cpu": result["_cpu_seconds"],
                                       "wall": result["_wall_seconds"],
                                       "load_before": load_before,
                                       "load_after": os.getloadavg()[0]})  # fmt: skip
        med = {
            label: {m: sorted(s[m] for s in v)[len(v) // 2] for m in ("cpu", "wall")}
            for label, v in samples.items()
        }
        targets.append({
            "target": target, "prepare_seconds": prepare_seconds,
            "semantic_equal": semantic["reference"] == semantic["candidate"],
            "samples": samples, "median": med,
            "median_ratio": {m: med["candidate"][m] / med["reference"][m] for m in ("cpu", "wall")},
            "max_load": max(max(s["load_before"], s["load_after"])
                            for v in samples.values() for s in v),
        })  # fmt: skip
        print(json.dumps({k: targets[-1][k] for k in ("target", "semantic_equal", "median",
                          "median_ratio", "max_load")}), flush=True)  # fmt: skip
    return {
        "kind": "L02 in-process paired timing (uninstrumented solve window; not an L01 verdict)",
        "experiment": str(exp_dir),
        "environment": _environment(),
        "pairs": args.pairs,
        "load_threshold": 0.5 * (os.cpu_count() or 1),
        "targets": targets,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
    p.add_argument("--pairs", type=int, default=5)
    p.add_argument("--out", required=True)
    args = parser.parse_args()
    run: Callable[[argparse.Namespace], dict[str, Any]] = work if args.command == "work" else paired
    result = run(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

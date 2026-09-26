"""WHI-1506 / L04 experiment evidence: exact CL traversal-prefix reuse across amounts.

Not a production module and not part of the L01 driver. It solves L01's derived bundles
in-process under two explicitly selected **post-L03** variants:

    l02_l03      CL `swap(..., skip_empty_spans=True, math_reuse=TickMathReuse)`, LB
                 `swap(..., math_reuse=BinMathReuse)` -- the control (fresh memos per solve).
    l02_l03_l04  the same plus `prefix_reuse=CLPrefixReuse(...)`, fresh per solve (cold).

Neither relies on a default; the default reference (everything off) is not re-run: its
outputs are the L01 baseline records. Only `factory.solve` runs under a variant; the
independent final evaluation (`benchmark.runner._independent_record`) runs afterwards on
the ordinary default path, so it re-checks every solved plan independently.

    work    per (bundle, algorithm, case), instrumented: both variants; every CL swap the
            solver makes (arguments and outcome or exception) digested in call order and
            compared between variants -- so every quote table/candidate score the solver
            saw is identical, not only its final answer; semantic and work fields
            compared with each other and with the L01 baseline record; CL swaps, logical
            steps (`SwapOutcome.steps`), executed `computeSwapStep` calls and the prefix
            stats. CPU times are instrumented and diagnostic only.
    shuffle per chosen target: every CL swap call of an ordinary solve, replayed in
            shuffled orders through a fresh prefix instance (L02 off and on), each complete
            outcome compared with a fresh reference-loop call.
    paired  per chosen target: one prepare, then rotated l02_l03 / l02_l03_l04 (cold)
            solves, CPU and wall around `solve` only, load around each; then one
            tracemalloc pass per variant: solve peak and bytes retained by the records.

Neither is an L01 experiment or an adopt verdict; WHI-1510 owns the performance decision.
Bounds and memo capacities are declared experiment settings, not runtime defaults.

    uv run python tools/latency/l04_prefix_reuse.py work \\
        --experiment <L01 experiment dir> --out data/latency-l04/work.json
    uv run python tools/latency/l04_prefix_reuse.py paired --experiment <L01 experiment dir> \\
        --target full_source/sentinel:uni_sor_port:<case> --pairs 3 --out data/latency-l04/p.json
"""

from __future__ import annotations

import argparse
import functools
import gc
import hashlib
import json
import os
import random
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
ORIGINAL_CL, ORIGINAL_LB, ORIGINAL_STEP = cl.swap, lb.swap, cl.compute_swap_step_exact_in
LABELS = ("l02_l03", "l02_l03_l04")


def _scalars(state: Any) -> tuple[Any, ...]:
    return tuple(getattr(state, f) for f in cl._STATE_FIELDS if f not in ("ticks", "tick_bitmap"))


class Variant:
    """Fresh (cold) per-solve instances of one explicitly selected variant."""

    def __init__(self, label: str, args: argparse.Namespace) -> None:
        self.tick = TickMathReuse(args.tick_capacity)
        self.bins = lb.BinMathReuse(args.bin_capacity)
        self.prefix = (
            cl.CLPrefixReuse(args.max_keys, args.max_checkpoints) if label == LABELS[1] else None
        )
        self.counts = dict.fromkeys(("cl_swaps", "logical_steps", "executed_steps"), 0)
        self.digest = hashlib.sha256()

    @contextmanager
    def installed(self, instrument: bool) -> Iterator[None]:
        swap = functools.partial(
            ORIGINAL_CL, skip_empty_spans=True, math_reuse=self.tick, prefix_reuse=self.prefix
        )
        counts, digest = self.counts, self.digest

        def counted_swap(state: Any, zero_for_one: bool, amount: int, limit: int) -> Any:
            counts["cl_swaps"] += 1
            call = (_scalars(state), zero_for_one, amount, limit)
            try:
                out = swap(state, zero_for_one, amount, limit)
            except Exception as exc:
                digest.update(repr((call, type(exc).__name__, str(exc))).encode())
                raise
            counts["logical_steps"] += out.steps
            new = out.new_state
            ticks = [(t, new.ticks[t]) for t in out.crossed_ticks]
            digest.update(repr((call, out.amount0, out.amount1, out.crossed_ticks, out.steps,
                                out.lm_pool_hook_calls, _scalars(new), ticks, len(new.ticks),
                                new.tick_bitmap is state.tick_bitmap)).encode())  # fmt: skip
            return out

        def counted_step(*a: int) -> tuple[int, int, int, int]:
            counts["executed_steps"] += 1
            return ORIGINAL_STEP(*a)

        cl.swap = counted_swap if instrument else swap  # type: ignore[assignment]
        lb.swap = functools.partial(ORIGINAL_LB, math_reuse=self.bins)
        if instrument:
            cl.compute_swap_step_exact_in = counted_step
        try:
            yield
        finally:
            cl.swap, lb.swap, cl.compute_swap_step_exact_in = (
                ORIGINAL_CL,
                ORIGINAL_LB,
                ORIGINAL_STEP,
            )


def _solve(factory: Any, prepared: Any, bundle: Any, profile: Any, case: Any, v: Variant,
           instrument: bool) -> dict[str, Any]:  # fmt: skip
    context = SolveContext(
        bundle=bundle,
        objective=profile.objective.bind(bundle),
        prepared=prepared,
        seed=case_seed(profile.measurement.seed, factory.name, case.case_id),
    )
    with v.installed(instrument), metered_quotes(profile.budget.max_quotes) as meter:
        cpu, wall = time.process_time_ns(), time.perf_counter_ns()
        try:
            solved = factory.solve(case, context, profile.budget)
        except QuoteLimitExceeded as exc:
            return {"quote_limit": str(exc), "quotes": {"counted": meter.counted}}
        wall, cpu = time.perf_counter_ns() - wall, time.process_time_ns() - cpu
    # Solve only: taken before the reference-path evaluation below.
    prefix = None if v.prefix is None else v.prefix.stats()
    record, _ = _independent_record(bundle, case, profile.objective, solved)
    out: dict[str, Any] = json.loads(json.dumps(record.to_dict()))
    out["quotes"] = {"attempted": meter.attempted, "counted": meter.counted}
    out["_cpu_seconds"], out["_wall_seconds"], out["_prefix"] = cpu / 1e9, wall / 1e9, prefix
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
                res, var = {}, {}
                for label in LABELS:
                    var[label] = Variant(label, args)
                    res[label] = _solve(factory, prepared, bundle, profile, case, var[label], True)
                ctl, cand = res[LABELS[0]], res[LABELS[1]]
                base = baseline[(name, case.case_id)]
                row = {
                    "bundle": key, "algorithm": name, "case": case.case_id,
                    "status": cand.get("status", "quote_limit"),
                    "cl_calls_equal": len({var[x].digest.digest() for x in LABELS}) == 1,
                    "semantic_equal": all(ctl.get(f) == cand.get(f) for f in SEMANTIC),
                    "work_equal": all(ctl.get(f) == cand.get(f) for f in WORK),
                    "baseline_semantic_equal": all(cand.get(f) == base.get(f) for f in SEMANTIC),
                    "baseline_work_equal": all(cand.get(f) == base.get(f) for f in WORK),
                    "counts": {label: var[label].counts for label in LABELS},
                    "prefix": cand["_prefix"],
                    "cpu_seconds_instrumented": {label: res[label].get("_cpu_seconds")
                                                 for label in LABELS},
                }  # fmt: skip
                rows.append(row)
                print(json.dumps({k: row[k] for k in ("bundle", "algorithm", "case",
                      "cl_calls_equal", "semantic_equal", "work_equal", "baseline_semantic_equal",
                      "baseline_work_equal", "counts")}), flush=True)  # fmt: skip
    totals = {
        label: {k: sum(r["counts"][label][k] for r in rows) for k in rows[0]["counts"][label]}
        for label in LABELS
    }
    stats = [r["prefix"] or {} for r in rows]
    prefix_totals = {
        k: sum(s.get(k, 0) for s in stats)
        for k in (
            "queries",
            "resumed",
            "reused_steps",
            "recorded_steps",
            "evictions",
            "invalidated",
        )
    } | {
        "max_keys_in_one_solve": max(s.get("keys", 0) for s in stats),
        "max_checkpoints_in_one_solve": max(s.get("checkpoints", 0) for s in stats),
    }
    return {
        "kind": "L04 work diagnostic (instrumented; post-L03 l02_l03 vs l02_l03_l04 cold)",
        "experiment": str(exp_dir),
        "experiment_source": experiment["source"].get("git_revision"),
        "environment": l02._environment(),
        "settings": {
            k: getattr(args, k)
            for k in ("tick_capacity", "bin_capacity", "max_keys", "max_checkpoints")
        },  # fmt: skip
        "records": len(rows),
        **{
            f"all_{k}": all(r[k] for r in rows)
            for k in (
                "cl_calls_equal",
                "semantic_equal",
                "work_equal",
                "baseline_semantic_equal",
                "baseline_work_equal",
            )
        },  # fmt: skip
        "totals": totals,
        "prefix_totals": prefix_totals,
        "rows": rows,
    }


def _memory(factory: Any, prepared: Any, bundle: Any, profile: Any, case: Any, label: str,
            args: argparse.Namespace) -> dict[str, Any]:  # fmt: skip
    """Instrumented tracemalloc pass: solve (+ reference evaluation) peak, record bytes."""
    gc.collect()
    tracemalloc.start()
    v = Variant(label, args)
    base_current = tracemalloc.get_traced_memory()[0]
    tracemalloc.reset_peak()
    result = _solve(factory, prepared, bundle, profile, case, v, False)
    current, peak = tracemalloc.get_traced_memory()
    retained = 0
    if v.prefix is not None:
        v.prefix.clear()
        gc.collect()
        retained = current - tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    return {"solve_peak_traced_bytes_over_start": peak - base_current,
            "prefix_record_bytes_released_by_clear": retained,
            "prefix": result["_prefix"], "fields": _fields(result)}  # fmt: skip


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
        samples: dict[str, list[dict[str, Any]]] = {label: [] for label in LABELS}
        semantic: dict[str, Any] = {}
        for pair in range(args.pairs):
            for label in LABELS if pair % 2 == 0 else LABELS[::-1]:
                load_before = os.getloadavg()[0]
                result = _solve(factory, prepared, bundle, profile, case, Variant(label, args),
                                False)  # fmt: skip
                if semantic.setdefault(label, _fields(result)) != _fields(result):
                    raise SystemExit(f"{target}: {label} attempts are not consistent")
                samples[label].append({"cpu": result["_cpu_seconds"],
                                       "wall": result["_wall_seconds"],
                                       "load_before": load_before,
                                       "load_after": os.getloadavg()[0],
                                       "prefix": result["_prefix"]})  # fmt: skip
        med = {
            label: {m: sorted(s[m] for s in v)[len(v) // 2] for m in ("cpu", "wall")}
            for label, v in samples.items()
        }
        memory = {
            label: _memory(factory, prepared, bundle, profile, case, label, args)
            for label in LABELS
        }
        consistent = semantic[LABELS[0]] == semantic[LABELS[1]]
        consistent &= all(m["fields"] == semantic[LABELS[0]] for m in memory.values())
        targets.append({
            "target": target, "semantic_equal": consistent, "samples": samples, "median": med,
            "median_ratio": {m: med[LABELS[1]][m] / med[LABELS[0]][m] for m in ("cpu", "wall")},
            "memory_instrumented": {k: {f: v for f, v in m.items() if f != "fields"}
                                    for k, m in memory.items()},
            "max_load": max(max(s["load_before"], s["load_after"])
                            for v in samples.values() for s in v),
        })  # fmt: skip
        print(json.dumps({k: targets[-1][k] for k in ("target", "semantic_equal", "median",
                          "median_ratio", "max_load")}), flush=True)  # fmt: skip
    return {
        "kind": "L04 in-process paired timing (post-L03 control vs +L04 cold; uninstrumented "
        "solve window; not an L01 verdict)",
        "experiment": str(exp_dir),
        "environment": l02._environment(),
        "settings": {
            k: getattr(args, k)
            for k in ("tick_capacity", "bin_capacity", "max_keys", "max_checkpoints")
        },  # fmt: skip
        "pairs": args.pairs,
        "load_threshold": 0.5 * (os.cpu_count() or 1),
        "targets": targets,
    }


def _outcome(swap: Callable[..., Any], call: tuple[Any, ...], **kw: Any) -> Any:
    try:
        return swap(*call, **kw)
    except Exception as exc:  # noqa: BLE001 -- the exact type and message are compared
        return (type(exc).__name__, str(exc))


def shuffle(args: argparse.Namespace) -> dict[str, Any]:
    """Order independence on real queries: capture every CL swap call of an ordinary
    (default-path) solve, then replay them in shuffled orders through a fresh
    `CLPrefixReuse` (L02 off and on) and compare each complete `SwapOutcome` -- full new
    state included -- or exception with a fresh reference-loop call."""
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
        calls: list[tuple[Any, ...]] = []

        def capture(*call: Any, sink: list[tuple[Any, ...]] = calls) -> Any:
            sink.append(call)
            return ORIGINAL_CL(*call)

        seed = case_seed(profile.measurement.seed, factory.name, case_id)
        context = SolveContext(bundle=bundle, objective=profile.objective.bind(bundle),
                               prepared=prepared, seed=seed)  # fmt: skip
        cl.swap = capture  # type: ignore[assignment]
        try:
            with metered_quotes(profile.budget.max_quotes):
                factory.solve(case, context, profile.budget)
        finally:
            cl.swap = ORIGINAL_CL
        originals = {id(p) for p in bundle.pools.values()}
        runs = []
        for seed in range(args.seeds):
            order = list(calls)
            random.Random(seed).shuffle(order)
            for skip in (False, True):
                reuse = cl.CLPrefixReuse(args.max_keys, args.max_checkpoints)
                mismatches = sum(
                    _outcome(ORIGINAL_CL, call) != _outcome(ORIGINAL_CL, call,
                                                            skip_empty_spans=skip,
                                                            prefix_reuse=reuse)
                    for call in order
                )  # fmt: skip
                runs.append({"seed": seed, "skip_empty_spans": skip, "mismatches": mismatches,
                             "prefix": reuse.stats()})  # fmt: skip
        targets.append({
            "target": target, "cl_calls": len(calls),
            "calls_on_original_states": sum(id(c[0]) in originals for c in calls),
            "distinct_states": len({id(c[0]) for c in calls}),
            "all_equal": all(r["mismatches"] == 0 for r in runs), "runs": runs,
        })  # fmt: skip
        print(json.dumps({k: targets[-1][k] for k in ("target", "cl_calls", "all_equal")}),
              flush=True)  # fmt: skip
    return {
        "kind": "L04 shuffled-order replay of captured solver CL queries vs the reference loop",
        "experiment": str(exp_dir),
        "environment": l02._environment(),
        "settings": {k: getattr(args, k) for k in ("max_keys", "max_checkpoints", "seeds")},
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
    sh = sub.add_parser("shuffle")
    sh.add_argument("--experiment", required=True)
    sh.add_argument("--target", action="append", required=True, help="bundle:algorithm:case")
    sh.add_argument("--seeds", type=int, default=2)
    sh.add_argument("--out", required=True)
    for s in (w, p, sh):  # declared experiment settings, not runtime defaults
        s.add_argument("--tick-capacity", type=int, default=16384)
        s.add_argument("--bin-capacity", type=int, default=4096)
        s.add_argument("--max-keys", type=int, default=4096)
        s.add_argument("--max-checkpoints", type=int, default=262144)
    args = parser.parse_args()
    run: Callable[[argparse.Namespace], dict[str, Any]] = {
        "work": work,
        "paired": paired,
        "shuffle": shuffle,
    }[args.command]
    result = run(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

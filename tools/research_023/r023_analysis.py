"""WHI-1627 research_023 analysis: pure functions over finished run records (contract R023-C/1
§5.3, §8, §10; schedule `config/research_023/schedule.yaml`, `rules`).

Nothing here solves or re-runs anything. Every rule below has a test in `tests/research_023/`:

- **Completeness.** Every scheduled arm x case has exactly one record (a `missing` cell, an
  unexpected case order or an absent run is a problem); every status is counted, none dropped.
- **Base-row equality.** An E1/E2 record's `search.base` (algorithm, status, quotes, search) equals
  its reference arm's record of the same case (algorithm, status, quotes.counted, search) and its
  `base_gross` equals the reference score; a hard-killed row (no `search.base`) must at least share
  the reference status.
- **Ledger, never-worse, refusal.** quotes.counted == base quotes + own quotes; the runner's own
  evaluated gross >= `base_gross`; an `unsupported_topology` refusal returns exactly the base gross;
  `invalid_plan` / `algorithm_error` where the reference was `ok` is a gate failure.
- **Controls (§5.3).** work-matched quotes <= target; call-matched calls started == target unless
  stopped; the embedded uncharged activation equals the treatment's own; a `not_reached` row stays
  in every denominator and is never a matched comparison.
- **Descriptive comparisons (§8).** Common-ok cells only for bps; every status transition kept;
  zero-baseline rows apart; nearest-rank percentiles; no p-values.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


r021: Any = _load("analysis", "tools/research_021/analysis.py")  # the 0.2.1 paired analysis

E1, E2 = "split_polish", "marginal_activation"
NOT_REACHED = "activation/control not reached: E1 truncated"
GATE_STATUSES = ("invalid_plan", "algorithm_error")
WORK_KEY = "r022_work"
VERDICTS = ("accepted", "dag_cycle", "seed_infeasible", "seed_structure", "no_gain_or_zero_flow",
            "replay_mismatch", "budget_before_validation", "stop_no_candidate")  # fmt: skip
# cooperative budget stops; `work_target` is the work-matched control's design stop, not a cut
BUDGET_STOPS = ("max_quotes", "time")
INCONCLUSIVE_SHARE = Fraction(1, 10)  # contract §10: > 10 % truncated or unsupported_topology


# ----------------------------------------------------------------------------- basics


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (`q` in (0, 1]); `None` for no values."""
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def dist(values: Sequence[float]) -> dict[str, Any]:
    """n / mean / p5 / p50 / p95 / min / max (nearest rank)."""
    values = list(values)
    return {
        "n": len(values),
        "mean": sum(values) / len(values) if values else None,
        **{f"p{int(q * 100)}": percentile(values, q) for q in (0.05, 0.5, 0.95)},
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def ratio(num: int | None, den: int | None) -> float | None:
    return None if num is None or not den else num / den


def own(record: Mapping[str, Any] | None, identity: str) -> Mapping[str, Any]:
    """`search.<identity>` of a record (empty when absent)."""
    search = (record or {}).get("search")
    value = search.get(identity) if isinstance(search, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def base_row(record: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    search = (record or {}).get("search")
    value = search.get("base") if isinstance(search, Mapping) else None
    return value if isinstance(value, Mapping) else None


def counted(record: Mapping[str, Any] | None) -> int | None:
    quotes = (record or {}).get("quotes")
    value = quotes.get("counted") if isinstance(quotes, Mapping) else None
    return int(value) if value is not None else None


def gross(record: Mapping[str, Any] | None) -> int | None:
    """The runner's own evaluated gross of an `ok` record (never the solver's claim)."""
    if not record or record.get("status") != "ok":
        return None
    evaluation = record.get("evaluation")
    value = evaluation.get("gross_output") if isinstance(evaluation, Mapping) else None
    return int(value) if value is not None else None


def is_not_reached(record: Mapping[str, Any] | None) -> bool:
    return own(record, E2).get("not_reached") == NOT_REACHED


# ----------------------------------------------------------------------------- arms


def records_of(arm: Any) -> dict[str, Mapping[str, Any] | None]:
    return {c: arm.cell(c).record for c in arm.case_ids}


def completeness(key: str, run: Any, algorithm: str, expected: Sequence[str]) -> list[str]:
    """Problems with the arm x case inventory of one run."""
    problems = []
    if list(run.algorithms) != [algorithm]:
        problems.append(f"{key}: runs {run.algorithms}, registered [{algorithm}]")
    if list(run.case_ids) != list(expected):
        problems.append(f"{key}: case order differs from the bundle's {len(expected)} cases")
    missing = [c for c in run.case_ids if run.row(c, algorithm).status == "missing"]
    if missing:
        problems.append(f"{key}: {len(missing)} scheduled case(s) without a record: {missing[:3]}")
    if run.manifest.state != "complete":
        problems.append(f"{key}: manifest state {run.manifest.state}")
    return problems


def mark_not_reached(arm: Any) -> Any:
    """`arm` with every `not_reached` cell relabelled status `not_reached` (no gross): kept in
    every denominator and status transition, never a matched (common-ok) comparison."""
    cells = {}
    for case_id, cell in arm.cells.items():
        if is_not_reached(cell.record):
            cell = replace(cell, status="not_reached", gross=None)
        cells[case_id] = cell
    return replace(arm, cells=cells)


# ----------------------------------------------------------------------------- audits


def base_audit(reference: Any, arm: Any) -> dict[str, Any]:
    """Per case: the E1/E2 record's base row equals the reference record (rules.base_equality)."""
    differences: list[dict[str, Any]] = []
    compared = 0
    for case_id in reference.case_ids:
        ref, rec = reference.cell(case_id).record or {}, arm.cell(case_id).record or {}
        row = base_row(rec)
        if row is None:  # hard-killed or crashed before a base row existed
            if rec.get("status") != ref.get("status"):
                differences.append(
                    {
                        "case_id": case_id,
                        "field": "status (no base row)",
                        "reference": ref.get("status"),
                        "arm": rec.get("status"),
                    }
                )
            continue
        compared += 1
        want = {"algorithm": ref.get("algorithm"), "status": ref.get("status"),
                "quotes": counted(ref), "search": ref.get("search")}  # fmt: skip
        for field, value in want.items():
            if row.get(field) != value:
                differences.append({"case_id": case_id, "field": field})
        identity = E1 if rec.get("algorithm") == E1 else E2
        base_gross = own(rec, identity).get("base_gross")
        if ref.get("status") == "ok" and base_gross is not None:
            if int(base_gross) != gross(ref):
                differences.append({"case_id": case_id, "field": "base_gross"})
    return {"reference": reference.label, "arm": arm.label, "scheduled": len(reference.case_ids),
            "compared": compared, "differences": differences}  # fmt: skip


def gate_audit(reference: Any, arm: Any, identity: str) -> dict[str, Any]:
    """Ledger == seam, never-worse, refusal identity and gate statuses (rules.ledger, §10)."""
    out: dict[str, list[str]] = {"ledger": [], "never_worse": [], "refusal": [], "gate_status": []}
    checked = Counter[str]()
    for case_id in arm.case_ids:
        ref, rec = reference.cell(case_id).record or {}, arm.cell(case_id).record or {}
        if rec.get("status") in GATE_STATUSES and ref.get("status") == "ok":
            out["gate_status"].append(case_id)
        row, mine = base_row(rec), own(rec, identity)
        if row is None or row.get("status") != "ok" or rec.get("status") != "ok":
            continue
        if "quotes" in mine:
            checked["ledger"] += 1
            if counted(rec) != int(row["quotes"]) + int(mine["quotes"]):
                out["ledger"].append(case_id)
        if mine.get("base_gross") is None:
            continue
        got, base = gross(rec), int(mine["base_gross"])
        checked["never_worse"] += 1
        if got is None or got < base:
            out["never_worse"].append(case_id)
        if mine.get("scope") == "unsupported_topology":
            checked["refusal"] += 1
            if got != base:
                out["refusal"].append(case_id)
    return {"arm": arm.label, "checked": dict(checked), "failures": out}


def _without_charged(activation: Any) -> Any:
    if not isinstance(activation, Mapping):
        return activation
    return {k: v for k, v in activation.items() if k != "charged"}


def control_audit(treatment: Any, control: Any) -> dict[str, Any]:
    """The §5.3 control audits of one control arm against its treatment arm."""
    rows = not_reached = matched = refused = 0
    unmatched_status: Counter[str] = Counter()  # a non-ok row (e.g. a hard `timeout`): an outcome
    stops: Counter[str] = Counter()
    over_target: list[str] = []
    call_mismatch: list[str] = []
    embedded_differs: list[str] = []
    target_differs: list[str] = []
    no_control: list[str] = []
    started = completed = converged = 0
    gains: list[float] = []
    for case_id in control.case_ids:
        rec = control.cell(case_id).record
        rows += 1
        if is_not_reached(rec):
            not_reached += 1
            continue
        if (rec or {}).get("status") != "ok":
            unmatched_status[str((rec or {}).get("status"))] += 1
            continue
        if own(rec, E2).get("scope") == "unsupported_topology":  # E1 refused: nothing to match
            refused += 1
            continue
        block = own(rec, E2).get("control")
        if not isinstance(block, Mapping):
            no_control.append(case_id)
            continue
        matched += 1
        stops[str(block.get("stop"))] += 1
        started += int(block["calls_started"])
        completed += int(block["calls_completed"])
        converged += bool(block.get("converged"))
        if block["kind"] == "work_matched":
            if int(block["quotes"]) > int(block["target_quotes"]):
                over_target.append(case_id)
        elif block["calls_started"] != block["target_calls"] and block.get("stop") is None:
            call_mismatch.append(case_id)
        mine = _without_charged(own(rec, E2).get("activation"))
        theirs = _without_charged(own(treatment.cell(case_id).record, E2).get("activation"))
        if mine != theirs:
            embedded_differs.append(case_id)
        if isinstance(theirs, Mapping) and (
            block["target_quotes"] if block["kind"] == "work_matched" else block["target_calls"]
        ) != (theirs["quotes"] if block["kind"] == "work_matched" else theirs["invocations"]):
            target_differs.append(case_id)
        e1 = own(rec, E2).get("e1", {}).get("gross")
        if e1 is not None and int(e1) > 0:
            gains.append(float(Fraction(int(block["gross"]) - int(e1), int(e1)) * 10_000))
    return {
        "treatment": treatment.label, "control": control.label, "rows": rows,
        "not_reached": not_reached, "matched": matched, "refused": refused,
        "unmatched_status": dict(sorted(unmatched_status.items())),
        "no_control_block": no_control,
        "stops": dict(sorted(stops.items())), "calls_started": started,
        "calls_completed": completed, "converged": converged,
        "over_work_target": over_target, "call_count_mismatch": call_mismatch,
        "embedded_activation_differs": embedded_differs,
        "target_differs_from_treatment": target_differs, "gain_over_e1_bps": dist(gains),
    }  # fmt: skip


# ----------------------------------------------------------------------------- descriptions


def identity_view(arm: Any, identity: str, treatment: bool = False) -> dict[str, Any]:
    """E1/E2 outcome counters of one arm: scopes, truncations, improvement, overhead, polish
    work, Brent statuses and (E2) activation outcomes."""
    scopes: Counter[str] = Counter()
    truncated: Counter[str] = Counter()
    work: Counter[str] = Counter()
    brent: Counter[str] = Counter()
    verdicts: Counter[str] = Counter()
    act_truncated: Counter[str] = Counter()
    improved = activated = invocations = not_reached = 0
    overhead: list[float] = []
    gain: list[float] = []
    for case_id in arm.case_ids:
        rec = arm.cell(case_id).record
        mine, row = own(rec, identity), base_row(rec)
        scopes[str(mine.get("scope"))] += 1
        not_reached += is_not_reached(rec)
        if mine.get("truncated_by") is not None:
            truncated[str(mine["truncated_by"])] += 1
        polish = mine.get("e1", {}).get("work") if identity == E2 else mine.get("work")
        stage = mine.get("control") or mine.get("activation") or {}
        for block in (polish or {}, stage.get("work") or {}):
            for k in ("polish_calls", "accepted", "brent_nfev", "golden_iters", "simulations"):
                work[k] += int(block.get(k, 0))
            for status, n in (block.get("brent_status") or {}).items():
                brent[str(status)] += int(n)
        if row is not None and mine.get("quotes") is not None and int(row["quotes"]) > 0:
            overhead.append(int(mine["quotes"]) / int(row["quotes"]))
        got = gross(rec)
        if got is not None and mine.get("base_gross") is not None and int(mine["base_gross"]) > 0:
            base = int(mine["base_gross"])
            improved += got > base
            gain.append(float(Fraction(got - base, base) * 10_000))
        activation = mine.get("activation")
        if treatment and isinstance(activation, Mapping):
            invocations += int(activation.get("invocations", 0))
            if activation.get("truncated_by") is not None:
                act_truncated[str(activation["truncated_by"])] += 1
            accepted = False
            for _, _, verdict in activation.get("log", []):
                name = "accepted" if str(verdict).startswith("accepted") else str(verdict)
                verdicts[name] += 1
                accepted |= name == "accepted"
            activated += accepted
    out = {
        "arm": arm.label, "scheduled": len(arm.case_ids), "scope": dict(sorted(scopes.items())),
        "truncated_by": dict(sorted(truncated.items())), "not_reached": not_reached,
        "improved": improved,
        "gain_over_base_bps": dist(gain), "overhead": dist(overhead), "work": dict(work),
        "brent_status": dict(sorted(brent.items())),
    }  # fmt: skip
    if treatment:
        out["activation"] = {"cases_activated": activated, "invocations": invocations,
                             "verdicts": {v: verdicts.get(v, 0) for v in VERDICTS},
                             "truncated_by": dict(sorted(act_truncated.items()))}  # fmt: skip
    return out


def comparison(question: str, baseline: Any, candidate: Any) -> dict[str, Any]:
    """`candidate` vs `baseline` (positive bps = candidate higher), common-ok cells only, every
    transition kept, plus per-family net wins, nearest-rank bps percentiles and paired work."""
    base, cand = mark_not_reached(baseline), mark_not_reached(candidate)
    paired = r021.paired(base, cand, comparison_class="same_domain")
    bps: list[float] = []
    work: list[float] = []
    for case_id in base.case_ids:
        a, b = base.cell(case_id), cand.cell(case_id)
        if a.status != "ok" or b.status != "ok" or a.gross is None or b.gross is None:
            continue
        value = r021.relative_bps(b.gross, a.gross)
        if value is not None:
            bps.append(value)
        w = ratio(counted(b.record), counted(a.record))
        if w is not None:
            work.append(w)
    families = paired["per_family"].values()
    return {
        "question": question, "baseline": baseline.label, "candidate": candidate.label,
        "scheduled": paired["scheduled"], "transitions": paired["transitions"],
        "common_ok": paired["common_ok"], "zero_baseline_na": paired["zero_baseline_na"],
        "only_baseline_ok": paired["only_baseline_ok"],
        "only_candidate_ok": paired["only_candidate_ok"],
        "higher": paired["pooled"]["higher"], "equal": paired["pooled"]["equal"],
        "lower": paired["pooled"]["lower"], "bps": dist(bps),
        "families": {"n": len(paired["per_family"]),
                     "net_win": sum(f["higher"] > f["lower"] for f in families),
                     "net_loss": sum(f["higher"] < f["lower"] for f in families)},
        "per_stratum": paired["per_stratum"], "work_ratio": dist(work),
        "worst_losses": paired["worst_losses"],
    }  # fmt: skip


def q5_solver_work(brent: Any, golden: Any) -> dict[str, Any]:
    """Q5: per case golden / Brent polish quotes (both > 0) and the gross comparison."""
    ratios = []
    sums = Counter[str]()
    for case_id in brent.case_ids:
        b, g = own(brent.cell(case_id).record, E1), own(golden.cell(case_id).record, E1)
        if b.get("quotes") is None or g.get("quotes") is None:
            continue
        sums["brent"] += int(b["quotes"])
        sums["golden"] += int(g["quotes"])
        if int(b["quotes"]) > 0:
            ratios.append(int(g["quotes"]) / int(b["quotes"]))
    return {"polish_quotes": dict(sums), "golden_over_brent": dist(ratios)}


def physical(ordinary: Any, work: Any) -> dict[str, Any]:
    """Physical CL / LB work of the work pass, and its cross-check against the ordinary run."""
    sums = Counter[str]()
    cells = 0
    differs: list[dict[str, Any]] = []
    for case_id in work.case_ids:
        rec, ref = work.cell(case_id).record or {}, ordinary.cell(case_id).record or {}
        block = own(rec, WORK_KEY)
        if (rec.get("status"), rec.get("score")) != (ref.get("status"), ref.get("score")):
            differs.append({"case_id": case_id, "field": "status/score"})
        if not block:
            continue
        cells += 1
        for k in ("quotes_executed", "cl_swap_steps", "cl_initialized_ticks_crossed",
                  "lb_bins_swapped"):  # fmt: skip
            sums[k] += int(block[k])
        if int(block["quotes_executed"]) != counted(rec):
            differs.append({"case_id": case_id, "field": "quotes_executed != quotes.counted"})
    return {"arm": ordinary.label, "cells": cells, "sums": dict(sums), "differs": differs}


def wall(arm: Any) -> dict[str, Any]:
    """Solve seconds (secondary evidence only: a shared multi-lane host, no L01 protocol)."""
    values = [c.solve_seconds for c in arm.cells.values() if c.solve_seconds is not None]
    return {"sum_seconds": sum(values), **dist(values)}


# ----------------------------------------------------------------------------- disposition


def disposition(
    identity_arms: Sequence[str],
    gates: Mapping[str, Mapping[str, Any]],
    bases: Mapping[str, Mapping[str, Any]],
    controls: Mapping[str, Mapping[str, Any]],
    views: Mapping[str, Mapping[str, Any]],
    statuses: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Contract §10 over the arms of one identity: `reject` on any gate failure, `inconclusive`
    when a question cannot be evaluated (> 10 % of an arm's scheduled cases cut by a budget stop
    or refused as `unsupported_topology`, or no `ok` cell), otherwise `keep_experimental`.
    Reasons are listed per arm."""
    reject: list[str] = []
    inconclusive: list[str] = []
    for key in identity_arms:
        if key not in gates:
            inconclusive.append(f"{key}: no completed run")
            continue
        for name, cases in gates[key]["failures"].items():
            if cases:
                reject.append(f"{key}: {name} on {len(cases)} case(s)")
        if bases[key]["differences"]:  # the question compares different bases on those cells
            n = len(bases[key]["differences"])
            inconclusive.append(f"{key}: base row differs from the reference on {n} cell(s)")
        view, scheduled = views[key], views[key]["scheduled"]
        budget = sum(n for reason, n in view["truncated_by"].items() if reason in BUDGET_STOPS)
        killed = statuses[key]["statuses"].get("timeout", 0)  # the runner's hard limit
        cut = budget + killed + view["scope"].get("unsupported_topology", 0)
        if Fraction(cut, scheduled) > INCONCLUSIVE_SHARE:
            inconclusive.append(
                f"{key}: {cut}/{scheduled} truncated (incl. timeout) or unsupported_topology"
            )
        if statuses[key]["statuses"].get("ok", 0) == 0:
            inconclusive.append(f"{key}: no ok cell (base unavailable)")
    for key, audit in controls.items():
        unmatched = {name: len(audit[name]) for name in (
            "over_work_target", "call_count_mismatch", "target_differs_from_treatment")
            if audit[name]}  # fmt: skip
        if unmatched:
            reject.append(f"{key}: control not matched {unmatched}")
    verdict = "reject" if reject else "inconclusive" if inconclusive else "keep_experimental"
    return {"disposition": verdict, "reject": reject, "inconclusive": inconclusive}


# ----------------------------------------------------------------------------- stage


def comparisons_of(arms: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, str, str]]:
    """(question, baseline arm, candidate arm), contract §8 Q1-Q4, in a fixed order."""
    out: list[tuple[str, str, str]] = []
    for key, spec in arms.items():
        if spec["kind"] == "e1":
            out.append(("Q1", str(spec["base"]), key))
    out += [("Q2", ref, "E1b-A0") for ref in ("C100", "C200")]
    out += [("Q2", ref, "E1g-A0") for ref in ("C100", "C200")]
    treatments = [k for k, s in arms.items() if s["kind"] == "e2" and s["e2_arm"] == "treatment"]
    for key in treatments:
        for control in (k for k, s in arms.items() if s.get("treatment") == key):
            out.append(("Q3", control, key))
    for key in treatments:
        out.append(("Q4", str(arms[key]["base"]), key))
        out += [("Q4", ref, key) for ref in ("C100", "C200", "M4", "S4", "REP")
                if ref != arms[key]["base"]]  # fmt: skip
    out.append(("Q5", "E1g-A0", "E1b-A0"))
    return sorted(set(out), key=out.index)


def analyze_stage(
    raw: Mapping[str, Any],
    arms: Mapping[str, Mapping[str, Any]],
    stage: str,
    runs: Mapping[str, Any],
    expected: Sequence[str],
) -> dict[str, Any]:
    """The whole stage analysis from loaded runs (`report.aggregate.RunData` per invocation id)."""
    problems: list[str] = []
    loaded: dict[str, Any] = {}
    work: dict[str, Any] = {}
    for key, spec in arms.items():
        for prefix, target in ((f"{stage}-", loaded), (f"{stage}-WP-", work)):
            run = runs.get(f"{prefix}{key}")
            if run is None:
                continue
            problems += completeness(f"{prefix}{key}", run, spec["algorithm"], expected)
            target[key] = r021.arm_from_run(run, spec["algorithm"], key)
    statuses = {k: r021.status_counts(a) for k, a in loaded.items()}
    bases: dict[str, Any] = {}
    gates: dict[str, Any] = {}
    views: dict[str, Any] = {}
    for key, spec in arms.items():
        if spec["kind"] == "reference" or key not in loaded or spec["base"] not in loaded:
            continue
        identity = E1 if spec["kind"] == "e1" else E2
        bases[key] = base_audit(loaded[str(spec["base"])], loaded[key])
        gates[key] = gate_audit(loaded[str(spec["base"])], loaded[key], identity)
        views[key] = identity_view(loaded[key], identity, spec.get("e2_arm") == "treatment")
        problems += [f"{key}: base row {d['field']} differs on {d['case_id']}"
                     for d in bases[key]["differences"]]  # fmt: skip
        problems += [f"{key}: {name} fails on {c}" for name, cases in gates[key]["failures"].items()
                     for c in cases]  # fmt: skip
    controls: dict[str, Any] = {}
    for key, spec in arms.items():
        treatment = spec.get("treatment")
        if treatment and key in loaded and treatment in loaded:
            audit = controls[key] = control_audit(loaded[str(treatment)], loaded[key])
            for name in ("over_work_target", "call_count_mismatch", "embedded_activation_differs",
                         "target_differs_from_treatment", "no_control_block"):  # fmt: skip
                problems += [f"{key}: {name} on {c}" for c in audit[name]]
    compared = []
    for question, baseline, candidate in comparisons_of(arms):
        if baseline in loaded and candidate in loaded:
            compared.append(comparison(question, loaded[baseline], loaded[candidate]))
    q5 = (q5_solver_work(loaded["E1b-A0"], loaded["E1g-A0"])
          if {"E1b-A0", "E1g-A0"} <= set(loaded) else None)  # fmt: skip
    phys = {k: physical(loaded[k], w) for k, w in work.items() if k in loaded}
    for key, view in phys.items():
        base = arms[key].get("base")
        if base in phys:
            view["vs_base"] = {unit: ratio(n, phys[str(base)]["sums"].get(unit))
                               for unit, n in view["sums"].items()}  # fmt: skip
    for key, view in phys.items():
        problems += [f"WP-{key}: {d['field']} differs on {d['case_id']}" for d in view["differs"]]
    identities = {
        E1: [k for k, s in arms.items() if s["kind"] == "e1"],
        E2: [k for k, s in arms.items() if s["kind"] == "e2"],
    }
    dispositions = {
        name: disposition(keys, gates, bases,
                          {k: v for k, v in controls.items() if k in keys}, views, statuses)
        for name, keys in identities.items()
    }  # fmt: skip
    per_arm = {
        key: disposition([key], gates, bases,
                         {k: v for k, v in controls.items() if k == key}, views, statuses)
        for key in gates
    }  # fmt: skip
    return {
        "schema": "r023.analysis/1", "arms": {k: dict(v) for k, v in arms.items()},
        "scheduled_cases": len(expected), "statuses": statuses, "base_rows": bases,
        "gates": gates, "identity": views, "controls": controls, "comparisons": compared,
        "q5": q5, "physical": phys, "wall": {k: wall(a) for k, a in loaded.items()},
        "dispositions": dispositions, "arm_dispositions": per_arm, "problems": problems,
    }  # fmt: skip


# ----------------------------------------------------------------------------- tables


def _f(value: Any, digits: int = 3) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _pct(value: Any) -> str:
    return "–" if value is None else f"{100 * value:.1f} %"


def _statuses(counts: Mapping[str, int]) -> str:
    return (
        ", ".join(f"{'none' if k == 'None' else k} {v}" for k, v in sorted(counts.items())) or "–"
    )


def _table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(_f(c) for c in row) + " |" for row in rows]
    return [*lines, ""]


def _comparison_rows(items: Sequence[Mapping[str, Any]]) -> list[list[Any]]:
    rows = []
    for c in items:
        bps, fam = c["bps"], c["families"]
        rows.append([
            f"`{c['candidate']}` vs `{c['baseline']}`", c["scheduled"], c["common_ok"],
            len(c["zero_baseline_na"]), f"{c['higher']}/{c['equal']}/{c['lower']}",
            bps["mean"], bps["p5"], bps["p50"], bps["p95"], f"{fam['net_win']}/{fam['net_loss']}"
            f" of {fam['n']}", c["work_ratio"]["p50"], c["work_ratio"]["p95"],
            _statuses({k: v for k, v in c["transitions"].items() if k != "ok->ok"}),
        ])  # fmt: skip
    return rows


def _stratum(group: Mapping[str, Any] | None) -> str:
    if not group:
        return "–"
    return f"{group['higher']}/{group['equal']}/{group['lower']} ({_f(group['median'])})"


COMPARISON_HEADER = ("candidate vs baseline", "scheduled", "common ok", "zero-baseline",
                     "H/E/L", "mean bps", "p5", "p50", "p95", "families net +/−",
                     "work p50", "work p95", "non-ok transitions")  # fmt: skip


def render_tables(analysis: Mapping[str, Any]) -> str:
    """The compact Markdown tables of `results.md`, regenerated from one analysis JSON."""
    a = analysis
    out = [f"<!-- GENERATED by tools/research_023/campaign.py tables from the stage-{a['stage']} "
           "analysis; do not edit by hand -->", "",
           f"Stage {a['stage']}: bundle `{a['bundle']}` (`{a['bundle_hash'][:12]}…`), "
           f"{a['scheduled_cases']} cases per arm; source revision(s) "
           f"{', '.join(f'`{g[:12]}`' for g in a['git_revisions'])}; "
           f"{len(a['problems'])} check problem(s).", ""]  # fmt: skip
    out += ["### Statuses and denominators (every scheduled arm x case)", ""]
    rows = [[f"`{k}`", a["arms"][k]["algorithm"], s["scheduled"], _statuses(s["statuses"]),
             _statuses(s["timeout_by_limit"]), s["last_valid_candidates"]]
            for k, s in a["statuses"].items()]  # fmt: skip
    rows += [[f"`{n['arm']}`", "–", 0, n["disposition"], "–", "–"] for n in a["not_scheduled"]]
    out += _table(("arm", "identity", "scheduled", "statuses", "timeout by limit",
                   "last valid candidates"), rows)  # fmt: skip
    out += ["### Audits: base row, ledger, never-worse, refusal (per E1/E2 arm)", ""]
    rows = []
    for k, b in a["base_rows"].items():
        g = a["gates"][k]
        rows.append([f"`{k}`", f"`{b['reference']}`", b["compared"], len(b["differences"]),
                     g["checked"].get("ledger", 0), len(g["failures"]["ledger"]),
                     g["checked"].get("never_worse", 0), len(g["failures"]["never_worse"]),
                     g["checked"].get("refusal", 0), len(g["failures"]["refusal"]),
                     len(g["failures"]["gate_status"])])  # fmt: skip
    out += _table(("arm", "reference", "base rows compared", "differ", "ledger checked",
                   "ledger ≠ seam", "never-worse checked", "worse", "refusals",
                   "refusal ≠ base", "invalid/error where ref ok"), rows)  # fmt: skip
    out += ["### E2 control audits (contract §5.3)", ""]
    rows = [[f"`{k}`", f"`{c['treatment']}`", c["rows"], c["not_reached"],
             _statuses(c["unmatched_status"]), c["refused"], c["matched"],
             len(c["over_work_target"]), len(c["call_count_mismatch"]),
             f"{c['calls_started']}/{c['calls_completed']}", c["converged"], _statuses(c["stops"]),
             len(c["embedded_activation_differs"]), len(c["target_differs_from_treatment"]),
             c["gain_over_e1_bps"]["mean"]]
            for k, c in a["controls"].items()]  # fmt: skip
    out += _table(("control", "treatment", "rows", "E1 truncated (not reached)",
                   "non-ok rows (unmatched)", "E1 refused", "matched",
                   "q > work target", "calls ≠ target (no stop)", "calls started/completed",
                   "converged", "stop reasons", "embedded activation ≠ treatment",
                   "target ≠ treatment's spend", "mean gain over E1 bps"), rows)  # fmt: skip
    out += ["### E1/E2 outcome counters", ""]
    rows = [[f"`{k}`", _statuses(v["scope"]), _statuses(v["truncated_by"]), v["not_reached"],
             v["improved"],
             v["gain_over_base_bps"]["mean"], v["gain_over_base_bps"]["p50"],
             v["gain_over_base_bps"]["p95"], _pct(v["overhead"]["p50"]),
             _pct(v["overhead"]["p95"]), v["work"].get("polish_calls", 0),
             v["work"].get("accepted", 0), _statuses(v["brent_status"])]
            for k, v in a["identity"].items()]  # fmt: skip
    out += _table(("arm", "scope", "truncated by", "E1 truncated (not reached)", "improved",
                   "gain vs own base mean bps", "p50",
                   "p95", "overhead p50", "overhead p95", "polish calls", "accepted exchanges",
                   "Brent status (SciPy flag count)"), rows)  # fmt: skip
    rows = [[f"`{k}`", v["activation"]["cases_activated"], v["activation"]["invocations"],
             _statuses({n: c for n, c in v["activation"]["verdicts"].items() if c}),
             _statuses(v["activation"]["truncated_by"])]
            for k, v in a["identity"].items() if "activation" in v]  # fmt: skip
    if rows:
        out += ["### E2 activation outcomes (treatments)", ""]
        out += _table(("arm", "cases with an accepted activation", "optimiser invocations",
                       "verdicts (all iterations)", "activation truncated by"), rows)  # fmt: skip
    for q, title in (("Q1", "Q1 — E1 additive value over its own base"),
                     ("Q2", "Q2 — E1 vs finer granularity"),
                     ("Q3", "Q3 — E2 vs its matched controls"),
                     ("Q4", "Q4 — E2 vs references"),
                     ("Q5", "Q5 — Brent vs golden (gross)")):  # fmt: skip
        items = [c for c in a["comparisons"] if c["question"] == q]
        if items:
            out += [f"### {title}", ""]
            out += _table(COMPARISON_HEADER, _comparison_rows(items))
    strata = sorted({k for c in a["comparisons"] for k in c["per_stratum"]})
    if strata:
        out += ["### Per stratum (H/E/L, median bps of the common-ok cells)", ""]
        rows = [[c["question"], f"`{c['candidate']}` vs `{c['baseline']}`",
                 *(_stratum(c["per_stratum"].get(k)) for k in strata)]
                for c in a["comparisons"]]  # fmt: skip
        out += _table(("Q", "candidate vs baseline", *strata), rows)
    if a.get("q5"):
        q5 = a["q5"]
        out += ["### Q5 — solver work (paired polish quotes, Brent vs golden on A0)", ""]
        out += _table(("polish quotes Brent", "polish quotes golden", "golden/Brent p50",
                       "golden/Brent p95", "cases"),
                      [[q5["polish_quotes"].get("brent"), q5["polish_quotes"].get("golden"),
                        q5["golden_over_brent"]["p50"], q5["golden_over_brent"]["p95"],
                        q5["golden_over_brent"]["n"]]])  # fmt: skip
    out += ["### Physical work (untimed work pass; E2 controls have none)", ""]
    rows = []
    for k, p in a["physical"].items():
        vs = p.get("vs_base") or {}
        rows.append([f"`{k}`", p["cells"], p["sums"].get("quotes_executed"),
                     p["sums"].get("cl_swap_steps"), p["sums"].get("lb_bins_swapped"),
                     vs.get("quotes_executed"), vs.get("cl_swap_steps"), vs.get("lb_bins_swapped"),
                     len(p["differs"])])  # fmt: skip
    out += _table(("arm", "cells", "quotes executed", "CL swap steps", "LB bins swapped",
                   "quotes / base arm", "CL steps / base arm", "LB bins / base arm",
                   "work pass ≠ ordinary"), rows)  # fmt: skip
    out += ["### Wall time (secondary only; shared multi-lane host; no speed claim)", ""]
    rows = [[f"`{k}`", w["sum_seconds"] / 3600, w["p50"], w["p95"], w["max"]]
            for k, w in a["wall"].items()]  # fmt: skip
    out += _table(("arm", "solve hours", "p50 s", "p95 s", "max s"), rows)
    out += ["### Dispositions (contract §10)", ""]
    rows = [
        [f"`{k}`", d["disposition"], "; ".join(d["reject"] + d["inconclusive"]) or "–"]
        for k, d in (*a["dispositions"].items(), *a["arm_dispositions"].items())
    ]
    rows += [[f"`{n['arm']}`", n["disposition"], n["reason"]] for n in a["not_scheduled"]]
    out += _table(("identity / arm", "disposition", "reasons"), rows)
    return "\n".join(out)

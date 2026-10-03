"""WHI-1602 analysis of the 0.2.2 bound-pruning campaign (pure functions over run records).

Protocol: `docs/references/research-022/pruning-contract.md` §11 (+ §8, §14), registered in
`config/research_022/schedule.yaml`. Everything here reads already-written run records
(`benchmark.results`) and never solves. The rules enforced, each with a test in
`tests/research_022/`:

- **Exactness first (§11.2).** A (reference, bounded) cell is compared only when neither side is
  budget-truncated; its status, error, score, evaluation (the independent replay of the plan),
  solver-reported outcome, and every counter outside the §8.1 allowed-to-differ column are equal.
  A cell with a truncated side is *excluded and listed*; `bounded truncated and reference not
  truncated` is its own list. Any difference is a defect, never an input to anything.
- **Units are never mixed (§11.4).** Every unit is summed and distributed (n / p50 / p90 / max)
  on its own; the only ratio is bounded/reference of the *same* unit; a missing value is counted
  as missing, never 0; the bound's cost columns are never netted against savings.
- **Bound preparation is read from the runner's prepare events** (§14 A1.1), not from `search`.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

PAIRS: dict[str, str] = {
    "single_path": "single_path_bounded",
    "incremental_graph": "incremental_graph_bounded",
    "metis_history": "metis_history_bounded",
}
BOUNDED_OF = {v: k for k, v in PAIRS.items()}
BUDGET_CAUSES = ("max_candidates", "max_quotes")
# a status for which the attempt produced no usable search: the cell is cut, not compared
FAILED_STATUSES = frozenset({"timeout", "cancelled", "algorithm_error", "model_error",
                             "incomplete_snapshot", "missing"})  # fmt: skip

# §8.1 allowed-to-differ columns (search keys). `bound_pruning` is the added key.
WORK_ONLY = frozenset({"marginal_failures", "marginal_incomplete", "incomplete_example",
                       "quotes_executed", "quotes_memoized"})  # fmt: skip
SINGLE_PATH_WORK = frozenset({"paths_evaluated", "paths_pruned", "quotes_executed",
                              "quotes_memoized", "failed_candidates", "paths_incomplete",
                              "incomplete_example"})  # fmt: skip
M2_POPULATION = frozenset({"label_relaxations", "label_rejected_cycle", "label_pruned_distance",
                           "label_skipped_revisit", "label_truncated_chunks",
                           "labels_dropped_signature_cap", "labels_dropped_frontier_cap",
                           "peak_signature_labels", "certified_strict_insertions"})  # fmt: skip
R021_WORK_POPULATION = frozenset({"label_relaxations", "labels_discarded_dominance",
                                  "labels_retained_unknown", "state_comparisons",
                                  "peak_frontier_labels", "admission_checks"})  # fmt: skip
ALLOWED_SEARCH: dict[str, frozenset[str]] = {
    "single_path_bounded": SINGLE_PATH_WORK,
    "incremental_graph_bounded": WORK_ONLY,
    "metis_history_bounded": WORK_ONLY,
}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (`q` in (0, 1]); `None` for no values."""
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def distribution(values: Sequence[float | int | None]) -> dict[str, Any]:
    """Sum and n / p50 / p90 / max of the present values; `missing` counts the `None`s."""
    present = [v for v in values if v is not None]
    return {
        "n": len(present),
        "missing": len(values) - len(present),
        "sum": sum(present) if present else None,
        "p50": percentile(present, 0.5),
        "p90": percentile(present, 0.9),
        "max": max(present) if present else None,
    }


# ----------------------------------------------------------------------------- truncation


def search_of(record: Mapping[str, Any] | None) -> Mapping[str, Any]:
    search = record.get("search") if record else None
    return search if isinstance(search, Mapping) else {}


def cut_reason(record: Mapping[str, Any] | None, names: Sequence[str]) -> str | None:
    """Why this run's search was cut (`None` = not budget-truncated: `truncated_by` is `None` or
    `state_cap`, §11.2). A failed attempt (timeout, a `max_quotes` hard stop recorded as a
    timeout, a worker error, no record) is cut too; the chunk strategies' own stage name in
    `truncated_stages` only mirrors their `truncated_by` (A1.2) and is not a second cause."""
    if record is None:
        return "missing"
    status = str(record.get("status"))
    if status in FAILED_STATUSES:
        limit = record.get("limit_hit")
        return f"status:{status}" + (f"/{limit}" if limit else "")
    search = search_of(record)
    truncated_by = search.get("truncated_by")
    if truncated_by not in (None, "state_cap"):
        return f"truncated_by:{truncated_by}"
    stages = [s for s in search.get("truncated_stages") or [] if s not in names]
    if stages:
        return f"truncated_stages:{','.join(stages)}"
    return None


# ----------------------------------------------------------------------------- exactness


def _m2_active(record: Mapping[str, Any]) -> bool:
    block = search_of(record).get("bound_pruning")
    m2 = block.get("m2") if isinstance(block, Mapping) else None
    return bool(isinstance(m2, Mapping) and m2.get("active"))


def _r021_view(search: Mapping[str, Any], population: bool) -> Any:
    block = search.get("r021")
    if not isinstance(block, Mapping):
        return None
    view = json.loads(json.dumps(block, default=str))
    view.pop("algorithm", None)
    view.pop("stages", None)  # observational seconds
    work = view.get("work")
    if isinstance(work, dict):
        for key in ("quotes_executed", "quotes_memoized"):
            work.pop(key, None)
        if population:
            for key in R021_WORK_POPULATION:
                work.pop(key, None)
    return view


def _strip(value: Any) -> Any:
    """`value` without the observational `stages` seconds and the strategy's own name, at every
    depth (the diagnostics record names the run it checked against)."""
    if isinstance(value, Mapping):
        return {k: _strip(v) for k, v in value.items() if k not in ("stages", "algorithm")}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def _diagnostics(value: Any, population: bool) -> Any:
    """The record-level `diagnostics` (the `r021` check block) without what §8.1 lets differ: the
    executed / memoized quotes (and the runner's count of them under `checked_against`) and, under
    an active M2, the label-population counters of its `work` block."""
    view = _strip(value)
    if isinstance(view, dict):
        work = view.get("work")
        if isinstance(work, dict):
            for key in ("quotes_executed", "quotes_memoized"):
                work.pop(key, None)
            if population:
                for key in R021_WORK_POPULATION:
                    work.pop(key, None)
        checked = view.get("checked_against")
        if isinstance(checked, dict):
            checked.pop("quotes_counted", None)
    return view


def differences(ref: Mapping[str, Any], bnd: Mapping[str, Any], bounded_id: str) -> list[str]:
    """Every §8.1 identity that fails between two non-truncated records (empty = identical)."""
    out: list[str] = []
    m2 = _m2_active(bnd)
    for key in ("status", "error", "score", "evaluation", "solver_reported", "limit_hit"):
        if canonical(ref.get(key)) != canonical(bnd.get(key)):
            out.append(key)
    if not m2:
        for key in ("candidates_considered", "candidates_truncated"):
            if ref.get(key) != bnd.get(key):
                out.append(key)
    s_ref, s_bnd = search_of(ref), search_of(bnd)
    if set(s_bnd) != set(s_ref) | {"bound_pruning"}:
        out.append("search_keys:" + ",".join(sorted(set(s_bnd) ^ (set(s_ref) | {"bound_pruning"}))))
    allowed = ALLOWED_SEARCH[bounded_id] | {"bound_pruning", "r021"}
    if m2:
        allowed |= M2_POPULATION
    out += [f"search.{k}" for k in sorted(s_ref) if k in s_bnd and k not in allowed
            and canonical(s_ref[k]) != canonical(s_bnd[k])]  # fmt: skip
    if canonical(_r021_view(s_ref, m2)) != canonical(_r021_view(s_bnd, m2)):
        out.append("search.r021")
    d_ref, d_bnd = ref.get("diagnostics"), bnd.get("diagnostics")
    if (d_ref is not None or d_bnd is not None) and canonical(_diagnostics(d_ref, m2)) != canonical(
        _diagnostics(d_bnd, m2)
    ):
        out.append("diagnostics")
    return out


def exactness(
    refs: Mapping[str, Mapping[str, Any]],
    bnds: Mapping[str, Mapping[str, Any]],
    bounded_id: str,
    case_ids: Sequence[str],
) -> dict[str, Any]:
    """The §11.2 gate of one (reference, bounded) pair over `case_ids` (record dicts by case id).
    `compared` counts cells where neither side is truncated; `identical` those that pass."""
    ref_id = BOUNDED_OF[bounded_id]
    names = (ref_id, bounded_id)
    excluded: list[dict[str, Any]] = []
    bad_cut: list[dict[str, Any]] = []
    different: list[dict[str, Any]] = []
    label_disagrees: list[str] = []
    new_failure: list[str] = []
    compared = identical = 0
    for case_id in case_ids:
        ref, bnd = refs.get(case_id), bnds.get(case_id)
        r_cut, b_cut = cut_reason(ref, names), cut_reason(bnd, names)
        if r_cut is None and b_cut is not None:
            bad_cut.append({"case": case_id, "bounded": b_cut})
            continue
        if r_cut is not None or b_cut is not None:
            excluded.append({"case": case_id, "reference": r_cut, "bounded": b_cut})
            continue
        assert ref is not None and bnd is not None
        compared += 1
        block = search_of(bnd).get("bound_pruning")
        label = block.get("exactness", {}).get("label") if isinstance(block, Mapping) else None
        if label != "exact":
            label_disagrees.append(case_id)
        if bnd["status"] in ("invalid_plan", "algorithm_error") and ref["status"] != bnd["status"]:
            new_failure.append(case_id)
        diff = differences(ref, bnd, bounded_id)
        if diff:
            different.append({"case": case_id, "keys": diff})
        else:
            identical += 1
    return {
        "reference": ref_id,
        "bounded": bounded_id,
        "scheduled": len(case_ids),
        "compared": compared,
        "identical": identical,
        "excluded_as_truncated": excluded,
        "bounded_truncated_reference_not": bad_cut,
        "differences": different,
        "label_disagrees_with_truncation": label_disagrees,
        "new_failure_status": new_failure,
    }


# ----------------------------------------------------------------------------- baseline


def deterministic_view(record: Mapping[str, Any]) -> Any:
    """The order-check's notion of a record's deterministic output (`benchmark.runner`)."""
    from benchmark.runner import _deterministic_view

    return _deterministic_view(record)


def baseline_compare(
    new: Mapping[tuple[str, str], Mapping[str, Any]],
    old: Mapping[tuple[str, str], Mapping[str, Any]],
    algorithms: Sequence[str],
) -> dict[str, Any]:
    """§11.2 last sentence: the reference IDs at HEAD against the 0.2.1 baseline records
    (status, score, plan replay, `search` and the rest of the deterministic view)."""
    per: dict[str, Any] = {}
    for algorithm in algorithms:
        keys = sorted({c for (a, c) in (*new, *old) if a == algorithm})
        same = 0
        missing: list[str] = []
        differing: list[dict[str, Any]] = []
        for case_id in keys:
            n, o = new.get((algorithm, case_id)), old.get((algorithm, case_id))
            if n is None or o is None:
                missing.append(case_id)
                continue
            a, b = deterministic_view(n), deterministic_view(o)
            if canonical(a) == canonical(b):
                same += 1
            else:
                bad = sorted(
                    k for k in a.keys() | b.keys() if canonical(a.get(k)) != canonical(b.get(k))
                )
                differing.append({"case": case_id, "keys": bad})
        per[algorithm] = {"cells": len(keys), "identical": same, "missing": missing,
                          "differing": differing}  # fmt: skip
    return per


# ----------------------------------------------------------------------------- families


def case_kind(case_id: str) -> str:
    return case_id.split("-", 1)[0]


def case_stratum(case_id: str) -> str:
    parts = case_id.split("-")
    return "boundary" if parts[0] == "bnd" else (parts[3] if len(parts) > 3 else "?")


def case_pair(case_id: str) -> str:
    parts = case_id.split("-")
    return f"{parts[1]}->{parts[2]}" if len(parts) > 2 else "?"


FAMILY_ABBREVIATION = {"constant_product": "cp", "concentrated": "cl", "liquidity_book": "lb"}


def plan_mix(record: Mapping[str, Any] | None, pool_family: Mapping[str, str]) -> str:
    """The pool families of the plan's steps (`cl+lb`), from the independent replay's trace."""
    evaluation = record.get("evaluation") if record else None
    trace = evaluation.get("trace") if isinstance(evaluation, Mapping) else None
    if not trace:
        return "none"
    kinds = {FAMILY_ABBREVIATION.get(pool_family.get(s.get("pool_id", ""), "?"), "?")
             for s in trace}  # fmt: skip
    return "+".join(sorted(kinds))


def family_keys(
    case_id: str, cohort: str, ref: Mapping[str, Any] | None, pool_family: Mapping[str, str]
) -> dict[str, str]:
    return {
        "cohort": cohort,
        "direct": "no_direct" if case_kind(case_id) == "nod" else "has_direct",
        "mix": plan_mix(ref, pool_family),
        "stratum": case_stratum(case_id),
        "pair": case_pair(case_id),
    }


# ----------------------------------------------------------------------------- work


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bound(record: Mapping[str, Any] | None, key: str) -> int | None:
    block = search_of(record).get("bound_pruning")
    return _int(block.get(key)) if isinstance(block, Mapping) else None


# unit name -> reader of one record (reference or bounded). `None` = the unit is absent.
def _search_unit(key: str) -> Callable[[Mapping[str, Any] | None], int | None]:
    return lambda rec: _int(search_of(rec).get(key))


def _work_unit(key: str) -> Callable[[Mapping[str, Any] | None], int | None]:
    def read(rec: Mapping[str, Any] | None) -> int | None:
        block = search_of(rec).get("r022_work")
        return _int(block.get(key)) if isinstance(block, Mapping) else None

    return read


SAVINGS_UNITS: dict[str, dict[str, Callable[[Mapping[str, Any] | None], int | None]]] = {
    "single_path": {
        "quotes_executed": _search_unit("quotes_executed"),
        "paths_evaluated": _search_unit("paths_evaluated"),
    },
    "incremental_graph": {
        "quotes_executed": _search_unit("quotes_executed"),
        "paths_scored": _search_unit("paths_scored"),
    },
    "metis_history": {
        "quotes_executed": _search_unit("quotes_executed"),
        "paths_scored": _search_unit("paths_scored"),
        "label_relaxations": _search_unit("label_relaxations"),
    },
}
WORK_PASS_UNITS = {"cl_swap_steps": _work_unit("cl_swap_steps"),
                   "lb_bins_swapped": _work_unit("lb_bins_swapped"),
                   "quotes_executed(work_pass)": _work_unit("quotes_executed")}  # fmt: skip
BOUND_UNITS = ("pruned_bound", "bound_evaluations", "bound_no_bound", "bound_table_cost")


def unit_row(ref_values: Sequence[int | None], bnd_values: Sequence[int | None]) -> dict[str, Any]:
    """One unit over the cases: both sides' sum/distribution, and the same-unit ratio over the
    cases where both sides have the value (a missing side is counted, never 0)."""
    pairs = [(a, b) for a, b in zip(ref_values, bnd_values, strict=True)
             if a is not None and b is not None]  # fmt: skip
    ref_sum, bnd_sum = sum(a for a, _ in pairs), sum(b for _, b in pairs)
    return {
        "cases_with_both": len(pairs),
        "reference": distribution(ref_values),
        "bounded": distribution(bnd_values),
        "ratio_bounded_over_reference": None if ref_sum == 0 else bnd_sum / ref_sum,
        "saved": ref_sum - bnd_sum,
    }


def work_table(
    refs: Mapping[str, Mapping[str, Any]],
    bnds: Mapping[str, Mapping[str, Any]],
    bounded_id: str,
    case_ids: Sequence[str],
) -> dict[str, Any]:
    """Per-unit table of one pair over `case_ids`: savings units (reference vs bounded), the
    bound's own cost units (bounded only), and the derived skipped fractions."""
    ref_id = BOUNDED_OF[bounded_id]
    cases = [c for c in case_ids if c in refs and c in bnds]
    units: dict[str, Any] = {}
    for name, read in {**SAVINGS_UNITS[ref_id], **WORK_PASS_UNITS}.items():
        r = [read(refs[c]) for c in cases]
        b = [read(bnds[c]) for c in cases]
        if all(v is None for v in (*r, *b)):
            continue  # the unit does not exist in these records (e.g. no work pass)
        units[name] = unit_row(r, b)
    cost = {name: distribution([_bound(bnds[c], name) for c in cases]) for name in BOUND_UNITS}
    pruned = [_bound(bnds[c], "pruned_bound") for c in cases]
    considered = [_int(bnds[c].get("candidates_considered")) for c in cases]
    same_unit = {"single_path": "paths_evaluated", "incremental_graph": "paths_scored",
                 "metis_history": "label_relaxations"}[ref_id]  # fmt: skip
    reference_unit = [SAVINGS_UNITS[ref_id][same_unit](refs[c]) for c in cases]

    def fraction(num: Sequence[int | None], den: Sequence[int | None]) -> dict[str, Any]:
        pairs = [(a, b) for a, b in zip(num, den, strict=True) if a is not None and b]
        total = sum(b for _, b in pairs)
        return {
            "cases": len(pairs),
            "sum_over_sum": sum(a for a, _ in pairs) / total if total else None,
            "cases_with_skips": sum(1 for a, _ in pairs if a > 0),
        }

    return {
        "cases": len(cases),
        "units": units,
        "bound_cost": cost,
        "skipped_fraction_of_candidates_considered": fraction(pruned, considered),
        f"skipped_fraction_of_reference_{same_unit}": fraction(pruned, reference_unit),
    }


def work_by_family(
    refs: Mapping[str, Mapping[str, Any]],
    bnds: Mapping[str, Mapping[str, Any]],
    bounded_id: str,
    case_ids: Sequence[str],
    families: Mapping[str, Mapping[str, str]],
) -> dict[str, dict[str, Any]]:
    """`work_table` per value of every case family (cohort, direct, mix, stratum, pair)."""
    out: dict[str, dict[str, Any]] = {}
    for kind in ("cohort", "direct", "mix", "stratum", "pair"):
        values = sorted({families[c][kind] for c in case_ids if c in families})
        out[kind] = {
            v: work_table(refs, bnds, bounded_id, [c for c in case_ids
                                                    if families.get(c, {}).get(kind) == v])
            for v in values
        }  # fmt: skip
    return out


def gate_states(bnds: Mapping[str, Mapping[str, Any]], case_ids: Sequence[str]) -> dict[str, Any]:
    """Re-derived `G_M2` / `p0` states of the bounded cells (a missing block is counted apart)."""
    gate: Counter[str] = Counter()
    active = p0 = missing = 0
    rules: Counter[str] = Counter()
    labels: Counter[str] = Counter()
    for case_id in case_ids:
        block = search_of(bnds.get(case_id)).get("bound_pruning")
        if not isinstance(block, Mapping):
            missing += 1
            continue
        m2 = block.get("m2")
        if isinstance(m2, Mapping):
            gate[str(m2.get("gate"))] += 1
            active += bool(m2.get("active"))
        p0 += bool(block.get("p0"))
        rules[str(block.get("rule"))] += 1
        labels[str(block.get("exactness", {}).get("label"))] += 1
    return {
        "cells": len(case_ids), "missing_block": missing, "m2_gate": dict(gate),
        "m2_active": active, "p0_true": p0, "rules": dict(rules), "labels": dict(labels),
    }  # fmt: skip


def prepare_seconds(prepare_events: Sequence[Mapping[str, Any]], algorithm: str) -> dict[str, Any]:
    """Preparation seconds of one algorithm from the runner's prepare events (§14 A1.1)."""
    seconds = [float(e["prepare_seconds"]) for e in prepare_events
               if e.get("algorithm") == algorithm and e.get("pass", "timing") == "timing"
               and isinstance(e.get("prepare_seconds"), int | float)]  # fmt: skip
    return {"events": len(seconds), "seconds": seconds}


def cost_columns(
    prepare_events: Sequence[Mapping[str, Any]], ref_id: str, bounded_id: str
) -> dict[str, Any]:
    """The bound's preparation cost, next to the savings: the runner's prepare events of the
    reference and the bounded strategy and their difference (descriptive, never netted)."""
    ref_p = prepare_seconds(prepare_events, ref_id)["seconds"]
    bnd_p = prepare_seconds(prepare_events, bounded_id)["seconds"]
    return {
        "prepare_seconds_reference": ref_p,
        "prepare_seconds_bounded": bnd_p,
        "bound_prepare_seconds": [b - r for r, b in zip(ref_p, bnd_p, strict=False)],
    }


# ----------------------------------------------------------------------------- A5


def binding_budget_view(
    refs: Mapping[str, Mapping[str, Any]],
    bnds: Mapping[str, Mapping[str, Any]],
    bounded_id: str,
    case_ids: Sequence[str],
) -> dict[str, Any]:
    """§11.3: what the bounded run did with the budget the reference exhausted. Reported apart
    under `not_exact_budget_binding`, never as exactness evidence."""
    ref_id = BOUNDED_OF[bounded_id]
    names = (ref_id, bounded_id)
    quadrant: Counter[str] = Counter()
    higher: list[str] = []
    lower: list[str] = []
    equal = 0
    for case_id in case_ids:
        ref, bnd = refs.get(case_id), bnds.get(case_id)
        r_cut, b_cut = cut_reason(ref, names), cut_reason(bnd, names)
        quadrant[
            ("ref_cut" if r_cut else "ref_clean") + "/" + ("bnd_cut" if b_cut else "bnd_clean")
        ] += 1
        if ref and bnd and ref["status"] == "ok" and bnd["status"] == "ok":
            a, b = _int_str(ref.get("score")), _int_str(bnd.get("score"))
            if a is not None and b is not None:
                if b > a:
                    higher.append(case_id)
                elif b < a:
                    lower.append(case_id)
                else:
                    equal += 1
    return {"cells": len(case_ids), "truncation": dict(quadrant),
            "score_vs_reference": {"higher": len(higher), "equal": equal, "lower": len(lower)},
            "higher_cases": higher, "lower_cases": lower,
            "bounded_truncated_reference_not": [
                c for c in case_ids if cut_reason(refs.get(c), names) is None
                and cut_reason(bnds.get(c), names) is not None]}  # fmt: skip


def _int_str(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def budget_values(reference_records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The registered A5 rule (`schedule.yaml` `a5.rule`): nearest-rank p25 / p50 of the
    reference's own `quotes_executed` (max_quotes) and `candidates_considered`
    (max_candidates) over its non-truncated `ok` tuning cells with a positive value."""
    quotes: list[int] = []
    candidates: list[int] = []
    for rec in reference_records:
        if rec.get("status") != "ok":
            continue
        q, c = _int(search_of(rec).get("quotes_executed")), _int(rec.get("candidates_considered"))
        if q:
            quotes.append(q)
        if c:
            candidates.append(c)
    return {
        "cells": {"quotes_executed": len(quotes), "candidates_considered": len(candidates)},
        "max_quotes": {"p25": percentile(quotes, 0.25), "p50": percentile(quotes, 0.5)},
        "max_candidates": {"p25": percentile(candidates, 0.25), "p50": percentile(candidates, 0.5)},
    }

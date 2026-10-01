"""WHI-1562 research-021 paired-campaign analysis (pure functions; no solver, no I/O).

Contract: `docs/references/research-021/contract.md` (R021-C/1) §§3.4, 5, 6, 8 and the
campaign pre-registration `docs/references/research-021/preregistration.md`. Every function
reads already-normalized rows (`report.aggregate.Row`, one per scheduled (case, algorithm)
cell, `status == "missing"` for a scheduled cell without a record) and never re-solves.

Rules enforced here (each has a behavioural test in `tests/research_021/`):

- **Unconditional first.** Status counts use the full schedule as the denominator; a
  failure, `unsupported`, `timeout` or missing record is counted, never dropped.
- **Paired quality only on common-OK cases.** A pair is scored only when both cells are `ok`
  with an evaluated gross; a missing score is N/A, **never 0** (`None` stays `None`). A zero
  baseline gross makes that case's relative value N/A (counted apart). Two arms whose
  scheduled case lists differ are refused (`AnalysisError`), never silently intersected.
- **Gains and losses together.** Every paired result reports higher / equal / lower counts,
  the worst losses by case, and per-family (directed token pair) and per-stratum splits
  beside the pooled distribution; cases sharing a pair or stratum are correlated (§6.4), so
  no pooled p-value is computed.
- **Units are never mixed.** Work counters are summarized per registered unit; a ratio is
  only formed between two arms of the same unit (`same_unit_ratio`).
- **Ratios, not added bps, across denominators.** Depth decomposition works on per-case log
  ratios over one common-OK case set (`Q_L4/Q_E3 = (Q_E4/Q_E3)(Q_L4/Q_E4)`), §6.2.
- **Timing is descriptive unless clean.** A window whose sampled 1-minute load exceeds
  `0.5 × logical CPUs` (or that has no load sample) is `inconclusive`; one sample per case
  gives no A/A noise floor, so no batch speed verdict is issued here; no single-request
  percentile is ever computed (`T_SINGLE_PERCENTILE`).
- **Bounds.** A missing or non-certified bound is reported as its kind (`unknown`,
  `estimate`, `unavailable`), never as zero; an estimate is never a certificate.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

OK = "ok"
MISSING = "missing"
# Statuses that are outcomes of a failed or cut attempt (never scope, never a route verdict).
FAILURE_STATUSES = frozenset(
    {"timeout", "invalid_plan", "algorithm_error", "model_error", "incomplete_snapshot",
     "cancelled", MISSING}
)  # fmt: skip
WORST_LOSSES = 10


class AnalysisError(ValueError):
    """The inputs cannot be analysed as asked (unlike schedules, unknown arm, bad rule)."""


@dataclass(frozen=True)
class Cell:
    """One scheduled (case, algorithm) outcome, as the analysis needs it."""

    case_id: str
    status: str
    gross: int | None
    record: Mapping[str, Any] | None = None
    solve_seconds: float | None = None
    limit_hit: str | None = None

    @property
    def search(self) -> Mapping[str, Any]:
        search = self.record.get("search") if self.record else None
        return search if isinstance(search, Mapping) else {}

    @property
    def r021(self) -> Mapping[str, Any]:
        value = self.search.get("r021")
        return value if isinstance(value, Mapping) else {}


@dataclass
class Arm:
    """One (invocation, algorithm) row set over its scheduled cases, in schedule order."""

    label: str
    algorithm: str
    case_ids: list[str]
    cells: dict[str, Cell]
    families: dict[str, str] = field(default_factory=dict)  # case -> directed token pair
    strata: dict[str, str] = field(default_factory=dict)  # case -> stratum
    splits: dict[str, str] = field(default_factory=dict)  # case -> tuning | report | ...
    prepare_events: list[Mapping[str, Any]] = field(default_factory=list)

    def cell(self, case_id: str) -> Cell:
        return self.cells[case_id]


def arm_from_run(run: Any, algorithm: str, label: str) -> Arm:
    """An `Arm` from a `report.aggregate.RunData` (duck-typed to keep this module pure)."""
    if algorithm not in run.algorithms:
        raise AnalysisError(f"{label}: algorithm {algorithm!r} is not in the run {run.algorithms}")
    cells: dict[str, Cell] = {}
    for case_id in run.case_ids:
        row = run.row(case_id, algorithm)
        cells[case_id] = Cell(
            case_id=case_id,
            status=row.status,
            gross=row.gross if row.status == OK else None,
            record=row.record,
            solve_seconds=row.solve_seconds,
            limit_hit=row.limit_hit,
        )
    families, strata, splits = {}, {}, {}
    for case_id in run.case_ids:
        context = run.cases[case_id]
        families[case_id] = f"{(context.token_in or '?')[:8]}->{(context.token_out or '?')[:8]}"
        strata[case_id] = context.stratum
        splits[case_id] = context.split
    events = [e for e in run.manifest.prepare_events if e.get("algorithm") == algorithm]
    return Arm(label, algorithm, list(run.case_ids), cells, families, strata, splits, events)


# ----------------------------------------------------------------------------- basics


def _median(values: Sequence[float]) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def summary(values: Iterable[float]) -> dict[str, Any]:
    """n / min / median / mean / max; no tail percentile (small, correlated samples)."""
    ordered = sorted(values)
    n = len(ordered)
    return {
        "n": n,
        "min": ordered[0] if n else None,
        "median": _median(ordered),
        "mean": sum(ordered) / n if n else None,
        "max": ordered[-1] if n else None,
    }


def relative_bps(candidate: int, baseline: int) -> float | None:
    """(candidate − baseline) / baseline × 10⁴, exact until the final float; N/A at 0."""
    if baseline <= 0:
        return None
    return float(Fraction(candidate - baseline, baseline) * 10_000)


def log_ratio(candidate: int, baseline: int) -> float | None:
    if baseline <= 0 or candidate <= 0:
        return None
    return math.log(candidate) - math.log(baseline)


def unsupported_reason(cell: Cell) -> str | None:
    scope = cell.r021.get("scope")
    if isinstance(scope, Mapping) and scope.get("supported") is False:
        return str(scope.get("reason"))
    record = cell.record or {}
    error = record.get("error")
    return str(error) if error else None


def status_counts(arm: Arm) -> dict[str, Any]:
    """Every status over the full schedule; unsupported reasons and timeout limits kept."""
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    limits: Counter[str] = Counter()
    last_valid = 0
    for case_id in arm.case_ids:
        cell = arm.cell(case_id)
        counts[cell.status] += 1
        if cell.status == "unsupported":
            reasons[unsupported_reason(cell) or "unstated"] += 1
        if cell.status == "timeout":
            limits[cell.limit_hit or "solver_budget"] += 1
        record = cell.record or {}
        last_valid += record.get("last_valid_candidate") is not None
    return {
        "arm": arm.label,
        "algorithm": arm.algorithm,
        "scheduled": len(arm.case_ids),
        "statuses": dict(sorted(counts.items())),
        "unsupported_reasons": dict(sorted(reasons.items())),
        "timeout_by_limit": dict(sorted(limits.items())),
        "last_valid_candidates": last_valid,
        "failures": sum(v for k, v in counts.items() if k in FAILURE_STATUSES),
    }


def _same_schedule(a: Arm, b: Arm) -> None:
    if a.case_ids != b.case_ids:
        only_a = sorted(set(a.case_ids) - set(b.case_ids))
        only_b = sorted(set(b.case_ids) - set(a.case_ids))
        raise AnalysisError(
            f"{a.label} and {b.label} have unlike schedules (only in first {only_a[:3]}…, only "
            f"in second {only_b[:3]}…, or a different order): refused, never intersected"
        )


def _group(values: list[tuple[str, float]]) -> dict[str, Any]:
    bps = [v for _, v in values]
    return {
        "n": len(bps),
        "higher": sum(1 for v in bps if v > 0),
        "equal": sum(1 for v in bps if v == 0),
        "lower": sum(1 for v in bps if v < 0),
        **{k: summary(bps)[k] for k in ("min", "median", "max")},
    }


def paired(baseline: Arm, candidate: Arm, *, comparison_class: str) -> dict[str, Any]:
    """Candidate vs baseline gross over the identical schedule (positive = candidate
    higher). Common-OK cases only for quality; every status transition is reported."""
    _same_schedule(baseline, candidate)
    transitions: Counter[str] = Counter()
    bps_rows: list[tuple[str, float]] = []
    logs: list[float] = []
    zero_baseline: list[str] = []
    only_baseline_ok = only_candidate_ok = 0
    for case_id in baseline.case_ids:
        a, b = baseline.cell(case_id), candidate.cell(case_id)
        transitions[f"{a.status}->{b.status}"] += 1
        a_ok, b_ok = a.status == OK and a.gross is not None, b.status == OK and b.gross is not None
        if a_ok and not b_ok:
            only_baseline_ok += 1
        if b_ok and not a_ok:
            only_candidate_ok += 1
        if not (a_ok and b_ok):
            continue
        assert a.gross is not None and b.gross is not None
        value = relative_bps(b.gross, a.gross)
        if value is None:
            zero_baseline.append(case_id)
            continue
        bps_rows.append((case_id, value))
        lr = log_ratio(b.gross, a.gross)
        if lr is not None:
            logs.append(lr)
    by_family: dict[str, list[tuple[str, float]]] = defaultdict(list)
    by_stratum: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for case_id, value in bps_rows:
        by_family[baseline.families.get(case_id, "unlabeled")].append((case_id, value))
        by_stratum[baseline.strata.get(case_id, "unlabeled")].append((case_id, value))
    worst = sorted((v, c) for c, v in bps_rows if v < 0)[:WORST_LOSSES]
    best = sorted(((v, c) for c, v in bps_rows if v > 0), reverse=True)[:WORST_LOSSES]
    return {
        "baseline": baseline.label,
        "candidate": candidate.label,
        "class": comparison_class,
        "ranked": comparison_class == "same_domain",
        "scheduled": len(baseline.case_ids),
        "transitions": dict(sorted(transitions.items())),
        "common_ok": len(bps_rows) + len(zero_baseline),
        "only_baseline_ok": only_baseline_ok,
        "only_candidate_ok": only_candidate_ok,
        "zero_baseline_na": zero_baseline,
        "pooled": _group(bps_rows) | {
            "mean_bps": summary([v for _, v in bps_rows])["mean"],
            "geometric_mean_ratio": math.exp(sum(logs) / len(logs)) if logs else None,
        },
        "per_family": {k: _group(v) for k, v in sorted(by_family.items())},
        "per_stratum": {k: _group(v) for k, v in sorted(by_stratum.items())},
        "worst_losses": [{"case_id": c, "bps": v} for v, c in worst],
        "largest_gains": [{"case_id": c, "bps": v} for v, c in best],
    }


# ----------------------------------------------------------------------------- identity


def budget_bound(cell: Cell) -> bool:
    """A cell whose search a budget cut (hard limit, declared truncation, timeout): an
    identical output proves nothing about exactness (L01 §7)."""
    search = cell.search
    return bool(
        cell.status == "timeout" or cell.limit_hit or search.get("truncated_by")
        or search.get("incremental_status") == "truncated"
    )  # fmt: skip


def _plan_view(cell: Cell) -> Any:
    record = cell.record or {}
    quotes = record.get("quotes") or {}
    return {
        "status": cell.status,
        "score": record.get("score"),
        "evaluation": record.get("evaluation"),
        "quotes_counted": quotes.get("counted") if isinstance(quotes, Mapping) else None,
    }


def identity(
    reference: Arm, variant: Arm, *, keys: Sequence[str] = ("status", "score", "evaluation")
) -> dict[str, Any]:
    """Cells that must be identical (a registered identity gate): compared on `keys` of the
    plan view; budget-bound cells on either side are excluded and listed, never passed."""
    _same_schedule(reference, variant)
    checked, differing, bound, missing = 0, [], [], []
    for case_id in reference.case_ids:
        a, b = reference.cell(case_id), variant.cell(case_id)
        if a.status == MISSING or b.status == MISSING:
            missing.append(case_id)
            continue
        if budget_bound(a) or budget_bound(b):
            bound.append(case_id)
            continue
        checked += 1
        va, vb = _plan_view(a), _plan_view(b)
        fields = [k for k in keys if va.get(k) != vb.get(k)]
        if fields:
            differing.append({"case_id": case_id, "fields": fields})
    return {
        "reference": reference.label,
        "variant": variant.label,
        "keys": list(keys),
        "checked": checked,
        "identical": checked - len(differing),
        "differing": differing,
        "missing": missing,
        "budget_bound_excluded": bound,
        "gate": "pass" if not differing and not missing else "fail",
    }


def not_below(control: Arm, variant: Arm) -> dict[str, Any]:
    """P1-style gate: on common-OK cells the variant's gross is never below the control's
    unless the variant was hard-killed/budget-cut (then listed apart, not passed)."""
    _same_schedule(control, variant)
    below: list[str] = []
    excused: list[str] = []
    ok_to_failure: list[str] = []
    for case_id in control.case_ids:
        a, b = control.cell(case_id), variant.cell(case_id)
        if a.status == OK and b.status != OK:
            (excused if budget_bound(b) else ok_to_failure).append(case_id)
            continue
        if a.status == OK and b.status == OK and a.gross is not None and b.gross is not None:
            if b.gross < a.gross:
                (excused if budget_bound(b) else below).append(case_id)
    return {
        "control": control.label,
        "variant": variant.label,
        "below_without_budget_cut": below,
        "ok_to_failure_without_budget_cut": ok_to_failure,
        "budget_cut_listed_apart": excused,
        "gate": "pass" if not below and not ok_to_failure else "fail",
    }


def equal_value(reference: Arm, variant: Arm) -> dict[str, Any]:
    """Same-domain value equality on cells both solved without a budget cut (e.g.
    `same_grid_allocation` P1: a difference is a defect signal, never a gain)."""
    _same_schedule(reference, variant)
    compared, differ, skipped = 0, [], Counter[str]()
    for case_id in reference.case_ids:
        a, b = reference.cell(case_id), variant.cell(case_id)
        if not (a.status == OK and b.status == OK):
            skipped[f"{a.status}->{b.status}"] += 1
            continue
        if budget_bound(a) or budget_bound(b):
            skipped["budget_bound"] += 1
            continue
        compared += 1
        if a.gross != b.gross:
            differ.append({"case_id": case_id, "reference": a.gross, "variant": b.gross})
    return {
        "reference": reference.label,
        "variant": variant.label,
        "compared": compared,
        "differing": differ,
        "not_compared": dict(sorted(skipped.items())),
        "gate": "pass" if not differ else "fail",
        "evaluable": compared > 0,
    }


# ----------------------------------------------------------------------------- work / timing


def work_units(arm: Arm) -> dict[str, Any]:
    """Per registered unit (search_stats.r021.work) and the runner's quote counter:
    n reporting, n missing (never 0-filled), min/median/max/sum. Units side by side only."""
    values: dict[str, list[float]] = defaultdict(list)
    missing: Counter[str] = Counter()
    units: set[str] = set()
    for case_id in arm.case_ids:
        work = arm.cell(case_id).r021.get("work")
        if isinstance(work, Mapping):
            units.update(str(k) for k in work)
    quotes: list[float] = []
    for case_id in arm.case_ids:
        cell = arm.cell(case_id)
        record = cell.record or {}
        q = (record.get("quotes") or {}).get("counted") if record else None
        if isinstance(q, int):
            quotes.append(float(q))
        work = cell.r021.get("work")
        for unit in units:
            value = work.get(unit) if isinstance(work, Mapping) else None
            if isinstance(value, int) and not isinstance(value, bool):
                values[unit].append(float(value))
            else:
                missing[unit] += 1
    out = {u: {**summary(values[u]), "sum": sum(values[u]), "missing": missing[u]}
           for u in sorted(units)}  # fmt: skip
    return {
        "arm": arm.label,
        "runner_quotes_counted": {**summary(quotes), "sum": sum(quotes),
                                  "missing": len(arm.case_ids) - len(quotes)},  # fmt: skip
        "units": out,
    }


def same_unit_ratio(baseline: Arm, candidate: Arm, unit: str) -> dict[str, Any]:
    """Per-case candidate/baseline ratio of ONE unit both arms report (never across units)."""
    _same_schedule(baseline, candidate)
    ratios: list[float] = []
    for case_id in baseline.case_ids:
        wa = baseline.cell(case_id).r021.get("work")
        wb = candidate.cell(case_id).r021.get("work")
        if unit == "quotes_counted":
            ra, rb = baseline.cell(case_id).record, candidate.cell(case_id).record
            va = (ra or {}).get("quotes", {}).get("counted") if ra else None
            vb = (rb or {}).get("quotes", {}).get("counted") if rb else None
        else:
            va = wa.get(unit) if isinstance(wa, Mapping) else None
            vb = wb.get(unit) if isinstance(wb, Mapping) else None
        if isinstance(va, int) and isinstance(vb, int) and va > 0:
            ratios.append(vb / va)
    return {"unit": unit, "baseline": baseline.label, "candidate": candidate.label,
            "ratio": summary(ratios)}  # fmt: skip


def timing(arm: Arm) -> dict[str, Any]:
    """Descriptive per-arm solve / evaluation / start-up / prepare seconds (one sample per
    case in a batch run). Never a verdict (see `timing_verdict`)."""
    solve, evaluation = [], []
    for case_id in arm.case_ids:
        cell = arm.cell(case_id)
        if cell.solve_seconds is not None:
            solve.append(cell.solve_seconds)
        measurement = (cell.record or {}).get("measurement") or {}
        ev = measurement.get("evaluation_seconds") if isinstance(measurement, Mapping) else None
        if isinstance(ev, (int, float)):
            evaluation.append(float(ev))
    stages: dict[str, list[float]] = defaultdict(list)
    for case_id in arm.case_ids:
        observed = arm.cell(case_id).r021.get("stages")
        if isinstance(observed, Mapping):
            for key, value in observed.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    stages[str(key)].append(float(value))
    events = [e for e in arm.prepare_events if e.get("pass", "timing") == "timing"]
    prepare = [float(e["prepare_seconds"]) for e in events
               if isinstance(e.get("prepare_seconds"), (int, float))]  # fmt: skip
    startup = [float(e["startup_seconds"]) for e in events
               if isinstance(e.get("startup_seconds"), (int, float))]  # fmt: skip
    return {
        "arm": arm.label,
        "solve_seconds": {**summary(solve), "sum": sum(solve)},
        "evaluation_seconds": {**summary(evaluation), "sum": sum(evaluation)},
        "prepare_seconds": {**summary(prepare), "events": len(prepare)},
        "startup_including_prepare_seconds": {**summary(startup), "events": len(startup)},
        # observed per-stage seconds inside the solve (R021-C/1 §9.4): observations, not budgets
        "solver_stage_seconds": {k: summary(v) for k, v in sorted(stages.items())},
    }


def host_window(
    samples: Sequence[Mapping[str, Any]], start: float, end: float, logical_cpus: int
) -> dict[str, Any]:
    """L01 host rule over a time window: contaminated iff some 1-minute load sample in
    [start, end] exceeds 0.5 × logical CPUs; no sample → unknown (treated as inconclusive)."""
    threshold = 0.5 * logical_cpus
    inside = [float(s["load1"]) for s in samples if start <= float(s["t"]) <= end]
    if not inside:
        return {"threshold": threshold, "samples": 0, "max_load1": None, "state": "unknown"}
    over = sum(1 for v in inside if v > threshold)
    return {
        "threshold": threshold,
        "samples": len(inside),
        "max_load1": max(inside),
        "samples_over": over,
        "state": "contaminated" if over else "clean",
    }


def timing_verdict(hosts: Sequence[Mapping[str, Any]], *, noise_floor_available: bool) -> str:
    """`inconclusive` unless every window is clean AND an A/A noise floor exists; a batch
    run with one sample per case has none, so its timing is descriptive only."""
    if any(h.get("state") != "clean" for h in hosts):
        return "inconclusive (host load or no load sample)"
    if not noise_floor_available:
        return "descriptive only (one sample per case: no A/A noise floor, no speed verdict)"
    return "eligible for the L01 comparator"


def memory_view(
    records: Sequence[Mapping[str, Any]], prepare_events: Sequence[Mapping[str, Any]],
    algorithm: str,
) -> dict[str, Any]:
    """Separate tracemalloc pass (DESIGN §2.10): per-case solve peak and per-worker
    prepare peak bytes, descriptive (no pre-registered memory tolerance exists)."""
    solve = [float(r["solve_peak_bytes"]) for r in records if r.get("algorithm") == algorithm
             and isinstance(r.get("solve_peak_bytes"), int)]  # fmt: skip
    statuses = Counter(str(r.get("status")) for r in records if r.get("algorithm") == algorithm)
    prepare = [float(e["prepare_peak_bytes"]) for e in prepare_events
               if e.get("algorithm") == algorithm and e.get("pass") == "memory"
               and isinstance(e.get("prepare_peak_bytes"), int)]  # fmt: skip
    return {"algorithm": algorithm, "solve_peak_bytes": summary(solve),
            "prepare_peak_bytes": summary(prepare), "memory_statuses": dict(statuses)}


# ----------------------------------------------------------------------------- views


def certificate_view(arm: Arm) -> dict[str, Any]:
    """Bound kinds as recorded (null certificate -> its unavailable reason), the runner's
    diagnostics check state and codes. Missing bounds are never zero."""
    kinds: Counter[str] = Counter()
    checks: Counter[str] = Counter()
    codes: Counter[str] = Counter()
    terminations: Counter[str] = Counter()
    for case_id in arm.case_ids:
        cell = arm.cell(case_id)
        certificate = cell.r021.get("certificate")
        if isinstance(certificate, Mapping):
            kinds[str(certificate.get("bound_kind"))] += 1
            terminations[str(certificate.get("termination"))] += 1
        elif cell.r021:
            kinds[f"none ({cell.r021.get('certificate_unavailable_reason')})"] += 1
        else:
            kinds["no r021 record"] += 1
        diagnostics = (cell.record or {}).get("diagnostics")
        if isinstance(diagnostics, Mapping):
            checks[str(diagnostics.get("state"))] += 1
            for code in diagnostics.get("codes") or []:
                codes[str(code)] += 1
    return {
        "arm": arm.label,
        "bound_kinds": dict(sorted(kinds.items())),
        "terminations": dict(sorted(terminations.items())),
        "runner_check_states": dict(sorted(checks.items())),
        "invalid_certificate_codes": dict(sorted(codes.items())),
        "certified_claims": kinds.get("certified", 0),
    }


def domain_view(arm: Arm) -> dict[str, Any]:
    """The recorded `r021.domain/1` of an arm's cells: distinct domain hashes and the
    grid / split / hop / reuse / zero-leg rules they name (a same-domain pair must agree)."""
    hashes: set[str] = set()
    rules: Counter[str] = Counter()
    for case_id in arm.case_ids:
        r021 = arm.cell(case_id).r021
        domain = r021.get("domain")
        if not isinstance(domain, Mapping):
            continue
        hashes.add(str(r021.get("candidate_domain_hash")))
        grid = domain.get("amount_grid")
        view = {k: domain.get(k) for k in ("hops", "splits", "pool_reuse", "zero_output_leg",
                                            "token_reuse", "full_fill", "protocols")}  # fmt: skip
        view["amount_grid"] = {k: v for k, v in grid.items()} if isinstance(grid, Mapping) else grid
        rules[json.dumps(view, sort_keys=True)] += 1
    return {"arm": arm.label, "distinct_domain_hashes": len(hashes),
            "rules": {k: v for k, v in sorted(rules.items())}}  # fmt: skip


def _counter(values: Iterable[Any]) -> dict[str, int]:
    return dict(sorted(Counter(str(v) for v in values).items()))


def cycle_safe_view(reference: Arm, variant: Arm) -> dict[str, Any]:
    """`matched_sor_cycle_safe` attribution (cycle-safe-sor.md §11): rejections, reference
    trajectory, CS-2 publication (withheld_by_cs2 vs the reference's streamed candidate),
    cooperative replay cut vs hard kill, token-cycle outcomes. Zero counters on a cell whose
    selector never completed are `unavailable`, never an identity."""
    _same_schedule(reference, variant)
    trajectory, phases = [], []
    withheld = published = rejections = 0
    hard_kill = {"reference": 0, "variant": 0}
    ref_candidate_without_variant = 0
    comparable_identical: list[str] = []
    for case_id in variant.case_ids:
        r, v = reference.cell(case_id), variant.cell(case_id)
        block = v.search.get("cycle_safe")
        block = block if isinstance(block, Mapping) else {}
        trajectory.append(block.get("reference_trajectory", "unavailable"))
        replay = block.get("replay")
        phases.append(replay.get("phase") if isinstance(replay, Mapping) else "unavailable")
        publication = block.get("publication")
        if isinstance(publication, Mapping):
            withheld += bool(publication.get("withheld_by_cs2"))
            published += bool(publication.get("published"))
        rejections += int(block.get("combinations_rejected_cycle") or 0) > 0
        for side, cell in (("reference", r), ("variant", v)):
            if cell.limit_hit in ("time", "quotes"):
                hard_kill[side] += 1
        r_lvc = (r.record or {}).get("last_valid_candidate") is not None
        v_lvc = (v.record or {}).get("last_valid_candidate") is not None
        ref_candidate_without_variant += r_lvc and not v_lvc
        if (
            block.get("reference_trajectory") == "identical"
            and block.get("comparable_completed") is True
            and not budget_bound(r)
        ):
            comparable_identical.append(case_id)
    return {
        "reference": reference.label,
        "variant": variant.label,
        "reference_trajectory": _counter(trajectory),
        "replay_phase": _counter(phases),
        "cases_with_rejections": rejections,
        "publication": {"published": published, "withheld_by_cs2": withheld},
        "hard_kills": hard_kill,
        "reference_last_valid_candidate_without_variant": ref_candidate_without_variant,
        "variant_invalid_plan": sum(1 for c in variant.case_ids
                                    if variant.cell(c).status == "invalid_plan"),  # fmt: skip
        "comparable_identical_cases": comparable_identical,
    }


def cfmm_view(arm: Arm) -> dict[str, Any]:
    """Stage, termination (numerical residual criterion only), recovery failures, labelled
    fallbacks, estimate coverage (initial full-network converged solve only) and the
    recovered/estimate ratio where an estimate exists. Never an integer bound."""
    terminations, failures, fallbacks, withheld, stages = [], [], [], [], []
    violations: list[str] = []
    ratios: list[float] = []
    estimates = 0
    for case_id in arm.case_ids:
        cell = arm.cell(case_id)
        block = cell.search.get("cfmm")
        if not isinstance(block, Mapping):
            continue
        stages.append(block.get("stage"))
        terminations.append(block.get("termination"))
        if block.get("recovery_failure"):
            failures.append(block.get("recovery_failure"))
        fb = block.get("fallback")
        if isinstance(fb, Mapping):
            fallbacks.append(fb.get("reason"))
        if block.get("estimate_withheld"):
            withheld.append(block.get("estimate_withheld"))
        estimate = block.get("estimate")
        initial = block.get("initial")
        initial_termination = initial.get("termination") if isinstance(initial, Mapping) else None
        if isinstance(estimate, Mapping) and (
            isinstance(fb, Mapping) or initial_termination != "converged"
        ):
            violations.append(case_id)  # an estimate outside the initial converged solve
        if isinstance(estimate, Mapping) and cell.gross is not None:
            estimates += 1
            value = float(str(estimate.get("value")))
            if value > 0:
                ratios.append(cell.gross / value)
    return {
        "arm": arm.label,
        "stage": _counter(stages),
        "termination": _counter(terminations),
        "recovery_failures": _counter(failures),
        "fallbacks": _counter(fallbacks),
        "estimate_present": estimates,
        "estimate_withheld": _counter(withheld),
        "recovered_over_estimate": summary(ratios),
        "estimate_rule_violations": violations,
        "gate": "pass" if not violations else "fail",
    }


def repair_view(arm: Arm) -> dict[str, Any]:
    stops, accepted, consistency, complete = [], 0, 0, 0
    for case_id in arm.case_ids:
        block = arm.cell(case_id).search.get("repair")
        if not isinstance(block, Mapping):
            continue
        complete += 1
        stops.append(block.get("stop"))
        accepted += int(block.get("accepted") or 0)
        consistency += int(block.get("consistency_failures") or 0)
    return {"arm": arm.label, "records_with_repair_block": complete, "stop": _counter(stops),
            "accepted": accepted, "consistency_failures": consistency}  # fmt: skip


def history_view(arm: Arm) -> dict[str, Any]:
    capped, frontier_drops, truncated = [], 0, []
    for case_id in arm.case_ids:
        search = arm.cell(case_id).search
        if "chunks_state_capped" in search:
            capped.append(float(search.get("chunks_state_capped") or 0))
            frontier_drops += int(search.get("labels_dropped_frontier_cap") or 0)
        truncated.append(search.get("truncated_by"))
    return {"arm": arm.label, "chunks_state_capped": summary(capped),
            "labels_dropped_frontier_cap": frontier_drops,
            "truncated_by": _counter(truncated)}  # fmt: skip


# ----------------------------------------------------------------------------- decomposition


def depth_decomposition(
    arms: Mapping[str, Arm], base: str, chains: Sequence[Sequence[str]]
) -> dict[str, Any]:
    """Per-case log-ratio decomposition over ONE common-OK case set of every named arm
    (R021-C/1 §6.2): for a chain (base, x1, …, xk) the log ratio x_k/base equals the sum of
    the consecutive terms exactly; geometric means per term are reported, never added bps."""
    names = sorted({base, *(n for chain in chains for n in chain)})
    for name in names:
        if name not in arms:
            raise AnalysisError(f"decomposition arm {name!r} is not defined")
    reference = arms[base]
    for name in names:
        _same_schedule(reference, arms[name])
    excluded: dict[str, Counter[str]] = {n: Counter() for n in names}
    common: list[str] = []
    for case_id in reference.case_ids:
        cells = {n: arms[n].cell(case_id) for n in names}
        if all(c.status == OK and c.gross is not None and c.gross > 0 for c in cells.values()):
            common.append(case_id)
        else:
            for n, c in cells.items():
                if not (c.status == OK and c.gross is not None and c.gross > 0):
                    excluded[n][c.status] += 1

    def term(num: str, den: str) -> dict[str, Any]:
        logs = []
        for case_id in common:
            a, b = arms[den].cell(case_id).gross, arms[num].cell(case_id).gross
            assert a is not None and b is not None
            logs.append(math.log(b) - math.log(a))
        return {"ratio": f"{num}/{den}", "n": len(logs),
                "geometric_mean": math.exp(sum(logs) / len(logs)) if logs else None,
                "higher": sum(1 for v in logs if v > 0), "equal": sum(1 for v in logs if v == 0),
                "lower": sum(1 for v in logs if v < 0), "sum_log": sum(logs)}  # fmt: skip

    out_chains = []
    for chain in chains:
        path = [base, *chain]
        steps = [term(path[i + 1], path[i]) for i in range(len(path) - 1)]
        total = term(path[-1], base)
        residual = total["sum_log"] - sum(s["sum_log"] for s in steps)
        out_chains.append({"chain": path, "steps": steps, "total": total,
                           "log_identity_residual": residual})  # fmt: skip
    return {
        "base": base,
        "arms": {n: arms[n].label for n in names},
        "common_ok_cases": len(common),
        "scheduled": len(reference.case_ids),
        "excluded_by_arm_status": {n: dict(sorted(c.items())) for n, c in excluded.items()},
        "chains": out_chains,
    }


# ----------------------------------------------------------------------------- nominee


def nominee(
    scan: Mapping[int, Sequence[tuple[str, Arm]]], *, canonical: int, algorithms: Sequence[str]
) -> dict[str, Any]:
    """The pre-registered max_splits nominee rule (preregistration §4.3, registered before
    any tuning observation). For value v, over every scheduled (case, algorithm) of the
    comparison's algorithms on its tuning bundles:

    - failures(v): cells whose status is a failure (timeout, invalid_plan, errors, missing);
    - shortfall(v): cells that are `ok` at some scanned value but at v are not `ok`, or are
      `ok` with a gross below the best gross that cell reaches over all scanned values.

    The nominee is the smallest v with shortfall(v) = 0 and failures(v) = min over V;
    if no value qualifies it is `canonical` (the unchanged base-profile value). No loss
    tolerance, no timing criterion, ties to the smaller (less work) value."""
    values = sorted(scan)
    if canonical not in values:
        raise AnalysisError(f"canonical value {canonical} is not scanned ({values})")
    cells: dict[int, dict[tuple[str, str, str], Cell]] = {}
    for v in values:
        cells[v] = {}
        for bundle, arm in scan[v]:
            if arm.algorithm not in algorithms:
                raise AnalysisError(f"max_splits={v}: unexpected arm algorithm {arm.algorithm}")
            for case_id in arm.case_ids:
                if (bundle, arm.algorithm, case_id) in cells[v]:
                    raise AnalysisError(f"max_splits={v}: {bundle}/{arm.algorithm} scanned twice")
                cells[v][(bundle, arm.algorithm, case_id)] = arm.cell(case_id)
    keys = set(cells[values[0]])
    for v in values:
        if set(cells[v]) != keys:
            raise AnalysisError(f"max_splits={v}: schedule differs from max_splits={values[0]}")
    best: dict[tuple[str, str, str], int] = {}
    for key in keys:
        grosses = [cells[v][key].gross for v in values
                   if cells[v][key].status == OK and cells[v][key].gross is not None]  # fmt: skip
        if grosses:
            best[key] = max(g for g in grosses if g is not None)
    table: dict[int, dict[str, Any]] = {}
    for v in values:
        failures = sum(1 for c in cells[v].values() if c.status in FAILURE_STATUSES)
        shortfall = [
            key for key, top in best.items()
            if cells[v][key].status != OK or cells[v][key].gross is None
            or cells[v][key].gross < top  # type: ignore[operator]
        ]  # fmt: skip
        table[v] = {"failures": failures, "shortfall": len(shortfall),
                    "shortfall_cells": [list(k) for k in sorted(shortfall)][:25]}  # fmt: skip
    fewest = min(int(t["failures"]) for t in table.values())
    eligible = [v for v in values if table[v]["shortfall"] == 0 and table[v]["failures"] == fewest]
    chosen = eligible[0] if eligible else canonical
    return {
        "values": {str(v): table[v] for v in values},
        "cells_per_value": len(keys),
        "eligible": eligible,
        "nominee": chosen,
        "reason": (
            f"smallest value with zero shortfall and the fewest failures ({fewest})"
            if eligible else f"no value saturates every cell: canonical {canonical} retained"
        ),
    }


# ----------------------------------------------------------------------------- reconciliation


def reconcile(
    label: str,
    *,
    algorithms: Sequence[str],
    case_ids: Sequence[str],
    expected_algorithms: Sequence[str],
    expected_case_ids: Sequence[str],
    cells: Mapping[tuple[str, str], str],
    complete: bool,
) -> list[str]:
    """Problems between a run and its registered inventory (empty = reconciled): complete
    manifest, exact algorithm order, exact case order, one record per scheduled cell."""
    problems = []
    if not complete:
        problems.append(f"{label}: run manifest is not complete")
    if list(algorithms) != list(expected_algorithms):
        problems.append(f"{label}: algorithms {list(algorithms)} != registered "
                        f"{list(expected_algorithms)}")  # fmt: skip
    if list(case_ids) != list(expected_case_ids):
        problems.append(f"{label}: case order differs from the registered bundle order")
    missing = [k for k, status in cells.items() if status == MISSING]
    if missing:
        problems.append(f"{label}: {len(missing)} scheduled cells have no record")
    expected = len(expected_algorithms) * len(expected_case_ids)
    if len(cells) != expected:
        problems.append(f"{label}: {len(cells)} cells, registered {expected}")
    return problems


__all__ = [
    "AnalysisError",
    "Arm",
    "Cell",
    "arm_from_run",
    "budget_bound",
    "certificate_view",
    "cfmm_view",
    "cycle_safe_view",
    "depth_decomposition",
    "domain_view",
    "equal_value",
    "history_view",
    "host_window",
    "identity",
    "memory_view",
    "nominee",
    "not_below",
    "paired",
    "reconcile",
    "relative_bps",
    "repair_view",
    "same_unit_ratio",
    "status_counts",
    "summary",
    "timing",
    "timing_verdict",
    "work_units",
]

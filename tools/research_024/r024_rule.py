"""WHI-1631 research-024 selection rule: pure functions over finished run records (contract
R024-C/1 §5.4-§5.8, §5.10, §5.11; schedule `config/research_024/selection.yaml`).

Nothing here solves, re-runs or reads a file. Every value the rule applies (epsilon, the work
limits, the tie-break order, the failure gross, the work units) is passed in from the schedule,
whose `check` holds it equal to contract §12; `tests/research_024/` reproduces the §12 worked
examples WE1-WE8 with these functions and fails when a value is altered.

- **Record branches** (§5.4): B0 terminated / B1 base pass-through / B2 not reached before polish /
  B3 polished, from the fields a record carries, never from defaults.
- **Gates** G2-G7 per branch (§12 `gate_applicability`), against the base reference record; G2
  is an eligibility gate, G3-G5, G7 and a G6 P5 are `defect`s.
- **Rule M / Rule P** (§5.4.1): a value a record does not carry is missing, never 0; the ordinary
  record and its work-pass twin reconcile as P1, or carry the label P2-P4, or differ (P5).
- **Objective, work limit, rule** (§5.5-§5.7): exact rationals end to end.
- **Outcome precedence** (§5.8): `blocked_defect` > `no_selection` > `selected`.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from fractions import Fraction
from typing import Any

UNITS = ("quotes", "cl_swap_steps", "lb_bins_swapped")
WORK_KEY = "r022_work"
B0, B1, B2, B3 = (
    "B0_terminated",
    "B1_base_passthrough",
    "B2_not_reached_before_polish",
    "B3_polished",
)
GATE_APPLICABILITY = {
    B0: ("G1", "G2", "G6"),
    B1: ("G1", "G2", "G4", "G5", "G6"),
    B2: ("G1", "G2", "G3", "G4", "G5", "G6"),
    B3: ("G1", "G2", "G3", "G4", "G5", "G6", "G7"),
}
DEFECT_GATES = ("G3", "G4", "G5", "G6", "G7")  # G6 only through a P5 `differs`
P1, P2, P3, P4, P5 = (
    "P1_reconciled",
    "P2_work_pass_terminated",
    "P3_both_terminated",
    "P4_ordinary_terminated",
    "P5_differs",
)
INFINITE = "infinite"


def canonical(value: Any) -> str:
    """Canonical JSON (the loader's convention: `json.dumps(value, sort_keys=True)`)."""
    return json.dumps(value, sort_keys=True)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def cec(resolved: Mapping[str, Any], identity: str) -> dict[str, Any]:
    """The candidate effective configuration of §4.3, from a resolved profile (a parsed
    profile's `resolved()` or a run manifest's `resolved_profile`)."""
    entry = (resolved.get("algorithm_options") or {}).get(identity)
    return {
        "algorithm": identity,
        **{k: resolved[k] for k in ("objective", "budget", "measurement", "worker", "search")},
        "params": resolved["algorithm_config"][identity]["params"],
        "options": None if entry is None else entry["options"],
        "settings_sha256": None if entry is None else entry["settings_sha256"],
    }


# ----------------------------------------------------------------------------- record fields


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def search_of(record: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return _mapping((record or {}).get("search")) or {}


def counted(record: Mapping[str, Any] | None) -> int | None:
    """`quotes.counted` (None when the record carries none, e.g. a runner hard limit)."""
    quotes = _mapping((record or {}).get("quotes"))
    value = None if quotes is None else quotes.get("counted")
    return None if value is None else int(value)


def gross(record: Mapping[str, Any] | None) -> int:
    """§5.4: `int(evaluation.gross_output)` when the status is `ok`, else 0."""
    if not record or record.get("status") != "ok":
        return 0
    evaluation = _mapping(record.get("evaluation")) or {}
    return int(evaluation["gross_output"])


def scored_gross(record: Mapping[str, Any] | None) -> int | None:
    """The per-case input of the objective: the gross of an `ok` record, None otherwise (the rule
    scores it as `failure_gross`)."""
    if not record or record.get("status") != "ok":
        return None
    return gross(record)


def work_block(record: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    return _mapping(search_of(record).get(WORK_KEY))


def branch(record: Mapping[str, Any] | None, identity: str) -> str:
    """§5.4: the branch of a post-processor record, from the fields it carries."""
    search = search_of(record)
    own, base = _mapping(search.get(identity)), _mapping(search.get("base"))
    if own is None or base is None:
        return B0
    if base.get("status") != "ok":
        return B1
    if "base_gross" not in own:
        return B2
    return B3


def reconcile(
    ordinary: Mapping[str, Any] | None, work_pass: Mapping[str, Any] | None
) -> tuple[str, tuple[str, ...]]:
    """§5.4.1 Rule P (first match) and the units Rule M keeps available."""
    work = work_block(work_pass)
    quotes = counted(ordinary)
    if work is not None and quotes is not None:
        same = all(
            (ordinary or {}).get(k) == (work_pass or {}).get(k) for k in ("status", "score")
        ) and canonical((ordinary or {}).get("evaluation")) == canonical(
            (work_pass or {}).get("evaluation")
        )
        if same and int(work["quotes_executed"]) == quotes:
            return P1, UNITS
        return P5, ()
    if work is None and quotes is not None:
        return P2, ("quotes",)
    if work is None:
        return P3, ()
    return P4, ("cl_swap_steps", "lb_bins_swapped")


def cell_work(
    ordinary: Mapping[str, Any] | None, work_pass: Mapping[str, Any] | None
) -> dict[str, int | None]:
    """Rule M per cell: quotes from the ordinary record, CL / LB from the work-pass block."""
    work = work_block(work_pass)
    return {
        "quotes": counted(ordinary),
        "cl_swap_steps": None if work is None else int(work["cl_swap_steps"]),
        "lb_bins_swapped": None if work is None else int(work["lb_bins_swapped"]),
    }


def totals(cells: Sequence[Mapping[str, int | None]]) -> dict[str, int | None]:
    """§5.6: the sum over every case per unit; any missing cell makes the total missing."""
    out: dict[str, int | None] = {}
    for unit in UNITS:
        values = [c[unit] for c in cells]
        out[unit] = None if any(v is None for v in values) else sum(int(v or 0) for v in values)
    return out


# ----------------------------------------------------------------------------- gates (§5.4)


def cell_gates(
    record: Mapping[str, Any] | None,
    reference: Mapping[str, Any] | None,
    identity: str,
    pair_case: str | None,
    gates: Sequence[str] = ("G2", "G3", "G4", "G5", "G6", "G7"),
) -> dict[str, Any]:
    """G2-G7 of one post-processor record against its base reference record, on the branches
    they apply to. Returns the branch and every failed gate (G2: `status`; the others: defects).
    `pair_case` is the Rule P outcome (None when the arm has no work pass, e.g. a sensitivity
    arm, where G6 does not apply)."""
    kind = branch(record, identity)
    applicable = [g for g in GATE_APPLICABILITY[kind] if g in gates]
    failed: list[str] = []
    rec, ref = record or {}, reference or {}
    search = search_of(rec)
    own, base = _mapping(search.get(identity)) or {}, _mapping(search.get("base")) or {}
    quotes = counted(rec)
    if "G2" in applicable and rec.get("status") != ref.get("status"):
        failed.append("G2")
    if "G3" in applicable:
        if (kind == B2 and gross(rec) != gross(ref)) or (kind == B3 and gross(rec) < gross(ref)):
            failed.append("G3")
    if "G4" in applicable:
        want = (ref.get("algorithm"), ref.get("status"), counted(ref), canonical(ref.get("search")))
        have = (
            base.get("algorithm"),
            base.get("status"),
            base.get("quotes"),
            canonical(base.get("search")),
        )
        if have != want or (kind == B3 and int(own["base_gross"]) != gross(ref)):
            failed.append("G4")
    if "G5" in applicable:
        base_quotes = base.get("quotes")
        if quotes is None or base_quotes is None:
            failed.append("G5")
        elif kind == B1 and quotes != int(base_quotes):
            failed.append("G5")
        elif kind == B2 and quotes < int(base_quotes):
            failed.append("G5")
        elif kind == B3 and (
            own.get("quotes") is None or quotes != int(base_quotes) + int(own["quotes"])
        ):
            failed.append("G5")
    if "G6" in applicable and pair_case == P5:
        failed.append("G6")
    if "G7" in applicable:
        evaluation = _mapping(rec.get("evaluation")) or {}
        if rec.get("status") == "ok" and str(evaluation.get("gross_output")) != str(
            own.get("gross")
        ):
            failed.append("G7")
        elif own.get("scope") == "unsupported_topology" and str(own.get("gross")) != str(
            own.get("base_gross")
        ):
            failed.append("G7")
    return {"branch": kind, "applicable": applicable, "failed": failed}


def completeness(
    manifest: Mapping[str, Any] | None,
    records: Sequence[Mapping[str, Any]] | None,
    algorithm: str,
    case_ids: Sequence[str],
    bundle_hash: str,
) -> list[str]:
    """G1 for one run: a complete manifest of the registered identity and bundle and exactly one
    record per tuning case (no missing, no duplicate, no unscheduled case)."""
    if manifest is None or records is None:
        return ["no run"]
    problems = []
    if not manifest.get("complete") or manifest.get("state") != "complete":
        problems.append(f"manifest state {manifest.get('state')}")
    if list(manifest.get("algorithms") or []) != [algorithm]:
        problems.append(f"runs {manifest.get('algorithms')}, registered [{algorithm}]")
    if manifest.get("bundle_hash") != bundle_hash:
        problems.append(f"bundle_hash {manifest.get('bundle_hash')}")
    seen = Counter(str(r.get("case_id")) for r in records)
    if any(r.get("algorithm") != algorithm for r in records):
        problems.append("a record of another algorithm")
    missing = [c for c in case_ids if seen[c] == 0]
    duplicate = sorted(c for c, n in seen.items() if n > 1)
    extra = sorted(set(seen) - set(case_ids))
    for label, cases in (("missing", missing), ("duplicate", duplicate), ("unscheduled", extra)):
        if cases:
            problems.append(f"{len(cases)} {label} case(s): {cases[:3]}")
    return problems


def by_case(records: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(r["case_id"]): r for r in records}


def evaluate_arm(
    identity: str,
    case_ids: Sequence[str],
    ordinary: Mapping[str, Mapping[str, Any]],
    work_pass: Mapping[str, Mapping[str, Any]] | None,
    reference: Mapping[str, Mapping[str, Any]] | None,
    gates: Sequence[str] = ("G2", "G3", "G4", "G5", "G6", "G7"),
) -> dict[str, Any]:
    """Every gate of one complete arm (G1 already passed), per case, plus Rule P / Rule M.
    `reference` None: a reference arm (only G6 and the work cells); `work_pass` None: no work
    pass (sensitivity arms; no G6, no work)."""
    branches: Counter[str] = Counter()
    pairs: Counter[str] = Counter()
    failures: dict[str, list[str]] = defaultdict(list)
    cells = []
    for case_id in case_ids:
        rec = ordinary[case_id]
        pair_case = None
        if work_pass is not None:
            pair_case, _ = reconcile(rec, work_pass[case_id])
            pairs[pair_case] += 1
            cells.append(cell_work(rec, work_pass[case_id]))
        if reference is None:
            if pair_case == P5:
                failures["G6"].append(case_id)
            continue
        result = cell_gates(rec, reference[case_id], identity, pair_case, gates)
        branches[result["branch"]] += 1
        for gate in result["failed"]:
            failures[gate].append(case_id)
    return {
        "branches": dict(sorted(branches.items())),
        "pairs": dict(sorted(pairs.items())),
        "failures": {g: failures[g] for g in sorted(failures)},
        "work": totals(cells) if work_pass is not None else None,
    }


# ----------------------------------------------------------------------------- objective and rule


def ratio(num: int | None, den: int | None) -> Fraction | str | None:
    """§5.6: W(x) / W(A0); 0/0 = 1; positive / 0 = +inf; None when either total is missing."""
    if num is None or den is None:
        return None
    if den == 0:
        return Fraction(1) if num == 0 else INFINITE
    return Fraction(num, den)


def within_limits(
    work: Mapping[str, int | None], reference: Mapping[str, int | None], limits: Mapping[str, Any]
) -> bool:
    for unit, limit in limits.items():
        value = ratio(work[unit], reference[unit])
        if value is None or value == INFINITE or Fraction(value) > Fraction(str(limit)):
            return False
    return True


def quality(
    candidate: Sequence[int | None], reference: Sequence[int], failure_gross: int
) -> Fraction | None:
    """§5.5: mean over C+ = {c : ref(c) > 0} of 10^4 (g(c) - ref(c)) / ref(c); None if C+ empty."""
    scored = [i for i, r in enumerate(reference) if r > 0]
    if not scored:
        return None
    total = sum(
        (
            Fraction(
                10**4
                * (
                    (failure_gross if candidate[i] is None else int(candidate[i] or 0))
                    - reference[i]
                ),
                reference[i],
            )
            for i in scored
        ),
        Fraction(0),
    )
    return total / len(scored)


def apply_rule(
    problem: Mapping[str, Any],
    *,
    epsilon: Fraction,
    limits: Mapping[str, Any],
    tie_break: Sequence[str],
    failure_gross: int = 0,
) -> dict[str, Any]:
    """§5.6-§5.7 (and the no-selection causes of §5.8 it can see) on one identity's rule inputs,
    in the shape of contract §12 `worked_examples`: `reference: {gross, work}` and `candidates:
    [{id, gates: pass|fail, gross, work}]`. Returns the outcome, the winner, the eligible set in
    input order, every Q, the tie band, the ranking, the frontier, the margin and the concession."""
    reference = [int(g) for g in problem["reference"]["gross"]]
    reference_work = problem["reference"]["work"]
    candidates = list(problem["candidates"])
    q = {c["id"]: quality(c["gross"], reference, failure_gross) for c in candidates}
    out: dict[str, Any] = {
        "outcome": "no_selection",
        "cause": None,
        "winner": None,
        "eligible": [],
        "q": {k: None if v is None else str(v) for k, v in q.items()},
        "q_star": None,
        "band": [],
        "ranking": [],
        "frontier": [],
        "margin": None,
        "concession": None,
        "scored_cases": sum(1 for r in reference if r > 0),
        "zero_reference_cases": sum(1 for r in reference if r <= 0),
    }
    if any(reference_work[u] is None for u in UNITS):
        out["cause"] = "reference_work_unavailable"
        return out
    if out["scored_cases"] == 0:
        out["cause"] = "no_scored_case"
        return out
    reasons: dict[str, str] = {}
    for c in candidates:
        if c["gates"] != "pass":
            reasons[c["id"]] = "gates"
        elif any(c["work"][u] is None for u in UNITS):
            reasons[c["id"]] = "work_unavailable"
        elif not within_limits(c["work"], reference_work, limits):
            reasons[c["id"]] = "work_limit"
    eligible = [c for c in candidates if c["id"] not in reasons]
    out["eligible"] = [c["id"] for c in eligible]
    out["ineligible"] = dict(sorted(reasons.items()))

    def key(c: Mapping[str, Any]) -> tuple[Any, ...]:
        return tuple(c["id"] if k == "candidate_id" else c["work"][k] for k in tie_break)

    out["ranking"] = [
        c["id"] for c in sorted(eligible, key=lambda c: (-Fraction(q[c["id"]] or 0), *key(c)))
    ]
    out["ranking"] += sorted(reasons)
    if not eligible:
        out["cause"] = "no_eligible_candidate"
        return out
    q_star = max(Fraction(q[c["id"]] or 0) for c in eligible)
    band = [c for c in eligible if Fraction(q[c["id"]] or 0) >= q_star - epsilon]
    winner = min(band, key=key)
    outside = [Fraction(q[c["id"]] or 0) for c in eligible if c not in band]
    q_winner = Fraction(q[winner["id"]] or 0)
    frontier = [
        x["id"] for x in eligible if not any(_dominates(y, x, q) for y in eligible if y is not x)
    ]
    out.update(
        outcome="selected",
        winner=winner["id"],
        q_star=str(q_star),
        band=[c["id"] for c in band],
        frontier=frontier,
        margin=None if not outside else str(q_winner - max(outside)),
        concession=str(q_star - q_winner),
    )
    return out


def _dominates(y: Mapping[str, Any], x: Mapping[str, Any], q: Mapping[str, Any]) -> bool:
    """§5.11: y dominates x in (Q, W_quotes): Q(y) >= Q(x), W(y) <= W(x), one strict."""
    qy, qx = Fraction(q[y["id"]] or 0), Fraction(q[x["id"]] or 0)
    wy, wx = int(y["work"]["quotes"]), int(x["work"]["quotes"])
    return qy >= qx and wy <= wx and (qy > qx or wy < wx)


def identity_outcome(
    *,
    defects: Sequence[str],
    reference_defects: Sequence[str],
    reference_unavailable: Sequence[str],
    rule: Mapping[str, Any],
) -> dict[str, Any]:
    """§5.8 precedence: `blocked_defect` (any defect of the identity or of a reference arm) >
    `no_selection` (a reference arm unavailable, or the rule's own causes) > `selected`."""
    if defects or reference_defects:
        return {
            "outcome": "blocked_defect",
            "winner": None,
            "cause": {"candidates": list(defects), "reference_arms": list(reference_defects)},
        }
    if reference_unavailable:
        return {
            "outcome": "no_selection",
            "winner": None,
            "cause": {"reference_arm_unavailable": list(reference_unavailable)},
        }
    if rule["outcome"] != "selected":
        return {"outcome": "no_selection", "winner": None, "cause": rule["cause"]}
    return {"outcome": "selected", "winner": rule["winner"], "cause": None}


# ----------------------------------------------------------------------------- descriptions


def own_base_gain(
    records: Mapping[str, Mapping[str, Any]],
    base: Mapping[str, Mapping[str, Any]],
    case_ids: Sequence[str],
    families: Mapping[str, str],
) -> dict[str, Any]:
    """§5.11: the gain over the candidate's own base reference record, over the cases whose base
    gross is > 0 (a failure scores 0): mean bps, H/E/L, improved count, families net +/-."""
    bps: list[Fraction] = []
    hel = Counter[str]()
    per_family: dict[str, Counter[str]] = defaultdict(Counter)
    for case_id in case_ids:
        b = gross(base[case_id])
        if b <= 0:
            continue
        g = gross(records[case_id])
        bps.append(Fraction(10**4 * (g - b), b))
        side = "higher" if g > b else "lower" if g < b else "equal"
        hel[side] += 1
        per_family[families.get(case_id, "unlabeled")][side] += 1
    mean = sum(bps, Fraction(0)) / len(bps) if bps else None
    return {
        "cases": len(bps),
        "mean_bps": None if mean is None else float(mean),
        "mean_bps_exact": None if mean is None else str(mean),
        "higher": hel["higher"],
        "equal": hel["equal"],
        "lower": hel["lower"],
        "improved": hel["higher"],
        "families": {
            "n": len(per_family),
            "net_win": sum(f["higher"] > f["lower"] for f in per_family.values()),
            "net_loss": sum(f["higher"] < f["lower"] for f in per_family.values()),
        },
    }


def identity_counters(
    records: Mapping[str, Mapping[str, Any]], identity: str, case_ids: Sequence[str]
) -> dict[str, Any]:
    """§5.11: status counts, `truncated_by` counts, refusals, Brent status counts."""
    statuses: Counter[str] = Counter()
    truncated: Counter[str] = Counter()
    scopes: Counter[str] = Counter()
    brent: Counter[str] = Counter()
    for case_id in case_ids:
        rec = records[case_id]
        statuses[str(rec.get("status"))] += 1
        own = _mapping(search_of(rec).get(identity)) or {}
        if own.get("truncated_by") is not None:
            truncated[str(own["truncated_by"])] += 1
        if identity in search_of(rec):
            scopes[str(own.get("scope"))] += 1
        blocks = [
            own.get("work"),
            (_mapping(own.get("e1")) or {}).get("work"),
            (_mapping(own.get("activation")) or {}).get("work"),
        ]
        for block in blocks:
            for status, n in ((_mapping(block) or {}).get("brent_status") or {}).items():
                brent[str(status)] += int(n)
    return {
        "statuses": dict(sorted(statuses.items())),
        "truncated_by": dict(sorted(truncated.items())),
        "scope": dict(sorted(scopes.items())),
        "refused": scopes.get("unsupported_topology", 0),
        "brent_status": dict(sorted(brent.items())),
    }


def preset_equality(
    candidate: Mapping[str, Mapping[str, Any]],
    preset: Mapping[str, Mapping[str, Any]],
    identity: str,
    case_ids: Sequence[str],
) -> list[dict[str, str]]:
    """§6.2: per case, the preset run's record equals the candidate's on status, error, score,
    `evaluation` (canonical JSON), `quotes.counted`, `search.<id>` and `search.base`."""
    differences = []
    for case_id in case_ids:
        a, b = candidate[case_id], preset[case_id]
        for name, get in (
            ("status", lambda r: r.get("status")),
            ("error", lambda r: r.get("error")),
            ("score", lambda r: r.get("score")),
            ("evaluation", lambda r: canonical(r.get("evaluation"))),
            ("quotes.counted", counted),
            (f"search.{identity}", lambda r: canonical(search_of(r).get(identity))),
            ("search.base", lambda r: canonical(search_of(r).get("base"))),
        ):
            if get(a) != get(b):
                differences.append({"case_id": case_id, "field": name})
    return differences

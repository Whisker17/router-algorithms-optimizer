"""WHI-1633 research-024 campaign analysis: pure functions over finished run records and the timing
stage's ledger (contract R024-C/1 §7.3-§7.5, §8.4-§8.7, §9; schedule
`config/research_024/campaign.yaml`).

Nothing here solves, re-runs or starts anything; the readers below only read files the driver
names. Every rule has a test in `tests/research_024/test_r024_campaign.py`.

- **Accounting (§7.4 C1, C7, C8, C13).** Every scheduled arm x case has exactly one record (a
  missing, duplicate or unscheduled case is a problem); every status is counted; truncation,
  refusal, fallback and branch counts are disclosed; work units stay separate and Rule M keeps a
  missing value missing; Rule P reconciles every cell that has a work-pass twin (P5 is a defect).
- **Gates (C10, C12)** through `r024_rule.cell_gates`, against the base record of the same cohort
  and split; G3-G7 (and G6 = P5) are defects, G2 a reported status transition.
- **E2 attribution (C11, C14, §7.5)**: the control classes K0-K5 in the registered order, the target
  and spend audits; K4 and a failed audit are defects.
- **Comparisons (C2-C6, C9)**: common-`ok` denominators, zero-baseline cases apart, nearest-rank
  distributions, directed-pair families and strata; rankable (§7.3) vs expanded-protocol; the
  per-case oracle envelope of the rankable rows.
- **Dispositions (§9.1)** and the cross-campaign determinism view against 0.2.2 (C15).
- **Timing (§8.4-§8.7)**: the literal T3 reading of `pmset -g log`, the trigger evaluation of an
  attempt, the resume state, the terminal outcome per unit and the two separate statements.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _p in (str(REPO), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import r024_rule as rr  # noqa: E402

E1, E2 = "split_polish", "marginal_activation"
POST = (E1, E2)
FAMILIES_ALL = ("constant_product", "concentrated", "liquidity_book")
T3_TYPES = ("Sleep", "Wake", "DarkWake")
DEFECT_GATES = ("G3", "G4", "G5", "G6", "G7")
TRUNCATION_SHARE = Fraction(1, 10)
# `<date> <time> <tz> <type><padding>\t<details>` (the T3 reading of the schedule)
PMSET_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}) ([^\t]*?)\s*\t")
PMSET_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}) ")


def _load(name: str, relative: str) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ra: Any = _load("r023_analysis", "tools/research_023/r023_analysis.py")  # dist, percentile


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# ----------------------------------------------------------------------------- inputs


def stratum_of(case_id: str) -> str:
    """C6: `bnd` / `nod` for those case-id prefixes, else the size word of an `emp-` case id."""
    prefix = case_id.split("-", 1)[0]
    if prefix in ("bnd", "nod"):
        return prefix
    words = [w for w in case_id.split("-") if w in ("low", "medium", "large")]
    return words[-1] if words else "unlabeled"


def bundle_view(path: Path) -> dict[str, Any]:
    """A bundle's scheduled case ids (file order), each case's directed-pair family
    `(token_in, token_out)` and stratum, and the pool families present (§7.3)."""
    cases = [
        json.loads(x)
        for x in (path / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]
    pools = json.loads((path / "pools.json").read_text(encoding="utf-8"))["pools"]
    return {
        "case_ids": [c["case_id"] for c in cases],
        "families": {c["case_id"]: f"{c['token_in']}->{c['token_out']}" for c in cases},
        "strata": {c["case_id"]: stratum_of(c["case_id"]) for c in cases},
        "pool_families": sorted({str(p.get("family") or "constant_product") for p in pools}),
    }


def ceiling(identity: str) -> tuple[str, ...]:
    from routing.algorithms.base import protocol_families
    from routing.algorithms.registry import ALGORITHMS

    return tuple(protocol_families(ALGORITHMS[identity].capabilities.protocols))


def rankable(a: str, b: str, present: Sequence[str]) -> bool:
    """§7.3: both protocol ceilings intersected with the cohort's families are equal."""
    return set(ceiling(a)) & set(present) == set(ceiling(b)) & set(present)


# ----------------------------------------------------------------------------- record views


def load_run(run_dir: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None, str]:
    from benchmark.results import load_case_records

    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        records = load_case_records(run_dir)
    except (OSError, ValueError) as exc:
        return None, None, str(exc)
    return manifest, records, ""


def slim(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """What a comparison reads: status, the runner's evaluated gross of an `ok` record, quotes."""
    if record is None:
        return {"status": "missing", "gross": None, "quotes": None}
    ok = record.get("status") == "ok"
    return {
        "status": str(record.get("status")),
        "gross": rr.gross(record) if ok else None,
        "quotes": rr.counted(record),
    }


def own_block(record: Mapping[str, Any] | None, identity: str) -> Mapping[str, Any]:
    search = rr.search_of(record)
    if identity in POST:
        return _mapping(search.get(identity))
    if identity == "cfmm_dual":
        return _mapping(search.get("cfmm"))
    return search


def numeric_counters(record: Mapping[str, Any] | None, identity: str) -> dict[str, int]:
    """C8: an identity's integer search counters (top level, and one level into its own block,
    `base`, `cfmm`, `bound_pruning`, `repair`, `r022_work`); booleans are not counters."""
    out: dict[str, int] = {}
    search = rr.search_of(record)
    for key, value in search.items():
        if isinstance(value, int) and not isinstance(value, bool):
            out[key] = value
        elif isinstance(value, Mapping) and key in (
            identity,
            "base",
            "cfmm",
            "bound_pruning",
            "repair",
            rr.WORK_KEY,
        ):
            for sub, inner in value.items():
                if isinstance(inner, int) and not isinstance(inner, bool):
                    out[f"{key}.{sub}"] = inner
    return out


def disclosures(
    records: Mapping[str, Mapping[str, Any] | None], identity: str, case_ids: Sequence[str]
) -> dict[str, Any]:
    """C7: truncation, refusal, `not_reached`, runner timeout by limit, last-valid candidates, the
    identity's own fallback fields and (post-processors) the branch counts B0-B3."""
    truncated: Counter[str] = Counter()
    scopes: Counter[str] = Counter()
    timeouts: Counter[str] = Counter()
    fallbacks: Counter[str] = Counter()
    branches: Counter[str] = Counter()
    not_reached = last_valid = 0
    for case_id in case_ids:
        rec = records.get(case_id)
        block = own_block(rec, identity)
        cut = block.get("truncated_by")
        if cut is not None:
            truncated[str(cut)] += 1
        if block.get("scope") is not None:
            scopes[str(block["scope"])] += 1
        not_reached += block.get("not_reached") is not None
        if (rec or {}).get("status") == "timeout":
            timeouts[str((rec or {}).get("limit_hit") or "solver_budget")] += 1
        last_valid += (rec or {}).get("last_valid_candidate") is not None
        for key, value in block.items():
            if "fallback" in key and value not in (None, False, 0, "", [], {}):
                fallbacks[f"{key}={value}" if isinstance(value, str) else key] += 1
        if identity in POST:
            branches[rr.branch(rec, identity)] += 1
    return {
        "truncated_by": dict(sorted(truncated.items())),
        "scope": dict(sorted(scopes.items())),
        "unsupported_topology": scopes.get("unsupported_topology", 0),
        "not_reached": not_reached,
        "timeout_by_limit": dict(sorted(timeouts.items())),
        "last_valid_candidates": last_valid,
        "fallback": dict(sorted(fallbacks.items())),
        "branches": dict(sorted(branches.items())),
    }


# ----------------------------------------------------------------------------- comparisons


def _bps(cand: int, base: int) -> Fraction:
    return Fraction(10**4 * (cand - base), base)


def compare(
    baseline: Mapping[str, Mapping[str, Any]],
    candidate: Mapping[str, Mapping[str, Any]],
    case_ids: Sequence[str],
    families: Mapping[str, str],
    strata: Mapping[str, str],
) -> dict[str, Any]:
    """C2-C6 for `candidate` vs `baseline` (slim records; positive bps = candidate higher):
    unconditional transitions, common-`ok` denominators, zero-baseline cases apart, the
    nearest-rank distribution with H/E/L, per-family and per-stratum counts."""
    transitions: Counter[str] = Counter()
    zero: list[str] = []
    only_a = only_b = 0
    values: list[tuple[str, Fraction]] = []
    for case_id in case_ids:
        a, b = baseline[case_id], candidate[case_id]
        transitions[f"{a['status']}->{b['status']}"] += 1
        a_ok, b_ok = a["gross"] is not None, b["gross"] is not None
        only_a += a_ok and not b_ok
        only_b += b_ok and not a_ok
        if not (a_ok and b_ok):
            continue
        if int(a["gross"]) <= 0:
            zero.append(case_id)
            continue
        values.append((case_id, _bps(int(b["gross"]), int(a["gross"]))))
    fam: dict[str, Counter[str]] = defaultdict(Counter)
    strat: dict[str, list[Fraction]] = defaultdict(list)
    for case_id, v in values:
        fam[families.get(case_id, "unlabeled")][_side(v)] += 1
        strat[strata.get(case_id, "unlabeled")].append(v)
    floats = [float(v) for _, v in values]
    hel = Counter(_side(v) for _, v in values)
    return {
        "scheduled": len(case_ids),
        "transitions": dict(sorted(transitions.items())),
        "common_ok": len(values) + len(zero),
        "only_baseline_ok": only_a,
        "only_candidate_ok": only_b,
        "zero_baseline_na": zero,
        "higher": hel["higher"],
        "equal": hel["equal"],
        "lower": hel["lower"],
        "bps": ra.dist(floats),
        "mean_bps_exact": str(sum((v for _, v in values), Fraction(0)) / len(values))
        if values
        else None,
        "families": {
            "n": len(fam),
            "net_plus": sum(c["higher"] > c["lower"] for c in fam.values()),
            "net_minus": sum(c["higher"] < c["lower"] for c in fam.values()),
        },
        "per_family": {k: dict(c) for k, c in sorted(fam.items())},
        "per_stratum": {
            k: {
                **dict(Counter(_side(v) for v in vs)),
                "n": len(vs),
                "mean_bps": float(sum(vs, Fraction(0)) / len(vs)),
            }
            for k, vs in sorted(strat.items())
        },
        "worst": sorted(((float(v), c) for c, v in values if v < 0))[:10],
    }


def _side(value: Fraction) -> str:
    return "higher" if value > 0 else "lower" if value < 0 else "equal"


def envelope(
    rows: Mapping[str, Mapping[str, Mapping[str, Any]]], case_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """C9(b): per case the largest gross among the `ok` records of `rows` (an oracle, not a
    portfolio); status `no_ok_row` where none is `ok`."""
    out = {}
    for case_id in case_ids:
        grosses = [r[case_id]["gross"] for r in rows.values() if r[case_id]["gross"] is not None]
        out[case_id] = (
            {"status": "ok", "gross": max(grosses), "quotes": None}
            if grosses
            else {"status": "no_ok_row", "gross": None, "quotes": None}
        )
    return out


# ----------------------------------------------------------------------------- control audit (§7.5)


def control_class(record: Mapping[str, Any] | None) -> str:
    """§7.5, first match, from the fields the record carries."""
    block = rr.search_of(record).get(E2)
    if not isinstance(block, Mapping):
        return "K0_terminated"
    if block.get("not_reached") is not None:
        return "K1_not_reached"
    if (record or {}).get("status") != "ok":
        return "K2_non_ok"
    if block.get("scope") == "unsupported_topology":
        return "K3_refused"
    if not isinstance(block.get("control"), Mapping):
        return "K4_missing_control_block"
    return "K5_matched"


def control_audit_row(control: Mapping[str, Any], embedded: Any, treatment: Any) -> str:
    """§7.5 audits of one K5 row: `ok`, `defect` or `treatment_unavailable`."""
    if control["kind"] == "work_matched":
        if int(control["quotes"]) > int(control["target_quotes"]):
            return "defect"
    elif control["calls_started"] != control["target_calls"] and control.get("stop") is None:
        return "defect"
    if not isinstance(treatment, Mapping):
        return "treatment_unavailable"
    mine = {k: v for k, v in _mapping(embedded).items() if k != "charged"}
    theirs = {k: v for k, v in treatment.items() if k != "charged"}
    target = (
        control["target_quotes"] if control["kind"] == "work_matched" else control["target_calls"]
    )
    spend = treatment["quotes"] if control["kind"] == "work_matched" else treatment["invocations"]
    if mine != theirs or target != spend:
        return "defect"
    return "ok"


def control_audit(
    control: Mapping[str, Mapping[str, Any] | None],
    treatment: Mapping[str, Mapping[str, Any] | None],
    case_ids: Sequence[str],
) -> dict[str, Any]:
    """§7.5 over one control arm against the treatment of the same case and cohort, with C14's
    two work columns (charged quotes; the embedded uncharged activation's quotes)."""
    classes: Counter[str] = Counter()
    audits: Counter[str] = Counter()
    defects: dict[str, list[str]] = defaultdict(list)
    charged: list[int | None] = []
    embedded_quotes: list[int | None] = []
    for case_id in case_ids:
        rec = control.get(case_id)
        kind = control_class(rec)
        classes[kind] += 1
        charged.append(rr.counted(rec))
        block = _mapping(rr.search_of(rec).get(E2))
        activation = block.get("activation")
        embedded_quotes.append(
            int(activation["quotes"])
            if isinstance(activation, Mapping) and activation.get("quotes") is not None
            else None
        )
        if kind == "K4_missing_control_block":
            defects["K4"].append(case_id)
            continue
        if kind != "K5_matched":
            continue
        theirs = _mapping(rr.search_of(treatment.get(case_id)).get(E2)).get("activation")
        outcome = control_audit_row(block["control"], activation, theirs)
        audits[outcome] += 1
        if outcome == "defect":
            defects["audit"].append(case_id)
    return {
        "rows": len(case_ids),
        "classes": dict(sorted(classes.items())),
        "matched_audits": dict(sorted(audits.items())),
        "defects": {k: v for k, v in sorted(defects.items())},
        "charged_quotes": _rule_m_sum(charged),
        "embedded_uncharged_activation_quotes": _rule_m_sum(embedded_quotes),
    }


def _rule_m_sum(values: Sequence[int | None]) -> dict[str, Any]:
    present = [int(v) for v in values if v is not None]
    return {
        "sum": None if len(present) != len(values) else sum(present),
        "present_cells": len(present),
        "missing_cells": len(values) - len(present),
        "sum_of_present": sum(present),
    }


# ----------------------------------------------------------------------------- stage analysis


def row_key(arm: str, cohort: str, identity: str) -> str:
    return f"{arm}/{identity}" if arm.startswith("Q19") else arm


def analyze_stage(
    raw: Mapping[str, Any],
    stage: str,
    runs: Mapping[str, Mapping[str, Any]],
    bundles: Mapping[str, Mapping[str, Any]],
    expected: Mapping[str, Mapping[str, Any]],
    *,
    bundle_hashes: Mapping[str, str],
    stage_revision: str | None,
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The whole §7.4 analysis of one quality stage from its runs (invocation id -> the
    invocation's fields plus `run_dir`), the cohorts' bundle views and the registered resolved
    profile of every generated profile."""
    problems: list[str] = []
    provenance: dict[str, Any] = {}
    rows: dict[str, dict[str, Any]] = {}
    full: dict[str, dict[str, Mapping[str, Any] | None]] = {}  # gate-relevant rows only
    current: dict[str, dict[str, Any]] = {}  # C15 views of the earlier identities (stage R)
    keep_full = {E1, E2, "metis_inspired", "incremental_graph"}
    ordinaries = {i: r for i, r in runs.items() if not r["work_pass"]}
    for inv_id, inv in ordinaries.items():
        cohort = str(inv["cohort"])
        case_ids = list(bundles[cohort]["case_ids"])
        twin_id = f"{stage}-WP-{inv['group']}-{cohort}"
        loaded: dict[str, tuple[dict[str, Any], list[dict[str, Any]]] | None] = {}
        for side, rid in (("ordinary", inv_id), ("work_pass", twin_id)):
            if side == "work_pass" and not any(
                spec["arm"] == inv["arm"] and spec["work_pass"] for spec in raw["arms"]
            ):
                continue
            run = runs.get(rid)
            if run is None:
                loaded[side] = None
                continue
            manifest, records, error = load_run(Path(run["run_dir"]))
            if manifest is None or records is None:
                problems.append(f"{rid}: unreadable run: {error}")
                loaded[side] = None
                continue
            provenance[rid] = _provenance(manifest, run["run_dir"])
            problems += _run_problems(
                rid, manifest, expected[str(inv["group"])], bundle_hashes[cohort], stage_revision
            )
            loaded[side] = (manifest, records)
        ordinary = loaded.get("ordinary")
        for identity in inv["algorithms"]:
            key = row_key(str(inv["arm"]), cohort, identity)
            recs: dict[str, Mapping[str, Any] | None] = {}
            wps: dict[str, Mapping[str, Any] | None] | None = None
            incomplete: list[str] = []
            if ordinary is not None:
                found = rr.completeness(
                    ordinary[0],
                    [r for r in ordinary[1] if r.get("algorithm") == identity],
                    identity,
                    case_ids,
                    bundle_hashes[cohort],
                )
                found = [p for p in found if not p.startswith("runs ")]
                incomplete += [f"{inv_id}: {p}" for p in found]
                problems += [f"{inv_id}/{identity}: {p}" for p in found]
                recs = {str(r["case_id"]): r for r in ordinary[1] if r.get("algorithm") == identity}
            if "work_pass" in loaded:
                wp = loaded["work_pass"]
                wps = {}
                if wp is not None:
                    found = rr.completeness(
                        wp[0],
                        [r for r in wp[1] if r.get("algorithm") == identity],
                        identity,
                        case_ids,
                        bundle_hashes[cohort],
                    )
                    found = [p for p in found if not p.startswith("runs ")]
                    incomplete += [f"{twin_id}: {p}" for p in found]
                    problems += [f"{twin_id}/{identity}: {p}" for p in found]
                    wps = {str(r["case_id"]): r for r in wp[1] if r.get("algorithm") == identity}
            rows[key] = _row_view(
                identity, cohort, str(inv["arm"]), case_ids, recs, wps, ordinary is not None
            )
            if "work_pass" in loaded and loaded["work_pass"] is None:
                incomplete.append(f"{twin_id}: no work-pass run")
            rows[key]["completeness"] = incomplete
            if wps is not None and ordinary is not None:
                problems += [f"{key}: P5 differs on {c}" for c in rows[key]["rule_p_cells"]["P5"]]
            if identity in keep_full or not str(inv["arm"]).startswith("Q19"):
                full[key] = {c: recs.get(c) for c in case_ids}
            if baseline is not None and str(inv["arm"]).startswith("Q19") and identity not in POST:
                current[key] = {c: _det(recs.get(c)) for c in case_ids}
    # C12 / C10 gates against the base record of the same cohort and split
    gates: dict[str, Any] = {}
    for key, spec in _gate_specs(raw, rows).items():
        if key not in full or spec["base"] not in full:
            continue
        cohort = rows[key]["cohort"]
        case_ids = list(bundles[cohort]["case_ids"])
        result = _gates(full[key], full[spec["base"]], rows[key], spec, case_ids)
        gates[key] = result
        problems += [
            f"{key}: {gate} fails on {c} (base {spec['base']})"
            for gate, cases in result["failures"].items()
            if gate in DEFECT_GATES
            for c in cases
        ]
    # C11 / §7.5 control audits and C14
    controls: dict[str, Any] = {}
    treatment_key = row_key("Q19-full", "full", E2)
    for spec in raw["arms"]:
        if spec["kind"] != "e2_control":
            continue
        key = str(spec["arm"])
        if key not in full or treatment_key not in full:
            continue
        audit = control_audit(full[key], full[treatment_key], bundles["full"]["case_ids"])
        controls[key] = audit
        problems += [
            f"{key}: {kind} defect on {c}"
            for kind, cases in audit["defects"].items()
            for c in cases
        ]
    # comparisons
    slims = {k: v["slim"] for k, v in rows.items()}
    comparisons = _comparisons(raw, rows, slims, bundles)
    dispositions = (
        dispositions_of(raw, rows, gates, controls, comparisons, bundles) if stage == "R" else None
    )
    out: dict[str, Any] = {
        "schema": "r024.campaign-analysis/1",
        "stage": stage,
        "stage_revision": stage_revision,
        "bundles": {
            c: {
                "bundle_hash": bundle_hashes[c],
                "cases": len(b["case_ids"]),
                "pool_families": b["pool_families"],
            }
            for c, b in bundles.items()
        },
        "base_controls": raw["base_controls"],
        "rows": {k: {x: y for x, y in v.items() if x != "slim"} for k, v in rows.items()},
        "gates": gates,
        "controls": controls,
        "comparisons": comparisons,
        "dispositions": dispositions,
        "provenance": provenance,
        "problems": problems,
        "not_scheduled": raw["not_scheduled"],
    }
    if baseline is not None:
        out["determinism_vs_0_2_2"] = determinism(baseline, current)
    return out


def _det(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """C15's compared fields of one record: status, score and `evaluation` (canonical JSON)."""
    if record is None:
        return None
    return {
        "status": record.get("status"),
        "score": record.get("score"),
        "evaluation": rr.canonical(record.get("evaluation")),
    }


def _provenance(manifest: Mapping[str, Any], run_dir: str) -> dict[str, Any]:
    env = _mapping(manifest.get("environment"))
    return {
        "run_dir": run_dir,
        "git_revision": env.get("git_revision"),
        "git_dirty": env.get("git_dirty"),
        "bundle_hash": manifest.get("bundle_hash"),
        "state": manifest.get("state"),
        "resolved_profile_sha256": rr.canonical_sha256(manifest.get("resolved_profile")),
        "replay_command": manifest.get("replay_command"),
    }


def _run_problems(
    rid: str,
    manifest: Mapping[str, Any],
    resolved: Mapping[str, Any],
    bundle_hash: str,
    revision: str | None,
) -> list[str]:
    problems = []
    env = _mapping(manifest.get("environment"))
    if manifest.get("state") != "complete" or not manifest.get("complete"):
        problems.append(f"{rid}: manifest state {manifest.get('state')}")
    if manifest.get("bundle_hash") != bundle_hash:
        problems.append(f"{rid}: bundle hash {manifest.get('bundle_hash')} != registered")
    if rr.canonical(manifest.get("resolved_profile")) != rr.canonical(resolved):
        problems.append(f"{rid}: resolved profile differs from the registered one")
    if env.get("git_dirty"):
        problems.append(f"{rid}: run from a dirty tree")
    if revision and env.get("git_revision") != revision:
        problems.append(f"{rid}: run revision {env.get('git_revision')} != stage {revision}")
    return problems


def _row_view(
    identity: str,
    cohort: str,
    arm: str,
    case_ids: Sequence[str],
    recs: Mapping[str, Mapping[str, Any] | None],
    wps: Mapping[str, Mapping[str, Any] | None] | None,
    present: bool,
) -> dict[str, Any]:
    """C1, C7, C8, C13 of one arm row."""
    statuses = Counter(str((recs.get(c) or {}).get("status", "missing")) for c in case_ids)
    pairs: Counter[str] = Counter()
    cells: dict[str, list[str]] = {p: [] for p in ("P2", "P3", "P4", "P5")}
    work: list[dict[str, int | None]] = []
    for case_id in case_ids:
        if wps is None:
            work.append(
                {
                    "quotes": rr.counted(recs.get(case_id)),
                    "cl_swap_steps": None,
                    "lb_bins_swapped": None,
                }
            )
            continue
        case, _ = rr.reconcile(recs.get(case_id), wps.get(case_id))
        pairs[case] += 1
        if case != rr.P1:
            cells[case[:2]].append(case_id)
        work.append(rr.cell_work(recs.get(case_id), wps.get(case_id)))
    counters: dict[str, list[int]] = defaultdict(list)
    flagged = []
    for case_id in case_ids:
        for name, value in numeric_counters(recs.get(case_id), identity).items():
            counters[name].append(value)
        block = own_block(recs.get(case_id), identity)
        if block.get("truncated_by") is not None or block.get("scope") == "unsupported_topology":
            flagged.append(case_id)
    return {
        "identity": identity,
        "cohort": cohort,
        "arm": arm,
        "present": present,
        "scheduled": len(case_ids),
        "statuses": dict(sorted(statuses.items())),
        "disclosures": disclosures(recs, identity, case_ids),
        "rule_p": None if wps is None else dict(sorted(pairs.items())),
        "rule_p_cells": cells,
        "flagged_cases": flagged,
        "work": {
            **rr.totals(work),
            "missing_cells": {u: sum(1 for w in work if w[u] is None) for u in rr.UNITS},
        },
        "search_counters": {
            k: {"sum": sum(v), "cells": len(v)} for k, v in sorted(counters.items())
        },
        "slim": {c: slim(recs.get(c)) for c in case_ids},
    }


def _gate_specs(raw: Mapping[str, Any], rows: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """C12: post-processor row -> its base row (same cohort and split) and its gates."""
    out: dict[str, dict[str, Any]] = {}
    bases = {
        str(s["post_processor"]): str(s["base"])
        for k, s in raw["base_controls"].items()
        if k.startswith("BASE-")
    }
    for key, row in rows.items():
        identity, cohort = row["identity"], row["cohort"]
        if row["arm"].startswith("Q19") and identity in POST:
            out[key] = {
                "base": row_key(f"Q19-{cohort}", cohort, bases[identity]),
                "identity": identity,
                "gates": ("G2", "G3", "G4", "G5", "G6", "G7"),
            }
        elif row["arm"] == "E2-E1only":
            out[key] = {
                "base": row_key(f"Q19-{cohort}", cohort, bases[E2]),
                "identity": E1,
                "gates": ("G2", "G3", "G4", "G5", "G6", "G7"),
            }
        elif row["arm"] in ("E2-wm", "E2-cm"):
            out[key] = {
                "base": row_key(f"Q19-{cohort}", cohort, bases[E2]),
                "identity": E2,
                "gates": ("G2", "G3", "G4", "G5", "G7"),
            }
    return out


def _gates(
    records: Mapping[str, Mapping[str, Any] | None],
    base: Mapping[str, Mapping[str, Any] | None],
    row: Mapping[str, Any],
    spec: Mapping[str, Any],
    case_ids: Sequence[str],
) -> dict[str, Any]:
    branches: Counter[str] = Counter()
    failures: dict[str, list[str]] = defaultdict(list)
    p5 = set(row["rule_p_cells"]["P5"])
    g4_checked = 0
    by_branch: dict[str, list[str]] = defaultdict(list)
    for case_id in case_ids:
        pair = rr.P5 if case_id in p5 else (rr.P1 if row["rule_p"] is not None else None)
        result = rr.cell_gates(
            records.get(case_id), base.get(case_id), spec["identity"], pair, spec["gates"]
        )
        branches[result["branch"]] += 1
        by_branch[result["branch"]].append(case_id)
        g4_checked += "G4" in result["applicable"]
        for gate in result["failed"]:
            failures[gate].append(case_id)
    return {
        "base": spec["base"],
        "gates": list(spec["gates"]),
        "branches": dict(sorted(branches.items())),
        "failures": {g: failures[g] for g in sorted(failures)},
        "b0_cases": by_branch[rr.B0],
        "b2_cases": by_branch[rr.B2],
        "c10_base_identity": {
            "checked": g4_checked,
            "equal": g4_checked - len(failures.get("G4", [])),
        },
    }


def _comparisons(
    raw: Mapping[str, Any],
    rows: Mapping[str, Any],
    slims: Mapping[str, Mapping[str, Any]],
    bundles: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """C9 (a) new identity vs each other row per cohort (rankable / expanded), (b) every row vs
    the envelope of the rows rankable with it, (c) new identity vs its base control; C11 E2 vs
    its attribution arms."""
    bases = {
        str(s["post_processor"]): str(s["base"])
        for k, s in raw["base_controls"].items()
        if k.startswith("BASE-")
    }
    out: dict[str, Any] = {
        "ranked": [],
        "expanded_protocol": [],
        "envelope": [],
        "base_control": [],
        "attribution": [],
    }
    for cohort, view in bundles.items():
        arm = f"Q19-{cohort}"
        q19 = {r["identity"]: k for k, r in rows.items() if r["arm"] == arm}
        present = view["pool_families"]
        args = (view["case_ids"], view["families"], view["strata"])
        for new in POST:
            if new not in q19:
                continue
            for other in raw["all19"]:
                if other == new or other not in q19:
                    continue
                item = {
                    "cohort": cohort,
                    "candidate": q19[new],
                    "baseline": q19[other],
                    **compare(slims[q19[other]], slims[q19[new]], *args),
                }
                section = "ranked" if rankable(new, other, present) else "expanded_protocol"
                out[section].append(item)
            base = q19.get(bases[new])
            if base is not None:
                out["base_control"].append(
                    {
                        "cohort": cohort,
                        "candidate": q19[new],
                        "baseline": base,
                        **compare(slims[base], slims[q19[new]], *args),
                    }
                )
        for identity, key in q19.items():
            peers = {k: slims[k] for i, k in q19.items() if rankable(identity, i, present)}
            env = envelope(peers, view["case_ids"])
            out["envelope"].append(
                {
                    "cohort": cohort,
                    "candidate": key,
                    "baseline": f"envelope({cohort}: {len(peers)} rows)",
                    "peers": sorted(peers),
                    "no_ok_row": sum(1 for v in env.values() if v["gross"] is None),
                    **compare(env, slims[key], *args),
                }
            )
    treatment = row_key("Q19-full", "full", E2)
    if treatment in slims:
        view = bundles["full"]
        for spec in raw["arms"]:
            if spec["kind"] in ("e1only", "e2_control") and spec["arm"] in slims:
                out["attribution"].append(
                    {
                        "cohort": "full",
                        "candidate": treatment,
                        "baseline": spec["arm"],
                        **compare(
                            slims[spec["arm"]],
                            slims[treatment],
                            view["case_ids"],
                            view["families"],
                            view["strata"],
                        ),
                    }
                )
    return out


# ----------------------------------------------------------------------------- dispositions (§9.1)


def dispositions_of(
    raw: Mapping[str, Any],
    rows: Mapping[str, Any],
    gates: Mapping[str, Any],
    controls: Mapping[str, Any],
    comparisons: Mapping[str, Any],
    bundles: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    bases = {
        str(s["post_processor"]): str(s["base"])
        for k, s in raw["base_controls"].items()
        if k.startswith("BASE-")
    }
    out = {}
    for identity in POST:
        reject: list[str] = []
        inconclusive: list[str] = []
        own = [
            k for k, r in rows.items() if r["arm"].startswith("Q19") and r["identity"] == identity
        ]
        controls_of = (
            [k for k, r in rows.items() if r["arm"] in ("E2-E1only", "E2-wm", "E2-cm")]
            if identity == E2
            else []
        )
        base_rows = [row_key(f"Q19-{c}", c, bases[identity]) for c in bundles]
        for key in (*own, *controls_of):
            for gate, cases in (gates.get(key) or {}).get("failures", {}).items():
                if gate in DEFECT_GATES and cases:
                    reject.append(f"{key}: {gate} defect on {len(cases)} case(s)")
        for key in (*own, *controls_of, *base_rows):
            if key in rows and rows[key]["rule_p_cells"]["P5"]:
                reject.append(
                    f"{key}: P5 differs on {len(rows[key]['rule_p_cells']['P5'])} cell(s)"
                )
        for key in controls_of:
            audit = controls.get(key) or {}
            for kind, cases in (audit.get("defects") or {}).items():
                if cases:
                    reject.append(f"{key}: control {kind} defect on {len(cases)} case(s)")
        for key in own:
            base = row_key(f"Q19-{rows[key]['cohort']}", rows[key]["cohort"], bases[identity])
            if base not in rows:
                continue
            bad = [
                c
                for c, s in rows[key]["slim"].items()
                if s["status"] in ("invalid_plan", "algorithm_error")
                and rows[base]["slim"][c]["status"] != s["status"]
            ]
            if bad:
                reject.append(
                    f"{key}: invalid_plan/algorithm_error where the base is not on "
                    f"{len(bad)} case(s)"
                )
        full_key = row_key("Q19-full", "full", identity)
        if full_key in rows:
            row = rows[full_key]
            flagged = _inconclusive_cases(
                row, gates.get(full_key), rows.get(row_key("Q19-full", "full", bases[identity]))
            )
            if Fraction(len(flagged), max(1, row["scheduled"])) > TRUNCATION_SHARE:
                inconclusive.append(
                    f"{full_key}: {len(flagged)}/{row['scheduled']} report_full "
                    "cases truncated / refused / B2 / B0 with base ok / P2-P4"
                )
        for key in (*own, *base_rows, *controls_of):
            if key not in rows or not rows[key]["present"]:
                inconclusive.append(f"{key}: arm missing")
            elif rows[key]["completeness"]:
                inconclusive.append(f"{key}: arm incomplete ({rows[key]['completeness'][0]})")
        verdict = "reject" if reject else "inconclusive" if inconclusive else "keep_experimental"
        out[identity] = {"disposition": verdict, "reject": reject, "inconclusive": inconclusive}
    return out


def _inconclusive_cases(
    row: Mapping[str, Any], gate: Mapping[str, Any] | None, base: Mapping[str, Any] | None
) -> list[str]:
    """§9.1: report_full cases truncated, `unsupported_topology`, B2, B0 with an `ok` base record,
    or carrying a P2-P4 label (from the stored per-case evidence)."""
    flagged = set()
    for label in ("P2", "P3", "P4"):
        flagged |= set(row["rule_p_cells"][label])
    flagged |= set(row.get("flagged_cases") or [])
    for case_id in (gate or {}).get("b2_cases", []):
        flagged.add(case_id)
    for case_id in (gate or {}).get("b0_cases", []):
        if base is not None and base["slim"][case_id]["status"] == "ok":
            flagged.add(case_id)
    return sorted(flagged)


# ----------------------------------------------------------------------------- C15


def load_baseline(raw: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """The 0.2.2 report-split records of the 17 earlier identities (hash-verified against the
    committed 0.2.2 analysis): cohort -> identity -> case -> slim record + evaluation."""
    spec = raw["baseline_022"]
    analysis_path = REPO / spec["analysis"]["path"]
    if sha256_bytes(analysis_path.read_bytes()) != spec["analysis"]["sha256"]:
        raise ValueError(f"{analysis_path} differs from its pin")
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    out: dict[str, Any] = {"full": {}, "sor": {}}
    for inv_id in spec["invocations"]:
        entry = analysis["invocations"][inv_id]
        run_dir = root / str(entry["run_dir"]).split(spec["artifacts_marker"], 1)[1].lstrip("/")
        if sha256_bytes((run_dir / "cases.jsonl").read_bytes()) != entry["cases_sha256"]:
            raise ValueError(f"{run_dir}: cases.jsonl differs from the 0.2.2 pin")
        cohort = "sor" if inv_id.endswith("-sor") else "full"
        _, records, error = load_run(run_dir)
        if records is None:
            raise ValueError(f"{run_dir}: {error}")
        for record in records:
            out[cohort].setdefault(record["algorithm"], {})[record["case_id"]] = _det(record)
    return out


def determinism(baseline: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """C15 (descriptive): per earlier identity and cohort, cells whose status, score or
    `evaluation` differ from 0.2.2's."""
    out: dict[str, Any] = {}
    for cohort, identities in baseline.items():
        for identity, cases in identities.items():
            key = row_key(f"Q19-{cohort}", cohort, identity)
            mine = current.get(key) or {}
            differing = [c for c, old in cases.items() if mine.get(c) != old]
            out[key] = {
                "cells": len(cases),
                "differing": len(differing),
                "examples": differing[:10],
                "compared": bool(mine),
            }
    return out


# ----------------------------------------------------------------------------- raw sums


def raw_sums(runs: Mapping[str, Mapping[str, Any]], root: Path) -> dict[str, str]:
    sums = {}
    for run in runs.values():
        directory = Path(run["run_dir"])
        for name in ("manifest.json", "cases.jsonl"):
            path = directory / name
            if path.is_file():
                try:
                    label = str(path.relative_to(root))
                except ValueError:
                    label = str(path)
                sums[label] = sha256_bytes(path.read_bytes())
    return dict(sorted(sums.items()))


# ----------------------------------------------------------------------------- timing (§8.4)


def _epoch(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S %z").timestamp()


def pmset_entries(text: str) -> list[dict[str, Any]]:
    """Every `pmset -g log` line with a timestamp and a TYPE COLUMN (the text between the timezone
    field and the first tab, trailing blanks removed): `{t, type, line}` (the T3 reading)."""
    out = []
    for line in text.splitlines():
        match = PMSET_LINE.match(line)
        if match is None:
            continue
        try:
            moment = _epoch(match.group(1))
        except ValueError:
            continue
        out.append({"t": moment, "type": match.group(2).rstrip(), "line": line})
    return out


def in_window(second: float, start: float, end: float) -> bool:
    """An entry stamped with second `s` happened in [s, s + 1): inside iff it meets [start, end]."""
    return second + 1 > start and second <= end


def t3_hits(entries: Iterable[Mapping[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    """T3: entries whose type column is EXACTLY Sleep, Wake or DarkWake inside the window."""
    return [dict(e) for e in entries if e["type"] in T3_TYPES and in_window(e["t"], start, end)]


def pmset_window_lines(text: str, low: float, high: float) -> list[str]:
    """The capture kept for an attempt: every line stamped inside [low, high], plus the unstamped
    continuation lines that follow such a line."""
    kept: list[str] = []
    inside = False
    for line in text.splitlines():
        match = PMSET_STAMP.match(line)
        if match is not None:
            try:
                inside = low <= _epoch(match.group(1)) <= high
            except ValueError:
                inside = False
        if inside:
            kept.append(line)
    return kept


def pmset_types(text: str | None, start: float, end: float) -> dict[str, Any]:
    """Every type in the window with its count (retained and reported, T3 or not), and the lines
    without a parsable type column."""
    if text is None:
        return {"types": None, "unparsed": None}
    lines = pmset_window_lines(text, start, end)
    entries = pmset_entries("\n".join(lines))
    types = Counter(e["type"] for e in entries if in_window(e["t"], start, end))
    stamped = [x for x in lines if PMSET_STAMP.match(x)]
    return {"types": dict(sorted(types.items())), "unparsed": len(stamped) - len(entries)}


def experiment_complete(kind: str, experiment: str, directory: Path) -> list[str]:
    """T5 per registered experiment, from the files it left (never from its results)."""
    problems: list[str] = []
    if experiment == "compare":
        if not (directory / "compare.json").is_file():
            problems.append("compare: no compare.json")
        return problems
    if kind == "quote_cli":
        manifests = sorted(directory.glob("*/runs/*/manifest.json"))
        if len(manifests) != 1:
            return [f"{experiment}: {len(manifests)} quote run manifests"]
        state = json.loads(manifests[0].read_text(encoding="utf-8")).get("state")
        return [] if state == "complete" else [f"{experiment}: run state {state}"]
    children = [p for p in directory.iterdir() if p.is_dir()] if directory.is_dir() else []
    if len(children) != 1 or not (children[0] / "experiment.json").is_file():
        return [f"{experiment}: no single experiment directory"]
    doc = json.loads((children[0] / "experiment.json").read_text(encoding="utf-8"))
    if doc.get("state") != "complete" or doc.get("partial"):
        problems.append(
            f"{experiment}: experiment state {doc.get('state')}, partial {doc.get('partial')}"
        )
    for run in doc.get("runs") or []:
        path = Path(run["run_dir"]) / "manifest.json"
        state = (
            json.loads(path.read_text(encoding="utf-8")).get("state") if path.is_file() else None
        )
        if state != "complete":
            problems.append(f"{experiment}: run {run.get('run_id')} state {state}")
    if not doc.get("runs"):
        problems.append(f"{experiment}: no run recorded")
    return problems


def attempt_validity(
    *,
    unit: Mapping[str, Any],
    experiments: Sequence[Mapping[str, Any]],
    start: float,
    end: float,
    samples: Sequence[Mapping[str, Any]],
    cpus: int,
    validity: Mapping[str, Any],
    pmset_text: str | None,
    caffeinate_held: bool,
    caffeinate_started: float | None,
    aborted: str | None,
    expected: Sequence[str],
) -> dict[str, Any]:
    """§8.4 item 2: the triggers T1-T5 of one attempt window [start, end]. Results never enter."""
    limit = float(validity["max_load1_per_cpu"]) * cpus
    inside = [s for s in samples if start <= float(s["t"]) <= end]
    over = [s for s in inside if float(s["load1"]) > limit]
    times = [start, *sorted(float(s["t"]) for s in inside), end]
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    max_gap = max(gaps) if gaps else None
    hits = [] if pmset_text is None else t3_hits(pmset_entries(pmset_text), start, end)
    t5: list[str] = []
    if aborted:
        t5.append(f"aborted: {aborted}")
    done = {str(e["experiment"]): e for e in experiments}
    for name in expected:
        entry = done.get(name)
        if entry is None or entry.get("exit_code") != 0:
            t5.append(
                f"{name}: {'not run' if entry is None else 'exit ' + str(entry.get('exit_code'))}"
            )
            continue
        t5 += experiment_complete(str(unit["kind"]), name, Path(str(entry["dir"])))
    t4: list[str] = []
    if not caffeinate_held:
        t4.append("caffeinate not held at the window end")
    if caffeinate_started is None or caffeinate_started > start:
        t4.append("caffeinate not held from the window start")
    if pmset_text is None:
        t4.append("pmset capture missing or failed")
    triggers = []
    if over:
        triggers.append("T1_load")
    if max_gap is None or max_gap > float(validity["max_sample_gap_seconds"]):
        triggers.append("T2_sampling_coverage")
    if hits:
        triggers.append("T3_sleep")
    if t4:
        triggers.append("T4_sleep_prevention_or_capture")
    if t5:
        triggers.append("T5_incomplete_execution")
    return {
        "valid": not triggers,
        "triggers": triggers,
        "detail": {
            "T1": {
                "threshold": limit,
                "samples": len(inside),
                "max_load1": max((float(s["load1"]) for s in inside), default=None),
                "over": len(over),
            },
            "T2": {"max_gap_seconds": max_gap},
            "T3": [{"t": h["t"], "type": h["type"]} for h in hits],
            "T4": t4,
            "T5": t5,
        },
        "pmset_window": pmset_types(pmset_text, start, end),
    }


def timing_state(entries: Sequence[Mapping[str, Any]], raw: Mapping[str, Any]) -> dict[str, Any]:
    """Resume state from the stage-L ledger: started attempts per unit, recorded outcomes, and the
    attempts that started but never ended (the driver died inside them, §10.4)."""
    attempts: dict[str, list[str]] = defaultdict(list)
    ended: set[str] = set()
    outcomes: dict[str, Any] = {}
    valid: dict[str, str] = {}
    for e in entries:
        kind = e.get("event")
        if kind == "attempt_start":
            attempts[str(e["unit"])].append(str(e["attempt"]))
        elif kind == "attempt_end":
            ended.add(str(e["attempt"]))
        elif kind == "attempt_validity" and e.get("valid"):
            valid.setdefault(str(e["unit"]), str(e["attempt"]))
        elif kind == "unit_outcome":
            outcomes[str(e["unit"])] = {
                k: e.get(k) for k in ("outcome", "attempt", "invalid_attempts")
            }
    interrupted = {u: a[-1] for u, a in attempts.items() if a and a[-1] not in ended}
    for unit, attempt in valid.items():
        outcomes.setdefault(unit, {"outcome": "valid", "attempt": attempt})
    return {"attempts": dict(attempts), "outcomes": outcomes, "interrupted": interrupted}


def unit_experiments(unit: Mapping[str, Any]) -> list[str]:
    """The registered experiments of one attempt of `unit`, in order (§8.2; the driver's
    `TimingStage.commands`)."""
    if unit["kind"] == "paired":
        return ["UP-base", "UP-cand", "compare"]
    if unit["kind"] == "quote_cli":
        return [f"q{index}" for index in range(1, int(unit["invocations"]) + 1)]
    return [str(unit["unit"])]


def gate_passes(gate: Mapping[str, Any], launch: Mapping[str, Any]) -> bool:
    """§8.4 launch gate re-read from its own samples: all of them taken, none above the headroom."""
    loads = [float(s["load1"]) for s in gate.get("samples") or []]
    return len(loads) == int(launch["samples"]) and max(loads) <= float(launch["headroom_load1"])


def no_launch_covered(gates: Sequence[Mapping[str, Any]], timing: Mapping[str, Any]) -> bool:
    """`no_launch` evidence: only failing gate groups, re-sampled without a gap longer than the
    re-sample interval (+ the T2 gap), spanning the whole wait (the driver stops once another
    re-sample would pass `max_wait_seconds`)."""
    launch = timing["launch"]
    every = float(launch["resample_every_seconds"])
    slack = every + float(timing["validity"]["max_sample_gap_seconds"])
    spans = [
        (min(times), max(times))
        for g in gates
        if (times := [float(s["t"]) for s in g.get("samples") or []])
    ]
    if not gates or len(spans) != len(gates) or any(gate_passes(g, launch) for g in gates):
        return False
    if any(b[0] - a[1] > slack for a, b in zip(spans, spans[1:], strict=False)):
        return False
    return spans[-1][1] - spans[0][0] + every > float(launch["max_wait_seconds"])


def verify_unit(
    raw: Mapping[str, Any],
    unit: Mapping[str, Any],
    out: Path,
    entries: Sequence[Mapping[str, Any]],
    samples: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    """§8.4 re-derived from the retained evidence, never from the recorded labels: per started
    attempt its launch gate, chronology, aborts and triggers (`attempt_validity` over the retained
    load samples, window, `<unit>/a<k>/pmset.txt` and caffeinate record); then the terminal outcome
    this evidence supports (None: none). Returns it with every disagreement with the ledger."""
    timing = raw["timing"]
    name = str(unit["unit"])
    prefix = f"L-{name}-a"
    cap = int(timing["max_started_attempts_per_unit"])
    order: list[str] = []
    starts: dict[str, Mapping[str, Any]] = {}
    ends: dict[str, Mapping[str, Any]] = {}
    recorded: dict[str, Mapping[str, Any]] = {}
    host: dict[str, tuple[Any, Any]] = {}  # attempt -> (logical CPUs, caffeinate start) at its end
    aborts: dict[str, list[str]] = defaultdict(list)
    gates: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    gave_up: set[int] = set()
    cpus: Any = None
    awake: Any = None
    for e in entries:
        kind, attempt = e.get("event"), str(e.get("attempt"))
        if kind == "stage":
            cpus = e.get("logical_cpus")
        elif kind == "caffeinate":
            awake = e.get("t_started")
        elif kind == "launch_gate" and e.get("unit") == name:
            gates[int(e["attempt"])].append(e)
        elif kind == "no_launch" and e.get("unit") == name:
            gave_up.add(int(e["attempt"]))
        elif kind == "abort" and attempt.startswith(prefix):
            aborts[attempt].append(str(e.get("trigger")))
        elif e.get("unit") != name:
            continue
        elif kind == "attempt_start":
            order.append(attempt)
            starts[attempt] = e
        elif kind == "attempt_end":
            ends[attempt] = e
            host[attempt] = (cpus, awake)
        elif kind == "attempt_validity":
            recorded[attempt] = e
    problems: list[str] = []
    if order != [f"{prefix}{k}" for k in range(1, len(order) + 1)]:
        problems.append(f"{name}: started attempts {order} are not {prefix}1, a2, ... in order")
    if len(order) > cap:
        problems.append(f"{name}: {len(order)} started attempts, more than N = {cap}")
    valid: list[str] = []
    breach = False
    previous_end: float | None = None
    for k, attempt in enumerate(order, 1):
        start = float(starts[attempt]["t_start"])
        if not any(
            gate_passes(g, timing["launch"]) and max(float(s["t"]) for s in g["samples"]) <= start
            for g in gates[k]
        ):
            problems.append(f"{attempt}: no passing launch gate before its start")
        if previous_end is not None and start < previous_end:
            problems.append(f"{attempt}: starts before the previous attempt ended")
        end = ends.get(attempt) or {}
        cpu_count, awake_started = host.get(attempt, (None, None))
        derived: dict[str, Any] = {"valid": False, "triggers": ["T5_incomplete_execution"]}
        if end.get("t_end") is not None and cpu_count is not None:
            if end.get("t_start") != starts[attempt]["t_start"]:
                problems.append(f"{attempt}: attempt_end has another window start")
            previous_end = float(end["t_end"])
            pmset = out / name / f"a{k}" / "pmset.txt"
            derived = attempt_validity(
                unit=unit,
                experiments=end.get("experiments") or [],
                start=start,
                end=previous_end,
                samples=samples,
                cpus=int(cpu_count),
                validity=timing["validity"],
                pmset_text=pmset.read_text(encoding="utf-8") if pmset.is_file() else None,
                caffeinate_held=end.get("caffeinate_held") is True,
                caffeinate_started=None if awake_started is None else float(awake_started),
                aborted=end.get("aborted"),
                expected=unit_experiments(unit),
            )
        elif end.get("aborted") != "driver_interrupted":  # §10.4: only that one has no window end
            problems.append(f"{attempt}: no attempt end, window or CPU count to evaluate")
        mine = recorded.get(attempt) or {}
        if any(mine.get(key) != value for key, value in derived.items()):
            problems.append(
                f"{attempt}: recorded validity {mine.get('valid')} {mine.get('triggers')} differs "
                f"from the retained evidence: {derived['valid']} {derived['triggers']}"
            )
        reasons = {*aborts[attempt], *([end["aborted"]] if end.get("aborted") else [])}
        if any(r != "driver_interrupted" and r not in derived["triggers"] for r in reasons):
            breach = True  # §8.4 item 2: an abort without a detected T1-T5 event
        if derived["valid"]:
            valid.append(attempt)
    supported: dict[str, Any] | None = None
    if breach:
        supported = {"outcome": "inconclusive_protocol_breach"}
    elif valid:
        supported = {"outcome": "valid", "attempt": valid[0]}
        if order[-1] != valid[0]:
            problems.append(f"{name}: attempts started after the first valid attempt {valid[0]}")
    elif len(order) >= cap:
        supported = {
            "outcome": "inconclusive_cap_exhausted",
            "attempt": None,
            "invalid_attempts": order,
        }
    elif len(order) + 1 in gave_up and no_launch_covered(gates[len(order) + 1], timing):
        supported = {
            "outcome": "inconclusive_no_launch",
            "attempt": None,
            "invalid_attempts": order,
        }
    return supported, problems


def analyze_timing(
    raw: Mapping[str, Any], out: Path, summarize: Callable[[Path], Any] | None = None
) -> dict[str, Any]:
    """Stage L: per unit the launch-gate history, every attempt with its triggers, the terminal
    outcome and the selected attempt; the two §8.7 statements; the §8.6 yield of each valid unit
    (`summarize`: `report.latency.summarize`, replaceable in tests)."""
    if summarize is None:
        from report.latency import summarize

    ledger = out / "ledger.jsonl"
    entries = (
        [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines() if x]
        if ledger.is_file()
        else []
    )
    state = timing_state(entries, raw)
    load = out / "load.jsonl"
    samples = (
        [json.loads(x) for x in load.read_text(encoding="utf-8").splitlines() if x]
        if load.is_file()
        else []
    )
    units: dict[str, Any] = {}
    problems: list[str] = []
    for unit in raw["timing"]["units"]:
        name = str(unit["unit"])
        gates = [
            e
            for e in entries
            if e.get("event") in ("launch_gate", "no_launch") and e.get("unit") == name
        ]
        attempts = {}
        for e in entries:
            if e.get("unit") != name:
                continue
            if e.get("event") == "attempt_start":
                attempts[e["attempt"]] = {"t_start": e.get("t_start")}
            elif e.get("event") in ("attempt_end", "attempt_validity") and e["attempt"] in attempts:
                attempts[e["attempt"]].update(
                    {k: v for k, v in e.items() if k not in ("event", "unit", "attempt")}
                )
        outcome = state["outcomes"].get(name)
        supported, unit_problems = verify_unit(raw, unit, out, entries, samples)
        problems += unit_problems
        if outcome is None:
            problems.append(f"{name}: no terminal outcome")
        elif supported is None:
            problems.append(f"{name}: the retained evidence supports no terminal outcome")
        elif any(outcome.get(k) != v for k, v in supported.items()):
            problems.append(
                f"{name}: recorded outcome {outcome} differs from the evidence {supported}"
            )
        view: dict[str, Any] = {
            "launch_gates": [
                {
                    k: g.get(k)
                    for k in ("event", "attempt", "group", "max_load1", "passed", "waited_seconds")
                }
                for g in gates
            ],
            "attempts": attempts,
            "outcome": outcome,
        }
        if outcome and outcome.get("outcome") == "valid":
            slot = out / name / str(outcome["attempt"]).rsplit("-", 1)[1]
            view["yield"] = _unit_yield(raw, unit, slot, summarize)
        units[name] = view
    terminal = [u["outcome"] for u in units.values()]
    attempted_correctly = not problems and all(  # missing evidence is a problem, never a "yes"
        t is not None and t.get("outcome") != "inconclusive_protocol_breach" for t in terminal
    )
    usable = [n for n, u in units.items() if (u["outcome"] or {}).get("outcome") == "valid"]
    sums = {}
    for path in sorted(out.rglob("*")):
        if path.is_file() and path.name in (
            "experiment.json",
            "manifest.json",
            "cases.jsonl",
            "compare.json",
            "pmset.txt",
            "ledger.jsonl",
            "load.jsonl",
            "memory.jsonl",
        ):
            sums[str(path.relative_to(out))] = sha256_bytes(path.read_bytes())
    return {
        "schema": "r024.timing-analysis/1",
        "units": units,
        "statements": {
            "measurement_attempted_correctly": attempted_correctly,
            "usable_latency_obtained": usable,
        },
        "t3_reading": raw["timing"]["t3_reading"],
        "problems": problems,
        "raw_sha256": sums,
    }


def _unit_yield(
    raw: Mapping[str, Any], unit: Mapping[str, Any], slot: Path, summarize: Any
) -> dict[str, Any]:
    """§8.6 per valid unit (descriptive; UP's comparison as `report.latency compare` wrote it)."""
    if unit["kind"] == "quote_cli":
        rows: dict[str, Any] = defaultdict(lambda: {"solve_seconds": [], "statuses": []})
        for index in range(1, int(unit["invocations"]) + 1):
            for manifest in sorted((slot / f"q{index}").glob("*/runs/*/manifest.json")):
                for line in (manifest.parent / "cases.jsonl").read_text().splitlines():
                    if line:
                        rec = json.loads(line)
                        rows[rec["algorithm"]]["solve_seconds"].append(
                            (rec["measurement"].get("solve_seconds") or [None])[0]
                        )
                        rows[rec["algorithm"]]["statuses"].append(rec["status"])
        return {"per_identity": dict(rows)}
    out: dict[str, Any] = {}
    names = ["UP-base", "UP-cand"] if unit["kind"] == "paired" else [str(unit["unit"])]
    for name in names:
        children = [p for p in (slot / name).iterdir() if p.is_dir()]
        summary = summarize(children[0])
        out[name] = {
            "experiment": children[0].name,
            "load": summary.get("load"),
            "timing": {
                label: {
                    a: {
                        k: v[k]
                        for k in (
                            "case_median_wall_seconds",
                            "case_median_cpu_seconds",
                            "prepare_seconds",
                            "noise_wall",
                            "noise_cpu",
                        )
                    }
                    for a, v in block.items()
                }
                for label, block in summary["timing"].items()
            },
            "cold": {
                label: {
                    a: {
                        k: v.get(k)
                        for k in (
                            "charged_seconds",
                            "prepare_seconds",
                            "solve_peak_bytes",
                            "prepare_peak_bytes",
                        )
                    }
                    for a, v in block.items()
                }
                for label, block in summary["cold"].items()
            },
        }
    if unit["kind"] == "paired":
        compare = json.loads((slot / "compare" / "compare.json").read_text(encoding="utf-8"))
        out["compare"] = {
            k: compare.get(k)
            for k in ("verdict", "reasons", "pairs", "timing", "charged", "charged_costs", "lane")
        }
        out["overhead_ratio"] = {
            key: {
                kind: None if v["improvement"][kind] is None else 1 - v["improvement"][kind]
                for kind in ("wall", "cpu")
            }
            for key, v in (compare.get("timing") or {}).items()
        }
    return out


# ----------------------------------------------------------------------------- tables


def _f(value: Any, digits: int = 3) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return "inf" if math.isinf(value) else f"{value:.{digits}f}"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return str(value)


def _counts(counts: Mapping[str, Any] | None) -> str:
    if not counts:
        return "–"
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))


def _table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(_f(c) for c in row) + " |" for row in rows]
    return [*lines, ""]


COMPARISON_HEADER = (
    "cohort",
    "candidate",
    "baseline",
    "scheduled",
    "common ok",
    "only base ok",
    "only cand ok",
    "zero-baseline",
    "H/E/L",
    "mean bps",
    "min",
    "p5",
    "p50",
    "p95",
    "max",
    "families net +/−",
)


def _comparison_row(c: Mapping[str, Any]) -> list[Any]:
    b = c["bps"]
    return [
        c["cohort"],
        f"`{c['candidate']}`",
        f"`{c['baseline']}`",
        c["scheduled"],
        c["common_ok"],
        c["only_baseline_ok"],
        c["only_candidate_ok"],
        len(c["zero_baseline_na"]),
        f"{c['higher']}/{c['equal']}/{c['lower']}",
        b["mean"],
        b["min"],
        b["p5"],
        b["p50"],
        b["p95"],
        b["max"],
        f"{c['families']['net_plus']}/{c['families']['net_minus']} of {c['families']['n']}",
    ]


def render_tables(raw: Mapping[str, Any], analyses: Mapping[str, Mapping[str, Any]]) -> str:
    """The Markdown tables of `results.md`, regenerated from the pinned analysis JSONs."""
    out = [
        "<!-- GENERATED by `tools/research_024/r024_campaign.py tables` from the pinned analyses; "
        "do not edit by hand -->",
        "",
    ]
    for stage in ("T", "R"):
        a = analyses.get(stage)
        if a is not None:
            out += _quality_tables(raw, stage, a)
    if "L" in analyses:
        out += _timing_tables(analyses["L"])
    return "\n".join(out) + "\n"


def _ordered(raw: Mapping[str, Any], items: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """Rows in the registered order (Q19 full, Q19 sor, then the separate arms; roster order),
    independent of the JSON's key order, so the tables regenerate identically."""
    arms = ["Q19-full", "Q19-sor", *(str(a["arm"]) for a in raw["arms"] if a["kind"] != "q19")]
    roster = list(raw["all19"])

    def rank(key: str) -> tuple[int, int, str]:
        arm, _, identity = key.partition("/")
        return (
            arms.index(arm) if arm in arms else len(arms),
            roster.index(identity) if identity in roster else -1,
            key,
        )

    return sorted(items.items(), key=lambda kv: rank(kv[0]))


def _quality_tables(raw: Mapping[str, Any], stage: str, a: Mapping[str, Any]) -> list[str]:
    split = "tuning" if stage == "T" else "report"
    out = [
        f"## Stage {stage} ({split} split)",
        "",
        f"Source revision `{a['stage_revision']}`; {len(a['problems'])} check problem(s); "
        + "; ".join(
            f"{c}: `{b['bundle_hash'][:12]}…`, {b['cases']} cases, pool families "
            f"{', '.join(b['pool_families'])}"
            for c, b in a["bundles"].items()
        ),
        "",
    ]
    out += [f"### C1 — statuses per arm × cohort ({split})", ""]
    out += _table(
        ("row", "cohort", "scheduled", "statuses"),
        [
            [f"`{k}`", r["cohort"], r["scheduled"], _counts(r["statuses"])]
            for k, r in _ordered(raw, a["rows"])
        ],
    )
    out += [f"### C7 — truncation, refusal, fallback, branches, Rule P labels ({split})", ""]
    out += _table(
        (
            "row",
            "truncated_by",
            "scope",
            "not reached",
            "timeout by limit",
            "last valid",
            "fallback",
            "branches B0-B3",
            "Rule P",
        ),
        [
            [
                f"`{k}`",
                _counts(r["disclosures"]["truncated_by"]),
                _counts(r["disclosures"]["scope"]),
                r["disclosures"]["not_reached"],
                _counts(r["disclosures"]["timeout_by_limit"]),
                r["disclosures"]["last_valid_candidates"],
                _counts(r["disclosures"]["fallback"]),
                _counts(r["disclosures"]["branches"]),
                _counts(r["rule_p"]),
            ]
            for k, r in _ordered(raw, a["rows"])
        ],
    )
    out += [f"### C8 / C13 — work per unit (Rule M: missing stays missing) ({split})", ""]
    out += _table(
        (
            "row",
            "quotes",
            "CL swap steps",
            "LB bins swapped",
            "missing cells q/CL/LB",
            "P1",
            "P2",
            "P3",
            "P4",
            "P5",
        ),
        [
            [
                f"`{k}`",
                r["work"]["quotes"],
                r["work"]["cl_swap_steps"],
                r["work"]["lb_bins_swapped"],
                "/".join(str(r["work"]["missing_cells"][u]) for u in rr.UNITS),
                *((r["rule_p"] or {}).get(p) for p in (rr.P1, rr.P2, rr.P3, rr.P4, rr.P5)),
            ]
            for k, r in _ordered(raw, a["rows"])
        ],
    )
    out += [f"### C10 / C12 — gates against the base record of the same cohort ({split})", ""]
    out += _table(
        ("row", "base", "gates", "branches", "failures", "C10 checked / equal"),
        [
            [
                f"`{k}`",
                f"`{g['base']}`",
                ",".join(g["gates"]),
                _counts(g["branches"]),
                _counts({n: len(c) for n, c in g["failures"].items()}),
                f"{g['c10_base_identity']['checked']} / {g['c10_base_identity']['equal']}",
            ]
            for k, g in _ordered(raw, a["gates"])
        ],
    )
    out += [f"### C11 / C14 — E2 control audit (§7.5) and control work ({split})", ""]
    out += _table(
        (
            "control",
            "classes K0-K5",
            "K5 audits",
            "defects",
            "charged quotes",
            "embedded uncharged activation quotes",
        ),
        [
            [
                f"`{k}`",
                _counts(c["classes"]),
                _counts(c["matched_audits"]),
                _counts({n: len(v) for n, v in c["defects"].items()}),
                c["charged_quotes"]["sum"],
                c["embedded_uncharged_activation_quotes"]["sum"],
            ]
            for k, c in _ordered(raw, a["controls"])
        ],
    )
    comps = a["comparisons"]
    for section, title in (
        ("base_control", "C9(c) — new identity vs its base control"),
        ("attribution", "C11 — E2 vs E2-E1only, E2-wm, E2-cm"),
        ("ranked", "C9(a) — new identity vs every other row, rankable (§7.3)"),
        (
            "expanded_protocol",
            "C9(a) — expanded-protocol section (not rankable; never ranked or pooled)",
        ),
        (
            "envelope",
            "C9(b) — every row vs the per-case best of the rows "
            "rankable with it (an oracle envelope, not a portfolio)",
        ),
    ):
        out += [f"### {title} ({split})", ""]
        out += _table(COMPARISON_HEADER, [_comparison_row(c) for c in comps[section]])
    strata = sorted(
        {s for c in comps["base_control"] + comps["attribution"] for s in c["per_stratum"]}
    )
    out += [f"### C6 — strata (H/E/L, mean bps) ({split})", ""]
    out += _table(
        ("cohort", "candidate", "baseline", *strata),
        [
            [
                c["cohort"],
                f"`{c['candidate']}`",
                f"`{c['baseline']}`",
                *(_stratum(c["per_stratum"].get(s)) for s in strata),
            ]
            for c in comps["base_control"] + comps["attribution"]
        ],
    )
    out += [f"### C5 — per-family table of each new identity ({split}): H/E/L per family", ""]
    for cohort in sorted({c["cohort"] for c in comps["base_control"]}):
        for new in POST:
            items = [
                c
                for c in comps["base_control"] + comps["ranked"] + comps["envelope"]
                if c["cohort"] == cohort and c["candidate"].endswith(f"/{new}")
            ]
            if not items:
                continue
            fams = sorted({f for c in items for f in c["per_family"]})
            out += [
                f"`{new}` on `{cohort}` vs: "
                + ", ".join(f"({n}) `{c['baseline']}`" for n, c in enumerate(items, 1)),
                "",
            ]
            out += _table(
                ("family", *(f"({n})" for n in range(1, len(items) + 1))),
                [
                    [
                        f[:14] + "…" + f[-8:] if len(f) > 24 else f,
                        *(_hel(c["per_family"].get(f)) for c in items),
                    ]
                    for f in fams
                ],
            )
    if a.get("dispositions"):
        out += ["### §9.1 dispositions (report split)", ""]
        out += _table(
            ("identity", "disposition", "reasons"),
            [
                [f"`{k}`", d["disposition"], "; ".join(d["reject"] + d["inconclusive"]) or "–"]
                for k, d in sorted(a["dispositions"].items())
            ],
        )
    if a.get("determinism_vs_0_2_2"):
        out += ["### C15 — the 17 earlier identities vs 0.2.2's report records (descriptive)", ""]
        out += _table(
            ("row", "cells", "differing (status, score, evaluation)", "examples"),
            [
                [f"`{k}`", v["cells"], v["differing"], ", ".join(v["examples"][:3]) or "–"]
                for k, v in _ordered(raw, a["determinism_vs_0_2_2"])
            ],
        )
    return out


def _stratum(group: Mapping[str, Any] | None) -> str:
    if not group:
        return "–"
    return (
        f"{group.get('higher', 0)}/{group.get('equal', 0)}/{group.get('lower', 0)} "
        f"({_f(group.get('mean_bps'))})"
    )


def _hel(group: Mapping[str, Any] | None) -> str:
    if not group:
        return "–"
    return f"{group.get('higher', 0)}/{group.get('equal', 0)}/{group.get('lower', 0)}"


def _timing_tables(a: Mapping[str, Any]) -> list[str]:
    s = a["statements"]
    out = [
        "## Stage L (timing, L01-R024)",
        "",
        f"- (a) measurement attempted correctly: **{s['measurement_attempted_correctly']}**",
        f"- (b) usable latency obtained (units with outcome `valid`): "
        f"**{', '.join(s['usable_latency_obtained']) or 'none'}**",
        "",
    ]
    rows = []
    for name, u in a["units"].items():
        for attempt, v in u["attempts"].items():
            rows.append(
                [
                    name,
                    attempt,
                    v.get("valid"),
                    ", ".join(v.get("triggers") or []) or "–",
                    v.get("aborted"),
                    _counts((v.get("pmset_window") or {}).get("types")),
                ]
            )
        rows.append(
            [
                name,
                "outcome",
                (u["outcome"] or {}).get("outcome"),
                (u["outcome"] or {}).get("attempt"),
                "",
                "",
            ]
        )
    out += _table(
        ("unit", "attempt", "valid", "triggers", "aborted", "pmset types in window"), rows
    )
    return out

"""Runtime validation of optional research diagnostics (WHI-1548 Stage B; contract R021-C/1
§§3-5, 9.4-9.6).

A solver may put ONE JSON object into its ordinary `SolveResult.search_stats["r021"]`
(`r021.diagnostics/1`: domain, certificate or null, unavailable reason, `max_candidates`
unit, named work counters, optional fallback/repair/scope/stages). There is no parallel
result framework: the runner (`benchmark.runner`) calls `check_diagnostics` after its own
independent final evaluation and persists the returned *view* as the record's
`diagnostics`. The view, never the solver's claim, is what every renderer shows.

Everything here treats the solver's object as untrusted input:

- It is compared only against what the runner supplies independently (`CheckContext`): the
  run's source revision, bundle hash, algorithm name and effective-settings hash (§9.3
  `settings_sha256` of the algorithm's resolved options), the exact request being solved
  (case id, input/output token, raw input), the runner's own evaluated score, objective and
  metered quote count. A certificate for another request fails `C_REQUEST` even when its
  score equals this request's evaluated score.
- Every field is type-checked before use; a malformed value (wrong type, missing key, huge
  or non-decimal amount, boolean counter, non-finite number, unserializable object) yields
  a visible `invalid` view with its codes, never an exception and never `certified`.
- The committed examples in `docs/references/research-021/fixtures/examples.json` and the
  test-only checker in `tests/docs/test_research_021_contract.py` are the specification;
  this module is the runtime validator and is tested against those examples plus malformed
  inputs.

View states: `certified` (a validated certified bound: `[lower, upper] gap g`), `estimate`
(a numerical value, never a bound), `unknown` (no bound claimed), `unavailable` (no
certificate: `hard_timeout`, `worker_error` or `not_produced`) and `invalid` (a violating or
malformed diagnostics object; counted as an unknown bound). Codes are the §4.4 validator
codes; `S_SHAPE` is this runtime's code for a structurally malformed object (not a mapping,
wrong schema/contract string, unknown or missing top-level key, unserializable content).

A hard-killed solve returns no `SolveResult`; its view is built by the runner from its own
observation only (`unavailable_view("hard_timeout")`), never from partial worker output
(§4.5, `C_KILLED`).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, TypeGuard

DIAGNOSTICS_KEY = "r021"
VIEW_SCHEMA = "r021.diagnostics_check/1"
DIAGNOSTICS_SCHEMA = "r021.diagnostics/1"
DOMAIN_SCHEMA = "r021.domain/1"
CERTIFICATE_SCHEMA = "r021.certificate/1"
CONTRACT = "R021-C/1"

# ------------------------------------------------------------------ R021-C/1 vocabulary
# Mirrors docs/references/research-021/contract-v1.json (tests/benchmark/test_diagnostics.py
# pins the equality, so the two cannot drift silently).

DOMAIN_FIELDS = (
    "schema",
    "universe",
    "protocols",
    "pool_order",
    "hops",
    "splits",
    "amount_grid",
    "zero_output_leg",
    "token_reuse",
    "pool_reuse",
    "dag_admission",
    "full_fill",
)
GRID_KINDS = ("single_leg", "repository_grid", "chunk_grid", "raw_integer", "recovered_continuous")
TOKEN_REUSE = ("simple_path",)
POOL_REUSE = ("single_pool", "disjoint", "shared_merged", "shared_sequential")
ZERO_OUTPUT_LEG = ("infeasible",)
DAG_ADMISSION = ("plan_token_dag",)
FULL_FILL = ("v1_full_fill",)
HOPS_PARAMS = (None, "search.max_hops", "graph.label_hops")
SPLITS_GOVERNS = ("allocation", "fallback_only", "none")
BOUND_KINDS = ("certified", "estimate", "unknown")
TERMINATIONS = (
    "complete",
    "node_cap",
    "state_cap",
    "candidate_cap",
    "quote_budget",
    "wall_budget",
    "iteration_cap",
    "converged",
    "not_converged",
    "recovery_failed",
    "unsupported_scope",
)
UNAVAILABLE_REASONS = ("hard_timeout", "worker_error", "not_produced")
CERTIFIED_SOURCES = ("exhaustive", "exact_rational", "outward_rounded")
WORK_UNITS: dict[str, str] = {
    "quotes_executed": "quote",
    "quotes_memoized": "quote",
    "exact_replay_quotes": "quote",
    "internal_evaluations": "validation",
    "paths_scored": "search",
    "label_relaxations": "search",
    "labels_discarded_dominance": "search",
    "labels_retained_unknown": "search",
    "state_comparisons": "search",
    "peak_frontier_labels": "memory",
    "bb_nodes_expanded": "search",
    "bound_evaluations": "search",
    "peak_open_nodes": "memory",
    "admission_checks": "search",
    "combinations_rejected_cycle": "search",
    "repair_attempts": "search",
    "checkpoint_restores": "search",
    "market_oracle_calls": "numeric",
    "objective_evaluations": "numeric",
    "gradient_evaluations": "numeric",
    "optimizer_iterations": "numeric",
    "recovery_attempts": "numeric",
}
NUMERIC_UNITS = frozenset(u for u, c in WORK_UNITS.items() if c == "numeric")


@dataclass(frozen=True)
class Identity:
    """One §3.3 row: the `Budget.max_candidates` unit and, where the contract fixes a list,
    the admitted objectives (`None`: "as <reference>", not checked beyond the run's own)."""

    max_candidates_unit: str
    objectives: tuple[str, ...] | None = None


# The existing nine and the five 0.2.1 identities of contract-v1.json (the cfmm_dual row as
# filled by WHI-1557). Listing a row here registers nothing: the roster is the algorithm
# registry, and a hard-timeout `unavailable` view is keyed on the factory's
# `options_validator`, never on this table.
IDENTITIES: dict[str, Identity] = {
    "direct": Identity("pools_evaluated"),
    "single_path": Identity("paths_evaluated"),
    "direct_split": Identity("finalist_plans_evaluated"),
    "path_split": Identity("per_stage_candidates_evaluated"),
    "incremental_graph": Identity("paths_scored_per_chunk"),
    "uni_sor_port": Identity("enumerated_routes_threshold"),
    "uni_sor_adaptive": Identity("enumerated_routes_threshold"),
    "uni_sor_optimized": Identity("enumerated_routes_threshold"),
    "metis_inspired": Identity("label_relaxations_per_chunk"),
    "metis_history": Identity("label_relaxations_per_chunk"),
    "direct_split_certified": Identity("finalist_plans_evaluated", ("gross_only",)),
    "incremental_graph_repair": Identity("paths_scored_per_chunk"),
    "uni_sor_cycle_safe": Identity("enumerated_routes_threshold"),
    "cfmm_dual": Identity("fallback_paths_evaluated", ("gross_only",)),
}

RUN_IDENTITY_KEYS = ("git_revision", "bundle_hash", "algorithm", "effective_settings_sha256")
REQUEST_KEYS = ("case_id", "token_in", "token_out", "amount_in")
TOP_LEVEL_REQUIRED = (
    "schema",
    "contract",
    "algorithm",
    "domain",
    "candidate_domain_hash",
    "certificate",
    "certificate_unavailable_reason",
    "max_candidates_unit",
    "work",
)
TOP_LEVEL_OPTIONAL = ("fallback", "repair", "scope", "stages")
CERTIFICATE_FIELDS = (
    "schema",
    "candidate_domain_hash",
    "objective",
    "source",
    "request",
    "lower_raw",
    "upper_raw",
    "gap_raw",
    "bound_kind",
    "upper_source",
    "estimate",
    "optimality_proven",
    "termination",
)
VIEW_STATES = ("certified", "estimate", "unknown", "unavailable", "invalid")

# A raw amount is a decimal integer string (DESIGN §2.2). Repository amounts are uint256
# (<= 78 digits); the cap also keeps `int()` far below Python's 4300-digit conversion limit.
_MAX_AMOUNT_CHARS = 100
_INTEGER = re.compile(r"-?(0|[1-9][0-9]*)")
_DECIMAL = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?")
_SHOWN = 60


def _shown(value: Any) -> str:
    """A bounded, printable rendering of an untrusted value for a detail message."""
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 - a hostile __repr__ must not escape
        text = f"<{type(value).__name__}>"
    return text if len(text) <= _SHOWN else text[: _SHOWN - 1] + "…"


def _is_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _is_raw(value: Any) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) <= _MAX_AMOUNT_CHARS
        and _INTEGER.fullmatch(value) is not None
    )


def _is_decimal_number(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > _MAX_AMOUNT_CHARS:
        return False
    if _DECIMAL.fullmatch(value) is None:
        return False
    try:
        return Decimal(value).is_finite()
    except (InvalidOperation, ValueError):
        return False


def _text(value: Any) -> bool:
    """A short printable string: safe to show on a terminal or in a report cell."""
    return isinstance(value, str) and len(value) <= 200 and value.isprintable()


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def domain_hash(domain: Any) -> str:
    """§3.1: SHA-256 hex of `json.dumps(domain, sort_keys=True).encode()`."""
    return hashlib.sha256(json.dumps(domain, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class CheckContext:
    """What the runner knows independently of the solver about one returned solve."""

    run: Mapping[str, str | None]  # RUN_IDENTITY_KEYS -> the runner's own value
    request: Mapping[str, str]  # REQUEST_KEYS -> the runner's case, raw amount as str
    status: str  # the record's status after the independent evaluation
    score: str | None  # the independently evaluated objective score (decimal string)
    objective: str  # the run's objective mode
    quotes_counted: int | None  # the worker meter's executed quotes of the attempt
    hard_killed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run": dict(self.run),
            "request": dict(self.request),
            "status": self.status,
            "score": self.score,
            "objective": self.objective,
            "quotes_counted": self.quotes_counted,
            "hard_killed": self.hard_killed,
        }


class _Findings:
    def __init__(self) -> None:
        self.codes: set[str] = set()
        self.details: list[str] = []

    def add(self, code: str, detail: str) -> None:
        self.codes.add(code)
        self.details.append(f"{code}: {detail}")


# ------------------------------------------------------------------ checks


def _check_domain(domain: Any, found: _Findings) -> None:
    if not isinstance(domain, Mapping):
        found.add("D_MISSING_FIELD", f"domain is not an object ({_shown(domain)})")
        return
    missing = [f for f in DOMAIN_FIELDS if f not in domain]
    if missing:
        found.add("D_MISSING_FIELD", f"domain lacks {', '.join(missing)}")
    if "schema" in domain and domain["schema"] != DOMAIN_SCHEMA:
        found.add("D_ENUM", f"domain.schema {_shown(domain['schema'])} is not {DOMAIN_SCHEMA}")
    enums: dict[str, tuple[str, ...]] = {
        "token_reuse": TOKEN_REUSE,
        "pool_reuse": POOL_REUSE,
        "zero_output_leg": ZERO_OUTPUT_LEG,
        "dag_admission": DAG_ADMISSION,
        "full_fill": FULL_FILL,
    }
    for key, allowed in enums.items():
        if key in domain and (not isinstance(domain[key], str) or domain[key] not in allowed):
            found.add("D_ENUM", f"domain.{key} {_shown(domain[key])} not in {list(allowed)}")
    grid = domain.get("amount_grid")
    kind = grid.get("kind") if isinstance(grid, Mapping) else None
    if "amount_grid" in domain and (not isinstance(kind, str) or kind not in GRID_KINDS):
        found.add("D_ENUM", f"domain.amount_grid.kind {_shown(kind)} not in {list(GRID_KINDS)}")
    hops = domain.get("hops")
    if "hops" in domain:
        if not isinstance(hops, Mapping) or "param" not in hops or "max" not in hops:
            found.add("D_MISSING_FIELD", f"domain.hops is not {{max, param}} ({_shown(hops)})")
        elif hops["param"] not in HOPS_PARAMS or not (
            hops["max"] is None or _is_count(hops["max"])
        ):
            found.add("D_ENUM", f"domain.hops {_shown(dict(hops))} is not a registered bound")
    splits = domain.get("splits")
    if "splits" in domain:
        if not isinstance(splits, Mapping) or any(
            k not in splits for k in ("max", "param", "governs")
        ):
            found.add(
                "D_MISSING_FIELD",
                f"domain.splits is not {{max, param, governs}} ({_shown(splits)})",
            )
        elif (
            not isinstance(splits["governs"], str)
            or splits["governs"] not in SPLITS_GOVERNS
            or not (splits["max"] is None or _is_count(splits["max"]))
        ):
            found.add("D_ENUM", f"domain.splits {_shown(dict(splits))} is not a registered bound")
    universe = domain.get("universe")
    pools = universe.get("pools") if isinstance(universe, Mapping) else None
    if "universe" in domain and not (
        isinstance(pools, list) and all(isinstance(p, str) for p in pools)
    ):
        found.add("D_MISSING_FIELD", "domain.universe.pools is not a list of pool ids")
        pools = None
    protocols = domain.get("protocols")
    if "protocols" in domain and not (
        isinstance(protocols, list) and all(isinstance(p, str) for p in protocols)
    ):
        found.add("D_MISSING_FIELD", "domain.protocols is not a list of protocol names")
    order = domain.get("pool_order")
    if "pool_order" in domain and not (
        isinstance(order, list) and all(isinstance(p, str) for p in order)
    ):
        found.add("D_POOL_ORDER", "domain.pool_order is not a list of pool ids")
    elif kind == "repository_grid" and "pool_order" in domain:
        if not order or pools is None or sorted(order) != sorted(pools):
            found.add("D_POOL_ORDER", "a repository_grid domain names its admitted pool order")


def _check_work(work: Any, ctx: CheckContext, found: _Findings) -> dict[str, int]:
    if not isinstance(work, Mapping):
        found.add("W_TYPE", f"work is not an object of counters ({_shown(work)})")
        return {}
    valid: dict[str, int] = {}
    for unit, value in work.items():
        if not isinstance(unit, str) or unit not in WORK_UNITS:
            found.add("W_UNIT", f"{_shown(unit)} is not a registered work unit")
        elif not _is_count(value):
            found.add("W_TYPE", f"work.{unit} {_shown(value)} is not a non-negative integer")
        else:
            valid[unit] = value
    executed = valid.get("quotes_executed")
    if not ctx.hard_killed and executed is not None:
        if executed != ctx.quotes_counted:
            found.add(
                "W_LEDGER",
                f"work.quotes_executed {executed} != the runner's metered {ctx.quotes_counted}",
            )
        replay = valid.get("exact_replay_quotes", 0)
        if replay > executed:
            found.add("W_LEDGER", f"exact_replay_quotes {replay} > quotes_executed {executed}")
    if NUMERIC_UNITS & set(valid) and "exact_replay_quotes" not in work:
        found.add("W_NUMERIC_REPLAY", "numeric work is reported without exact_replay_quotes")
    return valid


def _check_certificate(
    cert: Mapping[str, Any], rec: Mapping[str, Any], ctx: CheckContext, found: _Findings
) -> None:
    if ctx.hard_killed:
        found.add("C_KILLED", "a hard-killed solve keeps no certificate")
        return
    if cert.get("schema") != CERTIFICATE_SCHEMA:
        found.add("S_SHAPE", f"certificate.schema {_shown(cert.get('schema'))}")
    unknown = sorted(str(k) for k in cert if k not in CERTIFICATE_FIELDS)
    if unknown:
        found.add("S_SHAPE", f"unknown certificate field(s) {_shown(unknown)}")
    source = cert.get("source")
    if not isinstance(source, Mapping):
        found.add("C_IDENTITY", "certificate.source is missing or not an object")
    else:
        for key in RUN_IDENTITY_KEYS:
            claimed, own = source.get(key), ctx.run.get(key)
            if not _nonempty_str(claimed):
                found.add("C_IDENTITY", f"certificate.source.{key} is missing or empty")
            elif own is None or claimed != own:
                found.add(
                    "C_IDENTITY",
                    f"certificate.source.{key} {_shown(claimed)} != the run's {_shown(own)}",
                )
    request = cert.get("request")
    if not isinstance(request, Mapping):
        found.add("C_REQUEST", "certificate.request is missing or not an object")
    else:
        for key in REQUEST_KEYS:
            claimed = request.get(key)
            if not isinstance(claimed, str) or claimed != ctx.request.get(key):
                found.add(
                    "C_REQUEST",
                    f"certificate.request.{key} {_shown(claimed)} != this request's "
                    f"{_shown(ctx.request.get(key))}",
                )
        if not _is_raw(request.get("amount_in")):
            found.add("C_REQUEST", "certificate.request.amount_in is not a raw decimal string")
    if cert.get("candidate_domain_hash") != rec.get("candidate_domain_hash"):
        found.add("C_DOMAIN", "the certificate covers another domain than the diagnostics")
    identity = IDENTITIES.get(ctx.run.get("algorithm") or "")
    objective = cert.get("objective")
    if objective != ctx.objective or (
        identity is not None
        and identity.objectives is not None
        and objective not in identity.objectives
    ):
        found.add(
            "C_OBJECTIVE",
            f"certificate objective {_shown(objective)} vs run objective {ctx.objective!r}",
        )
    lower, upper, gap = cert.get("lower_raw"), cert.get("upper_raw"), cert.get("gap_raw")
    if not _is_raw(lower) or any(v is not None and not _is_raw(v) for v in (upper, gap)):
        found.add("C_AMOUNT_TYPE", "lower_raw/upper_raw/gap_raw must be raw decimal strings")
        return
    kind = cert.get("bound_kind")
    if not isinstance(kind, str) or kind not in BOUND_KINDS:
        found.add("C_BOUND_KIND", f"bound_kind {_shown(kind)} not in {list(BOUND_KINDS)}")
    certified = kind == "certified"
    if not certified and (upper is not None or gap is not None):
        found.add("C_UNCERTIFIED_BOUND", f"a {_shown(kind)} bound carries upper_raw/gap_raw")
    if not certified and cert.get("upper_source") is not None:
        found.add("C_UNCERTIFIED_BOUND", f"a {_shown(kind)} bound names an upper_source")
    closed = False
    if certified:
        source_kind = cert.get("upper_source")
        if (
            upper is None
            or not isinstance(source_kind, str)
            or source_kind not in CERTIFIED_SOURCES
        ):
            found.add(
                "C_CERTIFIED_SOURCE",
                f"certified needs upper_raw and upper_source in {list(CERTIFIED_SOURCES)}",
            )
        else:
            low, high = int(lower), int(str(upper))
            if high < low:
                found.add("C_ORDER", f"upper_raw {upper} < lower_raw {lower}")
            if gap is None or int(str(gap)) != high - low:
                found.add("C_GAP", f"gap_raw {gap} != upper_raw - lower_raw")
            closed = high == low
    if cert.get("optimality_proven") is not closed:
        found.add("C_OPTIMALITY", "optimality_proven must be true exactly for a zero certified gap")
    termination = cert.get("termination")
    if not isinstance(termination, str) or termination not in TERMINATIONS:
        found.add("C_TERMINATION", f"termination {_shown(termination)} is not registered")
    elif termination == "complete" and certified and not closed:
        found.add("C_TERMINATION", "a complete search closes the certified gap")
    estimate = cert.get("estimate")
    if estimate is not None and not (
        isinstance(estimate, Mapping)
        and set(estimate) == {"value", "residual", "tolerance"}
        and all(_is_decimal_number(estimate[k]) for k in ("value", "residual", "tolerance"))
    ):
        found.add("C_AMOUNT_TYPE", "estimate is not {value, residual, tolerance} decimal strings")
    if kind == "estimate" and estimate is None:
        found.add("S_SHAPE", "an estimate bound without its estimate {value, residual, tolerance}")
    if ctx.status != "ok" or ctx.score is None:
        found.add(
            "C_LOWER_EVAL",
            f"no independently validated incumbent (record status {ctx.status})",
        )
    elif lower != ctx.score:
        found.add("C_LOWER_EVAL", f"lower_raw {lower} != the evaluated score {ctx.score}")


def _check_optional(rec: Mapping[str, Any], found: _Findings) -> dict[str, Any]:
    """Optional `fallback`/`repair`/`scope`/`stages`; returns their sanitized copies."""
    out: dict[str, Any] = {}
    if "fallback" in rec:
        fb = rec["fallback"]
        if (
            isinstance(fb, Mapping)
            and set(fb) <= {"used", "source", "reason"}
            and isinstance(fb.get("used"), bool)
            and all(fb.get(k) is None or _text(fb.get(k)) for k in ("source", "reason"))
        ):
            out["fallback"] = {
                "used": fb["used"],
                "source": fb.get("source"),
                "reason": fb.get("reason"),
            }
        else:
            found.add("S_SHAPE", "fallback is not {used: bool, source, reason}")
    if "repair" in rec:
        repair = rec["repair"]
        if isinstance(repair, Mapping) and all(
            _text(k) and (v is None or isinstance(v, bool) or type(v) is int or _text(v))
            for k, v in repair.items()
        ):
            out["repair"] = dict(repair)
        else:
            found.add("S_SHAPE", "repair is not an object of scalar fields")
    if "scope" in rec:
        scope = rec["scope"]
        if (
            isinstance(scope, Mapping)
            and set(scope) <= {"supported", "reason"}
            and isinstance(scope.get("supported"), bool)
            and (scope.get("reason") is None or _text(scope.get("reason")))
        ):
            out["scope"] = {"supported": scope["supported"], "reason": scope.get("reason")}
        else:
            found.add("S_SHAPE", "scope is not {supported: bool, reason}")
    if "stages" in rec:
        stages = rec["stages"]
        if isinstance(stages, Mapping) and all(
            _text(k)
            and isinstance(v, int | float)
            and not isinstance(v, bool)
            and math.isfinite(v)
            and v >= 0
            for k, v in stages.items()
        ):
            out["stages"] = {k: float(v) for k, v in stages.items()}
        else:
            found.add("S_SHAPE", "stages is not an object of non-negative finite seconds")
    return out


def _findings(raw: Any, ctx: CheckContext) -> tuple[_Findings, dict[str, Any]]:
    found = _Findings()
    extras: dict[str, Any] = {}
    if not isinstance(raw, Mapping):
        found.add("S_SHAPE", f"search_stats['r021'] is not an object ({_shown(raw)})")
        return found, extras
    try:
        json.dumps(raw, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        found.add("S_SHAPE", f"not finite canonical JSON ({type(exc).__name__})")
        return found, extras
    if raw.get("schema") != DIAGNOSTICS_SCHEMA or raw.get("contract") != CONTRACT:
        found.add(
            "S_SHAPE",
            f"schema/contract {_shown(raw.get('schema'))}/{_shown(raw.get('contract'))} "
            f"is not {DIAGNOSTICS_SCHEMA}/{CONTRACT}",
        )
    missing = [k for k in TOP_LEVEL_REQUIRED if k not in raw]
    if missing:
        found.add("S_SHAPE", f"missing field(s) {', '.join(missing)}")
    unknown = sorted(str(k) for k in raw if k not in (*TOP_LEVEL_REQUIRED, *TOP_LEVEL_OPTIONAL))
    if unknown:
        found.add("S_SHAPE", f"unknown field(s) {_shown(unknown)}")
    algorithm = ctx.run.get("algorithm")
    if raw.get("algorithm") != algorithm:
        found.add(
            "C_IDENTITY", f"diagnostics name {_shown(raw.get('algorithm'))}, not {algorithm!r}"
        )
    domain = raw.get("domain")
    _check_domain(domain, found)
    if "domain" in raw and raw.get("candidate_domain_hash") != domain_hash(domain):
        found.add("D_HASH", "candidate_domain_hash is not the §3.1 hash of the domain")
    identity = IDENTITIES.get(algorithm or "")
    unit = raw.get("max_candidates_unit")
    if identity is None:
        found.add("W_MAX_CANDIDATES", f"{algorithm!r} has no R021-C/1 §3.3 row to check against")
    elif unit != identity.max_candidates_unit:
        found.add(
            "W_MAX_CANDIDATES",
            f"max_candidates_unit {_shown(unit)} is not {identity.max_candidates_unit!r}",
        )
    extras["work"] = _check_work(raw.get("work"), ctx, found)
    extras.update(_check_optional(raw, found))
    cert = raw.get("certificate")
    reason = raw.get("certificate_unavailable_reason")
    if cert is None:
        if not isinstance(reason, str) or reason not in UNAVAILABLE_REASONS:
            found.add("C_UNAVAILABLE_REASON", f"no certificate and reason {_shown(reason)}")
        elif reason == "hard_timeout" and not ctx.hard_killed:
            found.add("C_UNAVAILABLE_REASON", "a returned solve claims hard_timeout")
    elif not isinstance(cert, Mapping):
        found.add("S_SHAPE", f"certificate is not an object ({_shown(cert)})")
    else:
        if reason is not None:
            found.add("C_UNAVAILABLE_REASON", "a certificate with an unavailable reason")
        _check_certificate(cert, raw, ctx, found)
    return found, extras


def check_diagnostics(raw: Any, ctx: CheckContext) -> set[str]:
    """The §4.4 codes `raw` violates against `ctx` (empty: valid). Never raises."""
    return set(_evaluate(raw, ctx)[0].codes)


def _evaluate(raw: Any, ctx: CheckContext) -> tuple[_Findings, dict[str, Any]]:
    try:
        return _findings(raw, ctx)
    except Exception as exc:  # noqa: BLE001 - defensive net: untrusted input never crashes
        found = _Findings()
        found.add("S_SHAPE", f"unreadable diagnostics ({type(exc).__name__})")
        return found, {}


# ------------------------------------------------------------------ views


def _domain_summary(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    domain = raw.get("domain")
    grid = domain.get("amount_grid") if isinstance(domain, Mapping) else None
    kind = grid.get("kind") if isinstance(grid, Mapping) else None
    digest = raw.get("candidate_domain_hash")
    if not (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
        return None
    return {"hash": digest, "grid": kind if isinstance(kind, str) and kind in GRID_KINDS else None}


def diagnostics_view(raw: Any, ctx: CheckContext) -> dict[str, Any]:
    """The persisted view of a returned solve's `search_stats['r021']` (see module doc)."""
    found, extras = _evaluate(raw, ctx)
    view: dict[str, Any] = {
        "schema": VIEW_SCHEMA,
        "contract": CONTRACT,
        "origin": "solver",
        "checked_against": ctx.to_dict(),
    }
    if found.codes:
        try:
            cert = raw.get("certificate") if isinstance(raw, Mapping) else None
        except Exception:  # noqa: BLE001 - a hostile mapping: shown as unreadable
            cert = None
        view.update(
            state="invalid",
            codes=sorted(found.codes),
            details=found.details,
            certificate_present=cert is not None,
        )
        return view
    assert isinstance(raw, Mapping)
    view.update(
        codes=[],
        domain=_domain_summary(raw),
        max_candidates_unit=raw["max_candidates_unit"],
        work=extras.get("work", {}),
    )
    for key in TOP_LEVEL_OPTIONAL:
        if key in extras:
            view[key] = extras[key]
    cert = raw["certificate"]
    if cert is None:
        view.update(state="unavailable", reason=raw["certificate_unavailable_reason"])
        return view
    kind = cert["bound_kind"]
    view["state"] = kind
    view["termination"] = cert["termination"]
    view["lower"] = cert["lower_raw"]
    if kind == "certified":
        view.update(
            upper=cert["upper_raw"],
            gap=cert["gap_raw"],
            upper_source=cert["upper_source"],
            optimality_proven=cert["optimality_proven"],
        )
    elif kind == "estimate":
        view["estimate"] = dict(cert["estimate"])
    return view


def unavailable_view(reason: str, *, origin: str = "runner") -> dict[str, Any]:
    """A view the runner builds from its own observation: a hard-killed (`hard_timeout`),
    crashed/erroring (`worker_error`) or diagnostics-less (`not_produced`) solve of an
    options-accepting identity. Nothing of the solver's output is read."""
    if reason not in UNAVAILABLE_REASONS:
        raise ValueError(f"unregistered unavailable reason {reason!r}")
    return {
        "schema": VIEW_SCHEMA,
        "contract": CONTRACT,
        "origin": origin,
        "state": "unavailable",
        "reason": reason,
        "codes": [],
    }


def unserializable_marker(raw: Any) -> str | None:
    """`None` when `raw` can be persisted by the result writer (canonical JSON); otherwise the
    text that replaces it in the record's `search` so an unserializable claim cannot crash
    the run (its view is `invalid`, `S_SHAPE`)."""
    try:
        json.dumps(raw, sort_keys=True)
    except (TypeError, ValueError, RecursionError) as exc:
        return f"<r021 diagnostics not JSON-serializable: {type(exc).__name__}>"
    return None


# ------------------------------------------------------------------ rendering support


def read_view(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The renderable view of a saved record, or `None` when the record carries no
    diagnostics (every existing algorithm and every old run: rendered exactly as before).

    The persisted view is re-checked defensively; a view that is not self-consistent, or a
    solver `r021` object the runner never validated, is shown as invalid/unknown, never as
    certified."""
    view = record.get("diagnostics")
    search = record.get("search")
    if view is None:
        if isinstance(search, Mapping) and DIAGNOSTICS_KEY in search:
            return {
                "state": "unknown",
                "codes": [],
                "unvalidated": True,
                "details": ["search_stats['r021'] was not validated by the runner"],
            }
        return None
    if not isinstance(view, Mapping) or view.get("schema") != VIEW_SCHEMA:
        return _unreadable("the diagnostics view has an unknown schema")
    state = view.get("state")
    if state not in VIEW_STATES:
        return _unreadable(f"unknown diagnostics state {_shown(state)}")
    codes = view.get("codes")
    if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
        return _unreadable("malformed codes")
    if (state == "invalid") != bool(codes):
        return _unreadable("state and codes disagree")
    if state == "certified":
        lower, upper, gap = view.get("lower"), view.get("upper"), view.get("gap")
        if not (
            _is_raw(lower)
            and _is_raw(upper)
            and _is_raw(gap)
            and int(upper) >= int(lower)
            and int(gap) == int(upper) - int(lower)
        ):
            return _unreadable("a certified view whose bound does not reconcile")
    if state == "estimate":
        estimate = view.get("estimate")
        if not (
            isinstance(estimate, Mapping)
            and all(_is_decimal_number(estimate.get(k)) for k in ("value", "residual", "tolerance"))
        ):
            return _unreadable("an estimate view without a decimal estimate")
    if state == "unavailable" and view.get("reason") not in UNAVAILABLE_REASONS:
        return _unreadable("an unavailable view without a registered reason")
    return dict(view)


def _unreadable(why: str) -> dict[str, Any]:
    return {"state": "invalid", "codes": ["S_SHAPE"], "details": [f"S_SHAPE: {why}"]}


def bound_text(view: Mapping[str, Any]) -> str:
    """§9.5 bound wording for one view (from `read_view`)."""
    state = view["state"]
    if state == "certified":
        text = f"certified [{view['lower']}, {view['upper']}] gap {view['gap']}"
        extra = [str(view.get("upper_source")), f"termination {view.get('termination')}"]
        if view.get("optimality_proven") is True:
            extra.append("optimality proven")
        return f"{text} ({'; '.join(extra)})"
    if state == "estimate":
        est = view["estimate"]
        return (
            f"estimate {est['value']} (not a bound; residual {est['residual']}, tolerance "
            f"{est['tolerance']}; incumbent {view.get('lower')}; termination "
            f"{view.get('termination')})"
        )
    if state == "unknown":
        if view.get("unvalidated"):
            return "unknown (no bound: diagnostics not validated by the runner)"
        return f"unknown (no bound; termination {view.get('termination')})"
    if state == "unavailable":
        return f"unavailable ({view.get('reason')})"
    prefix = "invalid certificate" if view.get("certificate_present") else "invalid diagnostics"
    return f"{prefix} ({', '.join(view.get('codes', []))}) -- counted as unknown (no bound)"


def work_text(view: Mapping[str, Any]) -> str | None:
    work = view.get("work")
    if not isinstance(work, Mapping) or not work:
        return None
    order = {unit: index for index, unit in enumerate(WORK_UNITS)}  # §5.2 order, not dict order
    return ", ".join(
        f"{unit} {work[unit]} [{WORK_UNITS.get(str(unit), '?')}]"
        for unit in sorted(work, key=lambda u: (order.get(str(u), len(order)), str(u)))
    )


def domain_text(view: Mapping[str, Any]) -> str | None:
    domain = view.get("domain")
    if not isinstance(domain, Mapping) or not isinstance(domain.get("hash"), str):
        return None
    return f"{domain['hash'][:12]} ({domain.get('grid') or 'grid unknown'})"


def fallback_text(view: Mapping[str, Any]) -> str | None:
    parts: list[str] = []
    fb = view.get("fallback")
    if isinstance(fb, Mapping):
        if fb.get("used"):
            detail = "; ".join(
                f"{k} {fb[k]}" for k in ("source", "reason") if fb.get(k) is not None
            )
            parts.append("fallback used" + (f" ({detail})" if detail else ""))
        else:
            parts.append("fallback not used")
    repair = view.get("repair")
    if isinstance(repair, Mapping):
        parts.append(
            "repair " + (", ".join(f"{k} {v}" for k, v in repair.items()) or "(no fields)")
        )
    return "; ".join(parts) or None


def scope_text(view: Mapping[str, Any]) -> str | None:
    scope = view.get("scope")
    if not isinstance(scope, Mapping):
        return None
    if scope.get("supported"):
        return "supported"
    reason = scope.get("reason")
    return "UNSUPPORTED" + (f" ({reason})" if reason else "")


def stages_text(view: Mapping[str, Any]) -> str | None:
    stages = view.get("stages")
    if not isinstance(stages, Mapping) or not stages:
        return None
    return ", ".join(f"{k} {float(v):.6f} s" for k, v in stages.items())


__all__ = [
    "CONTRACT",
    "DIAGNOSTICS_KEY",
    "IDENTITIES",
    "VIEW_SCHEMA",
    "CheckContext",
    "Identity",
    "bound_text",
    "check_diagnostics",
    "diagnostics_view",
    "domain_hash",
    "domain_text",
    "fallback_text",
    "read_view",
    "scope_text",
    "stages_text",
    "unavailable_view",
    "unserializable_marker",
    "work_text",
]

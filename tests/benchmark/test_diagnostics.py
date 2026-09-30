"""WHI-1548 Stage B: the runtime validator of `search_stats["r021"]` (benchmark.diagnostics).

The committed R021-C/1 examples are the specification: every positive must be valid with
its runner-supplied context and every negative must fail with exactly its declared code.
Beyond them, malformed types/fields, unserializable or hostile objects and wrong run
identity / request values must produce an `invalid` view -- never an exception and never a
certified bound. Vocabulary constants are pinned to `contract-v1.json`.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from benchmark import diagnostics as dx
from benchmark.diagnostics import CheckContext

REPO = Path(__file__).resolve().parents[2]
R021 = REPO / "docs" / "references" / "research-021"
CONTRACT = json.loads((R021 / "contract-v1.json").read_text(encoding="utf-8"))
EXAMPLES = json.loads((R021 / "fixtures" / "examples.json").read_text(encoding="utf-8"))
POSITIVES = {p["id"]: p for p in EXAMPLES["positives"]}
DIAG_POSITIVES = [p for p in EXAMPLES["positives"] if p["kind"] == "diagnostics"]
DIAG_NEGATIVES = [n for n in EXAMPLES["negatives"] if POSITIVES[n["base"]]["kind"] == "diagnostics"]
EXPECTED_STATE = {
    "P-DSC-GRID": "certified",
    "P-DSC-GAP": "certified",
    "P-DSC-RAW": "certified",
    "P-CFMM-EST": "estimate",
    "P-HEUR-UNKNOWN": "unknown",
    "P-UNAVAILABLE": "unavailable",
}


def _ctx(context: Mapping[str, Any]) -> CheckContext:
    return CheckContext(
        run=dict(context["run"]),
        request=dict(context["request"]),
        status=context["status"],
        score=context["final_score"],
        objective=context["objective"],
        quotes_counted=context["quotes_counted"],
        hard_killed=context["hard_killed"],
    )


def _set(obj: dict[str, Any], dotted: str, value: Any) -> None:
    *parents, leaf = dotted.split(".")
    for key in parents:
        obj = obj[key]
    if value == "@delete":
        del obj[leaf]
    elif isinstance(value, str) and value.startswith("@P-"):
        ref, *path = value[1:].split(".")
        src: Any = POSITIVES[ref]["record"]
        for key in path:
            src = src[key]
        obj[leaf] = copy.deepcopy(src)
    else:
        obj[leaf] = value


def _grid() -> tuple[dict[str, Any], CheckContext]:
    base = POSITIVES["P-DSC-GRID"]
    return copy.deepcopy(base["record"]), _ctx(base["context"])


# ------------------------------------------------------------------ vocabulary


def test_vocabulary_is_contract_v1() -> None:
    assert list(dx.DOMAIN_FIELDS) == CONTRACT["domain_fields"]
    assert list(dx.GRID_KINDS) == CONTRACT["grid_kinds"]
    assert list(dx.TOKEN_REUSE) == CONTRACT["token_reuse"]
    assert list(dx.POOL_REUSE) == CONTRACT["pool_reuse"]
    assert list(dx.ZERO_OUTPUT_LEG) == CONTRACT["zero_output_leg"]
    assert list(dx.DAG_ADMISSION) == CONTRACT["dag_admission"]
    assert list(dx.FULL_FILL) == CONTRACT["full_fill"]
    assert list(dx.HOPS_PARAMS) == CONTRACT["hops_params"]
    assert list(dx.SPLITS_GOVERNS) == CONTRACT["splits_governs"]
    assert list(dx.BOUND_KINDS) == CONTRACT["bound_kinds"]
    assert list(dx.TERMINATIONS) == CONTRACT["terminations"]
    assert list(dx.UNAVAILABLE_REASONS) == CONTRACT["certificate_unavailable_reasons"]
    assert dx.WORK_UNITS == CONTRACT["work_units"]
    rows = {**CONTRACT["existing_identities"], **{i["id"]: i for i in CONTRACT["identities"]}}
    assert set(dx.IDENTITIES) == set(rows)
    for name, row in rows.items():
        identity = dx.IDENTITIES[name]
        assert identity.max_candidates_unit == row["max_candidates_unit"], name
        objectives = row.get("objectives")
        expected = tuple(objectives) if isinstance(objectives, list) else None
        assert identity.objectives == expected, name
        ceiling = row.get("protocols_ceiling")
        assert identity.protocols == (None if ceiling is None else tuple(ceiling)), name
    # §3.1 prose: the protocol families and universe cohorts a domain may name
    prose = (R021 / "contract.md").read_text(encoding="utf-8")
    assert "`constant_product`, `concentrated`, `liquidity_book`" in prose
    assert "cohort (`full_source`, `sor_compatible`, fixture)" in prose
    assert dx.PROTOCOLS == ("constant_product", "concentrated", "liquidity_book")
    assert dx.COHORTS == ("full_source", "sor_compatible", "fixture")
    assert {p for i in CONTRACT["identities"] for p in i["protocols_ceiling"]} == set(dx.PROTOCOLS)
    # the WHI-1557 row fill is what the runtime checks, not the WHI-1547 placeholder
    assert dx.IDENTITIES["cfmm_dual"].max_candidates_unit == "fallback_paths_evaluated"


# ------------------------------------------------------------------ committed examples


@pytest.mark.parametrize("example", DIAG_POSITIVES, ids=lambda e: e["id"])
def test_committed_positives_are_valid_runtime_views(example: dict[str, Any]) -> None:
    ctx = _ctx(example["context"])
    assert dx.check_diagnostics(example["record"], ctx) == set()
    view = dx.diagnostics_view(example["record"], ctx)
    assert view["state"] == EXPECTED_STATE[example["id"]]
    assert view["codes"] == [] and view["origin"] == "solver"
    assert view["checked_against"] == ctx.to_dict()
    json.dumps(view, sort_keys=True, allow_nan=False)  # persistable as is
    shown = dx.read_view({"diagnostics": view})
    assert shown is not None and shown["state"] == view["state"]
    cert = example["record"]["certificate"]
    if view["state"] == "certified":
        assert (view["lower"], view["upper"], view["gap"]) == (
            cert["lower_raw"],
            cert["upper_raw"],
            cert["gap_raw"],
        )
        assert dx.bound_text(view).startswith(
            f"certified [{cert['lower_raw']}, {cert['upper_raw']}] gap {cert['gap_raw']}"
        )
    elif view["state"] == "estimate":
        assert "upper" not in view and "gap" not in view
        assert dx.bound_text(view).startswith(f"estimate {cert['estimate']['value']} (not a bound")
    elif view["state"] == "unknown":
        assert dx.bound_text(view).startswith("unknown (no bound")
    else:
        assert dx.bound_text(view) == "unavailable (hard_timeout)"


@pytest.mark.parametrize("example", DIAG_NEGATIVES, ids=lambda e: e["id"])
def test_committed_negatives_fail_with_exactly_their_code(example: dict[str, Any]) -> None:
    base = POSITIVES[example["base"]]
    rec = copy.deepcopy(base["record"])
    for dotted, value in example["patch"].items():
        _set(rec, dotted, value)
    if example["rehash"]:
        rec["candidate_domain_hash"] = dx.domain_hash(rec["domain"])
        if rec.get("certificate"):
            rec["certificate"]["candidate_domain_hash"] = rec["candidate_domain_hash"]
    ctx = _ctx({**base["context"], **example["context_patch"]})
    assert dx.check_diagnostics(rec, ctx) == {example["violation"]}
    view = dx.diagnostics_view(rec, ctx)
    assert view["state"] == "invalid" and view["codes"] == [example["violation"]]
    assert "certified [" not in dx.bound_text(view)
    assert "counted as unknown" in dx.bound_text(view)


def test_every_diagnostics_code_of_the_examples_is_exercised() -> None:
    codes = {n["violation"] for n in DIAG_NEGATIVES}
    assert {
        "C_AMOUNT_TYPE",
        "C_UNCERTIFIED_BOUND",
        "C_CERTIFIED_SOURCE",
        "C_ORDER",
        "C_GAP",
        "C_OPTIMALITY",
        "C_TERMINATION",
        "C_DOMAIN",
        "C_OBJECTIVE",
        "C_LOWER_EVAL",
        "C_IDENTITY",
        "C_REQUEST",
        "C_KILLED",
        "C_UNAVAILABLE_REASON",
        "D_MISSING_FIELD",
        "D_ENUM",
        "D_POOL_ORDER",
        "D_HASH",
        "W_UNIT",
        "W_TYPE",
        "W_LEDGER",
        "W_MAX_CANDIDATES",
        "W_NUMERIC_REPLAY",
    } <= codes


# ------------------------------------------------------------------ WHI-1551 consumer evidence

INTEGER_ALLOCATION = json.loads(
    (R021 / "fixtures" / "integer-allocation.json").read_text(encoding="utf-8")
)["examples"]


def _ia_context(example: dict[str, Any]) -> CheckContext:
    """The runner-side context of a WHI-1551 published record, built as its own check does:
    the certificate's run identity and request (or placeholders without one), the example's
    status and final score, `gross_only` and the worker meter = `work.quotes_executed`."""
    rec = example["record"]
    cert = rec["certificate"]
    run = (
        dict(cert["source"])
        if cert
        else {  # the harness's own bundle; the other identity values are never compared
            **dict.fromkeys(dx.RUN_IDENTITY_KEYS, "x"),
            "bundle_hash": rec["domain"]["universe"]["bundle"],
        }
    )
    request = dict(cert["request"]) if cert else dict.fromkeys(dx.REQUEST_KEYS, "x")
    return CheckContext(
        run={**run, "algorithm": rec["algorithm"]},
        request=request,
        status=example["status"],
        score=example["final_score"],
        objective="gross_only",
        quotes_counted=rec["work"].get("quotes_executed"),
    )


@pytest.mark.parametrize("example", INTEGER_ALLOCATION, ids=lambda e: e["id"])
def test_whi1551_published_records_validate_at_runtime(example: dict[str, Any]) -> None:
    rec, ctx = example["record"], _ia_context(example)
    assert dx.check_diagnostics(rec, ctx) == set()
    view = dx.diagnostics_view(rec, ctx)
    cert = rec["certificate"]
    if cert is None:
        assert view["state"] == "unavailable" and view["reason"] == "not_produced"
        return
    assert view["state"] == cert["bound_kind"]
    if cert["bound_kind"] == "certified":
        assert (view["lower"], view["upper"], view["gap"]) == (
            cert["lower_raw"],
            cert["upper_raw"],
            cert["gap_raw"],
        )
    # the same proof for another request with the same evaluated score does not transfer
    other = {**ctx.request, "case_id": ctx.request["case_id"] + "-other"}
    assert dx.check_diagnostics(rec, CheckContext(**{**ctx.__dict__, "request": other})) == {
        "C_REQUEST"
    }


def test_whi1551_consistency_failure_is_unavailable_never_a_bound() -> None:
    """WHI-1551 §5.6: an internal consistency failure keeps the earlier valid plan (`ok`) but
    emits `certificate: null`, `not_produced`; absence of a certificate is never a proof."""
    base = next(
        e for e in INTEGER_ALLOCATION if (e["record"]["certificate"] or {}).get("optimality_proven")
    )
    rec = copy.deepcopy(base["record"])
    rec.update(certificate=None, certificate_unavailable_reason="not_produced")
    view = dx.diagnostics_view(rec, _ia_context(base))
    assert view["state"] == "unavailable" and view["reason"] == "not_produced"
    assert not {"lower", "upper", "gap", "termination"} & set(view)
    assert dx.bound_text(view) == "unavailable (not_produced)"


# ------------------------------------------------------------------ domain identity (BLOCKER-2)


def _rehashed(rec: dict[str, Any]) -> dict[str, Any]:
    rec["candidate_domain_hash"] = dx.domain_hash(rec["domain"])
    if rec.get("certificate"):
        rec["certificate"]["candidate_domain_hash"] = rec["candidate_domain_hash"]
    return rec


DOMAIN_MUTATIONS: list[tuple[str, Any, str]] = [
    ("domain.protocols", ["not_a_protocol"], "D_ENUM"),
    ("domain.protocols", ["liquidity_book"], "D_ENUM"),  # beyond the CPMM-only ceiling
    ("domain.protocols", ["constant_product", "concentrated"], "D_ENUM"),
    ("domain.protocols", [], "D_ENUM"),
    ("domain.protocols", ["constant_product", "constant_product"], "D_ENUM"),
    ("domain.protocols", "constant_product", "D_MISSING_FIELD"),
    ("domain.universe.bundle", "@delete", "D_MISSING_FIELD"),
    ("domain.universe.cohort", "@delete", "D_MISSING_FIELD"),
    ("domain.universe.bundle", "e" * 64, "D_UNIVERSE"),  # another bundle than the run's
    ("domain.universe.bundle", 7, "D_MISSING_FIELD"),
    ("domain.universe.bundle", "", "D_MISSING_FIELD"),
    ("domain.universe.cohort", "not_a_cohort", "D_ENUM"),
    ("domain.universe.cohort", None, "D_ENUM"),
    ("domain.universe.pools", ["p1", "p1"], "D_UNIVERSE"),
]


@pytest.mark.parametrize(("path", "value", "code"), DOMAIN_MUTATIONS, ids=lambda v: str(v)[:24])
def test_a_certificate_over_an_unregistered_or_foreign_domain_is_never_certified(
    path: str, value: Any, code: str
) -> None:
    rec, ctx = _grid()
    _set(rec, path, value)
    if path == "domain.universe.bundle" and value == "@delete":
        del rec["domain"]["universe"]["cohort"]  # the parent's repro deletes both
    _rehashed(rec)
    view = dx.diagnostics_view(rec, ctx)
    assert view["state"] == "invalid" and code in view["codes"], view
    assert "certified [" not in dx.bound_text(view)


GRID_POOLS = {"p1": "constant_product", "p2": "constant_product", "q9": "concentrated"}


def test_domain_pools_are_checked_against_the_runs_own_bundle() -> None:
    rec, ctx = _grid()
    with_pools = CheckContext(**{**ctx.__dict__, "pools": GRID_POOLS})
    assert dx.check_diagnostics(rec, with_pools) == set()
    assert "pools" not in dx.diagnostics_view(rec, with_pools)["checked_against"]
    # a pool the run's bundle does not have
    foreign = copy.deepcopy(rec)
    foreign["domain"]["universe"]["pools"] = foreign["domain"]["pool_order"] = ["p1", "zz"]
    assert dx.check_diagnostics(_rehashed(foreign), with_pools) == {"D_UNIVERSE"}
    # a certificate over a pool whose protocol the domain does not declare
    mixed = copy.deepcopy(rec)
    mixed["domain"]["universe"]["pools"] = mixed["domain"]["pool_order"] = ["p1", "q9"]
    assert dx.check_diagnostics(_rehashed(mixed), with_pools) == {"D_UNIVERSE"}
    # ... but the same universe on an unsupported row without a certificate is its declared
    # scope (WHI-1551 P-IA-UNSUPPORTED-MIXED: no CPMM-subset fallback), not a bound
    mixed.update(
        certificate=None,
        certificate_unavailable_reason="not_produced",
        scope={"supported": False, "reason": "non_constant_product_direct_pool"},
    )
    unsupported = CheckContext(**{**with_pools.__dict__, "status": "unsupported", "score": None})
    assert dx.check_diagnostics(mixed, unsupported) == set()


def test_legitimate_empty_and_narrowed_domains_stay_valid() -> None:
    rec, ctx = _grid()
    with_pools = CheckContext(**{**ctx.__dict__, "pools": GRID_POOLS})
    # a narrowed pool set of the same bundle and a registered narrower cohort
    narrow = copy.deepcopy(rec)
    narrow["domain"]["universe"]["cohort"] = "sor_compatible"
    assert dx.check_diagnostics(_rehashed(narrow), with_pools) == set()
    one = copy.deepcopy(rec)
    one["domain"]["universe"]["pools"] = one["domain"]["pool_order"] = ["p2"]
    one["domain"]["splits"]["max"] = 1
    assert dx.check_diagnostics(_rehashed(one), with_pools) == set()
    # the complete empty domain of a no-route case (no admitted pool)
    empty = copy.deepcopy(rec)
    empty["domain"]["universe"]["pools"] = empty["domain"]["pool_order"] = []
    empty.update(certificate=None, certificate_unavailable_reason="not_produced", work={})
    no_route = CheckContext(**{**with_pools.__dict__, "status": "no_route", "score": None})
    assert dx.check_diagnostics(_rehashed(empty), no_route) == set()


def test_identity_ceilings_follow_the_contract() -> None:
    rec, ctx = _grid()  # direct_split_certified: constant product only
    for algorithm, allowed in (
        ("cfmm_dual", True),  # constant product + concentrated
        ("uni_sor_cycle_safe", True),
        ("metis_history", True),
    ):
        other = copy.deepcopy(rec)
        other["domain"]["protocols"] = ["constant_product", "concentrated"]
        other["algorithm"] = algorithm
        other["max_candidates_unit"] = dx.IDENTITIES[algorithm].max_candidates_unit
        other["certificate"]["source"]["algorithm"] = algorithm
        run = {**ctx.run, "algorithm": algorithm}
        codes = dx.check_diagnostics(_rehashed(other), CheckContext(**{**ctx.__dict__, "run": run}))
        assert (codes == set()) is allowed, (algorithm, codes)
    lb = copy.deepcopy(rec)
    lb["domain"]["protocols"] = ["constant_product", "liquidity_book"]
    lb["algorithm"] = lb["certificate"]["source"]["algorithm"] = "cfmm_dual"
    lb["max_candidates_unit"] = "fallback_paths_evaluated"
    run = {**ctx.run, "algorithm": "cfmm_dual"}
    assert dx.check_diagnostics(_rehashed(lb), CheckContext(**{**ctx.__dict__, "run": run})) == {
        "D_ENUM"
    }


# ------------------------------------------------------------------ repair (BLOCKER-3)

# suffix-repair.md §8 as committed by WHI-1553 (3d0afbd): the `search_stats["repair"]` object
# without its path counters.
WHI1553_REPAIR: dict[str, Any] = {
    "enabled": True,
    "stop": "exhausted",
    "checkpoint_restores": 3,
    "repair_attempts": 4,
    "candidates_complete": 4,
    "candidates_failed": 0,
    "duplicates": 1,
    "rejected_worse": 1,
    "ties": 0,
    "accepted": 2,
    "consistency_failures": 0,
    "internal_evaluations": 4,
    "accepted_log": [
        {"checkpoint": 0, "alternative": 0, "score": "111176933"},
        {"checkpoint": 0, "alternative": 3, "score": "111178819"},
    ],
}


@pytest.mark.parametrize(
    "repair",
    [
        {"accepted": 1, "accepted_log": [{"checkpoint": 0, "score": "58"}]},  # parent's literal
        WHI1553_REPAIR,
        {**WHI1553_REPAIR, "enabled": False, "stop": "disabled", "accepted": 0, "accepted_log": []},
    ],
    ids=["parent-literal", "whi1553-preset", "whi1553-repair-off"],
)
def test_the_declared_repair_shape_validates_and_renders(repair: dict[str, Any]) -> None:
    rec, ctx = _grid()
    rec["repair"] = copy.deepcopy(repair)
    view = dx.diagnostics_view(rec, ctx)
    assert view["state"] == "certified" and view["repair"] == repair
    text = dx.fallback_text(view)
    assert text is not None and text.startswith("repair ")
    log = repair["accepted_log"]
    if log:
        first = log[0]
        assert f"accepted_log [checkpoint {first['checkpoint']}" in text
        assert f"score {first['score']}" in text
    else:
        assert "accepted_log []" in text
    # deterministic: the same fields in another insertion order render identically
    reordered = {k: repair[k] for k in reversed(list(repair))}
    rec["repair"] = reordered
    assert dx.fallback_text(dx.diagnostics_view(rec, ctx)) == text
    persisted = dx.read_view({"diagnostics": json.loads(json.dumps(view, sort_keys=True))})
    assert persisted is not None and dx.fallback_text(persisted) == text


def test_a_long_accepted_log_is_rendered_bounded() -> None:
    rec, ctx = _grid()
    log = [{"checkpoint": i, "alternative": 0, "score": str(58 + i)} for i in range(20)]
    rec["repair"] = {"accepted": 20, "accepted_log": log}
    text = dx.fallback_text(dx.diagnostics_view(rec, ctx))
    assert text is not None and text.count("checkpoint ") == 8 and "; +12 more]" in text
    rec["repair"] = {"accepted_log": log * 52}  # 1040 entries: over the bound
    assert dx.check_diagnostics(rec, ctx) == {"S_SHAPE"}


@pytest.mark.parametrize(
    "repair",
    [
        {"accepted_log": [{"checkpoint": 0}]},  # no score
        {"accepted_log": [{"checkpoint": 0, "score": 58}]},  # int score
        {"accepted_log": [{"checkpoint": -1, "score": "58"}]},
        {"accepted_log": [{"checkpoint": True, "score": "58"}]},
        {"accepted_log": [{"checkpoint": 0, "score": "58", "plan": "x"}]},  # undeclared field
        {"accepted_log": [{"checkpoint": 0, "score": "5" * 101}]},
        {"accepted_log": {"checkpoint": 0, "score": "58"}},  # not a list
        {"accepted_log": [[0, "58"]]},
        {"windows": [{"inner": [1, 2]}]},  # deeper than flat entries
        {"windows": [{"inner": {"a": 1}}]},
        {"windows": [[{"a": 1}]]},
        {"windows": {"a": 1}},
        {"note": "line\nbreak"},
        {"note": "x" * 201},
        {"n": 10**19},
        {"n": 1.5},
        {"": 1},
        {f"k{i}": i for i in range(33)},
        {"windows": [{f"k{i}": i for i in range(9)}]},
        [1, 2],
    ],
    ids=lambda r: json.dumps(r, default=str)[:40],
)
def test_arbitrary_or_unbounded_repair_objects_are_refused(repair: Any) -> None:
    rec, ctx = _grid()
    rec["repair"] = repair
    view = dx.diagnostics_view(rec, ctx)
    assert view["state"] == "invalid" and view["codes"] == ["S_SHAPE"], view
    assert any(d.startswith("S_SHAPE: repair") for d in view["details"])


def _whi1553_harness() -> Any:
    """WHI-1553's merged executable specification (dev 9adaa47), loaded by path under a
    private name so none of its tests is collected twice."""
    import importlib.util
    import sys

    path = REPO / "tests" / "routing" / "test_suffix_repair_contract.py"
    spec = importlib.util.spec_from_file_location("_whi1553_suffix_repair", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_whi1553_repair_records_validate_and_render_at_runtime() -> None:
    """The consumer shape WHI-1553 publishes (suffix-repair.md §8: `repair` without its path
    counters, with `accepted_log`) is a valid runtime view, checked against the harness's own
    bundle and its pools, and its accepted_log is rendered exactly."""
    from pools.quote import metered_quotes
    from routing.algorithms.base import Budget

    h = _whi1553_harness()
    seen_logs = 0
    for name in ("structural_trap", "twin_pools"):
        bundle, case, spec = h.fixture_case(name)
        for options in (h.PRESET, h.REPAIR_OFF):
            with metered_quotes(None) as meter:
                got = h.repair_solve(case, h.context(bundle, spec["settings"]), Budget(), options)
            rec = h.diagnostics_record(got, bundle, spec["settings"])
            ctx = CheckContext(
                run={
                    "git_revision": "r",
                    "bundle_hash": bundle.bundle_hash,
                    "algorithm": rec["algorithm"],
                    "effective_settings_sha256": "s",
                },
                request={"case_id": case.case_id, "token_in": case.token_in,
                         "token_out": case.token_out, "amount_in": str(case.amount_in)},
                status=got.result.status.value,
                score=None if got.result.score is None else str(got.result.score),
                objective="gross_only",
                quotes_counted=meter.counted,
                pools={pid: dx.pool_protocol(p) for pid, p in bundle.pools.items()},
            )  # fmt: skip
            view = dx.diagnostics_view(rec, ctx)
            assert (view["state"], view["codes"]) == ("unavailable", []), (name, view)
            assert view["repair"] == rec["repair"]
            text = dx.fallback_text(view) or ""
            log = rec["repair"]["accepted_log"]
            for entry in log[:8]:
                assert (
                    f"checkpoint {entry['checkpoint']} alternative {entry['alternative']} "
                    f"score {entry['score']}" in text
                )
            seen_logs += bool(log)
            # the same record for another bundle's run is not this run's domain
            other = CheckContext(**{**ctx.__dict__, "run": {**ctx.run, "bundle_hash": "e" * 64}})
            assert dx.check_diagnostics(rec, other) == {"D_UNIVERSE"}
    assert seen_logs >= 1  # at least one real acceptance log was exercised


# ------------------------------------------------------------------ canonical r021 (DEFECT-1)


def test_canonical_r021_never_reprs_the_untrusted_value() -> None:
    rec, _ = _grid()
    text = dx.canonical_r021(rec)
    assert text == json.dumps(rec, sort_keys=True)
    staged = {**rec, "stages": {"bound": 1.5}}
    assert dx.canonical_r021(staged) == text  # observational seconds are not compared
    huge = copy.deepcopy(rec)
    huge["work"]["paths_scored"] = 10**5000  # repr() and JSON both refuse it
    with pytest.raises(ValueError, match="4300"):
        repr(huge)
    assert dx.canonical_r021(huge) == "<r021 diagnostics not JSON-serializable: ValueError>"
    assert dx.canonical_r021(_Hostile()).startswith("<r021 diagnostics not JSON-serializable")
    assert dx.canonical_r021({"a", "b"}).startswith("<r021 diagnostics not JSON-serializable")


# ------------------------------------------------------------------ run identity / request


@pytest.mark.parametrize("key", dx.RUN_IDENTITY_KEYS)
def test_a_certificate_of_another_run_identity_fails_c_identity(key: str) -> None:
    rec, ctx = _grid()
    # the runner's own value differs from the certificate's (nonempty, otherwise valid)
    run = {**ctx.run, key: "f" * 64}
    extra = {  # another run bundle is also another domain universe; another algorithm has
        # no §3.3 row to check its unit against
        "bundle_hash": {"D_UNIVERSE"},
        "algorithm": {"W_MAX_CANDIDATES"},
    }.get(key, set())
    assert dx.check_diagnostics(rec, CheckContext(**{**ctx.__dict__, "run": run})) == (
        {"C_IDENTITY"} | extra
    )


def test_a_run_without_a_recorded_revision_certifies_nothing() -> None:
    rec, ctx = _grid()
    run = {**ctx.run, "git_revision": None}
    assert dx.check_diagnostics(rec, CheckContext(**{**ctx.__dict__, "run": run})) == {"C_IDENTITY"}


@pytest.mark.parametrize("key", dx.REQUEST_KEYS)
def test_a_proof_for_another_request_with_the_same_score_fails_c_request(key: str) -> None:
    rec, ctx = _grid()
    other = {**ctx.request, key: "39" if key == "amount_in" else "other"}
    # the score (58) and every other field stay valid for the runner's request
    assert dx.check_diagnostics(rec, CheckContext(**{**ctx.__dict__, "request": other})) == {
        "C_REQUEST"
    }


def test_the_diagnostics_must_name_the_runs_algorithm() -> None:
    rec, ctx = _grid()
    rec["algorithm"] = "direct_split"
    assert "C_IDENTITY" in dx.check_diagnostics(rec, ctx)


def test_an_algorithm_without_a_contract_row_is_not_validated_as_certified() -> None:
    rec, ctx = _grid()
    rec["algorithm"] = rec["certificate"]["source"]["algorithm"] = "unlisted"
    run = {**ctx.run, "algorithm": "unlisted"}
    assert dx.check_diagnostics(rec, CheckContext(**{**ctx.__dict__, "run": run})) == {
        "W_MAX_CANDIDATES"
    }


def test_a_certificate_without_an_evaluated_incumbent_fails_c_lower_eval() -> None:
    rec, ctx = _grid()
    for status, score in (("invalid_plan", None), ("no_route", None), ("ok", None)):
        got = dx.check_diagnostics(
            rec, CheckContext(**{**ctx.__dict__, "status": status, "score": score})
        )
        assert got == {"C_LOWER_EVAL"}, status


def test_pool_order_is_a_permutation_of_the_universe() -> None:
    rec, ctx = _grid()
    for order in ([], ["p1"], ["p1", "p1"], ["p1", "p3"]):
        rec["domain"]["pool_order"] = order
        rec["candidate_domain_hash"] = rec["certificate"]["candidate_domain_hash"] = dx.domain_hash(
            rec["domain"]
        )
        assert dx.check_diagnostics(rec, ctx) == {"D_POOL_ORDER"}, order
    # no admitted pool at all: the empty order is the admitted order (WHI-1551's complete
    # empty domain, `certificate: null`, `not_produced`, `no_route`)
    rec["domain"]["universe"]["pools"] = rec["domain"]["pool_order"] = []
    rec["candidate_domain_hash"] = dx.domain_hash(rec["domain"])
    rec.update(certificate=None, certificate_unavailable_reason="not_produced", work={})
    empty = CheckContext(**{**ctx.__dict__, "status": "no_route", "score": None})
    assert dx.check_diagnostics(rec, empty) == set()
    assert dx.diagnostics_view(rec, empty)["state"] == "unavailable"


def test_a_returned_solve_cannot_claim_hard_timeout() -> None:
    rec = copy.deepcopy(POSITIVES["P-UNAVAILABLE"]["record"])
    ctx = _ctx({**POSITIVES["P-UNAVAILABLE"]["context"], "hard_killed": False, "status": "ok"})
    assert dx.check_diagnostics(rec, ctx) == {"C_UNAVAILABLE_REASON"}
    rec["certificate_unavailable_reason"] = "not_produced"
    assert dx.check_diagnostics(rec, ctx) == set()
    assert dx.diagnostics_view(rec, ctx)["state"] == "unavailable"


# ------------------------------------------------------------------ malformed input


class _Hostile(Mapping[str, Any]):
    """A mapping whose every access raises: the validator must not propagate it."""

    def __getitem__(self, key: str) -> Any:
        raise RuntimeError("hostile")

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("hostile")

    def __len__(self) -> int:
        return 1


class _HostileDict(dict[str, Any]):
    """Serializes as ordinary JSON, but every keyed access raises."""

    def get(self, key: str, default: Any = None) -> Any:
        raise RuntimeError("hostile")

    def __getitem__(self, key: str) -> Any:
        raise RuntimeError("hostile")


def _deep(levels: int) -> dict[str, Any]:
    node: dict[str, Any] = {}
    root = node
    for _ in range(levels):
        node["x"] = {}
        node = node["x"]
    return root


MALFORMED_TOP: list[Any] = [
    None,
    [],
    "r021",
    58,
    1.5,
    True,
    {"schema"},
    _Hostile(),
    _HostileDict(schema="r021.diagnostics/1"),
    _deep(100_000),
]


@pytest.mark.parametrize("raw", MALFORMED_TOP, ids=lambda r: type(r).__name__)
def test_a_non_object_is_invalid_and_never_raises(raw: Any) -> None:
    rec, ctx = _grid()
    view = dx.diagnostics_view(raw, ctx)
    assert view["state"] == "invalid" and view["codes"] == ["S_SHAPE"]
    assert dx.bound_text(view).startswith("invalid diagnostics (S_SHAPE)")


MUTATIONS: list[tuple[str, Any, str]] = [
    # (dotted path, value, one expected code)
    ("schema", "r021.diagnostics/2", "S_SHAPE"),
    ("contract", "R021-C/2", "S_SHAPE"),
    ("extra", 1, "S_SHAPE"),
    ("work", "@delete", "S_SHAPE"),
    ("certificate_unavailable_reason", "@delete", "S_SHAPE"),
    ("certificate", [1, 2], "S_SHAPE"),
    ("certificate.schema", None, "S_SHAPE"),
    ("certificate.bonus", "x", "S_SHAPE"),
    ("certificate.source", "81559ab", "C_IDENTITY"),
    ("certificate.source.git_revision", 81559, "C_IDENTITY"),
    ("certificate.source.bundle_hash", "", "C_IDENTITY"),
    ("certificate.request", ["r021-grid38"], "C_REQUEST"),
    ("certificate.request.amount_in", 38, "C_REQUEST"),
    ("certificate.request.amount_in", "38.0", "C_REQUEST"),
    ("certificate.lower_raw", 58.0, "C_AMOUNT_TYPE"),
    ("certificate.lower_raw", True, "C_AMOUNT_TYPE"),
    ("certificate.lower_raw", " 58", "C_AMOUNT_TYPE"),
    ("certificate.lower_raw", "0x3a", "C_AMOUNT_TYPE"),
    ("certificate.lower_raw", "058", "C_AMOUNT_TYPE"),
    ("certificate.lower_raw", "9" * 5000, "C_AMOUNT_TYPE"),
    ("certificate.upper_raw", "NaN", "C_AMOUNT_TYPE"),
    ("certificate.gap_raw", 0, "C_AMOUNT_TYPE"),
    ("certificate.bound_kind", None, "C_BOUND_KIND"),
    ("certificate.bound_kind", ["certified"], "C_BOUND_KIND"),
    ("certificate.upper_source", 1, "C_CERTIFIED_SOURCE"),
    ("certificate.optimality_proven", "true", "C_OPTIMALITY"),
    ("certificate.optimality_proven", 1, "C_OPTIMALITY"),
    ("certificate.termination", 5, "C_TERMINATION"),
    ("certificate.termination", "@delete", "C_TERMINATION"),
    ("certificate.objective", "@delete", "C_OBJECTIVE"),
    ("certificate.candidate_domain_hash", None, "C_DOMAIN"),
    ("certificate.estimate", {"value": "nan", "residual": "0", "tolerance": "0"}, "C_AMOUNT_TYPE"),
    (
        "certificate.estimate",
        {"value": "Infinity", "residual": "0", "tolerance": "0"},
        "C_AMOUNT_TYPE",
    ),
    ("certificate.estimate", {"value": 59.3}, "C_AMOUNT_TYPE"),
    ("work", [41], "W_TYPE"),
    ("work.quotes_executed", 41.0, "W_TYPE"),
    ("work.quotes_executed", -1, "W_TYPE"),
    ("work.bb_nodes_expanded", "1", "W_TYPE"),
    ("work.paths", 1, "W_UNIT"),
    ("domain", None, "D_MISSING_FIELD"),
    ("domain", "repository_grid", "D_MISSING_FIELD"),
    ("domain.hops", "1", "D_MISSING_FIELD"),
    ("domain.hops.max", -1, "D_ENUM"),
    ("domain.splits", [2], "D_MISSING_FIELD"),
    ("domain.splits.governs", ["allocation"], "D_ENUM"),
    ("domain.universe", "p1,p2", "D_MISSING_FIELD"),
    ("domain.universe.pools", [1, 2], "D_MISSING_FIELD"),
    ("domain.protocols", "constant_product", "D_MISSING_FIELD"),
    ("domain.pool_order", {"p1": 0}, "D_POOL_ORDER"),
    ("domain.amount_grid", "repository_grid", "D_ENUM"),
    ("domain.schema", "r021.domain/2", "D_ENUM"),
    ("domain.pool_reuse", ["disjoint"], "D_ENUM"),
    ("fallback", {"used": "yes"}, "S_SHAPE"),
    ("fallback", {"used": True, "source": 3}, "S_SHAPE"),
    ("fallback", {"used": True, "reason": "\x1b[31mred"}, "S_SHAPE"),
    ("scope", {"supported": False, "reason": "x" * 201}, "S_SHAPE"),
    ("repair", {"note\n": 1}, "S_SHAPE"),
    ("repair", [1], "S_SHAPE"),
    ("repair", {"attempts": 1.5}, "S_SHAPE"),
    ("scope", {"supported": 1}, "S_SHAPE"),
    ("stages", {"solve": -1}, "S_SHAPE"),
    ("stages", {"solve": True}, "S_SHAPE"),
    ("stages", {"solve": float("nan")}, "S_SHAPE"),
    ("stages", {"solve": float("inf")}, "S_SHAPE"),
    ("work.quotes_memoized", {1, 2}, "S_SHAPE"),  # not JSON-serializable at all
]


@pytest.mark.parametrize(("path", "value", "code"), MUTATIONS, ids=lambda v: str(v)[:30])
def test_malformed_fields_are_invalid_views_not_crashes(path: str, value: Any, code: str) -> None:
    rec, ctx = _grid()
    _set(rec, path, value)
    codes = dx.check_diagnostics(rec, ctx)
    assert code in codes
    view = dx.diagnostics_view(rec, ctx)
    assert view["state"] == "invalid" and code in view["codes"]
    json.dumps(view, sort_keys=True, allow_nan=False)  # the view itself is always persistable
    assert all(len(d) < 400 for d in view["details"])  # untrusted values are shown bounded
    shown = dx.read_view({"diagnostics": view})
    assert shown is not None and shown["state"] == "invalid"
    assert "certified [" not in dx.bound_text(shown)


def test_detail_messages_are_deterministic_across_hash_seeds() -> None:
    import subprocess
    import sys

    code = (
        "from benchmark import diagnostics as dx;"
        "from benchmark.diagnostics import CheckContext as C;"
        "ctx = C(run={}, request={}, status='ok', score='1', objective='gross_only',"
        " quotes_counted=0);"
        "print(dx.diagnostics_view({'x', 'y', 'z', 'w'}, ctx)['details'])"
    )
    seen = {
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO,
            env={"PYTHONHASHSEED": seed, "PYTHONPATH": str(REPO)},
        ).stdout
        for seed in ("1", "2", "3", "4")
    }
    assert len(seen) == 1 and "set of 4 item(s)" in seen.pop()


def test_unserializable_objects_get_a_visible_marker_instead_of_crashing_the_writer() -> None:
    assert dx.unserializable_marker({"a": 1}) is None
    assert dx.unserializable_marker(float("nan")) is None  # the writer tolerates NaN tokens
    for raw in ({"a", "b"}, {"work": {1: 2, "x": 3}}, object(), _deep(100_000)):
        marker = dx.unserializable_marker(raw)
        assert marker is not None and marker.startswith("<r021 diagnostics not JSON")


def test_valid_optional_parts_are_rendered_with_their_names() -> None:
    rec, ctx = _grid()
    rec.update(
        fallback={"used": True, "source": "path_split", "reason": "no graph route"},
        repair={"windows": 2, "accepted": False},
        scope={"supported": False, "reason": "objective outside ceiling"},
        stages={"incumbent": 0.25, "bound": 0.5},
    )
    view = dx.diagnostics_view(rec, ctx)
    assert view["state"] == "certified", view
    assert dx.fallback_text(view) == (
        "fallback used (source path_split; reason no graph route); repair accepted False, windows 2"
    )
    assert dx.scope_text(view) == "UNSUPPORTED (objective outside ceiling)"
    assert dx.stages_text(view) == "incumbent 0.250000 s, bound 0.500000 s"
    assert dx.work_text(view) == (
        "quotes_executed 41 [quote], quotes_memoized 2 [quote], internal_evaluations 2 "
        "[validation], bb_nodes_expanded 1 [search], bound_evaluations 1 [search], "
        "peak_open_nodes 1 [memory]"
    )
    assert dx.domain_text(view) == "e1afda09c38f (repository_grid)"


# ------------------------------------------------------------------ persisted views


def test_read_view_is_none_for_records_without_diagnostics() -> None:
    assert dx.read_view({"search": {"paths_evaluated": 3}}) is None
    assert dx.read_view({"search": {}, "diagnostics": None}) is None


def test_an_unvalidated_solver_object_is_unknown_never_certified() -> None:
    rec, _ = _grid()
    view = dx.read_view({"search": {"r021": rec}})
    assert view is not None and view["state"] == "unknown"
    assert dx.bound_text(view) == "unknown (no bound: diagnostics not validated by the runner)"


def _certified_view() -> dict[str, Any]:
    rec, ctx = _grid()
    return dx.diagnostics_view(rec, ctx)


@pytest.mark.parametrize(
    "patch",
    [
        {"schema": "other"},
        {"state": "proven"},
        {"codes": "none"},
        {"codes": ["C_GAP"]},
        {"gap": "1"},
        {"upper": "57"},
        {"lower": 58},
        {"state": "estimate"},
        {"state": "unavailable"},
        {"state": "invalid"},
    ],
    ids=lambda p: json.dumps(p),
)
def test_a_self_inconsistent_persisted_view_is_invalid(patch: dict[str, Any]) -> None:
    view = {**_certified_view(), **patch}
    shown = dx.read_view({"diagnostics": view})
    assert shown is not None and shown["state"] == "invalid" and shown["codes"] == ["S_SHAPE"]
    assert "certified [" not in dx.bound_text(shown)


def test_runner_unavailable_views() -> None:
    for reason in dx.UNAVAILABLE_REASONS:
        view = dx.unavailable_view(reason)
        assert dx.read_view({"diagnostics": view}) == view
        assert dx.bound_text(view) == f"unavailable ({reason})"
    with pytest.raises(ValueError, match="unregistered"):
        dx.unavailable_view("cancelled")

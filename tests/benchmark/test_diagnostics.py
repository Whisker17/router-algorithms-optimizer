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
    run = dict(cert["source"]) if cert else dict.fromkeys(dx.RUN_IDENTITY_KEYS, "x")
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


# ------------------------------------------------------------------ run identity / request


@pytest.mark.parametrize("key", dx.RUN_IDENTITY_KEYS)
def test_a_certificate_of_another_run_identity_fails_c_identity(key: str) -> None:
    rec, ctx = _grid()
    # the runner's own value differs from the certificate's (nonempty, otherwise valid)
    run = {**ctx.run, key: "f" * 64}
    assert dx.check_diagnostics(rec, CheckContext(**{**ctx.__dict__, "run": run})) == (
        {"C_IDENTITY"} if key != "algorithm" else {"C_IDENTITY", "W_MAX_CANDIDATES"}
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
        "fallback used (source path_split; reason no graph route); repair windows 2, accepted False"
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

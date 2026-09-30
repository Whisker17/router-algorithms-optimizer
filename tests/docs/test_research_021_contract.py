"""R021-C/1 contract checks (docs/references/research-021/contract.md, WHI-1547).

Three kinds of evidence, kept apart:

1. The archived external report is byte-identical to the owner-supplied file (pinned hash).
2. The contract's example records (fixtures/examples.json) are accepted or rejected exactly
   as declared by a small validator of the contract rules defined here. This validator is a
   specification check for WHI-1548, not a runtime component.
3. NEW reconstructions (fixtures/reconstructions.json) of selected external counterexamples
   and handoff corrections. Expected values come from this module's own hand integer
   formula or from committed artifacts, never from the external author's absent
   verify_research.py/results.json and never from the solver under test; the repository
   evaluator/solver is then checked against them where stated.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import re
from collections.abc import Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from benchmark.objective import gross_only
from benchmark.strategies import R021_ADDITIONS
from routing.algorithms import direct_split
from routing.algorithms.base import AlgorithmConfig, Budget, SolveContext, SolveStatus
from routing.algorithms.registry import ALGORITHMS, BASE_STRATEGIES, OPTIMIZED_STRATEGIES
from routing.evaluator import EvalStatus, evaluate
from routing.plan import ALL_REMAINING, REQUEST_FUND_ID, FundInput, RoutePlan, SwapStep
from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle

ROOT = Path(__file__).resolve().parents[2]
R021 = ROOT / "docs" / "references" / "research-021"
CONTRACT = json.loads((R021 / "contract-v1.json").read_text(encoding="utf-8"))
EXAMPLES = json.loads((R021 / "fixtures" / "examples.json").read_text(encoding="utf-8"))
RECON = json.loads((R021 / "fixtures" / "reconstructions.json").read_text(encoding="utf-8"))
PROSE = (R021 / "contract.md").read_text(encoding="utf-8")

REPORT_SHA256 = "0588e83ab19a07bc0da4c6f7df42c60b7bd5e572d6903ab86d62318c388ba1d9"
REPORT_BYTES = 33416
BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


# ------------------------------------------------------------------ hand arithmetic


def _q(x: int, reserves: Sequence[int], fee_bps: int) -> int:
    """Independent hand CPMM exact-input output (Uniswap V2 getAmountOut in bps form);
    `reserves` is (input-side, output-side)."""
    if x == 0:
        return 0
    r_in, r_out = reserves
    keep = 10_000 - fee_bps
    return x * keep * r_out // (r_in * 10_000 + x * keep)


def _f(x: Fraction, reserves: Sequence[int], fee_bps: int) -> Fraction:
    """Continuous CPMM output (no final floor)."""
    r_in, r_out = reserves
    keep = 10_000 - fee_bps
    return x * keep * r_out / (r_in * 10_000 + x * keep)


def _df(x: Fraction, reserves: Sequence[int], fee_bps: int) -> Fraction:
    r_in, r_out = reserves
    keep = 10_000 - fee_bps
    return Fraction(keep * r_out * r_in * 10_000) / (r_in * 10_000 + x * keep) ** 2


Pools = dict[str, list[Any]]


def _step(pools: Pools, pid: str, token: str, amount: int, fee_bps: int) -> tuple[str, int]:
    t0, t1, r0, r1 = pools[pid]
    if token == t0:
        return t1, _q(amount, (r0, r1), fee_bps)
    assert token == t1
    return t0, _q(amount, (r1, r0), fee_bps)


def _walk(
    pools: Pools, path: list[str], token: str, amount: int, fee_bps: int
) -> tuple[list[str], int]:
    tokens = [token]
    for pid in path:
        token, amount = _step(pools, pid, token, amount, fee_bps)
        tokens.append(token)
    return tokens, amount


def _simple_paths(
    pools: Pools, source: str, target: str, max_hops: int
) -> list[tuple[list[str], list[str]]]:
    """Every pool-distinct, token-distinct path (independent exhaustive enumeration)."""
    out: list[tuple[list[str], list[str]]] = []

    def go(token: str, path: list[str], tokens: list[str]) -> None:
        if token == target:
            out.append((list(path), list(tokens)))
            return
        if len(path) == max_hops:
            return
        for pid, (t0, t1, _, _) in pools.items():
            if pid in path or token not in (t0, t1):
                continue
            nxt = t1 if token == t0 else t0
            if nxt in tokens:
                continue
            go(nxt, [*path, pid], [*tokens, nxt])

    go(source, [], [source])
    return out


# ------------------------------------------------------------------ repository objects


def _bundle(pools: Pools, fee_bps: int, order: list[str] | None = None) -> SnapshotBundle:
    states = {
        pid: ConstantProductPoolState(
            pool_id=pid,
            token0=t0,
            token1=t1,
            reserve0=r0,
            reserve1=r1,
            fee_bps=fee_bps,
            source_key=None,
        )
        for pid, (t0, t1, r0, r1) in pools.items()
    }
    ids = order if order is not None else list(pools)
    return SnapshotBundle(
        bundle_id="r021",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools={pid: states[pid] for pid in ids},
        cases=(),
        bundle_hash="r021",
        source_path="<test>",
    )


def _plan(pools: Pools, source: str, legs: list[tuple[list[str], int | None]]) -> RoutePlan:
    """Ordered steps for legs from the request fund; amount None = ALL_REMAINING."""
    steps: list[SwapStep] = []
    n = 0
    for path, amount in legs:
        fund: str = REQUEST_FUND_ID
        token = source
        for i, pid in enumerate(path):
            t0, t1, _, _ = pools[pid]
            out = t1 if token == t0 else t0
            n += 1
            ref = FundInput(fund, ALL_REMAINING if (i > 0 or amount is None) else amount)
            steps.append(SwapStep(pid, token, out, (ref,), f"F{n}"))
            fund, token = f"F{n}", out
    return RoutePlan(steps=tuple(steps))


def _evaluate(pools: Pools, fee: int, case: Case, legs: list[tuple[list[str], int | None]]) -> Any:
    return evaluate(_bundle(pools, fee), case, _plan(pools, case.token_in, legs), gross_only())


# ------------------------------------------------------------------ contract validator


def _is_decimal(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"-?(0|[1-9][0-9]*)", value) is not None


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


IDENTITIES: dict[str, dict[str, Any]] = {
    **CONTRACT["existing_identities"],
    **{entry["id"]: entry for entry in CONTRACT["identities"]},
}
NUMERIC_UNITS = {u for u, cat in CONTRACT["work_units"].items() if cat == "numeric"}
CERTIFIED_SOURCES = {"exhaustive", "exact_rational", "outward_rounded"}
RUN_IDENTITY_KEYS = ("git_revision", "bundle_hash", "algorithm", "effective_settings_sha256")
REQUEST_KEYS = ("case_id", "token_in", "token_out", "amount_in")


def check_domain(domain: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    if any(f not in domain for f in CONTRACT["domain_fields"]):
        found.add("D_MISSING_FIELD")
    enums = {
        "token_reuse": CONTRACT["token_reuse"],
        "pool_reuse": CONTRACT["pool_reuse"],
        "zero_output_leg": CONTRACT["zero_output_leg"],
        "dag_admission": CONTRACT["dag_admission"],
        "full_fill": CONTRACT["full_fill"],
    }
    for key, allowed in enums.items():
        if key in domain and domain[key] not in allowed:
            found.add("D_ENUM")
    grid = domain.get("amount_grid", {})
    if grid.get("kind") not in CONTRACT["grid_kinds"]:
        found.add("D_ENUM")
    if "hops" in domain and domain["hops"].get("param") not in CONTRACT["hops_params"]:
        found.add("D_ENUM")
    if "splits" in domain and domain["splits"].get("governs") not in CONTRACT["splits_governs"]:
        found.add("D_ENUM")
    order = domain.get("pool_order")
    if grid.get("kind") == "repository_grid" and "pool_order" in domain:
        if not order or sorted(order) != sorted(domain["universe"]["pools"]):
            found.add("D_POOL_ORDER")
    return found


def check_diagnostics(rec: dict[str, Any], ctx: dict[str, Any]) -> set[str]:
    found = check_domain(rec["domain"])
    if rec["candidate_domain_hash"] != _hash(rec["domain"]):
        found.add("D_HASH")
    identity = IDENTITIES[rec["algorithm"]]
    if rec["max_candidates_unit"] != identity["max_candidates_unit"]:
        found.add("W_MAX_CANDIDATES")
    work = rec["work"]
    for unit, value in work.items():
        if unit not in CONTRACT["work_units"]:
            found.add("W_UNIT")
        elif not _is_count(value):
            found.add("W_TYPE")
    if not ctx["hard_killed"] and "quotes_executed" in work:
        if work["quotes_executed"] != ctx["quotes_counted"]:
            found.add("W_LEDGER")
        if work.get("exact_replay_quotes", 0) > work["quotes_executed"]:
            found.add("W_LEDGER")
    if NUMERIC_UNITS & set(work) and "exact_replay_quotes" not in work:
        found.add("W_NUMERIC_REPLAY")

    cert = rec["certificate"]
    if cert is None:
        if rec["certificate_unavailable_reason"] not in CONTRACT["certificate_unavailable_reasons"]:
            found.add("C_UNAVAILABLE_REASON")
        return found
    if ctx["hard_killed"]:
        found.add("C_KILLED")
    # The certificate is bound to the run and the exact request that the runner supplies
    # independently (`ctx`); presence of nonempty fields alone proves nothing.
    source, run = cert["source"], ctx["run"]
    if (
        any(
            not isinstance(source.get(k), str) or not source[k] or source[k] != run[k]
            for k in RUN_IDENTITY_KEYS
        )
        or source["algorithm"] != rec["algorithm"]
    ):
        found.add("C_IDENTITY")
    request = cert.get("request")
    if (
        not isinstance(request, dict)
        or any(request.get(k) != ctx["request"][k] for k in REQUEST_KEYS)
        or not _is_decimal(request.get("amount_in"))
    ):
        found.add("C_REQUEST")
    if cert["candidate_domain_hash"] != rec["candidate_domain_hash"]:
        found.add("C_DOMAIN")
    objectives = identity.get("objectives")
    if cert["objective"] != ctx["objective"] or (
        isinstance(objectives, list) and cert["objective"] not in objectives
    ):
        found.add("C_OBJECTIVE")
    lower, upper, gap = cert["lower_raw"], cert["upper_raw"], cert["gap_raw"]
    if not _is_decimal(lower) or any(v is not None and not _is_decimal(v) for v in (upper, gap)):
        found.add("C_AMOUNT_TYPE")
        return found
    kind = cert["bound_kind"]
    certified = kind == "certified"
    if kind not in CONTRACT["bound_kinds"]:
        found.add("C_BOUND_KIND")
    if not certified and (upper is not None or gap is not None):
        found.add("C_UNCERTIFIED_BOUND")
    if certified:
        if upper is None or cert["upper_source"] not in CERTIFIED_SOURCES:
            found.add("C_CERTIFIED_SOURCE")
        else:
            if int(upper) < int(lower):
                found.add("C_ORDER")
            if gap is None or int(gap) != int(upper) - int(lower):
                found.add("C_GAP")
    closed = certified and upper is not None and int(upper) == int(lower)
    if cert["optimality_proven"] is not closed:
        found.add("C_OPTIMALITY")
    if cert["termination"] not in CONTRACT["terminations"]:
        found.add("C_TERMINATION")
    elif cert["termination"] == "complete" and certified and not closed:
        found.add("C_TERMINATION")
    if ctx["status"] == "ok" and ctx["final_score"] is not None and lower != ctx["final_score"]:
        found.add("C_LOWER_EVAL")
    return found


def check_comparison(rec: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    left, right = rec["left"], rec["right"]
    if rec["class"] not in CONTRACT["comparison_classes"]:
        found.add("K_CLASS")
    if rec["class"] == "same_domain" and any(
        left[k] != right[k] for k in ("candidate_domain_hash", "objective", "budget")
    ):
        found.add("K_SAME_DOMAIN")
    if left["objective"] != right["objective"] and (
        rec["class"] != "objective_mismatch" or rec["ranked"]
    ):
        found.add("K_OBJECTIVE")
    for metric in rec["metrics"]:
        units = (metric["numerator_unit"], metric["denominator_unit"])
        if units[0] != units[1] or any(u not in CONTRACT["work_units"] for u in units):
            found.add("K_UNIT_RATIO")
    return found


def check_timing(rec: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    if rec["mode"] == "quote" and (rec["samples"] != 1 or rec["percentiles"] is not None):
        found.add("T_SINGLE_PERCENTILE")
    if rec["host_load_contaminated"] and rec["verdict"] not in (None, "inconclusive"):
        found.add("T_CONTAMINATED_VERDICT")
    allowed = [None, *CONTRACT["timing_verdicts"], *CONTRACT["lane_verdicts"]]
    if rec["verdict"] not in allowed:
        found.add("T_VOCAB")
    return found


def check_disposition(rec: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    if rec["disposition"] not in CONTRACT["implementation_dispositions"]:
        found.add("V_VOCAB")
    if rec["research_outcome"] not in CONTRACT["research_outcomes"]:
        found.add("V_VOCAB")
    evidence = rec["evidence"]
    if evidence["split"] == "report" and evidence["holdout_exposure"] != "previously_exposed":
        found.add("V_EXPOSURE")
    if rec["research_outcome"] in ("no_go", "blocked") and rec["disposition"] != "not_implemented":
        found.add("V_NO_GO")
    if rec["loss_tolerance_bps"] is not None:
        found.add("V_TOLERANCE")
    return found


POSITIVES = {p["id"]: p for p in EXAMPLES["positives"]}


def _check(kind: str, rec: dict[str, Any], ctx: dict[str, Any] | None) -> set[str]:
    if kind == "diagnostics":
        assert ctx is not None
        return check_diagnostics(rec, ctx)
    return {
        "comparison": check_comparison,
        "timing": check_timing,
        "disposition": check_disposition,
    }[kind](rec)


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


@pytest.mark.parametrize("example", EXAMPLES["positives"], ids=lambda e: e["id"])
def test_positive_examples_satisfy_the_contract(example: dict[str, Any]) -> None:
    assert _check(example["kind"], example["record"], example.get("context")) == set()


@pytest.mark.parametrize("example", EXAMPLES["negatives"], ids=lambda e: e["id"])
def test_each_negative_example_fails_with_exactly_its_violation(example: dict[str, Any]) -> None:
    base = POSITIVES[example["base"]]
    rec = copy.deepcopy(base["record"])
    for dotted, value in example["patch"].items():
        _set(rec, dotted, value)
    ctx = None if base.get("context") is None else {**base["context"], **example["context_patch"]}
    if example["rehash"]:
        rec["candidate_domain_hash"] = _hash(rec["domain"])
        if rec.get("certificate"):
            rec["certificate"]["candidate_domain_hash"] = rec["candidate_domain_hash"]
    assert _check(base["kind"], rec, ctx) == {example["violation"]}


def test_example_domain_hashes_are_reproducible_and_order_sensitive() -> None:
    hashes = {name: _hash(dom) for name, dom in EXAMPLES["domains"].items()}
    assert hashes == EXAMPLES["domain_hashes"]
    grid, flipped = EXAMPLES["domains"]["grid38_p1p2"], EXAMPLES["domains"]["grid38_p2p1"]
    # Every parameter value is equal; only the admitted pool order differs.
    assert {k: v for k, v in grid.items() if k != "pool_order"} == {
        k: v for k, v in flipped.items() if k != "pool_order"
    }
    assert hashes["grid38_p1p2"] != hashes["grid38_p2p1"]


# ------------------------------------------------------------------ vocabulary vs prose/registry


def test_contract_vocabulary_appears_in_the_prose() -> None:
    assert f"R021-C/{CONTRACT['version']}" in PROSE and CONTRACT["publication_key"] in PROSE
    words: list[str] = [*IDENTITIES, *CONTRACT["work_units"], *CONTRACT["registered_exploration"]]
    for key in (
        "grid_kinds",
        "pool_reuse",
        "comparison_classes",
        "bound_kinds",
        "terminations",
        "certificate_unavailable_reasons",
        "research_outcomes",
        "implementation_dispositions",
        "timing_verdicts",
        "lane_verdicts",
        "domain_fields",
    ):
        words += CONTRACT[key]
    missing = sorted({w for w in words if f"`{w}`" not in PROSE})
    assert missing == []


def test_identities_match_the_registry_without_placeholders() -> None:
    roster = [*BASE_STRATEGIES, *OPTIMIZED_STRATEGIES, "metis_inspired"]
    assert list(CONTRACT["existing_identities"]) == roster
    new = [entry["id"] for entry in sorted(CONTRACT["identities"], key=lambda e: e["order"])]
    assert new == [
        "metis_history",
        "direct_split_certified",
        "incremental_graph_repair",
        "uni_sor_cycle_safe",
        "cfmm_dual",
    ]
    assert set(roster) <= set(ALGORITHMS)
    # Frozen names; a 0.2.1 factory is registered only by its implementation issue, and no
    # unimplemented identity has a placeholder (the contract issue registered none).
    implemented = ["metis_history", "incremental_graph_repair"]  # WHI-1550, WHI-1554
    assert [name for name in new if name in ALGORITHMS] == implemented
    assert R021_ADDITIONS == tuple(implemented)  # `--strategies all` appends exactly these
    assert len(roster) + len(new) == 14


# ------------------------------------------------------------------ archived source


def test_archived_external_report_is_byte_identical_to_the_pin() -> None:
    data = (R021 / "sources" / "research-report.md").read_bytes()
    assert len(data) == REPORT_BYTES
    assert hashlib.sha256(data).hexdigest() == REPORT_SHA256
    sums = (R021 / "sources" / "SHA256SUMS").read_text(encoding="utf-8")
    assert sums == f"{REPORT_SHA256}  research-report.md\n"


# ------------------------------------------------------------------ reconstructions


def test_r1_prefix_merge_loses_a_legal_path_and_the_walk_is_out_of_domain() -> None:
    r = RECON["R1_prefix_merge_vs_cycle"]
    pools, fee, amount = r["pools"], r["fee_bps"], r["amount_in"]
    paths = _simple_paths(pools, r["token_in"], r["token_out"], r["max_hops"])
    gross = {tuple(p): _walk(pools, p, r["token_in"], amount, fee)[1] for p, _ in paths}
    best = r["best_simple_path"]
    assert max(gross.values()) == gross[tuple(best["path"])] == best["gross"]
    assert _walk(pools, best["path"], "A", amount, fee)[0] == best["tokens"]  # no repeated token
    # At C the larger layer-2 amount arrives via B, which then cannot continue C -> B.
    via_b = _walk(pools, ["ab", "bc1"], "A", amount, fee)[1]
    via_d = _walk(pools, ["ad", "dc"], "A", amount, fee)[1]
    assert (via_b, via_d) == (r["layer2_amount_at_C"]["via_B"], r["layer2_amount_at_C"]["via_D"])
    assert via_b > via_d
    # Discarding the A-D-C prefix leaves at most the 2-hop A-B-T.
    without = [g for p, g in gross.items() if p[:2] != ("ad", "dc")]
    assert max(without) == r["single_label_result"]["gross"]
    walk = r["repeated_token_walk"]
    tokens, walk_gross = _walk(pools, walk["path"], "A", amount, fee)
    assert tokens == walk["tokens"] and walk_gross == walk["gross_if_allowed"] > best["gross"]
    case = Case("r1", "A", "T", amount)
    ok = _evaluate(pools, fee, case, [(best["path"], None)])
    assert ok.status is EvalStatus.OK and ok.gross_output == best["gross"]
    bad = _evaluate(pools, fee, case, [(walk["path"], None)])
    assert bad.status is EvalStatus.INVALID_PLAN and "cycle" in (bad.error or "")


def test_r2_integer_marginals_and_zero_output_legs_are_infeasible() -> None:
    r = RECON["R2_integer_marginals"]
    r_in, r_out = r["reserves"]
    outs = [_q(x, (r_in, r_out), r["fee_bps"]) for x in range(7)]
    assert outs == r["outputs_x0_to_x6"]
    marginals = [b - a for a, b in zip(outs, outs[1:], strict=False)]
    assert marginals[0] < marginals[1]  # not discretely concave
    pools = {"p": ["A", "B", r_in, r_out]}
    ev = _evaluate(pools, r["fee_bps"], Case("r2", "A", "B", 1), [(["p"], None)])
    assert ev.status is EvalStatus.INVALID_PLAN and "insufficient_output_amount" in (ev.error or "")


def test_r3_integer_plateau_stops_a_strict_local_search() -> None:
    r = RECON["R3_plateau"]
    a = r["amount_in"]

    def g(x: int) -> int:
        return _q(x, r["pool1"], r["fee_bps"]) + _q(a - x, r["pool2"], r["fee_bps"])

    assert {str(x): g(x) for x in (0, 1, 2, 3, 15)} == r["G"]
    assert max(g(x) for x in range(a + 1)) == r["raw_integer_max"]
    assert not (g(0) > g(1) or g(2) > g(1))  # +-1 strict improvement stops at x = 1


def test_r4_shared_pool_static_sequential_and_merged_differ() -> None:
    r = RECON["R4_shared_pool"]
    r_in, r_out = r["reserves"]
    d, fee = r["chunk"], r["fee_bps"]
    assert 2 * _q(d, (r_in, r_out), fee) == r["static_sum"]
    first = _q(d, (r_in, r_out), fee)
    assert first + _q(d, (r_in + d, r_out - first), fee) == r["sequential_two_steps"]
    assert _q(2 * d, (r_in, r_out), fee) == r["merged_single_step"]
    pools = {"p": ["A", "B", r_in, r_out]}
    case = Case("r4", "A", "B", 2 * d)
    seq = _evaluate(pools, fee, case, [(["p"], d), (["p"], None)])
    merged = _evaluate(pools, fee, case, [(["p"], None)])
    assert seq.gross_output == r["sequential_two_steps"]
    assert merged.gross_output == r["merged_single_step"]


def test_r5_multihop_rounding_is_not_one_unit_per_hop() -> None:
    r = RECON["R5_multihop_rounding"]
    fee = r["fee_bps"]
    mid = _q(r["amount_in"], r["hop1"], fee)
    assert _q(mid, r["hop2"], fee) == r["stepwise_integer"]
    cont = _f(_f(Fraction(r["amount_in"]), r["hop1"], fee), r["hop2"], fee)
    lo, hi = (Fraction(v) for v in r["continuous_bounds"])
    assert lo < cont < hi and cont - r["stepwise_integer"] > 2  # > one unit per hop


def _grid_values(a: int, pools: dict[str, list[int]], order: list[str], fee: int) -> dict[int, int]:
    """Repository grid (direct_split docstring): the first pool in admitted order gets
    floor(a*u/20), the last takes the rest; a positive leg with zero output is infeasible."""
    first, last = order
    values: dict[int, int] = {}
    for u in range(21):
        x = a * u // 20
        legs = [(first, x), (last, a - x)]
        outs = [_q(amt, pools[p], fee) for p, amt in legs]
        if all(o > 0 for (_, amt), o in zip(legs, outs, strict=True) if amt > 0):
            values[x if first == "p1" else a - x] = sum(outs)
    return values


def _raw_values(a: int, pools: dict[str, list[int]], fee: int) -> dict[int, int]:
    values: dict[int, int] = {}
    for x in range(a + 1):
        o1, o2 = _q(x, pools["p1"], fee), _q(a - x, pools["p2"], fee)
        if (x == 0 or o1 > 0) and (a - x == 0 or o2 > 0):
            values[x] = o1 + o2
    return values


R6 = RECON["R6_grid_order_and_bounds"]
R6_POOLS = {"p1": R6["pool1"], "p2": R6["pool2"]}


def test_r6_equal_parameters_with_another_pool_order_are_another_domain() -> None:
    a, fee = R6["amount_in"], R6["fee_bps"]
    assert (
        max(_grid_values(a, R6_POOLS, ["p1", "p2"], fee).values()) == R6["grid_optimum_order_p1_p2"]
    )
    assert (
        max(_grid_values(a, R6_POOLS, ["p2", "p1"], fee).values()) == R6["grid_optimum_order_p2_p1"]
    )
    raw = _raw_values(a, R6_POOLS, fee)
    top = max(raw.values())
    assert top == R6["raw_integer_optimum"]
    assert [x for x, v in raw.items() if v == top] == R6["raw_integer_argmax_pool1"]
    # The repository's direct_split realizes exactly these grid domains.
    pools = {pid: ["S", "T", *res] for pid, res in R6_POOLS.items()}
    for order, expected in ((["p1", "p2"], 58), (["p2", "p1"], 59)):
        bundle = _bundle(pools, fee, order)
        params = {"max_splits": R6["max_splits"], "percent_step": R6["percent_step"]}
        prepared = direct_split.prepare(bundle, AlgorithmConfig(direct_split.NAME, params))
        result = direct_split.solve(
            Case("r6", "S", "T", a), SolveContext(bundle, gross_only(), prepared), Budget()
        )
        assert result.status is SolveStatus.OK and result.score == expected
    # A different request (T -> S, 135) on the same pools and the same grid domain reaches
    # the same score 58: a matching score cannot transfer a proof (N-REQUEST-TRANSFER).
    rev = R6["reverse_request"]
    flipped = {pid: [res[1], res[0]] for pid, res in R6_POOLS.items()}
    rev_values = _grid_values(rev["amount_in"], flipped, ["p1", "p2"], fee)
    assert max(rev_values.values()) == rev["grid_optimum_order_p1_p2"]
    bundle = _bundle(pools, fee, ["p1", "p2"])
    params = {"max_splits": R6["max_splits"], "percent_step": R6["percent_step"]}
    prepared = direct_split.prepare(bundle, AlgorithmConfig(direct_split.NAME, params))
    case = Case("rev", rev["token_in"], rev["token_out"], rev["amount_in"])
    result = direct_split.solve(case, SolveContext(bundle, gross_only(), prepared), Budget())
    assert result.score == rev["grid_optimum_order_p1_p2"]
    transfer = next(n for n in EXAMPLES["negatives"] if n["id"] == "N-REQUEST-TRANSFER")
    assert transfer["context_patch"]["request"]["amount_in"] == str(rev["amount_in"])
    assert POSITIVES[transfer["base"]]["context"]["final_score"] == str(max(rev_values.values()))


def test_r6_exact_rational_tangent_bound_dominates_every_raw_allocation() -> None:
    a, fee = R6["amount_in"], R6["fee_bps"]
    p1, p2 = R6_POOLS["p1"], R6_POOLS["p2"]

    def big_f(x: Fraction) -> Fraction:
        return _f(x, p1, fee) + _f(a - x, p2, fee)

    z = Fraction(R6["tangent_point"])
    slope = _df(z, p1, fee) - _df(a - z, p2, fee)
    end = Fraction(a) if slope >= 0 else Fraction(0)
    upper = big_f(z) + slope * (end - z)  # concave F lies below every tangent
    assert int(upper) == R6["tangent_upper_floor"]
    raw = _raw_values(a, R6_POOLS, fee)
    assert all(v <= int(upper) for v in raw.values())
    assert _q(a, p2, fee) == R6["single_pool_incumbent"]
    lo, hi = (Fraction(v) for v in R6["continuous_optimum_bounds"])
    grid_x = [Fraction(i, 1000) for i in range(0, a * 1000 + 1, 10)]
    assert lo < max(big_f(x) for x in grid_x) <= upper and max(big_f(x) for x in grid_x) < hi


def test_r6_example_certificates_contain_the_exhaustive_optimum_of_their_domain() -> None:
    a, fee = R6["amount_in"], R6["fee_bps"]
    for example in EXAMPLES["positives"]:
        rec = example["record"]
        if example["kind"] != "diagnostics" or rec["certificate"] is None:
            continue
        kind = rec["domain"]["amount_grid"]["kind"]
        if kind == "repository_grid":
            values = _grid_values(a, R6_POOLS, rec["domain"]["pool_order"], fee)
        elif kind in ("raw_integer", "recovered_continuous"):
            values = _raw_values(a, R6_POOLS, fee)
        else:
            continue  # chunk domains: no bound is claimed (bound_kind unknown)
        cert = rec["certificate"]
        assert (cert["request"]["token_in"], cert["request"]["amount_in"]) == ("S", str(a))
        lower = int(cert["lower_raw"])
        assert lower in values.values()  # the incumbent is a feasible allocation
        if cert["bound_kind"] == "certified":
            assert lower <= max(values.values()) <= int(cert["upper_raw"])
            if cert["optimality_proven"]:
                assert lower == max(values.values())
        else:
            assert cert["upper_raw"] is None
    # The grid certificate does not transfer: the raw domain's optimum exceeds its upper bound.
    grid_upper = int(POSITIVES["P-DSC-GRID"]["record"]["certificate"]["upper_raw"])
    assert max(_raw_values(a, R6_POOLS, fee).values()) > grid_upper


def test_r7_x4_loses_a_simple_path_not_a_cyclic_one() -> None:
    r = RECON["R7_X4"]
    pools, fee, amount = r["pools"], r["fee_bps"], r["amount_in"]
    assert _walk(pools, ["sy", "yx"], "S", amount, fee)[1] == r["via_Y_at_X"]
    assert _walk(pools, ["sa", "ax"], "S", amount, fee)[1] == r["via_A_at_X"]
    tokens, best = _walk(pools, ["sa", "ax", "xy", "yd"], "S", amount, fee)
    assert tokens == r["best_four_hop"]["tokens"] and best == r["best_four_hop"]["gross"]
    assert len(set(tokens)) == len(tokens)  # the lost optimum repeats no token
    tokens2, two = _walk(pools, ["sy", "yd"], "S", amount, fee)
    assert tokens2 == r["returned_two_hop"]["tokens"] and two == r["returned_two_hop"]["gross"]
    assert f"{float(Fraction(10_000 * (best - two), best)):.2f}" == r["published_loss_bps"]
    ev = _evaluate(pools, fee, Case("x4", "S", "D", amount), [(["sa", "ax", "xy", "yd"], None)])
    assert ev.status is EvalStatus.OK and ev.gross_output == best


def test_r8_x4b_loses_a_legal_continuation_and_rejects_only_the_cyclic_union() -> None:
    r = RECON["R8_X4b"]
    pools, fee = r["pools"], r["fee_bps"]
    case = Case("x4b", "S", "D", r["amount_in"])
    half = r["amount_in"] // 2
    accepted = r["accepted_union"]
    ok = _evaluate(pools, fee, case, [(accepted[0], half), (accepted[1], None)])
    assert ok.status is EvalStatus.OK
    rejected = r["rejected_union"]
    bad = _evaluate(pools, fee, case, [(rejected[0], half), (rejected[1], None)])
    assert bad.status is EvalStatus.INVALID_PLAN and "cycle" in (bad.error or "")


def test_r9_d1_a_p95_is_not_an_upper_bound() -> None:
    r = RECON["R9_D1_p95"]
    path = ROOT / r["source"]
    with path.open(encoding="utf-8", newline="") as fh:
        rows = [row for row in csv.DictReader(fh) if all(row[k] == v for k, v in r["row"].items())]
    assert len(rows) == 1
    row = rows[0]
    assert (row["p95"], row["mean"]) == (r["p95"], r["mean"])
    # max >= mean > p95: some paired case exceeds the p95 by far.
    assert float(row["mean"]) > float(row["p95"])


def test_r10_d2_every_l08_decision_comparison_is_inconclusive() -> None:
    r = RECON["R10_D2_L08"]
    final = json.loads((ROOT / r["source"]).read_text(encoding="utf-8"))
    decision = [c for c in final["comparisons"] if c["role"] == "decision"]
    info = [c for c in final["comparisons"] if c["role"] != "decision"]
    assert (
        len(decision) == r["decision_comparisons"] and len(info) == r["informational_comparisons"]
    )
    assert sorted({c["verdict"] for c in decision}) == r["decision_verdicts"]
    clean = [c for c in info if c["verdict"] != "inconclusive"]
    expected = r["clean_informational"]
    assert [(c["id"], c["baseline"], c["candidate"], c["verdict"]) for c in clean] == [
        (expected["id"], expected["baseline"], expected["candidate"], expected["verdict"])
    ]

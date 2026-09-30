"""Test-only research-diagnostics factories for WHI-1548 Stage B (R021-C/1 §9.4-§9.6).

They are NOT registered strategies and NOT contract identities: tests insert them into
`registry.ALGORITHMS` and their §3.3 row into `benchmark.diagnostics.IDENTITIES` with
`monkeypatch` (`register`), so the ordinary roster stays nine and no future ID is
pre-registered. They live in an importable module because spawned workers unpickle a
factory's functions by module + qualified name.

Every factory accepts `algorithm_options` (an optional `tag` choice, so a declared value
changes the run's effective-settings hash) and therefore counts as an options-accepting
identity for the runner's `unavailable` views. Each solves like `direct` and then attaches
one `search_stats["r021"]` object according to its mode:

- `certified`: a TRUE certificate. `direct` evaluates every admitted single pool with the
  whole input, so over the `single_leg` domain of those pools the best evaluated score is an
  exhaustive upper bound equal to the incumbent (gap 0). Its source is the run identity the
  runner handed over (`SolveContext.run_identity`) and its request is the solved case.
- `estimate`, `unknown`, `not_produced`: the other honest outcomes (the "estimate" value is a
  labeled fixture number, not an optimizer result).
- `no_diag`: returns no `r021` at all; `hang` / `crash`: cut off / dies after publishing a
  candidate; `unsupported`: an `unsupported` status with a declared scope.
- tampered certificates whose other fields and score are valid: `wrong_request` (the proof
  of another request carrying THIS request's evaluated score), `wrong_revision`,
  `stale_settings`, `lying_score`, `estimate_gap`, `ledger`;
- malformed objects: `malformed` (wrong types everywhere), `garbage` (not an object and not
  JSON-serializable), `huge_int` (a 5001-digit counter: `repr()` and JSON both refuse it),
  `odd_mapping` (a non-dict Mapping whose default repr is a memory address);
- `repair_log`: an honest `unknown` record carrying the WHI-1553 §8 `repair` object (counters,
  `stop`, `enabled` and `accepted_log`).

Every domain declares the three registered protocols (the direct-like search may route
through any of them); its universe is exactly the case's admitted direct pools.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import pytest

from pools.quote import _ACTIVE_METER
from routing.algorithms import direct
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
    option_choice,
    require_option_keys,
    settings_sha256,
    validated_options,
)
from snapshot.models import Case, SnapshotBundle

PREFIX = "r021_fx_"
TAGS = ("a", "b")
MODES = (
    "certified",
    "estimate",
    "unknown",
    "not_produced",
    "no_diag",
    "hang",
    "crash",
    "unsupported",
    "wrong_request",
    "wrong_revision",
    "stale_settings",
    "lying_score",
    "estimate_gap",
    "ledger",
    "malformed",
    "garbage",
    "huge_int",
    "odd_mapping",
    "repair_log",
)


def validate(options: Mapping[str, Any]) -> dict[str, Any]:
    require_option_keys(options, set(), {"tag"})
    return {"tag": option_choice(options["tag"], "tag", TAGS)} if "tag" in options else {}


def _log_attempt(name: str) -> None:
    path = os.environ.get("FAKE_SOLVE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}\n")


def _domain(bundle: SnapshotBundle, case: Case) -> dict[str, Any]:
    pools = bundle.pools_for_pair(case.token_in, case.token_out)
    ids = [p.pool_id for p in pools]
    return {
        "schema": "r021.domain/1",
        "universe": {"bundle": bundle.bundle_hash, "cohort": "fixture", "pools": ids},
        "protocols": ["constant_product", "concentrated", "liquidity_book"],
        "pool_order": ids,
        "hops": {"max": 1, "param": None},
        "splits": {"max": 1, "param": None, "governs": "none"},
        "amount_grid": {"kind": "single_leg"},
        "zero_output_leg": "infeasible",
        "token_reuse": "simple_path",
        "pool_reuse": "single_pool",
        "dag_admission": "plan_token_dag",
        "full_fill": "v1_full_fill",
    }


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def _certificate(
    context: SolveContext, case: Case, digest: str, score: int, **fields: Any
) -> dict[str, Any]:
    cert: dict[str, Any] = {
        "schema": "r021.certificate/1",
        "candidate_domain_hash": digest,
        "objective": context.objective.mode,
        "source": dict(context.run_identity),
        "request": {
            "case_id": case.case_id,
            "token_in": case.token_in,
            "token_out": case.token_out,
            "amount_in": str(case.amount_in),
        },
        "lower_raw": str(score),
        "upper_raw": str(score),
        "gap_raw": "0",
        "bound_kind": "certified",
        "upper_source": "exhaustive",
        "estimate": None,
        "optimality_proven": True,
        "termination": "complete",
    }
    cert.update(fields)
    return cert


def _diagnostics(
    mode: str, name: str, case: Case, context: SolveContext, result: SolveResult
) -> Any:
    started = time.perf_counter()
    meter = _ACTIVE_METER.get()
    executed = meter.counted if meter is not None else 0
    domain = _domain(context.bundle, case)
    digest = _hash(domain)
    rec: dict[str, Any] = {
        "schema": "r021.diagnostics/1",
        "contract": "R021-C/1",
        "algorithm": name,
        "domain": domain,
        "candidate_domain_hash": digest,
        "certificate": None,
        "certificate_unavailable_reason": None,
        "max_candidates_unit": "pools_evaluated",
        "work": {"quotes_executed": executed, "internal_evaluations": 0},
    }
    score = result.score
    if mode == "unsupported" or score is None:
        rec.update(
            certificate_unavailable_reason="not_produced",
            scope={"supported": mode != "unsupported", "reason": "fixture: declared scope"},
        )
        return rec
    exhaustive = result.status is SolveStatus.OK and result.candidates_truncated == 0
    if mode == "certified" and exhaustive:
        rec["certificate"] = _certificate(context, case, digest, score)
    elif mode == "certified":
        rec["certificate"] = _certificate(
            context, case, digest, score, **_uncertified("unknown", "candidate_cap")
        )
    elif mode == "estimate":
        rec["certificate"] = _certificate(
            context,
            case,
            digest,
            score,
            **_uncertified("estimate", "converged"),
            estimate={"value": f"{score}.25", "residual": "1E-12", "tolerance": "0.00001"},
        )
        rec["work"].update(market_oracle_calls=1, exact_replay_quotes=0)
    elif mode == "unknown":
        rec["certificate"] = _certificate(
            context, case, digest, score, **_uncertified("unknown", "complete")
        )
        rec.update(
            fallback={"used": True, "source": "direct", "reason": "fixture fallback"},
            repair={"attempts": 0, "enabled": False},
            stages={"incumbent": time.perf_counter() - started},  # varies between attempts
        )
    elif mode == "not_produced":
        rec["certificate_unavailable_reason"] = "not_produced"
    elif mode == "wrong_request":
        # The proof of the reverse request, carrying THIS request's evaluated score.
        cert = _certificate(context, case, digest, score)
        cert["request"] = {
            "case_id": case.case_id + "-reverse",
            "token_in": case.token_out,
            "token_out": case.token_in,
            "amount_in": str(case.amount_in),
        }
        rec["certificate"] = cert
    elif mode == "wrong_revision":
        cert = _certificate(context, case, digest, score)
        cert["source"] = {**cert["source"], "git_revision": "0" * 40}
        rec["certificate"] = cert
    elif mode == "stale_settings":
        cert = _certificate(context, case, digest, score)
        cert["source"] = {
            **cert["source"],
            "effective_settings_sha256": settings_sha256({"tag": "a"}),
        }
        rec["certificate"] = cert
    elif mode == "lying_score":
        rec["certificate"] = _certificate(
            context, case, digest, score + 1, upper_raw=str(score + 1)
        )
    elif mode == "estimate_gap":
        rec["certificate"] = _certificate(
            context,
            case,
            digest,
            score,
            bound_kind="estimate",
            upper_source=None,
            upper_raw=str(score + 5),
            gap_raw="5",
            optimality_proven=False,
            termination="converged",
            estimate={"value": f"{score}.5", "residual": "0", "tolerance": "0"},
        )
    elif mode == "ledger":
        rec["certificate"] = _certificate(context, case, digest, score)
        rec["work"]["quotes_executed"] = executed + 7
    elif mode == "malformed":
        rec["certificate"] = _certificate(
            context, case, digest, score, lower_raw=score, optimality_proven="yes"
        )
        rec["work"] = {"quotes_executed": True, "paths": -1}
        rec["domain"] = "single_leg"
    elif mode == "garbage":
        return {"not", "a", "mapping"}
    elif mode == "huge_int":
        rec["certificate"] = _certificate(context, case, digest, score)
        rec["work"]["paths_scored"] = 10**5000
    elif mode == "odd_mapping":
        return OddMapping(rec)
    elif mode == "repair_log":
        rec["certificate"] = _certificate(
            context, case, digest, score, **_uncertified("unknown", "complete")
        )
        rec["repair"] = {  # suffix-repair.md §8 (WHI-1553, 3d0afbd): without path counters
            "enabled": True,
            "stop": "exhausted",
            "checkpoint_restores": 2,
            "repair_attempts": 3,
            "candidates_complete": 3,
            "candidates_failed": 0,
            "duplicates": 0,
            "rejected_worse": 1,
            "ties": 0,
            "accepted": 2,
            "consistency_failures": 0,
            "internal_evaluations": 3,
            "accepted_log": [
                {"checkpoint": 0, "alternative": 0, "score": str(score - 1)},
                {"checkpoint": 1, "alternative": 1, "score": str(score)},
            ],
        }
    return rec


class OddMapping(Mapping[str, Any]):
    """A picklable non-dict Mapping: JSON refuses it and its repr is a memory address."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Any:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def _uncertified(kind: str, termination: str) -> dict[str, Any]:
    return {
        "bound_kind": kind,
        "upper_raw": None,
        "gap_raw": None,
        "upper_source": None,
        "optimality_proven": False,
        "termination": termination,
    }


def prepare(bundle: SnapshotBundle, config: AlgorithmConfig) -> Any:
    return MappingProxyType(validated_options(FACTORIES[config.name], config.options))


def solve(mode: str, case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    name = PREFIX + mode
    _log_attempt(name)
    if mode == "crash":
        os._exit(23)
    plain = dataclasses.replace(context, prepared=None)
    if mode == "hang":
        direct.solve(case, plain, budget)  # publishes a valid candidate, then never returns
        while True:
            time.sleep(0.05)
    result = dataclasses.replace(direct.solve(case, plain, budget), algorithm=name)
    if mode == "unsupported":
        result = SolveResult(
            case_id=case.case_id,
            algorithm=name,
            status=SolveStatus.UNSUPPORTED,
            error="fixture: objective outside this identity's declared scope",
        )
    if mode == "no_diag":
        return result
    stats = {**result.search_stats, "r021": _diagnostics(mode, name, case, context, result)}
    return dataclasses.replace(result, search_stats=stats)


def _mode_solver(mode: str) -> Any:
    import functools

    return functools.partial(solve, mode)


FACTORIES: dict[str, AlgorithmFactory] = {
    PREFIX + mode: AlgorithmFactory(
        name=PREFIX + mode,
        solve=_mode_solver(mode),
        prepare=prepare,
        options_validator=validate,
    )
    for mode in MODES
}


def register(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test-scoped registration: the factories and their §3.3 row (the `direct` unit)."""
    import benchmark.diagnostics as diagnostics
    import routing.algorithms.registry as registry

    for name, factory in FACTORIES.items():
        monkeypatch.setitem(registry.ALGORITHMS, name, factory)
        monkeypatch.setitem(diagnostics.IDENTITIES, name, diagnostics.Identity("pools_evaluated"))

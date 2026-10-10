"""WHI-1559 component B: the CL stage of the registered `cfmm_dual` factory
(`market_protocols: constant_product+concentrated`, current preset `cfmm_dual/2`), the
preserved CPMM-v1 source/recipe identity (historical preset pin) and the real CLI paths
(`cfmm-dual.md` §8, §9.3, §11.2 G-L1..G-L7 at strategy level; the model/index gates are
component A's `test_cfmm_cl.py`).

Every behavioural check runs the REAL registered factory (`get_algorithm(NAME)`: its public
`prepare` and `solve`), the real recovery module or the real CLI/runner. Expected values
never come from the implementation under test: they are literal pins (file sha256, preset
values, WHI-1558 source identity), the exact protocol quote `pools.quote.quote_exact_in` on
the pool's own snapshot state (the money authority), a fresh memo-free evaluator replay,
wrappers that only count, and the runtime validator (`benchmark.diagnostics`) against an
independently built run context. Model-vs-exact numbers are never compared as a certified
bound here; exact replay equality is.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shlex
import sys
import tracemalloc
from collections.abc import Callable, Iterator
from functools import cache
from pathlib import Path
from typing import Any

import cfmm_contract_model as spec  # the WHI-1557 executable contract (synthetic CL fixture)
import pytest
import yaml
from test_cfmm_dual import REVISION, _context, _invariants
from test_cfmm_model import cp

import benchmark.profile as profile_module
import routing.cfmm.cl as cl
import routing.cfmm.model as cm
import routing.cfmm.optimizer as co
from benchmark.diagnostics import check_diagnostics
from benchmark.objective import gross_only, synthetic_fixed_cost
from benchmark.profile import (
    ProfileError,
    load_profile,
    options_entry,
    parse_profile,
    preset_options,
)
from benchmark.strategies import derive
from pools.cl_math import get_sqrt_ratio_at_tick
from pools.quote import metered_quotes, quote_exact_in
from pools.result import QuoteStatus
from routing.algorithms import cfmm_dual as cd
from routing.algorithms.base import (
    AlgorithmConfig,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
    settings_sha256,
)
from routing.algorithms.registry import ALGORITHMS, get_algorithm
from routing.cfmm.model import Trade
from routing.cfmm.recovery import RecoveryOptions, recover
from routing.evaluator import evaluate as reference_evaluate
from routing.plan import RoutePlan
from routing.search import QuoteCache
from snapshot.bundle import load_bundle
from snapshot.models import (
    BlockRef,
    Case,
    ConcentratedPoolState,
    LiquidityBookPoolState,
    PoolState,
    SnapshotBundle,
)

REPO = Path(__file__).resolve().parents[2]
NAME = "cfmm_dual"
FACTORY = get_algorithm(NAME)
FIX = REPO / "tests" / "fixtures"
MIXED = FIX / "routing" / "mantle_mixed"
CORPUS = FIX / "corpus" / "bundle"
LB = FIX / "moe_lb" / "bundle"
SOURCES = {
    "uniswap_v3": FIX / "uniswap_v3" / "bundle",
    "agni_v3": FIX / "agni" / "bundle",
    "fusionx_v3": FIX / "fusionx" / "bundle",
}
PROFILES = REPO / "config" / NAME
BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)
LM = "0x" + "11" * 20
CL_STAGE = ["constant_product", "concentrated"]

# Literal pins (independent of the implementation): the frozen files' bytes and identities.
V1_PIN: dict[str, Any] = {
    "path": "config/cfmm_dual/preset_v1.yaml",
    "sha256": "1526133cd3bf61a5493ee68f2704875ecf225637c1c7858aa30d3be464dbf605",
    "key": "R021-P12-cfmm_dual",
    "version": 1,
}
V2_PIN: dict[str, Any] = {
    "path": "config/cfmm_dual/preset_v2.yaml",
    "sha256": "865ad5929c0c3ce6548e6ff5b2ef5d99703c72fb1cdc4a0cbae2d61f436142c2",
    "key": "R021-P12-cfmm_dual",
    "version": 2,
}
CPMM_PROFILE_SHA256 = "674eeb79d97b79c3c1e9adf9c6cea8e0fb601e6379c6bb75441af9f7ec9853dd"  # WHI-1558
V1_SETTINGS_SHA256 = "63b62554234ac5ac3f03639bc4069fca8a3e39e053a026b88a6f5d9f48d3e3ac"
V1 = {
    "market_protocols": "constant_product",
    "max_iterations": 200,
    "max_function_evaluations": 600,
    "lbfgs_memory": 10,
    "pgtol": 1e-9,
    "ftol": 1e-15,
    "residual_tolerance": 1e-5,
    "log_price_bound": 50.0,
    "min_split_share": 1e-6,
    "max_recovery_attempts": 8,
    "cycle_resolve": True,
    "fallback": "single_path",
}
V2 = {
    **V1,
    "market_protocols": "constant_product+concentrated",
    "lbfgs_memory": 30,
    "log_price_bound": 10.0,
    "min_split_share": 1e-3,
}


# ------------------------------------------------------------------ helpers


@cache
def _bundle(path: Path) -> SnapshotBundle:
    return load_bundle(path)


def _prepare(bundle: SnapshotBundle, options: dict[str, Any] | None = None) -> cd.PreparedCfmm:
    assert FACTORY.prepare is not None
    prepared = FACTORY.prepare(bundle, AlgorithmConfig(NAME, {"max_hops": 3}, options or V2))
    assert isinstance(prepared, cd.PreparedCfmm)
    return prepared


def _solve(
    bundle: SnapshotBundle,
    case: Case,
    prepared: cd.PreparedCfmm | None = None,
    budget: Budget | None = None,
    *,
    objective: Any = None,
    sink: list[RoutePlan] | None = None,
    **over: Any,
) -> SolveResult:
    prepared = prepared or _prepare(bundle, {**V2, **over})
    context = SolveContext(
        bundle,
        objective or gross_only(),
        prepared,
        candidate_sink=None if sink is None else sink.append,
        run_identity={"git_revision": REVISION},
    )
    return FACTORY.solve(case, context, budget or Budget())


def _cfmm(result: SolveResult) -> dict[str, Any]:
    stats: dict[str, Any] = result.search_stats["cfmm"]
    return stats


def _r021(result: SolveResult) -> dict[str, Any]:
    rec: dict[str, Any] = result.search_stats["r021"]
    return rec


def _single(
    *pools: PoolState, token_in: str, token_out: str, amount: int
) -> tuple[SnapshotBundle, Case]:
    case = Case("x", token_in, token_out, amount)
    bundle = SnapshotBundle(
        "x", "synthetic", 1, BLOCK, {p.pool_id: p for p in pools}, (case,), "h", "<t>"
    )
    return bundle, case


def _real_states() -> Iterator[tuple[str, ConcentratedPoolState]]:
    for source, path in SOURCES.items():
        for pid, pool in _bundle(path).pools.items():
            if isinstance(pool, ConcentratedPoolState):
                yield f"{source}:{pid[:10]}", pool


def _source_state(source: str) -> ConcentratedPoolState:
    """The synthetic two-position fixture under each admitted source's own semantics: a
    non-zero protocol fee in the source's encoding and, for Agni/FusionX, a live LM hook."""
    fee_protocol = 4 | (5 << 4) if source == "uniswap_v3" else 3300 | (3300 << 16)
    return dataclasses.replace(
        spec.synthetic_cl(),
        pool_id=f"synth_{source}",
        source_key=source,
        fee_protocol=fee_protocol,
        lm_pool=None if source == "uniswap_v3" else LM,
    )


def _cl_steps(bundle: SnapshotBundle, plan: RoutePlan | None) -> list[str]:
    return [
        s.pool_id
        for s in (plan.steps if plan else ())
        if isinstance(bundle.pools[s.pool_id], ConcentratedPoolState)
    ]


def _exact_landing(state: ConcentratedPoolState, token: str) -> int:
    """The smallest exact input whose exact swap is not `ok` (bisection on the exact quote
    only): the input that brings the price exactly onto the first tick it cannot cross."""
    lo, hi = 1, 1
    while quote_exact_in(state, token, hi).status is QuoteStatus.OK:
        hi *= 2
    while lo < hi:
        mid = (lo + hi) // 2
        if quote_exact_in(state, token, mid).status is QuoteStatus.OK:
            lo = mid + 1
        else:
            hi = mid
    return lo


# ------------------------------------------------------------------ presets / versioning


def test_current_preset_is_cfmm_dual_2_and_v1_stays_a_byte_identical_historical_pin() -> None:
    for pin in (V1_PIN, V2_PIN):
        assert hashlib.sha256((REPO / pin["path"]).read_bytes()).hexdigest() == pin["sha256"]
    cpmm = (PROFILES / "cpmm.yaml").read_bytes()
    assert hashlib.sha256(cpmm).hexdigest() == CPMM_PROFILE_SHA256  # WHI-1558 bytes
    assert dict(FACTORY.options_preset or {}) == V2_PIN
    assert [dict(p) for p in FACTORY.historical_presets] == [V1_PIN]
    assert preset_options(FACTORY) == V2
    assert preset_options(FACTORY, FACTORY.historical_presets[0]) == V1
    assert settings_sha256(V1) == V1_SETTINGS_SHA256
    # the saved CPMM-only ablation keeps its WHI-1558 source identity (not an override)
    old = load_profile(PROFILES / "cpmm.yaml").algorithm_options[NAME]
    assert old == {"options": V1, "source": {"kind": "preset", **V1_PIN},
                   "settings_sha256": V1_SETTINGS_SHA256}  # fmt: skip
    new = load_profile(PROFILES / "cl.yaml")
    assert new.algorithm_options[NAME] == {
        "options": V2,
        "source": {"kind": "preset", **V2_PIN},
        "settings_sha256": settings_sha256(V2),
    }
    assert list(new.algorithms) == ["path_split", "incremental_graph", NAME]
    assert new.search == {"max_hops": 3, "max_splits": 4, "percent_step": 5}
    # anything else is an override, whatever its market_protocols
    for other in ({**V1, "lbfgs_memory": 30}, {**V2, "min_split_share": 1e-6}):
        assert options_entry(FACTORY, other)["source"] == {"kind": "override"}
    # every default roster: 19 strategies (WHI-1599/1600 append the three bounded identities after
    # the 0.2.1 ones, WHI-1632 the two 0.2.3 post-processors), cfmm_dual the last 0.2.1 identity,
    # with the CURRENT preset (CL mode)
    for name in ("daily_gross.yaml", "full_gross.yaml"):
        source = yaml.safe_load((REPO / "config" / name).read_text())
        document, profile = derive(source, "all", source_path=name, source_sha256="x")
        assert len(profile.algorithms) == 19 and profile.algorithms[-6] == NAME
        assert list(profile.algorithms[-5:]) == [
            "single_path_bounded",
            "incremental_graph_bounded",
            "metis_history_bounded",
            "split_polish",
            "marginal_activation",
        ]
        assert document["algorithm_options"][NAME] == V2
        assert profile.algorithm_options[NAME]["source"] == {"kind": "preset", **V2_PIN}
        assert len(set(profile.algorithms)) == 19  # no duplicate identity


def test_a_saved_whi_1558_effective_profile_replays_with_its_v1_identity() -> None:
    """A 14-strategy `all` document saved under WHI-1558 carried the v1 options: replayed with
    `--strategies profile` it keeps them, its v1 source pin and settings hash, and runs the CPMM
    stage (no CL index is built); deriving `all` from it again changes nothing."""
    source = yaml.safe_load((REPO / "config" / "daily_gross.yaml").read_text())
    document, _ = derive(source, "all", source_path="config/daily_gross.yaml", source_sha256="x")
    saved = json.loads(json.dumps(document))
    saved["algorithm_options"][NAME] = dict(V1)
    literal, profile = derive(saved, "profile", source_path="saved", source_sha256="y")
    assert literal == saved
    entry = profile.algorithm_options[NAME]
    assert entry["source"] == {"kind": "preset", **V1_PIN}
    assert entry["settings_sha256"] == V1_SETTINGS_SHA256
    again, reprofile = derive(saved, "all", source_path="saved", source_sha256="y")
    assert again["algorithm_options"][NAME] == V1 and reprofile.algorithms == profile.algorithms
    prepared = _prepare(_bundle(MIXED), dict(profile.algorithm_config(FACTORY).options))
    assert prepared.protocols == ("constant_product",) and dict(prepared.cl_indexes) == {}
    assert prepared.cl_prepare is None


def test_every_pin_is_verified_and_tamper_metadata_and_ambiguity_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config" / NAME).mkdir(parents=True)
    for pin in (V1_PIN, V2_PIN):
        (tmp_path / pin["path"]).write_bytes((REPO / pin["path"]).read_bytes())
    monkeypatch.setattr(profile_module, "REPO_ROOT", tmp_path)
    assert options_entry(FACTORY, V1)["source"]["version"] == 1  # the copies verify
    for pin in (V1_PIN, V2_PIN):  # a changed historical OR current file refuses every entry
        original = (REPO / pin["path"]).read_text()
        (tmp_path / pin["path"]).write_text(
            original.replace("max_iterations: 200", "max_iterations: 201")
        )
        for options in (V1, V2):
            with pytest.raises(ProfileError, match="differs from the pin"):
                options_entry(FACTORY, options)
        (tmp_path / pin["path"]).write_text(original)
    (tmp_path / V1_PIN["path"]).unlink()
    with pytest.raises(ProfileError, match="unreadable"):
        options_entry(FACTORY, V2)
    monkeypatch.undo()
    # pin metadata must match the file exactly: key, version, algorithm (no external trust)
    for wrong in ({"version": 3}, {"key": "R021-P12-other"}):
        fake = dataclasses.replace(FACTORY, historical_presets=({**V1_PIN, **wrong},))
        with pytest.raises(ProfileError, match="is not"):
            options_entry(fake, V1)
    other = dataclasses.replace(FACTORY, name="not_cfmm_dual")
    with pytest.raises(ProfileError, match="is not"):
        options_entry(other, V2)
    # the same options under two registered pins are ambiguous, never resolved by order
    for pins in ((V1_PIN, V1_PIN), (V2_PIN,)):
        fake = dataclasses.replace(FACTORY, historical_presets=pins)
        options = V1 if pins[0] is V1_PIN else V2
        with pytest.raises(ProfileError, match="ambiguous"):
            options_entry(fake, options)
    # a document can never claim a preset identity for its settings
    doc = yaml.safe_load((PROFILES / "cl.yaml").read_text())
    doc["algorithm_options"][NAME]["source"] = {"kind": "preset", **V1_PIN}
    with pytest.raises(ProfileError, match="unknown key"):
        parse_profile(doc, "<test>")


def test_the_historical_seam_leaves_every_other_factory_unchanged() -> None:
    assert [n for n, f in ALGORITHMS.items() if f.historical_presets] == [NAME]
    assert len(ALGORITHMS) == 20 and list(ALGORITHMS).count(NAME) == 1  # + 3 bounded, R023 (2)
    for name, factory in ALGORITHMS.items():
        if factory.options_validator is None or name == NAME:
            continue
        assert factory.options_preset is not None  # WHI-1632: the 0.2.3 ones too
        if name in ("split_polish", "marginal_activation"):  # recognised only in an `all` doc
            assert options_entry(factory, preset_options(factory))["source"] == {"kind": "override"}
            entry = options_entry(factory, preset_options(factory), "all")
            assert entry["source"] == {"kind": "preset", **dict(factory.options_preset)}
            continue
        entry = options_entry(factory, preset_options(factory))
        assert entry["source"] == {"kind": "preset", **dict(factory.options_preset or {})}


# ------------------------------------------------------------------ charged CL preparation


def test_cl_prepare_builds_every_index_once_and_no_solve_rebuilds_or_leaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _bundle(CORPUS)
    admitted = [
        pid
        for pid, p in corpus.pools.items()
        if isinstance(p, ConcentratedPoolState) and cl.cl_admitted(p)
    ]
    built: list[str] = []
    real_build = cl.build_cl_index

    def counting(state: ConcentratedPoolState) -> cl.ClIndex:
        built.append(state.pool_id)
        return real_build(state)

    monkeypatch.setattr(cl, "build_cl_index", counting)
    cpmm = _prepare(corpus, V1)
    assert built == [] and dict(cpmm.cl_indexes) == {} and cpmm.protocols == ("constant_product",)
    prepared = _prepare(corpus)
    assert built == admitted  # once each, bundle order, inside prepare
    assert prepared.protocols == ("constant_product", "concentrated")
    assert list(prepared.cl_indexes) == admitted
    assert all(prepared.cl_indexes[p].state is corpus.pools[p] for p in admitted)
    stats = [prepared.cl_indexes[p].stats() for p in admitted]
    assert prepared.cl_prepare is not None and prepared.cl_prepare["pools"] == len(admitted)
    for key in ("bitmap_words", "initialized_ticks", "segments_down", "segments_up"):
        assert prepared.cl_prepare[key] == sum(s[key] for s in stats)
    assert prepared.cl_prepare["float_entries"] == sum(
        4 * (s["segments_down"] + s["segments_up"]) + 6 for s in stats
    )  # 4*S+3 per side
    with pytest.raises(TypeError):
        prepared.cl_indexes["x"] = prepared.cl_indexes[admitted[0]]  # type: ignore[index]

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a CL index was built outside prepare")

    monkeypatch.setattr(cl, "build_cl_index", refuse)
    monkeypatch.setattr(cl, "prepare_cl_indexes", refuse)
    monkeypatch.setattr(cd, "prepare_cl_indexes", refuse)
    seen: set[int] = set()
    real_arb = cm.cl_arb

    def arb(index: cl.ClIndex, *a: Any, **k: Any) -> Any:
        seen.add(id(index))
        return real_arb(index, *a, **k)

    monkeypatch.setattr(cm, "cl_arb", arb)
    before = {p: dataclasses.replace(corpus.pools[p]) for p in admitted}
    resolved = 0
    for case in (*corpus.cases, *reversed(corpus.cases)):
        result = _solve(corpus, case, prepared)
        resolved += _cfmm(result)["resolve"] == "resolved"
        assert set(_cfmm(result)["cl_markets"]) <= set(admitted)
        assert "prepare_cl_indexes" in _r021(result)["stages"]
    assert seen <= {id(i) for i in prepared.cl_indexes.values()} and seen
    assert {p: corpus.pools[p] for p in admitted} == before  # no state mutation
    assert [id(prepared.cl_indexes[p]) for p in admitted] == [
        id(i) for i in prepared.cl_indexes.values()
    ]


def test_index_memory_is_paid_in_prepare_and_disclosed_structurally() -> None:
    """The indexes are allocated in `prepare` (what the worker's prepare_peak measures; that
    no solve rebuilds them is the monkeypatch test above); the CPMM stage allocates none."""
    bundle = _bundle(SOURCES["uniswap_v3"])
    _prepare(bundle, V1)  # the numeric import is paid once, outside the comparison

    def peak(options: dict[str, Any]) -> tuple[int, cd.PreparedCfmm]:
        tracemalloc.start()
        prepared = _prepare(bundle, options)
        size = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        return size, prepared

    cpmm_peak, _ = peak(V1)
    cl_peak, prepared = peak(V2)
    assert prepared.cl_prepare is not None
    assert cl_peak - cpmm_peak >= 8 * prepared.cl_prepare["float_entries"]


# ------------------------------------------------------------------ real states, per source


@pytest.mark.parametrize(
    ("name", "state"), list(_real_states()), ids=lambda v: v if isinstance(v, str) else ""
)
def test_single_market_cases_equal_the_exact_quote_on_every_real_state(
    name: str, state: ConcentratedPoolState
) -> None:
    """All 18 frozen Uniswap v3 / Agni / FusionX states, both directions, tiny to beyond the
    known range: with one market the recovered plan is that pool's whole input, so the result
    must be exactly the exact protocol quote -- ok with its output, or no plan at all."""
    assert cl.cl_admitted(state)
    idx = cl.build_cl_index(state)
    outcomes: set[str] = set()
    for zero_for_one in (True, False):
        token_in, token_out = (
            (state.token0, state.token1) if zero_for_one else (state.token1, state.token0)
        )
        cap = idx.side(zero_for_one).net[-1] / idx.gamma
        for amount in sorted({1, 10**3, 10**9, int(cap * 0.5), int(cap * 0.999), int(cap) * 2 + 1}):
            if amount <= 0:
                continue
            bundle, case = _single(state, token_in=token_in, token_out=token_out, amount=amount)
            result = _solve(bundle, case)
            exact = quote_exact_in(state, token_in, amount)
            stats = _cfmm(result)
            assert _r021(result)["domain"]["protocols"] == CL_STAGE
            if exact.status is QuoteStatus.OK and exact.amount_out > 0:
                assert result.status is SolveStatus.OK, (amount, result.error)
                assert result.score == exact.amount_out
                assert _invariants(bundle, case, result.plan) == exact.amount_out
                outcomes.add("ok")
            else:  # no feasible plan: never a zero-output (dust-donating) or failing leg
                assert result.plan is None and stats["recovery_failure"] in cd.FALLBACK_REASONS
                want = (  # single_path: a candidate needing uncollected state, else none
                    SolveStatus.INCOMPLETE_SNAPSHOT
                    if exact.status is QuoteStatus.INCOMPLETE_SNAPSHOT
                    else SolveStatus.NO_ROUTE  # incl. an exact zero output (infeasible leg)
                )
                assert result.status is want, (amount, result.status, exact.status)
                outcomes.add(result.status.value)
    if state.liquidity == 0 and not state.ticks:  # real zero-liquidity pools (fully known)
        assert outcomes == {"no_route"}
    else:
        assert "ok" in outcomes


@pytest.mark.parametrize("source", ["uniswap_v3", "agni_v3", "fusionx_v3"])
def test_admitted_sources_with_protocol_fee_and_lm_hook_route_both_directions(source: str) -> None:
    """Each admitted source's own exact semantics (protocol-fee encoding, the Agni/FusionX
    LM hook) execute the plan; the continuous model is source-independent."""
    state = _source_state(source)
    other = cp("cp", "T0", "T1", 10**18, 10**18)
    for token_in, token_out in (("T0", "T1"), ("T1", "T0")):
        for amount in (10**3, 10**12, 3 * 10**14):
            bundle, case = _single(
                state, other, token_in=token_in, token_out=token_out, amount=amount
            )
            result = _solve(bundle, case)
            assert result.status is SolveStatus.OK, result.error
            gross = _invariants(bundle, case, result.plan)
            assert gross == result.score
            assert result.plan is not None
            ev = reference_evaluate(bundle, case, result.plan, gross_only())
            for step in ev.trace:  # every leg is the exact quote on the ORIGINAL state
                q = quote_exact_in(bundle.pools[step.pool_id], step.token_in, step.amount_in)
                assert (q.status, q.amount_out) == (QuoteStatus.OK, step.amount_out)
            if (token_in, amount) == ("T0", 3 * 10**14):  # CL is the better price here
                assert _cl_steps(bundle, result.plan) == [state.pool_id]
        alone, case = _single(state, token_in=token_in, token_out=token_out, amount=10**12)
        result = _solve(alone, case)
        exact = quote_exact_in(state, token_in, 10**12)
        assert result.score == exact.amount_out > 0
        assert (exact.features["lm_pool_hook_calls"] > 0) == (source != "uniswap_v3")


def test_mixed_real_networks_validate_replay_exactly_and_keep_lb_out() -> None:
    """mantle_mixed and the five-source corpus fixture: every ok plan passes a fresh replay,
    its certificate validates against an independent run context, LB never enters a
    universe or plan, CL legs are actually routed, and the same cases in the CPMM stage stay
    CPMM-only (the stage comparison)."""
    cl_legs = 0
    for path in (MIXED, CORPUS):
        bundle = _bundle(path)
        prepared, cpmm = _prepare(bundle), _prepare(bundle, V1)
        lb = {p for p, s in bundle.pools.items() if isinstance(s, LiquidityBookPoolState)}
        assert lb
        for case in bundle.cases:
            with metered_quotes(None) as meter:
                result = _solve(bundle, case, prepared)
            rec, stats = _r021(result), _cfmm(result)
            assert rec["domain"]["protocols"] == CL_STAGE and stats["stage"] == cd.CL_STAGE
            assert not set(stats["markets"]) & lb
            assert (
                check_diagnostics(rec, _context(bundle, case, result, meter.counted, V2)) == set()
            )
            assert result.status in (SolveStatus.OK, SolveStatus.NO_ROUTE, SolveStatus.UNSUPPORTED)
            if result.status is SolveStatus.OK:
                assert _invariants(bundle, case, result.plan) == result.score
                assert not {s.pool_id for s in result.plan.steps} & lb  # type: ignore[union-attr]
                cl_legs += len(_cl_steps(bundle, result.plan))
            old = _solve(bundle, case, cpmm)
            assert _r021(old)["domain"]["protocols"] == ["constant_product"]
            assert _cfmm(old)["stage"] == "constant_product" and "cl_markets" not in _cfmm(old)
            assert "prepare_cl_indexes" not in _r021(old)["stages"]
            assert _cl_steps(bundle, old.plan) == []
    assert cl_legs > 0


def test_lb_only_and_unadmitted_cl_cases_are_explicit_unsupported_rows() -> None:
    lb = _bundle(LB)
    prepared = _prepare(lb)
    assert dict(prepared.cl_indexes) == {}
    for case in lb.cases:
        row = _solve(lb, case, prepared)
        assert row.status is SolveStatus.UNSUPPORTED and row.plan is None
        assert _r021(row)["scope"] == {"supported": False, "reason": "protocol_ceiling"}
        assert _r021(row)["domain"]["protocols"] == CL_STAGE
        assert "liquidity_book is excluded" in (row.error or "")
        assert _r021(row)["work"]["objective_evaluations"] == 0
    # CL variants outside the admitted source semantics are not markets either
    base = spec.synthetic_cl()
    for variant in (
        dataclasses.replace(base, source_key="pancake_v3"),
        dataclasses.replace(base, lm_pool=LM),  # uniswap_v3 has no LM hook
        dataclasses.replace(base, fee=10**6),
    ):
        assert not cl.cl_admitted(variant)
        bundle, case = _single(variant, token_in="T0", token_out="T1", amount=10**9)
        row = _solve(bundle, case)
        assert row.status is SolveStatus.UNSUPPORTED
        assert _r021(row)["scope"]["reason"] == "protocol_ceiling" and _cfmm(row)["markets"] == []
    # an LB pool next to an eligible path does not make the case unsupported
    mixed = _bundle(MIXED)
    lb_mixed = next(p for p, s in mixed.pools.items() if isinstance(s, LiquidityBookPoolState))
    for case in mixed.cases:
        row = _solve(mixed, case)
        assert row.status is SolveStatus.OK and lb_mixed not in _cfmm(row)["markets"]
    # a net objective is outside the gross-only ceiling in the CL stage too
    net = _solve(mixed, mixed.cases[0], objective=synthetic_fixed_cost(0))
    assert net.status is SolveStatus.UNSUPPORTED
    assert _r021(net)["scope"] == {"supported": False, "reason": "objective"}


# ------------------------------------------------------------------ boundaries / missing state


def test_empty_range_boundary_and_missing_state_through_the_strategy() -> None:
    state = spec.synthetic_cl()  # [-1800,-1200) L1, empty [-1200,-600), [-600,1200) L2
    idx = cl.build_cl_index(state)
    assert idx.down.liquidity == (1e16, 0.0, 4e15, 0.0)  # empty ranges inside the known range
    across = int(idx.down.net[2] / idx.gamma) + 10**12  # traverses the empty range
    for amount in (1, 10**6, across, 4 * 10**14):
        bundle, case = _single(state, token_in="T0", token_out="T1", amount=amount)
        exact = quote_exact_in(state, "T0", amount)
        result = _solve(bundle, case)
        if exact.status is QuoteStatus.OK and exact.amount_out > 0:
            assert result.score == exact.amount_out
        else:
            assert result.plan is None
    assert quote_exact_in(state, "T0", across).features["initialized_ticks_crossed"] >= 2
    # beyond the collected range: incomplete_snapshot, never extrapolated or zero liquidity
    beyond = int(idx.down.net[-1] / idx.gamma) * 2
    bundle, case = _single(state, token_in="T0", token_out="T1", amount=beyond)
    assert quote_exact_in(state, "T0", beyond).status is QuoteStatus.INCOMPLETE_SNAPSHOT
    result = _solve(bundle, case)
    assert result.plan is None and result.status is SolveStatus.INCOMPLETE_SNAPSHOT
    # a direction whose first read word is uncollected is empty: no CL market trade at all
    top = dataclasses.replace(
        state, tick=15359, sqrt_price_x96=get_sqrt_ratio_at_tick(15359) + 1
    )  # current tick in the last bit of the top word: "up" reads word 1, uncollected
    assert cl.build_cl_index(top).up.boundary == "first_word_uncollected"
    assert quote_exact_in(top, "T1", 10**9).status is QuoteStatus.INCOMPLETE_SNAPSHOT
    bundle, case = _single(top, cp("cp", "T0", "T1", 10**15, 10**15),
                           token_in="T1", token_out="T0", amount=10**9)  # fmt: skip
    result = _solve(bundle, case)
    assert result.status is SolveStatus.OK and _cl_steps(bundle, result.plan) == []


def test_an_exact_endpoint_on_a_missing_tick_info_is_pruned_never_ignored() -> None:
    """The continuous range is closed at a tick without TickInfo; the exact swap that lands
    exactly on it must cross it and is `incomplete_snapshot`. Deterministic: with one market
    the recovered leg is the whole request, so the landing input is hit exactly."""
    missing = spec.synthetic_cl(missing_tick_data=True)
    idx = cl.build_cl_index(missing)
    assert (idx.down.boundary, idx.down.boundary_tick) == ("missing_tick_data", -1800)
    x = _exact_landing(missing, "T0")
    assert quote_exact_in(missing, "T0", x).status is QuoteStatus.INCOMPLETE_SNAPSHOT
    assert "no collected tick data" in (quote_exact_in(missing, "T0", x).detail or "")
    below = quote_exact_in(missing, "T0", x - 1)
    assert below.status is QuoteStatus.OK and below.amount_out > 0
    # just before the tick: the plan is the exact quote
    bundle, case = _single(missing, token_in="T0", token_out="T1", amount=x - 1)
    ok = _solve(bundle, case)
    assert ok.status is SolveStatus.OK and ok.score == below.amount_out
    # exactly on it: the leg is pruned (incomplete_snapshot), the fallback cannot quote it
    bundle, case = _single(missing, token_in="T0", token_out="T1", amount=x)
    sink: list[RoutePlan] = []
    landed = _solve(bundle, case, sink=sink)
    stats = _cfmm(landed)
    assert stats["recovery"]["pruned"] == [
        {"pool_id": missing.pool_id, "reason": "incomplete_snapshot", "attempt": 1}
    ]
    assert stats["recovery_failure"] == "support_exhausted"
    assert landed.plan is None and sink == []
    assert landed.status is SolveStatus.INCOMPLETE_SNAPSHOT
    assert _r021(landed)["fallback"] == {
        "used": True,
        "source": "single_path",
        "reason": "support_exhausted",
    }
    # in a mixed support the landing CL leg is pruned and the CPMM leg takes the request
    other = cp("cp", "T0", "T1", 10**18, 10**18)
    bundle, case = _single(missing, other, token_in="T0", token_out="T1", amount=x + 10**12)
    rec = recover(
        bundle,
        case,
        [missing.pool_id, "cp"],
        [Trade(missing.pool_id, "T0", "T1", float(x), 1.0), Trade("cp", "T0", "T1", 1e12, 1.0)],
        {"T0": 1.0},
        RecoveryOptions(1e-6, 8, True),
        QuoteCache(bundle),
        gross_only(),
        None,
    )
    assert rec.pruned == [(missing.pool_id, "incomplete_snapshot", 1)]
    assert [f.edge.pool_id for f in rec.flows] == ["cp"] and rec.plan is not None
    assert rec.gross == quote_exact_in(other, "T0", x + 10**12).amount_out


# ------------------------------------------------------------------ mixed recovery and budgets


def _cycle_network() -> tuple[SnapshotBundle, Case]:
    """S -> T0 -> T1 -> T with two T0/T1 markets whose prices disagree far beyond the fees:
    the synthetic CL pool (T1 per T0 ~ 1.01) and a CPMM pool at 0.5. The continuous flow
    arbitrages them against each other (a T0 <-> T1 cycle) that recovery must remove."""
    state = spec.synthetic_cl()
    pools = (
        cp("s0", "S", "T0", 10**16, 10**16),
        state,
        cp("x01", "T0", "T1", 2 * 10**15, 10**15),
        cp("t1", "T1", "T", 10**16, 10**16),
    )
    return _single(*pools, token_in="S", token_out="T", amount=10**13)


def test_mixed_cycle_is_removed_and_resolved_on_the_one_shared_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, case = _cycle_network()
    calls = {"cl": 0, "cp": 0, "objective": 0}
    real_cl, real_cp, real_obj = cm.cl_arb, cm.cpmm_arb, cm.log_objective

    def count(key: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(*a: Any, **k: Any) -> Any:
            calls[key] += 1
            return fn(*a, **k)

        return wrapped

    monkeypatch.setattr(cm, "cl_arb", count("cl", real_cl))
    monkeypatch.setattr(cm, "cpmm_arb", count("cp", real_cp))
    monkeypatch.setattr(co, "log_objective", count("objective", real_obj))
    result = _solve(bundle, case)
    stats, work = _cfmm(result), _r021(result)["work"]
    assert result.status is SolveStatus.OK
    assert stats["recovery"]["cycle_removed"] and work["combinations_rejected_cycle"] >= 1
    assert stats["resolve"] == "resolved"
    (again,) = stats["restricted"]
    assert set(again["markets"]) < set(stats["markets"])
    assert work["objective_evaluations"] == calls["objective"]
    assert work["market_oracle_calls"] == calls["cl"] + calls["cp"]
    assert work["market_oracle_calls"] == (
        stats["initial"]["evaluations"] * len(stats["markets"])
        + again["evaluations"] * len(again["markets"])
    )
    total = stats["initial"]["evaluations"] + again["evaluations"]
    assert work["objective_evaluations"] == total <= V2["max_function_evaluations"]
    assert _invariants(bundle, case, result.plan) == result.score
    assert result.plan is not None
    ev = reference_evaluate(bundle, case, result.plan, gross_only())
    tokens = [(t.token_in, t.token_out) for t in ev.trace]
    assert ("T0", "T1") not in tokens or ("T1", "T0") not in tokens  # no cycle survives
    # estimate: the initial full-network value only, never the restricted one
    cert = _r021(result)["certificate"]
    if cert["bound_kind"] == "estimate":
        assert float(cert["estimate"]["value"]) == stats["initial"]["value"]
    else:
        assert stats["estimate_withheld"] == f"initial_{stats['initial']['termination']}"
    # forced shared caps: the guard never overruns, a starved re-solve is skipped (no refund)
    for cap in (1, 2, 3, 5):
        capped = _solve(bundle, case, max_function_evaluations=cap)
        c_work = _r021(capped)["work"]
        assert c_work["objective_evaluations"] <= cap
        assert capped.status is not SolveStatus.ALGORITHM_ERROR
        if _cfmm(capped)["initial"]["evaluations"] == cap:
            assert _cfmm(capped)["restricted"] == []
    first = stats["initial"]["iterations"]
    starved = _solve(bundle, case, max_iterations=first)
    assert _cfmm(starved)["resolve"] in ("skipped", None)
    assert _r021(starved)["work"]["optimizer_iterations"] <= first


def test_estimates_fallbacks_and_prunes_are_labelled_on_real_mixed_cases() -> None:
    """Over every corpus-fixture case: an estimate iff the INITIAL solve converged and no
    fallback was used; every recovery failure is a declared reason with a labelled fallback;
    every prune names an exact-quote reason; nothing is an algorithm_error."""
    corpus = _bundle(CORPUS)
    prepared = _prepare(corpus)
    seen: set[str] = set()
    for case in corpus.cases:
        result = _solve(corpus, case, prepared)
        stats, rec = _cfmm(result), _r021(result)
        assert result.status is not SolveStatus.ALGORITHM_ERROR
        if result.status is not SolveStatus.OK:
            assert rec["certificate"] is None
            continue
        cert = rec["certificate"]
        converged = stats["initial"]["termination"] == "converged"
        assert (cert["bound_kind"] == "estimate") == (converged and not rec["fallback"]["used"])
        assert cert["upper_raw"] is None and cert["optimality_proven"] is False
        if stats["recovery_failure"] is not None:
            assert stats["recovery_failure"] in cd.FALLBACK_REASONS
            assert rec["fallback"] == {
                "used": True,
                "source": "single_path",
                "reason": stats["recovery_failure"],
            }
            seen.add("fallback")
        for prune in (stats["recovery"] or {}).get("pruned", []):
            assert prune["reason"] in (
                "insufficient_output_amount", "insufficient_liquidity", "incomplete_snapshot",
                "reverted", "unsupported", "zero_output",
            )  # fmt: skip
        seen.add(stats["initial"]["termination"])
    assert {"converged", "not_converged", "fallback"} <= seen


def test_cl_solves_are_deterministic_in_any_case_order_with_one_shared_prepare() -> None:
    bundle = _bundle(SOURCES["uniswap_v3"])
    prepared = _prepare(bundle)
    before = {p: (i, i.stats()) for p, i in prepared.cl_indexes.items()}

    def comparable(result: SolveResult) -> Any:
        stats = json.loads(json.dumps(result.search_stats))
        stats["r021"].pop("stages")
        return (result.status, result.plan, result.score, stats)

    cases = list(bundle.cases)
    forward = [comparable(_solve(bundle, c, prepared)) for c in cases]
    backward = [comparable(_solve(bundle, c, prepared)) for c in reversed(cases)]
    assert forward == backward[::-1]
    fresh = [comparable(_solve(bundle, c, _prepare(bundle))) for c in cases[:6]]
    assert fresh == forward[:6]  # a fresh prepare changes nothing (no hidden state)
    assert {p: (i, i.stats()) for p, i in prepared.cl_indexes.items()} == before


# ------------------------------------------------------------------ CLI: run + quote --details


def _saved_run(out: str) -> Path:
    marker = "(run "
    return Path(out[out.index(marker) + len(marker) :].split(")", 1)[0])


def test_cli_run_replay_report_and_quote_details_with_the_cl_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both CLI paths through the spawned worker. The literal CL profile (with its matched
    controls) on mantle_mixed: the v2 source identity, validated views, literal replay with
    no deterministic difference, the report. The same profile's cfmm_dual alone with a memory
    pass over the five-source corpus fixture (full cohort: every case a visible row): charged
    preparation (worker prepare seconds and peak). One quote --details of the literal profile
    with exactly one timed solve per algorithm."""
    import main
    from benchmark.results import load_case_records, load_manifest
    from benchmark.runner import compare_runs

    def run(bundle: Path, profile: Path, results: Path) -> Path:
        argv = ["run", "--bundle", str(bundle), "--profile", str(profile),
                "--results-dir", str(results), "--strategies", "profile"]  # fmt: skip
        assert main.main(argv) == 0
        capsys.readouterr()
        (run_dir,) = results.iterdir()
        return run_dir

    run_dir = run(MIXED, PROFILES / "cl.yaml", tmp_path / "runs")
    manifest = load_manifest(run_dir)
    assert list(manifest.algorithms) == ["path_split", "incremental_graph", NAME]
    entry = manifest.resolved_profile["algorithm_options"][NAME]
    assert entry["options"] == V2 and entry["source"] == {"kind": "preset", **V2_PIN}
    config = manifest.resolved_profile["algorithm_config"][NAME]
    assert config["capabilities"]["protocols"] == CL_STAGE
    own = [r for r in load_case_records(run_dir) if r["algorithm"] == NAME]
    assert len(own) == 4 and {r["status"] for r in own} == {"ok"}
    for r in own:
        view = r["diagnostics"]
        assert view["codes"] == [] and view["origin"] == "solver", view
        assert view["work"]["quotes_executed"] == view["checked_against"]["quotes_counted"]
        assert "prepare_cl_indexes" in view["stages"]
    replay = shlex.split(manifest.replay_command)
    assert replay[-2:] == ["--strategies", "profile"]
    assert main.main(replay[replay.index("main.py") + 1 :]) == 0
    capsys.readouterr()
    replayed = next(p for p in (tmp_path / "runs").iterdir() if p != run_dir)
    assert load_manifest(replayed).resolved_profile == manifest.resolved_profile
    assert compare_runs(run_dir, replayed) == []
    report = tmp_path / "report"
    assert main.main(["report", str(run_dir), "--output", str(report)]) == 0
    assert NAME in (report / "report.html").read_text()
    capsys.readouterr()
    # full cohort with a memory pass: cfmm_dual alone, every one of the 96 cases a row
    doc = yaml.safe_load((PROFILES / "cl.yaml").read_text())
    doc["algorithms"] = [NAME]
    doc["measurement"]["memory_pass"] = True
    memory_profile = tmp_path / "cl-memory.yaml"
    memory_profile.write_text(yaml.safe_dump(doc, sort_keys=False))
    full = run(CORPUS, memory_profile, tmp_path / "full")
    events = load_manifest(full).prepare_events
    assert events and all(e["algorithm"] == NAME for e in events)
    assert all(e["prepare_seconds"] > 0 for e in events)
    assert any((e.get("prepare_peak_bytes") or 0) > 0 for e in events)
    records = load_case_records(full)
    assert len(records) == 96
    assert "ok" in {r["status"] for r in records}
    allowed = {"ok", "no_route", "unsupported", "incomplete_snapshot"}
    assert {r["status"] for r in records} <= allowed
    for r in records:
        assert r["diagnostics"]["codes"] == [], r["diagnostics"]
        assert r["search"]["cfmm"]["cl_prepare"]["pools"] == 9
    # LB-only and a net objective: explicit unsupported rows, never omitted
    lb_run = run(LB, memory_profile, tmp_path / "lb")
    lb_rows = load_case_records(lb_run)
    assert len(lb_rows) == len(_bundle(LB).cases)
    for r in lb_rows:
        assert r["status"] == "unsupported" and r["diagnostics"]["codes"] == []
        assert r["search"]["r021"]["scope"] == {"supported": False, "reason": "protocol_ceiling"}
    net = yaml.safe_load((REPO / "config" / "daily.yaml").read_text())
    net["algorithms"] = [NAME]
    net["algorithm_options"] = {NAME: dict(V2)}
    net_profile = tmp_path / "net.yaml"
    net_profile.write_text(yaml.safe_dump(net, sort_keys=False))
    net_rows = load_case_records(run(CORPUS, net_profile, tmp_path / "net"))
    assert net_rows and all(r["status"] == "unsupported" for r in net_rows)
    assert all(r["search"]["r021"]["scope"]["reason"] == "objective" for r in net_rows)
    quotes = tmp_path / "quotes"
    argv = ["quote", "--bundle", str(CORPUS), "--profile", str(PROFILES / "cl.yaml"),
            "--token-in", "USDC", "--token-out", "WMNT", "--amount", "25", "--details",
            "--quotes-dir", str(quotes), "--strategies", "profile"]  # fmt: skip
    assert main.main(argv) == 0
    out = capsys.readouterr().out
    assert f"[{NAME}] " in out and "prepare_cl_indexes" in out and "[numeric]" in out
    quote_run = _saved_run(out)
    rows = load_case_records(quote_run)
    assert [r["algorithm"] for r in rows] == ["path_split", "incremental_graph", NAME]
    for r in rows:
        assert r["measurement"]["attempts_completed"] == 1
        assert len(r["measurement"]["solve_seconds"]) == 1
    assert rows[-1]["diagnostics"]["codes"] == [] and rows[-1]["status"] == "ok"
    saved = yaml.safe_load((quote_run.parent.parent / "profile.yaml").read_text())
    assert saved["algorithm_options"] == {NAME: V2}


def test_help_names_the_current_cl_stage() -> None:
    import subprocess

    out = subprocess.run(
        [sys.executable, "main.py", "run", "--help"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert "cfmm_dual (CPMM+CL stage, gross-only)" in " ".join(out.split())

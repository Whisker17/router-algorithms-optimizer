"""WHI-1599: the production output-bound helper `pools.bounds` against (a) the reference
formulae of `docs/references/research-022/output-bounds.md` (held, as test-local functions,
by `tests/pools/test_output_bounds_contract.py`) and (b) the exact quote seam
`pools.quote.quote_exact_in`. The helper is never validated only against itself:

* `test_helper_equals_the_reference_formulae_*` compares its rate and slack, direction by
  direction, with WHI-1597's formulae on random states (including broken states, so the "no
  bound" side agrees too) and on the real pools;
* `test_helper_never_underestimates_the_exact_seam_*` takes every expected value from the
  seam: `quote <= floor(rate * x)` and `q(x + m) - q(x) <= rate * m + slack`;
* the mutation checks show those seam checks would fail for an underestimating rate or an
  under-sized slack.

The frozen real corpus lives only in the primary clone's gitignored `data/`; point
`ROUTER_CORPUS_BUNDLE` at its `bundle` directory (the contract test's variable). Unset, that
parameter *skips* (a skip is not evidence) and the tracked 19-pool real fixture bundle is the
always-run fallback.
"""

from __future__ import annotations

import importlib.util
import random
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, cast

import pytest

from pools.bounds import (
    CHUNK_DOMAIN_MAX,
    BoundTable,
    OutputBound,
    bound_out,
    build_bounds,
    output_bound,
)
from pools.cl_math import FEE_PIPS_DENOMINATOR, get_sqrt_ratio_at_tick
from pools.quote import quote_exact_in
from pools.result import QuoteStatus
from snapshot.models import (
    ConcentratedPoolState,
    ConstantProductPoolState,
    LiquidityBookPoolState,
    PoolState,
    SnapshotBundle,
)

REPO = Path(__file__).resolve().parents[2]


def _load_contract() -> ModuleType:
    path = REPO / "tests" / "pools" / "test_output_bounds_contract.py"
    spec = importlib.util.spec_from_file_location("_whi1599_output_bounds", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


OB = _load_contract()
Mutation = Callable[[OutputBound], OutputBound]


def _random_states(rng: random.Random, count: int) -> list[tuple[PoolState, int]]:
    """Valid and deliberately broken states of the three families (and their input scale)."""
    out: list[tuple[PoolState, int]] = []
    for i in range(count):
        cp = cast(ConstantProductPoolState, OB.random_cpmm(rng))
        out.append((cp, cp.reserve0))
        cl, cl_scale = cast(
            "tuple[ConcentratedPoolState, int]", OB.random_cl(rng, extreme=i % 5 == 0)
        )
        out.append((cl, cl_scale))
        lb, lb_scale = cast(
            "tuple[LiquidityBookPoolState, int]", OB.random_lb(rng, extreme=i % 6 == 0)
        )
        out.append((lb, lb_scale))
        # broken variants: each must be "no bound" in the helper exactly when it is in the formulae
        out.append((replace(cp, reserve1=0), 1))
        out.append((replace(cp, fee_bps=rng.choice([-1, 10_000, 12_345]), source_key=None), 1))
        out.append((replace(cl, tick=cl.tick + rng.choice([-3, 2, 60])), cl_scale))
        out.append((replace(cl, fee=rng.choice([-1, FEE_PIPS_DENOMINATOR])), cl_scale))
        out.append((replace(cl, lm_pool="0x" + "11" * 20, source_key="uniswap_v3"), cl_scale))
        out.append((replace(cl, source_key="not_a_source"), cl_scale))
        out.append((replace(lb, static_fee=None), lb_scale))
        out.append((replace(lb, bin_step=0), lb_scale))
        out.append(
            (replace(lb, active_id=lb.active_id + rng.choice([-600_000, 700_000])), lb_scale)
        )
    return out


# ======================================================================================
# (a) the helper equals the reference formulae
# ======================================================================================


def _assert_equals_formulae(state: PoolState, token: str) -> bool:
    """`True` iff the direction has a bound; the helper must agree with WHI-1597 exactly."""
    expected_rate = OB.output_rate(state, token)
    expected_slack = OB.chunk_slack(state, token)
    got = output_bound(state, token)
    if expected_rate is None:
        assert got is None, (state.pool_id, token)
        assert expected_slack is None
        return False
    assert got is not None, (state.pool_id, token)
    assert got.rate == expected_rate and got.slack == expected_slack, (state.pool_id, token)
    return True


def test_helper_equals_the_reference_formulae_on_random_and_broken_states() -> None:
    rng = random.Random(22_1599_01)
    bounded: Counter[str] = Counter()
    unbounded: Counter[str] = Counter()
    rate_only = 0
    for state, _ in _random_states(rng, 250):
        family = type(state).__name__
        for token in (state.token0, state.token1, "NOT_A_TOKEN"):
            if _assert_equals_formulae(state, token):
                bounded[family] += 1
                rate_only += (
                    output_bound(state, token) or OutputBound(Fraction(0), Fraction(0))
                ).slack is None
            else:
                unbounded[family] += 1
    print(
        f"formulae equality: bounded={dict(bounded)} no_bound={dict(unbounded)} "
        f"rate_only={rate_only}"
    )
    assert min(bounded.values()) > 200 and len(bounded) == 3  # every family, both outcomes
    assert min(unbounded.values()) > 100 and len(unbounded) == 3
    assert rate_only > 0  # a CL 0->1 pool with sqrt_price >= 2**128: a rate with no slack


def test_no_bound_is_never_zero_and_a_rate_is_never_negative() -> None:
    rng = random.Random(22_1599_02)
    for state, _ in _random_states(rng, 120):
        for token in (state.token0, state.token1):
            got = output_bound(state, token)
            if got is not None:
                assert got.rate > 0
                assert got.slack is None or got.slack > 0


def test_rate_and_slack_are_independent_for_a_high_price_cl_pool() -> None:
    rng = random.Random(22_1599_03)
    state = cast(
        ConcentratedPoolState,
        OB._cl_state(
            rng, source="uniswap_v3", spacing=60, fee=3000, tick=600_000, price_mode="mid",
            ranges=[(599_940, 600_060, 10**15)],
        ),
    )  # fmt: skip
    assert state.sqrt_price_x96 >= 1 << 128
    up, down = output_bound(state, "A"), output_bound(state, "B")
    assert up is not None and up.slack is None and up.rate > 0  # 0->1: rate only
    assert down is not None and down.slack is not None  # 1->0 keeps its slack


def test_helper_tick_guard_and_lb_window_match_the_contract() -> None:
    rng = random.Random(22_1599_04)
    state = cast(
        ConcentratedPoolState,
        OB._cl_state(
            rng, source="uniswap_v3", spacing=60, fee=3000, tick=0, price_mode="low",
            ranges=[(-600, 600, 10**15)],
        ),
    )  # fmt: skip
    assert output_bound(state, "A") is not None and output_bound(state, "B") is not None
    assert output_bound(replace(state, tick=1), "A") is None  # the frozen tick above the price
    top = replace(state, tick=887272, sqrt_price_x96=OB.MAX_SQRT_RATIO)
    assert output_bound(top, "B") is None  # tick + 1 outside TickMath's domain
    lb, _ = cast("tuple[LiquidityBookPoolState, int]", OB.random_lb(rng))
    for active in (OB.REAL_ID_SHIFT - 700_000, OB.REAL_ID_SHIFT + 700_000):
        assert output_bound(replace(lb, bin_step=1, active_id=active), "A") is None
    assert (get_sqrt_ratio_at_tick(0) >> 0) > 0


# ======================================================================================
# (b) the helper against the exact seam (expected values come only from the seam)
# ======================================================================================


class Violations:
    def __init__(self) -> None:
        self.ok_quotes = 0
        self.chunk_pairs = 0
        self.rate_violations = 0
        self.chunk_violations = 0
        self.chunk_above_rate = 0

    @property
    def total(self) -> int:
        return self.rate_violations + self.chunk_violations


def _seam_check(
    state: PoolState,
    token: str,
    amounts: list[int],
    rng: random.Random,
    tally: Violations,
    mutate: Mutation | None = None,
) -> None:
    bound = output_bound(state, token)
    if bound is None:
        return
    if mutate is not None:
        bound = mutate(bound)
    for x in amounts:
        result = quote_exact_in(state, token, x)
        if result.status is QuoteStatus.OK:
            tally.ok_quotes += 1
            tally.rate_violations += result.amount_out > bound_out(bound.rate, x)
        m = rng.choice([1, 2, 9, x // 3 + 1, x])
        if bound.slack is None or x + m > CHUNK_DOMAIN_MAX:
            continue
        q_x = 0 if x == 0 else OB.effective_out(quote_exact_in(state, token, x))
        q_xm = OB.effective_out(quote_exact_in(state, token, x + m))
        if q_x is None or q_xm is None:
            continue
        tally.chunk_pairs += 1
        marginal = q_xm - q_x
        tally.chunk_above_rate += marginal > bound.rate * m
        tally.chunk_violations += marginal > bound.rate * m + bound.slack


def _random_check(mutate: Mutation | None, seed: int = 22_1599_05) -> Violations:
    rng = random.Random(seed)
    tally = Violations()
    for state, scale in _random_states(rng, 140):
        for token in (state.token0, state.token1):
            amounts = [*OB.amount_grid(rng, scale, 2), 2**127, 2**128 - 1]
            _seam_check(state, token, amounts, rng, tally, mutate)
    return tally


def test_helper_never_underestimates_the_exact_seam_on_random_states() -> None:
    tally = _random_check(None)
    print(
        f"seam: ok_quotes={tally.ok_quotes} chunk_pairs={tally.chunk_pairs} "
        f"chunk_above_rate={tally.chunk_above_rate}"
    )
    assert tally.total == 0
    assert tally.ok_quotes > 2000 and tally.chunk_pairs > 2000
    assert tally.chunk_above_rate > 100  # the slack is exercised, not vacuous


@pytest.mark.parametrize(
    ("label", "mutate", "field"),
    [
        (
            "rate shrunk by 1e-6",
            lambda b: replace(b, rate=b.rate * Fraction(999_999, 10**6)),
            "rate",
        ),
        ("rate halved", lambda b: replace(b, rate=b.rate / 2), "rate"),
        ("slack dropped", lambda b: replace(b, slack=Fraction(0) if b.slack else None), "chunk"),
        (
            "slack halved",
            lambda b: replace(b, slack=None if b.slack is None else b.slack / 2),
            "chunk",
        ),
    ],
)
def test_the_seam_check_catches_an_underestimating_helper(
    label: str, mutate: Mutation, field: str
) -> None:
    """Mutation check: the same seam check fails when the bound is made too small."""
    tally = _random_check(mutate)
    caught = tally.rate_violations if field == "rate" else tally.chunk_violations
    assert caught > 0, f"mutation {label!r} was not caught"


# ======================================================================================
# The table of a bundle: eager, immutable, equal to the formulae, same quotes
# ======================================================================================


def _check_bundle(bundle: SnapshotBundle, label: str) -> None:
    table = build_bounds(bundle)
    assert isinstance(table, BoundTable) and isinstance(table.bounds, MappingProxyType)
    with pytest.raises(TypeError):
        table.bounds[("x", "y")] = None  # type: ignore[index]
    directions = 2 * len(bundle.pools)
    assert table.pool_directions == directions == len(table.bounds)
    assert table.bounded + table.rate_only + table.no_bound == directions
    assert table.no_bound == 0, f"{label}: real pool directions without a bound"
    assert table.seconds >= 0
    record = table.prepare_record()
    assert record == {
        "pool_directions": directions,
        "bounded": table.bounded,
        "rate_only": table.rate_only,
        "no_bound": 0,
    }
    rng = random.Random(22_1599_06)
    tally = Violations()
    for pool_id, state in bundle.pools.items():
        for token in (state.token0, state.token1):
            assert _assert_equals_formulae(state, token)
            assert table.bounds[(pool_id, token)] == output_bound(state, token)
            _seam_check(state, token, OB._amounts_for(bundle, token), rng, tally)
    assert tally.total == 0
    assert tally.ok_quotes > 0 and tally.chunk_pairs > 0
    print(
        f"BOUND TABLE {label}: pools={len(bundle.pools)} directions={directions} "
        f"bounded={table.bounded} rate_only={table.rate_only} ok_quotes={tally.ok_quotes} "
        f"chunk_pairs={tally.chunk_pairs}"
    )


@pytest.mark.parametrize("label", ["tracked-fixture", "frozen-corpus"])
def test_bound_table_of_the_real_pools_equals_the_formulae_and_dominates_the_seam(
    label: str,
) -> None:
    available = dict(OB._corpus_paths())
    if label not in available:
        pytest.skip(f"{label}: set {OB.CORPUS_ENV} (data/ exists only in the primary clone)")
    bundle = cast(SnapshotBundle, OB._bundle(str(available[label])))
    if label == "frozen-corpus":
        assert bundle.bundle_hash == OB.CORPUS_BUNDLE_HASH and len(bundle.pools) == 143
    _check_bundle(bundle, label)


def test_a_table_with_missing_values_is_all_no_bound() -> None:
    state = ConstantProductPoolState("p", "A", "B", 0, 10**9, 30)
    bundle = cast(Any, type("B", (), {"pools": {"p": state}})())
    table = build_bounds(bundle)
    assert (table.pool_directions, table.bounded, table.rate_only, table.no_bound) == (2, 0, 0, 2)
    assert all(v is None for v in table.bounds.values())

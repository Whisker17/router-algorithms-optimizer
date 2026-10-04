"""R022-Q02 (WHI-1598): the executable form of
`docs/references/research-022/pruning-contract.md`.

This module is research evidence, not a strategy. It holds three things, kept apart:

1. **A test-local pruning model** for each target (`run_single_path`, `BoundedAllocator`):
   the reference loops of `single_path`, `incremental_graph` and `metis_history` with the
   contract's skip rules inserted. It is *never* the expected value. The bound formulae
   (`output_rate`, `chunk_slack`) are WHI-1597's, loaded from
   `tests/pools/test_output_bounds_contract.py`, not re-derived.
2. **Expected values from the actual reference solvers** (`single_path.solve`,
   `incremental_graph.solve`, `metis_history.solve`, the shared `metis_inspired._Allocator`
   choosers) and from the exact quote seam.
3. **Negative controls**: each tempting-but-unsafe prune (a bound without the chunk slack, a
   `<=` prune against a solver that lets ties replace, an underestimating rate, a
   negative-cost objective, label pruning where dominance or a cap can interact) is shown to
   change the reference result, so the safe rules are not vacuous.

No solver is changed here. Fixtures are tracked (`tests/fixtures/routing/*`,
`tests/fixtures/corpus/bundle`), so nothing skips in a worktree.
"""

from __future__ import annotations

import importlib.util
import random
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from benchmark.objective import UNRANKED_OFFSET, gross_only, synthetic_fixed_cost
from pools.quote import quote_exact_in
from pools.result import QuoteStatus
from routing.algorithms import incremental_graph, metis_history, metis_inspired, single_path
from routing.algorithms.base import (
    AlgorithmConfig,
    Budget,
    SolveContext,
    SolveResult,
    SolveStatus,
)
from routing.algorithms.incremental_graph import PoolFlow, chunk_amounts, merged_plan
from routing.evaluator import EvalStatus, Evaluation, evaluate
from routing.plan import RoutePlan
from routing.search import (
    Edge,
    GraphIndex,
    QuoteCache,
    build_graph_index,
    enumerate_paths,
    path_plan,
)
from routing.search import (
    Path as RoutePath,
)
from snapshot.bundle import load_bundle
from snapshot.models import BlockRef, Case, ConstantProductPoolState, SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
DOC = REPO / "docs" / "references" / "research-022" / "pruning-contract.md"
TWO127 = 1 << 127
BLOCK = BlockRef(chain_id=0, number=0, hash="0x" + "00" * 32, timestamp=0)


# ======================================================================================
# WHI-1597's bound formulae (imported, never re-derived)
# ======================================================================================


def _load_output_bounds() -> ModuleType:
    path = REPO / "tests" / "pools" / "test_output_bounds_contract.py"
    spec = importlib.util.spec_from_file_location("_whi1597_output_bounds", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


OB = _load_output_bounds()


class Bounds:
    """Per-(pool, token_in) rate `r̄` and chunk slack `s` of the **original** bundle state
    (output-bounds.md §7 items 1, 6, 7). `None` is "no bound": the model never prunes on it.
    `rate_scale` / `slack_scale` exist only for the negative controls (an underestimate)."""

    def __init__(
        self,
        bundle: SnapshotBundle,
        *,
        rate_scale: Fraction = Fraction(1),
        slack_scale: Fraction = Fraction(1),
    ) -> None:
        self.bundle, self.rate_scale, self.slack_scale = bundle, rate_scale, slack_scale
        self._rate: dict[tuple[str, str], Fraction | None] = {}
        self._slack: dict[tuple[str, str], Fraction | None] = {}
        self.no_bound_lookups = 0

    def rate(self, edge: Edge) -> Fraction | None:
        key = (edge.pool_id, edge.token_in)
        if key not in self._rate:
            r = cast("Fraction | None", OB.output_rate(self.bundle.pools[key[0]], key[1]))
            self._rate[key] = None if r is None else r * self.rate_scale
        if self._rate[key] is None:
            self.no_bound_lookups += 1
        return self._rate[key]

    def slack(self, edge: Edge) -> Fraction | None:
        key = (edge.pool_id, edge.token_in)
        if key not in self._slack:
            s = cast("Fraction | None", OB.chunk_slack(self.bundle.pools[key[0]], key[1]))
            self._slack[key] = None if s is None else s * self.slack_scale
        return self._slack[key]


def floor_frac(value: Fraction) -> int:
    return value.numerator // value.denominator


# ======================================================================================
# Fixtures
# ======================================================================================


@cache
def fixture(name: str) -> SnapshotBundle:
    return load_bundle(FIXTURES / name)


def cp(
    pool_id: str, t0: str, t1: str, r0: int, r1: int, fee_bps: int = 30
) -> ConstantProductPoolState:
    return ConstantProductPoolState(
        pool_id=pool_id,
        token0=t0,
        token1=t1,
        reserve0=r0,
        reserve1=r1,
        fee_bps=fee_bps,
        source_key=None,
    )


def synthetic(*pools: ConstantProductPoolState) -> SnapshotBundle:
    return SnapshotBundle(
        bundle_id="t",
        kind="synthetic",
        schema_version=1,
        block=BLOCK,
        pools={p.pool_id: p for p in pools},
        cases=(),
        bundle_hash="deadbeef",
        source_path="<test>",
    )


def case_of(token_in: str, token_out: str, amount: int, case_id: str = "c") -> Case:
    return Case(case_id=case_id, token_in=token_in, token_out=token_out, amount_in=amount)


def corpus_cases(bundle: SnapshotBundle, per_pair: int = 2) -> list[Case]:
    """A small, deterministic, pair-diverse subset of a bundle's own cases."""
    seen: dict[tuple[str, str], int] = {}
    out: list[Case] = []
    for case in bundle.cases:
        key = (case.token_in, case.token_out)
        if seen.get(key, 0) < per_pair:
            seen[key] = seen.get(key, 0) + 1
            out.append(case)
    return out


# ======================================================================================
# 1. single_path: the model (reference loop + the §3 skip rule) and its reference
# ======================================================================================


@dataclass
class SPOutcome:
    status: str
    plan: RoutePlan | None
    evaluation: Evaluation | None
    score: int | None
    considered: int
    truncated: int
    stats: dict[str, Any] = field(default_factory=dict)
    published: list[RoutePlan] = field(default_factory=list)  # `report_candidate` sequence


def sp_reference(
    bundle: SnapshotBundle,
    case: Case,
    *,
    max_hops: int,
    objective: Any = None,
    budget: Budget | None = None,
    sink: list[RoutePlan] | None = None,
) -> SolveResult:
    prepared = single_path.prepare(
        bundle, AlgorithmConfig(single_path.NAME, {"max_hops": max_hops})
    )
    context = SolveContext(
        bundle=bundle,
        objective=objective or gross_only(),
        prepared=prepared,
        candidate_sink=None if sink is None else sink.append,
    )
    return single_path.solve(case, context, budget or Budget())


def sp_path_ub(
    case: Case, path: RoutePath, prefix_out: Mapping[RoutePath, int], bounds: Bounds
) -> int | None:
    """`UB = prefix_out x prod r̄(remaining edges)` as nested floors (output-bounds.md §5.1),
    from the longest already-evaluated prefix (the same search `_new_quotes_needed` makes).
    `None` if any remaining hop has no bound."""
    k, amount = 0, case.amount_in
    for j in range(len(path) - 1, 0, -1):
        if path[:j] in prefix_out:
            k, amount = j, prefix_out[path[:j]]
            break
    for edge in path[k:]:
        rate = bounds.rate(edge)
        if rate is None:
            return None
        amount = floor_frac(rate * amount)
    return amount


def run_single_path(
    bundle: SnapshotBundle,
    case: Case,
    *,
    max_hops: int,
    objective: Any = None,
    budget: Budget | None = None,
    bounds: Bounds | None = None,
    replace_on_tie: bool = False,
    compare_with: str = "score",
) -> SPOutcome:
    """`single_path.solve`'s loop, line for line, with one inserted rule: after the
    dead-prefix check and before any budget check, skip a candidate whose `UB` is at most
    the incumbent. `bounds=None` is the unmodified reference. `replace_on_tie` and
    `compare_with="gross"` exist for the negative controls only."""
    objective = objective or gross_only()
    budget = budget or Budget()
    index = build_graph_index(bundle)
    cache_ = QuoteCache(bundle)
    prefix_out: dict[RoutePath, int] = {}
    dead: dict[RoutePath, str] = {}
    best: tuple[RoutePlan, Evaluation, int, RoutePath] | None = None
    published: list[RoutePlan] = []
    enumerated = evaluated = pruned = pruned_bound = truncated = bound_evals = 0
    truncated_by: str | None = None
    for path in enumerate_paths(index, case.token_in, case.token_out, max_hops):
        enumerated += 1
        if truncated_by is not None:
            truncated += 1
            continue
        if any(path[:k] in dead for k in range(1, len(path))):
            pruned += 1
            continue
        if bounds is not None and best is not None:
            bound_evals += 1
            ub = sp_path_ub(case, path, prefix_out, bounds)
            incumbent = best[2] if compare_with == "score" else best[1].gross_output
            if ub is not None and ub <= incumbent:
                pruned_bound += 1
                continue
        if budget.max_candidates is not None and evaluated >= budget.max_candidates:
            truncated_by, truncated = "max_candidates", truncated + 1
            continue
        if budget.max_quotes is not None:
            need = single_path._new_quotes_needed(case, path, cache_, prefix_out)
            if cache_.misses + need > budget.max_quotes:
                truncated_by, truncated = "max_quotes", truncated + 1
                continue
        plan = path_plan(case, path)
        evaluation = evaluate(bundle, case, plan, objective, quote=cache_)
        evaluated += 1
        for step in evaluation.trace:
            if step.status in (QuoteStatus.OK.value, "zero_input"):
                prefix_out[path[: step.step + 1]] = step.amount_out
            else:
                dead[path[: step.step + 1]] = step.status
        if evaluation.status is not EvalStatus.OK:
            continue
        score = objective.score(evaluation)
        if best is None or score > best[2] or (replace_on_tie and score == best[2]):
            best = (plan, evaluation, score, path)
            published.append(plan)
    stats = {
        "paths_enumerated": enumerated,
        "paths_evaluated": evaluated,
        "paths_pruned": pruned,
        "pruned_bound": pruned_bound,
        "bound_evaluations": bound_evals,
        "paths_truncated": truncated,
        "truncated_by": truncated_by,
        "quotes_executed": cache_.misses,
        "quotes_memoized": cache_.hits,
    }
    considered = evaluated + pruned + pruned_bound
    if best is None:
        assert pruned_bound == 0, "no incumbent, so nothing may be pruned"
        return SPOutcome("no_plan", None, None, None, considered, truncated, stats, published)
    return SPOutcome("ok", best[0], best[1], best[2], considered, truncated, stats, published)


# fields of `SolveResult.search_stats` the contract (§6.1) requires to be identical /
# allows to differ under non-binding budgets.
SP_IDENTICAL = (
    "paths_enumerated",
    "paths_truncated",
    "truncated_by",
)
SP_ALLOWED_TO_DIFFER = (
    "paths_evaluated",
    "paths_pruned",
    "quotes_executed",
    "quotes_memoized",
    "failed_candidates",
    "paths_incomplete",
    "incomplete_example",
)


def assert_sp_identical(ref: SolveResult, out: SPOutcome) -> None:
    assert (ref.status is SolveStatus.OK) == (out.status == "ok")
    if ref.status is not SolveStatus.OK:
        return
    assert out.plan == ref.plan
    assert out.evaluation is not None and ref.evaluation is not None
    assert out.evaluation.to_dict() == ref.evaluation.to_dict()
    assert out.score == ref.score
    assert out.considered == ref.candidates_considered
    assert out.truncated == ref.candidates_truncated
    for key in SP_IDENTICAL:
        assert out.stats[key] == ref.search_stats[key], key


class ShapeCost:
    """A duck-typed `empirical_cost` objective (the evaluator only reads `mode`, `label`,
    `fixed_cost` and `plan_cost`): a cost that depends on the plan *shape* (hops), `None`
    marks a shape without a reliable cost (unranked: `gross - UNRANKED_OFFSET`). It is a
    stand-in for `ObjectiveContext.empirical_cost`, whose cost is non-negative by
    construction (price > 0 and fee >= 0 are enforced by the loaders)."""

    mode = "empirical_cost"
    fixed_cost = 0
    label = "stub shape cost"

    def __init__(self, cost_by_hops: Mapping[int, int | None]) -> None:
        self.cost_by_hops = dict(cost_by_hops)

    def plan_cost(self, route_features: dict[str, int], token_out: str) -> dict[str, Any]:
        cost = self.cost_by_hops.get(route_features["hops"])
        if cost is None:
            return {"net_rankable": False}
        return {"net_rankable": True, "nominal_out_raw": str(cost)}

    def score(self, evaluation: Evaluation) -> int:
        assert evaluation.status is EvalStatus.OK
        if evaluation.estimated_net_output is not None:
            return evaluation.estimated_net_output
        return evaluation.gross_output - UNRANKED_OFFSET


def sp_fixtures() -> list[tuple[str, SnapshotBundle, Case]]:
    out: list[tuple[str, SnapshotBundle, Case]] = []
    for name in ("cpmm_graph", "mantle_mixed"):
        bundle = fixture(f"routing/{name}")
        out += [(name, bundle, c) for c in bundle.cases]
    corpus = fixture("corpus/bundle")
    out += [("corpus", corpus, c) for c in corpus_cases(corpus)]
    return out


SP_OBJECTIVES: list[tuple[str, Callable[[], Any]]] = [
    ("gross", gross_only),
    ("fixed0", lambda: synthetic_fixed_cost(0)),
    ("fixed1e6", lambda: synthetic_fixed_cost(10**6)),
    ("shape", lambda: ShapeCost({1: 5, 2: 40, 3: None})),
]


def test_single_path_replaces_only_on_strictly_greater_score_so_equal_candidates_never_matter() -> (
    None
):
    """Premise: `score > best[2]` (single_path.py). Two parallel, identical pools tie; the
    reference keeps the first, so a prune of the equal later candidate (`UB <= incumbent`)
    cannot change the plan."""
    bundle = synthetic(
        cp("p1", "A", "B", 10**12, 10**12),
        cp("p2", "A", "B", 10**12, 10**12),
        cp("p3", "A", "B", 10**12, 5 * 10**11),
    )
    case = case_of("A", "B", 1001)
    ref = sp_reference(bundle, case, max_hops=2)
    assert ref.plan is not None and [s.pool_id for s in ref.plan.steps] == ["p1"]  # first wins
    out = run_single_path(bundle, case, max_hops=2, bounds=Bounds(bundle))
    assert_sp_identical(ref, out)
    # p2 ties p1 exactly (UB(p2) >= its score == incumbent), p3 is strictly worse
    assert out.stats["pruned_bound"] >= 1


def test_single_path_prune_is_exact_and_not_vacuous_on_every_objective() -> None:
    total_pruned = total_candidates = 0
    for name, bundle, case in sp_fixtures():
        bounds = Bounds(bundle)
        for label, make in SP_OBJECTIVES:
            for hops in (2, 3):
                sink: list[RoutePlan] = []
                ref = sp_reference(bundle, case, max_hops=hops, objective=make(), sink=sink)
                out = run_single_path(bundle, case, max_hops=hops, objective=make(), bounds=bounds)
                assert_sp_identical(ref, out)
                assert out.published == sink  # every new best is published, in the same order
                assert out.stats["bound_evaluations"] >= out.stats["pruned_bound"]
                # allowed-to-differ counters only ever shrink here (work saved, never added)
                assert out.stats["quotes_executed"] <= ref.search_stats["quotes_executed"], (
                    name,
                    case.case_id,
                    label,
                    hops,
                )
                assert out.stats["paths_evaluated"] <= ref.search_stats["paths_evaluated"]
                total_pruned += out.stats["pruned_bound"]
                total_candidates += out.stats["paths_enumerated"]
    assert total_pruned > 0 and total_candidates > 0  # a model that never prunes proves nothing


def test_single_path_no_bound_never_prunes() -> None:
    """`None` rate = never prune (output-bounds.md §7 item 6)."""

    class NoBound(Bounds):
        def rate(self, edge: Edge) -> Fraction | None:
            return None

    for _, bundle, case in sp_fixtures()[:6]:
        out = run_single_path(bundle, case, max_hops=3, bounds=NoBound(bundle))
        assert out.stats["pruned_bound"] == 0
        assert_sp_identical(sp_reference(bundle, case, max_hops=3), out)


def test_single_path_unsafe_prunes_are_caught() -> None:
    """Each tempting-but-unsafe variant changes the reference result somewhere."""
    # (a) a rate shrunk by 1e-6 (an underestimating bound) loses a candidate that wins
    caught_rate = caught_tie = caught_negative = False
    for _, bundle, case in sp_fixtures():
        ref = sp_reference(bundle, case, max_hops=3)
        shrunk = Bounds(bundle, rate_scale=Fraction(1, 2))
        out = run_single_path(bundle, case, max_hops=3, bounds=shrunk)
        caught_rate |= out.score != ref.score or out.status != ref.status.value
    assert caught_rate, "an underestimating rate must be detectable"

    # (b) a solver that lets an equal score replace (`>=`) is NOT safe under `UB <= incumbent`
    # x = 1001 on 10**12 reserves: the spot-rate bound is tight (UB == the exact output 998),
    # so the equal candidate p2 is pruned exactly at the tie
    bundle = synthetic(cp("p1", "A", "B", 10**12, 10**12), cp("p2", "A", "B", 10**12, 10**12))
    case = case_of("A", "B", 1001)
    later_wins = run_single_path(bundle, case, max_hops=2, replace_on_tie=True)
    pruned = run_single_path(bundle, case, max_hops=2, replace_on_tie=True, bounds=Bounds(bundle))
    assert later_wins.plan is not None and pruned.plan is not None
    caught_tie = later_wins.plan != pruned.plan  # p2 would have replaced p1; the prune skipped it
    assert caught_tie

    # (c) net objective with a *negative* cost (a bonus for 2-hop plans) breaks
    # `score <= gross`: the gross bound then prunes the true winner
    bundle = synthetic(
        cp("d", "A", "C", 10**9, 10**9),
        cp("x", "A", "B", 10**9, 10**9),
        cp("y", "B", "C", 10**9, 10**9),
    )
    case = case_of("A", "C", 10**6)
    bonus = ShapeCost({1: 0, 2: -(10**6)})
    ref = sp_reference(bundle, case, max_hops=2, objective=bonus)
    assert ref.plan is not None and len(ref.plan.steps) == 2  # the bonus plan wins on net
    wrong = run_single_path(bundle, case, max_hops=2, objective=bonus, bounds=Bounds(bundle))
    caught_negative = wrong.plan is not None and len(wrong.plan.steps) == 1
    assert caught_negative
    # the same objective with a non-negative cost is handled exactly
    ok = ShapeCost({1: 0, 2: 10})
    assert_sp_identical(
        sp_reference(bundle, case, max_hops=2, objective=ok),
        run_single_path(bundle, case, max_hops=2, objective=ok, bounds=Bounds(bundle)),
    )


def test_single_path_budget_labels() -> None:
    """Contract §6.2: a bounded run that truncates implies the reference truncates; a
    bounded run that does not truncate equals the unbudgeted reference; when the reference
    does not truncate the bounded run equals it."""
    checked = binding = both_free = 0
    for _, bundle, case in sp_fixtures():
        bounds = Bounds(bundle)
        free = sp_reference(bundle, case, max_hops=3)
        for budget in (
            *(Budget(max_candidates=n) for n in (1, 2, 3, 5, 8)),
            *(Budget(max_quotes=n) for n in (1, 2, 4, 6, 10, 20)),
        ):
            ref = sp_reference(bundle, case, max_hops=3, budget=budget)
            out = run_single_path(bundle, case, max_hops=3, budget=budget, bounds=bounds)
            checked += 1
            if out.stats["truncated_by"] is not None:
                binding += 1
                assert ref.search_stats["truncated_by"] is not None  # B truncates => R truncates
            else:
                assert_sp_identical(free, out)  # B untruncated == the unbudgeted reference
            if ref.search_stats["truncated_by"] is None:
                both_free += 1
                assert out.stats["truncated_by"] is None
                assert_sp_identical(ref, out)
    assert checked and binding and both_free


# ======================================================================================
# 2. chunk searches: the model is `metis_inspired._Allocator` with a skip rule inserted
# ======================================================================================


class Gate:
    """Counters of the skip rule (the contract's `pruned_bound`, `bound_evaluations`)."""

    def __init__(self) -> None:
        self.pruned_bound = 0
        self.bound_evaluations = 0
        self.no_bound = 0


@dataclass(frozen=True)
class Entry:
    """`U_h(v)` (contract §5.3): every arrival from a label of amount `a` at `v` with `h`
    hops left is `<= r * a + s`; every amount that enters a pool on the way is `<= p * a + t`.
    `reach` False = no walk to the target (no arrival possible)."""

    r: Fraction
    s: Fraction
    p: Fraction
    t: Fraction
    reach: bool = True


ARRIVED = Entry(Fraction(1), Fraction(0), Fraction(1), Fraction(0))
NO_WALK = Entry(Fraction(0), Fraction(0), Fraction(1), Fraction(0), reach=False)


class UTable:
    """The backward bounded-hop max-product table over the relaxed constraints (any token
    may repeat, committed-cycle admission ignored): never enters `source`, never leaves
    `target`. `entry(v, h)` is `None` when any walk that can reach the target uses an edge
    without a bound (then no label at `v` with `h` hops left may be pruned)."""

    def __init__(
        self, index: GraphIndex, source: str, target: str, hops: int, bounds: Bounds
    ) -> None:
        self.target = target
        self.entries: dict[tuple[str, int], Entry | None] = {}
        self.cost = 0  # edge relaxations spent building the table (the contract's table cost)
        tokens = list(index.adjacency)
        for h in range(hops):
            for v in tokens:
                if v == target:
                    continue
                self.entries[(v, h)] = self._build(index, source, v, h, bounds)

    def _sub(self, w: str, h: int) -> Entry | None:
        return ARRIVED if w == self.target else self.entries.get((w, h), NO_WALK)

    def _build(
        self, index: GraphIndex, source: str, v: str, h: int, bounds: Bounds
    ) -> Entry | None:
        if h == 0:
            return NO_WALK
        best: Entry | None = None
        for e in index.edges_from(v):
            self.cost += 1
            if e.token_out == source:
                continue
            sub = self._sub(e.token_out, h - 1)
            if sub is not None and not sub.reach:
                continue
            r, s = bounds.rate(e), bounds.slack(e)
            if sub is None or r is None or s is None:
                return None
            cand = Entry(r * sub.r, s * sub.r + sub.s, r * sub.p, s * sub.p + sub.t)
            best = (
                cand
                if best is None
                else Entry(
                    max(best.r, cand.r),
                    max(best.s, cand.s),
                    max(best.p, cand.p),
                    max(best.t, cand.t),
                )
            )
        if best is None:
            return NO_WALK
        return Entry(best.r, best.s, max(Fraction(1), best.p), max(Fraction(0), best.t))

    def entry(self, v: str, h: int) -> Entry | None:
        return ARRIVED if v == self.target else self.entries.get((v, h), NO_WALK)


def walk_counts(
    index: GraphIndex, source: str, target: str, hops: int, dist: Mapping[str, int]
) -> list[dict[str, int]]:
    """`W[k][v]` = walks of exactly k edges from `source` to `v` that the label search could
    create (never into `source`, never out of `target`, within the distance filter; a token
    may repeat, so this only over-counts). Layer k holds at most `sum_v W[k][v]` labels and a
    (v, visited-set) group at most `W[k][v]`."""
    layers: list[dict[str, int]] = [{source: 1}]
    for k in range(1, hops + 1):
        nxt: dict[str, int] = {}
        for t, n in layers[-1].items():
            if t == target:
                continue
            for e in index.edges_from(t):
                v = e.token_out
                if v == source or (v != target and dist.get(v, hops + 1) > hops - k):
                    continue
                nxt[v] = nxt.get(v, 0) + n
        layers.append(nxt)
    return layers


def m2_gate(
    index: GraphIndex,
    source: str,
    target: str,
    hops: int,
    dist: Mapping[str, int],
    options: Mapping[str, Any],
) -> bool:
    """Contract §5.4: label (non-arrival) pruning is enabled only when no dominance event
    and no cap can occur in either run. `dominance: off` gives every label its own group
    (only the frontier cap can bind); `history` needs every group to hold at most one label."""
    layers = walk_counts(index, source, target, hops, dist)
    labels = [{v: n for v, n in layer.items() if v != target} for layer in layers[1:]]
    frontier = max((sum(layer.values()) for layer in labels), default=0)
    if frontier > options["max_frontier_labels"]:
        return False
    if options["dominance"] == "off":
        return True
    return all(n <= 1 for layer in labels for n in layer.values())


class BoundedAllocator(metis_inspired._Allocator):
    """`metis_inspired._Allocator` plus the chunk skip rules of contract §4/§5. `mode`:

    - `"enum"`  (incremental_graph): `_marginal` skips a path whose chain bound from its
      longest memoized prefix is `<= best_m` (the chunk's best marginal so far);
    - `"labels"` (metis_history): `step` skips a relaxation whose arrival bound is `<= best_m`.
      A relaxation into the target is rule M1 (always population neutral); into any other
      token it is rule M2 (needs `table`, enabled only behind `m2_gate`).

    `slack_scale` / `rate_scale` (through `Bounds`) and `use_slack` are negative controls.
    """

    def __init__(
        self,
        bundle: SnapshotBundle,
        case: Case,
        index: GraphIndex,
        quote: Callable[..., Any],
        bounds: Bounds,
        *,
        mode: str,
        hops: int = 0,
        table: UTable | None = None,
        use_slack: bool = True,
    ) -> None:
        super().__init__(bundle, case, index, quote)
        self.bounds, self.mode, self.hops = bounds, mode, hops
        self.table, self.use_slack = table, use_slack
        self.gate = Gate()
        self.best_m: int | None = None

    # --- accounting: a skipped candidate is never a failure disclosure
    def _count(self, failure: metis_inspired._Failure, path: RoutePath) -> None:
        if failure.reason == "pruned_bound":
            self.gate.pruned_bound += 1
            return
        super()._count(failure, path)

    def committed(self, edge: Edge) -> int:
        flow = self.flows.get(edge.pool_id)
        return flow.amount_in if flow else 0

    def _hop(self, edge: Edge, m_ub: int) -> int | None:
        """`floor(r̄ m + s)` bounds the marginal output of `m_ub` more input into `edge`'s
        pool on top of its committed aggregate (output-bounds.md §5.3); `None` = no bound
        (or outside the proved domain x + m <= 2**127)."""
        r, s = self.bounds.rate(edge), self.bounds.slack(edge)
        if r is None or s is None or self.committed(edge) + m_ub > TWO127:
            self.gate.no_bound += 1
            return None
        return floor_frac(r * m_ub + (s if self.use_slack else 0))

    # --- incremental_graph: enumerated paths
    def _marginal(
        self, path: RoutePath, amount: int, memo: dict[RoutePath, Any]
    ) -> tuple[int, list[PoolFlow]]:
        if self.mode == "enum" and self.best_m is not None:
            start, m = 0, amount
            for i in range(len(path) - 1, 0, -1):
                hit = memo.get(path[:i])
                if hit is None:
                    continue
                if isinstance(hit, metis_inspired._Failure):
                    start = -1  # a memoized failure: the reference answers it for free
                else:
                    start, m = i, hit[0]
                break
            if start >= 0:
                self.gate.bound_evaluations += 1
                ub: int | None = m
                for edge in path[start:]:
                    ub = self._hop(edge, ub) if ub is not None else None
                if ub is not None and ub <= self.best_m:
                    self.gate.pruned_bound += 1
                    raise metis_inspired._ChunkFailed(metis_inspired._Failure("pruned_bound", 0))
        result = super()._marginal(path, amount, memo)
        if self.mode == "enum":
            self.best_m = result[0] if self.best_m is None else max(self.best_m, result[0])
        return result

    # --- metis_history: label relaxations
    def step(self, edge: Edge, m: int, depth: int) -> Any:
        if self.mode != "labels":
            return super().step(edge, m, depth)
        if self.best_m is not None and m > 0:  # a zero input makes no pool call: nothing to save
            self.gate.bound_evaluations += 1
            ub = self._relax_ub(edge, m, self.hops - depth)
            if ub is not None and ub <= self.best_m:
                return metis_inspired._Failure("pruned_bound", depth)
        result = super().step(edge, m, depth)
        if (
            not isinstance(result, metis_inspired._Failure)
            and edge.token_out == self.case.token_out
        ):
            self.best_m = result[0] if self.best_m is None else max(self.best_m, result[0])
        return result

    def _relax_ub(self, edge: Edge, m: int, hops_left: int) -> int | None:
        first = self._hop(edge, m)
        if first is None:
            return None
        if edge.token_out == self.case.token_out:  # M1: the relaxation is an arrival
            return first
        if self.table is None:  # M2 is off
            return None
        entry = self.table.entry(edge.token_out, hops_left)
        if entry is None:
            return None
        r, s = self.bounds.rate(edge), self.bounds.slack(edge)
        assert r is not None and s is not None
        a_ub = r * m + (s if self.use_slack else 0)
        x_max = max((f.amount_in for f in self.flows.values()), default=0)
        if x_max + max(Fraction(m), entry.p * a_ub + entry.t) > TWO127:
            self.gate.no_bound += 1
            return None
        return floor_frac(entry.r * a_ub + entry.s)


Trajectory = list[tuple[int, int, str, tuple[tuple[str, int, int], ...]]]


@dataclass
class ChunkRun:
    trajectory: Trajectory
    carried: int
    plan: RoutePlan | None
    evaluation: Evaluation | None
    status: str
    truncated_by: str | None
    alloc: metis_inspired._Allocator
    cache: QuoteCache
    work: metis_history.Work


def _flows_key(updates: Sequence[PoolFlow]) -> tuple[tuple[str, int, int], ...]:
    return tuple((u.edge.pool_id, u.amount_in, u.amount_out) for u in updates)


def run_chunks(
    bundle: SnapshotBundle,
    case: Case,
    *,
    chooser: str,
    hops: int,
    chunks: int,
    budget: Budget | None = None,
    bounds: Bounds | None = None,
    options: Mapping[str, Any] | None = None,
    m2: str = "auto",
    use_slack: bool = True,
    objective: Any = None,
) -> ChunkRun:
    """The chunk loop of `incremental_graph.solve` / `metis_history._solve` (carry, commit,
    `merged_plan`, the replay), driven through the shared `_Allocator` choosers.
    `chooser` is `"enum"` or `"labels"`; `bounds=None` is the unmodified reference.
    `m2` is `"auto"` (behind `m2_gate`), `"on"` (forced: a negative control) or `"off"`."""
    budget = budget or Budget()
    index = build_graph_index(bundle)
    cache_ = QuoteCache(bundle)
    source, target = case.token_in, case.token_out
    alloc_ref: list[metis_inspired._Allocator] = []

    def guarded(state: Any, token_in: str, amount: int) -> Any:
        pool_id: str = state.pool_id
        memo_hit = bundle.pools.get(pool_id) is state and cache_.cached(pool_id, token_in, amount)
        if budget.max_quotes is not None and cache_.misses >= budget.max_quotes and not memo_hit:
            alloc_ref[0].truncated_by = "max_quotes"
            raise metis_inspired._BudgetExhausted
        return cache_(state, token_in, amount)

    dist = metis_inspired.hops_to_target(index, source, target)
    opts = options or {
        "dominance": "history",
        "max_labels_per_signature": 1,
        "max_frontier_labels": 1024,
    }
    table: UTable | None = None
    if bounds is None:
        alloc: metis_inspired._Allocator = metis_inspired._Allocator(bundle, case, index, guarded)
    else:
        enabled = chooser == "labels" and (
            m2 == "on" or (m2 == "auto" and m2_gate(index, source, target, hops, dist, opts))
        )
        if enabled:
            table = UTable(index, source, target, hops, bounds)
        alloc = BoundedAllocator(
            bundle,
            case,
            index,
            guarded,
            bounds,
            mode=chooser,
            hops=hops,
            table=table,
            use_slack=use_slack,
        )
    alloc_ref.append(alloc)
    paths = list(enumerate_paths(index, source, target, hops)) if chooser == "enum" else []
    safe = metis_history.upward_safe_edges(bundle)
    work = metis_history.Work()
    region: dict[tuple[str, int], bool] = {}
    amounts = chunk_amounts(case.amount_in, chunks)
    trajectory: Trajectory = []
    carried = 0
    status = "ok"
    plan: RoutePlan | None = None
    evaluation: Evaluation | None = None
    try:
        last = max(k for k, a in enumerate(amounts) if a > 0)
        carry = 0
        for k, chunk in enumerate(amounts):
            if chunk == 0:
                continue
            amount = carry + chunk
            if isinstance(alloc, BoundedAllocator):
                alloc.best_m = None
            if chooser == "enum":
                choice = alloc.choose_enumeration(amount, paths, budget)
            else:
                drops = work.drops()
                choice = metis_history.choose_history(
                    alloc,
                    amount,
                    hops,
                    dist,
                    budget,
                    opts,
                    final=k == last,
                    safe=safe,
                    region=region,
                    work=work,
                )
                work.chunks_state_capped += work.drops() > drops
            if k != last and (choice is None or choice[0] == 0):
                carry, carried = amount, carried + 1
                trajectory.append((k, amount, "carried", ()))
                continue
            if choice is None:
                status = f"chunk_{k + 1}_no_admissible_path"
                break
            carry = 0
            m, path, updates = choice
            alloc.commit(path, updates)
            trajectory.append(
                (k, amount, f"{m}:" + ">".join(e.pool_id for e in path), _flows_key(updates))
            )
        else:
            plan = merged_plan(case, alloc.flows.values())
            evaluation = evaluate(bundle, case, plan, objective or gross_only(), quote=guarded)
            status = "ok" if evaluation.status is EvalStatus.OK else "invalid_plan"
    except metis_inspired._BudgetExhausted:
        status = "truncated"
    return ChunkRun(
        trajectory, carried, plan, evaluation, status, alloc.truncated_by, alloc, cache_, work
    )


def assert_same_run(ref: ChunkRun, out: ChunkRun) -> None:
    assert out.trajectory == ref.trajectory
    assert out.status == ref.status
    assert out.carried == ref.carried
    if ref.evaluation is not None:
        assert out.plan == ref.plan
        assert out.evaluation is not None
        assert out.evaluation.to_dict() == ref.evaluation.to_dict()


def pruned(run: ChunkRun) -> int:
    return run.alloc.gate.pruned_bound if isinstance(run.alloc, BoundedAllocator) else 0


# ======================================================================================
# 3. premise: the references never score a candidate against committed flow or a token cycle
# ======================================================================================


def record_quotes(
    bundle: SnapshotBundle,
    case: Case,
    *,
    chooser: str,
    hops: int,
    chunks: int,
) -> tuple[ChunkRun, list[tuple[str, str, dict[str, str]]]]:
    """Run the reference chunk loop recording, at every pool quote the search makes, the
    direction of the committed flow of every pool (pool -> token_in of its committed flow)."""
    log: list[tuple[str, str, dict[str, str]]] = []
    index = build_graph_index(bundle)
    cache_ = QuoteCache(bundle)
    holder: list[metis_inspired._Allocator] = []

    def spy(state: Any, token_in: str, amount: int) -> Any:
        flows = {pid: f.edge.token_in for pid, f in holder[0].flows.items()}
        log.append((state.pool_id, token_in, flows))
        return cache_(state, token_in, amount)

    alloc = metis_inspired._Allocator(bundle, case, index, spy)
    holder.append(alloc)
    dist = metis_inspired.hops_to_target(index, case.token_in, case.token_out)
    paths = list(enumerate_paths(index, case.token_in, case.token_out, hops))
    safe = metis_history.upward_safe_edges(bundle)
    amounts = chunk_amounts(case.amount_in, chunks)
    last = max(k for k, a in enumerate(amounts) if a > 0)
    carry = 0
    for k, chunk in enumerate(amounts):
        if chunk == 0:
            continue
        amount = carry + chunk
        if chooser == "enum":
            choice = alloc.choose_enumeration(amount, paths, Budget())
        else:
            choice = metis_history.choose_history(
                alloc,
                amount,
                hops,
                dist,
                Budget(),
                {
                    "dominance": "history",
                    "max_labels_per_signature": 1,
                    "max_frontier_labels": 1024,
                },
                final=k == last,
                safe=safe,
                region={},
                work=metis_history.Work(),
            )
        if k != last and (choice is None or choice[0] == 0):
            carry = amount
            continue
        if choice is None:
            break
        carry = 0
        alloc.commit(choice[1], choice[2])
    run = ChunkRun([], 0, None, None, "ok", None, alloc, cache_, metis_history.Work())
    return run, log


@pytest.mark.parametrize("chooser", ["enum", "labels"])
def test_reference_chunk_searches_never_quote_a_pool_against_its_committed_direction(
    chooser: str,
) -> None:
    """Contract §4.1: `creates_cycle` runs before every scored candidate, so a pool that already
    carries flow is only ever quoted in that flow's direction (a token-edge `a->b` committed
    makes `b->a` close a 2-cycle), and every quote is on the pool's ORIGINAL state (the
    cache's identity check) -- the same-direction / original-state premise of output-bounds §5.2."""
    rejected = quotes_on_loaded_pool = 0
    for name in ("cpmm_graph", "mantle_mixed", "corpus/bundle"):
        bundle = fixture(f"routing/{name}" if "/" not in name else name)
        for case in bundle.cases if name != "corpus/bundle" else corpus_cases(bundle):
            for hops, chunks in ((2, 8), (3, 12)):
                run, log = record_quotes(bundle, case, chooser=chooser, hops=hops, chunks=chunks)
                for pool_id, token_in, flows in log:
                    if pool_id in flows:
                        quotes_on_loaded_pool += 1
                        assert flows[pool_id] == token_in, (case.case_id, pool_id)
                rejected += run.alloc.rejected_cycle if chooser == "enum" else run.alloc.label_cycle
    assert quotes_on_loaded_pool > 0 and rejected > 0  # the premise was exercised, not vacuous


# ======================================================================================
# 4. incremental_graph: per-chunk path scoring
# ======================================================================================


def ig_reference(
    bundle: SnapshotBundle,
    case: Case,
    *,
    hops: int,
    chunks: int,
    budget: Budget | None = None,
    objective: Any = None,
) -> SolveResult:
    prepared = incremental_graph.prepare(
        bundle,
        AlgorithmConfig(
            incremental_graph.NAME,
            {"max_hops": hops, "max_splits": 2, "percent_step": 25, "chunks": chunks},
        ),
    )
    context = SolveContext(bundle=bundle, objective=objective or gross_only(), prepared=prepared)
    return incremental_graph.solve(case, context, budget or Budget())


def chunk_cases() -> Iterator[tuple[str, SnapshotBundle, Case]]:
    for name in ("cpmm_graph", "mantle_mixed"):
        bundle = fixture(f"routing/{name}")
        for case in bundle.cases:
            yield name, bundle, case
    corpus = fixture("corpus/bundle")
    for case in corpus_cases(corpus, per_pair=1):
        yield "corpus", corpus, case


def test_the_reference_driver_reproduces_incremental_graph() -> None:
    """The test-local driver is only trustworthy if it reproduces the actual solver."""
    n = 0
    for _, bundle, case in chunk_cases():
        for hops, chunks in ((2, 6), (3, 10)):
            drv = run_chunks(bundle, case, chooser="enum", hops=hops, chunks=chunks)
            sol = ig_reference(bundle, case, hops=hops, chunks=chunks)
            st = sol.search_stats
            assert st["incremental_status"] == drv.status or (
                st["incremental_status"] == "no_paths" and drv.status != "ok"
            )
            assert st["chunks_carried"] == drv.carried
            assert st["paths_scored"] == drv.alloc.scored
            assert st["paths_rejected_cycle"] == drv.alloc.rejected_cycle
            if drv.evaluation is not None:
                assert st["incremental_evaluated_gross"] == str(drv.evaluation.gross_output)
            n += 1
    assert n


def test_incremental_chunk_prune_equals_the_reference_and_is_not_vacuous() -> None:
    total_pruned = total_scored = carried = multi = no_bound = 0
    for name, bundle, case in chunk_cases():
        bounds = Bounds(bundle)
        for hops, chunks in ((2, 6), (3, 10), (3, 40)):
            ref = run_chunks(bundle, case, chooser="enum", hops=hops, chunks=chunks)
            out = run_chunks(bundle, case, chooser="enum", hops=hops, chunks=chunks, bounds=bounds)
            assert_same_run(ref, out)
            # counters that must be identical under the contract (§6.1)
            assert out.alloc.scored == ref.alloc.scored
            assert out.alloc.rejected_cycle == ref.alloc.rejected_cycle
            assert out.alloc.own_truncated == ref.alloc.own_truncated
            # a skipped candidate is never folded into the failure disclosures
            assert "pruned_bound" not in out.alloc.failures
            # work only shrinks
            assert out.cache.misses <= ref.cache.misses, (name, case.case_id)
            total_pruned += pruned(out)
            total_scored += out.alloc.scored
            assert isinstance(out.alloc, BoundedAllocator)
            no_bound += out.alloc.gate.no_bound
            carried += out.carried
            multi += len({t[2].split(":")[1] for t in out.trajectory if t[2] != "carried"}) > 1
    assert total_pruned > 0 and total_scored > 0
    assert no_bound > 0, "a pool without a bound (the dry pool) must have been met and not pruned"
    assert carried > 0, "a carried chunk must be exercised (carry is part of the contract)"
    assert multi > 0, "a split allocation (several distinct chunk paths) must be exercised"


def test_incremental_chunk_prune_is_objective_independent() -> None:
    """Chunk choices use gross marginals (incremental_graph module docstring); a net objective
    only enters the final comparison, which the pruned search leaves untouched."""
    n = 0
    for _, bundle, case in chunk_cases():
        bounds = Bounds(bundle)
        for objective in (synthetic_fixed_cost(10**6), ShapeCost({1: 5, 2: 40, 3: None})):
            ref = run_chunks(bundle, case, chooser="enum", hops=3, chunks=10, objective=objective)
            out = run_chunks(
                bundle, case, chooser="enum", hops=3, chunks=10, bounds=bounds, objective=objective
            )
            assert_same_run(ref, out)
            n += 1
    assert n


def _r2_bundle() -> tuple[SnapshotBundle, Case]:
    """R2 of output-bounds.md §3.3 on (1000, 1000) at 30 bps: q(2) = 1, q(3) = 2, so one more
    unit on top of 2 committed units has marginal 1 > r̄ * 1 = 0.997. `p_zero` is the other
    pool; 333 committed units there make the next unit's marginal exactly 0."""
    bundle = synthetic(cp("p_slack", "A", "B", 1000, 1000), cp("p_zero", "A", "B", 10**9, 10**9))
    return bundle, case_of("A", "B", 1)


def test_chunk_slack_is_required_a_bound_without_it_prunes_the_true_marginal() -> None:
    """The tempting-but-unsafe prune: `UB = floor(r̄ m)` with no integer slack. A chunk of one
    unit; `p_zero` (committed 333) is scored first with marginal 0 (the incumbent); `p_slack`
    (committed 2, q(2) = 1) has true marginal 1, but `floor(0.997 * 1) = 0 <= 0`."""
    bundle, case = _r2_bundle()
    index = build_graph_index(bundle)
    by_pool = {p[0].pool_id: p for p in enumerate_paths(index, "A", "B", 1)}

    def committed(alloc: metis_inspired._Allocator, pool: str, x: int) -> None:
        edge = by_pool[pool][0]
        out = quote_exact_in(bundle.pools[pool], "A", x)
        assert out.status is QuoteStatus.OK
        alloc.flows[pool] = PoolFlow(edge, x, out.amount_out, len(alloc.flows))
        alloc.token_edges.add(("A", "B"))

    def choose(bounds: Bounds | None, use_slack: bool = True) -> metis_inspired.Choice | None:
        cache_ = QuoteCache(bundle)
        if bounds is None:
            alloc: metis_inspired._Allocator = metis_inspired._Allocator(
                bundle, case, index, cache_
            )
        else:
            alloc = BoundedAllocator(
                bundle, case, index, cache_, bounds, mode="enum", use_slack=use_slack
            )
        committed(alloc, "p_zero", 333)
        committed(alloc, "p_slack", 2)
        return alloc.choose_enumeration(1, [by_pool["p_zero"], by_pool["p_slack"]], Budget())

    ref = choose(None)
    assert ref is not None and ref[0] == 1 and ref[1][0].pool_id == "p_slack"
    safe = choose(Bounds(bundle))
    assert safe == ref
    wrong = choose(Bounds(bundle), use_slack=False)
    assert wrong is not None and wrong[0] == 0 and wrong[1][0].pool_id == "p_zero"


def test_chunk_prune_with_an_underestimating_rate_is_caught() -> None:
    caught = 0
    for _, bundle, case in chunk_cases():
        ref = run_chunks(bundle, case, chooser="enum", hops=3, chunks=10)
        bad = run_chunks(
            bundle,
            case,
            chooser="enum",
            hops=3,
            chunks=10,
            bounds=Bounds(bundle, rate_scale=Fraction(1, 2)),
        )
        caught += bad.trajectory != ref.trajectory
    assert caught > 0


# ======================================================================================
# 5. metis_history: label relaxation (rules M1, M2) and dominance / cap side effects
# ======================================================================================

PRESET = {"dominance": "history", "max_labels_per_signature": 1, "max_frontier_labels": 1024}
GENEROUS = {
    "dominance": "history",
    "max_labels_per_signature": 1_000_000,
    "max_frontier_labels": 10_000_000,
}
OFF = {"dominance": "off", "max_labels_per_signature": 1, "max_frontier_labels": 10_000_000}
CAPPED_OFF = {"dominance": "off", "max_labels_per_signature": 1, "max_frontier_labels": 3}


def mh_reference(
    bundle: SnapshotBundle,
    case: Case,
    *,
    label_hops: int,
    chunks: int,
    options: Mapping[str, Any],
    budget: Budget | None = None,
) -> SolveResult:
    assert metis_history.FACTORY.prepare is not None
    params = {
        "max_hops": 2,
        "max_splits": 2,
        "percent_step": 25,
        "chunks": chunks,
        "label_hops": label_hops,
    }
    prepared = metis_history.FACTORY.prepare(
        bundle, AlgorithmConfig(metis_history.NAME, params, dict(options))
    )
    context = SolveContext(bundle=bundle, objective=gross_only(), prepared=prepared)
    return metis_history.FACTORY.solve(case, context, budget or Budget())


WORK_FIELDS = (
    "label_relaxations",
    "admission_checks",
    "state_comparisons",
    "labels_discarded_dominance",
    "labels_retained_unknown",
    "peak_frontier_labels",
    "labels_dropped_signature_cap",
    "labels_dropped_frontier_cap",
    "certified_strict_insertions",
    "peak_signature_labels",
    "chunks_state_capped",
)
ALLOC_FIELDS = ("relaxations", "label_cycle", "label_distance", "label_revisit", "label_truncated")


def test_the_reference_label_driver_reproduces_metis_history() -> None:
    n = 0
    for _, bundle, case in chunk_cases():
        for options in (PRESET, GENEROUS, OFF):
            drv = run_chunks(bundle, case, chooser="labels", hops=3, chunks=8, options=options)
            sol = mh_reference(bundle, case, label_hops=3, chunks=8, options=options)
            st = sol.search_stats
            assert st["label_relaxations"] == drv.alloc.relaxations
            assert st["chunks_carried"] == drv.carried
            assert st["chunks_state_capped"] == drv.work.chunks_state_capped
            if drv.evaluation is not None:
                assert st["incremental_evaluated_gross"] == str(drv.evaluation.gross_output)
            n += 1
    assert n


@pytest.mark.parametrize(
    "options",
    [PRESET, GENEROUS, OFF, CAPPED_OFF],
    ids=lambda o: str(o["dominance"]) + str(o["max_frontier_labels"]),
)
def test_arrival_prune_m1_is_population_neutral_under_every_dominance_and_cap_setting(
    options: Mapping[str, Any],
) -> None:
    """Rule M1 skips only a relaxation INTO the target: no label is created or removed, so
    dominance, signature caps, frontier caps and the relaxation / admission counters are the
    reference's exactly -- even on a chunk the reference itself caps."""
    total = 0
    capped = 0
    for _, bundle, case in chunk_cases():
        bounds = Bounds(bundle)
        for hops, chunks in ((3, 8), (3, 24)):
            ref = run_chunks(
                bundle, case, chooser="labels", hops=hops, chunks=chunks, options=options
            )
            out = run_chunks(
                bundle,
                case,
                chooser="labels",
                hops=hops,
                chunks=chunks,
                options=options,
                bounds=bounds,
                m2="off",
            )
            assert_same_run(ref, out)
            for f in WORK_FIELDS:
                assert getattr(out.work, f) == getattr(ref.work, f), f
            for f in ALLOC_FIELDS:
                assert getattr(out.alloc, f) == getattr(ref.alloc, f), f
            assert "pruned_bound" not in out.alloc.failures
            assert out.cache.misses <= ref.cache.misses
            total += pruned(out)
            capped += ref.work.chunks_state_capped > 0
    assert total > 0
    if options is PRESET or options is CAPPED_OFF:
        assert capped > 0  # the equality above was checked on capped chunks too


def chain_bundle() -> tuple[SnapshotBundle, Case]:
    """A -> B -> C -> D with one pool per pair: every (layer, token) is reached by a single
    walk, so no dominance event and no cap can occur (the history-mode M2 gate)."""
    bundle = synthetic(
        cp("ab", "A", "B", 10**9, 2 * 10**9),
        cp("bc", "B", "C", 3 * 10**9, 10**9),
        cp("cd", "C", "D", 10**9, 10**9),
        cp("ad", "A", "D", 10**8, 10**8),  # a poor direct pool (so there is something to prune)
    )
    return bundle, case_of("A", "D", 10**7)


def gate_of(bundle: SnapshotBundle, case: Case, hops: int, options: Mapping[str, Any]) -> bool:
    index = build_graph_index(bundle)
    dist = metis_inspired.hops_to_target(index, case.token_in, case.token_out)
    return m2_gate(index, case.token_in, case.token_out, hops, dist, options)


def test_m2_gate_opens_only_when_no_dominance_event_or_cap_can_occur() -> None:
    chain, chain_case = chain_bundle()
    assert gate_of(chain, chain_case, 3, PRESET)  # unique walk per (layer, token)
    assert gate_of(chain, chain_case, 3, OFF)
    mixed = fixture("routing/mantle_mixed")
    corpus = fixture("corpus/bundle")
    case = next(c for c in corpus.cases if c.case_id.startswith("nod-09bc4e-c96de2"))
    direct = next(c for c in corpus.cases if c.case_id.startswith("emp-09bc4e-779ded"))
    assert gate_of(corpus, direct, 3, PRESET)  # no non-target label exists: nothing for M2 to do
    assert not gate_of(mixed, mixed.cases[0], 3, PRESET)  # parallel pools => groups may hold >1
    assert not gate_of(corpus, case, 3, PRESET)  # the bounded preset keeps M2 closed
    assert not gate_of(corpus, case, 3, GENEROUS)  # history + several walks: not proved
    assert gate_of(corpus, case, 3, OFF)
    assert gate_of(mixed, mixed.cases[0], 3, OFF)  # no dominance, ample frontier
    graph = fixture("routing/cpmm_graph")
    multi = next(c for c in graph.cases if c.case_id == "a_d_multi_hop")
    assert not gate_of(graph, multi, 3, CAPPED_OFF)  # a frontier of 3 can bind


def test_label_prune_m2_is_exact_behind_its_gate_and_saves_quotes_beyond_m1() -> None:
    runs = beyond_m1 = 0
    candidates: list[tuple[SnapshotBundle, Case, Mapping[str, Any]]] = []
    chain, chain_case = chain_bundle()
    candidates += [(chain, chain_case, PRESET), (chain, chain_case, OFF)]
    candidates += [(b, c, OFF) for _, b, c in chunk_cases()]
    for bundle, case, options in candidates:
        assert gate_of(bundle, case, 3, options) or options is not OFF
        if not gate_of(bundle, case, 3, options):
            continue
        for chunks in (6, 24):
            ref = run_chunks(bundle, case, chooser="labels", hops=3, chunks=chunks, options=options)
            m1 = run_chunks(
                bundle,
                case,
                chooser="labels",
                hops=3,
                chunks=chunks,
                options=options,
                bounds=Bounds(bundle),
                m2="off",
            )
            m2 = run_chunks(
                bundle,
                case,
                chooser="labels",
                hops=3,
                chunks=chunks,
                options=options,
                bounds=Bounds(bundle),
                m2="auto",
            )
            assert_same_run(ref, m2)
            assert m2.cache.misses <= m1.cache.misses <= ref.cache.misses
            assert m2.alloc.relaxations <= ref.alloc.relaxations  # a subsequence of the reference's
            beyond_m1 += m2.cache.misses < m1.cache.misses
            runs += 1
    assert runs > 10 and beyond_m1 > 0


def test_label_prune_outside_the_gate_is_unsafe_when_a_frontier_cap_binds() -> None:
    """The tempting-but-unsafe prune: removing a label frees a frontier slot, so a label the
    reference refused (R7) now survives and wins. Reference chooses `ab_1>db_2` (862 for chunk 1);
    forced M2 finds the better `ac_1>cb_2>db_2` (902) because it never filled the frontier --
    different from the reference, so M2 is gated off whenever a cap can bind."""
    graph = fixture("routing/cpmm_graph")
    case = next(c for c in graph.cases if c.case_id == "a_d_multi_hop")
    kw: dict[str, Any] = {"chooser": "labels", "hops": 3, "chunks": 24, "options": CAPPED_OFF}
    ref = run_chunks(graph, case, **kw)
    assert ref.work.labels_dropped_frontier_cap > 0  # the cap really binds in the reference
    forced = run_chunks(graph, case, bounds=Bounds(graph), m2="on", **kw)
    assert forced.trajectory != ref.trajectory
    assert ref.evaluation is not None and forced.evaluation is not None
    assert forced.evaluation.gross_output != ref.evaluation.gross_output
    # the contract's rules: M1 alone (population neutral) and M2 behind the gate are exact
    assert_same_run(ref, run_chunks(graph, case, bounds=Bounds(graph), m2="off", **kw))
    assert_same_run(ref, run_chunks(graph, case, bounds=Bounds(graph), m2="auto", **kw))


def brute_walks(
    index: GraphIndex, source: str, target: str, v: str, h: int, bounds: Bounds
) -> tuple[Fraction, Fraction] | None:
    """Max product `r` and max slack-chain `s_path` over every walk of at most `h` edges from
    `v` to the target (any token may repeat; never into the source, never out of the target)."""
    best_r: Fraction | None = None
    best_s: Fraction | None = None

    def go(t: str, left: int, hops_so_far: list[tuple[Fraction, Fraction]]) -> None:
        nonlocal best_r, best_s
        if t == target:
            slack, tail = Fraction(0), Fraction(1)
            for r_k, s_k in reversed(hops_so_far):
                slack += s_k * tail
                tail *= r_k
            best_r = tail if best_r is None else max(best_r, tail)
            best_s = slack if best_s is None else max(best_s, slack)
            return
        if left == 0:
            return
        for e in index.edges_from(t):
            if e.token_out == source:
                continue
            r_e, s_e = bounds.rate(e), bounds.slack(e)
            assert r_e is not None and s_e is not None
            go(e.token_out, left - 1, [*hops_so_far, (r_e, s_e)])

    go(v, h, [])
    return None if best_r is None or best_s is None else (best_r, best_s)


def test_u_table_matches_a_brute_force_walk_maximum() -> None:
    """`U_h(v).r` is the exact maximum product over relaxed walks; `.s` dominates every walk's
    `s_path` (it maximizes the rate and the slack separately, hence only `>=`)."""
    bundles = [fixture("routing/cpmm_graph"), fixture("routing/mantle_mixed")]
    checked = 0
    for bundle in bundles:
        bounds = Bounds(bundle)
        index = build_graph_index(bundle)
        for case in bundle.cases:
            if case.token_in == case.token_out:
                continue
            try:
                table = UTable(index, case.token_in, case.token_out, 3, bounds)
            except AssertionError:
                continue
            for v in index.adjacency:
                if v in (case.token_in, case.token_out):
                    continue
                for h in range(1, 3):
                    entry = table.entry(v, h)
                    if entry is None:
                        continue  # a bound-less edge: no walk maximum is defined
                    try:
                        brute = brute_walks(index, case.token_in, case.token_out, v, h, bounds)
                    except AssertionError:
                        continue  # some edge off the table's walks has no bound
                    if brute is None:
                        assert not entry.reach
                        continue
                    assert entry.r == brute[0], (case.case_id, v, h)
                    assert entry.s >= brute[1], (case.case_id, v, h)
                    checked += 1
    assert checked > 0


def check_hop_bounds_dominate(bounds_of: Callable[[SnapshotBundle], Bounds]) -> int:
    total = 0
    for name, bundle, case in chunk_cases():
        bounds = bounds_of(bundle)
        index = build_graph_index(bundle)
        hops = 3
        table = UTable(index, case.token_in, case.token_out, hops, bounds)
        for chunks, divisor in ((5, 7), (12, 3), (3, 2)):
            committed = run_chunks(bundle, case, chooser="enum", hops=hops, chunks=chunks)
            amount = max(1, case.amount_in // divisor)
            alloc = BoundedAllocator(
                bundle,
                case,
                index,
                QuoteCache(bundle),
                bounds,
                mode="labels",
                hops=hops,
                table=table,
            )
            alloc.flows = dict(committed.alloc.flows)
            alloc.token_edges = set(committed.alloc.token_edges)
            for path in enumerate_paths(index, case.token_in, case.token_out, hops):
                if incremental_graph.creates_cycle(alloc.token_edges, path):
                    continue
                memo: dict[RoutePath, Any] = {}
                try:
                    final, _ = metis_inspired._Allocator._marginal(alloc, path, amount, memo)
                except metis_inspired._ChunkFailed:
                    continue
                for j in range(len(path)):
                    a_j = amount if j == 0 else memo[path[:j]][0]
                    ub = alloc._relax_ub(path[j], a_j, hops - (j + 1))
                    if ub is not None:
                        assert final <= ub, (name, case.case_id, [e.pool_id for e in path], j)
                        total += 1
    return total


def test_u_table_and_hop_bounds_dominate_every_real_path_marginal_on_a_committed_state() -> None:
    """Soundness of rules I1 / M1 / M2 against the exact quote seam: after real chunks are
    committed, for every admitted simple path and every prefix, the path's true marginal is at
    most the bound computed from that prefix's exact marginal (no bound = no claim)."""
    assert check_hop_bounds_dominate(Bounds) > 200


def test_the_dominance_check_catches_an_underestimating_rate_or_slack() -> None:
    for bad in (
        lambda b: Bounds(b, rate_scale=Fraction(9, 10)),
        lambda b: Bounds(b, slack_scale=Fraction(0)),
    ):
        with pytest.raises(AssertionError):
            check_hop_bounds_dominate(bad)


def test_chunk_search_budget_labels() -> None:
    """Contract §6.2 for the chunk searches: a bounded run that truncates implies the reference
    truncates; an untruncated bounded run equals the unbudgeted reference; and when the
    reference is untruncated the bounded run equals it."""
    checked = binding = free = 0
    chain, chain_case = chain_bundle()
    cases = [(chain, chain_case)] + [(b, c) for _, b, c in chunk_cases()]
    for bundle, case in cases:
        bounds = Bounds(bundle)
        for chooser, options in (("enum", PRESET), ("labels", PRESET), ("labels", OFF)):
            kw: dict[str, Any] = {"chooser": chooser, "hops": 3, "chunks": 6, "options": options}
            unbudgeted = run_chunks(bundle, case, **kw)
            for budget in (
                *(Budget(max_candidates=n) for n in (1, 2, 4, 8, 16)),
                *(Budget(max_quotes=n) for n in (1, 3, 6, 12, 24, 48)),
            ):
                ref = run_chunks(bundle, case, budget=budget, **kw)
                out = run_chunks(bundle, case, budget=budget, bounds=bounds, **kw)
                checked += 1
                if out.truncated_by is not None:
                    binding += 1
                    assert ref.truncated_by is not None  # B truncates => R truncates
                else:
                    assert_same_run(unbudgeted, out)  # B untruncated == the unbudgeted reference
                if ref.truncated_by is None:
                    free += 1
                    assert out.truncated_by is None
                    assert_same_run(ref, out)
    assert checked and binding and free


def test_a_hop_outside_the_slack_domain_has_no_bound() -> None:
    """output-bounds.md §5.3: the chunk claim needs x + m <= 2**127; beyond it the bound is
    `None` (never prune), whatever the rate is."""
    pool = cp("big", "A", "B", 1 << 140, 1 << 140)
    bundle = synthetic(pool)
    alloc = BoundedAllocator(
        bundle,
        case_of("A", "B", 1),
        build_graph_index(bundle),
        QuoteCache(bundle),
        Bounds(bundle),
        mode="enum",
    )
    edge = Edge("big", "A", "B")
    assert alloc._hop(edge, TWO127) is not None
    assert alloc._hop(edge, TWO127 + 1) is None
    alloc.flows["big"] = PoolFlow(edge, TWO127, 1, 0)  # committed aggregate counts too
    assert alloc._hop(edge, 1) is None


def test_recomputing_the_bound_from_the_swapped_state_is_unsafe_for_the_aggregate_marginal() -> (
    None
):
    """The chunk searches charge `f(x + m) - f(x)` on the pool's ORIGINAL state (merged
    execution). The bound of a sequentially swapped state (`new_state`) bounds a different
    function and is NOT an upper bound of that marginal -- output-bounds.md §7 item 7's
    `recompute (never larger)` is not usable here. The original-state bound is."""
    pool = cp("p", "A", "B", 10**8, 10**8)
    x, m = 10**8, 10**5
    first = quote_exact_in(pool, "A", x)
    assert first.status is QuoteStatus.OK and first.new_state is not None
    aggregate = quote_exact_in(pool, "A", x + m).amount_out - first.amount_out
    r0, s0 = OB.output_rate(pool, "A"), OB.chunk_slack(pool, "A")
    r1, s1 = OB.output_rate(first.new_state, "A"), OB.chunk_slack(first.new_state, "A")
    assert aggregate <= floor_frac(r0 * m + s0)  # the original-state bound holds
    assert aggregate > floor_frac(r1 * m + s1)  # the swapped-state bound does not


def random_cpmm_graph(rng: random.Random, tokens: str, pools: tuple[int, int]) -> SnapshotBundle:
    out: list[ConstantProductPoolState] = []
    for i in range(rng.randint(*pools)):
        a, b = rng.sample(tokens, 2)
        scale = rng.choice([10**6, 10**8, 10**10])
        out.append(
            cp(
                f"p{i}",
                a,
                b,
                int(scale * rng.uniform(0.3, 3)),
                int(scale * rng.uniform(0.3, 3)),
                rng.choice([5, 30, 100]),
            )
        )
    return synthetic(*out)


def test_single_path_budget_labels_on_random_graphs() -> None:
    """3 000 seeded random (graph, budget) cells: an untruncated bounded run equals the
    unbudgeted reference, and no cell shows the bounded run truncating where the reference
    does not (the converse of Lemma B holds on every drawn cell; it is not proved in general)."""
    rng = random.Random(7)
    cells = binding = 0
    for _ in range(1500):
        bundle = random_cpmm_graph(rng, "ABCDEF", (8, 16))
        case = case_of("A", "F", rng.choice([10**4, 10**6, 10**7]))
        free = sp_reference(bundle, case, max_hops=3)
        bounds = Bounds(bundle)
        for budget in (
            Budget(max_quotes=rng.randint(1, 30)),
            Budget(max_candidates=rng.randint(1, 10)),
        ):
            ref = sp_reference(bundle, case, max_hops=3, budget=budget)
            out = run_single_path(bundle, case, max_hops=3, budget=budget, bounds=bounds)
            cells += 1
            if out.stats["truncated_by"] is not None:
                binding += 1
                assert ref.search_stats["truncated_by"] is not None
            else:
                assert_sp_identical(free, out)
            if ref.search_stats["truncated_by"] is None:
                assert_sp_identical(ref, out)
    assert cells == 3000 and binding > 300


def test_history_mode_m2_has_no_counterexample_on_random_cpmm_graphs_evidence_not_proof() -> None:
    """Evidence only: forcing M2 under `dominance: history` (outside `G_M2`) matched the
    reference on every random CPMM graph drawn. This is NOT a proof and the contract does not
    specify that case; a failure here would be a finding, not a regression."""
    rng = random.Random(2026)
    runs = 0
    for _ in range(600):
        bundle = random_cpmm_graph(rng, "ABCDE", (7, 11))
        case = case_of("A", "E", rng.choice([10**5, 10**6, 5 * 10**6]))
        for options in (GENEROUS, PRESET):
            chunks = rng.choice([3, 6, 12])
            kw: dict[str, Any] = {
                "chooser": "labels",
                "hops": 3,
                "chunks": chunks,
                "options": options,
            }
            ref = run_chunks(bundle, case, **kw)
            forced = run_chunks(bundle, case, bounds=Bounds(bundle), m2="on", **kw)
            assert_same_run(ref, forced)
            runs += 1
    assert runs == 1200


FROZEN_TERMS = (
    "R022-Q02/1",
    "single_path_bounded",
    "incremental_graph_bounded",
    "metis_history_bounded",
    "bound_pruning: bool = False",
    "pruned_bound",
    "bound_evaluations",
    "bound_no_bound",
    "bound_table_cost",
    "not_exact_budget_binding",
    "G_M2",
    "R022_ADDITIONS",
    "Quiet-host gate",
    "0.5 ×",
)


def test_the_contract_document_pins_the_frozen_interface_and_names_real_tests() -> None:
    text = DOC.read_text()
    for term in FROZEN_TERMS:
        assert term in text, term
    own = Path(__file__).read_text()
    for token in text.split("`"):
        if token.startswith("test_") and token.endswith("…"):
            continue
        if token.startswith("test_") and " " not in token:
            assert f"def {token}" in own, token

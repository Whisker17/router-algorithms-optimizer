# SPDX-License-Identifier: GPL-3.0-only
#
# Thin named adapters over routing/algorithms/uni_sor_fast.py (itself built on the GPL-3.0
# translation of Uniswap/smart-order-router@04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647;
# docs/references/licenses/uniswap-smart-order-router-04c7c0b4-LICENSE.txt). No routing
# logic lives here. Private internal research use; not conveyed.
"""The two named **optimized strategies** (WHI-1528): fixed, registered recipes of the
opt-in `uni_sor_fast` heuristic, compared next to the six base strategies by the CLI.

- `uni_sor_adaptive` -- recipe: arm **H3** (L07 adaptive-only nomination): near-full
  candidates (vacuous shortlist) + adaptive percentage sampling; no exact controls.
- `uni_sor_optimized` -- recipe: arm **H4** (H2 + L02-L04 composition): L06 shortlist +
  adaptive sampling with the L02, L03 and L04 exact quote controls (no L05).

Both recipes are registered in config/latency/l08.yaml v1 (pinned by sha256).

What a strategy is:

- **The same solver.** `solve` calls the unchanged `uni_sor_fast.solve` with the
  `uni_sor_fast.prepare` result of the strategy's settings; nothing is re-implemented. The
  result is re-labelled with the strategy's own name (the runner refuses any other name),
  and its search metadata gains a `strategy` record (name, group, recipe, controls).
- **Its settings are the recipe's, not a default.** Every numeric setting comes from the
  frozen, sha256-pinned L08 arms file through the validated profile (`strategies.<name>`,
  checked against the arm by `benchmark.profile`); the caller's objective, budget and
  `search.*` values are the profile's own, shared with the base strategies. `prepare`
  re-checks the settings and the exact control set, since it can be called without a profile.
- **Per-solve controls.** `uni_sor_optimized` installs its L02-L04 quote controls with the
  shared `pools.exact_controls` installer: fresh instances built inside the timed solve,
  reference kernels restored in a `finally` (after errors too), never visible to the
  runner's independent evaluation (another process) or to any base strategy's worker.
- **Honest scope.** Both are experimental heuristics: `uni_sor_port`'s V2/V3 capabilities
  (LB-only cases are `unsupported`), a declared search approximation, and no parity,
  adoption, default-router or loss-tolerance claim. WHI-1510's L08 dispositions for these
  recipes apply to their recorded scope only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from pools import exact_controls
from routing.algorithms import uni_sor_fast as fast
from routing.algorithms import uni_sor_port as sor
from routing.algorithms.base import (
    AlgorithmConfig,
    AlgorithmFactory,
    Budget,
    SolveContext,
    SolveResult,
)
from snapshot.models import Case, SnapshotBundle

ADAPTIVE = "uni_sor_adaptive"
OPTIMIZED = "uni_sor_optimized"
GROUP = "optimized"
# The frozen arms file the recipes are registered in (its v1 bytes are pinned by sha256).
RECIPE_PATH = "config/latency/l08.yaml"
RECIPE_SHA256 = "e7add86add573033f04c6790f705cb2951712ffc272a4498d03f01251101beaa"
RECIPE_KEY = "L08"
RECIPE_VERSION = 1
CONTROLS_PARAM = "controls"
STRATEGY_STATS_KEY = "strategy"


class UniSorStrategyError(ValueError):
    """`prepare` received settings that are not a complete, valid recipe of the strategy."""


@dataclass(frozen=True)
class Strategy:
    name: str
    arm: str
    controls: tuple[str, ...]  # exact quote controls installed around every solve
    summary: str

    @property
    def recipe(self) -> dict[str, Any]:
        return {
            "path": RECIPE_PATH,
            "sha256": RECIPE_SHA256,
            "key": RECIPE_KEY,
            "version": RECIPE_VERSION,
            "arm": self.arm,
        }

    @property
    def quote_path(self) -> str:
        if not self.controls:
            return fast.QUOTE_PATH
        return (
            f"{self.name}: explicit exact controls {', '.join(self.controls)} "
            f"({RECIPE_KEY} arm {self.arm} settings) installed in the worker around each "
            "solve only, fresh per solve; the independent evaluation uses the reference path"
        )


STRATEGIES: Mapping[str, Strategy] = MappingProxyType(
    {
        ADAPTIVE: Strategy(
            ADAPTIVE,
            "H3",
            (),
            "L07 adaptive-only nomination: near-full candidates (vacuous shortlist, 10^6 "
            "routes per coarse probe) + adaptive coarse-to-fine percentage sampling",
        ),
        OPTIMIZED: Strategy(
            OPTIMIZED,
            "H4",
            ("L02", "L03", "L04"),
            "H2 composition: L06 shortlist + L07 adaptive sampling with the L02-L04 exact "
            "quote controls (no L05)",
        ),
    }
)


def _provenance(strategy: Strategy) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            "experimental": True,
            "strategy_group": GROUP,
            "strategy": strategy.name,
            "recipe": strategy.recipe,
            "recipe_summary": strategy.summary,
            "implementation": (
                "the unchanged uni_sor_fast solve (routing/algorithms/uni_sor_fast.py) with "
                "the recipe's settings; not a separate solver"
            ),
            "reference": sor.NAME,
            "search_approximation": fast.SEARCH_APPROXIMATION,
            "sampling_approximation": fast.SAMPLING_APPROXIMATION,
            "exact_controls": list(strategy.controls),
            "quote_path": strategy.quote_path,
            "routing_core": {
                "module": "routing/algorithms/uni_sor_port.py",
                "upstream": dict(sor.UPSTREAM),
                "contract": sor.CONTRACT,
            },
            "adoption": (
                "comparison only: not a default router, not adopted, no parity claim and no "
                "accepted loss tolerance (an L08 heuristic disposition is at most opt-in only)"
            ),
            "evidence": [
                "tests/routing/test_uni_sor_strategies.py",
                "docs/references/strategy-groups.md",
                "docs/references/latency-optimization-results.md",
            ],
        }
    )


@dataclass(frozen=True)
class PreparedStrategy:
    """`uni_sor_fast`'s preparation of the recipe settings plus the validated controls."""

    fast: fast.PreparedUniSorFast
    controls: Mapping[str, Mapping[str, Any]]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _controls(strategy: Strategy, raw: Any) -> dict[str, dict[str, Any]]:
    where = f"{strategy.name}: controls"
    if not isinstance(raw, Mapping) or tuple(sorted(raw)) != strategy.controls:
        raise UniSorStrategyError(
            f"{where} must be exactly {list(strategy.controls)} (recipe {RECIPE_KEY} "
            f"{strategy.arm}), got {raw!r}"
        )
    out: dict[str, dict[str, Any]] = {}
    for name in strategy.controls:
        settings = raw[name]
        keys = exact_controls.QUOTE_CONTROL_KEYS[name]
        if not isinstance(settings, Mapping) or set(settings) != keys:
            raise UniSorStrategyError(f"{where}.{name} needs exactly {sorted(keys)}")
        for key, value in settings.items():
            ok = (
                value is True
                if key == "skip_empty_spans"
                else value == "per_solve"
                if key == "lifetime"
                else _is_int(value) and value >= 1
            )
            if not ok:
                raise UniSorStrategyError(f"{where}.{name}.{key}: invalid value {value!r}")
        out[name] = dict(settings)
    return out


def _prepare(strategy: Strategy, bundle: SnapshotBundle, config: AlgorithmConfig) -> Any:
    params = config.params
    needed = (*sor.SEARCH_PARAMS, *fast.SHORTLIST_PARAMS, *fast.SAMPLING_PARAMS, CONTROLS_PARAM)
    missing = [k for k in needed if k not in params]
    if missing:
        raise UniSorStrategyError(
            f"{strategy.name}: missing recipe settings {missing} (no default; declare "
            f"strategies.{strategy.name} from {RECIPE_PATH} arm {strategy.arm})"
        )
    controls = _controls(strategy, params[CONTROLS_PARAM])
    settings = {k: params[k] for k in needed if k != CONTROLS_PARAM}
    try:
        prepared = fast.prepare(bundle, AlgorithmConfig(fast.NAME, settings))
    except fast.UniSorFastConfigError as exc:
        raise UniSorStrategyError(f"{strategy.name}: {exc}") from exc
    if prepared.sampling is None:  # unreachable: fast.prepare got every sampling key
        raise UniSorStrategyError(f"{strategy.name}: adaptive sampling settings are required")
    return PreparedStrategy(prepared, MappingProxyType(controls))


def _solve(strategy: Strategy, case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    prepared = context.prepared
    if not isinstance(prepared, PreparedStrategy):
        raise TypeError(f"{strategy.name}.solve needs the PreparedStrategy returned by prepare()")
    inner = replace(context, prepared=prepared.fast)
    quote_controls = (
        exact_controls.QuoteControls.fresh(prepared.controls) if strategy.controls else None
    )
    if quote_controls is None:
        result = fast.solve(case, inner, budget)
    else:
        with exact_controls.installed(quote_controls):
            result = fast.solve(case, inner, budget)
    search = dict(result.search_stats)
    inner_stats = search.get("sor_fast")
    if quote_controls is not None and isinstance(inner_stats, Mapping):
        search["sor_fast"] = {**inner_stats, "quote_path": strategy.quote_path}
    search[STRATEGY_STATS_KEY] = {
        "name": strategy.name,
        "group": GROUP,
        "implementation": fast.NAME,
        "recipe": strategy.recipe,
        "controls": list(strategy.controls),
        "lifetime": "per_solve",
        **(quote_controls.stats() if quote_controls is not None else {}),
    }
    return replace(result, algorithm=strategy.name, search_stats=search)


# Module-level functions (pickled by reference into spawned workers; docs: base.py).
def prepare_adaptive(bundle: SnapshotBundle, config: AlgorithmConfig) -> Any:
    return _prepare(STRATEGIES[ADAPTIVE], bundle, config)


def solve_adaptive(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    return _solve(STRATEGIES[ADAPTIVE], case, context, budget)


def prepare_optimized(bundle: SnapshotBundle, config: AlgorithmConfig) -> Any:
    return _prepare(STRATEGIES[OPTIMIZED], bundle, config)


def solve_optimized(case: Case, context: SolveContext, budget: Budget) -> SolveResult:
    return _solve(STRATEGIES[OPTIMIZED], case, context, budget)


def _factory(strategy: Strategy, prepare: Any, solve: Any) -> AlgorithmFactory:
    return AlgorithmFactory(
        name=strategy.name,
        solve=solve,
        prepare=prepare,
        capabilities=sor.CAPABILITIES,
        search_params=sor.SEARCH_PARAMS,
        provenance=_provenance(strategy),
        strategy_recipe=MappingProxyType(strategy.recipe),
    )


ADAPTIVE_FACTORY = _factory(STRATEGIES[ADAPTIVE], prepare_adaptive, solve_adaptive)
OPTIMIZED_FACTORY = _factory(STRATEGIES[OPTIMIZED], prepare_optimized, solve_optimized)

"""`main.py run|quote --strategies all|base|optimized|profile` (WHI-1528): the effective profile
the CLI runs, derived from an unchanged source profile.

The strategies are compared in two groups (`benchmark.profile.strategy_group`):

- **base** -- the six mandatory algorithms (`routing.algorithms.registry.BASE_STRATEGIES`);
- **optimized** -- the named optimized strategies `uni_sor_adaptive` (L08 arm H3) and
  `uni_sor_optimized` (L08 arm H4) (`OPTIMIZED_STRATEGIES`), with their registered recipes:
  experimental heuristics over the `uni_sor_port` routing core, never its replacement;
- any other algorithm a profile selects (e.g. the configurable `uni_sor_fast`) is **custom**.

Modes (`all` is the CLI default):

- `all`: the source's base and custom algorithms (source order), then every optimized
  strategy (an already listed one is not repeated);
- `base`: the source's base algorithms only;
- `optimized`: the optimized strategies only;
- `profile`: the source profile exactly as written (no derivation, no `selection` record).

Base strategies always run before optimized ones (the runner is sequential, one isolated
worker per algorithm), under the source's objective, budget, measurement, worker and
`search.*` values. An optimized strategy's `strategies.<name>` entry is the source's own when
it declares one (validated against the recipe like any other), otherwise the recipe's from
the frozen arms file. The derived document carries a `selection` record (mode, source path
and sha256, groups) and is validated in full before the caller writes anything or starts a
worker: an empty selection, a source without the `search.*` keys the optimized strategies
need or with a grid their recipes cannot use is refused with the alternatives. Nothing here
edits a source profile; the caller saves the effective document and replays it with
`--strategies profile`, so a replay never re-expands against later defaults.
"""

from __future__ import annotations

import json
from typing import Any

from benchmark.profile import (
    GROUPS,
    ProfileError,
    RunProfile,
    parse_profile,
    strategy_entry,
    strategy_group,
)
from routing.algorithms.registry import OPTIMIZED_STRATEGIES

MODES = ("all", "base", "optimized", "profile")
DEFAULT_MODE = "all"
GROUP_TITLES = {
    "base": "Base strategies",
    "optimized": "Optimized strategies",
    "custom": "Other profile-selected strategies",
}


class StrategySelectionError(ProfileError):
    """The requested strategy selection cannot be derived from the source profile."""


def effective_document(
    source: dict[str, Any], mode: str, *, source_path: str, source_sha256: str
) -> dict[str, Any]:
    """The effective profile document of `mode` (see module docstring). The source must
    itself be a valid profile; the result is validated too (`derive`)."""
    if mode not in MODES:
        raise StrategySelectionError(f"--strategies: expected one of {list(MODES)}, got {mode!r}")
    parse_profile(source, source_path)  # a malformed source is refused as such
    document: dict[str, Any] = json.loads(json.dumps(source))
    if mode == "profile":
        return document
    listed = [str(a) for a in source["algorithms"]]
    keep = {"all": ("base", "custom"), "base": ("base",), "optimized": ()}[mode]
    selected = [a for a in listed if strategy_group(a) in keep]
    optimized = [] if mode == "base" else list(OPTIMIZED_STRATEGIES)
    selected += optimized
    if not selected:
        raise StrategySelectionError(
            f"--strategies {mode}: {source_path} selects no base strategy (algorithms "
            f"{listed}); use --strategies all, optimized or profile"
        )
    declared = source.get("strategies") or {}
    document["algorithms"] = selected
    document.pop("strategies", None)
    if optimized:
        document["strategies"] = {
            name: json.loads(json.dumps(declared[name])) if name in declared
            else strategy_entry(name)
            for name in optimized
        }  # fmt: skip
    document["selection"] = {
        "mode": mode,
        "source_profile": {"path": source_path, "sha256": source_sha256},
        "groups": {g: [a for a in selected if strategy_group(a) == g] for g in GROUPS},
    }
    return document


def derive(
    source: dict[str, Any], mode: str, *, source_path: str, source_sha256: str
) -> tuple[dict[str, Any], RunProfile]:
    """`effective_document` plus its full validation, before any write or worker. A
    derivation the optimized recipes cannot run on is refused with the alternatives."""
    document = effective_document(
        source, mode, source_path=source_path, source_sha256=source_sha256
    )
    try:
        return document, parse_profile(document, source_path)
    except ProfileError as exc:
        raise StrategySelectionError(
            f"--strategies {mode}: {source_path} cannot run the optimized strategies: {exc}. "
            "They share the profile's objective, budget and search.max_hops/max_splits/"
            "percent_step and need a percent_step grid containing their recipe percents "
            "(e.g. config/daily_gross.yaml); use --strategies base for the base group only, "
            "or --strategies profile for the profile's exact selection"
        ) from exc


def selected_groups(profile: RunProfile) -> list[tuple[str, list[str]]]:
    """(group, algorithms) in run order, from the persisted selection record or, without
    one (`--strategies profile`), from the static classification of the algorithms."""
    groups = profile.selection.get("groups") if profile.selection else None
    if groups is None:
        groups = {g: [a for a in profile.algorithms if strategy_group(a) == g] for g in GROUPS}
    return [(g, list(groups[g])) for g in GROUPS if groups.get(g)]


def announce(profile: RunProfile, mode: str) -> str:
    parts = [f"{GROUP_TITLES[g]} ({len(m)}): {', '.join(m)}" for g, m in selected_groups(profile)]
    return f"strategies: {mode} -- " + "; ".join(parts)


__all__ = [
    "DEFAULT_MODE",
    "GROUP_TITLES",
    "MODES",
    "StrategySelectionError",
    "announce",
    "derive",
    "effective_document",
    "selected_groups",
]

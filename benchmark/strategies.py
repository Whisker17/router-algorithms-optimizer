"""`main.py run|quote --strategies all|base|optimized|profile` (WHI-1528, WHI-1540): the
effective profile the CLI runs, derived from an unchanged source profile.

The strategies are compared in groups (`benchmark.profile.strategy_group`):

- **base** -- the six mandatory algorithms (`routing.algorithms.registry.BASE_STRATEGIES`);
- **optimized** -- the named optimized strategies `uni_sor_adaptive` (L08 arm H3) and
  `uni_sor_optimized` (L08 arm H4) (`OPTIMIZED_STRATEGIES`), with their registered recipes:
  experimental heuristics over the `uni_sor_port` routing core, never its replacement;
- any other algorithm a profile selects (e.g. the configurable `uni_sor_fast`) is **custom**,
  and so is the experimental Metis-inspired (NOT Jupiter Metis) `metis_inspired` (WHI-1449),
  which `all` adds to the comparison (WHI-1540).

Modes (`all` is the CLI default):

- `all`: the source's base and custom algorithms (source order, so a listed
  `metis_inspired` keeps its place), then every optimized strategy, then `metis_inspired`
  when the source does not list it (an already listed one is not repeated). A source that is
  itself an `all` effective profile (`selection.mode: all`) also keeps its listed optimized
  strategies in place, so deriving `all` again changes nothing;
- `base`: the source's base algorithms only;
- `optimized`: the optimized strategies only;
- `profile`: the source profile exactly as written (no derivation, no `selection` record).

Every selected algorithm runs sequentially (one isolated worker per algorithm), under the
source's objective, budget, measurement, worker and `search.*` values. An optimized strategy's
`strategies.<name>` entry is the source's own when it declares one (validated against the
recipe like any other), otherwise the recipe's from the frozen arms file. Under `all`, a
`graph.label_hops`, `graph.label_pruning` or `graph.chunks` the source does not declare is
taken from the registered WHI-1449 arm M4 (`METIS_SETTINGS`, sha256-pinned); a declared
value always wins (e.g. daily's `chunks: 200`), and nothing is re-tuned: `label_hops` below
the source's `search.max_hops` is refused, not lowered. This is a new profile-derived
comparison, not a rerun of the frozen M4 arm (whose other values differ). The derived
document carries a `selection` record (mode, source path and sha256, groups) and is
validated in full before the caller writes anything or starts a worker: an empty selection,
a source without the `search.*` keys the added strategies need or with a grid their recipes
cannot use is refused with the alternatives. Nothing here edits a source profile; the caller
saves the effective document and replays it with `--strategies profile`, so a replay never
re-expands against later defaults.

`algorithm_options` (WHI-1548, R021-C/1 §2, §9.3): under `all`, every implemented 0.2.1
identity of `R021_ADDITIONS` (contract order; so far `metis_history`, WHI-1550,
`direct_split_certified`, WHI-1552, `incremental_graph_repair`, WHI-1554,
`uni_sor_cycle_safe`, WHI-1556, then `cfmm_dual`, WHI-1558) is appended once after
`metis_inspired` unless the source lists it. A derived document keeps the source's declared
entry of every selected algorithm (a declared entry wins; a source that lists an options algorithm
must itself declare its entry, like any other required setting), writes out the sha256-pinned
preset (`benchmark.profile.preset_options`) of an added algorithm the source does not configure,
and drops entries of unselected algorithms; the section is omitted when empty, so a source
without options derives exactly as before.
`profile` mode copies it as is.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from benchmark.profile import (
    GROUPS,
    ProfileError,
    RunProfile,
    parse_profile,
    preset_options,
    strategy_entry,
    strategy_group,
)
from routing.algorithms import (
    cfmm_dual,
    direct_split_certified,
    incremental_graph_repair,
    metis_history,
    metis_inspired,
    uni_sor_cycle_safe,
)
from routing.algorithms.registry import ALGORITHMS, OPTIMIZED_STRATEGIES

MODES = ("all", "base", "optimized", "profile")
DEFAULT_MODE = "all"
GROUP_TITLES = {
    "base": "Base strategies",
    "optimized": "Optimized strategies",
    "custom": "Experimental and other strategies",
}

REPO_ROOT = Path(__file__).resolve().parents[1]
METIS = metis_inspired.NAME
# The documented source of `metis_inspired` graph settings a source profile does not declare
# (WHI-1540): the registered WHI-1449 arm M4 (docs/references/metis-challenge-results.md §2),
# read only after its bytes match this pin. Only these keys are ever taken from it.
METIS_SETTINGS = {
    "path": "config/metis_challenge/m4.yaml",
    "sha256": "661311df6ff7a36a43954fff0e42f6b30b824d35d96c06f0e215960f256d4471",
}
METIS_GRAPH_KEYS = ("label_hops", "label_pruning", "chunks")
# WHI-1548: the implemented R021-C/1 §2 identities `all` appends after `metis_inspired`, in
# contract order. Each implementation ticket adds its own ID here with its registry entry,
# validator and preset; no placeholder is ever listed.
R021_ADDITIONS: tuple[str, ...] = (
    metis_history.NAME,  # WHI-1550 (contract order 1)
    direct_split_certified.NAME,  # WHI-1552 (contract order 2; its repository_grid preset)
    incremental_graph_repair.NAME,  # WHI-1554 (contract order 3)
    uni_sor_cycle_safe.NAME,  # WHI-1556 (contract order 4; its empty-options preset)
    cfmm_dual.NAME,  # WHI-1558 (contract order 5; WHI-1559: its current cfmm_dual/2 CL preset)
)
DERIVATION_NOTE = (
    "`algorithms`, `strategies` and `selection` are derived from the source; under `all`, a "
    f"graph.label_hops / label_pruning / chunks the source does not declare is copied for {METIS} "
    f"from {METIS_SETTINGS['path']} (sha256 {METIS_SETTINGS['sha256'][:12]}); replay with "
    "--strategies profile."
)


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
    # An already derived `all` profile keeps its recorded order (idempotent re-derivation).
    rederived = mode == "all" and (source.get("selection") or {}).get("mode") == "all"
    selected = [
        a for a in listed if strategy_group(a) in keep or (rederived and a in OPTIMIZED_STRATEGIES)
    ]
    optimized = [] if mode == "base" else list(OPTIMIZED_STRATEGIES)
    selected += [a for a in optimized if a not in selected]
    if mode == "all" and METIS not in selected:
        selected.append(METIS)
    if mode == "all":
        selected += [a for a in R021_ADDITIONS if a not in selected]
    if not selected:
        raise StrategySelectionError(
            f"--strategies {mode}: {source_path} selects no base strategy (algorithms "
            f"{listed}); use --strategies all, optimized or profile"
        )
    if mode == "all":  # metis_inspired's undeclared graph settings: the pinned M4 values
        graph = document.setdefault("graph", {})
        missing = [key for key in METIS_GRAPH_KEYS if key not in graph]
        if missing:
            registered = metis_graph_settings()
            graph.update({key: registered[key] for key in missing})
    declared = source.get("strategies") or {}
    declared_options = source.get("algorithm_options") or {}
    document.pop("algorithm_options", None)
    options = {
        name: json.loads(json.dumps(declared_options[name])) if name in declared_options
        else preset_options(ALGORITHMS[name])
        for name in selected
        if name in declared_options or ALGORITHMS[name].options_preset is not None
    }  # fmt: skip
    if options:
        document["algorithm_options"] = options
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


def metis_graph_settings() -> dict[str, Any]:
    """`METIS_GRAPH_KEYS` of the pinned M4 profile; a changed or unreadable file is refused."""
    where = f"{METIS} settings {METIS_SETTINGS['path']}"
    try:
        data = (REPO_ROOT / METIS_SETTINGS["path"]).read_bytes()
    except OSError as exc:
        raise StrategySelectionError(f"{where}: unreadable: {exc}") from exc
    digest = hashlib.sha256(data).hexdigest()
    if digest != METIS_SETTINGS["sha256"]:
        raise StrategySelectionError(
            f"{where}: sha256 {digest} differs from the pin {METIS_SETTINGS['sha256']}"
        )
    graph = yaml.safe_load(data)["graph"]
    return {key: graph[key] for key in METIS_GRAPH_KEYS}


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
        additions = (*OPTIMIZED_STRATEGIES, METIS, *R021_ADDITIONS)
        added = ", ".join(a for a in document["algorithms"] if a in additions)
        raise StrategySelectionError(
            f"--strategies {mode}: {source_path} cannot run the added strategies ({added}): "
            f"{exc}. They share the profile's objective, budget and search.max_hops/"
            "max_splits/percent_step; the optimized strategies need a percent_step grid "
            f"containing their recipe percents, and {METIS}'s graph.label_hops (the source's, "
            f"else {METIS_SETTINGS['path']}'s) must be >= search.max_hops -- nothing is "
            "re-tuned (e.g. config/daily_gross.yaml runs them all); use --strategies base for "
            "the base group only, or --strategies profile for the profile's exact selection"
        ) from exc


def selected_groups(profile: RunProfile) -> list[tuple[str, list[str]]]:
    """(group, algorithms), members in `profile.algorithms` order (groups are a presentation;
    `profile.algorithms` is the execution order), from the persisted selection record or, without
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
    "DERIVATION_NOTE",
    "GROUP_TITLES",
    "METIS",
    "METIS_SETTINGS",
    "MODES",
    "R021_ADDITIONS",
    "StrategySelectionError",
    "announce",
    "derive",
    "effective_document",
    "metis_graph_settings",
    "selected_groups",
]

"""WHI-1633 research-024 19-strategy campaign: schedule check, profiles, the L01-R024 timing files,
freeze, inputs, quality stages T / R, the timing stage L, analysis and tables (contract R024-C/1
§7, §8, §9, §10.2-§10.4; schedule `config/research_024/campaign.yaml`).

A thin driver over the ordinary CLI -- it never solves anything itself. Every quality invocation is
`main.py run --strategies profile` on a generated profile (a group of the 19-row `all` roster, or a
P*-rendered single-identity arm), plus its untimed work pass (`tools/research_022/pruning_work.py
run ...`). The quality stages use the 0.2.1 executor through the 0.2.2 driver
(`tools/research_022/pruning_campaign.py execute`: ledger, lanes, 30-s load samples, `caffeinate`,
`pmset` capture, the single infrastructure retry). Stage L is this file's own single-lane loop over
the timing units of §8.2 (`benchmark.latency run --arms ... --arm ...`, `report.latency compare`,
`main.py quote`), with the launch gate, triggers T1-T5, N = 3 and the terminal outcomes of §8.4.

    R=tools/research_024/r024_campaign.py
    uv run python $R profiles [--write]
    uv run python $R check [--stage T|R --inputs <dir> --out <dir>]
    uv run python $R freeze --out <file>
    uv run python $R inputs --primary <clone> --root <dir> --keys K...
    uv run python $R run --stage T|R --inputs <dir> --out <stage dir> [--tuning <T stage dir>]
    uv run python $R timing --inputs <dir> --out <L dir>
    uv run python $R analyze --stage T|R|L --inputs <dir> --out <stage dir> \\
        --json <analysis.json> [--sums <SHA256SUMS>] [--baseline-root <dir>]
    uv run python $R tables --analysis T=<json> R=<json> L=<json> --out <tables.md>

`check` without `--out` checks the schedule against contract §12, the checked-in files and the code
(the roster, the group profiles' per-identity values, the base-control decisions, the E2 arms, the
timing files and every timing arm's resolution); with `--out` it also runs every record check of
`analyze` over a finished quality stage and prints every problem.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _p in (str(REPO), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import r024_campaign_analysis as ca  # noqa: E402
import r024_rule as rr  # noqa: E402

from benchmark.profile import ProfileError, parse_profile, read_profile_document  # noqa: E402
from benchmark.strategies import derive, effective_document  # noqa: E402
from routing.algorithms.registry import ALGORITHMS  # noqa: E402


def _load(name: str, relative: str) -> ModuleType:
    """An earlier campaign driver, by path (never copied, never modified)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pc: Any = _load("r022_pruning_campaign", "tools/research_022/pruning_campaign.py")
c21: Any = pc.c21

SCHEDULE = REPO / "config" / "research_024" / "campaign.yaml"
SCHEMA = "r024.campaign/1"
E1, E2 = "split_polish", "marginal_activation"
GROUP_KEYS = ("base", "optimized", "custom")
SHARED = ("schema_version", "objective", "budget", "measurement", "worker", "search", "graph")
TOOLS = (
    "tools/research_024/r024_campaign.py",
    "tools/research_024/r024_campaign_analysis.py",
    "tools/research_024/r024_rule.py",
    "tools/research_022/pruning_campaign.py",
    "tools/research_022/pruning_work.py",
    "tools/research_021/campaign.py",
    "tools/research_021/analysis.py",
)


class CampaignError(ValueError):
    """The schedule, inputs or request are refused; nothing was executed or written."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_raw(path: Path = SCHEDULE) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise CampaignError(f"{path}: expected schema {SCHEMA}")
    return raw


def _pinned_bytes(spec: Mapping[str, Any]) -> bytes:
    data = (REPO / str(spec["path"])).read_bytes()
    if sha256_bytes(data) != spec["sha256"]:
        raise CampaignError(f"{spec['path']}: sha256 differs from the pin {spec['sha256']}")
    return data


def registry(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Contract §12 (the single YAML block of the pinned contract)."""
    text = _pinned_bytes(raw["contract"]).decode("utf-8")
    section = re.search(r"^## 12\. .*?(?=^## 13\. )", text, re.M | re.S)
    block = re.search(r"^```yaml\n(.*?)\n```", section.group(0) if section else "", re.M | re.S)
    if block is None:
        raise CampaignError("contract §12 holds no YAML registry")
    data = yaml.safe_load(block.group(1))
    assert isinstance(data, dict)
    return data


def preset_options(raw: Mapping[str, Any], algorithm: str) -> dict[str, Any]:
    doc = yaml.safe_load(_pinned_bytes(raw["presets"][algorithm]))
    if doc.get("algorithm") != algorithm:
        raise CampaignError(f"preset of {algorithm} names {doc.get('algorithm')}")
    return dict(doc["options"])


# ----------------------------------------------------------------------------- roster / profiles


def all_document(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The effective 19-row `--strategies all` document of the canonical profile (live roster)."""
    spec = raw["canonical"]
    _pinned_bytes(spec)
    source = read_profile_document(REPO / spec["path"])
    return effective_document(
        source, "all", source_path=str(spec["path"]), source_sha256=str(spec["sha256"])
    )


def restrict(doc: Mapping[str, Any], algorithms: Sequence[str]) -> dict[str, Any]:
    """`doc` (the derived roster document) with only `algorithms`: their `strategies` and
    `algorithm_options` entries, the `selection` record restricted to them (every key, `mode`
    included, kept; each group in the algorithms' order); every other section untouched."""
    out = copy.deepcopy(dict(doc))
    unknown = [a for a in algorithms if a not in out["algorithms"]]
    if unknown:
        raise CampaignError(f"{unknown} are not in the derived roster")
    out["algorithms"] = [a for a in out["algorithms"] if a in algorithms]
    for section in ("strategies", "algorithm_options"):
        kept = {k: v for k, v in (out.get(section) or {}).items() if k in algorithms}
        if kept:
            out[section] = kept
        else:
            out.pop(section, None)
    if "selection" in out:
        groups = out["selection"]["groups"]
        out["selection"]["groups"] = {
            g: [a for a in groups[g] if a in algorithms] for g in GROUP_KEYS
        }
    return out


def pstar_document(
    raw: Mapping[str, Any], identity: str, options: Mapping[str, Any]
) -> dict[str, Any]:
    """§4.4: the P*-rendered profile of `identity` with `options`."""
    spec = raw["canonical"]
    _pinned_bytes(spec)
    source = read_profile_document(REPO / spec["path"])
    doc: dict[str, Any] = {"schema_version": 2, "algorithms": [identity]}
    for key in ("objective", "search", "budget", "measurement", "worker"):
        doc[key] = copy.deepcopy(source[key])
    doc["graph"] = dict(raw["pstar_graph"])
    doc["algorithm_options"] = {identity: copy.deepcopy(dict(options))}
    return doc


def e1only_options(raw: Mapping[str, Any]) -> dict[str, Any]:
    """`split_polish` with E2's base and E2's six E1 keys (= E2's E1 stage, §7.2)."""
    e2 = preset_options(raw, E2)
    return {k: e2[k] for k in raw["e1only_keys"]}


def control_options(raw: Mapping[str, Any], e2_arm: str) -> dict[str, Any]:
    return {**preset_options(raw, E2), "arm": e2_arm}


def profiles(raw: Mapping[str, Any]) -> dict[str, tuple[dict[str, Any], str]]:
    """profile key -> (document, purpose): one per Q19 group, then the separate arms."""
    full = all_document(raw)
    out: dict[str, tuple[dict[str, Any], str]] = {}
    for group, algorithms in raw["groups"].items():
        out[f"q19-{group}"] = (
            restrict(full, algorithms),
            f"Q19 group {group}: the 19-row `all` roster restricted to {', '.join(algorithms)}",
        )
    for spec in raw["arms"]:
        if spec["kind"] == "e1only":
            out["e2-e1only"] = (
                pstar_document(raw, E1, e1only_options(raw)),
                "E2-E1only: split_polish with E2's base and E1 stage, P*",
            )
        elif spec["kind"] == "e2_control":
            key = str(spec["arm"]).lower()
            out[key] = (
                pstar_document(raw, E2, control_options(raw, str(spec["e2_arm"]))),
                f"{spec['arm']}: the E2 preset with arm {spec['e2_arm']}, P*",
            )
    return out


def profile_path(raw: Mapping[str, Any], key: str) -> str:
    return f"{raw['profile_dir']}/{key}.yaml"


def render_text(raw: Mapping[str, Any], key: str, doc: Mapping[str, Any], purpose: str) -> str:
    parse_profile(json.loads(json.dumps(doc)), key)  # refused exactly as the CLI would
    canonical = raw["canonical"]
    header = [
        f"# WHI-1633 research-024 campaign profile `{key}` (R024-C/1 §7.2). GENERATED by",
        "# `uv run python tools/research_024/r024_campaign.py profiles --write` from the",
        f"# `--strategies all` derivation of {canonical['path']}",
        f"# (sha256 {canonical['sha256']}) as config/research_024/campaign.yaml says;",
        "# do not edit by hand -- `r024_campaign.py check` refuses any drift from this rendering.",
        f"# Purpose: {purpose}.",
        "# Replay literally with --strategies profile.",
    ]
    return "\n".join(header) + "\n" + yaml.safe_dump(dict(doc), sort_keys=False)


def resolved_of(doc: Mapping[str, Any]) -> dict[str, Any]:
    return parse_profile(json.loads(json.dumps(doc)), "<rendered>").resolved()


def identity_values(resolved: Mapping[str, Any], identity: str) -> dict[str, Any]:
    """Everything a run records about one identity's configuration: its algorithm_config, its
    options entry (options, source, settings_sha256), its strategies entry and the shared
    sections (§7.2: per-identity resolved values)."""
    return {
        "algorithm_config": resolved["algorithm_config"][identity],
        "algorithm_options": (resolved.get("algorithm_options") or {}).get(identity),
        "strategies": (resolved.get("strategies") or {}).get(identity),
        **{k: resolved.get(k) for k in SHARED},
    }


def base_reads(base: str, options: Mapping[str, Any] | None) -> set[str]:
    factory = ALGORITHMS[base]
    keys = set(factory.search_params) | set(factory.graph_params)
    if factory.graph_params_for is not None:
        keys |= set(factory.graph_params_for(options or {}))
    return keys


def base_control_decision(raw: Mapping[str, Any], post: str) -> dict[str, Any]:
    """§7.2 BASE-*: does the Q19 row of the base serve as the identically configured control?"""
    resolved = resolved_of(all_document(raw))
    options = resolved["algorithm_options"][post]["options"]
    base = str(options["base"])
    handed = resolved["algorithm_config"][post]["params"]
    row = resolved["algorithm_config"][base]["params"]
    row_options = (resolved.get("algorithm_options") or {}).get(base)
    keys = base_reads(base, None if row_options is None else row_options["options"])
    same_params = {k: handed.get(k) for k in keys} == {k: row.get(k) for k in keys}
    same_options = options.get("base_options") == (
        None if row_options is None else row_options["options"]
    )
    return {
        "base": base,
        "keys": sorted(keys),
        "params_equal": same_params,
        "options_equal": same_options,
        "served_by": "Q19" if same_params and same_options else "arm",
    }


def e1only_decision(raw: Mapping[str, Any]) -> dict[str, Any]:
    resolved = resolved_of(all_document(raw))
    mine = rr.cec(resolved_of(pstar_document(raw, E1, e1only_options(raw))), E1)
    equal = rr.cec(resolved, E1) == mine
    return {"cec_equal": equal, "served_by": "Q19" if equal else "arm"}


# ----------------------------------------------------------------------------- timing files


def render_all19(raw: Mapping[str, Any]) -> str:
    doc = all_document(raw)
    parse_profile(json.loads(json.dumps(doc)), raw["timing"]["profile"]["path"])
    header = (
        "# WHI-1633 research-024 L01-R024 pinned profile (R024-C/1 §8.1): the effective\n"
        "# 19-identity `--strategies all` document of config/full_gross.yaml (sha256\n"
        f"# {raw['canonical']['sha256']}) at the schedule commit, written out\n"
        "# (it carries the selected presets). GENERATED by `r024_campaign.py profiles\n"
        "# --write`; do not edit -- `r024_campaign.py check` refuses any drift.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False)


def render_protocol(raw: Mapping[str, Any], profile_text: str) -> str:
    """`config/latency/l01.yaml` with only `key` and the pinned `profile` replaced (§8.1)."""
    spec = raw["timing"]["protocol"]
    source = (REPO / spec["source"]).read_bytes()
    if sha256_bytes(source) != spec["source_sha256"]:
        raise CampaignError(f"{spec['source']} differs from its pin")
    doc = yaml.safe_load(source)
    doc["key"] = spec["key"]
    doc["profile"] = {
        "path": raw["timing"]["profile"]["path"],
        "sha256": sha256_bytes(profile_text.encode()),
    }
    header = (
        "# WHI-1633 research-024 latency protocol L01-R024 (R024-C/1 §8.1, §8.3). GENERATED by\n"
        "# `r024_campaign.py profiles --write` from config/latency/l01.yaml (sha256\n"
        f"# {spec['source_sha256']}): only `key` and the pinned\n"
        "# `profile` (the effective 19-identity `all` document) differ -- the matrix, sentinel,\n"
        "# cohorts, timing, cold, quote-CLI, load and acceptance rules are L01's, verbatim.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False)


def up_baseline(raw: Mapping[str, Any]) -> list[str]:
    """§8.2 UP-base: the distinct bases of the two selected presets, in roster order."""
    bases = {str(preset_options(raw, a)["base"]) for a in (E1, E2)}
    return [a for a in raw["all19"] if a in bases]


def up_pairs(raw: Mapping[str, Any]) -> dict[str, str]:
    return {a: str(preset_options(raw, a)["base"]) for a in (E1, E2)}


def timing_arms(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The arms of the arms file: UP-base, UP-cand, then U1-U6 (registered order)."""
    out = [
        {"name": "UP-base", "algorithms": up_baseline(raw)},
        {"name": "UP-cand", "algorithms": [E1, E2]},
    ]
    for unit in raw["timing"]["units"]:
        if unit["kind"] == "descriptive":
            out.append({"name": str(unit["unit"]), "algorithms": list(unit["algorithms"])})
    return [{**a, "controls": [], "stages": ["timing", "cold"]} for a in out]


def render_arms(raw: Mapping[str, Any], protocol_text: str) -> str:
    timing = raw["timing"]
    doc = {
        "schema": "latency-arms/1",
        "key": timing["arms"]["key"],
        "version": 1,
        "protocol": {
            "path": timing["protocol"]["path"],
            "sha256": sha256_bytes(protocol_text.encode()),
        },
        "sufficient_budget": {
            "path": timing["sufficient_budget"]["path"],
            "sha256": timing["sufficient_budget"]["sha256"],
        },
        "source": "every_arm_same_clean_commit",
        "controls": {},
        "arms": timing_arms(raw),
        "comparisons": [
            {
                "id": "UP",
                "lane": "heuristic",
                "baseline": "UP-base",
                "candidate": "UP-cand",
                "role": "decision",
                "pairs": up_pairs(raw),
            }
        ],
        "dispositions": {
            "exact": {
                "adopt_eligible": "not used",
                "reject": "not used",
                "inconclusive": "not used",
            },
            "heuristic": {
                "opt_in_only": "not used (no adoption; R024-C/1 §9)",
                "reject": "not used (the UP ratio is reported as is, §8.6)",
                "inconclusive": "reported as is",
            },
        },
    }
    header = (
        "# WHI-1633 research-024 timing units (R024-C/1 §8.2) as L08 arms under L01-R024: UP\n"
        "# (UP-base = the distinct bases of the selected presets, UP-cand = split_polish and\n"
        "# marginal_activation, paired in the heuristic lane) and U1-U6 (descriptive). UQ is not\n"
        "# an arm (5 `main.py quote` processes). GENERATED by `r024_campaign.py profiles\n"
        "# --write`.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False)


def timing_files(raw: Mapping[str, Any]) -> dict[str, str]:
    """path -> exact text of the three generated timing files."""
    profile = render_all19(raw)
    protocol = render_protocol(raw, profile)
    return {
        raw["timing"]["profile"]["path"]: profile,
        raw["timing"]["protocol"]["path"]: protocol,
        raw["timing"]["arms"]["path"]: render_arms(raw, protocol),
    }


def timing_arm_problems(raw: Mapping[str, Any]) -> list[str]:
    """Every timing arm resolves through `benchmark.latency.arm_profile` (the runtime the stage
    runs) and each identity's resolved values equal the full 19-row document's (§8.1; WHI-1686)."""
    from benchmark.latency import LatencyError, arm_profile, load_arms

    problems = []
    try:
        arms = load_arms(REPO / raw["timing"]["arms"]["path"])
    except (LatencyError, OSError) as exc:
        return [f"timing arms file: {exc}"]
    full = resolved_of(all_document(raw))
    for name, arm in arms.arms.items():
        try:
            resolved = arm_profile(arm, raw["timing"]["profile"]["path"]).resolved()
        except (LatencyError, ProfileError) as exc:
            problems.append(f"timing arm {name}: refused: {exc}")
            continue
        for identity in arm.algorithms or ():
            if identity_values(resolved, identity) != identity_values(full, identity):
                problems.append(f"timing arm {name}: {identity} differs from the 19-row document")
    return problems


# ----------------------------------------------------------------------------- invocations


def invocations(raw: Mapping[str, Any], stage: str) -> list[dict[str, Any]]:
    """Every quality invocation of a stage, in the registered launch order of stage T (§ stages):
    ordinary invocations by descending `estimate_minutes` (full cohort first on ties), then the
    work passes in the same order. Ids: `<stage>-<arm key>-<cohort>`, `<stage>-WP-...`."""
    bundles = raw["stage_bundles"][stage]
    rows: list[dict[str, Any]] = []
    for spec in raw["arms"]:
        cohort = str(spec["cohort"])
        keys = (
            [f"q19-{g}" for g in raw["groups"]]
            if spec["kind"] == "q19"
            else [str(spec["arm"]).lower()]
        )
        for key in keys:
            algorithms = (
                list(raw["groups"][key[4:]])
                if spec["kind"] == "q19"
                else [E1 if spec["kind"] == "e1only" else E2]
            )
            rows.append(
                {
                    "key": key,
                    "cohort": cohort,
                    "arm": str(spec["arm"]),
                    "kind": str(spec["kind"]),
                    "algorithms": algorithms,
                    "work_pass": bool(spec["work_pass"]),
                    "estimate": float(raw["estimate_minutes"][key]),
                }
            )
    rows.sort(key=lambda r: (-r["estimate"], r["cohort"] != "full"))
    out = []
    for work in (False, True):
        for row in rows:
            if work and not row["work_pass"]:
                continue
            tag = "-WP" if work else ""
            out.append(
                {
                    "id": f"{stage}{tag}-{row['key']}-{row['cohort']}",
                    "stage": stage,
                    "kind": "run",
                    "bundle": bundles[row["cohort"]],
                    "profile": row["key"],
                    "strategies": "profile",
                    "algorithms": row["algorithms"],
                    "arm": row["arm"],
                    "arm_kind": row["kind"],
                    "group": row["key"],
                    "cohort": row["cohort"],
                    "work_pass": work,
                }
            )
    return out


def ordered_by_tuning(
    raw: Mapping[str, Any], invs: list[dict[str, Any]], tuning: Path
) -> list[dict[str, Any]]:
    """Stage R's registered launch order: descending stage-T wall time of the same invocation
    (ordinary invocations first, then work passes; ties keep the T order)."""
    done = c21.read_ledger(tuning)
    order = {i["id"]: n for n, i in enumerate(invocations(raw, "T"))}

    def wall(inv: Mapping[str, Any]) -> float:
        entry = done.get("T" + str(inv["id"])[1:])
        if not entry or entry.get("result") != "ok" or entry.get("started") is None:
            raise CampaignError(f"stage T has no completed {'T' + str(inv['id'])[1:]}")
        return float(entry["t"]) - float(entry["started"])

    return sorted(
        invs, key=lambda i: (bool(i["work_pass"]), -wall(i), order["T" + str(i["id"])[1:]])
    )


def build(
    raw: Mapping[str, Any], stage: str, tuning: Path | None = None, path: Path = SCHEDULE
) -> Any:
    """A 0.2.1 `Campaign` over one quality stage of this schedule."""
    invs = invocations(raw, stage)
    if raw["stages"][stage].get("order") == "stage_t_wall":
        if tuning is None:
            raise CampaignError(f"stage {stage}: the launch order needs --tuning <T stage dir>")
        invs = ordered_by_tuning(raw, invs, tuning)
    view = dict(raw)
    view["profiles"] = {k: {"path": profile_path(raw, k)} for k in profiles(raw)}
    view["stages"] = {s: dict(v) for s, v in raw["stages"].items()}
    campaign = c21.Campaign(view, c21._expand(invs), path)
    for inv in campaign.invocations:
        if inv.id in campaign.by_id:
            raise CampaignError(f"duplicate invocation {inv.id}")
        campaign.by_id[inv.id] = inv
    return campaign


# ----------------------------------------------------------------------------- check


def check(raw: Mapping[str, Any]) -> list[str]:
    """Every problem between the schedule, contract §12, the checked-in files and the code."""
    problems: list[str] = []
    pins = [
        ("contract", raw["contract"]),
        ("canonical", raw["canonical"]),
        ("m4 settings", raw["m4_settings"]),
        ("sufficient budget", raw["timing"]["sufficient_budget"]),
        *((f"preset {k}", v) for k, v in raw["presets"].items()),
    ]
    for label, spec in pins:
        try:
            _pinned_bytes(spec)
        except (CampaignError, OSError) as exc:
            problems.append(f"{label}: {exc}")
    if problems:
        return problems
    reg = registry(raw)
    # the schedule's registered values are the contract's (§12)
    for key, spec in reg["campaign"]["inputs"].items():
        mine = raw["inputs"].get(key) or {}
        if {k: mine.get(k) for k in ("bundle_hash", "cases")} != spec:
            problems.append(f"input {key} differs from contract §12 campaign.inputs")
    if set(raw["inputs"]) != set(reg["campaign"]["inputs"]):
        problems.append("inputs differ from contract §12 campaign.inputs")
    if list(raw["all19"]) != list(reg["roster"]["all19"]):
        problems.append("all19 differs from contract §12 roster.all19")
    scheduled = {*(str(a["arm"]) for a in raw["arms"]), *raw["base_controls"], "WP"}
    if scheduled != set(reg["campaign"]["arms"]):
        problems.append(f"arms differ from contract §12 campaign.arms {reg['campaign']['arms']}")
    no_wp = sorted(str(a["arm"]) for a in raw["arms"] if not a["work_pass"])
    if no_wp != sorted(reg["campaign"]["no_work_pass"]):
        problems.append("arms without a work pass differ from contract §12 no_work_pass")
    timing = reg["timing"]
    mine_timing = raw["timing"]
    for key in (
        "units",
        "max_started_attempts_per_unit",
        "launch",
        "validity",
        "triggers",
        "never_triggers",
        "outcomes",
    ):
        if mine_timing[key] != timing[key]:
            problems.append(f"timing.{key} differs from contract §12 timing.{key}")
    proto = timing["protocol"]
    if {k: mine_timing["protocol"][k] for k in ("key", "source", "replaced", "source_sha256")} != {
        k: proto[k] for k in ("key", "source", "replaced", "source_sha256")
    }:
        problems.append("timing.protocol differs from contract §12 timing.protocol")
    t3 = mine_timing["t3_reading"]
    if (t3["match"], tuple(t3["trigger_types"])) != ("exact_type_column", ca.T3_TYPES):
        problems.append(f"timing.t3_reading differs from the registered literal reading {t3}")
    covered = [a for g in raw["groups"].values() for a in g]
    if sorted(covered) != sorted(raw["all19"]) or len(covered) != len(set(covered)):
        problems.append("the Q19 groups do not partition all19 exactly once")
    for group, members in raw["groups"].items():
        if members != [a for a in raw["all19"] if a in members]:
            problems.append(f"group {group}: not in roster order")
    # the derived roster (live, every release's additions) and its resolved hash
    try:
        full_doc = all_document(raw)
        document, full_profile = derive(
            read_profile_document(REPO / raw["canonical"]["path"]),
            "all",
            source_path=raw["canonical"]["path"],
            source_sha256=raw["canonical"]["sha256"],
        )
    except (CampaignError, ProfileError) as exc:
        return [*problems, f"the roster does not derive: {exc}"]
    full = full_profile.resolved()
    if list(document["algorithms"]) != list(raw["all19"]):
        problems.append(f"`--strategies all` derives {document['algorithms']}")
    if rr.canonical_sha256(full) != raw["all19_resolved_sha256"]:
        problems.append("the 19-row resolved profile hash differs from all19_resolved_sha256")
    if full_doc != document:
        problems.append("all_document differs from the CLI derivation")
    for identity in (E1, E2):
        entry = full["algorithm_options"][identity]
        if entry["options"] != preset_options(raw, identity) or entry["source"]["kind"] != "preset":
            problems.append(f"{identity}: the derived row is not its selected preset")
    # generated profiles: rendering, per-identity values, nothing unregistered
    table = profiles(raw)
    for key, (doc, purpose) in table.items():
        path = REPO / profile_path(raw, key)
        try:
            text = render_text(raw, key, doc, purpose)
        except ProfileError as exc:
            problems.append(f"profile {key}: {exc}")
            continue
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            problems.append(f"profile {key}: {profile_path(raw, key)} differs from its rendering")
            continue
        written, parsed = derive(
            read_profile_document(path), "profile", source_path=str(path), source_sha256="0" * 64
        )
        if list(written["algorithms"]) != list(doc["algorithms"]):
            problems.append(f"profile {key}: derives {written['algorithms']}")
        if key.startswith("q19-"):
            resolved = parsed.resolved()
            for identity in doc["algorithms"]:
                if identity_values(resolved, identity) != identity_values(full, identity):
                    problems.append(f"profile {key}: {identity} differs from the 19-row roster")
    stray = {p.name for p in (REPO / raw["profile_dir"]).glob("*.yaml")} - {
        f"{k}.yaml" for k in table
    }
    if stray:
        problems.append(f"unregistered profiles in {raw['profile_dir']}: {sorted(stray)}")
    # §7.2 base-control decisions, re-derived by the stated rule
    for key, spec in raw["base_controls"].items():
        if key.startswith("BASE-"):
            got = base_control_decision(raw, str(spec["post_processor"]))
            if got["base"] != spec["base"] or got["served_by"] != spec["served_by"]:
                problems.append(f"{key}: the stated rule gives {got}, registered {spec}")
        else:
            got = e1only_decision(raw)
            if got["served_by"] != spec["served_by"]:
                problems.append(f"{key}: the stated rule gives {got}, registered {spec}")
    separate = {str(a["arm"]) for a in raw["arms"] if a["kind"] != "q19"}
    for key, spec in raw["base_controls"].items():
        if (spec["served_by"] == "arm") != (key in separate):
            problems.append(
                f"{key}: served_by {spec['served_by']} but arm scheduled: {key in separate}"
            )
    # the E2 controls equal the E2 row but for `arm`; E2-E1only is E2's E1 stage
    e2_cec = rr.cec(full, E2)
    for spec in raw["arms"]:
        if spec["kind"] == "e2_control":
            mine = rr.cec(resolved_of(table[str(spec["arm"]).lower()][0]), E2)
            if {
                **mine,
                "options": {**mine["options"], "arm": "treatment"},
                "settings_sha256": None,
            } != {**e2_cec, "settings_sha256": None}:
                problems.append(f"{spec['arm']}: differs from the Q19 E2 row beyond `arm`")
    # the timing files and every timing arm's resolution
    try:
        for rel, text in timing_files(raw).items():
            if not (REPO / rel).is_file() or (REPO / rel).read_text(encoding="utf-8") != text:
                problems.append(f"{rel} differs from its rendering")
    except (CampaignError, OSError, ProfileError) as exc:
        problems.append(f"timing files: {exc}")
        return problems
    units = [u for u in raw["timing"]["units"] if u["kind"] == "descriptive"]
    if sorted(a for u in units for a in u["algorithms"]) != sorted(raw["all19"][:17]):
        problems.append("U1-U6 do not partition the 17 earlier identities")
    problems += timing_arm_problems(raw)
    return problems


# ----------------------------------------------------------------------------- inputs / freeze


def prepare_inputs(
    raw: Mapping[str, Any], primary: Path, root: Path, keys: Sequence[str]
) -> dict[str, Any]:
    """Copy the named registered inputs into `root` (outside any worktree), hash-verified. A
    report bundle is copied only after the freeze push (the operator names the keys)."""
    out: dict[str, Any] = {}
    for key in keys:
        if key not in raw["inputs"]:
            raise CampaignError(f"unknown input {key}")
        spec = raw["inputs"][key]
        target = root / key
        if not target.exists():
            shutil.copytree(primary / spec["source"], target)
        digest = sha256_bytes((target / "manifest.json").read_bytes())
        if digest != spec["bundle_hash"]:
            raise CampaignError(f"input {key}: {target} bundle hash {digest} != registered")
        out[key] = {"path": str(target), "bundle_hash": digest}
    return out


def _file_pin(path: str) -> dict[str, str]:
    return {"path": path, "sha256": sha256_bytes((REPO / path).read_bytes())}


def freeze_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The pins of everything stages R and L execute and analyse from, the registered resolved
    identity of every quality invocation and the inventories."""
    campaign = build(raw, "T")
    return {
        "schema": "r024.campaign-freeze/1",
        "issue": raw["issue"],
        "contract": raw["contract"]["key"],
        "files": {
            "schedule": _file_pin(str(SCHEDULE.relative_to(REPO))),
            **{f"tool:{t}": _file_pin(t) for t in TOOLS},
            **{f"profile:{k}": _file_pin(profile_path(raw, k)) for k in profiles(raw)},
            **{f"timing:{Path(p).name}": _file_pin(p) for p in timing_files(raw)},
            "contract": _file_pin(str(raw["contract"]["path"])),
            "canonical": _file_pin(str(raw["canonical"]["path"])),
            "m4_settings": _file_pin(str(raw["m4_settings"]["path"])),
            "sufficient_budget": _file_pin(str(raw["timing"]["sufficient_budget"]["path"])),
            **{f"preset:{k}": _file_pin(str(v["path"])) for k, v in raw["presets"].items()},
        },
        "inputs": raw["inputs"],
        "base_controls": raw["base_controls"],
        "rules_sha256": sha256_bytes(json.dumps(raw["rules"], sort_keys=True).encode()),
        "timing": raw["timing"],
        "effective_settings": {
            inv.id: ident
            for inv in campaign.invocations
            if (ident := c21.resolved_identity(campaign, inv)) is not None
        },
        "inventory": {s: [i["id"] for i in invocations(raw, s)] for s in ("T", "R")},
    }


# ----------------------------------------------------------------------------- stage L


class TimingStage:
    """Stage L (§8.4): units in order, each up to N started attempts after its own launch gate.

    Everything is appended to `<out>/ledger.jsonl` (`launch_gate`, `attempt_start`, `experiment`,
    `abort`, `attempt_end`, `attempt_validity`, `unit_outcome`), the stage-wide 30-s load samples to
    `<out>/load.jsonl`, each attempt's pmset capture to `<out>/<unit>/a<k>/pmset.txt`. `runner`
    starts one command (default: a subprocess in the repository) and is replaceable in tests."""

    def __init__(
        self,
        raw: Mapping[str, Any],
        inputs: Path,
        out: Path,
        *,
        launcher: Sequence[str] = c21.DEFAULT_LAUNCHER,
        echo: Callable[[str], None] = print,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        load: Callable[[], float] = lambda: os.getloadavg()[0],
        pmset: Callable[[], str] | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        caffeinate: Sequence[str] | None = None,
    ) -> None:
        self.raw, self.inputs, self.out = raw, inputs, out
        self.timing = raw["timing"]
        self.launcher, self.echo = list(launcher), echo
        self.clock, self.sleep, self.load, self.popen = clock, sleep, load, popen
        self.pmset = pmset or _pmset_log
        self.caffeinate_argv = (
            list(caffeinate)
            if caffeinate is not None
            else [*str(self.timing["validity"]["caffeinate"]).split(), "-w", str(os.getpid())]
        )
        self.ledger = c21.Ledger(out / "ledger.jsonl")
        self.samples_path = out / "load.jsonl"
        self.awake: Any = None
        self.cpus = int(os.cpu_count() or raw["host"]["logical_cpus"])

    # -- host evidence
    def sample(self) -> dict[str, float]:
        entry = {"t": self.clock(), "load1": float(self.load())}
        with self.samples_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        return entry

    def samples(self) -> list[dict[str, Any]]:
        if not self.samples_path.is_file():
            return []
        text = self.samples_path.read_text(encoding="utf-8")
        return [json.loads(x) for x in text.splitlines() if x]

    def ensure_awake(self) -> None:
        if self.awake is not None and self.awake.poll() is None:
            return
        self.awake = self.popen(self.caffeinate_argv)
        self.ledger.append(
            {
                "event": "caffeinate",
                "pid": self.awake.pid,
                "argv": self.caffeinate_argv,
                "t_started": self.clock(),
            }
        )

    def wait(self, seconds: float) -> None:
        """Sleep `seconds` while keeping the 30-s load sampling going."""
        interval = float(self.timing["validity"]["sample_interval_seconds"])
        end = self.clock() + seconds
        while self.clock() < end:
            self.sleep(min(interval, max(0.0, end - self.clock())))
            self.sample()

    def launch_gate(self, unit: str, attempt: int) -> bool:
        """§8.4 launch gate: five 1-minute load samples 30 s apart, none above the headroom;
        otherwise re-sample every 300 s; no pass within `max_wait_seconds` -> no launch."""
        gate = self.timing["launch"]
        started = self.clock()
        window = 0
        while True:
            window += 1
            group = []
            for index in range(int(gate["samples"])):
                if index:
                    self.wait(float(gate["interval_seconds"]))
                group.append(self.sample())
            peak = max(s["load1"] for s in group)
            passed = peak <= float(gate["headroom_load1"])
            self.ledger.append(
                {
                    "event": "launch_gate",
                    "unit": unit,
                    "attempt": attempt,
                    "group": window,
                    "samples": group,
                    "max_load1": peak,
                    "headroom_load1": gate["headroom_load1"],
                    "passed": passed,
                }
            )
            if passed:
                return True
            waited = self.clock() - started
            if waited + float(gate["resample_every_seconds"]) > float(gate["max_wait_seconds"]):
                self.ledger.append(
                    {
                        "event": "no_launch",
                        "unit": unit,
                        "attempt": attempt,
                        "waited_seconds": waited,
                        "groups": window,
                    }
                )
                return False
            self.echo(f"{unit} a{attempt}: launch gate max load {peak:.2f}; waiting")
            self.wait(float(gate["resample_every_seconds"]))

    # -- commands
    def commands(self, unit: Mapping[str, Any], slot: Path) -> list[tuple[str, list[str]]]:
        """(experiment id, argv) of one attempt, in order (§8.2)."""
        py = self.launcher
        bundle = str(self.inputs / str(self.timing["bundle"]))
        name = str(unit["unit"])
        protocol, arms = self.timing["protocol"]["path"], self.timing["arms"]["path"]

        def latency(arm: str) -> list[str]:
            return [
                *py,
                "-m",
                "benchmark.latency",
                "run",
                "--protocol",
                protocol,
                "--arms",
                arms,
                "--arm",
                arm,
                "--bundle",
                bundle,
                "--out",
                str(slot / arm),
            ]

        if unit["kind"] == "paired":
            compare = [
                *py,
                "-m",
                "report.latency",
                "compare",
                "{dir:UP-base}",
                "{dir:UP-cand}",
                "--lane",
                "heuristic",
            ]
            for cand, ref in up_pairs(self.raw).items():
                compare += ["--pair", f"{cand}={ref}"]
            compare += [
                "--json",
                str(slot / "compare" / "compare.json"),
                "--markdown",
                str(slot / "compare" / "compare.md"),
            ]
            return [
                ("UP-base", latency("UP-base")),
                ("UP-cand", latency("UP-cand")),
                ("compare", compare),
            ]
        if unit["kind"] == "descriptive":
            return [(name, latency(name))]
        request = self.raw["requests"][self.timing["quote_cli"]["request"]]
        spec = self.timing["quote_cli"]
        out = []
        for index in range(1, int(unit["invocations"]) + 1):
            argv = [
                *py,
                "main.py",
                "quote",
                "--bundle",
                bundle,
                "--profile",
                spec["profile"],
                "--token-in",
                str(request["token_in"]),
                "--token-out",
                str(request["token_out"]),
                "--amount",
                str(request["amount"]),
                "--quotes-dir",
                str(slot / f"q{index}"),
                "--strategies",
                spec["strategies"],
            ]
            out.append((f"q{index}", argv + (["--details"] if spec["details"] else [])))
        return out

    def _resolve(self, argv: list[str], slot: Path) -> list[str]:
        """`{dir:<experiment>}` -> the single experiment directory under `<slot>/<experiment>`."""
        out = []
        for part in argv:
            if part.startswith("{dir:"):
                part = str(c21._single_child(slot / part[5:-1]))
            out.append(part)
        return out

    # -- one attempt
    def detect(self, start: float) -> str | None:
        """A T1-T4 event detected so far in an attempt that started at `start`."""
        samples = [s for s in self.samples() if s["t"] >= start]
        limit = float(self.timing["validity"]["max_load1_per_cpu"]) * self.cpus
        if any(s["load1"] > limit for s in samples):
            return "T1_load"
        last = samples[-1]["t"] if samples else start
        if self.clock() - last > float(self.timing["validity"]["max_sample_gap_seconds"]):
            return "T2_sampling_coverage"
        if self.awake is None or self.awake.poll() is not None:
            return "T4_sleep_prevention_or_capture"
        return None

    def run_attempt(self, unit: Mapping[str, Any], attempt: int) -> dict[str, Any]:
        name = str(unit["unit"])
        attempt_id = f"L-{name}-a{attempt}"
        slot = self.out / name / f"a{attempt}"
        slot.mkdir(parents=True)
        monitor = self.timing["monitor"]
        start = self.clock()
        self.ledger.append(
            {
                "event": "attempt_start",
                "unit": name,
                "attempt": attempt_id,
                "t_start": start,
                "caffeinate_pid": self.awake.pid,
                "load1": self.sample()["load1"],
            }
        )
        experiments: list[dict[str, Any]] = []
        aborted: str | None = None
        last_pmset = start
        for experiment, argv in self.commands(unit, slot):
            try:
                argv = self._resolve(argv, slot)
            except CampaignError as exc:
                experiments.append({"experiment": experiment, "exit_code": None, "error": str(exc)})
                break
            (slot / experiment).mkdir(parents=True, exist_ok=True)
            with (
                (slot / f"{experiment}.stdout.log").open("wb") as so,
                (slot / f"{experiment}.stderr.log").open("wb") as se,
            ):
                began = self.clock()
                proc = self.popen(argv, cwd=REPO, stdout=so, stderr=se)
                self.ledger.append(
                    {
                        "event": "experiment_start",
                        "attempt": attempt_id,
                        "experiment": experiment,
                        "argv": argv,
                        "t": began,
                    }
                )
                try:
                    while proc.poll() is None:
                        self.sleep(float(monitor["check_seconds"]))
                        self.sample()
                        event = self.detect(start)
                        if event is None and self.clock() - last_pmset >= float(
                            monitor["pmset_check_seconds"]
                        ):
                            last_pmset = self.clock()
                            try:
                                entries = ca.pmset_entries(self.pmset())
                            except (OSError, subprocess.SubprocessError):
                                entries = []  # only the final capture decides T4
                            if ca.t3_hits(entries, start, self.clock()):
                                event = "T3_sleep"
                        if event is not None:
                            aborted = event
                            self.ledger.append(
                                {
                                    "event": "abort",
                                    "attempt": attempt_id,
                                    "experiment": experiment,
                                    "trigger": event,
                                    "t": self.clock(),
                                }
                            )
                            _terminate(proc, float(monitor["terminate_grace_seconds"]))
                            break
                except KeyboardInterrupt:  # the driver itself is stopped: the attempt is T5
                    self.ledger.append(
                        {
                            "event": "abort",
                            "attempt": attempt_id,
                            "experiment": experiment,
                            "trigger": "driver_interrupted",
                            "t": self.clock(),
                        }
                    )
                    _terminate(proc, float(monitor["terminate_grace_seconds"]))
                    raise
                code = proc.wait()
            entry = {
                "experiment": experiment,
                "exit_code": code,
                "t_start": began,
                "t_end": self.clock(),
                "dir": str(slot / experiment),
            }
            experiments.append(entry)
            self.ledger.append({"event": "experiment_end", "attempt": attempt_id, **entry})
            if aborted or code != 0:
                break
        end = self.clock()
        self.sample()
        awake_held = self.awake is not None and self.awake.poll() is None
        try:
            capture: str | None = self.pmset()
        except (OSError, subprocess.SubprocessError) as exc:
            capture = None
            self.ledger.append(
                {"event": "pmset_capture", "attempt": attempt_id, "ok": False, "error": str(exc)}
            )
        if capture is not None:
            kept = ca.pmset_window_lines(capture, start - 60, end + 60)
            (slot / "pmset.txt").write_text("".join(f"{x}\n" for x in kept), encoding="utf-8")
        self.ledger.append(
            {
                "event": "attempt_end",
                "unit": name,
                "attempt": attempt_id,
                "t_start": start,
                "t_end": end,
                "aborted": aborted,
                "caffeinate_held": awake_held,
                "pmset_captured": capture is not None,
                "experiments": experiments,
            }
        )
        verdict = ca.attempt_validity(
            unit=dict(unit),
            experiments=experiments,
            start=start,
            end=end,
            samples=self.samples(),
            cpus=self.cpus,
            validity=self.timing["validity"],
            pmset_text=capture,
            caffeinate_held=awake_held,
            caffeinate_started=self._awake_started(),
            aborted=aborted,
            expected=[e for e, _ in self.commands(unit, slot)],
        )
        self.ledger.append(
            {"event": "attempt_validity", "unit": name, "attempt": attempt_id, **verdict}
        )
        return verdict

    def _awake_started(self) -> float | None:
        starts = [e["t_started"] for e in self.ledger.entries() if e.get("event") == "caffeinate"]
        return float(starts[-1]) if starts else None

    # -- the stage
    def run(self, only: Sequence[str] = ()) -> int:
        revision, dirty, _ = c21.git_provenance(REPO)
        self.out.mkdir(parents=True, exist_ok=True)
        state = ca.timing_state(self.ledger.entries(), self.raw)
        self.ledger.append(
            {
                "event": "stage",
                "stage": "L",
                "git_revision": revision,
                "git_dirty": dirty,
                "logical_cpus": self.cpus,
                "resume": state,
                "t": self.clock(),
            }
        )
        for name, interrupted in state["interrupted"].items():  # §10.4: an aborted attempt (T5)
            self.ledger.append(
                {
                    "event": "attempt_end",
                    "unit": name,
                    "attempt": interrupted,
                    "aborted": "driver_interrupted",
                    "experiments": [],
                    "t_end": None,
                }
            )
            self.ledger.append(
                {
                    "event": "attempt_validity",
                    "unit": name,
                    "attempt": interrupted,
                    "valid": False,
                    "triggers": ["T5_incomplete_execution"],
                    "detail": {"T5": ["the driver ended inside the attempt"]},
                }
            )
        self.ensure_awake()
        previous = signal.signal(signal.SIGTERM, _raise_interrupt)
        try:
            for unit in self.timing["units"]:
                name = str(unit["unit"])
                if only and name not in only:
                    continue
                state = ca.timing_state(self.ledger.entries(), self.raw)
                if name in state["outcomes"]:
                    continue
                started = state["attempts"].get(name, [])
                cap = int(self.timing["max_started_attempts_per_unit"])
                outcome: dict[str, Any] | None = None
                while len(started) < cap:
                    attempt = len(started) + 1
                    if not self.launch_gate(name, attempt):
                        outcome = {
                            "outcome": "inconclusive_no_launch",
                            "attempt": None,
                            "invalid_attempts": list(started),
                        }
                        break
                    self.ensure_awake()
                    verdict = self.run_attempt(unit, attempt)
                    started.append(f"L-{name}-a{attempt}")
                    if verdict["valid"]:
                        outcome = {"outcome": "valid", "attempt": f"L-{name}-a{attempt}"}
                        break
                if outcome is None:
                    outcome = {
                        "outcome": "inconclusive_cap_exhausted",
                        "attempt": None,
                        "invalid_attempts": list(started),
                    }
                self.ledger.append({"event": "unit_outcome", "unit": name, **outcome})
                self.echo(f"{name}: {outcome}")
        except KeyboardInterrupt:
            self.ledger.append({"event": "stage_interrupted", "t": self.clock()})
            return 130
        finally:
            signal.signal(signal.SIGTERM, previous)
            if self.awake is not None and self.awake.poll() is None:
                self.awake.terminate()
                self.awake.wait(timeout=10)
        self.ledger.append({"event": "stage_end", "stage": "L", "t": self.clock()})
        return 0


def _raise_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def _terminate(proc: Any, grace: float) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _pmset_log() -> str:
    return subprocess.run(
        ["pmset", "-g", "log"], capture_output=True, text=True, timeout=300, check=True
    ).stdout


# ----------------------------------------------------------------------------- analysis


def stage_runs(
    raw: Mapping[str, Any], stage: str, out: Path
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """invocation id -> its run directory (the effective ledger entry: own `ok` end, else its
    single infrastructure retry), plus a problem for every invocation without a completed run."""
    done = c21.read_ledger(out)
    runs: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for inv in invocations(raw, stage):
        entry, deviation = c21.effective_entry(done, inv["id"])
        if entry is None or not entry.get("run_dir"):
            state = None if entry is None else entry.get("result")
            problems.append(f"{inv['id']}: no recorded run (ledger: {state})")
            continue
        if entry.get("result") != "ok":
            problems.append(f"{inv['id']}: ledger result {entry.get('result')}")
        runs[inv["id"]] = {
            **inv,
            "run_dir": str(entry["run_dir"]),
            "deviation": deviation,
            "started": entry.get("started"),
            "ended": entry.get("t"),
        }
    return runs, problems


def expected_resolved(raw: Mapping[str, Any], key: str) -> dict[str, Any]:
    doc, _ = profiles(raw)[key]
    return resolved_of(doc)


def analyze(
    raw: Mapping[str, Any], stage: str, inputs: Path, out: Path, baseline_root: Path | None = None
) -> dict[str, Any]:
    """The registered analysis of a quality stage (§7.4) from its ledger and run records."""
    runs, problems = stage_runs(raw, stage, out)
    expected = {k: expected_resolved(raw, k) for k in profiles(raw)}
    families = {}
    for cohort, bundle in raw["stage_bundles"][stage].items():
        families[cohort] = ca.bundle_view(inputs / bundle)
    revision = None
    events = [e for e in c21.Ledger(out / "ledger.jsonl").entries() if e.get("event") == "stage"]
    if events:
        revision = events[-1].get("git_revision")
    baseline = None
    if stage == "R" and baseline_root is not None:
        baseline = ca.load_baseline(raw, baseline_root)
    result = ca.analyze_stage(
        raw,
        stage,
        runs,
        families,
        expected,
        bundle_hashes={
            c: raw["inputs"][b]["bundle_hash"] for c, b in raw["stage_bundles"][stage].items()
        },
        stage_revision=revision,
        baseline=baseline,
    )
    result["problems"] = problems + result["problems"]
    result["raw_sha256"] = ca.raw_sums(runs, out)
    result["analysis_source"] = {
        "schedule_sha256": sha256_bytes(SCHEDULE.read_bytes()),
        "tools_sha256": {t: sha256_bytes((REPO / t).read_bytes()) for t in TOOLS},
    }
    pmset = out / "pmset-sleep-wake.txt"
    result["host"] = {
        "load_samples": len((out / "load.jsonl").read_text().splitlines())
        if (out / "load.jsonl").is_file()
        else 0,
        "pmset_lines": len(pmset.read_text().splitlines()) if pmset.is_file() else None,
    }
    return result


def verify_sums(out: Path, sums_file: Path) -> list[str]:
    """Every pinned raw file under `out` hashes to its SHA256SUMS line."""
    problems = []
    for line in sums_file.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        path = out / name
        if not path.is_file() or sha256_bytes(path.read_bytes()) != digest:
            problems.append(f"{name}: missing or differs from {sums_file.name}")
    return problems


# ----------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="r024_campaign.py", description=(__doc__ or "").split("\n")[0]
    )
    parser.add_argument("--schedule", default=str(SCHEDULE))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("profiles")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("check")
    for name in ("--stage", "--out", "--inputs", "--baseline-root"):
        p.add_argument(name)
    p = sub.add_parser("freeze")
    p.add_argument("--out", required=True)
    p = sub.add_parser("inputs")
    p.add_argument("--primary", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--keys", nargs="+", required=True)
    p = sub.add_parser("run")
    p.add_argument("--stage", required=True, choices=["T", "R"])
    p.add_argument("--inputs", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--tuning", help="the stage-T output directory (stage R's launch order)")
    p.add_argument("--lanes", type=int)
    p.add_argument("--retry-infrastructure")
    p = sub.add_parser("timing")
    p.add_argument("--inputs", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("analyze")
    p.add_argument("--stage", required=True, choices=["T", "R", "L"])
    p.add_argument("--inputs", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--json", required=True)
    p.add_argument("--sums", help="also write the raw-record SHA256SUMS here")
    p.add_argument("--baseline-root", help="the artifacts root holding the 0.2.2 records (C15)")
    p = sub.add_parser("tables")
    p.add_argument("--analysis", nargs="+", required=True, metavar="STAGE=JSON")
    p.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        raw = load_raw(Path(args.schedule))
        if args.command == "profiles":
            texts = {
                profile_path(raw, k): render_text(raw, k, d, w)
                for k, (d, w) in profiles(raw).items()
            }
            texts.update(timing_files(raw))
            for path, text in texts.items():
                if args.write:
                    (REPO / path).parent.mkdir(parents=True, exist_ok=True)
                    (REPO / path).write_text(text, encoding="utf-8")
                print(f"{path} {sha256_bytes(text.encode())}")
            return 0
        if args.command == "check":
            problems = check(raw)
            if args.out:
                if not (args.stage and args.inputs):
                    raise CampaignError("check --out needs --stage and --inputs")
                baseline = Path(args.baseline_root) if args.baseline_root else None
                problems += analyze(raw, args.stage, Path(args.inputs), Path(args.out), baseline)[
                    "problems"
                ]
            for problem in problems:
                print(problem, file=sys.stderr)
            print(f"check: {len(problems)} problem(s)")
            return 1 if problems else 0
        if args.command == "freeze":
            Path(args.out).write_text(
                json.dumps(freeze_record(raw), indent=1, sort_keys=True) + "\n"
            )
            print(f"freeze record written: {args.out}")
            return 0
        if args.command == "inputs":
            result = prepare_inputs(raw, Path(args.primary), Path(args.root), args.keys)
            print(json.dumps(result, indent=1))
            return 0
        if args.command == "run":
            problems = check(raw)
            if problems:
                raise CampaignError(f"check reports {len(problems)} problem(s): {problems[:3]}")
            campaign = build(raw, args.stage, Path(args.tuning) if args.tuning else None)
            return int(
                pc.execute(
                    campaign,
                    args.stage,
                    inputs=Path(args.inputs),
                    out=Path(args.out),
                    lanes=args.lanes,
                    retry_infrastructure=args.retry_infrastructure,
                )
            )
        if args.command == "timing":
            problems = check(raw)
            if problems:
                raise CampaignError(f"check reports {len(problems)} problem(s): {problems[:3]}")
            revision, dirty, _ = c21.git_provenance(REPO)
            if dirty:
                raise CampaignError(
                    "the working tree is dirty: the measured source must be a commit"
                )
            return TimingStage(raw, Path(args.inputs), Path(args.out)).run()
        if args.command == "analyze":
            if args.stage == "L":
                result = ca.analyze_timing(raw, Path(args.out))
            else:
                baseline = Path(args.baseline_root) if args.baseline_root else None
                result = analyze(raw, args.stage, Path(args.inputs), Path(args.out), baseline)
            Path(args.json).write_text(
                json.dumps(result, indent=1, sort_keys=True, default=str) + "\n"
            )
            if args.sums:
                lines = [f"{d}  {n}\n" for n, d in result["raw_sha256"].items()]
                Path(args.sums).write_text("".join(lines), encoding="utf-8")
            print(f"analysis: {len(result['problems'])} problem(s) -> {args.json}")
            return 1 if result["problems"] else 0
        if args.command == "tables":
            analyses = {}
            for item in args.analysis:
                stage, _, path = item.partition("=")
                analyses[stage] = json.loads(Path(path).read_text(encoding="utf-8"))
            Path(args.out).write_text(ca.render_tables(raw, analyses), encoding="utf-8")
            print(f"wrote {args.out}")
            return 0
    except (CampaignError, ProfileError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

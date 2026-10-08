"""WHI-1602 research-022 campaign: schedule check, profile rendering, stage execution, the A5 budget
rule, analysis and the results tables (R022-Q06; pruning contract R022-Q02/1 §11, §14).

A thin driver over the ordinary CLI -- it never solves anything itself. It reuses the 0.2.1 executor
by import (`tools/research_021/campaign.py`: ledger, load sampler, lanes, `caffeinate`,
`pmset` capture, the infrastructure-retry rule; that file is never modified) and adds what
0.2.2 needs: a schedule
expanded from `config/research_022/schedule.yaml`, profiles generated from the unchanged canonical
bases' `--strategies all` derivation, the untimed work pass, the L01 protocol/arms files of stage
L, a launch policy that WAITS for a quiet host, and the §11 analysis (`pruning_analysis.py`).

    uv run python tools/research_022/pruning_campaign.py check
    uv run python tools/research_022/pruning_campaign.py profiles [--write]
    uv run python tools/research_022/pruning_campaign.py schedule --stage T
    uv run python tools/research_022/pruning_campaign.py inputs --primary <clone> --root <root>
    uv run python tools/research_022/pruning_campaign.py execute --stage T \\
        --inputs <root>/inputs --out <dir>
    uv run python tools/research_022/pruning_campaign.py a5-values --tuning <T dir> [--write]
    uv run python tools/research_022/pruning_campaign.py freeze --out <file>
    uv run python tools/research_022/pruning_campaign.py analyze --stage T \\
        --inputs <root>/inputs --out <dir>
    uv run python tools/research_022/pruning_campaign.py tables --analysis T=<json> R=<json> \\
        --out <md>
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
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _p in (str(REPO), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pruning_analysis as pa  # noqa: E402

from benchmark.profile import (  # noqa: E402
    ProfileError,
    parse_profile,
    read_profile_document,
)
from benchmark.results import load_case_records, load_manifest  # noqa: E402
from benchmark.strategies import (  # noqa: E402
    R021_ADDITIONS,
    R022_ADDITIONS,
    derive,
    effective_document,
    frozen_roster,
)


def _load_021() -> ModuleType:
    """The 0.2.1 campaign executor, by path (never copied, never modified)."""
    path = REPO / "tools" / "research_021" / "campaign.py"
    name = "r021_campaign"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


c21: Any = _load_021()
r021: Any = sys.modules["analysis"]  # imported by the 0.2.1 executor under this name

MANIFEST_PATH = REPO / "config" / "research_022" / "schedule.yaml"
PROFILE_DIR = "config/research_022/profiles"
SCHEMA = "r022.campaign/1"
WORK_SCRIPT = "tools/research_022/pruning_work.py"
KINDS = ("run", "quote", "report", "replay", "order_check", "l01_run", "l01_compare")
# WHI-1632 (R024-C/1 §6.4): the registered `all17` is the 0.2.1 + 0.2.2 additions; every derivation
# and registered resolved identity of this campaign uses exactly this frozen roster.
FROZEN_ADDITIONS = (*R021_ADDITIONS, *R022_ADDITIONS)


class CampaignError(ValueError):
    """The schedule, inputs or request are refused; nothing was executed or written."""


# ----------------------------------------------------------------------------- schedule


def _short(bundle: str) -> str:
    return bundle.split("_", 1)[1] if "_" in bundle else bundle


def a5_profiles(raw: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The generated A5 profiles, only once `a5.values` is filled: key -> profile entry."""
    a5 = raw.get("a5") or {}
    values = a5.get("values")
    out: dict[str, dict[str, Any]] = {}
    if not values:
        return out
    for group, base in a5["groups"].items():
        algorithms = raw["profiles"][base]["generate"]["algorithms"]
        for budget in a5["budgets"]:
            kind, p = str(budget["kind"]), int(budget["p"])
            key = f"a5_{group}_{'q' if kind == 'max_quotes' else 'c'}{p}"
            out[key] = {
                "path": f"{a5.get('profile_dir', PROFILE_DIR)}/{key}.yaml",
                "generate": {
                    "base": raw["profiles"][base]["generate"]["base"],
                    "algorithms": list(algorithms),
                    "budget": {kind: int(values[group][kind][f"p{p}"])},
                },
                "purpose": f"A5 {group} {kind} p{p} (binding budget; not_exact_budget_binding)",
            }
    return out


def _pair_of(algorithms: Sequence[str]) -> str | None:
    if len(algorithms) == 2 and pa.PAIRS.get(algorithms[0]) == algorithms[1]:
        return str(algorithms[0])
    return None


def expand(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The invocations of stages T, R, M from `arms` and `a5`, then the literal ones."""
    out: list[dict[str, Any]] = []
    profiles = {**raw["profiles"], **a5_profiles(raw)}
    for stage in ("T", "R"):
        for work in (False, True):
            for spec in raw["arms"]:
                if work and not spec.get("work_pass"):
                    continue
                for bundle in spec["stages"].get(stage, []):
                    for group, profile in spec["groups"].items():
                        algorithms = list(profiles[profile]["generate"]["algorithms"])
                        arm, tag = spec["arm"], "-WP" if work else ""
                        inv: dict[str, Any] = {
                            "id": f"{stage}{tag}-{arm}-{group}-{_short(bundle)}", "stage": stage,
                            "kind": "run", "bundle": bundle, "profile": profile,
                            "strategies": "profile", "algorithms": algorithms, "arm": arm,
                            "group": group, "pair": _pair_of(algorithms),
                            "work_pass": work, "exposure": None,
                        }  # fmt: skip
                        if spec.get("report") and stage == "R" and not work:
                            inv["derive"] = ["report"]
                        out.append(inv)
    a5 = raw.get("a5") or {}
    if a5.get("values"):
        for split_stage, bundle in (("T", "tuning_full"), ("R", "report_full")):
            for group in a5["groups"]:
                for budget in a5["budgets"]:
                    kind, p = str(budget["kind"]), int(budget["p"])
                    letter = "q" if kind == "max_quotes" else "c"
                    key = f"a5_{group}_{letter}{p}"
                    algorithms = list(profiles[key]["generate"]["algorithms"])
                    split = "tuning" if split_stage == "T" else "report"
                    out.append({
                        "id": f"M-A5-{group}-{letter}{p}-{split}",
                        "stage": "M", "kind": "run", "bundle": bundle, "profile": key,
                        "strategies": "profile", "algorithms": algorithms, "arm": "A5",
                        "group": group, "pair": _pair_of(algorithms), "work_pass": False,
                        "budget": budget, "split_of": split_stage,
                    })  # fmt: skip
    out += [dict(i) for i in raw["invocations"]]
    return out


def build(raw: dict[str, Any], path: Path = MANIFEST_PATH) -> Any:
    """A 0.2.1 `Campaign` object over this schedule (profiles with their generated A5 entries)."""
    if raw.get("schema") != SCHEMA:
        raise CampaignError(f"{path}: expected schema {SCHEMA}")
    raw = dict(raw)
    raw["profiles"] = {**raw["profiles"], **a5_profiles(raw)}
    invocations = c21._expand(expand(raw))
    campaign = c21.Campaign(raw, invocations, path)
    seen: set[str] = set()
    for inv in invocations:
        if inv.id in seen:
            raise CampaignError(f"duplicate invocation id {inv.id}")
        if inv.stage not in c21.STAGES or inv.kind not in KINDS:
            raise CampaignError(f"{inv.id}: stage/kind {inv.stage}/{inv.kind} not registered")
        for dep in inv.depends:
            if dep not in seen:
                raise CampaignError(f"{inv.id}: dependency {dep} is not registered before it")
        seen.add(inv.id)
        campaign.by_id[inv.id] = inv
        if inv.kind in ("run", "quote"):
            if str(inv.get("bundle")) not in raw["inputs"]:
                raise CampaignError(f"{inv.id}: unknown bundle {inv.get('bundle')}")
            if str(inv.get("profile")) not in raw["profiles"]:
                raise CampaignError(f"{inv.id}: unknown profile {inv.get('profile')}")
            if inv.get("strategies") not in ("all", "profile"):
                raise CampaignError(f"{inv.id}: strategies must be all|profile")
    return campaign


def load_schedule(path: Path | str = MANIFEST_PATH) -> Any:
    path = Path(path)
    return build(yaml.safe_load(path.read_text(encoding="utf-8")), path)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def profile_path(campaign: Any, key: str) -> str:
    return str(campaign.raw["profiles"][key]["path"])


# ----------------------------------------------------------------------------- profiles


def _pinned(campaign: Any, key: str) -> dict[str, Any]:
    spec = campaign.raw["profiles"][key]
    if "sha256" not in spec:
        raise CampaignError(f"profile {key}: a generation base must be a pinned file")
    data = (REPO / spec["path"]).read_bytes()
    if sha256_bytes(data) != spec["sha256"]:
        raise CampaignError(f"profile {key}: {spec['path']} sha256 differs from the pin")
    return read_profile_document(REPO / spec["path"])


def roster_document(campaign: Any, base_key: str) -> dict[str, Any]:
    """The base's `--strategies all` derivation: the full roster with every strategy's values."""
    spec = campaign.raw["profiles"][base_key]
    with frozen_roster(FROZEN_ADDITIONS):
        return effective_document(_pinned(campaign, base_key), "all",
                                  source_path=str(spec["path"]),
                                  source_sha256=str(spec["sha256"]))  # fmt: skip


def restrict(doc: Mapping[str, Any], algorithms: Sequence[str]) -> dict[str, Any]:
    """`doc` (a derived roster document) with only `algorithms`: their `strategies` and
    `algorithm_options` entries stay, every other section is untouched, the `selection` record
    goes."""
    out = copy.deepcopy(dict(doc))
    unknown = [a for a in algorithms if a not in out["algorithms"]]
    if unknown:
        raise CampaignError(f"{unknown} are not in the derived roster {out['algorithms']}")
    out["algorithms"] = list(algorithms)
    out.pop("selection", None)
    for section in ("strategies", "algorithm_options"):
        kept = {k: v for k, v in (out.get(section) or {}).items() if k in algorithms}
        if kept:
            out[section] = kept
        else:
            out.pop(section, None)
    return out


def render_profile(campaign: Any, key: str) -> str:
    spec = campaign.raw["profiles"][key]
    gen = spec.get("generate")
    if not isinstance(gen, Mapping):
        raise CampaignError(f"profile {key} is not generated")
    base = campaign.raw["profiles"][str(gen["base"])]
    doc = restrict(roster_document(campaign, str(gen["base"])), gen["algorithms"])
    for algorithm, change in (gen.get("options") or {}).items():
        options = doc.get("algorithm_options", {}).get(algorithm)
        if options is None:
            raise CampaignError(f"{key}: {algorithm} has no algorithm_options to change")
        for name, value in (change.get("set") or {}).items():
            if name not in options:
                raise CampaignError(f"{key}: {algorithm} has no option {name!r}")
            options[name] = value
    if gen.get("budget"):
        doc["budget"] = {**doc["budget"], **gen["budget"]}
    parse_profile(json.loads(json.dumps(doc)), spec["path"])  # refused exactly as the CLI would
    header = [
        f"# WHI-1602 research-022 campaign profile `{key}` (R022-Q06). GENERATED by",
        "# `uv run python tools/research_022/pruning_campaign.py profiles --write` from the",
        f"# `--strategies all` derivation of {base['path']}",
        f"# (sha256 {base['sha256']}) restricted to",
        "# the listed algorithms; do not edit by hand -- `pruning_campaign.py check` refuses any",
        "# drift from this rendering.",
        f"# Purpose: {spec.get('purpose', '')}",
        "# Every value not listed in config/research_022/schedule.yaml `profiles."
        f"{key}.generate` is the derived roster's. Replay literally with --strategies profile.",
    ]
    return "\n".join(header) + "\n" + yaml.safe_dump(doc, sort_keys=False)


def generated_profiles(campaign: Any) -> list[str]:
    return [k for k, s in campaign.raw["profiles"].items() if "generate" in s]


# ----------------------------------------------------------------------------- latency files


def render_protocol(campaign: Any) -> str:
    """`config/latency/l01.yaml` with only `key` and the pinned `profile` replaced."""
    spec = campaign.raw["latency"]["protocol"]
    source = (REPO / spec["source"]).read_bytes()
    if sha256_bytes(source) != spec["source_sha256"]:
        raise CampaignError(f"{spec['source']} differs from its pin")
    doc = yaml.safe_load(source)
    profile_file = REPO / profile_path(campaign, spec["profile"])
    doc["key"] = spec["key"]
    doc["profile"] = {
        "path": profile_path(campaign, spec["profile"]),
        "sha256": sha256_bytes(profile_file.read_bytes()),
    }
    header = (
        "# WHI-1602 research-022 latency protocol L01-R022 (R022-Q06; contract §11.5).\n"
        "# GENERATED by\n"
        "# `pruning_campaign.py profiles --write` from config/latency/l01.yaml (sha256\n"
        f"# {spec['source_sha256']}): only `key` and the pinned `profile` (the six pair\n"
        "# strategies over the unchanged config/full_gross.yaml values) differ -- the matrix,\n"
        "# cohorts, timing, cold, load and acceptance rules are L01's, verbatim.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False)


def render_arms(campaign: Any, protocol_text: str) -> str:
    lat = campaign.raw["latency"]
    pairs = lat["pairs"]
    sufficient = lat["sufficient_budget"]
    refs = list(pairs.values())
    doc = {
        "schema": "latency-arms/1", "key": lat["arms"]["key"], "version": 1,
        "protocol": {"path": lat["protocol"]["path"],
                     "sha256": sha256_bytes(protocol_text.encode())},
        "sufficient_budget": {"path": sufficient["path"], "sha256": sufficient["sha256"]},
        "source": "every_arm_same_clean_commit",
        "controls": {},
        "arms": [
            {"name": "REF", "algorithms": refs, "controls": [], "stages": ["timing", "cold"]},
            {"name": "BND", "algorithms": list(pairs), "controls": [],
             "stages": ["timing", "cold"]},
        ],
        "comparisons": [{"id": "R022-pairs", "lane": "heuristic", "baseline": "REF",
                         "candidate": "BND", "role": "decision", "pairs": dict(pairs)}],
        "dispositions": {
            "exact": {"adopt_eligible": "not used", "reject": "rejected",
                      "inconclusive": "not adopted (inconclusive)"},
            "heuristic": {"opt_in_only": "not used (exactness is the contract's own gate)",
                          "reject": "rejected", "inconclusive": "not adopted (inconclusive)"},
        },
    }  # fmt: skip
    header = (
        "# WHI-1602 research-022 latency arms (R022-Q06): the references (REF) against the\n"
        "# bounded\n"
        "# strategies (BND) under L01-R022. GENERATED by `pruning_campaign.py profiles --write`.\n"
        "# `report.latency compare` pairs different IDs only in its heuristic lane; the lane\n"
        "# label\n"
        "# carries no quality claim here -- exactness is the contract's §11.2 gate, run\n"
        "# separately.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False)


# ----------------------------------------------------------------------------- check


def check(campaign: Any) -> list[str]:
    """Every problem between the schedule, the checked-in files and the code (empty = ok)."""
    problems: list[str] = []
    raw = campaign.raw
    for key, spec in raw["profiles"].items():
        if "sha256" in spec:
            data = (REPO / spec["path"]).read_bytes()
            if sha256_bytes(data) != spec["sha256"]:
                problems.append(f"profile {key}: {spec['path']} sha256 differs from the pin")
            continue
        path = REPO / spec["path"]
        try:
            rendered = render_profile(campaign, key)
        except (CampaignError, ProfileError) as exc:
            problems.append(f"profile {key}: {exc}")
            continue
        if not path.is_file() or path.read_text(encoding="utf-8") != rendered:
            problems.append(f"profile {key}: {spec['path']} differs from its rendering")
    # the roster
    try:
        gross = roster_document(campaign, "canonical_gross")
    except (CampaignError, ProfileError) as exc:
        return [*problems, f"the roster does not derive: {exc}"]
    if list(gross["algorithms"]) != list(raw["rosters"]["all17"]):
        problems.append(f"`--strategies all` derives {gross['algorithms']}, registered all17")
    # every A1 / B14 group is a slice of that roster, each ID exactly once
    covered: Counter[str] = Counter()
    for spec in raw["arms"]:
        if spec["arm"] in ("A1", "B14"):
            for profile in spec["groups"].values():
                covered.update(raw["profiles"][profile]["generate"]["algorithms"])
    if sorted(covered) != sorted(raw["rosters"]["all17"]) or any(v != 1 for v in covered.values()):
        problems.append(f"A1 + B14 groups do not cover the 17 IDs exactly once: {dict(covered)}")
    # a generated profile differs from the derived roster only where its spec says so
    for key in generated_profiles(campaign):
        gen = raw["profiles"][key]["generate"]
        base_doc = roster_document(campaign, str(gen["base"]))
        try:
            doc = read_profile_document(REPO / raw["profiles"][key]["path"])
        except (OSError, ProfileError):
            continue  # already reported as a rendering problem
        for section in base_doc:
            if section in ("algorithms", "strategies", "algorithm_options", "selection"):
                continue
            expect = (
                {**base_doc[section], **gen["budget"]}
                if (section == "budget" and gen.get("budget"))
                else base_doc[section]
            )
            if doc.get(section) != expect:
                problems.append(f"profile {key}: section {section} differs from the derived roster")
        for algorithm in doc["algorithms"]:
            want = (
                dict(base_doc["algorithm_options"].get(algorithm, {}))
                if "algorithm_options" in base_doc
                else {}
            )
            want.update(((gen.get("options") or {}).get(algorithm) or {}).get("set") or {})
            if dict((doc.get("algorithm_options") or {}).get(algorithm, {})) != want:
                problems.append(
                    f"profile {key}: {algorithm} options differ from the derived roster"
                )
            if algorithm in (base_doc.get("strategies") or {}) and (
                (doc.get("strategies") or {}).get(algorithm) != base_doc["strategies"][algorithm]
            ):  # fmt: skip
                problems.append(
                    f"profile {key}: {algorithm} recipe differs from the derived roster"
                )
    # a pair runs the same options under both keys (A3/A4 `set`s must stay identical)
    for key in generated_profiles(campaign):
        doc_algorithms = raw["profiles"][key]["generate"]["algorithms"]
        pair = _pair_of(doc_algorithms)
        if pair is None:
            continue
        sets = raw["profiles"][key]["generate"].get("options") or {}
        if pair == "metis_history" and sets and (
            sets.get("metis_history") != sets.get("metis_history_bounded")
        ):  # fmt: skip
            problems.append(f"profile {key}: the pair's option sets differ")
    # invocations
    for inv in campaign.invocations:
        if inv.kind not in ("run", "quote"):
            continue
        path = profile_path(campaign, str(inv.get("profile")))
        try:
            source = read_profile_document(REPO / path)
            with frozen_roster(FROZEN_ADDITIONS):
                document, _ = derive(source, str(inv.get("strategies")), source_path=path,
                                     source_sha256="0" * 64)  # fmt: skip
        except (ProfileError, OSError) as exc:
            problems.append(f"{inv.id}: profile {inv.get('profile')} does not derive: {exc}")
            continue
        if list(document["algorithms"]) != inv.algorithms:
            problems.append(f"{inv.id}: derives {document['algorithms']}, registered "
                            f"{inv.algorithms}")  # fmt: skip
    # latency files
    lat = raw["latency"]
    try:
        protocol = render_protocol(campaign)
        arms = render_arms(campaign, protocol)
        for spec, text in ((lat["protocol"], protocol), (lat["arms"], arms)):
            path = REPO / spec["path"]
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                problems.append(f"{spec['path']} differs from its rendering")
        sufficient = REPO / lat["sufficient_budget"]["path"]
        if sha256_bytes(sufficient.read_bytes()) != lat["sufficient_budget"]["sha256"]:
            problems.append("the sufficient-budget protocol differs from its pin")
    except (CampaignError, OSError, ProfileError) as exc:
        problems.append(f"latency files: {exc}")
    return problems


# ----------------------------------------------------------------------------- inputs


def prepare_inputs(campaign: Any, primary: Path, root: Path) -> dict[str, Any]:
    """Copy every registered input into `root` (outside any worktree), hash-verified."""
    out: dict[str, Any] = {}
    for key, spec in campaign.raw["inputs"].items():
        target = root / key
        source = (REPO if spec.get("in_repository") else primary) / spec["source"]
        if not target.exists():
            shutil.copytree(source, target)
        digest = sha256_bytes((target / "manifest.json").read_bytes())
        if digest != spec["bundle_hash"]:
            raise CampaignError(f"input {key}: {target} bundle hash {digest} != registered")
        out[key] = {"path": str(target), "bundle_hash": digest}
    return out


# ----------------------------------------------------------------------------- execution


def argv_for(inv: Any, ctx: Any) -> list[str]:
    """The command of an invocation: the 0.2.1 commands for run / quote / report / replay /
    order-check, plus the work pass (`pruning_work.py`) and the two stage-L commands."""
    raw, py = inv.raw, list(ctx.launcher)
    if inv.kind == "run" and raw.get("work_pass"):
        argv = c21_argv_for(inv, ctx)
        argv[len(py)] = WORK_SCRIPT  # `main.py run ...` -> `pruning_work.py run ...`
        return argv  # type: ignore[no-any-return]
    if inv.kind == "l01_run":
        lat = ctx.campaign.raw["latency"]
        return [*py, "-m", "benchmark.latency", "run", "--protocol", lat["protocol"]["path"],
                "--arms", lat["arms"]["path"], "--arm", str(raw["arm"]), "--bundle",
                ctx.bundle(str(raw["bundle"])), "--out", str(ctx.slot(inv.id))]  # fmt: skip
    if inv.kind == "l01_compare":
        slot = ctx.slot(inv.id)
        pairs = [f"{c}={r}" for c, r in ctx.campaign.raw["latency"]["pairs"].items()]
        argv = [
            *py,
            "-m",
            "report.latency",
            "compare",
            str(ctx.dir_of(str(raw["baseline"]))),
            str(ctx.dir_of(str(raw["candidate"]))),
            "--lane",
            "heuristic",
        ]
        for pair in pairs:
            argv += ["--pair", pair]
        return [*argv, "--json", str(slot / "compare.json"), "--markdown", str(slot / "compare.md")]
    return c21_argv_for(inv, ctx)  # type: ignore[no-any-return]


c21_argv_for = c21.argv_for


def launch_window(window: Mapping[str, Any], ledger: Any, echo: Any) -> bool:
    """The stage-L launch policy (schedule `stages.L.launch_window`): five 1-minute load samples
    `interval_seconds` apart, none above `headroom_load1` -> launch; otherwise wait
    `resample_every_seconds` and sample again, up to `max_wait_seconds` in total, then do NOT launch
    (timing is `inconclusive`). Every group of samples is recorded in the ledger."""
    started = time.time()
    attempt = 0
    awake = subprocess.Popen(["caffeinate", "-i", "-m", "-s", "-w", str(os.getpid())])
    ledger.append({"event": "launch_wait_caffeinate", "pid": awake.pid})
    try:
        while True:
            attempt += 1
            samples: list[dict[str, float]] = []
            for index in range(int(window["samples"])):
                if index:
                    time.sleep(float(window["interval_seconds"]))
                samples.append({"t": time.time(), "load1": os.getloadavg()[0]})
            peak = max(s["load1"] for s in samples)
            busy = peak > float(window["headroom_load1"])
            ledger.append({"event": "launch_gate", "attempt": attempt, "samples": samples,
                           "max_load1": peak, "headroom_load1": window["headroom_load1"],
                           "busy": busy, "waiver": None, "launched": not busy})  # fmt: skip
            if not busy:
                return True
            waited = time.time() - started
            if waited + float(window["resample_every_seconds"]) > float(window["max_wait_seconds"]):
                ledger.append({"event": "stage_not_launched", "attempts": attempt,
                               "waited_seconds": waited,
                               "reason": "no quiet window within max_wait_seconds"})  # fmt: skip
                echo(f"launch window: not launched after {attempt} attempt(s), {waited:.0f} s")
                return False
            echo(f"launch window: attempt {attempt} max load {peak:.2f} > "
                 f"{window['headroom_load1']}; waiting")  # fmt: skip
            time.sleep(float(window["resample_every_seconds"]))
    finally:
        awake.terminate()
        awake.wait(timeout=10)


def execute(campaign: Any, stage: str, **kwargs: Any) -> int:
    """`c21.execute` with this schedule's commands; stage L first waits for a quiet window."""
    c21.argv_for = argv_for
    echo = kwargs.get("echo", print)
    window = campaign.raw["stages"][stage].get("launch_window")
    if window and not kwargs.get("retry_infrastructure"):
        out: Path = kwargs["out"]
        out.mkdir(parents=True, exist_ok=True)
        ledger = c21.Ledger(out / "ledger.jsonl")
        if not launch_window(window, ledger, echo):
            return 3
    return int(c21.execute(campaign, stage, **kwargs))


# ----------------------------------------------------------------------------- A5


def a5_values(campaign: Any, tuning: Path) -> dict[str, Any]:
    """The registered A5 rule applied to the reference records of `T-A1-<group>-full`."""
    done = c21.read_ledger(tuning)
    out: dict[str, Any] = {}
    for group in campaign.raw["a5"]["groups"]:
        inv_id = f"T-A1-{group}-full"
        entry = done.get(inv_id)
        if not entry or entry.get("result") != "ok" or not entry.get("run_dir"):
            raise CampaignError(f"{inv_id}: no completed run in {tuning}")
        inv = campaign.by_id[inv_id]
        reference = inv.algorithms[0]
        records = [r for r in load_case_records(entry["run_dir"]) if r["algorithm"] == reference]
        rule = pa.budget_values(records)
        out[group] = {"reference": reference, "run": entry["run_dir"],
                      "max_quotes": {f"p{p}": rule["max_quotes"][f"p{p}"] for p in (25, 50)},
                      "max_candidates": {
                f"p{p}": rule["max_candidates"][f"p{p}"] for p in (25, 50)
            },
                      "cells": rule["cells"]}  # fmt: skip
    return out


def write_a5_values(path: Path, values: Mapping[str, Any]) -> None:
    """Replace the `values: null` line of the schedule by the computed values (flow style)."""
    text = path.read_text(encoding="utf-8")
    lines = ["  values:"]
    for group, v in values.items():
        flow = {k: v[k] for k in ("max_quotes", "max_candidates")}
        lines.append(f"    {group}: {json.dumps(flow)}")
    new, count = re.subn(r"^  values: null.*$", "\n".join(lines), text, flags=re.M)
    if count != 1:
        raise CampaignError("a5.values is not `null`: the values are written once")
    path.write_text(new, encoding="utf-8")


# ----------------------------------------------------------------------------- freeze


def resolved_identity(campaign: Any, inv: Any) -> dict[str, Any] | None:
    """The 0.2.1 executor's registered resolved identity, derived with this campaign's frozen
    roster (WHI-1632)."""
    with frozen_roster(FROZEN_ADDITIONS):
        return c21.resolved_identity(campaign, inv)  # type: ignore[no-any-return]


def _file_pin(path: str) -> dict[str, str]:
    return {"path": path, "sha256": sha256_bytes((REPO / path).read_bytes())}


def freeze_record(campaign: Any) -> dict[str, Any]:
    """The pins of everything a stage executes from: schedule, tools, profiles, latency files, the
    registered effective settings of every invocation and the stage inventories."""
    stages = ("T", "R", "M", "I", "L")
    return {
        "schema": "r022.campaign-freeze/1", "issue": campaign.raw.get("issue"),
        "contract": campaign.raw.get("contract"),
        "files": {
            "schedule": _file_pin(str(campaign.path.relative_to(REPO))),
            **{f"tool:{n}": _file_pin(f"tools/research_022/{n}") for n in
               ("pruning_campaign.py", "pruning_analysis.py", "pruning_work.py")},
            "tool:r021_campaign": _file_pin("tools/research_021/campaign.py"),
            "tool:r021_analysis": _file_pin("tools/research_021/analysis.py"),
            **{
                f"profile:{k}": _file_pin(profile_path(campaign, k))
                for k in campaign.raw["profiles"]
            },
            "latency:protocol": _file_pin(campaign.raw["latency"]["protocol"]["path"]),
            "latency:arms": _file_pin(campaign.raw["latency"]["arms"]["path"]),
            "latency:l01": _file_pin("config/latency/l01.yaml"),
            "metis_m4_settings": _file_pin("config/metis_challenge/m4.yaml"),
            "metis_history_preset": _file_pin("config/metis_history/preset_v1.yaml"),
            "metis_history_bounded_preset": _file_pin(
                "config/metis_history_bounded/preset_v1.yaml"
            ),
            "cost_model": _file_pin("config/costs/mantle-101082044-cost-v1.json"),
        },
        "a5": campaign.raw.get("a5"),
        "rules_sha256": sha256_bytes(
            json.dumps(campaign.raw.get("rules"), sort_keys=True).encode()
        ),
        "effective_settings": {
            inv.id: ident for inv in campaign.invocations
            if (ident := resolved_identity(campaign, inv)) is not None
        },
        "inventory": {s: c21.schedule(campaign, s) for s in stages},
    }  # fmt: skip


# ----------------------------------------------------------------------------- analysis


class RunData:
    """One run: manifest, records by (algorithm, case id), case order."""

    def __init__(self, run_dir: str | Path) -> None:
        self.dir = str(run_dir)
        self.manifest = load_manifest(run_dir)
        self.records = {(r["algorithm"], r["case_id"]): r for r in load_case_records(run_dir)}
        self.case_ids: list[str] = list(self.manifest.measurement["case_order"])

    def of(self, algorithm: str) -> dict[str, Any]:
        return {c: r for (a, c), r in self.records.items() if a == algorithm}


def _case_ids(inputs: Path, bundle: str) -> list[str]:
    text = (inputs / bundle / "cases.jsonl").read_text(encoding="utf-8")
    return [json.loads(x)["case_id"] for x in text.splitlines() if x.strip()]


def _pool_families(inputs: Path, bundle: str) -> dict[str, str]:
    """pool id -> family; a pool entry without a `family` key is constant-product."""
    data = json.loads((inputs / bundle / "pools.json").read_text(encoding="utf-8"))
    return {p["pool_id"]: str(p.get("family") or "constant_product") for p in data["pools"]}


def _families(
    campaign: Any, inputs: Path, bundle: str, refs: Mapping[str, Any], case_ids: Sequence[str]
) -> dict[str, dict[str, str]]:
    cohort = str(campaign.raw["inputs"][bundle]["cohort"])
    pools = _pool_families(inputs, bundle)
    return {c: pa.family_keys(c, cohort, refs.get(c), pools) for c in case_ids}


def _baseline_runs(campaign: Any, root: Path) -> dict[str, RunData]:
    """The 0.2.1 baseline runs (hash-verified); `root` holds the artifacts directory."""
    base = campaign.raw["baseline_021"]
    out: dict[str, RunData] = {}
    for label in ("report_full", "report_sor"):
        spec = base[label]
        run = root / base["root_relative"] / spec["slot"] / spec["run"]
        for name, pin in (("manifest.json", "manifest_sha256"), ("cases.jsonl", "cases_sha256")):
            if sha256_bytes((run / name).read_bytes()) != spec[pin]:
                raise CampaignError(f"baseline {label}: {run / name} differs from its pin")
        out[label] = RunData(run)
    return out


def _status_counts(run: RunData) -> dict[str, dict[str, int]]:
    out: dict[str, Counter[str]] = {}
    for (algorithm, _), record in run.records.items():
        out.setdefault(algorithm, Counter())[str(record["status"])] += 1
    return {a: dict(sorted(c.items())) for a, c in out.items()}


def analyze(
    campaign: Any,
    stage: str,
    *,
    inputs: Path,
    out: Path,
    baseline_root: Path | None = None,
    pmset: Path | None = None,
) -> dict[str, Any]:
    """The registered analysis of a stage from its ledger and run records."""
    done = c21.read_ledger(out)
    events = c21.Ledger(out / "ledger.jsonl").entries()
    stage_events = [e for e in events if e.get("event") == "stage"]
    revision = stage_events[-1].get("git_revision") if stage_events else None
    problems: list[str] = []
    runs: dict[str, RunData] = {}
    invocations: dict[str, Any] = {}
    samples = ([json.loads(x) for x in (out / "load.jsonl").read_text().splitlines() if x]
               if (out / "load.jsonl").is_file() else [])  # fmt: skip
    pmset_path = pmset or out / "pmset-sleep-wake.txt"
    sleeps = (r021.sleep_events(pmset_path.read_text(encoding="utf-8", errors="replace"))
              if pmset_path.is_file() else None)  # fmt: skip
    cpus = (
        int(stage_events[-1].get("logical_cpus") or campaign.raw["host"]["logical_cpus"])
        if stage_events
        else int(campaign.raw["host"]["logical_cpus"])
    )
    for inv in campaign.stage(stage):
        entry, deviation = c21.effective_entry(done, inv.id)
        view: dict[str, Any] = {
            k: (entry or {}).get(k) for k in ("result", "exit_code", "run_dir", "started", "t")
        }
        if deviation:
            view["deviation"] = deviation
        invocations[inv.id] = view
        if entry is None:
            view["result"] = "never executed or still running"
            problems.append(f"{inv.id}: {view['result']}")
            continue
        if entry.get("result") != "ok":
            problems.append(f"{inv.id}: ended {entry.get('result')}")
            continue
        view["host"] = r021.host_window(
            samples,
            float(entry.get("started") or 0),
            float(entry.get("t") or 0),
            cpus,
            sleeps=sleeps,
        )
        if sleeps is None:
            view["host"]["state"] = "unknown"
        if inv.kind not in ("run", "quote", "replay") or not entry.get("run_dir"):
            continue
        run = RunData(entry["run_dir"])
        runs[inv.id] = run
        m = run.manifest
        parent = campaign.by_id.get(inv.parent) if inv.parent else None
        if inv.kind == "replay":  # a replay re-runs its parent's command: same algorithms, cases
            algorithms = list(parent.algorithms) if parent else []
            expected_cases = list(runs[parent.id].case_ids) if parent and parent.id in runs else []
        else:
            algorithms = inv.algorithms
            expected_cases = (
                run.case_ids if inv.kind == "quote" else _case_ids(inputs, str(inv.get("bundle")))
            )
        cells = {
            (a, c): (run.records[(a, c)]["status"] if (a, c) in run.records else "missing")
            for a in algorithms
            for c in expected_cases
        }
        problems += r021.reconcile(inv.id, algorithms=list(m.algorithms), case_ids=run.case_ids,
                                   expected_algorithms=algorithms,
                                   expected_case_ids=expected_cases, cells=cells,
                                   complete=m.complete)  # fmt: skip
        if inv.kind == "run" and (
            m.bundle_hash != campaign.raw["inputs"][str(inv.get("bundle"))]["bundle_hash"]
        ):
            problems.append(f"{inv.id}: bundle hash {m.bundle_hash} != registered")
        if m.git_dirty:
            problems.append(f"{inv.id}: run from a dirty tree")
        if revision and m.git_revision != revision:
            problems.append(f"{inv.id}: run revision {m.git_revision} != stage {revision}")
        identity = resolved_identity(campaign, inv)
        if identity is not None and identity["resolved_profile_sha256"] != sha256_bytes(
            json.dumps(m.resolved_profile, sort_keys=True).encode()
        ):
            problems.append(f"{inv.id}: resolved profile differs from the registered one")
        labels = campaign.raw["holdout_exposure"]
        split = campaign.raw["inputs"].get(str(inv.get("bundle")), {}).get("split")
        view["holdout_exposure"] = labels.get(str(split), "not_a_corpus_split")
        view.update(status_counts=_status_counts(run), git_revision=m.git_revision,
                    profile_sha256=m.profile_sha256, bundle_hash=m.bundle_hash,
                    replay_command=m.replay_command, cases_sha256=m.cases_sha256,
                    manifest_sha256=sha256_bytes(
                        (Path(entry["run_dir"]) / "manifest.json").read_bytes()),
                    environment={k: m.environment.get(k) for k in
                                 ("python_version", "platform", "cpu_model", "cpu_count",
                                  "dependencies")},
                    run_seconds=m.timing.get("total_seconds") if m.timing else None)  # fmt: skip
    exactness: dict[str, Any] = {}
    work: dict[str, Any] = {}
    budget: dict[str, Any] = {}
    agreement: dict[str, Any] = {}
    by_key = {
        (
            i.raw.get("stage"),
            i.raw.get("arm"),
            i.raw.get("group"),
            i.get("bundle"),
            bool(i.raw.get("work_pass")),
        ): i
        for i in campaign.stage(stage)
        if i.raw.get("arm")
    }
    for inv in campaign.stage(stage):
        if inv.id not in runs or not inv.raw.get("pair"):
            continue
        run = runs[inv.id]
        ref_id = str(inv.raw["pair"])
        bnd_id = pa.PAIRS[ref_id]
        case_ids = run.case_ids
        refs, bnds = run.of(ref_id), run.of(bnd_id)
        bundle = str(inv.get("bundle"))
        label = f"{inv.id}"
        if inv.raw.get("arm") == "A5":
            budget[label] = {
                **pa.binding_budget_view(refs, bnds, bnd_id, case_ids),
                "budget": inv.raw["budget"],
                "pair": ref_id,
                "bundle": bundle,
            }
            continue
        if not inv.raw.get("work_pass"):
            exactness[label] = {**pa.exactness(refs, bnds, bnd_id, case_ids), "arm": inv.raw["arm"],
                                "bundle": bundle, "gates": pa.gate_states(bnds, case_ids),
                                "replay": run.manifest.replay_command}  # fmt: skip
        families = _families(campaign, inputs, bundle, refs, case_ids)
        table = pa.work_table(refs, bnds, bnd_id, case_ids)
        entry = {
            **table,
            "arm": inv.raw["arm"],
            "bundle": bundle,
            "pair": ref_id,
            "work_pass": bool(inv.raw.get("work_pass")),
            "families": pa.work_by_family(refs, bnds, bnd_id, case_ids, families),
            "prepare": pa.cost_columns(run.manifest.prepare_events, ref_id, bnd_id),
        }
        work[label] = entry
        if inv.raw.get("work_pass"):
            twin = by_key.get((inv.raw["stage"], inv.raw["arm"], inv.raw["group"], bundle, False))
            problems += _work_pass_checks(inv.id, run, twin.id if twin else None, runs, agreement)
    result: dict[str, Any] = {
        "schema": "r022.campaign-analysis/1", "stage": stage,
        "analysis_source": {
            "tools_sha256": {n: sha256_bytes((HERE / n).read_bytes()) for n in
                             ("pruning_campaign.py", "pruning_analysis.py", "pruning_work.py")},
            "schedule_sha256": sha256_bytes(campaign.path.read_bytes()),
        },
        "stage_revision": revision, "reconciliation_problems": problems,
        "reconciled": not problems, "invocations": invocations, "exactness": exactness,
        "work": work, "binding_budgets": budget, "work_pass_agreement": agreement,
        "host_samples": len(samples),
        "host_sleep_transitions": None if sleeps is None else sum(
            1 for e in sleeps if e["type"] == "Sleep"),
    }  # fmt: skip
    if stage == "R" and baseline_root is not None:
        result["baseline_0_2_1"] = _baseline_view(campaign, runs, baseline_root)
    if stage == "L":
        result["latency"] = _latency_view(campaign, out, done, samples, sleeps, cpus)
    return result


def _work_pass_checks(
    inv_id: str,
    run: RunData,
    twin_id: str | None,
    runs: Mapping[str, RunData],
    agreement: dict[str, Any],
) -> list[str]:
    """The work pass must agree with its ordinary twin (deterministic outcome) and with itself
    (wrapper count == meter == `search.quotes_executed`); nothing is timed or compared on time."""
    problems: list[str] = []
    internal: list[str] = []
    for (algorithm, case_id), record in run.records.items():
        block = pa.search_of(record).get("r022_work")
        if record["status"] in pa.FAILED_STATUSES and block is None:
            continue
        if not isinstance(block, Mapping):
            internal.append(f"{algorithm}/{case_id}: no work block")
            continue
        quotes = pa.search_of(record).get("quotes_executed")
        if not (
            block["quotes_executed"] == record["quotes"]["counted"] == quotes
            or (quotes is None and block["quotes_executed"] == record["quotes"]["counted"])
        ):
            internal.append(f"{algorithm}/{case_id}: wrapper {block['quotes_executed']} / meter "
                            f"{record['quotes']['counted']} / search {quotes}")  # fmt: skip
    differing: list[str] = []
    twin = runs.get(twin_id) if twin_id else None
    if twin is not None:
        for key, record in run.records.items():
            other = twin.records.get(key)
            if other is None:
                differing.append(f"{key[0]}/{key[1]}: no ordinary record")
                continue
            a = pa.deterministic_view(_without_work(record))
            b = pa.deterministic_view(other)
            if pa.canonical(a) != pa.canonical(b):
                differing.append(f"{key[0]}/{key[1]}")
    agreement[inv_id] = {"ordinary_twin": twin_id, "internal_mismatches": internal,
                         "differs_from_twin": differing, "cells": len(run.records)}  # fmt: skip
    if internal:
        problems.append(f"{inv_id}: {len(internal)} work-pass count mismatches")
    if twin_id and twin is None:
        problems.append(f"{inv_id}: ordinary twin {twin_id} has no run")
    return problems


def _without_work(record: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(record)
    search = dict(pa.search_of(record))
    search.pop("r022_work", None)
    out["search"] = search
    return out


def _baseline_view(campaign: Any, runs: Mapping[str, RunData], root: Path) -> dict[str, Any]:
    old = _baseline_runs(campaign, root)
    references = list(campaign.raw["rosters"]["references14"])
    out: dict[str, Any] = {"pins": campaign.raw["baseline_021"]}
    for label in ("report_full", "report_sor"):
        new: dict[tuple[str, str], Any] = {}
        for inv in campaign.stage("R"):
            if inv.id in runs and inv.get("bundle") == label and not inv.raw.get("work_pass") \
                    and inv.raw.get("arm") in ("A1", "B14"):  # fmt: skip
                for key, record in runs[inv.id].records.items():
                    if key[0] in references:
                        new[key] = record
        out[label] = pa.baseline_compare(new, old[label].records, references)
    return out


def _latency_view(
    campaign: Any,
    out: Path,
    done: Mapping[str, Any],
    samples: Sequence[Any],
    sleeps: Sequence[Any] | None,
    cpus: int,
) -> dict[str, Any]:
    """Stage L: the gate evidence and, only if every window is clean, the comparison's verdicts."""
    view: dict[str, Any] = {
        "gate": [
            e
            for e in c21.Ledger(out / "ledger.jsonl").entries()
            if e.get("event") in ("launch_gate", "stage_not_launched")
        ]
    }
    windows = {}
    # the measuring invocations; L-cmp only reads their records (it measures nothing)
    for inv_id in ("L-ref", "L-bnd", "L-q1", "L-q2", "L-q3", "L-q4", "L-q5"):
        entry = done.get(inv_id)
        if entry and entry.get("result") == "ok":
            windows[inv_id] = r021.host_window(
                samples,
                float(entry.get("started") or 0),
                float(entry.get("t") or 0),
                cpus,
                sleeps=sleeps,
            )
    experiments: dict[str, Any] = {}
    for inv_id in ("L-ref", "L-bnd"):
        slot = out / inv_id
        children = [p for p in slot.iterdir() if p.is_dir()] if slot.is_dir() else []
        if len(children) == 1 and (children[0] / "experiment.json").is_file():
            doc = json.loads((children[0] / "experiment.json").read_text())
            experiments[inv_id] = {"dir": str(children[0]), "state": doc.get("state"),
                                   "load": doc.get("load"), "source": doc.get("source"),
                                   "partial": doc.get("partial")}  # fmt: skip
    view["windows"] = windows
    view["experiments"] = experiments
    clean = (bool(windows) and sleeps is not None
             and all(w.get("state") == "clean" for w in windows.values())
             and bool(experiments)
             and all(not (e.get("load") or {}).get("contaminated") and e.get("state") == "complete"
                     for e in experiments.values()))  # fmt: skip
    view["gate_passed"] = clean
    compare = out / "L-cmp" / "compare.json"
    if compare.is_file():
        result = json.loads(compare.read_text())
        view["compare"] = {"verdict": result.get("verdict"), "reasons": result.get("reasons"),
                           "pairs": result.get("pairs"), "timing": result.get("timing"),
                           "charged": result.get("charged"),
                           "charged_costs": result.get("charged_costs"),
                           "coverage_problems": result.get("coverage_problems"),
                           "internal_checks": result.get("internal_checks")}  # fmt: skip
        mapping = {
            "faster": "improvement",
            "no_worthwhile_change": "no_difference",
            "slower": "regression",
        }
        view["per_strategy"] = (
            {}
            if not clean
            else {
                key: {
                    "verdict": mapping.get(v["verdict"], v["verdict"]),
                    "reference": v["reference"],
                    "improvement": v["improvement"],
                    "threshold": v["threshold"],
                }
                for key, v in (result.get("timing") or {}).items()
            }
        )
        if not clean:
            view["verdict"] = "inconclusive"
    else:
        view["verdict"] = "inconclusive"
    return view


# ----------------------------------------------------------------------------- tables


def disposition(analyses: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Contract §11.6 per bounded strategy from the T and R (and L) analyses."""
    out: dict[str, Any] = {}
    exact_arms = ("A1", "A2", "A3", "A4")
    for ref_id, bnd_id in pa.PAIRS.items():
        diffs = 0
        new_failures = 0
        compared = 0
        pruned_cells = 0
        have_records = True
        for stage_key in ("T", "R"):
            a = analyses.get(stage_key)
            if a is None:
                have_records = False
                continue
            if not a["reconciled"]:
                have_records = False
            for e in a["exactness"].values():
                if e["bounded"] != bnd_id or e["arm"] not in exact_arms:
                    continue
                diffs += len(e["differences"])
                new_failures += len(e["new_failure_status"])
                compared += e["compared"]
            for w in a["work"].values():
                if w["pair"] == ref_id and w["arm"] == "A1" and not w["work_pass"]:
                    stats = w["bound_cost"]["pruned_bound"]
                    pruned_cells += int(stats["sum"] or 0)
        timing = (analyses.get("L") or {}).get("latency", {})
        verdict = "inconclusive"
        if timing.get("gate_passed"):
            per = [v for k, v in (timing.get("per_strategy") or {}).items() if k.endswith(bnd_id)]
            verdict = ",".join(sorted({v["verdict"] for v in per})) or "inconclusive"
        if diffs or new_failures:
            disp = "reject"
        elif not have_records or compared == 0 or pruned_cells == 0:
            disp = "inconclusive"
        else:
            disp = "keep_experimental"
        statement = None
        if disp == "keep_experimental":
            statement = "work and latency" if verdict == "improvement" else "work reduction only"
        out[bnd_id] = {"disposition": disp, "statement": statement, "exactness_differences": diffs,
                       "new_failures": new_failures, "cells_compared": compared,
                       "pruned_bound_total_A1": pruned_cells,
                       "timing_verdict": verdict}  # fmt: skip
    return out


def _f(x: Any, digits: int = 3) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.{digits}f}"
    return f"{x:,}" if isinstance(x, int) else str(x)


def _dist(d: Mapping[str, Any]) -> str:
    return f"{_f(d['n'])} / {_f(d['p50'])} / {_f(d['p90'])} / {_f(d['max'])}"


FAMILY_UNITS = ("quotes_executed", "cl_swap_steps", "lb_bins_swapped")


def render_tables(analyses: Mapping[str, Mapping[str, Any]]) -> str:
    """Markdown of the compact evidence tables for `results.md`: exactness with its exclusion lists,
    gate states, work (sums and n / p50 / p90 / max per unit, per case family), bound cost, A5 and
    the 14-reference comparison."""
    lines: list[str] = ["# WHI-1602 compact tables (generated by `pruning_campaign.py tables`)", ""]
    for stage, a in analyses.items():
        lines += [
            f"## Stage {stage}",
            "",
            f"reconciled: {a['reconciled']} ({len(a['reconciliation_problems'])} problems); "
            f"schedule sha256 `{a['analysis_source']['schedule_sha256'][:12]}…`",
            "",
        ]
        if a["exactness"]:
            lines += [
                "### Exactness (§11.2)",
                "",
                "| run | arm | scheduled | compared | identical | excluded (truncated) | "
                "bounded cut ∧ ref not | differences | label disagrees | new failure |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
            for label, e in sorted(a["exactness"].items()):
                lines.append(
                    f"| `{label}` | {e['arm']} | {e['scheduled']} | {e['compared']} | "
                    f"{e['identical']} | {len(e['excluded_as_truncated'])} | "
                    f"{len(e['bounded_truncated_reference_not'])} | {len(e['differences'])} | "
                    f"{len(e['label_disagrees_with_truncation'])} | "
                    f"{len(e['new_failure_status'])} |"
                )
            lines.append("")
            listed = False
            for label, e in sorted(a["exactness"].items()):
                for title, key in (("excluded as truncated", "excluded_as_truncated"),
                                   (
                        "bounded truncated, reference not",
                        "bounded_truncated_reference_not",
                    ),
                                   ("DIFFERENCES", "differences")):  # fmt: skip
                    if e[key]:
                        listed = True
                        lines.append(
                            f"- `{label}` {title} ({len(e[key])}): "
                            + "; ".join(json.dumps(x, sort_keys=True) for x in e[key])
                        )
                if e["differences"]:
                    lines.append(f"  reproduce: `{e['replay']}`")
            if not listed:
                lines += [
                    "No cell is excluded as truncated, no bounded run is truncated where its "
                    "reference is not, and there is no difference.",
                    "",
                ]
            lines += ["### Gate states (re-derived from the records)", "",
                      "| run | M2 gate | M2 active | p0 true | rules | labels |",
                      "| --- | --- | ---: | ---: | --- | --- |"]  # fmt: skip
            for label, e in sorted(a["exactness"].items()):
                g = e["gates"]
                lines.append(
                    f"| `{label}` | {g['m2_gate']} | {g['m2_active']} | {g['p0_true']} | "
                    f"{g['rules']} | {g['labels']} |"
                )
            lines.append("")
        if a["work"]:
            lines += [
                "### Work (§11.4): per unit, reference vs bounded (sum; n / p50 / p90 / max)",
                "",
                "| run | unit | reference sum | reference n/p50/p90/max | bounded sum | "
                "bounded n/p50/p90/max | bounded/reference | saved |",
                "| --- | --- | ---: | --- | ---: | --- | ---: | ---: |",
            ]
            for label, w in sorted(a["work"].items()):
                for unit, row in w["units"].items():
                    lines.append(
                        f"| `{label}` | {unit} | {_f(row['reference']['sum'])} | "
                        f"{_dist(row['reference'])} | {_f(row['bounded']['sum'])} | "
                        f"{_dist(row['bounded'])} | {_f(row['ratio_bounded_over_reference'], 4)} | "
                        f"{_f(row['saved'])} |"
                    )
            lines += [
                "",
                "### Bound cost beside the savings (never netted)",
                "",
                "| run | pruned_bound | bound_evaluations | bound_no_bound | bound_table_cost | "
                "skipped / candidates_considered | skipped / reference unit | cases with skips | "
                "prepare s (reference; bounded) |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
            for label, w in sorted(a["work"].items()):
                if w["work_pass"]:
                    continue
                c = w["bound_cost"]
                pre = w["prepare"]
                considered = w["skipped_fraction_of_candidates_considered"]
                unit_key = next(k for k in w if k.startswith("skipped_fraction_of_reference_"))
                same = w[unit_key]
                lines.append(
                    f"| `{label}` | {_f(c['pruned_bound']['sum'])} | "
                    f"{_f(c['bound_evaluations']['sum'])} | {_f(c['bound_no_bound']['sum'])} | "
                    f"{_f(c['bound_table_cost']['sum'])} | {_f(considered['sum_over_sum'], 4)} | "
                    f"{_f(same['sum_over_sum'], 4)} "
                    f"({unit_key.removeprefix('skipped_fraction_of_reference_')}) | "
                    f"{considered['cases_with_skips']}/{considered['cases']} | "
                    f"{[round(x, 4) for x in pre['prepare_seconds_reference']]}; "
                    f"{[round(x, 4) for x in pre['prepare_seconds_bounded']]} |"
                )
            lines += [
                "",
                "### Work by case family (work-pass runs; sum, then n / p50 / p90 / max per case)",
                "",
                "| run | family | value | cases | unit | reference sum | reference n/p50/p90/max | "
                "bounded sum | bounded n/p50/p90/max | ratio |",
                "| --- | --- | --- | ---: | --- | ---: | --- | ---: | --- | ---: |",
            ]
            for label, w in sorted(a["work"].items()):
                if not w["work_pass"]:
                    continue
                for kind in ("cohort", "direct", "mix", "stratum"):
                    for value, table in w["families"][kind].items():
                        for unit in FAMILY_UNITS:
                            row = table["units"].get(unit)
                            if row is not None:
                                lines.append(
                                    f"| `{label}` | {kind} | {value} | {table['cases']} | {unit} | "
                                    f"{_f(row['reference']['sum'])} | {_dist(row['reference'])} | "
                                    f"{_f(row['bounded']['sum'])} | {_dist(row['bounded'])} | "
                                    f"{_f(row['ratio_bounded_over_reference'], 4)} |"
                                )
            lines.append("")
        if a["binding_budgets"]:
            lines += [
                "### A5 binding budgets (not exact; reported apart)",
                "",
                "| run | budget | cells | truncation (reference/bounded) | bounded score higher / "
                "equal / lower | bounded cut ∧ reference not |",
                "| --- | --- | ---: | --- | --- | ---: |",
            ]
            for label, b in sorted(a["binding_budgets"].items()):
                s = b["score_vs_reference"]
                lines.append(
                    f"| `{label}` | {b['budget']} | {b['cells']} | {b['truncation']} | "
                    f"{s['higher']} / {s['equal']} / {s['lower']} | "
                    f"{len(b['bounded_truncated_reference_not'])} |"
                )
            lines.append("")
        if "baseline_0_2_1" in a:
            lines += [
                "### 14 references vs the 0.2.1 baseline records",
                "",
                "| bundle | algorithm | cells | outcome+search identical | outcome differing | "
                "search differing | missing | research block differs (cells) |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
            for label in ("report_full", "report_sor"):
                for algorithm, v in a["baseline_0_2_1"][label].items():
                    lines.append(
                        f"| {label} | `{algorithm}` | {v['cells']} | "
                        f"{v['outcome_and_search_identical']} | {len(v['outcome_differing'])} | "
                        f"{len(v['search_differing'])} | {len(v['missing'])} | "
                        f"{v['research_block_differing_cells']} |"
                    )
            lines.append("")
            lines += [
                "Paths at which the research block (`search.r021`, `diagnostics`) differs:",
                "",
            ]
            for label in ("report_full", "report_sor"):
                for algorithm, v in a["baseline_0_2_1"][label].items():
                    if v["research_block_paths"]:
                        lines.append(f"- {label} `{algorithm}`: {v['research_block_paths']}")
            lines.append("")
    lines += [
        "## Disposition (§11.6)",
        "",
        "```json",
        json.dumps(disposition(analyses), indent=1, sort_keys=True),
        "```",
        "",
    ]
    return "\n".join(lines)


def compact(result: Mapping[str, Any]) -> dict[str, Any]:
    """The committed form of an analysis: the same document without the per-directed-pair work
    families (the largest part), whose content stays in the full file and is pinned here by hash."""
    out = json.loads(json.dumps(result, default=str))
    for table in out.get("work", {}).values():
        pair = table["families"].pop("pair", {})
        table["families"]["pair"] = {
            "omitted_from_compact_copy": len(pair),
            "sha256": sha256_bytes(json.dumps(pair, sort_keys=True).encode()),
        }
    out["compact"] = True
    return out  # type: ignore[no-any-return]


# ----------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pruning_campaign.py", description=(__doc__ or "").split("\n")[0]
    )
    parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    p = sub.add_parser("profiles")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("schedule")
    p.add_argument("--stage", required=True)
    p.add_argument("--json")
    p = sub.add_parser("inputs")
    p.add_argument("--primary", required=True)
    p.add_argument("--root", required=True)
    p = sub.add_parser("execute")
    p.add_argument("--stage", required=True)
    p.add_argument("--inputs", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--lanes", type=int)
    p.add_argument("--only", action="append", default=[])
    p.add_argument("--retry-infrastructure")
    p = sub.add_parser("a5-values")
    p.add_argument("--tuning", required=True, help="the stage-T output directory")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("freeze")
    p.add_argument("--out", required=True)
    p = sub.add_parser("analyze")
    p.add_argument("--stage", required=True)
    p.add_argument("--inputs", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--baseline-root")
    p.add_argument("--json")
    p.add_argument("--pmset")
    p = sub.add_parser("tables")
    p.add_argument("--analysis", nargs="+", required=True, metavar="STAGE=JSON")
    p.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        campaign = load_schedule(args.manifest)
        if args.command == "check":
            problems = check(campaign)
            for problem in problems:
                print(problem, file=sys.stderr)
            print(f"check: {len(problems)} problem(s)")
            return 1 if problems else 0
        if args.command == "profiles":
            for key in generated_profiles(campaign):
                text = render_profile(campaign, key)
                path = REPO / profile_path(campaign, key)
                if args.write:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                print(f"{profile_path(campaign, key)} {sha256_bytes(text.encode())}")
            protocol = render_protocol(campaign)
            for spec, text in (
                (campaign.raw["latency"]["protocol"], protocol),
                (campaign.raw["latency"]["arms"], render_arms(campaign, protocol)),
            ):
                if args.write:
                    (REPO / spec["path"]).write_text(text, encoding="utf-8")
                print(f"{spec['path']} {sha256_bytes(text.encode())}")
            return 0
        if args.command == "schedule":
            result = c21.schedule(campaign, args.stage)
            if args.json:
                Path(args.json).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
            print(f"stage {args.stage}: {len(result['invocations'])} invocations, "
                  f"{result['cells']} scheduled run cells")  # fmt: skip
            return 0
        if args.command == "inputs":
            print(
                json.dumps(
                    prepare_inputs(campaign, Path(args.primary), Path(args.root)),
                    indent=1,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "execute":
            return execute(campaign, args.stage, inputs=Path(args.inputs), out=Path(args.out),
                           lanes=args.lanes, only=args.only,
                           retry_infrastructure=args.retry_infrastructure)  # fmt: skip
        if args.command == "a5-values":
            values = a5_values(campaign, Path(args.tuning))
            print(json.dumps(values, indent=1, sort_keys=True))
            if args.write:
                write_a5_values(Path(args.manifest), values)
                print(f"wrote a5.values into {args.manifest}")
            return 0
        if args.command == "freeze":
            record = freeze_record(campaign)
            Path(args.out).write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
            print(f"freeze record written: {args.out}")
            return 0
        if args.command == "analyze":
            result = analyze(campaign, args.stage, inputs=Path(args.inputs), out=Path(args.out),
                             baseline_root=Path(args.baseline_root) if args.baseline_root else None,
                             pmset=Path(args.pmset) if args.pmset else None)  # fmt: skip
            target = Path(args.json or Path(args.out) / "analysis.json")
            target.write_text(json.dumps(result, indent=1, sort_keys=True, default=str) + "\n")
            target.with_suffix(".compact.json").write_text(
                json.dumps(compact(result), indent=1, sort_keys=True) + "\n"
            )
            print(f"analysis: reconciled={result['reconciled']}, "
                  f"{len(result['reconciliation_problems'])} problem(s)")  # fmt: skip
            return 0 if result["reconciled"] else 1
        if args.command == "tables":
            analyses = {}
            for item in args.analysis:
                stage_name, _, path = item.partition("=")
                analyses[stage_name] = json.loads(Path(path).read_text())
            Path(args.out).write_text(render_tables(analyses), encoding="utf-8")
            print(f"wrote {args.out}")
            return 0
    except (CampaignError, ProfileError, r021.AnalysisError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    # import under the module's own name (the work pass pickles by reference; stay consistent)
    raise SystemExit(main(sys.argv[1:] if len(sys.argv) > 1 else None))

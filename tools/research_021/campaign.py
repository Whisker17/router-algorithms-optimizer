"""WHI-1562 research-021 paired campaign: manifest check, profile rendering, schedule
inventory, stage execution and analysis (R021-P14; contract R021-C/1 §§5-8).

A thin driver over the ordinary CLI -- it never solves anything itself. Every measured
invocation is a literal `main.py run|quote|report|order-check`, `benchmark.latency run|
sufficient` or `report.latency compare` subprocess, so every record is an ordinary run
record with its own manifest and `replay_command`. The schedule is
`config/research_021/campaign.yaml`; the rules are docs/references/research-021/
preregistration.md.

    uv run python tools/research_021/campaign.py check
    uv run python tools/research_021/campaign.py profiles [--write]
    uv run python tools/research_021/campaign.py schedule --stage T [--json out.json]
    uv run python tools/research_021/campaign.py inputs --primary <clone> --root <root>
    uv run python tools/research_021/campaign.py execute --stage T --inputs <root>/inputs \
        --out <dir>
    uv run python tools/research_021/campaign.py analyze --stage T --inputs <root>/inputs \
        --out <dir>

`execute` refuses a dirty tree (the measured source is the commit), a changed input, an
existing slot (no silent rerun) and an unknown stage; it samples the 1-minute load every
`host.load_sample_seconds` into `load.jsonl`, appends one ledger line per start/end to
`ledger.jsonl`, keeps each invocation's stdout/stderr, never retries, and on SIGTERM/SIGINT
stops the running children and records them `interrupted`. The only re-execution is the
pre-registered infrastructure deviation (`--retry-infrastructure ID`: once, into
`<id>.retry1`, only for an attempt that ended without a complete manifest).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _p in (str(REPO), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import analysis as r021  # noqa: E402

from benchmark.profile import (  # noqa: E402
    ProfileError,
    parse_profile,
    preset_options,
    read_profile_document,
    single_run_document,
    strategy_entry,
)
from benchmark.results import git_provenance, load_manifest  # noqa: E402
from benchmark.strategies import derive  # noqa: E402
from routing.algorithms.registry import ALGORITHMS  # noqa: E402

MANIFEST_PATH = REPO / "config" / "research_021" / "campaign.yaml"
PROFILE_DIR = "config/research_021/profiles"
SCHEMA = "r021.campaign/1"
STAGES = ("T", "L", "R", "M", "I")
KINDS = (
    "run", "quote", "report", "replay", "order_check", "replay_saved",
    "latency_run", "latency_sufficient", "latency_compare",
)  # fmt: skip
PRODUCES_RUN = ("run", "quote", "replay", "replay_saved")
DERIVABLE = ("report", "replay", "order_check")
DEFAULT_LAUNCHER = ("uv", "run", "python")
LATENCY_PROTOCOL = "config/latency/l01.yaml"
LATENCY_SUFFICIENT = "config/latency/l01-sufficient-budget.yaml"
LATENCY_ARMS = "config/latency/l08.yaml"


class CampaignError(ValueError):
    """The manifest, inputs or request are refused; nothing was executed or written."""


# ----------------------------------------------------------------------------- manifest


@dataclass(frozen=True)
class Invocation:
    id: str
    stage: str
    kind: str
    raw: Mapping[str, Any]
    parent: str | None = None  # the invocation a derived child reads

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def algorithms(self) -> list[str]:
        return [str(a) for a in self.raw.get("algorithms") or []]

    @property
    def depends(self) -> list[str]:
        deps = [str(d) for d in self.raw.get("after") or []]
        if self.parent:
            deps.append(self.parent)
        for key in ("of",):
            value = self.raw.get(key)
            if isinstance(value, list):
                deps += [str(v) for v in value]
        if self.kind == "latency_compare":
            for side in ("baseline", "candidate"):
                deps += [str(self.raw[side]), f"{self.raw[side]}-sb"]
        bundle = self.raw.get("bundle")
        if isinstance(bundle, str) and bundle.startswith("{dir:"):
            deps.append(bundle[5:].split("}", 1)[0])
        return list(dict.fromkeys(deps))


@dataclass
class Campaign:
    raw: dict[str, Any]
    invocations: list[Invocation]
    path: Path = MANIFEST_PATH
    by_id: dict[str, Invocation] = field(default_factory=dict)

    def stage(self, stage: str) -> list[Invocation]:
        if stage not in STAGES:
            raise CampaignError(f"unknown stage {stage!r} (known {list(STAGES)})")
        return [i for i in self.invocations if i.stage == stage]


def _expand(raw_list: Sequence[Mapping[str, Any]]) -> list[Invocation]:
    out: list[Invocation] = []
    for raw in raw_list:
        inv = Invocation(str(raw["id"]), str(raw["stage"]), str(raw["kind"]), raw)
        out.append(inv)
        for child in raw.get("derive") or []:
            if child not in DERIVABLE:
                raise CampaignError(f"{inv.id}: unknown derive {child!r}")
            if child == "order_check":
                cid = f"{inv.id}.order"
                out.append(Invocation(cid, inv.stage, "order_check",
                                      {"id": cid, "of": [inv.id, f"{inv.id}.replay"]}))  # fmt: skip
            else:
                cid = f"{inv.id}.{child}"
                out.append(Invocation(cid, inv.stage, child, {"id": cid}, parent=inv.id))
    return out


def load_campaign(path: Path | str = MANIFEST_PATH) -> Campaign:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise CampaignError(f"{path}: expected schema {SCHEMA}")
    invocations = _expand(raw.get("invocations") or [])
    campaign = Campaign(raw, invocations, path)
    seen: set[str] = set()
    for inv in invocations:
        if inv.id in seen:
            raise CampaignError(f"duplicate invocation id {inv.id}")
        if inv.stage not in STAGES or inv.kind not in KINDS:
            raise CampaignError(f"{inv.id}: stage/kind {inv.stage}/{inv.kind} not registered")
        for dep in inv.depends:
            if dep not in seen:
                raise CampaignError(f"{inv.id}: dependency {dep} is not registered before it")
        seen.add(inv.id)
        campaign.by_id[inv.id] = inv
        _validate_invocation(campaign, inv)
    return campaign


def _validate_invocation(campaign: Campaign, inv: Invocation) -> None:
    raw = campaign.raw
    if inv.kind in ("run", "quote"):
        bundle = str(inv.get("bundle"))
        if not bundle.startswith("{dir:") and bundle not in raw["inputs"]:
            raise CampaignError(f"{inv.id}: unknown bundle {bundle}")
        profile = str(inv.get("profile"))
        if profile.startswith("saved:"):
            if profile[6:] not in raw.get("saved_quotes", {}):
                raise CampaignError(f"{inv.id}: unknown saved profile {profile}")
        elif profile not in raw["profiles"]:
            raise CampaignError(f"{inv.id}: unknown profile {profile}")
        if inv.get("strategies") not in ("all", "base", "optimized", "profile"):
            raise CampaignError(f"{inv.id}: strategies must be all|base|optimized|profile")
        if inv.kind == "quote" and inv.get("request") not in raw.get("requests", {}):
            raise CampaignError(f"{inv.id}: unknown request {inv.get('request')}")
        if int(inv.get("expect_exit", 0)) == 0 and not inv.algorithms:
            raise CampaignError(f"{inv.id}: a successful run/quote must list its algorithms")
    if inv.kind in ("latency_run", "latency_sufficient") and inv.get("bundle") not in raw["inputs"]:
        raise CampaignError(f"{inv.id}: unknown bundle {inv.get('bundle')}")
    if inv.kind == "replay_saved" and inv.get("saved") not in raw.get("saved_quotes", {}):
        raise CampaignError(f"{inv.id}: unknown saved quote {inv.get('saved')}")
    if inv.kind in ("replay", "order_check") and inv.parent is None and not inv.get("of"):
        raise CampaignError(f"{inv.id}: {inv.kind} needs its source invocation(s)")


# ----------------------------------------------------------------------------- profiles


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def profile_path(campaign: Campaign, key: str) -> str:
    """Repository-relative path of a registered profile."""
    spec = campaign.raw["profiles"][key]
    return str(spec["path"]) if "path" in spec else f"{PROFILE_DIR}/{key}.yaml"


def _pinned_document(campaign: Campaign, key: str) -> dict[str, Any]:
    spec = campaign.raw["profiles"][key]
    if "path" not in spec:
        raise CampaignError(f"profile {key}: a generation base must be a pinned file")
    data = (REPO / spec["path"]).read_bytes()
    if _sha256_bytes(data) != spec["sha256"]:
        raise CampaignError(f"profile {key}: {spec['path']} sha256 differs from the pin")
    return read_profile_document(REPO / spec["path"])


def _options(algorithm: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    factory = ALGORITHMS[algorithm]
    preset = spec.get("preset", "current")
    if preset == "current":
        options = preset_options(factory)
    elif preset == "historical_v1":
        pins = [p for p in factory.historical_presets if p["version"] == 1]
        if len(pins) != 1:
            raise CampaignError(f"{algorithm}: no single historical v1 preset")
        options = preset_options(factory, pins[0])
    else:
        raise CampaignError(f"{algorithm}: unknown preset selector {preset!r}")
    options = dict(options)
    for key, value in (spec.get("set") or {}).items():
        if key not in options:
            raise CampaignError(f"{algorithm}: `set` names unknown option {key!r}")
        options[key] = value
    return options


def render_profile(campaign: Campaign, key: str) -> str:
    """The exact bytes of a generated profile: its base document with the declared
    overrides, fully validated by the ordinary loader."""
    spec = campaign.raw["profiles"][key]
    gen = spec.get("generate")
    if not isinstance(gen, Mapping):
        raise CampaignError(f"profile {key} is not generated")
    base_key = str(gen["base"])
    base_spec = campaign.raw["profiles"][base_key]
    doc = copy.deepcopy(_pinned_document(campaign, base_key))
    if "algorithms" in gen:
        doc["algorithms"] = [str(a) for a in gen["algorithms"]]
    for section in ("search", "graph", "measurement"):
        if gen.get(section):
            doc.setdefault(section, {}).update(dict(gen[section]))
    if gen.get("named_strategies"):
        doc["strategies"] = {str(n): strategy_entry(str(n)) for n in gen["named_strategies"]}
    if gen.get("options"):
        doc["algorithm_options"] = {
            str(a): _options(str(a), o) for a, o in dict(gen["options"]).items()
        }
    path = f"{PROFILE_DIR}/{key}.yaml"
    parse_profile(json.loads(json.dumps(doc)), path)  # refused exactly as the CLI would
    header = [
        f"# WHI-1562 research-021 campaign profile `{key}` (R021-P14). GENERATED by",
        "# `uv run python tools/research_021/campaign.py profiles --write` from",
        f"# {base_spec['path']} (sha256 {base_spec['sha256']}); do not edit by hand --",
        "# `campaign.py check` refuses any drift from this rendering.",
        f"# Purpose: {spec.get('purpose', '')}",
        "# Every value not listed in config/research_021/campaign.yaml `profiles."
        f"{key}.generate` is the base file's. Replay literally with --strategies profile.",
    ]
    return "\n".join(header) + "\n" + yaml.safe_dump(doc, sort_keys=False)


def generated_profiles(campaign: Campaign) -> list[str]:
    return [k for k, s in campaign.raw["profiles"].items() if "generate" in s]


def check(campaign: Campaign) -> list[str]:
    """Every problem between the manifest, the checked-in files and the code (empty = ok)."""
    problems: list[str] = []
    for key, spec in campaign.raw["profiles"].items():
        if "path" in spec:
            data = (REPO / spec["path"]).read_bytes()
            if _sha256_bytes(data) != spec["sha256"]:
                problems.append(f"profile {key}: {spec['path']} sha256 differs from the pin")
            continue
        path = REPO / profile_path(campaign, key)
        try:
            rendered = render_profile(campaign, key)
        except (CampaignError, ProfileError) as exc:
            problems.append(f"profile {key}: {exc}")
            continue
        if not path.is_file() or path.read_text(encoding="utf-8") != rendered:
            problems.append(f"profile {key}: {path.relative_to(REPO)} differs from its rendering")
    for inv in campaign.invocations:
        if inv.kind not in ("run", "quote") or int(inv.get("expect_exit", 0)) != 0:
            continue
        profile = str(inv.get("profile"))
        if profile.startswith("saved:"):
            continue
        try:
            source = read_profile_document(REPO / profile_path(campaign, profile))
            document, _ = derive(source, str(inv.get("strategies")), source_path=profile,
                                 source_sha256="0" * 64)  # fmt: skip
        except (ProfileError, OSError) as exc:
            problems.append(f"{inv.id}: profile {profile} does not derive: {exc}")
            continue
        if list(document["algorithms"]) != inv.algorithms:
            problems.append(f"{inv.id}: derives {document['algorithms']}, registered "
                            f"{inv.algorithms}")  # fmt: skip
    return problems


# ----------------------------------------------------------------------------- schedule


def schedule(campaign: Campaign, stage: str) -> dict[str, Any]:
    """The exact expected invocation and row inventory of a stage."""
    rows = []
    cells_total = 0
    for inv in campaign.stage(stage):
        entry: dict[str, Any] = {"id": inv.id, "kind": inv.kind, "depends": inv.depends}
        if inv.kind in ("run", "quote"):
            bundle = str(inv.get("bundle"))
            cases = 1 if inv.kind == "quote" else campaign.raw["inputs"].get(bundle, {}).get(
                "cases", inv.get("cases"))  # fmt: skip
            entry.update(bundle=bundle, profile=str(inv.get("profile")),
                         strategies=inv.get("strategies"), algorithms=inv.algorithms,
                         cases=cases, expect_exit=int(inv.get("expect_exit", 0)))  # fmt: skip
            if isinstance(cases, int):
                entry["cells"] = cases * len(inv.algorithms)
                cells_total += entry["cells"]
            if inv.kind == "quote":
                entry.update(request=campaign.raw["requests"][inv.get("request")],
                             details=bool(inv.get("details")),
                             solves_per_algorithm=1)  # fmt: skip
        for key in ("arm", "baseline", "candidate", "lane", "of", "saved"):
            if key in inv.raw:
                entry[key] = inv.raw[key]
        if inv.parent:
            entry["of"] = inv.parent
        rows.append(entry)
    return {"stage": stage, "stage_rules": campaign.raw["stages"][stage],
            "invocations": rows, "cells": cells_total}  # fmt: skip


# ----------------------------------------------------------------------------- inputs


def prepare_inputs(campaign: Campaign, primary: Path, root: Path) -> dict[str, Any]:
    """Copy every registered input into `root` (outside any worktree), verified."""
    out: dict[str, Any] = {}
    for key, spec in campaign.raw["inputs"].items():
        target = root / key
        source = primary / spec["source"]
        if not target.exists():
            shutil.copytree(source, target)
        digest = _sha256_bytes((target / "manifest.json").read_bytes())
        if digest != spec["bundle_hash"]:
            raise CampaignError(f"input {key}: {target} bundle hash {digest} != registered")
        out[key] = {"path": str(target), "bundle_hash": digest}
    for key, spec in (campaign.raw.get("saved_quotes") or {}).items():
        target = root / "saved" / key
        if not target.exists():
            shutil.copytree(primary / spec["source"], target)
        for name, pin in (("profile.yaml", "profile_sha256"), ("quote.json", "quote_sha256"),
                          ("bundle/manifest.json", "bundle_hash")):  # fmt: skip
            if _sha256_bytes((target / name).read_bytes()) != spec[pin]:
                raise CampaignError(f"saved quote {key}: {name} differs from its pin")
        out[f"saved:{key}"] = {"path": str(target)}
    return out


def verify_inputs(campaign: Campaign, inputs: Path, keys: Sequence[str]) -> None:
    for key in keys:
        spec = campaign.raw["inputs"][key]
        manifest = inputs / key / "manifest.json"
        if not manifest.is_file() or _sha256_bytes(manifest.read_bytes()) != spec["bundle_hash"]:
            raise CampaignError(f"input {key}: {manifest} missing or its hash differs")


# ----------------------------------------------------------------------------- execution


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    def append(self, entry: Mapping[str, Any]) -> None:
        line = json.dumps({"t": time.time(), **entry}, sort_keys=True) + "\n"
        with self.lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x]


def read_ledger(out: Path) -> dict[str, dict[str, Any]]:
    """id -> its latest `end` entry (plus the start time), from `ledger.jsonl`."""
    latest: dict[str, dict[str, Any]] = {}
    starts: dict[str, float] = {}
    for entry in Ledger(out / "ledger.jsonl").entries():
        if entry.get("event") == "start":
            starts[entry["id"]] = float(entry["t"])
        elif entry.get("event") in ("end", "blocked"):
            latest[entry["id"]] = {**entry, "started": starts.get(entry["id"])}
    return latest


def cross_stage_done(
    campaign: Campaign, invocations: Sequence[Invocation], stage: str, stage_root: Path
) -> dict[str, dict[str, Any]]:
    """The recorded outcome of every dependency of `invocations` registered in ANOTHER stage,
    read from that stage's sibling ledger `<stage_root>/<stage>/ledger.jsonl` (the documented
    stage-by-stage layout). A dependency without a recorded `end` is simply absent here, and the
    executor then blocks the dependent invocation immediately -- it never waits for it."""
    out: dict[str, dict[str, Any]] = {}
    for inv in invocations:
        for dep in inv.depends:
            other = campaign.by_id.get(dep)
            if other is None or other.stage == stage:
                continue
            entry = read_ledger(stage_root / other.stage).get(dep)
            if entry is not None:
                out[dep] = entry
    return out


def effective_entry(
    done: Mapping[str, Mapping[str, Any]], inv_id: str
) -> tuple[Mapping[str, Any] | None, str | None]:
    """The ledger entry the analysis uses for a registered invocation: its own `ok` end, else
    its single registered infrastructure re-execution `<id>.retry1` when that ended `ok`
    (returned with the deviation label), else its own (failed/blocked) entry or None."""
    own = done.get(inv_id)
    if own is not None and own.get("result") == "ok":
        return own, None
    retry = done.get(f"{inv_id}.retry1")
    if retry is not None and retry.get("result") == "ok":
        return retry, "infrastructure_retry"
    return own, None


def _single_child(directory: Path) -> Path:
    children = sorted(p for p in directory.iterdir() if p.is_dir())
    if len(children) != 1:
        raise CampaignError(f"{directory}: expected exactly one output directory, "
                            f"found {[c.name for c in children]}")  # fmt: skip
    return children[0]


@dataclass
class Context:
    campaign: Campaign
    inputs: Path
    out: Path
    launcher: tuple[str, ...]
    done: dict[str, dict[str, Any]] = field(default_factory=dict)
    stage: str | None = None
    stage_root: Path | None = None  # sibling stage outputs: <stage_root>/<stage>/

    def slot(self, inv_id: str) -> Path:
        """An invocation's slot: in this stage's `out`, or -- for a registered invocation of
        another stage -- in that stage's sibling output `<stage_root>/<stage>/<id>`."""
        inv = self.campaign.by_id.get(inv_id)
        if inv is not None and self.stage and inv.stage != self.stage and self.stage_root:
            return self.stage_root / inv.stage / inv_id
        return self.out / inv_id

    def run_dir(self, inv_id: str) -> Path:
        entry = self.done.get(inv_id)
        if not entry or not entry.get("run_dir"):
            raise CampaignError(f"{inv_id}: no recorded run directory")
        return Path(entry["run_dir"])

    def dir_of(self, inv_id: str) -> Path:
        return _single_child(self.slot(inv_id))

    def bundle(self, value: str) -> str:
        if value.startswith("{dir:"):
            ref, rest = value[5:].split("}", 1)
            return str(self.dir_of(ref)) + rest
        return str(self.inputs / value)

    def profile(self, key: str) -> str:
        if key.startswith("saved:"):
            return str(self.inputs / "saved" / key[6:] / "profile.yaml")
        return profile_path(self.campaign, key)


def argv_for(inv: Invocation, ctx: Context) -> list[str]:
    raw, py = inv.raw, list(ctx.launcher)
    slot = ctx.slot(inv.id)
    if inv.kind == "run":
        return [*py, "main.py", "run", "--bundle", ctx.bundle(str(raw["bundle"])),
                "--profile", ctx.profile(str(raw["profile"])), "--results-dir", str(slot),
                "--strategies", str(raw["strategies"])]  # fmt: skip
    if inv.kind == "quote":
        request = ctx.campaign.raw["requests"][raw["request"]]
        argv = [*py, "main.py", "quote", "--bundle", ctx.bundle(str(raw["bundle"])),
                "--profile", ctx.profile(str(raw["profile"])), "--token-in",
                str(request["token_in"]), "--token-out", str(request["token_out"]),
                "--amount", str(request["amount"]), "--quotes-dir", str(slot),
                "--strategies", str(raw["strategies"])]  # fmt: skip
        return argv + (["--details"] if raw.get("details") else [])
    if inv.kind == "report":
        assert inv.parent is not None
        return [*py, "main.py", "report", str(ctx.run_dir(inv.parent)), "--output",
                str(slot / "report")]  # fmt: skip
    if inv.kind == "replay":
        assert inv.parent is not None
        command = load_manifest(ctx.run_dir(inv.parent)).replay_command
        parts = shlex.split(command)
        if parts[:3] != ["uv", "run", "python"]:
            raise CampaignError(f"{inv.id}: unexpected replay command {command!r}")
        return [*py, *parts[3:]]
    if inv.kind == "order_check":
        a, b = (str(x) for x in raw["of"])
        return [*py, "main.py", "order-check", str(ctx.run_dir(a)), str(ctx.run_dir(b))]
    if inv.kind == "replay_saved":
        spec = ctx.campaign.raw["saved_quotes"][raw["saved"]]
        saved = ctx.inputs / "saved" / str(raw["saved"])
        quote = json.loads((saved / "quote.json").read_text(encoding="utf-8"))
        parts = shlex.split(str(quote["replay_command"]))
        prefix = str(spec["source"])
        relocated = [p.replace(prefix, str(saved), 1) if p.startswith(prefix) else p
                     for p in parts]  # fmt: skip
        index = relocated.index("--results-dir")
        relocated[index + 1] = str(slot)  # never write into the durable input copy
        if relocated[:3] != ["uv", "run", "python"]:
            raise CampaignError(f"{inv.id}: unexpected saved replay command")
        return [*py, *relocated[3:]]
    if inv.kind in ("latency_run", "latency_sufficient"):
        common = ["--arms", LATENCY_ARMS, "--arm", str(raw["arm"]), "--bundle",
                  ctx.bundle(str(raw["bundle"])), "--out", str(slot)]  # fmt: skip
        if inv.kind == "latency_run":
            return [*py, "-m", "benchmark.latency", "run", "--protocol", LATENCY_PROTOCOL, *common]
        return [*py, "-m", "benchmark.latency", "sufficient", "--sufficient",
                LATENCY_SUFFICIENT, *common]  # fmt: skip
    if inv.kind == "latency_compare":
        b, c = str(raw["baseline"]), str(raw["candidate"])
        return [*py, "-m", "report.latency", "compare", str(ctx.dir_of(b)), str(ctx.dir_of(c)),
                "--lane", str(raw["lane"]), "--sufficient", str(ctx.dir_of(f"{b}-sb")),
                str(ctx.dir_of(f"{c}-sb")), "--json", str(slot / "compare.json"),
                "--markdown", str(slot / "compare.md")]  # fmt: skip
    raise CampaignError(f"{inv.id}: kind {inv.kind} has no command")


def _produced_run(inv: Invocation, ctx: Context, before: set[Path]) -> str | None:
    slot = ctx.slot(inv.id)
    if inv.kind == "run" or inv.kind == "replay_saved":
        children = [p for p in slot.iterdir() if p.is_dir()]
        return str(children[0]) if len(children) == 1 else None
    if inv.kind == "quote":
        quotes = [p for p in slot.iterdir() if p.is_dir()]
        if len(quotes) != 1:
            return None
        runs = [p for p in (quotes[0] / "runs").iterdir() if p.is_dir()]
        return str(runs[0]) if len(runs) == 1 else None
    if inv.kind == "replay":
        assert inv.parent is not None
        parent = ctx.run_dir(inv.parent).parent
        new = [p for p in parent.iterdir() if p.is_dir() and p not in before]
        return str(new[0]) if len(new) == 1 else None
    return None


class _Sampler(threading.Thread):
    def __init__(self, path: Path, interval: float) -> None:
        super().__init__(daemon=True)
        self.path, self.interval, self.stop = path, interval, threading.Event()

    def sample(self) -> None:
        load = os.getloadavg()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"t": time.time(), "load1": load[0], "load5": load[1],
                                     "load15": load[2]}) + "\n")  # fmt: skip

    def run(self) -> None:
        while not self.stop.is_set():
            self.sample()
            self.stop.wait(self.interval)


def execute(
    campaign: Campaign,
    stage: str,
    *,
    inputs: Path,
    out: Path,
    lanes: int | None = None,
    only: Sequence[str] = (),
    retry_infrastructure: str | None = None,
    allow_dirty: bool = False,
    launcher: Sequence[str] = DEFAULT_LAUNCHER,
    echo: Callable[[str], None] = print,
    stage_root: Path | None = None,
    waive_launch_headroom: str | None = None,
) -> int:
    """Run a stage's invocations (see module docstring). Returns 0 when every executed
    invocation ended with its expected exit status, 1 otherwise, 3 when the registered launch
    gate did not launch the stage, 130 when interrupted. `stage_root` (default `out.parent`)
    holds the sibling stage outputs a cross-stage dependency is read from."""
    revision, dirty, _ = git_provenance(REPO)
    if dirty and not allow_dirty:
        raise CampaignError("the working tree is dirty: the measured source must be a commit")
    if os.cpu_count() != campaign.raw["host"]["logical_cpus"]:
        registered = campaign.raw["host"]["logical_cpus"]
        echo(f"note: host has {os.cpu_count()} logical CPUs, manifest registers {registered} "
             "(the load threshold uses the host's)")  # fmt: skip
    invocations = campaign.stage(stage)
    if retry_infrastructure:
        invocations = _retry_plan(campaign, stage, out, retry_infrastructure)
    elif only:
        unknown = set(only) - {i.id for i in invocations}
        if unknown:
            raise CampaignError(f"unknown invocations for stage {stage}: {sorted(unknown)}")
        invocations = [i for i in invocations if i.id in set(only)]
    bundles = sorted({str(i.get("bundle")) for i in invocations
                      if i.get("bundle") in campaign.raw["inputs"]})  # fmt: skip
    verify_inputs(campaign, inputs, bundles)
    out.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(out / "ledger.jsonl")
    root = stage_root if stage_root is not None else out.parent
    done = {**cross_stage_done(campaign, invocations, stage, root), **read_ledger(out)}
    ctx = Context(campaign, inputs, out, tuple(launcher), done=done, stage=stage,
                  stage_root=root)  # fmt: skip
    for inv in invocations:
        if ctx.slot(inv.id).exists():
            raise CampaignError(f"{inv.id}: slot {ctx.slot(inv.id)} exists -- never re-run")
    lanes = lanes or int(campaign.raw["stages"][stage]["lanes"])
    ledger.append({"event": "stage", "stage": stage, "git_revision": revision,
                   "git_dirty": dirty, "lanes": lanes, "invocations": [i.id for i in invocations],
                   "logical_cpus": os.cpu_count()})  # fmt: skip
    stage_rules = campaign.raw["stages"][stage]
    gate = stage_rules.get("launch_gate")
    if gate and not retry_infrastructure:
        if not _launch_gate(gate, ledger, echo, waive_launch_headroom):
            return 3
    sampler = _Sampler(out / "load.jsonl", float(campaign.raw["host"]["load_sample_seconds"]))
    sampler.start()
    stage_started = time.time()
    keep_awake: subprocess.Popen[bytes] | None = None
    if stage_rules.get("caffeinate"):
        # a sleep-prevention assertion that lives exactly as long as this executor (-w)
        awake_argv = ["caffeinate", "-i", "-m", "-s", "-w", str(os.getpid())]
        keep_awake = subprocess.Popen(awake_argv)
        ledger.append({"event": "caffeinate", "pid": keep_awake.pid, "argv": awake_argv})
    pending = list(invocations)
    planned = {i.id for i in invocations}
    running: dict[str, tuple[subprocess.Popen[bytes], Invocation, set[Path], Any, Any]] = {}
    failed = False
    interrupted = threading.Event()

    def _stop(signum: int, frame: object) -> None:
        interrupted.set()

    previous = {s: signal.signal(s, _stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        while pending or running:
            if interrupted.is_set():
                break
            for inv in list(pending):
                if len(running) >= lanes:
                    break
                deps = [d for d in inv.depends if d in campaign.by_id or d in planned]
                states = [ctx.done.get(d, {}).get("result") for d in deps]
                waiting = {i.id for i in pending} | set(running)
                unresolvable = [d for d, st in zip(deps, states, strict=True)
                                if st is None and d not in waiting]  # fmt: skip
                if any(s not in (None, "ok") for s in states) or unresolvable:
                    pending.remove(inv)
                    reason = (f"dependency never ended in any registered ledger: {unresolvable}"
                              if unresolvable else f"dependency not ok: {deps}")  # fmt: skip
                    ledger.append({"event": "blocked", "id": inv.id, "result": "blocked",
                                   "reason": reason})  # fmt: skip
                    ctx.done[inv.id] = {"result": "blocked"}
                    failed = True
                    continue
                if any(s is None for s in states):
                    continue
                pending.remove(inv)
                slot = ctx.slot(inv.id)
                slot.mkdir(parents=True)
                argv = argv_for(inv, ctx)
                before: set[Path] = set()
                if inv.kind == "replay" and inv.parent:
                    before = set(ctx.run_dir(inv.parent).parent.iterdir())
                stdout = (slot.parent / f"{inv.id}.stdout.log").open("wb")
                stderr = (slot.parent / f"{inv.id}.stderr.log").open("wb")
                ledger.append({"event": "start", "id": inv.id, "argv": argv,
                               "load1": os.getloadavg()[0]})  # fmt: skip
                echo(f"start {inv.id}: {shlex.join(argv)}")
                proc = subprocess.Popen(argv, cwd=REPO, stdout=stdout, stderr=stderr)
                running[inv.id] = (proc, inv, before, stdout, stderr)
            for inv_id, (proc, inv, before, so, se) in list(running.items()):
                code = proc.poll()
                if code is None:
                    continue
                so.close()
                se.close()
                del running[inv_id]
                expected = int(inv.get("expect_exit", 0))
                result = "ok" if code == expected else "failed"
                entry: dict[str, Any] = {"event": "end", "id": inv_id, "exit_code": code,
                                         "expected_exit": expected, "result": result,
                                         "load1": os.getloadavg()[0]}  # fmt: skip
                if expected == 0 and inv.kind in PRODUCES_RUN:
                    entry["run_dir"] = _produced_run(inv, ctx, before)
                    if entry["run_dir"] is None and result == "ok":
                        entry["result"] = result = "failed"
                if result == "ok" and inv.get("expect_stderr"):
                    text = (ctx.out / f"{inv_id}.stderr.log").read_text(errors="replace")
                    if str(inv.get("expect_stderr")) not in text:
                        entry["result"] = result = "failed"
                ledger.append(entry)
                ctx.done[inv_id] = entry
                failed |= result != "ok"
                echo(f"end {inv_id}: exit {code} ({result})")
            time.sleep(0.5)
    finally:
        for inv_id, (proc, _inv, _b, so, se) in running.items():
            proc.terminate()
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            so.close()
            se.close()
            ledger.append({"event": "end", "id": inv_id, "exit_code": proc.returncode,
                           "result": "interrupted"})  # fmt: skip
        sampler.stop.set()
        sampler.join(timeout=5)
        sampler.sample()
        if stage_rules.get("pmset_capture"):
            _capture_pmset(out, stage_started, ledger)
        if keep_awake is not None:
            keep_awake.terminate()
            keep_awake.wait(timeout=10)
        for s, handler in previous.items():
            signal.signal(s, handler)
    if interrupted.is_set():
        ledger.append({"event": "stage_interrupted", "stage": stage,
                       "not_started": [i.id for i in pending]})  # fmt: skip
        return 130
    ledger.append({"event": "stage_end", "stage": stage, "failed": failed})
    return 1 if failed else 0


def _launch_gate(
    gate: Mapping[str, Any], ledger: Ledger, echo: Callable[[str], None], waiver: str | None
) -> bool:
    """The registered stage launch gate (L08 §2): `samples` 1-minute load samples
    `interval_seconds` apart, recorded; the stage launches only if none exceeds
    `headroom_load1` (headroom only, never a validity rule) -- otherwise it is NOT launched and
    the blocker is recorded (no waiting loop, no retry). A waiver given by the orchestrator is
    recorded with its reason and launches regardless."""
    samples = []
    for index in range(int(gate["samples"])):
        if index:
            time.sleep(float(gate["interval_seconds"]))
        samples.append({"t": time.time(), "load1": os.getloadavg()[0]})
    peak = max(s["load1"] for s in samples)
    busy = peak > float(gate["headroom_load1"])
    launched = not busy or waiver is not None
    ledger.append({"event": "launch_gate", "samples": samples, "max_load1": peak,
                   "headroom_load1": gate["headroom_load1"], "busy": busy,
                   "waiver": waiver, "launched": launched})  # fmt: skip
    if not launched:
        ledger.append({"event": "stage_not_launched", "reason": "launch gate: host busy"})
        echo(f"launch gate: max 1-minute load {peak:.2f} > {gate['headroom_load1']}: not launched")
    return launched


def _capture_pmset(out: Path, since: float, ledger: Ledger) -> None:
    """Append the host's sleep/wake transitions since the stage start (`pmset -g log`) to
    `pmset-sleep-wake.txt`; a failed capture is recorded (its windows are then `unknown`)."""
    try:
        text = subprocess.run(["pmset", "-g", "log"], capture_output=True, text=True,
                              timeout=300, check=True).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        ledger.append({"event": "pmset_capture", "ok": False, "error": str(exc)})
        return
    keep = [line for line, e in ((x, r021.sleep_events(x)) for x in text.splitlines())
            if e and e[0]["t"] >= since - 60]  # fmt: skip
    with (out / "pmset-sleep-wake.txt").open("a", encoding="utf-8") as handle:
        handle.write("".join(f"{line}\n" for line in keep))
    ledger.append({"event": "pmset_capture", "ok": True, "lines": len(keep)})


def _retry_plan(campaign: Campaign, stage: str, out: Path, inv_id: str) -> list[Invocation]:
    """The pre-registered infrastructure deviation: one re-execution into `<id>.retry1`,
    only for an attempt that ended without a complete manifest (never a completed run)."""
    original = campaign.by_id.get(inv_id)
    if original is None or original.stage != stage or original.kind not in PRODUCES_RUN:
        raise CampaignError(f"{inv_id}: not a run-producing invocation of stage {stage}")
    entry = read_ledger(out).get(inv_id)
    if entry is None:
        raise CampaignError(f"{inv_id}: no recorded attempt to deviate from")
    run_dir = entry.get("run_dir")
    if run_dir:
        try:
            load_manifest(run_dir)
        except Exception:  # noqa: BLE001 - an incomplete manifest is exactly the admitted case
            pass
        else:
            raise CampaignError(f"{inv_id}: its run is complete -- a completed run is never re-run")
    retry_id = f"{inv_id}.retry1"
    if (out / retry_id).exists():
        raise CampaignError(f"{inv_id}: the single infrastructure re-execution was already used")
    plan = [Invocation(retry_id, stage, original.kind, {**original.raw, "id": retry_id})]
    # its registered derived children (report / replay / order-check) are re-planned against
    # the retried run with the same `.retry1` suffix, so the analysis can use them in its place
    mapped = {inv_id: retry_id}
    for inv in campaign.stage(stage):
        refs = [inv.parent] if inv.parent else [str(x) for x in inv.raw.get("of") or []]
        if inv.id in mapped or not refs or not any(r in mapped for r in refs):
            continue
        child_id = f"{inv.id}.retry1"
        mapped[inv.id] = child_id
        raw = {**inv.raw, "id": child_id}
        if "of" in raw:
            raw["of"] = [mapped.get(str(x), str(x)) for x in raw["of"]]
        parent = mapped.get(inv.parent) if inv.parent else None
        plan.append(Invocation(child_id, stage, inv.kind, raw, parent=parent))
    return plan


# ----------------------------------------------------------------------------- freeze


def _canonical_sha256(value: Any) -> str:
    return _sha256_bytes(json.dumps(value, sort_keys=True).encode())


def resolved_identity(campaign: Campaign, inv: Invocation) -> dict[str, Any] | None:
    """The resolved profile a registered run/quote must record (`manifest.resolved_profile`),
    computed exactly as `main.py run|quote` derive it from the registered source path, with
    each algorithm's prepare params and options identity. None for saved/refused inputs."""
    if inv.kind not in ("run", "quote") or int(inv.get("expect_exit", 0)) != 0:
        return None
    key = str(inv.get("profile"))
    if key.startswith("saved:"):
        return None
    path = profile_path(campaign, key)
    source = read_profile_document(REPO / path)
    mode = str(inv.get("strategies"))
    document = source
    if mode != "profile" or inv.kind == "quote":
        document, _ = derive(source, mode, source_path=path,
                             source_sha256=_sha256_bytes((REPO / path).read_bytes()))  # fmt: skip
    if inv.kind == "quote":
        document = single_run_document(document)
    profile = parse_profile(json.loads(json.dumps(document)), path)
    resolved = profile.resolved()
    if resolved["objective"]["mode"] == "empirical_cost":
        # the runner binds the objective to the bundle's frozen price context (its file hash)
        bundle = campaign.raw["inputs"].get(str(inv.get("bundle")), {})
        resolved["objective"]["price_context_sha256"] = bundle.get("prices_sha256")
    algorithms = {}
    for name in profile.algorithms:
        entry = profile.algorithm_options.get(name)
        algorithms[name] = {
            "params": resolved["algorithm_config"][name]["params"],
            "settings_sha256": None if entry is None else entry["settings_sha256"],
            "options_source": None if entry is None else entry["source"],
        }
    return {"resolved_profile_sha256": _canonical_sha256(resolved), "algorithms": algorithms}


def _file_pin(path: str) -> dict[str, str]:
    return {"path": path, "sha256": _sha256_bytes((REPO / path).read_bytes())}


def freeze_record(campaign: Campaign, *, inputs: Path | None,
                  nominees: Mapping[str, Any] | None) -> dict[str, Any]:  # fmt: skip
    """Everything R021-C/1 §6.3 freezes before the report comparison, from the code, the
    manifest and (optionally) the durable inputs; `check_freeze` regenerates it."""
    presets = {}
    for name, factory in sorted(ALGORITHMS.items()):
        pins = [p for p in (factory.options_preset, *factory.historical_presets) if p]
        if pins:
            presets[name] = [
                {**{k: pin[k] for k in ("path", "sha256", "key", "version")},
                 "file_sha256_now": _sha256_bytes((REPO / str(pin["path"])).read_bytes()),
                 "current": pin is factory.options_preset}
                for pin in pins
            ]  # fmt: skip
    inputs_view: dict[str, Any] = {}
    for key, spec in campaign.raw["inputs"].items():
        view = {k: spec[k] for k in ("bundle_hash", "cases_sha256", "cases", "split", "cohort")}
        if inputs is not None:
            cases = (inputs / key / "cases.jsonl").read_bytes()
            if _sha256_bytes(cases) != spec["cases_sha256"]:
                raise CampaignError(f"input {key}: cases.jsonl differs from the registered hash")
            ids = [json.loads(x)["case_id"] for x in cases.decode().splitlines() if x.strip()]
            if len(ids) != spec["cases"]:
                raise CampaignError(f"input {key}: {len(ids)} cases, registered {spec['cases']}")
            view["case_ids_sha256"] = _canonical_sha256(ids)
        inputs_view[key] = view
    return {
        "schema": "r021.campaign-freeze/1",
        "issue": campaign.raw.get("issue"),
        "contract": campaign.raw.get("contract"),
        "files": {
            "manifest": _file_pin(str(campaign.path.relative_to(REPO))),
            "campaign_py": _file_pin("tools/research_021/campaign.py"),
            "analysis_py": _file_pin("tools/research_021/analysis.py"),
            **{f"profile:{k}": _file_pin(profile_path(campaign, k))
               for k in campaign.raw["profiles"]},
            "l01": _file_pin(LATENCY_PROTOCOL),
            "l01_sufficient": _file_pin(LATENCY_SUFFICIENT),
            "l08": _file_pin(LATENCY_ARMS),
            "metis_m4_settings": _file_pin("config/metis_challenge/m4.yaml"),
            "cost_model": _file_pin("config/costs/mantle-101082044-cost-v1.json"),
        },  # fmt: skip
        "presets": presets,
        "inputs": inputs_view,
        "saved_quotes": campaign.raw.get("saved_quotes"),
        "known_report_defects": campaign.raw.get("known_report_defects"),
        "rules_sha256": _canonical_sha256(campaign.raw.get("rules")),
        "rules": campaign.raw.get("rules"),
        "effective_settings": {
            inv.id: ident for inv in campaign.invocations
            if (ident := resolved_identity(campaign, inv)) is not None
        },  # fmt: skip
        "inventory": {stage: schedule(campaign, stage) for stage in STAGES},
        "nominees": nominees,
    }


# ----------------------------------------------------------------------------- analysis


def _arm_ref(ref: str) -> tuple[str, str]:
    inv, _, algorithm = ref.partition("/")
    if not algorithm:
        raise CampaignError(f"arm reference {ref!r} is not <invocation>/<algorithm>")
    return inv, algorithm


class Loaded:
    """Runs of a stage, loaded once (bundle labels from the hash-verified inputs)."""

    def __init__(self, ctx: Context, stage: str) -> None:
        from report.aggregate import load_run

        self.ctx, self.stage, self.runs, self.problems = ctx, stage, {}, []
        self.deviations: dict[str, str] = {}
        for inv in ctx.campaign.stage(stage):
            entry, deviation = effective_entry(ctx.done, inv.id)
            if deviation:
                self.deviations[inv.id] = deviation
            if inv.kind not in PRODUCES_RUN or int(inv.get("expect_exit", 0)) != 0:
                continue
            if not entry:
                continue  # reported once by `analyze` (never executed / still running)
            if entry.get("result") != "ok" or not entry.get("run_dir"):
                self.problems.append(f"{inv.id}: not completed ({entry.get('result')})")
                continue
            bundle = inv.get("bundle")
            dirs = [ctx.inputs / str(bundle)] if bundle in ctx.campaign.raw["inputs"] else []
            self.runs[inv.id] = load_run(entry["run_dir"], bundle_dirs=dirs)

    def arm(self, ref: str) -> r021.Arm:
        inv, algorithm = _arm_ref(ref)
        if inv not in self.runs:
            raise CampaignError(f"arm {ref}: invocation {inv} has no loaded run")
        return r021.arm_from_run(self.runs[inv], algorithm, ref)


def _mirror(campaign: Campaign, comparison: Mapping[str, Any]) -> dict[str, Any] | None:
    mirror = campaign.raw.get("report_mirror") or {}
    if comparison.get("kind") in (mirror.get("exclude_kinds") or []):
        return None
    mapping = mirror.get("invocation_map") or {}

    def sub(value: Any) -> Any:
        if isinstance(value, str) and "/" in value and value.split("/", 1)[0] in mapping:
            inv, alg = value.split("/", 1)
            return f"{mapping[inv]}/{alg}"
        if isinstance(value, list):
            return [sub(v) for v in value]
        if isinstance(value, dict):
            return {k: sub(v) for k, v in value.items()}
        return value

    out: dict[str, Any] = sub(dict(comparison))
    out["stage"] = mirror.get("to_stage", "R")
    # a report-split comparison never carries a tuning label (e.g. depth.tuning -> depth.report)
    out["id"] = str(comparison["id"]).replace("tuning", "report")
    out["mirrored_from"] = comparison["id"]
    if comparison.get("id") in (mirror.get("known_defects_apart") or []):
        out["exclude_cases"] = list(campaign.raw["known_report_defects"]["cases"])
    return out


def stage_comparisons(campaign: Campaign, stage: str) -> list[dict[str, Any]]:
    own = [dict(c) for c in campaign.raw.get("comparisons") or [] if c.get("stage") == stage]
    mirror = campaign.raw.get("report_mirror") or {}
    if stage == mirror.get("to_stage"):
        for c in campaign.raw.get("comparisons") or []:
            if c.get("stage") == mirror.get("from_stage"):
                mirrored = _mirror(campaign, c)
                if mirrored is not None:
                    own.append(mirrored)
    return own


def _without(arm: r021.Arm, cases: Sequence[str]) -> r021.Arm:
    drop = set(cases)
    return r021.Arm(arm.label, arm.algorithm, [c for c in arm.case_ids if c not in drop],
                    {k: v for k, v in arm.cells.items() if k not in drop}, arm.families,
                    arm.strata, arm.splits, arm.prepare_events)  # fmt: skip


def run_comparison(loaded: Loaded, spec: Mapping[str, Any]) -> dict[str, Any]:
    kind = spec["kind"]
    out: dict[str, Any] = {"id": spec["id"], "registered": spec.get("registered"), "kind": kind}
    exclude = list(spec.get("exclude_cases") or [])
    if kind in ("paired", "identity", "not_below", "equal_value", "cycle_safe"):
        base, cand = loaded.arm(spec["baseline"]), loaded.arm(spec["candidate"])
        if exclude:
            out["excluded_cases_listed_apart"] = {
                c: {"baseline": base.cell(c).status, "candidate": cand.cell(c).status}
                for c in exclude if c in base.cells
            }  # fmt: skip
            base, cand = _without(base, exclude), _without(cand, exclude)
        if kind == "paired":
            out["result"] = r021.paired(base, cand, comparison_class=spec["class"])
        elif kind == "identity":
            out["result"] = r021.identity(base, cand, keys=spec.get("keys") or
                                          ("status", "score", "evaluation"))  # fmt: skip
        elif kind == "not_below":
            out["result"] = r021.not_below(base, cand)
        elif kind == "equal_value":
            out["result"] = r021.equal_value(base, cand)
            out["paired"] = r021.paired(base, cand, comparison_class=spec["class"])
        else:
            view = r021.cycle_safe_view(base, cand)
            out["result"] = view
            out["paired"] = r021.paired(base, cand, comparison_class=spec["class"])
            out["identity_on_comparable_identical"] = r021.identity(
                _only(base, view["comparable_identical_cases"]),
                _only(cand, view["comparable_identical_cases"]),
            )
        for unit in spec.get("units") or []:
            out.setdefault("same_unit_ratios", []).append(r021.same_unit_ratio(base, cand, unit))
        return out
    if kind == "decomposition":
        arms = {name: loaded.arm(ref) for name, ref in spec["arms"].items()}
        out["result"] = r021.depth_decomposition(arms, spec["base"], spec["chains"])
        out["contrasts"] = [
            r021.paired(arms[c["baseline"]], arms[c["candidate"]], comparison_class="same_domain")
            for c in spec.get("contrasts") or []
        ]
        return out
    if kind == "nominee":
        scan: dict[int, list[tuple[str, r021.Arm]]] = {}
        for value, invs in spec["scan"].items():
            scan[int(value)] = []
            for inv in invs:
                bundle = str(loaded.ctx.campaign.by_id[inv].get("bundle"))
                for algorithm in spec["algorithms"]:
                    scan[int(value)].append((bundle, loaded.arm(f"{inv}/{algorithm}")))
        out["apply"] = bool(spec.get("apply"))
        out["result"] = r021.nominee(scan, canonical=int(spec["canonical"]),
                                     algorithms=spec["algorithms"])  # fmt: skip
        return out
    if kind == "saved_literal":
        return _saved_literal(loaded, spec, out)
    if kind == "all_unsupported":
        rows = [r021.status_counts(loaded.arm(ref)) for ref in spec["arms"]]
        out["result"] = rows
        out["gate"] = "pass" if all(r["statuses"] == {"unsupported": r["scheduled"]}
                                    for r in rows) else "fail"  # fmt: skip
        return out
    raise CampaignError(f"{spec['id']}: unknown comparison kind {kind}")


def _saved_literal(loaded: Loaded, spec: Mapping[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    """A literal replay of a saved pre-0.2.1 profile against its durable ORIGINAL run: the
    same algorithms in order, an identical resolved profile, and per algorithm identical status,
    score and evaluation (search-stat and revision differences across sources are legitimate
    and not compared)."""
    from report.aggregate import load_run

    key = str(spec["saved"])
    saved_spec = loaded.ctx.campaign.raw["saved_quotes"][key]
    saved = loaded.ctx.inputs / "saved" / key
    original = load_run(saved / "runs" / str(saved_spec["run"]), bundle_dirs=[saved / "bundle"])
    replay_id = str(spec["replay"])
    if replay_id not in loaded.runs:
        raise CampaignError(f"{spec['id']}: replay {replay_id} has no loaded run")
    replay = loaded.runs[replay_id]
    same_resolved = original.manifest.resolved_profile == replay.manifest.resolved_profile
    same_algorithms = list(original.algorithms) == list(replay.algorithms) == [
        str(a) for a in saved_spec["algorithms"]]  # fmt: skip
    per_algorithm = {
        a: r021.identity(r021.arm_from_run(original, a, f"original/{a}"),
                         r021.arm_from_run(replay, a, f"{replay_id}/{a}"))
        for a in original.algorithms if a in replay.algorithms
    }  # fmt: skip
    gates = [same_resolved, same_algorithms, *(v["gate"] == "pass" for v in per_algorithm.values())]
    out["result"] = {
        "original_run": str(saved / "runs" / str(saved_spec["run"])),
        "original_git_revision": original.manifest.git_revision,
        "replay_git_revision": replay.manifest.git_revision,
        "same_algorithms_in_order": same_algorithms,
        "same_resolved_profile": same_resolved,
        "per_algorithm": per_algorithm,
    }
    out["gate"] = "pass" if all(gates) else "fail"
    return out


def _only(arm: r021.Arm, cases: Sequence[str]) -> r021.Arm:
    keep = set(cases)
    return _without(arm, [c for c in arm.case_ids if c not in keep])


NEW_IDS = ("metis_history", "direct_split_certified", "incremental_graph_repair",
           "uni_sor_cycle_safe", "cfmm_dual")  # fmt: skip


def analyze(
    campaign: Campaign, stage: str, *, inputs: Path, out: Path, pmset: Path | None = None,
    stage_root: Path | None = None,
) -> dict[str, Any]:
    """The registered analysis of a stage from its ledger and run records. Host sleep is read
    from `pmset` (default `<out>/pmset-sleep-wake.txt`); a stage that registers
    `pmset_capture` without a readable log has every timing window `unknown`."""
    stage_root = out.parent if stage_root is None else stage_root
    done = {**cross_stage_done(campaign, campaign.stage(stage), stage, stage_root),
            **read_ledger(out)}  # fmt: skip
    events = Ledger(out / "ledger.jsonl").entries()
    started_ids = {e["id"] for e in events if e.get("event") == "start"}
    ctx = Context(campaign, inputs, out, DEFAULT_LAUNCHER, done=done, stage=stage,
                  stage_root=stage_root)  # fmt: skip
    loaded = Loaded(ctx, stage)
    samples = [json.loads(x) for x in (out / "load.jsonl").read_text().splitlines() if x] \
        if (out / "load.jsonl").is_file() else []  # fmt: skip
    # the load threshold uses the executing host's logical CPUs recorded in the ledger
    recorded = [e.get("logical_cpus") for e in events if e.get("event") == "stage"]
    cpus = int(recorded[-1] if recorded and recorded[-1] else campaign.raw["host"]["logical_cpus"])
    pmset_path = pmset or out / "pmset-sleep-wake.txt"
    sleeps: list[dict[str, Any]] | None = None
    if pmset_path.is_file():
        sleeps = r021.sleep_events(pmset_path.read_text(encoding="utf-8", errors="replace"))
    host_note = "sleep/wake log: " + (str(pmset_path) if sleeps is not None else "none")
    inventory = schedule(campaign, stage)
    invocations: dict[str, Any] = {}
    reconciliation: list[str] = list(loaded.problems)
    for inv in campaign.stage(stage):
        entry, deviation = effective_entry(done, inv.id)
        invocations[inv.id] = {k: (entry or {}).get(k) for k in
                               ("result", "exit_code", "expected_exit", "run_dir", "started", "t")}
        invocations[inv.id]["holdout_exposure"] = _exposure(campaign, inv)
        if deviation:
            invocations[inv.id]["deviation"] = {"kind": deviation, "used": f"{inv.id}.retry1",
                                                "original": done.get(inv.id)}  # fmt: skip
        if entry is None:
            state = "started, not ended (running or interrupted)" if inv.id in started_ids \
                else "never executed"  # fmt: skip
            invocations[inv.id]["result"] = state
            reconciliation.append(f"{inv.id}: {state}")
            continue
        if inv.id not in loaded.runs:
            continue
        run = loaded.runs[inv.id]
        expected_algorithms = _expected_algorithms(campaign, inv)
        cases = _expected_case_ids(ctx, inv, run)
        identity = resolved_identity(campaign, inv)
        if identity is not None and identity["resolved_profile_sha256"] != _canonical_sha256(
            run.manifest.resolved_profile
        ):
            reconciliation.append(f"{inv.id}: resolved profile differs from the registered one")
        registered_hash = _expected_bundle_hash(ctx, inv)
        if inv.kind != "quote" and registered_hash != run.manifest.bundle_hash:
            reconciliation.append(f"{inv.id}: bundle hash {run.manifest.bundle_hash} != "
                                  f"registered {registered_hash}")  # fmt: skip
        reconciliation += r021.reconcile(
            inv.id, algorithms=run.algorithms, case_ids=run.case_ids,
            expected_algorithms=expected_algorithms, expected_case_ids=cases,
            cells={k: v.status for k, v in run.rows.items()}, complete=run.manifest.complete,
        )  # fmt: skip
        window = r021.host_window(samples, float(entry.get("started") or 0),
                                  float(entry.get("t") or 0), cpus,
                                  sleeps=sleeps)  # fmt: skip
        if sleeps is None and campaign.raw["stages"][stage].get("pmset_capture"):
            window["state"] = "unknown"  # a registered sleep log is missing: never "clean"
        arms = []
        for algorithm in run.algorithms:
            arm = r021.arm_from_run(run, algorithm, f"{inv.id}/{algorithm}")
            view: dict[str, Any] = {
                "status": r021.status_counts(arm),
                "work": r021.work_units(arm),
                "timing": r021.timing(arm),
            }
            if algorithm in NEW_IDS:
                view["certificate"] = r021.certificate_view(arm)
                view["domain"] = r021.domain_view(arm)
            if algorithm == "cfmm_dual":
                view["cfmm"] = r021.cfmm_view(arm)
            if algorithm == "incremental_graph_repair":
                view["repair"] = r021.repair_view(arm)
            if algorithm == "metis_history":
                view["history"] = r021.history_view(arm)
            if run.memory:
                view["memory"] = r021.memory_view(run.memory, run.manifest.prepare_events,
                                                  algorithm)  # fmt: skip
            arms.append(view)
        if inv.kind == "quote":
            reconciliation += _quote_checks(inv.id, run)
        started, ended = entry.get("started"), entry.get("t")
        invocations[inv.id].update(
            # the whole subprocess (incl. `uv run` resolution); a quote's complete response
            process_wall_seconds=None if started is None or ended is None else ended - started,
            host=window,
            timing_verdict=r021.timing_verdict([window], noise_floor_available=False),
            git_revision=run.manifest.git_revision, git_dirty=run.manifest.git_dirty,
            profile_sha256=run.manifest.profile_sha256, bundle_hash=run.manifest.bundle_hash,
            replay_command=run.manifest.replay_command, arms=arms,
            holdout_exposure=_exposure(campaign, inv),
        )  # fmt: skip
    comparisons = []
    for spec in stage_comparisons(campaign, stage):
        try:
            comparisons.append(run_comparison(loaded, spec))
        except (CampaignError, r021.AnalysisError) as exc:
            comparisons.append({"id": spec["id"], "error": str(exc)})
            reconciliation.append(f"comparison {spec['id']}: {exc}")
    latency = {}
    for inv in campaign.stage(stage):
        if inv.kind == "latency_compare" and (out / inv.id / "compare.json").is_file():
            latency[inv.id] = {"holdout_exposure": _exposure(campaign, inv),
                               "compare": json.loads((out / inv.id / "compare.json").read_text())}
    revision, dirty, _ = git_provenance(REPO)
    return {
        "schema": "r021.campaign-analysis/1",
        "stage": stage,
        "analysis_source": {
            "git_revision": revision, "git_dirty": dirty,
            "campaign_py_sha256": _sha256_bytes(Path(__file__).read_bytes()),
            "analysis_py_sha256": _sha256_bytes((HERE / "analysis.py").read_bytes()),
            "manifest_sha256": _sha256_bytes(campaign.path.read_bytes()),
        },
        "inventory": inventory,
        "reconciliation_problems": reconciliation,
        "reconciled": not reconciliation,
        "invocations": invocations,
        "comparisons": comparisons,
        "latency_comparisons": latency,
        "host_samples": len(samples),
        "host_sleep_log": host_note,
        "host_sleep_transitions": None if sleeps is None else sum(
            1 for e in sleeps if e["type"] == "Sleep"),
    }  # fmt: skip


def _exposure(campaign: Campaign, inv: Invocation) -> str:
    """Holdout exposure label: an invocation's declared `exposure`, else its bundle's split
    (`holdout_exposure` of the manifest); only a request outside every corpus split is
    `not_a_corpus_split`."""
    labels = campaign.raw["holdout_exposure"]
    if inv.get("exposure"):
        return str(labels.get(str(inv.get("exposure")), inv.get("exposure")))
    split = campaign.raw["inputs"].get(str(inv.get("bundle")), {}).get("split")
    return str(labels.get(str(split), "not_a_corpus_split"))


def _expected_algorithms(campaign: Campaign, inv: Invocation) -> list[str]:
    if inv.kind == "replay" and inv.parent:
        return _expected_algorithms(campaign, campaign.by_id[inv.parent])
    saved = inv.get("saved")
    if saved:
        return [str(a) for a in campaign.raw["saved_quotes"][saved]["algorithms"]]
    return inv.algorithms


def _expected_bundle_hash(ctx: Context, inv: Invocation) -> str | None:
    """The registered bundle of a run: an input's hash, the invocation's declared hash
    (derived bundles), a saved quote's request bundle, or a replay's source run's bundle."""
    if inv.kind == "replay" and inv.parent:
        return load_manifest(ctx.run_dir(inv.parent)).bundle_hash
    if inv.kind == "replay_saved":
        return str(ctx.campaign.raw["saved_quotes"][inv.get("saved")]["bundle_hash"])
    spec = ctx.campaign.raw["inputs"].get(str(inv.get("bundle")))
    if spec:
        return str(spec["bundle_hash"])
    value = inv.get("bundle_hash")
    return None if value is None else str(value)


def _expected_case_ids(ctx: Context, inv: Invocation, run: Any) -> list[str]:
    bundle = inv.get("bundle")
    if bundle in ctx.campaign.raw["inputs"]:
        if inv.kind == "quote":
            return list(run.case_ids)  # the derived single request (checked in _quote_checks)
        text = (ctx.inputs / str(bundle) / "cases.jsonl").read_text(encoding="utf-8")
        return [json.loads(x)["case_id"] for x in text.splitlines() if x.strip()]
    return list(run.case_ids)


def _quote_checks(inv_id: str, run: Any) -> list[str]:
    """One exploratory request, exactly one solve attempt per selected algorithm, one
    prepare per algorithm, no memory pass (quote's registered contract)."""
    problems = []
    if len(run.case_ids) != 1:
        problems.append(f"{inv_id}: a quote must schedule exactly one request")
    for (_case_id, algorithm), row in run.rows.items():
        measurement = (row.record or {}).get("measurement") or {}
        if measurement.get("attempts_completed") != 1 or len(measurement.get("solve_seconds")
                                                              or []) != 1:  # fmt: skip
            if row.status not in ("unsupported",) or measurement.get("attempts_completed") != 1:
                problems.append(f"{inv_id}: {algorithm} is not exactly one solve attempt")
    events = [e["algorithm"] for e in run.manifest.prepare_events]
    if events != list(run.algorithms):
        problems.append(f"{inv_id}: prepare events {events} != one worker per algorithm in order")
    if run.manifest.memory_record_count is not None:
        problems.append(f"{inv_id}: a quote must have no memory pass")
    return problems


def render_markdown(result: Mapping[str, Any]) -> str:
    """A compact human summary of an analysis (the JSON is the evidence)."""
    lines = [f"# research-021 campaign analysis, stage {result['stage']}", ""]
    src = result["analysis_source"]
    lines += [f"- analysis source `{src['git_revision']}` dirty={src['git_dirty']}; "
              f"manifest `{src['manifest_sha256'][:12]}`, analysis.py "
              f"`{src['analysis_py_sha256'][:12]}`", f"- reconciled: **{result['reconciled']}**"]
    for problem in result["reconciliation_problems"]:
        lines.append(f"  - {problem}")
    lines += ["", "## Invocations", "",
              "| id | result | host | max load1 | timing |", "| --- | --- | --- | ---: | --- |"]
    for inv_id, inv in result["invocations"].items():
        host = inv.get("host") or {}
        lines.append(f"| {inv_id} | {inv.get('result')} | {host.get('state', '-')} | "
                     f"{host.get('max_load1', '-')} | {inv.get('timing_verdict', '-')} |")
    lines += ["", "## Unconditional statuses", "", "| arm | scheduled | statuses |",
              "| --- | ---: | --- |"]  # fmt: skip
    for inv in result["invocations"].values():
        for arm in inv.get("arms") or []:
            s = arm["status"]
            lines.append(f"| {s['arm']} | {s['scheduled']} | {json.dumps(s['statuses'])} |")
    lines += ["", "## Comparisons", ""]
    for c in result["comparisons"]:
        lines.append(f"### {c['id']} ({c.get('registered')}, {c.get('kind')})")
        if "error" in c:
            lines.append(f"- ERROR: {c['error']}")
            continue
        res = c.get("result")
        if c["kind"] == "paired" or "paired" in c:
            p = res if c["kind"] == "paired" else c["paired"]
            pooled = p["pooled"]
            lines.append(
                f"- {p['class']}: common-OK {p['common_ok']}/{p['scheduled']}, higher/equal/"
                f"lower {pooled['higher']}/{pooled['equal']}/{pooled['lower']}, median bps "
                f"{pooled['median']}, min {pooled['min']}, max {pooled['max']}; transitions "
                f"{json.dumps(p['transitions'])}"
            )
        if c["kind"] in ("identity", "not_below", "equal_value", "all_unsupported"):
            gate = res.get("gate") if isinstance(res, dict) else c.get("gate")
            lines.append(f"- gate: **{gate}** {json.dumps(_brief(res))}")
        if c["kind"] == "nominee":
            use = "applied" if c["apply"] else "informational"
            lines.append(f"- nominee **{res['nominee']}** ({use}): {res['reason']}; " + ", ".join(
                             f"v={v}: failures {t['failures']}, shortfall {t['shortfall']}"
                             for v, t in res["values"].items()))  # fmt: skip
        if c["kind"] == "decomposition":
            lines.append(f"- common-OK {res['common_ok_cases']}/{res['scheduled']}")
            for chain in res["chains"]:
                steps = " x ".join(f"{s['ratio']}={s['geometric_mean']}" for s in chain["steps"])
                lines.append(f"  - {'->'.join(chain['chain'])}: {steps}")
        if c["kind"] == "cycle_safe":
            lines.append(f"- view {json.dumps({k: res[k] for k in ('reference_trajectory', 'publication', 'hard_kills', 'variant_invalid_plan', 'cases_with_rejections')})}")  # noqa: E501
            ident = c["identity_on_comparable_identical"]
            lines.append(f"- identity on comparable identical cases: **{ident['gate']}** "
                         f"({ident['checked']} checked, {len(ident['differing'])} differing)")
    return "\n".join(lines) + "\n"


def _brief(res: Any) -> Any:
    if not isinstance(res, dict):
        return res
    return {k: (v if not isinstance(v, list) else len(v)) for k, v in res.items()
            if k not in ("reference", "variant", "control", "baseline", "candidate")}


# ----------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="campaign.py", description=__doc__.split("\n")[0])
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
    p.add_argument("--stage-root", help="sibling stage outputs (default: the parent of --out)")
    p.add_argument("--waive-launch-headroom", metavar="REASON",
                   help="orchestrator-authorized waiver of the launch headroom (recorded)")
    p = sub.add_parser("freeze")
    p.add_argument("--inputs")
    p.add_argument("--nominees", help="stage-T analysis.json whose applied nominees to record")
    p.add_argument("--out", required=True)
    p = sub.add_parser("analyze")
    p.add_argument("--stage", required=True)
    p.add_argument("--inputs", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--json")
    p.add_argument("--markdown")
    p.add_argument("--pmset", help="pmset sleep/wake log (default <out>/pmset-sleep-wake.txt)")
    p.add_argument("--stage-root", dest="analysis_stage_root",
                   help="sibling stage outputs (default: the parent of --out)")
    args = parser.parse_args(argv)
    try:
        campaign = load_campaign(args.manifest)
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
                print(f"{profile_path(campaign, key)} {_sha256_bytes(text.encode())}")
            return 0
        if args.command == "schedule":
            result = schedule(campaign, args.stage)
            text = json.dumps(result, indent=1, sort_keys=True)
            if args.json:
                Path(args.json).write_text(text + "\n", encoding="utf-8")
            print(f"stage {args.stage}: {len(result['invocations'])} invocations, "
                  f"{result['cells']} scheduled run cells")  # fmt: skip
            return 0
        if args.command == "inputs":
            result = prepare_inputs(campaign, Path(args.primary), Path(args.root))
            print(json.dumps(result, indent=1, sort_keys=True))
            return 0
        if args.command == "execute":
            return execute(campaign, args.stage, inputs=Path(args.inputs), out=Path(args.out),
                           lanes=args.lanes, only=args.only,
                           retry_infrastructure=args.retry_infrastructure,
                           stage_root=Path(args.stage_root) if args.stage_root else None,
                           waive_launch_headroom=args.waive_launch_headroom)  # fmt: skip
        if args.command == "freeze":
            nominees = None
            if args.nominees:
                analysis = json.loads(Path(args.nominees).read_text())
                nominees = {c["id"]: {"apply": c["apply"], "nominee": c["result"]["nominee"],
                                      "canonical": 4, "reason": c["result"]["reason"],
                                      "analysis_sha256": _sha256_bytes(
                                          Path(args.nominees).read_bytes())}
                            for c in analysis["comparisons"] if c.get("kind") == "nominee"}
            record = freeze_record(campaign, inputs=Path(args.inputs) if args.inputs else None,
                                   nominees=nominees)  # fmt: skip
            Path(args.out).write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
            print(f"freeze record written: {args.out}")
            return 0
        if args.command == "analyze":
            result = analyze(campaign, args.stage, inputs=Path(args.inputs), out=Path(args.out),
                             pmset=Path(args.pmset) if args.pmset else None,
                             stage_root=Path(args.analysis_stage_root)
                             if args.analysis_stage_root else None)  # fmt: skip
            text = json.dumps(result, indent=1, sort_keys=True, default=str)
            Path(args.json or Path(args.out) / "analysis.json").write_text(text + "\n")
            Path(args.markdown or Path(args.out) / "analysis.md").write_text(
                render_markdown(result))
            print(f"analysis: reconciled={result['reconciled']}, "
                  f"{len(result['reconciliation_problems'])} problem(s)")  # fmt: skip
            return 0 if result["reconciled"] else 1
    except (CampaignError, ProfileError, r021.AnalysisError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

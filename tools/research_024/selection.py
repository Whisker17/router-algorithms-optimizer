"""WHI-1631 research-024 preset selection: schedule check, profiles, reuse check, execution,
analysis, presets and tables (contract R024-C/1 §4, §5, §10.1; schedule
`config/research_024/selection.yaml`; the rule is `r024_rule.py`).

A thin driver over the ordinary CLI -- it never solves anything itself. Every candidate and
reference arm is one identity in one P*-rendered profile and one `main.py run --strategies
profile` invocation plus its untimed work pass (`tools/research_022/pruning_work.py run ...`).
Execution is the 0.2.1 executor reached through the 0.2.2 driver
(`tools/research_022/pruning_campaign.py execute`: ledger, lanes, load samples, `caffeinate`,
`pmset` capture, the single infrastructure retry); no earlier campaign file is modified.

    uv run python tools/research_024/selection.py profiles [--write]
    uv run python tools/research_024/selection.py check [--stage T|I --inputs <dir> \\
        --out <stage dir> --artifacts <root>]
    uv run python tools/research_024/selection.py freeze --out <file>
    uv run python tools/research_024/selection.py inputs --primary <clone> --root <dir>
    uv run python tools/research_024/selection.py reuse --artifacts <root> [--stage T|I]
    uv run python tools/research_024/selection.py run --stage T|I --inputs <dir> \\
        --out <stage dir> --artifacts <root>
    uv run python tools/research_024/selection.py analyze --stage T|I --inputs <dir> \\
        --out <stage dir> --artifacts <root> --json <analysis.json> [--rule-inputs <file>] \\
        [--sums <SHA256SUMS>]
    uv run python tools/research_024/selection.py presets [--write]
    uv run python tools/research_024/selection.py tables --analysis <T.json> [--stage-i <I.json>] \\
        --out <tables.md>

`check` without `--out` checks the schedule against contract §12, the checked-in files and the
code (P* hashes, base CECs, grids, renderings, the report-bundle ban); with `--out` it also runs
every record check of `analyze` over a finished stage and prints every problem: a missing or
duplicate scheduled record, every `defect`, a report bundle named by a ledger or manifest.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import itertools
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _p in (str(REPO), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import r024_rule as rr  # noqa: E402

from benchmark.profile import ProfileError, parse_profile, read_profile_document  # noqa: E402
from benchmark.results import environment_record, load_case_records  # noqa: E402
from benchmark.strategies import derive  # noqa: E402
from routing.algorithms.base import OptionsError, validated_options  # noqa: E402
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

SCHEDULE = REPO / "config" / "research_024" / "selection.yaml"
SCHEMA = "r024.selection/1"
E1, E2 = "split_polish", "marginal_activation"
TOOLS = (
    "tools/research_024/selection.py",
    "tools/research_024/r024_rule.py",
    "tools/research_022/pruning_campaign.py",
    "tools/research_022/pruning_work.py",
    "tools/research_021/campaign.py",
)
CONTRACT_KEYS = ("identities", "quality", "work", "rule")


class SelectionError(ValueError):
    """The schedule, inputs or request are refused; nothing was executed or written."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_raw(path: Path = SCHEDULE) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise SelectionError(f"{path}: expected schema {SCHEMA}")
    return raw


def _pinned_bytes(spec: Mapping[str, Any]) -> bytes:
    data = (REPO / str(spec["path"])).read_bytes()
    if sha256_bytes(data) != spec["sha256"]:
        raise SelectionError(f"{spec['path']}: sha256 differs from the pin {spec['sha256']}")
    return data


def registry(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Contract §12 (the single YAML block of the pinned contract)."""
    text = _pinned_bytes(raw["contract"]).decode("utf-8")
    section = re.search(r"^## 12\. .*?(?=^## 13\. )", text, re.M | re.S)
    block = re.search(r"^```yaml\n(.*?)\n```", section.group(0) if section else "", re.M | re.S)
    if block is None:
        raise SelectionError("contract §12 holds no YAML registry")
    data = yaml.safe_load(block.group(1))
    assert isinstance(data, dict)
    return data


def rule_parameters(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The values the rule applies, from the schedule (held equal to contract §12 by `check`)."""
    return {
        "epsilon": Fraction(str(raw["rule"]["epsilon_bps"])),
        "limits": dict(raw["work"]["limits"]),
        "tie_break": list(raw["rule"]["tie_break"]),
        "failure_gross": int(raw["quality"]["failure_gross"]),
    }


# ----------------------------------------------------------------------------- candidates


def preset_options(raw: Mapping[str, Any], algorithm: str) -> dict[str, Any]:
    doc = yaml.safe_load(_pinned_bytes(raw["presets"][algorithm]))
    if doc.get("algorithm") != algorithm:
        raise SelectionError(f"preset of {algorithm} names {doc.get('algorithm')}")
    return dict(doc["options"])


def base_options(raw: Mapping[str, Any], base: str) -> dict[str, Any] | None:
    """§4.2: the options a base identity runs with (its preset v1, else none)."""
    return preset_options(raw, base) if base in raw["presets"] else None


def slug(candidate_id: str) -> str:
    return candidate_id.replace("|", "-")


def candidates(raw: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """candidate id -> {identity, base, options, labels}: the reference arms (`ref|<base>`), then
    the full Cartesian grid of each identity (§5.2; the labels fill the id), in grid order."""
    out: dict[str, dict[str, Any]] = {}
    for base in raw["quality"]["reference_set"]:
        out[f"ref|{base}"] = {
            "identity": base,
            "base": base,
            "kind": "reference",
            "options": base_options(raw, base),
            "labels": {},
        }
    for identity, spec in raw["identities"].items():
        axes = list(spec["grid"])
        values = [
            list(spec["grid"][a].items())
            if isinstance(spec["grid"][a], dict)
            else [(v, v) for v in spec["grid"][a]]
            for a in axes
        ]
        for combo in itertools.product(*values):
            labels = {a: label for a, (label, _) in zip(axes, combo, strict=True)}
            options = {a: value for a, (_, value) in zip(axes, combo, strict=True)}
            options.update(spec["fixed"])
            if options["base"] in spec.get("base_options_from_preset", []):
                options["base_options"] = base_options(raw, options["base"])
            cid = spec["id_format"].format(**labels)
            if cid in out:
                raise SelectionError(f"candidate {cid} generated twice")
            out[cid] = {
                "identity": identity,
                "base": options["base"],
                "kind": "candidate",
                "options": options,
                "labels": labels,
            }
    return out


def render_document(
    raw: Mapping[str, Any],
    identity: str,
    options: Mapping[str, Any] | None,
    chunks: int | None = None,
) -> dict[str, Any]:
    """§4.4: the P*-rendered profile of `identity` with `options` (`chunks`: a §5.10 sensitivity
    rendering, P* with only graph.chunks replaced)."""
    source = read_profile_document(REPO / raw["canonical"]["path"])
    _pinned_bytes(raw["canonical"])
    doc: dict[str, Any] = {"schema_version": 2, "algorithms": [identity]}
    for key in ("objective", "search", "budget", "measurement", "worker"):
        doc[key] = copy.deepcopy(source[key])
    doc["graph"] = dict(raw["pstar_graph"])
    if chunks is not None:
        doc["graph"]["chunks"] = int(chunks)
    if options is not None:
        doc["algorithm_options"] = {identity: copy.deepcopy(dict(options))}
    return doc


def render_text(raw: Mapping[str, Any], key: str, doc: Mapping[str, Any], what: str) -> str:
    parse_profile(json.loads(json.dumps(doc)), key)  # refused exactly as the CLI would
    canonical = raw["canonical"]
    header = [
        f"# WHI-1631 research_024 selection profile `{key}`: {what} (R024-C/1 §4.4).",
        "# GENERATED by `uv run python tools/research_024/selection.py profiles --write` from",
        f"# {canonical['path']} (sha256 {canonical['sha256']})",
        "# with only `algorithms`, `graph` and `algorithm_options` replaced as",
        "# config/research_024/selection.yaml says; do not edit by hand -- `selection.py check`",
        "# refuses any drift from this rendering. Replay literally with --strategies profile.",
    ]
    return "\n".join(header) + "\n" + yaml.safe_dump(dict(doc), sort_keys=False)


def profile_path(raw: Mapping[str, Any], key: str) -> str:
    return f"{raw['profile_dir']}/{key}.yaml"


def resolved_of(doc: Mapping[str, Any]) -> dict[str, Any]:
    return parse_profile(json.loads(json.dumps(doc)), "<rendered>").resolved()


def cec_of_doc(doc: Mapping[str, Any], identity: str) -> dict[str, Any]:
    return rr.cec(resolved_of(doc), identity)


# ----------------------------------------------------------------------------- stage I arms


def pinned_analysis(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    path = REPO / str(raw["selection_analysis"])
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def preset_document(raw: Mapping[str, Any], identity: str) -> dict[str, Any]:
    """The committed preset file of `identity`: `{key, version, algorithm, options}` (§5.1)."""
    spec = raw["identities"][identity]["preset"]
    path = REPO / spec["path"]
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or (doc.get("key"), doc.get("version"), doc.get("algorithm")) != (
        spec["key"],
        spec["version"],
        identity,
    ):
        raise SelectionError(
            f"{spec['path']} is not {spec['key']} v{spec['version']} of {identity}"
        )
    return doc


def stage_i_arms(raw: Mapping[str, Any], analysis: Mapping[str, Any] | None) -> dict[str, Any]:
    """arm key -> spec for stage I, from the pinned stage-T analysis: per `selected` identity the
    preset run and the §5.10 sensitivity arms (winner arm and base arm per graph.chunks value; a
    base that does not read graph.chunks gets none). A base arm shared by both identities runs
    once. Empty without a pinned analysis."""
    if analysis is None:
        return {}
    arms: dict[str, Any] = {}
    for identity in raw["identities"]:
        result = analysis["identities"][identity]
        if result["outcome"] != "selected":
            continue
        winner = result["winner"]
        options = analysis["candidates"][winner]["options"]
        base = str(options["base"])
        arms[f"preset-{identity}"] = {"kind": "preset", "identity": identity, "candidate": winner}
        if base in raw["sensitivity"]["not_applicable_bases"]:
            continue
        for k in raw["sensitivity"]["graph_chunks"]:
            arms[f"sens-{slug(winner)}-c{k}"] = {
                "kind": "sensitivity_winner",
                "identity": identity,
                "candidate": winner,
                "chunks": int(k),
                "options": options,
                "base_arm": f"sens-ref-{base}-c{k}",
            }
            arms.setdefault(
                f"sens-ref-{base}-c{k}",
                {
                    "kind": "sensitivity_base",
                    "identity": base,
                    "chunks": int(k),
                    "options": base_options(raw, base),
                },
            )
    return arms


def stage_i_document(raw: Mapping[str, Any], spec: Mapping[str, Any]) -> dict[str, Any]:
    if spec["kind"] == "preset":
        doc = preset_document(raw, spec["identity"])
        return render_document(raw, spec["identity"], doc["options"])
    return render_document(raw, spec["identity"], spec["options"], chunks=spec["chunks"])


def all_profiles(raw: Mapping[str, Any]) -> dict[str, tuple[dict[str, Any], str]]:
    """profile key -> (document, description) of every generated profile of the schedule."""
    out: dict[str, tuple[dict[str, Any], str]] = {}
    for cid, spec in candidates(raw).items():
        what = "reference arm" if spec["kind"] == "reference" else f"candidate `{cid}`"
        out[slug(cid)] = (render_document(raw, spec["identity"], spec["options"]), what)
    for key, spec in stage_i_arms(raw, pinned_analysis(raw)).items():
        what = {
            "preset": f"preset run of `{spec['identity']}` (§6.2)",
            "sensitivity_winner": f"sensitivity winner arm of `{spec['identity']}` (§5.10)",
            "sensitivity_base": "sensitivity base arm (§5.10)",
        }[spec["kind"]]
        out[f"i-{key}"] = (stage_i_document(raw, spec), what)
    return out


# ----------------------------------------------------------------------------- reuse (§5.9)


def _sums(raw: Mapping[str, Any]) -> dict[str, str]:
    text = _pinned_bytes(raw["reuse"]["sums"]).decode("utf-8")
    return {name: digest for digest, _, name in (x.partition("  ") for x in text.splitlines())}


def _r023_run(raw: Mapping[str, Any], artifacts: Path, run: str) -> Path:
    directory: Path = artifacts / str(raw["reuse"]["root"]) / run
    children = sorted(p for p in directory.iterdir() if p.is_dir()) if directory.is_dir() else []
    if len(children) != 1:
        raise SelectionError(f"{directory}: expected exactly one run directory")
    return children[0]


def _code_unchanged(raw: Mapping[str, Any]) -> bool:
    """R2: `git diff --quiet <R023 stage-T revision> HEAD -- <code paths>` (HEAD = the measured
    schedule commit: `run` refuses a dirty tree)."""
    argv = [
        "git",
        "diff",
        "--quiet",
        raw["reuse"]["revision"],
        "HEAD",
        "--",
        *raw["reuse"]["code_paths"],
    ]
    return subprocess.run(argv, cwd=REPO, capture_output=True).returncode == 0


def _environment(manifest: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    return {k: manifest.get("environment", {}).get(k) for k in keys}


def reuse_check(
    raw: Mapping[str, Any],
    artifacts: Path,
    config: Mapping[str, Any],
    doc: Mapping[str, Any],
    identity: str,
    runs: Sequence[str],
    case_ids: Sequence[str],
    environment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """R1-R4 for one configuration (all must hold for every listed run)."""
    keys = list(raw["reuse"]["environment_keys"])
    here = environment or {k: environment_record().get(k) for k in keys}
    mine = rr.cec(resolved_of(doc), identity)
    old_path = REPO / raw["reuse"]["profiles"] / f"{config['r023_profile']}.yaml"
    old = rr.cec(resolved_of(read_profile_document(old_path)), identity)
    sums = _sums(raw)
    outcome: dict[str, Any] = {
        "R1": old == mine,
        "R2": _code_unchanged(raw),
        "R3": True,
        "R4": True,
        "runs": {},
        "notes": [],
    }
    for run in runs:
        try:
            run_dir = _r023_run(raw, artifacts, run)
        except SelectionError as exc:
            outcome["R4"] = False
            outcome["notes"].append(str(exc))
            continue
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        rel = str(run_dir.relative_to(artifacts / raw["reuse"]["root"]))
        outcome["runs"][run] = str(run_dir.relative_to(artifacts))
        if rr.cec(manifest["resolved_profile"], identity) != mine:
            outcome["R1"] = False
            outcome["notes"].append(f"{run}: recorded resolved profile differs (CEC)")
        if _environment(manifest, keys) != dict(here):
            outcome["R2"] = False
            outcome["notes"].append(f"{run}: environment {_environment(manifest, keys)}")
        records = load_case_records(run_dir)
        if manifest.get("bundle_hash") != raw["inputs"]["tuning_full"]["bundle_hash"] or sorted(
            str(r["case_id"]) for r in records
        ) != sorted(case_ids):
            outcome["R3"] = False
            outcome["notes"].append(f"{run}: bundle or case ids differ from tuning_full")
        complete = manifest.get("complete") is True and manifest.get("state") == "complete"
        pinned = all(
            sums.get(f"{rel}/{leaf}") == sha256_bytes((run_dir / leaf).read_bytes())
            for leaf in ("cases.jsonl", "manifest.json")
        )
        if not (complete and pinned):
            outcome["R4"] = False
            outcome["notes"].append(f"{run}: incomplete or not pinned by the R023 sums")
    outcome["reused"] = all(outcome[r] for r in ("R1", "R2", "R3", "R4"))
    return outcome


def tuning_case_ids(raw: Mapping[str, Any], inputs: Path | None, artifacts: Path) -> list[str]:
    """The 96 case ids of `tuning_full` (from the durable input copy, else an R023 manifest)."""
    if inputs is not None and (inputs / "tuning_full" / "cases.jsonl").is_file():
        text = (inputs / "tuning_full" / "cases.jsonl").read_text(encoding="utf-8")
        return [json.loads(x)["case_id"] for x in text.splitlines() if x]
    first = raw["reuse"]["configurations"][0]["runs"][0]
    manifest = json.loads((_r023_run(raw, artifacts, first) / "manifest.json").read_text())
    return [str(c) for c in manifest["measurement"]["case_order"]]


def reuse_plan(
    raw: Mapping[str, Any], artifacts: Path, stage: str, inputs: Path | None = None
) -> dict[str, Any]:
    """Every registered reuse of `stage` with its R1-R4 outcome. Stage T: the configurations of
    §12 `selection.reuse`; stage I: every sensitivity arm whose CEC equals a registered R023
    sensitivity source (§5.10, §5.9)."""
    case_ids = tuning_case_ids(raw, inputs, artifacts)
    out: dict[str, Any] = {}
    if stage == "T":
        table = candidates(raw)
        for config in raw["reuse"]["configurations"]:
            cid = config["candidate"]
            spec = table[cid]
            doc = render_document(raw, spec["identity"], spec["options"])
            out[cid] = {
                "source": dict(config),
                **reuse_check(
                    raw, artifacts, config, doc, spec["identity"], config["runs"], case_ids
                ),
            }
        return out
    for key, spec in stage_i_arms(raw, pinned_analysis(raw)).items():
        if spec["kind"] == "preset":
            continue
        doc = stage_i_document(raw, spec)
        mine = cec_of_doc(doc, spec["identity"])
        for source in raw["reuse"]["sensitivity_sources"]:
            path = REPO / raw["reuse"]["profiles"] / f"{source['r023_profile']}.yaml"
            old_doc = read_profile_document(path)
            if list(old_doc["algorithms"]) != [spec["identity"]]:
                continue
            if cec_of_doc(old_doc, spec["identity"]) == mine:
                out[key] = {
                    "source": dict(source),
                    **reuse_check(
                        raw, artifacts, source, doc, spec["identity"], [source["run"]], case_ids
                    ),
                }
    return out


# ----------------------------------------------------------------------------- invocations


def _cost(raw: Mapping[str, Any], spec: Mapping[str, Any]) -> float:
    est = raw["cost_estimate_lane_minutes"]
    if spec["kind"] == "reference":
        return float(est["reference"][spec["base"]])
    if spec["identity"] == E1:
        return float(est[E1][spec["base"]])
    options = spec["options"]
    extra = int(options["activations"]) * int(options["top_k"]) / 1000  # K x top-k breaks ties
    return float(est[E2][spec["base"]][options["mode"]]) + extra


def invocations(raw: Mapping[str, Any], stage: str) -> list[dict[str, Any]]:
    """Stage T: `T-<slug>` and its work pass `T-WP-<slug>` for every candidate and reference arm
    not in the registered reuse list, longest estimated first (§10.1; launch order only). Stage
    I: `I-<arm>` (ordinary runs only) for every stage-I arm not reused."""
    bundle = raw["stages"][stage]["split"]
    out: list[dict[str, Any]] = []
    if stage == "T":
        reused = {c["candidate"] for c in raw["reuse"]["configurations"]}
        table = [(cid, s) for cid, s in candidates(raw).items() if cid not in reused]
        table.sort(key=lambda item: (-_cost(raw, item[1]), item[0]))
        for cid, spec in table:
            for work in (False, True):
                out.append(
                    {
                        "id": f"T{'-WP' if work else ''}-{slug(cid)}",
                        "stage": "T",
                        "kind": "run",
                        "bundle": bundle,
                        "profile": slug(cid),
                        "strategies": "profile",
                        "algorithms": [spec["identity"]],
                        "candidate": cid,
                        "work_pass": work,
                    }
                )
        return out
    arms = stage_i_arms(raw, pinned_analysis(raw))
    reusable = set(raw.get("_stage_i_reused", ()))
    for key, spec in arms.items():
        if key in reusable:
            continue
        out.append(
            {
                "id": f"I-{key}",
                "stage": "I",
                "kind": "run",
                "bundle": bundle,
                "profile": f"i-{key}",
                "strategies": "profile",
                "algorithms": [spec["identity"]],
                "arm": key,
                "work_pass": False,
            }
        )
    return out


def build(raw: Mapping[str, Any], stage: str, path: Path = SCHEDULE) -> Any:
    """A 0.2.1 `Campaign` over one stage of this schedule."""
    view = dict(raw)
    view["profiles"] = {k: {"path": profile_path(raw, k)} for k in all_profiles(raw)}
    view["stages"] = {s: dict(v) for s, v in raw["stages"].items()}
    campaign = c21.Campaign(view, c21._expand(invocations(raw, stage)), path)
    for inv in campaign.invocations:
        if inv.id in campaign.by_id:
            raise SelectionError(f"duplicate invocation {inv.id}")
        campaign.by_id[inv.id] = inv
    return campaign


def with_stage_i_reuse(
    raw: Mapping[str, Any], artifacts: Path, inputs: Path | None
) -> dict[str, Any]:
    """`raw` with the stage-I arms that are registered reuses (CEC-equal to an R023 sensitivity
    source) marked; `run` refuses to start if one of them fails R2-R4."""
    plan = reuse_plan(raw, artifacts, "I", inputs)
    view = dict(raw)
    view["_stage_i_reused"] = sorted(plan)
    view["_stage_i_reuse_plan"] = plan
    return view


# ----------------------------------------------------------------------------- check


def _forbidden_hits(raw: Mapping[str, Any], text: str) -> list[str]:
    needles = [*raw["forbidden"]["bundle_hashes"], *raw["forbidden"]["names"]]
    return [n for n in needles if n in text]


def check(raw: Mapping[str, Any]) -> list[str]:
    """Every problem between the schedule, contract §12, the checked-in files and the code."""
    problems: list[str] = []
    for label, spec in (
        ("contract", raw["contract"]),
        ("canonical", raw["canonical"]),
        ("m4 settings", raw["m4_settings"]),
        ("reuse sums", raw["reuse"]["sums"]),
        *((f"preset {k}", v) for k, v in raw["presets"].items()),
    ):
        try:
            _pinned_bytes(spec)
        except (SelectionError, OSError) as exc:
            problems.append(f"{label}: {exc}")
    if problems:
        return problems
    reg = registry(raw)
    sel = reg["selection"]
    # the rule's values, the grids and the reuse list are the contract's (§12)
    for key in CONTRACT_KEYS:
        if raw[key] != sel[key]:
            problems.append(f"schedule `{key}` differs from contract §12 selection.{key}")
    if set(raw["forbidden"]["bundle_hashes"]) != set(sel["forbidden_bundle_hashes"]):
        problems.append("forbidden bundle hashes differ from contract §12")
    if raw["inputs"]["tuning_full"]["bundle_hash"] != sel["split"]["bundle_hash"]:
        problems.append("tuning_full bundle_hash differs from contract §12 selection.split")
    if list(raw["inputs"]) != ["tuning_full"]:
        problems.append(f"inputs {list(raw['inputs'])}: the selection reads tuning_full only")
    reuse = sel["reuse"]
    mine = [
        {k: c[k] for k in ("candidate", "r023_profile", "runs")}
        for c in raw["reuse"]["configurations"]
    ]
    if (
        mine != reuse["configurations"]
        or raw["reuse"]["revision"] != reuse["stage_t_revision"]
        or raw["reuse"]["sums"] != reuse["sums"]
        or raw["reuse"]["code_paths"] != reuse["code_paths"]
    ):
        problems.append("reuse list differs from contract §12 selection.reuse")
    sens = sel["sensitivity"]
    if [raw["sensitivity"][k] for k in ("graph_chunks", "not_applicable_bases", "work_pass")] != [
        sens[k] for k in ("graph_chunks", "not_applicable_bases", "work_pass")
    ]:
        problems.append("sensitivity differs from contract §12 selection.sensitivity")
    # P*: the shared sections of the `all` derivation of the canonical profile, and its hash
    pstar = reg["pstar"]
    source = read_profile_document(REPO / raw["canonical"]["path"])
    _, all_profile = derive(
        source,
        "all",
        source_path=raw["canonical"]["path"],
        source_sha256=raw["canonical"]["sha256"],
    )
    resolved_all = all_profile.resolved()
    shared = {k: resolved_all[k] for k in pstar["shared"]}
    if shared != pstar["shared"] or rr.canonical_sha256(shared) != pstar["shared_sha256"]:
        problems.append("P* shared sections differ from contract §12 pstar.shared")
    if raw["pstar_graph"] != pstar["shared"]["graph"]:
        problems.append("pstar_graph differs from contract §12 pstar.shared.graph")
    table = candidates(raw)
    for base, pin in pstar["bases"].items():
        doc = render_document(raw, base, base_options(raw, base))
        mine_cec = cec_of_doc(doc, base)
        if rr.canonical_sha256(mine_cec) != pin["cec_sha256"]:
            problems.append(f"reference arm {base}: CEC sha256 differs from contract §12")
        if mine_cec != rr.cec(resolved_all, base):
            problems.append(f"reference arm {base}: CEC differs from its `all` row")
    sizes = {i: sum(1 for s in table.values() if s["identity"] == i) for i in raw["identities"]}
    for identity, spec in raw["identities"].items():
        if sizes[identity] != spec["candidates"]:
            problems.append(
                f"{identity}: {sizes[identity]} candidates, registered {spec['candidates']}"
            )
    graph_keys = set(raw["pstar_graph"])
    for cid, spec in table.items():
        if spec["kind"] != "candidate":
            continue
        factory = ALGORITHMS[spec["identity"]]
        try:
            normalized = validated_options(factory, spec["options"])
        except OptionsError as exc:
            problems.append(f"{cid}: options refused: {exc}")
            continue
        if normalized != spec["options"]:
            problems.append(f"{cid}: options are not in normalized form")
        keys = set(factory.graph_params_for(normalized) if factory.graph_params_for else ())
        if not (keys | set(factory.graph_params)) <= graph_keys:
            problems.append(f"{cid}: not representable under P* (graph keys {sorted(keys)})")
    # every generated profile equals its rendering; nothing unregistered in the directory
    profiles = all_profiles(raw)
    for key, (doc, what) in profiles.items():
        path = REPO / profile_path(raw, key)
        try:
            text = render_text(raw, key, doc, what)
        except ProfileError as exc:
            problems.append(f"profile {key}: {exc}")
            continue
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            problems.append(f"profile {key}: {profile_path(raw, key)} differs from its rendering")
            continue
        document, _ = derive(
            read_profile_document(path), "profile", source_path=str(path), source_sha256="0" * 64
        )
        if list(document["algorithms"]) != list(doc["algorithms"]):
            problems.append(f"profile {key}: derives {document['algorithms']}")
        if _forbidden_hits(raw, text):
            problems.append(f"profile {key}: names a report bundle")
    stray = {p.name for p in (REPO / raw["profile_dir"]).glob("*.yaml")} - {
        f"{k}.yaml" for k in profiles
    }
    if stray:
        problems.append(f"unregistered profiles in {raw['profile_dir']}: {sorted(stray)}")
    # the schedule itself names no report bundle outside its own `forbidden` list
    view = {k: v for k, v in raw.items() if k != "forbidden"}
    if _forbidden_hits(raw, json.dumps(view)):
        problems.append("the schedule names a report bundle outside `forbidden`")
    # stage I: every committed preset is the selected winner's options (options-only, §5.1)
    analysis = pinned_analysis(raw)
    if analysis is not None:
        for identity in raw["identities"]:
            result = analysis["identities"][identity]
            path = REPO / raw["identities"][identity]["preset"]["path"]
            if result["outcome"] != "selected":
                if path.exists():
                    problems.append(
                        f"{identity}: preset file exists for outcome {result['outcome']}"
                    )
                continue
            try:
                doc = preset_document(raw, identity)
            except (SelectionError, OSError) as exc:
                problems.append(f"{identity}: {exc}")
                continue
            winner = analysis["candidates"][result["winner"]]
            if doc["options"] != winner["options"] or set(doc) != {
                "key",
                "version",
                "algorithm",
                "options",
            }:
                problems.append(f"{identity}: preset differs from winner {result['winner']}")
            if path.read_text(encoding="utf-8") != render_preset(raw, identity, analysis):
                problems.append(f"{identity}: {path} differs from its rendering")
    return problems


# ----------------------------------------------------------------------------- presets


def render_preset(raw: Mapping[str, Any], identity: str, analysis: Mapping[str, Any]) -> str:
    """§5.1: `{key, version, algorithm, options}`, `options` = the winner's normalized options."""
    spec = raw["identities"][identity]["preset"]
    result = analysis["identities"][identity]
    winner = analysis["candidates"][result["winner"]]
    options = validated_options(ALGORITHMS[identity], winner["options"])
    if options != winner["options"]:
        raise SelectionError(f"{result['winner']}: options are not in normalized form")
    header = [
        f"# Selected preset v1 of `{identity}` (WHI-1631; contract R024-C/1 §5.1, §5.12).",
        f"# Selected by the registered rule R024-C/1 §5.7 among these {winner['grid_size']}",
        f"# registered `{identity}` candidates under P*, on the already exposed tuning split:",
        "# candidate",
        f"# `{result['winner']}` (docs/references/research-024/selection.md). Not claimed:",
        "# global optimality, optimality among unregistered values, validity outside block",
        "# 101082044, best under any other profile, or adoption. GENERATED by",
        "# `uv run python tools/research_024/selection.py presets --write` from the pinned stage-T",
        "# analysis; options only -- search/graph/budget values come from the run profile.",
        "# Frozen: WHI-1632 pins its sha256; a changed file is refused.",
    ]
    doc = {
        "key": spec["key"],
        "version": spec["version"],
        "algorithm": identity,
        "options": options,
    }
    return "\n".join(header) + "\n" + yaml.safe_dump(doc, sort_keys=False)


# ----------------------------------------------------------------------------- freeze / inputs


def _file_pin(path: str) -> dict[str, str]:
    return {"path": path, "sha256": sha256_bytes((REPO / path).read_bytes())}


def freeze_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The pins of everything stage T executes and analyses from, every candidate (options,
    profile, CEC) and the stage-T inventory."""
    table = candidates(raw)
    reused = {c["candidate"]: c for c in raw["reuse"]["configurations"]}
    rows = {}
    for cid, spec in table.items():
        doc = render_document(raw, spec["identity"], spec["options"])
        rows[cid] = {
            "identity": spec["identity"],
            "kind": spec["kind"],
            "options": spec["options"],
            "profile": _file_pin(profile_path(raw, slug(cid))),
            "cec_sha256": rr.canonical_sha256(cec_of_doc(doc, spec["identity"])),
            "runs": (
                {"reuse": reused[cid]["runs"]}
                if cid in reused
                else {"fresh": [f"T-{slug(cid)}", f"T-WP-{slug(cid)}"]}
            ),
        }
    return {
        "schema": "r024.selection-freeze/1",
        "issue": raw["issue"],
        "contract": raw["contract"]["key"],
        "files": {
            "schedule": _file_pin(str(SCHEDULE.relative_to(REPO))),
            **{f"tool:{t}": _file_pin(t) for t in TOOLS},
            "contract": _file_pin(str(raw["contract"]["path"])),
            "canonical": _file_pin(str(raw["canonical"]["path"])),
            "m4_settings": _file_pin(str(raw["m4_settings"]["path"])),
            **{f"preset:{k}": _file_pin(str(v["path"])) for k, v in raw["presets"].items()},
        },
        "inputs": raw["inputs"],
        "rule": {**{k: raw[k] for k in ("quality", "work", "rule")}},
        "candidates": rows,
        "inventory": [i["id"] for i in invocations(raw, "T")],
    }


def prepare_inputs(raw: Mapping[str, Any], primary: Path, root: Path) -> dict[str, Any]:
    """Copy the registered input into `root` (outside any worktree), hash-verified."""
    out: dict[str, Any] = {}
    for key, spec in raw["inputs"].items():
        if _forbidden_hits(raw, str(spec["source"])):
            raise SelectionError(f"input {key}: names a report bundle")
        target = root / key
        if not target.exists():
            shutil.copytree(primary / spec["source"], target)
        for name, pin in (("manifest.json", "bundle_hash"), ("cases.jsonl", "cases_sha256")):
            if sha256_bytes((target / name).read_bytes()) != spec[pin]:
                raise SelectionError(f"input {key}: {target / name} differs from its pin")
        out[key] = str(target)
    return out


# ----------------------------------------------------------------------------- analysis


def manifest_of(run_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_view(run_dir: Path) -> dict[str, Any]:
    """manifest (raw dict) and records of one run; `error` when either is unreadable."""
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        records = load_case_records(run_dir)
    except Exception as exc:  # noqa: BLE001 - an unreadable run is a G1 failure, reported
        return {"manifest": None, "records": None, "error": str(exc)}
    return {"manifest": manifest, "records": records, "error": None}


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
        runs[inv["id"]] = {"run_dir": str(entry["run_dir"]), "deviation": deviation}
    return runs, problems


def _rel(path: str, root: Path) -> str:
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return path


def _sums_of(runs: Mapping[str, Mapping[str, Any]], root: Path) -> dict[str, str]:
    sums = {}
    for run in runs.values():
        directory = Path(run["run_dir"])
        for name in ("manifest.json", "cases.jsonl"):
            if (directory / name).is_file():
                sums[str((directory / name).relative_to(root))] = sha256_bytes(
                    (directory / name).read_bytes()
                )
    return dict(sorted(sums.items()))


def families(inputs: Path) -> dict[str, str]:
    """case id -> its directed-pair family `(token_in, token_out)` (§5.11)."""
    text = (inputs / "tuning_full" / "cases.jsonl").read_text(encoding="utf-8")
    out = {}
    for line in text.splitlines():
        if line:
            case = json.loads(line)
            out[case["case_id"]] = f"{case['token_in'][:8]}->{case['token_out'][:8]}"
    return out


def _forbidden_in_runs(
    raw: Mapping[str, Any], out: Path, runs: Mapping[str, Mapping[str, Any]], inputs: Path
) -> list[str]:
    """Acceptance 5: no report bundle in the stage ledger, any manifest or the inputs root."""
    problems = []
    ledger = out / "ledger.jsonl"
    if ledger.is_file() and _forbidden_hits(raw, ledger.read_text(encoding="utf-8")):
        problems.append(f"{ledger}: names a report bundle")
    for key, run in runs.items():
        manifest = Path(run["run_dir"]) / "manifest.json"
        if manifest.is_file() and _forbidden_hits(raw, manifest.read_text(encoding="utf-8")):
            problems.append(f"{key}: manifest names a report bundle")
    for child in sorted(inputs.iterdir()) if inputs.is_dir() else []:
        if child.name not in raw["inputs"] or _forbidden_hits(raw, child.name):
            problems.append(f"inputs root holds {child.name}")
    return problems


def _environments(raw: Mapping[str, Any], runs: Mapping[str, Mapping[str, Any]]) -> list[str]:
    keys = list(raw["reuse"]["environment_keys"])
    return sorted(
        {
            json.dumps(_environment(m, keys), sort_keys=True)
            for r in runs.values()
            if (m := manifest_of(Path(r["run_dir"]))) is not None
        }
    )


def _revisions(runs: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return sorted(
        {
            str(m["environment"].get("git_revision"))
            for r in runs.values()
            if (m := manifest_of(Path(r["run_dir"]))) is not None
        }
    )


def _ratio_text(value: Fraction | str | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return f"{value.numerator}/{value.denominator}" if value.denominator != 1 else str(value)


def _arm_records(
    run_dirs: Mapping[str, str],
    identity: str,
    case_ids: Sequence[str],
    bundle_hash: str,
    expected_cec: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Mapping[str, Any]]], list[str]]:
    """G1 of one arm over its runs (`ordinary`, optionally `work_pass`): records by case, plus
    every G1 problem (a missing or incomplete run, a missing / duplicate / unscheduled case, a
    recorded configuration other than the arm's)."""
    loaded: dict[str, dict[str, Mapping[str, Any]]] = {}
    problems: list[str] = []
    for side, run_dir in run_dirs.items():
        view = load_view(Path(run_dir))
        if view["manifest"] is None:
            problems.append(f"{side}: {view['error']}")
            continue
        found = rr.completeness(view["manifest"], view["records"], identity, case_ids, bundle_hash)
        problems += [f"{side}: {p}" for p in found]
        if rr.cec(view["manifest"]["resolved_profile"], identity) != expected_cec:
            problems.append(f"{side}: recorded configuration differs from the arm's")
        loaded[side] = rr.by_case(view["records"])
    return loaded, problems


def analyze_t(raw: Mapping[str, Any], inputs: Path, out: Path, artifacts: Path) -> dict[str, Any]:
    """Stage T: completeness, every gate, Rule P / Rule M, the objective, the work limit and the
    rule per identity (§5.4-§5.8), with every §5.11 description. Records are loaded one arm at a
    time (only the five reference arms stay in memory)."""
    params = rule_parameters(raw)
    case_ids = tuning_case_ids(raw, inputs, artifacts)
    bundle_hash = raw["inputs"]["tuning_full"]["bundle_hash"]
    fresh, problems = stage_runs(raw, "T", out)
    reuse = reuse_plan(raw, artifacts, "T", inputs)
    table = candidates(raw)
    run_dirs: dict[str, dict[str, str]] = {}
    labels: dict[str, dict[str, str]] = {}
    reused_runs: dict[str, dict[str, Any]] = {}
    for cid in table:
        if cid in reuse:
            outcome = reuse[cid]
            if not outcome["reused"]:
                problems.append(f"{cid}: registered reuse fails R1-R4 {outcome['notes']}")
            ordinary, twin = outcome["source"]["runs"]
            dirs = {
                "ordinary": str(_r023_run(raw, artifacts, ordinary)),
                "work_pass": str(_r023_run(raw, artifacts, twin)),
            }
            labels[cid] = {"ordinary": f"R023:{ordinary}", "work_pass": f"R023:{twin}"}
            for label, d in zip(labels[cid].values(), dirs.values(), strict=True):
                reused_runs[label] = {"run_dir": d}
        else:
            ids = (f"T-{slug(cid)}", f"T-WP-{slug(cid)}")
            dirs = {
                side: fresh[i]["run_dir"]
                for side, i in zip(("ordinary", "work_pass"), ids, strict=True)
                if i in fresh
            }
            labels[cid] = dict(zip(("ordinary", "work_pass"), ids, strict=True))
        run_dirs[cid] = dirs
    every = {**fresh, **reused_runs}
    environments = _environments(raw, every)
    if len(environments) > 1:
        problems.append(f"runs come from {len(environments)} environments: {environments}")
    revisions = _revisions(fresh)
    if len(revisions) > 1:
        problems.append(f"fresh runs come from {len(revisions)} source revisions: {revisions}")
    problems += _forbidden_in_runs(raw, out, fresh, inputs)
    fams = families(inputs)

    def load(cid: str) -> tuple[dict[str, dict[str, Mapping[str, Any]]], list[str]]:
        spec = table[cid]
        doc = render_document(raw, spec["identity"], spec["options"])
        loaded, g1 = _arm_records(
            run_dirs[cid],
            spec["identity"],
            case_ids,
            bundle_hash,
            cec_of_doc(doc, spec["identity"]),
        )
        missing = [s for s in ("ordinary", "work_pass") if s not in run_dirs[cid]]
        return loaded, [f"{s}: no run" for s in missing] + g1

    def source(cid: str) -> dict[str, Any]:
        o = reuse.get(cid)
        reuse_view = None if o is None else {k: o[k] for k in ("R1", "R2", "R3", "R4", "runs")}
        return {**labels[cid], "reuse": reuse_view}

    # reference arms: G1, G6 (a P5 is a defect of both identities), A0's work, ref(c)
    references: dict[str, Any] = {}
    reference_records: dict[str, dict[str, Mapping[str, Any]]] = {}
    reference_defects: list[str] = []
    unavailable: list[str] = []
    for base in raw["quality"]["reference_set"]:
        cid = f"ref|{base}"
        loaded, g1 = load(cid)
        entry: dict[str, Any] = {
            "g1": g1,
            "source": source(cid),
            "options": table[cid]["options"],
            "identity": base,
            "kind": "reference",
        }
        if g1:
            unavailable.append(cid)
            problems.append(f"{cid}: G1 {g1}")
        else:
            gates = rr.evaluate_arm(base, case_ids, loaded["ordinary"], loaded["work_pass"], None)
            entry.update(gates)
            entry["counters"] = rr.identity_counters(loaded["ordinary"], base, case_ids)
            if gates["failures"]:
                reference_defects.append(cid)
                problems.append(f"{cid}: defect {gates['failures']}")
            reference_records[base] = loaded["ordinary"]
        references[cid] = entry
    reference_gross = [
        max(rr.gross(reference_records[b][c]) for b in raw["quality"]["reference_set"])
        if not unavailable
        else 0
        for c in case_ids
    ]
    a0 = references[f"ref|{raw['work']['reference']}"]
    reference_work = dict(a0.get("work") or dict.fromkeys(rr.UNITS))
    # candidates, one at a time
    rows: dict[str, Any] = {}
    rule_inputs: dict[str, list[dict[str, Any]]] = {i: [] for i in raw["identities"]}
    defects: dict[str, list[str]] = {i: [] for i in raw["identities"]}
    for cid, spec in table.items():
        if spec["kind"] != "candidate":
            continue
        identity = spec["identity"]
        loaded, g1 = load(cid)
        row: dict[str, Any] = {
            "identity": identity,
            "kind": "candidate",
            "options": spec["options"],
            "base": spec["base"],
            "source": source(cid),
            "grid_size": raw["identities"][identity]["candidates"],
            "g1": g1,
        }
        gates_pass = False
        work: dict[str, int | None] = dict.fromkeys(rr.UNITS)
        scored: list[int | None] = [None] * len(case_ids)
        if g1:
            row["eligibility"] = "ineligible: incomplete"
            problems.append(f"{cid}: G1 {g1}")
        elif spec["base"] not in reference_records:
            row["eligibility"] = "not_evaluated: reference arm unavailable"
        else:
            base_ref = reference_records[spec["base"]]
            gates = rr.evaluate_arm(
                identity, case_ids, loaded["ordinary"], loaded["work_pass"], base_ref
            )
            row.update(gates)
            work = dict(gates["work"])
            scored = [rr.scored_gross(loaded["ordinary"][c]) for c in case_ids]
            found = {g: v[:3] for g, v in gates["failures"].items() if g in rr.DEFECT_GATES}
            if found:
                row["eligibility"] = "defect"
                defects[identity].append(cid)
                problems.append(f"{cid}: defect {found}")
            elif "G2" in gates["failures"]:
                row["eligibility"] = "ineligible: status"
            else:
                gates_pass = True
            row["counters"] = rr.identity_counters(loaded["ordinary"], identity, case_ids)
            row["own_base_gain"] = rr.own_base_gain(loaded["ordinary"], base_ref, case_ids, fams)
        row["work_totals"] = work
        row["work_ratio"] = {u: _ratio_text(rr.ratio(work[u], reference_work[u])) for u in rr.UNITS}
        rows[cid] = row
        rule_inputs[identity].append(
            {"id": cid, "gates": "pass" if gates_pass else "fail", "gross": scored, "work": work}
        )
    identities: dict[str, Any] = {}
    for identity in raw["identities"]:
        problem = {
            "reference": {"gross": reference_gross, "work": reference_work},
            "candidates": rule_inputs[identity],
        }
        rule = rr.apply_rule(problem, **params)
        outcome = rr.identity_outcome(
            defects=defects[identity],
            reference_defects=reference_defects,
            reference_unavailable=unavailable,
            rule=rule,
        )
        identities[identity] = {
            **rule,
            **outcome,
            "rule_outcome": rule["outcome"],
            "rule_cause": rule["cause"],
        }
        for cid in rule["eligible"]:
            rows[cid]["eligibility"] = "eligible"
        for cid, why in rule.get("ineligible", {}).items():
            if why != "gates":
                rows[cid]["eligibility"] = f"ineligible: {why}"
        for cid, q in rule["q"].items():
            rows[cid]["q"] = q
    return {
        "schema": "r024.selection-analysis/1",
        "stage": "T",
        "contract": raw["contract"]["key"],
        "bundle_hash": bundle_hash,
        "cases": len(case_ids),
        "case_ids": case_ids,
        "git_revisions": revisions,
        "environments": environments,
        "problems": problems,
        "deviations": {k: v["deviation"] for k, v in fresh.items() if v["deviation"]},
        "reuse": reuse,
        "references": references,
        "reference_work": reference_work,
        "reference_gross_zero_cases": sum(1 for g in reference_gross if g <= 0),
        "candidates": {**references, **rows},
        "identities": identities,
        "rule": {k: str(v) if isinstance(v, Fraction) else v for k, v in params.items()},
        "raw_sha256": _sums_of(fresh, out),
        "run_dirs": {k: _rel(v["run_dir"], artifacts) for k, v in sorted(every.items())},
        "_rule_inputs": {
            i: {
                "reference": {"gross": reference_gross, "work": reference_work},
                "candidates": rule_inputs[i],
            }
            for i in raw["identities"]
        },
    }


def rule_inputs_path(raw: Mapping[str, Any]) -> Path:
    return REPO / str(raw["selection_analysis"]).replace("T-analysis.json", "T-rule-inputs.json")


def analyze_i(raw: Mapping[str, Any], inputs: Path, out: Path, artifacts: Path) -> dict[str, Any]:
    """Stage I: the preset runs equal their candidates (§6.2) and the sensitivity arms (§5.10)
    with their gates, Q against the P* common reference and own-base gain; a defect turns the
    identity into `blocked_defect`."""
    analysis = pinned_analysis(raw)
    if analysis is None:
        raise SelectionError("stage I needs the pinned stage-T analysis")
    pinned_inputs = json.loads(rule_inputs_path(raw).read_text(encoding="utf-8"))
    case_ids = list(analysis["case_ids"])
    bundle_hash = raw["inputs"]["tuning_full"]["bundle_hash"]
    failure = int(raw["quality"]["failure_gross"])
    view = with_stage_i_reuse(raw, artifacts, inputs)
    fresh, problems = stage_runs(view, "I", out)
    arms = stage_i_arms(raw, analysis)
    run_dirs: dict[str, str] = {}
    sources: dict[str, Any] = {}
    for key in arms:
        plan = view["_stage_i_reuse_plan"].get(key)
        if plan is not None:
            if not plan["reused"]:
                problems.append(f"I-{key}: registered reuse fails R1-R4 {plan['notes']}")
            run = plan["source"]["run"]
            run_dirs[key] = str(_r023_run(raw, artifacts, run))
            sources[key] = {
                "run": f"R023:{run}",
                "reuse": {k: plan[k] for k in ("R1", "R2", "R3", "R4", "runs")},
            }
        elif f"I-{key}" in fresh:
            run_dirs[key] = fresh[f"I-{key}"]["run_dir"]
            sources[key] = {"run": f"I-{key}", "reuse": None}
    problems += _forbidden_in_runs(raw, out, fresh, inputs)
    revisions = _revisions(fresh)
    every = {
        **fresh,
        **{
            f"R023:{k}": {"run_dir": d}
            for k, d in run_dirs.items()
            if k not in {i[2:] for i in fresh}
        },
    }
    environments = _environments(raw, every)
    if len(environments) > 1:
        problems.append(f"runs come from {len(environments)} environments: {environments}")
    fams = families(inputs)

    def records(key: str) -> tuple[dict[str, Mapping[str, Any]] | None, list[str]]:
        spec = arms[key]
        if key not in run_dirs:
            return None, ["no run"]
        doc = stage_i_document(raw, spec)
        loaded, g1 = _arm_records(
            {"ordinary": run_dirs[key]},
            spec["identity"],
            case_ids,
            bundle_hash,
            cec_of_doc(doc, spec["identity"]),
        )
        return (None if g1 else loaded["ordinary"]), g1

    result: dict[str, Any] = {}
    for identity in raw["identities"]:
        outcome = analysis["identities"][identity]
        entry: dict[str, Any] = {"t_outcome": outcome["outcome"], "winner": outcome["winner"]}
        result[identity] = entry
        if outcome["outcome"] != "selected":
            entry["final_outcome"] = outcome["outcome"]
            continue
        winner = outcome["winner"]
        reference_gross = [int(g) for g in pinned_inputs[identity]["reference"]["gross"]]
        defects: list[str] = []
        # §6.2: the preset run against the candidate's own ordinary records (fresh or reused)
        key = f"preset-{identity}"
        preset, g1 = records(key)
        source_label = analysis["candidates"][winner]["source"]["ordinary"]
        cand_view = load_view(artifacts / analysis["run_dirs"][source_label])
        cand = rr.by_case(cand_view["records"] or [])
        preset_cec = cec_of_doc(stage_i_document(raw, arms[key]), identity)
        cand_cec = cec_of_doc(
            render_document(raw, identity, analysis["candidates"][winner]["options"]), identity
        )
        differences = [] if preset is None else rr.preset_equality(cand, preset, identity, case_ids)
        entry["preset"] = {
            "run": sources.get(key),
            "candidate_run": source_label,
            "g1": g1,
            "cec_equal": preset_cec == cand_cec,
            "cec_sha256": rr.canonical_sha256(preset_cec),
            "cases_compared": 0 if preset is None else len(case_ids),
            "differences": differences,
        }
        if g1:
            problems.append(f"I-{key}: G1 {g1}")
        if preset_cec != cand_cec or differences:
            defects.append(key)
            problems.append(
                f"I-{key}: defect: the preset run differs from {winner} "
                f"(cec_equal={preset_cec == cand_cec}, {differences[:3]})"
            )
        # §5.10 sensitivity arms
        base = str(analysis["candidates"][winner]["options"]["base"])
        if base in raw["sensitivity"]["not_applicable_bases"]:
            entry["sensitivity"] = {"not_applicable": f"{base} does not read graph.chunks"}
        else:
            sens: dict[str, Any] = {}
            for k in raw["sensitivity"]["graph_chunks"]:
                wkey, bkey = f"sens-{slug(winner)}-c{k}", f"sens-ref-{base}-c{k}"
                wrec, wg1 = records(wkey)
                brec, bg1 = records(bkey)
                arm_out: dict[str, Any] = {
                    "winner_arm": {"key": wkey, "run": sources.get(wkey), "g1": wg1},
                    "base_arm": {"key": bkey, "run": sources.get(bkey), "g1": bg1},
                }
                for label, found in (("winner", wg1), ("base", bg1)):
                    if found:
                        problems.append(f"I-sens {identity} c{k} {label} arm: G1 {found}")
                if wrec is not None and brec is not None:
                    gates = rr.evaluate_arm(
                        identity, case_ids, wrec, None, brec, gates=("G2", "G3", "G4", "G5", "G7")
                    )
                    arm_out["winner_arm"].update(gates)
                    found_defects = {
                        g: v[:3] for g, v in gates["failures"].items() if g in rr.DEFECT_GATES
                    }
                    if found_defects:
                        defects.append(wkey)
                        problems.append(f"I-{wkey}: defect {found_defects}")
                    for side, recs, ident in (
                        ("winner_arm", wrec, identity),
                        ("base_arm", brec, base),
                    ):
                        q = rr.quality(
                            [rr.scored_gross(recs[c]) for c in case_ids], reference_gross, failure
                        )
                        arm_out[side]["q"] = None if q is None else str(q)
                        arm_out[side]["counters"] = rr.identity_counters(recs, ident, case_ids)
                    arm_out["winner_arm"]["own_base_gain"] = rr.own_base_gain(
                        wrec, brec, case_ids, fams
                    )
                sens[f"c{k}"] = arm_out
            entry["sensitivity"] = sens
        entry["defects"] = defects
        entry["final_outcome"] = "blocked_defect" if defects else "selected"
    return {
        "schema": "r024.selection-analysis/1",
        "stage": "I",
        "contract": raw["contract"]["key"],
        "bundle_hash": bundle_hash,
        "cases": len(case_ids),
        "git_revisions": revisions,
        "environments": environments,
        "problems": problems,
        "arms": {k: {**v, "source": sources.get(k)} for k, v in arms.items()},
        "identities": result,
        "deviations": {k: v["deviation"] for k, v in fresh.items() if v["deviation"]},
        "reuse": view["_stage_i_reuse_plan"],
        "raw_sha256": _sums_of(fresh, out),
        "run_dirs": {k: _rel(v, artifacts) for k, v in sorted(run_dirs.items())},
    }


def write_rule_inputs(path: Path, rule_inputs: Mapping[str, Any]) -> None:
    """One candidate per line (diffable, still one JSON document)."""
    lines = ["{"]
    for n, (identity, problem) in enumerate(rule_inputs.items()):
        lines.append(
            f'"{identity}": {{"reference": {json.dumps(problem["reference"], sort_keys=True)},'
        )
        lines.append('"candidates": [')
        rows = [json.dumps(c, sort_keys=True) for c in problem["candidates"]]
        lines.append(",\n".join(rows))
        lines.append("]}" + ("," if n < len(rule_inputs) - 1 else ""))
    lines.append("}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def verify_sums(out: Path, sums_file: Path) -> list[str]:
    problems = []
    for line in sums_file.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        path = out / name
        if not path.is_file() or sha256_bytes(path.read_bytes()) != digest:
            problems.append(f"{name}: missing or differs from {sums_file.name}")
    return problems


# ----------------------------------------------------------------------------- tables


def _q(value: str | None) -> str:
    return "—" if value is None else f"{float(Fraction(value)):+.4f}"


def _ratio(value: str | None) -> str:
    if value is None:
        return "missing"
    if value == rr.INFINITE:
        return "∞"
    return f"{float(Fraction(value)):.3f}"


def render_tables(analysis: Mapping[str, Any], stage_i: Mapping[str, Any] | None = None) -> str:
    """The selection tables, regenerated byte-identically from the pinned analyses."""
    out = [
        "# research-024 selection tables (GENERATED by `tools/research_024/selection.py "
        "tables`; do not edit)",
        "",
    ]
    out.append(
        f"Stage T: {analysis['cases']} tuning cases, bundle `{analysis['bundle_hash']}`, "
        f"source revision(s) {', '.join(f'`{r}`' for r in analysis['git_revisions'])}; "
        f"`check` problems: {len(analysis['problems'])}."
    )
    out += [
        "",
        "## Outcomes",
        "",
        "| identity | outcome | winner | Q* | Q(winner) | concession | "
        "margin | eligible | band | frontier | cause |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for identity, r in analysis["identities"].items():
        qw = r["q"].get(r["winner"]) if r["winner"] else None
        out.append(
            f"| `{identity}` | {r['outcome']} | `{r['winner']}` | {_q(r['q_star'])} | "
            f"{_q(qw)} | {_q(r['concession'])} | {_q(r['margin'])} | {len(r['eligible'])} | "
            f"{len(r['band'])} | {len(r['frontier'])} | {r['cause']} |"
        )
    out += [
        "",
        "## Reference arms",
        "",
        "| arm | source | G1 | statuses | Rule P | quotes | "
        "CL swap steps | LB bins | G6 failures |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for cid, r in analysis["references"].items():
        work = r.get("work") or {}
        src = r["source"]["ordinary"]
        out.append(
            f"| `{cid}` | {src} | {'ok' if not r['g1'] else r['g1']} | "
            f"{(r.get('counters') or {}).get('statuses')} | {r.get('pairs')} | "
            f"{work.get('quotes')} | {work.get('cl_swap_steps')} | "
            f"{work.get('lb_bins_swapped')} | {len((r.get('failures') or {}).get('G6', []))} |"
        )
    out.append("")
    out.append(
        f"Zero-reference cases (counted, never scored): {analysis['reference_gross_zero_cases']}."
    )
    for identity, r in analysis["identities"].items():
        out += [
            "",
            f"## `{identity}`: full ranking",
            "",
            "Eligible candidates by (−Q, W_quotes, W_cl, W_lb, id), then the ineligible by id. "
            "ρ = W / W(A0) per unit (limit 2). Own-base gain: mean bps over the cases whose "
            "own-base gross > 0, H/E/L, families net +/−.",
            "",
            "| # | candidate | eligibility | Q (bps) | ρ quotes | ρ CL | ρ LB | W quotes | "
            "W CL | W LB | own-base bps | H/E/L | fam +/− | branches | Rule P | statuses | "
            "truncated_by | refused | Brent status | source |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for n, cid in enumerate(r["ranking"], 1):
            c = analysis["candidates"][cid]
            gain = c.get("own_base_gain") or {}
            fam = gain.get("families") or {}
            counters = c.get("counters") or {}
            w = c["work_totals"]
            tags = [
                t
                for t, on in (
                    ("winner", cid == r["winner"]),
                    ("band", cid in r["band"]),
                    ("frontier", cid in r["frontier"]),
                )
                if on
            ]
            src = (
                "reuse " + "/".join(c["source"]["reuse"]["runs"])
                if c["source"]["reuse"]
                else "fresh"
            )
            mean = gain.get("mean_bps")
            out.append(
                f"| {n} | `{cid}`{' **' + ', '.join(tags) + '**' if tags else ''} | "
                f"{c['eligibility']} | {_q(c.get('q'))} | {_ratio(c['work_ratio']['quotes'])} | "
                f"{_ratio(c['work_ratio']['cl_swap_steps'])} | "
                f"{_ratio(c['work_ratio']['lb_bins_swapped'])} | {w['quotes']} | "
                f"{w['cl_swap_steps']} | {w['lb_bins_swapped']} | "
                f"{'—' if mean is None else f'{mean:+.4f}'} | "
                f"{gain.get('higher')}/{gain.get('equal')}/{gain.get('lower')} | "
                f"{fam.get('net_win')}/{fam.get('net_loss')} | {c.get('branches')} | "
                f"{c.get('pairs')} | {counters.get('statuses')} | {counters.get('truncated_by')} | "
                f"{counters.get('refused')} | {counters.get('brent_status')} | {src} |"
            )
    out += [
        "",
        "## Reuse (§5.9)",
        "",
        "| configuration | R1 | R2 | R3 | R4 | reused | runs |",
        "|---|---|---|---|---|---|---|",
    ]
    for cid, o in analysis["reuse"].items():
        out.append(
            f"| `{cid}` | {o['R1']} | {o['R2']} | {o['R3']} | {o['R4']} | {o['reused']} | "
            f"{', '.join(o['runs'].values())} |"
        )
    if stage_i is not None:
        out += ["", "## Stage I: preset runs (§6.2) and sensitivity arms (§5.10)", ""]
        out.append(
            f"Source revision(s) {', '.join(f'`{r}`' for r in stage_i['git_revisions'])}; "
            f"`check` problems: {len(stage_i['problems'])}."
        )
        for identity, r in stage_i["identities"].items():
            out += ["", f"### `{identity}`: final outcome {r['final_outcome']}", ""]
            if "preset" not in r:
                continue
            p = r["preset"]
            out.append(
                f"Preset run {p['run']}: CEC equal {p['cec_equal']} "
                f"(`{p['cec_sha256']}`), G1 {p['g1'] or 'ok'}, differing fields "
                f"{len(p['differences'])} over {p['cases_compared']} cases."
            )
            sens = r.get("sensitivity") or {}
            if "not_applicable" in sens:
                out.append(f"Sensitivity: not_applicable ({sens['not_applicable']}).")
                continue
            out += [
                "",
                "| chunks | winner arm | base arm | winner G1 | base G1 | gate failures | "
                "branches | Q winner arm | Q base arm | own-base bps (vs same-chunks base) | "
                "H/E/L | statuses |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|",
            ]
            for k, s in sens.items():
                w, b = s["winner_arm"], s["base_arm"]
                gain = w.get("own_base_gain") or {}
                mean = gain.get("mean_bps")
                out.append(
                    f"| {k} | {(w['run'] or {}).get('run')} | {(b['run'] or {}).get('run')} | "
                    f"{w['g1'] or 'ok'} | {b['g1'] or 'ok'} | {w.get('failures')} | "
                    f"{w.get('branches')} | {_q(w.get('q'))} | {_q(b.get('q'))} | "
                    f"{'—' if mean is None else f'{mean:+.4f}'} | "
                    f"{gain.get('higher')}/{gain.get('equal')}/{gain.get('lower')} | "
                    f"{(w.get('counters') or {}).get('statuses')} |"
                )
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------- CLI


def _precondition(
    raw: Mapping[str, Any], stage: str, artifacts: Path, inputs: Path
) -> dict[str, Any]:
    """`run` refuses unless `check` is clean and every registered reuse of the stage holds."""
    problems = check(raw)
    if problems:
        raise SelectionError(f"check reports {len(problems)} problem(s): {problems[:3]}")
    plan = reuse_plan(raw, artifacts, stage, inputs)
    failing = {k: v["notes"] for k, v in plan.items() if not v["reused"]}
    if failing:
        raise SelectionError(
            f"registered reuse fails R1-R4 (amend the schedule before any solve): {failing}"
        )
    return plan


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="selection.py", description=(__doc__ or "").split("\n")[0]
    )
    parser.add_argument("--schedule", default=str(SCHEDULE))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("profiles")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("check")
    for name in ("--stage", "--out", "--inputs", "--artifacts"):
        p.add_argument(name)
    p = sub.add_parser("freeze")
    p.add_argument("--out", required=True)
    p = sub.add_parser("inputs")
    p.add_argument("--primary", required=True)
    p.add_argument("--root", required=True)
    p = sub.add_parser("reuse")
    p.add_argument("--artifacts", required=True)
    p.add_argument("--stage", default="T", choices=["T", "I"])
    p.add_argument("--inputs")
    p = sub.add_parser("run")
    p.add_argument("--stage", required=True, choices=["T", "I"])
    p.add_argument("--inputs", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--artifacts", required=True)
    p.add_argument("--lanes", type=int)
    p.add_argument("--retry-infrastructure")
    p = sub.add_parser("analyze")
    p.add_argument("--stage", required=True, choices=["T", "I"])
    for name in ("--inputs", "--out", "--artifacts", "--json"):
        p.add_argument(name, required=True)
    p.add_argument("--rule-inputs")
    p.add_argument("--sums")
    p = sub.add_parser("presets")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("tables")
    p.add_argument("--analysis", required=True)
    p.add_argument("--stage-i")
    p.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        raw = load_raw(Path(args.schedule))
        if args.command == "profiles":
            for key, (doc, what) in all_profiles(raw).items():
                text = render_text(raw, key, doc, what)
                path = REPO / profile_path(raw, key)
                if args.write:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                print(f"{profile_path(raw, key)} {sha256_bytes(text.encode())}")
            return 0
        if args.command == "check":
            problems = check(raw)
            if args.out:
                if not (args.stage and args.inputs and args.artifacts):
                    raise SelectionError("check --out needs --stage, --inputs and --artifacts")
                inputs, out, artifacts = Path(args.inputs), Path(args.out), Path(args.artifacts)
                if args.stage == "T":
                    result = analyze_t(raw, inputs, out, artifacts)
                else:
                    result = analyze_i(raw, inputs, out, artifacts)
                problems += result["problems"]
            for problem in problems:
                print(problem, file=sys.stderr)
            print(f"check: {len(problems)} problem(s)")
            return 1 if problems else 0
        if args.command == "freeze":
            record = freeze_record(raw)
            Path(args.out).write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
            print(f"freeze record written: {args.out}")
            return 0
        if args.command == "inputs":
            print(json.dumps(prepare_inputs(raw, Path(args.primary), Path(args.root)), indent=1))
            return 0
        if args.command == "reuse":
            plan = reuse_plan(
                raw, Path(args.artifacts), args.stage, Path(args.inputs) if args.inputs else None
            )
            print(json.dumps(plan, indent=1, sort_keys=True))
            return 0 if all(v["reused"] for v in plan.values()) else 1
        if args.command == "run":
            artifacts, inputs = Path(args.artifacts), Path(args.inputs)
            plan = _precondition(raw, args.stage, artifacts, inputs)
            view = with_stage_i_reuse(raw, artifacts, inputs) if args.stage == "I" else raw
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            c21.Ledger(out / "ledger.jsonl").append(
                {"event": "reuse", "stage": args.stage, "plan": plan}
            )
            return int(
                pc.execute(
                    build(view, args.stage),
                    args.stage,
                    inputs=inputs,
                    out=out,
                    lanes=args.lanes,
                    retry_infrastructure=args.retry_infrastructure,
                )
            )
        if args.command == "analyze":
            inputs, out, artifacts = Path(args.inputs), Path(args.out), Path(args.artifacts)
            if args.stage == "T":
                result = analyze_t(raw, inputs, out, artifacts)
                rule_inputs = result.pop("_rule_inputs")
                if args.rule_inputs:
                    write_rule_inputs(Path(args.rule_inputs), rule_inputs)
                    result["rule_inputs_sha256"] = sha256_bytes(Path(args.rule_inputs).read_bytes())
            else:
                result = analyze_i(raw, inputs, out, artifacts)
            Path(args.json).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
            if args.sums:
                lines = [f"{d}  {n}\n" for n, d in result["raw_sha256"].items()]
                Path(args.sums).write_text("".join(lines), encoding="utf-8")
            print(f"analysis: {len(result['problems'])} problem(s) -> {args.json}")
            return 1 if result["problems"] else 0
        if args.command == "presets":
            analysis = pinned_analysis(raw)
            if analysis is None:
                raise SelectionError(f"no pinned stage-T analysis at {raw['selection_analysis']}")
            for identity in raw["identities"]:
                if analysis["identities"][identity]["outcome"] != "selected":
                    print(f"{identity}: {analysis['identities'][identity]['outcome']} (no preset)")
                    continue
                text = render_preset(raw, identity, analysis)
                path = REPO / raw["identities"][identity]["preset"]["path"]
                if args.write:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                print(f"{path.relative_to(REPO)} {sha256_bytes(text.encode())}")
            return 0
        if args.command == "tables":
            analysis = json.loads(Path(args.analysis).read_text(encoding="utf-8"))
            stage_i = json.loads(Path(args.stage_i).read_text()) if args.stage_i else None
            Path(args.out).write_text(render_tables(analysis, stage_i), encoding="utf-8")
            print(f"wrote {args.out}")
            return 0
    except (SelectionError, ProfileError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

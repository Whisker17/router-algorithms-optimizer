"""WHI-1627 research_023 paired campaign: profiles, check, freeze, run, analysis and tables
(contract R023-C/1 §5.3, §8, §10; schedule `config/research_023/schedule.yaml`).

A thin driver over the ordinary CLI -- it never solves anything itself. Every arm is one
identity in one generated profile and one `main.py run --strategies profile` invocation (plus its
untimed work pass, `tools/research_022/pruning_work.py run ...`). Execution is the 0.2.1
executor reached through the 0.2.2 driver (`tools/research_022/pruning_campaign.py execute`:
ledger, lanes, load samples, `caffeinate`, `pmset` capture, the single infrastructure retry); no
earlier campaign file is modified.

    uv run python tools/research_023/campaign.py profiles [--write]
    uv run python tools/research_023/campaign.py check [--stage R --out <stage dir> --inputs <dir>]
    uv run python tools/research_023/campaign.py freeze --out <file>
    uv run python tools/research_023/campaign.py inputs --primary <clone> --root <dir>
    uv run python tools/research_023/campaign.py run --stage T --inputs <dir> --out <stage dir>
    uv run python tools/research_023/campaign.py analyze --stage R --inputs <dir> \\
        --out <stage dir> --json <analysis.json> [--sums <SHA256SUMS>]
    uv run python tools/research_023/campaign.py tables --analysis <analysis.json> --out <tables.md>

`check` without `--out` checks the schedule against the checked-in files and the code (profiles
equal their rendering, pins, base-configuration equality, control wiring); with `--out` it also
runs every record audit of `analyze` over a finished stage (completeness, base-row equality,
ledger == seam, never-worse, the control audits) and prints every problem.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import shutil
import sys
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

import r023_analysis as ra  # noqa: E402

from benchmark.profile import ProfileError, parse_profile, read_profile_document  # noqa: E402
from benchmark.strategies import derive  # noqa: E402


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

SCHEDULE = REPO / "config" / "research_023" / "schedule.yaml"
SCHEMA = "r023.campaign/1"
E1, E2 = "split_polish", "marginal_activation"
TOOLS = ("tools/research_023/campaign.py", "tools/research_023/r023_analysis.py",
         "tools/research_022/pruning_campaign.py", "tools/research_022/pruning_work.py",
         "tools/research_022/pruning_analysis.py", "tools/research_021/campaign.py",
         "tools/research_021/analysis.py")  # fmt: skip


class CampaignError(ValueError):
    """The schedule, inputs or request are refused; nothing was executed or written."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_raw(path: Path = SCHEDULE) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise CampaignError(f"{path}: expected schema {SCHEMA}")
    return raw


# ----------------------------------------------------------------------------- arms / profiles


def arms(raw: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """arm key -> its spec, with `algorithm` (the identity the profile runs) filled in."""
    out: dict[str, dict[str, Any]] = {}
    for spec in raw["arms"]:
        key, kind = str(spec["arm"]), str(spec["kind"])
        if key in out:
            raise CampaignError(f"arm {key} registered twice")
        if kind == "reference":
            if key not in raw["bases"]:
                raise CampaignError(f"reference arm {key} has no `bases` entry")
            algorithm = raw["bases"][key]["algorithm"]
        elif kind in ("e1", "e2"):
            if spec["base"] not in raw["bases"]:
                raise CampaignError(f"arm {key}: unknown base {spec['base']}")
            algorithm = E1 if kind == "e1" else E2
        else:
            raise CampaignError(f"arm {key}: unknown kind {kind}")
        out[key] = {**spec, "algorithm": algorithm}
    return out


def _pinned_bytes(spec: Mapping[str, Any]) -> bytes:
    data = (REPO / str(spec["path"])).read_bytes()
    if sha256_bytes(data) != spec["sha256"]:
        raise CampaignError(f"{spec['path']}: sha256 differs from the pin {spec['sha256']}")
    return data


def preset(raw: Mapping[str, Any], algorithm: str) -> dict[str, Any]:
    doc = yaml.safe_load(_pinned_bytes(raw["presets"][algorithm]))
    if doc.get("algorithm") != algorithm:
        raise CampaignError(f"preset of {algorithm} names {doc.get('algorithm')}")
    return dict(doc["options"])


def base_options(raw: Mapping[str, Any], base: str) -> dict[str, Any] | None:
    spec = raw["bases"][base]
    if spec.get("options") is None:
        return None
    if spec["options"] != "preset":
        raise CampaignError(f"base {base}: options must be `preset`")
    return preset(raw, str(spec["algorithm"]))


def arm_document(raw: Mapping[str, Any], key: str) -> dict[str, Any]:
    """The profile document of arm `key`: `canonical` with only `algorithms`, `graph` and
    `algorithm_options` replaced (schedule § settings)."""
    spec = arms(raw)[key]
    doc = copy.deepcopy(read_profile_document(REPO / raw["canonical"]["path"]))
    _pinned_bytes(raw["canonical"])
    doc.pop("algorithm_options", None)
    base = key if spec["kind"] == "reference" else str(spec["base"])
    base_spec = raw["bases"][base]
    doc["algorithms"] = [spec["algorithm"]]
    doc["graph"] = dict(base_spec["graph"])
    own = base_options(raw, base)
    if spec["kind"] == "reference":
        if own is not None:
            doc["algorithm_options"] = {spec["algorithm"]: own}
        return doc
    options: dict[str, Any] = {"base": base_spec["algorithm"], **raw["polish"]}
    if spec["kind"] == "e1":
        options["solver"] = spec["solver"]
        if own is not None:
            options["base_options"] = own
    else:
        if own is not None:
            raise CampaignError(f"arm {key}: E2 bases take no options")
        options.update(mode=spec["mode"], **raw["activation"], arm=spec["e2_arm"])
    doc["algorithm_options"] = {spec["algorithm"]: options}
    return doc


def profile_path(raw: Mapping[str, Any], key: str) -> str:
    return f"{raw['profile_dir']}/{key.lower()}.yaml"


def render_profile(raw: Mapping[str, Any], key: str) -> str:
    doc = arm_document(raw, key)
    path = profile_path(raw, key)
    parse_profile(json.loads(json.dumps(doc)), path)  # refused exactly as the CLI would
    canonical = raw["canonical"]
    header = [
        f"# WHI-1627 research_023 campaign profile, arm `{key}` (R023-C/1 §8). GENERATED by",
        "# `uv run python tools/research_023/campaign.py profiles --write` from",
        f"# {canonical['path']} (sha256 {canonical['sha256']})",
        "# with only `algorithms`, `graph` and `algorithm_options` replaced as",
        "# config/research_023/schedule.yaml says; do not edit by hand -- `campaign.py check`",
        "# refuses any drift from this rendering. Replay literally with --strategies profile.",
    ]
    return "\n".join(header) + "\n" + yaml.safe_dump(doc, sort_keys=False)


def resolved(raw: Mapping[str, Any], key: str) -> dict[str, Any]:
    """The resolved profile of arm `key`, as `main.py run --strategies profile` records it."""
    path = REPO / profile_path(raw, key)
    return parse_profile(read_profile_document(path), str(path)).resolved()


def base_configuration(raw: Mapping[str, Any], key: str) -> dict[str, Any]:
    """What the base of arm `key` (or the reference itself) runs with: the shared sections, the
    `prepare` params the base reads (its declared `search_params` and `graph_params`) and its
    options. An E1/E2 identity hands the base its own params (`split_polish.prepare`; the loader
    forwards exactly the base's graph keys via `graph_params_for`, plus `graph.chunks`, which
    `path_split` does not read) and its `base_options`."""
    from routing.algorithms.registry import ALGORITHMS

    spec = arms(raw)[key]
    res = resolved(raw, key)
    params = res["algorithm_config"][spec["algorithm"]]["params"]
    options = (res.get("algorithm_options") or {}).get(spec["algorithm"], {}).get("options")
    shared = {k: res[k] for k in ("objective", "budget", "measurement", "worker", "search")}
    if spec["kind"] == "reference":
        algorithm, base_opts = spec["algorithm"], options or None
    else:
        algorithm, base_opts = options["base"], options.get("base_options")
    factory = ALGORITHMS[algorithm]
    reads = set(factory.search_params) | set(factory.graph_params)
    return {**shared, "algorithm": algorithm, "options": base_opts,
            "params": {k: v for k, v in params.items() if k in reads}}  # fmt: skip


# ----------------------------------------------------------------------------- campaign


def invocations(raw: Mapping[str, Any], stage: str) -> list[dict[str, Any]]:
    """`<stage>-<arm>` for every arm in registered order, then the work pass `<stage>-WP-<arm>` of
    every arm but the E2 controls (their physical work includes the uncharged treatment re-run)."""
    bundle = raw["stages"][stage]["split"]
    out = []
    for work in (False, True):
        for key, spec in arms(raw).items():
            if work and spec.get("treatment"):
                continue
            out.append({
                "id": f"{stage}{'-WP' if work else ''}-{key}", "stage": stage, "kind": "run",
                "bundle": bundle, "profile": key, "strategies": "profile",
                "algorithms": [spec["algorithm"]], "arm": key, "work_pass": work,
            })  # fmt: skip
    return out


def build(raw: Mapping[str, Any], path: Path = SCHEDULE) -> Any:
    """A 0.2.1 `Campaign` over this schedule's stages T and R."""
    view = dict(raw)
    view["profiles"] = {k: {"path": profile_path(raw, k)} for k in arms(raw)}
    view["stages"] = {s: dict(v) for s, v in raw["stages"].items()}
    expanded = [inv for stage in raw["stages"] for inv in invocations(raw, stage)]
    campaign = c21.Campaign(view, c21._expand(expanded), path)
    for inv in campaign.invocations:
        if inv.id in campaign.by_id:
            raise CampaignError(f"duplicate invocation {inv.id}")
        campaign.by_id[inv.id] = inv
    return campaign


def check(raw: Mapping[str, Any]) -> list[str]:
    """Every problem between the schedule, the checked-in files and the code (empty = ok)."""
    problems: list[str] = []
    for label, spec in (("contract", raw["contract"]),
                        ("probe sums", raw["probe_sums"]),
                        ("canonical", raw["canonical"]), ("m4 settings", raw["m4_settings"]),
                        *((f"preset {k}", v) for k, v in raw["presets"].items())):  # fmt: skip
        try:
            _pinned_bytes(spec)
        except (CampaignError, OSError) as exc:
            problems.append(f"{label}: {exc}")
    m4 = read_profile_document(REPO / raw["m4_settings"]["path"])["graph"]
    if raw["bases"]["M4"]["graph"] != m4:
        problems.append(f"base M4 graph != {raw['m4_settings']['path']} graph {m4}")
    try:
        registered = arms(raw)
    except CampaignError as exc:
        return [*problems, str(exc)]
    for key in registered:
        path = REPO / profile_path(raw, key)
        try:
            text = render_profile(raw, key)
        except (CampaignError, ProfileError, OSError) as exc:
            problems.append(f"profile {key}: {exc}")
            continue
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            problems.append(f"profile {key}: {path.relative_to(REPO)} differs from its rendering")
            continue
        document, _ = derive(read_profile_document(path), "profile", source_path=str(path),
                             source_sha256="0" * 64)  # fmt: skip
        if list(document["algorithms"]) != [registered[key]["algorithm"]]:
            problems.append(f"profile {key}: derives {document['algorithms']}")
    stray = {p.name for p in (REPO / raw["profile_dir"]).glob("*.yaml")} - {
        Path(profile_path(raw, k)).name for k in registered
    }
    if stray:
        problems.append(f"unregistered profiles in {raw['profile_dir']}: {sorted(stray)}")
    if problems:
        return problems
    # contract §8: every base arm is a reference, every reference arm is scheduled
    references = {k for k, s in registered.items() if s["kind"] == "reference"}
    if references != set(raw["bases"]):
        problems.append(f"reference arms {sorted(references)} != bases {sorted(raw['bases'])}")
    # the base of every E1/E2 arm runs exactly as its reference arm
    for key, spec in registered.items():
        if spec["kind"] == "reference":
            continue
        mine, ref = base_configuration(raw, key), base_configuration(raw, str(spec["base"]))
        for field in sorted(set(mine) | set(ref)):
            if mine.get(field) != ref.get(field):
                problems.append(f"arm {key}: base {field} {mine.get(field)!r} != reference "
                                f"{spec['base']} {ref.get(field)!r}")  # fmt: skip
    # every E2 treatment has both controls, each identical to it but for `arm`
    for key, spec in registered.items():
        if spec["kind"] != "e2" or spec["e2_arm"] != "treatment":
            continue
        controls = {s["e2_arm"]: k for k, s in registered.items()
                    if s["kind"] == "e2" and s.get("treatment") == key}  # fmt: skip
        if set(controls) != {"work_matched", "call_matched"}:
            problems.append(f"treatment {key}: controls {sorted(controls)} (both are required)")
        mine = resolved(raw, key)
        for control in controls.values():
            other = resolved(raw, control)
            same = {**other["algorithm_options"][E2]["options"], "arm": "treatment"} == mine[
                "algorithm_options"
            ][E2]["options"]
            if not same or other["algorithm_config"] != mine["algorithm_config"]:
                problems.append(f"control {control}: differs from {key} beyond `arm`")
    for key, spec in registered.items():
        treatment = spec.get("treatment")
        if treatment is not None and registered.get(treatment, {}).get("e2_arm") != "treatment":
            problems.append(f"control {key}: {treatment} is not a registered treatment")
    return problems


# ----------------------------------------------------------------------------- freeze / inputs


def _file_pin(path: str) -> dict[str, str]:
    return {"path": path, "sha256": sha256_bytes((REPO / path).read_bytes())}


def freeze_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The pins of everything stage R executes and analyses from (schedule, tools, profiles,
    contract, presets), the resolved settings of every invocation and the stage inventories."""
    campaign = build(raw)
    return {
        "schema": "r023.campaign-freeze/1",
        "issue": raw["issue"],
        "contract": raw["contract"]["key"],
        "files": {
            "schedule": _file_pin(str(SCHEDULE.relative_to(REPO))),
            **{
                f"tool:{Path(t).name}" if "research_023" in t else f"tool:{t}": _file_pin(t)
                for t in TOOLS
            },
            **{f"profile:{k}": _file_pin(profile_path(raw, k)) for k in arms(raw)},
            "contract": _file_pin(str(raw["contract"]["path"])),
            "canonical": _file_pin(str(raw["canonical"]["path"])),
            "m4_settings": _file_pin(str(raw["m4_settings"]["path"])),
            **{f"preset:{k}": _file_pin(str(v["path"])) for k, v in raw["presets"].items()},
        },  # fmt: skip
        "inputs": raw["inputs"],
        "budget": read_profile_document(REPO / raw["canonical"]["path"])["budget"],
        "seed": read_profile_document(REPO / raw["canonical"]["path"])["measurement"]["seed"],
        "polish": raw["polish"],
        "activation": raw["activation"],
        "stages": raw["stages"],
        "not_scheduled": raw["not_scheduled"],
        "rules_sha256": sha256_bytes(json.dumps(raw["rules"], sort_keys=True).encode()),
        "effective_settings": {
            inv.id: ident
            for inv in campaign.invocations
            if (ident := c21.resolved_identity(campaign, inv)) is not None
        },  # fmt: skip
        "inventory": {s: c21.schedule(campaign, s) for s in raw["stages"]},
    }


def prepare_inputs(raw: Mapping[str, Any], primary: Path, root: Path) -> dict[str, Any]:
    """Copy every registered input into `root` (outside any worktree), hash-verified."""
    out: dict[str, Any] = {}
    for key, spec in raw["inputs"].items():
        target = root / key
        if not target.exists():
            shutil.copytree(primary / spec["source"], target)
        for name, pin in (("manifest.json", "bundle_hash"), ("cases.jsonl", "cases_sha256")):
            if sha256_bytes((target / name).read_bytes()) != spec[pin]:
                raise CampaignError(f"input {key}: {target / name} differs from its pin")
        out[key] = str(target)
    return out


# ----------------------------------------------------------------------------- analysis


def stage_runs(raw: Mapping[str, Any], stage: str, out: Path) -> tuple[dict[str, Any], list[str]]:
    """invocation id -> its run directory (the effective ledger entry: own `ok` end, else its
    single infrastructure retry), plus every invocation without a completed run."""
    done = c21.read_ledger(out)
    runs: dict[str, Any] = {}
    problems: list[str] = []
    for inv in invocations(raw, stage):
        entry, deviation = c21.effective_entry(done, inv["id"])
        if entry is None or entry.get("result") != "ok" or not entry.get("run_dir"):
            state = None if entry is None else entry.get("result")
            problems.append(f"{inv['id']}: no completed run (ledger: {state})")
            continue
        runs[inv["id"]] = {"run_dir": entry["run_dir"], "deviation": deviation}
    return runs, problems


def raw_sums(runs: Mapping[str, Mapping[str, Any]], root: Path) -> dict[str, str]:
    """`<path relative to root>` -> sha256 of every run's manifest.json and cases.jsonl."""
    sums = {}
    for entry in runs.values():
        run = Path(entry["run_dir"])
        for name in ("manifest.json", "cases.jsonl"):
            sums[str((run / name).relative_to(root))] = sha256_bytes((run / name).read_bytes())
    return dict(sorted(sums.items()))


def analyze(raw: Mapping[str, Any], stage: str, inputs: Path, out: Path) -> dict[str, Any]:
    from report.aggregate import load_run

    runs, problems = stage_runs(raw, stage, out)
    bundle = str(raw["stages"][stage]["split"])
    expected = [
        json.loads(x)["case_id"]
        for x in (inputs / bundle / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        if x
    ]
    loaded: dict[str, Any] = {}
    for inv_id, entry in runs.items():
        loaded[inv_id] = load_run(entry["run_dir"], bundle_dirs=[inputs / bundle])
    git = sorted({str(r.manifest.git_revision) for r in loaded.values()})
    result = ra.analyze_stage(raw, arms(raw), stage, loaded, expected)
    result["problems"] = problems + result["problems"]
    result.update(
        stage=stage, bundle=bundle, bundle_hash=raw["inputs"][bundle]["bundle_hash"],
        git_revisions=git,
        deviations={k: v["deviation"] for k, v in runs.items() if v["deviation"]},
        raw_sha256=raw_sums(runs, out), not_scheduled=raw["not_scheduled"],
    )  # fmt: skip
    if len(git) != 1:
        result["problems"].append(f"runs come from {len(git)} source revisions: {git}")
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="campaign.py", description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--schedule", default=str(SCHEDULE))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("profiles")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("check")
    p.add_argument("--stage")
    p.add_argument("--out")
    p.add_argument("--inputs")
    p = sub.add_parser("freeze")
    p.add_argument("--out", required=True)
    p = sub.add_parser("inputs")
    p.add_argument("--primary", required=True)
    p.add_argument("--root", required=True)
    p = sub.add_parser("run")
    p.add_argument("--stage", required=True, choices=["T", "R"])
    p.add_argument("--inputs", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--lanes", type=int)
    p.add_argument("--only", action="append", default=[])
    p.add_argument("--retry-infrastructure")
    p = sub.add_parser("analyze")
    p.add_argument("--stage", required=True, choices=["T", "R"])
    p.add_argument("--inputs", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--json", required=True)
    p.add_argument("--sums", help="also write the raw-record SHA256SUMS here")
    p = sub.add_parser("tables")
    p.add_argument("--analysis", required=True)
    p.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        raw = load_raw(Path(args.schedule))
        if args.command == "profiles":
            for key in arms(raw):
                text = render_profile(raw, key)
                path = REPO / profile_path(raw, key)
                if args.write:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                print(f"{profile_path(raw, key)} {sha256_bytes(text.encode())}")
            return 0
        if args.command == "check":
            problems = check(raw)
            if args.out:
                if not (args.stage and args.inputs):
                    raise CampaignError("check --out needs --stage and --inputs")
                result = analyze(raw, args.stage, Path(args.inputs), Path(args.out))
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
        if args.command == "run":
            problems = check(raw)
            if problems:
                raise CampaignError(f"check reports {len(problems)} problem(s): {problems[:3]}")
            return int(pc.execute(build(raw), args.stage, inputs=Path(args.inputs),
                                  out=Path(args.out), lanes=args.lanes, only=args.only,
                                  retry_infrastructure=args.retry_infrastructure))  # fmt: skip
        if args.command == "analyze":
            result = analyze(raw, args.stage, Path(args.inputs), Path(args.out))
            Path(args.json).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
            if args.sums:
                lines = [f"{d}  {n}\n" for n, d in result["raw_sha256"].items()]
                Path(args.sums).write_text("".join(lines), encoding="utf-8")
            print(f"analysis: {len(result['problems'])} problem(s) -> {args.json}")
            return 1 if result["problems"] else 0
        if args.command == "tables":
            analysis = json.loads(Path(args.analysis).read_text(encoding="utf-8"))
            Path(args.out).write_text(ra.render_tables(analysis), encoding="utf-8")
            print(f"wrote {args.out}")
            return 0
    except (CampaignError, ProfileError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""`main.py quote`: compare the profiled algorithms on ONE exploratory Exact Input request
(WHI-1498), through the ordinary measured-run path.

`prepare_quote` does every check before anything is written or any solver starts: the
parent bundle and source profile load and validate, the objective is supported, both
tokens resolve unambiguously and the amount converts exactly. `run_quote` then writes

    <quotes_dir>/<quote id>/
        quote.json     the request record: inputs, resolution, parent/profile identity
        profile.yaml   the effective profile: the source with warmup 0, repeats 1,
                       memory_pass false (the source YAML is never modified)
        bundle/        the derived single-case bundle (`snapshot.request`)
        runs/<run id>/ the run, written by the unchanged `benchmark.runner.run_experiment`

so every algorithm gets exactly one solve attempt in an isolated worker under the
profile's budget, is independently evaluated, and keeps failures and labelled last
valid candidates, exactly like a batch run. The recorded replay command is a plain
`main.py run` over the derived bundle and effective profile; `main.py report RUN_DIR`
renders the saved run offline.

`--strategies` (WHI-1528, `benchmark.strategies`; default `all`) derives the compared set
from the source profile first: `all` = the source's base (and custom) algorithms, then the
named optimized strategies, then the experimental `metis_inspired` (WHI-1540); `base`,
`optimized`, or `profile` = the source's exact selection. The single-run override applies to
the derived document, which is the saved effective profile; the replay command runs it with
`--strategies profile`, so a replay keeps exactly the saved algorithms and recipe settings.
`quote.json` records the mode and groups. In `profile` mode the effective profile is
byte-for-byte the pre-WHI-1528 one.

`empirical_cost` profiles are refused: the derived bundle carries no frozen price
context, and without one that objective ranks plans differently (unranked gross), so the
comparison would silently differ from the same objective on the corpus.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from benchmark.profile import (
    SINGLE_RUN_MEASUREMENT,
    RunProfile,
    load_profile,
    parse_profile,
    read_profile_document,
    single_run_document,
)
from benchmark.results import RunManifest, new_run_id
from benchmark.runner import run_experiment
from benchmark.strategies import DEFAULT_MODE, DERIVATION_NOTE, derive, selected_groups
from snapshot.bundle import load_bundle, sha256_file
from snapshot.models import Case, SnapshotBundle
from snapshot.request import (
    EXPLORATORY_LABEL,
    RequestError,
    TokenInfo,
    derive_request_bundle,
    parse_amount,
    request_case,
    resolve_token,
)

DEFAULT_QUOTES_DIR = "data/quotes"
QUOTE_RECORD_SCHEMA = "exploratory-quote/1"
QUOTE_FILE = "quote.json"
PROFILE_FILE = "profile.yaml"


class QuoteError(ValueError):
    """The request or profile cannot be run as a single-request comparison."""


@dataclass(frozen=True)
class PreparedQuote:
    inputs: dict[str, str]
    parent: SnapshotBundle
    source_profile: dict[str, Any]
    token_in: TokenInfo
    token_out: TokenInfo
    case: Case
    strategies: str = "profile"
    # The validated single-run effective document (source + strategy derivation + override).
    effective_document: dict[str, Any] | None = None


@dataclass(frozen=True)
class QuoteRun:
    quote_dir: Path
    bundle: SnapshotBundle
    manifest: RunManifest


def prepare_quote(
    *,
    bundle: str,
    profile: str,
    token_in: str,
    token_out: str,
    amount: str,
    strategies: str = DEFAULT_MODE,
) -> PreparedQuote:
    """Every validation, before any write or solver work."""
    parent = load_bundle(bundle)
    document = read_profile_document(profile)
    derived, _ = derive(
        document, strategies, source_path=profile, source_sha256=sha256_file(Path(profile))
    )
    effective = single_run_document(derived)
    parsed: RunProfile = parse_profile(effective, profile)
    if parsed.objective.mode == "empirical_cost":
        raise QuoteError(
            f"{profile}: objective empirical_cost is not supported by quote -- the "
            "exploratory request bundle carries no frozen price context, which would change "
            "how plans are ranked; use a gross_only profile (e.g. config/daily_gross.yaml)"
        )
    tin = resolve_token(parent, token_in, "--token-in")
    tout = resolve_token(parent, token_out, "--token-out")
    case = request_case(parent, tin, tout, parse_amount(amount, tin))
    return PreparedQuote(
        inputs={
            "bundle": bundle,
            "profile": profile,
            "token_in": token_in,
            "token_out": token_out,
            "amount": amount,
        },
        parent=parent,
        source_profile=document,
        token_in=tin,
        token_out=tout,
        case=case,
        strategies=strategies,
        effective_document=effective,
    )


def run_quote(prepared: PreparedQuote, quotes_dir: str | Path) -> QuoteRun:
    quote_dir = Path(quotes_dir) / new_run_id()
    quote_dir.mkdir(parents=True)  # exclusive: never reuse an earlier quote
    effective_path = quote_dir / PROFILE_FILE
    source_path = prepared.inputs["profile"]
    source_sha = sha256_file(Path(source_path))
    header = (
        "# Effective single-run profile written by `main.py quote` (WHI-1498).\n"
        f"# Source profile: {source_path} (sha256 {source_sha}); the source is unchanged.\n"
        "# Override: measurement.warmup 0, measurement.repeats 1, measurement.memory_pass "
        "false -- one solve attempt per algorithm, no separate memory pass.\n"
    )
    if prepared.strategies != "profile":
        header += (
            f"# Strategies: --strategies {prepared.strategies} (WHI-1528, WHI-1540): "
            f"{DERIVATION_NOTE}\n"
        )
    effective = prepared.effective_document
    if effective is None:  # a PreparedQuote built without prepare_quote: the source as is
        effective = single_run_document(prepared.source_profile)
    effective_path.write_text(header + yaml.safe_dump(effective, sort_keys=False), encoding="utf-8")
    profile = load_profile(effective_path)
    bundle = derive_request_bundle(prepared.parent, prepared.case, quote_dir / "bundle")
    results_dir = quote_dir / "runs"
    replay = shlex.join(
        [
            "uv", "run", "python", "main.py", "run",
            "--bundle", str(quote_dir / "bundle"),
            "--profile", str(effective_path),
            "--results-dir", str(results_dir),
            "--strategies", "profile",
        ]
    )  # fmt: skip
    record = {
        "schema": QUOTE_RECORD_SCHEMA,
        "exploratory": True,
        "label": EXPLORATORY_LABEL,
        "created_at": datetime.now(UTC).isoformat(),
        "inputs": prepared.inputs,
        "request": {
            "case_id": prepared.case.case_id,
            "token_in": prepared.token_in.to_dict(),
            "token_out": prepared.token_out.to_dict(),
            "amount_in_raw": str(prepared.case.amount_in),
        },
        "parent_bundle": {
            "path": prepared.inputs["bundle"],
            "bundle_id": prepared.parent.bundle_id,
            "bundle_hash": prepared.parent.bundle_hash,
        },
        "source_profile": {"path": source_path, "sha256": source_sha},
        "effective_profile": {
            "path": str(effective_path),
            "sha256": sha256_file(effective_path),
            "measurement_override": dict(SINGLE_RUN_MEASUREMENT),
        },
        "request_bundle": {
            "path": str(quote_dir / "bundle"),
            "bundle_id": bundle.bundle_id,
            "bundle_hash": bundle.bundle_hash,
        },
        "results_dir": str(results_dir),
        "replay_command": replay,
        "strategies": {
            "mode": prepared.strategies,
            "groups": {g: members for g, members in selected_groups(profile)},
        },
    }
    (quote_dir / QUOTE_FILE).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = run_experiment(bundle, profile, results_dir=results_dir, replay_command=replay)
    return QuoteRun(quote_dir=quote_dir, bundle=bundle, manifest=manifest)


__all__ = [
    "DEFAULT_QUOTES_DIR",
    "PreparedQuote",
    "QuoteError",
    "QuoteRun",
    "RequestError",
    "prepare_quote",
    "run_quote",
]

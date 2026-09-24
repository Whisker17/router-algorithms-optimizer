"""CLI entrypoint: `prepare`, `validate`, `run` (docs/DESIGN.md §4.4 core flows,
§4.2 "thin CLI"). `report` and protocol-verification subcommands land with their
owning modules; `snapshot.preflight` remains the standalone online-admission tool.

All three subcommands here are fully offline for the synthetic source/bundle:
no `.env` credential is read and no network call is made (docs/DESIGN.md §2.12:
"offline run/report and public-RPC-only preparation must not require a private
RPC credential"). A future online collector would validate its own required
credentials at the start of its own `prepare --source ...` branch, not here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from benchmark.profile import ProfileError, load_profile
from benchmark.runner import run_experiment
from snapshot.bundle import BundleError, load_bundle
from snapshot.collectors import get_collector

DEFAULT_RESULTS_DIR = "data/results"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="router-algorithms-optimizer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_p = subparsers.add_parser(
        "prepare", help="Build a bundle from a registered prepare source (offline sources only)"
    )
    prepare_p.add_argument(
        "--source", required=True, help="Registered collector name, e.g. 'synthetic'"
    )
    prepare_p.add_argument("--output", required=True, help="Bundle directory to create")

    validate_p = subparsers.add_parser("validate", help="Validate a bundle directory")
    validate_p.add_argument("--bundle", required=True)

    run_p = subparsers.add_parser(
        "run", help="Run a profile's algorithms over a bundle and save a versioned result"
    )
    run_p.add_argument("--bundle", required=True)
    run_p.add_argument("--profile", required=True)
    run_p.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR)

    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _cmd_prepare(args: argparse.Namespace) -> int:
    try:
        collector = get_collector(args.source)
    except KeyError as exc:
        print(f"prepare failed: {exc}", file=sys.stderr)
        return 1
    try:
        bundle = collector(Path(args.output))
    except BundleError as exc:
        print(f"prepare failed: {exc}", file=sys.stderr)
        return 1
    print(f"prepared bundle {bundle.bundle_id!r} ({bundle.kind}) at {bundle.source_path}")
    print(f"bundle_hash={bundle.bundle_hash}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        bundle = load_bundle(args.bundle)
    except BundleError as exc:
        print(f"validate failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"bundle {bundle.bundle_id!r} ({bundle.kind}) is valid: "
        f"{len(bundle.pools)} pool(s), {len(bundle.cases)} case(s)"
    )
    print(f"bundle_hash={bundle.bundle_hash}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        bundle = load_bundle(args.bundle)
        profile = load_profile(args.profile)
    except (BundleError, ProfileError) as exc:
        print(f"run failed: {exc}", file=sys.stderr)
        return 1

    replay_command = (
        f"uv run python main.py run --bundle {args.bundle} --profile {args.profile} "
        f"--results-dir {args.results_dir}"
    )
    manifest = run_experiment(
        bundle,
        profile,
        results_dir=args.results_dir,
        replay_command=replay_command,
    )
    print(f"run {manifest.run_id!r} saved to {manifest.run_dir}")
    print(f"objective: {manifest.objective_label}")
    print(f"cases: {manifest.case_count}")
    print(f"replay: {manifest.replay_command}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "prepare":
        return _cmd_prepare(args)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "run":
        return _cmd_run(args)
    raise AssertionError(f"unreachable: unknown command {args.command!r}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())

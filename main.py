"""CLI entrypoint: `prepare`, `validate`, `run` (docs/DESIGN.md §4.4 core flows,
§4.2 "thin CLI"). `report` and protocol-verification subcommands land with their
owning modules; `snapshot.preflight` remains the standalone online-admission tool.

`corpus sql|ingest|plan|assemble` (WHI-1436) prepares the frozen five-source corpus:
the Dune SQL is printed (queries run through the operator's Dune access, never from
here), saved results are ingested as canonical exports, `plan` derives cases, prices,
the envelope and the five prepare configs, and -- after `prepare --source <each>` has
run at the corpus block -- `assemble` publishes the one corpus bundle. `plan` and
`assemble` read only local files.

`run` measures every profiled algorithm in isolated worker processes under the
profile's declared budget (WHI-1437, `benchmark.runner`); `run --order reverse|shuffle`
plus `order-check RUN_A RUN_B` is the state-leak check. SIGTERM/Ctrl-C finalize the run
as `interrupted` with every unfinished case recorded as `cancelled` (exit code 130).

`validate` and `run` are always offline: they read only the bundle directory
(docs/DESIGN.md §4.5: "Ordinary offline commands require no RPC/Dune access").
`prepare --source synthetic` is offline too. `prepare --source agni --block N` is the
explicit online preparation path (WHI-1429): it reads the public Mantle RPC at exactly
block N (never `latest`) and publishes only after its admission checks pass. No `.env`
credential is read anywhere here (docs/DESIGN.md §2.12: "public-RPC-only preparation
must not require a private RPC credential").
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from benchmark.profile import ProfileError, load_profile
from benchmark.results import ResultError
from benchmark.runner import RunInterrupted, compare_runs, run_experiment
from snapshot.bundle import BundleError, load_bundle
from snapshot.collectors import PrepareError, PrepareRequest, get_collector
from snapshot.collectors.base import DEFAULT_CATALOG_PATH, DEFAULT_RPC_CACHE_DIR
from snapshot.config import ConfigError, load_catalog
from snapshot.corpus import (
    DEFAULT_CORPUS_CONFIG,
    CorpusError,
    assemble,
    generate_sql,
    ingest_export,
    load_corpus_config,
    run_plan,
    validate_corpus_bundle,
    write_plan,
)
from snapshot.prepare_config import PrepareConfigError

DEFAULT_RESULTS_DIR = "data/results"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="router-algorithms-optimizer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_p = subparsers.add_parser(
        "prepare", help="Build a bundle from a registered prepare source"
    )
    prepare_p.add_argument(
        "--source", required=True, help="Registered collector name, e.g. 'synthetic' or 'agni'"
    )
    prepare_p.add_argument("--output", required=True, help="Bundle directory to create")
    prepare_p.add_argument(
        "--block", type=int, default=None, help="Online sources: the explicit block to freeze"
    )
    prepare_p.add_argument(
        "--block-hash", default=None, help="Online sources: refuse unless block N has this hash"
    )
    prepare_p.add_argument(
        "--rpc-url", default=None, help="Online sources: override the catalog's public RPC"
    )
    prepare_p.add_argument(
        "--prepare-config", default=None, help="Online sources: selection config override"
    )
    prepare_p.add_argument("--catalog", default=str(DEFAULT_CATALOG_PATH))
    prepare_p.add_argument(
        "--rpc-cache-dir",
        default=str(DEFAULT_RPC_CACHE_DIR),
        help="On-disk cache of block-hash-pinned reads ('' disables)",
    )

    corpus_p = subparsers.add_parser("corpus", help="Prepare the frozen five-source corpus")
    corpus_sub = corpus_p.add_subparsers(dest="corpus_command", required=True)
    for name, help_text in (
        ("sql", "Print the Dune SQL of one corpus query"),
        ("ingest", "Turn a saved Dune result into a canonical export"),
        ("plan", "Derive cases, prices, envelope and the five prepare configs"),
        ("assemble", "Publish the corpus bundle from the five per-source bundles"),
        ("fixture", "Cut the checked-in regression fixture from the full corpus bundle"),
        ("cohort", "Cut the matched V2/V3 (sor_compatible) comparison bundle, every case"),
    ):
        sp = corpus_sub.add_parser(name, help=help_text)
        sp.add_argument("--config", default=str(DEFAULT_CORPUS_CONFIG))
        sp.add_argument("--exports", default="tests/fixtures/corpus/dune")
        if name == "sql":
            sp.add_argument("--step", required=True, choices=["activity", "strata", "prices"])
        if name == "ingest":
            sp.add_argument("--name", required=True, choices=["activity", "strata", "prices"])
            sp.add_argument("--raw", required=True, help="Saved Dune result JSON")
        if name in ("plan", "assemble"):
            sp.add_argument("--plan", required=True, help="plan.json path")
            sp.add_argument("--prepare-dir", default="config/corpus/prepare")
            sp.add_argument("--catalog", default=str(DEFAULT_CATALOG_PATH))
        if name == "assemble":
            sp.add_argument("--sources", required=True, help="Dir holding <collector>/ bundles")
            sp.add_argument("--output", required=True)
        if name == "fixture":
            sp.add_argument("--bundle", required=True, help="The full corpus bundle")
            sp.add_argument("--selection", default="config/corpus/fixture.yaml")
            sp.add_argument("--output", required=True)
        if name == "cohort":
            sp.add_argument("--bundle", required=True, help="A corpus bundle")
            sp.add_argument("--output", required=True)

    validate_p = subparsers.add_parser("validate", help="Validate a bundle directory")
    validate_p.add_argument("--bundle", required=True)

    run_p = subparsers.add_parser(
        "run", help="Run a profile's algorithms over a bundle and save a versioned result"
    )
    run_p.add_argument("--bundle", required=True)
    run_p.add_argument("--profile", required=True)
    run_p.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR)
    run_p.add_argument(
        "--order",
        choices=["fixed", "reverse", "shuffle"],
        default=None,
        help="Override the profile's case order (state-leak checks); recorded in the run",
    )

    order_p = subparsers.add_parser(
        "order-check",
        help="Compare two complete runs' deterministic outputs (e.g. fixed vs reverse order)",
    )
    order_p.add_argument("run_a")
    order_p.add_argument("run_b")

    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _cmd_prepare(args: argparse.Namespace) -> int:
    try:
        collector = get_collector(args.source)
    except KeyError as exc:
        print(f"prepare failed: {exc}", file=sys.stderr)
        return 1
    request = PrepareRequest(
        output_dir=Path(args.output),
        block_number=args.block,
        expected_block_hash=args.block_hash,
        rpc_url=args.rpc_url,
        prepare_config=Path(args.prepare_config) if args.prepare_config else None,
        catalog_path=Path(args.catalog),
        cache_dir=Path(args.rpc_cache_dir) if args.rpc_cache_dir else None,
    )
    try:
        bundle = collector(request)
    except (BundleError, PrepareError, ConfigError, PrepareConfigError) as exc:
        print(f"prepare failed: {exc}", file=sys.stderr)
        return 1
    print(f"prepared bundle {bundle.bundle_id!r} ({bundle.kind}) at {bundle.source_path}")
    print(f"bundle_hash={bundle.bundle_hash}")
    return 0


def _cmd_corpus(args: argparse.Namespace) -> int:
    import json

    exports = Path(args.exports)
    try:
        config = load_corpus_config(args.config)
        if args.corpus_command == "sql":
            sys.stdout.write(generate_sql(config, args.step, exports))
            return 0
        if args.corpus_command == "ingest":
            from snapshot.corpus import EXPORT_FILES

            sql_name, export_name = EXPORT_FILES[args.name]
            export = ingest_export(args.name, args.raw, exports / sql_name, exports / export_name)
            print(f"export {args.name}: {len(export.rows)} row(s), sha256={export.sha256}")
            return 0
        if args.corpus_command == "cohort":
            from snapshot.corpus import sor_cohort_bundle

            full = load_bundle(args.bundle)
            validate_corpus_bundle(full)
            bundle = sor_cohort_bundle(full, Path(args.output))
            validate_corpus_bundle(bundle)
            print(
                f"wrote matched V2/V3 cohort {bundle.bundle_id!r}: {len(bundle.pools)} pool(s), "
                f"{len(bundle.cases)} case(s) at {bundle.source_path}"
            )
            print(f"bundle_hash={bundle.bundle_hash}")
            return 0
        if args.corpus_command == "fixture":
            from snapshot.corpus import fixture_selection, subset_corpus_bundle

            full = load_bundle(args.bundle)
            validate_corpus_bundle(full)
            pool_ids, case_ids = fixture_selection(full, Path(args.selection))
            bundle = subset_corpus_bundle(full, pool_ids, case_ids, Path(args.output))
            validate_corpus_bundle(bundle)
            print(
                f"wrote fixture {bundle.bundle_id!r}: {len(bundle.pools)} pool(s), "
                f"{len(bundle.cases)} case(s) at {bundle.source_path}"
            )
            print(f"bundle_hash={bundle.bundle_hash}")
            return 0
        catalog = load_catalog(args.catalog)
        if args.corpus_command == "plan":
            plan = run_plan(config, exports, catalog)
            for path, digest in write_plan(
                config, plan, Path(args.plan), Path(args.prepare_dir)
            ).items():
                print(f"wrote {path} sha256={digest}")
            return 0
        # Re-derive the plan from the checked-in exports; the saved plan must agree.
        plan = run_plan(config, exports, catalog)
        saved = json.loads(Path(args.plan).read_text())
        if json.dumps(saved, sort_keys=True) != json.dumps(plan, sort_keys=True):
            raise CorpusError(f"{args.plan}: differs from the plan the exports derive")
        sources = Path(args.sources)
        bundle = assemble(
            config,
            plan,
            {s.collector: sources / s.collector for s in config.sources},
            Path(args.output),
            catalog=catalog,
            prepare_dir=Path(args.prepare_dir),
        )
    except (CorpusError, BundleError, ConfigError, OSError, KeyError) as exc:
        print(f"corpus {args.corpus_command} failed: {exc}", file=sys.stderr)
        return 1
    print(f"published corpus bundle {bundle.bundle_id!r} at {bundle.source_path}")
    print(f"bundle_hash={bundle.bundle_hash}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        bundle = load_bundle(args.bundle)
        summary = validate_corpus_bundle(bundle) if bundle.corpus is not None else None
    except (BundleError, CorpusError) as exc:
        print(f"validate failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"bundle {bundle.bundle_id!r} ({bundle.kind}) is valid: "
        f"{len(bundle.pools)} pool(s), {len(bundle.cases)} case(s)"
    )
    if summary is not None:
        print(
            f"corpus: sources={summary['sources']} groups={summary['groups']} "
            f"splits={summary['splits']}; every pool covers the envelope in both directions"
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
    if args.order is not None:
        replay_command += f" --order {args.order}"

    def _terminate(signum: int, frame: object) -> None:
        raise KeyboardInterrupt(f"signal {signum}")

    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        manifest = run_experiment(
            bundle,
            profile,
            results_dir=args.results_dir,
            replay_command=replay_command,
            order=args.order,
        )
    except RunInterrupted as exc:
        print(
            f"run {exc.manifest.run_id!r} INTERRUPTED; {exc.manifest.case_count} record(s) "
            f"(unfinished cases recorded as cancelled) in {exc.manifest.run_dir}",
            file=sys.stderr,
        )
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(f"run {manifest.run_id!r} saved to {manifest.run_dir}")
    print(f"objective: {manifest.objective_label}")
    print(f"cases: {manifest.case_count}")
    print(f"statuses: {manifest.status_counts}")
    print(f"replay: {manifest.replay_command}")
    return 0


def _cmd_order_check(args: argparse.Namespace) -> int:
    try:
        mismatches = compare_runs(args.run_a, args.run_b)
    except ResultError as exc:
        print(f"order-check failed: {exc}", file=sys.stderr)
        return 1
    if mismatches:
        print(f"{len(mismatches)} order-dependent result(s) -- possible state leakage:")
        for line in mismatches:
            print(f"  {line}")
        return 1
    print("no order-dependent results: deterministic outputs agree")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "prepare":
        return _cmd_prepare(args)
    if args.command == "corpus":
        return _cmd_corpus(args)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "order-check":
        return _cmd_order_check(args)
    raise AssertionError(f"unreachable: unknown command {args.command!r}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())

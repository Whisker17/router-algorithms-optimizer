"""CLI entrypoint: `prepare`, `validate`, `run`, `report` (docs/DESIGN.md §4.4 core
flows, §4.2 "thin CLI"). Protocol-verification subcommands land with their owning
modules; `snapshot.preflight` remains the standalone online-admission tool.

`report RUN_DIR [RUN_DIR ...]` (WHI-1446) writes an offline `report.html` plus CSV
summaries from saved run records only (`report.render`); each run is its own cohort
section, so a matched V2/V3 cohort run and a full-coverage run can sit side by side
without being pooled.

`corpus sql|ingest|plan|assemble` (WHI-1436) prepares the frozen five-source corpus:
the Dune SQL is printed (queries run through the operator's Dune access, never from
here), saved results are ingested as canonical exports, `plan` derives cases, prices,
the envelope and the five prepare configs, and -- after `prepare --source <each>` has
run at the corpus block -- `assemble` publishes the one corpus bundle. `plan` and
`assemble` read only local files.

`costs sql|ingest|evidence|fit|report` (WHI-1445) prepares the empirical execution-cost
model: the Dune SQL is printed (run through the operator's Dune access), saved results
are ingested as canonical exports, `evidence` reads a handful of receipts / balances /
fee parameters over the public RPC, and `fit` writes the frozen cost-model artifact from
those checked-in files alone (offline). `report` re-verifies an artifact and prints its
holdout validation table.

`corpus split` (WHI-1447) cuts the declared `tuning` or `report` split of a corpus
bundle (every pool, that split's cases) so calibration and the held-out report run on
disjoint declared cases. `calibrate profiles|summarize` (WHI-1447) writes one run profile
per grid point of a base profile and summarizes saved runs of one bundle (statuses,
time/quote distributions, shortfall vs the best known gross among them); offline.

`acceptance --run LABEL=DIR ... --order-check LABEL=A,B --report DIR --output PATH`
(WHI-1447) composes the write-once final experiment manifest from complete saved runs,
re-running the declared order checks; offline.

`run` measures every profiled algorithm in isolated worker processes under the
profile's declared budget (WHI-1437, `benchmark.runner`); `run --order reverse|shuffle`
plus `order-check RUN_A RUN_B` is the state-leak check. SIGTERM/Ctrl-C finalize the run
as `interrupted` with every unfinished case recorded as `cancelled` (exit code 130).

`quote --bundle B --profile P --token-in X --token-out Y --amount N [--details]`
(WHI-1498) compares the profile's algorithms on one exploratory exact-input request with
exactly one solve attempt each (`benchmark.quote`: a derived single-case bundle and an
effective profile with warmup 0 / repeats 1 / memory_pass false, run by the unchanged
runner). `report` renders such a run as a single-case text report (`report.quote`).

`run` and `quote` take `--strategies all|base|optimized|profile` (WHI-1528,
`benchmark.strategies`; default `all`): the six base strategies the profile selects, then
the two named optimized strategies (`uni_sor_adaptive`, `uni_sor_optimized`: registered
L08 recipes of the heuristic over the `uni_sor_port` core), then the experimental
Metis-inspired (NOT Jupiter Metis) `metis_inspired` (WHI-1540; undeclared graph.label_hops /
label_pruning / chunks from `config/metis_challenge/m4.yaml`), run sequentially under the
profile's objective, budget and search. `base` / `optimized` run one group; `profile`
runs the profile's exact selection (the replay path: a saved eight-algorithm profile stays
eight). The derived effective profile is validated before
anything is written, saved (`run`: `<results-dir>/<run id>/profile.yaml`; `quote`: its
`profile.yaml`) and replayed with `--strategies profile`; source profiles are never edited.

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
from typing import Any

from benchmark.profile import ProfileError, load_profile, read_profile_document
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
        ("split", "Cut the declared tuning or report split of a corpus bundle, every pool"),
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
        if name == "split":
            sp.add_argument("--bundle", required=True, help="A corpus bundle (full or cohort)")
            sp.add_argument("--split", required=True, choices=["tuning", "report"])
            sp.add_argument("--output", required=True)

    costs_p = subparsers.add_parser("costs", help="Calibrate the empirical execution-cost model")
    costs_sub = costs_p.add_subparsers(dest="costs_command", required=True)
    for name, help_text in (
        ("sql", "Print the Dune SQL of one cost-calibration query"),
        ("ingest", "Turn a saved Dune result into a canonical export"),
        ("evidence", "Capture receipt/balance/fee-parameter evidence (public RPC)"),
        ("fit", "Fit and write the frozen cost-model artifact (offline)"),
        ("report", "Re-verify an artifact and print its holdout validation"),
    ):
        sp = costs_sub.add_parser(name, help=help_text)
        sp.add_argument("--config", default="config/cost_calibration.yaml")
        sp.add_argument("--exports", default="tests/fixtures/costs")
        if name in ("sql", "ingest"):
            sp.add_argument("--name", required=True, choices=["census", "samples"])
        if name == "ingest":
            sp.add_argument("--raw", required=True, help="Saved Dune result JSON")
            sp.add_argument("--query-id", type=int, default=None)
        if name == "evidence":
            sp.add_argument("--force", action="store_true", help="Replace saved evidence")
        if name in ("fit", "report"):
            sp.add_argument("--model", required=True, help="Cost-model artifact path")

    calibrate_p = subparsers.add_parser(
        "calibrate", help="Profile calibration: generate sweep profiles, summarize runs"
    )
    calibrate_sub = calibrate_p.add_subparsers(dest="calibrate_command", required=True)
    cal_profiles = calibrate_sub.add_parser(
        "profiles", help="Write one profile per grid point of a base profile"
    )
    cal_profiles.add_argument("--base", required=True, help="Base run profile YAML")
    cal_profiles.add_argument("--output", required=True, help="Directory for the profiles")
    cal_profiles.add_argument(
        "--grid", action="append", required=True, help="Axis key=v1,v2 (search/graph key)"
    )
    cal_profiles.add_argument(
        "--algorithms", default=None, help="Comma-separated algorithm list override"
    )
    cal_profiles.add_argument("--prefix", default="sweep")
    cal_summary = calibrate_sub.add_parser(
        "summarize", help="Per-variant statuses, time/quote distributions, best-known shortfall"
    )
    cal_summary.add_argument("runs", nargs="+", help="Complete runs over ONE bundle")
    cal_summary.add_argument("--json", default=None, help="Also write the summary as JSON")

    acceptance_p = subparsers.add_parser(
        "acceptance", help="Compose the immutable final experiment manifest (WHI-1447)"
    )
    acceptance_p.add_argument(
        "--run", action="append", required=True, help="LABEL=RUN_DIR (a complete run)"
    )
    acceptance_p.add_argument(
        "--order-check", action="append", default=[], help="LABEL=RUN_LABEL_A,RUN_LABEL_B"
    )
    acceptance_p.add_argument("--report", action="append", default=[], help="Report dir")
    acceptance_p.add_argument("--bundles", default="docs/references/v1-acceptance/bundles.json")
    acceptance_p.add_argument("--output", required=True, help="Manifest path (write-once)")

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
    _strategies_argument(run_p)

    order_p = subparsers.add_parser(
        "order-check",
        help="Compare two complete runs' deterministic outputs (e.g. fixed vs reverse order)",
    )
    order_p.add_argument("run_a")
    order_p.add_argument("run_b")

    report_p = subparsers.add_parser(
        "report", help="Render an offline HTML/CSV report from saved run records"
    )
    report_p.add_argument("runs", nargs="+", help="Run directories (each its own cohort)")
    report_p.add_argument(
        "--output", default=None, help="Report directory (default data/reports/<run id>)"
    )
    report_p.add_argument(
        "--bundle",
        action="append",
        default=[],
        help="Frozen bundle for case labels; used only when it hashes to a run's bundle_hash "
        "(default: each run's own --bundle from its replay command)",
    )
    report_p.add_argument("--min-samples", type=int, default=30)
    report_p.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Report an interrupted/running run (unrecorded cases shown as missing)",
    )

    quote_p = subparsers.add_parser(
        "quote",
        help="Compare the profile's algorithms on one exploratory exact-input request "
        "(one solve attempt each)",
    )
    quote_p.add_argument("--bundle", required=True, help="Frozen corpus bundle (read only)")
    quote_p.add_argument("--profile", required=True, help="Source run profile (not modified)")
    quote_p.add_argument("--token-in", required=True, help="Symbol or token address")
    quote_p.add_argument("--token-out", required=True, help="Symbol or token address")
    quote_p.add_argument("--amount", required=True, help="Exact input in whole tokens, e.g. 1.5")
    quote_p.add_argument(
        "--details", action="store_true", help="Explain every final plan and its timings"
    )
    quote_p.add_argument("--quotes-dir", default="data/quotes")
    _strategies_argument(quote_p)

    return parser


def _strategies_argument(parser: argparse.ArgumentParser) -> None:
    from benchmark.strategies import DEFAULT_MODE, MODES

    parser.add_argument(
        "--strategies",
        choices=list(MODES),
        default=DEFAULT_MODE,
        help="all (default): the profile's base (and other listed) strategies, then the "
        "named optimized strategies uni_sor_adaptive / uni_sor_optimized, then the "
        "experimental metis_inspired (Metis-inspired, NOT Jupiter Metis; undeclared "
        "graph.label_hops/label_pruning/chunks from config/metis_challenge/m4.yaml), then "
        "the implemented 0.2.1 experimental identities (metis_history, "
        "direct_split_certified in its repository_grid mode, incremental_graph_repair, each "
        "with its pinned preset); base / optimized: one group; "
        "profile: the profile's exact algorithm "
        "selection "
        "(the replay path)",
    )


def _cmd_report(args: argparse.Namespace) -> int:
    import shlex

    from benchmark.results import load_manifest
    from report.aggregate import ReportInputError
    from report.render import render_report

    if args.min_samples < 1:
        print("report failed: --min-samples must be >= 1", file=sys.stderr)
        return 1
    command = ["uv", "run", "python", "main.py", "report", *args.runs]
    if args.output is not None:
        command += ["--output", args.output]
    for bundle in args.bundle:
        command += ["--bundle", bundle]
    command += ["--min-samples", str(args.min_samples)]
    if args.allow_incomplete:
        command.append("--allow-incomplete")
    try:
        from report.quote import load_quote_view

        root = Path(__file__).resolve().parent
        views = [
            load_quote_view(
                r, bundle_dirs=args.bundle, repo_root=root, allow_incomplete=args.allow_incomplete
            )
            for r in args.runs
        ]
        if any(v is not None for v in views):
            if not all(views):
                raise ReportInputError(
                    "exploratory single-request runs are reported on their own, not mixed "
                    "with corpus runs"
                )
            return _write_quote_reports([v for v in views if v is not None], args.output)
        manifests = [load_manifest(r, allow_incomplete=args.allow_incomplete) for r in args.runs]
        paths = render_report(
            manifests,
            args.output,
            bundle_dirs=args.bundle,
            min_samples=args.min_samples,
            report_command=shlex.join(command),
            repo_root=Path(__file__).resolve().parent,
        )
    except (ResultError, ReportInputError, OSError) as exc:
        print(f"report failed: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {paths.html}")
    for name, path in paths.csv.items():
        print(f"wrote {path} ({name})")
    return 0


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
        if args.corpus_command == "split":
            from snapshot.corpus import split_bundle

            full = load_bundle(args.bundle)
            validate_corpus_bundle(full)
            bundle = split_bundle(full, args.split, Path(args.output))
            validate_corpus_bundle(bundle)
            print(
                f"wrote {args.split} split {bundle.bundle_id!r}: {len(bundle.pools)} pool(s), "
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


def _cmd_costs(args: argparse.Namespace) -> int:
    import json

    from benchmark import costs
    from snapshot import cost_evidence as ce

    exports = Path(args.exports)
    try:
        config = ce.load_cost_config(args.config)
        if args.costs_command == "sql":
            sys.stdout.write(ce.generate_sql(config, args.name))
            return 0
        if args.costs_command == "ingest":
            export = ce.ingest(config, args.name, args.raw, exports, query_id=args.query_id)
            print(f"export {args.name}: {len(export.rows)} row(s), sha256={export.sha256}")
            return 0
        if args.costs_command == "report":
            model = costs.load_cost_model(args.model)
            print("\n".join(costs.validation_report(model)))
            return 0
        sample_set = ce.load_sample_set(exports, config)
        evidence_path = exports / ce.EVIDENCE_FILE
        if args.costs_command == "evidence":
            from snapshot.rpc import HttpJsonRpcTransport

            if evidence_path.exists() and not args.force:
                raise ce.CostEvidenceError(f"{evidence_path}: exists (pass --force to recapture)")
            transport = HttpJsonRpcTransport(config.rpc_url)
            evidence = ce.capture_fee_evidence(
                transport, config, sample_set, costs.receipt_selection(config, sample_set)
            )
            ce.check_fee_evidence(evidence, config)
            evidence_path.write_text(json.dumps(evidence, indent=1, sort_keys=True) + "\n")
            print(f"wrote {evidence_path} ({transport.call_count} HTTP request(s))")
            return 0
        evidence, evidence_sha = ce.load_fee_evidence(evidence_path)
        if evidence["samples_export_sha256"] != sample_set.samples.sha256:
            raise ce.CostEvidenceError(f"{evidence_path}: captured for a different export")
        rules, summary = ce.check_fee_evidence(evidence, config)
        provenance = {
            "config": {"path": config.source_path, "sha256": config.sha256},
            "exports": {
                e.name: {
                    "query_id": e.query_id,
                    "execution_id": e.execution_id,
                    "sql_sha256": e.sql_sha256,
                    "export_sha256": e.sha256,
                    "rows": len(e.rows),
                }
                for e in (sample_set.census, sample_set.samples)
            },
            "fee_evidence": {"path": str(evidence_path), "sha256": evidence_sha} | summary,
        }
        document = costs.fit_cost_model(
            config,
            sample_set,
            rules,
            provenance=provenance,
            validated_receipts=costs.receipts_per_cohort(
                summary["explained_tx_hashes"], sample_set
            ),
        )
        digest = costs.write_cost_model(document, args.model)
        model = costs.load_cost_model(args.model, expected_sha256=digest)
        print("\n".join(costs.validation_report(model)))
        print(f"wrote {args.model} sha256={digest}")
        return 0
    except (ce.CostEvidenceError, costs.CostModelError, OSError, KeyError) as exc:
        print(f"costs {args.costs_command} failed: {exc}", file=sys.stderr)
        return 1


def _cmd_calibrate(args: argparse.Namespace) -> int:
    import json

    from benchmark import calibration as cal

    try:
        if args.calibrate_command == "profiles":
            algorithms = args.algorithms.split(",") if args.algorithms else None
            profiles = cal.sweep_profiles(
                args.base, cal.parse_grid(args.grid), algorithms=algorithms, prefix=args.prefix
            )
            out = Path(args.output)
            out.mkdir(parents=True, exist_ok=True)
            for name, text in profiles.items():
                (out / name).write_text(text, encoding="utf-8")
                print(f"wrote {out / name}")
            return 0
        summaries = cal.summarize_runs(args.runs)
    except (cal.CalibrationError, ResultError, OSError) as exc:
        print(f"calibrate {args.calibrate_command} failed: {exc}", file=sys.stderr)
        return 1
    print("\n".join(cal.summary_table(summaries)))
    if args.json:
        Path(args.json).write_text(
            json.dumps([s.to_dict() for s in summaries], indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {args.json}")
    return 0


def _cmd_acceptance(args: argparse.Namespace) -> int:
    from benchmark import acceptance as acc

    try:
        document = acc.compose_manifest(
            acc.parse_labeled(args.run, what="--run"),
            order_checks=acc.parse_labeled(args.order_check, what="--order-check"),
            reports=args.report,
            bundles_record=args.bundles,
        )
        digest = acc.write_manifest(document, args.output)
    except (acc.AcceptanceError, ResultError, OSError, KeyError) as exc:
        print(f"acceptance failed: {exc}", file=sys.stderr)
        return 1
    for check in document["order_checks"]:
        print(
            f"order check {check['label']} {check['runs']}: {check['mismatches']} mismatch(es) "
            f"over {check['compared_records']} record(s)"
        )
    print(f"wrote {args.output} sha256={digest}")
    return 1 if any(c["mismatches"] for c in document["order_checks"]) else 0


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
    import yaml

    from benchmark.profile import parse_profile
    from benchmark.results import PROFILE_FILE, new_run_id
    from benchmark.strategies import DERIVATION_NOTE, announce, derive
    from snapshot.bundle import sha256_file

    try:
        bundle = load_bundle(args.bundle)
        if args.strategies == "profile":
            profile = load_profile(args.profile)
            effective = None
        else:  # validated in full before anything is written or any worker starts
            effective, profile = derive(
                read_profile_document(args.profile),
                args.strategies,
                source_path=args.profile,
                source_sha256=sha256_file(Path(args.profile)),
            )
    except (BundleError, ProfileError, OSError) as exc:
        print(f"run failed: {exc}", file=sys.stderr)
        return 1

    run_id = new_run_id()
    profile_path = args.profile
    profile_text = None
    if effective is not None:
        # The effective profile is saved into the run directory and is what the run (and
        # its replay, with --strategies profile) reads; the source profile is untouched.
        saved = Path(args.results_dir) / run_id / PROFILE_FILE
        profile_text = (
            f"# Effective run profile written by `main.py run --strategies {args.strategies}` "
            f"(WHI-1528).\n# Source profile: {args.profile} (sha256 "
            f"{profile.selection['source_profile']['sha256']}); the source is unchanged.\n"
            f"# {DERIVATION_NOTE}\n" + yaml.safe_dump(effective, sort_keys=False)
        )
        try:
            profile = parse_profile(yaml.safe_load(profile_text), str(saved))  # as read back
        except ProfileError as exc:  # pragma: no cover - the same, already validated document
            print(f"run failed: {exc}", file=sys.stderr)
            return 1
        profile_path = str(saved)
    print(announce(profile, args.strategies))

    replay_command = (
        f"uv run python main.py run --bundle {args.bundle} --profile {profile_path} "
        f"--results-dir {args.results_dir}"
    )
    if args.order is not None:
        replay_command += f" --order {args.order}"
    replay_command += " --strategies profile"

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
            run_id=run_id,
            profile_text=profile_text,
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


def _write_quote_reports(views: list[Any], output: str | None) -> int:
    """Single-request runs get the single-case text report (report.quote), never the corpus
    report's distribution statistics."""
    from report.quote import render_compact, render_details

    for view in views:
        out = Path(output) if output else Path("data/reports") / view.manifest.run_id
        if output and len(views) > 1:
            out = out / view.manifest.run_id
        out.mkdir(parents=True, exist_ok=True)
        path = out / "single_request.txt"
        path.write_text(render_compact(view) + render_details(view), encoding="utf-8")
        print(f"wrote {path}")
    return 0


def _cmd_quote(args: argparse.Namespace) -> int:
    from benchmark.quote import QuoteError, prepare_quote, run_quote
    from report.quote import load_quote_view, render_compact, render_details
    from snapshot.request import RequestError

    try:
        prepared = prepare_quote(
            bundle=args.bundle,
            profile=args.profile,
            token_in=args.token_in,
            token_out=args.token_out,
            amount=args.amount,
            strategies=args.strategies,
        )
    except (BundleError, ProfileError, QuoteError, RequestError, OSError) as exc:
        print(f"quote failed: {exc}", file=sys.stderr)
        return 1

    def _terminate(signum: int, frame: object) -> None:
        raise KeyboardInterrupt(f"signal {signum}")

    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        result = run_quote(prepared, args.quotes_dir)
    except RunInterrupted as exc:
        print(f"quote run INTERRUPTED; partial records in {exc.manifest.run_dir}", file=sys.stderr)
        return 130
    except (BundleError, ProfileError, RequestError, ResultError, OSError) as exc:
        print(f"quote failed: {exc}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
    view = load_quote_view(result.manifest.run_dir)
    assert view is not None  # a quote run's bundle is always a request bundle
    sys.stdout.write(render_compact(view))
    if args.details:
        sys.stdout.write(render_details(view))
    print()
    print(f"saved: {result.quote_dir} (run {result.manifest.run_dir})")
    print(f"replay: {result.manifest.replay_command}")
    print(f"report: uv run python main.py report {result.manifest.run_dir}")
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
    if args.command == "costs":
        return _cmd_costs(args)
    if args.command == "acceptance":
        return _cmd_acceptance(args)
    if args.command == "calibrate":
        return _cmd_calibrate(args)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "order-check":
        return _cmd_order_check(args)
    if args.command == "report":
        return _cmd_report(args)
    if args.command == "quote":
        return _cmd_quote(args)
    raise AssertionError(f"unreachable: unknown command {args.command!r}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())

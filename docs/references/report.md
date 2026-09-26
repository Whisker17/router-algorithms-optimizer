# Offline report (WHI-1446 / I20)

`main.py report` turns saved run records into a self-contained `report.html` plus CSV
summaries (docs/DESIGN.md §2.11, §4.3 `render_report(run_manifest) -> ReportPaths`).

```bash
# one run (output defaults to data/reports/<run id>)
uv run python main.py report data/results/<run id>
# matched V2/V3 cohort run and full-coverage run side by side (never pooled)
uv run python main.py report data/results/<cohort run> data/results/<full run> \
  --bundle data/corpus/mantle-5src-101082044/sor_cohort \
  --bundle data/corpus/mantle-5src-101082044/bundle --output data/reports/smoke
```

Options: `--min-samples N` (default 30; smaller paired samples are marked
underpowered), `--allow-incomplete` (report an interrupted/running run; unrecorded
cases count as `missing`).

## Inputs

- Only the schema-2 run directory (`manifest.json`, `cases.jsonl`, `memory.jsonl`) is
  required; no RPC, Dune or live chain data is read.
- Case labels (token symbols, amount stratum, tuning/report split, cohort) come from the
  frozen bundle the run names — the run's own `--bundle` from its replay command, or a
  `--bundle` given here — and are used **only** when that bundle's `manifest.json` hashes
  to the run's `bundle_hash` and every file read matches its checksum. Otherwise the
  report still renders with raw addresses and an `unlabeled` stratum, and says so.

## Comparison rules

| Rule | Where |
| --- | --- |
| Status counts over the **full schedule**; timeouts split by `limit_hit` (`quotes`/`time` = runner budget cut-off, `solver_budget` = solver's own stop); last valid candidates counted apart | *Coverage and status* |
| Quality = per-case relative improvement in bps on **common-success** cases; raw amounts never averaged | *Gross quality vs direct*, *Pairwise* |
| No direct baseline (direct failed) or direct output 0 → **N/A**, counted, never zero | same |
| Like-for-like vs capability split by plan topology (`single_route`/`disjoint_split`/`shared_pool`) and multi-hop vs the baseline's declared capabilities | *Pairwise* detail |
| A protocol-restricted algorithm (SOR, V2/V3) compared in a non-matched universe is flagged ⚑ *coverage* | *Pairwise* |
| Each run is its own cohort section: matched V2/V3 (`corpus.cohort: sor_compatible` or SOR `coverage_mode: matched_cohort`) vs full five-source coverage | section headers |
| Net = `evaluation.estimated_net_output` and `evaluation.cost` (`low_out_raw`/`high_out_raw` scenarios), never the ranking sentinel `score`; unranked plans listed with their cost status; scenario and gross-vs-net-order rank reversals counted; gross and net magnitudes never compared | *Estimated net output* |
| Gross-only runs show no net ranking; synthetic fixed cost is bannered SYNTHETIC | same |
| Quality vs time is a Pareto table + inline SVG over cases every algorithm solved (shortfall vs best known among the run's algorithms); no weighted score | *Pareto view* |
| Latency (median of repeats), quote counts, memory (separate pass only), prepare/evaluation totals | *Latency* |
| Representative traces: median gain vs direct among improved non-boundary cases, plus one example per failure status; every non-ok outcome grouped with its full error text | *Traces*, *All non-ok outcomes* |
| Evaluation scope (WHI-1447): **held-out** only when every case is a declared `report`-split case (e.g. a `main.py corpus split --split report` bundle); tuning-only, mixed or unlabeled runs are bannered EXPLORATORY | run header |
| Source coverage (WHI-1447): per algorithm, `ok` plans with a step through each admitted source, next to the bundle's pools per source (from the hash-verified `pools.json`) | *Source coverage of solved plans* |
| Replay command, bundle/profile/cost-model/price hashes, experiment id, git revision + dirty diff hash, environment, SOR source pin/scope/adaptations/deviations | *Provenance*, *Source pin* |

`uni_sor_port` is labeled a **scoped routing-core port** of the pinned
`@uniswap/smart-order-router`, not the full upstream product. The `metis_inspired`
label is reserved (0.2.0); any algorithm whose name contains `metis` is labeled an
experimental Metis-inspired variant, never Jupiter Metis.

## Output safety

The HTML has no script, stylesheet link or external resource; a Content-Security-Policy
(`default-src 'none'`) forbids scripts outright and long lists use `<details>`, so the
report is fully usable with JavaScript disabled. Every value is HTML-escaped. CSV cells
of external text that a spreadsheet would read as a formula are prefixed with `'`.
Output is deterministic (no timestamps) for identical inputs.

## CSV files

`status_counts`, `paired_gross`, `vs_direct_by_stratum`, `vs_direct_by_pair`,
`latency`, `pareto`, `source_coverage`, `cases` (one row per scheduled case ×
algorithm), and — when the run has net scores — `net_coverage`, `paired_net`. Every row carries `run_id` and
`cohort`.

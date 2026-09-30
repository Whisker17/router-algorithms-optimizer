# Single-request comparison (WHI-1498)

Compare the routing strategies on **one** exact-input request against a frozen snapshot,
with exactly one solve attempt per strategy. By default (`--strategies all`, WHI-1528,
WHI-1540 and Release 0.2.1) these are fourteen for a six-algorithm profile:

1. the profile's six base algorithms;
2. the two named optimized strategies `uni_sor_adaptive` and `uni_sor_optimized`;
3. the experimental Metis-inspired (NOT Jupiter Metis) `metis_inspired`;
4. the five 0.2.1 experimental identities `metis_history`, `direct_split_certified`,
   `incremental_graph_repair`, `uni_sor_cycle_safe` and `cfmm_dual`, each with its pinned
   current preset.

`--strategies base|optimized` runs one group, and `--strategies profile` runs the profile's exact
selection ([`strategy-groups.md`](strategy-groups.md)).

The checked-in 19-pool real-state fixture (block 101082044) runs offline in any checkout:

```bash
uv run python main.py quote \
  --bundle tests/fixtures/corpus/bundle \
  --profile config/daily_gross.yaml \
  --token-in USDC --token-out USDT0 --amount 10000 --details
```

Its 14 rows are the walkthrough of [`routing-algorithms.md`](routing-algorithms.md) §11. With the
frozen five-source corpus prepared locally (the primary clone's gitignored
`data/corpus/mantle-5src-101082044/`), the same command runs against the whole corpus bundle:

```bash
uv run python main.py quote \
  --bundle data/corpus/mantle-5src-101082044/bundle \
  --profile config/daily_gross.yaml \
  --token-in USDC --token-out USDT --amount 10000 --details
```

Without `--details` it prints one row per selected algorithm (failures included), under
*Base strategies* / *Optimized strategies* / *Experimental and other strategies* headings
when groups were selected:
status, evaluated gross output in human and raw units, gain versus a valid nonzero
`direct` baseline (otherwise `N/A`, with the reason), solve latency and counted quotes. A
net column appears only for an objective that produces a net output. `--details` adds,
for every algorithm, the final plan as the independent evaluation replayed it — ordered
swaps with source, pool id, tokens and addresses, exact per-step input/output, the fund
ledger (each fund's allocation to later steps with explicit denominators, the step that
drains the remaining balance and so takes any integer remainder, merges, reused pools,
terminal outputs, residuals) and a reconciliation check — plus this execution's
preparation, worker start-up, solve and final-evaluation durations, counters and limit
hit. `--details` changes presentation only; it adds no solver run or measurement pass.
For the 0.2.1 rows it also prints the research diagnostics block (§ *Research diagnostics and
work units* below).

With `config/daily_gross.yaml`, `metis_inspired` uses the source's `chunks: 200`, budget
and `search.*` values plus `label_hops: 4` and `label_pruning: true` from
`config/metis_challenge/m4.yaml` (the source declares neither). Its label search can
therefore reach 4 hops while the others search at most 2. The header prints these recorded
settings next to the shared `search.max_hops` instead of claiming one identical search. This
is not the frozen WHI-1449 M4 arm, whose 3-hop search and 900 s / 300,000-quote budget
are not used here ([`strategy-groups.md`](strategy-groups.md)).

The five 0.2.1 identities receive their pinned presets (`metis_history/1`,
`direct_split_certified/1` in its `repository_grid` mode, `incremental_graph_repair/1`,
`uni_sor_cycle_safe/1` and the current CL-stage `cfmm_dual/2`), recorded per row with the preset's
path, sha256, key, version and `settings_sha256`. On the fixture request above,
`direct_split_certified` is a visible `unsupported` row: the pair's direct pools include
concentrated and Liquidity Book pools.

This is an **exploratory** request, not a held-out corpus result.

## What runs

`benchmark/quote.py` validates everything before writing anything or starting a solver:
the bundle and source profile load, the `--strategies` selection derives a valid effective
profile (an incompatible `search` grid or an empty selection is refused), the objective is
supported, both tokens resolve and the amount converts exactly. It then writes

```
data/quotes/<quote id>/
  quote.json      inputs, resolved tokens and raw amount, parent bundle and source-profile
                  identity (sha256), effective-profile identity, replay command, the
                  strategy mode and groups
  profile.yaml    the effective profile: the source's (or the --strategies derivation's)
                  algorithms and settings with warmup 0, repeats 1, memory_pass false
  bundle/         the derived single-case bundle
  runs/<run id>/  manifest.json + cases.jsonl from the ordinary runner
```

and runs the unchanged `benchmark.runner.run_experiment`: algorithms sequentially in
isolated spawned workers, the profile's hard time/quote limits, independent evaluation of
each final plan, failures and separately labelled last valid candidates kept. Preparation
and final evaluation are stages of this one execution, not extra solves. No warmup,
repeat, retry or memory pass runs, whatever the source profile declares; the source YAML
is not modified and batch `run` behaviour is unchanged.

Each algorithm's one attempt has four recorded stages:

1. **Preparation** in its worker: the factory's `prepare` (graph indexes; `metis_history`'s
   certified-edge set; `cfmm_dual`'s concentrated-liquidity indexes, built once here and charged
   to preparation, never to a free precomputation).
2. **Worker start-up.**
3. **Solve**: the whole search, including incumbents, bounds, repairs, numerical recovery and
   every internal validation replay (`internal_evaluations`), on one quote meter and wall
   clock.
4. **Final independent evaluation** of the returned plan by the runner, outside the solve.

## Inputs

- **Tokens**: a symbol from the bundle's frozen token universe (`prices.json`,
  case-insensitive) or an explicit address. An unknown or unsupported token, a symbol
  shared by several tokens (pass the address), or the same token on both sides is refused.
  Native MNT wrapping is out of scope: use WMNT explicitly.
- **Amount**: a plain positive decimal in whole tokens (`10000`, `1.5`). It converts to
  integer base units exactly; more fractional digits than the token's decimals is refused
  unless they are zeros. No sign, exponent, separators, NaN or infinity. Money is never
  rounded.
- **Profile**: `gross_only` (or `synthetic_fixed_cost`). `empirical_cost` profiles such as
  `config/daily.yaml` are refused (next section).

## Scope and limits

- **Frozen block.** Every quote reads the parent bundle's state at its one block
  (`mantle-5src-101082044`: block 101082044). There is no live quote, RPC or Dune access
  during a run.
- **Token universe.** Only the parent corpus's eight tokens and its admitted pools of the
  five sources. The header lists the pool count per source.
- **Derived bundle.** The request becomes a plain (non-corpus) single-case bundle: the
  parent's pool records at the parent's block, the one case, and a provenance record naming
  the parent (bundle id, `bundle_hash`, checksum table), the request and the token metadata.
  The corpus descriptor's labels (origin, stratum, split) cannot describe a custom request,
  so none are borrowed. The bundle id carries `exploratory`, and its content is
  deterministic: the same request over the same parent gives the same `bundle_hash`. The
  original bundle is only read.
- **Collected-state envelope.** The corpus collected concentrated-liquidity ticks and
  Liquidity Book bins for amounts up to a per-token envelope. A request beyond it is run
  but flagged. An `incomplete_snapshot` outcome then means uncollected state, **not**
  missing liquidity.
- **SOR scope.** `uni_sor_port` is a scoped Uniswap SOR V2/V3 port. It never sees
  Liquidity Book pools (`uni-sor-port-contract.md` D-4), so its coverage is narrower than
  the other algorithms' on the same bundle.
- **Cost applicability.** Gross output is always comparable. The derived bundle carries
  no price context (the loader admits one only with a corpus descriptor). Under
  `empirical_cost` a missing price context makes the objective rank plans by unranked gross,
  so the comparison would silently differ from the same objective on the corpus. `quote`
  therefore refuses that objective instead of reporting misleading net figures. Even on the
  corpus, split and shared-pool plans outside the calibrated cohorts have no reliable net
  score (`cost-model.md` §4, §6).
- **One observed latency.** Each algorithm has one solve-latency observation from this
  execution. It is not a stable performance distribution or a production latency claim,
  and no sample list or distribution statistic is shown. No memory figure is measured or
  shown. Trace rendering happens outside the timed solve, and no per-hop latency is derived.

## Research diagnostics and work units

The 0.2.1 rows carry an `r021` record that the runner checks against its own run identity,
request and evaluated score (never the solver's claims). The compact output prints one line per
identity: `certified` (with lower, upper and gap), `estimate … (not a bound; residual …,
tolerance …)`, `unknown`, or `unavailable (not_produced)`. `--details` adds:

- the domain hash and its amount-grid kind;
- the `Budget.max_candidates` unit of that identity;
- the named work units with their categories (quote, search, numeric, validation, memory);
- fallback and repair outcomes and the scope (`supported` or the `unsupported` reason);
- observed per-stage seconds such as `prepare_cl_indexes`, `initial_solve` and `recovery`.

Units of different strategies are never divided by each other; `quotes_executed` is the only
cross-strategy work unit. A `cfmm_dual` estimate is a numerical dual value, not an upper bound.

## Replay and report

`quote.json` and the run manifest record the exact replay command (`replay_command`), which
is a plain `main.py run --strategies profile` over the derived bundle and effective profile.
The quote also prints it on its `replay:` line. Copy it from there rather than reconstructing
paths:

```bash
uv run python main.py run --bundle data/quotes/<quote id>/bundle \
  --profile data/quotes/<quote id>/profile.yaml \
  --results-dir data/quotes/<quote id>/runs --strategies profile
```

It reruns exactly the saved algorithms, preset identities and recipe/graph settings, never a
re-expansion: a quote saved before WHI-1540 replays its eight algorithms, without
`metis_inspired`, and one saved before 0.2.1 replays its nine without any 0.2.1 identity. The
replay is a new solve with its own timings; its statuses, plans and scores should match, and
`main.py order-check <original run> <replay run>` compares the deterministic outputs. The saved
run also renders offline without credentials:

```bash
uv run python main.py report data/quotes/<quote id>/runs/<run id>
```

For such a run, `report` writes `single_request.txt` (the compact table and every plan's
details) instead of the corpus HTML/CSV report and its distribution tables. It refuses to
mix an exploratory run with corpus runs.

## Batch runs and explicit comparison profiles

A batch `run` uses the same `--strategies` modes over every case of a bundle. Bounded offline
examples on checked-in fixtures:

```bash
# all 14 strategies of the ordinary roster over the 4 cases of the mantle_mixed fixture
uv run python main.py run --bundle tests/fixtures/routing/mantle_mixed \
  --profile config/daily_gross.yaml --results-dir <results dir>

# an explicit comparison profile, run literally (here the current cfmm_dual/2 CL stage
# next to path_split and incremental_graph)
uv run python main.py run --bundle tests/fixtures/routing/mantle_mixed \
  --profile config/cfmm_dual/cl.yaml --results-dir <results dir> --strategies profile
```

Under `all`, `run` saves the effective profile as `<run dir>/profile.yaml`; under `profile`, the
manifest names the source profile instead. In both cases the manifest's `replay_command` is the
literal replay. The explicit profiles for the 0.2.1 controls, ablations and stress domains are
listed in [`routing-algorithms.md`](routing-algorithms.md) §11.4 (for example
`config/cfmm_dual/cpmm.yaml`, the historical CPMM-only `cfmm_dual/1` ablation, and
`config/direct_split_certified/raw_stress.yaml`, the `raw_integer` stress domain). They are
smoke-scale comparison profiles, not a performance campaign.

## References and algorithm guides

For first-principles explanations, mathematical derivations, pseudocode, and reproducible
worked examples of the six base routing algorithms compared here, see
[`routing-algorithms.md`](routing-algorithms.md) §§2–7. The same guide explains the two
optimized strategies, `uni_sor_adaptive` and `uni_sor_optimized`, in §§8–9. Their grouping,
selection and registered recipes are described in [`strategy-groups.md`](strategy-groups.md).
`metis_inspired`'s label search, with hand-worked examples, is in §10 of the same guide. Its
contract is [`jupiter-metis-challenge.md`](jupiter-metis-challenge.md) §9.2, and its frozen
WHI-1449 results are in [`metis-challenge-results.md`](metis-challenge-results.md).
The five 0.2.1 identities are explained in §§14–18 of the guide (`metis_history`,
`direct_split_certified`, `incremental_graph_repair`, `uni_sor_cycle_safe`, `cfmm_dual`), with
their shared vocabulary in §1.8; their research contracts are in
[`research-021/`](research-021/contract.md).


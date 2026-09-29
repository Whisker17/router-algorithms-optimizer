# Single-request comparison (WHI-1498)

Compare the routing strategies on **one** exact-input request against a frozen snapshot,
with exactly one solve attempt per strategy. By default (`--strategies all`, WHI-1528) these
are the profile's six base algorithms followed by the two named optimized strategies
`uni_sor_adaptive` and `uni_sor_optimized`. `--strategies base|optimized` runs one group,
and `--strategies profile` runs the profile's exact selection
([`strategy-groups.md`](strategy-groups.md)):

```bash
uv run python main.py quote \
  --bundle data/corpus/mantle-5src-101082044/bundle \
  --profile config/daily_gross.yaml \
  --token-in USDC --token-out USDT --amount 10000 --details
```

Without `--details` it prints one row per selected algorithm (failures included), under
*Base strategies* / *Optimized strategies* headings when groups were selected:
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

## Replay and report

`quote.json` and the run manifest record the exact replay command, which is a plain
`main.py run --strategies profile` over the derived bundle and effective profile. It
reruns exactly the saved algorithms and recipe settings, never a re-expansion. The saved run also renders
offline without credentials:

```bash
uv run python main.py report data/quotes/<quote id>/runs/<run id>
```

For such a run, `report` writes `single_request.txt` (the compact table and every plan's
details) instead of the corpus HTML/CSV report and its distribution tables. It refuses to
mix an exploratory run with corpus runs.

## References and algorithm guides

For first-principles explanations, mathematical derivations, pseudocode, and reproducible
worked examples of the six base routing algorithms compared here, see
[`routing-algorithms.md`](routing-algorithms.md) §§2–7. The same guide explains the two
optimized strategies, `uni_sor_adaptive` and `uni_sor_optimized`, in §§8–9. Their grouping,
selection and registered recipes are described in [`strategy-groups.md`](strategy-groups.md).


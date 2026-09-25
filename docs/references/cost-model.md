# Empirical execution-cost model `mantle-101082044-cost-v1` (WHI-1445 / I19)

Status: **published.** A frozen, holdout-validated model of the whole-transaction native fee a
complete route plan costs to execute on Mantle, for the corpus snapshot 101082044
(`docs/references/corpus.md`). It is a separate immutable artifact, not part of the corpus bundle:
a run under it is a new experiment identity (bundle hash + objective + price-context hash +
cost-model hash); the bundle is never rewritten.

| Item | Value |
| --- | --- |
| Artifact | `config/costs/mantle-101082044-cost-v1.json`, sha256 `50e89727d6b6ac869c14c837c7cdec8099efc897d201794640912f3f6b4b7e71` |
| Definition | `config/cost_calibration.yaml` (window, sampling seed, split, thresholds) |
| Code | `snapshot/cost_evidence.py` (SQL, exports, fee rules, receipt evidence), `benchmark/costs.py` (fit, artifact, applicability), `benchmark/objective.py` (`empirical_cost`) |
| Window | [2026-09-18 00:00, 2026-09-25 00:00) UTC, which ends at the snapshot block's timestamp; after Mantle's Arsia fee model (2026-04-22) |
| Population | 25,427 swap transactions (44,335 `dex.trades` legs) of the five labels; 0 duplicated leg rows, 0 missing transaction rows |
| Sample | 3,151 transactions: a seeded uniform 1/8 sample (`xxhash64('WHI-1445/cost/v1|' ‖ tx hash) & 7 = 0`) |
| Split | 70/30 by sample key (`int(key, 16) % 10 < 3` ⇒ holdout; the key bits are independent of the sampling bits) |
| Profile | `config/corpus_empirical_cost_smoke.yaml` (`objective.mode: empirical_cost`, path + sha256 pinned) |

## 1. What the fee is on Mantle, verified

Mantle's post-Arsia fee model (the [Fee Model Handbook (After Arsia)](https://docs.mantle.xyz/network/system-information/fee-mechanism/fee-model-handbook-after-arsia.md),
fetched 2026-09-25) charges one total per transaction:

```
TotalFee = gasUsed * effectiveGasPrice            # L2 execution
         + l1Fee                                  # L1 data fee: a separate charge, not L2 gas
         + operatorFeeConstant + operatorFeeScalar * 100 * gasUsed   # operator fee
```

This was checked against the chain before it was used, not assumed:

- **Receipts** (12 sampled transactions, `tests/fixtures/costs/fee_evidence.json`): Dune's
  `mantle.transactions` `gas_used`, `gas_price` and `l1_fee` equal the receipt's `gasUsed`,
  `effectiveGasPrice` and `l1Fee`, and the receipt's `operatorFeeScalar`/`operatorFeeConstant` are
  1e8 / 0 (= 10 gwei per gas).
- **Sender balances** (`eth_getBalance` at the block before and the block of each transaction):
  for 10 of the 12, where the sender had only one transaction in that block, the balance drop
  minus `value` **exactly** equals the formula. For the same transactions it does **not** equal
  `gas_used * gas_price`, it does not equal Dune `gas.fees.tx_fee_raw`, and it does not equal
  `tx_fee_raw + l1_fee`.
- **Dune `gas.fees`**: `tx_fee_raw == gas_used * gas_price + l1_fee` on all 3,151 sampled
  transactions. This omits the operator fee, so it is short by exactly that amount on 12/12
  receipts. It already contains `l1_fee`, so adding `l1_fee` again would double count it. The
  model therefore uses the three components from `mantle.transactions` and never `gas.fees`.
- **Parameters over the window** (8 evenly spaced `L1Block` reads, window start → snapshot): the
  operator-fee parameters are constant, and the L2 base fee is 50 gwei at every checkpoint and in
  every receipt block.

`snapshot.cost_evidence.FeeRules.total_fee_wei` is the one place these components are combined.
It is charged **once per transaction/plan**. The export groups `dex.trades` legs by transaction
hash *before* joining the fee row. `tx_samples` refuses a repeated transaction hash, which is what
a per-leg export would contain, and refuses duplicated leg rows. Pool swap fees and price impact
are not part of this cost: they are already inside the simulator's gross output.

## 2. Dune evidence (bounded, cached)

Both queries filter `blockchain = 'mantle'`, the `block_month` / `block_date` partitions and the
absolute window. The SQL is generated from the config (`main.py costs sql --name …`). The checked-in
SQL files are byte-identical to the generated SQL, and each export records the SHA-256 of that SQL.
The medium engine was used; the canonical runs cost 0.534 credits in total. The raw results are
cached in the primary clone under `data/cache/dune/whi-1445/`.

| Query | Dune query / execution | Rows | Export (`tests/fixtures/costs/`) sha256 | Credits |
| --- | --- | ---: | --- | ---: |
| Q1 census | [8835037](https://dune.com/queries/8835037) / `01M3CEQQ3HRNAV0NP20528YXKG` | 642 | `q1_census.jsonl` `2e8789bb…e862` | 0.041 |
| Q2 samples | [8835020](https://dune.com/queries/8835020) / `01M3CET9A2TG1K632150KHY6XW` | 3,151 | `q2_samples.jsonl.gz` `de91abed…0389` (uncompressed canonical text) | 0.493 |

Exploratory, non-canonical queries were also run, and nothing derives from them:

- 8834922: fee fields of 8 transactions (1.641 credits).
- 8834949: window sizing.
- Two failed single-query attempts that were "too many stages". Their fix was to split the
  work into Q1/Q2 and drop a per-transaction log count.

The sample export contains:

- tx hash, block, `to` (the router), success;
- per-family leg counts and distinct pools;
- tokens sold/bought/touched, source/sink token counts;
- `gas_used`, effective `gas_price` and `l1_fee` (exact integers);
- `gas.fees` `tx_fee_raw`, and whether `gas.fees` agrees on gas/price.

No sender address is exported.

**Observed limits of the source.**

- **Only successful transactions.** A reverted transaction emits no `Swap` event, so it has no
  `dex.trades` row. The model therefore prices successful executions only.
- **LB legs are bins, not calls.** Merchant Moe LB emits one `Swap` event per bin crossed. For LB,
  the number of `dex.trades` rows per pool is the bin count (used only as an applicability bound),
  and the pool-call count is the number of distinct pools.
- **Other labels.** No leg of another project appears in the window.

## 3. The model (smallest interpretable, per-shape medians)

**Cohort.** A transaction's cohort is `c<k>.l<m>.p<n>.<topology>`: the number of distinct
concentrated, LB and constant-product pools, plus whether they form one chain
(`pools == tokens − 1`, one source and one sink token) or a parallel/split graph. Transactions
are excluded when they:

- are cyclic or multi-in/out (sources ≠ 1 or sinks ≠ 1, i.e. arbitrage cycles and batches; 367
  transactions);
- repeat a CL/CP pool;
- touch another protocol.

**Evaluated plans.** The evaluator's `route_features` map a plan to a cohort:

- `pool_calls_<family>` gives the family counts;
- `split_funds` or `merge_steps` greater than 0 means `parallel`;
- `repeated_pool_calls > 0`, a shared-pool graph, means there is **no** cohort.

**Fit and prediction.** Each cohort stores its training medians (`median_low`) of `gas_used` and
`l1_fee`. One reference effective gas price P_ref prices all cohorts. It is the training median,
60.600121200 gwei; the base fee was 50 gwei, and the rest is the typical priority bid in the
window.

```
nominal_wei(cohort) = gas_med * (P_ref + 10 gwei operator/gas) + l1_med + 0
```

**Holdout error.** Each holdout transaction is priced at P_ref with its *own* gas and L1 fee. Its
relative error against the cohort nominal therefore measures the shape model (the spread of
router implementations and bins), not the sender's price bid. The as-paid ratio, which uses the
sender's own price, is reported next to it; its upper tail is far wider (priority bids reach
~4,000 gwei).

**Low/high scenarios.** Low = nominal × (1 + holdout 5th-percentile error) and high = nominal × (1 +
95th). These are empirical scenario bounds, **not confidence intervals**.

**Supported cohorts** need ≥ 30 training and ≥ 10 holdout transactions, and ≥ 20 training
transactions for each pool family involved. "Receipts" in the table is the number of that
cohort's transactions whose sender balance drop was independently reproduced exactly (§1).

| Cohort | Train | Holdout | gas med | Nominal MNT | err median | err q05..q95 | mean \|err\| | as-paid q05..q95 | Receipts |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- | ---: |
| `c1.l0.p0.chain` (1 CL pool) | 1,412 | 590 | 141,591 | 0.010309 | +0.056 | −0.103..+1.842 | 0.404 | 0.77..25.8 | 2 |
| `c0.l1.p0.chain` (1 LB pool) | 260 | 98 | 258,208 | 0.018541 | +0.012 | −0.197..+1.283 | 0.311 | 0.84..21.5 | 2 |
| `c2.l0.p0.chain` (CL→CL) | 111 | 53 | 372,314 | 0.026648 | +0.050 | −0.276..+0.727 | 0.332 | 0.65..2.00 | 2 |
| `c0.l0.p1.chain` (1 Classic pool) | 65 | 20 | 118,748 | 0.008749 | −0.046 | −0.111..+0.227 | 0.097 | 1.38..1.70 | 1 |
| `c1.l1.p0.chain` (CL→LB / LB→CL) | 40 | 22 | 517,302 | 0.037082 | +0.096 | −0.303..+1.607 | 0.444 | 0.79..5.37 | 2 |

The right tails, e.g. +184% for one CL pool, are real. The same one-pool swap costs several times
more gas through some routers and aggregators. The model does not attribute that overhead to
pools. It keeps the router mix per cohort (top-5 routers and the distinct-router count in the
artifact). Every other cohort is `low_confidence`: CL→Classic has 5/1, two LB pools 11/4, and every
`parallel` shape has at most 6 training transactions. Such cohorts keep an informational nominal,
but no net score.

`uv run python main.py costs report --model config/costs/mantle-101082044-cost-v1.json` prints
the full table. Loading the artifact **re-derives every nominal, every holdout statistic and
every scenario multiplier from its stored holdout rows** and refuses one that does not reproduce.

## 4. Applicability and net scores

`ObjectiveContext.empirical_cost(model).bind(bundle)` takes the bundle's frozen `price-context/1`
(`snapshot/prices.py`, unchanged) and its file hash. `routing.evaluator.evaluate` computes the
cost **once from the complete plan's `route_features`**, after replay. Per-step costs are never
summed. The native fee is converted to output-token raw units with
`raw_amount_for_usd(token_out, usd_value(WMNT, fee))`, which rounds up. `Evaluation.cost` records
one of four statuses:

| `cost.status` | When | Net score |
| --- | --- | --- |
| `supported` | supported cohort, LB bins ≤ the cohort's training max, prices known | `estimated_net_output = gross − nominal`; low/high in `cost` |
| `low_confidence` | the cohort exists but is below the thresholds, or the plan has more LB bins than the cohort's training range | none (an informational nominal only) |
| `unsupported` | no historical cohort: shared/reused pools, or a shape never seen | none |
| `unknown_price` | no price context, or a missing native/output price | none |

The gross output is always kept, so gross comparisons remain valid. A model calibrated for another
block is refused at bind.

**Selection policy** (every algorithm's final choice goes through `ObjectiveContext.score`):

- A net-rankable plan scores its estimated net output.
- Any other plan scores `gross − 2**257`. That is below every rankable score, and unrankable plans
  keep their gross order among themselves.

An algorithm therefore returns the best *rankable* complete plan among its own final candidates.
It falls back to the gross-best unrankable one, which is flagged, only when none of its finalists
is rankable. Reports must use `estimated_net_output` and `cost.status`, never the sentinel score.

`uni_sor_port` keeps upstream's zero gas scores (contract A-3) and selects on raw quotes. Its
returned plan is still net-evaluated and flagged like any other.

**Consequence at this model version.** Splits (`parallel`) and shared-pool graphs have no
validated cost. Under `empirical_cost`, split-capable algorithms therefore settle for rankable
single-route plans whenever one exists, and the gross they give up is visible against a gross-only
run with the same profile. This is the honest effect of the evidence, not a claim that splitting
is never worth it.

## 5. Reproduce and results

```bash
uv run python main.py costs sql --name census    # == tests/fixtures/costs/q1_census.sql
uv run python main.py costs sql --name samples   # == tests/fixtures/costs/q2_samples.sql
#   ...run each as a saved Dune query, save the result JSON, then:
uv run python main.py costs ingest --name census  --raw <saved.json> --query-id 8835037
uv run python main.py costs ingest --name samples --raw <saved.json> --query-id 8835020
uv run python main.py costs evidence             # public RPC: receipts, balances, L1Block (85 requests)
uv run python main.py costs fit --model config/costs/mantle-101082044-cost-v1.json   # offline
uv run pytest tests/benchmark/test_costs.py
uv run python main.py run --bundle tests/fixtures/corpus/bundle \
  --profile config/corpus_empirical_cost_smoke.yaml
```

`fit` reads only checked-in files. Refitting reproduces the artifact byte for byte, and the test
asserts this. `write_cost_model` refuses to replace an existing artifact with different bytes: a
new model needs a new path and a new `model_id`.

**Fixture smoke** (six algorithms, 96 cases, `config/corpus_empirical_cost_smoke.yaml`; run
`20260925T142837200797Z-9889c0d8` vs its gross-only twin `…142847887167Z-a702c15b`):

- Both runs: 552 `ok`, 24 `no_route`.
- Every returned plan is `supported`.
- Plans changed versus gross: `direct` 1, `single_path` 7, `direct_split` 15, `path_split` 22,
  `incremental_graph` 22, `uni_sor_port` 0.

**Full corpus** (`direct`, `single_path`, `direct_split`; 398 cases; runs
`20260925T143121143846Z-10726255` vs `…138782Z-972fcbe6`):

- `direct` has **34 strict fee-driven reversals**. For example, on USDC→USDT large-1 the gross-best
  pool yields 20,203,997 raw, but the concentrated pool, at 20,203,866, has the higher net
  (cost 6,986 raw).
- `single_path` changed 103 plans; 24 stay `low_confidence` (only CL→Classic routes).
- `direct_split` changed 117 plans; 4 stay `low_confidence`.

`tests/benchmark/test_costs.py` pins two fixture reversals. On `bnd-78c1b0-779ded-round_at`,
`direct`'s gross choice (LB) and net choice (CL) differ. On USDC→FBTC, the gross tie between CL→LB
and CL→CL is broken by cost.

## 6. Limits

- One seven-day window and one snapshot. P_ref is the window's median bid, not a forecast.
- Router overhead is included and averaged over the observed router mix. The model is not a
  per-router or intrinsic per-pool cost.
- Features not observed historically are not modeled: CL ticks crossed, per-transaction log count,
  calldata size, and reverted transactions.
- Parallel/split and shared-pool plans are unranked on net until evidence for them exists.

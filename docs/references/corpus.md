# Frozen five-source corpus `mantle-5src-101082044` (WHI-1436 / I11)

Status: **published.** One immutable real bundle fixes the liquidity universe, the request
distribution and the price context for the 0.1.0 comparison
(`data/corpus/mantle-5src-101082044/bundle`, gitignored; its SHA-256 manifest is checked in
as `tests/fixtures/corpus/full_bundle.json`). All five sources were collected **freshly at
one common block**; the per-source replay bundles of WHI-1429–WHI-1434 remain admission
evidence at the provisional catalog block 101057678 and are not part of this corpus.
The corpus is usable in **gross-only** mode now; a later experiment manifest binds its
bundle hash to a separate cost-model hash (WHI-1445) — this bundle is never rewritten.

| Item | Value |
| --- | --- |
| Bundle id / hash | `mantle-5src-101082044-091b0759` / `717c21f35d1f7f6a02f7076b40793eaba564148cc4d187392049e503fa4d3143` |
| Block | Mantle 5000, **101082044**, `0x091b0759c9d3031f30658cdfa8bf4cd5ed311ece986e3c91eb1eeb121b2b65c4`, timestamp 1790294400 (2026-09-25T00:00:00Z) |
| Pools | 143: Agni 45, FusionX 26, Uniswap v3 13, Moe Classic 14, Moe LB 45 — no exclusions |
| Cases | 398: 288 leg-derived activity-pair cases, 48 leg-derived no-direct-pool cases, 62 state-derived boundary cases |
| Splits | 96 `tuning`, 302 `report` (disjoint by construction) |
| Price context | Dune `prices.minute`, 8/8 tokens priced, all at the 23:59 UTC minute bucket |
| Definition | `config/corpus.yaml` (sha256 `f9dc82d2…c0c4`) |

## 1. One common block

Block 101082044 is the first Mantle block at/after 2026-09-25T00:00:00Z. When it was chosen
(2026-09-25T05:21Z) the public RPC's `finalized` head was 101090943, ~8,900 blocks later.
It is declared with its hash in `config/corpus.yaml`, and every collector was run with
`--block 101082044 --block-hash 0x091b…65c4`: the collector resolves the block by number,
refuses a different hash or a block above the finalized head, pins every state read to the
hash (EIP-1898, `requireCanonical`), and re-reads the header by number and hash before
publishing. Every pool record carries `read_at == (101082044, 0x091b…)`; the loader
rejects any other (`one bundle never mixes blocks`), and `assemble` refuses a source bundle
whose block differs from the corpus block. The historical window ends exactly at this
block's timestamp.

## 2. Dune evidence (bounded, absolute window)

All three queries filter `blockchain = 'mantle'`, the `block_month` partition
(`2026-09-01`) and the absolute window **[2026-09-11 00:00:00, 2026-09-25 00:00:00) UTC**
over exactly the five sources' `dex.trades` labels (`agni 3`, `fusionx 3`, `uniswap 3`,
`merchant_moe 1`, `merchant_moe 2.2` — labels rank activity only; pool identity always
comes from each collector's factory discovery at the block). The SQL is generated from the
config (`main.py corpus sql --step …`), the saved SQL files are byte-identical to the SQL
stored in Dune (checked at ingestion), and each canonical export records the SHA-256 of the
SQL it came from. Medium engine; total 0.914 credits.

| Query | Dune query / execution | Rows | Export (`tests/fixtures/corpus/dune/`) sha256 | Credits |
| --- | --- | ---: | --- | ---: |
| Q1 activity | [8832124](https://dune.com/queries/8832124) / `01M3BGP1M8V2TTJX143WBWGBVF` | 84 | `q1_activity.jsonl` `1c331cfe…d157` | 0.089 |
| Q2 strata samples | [8832129](https://dune.com/queries/8832129) / `01M3BGTCFHDNCV5Q8AKD37W1N3` | 336 | `q2_strata.jsonl` `3aefe160…f91e` | 0.446 |
| Q3 prices | [8832131](https://dune.com/queries/8832131) / `01M3BGWAD5MSW779Z7JZ6R9X4P` | 8 | `q3_prices.jsonl` `a2ec6767…f3a3` | 0.379 |

An exploratory, non-canonical query (8832108, 0.043 credits) preceded Q1; nothing derives
from it. The exports are small aggregates/samples (Q2 is 211 KB), so they are checked in
next to their SQL: the selection below is re-derivable offline.

## 3. Selection rule (deterministic, seed `WHI-1436/corpus/v1`)

- **Token identity is the lowercase address.** Symbols are never selected in SQL and are
  informational labels only; USDT (`0x201e…`) and USDT0 (`0x779d…`) are distinct tokens.
- **Activity pairs:** the top 12 unordered token pairs by distinct transactions over the
  five sources in the window (ties by address, ≥ 100 each). The **token universe** is exactly
  their 8 tokens: USDC, CATI, USDT, USDT0, WMNT, FBTC, mETH, WETH.

  | Pair | Distinct txs | Pair | Distinct txs |
  | --- | ---: | --- | ---: |
  | USDT0/WMNT | 12,184 | USDT0/FBTC | 2,315 |
  | mETH/WETH | 7,065 | USDC/USDT0 | 2,290 |
  | USDT0/mETH | 6,829 | USDT/USDT0 | 1,652 |
  | USDT/WMNT | 5,683 | USDC/USDT | 1,323 |
  | WMNT/WETH | 4,410 | USDT/mETH | 929 |
  | USDC/WMNT | 3,057 | CATI/WMNT | 924 |

- **Amount strata** per direction (sampling unit = one direction of an activity pair):
  legs ordered by raw input amount (ties: tx hash, log index); a leg of 0-based rank r of n
  is `low` iff 0.05n ≤ r < 0.40n, `medium` iff 0.40n ≤ r < 0.80n, `large` iff
  0.80n ≤ r < 0.99n (the extreme tails are left to the boundary cases). At most one leg per
  transaction enters a stratum's sample; 4 legs per stratum are taken in
  `xxhash64(seed | tx_hash | evt_index)` order. Every case records its leg (tx hash, log
  index, block, source label), the stratum's raw bounds and the leg's rank. 24 directions ×
  3 strata × 4 = **288 cases**.
- **Leg-derived, not user orders.** Every empirical case is `origin: leg_derived`: a
  historical swap leg's pair and raw input, not a reconstructed user order
  (`corpus.json` `origin.reconstructed_orders: false`). Endpoints were not reconstructed.
- **No-direct-pool pairs** are discovered at the block, not guessed: universe pairs with no
  admitted pool on any source, ranked by the smaller token's distinct-tx count — USDC/FBTC,
  FBTC/WETH, USDC/CATI, CATI/USDT0 (4 × 2 directions × 3 strata × 2 = **48 cases**). Their
  amounts are the input token's own legs (all counterparties, sampled like above, 2 per
  stratum). All are reachable multi-hop in both cohorts.
- **Boundary cases** (`origin: state_derived_boundary`, stratum `boundary`, always `report`),
  derived from the frozen state with the exact simulator for every activity direction:
  `dust` (1 raw unit, 24); `round_below`/`round_at` — one below / exactly the smallest
  input some direct pool turns into a positive output (9 + 9; skipped when that is ≤ 2 raw
  units); `liq_at`/`liq_above` — exactly / one above the largest full-fill input of the
  deepest direct pool whose liquidity is proven exhausted within the envelope (10 + 10;
  skipped when none is). The per-direction search record is `corpus.json` `boundary`.
  These are not empirical trades and must be reported apart from empirical strata.
- **Tuning vs report:** within every (token_in, token_out, stratum) cell the case with the
  smallest `sha256(seed | case_id)` is `tuning`, the rest `report` (activity cells: 1 of 4;
  no-direct cells: 1 of 2). Boundary cases are `report`. Declared in `corpus.json` `splits`.

## 4. Frozen price context (`snapshot/prices.py`, schema `price-context/1`)

`prices.json` records, per token address: symbol (informational), decimals, `status`
(`ok` | `missing`), the price as a decimal string in **USD per whole token**, the
observation timestamp, and the upstream feed (`origin`), plus the Dune query/execution,
export hash and selection rule. Selection: the latest `prices.minute` bucket [t, t+60 s)
with t+60 s ≤ the snapshot timestamp and t ≥ snapshot − 3600 s. A token without such a row
would be `missing` with null price and timestamp — **never zero**; conversions refuse a
missing price. The loader rejects a zero/negative price, an observation after the snapshot
or older than the bound, another block, and unknown keys. Native MNT is priced through WMNT
(a 1:1 wrap), recorded as `native.via_token`. No stablecoin is assumed pegged: each has its
own observation (USDC 0.999718, USDT 0.999436, USDT0 0.99903765…).

| Token | Address | Decimals | USD price | Feed |
| --- | --- | ---: | ---: | --- |
| USDC | `0x09bc…0df9` | 6 | 0.999718 | coinpaprika |
| CATI | `0x1bdd…edc0` | 18 | 0.06138980477081803 | dex.trades |
| USDT | `0x201e…56ae` | 6 | 0.999436 | coinpaprika |
| USDT0 | `0x779d…3736` | 6 | 0.9990376510332882 | dex.trades |
| WMNT | `0x78c1…4cb8` | 18 | 0.677247 | coinpaprika |
| FBTC | `0xc96d…c364` | 8 | 84212.65 | coinpaprika |
| mETH | `0xcda8…0bb0` | 18 | 2953.29 | coinpaprika |
| WETH | `0xdead…1111` | 18 | 2687.8 | coinpaprika |

## 5. Envelope and the single exclusion rule

**Envelope.** Every admitted pool must cover, in both directions, a swap of
E(token) = ⌈2 × (largest case notional) / price(token)⌉ raw units of its input token. The
largest case notional at the frozen prices is $8,027.79 (a large WETH→mETH leg), so
E ≈ $16,055.57 of each token (e.g. 16,064.63 USDT, 23,707.1 WMNT, 5.9735 WETH). The
multiplier leaves room for multi-hop intermediates whose pool price differs from the
reference price. Each generated prepare config lists these as the reference cases of every
universe pair, so each collector walked ticks/bins until the envelope swap fits **or real
exhaustion is proven**; `assemble` and `validate` re-quote every pool at the envelope in
both directions and fail on anything but `ok`, `insufficient_liquidity` (proven) or
`insufficient_output_amount` (dust). Every case amount is ≤ its input token's envelope.

**One rule, declared once** (`config/corpus.yaml` `exclusion_rule`, recorded in
`corpus.json` `exclusions` and each source's provenance): *a discovered pool is excluded iff
proving the envelope in one direction needs more than 8,192 bitmap words (CL) or 5,000
non-empty bins (LB) from the current tick/active bin.* 8,192 words reach MIN_TICK/MAX_TICK
for every tick spacing, so thinness alone can never exclude a CL pool, and a source left
without an admitted pool blocks publication rather than being dropped. The generated
configs carry no ad-hoc `excluded_fee_tiers`/`excluded_bin_steps`. **No pool was excluded.**
The three pools the admission-stage configs excluded by hand are now admitted, their whole
tick range walked where needed: Agni USDC/WMNT 0.01% `0x7b3a…6f85` (2,982 initialized
ticks), FusionX USDT/WMNT 0.01% `0x4a31…5f88` (7,349 ticks), Uniswap USDC/USDT 0.01%
`0x8cfe…9d2c` (walked −3,466…3,465, proven exhausted). Those admission configs keep their
exclusions only because the provisional-block fixtures are reproduced from them.

## 6. Collection at the block

`main.py corpus plan` generated the five prepare configs (`config/corpus/prepare/*.yaml`,
every one of the 28 universe pairs × every catalog fee tier / every bin step / the Classic
pair, the envelope cases, the rule). All five collectors ran in the background against the
public RPC, in parallel, with separate on-disk caches and bounded retries (started
13:40:50 local, all done by 14:22:32):

| Source | Pools admitted | Discovery omissions | HTTP requests | Wall time |
| --- | ---: | ---: | ---: | --- |
| Moe Classic v1 | 14 | 14 (no pair) | 67 | ~1.5 min |
| Uniswap v3 | 13 | 99 (no pool) | 361 | ~8 min |
| FusionX v3 | 26 | 86 | 598 | ~16 min |
| Agni v3 | 45 | 67 | 1,083 | ~28 min |
| Moe LB v2.2 | 45 (28 empty books) | 9 (no pair) | 2,075 | ~42 min |

Reads are batched through **Multicall3** (`0xcA11…CA11`, `aggregate3`, explorer-verified
source, runtime code hash `0x8ec8aa5d…99ad` pinned in `config/protocols.yaml`
`network.multicall3`; re-verified at the block, each sub-call's success flag checked, the
first aggregated result cross-checked against a direct `eth_call`). CL tick walks read 64
bitmap words per step. The LB tree walk is inherently sequential per side
(`getNextNonEmptyBin`); its long pole was WMNT/USDT bin step 15 (1,366 + 1,714 bins, both
sides walked to the end). Every token's `decimals()` was verified on-chain by the Classic
and LB collectors. **Re-running all five `prepare` commands from the RPC cache reproduces
byte-identical source bundles, and `assemble` reproduces the corpus bundle hash.**

**Token semantics.** `tools/cl_evidence/capture_corpus_tokens.sh` forks the corpus block and,
for every universe token, transfers a tenth of an admitted pool's balance to a fresh
address and back with the deployed token bytecode: all 8 tokens move exactly the
requested amount both ways and keep `totalSupply` (`tests/fixtures/corpus/token_transfers.jsonl`,
checked offline by `test_every_universe_token_transfers_exactly`). The swap math itself
keeps its per-source fork evidence from WHI-1428–WHI-1434; the collectors re-verify every
pool's deployment fingerprint (normalized code hash / clone + CREATE2 identity, approved
LB hooks) at this block.

## 7. Bundle contents and cohorts

`manifest.json` checksums exactly `pools.json`, `cases.jsonl`, `provenance.json`,
`prices.json` and `corpus.json`; the loader also rejects any unlisted file in the bundle
directory. `provenance.json` embeds each source bundle's id, hash and full collection
provenance, the corpus config hash and the three export identities. `corpus.json` holds the
origin labels, strata/split definitions, universe, envelope and its per-pool check, the
exclusion rule, the boundary search records and per-case metadata (group, origin, stratum,
split, notional USD, leg, direct-pool counts, reachability). Cohort descriptors:

- `full_source` — all 143 pools of the five sources; every case.
- `sor_compatible` — the 98 pools whose source has a Uniswap SOR protocol (Agni/FusionX/
  Uniswap as V3, Moe Classic as V2; LB has none). A case unreachable in this graph is
  `unsupported` for SOR, never `no_route`; at this block every case is reachable in both
  cohorts, and every case with a direct pool also has an SOR-compatible one.

Offline `direct` replay (smoke profile, gross-only): 288/288 activity cases `ok`, 61/62
boundary cases `ok` (1 `no_route`), 48/48 no-direct cases `no_route` (as expected for a
single-pool baseline); no `incomplete_snapshot`.

Offline `path_split` smoke (WHI-1440; `config/corpus_path_split_smoke.yaml`, gross-only,
smoke-scale `max_hops: 2`, `max_splits: 4`, `percent_step: 5`, one isolated pass, no
truncation or limit hit in any algorithm): `path_split` 397/398 `ok` (1 dust boundary
case `no_route`: every path fails); never below `direct`, `single_path` or
`direct_split` on any case; strictly above both `single_path` and `direct_split` on 179
cases (chosen legs: 1 x208, 2 x56, 3 x46, 4 x87); median gain over `single_path` where
better 4.8 bps. Solve time median 0.38 s / p95 1.77 s / max 3.17 s per case (quotes
median 1156, max 4553). A 12-case in-process sample at `max_hops: 3` measured ~6.7 s
mean / 16.5 s max per case (quote-bound by large CL swaps), so the hop-3 corpus pass is
left to the calibrated profiles (I21). Not an optimality or net-output claim.

## 8. Reproduce

```bash
# 0. Dune (operator's Enterprise access; the SQL is generated, then run as saved queries)
uv run python main.py corpus sql --step activity > tests/fixtures/corpus/dune/q1_activity.sql
#    ...run it, save the result JSON, then:
uv run python main.py corpus ingest --name activity --raw <saved-result.json>
uv run python main.py corpus sql --step strata > tests/fixtures/corpus/dune/q2_strata.sql
uv run python main.py corpus ingest --name strata --raw <saved-result.json>
uv run python main.py corpus sql --step prices > tests/fixtures/corpus/dune/q3_prices.sql
uv run python main.py corpus ingest --name prices --raw <saved-result.json>
# 1. plan: cases, prices, envelope, the five prepare configs (offline)
uv run python main.py corpus plan --plan data/corpus/mantle-5src-101082044/plan.json
# 2. collect every source at the one block (online, bounded; run in the background)
for s in agni fusionx uniswap_v3 moe_classic moe_lb; do
  uv run python main.py prepare --source $s --block 101082044 \
    --block-hash 0x091b0759c9d3031f30658cdfa8bf4cd5ed311ece986e3c91eb1eeb121b2b65c4 \
    --prepare-config config/corpus/prepare/$s.yaml \
    --output data/corpus/mantle-5src-101082044/sources/$s \
    --rpc-cache-dir data/cache/rpc/$s > data/corpus/logs/$s.log 2>&1 &
done; wait
# 3. assemble + validate (offline)
uv run python main.py corpus assemble --plan data/corpus/mantle-5src-101082044/plan.json \
  --sources data/corpus/mantle-5src-101082044/sources \
  --output data/corpus/mantle-5src-101082044/bundle
uv run python main.py validate --bundle data/corpus/mantle-5src-101082044/bundle
# 4. evidence + fixture
tools/cl_evidence/capture_corpus_tokens.sh data/corpus/mantle-5src-101082044/bundle
uv run python main.py corpus fixture --bundle data/corpus/mantle-5src-101082044/bundle \
  --output tests/fixtures/corpus/bundle
uv run pytest tests/snapshot/test_corpus.py
```

`plan` refuses SQL files that drift from the config; `assemble` re-derives the plan from
the exports, requires each source bundle's provenance to name the checked-in generated
config by hash, and refuses a missing source, a source without pools, a foreign block, an
undeclared exclusion rule, an unverified token decimal, or an envelope that is not covered.

## 9. Checked-in fixture

`tests/fixtures/corpus/bundle` (`main.py corpus fixture`, selection
`config/corpus/fixture.yaml`): the 19 pools of mETH/WETH (all five sources), USDT0/WMNT,
USDC/USDT0 and USDT0/FBTC, copied byte-for-byte from the full bundle, with their 96 cases
(empirical strata, the USDC/FBTC no-direct pair, boundary cases). Its descriptor recomputes
the pool-dependent fields for the subset and names the full bundle (`subset_of`).
`tests/snapshot/test_corpus.py` checks the fixture (sources, one block, envelope, cohorts,
boundaries), rejects tampered, mixed-block, unlisted-file, zero-price, future-price,
undeclared-exclusion, reconstructed-order and envelope-violating variants, replays
`validate` and `run` with sockets disabled, and — when `data/` holds them — verifies the
full bundle against `full_bundle.json` and re-assembles it from the source bundles.

## 10. Limits

- One snapshot; no temporal generalization claim. Activity is Dune `dex.trades` legs of
  the five labels only (not TVL, not complete Mantle coverage).
- The universe is the 8 tokens of the 12 most active pairs; pools of other tokens (e.g.
  USDe, cmETH, AUSD) are outside this corpus by that rule.
- Boundary cases come from the frozen state and belong to a separately labeled group.
- The LB collector's per-side walk costs one RPC round trip per non-empty bin; see
  `docs/DEFERRED_ISSUES.md`.

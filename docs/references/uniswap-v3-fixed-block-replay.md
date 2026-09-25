# Uniswap v3 (Mantle) fixed-block snapshot and offline direct replay (WHI-1431 / I06)

Status: **Uniswap v3 is admitted for fixed-block collection.** `main.py prepare --source
uniswap_v3 --block <n>` captures verified Uniswap v3 pools (Mantle deployment,
`Uniswap/v3-core@v1.0.0`) at one finalized block through the shared CL collector
(`docs/references/agni-fixed-block-replay.md` §1 — block-hash pinning, finality, reorg
re-verification, envelope-driven tick walk, `incomplete_snapshot` refusal, batched RPC
with backoff and disk cache); `main.py run` replays `direct` on it offline. Every swap —
full fill *and* partial fill — its next state and its crossed ticks agree exactly with
fork execution of the deployed UniswapV3Pool bytecode and with Uniswap's own deployed
QuoterV2, in both directions. Uniswap v3 is admitted on its **own** evidence below;
nothing is inherited from the Agni/FusionX admissions except the shared code. It is an
SOR `V3` source, and that capability is exposed to cohort selection (§5).

## 1. What is Uniswap-specific

| | Uniswap v3 | (Agni v3 / FusionX v3, for contrast) |
| --- | --- | --- |
| Catalog record | `config/protocols.yaml` `uniswap_v3.cl_collection` | `agni_v3` / `fusionx_v3.cl_collection` |
| Pool fingerprint (immutable-normalized code hash) | `0x625b6d8f…cde0c9ca`, 22,142-byte pools | `0xa9364045…` 22,964 / `0x7ba898ca…` 22,718 |
| Pool immutables normalized | factory, token0, token1, fee, tickSpacing, maxLiquidityPerTick **+ `original`** (3/6/6/4/4/3/1 sites) | the first six only (7/6/6/8/4/3) |
| Compiler settings | solc 0.7.6, runs **800**, `bytecodeHash: none` (v3-core `hardhat.config.ts`) | runs 200 / 20 |
| Pool deployer | **none** — `UniswapV3Factory is UniswapV3PoolDeployer` (`factory_deploys_pools: true`) | separate contract, `factory.poolDeployer()` round-trip |
| Fee tiers | `100→1, 500→10, 3000→60, 10000→200` | `…, 2500→50, …` (no 3000) |
| `slot0.feeProtocol` | `uint8`, two 4-bit denominators | `uint32`, two 16-bit ratios |
| LM hook | none (`lmPool()` does not exist and is never called) | `lmPool` hook branch |
| Independent source match | explorer **and** local recompile (§2) | explorer + first-party record / + recompile |
| Second on-chain quote path | deployed QuoterV2 `0xdD489C75…B067Cf` | none / FusionX QuoterV2 |
| Swap callback (fork harness) | `uniswapV3SwapCallback` | `agniSwapCallback` / `fusionXV3SwapCallback` |
| SOR route protocol (`sor_protocol`) | `V3` | `V3` / `V3` |

The migrated swap semantics (`pools.concentrated.SOURCES["uniswap_v3"]`: 4-bit protocol-fee
denominator, no hook) come from WHI-1428's source diff
(`concentrated-liquidity-migration.md` §2); this issue proves they hold for real Uniswap
pools at a fixed block.

**`original` — the one new collector mechanism.** v3-core's `NoDelegateCall` stores
`address(this)` as an immutable, so every UniswapV3Pool embeds its *own address* once.
Without normalizing it, the eight pools below give eight different "normalized" hashes;
with it, one. There is no getter; the collector supplies the pool's own address
(`snapshot.config.KNOWN_POOL_IMMUTABLES`, `ConcentratedCollector._verify_pool`). Code that
embeds any other address there (a copy of another pool, a delegating proxy) fails the
fingerprint (`test_pool_code_embedding_another_pools_address_is_rejected`).

**Fingerprint.** At block `101057678` every non-zero `UniswapV3Factory.getPool` result
checked (USDT/WMNT, USDC/WMNT, USDC/USDT, WETH/WMNT, WETH/USDT, mETH/WETH, mETH/WMNT — eight
pools including the example pool) is 22,142 bytes and normalizes to the same hash, which
is also exactly Keccak-256 of the independent rebuild (§2). Genuine AgniPool and
FusionXV3Pool code fails it (`test_real_pool_code_fingerprints`, reusing WHI-1430's
captured example-pool code `tests/fixtures/fusionx/pool_code.json.gz`).

**No pool deployer.** `cl_collection.factory_deploys_pools: true` makes the collector pin
and hash-check the factory only and skip the `poolDeployer()` round trip (provenance
`catalog.pool_deployer: "factory"`); the catalog loader rejects the flag next to a
pinned `contracts.pool_deployer`. Pool identity rests on `getPool` + `pool.factory()` +
the fingerprint, as for the forks. Agni/FusionX behaviour is unchanged.

## 2. Independent local recompile

`tools/cl_evidence/rebuild_uniswap_v3.sh` clones `Uniswap/v3-core` at tag `v1.0.0`
(`e3589b19…`), compiles with solc-js 0.7.6 (runs 800, `istanbul`, `bytecodeHash: none`)
and compares with the deployed runtime code after zeroing compiler-reported immutable
sites:

| Contract | Built / on-chain bytes | Immutable sites | Result |
| --- | --- | --- | --- |
| `UniswapV3Pool` (`0x4cdFc22b…`) | 22,142 / 22,142 | 27 | identical |
| `UniswapV3Factory` (`0x0d922Fb1…`) | 24,535 / 24,535 | 1 (`NoDelegateCall.original`) | identical |

Keccak-256 of the pool build (immutables zero) is exactly the pinned fingerprint, and
Keccak-256 of the pool *creation* code is `0xe34f199b…87b8b54`, v3-periphery's canonical
`POOL_INIT_CODE_HASH` — the Mantle factory deploys byte-for-byte the canonical pool.

## 3. The published Uniswap v3 bundle

`tests/fixtures/uniswap_v3/bundle/` — `uniswap-v3-101057678-a2195dd2`, block `101057678`
(`0xa2195dd2…339354`, the catalog's preflight-verified candidate block, the same block as
the Agni and FusionX bundles). Produced by:

```bash
uv run python main.py prepare --source uniswap_v3 --block 101057678 \
  --block-hash 0xa2195dd2b8bf76e093509e7cdf013223ee2127b002be568284f989811a339354 \
  --output tests/fixtures/uniswap_v3/bundle
```

A cold run is ~600 HTTP requests, ~7 minutes on the public RPC; re-runs hit the
hash-pinned disk cache.

**Universe.** Uniswap v3 on Mantle is thin (its deepest pool, WMNT/mETH 1%, holds ~$7k;
the catalog example pool USDT/WMNT 0.05% ~$22), so the universe is not a liquidity-ranked
subset but *every pool that exists* among the five tokens the other Mantle sources also
list (USDT, USDC, WMNT, WETH, mETH): 10 pairs × every catalog fee tier through
`getPool` = 40 lookups → 12 admitted, 1 declared exclusion, 27 zero-address omissions
(all recorded in `provenance.discovery`).

| Pool | Pair | Fee / spacing | Collected words | Initialized ticks | `liquidity()` |
| --- | --- | --- | --- | --- | --- |
| `0x4cdfc22b…631d2b` (catalog example) | USDT/WMNT | 500 / 10 | 54–158 | 6 | 1.5e14 |
| `0x086f766b…d81266` | USDC/WMNT | 3000 / 60 | 15–57 | 2 | 1.2e13 |
| `0xfc60a4d0…c7820a` | WMNT/WETH | 500 / 10 | -45–22 | 1 | 4.9e17 |
| `0x082a6df2…fa0745` | WMNT/WETH | 3000 / 60 | -58–57 (whole range) | 0 | **0** |
| `0xc6463950…529a99` | WMNT/WETH | 10000 / 200 | -18–-1 (whole lower range) | 6 | 2.6e19 |
| `0xeaf42c2b…ccd77b` | WMNT/mETH | 3000 / 60 | -58–57 (whole range) | 0 | **0** |
| `0x5d637c5c…0f2686` | WMNT/mETH | 10000 / 200 | -3–-1 | 16 | 6.8e20 |
| `0x48ef5640…09deb7` | mETH/WETH | 100 / 1 | -2–6 | 6 | 4.0e18 |
| `0x2c7c187e…239ae8` | USDC/mETH | 3000 / 60 | -58–57 (whole range) | 2 | 1.7e13 |
| `0x9df1e55e…7afd32` | USDC/mETH | 10000 / 200 | 2–4 | 5 | 1.2e14 |
| `0x076eb72e…6b8f28` | USDT/WETH | 500 / 10 | 73–83 | 3 | 4.8e11 |
| `0xcff260ae…4d859b` | USDT/mETH | 3000 / 60 | -58–57 (whole range) | 6 | 4.6e13 |

`feeProtocol` is 0 on every admitted pool at this block; the non-zero 4-bit
denominator branch is covered by WHI-1428's controlled fixture (Uniswap `4/7`).

**Proven exhaustion instead of exclusion.** Where a reference case runs a pool dry, the
envelope walk continues to `MIN_TICK`/`MAX_TICK` (for tickSpacing ≥ 60 that is ≤ 58 words
each side of word 0, within `max_words_per_direction: 128`), so the case is an admitted,
*proven* `insufficient_liquidity` — including on the two `liquidity() == 0` pools, which
are therefore kept, not dropped.

**Explicit exclusion.** Only USDC/USDT 0.01% (`0x8cfee38a…9c9d2c`, tickSpacing 1,
`liquidity() == 0`, dust balances) is excluded, with the reason in the prepare config
and provenance: proving exhaustion from its current word (-1) would walk 3,465 words per
direction, far beyond the declared bound, so its envelope genuinely cannot be complete.
The pair has no other Uniswap pool and carries no cases.
*Update (WHI-1436):* the corpus replaces per-config exclusions with one declared,
source-agnostic rule and walks thin pools to MIN_TICK/MAX_TICK; at block 101082044 this
pool is admitted and proven exhausted (`docs/references/corpus.md` §5). The exclusion
here remains only because this provisional-block fixture is reproduced from this config.

**Reference cases** (the declared envelope, `config/prepare/uniswap_v3.yaml`): both
directions of the eight pairs with a pool, small/medium/large scaled to each pair's depth
(48 cases, 72 case × pool requests; WMNT ≈ $0.67, WETH ≈ $2,610, mETH ≈ $2,910). The
large cases deliberately reach the thin pools' depth. `direct` result (gross output):

| Case | In | `direct` out | | Case | In | `direct` out |
| --- | ---: | --- | --- | --- | ---: | --- |
| `usdt_wmnt_small` | 1 USDT | 1.4845 WMNT | | `wmnt_usdt_small` | 1 WMNT | 0.408114 USDT |
| `usdt_wmnt_medium` | 5 USDT | 7.19204 WMNT | | `wmnt_usdt_medium` | 10 WMNT | 0.445178 USDT |
| `usdt_wmnt_large` | 20 USDT | 17.9399 WMNT | | `wmnt_usdt_large` | 100 WMNT | 0.447968 USDT |
| `usdc_wmnt_small` | 1 USDC | 1.3597 WMNT | | `wmnt_usdc_small` | 1 WMNT | 0.620776 USDC |
| `usdc_wmnt_medium` | 5 USDC | 4.9557 WMNT | | `wmnt_usdc_medium` | 10 WMNT | `no_route` |
| `usdc_wmnt_large` | 20 USDC | 9.83047 WMNT | | `wmnt_usdc_large` | 100 WMNT | `no_route` |
| `wmnt_weth_small` | 1 WMNT | 0.000250778 WETH (1%) | | `weth_wmnt_small` | 0.0005 WETH | 1.95063 WMNT (1%) |
| `wmnt_weth_medium` | 10 WMNT | 0.00249432 WETH (1%) | | `weth_wmnt_medium` | 0.002 WETH | 7.77495 WMNT (1%) |
| `wmnt_weth_large` | 100 WMNT | 0.00602761 WETH (0.05%) | | `weth_wmnt_large` | 0.01 WETH | 38.1553 WMNT (1%) |
| `wmnt_meth_small` | 10 WMNT | 0.00227159 mETH | | `meth_wmnt_small` | 0.005 mETH | 21.5577 WMNT |
| `wmnt_meth_medium` | 100 WMNT | 0.0226695 mETH | | `meth_wmnt_medium` | 0.05 mETH | 214.652 WMNT |
| `wmnt_meth_large` | 1,000 WMNT | 0.221928 mETH | | `meth_wmnt_large` | 0.5 mETH | 2,033.99 WMNT |
| `meth_weth_small` | 0.005 mETH | 0.00547588 WETH | | `weth_meth_small` | 0.005 WETH | 0.00455312 mETH |
| `meth_weth_medium` | 0.05 mETH | 0.0541197 WETH | | `weth_meth_medium` | 0.02 WETH | 0.0181471 mETH |
| `meth_weth_large` | 0.2 mETH | 0.208553 WETH | | `weth_meth_large` | 0.1 WETH | 0.0890283 mETH |
| `usdc_meth_small` | 5 USDC | 0.00170164 mETH (0.3%) | | `meth_usdc_small` | 0.002 mETH | 5.78219 USDC (1%) |
| `usdc_meth_medium` | 50 USDC | 0.0168095 mETH (1%) | | `meth_usdc_medium` | 0.02 mETH | 57.37 USDC (1%) |
| `usdc_meth_large` | 500 USDC | 0.15755 mETH (1%) | | `meth_usdc_large` | 0.2 mETH | 532.111 USDC (1%) |
| `usdt_weth_small` | 1 USDT | 0.000358617 WETH | | `weth_usdt_small` | 0.0005 WETH | 1.26977 USDT |
| `usdt_weth_medium` | 5 USDT | 0.00155155 WETH | | `weth_usdt_medium` | 0.002 WETH | 4.4 USDT |
| `usdt_weth_large` | 20 USDT | 0.00482752 WETH | | `weth_usdt_large` | 0.01 WETH | 12.8424 USDT |
| `usdt_meth_small` | 1 USDT | 0.000311104 mETH | | `meth_usdt_small` | 0.0005 mETH | `no_route` |
| `usdt_meth_medium` | 5 USDT | 0.00155315 mETH | | `meth_usdt_medium` | 0.005 mETH | `no_route` |
| `usdt_meth_large` | 20 USDT | `no_route` | | `meth_usdt_large` | 0.05 mETH | `no_route` |

`no_route` = every candidate pool is proven exhausted for that amount (the fork's swap
stops at the price limit with input left). The USDT/WMNT and USDC/WMNT outputs are far
below the WMNT spot price because almost no in-range liquidity sits on that side — the
real state at the block, reproduced exactly by the fork.

## 4. Independent fixed-block evidence

`tools/cl_evidence/replay_uniswap_v3.sh <bundle>` writes the request file
(`tools/cl_evidence/requests/uniswap_v3_replay.json`: inputs only) and runs
`tools/cl_evidence/test/CaptureUniswapV3Replay.t.sol` on a Mantle fork at the bundle's
block (refusing a non-5000 chain or a different block hash). Per request it records the
deployed pool's pre-state, every bitmap word and initialized tick of the collected
range, the pool code hash, **Uniswap QuoterV2's `quoteExactInputSingle`** for the same
input (or `quoter_reverted` — QuoterV2 refuses a swap that moves no token, "swaps
entirely within 0-liquidity regions are not supported"), the swap's `(amount0, amount1)`
at the widest price limit (a full or a partial fill), the post-state, the ticks whose
storage changed, and a follow-up reverse swap of half the output through the
post-state. Crossed-tick discovery walks `tickBitmap` word by word, since a partial fill
can traverse the whole tick range. Nothing in it imports or reimplements the Python under
test. Output: `tests/fixtures/uniswap_v3/evidence.jsonl.gz` (970 records; 191 KB of JSON
Lines gzipped to 22 KB). A cold fork run takes ~4.5 minutes on the public RPC.

`tests/snapshot/test_uniswap_v3.py` requires, offline:

- collected scalars, words, ticks and pool code hash = the fork;
- every case × pool (72): the migrated `swap`'s signed amounts, next-state scalars,
  crossed-tick set and flipped fee-growth-outside values = the fork's, for 51 full fills,
  9 partial fills and 12 no-token swaps; `quote_exact_in` is `ok` exactly for the full
  fills and `insufficient_liquidity` (with the fork's consumed/produced amounts) exactly
  for the rest; output and post-price = QuoterV2 wherever it answers; the follow-up swap
  from the Python next state = the fork's (60 follow-ups); 16 zero-for-one and 12
  one-for-zero initialized-tick crossings on full fills;
- `direct` picks the fork's best full-filling pool with the fork's output, or
  `no_route` when none fills, in both directions;
- `validate` + `run` over a copy of the bundle with sockets disabled reproduce it;
- the collector, driven by a scripted node serving the bundle state, reproduces
  `pools.json`/`cases.jsonl` byte-for-byte without ever calling `poolDeployer()` or
  `lmPool()`, and aborts on a reorg;
- **incompatible deployments are rejected**: real AgniPool / FusionXV3Pool code behind the
  Uniswap factory and pool code embedding another pool's address (`code_hash_mismatch`),
  a foreign factory (`code_hash_mismatch`), a pool reporting another factory
  (`identity_mismatch`), the Pancake 2500 tier (`invalid_request`), a factory tick
  spacing disagreeing with the catalog (`fee_tier_mismatch`);
- **malformed state is rejected**: a locked `slot0`, a `feeProtocol` wider than `uint8`, a
  bitmap bit without an initialized tick (`inconsistent_state`), an envelope beyond the
  word bound (`incomplete_snapshot`), a mixed-block / truncated / wrong-range / unknown-
  source bundle record (`BundleError`), an LM hook on this hookless source (`unsupported`),
  malformed catalog admission (`ConfigError`).

## 5. Source capability for cohort selection

`config/protocols.yaml` now carries `sor_protocol` per source — the Uniswap SOR route
protocol its pools enter the matched V2/V3 cohort as (`uni-sor-port-contract.md` §2,
A-1, D-4): `V3` for `uniswap_v3`, `agni_v3`, `fusionx_v3`; absent (never SOR-routable)
for Moe LB. Merchant Moe Classic (`V2`) is declared by its own admission (WHI-1432).
`ProtocolCatalog.sor_protocols()` returns the mapping, and every bundle the shared CL
collector publishes records `provenance.source_capability = {protocol_family,
sor_protocol}`, so the corpus/cohort work (WHI-1436, WHI-1444) can form the matched cohort
from the bundle and catalog alone. (Existing Agni/FusionX fixture bundles predate the
provenance key; the catalog entry covers them.)

## 6. Residual gaps

1. The collection block is the catalog candidate block (provisional admission evidence,
   DESIGN §2.2); the final five-source corpus (WHI-1436) re-runs every collector at one
   common block.
2. Uniswap v3 on Mantle is shallow; the envelope is sized to it (tens to hundreds of
   dollars on most pairs, up to ~$1.5k on WMNT/mETH and USDC/mETH). Stratified empirical
   requests are WHI-1436's job.
3. `feeProtocol` is zero on every admitted pool at this block, so the non-zero
   protocol-fee branch has no *fixed-block* Uniswap evidence (it has WHI-1428 fork
   evidence on a controlled pool).

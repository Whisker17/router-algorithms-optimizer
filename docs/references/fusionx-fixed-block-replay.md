# FusionX v3 fixed-block snapshot and offline direct replay (WHI-1430 / I05)

Status: **FusionX v3 is admitted for fixed-block collection.** `main.py prepare --source
fusionx --block <n>` captures verified FusionX pools at one finalized block through the
shared CL collector (`docs/references/agni-fixed-block-replay.md` §1 — block-hash
pinning, finality, reorg re-verification, envelope-driven tick walk, `incomplete_snapshot`
refusal, batched RPC with backoff and disk cache); `main.py run` replays `direct` on it
offline. Every quote, next state and crossed tick agrees exactly with fork execution of
the deployed FusionXV3Pool bytecode *and* with FusionX's own deployed QuoterV2, in both
directions. FusionX is admitted on its **own** evidence below; nothing is inherited from
Agni's admission except the shared code.

## 1. What is FusionX-specific

| | FusionX v3 | (Agni v3, for contrast) |
| --- | --- | --- |
| Catalog record | `config/protocols.yaml` `fusionx_v3.cl_collection` | `agni_v3.cl_collection` |
| Pool fingerprint (immutable-normalized code hash) | `0x7ba898ca…2bf293`, 22,718-byte pools | `0xa9364045…ffb015f`, 22,964 bytes |
| Compiler settings | solc 0.7.6, runs **20**, `bytecodeHash: none` | solc 0.7.6, runs 200 |
| Independent source match | explorer **and** local recompile (§2) | explorer + first-party deployment record |
| Prepare selection | `config/prepare/fusionx.yaml` (USDT/WMNT, the catalog pair) | `config/prepare/agni.yaml` (USDC/WMNT) |
| Second on-chain quote path | deployed QuoterV2 `0x90f72244…913995` | none deployed |
| LM hook | **live** on the 0.05% pool (`0x19170a0f…023107`) | all admitted pools `lmPool == 0` |
| Swap callback (fork harness) | `fusionXV3SwapCallback` | `agniSwapCallback` |

The migrated swap semantics (`pools.concentrated.SOURCES["fusionx_v3"]`: Pancake-v3
protocol-fee ratio, LM-hook branch) come from WHI-1428's source diff
(`concentrated-liquidity-migration.md` §2: `FusionXV3Pool` ≡ `AgniPool` after renaming);
this issue proves they hold for real FusionX pools at a fixed block.

**Fingerprint.** At block `101057678` all four USDT/WMNT FusionX pools
(`FusionXV3Factory.getPool` for fee 100/500/2500/10000) are 22,718 bytes and zeroing the
PUSH32 sites equal to each pool's own `factory/token0/token1/fee/tickSpacing/
maxLiquidityPerTick` (7/6/6/8/4/3 sites for every pool) gives one hash. A genuine AgniPool
or UniswapV3Pool fails it (`test_real_pool_code_fingerprints`, using the real runtime
code of the three catalog example pools captured at the block,
`tests/fixtures/fusionx/pool_code.json.gz`, 35 KB).

## 2. Independent local recompile (closes catalog §8.1)

`tools/cl_evidence/rebuild_fusionx.sh` clones `FusionX-Finance/v3-contracts@7f7406e`,
compiles `projects/v3-core` with solc-js 0.7.6 (runs 20, `istanbul`, `bytecodeHash: none`,
matching the on-chain CBOR trailer `a164736f6c6343000706000a`) and compares with the
deployed runtime code after zeroing compiler-reported immutable sites:

| Contract | Built / on-chain bytes | Immutable sites | Result |
| --- | --- | --- | --- |
| `FusionXV3Pool` (`0x262255f4…`) | 22,718 / 22,718 | 34 | identical |
| `FusionXV3Factory` (`0x530d2766…`) | 3,806 / 3,806 | 2 (`poolDeployer`) | identical |
| `FusionXV3PoolDeployer` (`0x8790c2c3…`) | 24,461 / 24,461 | 0 | identical |

Keccak-256 of the pool build (immutables zero) is exactly the pinned fingerprint — the
catalog's normalization and the compiler's own immutable map agree. The pinned commit
ships only `projects/v3-core`; its one external import, the `IFusionXV3LmPool`
interface (`accumulateReward(uint32)`, `crossLmTick(int24,bool)`), is written from the
verified source's flattened `@fusionx/v3-lm-pool@v1.0.0` section. (The x86-only native
solc 0.7.6 does not run on this arm64 host without Rosetta, hence solc-js.)

## 3. LM hook

The FusionX 0.05% pool — by far the deepest FusionX pool — calls a live LM hook on every
swap (`accumulateReward` once, `crossLmTick` per crossed initialized tick). The hook
returns nothing and the pool writes nothing from it, so the simulator models it as a
counted no-op (WHI-1428 D3). This issue adds, in the shared collector:

- a non-zero `lmPool` must have runtime code and `lmPool.pool()` must equal the pool
  (`identity_mismatch` / `missing_code` otherwise);
- provenance records `lm_pool_identity` (`code_hash` `0x1d8b1774…9f6ea0`, the pool, the
  modeling choice). Pools without a hook get no such key, so the Agni bundle is
  byte-for-byte unchanged.

The hook's source is not explorer-verified (Routescan has none); its only possible
effect is a revert, which is not modeled. At this block the fork executes the real hook
for all six cases on the hooked pool — 1,202 initialized-tick crossings, i.e. 1,202
`crossLmTick` calls plus six `accumulateReward` — and every swap succeeds and matches.

## 4. The published FusionX bundle

`tests/fixtures/fusionx/bundle/` — `fusionx-v3-101057678-a2195dd2`, block `101057678`
(`0xa2195dd2…339354`, the catalog's preflight-verified candidate block, the same block as
the Agni bundle), bundle hash `f349fb26…9e9a18f5`. Produced by:

```bash
uv run python main.py prepare --source fusionx --block 101057678 \
  --block-hash 0xa2195dd2b8bf76e093509e7cdf013223ee2127b002be568284f989811a339354 \
  --output tests/fixtures/fusionx/bundle
```

From an empty RPC cache (2026-09-24): 575 HTTP requests, 17 retry rounds, ~10 minutes.

| Pool | Fee / spacing | Collected words | Tick range | Initialized ticks | LM hook |
| --- | --- | --- | --- | --- | --- |
| `0x262255f4…2a0aff` (catalog example) | 500 / 10 | 101–112 | 258560–289279 | 1,621 | live |
| `0xed3ee32b…29570e` | 2500 / 50 | 11–31 | 140800–409599 | 642 | none |
| `0xe39688f2…1e0402` | 10000 / 200 | 2–8 | 102400–460799 | 151 | none |

**Explicit omission.** The 0.01% tier (`0x4a313244…782a5f88`, tickSpacing 1) is looked up
and recorded in `provenance.discovery.omitted` with the reason declared in the prepare
config. Measured at the block: a 200 USDT USDT→WMNT swap fits in 2 bitmap words, but
the 500 USDT medium case still needs state beyond 64 words (observed at 32 and 64) and
proving exhaustion would mean walking ~4,560 words to `MIN_TICK`. Without the declaration
the collector refuses publication with `incomplete_snapshot`.

Reference cases (10/500/5,000 USDT and 10/500/5,000 WMNT; WMNT ≈ $0.66) and the
`direct` result — the 0.05% pool wins every case:

| Case | Amount in | 0.05% out | 0.25% out | 1% out | Ticks crossed (0.05%) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `usdt_wmnt_small` | 10 USDT | 15.127833 WMNT | 14.741623 | 14.756395 | 1 |
| `usdt_wmnt_medium` | 500 USDT | 735.933164 | 108.596488 | 451.367861 | 38 |
| `usdt_wmnt_large` | 5,000 USDT | 4,788.533222 | 109.087149 | 453.770337 | 713 |
| `wmnt_usdt_small` | 10 WMNT | 6.595967 USDT | 6.506221 | 6.577884 | 1 |
| `wmnt_usdt_medium` | 500 WMNT | 323.140639 | 188.384428 | 286.327455 | 42 |
| `wmnt_usdt_large` | 5,000 WMNT | 2,613.661903 | 191.252101 | 691.089293 | 407 |

## 5. Independent fixed-block evidence

`tools/cl_evidence/replay_fusionx.sh <bundle>` writes the request file
(`tools/cl_evidence/requests/fusionx_v3_replay.json`: inputs only) and runs
`tools/cl_evidence/test/CaptureFusionXReplay.t.sol` on a Mantle fork at the bundle's block
(refusing a non-5000 chain or a different block hash). Per request it records the
deployed pool's pre-state, every bitmap word and initialized tick of the collected
range, the pool and LM-hook code hashes, **FusionX QuoterV2's `quoteExactInputSingle`**
for the same input, the swap's `(amount0, amount1)`, the post-state, the ticks whose
storage changed, and a follow-up reverse swap through the post-state. Nothing in it
imports or reimplements the Python under test. Output:
`tests/fixtures/fusionx/evidence.jsonl.gz` (5,046 records; 1.6 MB of JSON Lines gzipped
to 273 KB). A first run fetched the fork state slot by slot through the public RPC in
~40 minutes; Foundry caches it under `~/.foundry/cache/rpc/mantle/101057678`.
QuoterV2 agrees on output and post-price for all 18 requests (its
`initializedTicksCrossed` counts differently and is recorded, not compared).

`tests/snapshot/test_fusionx.py` requires, offline:

- collected scalars, words, ticks, pool code hash and LM-hook code hash = the fork;
- every case × pool: output, next-state scalars, crossed-tick set and flipped
  fee-growth-outside values = the fork; output and post-price = QuoterV2; the follow-up
  swap from the Python next state = the fork's; `lm_pool_hook_calls = 1 + crossed` on
  the hooked pool;
- `direct` picks the fork's best pool with the fork's output, in both directions;
- `validate` + `run` over a copy of the bundle with sockets disabled reproduce it;
- the collector, driven by a scripted node serving the bundle state, reproduces
  `pools.json`/`cases.jsonl` byte-for-byte and aborts on a reorg;
- **incompatible deployments are rejected**: real AgniPool / UniswapV3Pool code behind the
  FusionX factory (`code_hash_mismatch`), a foreign factory (`code_hash_mismatch`), a
  broken `poolDeployer()` round trip or a pool reporting another factory
  (`identity_mismatch`), an LM hook bound to another pool (`identity_mismatch`) or
  without code (`missing_code`), Uniswap's 3000 tier (`invalid_request`), a factory
  tick spacing disagreeing with the catalog (`fee_tier_mismatch`).

## 6. Residual gaps

1. The collection block is the catalog candidate block (provisional admission evidence,
   DESIGN §2.2); the final five-source corpus (WHI-1436) re-runs every collector at one
   common block.
2. LM-hook reverts remain unmodeled; the hook's own source is unverified (identity and
   code hash are recorded; a changed hook is visible in provenance).
3. One pair; stratified cases and more pairs are WHI-1436's job. The 0.01% tier is the
   only declared exclusion.
   *Update (WHI-1436):* the corpus replaces per-config exclusions with one declared,
   source-agnostic rule and walks thin pools to MIN_TICK/MAX_TICK; at block 101082044
   this pool is admitted (`docs/references/corpus.md` §5). The exclusion here remains
   only because this provisional-block fixture is reproduced from this config.

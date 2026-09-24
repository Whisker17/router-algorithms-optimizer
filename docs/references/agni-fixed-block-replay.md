# Agni v3 fixed-block snapshot and offline direct replay (WHI-1429 / I04)

Status: **Agni v3 is admitted for fixed-block collection.** `main.py prepare --source
agni --block <n>` captures verified Agni pools at one finalized block and publishes an
immutable bundle; `main.py run` replays the `direct` baseline on it offline. The result
agrees exactly with fork execution of the deployed AgniPool bytecode at that block, in
both swap directions. FusionX (WHI-1430) and Uniswap v3 (WHI-1431) will reuse the shared
collector but are **not** admitted by it (see §1).

## 1. Shared, source-parametrized CL collector

`snapshot/collectors/concentrated.py` collects any Uniswap-v3-family source that has
*both* an admitted `cl_collection` block in `config/protocols.yaml` and migrated swap
semantics in `pools.concentrated.SOURCES`. Only `agni_v3` has the former today;
`fusionx_v3`/`uniswap_v3` are refused with `source_not_admitted`
(`test_only_admitted_sources_are_collected`). A later source adds its own catalog block
and a two-line `snapshot/collectors/<name>.py`, plus one `COLLECTORS` entry.

Selection lives in `config/prepare/agni.yaml` (pairs x fee tiers, reference cases = the
declared amount envelope, walk limits, RPC patience); verified facts stay in the
catalog.

| Step | What is checked | Failure code (publication refused) |
| --- | --- | --- |
| Block identity | `eth_chainId` = 5000; block N resolved once to hash/timestamp; optional `--block-hash` must match; N ≤ the node's `finalized` head | `wrong_chain`, `block_hash_mismatch`, `block_not_finalized` |
| Pinned reads | every `eth_call`/`eth_getCode` is EIP-1898 `{"blockHash": h, "requireCanonical": true}` — never a number, never `latest`, no fallback block | (node error → `rpc_unavailable`) |
| Deployment | AgniFactory and AgniPoolDeployer code hashes = catalog pins; `factory.poolDeployer()` round-trips | `code_hash_mismatch`, `identity_mismatch` |
| Discovery | `factory.getPool(token0, token1, fee)` per configured tier; `feeAmountTickSpacing(fee)` = catalog tier | zero address → recorded omission; `fee_tier_mismatch` |
| Pool code | immutable-normalized runtime-code hash = `cl_collection.pool_code_normalized_hash`; the example pool also matches its exact pinned hash | `code_hash_mismatch` |
| Pool identity | `factory()`, sorted `token0()/token1()`, `fee()`, `tickSpacing()` agree; `slot0.unlocked` | `identity_mismatch`, `inconsistent_state` |
| State | `slot0`, `liquidity`, `feeGrowthGlobal{0,1}X128`, `protocolFees`, `lmPool`; `tickBitmap` words; `ticks()` for every set bit (must say `initialized`, gross > 0) | `inconsistent_state` |
| Tick recovery | walk driven by the migrated simulator: the largest reference case per direction is quoted on the collected state and the word range grows toward the side that returned `incomplete_snapshot`, then `margin_words` more; beyond `max_words_per_direction` | `incomplete_snapshot` |
| Admission | every case x pool of its pair quotes `ok` or `insufficient_liquidity` | `incomplete_snapshot`, `admission_failed` |
| Re-verification | header re-read by number *and* by hash after all state reads; a different hash (reorg) invalidates the pending bundle | `block_hash_mismatch` |
| Publish | `snapshot.bundle.write_bundle`: temp dir → full `load_bundle` round trip → atomic rename; refuses an existing directory | — |

**Why a normalized code hash.** Solidity 0.7.6 embeds every `immutable` as a `PUSH32`,
so each AgniPool's exact code hash depends on its own factory/tokens/fee/tickSpacing/
maxLiquidityPerTick. At block 101057678 all four USDC/WMNT AgniPools are 22,964 bytes,
differ only inside `PUSH32` immediates, and zeroing the sites equal to their own getter
values gives one hash (`0xa9364045…ffb015f`; 7/6/6/8/4/3 sites for every pool). A
coincidental constant match can only *change* the normalized bytes, so the check fails
closed.

**RPC behaviour** (`snapshot/rpc.py`). The public endpoint answers batches with
per-item `-32016 rate limit exceeded` beyond ~5 items and has 1–5 s latency, and its
load-balanced nodes can briefly lag (`header not found`). Calls go in JSON-RPC batches of
`rpc.max_batch_size` (5); rate-limit/internal/lagging-node errors and network failures
are retried with exponential backoff up to `rpc.max_attempts`; deterministic errors
(reverts) are raised at once. Hash-pinned results are appended to an on-disk cache
(`data/cache/rpc/<blockHash>.jsonl`, gitignored), which can never be stale because state
at a block hash is immutable; block-header queries are never cached. A first Agni
collection took ~7 minutes (~2,000 reads); a re-run from the cache takes seconds.

## 2. Bundle format additions (schema 1, additive)

- `pools.json` entries with `"family": "concentrated"` carry the full
  `ConcentratedPoolState` (decimal-string integers), `bitmap_word_range`, the non-zero
  bitmap words, every initialized tick's `TickInfo`, and
  `read_at: {block_number, block_hash}`. The loader rejects a record whose `read_at`
  differs from the manifest block, a word range not containing the current tick, a tick
  that is not a set bit of a collected word, and any set bit without its tick data (the
  collected range must be internally complete).
- `provenance.json` (checksummed like every file): source, block identity, catalog pins,
  prepare-config hash, discovery (admitted + omitted with reasons), per-pool code hashes,
  immutable sites and **completeness bounds** (word range, tick range, initialized-tick
  count, limits, the envelope amount/end tick per direction), and the admission table.
  Its `block` must equal the manifest's. It contains no timestamps or call counts, so a
  re-run at the same block produces a byte-identical bundle (and bundle hash).
- Constant-product entries are unchanged; the synthetic fixture is byte-identical.

## 3. The published Agni bundle

`tests/fixtures/agni/bundle/` — `agni-v3-101057678-a2195dd2`, block `101057678`
(`0xa2195dd2…339354`, timestamp `1790245668`, the catalog's preflight-verified candidate
block), bundle hash `05002866…0c561726`. Produced by:

```bash
uv run python main.py prepare --source agni --block 101057678 \
  --block-hash 0xa2195dd2b8bf76e093509e7cdf013223ee2127b002be568284f989811a339354 \
  --output tests/fixtures/agni/bundle
```

A fresh run from an empty RPC cache (2026-09-24: 467 HTTP requests, 7 retry rounds,
~10 minutes) produced a byte-identical `pools.json`; re-runs from the disk cache take
seconds. The bundle contains no run-time metadata, so the same block and config always
give the same bundle hash.

| Pool | Fee / spacing | Collected words | Tick range | Initialized ticks |
| --- | --- | --- | --- | --- |
| `0x1858d52c…17144f` (catalog example) | 500 / 10 | 83–114 | 212480–294399 | 1,189 |
| `0x9cae9b5d…907242` | 2500 / 50 | 11–31 | 140800–409599 | 480 |
| `0x8e2c009e…2cc724` | 10000 / 200 | 1–8 | 51200–460799 | 149 |

**Explicit omission.** The 0.01% tier (`0x7b3a4b36…166f85`, tickSpacing 1) is looked up
and recorded in `provenance.discovery.omitted` with the reason declared in the prepare
config: it holds under ~150 USDC of depth below the price, so a 200 USDC swap already
needs more than 48 bitmap words, and proving real exhaustion would mean walking ~4,500
words to `MIN_TICK`. Without the declaration the collector refuses publication with
`incomplete_snapshot` (observed at 16 and 48 words) rather than truncating the envelope.

Reference cases (both directions: 10/500/5,000 USDC and 10/500/5,000 WMNT, WMNT ≈ $0.66
at the block) and the `direct` result — the
0.05% pool wins every case at this block:

| Case | Amount in | 0.05% out | 0.25% out | 1% out | `direct` | Ticks crossed (0.05%) |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| `usdc_wmnt_small` | 10 USDC | 15.075145… WMNT | 14.880038… | 14.728311… | 0.05% | 5 |
| `usdc_wmnt_medium` | 500 USDC | 688.913864… | 410.400293… | 381.370496… | 0.05% | 209 |
| `usdc_wmnt_large` | 5,000 USDC | 1,186.308131… | 477.194239… | 381.569997… | 0.05% | 455 |
| `wmnt_usdc_small` | 10 WMNT | 6.598338 USDC | 6.567498 | 6.551335 | 0.05% | 4 |
| `wmnt_usdc_medium` | 500 WMNT | 314.273338 | 238.438566 | 248.432422 | 0.05% | 94 |
| `wmnt_usdc_large` | 5,000 WMNT | 2,393.589594 | 278.666953 | 333.490751 | 0.05% | 733 |

Raw integers are in the evidence file; these pools are shallow near the price, so the
large cases move the price far (up to 733 initialized ticks crossed).

## 4. Independent fixed-block evidence

`tools/cl_evidence/replay_agni.sh <bundle>` writes the request file
(`tools/cl_evidence/requests/agni_v3_replay.json`: pool, direction and amount for every
case x pool, plus the bundle block — inputs only) and runs
`tools/cl_evidence/test/CaptureAgniReplay.t.sol` with `forge test` on a Mantle fork at
the bundle's block (refusing a non-5000 chain or a different block hash). For every
request the deployed AgniPool executes the exact-input swap at the widest price limit;
the fork records pre-state scalars, every bitmap word of the collected range, the
initialized ticks of the traversed interval before and after, `(amount0, amount1)`, the
post-state, and a follow-up reverse swap of half the output through that post-state.
Nothing in the generator imports or reimplements the Python under test.

`tests/snapshot/test_agni.py` requires, offline:

- collected scalars, bitmap words and traversed ticks = fork storage;
- every case x pool quote, next-state scalars, crossed-tick set and flipped
  fee-growth-outside values = the fork, and the follow-up swap from the Python next
  state = the fork's follow-up;
- `direct` picks the fork's best pool with the fork's output, in both directions;
- `validate` + `run` over a copy of the bundle with sockets disabled reproduce it.

## 5. Tick bounds and failure behaviour

A quote whose traversal needs a bitmap word outside `bitmap_word_range` (or tick data
missing inside it) is `incomplete_snapshot` and leaves the input untouched. `direct` now
reports `incomplete_snapshot` for the whole case when any candidate is incomplete
instead of silently choosing among the rest (the missing pool could have been best).

## 6. Residual gaps

1. The collection block is the catalog candidate block (provisional admission evidence,
   DESIGN §2.2); the final five-source corpus (WHI-1436) re-runs every collector at one
   common block.
2. The Agni example pair is shallow; stratified empirical cases and additional pairs are
   WHI-1436's job. The explicit per-tier exclusion is the only way this collector drops a
   discovered pool.
3. LM-hook reverts remain unmodeled (all three admitted pools have `lmPool == 0` at this
   block, recorded in provenance).

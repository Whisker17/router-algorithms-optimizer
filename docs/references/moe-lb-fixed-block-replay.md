# Merchant Moe Liquidity Book v2.2 fixed-block snapshot and offline direct replay (WHI-1434 / I09)

Status: **Merchant Moe LB v2.2 is admitted for fixed-block collection.** `main.py prepare
--source moe_lb --block <n>` captures every LB pair of the configured token universe at one
finalized block (`snapshot/collectors/liquidity_book.py`, selection
`config/prepare/moe_lb.yaml`, admission `config/protocols.yaml` `moe_lb_v2_2.lb_collection`)
with a bin snapshot that is complete for the declared amount envelope in both directions;
`main.py validate` / `run` replay it offline. Every reference swap on every pair, and a
sequential reverse swap through each post-state, agree exactly with fork execution of the
deployed `LBPair` clones and their live hooks: output, every per-bin `Swap` event
(including the per-bin volatility accumulator and fees), the next active id, variable fee
parameters, reserves, protocol fees and every touched bin. The swap math is WHI-1433's
`pools/liquidity_book.py` (`docs/references/liquidity-book-migration.md`). LB has **no SOR
capability** (§6).

## 1. Collector (`snapshot/collectors/liquidity_book.py`)

Block identity and reads come from `snapshot/collectors/fixed_block.py`
(`FixedBlockReader`): chain id, block number → hash (optionally `--block-hash`), ≤ the
finalized head, every read EIP-1898 pinned to the hash with `requireCanonical`, the header
re-read by number and by hash before publishing (a reorg aborts). Reads go through the
batched public-RPC transport (batches of 5, bounded backoff, on-disk cache of hash-pinned
results).

1. **Deployment.** `LBFactory` and the `LBPair` implementation `0xf6863Db7…DDB3B` code
   hashes equal the catalog pins; `factory.getLBPairImplementation()` is that
   implementation. Every catalog-approved hook implementation must also be admitted as
   amount-neutral by the simulator (`pools.liquidity_book.SOURCES`), otherwise
   `config_incomplete`.
2. **Discovery.** `factory.getAllLBPairs(tokenA, tokenB)` over the configured token pairs
   returns every bin step (with `createdByOwner` / `ignoredForRouting`, recorded). An empty
   answer is a recorded omission; `excluded_bin_steps: [{bin_step, reason}]` in the prepare
   config would turn a pair into a reasoned omission (none is used), and an exclusion the
   factory does not list is refused. Pairs with an empty book are collected, not skipped.
3. **Pair identity** (before any state is trusted). An LB pair is a 97-byte
   `ImmutableClone` (`joe-v2 src/libraries/ImmutableClone.sol`, the same layout as Moe
   Classic's). The runtime must equal *exactly* the clone of the pinned implementation with
   immutable args `tokenX ++ tokenY ++ uint16 binStep` (`LBFactory.createLBPair`,
   `LBFactory.sol:365` at v2.2.0) — so the delegatecall target, the X/Y order and the bin
   step are read from the bytecode — and the address must equal the CREATE2 address the
   factory deploys it to (salt `keccak256(abi.encode(tokenA, tokenB, binStep))` with the
   tokens sorted). `factory.getLBPairInformation(tokenX, tokenY, binStep)` must round-trip
   to the pair (same `createdByOwner` / `ignoredForRouting`), and `getTokenX`,
   `getTokenY`, `getBinStep`, `getFactory`, `implementation` must agree. Token
   `decimals()` must equal the declaration.
4. **State.** `getActiveId`, `getReserves`, `getProtocolFees`, `getStaticFeeParameters`
   (widths checked), `getVariableFeeParameters` (volatility accumulator / reference,
   `idReference`, `timeOfLastUpdate`, which must not be after the block) and the frozen
   block timestamp (`LiquidityBookPoolState.block_timestamp` = the bundle block's; the
   loader enforces it). Each token's `balanceOf(pair)` must equal reserves + protocol fees
   (`_reserves`): a pending donation would be credited to the next swapper
   (`receivedX/Y = balance − reserve`), which the migrated transition does not model →
   `unsupported_token_behavior`.
5. **Hooks** (resolves the WHI-1433 deferred item, `docs/DEFERRED_ISSUES.md`). The pair's
   `getLBHooksParameters()`: a swap-flagged hook must be an `ImmutableClone` of an
   implementation on the catalog's approved `lb_collection.swap_hooks` list
   (`LBHooksRewarder`), whose code hash equals the pin, whose clone args start with this
   pair and whose `getLBPair()` returns it. The rewarder's `getExtraHooksParameters()` is
   then read: a swap-flagged extra hook must be a clone of an approved
   `lb_collection.extra_swap_hooks` implementation (`LBHooksExtraRewarder`, pinned code
   hash) with clone args `(pair, reward token, rewarder)`, `getLBPair()` = the pair and
   `getParentRewarder()` = the rewarder. Anything else — an unknown implementation, a flag
   with a zero address, an unbound clone — refuses the pool (`unsupported_hook` /
   `identity_mismatch` / `code_hash_mismatch`). Hook and extra-hook addresses, clone code
   hashes, clone args, implementations and implementation code hashes go into provenance
   (`pools.<pair>.hooks`), and the state carries `hooks_parameters`,
   `swap_hook_implementation`, `extra_hooks_parameters` and
   `extra_swap_hook_implementation`, so the pool model independently returns `UNSUPPORTED`
   for an unadmitted swap-flagged extra hook (`LBSource.amount_neutral_extra_swap_hooks`).
6. **Envelope-complete bins.** Starting from the active bin, the tree is walked with
   `getNextNonEmptyBin` and every member read with `getBin`. The walk is driven by the
   migrated simulator: for each direction the largest reference case is quoted against
   the collected state; while it returns `incomplete_snapshot`, `walk_chunk_bins` (16) more
   members are walked on that side; once it fits — or the walk reached the end of the id
   space (the `0` / `2²⁴−1` sentinels), which proves real exhaustion — `margin_bins` (4)
   more are read past the bin the swap ends in. More than `max_bins_per_direction` (2000)
   on a side is `incomplete_snapshot`: publication is refused rather than the envelope
   truncated. Rounds batch one walk step of every unfinished (pair, side). The walked
   range is `bin_range`; every tree member inside it is in `bins`, so an id inside the
   range that is absent is known empty, and an id outside is unknown. The collected bins
   must not exceed the reserves, and over a whole-tree range must sum to exactly
   `getReserves()` (the loader re-checks the latter).
7. **Admission.** Every case × pair of its token pair is quoted; when it fills, a reverse
   swap of half its output is quoted through the returned next state (the sequential-state
   gate). Anything but `ok` / `insufficient_liquidity` / `insufficient_output_amount`
   refuses publication.
8. **Publish** through `snapshot.bundle.write_bundle` (temp dir, self-validating round
   trip, atomic rename) with a deterministic `provenance.json`.

Bundle record (`"family": "liquidity_book"`, `snapshot/bundle.py`): pool id, source key,
tokens (X, Y — not address-sorted), bin step, block timestamp, active id, reserves,
protocol fees, static / variable fee parameters, `bin_range`, `bins` (`[{id, x, y}]`), the
raw hooks and extra-hooks words with the resolved implementations, and `read_at`. The
loader rejects another block, another timestamp, an active id or bin outside the range, an
empty or duplicated bin, a field wider than its packed slot and a whole-tree range that
does not account for the reserves.

## 2. The bundle at block 101057678

`tests/fixtures/moe_lb/bundle/` (`moe-lb-101057678-a2195dd2`), published with

```bash
uv run python main.py prepare --source moe_lb --block 101057678 \
  --block-hash 0xa2195dd2b8bf76e093509e7cdf013223ee2127b002be568284f989811a339354 \
  --output tests/fixtures/moe_lb/bundle
```

A cold run takes about half an hour on the public endpoint (the tree walk is sequential per side);
a re-run from the RPC cache takes seconds and reproduces the bundle byte for byte.

25 LB pairs among USDT/USDC/WMNT/WETH/mETH (USDC/mETH and USDC/WETH have none — recorded
omissions); 16 have an empty book at the block and are collected over the whole id range
with no bins. The nine with liquidity:

| Pair | X/Y | binStep | Swap hook | Active id | `bin_range` | Bins | Walked below / above |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `0xf6c9020c…` (catalog example) | WMNT/USDT | 15 | rewarder + extra | 8369897 | [8369833, 8370061] | 229 | 64 / 164 |
| `0x365722f1…` | WMNT/USDT | 25 | — | 8377378 | [0, 8377956] | 342 | 85 (end) / 256 |
| `0x1606c79b…` | WMNT/WETH | 10 | rewarder | 8380309 | [8380245, 8380405] | 161 | 64 / 96 |
| `0xf59c79b9…` | WMNT/mETH | 10 | rewarder + extra | 8380217 | [8380185, 8380328] | 129 | 32 / 96 |
| `0x96069f55…` | WMNT/mETH | 25 | — | 8386690 | [0, 2²⁴−1] | 149 | 0 (end) / 148 (end) |
| `0x3b6c029e…` | mETH/WETH | 2 | rewarder + extra | 8389083 | [8389067, 8389099] | 33 | 16 / 16 |
| `0xa15c851a…` | WETH/USDT | 10 | rewarder + extra | 8368848 | [0, 8369300] | 522 | 69 (end) / 452 |
| `0x3f004760…` | mETH/USDT | 10 | rewarder | 8368942 | [8368718, 8369351] | 609 | 224 / 384 |
| `0x48c1a89a…` | USDC/USDT | 1 | rewarder + extra | 8388612 | [8388596, 8388628] | 33 | 16 / 16 |

Seven pairs run an `LBHooksRewarder` on `beforeSwap`; five of those forward to an
`LBHooksExtraRewarder`. 47 reference cases: small/medium/large both ways per token pair with
an LB pair (large ones cross up to 449 bins and exhaust the shallower siblings, proven by
walking those to the end of the id space), a small case each way on USDC/WMNT (both pairs
empty), `weth_usdt_exhaust` (more WETH than the whole USDT side of the book absorbs), and
two dust cases (`weth_usdt_dust`, `usdc_usdt_dust`: `LBPair__InsufficientAmountOut`).
Admission: 150 case × pair quotes (47 `ok`, each with an `ok` sequential follow-up; 101
`insufficient_liquidity`; 2 `insufficient_output_amount`). `direct` on the bundle: 42 `ok`,
5 `no_route` (the two empty-book cases, the exhaustion case and the two dust cases).

## 3. Independent fork evidence

`tools/cl_evidence/test/CaptureMoeLBReplay.t.sol` (run by
`tools/cl_evidence/replay_moe_lb.sh <bundle>`, ~9 min on a warm Foundry cache; requests
from `make_lb_replay_requests.py`; output `tests/fixtures/moe_lb/evidence.jsonl.gz`,
160 KB) forks Mantle at the bundle's block (refusing another chain id or block hash) and,
with no LB formula of its own:

- records each pair's fork-side state — identity, decimals, active id, fee parameters,
  reserves, protocol fees, balances, the hooks word, the rewarder's extra-hooks word and
  both hook implementations with code hashes — and **re-walks the bin tree** inside the
  bundle's `bin_range` with `getNextNonEmptyBin` / `getBin`, plus the first member (or
  sentinel) past each end of the range;
- per (case × pair), from the pristine state: `getSwapOut`, the executed exact-input swap
  (input transferred, crediting recorded; received output or revert data), every per-bin
  `Swap` event, the storage reads of the live hook and extra hook, and the post-swap state
  (active id, variable fees, reserves, protocol fees, each touched bin);
- a **follow-up reverse swap** of half the output from that post-state, recorded the same
  way.

Result at block 101057678: 25 pair states and 2,207 bins identical to the bundle; every
range end is followed by a member outside the range (or the sentinel); 150 swaps (47
executed, 101 `LBPair__OutOfLiquidity`, 2 `LBPair__InsufficientAmountOut`) + 47 follow-ups,
3,523 per-bin events; every transfer credited exactly the amount sent; the live rewarder
and extra rewarder read their storage in every hooked swap. `tests/snapshot/test_moe_lb.py`
replays all of it offline from the bundle alone: outputs, per-bin events, next states (as
the start of the follow-up) and failure statuses match exactly, and both directions of
every pair holding more than dust of both tokens are exercised.

## 4. Completeness and exclusions

The envelope is the prepare config's reference cases; the collector proves each fits the
collected range (or is real exhaustion over a range reaching the id-space end). Nothing was
excluded at this block. A pair whose envelope needs more than `max_bins_per_direction` on a
side cannot be published silently truncated: it needs an `excluded_bin_steps` entry with a
written reason, which lands in `provenance.discovery.omitted`. Scope limits: the cases are
hand-chosen admission cases, not the stratified corpus; the bundle is at the provisional
catalog block (WHI-1436 re-runs the collector at the final common block, where envelope
size, and thus walk length, may differ).

## 5. Tests

`uv run pytest tests/snapshot/test_moe_lb.py` (39 tests, < 3 s, offline): catalog/simulator
hook-list agreement and clone/CREATE2 derivation; the published bundle, provenance, hooks
and admission; the fork evidence (state, bins, swaps, follow-ups); the pool model's
`UNSUPPORTED` for an unadmitted extra hook; loader and prepare-config rejections; the
collector against a scripted node (reproduces the fixture bundle byte for byte from pinned
reads; rejects a foreign factory/implementation, a clone of another implementation /
swapped args / a Classic clone, a broken `getLBPairInformation` round trip or getter, an
unapproved swap hook, a flag without a hook, an unapproved, unbound or tampered extra hook,
balance drift, wrong decimals, a truncated envelope and a reorg; records reasoned
exclusions); and `validate` + `run` with sockets disabled.

## 6. SOR capability

`moe_lb_v2_2` has no `sor_protocol` in the catalog (Uniswap SOR routes V2/V3 pools only,
`uni-sor-port-contract.md` §2); the bundle's provenance records
`source_capability {protocol_family: liquidity_book_v2, sor_protocol: null}`, so LB pools
never enter the matched SOR cohort and an LB-only case is `unsupported` for SOR there.

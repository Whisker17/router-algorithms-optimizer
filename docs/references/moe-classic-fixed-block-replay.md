# Merchant Moe Classic v1 fixed-block snapshot and offline direct replay (WHI-1432 / I07)

Status: **Merchant Moe Classic v1 is admitted for fixed-block collection.** `main.py
prepare --source moe_classic --block <n>` captures verified Moe Classic pairs at one
finalized block (`snapshot/collectors/classic.py`, selection `config/prepare/
moe_classic.yaml`); `main.py run` replays `direct` on them offline. Every swap, in both
directions, and a sequential second swap from each post-state agree exactly with fork
execution of the deployed `MoePair` clones and the explorer-verified `MoeRouter`; dust
and uint112 overflow fail exactly where the pair reverts. The fee is the pair's own
hardcoded constant, read from the source (§1), not an assumed Uniswap-V2 default. Moe
Classic is an SOR `V2` source; every captured pair is in the full cohort and the SOR V2
cohort (§5).

## 1. Source → Python migration

Source: `merchant-moe/moe-core@460bf55b8330f31d31d356634d0129fd78b4fd07`,
`src/dex/MoePair.sol` / `MoeFactory.sol` / `libraries/MoeLibrary.sol` (GPL-3.0),
forge-rebuilt byte-identical outside immutables and metadata by WHI-1426
(`protocol-admission.md` §3.4). `src/dex/` is unchanged between `460bf55` and the repo's
later `main` (`git diff 460bf55 HEAD -- src/dex` is empty), so there is one source text.

| `MoePair.sol` @ 460bf55 | Python (`pools/constant_product.py`, `SOURCES["moe_classic_v1"]`) |
| --- | --- |
| `swap`: `balance{0,1}Adjusted = balance * 1000 - amountIn * 3`; `require(adj0 * adj1 >= r0 * r1 * 1000**2, "Moe: K")` | `fee_bps = 30`, fixed. There is **no per-pair fee** (no storage slot, no setter) and **no swap-time protocol fee**: `MoeFactory.feeTo` (non-zero at the block, `0x03183940…`) only mints LP shares inside `mint`/`burn` (`_sendFee`, 1/6 of √k growth). |
| (exact input) the largest `amountOut` passing `K` | solving `(1000 rIn + 997 in)(rOut − out) ≥ 1000 rIn rOut` gives `out ≤ 997·in·rOut / (1000 rIn + 997 in)`, i.e. exactly `MoeLibrary.getAmountOut`'s floor. `fee_bps=30` scales numerator and denominator by 10, same quotient. |
| `require(amount0Out > 0 \|\| amount1Out > 0, "Moe: INSUFFICIENT_OUTPUT_AMOUNT")` | an output that floors to 0 → `insufficient_output_amount` |
| `require(amountOut < reserveOut, "Moe: INSUFFICIENT_LIQUIDITY")` | `amount_out >= reserve_out` → `insufficient_liquidity` (unreachable for fee > 0) |
| `_update`: `require(balance ≤ type(uint112).max, "Moe: OVERFLOW")`; `reserve = uint112(balance)` | post-swap `reserve_in + in ≥ 2**112` → `reverted`; else next state `(reserve_in + in, reserve_out − out)` |
| pair re-reads `balanceOf(this)` | modeled as reserve + transfer: the collector requires balance == reserve at the block, and only declared standard tokens are collected (fork evidence: transfers credit exactly `in`) |
| `token0()/token1()` = clone immutable args, sorted by `createPair` | every sourced record must have `token0 < token1` (`snapshot.bundle`) |
| checked arithmetic | cannot overflow before `_update` for any admitted input (reserves < 2**112, widest product < 2**246) |

The source-free generic formula (synthetic suite, `source_key: None`) is unchanged; the
source-specific checks apply only to a state whose `source_key` names a migrated source,
and an unknown `source_key` or a `fee_bps` other than the source's is `unsupported`.
`snapshot.config` carries the catalog fact (`classic_collection.swap_fee_bps: 30`); the
collector refuses to run if it differs from the simulator's constant.

## 2. Collector (`snapshot/collectors/classic.py`)

Block identity and reads come from `snapshot/collectors/fixed_block.py`
(`FixedBlockReader`, extracted unchanged from the CL collector): chain id, block number
→ hash (optionally `--block-hash`), ≤ finalized head, every read EIP-1898 pinned to the
hash with `requireCanonical`, header re-read by number and hash before publishing.

1. **Deployment.** `MoeFactory` and the pair implementation `0x08477e01…c28B` code hashes
   equal the catalog pins; `factory.moePairImplementation()` is that implementation.
2. **Discovery.** `factory.getPair` in both argument orders over the configured token
   pairs (must agree); zero → recorded omission; a configured `excluded` → reasoned
   omission.
3. **Clone identity.** A Moe Classic pair is a 95-byte `ImmutableClone`
   (`lib/dexv2 src/libraries/ImmutableClone.sol`): `363d3d373d3d3d3d61 | extra | 806035363936013d73 | implementation | 5af43d3d93803e603357fd5bf3 | token0 ++ token1 | extra`.
   The collector rebuilds that exact runtime from the pinned implementation and the
   pair's sorted tokens and requires byte equality — so the delegatecall target **and**
   the token order are verified from the bytecode — and requires the address to equal the
   CREATE2 address `MoeFactory.createPair` deploys to (salt `keccak256(token0 ++ token1)`,
   init code `61 runSize 3d81600a3d39f3 | runtime`). The getters `token0/token1/factory/
   implementation` must agree. *Catalog correction:* WHI-1426 stated every Moe Classic
   pool shares the example pool's code hash; it does not (the tokens are in the code) —
   `config/protocols.yaml` now says so.
4. **State and token behaviour.** `getReserves()` (uint112/uint112/uint32 bounds) and
   both `balanceOf(pair)`: balance ≠ reserve (pending donation, rebasing or other
   non-standard token) → `unsupported_token_behavior`. Each token's `decimals()` must
   equal the prepare config's declaration (recorded in provenance `tokens`, e.g. for the
   SOR `Token` objects); only declared tokens can form a pair.
5. **Admission.** Every case × pair of its token pair must quote `ok` or the pair's own
   dust refusal `insufficient_output_amount`.

## 3. The bundle at block 101057678

`tests/fixtures/moe_classic/bundle/` (`moe-classic-101057678-a2195dd2`), published with

```bash
uv run python main.py prepare --source moe_classic --block 101057678 \
  --block-hash 0xa2195dd2b8bf76e093509e7cdf013223ee2127b002be568284f989811a339354 \
  --output tests/fixtures/moe_classic/bundle
```

All ten pairs of USDT/USDC/WMNT/WETH/mETH exist and are admitted (no omissions). They are
small (USDT/WMNT, the catalog example pair: 277.2 USDT / 416.5 WMNT; the deepest,
USDT/mETH: 1,244 USDT / 0.426 mETH). 62 reference cases: small/medium/large both ways
per pair (large sized to a large fraction of reserves) plus two dust cases
(`weth_usdt_dust`, `usdc_usdt_dust`: 1 raw unit floors to zero output → admitted as
`insufficient_output_amount`, `direct` returns `no_route`).

## 4. Independent fork evidence

`tools/cl_evidence/test/CaptureMoeClassicReplay.t.sol` (run by
`tools/cl_evidence/replay_moe_classic.sh <bundle>`, ~40 s; output
`tests/fixtures/moe_classic/evidence.jsonl.gz`, 9 KB) forks Mantle at the bundle's block
(refusing another chain id or block hash) and, with no swap formula of its own:

- records each pair's fork-side state (tokens, decimals, factory, implementation, code
  hash, reserves, balances);
- per (case × pair): `MoeRouter.getAmountsOut` (the deployed periphery's quote path),
  transfers the input to the pair (recording how much its balance grew), asks the pair
  for **one wei more** than the quote (must revert `Moe: K` — the quote is the pair's
  exact maximum), then executes the quote and records what the recipient received and
  the post-swap reserves/balances;
- a **follow-up reverse swap** of half the output from that post-state, same procedure;
- uint112 overflow probes on the example pair in both directions: the input leaving the
  reserve at exactly `type(uint112).max` executes; one more reverts `Moe: OVERFLOW`.

Result at block 101057678: 62 swaps (60 executed, 2 dust) + 60 follow-ups; all 122
`quote + 1` probes revert `Moe: K` (including the dust swaps' 1-wei probe); every transfer credited exactly the amount sent; the two dust
swaps revert `Moe: INSUFFICIENT_OUTPUT_AMOUNT`; the overflow probes behave as above.
`tests/snapshot/test_moe_classic.py` replays all of it offline against
`pools.constant_product.quote_exact_in`: outputs, next states (as the starting state of
the follow-up) and failure statuses match exactly, and every pair is exercised in both
directions.

## 5. Cohorts and SOR capability

`config/protocols.yaml` `moe_classic_v1.sor_protocol: V2` (catalog loader now rejects a
`V2` marking on a non-`v2_classic` source and `V3` on a non-CL one). The bundle's
provenance carries `source_capability {protocol_family: v2_classic, sor_protocol: V2}`
and every pool record carries `source_key`, so cohort selection needs only the bundle and
`ProtocolCatalog.sor_protocols()`: all ten pairs are in the full cohort and the SOR V2
cohort, with one pair per token pair (SOR's `v2PoolProvider` identity model). SOR quotes
for V2 routes come from this simulator (uni-sor-port-contract.md A-2).

## 6. Rejections covered by tests

- **Wrong source**: a non-`v2_classic` or unknown catalog key, an unadmitted entry, a
  catalog fee that disagrees with the simulator, a foreign factory, an implementation
  mismatch, pair code that is a clone of another implementation / an LB-style clone /
  full bytecode, correct code at a non-CREATE2 address, asymmetric `getPair`, a
  `source_key`/`fee_bps` the simulator does not know (quote `unsupported`, loader
  `BundleError`).
- **Wrong token order**: swapped clone args, getters disagreeing with the clone, an
  unsorted sourced bundle record.
- **Unsupported token behaviour**: balance ≠ reserve, `decimals()` ≠ declaration, an
  undeclared token in the prepare config, a token the pair does not hold
  (`unsupported_token`).
- Mixed blocks (`read_at`), reserves beyond uint112, a sourced record without `read_at`,
  a block hash that changes during collection.

## 7. Out of scope

Merchant Moe LB (WHI-1433), fee-on-transfer/rebasing tokens, flash swaps (`moeCall`),
Exact Output (`getAmountIn`), the final five-source corpus block (WHI-1436).

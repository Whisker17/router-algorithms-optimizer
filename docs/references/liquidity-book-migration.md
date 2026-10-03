# Liquidity Book v2.2 migration map (WHI-1433 / I08)

Status: **`pools/liquidity_book.py` implements Merchant Moe LB v2.2 `LBPair.swap` (exact
input, both directions, multi-bin traversal, evolving volatility-accumulator fee state)
and is verified offline against fork-simulation evidence of the deployed contracts.**
Real bin-snapshot collection, bundle serialization and fixed-block admission of LB pools
landed with WHI-1434 (I09): `docs/references/moe-lb-fixed-block-replay.md`.

This is the Solidity-to-Python record DESIGN §2.3 requires: the deployed code that was
migrated, every function/storage field on the swap path and its Python counterpart,
every exclusion and why, and the independent evidence.

## 1. Pinned source

| | |
| --- | --- |
| LBFactory | `0xa6630671775c4EA2743840F9A5016dCf2A104054` (code hash pinned in `config/protocols.yaml`) |
| LBPair implementation | `0xf6863Db7323aaC43fE8AEF0B3eF63AA6b32DDB3B` (every pair is an EIP-1167-style clone of it) |
| Upstream | `lfj-gg/joe-v2` tag `v2.2.0`, commit `1297c3822f0605e643155c35948959c0a0d05e17`, MIT |
| Build | solc 0.8.20, optimizer **runs=300** for LBPair (runs=800 for LBFactory) |

Source identity is established at bytecode level by WHI-1426
(`docs/references/protocol-admission.md` §3.5): a local `forge build` of the pinned tag
reproduces the on-chain LBFactory byte-for-byte and the LBPair implementation
byte-for-byte outside its two immutables and the metadata hash. The Python therefore
migrates the files of that tag, not a re-derived formula. The evidence (§5) re-checks
the factory and implementation code hashes against the catalog pin.

## 2. Solidity → Python map (swap path)

Widths: every `uint128`/`uint256` value is a Python `int` checked at the same points
the Solidity checks it. `unchecked` blocks are reproduced by masking (`& UINT128_MAX`,
`& UINT256_MAX`); checked arithmetic raises `LBRevert("Panic(0x11)")`; each custom
error raises `LBRevert(<error name>)` so its 4-byte selector can be compared with the
fork's revert data.

| Solidity (`src/…` at v2.2.0) | Python (`pools/liquidity_book.py`) | Notes |
| --- | --- | --- |
| `LBPair.swap(bool swapForY, address to)` | `swap(state, swap_for_y, amount_in)` | Line-for-line loop: `receivedX/Y` → `InsufficientAmountIn`; `Hooks.beforeSwap`; `reserves.add`; `updateReferences(block.timestamp)`; per bin `isEmpty(!swapForY)` → `updateVolatilityAccumulator` → `getAmounts` → protocol-fee split → bin write; `_getNextNonEmptyBin`; `nextId == 0 \|\| nextId == type(uint24).max` → `OutOfLiquidity`; `amountsOut == 0` → `InsufficientAmountOut`; `_reserves.sub(amountsOut)`; `setActiveId`. |
| `TokenHelper.received{X,Y}` / `SafeCast.safe128` | `swap`: `_safe(reserve_in + amount_in, 128) - reserve_in` | The pair's balance before the transfer equals `_reserves` (which *includes* `_protocolFees`). |
| `LBPair._getNextNonEmptyBin` → `TreeMath.findFirstRight` / `findFirstLeft` | `next_non_empty_bin` | The 3-level bitmap returns the nearest tree member strictly below (`swapForY`) / above the id, or the sentinel `type(uint24).max` / `0`. Python bisects the sorted tree membership (the keys of `state.bins`), `O(log n)` per step. Swaps never change tree membership (only mint/burn do): a traversed bin keeps the input token. |
| `PairParameterHelper` getters/setters, `Encoded.set/decode` | `_get`, `_set`, `encode_parameters`, `decode_variable` | The packed `_parameters` word is rebuilt from `getStaticFeeParameters`/`getVariableFeeParameters` + `getActiveId` with the same offsets/masks. |
| `PairParameterHelper.updateReferences` (+ `updateIdReference`, `updateVolatilityReference`, `setVolatilityReference`, `updateTimeOfLastUpdate`/`safe40`) | `update_references` | Checked `timestamp - timeOfLastUpdate`; `dt >= filterPeriod` moves `idReference`; `dt < decayPeriod` decays `volRef = uint24(volAcc * reductionFactor / 10_000)`, else resets to 0; always stamps the time. |
| `PairParameterHelper.updateVolatilityAccumulator` | `update_volatility_accumulator` | `volRef + \|activeId − idRef\| * 10_000`, capped at `maxVolatilityAccumulator`; runs for **every** non-empty bin, so the fee rises while the swap crosses bins. |
| `PairParameterHelper.getBaseFee` / `getVariableFee` / `getTotalFee` | `get_base_fee`, `get_variable_fee`, `get_total_fee` | `baseFactor * binStep * 1e10`; `ceil((volAcc * binStep)² * variableFeeControl / 100)`; sum `safe128`. |
| `FeeHelper.getFeeAmountFrom` / `getFeeAmount` / `verifyFee` | `get_fee_amount_from`, `get_fee_amount`, `_verify_fee` | Fee included in / on top of the amount, both rounded up, `uint128` truncation, `FeeTooLarge` above 10%. |
| `BinHelper.getAmounts` | `get_amounts` | `maxAmountIn` rounded up (`shiftDivRoundUp`/`mulShiftRoundUp`), `amountIn >= maxAmountIn` drains the bin exactly; else output rounded down and capped at the bin reserve; `MaxLiquidityPerBinExceeded` check on the post-swap bin. |
| `BinHelper.getLiquidity` | `get_liquidity` | `price * x + (y << 128)` with `LiquidityOverflow` checks. |
| `PriceHelper.getBase` / `getPriceFromId` | `get_base`, `get_price_from_id` | `(1 + binStep/1e4)` in 128.128, raised to `id − 2²³`. |
| `Uint128x128Math.pow` | `pow128` | Assembly semantics: wrapping 256-bit products, 20-bit exponent bound, inversion for `x > uint128.max` and negative exponents, `PowUnderflow`. |
| `Uint256x256Math.mulShiftRound{Down,Up}`, `shiftDivRound{Down,Up}` (`_getMulProds`, `_getEndOfDivRoundDown`) | `mul_shift_round_down/up`, `shift_div_round_down/up` | 512-bit intermediate via Python ints; the high-word overflow reverts are reproduced. |
| `PackedUint128Math.encode/decode/add/sub/scalarMulDivBasisPointRoundDown` | `_encode`, `_decode`, `_packed_add`, `_packed_sub`, `_scalar_mul_div_basis_point_round_down` | Protocol fee = `totalFees * protocolShare / 10_000` per side, rounded down, removed from the bin's credited input. |
| `Constants` (`SCALE_OFFSET`, `PRECISION`, `BASIS_POINT_MAX`, `MAX_LIQUIDITY_PER_BIN`, `REAL_ID_SHIFT`), `FeeHelper`/`PairParameterHelper` bounds | module constants | `MAX_LIQUIDITY_PER_BIN` and prices re-derived in `test_price_and_constants_match_their_definitions`. |
| `Hooks.beforeSwap` / `Hooks.afterSwap` | `_swap_hook_calls` | See §3. |

### Storage fields

| `LBPair` storage | `LiquidityBookPoolState` field | Updated by `swap` |
| --- | --- | --- |
| `_parameters` static half (`baseFactor` … `maxVolatilityAccumulator`) | `static_fee: LBStaticFeeParameters` | no |
| `_parameters` `volatilityAccumulator`, `volatilityReference`, `idReference`, `timeOfLastUpdate` | `variable_fee: LBVariableFeeParameters` | yes (`new_state.variable_fee`) |
| `_parameters` `activeId` | `active_id` | yes |
| `_parameters` `oracleId` | — | excluded (§4) |
| `_reserves` | `reserve_x/y` (= `getReserves()`, i.e. minus protocol fees) + `protocol_fee_x/y` | yes |
| `_protocolFees` | `protocol_fee_x/y` | yes |
| `_bins[id]` | `bins[id] = (x, y)` | yes, only traversed bins; others shared unchanged |
| `_tree` | keys of `bins` within `bin_range` | no (swap never changes membership) |
| `_hooksParameters` | `hooks_parameters` (+ collector-resolved `swap_hook_implementation`) | no |
| rewarder `_extraHooksParameters` (hook storage, WHI-1434) | `extra_hooks_parameters` (+ collector-resolved `extra_swap_hook_implementation`) | no |
| immutables `tokenX`, `tokenY`, `binStep` | `token0`, `token1`, `bin_step` | no |
| `block.timestamp` | `block_timestamp` (frozen per snapshot) | no |

`swap` never mutates its input: bins are a read-only mapping, the result is a
`dataclasses.replace` copy whose `bins` is a new mapping only when a bin changed.
Repeated use of the same pair within a plan works because the evaluator threads each
step's `new_state` into the next step (`routing/evaluator.py`).

## 3. Swap hooks

Three of the four real pairs carry an `LBHooksRewarder` clone (implementation
`0xDc0e38cBD08fa532847BAECbF26c8b09eD9008A7`, Routescan-verified `LBHooksRewarder`,
solc 0.8.20, runs 600) with the `beforeSwap` flag. For the quote this is output-neutral
by construction of `LBPair.swap`: `amountsLeft` is read *before* `Hooks.beforeSwap`,
the loop reads only pair storage, every state-changing pair entry point is
`nonReentrant` (a hook that re-enters reverts the whole swap), and `afterSwap` runs
after every write. The rewarder's `_beforeSwap` only calls
`_updateAccruedRewardsPerShare` (its own storage and `MasterChef`) and then forwards to
its `_extraHooksParameters`. At the fixture block two of the three rewarders forward to
an `LBHooksExtraRewarder` clone (implementation
`0x2D4BF9f668e5B7C7fE33c8F116ae190669304676`, Routescan-verified, same accounting-only
`_beforeSwap`); the fork evidence executes both live.

What a hook *can* do is revert. The model therefore simulates a swap hook only when the
clone's implementation is in `SOURCES["moe_lb_v2_2"].amount_neutral_swap_hooks`; any
other swap-flagged hook (or a flag with a zero hooks address) is `UNSUPPORTED`. The
extra-rewarder hop lives in rewarder storage, not pair state: since WHI-1434 the collector
reads the rewarder's `getExtraHooksParameters()`, admits it only against the catalog's
approved `lb_collection.extra_swap_hooks` (pinned implementation code hash, clone bound to
the pair and rewarder) and stores it in the state, and the model returns `UNSUPPORTED`
unless a swap-flagged extra hook's implementation is in
`SOURCES["moe_lb_v2_2"].amount_neutral_extra_swap_hooks` (the deferred item is resolved,
`docs/DEFERRED_ISSUES.md`).

## 4. Exclusions (not migrated)

| Solidity | Why excluded |
| --- | --- |
| `OracleHelper.update` (`_oracle`, `oracleId`) | Runs after the swap loop and writes only oracle samples and `oracleId`; never read by swap math. |
| `mint`, `burn`, `collectProtocolFees`, `increaseOracleLength`, `setStaticFeeParameters`, `setHooksParameters`, `forceDecay` | Not on the exact-input quote path. |
| `flashLoan` | Not a swap. |
| `getSwapIn` / `getSwapOut` views | Periphery-style estimates; the evidence records `getSwapOut` only as a contract-side cross-check of the executed swap. |
| Composition fees | Charged by `mint` only. |
| Token transfers (`TokenHelper.safeTransfer`) | The quote is the pair's computed output; token transfer semantics are out of domain (fee-on-transfer tokens are not admitted). |

## 5. Independent evidence

`tools/cl_evidence/test/CaptureLB.t.sol` (run by `tools/cl_evidence/capture_lb.sh`)
forks Mantle at the catalog candidate block `101057678`, checks the factory and pair
implementation code hashes, and records for each scenario the pair identity, the whole
bin tree (walked with `getNextNonEmptyBin` to both sentinels), every bin's `getBin`,
the packed fee state and reserves; then for each swap sequence (each from the pristine
state) the `getSwapOut` view, the executed result (received output, or revert data),
every per-bin `Swap` event (id, amounts, volatility accumulator, total and protocol
fees), the hook's storage accesses, and the post-swap scalars plus every bin whose
reserves changed. No LB formula appears in the Solidity harness; inputs are sized with
the pair's own views.

| File | Pair | binStep | Tree bins | Swaps (ok) | Max bins in one swap |
| --- | --- | --- | --- | --- | --- |
| `real_wmnt_usdt_15` | `0xf6C9020c…aE2415` (catalog example, hooked + extra rewarder) | 15 | 3081 | 20 (16) | 1346 |
| `real_wmnt_usdt_25` | `0x365722f1…B7C00F` (no hooks) | 25 | 463 | 20 (16) | 210 |
| `real_usdc_usdt_1` | `0x48C1A89a…CddFEc` (hooked + extra rewarder) | 1 | 176 | 20 (16) | 87 |
| `real_weth_wmnt_10` | `0x1606C79b…CfBefA2` (hooked, no extra rewarder; enters the swap in the decay branch) | 10 | 1398 | 20 (16) | 253 |
| `controlled` | 4 pairs created through the deployed LBFactory with mock tokens: sparse bins across tree words and a level-1 boundary; dense bins with max protocol share and `vm.warp` over every `updateReferences` branch and edge; irregular reserves with exact drains, drain−1 and a dust grid; a bin at `MAX_LIQUIDITY_PER_BIN` | 1/10/25/1 | 124 | 68 (63) | 13 |

Real sequences: both directions, round trips, the same input twice at one timestamp
(repeated use), small swaps, exact active-bin drains and drain−1, whole-book swaps and
book exhaustion in both directions. Reverts covered: `LBPair__OutOfLiquidity`,
`LBPair__InsufficientAmountOut`, `BinHelper__MaxLiquidityPerBinExceeded`.

`tests/pools/test_liquidity_book.py` replays every swap and asserts the output, every
per-bin event tuple, the next active id, variable fee parameters, reserves, protocol
fees and **every** bin of the next state against the fork, through both `swap` and
`quote_exact_in`, plus the evaluator's repeated-use threading. The SafeCast/clock
underflow reverts that real tokens cannot reach are tested directly.

Regenerate (network, Foundry): `tools/cl_evidence/capture_lb.sh`. The first run
fetches every bin slot through the public RPC; Foundry caches the fork state.

## 6. Incomplete snapshot vs real exhaustion

`bin_range` is the inclusive id range over which the tree was fully walked; `bins`
holds every tree member inside it. `quote_exact_in` maps:

- `LBPair__OutOfLiquidity` over a range reaching the id-space end on the walked side
  (`0` for X→Y, `2²⁴−1` for Y→X) → `INSUFFICIENT_LIQUIDITY` (real exhaustion);
- a traversal leaving a narrower range, an active id outside it, or missing
  static/variable fee parameters → `INCOMPLETE_SNAPSHOT` (`MissingState`);
- `LBPair__InsufficientAmountOut` → `INSUFFICIENT_OUTPUT_AMOUNT`; any other revert →
  `REVERTED`; unknown source or unadmitted swap hook → `UNSUPPORTED`.

An LB swap is all-or-nothing on-chain (no partial fill), so a non-OK status never
returns a partial amount.

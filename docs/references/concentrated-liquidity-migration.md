# Concentrated-liquidity migration map (WHI-1428 / I03)

Status: **`pools/concentrated.py` + `pools/cl_math.py` implement Exact Input swaps and
state transitions for `uniswap_v3`, `agni_v3` and `fusionx_v3`; verified offline
against fork-simulation evidence of the deployed contracts.** Source-specific live
admission (real collectors, bundle format, fixed-block replay): Agni done by WHI-1429
(`docs/references/agni-fixed-block-replay.md`), FusionX by WHI-1430
(`docs/references/fusionx-fixed-block-replay.md`), Uniswap v3 by WHI-1431
(`docs/references/uniswap-v3-fixed-block-replay.md`, including fork-verified partial
fills on real pools).

This document is the Solidity-to-Python record DESIGN §2.3 requires: which deployed
code was migrated, how the three V3-family deployments differ, every Solidity
function on the admitted Exact Input path and its Python counterpart (integer widths,
signedness, wrap/revert behavior), every omitted branch and why, and the independent
evidence that verifies the migration.

## 1. Pinned sources

| Source key | Deployed pool (example) | Verified source | Upstream pin | License |
| --- | --- | --- | --- | --- |
| `uniswap_v3` | `0x4cdFc22b…631d2b` (`UniswapV3Pool`, solc 0.7.6, runs 800) | Routescan, 31 files | `Uniswap/v3-core` v1.0.0 `e3589b1` | GPL-2.0-or-later (BUSL-1.1 change date passed); FullMath MIT |
| `agni_v3` | `0x1858d52c…17144f` (`AgniPool`, solc 0.7.6, runs 200) | Routescan, 31 files | `agni-protocol/contracts@7278c3a` | GPL-2.0-or-later |
| `fusionx_v3` | `0x262255f4…2a0aff` (`FusionXV3Pool`, solc 0.7.6, runs 20) | Routescan, one hardhat-flattened file | `FusionX-Finance/v3-contracts@7f7406e` | GPL-2.0-or-later |

Code hashes and addresses are the ones pinned in `config/protocols.yaml` (WHI-1426);
the real-pool evidence re-checks each example pool's `codehash` against that pin.

## 2. Deployed swap-path diff (done before sharing code)

Reproduce with `tools/cl_evidence/diff_sources.sh` (fetches the three verified
sources from Routescan and v3-core v1.0.0 from GitHub; compares with comments,
whitespace, `pragma` and `import` lines removed, and with fork naming neutralized).
Result on 2026-09-24:

1. **Uniswap verified source vs `v3-core` v1.0.0: no semantic difference** in any of the
   31 files (only the `pragma … <0.8.0` bound and prettier line wrapping differ).
2. **Libraries** (`BitMath, FixedPoint96, FixedPoint128, FullMath, LiquidityMath,
   LowGasSafeMath, Oracle, Position, SafeCast, SqrtPriceMath, SwapMath, Tick, TickBitmap,
   TickMath, TransferHelper, UnsafeMath`): **identical** in Agni and FusionX to Uniswap's.
3. **`AgniPool` vs `FusionXV3Pool`: identical** after renaming (`Agni*` ↔ `FusionXV3*`).
4. **`AgniPool`/`FusionXV3Pool` vs `UniswapV3Pool`** — the complete list of semantic
   differences, and what each means for Exact Input simulation:

| # | Difference | Swap-relevant? | Python |
| --- | --- | --- | --- |
| D1 | `slot0.feeProtocol` is `uint32` (two 16-bit halves) instead of `uint8` (two 4-bit halves); swap reads `% 65536` / `>> 16` instead of `% 16` / `>> 4` | **yes** | `ProtocolFeeRule.PANCAKE_V3_RATIO` vs `UNISWAP_V3_DENOMINATOR` in `_input_fee_protocol` |
| D2 | protocol-fee share `delta = step.feeAmount.mul(feeProtocol) / 10000` (checked `mul`) instead of `step.feeAmount / feeProtocol` | **yes** (splits fee between `feeGrowthGlobal` and `protocolFees`) | `_protocol_fee_delta` (checked multiply → `SolidityRevert`) |
| D3 | `lmPool.accumulateReward(blockTimestamp)` at swap start and `lmPool.crossLmTick(tickNext, zeroForOne)` on every initialized-tick cross when `lmPool != 0` | external calls, no return value, no pool-storage write | explicit no-op, counted in `SwapOutcome.lm_pool_hook_calls` (see §5) |
| D4 | `initialize` sets a non-zero default `feeProtocol` (3200/3300/3400 per side by fee tier) | no (state input; read from `slot0`) | taken from the snapshot |
| D5 | `setFeeProtocol` range 1000–4000 and `onlyFactoryOrFactoryOwner`; `collectProtocol`/`flash` protocol split mirrors D2; `setLmPool` added | no (owner/flash paths) | not migrated |
| D6 | no `noDelegateCall` modifier; callback names `agniSwapCallback`/`fusionXV3SwapCallback`; `Swap` event carries `protocolFeesToken0/1` | no | not migrated |

Conclusion: a shared implementation is sound **only** with D1/D2 selected per source
and D3 declared per source. `pools.concentrated.SOURCES` encodes exactly that; any
other `source_key` is `UNSUPPORTED`, and a `uniswap_v3` state carrying an `lm_pool` is
`UNSUPPORTED`. `test_protocol_fee_encodings_differ_by_source` shows Agni evidence
replayed under Uniswap semantics disagrees with the contract.

## 3. Solidity → Python map (admitted Exact Input path)

Solidity 0.7.6 has no automatic overflow checks: plain `+ - *` wrap. Every wrapping
site is masked explicitly in Python; every checked site (`LowGasSafeMath`, `SafeCast`,
`require`) raises `pools.cl_math.SolidityRevert(reason)`.

| Solidity (v3-core v1.0.0 path:line) | Python | Widths / signedness | Revert / wrap behavior preserved |
| --- | --- | --- | --- |
| `FullMath.mulDiv` (`libraries/FullMath.sol:14`) | `cl_math.mul_div` | uint256 × uint256 / uint256, 512-bit intermediate | `require(denominator > 0)`, result must fit uint256 |
| `FullMath.mulDivRoundingUp` (`:113`) | `mul_div_rounding_up` | same | `require(result < type(uint256).max)` before `+1` |
| `UnsafeMath.divRoundingUp` (`UnsafeMath.sol:12`) | `div_rounding_up` | uint256 | assembly `div`/`mod` by 0 → 0 (no revert) |
| `BitMath.mostSignificantBit` / `leastSignificantBit` (`BitMath.sol:13/53`) | `most_significant_bit` / `least_significant_bit` | uint256 → uint8 | `require(x > 0)` |
| `TickMath.getSqrtRatioAtTick` (`TickMath.sol:23`) | `get_sqrt_ratio_at_tick` | int24 → uint160 | `'T'` if `|tick| > MAX_TICK`; same 20 magic constants; `type(uint256).max / ratio` for positive ticks; Q128.128→Q64.96 round-up |
| `TickMath.getTickAtSqrtRatio` (`:61`) | `get_tick_at_sqrt_ratio` | uint160 → int24 | `'R'` outside `[MIN_SQRT_RATIO, MAX_SQRT_RATIO)`; msb search, 14 squaring rounds, signed `log_2` (`or`/`sar` two's complement == Python `|`/`>>`), tickLow/tickHi tie-break |
| `SqrtPriceMath.getNextSqrtPriceFromAmount0RoundingUp(add=true)` (`SqrtPriceMath.sol:28`) | `get_next_sqrt_price_from_amount0_rounding_up_add` | uint160, uint128, uint256 | overflow *detection* of wrapping `amount * sqrtPX96` and `numerator1 + product` reproduced (selects the same branch/rounding); fallback uses checked `.add`; `uint160(...)` truncation |
| `SqrtPriceMath.getNextSqrtPriceFromAmount1RoundingDown(add=true)` (`:68`) | `get_next_sqrt_price_from_amount1_rounding_down_add` | same | `amount <= type(uint160).max` branch split; checked `.add` then `toUint160` |
| `SqrtPriceMath.getNextSqrtPriceFromInput` (`:106`) | `get_next_sqrt_price_from_input` | same | `require(sqrtPX96 > 0)`, `require(liquidity > 0)` |
| `SqrtPriceMath.getAmount0Delta(…, bool)` (`:153`) | `get_amount0_delta` | → uint256 | sorts; `require(sqrtRatioAX96 > 0)`; round-up = `divRoundingUp(mulDivRoundingUp(...))`, down = `mulDiv(...) / A` |
| `SqrtPriceMath.getAmount1Delta(…, bool)` (`:182`) | `get_amount1_delta` | → uint256 | `mulDiv[RoundingUp](liquidity, B-A, Q96)` |
| `SwapMath.computeSwapStep` (`SwapMath.sol:21`), `exactIn` branch | `compute_swap_step_exact_in` | fee in pips (`1e6`) | `amountRemainingLessFee = mulDiv(rem, 1e6-fee, 1e6)`; `max` short-circuit; fee = unchecked `rem - amountIn` when the target is not reached, else `mulDivRoundingUp(amountIn, fee, 1e6-fee)` |
| `LiquidityMath.addDelta` (`LiquidityMath.sol:10`) | `add_delta` | uint128 + int128 | uint128 wrap then `'LS'` / `'LA'` |
| `TickBitmap.nextInitializedTickWithinOneWord` (`TickBitmap.sol:42`) + `position` (`:14`) | `next_initialized_tick_within_one_word` | int24 tick, int16 word, uint8 bit | `tick / tickSpacing` with the negative-remainder decrement == floor `//`; `uint8(t % 256)` == `t & 0xff`; reads outside the collected range raise `MissingState` |
| `Tick.cross` (`Tick.sol:166`), fee-growth fields | inline in `concentrated.swap` | uint256 | `feeGrowthOutside = feeGrowthGlobal - outside` wrapping; returns `liquidityNet` |
| `SafeCast.toInt256` (`SafeCast.sol:24`) / `LowGasSafeMath.sub(int256)` (`LowGasSafeMath.sol:43`) / `.mul(uint256)` (`:27`) / `.add(uint256)` (`:11`) | `to_int256` / `checked_sub_i256` / `checked_mul_u256` / `checked_add_u256` | | revert on overflow |
| `UniswapV3Pool.swap` (`UniswapV3Pool.sol:596`); `AgniPool.swap` (`AgniPool.sol:602`); `FusionXV3Pool.swap` (flattened file `:3031`) | `concentrated.swap` | `amountSpecified` int256, limit uint160 | `'AS'` (zero amount), `'SPL'` (limit strictly inside `(MIN_SQRT_RATIO, price)` / `(price, MAX_SQRT_RATIO)`); loop condition; `MIN_TICK/MAX_TICK` clamp; limit-vs-next target choice; `amountSpecifiedRemaining -= (amountIn+fee).toInt256()`; `amountCalculated.sub(amountOut.toInt256())`; protocol-fee split (D1/D2) with `uint128` wrapping accumulation; `feeGrowthGlobal += mulDiv(fee, Q128, liquidity)` wrapping, only when `liquidity > 0`; cross on reaching `sqrtPriceNext` with the in-flight input-token growth and the storage other-token growth; `tick = zeroForOne ? tickNext - 1 : tickNext`; `getTickAtSqrtRatio` only when the price moved inside a step; final `protocolFees.tokenX += protocolFee` (uint128 wrap); `(amount0, amount1)` by `zeroForOne == exactInput` |
| `QuoterV2`/`SwapRouter` default limit (`sqrtPriceLimitX96 == 0` → `MIN+1`/`MAX-1`) | `concentrated.quote_exact_in` | | full-fill check: consumed input must equal the request |

## 4. Scoped omissions (not migrated) and why

| Omitted branch/state | Why it is outside the admitted Exact Input domain |
| --- | --- |
| Exact Output (`amountSpecified < 0`): `computeSwapStep`'s `!exactIn` branches, `getNextSqrtPriceFromOutput`, `getNextSqrtPriceFromAmount{0,1}…(add=false)` | v1 cases are Exact Input only (DESIGN §1.2). `swap` raises `ValueError` for a negative amount. |
| Oracle: `observations.observeSingle`/`write`, `slot0.observationIndex/Cardinality[Next]`; tick fields `tickCumulativeOutside`, `secondsPerLiquidityOutsideX128`, `secondsOutside` updated by `Tick.cross` | Depend on `block.timestamp` and the observation ring; never read by amount math. Not part of `TickInfo`/`ConcentratedPoolState`, so they are not claimed. |
| `slot0.unlocked` / `'LOK'` | Reentrancy lock only; a pool read at a block boundary is always unlocked (the evidence records `unlocked: true`). |
| Token transfers, `uniswapV3SwapCallback`/`agniSwapCallback`/`fusionXV3SwapCallback`, the `'IIA'` balance check, `Swap` event | Settlement, not pricing. Fee-on-transfer/rebasing token behavior is out of scope. |
| `mint`/`burn`/`collect`/`flash`/`setFeeProtocol`/`collectProtocol`/`setLmPool`/`increaseObservationCardinalityNext`, `Position`, `Tick.update`, `TickBitmap.flipTick` | Liquidity management and governance; a swap never flips bitmap bits or changes `liquidityGross/Net`. |
| `noDelegateCall` (Uniswap) | Deployment-safety modifier; no effect on the math. |
| LM-pool hook *revert* (D3) | See §5. |

## 5. LM-pool hook (Agni/FusionX)

`accumulateReward`/`crossLmTick` are external calls to a separate contract. They
return nothing and the pool writes nothing based on them, so amounts and every pool
storage field are independent of them. They are modeled as no-ops and counted. The
only possible observable effect is a revert inside the hook contract, which would make
the whole swap revert; this is not modeled. It is bounded by evidence: the FusionX
example pool has a live `lmPool` (`0x19170A0F…023107`) at the evidence block, and all ten
real-pool swaps there (including initialized-tick crossings, i.e. `crossLmTick` calls)
succeed and match. The Agni example pool has `lmPool == 0`. The shared collector records
an attached LM pool in `ConcentratedPoolState.lm_pool`, requires its code and `pool()`
back-reference, and records its code hash in provenance (WHI-1430, where the FusionX
0.05% pool's hook runs through every fixed-block replay swap).

## 6. Independent evidence

Generator: `tools/cl_evidence/test/CaptureCL.t.sol`, run by `tools/cl_evidence/regen.sh`
(`forge test` on a Mantle mainnet fork at the catalog candidate block `101057678`,
hash `0xa2195dd2…339354`; the script refuses a non-5000 chain id and records the block
hash). Nothing in the generator imports or reimplements the Python under test; the
only math it contains is a copy of `getSqrtRatioAtTick`, used solely to *choose*
`sqrtPriceLimitX96` inputs (the pool decides every recorded output). Probe swaps with a
price limit (then reverted) let the pool itself determine amounts that reach a given
tick, so boundary/multi-tick amounts are also not derived from Python.

Fixtures (`tests/fixtures/concentrated/*.jsonl`, decimal-string integers, one record per
line, terminated by `{"kind":"end"}`):

- `<source>_real.jsonl` — the catalog example pool of each source, executed as deployed
  (`pool_code_hash` equals the pinned `code_hash`). Pre-state: `slot0`, `liquidity`, both
  `feeGrowthGlobal`, `protocolFees`, bitmap words `[w-1, w+1]` around the current tick
  and every initialized tick's `ticks()` data in them. Five two-swap sequences: small
  both directions; multi-tick round trip; exact-boundary landing then continue;
  bitmap-word crossing (130+ initialized ticks crossed) then back; explicit-limit
  partial fill then back. The deployed QuoterV2 (Uniswap, FusionX) is also called for
  the two small swaps and the first multi-tick swap; its `amountOut` and
  `sqrtPriceX96After` must equal both the pool and Python.
- `<source>_controlled.jsonl` — a fresh pool created through the deployed factory on
  the fork (so the deployed pool creation code, pinned by the factory/deployer code
  hash), mock tokens, fee 500, five positions with a zero-liquidity gap, non-default
  protocol fees (Uniswap `4/7`, Agni/FusionX `1000/4000`), and the **full** bitmap range
  captured. Six sequences: small; multi-tick through a gap and a word boundary and back;
  exact boundary; insufficient liquidity both directions (partial fill at
  `MIN_SQRT_RATIO+1`/`MAX_SQRT_RATIO-1`); 1–2 wei dust (fee-only step, zero output).

After every swap the evidence records the pool scalars and exactly the ticks whose
storage changed; `tests/pools/test_concentrated.py` requires Python's amounts, scalars
and changed-tick set/values to be identical. Sequences restart from the pre-state, so
every second swap checks state threading from the first.

## 7. Incomplete snapshot vs real exhaustion

`ConcentratedPoolState.bitmap_word_range` declares which bitmap words were collected.
A traversal that needs a word outside it, or an initialized tick without `TickInfo`,
raises `MissingState` → `QuoteStatus.INCOMPLETE_SNAPSHOT`, and the input state is left
unchanged. `INSUFFICIENT_LIQUIDITY` is only returned when the swap reaches the extreme
price limit with input left over — which by construction required every word to that
bound to be collected — or when the price already sits at the bound (`'SPL'`).
`test_exhaustion_needs_full_range_otherwise_incomplete` shows the same request flips to
`INCOMPLETE_SNAPSHOT` once the lowest words are dropped from the collected range.

## 8. Residual gaps (not blockers for I03)

1. **LM hook reverts** are not modeled (§5).
2. **Agni has no deployed QuoterV2** in the catalog; its evidence is fork simulation only
   (as permitted by DESIGN §2.3), whereas Uniswap/FusionX additionally agree with their
   deployed QuoterV2.
3. **Bundle format/collectors** for CL state are I04–I06's job (Agni landed in WHI-1429;
   the CL bundle record and shared collector are reused by I05/I06); this issue provides the
   state type, the simulator and the evaluator dispatch (`pools.quote.quote_exact_in`).

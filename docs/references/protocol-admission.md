# Protocol admission catalog — findings (WHI-1426 / I01)

Status: **catalog + preflight published (code-hash pinned).** The shared
concentrated-liquidity math for §3.1–§3.3 (Uniswap v3 / Agni v3 / FusionX v3) is
migrated and verified against fork evidence by WHI-1428 — see
`docs/references/concentrated-liquidity-migration.md` (source diff, Solidity-to-Python
map, LM-hook handling, evidence). Per-source fixed-block admission: **Agni v3 is
admitted** by WHI-1429 (I04) — shared collector, pool-code normalization, published
bundle and fork evidence in `docs/references/agni-fixed-block-replay.md`; **FusionX v3
is admitted** by WHI-1430 (I05) on its own fingerprint, bundle, fork + QuoterV2
evidence and an independent local recompile (§3.3, §8) —
`docs/references/fusionx-fixed-block-replay.md`; I06–I08 are not started.
This document records what was independently verified about each of the five
DESIGN §1.2 sources on Mantle mainnet (chain id `5000`), what still cannot be
verified with the access available in this pass, and the exact Solidity→Python
migration scope each later issue (I03–I08) inherits. It is the narrative
companion to the machine-readable `config/protocols.yaml`, which
`snapshot/preflight.py` checks against on every run (including a Keccak-256
code-hash check per contract, not just code presence).

Per DESIGN §2.2/§2.3: a Dune project label or ABI resemblance is never treated as
proof of protocol identity here. Every fact below is tagged with **how** it was
established, and every unresolved item is recorded as a **blocker**, not silently
dropped or filled in with a generic AMM formula.

> **Revision note (2026-09-24, second pass).** The first pass of this document
> concluded that explorer-verified-source retrieval was blocked in this
> environment ("mantlescan now requires a paid Etherscan-V2 API key;
> explorer.mantle.xyz is down") and left Agni v3/FusionX v3 as *candidate*
> (not confirmed) PancakeSwap-family pins and Merchant Moe Classic v1 as
> `confidence: unmatched`. A supervisor review pointed at a free, keyless,
> Etherscan-compatible endpoint this pass had not tried
> (`api.routescan.io/v2/network/mainnet/evm/5000/etherscan/api`), which returns
> verified source for nearly every contract below, plus two first-party GitHub
> repositories (`merchant-moe/moe-core`, `agni-protocol/contracts`) this pass had
> not searched for. Every finding below reflects that follow-up work: three of
> five sources moved from `moderate`/`unmatched` to `high` confidence, the
> "explorer access is blocked" blocker is gone (it was a wrong conclusion, not a
> real gap), and every contract now carries an independently-recomputed
> Keccak-256 code hash that the preflight verifies on every run. Nothing below
> claims more than what was actually checked — where evidence is still
> genuinely incomplete (e.g. FusionX not independently recompiled by this
> catalog; Merchant Moe's LB Router/Quoter carrying real customizations), that
> is stated plainly rather than rounded up.

## 1. Verification hierarchy used in this pass

In order of how much weight a claim gets:

1. **On-chain differential evidence** — `eth_getCode` (presence, byte size, and
   now a pinned Keccak-256 hash) and `eth_call` round-trips (e.g. `pool.factory()`
   must equal the configured factory; `factory.getPool(token0,token1,fee)` must
   equal the configured example pool) at the fixed candidate block. This is what
   `snapshot/preflight.py` re-checks every run.
2. **Explorer-verified source**, fetched from Routescan's free, keyless,
   Etherscan-compatible API for Mantle
   (`api.routescan.io/v2/network/mainnet/evm/5000/etherscan/api`). An explorer's
   "verified" status means it independently recompiled the submitted source
   with the reported compiler/optimizer settings and confirmed the output
   matches the on-chain runtime bytecode (modulo constructor immutables) — this
   is itself a legitimate, independent code-hash-to-source proof, not merely a
   claim.
3. **A project's own first-party GitHub repository** (`agni-protocol/contracts`,
   `FusionX-Finance/v3-contracts`, `merchant-moe/moe-core`) or **current official
   documentation site**, cross-checked against (1)/(2).
4. **An independent local `forge build` bytecode comparison** against a
   candidate upstream repository, used specifically where (2) was unavailable
   (Merchant Moe Classic v1's factory/pair) or where matching compiler settings
   were not obvious from the repo's own defaults (Merchant Moe LB's `LBPair`
   implementation needed a non-default `--optimizer-runs 300`, discovered from
   (2)'s own reported metadata rather than guessed).
5. **Rejected**: search-engine snippets, GeckoTerminal/DexScreener pool listings
   *alone* (used only to discover a candidate address, then verified via
   (1)-(4)), and any address recalled from training data without an
   (1)-(4) cross-check. One candidate documentation domain (`agni.gitbook.io`)
   turned out to be an unrelated empty placeholder workspace and was discarded
   rather than cited.

All on-chain evidence in this document was captured against the public
`https://rpc.mantle.xyz` endpoint at the **candidate block** below. Re-running
`uv run python -m snapshot.preflight` reproduces every check, including the
code-hash comparisons; the raw redacted output of the run this document cites is
checked in at `docs/references/preflight-evidence-2026-09-24.json`.

## 2. Candidate common block

| Field | Value |
| --- | --- |
| chain id | `5000` (Mantle mainnet) |
| number | `101057678` (`0x606048e`) |
| hash | `0xa2195dd2b8bf76e093509e7cdf013223ee2127b002be568284f989811a339354` |
| timestamp | `1790245668` (`2026-09-24T10:27:48Z`) |
| status | **candidate only** — chosen as `latest - 1000` at catalog-authoring time for a little reorg margin, *not* an archive-node or finality proof |

DESIGN §2.2 asks I01 to identify a *candidate* block and the tick/bin recovery
method, not to freeze the corpus block; that happens once all five collectors
exist (I04–I08, M2), by re-running the same preflight and collectors together
and rejecting the block if any source's state is incomplete at that point. The
public RPC gave no evidence of archive-node/full-history guarantees — connectivity
and a chain-ID probe are not evidence of retained historical state (owner
decision, 2026-09-24) — so **every** later collector must re-verify this or any
other chosen block's availability itself; nothing here should be read as "this
block is guaranteed usable in three months."

## 3. Per-source findings

Confidence levels (mirrored in `config/protocols.yaml`): `high` (multiple
independent evidence types agree — explorer verification, a first-party repo,
and/or an independent local recompilation), `moderate` (one strong fingerprint,
no independent corroboration yet), `unmatched` (deployment identity confirmed
on-chain, but no upstream source matched — none of the five sources are in this
state after this revision). Every contract entry in `config/protocols.yaml` also
carries its own Keccak-256 `code_hash`, pinned at the candidate block, that
`snapshot/preflight.py` re-verifies on every run — a future bytecode change at
any of these addresses fails the preflight immediately, distinct from a missing
address entirely.

### 3.1 Uniswap v3 (Mantle) — confidence: high

| | |
| --- | --- |
| Factory | `0x0d922Fb1Bc191F64970ac40376643808b4B74Df9` |
| QuoterV2 | `0xdD489C75be1039ec7d843A6aC2Fd658350B067Cf` |
| TickLens | `0x38EB9e62ABe4d3F70C0e161971F29593b8aE29FF` |
| Example pool | `0x4cdFc22bF05209de87Ee564746Dc7E5174631d2b` (WMNT/USDT 0.05%) |
| Upstream | `Uniswap/v3-core` tag `v1.0.0`, commit `e3589b192d0be27e100cd0daaf6c97204fdb1899` |
| License | BUSL-1.1 at publication; **Change Date 2023-04-01 already passed** → automatically GPL-2.0-or-later per the LICENSE file at that tag. Treat as GPL-2.0-or-later. |

**Evidence.** Routescan's explorer-verified source confirms `ContractName =
UniswapV3Factory/UniswapV3Pool/QuoterV2/TickLens` at these four addresses,
compiler `v0.7.6+commit.7338295f`. Independently, `developers.uniswap.org/
deployments.json` (Uniswap Labs' own signed feed, source repo
`Uniswap/contracts`) lists all four for chain `Mantle`/`5000`, tier
`dao-deployed`. On-chain: factory bytecode is 24,535 bytes (`v1.0.0`'s
`UniswapV3Factory` embeds the full `UniswapV3Pool` *creation* bytecode inline,
which is why a genuine deployment is this large — Agni/FusionX below are not,
§3.2/§3.3); `feeAmountTickSpacing` is the canonical Uniswap set
(`100→1, 500→10, 3000→60, 10000→200`); the CBOR metadata trailer decodes to
`{"solc": 0x000706}` (0.7.6) with **no `ipfs` key**, matching `v1.0.0`'s
`hardhat.config.ts` `metadata.bytecodeHash: "none"` setting exactly (Uniswap's
own deterministic-build convention). `factory.getPool(WMNT, USDT, 500)`
round-trips to the example pool; other populated pools exist too
(`getPool(WMNT, USDC, 3000)`, `getPool(USDT, USDC, 100)`), consistent with the
low-but-nonzero activity the exploratory Dune query recorded (DESIGN §5.1: 16
emitting contracts, ~$55.9K priced weekly volume). `tick_lens`'s code hash is
byte-identical to `fusionx_v3.tick_lens` — both run the unmodified upstream
`TickLens.sol`.

**Migration scope (for I03, generic CL math).** `quote_exact_in` for this family
needs: `UniswapV3Pool.swap` (`contracts/UniswapV3Pool.sol`), `SwapMath.computeSwapStep`,
`TickMath.getSqrtRatioAtTick`/`getTickAtSqrtRatio`, `SqrtPriceMath`, `LiquidityMath`,
`TickBitmap.nextInitializedTickWithinOneWord`, and `Tick`'s per-tick
`liquidityNet`/`liquidityGross`/fee-growth-outside fields — mapped 1:1 to Python
integer functions, preserving Q64.96 fixed-point widths, `mulDiv` rounding
direction, and revert conditions (e.g. `SPL`/`SPU` price-limit checks). No
governance/NFT-position/staking contracts are in scope.

### 3.2 Agni Finance v3 — confidence: high

| | |
| --- | --- |
| Factory | `0x25780dc8Fc3cfBD75F33bFDAB65e969b603b2035` |
| PoolDeployer | `0xe9827B4EBeB9AE41FC57efDdDd79EDddC2EA4d03` |
| Example pool | `0x1858d52cf57c07a018171d7a1e68dc081f17144f` (USDC/WMNT 0.05%) |
| Upstream | `agni-protocol/contracts` (main), `core/contracts/{AgniFactory,AgniPool,AgniPoolDeployer}.sol` @ commit `7278c3aafba82db83bbb85e5c0f5554f77ec3db4` |
| License | GPL-2.0-or-later (SPDX header) |

**Evidence — upgraded from `moderate` to `high` in this revision.** Routescan's
explorer-verified source confirms `ContractName = AgniFactory/AgniPool/
AgniPoolDeployer` at these three addresses, compiler `v0.7.6+commit.7338295f`,
optimizer runs `200`. Independently, Agni's own first-party repository,
`agni-protocol/contracts`, ships a checked-in
`core/deployments/mantleMainnet.json` that names these **exact** addresses
(`"AgniFactory": "0x2578...2035"`, `"AgniPoolDeployer": "0xe982...4d03"`). The
`core/contracts/AgniPool.sol` file has a single commit in its entire history
(`7278c3a`, "add code" — 2023-07-18), so the pin is unambiguous.

The verified `AgniPool.sol` source confirms what the first pass only inferred
from bytecode fingerprinting: it imports `IAgniLmPool`, a liquidity-mining pool
hook interface with **no Uniswap-v3-core equivalent** — the same concept as
PancakeSwap V3's `ILMPool`. Combined with the Factory/PoolDeployer split (the
factory is only 3,900 bytes; a second, ~24,565-byte `PoolDeployer` contract
holds the actual `UniswapV3Pool`-sized creation code) and the non-canonical
fee-tier set (`100→1, 500→10, 2500→50, 10000→200` — no `3000`, matching
`PancakeV3Factory.sol`'s constructor at `pancakeswap/pancake-v3-contracts@ffa4fb2`
token-for-token), **Agni v3 is confirmed as a PancakeSwap-V3-core-family fork,
not a Uniswap-v3-core deployment**, despite the discovery label. `getPool(USDC,
WMNT, 500)` round-trips to the example pool.

**Migration scope.** The verified source's library file list
(`BitMath`/`FixedPoint128`/`FixedPoint96`/`FullMath`/`LiquidityMath`/
`LowGasSafeMath`/`Oracle`/`Position`/`SafeCast`/`SqrtPriceMath`/`SwapMath`/
`Tick`/`TickBitmap`/`TickMath`/`TransferHelper`/`UnsafeMath`) is otherwise
file-for-file identical to Uniswap's own, so the core swap math is
Uniswap-V3-core-equivalent — **plus** an `lmPool.accumulateReward(...)` call at
swap start and `lmPool.crossLmTick(...)` on every tick crossing (confirmed
present in the sibling FusionX source, §3.3, which was verified as a single
flattened file showing this exact logic; Agni's multi-file verified source
declares the same `IAgniLmPool` hook on the pool). Since no Mantle Agni pool is
expected to have an LM pool hook attached in the admitted Exact Input scope,
this must be modeled as an explicit no-op branch (or excluded with a
documented reason), not silently dropped.

### 3.3 FusionX v3 — confidence: high

| | |
| --- | --- |
| Factory | `0x530d2766D1988CC1c000C8b7d00334c14B69AD71` |
| PoolDeployer | `0x8790c2C3BA67223D83C8FCF2a5E3C650059987b4` |
| Example pool | `0x262255f4770aebe2d0c8b97a46287dcecc2a0aff` (WMNT/USDT 0.05%) |
| QuoterV2 | `0x90f72244294E7c5028aFd6a96E18CC2c1E913995` |
| TickLens | `0xE80EE7e19Dd06365111471E1478bA671CAd024C0` |
| Upstream | `FusionX-Finance/v3-contracts` (main), `projects/v3-core/contracts/{FusionXV3Factory,FusionXV3Pool,FusionXV3PoolDeployer}.sol` @ commit `7f7406e5fbdf610acf0fedac815c29fbca69925a` |
| License | GPL-2.0-or-later (SPDX header) |

**Evidence.** Routescan's explorer-verified source confirms `ContractName =
FusionXV3Factory/FusionXV3Pool/FusionXV3PoolDeployer/QuoterV2/TickLens`,
compiler `v0.7.6+commit.7338295f`. FusionX's own current official docs
(`docs.fusionx.finance/developers/smart-contracts-mantle-mainnet/v3-smart-contracts.md`)
publish the factory/pool_deployer addresses verbatim, matching both the
explorer record and the independent on-chain round-trip
(`example_pool.factory()` / `factory.poolDeployer()`) done before that page was
ever fetched. `FusionX-Finance/v3-contracts` mirrors
`pancakeswap/pancake-v3-contracts`' own `projects/{v3-core,v3-periphery,
v3-lm-pool}` monorepo layout, just under the `@fusionx/*` package scope — the
verified (flattened single-file) `FusionXV3Pool.sol` source's own import
comment, `// File @fusionx/v3-lm-pool/contracts/interfaces/
IFusionXV3LmPool.sol@v1.0.0`, names that scope directly and contains the exact
LM-pool-hook swap-loop calls described in §3.2. Same
PancakeSwap-V3-core-family lineage and non-canonical fee-tier set as Agni.
`tick_lens`'s code hash is byte-identical to `uniswap_v3.tick_lens` (both run
unmodified upstream `TickLens.sol`).

**Local recompilation (added by WHI-1430).** The first pass did not recompile
FusionX (it rested on Routescan's verification plus the corroboration above).
WHI-1430 closed that gap: `tools/cl_evidence/rebuild_fusionx.sh` compiles
`FusionX-Finance/v3-contracts@7f7406e` `projects/v3-core` with solc 0.7.6 (solc-js;
optimizer runs 20, `istanbul`, `bytecodeHash: none` — the on-chain CBOR trailer
`a164736f6c6343000706000a` carries no IPFS hash) and the deployed runtime code at the
candidate block is **byte-identical** outside compiler-reported immutable sites:
`FusionXV3Pool` 22,718/22,718 bytes (34 sites), `FusionXV3Factory` 3,806/3,806 (2
`poolDeployer` sites), `FusionXV3PoolDeployer` 24,461/24,461 (none). The pinned commit
ships only `projects/v3-core`; its one external import, the two-function
`IFusionXV3LmPool` *interface*, is written from the verified source's own flattened
`@fusionx/v3-lm-pool@v1.0.0` section (interfaces only fix selectors). The pool build's
Keccak-256 (immutables zero) is exactly `fusionx_v3.cl_collection.pool_code_normalized_hash`.

**Migration scope.** Same as Agni (§3.2): Uniswap-V3-core-equivalent math
libraries plus the LmPool hook branch, in a separate `pools/concentrated.py`
admission record per DESIGN §2.3 ("A shared CL implementation is allowed only
after Agni/FusionX/Uniswap differences are checked; each source retains its own
admission record and tests").

### 3.4 Merchant Moe Classic v1 — confidence: high

| | |
| --- | --- |
| Factory | `0x5bEf015CA9424A7C07B68490616a4C1F094BEdEc` |
| Pair implementation | `0x08477e01A19d44C31E4C11Dc2aC86E3BBE69c28B` |
| Example pool | `0x4e7685df06201521f35a182467feefe02c53d847` (USDT/WMNT) |
| Router | `0xeaEE7EE68874218c3558b40063c42B82D3E7232a` |
| Upstream | `merchant-moe/moe-core` (main), `src/dex/{MoeFactory,MoePair}.sol` @ commit `460bf55b8330f31d31d356634d0129fd78b4fd07` |
| License | GPL-3.0 (SPDX header in `MoeFactory.sol`/`MoePair.sol`) |

**Evidence — resolved from `unmatched` to `high` in this revision.** The first
pass correctly *rejected* the natural candidate, `lfj-gg/joe-core`
(architecturally incompatible: full-`creationCode` `CREATE2` vs. this
deployment's minimal-proxy clones; `pragma solidity =0.6.12` vs. this
deployment's on-chain `solc 0.8.20`), but stopped there instead of searching
for a Merchant-Moe-owned repository. `merchant-moe/moe-core` exists and is
conclusive: its own `script/mantle/Addresses.sol` hardcodes
`moeFactory = 0x5bEf015CA9424A7C07B68490616a4C1F094BEdEc`,
`moePairImplementation = 0x08477e01A19d44C31E4C11Dc2aC86E3BBE69c28B` and
`moeRouter = 0xeaEE7EE68874218c3558b40063c42B82D3E7232a` — all three, verbatim.
`src/dex/MoeFactory.sol` imports `ImmutableClone` and takes a
`moePairImplementation_` constructor immutable, exactly matching the
minimal-proxy architecture already observed on-chain.

Routescan does **not** have verified source for `MoeFactory`/`MoePair`
themselves (only the `MoeRouter` periphery contract is explorer-verified there:
`ContractName = MoeRouter`, `v0.8.20+commit.a1b79de6`, runs `600`). For the two
contracts explorer verification could not confirm, this catalog did the
fallback DESIGN §2.3 anticipates ("otherwise a fork simulation") one level
earlier — an independent local `forge build` of `merchant-moe/moe-core`
(commit `460bf55`, the repo's own `foundry.toml`: solc `0.8.20`, optimizer
runs `600`, `evm_version = paris`) reproduces the on-chain runtime bytecode of
both `MoeFactory` and `MoePair` **byte-for-byte outside of embedded immutables
and the CBOR metadata hash**:

- `MoeFactory`: 2,141/2,141 bytes match; the only differences are the
  `moePairImplementation` immutable (3 occurrences: `08477e01a19d44c31e4c11
  dc2ac86e3bbe69c28b` on-chain vs. an unlinked zero placeholder in the fresh
  build) and the metadata hash (differs only because absolute build paths
  differ, as expected).
- `MoePair`: 9,591/9,591 bytes match; differences are the implementation's own
  self-referential immutable and the `factory` immutable (both addresses,
  matching exactly), plus the metadata hash.

This is the strongest non-explorer evidence class this catalog uses, and it
fully resolves what was the single largest gap in the first pass.

**Migration scope (for I07).** `src/dex/MoePair.sol`'s swap/mint/burn logic —
this is a genuine Merchant-Moe-authored constant-product implementation (not a
literal copy of any Trader Joe/Uniswap-V2 file), so its exact fee/rounding
behavior must be read from *this* source, not assumed equivalent to a generic
Uniswap-V2 fork. `MoeFactory.getPair`/`allPairsLength`/`createPair` (clone
deployment via `ImmutableClone`) and `MoePair.getReserves`/`swap` are the
relevant surface; `MoeRouter.sol` (separately explorer-verified) is
periphery-only and out of the `quote_exact_in` critical path.

### 3.5 Merchant Moe Liquidity Book v2.2 — confidence: high

| | |
| --- | --- |
| LBFactory | `0xa6630671775c4EA2743840F9A5016dCf2A104054` |
| LBPair implementation | `0xf6863Db7323aaC43fE8AEF0B3eF63AA6b32DDB3B` |
| Example pair (clone) | `0xf6c9020c9e915808481757779edb53daceae2415` (WMNT/USDT, binStep 15) |
| LB Router | `0x013e138EF6008ae5FDFDE29700e3f2Bc61d21E3a` |
| LB Quoter | `0x501b8AFd35df20f531fF45F6f695793AC3316c85` |
| Upstream (LBFactory + LBPair) | `lfj-gg/joe-v2` tag `v2.2.0`, commit `1297c3822f0605e643155c35948959c0a0d05e17` |
| License | MIT (repo-root `LICENSE` at the pinned tag; per-file SPDX headers agree) |

**Evidence for the swap-math-critical contracts (LBFactory, LBPair).** Both are
confirmed *two independent ways*: Routescan explorer verification
(`ContractName = LBFactory`/`LBPair`, compiler `v0.8.20+commit.a1b79de6`) **and**
an independent local `forge build` of `lfj-gg/joe-v2`:

- `LBFactory`: building the repo's `tag v2.2.0` (or, equivalently for this one
  file, `merchant-moe/moe-core`'s pinned `lib/dexv2` submodule commit `24efde2`
  on branch `v2.2` — both give identical output for this file) with the repo's
  default settings (`optimizer runs=800`) reproduces the on-chain runtime
  bytecode **100% byte-identical, including the CBOR metadata trailer** — no
  immutable substitution was even needed to get a clean diff.
- `LBPair` (the implementation behind every clone, obtained via
  `LBFactory.getLBPairImplementation()`): building with the repo's *default*
  `runs=800` gives bytecode of the *wrong length* (25,224 vs. the on-chain
  24,321 bytes). Routescan's own verified-source metadata for this address
  reports `Runs=300` — a **per-contract optimizer override**, different from
  `LBFactory`/`LBRouter`/`LBQuoter`'s `800`. Rebuilding with
  `forge build --optimizer-runs 300` reproduces the on-chain bytecode
  byte-for-byte outside of two embedded immutables (this contract's own
  address and the `LBFactory` address, 7 occurrences total) and the metadata
  hash. This nuance — *how* the mismatch was diagnosed and resolved — is the
  concrete illustration of DESIGN §2.3's "match by compiling... and comparing
  runtime bytecode" for a case where the naive default-settings rebuild would
  have wrongly looked like a source mismatch.

`example_pair` (the clone) round-trips to `LBFactory` via `getFactory()`;
`LBFactory.getNumberOfLBPairs() = 195` (a real, heavily-populated deployment).
Merchant Moe's own docs (`docs.merchantmoe.com/resources/contracts.md`) publish
the `LBFactory` address verbatim.

**LB Router and LB Quoter are explorer-verified but *not* vanilla `joe-v2`.**
Both are confirmed on Routescan (`ContractName = LBRouter`/`LBQuoter`, same
compiler and `runs=800` as `LBFactory`), yet a local `forge build` of either
pinned `joe-v2` commit produces bytecode of a different *length* for both
(`LBRouter`: 21,003 compiled vs. 21,011 on-chain; `LBQuoter`: 14,792 compiled
vs. 18,102 on-chain — plausibly a Merchant-Moe-specific quoter that also
handles Classic routes in one contract). This is a genuine, confirmed
customization, not a build-settings artifact like `LBPair`'s was. Neither
contract is in the `quote_exact_in` critical path (`LBPair.swap` owns the
actual math), so this does not block LB admission, but it does mean a future
router/quoter-level integration must read *this* deployment's own verified
source rather than assuming vanilla `joe-v2` periphery behavior.

**Migration scope (for I08).** `src/LBPair.sol`'s `swap`, `src/libraries/`:
`PriceHelper` (bin id ↔ price), `BinHelper`, `FeeHelper` (static **and**
dynamic/volatility-accumulator fee state — DESIGN §2.2's "an LB swap may update
its dynamic-fee state within a plan" — `PairParameterHelper`'s packed
`volatilityAccumulator`/`volatilityReference`/`idReference`/
`timeOfLastUpdate` fields), `Uint128x128Math`/`Uint256x256Math` for the
fixed-point bin math, and `SafeCast`. All MIT-licensed, so no BUSL/change-date
consideration is needed here (unlike Uniswap v3, §3.1). Preserve the
`optimizer runs=300` fact only as provenance metadata (Python has no optimizer
runs); it does not affect the math port, only which exact bytecode was being
matched during verification.

## 4. License summary

| Source | Confirmed upstream | License | Notes |
| --- | --- | --- | --- |
| Uniswap v3 | `Uniswap/v3-core@v1.0.0` | GPL-2.0-or-later | Was BUSL-1.1; Change Date 2023-04-01 already passed. |
| Agni v3 | `agni-protocol/contracts@7278c3a` | GPL-2.0-or-later | Confirmed via explorer + first-party repo (this revision). |
| FusionX v3 | `FusionX-Finance/v3-contracts@7f7406e` | GPL-2.0-or-later | Confirmed via explorer + official docs + package namespace + independent local recompile (WHI-1430, §3.3). |
| Moe Classic v1 | `merchant-moe/moe-core@460bf55` | GPL-3.0 | Confirmed via first-party repo + independent `forge` bytecode match (this revision). The repo's own deploy-script files, e.g. `script/mantle/Addresses.sol`, are separately MIT-licensed but are not migration-relevant. |
| Moe LB v2.2 | `lfj-gg/joe-v2@v2.2.0` (LBFactory/LBPair only — see §3.5 for Router/Quoter caveat) | MIT | Confirmed via explorer + independent `forge` bytecode match (including the `runs=300` LBPair nuance). |

Per the owner's 2026-09-24 decision (`docs/references/0.1.0-execution-decisions.md`):
private internal research reuse of suitable upstream code is agent-autonomous —
no separate licensing-approval gate — provided pinned source/version notices are
retained and the repository's visibility/redistribution posture is not changed
incidentally. All licenses identified above (GPL-2.0-or-later, GPL-3.0, MIT) are
compatible with that private, non-redistributing internal use. This
determination is scoped to the sources actually matched above.

## 5. Dune-emitter-to-pool validation strategy

DESIGN §2.2 warns that Dune's `project_contract_address` is an *emitting*
address and "can identify a router instead of a pool." The strategy this
catalog used, and that later per-source issues (I04–I08) must repeat before
trusting any Dune-derived pool list:

1. Never admit a pool because Dune labeled its emitting contract with a project
   name. Take the candidate address only as a *lead*.
2. Independently call the candidate address's own identity getters
   (`factory()`/`token0()`/`token1()` for v3-style; `factory()`/`getReserves()`
   for Classic; `getFactory()`/`getTokenX()`/`getTokenY()` for LB).
3. Round-trip through the configured factory in the *other* direction
   (`factory.getPool(token0,token1,fee)` / `factory.getPair(token0,token1)` /
   `factory.getNumberOfLBPairs()` + membership) and require it to return the
   same address. A router, a periphery contract, or a stale/rugged pool will
   fail this round-trip even if Dune's decoder happily labeled its logs.
4. Where possible, corroborate with an explorer-verified `ContractName` (§1.2)
   and/or a first-party deployment record (§1.3) — as done for every contract
   in this catalog except the two Merchant Moe Classic contracts explorer
   verification does not cover (§3.4), where the `forge`-recompile match (§1.4)
   stands in.
5. Only after (2)-(4) agree does the address enter `config/protocols.yaml` as
   an `example_pool`/`example_pair` — which is exactly what
   `snapshot/preflight.py`'s per-family checks (plus the code-hash check) re-verify
   on every run.

This is why every address in §3 has an explicit "round-trip" and "verified"
sentence: the round-trip *is* the emitter-to-pool validation, not a separate
step done once and forgotten.

## 6. State completeness strategy (tick/bin recovery)

DESIGN §2.2 requires identifying "the method for complete tick/bin recovery," not
performing a full historical crawl (explicitly out of scope for this issue).

**Concentrated-liquidity sources (Uniswap v3 / Agni v3 / FusionX v3).** Ticks are
recorded in 256-bit words via `tickBitmap(int16 wordPosition) -> uint256`
(selector `0x5339c296`), one bit per initialized tick at the pool's
`tickSpacing`. The method is: read `slot0()` for the current tick, compute
`wordPosition = (tick / tickSpacing) >> 8`, then walk outward through
consecutive words (both directions) calling `tickBitmap` until a word is empty
past the amount envelope's price-impact bound, decoding each set bit position
back to a tick index and reading that tick's full state via `ticks(int24) ->
(liquidityGross, liquidityNet, feeGrowthOutside0X128, feeGrowthOutside1X128,
tickCumulativeOutside, secondsPerLiquidityOutsideX128, secondsOutside,
initialized)`. Where a project's own `TickLens` contract exists (confirmed
deployed for Uniswap v3 and FusionX v3, §3.1/§3.3;
`getPopulatedTicksInWord(pool, wordPosition)`), it returns already-decoded
`(tick, liquidityNet, liquidityGross)` tuples in one call, which is strictly
more efficient than the raw bitmap-then-ticks walk but requires that contract to
exist per-factory (Agni does not have a deployed `TickLens` in this catalog —
use the raw `tickBitmap`+`ticks` walk for Agni until/unless one is found).

This was **probed, not fully executed**, against the Agni example pool at the
candidate block: `tickBitmap(0)` returned all zeros (word 0 is far from the
current price); the word actually containing the current tick
(`slot0().tick=280457`, `tickSpacing=10` → compressed tick `28045` → word `109`)
returned `0xfff...ffeff...fff` — **255 of 256 bits set**, i.e. genuinely dense,
real initialized-tick data, confirming the method is RPC-readable on the public
endpoint at this block. Walking the full envelope for a real corpus is I04/I05's
job, not this one's (explicitly out of scope: "Full dataset capture").

*Executed for Agni by WHI-1429:* `snapshot/collectors/concentrated.py` performs this
raw `tickBitmap` + `ticks()` walk with EIP-1898 hash-pinned reads, growing the word
range until the declared amount envelope fits the migrated simulator
(`docs/references/agni-fixed-block-replay.md` §1, §3).

**Liquidity Book (Merchant Moe v2.2).** Bins are not bitmap-indexed the same
way; recovery is via `LBPair.getActiveId()` for the current bin plus
`getNextNonEmptyBin(swapForY, id)` (present in the `v2.2.0` `LBPair` ABI,
independently confirmed present in this catalog's own `forge`-compiled
artifact, §3.5) walked outward in both directions from the active id until the
envelope is covered, or via the `LBQuoter` periphery contract (deployed and
code-present at the candidate block, though confirmed to be a
Merchant-Moe-customized build, §3.5) if it exposes an equivalent batch view.
Each bin's full state is `getBin(id) -> (binReserveX, binReserveY)` plus the
pair-level packed parameters (`getStaticFeeParameters`,
`getVariableFeeParameters` — the volatility accumulator DESIGN §2.2 calls out as
needing exact modeling of same-plan dynamic-fee transitions). This was not
probed on-chain in this pass (the `getNumberOfLBPairs`/`getFactory`/
`getTokenX`/`getTokenY`/`getBinStep` checks in §3.5 establish the pair is real
and readable; full bin enumeration is I08's job) — recorded here as the
identified method, per DESIGN's "identify... the method," not as executed
recovery.

**Classic (Merchant Moe v1).** Trivial: `getReserves()` returns the complete
state (two reserves + `blockTimestampLast`); already confirmed readable in §3.4.

## 7. What the preflight actually rejects

`snapshot/preflight.py` (executable: `uv run python -m snapshot.preflight`)
loads `config/protocols.yaml`, and for every run:

1. Calls `eth_chainId` and rejects immediately (no further calls made) if it is
   not `5000` — a `--rpc-url` override pointed at the wrong network is caught
   before any address is even dereferenced.
2. Calls `eth_getBlockByNumber(candidate_block.number)` and rejects if the
   returned hash or timestamp disagrees with the frozen values (reorg, wrong
   chain despite (1), or a stale catalog after the RPC pruned the block).
3. For every configured contract, checks code is present *and*, when the
   catalog pins one, that its Keccak-256 hash still matches — a pruned/
   never-deployed/mistyped address fails on presence; a live bytecode change
   since this catalog was written fails on hash, with its `source_key`/
   `contract_role` attached either way.
4. Re-runs every identity round-trip and fee-tier/state-shape check narrated in
   §3, failing on any disagreement (`identity_mismatch`, `fee_tier_mismatch`,
   `missing_state`) or unreachable state (`call_reverted`, `rpc_unavailable`).

None of these ever substitute `latest` for the pinned block, merge results from
two different blocks, or return a fabricated "ok" when a read fails — a failed
read is always a reported issue, never silently absent from the report.

Offline behavior (all of §7's rejection paths, including code-hash mismatch,
exercised with a fully scripted fake transport, no network) is tested in
`tests/snapshot/test_preflight.py` and `tests/snapshot/test_abi.py` (the latter
independently re-derives every hardcoded ABI selector from its signature via
Keccak-256, so the encoding constants themselves are checked, not just
trusted). The real, network-touching run whose output is quoted throughout §3
is retained, redacted, at `docs/references/preflight-evidence-2026-09-24.json`
(`ok: true`, chain id `5000`, block hash matching §2, zero `known_blockers` —
the public RPC's default URL carries no credential, so nothing was stripped,
but `redact_url()` in `snapshot/preflight.py` would strip one if a future
`--rpc-url` override had one).

## 8. Consolidated blockers

After this revision, **no source-identity blocker remains** for any of the five
sources: all five now have an explorer-verified and/or independently
`forge`-recompiled match to a named, commit-pinned upstream repository. The
residual, honestly-scoped gaps are:

1. ~~FusionX v3 was not independently recompiled~~ — **resolved by WHI-1430** (§3.3):
   the pinned upstream rebuilds byte-identically (outside immutables) for the pool,
   factory and pool deployer.
2. **Merchant Moe LB Router/Quoter are confirmed customized, not vanilla
   `joe-v2`** (§3.5) — real, source-confirmed (via their own Routescan-verified
   code, not guessed) but not yet diffed against a specific alternate upstream.
   Not in the `quote_exact_in` critical path, so does not block LB pair-level
   admission (I08); relevant only if a future issue needs Router/Quoter-level
   fidelity.

Neither of these is papered over with a generic AMM formula or an assumed
source, and neither shrinks the five-source scope: all five remain fully
admitted into the catalog and preflight.

## 9. Verification / reproduction

```bash
uv run pytest tests/snapshot/                     # offline, no network
uv run python -m snapshot.preflight                # explicit online run (needs network)
```

The online command is idempotent and safe to re-run (bounded retries/backoff,
in-run response cache, never falls back to a different block/endpoint); it
prints and can also write (`--output`) the full redacted evidence JSON quoted
throughout this document, including the per-contract code-hash comparison.

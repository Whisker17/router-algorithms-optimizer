# Protocol admission catalog — findings (WHI-1426 / I01)

Status: **catalog + preflight published, per-source math admission NOT started.**
This document records what was independently verified about each of the five
DESIGN §1.2 sources on Mantle mainnet (chain id `5000`), what still cannot be
verified with the access available in this pass, and the exact Solidity→Python
migration scope each later issue (I03–I08) inherits. It is the narrative
companion to the machine-readable `config/protocols.yaml`, which
`snapshot/preflight.py` checks against on every run.

Per DESIGN §2.2/§2.3: a Dune project label or ABI resemblance is never treated as
proof of protocol identity here. Every fact below is tagged with **how** it was
established, and every unresolved item is recorded as a **blocker**, not silently
dropped or filled in with a generic AMM formula.

## 1. Verification hierarchy used in this pass

In order of how much weight a claim gets:

1. **On-chain differential evidence** — `eth_getCode` (presence + byte size +
   CBOR metadata trailer: `solc` version and, when present, the IPFS metadata
   hash) and `eth_call` round-trips (e.g. `pool.factory()` must equal the
   configured factory; `factory.getPool(token0,token1,fee)` must equal the
   configured example pool) at the fixed candidate block. This is what
   `snapshot/preflight.py` re-checks every run.
2. **A project's own current official documentation** naming the exact address
   (fetched live during this pass, cross-checked against (1)).
3. **Upstream GitHub source at a pinned commit/tag**, matched to (1) by
   behavioral/metadata fingerprint (constructor-initialized fee tiers, compiler
   version, architecture such as a separate `PoolDeployer` or `ImmutableClone`
   minimal-proxy pattern) — this pass could not obtain byte-for-byte
   explorer-verified source (§6), so upstream pins below are the *best matched
   candidate*, explicitly labeled `confidence: high|moderate|unmatched` in the
   YAML, never asserted as a confirmed compiler-output diff.
4. **Rejected**: search-engine snippets, GeckoTerminal/DexScreener pool listings
   *alone* (used only to discover a candidate address to then verify via (1)),
   and any address recalled from training data without an (1)/(2)/(3) check.
   One candidate documentation domain (`agni.gitbook.io`) turned out to be an
   unrelated empty placeholder workspace and was discarded rather than cited.

All on-chain evidence in this document was captured against the public
`https://rpc.mantle.xyz` endpoint at the **candidate block** below. Re-running
`uv run python -m snapshot.preflight` reproduces every check; the raw redacted
output of the run this document cites is checked in at
`docs/references/preflight-evidence-2026-09-24.json`.

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

Confidence levels (mirrored in `config/protocols.yaml`): `high` (two independent
discovery paths agree, or an authoritative first-party feed matches on-chain
behavior), `moderate` (one strong behavioral/metadata fingerprint, no independent
corroboration yet), `unmatched` (identity of the *deployment* is confirmed
on-chain, but no upstream Solidity source has been matched — swap math must not
be migrated from a guess).

### 3.1 Uniswap v3 (Mantle) — confidence: high

| | |
| --- | --- |
| Factory | `0x0d922Fb1Bc191F64970ac40376643808b4B74Df9` |
| QuoterV2 | `0xdD489C75be1039ec7d843A6aC2Fd658350B067Cf` |
| TickLens | `0x38EB9e62ABe4d3F70C0e161971F29593b8aE29FF` |
| Example pool | `0x4cdFc22bF05209de87Ee564746Dc7E5174631d2b` (WMNT/USDT 0.05%) |
| Upstream | `Uniswap/v3-core` tag `v1.0.0`, commit `e3589b192d0be27e100cd0daaf6c97204fdb1899` |
| License | BUSL-1.1 at publication; **Change Date 2023-04-01 already passed** → automatically GPL-2.0-or-later per the LICENSE file at that tag. Treat as GPL-2.0-or-later. |

**Evidence.** `developers.uniswap.org/deployments.json` (Uniswap Labs' own signed
feed, source repo `Uniswap/contracts`) lists all four addresses above for chain
`Mantle`/`5000`, tier `dao-deployed`. On-chain, at the candidate block:

- Factory bytecode is 24,535 bytes. `v1.0.0`'s `UniswapV3Factory` embeds the full
  `UniswapV3Pool` *creation* bytecode inline (`new UniswapV3Pool{salt}()` inside
  `UniswapV3PoolDeployer`), which is why a genuine deployment is this large —
  Agni/FusionX below are not (§3.2/§3.3).
- `feeAmountTickSpacing`: `100→1, 500→10, 3000→60, 10000→200` — the canonical
  Uniswap set, unlike both Mantle-native v3-style forks below.
- CBOR metadata trailer decodes to `{"solc": 0x000706}` (0.7.6) **with no `ipfs`
  key**. `v1.0.0/hardhat.config.ts` sets `metadata.bytecodeHash: "none"` with the
  comment "so we want all generated code to be deterministic" — this is
  Uniswap's own distinctive build convention, not a generic omission.
- `factory.getPool(WMNT, USDT, 500)` round-trips to the example pool; other
  populated pools exist too (`getPool(WMNT, USDC, 3000)`,
  `getPool(USDT, USDC, 100)`, both with real ~22 KB pool bytecode) — consistent
  with the low-but-nonzero activity the exploratory Dune query recorded
  (DESIGN §5.1: 16 emitting contracts, ~$55.9K priced weekly volume).

**Migration scope (for I03, generic CL math).** `quote_exact_in` for this family
needs: `UniswapV3Pool.swap` (`contracts/UniswapV3Pool.sol`), `SwapMath.computeSwapStep`,
`TickMath.getSqrtRatioAtTick`/`getTickAtSqrtRatio`, `SqrtPriceMath`, `LiquidityMath`,
`TickBitmap.nextInitializedTickWithinOneWord`, and `Tick`'s per-tick
`liquidityNet`/`liquidityGross`/fee-growth-outside fields — mapped 1:1 to Python
integer functions, preserving Q64.96 fixed-point widths, `mulDiv` rounding
direction, and revert conditions (e.g. `SPL`/`SPU` price-limit checks). No
governance/NFT-position/staking contracts are in scope.

### 3.2 Agni Finance v3 — confidence: moderate

| | |
| --- | --- |
| Factory | `0x25780dc8Fc3cfBD75F33bFDAB65e969b603b2035` |
| PoolDeployer | `0xe9827B4EBeB9AE41FC57efDdDd79EDddC2EA4d03` |
| Example pool | `0x1858d52cf57c07a018171d7a1e68dc081f17144f` (USDC/WMNT 0.05%) |
| Upstream (candidate, unconfirmed) | `pancakeswap/pancake-v3-contracts` @ `ffa4fb2cef38cf4769ff88e1cc5551c4af4f6c57`, `projects/v3-core/contracts/PancakeV3Factory.sol` |
| License (of the candidate) | GPL-2.0-or-later (SPDX header) |

**Evidence and why "Uniswap v3-core" was rejected as the reference.** The Agni
factory is only 3,900 bytes — far too small to embed a Pool's ~24 KB creation
code the way real `UniswapV3Factory` does — and it exposes a `poolDeployer()`
getter (selector `0x3119049a`) that Uniswap's factory does not have, returning a
second, ~24,565-byte contract. `factory.feeAmountTickSpacing` for
`{100,500,2500,10000}` returns `{1,10,50,200}`; **`3000` is not enabled at all**
(returns `0`). Fetching `PancakeV3Factory.sol` at the commit above shows its
constructor initializes *exactly* `100→1, 500→10, 2500→50, 10000→200` and takes
an immutable `poolDeployer` address in its own constructor — an exact
architectural and constant match. `getPool(USDC, WMNT, 500)` round-trips to the
example pool. **Conclusion: Agni v3 is a PancakeSwap-V3-core-family fork, not a
Uniswap-v3-core deployment** — migrating its math from raw Uniswap v3-core
without checking `PancakeV3Pool.sol`'s deltas (whitelisting, LM-pool hook,
`feeProtocol` packing, any swap-math changes) would be exactly the "generic
formula substitutes for an unmatched deployment" DESIGN §2.3 forbids.

**Blockers (see `config/protocols.yaml` for full text/IDs):**
- No byte-exact explorer/commit match yet — mantlescan's API now requires a paid
  Etherscan-V2 key we do not have, `explorer.mantle.xyz` returned `502` for the
  entire session, mantlescan's web UI returns `403` to non-browser fetches, and
  Sourcify has no full/partial match for any of the three addresses on chain
  `5000`. Blocks I04.
- No authoritative Agni documentation site with a contracts page was reachable
  (`docs.agni.finance` did not resolve; `agni.gitbook.io` is an empty,
  unaffiliated placeholder space). Identity rests entirely on independent
  on-chain discovery (a live pool GeckoTerminal labels "Agni Finance", then
  `pool.factory()`/`factory.poolDeployer()` round-trips), not a project-published
  source. Blocks I04.

### 3.3 FusionX v3 — confidence: high

| | |
| --- | --- |
| Factory | `0x530d2766D1988CC1c000C8b7d00334c14B69AD71` |
| PoolDeployer | `0x8790c2C3BA67223D83C8FCF2a5E3C650059987b4` |
| Example pool | `0x262255f4770aebe2d0c8b97a46287dcecc2a0aff` (WMNT/USDT 0.05%) |
| QuoterV2 | `0x90f72244294E7c5028aFd6a96E18CC2c1E913995` |
| TickLens | `0xE80EE7e19Dd06365111471E1478bA671CAd024C0` |
| Upstream (candidate, unconfirmed) | same PancakeSwap-v3-core-family commit as §3.2 |
| License (of the candidate) | GPL-2.0-or-later |

**Evidence.** Same PancakeSwap-family fingerprint as Agni (identical fee-tier
set, identical Factory/PoolDeployer split). Higher confidence than Agni because
FusionX's **own current official docs**
(`docs.fusionx.finance/developers/smart-contracts-mantle-mainnet/v3-smart-contracts.md`)
publish `FusionXV3Factory = 0x530d...AD71` and
`FusionXV3PoolDeployer = 0x8790...87b4` — both addresses match, character for
character, what was independently derived on-chain via
`example_pool.factory()` and `factory.poolDeployer()` before that page was ever
fetched. Two independent discovery paths agreeing is strong identity evidence;
it is still not an explorer-verified-source diff (same blocker family as §3.2,
blocking I05).

**Migration scope.** Same planned Python module as Agni/Uniswap v3
(`pools/concentrated.py`), but admission tests must not assume Agni and FusionX
share one fixture set until the exact PancakeSwap-derived source (and any
Agni/FusionX-specific edits on top of it) is diffed per source, per DESIGN
§2.3 ("A shared CL implementation is allowed only after ... differences are
checked; each source retains its own admission record and tests").

### 3.4 Merchant Moe Classic v1 — confidence: **unmatched**

| | |
| --- | --- |
| Factory | `0x5bEf015CA9424A7C07B68490616a4C1F094BEdEc` |
| Pair implementation | `0x08477e01A19d44C31E4C11Dc2aC86E3BBE69c28B` |
| Example pool | `0x4e7685df06201521f35a182467feefe02c53d847` (USDT/WMNT) |
| Router | `0xeaEE7EE68874218c3558b40063c42B82D3E7232a` |
| Upstream | **none matched** (see rejected candidate below) |

**Evidence of the deployment itself (solid).** `docs.merchantmoe.com/resources/contracts.md`
(Merchant Moe's own current docs) publish `MoeFactory`, `MoePair`, `MoeRouter` at
exactly these addresses. On-chain: `factory.getPair(USDT, WMNT)` round-trips to
the example pool; `factory.allPairsLength() = 230`; `pool.getReserves()` returns
a well-formed `(reserve0, reserve1, blockTimestampLast)` triple; **every** Moe
Classic pool is a ~95-byte minimal-proxy clone whose embedded target address
decodes to `0x08477e01A19d44C31E4C11Dc2aC86E3BBE69c28B` — the exact "MoePair"
address the docs publish, byte for byte. The deployment identity is not in
doubt.

**Evidence the natural upstream candidate does NOT match (why this is
unmatched, not just unconfirmed).** Merchant Moe's docs describe it as "built
with the same robust technology as LFJ Dex" (LFJ = Trader Joe's current org,
`github.com/lfj-gg`), making `lfj-gg/joe-core`'s
`contracts/traderjoe/{JoeFactory,JoePair}.sol` the obvious first candidate. It
was checked and rejected:

1. **Architecture.** `JoeFactory.createPair` deploys `JoePair`'s *full*
   `creationCode` via `create2` — it has no clone/minimal-proxy path at all.
   Moe Classic's on-chain factory deploys ~95-byte `EIP-1167`-shaped clones
   pointing at a single shared implementation. These are structurally
   incompatible deployment strategies, not a matter of a later patch.
2. **Compiler version.** The Moe `MoePair` implementation's own CBOR metadata
   trailer decodes to `{"solc": 0x000814}` (0.8.20). `JoeFactory.sol`/`JoePair.sol`
   at `lfj-gg/joe-core@master` declare `pragma solidity =0.6.12`.

No other candidate repository was identified in this pass. **This source is
therefore admitted as an identified-but-unmatched deployment**: its Classic
(constant-product, two-token) *shape* is confirmed behaviorally, but its exact
swap-fee/rounding implementation must not be assumed to be Uniswap-V2-identical
math translated 1:1, because a customized fork could easily differ in fee
calculation, rounding, or K-invariant enforcement in ways that only a source (or
an independent fixed-block differential fixture) would reveal.

**Blocker (blocks I07):** no upstream Solidity source is matched. I07 needs one
of: (a) explorer/API access once available, (b) a response from the Merchant Moe
team, or (c) DESIGN §2.3's documented fallback — "otherwise a fork simulation of
the pool operation" — i.e. admission entirely from independent fixed-block
quoter/simulation evidence without a source diff. It must not proceed by
assuming `JoePair`-equivalent (or any other) math without one of these.

### 3.5 Merchant Moe Liquidity Book v2.2 — confidence: high (strongest of the five)

| | |
| --- | --- |
| LBFactory | `0xa6630671775c4EA2743840F9A5016dCf2A104054` |
| Example pair | `0xf6c9020c9e915808481757779edb53daceae2415` (WMNT/USDT, binStep 15) |
| LB Router | `0x013e138EF6008ae5FDFDE29700e3f2Bc61d21E3a` |
| LB Quoter | `0x501b8AFd35df20f531fF45F6f695793AC3316c85` |
| Upstream | `lfj-gg/joe-v2` tag `v2.2.0`, commit `1297c3822f0605e643155c35948959c0a0d05e17` |
| License | MIT (repo-root `LICENSE` at the pinned tag) |

**Evidence.** `docs.merchantmoe.com/resources/contracts.md` publishes
`LB Factory = 0xa663...4054`, matching independent on-chain discovery. At the
candidate block: `example_pair.getFactory()` round-trips to that factory;
`factory.getNumberOfLBPairs() = 195` (a real, heavily-populated deployment, not
a placeholder); `example_pair.getTokenX()/getTokenY()` resolve to WMNT/USDT
(`symbol()`/`decimals()` checked); `getBinStep() = 15`. The pair's own CBOR
metadata decodes to `{"ipfs": "...", "solc": 0x000814}` (0.8.20) — exactly
matching `lfj-gg/joe-v2@v2.2.0`'s `foundry.toml` (`solc_version = "0.8.20"`).
The pair is itself a ~97-byte minimal-proxy clone, matching that repository's
own `src/libraries/ImmutableClone.sol` deployment pattern (an `LBFactory` never
embeds full `LBPair` bytecode per pair — it clones one implementation, same
architecture as Moe Classic's clone but a different, matched, source this
time). The GitHub tag name itself, `v2.2.0`, matches the "v2.2" label
DESIGN §1.2/Dune use. This is the strongest, most independently corroborated
match of the five sources.

**Migration scope (for I08).** `src/LBPair.sol`'s `swap`, `src/libraries/`:
`PriceHelper` (bin id ↔ price), `BinHelper`, `FeeHelper` (static **and**
dynamic/volatility-accumulator fee state — DESIGN §2.2's "an LB swap may update
its dynamic-fee state within a plan" — `PairParameterHelper`'s packed
`volatilityAccumulator`/`volatilityReference`/`idReference`/
`timeOfLastUpdate` fields), `Uint128x128Math`/`Uint256x256Math` for the
fixed-point bin math, and `SafeCast`. All under MIT, so no BUSL/change-date
consideration is needed here (unlike Uniswap v3, §3.1).

**Blocker (blocks I08):** same explorer/API access gap as the v3 sources — no
`getsourcecode` key, `explorer.mantle.xyz` down, no Sourcify match. The
`v2.2.0` match above is from independent behavioral/metadata fingerprinting, not
an explorer source diff, so I08 should still try to obtain one (to rule out any
Merchant-Moe-specific patch on top of vanilla `v2.2.0`) before finalizing
admission.

## 4. License summary

| Source | Candidate/confirmed upstream | License | Notes |
| --- | --- | --- | --- |
| Uniswap v3 | `Uniswap/v3-core@v1.0.0` | GPL-2.0-or-later | Was BUSL-1.1; Change Date 2023-04-01 already passed. |
| Agni v3 | `pancakeswap/pancake-v3-contracts` (candidate) | GPL-2.0-or-later | Identity of Agni's *deployment* is on-chain-confirmed; the *source pin* is a matched candidate, not confirmed. |
| FusionX v3 | same candidate as Agni | GPL-2.0-or-later | Higher-confidence address match than Agni (official docs + on-chain agree). |
| Moe Classic v1 | none matched | n/a | Do not adopt `lfj-gg/joe-core`'s GPL-3.0 code for this source — it does not match. |
| Moe LB v2.2 | `lfj-gg/joe-v2@v2.2.0` | MIT | Strongest overall match; simplest license. |

Per the owner's 2026-09-24 decision (`docs/references/0.1.0-execution-decisions.md`):
private internal research reuse of suitable upstream code is agent-autonomous —
no separate licensing-approval gate — provided pinned source/version notices are
retained and the repository's visibility/redistribution posture is not changed
incidentally. All licenses identified above (GPL-2.0-or-later, MIT) are
compatible with that private, non-redistributing internal use; nothing here
found a BUSL-restricted-and-not-yet-changed file or a no-derivatives license
that would need a different answer. This determination is scoped to the
sources actually matched above — a future confirmed Agni/FusionX commit pin
must still be checked for its own license header before translation, and the
still-unmatched Moe Classic source (whatever it turns out to be) has not been
cleared at all.

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
4. Only after (2) and (3) agree does the address enter `config/protocols.yaml`
   as an `example_pool`/`example_pair` — which is exactly what
   `snapshot/preflight.py`'s per-family checks re-verify on every run (so a
   future catalog edit that adds a pool without doing this round-trip fails the
   preflight, not silently).

This is why every address in §3 has an explicit "round-trip" sentence: the
round-trip *is* the emitter-to-pool validation, not a separate step done once
and forgotten.

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
exist per-factory (Agni's was not located in this pass — use the raw
`tickBitmap`+`ticks` walk for Agni until/unless one is found).

This was **probed, not fully executed**, against the Agni example pool at the
candidate block: `tickBitmap(0)` returned all zeros (word 0 is far from the
current price); the word actually containing the current tick
(`slot0().tick=280457`, `tickSpacing=10` → compressed tick `28045` → word `109`)
returned `0xfff...ffeff...fff` — **255 of 256 bits set**, i.e. genuinely dense,
real initialized-tick data, confirming the method is RPC-readable on the public
endpoint at this block. Walking the full envelope for a real corpus is I04/I05's
job, not this one's (explicitly out of scope: "Full dataset capture").

**Liquidity Book (Merchant Moe v2.2).** Bins are not bitmap-indexed the same
way; recovery is via `LBPair.getActiveId()` for the current bin plus
`getNextNonEmptyBin(swapForY, id)` (present in the `v2.2.0` `LBPair` ABI) walked
outward in both directions from the active id until the envelope is covered, or
via the `LBQuoter` periphery contract (`0x501b...c85`, deployed and code-present
at the candidate block) if it exposes an equivalent batch view. Each bin's full
state is `getBin(id) -> (binReserveX, binReserveY)` plus the pair-level packed
parameters (`getStaticFeeParameters`, `getVariableFeeParameters` — the
volatility accumulator DESIGN §2.2 calls out as needing exact modeling of
same-plan dynamic-fee transitions). This was not probed on-chain in this pass
(the `getNumberOfLBPairs`/`getFactory`/`getTokenX`/`getTokenY`/`getBinStep`
checks in §3.5 establish the pair is real and readable; full bin enumeration is
I08's job) — recorded here as the identified method, per DESIGN's "identify...
the method," not as executed recovery.

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
3. For every configured contract, checks code is present at the exact block; a
   pruned/never-deployed/mistyped address fails here, per contract, with its
   `source_key`/`contract_role` attached.
4. Re-runs every identity round-trip and fee-tier/state-shape check narrated in
   §3, failing on any disagreement (`identity_mismatch`, `fee_tier_mismatch`,
   `missing_state`) or unreachable state (`call_reverted`, `rpc_unavailable`).

None of these ever substitute `latest` for the pinned block, merge results from
two different blocks, or return a fabricated "ok" when a read fails — a failed
read is always a reported issue, never silently absent from the report. Known,
already-investigated gaps (the explorer/API access blockers in §3) are reported
as `known_blockers`, separately from `issues`, so a genuinely new regression is
never confused with old, accepted news, and vice versa.

Offline behavior (all of §7's rejection paths, exercised with a fully scripted
fake transport, no network) is tested in `tests/snapshot/test_preflight.py`. The
real, network-touching run whose output is quoted throughout §3 is retained,
redacted, at `docs/references/preflight-evidence-2026-09-24.json` (`ok: true`,
`rpc_call_count: 48`, chain id `5000`, block hash matching §2 — the public RPC's
default URL carries no credential, so nothing was stripped, but `redact_url()`
in `snapshot/preflight.py` would strip one if a future `--rpc-url` override had
one).

## 8. Consolidated blockers (do not silently shrink the five-source scope)

All five sources are **admitted into the catalog and preflight** — none was
dropped. Two independent kinds of gap remain, both recorded per source in
`config/protocols.yaml`'s `blockers` list and surfaced in every preflight run's
`known_blockers`:

1. **Explorer-verified-source access.** `api.mantlescan.xyz` now requires a paid
   Etherscan-V2 API key (none provisioned this pass); `explorer.mantle.xyz`
   returned `502` for the entire session; mantlescan's web UI returns `403` to
   non-browser fetches; Sourcify has no full/partial match for any address in
   this catalog on chain `5000`. This affects **all five** sources' final
   byte-exact commit confirmation (blocks I03/I04/I05/I08 to varying degrees —
   Moe LB and Uniswap v3 already have strong independent corroboration without
   it; Agni/FusionX/Moe-Classic need it more).
2. **No matched upstream source for Merchant Moe Classic v1** (§3.4) — the only
   source with `confidence: unmatched`. This blocks I07 specifically until one
   of the three documented resolutions (explorer access, a team response, or a
   source-free differential-fixture admission per DESIGN §2.3) is available.

Neither gap is papered over with a generic AMM formula or an assumed source: I07
is explicitly told not to proceed on an assumption, and I03/I04/I05/I08 inherit
the "candidate, not confirmed" upstream pins verbatim rather than an upgraded,
unearned "confirmed."

## 9. Verification / reproduction

```bash
uv run pytest tests/snapshot/test_preflight.py   # offline, no network
uv run python -m snapshot.preflight               # explicit online run (needs network)
```

The online command is idempotent and safe to re-run (bounded retries/backoff,
in-run response cache, never falls back to a different block/endpoint); it
prints and can also write (`--output`) the full redacted evidence JSON quoted
throughout this document.

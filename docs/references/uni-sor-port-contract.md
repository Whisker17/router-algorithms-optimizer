# Uniswap SOR source, parity and provenance contract (WHI-1442 / I17)

Status: **contract of record for WHI-1443 (I22, harness + goldens) and WHI-1444 (I18,
Python port).** Written against the pinned upstream source, and checked by calling
the actual pinned functions in a scratch Node probe (§11). This contract does not
contain the harness, the goldens or the port.

Machine-readable pins and hashes: [`uni-sor-source-inventory.json`](uni-sor-source-inventory.json).
Verbatim notices: [`licenses/`](licenses/). `tests/routing/test_uni_sor_contract.py`
checks that this document, the inventory and the notice files still agree.

Normative words: **MUST** / **MUST NOT** are requirements for I22/I18. Behaviour IDs
(`B-*`), adaptations (`A-*`), deviations (`D-*`) and golden categories (`G-*`) are
stable identifiers. Parity tests, the port's source-mapping comments and result
metadata cite them.

---

## 1. Pin

| Item | Value |
| --- | --- |
| Repository | `https://github.com/Uniswap/smart-order-router` |
| Commit (pin) | `04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647` (2026-04-02, "fix: pin npm to specific version in release workflow (#966)") |
| `package.json` version | `4.31.10`; `"license": "GPL"` (not a valid SPDX id; the actual terms are in `LICENSE`) |
| `src/` tree at pin | `7c84edc9e890eea42935500884286ee4338af4fb` |
| `LICENSE` | GPL-3.0 text, sha256 `3972dc97…986`. No "or later" grant, no per-file headers |
| npm `4.31.10` tarball | integrity `sha512-Bmg46K…H74Q==`, sha256 `647fb12b…24e4`, `gitHead` `f506b99b8a7bf4d6f9c386ee721e0f8adb61d7f9` |
| npm ↔ pin relation | `f506b99b` is an ancestor of the pin. `git diff f506b99b 04c7c0b4` touches only 4 `.github/workflows` files. **The `src/` tree hashes are identical**, so the npm build of 4.31.10 is source-equivalent to the pin |

A pin refresh is an explicit provenance change. It MUST re-verify every row above and
every line reference in §3, then regenerate the goldens and re-run port parity
(DESIGN §2.7).

## 2. Parity boundary

In scope: the **exact-input V2/V3 routing core**. Given identical ordered candidate
pools, percentage grid, per-(route, percent) quote table and abstract gas-score
table, the port MUST reproduce upstream's enumerated routes, selected route set,
per-route percentages and final route order, bit for bit. §5 defines the integer
normalization applied after that.

Route families in scope: **V3** (Agni v3, FusionX v3, Uniswap v3 on Mantle),
**V2** (Merchant Moe Classic v1), and **MIXED** V2+V3 paths. MIXED is in scope
because `computeAllMixedRoutes` reuses the same `computeAllRoutes` DFS, and its
output goes through the same combination step.

Out of scope, and never silently emulated:

| Excluded | Upstream location | Why / replacement |
| --- | --- | --- |
| Candidate pool selection (subgraph top-N, TVL heuristics, base tokens, token-list/validator filtering, FOT/STF filtering) | `functions/get-candidate-pools.ts`, `providers/*subgraph*`, `providers/token-validator-provider.ts`, quoters' `getRoutes` | Replaced by the benchmark's frozen cohort (A-1) |
| On-chain quoting, multicall batching, retries, caches | `providers/on-chain-quote-provider.ts`, `providers/v2/quote-provider.ts`, `providers/caching/*`, cached-routes path of `alpha-router.ts` | Replaced by frozen quote tables / benchmark simulator (A-2) |
| Chain gas models (V2/V3 heuristic, tick-crossing gas, L1 data fee, gas-token pricing) | `gas-models/**`, `HAS_L1_FEE` branches of `best-swap-route.ts` | Replaced by an abstract per-route gas score (A-3); the L1 branch is disabled (A-4) |
| V4 pools, hooks, the fake ETH/WETH V4 pool, `hooksOptions`, `shouldEnableMixedRouteEthWeth` | `computeAllV4Routes`, V4 parts of `computeAllMixedRoutes`, `V4RouteWithValidQuote` | Not on Mantle cohort. The port MUST reject V4 inputs |
| Merchant Moe Liquidity Book | none (not an SOR protocol) | Cohort exclusion. LB-only cases are `unsupported` (§6) |
| Exact Output | `TradeType.EXACT_OUTPUT` branches | Benchmark is exact-input only. The port MUST reject it |
| Native-currency routes, wrap/unwrap | `routeHasNativeTokenInputOrOutput` path, `nativeOnChain` | Cohort uses ERC-20 tokens only (A-7) |
| Portion/fee-taking (`SwapType.UNIVERSAL_ROUTER`) | `providers/portion-provider.ts` | `swapConfig` is always `undefined`, so routes pass through unchanged (A-6) |
| `forceCrossProtocol`, `forceMixedRoutes` debug flags | `best-swap-route.ts` L58-74, L232, L852-857 | Fixed `false`. The port MUST reject `true` |
| Trade/calldata/method-parameter building, simulation, swap-and-add | rest of `alpha-router.ts`, `util/methodParameters.ts` | Plan executes in the benchmark evaluator (DESIGN §2.5) |
| Metrics, logging, `FixedReverseHeap` "top 3" bookkeeping | `metric.*`, `log.*`, `bestSwapsPerSplit` | No effect on selection. The heap is only consumed inside a `log.info` argument |

## 3. Included upstream functions and source-to-behaviour mapping

Line numbers are at the pin.

| Upstream function | File:lines | Behaviour IDs | Port target (I18) |
| --- | --- | --- | --- |
| `computeAllRoutes` | `src/routers/alpha-router/functions/compute-all-routes.ts:161-271` | B-R1…B-R6 | `routing/algorithms/uni_sor_port.py::compute_all_routes` |
| `computeAllV3Routes`, `computeAllV2Routes` | same file `:52-86` | B-R7 | `compute_all_routes(family=…)` |
| `computeAllMixedRoutes` (V2+V3 subset only) | same file `:88-159` | B-R8 | `compute_all_routes(family="mixed")` |
| `AlphaRouter.getAmountDistribution` (private) | `src/routers/alpha-router/alpha-router.ts:3348-3362` | B-A1 | `amount_distribution` |
| quote-list assembly order + null-drop | `quoters/v3-quoter.ts:203-248`, `quoters/v2-quoter.ts:224-255`, `quoters/mixed-quoter.ts:271-…`, `alpha-router.ts:2916-3122` | B-Q1…B-Q3 | `build_route_quotes` |
| `V2/V3/MixedRouteWithValidQuote` constructors (quote, gas, `quoteAdjustedForGas`, `poolIdentifiers`) | `entities/route-with-valid-quote.ts:89-160, 183-262, 389-…` | B-Q4, B-Q5 | `RouteQuote` dataclass |
| `getBestSwapRoute` | `functions/best-swap-route.ts:42-172` | B-S1, B-F2 | `get_best_swap_route` |
| `getBestSwapRouteBy` | `functions/best-swap-route.ts:174-817` | B-S2…B-S12, B-F1 | `get_best_swap_route_by` |
| `findFirstRouteNotUsingUsedPools` | `functions/best-swap-route.ts:821-881` | B-S9 | `find_first_route_not_using_used_pools` |
| V8 `Array.prototype.sort` small-array path, used by the final route sort | V8 `third_party/v8/builtins/array-sort.tq` `CountAndMakeRun`, `BinaryInsertionSort`, `ReverseRange` | B-F1 | `v8_small_array_sort` |

### 3.1 Route enumeration (`computeAllRoutes`)

- **B-R1 DFS order.** A recursive DFS over the candidate pool list. At every depth
  it iterates pools in index order `0…n-1` and skips pools already used on the
  current path (`poolsUsed[i]`). The routes it emits are in DFS pre-order. The route
  list order is behaviour: it drives later quote-list order and tie outcomes.
- **B-R2 Hop bound.** `if (currentRoute.length > maxHops) return` runs *before*
  the terminal check, so the maximum emitted route length is exactly `maxHops`.
  `maxHops` is `maxSwapsPerPath`, which maps to `search.max_hops`.
- **B-R3 Terminal rule.** A path is emitted, and the DFS stops extending it, as soon
  as the **last pool involves `tokenOut`**. No path ever passes through `tokenOut`.
- **B-R4 Token cycle rule.** `tokensVisited` starts as `{lower(tokenIn)}`. A pool is
  skipped if its other token (`token0.equals(prev) ? token1 : token0`) is already
  visited. Keys are lowercase addresses.
- **B-R5 Parallel pools.** Distinct pools with the same token pair (different fee
  tiers or factories) are distinct array entries. They yield distinct routes, in
  index order.
- **B-R6 Pool identity inside enumeration** is the array index (object identity),
  not an address.
- **B-R7 Family isolation.** V3 routes come only from the V3 pool list, and V2
  routes only from the V2 pool list.
- **B-R8 Mixed.** The DFS runs over `V3 pools ++ V2 pools`, in that concatenation
  order (from `mixed-quoter.ts:139-147`, with V4 absent). Routes whose pools are all
  V3 or all V2 are then filtered out, keeping DFS order.

### 3.2 Amount grid and route-quote list

- **B-A1 Grid.** `for i = 1; i <= 100 / d; i++: percents.push(i*d); amounts.push(amount × Fraction(i*d, 100))`.
  `d` is `distributionPercent`, which maps to `search.percent_step`. `100/d` is JS
  floating-point division. **Amounts are exact rationals, not floored.** For
  example, 101 at 25 % is `2525/100`. Integer quoting uses the `quotient` (floor).
  If `d` does not divide 100, there is no 100 % entry. The port MUST reproduce the
  loop rather than validate it away, and the profile validator separately
  requires a divisor (DESIGN §2.12).
- **B-Q1 List order.** `routesWithValidQuotes` is the concatenation of the family
  blocks in upstream quoter order: **V3, then V2, then MIXED**. V4 is absent. The
  order comes from the `quotePromises.push` sequence at `alpha-router.ts:2916-3058`
  and the order-preserving `Promise.all`. Within a block the order is route order
  (B-R1), then percent ascending.
- **B-Q2 Drop rule.** An entry whose quote is `null` is dropped. Remaining entries
  keep their relative order. Upstream keeps a numeric zero quote from the V3/mixed
  quoters: a `BigNumber` object is truthy.
- **B-Q3 Percent key.** Each entry carries `percent = percents[i]` of the amount it
  was quoted at.
- **B-Q4 Gas-adjusted quote.** For exact input,
  `quoteAdjustedForGas = quote − gasCostInToken`. It is an exact integer and **may
  be negative**. Negative values compare correctly. This was verified with sdk-core
  7.10.1.
- **B-Q5 Route pool identifiers.** `poolIdentifiers` holds one id per hop, in hop
  order. It is used only for overlap exclusion (B-S9).

### 3.3 Combination (`getBestSwapRoute` / `getBestSwapRouteBy`)

- **B-S1 Grouping.** Group entries by `percent` into arrays, keeping list order.
- **B-S2 Per-percent sort.** Each array is sorted in place with
  `(a, b) => by(a).greaterThan(by(b)) ? -1 : 1`, where `by = quoteAdjustedForGas`.
  The comparator never returns 0, but V8 TimSort only ever tests `order < 0` (see
  `array-sort.tq`), so returning 1 on a tie is indistinguishable from returning 0.
  **The result is a stable sort by `quoteAdjustedForGas` descending.** Ties keep
  B-Q1 order. This was verified against Node 24.16.0 on random arrays up to 1500
  elements (§11). The port MAY use Python's stable `sorted(key=-value)` for this
  sort only.
- **B-S3 100 % baseline.** If a 100 % group exists and `minSplits <= 1`, then
  `bestQuote = by(sorted[100][0])` and `bestSwap = [sorted[100][0]]`. Otherwise
  there is no baseline, and BFS still runs.
- **B-S4 Seeding.** The loop runs `for i = percents.length; i >= 0; i--`. Index
  `percents.length` is out of range and is skipped. For each percent that has a
  group, in **descending index order**, enqueue the best entry as a node
  `{curRoutes:[best], percentIndex:i, remainingPercent:100−p}`. If a second entry
  exists, enqueue it immediately after (the upstream `special` flag). 100 % seeds
  are enqueued too. They have remaining 0, so they expand to nothing but still
  count in the layer size.
- **B-S5 Layers.** `splits` starts at 1. Each outer iteration first increments
  `splits`, then checks the two stop rules in this order:
  1. **Pruning:** stop if `splits >= 3 && bestSwap && bestSwap.length < splits − 1`.
     A 3-way search therefore runs only if a 2-way split beat everything so far, and
     so on.
  2. **Split cap:** stop if `splits > maxSplits`. `maxSplits` maps to
     `search.max_splits`. With `maxSplits = 1`, only the baseline can win.

  It then processes exactly the nodes that were queued when the layer started
  (`layer = queue.size`), FIFO. Nodes enqueued during the layer belong to the next
  layer. The loop also ends when the queue is empty.
- **B-S6 Expansion.** For a node, iterate `i = percentIndex … 0` (descending). Skip
  a percent that exceeds `remainingPercent`, or that has no group. Along any node
  chain, percents are therefore non-increasing, and one percent may repeat.
- **B-S7 Greedy per-percent choice.** For each such percent, take only the
  **first** entry in its sorted group that passes B-S9. If none passes, skip it.
- **B-S8 Completion.** If `remainingPercent − p == 0` and `splits >= minSplits`, the
  candidate is complete. Its value is `Σ by(r)` in `curRoutes` order, plus
  `gasCostL1QuoteToken`, which is always 0 under A-4. It replaces the best only if
  it is **strictly** greater (`quoteCompFn(new, best)`): among equal totals, the
  first one discovered wins, and a baseline wins ties against later splits. Any
  other result, including `remaining == 0` with `splits < minSplits`, is enqueued
  as `{curRoutes+[r], remaining−p, percentIndex:i}`.
- **B-S9 Overlap exclusion.** A candidate entry is rejected if any of its
  `poolIdentifiers` is already in any route of `curRoutes`. The native/wrapped-native
  exclusion (L861-875) cannot trigger without native routes (A-7). The
  `forceCrossProtocol` branch is disabled.
- **B-S10 No route.** If `bestSwap` was never set, `getBestSwapRouteBy` returns
  `undefined` and `getBestSwapRoute` returns `null`.
- **B-S11 Reported totals.** These are cached, not re-quoted. `quote = Σ quote`,
  `quoteGasAdjusted = Σ quoteAdjustedForGas`, `estimatedGasUsed = Σ gasEstimate`,
  `estimatedGasUsedQuoteToken = Σ gasCostInToken`, all over the selected routes.
- **B-S12 Parameters.** `minSplits` (upstream default 1, and the port default 1),
  `maxSplits`, `maxSwapsPerPath` and `distributionPercent` are the only routing
  parameters in the boundary. Upstream chain defaults are 5 % / 7 splits / 3 hops.
  The benchmark trial profile is 5 % / 4 splits / 3 hops (DESIGN §2.12). Results
  MUST record the values used.

### 3.4 Final order and remainder

- **B-F1 Final route order.** `bestSwap.sort((A, B) => B.amount.greaterThan(A.amount) ? 1 : -1)`.
  On a tie this comparator returns −1 for both argument orders. That makes it
  **inconsistent**, so the outcome is defined only by V8's algorithm. Here the array
  length is at most `maxSplits`, and for fewer than 64 elements V8 TimSort does one
  `CountAndMakeRun` from index 0 and then `BinaryInsertionSort`s the rest.
  `CountAndMakeRun` treats a leading run of equal amounts as *strictly descending*
  and reverses it. For two equal 50 % routes found as `[X, Y]`, the returned order
  is `[Y, X]` (confirmed in §11).
  The port MUST implement this procedure exactly (`v8_small_array_sort`, translated
  from `array-sort.tq`, PSF notice per §9.3). It MUST NOT rely on Python's
  `list.sort`, even though CPython 3.12/3.13 happened to agree in §11. It MUST
  reject `max_splits >= 64`.
  Amounts compare as exact rationals. For a positive input this is the same as
  comparing percents.
- **B-F2 Upstream remainder step** (`best-swap-route.ts:127-150`).
  `missingAmount = amount − Σ route.amount` is computed on the exact rationals.
  If it is > 0, it is added to the **last route of the post-B-F1 array**. With an
  exact-input percent grid summing to 100, `Σ route.amount == amount` exactly, so
  **upstream never adds a remainder**. Each route keeps `amount × p/100` as a
  fraction, and the integer quotients can sum to less than `amount`: 101 wei at
  50/50 gives quotients 50 + 50 = 100. Goldens MUST record this (G-11), and the port
  MUST NOT pretend upstream filled the input. D-1 defines the benchmark's integer
  fill.

## 4. Provider substitutions and adaptations

These inputs are replaced. Every result produced by the harness or the port MUST
list the IDs that applied.

| ID | Upstream component | Substitute (harness **and** port use the same inputs) |
| --- | --- | --- |
| A-1 | Candidate pools (subgraph + top-N selection, TVL order) | The frozen matched V2/V3 cohort from the snapshot bundle. The per-family list is **ordered by ascending lowercase `pool_id`**. Mixed uses `V3 list ++ V2 list` (B-R8) |
| A-2 | On-chain / off-chain quote providers | A frozen `(route_index, percent) → raw_quote \| null` table. In the port, the table comes from the benchmark simulator. Each entry chains `quote_exact_in` through the route's pools from the frozen snapshot state, independently per entry, at `quotient(amount × p/100)`. An entry is `null` when that integer input is 0, when a hop fails (insufficient liquidity, or state outside the captured range, which is reported as incomplete snapshot rather than dropped silently), or, for pure-V2 routes, when any hop outputs 0 (mirrors v2-sdk `InsufficientInputAmountError`) |
| A-3 | Chain gas models | An abstract gas score per `(route_index, percent)`: `gas_estimate` (int) and `gas_cost_in_quote_token` (int, output-token raw units, ≥ 0), with an optional `gas_cost_usd_raw` that only affects reported USD totals. The harness implements `IGasModel.estimateGasCost` as a table lookup. In the port the table comes from a declared cost provider. In `gross_only` objective mode every score is `(0, 0)`, so SOR selects on raw quotes. Any non-zero provider MUST be named in results |
| A-4 | `chainId` (Mantle 5000 is not an sdk-core `ChainId`, has no `usdGasTokensByChain` entry, and would make `getBestSwapRouteBy` throw) | The harness runs as `ChainId.MAINNET` (1) with synthetic token addresses. Chain 1 is not in `HAS_L1_FEE`, so the L1-fee branches are skipped and `gasCostL1QuoteToken = 0`. Token addresses MUST NOT equal mainnet WETH `0xC02a…6Cc2`. Mantle's L1 data fee belongs to the benchmark cost model, not the parity boundary |
| A-5 | Pool identity (`v3PoolProvider.getPoolAddress(token0, token1, fee)` and `v2PoolProvider.getPoolAddress(token0, token1)`: a CREATE2 address from one factory per chain) | A **route-scoped pool-id provider**. Immediately before constructing each `*RouteWithValidQuote`, the harness sets the route being constructed. The provider then returns that route's k-th pool `pool_id` on the k-th call, and asserts that the call count equals the hop count and that the tokens/fee match. This keeps the actual constructors while giving each **physical** pool its own identity. It matters because Agni, FusionX and Uniswap v3 on Mantle can each hold a pool with the same `(token0, token1, fee)`, which upstream's single-factory identity would conflate. The port's identity key is the verified `pool_id` |
| A-6 | Portion provider | The real `PortionProvider` with `swapConfig = undefined`, which is a pass-through for exact input (`portion-provider.ts:201-217`) |
| A-7 | Native currency | None in inputs. The native/wrapped-native exclusion is dead code under this boundary |
| A-8 | Logger | `setGlobalLogger(bunyan-blackhole)` or a stderr logger. Log arguments are still evaluated, so the harness MUST use chain 1 (A-4), where `routeToString` can resolve `V3_CORE_FACTORY_ADDRESSES[1]` |

## 5. Deliberate deviations of the benchmark port

These are declared, reported, and never counted as parity failures.

- **D-1 Integer fill.** After B-F1, the port turns upstream's selection into an
  executable plan (DESIGN §2.5 full-fill policy). For routes in B-F1 order
  `r_1 … r_k`, route `j < k` receives the explicit integer `quotient(amount × p_j / 100)`,
  which is the amount it was quoted at. The last route `r_k` receives
  `ALL_REMAINING` of the request fund, which is `quotient_k + residual` with
  `residual = amount − Σ_j quotient_j ∈ [0, k−1]`. This implements upstream's stated
  intent ("add the missing amount to the last route in the array", L145) on
  integers, where upstream's rational arithmetic never triggers it (B-F2).
  Selection parity is judged **before** D-1. D-1 is tested separately.
- **D-2 Physical pool identity.** A-5 is a deviation only where two admitted pools
  share `(token0, token1, fee)` (or a V2 token pair). Upstream cannot represent that
  case. Goldens pin the adapted behaviour (G-9).
- **D-3 Independent re-quote.** The evaluator re-simulates the final integer plan on
  a fresh transaction-local state. Differences from upstream's cached `quote`
  (B-S11) are reported next to selection parity, never instead of it. Routes are
  pool-disjoint (B-S9), so the only source of difference is D-1's residual on
  `r_k`.
- **D-4 Cohort scope.** LB pools never enter SOR candidates.

## 6. Port interface, statuses and plan shape (I18)

- Registry name `uni_sor_port`. Capability is declared as `V2/V3 exact-input, split,
  multi-hop, pool-disjoint`.
- Status mapping:
  - `unsupported`: the full-universe DFS (same B-R rules and hop bound, over all
    admitted pools including LB) yields at least one route, but the cohort DFS
    yields none. This covers LB-only cases.
  - `no_route`: both DFS yield no routes, or B-S10 occurs.
  - `invalid_plan`: only as returned by the independent evaluator.
- Plan: one chain of `SwapStep`s per selected route, emitted in B-F1 order. The
  first step of route `j` consumes `FundInput(REQUEST, quotient_j)`, or
  `ALL_REMAINING` for `r_k`. Each later hop consumes `ALL_REMAINING` of its
  predecessor's output fund. Every route's last output fund is target-token terminal
  balance.
- Result metadata MUST include:
  - the upstream pin and npm integrity;
  - the contract path;
  - the applied `A-*` / `D-*` IDs;
  - B-S12 parameter values;
  - the gas-score provider;
  - candidate/route/quote counts;
  - the upstream-shaped selection (route pool ids, percents, rational amounts,
    cached quotes);
  - the D-1 residual.

## 7. Harness contract (I22, `tools/upstream/uni_sor/`)

Validation-only. It is not timed, it is not a runtime fallback, and ordinary
benchmark replay never needs it.

1. **Toolchain.**
   - Node `v24.16.0`, pinned via `.nvmrc`/`engines`; record `process.versions`.
   - `package.json` depends on the npm tarball `@uniswap/smart-order-router@4.31.10`
     (integrity from §1). Its `overrides` MUST equal the inventory's
     `harness_dependency_pins.direct_overrides`, which are the upstream lockfile
     versions at the pin.
   - Commit `package.json` and `package-lock.json`. Do not commit `node_modules/`.
   - Install with `npm ci --ignore-scripts`.
   - Why the overrides matter: without them, npm resolves newer
     sdk-core/universal-router-sdk, and module loading breaks (§11).
2. **Calling the actual functions.**
   - First `require('@uniswap/smart-order-router')` (the package root), then load
     deep modules from `build/main/…`: `routers/alpha-router/functions/{compute-all-routes,best-swap-route}.js`,
     `routers/alpha-router/entities/route-with-valid-quote.js`,
     `providers/portion-provider.js`. Deep-requiring before the root fails with a
     circular-import `TypeError`.
   - Call `computeAllV3Routes`, `computeAllV2Routes` and `computeAllMixedRoutes`
     for enumeration, the real `V2/V3/MixedRouteWithValidQuote` constructors, and
     `getBestSwapRoute(amount, percents, list, TradeType.EXACT_INPUT, 1, cfg, new PortionProvider())`.
   - `cfg` is exactly `{minSplits, maxSplits, forceCrossProtocol: false, forceMixedRoutes: false}`.
     It has no `gasToken`: a set `gasToken` requires `gasCostInGasToken` and throws
     otherwise (L752-778).
   - For mixed routes, the A-5 provider serves both the `v3PoolProvider` and
     `v2PoolProvider` arguments, with one shared per-route call counter. The
     `v4PoolProvider` stub MUST throw if it is called.
   - Use real `Token`, `CurrencyAmount` and `Fraction` from the pinned sdk-core.
     Build amounts exactly as B-A1 does.
   - The harness MUST NOT reimplement any B-* logic in JS. Grouping and sorting
     happen inside upstream code.
   - An optional diagnostic may call `getBestSwapRouteBy` on a harness-grouped copy
     and read back the in-place-sorted per-percent arrays (B-S2). Its selection MUST
     equal the primary call's.
3. **Objects.**
   - V3 pools: `new Pool(t0, t1, fee, sqrtRatioAtTick(0), liquidity, 0)`. The state
     is irrelevant because quotes come from the table.
   - V2: `new Pair(CurrencyAmount(t0, r0), CurrencyAmount(t1, r1))`. Reserves are
     irrelevant.
   - Tokens: `new Token(1, address, decimals, symbol)` with lowercase-distinct
     addresses.
   - Every case uses fresh objects, because `getBestSwapRoute` mutates route
     objects.
4. **Input fixture** (`tests/fixtures/uni_sor/<case>.input.json`):
   - `case_id`, `amount_in_raw`, `token_in`, `token_out`, and the token table;
   - per-family ordered pool lists (`pool_id`, tokens, fee);
   - `max_hops`, `percent_step`, `min_splits`, `max_splits`;
   - quote table rows `{family, route_pool_ids, percent, raw_quote|null, gas_estimate, gas_cost_in_quote_token, gas_cost_usd_raw}`.
   The table is keyed by route pool-id sequence. The harness MUST fail if a
   table route is not enumerated, or if an enumerated route is missing from the
   table without being explicitly `null`.
5. **Golden output** (`<case>.golden.json`):
   - provenance: upstream pin, npm integrity, lockfile sha256, `process.versions`,
     harness git revision, input sha256, and the list of upstream functions called;
   - `routes` per family, in upstream order (B-R1/B-R8);
   - the ordered `routesWithValidQuotes` as passed (B-Q1/B-Q2);
   - `result` is `null` or:
     - ordered `routes[]`, each with `protocol`, `pool_ids`, `token_path`, `percent`,
       `amount` as `{numerator, denominator, quotient}`, `quote`,
       `quote_adjusted_for_gas`, `gas_estimate`, `gas_cost_in_token`;
     - `missing_amount` as a rational, and `sum_of_quotients`;
     - totals `quote`, `quote_gas_adjusted`, `estimated_gas_used`,
       `estimated_gas_used_quote_token`.
   Integers are decimal strings. Output is canonical JSON with sorted keys and a
   trailing newline.
6. **Determinism.** Generate twice and require byte-identical outputs and hashes.
   Nothing in the boundary depends on time or randomness. `Date.now()` feeds only
   metrics. If nondeterminism is ever observed, it MUST be demonstrated and
   canonicalized explicitly under a contract amendment.
7. **As implemented (WHI-1443).** These choices implement §7.1–§7.6 without changing
   any pin or B-/A-/D-/G- item. Details: `tools/upstream/uni_sor/README.md`.
   - The `*RouteWithValidQuote` objects are built by the real `V3Quoter`, `V2Quoter`
     and `MixedQuoter` `.getQuotes`, fed by the frozen table as their quote
     providers. The null drop (B-Q2) and in-block order (B-Q1) are therefore upstream
     code too. Only the V3 ++ V2 ++ MIXED concatenation is glue.
   - The A-5 provider learns the route being constructed from the table gas model.
     Every constructor calls `estimateGasCost(this)` before it resolves
     `poolIdentifiers`.
   - The "harness git revision" is recorded as the git blob ids of the harness files. A
     commit id cannot be embedded in files that the same commit adds.
     `git log --find-object=<blob>` recovers the commit.
   - Goldens also record the npm version, the SHA-256 of the executed upstream build
     files, `remainder_added` (whether B-F2 replaced a route amount) and the
     diagnostic per-percent sorted groups as quote-list indices.
   - Two regenerations from a fresh `npm ci` produced byte-identical files, and no
     nondeterminism was observed.

## 8. Required golden categories (I22 produces, I18 must pass)

Each category needs at least one case whose expected selection **differs** from the
naive alternative named. That way a port missing the behaviour fails. Assertions
cover route lists, selected pool ids, percents, rational amounts and the final
order, not just the total quote.

| ID | Category | What the case must pin |
| --- | --- | --- |
| G-1 | No route: disconnected tokens; routes exist but every quote is `null` | `result: null`, empty/non-empty route lists respectively (B-R, B-Q2, B-S10) |
| G-2 | Enumeration order and hop bound | ≥ 3-hop graph with parallel pools and a path that is exactly `max_hops` long and one that would be `max_hops+1` (B-R1, B-R2, B-R5) |
| G-3 | Terminal/cycle rules | A graph where extending through `tokenOut`, or revisiting `tokenIn` or an intermediate token, would add routes (B-R3, B-R4) |
| G-4 | Mixed family | V2+V3 path present, pure paths excluded from the mixed list, V3++V2 candidate order observable, and the full list order V3, V2, MIXED (B-R8, B-Q1) |
| G-5 | Competing percentages | A split where a non-best 100 % route and uneven splits (e.g. 70/30 vs 50/50) compete, and the winner is a multi-way split (B-S4…B-S8) |
| G-6 | Second-best seed | The optimum reachable only from a percent's second entry, while greedy best-first expansion misses it (B-S4) |
| G-7 | Greedy first-non-overlapping choice | The best entry for a percent overlaps, so the next non-overlapping one is chosen, and a globally better overlapping combination is excluded (B-S7, B-S9) |
| G-8 | Pool overlap | Shared intermediate pool across two paths, shared first hop, and disjoint paths through the same intermediate token, which is allowed (B-S9) |
| G-9 | Same `(token0,token1,fee)` on two physical pools | Selected as disjoint under A-5 (D-2) |
| G-10 | Split bounds and pruning | Ordered as below (B-S3, B-S5, B-S8): `max_splits=1`; `max_splits=2` stopping a better 3-way; the `splits>=3` pruning rule stopping a 3-way that would have been better; `min_splits=2` with a better 100 % route; `min_splits > 1` suppressing the baseline |
| G-11 | Nondivisible input | e.g. 101 at 50/50 and 7 at 5 % steps. Upstream `missing_amount = 0` and `sum_of_quotients < amount` are recorded, and D-1 is exercised by the port tests (B-A1, B-F2) |
| G-12 | Ties | These tie cases are all required: equal `quoteAdjustedForGas` within a percent (stable, B-S2); equal completed totals (first found wins, baseline wins over an equal split, B-S8); equal final amounts in 2- and 3-route selections, including equal-leading and mixed positions (V8 reversal, B-F1) |
| G-13 | Gas score effects | Higher raw quote loses after gas; negative `quoteAdjustedForGas`; zero raw quote kept (B-Q2, B-Q4) |
| G-14 | Grid edge | A zero-quotient small percent entry `null`-dropped; a `percent_step` that does not divide 100, so there is no 100 % key (B-A1, B-S3) |

I18 parity test (`tests/routing/test_uni_sor_parity.py`) runs these checks:

1. enumerated routes equal `routes`;
2. the port's quote list equals the recorded list;
3. the selection before D-1 equals `result`: the same ordered routes, percents and
   rational amounts;
4. separately, D-1 allocation conservation, and the evaluator re-quote per D-3.

## 9. Source, license and distribution inventory

Assumptions: the repository is **private, internal research**, and its visibility is
unchanged. Nothing is conveyed to third parties. The repo has no top-level
`LICENSE`, and this contract does not add or change one. What follows is an
engineering record, not a legal opinion. DESIGN §2.7 and
`0.1.0-execution-decisions.md` authorize routine permitted reuse without a separate
approval gate.

### 9.1 Harness (`tools/upstream/uni_sor/`)

- **Runs** unmodified GPL-3.0 SOR code in one Node process with MIT/Apache-2.0
  libraries.
- **Installed but not behaviour-relevant:** GPL-2.0-or-later, GPL-3.0-or-later,
  LGPL-3.0 and MPL-2.0 packages, plus `@uniswap/v3-core`. That package still says
  BUSL-1.1 in its metadata, but its Change Date of 2023-04-01 has passed, so it is
  GPL-2.0-or-later. Full list in the inventory.
- **Obligation assessment:** GPL-3.0 §2 lets you run and modify covered works you
  do not convey without conditions. Internal use is permitted. The harness glue
  calls GPL code in-process, so treat it as a work based on SOR: mark it
  `SPDX-License-Identifier: GPL-3.0-only`.
- **Notices:**
  - ship `tools/upstream/uni_sor/NOTICE.md` naming the pin, the npm integrity and
    this contract;
  - copy (or point to) `docs/references/licenses/uniswap-smart-order-router-04c7c0b4-LICENSE.txt`;
  - `node_modules` keeps each package's own license files and is never committed.
- **Concrete restriction for current use:** none.

### 9.2 Generated fixtures (`tests/fixtures/uni_sor/`)

- **Inputs:** synthetic, authored in this repo. Future variants derived from the
  frozen Mantle snapshot contain public on-chain facts plus benchmark-computed
  quotes.
- **Outputs:** numeric selections produced by running the program. GPL-3.0 §2
  covers program output only when the content itself is a covered work, and these
  goldens contain no upstream code or expression.
- **Obligations:** none beyond provenance. Each golden records the upstream pin,
  integrity, and the upstream functions called (§7.5). A short
  `tests/fixtures/uni_sor/README.md` states "generated by the pinned GPL-3.0 SOR via
  tools/upstream/uni_sor; data, not code".
- **Concrete restriction:** none.

### 9.3 Translated Python port (`routing/algorithms/uni_sor_port.py`)

- **Nature:** a translation of GPL-3.0 functions (§3), which makes it a modified
  version / work based on SOR. Its V8 sort helper translates PSF-2.0 TimSort macros
  (§3.4). PSF-2.0 is permissive and GPL-compatible.
- **Required notices in the port file header:**
  1. `SPDX-License-Identifier: GPL-3.0-only`;
  2. "Translated from Uniswap/smart-order-router@04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647 (4.31.10)";
  3. a pointer to the GPL text copy;
  4. a statement that it was modified: translated to Python, with A-/D- adaptations;
  5. for `v8_small_array_sort`: "Translated from V8 13.6.233.17 third_party/v8/builtins/array-sort.tq; Copyright Python Software Foundation; PSF-2.0" and a pointer to
     `docs/references/licenses/v8-13.6.233.17-third_party-builtins-LICENSE.txt`.

  Every translated function carries a comment citing its upstream file:lines and
  B-* IDs.
- **Distribution assumption:** private and unconveyed, so there is no present
  obligation beyond notices. Conveying the benchmark with the port later would
  require GPL-3.0 compliance for the covered work. That is a future decision
  outside this ticket, and it does not block current use.
- **Concrete restriction:** none. The port MUST NOT strip notices, and MUST NOT
  replace B-* behaviour with a generic heuristic.

### 9.4 Dependencies whose semantics the port reproduces (not their code)

| Package | Version | License | Semantics reproduced |
| --- | --- | --- | --- |
| `@uniswap/sdk-core` | 7.10.1 | MIT | Exact rational `Fraction`/`CurrencyAmount` add/subtract/compare, `quotient` = floor |
| `mnemonist` `Queue` | 0.38.5 | MIT | FIFO |
| `lodash` | 4.17.21 | MIT | Order-preserving `map`/`filter`/`flatMap` |
| `jsbi`, `@ethersproject/bignumber` | 3.2.5, 5.7.0 | Apache-2.0, MIT | Arbitrary-precision integers (Python `int`) |

## 10. Refresh / amendment procedure

Changing the pin, Node version, dependency overrides or any B-/A-/D-/G- item
requires all of the following:

- edit this contract and the inventory in the same PR;
- re-run the §11 checks;
- regenerate the goldens;
- re-run port parity.

`test_uni_sor_contract.py` fails if the pin, npm integrity or notice hashes drift
between the documents.

## 11. Verification performed for this contract (2026-09-24)

1. **Source checks.** I cloned the repo at the pin and read both in-scope files
   in full, plus their transitive in-scope dependencies: route-with-valid-quote,
   quoters, portion provider, `util/routes.ts`, chains and config. I verified these
   git facts:
   - the pin commit;
   - the `src/` tree hashes at the pin and at the npm `gitHead`;
   - ancestry and diff scope between the two;
   - LICENSE and file blob hashes;
   - the npm registry metadata and tarball sha256.
2. **Actual-upstream probe.** I installed the npm tarball with the §7 overrides
   into a scratch directory under Node v24.16.0 / V8 13.6.233.17-node.49. Without
   the overrides, a fresh resolution fails at module load
   (`universal-router-sdk` vs `sdk-core`). With them, I called the real
   `computeAllV3Routes` and `getBestSwapRoute`, using the route-scoped pool-id
   provider (A-5) and a table gas model (A-3):
   - Enumeration over `[AB500, AC, CB, AB3000]` returned
     `[AB500, AC>CB, AB3000]` (B-R1, B-R5).
   - For 101 wei on a 25 % grid, the amounts were `2525/100, 5050/100, 7575/100, 10100/100` (B-A1).
   - With equal quotes on all routes, the selection was two 50 % routes, returned
     as `[AC>CB, AB500]`. That reverses discovery order (B-F1).
   - Each amount was `5050/100` and no remainder was added: quotients 50 + 50 =
     100 < 101 (B-F2).
3. **Sort semantics.** Against Node's `Array.prototype.sort` with both upstream
   comparators:
   - A model of V8's small-array path (`CountAndMakeRun` + `BinaryInsertionSort`,
     checked against `array-sort.tq` at V8 13.6.233.17) matched on 400,000 random
     arrays of length 1–63.
   - The per-percent comparator matched a stable descending sort on 2,000 random
     arrays of length up to 1500.
   - A Python translation of the model matched Node on 3,000 cases.
   - CPython 3.12.7 and 3.13.13 `list.sort` also matched, which is incidental and
     not relied on (B-F1).
4. **Behavioural constants.** Mantle (5000) is absent from sdk-core `ChainId` and
   from `usdGasTokensByChain`. Chain 1 is absent from `HAS_L1_FEE`. A negative
   `CurrencyAmount` compares correctly (A-4, B-Q4).

The probe was scratch work outside the repo. It is not the I22 harness and
produced no goldens.

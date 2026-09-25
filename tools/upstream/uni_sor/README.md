# `tools/upstream/uni_sor` — Uniswap SOR golden harness (WHI-1443)

Validation-only. It runs the **actual pinned** `@uniswap/smart-order-router@4.31.10`
routing core on frozen inputs and writes the goldens in `tests/fixtures/uni_sor/` that
the Python port (WHI-1444, `routing/algorithms/uni_sor_port.py`) must reproduce. It is not
a timed solver, not a runtime fallback and not a quote service: `uv run pytest` and every
benchmark run read the checked-in goldens and never need Node. Contract:
`docs/references/uni-sor-port-contract.md` §7 (harness) and §8 (golden categories).
Notices: [`NOTICE.md`](NOTICE.md).

## Regenerate

Requires Node `v24.16.0` (`.nvmrc`; `nvm use`), npm, `uv`, and registry access for the
first `npm ci`.

```bash
tools/upstream/uni_sor/regen.sh                 # author inputs, npm ci, generate, re-check
SKIP_NPM_CI=1 tools/upstream/uni_sor/regen.sh   # reuse an existing install
uv run pytest tests/routing/test_sor_fixture_schema.py   # offline checks, no Node
```

`regen.sh` runs `author_inputs.py` (inputs), `npm ci --ignore-scripts` (the committed
lock), `node generate.js` (goldens + `MANIFEST.json`), then `node generate.js --check`,
which regenerates everything in memory and fails unless every file is byte-identical. It
prints one digest over all fixture files; two runs print the same digest.

`generate.js` fails closed before loading upstream if Node/V8 differ from the inventory
(`docs/references/uni-sor-source-inventory.json`), if `package.json` overrides differ
from the inventory's `direct_overrides`, or if `node_modules/` differs from the committed
lock in any package version or integrity.

## What runs, and what is glue

| Step | Actual upstream code (from `build/main/`) | Harness glue |
| --- | --- | --- |
| Grid (B-A1) | `AlphaRouter.prototype.getAmountDistribution` | — |
| Enumeration (B-R*) | `computeAllV3Routes`, `computeAllV2Routes`, `computeAllMixedRoutes` (V3 ++ V2 candidates), `mixedRouteFilterOutV4Pools` | builds `Token`/`Pool`/`Pair` with the pinned SDK instances |
| Quote list (B-Q*) | `V3Quoter` / `V2Quoter` / `MixedQuoter` `.getQuotes` (null drop, real `*RouteWithValidQuote` constructors) | frozen quote table as the on-chain quote providers (A-2); table gas model (A-3); V3 ++ V2 ++ MIXED concatenation as `alpha-router.ts` does after `Promise.all` (B-Q1) |
| Pool identity (A-5) | constructors call `getPoolAddress` per hop | route-scoped provider: the table gas model, which every constructor calls before resolving `poolIdentifiers`, sets the route; the k-th call returns its k-th `pool_id`, checking tokens/fee and the call count |
| Combination (B-S*, B-F*) | `getBestSwapRoute(amount, percents, list, EXACT_INPUT, 1, cfg, new PortionProvider())` | serialization only |
| Diagnostic (B-S2) | `getBestSwapRouteBy` on a fresh, harness-grouped copy | reads the in-place-sorted per-percent arrays; asserts the same selection |

Chain 1 is impersonated (A-4) with synthetic token addresses (or the corpus' Mantle
addresses; never mainnet WETH); `bunyan-blackhole` is the global logger (A-8).

The harness validates each input before running: every table route must be enumerated by
upstream, every enumerated `(route, percent)` must have a row (possibly `null`), every row
must be consumed, and an integer input of 0 must be `null` (A-2).

## Inputs

`author_inputs.py` writes `tests/fixtures/uni_sor/<case>.input.json`:

- `g01…g14_*` — synthetic graphs and quote/gas tables, one or more per contract category,
  each with a hand-derived `expect` block and the naive alternative it defeats;
- `c01`, `c02` — the `sor_compatible` V2/V3 cohort of the frozen corpus fixture
  `tests/fixtures/corpus/bundle` (block 101,082,044) in ascending `pool_id` order (A-1),
  with quotes from the benchmark simulator per A-2 and `gross_only` zero gas.

## Provenance in each golden

Upstream pin, npm integrity and git head; SHA-256 of the executed upstream build files;
package versions and integrities from the lock; `package-lock.json` SHA-256;
`process.versions` and the npm version; the git blob ids of the harness files (the
harness revision — a commit id cannot be embedded in files that the same commit adds;
`git log --find-object=<blob>` finds it); the input SHA-256; and the ordered list of
upstream functions called.

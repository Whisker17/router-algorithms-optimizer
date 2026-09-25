// SPDX-License-Identifier: GPL-3.0-only
//
// Validation-only golden harness for the Uniswap SOR port (WHI-1443 / I22).
//
// Runs the ACTUAL pinned @uniswap/smart-order-router@4.31.10 routing core (commit
// 04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647, npm tarball integrity in NOTICE.md) on the
// frozen inputs in tests/fixtures/uni_sor/*.input.json and writes *.golden.json.
// This file is glue that calls GPL-3.0 code in-process, so it is marked GPL-3.0-only
// (docs/references/uni-sor-port-contract.md §9.1). It is not a timed solver and not a
// runtime dependency of the Python benchmark.
//
// What is upstream and what is glue (contract §7.2):
//   upstream  computeAllV3Routes / computeAllV2Routes / computeAllMixedRoutes,
//             mixedRouteFilterOutV4Pools, AlphaRouter.prototype.getAmountDistribution,
//             V3Quoter / V2Quoter / MixedQuoter .getQuotes (null-drop, list order,
//             the real *RouteWithValidQuote constructors), getBestSwapRoute (grouping,
//             sorting, BFS, remainder, final sort, PortionProvider), and in the optional
//             diagnostic getBestSwapRouteBy.
//   glue      object construction from the input (Token/Pool/Pair from the pinned SDKs),
//             the frozen quote table standing in for on-chain quote providers (A-2), the
//             table gas model (A-3), the route-scoped pool-id provider (A-5), the
//             V3 ++ V2 ++ MIXED concatenation that alpha-router.ts performs after
//             Promise.all(quotePromises) (B-Q1), and serialization.
// No B-* routing behaviour is reimplemented here.
//
// Usage (from this directory, after `npm ci --ignore-scripts`):
//   node generate.js           write every golden + MANIFEST.json
//   node generate.js --check   regenerate in memory; fail unless byte-identical on disk
// The documented one-shot command is ./regen.sh (README.md).

'use strict';

const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const HERE = __dirname;
const REPO = path.resolve(HERE, '..', '..', '..');
const FIXTURES = path.join(REPO, 'tests', 'fixtures', 'uni_sor');
const INVENTORY_PATH = path.join(REPO, 'docs', 'references', 'uni-sor-source-inventory.json');
const CONTRACT = 'docs/references/uni-sor-port-contract.md';
const INPUT_SUFFIX = '.input.json';
const GOLDEN_SUFFIX = '.golden.json';
const INPUT_SCHEMA = 'uni-sor-golden-input/1';
const GOLDEN_SCHEMA = 'uni-sor-golden/1';
const MANIFEST_SCHEMA = 'uni-sor-golden-manifest/1';
const COMMAND = 'tools/upstream/uni_sor/regen.sh';
const MAINNET_WETH = '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2';
const HARNESS_FILES = [
  'tools/upstream/uni_sor/.nvmrc',
  'tools/upstream/uni_sor/author_inputs.py',
  'tools/upstream/uni_sor/generate.js',
  'tools/upstream/uni_sor/package-lock.json',
  'tools/upstream/uni_sor/package.json',
  'tools/upstream/uni_sor/regen.sh',
];

// ---------------------------------------------------------------------------------
// Toolchain preflight (contract §7.1): fail closed before touching upstream code.

const inventory = JSON.parse(fs.readFileSync(INVENTORY_PATH, 'utf8'));
const pins = inventory.harness_dependency_pins;

function preflight() {
  assert.equal(process.version, pins.node.version, `Node ${pins.node.version} required (.nvmrc)`);
  assert.equal(process.versions.v8, pins.node.v8, `V8 ${pins.node.v8} required`);
  const pkg = JSON.parse(fs.readFileSync(path.join(HERE, 'package.json'), 'utf8'));
  assert.deepEqual(pkg.overrides, pins.direct_overrides, 'package.json overrides != inventory');
  assert.deepEqual(pkg.dependencies, { [inventory.upstream.package_name]: inventory.upstream.package_version });
  const lock = JSON.parse(fs.readFileSync(path.join(HERE, 'package-lock.json'), 'utf8'));
  const sorKey = `node_modules/${inventory.upstream.package_name}`;
  assert.equal(lock.packages[sorKey].version, inventory.upstream.package_version);
  assert.equal(lock.packages[sorKey].integrity, inventory.npm_artifact.integrity, 'SOR tarball integrity');
  // node_modules must be exactly what `npm ci` installs from the committed lock.
  const hidden = JSON.parse(fs.readFileSync(path.join(HERE, 'node_modules', '.package-lock.json'), 'utf8'));
  for (const [key, entry] of Object.entries(lock.packages)) {
    if (key === '') continue;
    const got = hidden.packages[key];
    if (!got && entry.optional) continue; // platform-specific optional package skipped by npm
    assert.ok(got, `${key} missing from node_modules; run npm ci --ignore-scripts`);
    assert.equal(got.version, entry.version, `${key} installed version`);
    assert.equal(got.integrity, entry.integrity, `${key} installed integrity`);
  }
  for (const key of Object.keys(hidden.packages)) assert.ok(lock.packages[key], `${key} not in lock`);
  return lock;
}

const LOCK = preflight();

// ---------------------------------------------------------------------------------
// Load the pinned upstream. Root first, then deep modules (contract §7.2: deep-requiring
// before the root fails with a circular-import TypeError). Every SDK is resolved from the
// SOR package so instanceof checks see the same module instances upstream uses.

const SOR_MAIN = require.resolve('@uniswap/smart-order-router', { paths: [HERE] });
const SOR_DIR = path.resolve(path.dirname(SOR_MAIN), '..', '..');
const SOR_BUILD = path.join(SOR_DIR, 'build', 'main');
const sor = require(SOR_MAIN);
const deep = (rel) => require(path.join(SOR_BUILD, rel));
const fromSor = (name) => {
  const resolved = require.resolve(name, { paths: [SOR_DIR] });
  assert.ok(resolved.startsWith(path.join(HERE, 'node_modules') + path.sep), resolved);
  return require(resolved);
};

const UPSTREAM_FILES = [
  'routers/alpha-router/functions/compute-all-routes.js',
  'routers/alpha-router/functions/best-swap-route.js',
  'routers/alpha-router/entities/route-with-valid-quote.js',
  'routers/alpha-router/quoters/v3-quoter.js',
  'routers/alpha-router/quoters/v2-quoter.js',
  'routers/alpha-router/quoters/mixed-quoter.js',
  'routers/alpha-router/alpha-router.js',
  'providers/portion-provider.js',
  'util/mixedRouteFilterOutV4Pools.js',
];
const computeAll = deep(UPSTREAM_FILES[0]);
const bestSwap = deep(UPSTREAM_FILES[1]);
const { PortionProvider } = deep(UPSTREAM_FILES[7]);
const { mixedRouteFilterOutV4Pools } = deep(UPSTREAM_FILES[8]);
const { V3Quoter, V2Quoter, MixedQuoter, AlphaRouter, V3RouteWithValidQuote, V2RouteWithValidQuote,
  MixedRouteWithValidQuote, DAI_MAINNET, usdGasTokensByChain, HAS_L1_FEE, setGlobalLogger } = sor;

const sdkCore = fromSor('@uniswap/sdk-core');
const v3sdk = fromSor('@uniswap/v3-sdk');
const v2sdk = fromSor('@uniswap/v2-sdk');
const routerSdk = fromSor('@uniswap/router-sdk');
const { BigNumber } = fromSor('@ethersproject/bignumber');
const JSBI = fromSor('jsbi');

const CHAIN_ID = sdkCore.ChainId.MAINNET; // A-4
assert.equal(CHAIN_ID, 1);
assert.ok(!HAS_L1_FEE.includes(CHAIN_ID), 'chain 1 must have no L1-fee branch (A-4)');
assert.ok(DAI_MAINNET.equals(usdGasTokensByChain[CHAIN_ID][0]), 'gas USD token of chain 1');
setGlobalLogger(fromSor('bunyan-blackhole')('uni-sor-golden-harness')); // A-8

const PROTOCOL = { V3: routerSdk.Protocol.V3, V2: routerSdk.Protocol.V2, MIXED: routerSdk.Protocol.MIXED };
const FAMILIES = ['V3', 'V2', 'MIXED'];

// ---------------------------------------------------------------------------------
// Canonical serialization (contract §7.5): sorted keys, 2-space indent, trailing newline.

// Hand-rolled so the bytes equal Python's json.dumps(obj, indent=2, sort_keys=True,
// ensure_ascii=True) + "\n" (JS objects enumerate integer-like keys numerically, so
// JSON.stringify on a key-sorted object is not enough).
function serialize(value, indent) {
  if (value === null || typeof value === 'boolean') return String(value);
  if (typeof value === 'number') {
    assert.ok(Number.isSafeInteger(value), `non-integer number ${value} in output`);
    return String(value);
  }
  if (typeof value === 'string') {
    assert.match(value, /^[\x20-\x7e]*$/, 'output strings must be printable ASCII');
    return JSON.stringify(value);
  }
  const pad = ' '.repeat(indent + 2);
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]';
    return `[\n${value.map((v) => pad + serialize(v, indent + 2)).join(',\n')}\n${' '.repeat(indent)}]`;
  }
  assert.equal(typeof value, 'object', `unserializable ${typeof value}`);
  const keys = Object.keys(value).filter((k) => value[k] !== undefined).sort();
  if (keys.length === 0) return '{}';
  const body = keys.map((k) => `${pad}${JSON.stringify(k)}: ${serialize(value[k], indent + 2)}`);
  return `{\n${body.join(',\n')}\n${' '.repeat(indent)}}`;
}
const canonical = (value) => serialize(value, 0) + '\n';
const sha256 = (data) => crypto.createHash('sha256').update(data).digest('hex');
const gitBlob = (data) =>
  crypto.createHash('sha1').update(`blob ${data.length}\0`).update(data).digest('hex');

// ---------------------------------------------------------------------------------
// Input validation (contract §7.4). Structural only: no routing logic.

const DECIMAL = /^-?(0|[1-9][0-9]*)$/;
const UINT = /^(0|[1-9][0-9]*)$/;

function validateInput(input, file) {
  const where = (msg) => `${file}: ${msg}`;
  assert.equal(input.schema, INPUT_SCHEMA, where('schema'));
  assert.equal(`${input.case_id}${INPUT_SUFFIX}`, path.basename(file), where('case_id/file name'));
  assert.equal(input.chain_id, CHAIN_ID, where('chain_id must be 1 (A-4)'));
  assert.match(input.amount_in_raw, UINT, where('amount_in_raw'));
  assert.ok(BigInt(input.amount_in_raw) > 0n, where('amount_in_raw must be positive'));
  const r = input.routing;
  for (const key of ['max_hops', 'percent_step', 'min_splits', 'max_splits']) {
    assert.ok(Number.isInteger(r[key]) && r[key] > 0, where(`routing.${key}`));
  }
  assert.ok(r.max_splits < 64, where('max_splits >= 64 is outside the contract (B-F1)'));
  const tokens = new Map();
  for (const t of input.tokens) {
    assert.match(t.address, /^0x[0-9a-f]{40}$/, where(`token ${t.address} must be lowercase hex`));
    assert.notEqual(t.address, MAINNET_WETH, where('mainnet WETH is not allowed (A-4)'));
    assert.ok(!tokens.has(t.address), where(`duplicate token ${t.address}`));
    tokens.set(t.address, t);
  }
  assert.ok(tokens.has(input.token_in) && tokens.has(input.token_out), where('token_in/out'));
  assert.notEqual(input.token_in, input.token_out, where('token_in == token_out'));
  const ids = new Set();
  for (const fam of ['V3', 'V2']) {
    for (const p of input.pools[fam]) {
      assert.ok(typeof p.pool_id === 'string' && p.pool_id.length > 0, where('pool_id'));
      assert.ok(!ids.has(p.pool_id), where(`duplicate pool_id ${p.pool_id}`));
      ids.add(p.pool_id);
      assert.ok(tokens.has(p.token0) && tokens.has(p.token1) && p.token0 !== p.token1, where(`${p.pool_id} tokens`));
      if (fam === 'V3') assert.ok(Number.isInteger(p.fee) && p.fee > 0 && p.fee < 1_000_000, where(`${p.pool_id} fee`));
      else assert.equal(p.fee, undefined, where(`${p.pool_id}: V2 pools carry no fee`));
    }
  }
  assert.deepEqual(Object.keys(input.pools).sort(), ['V2', 'V3'], where('pools families'));
  for (const q of input.quotes) {
    assert.ok(FAMILIES.includes(q.family), where(`quote family ${q.family}`));
    assert.ok(Array.isArray(q.route_pool_ids) && q.route_pool_ids.length > 0, where('route_pool_ids'));
    assert.ok(Number.isInteger(q.percent), where('percent'));
    if (q.raw_quote === null) {
      for (const k of ['gas_estimate', 'gas_cost_in_quote_token', 'gas_cost_usd_raw']) {
        assert.equal(q[k], null, where(`${k} must be null for a null quote`));
      }
    } else {
      for (const k of ['raw_quote', 'gas_estimate', 'gas_cost_in_quote_token', 'gas_cost_usd_raw']) {
        assert.match(q[k], UINT, where(`${k}`));
      }
    }
  }
}

// ---------------------------------------------------------------------------------
// One "world" = fresh upstream objects for one case (contract §7.3: getBestSwapRoute
// mutates route objects, so every call gets its own).

function buildWorld(input) {
  const tokens = new Map();
  for (const t of input.tokens) tokens.set(t.address, new sdkCore.Token(CHAIN_ID, t.address, t.decimals, t.symbol));
  const idOf = new Map(); // pool object -> pool_id (A-5 identity)
  const v3Pools = input.pools.V3.map((p) => {
    const pool = new v3sdk.Pool(tokens.get(p.token0), tokens.get(p.token1), p.fee,
      v3sdk.TickMath.getSqrtRatioAtTick(0), JSBI.BigInt('1000000000000000000'), 0);
    idOf.set(pool, p.pool_id);
    return pool;
  });
  const v2Pools = input.pools.V2.map((p) => {
    const pair = new v2sdk.Pair(
      sdkCore.CurrencyAmount.fromRawAmount(tokens.get(p.token0), '1000000000000000000'),
      sdkCore.CurrencyAmount.fromRawAmount(tokens.get(p.token1), '1000000000000000000'));
    idOf.set(pair, p.pool_id);
    return pair;
  });
  return { tokens, idOf, v3Pools, v2Pools, tokenIn: tokens.get(input.token_in), tokenOut: tokens.get(input.token_out) };
}

const routePools = (route) => (route.pairs !== undefined && route.pools === undefined ? route.pairs : route.pools);

function routeIds(world, route) {
  return routePools(route).map((p) => {
    const id = world.idOf.get(p);
    assert.ok(id !== undefined, 'route holds a pool object the harness did not build');
    return id;
  });
}

const routeKey = (family, ids) => `${family}|${ids.join('>')}`;

// A-5 route-scoped pool-id provider. `begin` is called from the table gas model, which
// every *RouteWithValidQuote constructor invokes before it resolves poolIdentifiers; the
// k-th getPoolAddress call then answers the k-th pool of that route.
function makePoolIdProvider(world) {
  const state = { ids: null, pools: null, calls: 0, family: null };
  const finish = () => {
    if (state.ids !== null) assert.equal(state.calls, state.ids.length, 'A-5: call count != hop count');
  };
  const next = (kind, tokenA, tokenB, fee) => {
    assert.ok(state.ids !== null && state.calls < state.ids.length, 'A-5: unexpected getPoolAddress call');
    const pool = state.pools[state.calls];
    if (kind === 'V3') {
      assert.ok(pool instanceof v3sdk.Pool, 'A-5: V3 lookup for a non-V3 hop');
      assert.equal(fee, pool.fee, 'A-5: fee mismatch');
    } else {
      assert.ok(pool instanceof v2sdk.Pair, 'A-5: V2 lookup for a non-V2 hop');
    }
    assert.ok(pool.token0.equals(tokenA) && pool.token1.equals(tokenB), 'A-5: token mismatch');
    const id = state.ids[state.calls];
    state.calls += 1;
    return { poolAddress: id, token0: pool.token0, token1: pool.token1 };
  };
  return {
    begin(route) {
      finish();
      state.pools = routePools(route);
      state.ids = routeIds(world, route);
      state.calls = 0;
    },
    finish,
    v3: { getPoolAddress: (a, b, fee) => next('V3', a, b, fee) },
    v2: { getPoolAddress: (a, b) => next('V2', a, b) },
    v4: {
      getPoolId: () => { throw new Error('V4 pool provider must never be called'); },
      getPools: () => { throw new Error('V4 pool provider must never be called'); },
    },
  };
}

// The frozen quote table (A-2) and table gas model (A-3).
function makeTable(input, world, enumerated, percents, amounts) {
  const rows = new Map();
  const grid = new Set(percents);
  for (const q of input.quotes) {
    const key = `${routeKey(q.family, q.route_pool_ids)}|${q.percent}`;
    assert.ok(!rows.has(key), `duplicate quote row ${key}`);
    assert.ok(enumerated.has(routeKey(q.family, q.route_pool_ids)),
      `quote row for a route upstream did not enumerate: ${routeKey(q.family, q.route_pool_ids)}`);
    assert.ok(grid.has(q.percent), `quote row percent ${q.percent} is not on the upstream grid`);
    const quotient = BigInt(amounts[percents.indexOf(q.percent)].quotient.toString());
    if (quotient === 0n) assert.equal(q.raw_quote, null, `${key}: integer input 0 must be null (A-2)`);
    rows.set(key, q);
  }
  for (const key of enumerated.keys()) {
    for (const p of percents) assert.ok(rows.has(`${key}|${p}`), `no quote row for enumerated ${key} at ${p}%`);
  }
  const used = new Set();
  const lookup = (family, route, percent) => {
    const key = `${routeKey(family, routeIds(world, route))}|${percent}`;
    const found = rows.get(key);
    assert.ok(found, `missing quote row ${key}`);
    used.add(key);
    return found;
  };
  return { rows, used, lookup };
}

function makeGasModel(family, table, poolIds, quoteToken) {
  return {
    estimateGasCost(rq) {
      assert.equal(rq.protocol, PROTOCOL[family]);
      poolIds.begin(rq.route);
      const q = table.lookup(family, rq.route, rq.percent);
      assert.notEqual(q.raw_quote, null, 'gas model called for a null quote');
      assert.equal(rq.rawQuote.toString(), q.raw_quote);
      return {
        gasEstimate: BigNumber.from(q.gas_estimate),
        gasCostInToken: sdkCore.CurrencyAmount.fromRawAmount(quoteToken, q.gas_cost_in_quote_token),
        gasCostInUSD: sdkCore.CurrencyAmount.fromRawAmount(DAI_MAINNET, q.gas_cost_usd_raw),
      };
    },
  };
}

function makeQuoteProvider(family, table, percents, amounts, withV3Fields) {
  return {
    async getQuotesManyExactIn(amountsArg, routes) {
      assert.equal(amountsArg, amounts, 'quoter must receive the upstream grid amounts');
      return {
        routesWithQuotes: routes.map((route) => [route, amounts.map((amount, i) => {
          const q = table.lookup(family, route, percents[i]);
          if (q.raw_quote === null) {
            return withV3Fields
              ? { amount, quote: null, sqrtPriceX96AfterList: null, initializedTicksCrossedList: null, gasEstimate: null }
              : { amount, quote: null };
          }
          const quote = BigNumber.from(q.raw_quote);
          return withV3Fields
            ? { amount, quote, sqrtPriceX96AfterList: [], initializedTicksCrossedList: [], gasEstimate: BigNumber.from(0) }
            : { amount, quote };
        })]),
        blockNumber: BigNumber.from(0),
      };
    },
    async getQuotesManyExactOut() { throw new Error('exact output is out of scope'); },
  };
}

// ---------------------------------------------------------------------------------
// Serialization helpers.

const rational = (ca) => ({
  numerator: ca.numerator.toString(),
  denominator: ca.denominator.toString(),
  quotient: ca.quotient.toString(),
});
const integer = (ca) => {
  assert.equal(ca.denominator.toString(), '1', 'expected an integral CurrencyAmount');
  return ca.numerator.toString();
};
const addr = (t) => t.address.toLowerCase();

// `full` adds the per-route amount and token path (selected routes, contract §7.5); the
// quote-list entries omit them (the grid amount is in `amounts`, the path in `routes`).
function entryJson(rq, list, full) {
  const extra = full ? { token_path: rq.tokenPath.map(addr), amount: rational(rq.amount) } : {};
  return {
    ...extra,
    list_index: list.indexOf(rq),
    protocol: rq.protocol,
    pool_ids: rq.poolIdentifiers,
    percent: rq.percent,
    raw_quote: rq.rawQuote.toString(),
    quote: integer(rq.quote),
    quote_adjusted_for_gas: integer(rq.quoteAdjustedForGas),
    gas_estimate: rq.gasEstimate.toString(),
    gas_cost_in_token: integer(rq.gasCostInToken),
    gas_cost_in_usd: integer(rq.gasCostInUSD),
  };
}

// ---------------------------------------------------------------------------------
// The upstream pipeline for one fresh world.

async function runPipeline(input, calls) {
  const world = buildWorld(input);
  const r = input.routing;
  const amount = sdkCore.CurrencyAmount.fromRawAmount(world.tokenIn, input.amount_in_raw);

  // B-A1: the private AlphaRouter method, called on the prototype (it uses no `this`).
  calls.push('AlphaRouter.prototype.getAmountDistribution');
  const [percents, amounts] = AlphaRouter.prototype.getAmountDistribution.call(
    undefined, amount, { distributionPercent: r.percent_step });

  // B-R*: enumeration, per family (A-1 order is the input order; mixed = V3 ++ V2).
  calls.push('computeAllV3Routes');
  const v3Routes = computeAll.computeAllV3Routes(world.tokenIn, world.tokenOut, world.v3Pools, r.max_hops);
  calls.push('computeAllV2Routes');
  const v2Routes = computeAll.computeAllV2Routes(world.tokenIn, world.tokenOut, world.v2Pools, r.max_hops);
  calls.push('computeAllMixedRoutes');
  let mixedRoutes = computeAll.computeAllMixedRoutes(world.tokenIn, world.tokenOut,
    [...world.v3Pools, ...world.v2Pools], r.max_hops, false, undefined);
  calls.push('mixedRouteFilterOutV4Pools');
  mixedRoutes = mixedRouteFilterOutV4Pools(mixedRoutes);
  const byFamily = { V3: v3Routes, V2: v2Routes, MIXED: mixedRoutes };

  const enumerated = new Map();
  for (const fam of FAMILIES) {
    for (const route of byFamily[fam]) {
      const key = routeKey(fam, routeIds(world, route));
      assert.ok(!enumerated.has(key), `upstream enumerated ${key} twice`);
      enumerated.set(key, route);
    }
  }
  const table = makeTable(input, world, enumerated, percents, amounts);
  const poolIds = makePoolIdProvider(world);
  const cfg = { minSplits: r.min_splits, maxSplits: r.max_splits, forceCrossProtocol: false, forceMixedRoutes: false };
  const exactIn = sdkCore.TradeType.EXACT_INPUT;
  const quoteToken = world.tokenOut;

  // B-Q*: the real quoters build the RouteWithValidQuote objects and drop null quotes.
  const v3Quoter = new V3Quoter(undefined, poolIds.v3, makeQuoteProvider('V3', table, percents, amounts, true),
    undefined, CHAIN_ID, undefined, undefined);
  const v2Quoter = new V2Quoter(undefined, poolIds.v2, makeQuoteProvider('V2', table, percents, amounts, false),
    { buildGasModel: async () => makeGasModel('V2', table, poolIds, quoteToken) },
    undefined, CHAIN_ID, undefined, undefined, undefined);
  const mixedQuoter = new MixedQuoter(undefined, poolIds.v4, undefined, poolIds.v3, undefined, poolIds.v2,
    makeQuoteProvider('MIXED', table, percents, amounts, true), undefined, CHAIN_ID, undefined, undefined);

  calls.push('V3Quoter.getQuotes');
  const v3q = await v3Quoter.getQuotes(v3Routes, amounts, percents, quoteToken, exactIn, cfg, undefined,
    makeGasModel('V3', table, poolIds, quoteToken));
  poolIds.finish();
  calls.push('V2Quoter.getQuotes');
  const v2q = await v2Quoter.getQuotes(v2Routes, amounts, percents, quoteToken, exactIn, cfg, undefined,
    undefined, BigNumber.from(1));
  poolIds.finish();
  calls.push('MixedQuoter.getQuotes');
  const mq = await mixedQuoter.getQuotes(mixedRoutes, amounts, percents, quoteToken, exactIn, cfg, undefined,
    makeGasModel('MIXED', table, poolIds, quoteToken));
  poolIds.finish();

  // alpha-router.ts: quotePromises are pushed V3, V2, MIXED and flattened in that order.
  const list = [...v3q.routesWithValidQuotes, ...v2q.routesWithValidQuotes, ...mq.routesWithValidQuotes];
  for (const [key] of table.rows) assert.ok(table.used.has(key), `quote row ${key} was never consumed`);
  for (const rq of list) {
    assert.ok(rq instanceof V3RouteWithValidQuote || rq instanceof V2RouteWithValidQuote
      || rq instanceof MixedRouteWithValidQuote);
    assert.deepEqual(rq.poolIdentifiers, routeIds(world, rq.route), 'A-5 identities');
  }
  return { world, amount, percents, amounts, byFamily, list, cfg, exactIn };
}

async function runCase(input) {
  const calls = [];
  const primary = await runPipeline(input, calls);
  const { world, amount, percents, amounts, byFamily, list, cfg, exactIn } = primary;
  const gridAmounts = new Map(percents.map((p, i) => [p, amounts[i]]));

  calls.push('getBestSwapRoute');
  const swap = await bestSwap.getBestSwapRoute(amount, percents, list, exactIn, CHAIN_ID, cfg, new PortionProvider());

  // Optional diagnostic (contract §7.2): getBestSwapRouteBy on a harness-grouped fresh
  // copy exposes the in-place-sorted per-percent arrays (B-S2).
  const diagCalls = [];
  const diag = await runPipeline(input, diagCalls);
  const grouped = {};
  for (const rq of diag.list) (grouped[rq.percent] = grouped[rq.percent] || []).push(rq);
  diagCalls.push('getBestSwapRouteBy');
  const diagSwap = await bestSwap.getBestSwapRouteBy(exactIn, grouped, diag.percents, CHAIN_ID,
    (rq) => rq.quoteAdjustedForGas, diag.cfg, new PortionProvider(), undefined, undefined, undefined,
    undefined, undefined);
  const sortedByPercent = diag.percents.filter((p) => grouped[p]).map((p) => ({
    percent: p, list_indices: grouped[p].map((rq) => diag.list.indexOf(rq)) }));
  const shape = (s) => (s ? s.routes.map((rq) => [rq.poolIdentifiers.join('>'), rq.percent]) : null);
  assert.deepEqual(shape(diagSwap), shape(swap), 'diagnostic getBestSwapRouteBy selection != primary');

  let result = null;
  if (swap) {
    const zero = sdkCore.CurrencyAmount.fromRawAmount(world.tokenIn, 0);
    const total = swap.routes.reduce((acc, rq) => acc.add(rq.amount), zero);
    const missing = amount.subtract(total);
    const quotients = swap.routes.reduce((acc, rq) => acc + BigInt(rq.amount.quotient.toString()), 0n);
    result = {
      routes: swap.routes.map((rq) => entryJson(rq, list, true)),
      missing_amount: rational(missing),
      remainder_added: swap.routes.some((rq) => rq.amount !== gridAmounts.get(rq.percent)),
      sum_of_quotients: quotients.toString(),
      quote: integer(swap.quote),
      quote_gas_adjusted: integer(swap.quoteGasAdjusted),
      estimated_gas_used: swap.estimatedGasUsed.toString(),
      estimated_gas_used_quote_token: integer(swap.estimatedGasUsedQuoteToken),
      estimated_gas_used_usd: integer(swap.estimatedGasUsedUSD),
    };
  }
  return {
    calls,
    diagCalls,
    body: {
      percents,
      amounts: percents.map((p, i) => ({ percent: p, ...rational(amounts[i]) })),
      routes: Object.fromEntries(FAMILIES.map((fam) => [fam, byFamily[fam].map((route) => ({
        pool_ids: routeIds(world, route),
        token_path: (route.tokenPath || route.path).map(addr),
      }))])),
      routes_with_valid_quotes: list.map((rq) => entryJson(rq, list, false)),
      diagnostic: { function: 'getBestSwapRouteBy', sorted_by_percent: sortedByPercent, selection_matches_primary: true },
      result,
    },
  };
}

// ---------------------------------------------------------------------------------
// Provenance shared by every golden (contract §7.5).

function npmVersion() {
  return childProcess.execFileSync('npm', ['--version'], { encoding: 'utf8' }).trim();
}

function sharedProvenance() {
  const pkgEntry = (name) => {
    const e = LOCK.packages[`node_modules/${name}`];
    return { version: e.version, integrity: e.integrity };
  };
  const names = [inventory.upstream.package_name, ...inventory.behavior_relevant_dependencies.map((d) => d.name)];
  const harness = {};
  for (const rel of HARNESS_FILES) harness[rel] = gitBlob(fs.readFileSync(path.join(REPO, rel)));
  const upstreamFiles = {};
  for (const rel of UPSTREAM_FILES) upstreamFiles[`build/main/${rel}`] = sha256(fs.readFileSync(path.join(SOR_BUILD, rel)));
  return {
    contract: CONTRACT,
    upstream: {
      repository: inventory.upstream.repository,
      commit: inventory.upstream.commit,
      package: inventory.upstream.package_name,
      version: inventory.upstream.package_version,
      npm_integrity: inventory.npm_artifact.integrity,
      npm_git_head: inventory.npm_artifact.git_head,
      build_files_sha256: upstreamFiles,
    },
    harness: {
      command: COMMAND,
      install: 'npm ci --ignore-scripts',
      git_blob_sha1: harness,
      package_lock_sha256: sha256(fs.readFileSync(path.join(HERE, 'package-lock.json'))),
    },
    toolchain: { process_versions: { ...process.versions }, npm: npmVersion() },
    packages: Object.fromEntries(names.map((n) => [n, pkgEntry(n)])),
    chain_id: CHAIN_ID,
  };
}

// ---------------------------------------------------------------------------------

async function generateAll() {
  const shared = sharedProvenance();
  const inputs = fs.readdirSync(FIXTURES).filter((f) => f.endsWith(INPUT_SUFFIX)).sort();
  assert.ok(inputs.length > 0, 'no inputs; run author_inputs.py first');
  const files = new Map();
  const manifest = [];
  for (const file of inputs) {
    const raw = fs.readFileSync(path.join(FIXTURES, file));
    const input = JSON.parse(raw.toString('utf8'));
    assert.equal(canonical(input), raw.toString('utf8'), `${file} is not canonical JSON`);
    validateInput(input, file);
    let out;
    try {
      out = await runCase(input);
    } catch (err) {
      err.message = `${file}: ${err.message}`;
      throw err;
    }
    const golden = {
      schema: GOLDEN_SCHEMA,
      case_id: input.case_id,
      categories: input.categories,
      input_file: `tests/fixtures/uni_sor/${file}`,
      input_sha256: sha256(raw),
      routing: input.routing,
      routing_config: { minSplits: input.routing.min_splits, maxSplits: input.routing.max_splits,
        forceCrossProtocol: false, forceMixedRoutes: false, maxSwapsPerPath: input.routing.max_hops,
        distributionPercent: input.routing.percent_step },
      adaptations: input.adaptations,
      gas_score_provider: input.gas_score_provider,
      provenance: { ...shared, upstream_calls: out.calls, diagnostic_calls: out.diagCalls },
      ...out.body,
    };
    const name = `${input.case_id}${GOLDEN_SUFFIX}`;
    const text = canonical(golden);
    files.set(name, text);
    manifest.push({ case_id: input.case_id, categories: input.categories,
      input: file, input_sha256: sha256(raw), golden: name, golden_sha256: sha256(text) });
  }
  files.set('MANIFEST.json', canonical({
    schema: MANIFEST_SCHEMA,
    command: COMMAND,
    contract: CONTRACT,
    upstream_commit: inventory.upstream.commit,
    npm_integrity: inventory.npm_artifact.integrity,
    package_lock_sha256: shared.harness.package_lock_sha256,
    node: process.version,
    npm: shared.toolchain.npm,
    cases: manifest,
  }));
  return files;
}

async function main() {
  const check = process.argv.includes('--check');
  const files = await generateAll();
  const onDisk = fs.readdirSync(FIXTURES).filter((f) => f.endsWith(GOLDEN_SUFFIX) || f === 'MANIFEST.json');
  const stale = onDisk.filter((f) => !files.has(f));
  if (check) {
    const diffs = [...files].filter(([f, text]) => !fs.existsSync(path.join(FIXTURES, f))
      || fs.readFileSync(path.join(FIXTURES, f), 'utf8') !== text).map(([f]) => f);
    if (diffs.length || stale.length) {
      console.error(`golden drift: changed=${JSON.stringify(diffs)} stale=${JSON.stringify(stale)}`);
      process.exit(1);
    }
    console.log(`check ok: ${files.size} files byte-identical`);
    return;
  }
  for (const f of stale) fs.unlinkSync(path.join(FIXTURES, f));
  for (const [f, text] of files) fs.writeFileSync(path.join(FIXTURES, f), text);
  console.log(`wrote ${files.size} files to tests/fixtures/uni_sor`);
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});

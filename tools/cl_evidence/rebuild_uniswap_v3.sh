#!/usr/bin/env bash
# Independent local recompile of Uniswap v3 core (WHI-1431).
#
# Clones Uniswap/v3-core at the catalog pin (tag v1.0.0), compiles UniswapV3Pool and
# UniswapV3Factory with solc-js 0.7.6 (optimizer runs 800, evm istanbul, bytecodeHash
# none -- exactly v3-core's own hardhat.config.ts, the settings Routescan reports and
# the on-chain CBOR trailer `a164736f6c6343000706000a` implies), and compares each
# runtime bytecode with the deployed code at the catalog candidate block after zeroing
# the compiler-reported immutable sites. For the pool it also prints keccak256 of the
# build (immutables zero), which must equal config/protocols.yaml
# uniswap_v3.cl_collection.pool_code_normalized_hash, and keccak256 of the pool
# *creation* code, which must equal v3-periphery's POOL_INIT_CODE_HASH
# (0xe34f199b...) -- the CREATE2 init-code hash every canonical Uniswap v3 pool address
# is derived from. Uniswap v3 has no separate pool deployer: UniswapV3Factory inherits
# UniswapV3PoolDeployer and CREATE2-deploys each pool itself.
#
# Needs git, node/npm, cast and network access.  tools/cl_evidence/rebuild_uniswap_v3.sh [workdir]
set -euo pipefail
WORK="${1:-$(mktemp -d)}"
RPC_URL="${CL_RPC_URL:-https://rpc.mantle.xyz}"
BLOCK="${CL_FORK_BLOCK:-101057678}"
COMMIT=e3589b192d0be27e100cd0daaf6c97204fdb1899
mkdir -p "$WORK" && cd "$WORK"
[ -d v3-core ] || git clone -q https://github.com/Uniswap/v3-core.git
git -C v3-core checkout -q "$COMMIT"
[ -d node_modules/solc ] || npm install --silent --no-save solc@0.7.6 >/dev/null
for pair in UniswapV3Pool:0x4cdFc22bF05209de87Ee564746Dc7E5174631d2b \
            UniswapV3Factory:0x0d922Fb1Bc191F64970ac40376643808b4B74Df9; do
  name="${pair%%:*}"; addr="${pair##*:}"
  cast code "$addr" -B "$BLOCK" -r "$RPC_URL" > "$name.onchain"
  NAME="$name" node - <<'JS'
const solc = require(require.resolve('solc', {paths: [process.cwd()]}));
const fs = require('fs'), path = require('path');
const root = 'v3-core/contracts';
const sources = {};
(function walk(d) { for (const f of fs.readdirSync(d)) { const p = path.join(d, f);
  if (fs.statSync(p).isDirectory()) { if (f !== 'test') walk(p); }
  else if (p.endsWith('.sol')) sources['contracts/' + path.relative(root, p)] = {content: fs.readFileSync(p, 'utf8')}; } })(root);
const name = process.env.NAME, file = `contracts/${name}.sol`;
const out = JSON.parse(solc.compile(JSON.stringify({language: 'Solidity', sources, settings: {
  optimizer: {enabled: true, runs: 800}, evmVersion: 'istanbul', metadata: {bytecodeHash: 'none'},
  outputSelection: {[file]: {[name]: ['evm.bytecode.object', 'evm.deployedBytecode.object', 'evm.deployedBytecode.immutableReferences']}}}})));
for (const e of out.errors || []) if (e.severity === 'error') { console.error(e.formattedMessage); process.exit(1); }
const evm = out.contracts[file][name].evm, d = evm.deployedBytecode;
const built = Buffer.from(d.object, 'hex');
const onchain = Buffer.from(fs.readFileSync(`${name}.onchain`, 'utf8').trim().slice(2), 'hex');
const zeroed = Buffer.from(onchain); let sites = 0;
for (const refs of Object.values(d.immutableReferences || {})) for (const r of refs) { zeroed.fill(0, r.start, r.start + r.length); sites++; }
console.log(`${name}: solc ${solc.version()}, ${built.length} built / ${onchain.length} on-chain bytes, ` +
  `${sites} immutable site(s) zeroed -> ${built.equals(zeroed) ? 'IDENTICAL' : 'MISMATCH'}`);
fs.writeFileSync(`${name}.built`, built);
fs.writeFileSync(`${name}.creation`, Buffer.from(evm.bytecode.object, 'hex'));
if (!built.equals(zeroed)) process.exitCode = 1;
JS
done
echo "keccak256(UniswapV3Pool build, immutables zero) = $(cast keccak "0x$(xxd -p UniswapV3Pool.built | tr -d '\n')")"
echo "keccak256(UniswapV3Pool creation code) = $(cast keccak "0x$(xxd -p UniswapV3Pool.creation | tr -d '\n')")"

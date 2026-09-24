#!/usr/bin/env bash
# Independent local recompile of FusionX v3 core (WHI-1430; closes the catalog's
# "FusionX not forge-recompiled" residual, docs/references/protocol-admission.md §8).
#
# Clones FusionX-Finance/v3-contracts at the catalog pin, compiles
# FusionXV3Pool/FusionXV3Factory/FusionXV3PoolDeployer with solc-js 0.7.6 (optimizer
# runs 20, evm istanbul, bytecodeHash none -- the settings Routescan reports and the
# on-chain CBOR trailer `a164736f6c6343000706000a` implies), and compares each runtime
# bytecode with the deployed code at the catalog candidate block after zeroing the
# compiler-reported immutable sites. For the pool it also prints keccak256 of the build
# (immutables zero), which must equal config/protocols.yaml
# fusionx_v3.cl_collection.pool_code_normalized_hash.
#
# The pinned commit ships only projects/v3-core; the one external import, the
# IFusionXV3LmPool *interface* (two function signatures), is written from the verified
# pool source's own flattened `@fusionx/v3-lm-pool@v1.0.0` section.
#
# Needs git, node/npm, cast and network access.  tools/cl_evidence/rebuild_fusionx.sh [workdir]
set -euo pipefail
WORK="${1:-$(mktemp -d)}"
RPC_URL="${CL_RPC_URL:-https://rpc.mantle.xyz}"
BLOCK="${CL_FORK_BLOCK:-101057678}"
COMMIT=7f7406e5fbdf610acf0fedac815c29fbca69925a
mkdir -p "$WORK" && cd "$WORK"
[ -d v3-contracts ] || git clone -q https://github.com/FusionX-Finance/v3-contracts.git
git -C v3-contracts checkout -q "$COMMIT"
mkdir -p lm/contracts/interfaces
cat > lm/contracts/interfaces/IFusionXV3LmPool.sol <<'SOL'
// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity >=0.5.0;

interface IFusionXV3LmPool {
  function accumulateReward(uint32 currTimestamp) external;

  function crossLmTick(int24 tick, bool zeroForOne) external;
}
SOL
[ -d node_modules/solc ] || npm install --silent --no-save solc@0.7.6 >/dev/null
for pair in FusionXV3Pool:0x262255f4770aebe2d0c8b97a46287dcecc2a0aff \
            FusionXV3Factory:0x530d2766D1988CC1c000C8b7d00334c14B69AD71 \
            FusionXV3PoolDeployer:0x8790c2C3BA67223D83C8FCF2a5E3C650059987b4; do
  name="${pair%%:*}"; addr="${pair##*:}"
  cast code "$addr" -B "$BLOCK" -r "$RPC_URL" > "$name.onchain"
  NAME="$name" node - <<'JS'
const solc = require(require.resolve('solc', {paths: [process.cwd()]}));
const fs = require('fs'), path = require('path');
const root = 'v3-contracts/projects/v3-core/contracts';
const sources = {};
(function walk(d) { for (const f of fs.readdirSync(d)) { const p = path.join(d, f);
  if (fs.statSync(p).isDirectory()) { if (f !== 'test') walk(p); }
  else if (p.endsWith('.sol')) sources['contracts/' + path.relative(root, p)] = {content: fs.readFileSync(p, 'utf8')}; } })(root);
sources['@fusionx/v3-lm-pool/contracts/interfaces/IFusionXV3LmPool.sol'] =
  {content: fs.readFileSync('lm/contracts/interfaces/IFusionXV3LmPool.sol', 'utf8')};
const name = process.env.NAME, file = `contracts/${name}.sol`;
const out = JSON.parse(solc.compile(JSON.stringify({language: 'Solidity', sources, settings: {
  optimizer: {enabled: true, runs: 20}, evmVersion: 'istanbul', metadata: {bytecodeHash: 'none'},
  outputSelection: {[file]: {[name]: ['evm.deployedBytecode.object', 'evm.deployedBytecode.immutableReferences']}}}})));
for (const e of out.errors || []) if (e.severity === 'error') { console.error(e.formattedMessage); process.exit(1); }
const d = out.contracts[file][name].evm.deployedBytecode;
const built = Buffer.from(d.object, 'hex');
const onchain = Buffer.from(fs.readFileSync(`${name}.onchain`, 'utf8').trim().slice(2), 'hex');
const zeroed = Buffer.from(onchain); let sites = 0;
for (const refs of Object.values(d.immutableReferences || {})) for (const r of refs) { zeroed.fill(0, r.start, r.start + r.length); sites++; }
console.log(`${name}: solc ${solc.version()}, ${built.length} built / ${onchain.length} on-chain bytes, ` +
  `${sites} immutable site(s) zeroed -> ${built.equals(zeroed) ? 'IDENTICAL' : 'MISMATCH'}`);
fs.writeFileSync(`${name}.built`, built);
if (!built.equals(zeroed)) process.exitCode = 1;
JS
done
echo "keccak256(FusionXV3Pool build, immutables zero) = $(cast keccak "0x$(xxd -p FusionXV3Pool.built | tr -d '\n')")"

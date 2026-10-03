#!/usr/bin/env bash
# Regenerate tests/fixtures/concentrated/*.jsonl (WHI-1428 CL evidence).
#
# Explicit, network-touching preparation command. Ordinary `uv run pytest` never runs
# this; it replays the committed JSONL offline. Requires Foundry (forge, cast).
#
#   tools/cl_evidence/regen.sh                 # candidate block from config/protocols.yaml
#   CL_FORK_BLOCK=101057678 tools/cl_evidence/regen.sh
set -euo pipefail
cd "$(dirname "$0")"

RPC_URL="${CL_RPC_URL:-https://rpc.mantle.xyz}"
BLOCK="${CL_FORK_BLOCK:-$(awk '/^candidate_block:/{f=1} f&&/number:/{print $2; exit}' ../../config/protocols.yaml)}"

chain_id="$(cast chain-id --rpc-url "$RPC_URL")"
[ "$chain_id" = "5000" ] || { echo "refusing: chain id $chain_id is not Mantle mainnet (5000)" >&2; exit 1; }
CL_FORK_BLOCK_HASH="$(cast block "$BLOCK" --field hash --rpc-url "$RPC_URL")"
export CL_FORK_BLOCK_HASH
echo "forking Mantle block $BLOCK ($CL_FORK_BLOCK_HASH) via $RPC_URL"

mkdir -p ../../tests/fixtures/concentrated
forge test --fork-url "$RPC_URL" --fork-block-number "$BLOCK" \
  --fork-retries 8 --fork-retry-backoff 2000 --match-contract CaptureCL -vv "$@"
echo "wrote:"; ls -l ../../tests/fixtures/concentrated/*.jsonl

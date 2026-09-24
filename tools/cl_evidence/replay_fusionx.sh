#!/usr/bin/env bash
# Regenerate tests/fixtures/fusionx/evidence.jsonl.gz for a published FusionX bundle (WHI-1430).
# forge streams plain JSON Lines; they are gzipped deterministically (-n) to keep the
# checked-in fixture small (1.6 MB -> ~0.3 MB).
#
# Explicit, network-touching preparation command (Foundry + public Mantle RPC). The
# ordinary `uv run pytest` replays the committed evidence offline.
#
#   tools/cl_evidence/replay_fusionx.sh tests/fixtures/fusionx/bundle
set -euo pipefail
BUNDLE="$(cd "$1" && pwd)"
cd "$(dirname "$0")"
RPC_URL="${CL_RPC_URL:-https://rpc.mantle.xyz}"

uv run --project ../.. python make_replay_requests.py "$BUNDLE" requests/fusionx_v3_replay.json
BLOCK="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["block_number"])' requests/fusionx_v3_replay.json)"
WANT_HASH="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["block_hash"])' requests/fusionx_v3_replay.json)"

chain_id="$(cast chain-id --rpc-url "$RPC_URL")"
[ "$chain_id" = "5000" ] || { echo "refusing: chain id $chain_id is not Mantle mainnet (5000)" >&2; exit 1; }
CL_FORK_BLOCK_HASH="$(cast block "$BLOCK" --field hash --rpc-url "$RPC_URL")"
[ "$CL_FORK_BLOCK_HASH" = "$WANT_HASH" ] || {
  echo "refusing: block $BLOCK hash $CL_FORK_BLOCK_HASH != bundle hash $WANT_HASH" >&2; exit 1; }
export CL_FORK_BLOCK_HASH
echo "forking Mantle block $BLOCK ($CL_FORK_BLOCK_HASH) via $RPC_URL"

mkdir -p ../../tests/fixtures/fusionx
forge test --fork-url "$RPC_URL" --fork-block-number "$BLOCK" \
  --fork-retries 8 --fork-retry-backoff 2000 --match-contract CaptureFusionXReplay -vv
gzip -9 -n -f ../../tests/fixtures/fusionx/evidence.jsonl
echo "wrote:"; ls -l ../../tests/fixtures/fusionx/evidence.jsonl.gz

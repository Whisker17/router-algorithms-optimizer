#!/usr/bin/env bash
# Regenerate tests/fixtures/liquidity_book/{real_<pair>,controlled}.jsonl.gz (WHI-1433 LB evidence).
#
# Explicit, network-touching preparation command (Foundry + public Mantle RPC). The
# ordinary `uv run pytest` replays the committed evidence offline. forge streams plain
# JSON Lines; they are gzipped deterministically (-n).
#
#   tools/cl_evidence/capture_lb.sh                      # candidate block from config/protocols.yaml
#   tools/cl_evidence/capture_lb.sh --match-test controlled
set -euo pipefail
cd "$(dirname "$0")"

RPC_URL="${CL_RPC_URL:-https://rpc.mantle.xyz}"
BLOCK="${CL_FORK_BLOCK:-$(awk '/^candidate_block:/{f=1} f&&/number:/{print $2; exit}' ../../config/protocols.yaml)}"

chain_id="$(cast chain-id --rpc-url "$RPC_URL")"
[ "$chain_id" = "5000" ] || { echo "refusing: chain id $chain_id is not Mantle mainnet (5000)" >&2; exit 1; }
CL_FORK_BLOCK_HASH="$(cast block "$BLOCK" --field hash --rpc-url "$RPC_URL")"
export CL_FORK_BLOCK_HASH
echo "forking Mantle block $BLOCK ($CL_FORK_BLOCK_HASH) via $RPC_URL"

OUT=../../tests/fixtures/liquidity_book
mkdir -p "$OUT"
forge test --fork-url "$RPC_URL" --fork-block-number "$BLOCK" \
  --fork-retries 8 --fork-retry-backoff 2000 --gas-limit 100000000000 --match-contract CaptureLB -vv "$@"
for f in "$OUT"/*.jsonl; do [ -s "$f" ] && gzip -9 -n -f "$f"; done
echo "wrote:"; ls -l "$OUT"/*.jsonl.gz

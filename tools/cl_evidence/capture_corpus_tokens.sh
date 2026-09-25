#!/usr/bin/env bash
# Regenerate tests/fixtures/corpus/token_transfers.jsonl for a published corpus bundle
# (WHI-1436): exact-transfer evidence for every universe token at the corpus block.
#
# Explicit, network-touching preparation command (Foundry + public Mantle RPC). The
# ordinary `uv run pytest` checks the committed evidence offline.
#
#   tools/cl_evidence/capture_corpus_tokens.sh data/corpus/mantle-5src-101082044/bundle
set -euo pipefail
BUNDLE="$(cd "$1" && pwd)"
cd "$(dirname "$0")"
RPC_URL="${CL_RPC_URL:-https://rpc.mantle.xyz}"

uv run --project ../.. python make_corpus_token_requests.py "$BUNDLE" requests/corpus_tokens.json
BLOCK="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["block_number"])' requests/corpus_tokens.json)"
WANT_HASH="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["block_hash"])' requests/corpus_tokens.json)"

chain_id="$(cast chain-id --rpc-url "$RPC_URL")"
[ "$chain_id" = "5000" ] || { echo "refusing: chain id $chain_id is not Mantle mainnet (5000)" >&2; exit 1; }
CL_FORK_BLOCK_HASH="$(cast block "$BLOCK" --field hash --rpc-url "$RPC_URL")"
[ "$CL_FORK_BLOCK_HASH" = "$WANT_HASH" ] || {
  echo "refusing: block $BLOCK hash $CL_FORK_BLOCK_HASH != corpus hash $WANT_HASH" >&2; exit 1; }
export CL_FORK_BLOCK_HASH
echo "forking Mantle block $BLOCK ($CL_FORK_BLOCK_HASH) via $RPC_URL"

mkdir -p ../../tests/fixtures/corpus
forge test --fork-url "$RPC_URL" --fork-block-number "$BLOCK" \
  --fork-retries 8 --fork-retry-backoff 2000 --match-contract CaptureCorpusTokens -vv
echo "wrote:"; ls -l ../../tests/fixtures/corpus/token_transfers.jsonl

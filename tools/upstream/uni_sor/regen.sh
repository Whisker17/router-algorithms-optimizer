#!/usr/bin/env bash
# Regenerate the Uniswap SOR goldens (WHI-1443) end to end and prove they are
# reproducible: author the inputs, install the locked harness, generate, then
# regenerate in memory and require byte-identical files. Validation-only tooling;
# nothing here is used by the Python benchmark at runtime.
#
#   tools/upstream/uni_sor/regen.sh             # from anywhere in the repo
#   SKIP_NPM_CI=1 tools/upstream/uni_sor/regen.sh   # reuse an existing npm ci install
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
FIXTURES="$REPO/tests/fixtures/uni_sor"

want_node="$(cat "$HERE/.nvmrc")"
if [ "$(node --version)" != "$want_node" ]; then
  echo "regen.sh: Node $want_node required (found $(node --version)); e.g. 'nvm use' in $HERE" >&2
  exit 1
fi

cd "$REPO"
uv run python tools/upstream/uni_sor/author_inputs.py

cd "$HERE"
if [ "${SKIP_NPM_CI:-0}" != "1" ]; then
  npm ci --ignore-scripts --no-audit --no-fund
fi
node generate.js
node generate.js --check

cd "$FIXTURES"
echo "fixture digest (sha256 of sorted per-file sha256):"
shasum -a 256 *.json | sort -k2 | shasum -a 256

#!/usr/bin/env bash
# Regenerate the CFMMRouter.jl author-reference fixtures (WHI-1557) and prove they are
# reproducible: author the inputs, instantiate the pinned Julia environment, generate,
# then regenerate in memory and require byte-identical output. Validation-only tooling;
# nothing here runs in the Python benchmark or in `uv run pytest`.
#
#   tools/upstream/cfmm/regen.sh              # JULIA=/path/to/julia to override
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
JULIA="${JULIA:-julia}"
want="julia version 1.10.10"
if [ "$("$JULIA" --version)" != "$want" ]; then
  echo "regen.sh: $want required (found $("$JULIA" --version))" >&2
  exit 1
fi
export JULIA_NUM_THREADS=1

cd "$REPO"
uv run python tools/upstream/cfmm/author_inputs.py
"$JULIA" --project="$HERE" -e 'using Pkg; Pkg.instantiate()'
"$JULIA" --project="$HERE" "$HERE/generate.jl"
"$JULIA" --project="$HERE" "$HERE/generate.jl" --check
shasum -a 256 tests/fixtures/cfmm/author_inputs.json tests/fixtures/cfmm/author_reference.json

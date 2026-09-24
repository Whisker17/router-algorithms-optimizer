# `tools/cl_evidence` — independent CL swap evidence (WHI-1428)

Validation-only Foundry project. It generates the offline fixtures in
`tests/fixtures/concentrated/` that `tests/pools/test_concentrated.py` replays against
`pools/concentrated.py`. It is **not** a runtime dependency: `uv run pytest` never runs
it, and benchmark solves never execute Solidity.

What it does (details: `docs/references/concentrated-liquidity-migration.md` §6):

- forks Mantle mainnet at the catalog candidate block (`config/protocols.yaml`
  `candidate_block`, default `101057678`) through the public RPC;
- for each of `uniswap_v3`, `agni_v3`, `fusionx_v3`:
  - **real**: executes swap sequences on the catalog example pool (the deployed
    bytecode, `pool_code_hash` checked by the Python tests against the pin) and, where
    deployed, the source's QuoterV2;
  - **controlled**: creates a pool through the deployed factory with mock tokens and
    known positions, captures the full tick bitmap, and executes edge-case sequences
    (gaps, word boundaries, insufficient liquidity, dust, protocol fees);
- streams pre-state, every swap's `(amount0, amount1)`, post-state scalars and the ticks
  whose storage changed, as JSON Lines with decimal-string integers.

## Regenerate

Requires Foundry (`forge`, `cast`) and network access to `https://rpc.mantle.xyz`.

```bash
tools/cl_evidence/regen.sh                         # all six fixture files
tools/cl_evidence/regen.sh --match-test controlled # just the controlled pools (~15 s)
CL_FORK_BLOCK=<n> CL_RPC_URL=<url> tools/cl_evidence/regen.sh
uv run pytest tests/pools/test_concentrated.py     # offline replay
```

The first real-pool run fetches ~700 dense ticks per pool one storage slot at a time
over the public RPC (tens of minutes); Foundry caches the fork state under
`~/.foundry/cache/rpc/mantle/<block>`, so reruns are fast. Using a block other than the
catalog candidate makes `test_evidence_provenance_matches_catalog` fail on purpose —
update the catalog (and re-run the preflight) first.

`diff_sources.sh [workdir]` reproduces the deployed-source comparison in the migration
doc §2 (Routescan verified sources + `Uniswap/v3-core` v1.0.0).

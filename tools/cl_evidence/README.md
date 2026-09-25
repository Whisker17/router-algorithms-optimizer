# `tools/cl_evidence` — independent fork swap evidence (WHI-1428, WHI-1429, WHI-1430, WHI-1431, WHI-1432, WHI-1433, WHI-1434)

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

## Agni fixed-block replay evidence (WHI-1429)

`test/CaptureAgniReplay.t.sol` checks a *published bundle* rather than a hand-built
state: for every (reference case x pool of its pair) request it executes the deployed
AgniPool swap on a fork at the bundle's own block, and records pre-state scalars, every
bitmap word of the bundle's collected range, the traversed initialized ticks before and
after, the swap amounts, the post-state and a follow-up reverse swap. Output:
`tests/fixtures/agni/evidence.jsonl`, replayed by `tests/snapshot/test_agni.py`.

```bash
tools/cl_evidence/replay_agni.sh tests/fixtures/agni/bundle   # writes requests/ + evidence
uv run pytest tests/snapshot/test_agni.py                      # offline replay
```

`replay_agni.sh` derives the request file (inputs only) with `make_replay_requests.py`
and refuses a chain other than 5000 or a block whose hash differs from the bundle's.
The large cases cross several hundred initialized ticks per pool; the first run fetches
that storage slot by slot through the public RPC (tens of minutes) and is cached by
Foundry afterwards. Details: `docs/references/agni-fixed-block-replay.md` §4.

## FusionX fixed-block replay evidence and recompile (WHI-1430)

`test/CaptureFusionXReplay.t.sol` is the FusionX counterpart of `CaptureAgniReplay`: the
same per-request capture against the published FusionX bundle's block, plus FusionX's
deployed QuoterV2 quote for every request and the pool/LM-hook code hashes. Output:
`tests/fixtures/fusionx/evidence.jsonl.gz` (gzipped by the script), replayed by `tests/snapshot/test_fusionx.py`.

```bash
tools/cl_evidence/replay_fusionx.sh tests/fixtures/fusionx/bundle   # requests/ + evidence
uv run pytest tests/snapshot/test_fusionx.py                         # offline replay
tools/cl_evidence/rebuild_fusionx.sh [workdir]                       # local recompile check
```

`rebuild_fusionx.sh` (git, node/npm, cast) recompiles `FusionX-Finance/v3-contracts@7f7406e`
with solc-js 0.7.6 and reports `IDENTICAL` for FusionXV3Pool/Factory/PoolDeployer against
the deployed code outside immutable sites, plus the pool build's Keccak-256 (= the catalog
fingerprint). Details: `docs/references/fusionx-fixed-block-replay.md`.

## Uniswap v3 fixed-block replay evidence and recompile (WHI-1431)

`test/CaptureUniswapV3Replay.t.sol` is the Uniswap v3 counterpart: the same per-request
capture against the published Uniswap bundle's block, with Uniswap's deployed QuoterV2
(or `quoter_reverted` for a swap that moves no token), recording partial fills as the pool
really executes them and discovering crossed ticks word by word. Output:
`tests/fixtures/uniswap_v3/evidence.jsonl.gz`, replayed by `tests/snapshot/test_uniswap_v3.py`.

```bash
tools/cl_evidence/replay_uniswap_v3.sh tests/fixtures/uniswap_v3/bundle   # requests/ + evidence
uv run pytest tests/snapshot/test_uniswap_v3.py                          # offline replay
tools/cl_evidence/rebuild_uniswap_v3.sh [workdir]                        # local recompile check
```

`rebuild_uniswap_v3.sh` (git, node/npm, cast) recompiles `Uniswap/v3-core@v1.0.0` with
solc-js 0.7.6 and reports `IDENTICAL` for UniswapV3Pool/UniswapV3Factory against the
deployed code outside immutable sites, the pool build's Keccak-256 (= the catalog
fingerprint) and its creation-code hash (= `POOL_INIT_CODE_HASH`). Details:
`docs/references/uniswap-v3-fixed-block-replay.md`.

## Merchant Moe Classic v1 fixed-block replay evidence (WHI-1432)

`test/CaptureMoeClassicReplay.t.sol` executes every (case x pair) request of a published
Moe Classic bundle on a fork at its block with the deployed MoePair clones and MoeRouter:
router quote, the input transfer (credited amount), a `quote + 1` probe that must revert
`Moe: K`, the swap itself, the post-swap reserves/balances, a follow-up reverse swap, and
uint112 overflow probes on the example pair. Output:
`tests/fixtures/moe_classic/evidence.jsonl.gz`, replayed by
`tests/snapshot/test_moe_classic.py`. Requests come from `make_classic_replay_requests.py`.

```bash
tools/cl_evidence/replay_moe_classic.sh tests/fixtures/moe_classic/bundle   # ~40 s
uv run pytest tests/snapshot/test_moe_classic.py                             # offline replay
```

Details: `docs/references/moe-classic-fixed-block-replay.md`.

## Merchant Moe Liquidity Book v2.2 evidence (WHI-1433)

`test/CaptureLB.t.sol` executes the deployed LB v2.2 bytecode on a fork at the catalog
candidate block: four real pairs (clones of the pinned LBPair implementation, three with
their live `LBHooksRewarder` swap hooks) and four controlled pairs created through the
deployed LBFactory with mock tokens (sparse/dense bins, every `updateReferences` time
branch via `vm.warp`, exact drains and dust, the per-bin liquidity ceiling). It records
the whole bin tree, the fee state, every swap's per-bin `Swap` events, reverts and the
post-swap state. Output: `tests/fixtures/liquidity_book/{real_<pair>,controlled}.jsonl.gz`,
replayed by `tests/pools/test_liquidity_book.py`.

```bash
tools/cl_evidence/capture_lb.sh                        # all five files
tools/cl_evidence/capture_lb.sh --match-test controlled
uv run pytest tests/pools/test_liquidity_book.py       # offline replay (< 1 s)
```

The first real-pair run fetches every bin slot (up to ~3,000 per pair) through the
public RPC; Foundry caches the fork state. Details:
`docs/references/liquidity-book-migration.md` §5.

## Merchant Moe Liquidity Book v2.2 fixed-block replay evidence (WHI-1434)

`test/CaptureMoeLBReplay.t.sol` checks a *published* Moe LB bundle on a fork at its block
with the deployed LBPair clones and their live `LBHooksRewarder` / `LBHooksExtraRewarder`
hooks: an independent read of every pair's state, hooks and extra hooks, a re-walk of the
bin tree inside each pair's collected `bin_range`, and for every (case x pair) request the
executed swap (per-bin `Swap` events, post-state, touched bins) plus a follow-up reverse
swap through the post-state. Output: `tests/fixtures/moe_lb/evidence.jsonl.gz`, replayed
by `tests/snapshot/test_moe_lb.py`. Requests come from `make_lb_replay_requests.py`.

```bash
tools/cl_evidence/replay_moe_lb.sh tests/fixtures/moe_lb/bundle   # ~9 min (warm cache)
uv run pytest tests/snapshot/test_moe_lb.py                        # offline replay
```

Details: `docs/references/moe-lb-fixed-block-replay.md`.

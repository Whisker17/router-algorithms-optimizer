# Routing fixture bundles (WHI-1435)

Small checked-in bundles for plan-evaluation tests and for the later routing algorithms
(single path, direct/path split, incremental graph, SOR port). Both load with
`snapshot.bundle.load_bundle`; `tests/routing/test_plan_evaluation.py` pins their
content and uses them.

## `cpmm_graph/` — synthetic, hand-derivable

Generic constant-product pools (`fee_bps` 30 unless noted), tokens `TKA`..`TKD`:

| pool | pair | reserves | notes |
| --- | --- | --- | --- |
| `ab_1` | TKA/TKB | 200,000,000 / 200,000,000 | shallow direct pool |
| `ac_1` | TKA/TKC | 1,000,000,000 / 1,000,000 | pre-research §4.2 "pool one" |
| `ac_2` | TKA/TKC | 500,000,000 / 500,000 | pre-research §4.2 "pool two" |
| `cb_1` | TKC/TKB | 1,000,000 / 1,000,000,000 | shared suffix ("pool three") |
| `cb_2` | TKC/TKB | 400,000 / 420,000,000 | second suffix |
| `cd_1` | TKC/TKD | 1,000,000 / 2,000,000 | merge target for split-after-merge |
| `db_1` | TKD/TKB | 2,000,000 / 1,000,000,000 | |
| `db_2` | TKD/TKB | 1,000,000 / 480,000,000 | `fee_bps` 5 |
| `ab_dry` | TKA/TKB | 0 / 1,000,000 | any pool call fails (zero-input / no-liquidity cases) |

Cases: `a_b_large` (150,000,000 TKA→TKB), `a_b_small` (1,000,000), `a_d_multi_hop`
(10,000,000 TKA→TKD, no direct pool), `a_b_dust` (3). Expected values are derived by
hand from these reserves with the Solidity `getAmountOut` formula; e.g. splitting
`a_b_large` 100M/50M over `ac_1`/`ac_2` and merging into `cb_1` yields 119,395,080 TKB,
while quoting `cb_1` separately for the two legs claims 126,135,946 (5.65% phantom).

## `mantle_mixed/` — real states at Mantle block 101,057,678

Unchanged pool states copied from the published per-source bundles
(`tests/fixtures/{uniswap_v3,moe_lb,moe_classic}/bundle`, all frozen at the same block;
`provenance.json` records each source bundle's hash and the pools taken):
Uniswap v3 USDT/WMNT `0x4cdf…` and USDC/WMNT `0x086f…`; Merchant Moe LB WMNT/USDT bin
step 25 `0x3657…` and 15 `0xf6c9…`, USDC/USDT `0x48c1…`; Merchant Moe Classic USDT/WMNT
`0x4e76…`, USDC/WMNT `0x1a4d…`, USDC/USDT `0x8e3a…`. Its cases are correctness cases
whose split legs are the exact amounts the fork evidence in
`tests/fixtures/{concentrated,liquidity_book,moe_classic}/` executed — not an empirical
corpus.

Regenerate (only if a source bundle changes) by loading those bundles and passing the
listed pools, the cases in `cases.jsonl` and an updated `provenance.json` to
`snapshot.bundle.write_bundle(..., kind="real")`;
`test_mantle_fixture_is_unchanged_published_state_at_one_block` fails until they agree.

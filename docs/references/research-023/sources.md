# 0.2.3 source register (WHI-1622)

Companion of [`contract.md`](contract.md) (`R023-C/1`). Contract §1 is the normative source table
and its reading of each source. This file records how each source was retrieved, pins the bytes
that were read, and lists the exact quotations §1 relies on. Every source is motivation or prior
art only. No Jupiter API, binary or code is used, and nothing here is a performance target.

## 1. Retrieval

- Fetched 2026-10-05 at about 11:55 UTC by the independent reviewer (`mantle/gpt-6-astra`, review
  round 1) with `curl -sL --max-time 50`. Every URL returned HTTP 200. The fetch and the source
  audit are recorded in [`reviews/round-1.md`](reviews/round-1.md) §3–§4.
- The raw pages are third-party content and are **not** committed. Their SHA-256 at retrieval is
  pinned in §2: `raw` is the HTML as fetched, `text` is the tag-stripped copy used for the quote
  checks. A page that changes later will no longer match its pin. The quotations in §3 are what the
  contract relies on.
- Each quotation in §3 was re-checked verbatim against the retained text copy on 2026-10-05.
  `…` marks an elision. The spelling is the source's own (U1 "utilzies").

## 2. Register and pins

| ID | Source | Displayed date |
| --- | --- | --- |
| U1 | <https://developers.jup.ag/blog/ultra-v3> | October 15, 2025 |
| M7 | <https://developers.jup.ag/blog/metis-v7> | November 17, 2025 |
| MU | <https://developers.jup.ag/blog/metis-update> | April 23, 2026 |
| CL5 | <https://developers.jup.ag/changelog/2026-05> | May 31, 2026 |
| JS | <https://developers.jup.ag/blog/jit-swap> | June 11, 2026 |
| QE | <https://developers.jup.ag/blog/why-you-dont-get-what-you-were-quoted> | May 25, 2026 |
| MC | <https://metis.builders/changelog> | entries undated |
| P1 | Weiye Xi, Ciamac C. Moallemi, "Quantifying Sub-Optimality in Routing for Automated Market Makers", <https://arxiv.org/abs/2607.20762> (HTML v1 read for the numbers) | submitted 22 Jul 2026 |
| P2 | "PRIME: Efficient Algorithm for Token Graph Routing Problem", <https://arxiv.org/abs/2603.08337> (HTML v2 read) | v1 9 Mar 2026, v2 17 Jul 2026 |
| P3 | "Optimal Routing for Constant Function Market Makers", <https://arxiv.org/abs/2204.05238>; "An Efficient Algorithm for Optimal Routing Through Constant Function Market Makers", <https://arxiv.org/abs/2302.04938> | 11 Apr 2022; 9 Feb 2023 |
| B | R. P. Brent, *Algorithms for Minimization without Derivatives*, Prentice-Hall 1973 (Dover reprint 2002), ch. 5; author page <https://maths-people.anu.edu.au/~brent/pub/pub011.html> | 1973 |

```text
# sha256                                                            kind  source
732198befa1ccc414a82e8c57f85d1395f9c8fc456a4840b5d370a64f3cec8f1  raw   U1
b1b657a717c0dbe8f1ebe19962d29bb99983cef2a3d838150610b97f0dd1d9c4  text  U1
608a670b2e09b7cd8c30321bedcb5e1c21e66d3fe17d89a9b0432a092e7866c0  raw   M7
16522aaef74cfb7994fa4be46ef19bb7200182656559d99e9498a4c46ad5448b  text  M7
17dd256140e80df66b3ddd403d69ea2fae613dc04964f73841fa4511c504cabc  raw   MU
7b6742ce4fe0c0c0853e0f5102ec6e84e4e72051f4ee719738bd11314fba027c  text  MU
52ed4a1d9e82fdd310e00dfa60d0a7a55899c15c85654796384c69519cba2dd0  raw   CL5
1f3e5c83d7aae2c4c2d1621ee7ad8e52819fb1647136a66a24ea200f1eef1632  text  CL5
a3936355a625a35a92ffbc840fe45f684c48143a580a436724d82abb05b3439f  raw   JS
1b2cd446dc29c4e7a39d3f9529357a101633200a51418f466b15e882be80b11b  text  JS
536a47736d64cfb17d314ca16aa0652dd858ceddc3892dffefd296937f4af3f5  raw   QE
6a33f47b9b1d671dba9c5c880277b7efa6608ca8d979c13c86a4c7dd8b089b55  text  QE
2e78429aef0df37b31128c0d4f4d4c24bf7e9bae80b547e7de4fbda2cfe6ed1b  raw   MC
e40769442241326949486f1426997f142cc42912273675f9a87a83ddd5ab5c98  text  MC
3d6e371613920f454a2b2a0269e220a58b6a1a58b6b373b7c7b237c9693490c9  raw   P1 abs
0098a7ce4bfde6480dd3d9febe8a40359a5769922ae7689686a8ef99b41c40f9  text  P1 abs
2c83ed60a86e526c099e0f956e5ba469f08cfdbc98f6fb0f29b10fc8c31375d7  raw   P1 html v1
d7340b26df3230037216682ec310b174e3f10aac349c7cf88da54e7a9d2964ec  text  P1 html v1
a5a524c0d2588546130c55b2a669ac96aeb169b6e8b8c48588a611b1c7b8a57c  raw   P2 abs
db3f03004182fab4f12b53905e2eeb651b1cd2c3dca26850d24a710274e8cc18  text  P2 abs
9b148a1caadffac9d268b66904e096e9262d25c52d4d4302c48305131245f3bf  raw   P2 html v2
1311dd81db11742d6b4ef7180a74ee26f9ffbebf96c8a694f3cdcd828fadbbb7  text  P2 html v2
42d09869b1c84b5aa43430df845d5128c21bd996c43ac409f8e6885844a1ebef  raw   P3 2204.05238
d973deb458bf0e76760f5b88a5f25930aa9f7edf3cc1f5afd1c6d396d6ae209f  text  P3 2204.05238
c1bb2c722b09ffdea95e71d0d6e936a06b6f33b776d7500b442ea4fe9cbc3316  raw   P3 2302.04938
f3dec4c65b66b6dead0cbbce28c5ca676e7d2253a3a472c084058366e11bf3ad  text  P3 2302.04938
ec9d8601e0c6188e94dae1949f601339a4b50c46a52c460a6e9c2c49fc14daab  raw   B author page
```

## 3. Quotations relied on

**U1 (Ultra V3).** "Iris now utilzies better routing algorithms like Golden-section and Brent's
method." · "Iris uses Brent's method to optimize route splitting" · "Iris is now capable of more
granular splitting, up to 0.01%." · "We are seeing 100x performance improvements with these changes
compared to our sunsetted Metis router." The page does not define the metric behind "100x".

**M7 (Metis v7).** "Re-engineered splitting algorithm from GGS to Brent Op Splitting, allowing the
router to find more efficient paths by splitting trades with hyper-granularity (down to 1 BPS
precision) without sacrificing speed". "GGS" is not defined on the page.

**MU (Metis Update).** "Real-Time Slippage Estimator (RTSE) analyzes every swap on every market to
build a statistical model of expected slippage" · "Metis router now carries multiple candidate routes
into execution and finalizes splits on-chain, at the last possible moment." · "Fast Mode skips those
optimization steps and returns a quote in under 100ms" · section headings "Dynamic Intermediate
Tokens", "JupiterZ V2" ("market maker orders can now function as a hop") and "Jupiter Lend (Mint &
Redeem)".

**CL5 (Changelog May 2026).** "Integrators matching on router === "iris" must update the comparison.
"Iris" was the legacy name for Jupiter's onchain routing engine; the canonical name is Metis." (the
inner quotation marks are the source's) · "Metis V8 routing upgrade is live". Read as naming/lineage, not version
identity.

**JS (JIT Swap).** "The off-chain route computes splits up to single basis point resolution. JIT
Swap can re-split across pools up to 5% increments." · "The current JIT Swap runs at an average of
600k CU" · "It evaluates six venues on-chain" · "26.89% of JIT Swaps executed through a different set
of pools than the off-chain quote had planned." · "57.82% of same-pool swaps had their split
rebalanced at execution." · "A swap is counted as "rebalanced" when any pool's share of the order
moves by more than 0.5%."

**QE (quote vs execution).** "EMA algorithms: Exponential Moving Averages on recent execution data
per market." · "Predictive Execution: simulate, then decide" · "Jupiter swaps execute with an average
of +0.63 bps positive slippage." U1 reports +0.63 bps for its October 2025 window too.

**MC (metis.builders changelog; entries undated).** v7.0.1 "Feat: Brent optimization for routing" ·
v7.0.4 "When enabled, top 3 intermediary tokens will be chosen for the input and output mints
always." · v7.0.5 "Fix: Quote multiple use bellman logic", "Perf: Optimize routing allocations and
lock usage", "Chore: Run Metis Using Snapshot", "Feat: DLMM cache liquidity available per tick to
bypass heavy math". These are one-line strings. No mechanism is inferred from them.

**P1 (Xi & Moallemi).** "we measure an average shortfall of 2.02 bps per trade" · "insufficient pool
activation dominates mis-splitting within the activated set, even after charging per-pool gas." ·
scope: "we restrict attention to a setting in which all pools under consideration are for the same
token pair". Three numbers sit in inline math, which the HTML text copy renders twice (for example
`0.054670.05467`). With the duplicate removed they read: relative to SCO, "the mean shortfall is
0.05467 bps"; one block of staleness under FVO raises it "by 1.28739 bps"; under G–FVO "the N=0→1
jump is 1.77525 bps".

**P2 (PRIME).** "the chosen paths must be pool-disjoint" · "Our algorithm employs a backtracking line
search" (ASGM) · PRIME-Flow: "a relaxed variant of PRIME that permits overlapping paths during the
discovery phase" · "We employ a ternary search algorithm to determine the optimal split ratio of the
input amount between the current flow and the new path" · "The process terminates when no path with
a better marginal price can be found."

**P3 (CFMM routing).** 2302.04938: "We present an efficient algorithm, based on a decomposition
method" · "makes it simple to incorporate more complicated CFMMs, or even include 'aggregate CFMMs'
(such as Uniswap v3), into the routing problem." 2204.05238 is cited for the convex formulation of
CFMM routing. The repository already uses both in `cfmm_dual`.

**B (Brent 1973).** Author page abstract: "An algorithm for finding a local minimum of a function of
one variable is described in Chapter 5. The algorithm combines golden section search and successive
parabolic interpolation". The method is a **local** minimiser. The contract does not use Brent's root finder.

## 4. Not used

The reviewer also fetched a 2023 Jupiter v3 forum post on the original Metis algorithm
(`discuss.jup.ag/raw/21712`, sha256 `825598a5af7e5457721433f11d0e008304a7a1b92a91fa403efe50fb66492169`
of the text copy). WHI-1448 already covers it (`docs/references/jupiter-metis-challenge.md`). This
contract relies on nothing in it.

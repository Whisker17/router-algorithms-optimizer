# Jupiter Metis challenge: source register, Mantle mapping and a falsifiable hypothesis (WHI-1448)

Status: **research deliverable, verdict GO, scoped narrowly** (§11). This memo is the
[WHI-1448](https://linear.app/whisker-personal/issue/WHI-1448/020-routing-define-a-source-backed-jupiter-metis-challenge)
feasibility result that DESIGN §2.8 asks for. It implements no algorithm, and it changes no
code, test, profile, DESIGN text or historical evidence. Nothing here claims Jupiter Metis
equivalence. A go verdict only lets
[WHI-1449](https://linear.app/whisker-personal/issue/WHI-1449/020-routing-evaluate-the-approved-metis-inspired-challenge)
be finalized against the contract in §10. It does not complete WHI-1449, and research-only
completion never does. Core v1 (Release 0.1.0) does not depend on this challenge (§12).

Written 2026-09-28 by the IMPLEMENTER role (model claude/claude-opus-5-5, effort high) on
branch `chore/whi-1448-metis-challenge`, based on `origin/dev`
`d89238227a1449dd6e5309eb43b76b1cad484136`.

## 1. Inputs and the baseline under challenge

- Spec: DESIGN §2.8 (challenge deliverable), §2.6 (the `incremental_graph` contract), §2.10–2.12
  (budgets, comparison rules, parameters) and §9 (the two Jupiter references).
- Pre-research: `pre-research-from-gpt-6-pro.md` §3.4 and §5. Its Metis statements are checked
  against primary passages in §3 below. The pre-research citation markers remain unresolved
  in that file. This memo does not rely on them.
- The baseline under challenge is `routing/algorithms/incremental_graph.py` (WHI-1441, PR #23
  merged as `249ac3cd28efa5f59b4880679f288d46ccddf98b`). It was last changed by WHI-1507
  (`317106e34769c03571635b385da5cec42a012e3e`, default-off exact reuse). File sha256 at this
  base is `716d6f949fc2fbf2b40a6ddb8effc6b2c7a3f420eff5cbb924806bb65116f948`. Its tests are in
  `tests/routing/test_incremental_graph.py` (sha256
  `a63384499823c186d94ffb33a79ccb103513e8fe29aa4d14b12cb4665a548940`), and its path search is
  in `routing/search.py` (sha256
  `ca4ea207cb322aa41791a663e4f59c63d26108e0f3ac61126a7bad82fb1c06f9`).
- Calibration and acceptance evidence: `v1-acceptance.md` §§2–3, plus the frozen corpus
  `mantle-5src-101082044-091b0759` and its tuning/report cuts (§10.2).

## 2. Source register

The retrieval date for every source is 2026-09-28 UTC. A hash identifies the exact bytes
that were fetched. It does not identify a stable upstream revision: forum and documentation
pages can change without notice.

| ID | Source | Kind | Content date | Identity at retrieval | Terms / license | Use here |
| --- | --- | --- | --- | --- | --- | --- |
| J1 | [Archived: Jupiter v3: The Metis Routing Algo](https://discuss.jup.ag/t/archived-jupiter-v3-the-metis-routing-algo/21712) (raw: `https://discuss.jup.ag/raw/21712`) | **Historical** technical description | Written 2023-07-27 per its header, reposted 2024-08-23 by `0xSoju`, 1 post, version 1 | raw markdown sha256 `825598a5af7e5457721433f11d0e008304a7a1b92a91fa403efe50fb66492169` (identical on two fetches) | Forum post with no license statement | Only primary description of the mechanism. Short attributed quotations only |
| J2 | [Reduce Latency](https://developers.jup.ag/docs/swap/advanced/reduce-latency) (`…/reduce-latency.md`) | **Current** API documentation (Swap API V2 `/build`) | Undated. `mode=fast` is marked "new and in BETA" | `.md` sha256 `d7ab87af2f137ff94c3ded402df6b62d152cf01f66f990d0925ea81acc113e53` (identical on two fetches) | Jupiter documentation site, no open license found | Current behavior distinction |
| J3 | [Swap API overview](https://developers.jup.ag/docs/swap/index.md) | Current API documentation | Undated | sha256 `e212d6044630ce510ad4b4dd5b4fb2eaf4e877d8aeee16da086bc4c5b7ca30d7` | as J2 | Meta-Aggregator versus Metis-only Router |
| J4 | [Reduce Transaction Size](https://developers.jup.ag/docs/swap/advanced/reduce-transaction-size.md) and [Build](https://developers.jup.ag/docs/swap/build/index.md) | Current API documentation | Undated | sha256 `5a2ad160837512eb6a4a30c50c72cce647a2ab4bc3cae6233bc992f662d5228e` / `11ba294da2854a8e43314c928bc9d59967f09494cdecb25b87d057d3f11e0614` | as J2 | Solana account and transaction-size constraints, `routePlan` shape |
| J5 | [Integrate AMM into Metis](https://developers.jup.ag/docs/swap/routing/amm/integration.md) | Current integration documentation | Undated | sha256 `50b3b81869869dff1996db5a85e697e34e53bb5be0f6b7fd07b75c6f3891d3de` | as J2 | Quote-interface constraints (no network calls, cached state) |
| J6 | [Metis v7: Migration to Metis.builders](https://metis.builders/blog/metis-v7.md) | **Current** product announcement | v7.0.0 released 2025-11-14 (GitHub release timestamp, J8) | sha256 `5eeff7ff3133989eb5cf0e8962cd180040c979451b05d021feb264dc5c29d501` | Governed by J7 for licensees | Current-engine distinctions only |
| J7 | [Metis SDK & API License Agreement](https://metis.builders/sdk-and-api-license-agreement.md) | Terms | Undated | sha256 `0804c6bf0f5862b5d3f48989c24d1dda8764c416e141fde695d546efd2015f3f` | Proprietary licence (see §5) | License boundary |
| J8 | [`jup-ag/metis-binary`](https://github.com/jup-ag/metis-binary) and its [changelog](https://metis.builders/changelog/index.md) | Current closed binary distribution | HEAD `f7e6cbd8c0a6ca9314ff6fc6c9887c4ad258bdb4` (2026-06-18). Latest release `v7.1.2` published 2026-09-18 | changelog `.md` sha256 `a5f3b3a9211aede4741918939a645fcc0cc91f6e6f41d55ecab6b370b57c5818` (cited lines quoted in §4) | GitHub `license: null`. Repo holds `README.md`, `Dockerfile`, `examples` only. Releases are zipped binaries that need a `BINARY_KEY` | Evidence that no source code is published |
| J9 | [`jup-ag/jupiter-amm-interface`](https://github.com/jup-ag/jupiter-amm-interface) `interface/src/lib.rs` | Current public code (AMM quote trait only) | HEAD `95bd18485e580625a39eb92a0c0ad2061579daad` (2026-08-07), crate `jupiter-amm-interface` 0.6.1 | `lib.rs` sha256 `11dbcac4e5e6d6433220d15fb00a32130830565c607539dd5652916bb546d414` | `license = "Apache-2.0"` in `interface/Cargo.toml`. No LICENSE file at repo root. GitHub reports none | Per-pool quote seam semantics. Contains no routing code |
| J10 | [`jup-ag/jupiter-swap-api-client`](https://github.com/jup-ag/jupiter-swap-api-client) | Current public code (HTTP client) | HEAD `fb95cfad9f19ed64c40f455772a6bfc4706d14f6` (2025-12-29) | not needed | GitHub `license: null` | None: API client, no routing |
| W1 | [Bellman–Ford algorithm](https://en.wikipedia.org/wiki/Bellman%E2%80%93Ford_algorithm) (linked from J1) | Public textbook algorithm | – | – | Public algorithm with no code reuse | Generic label-correcting idea only |

No Jupiter or Metis routing source code was found publicly. The GitHub API returned HTTP 404
for `jup-ag/jupiter-core` (a guessed name) and for `jup-ag/metis-docs`, which the J7 card
links, so that repository is not publicly readable.
The earlier latency research recorded 404s for historical Metis pages
(`latency-optimization-research.md` §5, "Source retrieval gaps"). This run retrieved J1
through the Discourse JSON and raw endpoints (HTTP 200), which resolves that gap for J1
only.

## 3. Verified claims and where inference begins

Every algorithm statement used later is checked here against its primary passage. Short
quotations come from J1 unless marked otherwise.

| # | Claim | Primary passage | Status |
| --- | --- | --- | --- |
| C1 | Metis is a Bellman–Ford variant | "a heavily modified variant of the Bellman-Ford algorithm" | **Verified wording.** The modifications are not described |
| C2 | Input is split incrementally, with split and merge at any stage | "Metis streams the input tokens to incrementally build a route to split and merge at any stage" | **Verified wording.** Chunk sizes, order and stop rule are not given |
| C3 | Splits are generated one after another, and a DEX may be reused across splits | "By generating the routes for each split iteratively one after another, we can also use the same DEX in different splits" | **Verified wording.** How the reused DEX's state is accounted for is not given |
| C4 | Route generation and quoting are one step, which prunes bad routes and allows more intermediaries | "we combine route generation and quoting into a single step, allowing us to avoid generating and using bad routes, which besides improving the efficiency, also allows us to use a larger set of tokens as intermediaries" | **Verified wording.** The pruning rule is not given |
| C5 | The v2 limit of at most 4 DEXs came from Solana's account-lock limit | "Solana limits us to use at most 4 DEXs in a swap *(due to the account lock limit of 64)*" | Verified. This is a Solana-specific constraint |
| C6 | Quotes were refreshed in parallel and in real time | "a major infra upgrade to refresh quotes in parallel and in real time" | Verified. This is infrastructure, not the algorithm |
| C7 | Metis quotes were 5.22% better than v2 on average | "On average, Metis quotes prices that are 5.22% better than our v2 engine" | Verified as a vendor claim. The method, corpus and statistic are undisclosed, so it is **not evidence** for Mantle |
| C8 | The route search is Bellman–Ford, with splitting as a separate layer that can be turned off | J2: "Bellman-Ford with no splitting: the routing algorithm uses Bellman-Ford without splitting the route across multiple pools" | **Verified for the current `mode=fast` beta.** Default-mode internals are not documented |
| C9 | The current splitter uses Brent's method | J6: "Re-engineered splitting algorithm from GGS to Brent Op Splitting, … down to 1 BPS precision". J8 v7.0.1: "Feat: Brent optimization for routing" | Verified wording. What is optimized, and what "GGS" means, are **undefined in the source** |
| C10 | Per-AMM quotes are pure reads of cached state | J9: `fn quote(&self, quote_params: &QuoteParams) -> Result<Quote, AmmError>`, which returns `in_amount`/`out_amount`/fees and no next state. J5: "There might be multiple calls to `quote` using the same cache, so **we do not allow any network calls**" | Verified for the AMM interface. How the engine handles repeated use of one pool is not visible |
| C11 | Pre-research §3.4: Metis is a heavily modified Bellman–Ford variant with incremental building, split and merge, and combined path generation and quoting | C1–C4 | **Consistent with J1.** The pre-research reading ("a specialized algorithm, not textbook Bellman–Ford") is fair |
| C12 | Pre-research §5: Metis belongs to the Solana stack, so its ideas can be borrowed but not its transaction constraints or execution layer | C5, C6, §6 | Consistent |

**The inference boundary.** J1 names the ingredients: a Bellman–Ford variant, incremental
splits that may reuse a DEX, and quote-driven route generation. It gives no data structure,
chunk rule, label or dominance rule, tie rule, state accounting or budget. Any executable
algorithm built from it is therefore **Metis-inspired and inferred**. §9 builds one from C1,
C2 and C4 plus the textbook structure in W1, and marks each inferred choice. The
"modifications" in C1 and the default-mode behavior in C8/C9 remain unknown.

## 4. Historical Metis versus current API behavior

| Aspect | Historical (J1, 2023) | Current (J2–J8, 2025–2026) | Consequence here |
| --- | --- | --- | --- |
| Product | Metis is "our new advanced routing algorithm" in Jupiter v3 | The Meta-Aggregator `/order` lets "Metis, JupiterZ, Dflow, OKX" compete (J3). The Router `/build` path is "Metis onchain routing only". Metis v7 is "no longer serving as Jupiter's primary and sole router" (J6) | A Jupiter quote today is not a Metis quote. Neither is a Mantle oracle |
| Search | Bellman–Ford variant, incremental splits (C1–C4) | `mode=fast` = Bellman–Ford with no splitting (C8). The default mode is undocumented | Bellman–Ford is a path engine, and splitting sits on top of it |
| Splitting | Iterative, one split after another (C3) | "Brent Op Splitting" replaced "GGS" in v7 (C9) | The current splitter is unspecified, so it is **not adopted** (§8) |
| Intermediates | Larger intermediary set enabled by C4 | `supportDynamicIntermediateTokens` picks "top 3 intermediary tokens" (J8 v7.0.4). `restrictIntermediateTokens` and `onlyDirectRoutes` are API knobs | API knobs reveal no algorithm |
| Changelog hints | – | v7.0.5 "Fix: Quote multiple use bellman logic". v7.0.6 "Cleanup: Remove unused prefer\_simpler\_routing" | These suggest Bellman–Ford logic and multiple-use handling persist. The wording is too thin to specify anything |
| Access | Blog post | Binary needs a key and "10,000 staked JUP" (J6), under the J7 licence | The binary is not usable as a reference (§5) |

## 5. Code, license and reproducibility inventory

- **Routing source:** none public (J8, J10, §2). Nothing can be ported or parity-tested, so
  the §2.7-style upstream parity used for Uni SOR is impossible for Metis.
- **Binary (J8):** closed, key-gated and governed by J7. J7 prohibits licensees from, among
  other things, "access[ing] the API or SDK for competitive analysis or disseminat[ing]
  performance information (including uptime, response time and/or benchmarks)", and from
  "reverse-engineer[ing], disassembl[ing], decompil[ing] … or creat[ing] derivative works of
  the API, the SDK or the Documentation". This project has not applied for, downloaded or
  used the API, SDK or binary, and this memo does not propose to. Benchmarking or inspecting
  the binary is outside scope. No design choice in §9 depends on metis.builders content: J6
  and J8 are used only to *distinguish* current behavior, and they are cited, not
  reproduced. Legal interpretation of J7 beyond that is the owner's. Nothing here turns it
  into an adopted obligation or waiver.
- **AMM interface (J9):** Apache-2.0 per crate metadata, and it contains a quote trait only.
  Used only as evidence for C10. Nothing is copied or translated.
- **J1–J5 text:** no open license. Only short attributed passages are quoted.
- **What can be reproduced:** the *ingredients* named in C1–C4, in Python, on this project's
  frozen Mantle state and exact evaluator, from a public algorithm (W1). **What cannot be
  reproduced:** Metis itself, its modifications, its split optimizer, its production quote
  refresh, its accounting for reused DEXs, and its results (C7).

## 6. Solana → Mantle semantic mapping

| Solana/Metis assumption | Source | Mantle benchmark domain | Mapping |
| --- | --- | --- | --- |
| Account-lock limit 64. `maxAccounts` 1–64. 1232-byte transaction. At most 4 DEXs in v2 | C5, J4 | EVM with no account-lock or per-DEX limit, and no transaction-size model in v1 (DESIGN §1.3) | **Does not transfer.** "Future proofing" (J1) is not a hypothesis here |
| Gas cost is negligible for complex routes ("On Solana, the cost to the user will remain small") | J1 | Mantle L2 fees are material. Empirical cost model v1 (`cost-model.md`). New shared-graph shapes carry low-confidence cost flags (DESIGN §2.9) | Primary endpoint is **gross**. Net results are secondary and flagged by applicability |
| Real-time, parallel quote refresh from streamed state | C6, J8 | One frozen block, offline solves (DESIGN §2.2, §2.10) | Excluded |
| Venues include CLOBs, limit orders, RFQ (JupiterZ), prop AMMs, JIT aggregation | J1, J3, J6 | Five AMM sources: Agni v3, FusionX v3, Uniswap v3, Moe LB v2.2, Moe Classic (DESIGN §1.2) | Excluded. RFQ/JIT are unsupported |
| `Amm::quote(&self)` on `u64` returns amounts only, with no next state | C10 | `quote_exact_in` on Python integers returns amounts **and** next state (DESIGN §2.3) | Reused pools use `incremental_graph`'s aggregate `f(x+δ)−f(x)` accounting (§7). Metis's own accounting is unknown and not imported |
| Market listing by liquidity heuristics | J5 market-listing | Explicit verified universe with recorded exclusions (DESIGN §2.2) | Not imported |
| ExactIn and ExactOut. Circular arbitrage unsupported on the API | J8; Swap "Advanced" index entry in `https://dev.jup.ag/docs/llms.txt` (sha256 `53060c142319951977ef5659b1bc9f32e6c8648a9fd9061082e39b86b37c10a3`) | Exact Input only. Economic cycles are rejected (DESIGN §2.5) | Consistent: exact-in, acyclic |
| Large token universe, where "larger set of intermediaries" is the lever | C4 | 8 tokens and 143 pools at block 101082044, and every token is already an intermediary | The Mantle lever is the **hop bound**, not the token set (§9.4) |
| Live Jupiter quotes | – | Not an oracle, and not a timed input (DESIGN §2.8, §7) | Excluded |

## 7. What `incremental_graph` already covers

These points come from the code at the pin in §1, not from its name.

- **C2/C3 are already implemented.** The input is split into `graph.chunks` integer chunks
  (`chunk_amounts`). Each chunk takes the path with the best marginal output given the
  tentative aggregate state of every physical pool. A pool may serve several chunks, which
  is "use the same DEX in different splits". The chunks are merged per pool (`merged_plan`),
  then split and merged at tokens. The complete plan is re-evaluated by `evaluate`, and
  `accounting_matches_evaluation` is recorded. `path_split` (with `single_path` and
  `direct_split`) is retained as a simpler candidate.
- **C1/C4 are not implemented.** Candidate generation is **static exhaustive enumeration**:
  `enumerate_paths` lists every cycle-free path up to `search.max_hops` before any quote, in
  hop-major depth-first order. Each chunk then scores **every** listed path. Route
  generation and quoting are separate, and no quote result prunes the candidate set. The
  WHI-1507 exact reuse (`graph_reuse=True`, default off) saves physical recomputation with
  identical results. It is not a pruning rule.
- **Measured consequence** (tuning split, uncapped, `v1-acceptance.md` §3). At 2 hops,
  `incremental_graph` falls about 13.5–13.8 bps (mean) short of the 3-hop best known. At
  3 hops with chunks=50 it costs 9.25 s p50 / 76.27 s max and 26,358 / 58,955 quotes
  (p50 / max). That cost is why the daily profile is limited to 2 hops.

An experiment that only relabels C2/C3 would duplicate `incremental_graph`, which the issue
forbids. The distinct ingredient left over is C1+C4: **quote-driven, Bellman–Ford-style
candidate generation inside each chunk.**

## 8. Candidate hypotheses considered

| Candidate | Source | Decision |
| --- | --- | --- |
| Quote-driven hop-layered label search per chunk (C1+C4 on top of C2/C3) | J1, J2, W1 | **Selected** (§9) |
| Brent-method split optimization | C9 | **Rejected.** The source does not say what variable is optimized, over which routes, under which constraints, or what "GGS" was. Building it would mean inventing the method. Continuous marginal allocation is already a separate future lead (DESIGN §2.8, Balancer-style) |
| Dynamic top-3 intermediary selection | J8 v7.0.4 | Rejected. Mantle's 8-token universe already exposes every intermediary |
| Future-proofing across more DEXs per transaction | C5 | Rejected. There is no account-lock analogue on Mantle |
| Real-time parallel quote refresh | C6 | Rejected. Infrastructure, and incompatible with the frozen-state design |
| JIT/prop-AMM/RFQ aggregation | J3, J6 | Rejected. Not in the admitted Mantle domain |
| Live Jupiter quotes as a reference | – | Rejected (DESIGN §2.8) |

## 9. Selected hypothesis H-M1: quote-driven label search per chunk

### 9.1 Statements

- **H-M1a (mechanism soundness, precondition).** Replace `incremental_graph`'s per-chunk
  exhaustive path scoring with a hop-layered, quote-driven label search. At the same hop
  bound, this selects the same chunk marginal as exhaustive scoring, except in the
  divergence classes of §9.3. It also executes no more quotes, and far fewer scored
  candidates, than `incremental_graph`.
- **H-M1b (routing hypothesis).** Because the label search's work grows roughly linearly
  with the hop bound (§9.4), the variant can run incremental allocation at **4 hops**
  (`graph.label_hops: 4`) inside the full profile's budget. The resulting plans differ
  observably from `incremental_graph` at 3 hops: they include 4-hop chunk paths. They also
  achieve higher evaluated gross output on the held-out report split, under the
  pre-registered sign-test rule in §10.6.

Either can be falsified. H-M1b fails if 4-hop chunk paths bring no paired gain, if they lose
cases, or if the variant needs more than the budget. H-M1a fails if unexplained divergences
appear, or if the work does not fall.

### 9.2 Mechanism (executable pseudocode)

Everything outside `choose_chunk_path` is unchanged `incremental_graph` behavior, using its
existing helpers: `chunk_amounts`, the carry rules, `creates_cycle`, `PoolFlow`,
`merged_plan`, `evaluate`, `topology`, simpler-candidate retention via `path_split` at
`search.max_hops`, the guarded quote meter, and statuses. Items marked *[inferred]* are this
memo's choices, not Metis facts.

```text
# Inputs for one chunk (identical to incremental_graph): amount a > 0 (with carry),
# committed pool flows F (pool -> (x_in, f(x_in)) on original state), committed token
# edges T, graph index G, target D, source S, hop bound H = graph.label_hops,
# quote(pool, token_in, amount) = the guarded QuoteCache seam (meter checked first).

def marginal_edge(e, m):                      # identical rule to incremental_graph.marginal
    x, out = F.get(e.pool, (0, 0))
    if m == 0: return 0, PoolFlow(e, x, out)  # no pool call
    r = quote(e.pool, e.token_in, x + m)
    if r.status != OK or r.consumed != x + m: raise Fail(reason)   # counted as today
    if r.amount_out < out: raise Fail("nonmonotone")
    return r.amount_out - out, PoolFlow(e, x + m, r.amount_out)

def choose_chunk_path(a):
    dist = hops_to(D) on G reversed, ignoring edges into S and out of D    # structural, exact prune
    L = [ {S: Label(amount=a, path=())} ]      # L[k][token] = best label reached in exactly k hops
    best = None                                # (marginal, path, updates)
    for k in 1..H:                             # [inferred] hop-layered Bellman–Ford (W1), no in-place relaxation
        L.append({})
        for t in tokens of L[k-1] in insertion order:         # deterministic order
            lab = L[k-1][t]
            if t == D: continue                                # never pass through the target
            for e in G.edges_from(t) in adjacency order:
                v = e.token_out
                if v == S or v in tokens(lab.path) or v in (t,): continue   # token-simple
                if v != D and dist[v] > H - k: continue       # cannot reach D in time (exact)
                if k == H and v != D: continue
                p = lab.path + (e,)
                if creates_cycle(T, p): count("paths_rejected_cycle"); continue
                if max_candidates reached this chunk: truncated("max_candidates"); stop chunk
                scored += 1                                    # one relaxation
                try: m, u = marginal_edge(e, lab.amount); updates = lab.updates + [u]
                except Fail: continue
                if v == D:
                    if best is None or m > best.marginal: best = (m, p, updates)   # strict: ties keep earlier
                elif v not in L[k] or m > L[k][v].amount:                          # strict dominance
                    L[k][v] = Label(amount=m, path=p, updates=updates)
    return best                                # None -> carry / abandon exactly as incremental_graph
```

Commit, carry, abandon, merge, evaluate, retain and report all stay exactly as in
`incremental_graph.solve`. **Ablation switch** `graph.label_pruning: false` replaces
`choose_chunk_path` with `incremental_graph`'s own per-chunk loop over
`enumerate_paths(G, S, D, label_hops)`. Everything else stays the same.

### 9.3 Dominance argument and divergence classes

For a fixed committed state, an edge's chunk marginal `g_e(m) = f_e(x_e + m) − f_e(x_e)` is
nondecreasing in `m` for exact-input AMMs, and pools on a token-simple path are distinct.
If a larger amount reaches token `t`, every continuation from `t` does at least as well,
provided that continuation is admissible and quotes successfully. Admissibility under
`creates_cycle` depends only on the path's token sequence. Two consequences follow
(derived, to be checked by WHI-1449 gate S2):

- **For H ≤ 3, the token-simple exclusion never removes a needed continuation.**
  A layer-1 label's prefix is `{S, t}`, and edges back into `S` are excluded anyway. The
  final hop always enters `D`, which is never on a prefix. `D` has no committed outgoing
  edges, so a final edge into `D` cannot close a cycle. With a non-failing continuation, the
  maximal label at each (layer, token) therefore reaches the same maximal marginal as
  enumeration.
- **Enumerated divergence classes:**
  1. **tie** — several paths share the maximal marginal, and the orders differ;
  2. **non-downward-closed quote failure** — the maximal label's amount makes a later edge
     fail (`incomplete_snapshot`, `insufficient_liquidity`, partial fill, `nonmonotone`),
     while a smaller dominated amount would have succeeded;
  3. **budget order** — `max_quotes` or `max_candidates` truncates at a different point
     because work is ordered differently;
  4. **token-revisit pruning, H ≥ 4 only** — the best prefix to `v` visits a token that the
     best continuation needs. This is the heuristic loss that textbook Bellman–Ford labels
     accept. *[inferred]* It is not repaired, for example with k-best labels, because that
     would be an unsourced extra parameter.

Quote subset: with identical committed state, every relaxation corresponds to a prefix that
`incremental_graph` also quotes at the same amount. Per agreeing chunk, the label search's
executed quotes are therefore a subset of the reference's.

### 9.4 Structural feasibility evidence (no quotes, no timing)

This probe is a read-only structural count on `bundle_tuning`
(`ee7afa7e2d1ef43dde67cada11aeb15e064e2b90ed9bff57113f04268b70279b`; 96 cases, 143 pools,
8 tokens), run with the repository code at `d892382`. It is not a performance measurement.

| hop bound | simple paths per case p50 / p95 / max | enumeration quote positions per chunk (prefix-tree nodes) p50 / p95 / max | label-search relaxations per chunk, upper bound, p50 / p95 / max |
| ---: | ---: | ---: | ---: |
| 2 | 161 / 469 / 469 | 187.5 / 507 / 521 | 68 / 105 / 105 |
| 3 | 4,889.5 / 10,042 / 10,042 | 6,223.5 / 11,011 / 11,253 | 228.5 / 264 / 268 |
| 4 | 104,527 / 145,992 / 145,992 | 118,907.5 / 161,549 / 166,241 | 398 / 503 / 514 |

The dense parallel-pool multigraph makes enumeration grow with the product of parallel pools
per hop. The label bound grows with layers × directed edges. Executed quotes are lower than
either column across chunks, because `QuoteCache` answers unchanged subtrees. This is why
`incremental_graph`'s measured 3-hop quotes (p50 26,358 at chunks=50) are far below
50 × 6,224. So the table predicts scored-candidate and lookup work, not measured quotes or
time. Worst case for the variant at `label_hops: 4`, chunks=50: ≤ 50 × 514 = 25,700
relaxation quotes. Adding the embedded `path_split` at 3 hops (p95 28,369 quotes) stays well
below the full profile's 300,000 cap.

Reproduce from a worktree containing `data/corpus/mantle-5src-101082044/bundle_tuning`:

```python
# PYTHONPATH=. uv run python probe.py <bundle_tuning path>   (read-only, no quotes)
import json, statistics, sys
from snapshot.bundle import load_bundle
from routing.search import build_graph_index, enumerate_paths
b = load_bundle(sys.argv[1]); idx = build_graph_index(b); out = {}
for H in (2, 3, 4):
    enum, bf, npaths = [], [], []
    for c in b.cases:
        pre = set(); n = 0
        for p in enumerate_paths(idx, c.token_in, c.token_out, H):
            n += 1; pre.update(p[:i] for i in range(1, len(p) + 1))
        frontier, relax = {c.token_in}, 0
        for k in range(1, H + 1):
            nxt = set()
            for t in frontier:
                for e in idx.edges_from(t):
                    if e.token_out == c.token_in or (k == H and e.token_out != c.token_out):
                        continue
                    relax += 1
                    if e.token_out != c.token_out: nxt.add(e.token_out)
            frontier = nxt
        enum.append(len(pre)); bf.append(relax); npaths.append(n)
    q = lambda xs: (statistics.median(xs), sorted(xs)[int(0.95 * (len(xs) - 1))], max(xs))
    out[H] = {"paths": q(npaths), "enum_prefix": q(enum), "bf_upper": q(bf)}
print(json.dumps(out))
```

## 10. Contract for WHI-1449 (go path)

### 10.1 Identity, capability, objective

- Registered name `metis_inspired`. Its report label is already reserved: "Metis-inspired
  experimental Python variant — NOT Jupiter Metis; no production equivalence claimed"
  (`report/aggregate.py`, `report.md`). It
  runs in a custom profile, outside the base and optimized strategy groups (DESIGN §2.6,
  WHI-1528).
- Capabilities are identical to `incremental_graph`: `multi_hop`, `split`, `shared_pools`.
  LB is supported, and the full five-source cohort is primary.
- Objective: **gross-only** primary (the `full_gross.yaml` twin). Empirical-cost net
  (`full.yaml`, cost model sha256 `50e89727d6b6ac869c14c837c7cdec8099efc897d201794640912f3f6b4b7e71`) is secondary, with the extrapolation flags
  for new shapes. Chunk choices use gross marginals, as in `incremental_graph`.
- No change to `incremental_graph.py`, `path_split.py` or any existing profile. The variant
  imports `incremental_graph`'s public helpers. Any shared seam needs parent approval and
  must be proven output-identical by the existing tests.

### 10.2 Corpus and splits

| Cut | Hash | Use |
| --- | --- | --- |
| `bundle_tuning` | `ee7afa7e2d1ef43dde67cada11aeb15e064e2b90ed9bff57113f04268b70279b` | Gates S2–S4 only. No value is selected on it |
| `bundle_report` | `85202b20c13685af6fd999fe3d228ca6de37abb5914b5459621e9a67a71971c0` | Held-out H-M1b verdict, computed once |
| `sor_cohort_report` | `8213b7b07222c98a5ec99f0bae53d0876d2d14dc7dfd3cb7aa6cec1e20afe640` | Optional secondary matched V2/V3 view |

All cuts derive from corpus `717c21f35d1f7f6a02f7076b40793eaba564148cc4d187392049e503fa4d3143`.
The report split was seen in the 0.1.0 acceptance for other algorithms, but no variant value
is tuned on anything (§10.3), so it stays held out for this variant.

### 10.3 Parameters: reuse first, new values labeled

| Key | Value | Provenance |
| --- | --- | --- |
| `search.max_hops` | 3 | Reused from `full_gross.yaml` (DESIGN trial value kept on evidence). Also bounds the embedded `path_split` |
| `search.max_splits` / `search.percent_step` | 4 / 5 | Reused (DESIGN trial values) |
| `graph.chunks` | 50 | Reused. Calibrated for `incremental_graph` at 3 hops. **Not re-calibrated** for the variant, so the arms stay matched |
| `budget.time_limit_seconds` / `max_quotes` / `max_candidates` | 900 / 300,000 / null | Reused from `full_gross.yaml`. For the variant, `max_candidates` caps relaxations per chunk (declared unit change, inactive at null) |
| `measurement` / `worker` | warmup 0, repeats 1, seed 1447, fixed order; spawn, scope algorithm | Reused from `full_gross.yaml` |
| **`graph.label_hops`** | 3 (arm M3), 4 (arms M4, M4-off) | **New, unvalidated, registered, not tuned.** 3 matches `max_hops` for H-M1a. 4 is the smallest bound above the full profile, and §9.4 shows its enumeration is about 300× the label bound. Must be ≥ `search.max_hops`. No default |
| **`graph.label_pruning`** | true (M3, M4), false (M4-off) | **New ablation switch**, not a tunable. No default |

### 10.4 Arms (one profile each; one pass, same source revision)

| Arm | Algorithm | label_hops / pruning | Split(s) | Purpose |
| --- | --- | --- | --- | --- |
| A0 | `incremental_graph` | – (max_hops 3) | tuning, report | Reference and disabled-mechanism baseline for M3 |
| M3 | `metis_inspired` | 3 / true | tuning | H-M1a. Ablation of M3 = A0, since the mechanism off at 3 hops is A0's loop |
| M4 | `metis_inspired` | 4 / true | tuning, report | H-M1b |
| M4-off | `metis_inspired` | 4 / false | tuning | Same-hop ablation: is the mechanism what makes 4 hops tractable? |
| ctx | `path_split`, `uni_sor_port` (and `direct`) | full_gross values | report | Context rows only, never the verdict |

### 10.5 Differentiating fixtures (hand-checkable; `tests/routing/test_metis_inspired.py`)

Expected values come from hand CPMM formulas (like the existing `_v2_out` in
`test_incremental_graph.py`) or from explicit per-path exact quotes. They must not come from
the solver under test.

1. **X1 parallel-pool equivalence (H=3).** Tokens S, B, C, D, with k parallel CPMM pools per
   hop. M3 and A0 return identical plans and gross. M3's relaxation count matches the
   hand-computed layer sum, A0's `paths_scored` matches the hand-computed k-product count,
   and `quotes_executed(M3) ≤ quotes_executed(A0)`.
2. **X2 non-downward-closed failure (class 2).** The only good X→D pool has collected state
   that the maximal label amount exceeds (`incomplete_snapshot`), while a dominated smaller
   prefix fits. A0 takes the fitting path. M3 skips it, and the difference is asserted from
   explicit per-path quotes. The failure count is visible.
3. **X3 four-hop-only route.** Deep liquidity exists only along S→B→C→E→D, and the 3-hop
   alternatives are shallow. M4 uses the 4-hop path and beats A0 (max_hops 3) by the hand
   figure. The plan replays independently and `accounting_matches_evaluation` holds.
4. **X4 token-revisit loss (class 4, H=4).** The best 2-prefix to X runs through Y, and the
   best 4-hop continuation needs X→Y. M4 misses it and M4-off finds it, with the gap asserted
   by hand. This documents the heuristic limit.
5. **X5 common semantics.** Shared-prefix, shared-suffix, nondivisible, dust-carry and
   never-worse-than-`path_split` cases reuse `incremental_graph`'s fixture pattern. They
   check valid replay, full fill and conservation.
6. **X6 budgets and statuses.** `max_quotes` stops before the meter. Truncation is declared.
   A timeout is never `no_route`. Incomplete-only candidates give `incomplete_snapshot`.
7. **X7 ablation identity.** With `label_pruning: false` and `label_hops == max_hops`, the
   plan, evaluation and logical counters equal `incremental_graph` on X1–X6 and on a few real
   tuning cases.

### 10.6 Metrics, pre-registered decisions and stop conditions

Load-independent primary metrics are exact integer gross, status and coverage counts, and
stage-2 counters (relaxations or paths scored, quotes executed and memoized, truncation).
Also recorded: chunk-path hop histogram and the `accounting_matches_evaluation` rate. Wall
time is secondary. A speed claim requires the L01 host-load validity rule
(`latency-optimization-results.md` §2). Otherwise timing is labeled contaminated and no
speed claim is made.

- **S1 correctness (stop).** Any failure in X1–X7 or in the common evaluator/conservation
  tests stops the work before any corpus run.
- **S2 H-M1a (tuning, M3 vs A0; stop).** WHI-1449 adds a diagnostic that re-scores the
  enumeration maximum at each chunk. Pass: every case whose plan differs from A0 has each
  differing chunk attributed to class 1–3, and `quotes_executed(M3) ≤ quotes_executed(A0)`
  on every case with identical chunk choices. Any unattributed divergence is a bug or an
  unsound dominance assumption: stop, report, and do not run M4.
- **S3 work (stop).** If M3's median stage-2 scored candidates are not below A0's, the
  mechanism has no advantage. Report drop and skip H-M1b.
- **S4 tractability (tuning, M4 and M4-off; report, not tuned).** Record the timeout and
  truncation counts of M4 and M4-off against A0. If M4-off finishes every case within the
  caps with the same gross as M4, report that any 4-hop gain is **not attributable** to the
  label mechanism. If M4 has more timeouts than A0, report that. No new parameter values
  may be introduced either way.
- **H-M1b verdict (report split, once).** Paired per-case final gross, M4 versus A0, over
  cases `ok` in both. Ties are excluded, and the sign test is exact binomial and two-sided.
  **Better**: wins > losses and p < 0.05. **Worse**: losses > wins and p < 0.05. Otherwise
  **inconclusive**. Also report the bps distribution (mean/p50/p95, per stratum), coverage
  denominators with every status, and the share of M4 wins whose plan uses a 4-hop chunk.
  Keep the variant only if S2–S3 passed and the verdict is **better**. Otherwise record
  drop/inconclusive. A negative result still completes WHI-1449's measurement. It does not
  turn this research into an implementation.
- **Campaign bound.** One pass per arm and split in §10.4: tuning 4 × 96, report 2 × 302,
  plus optional ctx and matched rows. No repeats, no re-runs after seeing report results,
  and no sweep of `label_hops`, `chunks` or budgets.

## 11. Verdict

**GO**, narrowly: a reproducible Metis-*inspired* experiment exists. It tests C1+C4 (quote-
driven Bellman–Ford-style generation), which `incremental_graph` lacks, and it is not a
relabeling of C2/C3. It is executable from public sources, integrates with the existing
interfaces, and comes with fixtures, ablation, budget and stop rules (§§9–10).

Limits of the go:

- It tests an **inferred** composition. Jupiter's modifications, split optimizer (C9) and
  production behavior remain unknown and are not claimed.
- On Mantle's 8-token universe, H-M1a predicts nearly identical routes at 3 hops. The only
  routing difference under test is the 4-hop reach (H-M1b), which may well show no gain.
  That would be an honest negative result.
- No Jupiter binary, API or live quote is used or required.

## 12. Consequences for WHI-1449 and core v1

- WHI-1449 may be finalized against §10. Its Implementation section should reference
  §§9.2, 10.3–10.6 of this memo verbatim as "the WHI-1448 decision section". Its readiness
  also needs its other blocker, WHI-1447, which the tracker reports as Done.
- The proposed Execution for WHI-1449 is Complexity **high**. Reason: a new search mechanism
  over the ordered-state graph accounting, plus divergence diagnostics and held-out
  statistics. Expected scope: new `routing/algorithms/metis_inspired.py`; its registry entry;
  the `graph.label_hops` / `graph.label_pruning` profile keys, declared only for this
  factory; new `config/metis_challenge/*.yaml` (one per arm, full_gross values plus the two
  keys); `tests/routing/test_metis_inspired.py`; `docs/references/metis-challenge-results.md`
  and small result/provenance artifacts. No edits to `incremental_graph.py`, `path_split.py`,
  existing profiles, base/optimized strategy groups or historical reports.
- The gates stay in force. S1–S3 can stop WHI-1449 before the held-out run, and a
  drop/inconclusive result is reported, never hidden. Research-only completion never marks
  WHI-1449 Done.
- **Core v1 is unaffected.** Release 0.1.0's acceptance (WHI-1447) did not depend on this
  challenge and does not change. `incremental_graph` stays a mandatory base strategy with
  unchanged identity.

## 13. Remaining gaps

- The C1 "modifications", C8 default-mode behavior, C9 split optimizer, Metis's accounting
  for reused DEXs and its budgets are all unknown. No public source code exists.
- The §9.3 dominance argument is a derivation. Gate S2 is its empirical check on real
  CL/LB/CPMM state.
- The §9.4 counts are structural. Actual quotes and time come only from WHI-1449.
- The C7 improvement figure is not evidence for Mantle. This memo draws no quality
  expectation from it.
- J7's reach over readers of public documentation who have not accepted it is a legal
  question left to the owner. This design avoids depending on J6–J8 content.

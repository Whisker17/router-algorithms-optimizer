# NOTICE — `tools/upstream/cfmm` (WHI-1557)

This validation-only harness runs, unmodified and OFFLINE, the authors' reference code of
the CFMM dual-decomposition router, to produce `tests/fixtures/cfmm/author_reference.json`:

- **Paper:** T. Diamandis, M. Resnick, T. Chitra, G. Angeris, *An Efficient Algorithm for
  Optimal Routing Through Constant Function Market Makers*, arXiv:2302.04938v1 (9 Feb 2023,
  the only version). PDF sha256
  `8b26956fb768240791081189506448a70c646aa6bc23bdfa7d5fc700899a5ed0`. arXiv non-exclusive
  distribution licence: cited, not redistributed here.
- **Upstream code:** `https://github.com/bcc-research/CFMMRouter.jl`, commit
  `5932e42e5077ffc7d8e02c3b3ad2e9ed1d441267` (package 0.3.1, git tree
  `dbf991b18897abdcca08647dd753a955f7a477f6`), pinned by `Manifest.toml` (`repo-rev`,
  `git-tree-sha1`; Pkg verifies the tree hash on install).
- **Licence of the upstream code:** MIT, "Copyright (c) 2021 Guillermo Angeris, Theo
  Diamandis"; verbatim copy `LICENSE-CFMMRouter.jl.txt` (sha256
  `99056ed306835ccac7874395818f302f264d91d6f13f28ca6b88440e8cbd042e`).
- **Optimizer it loads:** LBFGSB.jl 0.4.1 (MIT, "Copyright (c) 2018 Yupei Qi"; verbatim
  `LICENSE-LBFGSB.jl.txt`) wrapping `L_BFGS_B_jll` 3.0.1+0 (L-BFGS-B 3.0 by Zhu, Byrd,
  Lu, Nocedal and Morales, BSD-3-Clause; its licence ships in the Julia artifact
  `share/licenses/L_BFGS_B/License.txt`). Other Julia dependencies keep their own licences
  in the Julia depot, which is never committed.

`generate.jl` is MIT-licensed glue that calls the upstream code in-process.
`author_inputs.py` and `python_reference.py` load no upstream code: they write inputs from
this repository's own fixtures and run this repository's model. Use is private and
internal; nothing here is conveyed or relicensed, and repository visibility is unchanged.
Contract of record: `docs/references/research-021/cfmm-dual.md`.

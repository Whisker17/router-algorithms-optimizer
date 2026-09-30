# NOTICE — `routing/cfmm` (WHI-1558, `cfmm_dual` CPMM stage)

Runtime Python port of the CFMM dual-decomposition router's CPMM part. No upstream file is
copied or vendored; the formulas and the dual structure follow the sources below, and the
code is ported from this repository's own validated WHI-1557 contract model.

- **Method:** T. Diamandis, M. Resnick, T. Chitra, G. Angeris, *An Efficient Algorithm for
  Optimal Routing Through Constant Function Market Makers*, arXiv:2302.04938v1 (9 Feb 2023),
  eqs. (5)–(9) and App. A (PDF sha256
  `8b26956fb768240791081189506448a70c646aa6bc23bdfa7d5fc700899a5ed0`). Cited, not
  redistributed.
- **Author reference code:** `https://github.com/bcc-research/CFMMRouter.jl` commit
  `5932e42e5077ffc7d8e02c3b3ad2e9ed1d441267` (`ProductTwoCoin` `find_arb!`, `route!`
  dual/gradient structure). MIT, "Copyright (c) 2021 Guillermo Angeris, Theo Diamandis";
  verbatim licence: [`tools/upstream/cfmm/LICENSE-CFMMRouter.jl.txt`](../../tools/upstream/cfmm/LICENSE-CFMMRouter.jl.txt)
  (sha256 `99056ed306835ccac7874395818f302f264d91d6f13f28ca6b88440e8cbd042e`).
- **Ported from (this repository):** `tests/routing/cfmm_contract_model.py` (sha256
  `7fb979394cbcba243058fe38ebdc37b56602735ab3887475c6386851006ac387`) and
  `tools/upstream/cfmm/python_reference.py` `solve` (sha256
  `d7657c561d194db6aaff4447745e2dd1c6c4a4d868c93586a5c1fd21049c9148`), both at WHI-1557
  merge `91d7f4b056e04dbfe7de0a0b867618875c2efd27`. Neither is imported at runtime.
- **Optimizer:** SciPy 1.18.1 `scipy.optimize.minimize(method="L-BFGS-B")` (BSD-3-Clause;
  L-BFGS-B 3.0 by Zhu, Byrd, Lu, Nocedal and Morales) with NumPy 2.5.3 (BSD-3-Clause),
  installed from the locked PyPI wheels; their licences ship inside the wheels.

Use is private and internal; nothing is conveyed or relicensed. Contract of record:
`docs/references/research-021/cfmm-dual.md`.

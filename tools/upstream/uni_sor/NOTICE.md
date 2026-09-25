# NOTICE — `tools/upstream/uni_sor` (WHI-1443)

This validation-only harness runs, unmodified and in-process, the Uniswap Smart Order
Router routing core:

- **Upstream:** `https://github.com/Uniswap/smart-order-router`, commit
  `04c7c0b4d85ac2a19a3ff53987d21f9c8f1fe647` (package `@uniswap/smart-order-router`
  `4.31.10`).
- **Installed artifact:** the npm tarball `@uniswap/smart-order-router@4.31.10`, integrity
  `sha512-Bmg46KXDSfE1AAf1tPg+vDzMngVoGfzdohekhjJ1hIQG13tXcjEykqapQy+CrJUqXLL3lQvZj2uVEKfzCNH74Q==`
  (built from `f506b99b8a7bf4d6f9c386ee721e0f8adb61d7f9`, whose `src/` tree is identical to
  the pin), pinned in `package-lock.json` with the upstream-lockfile dependency versions
  as npm `overrides`.
- **License of the upstream code:** GNU General Public License v3 (`LICENSE` at the pin;
  verbatim copy: `docs/references/licenses/uniswap-smart-order-router-04c7c0b4-LICENSE.txt`).
  Installed dependencies keep their own license files under `node_modules/`, which is
  never committed; the license inventory is `docs/references/uni-sor-source-inventory.json`.
- **Contract of record:** `docs/references/uni-sor-port-contract.md` (§7 harness, §9.1
  obligations).

`generate.js` (and this directory's `package.json`) are glue that calls the GPL-3.0 code
in-process and are therefore marked `SPDX-License-Identifier: GPL-3.0-only`.
`author_inputs.py` only writes input data from this repository's own fixtures and
simulator; it does not load upstream code. Use is private and internal; nothing here is
conveyed or relicensed, and repository visibility is unchanged.

# `tools/upstream/cfmm` — CFMM dual-routing reference harness (WHI-1557)

Validation-only. Nothing here is imported by the benchmark or by `uv run pytest`; the
tests read the checked-in fixtures in `tests/fixtures/cfmm/`. Contract:
`docs/references/research-021/cfmm-dual.md`. Notices: [`NOTICE.md`](NOTICE.md).

| File | Role | Evidence class |
| --- | --- | --- |
| `author_inputs.py` | writes `author_inputs.json` from repository fixtures (CPMM reserves, a synthetic and a real Uniswap v3 state mapped to the author `UniV3` arguments) | inputs |
| `generate.jl`, `Project.toml`, `Manifest.toml` | runs the pinned CFMMRouter.jl OFFLINE (one thread) and writes `author_reference.json`; `--check` re-runs in memory and requires byte identity | **author execution** |
| `python_reference.py` | this repository's model (`tests/routing/cfmm_contract_model.py`) + SciPy L-BFGS-B in an ephemeral environment: `fixtures` writes `model_reference.json`; `tuning`/`sweep` are the bounded preset probe on the tuning split (outputs stay outside the repo) | **Python model, not author execution** |
| `regen.sh` | end-to-end regeneration of the author fixtures | — |

## Regenerate

Requires Julia 1.10.10 (`JULIA=/path/to/julia`, default `julia`), `uv`, and package
registry access for the first `Pkg.instantiate()` (a mirror may be set with
`JULIA_PKG_SERVER`). At WHI-1557 SciPy was not a project dependency and `uv run --with`
layered it for one command; since WHI-1558 the runtime pins the same `scipy==1.18.1` /
`numpy==2.5.3` in `pyproject.toml`/`uv.lock` (`routing/cfmm/optimizer.py`), so the
`--with` pins below resolve to the locked versions.

```bash
tools/upstream/cfmm/regen.sh
uv run --with scipy==1.18.1 --with numpy==2.5.3 python tools/upstream/cfmm/python_reference.py fixtures
uv run pytest tests/routing/test_cfmm_contract.py -q
```

The Fortran L-BFGS-B inside the author run prints `ascent direction in projection gd = …`
to stdout for one router case (`r-tiny`); it is part of the recorded author behaviour
(the run returns without any status), not a harness error.

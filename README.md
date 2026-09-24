# router-algorithms-optimizer

An offline Python benchmark for Mantle swap-routing algorithms: freeze one real
liquidity snapshot, compare exact-input solvers with a common evaluator, and report
quality/latency/cost trade-offs. Uniswap SOR is a required scoped comparator;
Jupiter/Metis is a separate research challenge.

The spec of record is `docs/DESIGN.md`. Issue design and published tracking mappings are in
`docs/ISSUE_PLAN.md` and `docs/RELEASE_PLAN.md`.

## Workflow & Development

This repository follows a PRD-first, Linear-tracked, worktree-per-issue workflow:

- **Tracker**: Linear project **Mantle Router Algorithm Optimizer** (team `Whisker-Personal` / `WHI`).
- **Git workflow**: Main + dev + version integration branches. Base branch resolution is mandatory:
  - Version-scoped work targets `origin/release/v{version}` (or `dev` during initial bootstrap before the first production tag).
  - Repo-wide governance targets `origin/dev`.
  - Hotfixes target `origin/main`.
- **Review loop**: 3-round review loop via `scripts/agent-dispatch.sh` before self-squash-merge; promotions to `main` and integration-branch merges back to `dev` remain human gates.

## Build, Test, Run

```bash
uv sync                                  # install deps (creates .venv)
uv run pytest                            # unit tests
uv run pytest tests/test_smoke.py        # single test file
uv run ruff check .                      # lint
uv run mypy                              # type check
uv run python main.py                    # entrypoint
```

## Agent runtime & skills

Runtime role mappings live in `config/agent-roles.conf`. Verify runtime dispatch with:

```bash
scripts/agent-dispatch.sh --probe
```

Skills inventory lives in `.claude/skills/`. See `AGENTS.md` for detailed agent instructions.

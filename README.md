# router-algorithms-optimizer

An offline Python benchmark for Mantle swap-routing algorithms: freeze one real
liquidity snapshot, compare exact-input solvers with a common evaluator, and report
quality/latency/cost trade-offs. Uniswap SOR is a required scoped comparator;
Jupiter/Metis is a separate research challenge.

The full PRD will live in `docs/DESIGN.md`.

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

## Locally customized skills

Skills are pinned by `skills-lock.json`; upgrade them deliberately, not per-project.

> ⚠️ **Locally customized skills** — `skills-lock.json` records the *upstream* hash, so it
> cannot detect these edits and **re-vendoring via `/setup-matt-pocock-skills` will
> silently overwrite them.** Diff before accepting any skill upgrade to:
>
> - `implement/SKILL.md` — three-round review loop + escalation pass; self-merge
>   authorization; `REVIEWER`/`ESCALATOR` role dispatch; ponytail generation
>   constraint + shrink pass before review
> - `code-review/SKILL.md` — `REVIEWER` role dispatch on both axes; Reinvented Wheel
>   smell on the Standards baseline
> - `improve-codebase-architecture/`, `codebase-design/DESIGN-IT-TWICE.md`,
>   `wayfinder/SKILL.md` — `EXPLORER` role dispatch with a documented serial fallback
> - `ask-matt/SKILL.md` — runtime-neutral compaction wording; implement drives
>   tdd + ponytail
> - `orchestrate/` — first-party, not vendored; do not add it to `skills-lock.json`.
>   Trap registry lives in `docs/TRAPS.md` (skill-local `traps.md` is a pointer).
>   Verify checks the implementer's rung report.
> - `ponytail/` — first-party, not vendored; do not add it to `skills-lock.json`.
>   Generation constraint driven by `/implement`, not a process skill.

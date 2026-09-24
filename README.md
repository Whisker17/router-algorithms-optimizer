# router-algorithms-optimizer

An offline Python benchmark for Mantle swap-routing algorithms: freeze one real
liquidity snapshot, compare exact-input solvers with a common evaluator, and report
quality/latency/cost trade-offs. Uniswap SOR is a required scoped comparator;
Jupiter/Metis is a separate research challenge.

The full PRD is in [docs/DESIGN.md](docs/DESIGN.md).

## Workflow & Development

This repository follows a PRD-first, Linear-tracked, worktree-per-issue workflow:

- **Tracker**: Linear project **Mantle Router Algorithm Optimizer** (team `Whisker-Personal` / `WHI`).
- **Git workflow**: Main + dev + version integration branches. Base branch resolution is mandatory:
  - Version-scoped work targets `origin/release/v{version}` (or `dev` during initial bootstrap before the first production tag).
  - Repo-wide governance targets `origin/dev`.
  - Hotfixes target `origin/main`.
- **Development checks**: ordinary issue PRs may self-squash-merge after acceptance, required tests/lint/type checks, scope and mergeability checks. No fixed per-issue review loop.
- **Release review**: independently review the complete integrated release (Standards + Spec), validate end-to-end behavior and resolve blockers before release. Promotions to `main` and finished integration-branch merges into `dev` remain human gates; see `docs/GIT_WORKFLOW.md` § Release review gate.

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
> - `implement/SKILL.md` — validation-driven issue self-merge; independent whole-release
>   review; optional focused review; ponytail generation constraint + shrink pass
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

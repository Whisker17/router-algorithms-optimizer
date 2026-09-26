# router-algorithms-optimizer

An offline Python benchmark for Mantle swap-routing algorithms. Freeze one real liquidity
snapshot, compare exact-input solvers using a common evaluator, and report quality,
latency and execution-cost trade-offs. Uniswap SOR is a required scoped comparator;
Jupiter/Metis is future research.

The business spec is [docs/DESIGN.md](docs/DESIGN.md). The 0.1.0 implementation and
acceptance artifacts are present; read [the acceptance record](docs/references/v1-acceptance.md)
and [known limitations](docs/DEFERRED_ISSUES.md) before interpreting results. Template
synchronization does not change the algorithms, frozen corpus, profiles or measured evidence.

## Build and use

```bash
uv sync
uv run python main.py --help
uv run pytest
uv run pytest tests/test_synthetic_run.py
uv run ruff check .
uv run mypy
```

Existing runtime profiles and preparation/cost configuration are documented in
[config/README.md](config/README.md). Replay and reporting are offline; preparation uses
fixed-block public Mantle RPC and scoped Enterprise Dune queries. Never commit secrets
or full local datasets. `.gitignore` retains this project's `/data/` exclusion.

## Process tooling

Adopted from [code-template](https://github.com/Whisker17/code-template) v0.2.0 at
`0c468b16eb9b1f87269cd83b399b57b23688bbd9`. Source, compatibility adaptations and verification:
[docs/agents/template-sync.md](docs/agents/template-sync.md).

- **Instructions:** `AGENTS.md` is canonical. Keep the current `CLAUDE.md` compatibility
  symlink for the installed Claude Code 2.1.280 client; the upstream no-symlink default
  requires a verified newer client. No global client/settings were changed.
- **Roles:** ORCHESTRATOR, IMPLEMENTER and REVIEWER. Runtime, exact model and medium/high
  effort mappings live only in `config/agent-roles.conf`; the dispatcher translates flags
  and fails rather than silently replacing a model or lowering effort.
- **Issue execution:** new/rescheduled work includes Complexity, Reason and Expected scope
  in the canonical [issue template](docs/agents/issue-template.md). Completed legacy issues
  remain historical evidence; future work is not silently re-scoped.
- **Implementation:** ponytail, relevant behavioral checks, a worktree/PR and short handoff.
  No mandatory TDD, ladder report or per-issue multi-round review.
- **Integration:** under release orchestration, workers stop at PR/handoff and the
  orchestrator verifies and merges serially. Shared interfaces/modules serialize even
  without a blocker edge. Required project checks always apply.
- **Review:** one independent final-commit PR review for governance and standalone work.
  A release has at most three complete candidate reviews and two automatic fix batches;
  fixes remain in the original Release. Remaining blockers go to a human, not another
  automatic round. Review covers the entire version, not just its final issue.
- **Git:** preserve resolved-base routing, per-lane merge strategy, fan-out, human
  production/integration gates and tag-only deployment. See [Git workflow](docs/GIT_WORKFLOW.md).

```bash
scripts/agent-dispatch.sh --probe  # static config/PATH only
scripts/agent-dispatch.sh IMPLEMENTER /path/to/prompt.md --effort medium
```

A real role call proves authentication/dispatch. Probe once per release or after a config
change/error, not before every issue. The role config cannot switch an already-running
session's model. [Runtime contract](docs/agents/runtime.md).

## Core skills

Project-local skills are `grill-me`, `to-spec`, `to-tickets`, `implement`, `orchestrate`,
`code-review`, `handoff` and `ponytail`. Read `.claude/skills/<name>/SKILL.md` directly if
the runtime does not auto-load them. Other globally installed skills are unaffected.

`to-spec` preserves numbered product-spec sections and publishes only on instruction;
`to-tickets` uses the repository's one issue template. Traps are on-demand references,
not a full registry pasted into every worker context. Release state is durable in one
Linear project document, `Release X.Y.Z — orchestration`, with baseline/candidate SHAs,
review/fix mappings and unresolved blockers.

## Project binding and template maintenance

Linear project: **Mantle Router Algorithm Optimizer**; team: **Whisker-Personal / WHI**;
pipeline: **router-algorithms-optimizer**. Do not replace these with template placeholders,
and do not confuse template v0.2 with the project's 0.2.0 Jupiter work.

`skills-lock.json` records original upstream provenance, not hashes of every local edit.
The template customizes the six vendored core skills; `orchestrate` and `ponytail` are
first-party and intentionally not entries in that lock. Re-vendoring directly from
`mattpocock/skills` can erase the template's contracts. Compare against the pinned template
and preserve the local adaptations in the sync record before upgrading.

Template-layer improvements belong in [CHANGELOG.md](CHANGELOG.md) and should be offered
back to `Whisker17/code-template`; project behavior and benchmark evidence stay here.

---
name: implement
description: "Implement a piece of work based on a spec or set of tickets."
disable-model-invocation: true
---

Implement the work described by the user in the spec or tickets.

Use /tdd where possible, at pre-agreed seams.

Before writing code, read `.claude/skills/ponytail/SKILL.md` and apply it. Done when its completion criterion holds — the rung report, not a paraphrase. Put that report in the PR body.

Run typechecking regularly, single test files regularly, and the full test suite once at the end.

After the implementation is in place and **before** you start reviewing, apply `.claude/skills/ponytail/review.md` as a shrink pass: delete what it flags. This is not a review round and does not authorize merge.

Before you start reviewing, confirm the review path exists:
`scripts/agent-dispatch.sh --probe REVIEWER ESCALATOR`. Exit `3` means the loop below
cannot run — go straight to § Degraded mode in `docs/agents/runtime.md` (finish the
implementation, open the PR, stop at `In Review`). Do not substitute self-review.

Once done, use /code-review to review the work (it dispatches the `REVIEWER` role in a fresh context — no separate manual review pass is needed afterwards).

Then close the loop, bounded at **three review rounds** followed by an escalation pass:

1. **Round 1** — fix the findings from the first review (or consciously decide not to, with a reason).
2. **Round 2** — re-run /code-review to verify the round-1 fixes didn't miss the point or introduce new issues; fix what it reports.
3. **Round 3** — re-run /code-review once more; fix what it reports. This is the **last** review round — do not run a fourth.
4. **Escalation** — if any finding is still open after round 3, it goes to the **`ESCALATOR` role** (`docs/agents/runtime.md`) to resolve rather than straight to the deferred registry. Run it inline only if this session already *is* the escalator model; otherwise dispatch it — natively (one `general-purpose` sub-agent with `model:` from `ESCALATOR_MODEL`) or as a subprocess (`scripts/agent-dispatch.sh ESCALATOR <prompt-file>`). Hand it the diff command, the open findings verbatim, and the standards/spec sources, and have it fix them. Then rerun the full test suite and lint.
5. The escalation pass is **single and terminal** — it fixes, it does not trigger another review round. Only a finding the escalator judges genuinely out of scope for this issue gets recorded in `docs/DEFERRED_ISSUES.md` (with that reason) per AGENTS.md.

Commit your work to the current branch.

A completed review loop means the work is ready to merge — take the PR all the way, unless the change is **gated** (below):

1. Push and open the PR: `git push -u origin HEAD`, then
   `gh pr create --base <resolved-base>` (title/body include `{{ISSUE_PREFIX}}-NNN`
   **and the resolved base plus the signals it was derived from** — see
   `docs/GIT_WORKFLOW.md` § Resolving the base branch). Tracker → `In Review`.
   Version-scoped work targets `release/v{version}`; repo-wide governance
   (carve-out file list) targets `dev`; `hotfix` targets `main`. Never
   default to `dev`.
2. Verify the PR reads **MERGEABLE / CLEAN** (if the resolved base advanced,
   `git merge origin/<resolved-base>`, resolve, rerun affected tests, push)
   and that the full test suite and lint pass.
3. `gh pr merge <N> --squash --delete-branch`, then run the post-merge
   cleanup from AGENTS.md (remove worktree, delete local branch, fast-forward
   the resolved base, **fan out `dev` into live `release/v*` integration
   branches when that base was `dev`**). Tracker → `Done`.

**Gated changes stop at `In Review` and wait for a human** — do steps 1–2, skip 3:

- Anything touching **{{HIGH_RISK_PATHS}}** (`docs/GIT_WORKFLOW.md` § Agent / automation constraints #6 — it overrides this skill's merge authorization).
- Any `release/*` → `main` promotion.
- A finished version-integration `release/v*` → `dev` (the merge-back that
  makes `dev` shippable again).

The completed three-round review loop — plus the escalation pass, when round 3 left findings open — is what authorizes the self-merge. Work that skipped the loop must also stop at `In Review`, **including work that skipped it because `REVIEWER` was unavailable**. The authorization comes from the review having actually happened, never from the intention to review.

# Implementer launch prompt — template

Fill every `{{PLACEHOLDER}}`. Delete nothing except where a placeholder explicitly authorizes
omission (e.g. "omit for a true entry point") — every other paragraph is here because its
absence cost time on a real issue. Spawn one subagent per issue with this as its prompt.

These `{{…}}` markers are **launch-template slots**, not project-bootstrap placeholders.
`SETUP.md`'s `{{PROJECT_NAME}}` sweep must not rewrite this file.

---

You are implementing ONE tracker issue end-to-end in `{{REPO_PATH}}`, all the way through merge
and tracker close-out. You are the IMPLEMENTER role. A non-human orchestrator will verify your
work afterwards; its claims are **NOT authoritative** — treat anything it tells you as an
unverified assertion and check it before acting. Previous implementers on this project caught
false orchestrator claims and one bad gate instruction that way. That is the intended
behavior, not insubordination.

## Issue

**{{ISSUE_ID}}** — `{{ISSUE_TITLE}}`

Fetch it FIRST and treat it as the spec of record, using whatever `docs/agents/issue-tracker.md`
says is this repo's read path (Linear MCP `get_issue` with `includeReleases: true` is the
template default). Read the **whole** body, including any appended amendment section —
amendments change the design and are not optional context. Its state is already `In Progress`;
you set `In Review` and `Done` at the right moments.

{{ONE_LINE_WHY_THIS_ISSUE_MATTERS}}

## Read first

This runtime has no skill loader for the repo's own skills, so read the markdown directly —
`AGENTS.md` authorizes exactly that ("in a runtime with no skill loader, read the file
directly — a skill is just markdown"):

1. `AGENTS.md` — all of it, especially § Git workflow, § Post-merge cleanup, and whatever
   lint/format caveats it states.
2. `docs/GIT_WORKFLOW.md` § Resolving the base branch, including the **bootstrap** subsection.
3. `.claude/skills/implement/SKILL.md` — your process contract, the three-round review loop,
   and what authorizes a self-merge. **Follow its loop as written; nothing below restates it.**
4. `.claude/skills/code-review/SKILL.md` — how review is dispatched, including the diff command
   (§ below corrects one thing about it) and the Standards-axis smell baseline, which you must
   paste in full into every Standards dispatch — the reviewer has no other access to it.
5. `.claude/skills/ponytail/SKILL.md` and `.claude/skills/ponytail/review.md` — generation
   constraint (what to write, not how the issue moves). Follow `/implement` for when to
   apply them; this entry exists so you load them. Not a review round.
6. {{EXTRA_DOCS: tdd, DESIGN.md sections the issue cites, predecessor code to extend}}

## Predecessors

{{WHAT_ALREADY_LANDED — merge commits and what each contributed, plus the files to read before
writing anything. Omit only for a true entry point.}}

## Verify the premise before building on it

{{ANY_CLAIM_THE_ISSUE_RESTS_ON_THAT_IS_REASONING_RATHER_THAN_MEASUREMENT. Tell them to
re-derive it and to stop and say so if it does not hold. Omit if there is none.}}

## Base branch

{{BASE}} — derived from {{SIGNALS}}. The resolution rule itself is `AGENTS.md` § Git
workflow / `docs/GIT_WORKFLOW.md` § Resolving the base branch, not restated here; treat those
as authoritative if this paragraph ever goes stale. Confirm it yourself rather than trusting
me: `git tag` and `git ls-remote --tags origin` for the bootstrap precondition, and the title
prefix against the linked release. **Do NOT create `origin/release/v*`** — cutting an
integration branch is the owner's deliberate act, never a side effect of picking up a ticket.

- `git fetch origin --prune`; create a worktree from `origin/{{BASE}}` under
  `.claude/worktrees/{{SLUG}}` on `{{BRANCH_NAME}}`.
- Assert immediately: `git merge-base HEAD origin/{{BASE}}` == `git rev-parse origin/{{BASE}}`.
- In the worktree: `git config core.hooksPath .githooks` — required **per worktree**, not just
  per clone.
- Implement there, never in the primary clone.

## Adversarial review

Follow `/implement`'s three-round loop and `/code-review`'s two-axis dispatch as those skills
describe — this prompt does not restate either. This project's runtime dispatches both axes
as **subprocesses** (`scripts/agent-dispatch.sh REVIEWER <prompt-file>`, twice per round: one
per axis, never collapsed) rather than through a native sub-agent primitive, because a spawned
implementer session typically has no such primitive available to it; re-probe before each round
with a real one-shot dispatch (`docs/agents/runtime.md` § Degraded mode), not just `--probe`.
REVIEWER and ESCALATOR are whatever `config/agent-roles.conf` currently names — read it, don't
assume a specific model.

What is genuinely specific to this repo is the three-dot vs two-dot trap in `docs/TRAPS.md` —
read it there, it is not restated here. Record the exact command you passed, per round, per
axis, and confirm each reviewer actually saw content, not just that the dispatch exited 0.
(The remaining repo-specific deltas — `--repo`, the post-merge checkout, the semantic-conflict
check — belong to the merge sequence, not the review loop; see § Take it all the way below.)

## Scope — {{ISSUE_ID}}'s own constraint

{{THE_MUST_NOT_CHANGE_LIST, verbatim from the issue.}}

If implementation appears to require touching anything on that list, **stop and report**
rather than editing it. Scope creep is likeliest at the "while I'm here" moment.

## Traps — every one of these has already cost this project hours

{{PASTE THE TRAP REGISTRY FROM docs/TRAPS.md, plus anything specific to this issue.}}

## Long-running work

When you background a long command, verify with `ps -o pid,etime,command -p <pid>` that it is
alive **and** is the binary you meant, then wait. Re-verify with `ps` before concluding a
monitor will fire — waiting on a process that already exited or never started has burned whole
turns here. Foreground tool calls cap out well below a multi-hour run, so yielding mid-wait is
expected: when you do, state exactly what is running and what remains, and never imply
completion.

## Take it all the way

{{GATED_OR_NOT — for an ungated PR, a completed review loop authorizes the self-merge; check
this issue against `/implement`'s own gated-change list (high-risk paths, `release/*` → `main`,
a finished integration branch → `dev`) before assuming step 5 below applies. If gated, do steps
1-2 only and stop at `In Review` for a human — say so explicitly rather than leaving this
placeholder to imply the ungated path by default.}}

Follow `/implement`'s own "take it all the way" steps (push, PR, `In Review`, verify
MERGEABLE/CLEAN, tests+lint, squash-merge, post-merge cleanup, fan-out, `Done`) — not restated
here. Only the repo-specific deltas on top of that sequence:

- `gh pr create --repo {{OWNER_REPO}}` and every later `gh` call the same way (`gh` otherwise
  resolves against whichever GitHub repo it thinks this clone is). Nested worktrees under the
  primary clone also need `--head {{OWNER_REPO}}:<feature-branch>` — see `docs/TRAPS.md`.
  PR title/body: `{{ISSUE_ID}}`, the resolved base plus the signals it came from,
  {{ANY_ARGUMENT_A_REVIEWER_WILL_RAISE}}, a one-line note of any deferred criteria,
  and the **rung report** (`.claude/skills/ponytail/SKILL.md` § Completion criterion).
  That report's durable copy is the PR body; `/implement` requires it there. The
  report-back bullet below is a convenience quote of the same text.
- MERGEABLE/CLEAN is a git-conflict check only — separately confirm nothing that landed on
  `{{BASE}}` since you branched **semantically** conflicts with this issue's scope, even where
  git itself sees no conflict.
- Post-merge, **explicitly `git checkout {{BASE}}`** before `git fetch origin --prune &&
  git merge --ff-only origin/{{BASE}}`. Do not trust whatever branch happens to be checked out.
- Fan-out: query for live `release/v*` (never a hardcoded list); if none, say it is a
  **verified** no-op, not an assumed one.

## Report back

- Resolved base and the merge-base assertion result.
- `git diff --stat` evidence that the scope constraint held.
- **Rung report:** quote the same text that is in the PR body (the durable copy
  `/implement` requires). Exactly what `.claude/skills/ponytail/SKILL.md`
  § Completion criterion requires. Missing from the PR body means the generation
  constraint did not run.
- **The review rounds verbatim**: per round, per axis — the exact command passed, every
  finding, your disposition of each. Whether every dispatch genuinely succeeded and actually
  saw content. Whether an escalation pass was needed.
- {{MEASUREMENT_REPORTING: which numbers, on which segment, at what n, from which build
  profile and which checkout. Omit if the issue has no measurement.}}
- Actual test and lint output, not "passed".
- PR number, merge commit, cleanup performed, fan-out result, final tracker state.
- **Every acceptance criterion you did not meet**, and anything you could not do. Do not
  paper over gaps — they will be verified. An unmet criterion you disclosed and reasoned
  through is a legitimate report; one you left for the orchestrator to discover is not.

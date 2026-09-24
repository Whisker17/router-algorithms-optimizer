---
name: implement
description: "Implement one issue with fast validation-driven iteration; perform comprehensive independent review at release time."
disable-model-invocation: true
---

Implement the requested issue and its acceptance criteria. Read the complete issue,
including amendments, `AGENTS.md`, the relevant DESIGN sections and the resolved git
workflow before changing files. Use one issue worktree from the verified base; never
implement in the primary clone.

## Implement and validate

1. Apply `.claude/skills/ponytail/SKILL.md` before writing code. Reuse existing code and
   keep the smallest change that meets the spec. Use `/tdd` at the pre-agreed behavioral
   seams where appropriate.
2. Run focused tests/type checks during implementation. Once the change is complete,
   run the repository's required tests, lint and type checks. Record actual evidence and
   every unmet acceptance criterion; do not treat template smoke coverage as product proof.
3. Apply `.claude/skills/ponytail/review.md` as the author's shrink/self-check. Inspect the
   diff for scope, correctness and semantic conflicts with the advanced base. Put the
   required rung report in the PR body. This is not independent review.
4. There is **no mandatory per-issue independent-review loop, fixed round count or
   automatic escalation pass**. Use a focused `/code-review` when explicitly requested or
   a concrete unresolved concern warrants it. State that review's scope and whether it
   is a required issue gate. Do not invoke reviewer preflight for every ordinary issue.
5. Commit only this issue's changes. Resolve any failed check or unmet required criterion
   before claiming completion; report real blockers instead of quietly weakening scope.

## Take an ordinary issue through merge

For ordinary issue PRs into `dev` or a live version-integration branch:

1. Push and open a PR against the **resolved base**; title/body carry `WHI-NNN` and the
   routing signals (title prefix plus actual Release, or governance carve-out). Tracker
   moves to `In Review` when the PR opens.
2. Verify MERGEABLE/CLEAN, required test/lint/type evidence, acceptance and scope. Check
   semantic conflicts if the base advanced; resolve and rerun affected checks.
3. When these gates pass, self-squash-merge, run the mandatory post-merge cleanup/fan-out
   from AGENTS.md, then set the tracker to `Done`. No separate human approval or reviewer
   availability is required merely because independent issue review was not run.

An explicit issue-specific review requirement is still binding. A failed check, unresolved
correctness concern or unmet acceptance criterion is not made acceptable by the faster
workflow.

## Release and human gates

Systematic independent review is performed over the **whole integrated release**, per
`docs/GIT_WORKFLOW.md` § Release review gate. Pin the candidate and baseline, use separate
fresh Standards/Spec contexts, run full relevant regression/end-to-end acceptance, fix
blockers and verify the changed areas. No preset count of review rounds or escalation
ladder is imposed. Required reviewer unavailability blocks release, not ordinary issue
iteration. `ESCALATOR` is optional expert help for a specific hard question.

Existing human gates remain: configured high-risk paths (none for this repo),
`release/*` → `main` promotion, and a finished version-integration branch → `dev`.
Complete required release evidence and leave those gated PRs at `In Review` for the human;
do not confuse successful issue tests with release approval. Preserve per-lane merge
strategies and tag/deploy rules.

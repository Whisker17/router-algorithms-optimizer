> **Canonical home.** This file is the trap registry. `/orchestrate` pastes it into
> every implementer launch prompt. `.claude/skills/orchestrate/traps.md` is a
> pointer, not the list — append entries here (a version-scoped docs PR, not a
> governance PR). Downstream projects seed this file with *their* measured traps;
> do not copy another repo's numbered list in wholesale.

# Trap registry — append-only, repo-specific

This registry is the **durable channel** by which a trap reaches every implementer after the
first one it's handed to — `implementer-prompt.md`'s launch prompt pastes it verbatim into
every issue's prompt. (`SKILL.md`'s "Feed forward" step already names the other, immediate
channel: mentioning a fresh trap directly in the very next launch prompt, which takes effect
with no commit at all but doesn't outlive that one issue.) An entry that never makes it into
this file does not survive past whichever single issue it may have been mentioned to by hand.
Carry every entry into each launch prompt. Cost is why they are here. Each entry below is
cited against the repo so it stays checkable; an entry that stops being true belongs in
`docs/DEFERRED_ISSUES.md`'s own resolved section, not silently deleted here.

**A trim is self-declaring.** An entry leaves this file in one of exactly two ways: it
graduates to `docs/DEFERRED_ISSUES.md`'s resolved section once it stops being true (the
paragraph above), or it gets merged into another entry that already covers the same failure.
Either way, the PR that does it must say which entry number left or was absorbed, and why.
A trim with no disclosure of what left and why, stated in the PR that does it, is presumed
accidental, not reviewed.

The numbered entries below are **process-layer** traps measured while this skill was
dogfooded. Stack-specific traps (formatter scoping, measurement-binary paths, report-slot
collisions) belong in the downstream project's copy of this file, not here.

1. **`gh pr create` (and every later `gh` call) needs `--repo <owner/repo>` whenever
   `gh` would resolve the wrong GitHub repo.** A fork, a `no_push` `upstream`, or
   `gh repo view` reporting a different `nameWithOwner` than `origin` are the usual
   causes. Verify with `gh repo view --json nameWithOwner` before the first `gh pr
   create` of an issue.
2. **Nested worktrees under the primary clone need `--head <owner/repo>:<feature-branch>`
   as well.** `gh pr create` without `--head` infers the primary clone's checked-out
   branch (often `dev` or `main`), not the worktree's. After create,
   `gh pr view --repo <owner/repo> <N> --json headRefName` must equal the worktree
   branch. If it is the trunk, close the PR and recreate — do not push more commits
   onto a PR whose head is the trunk.
3. **Commit before dispatching a reviewer, then hand it a three-dot diff, and verify it
   is non-empty before dispatching.** `/code-review`'s canonical command is
   `git diff <fixed-point>...HEAD` (three-dot, against the merge-base). Two-dot
   (`git diff <base>`) looks like the fix for an empty three-dot on an uncommitted
   branch, but once `<base>` advances it shows the reviewer those foreign commits,
   reversed. Commit first; use three-dot; refuse to dispatch on an empty range.
4. **`--probe` is not G1.** `scripts/agent-dispatch.sh --probe` only checks that the
   binary resolves. A role whose binary is on `PATH` but whose auth/quota/flags fail
   still reports `ok`. The gate is a real one-line dispatch that must print
   `DISPATCH-OK` and exit 0 (`docs/agents/runtime.md` § Preflight). A failed G1 means
   the review loop did not run — stop at `In Review`, never self-review.
5. **Empty reviewer stdout is a vacuous gate, not a clean pass.** Exit 0 with no
   findings-shaped content is indistinguishable from "the dispatch never ran." Inline
   the three-dot diff (and any file the spec needs) in both axis prompts rather than
   depending on the reviewer to run git; if stdout is empty or a transport error,
   retry once, then stop and report.
6. **`pgrep -fl` dumps this machine's entire shell-snapshot environment** instead of the
   one process you meant to find. Use `pgrep -f <pat> | head -1` to get the pid, then
   `ps -o pid,etime,command -p <pid>`.
7. **Generate committed measurement artifacts once, after the review loop closes, from a
   clean committed sha.** Reviewers read `git diff <fixed-point>...HEAD`, not generated
   reports — regenerating a report per round turns every prose finding into a full
   re-measurement. If the tree is dirty at generation time, stamp provenance accordingly
   rather than silently attributing a dirty run to a later commit.

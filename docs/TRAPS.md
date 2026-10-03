> **Canonical home.** This file is the on-demand project trap registry. Include only
> entries relevant to the current issue in an implementer handoff; do not paste the
> complete registry into every prompt. Append durable findings here (a version-scoped
> docs PR, not a governance PR). The former skill-local pointer was removed by the
> code-template v0.2 synchronization.

# Trap registry — append-only, repo-specific

This registry preserves reusable project evidence across sessions. Feed a relevant new
trap directly into the next affected issue/handoff and retain it here when it outlives
that task. The current orchestrator reads this file on demand and selects applicable
entries instead of injecting all historical material. Each entry stays checkable; an
obsolete lesson belongs in the resolved section of `docs/DEFERRED_ISSUES.md`, not in a
silent deletion. WHI-1500 adapts the process wording to template v0.2 and preserves all
seven numbered entries.

**A trim is self-declaring.** An entry leaves this file in one of exactly two ways: it
graduates to `docs/DEFERRED_ISSUES.md`'s resolved section once it stops being true (the
paragraph above), or it gets merged into another entry that already covers the same failure.
Either way, the PR that does it must say which entry number left or was absorbed, and why.
A trim with no disclosure of what left and why, stated in the PR that does it, is presumed
accidental, not reviewed.

The numbered entries below are **process-layer** traps measured while this skill was
dogfooded. Stack-specific traps (formatter scoping, measurement-binary paths, report-slot
collisions) belong in the downstream project's copy of this file, not here.

**Review applicability (WHI-1472):** reviewer-specific entries apply only when independent
review is requested or required. They do not create an ordinary per-issue reviewer gate.
The current development/release policy in `docs/GIT_WORKFLOW.md` is authoritative.
Entries 4 and 7 retain their measured lessons with that scope; no numbered entry is removed.

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
4. **`--probe` is not proof that a required review can run.** It only checks binary
   discovery. Authentication, quota and flags need a real bounded dispatch (for example,
   one that prints `DISPATCH-OK` and exits 0; see runtime § Preflight). If required
   release review cannot run, keep its PR open and tracking issue at `In Review`; do not
   claim self-review as independent evidence. Governance, standalone and hotfix lanes
   that require pre-merge review also remain blocked on that review. Ordinary version
   issues under release orchestration can continue after their own validation gates.
   Use the configured role and explicit `--effort high`, without model/effort substitution.
5. **Empty reviewer stdout is a vacuous gate, not a clean pass.** Exit 0 with no
   findings-shaped content is indistinguishable from "the dispatch never ran." Inline
   the three-dot diff (and any file the spec needs) in the single reviewer prompt rather
   than relying on a later git invocation. Empty or transport-error output is not a pass:
   record it, diagnose the dispatch and stop the gated step; no blind retry or fallback.
6. **`pgrep -fl` dumps this machine's entire shell-snapshot environment** instead of the
   one process you meant to find. Use `pgrep -f <pat> | head -1` to get the pid, then
   `ps -o pid,etime,command -p <pid>`.
7. **Generate measurement artifacts from the validated candidate and bind them to its
   commit/data/config identities.** Run the full acceptance required for each release
   candidate/fix batch by `docs/GIT_WORKFLOW.md` and `/orchestrate`; do not substitute
   evidence from an older SHA. Ordinary version issues under orchestration do not rerun
   full E2E or regenerate reports merely to populate a handoff. If generation uses a
   dirty tree, record that provenance rather than attributing it to a clean commit.

# Template Changelog

Improvements to the template layer, newest first. When a downstream project ports a
change back (see "Template feedback loop" in `AGENTS.md`), record it here so other
projects can pick it up deliberately.

Format: date — what changed and why — which project surfaced it (if any).

---

## 2026-08-25 — `/ponytail` generation constraint

Distilled from [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail)
`@2ed6c52c9d7e5e56942508591085fd45dea277d3`. The upstream plugin cannot reach
worktree implementers (no skill loader; Grok hooks cannot inject instructions),
so the ladder is a first-party skill wired into `/implement` and the
orchestrator's launch prompt — not an always-on `AGENTS.md` dump and not a
third `/code-review` axis.

- **New first-party skill** `.claude/skills/ponytail/` (`SKILL.md`, shrink
  checklist `review.md`). Not in `skills-lock.json`.
  `disable-model-invocation: true`. Repo overrides live in the skill: spec
  binds capability, ladder binds how; `/tdd` wins tests; designed modules win
  line-count; process report is not debt. No intensity modes — the skill *is*
  full behavior (lowest rung that satisfies the spec).
- **`/implement`** reads the skill before writing (completion criterion lives
  in the ponytail skill; the rung report goes in the PR body) and runs a
  shrink pass before round-1 `/code-review`. The shrink pass is not a review
  round and does not authorize merge.
- **`implementer-prompt.md`** adds a mandatory Read-first entry and a Report
  back pointer at the ponytail criterion; `/orchestrate` Verify reads the
  rung report from the PR body and cross-checks it against the merge; "no
  hits" greps are re-run on `<merge-sha>^`, not the merge tree.
- **`code-review`** Standards baseline gains **Reinvented Wheel** (stdlib /
  platform / already-installed dep). Two axes stay two axes.
- **Carve-out** now includes `README.md` and `CHANGELOG.md`, so a skills PR
  is no longer mixed by having to edit the customized-skills inventory and
  this file.

Do not install the upstream host plugin as the load-bearing path. The
distilled skill is the only activation path the template depends on.

---

## 2026-08-24 — `/orchestrate` skill

Ported back from `prop-amm-challenge` (WHI-1251 through WHI-1255). Four consecutive
issues there were driven by one orchestrator spawning one implementer per issue, with
adversarial review in a fresh context. The pattern worked — and wasted hours on traps
that recur — so the orchestrator's contract is now a skill rather than a remembered
habit.

- **New first-party skill** `.claude/skills/orchestrate/` (`SKILL.md`, fill-in
  `implementer-prompt.md`, pointer `traps.md`). Not vendored from `mattpocock/skills`;
  not in `skills-lock.json`. `disable-model-invocation: true` — entered only on an
  explicit human request, matching `/implement`.
- **Canonical trap registry is `docs/TRAPS.md`**, not the skill file. Skill-local
  `traps.md` is a pointer. Append-only, self-declaring trim: a silent trim is how
  three measured entries vanished between a reviewed 9-entry draft and what first
  landed. Downstream projects seed the numbered list with *their* traps; the template
  ships only process-layer entries (wrong `gh` repo, nested-worktree `--head`,
  three-dot vs two-dot, probe ≠ G1, vacuous empty reviewer stdout, `pgrep -fl`,
  generate-once-after-review).
- **`ask-matt`** routes a ready ticket *set* to `/orchestrate` rather than only
  per-ticket `/implement`. `AGENTS.md` skill table has an `/orchestrate` row.
- **What this skill is not.** It drives a set of already-written tracker issues. It
  does not invent the backlog from a vision, and it cannot self-merge when G1 (a real
  `DISPATCH-OK` dispatch, not `--probe`) fails.

**Do not port back the source project's numbered trap list.** Those cite `cargo fmt`,
a bench compile path, and a fork of a specific upstream — they are not template-true.

---

## 2026-08-14 — Version-routed PRs, fail-closed base resolution, fan-out

Ported back from `mantle-stocks-arbitrage-bots` (`8249baf`, `639c85d`), which hit the
failure this template's main+dev model sets up: every PR merged into `dev`, so when
production broke, the hotfix's backmerge landed on a trunk carrying ~15 commits of
unreleased, never-live-tested next-version work. The trunk was no longer shippable.

- **PRs route by version, fail closed.** A three-category resolution table replaces
  "one PR into `dev`": hotfix → `main`; repo-wide governance (a named carve-out file
  list — workflow docs, hooks, CI, PR template, agent-roles conf + dispatch script,
  skills) → `dev`; everything else → `release/v{version}`. The version comes from two
  independent signals — the issue-title prefix `[X.Y.Z]` (primary,
  tracker-independent) cross-checked against the tracker's release binding
  (`docs/agents/issue-tracker.md`, where the Linear-specific sharp edges now live:
  Release field, **not** `projectMilestone`). Disagree or missing → **refuse to
  start**. Never default to `dev` — a silent fallback recreates the trunk pollution
  invisibly. Bootstrap clause: before the first production tag, the version-scoped
  row resolves to `dev` (no hotfix lane exists yet), as a table row, not a default.
- **Two `release/*` lifecycles.** Temporary cut (no feature PRs, PR → `main`,
  deleted) vs long-lived version integration (receives feature PRs, merges back to
  `dev` under a human gate). Cutting one is a deliberate act, never a side effect of
  picking up a ticket.
- **Fan-out is a standing obligation.** Hotfix backmerges and governance landings on
  `dev` merge into every live `release/v*` in the same session — *a governance rule
  is only in force on branches that carry it*. Live branches come from a query (with
  refuse cases for in-flight cuts), never a name list. Source-project evidence: 9
  hotfixes in ~2 days put a version branch 11 commits behind with 15 overlapping
  files. `pre-push` now protects `release/v*`; server-side protection on those
  branches must NOT require a PR (fan-out is a direct merge).
- **Base verification generalized.** `merge-base`/`rev-parse` against
  `origin/<resolved-base>` immediately after worktree creation; the PR body states
  the resolved base and the signals it was derived from.
- **Scoped-waiver pattern (mechanism only).** How an owner waives a human merge gate:
  machine-checkable scope, expiry bound to a Release, explicit list of what stays in
  force, and the compensating control — no acceptance criterion may depend on a
  reviewer noticing anything. The downstream instance stays downstream.
- **Two footgun notes.** Annotated tags: `git rev-parse <tag>` returns the tag
  object, not the commit — use `<tag>^{commit}`. Fan-out conflicts: a green suite
  cannot catch a semantic merge bug — ask "would this fail if the fix were reverted?"
- Title convention migrates `[Mn]` (milestone) → `[X.Y.Z]` (version/Release) as the
  primary routing signal; milestones stay tracker metadata and never route git.

**Beyond the source — flow these forward to `mantle-stocks-arbitrage-bots`.** Review
found three defects the upstream text shares: (1) the hotfix backmerge never pushed
`dev`, so the fan-out behind it read an `origin/dev` without the fix and silently
shipped nothing — it now fetches, fast-forwards, merges, and pushes from the primary
clone (`git checkout dev` fails inside the hotfix worktree); (2) the fan-out query
failed *open* when `gh` was unreachable, classifying every in-flight temporary cut as
live; (3) the fan-out merge did not fast-forward the local branch first, so conflicts
got resolved against code that was no longer there. The set of direct-push exceptions
is also stated as all three (fan-out, hotfix backmerge, first push of a fresh cut)
everywhere it appears, including the `pre-push` message an operator actually reads.
Two residual issues are logged in `docs/DEFERRED_ISSUES.md`.

---

## 2026-07-28 — Runtime-neutral agent roles

The template pinned its reviewer to `model: "opus"` inside `code-review/SKILL.md` and
narrated the `/implement` escalation pass as "Opus 5". Both are Claude-Code-only
encodings, so the review path silently had no meaning under Codex, Grok, or any other
runtime — and since `/implement`'s self-merge authorization is *derived from* the review
loop having run, a portability gap could turn into an unreviewed self-merge.

**The invariant, stated once:** review happens in a **different context** than
implementation, on a model **at least as capable**, **preferably cross-vendor**. A model
name is one encoding of that; the invariant is what the workflow rests on.

- **New adapter layer.** `docs/agents/runtime.md` defines four roles — `IMPLEMENTER`,
  `REVIEWER`, `ESCALATOR`, `EXPLORER`. `config/agent-roles.conf` maps roles → commands
  (checked in, non-secret). `scripts/agent-dispatch.sh` dispatches and probes them.
  A model generation turnover is now a one-line edit in the conf, not a skill edit.
- **Subprocess dispatch is canonical; native sub-agents are an optimization.** A
  non-interactive CLI call (`claude -p`, `codex exec`, …) works in every runtime including
  ones with no sub-agent primitive, and gives every runtime an identical prompt/output
  contract. Claude Code's `Agent` tool is a faster path to the same contract, not a
  different design. Skills must not branch three ways on runtime.
- **Cross-vendor review is preferred, not a fallback.** Sonnet→Opus is stronger but shares
  the implementer's blind spots; Codex→Opus has independent failure modes. `--probe` warns
  when both sides of the review are the same vendor.
- **Degraded mode is a hard stop.** If `REVIEWER` probes unusable, the loop did not run:
  finish the work, open the PR, stop at `In Review`, name the missing role. Self-review in
  the implementing context never authorizes a self-merge. Wired into
  `docs/GIT_WORKFLOW.md` § Agent / automation constraints #4.
- **Parallelism was never the point.** Skills that fan out (`/code-review` 2,
  design-it-twice 3+, `/wayfinder` research N) need *context isolation*, not concurrency —
  serial dispatch is an explicit, documented fallback. Reducing the agent count is not.
- **Other runtime assumptions generalized:** `EnterWorktree` demoted to a labelled aside
  behind the universal `merge-base`/`rev-parse` check; Linear access became a fallback
  ladder (MCP → GraphQL + `LINEAR_API_KEY` → stop and say so, never a silent local
  tracker); `/compact` reworded as "your runtime's compaction, if it has one"; bootstrap
  entry no longer says "open in Claude Code"; `AGENTS.md` now lists skills **by file path**
  so a runtime with no skill loader can still be pointed at one.

**Two template bugs found while auditing** (unrelated to portability, both live):

- `implement/SKILL.md` still carried `pm-arbitrage-bot`'s values — a hardcoded `WHI-NNN`
  issue prefix and its trading-specific gated paths ("order-placement, stop-loss,
  circuit-breaker, private-key … once real funds are live") instead of `{{ISSUE_PREFIX}}`
  and `{{HIGH_RISK_PATHS}}`. Its gate also cited a `docs/GIT_WORKFLOW.md` section
  ("Agent 约束 #6") that does not exist in the template, so the high-risk-path gate pointed
  at a dead reference.
- **Root cause:** `SETUP.md`'s placeholder-verification grep passed
  `--exclude-dir=.claude`, so no placeholder inside a vendored skill was ever checked.
  That exclusion is why the values above survived bootstrap. `.claude/` is now in scope,
  with one narrow exception for `HTML-REPORT.md`'s report template marker.

## 2026-07-28 — Release/hotfix lanes + full skill parity

Ported back from `pm-arbitrage-bot`, which hardened its workflow after shipping five
releases and discovering the template's promotion rules were underspecified.

**Git workflow (`docs/GIT_WORKFLOW.md`)** — the substantive change:

- **`main` ≡ production**, defined as always equal to the last deployed tag. The two
  promotion lanes are only distinguishable once that holds.
- **Release-vs-hotfix decision rule**, mechanical: if
  `git log origin/main..origin/dev` holds a single commit you would not ship right now,
  the hotfix lane is mandatory. Cutting a release to rush one fix drags everything on
  `dev` into production.
- **Per-lane merge strategy** — squash into `dev`, **merge commit** into `main`.
  Squashing a release/hotfix creates a commit absent from `dev`'s history, so the tag
  lands on a disconnected branch and `git log <tag>..origin/dev` silently returns
  garbage. `pm-arbitrage-bot` squashed v0.1.1–v0.1.5 before catching this and left five
  orphaned commits on `main`. **This reverses the template's previous squash-only
  policy** — `SETUP.md` no longer passes `--enable-merge-commit=false`.
- **Version axis** — tracker Release ↔ annotated git tag ↔ GitHub Release must agree;
  backfill the Release's `commitSha` as the authoritative version↔code binding.
  Milestones and Releases are orthogonal axes. Hotfixes take a fourth version segment
  (`0.1.5.1`) rather than stealing the next minor.
- **Deploy from tags only, never branches** — a branch deploy leaves nobody able to say
  which tree production is running.
- **Base verification after every worktree creation** (`merge-base` vs `rev-parse`).
  Claude Code's `EnterWorktree` branches from `origin/<default-branch>` = `main`, which
  is wrong for the issue lane and coincidentally right for hotfixes.
- Hotfix lane now requires a patch version bump before tagging and a `main` → `dev`
  merge-back, or the next release re-ships the fixed bug.
- Expanded **Forbidden** list; new **Agent / automation constraints** section (the
  high-risk-path gate explicitly overrides the self-merge pre-authorization).
- New **Branch protection** section: private repos on the Free plan get
  `403 Upgrade to GitHub Pro` for both classic protection and rulesets, so
  `.githooks/pre-push` is the stand-in.

**New: `.githooks/pre-push`** — refuses direct pushes to `main`/`dev`, `ALLOW_DIRECT_PUSH=1`
to override. Must be enabled per clone *and per worktree* (`git config core.hooksPath
.githooks`); `SETUP.md` now does this at bootstrap.

**Skills: 15 → 22 (full parity with `pm-arbitrage-bot`)** — added `diagnosing-bugs`,
`wayfinder`, `prototype`, `grill-with-docs`, `teach`, `writing-great-skills`, `ask-matt`.
Reverses the "trimmed from 22" decision below. Two caveats recorded in `README.md`:
`ask-matt` routes to five skills neither repo vendors, and `skills-lock.json` stores
upstream hashes so it cannot detect the local customizations in `implement`/`code-review`.

**`docs/agents/issue-tracker.md`** — new **Wayfinding operations** section (Linear map
issue, `parent` sub-issues, native blocking, frontier query). Without it `/wayfinder`
silently falls back to a local-markdown tracker; `pm-arbitrage-bot` has this gap too.

**`docs/agents/triage-labels.md`** — documents `hotfix` as the one type label that changes
git behaviour, so agents read labels before choosing a base branch.

## 2026-07-25 — Initial extraction

Extracted and generalized from `pm-arbitrage-bot` (dev branch):

- AGENTS.md as canonical agent guidance, CLAUDE.md symlinked.
- 15 vendored skills (trimmed from 22) + trimmed `skills-lock.json`.
- Docs system: DESIGN.md PRD skeleton as spec of record, GIT_WORKFLOW.md,
  DEFERRED_ISSUES.md, adr/, agents/ four-pack.
- Linear as default tracker; ticket-set publishing rules (native blocked-by relations)
  added to `docs/agents/issue-tracker.md` so `/to-tickets` output lands Linear-native.
- Worktree-per-issue git flow with agent self-squash-merge + high-risk-path
  human-review exceptions (paths defined per-project at setup).
- Minimal Python/uv stack layer (no CI, no shared core/ helpers — deliberate).
- SETUP.md self-destructing bootstrap runbook.

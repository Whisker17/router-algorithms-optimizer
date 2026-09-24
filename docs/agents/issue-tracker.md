# Issue tracker: Linear

Issues and PRDs for this repo live in **Linear**, project **"{{LINEAR_PROJECT}}"**, team
`{{LINEAR_TEAM}}`.

## How to reach Linear

Tracker state moves in lockstep with the PR (below), so this access path is **mandatory,
not a convenience** — a runtime that cannot reach the tracker cannot complete the git
workflow. Take the first rung of this ladder that your runtime actually offers:

1. **Linear MCP tools.** In this template's reference setup they are exposed via the
   `slim-tools` gateway rather than as top-level tools:
   - Discover the tool you need:
     `discover_tools({ query: "linear <capability>", detail: "typescript" })`
     (e.g. `"linear create issue"`, `"linear list issues"`, `"linear comment"`).
   - Call it from `execute_code` using the returned `codeApi.path`. Aggregate/filter in
     the sandbox; return only the final shape.

   If your runtime has the Linear MCP server mounted directly, the tool names below apply
   without the gateway indirection.
2. **Linear GraphQL API** over plain HTTP, with `LINEAR_API_KEY` from `.env`. Every
   operation named below exists as a GraphQL mutation/query (`issueCreate`,
   `issueUpdate`, `commentCreate`, `issues`, `workflowStates`). Use this when your runtime
   has shell/network access but no MCP.
3. **Neither available:** stop and tell the user. Do **not** silently switch to a
   local-markdown tracker or `gh issue` — the tracker is shared state, and a divergent
   local copy is worse than an honest block. If the project genuinely has no Linear
   access, re-run `/setup-matt-pocock-skills` to install a different tracker binding
   (`issue-tracker-github.md` / `-gitlab.md` / `-local.md`) so *all* skills agree on
   where issues live. Any replacement binding must also define
   [§ Release ↔ version binding](#release--version-binding), or declare that it has
   no release entity — git routing reads that section.

The rest of this file names Linear MCP tools (namespace `linear`); under rung 2, read each
as its GraphQL equivalent. Core tools:

- **Create / update an issue**: `linear.save_issue({...})`. When creating, `title` and
  `team` are required; also set `project` to `"{{LINEAR_PROJECT}}"` so it's scoped
  correctly. Omit `id` on create; pass `id` to update. Use `assignee` (a user id, name,
  email, or `"me"`) — not `assigneeId`. Set labels via the `labels` field (see
  `triage-labels.md` for the canonical strings).
- **Read / list issues**: `linear.list_issues({...})`. Filter by `assignee`
  (`"me"` / `"null"`), team, project, state, or label. For a single issue, list with the
  id/filter and read the returned record.
- **Comment**: `linear.save_comment({ issueId, body })` to start a thread;
  `linear.save_comment({ parentId, body })` to reply. Read with
  `linear.list_comments({ issueId })`.
- **Labels**: `linear.list_issue_labels({...})` to find existing labels;
  `linear.create_issue_label({...})` to create a missing one.
- **Workflow states**: `linear.list_issue_statuses({ team })` — Linear tracks progress
  as workflow states in addition to labels. Move an issue by setting its `state` via
  `linear.save_issue`.
- **Close**: set the issue's `state` to a completed/canceled workflow state via
  `linear.save_issue`, optionally after a `linear.save_comment` explaining why.

Prefer Linear's native **workflow states** for lifecycle (open → done) and **labels**
for the triage roles in `triage-labels.md`.

## Issue lifecycle ↔ Git (mandatory)

Implementation always uses a **git worktree** off the **resolved base** (see
`docs/GIT_WORKFLOW.md` — version-scoped work targets `release/v{version}`,
governance targets `dev`, hotfix targets `main`). Agents must keep Linear
state in lockstep with the PR:

| When | Linear `state` |
| --- | --- |
| Claimed / coding in worktree | `In Progress` |
| PR opened against the resolved base (awaiting review) | **`In Review`** |
| PR squash-merged into the resolved base | **`Done`** |
| Abandoned | `Canceled` |

Do not mark `Done` when the PR is only opened. Do not leave an open PR in `In Progress`.
Triage labels (`ready-for-agent`, etc.) stay orthogonal to these states.

## Release ↔ version binding

`docs/GIT_WORKFLOW.md` § Version determination resolves a version-scoped issue's
base branch from two signals: the `[X.Y.Z]` title prefix (primary) cross-checked
against **this tracker's release entity**. That entity is tracker-specific, so it
is defined here.

**For Linear, the release entity is the issue's `Release` field.** Sharp edges,
all of which have already caused a real mis-resolution in the source project
(`mantle-stocks-arbitrage-bots`, WHI-1097):

- **`Release` is not `projectMilestone`.** Both render as a bare `X.Y.Z` in the
  UI and in API payloads, and reading the milestone as the Release let a title
  prefix of `[0.2.0]` sit against a milestone of `0.3.0` undetected for days.
  Milestone is *capability stage*; Release is *when it ships* and what routes
  git (`docs/GIT_WORKFLOW.md` § Version axis).
- **Linear does not enforce non-empty fields.** `issue-template.md`'s metadata
  table asks for a Release, but nothing rejects an issue without one — several
  shipped with the field empty. The enforcement is the agent's **refusal**, not
  the tracker.
- **Read it through the same access ladder as everything else** (above): via MCP,
  `discover_tools({ query: "linear issue release", detail: "typescript" })` then
  the returned `codeApi.path` from `execute_code`; under rung 2, the GraphQL
  `issue` query — select the release/version field explicitly and confirm you did
  not select the milestone by accident. If the tracker is unreachable, the
  cross-check has not been performed: say so and stop, rather than proceeding on
  the title prefix alone as though both signals had agreed.

**Swapping trackers:** an alternative binding (`issue-tracker-github.md` /
`-gitlab.md` / `-local.md`) must define this section too — naming its own release
entity and how to read it — or state explicitly that it has **no** release
entity, which drops the cross-check per `docs/GIT_WORKFLOW.md` § Version
determination and leaves the title prefix standing alone. A missing or ambiguous
prefix still refuses.

## Pull requests as a triage surface

**PRs as a request surface: no.** Code review happens on GitHub; the Linear queue is not
fed from pull requests. `/triage` processes Linear issues only.

## When a skill says "publish to the issue tracker"

Create a Linear issue with `linear.save_issue` (`title` + `team` required, `project` set
to `"{{LINEAR_PROJECT}}"`). Follow the canonical structure in
`docs/agents/issue-template.md` — title convention, body sections, and acceptance
criteria. All issue content is written in English.

## Publishing ticket sets (e.g. from `/to-tickets`)

When a skill produces a **set** of tickets with blocking edges, publish them to Linear
like this:

1. **Order**: create issues in dependency order — blockers first — so each later issue
   can reference real identifiers.
2. **Blocking edges**: wire them as Linear's native **`blocked-by` / `blocks`
   relations** (via `linear.save_issue`'s relations support, or the dedicated relation
   tool if `discover_tools` surfaces one — query `"linear issue relation"`). Also mirror
   each edge as a human-readable `## Blocked By` line in the body per
   `issue-template.md`; the native relation is the source of truth, the body line is the
   mirror.
3. **Scoping**: every issue gets `project: "{{LINEAR_PROJECT}}"` and, if the
   set belongs to a version, the matching **Release** (title carries the `[X.Y.Z]`
   prefix per `issue-template.md`). Milestone is orthogonal — attach it when the
   set is a capability stage, but do not put it in the title.
4. **State + labels**: state `Todo`, triage label `ready-for-agent` (unless the user
   says otherwise) — the tickets are agent-grabbable by construction.
5. **Body**: use the copy-paste skeleton in `issue-template.md`, not the skill's generic
   local-file template.

## When a skill says "fetch the relevant ticket"

Read it with `linear.list_issues` (filter to the id/identifier), then pull discussion
with `linear.list_comments({ issueId })`.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue; its tickets are **sub-issues** of it.
Without this section the skill falls back to a local-markdown tracker — so keep it
accurate.

- **Map**: one Linear issue labelled `wayfinder:map`, holding the Notes /
  Decisions-so-far / Fog body. Create with `linear.save_issue({ title, team,
  project: "{{LINEAR_PROJECT}}", labels: ["wayfinder:map"] })`. Create the label first
  with `linear.create_issue_label` if `linear.list_issue_labels` doesn't have it.
- **Child ticket**: an issue whose **`parent`** is the map — Linear's native sub-issue
  relationship, visible in the map's own UI:
  `linear.save_issue({ title, team, project: "{{LINEAR_PROJECT}}", parent: <map-id>,
  labels: ["wayfinder:<type>"] })`, where `<type>` is `research` / `prototype` /
  `grilling` / `task`. Once claimed, set `assignee` to the driving dev (`"me"` for the
  agent's own session).
- **Blocking**: Linear's **native `blocked-by` / `blocks` relations** — the canonical,
  UI-visible representation, same mechanism as
  [§ Publishing ticket sets](#publishing-ticket-sets-eg-from-to-tickets). Wire edges via
  `linear.save_issue`'s relations support, or the dedicated relation tool if
  `discover_tools({ query: "linear issue relation" })` surfaces one. Wire edges in a
  **second pass**, after the issues exist and have ids. A ticket is unblocked when every
  issue blocking it is closed.
- **Frontier query**: `linear.list_issues` filtered to the map's children
  (`parent: <map-id>`) and open states, then drop any ticket that still has an open
  `blocked-by` relation or a non-empty `assignee`. First in map order wins.
- **Claim**: `linear.save_issue({ id, assignee: "me" })` — the session's first write.
- **Resolve**: post the answer with `linear.save_comment({ issueId, body })`, set the
  ticket's `state` to a completed workflow state via `linear.save_issue`, then append a
  context pointer to the map's Decisions-so-far.

Wayfinder tickets are **decision** tickets, not build slices — they are orthogonal to the
`[X.Y.Z]` version prefix in `issue-template.md`. Don't attach them to a Release
(`docs/GIT_WORKFLOW.md` § Version axis): nothing ships from resolving one.

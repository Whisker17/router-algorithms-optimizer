# {{PROJECT_NAME}} — Design Document / PRD

> This file is the **spec of record** for this project. Every issue, architectural
> decision, and parameter traces back to a section here. It is produced by grilling the
> idea into shape (`/grill-me`) and formalizing the result (`/to-spec`) — do not skip
> straight to code with this document empty.
>
> Sections marked _(fill in)_ contain guidance for what belongs there. Replace the
> guidance as you write; delete sections that genuinely don't apply, but keep §7 and §8
> even when short — they are what stops an agent from re-inventing a rejected design or
> ignoring a known risk.

## 1. Background & Goals

### 1.1 Vision

_(fill in: the long-term product vision, one paragraph. What does this exist to do?)_

### 1.2 v1 Scope

_(fill in: exactly what v1 delivers. Cite prior research / validated data where
parameters are inherited rather than invented — agents are instructed not to re-derive
anything sourced here.)_

### 1.3 Non-goals (explicitly out of scope for v1)

_(fill in: what v1 deliberately does not do. Be as concrete as §1.2 — this list is what
prevents scope creep in tickets.)_

### 1.4 Success criteria

_(fill in: objectively checkable conditions under which v1 is "done and working".)_

## 2. Requirements / Specification

_(fill in: the functional spec. For a product, the feature-level requirements; for a
system, the behavioral rules. Number the subsections — issues will cite them as
`docs/DESIGN.md §2.x`. Any tunable parameter defined here must state where its value
comes from.)_

## 3. Cross-cutting Policies

_(fill in: policies that constrain every feature rather than belonging to one — e.g.
risk limits, security/privacy rules, compliance constraints, performance budgets. Delete
if the project truly has none.)_

## 4. System Architecture

### 4.1 Tech stack

_(fill in: language, runtime, key libraries, storage, deploy target — with a one-line
reason for each non-obvious choice.)_

### 4.2 Module layout

_(fill in: the package/directory structure and each module's single responsibility.
`AGENTS.md` §Architecture mirrors this section — keep them in sync.)_

### 4.3 Key interfaces

_(fill in: the seams that decouple modules — protocols, traits, API contracts. Note
which are load-bearing ("strategy code depends only on this interface") and which are
deliberately NOT abstracted yet — see §7.)_

### 4.4 Core flows

_(fill in: the main runtime sequences, end to end. A numbered walk-through per flow.)_

### 4.5 State & recovery

_(fill in: what state persists, where, and how the system behaves across restarts and
crashes. Delete if stateless.)_

## 5. Data & Observability

_(fill in: what gets logged/journaled, alerting channels and tiers, and how the data
supports post-hoc review. Delete subsections that don't apply.)_

## 6. Milestones

_(fill in: numbered milestones M1..Mn, each with a one-line success criterion. Issue
titles carry the `[X.Y.Z]` version prefix, not a milestone tag — a milestone is tracker
metadata (which capability stage), and never routes git. See
`docs/agents/issue-template.md` § Title convention and `docs/GIT_WORKFLOW.md`
§ Version axis.)_

## 7. Rejected Alternatives

_(fill in: every significant design option that was considered and rejected, with the
reason. This section exists so reviewers and agents don't re-propose them. An agent whose
output contradicts an entry here must flag it explicitly rather than silently override —
see `docs/agents/domain.md`.)_

## 8. Known Risks & Open Questions

_(fill in: what could sink this design, and what remains undecided. Reviewers are
invited to attack this list. Move items out as they get resolved — resolved decisions
made after v1 ships go to `docs/adr/`.)_

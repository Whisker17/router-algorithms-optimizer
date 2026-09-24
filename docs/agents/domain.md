# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring
the codebase.

This repo is **single-context**, and unlike the generic `CONTEXT.md` convention used by
the upstream skills, the spec of record here is a full PRD: **`docs/DESIGN.md`**.

## Before exploring, read these

- **`docs/DESIGN.md`** — background, scope, requirements, architecture, milestones,
  rejected alternatives (§7), and known risks (§8). This is the single source of truth
  for *why* the project is scoped the way it is; do not re-litigate a decision in §7
  without flagging it explicitly (see below).
- **`docs/references/`** — prior research or external material the project's parameters
  and decisions are inherited from. Treat sourced numbers as validated inputs, not
  something to re-derive.
- **`docs/adr/`** (created lazily; may not exist yet) — narrower decisions made *after*
  the initial version ships that don't belong in the PRD itself (e.g. a specific library
  choice, a schema migration). Read any ADR that touches the area you're about to work
  in.

If `docs/adr/` is empty or missing, **proceed silently**. Don't flag its absence; don't
suggest creating it upfront — it gets created the first time a post-v1 decision actually
needs recording.

## File structure

```
/
├── docs/
│   ├── DESIGN.md          # PRD — spec of record
│   ├── references/        # prior research the project inherits from
│   ├── adr/               # narrower post-v1 decisions (created lazily)
│   │   ├── 0001-....md
│   │   └── 0002-....md
│   └── agents/            # this directory — agent operating conventions
└── (source modules per docs/DESIGN.md §4.2)
```

## Use the spec's vocabulary

`docs/DESIGN.md` fixes specific domain terms. When your output names a domain concept
(in an issue title, a refactor proposal, a test name), use the term as defined there.
Don't drift to synonyms.

## Flag design-doc conflicts

If your output contradicts a decision recorded in `docs/DESIGN.md` §7 (rejected
alternatives) or would introduce a parameter not backed by §2 / `docs/references/`,
surface it explicitly rather than silently overriding:

> _Contradicts §7 ("X was rejected because Y") — but worth reopening because…_

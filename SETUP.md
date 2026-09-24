# SETUP.md — Project bootstrap runbook (self-destructing)

> **To the human:** after cloning this template, open the repo in any coding agent
> (Claude Code, Codex, …) and say *"Read SETUP.md and execute it."* That's the whole
> setup.
>
> **To the agent:** this file is an executable runbook. Work through the steps in
> order. Steps marked **[interview]** require asking the user one question at a time and
> waiting for the answer. The final step deletes this file.

## 0. Sanity check

Confirm you are in a fresh clone of the template (not the template repo itself): if
`git remote -v` points at the template repo (`{{TEMPLATE_REPO_URL}}`) **and** the user
has not said they want to edit the template itself, stop and ask.

## 1. Interview — collect the values

Ask **one at a time** ([interview]):

1. **Project name** (kebab-case, used as repo/package name) → `PROJECT_NAME`
2. **One-paragraph description** (what this project is; goes into AGENTS.md "What this
   is") → `PROJECT_DESCRIPTION`
3. **Linear project name** (create it in Linear if it doesn't exist — ask the user
   whether to create it via the Linear MCP tools) → `LINEAR_PROJECT`
4. **Linear team key** (e.g. `WHI`; also derives the issue prefix) → `LINEAR_TEAM`,
   `ISSUE_PREFIX` (the team key, uppercase), `ISSUE_PREFIX_LOWER` (lowercase)
5. **High-risk paths** — the areas of *this* project where an agent-authored PR must
   always stop for human review (e.g. payment flows, auth, production data migrations,
   key handling). Push the user to actually think about this; "none" is an acceptable
   answer only for throwaway projects → `HIGH_RISK_PATHS` (a short comma-separated
   phrase, e.g. `order placement, key handling`)
6. **Stack** — keep the default Python/uv layer, or replace it? If replacing, agree on
   the equivalents (package manager, lint, typecheck, test commands) and rewrite the
   stack-specific files (`pyproject.toml`, `main.py`, `tests/`, the Build/test/run
   section of AGENTS.md) accordingly.
7. **Docker** — keep `Dockerfile` + `docker-compose.yml` skeletons, or delete both?
8. **Template repo URL** (for the feedback-loop pointer; default to the URL in
   `git remote -v` before you change it) → `TEMPLATE_REPO_URL`
9. **Agent roles** — which runtime is implementing, and which agent reviews it. Read
   `docs/agents/runtime.md` § Choosing a reviewer first, then ask. The default in
   `config/agent-roles.conf` assumes Claude Code implementing with `claude -p --model
   opus` reviewing. Two things to settle with the user:
   - Their implementing runtime → `IMPLEMENTER_LABEL`
   - Their reviewer. **Push for cross-vendor** (e.g. Codex implements, `claude` CLI
     reviews) — independent failure modes catch more than a stronger same-vendor model.
     Correct `REVIEWER_CMD` / `ESCALATOR_CMD` / `EXPLORER_CMD` accordingly, and verify
     each CLI's real flags with `--help` rather than trusting the commented examples.

## 2. Replace placeholders

Every placeholder marker in the repo must be gone when you finish. Replace across all
tracked files:

- `{{PROJECT_NAME}}`, `{{PROJECT_DESCRIPTION}}`, `{{LINEAR_PROJECT}}`,
  `{{LINEAR_TEAM}}`, `{{ISSUE_PREFIX}}`, `{{ISSUE_PREFIX_LOWER}}`,
  `{{HIGH_RISK_PATHS}}`, `{{TEMPLATE_REPO_URL}}`

Then verify (SETUP.md itself is exempt — it gets deleted in step 6):

```bash
grep -rn '{{' --include='*.md' --include='*.toml' --include='*.yml' --include='*.py' \
  --exclude=SETUP.md --exclude-dir=.git . \
  | grep -v 'improve-codebase-architecture/HTML-REPORT.md' \
  | grep -v 'orchestrate/implementer-prompt.md'
```

This must return nothing. **`.claude/` is deliberately in scope** — vendored skills
carry `{{ISSUE_PREFIX}}` / `{{HIGH_RISK_PATHS}}` markers too, and excluding that
directory is how unported values survive bootstrap. Two legitimate exceptions:
`HTML-REPORT.md`'s `{{repo name}}`, and `orchestrate/implementer-prompt.md`'s
`{{PLACEHOLDER}}` launch-template slots — neither is a bootstrap marker.

Also:

- `pyproject.toml`: confirm `name` / `description` landed correctly
- `AGENTS.md` §Status: set to "freshly bootstrapped; DESIGN.md not yet written"
- Delete `CHANGELOG.md` — it belongs to the template repo, not to projects
- Rewrite `README.md` for the new project (the template's README describes the
  template, not your project)

## 3. Wire up git + GitHub

1. Point the remote at the new repo (create it first if needed:
   `gh repo create <owner>/<PROJECT_NAME> --private`):
   `git remote set-url origin <new-repo-url>`
2. Create the integration branch: `git checkout -b dev` (keep `main` as the release
   line). Do **not** cut a `release/v*` branch yet: until the first production tag
   exists, `docs/GIT_WORKFLOW.md` § Resolving the base branch resolves the
   version-scoped row to `dev`. The first long-lived `release/v*` integration
   branch is cut deliberately, later, when work on the next version begins while a
   deployed version is live.
3. Set merge policy on the GitHub repo. **Both** squash and merge-commit must be
   allowed — the lane decides which you use (`docs/GIT_WORKFLOW.md` § Merge strategy):
   squash for issues → `dev` or → a long-lived `release/v*`, merge commit for
   `release/*` / `hotfix/*` → `main` and for a finished version-integration branch
   merging back into `dev`.
   ```bash
   gh repo edit --enable-squash-merge --enable-merge-commit \
     --enable-rebase-merge=false --delete-branch-on-merge
   ```
4. Enable the local push guard, which refuses direct pushes to `main`, `dev`, and
   any `release/v*` branch:
   ```bash
   git config core.hooksPath .githooks
   ```
   Tell the user this must be re-run **in every worktree**, not just the primary clone.
5. Try to enable server-side branch protection on `main` and `dev` (require a PR, block
   force-push and deletion). On a **private repo on the Free plan this returns
   `403 Upgrade to GitHub Pro`** for both classic protection and rulesets — if so, say so
   plainly, note that the remote is genuinely unprotected, and move on. The `pre-push`
   hook from step 4 is the only guard until the plan allows it.

   When live `release/v*` integration branches later exist, protect them with
   **force-push and deletion blocks only — never require a PR**. Fan-out is a
   direct merge of `dev` from the primary clone, and a require-PR ruleset has no
   `ALLOW_DIRECT_PUSH` equivalent (`docs/GIT_WORKFLOW.md` § Branch protection).
   Require-a-PR on `dev` also blocks the hotfix backmerge, which is a direct push —
   keep an owner bypass, or plan to land that backmerge as a PR off `origin/main`
   (same section).
6. Initial commit on `main`, then push both branches:
   `git push -u origin main dev`.

## 4. Verify the skeleton

```bash
uv sync
uv run pytest        # the smoke test must pass
uv run ruff check .
```

(Adjust if the stack was replaced in step 1.6.)

Then verify the agent role mapping from step 1.9 resolves:

```bash
scripts/agent-dispatch.sh --probe
```

Every role must report `ok`. A `UNUSABLE` role means `/implement`'s review loop cannot run
in this environment — fix the mapping now, or tell the user plainly that PRs will have to
stop at `In Review` for human review until it is fixed (`docs/agents/runtime.md`
§ Degraded mode).

## 5. Hand off to the design phase

Tell the user the project is bootstrapped, and that the next step is producing the spec
of record: run `/grill-me <the project idea>` and then `/to-spec` to fill
`docs/DESIGN.md`. After the spec lands, `/to-tickets` breaks it into Linear issues.

## 6. Self-destruct

```bash
git rm SETUP.md
git commit -m "chore: complete template bootstrap, remove SETUP.md"
git push
```

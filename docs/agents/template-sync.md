# code-template v0.2 process-layer synchronization

Source: [Whisker17/code-template](https://github.com/Whisker17/code-template), main commit
`0c468b16eb9b1f87269cd83b399b57b23688bbd9` (template v0.2.0, 2026-09-25).
Project baseline: `92cfcba8e4636c3eb881e5b3936d945977224f79` (business acceptance PR #27).
Sync tasks: WHI-1499 (governance/process) and WHI-1500 (version-scoped lock/test artifacts).
This is a process migration, not the project's 0.2.0 Jupiter work or a product release.

## Adopted from the pinned template

- Eight project-local core skills: grill-me, to-spec, to-tickets, implement, orchestrate,
  code-review, handoff, ponytail. The retained skill files/metadata are copied from the
  pinned source. Removed the sixteen former non-core project-local skill directories and
  obsolete shrink/trap helper files; globally installed skills are untouched.
- Short canonical AGENTS entry with on-demand links. No mandatory TDD, ladder report or
  complete trap-registry injection. Required behavior checks and minimal implementation
  remain mandatory.
- ORCHESTRATOR / IMPLEMENTER / REVIEWER with exact runtime/model and semantic effort
  translation, unsupported-configuration errors and no silent fallback. The shell adapter
  is the pinned upstream implementation; only project role values are configured locally.
- Complexity, Reason and Expected scope in new issue bodies. Orchestrator owns issue
  transitions and serial integration; workers stop at PR/handoff under orchestration.
- One independent PR review for governance/standalone lanes. Release-level review is
  bounded to at most three complete candidate reviews and two automatic fix batches,
  with durable baseline/candidate/finding state in one Linear orchestration document.
  This is not the removed per-issue three-review requirement.
- New tracker operations, PR evidence format and explicit production/hotfix human gates.

## Project adaptations and preserved boundaries

1. **Business is protected.** Existing `main.py`, snapshot/pools/routing/benchmark/report
   modules, all existing tests/fixtures, tools, runtime profiles, calibrated cost artifacts,
   dependency/version files, Docker files, `.env.example`, product DESIGN/ISSUE_PLAN/
   RELEASE_PLAN, ADRs, references and accepted-debt records are unchanged. The existing
   `/data/` ignore rule stays; the template's more generic `.gitignore` is not copied.
   The new dispatcher test is tooling coverage, not a rewrite of any business test.
2. **No bootstrap rerun.** SETUP.md, the template's empty product spec, placeholder
   entrypoint/tests and package/deployment files are not imported. Existing project/team/
   pipeline bindings and historical Release exceptions remain. No tag, Release status,
   repository visibility or global client setting is changed.
3. **Instruction compatibility.** The installed Claude Code is 2.1.280, below the
   template's 2.1.281 native-AGENTS compatibility baseline. Retain the existing
   `CLAUDE.md -> AGENTS.md` symlink; it is an alias to the single canonical file, not a
   competing instruction source. Removing it requires a separately verified client
   upgrade/loading check. Pi 0.87.1 supports the configured CLI model/effort flags.
4. **Real role values.** The template's empty configuration is populated from the local
   Pi registry; exact IDs live only in `config/agent-roles.conf`. ORCHESTRATOR and REVIEWER
   support high, IMPLEMENTER supports medium/high. The synchronization was written in the
   existing parent session; writing the config did not pretend to switch that session's
   model. Future dispatches use the configured contract.
5. **Stable business references.** `docs/GIT_WORKFLOW.md` retains a `Release review gate`
   compatibility anchor for existing product-document links. It points to the adopted
   release orchestration contract and preserves the benchmark's validation obligations.
6. **Legacy issue migration.** Completed issues are not reopened or rewritten just to
   add new execution metadata. Reconstruct their real scope from committed PRs/evidence.
   New or rescheduled unfinished work needs Execution metadata before dispatch. WHI-1425's
   completed, owner-approved governance Release association remains a historical exception;
   new governance work has no Release. Existing business issue requirements are unchanged.
7. **Strict test typing.** The upstream fake-runtime tests are imported by WHI-1500 with
   annotations needed by this project's existing strict mypy. No project type/lint rule is
   relaxed. `skills-lock.json` is synchronized separately to respect the carve-out boundary.

## Verification evidence

- Before synchronization: `uv run pytest -q` → **1347 passed** in 45.63 seconds.
- The canonical AGENTS entry shrank from **306 to 169 lines**; project-local skill
  directories shrank from **24 to 8**. These are measured file counts, not token-savings claims.
- A baseline inventory captured SHA-256 for **401 existing protected files**. Inventory
  hash: `28ce4ccf16013c3226d7016bba81fa1083cb1decb8cd86f664a45290ab8f5663`.
  Validation compares every protected file after each sync stage; it does not infer
  noninterference from a passing smoke test alone.
- The copied adapter passed all **31 upstream fake-runtime regression cases** in an
  isolated harness with the current script/config. The tests check argv, file/stdin
  transport, unsupported roles/efforts, no model/session override and error propagation;
  they invoke no real models.
- `scripts/agent-dispatch.sh --probe` resolved all three configured roles and explicitly
  identified itself as a static check only.
- Bounded real invocations succeeded with exit 0 and exact `DISPATCH-OK` output for
  ORCHESTRATOR/high, IMPLEMENTER/medium, IMPLEMENTER/high and REVIEWER/high. They requested
  no tool use or state changes. No fallback model/effort was attempted.
- Final candidate checks, protected-file comparison and independent reviewer verdicts
  are recorded in the corresponding PRs, tied to their exact heads; this source record
  does not pre-claim those later results.

Local validation artifacts were written under `/tmp/router-template-sync-evidence/`.
The durable source pin, adaptations and evidence summary are this record and the PRs;
the temporary directory is not the only recovery source.

## Safe update and rollback

Compare the pinned source against local adaptations before a future refresh. Do not
blindly replace config/, pyproject/uv.lock, tests/fixtures or the business PRD. Keep
model IDs in the role config; skill bodies describe roles/efforts, not a provider catalog.
The upstream skill lock records original vendored provenance; it is not a hash manifest
for the template's customized text, and first-party orchestrate/ponytail are not entries.

Rollback uses normal scoped revert PRs. Preserve existing Linear issues, release evidence,
model authentication and business data; do not reset shared branches or delete history.

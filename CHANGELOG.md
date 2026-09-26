# Template Feedback Changelog

Improvements to the template layer discovered in this project. When work here surfaces
an improvement that belongs to the template repo (see "Template feedback loop" in `AGENTS.md`),
record it here so it can be ported back to `Whisker17/code-template`.

## 2026-09-26 — Adopt code-template v0.2 process tooling

- Source pin: `0c468b16eb9b1f87269cd83b399b57b23688bbd9`, tracked by WHI-1499/WHI-1500.
  Adopt eight core skills, explicit role/model/effort dispatch, complexity/scope metadata,
  one-pass PR review and bounded release orchestration; remove legacy mandatory TDD,
  rung-report and full-trap injection contracts. This supersedes earlier process notes below.
- Preserve all business modules, runtime profiles, dependency versions and frozen evidence.
  Keep project bindings and a short compatibility anchor for existing release-review links.
- Template feedback: do not copy blank role values over a configured project; preserve an
  instruction alias until the installed client actually supports native AGENTS loading;
  distinguish completed legacy issue metadata from new dispatch requirements; type upstream
  tooling tests when the downstream project checks tests strictly. These are local adapters,
  not a reason to overwrite the downstream business scaffold.
- Detailed adoption/verification record: `docs/agents/template-sync.md`.

## Earlier discoveries in router-algorithms-optimizer (historical)

- Owner-directed fast iteration (WHI-1471): ordinary issue PRs merge on acceptance,
  test/lint/type, scope and mergeability evidence, with optional targeted review. Move
  systematic independent Standards/Spec and integration review to the release gate;
  use finding-driven follow-up rather than a fixed round count or escalation ladder.
  Reviewer availability blocks required release review, not every development issue.
  Keep existing human production-promotion and version-integration merge-back gates.
  Port this as a coordinated rule/skill/prompt update, not a one-file wording change.

- Distinguish Linear Release assignment from git execution readiness (a planned Release in
  the tracker does not create or authorize git release branches before bootstrap).
- Handle the 255-character limit on Linear Release descriptions by linking to the detailed
  local release plan.
- Validate Linear issue-mention targets before comparing normalized markdown without counting
  automatic related links as blocking edges.

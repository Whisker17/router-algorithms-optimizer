# Template Feedback Changelog

Improvements to the template layer discovered in this project. When work here surfaces
an improvement that belongs to the template repo (see "Template feedback loop" in `AGENTS.md`),
record it here so it can be ported back to `Whisker17/code-template`.

## Discovered in router-algorithms-optimizer

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

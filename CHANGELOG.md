# Template Feedback Changelog

Improvements to the template layer discovered in this project. When work here surfaces
an improvement that belongs to the template repo (see "Template feedback loop" in `AGENTS.md`),
record it here so it can be ported back to `Whisker17/code-template`.

## Discovered in router-algorithms-optimizer

- Distinguish Linear Release assignment from git execution readiness (a planned Release in
  the tracker does not create or authorize git release branches before bootstrap).
- Handle the 255-character limit on Linear Release descriptions by linking to the detailed
  local release plan.
- Validate Linear issue-mention targets before comparing normalized markdown without counting
  automatic related links as blocking edges.

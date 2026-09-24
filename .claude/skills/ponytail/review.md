# Shrink checklist

Over-engineering only. Correctness, security, and performance are out of scope here — `/code-review` owns those. Apply this to the diff, delete what it flags, then stop. Do not count this as a review round.

A test at a pre-agreed `/tdd` seam is not bloat. Do not delete it.

## Tags

One line per finding: `<file>:L<line>: <tag> <what>. <replacement>.`

- `delete:` dead code, unused flexibility, speculative feature. Replacement: nothing.
- `stdlib:` hand-rolled thing the standard library ships. Name the function.
- `native:` dependency or code doing what the platform already does. Name the feature.
- `yagni:` abstraction with one implementation, config nobody sets, layer with one caller.
- `shrink:` same logic, fewer lines. Show the shorter form.

Nothing to cut: `Lean already.` — then proceed to `/code-review`. Do not treat that sentence as a review pass.

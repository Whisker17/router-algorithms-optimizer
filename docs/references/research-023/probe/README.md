# research-023 probe (validation only)

Pinned, reviewed copy of the parent-session probe v4 (`R023-C/1` §7). Never imported by runtime code.
Hashes: `SHA256SUMS` (scripts, frozen reference list, analysis outputs, fixture log) and
`results/SHA256SUMS` (arm results, hashed **uncompressed**; they are stored gzip-compressed under
`results/`). Every hash equals the one the round-4 reviewer pinned.

Scripts are archived as `*.py.txt` (repository precedent: `metis-challenge-results/scripts/`), so they
are outside Ruff/mypy and byte-identical to the reviewed files; the hashes are of the content.
`fixtures4.log` is committed with `git add -f` (the repository ignores `*.log`).

`fixtures4.py` builds its own synthetic bundles and reads no corpus. `polish4.py`, `analyze4.py` and
`c100_summary.py` read `<repo>/data/results/<run>/` (gitignored; it exists only in the primary clone).
In a worktree pass the primary clone's absolute path as `$REPO`. Do not symlink `data/`.

```bash
# from the repository (or worktree) root
REPO=/path/to/primary/clone   # holds data/corpus/mantle-5src-101082044 and data/results
(cd docs/references/research-023/probe && shasum -a 256 -c SHA256SUMS)
W=$(mktemp -d); for f in docs/references/research-023/probe/*.py.txt; do cp "$f" "$W/$(basename "${f%.txt}")"; done
cp docs/references/research-023/probe/reference-runs.frozen.json "$W/"
(cd "$W" && PYTHONPATH="$OLDPWD" uv run --project "$OLDPWD" python fixtures4.py)   # 27/27, exit 0
# analysis over the pinned arm results
for f in docs/references/research-023/probe/results/*.gz; do gunzip -c "$f" > "$W/$(basename "${f%.gz}")"; done
(cd "$W" && shasum -a 256 -c "$OLDPWD/docs/references/research-023/probe/results/SHA256SUMS")
python3 "$W/analyze4.py" "$REPO" | diff - docs/references/research-023/probe/analysis.txt && echo analysis-identical
python3 "$W/c100_summary.py" "$REPO" | diff - docs/references/research-023/probe/c100_summary.txt && echo c100-identical
# one arm rerun (long; not needed for the identity checks above)
CAP_RESIDUAL=300000 TOL_BPS=1 TIME_LIMIT=900 ROUNDS=2 PYTHONPATH=. uv run python "$W/polish4.py" \
  "$REPO/data/corpus/mantle-5src-101082044/bundle_tuning" "$REPO/data/results/20260925T153506524692Z-09f63aea" \
  incremental_graph polish brent "$W/ig_b2.check.json"
```

Arms: `ig_{g,b}{1,2}` (E1 on IG c50), `ps_{g,b}2` (E1 on path_split), `c100_b2`, `ig_act` (E2 full),
`ig_actpf` / `c100_actpf` (E2 PF). Base runs are the 0.1.0 calibration records in `data/results/`
(gitignored; listed in `reference-runs.frozen.json`). Source comments in the scripts still say v3 in
places and the `pf_split` docstring shows an older formula; the implemented formula and the contract
are `w_orig·(1−t) ⊕ t` (review R4-03). The files are kept byte-identical to the reviewed hashes.

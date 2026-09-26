# WHI-1510 / L08 launch record (gitignored session-side evidence)

- Parent decision (orchestrator, 2026-09-26): GO for ONE registered campaign at clean
  2fda208a6eea1ba21bfc420a02d02ca74e2a2aa1; waives ONLY the optional 3.0 launch headroom,
  prospectively before collection. No measurement threshold, coverage or adoption rule
  change; the owner's 5.174 exception (experiment 4e49878d) is NOT extended.
- Readiness window 19:58:22-20:00:23 UTC, 1-min load: 3.85, 4.24, 3.84, 4.29, 4.07
  (5-min 5.77->5.25, 15-min 9.03->8.40); no rustc/cargo/node --test.
- Instantaneous safety check 2026-09-26T20:05:40Z: loadavg 4.63 5.58 7.58; no rustc/cargo/
  node --test/pytest/benchmark process.
- HEAD 2fda208a6eea1ba21bfc420a02d02ca74e2a2aa1, tree clean.
- config/latency/l08.yaml sha256 e7add86add573033f04c6790f705cb2951712ffc272a4498d03f01251101beaa
- L01 v1 sha256 961fb52208c7baac3d0ffe492cef89818498543f1f00f56217d2f99113e27f1b;
  L01-SB v1 sha256 4059506da09280cdb0e8d4a94ac6ecef1c786113016e8eee430bef38089db43b
- Parent bundle (read-only): /Users/whisker/Work/src/tools/work/mantle-router/router-algorithms-optimizer/data/corpus/mantle-5src-101082044/bundle
- Host: Apple M2 Pro, 10 logical CPUs, shared, not exclusive.
- Command: uv run python -m benchmark.latency session --arms config/latency/l08.yaml --bundle <parent bundle> --out data/latency-l08

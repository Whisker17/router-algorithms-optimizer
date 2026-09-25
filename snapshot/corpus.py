"""Frozen five-source corpus (WHI-1436 / I11; docs/DESIGN.md §2.2, §2.4, §2.7, §2.12).

One immutable real bundle fixes the liquidity universe, the request distribution and the
price context before any algorithm is compared. This module owns:

- `CorpusConfig` -- the validated `config/corpus.yaml` definition (common block, sources,
  absolute Dune window, selection rule, seed, envelope rule, the single exclusion rule).
- The **Dune SQL** of the three bounded queries (`activity_sql`, `strata_sql`,
  `prices_sql`), generated deterministically from the config (and, for strata/prices,
  from the activity export), so a saved export can be re-derived from its SQL text.
- **Export ingestion** (`ingest_export`): a Dune result JSON -> canonical JSONL whose
  SHA-256 is the export identity recorded in provenance.
- **Selection** (`select_universe`, `plan_cases`): the token universe and activity pairs
  from the activity export; low/medium/large raw-amount strata per direction from
  historical swap legs (`origin = leg_derived`: the corpus never claims reconstructed user
  orders); a seeded, deterministic tuning/report split.
- **Envelope + per-source prepare configs** (`write_prepare_configs`): every universe pair
  on every source, with the per-token envelope as its reference cases and the one
  exclusion rule, so all five collectors run freshly at the one common block.
- **Assembly** (`assemble`): the five per-source bundles (same block or refusal), the
  no-direct-pool and liquidity/rounding boundary cases derived from the frozen state, the
  envelope gate, full-source and SOR-compatible cohort descriptors, `prices.json` and
  `corpus.json`, published atomically through `snapshot.bundle.write_bundle`.
- **Offline validation** (`validate_corpus_bundle`) used by `main.py validate`.

Nothing here is read at benchmark time except through the published bundle; measured runs
stay offline.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_CORPUS_CONFIG = Path("config/corpus.yaml")
STRATA = ("low", "medium", "large")
ORIGIN_LEG_DERIVED = "leg_derived"
ORIGIN_STATE_DERIVED = "state_derived_boundary"
_HEX = set("0123456789abcdef")


class CorpusError(ValueError):
    """The corpus definition, an export or an assembled corpus fails validation."""


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def _require_keys(obj: Any, required: set[str], optional: set[str], where: str) -> None:
    if not isinstance(obj, dict):
        raise CorpusError(f"{where}: expected a mapping, got {type(obj).__name__}")
    missing = required - obj.keys()
    if missing:
        raise CorpusError(f"{where}: missing required key(s) {sorted(missing)}")
    unknown = obj.keys() - required - optional
    if unknown:
        raise CorpusError(f"{where}: unknown key(s) {sorted(unknown)}")


def _int(value: Any, lo: int, hi: int | None, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CorpusError(f"{where}: expected int, got {value!r}")
    if value < lo or (hi is not None and value > hi):
        raise CorpusError(f"{where}: {value} outside [{lo}, {hi}]")
    return value


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorpusError(f"{where}: expected a non-empty string")
    return value


def _address(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
        raise CorpusError(f"{where}: not a 20-byte 0x-address: {value!r}")
    lowered = value.lower()
    if not all(c in _HEX for c in lowered[2:]):
        raise CorpusError(f"{where}: not valid hex: {value!r}")
    return lowered


def _utc_seconds(text: str, where: str) -> int:
    """`YYYY-MM-DD HH:MM:SS` (UTC) -> unix seconds."""
    from datetime import UTC, datetime

    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError as exc:
        raise CorpusError(f"{where}: expected 'YYYY-MM-DD HH:MM:SS' UTC, got {text!r}") from exc
    return int(parsed.timestamp())


@dataclass(frozen=True)
class SourceSpec:
    source_key: str
    collector: str
    dune_project: str
    dune_version: str


@dataclass(frozen=True)
class CorpusConfig:
    corpus_id: str
    chain_id: int
    block_number: int
    block_hash: str
    block_timestamp: int
    sources: tuple[SourceSpec, ...]
    window_start: str
    window_end: str
    block_months: tuple[str, ...]
    dune_performance: str
    seed: str
    top_n_pairs: int
    min_pair_txs: int
    strata: dict[str, tuple[float, float]]
    min_legs: int
    samples_per_stratum: int
    no_direct_max_pairs: int
    no_direct_samples_per_stratum: int
    tuning_per_cell: int
    envelope_multiplier: int
    exclusion_rule_id: str
    max_bitmap_words_per_direction: int
    max_bins_per_direction: int
    prices_table: str
    max_price_staleness_seconds: int
    source_path: str
    sha256: str

    @property
    def source_keys(self) -> tuple[str, ...]:
        return tuple(s.source_key for s in self.sources)

    def exclusion_rule(self) -> dict[str, Any]:
        return {
            "id": self.exclusion_rule_id,
            "text": (
                "a discovered pool is excluded iff proving the corpus envelope in one "
                "direction (the envelope swap fits the collected state, or real exhaustion "
                "is proven) needs more than the declared read bound; the bound lets every "
                "concentrated pool be walked to MIN_TICK/MAX_TICK, so thinness alone never "
                "excludes a pool; a source left without an admitted pool blocks publication"
            ),
            "max_bitmap_words_per_direction": self.max_bitmap_words_per_direction,
            "max_bins_per_direction": self.max_bins_per_direction,
        }


def parse_corpus_config(raw: Any, *, source_path: str, sha256: str) -> CorpusConfig:
    _require_keys(
        raw,
        {
            "schema_version",
            "corpus_id",
            "snapshot",
            "sources",
            "dune",
            "selection",
            "envelope",
            "exclusion_rule",
            "prices",
        },
        set(),
        "<root>",
    )
    if raw["schema_version"] != SUPPORTED_SCHEMA_VERSION:
        raise CorpusError(f"schema_version: unsupported {raw['schema_version']!r}")
    snap = raw["snapshot"]
    _require_keys(
        snap, {"chain_id", "block_number", "block_hash", "block_timestamp"}, set(), "snapshot"
    )
    block_hash = snap["block_hash"]
    if not isinstance(block_hash, str) or len(block_hash) != 66 or not block_hash.startswith("0x"):
        raise CorpusError("snapshot.block_hash: expected a 32-byte 0x-hash")
    sources: list[SourceSpec] = []
    for i, entry in enumerate(raw["sources"] if isinstance(raw["sources"], list) else []):
        w = f"sources[{i}]"
        _require_keys(entry, {"source_key", "collector", "dune_project", "dune_version"}, set(), w)
        sources.append(
            SourceSpec(
                source_key=_str(entry["source_key"], f"{w}.source_key"),
                collector=_str(entry["collector"], f"{w}.collector"),
                dune_project=_str(entry["dune_project"], f"{w}.dune_project"),
                dune_version=_str(entry["dune_version"], f"{w}.dune_version"),
            )
        )
    if len(sources) != 5 or len({s.source_key for s in sources}) != 5:
        raise CorpusError("sources: the corpus requires exactly the five distinct core sources")
    dune = raw["dune"]
    _require_keys(
        dune, {"window_start", "window_end", "block_months", "performance"}, set(), "dune"
    )
    start = _utc_seconds(_str(dune["window_start"], "dune.window_start"), "dune.window_start")
    end = _utc_seconds(_str(dune["window_end"], "dune.window_end"), "dune.window_end")
    block_ts = _int(snap["block_timestamp"], 0, None, "snapshot.block_timestamp")
    if not start < end <= block_ts:
        raise CorpusError(
            "dune: the window must satisfy window_start < window_end <= snapshot.block_timestamp "
            "(the historical window ends no later than the snapshot)"
        )
    months = dune["block_months"]
    if not isinstance(months, list) or not months:
        raise CorpusError("dune.block_months: expected a non-empty list of partition dates")
    sel = raw["selection"]
    _require_keys(
        sel,
        {
            "seed",
            "activity_pairs",
            "strata",
            "min_legs",
            "samples_per_stratum",
            "no_direct_pairs",
            "tuning_per_cell",
        },
        set(),
        "selection",
    )
    ap = sel["activity_pairs"]
    _require_keys(ap, {"top_n", "min_distinct_txs"}, set(), "selection.activity_pairs")
    strata_obj = sel["strata"]
    _require_keys(strata_obj, set(STRATA), set(), "selection.strata")
    strata: dict[str, tuple[float, float]] = {}
    previous = 0.0
    for name in STRATA:
        band = strata_obj[name]
        if (
            not isinstance(band, list)
            or len(band) != 2
            or not all(isinstance(q, int | float) and not isinstance(q, bool) for q in band)
        ):
            raise CorpusError(f"selection.strata.{name}: expected [lo, hi] quantiles")
        lo, hi = float(band[0]), float(band[1])
        if not (0.0 <= lo < hi <= 1.0) or lo < previous:
            raise CorpusError(f"selection.strata.{name}: bands must be ordered and within [0, 1]")
        strata[name] = (lo, hi)
        previous = hi
    nd = sel["no_direct_pairs"]
    _require_keys(nd, {"max_pairs", "samples_per_stratum"}, set(), "selection.no_direct_pairs")
    samples = _int(sel["samples_per_stratum"], 1, 64, "selection.samples_per_stratum")
    tuning = _int(sel["tuning_per_cell"], 0, samples - 1, "selection.tuning_per_cell")
    env = raw["envelope"]
    _require_keys(env, {"multiplier"}, set(), "envelope")
    rule = raw["exclusion_rule"]
    _require_keys(
        rule,
        {"id", "max_bitmap_words_per_direction", "max_bins_per_direction"},
        set(),
        "exclusion_rule",
    )
    prices = raw["prices"]
    _require_keys(prices, {"table", "max_staleness_seconds"}, set(), "prices")
    return CorpusConfig(
        corpus_id=_str(raw["corpus_id"], "corpus_id"),
        chain_id=_int(snap["chain_id"], 1, None, "snapshot.chain_id"),
        block_number=_int(snap["block_number"], 0, None, "snapshot.block_number"),
        block_hash=block_hash.lower(),
        block_timestamp=block_ts,
        sources=tuple(sources),
        window_start=dune["window_start"],
        window_end=dune["window_end"],
        block_months=tuple(_str(m, "dune.block_months[]") for m in months),
        dune_performance=_str(dune["performance"], "dune.performance"),
        seed=_str(sel["seed"], "selection.seed"),
        top_n_pairs=_int(ap["top_n"], 1, 200, "selection.activity_pairs.top_n"),
        min_pair_txs=_int(
            ap["min_distinct_txs"], 1, None, "selection.activity_pairs.min_distinct_txs"
        ),
        strata=strata,
        min_legs=_int(sel["min_legs"], 1, None, "selection.min_legs"),
        samples_per_stratum=samples,
        no_direct_max_pairs=_int(nd["max_pairs"], 0, 100, "selection.no_direct_pairs.max_pairs"),
        no_direct_samples_per_stratum=_int(
            nd["samples_per_stratum"], 1, 64, "selection.no_direct_pairs.samples_per_stratum"
        ),
        tuning_per_cell=tuning,
        envelope_multiplier=_int(env["multiplier"], 1, 100, "envelope.multiplier"),
        exclusion_rule_id=_str(rule["id"], "exclusion_rule.id"),
        max_bitmap_words_per_direction=_int(
            rule["max_bitmap_words_per_direction"], 1, 1 << 15, "exclusion_rule.max_bitmap_words"
        ),
        max_bins_per_direction=_int(
            rule["max_bins_per_direction"], 1, 100_000, "exclusion_rule.max_bins_per_direction"
        ),
        prices_table=_str(prices["table"], "prices.table"),
        max_price_staleness_seconds=_int(
            prices["max_staleness_seconds"], 1, 7 * 86400, "prices.max_staleness_seconds"
        ),
        source_path=source_path,
        sha256=sha256,
    )


def load_corpus_config(path: str | Path = DEFAULT_CORPUS_CONFIG) -> CorpusConfig:
    data = Path(path).read_bytes()
    try:
        raw = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise CorpusError(f"{path}: invalid YAML: {exc}") from exc
    try:
        return parse_corpus_config(
            raw, source_path=str(path), sha256=hashlib.sha256(data).hexdigest()
        )
    except CorpusError as exc:
        raise CorpusError(f"{path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Dune SQL (bounded: chain, partition and absolute-window filters on every scan)
# ---------------------------------------------------------------------------


def _legs_cte(config: CorpusConfig) -> str:
    source_case = "\n".join(
        f"      WHEN project = '{s.dune_project}' AND version = '{s.dune_version}' "
        f"THEN '{s.source_key}'"
        for s in config.sources
    )
    source_filter = "\n     OR ".join(
        f"(project = '{s.dune_project}' AND version = '{s.dune_version}')" for s in config.sources
    )
    months = ", ".join(f"DATE '{m}'" for m in config.block_months)
    return f"""legs AS (
  SELECT
    CASE
{source_case}
    END AS source_key,
    token_sold_address AS token_in,
    token_bought_address AS token_out,
    token_sold_amount_raw AS amount_in_raw,
    amount_usd,
    tx_hash,
    evt_index,
    block_time,
    block_number
  FROM dex.trades
  WHERE blockchain = 'mantle'
    AND block_month IN ({months})
    AND block_time >= TIMESTAMP '{config.window_start}'
    AND block_time < TIMESTAMP '{config.window_end}'
    AND ({source_filter})
    AND token_sold_address <> token_bought_address
    AND token_sold_amount_raw > UINT256 '0'
)"""


def activity_sql(config: CorpusConfig) -> str:
    """Q1: unordered-pair and per-token activity over the five sources in the window.
    Identity is the token address; symbols are not selected at all."""
    per_source = ",\n".join(
        f"  COUNT_IF(source_key = '{s.source_key}') AS legs_{s.source_key}" for s in config.sources
    )
    return f"""-- WHI-1436 corpus Q1 (activity) for {config.corpus_id}
-- window [{config.window_start}, {config.window_end}) UTC; end == snapshot block timestamp
WITH {_legs_cte(config)},
pair_rows AS (
  SELECT
    'pair' AS kind,
    LEAST(token_in, token_out) AS token_a,
    GREATEST(token_in, token_out) AS token_b,
    COUNT(*) AS legs,
    COUNT(DISTINCT tx_hash) AS distinct_txs,
    SUM(amount_usd) AS priced_usd,
    COUNT_IF(amount_usd IS NULL) AS unpriced_legs,
{per_source}
  FROM legs
  GROUP BY 2, 3
  HAVING COUNT(DISTINCT tx_hash) >= 5
),
token_legs AS (
  SELECT token_in AS token, tx_hash, amount_usd, source_key FROM legs
  UNION ALL
  SELECT token_out AS token, tx_hash, amount_usd, source_key FROM legs
),
token_rows AS (
  SELECT
    'token' AS kind,
    token AS token_a,
    CAST(NULL AS VARBINARY) AS token_b,
    COUNT(*) AS legs,
    COUNT(DISTINCT tx_hash) AS distinct_txs,
    SUM(amount_usd) AS priced_usd,
    COUNT_IF(amount_usd IS NULL) AS unpriced_legs,
{per_source}
  FROM token_legs
  GROUP BY 2
  HAVING COUNT(DISTINCT tx_hash) >= 50
)
SELECT * FROM pair_rows
UNION ALL
SELECT * FROM token_rows
ORDER BY kind, distinct_txs DESC, token_a, token_b
"""


def _units_values(units: Sequence[tuple[str, str, str | None]]) -> str:
    rows = []
    for kind, token_in, token_out in units:
        out = "CAST(NULL AS VARBINARY)" if token_out is None else token_out
        rows.append(f"    ('{kind}', {token_in}, {out})")
    return ",\n".join(rows)


def strata_sql(config: CorpusConfig, units: Sequence[tuple[str, str, str | None]]) -> str:
    """Q2: exact rank-based raw-amount strata and a seeded, deterministic sample of
    historical legs for every sampling unit: `('pair', token_in, token_out)` -- one
    direction of an activity pair -- or `('token', token_in, NULL)` -- every leg selling a
    universe token (the amount source of no-direct-pool pairs). Within a unit, legs are
    ordered by raw input amount (ties: tx hash, log index); a leg of rank r (0-based) of n
    is in stratum [lo, hi) iff lo*n <= r < hi*n. At most one leg per transaction enters a
    stratum's sample (transaction identity is deduplicated); the sample order is
    xxhash64(seed | tx_hash | evt_index)."""
    bands = "\n".join(
        f"      WHEN amount_rank - 1 >= {lo!r} * unit_legs "
        f"AND amount_rank - 1 < {hi!r} * unit_legs THEN '{name}'"
        for name, (lo, hi) in config.strata.items()
    )
    return f"""-- WHI-1436 corpus Q2 (strata samples) for {config.corpus_id}
-- window [{config.window_start}, {config.window_end}) UTC; seed '{config.seed}'
WITH {_legs_cte(config)},
units (unit_kind, unit_in, unit_out) AS (
  VALUES
{_units_values(units)}
),
unit_legs AS (
  SELECT u.unit_kind, u.unit_in, u.unit_out, l.*
  FROM legs l
  JOIN units u
    ON l.token_in = u.unit_in
   AND (u.unit_out IS NULL OR l.token_out = u.unit_out)
),
ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY unit_kind, unit_in, unit_out ORDER BY amount_in_raw, tx_hash, evt_index
    ) AS amount_rank,
    COUNT(*) OVER (PARTITION BY unit_kind, unit_in, unit_out) AS unit_legs
  FROM unit_legs
),
stratified AS (
  SELECT
    *,
    CASE
{bands}
    END AS stratum
  FROM ranked
),
banded AS (
  SELECT
    *,
    COUNT(*) OVER (PARTITION BY unit_kind, unit_in, unit_out, stratum) AS stratum_legs,
    MIN(amount_in_raw) OVER (PARTITION BY unit_kind, unit_in, unit_out, stratum) AS stratum_min_raw,
    MAX(amount_in_raw) OVER (PARTITION BY unit_kind, unit_in, unit_out, stratum) AS stratum_max_raw,
    ROW_NUMBER() OVER (
      PARTITION BY unit_kind, unit_in, unit_out, stratum, tx_hash ORDER BY evt_index
    ) AS tx_leg
  FROM stratified
  WHERE stratum IS NOT NULL
),
sampled AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY unit_kind, unit_in, unit_out, stratum
      ORDER BY xxhash64(to_utf8(concat(
        '{config.seed}', '|', lower(to_hex(tx_hash)), '|', CAST(evt_index AS VARCHAR)
      ))), tx_hash, evt_index
    ) AS sample_rank
  FROM banded
  WHERE tx_leg = 1
)
SELECT
  unit_kind,
  unit_in,
  unit_out,
  stratum,
  sample_rank,
  unit_legs,
  stratum_legs,
  CAST(stratum_min_raw AS VARCHAR) AS stratum_min_raw,
  CAST(stratum_max_raw AS VARCHAR) AS stratum_max_raw,
  amount_rank,
  source_key,
  token_out AS leg_token_out,
  CAST(amount_in_raw AS VARCHAR) AS amount_in_raw,
  amount_usd,
  tx_hash,
  evt_index,
  block_number,
  block_time
FROM sampled
WHERE sample_rank <= CASE unit_kind
    WHEN 'pair' THEN {config.samples_per_stratum}
    ELSE {config.no_direct_samples_per_stratum}
  END
ORDER BY unit_kind, unit_in, unit_out, stratum, sample_rank
"""


def prices_sql(config: CorpusConfig, tokens: Sequence[str]) -> str:
    """Q3: for every universe token, the latest `prices.minute` row whose whole minute
    bucket [t, t+60s) ends at or before the snapshot timestamp, within the staleness
    bound. A token without such a row is missing (never zero)."""
    lo = config.block_timestamp - config.max_price_staleness_seconds
    hi = config.block_timestamp - 60
    values = ",\n".join(f"    ({t})" for t in tokens)
    bound = (
        f"-- snapshot - {config.max_price_staleness_seconds}s <= t and t + 60s <= "
        f"snapshot = {config.block_timestamp}"
    )
    return f"""-- WHI-1436 corpus Q3 (frozen price context) for {config.corpus_id}
-- latest {config.prices_table} minute bucket [t, t+60s) with
{bound}
WITH universe (token) AS (
  VALUES
{values}
),
candidates AS (
  SELECT
    p.contract_address AS token,
    p.timestamp,
    p.price,
    p.decimals,
    p.symbol,
    p.source,
    ROW_NUMBER() OVER (PARTITION BY p.contract_address ORDER BY p.timestamp DESC) AS rn
  FROM {config.prices_table} p
  JOIN universe u ON p.contract_address = u.token
  WHERE p.blockchain = 'mantle'
    AND p.timestamp >= from_unixtime({lo})
    AND p.timestamp <= from_unixtime({hi})
)
SELECT
  u.token,
  to_unixtime(c.timestamp) AS price_timestamp,
  c.price,
  c.decimals,
  c.symbol,
  c.source
FROM universe u
LEFT JOIN candidates c ON c.token = u.token AND c.rn = 1
ORDER BY u.token
"""


# ---------------------------------------------------------------------------
# exports: a Dune result -> canonical JSONL (identity = its SHA-256)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DuneExport:
    name: str
    query_id: int
    execution_id: str
    sql_sha256: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    sha256: str


def read_dune_result(path: str | Path) -> dict[str, Any]:
    """A saved `createAndExecuteQuery`/`getExecutionResults` JSON document (the first
    JSON line of a saved MCP tool output, or a plain JSON file)."""
    text = Path(path).read_text()
    first = text.split("\nstructuredContent:", 1)[0].strip()
    try:
        doc = json.loads(first)
    except json.JSONDecodeError as exc:
        raise CorpusError(f"{path}: not a Dune result JSON document: {exc}") from exc
    if not isinstance(doc, dict):
        raise CorpusError(f"{path}: expected a JSON object")
    return doc


def canonical_export_text(
    name: str,
    query_id: int,
    execution_id: str,
    sql_text: str,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
) -> str:
    header = {
        "export": name,
        "query_id": query_id,
        "execution_id": execution_id,
        "sql_sha256": hashlib.sha256(sql_text.encode()).hexdigest(),
        "columns": list(columns),
        "row_count": len(rows),
    }
    lines = [json.dumps(header, sort_keys=True)]
    for row in rows:
        if set(row) != set(columns):
            raise CorpusError(f"export {name}: row keys {sorted(row)} != columns {list(columns)}")
        lines.append(json.dumps(dict(row), sort_keys=True))
    return "\n".join(lines) + "\n"


def ingest_export(
    name: str, raw_path: str | Path, sql_path: str | Path, out_path: str | Path
) -> DuneExport:
    """Check that the Dune-side SQL is byte-identical to the generated SQL file, that the
    execution completed with every row present, and write the canonical export."""
    doc = read_dune_result(raw_path)
    sql_text = Path(sql_path).read_text()
    query = doc.get("query") or {}
    stored_sql = query.get("query")
    if stored_sql is not None and stored_sql != sql_text:
        raise CorpusError(f"{raw_path}: the Dune query text differs from {sql_path}")
    preview = doc.get("result_preview", doc)
    state = preview.get("state")
    if state != "COMPLETED":
        raise CorpusError(f"{raw_path}: execution state {state!r}, expected COMPLETED")
    meta = preview.get("resultMetadata") or {}
    columns = tuple(c["name"] for c in meta.get("columns", []))
    rows = preview.get("data", {}).get("rows", [])
    total = meta.get("totalRowCount")
    if total is not None and total != len(rows):
        raise CorpusError(f"{raw_path}: {len(rows)} row(s) saved of {total}; export is partial")
    query_id = int(query.get("query_id") or doc.get("query_id") or 0)
    execution_id = str(preview.get("executionId") or doc.get("execution", {}).get("execution_id"))
    text = canonical_export_text(name, query_id, execution_id, sql_text, columns, rows)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(text)
    return load_export(out_path)


def load_export(path: str | Path) -> DuneExport:
    data = Path(path).read_bytes()
    lines = data.decode().splitlines()
    if not lines:
        raise CorpusError(f"{path}: empty export")
    header = json.loads(lines[0])
    _require_keys(
        header,
        {"export", "query_id", "execution_id", "sql_sha256", "columns", "row_count"},
        set(),
        f"{path}:1",
    )
    rows = tuple(json.loads(line) for line in lines[1:])
    if len(rows) != header["row_count"]:
        raise CorpusError(f"{path}: {len(rows)} row(s), header says {header['row_count']}")
    return DuneExport(
        name=header["export"],
        query_id=header["query_id"],
        execution_id=header["execution_id"],
        sql_sha256=header["sql_sha256"],
        columns=tuple(header["columns"]),
        rows=rows,
        sha256=hashlib.sha256(data).hexdigest(),
    )


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Universe:
    tokens: tuple[str, ...]  # sorted lowercase addresses
    activity_pairs: tuple[tuple[str, str], ...]  # rank order; each (token_a, token_b) sorted
    pair_txs: dict[tuple[str, str], int]
    token_txs: dict[str, int]

    @property
    def all_pairs(self) -> tuple[tuple[str, str], ...]:
        t = self.tokens
        return tuple((t[i], t[j]) for i in range(len(t)) for j in range(i + 1, len(t)))


def select_universe(config: CorpusConfig, activity: DuneExport) -> Universe:
    """Activity pairs = the `top_n` unordered pairs by distinct transactions (ties by
    address) with at least `min_distinct_txs`; the token universe is exactly their
    tokens. Identity is the lowercase address."""
    pair_rows = []
    token_txs: dict[str, int] = {}
    for row in activity.rows:
        if row["kind"] == "pair":
            a, b = _address(row["token_a"], "token_a"), _address(row["token_b"], "token_b")
            pair_rows.append(((a, b), int(row["distinct_txs"])))
        elif row["kind"] == "token":
            token_txs[_address(row["token_a"], "token_a")] = int(row["distinct_txs"])
        else:
            raise CorpusError(f"activity export: unknown row kind {row['kind']!r}")
    ranked = sorted(pair_rows, key=lambda r: (-r[1], r[0]))
    chosen = [p for p, txs in ranked if txs >= config.min_pair_txs][: config.top_n_pairs]
    tokens = tuple(sorted({t for p in chosen for t in p}))
    return Universe(
        tokens=tokens,
        activity_pairs=tuple(chosen),
        pair_txs={p: txs for p, txs in pair_rows},
        token_txs=token_txs,
    )


def sampling_units(universe: Universe) -> list[tuple[str, str, str | None]]:
    units: list[tuple[str, str, str | None]] = []
    for a, b in universe.activity_pairs:
        units.append(("pair", a, b))
        units.append(("pair", b, a))
    units.extend(("token", t, None) for t in universe.tokens)
    return units


# ---------------------------------------------------------------------------
# planning: frozen prices, empirical cases, envelope, per-source prepare configs
# ---------------------------------------------------------------------------

PLAN_SCHEMA = "corpus-plan/1"
CL_COLLECTORS = {"agni": "agni_v3", "fusionx": "fusionx_v3", "uniswap_v3": "uniswap_v3"}
RPC_SETTINGS = {
    "max_batch_size": 5,
    "max_attempts": 8,
    "base_delay_seconds": 1.0,
    "max_delay_seconds": 16.0,
    "timeout_seconds": 60.0,
    "use_multicall3": True,
}


def _short(address: str) -> str:
    return address[2:8]


def _cell_hash(seed: str, case_id: str) -> str:
    return hashlib.sha256(f"{seed}|{case_id}".encode()).hexdigest()


def assign_splits(config: CorpusConfig, cells: Mapping[Any, Sequence[str]]) -> dict[str, str]:
    """Within every cell, the `tuning_per_cell` case ids with the smallest seeded hash are
    `tuning`; the rest are `report`. Deterministic and disjoint by construction."""
    split: dict[str, str] = {}
    for ids in cells.values():
        ordered = sorted(ids, key=lambda cid: (_cell_hash(config.seed, cid), cid))
        for i, cid in enumerate(ordered):
            split[cid] = "tuning" if i < config.tuning_per_cell else "report"
    return split


def token_labels(prices: Mapping[str, Any]) -> dict[str, str]:
    """address -> a unique, informational label (symbol, else address prefix)."""
    labels: dict[str, str] = {}
    used: set[str] = set()
    for address in sorted(prices):
        symbol = prices[address].get("symbol") or ""
        base = "".join(ch for ch in symbol.upper() if ch.isalnum()) or f"T{_short(address)}"
        label = base if base not in used else f"{base}_{_short(address)}"
        used.add(label)
        labels[address] = label
    return labels


def _leg_record(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tx_hash": row["tx_hash"],
        "evt_index": row["evt_index"],
        "block_number": row["block_number"],
        "block_time": row["block_time"],
        "source_key": row["source_key"],
        "leg_token_out": row["leg_token_out"],
        "dune_amount_usd": row["amount_usd"],
    }


def _selection_record(row: Mapping[str, Any], config: CorpusConfig) -> dict[str, Any]:
    return {
        "unit": row["unit_kind"],
        "stratum_band": list(config.strata[row["stratum"]]),
        "unit_legs": row["unit_legs"],
        "stratum_legs": row["stratum_legs"],
        "stratum_min_raw": row["stratum_min_raw"],
        "stratum_max_raw": row["stratum_max_raw"],
        "amount_rank": row["amount_rank"],
        "sample_rank": row["sample_rank"],
        "leg": _leg_record(row),
    }


def plan_corpus(
    config: CorpusConfig,
    activity: DuneExport,
    strata: DuneExport,
    prices_export: DuneExport,
    catalog_fee_tiers: Mapping[str, Sequence[int]],
) -> dict[str, Any]:
    """Everything decidable before collection: universe, frozen prices, empirical
    (leg-derived) cases with strata and splits, token-level samples for the no-direct-pool
    pairs, the per-token envelope, and the five generated prepare configs."""
    from snapshot.prices import build_price_context, parse_price_context

    universe = select_universe(config, activity)
    units = sampling_units(universe)
    expected_units = {(k, a, b) for k, a, b in units}
    block = {
        "chain_id": config.chain_id,
        "number": config.block_number,
        "hash": config.block_hash,
        "timestamp": config.block_timestamp,
    }
    price_rows = [dict(r) for r in prices_export.rows]
    if sorted(r["token"] for r in price_rows) != list(universe.tokens):
        raise CorpusError("prices export: rows do not cover exactly the token universe")
    price_doc = build_price_context(
        price_rows,
        block=block,
        source={
            "provider": "dune",
            "table": config.prices_table,
            "query_id": prices_export.query_id,
            "execution_id": prices_export.execution_id,
            "export_sha256": prices_export.sha256,
            "selection": (
                "latest minute bucket [t, t+60s) with t+60s <= snapshot timestamp and "
                "t >= snapshot - max_staleness_seconds"
            ),
            "max_staleness_seconds": config.max_price_staleness_seconds,
        },
        native_via="0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8",
    )
    prices = parse_price_context(price_doc)
    missing = [t for t, p in prices.tokens.items() if p.status != "ok"]
    if missing:
        raise CorpusError(
            f"tokens {missing} have no frozen price: the envelope cannot be sized in their "
            "units, so they cannot be in the universe"
        )

    seen_units: set[tuple[str, str, str | None]] = set()
    empirical: list[dict[str, Any]] = []
    token_samples: dict[str, list[dict[str, Any]]] = {t: [] for t in universe.tokens}
    for row in strata.rows:
        kind = row["unit_kind"]
        unit = (kind, str(row["unit_in"]).lower(), row["unit_out"] and str(row["unit_out"]).lower())
        if unit not in expected_units:
            raise CorpusError(f"strata export: unexpected unit {unit}")
        seen_units.add(unit)
        if row["unit_legs"] < config.min_legs:
            continue
        amount = int(row["amount_in_raw"])
        if kind == "pair":
            token_in, token_out = unit[1], unit[2]
            assert token_out is not None
            case_id = (
                f"emp-{_short(token_in)}-{_short(token_out)}-{row['stratum']}-{row['sample_rank']}"
            )
            empirical.append(
                {
                    "case_id": case_id,
                    "token_in": token_in,
                    "token_out": token_out,
                    "amount_in": str(amount),
                    "stratum": row["stratum"],
                    "origin": ORIGIN_LEG_DERIVED,
                    "selection": _selection_record(row, config),
                }
            )
        else:
            token_samples[unit[1]].append(
                {
                    "stratum": row["stratum"],
                    "sample_rank": row["sample_rank"],
                    "amount_in": str(amount),
                    "selection": _selection_record(row, config),
                }
            )
    if seen_units != expected_units:
        raise CorpusError(f"strata export: missing unit(s) {sorted(expected_units - seen_units)}")

    notionals = [prices.usd_value(c["token_in"], int(c["amount_in"])) for c in empirical]
    notionals += [
        prices.usd_value(t, int(s["amount_in"])) for t, ss in token_samples.items() for s in ss
    ]
    max_notional = max(notionals)
    envelope_usd = max_notional * config.envelope_multiplier
    envelope = {t: str(prices.raw_amount_for_usd(t, envelope_usd)) for t in universe.tokens}

    cells: dict[tuple[str, str, str], list[str]] = {}
    for c in empirical:
        cells.setdefault((c["token_in"], c["token_out"], c["stratum"]), []).append(c["case_id"])
    split = assign_splits(config, cells)
    for c in empirical:
        c["split"] = split[c["case_id"]]
        c["notional_usd"] = str(prices.usd_value(c["token_in"], int(c["amount_in"])))

    labels = token_labels(price_doc["tokens"])
    configs = prepare_configs(config, universe, labels, price_doc, envelope, catalog_fee_tiers)
    exports = {
        e.name: {
            "query_id": e.query_id,
            "execution_id": e.execution_id,
            "export_sha256": e.sha256,
            "sql_sha256": e.sql_sha256,
            "rows": len(e.rows),
            "dune_url": f"https://dune.com/queries/{e.query_id}",
        }
        for e in (activity, strata, prices_export)
    }
    return {
        "schema": PLAN_SCHEMA,
        "corpus_id": config.corpus_id,
        "config": {"path": repo_path(config.source_path), "sha256": config.sha256},
        "block": block,
        "exports": exports,
        "dune_window": {
            "start": config.window_start,
            "end": config.window_end,
            "end_exclusive": True,
            "block_months": list(config.block_months),
        },
        "universe": {
            "rule": (
                f"top {config.top_n_pairs} unordered token pairs (by address) by distinct "
                f"transactions over the five sources in the window, >= {config.min_pair_txs} "
                "each; the token universe is exactly their tokens"
            ),
            "tokens": list(universe.tokens),
            "labels": labels,
            "activity_pairs": [
                {"token_a": a, "token_b": b, "distinct_txs": universe.pair_txs[(a, b)]}
                for a, b in universe.activity_pairs
            ],
            "token_distinct_txs": {t: universe.token_txs.get(t, 0) for t in universe.tokens},
        },
        "prices": price_doc,
        "empirical_cases": empirical,
        "token_samples": token_samples,
        "envelope": {
            "rule": (
                f"E(token) = ceil({config.envelope_multiplier} x max case notional USD / "
                "price(token)) raw units, both directions of every admitted pool"
            ),
            "max_case_notional_usd": str(max_notional),
            "envelope_usd": str(envelope_usd),
            "amount_in": envelope,
        },
        "prepare_configs": configs,
    }


def _envelope_cases(
    universe: Universe, envelope: Mapping[str, str], labels: Mapping[str, str]
) -> list[dict[str, str]]:
    cases = []
    for a, b in universe.all_pairs:
        for tin, tout in ((a, b), (b, a)):
            cases.append(
                {
                    "case_id": f"envelope-{_short(tin)}-{_short(tout)}",
                    "token_in": labels[tin],
                    "token_out": labels[tout],
                    "amount_in": envelope[tin],
                }
            )
    return cases


def prepare_configs(
    config: CorpusConfig,
    universe: Universe,
    labels: Mapping[str, str],
    price_doc: Mapping[str, Any],
    envelope: Mapping[str, str],
    catalog_fee_tiers: Mapping[str, Sequence[int]],
) -> dict[str, dict[str, Any]]:
    """The five generated prepare configs (collector name -> YAML-able document): every
    universe pair on every source, the envelope as reference cases, the one exclusion
    rule, Multicall3 read batching."""
    pairs = [[labels[a], labels[b]] for a, b in universe.all_pairs]
    cases = _envelope_cases(universe, envelope, labels)
    declared = {
        labels[t]: {"address": t, "decimals": price_doc["tokens"][t]["decimals"]}
        for t in universe.tokens
    }
    out: dict[str, dict[str, Any]] = {}
    for spec in config.sources:
        doc: dict[str, Any] = {
            "schema_version": 1,
            "source": spec.source_key,
            "bundle_id_prefix": f"{config.corpus_id}-{spec.source_key}",
        }
        if spec.collector in CL_COLLECTORS:
            tiers = sorted(catalog_fee_tiers[spec.source_key])
            doc["tokens"] = {labels[t]: t for t in universe.tokens}
            doc["pairs"] = [{"tokens": list(p), "fee_tiers": list(tiers)} for p in pairs]
            doc["cases"] = [dict(c) for c in cases]
            doc["collection"] = {
                "initial_margin_words": 1,
                "margin_words": 1,
                "max_words_per_direction": config.max_bitmap_words_per_direction,
                "walk_chunk_words": 64,
                "exclusion_rule": config.exclusion_rule_id,
            }
        elif spec.collector == "moe_classic":
            doc["tokens"] = declared
            doc["pairs"] = [{"tokens": list(p)} for p in pairs]
            doc["cases"] = [dict(c) for c in cases]
        elif spec.collector == "moe_lb":
            doc["tokens"] = declared
            doc["pairs"] = [{"tokens": list(p)} for p in pairs]
            doc["cases"] = [dict(c) for c in cases]
            doc["collection"] = {
                "walk_chunk_bins": 16,
                "margin_bins": 4,
                "max_bins_per_direction": config.max_bins_per_direction,
                "exclusion_rule": config.exclusion_rule_id,
            }
        else:
            raise CorpusError(f"no prepare-config generator for collector {spec.collector!r}")
        doc["rpc"] = dict(RPC_SETTINGS)
        if spec.collector in CL_COLLECTORS or spec.collector == "moe_lb":
            doc["tokens"] = dict(doc["tokens"])
        out[spec.collector] = doc
    return out


def repo_path(path: str) -> str:
    """`path` relative to the repository root when it lies inside it (stable across
    checkouts/worktrees), else as given."""
    root = Path(__file__).resolve().parents[1]
    try:
        return str(Path(path).resolve().relative_to(root))
    except ValueError:
        return path


def prepare_config_text(config: CorpusConfig, collector: str, doc: Mapping[str, Any]) -> str:
    header = (
        f"# GENERATED by `main.py corpus plan` ({config.corpus_id}, WHI-1436); do not edit.\n"
        f"# Derived from {repo_path(config.source_path)} (sha256 {config.sha256[:16]}...) and the\n"
        "# Dune exports it names: every universe pair on this source, the corpus envelope as\n"
        f"# reference cases, and the single exclusion rule. Collector: {collector}.\n"
    )
    body = yaml.safe_dump(dict(doc), sort_keys=False, width=100, default_flow_style=None)
    return header + body


# ---------------------------------------------------------------------------
# assembly: five fresh per-source bundles at the one block -> one corpus bundle
# ---------------------------------------------------------------------------

CORPUS_DOC_SCHEMA = "corpus/1"
STRATUM_BOUNDARY = "boundary"
SPLITS = ("tuning", "report")
ACCEPTED_ENVELOPE_STATUSES = frozenset(
    {"ok", "insufficient_liquidity", "insufficient_output_amount"}
)
_SEARCH_OK = "ok"


def _pool_source(pool: Any) -> str | None:
    return getattr(pool, "source_key", None)


def _reachable(pools: Iterable[Any], token_in: str, token_out: str) -> bool:
    adjacency: dict[str, set[str]] = {}
    for p in pools:
        adjacency.setdefault(p.token0, set()).add(p.token1)
        adjacency.setdefault(p.token1, set()).add(p.token0)
    seen = {token_in}
    frontier = [token_in]
    while frontier:
        nxt = []
        for t in frontier:
            for u in adjacency.get(t, ()):
                if u not in seen:
                    seen.add(u)
                    nxt.append(u)
        frontier = nxt
    return token_out in seen


def _quote(pool: Any, token_in: str, amount: int) -> Any:
    from pools.quote import quote_exact_in

    return quote_exact_in(pool, token_in, amount)


def _smallest_positive_output(pool: Any, token_in: str, hi: int) -> int | None:
    """Smallest raw input in [1, hi] this pool fills fully with a positive output
    (outputs are monotone in the input), or None."""
    top = _quote(pool, token_in, hi)
    if top.status.value != _SEARCH_OK or top.amount_out <= 0:
        return None
    lo, best = 1, hi
    while lo < best:
        mid = (lo + best) // 2
        r = _quote(pool, token_in, mid)
        if r.status.value == _SEARCH_OK and r.amount_out > 0:
            best = mid
        else:
            lo = mid + 1
    return best


def _fill_capacity(pool: Any, token_in: str, hi: int) -> int | None:
    """Largest raw input in [1, hi] this pool fills fully, if the pool is proven
    exhausted below `hi` (`insufficient_liquidity` at hi, `ok` at 1); else None."""
    if _quote(pool, token_in, hi).status.value != "insufficient_liquidity":
        return None
    if _quote(pool, token_in, 1).status.value != _SEARCH_OK:
        return None
    lo, top = 1, hi - 1
    while lo < top:
        mid = (lo + top + 1) // 2
        if _quote(pool, token_in, mid).status.value == _SEARCH_OK:
            lo = mid
        else:
            top = mid - 1
    return lo


def boundary_cases(
    direct: Sequence[Any], token_in: str, token_out: str, envelope_in: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """State-derived rounding and liquidity boundary cases for one direction with
    direct pools (origin `state_derived_boundary`, stratum `boundary`):

    - `dust`: 1 raw unit;
    - `round_below` / `round_at`: one below / exactly the smallest input some direct pool
      turns into a positive output (skipped when that is 1 or 2 raw units: dust covers it);
    - `liq_at` / `liq_above`: exactly / one above the largest full-fill input of the
      deepest direct pool whose liquidity is proven exhausted within the envelope
      (skipped when no direct pool exhausts within it).
    """
    prefix = f"bnd-{_short(token_in)}-{_short(token_out)}"
    record: dict[str, Any] = {"token_in": token_in, "token_out": token_out}
    out: list[dict[str, Any]] = [
        {"case_id": f"{prefix}-dust", "amount_in": 1, "kind": "dust", "pool_id": None}
    ]
    thresholds = [
        (a, p.pool_id)
        for p in direct
        if (a := _smallest_positive_output(p, token_in, envelope_in)) is not None
    ]
    if thresholds:
        a_min, pool_id = min(thresholds)
        record["smallest_positive_output_input"] = {"amount_in": str(a_min), "pool_id": pool_id}
        if a_min > 2:
            out.append(
                {
                    "case_id": f"{prefix}-round_below",
                    "amount_in": a_min - 1,
                    "kind": "round_below",
                    "pool_id": pool_id,
                }
            )
            out.append(
                {
                    "case_id": f"{prefix}-round_at",
                    "amount_in": a_min,
                    "kind": "round_at",
                    "pool_id": pool_id,
                }
            )
    else:
        record["smallest_positive_output_input"] = None
    capacities = [
        (c, p.pool_id)
        for p in direct
        if (c := _fill_capacity(p, token_in, envelope_in)) is not None
    ]
    if capacities:
        cap, pool_id = max(capacities)
        record["deepest_exhausted_pool"] = {"fill_capacity": str(cap), "pool_id": pool_id}
        out.append(
            {"case_id": f"{prefix}-liq_at", "amount_in": cap, "kind": "liq_at", "pool_id": pool_id}
        )
        out.append(
            {
                "case_id": f"{prefix}-liq_above",
                "amount_in": cap + 1,
                "kind": "liq_above",
                "pool_id": pool_id,
            }
        )
    else:
        record["deepest_exhausted_pool"] = None
    return out, record


def _check_block(bundle: Any, config: CorpusConfig, where: str) -> None:
    b = bundle.block
    if (b.chain_id, b.number, b.hash.lower(), b.timestamp) != (
        config.chain_id,
        config.block_number,
        config.block_hash,
        config.block_timestamp,
    ):
        raise CorpusError(
            f"{where}: bundle block ({b.chain_id}, {b.number}, {b.hash}, {b.timestamp}) is not "
            f"the corpus block ({config.chain_id}, {config.block_number}, {config.block_hash}, "
            f"{config.block_timestamp}); one corpus never mixes blocks"
        )


def assemble(
    config: CorpusConfig,
    plan: Mapping[str, Any],
    source_dirs: Mapping[str, Path],
    output_dir: Path,
    *,
    catalog: Any,
    prepare_dir: Path,
) -> Any:
    """Validate the five fresh per-source bundles and publish the corpus bundle."""
    from snapshot.bundle import PROVENANCE_FILE, load_bundle, sha256_file, write_bundle
    from snapshot.models import BlockRef, Case
    from snapshot.prices import parse_price_context

    if plan.get("schema") != PLAN_SCHEMA or plan.get("corpus_id") != config.corpus_id:
        raise CorpusError("plan: not a plan for this corpus")
    if plan["config"]["sha256"] != config.sha256:
        raise CorpusError("plan: made from a different corpus config")
    if set(source_dirs) != {s.collector for s in config.sources}:
        wanted = sorted(s.collector for s in config.sources)
        raise CorpusError(f"assemble needs exactly the five source bundles {wanted}")
    prices = parse_price_context(
        plan["prices"], block=(config.block_number, config.block_hash, config.block_timestamp)
    )
    universe_tokens = list(plan["universe"]["tokens"])
    envelope = {t: int(a) for t, a in plan["envelope"]["amount_in"].items()}

    sor = catalog.sor_protocols()
    pools: dict[str, Any] = {}
    source_records: dict[str, Any] = {}
    exclusions: list[dict[str, Any]] = []
    verified_decimals: dict[str, int] = {}
    for spec in config.sources:
        path = source_dirs[spec.collector]
        bundle = load_bundle(path)
        _check_block(bundle, config, str(path))
        provenance = json.loads((Path(path) / PROVENANCE_FILE).read_text())
        if provenance.get("source_key") != spec.source_key:
            raise CorpusError(f"{path}: provenance is for {provenance.get('source_key')!r}")
        cfg_path = prepare_dir / f"{spec.collector}.yaml"
        expected_text = prepare_config_text(
            config, spec.collector, plan["prepare_configs"][spec.collector]
        )
        if cfg_path.read_text() != expected_text:
            raise CorpusError(f"{cfg_path}: differs from the configs the plan generates")
        if provenance["prepare_config"]["sha256"] != sha256_file(cfg_path):
            raise CorpusError(
                f"{path}: collected with a prepare config other than {cfg_path} (sha256 "
                f"{provenance['prepare_config']['sha256']})"
            )
        if not bundle.pools:
            raise CorpusError(f"{spec.source_key}: no admitted pool; the source blocks publication")
        for pid, pool in bundle.pools.items():
            if _pool_source(pool) != spec.source_key:
                raise CorpusError(f"{path}: pool {pid} is not a {spec.source_key} pool")
            if pid in pools:
                raise CorpusError(f"pool {pid} appears in two source bundles")
            pools[pid] = pool
        rule = (provenance.get("exclusions") or {}).get("rule")
        if rule not in (None, config.exclusion_rule_id):
            raise CorpusError(f"{path}: exclusions under an undeclared rule {rule!r}")
        for rec in (provenance.get("exclusions") or {}).get("pools", []):
            exclusions.append({"source_key": spec.source_key, **rec})
        for token, info in (provenance.get("tokens") or {}).items():
            verified_decimals[token] = int(info["decimals"])
        source_records[spec.source_key] = {
            "collector": spec.collector,
            "bundle_id": bundle.bundle_id,
            "bundle_hash": bundle.bundle_hash,
            "pools": len(bundle.pools),
            "sor_protocol": sor.get(spec.source_key),
            "provenance": provenance,
        }
    for t in universe_tokens:
        if verified_decimals.get(t) != prices.tokens[t].decimals:
            raise CorpusError(
                f"token {t}: decimals {prices.tokens[t].decimals} not verified on-chain by a "
                f"collector at the block (saw {verified_decimals.get(t)})"
            )

    # -- cases -------------------------------------------------------------------
    metadata: dict[str, dict[str, Any]] = {}
    cases: list[Any] = []

    def add(case_id: str, token_in: str, token_out: str, amount: int, meta: dict[str, Any]) -> None:
        if case_id in metadata:
            raise CorpusError(f"duplicate case id {case_id}")
        if amount > envelope[token_in]:
            raise CorpusError(f"case {case_id}: {amount} exceeds the {token_in} envelope")
        cases.append(
            Case(case_id=case_id, token_in=token_in, token_out=token_out, amount_in=amount)
        )
        metadata[case_id] = meta

    for c in plan["empirical_cases"]:
        add(
            c["case_id"],
            c["token_in"],
            c["token_out"],
            int(c["amount_in"]),
            {
                "origin": c["origin"],
                "stratum": c["stratum"],
                "split": c["split"],
                "group": "activity_pair",
                "selection": c["selection"],
            },
        )

    def direct_pools(a: str, b: str) -> list[Any]:
        return [p for p in pools.values() if {p.token0, p.token1} == {a, b}]

    token_txs = plan["universe"]["token_distinct_txs"]
    no_direct = [
        (a, b)
        for i, a in enumerate(universe_tokens)
        for b in universe_tokens[i + 1 :]
        if not direct_pools(a, b)
    ]
    no_direct.sort(key=lambda p: (-min(token_txs[p[0]], token_txs[p[1]]), p))
    chosen_no_direct = no_direct[: config.no_direct_max_pairs]
    if not chosen_no_direct:
        raise CorpusError("the universe has no pair without a direct pool at the block")
    nd_cells: dict[tuple[str, str, str], list[str]] = {}
    nd_rows: list[tuple[str, str, str, dict[str, Any]]] = []
    for a, b in chosen_no_direct:
        for tin, tout in ((a, b), (b, a)):
            for sample in plan["token_samples"][tin]:
                cid = (
                    f"nod-{_short(tin)}-{_short(tout)}-{sample['stratum']}-{sample['sample_rank']}"
                )
                nd_cells.setdefault((tin, tout, sample["stratum"]), []).append(cid)
                nd_rows.append((cid, tin, tout, sample))
    nd_split = assign_splits(config, nd_cells)
    for cid, tin, tout, sample in nd_rows:
        add(
            cid,
            tin,
            tout,
            int(sample["amount_in"]),
            {
                "origin": ORIGIN_LEG_DERIVED,
                "stratum": sample["stratum"],
                "split": nd_split[cid],
                "group": "no_direct_pool_pair",
                "selection": sample["selection"],
            },
        )

    boundary_records = []
    for pair in plan["universe"]["activity_pairs"]:
        a, b = pair["token_a"], pair["token_b"]
        for tin, tout in ((a, b), (b, a)):
            direct = direct_pools(tin, tout)
            if not direct:
                boundary_records.append(
                    {"token_in": tin, "token_out": tout, "skipped": "no direct pool"}
                )
                continue
            found, record = boundary_cases(direct, tin, tout, envelope[tin])
            boundary_records.append(record)
            for bc in found:
                add(
                    bc["case_id"],
                    tin,
                    tout,
                    int(bc["amount_in"]),
                    {
                        "origin": ORIGIN_STATE_DERIVED,
                        "stratum": STRATUM_BOUNDARY,
                        "split": "report",
                        "group": "boundary",
                        "selection": {"kind": bc["kind"], "pool_id": bc["pool_id"]},
                    },
                )

    sor_pools = [p for p in pools.values() if sor.get(_pool_source(p) or "") in ("V2", "V3")]
    for case in cases:
        meta = metadata[case.case_id]
        meta["notional_usd"] = str(prices.usd_value(case.token_in, case.amount_in))
        meta["direct_pools"] = len(direct_pools(case.token_in, case.token_out))
        meta["sor_direct_pools"] = sum(
            1 for p in sor_pools if {p.token0, p.token1} == {case.token_in, case.token_out}
        )
        meta["full_source_reachable"] = _reachable(pools.values(), case.token_in, case.token_out)
        meta["sor_reachable"] = _reachable(sor_pools, case.token_in, case.token_out)

    envelope_check = envelope_quotes(pools, envelope)
    block = BlockRef(
        chain_id=config.chain_id,
        number=config.block_number,
        hash=config.block_hash,
        timestamp=config.block_timestamp,
    )
    doc = corpus_document(
        config=config,
        plan=plan,
        pools=pools,
        sor=sor,
        metadata=metadata,
        exclusions=exclusions,
        envelope_check=envelope_check,
        chosen_no_direct=chosen_no_direct,
        boundary_records=boundary_records,
    )
    provenance = {
        "schema": "corpus-provenance/1",
        "corpus_id": config.corpus_id,
        "block": plan["block"],
        "block_verification": (
            "each of the five collectors resolved the block by number, required the corpus "
            "hash, required <= the finalized head, pinned every read to the hash with "
            "requireCanonical and re-read the header afterwards; every pool record carries "
            "read_at == this block"
        ),
        "config": plan["config"],
        "exports": dict(plan["exports"]),
        "sources": source_records,
    }
    parse_corpus_document(doc, block=block, pools=pools, cases=tuple(cases), prices=prices)
    return write_bundle(
        output_dir,
        bundle_id=f"{config.corpus_id}-{config.block_hash[2:10]}",
        kind="real",
        block=block,
        pools=list(pools.values()),
        cases=cases,
        provenance=provenance,
        prices=plan["prices"],
        corpus=doc,
    )


def envelope_quotes(pools: Mapping[str, Any], envelope: Mapping[str, int]) -> dict[str, Any]:
    """Quote every pool at the envelope in both directions; anything but a full fill or a
    proven exhaustion/dust refusal (i.e. incomplete, unsupported or reverting) fails."""
    out: dict[str, Any] = {}
    for pid in sorted(pools):
        pool = pools[pid]
        entry = {}
        for tin in (pool.token0, pool.token1):
            if tin not in envelope:
                raise CorpusError(f"pool {pid}: token {tin} is outside the corpus universe")
            r = _quote(pool, tin, envelope[tin])
            if r.status.value not in ACCEPTED_ENVELOPE_STATUSES:
                raise CorpusError(
                    f"pool {pid}: envelope swap of {envelope[tin]} {tin} is {r.status.value}: "
                    f"{r.detail}"
                )
            entry["zero_for_one" if tin == pool.token0 else "one_for_zero"] = r.status.value
        out[pid] = entry
    return out


def corpus_document(
    *,
    config: CorpusConfig,
    plan: Mapping[str, Any],
    pools: Mapping[str, Any],
    sor: Mapping[str, str | None],
    metadata: Mapping[str, dict[str, Any]],
    exclusions: list[dict[str, Any]],
    envelope_check: Mapping[str, Any],
    chosen_no_direct: Sequence[tuple[str, str]],
    boundary_records: list[dict[str, Any]],
) -> dict[str, Any]:
    all_ids = sorted(pools)
    sor_ids = sorted(p for p in all_ids if sor.get(_pool_source(pools[p]) or "") in ("V2", "V3"))
    split_counts = {s: sum(1 for m in metadata.values() if m["split"] == s) for s in SPLITS}
    return {
        "schema": CORPUS_DOC_SCHEMA,
        "corpus_id": config.corpus_id,
        "block": plan["block"],
        "config": plan["config"],
        "origin": {
            ORIGIN_LEG_DERIVED: (
                "pair and raw amount taken from one historical swap leg (dex.trades row) in the "
                "window; a leg is not a reconstructed user order and its endpoints were not "
                "reconstructed"
            ),
            ORIGIN_STATE_DERIVED: (
                "amount derived from the frozen pool state (rounding / liquidity boundary of a "
                "direct pool); not an empirical trade, report separately from empirical strata"
            ),
            "reconstructed_orders": False,
        },
        "strata": {
            "definition": (
                "rank-based raw input amount bands per sampling unit (one direction of an "
                "activity pair, or every leg selling a token for no-direct-pool pairs)"
            ),
            "bands": {k: list(v) for k, v in config.strata.items()},
            "boundary": "state-derived dust / rounding / liquidity-edge cases",
            "seed": config.seed,
            "samples_per_stratum": config.samples_per_stratum,
            "no_direct_samples_per_stratum": config.no_direct_samples_per_stratum,
        },
        "splits": {
            "rule": (
                f"within every (token_in, token_out, stratum) cell the {config.tuning_per_cell} "
                "case(s) with the smallest sha256(seed|case_id) are `tuning`, the rest `report`; "
                "boundary cases are `report`"
            ),
            "counts": split_counts,
        },
        "sources": [
            {
                "source_key": s.source_key,
                "collector": s.collector,
                "sor_protocol": sor.get(s.source_key),
            }
            for s in config.sources
        ],
        "universe": {
            "rule": plan["universe"]["rule"],
            "tokens": plan["universe"]["tokens"],
            "labels": plan["universe"]["labels"],
            "activity_pairs": plan["universe"]["activity_pairs"],
            "no_direct_pool_pairs": [{"token_a": a, "token_b": b} for a, b in chosen_no_direct],
            "no_direct_rule": (
                f"up to {config.no_direct_max_pairs} universe pairs with no admitted pool on any "
                "source at the block, ranked by the smaller token distinct-tx count (ties: "
                "addresses)"
            ),
        },
        "envelope": {
            **{k: v for k, v in plan["envelope"].items()},
            "check": dict(envelope_check),
        },
        "exclusions": {"rule": config.exclusion_rule(), "pools": exclusions},
        "boundary": boundary_records,
        "cohorts": {
            "full_source": {
                "description": "all five sources' admitted pools; every case",
                "sources": list(config.source_keys),
                "pools": all_ids,
            },
            "sor_compatible": {
                "description": (
                    "pools whose source has a Uniswap SOR protocol (V2/V3; Liquidity Book has "
                    "none); a case unreachable in this pool graph is `unsupported` for SOR, "
                    "never `no_route`"
                ),
                "sources": [
                    s.source_key for s in config.sources if sor.get(s.source_key) in ("V2", "V3")
                ],
                "pools": sor_ids,
            },
        },
        "case_metadata": {cid: metadata[cid] for cid in sorted(metadata)},
    }


def parse_corpus_document(
    raw: Any, *, block: Any, pools: Mapping[str, Any], cases: Sequence[Any], prices: Any
) -> dict[str, Any]:
    """Structural validation of `corpus.json` against the bundle it ships in."""
    _require_keys(
        raw,
        {
            "schema",
            "corpus_id",
            "block",
            "config",
            "origin",
            "strata",
            "splits",
            "sources",
            "universe",
            "envelope",
            "exclusions",
            "boundary",
            "cohorts",
            "case_metadata",
        },
        {"subset_of", "cohort"},
        "corpus",
    )
    if raw["schema"] != CORPUS_DOC_SCHEMA:
        raise CorpusError(f"corpus.schema: {raw['schema']!r} is not {CORPUS_DOC_SCHEMA!r}")
    blk = raw["block"]
    if not isinstance(blk, dict) or (
        blk.get("chain_id"),
        blk.get("number"),
        str(blk.get("hash")).lower(),
        blk.get("timestamp"),
    ) != (block.chain_id, block.number, block.hash.lower(), block.timestamp):
        raise CorpusError("corpus.block: differs from the bundle block")
    if raw["origin"].get("reconstructed_orders") is not False:
        raise CorpusError("corpus.origin: this corpus declares leg-derived cases only")
    source_keys = [s["source_key"] for s in raw["sources"]]
    if len(source_keys) != 5 or len(set(source_keys)) != 5:
        raise CorpusError("corpus.sources: exactly five distinct sources are required")
    represented = {getattr(p, "source_key", None) for p in pools.values()}
    if represented - set(source_keys):
        raise CorpusError(
            f"corpus: pools of undeclared sources {sorted(represented - set(source_keys), key=str)}"
        )
    missing = set(source_keys) - represented
    if raw.get("cohort") is not None:
        # A matched-cohort cut (`sor_cohort_bundle`, WHI-1444): exactly the sources outside
        # the named cohort lose their pools, and only in a declared subset.
        if raw["cohort"] != "sor_compatible" or "subset_of" not in raw:
            raise CorpusError("corpus.cohort: only a sor_compatible subset may omit sources")
        outside = set(source_keys) - set(raw["cohorts"]["sor_compatible"]["sources"])
        if missing != outside:
            raise CorpusError(
                f"corpus.cohort: the omitted sources {sorted(missing)} must be exactly the "
                f"non-SOR sources {sorted(outside)}"
            )
    elif missing:
        raise CorpusError(f"corpus: sources {sorted(missing)} have no admitted pool")
    tokens = raw["universe"]["tokens"]
    if sorted(tokens) != sorted(prices.tokens):
        raise CorpusError("corpus.universe.tokens: differs from the price context tokens")
    envelope = {t: int(a) for t, a in raw["envelope"]["amount_in"].items()}
    if sorted(envelope) != sorted(tokens):
        raise CorpusError("corpus.envelope: must size exactly the universe tokens")
    if sorted(raw["envelope"]["check"]) != sorted(pools):
        raise CorpusError("corpus.envelope.check: must cover exactly the bundle's pools")
    for pid, entry in raw["envelope"]["check"].items():
        if set(entry) != {"zero_for_one", "one_for_zero"} or not set(entry.values()) <= (
            ACCEPTED_ENVELOPE_STATUSES
        ):
            raise CorpusError(f"corpus.envelope.check[{pid}]: both directions must be covered")
    for p in pools.values():
        if p.token0 not in envelope or p.token1 not in envelope:
            raise CorpusError(f"pool {p.pool_id}: token outside the corpus universe")
    rule_id = raw["exclusions"]["rule"]["id"]
    for rec in raw["exclusions"]["pools"]:
        if rec.get("rule") != rule_id or not rec.get("reason"):
            raise CorpusError(
                f"corpus.exclusions: {rec.get('pool_id')} not under the declared rule"
            )
        if rec.get("pool_id") in pools:
            raise CorpusError(f"corpus.exclusions: excluded pool {rec['pool_id']} is in the bundle")
    meta = raw["case_metadata"]
    case_ids = [c.case_id for c in cases]
    if sorted(meta) != sorted(case_ids):
        raise CorpusError("corpus.case_metadata: must describe exactly the bundle's cases")
    for c in cases:
        m = meta[c.case_id]
        where = f"corpus.case_metadata[{c.case_id}]"
        _require_keys(
            m,
            {
                "origin",
                "stratum",
                "split",
                "group",
                "selection",
                "notional_usd",
                "direct_pools",
                "sor_direct_pools",
                "full_source_reachable",
                "sor_reachable",
            },
            set(),
            where,
        )
        if m["split"] not in SPLITS:
            raise CorpusError(f"{where}.split: {m['split']!r}")
        if m["origin"] == ORIGIN_LEG_DERIVED:
            if m["stratum"] not in STRATA:
                raise CorpusError(f"{where}.stratum: {m['stratum']!r} for a leg-derived case")
        elif m["origin"] == ORIGIN_STATE_DERIVED:
            if m["stratum"] != STRATUM_BOUNDARY or m["split"] != "report":
                raise CorpusError(f"{where}: a boundary case is stratum `boundary`, split `report`")
        else:
            raise CorpusError(f"{where}.origin: {m['origin']!r}")
        if c.token_in not in envelope or c.token_out not in envelope:
            raise CorpusError(f"{where}: token outside the corpus universe")
        if c.amount_in > envelope[c.token_in]:
            raise CorpusError(f"{where}: amount {c.amount_in} exceeds the envelope")
        direct = sum(1 for p in pools.values() if {p.token0, p.token1} == {c.token_in, c.token_out})
        if m["direct_pools"] != direct:
            raise CorpusError(f"{where}.direct_pools: {m['direct_pools']} != {direct}")
    groups = {m["group"] for m in meta.values()}
    if "no_direct_pool_pair" not in groups or any(
        m["direct_pools"] for m in meta.values() if m["group"] == "no_direct_pool_pair"
    ):
        raise CorpusError("corpus: no-direct-pool cases are missing or have a direct pool")
    counts = {s: sum(1 for m in meta.values() if m["split"] == s) for s in SPLITS}
    if raw["splits"]["counts"] != counts:
        raise CorpusError(f"corpus.splits.counts: {raw['splits']['counts']} != {counts}")
    cohorts = raw["cohorts"]
    _require_keys(cohorts, {"full_source", "sor_compatible"}, set(), "corpus.cohorts")
    if cohorts["full_source"]["pools"] != sorted(pools):
        raise CorpusError("corpus.cohorts.full_source: must list every pool")
    sor_sources = set(cohorts["sor_compatible"]["sources"])
    expected_sor = sorted(
        p for p, s in pools.items() if getattr(s, "source_key", None) in sor_sources
    )
    if cohorts["sor_compatible"]["pools"] != expected_sor:
        raise CorpusError("corpus.cohorts.sor_compatible: pools disagree with its sources")
    return dict(raw)


def validate_corpus_bundle(bundle: Any) -> dict[str, Any]:
    """The deep offline check of a loaded corpus bundle (no network): every pool is quoted
    at the envelope in both directions and must be covered by its captured state."""
    if bundle.corpus is None or bundle.prices is None:
        raise CorpusError(f"bundle {bundle.bundle_id!r} is not a corpus bundle")
    envelope = {t: int(a) for t, a in bundle.corpus["envelope"]["amount_in"].items()}
    observed = envelope_quotes(bundle.pools, envelope)
    if observed != bundle.corpus["envelope"]["check"]:
        raise CorpusError("envelope quotes differ from the recorded envelope check")
    meta = bundle.corpus["case_metadata"]
    groups: dict[str, int] = {}
    for m in meta.values():
        groups[m["group"]] = groups.get(m["group"], 0) + 1
    return {
        "pools": len(bundle.pools),
        "cases": len(bundle.cases),
        "groups": groups,
        "splits": bundle.corpus["splits"]["counts"],
        "sources": sorted({getattr(p, "source_key", "") for p in bundle.pools.values()}),
    }


# ---------------------------------------------------------------------------
# file-level drivers (main.py corpus ...)
# ---------------------------------------------------------------------------

EXPORT_FILES = {
    "activity": ("q1_activity.sql", "q1_activity.jsonl"),
    "strata": ("q2_strata.sql", "q2_strata.jsonl"),
    "prices": ("q3_prices.sql", "q3_prices.jsonl"),
}


def generate_sql(config: CorpusConfig, step: str, exports_dir: Path) -> str:
    if step == "activity":
        return activity_sql(config)
    universe = select_universe(config, load_export(exports_dir / EXPORT_FILES["activity"][1]))
    if step == "strata":
        return strata_sql(config, sampling_units(universe))
    if step == "prices":
        return prices_sql(config, universe.tokens)
    raise CorpusError(f"unknown SQL step {step!r} (activity, strata, prices)")


def check_sql_files(config: CorpusConfig, exports_dir: Path) -> None:
    """Every saved SQL file equals the SQL the config (and upstream exports) generate, and
    each export names that SQL's hash."""
    for step, (sql_name, export_name) in EXPORT_FILES.items():
        text = (exports_dir / sql_name).read_text()
        if text != generate_sql(config, step, exports_dir):
            raise CorpusError(f"{exports_dir / sql_name}: not the SQL the config generates")
        export = load_export(exports_dir / export_name)
        if export.sql_sha256 != hashlib.sha256(text.encode()).hexdigest():
            raise CorpusError(f"{exports_dir / export_name}: exported from different SQL")


def run_plan(config: CorpusConfig, exports_dir: Path, catalog: Any) -> dict[str, Any]:
    check_sql_files(config, exports_dir)
    tiers = {s.key: list((s.expected_fee_tiers or {}).keys()) for s in catalog.sources}
    return plan_corpus(
        config,
        load_export(exports_dir / EXPORT_FILES["activity"][1]),
        load_export(exports_dir / EXPORT_FILES["strata"][1]),
        load_export(exports_dir / EXPORT_FILES["prices"][1]),
        tiers,
    )


def write_plan(
    config: CorpusConfig, plan: Mapping[str, Any], plan_path: Path, prepare_dir: Path
) -> dict[str, str]:
    """Write `plan.json` and the five generated prepare configs; returns path -> sha256."""
    written: dict[str, str] = {}
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(plan, indent=2, sort_keys=True) + "\n"
    plan_path.write_text(text)
    written[str(plan_path)] = hashlib.sha256(text.encode()).hexdigest()
    prepare_dir.mkdir(parents=True, exist_ok=True)
    for collector, doc in plan["prepare_configs"].items():
        path = prepare_dir / f"{collector}.yaml"
        body = prepare_config_text(config, collector, doc)
        path.write_text(body)
        written[str(path)] = hashlib.sha256(body.encode()).hexdigest()
    return written


def subset_corpus_bundle(
    bundle: Any,
    pool_ids: Iterable[str],
    case_ids: Iterable[str],
    output_dir: Path,
    *,
    id_suffix: str = "fixture",
    cohort: str | None = None,
) -> Any:
    """A representative regression fixture from a full corpus bundle: the same block,
    the chosen pool records unchanged, the chosen cases, and a corpus descriptor whose
    pool-dependent fields (direct pools, reachability, envelope check, cohorts, split
    counts) are recomputed for the subset and which names the full bundle it came from."""
    from snapshot.bundle import PROVENANCE_FILE, write_bundle

    if bundle.corpus is None or bundle.prices is None:
        raise CorpusError("not a corpus bundle")
    keep_pools = {pid: bundle.pools[pid] for pid in sorted(set(pool_ids))}
    keep_cases = [c for c in bundle.cases if c.case_id in set(case_ids)]
    doc = copy.deepcopy(dict(bundle.corpus))
    sor_sources = set(doc["cohorts"]["sor_compatible"]["sources"])
    sor_pools = [p for p in keep_pools.values() if _pool_source(p) in sor_sources]
    meta = {}
    for c in keep_cases:
        m = dict(doc["case_metadata"][c.case_id])
        m["direct_pools"] = sum(
            1 for p in keep_pools.values() if {p.token0, p.token1} == {c.token_in, c.token_out}
        )
        m["sor_direct_pools"] = sum(
            1 for p in sor_pools if {p.token0, p.token1} == {c.token_in, c.token_out}
        )
        m["full_source_reachable"] = _reachable(keep_pools.values(), c.token_in, c.token_out)
        m["sor_reachable"] = _reachable(sor_pools, c.token_in, c.token_out)
        meta[c.case_id] = m
    doc["case_metadata"] = meta
    doc["splits"]["counts"] = {s: sum(1 for m in meta.values() if m["split"] == s) for s in SPLITS}
    doc["envelope"]["check"] = {pid: doc["envelope"]["check"][pid] for pid in keep_pools}
    doc["cohorts"]["full_source"]["pools"] = sorted(keep_pools)
    doc["cohorts"]["sor_compatible"]["pools"] = sorted(p.pool_id for p in sor_pools)
    doc["subset_of"] = {"bundle_id": bundle.bundle_id, "bundle_hash": bundle.bundle_hash}
    if cohort is not None:
        doc["cohort"] = cohort
    provenance = json.loads((Path(bundle.source_path) / PROVENANCE_FILE).read_text())
    provenance["subset_of"] = doc["subset_of"]
    for record in provenance["sources"].values():
        pools_prov = record["provenance"].get("pools", {})
        record["provenance"]["pools"] = {k: v for k, v in pools_prov.items() if k in keep_pools}
        record["provenance"].pop("admission", None)
    prices_doc = json.loads((Path(bundle.source_path) / "prices.json").read_text())
    return write_bundle(
        output_dir,
        bundle_id=f"{bundle.bundle_id}-{id_suffix}",
        kind="real",
        block=bundle.block,
        pools=list(keep_pools.values()),
        cases=keep_cases,
        provenance=provenance,
        prices=prices_doc,
        corpus=doc,
    )


def sor_cohort_bundle(bundle: Any, output_dir: Path) -> Any:
    """The matched V2/V3 comparison bundle (docs/DESIGN.md §2.7; WHI-1444): the corpus's
    `sor_compatible` pools only (no Liquidity Book) and **every** case, so all algorithms
    share `uni_sor_port`'s candidate universe. Built with `subset_corpus_bundle`, so the
    pool records are unchanged and the descriptor names the full bundle (`subset_of`)."""
    if bundle.corpus is None:
        raise CorpusError("not a corpus bundle")
    pools = bundle.corpus["cohorts"]["sor_compatible"]["pools"]
    cases = [c.case_id for c in bundle.cases]
    return subset_corpus_bundle(
        bundle, pools, cases, output_dir, id_suffix="sor-cohort", cohort="sor_compatible"
    )


def fixture_selection(bundle: Any, path: Path) -> tuple[list[str], list[str]]:
    """(pool ids, case ids) of the declared fixture (`config/corpus/fixture.yaml`)."""
    raw = yaml.safe_load(path.read_text())
    _require_keys(raw, {"pairs", "case_pairs"}, set(), str(path))
    if bundle.corpus is None:
        raise CorpusError("not a corpus bundle")
    by_label = {label: t for t, label in bundle.corpus["universe"]["labels"].items()}

    def pairs(key: str) -> set[frozenset[str]]:
        try:
            return {frozenset(by_label[t] for t in p) for p in raw[key]}
        except KeyError as exc:
            raise CorpusError(f"{path}: unknown token label {exc}") from exc

    pool_pairs, case_pairs = pairs("pairs"), pairs("case_pairs")
    pool_ids = sorted(
        pid for pid, p in bundle.pools.items() if frozenset((p.token0, p.token1)) in pool_pairs
    )
    case_ids = [
        c.case_id for c in bundle.cases if frozenset((c.token_in, c.token_out)) in case_pairs
    ]
    return pool_ids, case_ids

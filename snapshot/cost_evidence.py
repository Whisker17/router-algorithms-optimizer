"""Execution-cost evidence: the Dune transaction export and the Mantle fee rules it is
read with (WHI-1445 / I19; docs/DESIGN.md §2.9, docs/references/cost-model.md).

Three pieces, all preparation-time (the benchmark itself stays offline):

1. **The export.** `samples_sql` generates one bounded query (chain `mantle`, the
   `block_month`/`block_date` partitions and the absolute window of
   `config/cost_calibration.yaml`, which ends at the corpus snapshot timestamp). It
   groups `dex.trades` legs **by transaction hash first** -- so every transaction appears
   once and a whole-transaction fee is never repeated per leg -- joins the transaction's
   own `mantle.transactions` fee fields once, and returns a single row whose packed
   columns hold the window census, router mix, fee-field reconciliation counts and a
   seeded uniform transaction sample. `ingest_samples` checks the Dune-side SQL is
   byte-identical to the generated file and the execution completed, and writes the
   canonical export `samples.jsonl` (identity = its SHA-256). `load_samples` rejects a
   duplicated transaction key: a leg-level (per-swap) export can never be read as
   transactions.
2. **Fee rules.** Post-Arsia Mantle charges `TotalFee = L2ExecutionFee + L1DataFee +
   OperatorFee` with `L2ExecutionFee = gasUsed * effectiveGasPrice` and
   `OperatorFee = operatorFeeConstant + operatorFeeScalar * 100 * gasUsed` (Mantle "Fee
   Model Handbook (After Arsia)"). `mantle.transactions.gas_price` is the effective
   price and `l1_fee` the receipt's separate `l1Fee`; Dune's `gas.fees.tx_fee_raw` is
   `gasUsed * gasPrice + l1Fee` -- it **omits the operator fee**, so it is neither the
   total nor something to add `l1_fee` to again. `total_fee_wei` is the only place the
   three components are combined.
3. **Receipt evidence.** `capture_fee_evidence` reads, over the public RPC with the
   on-disk cache, a handful of receipts (the Dune fields must equal them), the sender's
   balance just before and after the transaction's block (the balance drop must equal
   the recomputed total exactly), and the `L1Block` operator-fee parameters at evenly
   spaced window checkpoints. `check_fee_evidence` re-verifies the saved records offline.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from snapshot.corpus import DuneExport, canonical_export_text, load_export, read_dune_result

DEFAULT_COST_CONFIG = Path("config/cost_calibration.yaml")
EVIDENCE_FILE = "fee_evidence.json"
EVIDENCE_SCHEMA = "fee-evidence/1"
FAMILIES = ("concentrated", "liquidity_book", "constant_product")
_FAMILY_CODE = {"concentrated": "c", "liquidity_book": "l", "constant_product": "p"}
# Operator fee multiplier of the post-Arsia (OP Stack Jovian-aligned) formula.
OPERATOR_FEE_MULTIPLIER = 100
L1_BLOCK = "0x4200000000000000000000000000000000000015"
# keccak256 selectors of L1Block's `operatorFeeScalar()` / `operatorFeeConstant()`.
SEL_OPERATOR_FEE_SCALAR = "0x4d5d9a2a"
SEL_OPERATOR_FEE_CONSTANT = "0x16d3bc7f"
MANTLE_FEE_DOC = (
    "https://docs.mantle.xyz/network/system-information/fee-mechanism/"
    "fee-model-handbook-after-arsia.md"
)
ARSIA_ACTIVATION_TIMESTAMP = 1776841200  # 2026-04-22T07:00:00Z (Mantle v2 v1.5.4 notes)


class CostEvidenceError(ValueError):
    """The calibration config, an export or a fee-evidence record fails validation."""


def _require_keys(obj: Any, required: set[str], optional: set[str], where: str) -> None:
    if not isinstance(obj, dict):
        raise CostEvidenceError(f"{where}: expected a mapping, got {type(obj).__name__}")
    missing = required - obj.keys()
    if missing:
        raise CostEvidenceError(f"{where}: missing required key(s) {sorted(missing)}")
    unknown = obj.keys() - required - optional
    if unknown:
        raise CostEvidenceError(f"{where}: unknown key(s) {sorted(unknown)}")


def _int(value: Any, lo: int, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < lo:
        raise CostEvidenceError(f"{where}: expected an integer >= {lo}, got {value!r}")
    return value


def _utc_seconds(text: Any, where: str) -> int:
    from datetime import UTC, datetime

    try:
        parsed = datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError as exc:
        raise CostEvidenceError(f"{where}: expected 'YYYY-MM-DD HH:MM:SS' UTC") from exc
    return int(parsed.timestamp())


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FamilyLabel:
    dune_project: str
    dune_version: str
    family: str


@dataclass(frozen=True)
class CostConfig:
    model_id: str
    chain_id: int
    block_number: int
    block_hash: str
    block_timestamp: int
    window_start: str
    window_end: str
    window_start_ts: int
    window_end_ts: int
    block_months: tuple[str, ...]
    performance: str
    families: tuple[FamilyLabel, ...]
    seed: str
    modulus: int
    top_routers: int
    holdout_of_10: int
    min_train: int
    min_holdout: int
    min_family_train: int
    scenario_quantiles: tuple[float, float]
    rpc_url: str
    receipt_samples: int
    param_checkpoints: int
    source_path: str
    sha256: str


def parse_cost_config(raw: Any, *, source_path: str, sha256: str) -> CostConfig:
    _require_keys(
        raw,
        {
            "schema_version",
            "model_id",
            "snapshot",
            "dune",
            "families",
            "sampling",
            "split",
            "model",
            "evidence",
        },
        set(),
        "<root>",
    )
    if raw["schema_version"] != 1:
        raise CostEvidenceError(f"schema_version: unsupported {raw['schema_version']!r}")
    snap = raw["snapshot"]
    _require_keys(
        snap, {"chain_id", "block_number", "block_hash", "block_timestamp"}, set(), "snapshot"
    )
    dune = raw["dune"]
    _require_keys(
        dune, {"window_start", "window_end", "block_months", "performance"}, set(), "dune"
    )
    start_ts = _utc_seconds(dune["window_start"], "dune.window_start")
    end_ts = _utc_seconds(dune["window_end"], "dune.window_end")
    block_ts = _int(snap["block_timestamp"], 0, "snapshot.block_timestamp")
    if not start_ts < end_ts <= block_ts:
        raise CostEvidenceError(
            "dune: the window must be non-empty and end no later than the snapshot timestamp"
        )
    if start_ts < ARSIA_ACTIVATION_TIMESTAMP:
        raise CostEvidenceError("dune.window_start: before Mantle's Arsia fee model activated")
    families = []
    for i, entry in enumerate(raw["families"]):
        _require_keys(entry, {"dune_project", "dune_version", "family"}, set(), f"families[{i}]")
        if entry["family"] not in FAMILIES:
            raise CostEvidenceError(f"families[{i}].family: not one of {FAMILIES}")
        families.append(
            FamilyLabel(str(entry["dune_project"]), str(entry["dune_version"]), entry["family"])
        )
    sampling = raw["sampling"]
    _require_keys(sampling, {"seed", "modulus", "top_routers"}, set(), "sampling")
    modulus = _int(sampling["modulus"], 1, "sampling.modulus")
    if modulus & (modulus - 1):
        raise CostEvidenceError("sampling.modulus: must be a power of two")
    split = raw["split"]
    _require_keys(split, {"holdout_of_10"}, set(), "split")
    holdout = _int(split["holdout_of_10"], 1, "split.holdout_of_10")
    if holdout >= 10:
        raise CostEvidenceError("split.holdout_of_10: must leave training data")
    model = raw["model"]
    _require_keys(
        model,
        {"min_train", "min_holdout", "min_family_train", "scenario_quantiles"},
        set(),
        "model",
    )
    quantiles = model["scenario_quantiles"]
    if (
        not isinstance(quantiles, list)
        or len(quantiles) != 2
        or not all(isinstance(q, float) for q in quantiles)
        or not 0 < quantiles[0] < 0.5 < quantiles[1] < 1
    ):
        raise CostEvidenceError("model.scenario_quantiles: expected [low < 0.5 < high] in (0, 1)")
    evidence = raw["evidence"]
    _require_keys(evidence, {"rpc_url", "receipt_samples", "param_checkpoints"}, set(), "evidence")
    return CostConfig(
        model_id=str(raw["model_id"]),
        chain_id=_int(snap["chain_id"], 1, "snapshot.chain_id"),
        block_number=_int(snap["block_number"], 0, "snapshot.block_number"),
        block_hash=str(snap["block_hash"]).lower(),
        block_timestamp=block_ts,
        window_start=str(dune["window_start"]),
        window_end=str(dune["window_end"]),
        window_start_ts=start_ts,
        window_end_ts=end_ts,
        block_months=tuple(str(m) for m in dune["block_months"]),
        performance=str(dune["performance"]),
        families=tuple(families),
        seed=str(sampling["seed"]),
        modulus=modulus,
        top_routers=_int(sampling["top_routers"], 1, "sampling.top_routers"),
        holdout_of_10=holdout,
        min_train=_int(model["min_train"], 1, "model.min_train"),
        min_holdout=_int(model["min_holdout"], 1, "model.min_holdout"),
        min_family_train=_int(model["min_family_train"], 1, "model.min_family_train"),
        scenario_quantiles=(quantiles[0], quantiles[1]),
        rpc_url=str(evidence["rpc_url"]),
        receipt_samples=_int(evidence["receipt_samples"], 1, "evidence.receipt_samples"),
        param_checkpoints=_int(evidence["param_checkpoints"], 2, "evidence.param_checkpoints"),
        source_path=source_path,
        sha256=sha256,
    )


def load_cost_config(path: str | Path = DEFAULT_COST_CONFIG) -> CostConfig:
    data = Path(path).read_bytes()
    try:
        raw = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise CostEvidenceError(f"{path}: invalid YAML: {exc}") from exc
    try:
        return parse_cost_config(
            raw, source_path=str(path), sha256=hashlib.sha256(data).hexdigest()
        )
    except CostEvidenceError as exc:
        raise CostEvidenceError(f"{path}: {exc}") from exc


# ---------------------------------------------------------------------------
# SQL (bounded: chain, partitions and the absolute window on every scan)
# ---------------------------------------------------------------------------


def _day(ts_text: str) -> str:
    return ts_text.split(" ", 1)[0]


def _end_day_exclusive(config: CostConfig) -> str:
    """The first `block_date` not needed: the day of `window_end`, plus one unless the
    window ends exactly at midnight."""
    from datetime import UTC, datetime, timedelta

    end = datetime.fromtimestamp(config.window_end_ts, UTC)
    day = end.date() if end.time() == end.time().min else end.date() + timedelta(days=1)
    return day.isoformat()


def _window(config: CostConfig, q: str = "") -> str:
    return (
        f"{q}block_time >= TIMESTAMP '{config.window_start}' "
        f"AND {q}block_time < TIMESTAMP '{config.window_end}'"
    )


def _days(config: CostConfig, q: str = "") -> str:
    return (
        f"{q}block_date >= DATE '{_day(config.window_start)}' "
        f"AND {q}block_date < DATE '{_end_day_exclusive(config)}'"
    )


def _tx_cte(config: CostConfig) -> str:
    """Legs grouped **by transaction first** (`tx_legs`), then the transaction's own fee
    row joined once (`j`). Every later column is per transaction, never per leg."""
    fam_case = "\n".join(
        f"      WHEN project = '{f.dune_project}' AND version = '{f.dune_version}' "
        f"THEN '{_FAMILY_CODE[f.family]}'"
        for f in config.families
    )
    months = ", ".join(f"DATE '{m}'" for m in config.block_months)
    window = _window(config)
    return f"""legs AS (
  SELECT
    tx_hash,
    evt_index,
    CASE
{fam_case}
      ELSE 'o'
    END AS fam,
    project_contract_address AS pool,
    token_sold_address AS token_in,
    token_bought_address AS token_out
  FROM dex.trades
  WHERE blockchain = 'mantle'
    AND block_month IN ({months})
    AND {window}
),
tx_legs AS (
  SELECT
    tx_hash,
    COUNT(*) AS legs,
    COUNT(DISTINCT evt_index) AS evts,
    COUNT_IF(fam = 'c') AS c,
    COUNT_IF(fam = 'l') AS l,
    COUNT_IF(fam = 'p') AS p,
    COUNT_IF(fam = 'o') AS o,
    COUNT(DISTINCT pool) AS pools,
    COUNT(DISTINCT IF(fam = 'c', pool)) AS pc,
    COUNT(DISTINCT IF(fam = 'l', pool)) AS pl,
    COUNT(DISTINCT IF(fam = 'p', pool)) AS pp,
    COUNT(DISTINCT token_in) AS sold,
    COUNT(DISTINCT token_out) AS bought,
    cardinality(array_union(array_agg(token_in), array_agg(token_out))) AS tokens,
    cardinality(array_except(array_agg(token_in), array_agg(token_out))) AS sources,
    cardinality(array_except(array_agg(token_out), array_agg(token_in))) AS sinks
  FROM legs
  GROUP BY tx_hash
),
j AS (
  SELECT
    tl.*,
    tx."to" AS tx_to,
    tx.block_number,
    tx.gas_used,
    tx.gas_price,
    tx.l1_fee,
    tx.success,
    xxhash64(to_utf8(concat('{config.seed}|', lower(to_hex(tl.tx_hash))))) AS h
  FROM tx_legs tl
  LEFT JOIN mantle.transactions tx
    ON tx.hash = tl.tx_hash
   AND {_days(config, "tx.")}
   AND {_window(config, "tx.")}
)"""


def census_sql(config: CostConfig) -> str:
    """Q1: the window population per transaction shape (the sample's denominators),
    the leg-row duplicate check, missing/failed transactions and the top routers."""
    return f"""-- WHI-1445 cost-calibration Q1 (window census) for {config.model_id}
-- window [{config.window_start}, {config.window_end}) UTC
-- the window ends no later than snapshot block {config.block_number}'s timestamp
WITH {_tx_cte(config)},
shapes AS (
  SELECT
    'shape' AS kind,
    CAST(NULL AS VARBINARY) AS tx_to,
    c, l, p, o, pools, pc, pl, pp, sold, bought, tokens, sources, sinks,
    COUNT(*) AS txs,
    COUNT_IF(success = false) AS failed,
    COUNT_IF(gas_used IS NULL) AS missing,
    COUNT_IF(legs <> evts) AS duplicate_leg_rows,
    SUM(legs) AS legs
  FROM j
  GROUP BY c, l, p, o, pools, pc, pl, pp, sold, bought, tokens, sources, sinks
),
routers AS (
  SELECT
    'router' AS kind,
    tx_to,
    NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    COUNT(*) AS txs,
    COUNT_IF(success = false) AS failed,
    0,
    0,
    SUM(legs) AS legs
  FROM j
  WHERE tx_to IS NOT NULL
  GROUP BY tx_to
  ORDER BY COUNT(*) DESC, tx_to
  LIMIT {config.top_routers}
)
SELECT * FROM shapes
UNION ALL
SELECT * FROM routers
ORDER BY kind, txs DESC, c, l, p, o, pools, pc, pl, pp, sold, bought, tokens, sources, sinks,
  tx_to
"""


def samples_sql(config: CostConfig) -> str:
    """Q2: one row per sampled transaction (seeded uniform sample over the window's
    swap transactions): shape features, success, router, the exact fee fields of
    `mantle.transactions` and Dune `gas.fees`."""
    mask = config.modulus - 1
    months = ", ".join(f"DATE '{m}'" for m in config.block_months)
    window = _window(config)
    return f"""-- WHI-1445 cost-calibration Q2 (transaction samples) for {config.model_id}
-- window [{config.window_start}, {config.window_end}) UTC
-- the window ends no later than snapshot block {config.block_number}'s timestamp
-- sample: h = xxhash64('{config.seed}|' || tx hash hex); sampled iff h & {mask} = 0
WITH {_tx_cte(config)},
s AS (
  SELECT * FROM j WHERE bitwise_and(from_big_endian_64(h), {mask}) = 0
),
gf AS (
  SELECT tx_hash, tx_fee_raw, gas_used AS gf_gas_used, gas_price AS gf_gas_price
  FROM gas.fees
  WHERE blockchain = 'mantle'
    AND block_month IN ({months})
    AND {window}
)
SELECT
  substr(lower(to_hex(s.h)), 1, 8) AS sample_key,
  s.tx_hash,
  s.block_number,
  s.tx_to,
  s.success,
  s.legs,
  s.evts,
  s.c,
  s.l,
  s.p,
  s.o,
  s.pools,
  s.pc,
  s.pl,
  s.pp,
  s.sold,
  s.bought,
  s.tokens,
  s.sources,
  s.sinks,
  s.gas_used,
  CAST(s.gas_price AS VARCHAR) AS gas_price,
  CAST(s.l1_fee AS VARCHAR) AS l1_fee,
  CAST(gf.tx_fee_raw AS VARCHAR) AS gasfees_tx_fee_raw,
  gf.gf_gas_used = s.gas_used AND gf.gf_gas_price = s.gas_price AS gasfees_same_gas
FROM s
LEFT JOIN gf ON gf.tx_hash = s.tx_hash
ORDER BY sample_key, s.tx_hash
"""


EXPORTS = {
    "census": ("q1_census.sql", "q1_census.jsonl", census_sql),
    "samples": ("q2_samples.sql", "q2_samples.jsonl.gz", samples_sql),
}


def generate_sql(config: CostConfig, name: str) -> str:
    return EXPORTS[name][2](config)


def check_sql_files(config: CostConfig, exports_dir: Path) -> None:
    """The checked-in SQL files are byte-identical to what the config generates."""
    for name, (sql_file, _, make) in EXPORTS.items():
        path = exports_dir / sql_file
        if not path.is_file() or path.read_text() != make(config):
            raise CostEvidenceError(
                f"{path}: differs from the {name} SQL generated from {config.source_path}"
            )


def ingest(
    config: CostConfig,
    name: str,
    raw_path: str | Path,
    exports_dir: Path,
    *,
    query_id: int | None = None,
) -> DuneExport:
    """A saved Dune result (`createAndExecuteQuery` or `getExecutionResults` JSON) ->
    the canonical export `EXPORTS[name]`. The checked-in SQL must be the generated SQL;
    if the saved document carries the Dune-side query text it must be byte-identical;
    the execution must be COMPLETED with every row present."""
    sql_file, export_file, make = EXPORTS[name]
    sql_path = exports_dir / sql_file
    sql_text = make(config)
    if not sql_path.is_file() or sql_path.read_text() != sql_text:
        raise CostEvidenceError(f"{sql_path}: differs from the generated {name} SQL")
    doc = read_dune_result(raw_path)
    query = doc.get("query") or {}
    stored = query.get("query")
    if stored is not None and stored != sql_text:
        raise CostEvidenceError(f"{raw_path}: the Dune query text differs from {sql_path}")
    preview = doc.get("result_preview", doc)
    if preview.get("state") != "COMPLETED":
        raise CostEvidenceError(f"{raw_path}: execution state {preview.get('state')!r}")
    meta = preview.get("resultMetadata") or {}
    columns = tuple(c["name"] for c in meta.get("columns", []))
    rows = preview.get("data", {}).get("rows", [])
    total = meta.get("totalRowCount")
    if total is None or total != len(rows):
        raise CostEvidenceError(f"{raw_path}: {len(rows)} row(s) saved of {total}; partial")
    qid = int(query.get("query_id") or doc.get("query_id") or query_id or 0)
    if qid <= 0:
        raise CostEvidenceError(f"{raw_path}: no Dune query id (pass it explicitly)")
    execution_id = str(preview.get("executionId") or doc.get("execution", {}).get("execution_id"))
    text = canonical_export_text(name, qid, execution_id, sql_text, columns, rows)
    data = text.encode()
    if export_file.endswith(".gz"):
        data = gzip.compress(data, compresslevel=9, mtime=0)
    (exports_dir / export_file).write_bytes(data)
    return load_cost_export(exports_dir / export_file)


def load_cost_export(path: str | Path) -> DuneExport:
    """`snapshot.corpus.load_export` for a plain or gzip-compressed canonical export. The
    identity (`sha256`) is always that of the uncompressed canonical text."""
    path = Path(path)
    if not path.name.endswith(".gz"):
        return load_export(path)
    data = gzip.decompress(path.read_bytes())
    lines = data.decode().splitlines()
    if not lines:
        raise CostEvidenceError(f"{path}: empty export")
    header = json.loads(lines[0])
    _require_keys(
        header,
        {"export", "query_id", "execution_id", "sql_sha256", "columns", "row_count"},
        set(),
        f"{path}:1",
    )
    rows = tuple(json.loads(line) for line in lines[1:])
    if len(rows) != header["row_count"]:
        raise CostEvidenceError(f"{path}: {len(rows)} row(s), header says {header['row_count']}")
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
# typed samples
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Shape:
    """Per-transaction shape features (all `dex.trades` legs of one transaction)."""

    legs_concentrated: int
    legs_liquidity_book: int
    legs_constant_product: int
    legs_other: int
    distinct_pools: int
    pools_concentrated: int
    pools_liquidity_book: int
    pools_constant_product: int
    tokens_sold: int
    tokens_bought: int
    tokens: int
    source_tokens: int
    sink_tokens: int


@dataclass(frozen=True)
class TxSample:
    key: str
    tx_hash: str
    block_number: int | None
    router: str | None
    success: bool | None  # None: no transaction row
    shape: Shape
    gas_used: int | None
    gas_price_wei: int | None  # mantle.transactions.gas_price (effective price)
    l1_fee_wei: int | None
    gasfees_tx_fee_raw: int | None
    gasfees_same_gas: bool


@dataclass(frozen=True)
class SampleSet:
    census: DuneExport
    samples: DuneExport
    txs: tuple[TxSample, ...]


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _shape(row: Mapping[str, Any]) -> Shape:
    return Shape(
        *(
            int(row[k])
            for k in (
                "c",
                "l",
                "p",
                "o",
                "pools",
                "pc",
                "pl",
                "pp",
                "sold",
                "bought",
                "tokens",
                "sources",
                "sinks",
            )
        )
    )


def tx_samples(export: DuneExport) -> tuple[TxSample, ...]:
    """Typed rows of the samples export. A transaction may appear only once (a repeated
    hash means per-leg rows, which would repeat the whole-transaction fee per leg); a
    shape with fewer distinct log indexes than leg rows is a duplicated leg row."""
    seen: set[str] = set()
    out = []
    for n, row in enumerate(export.rows, start=2):
        tx_hash = str(row["tx_hash"]).lower()
        if tx_hash in seen:
            raise CostEvidenceError(
                f"samples row {n}: transaction {tx_hash} repeated -- an export holds one row "
                "per transaction, never one per swap leg"
            )
        seen.add(tx_hash)
        if int(row["legs"]) != int(row["evts"]):
            raise CostEvidenceError(f"samples row {n}: {tx_hash} has duplicated leg rows")
        gas_used = _opt_int(row["gas_used"])
        gas_price = _opt_int(row["gas_price"])
        out.append(
            TxSample(
                key=str(row["sample_key"]),
                tx_hash=tx_hash,
                block_number=_opt_int(row["block_number"]),
                router=None if row["tx_to"] is None else str(row["tx_to"]).lower(),
                success=row["success"],
                shape=_shape(row),
                gas_used=gas_used,
                gas_price_wei=gas_price,
                l1_fee_wei=_opt_int(row["l1_fee"]),
                gasfees_tx_fee_raw=_opt_int(row["gasfees_tx_fee_raw"]),
                gasfees_same_gas=bool(row["gasfees_same_gas"]),
            )
        )
    return tuple(out)


def load_sample_set(exports_dir: str | Path, config: CostConfig | None = None) -> SampleSet:
    exports_dir = Path(exports_dir)
    if config is not None:
        check_sql_files(config, exports_dir)
    loaded = {}
    for name, (sql_file, export_file, _) in EXPORTS.items():
        export = load_cost_export(exports_dir / export_file)
        sql_sha = hashlib.sha256((exports_dir / sql_file).read_bytes()).hexdigest()
        if export.sql_sha256 != sql_sha:
            raise CostEvidenceError(f"{export_file}: exported from different SQL than {sql_file}")
        loaded[name] = export
    return SampleSet(
        census=loaded["census"], samples=loaded["samples"], txs=tx_samples(loaded["samples"])
    )


# ---------------------------------------------------------------------------
# fee rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeeRules:
    """Post-Arsia Mantle fee components, with the operator-fee parameters observed
    (constant) over the calibration window."""

    operator_fee_scalar: int
    operator_fee_constant: int

    @property
    def operator_fee_per_gas_wei(self) -> int:
        return self.operator_fee_scalar * OPERATOR_FEE_MULTIPLIER

    def operator_fee_wei(self, gas_used: int) -> int:
        return self.operator_fee_constant + self.operator_fee_per_gas_wei * gas_used

    def total_fee_wei(self, gas_used: int, effective_gas_price_wei: int, l1_fee_wei: int) -> int:
        """The whole transaction's fee, charged once per transaction:
        L2 execution (`gasUsed * effectiveGasPrice`) + the separate L1 data fee + the
        operator fee. `l1_fee_wei` is the receipt's own `l1Fee` -- never a total that
        already contains it (Dune `gas.fees.tx_fee_raw` does)."""
        for name, value in (
            ("gas_used", gas_used),
            ("effective_gas_price_wei", effective_gas_price_wei),
            ("l1_fee_wei", l1_fee_wei),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise CostEvidenceError(f"{name}: expected a non-negative int, got {value!r}")
        return gas_used * effective_gas_price_wei + l1_fee_wei + self.operator_fee_wei(gas_used)

    def to_dict(self) -> dict[str, Any]:
        return {
            "formula": (
                "total = gasUsed * effectiveGasPrice + l1Fee + "
                "(operatorFeeConstant + operatorFeeScalar * 100 * gasUsed)"
            ),
            "operator_fee_scalar": self.operator_fee_scalar,
            "operator_fee_constant": self.operator_fee_constant,
            "operator_fee_per_gas_wei": self.operator_fee_per_gas_wei,
            "source": MANTLE_FEE_DOC,
        }


# ---------------------------------------------------------------------------
# receipt / parameter evidence (online capture, offline check)
# ---------------------------------------------------------------------------


def _hex_int(value: Any) -> int:
    return int(str(value), 16)


def capture_fee_evidence(
    transport: Any,
    config: CostConfig,
    sample_set: SampleSet,
    chosen: Sequence[TxSample],
) -> dict[str, Any]:
    """Read receipts, sender balances and L1Block operator-fee parameters over the RPC
    for the `chosen` sampled transactions (the caller's deterministic selection). Every
    state read names an explicit block (never `latest`)."""
    receipts = []
    for dune in chosen:
        tx_hash = dune.tx_hash
        receipt = transport.call("eth_getTransactionReceipt", [tx_hash])
        tx = transport.call("eth_getTransactionByHash", [tx_hash])
        block_number = _hex_int(receipt["blockNumber"])
        block = transport.call("eth_getBlockByNumber", [hex(block_number), True])
        sender = str(receipt["from"]).lower()
        same_sender = [t["hash"] for t in block["transactions"] if t["from"].lower() == sender]
        before = transport.call("eth_getBalance", [sender, hex(block_number - 1)])
        after = transport.call("eth_getBalance", [sender, hex(block_number)])
        receipts.append(
            {
                "tx_hash": tx_hash,
                "key": dune.key,
                "dune": {
                    "gas_used": dune.gas_used,
                    "gas_price": str(dune.gas_price_wei),
                    "l1_fee": str(dune.l1_fee_wei),
                    "gasfees_tx_fee_raw": (
                        None if dune.gasfees_tx_fee_raw is None else str(dune.gasfees_tx_fee_raw)
                    ),
                },
                "receipt": {
                    k: receipt.get(k)
                    for k in (
                        "blockNumber",
                        "blockHash",
                        "status",
                        "gasUsed",
                        "effectiveGasPrice",
                        "l1Fee",
                        "operatorFeeScalar",
                        "operatorFeeConstant",
                        "to",
                    )
                },
                "tx_value": tx["value"],
                "block_base_fee": block.get("baseFeePerGas"),
                "sender_txs_in_block": len(same_sender),
                "sender_balance_before": before,
                "sender_balance_after": after,
            }
        )
    first = transport.call("eth_getBlockByNumber", [hex(config.block_number), False])
    if str(first["hash"]).lower() != config.block_hash:
        raise CostEvidenceError("snapshot block hash differs from the calibration config")
    blocks_per_second = 0.5  # Mantle: 2-second blocks (checked below against headers)
    span = int((config.window_end_ts - config.window_start_ts) * blocks_per_second)
    checkpoints = []
    for i in range(config.param_checkpoints):
        number = config.block_number - span + (span * i) // (config.param_checkpoints - 1)
        header = transport.call("eth_getBlockByNumber", [hex(number), False])
        scalar = transport.call(
            "eth_call", [{"to": L1_BLOCK, "data": SEL_OPERATOR_FEE_SCALAR}, hex(number)]
        )
        constant = transport.call(
            "eth_call", [{"to": L1_BLOCK, "data": SEL_OPERATOR_FEE_CONSTANT}, hex(number)]
        )
        checkpoints.append(
            {
                "block_number": number,
                "block_hash": header["hash"],
                "timestamp": _hex_int(header["timestamp"]),
                "base_fee_per_gas": header.get("baseFeePerGas"),
                "operator_fee_scalar": scalar,
                "operator_fee_constant": constant,
            }
        )
    return {
        "schema": EVIDENCE_SCHEMA,
        "config_sha256": config.sha256,
        "samples_export_sha256": sample_set.samples.sha256,
        "rpc_url": config.rpc_url,
        "fee_doc": MANTLE_FEE_DOC,
        "receipts": receipts,
        "param_checkpoints": checkpoints,
    }


def _balance_explained(rec: Mapping[str, Any], total: int) -> bool:
    before = _hex_int(rec["sender_balance_before"])
    after = _hex_int(rec["sender_balance_after"])
    return before - after == total + _hex_int(rec["tx_value"])


def check_fee_evidence(
    evidence: Mapping[str, Any], config: CostConfig
) -> tuple[FeeRules, dict[str, Any]]:
    """Offline re-verification of captured fee evidence. Returns the window's fee rules
    and a summary. Raises unless every receipt matches its Dune fields, the operator-fee
    parameters are constant over the checkpoints and inside the window, and at least one
    sender balance drop equals the recomputed total exactly."""
    _require_keys(
        evidence,
        {
            "schema",
            "config_sha256",
            "samples_export_sha256",
            "rpc_url",
            "fee_doc",
            "receipts",
            "param_checkpoints",
        },
        set(),
        "fee evidence",
    )
    if evidence["schema"] != EVIDENCE_SCHEMA:
        raise CostEvidenceError(f"fee evidence: schema {evidence['schema']!r}")
    checkpoints = evidence["param_checkpoints"]
    params = {
        (_hex_int(c["operator_fee_scalar"]), _hex_int(c["operator_fee_constant"]))
        for c in checkpoints
    }
    if len(params) != 1:
        raise CostEvidenceError(f"operator-fee parameters changed within the window: {params}")
    for c in checkpoints:
        if not config.window_start_ts - 60 <= c["timestamp"] <= config.block_timestamp:
            raise CostEvidenceError(f"checkpoint {c['block_number']}: outside the window")
    scalar, constant = params.pop()
    rules = FeeRules(operator_fee_scalar=scalar, operator_fee_constant=constant)
    explained: list[str] = []
    gasfees_short_by_operator = 0
    for rec in evidence["receipts"]:
        receipt, dune = rec["receipt"], rec["dune"]
        where = f"receipt {rec['tx_hash']}"
        if receipt["status"] != "0x1":
            raise CostEvidenceError(f"{where}: not a successful transaction")
        gas_used = _hex_int(receipt["gasUsed"])
        price = _hex_int(receipt["effectiveGasPrice"])
        l1_fee = _hex_int(receipt["l1Fee"])
        if (dune["gas_used"], int(dune["gas_price"]), int(dune["l1_fee"])) != (
            gas_used,
            price,
            l1_fee,
        ):
            raise CostEvidenceError(f"{where}: Dune fee fields differ from the receipt")
        if (_hex_int(receipt["operatorFeeScalar"]), _hex_int(receipt["operatorFeeConstant"])) != (
            scalar,
            constant,
        ):
            raise CostEvidenceError(f"{where}: receipt operator-fee parameters differ")
        total = rules.total_fee_wei(gas_used, price, l1_fee)
        if dune["gasfees_tx_fee_raw"] is not None:
            gasfees = int(dune["gasfees_tx_fee_raw"])
            if gasfees != gas_used * price + l1_fee:
                raise CostEvidenceError(f"{where}: gas.fees is not execution + l1 fee")
            if gasfees + rules.operator_fee_wei(gas_used) == total:
                gasfees_short_by_operator += 1
        if rec["sender_txs_in_block"] == 1 and _balance_explained(rec, total):
            explained.append(str(rec["tx_hash"]))
    if not explained:
        raise CostEvidenceError("no receipt's sender balance drop equals the recomputed total")
    return rules, {
        "receipts": len(evidence["receipts"]),
        "balance_drop_equals_total": len(explained),
        "explained_tx_hashes": explained,
        "gasfees_short_by_exactly_the_operator_fee": gasfees_short_by_operator,
        "param_checkpoints": len(checkpoints),
        "base_fee_per_gas_observed": sorted(
            {_hex_int(c["base_fee_per_gas"]) for c in checkpoints}
            | {_hex_int(r["block_base_fee"]) for r in evidence["receipts"]}
        ),
        "operator_fee_scalar": scalar,
        "operator_fee_constant": constant,
    }


def load_fee_evidence(path: str | Path) -> tuple[dict[str, Any], str]:
    data = Path(path).read_bytes()
    return json.loads(data), hashlib.sha256(data).hexdigest()

"""WHI-1436 / I11: the frozen five-source corpus.

1. **Definition + Dune provenance.** `config/corpus.yaml` fixes one block and an absolute
   historical window ending no later than it. The three checked-in SQL files are exactly
   what the config (and the upstream exports) generate, each canonical export names the
   hash of the SQL it came from, and a window reaching past the snapshot is refused.
2. **Deterministic selection.** Planning twice from the checked-in exports gives the same
   plan; the five generated prepare configs are byte-identical to `config/corpus/prepare/`
   (the configs every collector ran with); token identity is the address, never a
   symbol; every empirical case is `leg_derived` (no reconstructed orders); the
   tuning/report split is seeded, per cell and disjoint; every case fits the envelope.
3. **Frozen price schema** (`snapshot/prices.py`): unknown prices are `missing`, never
   zero; a price after the snapshot, stale beyond the bound, or zero is refused.
4. **The corpus fixture** (`tests/fixtures/corpus/bundle`, a representative subset of the
   full bundle, same block and unchanged pool records): all five sources represented at
   one block identity, every pool covers the envelope in both directions, the declared
   cohorts, no-direct-pool and boundary cases; tampered, mixed-block, unlisted-file and
   malformed-corpus bundles fail; `validate` and `run` replay offline with sockets
   disabled. If the full bundle is present under `data/`, its hash and checks are verified
   too.
5. **Token semantics** (`token_transfers.jsonl`, fork evidence at the corpus block): every
   universe token transfers exactly and keeps its supply.
"""

from __future__ import annotations

import copy
import json
import shutil
import socket
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

import main
from snapshot import corpus as corpus_mod
from snapshot.bundle import (
    CORPUS_FILE,
    MANIFEST_FILE,
    POOLS_FILE,
    PRICES_FILE,
    BundleError,
    load_bundle,
    sha256_bytes,
)
from snapshot.config import load_catalog
from snapshot.corpus import (
    CorpusError,
    load_corpus_config,
    load_export,
    parse_corpus_config,
    run_plan,
    validate_corpus_bundle,
)
from snapshot.prices import PriceError, parse_price_context

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "config" / "corpus.yaml"
EXPORTS = REPO / "tests" / "fixtures" / "corpus" / "dune"
PREPARE_DIR = REPO / "config" / "corpus" / "prepare"
FIXTURE = REPO / "tests" / "fixtures" / "corpus" / "bundle"
TOKEN_EVIDENCE = REPO / "tests" / "fixtures" / "corpus" / "token_transfers.jsonl"
FULL_BUNDLE = REPO / "data" / "corpus" / "mantle-5src-101082044" / "bundle"
SOURCES = {"agni_v3", "fusionx_v3", "uniswap_v3", "moe_classic_v1", "moe_lb_v2_2"}


@pytest.fixture(scope="module")
def config() -> corpus_mod.CorpusConfig:
    return load_corpus_config(CONFIG)


@pytest.fixture(scope="module")
def plan(config: corpus_mod.CorpusConfig) -> dict[str, Any]:
    return run_plan(config, EXPORTS, load_catalog(REPO / "config" / "protocols.yaml"))


# ---------------------------------------------------------------------------
# 1. definition + Dune provenance
# ---------------------------------------------------------------------------


def test_one_block_and_a_window_ending_at_the_snapshot(config: corpus_mod.CorpusConfig) -> None:
    assert (config.chain_id, config.block_number) == (5000, 101082044)
    assert config.block_hash == (
        "0x091b0759c9d3031f30658cdfa8bf4cd5ed311ece986e3c91eb1eeb121b2b65c4"
    )
    assert corpus_mod._utc_seconds(config.window_end, "end") == config.block_timestamp
    assert config.source_keys == (
        "agni_v3",
        "fusionx_v3",
        "uniswap_v3",
        "moe_classic_v1",
        "moe_lb_v2_2",
    )


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda r: r["dune"].update(window_end="2026-09-25 00:00:01"), "ends no later"),
        (lambda r: r["dune"].update(window_start="2026-09-26 00:00:00"), "window_start <"),
        (lambda r: r["sources"].pop(), "exactly the five"),
        (lambda r: r["selection"]["strata"].update(low=[0.5, 0.4]), "ordered"),
        (lambda r: r.update(extra=1), "unknown key"),
        (lambda r: r["selection"].update(tuning_per_cell=4), "outside"),
    ],
)
def test_invalid_corpus_definitions_are_refused(mutate: Any, match: str) -> None:
    raw = yaml.safe_load(CONFIG.read_text())
    mutate(raw)
    with pytest.raises(CorpusError, match=match):
        parse_corpus_config(raw, source_path="t", sha256="0" * 64)


def test_saved_sql_is_exactly_the_generated_sql_and_exports_name_it(
    config: corpus_mod.CorpusConfig,
) -> None:
    corpus_mod.check_sql_files(config, EXPORTS)  # raises on any drift
    for name, (sql_name, export_name) in corpus_mod.EXPORT_FILES.items():
        sql = (EXPORTS / sql_name).read_text()
        export = load_export(EXPORTS / export_name)
        assert export.name == name and export.query_id > 0 and export.execution_id
        assert export.sql_sha256 == sha256_bytes(sql.encode())
        # Every scan is bounded: chain, partition and absolute-window filters.
        if name != "prices":
            assert "blockchain = 'mantle'" in sql and "block_month IN" in sql
            assert f"block_time < TIMESTAMP '{config.window_end}'" in sql
        else:
            assert f"from_unixtime({config.block_timestamp - 60})" in sql


def test_ingest_refuses_sql_drift_and_partial_exports(tmp_path: Path) -> None:
    sql = EXPORTS / "q3_prices.sql"
    export = load_export(EXPORTS / "q3_prices.jsonl")
    doc: dict[str, Any] = {
        "query": {"query_id": export.query_id, "query": sql.read_text()},
        "result_preview": {
            "executionId": export.execution_id,
            "state": "COMPLETED",
            "resultMetadata": {
                "columns": [{"name": c} for c in export.columns],
                "totalRowCount": len(export.rows),
            },
            "data": {"rows": list(export.rows)},
        },
    }
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(doc))
    out = corpus_mod.ingest_export("prices", raw, sql, tmp_path / "e.jsonl")
    assert out.sha256 == export.sha256  # canonical: the same rows give the same identity
    doc["query"]["query"] = sql.read_text() + "-- edited\n"
    raw.write_text(json.dumps(doc))
    with pytest.raises(CorpusError, match="differs"):
        corpus_mod.ingest_export("prices", raw, sql, tmp_path / "e2.jsonl")
    doc["query"]["query"] = sql.read_text()
    doc["result_preview"]["resultMetadata"]["totalRowCount"] = 99
    raw.write_text(json.dumps(doc))
    with pytest.raises(CorpusError, match="partial"):
        corpus_mod.ingest_export("prices", raw, sql, tmp_path / "e3.jsonl")


# ---------------------------------------------------------------------------
# 2. deterministic selection
# ---------------------------------------------------------------------------


def test_plan_is_deterministic_and_matches_the_collected_prepare_configs(
    config: corpus_mod.CorpusConfig, plan: dict[str, Any]
) -> None:
    again = run_plan(config, EXPORTS, load_catalog(REPO / "config" / "protocols.yaml"))
    assert json.dumps(plan, sort_keys=True) == json.dumps(again, sort_keys=True)
    for collector, doc in plan["prepare_configs"].items():
        text = corpus_mod.prepare_config_text(config, collector, doc)
        assert (PREPARE_DIR / f"{collector}.yaml").read_text() == text, collector


def test_universe_is_the_top_activity_pairs_by_address(plan: dict[str, Any]) -> None:
    universe = plan["universe"]
    pairs = universe["activity_pairs"]
    assert len(pairs) == 12
    txs = [p["distinct_txs"] for p in pairs]
    assert txs == sorted(txs, reverse=True) and min(txs) >= 100
    tokens = {t for p in pairs for t in (p["token_a"], p["token_b"])}
    assert sorted(tokens) == universe["tokens"]
    assert all(t == t.lower() and len(t) == 42 for t in tokens)
    # USDT and USDT0 are different tokens (different addresses), never merged.
    assert "0x201eba5cc46d216ce6dc03f6a759e8e766e956ae" in tokens
    assert "0x779ded0c9e1022225f8e0630b35a9b54be713736" in tokens


def test_symbol_collisions_never_merge_tokens() -> None:
    prices = {
        "0x" + "11" * 20: {"symbol": "USD"},
        "0x" + "22" * 20: {"symbol": "USD"},
        "0x" + "33" * 20: {"symbol": None},
    }
    labels = corpus_mod.token_labels(prices)
    assert len(set(labels.values())) == 3
    assert labels["0x" + "22" * 20] == "USD_222222"


def test_empirical_cases_are_leg_derived_stratified_and_split(
    config: corpus_mod.CorpusConfig, plan: dict[str, Any]
) -> None:
    cases = plan["empirical_cases"]
    assert len(cases) == 12 * 2 * 3 * config.samples_per_stratum
    cells: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for c in cases:
        assert c["origin"] == "leg_derived"
        leg = c["selection"]["leg"]
        assert leg["tx_hash"].startswith("0x") and isinstance(leg["evt_index"], int)
        sel = c["selection"]
        assert int(sel["stratum_min_raw"]) <= int(c["amount_in"]) <= int(sel["stratum_max_raw"])
        lo, hi = config.strata[c["stratum"]]
        assert lo * sel["unit_legs"] <= sel["amount_rank"] - 1 < hi * sel["unit_legs"]
        cells.setdefault((c["token_in"], c["token_out"], c["stratum"]), []).append(c)
    for cell in cells.values():
        assert len(cell) == config.samples_per_stratum
        assert sum(c["split"] == "tuning" for c in cell) == config.tuning_per_cell
        # one leg per transaction within a stratum sample
        assert len({c["selection"]["leg"]["tx_hash"] for c in cell}) == len(cell)
    assert len({c["case_id"] for c in cases}) == len(cases)


def test_every_case_fits_the_envelope(plan: dict[str, Any]) -> None:
    envelope = {t: int(a) for t, a in plan["envelope"]["amount_in"].items()}
    prices = parse_price_context(plan["prices"])
    usd = Decimal(plan["envelope"]["envelope_usd"])
    for token, amount in envelope.items():
        assert prices.usd_value(token, amount) >= usd
        assert prices.usd_value(token, amount - 1) < usd
    for c in plan["empirical_cases"]:
        assert int(c["amount_in"]) * 2 <= envelope[c["token_in"]]
    for token, samples in plan["token_samples"].items():
        for s in samples:
            assert int(s["amount_in"]) * 2 <= envelope[token]


# ---------------------------------------------------------------------------
# 3. frozen price schema
# ---------------------------------------------------------------------------


def _price_doc(plan: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(plan["prices"])


def test_price_context_is_frozen_with_units_timestamps_and_sources(plan: dict[str, Any]) -> None:
    prices = parse_price_context(
        plan["prices"], block=(101082044, plan["block"]["hash"], 1790294400)
    )
    assert prices.quote_currency == "USD"
    assert prices.source["query_id"] == load_export(EXPORTS / "q3_prices.jsonl").query_id
    for entry in prices.tokens.values():
        assert entry.status == "ok" and entry.price is not None and entry.price > 0
        assert entry.timestamp is not None and entry.timestamp + 60 <= prices.block_timestamp
    assert prices.native["via_token"] == "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"


def test_unknown_price_is_missing_never_zero(plan: dict[str, Any]) -> None:
    doc = _price_doc(plan)
    token = sorted(doc["tokens"])[0]
    doc["tokens"][token].update(status="missing", price=None, timestamp=None, origin=None)
    prices = parse_price_context(doc)
    with pytest.raises(PriceError, match="no frozen price"):
        prices.usd_value(token, 10**6)
    doc["tokens"][token].update(status="ok", price="0", timestamp=1790294340, origin="x")
    with pytest.raises(PriceError, match="never zero"):
        parse_price_context(doc)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("timestamp", 1790294401, "after the snapshot"),
        ("timestamp", 1790294400 - 3601, "staleness"),
        ("price", "-1", "positive"),
        ("price", "abc", "not a decimal"),
        ("status", "guess", "status"),
    ],
)
def test_bad_price_entries_are_refused(
    plan: dict[str, Any], field: str, value: Any, match: str
) -> None:
    doc = _price_doc(plan)
    doc["tokens"][sorted(doc["tokens"])[0]][field] = value
    with pytest.raises(PriceError, match=match):
        parse_price_context(doc)


def test_price_context_of_another_block_is_refused(plan: dict[str, Any]) -> None:
    with pytest.raises(PriceError, match="not the bundle block"):
        parse_price_context(plan["prices"], block=(1, plan["block"]["hash"], 1790294400))


# ---------------------------------------------------------------------------
# 4. the corpus fixture (and the full bundle when present)
# ---------------------------------------------------------------------------


def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network access attempted during an offline corpus replay")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)


@pytest.fixture(scope="module")
def fixture_bundle() -> Any:
    return load_bundle(FIXTURE)


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "bundle"
    shutil.copytree(FIXTURE, target)
    return target


def _rewrite(bundle_dir: Path, name: str, mutate: Any) -> None:
    """Mutate one JSON file and re-seal the manifest checksums, so only the semantic
    check under test can reject the bundle."""
    path = bundle_dir / name
    doc = json.loads(path.read_text())
    mutate(doc)
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    path.write_text(text)
    manifest = json.loads((bundle_dir / MANIFEST_FILE).read_text())
    manifest["checksums"][name] = sha256_bytes(text.encode())
    (bundle_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def test_fixture_represents_all_five_sources_at_one_block(fixture_bundle: Any) -> None:
    assert fixture_bundle.block.number == 101082044
    assert {p.source_key for p in fixture_bundle.pools.values()} == SOURCES
    pools_doc = json.loads((FIXTURE / POOLS_FILE).read_text())["pools"]
    for record in pools_doc:
        assert record["read_at"] == {
            "block_number": 101082044,
            "block_hash": fixture_bundle.block.hash,
        }
    assert fixture_bundle.corpus["subset_of"]["bundle_id"].startswith("mantle-5src-101082044")
    assert fixture_bundle.corpus["origin"]["reconstructed_orders"] is False


def test_fixture_covers_the_envelope_and_declares_cohorts(fixture_bundle: Any) -> None:
    summary = validate_corpus_bundle(fixture_bundle)  # quotes every pool at the envelope
    assert summary["sources"] == sorted(SOURCES)
    corpus = fixture_bundle.corpus
    groups = {m["group"] for m in corpus["case_metadata"].values()}
    assert groups == {"activity_pair", "no_direct_pool_pair", "boundary"}
    assert set(corpus["splits"]["counts"]) == {"tuning", "report"}
    sor = corpus["cohorts"]["sor_compatible"]
    assert "moe_lb_v2_2" not in sor["sources"] and len(sor["sources"]) == 4
    lb = {pid for pid, p in fixture_bundle.pools.items() if p.source_key == "moe_lb_v2_2"}
    assert lb and not lb & set(sor["pools"])
    for case in fixture_bundle.cases:
        m = corpus["case_metadata"][case.case_id]
        if m["group"] == "no_direct_pool_pair":
            assert m["direct_pools"] == 0 and not fixture_bundle.pools_for_pair(
                case.token_in, case.token_out
            )
        if m["group"] == "boundary":
            assert m["origin"] == "state_derived_boundary" and m["split"] == "report"
        else:
            assert m["origin"] == "leg_derived"


def test_boundary_cases_sit_on_the_pool_boundaries(fixture_bundle: Any) -> None:
    from pools.quote import quote_exact_in

    corpus = fixture_bundle.corpus
    for case in fixture_bundle.cases:
        m = corpus["case_metadata"][case.case_id]
        if m["group"] != "boundary" or m["selection"]["pool_id"] not in fixture_bundle.pools:
            continue
        pool = fixture_bundle.pools[m["selection"]["pool_id"]]
        r = quote_exact_in(pool, case.token_in, case.amount_in)
        kind = m["selection"]["kind"]
        if kind == "round_at":
            assert r.status.value == "ok" and r.amount_out > 0
        elif kind == "round_below":
            assert not (r.status.value == "ok" and r.amount_out > 0)
        elif kind == "liq_at":
            assert r.status.value == "ok"
        elif kind == "liq_above":
            assert r.status.value == "insufficient_liquidity"


@pytest.mark.parametrize(
    ("mutate_file", "mutate", "match"),
    [
        (
            POOLS_FILE,
            lambda d: d["pools"][0]["read_at"].update(block_number=101082043),
            "never mixes blocks",
        ),
        (PRICES_FILE, lambda d: d["tokens"][sorted(d["tokens"])[0]].update(price="0"), "zero"),
        (
            PRICES_FILE,
            lambda d: d["tokens"][sorted(d["tokens"])[0]].update(timestamp=1790294460),
            "after the snapshot",
        ),
        (CORPUS_FILE, lambda d: d["case_metadata"].popitem(), "exactly the bundle's cases"),
        (
            CORPUS_FILE,
            lambda d: d["exclusions"]["pools"].append(
                {"pool_id": "0x" + "00" * 20, "rule": "ad-hoc", "reason": "because"}
            ),
            "declared rule",
        ),
        (CORPUS_FILE, lambda d: d["origin"].update(reconstructed_orders=True), "leg-derived"),
        (CORPUS_FILE, lambda d: d["cohorts"]["sor_compatible"]["pools"].pop(), "disagree"),
        (
            CORPUS_FILE,
            lambda d: d["envelope"]["amount_in"].update(
                {k: "1" for k in d["envelope"]["amount_in"]}
            ),
            "exceeds the envelope",
        ),
        (CORPUS_FILE, lambda d: d["block"].update(number=1), "bundle block"),
    ],
)
def test_malformed_corpus_bundles_fail(
    tmp_path: Path, mutate_file: str, mutate: Any, match: str
) -> None:
    bundle_dir = _copy(tmp_path)
    _rewrite(bundle_dir, mutate_file, mutate)
    with pytest.raises(BundleError, match=match):
        load_bundle(bundle_dir)


def test_tampered_or_unlisted_files_fail(tmp_path: Path) -> None:
    bundle_dir = _copy(tmp_path)
    path = bundle_dir / PRICES_FILE
    path.write_text(path.read_text().replace('"USD"', '"EUR"', 1))
    with pytest.raises(BundleError, match="bad checksum"):
        load_bundle(bundle_dir)
    bundle_dir = _copy(tmp_path / "second")
    (bundle_dir / "notes.txt").write_text("not covered by the manifest")
    with pytest.raises(BundleError, match="not covered by the manifest"):
        load_bundle(bundle_dir)


def test_envelope_check_detects_state_that_no_longer_covers_it(tmp_path: Path) -> None:
    bundle_dir = _copy(tmp_path)

    def truncate(doc: dict[str, Any]) -> None:
        for record in doc["pools"]:
            if record.get("family") == "concentrated" and record["ticks"]:
                lo, hi = record["bitmap_word_range"]
                current = (record["tick"] // record["tick_spacing"]) >> 8
                record["bitmap_word_range"] = [current, current]
                record["tick_bitmap"] = [w for w in record["tick_bitmap"] if w["word"] == current]
                keep = {w["word"] for w in record["tick_bitmap"]}
                record["ticks"] = [
                    t
                    for t in record["ticks"]
                    if ((t["tick"] // record["tick_spacing"]) >> 8) in keep
                ]
                return

    _rewrite(bundle_dir, POOLS_FILE, truncate)
    bundle = load_bundle(bundle_dir)  # structurally still a valid bundle ...
    with pytest.raises(CorpusError, match="incomplete_snapshot|differ"):
        validate_corpus_bundle(bundle)  # ... but no longer covers the envelope


def test_validate_and_run_replay_the_fixture_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _block_network(monkeypatch)
    for var in ("DUNE_API_KEY", "LINEAR_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert main.main(["validate", "--bundle", str(FIXTURE)]) == 0
    results = tmp_path / "results"
    argv = ["run", "--bundle", str(FIXTURE), "--profile", str(REPO / "config" / "smoke.yaml")]
    assert main.main([*argv, "--results-dir", str(results)]) == 0
    (run_dir,) = results.iterdir()
    records = [json.loads(line) for line in (run_dir / "cases.jsonl").read_text().splitlines()]
    bundle = load_bundle(FIXTURE)
    assert bundle.corpus is not None
    assert [r["case_id"] for r in records] == [c.case_id for c in bundle.cases]
    by_group: dict[str, set[str]] = {}
    for r in records:
        group = bundle.corpus["case_metadata"][r["case_id"]]["group"]
        by_group.setdefault(group, set()).add(r["status"])
    assert "ok" in by_group["activity_pair"]
    assert by_group["no_direct_pool_pair"] <= {"no_route", "unsupported"}
    assert all(r["status"] != "incomplete_snapshot" for r in records)


def test_full_bundle_hash_matches_the_recorded_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    record = json.loads((REPO / "tests" / "fixtures" / "corpus" / "full_bundle.json").read_text())
    fixture = load_bundle(FIXTURE)
    assert fixture.corpus is not None
    assert fixture.corpus["subset_of"] == {
        "bundle_id": record["bundle_id"],
        "bundle_hash": record["bundle_hash"],
    }
    if not FULL_BUNDLE.is_dir():
        pytest.skip("full corpus bundle not prepared locally (data/ is gitignored)")
    _block_network(monkeypatch)
    bundle = load_bundle(FULL_BUNDLE)
    assert bundle.bundle_hash == record["bundle_hash"]
    manifest = json.loads((FULL_BUNDLE / MANIFEST_FILE).read_text())
    assert manifest["checksums"] == record["checksums"]
    summary = validate_corpus_bundle(bundle)
    assert summary["sources"] == sorted(SOURCES)
    assert summary["cases"] == record["cases"] and summary["pools"] == record["pools"]
    # The fixture's pool records are byte-for-byte records of the full bundle.
    full = {p["pool_id"]: p for p in json.loads((FULL_BUNDLE / POOLS_FILE).read_text())["pools"]}
    for p in json.loads((FIXTURE / POOLS_FILE).read_text())["pools"]:
        assert full[p["pool_id"]] == p


# ---------------------------------------------------------------------------
# 5. token semantics at the corpus block (fork evidence)
# ---------------------------------------------------------------------------


def test_every_universe_token_transfers_exactly(plan: dict[str, Any]) -> None:
    lines = [json.loads(line) for line in TOKEN_EVIDENCE.read_text().splitlines()]
    meta, rows, end = lines[0], lines[1:-1], lines[-1]
    assert meta["kind"] == "meta" and end == {"kind": "end"}
    assert (meta["chain_id"], meta["block_number"], meta["block_hash"]) == (
        5000,
        101082044,
        plan["block"]["hash"],
    )
    assert sorted(r["token"].lower() for r in rows) == plan["universe"]["tokens"]
    for r in rows:
        assert int(r["amount"]) > 0
        assert r["holder_sent"] == r["probe_received"] == r["amount"], r
        assert r["probe_sent"] == r["holder_received"] == r["back_amount"] == r["amount"], r
        assert r["supply_before"] == r["supply_after"], r
        assert r["decimals"] == plan["prices"]["tokens"][r["token"].lower()]["decimals"]


def test_prepare_configs_carry_the_single_rule_and_no_ad_hoc_exclusions(
    config: corpus_mod.CorpusConfig,
) -> None:
    for path in sorted(PREPARE_DIR.glob("*.yaml")):
        text = path.read_text()
        assert "excluded_fee_tiers" not in text and "excluded_bin_steps" not in text
        assert "excluded:" not in text
        doc = yaml.safe_load(text)
        assert doc["rpc"]["use_multicall3"] is True
        if "collection" in doc:
            assert doc["collection"]["exclusion_rule"] == config.exclusion_rule_id


# ---------------------------------------------------------------------------
# 6. assembly refusals (and reproducibility when the source bundles are present)
# ---------------------------------------------------------------------------

SOURCE_BUNDLES = REPO / "data" / "corpus" / "mantle-5src-101082044" / "sources"


def test_a_source_bundle_at_another_block_is_refused(config: corpus_mod.CorpusConfig) -> None:
    other = load_bundle(REPO / "tests" / "fixtures" / "moe_classic" / "bundle")  # block 101057678
    with pytest.raises(CorpusError, match="never mixes blocks"):
        corpus_mod._check_block(other, config, "moe_classic")


def test_assembly_needs_all_five_sources(
    config: corpus_mod.CorpusConfig, plan: dict[str, Any], tmp_path: Path
) -> None:
    dirs = {s.collector: tmp_path / s.collector for s in config.sources if s.collector != "moe_lb"}
    with pytest.raises(CorpusError, match="exactly the five source bundles"):
        corpus_mod.assemble(
            config,
            plan,
            dirs,
            tmp_path / "out",
            catalog=load_catalog(REPO / "config" / "protocols.yaml"),
            prepare_dir=PREPARE_DIR,
        )


def test_assembly_is_reproducible_from_the_source_bundles(
    config: corpus_mod.CorpusConfig, plan: dict[str, Any], tmp_path: Path
) -> None:
    if not SOURCE_BUNDLES.is_dir():
        pytest.skip("per-source bundles not prepared locally (data/ is gitignored)")
    record = json.loads((REPO / "tests" / "fixtures" / "corpus" / "full_bundle.json").read_text())
    bundle = corpus_mod.assemble(
        config,
        plan,
        {s.collector: SOURCE_BUNDLES / s.collector for s in config.sources},
        tmp_path / "bundle",
        catalog=load_catalog(REPO / "config" / "protocols.yaml"),
        prepare_dir=PREPARE_DIR,
    )
    assert bundle.bundle_hash == record["bundle_hash"]


# ---------------------------------------------------------------------------
# 6. declared tuning/report cuts (WHI-1447)
# ---------------------------------------------------------------------------


def test_split_cut_keeps_every_pool_and_exactly_that_splits_cases(tmp_path: Path) -> None:
    from snapshot.corpus import split_bundle

    fixture = load_bundle(FIXTURE)
    assert fixture.corpus is not None
    meta = fixture.corpus["case_metadata"]
    for split in ("tuning", "report"):
        cut = split_bundle(fixture, split, tmp_path / split)
        assert cut.corpus is not None and cut.corpus["split"] == split
        assert sorted(cut.pools) == sorted(fixture.pools)
        assert [c.case_id for c in cut.cases] == [
            c.case_id for c in fixture.cases if meta[c.case_id]["split"] == split
        ]
        assert cut.corpus["splits"]["counts"][split] == len(cut.cases) > 0
        assert cut.corpus["subset_of"]["bundle_hash"] == fixture.bundle_hash
        validate_corpus_bundle(load_bundle(tmp_path / split))
        # Deterministic: the same cut has the same identity.
        again = split_bundle(fixture, split, tmp_path / f"{split}-again")
        assert again.bundle_hash == cut.bundle_hash
        with pytest.raises(CorpusError, match="already a"):
            split_bundle(cut, split, tmp_path / f"{split}-twice")
    with pytest.raises(CorpusError, match="unknown split"):
        split_bundle(fixture, "holdout", tmp_path / "bad")


def test_split_cut_refuses_a_case_of_the_other_split(tmp_path: Path) -> None:
    from snapshot.corpus import split_bundle

    split_bundle(load_bundle(FIXTURE), "report", tmp_path / "cut")

    def mutate(doc: dict[str, Any]) -> None:
        doc["split"] = "tuning"

    _rewrite(tmp_path / "cut", "corpus.json", mutate)
    with pytest.raises(BundleError, match="outside split"):
        load_bundle(tmp_path / "cut")


def test_cli_split_cuts_a_cohort_bundle_and_keeps_its_cohort(tmp_path: Path) -> None:
    cohort = tmp_path / "cohort"
    assert main.main(["corpus", "cohort", "--bundle", str(FIXTURE), "--output", str(cohort)]) == 0
    out = tmp_path / "cohort-report"
    argv = ["corpus", "split", "--bundle", str(cohort), "--split", "report"]
    assert main.main([*argv, "--output", str(out)]) == 0
    cut = load_bundle(out)
    assert cut.corpus is not None
    assert cut.corpus["cohort"] == "sor_compatible" and cut.corpus["split"] == "report"
    assert "moe_lb_v2_2" not in {getattr(p, "source_key", None) for p in cut.pools.values()}


def test_acceptance_split_bundles_reproduce_their_recorded_hashes(tmp_path: Path) -> None:
    """The WHI-1447 acceptance bundles are cut deterministically from the frozen corpus;
    when it is present locally, re-cutting reproduces the recorded identities."""
    from snapshot.corpus import sor_cohort_bundle, split_bundle

    record_path = REPO / "docs" / "references" / "v1-acceptance" / "bundles.json"
    record = json.loads(record_path.read_text())
    full_record = json.loads(
        (REPO / "tests" / "fixtures" / "corpus" / "full_bundle.json").read_text()
    )
    assert record["source"]["bundle_hash"] == full_record["bundle_hash"]
    if not FULL_BUNDLE.is_dir():
        pytest.skip("full corpus bundle not prepared locally (data/ is gitignored)")
    full = load_bundle(FULL_BUNDLE)
    cohort = sor_cohort_bundle(full, tmp_path / "sor_cohort")
    assert cohort.bundle_hash == record["bundles"]["sor_cohort"]["bundle_hash"]
    for name, (parent, split) in {
        "bundle_tuning": (full, "tuning"),
        "bundle_report": (full, "report"),
        "sor_cohort_tuning": (cohort, "tuning"),
        "sor_cohort_report": (cohort, "report"),
    }.items():
        cut = split_bundle(parent, split, tmp_path / name)
        assert cut.bundle_hash == record["bundles"][name]["bundle_hash"], name
        assert len(cut.cases) == record["bundles"][name]["cases"]
        assert len(cut.pools) == record["bundles"][name]["pools"]

"""WHI-1429: Agni v3 fixed-block snapshot -> offline direct replay.

Three evidence seams, all offline:

1. **Fixed-block fork evidence.** `tests/fixtures/agni/bundle/` is the bundle published
   by `main.py prepare --source agni --block 101057678` (the catalog's preflight-verified
   candidate block). `tests/fixtures/agni/evidence.jsonl` was produced independently by
   `tools/cl_evidence/test/CaptureAgniReplay.t.sol`, executing every (case x pool)
   request with the *deployed* AgniPool bytecode on a Mantle fork at that block
   (`tools/cl_evidence/replay_agni.sh`). The `direct` result, every candidate quote, the
   next state and a follow-up swap through that next state must equal it exactly.
2. **Collector mechanics** against `FakeChain`, a scripted JSON-RPC node serving the
   fixture's state: block identity pinning, reorg abort, finality, envelope-driven tick
   recovery bounds, code/identity admission, deterministic atomic publication.
3. **Offline replay**: `validate`/`run` over the saved bundle with sockets disabled.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest
from cl_chain import FakeChain, addr_int

import main
from benchmark.objective import gross_only
from pools.concentrated import quote_exact_in
from pools.result import QuoteStatus
from routing.algorithms import direct
from routing.algorithms.base import Budget, SolveContext, SolveStatus
from snapshot import abi
from snapshot.bundle import (
    CASES_FILE,
    MANIFEST_FILE,
    POOLS_FILE,
    PROVENANCE_FILE,
    BundleError,
    load_bundle,
    sha256_bytes,
)
from snapshot.collectors import COLLECTORS, PrepareError, PrepareRequest
from snapshot.collectors.concentrated import ConcentratedCollector, publish
from snapshot.config import ContractRef, ProtocolCatalog, load_catalog
from snapshot.models import ConcentratedPoolState, SnapshotBundle
from snapshot.prepare_config import (
    ClPrepareConfig,
    CollectionLimits,
    PrepareConfigError,
    load_prepare_config,
    parse_prepare_config,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE_BUNDLE = REPO / "tests" / "fixtures" / "agni" / "bundle"
EVIDENCE = REPO / "tests" / "fixtures" / "agni" / "evidence.jsonl"
PREPARE_CONFIG = REPO / "config" / "prepare" / "agni.yaml"
CATALOG = load_catalog(REPO / "config" / "protocols.yaml")
SMOKE_PROFILE = REPO / "config" / "smoke.yaml"
USDC = "0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9"
WMNT = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"
OBJECTIVE = gross_only()


def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted during an offline command")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)


@pytest.fixture(scope="module")
def bundle() -> SnapshotBundle:
    return load_bundle(FIXTURE_BUNDLE)


def _cl_pools(b: SnapshotBundle) -> dict[str, ConcentratedPoolState]:
    out = {k: v for k, v in b.pools.items() if isinstance(v, ConcentratedPoolState)}
    assert len(out) == len(b.pools)
    return out


# ---------------------------------------------------------------------------
# 1. Fixed-block fork evidence
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Request:
    case_id: str
    pool: str
    swap: dict[str, Any]
    changed_ticks: dict[int, dict[str, Any]]
    followup: dict[str, Any] | None = None


@dataclasses.dataclass
class Evidence:
    meta: dict[str, Any]
    pool_states: dict[str, dict[str, Any]]
    words: dict[str, dict[int, int]]
    ticks: dict[str, dict[int, dict[str, Any]]]
    requests: dict[tuple[str, str], Request]


def _load_evidence() -> Evidence:
    records = [json.loads(line) for line in EVIDENCE.read_text().splitlines() if line.strip()]
    assert records and records[-1] == {"kind": "end"}, "truncated evidence"
    meta = [r for r in records if r["kind"] == "meta"]
    assert len(meta) == 1
    pool_states = {r["pool"].lower(): r for r in records if r["kind"] == "pool_state"}
    words: dict[str, dict[int, int]] = {}
    ticks: dict[str, dict[int, dict[str, Any]]] = {}
    requests: dict[tuple[str, str], Request] = {}
    for r in records:
        kind = r["kind"]
        if kind == "pool_word":
            words.setdefault(r["pool"].lower(), {})[int(r["word"])] = int(r["bitmap"])
        elif kind == "pool_tick":
            ticks.setdefault(r["pool"].lower(), {})[int(r["tick"])] = r
        elif kind == "swap":
            key = (r["case_id"], r["pool"].lower())
            assert key not in requests, f"duplicate swap record for {key}"
            requests[key] = Request(key[0], key[1], r, {})
        elif kind == "post_tick":
            requests[(r["case_id"], r["pool"].lower())].changed_ticks[int(r["tick"])] = r
        elif kind == "followup":
            requests[(r["case_id"], r["pool"].lower())].followup = r
    return Evidence(meta[0], pool_states, words, ticks, requests)


@pytest.fixture(scope="module")
def evidence() -> Evidence:
    return _load_evidence()


SCALARS = (
    "sqrt_price_x96",
    "tick",
    "liquidity",
    "fee_protocol",
    "fee_growth_global0_x128",
    "fee_growth_global1_x128",
    "protocol_fees0",
    "protocol_fees1",
)


def _scalars(state: ConcentratedPoolState) -> dict[str, int]:
    return {k: int(getattr(state, k)) for k in SCALARS}


def _ev_scalars(rec: dict[str, Any]) -> dict[str, int]:
    return {k: int(rec[k]) for k in SCALARS}


def _tick_tuple(rec: dict[str, Any]) -> tuple[int, int, int, int]:
    assert rec["initialized"] is True
    return (
        int(rec["liquidity_gross"]),
        int(rec["liquidity_net"]),
        int(rec["fee_growth_outside0_x128"]),
        int(rec["fee_growth_outside1_x128"]),
    )


def test_fixture_is_the_published_agni_bundle_at_the_catalog_block(bundle: SnapshotBundle) -> None:
    cb = CATALOG.candidate_block
    assert bundle.kind == "real"
    assert (
        bundle.block.chain_id,
        bundle.block.number,
        bundle.block.hash,
        bundle.block.timestamp,
    ) == (
        CATALOG.network.chain_id,
        cb.number,
        cb.hash,
        cb.timestamp,
    )
    agni = CATALOG.source("agni_v3")
    assert agni.cl_collection is not None
    provenance = json.loads((FIXTURE_BUNDLE / PROVENANCE_FILE).read_text())
    assert provenance["source_key"] == "agni_v3"
    assert provenance["catalog"]["factory"]["code_hash"] == agni.contracts["factory"].code_hash
    example = agni.contracts["example_pool"]
    pools = _cl_pools(bundle)
    assert example.address.lower() in pools
    assert provenance["pools"][example.address.lower()]["code_hash"] == example.code_hash
    for pool_id, pool in pools.items():
        assert pool.source_key == "agni_v3"
        assert {pool.token0, pool.token1} == {USDC, WMNT}
        record = provenance["pools"][pool_id]
        assert record["normalized_code_hash"] == agni.cl_collection.pool_code_normalized_hash
        assert record["completeness"]["bitmap_word_range"] == list(pool.bitmap_word_range)
    # both directions are in the declared envelope
    directions = {c.token_in for c in bundle.cases}
    assert directions == {USDC, WMNT}
    # the explicitly excluded tier is recorded with its reason, not silently dropped
    omitted = provenance["discovery"]["omitted"]
    assert [o["fee"] for o in omitted] == [100]
    assert omitted[0]["reason"].startswith("excluded by prepare config:")


def test_evidence_is_bound_to_the_bundle_block(bundle: SnapshotBundle, evidence: Evidence) -> None:
    meta = evidence.meta
    assert meta["bundle_id"] == bundle.bundle_id
    assert (
        meta["chain_id"],
        meta["block_number"],
        meta["block_timestamp"],
        meta["block_hash"],
    ) == (
        bundle.block.chain_id,
        bundle.block.number,
        bundle.block.timestamp,
        bundle.block.hash,
    )


def test_collected_state_matches_fork_storage(bundle: SnapshotBundle, evidence: Evidence) -> None:
    """The collector's eth_call reads equal the fork's own storage reads: scalars, every
    bitmap word of the collected range and every initialized tick in it."""
    pools = _cl_pools(bundle)
    assert set(evidence.pool_states) == set(pools)
    for pool_id, pool in pools.items():
        ev = evidence.pool_states[pool_id]
        assert _ev_scalars(ev) == _scalars(pool)
        assert ev["unlocked"] is True
        assert (int(ev["fee"]), int(ev["tick_spacing"])) == (pool.fee, pool.tick_spacing)
        assert (ev["token0"].lower(), ev["token1"].lower()) == (pool.token0, pool.token1)
        assert ev["lm_pool"].lower() == (pool.lm_pool or "").lower()
        lo, hi = pool.bitmap_word_range
        assert sorted(evidence.words[pool_id]) == list(range(lo, hi + 1))
        for word, value in evidence.words[pool_id].items():
            assert pool.tick_bitmap.get(word, 0) == value, f"{pool_id} word {word}"
        # every initialized tick of the collected range, read independently by the fork
        assert set(evidence.ticks[pool_id]) == set(pool.ticks)
        for t, rec in evidence.ticks[pool_id].items():
            info = pool.ticks[t]
            assert _tick_tuple(rec) == (
                info.liquidity_gross,
                info.liquidity_net,
                info.fee_growth_outside0_x128,
                info.fee_growth_outside1_x128,
            ), f"{pool_id} tick {t}"


def test_every_quote_and_next_state_match_fork_evidence(
    bundle: SnapshotBundle, evidence: Evidence
) -> None:
    pools = _cl_pools(bundle)
    expected_keys = {
        (c.case_id, p.pool_id)
        for c in bundle.cases
        for p in bundle.pools_for_pair(c.token_in, c.token_out)
    }
    assert set(evidence.requests) == expected_keys
    crossings = 0
    for (case_id, pool_id), req in sorted(evidence.requests.items()):
        case = bundle.case(case_id)
        state = pools[pool_id]
        zero_for_one = case.token_in == state.token0
        assert req.swap["zero_for_one"] is zero_for_one
        assert int(req.swap["amount_specified"]) == case.amount_in
        a0, a1 = int(req.swap["amount0"]), int(req.swap["amount1"])
        consumed, produced = (a0, -a1) if zero_for_one else (a1, -a0)
        assert consumed == case.amount_in, "fixture requests must be full exact-input fills"

        result = quote_exact_in(state, case.token_in, case.amount_in)
        assert result.status is QuoteStatus.OK, result.detail
        assert result.amount_out == produced, f"{case_id} on {pool_id}"
        new_state = result.new_state
        assert new_state is not None
        assert _scalars(new_state) == _ev_scalars(req.swap)

        # the crossed ticks are exactly the ones the fork changed, with the same values;
        # every other tick is untouched
        crossed = set(req.changed_ticks)
        assert result.features["initialized_ticks_crossed"] == len(crossed)
        assert int(req.swap["traversed_initialized_ticks"]) >= len(crossed)
        for t, info in new_state.ticks.items():
            if t in crossed:
                assert _tick_tuple(req.changed_ticks[t]) == (
                    info.liquidity_gross,
                    info.liquidity_net,
                    info.fee_growth_outside0_x128,
                    info.fee_growth_outside1_x128,
                ), f"{case_id} on {pool_id}: tick {t}"
            else:
                assert info == state.ticks[t], f"{case_id} on {pool_id}: tick {t} changed"
        crossings += len(crossed)
        assert state.ticks == pools[pool_id].ticks  # input snapshot never mutated

        # a second swap through the updated pool (stateful reuse)
        fu = req.followup
        assert fu is not None
        back_in = state.token1 if zero_for_one else state.token0
        back = quote_exact_in(new_state, back_in, int(fu["amount_specified"]))
        assert back.status is QuoteStatus.OK, back.detail
        b0, b1 = int(fu["amount0"]), int(fu["amount1"])
        assert back.amount_out == (-b0 if zero_for_one else -b1)
        assert back.new_state is not None
        assert _scalars(back.new_state) == _ev_scalars(fu)
    assert crossings > 100


def test_direct_agrees_with_fixed_block_evidence_in_both_directions(
    bundle: SnapshotBundle, evidence: Evidence
) -> None:
    context = SolveContext(bundle=bundle, objective=OBJECTIVE)
    seen_directions: set[str] = set()
    for case in bundle.cases:
        solved = direct.solve(case, context, Budget())
        assert solved.status is SolveStatus.OK, solved.error
        assert solved.evaluation is not None and solved.plan is not None
        outputs = {}
        for (case_id, pool_id), req in evidence.requests.items():
            if case_id != case.case_id:
                continue
            a0, a1 = int(req.swap["amount0"]), int(req.swap["amount1"])
            outputs[pool_id] = -a1 if req.swap["zero_for_one"] else -a0
        best_pool = max(outputs, key=lambda p: outputs[p])
        assert solved.plan.steps[0].pool_id == best_pool
        assert solved.evaluation.gross_output == outputs[best_pool]
        assert solved.candidates_considered == len(outputs) == 3
        seen_directions.add(case.token_in)
    assert seen_directions == {USDC, WMNT}


def test_offline_cli_replay_of_the_saved_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: Evidence
) -> None:
    _block_network(monkeypatch)
    bundle_dir = tmp_path / "bundle"
    shutil.copytree(FIXTURE_BUNDLE, bundle_dir)
    assert main.main(["validate", "--bundle", str(bundle_dir)]) == 0
    results = tmp_path / "results"
    argv = ["run", "--bundle", str(bundle_dir), "--profile", str(SMOKE_PROFILE)]
    assert main.main([*argv, "--results-dir", str(results)]) == 0
    (run_dir,) = results.iterdir()
    records = [json.loads(line) for line in (run_dir / "cases.jsonl").read_text().splitlines()]
    assert len(records) == 6
    for rec in records:
        assert rec["status"] == "ok"
        pool_id = rec["evaluation"]["trace"][0]["pool_id"]
        req = evidence.requests[(rec["case_id"], pool_id)]
        a0, a1 = int(req.swap["amount0"]), int(req.swap["amount1"])
        assert int(rec["evaluation"]["gross_output"]) == (-a1 if req.swap["zero_for_one"] else -a0)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["bundle_hash"] == load_bundle(bundle_dir).bundle_hash


# ---------------------------------------------------------------------------
# Tick bounds: traversal beyond the collected range fails explicitly
# ---------------------------------------------------------------------------


def test_traversal_beyond_collected_tick_bounds_is_incomplete_snapshot(
    bundle: SnapshotBundle,
) -> None:
    pools = _cl_pools(bundle)
    example = CATALOG.source("agni_v3").contracts["example_pool"].address.lower()
    state = pools[example]
    too_large = 10 * max(c.amount_in for c in bundle.cases if c.token_in == USDC)
    before = (_scalars(state), dict(state.ticks), dict(state.tick_bitmap), state.bitmap_word_range)
    result = quote_exact_in(state, USDC, too_large)
    assert result.status is QuoteStatus.INCOMPLETE_SNAPSHOT
    assert "outside the collected range" in result.detail
    assert result.new_state is None and result.amount_out == 0
    assert (
        _scalars(state),
        dict(state.ticks),
        dict(state.tick_bitmap),
        state.bitmap_word_range,
    ) == before

    # `direct` does not skip an incomplete candidate and report the others' best
    case = dataclasses.replace(bundle.cases[0], case_id="beyond", amount_in=too_large)
    solved = direct.solve(case, SolveContext(bundle=bundle, objective=OBJECTIVE), Budget())
    assert solved.status is SolveStatus.INCOMPLETE_SNAPSHOT
    assert solved.plan is None and example in (solved.error or "")

    # the same in-envelope request flips to incomplete once its words are dropped
    lo, hi = state.bitmap_word_range
    truncated = dataclasses.replace(state, bitmap_word_range=(lo + 20, hi))
    large = max(c.amount_in for c in bundle.cases if c.token_in == USDC)
    assert quote_exact_in(state, USDC, large).status is QuoteStatus.OK
    assert quote_exact_in(truncated, USDC, large).status is QuoteStatus.INCOMPLETE_SNAPSHOT


# ---------------------------------------------------------------------------
# Bundle block identity
# ---------------------------------------------------------------------------


def _rewrite(bundle_dir: Path, name: str, mutate: Any) -> None:
    path = bundle_dir / name
    obj = json.loads(path.read_text())
    mutate(obj)
    text = json.dumps(obj, indent=2, sort_keys=True) + "\n"
    path.write_text(text)
    manifest = json.loads((bundle_dir / MANIFEST_FILE).read_text())
    if name != MANIFEST_FILE:
        manifest["checksums"][name] = sha256_bytes(text.encode())
        (bundle_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True))


def test_every_state_component_records_the_bundle_block(bundle: SnapshotBundle) -> None:
    manifest = json.loads((FIXTURE_BUNDLE / MANIFEST_FILE).read_text())
    raw = json.loads((FIXTURE_BUNDLE / POOLS_FILE).read_text())
    assert raw["pools"]
    for rec in raw["pools"]:
        assert rec["read_at"] == {
            "block_number": manifest["block"]["number"],
            "block_hash": manifest["block"]["hash"],
        }
    provenance = json.loads((FIXTURE_BUNDLE / PROVENANCE_FILE).read_text())
    assert provenance["block"] == manifest["block"]


@pytest.mark.parametrize(
    "target,mutate,match",
    [
        (
            POOLS_FILE,
            lambda o: o["pools"][1]["read_at"].update(block_hash="0x" + "ee" * 32),
            "never mixes blocks",
        ),
        (
            POOLS_FILE,
            lambda o: o["pools"][0]["read_at"].update(block_number=1),
            "never mixes blocks",
        ),
        (
            PROVENANCE_FILE,
            lambda o: o["block"].update(hash="0x" + "ee" * 32),
            "differs from the manifest",
        ),
        (MANIFEST_FILE, lambda o: o["block"].update(hash="0x" + "ee" * 32), "never mixes blocks"),
        (POOLS_FILE, lambda o: o["pools"][0]["ticks"].pop(3), "must be complete"),
    ],
)
def test_loader_rejects_mixed_block_or_incomplete_state(
    tmp_path: Path, target: str, mutate: Any, match: str
) -> None:
    bundle_dir = tmp_path / "b"
    shutil.copytree(FIXTURE_BUNDLE, bundle_dir)
    _rewrite(bundle_dir, target, mutate)
    with pytest.raises(BundleError, match=match):
        load_bundle(bundle_dir)


# ---------------------------------------------------------------------------
# 2. Collector mechanics against a scripted chain
# ---------------------------------------------------------------------------


def _fake_catalog(chain: FakeChain, example_pool: str) -> ProtocolCatalog:
    agni = CATALOG.source("agni_v3")
    assert agni.cl_collection is not None
    immutables = {
        "factory": addr_int(chain.factory),
        "token0": addr_int(chain.pools[example_pool].token0),
        "token1": addr_int(chain.pools[example_pool].token1),
        "fee": chain.pools[example_pool].fee,
        "tickSpacing": chain.pools[example_pool].tick_spacing,
        "maxLiquidityPerTick": chain.max_liq,
    }
    normalized, _ = abi.immutable_normalized_code_hash(chain.pool_code(example_pool), immutables)
    contracts = dict(agni.contracts)
    contracts["factory"] = ContractRef(chain.factory, abi.keccak256_hex(chain.code(chain.factory)))
    contracts["pool_deployer"] = ContractRef(
        chain.deployer, abi.keccak256_hex(chain.code(chain.deployer))
    )
    contracts["example_pool"] = ContractRef(
        example_pool, abi.keccak256_hex(chain.pool_code(example_pool))
    )
    source = dataclasses.replace(
        agni,
        contracts=contracts,
        cl_collection=dataclasses.replace(agni.cl_collection, pool_code_normalized_hash=normalized),
    )
    sources = tuple(source if s.key == "agni_v3" else s for s in CATALOG.sources)
    return dataclasses.replace(CATALOG, sources=sources)


@pytest.fixture()
def chain(bundle: SnapshotBundle) -> FakeChain:
    fake = FakeChain(
        dict(_cl_pools(bundle)),
        block_number=bundle.block.number,
        block_hash=bundle.block.hash,
        timestamp=bundle.block.timestamp,
    )
    fake.extra_pools[(USDC, WMNT, 100)] = "0x7b3a4b36b0c5c95142afcd1b883ed055aa166f85"
    return fake


EXAMPLE = CATALOG.source("agni_v3").contracts["example_pool"].address.lower()


def _collector(
    chain: FakeChain, config: ClPrepareConfig | None = None, **kwargs: Any
) -> ConcentratedCollector:
    return ConcentratedCollector(
        catalog=kwargs.pop("catalog", _fake_catalog(chain, EXAMPLE)),
        source_key=kwargs.pop("source_key", "agni_v3"),
        config=config or load_prepare_config(PREPARE_CONFIG),
        transport=chain,
        block_number=kwargs.pop("block_number", chain.block_number),
        **kwargs,
    )


def test_collector_reproduces_the_fixture_state_from_pinned_reads(
    chain: FakeChain, tmp_path: Path
) -> None:
    collected = _collector(chain, expected_block_hash=chain.block_hash).collect()
    out = publish(tmp_path / "bundle", collected)
    # The walk over the same chain state is deterministic: same words, ticks, cases.
    assert (tmp_path / "bundle" / POOLS_FILE).read_bytes() == (
        FIXTURE_BUNDLE / POOLS_FILE
    ).read_bytes()
    assert (tmp_path / "bundle" / CASES_FILE).read_bytes() == (
        FIXTURE_BUNDLE / CASES_FILE
    ).read_bytes()
    assert out.bundle_id == load_bundle(FIXTURE_BUNDLE).bundle_id
    # Every state read was pinned to the frozen hash (FakeChain asserts the pin); no
    # read ever named `latest` or a bare block number.
    state_reads = [p for m, p in chain.log if m in ("eth_call", "eth_getCode")]
    assert len(state_reads) > 1000
    assert all(json.dumps(p).count("latest") == 0 for _, p in chain.log)
    methods = [m for m, _ in chain.log]
    assert methods[:3] == ["eth_chainId", "eth_getBlockByNumber", "eth_getBlockByNumber"]
    assert methods[-2:] == ["eth_getBlockByHash", "eth_getBlockByNumber"]  # re-verified last


def test_changing_block_hash_during_collection_aborts_publication(
    chain: FakeChain, tmp_path: Path
) -> None:
    chain.hashes_by_number = [chain.block_hash, "0x" + "ee" * 32]  # reorg after the start
    output = tmp_path / "bundle"
    with pytest.raises(PrepareError, match="block_hash_mismatch.*not published"):
        publish(output, _collector(chain).collect())
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_expected_hash_mismatch_aborts_before_any_state_read(chain: FakeChain) -> None:
    with pytest.raises(PrepareError, match="block_hash_mismatch"):
        _collector(chain, expected_block_hash="0x" + "12" * 32).collect()
    assert not [m for m, _ in chain.log if m in ("eth_call", "eth_getCode")]


def test_unfinalized_block_is_refused(chain: FakeChain) -> None:
    chain.finalized = chain.block_number - 1
    with pytest.raises(PrepareError, match="block_not_finalized"):
        _collector(chain).collect()


def test_envelope_beyond_word_limit_refuses_publication(chain: FakeChain) -> None:
    config = load_prepare_config(PREPARE_CONFIG)
    tight = dataclasses.replace(config, limits=CollectionLimits(1, 1, max_words_per_direction=5))
    with pytest.raises(PrepareError, match="incomplete_snapshot.*beyond 5 bitmap words"):
        _collector(chain, tight).collect()


def test_only_admitted_sources_are_collected(chain: FakeChain) -> None:
    no_record = dataclasses.replace(
        CATALOG,
        sources=tuple(
            dataclasses.replace(s, cl_collection=None) if s.key == "uniswap_v3" else s
            for s in CATALOG.sources
        ),
    )
    with pytest.raises(PrepareError, match="source_not_admitted"):
        _collector(chain, catalog=no_record, source_key="uniswap_v3")
    agni = CATALOG.source("agni_v3")
    assert agni.cl_collection is not None
    not_admitted = dataclasses.replace(
        _fake_catalog(chain, EXAMPLE),
        sources=tuple(
            dataclasses.replace(
                s, cl_collection=dataclasses.replace(agni.cl_collection, admitted=False)
            )
            if s.key == "agni_v3"
            else s
            for s in CATALOG.sources
        ),
    )
    with pytest.raises(PrepareError, match="source_not_admitted"):
        _collector(chain, catalog=not_admitted)


def test_pool_with_other_code_is_rejected(chain: FakeChain) -> None:
    other = next(p for p in chain.pools if p != EXAMPLE)
    chain.code_patch[other] = chain.pool_code(other) + b"\x00"
    with pytest.raises(PrepareError, match="code_hash_mismatch.*immutable-normalized"):
        _collector(chain).collect()


def test_prepare_requires_an_explicit_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main.main(["prepare", "--source", "agni", "--output", str(tmp_path / "b")])
    assert rc == 1
    assert "explicit --block is required" in capsys.readouterr().err
    assert not (tmp_path / "b").exists()
    assert "agni" in COLLECTORS


def test_synthetic_source_refuses_a_block(tmp_path: Path) -> None:
    with pytest.raises(PrepareError, match="offline"):
        COLLECTORS["synthetic"](PrepareRequest(output_dir=tmp_path / "s", block_number=1))


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def _raw_config() -> dict[str, Any]:
    import yaml

    raw = yaml.safe_load(PREPARE_CONFIG.read_text())
    assert isinstance(raw, dict)
    return raw


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda r: r.update(extra=1), "unknown key"),
        (lambda r: r["collection"].update(max_words_per_direction=0), "outside"),
        (lambda r: r["cases"][0].update(amount_in="0"), "must be positive"),
        (lambda r: r["cases"][0].update(amount_in=10), "decimal string"),
        (lambda r: r["pairs"][0]["excluded_fee_tiers"][0].update(reason=" "), "written reason"),
        (
            lambda r: r["pairs"][0]["excluded_fee_tiers"][0].update(fee=500),
            "both admitted and excluded",
        ),
        (
            lambda r: r["tokens"].update(ALIAS="0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9"),
            "same address",
        ),
        (lambda r: r["cases"][0].update(token_out="0x" + "99" * 20), "not in `pairs`"),
    ],
)
def test_prepare_config_rejects_invalid_values(mutate: Any, match: str) -> None:
    raw = _raw_config()
    mutate(raw)
    with pytest.raises(PrepareConfigError, match=match):
        parse_prepare_config(raw, source_path="<test>", sha256="0" * 64)


def test_cl_collection_admission_is_explicit_per_source() -> None:
    admitted = [
        s.key for s in CATALOG.sources if s.cl_collection is not None and s.cl_collection.admitted
    ]
    # each by its own issue: Uniswap v3 (WHI-1431), Agni (WHI-1429), FusionX (WHI-1430)
    assert admitted == ["uniswap_v3", "agni_v3", "fusionx_v3"]

"""WHI-1430: FusionX v3 admitted through the shared fixed-block CL collector.

FusionX is collected by the same `snapshot.collectors.concentrated` code as Agni
(WHI-1429), but is admitted on its *own* evidence -- nothing here assumes Agni or
Uniswap equivalence:

1. **Admission record.** `config/protocols.yaml` `fusionx_v3.cl_collection` pins the
   FusionXV3Pool immutable-normalized code fingerprint; the published bundle's
   provenance carries factory/deployer/pool code identity, the attached LM hook's
   identity and per-pool completeness bounds.
2. **Fixed-block fork evidence.** `tests/fixtures/fusionx/bundle/` was published by
   `main.py prepare --source fusionx --block 101057678`; `tests/fixtures/fusionx/
   evidence.jsonl.gz` was produced independently by
   `tools/cl_evidence/test/CaptureFusionXReplay.t.sol`, executing every (case x pool)
   request with the *deployed* FusionXV3Pool bytecode (and its live LM hook) on a Mantle
   fork at that block, plus FusionX's own deployed QuoterV2. Quotes, next states,
   crossed ticks and a follow-up swap must equal it exactly, in both directions.
3. **Incompatible deployments are rejected**: genuine AgniPool / UniswapV3Pool runtime
   code (captured at the same block) behind FusionX's factory, a foreign factory, a
   broken deployer round-trip, an LM hook bound to another pool, a non-FusionX fee tier.
4. **Offline replay**: `validate`/`run` over the saved bundle with sockets disabled.
"""

from __future__ import annotations

import dataclasses
import gzip
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
from snapshot.bundle import CASES_FILE, MANIFEST_FILE, POOLS_FILE, PROVENANCE_FILE, load_bundle
from snapshot.collectors import COLLECTORS, PrepareError
from snapshot.collectors.concentrated import ConcentratedCollector, publish
from snapshot.config import ContractRef, ProtocolCatalog, load_catalog
from snapshot.models import ConcentratedPoolState, SnapshotBundle
from snapshot.prepare_config import ClPrepareConfig, PairSpec, load_prepare_config

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "fusionx"
FIXTURE_BUNDLE = FIXTURES / "bundle"
EVIDENCE = FIXTURES / "evidence.jsonl.gz"
POOL_CODE = FIXTURES / "pool_code.json.gz"
PREPARE_CONFIG = REPO / "config" / "prepare" / "fusionx.yaml"
CATALOG = load_catalog(REPO / "config" / "protocols.yaml")
FUSIONX = CATALOG.source("fusionx_v3")
SMOKE_PROFILE = REPO / "config" / "smoke.yaml"
USDT = "0x201eba5cc46d216ce6dc03f6a759e8e766e956ae"
WMNT = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"
EXAMPLE = FUSIONX.contracts["example_pool"].address.lower()
EXCLUDED_POOL = "0x4a313244ccddd402ef8c3b2c0bcbbd31782a5f88"  # USDT/WMNT 0.01%
OBJECTIVE = gross_only()
ZERO = "0x" + "00" * 20


def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted during an offline command")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)


@pytest.fixture(scope="module")
def bundle() -> SnapshotBundle:
    return load_bundle(FIXTURE_BUNDLE)


@pytest.fixture(scope="module")
def provenance() -> dict[str, Any]:
    obj = json.loads((FIXTURE_BUNDLE / PROVENANCE_FILE).read_text())
    assert isinstance(obj, dict)
    return obj


def _cl_pools(b: SnapshotBundle) -> dict[str, ConcentratedPoolState]:
    out = {k: v for k, v in b.pools.items() if isinstance(v, ConcentratedPoolState)}
    assert len(out) == len(b.pools)
    return out


def _has_lm(state: ConcentratedPoolState) -> bool:
    return state.lm_pool is not None and state.lm_pool.lower() != ZERO


# ---------------------------------------------------------------------------
# 1. Admission record: identity + completeness accompany every admitted state
# ---------------------------------------------------------------------------


def test_fusionx_admission_is_its_own_record() -> None:
    agni = CATALOG.source("agni_v3")
    assert FUSIONX.cl_collection is not None and FUSIONX.cl_collection.admitted
    assert agni.cl_collection is not None
    # FusionX's own pool fingerprint, not Agni's (different optimizer settings/bytes)
    assert (
        FUSIONX.cl_collection.pool_code_normalized_hash
        != agni.cl_collection.pool_code_normalized_hash
    )
    assert FUSIONX.contracts["factory"].address != agni.contracts["factory"].address
    assert "fusionx" in COLLECTORS
    assert load_prepare_config(PREPARE_CONFIG).source_key == "fusionx_v3"


def test_fixture_is_the_published_fusionx_bundle_at_the_catalog_block(
    bundle: SnapshotBundle, provenance: dict[str, Any]
) -> None:
    cb = CATALOG.candidate_block
    assert bundle.kind == "real"
    assert (bundle.block.chain_id, bundle.block.number, bundle.block.hash) == (
        CATALOG.network.chain_id,
        cb.number,
        cb.hash,
    )
    assert bundle.block.timestamp == cb.timestamp
    assert FUSIONX.cl_collection is not None
    assert provenance["source_key"] == "fusionx_v3"
    catalog = provenance["catalog"]
    for role in ("factory", "pool_deployer"):
        ref = FUSIONX.contracts[role]
        assert catalog[role] == {"address": ref.address.lower(), "code_hash": ref.code_hash}
    assert catalog["pool_code_normalized_hash"] == FUSIONX.cl_collection.pool_code_normalized_hash
    assert FUSIONX.upstream is not None
    assert catalog["upstream"]["ref"] == FUSIONX.upstream.ref
    assert provenance["prepare_config"]["path"].endswith("config/prepare/fusionx.yaml")

    pools = _cl_pools(bundle)
    assert EXAMPLE in pools
    assert provenance["pools"][EXAMPLE]["code_hash"] == FUSIONX.contracts["example_pool"].code_hash
    assert {p.fee for p in pools.values()} == {500, 2500, 10000}
    for pool_id, pool in pools.items():
        assert pool.source_key == "fusionx_v3"
        assert {pool.token0, pool.token1} == {USDT, WMNT}
        record = provenance["pools"][pool_id]
        assert record["normalized_code_hash"] == FUSIONX.cl_collection.pool_code_normalized_hash
        # the immutables really are embedded where solc 0.7.6 puts them
        assert all(n > 0 for n in record["immutable_sites"].values())
        completeness = record["completeness"]
        assert completeness["bitmap_word_range"] == list(pool.bitmap_word_range)
        assert completeness["initialized_ticks"] == len(pool.ticks)
        for direction in ("zero_for_one", "one_for_zero"):
            assert completeness["envelope"][direction]["status"] == "ok"
        assert record["lm_pool"] == pool.lm_pool
    assert {c.token_in for c in bundle.cases} == {USDT, WMNT}
    # every case x pool was admitted
    assert len(provenance["admission"]) == len(bundle.cases) * len(pools)
    assert {a["status"] for a in provenance["admission"]} == {"ok"}


def test_live_lm_hook_is_recorded_with_its_identity(
    bundle: SnapshotBundle, provenance: dict[str, Any]
) -> None:
    """FusionX's main pool (unlike every admitted Agni pool) calls a live LM hook on
    every swap. It is modeled as a no-op, so its identity is part of the admission."""
    pools = _cl_pools(bundle)
    hooked = {pid for pid, p in pools.items() if _has_lm(p)}
    assert EXAMPLE in hooked
    for pool_id in pools:
        record = provenance["pools"][pool_id]
        if pool_id in hooked:
            identity = record["lm_pool_identity"]
            assert identity["pool"] == pool_id
            assert identity["code_hash"].startswith("0x") and len(identity["code_hash"]) == 66
        else:
            assert "lm_pool_identity" not in record


def test_excluded_tier_is_explicit_not_silent(provenance: dict[str, Any]) -> None:
    omitted = provenance["discovery"]["omitted"]
    assert [(o["fee"], o["pool_id"]) for o in omitted] == [(100, EXCLUDED_POOL)]
    assert omitted[0]["reason"].startswith("excluded by prepare config:")
    assert "MIN_TICK" in omitted[0]["reason"]
    config = load_prepare_config(PREPARE_CONFIG)
    assert config.pairs[0].excluded_fee_tiers[0][0] == 100


def test_every_state_component_records_the_bundle_block() -> None:
    manifest = json.loads((FIXTURE_BUNDLE / MANIFEST_FILE).read_text())
    raw = json.loads((FIXTURE_BUNDLE / POOLS_FILE).read_text())
    assert raw["pools"]
    for rec in raw["pools"]:
        assert rec["read_at"] == {
            "block_number": manifest["block"]["number"],
            "block_hash": manifest["block"]["hash"],
        }


# ---------------------------------------------------------------------------
# 2. Fixed-block fork evidence (both directions, crossed ticks, QuoterV2)
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
    text = gzip.decompress(EVIDENCE.read_bytes()).decode()
    records = [json.loads(line) for line in text.splitlines() if line.strip()]
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


def _fork_output(req: Request) -> int:
    a0, a1 = int(req.swap["amount0"]), int(req.swap["amount1"])
    return -a1 if req.swap["zero_for_one"] else -a0


def test_evidence_is_bound_to_the_bundle_block(bundle: SnapshotBundle, evidence: Evidence) -> None:
    meta = evidence.meta
    assert meta["schema"] == "fusionx-replay-evidence/1"
    assert meta["bundle_id"] == bundle.bundle_id
    assert (
        meta["chain_id"],
        meta["block_number"],
        meta["block_timestamp"],
        meta["block_hash"],
    ) == (bundle.block.chain_id, bundle.block.number, bundle.block.timestamp, bundle.block.hash)
    assert meta["quoter"].lower() == FUSIONX.contracts["quoter_v2"].address.lower()


def test_collected_state_matches_fork_storage(
    bundle: SnapshotBundle, evidence: Evidence, provenance: dict[str, Any]
) -> None:
    """The collector's eth_call reads equal the fork's own storage reads: scalars, every
    bitmap word of the collected range, every initialized tick in it, the pool's code
    and the LM hook's code."""
    pools = _cl_pools(bundle)
    assert set(evidence.pool_states) == set(pools)
    for pool_id, pool in pools.items():
        ev = evidence.pool_states[pool_id]
        record = provenance["pools"][pool_id]
        assert _ev_scalars(ev) == _scalars(pool)
        assert ev["unlocked"] is True
        assert (int(ev["fee"]), int(ev["tick_spacing"])) == (pool.fee, pool.tick_spacing)
        assert (ev["token0"].lower(), ev["token1"].lower()) == (pool.token0, pool.token1)
        assert ev["code_hash"].lower() == record["code_hash"]
        assert ev["lm_pool"].lower() == (pool.lm_pool or ZERO).lower()
        if _has_lm(pool):
            assert ev["lm_pool_code_hash"].lower() == record["lm_pool_identity"]["code_hash"]
        lo, hi = pool.bitmap_word_range
        assert sorted(evidence.words[pool_id]) == list(range(lo, hi + 1))
        for word, value in evidence.words[pool_id].items():
            assert pool.tick_bitmap.get(word, 0) == value, f"{pool_id} word {word}"
        assert set(evidence.ticks[pool_id]) == set(pool.ticks)
        for t, rec in evidence.ticks[pool_id].items():
            info = pool.ticks[t]
            assert _tick_tuple(rec) == (
                info.liquidity_gross,
                info.liquidity_net,
                info.fee_growth_outside0_x128,
                info.fee_growth_outside1_x128,
            ), f"{pool_id} tick {t}"


def test_every_quote_and_next_state_match_fork_and_quoter(
    bundle: SnapshotBundle, evidence: Evidence
) -> None:
    pools = _cl_pools(bundle)
    expected_keys = {
        (c.case_id, p.pool_id)
        for c in bundle.cases
        for p in bundle.pools_for_pair(c.token_in, c.token_out)
    }
    assert set(evidence.requests) == expected_keys
    crossings = {True: 0, False: 0}
    hooked_crossings = 0
    for (case_id, pool_id), req in sorted(evidence.requests.items()):
        case = bundle.case(case_id)
        state = pools[pool_id]
        zero_for_one = case.token_in == state.token0
        assert req.swap["zero_for_one"] is zero_for_one
        assert int(req.swap["amount_specified"]) == case.amount_in
        a0, a1 = int(req.swap["amount0"]), int(req.swap["amount1"])
        assert (a0 if zero_for_one else a1) == case.amount_in, "must be full exact-input fills"

        result = quote_exact_in(state, case.token_in, case.amount_in)
        assert result.status is QuoteStatus.OK, result.detail
        assert result.amount_out == _fork_output(req), f"{case_id} on {pool_id}"
        new_state = result.new_state
        assert new_state is not None
        assert _scalars(new_state) == _ev_scalars(req.swap)

        # FusionX's deployed QuoterV2: a second on-chain quote path
        assert int(req.swap["quoter_amount_out"]) == result.amount_out
        assert int(req.swap["quoter_sqrt_price_x96_after"]) == new_state.sqrt_price_x96

        # crossed ticks are exactly those the fork changed, with the fork's values
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
        crossings[zero_for_one] += len(crossed)
        # the live LM hook really ran on the fork (accumulateReward + crossLmTick per
        # crossed tick) and changed nothing the simulator's no-op branch did not
        expected_hook_calls = 1 + len(crossed) if _has_lm(state) else 0
        assert result.features["lm_pool_hook_calls"] == expected_hook_calls
        if _has_lm(state):
            hooked_crossings += len(crossed)

        fu = req.followup
        assert fu is not None
        back_in = state.token1 if zero_for_one else state.token0
        back = quote_exact_in(new_state, back_in, int(fu["amount_specified"]))
        assert back.status is QuoteStatus.OK, back.detail
        b0, b1 = int(fu["amount0"]), int(fu["amount1"])
        assert back.amount_out == (-b0 if zero_for_one else -b1)
        assert back.new_state is not None
        assert _scalars(back.new_state) == _ev_scalars(fu)
    # both directions cross initialized ticks, including through the hooked pool
    assert crossings[True] > 20 and crossings[False] > 20, crossings
    assert hooked_crossings > 20


def test_direct_agrees_with_fixed_block_evidence_in_both_directions(
    bundle: SnapshotBundle, evidence: Evidence
) -> None:
    context = SolveContext(bundle=bundle, objective=OBJECTIVE)
    seen_directions: set[str] = set()
    for case in bundle.cases:
        solved = direct.solve(case, context, Budget())
        assert solved.status is SolveStatus.OK, solved.error
        assert solved.evaluation is not None and solved.plan is not None
        outputs = {
            pool_id: _fork_output(req)
            for (case_id, pool_id), req in evidence.requests.items()
            if case_id == case.case_id
        }
        best_pool = max(outputs, key=lambda p: outputs[p])
        assert solved.plan.steps[0].pool_id == best_pool
        assert solved.evaluation.gross_output == outputs[best_pool]
        assert solved.candidates_considered == len(outputs) == 3
        seen_directions.add(case.token_in)
    assert seen_directions == {USDT, WMNT}


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
        assert int(rec["evaluation"]["gross_output"]) == _fork_output(req)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["bundle_hash"] == load_bundle(bundle_dir).bundle_hash


def test_traversal_beyond_collected_tick_bounds_is_incomplete_snapshot(
    bundle: SnapshotBundle,
) -> None:
    state = _cl_pools(bundle)[EXAMPLE]
    too_large = 10 * max(c.amount_in for c in bundle.cases if c.token_in == WMNT)
    result = quote_exact_in(state, WMNT, too_large)
    assert result.status is QuoteStatus.INCOMPLETE_SNAPSHOT
    assert "outside the collected range" in result.detail
    case = dataclasses.replace(bundle.cases[-1], case_id="beyond", amount_in=too_large)
    solved = direct.solve(case, SolveContext(bundle=bundle, objective=OBJECTIVE), Budget())
    assert solved.status is SolveStatus.INCOMPLETE_SNAPSHOT
    assert solved.plan is None


# ---------------------------------------------------------------------------
# 3. Collector mechanics and incompatible deployments (scripted chain)
# ---------------------------------------------------------------------------


def _fake_catalog(chain: FakeChain) -> ProtocolCatalog:
    """The real catalog, with FusionX's factory/deployer/example pins pointed at the fake
    node's synthesized code (the real pins are checked against the fork above)."""
    assert FUSIONX.cl_collection is not None
    example = chain.pools[EXAMPLE]
    immutables = {
        "factory": addr_int(chain.factory),
        "token0": addr_int(example.token0),
        "token1": addr_int(example.token1),
        "fee": example.fee,
        "tickSpacing": example.tick_spacing,
        "maxLiquidityPerTick": chain.max_liq,
    }
    normalized, _ = abi.immutable_normalized_code_hash(chain.pool_code(EXAMPLE), immutables)
    contracts = dict(FUSIONX.contracts)
    contracts["factory"] = ContractRef(chain.factory, abi.keccak256_hex(chain.code(chain.factory)))
    contracts["pool_deployer"] = ContractRef(
        chain.deployer, abi.keccak256_hex(chain.code(chain.deployer))
    )
    contracts["example_pool"] = ContractRef(EXAMPLE, abi.keccak256_hex(chain.pool_code(EXAMPLE)))
    source = dataclasses.replace(
        FUSIONX,
        contracts=contracts,
        cl_collection=dataclasses.replace(
            FUSIONX.cl_collection, pool_code_normalized_hash=normalized
        ),
    )
    sources = tuple(source if s.key == "fusionx_v3" else s for s in CATALOG.sources)
    return dataclasses.replace(CATALOG, sources=sources)


@pytest.fixture()
def chain(bundle: SnapshotBundle) -> FakeChain:
    fake = FakeChain(
        dict(_cl_pools(bundle)),
        block_number=bundle.block.number,
        block_hash=bundle.block.hash,
        timestamp=bundle.block.timestamp,
    )
    fake.extra_pools[(USDT, WMNT, 100)] = EXCLUDED_POOL
    return fake


def _collector(
    chain: FakeChain, config: ClPrepareConfig | None = None, **kwargs: Any
) -> ConcentratedCollector:
    return ConcentratedCollector(
        catalog=kwargs.pop("catalog", _fake_catalog(chain)),
        source_key="fusionx_v3",
        config=config or load_prepare_config(PREPARE_CONFIG),
        transport=chain,
        block_number=chain.block_number,
        **kwargs,
    )


def test_collector_reproduces_the_fixture_state_from_pinned_reads(
    chain: FakeChain, tmp_path: Path
) -> None:
    collected = _collector(chain, expected_block_hash=chain.block_hash).collect()
    out = publish(tmp_path / "bundle", collected)
    for name in (POOLS_FILE, CASES_FILE):
        assert (tmp_path / "bundle" / name).read_bytes() == (FIXTURE_BUNDLE / name).read_bytes()
    assert out.bundle_id == load_bundle(FIXTURE_BUNDLE).bundle_id
    # the LM hook's back-reference was read at the pinned block
    lm = chain.pools[EXAMPLE].lm_pool
    assert lm is not None
    assert any(
        m == "eth_call" and p[0]["to"].lower() == lm.lower() and p[0]["data"][2:10] == abi.SEL_POOL
        for m, p in chain.log
    )
    assert all(json.dumps(p).count("latest") == 0 for _, p in chain.log)
    assert [m for m, _ in chain.log][-2:] == ["eth_getBlockByHash", "eth_getBlockByNumber"]


def test_changing_block_hash_during_collection_aborts_publication(
    chain: FakeChain, tmp_path: Path
) -> None:
    chain.hashes_by_number = [chain.block_hash, "0x" + "ee" * 32]
    with pytest.raises(PrepareError, match="block_hash_mismatch.*not published"):
        publish(tmp_path / "bundle", _collector(chain).collect())
    assert list(tmp_path.iterdir()) == []


def _real_pool_code() -> dict[str, dict[str, Any]]:
    obj = json.loads(gzip.decompress(POOL_CODE.read_bytes()))
    assert obj["block_hash"] == CATALOG.candidate_block.hash
    pools = obj["pools"]
    assert isinstance(pools, dict)
    return pools


def _normalized(entry: dict[str, Any]) -> tuple[str, str]:
    code = bytes.fromhex(entry["runtime_code"].removeprefix("0x"))
    immutables = {k: int(v) for k, v in entry["immutables"].items()}
    normalized, _ = abi.immutable_normalized_code_hash(code, immutables)
    return abi.keccak256_hex(code), normalized


@pytest.mark.parametrize("source_key", ["fusionx_v3", "agni_v3", "uniswap_v3"])
def test_real_pool_code_fingerprints(source_key: str) -> None:
    """The checked-in runtime code is the genuine example-pool code of each CL source at
    the block (exact hash = catalog pin); only FusionXV3Pool matches FusionX's
    fingerprint."""
    entry = _real_pool_code()[source_key]
    source = CATALOG.source(source_key)
    assert entry["address"] == source.contracts["example_pool"].address.lower()
    exact, normalized = _normalized(entry)
    assert exact == source.contracts["example_pool"].code_hash
    assert FUSIONX.cl_collection is not None
    matches_fusionx = normalized == FUSIONX.cl_collection.pool_code_normalized_hash
    assert matches_fusionx is (source_key == "fusionx_v3")
    if source_key == "agni_v3":  # valid code for its *own* admission
        assert source.cl_collection is not None
        assert normalized == source.cl_collection.pool_code_normalized_hash
    # (Uniswap's own fingerprint also normalizes `original`: tests/snapshot/test_uniswap_v3.py)


@pytest.mark.parametrize("foreign", ["agni_v3", "uniswap_v3"])
def test_foreign_pool_code_behind_fusionx_factory_is_rejected(
    chain: FakeChain, foreign: str
) -> None:
    """A pool the FusionX factory returns, whose getters look right, but which runs
    genuine AgniPool / UniswapV3Pool bytecode, is refused before any state is read."""
    victim = next(p for p in chain.pools if p != EXAMPLE)
    chain.code_patch[victim] = bytes.fromhex(
        _real_pool_code()[foreign]["runtime_code"].removeprefix("0x")
    )
    with pytest.raises(PrepareError, match="code_hash_mismatch.*not the verified fusionx_v3"):
        _collector(chain).collect()
    assert not [
        p for m, p in chain.log if m == "eth_call" and p[0]["data"][2:10] == abi.SEL_TICK_BITMAP
    ]


def test_foreign_factory_is_rejected(chain: FakeChain) -> None:
    catalog = _fake_catalog(chain)
    fx = catalog.source("fusionx_v3")
    agni_factory = CATALOG.source("agni_v3").contracts["factory"]
    contracts = {**fx.contracts, "factory": ContractRef(chain.factory, agni_factory.code_hash)}
    bad = dataclasses.replace(
        catalog,
        sources=tuple(
            dataclasses.replace(s, contracts=contracts) if s.key == "fusionx_v3" else s
            for s in catalog.sources
        ),
    )
    with pytest.raises(PrepareError, match="code_hash_mismatch: factory"):
        _collector(chain, catalog=bad).collect()


def test_deployer_round_trip_mismatch_is_rejected(chain: FakeChain) -> None:
    chain.deployer_answer = "0x" + "d0" * 20  # factory.poolDeployer() names another contract
    with pytest.raises(PrepareError, match="identity_mismatch: factory.poolDeployer"):
        _collector(chain).collect()


def test_pool_reporting_another_factory_is_rejected(chain: FakeChain) -> None:
    chain.factory_of = {EXAMPLE: "0x" + "ab" * 20}
    with pytest.raises(PrepareError, match="identity_mismatch.*expected"):
        _collector(chain).collect()


def test_lm_hook_bound_to_another_pool_is_rejected(chain: FakeChain) -> None:
    lm = chain.pools[EXAMPLE].lm_pool
    assert lm is not None
    chain.lm_backref[lm.lower()] = "0x" + "cd" * 20
    with pytest.raises(PrepareError, match="identity_mismatch.*lmPool.*not this pool"):
        _collector(chain).collect()


def test_lm_hook_without_code_is_rejected(chain: FakeChain) -> None:
    lm = chain.pools[EXAMPLE].lm_pool
    assert lm is not None
    chain.lm_code[lm.lower()] = b""
    with pytest.raises(PrepareError, match="missing_code"):
        _collector(chain).collect()


def test_uniswap_fee_tier_is_not_a_fusionx_tier(chain: FakeChain) -> None:
    config = load_prepare_config(PREPARE_CONFIG)
    uni_tier = dataclasses.replace(config, pairs=(PairSpec(USDT, WMNT, (3000,)),))
    with pytest.raises(PrepareError, match="invalid_request: fee tier 3000"):
        _collector(chain, uni_tier).collect()


def test_factory_tick_spacing_disagreeing_with_catalog_is_rejected(chain: FakeChain) -> None:
    chain.spacings[500] = 60
    with pytest.raises(PrepareError, match="fee_tier_mismatch"):
        _collector(chain).collect()

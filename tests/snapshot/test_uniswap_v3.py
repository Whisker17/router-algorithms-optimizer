"""WHI-1431: Uniswap v3 (Mantle) admitted through the shared fixed-block CL collector.

Uniswap v3 is collected by the same `snapshot.collectors.concentrated` code as Agni
(WHI-1429) and FusionX (WHI-1430), but is admitted on its *own* evidence -- nothing here
assumes equivalence with either fork:

1. **Admission record.** `config/protocols.yaml` `uniswap_v3.cl_collection` pins the
   UniswapV3Pool immutable-normalized code fingerprint (including v3-core's
   `NoDelegateCall.original` = the pool's own address) and declares that the factory
   deploys its own pools (no separate pool deployer). The published bundle's provenance
   carries factory/pool code identity, per-pool completeness bounds, explicit omissions
   and the source's SOR capability (`V3`) for cohort selection.
2. **Fixed-block fork evidence.** `tests/fixtures/uniswap_v3/bundle/` was published by
   `main.py prepare --source uniswap_v3 --block 101057678`;
   `tests/fixtures/uniswap_v3/evidence.jsonl.gz` was produced independently by
   `tools/cl_evidence/test/CaptureUniswapV3Replay.t.sol`, executing every (case x pool)
   request with the *deployed* UniswapV3Pool bytecode on a Mantle fork at that block,
   plus Uniswap's own deployed QuoterV2. Amounts, next states, crossed ticks and a
   follow-up swap must equal it exactly, in both directions -- for full fills *and* for
   the partial fills of Uniswap-on-Mantle's thin pools (proven `insufficient_liquidity`).
3. **Incompatible deployments and malformed state are rejected**: genuine AgniPool /
   FusionXV3Pool runtime code behind Uniswap's factory, pool code embedding another
   pool's address, a foreign factory, a Pancake-only fee tier, a locked or out-of-range
   `slot0`, a bitmap bit without an initialized tick, a hook on a hookless source.
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
from pools.cl_math import MAX_SQRT_RATIO, MAX_TICK, MIN_SQRT_RATIO, MIN_TICK
from pools.concentrated import quote_exact_in, swap
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
from snapshot.collectors import COLLECTORS, PrepareError
from snapshot.collectors.concentrated import ConcentratedCollector, publish
from snapshot.config import ConfigError, ContractRef, ProtocolCatalog, load_catalog, parse_catalog
from snapshot.models import ConcentratedPoolState, SnapshotBundle, TickInfo
from snapshot.prepare_config import (
    ClPrepareConfig,
    CollectionLimits,
    PairSpec,
    load_prepare_config,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "uniswap_v3"
FIXTURE_BUNDLE = FIXTURES / "bundle"
EVIDENCE = FIXTURES / "evidence.jsonl.gz"
# Real runtime code of the three CL sources' example pools at the block (WHI-1430).
POOL_CODE = REPO / "tests" / "fixtures" / "fusionx" / "pool_code.json.gz"
PREPARE_CONFIG = REPO / "config" / "prepare" / "uniswap_v3.yaml"
CATALOG_PATH = REPO / "config" / "protocols.yaml"
CATALOG = load_catalog(CATALOG_PATH)
UNI = CATALOG.source("uniswap_v3")
SMOKE_PROFILE = REPO / "config" / "smoke.yaml"
USDT = "0x201eba5cc46d216ce6dc03f6a759e8e766e956ae"
USDC = "0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9"
WMNT = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"
WETH = "0xdeaddeaddeaddeaddeaddeaddeaddeaddead1111"
METH = "0xcda86a272531e8640cd7f1a92c01839911b90bb0"
EXAMPLE = UNI.contracts["example_pool"].address.lower()  # USDT/WMNT 0.05%
EXCLUDED_POOL = "0x8cfee38ab8b8f4bc2ff662e8cc8bdfb0439c9d2c"  # USDC/USDT 0.01%
# Every non-zero UniswapV3Factory.getPool over the configured pairs x tiers, but the
# excluded one; the two liquidity()==0 pools are collected over their whole tick range.
ADMITTED = {
    "0x4cdfc22bf05209de87ee564746dc7e5174631d2b": (USDT, WMNT, 500),
    "0x086f766b336dfb0f705dc030db01993b22d81266": (USDC, WMNT, 3000),
    "0xfc60a4d05ac8c93f62276e046ad5a098f5c7820a": (WMNT, WETH, 500),
    "0x082a6df295d9efeedd2838d154a2bbc255fa0745": (WMNT, WETH, 3000),
    "0xc64639501bcee4c48ef05a46f7bedf00b3529a99": (WMNT, WETH, 10000),
    "0xeaf42c2ba326b37530b7f67d20ac1e7ec9ccd77b": (WMNT, METH, 3000),
    "0x5d637c5c1ddda97fc6115fa68ca66e444c0f2686": (WMNT, METH, 10000),
    "0x48ef5640e71001cac842f5627a0bfec1ef09deb7": (METH, WETH, 100),
    "0x2c7c187e3990053c265f8a8d599b4323c6239ae8": (USDC, METH, 3000),
    "0x9df1e55e6281b2c8c9211ce7fadcfbf49c7afd32": (USDC, METH, 10000),
    "0x076eb72e74c16b208c692eeab3750978d76b8f28": (USDT, WETH, 500),
    "0xcff260aea6b7cc472db5e7abb15272605a4d859b": (USDT, METH, 3000),
}
DEAD_POOLS = {
    "0x082a6df295d9efeedd2838d154a2bbc255fa0745",
    "0xeaf42c2ba326b37530b7f67d20ac1e7ec9ccd77b",
}
OBJECTIVE = gross_only()


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


def _sorted_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


# ---------------------------------------------------------------------------
# 1. Admission record: identity + completeness accompany every admitted state
# ---------------------------------------------------------------------------


def test_uniswap_admission_is_its_own_record() -> None:
    assert UNI.cl_collection is not None and UNI.cl_collection.admitted
    assert UNI.cl_collection.factory_deploys_pools
    assert "pool_deployer" not in UNI.contracts
    assert "original" in UNI.cl_collection.pool_immutables
    for fork in ("agni_v3", "fusionx_v3"):
        other = CATALOG.source(fork)
        assert other.cl_collection is not None
        assert (
            UNI.cl_collection.pool_code_normalized_hash
            != other.cl_collection.pool_code_normalized_hash
        )
        assert not other.cl_collection.factory_deploys_pools
        assert UNI.contracts["factory"].address != other.contracts["factory"].address
    assert "uniswap_v3" in COLLECTORS
    assert load_prepare_config(PREPARE_CONFIG).source_key == "uniswap_v3"
    # the catalog's example pair is USDT/WMNT with the pool's own (sorted) token order
    assert (UNI.tokens["token0"].lower(), UNI.tokens["token1"].lower()) == (USDT, WMNT)


def test_fixture_is_the_published_uniswap_bundle_at_the_catalog_block(
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
    assert UNI.cl_collection is not None
    assert provenance["source_key"] == "uniswap_v3"
    catalog = provenance["catalog"]
    factory = UNI.contracts["factory"]
    assert catalog["factory"] == {
        "address": factory.address.lower(),
        "code_hash": factory.code_hash,
    }
    assert catalog["pool_deployer"] == "factory"
    assert catalog["pool_code_normalized_hash"] == UNI.cl_collection.pool_code_normalized_hash
    assert catalog["pool_immutables"] == list(UNI.cl_collection.pool_immutables)
    assert UNI.upstream is not None
    assert catalog["upstream"]["ref"] == UNI.upstream.ref
    assert provenance["prepare_config"]["path"].endswith("config/prepare/uniswap_v3.yaml")

    pools = _cl_pools(bundle)
    assert {pid: (p.token0, p.token1, p.fee) for pid, p in pools.items()} == ADMITTED
    assert provenance["pools"][EXAMPLE]["code_hash"] == UNI.contracts["example_pool"].code_hash
    for pool_id, pool in pools.items():
        assert pool.source_key == "uniswap_v3"
        assert pool.lm_pool is None  # UniswapV3Pool has no LM hook (lmPool() never called)
        assert pool.tick_spacing == UNI.expected_fee_tiers[pool.fee]  # type: ignore[index]
        record = provenance["pools"][pool_id]
        assert record["normalized_code_hash"] == UNI.cl_collection.pool_code_normalized_hash
        # every immutable really is embedded where solc 0.7.6 puts it, `original` once
        assert record["immutable_sites"] == {
            "factory": 3,
            "fee": 4,
            "maxLiquidityPerTick": 3,
            "original": 1,
            "tickSpacing": 4,
            "token0": 6,
            "token1": 6,
        }
        assert "lm_pool_identity" not in record
        completeness = record["completeness"]
        assert completeness["bitmap_word_range"] == list(pool.bitmap_word_range)
        assert completeness["initialized_ticks"] == len(pool.ticks)
        for direction in ("zero_for_one", "one_for_zero"):
            assert completeness["envelope"][direction]["status"] in ("ok", "insufficient_liquidity")
    # every case x pool of its pair was admitted, as ok or as proven exhaustion
    expected = {
        (c.case_id, p.pool_id)
        for c in bundle.cases
        for p in bundle.pools_for_pair(c.token_in, c.token_out)
    }
    assert {(a["case_id"], a["pool_id"]) for a in provenance["admission"]} == expected
    assert {a["status"] for a in provenance["admission"]} == {"ok", "insufficient_liquidity"}
    assert len(bundle.cases) == 48 and len(expected) == 72


def test_every_pool_of_the_universe_is_admitted_or_explicitly_omitted(
    provenance: dict[str, Any],
) -> None:
    """Selection method: every pair of the five configured tokens x every catalog fee
    tier through getPool. Each lookup ends up admitted or as a recorded omission."""
    config = load_prepare_config(PREPARE_CONFIG)
    discovery = provenance["discovery"]
    looked_up = {
        (pair.token0, pair.token1, fee)
        for pair in config.pairs
        for fee in (*pair.fee_tiers, *(f for f, _ in pair.excluded_fee_tiers))
    }
    assert len(config.pairs) == 10 and len(looked_up) == 40
    admitted = {(a["token0"], a["token1"], a["fee"]) for a in discovery["admitted"]}
    omitted = {(o["token0"], o["token1"], o["fee"]): o for o in discovery["omitted"]}
    assert admitted | set(omitted) == looked_up and not admitted & set(omitted)
    assert admitted == {(*_sorted_pair(t0, t1), fee) for t0, t1, fee in ADMITTED.values()}
    excluded = [o for o in omitted.values() if o["pool_id"] is not None]
    assert [(o["token0"], o["token1"], o["fee"], o["pool_id"]) for o in excluded] == [
        (*_sorted_pair(USDC, USDT), 100, EXCLUDED_POOL)
    ]
    assert excluded[0]["reason"].startswith("excluded by prepare config:")
    assert "MIN_TICK" in excluded[0]["reason"]
    assert all(
        o["reason"] == "factory.getPool returned the zero address"
        for o in omitted.values()
        if o["pool_id"] is None
    )


def test_liquidity_free_pools_are_collected_over_the_whole_tick_range(
    bundle: SnapshotBundle, provenance: dict[str, Any]
) -> None:
    """A liquidity()==0 pool is not dropped: its entire int24 bitmap is collected, so
    every quote on it is a *proven* insufficient_liquidity, not a guess."""
    pools = _cl_pools(bundle)
    for pool_id in DEAD_POOLS:
        pool = pools[pool_id]
        assert pool.liquidity == 0 and pool.ticks == {}
        lo, hi = pool.bitmap_word_range
        assert lo <= (MIN_TICK // pool.tick_spacing) >> 8
        assert hi >= (MAX_TICK // pool.tick_spacing) >> 8
        for case in bundle.cases:
            if {case.token_in, case.token_out} == {pool.token0, pool.token1}:
                result = quote_exact_in(pool, case.token_in, case.amount_in)
                assert result.status is QuoteStatus.INSUFFICIENT_LIQUIDITY
                assert "only 0 of" in result.detail


def test_source_capability_is_exposed_for_cohort_selection(
    bundle: SnapshotBundle, provenance: dict[str, Any]
) -> None:
    """Uniswap v3 is an SOR V3 source (docs/references/uni-sor-port-contract.md §2): the
    catalog exposes it to cohort selection, and the published bundle carries it."""
    sor = CATALOG.sor_protocols()
    assert sor == {
        "uniswap_v3": "V3",
        "agni_v3": "V3",
        "fusionx_v3": "V3",
        "moe_classic_v1": "V2",  # WHI-1432
    }
    assert "moe_lb_v2_2" not in sor  # LB never enters the SOR cohort (D-4)
    assert provenance["source_capability"] == {
        "protocol_family": "v3_concentrated_liquidity",
        "sor_protocol": "V3",
    }
    # from the bundle alone: every pool is in the matched SOR V3 cohort
    cohort = {
        pid
        for pid, p in _cl_pools(bundle).items()
        if sor.get(p.source_key) == provenance["source_capability"]["sor_protocol"]
    }
    assert cohort == set(ADMITTED)


def _raw_catalog() -> dict[str, Any]:
    import yaml

    raw = yaml.safe_load(CATALOG_PATH.read_text())
    assert isinstance(raw, dict)
    return raw


def _uni_raw(raw: dict[str, Any]) -> dict[str, Any]:
    entry = next(s for s in raw["sources"] if s["key"] == "uniswap_v3")
    assert isinstance(entry, dict)
    return entry


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda s: s.update(sor_protocol="V4"), "sor_protocol"),
        (
            lambda s: s["contracts"].update(pool_deployer={"address": "0x" + "11" * 20}),
            "contradicts a pinned contracts.pool_deployer",
        ),
        (lambda s: s["cl_collection"].update(factory_deploys_pools="yes"), "expected a boolean"),
        (lambda s: s["cl_collection"].update(pool_immutables=["self"]), "distinct names"),
    ],
)
def test_catalog_rejects_malformed_uniswap_admission(mutate: Any, match: str) -> None:
    raw = _raw_catalog()
    mutate(_uni_raw(raw))
    with pytest.raises(ConfigError, match=match):
        parse_catalog(raw)


def test_every_state_component_records_the_bundle_block(provenance: dict[str, Any]) -> None:
    manifest = json.loads((FIXTURE_BUNDLE / MANIFEST_FILE).read_text())
    raw = json.loads((FIXTURE_BUNDLE / POOLS_FILE).read_text())
    assert len(raw["pools"]) == len(ADMITTED)
    for rec in raw["pools"]:
        assert rec["read_at"] == {
            "block_number": manifest["block"]["number"],
            "block_hash": manifest["block"]["hash"],
        }
    assert provenance["block"] == manifest["block"]


# ---------------------------------------------------------------------------
# 2. Fixed-block fork evidence (both directions, crossed ticks, partial fills, QuoterV2)
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


def _info_tuple(info: TickInfo) -> tuple[int, int, int, int]:
    return (
        info.liquidity_gross,
        info.liquidity_net,
        info.fee_growth_outside0_x128,
        info.fee_growth_outside1_x128,
    )


def _fork_in_out(req: Request) -> tuple[int, int]:
    """(input the pool actually took, output it paid) on the fork."""
    a0, a1 = int(req.swap["amount0"]), int(req.swap["amount1"])
    return (a0, -a1) if req.swap["zero_for_one"] else (a1, -a0)


def _full_fill_outputs(evidence: Evidence, case_id: str, amount: int) -> dict[str, int]:
    out: dict[str, int] = {}
    for (cid, pool_id), req in evidence.requests.items():
        if cid != case_id:
            continue
        consumed, produced = _fork_in_out(req)
        if consumed == amount:
            out[pool_id] = produced
    return out


def test_evidence_is_bound_to_the_bundle_block(bundle: SnapshotBundle, evidence: Evidence) -> None:
    meta = evidence.meta
    assert meta["schema"] == "uniswap-v3-replay-evidence/1"
    assert meta["bundle_id"] == bundle.bundle_id
    assert (
        meta["chain_id"],
        meta["block_number"],
        meta["block_timestamp"],
        meta["block_hash"],
    ) == (bundle.block.chain_id, bundle.block.number, bundle.block.timestamp, bundle.block.hash)
    assert meta["quoter"].lower() == UNI.contracts["quoter_v2"].address.lower()


def test_collected_state_matches_fork_storage(
    bundle: SnapshotBundle, evidence: Evidence, provenance: dict[str, Any]
) -> None:
    """The collector's eth_call reads equal the fork's own storage reads: scalars, every
    bitmap word of the collected range, every initialized tick in it and the pool code."""
    pools = _cl_pools(bundle)
    assert set(evidence.pool_states) == set(pools)
    for pool_id, pool in pools.items():
        ev = evidence.pool_states[pool_id]
        assert _ev_scalars(ev) == _scalars(pool)
        assert ev["unlocked"] is True
        assert (int(ev["fee"]), int(ev["tick_spacing"])) == (pool.fee, pool.tick_spacing)
        assert (ev["token0"].lower(), ev["token1"].lower()) == (pool.token0, pool.token1)
        assert ev["code_hash"].lower() == provenance["pools"][pool_id]["code_hash"]
        lo, hi = pool.bitmap_word_range
        assert sorted(evidence.words[pool_id]) == list(range(lo, hi + 1))
        for word, value in evidence.words[pool_id].items():
            assert pool.tick_bitmap.get(word, 0) == value, f"{pool_id} word {word}"
        assert set(evidence.ticks.get(pool_id, {})) == set(pool.ticks)
        for t, rec in evidence.ticks.get(pool_id, {}).items():
            assert _tick_tuple(rec) == _info_tuple(pool.ticks[t]), f"{pool_id} tick {t}"


def test_every_swap_and_next_state_match_fork_and_quoter(
    bundle: SnapshotBundle, evidence: Evidence
) -> None:
    """Every request, full fill or partial: the migrated swap's signed amounts, next
    state and crossed ticks equal the deployed pool's; `quote_exact_in` is OK exactly
    for full fills and `insufficient_liquidity` exactly for the fork's partial fills;
    QuoterV2 agrees; and a follow-up reverse swap from the new state agrees too."""
    pools = _cl_pools(bundle)
    expected_keys = {
        (c.case_id, p.pool_id)
        for c in bundle.cases
        for p in bundle.pools_for_pair(c.token_in, c.token_out)
    }
    assert set(evidence.requests) == expected_keys
    crossings = {True: 0, False: 0}
    kinds = {"full": 0, "partial": 0, "empty": 0}
    for (case_id, pool_id), req in sorted(evidence.requests.items()):
        case = bundle.case(case_id)
        state = pools[pool_id]
        zero_for_one = case.token_in == state.token0
        assert req.swap["zero_for_one"] is zero_for_one
        assert int(req.swap["amount_specified"]) == case.amount_in
        limit = MIN_SQRT_RATIO + 1 if zero_for_one else MAX_SQRT_RATIO - 1

        outcome = swap(state, zero_for_one, case.amount_in, limit)
        assert (outcome.amount0, outcome.amount1) == (
            int(req.swap["amount0"]),
            int(req.swap["amount1"]),
        ), f"{case_id} on {pool_id}"
        assert _scalars(outcome.new_state) == _ev_scalars(req.swap)
        assert outcome.lm_pool_hook_calls == 0
        crossed = set(req.changed_ticks)
        assert set(outcome.crossed_ticks) == crossed
        assert int(req.swap["traversed_initialized_ticks"]) >= len(crossed)
        for t, info in outcome.new_state.ticks.items():
            if t in crossed:
                assert _tick_tuple(req.changed_ticks[t]) == _info_tuple(info), f"{case_id} {t}"
            else:
                assert info == state.ticks[t], f"{case_id} on {pool_id}: tick {t} changed"

        consumed, produced = _fork_in_out(req)
        result = quote_exact_in(state, case.token_in, case.amount_in)
        if consumed == case.amount_in:
            kinds["full"] += 1
            assert result.status is QuoteStatus.OK, result.detail
            assert result.amount_out == produced
            assert result.new_state is not None
            assert _scalars(result.new_state) == _ev_scalars(req.swap)
            assert result.features["initialized_ticks_crossed"] == len(crossed)
            crossings[zero_for_one] += len(crossed)
        else:
            assert 0 <= consumed < case.amount_in
            kinds["partial" if consumed else "empty"] += 1
            assert result.status is QuoteStatus.INSUFFICIENT_LIQUIDITY
            assert f"only {consumed} of {case.amount_in}" in result.detail
            assert f"would yield {produced})" in result.detail
            # the fork ran all the way to the price limit
            assert int(req.swap["sqrt_price_x96"]) == limit

        # Uniswap's deployed QuoterV2: a second on-chain quote path. It refuses a swap
        # that moves no token ("swaps entirely within 0-liquidity regions").
        if req.swap["quoter_reverted"]:
            assert consumed == 0 and produced == 0
        else:
            assert int(req.swap["quoter_amount_out"]) == produced
            assert int(req.swap["quoter_sqrt_price_x96_after"]) == outcome.new_state.sqrt_price_x96

        fu = req.followup
        assert (fu is None) is (produced // 2 == 0)
        if fu is not None:
            back_in = state.token1 if zero_for_one else state.token0
            back = quote_exact_in(outcome.new_state, back_in, int(fu["amount_specified"]))
            assert back.status is QuoteStatus.OK, back.detail
            b0, b1 = int(fu["amount0"]), int(fu["amount1"])
            assert back.amount_out == (-b0 if zero_for_one else -b1)
            assert back.new_state is not None
            assert _scalars(back.new_state) == _ev_scalars(fu)
    # both directions cross initialized ticks on full fills; the thin pools really run
    # dry (partial fills), and the liquidity-free ones move no token at all
    assert crossings == {True: 16, False: 12}, crossings
    assert kinds == {"full": 51, "partial": 9, "empty": 12}, kinds


def test_direct_agrees_with_fixed_block_evidence_in_both_directions(
    bundle: SnapshotBundle, evidence: Evidence
) -> None:
    context = SolveContext(bundle=bundle, objective=OBJECTIVE)
    seen: dict[SolveStatus, set[str]] = {SolveStatus.OK: set(), SolveStatus.NO_ROUTE: set()}
    for case in bundle.cases:
        solved = direct.solve(case, context, Budget())
        pools = bundle.pools_for_pair(case.token_in, case.token_out)
        assert solved.candidates_considered == len(pools)
        outputs = _full_fill_outputs(evidence, case.case_id, case.amount_in)
        if not outputs:
            # every candidate is proven exhausted: no route, never a partial "success"
            assert solved.status is SolveStatus.NO_ROUTE, solved.error
            assert solved.plan is None
        else:
            assert solved.status is SolveStatus.OK, solved.error
            assert solved.evaluation is not None and solved.plan is not None
            best_pool = max(outputs, key=lambda p: outputs[p])
            assert solved.plan.steps[0].pool_id == best_pool
            assert solved.evaluation.gross_output == outputs[best_pool]
        seen[solved.status].add(case.token_in)
    assert seen[SolveStatus.OK] == {USDT, USDC, WMNT, WETH, METH}
    assert seen[SolveStatus.NO_ROUTE]  # e.g. every mETH->USDT case on the one thin pool


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
    replayed = load_bundle(bundle_dir)
    assert len(records) == len(replayed.cases)
    for rec in records:
        case = replayed.case(rec["case_id"])
        outputs = _full_fill_outputs(evidence, case.case_id, case.amount_in)
        if not outputs:
            assert rec["status"] == "no_route"
            continue
        assert rec["status"] == "ok"
        assert int(rec["evaluation"]["gross_output"]) == max(outputs.values())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["bundle_hash"] == replayed.bundle_hash


def test_traversal_beyond_collected_tick_bounds_is_incomplete_snapshot(
    bundle: SnapshotBundle,
) -> None:
    state = _cl_pools(bundle)[EXAMPLE]
    large = max(c.amount_in for c in bundle.cases if c.token_in == USDT and c.token_out == WMNT)
    assert quote_exact_in(state, USDT, large).status is QuoteStatus.OK
    result = quote_exact_in(state, USDT, 10 * large)
    assert result.status is QuoteStatus.INCOMPLETE_SNAPSHOT
    assert "outside the collected range" in result.detail
    case = dataclasses.replace(bundle.cases[0], case_id="beyond", amount_in=10 * large)
    solved = direct.solve(case, SolveContext(bundle=bundle, objective=OBJECTIVE), Budget())
    assert solved.status is SolveStatus.INCOMPLETE_SNAPSHOT
    assert solved.plan is None
    # the same in-envelope request flips to incomplete once its words are dropped
    lo, hi = state.bitmap_word_range
    truncated = dataclasses.replace(state, bitmap_word_range=(lo + 10, hi))
    assert quote_exact_in(truncated, USDT, large).status is QuoteStatus.INCOMPLETE_SNAPSHOT


def test_a_hook_on_a_hookless_source_is_unsupported(bundle: SnapshotBundle) -> None:
    """UniswapV3Pool has no LM hook; a state claiming one is malformed for this source."""
    state = dataclasses.replace(_cl_pools(bundle)[EXAMPLE], lm_pool="0x" + "12" * 20)
    result = quote_exact_in(state, USDT, 1_000_000)
    assert result.status is QuoteStatus.UNSUPPORTED
    assert "has no LM-pool hook" in result.detail


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


def _example_record(obj: dict[str, Any]) -> dict[str, Any]:
    rec = next(p for p in obj["pools"] if p["pool_id"] == EXAMPLE)
    assert isinstance(rec, dict)
    return rec


@pytest.mark.parametrize(
    "target,mutate,match",
    [
        (
            POOLS_FILE,
            lambda o: _example_record(o)["read_at"].update(block_hash="0x" + "ee" * 32),
            "never mixes blocks",
        ),
        (POOLS_FILE, lambda o: _example_record(o)["ticks"].pop(0), "must be complete"),
        (
            POOLS_FILE,
            lambda o: _example_record(o).update(bitmap_word_range=[0, 1]),
            "does not contain the current tick's word",
        ),
        (POOLS_FILE, lambda o: _example_record(o).update(source_key="uniswap_v4"), "source_key"),
        (
            PROVENANCE_FILE,
            lambda o: o["block"].update(hash="0x" + "ee" * 32),
            "differs from the manifest",
        ),
    ],
)
def test_loader_rejects_mixed_block_or_malformed_state(
    tmp_path: Path, target: str, mutate: Any, match: str
) -> None:
    bundle_dir = tmp_path / "b"
    shutil.copytree(FIXTURE_BUNDLE, bundle_dir)
    _rewrite(bundle_dir, target, mutate)
    with pytest.raises(BundleError, match=match):
        load_bundle(bundle_dir)


# ---------------------------------------------------------------------------
# 3. Collector mechanics, incompatible deployments, malformed chain state
# ---------------------------------------------------------------------------


def _fake_catalog(chain: FakeChain) -> ProtocolCatalog:
    """The real catalog, with Uniswap's factory/example pins pointed at the fake node's
    synthesized code (the real pins are checked against the fork above)."""
    assert UNI.cl_collection is not None
    example = chain.pools[EXAMPLE]
    immutables = {
        "factory": addr_int(chain.factory),
        "token0": addr_int(example.token0),
        "token1": addr_int(example.token1),
        "fee": example.fee,
        "tickSpacing": example.tick_spacing,
        "maxLiquidityPerTick": chain.max_liq,
        "original": addr_int(EXAMPLE),
    }
    normalized, _ = abi.immutable_normalized_code_hash(chain.pool_code(EXAMPLE), immutables)
    contracts = dict(UNI.contracts)
    contracts["factory"] = ContractRef(chain.factory, abi.keccak256_hex(chain.code(chain.factory)))
    contracts["example_pool"] = ContractRef(EXAMPLE, abi.keccak256_hex(chain.pool_code(EXAMPLE)))
    source = dataclasses.replace(
        UNI,
        contracts=contracts,
        cl_collection=dataclasses.replace(UNI.cl_collection, pool_code_normalized_hash=normalized),
    )
    sources = tuple(source if s.key == "uniswap_v3" else s for s in CATALOG.sources)
    return dataclasses.replace(CATALOG, sources=sources)


@pytest.fixture()
def chain(bundle: SnapshotBundle) -> FakeChain:
    fake = FakeChain(
        dict(_cl_pools(bundle)),
        block_number=bundle.block.number,
        block_hash=bundle.block.hash,
        timestamp=bundle.block.timestamp,
    )
    fake.embed_self_address = True
    fake.spacings = {100: 1, 500: 10, 3000: 60, 10000: 200}
    fake.extra_pools[(USDC, USDT, 100)] = EXCLUDED_POOL
    return fake


def _collector(
    chain: FakeChain, config: ClPrepareConfig | None = None, **kwargs: Any
) -> ConcentratedCollector:
    return ConcentratedCollector(
        catalog=kwargs.pop("catalog", _fake_catalog(chain)),
        source_key="uniswap_v3",
        config=config or load_prepare_config(PREPARE_CONFIG),
        transport=chain,
        block_number=chain.block_number,
        **kwargs,
    )


def _selectors(chain: FakeChain) -> set[str]:
    return {p[0]["data"][2:10] for m, p in chain.log if m == "eth_call"}


def test_collector_reproduces_the_fixture_state_from_pinned_reads(
    chain: FakeChain, tmp_path: Path
) -> None:
    collected = _collector(chain, expected_block_hash=chain.block_hash).collect()
    out = publish(tmp_path / "bundle", collected)
    for name in (POOLS_FILE, CASES_FILE):
        assert (tmp_path / "bundle" / name).read_bytes() == (FIXTURE_BUNDLE / name).read_bytes()
    assert out.bundle_id == load_bundle(FIXTURE_BUNDLE).bundle_id
    # no pool deployer to round-trip and no LM hook to read: Uniswap has neither
    assert abi.SEL_POOL_DEPLOYER not in _selectors(chain)
    assert abi.SEL_LM_POOL not in _selectors(chain)
    assert all(json.dumps(p).count("latest") == 0 for _, p in chain.log)
    assert [m for m, _ in chain.log][-2:] == ["eth_getBlockByHash", "eth_getBlockByNumber"]
    assert collected.provenance["source_capability"]["sor_protocol"] == "V3"


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


@pytest.mark.parametrize("source_key", ["uniswap_v3", "agni_v3", "fusionx_v3"])
def test_real_pool_code_fingerprints(source_key: str) -> None:
    """The genuine example-pool runtime code of each CL source at the block: only
    UniswapV3Pool matches Uniswap's fingerprint, and only once `original` (the pool's own
    address) is normalized too."""
    entry = _real_pool_code()[source_key]
    code = bytes.fromhex(entry["runtime_code"].removeprefix("0x"))
    assert abi.keccak256_hex(code) == CATALOG.source(source_key).contracts["example_pool"].code_hash
    assert UNI.cl_collection is not None
    immutables = {k: int(v) for k, v in entry["immutables"].items()}
    with_self = {**immutables, "original": int(entry["address"], 16)}
    normalized, sites = abi.immutable_normalized_code_hash(code, with_self)
    assert (normalized == UNI.cl_collection.pool_code_normalized_hash) is (
        source_key == "uniswap_v3"
    )
    if source_key == "uniswap_v3":
        assert sites["original"] == 1
        without_self, _ = abi.immutable_normalized_code_hash(code, immutables)
        assert without_self != UNI.cl_collection.pool_code_normalized_hash


@pytest.mark.parametrize("foreign", ["agni_v3", "fusionx_v3"])
def test_foreign_pool_code_behind_uniswap_factory_is_rejected(
    chain: FakeChain, foreign: str
) -> None:
    """A pool the Uniswap factory returns, whose getters look right, but which runs
    genuine AgniPool / FusionXV3Pool bytecode, is refused before any state is read."""
    victim = next(p for p in chain.pools if p != EXAMPLE)
    chain.code_patch[victim] = bytes.fromhex(
        _real_pool_code()[foreign]["runtime_code"].removeprefix("0x")
    )
    with pytest.raises(PrepareError, match="code_hash_mismatch.*not the verified uniswap_v3"):
        _collector(chain).collect()
    assert abi.SEL_TICK_BITMAP not in _selectors(chain)


def test_pool_code_embedding_another_pools_address_is_rejected(chain: FakeChain) -> None:
    """`original` must be the pool's *own* address: code whose NoDelegateCall immutable
    names another pool (e.g. a copy or a delegating proxy) fails the fingerprint."""
    victim = next(p for p in chain.pools if p != EXAMPLE)
    code = chain.pool_code(victim)
    own = addr_int(victim).to_bytes(32, "big")
    assert code.count(own) == 1
    chain.code_patch[victim] = code.replace(own, addr_int(EXAMPLE).to_bytes(32, "big"))
    with pytest.raises(PrepareError, match="code_hash_mismatch.*immutable-normalized"):
        _collector(chain).collect()


def test_foreign_factory_is_rejected(chain: FakeChain) -> None:
    catalog = _fake_catalog(chain)
    uni = catalog.source("uniswap_v3")
    fx_factory = CATALOG.source("fusionx_v3").contracts["factory"]
    contracts = {**uni.contracts, "factory": ContractRef(chain.factory, fx_factory.code_hash)}
    bad = dataclasses.replace(
        catalog,
        sources=tuple(
            dataclasses.replace(s, contracts=contracts) if s.key == "uniswap_v3" else s
            for s in catalog.sources
        ),
    )
    with pytest.raises(PrepareError, match="code_hash_mismatch: factory"):
        _collector(chain, catalog=bad).collect()


def test_pool_reporting_another_factory_is_rejected(chain: FakeChain) -> None:
    chain.factory_of = {EXAMPLE: "0x" + "ab" * 20}
    with pytest.raises(PrepareError, match="identity_mismatch.*expected"):
        _collector(chain).collect()


def test_pancake_fee_tier_is_not_a_uniswap_tier(chain: FakeChain) -> None:
    config = load_prepare_config(PREPARE_CONFIG)
    pancake_tier = dataclasses.replace(config, pairs=(PairSpec(USDT, WMNT, (2500,)),))
    with pytest.raises(PrepareError, match="invalid_request: fee tier 2500"):
        _collector(chain, pancake_tier).collect()


def test_factory_tick_spacing_disagreeing_with_catalog_is_rejected(chain: FakeChain) -> None:
    chain.spacings[3000] = 50
    with pytest.raises(PrepareError, match="fee_tier_mismatch"):
        _collector(chain).collect()


def test_locked_pool_at_a_block_boundary_is_rejected(chain: FakeChain) -> None:
    chain.unlocked[EXAMPLE] = 0
    with pytest.raises(PrepareError, match="inconsistent_state.*unlocked is 0"):
        _collector(chain).collect()


def test_fee_protocol_beyond_uint8_is_rejected(chain: FakeChain) -> None:
    """Uniswap's slot0.feeProtocol is a uint8 (two 4-bit denominators), unlike the
    Pancake-family uint32; a wider value means the node is not a UniswapV3Pool."""
    chain.pools[EXAMPLE] = dataclasses.replace(chain.pools[EXAMPLE], fee_protocol=1 << 16)
    with pytest.raises(PrepareError, match="inconsistent_state.*does not fit uint8"):
        _collector(chain).collect()


def test_bitmap_bit_without_an_initialized_tick_is_rejected(chain: FakeChain) -> None:
    state = chain.pools[EXAMPLE]
    word = (state.tick // state.tick_spacing) >> 8
    free_bit = next(b for b in range(256) if not (state.tick_bitmap.get(word, 0) >> b) & 1)
    bitmap = {**state.tick_bitmap, word: state.tick_bitmap.get(word, 0) | (1 << free_bit)}
    chain.pools[EXAMPLE] = dataclasses.replace(state, tick_bitmap=bitmap)
    with pytest.raises(PrepareError, match="inconsistent_state.*bitmap marks tick"):
        _collector(chain).collect()


def test_envelope_beyond_word_limit_refuses_publication(chain: FakeChain) -> None:
    config = load_prepare_config(PREPARE_CONFIG)
    tight = dataclasses.replace(config, limits=CollectionLimits(1, 1, max_words_per_direction=40))
    with pytest.raises(PrepareError, match="incomplete_snapshot.*beyond 40 bitmap words"):
        _collector(chain, tight).collect()

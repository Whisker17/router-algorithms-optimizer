"""WHI-1434: Merchant Moe Liquidity Book v2.2 replayed from a complete, bounded,
fixed-block bin snapshot.

1. **Collection admission.** The catalog's `moe_lb_v2_2.lb_collection` (approved swap hooks
   and extra hooks) agrees with the migrated simulator (`pools.liquidity_book.SOURCES`),
   LB has no SOR capability, and the `ImmutableClone`/CREATE2 derivation reproduces the
   pinned example pair.
2. **Published bundle.** `tests/fixtures/moe_lb/bundle/` was published by
   `main.py prepare --source moe_lb --block 101057678`: every LB pair of the five-token
   universe (`LBFactory.getAllLBPairs`, every bin step, including empty books), each
   verified as a clone of the pinned implementation at its CREATE2 address, its hooks and
   the rewarders' extra hooks on the approved lists, with bins walked until both directions
   of the declared envelope fit (or the book is proven exhausted), plus completeness bounds
   and hook provenance.
3. **Fixed-block fork evidence.** `tests/fixtures/moe_lb/evidence.jsonl.gz` was produced
   independently by `tools/cl_evidence/test/CaptureMoeLBReplay.t.sol` with the *deployed*
   LBPair clones and their live hooks on a Mantle fork at that block: an independent read
   of every collected field and bin, and for every (case x pair) request plus a sequential
   reverse follow-up through the post-state, the output, per-bin `Swap` events and next
   state. The Python must reproduce all of it from the bundle alone.
4. **Rejections** against a scripted node: wrong clone / CREATE2 / factory round trip /
   getters, unapproved or unbound hooks and extra hooks (the WHI-1433 deferred item),
   balance drift, wrong decimals, a truncated envelope, reorg, and malformed bundles.
5. **Offline replay** of `validate` and `run` with sockets disabled.
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

import main
from pools.liquidity_book import SOURCES, quote_exact_in, swap
from pools.result import QuoteStatus
from snapshot import abi
from snapshot.bundle import (
    MANIFEST_FILE,
    POOLS_FILE,
    PROVENANCE_FILE,
    BundleError,
    load_bundle,
    sha256_bytes,
)
from snapshot.collectors import COLLECTORS, PrepareError, PrepareRequest
from snapshot.collectors.classic import clone_create2_address, clone_runtime_code
from snapshot.collectors.liquidity_book import LBCollector, pair_salt, parse_clone, publish
from snapshot.config import ConfigError, ContractRef, ProtocolCatalog, load_catalog, parse_catalog
from snapshot.models import LiquidityBookPoolState, SnapshotBundle
from snapshot.prepare_config import (
    LBPairSpec,
    LBPrepareConfig,
    PrepareConfigError,
    load_lb_prepare_config,
    parse_lb_prepare_config,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "moe_lb"
FIXTURE_BUNDLE = FIXTURES / "bundle"
EVIDENCE = FIXTURES / "evidence.jsonl.gz"
PREPARE_CONFIG = REPO / "config" / "prepare" / "moe_lb.yaml"
CATALOG = load_catalog(REPO / "config" / "protocols.yaml")
LB = CATALOG.source("moe_lb_v2_2")
SMOKE_PROFILE = REPO / "config" / "smoke.yaml"
FACTORY = LB.contracts["factory"].address.lower()
IMPLEMENTATION = LB.contracts["pair_implementation"].address.lower()
EXAMPLE = LB.contracts["example_pair"].address.lower()  # WMNT/USDT binStep 15
REWARDER = "0xdc0e38cbd08fa532847baecbf26c8b09ed9008a7"
EXTRA_REWARDER = "0x2d4bf9f668e5b7c7fe33c8f116ae190669304676"
USDT = "0x201eba5cc46d216ce6dc03f6a759e8e766e956ae"
USDC = "0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9"
WMNT = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"
WETH = "0xdeaddeaddeaddeaddeaddeaddeaddeaddead1111"
METH = "0xcda86a272531e8640cd7f1a92c01839911b90bb0"
DECIMALS = {USDT: 6, USDC: 6, WMNT: 18, WETH: 18, METH: 18}
UINT24_MAX = (1 << 24) - 1
SWAP_FLAGS = (1 << 160) | (1 << 161)
ADDRESS_MASK = (1 << 160) - 1


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


@pytest.fixture(scope="module")
def evidence() -> list[dict[str, Any]]:
    lines = gzip.decompress(EVIDENCE.read_bytes()).decode().splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    assert records[0]["kind"] == "meta" and records[-1]["kind"] == "end"
    return records


def _pools(b: SnapshotBundle) -> dict[str, LiquidityBookPoolState]:
    out = {k: v for k, v in b.pools.items() if isinstance(v, LiquidityBookPoolState)}
    assert len(out) == len(b.pools)
    return out


def _nonempty(b: SnapshotBundle) -> dict[str, LiquidityBookPoolState]:
    return {k: v for k, v in _pools(b).items() if v.reserve_x or v.reserve_y}


def _deep(b: SnapshotBundle) -> dict[str, LiquidityBookPoolState]:
    """Pairs holding more than dust of both tokens (so both directions can fill)."""
    return {k: v for k, v in _pools(b).items() if min(v.reserve_x, v.reserve_y) > 10**6}


def _ints(values: list[str]) -> tuple[int, ...]:
    return tuple(int(v) for v in values)


# ---------------------------------------------------------------------------
# 1. Collection admission
# ---------------------------------------------------------------------------


def test_catalog_hook_lists_agree_with_the_simulator_and_lb_has_no_sor_capability() -> None:
    lb = LB.lb_collection
    assert lb is not None and lb.admitted
    source = SOURCES["moe_lb_v2_2"]
    assert {h.implementation for h in lb.swap_hooks} == set(source.amount_neutral_swap_hooks)
    assert {h.implementation for h in lb.extra_swap_hooks} == set(
        source.amount_neutral_extra_swap_hooks
    )
    assert [h.contract for h in lb.swap_hooks] == ["LBHooksRewarder"]
    assert [h.contract for h in lb.extra_swap_hooks] == ["LBHooksExtraRewarder"]
    assert LB.sor_protocol is None and "moe_lb_v2_2" not in CATALOG.sor_protocols()
    assert "moe_lb" in COLLECTORS
    assert load_lb_prepare_config(PREPARE_CONFIG).source_key == "moe_lb_v2_2"


def test_clone_layout_reproduces_the_pinned_example_pair() -> None:
    data = bytes.fromhex(WMNT[2:] + USDT[2:]) + (15).to_bytes(2, "big")
    code = clone_runtime_code(IMPLEMENTATION, data)
    assert len(code) == 97
    assert abi.keccak256_hex(code) == LB.contracts["example_pair"].code_hash
    assert parse_clone(code) == (IMPLEMENTATION, data)
    assert parse_clone(code[:-1]) is None and parse_clone(b"\x60\x80" + code[2:]) is None
    salt = pair_salt(WMNT, USDT, 15)
    assert clone_create2_address(FACTORY, IMPLEMENTATION, data, salt) == EXAMPLE
    assert pair_salt(USDT, WMNT, 15) == salt  # the factory sorts the tokens for the salt


def test_fixture_is_the_published_lb_bundle_at_the_catalog_block(
    bundle: SnapshotBundle, provenance: dict[str, Any]
) -> None:
    cb = CATALOG.candidate_block
    assert bundle.kind == "real"
    assert (bundle.block.number, bundle.block.hash, bundle.block.timestamp) == (
        cb.number,
        cb.hash,
        cb.timestamp,
    )
    assert provenance["source_key"] == "moe_lb_v2_2"
    assert provenance["source_capability"] == {
        "protocol_family": "liquidity_book_v2",
        "sor_protocol": None,
    }
    catalog = provenance["catalog"]
    for role in ("factory", "pair_implementation"):
        assert catalog[role] == {
            "address": LB.contracts[role].address.lower(),
            "code_hash": LB.contracts[role].code_hash,
        }
    assert catalog["approved_swap_hooks"] == [REWARDER]
    assert catalog["approved_extra_swap_hooks"] == [EXTRA_REWARDER]
    assert provenance["prepare_config"] == {
        "path": "config/prepare/moe_lb.yaml",
        "sha256": sha256_bytes(PREPARE_CONFIG.read_bytes()),
    }
    assert provenance["tokens"] == {
        t: {"label": label, "decimals": DECIMALS[t]}
        for t, label in (
            (USDT, "USDT"),
            (USDC, "USDC"),
            (WMNT, "WMNT"),
            (WETH, "WETH"),
            (METH, "METH"),
        )
    }
    omitted = provenance["discovery"]["omitted"]
    assert {(o["token0"], o["token1"]) for o in omitted} == {
        tuple(sorted((USDC, METH))),
        tuple(sorted((USDC, WETH))),
    }
    assert all(o["reason"] == "factory.getAllLBPairs returned no pair" for o in omitted)

    pools = _pools(bundle)
    admitted = provenance["discovery"]["admitted"]
    assert [a["pool_id"] for a in admitted] == list(pools)
    assert len(pools) == 25 and len(_nonempty(bundle)) == 9
    for a in admitted:
        pool = pools[a["pool_id"]]
        assert (pool.token0, pool.token1, pool.bin_step) == (
            a["token_x"],
            a["token_y"],
            a["bin_step"],
        )
        assert pool.source_key == "moe_lb_v2_2"
        assert pool.block_timestamp == bundle.block.timestamp
        record = provenance["pools"][pool.pool_id]
        data = bytes.fromhex(pool.token0[2:] + pool.token1[2:]) + pool.bin_step.to_bytes(2, "big")
        assert record["code_hash"] == abi.keccak256_hex(clone_runtime_code(IMPLEMENTATION, data))
        assert record["balances_equal_reserves_plus_protocol_fees"] is True
        assert record["completeness"]["bin_range"] == list(pool.bin_range)
        assert record["completeness"]["tree_bins"] == len(pool.bins)
        for side, end in (("below", 0), ("above", UINT24_MAX)):
            walk = record["completeness"]["walk"][side]
            assert walk["reached_end"] is (pool.bin_range[0 if side == "below" else 1] == end)
            assert walk["bins_walked"] <= record["completeness"]["limits"]["max_bins_per_direction"]
    assert provenance["pools"][EXAMPLE]["code_hash"] == LB.contracts["example_pair"].code_hash
    # An empty book is proven empty over the whole id space, never assumed.
    for pool_id, pool in pools.items():
        if pool_id not in _nonempty(bundle):
            assert pool.bins == {} and pool.bin_range == (0, UINT24_MAX)


def test_hooks_and_extra_hooks_are_recorded_and_approved(
    bundle: SnapshotBundle, provenance: dict[str, Any]
) -> None:
    approved = LB.lb_collection
    assert approved is not None
    hooked = extra = 0
    for pool_id, pool in _pools(bundle).items():
        record = provenance["pools"][pool_id]["hooks"]
        if not pool.hooks_parameters & SWAP_FLAGS:
            assert record is None and pool.swap_hook_implementation is None
            continue
        hooked += 1
        hook = f"0x{pool.hooks_parameters & ADDRESS_MASK:040x}"
        assert record["address"] == hook and record["implementation"] == REWARDER
        assert record["parameters"] == f"0x{pool.hooks_parameters:064x}"
        assert record["implementation_code_hash"] == approved.swap_hooks[0].code_hash
        assert record["clone_args"][2:42] == pool_id[2:]  # bound to this pair
        assert pool.swap_hook_implementation == REWARDER
        assert record["extra_hooks_parameters"] == f"0x{pool.extra_hooks_parameters:064x}"
        if pool.extra_hooks_parameters & SWAP_FLAGS:
            extra += 1
            e = record["extra"]
            assert e["address"] == f"0x{pool.extra_hooks_parameters & ADDRESS_MASK:040x}"
            assert e["implementation"] == EXTRA_REWARDER == pool.extra_swap_hook_implementation
            assert e["clone_args"][2:42] == pool_id[2:] and e["clone_args"][82:122] == hook[2:]
        else:
            assert record["extra"] is None and pool.extra_swap_hook_implementation is None
    assert (hooked, extra) == (7, 5)


def test_every_reference_case_and_sequential_followup_is_admitted(
    bundle: SnapshotBundle, provenance: dict[str, Any]
) -> None:
    expected = {
        (c.case_id, p.pool_id)
        for c in bundle.cases
        for p in bundle.pools_for_pair(c.token_in, c.token_out)
    }
    admission = provenance["admission"]
    assert {(a["case_id"], a["pool_id"]) for a in admission} == expected
    statuses = {a["status"] for a in admission} | {
        a["followup_status"] for a in admission if "followup_status" in a
    }
    assert statuses == {"ok", "insufficient_liquidity", "insufficient_output_amount"}
    # both directions of every non-empty pair are exercised by an `ok` swap
    ok = {
        (a["pool_id"], bundle.case(a["case_id"]).token_in) for a in admission if a["status"] == "ok"
    }
    assert len(_deep(bundle)) == 8
    for pool_id, pool in _deep(bundle).items():
        assert {(pool_id, pool.token0), (pool_id, pool.token1)} <= ok, pool_id
    # every non-empty pair fills at least one reference case
    assert set(_nonempty(bundle)) <= {pool_id for pool_id, _ in ok}


def test_every_state_component_records_the_bundle_block(provenance: dict[str, Any]) -> None:
    manifest = json.loads((FIXTURE_BUNDLE / MANIFEST_FILE).read_text())
    raw = json.loads((FIXTURE_BUNDLE / POOLS_FILE).read_text())
    for rec in raw["pools"]:
        assert rec["family"] == "liquidity_book"
        assert rec["read_at"] == {
            "block_number": manifest["block"]["number"],
            "block_hash": manifest["block"]["hash"],
        }
        assert rec["block_timestamp"] == manifest["block"]["timestamp"]
    assert provenance["block"] == manifest["block"]


# ---------------------------------------------------------------------------
# 2. Independent fork evidence at the same block
# ---------------------------------------------------------------------------


def test_evidence_was_captured_at_the_bundle_block(
    bundle: SnapshotBundle, evidence: list[dict[str, Any]]
) -> None:
    meta = evidence[0]
    assert meta["schema"] == "moe-lb-replay-evidence/1"
    assert meta["bundle_id"] == bundle.bundle_id
    assert (meta["chain_id"], meta["block_number"], meta["block_hash"]) == (
        bundle.block.chain_id,
        bundle.block.number,
        bundle.block.hash,
    )
    assert meta["block_timestamp"] == bundle.block.timestamp


def test_forked_pair_state_equals_the_collected_state(
    bundle: SnapshotBundle, provenance: dict[str, Any], evidence: list[dict[str, Any]]
) -> None:
    """An independent read (the fork's own view of each deployed pair) of every collected
    field, hook and bin -- the tree re-walked inside the bundle's `bin_range`."""
    pools = _pools(bundle)
    states = {r["pool"].lower(): r for r in evidence if r["kind"] == "pool_state"}
    assert set(states) == set(pools)
    for pool_id, rec in states.items():
        pool = pools[pool_id]
        assert (rec["token_x"].lower(), rec["token_y"].lower()) == (pool.token0, pool.token1)
        assert (rec["decimals_x"], rec["decimals_y"]) == (
            DECIMALS[pool.token0],
            DECIMALS[pool.token1],
        )
        assert int(rec["bin_step"]) == pool.bin_step
        assert (rec["factory"].lower(), rec["implementation"].lower()) == (FACTORY, IMPLEMENTATION)
        assert rec["code_hash"] == provenance["pools"][pool_id]["code_hash"]
        assert int(rec["active_id"]) == pool.active_id
        assert pool.static_fee is not None and pool.variable_fee is not None
        assert _ints(rec["static"]) == dataclasses.astuple(pool.static_fee)
        assert _ints(rec["variable"]) == dataclasses.astuple(pool.variable_fee)
        assert _ints(rec["reserves"]) == (pool.reserve_x, pool.reserve_y)
        assert _ints(rec["protocol_fees"]) == (pool.protocol_fee_x, pool.protocol_fee_y)
        assert (int(rec["balance_x"]), int(rec["balance_y"])) == (
            pool.reserve_x + pool.protocol_fee_x,
            pool.reserve_y + pool.protocol_fee_y,
        )
        assert int(rec["hooks_parameters"], 16) == pool.hooks_parameters
        hooks = provenance["pools"][pool_id]["hooks"]
        if hooks is not None:
            assert rec["hook_implementation"].lower() == pool.swap_hook_implementation
            assert rec["hook_implementation_code_hash"] == hooks["implementation_code_hash"]
            assert int(rec["extra_hooks_parameters"], 16) == pool.extra_hooks_parameters
            if hooks["extra"] is not None:
                assert rec["extra_hook_implementation"].lower() == EXTRA_REWARDER
                assert (
                    rec["extra_hook_implementation_code_hash"]
                    == hooks["extra"]["implementation_code_hash"]
                )
        bins = {
            int(r["id"]): (int(r["x"]), int(r["y"]))
            for r in evidence
            if r["kind"] == "bin" and r["pool"].lower() == pool_id
        }
        assert bins == dict(pool.bins), pool_id
        edges = {
            r["side"]: int(r["next"])
            for r in evidence
            if r["kind"] == "bin_edge" and r["pool"].lower() == pool_id
        }
        lo, hi = pool.bin_range
        # the member just past each end of the range lies outside it (or is the sentinel)
        assert edges["below"] == UINT24_MAX if lo == 0 else edges["below"] < lo
        assert edges["above"] == 0 if hi == UINT24_MAX else edges["above"] > hi


def _selector(reason: str) -> str:
    return "0x" + abi.keccak256_hex(f"{reason}()".encode())[2:10]


_STATUS_BY_REVERT = {
    _selector("LBPair__OutOfLiquidity"): QuoteStatus.INSUFFICIENT_LIQUIDITY,
    _selector("LBPair__InsufficientAmountOut"): QuoteStatus.INSUFFICIENT_OUTPUT_AMOUNT,
}


def _check_swap(
    state: LiquidityBookPoolState, rec: dict[str, Any]
) -> LiquidityBookPoolState | None:
    """One executed (or reverted) swap in the evidence against the migrated Python."""
    swap_for_y = rec["swap_for_y"]
    token_in = state.token0 if swap_for_y else state.token1
    amount = int(rec["amount_in"])
    assert int(rec["timestamp"]) == state.block_timestamp
    result = quote_exact_in(state, token_in, amount)
    where = f"{rec['kind']} {rec['case_id']} on {state.pool_id}"
    if rec["status"] == "revert":
        assert result.status is _STATUS_BY_REVERT[rec["revert_data"][:10]], where
        assert result.new_state is None and result.amount_out == 0
        return None
    assert result.status is QuoteStatus.OK, (where, result.detail)
    assert int(rec["credited"]) == amount  # no fee-on-transfer
    assert result.amount_out == int(rec["received"]), where
    outcome = swap(state, swap_for_y, amount)
    side = 0 if swap_for_y else 1
    expected_events = [
        (
            int(e[0]),
            int(e[1][side]),
            int(e[2][1 - side]),
            int(e[3]),
            int(e[4][side]),
            int(e[5][side]),
        )
        for e in rec["events"]
    ]
    got = [
        (b.bin_id, b.amount_in, b.amount_out, b.volatility_accumulator, b.total_fee, b.protocol_fee)
        for b in outcome.bins
    ]
    assert got == expected_events, where
    new = result.new_state
    assert new is not None and new.variable_fee is not None
    assert new.active_id == int(rec["active_id"]), where
    assert dataclasses.astuple(new.variable_fee) == _ints(rec["variable"]), where
    assert (new.reserve_x, new.reserve_y) == _ints(rec["reserves"]), where
    assert (new.protocol_fee_x, new.protocol_fee_y) == _ints(rec["protocol_fees"]), where
    for bin_id, x, y in rec["bins_after"]:
        assert new.bins[int(bin_id)] == (int(x), int(y)), (where, bin_id)
    if state.hooks_parameters & SWAP_FLAGS:
        assert int(rec["hook_storage_reads"]) > 0  # the live rewarder really ran
    if state.extra_hooks_parameters & SWAP_FLAGS:
        assert int(rec["extra_hook_storage_reads"]) > 0  # and so did its extra hook
    return new


def test_every_swap_and_sequential_followup_matches_the_deployed_pair(
    bundle: SnapshotBundle, evidence: list[dict[str, Any]]
) -> None:
    pools = _pools(bundle)
    swaps = [r for r in evidence if r["kind"] == "swap"]
    followups = {(r["case_id"], r["pool"].lower()): r for r in evidence if r["kind"] == "followup"}
    assert {(r["case_id"], r["pool"].lower()) for r in swaps} == {
        (c.case_id, p.pool_id)
        for c in bundle.cases
        for p in bundle.pools_for_pair(c.token_in, c.token_out)
    }
    directions: dict[str, set[bool]] = {pid: set() for pid in _nonempty(bundle)}
    statuses: set[str] = set()
    for rec in swaps:
        key = (rec["case_id"], rec["pool"].lower())
        case = bundle.case(rec["case_id"])
        state = pools[key[1]]
        assert rec["swap_for_y"] is (case.token_in == state.token0)
        assert int(rec["amount_in"]) == case.amount_in
        after = _check_swap(state, rec)
        statuses.add(rec["status"] if after is None else "ok")
        if after is None:
            assert key not in followups
            continue
        directions[key[1]].add(rec["swap_for_y"])
        # the second, reverse swap starts from the Python next state
        follow = followups.pop(key)
        assert follow["swap_for_y"] is not rec["swap_for_y"]
        assert int(follow["amount_in"]) == int(rec["received"]) // 2
        _check_swap(after, follow)
    assert followups == {}
    assert statuses == {"ok", "revert"}
    for pool_id in _deep(bundle):
        assert directions[pool_id] == {True, False}, pool_id


def test_unadmitted_extra_hook_is_unsupported_offline(bundle: SnapshotBundle) -> None:
    """The WHI-1433 deferred item: the pool model sees the rewarder's extra hook."""
    pool = _pools(bundle)[EXAMPLE]
    assert pool.extra_hooks_parameters & SWAP_FLAGS
    ok = quote_exact_in(pool, pool.token0, 10**18)
    assert ok.status is QuoteStatus.OK
    assert ok.features["lb_swap_hook_calls"] == 2  # the rewarder and its extra hook
    for bad in ("0x" + "11" * 20, None):
        rogue = dataclasses.replace(pool, extra_swap_hook_implementation=bad)
        result = quote_exact_in(rogue, pool.token0, 10**18)
        assert result.status is QuoteStatus.UNSUPPORTED and "extra hook" in result.detail
    # an extra-hooks word without swap flags is never called on a swap
    quiet = dataclasses.replace(
        pool,
        extra_hooks_parameters=pool.extra_hooks_parameters & ADDRESS_MASK,
        extra_swap_hook_implementation=None,
    )
    assert quote_exact_in(quiet, pool.token0, 10**18).amount_out == ok.amount_out


# ---------------------------------------------------------------------------
# 3a. Bundle- and config-level rejections
# ---------------------------------------------------------------------------


def _rewrite(bundle_dir: Path, name: str, mutate: Any) -> None:
    path = bundle_dir / name
    obj = json.loads(path.read_text())
    mutate(obj)
    text = json.dumps(obj, indent=2, sort_keys=True) + "\n"
    path.write_text(text)
    manifest = json.loads((bundle_dir / MANIFEST_FILE).read_text())
    manifest["checksums"][name] = sha256_bytes(text.encode())
    (bundle_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True))


def _example_record(obj: dict[str, Any]) -> dict[str, Any]:
    rec = next(p for p in obj["pools"] if p["pool_id"] == EXAMPLE)
    assert isinstance(rec, dict)
    return rec


def _empty_record(obj: dict[str, Any]) -> dict[str, Any]:
    rec = next(p for p in obj["pools"] if p["bin_range"] == [0, UINT24_MAX])
    assert isinstance(rec, dict)
    return rec


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda o: _example_record(o).update(block_timestamp=1), "block's timestamp"),
        (lambda o: _example_record(o).update(source_key="moe_classic_v1"), "source_key"),
        (lambda o: _example_record(o)["read_at"].update(block_number=1), "never mixes blocks"),
        (lambda o: _example_record(o).update(active_id=0), "active_id"),
        (lambda o: _example_record(o)["bins"][0].update(id=0), r"bins\[0\].id"),
        (lambda o: _example_record(o)["bins"][0].update(x="0", y="0"), "is empty"),
        (lambda o: _example_record(o)["bins"].append(_example_record(o)["bins"][0]), "duplicate"),
        (lambda o: _example_record(o)["static_fee"].update(base_factor=1 << 16), "base_factor"),
        (lambda o: _example_record(o).pop("extra_hooks_parameters"), "missing"),
        (lambda o: _empty_record(o).update(reserve_x="1"), "whole tree holds"),
    ],
)
def test_loader_rejects_malformed_lb_state(tmp_path: Path, mutate: Any, match: str) -> None:
    bundle_dir = tmp_path / "b"
    shutil.copytree(FIXTURE_BUNDLE, bundle_dir)
    _rewrite(bundle_dir, POOLS_FILE, mutate)
    with pytest.raises(BundleError, match=match):
        load_bundle(bundle_dir)


@pytest.mark.parametrize(
    "key,mutate,match",
    [
        (
            "moe_classic_v1",
            lambda s: s.update(lb_collection={"admitted": True}),
            "only valid for liquidity_book_v2",
        ),
        (
            "moe_lb_v2_2",
            lambda s: s["lb_collection"]["swap_hooks"].append(s["lb_collection"]["swap_hooks"][0]),
            "duplicate implementation",
        ),
        (
            "moe_lb_v2_2",
            lambda s: s["lb_collection"]["extra_swap_hooks"][0].pop("code_hash"),
            "missing",
        ),
        ("moe_lb_v2_2", lambda s: s.update(sor_protocol="V2"), "routes only"),
    ],
)
def test_catalog_rejects_malformed_lb_admission(key: str, mutate: Any, match: str) -> None:
    import yaml

    raw = yaml.safe_load((REPO / "config" / "protocols.yaml").read_text())
    mutate(next(s for s in raw["sources"] if s["key"] == key))
    with pytest.raises(ConfigError, match=match):
        parse_catalog(raw)


def test_prepare_config_rejects_unreasoned_exclusions_and_bad_bounds() -> None:
    import yaml

    raw = yaml.safe_load(PREPARE_CONFIG.read_text())
    bad = json.loads(json.dumps(raw))
    bad["pairs"][0]["excluded_bin_steps"] = [{"bin_step": 50, "reason": " "}]
    with pytest.raises(PrepareConfigError, match="written reason"):
        parse_lb_prepare_config(bad, source_path="x", sha256="0")
    bad = json.loads(json.dumps(raw))
    bad["collection"]["max_bins_per_direction"] = 0
    with pytest.raises(PrepareConfigError, match="max_bins_per_direction"):
        parse_lb_prepare_config(bad, source_path="x", sha256="0")


# ---------------------------------------------------------------------------
# 3b. Collector mechanics against a scripted node
# ---------------------------------------------------------------------------


def _word(value: int) -> str:
    return format(value, "064x")


def _ret(*values: int) -> str:
    return "0x" + "".join(_word(v) for v in values)


def _pair_data(p: LiquidityBookPoolState) -> bytes:
    return bytes.fromhex(p.token0[2:] + p.token1[2:]) + p.bin_step.to_bytes(2, "big")


class FakeLBChain:
    """A JSON-RPC node serving the fixture's LB pairs at one block, from the bundle and
    its provenance only. Every state read must be EIP-1898 pinned to the block hash;
    `getNextNonEmptyBin` answers only inside each pair's collected `bin_range`."""

    def __init__(self, bundle: SnapshotBundle, provenance: dict[str, Any]) -> None:
        self.pools = dict(_pools(bundle))
        self.block_number = bundle.block.number
        self.block_hash = bundle.block.hash
        self.hashes_by_number = [bundle.block.hash]
        self.timestamp = bundle.block.timestamp
        self.discovery = provenance["discovery"]["admitted"]
        self.factory_code = b"\x60\x01lbfactory"
        self.impl_code = b"\x60\x02lbpair"
        self.hook_impl_code = {REWARDER: b"\x60\x03rewarder", EXTRA_REWARDER: b"\x60\x04extra"}
        self.impl_answer = IMPLEMENTATION
        self.decimals = dict(DECIMALS)
        self.balance_delta: dict[tuple[str, str], int] = {}
        self.code_patch: dict[str, bytes] = {}
        self.getter_patch: dict[tuple[str, str], int] = {}
        self.info_patch: dict[str, str] = {}
        self.parent_patch: dict[str, str] = {}
        self.extra_patch: dict[str, int] = {}
        self.hooks: dict[str, tuple[str, bytes]] = {}  # clone -> (impl, args)
        self.hook_of: dict[str, str] = {}
        self.extra_of: dict[str, int] = {}
        for pool_id, record in provenance["pools"].items():
            h = record["hooks"]
            if h is None:
                continue
            self.hooks[h["address"]] = (h["implementation"], bytes.fromhex(h["clone_args"][2:]))
            self.hook_of[h["address"]] = pool_id
            self.extra_of[h["address"]] = int(h["extra_hooks_parameters"], 16)
            if h["extra"] is not None:
                e = h["extra"]
                self.hooks[e["address"]] = (
                    e["implementation"],
                    bytes.fromhex(e["clone_args"][2:]),
                )
                self.hook_of[e["address"]] = pool_id
        self.log: list[tuple[str, list[Any]]] = []

    def _check_pin(self, pin: Any) -> None:
        assert pin == {"blockHash": self.block_hash, "requireCanonical": True}, pin

    def _header(self, block_hash: str) -> dict[str, str]:
        return {
            "number": hex(self.block_number),
            "hash": block_hash,
            "timestamp": hex(self.timestamp),
        }

    def code(self, address: str) -> bytes:
        if address in self.code_patch:
            return self.code_patch[address]
        if address == FACTORY:
            return self.factory_code
        if address == IMPLEMENTATION:
            return self.impl_code
        if address in self.hook_impl_code:
            return self.hook_impl_code[address]
        if address in self.hooks:
            impl, args = self.hooks[address]
            return clone_runtime_code(impl, args)
        return clone_runtime_code(IMPLEMENTATION, _pair_data(self.pools[address]))

    def call(self, method: str, params: list[Any]) -> Any:
        self.log.append((method, params))
        if method == "eth_chainId":
            return hex(5000)
        if method == "eth_getBlockByNumber":
            if params[0] == "finalized":
                return {"number": hex(self.block_number + 100), "hash": "0x" + "00" * 32}
            h = (
                self.hashes_by_number.pop(0)
                if len(self.hashes_by_number) > 1
                else self.hashes_by_number[0]
            )
            return self._header(h)
        if method == "eth_getBlockByHash":
            return self._header(params[0])
        if method == "eth_getCode":
            self._check_pin(params[1])
            return "0x" + self.code(params[0].lower()).hex()
        if method == "eth_call":
            self._check_pin(params[1])
            return self._eth_call(params[0]["to"].lower(), params[0]["data"])
        raise AssertionError(f"unscripted RPC method {method}")

    def call_batch(self, calls: Any) -> list[Any]:
        return [self.call(m, p) for m, p in calls]

    def _eth_call(self, to: str, data: str) -> str:
        sel, args = data[2:10], data[10:]
        argv = [int(args[i : i + 64], 16) for i in range(0, len(args), 64)]
        if to == FACTORY:
            return self._factory(sel, argv)
        if to in DECIMALS:
            if sel == abi.SEL_DECIMALS:
                return _ret(self.decimals[to])
            assert sel == abi.SEL_BALANCE_OF, sel
            holder = f"0x{argv[0]:040x}"
            p = self.pools[holder]
            owed = (
                p.reserve_x + p.protocol_fee_x if to == p.token0 else p.reserve_y + p.protocol_fee_y
            )
            return _ret(owed + self.balance_delta.get((holder, to), 0))
        if to in self.hooks:
            return self._hook(to, sel)
        return self._pair(self.pools[to], sel, argv)

    def _factory(self, sel: str, argv: list[int]) -> str:
        if sel == abi.SEL_GET_LB_PAIR_IMPLEMENTATION:
            return _ret(int(self.impl_answer, 16))
        a, b = sorted((f"0x{argv[0]:040x}", f"0x{argv[1]:040x}"))
        found = [d for d in self.discovery if tuple(sorted((d["token_x"], d["token_y"]))) == (a, b)]
        if sel == abi.SEL_GET_ALL_LB_PAIRS:
            words = [0x20, len(found)]
            for d in found:
                words += [
                    d["bin_step"],
                    int(d["pool_id"], 16),
                    int(d["created_by_owner"]),
                    int(d["ignored_for_routing"]),
                ]
            return _ret(*words)
        assert sel == abi.SEL_GET_LB_PAIR_INFORMATION, sel
        d = next(d for d in found if d["bin_step"] == argv[2])
        pool_id = self.info_patch.get(d["pool_id"], d["pool_id"])
        return _ret(
            d["bin_step"],
            int(pool_id, 16),
            int(d["created_by_owner"]),
            int(d["ignored_for_routing"]),
        )

    def _hook(self, to: str, sel: str) -> str:
        pool_id = self.hook_of[to]
        if sel == abi.SEL_GET_LB_PAIR:
            return _ret(int(pool_id, 16))
        if sel == abi.SEL_GET_EXTRA_HOOKS_PARAMETERS:
            return _ret(self.extra_patch.get(to, self.extra_of[to]))
        assert sel == abi.SEL_GET_PARENT_REWARDER, sel
        parent = f"0x{self.pools[pool_id].hooks_parameters & ADDRESS_MASK:040x}"
        return _ret(int(self.parent_patch.get(to, parent), 16))

    def _pair(self, p: LiquidityBookPoolState, sel: str, argv: list[int]) -> str:
        assert p.static_fee is not None and p.variable_fee is not None
        answers = {
            abi.SEL_GET_TOKEN_X: int(p.token0, 16),
            abi.SEL_GET_TOKEN_Y: int(p.token1, 16),
            abi.SEL_GET_FACTORY: int(FACTORY, 16),
            abi.SEL_IMPLEMENTATION: int(IMPLEMENTATION, 16),
            abi.SEL_GET_BIN_STEP: p.bin_step,
            abi.SEL_GET_ACTIVE_ID: p.active_id,
            abi.SEL_GET_LB_HOOKS_PARAMETERS: p.hooks_parameters,
        }
        if sel in answers:
            return _ret(self.getter_patch.get((p.pool_id, sel), answers[sel]))
        if sel == abi.SEL_GET_RESERVES:
            return _ret(p.reserve_x, p.reserve_y)
        if sel == abi.SEL_GET_PROTOCOL_FEES:
            return _ret(p.protocol_fee_x, p.protocol_fee_y)
        if sel == abi.SEL_GET_STATIC_FEE_PARAMETERS:
            return _ret(*dataclasses.astuple(p.static_fee))
        if sel == abi.SEL_GET_VARIABLE_FEE_PARAMETERS:
            return _ret(*dataclasses.astuple(p.variable_fee))
        if sel == abi.SEL_GET_BIN:
            return _ret(*p.bins.get(argv[0], (0, 0)))
        assert sel == abi.SEL_GET_NEXT_NON_EMPTY_BIN, sel
        below, frontier = bool(argv[0]), argv[1]
        lo, hi = p.bin_range
        if below:
            members = [i for i in p.bins if i < frontier]
            if members:
                return _ret(max(members))
            assert lo == 0, f"walk below the collected range of {p.pool_id}"
            return _ret(UINT24_MAX)
        members = [i for i in p.bins if i > frontier]
        if members:
            return _ret(min(members))
        assert hi == UINT24_MAX, f"walk above the collected range of {p.pool_id}"
        return _ret(0)


def _fake_catalog(chain: FakeLBChain) -> ProtocolCatalog:
    contracts = dict(LB.contracts)
    contracts["factory"] = ContractRef(FACTORY, abi.keccak256_hex(chain.factory_code))
    contracts["pair_implementation"] = ContractRef(
        IMPLEMENTATION, abi.keccak256_hex(chain.impl_code)
    )
    lb = LB.lb_collection
    assert lb is not None
    lb = dataclasses.replace(
        lb,
        swap_hooks=tuple(
            dataclasses.replace(h, code_hash=abi.keccak256_hex(chain.hook_impl_code[REWARDER]))
            for h in lb.swap_hooks
        ),
        extra_swap_hooks=tuple(
            dataclasses.replace(
                h, code_hash=abi.keccak256_hex(chain.hook_impl_code[EXTRA_REWARDER])
            )
            for h in lb.extra_swap_hooks
        ),
    )
    source = dataclasses.replace(LB, contracts=contracts, lb_collection=lb)
    return dataclasses.replace(
        CATALOG,
        sources=tuple(source if s.key == "moe_lb_v2_2" else s for s in CATALOG.sources),
    )


@pytest.fixture()
def chain(bundle: SnapshotBundle, provenance: dict[str, Any]) -> FakeLBChain:
    return FakeLBChain(bundle, provenance)


def _collector(
    chain: FakeLBChain, config: LBPrepareConfig | None = None, **kwargs: Any
) -> LBCollector:
    return LBCollector(
        catalog=kwargs.pop("catalog", _fake_catalog(chain)),
        source_key=kwargs.pop("source_key", "moe_lb_v2_2"),
        config=config or load_lb_prepare_config(PREPARE_CONFIG),
        transport=chain,
        block_number=chain.block_number,
        **kwargs,
    )


def _normalized(prov: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(json.dumps(prov))
    for key in ("rpc_endpoint", "catalog"):
        del out[key]
    out["prepare_config"]["path"] = Path(out["prepare_config"]["path"]).name
    for record in out["pools"].values():
        for hook in (record["hooks"], (record["hooks"] or {}).get("extra")):
            if hook:
                del hook["implementation_code_hash"]
    return out


def test_collector_reproduces_the_fixture_state_from_pinned_reads(
    chain: FakeLBChain, provenance: dict[str, Any], tmp_path: Path
) -> None:
    collected = _collector(chain, expected_block_hash=chain.block_hash).collect()
    out = publish(tmp_path / "bundle", collected)
    for name in (POOLS_FILE, "cases.jsonl"):
        assert (tmp_path / "bundle" / name).read_bytes() == (FIXTURE_BUNDLE / name).read_bytes()
    assert out.bundle_id == load_bundle(FIXTURE_BUNDLE).bundle_id
    # everything but what the scripted node fakes (RPC label, the placeholder factory /
    # implementation / hook-implementation code) is reproduced
    assert _normalized(collected.provenance) == _normalized(provenance)
    assert all(json.dumps(p).count("latest") == 0 for _, p in chain.log)
    assert [m for m, _ in chain.log][-2:] == ["eth_getBlockByHash", "eth_getBlockByNumber"]


@pytest.mark.parametrize("source_key", ["moe_classic_v1", "uniswap_v3", "moe_lb_v2_1"])
def test_wrong_source_is_refused(chain: FakeLBChain, source_key: str) -> None:
    with pytest.raises(PrepareError, match="invalid_request"):
        _collector(chain, source_key=source_key)


def test_unadmitted_or_unmodeled_catalog_entry_is_refused(chain: FakeLBChain) -> None:
    catalog = _fake_catalog(chain)
    lb = catalog.source("moe_lb_v2_2").lb_collection
    assert lb is not None
    rogue = dataclasses.replace(lb.extra_swap_hooks[0], implementation="0x" + "ab" * 20)
    for entry, match in (
        (dataclasses.replace(lb, admitted=False), "source_not_admitted"),
        (None, "source_not_admitted"),
        (dataclasses.replace(lb, extra_swap_hooks=(rogue,)), "config_incomplete.*extra_swap"),
    ):
        bad = dataclasses.replace(
            catalog,
            sources=tuple(
                dataclasses.replace(s, lb_collection=entry) if s.key == "moe_lb_v2_2" else s
                for s in catalog.sources
            ),
        )
        with pytest.raises(PrepareError, match=match):
            _collector(chain, catalog=bad)


def test_foreign_factory_or_implementation_is_rejected(chain: FakeLBChain) -> None:
    catalog = _fake_catalog(chain)
    chain.factory_code = b"\x60\x09otherfactory"
    with pytest.raises(PrepareError, match="code_hash_mismatch: factory"):
        _collector(chain, catalog=catalog).collect()
    chain.factory_code = b"\x60\x01lbfactory"
    chain.impl_answer = "0x" + "ab" * 20
    with pytest.raises(PrepareError, match="identity_mismatch.*getLBPairImplementation"):
        _collector(chain).collect()


def _victim(chain: FakeLBChain) -> LiquidityBookPoolState:
    return next(p for pid, p in chain.pools.items() if pid != EXAMPLE and p.bins)


@pytest.mark.parametrize("patch", ["other_implementation", "swapped_args", "classic_clone"])
def test_pair_code_that_is_not_the_verified_clone_is_rejected(
    chain: FakeLBChain, patch: str
) -> None:
    victim = _victim(chain)
    data = _pair_data(victim)
    chain.code_patch[victim.pool_id] = {
        "other_implementation": clone_runtime_code("0x" + "cd" * 20, data),
        "swapped_args": clone_runtime_code(IMPLEMENTATION, data[20:40] + data[:20] + data[40:]),
        "classic_clone": clone_runtime_code(IMPLEMENTATION, data[:40]),
    }[patch]
    with pytest.raises(PrepareError, match="code_hash_mismatch|identity_mismatch.*CREATE2"):
        _collector(chain).collect()
    assert not any(m == "eth_call" and p[0]["to"].lower() == victim.pool_id for m, p in chain.log)


def test_factory_round_trip_and_getters_must_agree(chain: FakeLBChain) -> None:
    victim = _victim(chain)
    chain.info_patch[victim.pool_id] = EXAMPLE
    with pytest.raises(PrepareError, match="identity_mismatch.*getLBPairInformation"):
        _collector(chain).collect()
    chain.info_patch.clear()
    chain.getter_patch[(victim.pool_id, abi.SEL_GET_BIN_STEP)] = victim.bin_step + 1
    with pytest.raises(PrepareError, match="identity_mismatch.*getBinStep"):
        _collector(chain).collect()


def _hooked(chain: FakeLBChain, *, extra: bool) -> tuple[LiquidityBookPoolState, str]:
    pool = next(
        p
        for p in chain.pools.values()
        if p.hooks_parameters & SWAP_FLAGS and bool(p.extra_hooks_parameters & SWAP_FLAGS) is extra
    )
    return pool, f"0x{pool.hooks_parameters & ADDRESS_MASK:040x}"


def test_unapproved_swap_hook_is_refused(chain: FakeLBChain) -> None:
    pool, hook = _hooked(chain, extra=False)
    chain.code_patch[hook] = clone_runtime_code("0x" + "ee" * 20, chain.hooks[hook][1])
    with pytest.raises(PrepareError, match="unsupported_hook.*swap hook"):
        _collector(chain).collect()
    chain.code_patch.clear()
    chain.getter_patch[(pool.pool_id, abi.SEL_GET_LB_HOOKS_PARAMETERS)] = 1 << 160
    with pytest.raises(PrepareError, match="unsupported_hook.*no hooks address"):
        _collector(chain).collect()


def test_unapproved_or_unbound_extra_hook_is_refused(chain: FakeLBChain) -> None:
    """The WHI-1433 deferred item: the rewarder's own extra hook is read and must be an
    approved, verified implementation bound to this pair and rewarder."""
    pool, hook = _hooked(chain, extra=True)
    extra = f"0x{pool.extra_hooks_parameters & ADDRESS_MASK:040x}"
    impl, args = chain.hooks[extra]
    chain.code_patch[extra] = clone_runtime_code("0x" + "ee" * 20, args)
    with pytest.raises(PrepareError, match="unsupported_hook.*extra hook"):
        _collector(chain).collect()
    chain.code_patch = {extra: clone_runtime_code(impl, args[:40] + bytes(20))}
    with pytest.raises(PrepareError, match="identity_mismatch.*do not name rewarder"):
        _collector(chain).collect()
    chain.code_patch.clear()
    chain.parent_patch[extra] = "0x" + "12" * 20
    with pytest.raises(PrepareError, match="identity_mismatch.*getParentRewarder"):
        _collector(chain).collect()
    chain.parent_patch.clear()
    stale = _fake_catalog(chain)  # pins the untampered implementation
    chain.hook_impl_code[EXTRA_REWARDER] = b"\x60\x04tampered"
    with pytest.raises(PrepareError, match="code_hash_mismatch.*extra hook implementation"):
        _collector(chain, catalog=stale).collect()


def test_balance_drift_and_wrong_decimals_are_refused(chain: FakeLBChain) -> None:
    victim = _victim(chain)
    chain.balance_delta[(victim.pool_id, victim.token1)] = 1  # a pending donation
    with pytest.raises(PrepareError, match="unsupported_token_behavior"):
        _collector(chain).collect()
    chain.balance_delta.clear()
    chain.decimals[USDC] = 18
    with pytest.raises(PrepareError, match="identity_mismatch.*decimals"):
        _collector(chain).collect()


def test_envelope_beyond_the_walk_bound_is_incomplete_not_truncated(
    chain: FakeLBChain, tmp_path: Path
) -> None:
    config = load_lb_prepare_config(PREPARE_CONFIG)
    tight = dataclasses.replace(
        config, limits=dataclasses.replace(config.limits, max_bins_per_direction=8)
    )
    with pytest.raises(PrepareError, match="incomplete_snapshot.*truncated envelope"):
        publish(tmp_path / "bundle", _collector(chain, tight).collect())
    assert list(tmp_path.iterdir()) == []


def test_exclusion_rule_omits_an_unprovable_pair_instead_of_refusing(
    chain: FakeLBChain,
) -> None:
    """WHI-1436: under the corpus's declared rule, a pair whose envelope needs more bins
    than the bound is excluded and recorded (rule id, direction, bound), not truncated."""
    config = load_lb_prepare_config(PREPARE_CONFIG)
    rule = "envelope-unprovable-within-read-bound"
    tight = dataclasses.replace(
        config,
        limits=dataclasses.replace(config.limits, max_bins_per_direction=8, exclusion_rule=rule),
    )
    collected = _collector(chain, tight).collect()
    excluded = collected.provenance["exclusions"]["pools"]
    assert excluded and collected.provenance["exclusions"]["rule"] == rule
    kept = {p.pool_id for p in collected.pools}
    for record in excluded:
        assert record["pool_id"] not in kept and record["rule"] == rule
        assert record["bound"] == {"max_bins_per_direction": 8}
        assert record in collected.provenance["discovery"]["omitted"]
        assert record["pool_id"] not in collected.provenance["pools"]
    assert kept


def test_exclusions_are_reasoned_omissions(chain: FakeLBChain) -> None:
    config = load_lb_prepare_config(PREPARE_CONFIG)
    usdt_wmnt = next(p for p in config.pairs if {p.token0, p.token1} == {USDT, WMNT})
    excluded = LBPairSpec(
        usdt_wmnt.token0, usdt_wmnt.token1, excluded_bin_steps=((25, "illustrative exclusion"),)
    )
    trimmed = dataclasses.replace(
        config, pairs=tuple(excluded if p is usdt_wmnt else p for p in config.pairs)
    )
    collected = _collector(chain, trimmed).collect()
    omitted = [o for o in collected.provenance["discovery"]["omitted"] if o["bin_step"] == 25]
    assert omitted == [
        {
            "token0": usdt_wmnt.token0,
            "token1": usdt_wmnt.token1,
            "bin_step": 25,
            "pool_id": "0x365722f12ceb2063286a268b03c654df81b7c00f",
            "reason": "excluded by prepare config: illustrative exclusion",
        }
    ]
    assert "0x365722f12ceb2063286a268b03c654df81b7c00f" not in {p.pool_id for p in collected.pools}
    phantom = LBPairSpec(usdt_wmnt.token0, usdt_wmnt.token1, excluded_bin_steps=((7, "none"),))
    with pytest.raises(PrepareError, match="invalid_request.*does not list"):
        _collector(
            chain,
            dataclasses.replace(
                config, pairs=tuple(phantom if p is usdt_wmnt else p for p in config.pairs)
            ),
        ).collect()


def test_changing_block_hash_during_collection_aborts_publication(
    chain: FakeLBChain, tmp_path: Path
) -> None:
    chain.hashes_by_number = [chain.block_hash, "0x" + "ee" * 32]
    with pytest.raises(PrepareError, match="block_hash_mismatch.*not published"):
        publish(tmp_path / "bundle", _collector(chain).collect())
    assert list(tmp_path.iterdir()) == []


def test_prepare_requires_an_explicit_block(tmp_path: Path) -> None:
    with pytest.raises(PrepareError, match="explicit --block"):
        COLLECTORS["moe_lb"](PrepareRequest(output_dir=tmp_path / "b"))


# ---------------------------------------------------------------------------
# 4. Offline replay through the CLI
# ---------------------------------------------------------------------------


def test_validate_and_run_replay_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: list[dict[str, Any]]
) -> None:
    _block_network(monkeypatch)
    bundle_dir = tmp_path / "bundle"
    shutil.copytree(FIXTURE_BUNDLE, bundle_dir)
    assert main.main(["validate", "--bundle", str(bundle_dir)]) == 0
    results = tmp_path / "results"
    argv = ["run", "--bundle", str(bundle_dir), "--profile", str(SMOKE_PROFILE)]
    argv += ["--strategies", "profile"]  # the smoke profile's exact selection
    assert main.main([*argv, "--results-dir", str(results)]) == 0
    (run_dir,) = results.iterdir()
    records = [json.loads(line) for line in (run_dir / "cases.jsonl").read_text().splitlines()]
    replayed = load_bundle(bundle_dir)
    # the best executed fork output over the case's pairs
    best: dict[str, int] = {}
    for r in evidence:
        if r["kind"] == "swap" and r["status"] == "ok":
            best[r["case_id"]] = max(best.get(r["case_id"], 0), int(r["received"]))
    assert len(records) == len(replayed.cases)
    for rec in records:
        if rec["case_id"] not in best:  # every pair reverts: exhausted book or dust
            assert rec["status"] == "no_route", rec
            continue
        assert rec["status"] == "ok", rec
        assert int(rec["evaluation"]["gross_output"]) == best[rec["case_id"]]
    assert {r["status"] for r in records} == {"ok", "no_route"}
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["bundle_hash"] == replayed.bundle_hash

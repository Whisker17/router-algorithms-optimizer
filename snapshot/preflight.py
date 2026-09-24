"""Executable preflight for the five-source admission catalog (docs/DESIGN.md §§2.2-2.3).

`run_preflight` takes a loaded `ProtocolCatalog` (see `snapshot.config`) and an
`RpcTransport` (see `snapshot.rpc`) and re-derives, from live (or fake, in tests)
chain reads, whether the catalog's chain/block/contract claims still hold:

- wrong chain            -> `eth_chainId` != `network.chain_id`
- block mismatch         -> `eth_getBlockByNumber(candidate_block.number)` hash/
                             existence disagrees with the frozen `candidate_block`
- unavailable required   -> missing code at a configured contract address, or a
  state                     read that the catalog depends on reverts/returns
                             empty, or a round-trip identity check disagrees

None of these ever "fall back": a failed read is recorded as an issue, never
silently retried against `latest`, a different block, or a different endpoint.
Known, already-investigated gaps (e.g. "explorer verified-source requires a paid
API key we do not have") are *config-declared blockers*, reported separately from
freshly-detected issues so a real regression is never mistaken for old, accepted
news (or vice versa).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from snapshot import abi
from snapshot.config import CandidateBlock, ContractRef, ProtocolCatalog, SourceConfig, load_catalog
from snapshot.rpc import HttpJsonRpcTransport, RpcError, RpcTransport, RpcTransportError


@dataclass(frozen=True)
class PreflightIssue:
    severity: str  # "error" | "info"
    code: str
    message: str
    source_key: str | None = None
    contract_role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "source_key": self.source_key,
            "contract_role": self.contract_role,
        }


@dataclass
class PreflightReport:
    rpc_url: str
    observed_chain_id: int | None = None
    observed_block: dict[str, Any] | None = None
    issues: list[PreflightIssue] = field(default_factory=list)
    known_blockers: list[dict[str, Any]] = field(default_factory=list)
    checked_sources: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "rpc_url": self.rpc_url,
            "observed_chain_id": self.observed_chain_id,
            "observed_block": self.observed_block,
            "issues": [issue.to_dict() for issue in self.issues],
            "known_blockers": self.known_blockers,
            "checked_sources": self.checked_sources,
        }


class PreflightFailed(RuntimeError):
    """Raised by `run_preflight_or_raise` when the report is not `ok`."""

    def __init__(self, report: PreflightReport) -> None:
        self.report = report
        error_summary = "; ".join(
            f"{i.code}: {i.message}" for i in report.issues if i.severity == "error"
        )
        super().__init__(f"preflight rejected: {error_summary}")


def redact_url(url: str) -> str:
    """Strip credentials/query string before an endpoint URL is ever written to
    output; the public default has none, but overrides might (docs/DESIGN.md §2.12
    forbids serializing credential-bearing RPC overrides into artifacts)."""
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _to_block_param(block_number: int) -> str:
    return hex(block_number)


def _call(
    transport: RpcTransport,
    to: str,
    selector_and_args: str,
    block_param: str,
) -> tuple[str | None, PreflightIssue | None]:
    try:
        result = transport.call("eth_call", [{"to": to, "data": selector_and_args}, block_param])
        return result, None
    except RpcError as exc:
        return None, PreflightIssue(
            severity="error",
            code="call_reverted",
            message=f"eth_call to {to} data={selector_and_args[:10]}... reverted: {exc.message}",
        )
    except RpcTransportError as exc:
        return None, PreflightIssue(
            severity="error", code="rpc_unavailable", message=f"eth_call to {to} failed: {exc}"
        )


def check_chain_id(
    transport: RpcTransport, expected_chain_id: int
) -> tuple[int | None, PreflightIssue | None]:
    try:
        raw = transport.call("eth_chainId", [])
    except (RpcError, RpcTransportError) as exc:
        return None, PreflightIssue(
            severity="error", code="rpc_unavailable", message=f"eth_chainId failed: {exc}"
        )
    observed = int(raw, 16)
    if observed != expected_chain_id:
        return observed, PreflightIssue(
            severity="error",
            code="wrong_chain",
            message=f"expected chain_id={expected_chain_id}, RPC reports {observed}",
        )
    return observed, None


def check_candidate_block(
    transport: RpcTransport, candidate: CandidateBlock
) -> tuple[dict[str, Any] | None, list[PreflightIssue]]:
    issues: list[PreflightIssue] = []
    try:
        block = transport.call(
            "eth_getBlockByNumber", [_to_block_param(candidate.number), False]
        )
    except (RpcError, RpcTransportError) as exc:
        return None, [
            PreflightIssue(
                severity="error",
                code="block_unavailable",
                message=f"eth_getBlockByNumber({candidate.number}) failed: {exc}",
            )
        ]
    if block is None:
        return None, [
            PreflightIssue(
                severity="error",
                code="block_unavailable",
                message=f"block {candidate.number} not returned by RPC (pruned/unmined?)",
            )
        ]
    observed_hash = block.get("hash")
    if observed_hash is None or observed_hash.lower() != candidate.hash.lower():
        issues.append(
            PreflightIssue(
                severity="error",
                code="block_hash_mismatch",
                message=(
                    f"block {candidate.number}: expected hash {candidate.hash}, "
                    f"RPC reports {observed_hash} (reorg, wrong chain, or stale catalog)"
                ),
            )
        )
    observed_timestamp = int(block["timestamp"], 16) if block.get("timestamp") else None
    if observed_timestamp is not None and observed_timestamp != candidate.timestamp:
        issues.append(
            PreflightIssue(
                severity="error",
                code="block_timestamp_mismatch",
                message=(
                    f"block {candidate.number}: expected timestamp {candidate.timestamp}, "
                    f"RPC reports {observed_timestamp}"
                ),
            )
        )
    return block, issues


def _check_code_present(
    transport: RpcTransport, block_param: str, ref: ContractRef, source_key: str, role: str
) -> tuple[int, list[PreflightIssue]]:
    """Verify code is present, and — when the catalog pins one — that its
    Keccak-256 still matches. A hash mismatch is reported distinctly from missing
    code entirely: the former is a live drift/substitution, the latter is an
    address/chain/pruning problem."""
    issues: list[PreflightIssue] = []
    try:
        code = transport.call("eth_getCode", [ref.address, block_param])
    except (RpcError, RpcTransportError) as exc:
        return 0, [
            PreflightIssue(
                severity="error",
                code="rpc_unavailable",
                message=f"eth_getCode({ref.address}) failed: {exc}",
                source_key=source_key,
                contract_role=role,
            )
        ]
    size = abi.byte_length(code)
    if size == 0:
        return 0, [
            PreflightIssue(
                severity="error",
                code="missing_code",
                message=f"no bytecode at {ref.address} ({role}) for {source_key} at this block",
                source_key=source_key,
                contract_role=role,
            )
        ]
    if ref.code_hash is not None:
        observed_hash = abi.code_hash(code)
        if observed_hash is None or observed_hash.lower() != ref.code_hash.lower():
            issues.append(
                PreflightIssue(
                    severity="error",
                    code="code_hash_mismatch",
                    message=(
                        f"{ref.address} ({role}) code hash {observed_hash} != pinned "
                        f"{ref.code_hash} (bytecode changed since the catalog was written)"
                    ),
                    source_key=source_key,
                    contract_role=role,
                )
            )
    return size, issues


def _check_all_contract_code(
    transport: RpcTransport, block_param: str, source: SourceConfig
) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    for role, ref in source.contracts.items():
        _, ref_issues = _check_code_present(transport, block_param, ref, source.key, role)
        issues.extend(ref_issues)
    return issues


def _check_v3_family(
    transport: RpcTransport, block_param: str, source: SourceConfig
) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    factory_ref = source.contracts.get("factory")
    pool_ref = source.contracts.get("example_pool")
    if factory_ref is None or pool_ref is None:
        return [
            PreflightIssue(
                severity="error",
                code="config_incomplete",
                message="v3_concentrated_liquidity source needs 'factory' and 'example_pool'",
                source_key=source.key,
            )
        ]
    factory, example_pool = factory_ref.address, pool_ref.address

    issues.extend(_check_all_contract_code(transport, block_param, source))
    if issues:
        return issues  # missing/mismatched code makes every further call meaningless

    # pool.factory() must round-trip to the configured factory.
    result, issue = _call(transport, example_pool, abi.encode_call(abi.SEL_FACTORY), block_param)
    if issue:
        issues.append(issue)
    elif abi.decode_address(result) is None or abi.decode_address(
        result
    ).lower() != factory.lower():  # type: ignore[union-attr]
        issues.append(
            PreflightIssue(
                severity="error",
                code="identity_mismatch",
                message=f"{example_pool}.factory() != configured factory {factory}",
                source_key=source.key,
                contract_role="example_pool",
            )
        )

    token0_addr = source.tokens.get("token0")
    token1_addr = source.tokens.get("token1")
    fee = source.pool_fee
    if token0_addr and token1_addr and fee is not None:
        args = abi.pad_address(token0_addr) + abi.pad_address(token1_addr) + abi.pad_uint(int(fee))
        result, issue = _call(
            transport, factory, abi.encode_call(abi.SEL_GET_POOL, args), block_param
        )
        if issue:
            issues.append(issue)
        else:
            round_trip = abi.decode_address(result)
            if round_trip is None or round_trip.lower() != example_pool.lower():
                issues.append(
                    PreflightIssue(
                        severity="error",
                        code="identity_mismatch",
                        message=(
                            f"{factory}.getPool(token0,token1,fee) returned {round_trip}, "
                            f"expected {example_pool}"
                        ),
                        source_key=source.key,
                        contract_role="factory",
                    )
                )

    if source.expected_fee_tiers:
        for fee_amount, expected_spacing in source.expected_fee_tiers.items():
            result, issue = _call(
                transport,
                factory,
                abi.encode_call(abi.SEL_FEE_AMOUNT_TICK_SPACING, abi.pad_uint(fee_amount)),
                block_param,
            )
            if issue:
                issues.append(issue)
                continue
            observed_spacing = abi.decode_int24(result)
            if observed_spacing != expected_spacing:
                issues.append(
                    PreflightIssue(
                        severity="error",
                        code="fee_tier_mismatch",
                        message=(
                            f"{factory}.feeAmountTickSpacing({fee_amount}) = "
                            f"{observed_spacing}, expected {expected_spacing}"
                        ),
                        source_key=source.key,
                        contract_role="factory",
                    )
                )
    return issues


def _check_v2_classic_family(
    transport: RpcTransport, block_param: str, source: SourceConfig
) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    factory_ref = source.contracts.get("factory")
    pool_ref = source.contracts.get("example_pool")
    if factory_ref is None or pool_ref is None:
        return [
            PreflightIssue(
                severity="error",
                code="config_incomplete",
                message="v2_classic source needs 'factory' and 'example_pool'",
                source_key=source.key,
            )
        ]
    factory, example_pool = factory_ref.address, pool_ref.address

    issues.extend(_check_all_contract_code(transport, block_param, source))
    if issues:
        return issues

    result, issue = _call(transport, example_pool, abi.encode_call(abi.SEL_FACTORY), block_param)
    if issue:
        issues.append(issue)
    elif abi.decode_address(result) is None or abi.decode_address(
        result
    ).lower() != factory.lower():  # type: ignore[union-attr]
        issues.append(
            PreflightIssue(
                severity="error",
                code="identity_mismatch",
                message=f"{example_pool}.factory() != configured factory {factory}",
                source_key=source.key,
                contract_role="example_pool",
            )
        )

    result, issue = _call(
        transport, example_pool, abi.encode_call(abi.SEL_GET_RESERVES), block_param
    )
    if issue:
        issues.append(issue)
    elif not result or abi.byte_length(result) < 96:
        issues.append(
            PreflightIssue(
                severity="error",
                code="missing_state",
                message=f"{example_pool}.getReserves() did not return (reserve0, reserve1, ts)",
                source_key=source.key,
                contract_role="example_pool",
            )
        )

    token0_addr = source.tokens.get("token0")
    token1_addr = source.tokens.get("token1")
    if token0_addr and token1_addr:
        args = abi.pad_address(token0_addr) + abi.pad_address(token1_addr)
        result, issue = _call(
            transport, factory, abi.encode_call(abi.SEL_GET_PAIR, args), block_param
        )
        if issue:
            issues.append(issue)
        else:
            round_trip = abi.decode_address(result)
            if round_trip is None or round_trip.lower() != example_pool.lower():
                issues.append(
                    PreflightIssue(
                        severity="error",
                        code="identity_mismatch",
                        message=(
                            f"{factory}.getPair(token0,token1) returned {round_trip}, "
                            f"expected {example_pool}"
                        ),
                        source_key=source.key,
                        contract_role="factory",
                    )
                )
    return issues


def _check_liquidity_book_family(
    transport: RpcTransport, block_param: str, source: SourceConfig
) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    factory_ref = source.contracts.get("factory")
    pair_ref = source.contracts.get("example_pair")
    if factory_ref is None or pair_ref is None:
        return [
            PreflightIssue(
                severity="error",
                code="config_incomplete",
                message="liquidity_book_v2 source needs 'factory' and 'example_pair'",
                source_key=source.key,
            )
        ]
    factory, example_pair = factory_ref.address, pair_ref.address

    issues.extend(_check_all_contract_code(transport, block_param, source))
    if issues:
        return issues

    result, issue = _call(
        transport, example_pair, abi.encode_call(abi.SEL_GET_FACTORY), block_param
    )
    if issue:
        issues.append(issue)
    elif abi.decode_address(result) is None or abi.decode_address(
        result
    ).lower() != factory.lower():  # type: ignore[union-attr]
        issues.append(
            PreflightIssue(
                severity="error",
                code="identity_mismatch",
                message=f"{example_pair}.getFactory() != configured factory {factory}",
                source_key=source.key,
                contract_role="example_pair",
            )
        )

    for selector, label in (
        (abi.SEL_GET_TOKEN_X, "getTokenX"),
        (abi.SEL_GET_TOKEN_Y, "getTokenY"),
        (abi.SEL_GET_BIN_STEP, "getBinStep"),
    ):
        result, issue = _call(transport, example_pair, abi.encode_call(selector), block_param)
        if issue:
            issues.append(issue)
            continue
        if label == "getBinStep":
            if abi.decode_uint(result) in (None, 0):
                issues.append(
                    PreflightIssue(
                        severity="error",
                        code="missing_state",
                        message=f"{example_pair}.getBinStep() returned no/zero bin step",
                        source_key=source.key,
                        contract_role="example_pair",
                    )
                )
        elif abi.is_zero_address(abi.decode_address(result)):
            issues.append(
                PreflightIssue(
                    severity="error",
                    code="missing_state",
                    message=f"{example_pair}.{label}() returned the zero address",
                    source_key=source.key,
                    contract_role="example_pair",
                )
            )

    result, issue = _call(
        transport, factory, abi.encode_call(abi.SEL_GET_NUMBER_OF_LB_PAIRS), block_param
    )
    if issue:
        issues.append(issue)
    elif abi.decode_uint(result) in (None, 0):
        issues.append(
            PreflightIssue(
                severity="error",
                code="missing_state",
                message=f"{factory}.getNumberOfLBPairs() returned 0 pairs",
                source_key=source.key,
                contract_role="factory",
            )
        )
    return issues


_FAMILY_CHECKS = {
    "v3_concentrated_liquidity": _check_v3_family,
    "v2_classic": _check_v2_classic_family,
    "liquidity_book_v2": _check_liquidity_book_family,
}


def run_preflight(catalog: ProtocolCatalog, transport: RpcTransport) -> PreflightReport:
    rpc_url = getattr(transport, "url", "<fake-transport>")
    report = PreflightReport(rpc_url=redact_url(rpc_url) if isinstance(rpc_url, str) else "?")

    observed_chain_id, chain_issue = check_chain_id(transport, catalog.network.chain_id)
    report.observed_chain_id = observed_chain_id
    if chain_issue:
        report.issues.append(chain_issue)
        # A wrong chain makes every subsequent read meaningless (could be reading a
        # same-numbered block on an entirely different network) — reject immediately
        # rather than accumulate confusing downstream errors.
        return report

    block, block_issues = check_candidate_block(transport, catalog.candidate_block)
    report.observed_block = block
    report.issues.extend(block_issues)
    if any(i.severity == "error" for i in block_issues):
        return report

    block_param = _to_block_param(catalog.candidate_block.number)
    for source in catalog.sources:
        report.checked_sources.append(source.key)
        checker = _FAMILY_CHECKS.get(source.protocol_family)
        if checker is None:
            report.issues.append(
                PreflightIssue(
                    severity="error",
                    code="unknown_protocol_family",
                    message=f"no preflight checks implemented for {source.protocol_family!r}",
                    source_key=source.key,
                )
            )
            continue
        report.issues.extend(checker(transport, block_param, source))
        for blocker in source.blockers:
            entry = {
                "source_key": source.key,
                "id": blocker.id,
                "description": blocker.description,
                "blocks": list(blocker.blocks),
            }
            report.known_blockers.append(entry)

    return report


def run_preflight_or_raise(catalog: ProtocolCatalog, transport: RpcTransport) -> PreflightReport:
    report = run_preflight(catalog, transport)
    if not report.ok:
        raise PreflightFailed(report)
    return report


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m snapshot.preflight",
        description=(
            "Run the explicit online admission preflight against config/protocols.yaml. "
            "Requires network access to the configured Mantle RPC; never used by the "
            "offline test suite."
        ),
    )
    parser.add_argument("--config", default="config/protocols.yaml")
    parser.add_argument(
        "--rpc-url",
        default=None,
        help="Override the catalog's default_rpc_url (must still be non-secret).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write the redacted JSON evidence report to this path (also printed to stdout).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    catalog = load_catalog(args.config)
    rpc_url = args.rpc_url or catalog.network.default_rpc_url
    transport = HttpJsonRpcTransport(rpc_url)

    report = run_preflight(catalog, transport)
    evidence = report.to_dict()
    evidence["generated_at"] = datetime.now(UTC).isoformat()
    evidence["rpc_call_count"] = transport.call_count
    evidence["rpc_cache_hits"] = transport.cache_hits

    text = json.dumps(evidence, indent=2, sort_keys=True)
    print(text)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")

    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())

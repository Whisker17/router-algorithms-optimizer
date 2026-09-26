"""One exploratory Exact Input request against a frozen corpus bundle (WHI-1498).

`resolve_token` maps a symbol or an explicit address onto the bundle's frozen token
metadata (`prices.json`: address, symbol, decimals); `parse_amount` turns a human
decimal into integer base units exactly -- money is never rounded (docs/DESIGN.md
§2.12). `derive_request_bundle` publishes a new single-case bundle: the parent's pool
states unchanged at the parent's block, the one custom case, and a provenance record
naming the parent bundle (id, hash, checksum table) and the request. The parent bundle
is only read; the derived bundle has its own identity and is labelled exploratory, so a
custom request never borrows a corpus case's labels or a corpus bundle's identity.

The derived bundle is a plain (non-corpus) bundle: the corpus descriptor's case labels
(origin, stratum, split, envelope bound) cannot describe a custom request honestly, and
the bundle loader only admits a price context together with a corpus descriptor. Token
metadata therefore travels in the provenance record. Its content is deterministic (no
timestamps or local paths), so an identical request over an identical parent yields an
identical bundle hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snapshot.bundle import MANIFEST_FILE, write_bundle
from snapshot.models import Case, SnapshotBundle

REQUEST_SCHEMA = "exploratory-request/1"
EXPLORATORY_LABEL = "exploratory single request; not a held-out corpus result"

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_AMOUNT = re.compile(r"^(\d+)(?:\.(\d+))?$")


class RequestError(ValueError):
    """The request cannot be resolved against the bundle exactly and unambiguously."""


@dataclass(frozen=True)
class TokenInfo:
    address: str
    symbol: str | None
    decimals: int

    def to_dict(self) -> dict[str, Any]:
        return {"address": self.address, "symbol": self.symbol, "decimals": self.decimals}


def token_universe(bundle: SnapshotBundle) -> dict[str, TokenInfo]:
    """address -> frozen token metadata. Only a corpus bundle carries it."""
    if bundle.prices is None:
        raise RequestError(
            f"bundle {bundle.bundle_id!r} has no frozen token metadata (prices.json); "
            "a single-request comparison needs a corpus bundle"
        )
    return {
        address: TokenInfo(address, entry.symbol, int(entry.decimals))
        for address, entry in sorted(bundle.prices.tokens.items())
    }


def _known(tokens: dict[str, TokenInfo]) -> str:
    return ", ".join(f"{t.symbol or '?'} ({t.address})" for t in tokens.values())


def resolve_token(bundle: SnapshotBundle, text: str, role: str) -> TokenInfo:
    """A symbol (case-insensitive, must match exactly one token) or an explicit address
    of the frozen token universe."""
    tokens = token_universe(bundle)
    value = text.strip()
    if _ADDRESS.match(value):
        token = tokens.get(value.lower())
        if token is None:
            raise RequestError(
                f"{role}: address {value} is not in the frozen token universe of "
                f"{bundle.bundle_id!r}; supported: {_known(tokens)}"
            )
        return token
    matches = [t for t in tokens.values() if t.symbol and t.symbol.lower() == value.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RequestError(
            f"{role}: symbol {value!r} is ambiguous "
            f"({_known({t.address: t for t in matches})}); pass the token address instead"
        )
    assert bundle.prices is not None  # token_universe checked it
    native = bundle.prices.native
    if value and str(native.get("symbol", "")).lower() == value.lower():
        wrapped = tokens.get(str(native.get("via_token", "")).lower())
        hint = f"{wrapped.symbol} ({wrapped.address})" if wrapped else "the wrapped token"
        raise RequestError(
            f"{role}: native {value} is not a routable token and wrapping is out of scope; "
            f"use {hint} explicitly"
        )
    raise RequestError(f"{role}: unknown token {value!r}; supported: {_known(tokens)}")


def parse_amount(text: str, token: TokenInfo) -> int:
    """A plain positive decimal (`10000`, `1.5`) in whole-token units -> exact raw base
    units. No sign, exponent, separators, NaN or infinity; more fractional digits than the
    token has decimals is refused unless they are zeros -- never rounded."""
    value = text.strip()
    match = _AMOUNT.match(value)
    if match is None:
        raise RequestError(
            f"amount {text!r}: expected a plain positive decimal such as 10000 or 1.5 "
            "(no sign, exponent, separators, NaN or infinity)"
        )
    whole, fraction = match.group(1), match.group(2) or ""
    if len(fraction) > token.decimals:
        if fraction[token.decimals :].strip("0"):
            raise RequestError(
                f"amount {text!r}: {token.symbol or token.address} has {token.decimals} "
                "decimals; the extra precision would have to be rounded, which is refused"
            )
        fraction = fraction[: token.decimals]
    raw = int(whole + fraction.ljust(token.decimals, "0"))  # exact: digits, never floats
    if raw <= 0:
        raise RequestError(f"amount {text!r}: must be positive")
    return raw


def format_amount(raw: int, decimals: int) -> str:
    """Exact human rendering of `raw` base units (no rounding, trailing zeros dropped)."""
    sign = "-" if raw < 0 else ""
    whole, fraction = divmod(abs(raw), 10**decimals)
    digits = str(fraction).rjust(decimals, "0").rstrip("0") if decimals else ""
    return f"{sign}{whole}.{digits}" if digits else f"{sign}{whole}"


def request_case(
    parent: SnapshotBundle, token_in: TokenInfo, token_out: TokenInfo, raw: int
) -> Case:
    if token_in.address == token_out.address:
        raise RequestError(
            f"token-in and token-out are the same token ({token_in.symbol} {token_in.address})"
        )
    body = f"{parent.bundle_hash}:{token_in.address}:{token_out.address}:{raw}"
    digest = hashlib.sha256(body.encode()).hexdigest()[:12]
    return Case(
        case_id=f"quote-{digest}",
        token_in=token_in.address,
        token_out=token_out.address,
        amount_in=raw,
    )


def request_provenance(parent: SnapshotBundle, case: Case) -> dict[str, Any]:
    """The derived bundle's provenance record (see module docstring)."""
    tokens = token_universe(parent)
    parent_manifest = json.loads((Path(parent.source_path) / MANIFEST_FILE).read_text())
    envelope = None
    if parent.corpus is not None:
        envelope = parent.corpus["envelope"]["amount_in"].get(case.token_in)
    sources = Counter(
        str(getattr(p, "source_key", None) or "generic") for p in parent.pools.values()
    )
    token_in, token_out = tokens[case.token_in], tokens[case.token_out]
    return {
        "schema": REQUEST_SCHEMA,
        "exploratory": True,
        "label": EXPLORATORY_LABEL,
        "block": {
            "chain_id": parent.block.chain_id,
            "number": parent.block.number,
            "hash": parent.block.hash,
            "timestamp": parent.block.timestamp,
        },
        "derived_from": {
            "bundle_id": parent.bundle_id,
            "bundle_hash": parent.bundle_hash,
            "kind": parent.kind,
            "checksums": parent_manifest["checksums"],
        },
        "request": {
            "case_id": case.case_id,
            "token_in": token_in.to_dict(),
            "token_out": token_out.to_dict(),
            "amount_in_raw": str(case.amount_in),
            "amount_in": format_amount(case.amount_in, token_in.decimals),
        },
        # The parent corpus sized its collected pool state (CL ticks, LB bins) for amounts
        # up to this per-token bound; beyond it an incomplete-snapshot outcome reflects
        # uncollected state, not missing liquidity.
        "envelope": {
            "token_in_max_raw": envelope,
            "within": None if envelope is None else case.amount_in <= int(envelope),
        },
        "pool_scope": {"pools": len(parent.pools), "sources": dict(sorted(sources.items()))},
        "tokens": {address: t.to_dict() for address, t in tokens.items()},
    }


def derive_request_bundle(parent: SnapshotBundle, case: Case, output_dir: Path) -> SnapshotBundle:
    """Publish the single-case request bundle at `output_dir` (refuses to overwrite)."""
    provenance = request_provenance(parent, case)
    bundle = write_bundle(
        output_dir,
        bundle_id=f"{parent.bundle_id}-exploratory-{case.case_id.removeprefix('quote-')}",
        kind=parent.kind,
        block=parent.block,
        pools=list(parent.pools.values()),
        cases=[case],
        provenance=provenance,
    )
    if bundle.pools != parent.pools:  # the serialization round trip must preserve state
        raise RequestError(f"{output_dir}: derived pool state differs from the parent bundle")
    return bundle

"""Frozen price context (WHI-1436 owns this schema; docs/DESIGN.md §2.4, §2.9).

A price context converts amounts between assets for *reporting and envelope sizing* --
e.g. a case's USD notional, or later (WHI-1445) native execution fees into the output
asset. It is frozen with the corpus and covered by the bundle checksums; it never feeds
pool math (pools are simulated exactly from their own state).

Schema `price-context/1` (`prices.json` in a corpus bundle):

    {
      "schema": "price-context/1",
      "block": {chain_id, number, hash, timestamp},     # the bundle's block identity
      "quote_currency": "USD",
      "unit": "quote currency per 1 whole token (10**decimals raw units)",
      "source": {provider, table, query_id, execution_id, export_sha256,
                 selection, max_staleness_seconds},
      "tokens": {
        "<lowercase address>": {
          "symbol": str | null,       # informational only; identity is the address
          "decimals": int,
          "status": "ok" | "missing",
          "price": "<decimal string>" | null,   # null iff missing; never "0"
          "timestamp": int | null,    # unix seconds of the price observation
          "origin": str | null        # the upstream price feed label (e.g. coinpaprika)
        }, ...
      },
      "native": {"symbol": "MNT", "via_token": "<address>", "derivation": str}
    }

Rules enforced by `parse_price_context`: every `ok` price is a positive finite decimal,
observed at or before the snapshot timestamp and no older than the declared staleness
bound; a `missing` token has neither price nor timestamp (an unknown price is never zero);
unknown keys are rejected. `usd_value`/`raw_amount_for_usd` refuse a missing price instead
of treating it as zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

PRICE_SCHEMA = "price-context/1"
PRICE_UNIT = "quote currency per 1 whole token (10**decimals raw units)"
STATUS_OK = "ok"
STATUS_MISSING = "missing"


class PriceError(ValueError):
    """A malformed price context, or a conversion that needs a missing price."""


@dataclass(frozen=True)
class TokenPrice:
    address: str
    symbol: str | None
    decimals: int
    status: str
    price: Decimal | None
    timestamp: int | None
    origin: str | None


@dataclass(frozen=True)
class PriceContext:
    block_number: int
    block_hash: str
    block_timestamp: int
    quote_currency: str
    source: dict[str, Any]
    tokens: dict[str, TokenPrice]
    native: dict[str, Any]

    def price(self, token: str) -> TokenPrice:
        try:
            return self.tokens[token.lower()]
        except KeyError as exc:
            raise PriceError(f"token {token} is not in the price context") from exc

    def usd_value(self, token: str, amount_raw: int) -> Decimal:
        entry = self.price(token)
        if entry.status != STATUS_OK or entry.price is None:
            raise PriceError(f"token {token} has no frozen price (status {entry.status})")
        return Decimal(amount_raw) * entry.price / (Decimal(10) ** entry.decimals)

    def raw_amount_for_usd(self, token: str, usd: Decimal) -> int:
        """The smallest raw amount of `token` worth at least `usd` (rounded up)."""
        entry = self.price(token)
        if entry.status != STATUS_OK or entry.price is None:
            raise PriceError(f"token {token} has no frozen price (status {entry.status})")
        raw = usd * (Decimal(10) ** entry.decimals) / entry.price
        return int(raw.to_integral_value(rounding=ROUND_CEILING))


def _require_keys(obj: Any, required: set[str], optional: set[str], where: str) -> None:
    if not isinstance(obj, dict):
        raise PriceError(f"{where}: expected a mapping, got {type(obj).__name__}")
    missing = required - obj.keys()
    if missing:
        raise PriceError(f"{where}: missing required key(s) {sorted(missing)}")
    unknown = obj.keys() - required - optional
    if unknown:
        raise PriceError(f"{where}: unknown key(s) {sorted(unknown)}")


def _int(value: Any, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PriceError(f"{where}: expected int, got {value!r}")
    return value


def price_string(value: float) -> str:
    """A Dune double as the shortest round-tripping decimal string."""
    text = repr(float(value))
    if "e" in text or "E" in text:
        text = format(Decimal(text), "f")
    return text


def parse_price_context(raw: Any, *, block: tuple[int, str, int] | None = None) -> PriceContext:
    """Validate a `price-context/1` document. `block` = (number, hash, timestamp) of the
    bundle it belongs to, if known; a mismatch is rejected."""
    _require_keys(
        raw,
        {"schema", "block", "quote_currency", "unit", "source", "tokens", "native"},
        set(),
        "prices",
    )
    if raw["schema"] != PRICE_SCHEMA:
        raise PriceError(f"prices.schema: {raw['schema']!r} is not {PRICE_SCHEMA!r}")
    if raw["unit"] != PRICE_UNIT:
        raise PriceError(f"prices.unit: {raw['unit']!r} is not {PRICE_UNIT!r}")
    if raw["quote_currency"] != "USD":
        raise PriceError("prices.quote_currency: only USD is supported")
    blk = raw["block"]
    _require_keys(blk, {"chain_id", "number", "hash", "timestamp"}, set(), "prices.block")
    number, block_hash, ts = (
        _int(blk["number"], "prices.block.number"),
        str(blk["hash"]).lower(),
        _int(blk["timestamp"], "prices.block.timestamp"),
    )
    if block is not None and (number, block_hash, ts) != (block[0], block[1].lower(), block[2]):
        raise PriceError(
            f"prices.block: ({number}, {block_hash}, {ts}) is not the bundle block {block}"
        )
    source = raw["source"]
    _require_keys(
        source,
        {
            "provider",
            "table",
            "query_id",
            "execution_id",
            "export_sha256",
            "selection",
            "max_staleness_seconds",
        },
        set(),
        "prices.source",
    )
    staleness = _int(source["max_staleness_seconds"], "prices.source.max_staleness_seconds")
    if staleness <= 0:
        raise PriceError("prices.source.max_staleness_seconds: must be positive")
    tokens_obj = raw["tokens"]
    if not isinstance(tokens_obj, dict) or not tokens_obj:
        raise PriceError("prices.tokens: expected a non-empty mapping")
    tokens: dict[str, TokenPrice] = {}
    for address, entry in tokens_obj.items():
        where = f"prices.tokens[{address}]"
        if (
            not isinstance(address, str)
            or address != address.lower()
            or len(address) != 42
            or not address.startswith("0x")
        ):
            raise PriceError(f"{where}: keys are lowercase 0x-addresses")
        _require_keys(
            entry, {"symbol", "decimals", "status", "price", "timestamp", "origin"}, set(), where
        )
        decimals = _int(entry["decimals"], f"{where}.decimals")
        if not 0 <= decimals <= 77:
            raise PriceError(f"{where}.decimals: {decimals} out of range")
        status = entry["status"]
        price: Decimal | None = None
        timestamp: int | None = None
        if status == STATUS_OK:
            if not isinstance(entry["price"], str):
                raise PriceError(f"{where}.price: expected a decimal string")
            try:
                price = Decimal(entry["price"])
            except InvalidOperation as exc:
                raise PriceError(f"{where}.price: not a decimal: {entry['price']!r}") from exc
            if not price.is_finite() or price <= 0:
                raise PriceError(f"{where}.price: must be a positive finite price (never zero)")
            timestamp = _int(entry["timestamp"], f"{where}.timestamp")
            if timestamp > ts:
                raise PriceError(
                    f"{where}.timestamp: {timestamp} is after the snapshot timestamp {ts}"
                )
            if ts - timestamp > staleness:
                raise PriceError(
                    f"{where}.timestamp: {ts - timestamp}s older than the snapshot, beyond "
                    f"the declared {staleness}s staleness bound"
                )
            if not isinstance(entry["origin"], str) or not entry["origin"]:
                raise PriceError(f"{where}.origin: an observed price names its feed")
        elif status == STATUS_MISSING:
            if entry["price"] is not None or entry["timestamp"] is not None:
                raise PriceError(f"{where}: a missing price has null price and timestamp")
        else:
            raise PriceError(f"{where}.status: {status!r} not in ['missing', 'ok']")
        symbol = entry["symbol"]
        if symbol is not None and not isinstance(symbol, str):
            raise PriceError(f"{where}.symbol: expected a string or null")
        tokens[address] = TokenPrice(
            address=address,
            symbol=symbol,
            decimals=decimals,
            status=status,
            price=price,
            timestamp=timestamp,
            origin=entry["origin"],
        )
    native = raw["native"]
    _require_keys(native, {"symbol", "via_token", "derivation"}, set(), "prices.native")
    if native["via_token"] not in tokens:
        raise PriceError("prices.native.via_token: must be a token of this context")
    return PriceContext(
        block_number=number,
        block_hash=block_hash,
        block_timestamp=ts,
        quote_currency="USD",
        source=dict(source),
        tokens=tokens,
        native=dict(native),
    )


def build_price_context(
    rows: list[dict[str, Any]],
    *,
    block: dict[str, Any],
    source: dict[str, Any],
    native_via: str,
) -> dict[str, Any]:
    """Build a `price-context/1` document from Q3 export rows
    (`token, price_timestamp, price, decimals, symbol, source`). A row with a null price
    becomes `missing`; nothing is defaulted to zero."""
    tokens: dict[str, Any] = {}
    for row in rows:
        address = str(row["token"]).lower()
        decimals = row.get("decimals")
        if decimals is None:
            raise PriceError(f"token {address}: no decimals known")
        if row.get("price") is None:
            tokens[address] = {
                "symbol": row.get("symbol"),
                "decimals": int(decimals),
                "status": STATUS_MISSING,
                "price": None,
                "timestamp": None,
                "origin": None,
            }
            continue
        tokens[address] = {
            "symbol": row.get("symbol"),
            "decimals": int(decimals),
            "status": STATUS_OK,
            "price": price_string(row["price"]),
            "timestamp": int(row["price_timestamp"]),
            "origin": row.get("source"),
        }
    doc = {
        "schema": PRICE_SCHEMA,
        "block": block,
        "quote_currency": "USD",
        "unit": PRICE_UNIT,
        "source": source,
        "tokens": dict(sorted(tokens.items())),
        "native": {
            "symbol": "MNT",
            "via_token": native_via,
            "derivation": (
                "WMNT wraps native MNT 1:1 (deposit/withdraw at par), so the native gas "
                "token is priced at the WMNT observation"
            ),
        },
    }
    parse_price_context(doc)
    return doc

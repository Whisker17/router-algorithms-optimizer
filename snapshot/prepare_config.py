"""Typed, validated loader for concentrated-liquidity prepare configs
(`config/prepare/<source>.yaml`, WHI-1429).

A prepare config is the *selection* half of a fixed-block collection: which pairs and
fee tiers to discover through the verified factory, which reference cases define the
declared amount envelope, how far the tick walk may go, and how patiently to talk to
the flaky public RPC. Verified identity facts (addresses, code hashes, admission) stay
in `config/protocols.yaml`. Loading is fail-fast on unknown keys, bad addresses and
invalid bounds (docs/DESIGN.md §2.12). Addresses are normalized to lowercase so a
token's identity never depends on checksum casing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from snapshot.models import Case

_HEX = set("0123456789abcdef")
SUPPORTED_SCHEMA_VERSION = 1


class PrepareConfigError(ValueError):
    """The prepare config is missing, malformed, or fails validation."""


def _require_keys(obj: Any, required: set[str], optional: set[str], where: str) -> None:
    if not isinstance(obj, dict):
        raise PrepareConfigError(f"{where}: expected a mapping, got {type(obj).__name__}")
    missing = required - obj.keys()
    if missing:
        raise PrepareConfigError(f"{where}: missing required key(s) {sorted(missing)}")
    unknown = obj.keys() - required - optional
    if unknown:
        raise PrepareConfigError(f"{where}: unknown key(s) {sorted(unknown)}")


def _address(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
        raise PrepareConfigError(f"{where}: not a 20-byte 0x-address: {value!r}")
    lowered = value.lower()
    if not all(c in _HEX for c in lowered[2:]):
        raise PrepareConfigError(f"{where}: not valid hex: {value!r}")
    return lowered


def _int(value: Any, lo: int, hi: int | None, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PrepareConfigError(f"{where}: expected int, got {value!r}")
    if value < lo or (hi is not None and value > hi):
        raise PrepareConfigError(
            f"{where}: {value} outside [{lo}, {hi if hi is not None else '∞'}]"
        )
    return value


def _number(value: Any, lo: float, where: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise PrepareConfigError(f"{where}: expected a number, got {value!r}")
    if value < lo:
        raise PrepareConfigError(f"{where}: {value} below {lo}")
    return float(value)


@dataclass(frozen=True)
class PairSpec:
    """`fee_tiers` are discovered and admitted; `excluded_fee_tiers` (fee -> reason) are
    still looked up through the factory and recorded as explicit, reasoned omissions in
    the bundle provenance, never silently dropped (docs/DESIGN.md §2.2: "Record both the
    selection method and omitted sources/pools")."""

    token0: str  # sorted: token0 < token1, as the factory orders them
    token1: str
    fee_tiers: tuple[int, ...]
    excluded_fee_tiers: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class CollectionLimits:
    """Tick-walk bounds. The walk starts at the current tick's bitmap word widened by
    `initial_margin_words`, extends one word at a time in the direction a reference
    swap needs, and after each direction is covered adds `margin_words` beyond the
    word the largest swap ends in. More than `max_words_per_direction` words on one
    side of the current word is `incomplete_snapshot`: publication is refused rather
    than the envelope silently truncated."""

    initial_margin_words: int
    margin_words: int
    max_words_per_direction: int


@dataclass(frozen=True)
class RpcSettings:
    max_batch_size: int
    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    timeout_seconds: float


@dataclass(frozen=True)
class ClPrepareConfig:
    source_key: str
    bundle_id_prefix: str
    token_labels: dict[str, str]  # lowercase address -> human label (informational)
    pairs: tuple[PairSpec, ...]
    cases: tuple[Case, ...]
    limits: CollectionLimits
    rpc: RpcSettings
    source_path: str
    sha256: str


def _resolve_token(value: Any, aliases: dict[str, str], where: str) -> str:
    if isinstance(value, str) and value in aliases:
        return aliases[value]
    return _address(value, where)


def parse_prepare_config(raw: Any, *, source_path: str, sha256: str) -> ClPrepareConfig:
    _require_keys(
        raw,
        {
            "schema_version",
            "source",
            "bundle_id_prefix",
            "tokens",
            "pairs",
            "cases",
            "collection",
            "rpc",
        },
        set(),
        "<root>",
    )
    if raw["schema_version"] != SUPPORTED_SCHEMA_VERSION:
        raise PrepareConfigError(
            f"schema_version: unsupported {raw['schema_version']!r} "
            f"(expected {SUPPORTED_SCHEMA_VERSION})"
        )
    source_key = raw["source"]
    if not isinstance(source_key, str) or not source_key:
        raise PrepareConfigError("source: expected a non-empty string")
    prefix = raw["bundle_id_prefix"]
    if not isinstance(prefix, str) or not prefix:
        raise PrepareConfigError("bundle_id_prefix: expected a non-empty string")

    tokens_obj = raw["tokens"]
    if not isinstance(tokens_obj, dict) or not tokens_obj:
        raise PrepareConfigError("tokens: expected a non-empty label -> address mapping")
    aliases: dict[str, str] = {}
    labels: dict[str, str] = {}
    for label, addr in tokens_obj.items():
        resolved = _address(addr, f"tokens.{label}")
        if resolved in labels:
            raise PrepareConfigError(
                f"tokens: {label!r} and {labels[resolved]!r} name the same address {resolved}"
            )
        aliases[str(label)] = resolved
        labels[resolved] = str(label)

    pairs_obj = raw["pairs"]
    if not isinstance(pairs_obj, list) or not pairs_obj:
        raise PrepareConfigError("pairs: expected a non-empty list")
    pairs: list[PairSpec] = []
    seen_pairs: set[tuple[str, str]] = set()
    for i, entry in enumerate(pairs_obj):
        where = f"pairs[{i}]"
        _require_keys(entry, {"tokens", "fee_tiers"}, {"excluded_fee_tiers"}, where)
        toks = entry["tokens"]
        if not isinstance(toks, list) or len(toks) != 2:
            raise PrepareConfigError(f"{where}.tokens: expected two tokens")
        a = _resolve_token(toks[0], aliases, f"{where}.tokens[0]")
        b = _resolve_token(toks[1], aliases, f"{where}.tokens[1]")
        if a == b:
            raise PrepareConfigError(f"{where}.tokens: the two tokens must differ")
        t0, t1 = sorted((a, b))
        if (t0, t1) in seen_pairs:
            raise PrepareConfigError(f"{where}: duplicate pair {t0}/{t1}")
        seen_pairs.add((t0, t1))
        fees = entry["fee_tiers"]
        if not isinstance(fees, list) or not fees:
            raise PrepareConfigError(f"{where}.fee_tiers: expected a non-empty list")
        fee_tiers = tuple(_int(f, 1, 999_999, f"{where}.fee_tiers") for f in fees)
        if len(set(fee_tiers)) != len(fee_tiers):
            raise PrepareConfigError(f"{where}.fee_tiers: duplicates in {fee_tiers}")
        excluded: list[tuple[int, str]] = []
        for j, ex in enumerate(entry.get("excluded_fee_tiers", [])):
            w = f"{where}.excluded_fee_tiers[{j}]"
            _require_keys(ex, {"fee", "reason"}, set(), w)
            fee = _int(ex["fee"], 1, 999_999, f"{w}.fee")
            reason = ex["reason"]
            if not isinstance(reason, str) or not reason.strip():
                raise PrepareConfigError(f"{w}.reason: an exclusion needs a written reason")
            if fee in fee_tiers or fee in {f for f, _ in excluded}:
                raise PrepareConfigError(f"{w}.fee: {fee} is both admitted and excluded/duplicated")
            excluded.append((fee, " ".join(reason.split())))
        pairs.append(
            PairSpec(token0=t0, token1=t1, fee_tiers=fee_tiers, excluded_fee_tiers=tuple(excluded))
        )

    cases_obj = raw["cases"]
    if not isinstance(cases_obj, list) or not cases_obj:
        raise PrepareConfigError("cases: expected a non-empty list")
    cases: list[Case] = []
    seen_ids: set[str] = set()
    for i, entry in enumerate(cases_obj):
        where = f"cases[{i}]"
        _require_keys(entry, {"case_id", "token_in", "token_out", "amount_in"}, set(), where)
        case_id = entry["case_id"]
        if not isinstance(case_id, str) or not case_id or case_id in seen_ids:
            raise PrepareConfigError(f"{where}.case_id: expected a unique non-empty string")
        seen_ids.add(case_id)
        token_in = _resolve_token(entry["token_in"], aliases, f"{where}.token_in")
        token_out = _resolve_token(entry["token_out"], aliases, f"{where}.token_out")
        if token_in == token_out:
            raise PrepareConfigError(f"{where}: token_in and token_out must differ")
        if tuple(sorted((token_in, token_out))) not in seen_pairs:
            raise PrepareConfigError(f"{where}: pair {token_in}/{token_out} is not in `pairs`")
        amount = entry["amount_in"]
        if not isinstance(amount, str):
            raise PrepareConfigError(f"{where}.amount_in: expected a decimal string")
        try:
            amount_in = int(amount)
        except ValueError as exc:
            raise PrepareConfigError(f"{where}.amount_in: not an integer: {amount!r}") from exc
        if amount_in <= 0:
            raise PrepareConfigError(f"{where}.amount_in: must be positive")
        cases.append(
            Case(case_id=case_id, token_in=token_in, token_out=token_out, amount_in=amount_in)
        )

    coll = raw["collection"]
    _require_keys(
        coll,
        {"initial_margin_words", "margin_words", "max_words_per_direction"},
        set(),
        "collection",
    )
    limits = CollectionLimits(
        initial_margin_words=_int(
            coll["initial_margin_words"], 0, 64, "collection.initial_margin_words"
        ),
        margin_words=_int(coll["margin_words"], 0, 64, "collection.margin_words"),
        max_words_per_direction=_int(
            coll["max_words_per_direction"], 1, 4096, "collection.max_words_per_direction"
        ),
    )
    if limits.initial_margin_words > limits.max_words_per_direction:
        raise PrepareConfigError("collection: initial_margin_words exceeds max_words_per_direction")

    rpc = raw["rpc"]
    _require_keys(
        rpc,
        {
            "max_batch_size",
            "max_attempts",
            "base_delay_seconds",
            "max_delay_seconds",
            "timeout_seconds",
        },
        set(),
        "rpc",
    )
    settings = RpcSettings(
        max_batch_size=_int(rpc["max_batch_size"], 1, 100, "rpc.max_batch_size"),
        max_attempts=_int(rpc["max_attempts"], 1, 20, "rpc.max_attempts"),
        base_delay_seconds=_number(rpc["base_delay_seconds"], 0.0, "rpc.base_delay_seconds"),
        max_delay_seconds=_number(rpc["max_delay_seconds"], 0.0, "rpc.max_delay_seconds"),
        timeout_seconds=_number(rpc["timeout_seconds"], 1.0, "rpc.timeout_seconds"),
    )
    if settings.max_delay_seconds < settings.base_delay_seconds:
        raise PrepareConfigError("rpc: max_delay_seconds must be >= base_delay_seconds")

    return ClPrepareConfig(
        source_key=source_key,
        bundle_id_prefix=prefix,
        token_labels=labels,
        pairs=tuple(pairs),
        cases=tuple(cases),
        limits=limits,
        rpc=settings,
        source_path=source_path,
        sha256=sha256,
    )


def load_prepare_config(path: str | Path) -> ClPrepareConfig:
    data = Path(path).read_bytes()
    try:
        raw = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise PrepareConfigError(f"{path}: invalid YAML: {exc}") from exc
    try:
        return parse_prepare_config(
            raw, source_path=str(path), sha256=hashlib.sha256(data).hexdigest()
        )
    except PrepareConfigError as exc:
        raise PrepareConfigError(f"{path}: {exc}") from exc

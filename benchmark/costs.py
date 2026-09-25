"""Empirical execution-cost model (WHI-1445 / I19; docs/DESIGN.md §2.9,
docs/references/cost-model.md).

**What is modeled.** The whole-transaction native fee (MNT wei) a historical Mantle
transaction of a given *complete-plan shape* paid, split into what the shape determines
-- L2 gas used and the separate L1 data fee -- and what the sender chose -- the gas price.
Pool swap fees and price impact are *not* here: they are already inside the simulator's
gross output, so nothing is double counted. Historical costs are those of router-executed
transactions (the router's own overhead is included; it is not attributed to pools).

**Shape cohorts (the smallest interpretable model).** A cohort is
`c<k>.l<m>.p<n>.<topology>`: the number of distinct concentrated / Liquidity Book /
constant-product pools a transaction used and whether they form one chain
(`pools == tokens - 1`, one source and one sink token) or a parallel/split graph.
Per cohort the model stores the training medians of gas used and L1 fee; one reference
gas price (the training median effective price) prices every cohort:

    nominal_wei = gas_med * (P_ref + operatorFeeScalar * 100) + l1_med + operatorFeeConstant

(the post-Arsia total-fee formula of `snapshot.cost_evidence.FeeRules`, charged **once
per plan/transaction**, never per leg). Holdout transactions are priced the same way at
P_ref, so their relative error isolates the shape model from each sender's price bid;
the empirical 5th/95th percentile of that error gives the **low/high cost scenarios**
(not confidence intervals). The as-paid dispersion is reported separately.

**Applicability.** A plan maps to a cohort from `routing.evaluator` `route_features`
(`pool_calls_<family>`, `split_funds`, `merge_steps`, `repeated_pool_calls`,
`lb_bins_swapped`). Only a cohort with enough training *and* holdout transactions is
`supported`; a plan that reuses a pool (shared-pool graphs), has no historical cohort, a
cohort below the thresholds, or more LB bins than the cohort's training range is
`unsupported`/`low_confidence` and gets **no net score** (it stays unranked on net; gross
is still comparable). An unknown price likewise withholds the net score. Features not
available historically (e.g. CL ticks crossed) are not invented.

**Artifact.** `fit_cost_model` is deterministic; `write_cost_model` stores the canonical
JSON (`cost-model/1`) whose SHA-256 is the model identity and refuses to overwrite a
different artifact at the same path (a new model is a new file). `load_cost_model`
re-derives every cohort's predictions and holdout statistics from the stored holdout
rows and rejects an artifact that does not reproduce them.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from statistics import median_low
from typing import Any

from snapshot.cost_evidence import (
    FAMILIES,
    CostConfig,
    FeeRules,
    SampleSet,
    Shape,
    TxSample,
)
from snapshot.prices import PriceContext, PriceError

COST_MODEL_SCHEMA = "cost-model/1"
SUPPORTED = "supported"
LOW_CONFIDENCE = "low_confidence"
UNSUPPORTED = "unsupported"
UNKNOWN_PRICE = "unknown_price"
_FAMILY_TAG = {"concentrated": "c", "liquidity_book": "l", "constant_product": "p"}


class CostModelError(ValueError):
    """A malformed or non-reproducing cost-model artifact, or an invalid fit input."""


# ---------------------------------------------------------------------------
# shape cohorts
# ---------------------------------------------------------------------------


def cohort_key(pools: Mapping[str, int], topology: str) -> str:
    return ".".join(f"{_FAMILY_TAG[f]}{pools.get(f, 0)}" for f in FAMILIES) + f".{topology}"


def tx_cohort(shape: Shape) -> tuple[str | None, str | None]:
    """(cohort key, None) for a transaction the plan shapes can describe, else
    (None, exclusion reason)."""
    if shape.legs_other:
        return None, "other_protocol_legs"
    if shape.source_tokens != 1 or shape.sink_tokens != 1:
        return None, "cyclic_or_multi_io"  # arbitrage cycles, multi-in/out batches
    if (
        shape.legs_concentrated != shape.pools_concentrated
        or shape.legs_constant_product != shape.pools_constant_product
    ):
        return None, "repeated_pool_calls"
    if shape.distinct_pools < shape.tokens - 1:
        return None, "disconnected"
    topology = "chain" if shape.distinct_pools == shape.tokens - 1 else "parallel"
    pools = {
        "concentrated": shape.pools_concentrated,
        "liquidity_book": shape.pools_liquidity_book,
        "constant_product": shape.pools_constant_product,
    }
    return cohort_key(pools, topology), None


def plan_cohort(route_features: Mapping[str, int]) -> tuple[str | None, str | None]:
    """The cohort of an evaluated complete plan, or (None, why it has none)."""
    calls = {f: int(route_features.get(f"pool_calls_{f}", 0)) for f in FAMILIES}
    if sum(calls.values()) == 0:
        return None, "plan makes no pool call"
    if sum(calls.values()) != int(route_features.get("pool_calls", sum(calls.values()))):
        return None, "plan calls a pool family the model does not know"
    if int(route_features.get("repeated_pool_calls", 0)) > 0:
        return None, "plan reuses a physical pool (shared-pool graph): no historical cohort"
    split = int(route_features.get("split_funds", 0)) or int(route_features.get("merge_steps", 0))
    return cohort_key(calls, "parallel" if split else "chain"), None


def assign_split(key: str, holdout_of_10: int) -> str:
    """Deterministic from the exported sample key (independent of the sampling bits)."""
    return "holdout" if int(key, 16) % 10 < holdout_of_10 else "train"


# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cohort:
    key: str
    status: str
    reason: str | None
    n_train: int
    n_holdout: int
    gas_used_median: int
    l1_fee_median_wei: int
    lb_bins_max: int | None  # training maximum of LB Swap events (bins); None: no LB pool
    nominal_fee_wei: int
    low_multiplier: Decimal | None
    high_multiplier: Decimal | None
    holdout: dict[str, Any]
    routers: dict[str, int]
    validated_receipts: int


@dataclass(frozen=True)
class CostModel:
    model_id: str
    sha256: str
    path: str
    snapshot_block: int
    reference_gas_price_wei: int
    fee_rules: FeeRules
    cohorts: dict[str, Cohort]
    document: dict[str, Any]

    def native_cost(self, route_features: Mapping[str, int]) -> dict[str, Any]:
        """Native-fee estimate and applicability for one complete plan."""
        key, why = plan_cohort(route_features)
        out: dict[str, Any] = {"model_id": self.model_id, "model_sha256": self.sha256}
        if key is None:
            return out | {"status": UNSUPPORTED, "cohort": None, "reason": why}
        cohort = self.cohorts.get(key)
        if cohort is None:
            return out | {
                "status": UNSUPPORTED,
                "cohort": key,
                "reason": "no historical transaction of this shape (extrapolated)",
            }
        out |= {
            "cohort": key,
            "native_nominal_wei": str(cohort.nominal_fee_wei),
        }
        if cohort.status != SUPPORTED:
            return out | {"status": LOW_CONFIDENCE, "reason": cohort.reason}
        bins = int(route_features.get("lb_bins_swapped", 0))
        if cohort.lb_bins_max is not None and bins > cohort.lb_bins_max:
            return out | {
                "status": LOW_CONFIDENCE,
                "reason": (
                    f"plan crosses {bins} LB bins, beyond the cohort's training maximum "
                    f"{cohort.lb_bins_max} (extrapolated)"
                ),
            }
        assert cohort.low_multiplier is not None and cohort.high_multiplier is not None
        return out | {
            "status": SUPPORTED,
            "reason": None,
            "native_low_wei": str(_scale(cohort.nominal_fee_wei, cohort.low_multiplier)),
            "native_high_wei": str(_scale(cohort.nominal_fee_wei, cohort.high_multiplier)),
        }

    def plan_cost(
        self, route_features: Mapping[str, int], token_out: str, prices: PriceContext | None
    ) -> dict[str, Any]:
        """`native_cost` converted into `token_out` raw units through the frozen price
        context (rounded up: a cost is never understated by rounding). Only a
        `supported` cost with known prices is `net_rankable`."""
        detail = self.native_cost(route_features)
        detail["net_rankable"] = False
        if detail["status"] != SUPPORTED:
            return detail
        if prices is None:
            return detail | {"status": UNKNOWN_PRICE, "reason": "no frozen price context"}
        try:
            via = str(prices.native["via_token"])
            for scenario in ("nominal", "low", "high"):
                usd = prices.usd_value(via, int(detail[f"native_{scenario}_wei"]))
                detail[f"{scenario}_out_raw"] = str(prices.raw_amount_for_usd(token_out, usd))
        except PriceError as exc:
            for scenario in ("nominal", "low", "high"):
                detail.pop(f"{scenario}_out_raw", None)
            return detail | {"status": UNKNOWN_PRICE, "reason": str(exc)}
        detail["net_rankable"] = True
        return detail


def _scale(value: int, multiplier: Decimal) -> int:
    return int((Decimal(value) * multiplier).to_integral_value())


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation empirical quantile (numpy's default 'linear' method)."""
    if not sorted_values:
        raise CostModelError("quantile of an empty sample")
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _round(x: float) -> str:
    return f"{x:.6f}"


def holdout_statistics(
    predicted_wei: int,
    rows: Sequence[Mapping[str, Any]],
    rules: FeeRules,
    reference_price: int,
    quantiles: tuple[float, float],
) -> dict[str, Any]:
    """Relative errors of reference-priced holdout costs against the cohort prediction,
    plus the as-paid ratio (each sender's own price) for context."""
    errors = sorted(
        rules.total_fee_wei(int(r["gas_used"]), reference_price, int(r["l1_fee_wei"]))
        / predicted_wei
        - 1.0
        for r in rows
    )
    paid = sorted(
        rules.total_fee_wei(int(r["gas_used"]), int(r["gas_price_wei"]), int(r["l1_fee_wei"]))
        / predicted_wei
        for r in rows
    )
    lo, hi = quantiles
    return {
        "n": len(rows),
        "rel_error_median": _round(_quantile(errors, 0.5)),
        "rel_error_mean_abs": _round(sum(abs(e) for e in errors) / len(errors)),
        "rel_error_q_low": _round(_quantile(errors, lo)),
        "rel_error_q_high": _round(_quantile(errors, hi)),
        "rel_error_min": _round(errors[0]),
        "rel_error_max": _round(errors[-1]),
        "as_paid_ratio_median": _round(_quantile(paid, 0.5)),
        "as_paid_ratio_q_low": _round(_quantile(paid, lo)),
        "as_paid_ratio_q_high": _round(_quantile(paid, hi)),
    }


def _row(tx: TxSample) -> dict[str, Any]:
    assert tx.gas_used is not None and tx.gas_price_wei is not None
    assert tx.l1_fee_wei is not None
    return {
        "key": tx.key,
        "gas_used": tx.gas_used,
        "gas_price_wei": str(tx.gas_price_wei),
        "l1_fee_wei": str(tx.l1_fee_wei),
        "lb_bins": tx.shape.legs_liquidity_book,
    }


def fit_cost_model(
    config: CostConfig,
    sample_set: SampleSet,
    rules: FeeRules,
    *,
    provenance: Mapping[str, Any],
    validated_receipts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Deterministic fit -> the `cost-model/1` document (see module docstring)."""
    validated_receipts = validated_receipts or {}
    exclusions: Counter[str] = Counter()
    groups: dict[str, dict[str, list[TxSample]]] = defaultdict(lambda: {"train": [], "holdout": []})
    for tx in sample_set.txs:
        if not tx.success or tx.gas_used is None or tx.gas_price_wei is None:
            exclusions["no_successful_transaction_row"] += 1
            continue
        if tx.l1_fee_wei is None:
            exclusions["no_l1_fee"] += 1
            continue
        key, why = tx_cohort(tx.shape)
        if key is None:
            assert why is not None
            exclusions[why] += 1
            continue
        groups[key][assign_split(tx.key, config.holdout_of_10)].append(tx)
    train_all = [t for g in groups.values() for t in g["train"]]
    if not train_all:
        raise CostModelError("no training transaction in any cohort")
    reference_price = median_low(sorted(t.gas_price_wei or 0 for t in train_all))
    family_train = {
        f: sum(
            1
            for t in train_all
            if (
                t.shape.pools_concentrated,
                t.shape.pools_liquidity_book,
                t.shape.pools_constant_product,
            )[FAMILIES.index(f)]
        )
        for f in FAMILIES
    }
    cohorts: dict[str, Any] = {}
    for key in sorted(groups):
        train, holdout = groups[key]["train"], groups[key]["holdout"]
        if not train:
            continue
        gas_med = median_low(sorted(t.gas_used or 0 for t in train))
        l1_med = median_low(sorted(t.l1_fee_wei or 0 for t in train))
        nominal = rules.total_fee_wei(gas_med, reference_price, l1_med)
        families_used = [f for f in FAMILIES if f"{_FAMILY_TAG[f]}0" not in key.split(".")]
        reason = None
        if len(train) < config.min_train or len(holdout) < config.min_holdout:
            reason = (
                f"{len(train)} training / {len(holdout)} holdout transaction(s); needs "
                f">= {config.min_train} / >= {config.min_holdout}"
            )
        elif any(family_train[f] < config.min_family_train for f in families_used):
            reason = "a pool family of this cohort has too few training transactions"
        rows = [_row(t) for t in sorted(holdout, key=lambda t: t.key)]
        stats = (
            holdout_statistics(nominal, rows, rules, reference_price, config.scenario_quantiles)
            if rows
            else {"n": 0}
        )
        low = high = None
        if reason is None:
            low = str(max(Decimal(0), Decimal(1) + Decimal(stats["rel_error_q_low"])))
            high = str(Decimal(1) + Decimal(stats["rel_error_q_high"]))
        lb_bins = [t.shape.legs_liquidity_book for t in train] if "l0" not in key else []
        routers = Counter(t.router or "none" for t in train + holdout)
        cohorts[key] = {
            "status": SUPPORTED if reason is None else LOW_CONFIDENCE,
            "reason": reason,
            "n_train": len(train),
            "n_holdout": len(holdout),
            "gas_used_median": gas_med,
            "l1_fee_median_wei": str(l1_med),
            "lb_bins_max": max(lb_bins) if lb_bins else None,
            "nominal_fee_wei": str(nominal),
            "low_multiplier": low,
            "high_multiplier": high,
            "holdout_statistics": stats,
            "holdout_rows": rows,
            "train_keys": sorted(t.key for t in train),
            "routers": dict(sorted(routers.items(), key=lambda kv: (-kv[1], kv[0]))[:5]),
            "distinct_routers": len(routers),
            "validated_receipts": int(validated_receipts.get(key, 0)),
        }
    return {
        "schema": COST_MODEL_SCHEMA,
        "model_id": config.model_id,
        "snapshot": {
            "chain_id": config.chain_id,
            "block_number": config.block_number,
            "block_hash": config.block_hash,
            "block_timestamp": config.block_timestamp,
        },
        "window": {"start": config.window_start, "end": config.window_end},
        "provenance": dict(provenance),
        "fee_rules": rules.to_dict(),
        "reference_gas_price_wei": str(reference_price),
        "reference_gas_price_rule": "median_low of training effective gas prices",
        "split": {
            "rule": "holdout iff int(sample_key, 16) % 10 < holdout_of_10",
            "holdout_of_10": config.holdout_of_10,
        },
        "rules": {
            "min_train": config.min_train,
            "min_holdout": config.min_holdout,
            "min_family_train": config.min_family_train,
            "scenario_quantiles": list(config.scenario_quantiles),
            "scenario_meaning": (
                "low/high = nominal x (1 + empirical holdout relative-error quantile); "
                "not a confidence interval"
            ),
        },
        "family_train": family_train,
        "exclusions": dict(sorted(exclusions.items())),
        "cohorts": cohorts,
        "features_not_modeled": [
            "CL initialized ticks crossed (not available for historical transactions)",
            "failed/reverted transactions (dex.trades has no rows for them)",
            "LB bins beyond the per-cohort training maximum",
        ],
    }


def canonical_model_bytes(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, indent=1, sort_keys=True) + "\n").encode()


def write_cost_model(document: Mapping[str, Any], path: str | Path) -> str:
    """Write the artifact once. Rewriting the same bytes is a no-op; different bytes at
    an existing path are refused (a new model gets a new path/model id)."""
    data = canonical_model_bytes(document)
    path = Path(path)
    if path.exists() and path.read_bytes() != data:
        raise CostModelError(
            f"{path}: a different cost-model artifact already exists; artifacts are "
            "immutable -- write the new model to a new path with a new model_id"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _decimal(value: Any, where: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 -- any parse failure is a malformed artifact
        raise CostModelError(f"{where}: not a decimal: {value!r}") from exc
    if not d.is_finite() or d < 0:
        raise CostModelError(f"{where}: must be a finite non-negative decimal")
    return d


def parse_cost_model(
    document: Mapping[str, Any], *, sha256: str, path: str = "<memory>"
) -> CostModel:
    """Validate a `cost-model/1` document and check it reproduces itself: every cohort's
    nominal fee from its medians, and its holdout statistics and scenario multipliers
    from its stored holdout rows."""
    if document.get("schema") != COST_MODEL_SCHEMA:
        raise CostModelError(
            f"{path}: schema {document.get('schema')!r} is not {COST_MODEL_SCHEMA}"
        )
    fr = document["fee_rules"]
    rules = FeeRules(
        operator_fee_scalar=int(fr["operator_fee_scalar"]),
        operator_fee_constant=int(fr["operator_fee_constant"]),
    )
    reference = int(document["reference_gas_price_wei"])
    quantiles = tuple(float(q) for q in document["rules"]["scenario_quantiles"])
    assert len(quantiles) == 2
    cohorts: dict[str, Cohort] = {}
    for key, raw in document["cohorts"].items():
        where = f"{path}: cohort {key}"
        nominal = rules.total_fee_wei(
            int(raw["gas_used_median"]), reference, int(raw["l1_fee_median_wei"])
        )
        if str(nominal) != raw["nominal_fee_wei"]:
            raise CostModelError(f"{where}: nominal fee does not reproduce")
        rows = raw["holdout_rows"]
        if len(rows) != raw["n_holdout"]:
            raise CostModelError(f"{where}: holdout rows != n_holdout")
        expected = (
            holdout_statistics(nominal, rows, rules, reference, (quantiles[0], quantiles[1]))
            if rows
            else {"n": 0}
        )
        if expected != raw["holdout_statistics"]:
            raise CostModelError(f"{where}: holdout statistics do not reproduce")
        low = high = None
        if raw["status"] == SUPPORTED:
            low = _decimal(raw["low_multiplier"], f"{where}.low_multiplier")
            high = _decimal(raw["high_multiplier"], f"{where}.high_multiplier")
            want_low = max(Decimal(0), Decimal(1) + Decimal(expected["rel_error_q_low"]))
            want_high = Decimal(1) + Decimal(expected["rel_error_q_high"])
            if (low, high) != (want_low, want_high):
                raise CostModelError(f"{where}: scenario multipliers do not reproduce")
        elif raw["status"] != LOW_CONFIDENCE:
            raise CostModelError(f"{where}: unknown status {raw['status']!r}")
        cohorts[key] = Cohort(
            key=key,
            status=raw["status"],
            reason=raw["reason"],
            n_train=int(raw["n_train"]),
            n_holdout=int(raw["n_holdout"]),
            gas_used_median=int(raw["gas_used_median"]),
            l1_fee_median_wei=int(raw["l1_fee_median_wei"]),
            lb_bins_max=raw["lb_bins_max"],
            nominal_fee_wei=nominal,
            low_multiplier=low,
            high_multiplier=high,
            holdout=dict(raw["holdout_statistics"]),
            routers=dict(raw["routers"]),
            validated_receipts=int(raw["validated_receipts"]),
        )
    return CostModel(
        model_id=str(document["model_id"]),
        sha256=sha256,
        path=path,
        snapshot_block=int(document["snapshot"]["block_number"]),
        reference_gas_price_wei=reference,
        fee_rules=rules,
        cohorts=cohorts,
        document=dict(document),
    )


def load_cost_model(path: str | Path, *, expected_sha256: str | None = None) -> CostModel:
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise CostModelError(
            f"{path}: sha256 {digest} is not the declared {expected_sha256} (the artifact "
            "changed; a new model needs a new declared identity)"
        )
    document = json.loads(data)
    if canonical_model_bytes(document) != data:
        raise CostModelError(f"{path}: not in canonical form")
    return parse_cost_model(document, sha256=digest, path=str(path))


def validation_report(model: CostModel) -> list[str]:
    """Human-readable per-cohort validation table (docs/references/cost-model.md)."""
    lines = [
        f"cost model {model.model_id} sha256={model.sha256}",
        f"reference gas price {model.reference_gas_price_wei} wei; "
        f"operator fee {model.fee_rules.operator_fee_per_gas_wei} wei/gas "
        f"+ {model.fee_rules.operator_fee_constant}",
        "cohort | status | train | holdout | gas med | nominal MNT | "
        "err med | err q_low..q_high | |err| mean | as-paid q_low..q_high | receipts",
    ]
    for key, c in sorted(model.cohorts.items(), key=lambda kv: -(kv[1].n_train + kv[1].n_holdout)):
        h = c.holdout
        if h.get("n"):
            err = (
                f"{h['rel_error_median']} | {h['rel_error_q_low']}..{h['rel_error_q_high']} | "
                f"{h['rel_error_mean_abs']} | "
                f"{h['as_paid_ratio_q_low']}..{h['as_paid_ratio_q_high']}"
            )
        else:
            err = "- | - | - | -"
        lines.append(
            f"{key} | {c.status} | {c.n_train} | {c.n_holdout} | {c.gas_used_median} | "
            f"{Decimal(c.nominal_fee_wei) / Decimal(10**18):.6f} | {err} | {c.validated_receipts}"
        )
    return lines


def receipt_selection(
    config: CostConfig, sample_set: SampleSet, *, per_cohort: int = 2
) -> list[TxSample]:
    """Deterministic receipt-check selection: the `per_cohort` smallest-key successful
    holdout transactions of each cohort, largest cohorts first, up to
    `evidence.receipt_samples` in total."""
    by_cohort: dict[str, list[TxSample]] = defaultdict(list)
    for tx in sample_set.txs:
        key, _ = tx_cohort(tx.shape)
        if (
            key is not None
            and tx.success
            and assign_split(tx.key, config.holdout_of_10) == ("holdout")
        ):
            by_cohort[key].append(tx)
    chosen: list[TxSample] = []
    for key in sorted(by_cohort, key=lambda k: (-len(by_cohort[k]), k)):
        chosen += sorted(by_cohort[key], key=lambda t: t.key)[:per_cohort]
        if len(chosen) >= config.receipt_samples:
            break
    return chosen[: config.receipt_samples]


def receipts_per_cohort(
    explained_tx_hashes: Iterable[str], sample_set: SampleSet
) -> dict[str, int]:
    """Cohort -> number of receipts whose sender balance drop equals the recomputed total
    (`check_fee_evidence`): independent validation of that cohort's fee accounting."""
    shapes = {t.tx_hash: t.shape for t in sample_set.txs}
    out: Counter[str] = Counter()
    for tx_hash in explained_tx_hashes:
        key, _ = tx_cohort(shapes[tx_hash])
        if key is not None:
            out[key] += 1
    return dict(out)

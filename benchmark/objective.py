"""Shared complete-plan cost/objective seam (docs/DESIGN.md §2.9).

`ObjectiveContext` is the single interface every algorithm uses to score a fully
evaluated plan when choosing among candidates (e.g. `direct` picking the best
admitted pool). Three modes share this one interface:

- `gross_only()` -- scores purely by `Evaluation.gross_output` (a labeled development
  mode: no cost model).
- `synthetic_fixed_cost(...)` -- subtracts a fixed, explicitly labeled synthetic
  cost from gross output. This exists **only** so algorithm/selection-ordering
  tests can exercise cost-sensitive selection before empirical calibration
  exists (docs/DESIGN.md §2.9: "These modes let algorithm work proceed before
  empirical calibration; they cannot substantiate real net-output claims.").
  Every score and every serialized result produced under this mode carries a
  `SYNTHETIC` marker in `label`/`is_synthetic` so a report can never present it
  as a real net-output result.
- `empirical_cost(model)` (WHI-1445) -- the holdout-validated cost model of
  `benchmark.costs`, a function of the *complete* evaluated plan's `route_features`
  (non-additive: one whole-transaction fee per plan, keyed by the plan's shape), bound
  by `bind(bundle)` to the bundle's frozen price context to convert the native fee into
  the case's output token. A plan whose shape is `supported` and whose prices are known
  gets `estimated_net_output = gross - nominal cost`; any other plan gets **no net
  score** (`estimated_net_output is None`) and its cost detail says why.

Selection under `empirical_cost` (every algorithm's final choice goes through
`score`): a net-rankable plan scores its estimated net output; a plan without a
reliable net score scores `gross - UNRANKED_OFFSET` (2**257), which is below every
net-rankable score (gross and cost are uint256-bounded, so net > -2**256) but keeps gross
order among unranked plans. So an algorithm picks
the best *rankable* complete plan when it has one, and never trades a known net result
for an unrankable one on the strength of an unknown cost. Reports use
`estimated_net_output`/`cost` status, never the sentinel score, for net comparisons.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from routing.evaluator import EvalStatus, Evaluation

if TYPE_CHECKING:
    from benchmark.costs import CostModel
    from snapshot.models import SnapshotBundle
    from snapshot.prices import PriceContext

ObjectiveMode = Literal["gross_only", "synthetic_fixed_cost", "empirical_cost"]

_MODES: tuple[ObjectiveMode, ...] = ("gross_only", "synthetic_fixed_cost", "empirical_cost")

# gross and cost are uint256-bounded, so `gross - UNRANKED_OFFSET` < -2**256 < every net.
UNRANKED_OFFSET = 1 << 257


@dataclass(frozen=True)
class ObjectiveContext:
    """Construct via `gross_only()`/`synthetic_fixed_cost()`/`empirical_cost()` below
    rather than directly, so each mode's fields are always validated."""

    mode: ObjectiveMode
    fixed_cost: int = 0  # only meaningful when mode == "synthetic_fixed_cost";
    # denominated in the case's output-token raw units.
    cost_model: CostModel | None = None  # empirical_cost only
    bound: bool = False  # empirical_cost: bound to a bundle's price context
    prices: PriceContext | None = None  # the bound price context (None: missing)
    price_context_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in _MODES:
            raise ValueError(f"unknown objective mode: {self.mode!r} (expected one of {_MODES})")
        if self.mode == "gross_only" and self.fixed_cost != 0:
            raise ValueError("gross_only objective must not carry a nonzero fixed_cost")
        if self.mode == "synthetic_fixed_cost" and self.fixed_cost < 0:
            raise ValueError(f"fixed_cost must be non-negative, got {self.fixed_cost}")
        if (self.mode == "empirical_cost") != (self.cost_model is not None):
            raise ValueError("a cost_model is required by, and only by, empirical_cost")
        if self.mode == "empirical_cost" and self.fixed_cost != 0:
            raise ValueError("empirical_cost objective must not carry a fixed_cost")

    @property
    def is_synthetic(self) -> bool:
        return self.mode == "synthetic_fixed_cost"

    @property
    def label(self) -> str:
        """A human-readable, always-visible label. Synthetic-mode labels always
        start with the literal string `SYNTHETIC` so it can never be mistaken
        for a calibrated empirical result in a report or saved run record."""
        if self.mode == "gross_only":
            return "gross-only (no cost model; docs/DESIGN.md §2.9 development mode)"
        if self.mode == "empirical_cost":
            assert self.cost_model is not None
            prices = (
                "unbound"
                if not self.bound
                else (
                    f"prices sha256 {self.price_context_sha256[:12]}"
                    if self.price_context_sha256
                    else "NO price context (no net scores)"
                )
            )
            return (
                f"empirical cost {self.cost_model.model_id} sha256 "
                f"{self.cost_model.sha256[:12]} ({prices}); holdout-validated cohorts only, "
                "unsupported shapes unranked on net"
            )
        return (
            f"SYNTHETIC fixed-cost={self.fixed_cost} (test-only synthetic cost, "
            "NOT an empirical net-output claim -- docs/DESIGN.md §2.9)"
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mode": self.mode, "fixed_cost": self.fixed_cost}
        if self.mode == "empirical_cost":
            assert self.cost_model is not None
            out["cost_model"] = {
                "model_id": self.cost_model.model_id,
                "path": self.cost_model.path,
                "sha256": self.cost_model.sha256,
            }
            out["price_context_sha256"] = self.price_context_sha256
        return out

    def bind(self, bundle: SnapshotBundle) -> ObjectiveContext:
        """The objective a run over `bundle` uses. Only `empirical_cost` changes: it takes
        the bundle's frozen price context (and its file hash). A model calibrated for a
        different snapshot block is refused; a bundle without a price context binds with
        none, so every plan keeps its gross output but gets no net score."""
        if self.mode != "empirical_cost":
            return self
        assert self.cost_model is not None
        if bundle.block.number != self.cost_model.snapshot_block:
            raise ValueError(
                f"cost model {self.cost_model.model_id} is calibrated for block "
                f"{self.cost_model.snapshot_block}, not bundle block {bundle.block.number}"
            )
        sha = None
        if bundle.prices is not None:
            sha = hashlib.sha256(
                (Path(bundle.source_path) / "prices.json").read_bytes()
            ).hexdigest()
        return replace(self, bound=True, prices=bundle.prices, price_context_sha256=sha)

    def plan_cost(self, route_features: dict[str, int], token_out: str) -> dict[str, Any]:
        """Cost detail of one complete `ok` plan (`benchmark.costs.CostModel.plan_cost`);
        called by `routing.evaluator.evaluate`."""
        if self.mode != "empirical_cost" or self.cost_model is None:
            raise ValueError(f"{self.mode} has no plan cost model")
        if not self.bound:
            raise ValueError("empirical_cost objective used before bind(bundle)")
        return self.cost_model.plan_cost(route_features, token_out, self.prices)

    def score(self, evaluation: Evaluation) -> int:
        """Score a *completed* Evaluation for candidate comparison. Reads the
        already-computed `estimated_net_output`/`gross_output` fields on the
        Evaluation itself (populated by `routing.evaluator.evaluate`, which was
        given this same `ObjectiveContext`) rather than recomputing cost
        arithmetic here -- this keeps exactly one place that turns an objective
        into a cost figure. Only defined for `EvalStatus.OK` plans -- callers
        must filter failed evaluations before scoring (an invalid plan has no
        meaningful output to compare)."""
        if evaluation.status is not EvalStatus.OK:
            raise ValueError(f"cannot score a non-ok evaluation (status={evaluation.status})")
        if evaluation.estimated_net_output is not None:
            return evaluation.estimated_net_output
        if self.mode == "empirical_cost":
            return evaluation.gross_output - UNRANKED_OFFSET
        return evaluation.gross_output


def gross_only() -> ObjectiveContext:
    return ObjectiveContext(mode="gross_only")


def synthetic_fixed_cost(fixed_cost: int) -> ObjectiveContext:
    return ObjectiveContext(mode="synthetic_fixed_cost", fixed_cost=fixed_cost)


def empirical_cost(model: CostModel) -> ObjectiveContext:
    return ObjectiveContext(mode="empirical_cost", cost_model=model)

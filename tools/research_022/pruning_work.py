"""WHI-1602 untimed work pass (pruning contract §11.4, §12.3(2)).

The ordinary `main.py run` records `quotes_executed` and the pruning counters but not what an
executed quote cost inside the pool maths. This pass is `main.py run` itself, with one
harness-side seam: every algorithm factory the runner looks up is wrapped so that, inside the
solve window of the (spawned) worker, the three pool-family quote functions
`pools.{constant_product,concentrated,liquidity_book}.quote_exact_in` -- the calls
`pools.quote.quote_exact_in` dispatches to once it has charged the meter -- are replaced by
counting twins. Each result's `features` are summed: CL `swap_steps` (and initialized ticks
crossed) and LB `lb_bins_swapped`. The sums, the number of executed quotes per family and a
SHA-256 of the canonical submitted plan are added to `search_stats["r022_work"]`.

Nothing here is timed or compared on time: a work-pass record is never an exactness record and
never a timing record (§11.4: "never inside a timed run"). The ordinary runner, the solvers and
their results are otherwise untouched; the wrappers are restored when the solve returns.

    uv run python tools/research_022/pruning_work.py run --bundle B --profile P --results-dir D \
        --strategies profile
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _p in (str(REPO), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pools import concentrated, constant_product, liquidity_book  # noqa: E402
from routing.algorithms.base import AlgorithmFactory, SolveResult  # noqa: E402

KEY = "r022_work"
FAMILIES = ("constant_product", "concentrated", "liquidity_book")
MODULES: dict[str, Any] = {
    "constant_product": constant_product,
    "concentrated": concentrated,
    "liquidity_book": liquidity_book,
}
# the `features` of a result that are summed, per family (pools/concentrated.py, liquidity_book.py)
FEATURES = {
    "concentrated": ("swap_steps", "initialized_ticks_crossed"),
    "liquidity_book": ("lb_bins_swapped",),
    "constant_product": (),
}


class WorkTally:
    """Executed quotes of one solve: calls per family and summed integer features."""

    def __init__(self) -> None:
        self.calls = dict.fromkeys(FAMILIES, 0)
        self.features: dict[str, int] = {}

    def add(self, family: str, result: Any) -> None:
        self.calls[family] += 1
        features = getattr(result, "features", None) or {}
        for name in FEATURES[family]:
            self.features[name] = self.features.get(name, 0) + int(features.get(name, 0))

    def record(self) -> dict[str, Any]:
        return {
            "quotes_by_family": dict(self.calls),
            "quotes_executed": sum(self.calls.values()),
            "cl_swap_steps": self.features.get("swap_steps", 0),
            "cl_initialized_ticks_crossed": self.features.get("initialized_ticks_crossed", 0),
            "lb_bins_swapped": self.features.get("lb_bins_swapped", 0),
        }


def _twin(family: str, original: Callable[..., Any], tally: WorkTally) -> Callable[..., Any]:
    @functools.wraps(original)
    def counting(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        tally.add(family, result)
        return result

    return counting


@contextmanager
def counting_quotes() -> Iterator[WorkTally]:
    """The family quote functions replaced by counting twins for the block, restored after it
    (also after an error or a `QuoteLimitExceeded`)."""
    tally = WorkTally()
    originals = {family: module.quote_exact_in for family, module in MODULES.items()}
    try:
        for family, module in MODULES.items():
            module.quote_exact_in = _twin(family, originals[family], tally)
        yield tally
    finally:
        for family, module in MODULES.items():
            module.quote_exact_in = originals[family]


def plan_sha256(result: SolveResult) -> str | None:
    """SHA-256 of the canonical JSON of the submitted plan (`None` without one)."""
    if result.plan is None:
        return None
    text = json.dumps(dataclasses.asdict(result.plan), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def counted_solve(solve: Callable[..., Any], case: Any, context: Any, budget: Any) -> Any:
    """`solve` with the counting twins installed; its `SolveResult` gains `search_stats[KEY]`."""
    with counting_quotes() as tally:
        result = solve(case, context, budget)
    if not isinstance(result, SolveResult):
        return result  # the worker reports it as an algorithm error
    search = dict(result.search_stats)
    search[KEY] = {**tally.record(), "plan_sha256": plan_sha256(result)}
    return dataclasses.replace(result, search_stats=search)


def counted_factory(factory: AlgorithmFactory) -> AlgorithmFactory:
    """`factory` whose `solve` is `counted_solve` (module-level function + partial: pickled by
    reference into the spawned worker; `prepare` and every declaration unchanged)."""
    return dataclasses.replace(factory, solve=functools.partial(counted_solve, factory.solve))


def install() -> None:
    """Make the ordinary runner look every algorithm up through `counted_factory`."""
    import benchmark.runner

    runner: Any = benchmark.runner
    original = runner.get_algorithm

    def instrumented(name: str) -> AlgorithmFactory:
        return counted_factory(original(name))

    runner.get_algorithm = instrumented


def work_of(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The work block of a work-pass record (`None` for an ordinary record or a failed solve)."""
    search = record.get("search")
    block = search.get(KEY) if isinstance(search, Mapping) else None
    return block if isinstance(block, Mapping) else None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "run":
        print("usage: pruning_work.py run <main.py run arguments>", file=sys.stderr)
        return 2
    import main as cli

    install()
    return int(cli.main(args))


if __name__ == "__main__":
    # import under the module's own name so the spawned workers unpickle `counted_solve`
    # from `pruning_work`, not from `__main__`
    import pruning_work

    raise SystemExit(pruning_work.main())

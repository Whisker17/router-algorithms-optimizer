"""WHI-1603 (release review R1-F2): `metis_history` and `incremental_graph_repair` record
the bundle's real corpus cohort in their `r021.domain/1` `universe.cohort` (R021-C/1
§3.1, `contract.md`), exactly as `direct_split_certified.cohort_of` derives it for
`direct_split_certified` and `cfmm_dual`: `fixture` for a synthetic bundle,
`sor_compatible` for a matched-cohort cut, else `full_source`. Before the fix both
identities hard-coded `full_source` for every non-synthetic bundle."""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmark.objective import gross_only
from benchmark.profile import preset_options
from routing.algorithms.base import AlgorithmConfig, Budget, SolveContext
from routing.algorithms.direct_split_certified import cohort_of
from routing.algorithms.registry import get_algorithm
from snapshot.bundle import load_bundle
from snapshot.corpus import sor_cohort_bundle
from snapshot.models import SnapshotBundle

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "tests" / "fixtures" / "corpus" / "bundle"
SYNTHETIC = REPO / "tests" / "fixtures" / "synthetic"
PARAMS = {"max_hops": 2, "max_splits": 4, "percent_step": 5, "chunks": 2, "label_hops": 2}
IDENTITIES = ("metis_history", "incremental_graph_repair")


@pytest.fixture(scope="module")
def bundles(tmp_path_factory: pytest.TempPathFactory) -> dict[str, SnapshotBundle]:
    full = load_bundle(CORPUS)
    out = tmp_path_factory.mktemp("cohort") / "sor"
    sor_cohort_bundle(full, out)
    cut = load_bundle(out)
    return {"full_source": full, "sor_compatible": cut, "fixture": load_bundle(SYNTHETIC)}


def _params(name: str) -> dict[str, int]:
    factory = get_algorithm(name)
    allowed = set(factory.search_params) | set(factory.graph_params)
    return {k: v for k, v in PARAMS.items() if k in allowed}


@pytest.mark.parametrize("name", IDENTITIES)
@pytest.mark.parametrize("expected", ["full_source", "sor_compatible", "fixture"])
def test_domain_universe_records_the_bundle_cohort(
    bundles: dict[str, SnapshotBundle], name: str, expected: str
) -> None:
    bundle = bundles[expected]
    assert cohort_of(bundle) == expected  # the fixtures carry the cohort under test
    factory = get_algorithm(name)
    assert factory.prepare is not None
    config = AlgorithmConfig(name, _params(name), preset_options(factory))
    context = SolveContext(bundle, gross_only(), factory.prepare(bundle, config))
    case = bundle.cases[0]
    result = factory.solve(case, context, Budget())
    domain = result.search_stats["r021"]["domain"]
    assert domain["universe"]["cohort"] == expected
    assert domain["universe"]["bundle"] == bundle.bundle_hash
    assert domain["universe"]["pools"] == list(bundle.pools)  # nothing else changed

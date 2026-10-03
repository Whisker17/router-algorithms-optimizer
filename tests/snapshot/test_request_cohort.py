"""WHI-1606 (release review R2-F1): the single-request path carries a matched parent's
cohort into the derived request bundle, and nothing else changes.

`derive_request_bundle(..., record_cohort=True)` -- used only by `main.py quote` -- records
`derived_from.cohort = "sor_compatible"` in the request provenance when the parent is a
`sor_compatible` cut; the loader exposes it as `SnapshotBundle.derived_cohort`, which
`direct_split_certified.cohort_of` reads. Every other request bundle keeps its exact bytes:
full-source and synthetic quote bundles, and the latency harness's sentinels (which
deliberately do not record it, so the recorded L01 experiments stay comparable;
docs/DEFERRED_ISSUES.md). The pinned hashes below were measured at H2
`d5470f569af57ad4ef5b11142672a628dc54aec2`, before the change."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
import yaml

from benchmark.latency import build_bundles, load_protocol
from routing.algorithms.direct_split_certified import cohort_of
from snapshot.bundle import BundleError, load_bundle, sha256_file
from snapshot.corpus import sor_cohort_bundle
from snapshot.models import SnapshotBundle
from snapshot.request import derive_request_bundle, parse_amount, request_case, resolve_token

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "corpus" / "bundle"
REAL_PARENT = REPO / "data" / "corpus" / "mantle-5src-101082044" / "bundle"


@pytest.fixture(scope="module")
def parents(tmp_path_factory: pytest.TempPathFactory) -> dict[str, SnapshotBundle]:
    full = load_bundle(FIXTURE)
    out = tmp_path_factory.mktemp("cohort") / "sor"
    sor_cohort_bundle(full, out)  # what `main.py corpus cohort` writes
    return {
        "full_source": full,
        "sor_compatible": load_bundle(out),
        # A synthetic-kind corpus parent: no checked-in synthetic bundle carries token
        # metadata (quote refuses them), so this is the only synthetic request bundle there is.
        "synthetic": dataclasses.replace(full, kind="synthetic"),
    }


def _request(parent: SnapshotBundle, out: Path, *, record_cohort: bool) -> SnapshotBundle:
    tin = resolve_token(parent, "USDC", "--token-in")
    tout = resolve_token(parent, "USDT0", "--token-out")
    case = request_case(parent, tin, tout, parse_amount("10000", tin))
    return derive_request_bundle(parent, case, out, record_cohort=record_cohort)


@pytest.mark.parametrize(
    ("parent", "record_cohort", "bundle_hash", "cohort"),
    [
        # `main.py quote` over a full-source corpus bundle
        ("full_source", True, "5fc95c8887b83d85b500da01879eac5bdb63f05185ef16dd30f5b76c70dd8d02",
         "full_source"),
        ("synthetic", True, "6e90af418d641df89dd0a6953e8d12e6b14ca3ea26e5d9e49bdf290c1771a5b2",
         "fixture"),
        # the default (the latency harness's matched sentinel): unchanged, still full_source
        ("sor_compatible", False,
         "183896a738b6a5e18ccf25592e610b059e7e34045e3ae0f975bbe263dc2167d9", "full_source"),
    ],
)  # fmt: skip
def test_request_bundles_without_a_recorded_cohort_keep_their_h2_bytes(
    parents: dict[str, SnapshotBundle],
    tmp_path: Path,
    parent: str,
    record_cohort: bool,
    bundle_hash: str,
    cohort: str,
) -> None:
    bundle = _request(parents[parent], tmp_path / "r", record_cohort=record_cohort)
    assert bundle.bundle_hash == bundle_hash
    provenance = json.loads((tmp_path / "r" / "provenance.json").read_text())
    assert "cohort" not in provenance["derived_from"] and bundle.derived_cohort is None
    assert cohort_of(bundle) == cohort == cohort_of(load_bundle(tmp_path / "r"))


def test_quote_request_bundle_of_a_matched_cut_records_its_cohort(
    parents: dict[str, SnapshotBundle], tmp_path: Path
) -> None:
    plain = _request(parents["sor_compatible"], tmp_path / "plain", record_cohort=False)
    quote = _request(parents["sor_compatible"], tmp_path / "quote", record_cohort=True)
    a, b = (json.loads((tmp_path / d / "provenance.json").read_text()) for d in ("plain", "quote"))
    assert b["derived_from"].pop("cohort") == "sor_compatible"
    assert a == b  # the recorded cohort is the only difference
    assert quote.bundle_hash != plain.bundle_hash
    assert quote.pools == plain.pools and quote.cases == plain.cases
    reloaded = load_bundle(tmp_path / "quote")  # a saved quote replays with its cohort
    assert reloaded.derived_cohort == quote.derived_cohort == "sor_compatible"
    assert cohort_of(reloaded) == cohort_of(quote) == "sor_compatible"


def test_cohort_of_is_unchanged_for_bundles_that_are_not_request_bundles(
    parents: dict[str, SnapshotBundle],
) -> None:
    assert cohort_of(parents["full_source"]) == "full_source"
    assert cohort_of(parents["sor_compatible"]) == "sor_compatible"
    assert cohort_of(load_bundle(REPO / "tests" / "fixtures" / "synthetic")) == "fixture"
    assert all(b.derived_cohort is None for b in parents.values())


def test_loader_refuses_an_unknown_recorded_cohort(
    parents: dict[str, SnapshotBundle], tmp_path: Path
) -> None:
    _request(parents["sor_compatible"], tmp_path / "r", record_cohort=True)
    path = tmp_path / "r" / "provenance.json"
    path.write_text(path.read_text().replace('"cohort": "sor_compatible"', '"cohort": "other"'))
    manifest = json.loads((tmp_path / "r" / "manifest.json").read_text())
    manifest["checksums"]["provenance.json"] = sha256_file(path)  # a consistent tampering
    (tmp_path / "r" / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BundleError, match="derived_from.cohort"):
        load_bundle(tmp_path / "r")


def _bundle_hashes(protocol: Path, parent: SnapshotBundle, out: Path) -> dict[str, str]:
    bundles = build_bundles(load_protocol(protocol), parent, out)
    return {label: bundle.bundle_hash for label, bundle in bundles.items()}


def test_latency_derived_bundles_keep_their_h2_hashes(tmp_path: Path) -> None:
    """The fixture analogue of the L01 bundles (`tests/benchmark/test_latency.py`)."""
    fixture = load_bundle(FIXTURE)
    doc = yaml.safe_load((REPO / "config" / "latency" / "l01.yaml").read_text())
    profile = REPO / "config" / "daily_gross.yaml"
    doc["parent_bundle"] = {"bundle_id": fixture.bundle_id, "bundle_hash": fixture.bundle_hash}
    doc["profile"] = {"path": str(profile), "sha256": sha256_file(profile)}
    doc["sentinel"] = {"token_in": "USDC", "token_out": "USDT0", "amount": "1500.25"}
    doc["matrix"] = [
        {"case": "emp-09bc4e-779ded-low-2", "split": "tuning", "covers": ["small"]},
        {"case": "emp-09bc4e-779ded-low-1", "split": "held_out", "covers": ["small"]},
        {"case": "nod-09bc4e-c96de2-medium-1", "split": "held_out", "covers": ["no_route"]},
    ]
    protocol = tmp_path / "protocol.yaml"
    protocol.write_text(yaml.safe_dump(doc, sort_keys=False))
    assert _bundle_hashes(protocol, fixture, tmp_path / "b") == {
        "full_source/matrix":
            "1b8ffc7666e6b11e839ddb98b15e37f3de88bf5227a5c71214460e14c06a2e5e",
        "full_source/sentinel":
            "491115d848379e1684d251bace65eab729fb4129d227da04246fe61884912129",
        "sor_compatible/matrix":
            "fbcf664d5a753a7d602eaf62777ee255511fcde4940f724307b20f1cdd5ff8f5",
        "sor_compatible/sentinel":
            "20fd165fdacfb5f501ff9b22c076b932d3bbcddf22fb96a26ea1f51f1f21346a",
    }


@pytest.mark.skipif(not REAL_PARENT.is_dir(), reason="the frozen real corpus (data/) is absent")
def test_l01_derived_bundles_keep_their_recorded_hashes(tmp_path: Path) -> None:
    """The derived-bundle hashes recorded by the 0.1.2 L01 experiments and the 0.2.1
    stage-L experiments; `report.latency compare` refuses experiments whose derived
    bundles differ."""
    hashes = _bundle_hashes(REPO / "config" / "latency" / "l01.yaml",
                            load_bundle(REAL_PARENT), tmp_path / "b")  # fmt: skip
    assert hashes == {
        "full_source/matrix":
            "72f4f4e4c08d4c34e8480ee56ca8eebab005bb15eb3865349d7df12ba59cfb04",
        "full_source/sentinel":
            "f67f9d2bab91be39e05c8fdb621a7c2e4f1689c106160944ad71f65969445693",
        "sor_compatible/matrix":
            "0d448c4795180ed8c731d29cd07b0627714e48b3ad2be4dc57e518772daefd70",
        "sor_compatible/sentinel":
            "c967974a204a19f03439ea4968be2a5225cdd1ccc4a13c569d50d8f211db092c",
    }

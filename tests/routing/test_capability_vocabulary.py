"""WHI-1605 (release review R1-F1): `Capabilities.protocols` has one meaning, the protocol-
family ceiling (`routing.algorithms.base.protocol_families`), and every factory agrees with
the R021-C/1 identity table of `benchmark.diagnostics.IDENTITIES`."""

from __future__ import annotations

import json

import pytest

from benchmark.diagnostics import IDENTITIES, PROTOCOLS
from benchmark.strategies import R021_ADDITIONS
from routing.algorithms import direct_split, direct_split_certified
from routing.algorithms.base import Capabilities, protocol_families
from routing.algorithms.registry import ALGORITHMS

# The recorded `capabilities` of every pre-existing identity, byte for byte as at base
# cb2f806 (resolved profiles and their literal replay embed exactly these).
PRE_EXISTING = {
    "direct": '{"multi_hop": false, "shared_pools": false, "split": false}',
    "single_path": '{"multi_hop": true, "shared_pools": false, "split": false}',
    "direct_split": '{"multi_hop": false, "shared_pools": false, "split": true}',
    "path_split": '{"multi_hop": true, "shared_pools": false, "split": true}',
    "incremental_graph": '{"multi_hop": true, "shared_pools": true, "split": true}',
    "metis_inspired": '{"multi_hop": true, "shared_pools": true, "split": true}',
    **{
        name: '{"multi_hop": true, "protocols": ["V2", "V3"], "shared_pools": false, "split": true}'
        for name in ("uni_sor_port", "uni_sor_adaptive", "uni_sor_optimized", "uni_sor_fast")
    },
}


def test_direct_split_certified_declares_the_constant_product_ceiling() -> None:
    caps = ALGORITHMS[direct_split_certified.NAME].capabilities
    assert caps == Capabilities(multi_hop=False, split=True, protocols=("constant_product",))
    assert (caps.multi_hop, caps.split, caps.shared_pools) == (
        direct_split.CAPABILITIES.multi_hop,
        direct_split.CAPABILITIES.split,
        direct_split.CAPABILITIES.shared_pools,
    )
    assert caps.to_dict()["protocols"] == ["constant_product"]


@pytest.mark.parametrize("name", R021_ADDITIONS)
def test_new_factories_agree_with_the_identity_table(name: str) -> None:
    declared = protocol_families(ALGORITHMS[name].capabilities.protocols)
    assert declared == (IDENTITIES[name].protocols or PROTOCOLS), name


def test_pre_existing_identities_record_byte_identical_capabilities() -> None:
    for name, recorded in PRE_EXISTING.items():
        assert json.dumps(ALGORITHMS[name].capabilities.to_dict(), sort_keys=True) == recorded
    assert set(ALGORITHMS) == set(PRE_EXISTING) | set(R021_ADDITIONS)


def test_protocol_families_mapping() -> None:
    assert protocol_families(None) == PROTOCOLS
    assert protocol_families(("V2", "V3")) == ("constant_product", "concentrated")
    assert protocol_families(["concentrated"]) == ("concentrated",)
    assert protocol_families(("V4",)) == ("V4",)  # unknown: kept, never widened

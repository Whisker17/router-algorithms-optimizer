"""The `uniswap_v3` prepare source (WHI-1431): Uniswap v3 (Mantle deployment,
Uniswap/v3-core v1.0.0) pools collected at an explicit, finalized block by the shared CL
collector (`snapshot.collectors.concentrated`) with the selection in
`config/prepare/uniswap_v3.yaml` and the verified deployment facts in
`config/protocols.yaml` (`uniswap_v3`)."""

from __future__ import annotations

from snapshot.collectors.base import PrepareRequest
from snapshot.collectors.concentrated import collect_for_source
from snapshot.models import SnapshotBundle

SOURCE_KEY = "uniswap_v3"


def collect(request: PrepareRequest) -> SnapshotBundle:
    return collect_for_source(SOURCE_KEY, request)

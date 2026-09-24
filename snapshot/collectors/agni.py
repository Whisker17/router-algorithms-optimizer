"""The `agni` prepare source (WHI-1429): Agni Finance v3 pools collected at an explicit,
finalized block by the shared CL collector (`snapshot.collectors.concentrated`) with the
selection in `config/prepare/agni.yaml` and the verified deployment facts in
`config/protocols.yaml` (`agni_v3`)."""

from __future__ import annotations

from snapshot.collectors.base import PrepareRequest
from snapshot.collectors.concentrated import collect_for_source
from snapshot.models import SnapshotBundle

SOURCE_KEY = "agni_v3"


def collect(request: PrepareRequest) -> SnapshotBundle:
    return collect_for_source(SOURCE_KEY, request)

"""R023-C/1 contract checks (docs/references/research-023/, WHI-1622).

Machine check of the publication's acceptance criteria: the pinned probe matches its hashes and
stays outside runtime code; the contract carries every required section and the round-4 wording
without pointing at local drafts; every review finding id has exactly one recorded disposition.
"""

from __future__ import annotations

import gzip
import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
R023 = ROOT / "docs" / "references" / "research-023"
PROBE = R023 / "probe"
CONTRACT = (R023 / "contract.md").read_text(encoding="utf-8")
SOURCES = (R023 / "sources.md").read_text(encoding="utf-8")
POLISH_SHA256 = "b508bcc48d520ba56840e8e9bc4274d548c6d0d962fe2607adfc66c7fc9111b3"
FINDING_ID = re.compile(r"\b(R023-(?:0[1-9]|1[0-9])|R[234]-(?:m)?0?[1-9])\b")


def _sums(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return {name: digest for digest, name in (line.split("  ", 1) for line in lines)}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _section(number: int) -> str:
    match = re.search(rf"^## {number}\. .*?(?=^## {number + 1}\. |\Z)", CONTRACT, re.M | re.S)
    assert match, f"section {number} missing"
    return match.group(0)


def test_probe_files_match_their_pins() -> None:
    sums = _sums(PROBE / "SHA256SUMS")
    assert sums["polish4.py.txt"] == POLISH_SHA256 and POLISH_SHA256 in CONTRACT
    for name, digest in sums.items():
        assert _sha((PROBE / name).read_bytes()) == digest, name


def test_arm_results_match_their_uncompressed_pins() -> None:
    sums = _sums(PROBE / "results" / "SHA256SUMS")
    stored = {p.name.removesuffix(".gz") for p in (PROBE / "results").glob("*.json.gz")}
    assert stored == set(sums) and len(stored) == 10
    for name, digest in sums.items():
        assert _sha(gzip.decompress((PROBE / "results" / f"{name}.gz").read_bytes())) == digest


def test_probe_is_archived_text_and_never_imported() -> None:
    assert not list(R023.rglob("*.py")), "probe scripts must stay *.py.txt"
    runtime = ["main.py", "snapshot", "pools", "routing", "benchmark", "report", "tools"]
    for top in runtime:
        for path in [ROOT / top] if top.endswith(".py") else (ROOT / top).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "research-023" not in text and "polish4" not in text, path


@pytest.mark.parametrize("doc", ["contract.md", "sources.md", "probe/README.md"])
def test_normative_documents_do_not_point_at_local_drafts(doc: str) -> None:
    assert "/tmp" not in (R023 / doc).read_text(encoding="utf-8")


def test_contract_has_every_required_section() -> None:
    headings = re.findall(r"^## (\d+)\. ", CONTRACT, re.M)
    assert headings == [str(n) for n in range(1, 14)]
    required = {
        1: ["U1", "M7", "P1", "P2", "2025-10-15", "2025-11-17", 'Reading "100x"'],
        2: ["PRIME-Flow", "incremental_graph_repair", "WHI-1448"],
        3: ["fixed funding topology", "`shared_sequential`", "unsupported_topology"],
        4: ["that consumer takes the remainder", "D = 10⁹", "MAXITER 60", "Nominee.", "R = 2"],
        5: [
            "acyclic",
            "w_orig · (1 − t) ⊕ t",
            "K = 2",
            "work-matched",
            "call-matched",
            "activation/control not reached: E1 truncated",
            "invocations started",
        ],
        6: ["unsupported_topology` → `ok`", "truncated_by", "`timeout`", "gross-only"],
        7: [
            "remainder rule",
            "qualitative conclusions are unchanged",
            "emp-78c1b0-deadde-medium-4",
        ],
        8: [
            *(f"Q{i}" for i in range(1, 6)),
            "no p-values",
            "**Arms.**",
            "**Metrics.**",
            "**Evidence boundaries.**",
            "previously_exposed",
        ],
        9: [*(f"F{i}" for i in range(1, 17)), "Implementation gates"],
        10: ["`reject`", "`inconclusive`", "`keep_experimental`"],
        11: ["Deferred static follow-ups", "Out of scope"],
        13: ["Single block"],
    }
    for number, phrases in required.items():
        body = " ".join(_section(number).split())  # prose is hard-wrapped
        missing = [p for p in phrases if p not in body]
        assert missing == [], (number, missing)


def test_every_review_finding_has_one_disposition() -> None:
    raised: set[str] = set()
    for report in sorted((R023 / "reviews").glob("round-*.md")):
        raised |= set(FINDING_ID.findall(report.read_text(encoding="utf-8")))
    rows = re.findall(r"^\| (R\S+) \| ([^|]+) \| ([^|]+) \|", _section(12), re.M)
    ids = [row[0] for row in rows]
    assert len(ids) == len(set(ids)) == 43
    assert set(ids) == raised
    assert all(finding.strip() and disposition.strip() for _, finding, disposition in rows)


def test_contract_sources_are_registered_with_pins() -> None:
    ids = [i for i in re.findall(r"^\| (\w+) \| ", _section(1), re.M) if i != "ID"]
    assert ids == ["U1", "M7", "MU", "CL5", "JS", "QE", "MC", "P1", "P2", "P3", "B"]
    for source in ids:
        assert re.search(rf"^\| {source} \|", SOURCES, re.M), source
    pins = re.findall(r"^[0-9a-f]{64}  (raw|text) ", SOURCES, re.M)
    assert len(pins) >= 2 * 10


def test_relative_links_resolve() -> None:
    for doc in ("contract.md", "sources.md", "probe/README.md", "reviews/README.md"):
        base = (R023 / doc).parent
        text = (R023 / doc).read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)#:]+)\)", text):
            assert (base / target).exists(), (doc, target)

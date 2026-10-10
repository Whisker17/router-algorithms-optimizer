"""Strict mypy for the frozen 0.2.3 driver `tools/research_023/campaign.py` (WHI-1745, R1-F2).

R024-C/1 §10.4 requires strict mypy over `tools/research_023/*.py`. The driver cannot join the
`[tool.mypy] files` list: mypy maps it and `tools/research_021/campaign.py` (both outside any
package, both importing siblings by bare name) to the same module `campaign`, and renaming or
editing either frozen driver is out of bounds. This test is that file's separate strict invocation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_r023_driver_passes_strict_mypy() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(REPO / "pyproject.toml"),
            "--strict",
            "--cache-dir",
            os.devnull,
            "tools/research_023/campaign.py",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

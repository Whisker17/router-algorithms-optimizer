"""WHI-1745 (release review R1-F2): strict mypy covers `tools/research_023/campaign.py`.

R024-C/1 §10.4 requires strict mypy over `tools/research_023/*.py`. The driver cannot sit in the
`[tool.mypy] files` list: none of `tools/` is a package, so it and `tools/research_021/campaign.py`
are both the top-level module `campaign` and one mypy run refuses them ("Duplicate module named
'campaign'"). Both frozen drivers keep their names, so this required test runs a second strict
mypy invocation on the R023 driver alone, with the project's `[tool.mypy]` settings.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DRIVER = "tools/research_023/campaign.py"


def test_r023_campaign_passes_strict_mypy(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            "pyproject.toml",
            "--cache-dir",
            str(tmp_path),
            DRIVER,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no issues found in 1 source file" in result.stdout


def test_r023_tools_are_all_covered() -> None:
    config = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["mypy"]
    assert config["strict"] is True
    files = set(config["files"])
    assert "tools/research_024/*.py" in files
    assert DRIVER not in files  # the collision above; checked by the test instead
    for path in sorted((REPO / "tools" / "research_023").glob("*.py")):
        relative = path.relative_to(REPO).as_posix()
        assert relative in files | {DRIVER}, f"{relative} is not type-checked"

"""Smoke test: the skeleton entrypoint runs. Replace with real tests as modules land."""

import main


def test_entrypoint_dry_run() -> None:
    assert main.main(["--dry-run"]) == 0

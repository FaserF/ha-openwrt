"""Behavior tests for the private-data branch scanner."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _git(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


def test_manual_scan_uses_default_branch_merge_base(tmp_path: Path) -> None:
    """Scan every feature commit during manual workflow runs."""
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "init", "--initial-branch=main", str(repository))
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.invalid")

    (repository / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    _git(repository, "add", "baseline.txt")
    _git(repository, "commit", "-m", "baseline")
    _git(repository, "remote", "add", "origin", str(origin))
    _git(repository, "push", "-u", "origin", "main")
    _git(repository, "switch", "-c", "feature")

    private_address = "192" + ".168.42.9"
    (repository / "installation.txt").write_text(
        f"host={private_address}\n", encoding="utf-8"
    )
    _git(repository, "add", "installation.txt")
    _git(repository, "commit", "-m", "installation data")
    (repository / "safe.txt").write_text("safe follow-up\n", encoding="utf-8")
    _git(repository, "add", "safe.txt")
    _git(repository, "commit", "-m", "safe follow-up")

    scanner = Path(__file__).parents[1] / ".github/scripts/check_private_data.py"
    result = subprocess.run(
        [
            sys.executable,
            str(scanner),
            "--github-event-name",
            "workflow_dispatch",
            "--github-before",
            "",
            "--default-branch",
            "main",
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Private-data check failed with 1 finding(s)." in result.stdout

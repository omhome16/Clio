"""Shared fixtures for Clio tests."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def local_repo(tmp_path: Path) -> Path:
    """A tiny real git repo (offline, deterministic) ready to be cloned."""
    repo = tmp_path / "source-repo"
    repo.mkdir()
    (repo / "hello.txt").write_text("hello clio\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "-q"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "add", "."], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        [
            "git", "-c", "user.email=test@clio.local", "-c", "user.name=Clio Test",
            "commit", "-q", "-m", "init",
        ],
        cwd=repo, check=True, capture_output=True,
    )
    return repo

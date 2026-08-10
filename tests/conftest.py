"""Shared fixtures for Clio tests."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def write_tree(tmp_path: Path):
    """Write a dict of relative-path -> content files under tmp_path/repo."""
    def _write(files: dict[str, str]) -> Path:
        root = tmp_path / "repo"
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return root
    return _write


@pytest.fixture
def local_repo(tmp_path: Path) -> Path:
    """A tiny real git repo (offline, deterministic) ready to be cloned."""
    repo = tmp_path / "source-repo"
    repo.mkdir()
    (repo / "hello.txt").write_text("hello clio\n", encoding="utf-8")
    (repo / "app").mkdir()
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "service.py").write_text(
        "def greet(name: str) -> str:\n    return f'hello {name}'\n",
        encoding="utf-8",
    )
    (repo / "app" / "main.py").write_text(
        "from app.service import greet\n\n\ndef run() -> str:\n    return greet('clio')\n",
        encoding="utf-8",
    )
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

"""Shared fixtures for Clio tests."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from clio.graph import build_repo_graph
from clio.store import GraphStore


@pytest.fixture(autouse=True)
def _isolate_dotenv(monkeypatch, tmp_path):
    """Tests must never read the developer's real .env from the repo root."""
    monkeypatch.setenv("CLIO_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("CLIO_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("CLIO_CHEAP_MODEL", raising=False)
    monkeypatch.delenv("CLIO_FRONTIER_MODEL", raising=False)
    monkeypatch.delenv("CLIO_RPM", raising=False)
    monkeypatch.delenv("CLIO_MAX_RETRIES", raising=False)
    monkeypatch.setenv("CLIO_RATE_LIMIT", "0")


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


@pytest.fixture
def seed_job(write_tree):
    """Seed a persisted job: workspace clone, job record, graph.db + report.json."""
    def _seed(root: Path, job_id: str, created_at: str, files: dict[str, str]) -> None:
        repo = write_tree(files)
        graph = build_repo_graph(repo)
        workspace = root / job_id
        if workspace != repo:
            shutil.copytree(repo, workspace)
        jobs = root / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        GraphStore(jobs / f"{job_id}.graph.db").save(graph)
        job = {
            "job_id": job_id, "url": "https://github.com/x/y.git",
            "status": "PERSISTED", "workspace": str(workspace),
            "commit_sha": "", "created_at": created_at,
        }
        (jobs / f"{job_id}.json").write_text(json.dumps(job), encoding="utf-8")
        report = {
            "job_id": job_id, "repo_url": "https://github.com/x/y.git",
            "summary": "merged", "created_at": created_at,
        }
        (jobs / f"{job_id}.report.json").write_text(json.dumps(report), encoding="utf-8")
    return _seed

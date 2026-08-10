# M0 — Sandbox, Clone & Tree Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Clio's foundation milestone: a sandboxed workspace manager, a safe git-clone operation with size guards, a repository tree/statistics tool, and a persisted job record — all tested, no network required in CI (tests use local `file://` repos).

**Architecture:** Three independent stdlib-only modules (`sandbox`, `tree`, `clone`) plus a `job` record store, orchestrated around a `Limits` config dataclass read from environment, kernel-friendly on Windows. Each module has one responsibility and communicates through dataclasses. TDD throughout: every task starts with failing tests.

**Tech Stack:** Python 3.11+, stdlib only (subprocess, pathlib, json, urllib.parse, dataclasses, shutil, datetime), pytest. No third-party runtime deps in M0 — keeps the sandbox surface minimal and the learning surface maximal.

## Global Constraints

- All code lives under `src/clio/` and `tests/` in the repo root; never touch files outside this repo.
- Maximum repo size: 50 MB default (env `CLIO_MAX_REPO_SIZE_MB`).
- Maximum files listed: 20,000 default (env `CLIO_MAX_FILES`).
- Clone timeout: 120 s default (env `CLIO_CLONE_TIMEOUT_S`).
- Workspace root: `./sandbox` default (env `CLIO_WORKSPACE_ROOT`).
- Only `https://github.com/*` URLs and `file://` URLs (tests) are clonable.
- Excluded dirs (never walked): `.git`, `node_modules`, `venv`, `.venv`, `__pycache__`, `dist`, `build`, `.idea`, `.vscode`.
- Windows is a first-class platform: no POSIX-only APIs, no symlink assumptions.
- `sandbox/`, `data/`, `*.db` must stay gitignored (already in `.gitignore`).
- Python requires >= 3.11.

---

### Task 1: Project scaffolding + config limits

**Files:**
- Create: `pyproject.toml`
- Create: `src/clio/__init__.py`
- Create: `src/clio/config.py`
- Create: `tests/test_config.py`
- Create: `tests/__init__.py`
- Modify: `.gitignore` (add pytest caches)

**Interfaces:**
- Produces: `clio.config.Limits` dataclass — `max_repo_size: int` (bytes), `max_files: int`, `clone_timeout_s: int`, `workspace_root: Path`, `exclude_dirs: tuple[str, ...]`, `allowed_hosts: tuple[str, ...]`; plus `clio.config.get_limits() -> Limits` reading env vars fresh on every call (test-friendly).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
from clio.config import Limits, get_limits


def test_default_limits():
    limits = get_limits()
    assert limits.max_repo_size == 50 * 1024 * 1024
    assert limits.max_files == 20_000
    assert limits.clone_timeout_s == 120
    assert limits.workspace_root.name == "sandbox"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("CLIO_MAX_REPO_SIZE_MB", "3")
    monkeypatch.setenv("CLIO_MAX_FILES", "10")
    monkeypatch.setenv("CLIO_CLONE_TIMEOUT_S", "7")
    limits = get_limits()
    assert limits.max_repo_size == 3 * 1024 * 1024
    assert limits.max_files == 10
    assert limits.clone_timeout_s == 7


def test_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CLIO_MAX_FILES", "not-a-number")
    assert get_limits().max_files == 20_000


def test_exclude_dirs_defaults():
    limits = get_limits()
    assert ".git" in limits.exclude_dirs
    assert "node_modules" in limits.exclude_dirs
```

- [ ] **Step 2: Run tests, verify they fail (module/function not found)**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clio'`

- [ ] **Step 3: Write the scaffolding and implementation**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "clio"
version = "0.1.0"
description = "A repo analyzer with a visible nervous system"
requires-python = ">=3.11"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

```python
# src/clio/__init__.py
"""Clio — a repo analyzer with a visible nervous system."""

__version__ = "0.1.0"
```

```python
# src/clio/config.py
"""Runtime limits and defaults for Clio, overridable via environment."""
import os
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_EXCLUDE_DIRS = (
    ".git", "node_modules", "venv", ".venv", "__pycache__",
    "dist", "build", ".idea", ".vscode",
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Limits:
    max_repo_size: int = 50 * 1024 * 1024
    max_files: int = 20_000
    clone_timeout_s: int = 120
    workspace_root: Path = field(default_factory=lambda: Path("sandbox"))
    exclude_dirs: tuple[str, ...] = _DEFAULT_EXCLUDE_DIRS
    allowed_hosts: tuple[str, ...] = ("github.com",)


def get_limits() -> Limits:
    return Limits(
        max_repo_size=_env_int("CLIO_MAX_REPO_SIZE_MB", 50) * 1024 * 1024,
        max_files=_env_int("CLIO_MAX_FILES", 20_000),
        clone_timeout_s=_env_int("CLIO_CLONE_TIMEOUT_S", 120),
        workspace_root=Path(os.environ.get("CLIO_WORKSPACE_ROOT", "sandbox")),
    )
```

```python
# tests/__init__.py
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: 4 PASSED

- [ ] **Step 5: Append pytest artifacts to .gitignore and commit**

```bash
git add .gitignore pyproject.toml src/clio/__init__.py src/clio/config.py tests/test_config.py tests/__init__.py
git commit -m "feat: scaffold clio package with configurable limits"
```

---

### Task 2: Sandbox — workspaces and path containment

**Files:**
- Create: `src/clio/sandbox.py`
- Create: `tests/conftest.py`
- Create: `tests/test_sandbox.py`

**Interfaces:**
- Consumes: `get_limits()` from Task 1.
- Produces:
  - `clio.sandbox.PathViolation(ValueError)` — raised for any path escaping the sandbox root.
  - `clio.sandbox.Sandbox` class:
    - `Sandbox(root: Path, limits: Limits | None = None)`
    - `Sandbox.create_workspace(job_id: str) -> Path` — creates `<root>/<job_id>/` (mkdir parents), returns absolute resolved path.
    - `Sandbox.workspace(job_id: str) -> Path` — returns `<root>/<job_id>` without creating.
    - `Sandbox.ensure_contained(path: Path | str) -> Path` — resolves and asserts the path stays under root; raises `PathViolation` otherwise.
    - `Sandbox.jobs_glob() -> list[str]` — sorted existing job ids under root (are directories).
    - `Sandbox.cleanup(job_id: str) -> None` — removes the job workspace if present, silently ignores missing.

- [ ] **Step 1: Write the failing tests**

```python
# tests/conftest.py
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
```

```python
# tests/test_sandbox.py
from pathlib import Path

import pytest

from clio.sandbox import PathViolation, Sandbox


def test_create_workspace_creates_dir(tmp_path):
    sb = Sandbox(root=tmp_path)
    ws = sb.create_workspace("job-1")
    assert ws == tmp_path / "job-1"
    assert ws.is_dir()


def test_workspace_does_not_create(tmp_path):
    sb = Sandbox(root=tmp_path)
    ws = sb.workspace("job-2")
    assert not ws.exists()


def test_ensure_contained_accepts_inside(tmp_path):
    sb = Sandbox(root=tmp_path)
    ws = sb.create_workspace("job-3")
    (ws / "nested").mkdir()
    inside = ws / "nested" / "file.txt"
    assert sb.ensure_contained(inside) == inside.resolve()


def test_ensure_contained_rejects_outside(tmp_path):
    sb = Sandbox(root=tmp_path)
    outside = tmp_path / ".." / "evil.txt"
    with pytest.raises(PathViolation):
        sb.ensure_contained(outside)


def test_ensure_contained_rejects_dotdot_traversal(tmp_path):
    sb = Sandbox(root=tmp_path)
    sb.create_workspace("job-4")
    traversal = tmp_path / "job-4" / ".." / ".." / "escape.txt"
    with pytest.raises(PathViolation):
        sb.ensure_contained(traversal)


def test_jobs_glob_lists_only_directories(tmp_path):
    sb = Sandbox(root=tmp_path)
    sb.create_workspace("b-job")
    sb.create_workspace("a-job")
    (tmp_path / "not-a-job.txt").write_text("x")
    assert sb.jobs_glob() == ["a-job", "b-job"]


def test_cleanup_removes_workspace(tmp_path):
    sb = Sandbox(root=tmp_path)
    sb.create_workspace("job-5")
    assert sb.workspace("job-5").exists()
    sb.cleanup("job-5")
    assert not sb.workspace("job-5").exists()


def test_cleanup_missing_is_silent(tmp_path):
    sb = Sandbox(root=tmp_path)
    sb.cleanup("ghost-job")
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_sandbox.py -v`
Expected: FAIL — `cannot import name 'PathViolation' from 'clio.sandbox'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/clio/sandbox.py
"""Sandboxed job workspaces with path-containment enforcement."""
import shutil
from pathlib import Path

from clio.config import Limits, get_limits


class PathViolation(ValueError):
    """A path escaped, or tried to escape, the sandbox root."""


class Sandbox:
    """Owns a root directory; every job workspace lives directly under it."""

    def __init__(self, root: Path | str, limits: Limits | None = None):
        self.root = Path(root).resolve()
        self.limits = limits or get_limits()

    def create_workspace(self, job_id: str) -> Path:
        ws = self.workspace(job_id)
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def workspace(self, job_id: str) -> Path:
        return self.root / job_id

    def ensure_contained(self, path: Path | str) -> Path:
        resolved = Path(path).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise PathViolation(
                f"path {resolved} escapes sandbox root {self.root}"
            ) from exc
        return resolved

    def jobs_glob(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def cleanup(self, job_id: str) -> None:
        shutil.rmtree(self.workspace(job_id), ignore_errors=True)
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `python -m pytest tests/test_sandbox.py -v`
Expected: 8 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/clio/sandbox.py tests/conftest.py tests/test_sandbox.py
git commit -m "feat: sandbox with path-containment enforcement"
```

---

### Task 3: Tree listing + workspace statistics

**Files:**
- Create: `src/clio/tree.py`
- Create: `tests/test_tree.py`

**Interfaces:**
- Consumes: `get_limits()` from Task 1.
- Produces:
  - `clio.tree.TreeLimitError(RuntimeError)` — raised when file count exceeds `max_files` or depth exceeds `max_depth`.
  - `clio.tree.list_tree(root: Path, *, exclude_dirs: tuple[str, ...] | None = None, max_files: int | None = None, max_depth: int | None = None) -> list[Path]` — sorted relative file paths (files only), never descends into excluded dirs, raises `TreeLimitError` if file count exceeds the cap.
  - `clio.tree.WorkspaceStats` dataclass — `file_count: int`, `size_bytes: int`, `extensions: dict[str, int]` (lowercased, leading dot kept, `""` key for extensionless).
  - `clio.tree.workspace_stats(root: Path, **kwargs) -> WorkspaceStats` — single walk that computes all three.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tree.py
from pathlib import Path

import pytest

from clio.tree import TreeLimitError, WorkspaceStats, list_tree, workspace_stats


def _make_fixture(root: Path) -> Path:
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('a')\n" * 20)
    (root / "src" / "core").mkdir()
    (root / "src" / "core" / "engine.py").write_text("x = 1\n")
    (root / "README.md").write_text("# repo\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("let a = 1;\n")
    (root / "noext").write_text("raw\n")
    (root / "DATA.TXT").write_text("caps\n")
    return root


def test_list_tree_returns_relative_paths_sorted(tmp_path):
    root = _make_fixture(tmp_path / "proj")
    paths = list_tree(root)
    assert ".git" not in [p.parts[0] for p in paths]
    assert "node_modules" not in [p.parts[0] for p in paths]
    assert paths == sorted(paths)


def test_list_tree_excludes_configured_dirs(tmp_path):
    root = _make_fixture(tmp_path / "proj")
    paths = list_tree(root)
    names = {p.as_posix() for p in paths}
    assert "src/core/engine.py" in names
    assert "README.md" in names
    assert all(".git" not in p.parts and "node_modules" not in p.parts for p in paths)


def test_list_tree_respects_max_files(tmp_path):
    root = _make_fixture(tmp_path / "proj")
    with pytest.raises(TreeLimitError):
        list_tree(root, max_files=1)


def test_list_tree_respects_max_depth(tmp_path):
    root = _make_fixture(tmp_path / "proj")
    with pytest.raises(TreeLimitError):
        list_tree(root, max_depth=0)


def test_workspace_stats_counts_and_sizes(tmp_path):
    root = _make_fixture(tmp_path / "proj")
    stats = workspace_stats(root)
    assert stats.file_count == 5  # hello? no: app.py, engine.py, README, noext, DATA.TXT
    assert stats.size_bytes > 0
    assert stats.extensions[".py"] == 2
    assert stats.extensions[""] == 1
    assert stats.extensions[".txt"] == 1
    assert stats.extensions[".md"] == 1
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_tree.py -v`
Expected: FAIL — `cannot import name 'list_tree' from 'clio.tree'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/clio/tree.py
"""Repository tree listing and workspace statistics."""
from dataclasses import dataclass
from pathlib import Path

from clio.config import Limits, get_limits


class TreeLimitError(RuntimeError):
    """A tree walk exceeded its configured caps."""


def list_tree(
    root: Path,
    *,
    exclude_dirs: tuple[str, ...] | None = None,
    max_files: int | None = None,
    max_depth: int | None = None,
) -> list[Path]:
    limits = get_limits()
    excluded = exclude_dirs if exclude_dirs is not None else limits.exclude_dirs
    cap = max_files if max_files is not None else limits.max_files

    results: list[Path] = []

    def walk(dirpath: Path, depth: int) -> None:
        if max_depth is not None and depth > max_depth:
            raise TreeLimitError(f"max depth {max_depth} exceeded at {dirpath}")
        if len(results) > cap:
            raise TreeLimitError(f"max files {cap} exceeded")
        for child in dirpath.iterdir():
            if child.is_dir():
                if child.name in excluded:
                    continue
                walk(child, depth + 1)
            else:
                results.append(child)

    walk(root, depth=0)
    return sorted(p.relative_to(root) for p in results)


@dataclass(frozen=True)
class WorkspaceStats:
    file_count: int
    size_bytes: int
    extensions: dict[str, int]


def workspace_stats(
    root: Path,
    *,
    exclude_dirs: tuple[str, ...] | None = None,
    max_files: int | None = None,
) -> WorkspaceStats:
    limits = get_limits()
    excluded = exclude_dirs if exclude_dirs is not None else limits.exclude_dirs
    cap = max_files if max_files is not None else limits.max_files

    file_count = 0
    size_bytes = 0
    extensions: dict[str, int] = {}

    def walk(dirpath: Path) -> None:
        nonlocal file_count, size_bytes
        for child in dirpath.iterdir():
            if child.is_dir():
                if child.name in excluded:
                    continue
                walk(child)
            else:
                file_count += 1
                size_bytes += child.stat().st_size
                ext = child.suffix.lower()
                extensions[ext] = extensions.get(ext, 0) + 1
                if file_count > cap:
                    raise TreeLimitError(f"max files {cap} exceeded")

    walk(root)
    return WorkspaceStats(file_count=file_count, size_bytes=size_bytes, extensions=extensions)
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `python -m pytest tests/test_tree.py -v`
Expected: 6 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/clio/tree.py tests/test_tree.py
git commit -m "feat: tree listing with exclusion, depth and file caps"
```

---

### Task 4: Safe git clone with URL validation and size guard

**Files:**
- Create: `src/clio/clone.py`
- Create: `tests/test_clone.py`

**Interfaces:**
- Consumes: `Sandbox` (Task 2), `workspace_stats` (Task 3), `get_limits()` (Task 1).
- Produces:
  - `clio.clone.CloneError(RuntimeError)` with `.stderr: str` attribute — raised for invalid URLs, git failures, timeouts.
  - `clio.clone.RepoTooLargeError(CloneError)` — raised when the cloned repo exceeds `limits.max_repo_size`; the partial clone is removed.
  - `clio.clone.CloneResult` dataclass — `repo_path: Path`, `commit_sha: str` (up to 12 chars or `""` if unavailable).
  - `clio.clone.validate_repo_url(url: str) -> None` — raises `CloneError` unless scheme is `https` (host in `limits.allowed_hosts`) or `file`.
  - `clio.clone.clone_repo(url: str, sandbox: Sandbox, job_id: str, *, depth: int = 1, timeout: int | None = None) -> CloneResult` — validates, creates workspace, `git clone --depth <depth>`, runs the size guard, records commit SHA, and cleans up on any failure.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_clone.py
from pathlib import Path

import pytest

from clio.clone import CloneError, CloneResult, RepoTooLargeError, clone_repo, validate_repo_url
from clio.config import Limits
from clio.sandbox import Sandbox


@pytest.mark.parametrize("bad", ["", "ftp://x/y", "javascript:alert(1)", "not-a-url", "https://evil.com/x.git"])
def test_validate_repo_url_rejects(bad):
    with pytest.raises(CloneError):
        validate_repo_url(bad)


@pytest.mark.parametrize("good", ["https://github.com/omhome16/Clio.git", "file:///tmp/x"])
def test_validate_repo_url_accepts(good):
    validate_repo_url(good)


def test_clone_repo_success(tmp_path, local_repo):
    sandbox = Sandbox(root=tmp_path / "sandbox")
    result = clone_repo(local_repo.as_uri(), sandbox, "job-1")
    assert result.repo_path.is_dir()
    assert (result.repo_path / "hello.txt").exists()
    assert len(result.commit_sha) == 12


def test_clone_repo_size_guard(tmp_path, local_repo):
    sandbox = Sandbox(root=tmp_path / "sandbox")
    small_limits = Limits(max_repo_size=1, max_files=20_000, clone_timeout_s=120,
                          workspace_root=Path("sandbox"))
    with pytest.raises(RepoTooLargeError):
        clone_repo(local_repo.as_uri(), sandbox, "job-2", _limits=small_limits)


def test_clone_repo_bad_source_cleans_workspace(tmp_path):
    sandbox = Sandbox(root=tmp_path / "sandbox")
    with pytest.raises(CloneError):
        clone_repo("https://github.com/omhome16/does-not-exist-xyz.git", sandbox, "job-3")
    assert not sandbox.workspace("job-3").exists()


def test_clone_repo_invalid_url_raises(tmp_path):
    sandbox = Sandbox(root=tmp_path / "sandbox")
    with pytest.raises(CloneError):
        clone_repo("ftp://x/y", sandbox, "job-4")
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_clone.py -v`
Expected: FAIL — `cannot import name 'clone_repo' from 'clio.clone'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/clio/clone.py
"""Safe git cloning: URL validation, timeouts, and a repo size guard."""
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from clio.config import Limits, get_limits
from clio.sandbox import Sandbox
from clio.tree import workspace_stats


class CloneError(RuntimeError):
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


class RepoTooLargeError(CloneError):
    pass


@dataclass(frozen=True)
class CloneResult:
    repo_path: Path
    commit_sha: str


def validate_repo_url(url: str, limits: Limits | None = None) -> None:
    limits = limits or get_limits()
    if not url:
        raise CloneError("empty repo URL")
    parsed = urlparse(url)
    if parsed.scheme == "https":
        host = (parsed.hostname or "").lower()
        if host not in limits.allowed_hosts:
            raise CloneError(f"https host '{host}' not allowed ({limits.allowed_hosts})")
    elif parsed.scheme != "file":
        raise CloneError(f"unsupported URL scheme '{parsed.scheme}'")


def clone_repo(
    url: str,
    sandbox: Sandbox,
    job_id: str,
    *,
    depth: int = 1,
    timeout: int | None = None,
    _limits: Limits | None = None,
) -> CloneResult:
    limits = _limits or get_limits()
    validate_repo_url(url, limits)
    timeout_s = timeout if timeout is not None else limits.clone_timeout_s

    dest = sandbox.create_workspace(job_id)
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", str(depth), "--quiet", url, str(dest)],
            capture_output=True, text=True, timeout=timeout_s,
        )
        if proc.returncode != 0:
            raise CloneError(
                f"git clone failed for '{url}'", stderr=proc.stderr.strip()
            )
        stats = workspace_stats(dest, max_files=limits.max_files)
        if stats.size_bytes > limits.max_repo_size:
            raise RepoTooLargeError(
                f"repo is {stats.size_bytes} bytes "
                f"(limit {limits.max_repo_size}) after cloning"
            )
        sha = _head_sha(dest)
        return CloneResult(repo_path=dest, commit_sha=sha)
    except subprocess.TimeoutExpired as exc:
        raise CloneError(
            f"git clone timed out after {timeout_s}s for '{url}'"
        ) from exc
    except (CloneError, Exception):
        sandbox.cleanup(job_id)
        raise


def _head_sha(repo_path: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        return ""
```

- [ ] **Step 4: Run tests, verify they pass (network tests may 2-10 s)**

Run: `python -m pytest tests/test_clone.py -v`
Expected: 8 PASSED (note: `test_clone_repo_bad_source_cleans_workspace` needs internet; if offline it FAILS — that's expected and documented in the caveat)

- [ ] **Step 5: Commit**

```bash
git add src/clio/clone.py tests/test_clone.py
git commit -m "feat: sandboxed git clone with url validation and size guard"
```

---

### Task 5: Job record persistence

**Files:**
- Create: `src/clio/job.py`
- Create: `tests/test_job.py`

**Interfaces:**
- Consumes: `CloneResult` (Task 4).
- Produces:
  - `clio.job.JOB_STATUSES` — tuple of allowed statuses: `("QUEUED", "CLONING", "INDEXING", "ANALYZING", "SYNTHESIZING", "GRAPHING", "PERSISTED", "FAILED")`.
  - `clio.job.AnalysisJob` dataclass — `job_id: str`, `url: str`, `status: str`, `workspace: Path | None`, `commit_sha: str`, `created_at: str` (ISO-8601), with `to_dict() -> dict` and `from_dict(data: dict) -> "AnalysisJob"`.
  - `clio.job.new_job(url: str, *, job_id: str | None = None, now: str | None = None) -> AnalysisJob` — generates `clio-<8 hex>` job ids, status `QUEUED`.
  - `clio.job.jobs_dir(root: Path) -> Path` — `<root>/jobs`.
  - `clio.job.save_job(job: AnalysisJob, root: Path) -> None` — writes `<root>/jobs/<job_id>.json`.
  - `clio.job.load_job(job_id: str, root: Path) -> AnalysisJob | None` — returns None for missing/invalid JSON.
  - `clio.job.update_status(job: AnalysisJob, status: str, root: Path) -> AnalysisJob` — validates the status, reassigns, saves, returns the same object.
  - `clio.job.record_clone(job: AnalysisJob, result: CloneResult, root: Path) -> AnalysisJob` — sets workspace + commit_sha, status `INDEXING`, saves.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_job.py
import json
from pathlib import Path

import pytest

from clio.job import (
    JOB_STATUSES, AnalysisJob, jobs_dir, load_job, new_job,
    record_clone, save_job, update_status,
)
from clio.clone import CloneResult


def test_job_defaults(tmp_path):
    job = new_job("https://github.com/omhome16/Clio.git", now="2026-08-10T00:00:00")
    assert job.status == "QUEUED"
    assert job.job_id.startswith("clio-")
    assert len(job.job_id) == len("clio-") + 8
    assert job.workspace is None


def test_save_and_load_roundtrip(tmp_path):
    job = new_job("https://github.com/omhome16/Clio.git")
    save_job(job, tmp_path)
    loaded = load_job(job.job_id, tmp_path)
    assert loaded == job


def test_load_missing_returns_none(tmp_path):
    assert load_job("clio-00000000", tmp_path) is None


def test_load_corrupt_json_returns_none(tmp_path):
    jd = jobs_dir(tmp_path)
    jd.mkdir(parents=True)
    (jd / "clio-deadbeef.json").write_text("{not json")
    assert load_job("clio-deadbeef", tmp_path) is None


def test_update_status_validates(tmp_path):
    job = new_job("https://github.com/omhome16/Clio.git")
    with pytest.raises(ValueError):
        update_status(job, "BOGUS", tmp_path)


def test_update_status_persists(tmp_path):
    job = new_job("https://github.com/omhome16/Clio.git")
    update_status(job, "CLONING", tmp_path)
    assert load_job(job.job_id, tmp_path).status == "CLONING"


def test_record_clone_sets_workspace_and_sha(tmp_path):
    job = new_job("https://github.com/omhome16/Clio.git")
    result = CloneResult(repo_path=Path("x"), commit_sha="abc123abc123")
    record_clone(job, result, tmp_path)
    assert job.status == "INDEXING"
    assert job.workspace == Path("x")
    assert job.commit_sha == "abc123abc123"
    assert load_job(job.job_id, tmp_path).workspace == Path("x")


def test_statuses_are_stable():
    assert JOB_STATUSES == (
        "QUEUED", "CLONING", "INDEXING", "ANALYZING",
        "SYNTHESIZING", "GRAPHING", "PERSISTED", "FAILED",
    )
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_job.py -v`
Expected: FAIL — `cannot import name 'AnalysisJob' from 'clio.job'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/clio/job.py
"""Persistent job records: the checkpointing backbone for later phases."""
import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from clio.clone import CloneResult

JOB_STATUSES = (
    "QUEUED", "CLONING", "INDEXING", "ANALYZING",
    "SYNTHESIZING", "GRAPHING", "PERSISTED", "FAILED",
)


@dataclass
class AnalysisJob:
    job_id: str
    url: str
    status: str = "QUEUED"
    workspace: Path | None = None
    commit_sha: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.workspace is not None:
            data["workspace"] = str(self.workspace)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AnalysisJob":
        ws = Path(data["workspace"]) if data.get("workspace") else None
        return cls(
            job_id=data["job_id"],
            url=data["url"],
            status=data.get("status", "QUEUED"),
            workspace=ws,
            commit_sha=data.get("commit_sha", ""),
            created_at=data.get("created_at", ""),
        )


def new_job(url: str, *, job_id: str | None = None, now: str | None = None) -> AnalysisJob:
    return AnalysisJob(
        job_id=job_id or f"clio-{secrets.token_hex(4)}",
        url=url,
        status="QUEUED",
        created_at=now or datetime.now(UTC).isoformat(),
    )


def jobs_dir(root: Path) -> Path:
    return Path(root) / "jobs"


def save_job(job: AnalysisJob, root: Path) -> None:
    jd = jobs_dir(root)
    jd.mkdir(parents=True, exist_ok=True)
    (jd / f"{job.job_id}.json").write_text(
        json.dumps(job.to_dict(), indent=2), encoding="utf-8"
    )


def load_job(job_id: str, root: Path) -> AnalysisJob | None:
    path = jobs_dir(root) / f"{job_id}.json"
    if not path.is_file():
        return None
    try:
        return AnalysisJob.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def update_status(job: AnalysisJob, status: str, root: Path) -> AnalysisJob:
    if status not in JOB_STATUSES:
        raise ValueError(f"unknown job status '{status}'")
    job.status = status
    save_job(job, root)
    return job


def record_clone(job: AnalysisJob, result: CloneResult, root: Path) -> AnalysisJob:
    job.workspace = result.repo_path
    job.commit_sha = result.commit_sha
    job.status = "INDEXING"
    save_job(job, root)
    return job
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `python -m pytest tests/test_job.py -v`
Expected: 8 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/clio/job.py tests/test_job.py
git commit -m "feat: persistent job records with status machine"
```

---

## Full-suite verification

- [ ] Run the whole suite: `python -m pytest -v` — expected: 37 PASSED
- [ ] Smoke demo (manual): from the repo root run a one-liner clone+stats:

```bash
python -c "from pathlib import Path; from clio.sandbox import Sandbox; from clio.clone import clone_repo; from clio.tree import workspace_stats; r = clone_repo('https://github.com/omhome16/Clio.git', Sandbox(Path('sandbox')), 'demo-1'); print(workspace_stats(r.repo_path))"
```

Expected: a `WorkspaceStats` line printed, `sandbox/demo-1/` contains a clone of this repo, and a short `commit_sha`.

## Self-review notes

- Coverage vs spec: all M0 requirements mapped (sandbox containment, clone safety, tree tools, size guard, job persistence).
- Caution: `tests/test_clone.py::test_clone_repo_bad_source_cleans_workspace` hits the network; documents the failure mode when offline — it intentionally exercises error-path cleanup.
- Gotcha: `clone_repo` swallows arbitrary exceptions during cleanup and re-raises; do not silenanything important.
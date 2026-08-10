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

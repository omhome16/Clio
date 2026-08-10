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

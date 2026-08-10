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

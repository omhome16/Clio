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


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw is not None else default
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
    max_tool_output_chars: int = 12_000
    max_file_read_chars: int = 8_000
    max_agent_steps: int = 10
    subagent_max_context_chars: int = 16_000
    max_concurrency: int = 4
    task_max_retries: int = 2
    task_backoff_s: float = 0.5
    cheap_model: str = "gemini-2.0-flash"
    frontier_model: str = "gemini-2.5-pro"


def get_limits() -> Limits:
    return Limits(
        max_repo_size=_env_int("CLIO_MAX_REPO_SIZE_MB", 50) * 1024 * 1024,
        max_files=_env_int("CLIO_MAX_FILES", 20_000),
        clone_timeout_s=_env_int("CLIO_CLONE_TIMEOUT_S", 120),
        workspace_root=Path(os.environ.get("CLIO_WORKSPACE_ROOT", "sandbox")),
        max_tool_output_chars=_env_int("CLIO_MAX_TOOL_OUTPUT_CHARS", 12_000),
        max_file_read_chars=_env_int("CLIO_MAX_FILE_READ_CHARS", 8_000),
        max_agent_steps=_env_int("CLIO_MAX_AGENT_STEPS", 10),
        subagent_max_context_chars=_env_int("CLIO_SUBAGENT_MAX_CONTEXT_CHARS", 16_000),
        max_concurrency=_env_int("CLIO_MAX_CONCURRENCY", 4),
        task_max_retries=_env_int("CLIO_TASK_MAX_RETRIES", 2),
        task_backoff_s=_env_float("CLIO_TASK_BACKOFF_S", 0.5),
        cheap_model=os.environ.get("CLIO_CHEAP_MODEL", "gemini-2.0-flash"),
        frontier_model=os.environ.get("CLIO_FRONTIER_MODEL", "gemini-2.5-pro"),
    )

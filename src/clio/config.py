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
    allowed_hosts: tuple[str, ...] = ()  # empty = any https host allowed
    max_tool_output_chars: int = 12_000
    max_file_read_chars: int = 8_000
    max_agent_steps: int = 10
    subagent_max_context_chars: int = 16_000
    max_concurrency: int = 4
    task_max_retries: int = 2
    task_backoff_s: float = 0.5
    cheap_model: str = "gemini-2.5-flash"
    frontier_model: str = "gemini-2.5-flash"
    rpm: int = 5
    max_retries: int = 2
    rate_limit: bool = True
    repo_map_chars: int = 1200
    aspect_pack_chars: int = 6000


def _env_hosts(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return ()
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def get_limits() -> Limits:
    load_env()
    return Limits(
        max_repo_size=_env_int("CLIO_MAX_REPO_SIZE_MB", 50) * 1024 * 1024,
        max_files=_env_int("CLIO_MAX_FILES", 20_000),
        clone_timeout_s=_env_int("CLIO_CLONE_TIMEOUT_S", 120),
        workspace_root=Path(os.environ.get("CLIO_WORKSPACE_ROOT", "sandbox")),
        allowed_hosts=_env_hosts("CLIO_ALLOWED_HOSTS"),
        max_tool_output_chars=_env_int("CLIO_MAX_TOOL_OUTPUT_CHARS", 12_000),
        max_file_read_chars=_env_int("CLIO_MAX_FILE_READ_CHARS", 8_000),
        max_agent_steps=_env_int("CLIO_MAX_AGENT_STEPS", 10),
        subagent_max_context_chars=_env_int("CLIO_SUBAGENT_MAX_CONTEXT_CHARS", 16_000),
        max_concurrency=_env_int("CLIO_MAX_CONCURRENCY", 4),
        task_max_retries=_env_int("CLIO_TASK_MAX_RETRIES", 2),
        task_backoff_s=_env_float("CLIO_TASK_BACKOFF_S", 0.5),
        cheap_model=os.environ.get("CLIO_CHEAP_MODEL", "gemini-2.5-flash"),
        frontier_model=os.environ.get("CLIO_FRONTIER_MODEL", "gemini-2.5-flash"),
        rpm=max(_env_int("CLIO_RPM", 5), 1),
        max_retries=_env_int("CLIO_MAX_RETRIES", 2),
        rate_limit=os.environ.get("CLIO_RATE_LIMIT", "1") not in ("0", "false", "no"),
        repo_map_chars=_env_int("CLIO_REPO_MAP_CHARS", 1200),
        aspect_pack_chars=_env_int("CLIO_ASPECT_PACK_CHARS", 6000),
    )


def load_env() -> None:
    """Load ``KEY=VALUE`` lines from ``$CLIO_ENV_FILE`` (default ``.env`` in the
    current directory) into ``os.environ`` without overriding existing variables."""
    path = Path(os.environ.get("CLIO_ENV_FILE", ".env"))
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_provider() -> str:
    """LLM provider name from ``CLIO_PROVIDER`` (default ``"gemini"``)."""
    load_env()
    return os.environ.get("CLIO_PROVIDER", "gemini")

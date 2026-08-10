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


def test_harness_defaults():
    limits = get_limits()
    assert limits.max_tool_output_chars == 12_000
    assert limits.max_file_read_chars == 8_000
    assert limits.max_agent_steps == 10
    assert limits.subagent_max_context_chars == 16_000
    assert limits.max_concurrency == 4
    assert limits.task_max_retries == 2
    assert limits.task_backoff_s == 0.5
    assert limits.cheap_model == "gemini-2.0-flash"
    assert limits.frontier_model == "gemini-2.5-pro"


def test_harness_env_overrides(monkeypatch):
    monkeypatch.setenv("CLIO_MAX_AGENT_STEPS", "3")
    monkeypatch.setenv("CLIO_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("CLIO_CHEAP_MODEL", "gemini-2.0-flash-lite")
    monkeypatch.setenv("CLIO_TASK_BACKOFF_S", "0.1")
    limits = get_limits()
    assert limits.max_agent_steps == 3
    assert limits.max_concurrency == 2
    assert limits.cheap_model == "gemini-2.0-flash-lite"
    assert limits.task_backoff_s == 0.1


def test_harness_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("CLIO_MAX_AGENT_STEPS", "x")
    monkeypatch.setenv("CLIO_TASK_BACKOFF_S", "y")
    limits = get_limits()
    assert limits.max_agent_steps == 10
    assert limits.task_backoff_s == 0.5


# --- .env loading and provider resolution (M7) ---
import os

from clio.config import get_provider


def test_dotenv_applies_values(monkeypatch, tmp_path):
    env_file = tmp_path / "clio.env"
    env_file.write_text(
        "# local config\nCLIO_PROVIDER=groq\nGEMINI_API_KEY=secret-123\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLIO_ENV_FILE", str(env_file))
    monkeypatch.delenv("CLIO_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert get_provider() == "groq"
    assert os.environ["GEMINI_API_KEY"] == "secret-123"
    os.environ.pop("CLIO_PROVIDER", None)
    os.environ.pop("GEMINI_API_KEY", None)


def test_dotenv_does_not_override_existing(monkeypatch, tmp_path):
    env_file = tmp_path / "clio.env"
    env_file.write_text("CLIO_PROVIDER=groq\n", encoding="utf-8")
    monkeypatch.setenv("CLIO_ENV_FILE", str(env_file))
    monkeypatch.setenv("CLIO_PROVIDER", "gemini")
    assert get_provider() == "gemini"


def test_dotenv_supplies_limits(monkeypatch, tmp_path):
    env_file = tmp_path / "clio.env"
    env_file.write_text("CLIO_MAX_FILES=10\n", encoding="utf-8")
    monkeypatch.setenv("CLIO_ENV_FILE", str(env_file))
    monkeypatch.delenv("CLIO_MAX_FILES", raising=False)
    assert get_limits().max_files == 10
    os.environ.pop("CLIO_MAX_FILES", None)


def test_get_provider_from_env(monkeypatch):
    monkeypatch.setenv("CLIO_PROVIDER", "groq")
    monkeypatch.delenv("CLIO_ENV_FILE", raising=False)
    assert get_provider() == "groq"


def test_get_provider_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("CLIO_PROVIDER", raising=False)
    monkeypatch.delenv("CLIO_ENV_FILE", raising=False)
    assert get_provider() == "mock"

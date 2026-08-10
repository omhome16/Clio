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

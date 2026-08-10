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

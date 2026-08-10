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

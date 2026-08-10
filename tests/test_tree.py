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

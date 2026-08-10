# tests/test_tools.py
import asyncio

from clio.config import Limits
from clio.sandbox import Sandbox
from clio.tools import Tool, ToolRegistry


def _make_registry(tmp_path, job_id="job-x", tools=None, **limits_kwargs):
    sandbox = Sandbox(root=tmp_path / "sandbox")
    sandbox.create_workspace(job_id)
    limits = Limits(workspace_root=tmp_path / "sandbox", **limits_kwargs)
    return ToolRegistry(sandbox, job_id, tools=tools, limits=limits)


def test_read_file_ok(tmp_path):
    reg = _make_registry(tmp_path)
    (reg.workspace / "hello.txt").write_text("hello world", encoding="utf-8")
    result = asyncio.run(reg.execute("read_file", {"path": "hello.txt"}))
    assert result.ok and result.content == "hello world" and not result.truncated


def test_read_file_missing(tmp_path):
    reg = _make_registry(tmp_path)
    result = asyncio.run(reg.execute("read_file", {"path": "nope.txt"}))
    assert not result.ok and "error" in result.error.lower() or "no such" in result.error.lower()


def test_read_file_truncated(tmp_path):
    reg = _make_registry(tmp_path, max_tool_output_chars=10)
    (reg.workspace / "big.txt").write_text("x" * 100, encoding="utf-8")
    result = asyncio.run(reg.execute("read_file", {"path": "big.txt"}))
    assert result.truncated and len(result.content) < 50 and "truncated" in result.content


def test_read_file_escaping_rejected(tmp_path):
    reg = _make_registry(tmp_path)
    result = asyncio.run(reg.execute("read_file", {"path": "../../escape.txt"}))
    assert not result.ok and "sandbox" in result.error.lower()


def test_list_tree_ok(tmp_path):
    reg = _make_registry(tmp_path)
    (reg.workspace / "a.py").write_text("x", encoding="utf-8")
    (reg.workspace / "sub").mkdir()
    (reg.workspace / "sub" / "b.py").write_text("y", encoding="utf-8")
    result = asyncio.run(reg.execute("list_tree", {}))
    assert result.ok
    assert "a.py" in result.content and "sub/b.py" in result.content


def test_grep_finds_lines(tmp_path):
    reg = _make_registry(tmp_path)
    (reg.workspace / "app.py").write_text("import os\nprint('ok')\nimport sys\n", encoding="utf-8")
    result = asyncio.run(reg.execute("grep", {"pattern": "import"}))
    assert result.ok and "app.py:1" in result.content and "app.py:3" in result.content


def test_grep_skips_excluded_dirs(tmp_path):
    reg = _make_registry(tmp_path)
    (reg.workspace / "app.py").write_text("import x\n", encoding="utf-8")
    (reg.workspace / "node_modules").mkdir()
    (reg.workspace / "node_modules" / "dep.js").write_text("import y\n", encoding="utf-8")
    result = asyncio.run(reg.execute("grep", {"pattern": "import"}))
    assert result.ok and "node_modules" not in result.content


def test_git_log_lines(tmp_path, local_repo):
    from clio.clone import clone_repo
    sandbox = Sandbox(root=tmp_path / "sandbox")
    result = clone_repo(local_repo.as_uri(), sandbox, "job-git")
    reg = ToolRegistry(sandbox, "job-git")
    git_result = asyncio.run(reg.execute("git_log", {"count": 5}))
    assert git_result.ok and "init" in git_result.content


def test_unknown_tool(tmp_path):
    reg = _make_registry(tmp_path)
    result = asyncio.run(reg.execute("nope", {}))
    assert not result.ok and "unknown" in result.error


def test_tool_timeout(tmp_path):
    import time
    slow = Tool(name="slow", description="slow", handler=lambda args, ws: (time.sleep(0.5) or "x"), timeout_s=0.1)
    reg = _make_registry(tmp_path, tools=[slow])
    result = asyncio.run(reg.execute("slow", {}))
    assert not result.ok and "timed out" in result.error

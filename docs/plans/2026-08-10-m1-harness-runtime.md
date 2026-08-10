# M1 — Harness Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Clio's harness runtime: typed event bus, sandboxed tool registry, model-agnostic LLM client (mock + Gemini), a subagent with an isolated context tool-loop, an async fan-out scheduler with retries/timeouts, an orchestrator that runs the full analysis pipeline (clone → fan-out aspects → synthesize → persist), and a CLI to demo it headlessly.

**Architecture:** Seven small stdlib-plus-httpx modules, each with one responsibility, connected through dataclasses: `events` (pub/sub + SSE formatting), `tools` (registry + 4 sandboxed tools), `llm` (protocol + MockLLM + GeminiClient + reply parsing), `subagent` (tool loop with context budget), `scheduler` (fan_out + retries), `orchestrator` (phase machine wiring the pipeline, produces `AnalysisReport`), `cli` (headless demo). All tests run against `MockLLM` — zero network in CI; the only network test is one pre-existing clone error-path test.

**Tech Stack:** Python 3.11+, asyncio, httpx (Gemini REST only — imported lazily), pytest with `asyncio_mode = "auto"`.

## Global Constraints

- All code lives under `src/clio/` and `tests/`; never touch files outside this repo.
- Reuse M0 modules verbatim: `Sandbox`, `clone_repo`, `list_tree`, `workspace_stats`, `AnalysisJob`, `update_status`, `record_clone`, `jobs_dir`, `get_limits`.
- Tests must pass offline except the pre-existing `test_clone_repo_bad_source_cleans_workspace`.
- All LLM calls in tests use `MockLLM`; `GeminiClient` raises `LLMError` without `GEMINI_API_KEY`.
- New env vars (defaults): `CLIO_CHEAP_MODEL` (gemini-2.0-flash), `CLIO_FRONTIER_MODEL` (gemini-2.5-pro), `CLIO_MAX_TOOL_OUTPUT_CHARS` (12000), `CLIO_MAX_FILE_READ_CHARS` (8000), `CLIO_MAX_AGENT_STEPS` (10), `CLIO_SUBAGENT_MAX_CONTEXT_CHARS` (16000), `CLIO_MAX_CONCURRENCY` (4), `CLIO_TASK_MAX_RETRIES` (2), `CLIO_TASK_BACKOFF_S` (0.5).
- Every job status transition goes through `job.update_status` AND emits a matching event.
- Tool outputs are capped and truncated with a visible note; truncated means `ToolResult.truncated == True`.
- `asyncio_mode = "auto"` in pyproject so async tests need no decorator.

---

### Task 1: Extended limits config + pyproject async setup

**Files:**
- Modify: `pyproject.toml` (add `dependencies = ["httpx>=0.27"]`, `asyncio_mode = "auto"` under `[tool.pytest.ini_options]`)
- Modify: `src/clio/config.py` (add new fields + `_env_float` helper)
- Modify: `tests/test_config.py` (extend to 8 tests)

**Interfaces:**
- Produces: new `Limits` fields: `max_tool_output_chars: int`, `max_file_read_chars: int`, `max_agent_steps: int`, `subagent_max_context_chars: int`, `max_concurrency: int`, `task_max_retries: int`, `task_backoff_s: float`, `cheap_model: str`, `frontier_model: str`.

- [ ] **Step 1: Write the failing tests (extend existing file to these 8)**

```python
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
```

- [ ] **Step 2: Run tests — verify 3 new tests fail**

Run: `python -m pytest tests/test_config.py -v`
Expected: 5 passed, 3 failed (test_harness_defaults, test_harness_env_overrides, test_harness_env_invalid_falls_back)

- [ ] **Step 3: Implement**

```toml
# pyproject.toml — full file
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "clio"
version = "0.1.0"
description = "A repo analyzer with a visible nervous system"
requires-python = ">=3.11"
dependencies = ["httpx>=0.27"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
asyncio_mode = "auto"
```

```python
# src/clio/config.py — replace file
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
```

- [ ] **Step 4: Run tests — 8 passed**

Run: `python -m pytest tests/test_config.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/clio/config.py tests/test_config.py
git commit -m "feat: harness limits config and async test setup"
```

---

### Task 2: Event bus with SSE formatting

**Files:**
- Create: `src/clio/events.py`
- Create: `tests/test_events.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `clio.events.Event` frozen dataclass — `type: str`, `job_id: str`, `data: dict = {}`, `ts: str = ""` (auto-filled ISO-8601 UTC when empty).
  - `clio.events.EventBus` — `subscribe(fn: Callable[[Event], None])`, `publish(event: Event)`, `subscribers()`.
  - `clio.events.SseFormatter.format(event: Event) -> str` — `"data: {json}\n\n"`.
  - Event type constants: `EVENT_JOB_CREATED, EVENT_JOB_CLONING, EVENT_JOB_CLONED, EVENT_JOB_INDEXING, EVENT_JOB_ANALYZING, EVENT_SUBAGENT_START, EVENT_SUBAGENT_TOOL, EVENT_SUBAGENT_DONE, EVENT_JOB_SYNTHESIZING, EVENT_JOB_PERSISTED, EVENT_JOB_FAILED, EVENT_LOG`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_events.py
import json

from clio.events import (
    EVENT_JOB_PERSISTED, Event, EventBus, SseFormatter,
)


def test_event_ts_autofills():
    event = Event(type=EVENT_JOB_PERSISTED, job_id="clio-1")
    assert event.ts.startswith("2")
    assert len(event.ts) > 10


def test_event_ts_preserved():
    event = Event(type="x", job_id="j", ts="fixed")
    assert event.ts == "fixed"


def test_bus_delivers_to_subscribers():
    bus = EventBus()
    received = []
    bus.subscribe(received.append)
    event = Event(type="a", job_id="j")
    bus.publish(event)
    assert received == [event]


def test_bus_multiple_subscribers_in_order():
    bus = EventBus()
    first, second = [], []
    bus.subscribe(first.append)
    bus.subscribe(second.append)
    event = Event(type="a", job_id="j")
    bus.publish(event)
    assert first == second == [event]


def test_sse_format():
    event = Event(type=EVENT_JOB_PERSISTED, job_id="clio-1", data={"status": "ok"})
    raw = SseFormatter.format(event)
    assert raw.startswith("data: {")
    assert raw.endswith("\n\n")
    payload = json.loads(raw[len("data: "):])
    assert payload["type"] == EVENT_JOB_PERSISTED
    assert payload["job_id"] == "clio-1"
    assert payload["data"] == {"status": "ok"}
```

- [ ] **Step 2: Run tests — verify FAIL (`cannot import name 'Event'`)**

Run: `python -m pytest tests/test_events.py -v`

- [ ] **Step 3: Implement**

```python
# src/clio/events.py
"""Typed event bus and SSE serialization — the visibility layer."""
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable

EVENT_JOB_CREATED = "job.created"
EVENT_JOB_CLONING = "job.cloning"
EVENT_JOB_CLONED = "job.cloned"
EVENT_JOB_INDEXING = "job.indexing"
EVENT_JOB_ANALYZING = "job.analyzing"
EVENT_SUBAGENT_START = "subagent.start"
EVENT_SUBAGENT_TOOL = "subagent.tool"
EVENT_SUBAGENT_DONE = "subagent.done"
EVENT_JOB_SYNTHESIZING = "job.synthesizing"
EVENT_JOB_PERSISTED = "job.persisted"
EVENT_JOB_FAILED = "job.failed"
EVENT_LOG = "log"


@dataclass(frozen=True)
class Event:
    type: str
    job_id: str
    data: dict = field(default_factory=dict)
    ts: str = ""

    def __post_init__(self) -> None:
        if not self.ts:
            object.__setattr__(self, "ts", datetime.now(UTC).isoformat())


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[Callable[[Event], None]] = []

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._subscribers.append(fn)

    def publish(self, event: Event) -> None:
        for fn in self._subscribers:
            fn(event)

    def subscribers(self) -> list[Callable[[Event], None]]:
        return list(self._subscribers)


class SseFormatter:
    @staticmethod
    def format(event: Event) -> str:
        payload = json.dumps(
            {"type": event.type, "job_id": event.job_id, "data": event.data, "ts": event.ts}
        )
        return f"data: {payload}\n\n"
```

- [ ] **Step 4: Run tests — 5 passed**

Run: `python -m pytest tests/test_events.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/clio/events.py tests/test_events.py
git commit -m "feat: typed event bus with sse formatting"
```

---

### Task 3: Sandboxed tool registry

**Files:**
- Create: `src/clio/tools.py`
- Create: `tests/test_tools.py`

**Interfaces:**
- Consumes: `Sandbox` (M0), `list_tree` (M0), `get_limits` (Task 1).
- Produces:
  - `clio.tools.Tool` frozen dataclass — `name: str`, `description: str`, `handler: Callable[[dict, Path], str]`, `path_arg: str | None = None`, `timeout_s: int = 30`.
  - `clio.tools.ToolResult` dataclass — `ok: bool`, `content: str = ""`, `truncated: bool = False`, `error: str = ""`.
  - `clio.tools.ToolRegistry` — `ToolRegistry(sandbox: Sandbox, job_id: str, tools: Sequence[Tool] = BUILTIN_TOOLS, limits: Limits | None = None)`; `names() -> list[str]`; `get(name) -> Tool | None`; `async execute(name: str, args: dict) -> ToolResult`.
  - `BUILTIN_TOOLS` tuple: `read_file` (path_arg="path"), `list_tree`, `grep`, `git_log`.
  - Handlers run with `(args: dict, workspace: Path) -> str`; `path_arg` paths are containment-checked before the handler runs; output is capped at `limits.max_tool_output_chars` and truncated with `truncated=True`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

Note: `asyncio.wait_for` raises `TimeoutError` inside `execute`; the registry converts it to `ToolResult(ok=False, error="... timed out ...")` — the test asserts on the result, not the exception.

- [ ] **Step 2: Run — verify FAIL**

Run: `python -m pytest tests/test_tools.py -v`
Expected: FAIL — `cannot import name 'Tool' from 'clio.tools'`

- [ ] **Step 3: Implement**

```python
# src/clio/tools.py
"""Sandboxed tool registry: every tool is capped, timed, and contained."""
import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from clio.config import Limits, get_limits
from clio.sandbox import Sandbox, PathViolation
from clio.tree import list_tree

Handler = Callable[[dict, Path], str]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: Handler
    path_arg: str | None = None
    timeout_s: int = 30


@dataclass
class ToolResult:
    ok: bool
    content: str = ""
    truncated: bool = False
    error: str = ""


def _read_file(args: dict, workspace: Path) -> str:
    return Path(args["path"]).read_text(encoding="utf-8", errors="replace")


def _list_tree(args: dict, workspace: Path) -> str:
    paths = list_tree(workspace, max_files=args.get("max_files"))
    return "\n".join(p.as_posix() for p in paths) or "(empty)"


def _grep(args: dict, workspace: Path) -> str:
    pattern = args["pattern"].lower()
    limits = get_limits()
    hits: list[str] = []
    for path in sorted(workspace.rglob("*")):
        if path.is_dir():
            continue
        if any(part in limits.exclude_dirs for part in path.relative_to(workspace).parts):
            continue
        try:
            for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                if pattern in line.lower():
                    hits.append(f"{path.relative_to(workspace).as_posix()}:{line_no}:{line.strip()[:120]}")
                    if len(hits) >= 200:
                        return "\n".join(hits) + "\n...(200 line cap)"
        except OSError:
            continue
    return "\n".join(hits) or "(no matches)"


def _git_log(args: dict, workspace: Path) -> str:
    count = int(args.get("count", 20))
    proc = subprocess.run(
        ["git", "-C", str(workspace), "log", "--oneline", f"-n", str(count)],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return f"(git log failed: {proc.stderr.strip()[:200]})"
    return proc.stdout.strip() or "(no commits)"


READ_FILE = Tool(name="read_file", description="Read a text file from the workspace", handler=_read_file, path_arg="path")
LIST_TREE = Tool(name="list_tree", description="List files in the workspace tree", handler=_list_tree)
GREP = Tool(name="grep", description="Find lines containing a pattern (case-insensitive)", handler=_grep)
GIT_LOG = Tool(name="git_log", description="Recent commit messages of the repo", handler=_git_log)

BUILTIN_TOOLS: tuple[Tool, ...] = (READ_FILE, LIST_TREE, GREP, GIT_LOG)


class ToolRegistry:
    def __init__(
        self,
        sandbox: Sandbox,
        job_id: str,
        tools: Sequence[Tool] = BUILTIN_TOOLS,
        limits: Limits | None = None,
    ) -> None:
        self._sandbox = sandbox
        self.job_id = job_id
        self._tools = {tool.name: tool for tool in (tools or BUILTIN_TOOLS)}
        self._limits = limits or get_limits()

    @property
    def workspace(self) -> Path:
        return self._sandbox.workspace(self.job_id)

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    async def execute(self, name: str, args: dict) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(ok=False, error=f"unknown tool '{name}'")
        resolved = dict(args)
        if tool.path_arg is not None:
            raw = args.get(tool.path_arg)
            if raw is None:
                return ToolResult(ok=False, error=f"missing required arg '{tool.path_arg}'")
            try:
                # Resolve relative to the WORKSPACE (never the process CWD).
                resolved[tool.path_arg] = str(
                    self._sandbox.ensure_contained(self.workspace / raw)
                )
            except PathViolation as exc:
                return ToolResult(ok=False, error=f"path escapes sandbox: {exc}")
        try:
            content = await asyncio.wait_for(
                asyncio.to_thread(tool.handler, resolved, self.workspace),
                timeout=tool.timeout_s,
            )
        except TimeoutError:
            return ToolResult(ok=False, error=f"tool '{name}' timed out after {tool.timeout_s}s")
        except Exception as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        cap = self._limits.max_tool_output_chars
        if len(content) > cap:
            content = content[:cap] + f"\n...[truncated, dropped {len(content) - cap} chars]"
            return ToolResult(ok=True, content=content, truncated=True)
        return ToolResult(ok=True, content=content)
```

- [ ] **Step 4: Run — 10 passed**

Run: `python -m pytest tests/test_tools.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/clio/tools.py tests/test_tools.py
git commit -m "feat: sandboxed tool registry with caps, timeouts, containment"
```

---

### Task 4: Model-agnostic LLM client + reply parsing

**Files:**
- Create: `src/clio/llm.py`
- Create: `tests/test_llm.py`

**Interfaces:**
- Produces:
  - `clio.llm.LLMMessage` frozen dataclass — `role: str`, `content: str`.
  - `clio.llm.LLMError(RuntimeError)`.
  - `clio.llm.LLMClient` Protocol — `async complete(messages: list[LLMMessage], *, model: str | None = None, max_tokens: int = 2000) -> str`.
  - `clio.llm.MockLLM` — `MockLLM(responses: list[str] | None = None, handler: Callable[[list[LLMMessage], str | None], str] | None = None)`; records `calls: list[list[LLMMessage]]`; pops scripted responses; raises `LLMError` when exhausted.
  - `clio.llm.GeminiClient` — `GeminiClient(api_key: str | None = None)`; raises `LLMError` without a key; lazily imports httpx.
  - `clio.llm.ToolCall` frozen dataclass — `tool: str`, `args: dict`.
  - `clio.llm.LLMReply` frozen dataclass — `kind: Literal["tool", "final", "none"]`, `tool: ToolCall | None = None`, `final: str | None = None`.
  - `clio.llm.parse_reply(text: str) -> LLMReply` — strips ``` fences; JSON dict with `"tool"` wins over `"final"`; plain text → `final`; unparseable → `none`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_llm.py
import pytest

from clio.llm import (
    GeminiClient, LLMError, LLMMessage, MockLLM, ToolCall, parse_reply,
)


def test_parse_plain_text_is_final():
    reply = parse_reply("just a summary")
    assert reply.kind == "final" and reply.final == "just a summary"


def test_parse_json_final():
    reply = parse_reply('{"final": "done"}')
    assert reply.kind == "final" and reply.final == "done"


def test_parse_json_tool():
    reply = parse_reply('{"tool": "read_file", "args": {"path": "a.py"}}')
    assert reply.kind == "tool"
    assert reply.tool == ToolCall(tool="read_file", args={"path": "a.py"})


def test_parse_fenced_json():
    reply = parse_reply('```json\n{"tool": "list_tree", "args": {}}\n```')
    assert reply.kind == "tool" and reply.tool.tool == "list_tree"


def test_parse_tool_wins_over_final():
    reply = parse_reply('{"tool": "grep", "args": {}, "final": "nope"}')
    assert reply.kind == "tool"


def test_parse_garbage_is_none():
    reply = parse_reply("not json at all {{")
    assert reply.kind == "none"


def test_parse_empty_is_none():
    assert parse_reply("").kind == "none"


async def test_mock_llm_pops_scripted():
    mock = MockLLM(responses=["one", "two"])
    out1 = await mock.complete([LLMMessage(role="user", content="hi")], model="m")
    out2 = await mock.complete([LLMMessage(role="user", content="hi")], model="m")
    assert (out1, out2) == ("one", "two")
    assert len(mock.calls) == 2


async def test_mock_llm_exhausted_raises():
    mock = MockLLM(responses=[])
    with pytest.raises(LLMError):
        await mock.complete([LLMMessage(role="user", content="hi")])


async def test_mock_llm_handler_mode():
    mock = MockLLM(handler=lambda messages, model: f"handled-{model}")
    out = await mock.complete([LLMMessage(role="user", content="hi")], model="cheap")
    assert out == "handled-cheap"


def test_gemini_requires_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(LLMError):
        GeminiClient(api_key=None)
```

- [ ] **Step 2: Run — verify FAIL**

Run: `python -m pytest tests/test_llm.py -v`
Expected: FAIL — `cannot import name 'MockLLM' from 'clio.llm'`

- [ ] **Step 3: Implement**

```python
# src/clio/llm.py
"""Model-agnostic LLM client interface, mock, and Gemini implementation."""
import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Literal, Protocol


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMMessage:
    role: str
    content: str


class LLMClient(Protocol):
    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        max_tokens: int = 2000,
    ) -> str: ...


class MockLLM:
    def __init__(
        self,
        responses: list[str] | None = None,
        handler: Callable[[list[LLMMessage], str | None], str] | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._handler = handler
        self.calls: list[list[LLMMessage]] = []

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        max_tokens: int = 2000,
    ) -> str:
        self.calls.append(messages)
        if self._handler is not None:
            return self._handler(messages, model)
        if not self._responses:
            raise LLMError("no scripted responses left")
        return self._responses.pop(0)


class GeminiClient:
    def __init__(self, api_key: str | None = None) -> None:
        self._key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self._key:
            raise LLMError("GEMINI_API_KEY is not set")

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        max_tokens: int = 2000,
    ) -> str:
        try:
            import httpx
        except ImportError as exc:
            raise LLMError("httpx is required for GeminiClient") from exc
        model = model or "gemini-2.0-flash"
        payload = {
            "contents": [
                {"role": m.role, "parts": [{"text": m.content}]} for m in messages
            ],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={self._key}"
        )
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        return "".join(part.get("text", "") for part in parts)


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: dict


@dataclass(frozen=True)
class LLMReply:
    kind: Literal["tool", "final", "none"]
    tool: ToolCall | None = None
    final: str | None = None


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) > 1:
            return "\n".join(lines[1:]).removesuffix("```").strip()
    return stripped


def parse_reply(text: str) -> LLMReply:
    cleaned = _strip_fence(text)
    if not cleaned:
        return LLMReply(kind="none")
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        # Unparseable: JSON-ish garbage is "none"; plain prose is a final answer.
        if "{" in cleaned:
            return LLMReply(kind="none")
        return LLMReply(kind="final", final=cleaned)
    if isinstance(obj, dict):
        if "tool" in obj:
            return LLMReply(
                kind="tool",
                tool=ToolCall(tool=str(obj["tool"]), args=dict(obj.get("args") or {})),
            )
        if "final" in obj:
            return LLMReply(kind="final", final=str(obj["final"]))
    return LLMReply(kind="none")
```

- [ ] **Step 4: Run — 11 passed**

Run: `python -m pytest tests/test_llm.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/clio/llm.py tests/test_llm.py
git commit -m "feat: model-agnostic llm client with mock and gemini backends"
```

---

### Task 5: Subagent — the tool loop with context budget

**Files:**
- Create: `src/clio/subagent.py`
- Create: `tests/test_subagent.py`

**Interfaces:**
- Consumes: `LLMClient`, `MockLLM`, `parse_reply` (Task 4), `ToolRegistry` (Task 3), `EventBus` + `EVENT_SUBAGENT_*` (Task 2), `get_limits` (Task 1).
- Produces:
  - `clio.subagent.SubagentSpec` frozen dataclass — `name: str`, `role: str`, `system_prompt: str`, `tools: tuple[str, ...]`.
  - `clio.subagent.SubagentReport` dataclass — `name`, `content: str`, `steps: int`, `tool_calls: int`, `ok: bool = True`; `to_dict()` / `from_dict()`.
  - `clio.subagent.Subagent` — `Subagent(spec, client, registry, *, bus=None, job_id="", model=None, max_steps=None, max_context_chars=None)`; `async run(task: str) -> SubagentReport` — system+user bootstrap, loop: complete → parse → final→break / tool→execute+append result (tool errors loop back in-band) / none→ok=False break; emits `EVENT_SUBAGENT_START` before, `EVENT_SUBAGENT_TOOL` per tool, `EVENT_SUBAGENT_DONE` after (also when hitting max steps); context compaction when total chars exceed `max_context_chars` (keep system + first user + last 4 messages + note).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_subagent.py
import json

from clio.events import EVENT_SUBAGENT_DONE, EVENT_SUBAGENT_START, EVENT_SUBAGENT_TOOL, Event, EventBus
from clio.llm import LLMMessage, MockLLM
from clio.sandbox import Sandbox
from clio.subagent import Subagent, SubagentReport, SubagentSpec
from clio.tools import ToolRegistry


SPEC = SubagentSpec(name="t", role="test agent", system_prompt="be good", tools=("read_file",))


def _agent(mock, tmp_path, **kwargs):
    sandbox = Sandbox(root=tmp_path / "sb")
    sandbox.create_workspace("j")
    registry = ToolRegistry(sandbox, "j")
    return Subagent(SPEC, mock, registry, job_id="j", **kwargs)


async def test_tool_loop_then_final(tmp_path):
    mock = MockLLM(responses=[
        json.dumps({"tool": "list_tree", "args": {}}),
        json.dumps({"final": "all good"}),
    ])
    agent = _agent(mock, tmp_path)
    report = await agent.run("analyze")
    assert report.ok and report.content == "all good"
    assert report.tool_calls == 1 and report.steps == 2
    tool_msg = mock.calls[1][-1]
    assert tool_msg.role == "tool"


async def test_tool_error_loops_back_and_recovers(tmp_path):
    mock = MockLLM(responses=[
        json.dumps({"tool": "read_file", "args": {"path": "missing.txt"}}),
        json.dumps({"final": "recovered"}),
    ])
    agent = _agent(mock, tmp_path)
    report = await agent.run("x")
    assert report.ok and report.content == "recovered"
    error_msg = mock.calls[1][-1]
    assert error_msg.role == "tool" and "error" in error_msg.content.lower() or "no such" in error_msg.content.lower()


async def test_max_steps_caps_and_marks_not_ok(tmp_path):
    responses = [json.dumps({"tool": "list_tree", "args": {}})] * 10
    mock = MockLLM(responses=responses)
    agent = _agent(mock, tmp_path, max_steps=3)
    report = await agent.run("x")
    assert not report.ok and report.steps == 3
    assert "max steps" in report.content


async def test_events_emitted(tmp_path):
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    mock = MockLLM(responses=[json.dumps({"tool": "list_tree", "args": {}}), json.dumps({"final": "ok"})])
    agent = _agent(mock, tmp_path, bus=bus)
    await agent.run("x")
    types = [e.type for e in seen]
    assert types[0] == EVENT_SUBAGENT_START
    assert EVENT_SUBAGENT_TOOL in types
    assert types[-1] == EVENT_SUBAGENT_DONE


async def test_compaction_trims_context(tmp_path):
    mock = MockLLM(handler=lambda messages, model: json.dumps({"final": "done"}))
    agent = _agent(mock, tmp_path, max_context_chars=200)
    big = "x" * 10_000
    report = await agent.run(big)
    assert report.ok
    compacted = [m for m in mock.calls[0] if "dropped to fit budget" in m.content]
    assert compacted
    assert sum(len(m.content) for m in mock.calls[0]) <= 200
```

- [ ] **Step 2: Run — verify FAIL**

Run: `python -m pytest tests/test_subagent.py -v`
Expected: FAIL — `cannot import name 'Subagent' from 'clio.subagent'`

- [ ] **Step 3: Implement**

```python
# src/clio/subagent.py
"""A subagent: isolated context window + tool loop + context budget."""
from dataclasses import asdict, dataclass

from clio.config import Limits, get_limits
from clio.events import (
    EVENT_SUBAGENT_DONE, EVENT_SUBAGENT_START, EVENT_SUBAGENT_TOOL, Event, EventBus,
)
from clio.llm import LLMClient, LLMMessage, parse_reply


@dataclass(frozen=True)
class SubagentSpec:
    name: str
    role: str
    system_prompt: str
    tools: tuple[str, ...]


@dataclass
class SubagentReport:
    name: str
    content: str
    steps: int
    tool_calls: int
    ok: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SubagentReport":
        return cls(**data)


class Subagent:
    def __init__(
        self,
        spec: SubagentSpec,
        client: LLMClient,
        registry,
        *,
        bus: EventBus | None = None,
        job_id: str = "",
        model: str | None = None,
        max_steps: int | None = None,
        max_context_chars: int | None = None,
    ) -> None:
        self.spec = spec
        self._client = client
        self._registry = registry
        self._bus = bus
        self._job_id = job_id
        self._model = model
        limits = get_limits()
        self._max_steps = max_steps if max_steps is not None else limits.max_agent_steps
        self._max_context_chars = (
            max_context_chars if max_context_chars is not None else limits.subagent_max_context_chars
        )

    def _emit(self, event_type: str, data: dict) -> None:
        if self._bus is not None:
            self._bus.publish(Event(type=event_type, job_id=self._job_id, data=data))

    def _compact(self, messages: list[LLMMessage]) -> None:
        if sum(len(m.content) for m in messages) <= self._max_context_chars:
            return
        head = messages[:2]
        tail = messages[-4:] if len(messages) > 4 else messages[2:]
        note = LLMMessage(role="tool", content="...(earlier context dropped to fit budget)")
        messages[:] = head + tail + [note]
        if len(head[1].content) > self._max_context_chars // 2:
            head[1] = LLMMessage(
                role=head[1].role,
                content=head[1].content[: self._max_context_chars // 2]
                + "...(truncated to fit budget)",
            )
            messages[:] = [head[0], head[1]] + tail + [note]

    async def run(self, task: str) -> SubagentReport:
        messages = [
            LLMMessage(role="system", content=self.spec.system_prompt),
            LLMMessage(role="user", content=task),
        ]
        self._emit(EVENT_SUBAGENT_START, {"name": self.spec.name, "role": self.spec.role})
        steps = 0
        tool_calls = 0
        content = ""
        ok = True
        while steps < self._max_steps:
            steps += 1
            self._compact(messages)
            text = await self._client.complete(messages, model=self._model)
            reply = parse_reply(text)
            if reply.kind == "tool":
                tool_calls += 1
                self._emit(EVENT_SUBAGENT_TOOL, {"name": self.spec.name, "tool": reply.tool.tool, "args": reply.tool.args})
                result = await self._registry.execute(reply.tool.tool, reply.tool.args)
                message = (
                    f"tool result (ok={result.ok}):\n{result.content}"
                    if result.ok
                    else f"tool error: {result.error}"
                )
                messages.append(LLMMessage(role="assistant", content=text))
                messages.append(LLMMessage(role="tool", content=message))
                continue
            if reply.kind == "final":
                content = reply.final or ""
                break
            ok = False
            content = text or "(unparseable model output)"
            break
        else:
            ok = False
            content = "(max steps reached)"
        report = SubagentReport(
            name=self.spec.name, content=content, steps=steps, tool_calls=tool_calls, ok=ok
        )
        self._emit(EVENT_SUBAGENT_DONE, {"name": self.spec.name, "ok": ok, "steps": steps, "tool_calls": tool_calls})
        return report
```

- [ ] **Step 4: Run — 5 passed**

Run: `python -m pytest tests/test_subagent.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/clio/subagent.py tests/test_subagent.py
git commit -m "feat: subagent tool loop with context budget and events"
```

---

### Task 6: Async fan-out scheduler with retries and timeouts

**Files:**
- Create: `src/clio/scheduler.py`
- Create: `tests/test_scheduler.py`

**Interfaces:**
- Produces:
  - `clio.scheduler.run_with_retries(fn: Callable[[], Awaitable[R]], *, max_retries: int = 2, backoff_s: float = 0.5, timeout_s: float | None = None) -> R` — retries on `Exception` (backoff between attempts), `asyncio.wait_for` when `timeout_s` set, raises last exception.
  - `clio.scheduler.fan_out(items: Sequence[K], worker: Callable[[K], Awaitable[R]], *, max_concurrency: int = 4, max_retries: int = 2, backoff_s: float = 0.5, timeout_s: float | None = None) -> dict[K, R | BaseException]` — one item → one result; per-item exceptions captured as values (never raised).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_scheduler.py
import asyncio
import time

import pytest

from clio.scheduler import fan_out, run_with_retries


async def test_fan_out_returns_results():
    async def double(x):
        return x * 2

    results = await fan_out([1, 2, 3], double, max_concurrency=2)
    assert results == {1: 2, 2: 4, 3: 6}


async def test_fan_out_respects_concurrency_cap():
    active = 0
    peak = 0

    async def slow(x):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return x

    results = await fan_out(list(range(6)), slow, max_concurrency=3)
    assert peak == 3
    assert len(results) == 6


async def test_fan_out_captures_failures():
    async def boom(x):
        if x == 2:
            raise ValueError("nope")
        return x

    results = await fan_out([1, 2, 3], boom, max_concurrency=2)
    assert results[1] == 1
    assert isinstance(results[2], ValueError)
    assert results[3] == 3


async def test_run_with_retries_eventually_succeeds():
    calls = {"n": 0}

    async def worker():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("flaky")
        return "ok"

    out = await run_with_retries(worker, max_retries=3, backoff_s=0)
    assert out == "ok" and calls["n"] == 3


async def test_run_with_retries_raises_after_exhaustion():
    async def always_fails():
        raise ValueError("still failing")

    with pytest.raises(ValueError):
        await run_with_retries(always_fails, max_retries=1, backoff_s=0)


async def test_run_with_retries_timeout():
    async def sleepy():
        await asyncio.sleep(1)
        return "late"

    with pytest.raises(TimeoutError):
        await run_with_retries(sleepy, max_retries=0, timeout_s=0.05)
```

- [ ] **Step 2: Run — verify FAIL**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: FAIL — `cannot import name 'fan_out' from 'clio.scheduler'`

- [ ] **Step 3: Implement**

```python
# src/clio/scheduler.py
"""Async fan-out: bounded concurrency, per-item retries, per-item timeouts."""
import asyncio
from typing import Awaitable, Callable, Sequence, TypeVar

K = TypeVar("K")
R = TypeVar("R")


async def run_with_retries(
    fn: Callable[[], Awaitable[R]],
    *,
    max_retries: int = 2,
    backoff_s: float = 0.5,
    timeout_s: float | None = None,
) -> R:
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            coro = fn()
            if timeout_s is not None:
                return await asyncio.wait_for(coro, timeout=timeout_s)
            return await coro
        except Exception as exc:
            last = exc
            if attempt < max_retries:
                await asyncio.sleep(backoff_s)
    assert last is not None
    raise last


async def fan_out(
    items: Sequence[K],
    worker: Callable[[K], Awaitable[R]],
    *,
    max_concurrency: int = 4,
    max_retries: int = 2,
    backoff_s: float = 0.5,
    timeout_s: float | None = None,
) -> dict[K, R | BaseException]:
    semaphore = asyncio.Semaphore(max_concurrency)
    results: dict[K, R | BaseException] = {}

    async def run_one(item: K) -> None:
        async with semaphore:
            try:
                results[item] = await run_with_retries(
                    lambda: worker(item),
                    max_retries=max_retries,
                    backoff_s=backoff_s,
                    timeout_s=timeout_s,
                )
            except BaseException as exc:
                results[item] = exc

    await asyncio.gather(*(run_one(item) for item in items))
    return results
```

- [ ] **Step 4: Run — 6 passed**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/clio/scheduler.py tests/test_scheduler.py
git commit -m "feat: async fan-out scheduler with retries and timeouts"
```

---

### Task 7: Orchestrator — the phase machine

**Files:**
- Create: `src/clio/orchestrator.py`
- Create: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `clio.orchestrator.make_aspect_specs() -> tuple[SubagentSpec, ...]` — four aspects: `structure`, `dependencies`, `risks`, `entrypoints` (prompts below).
  - `clio.orchestrator.SYNTH_SPEC` — synthesizer subagent (no tools).
  - `clio.orchestrator.AnalysisReport` dataclass — `job_id`, `repo_url`, `commit_sha`, `aspects: dict[str, dict]` (name → `SubagentReport.to_dict()` or `{"ok": False, "error": ...}`), `summary: str`, `created_at: str`; `to_dict()` / `from_dict()`.
  - `clio.orchestrator.Orchestrator` — `Orchestrator(sandbox: Sandbox, client: LLMClient, *, bus: EventBus | None = None, limits: Limits | None = None)`; `async run(url: str, root: Path, *, job_id: str | None = None) -> AnalysisReport` — full pipeline; emits events for every phase; persists `jobs_dir(root)/<job_id>.report.json`; on failure sets job status `FAILED`, emits `EVENT_JOB_FAILED`, re-raises.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_orchestrator.py
import json

import pytest

from clio.config import Limits
from clio.events import (
    EVENT_JOB_CLONED, EVENT_JOB_FAILED, EVENT_JOB_PERSISTED,
    EVENT_SUBAGENT_DONE, EVENT_SUBAGENT_START, Event, EventBus,
)
from clio.job import load_job
from clio.llm import LLMMessage, MockLLM
from clio.orchestrator import AnalysisReport, Orchestrator
from clio.sandbox import Sandbox


def _mock_handler(limits):
    def handler(messages, model):
        if model == limits.frontier_model:
            return json.dumps({"final": '{"summary": "merged", "modules": ["core"]}'})
        if len(messages) < 3:
            return json.dumps({"tool": "list_tree", "args": {}})
        return json.dumps({"final": '{"findings": ["nothing"]}'})
    return handler


async def test_full_pipeline(tmp_path, local_repo):
    limits = Limits(workspace_root=tmp_path / "sandbox", max_agent_steps=5)
    sandbox = Sandbox(root=tmp_path / "sandbox", limits=limits)
    client = MockLLM(handler=_mock_handler(limits))
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    orch = Orchestrator(sandbox, client, bus=bus, limits=limits)
    report = await orch.run(local_repo.as_uri(), root=tmp_path, job_id="clio-test")
    assert report.repo_url == local_repo.as_uri()
    assert len(report.commit_sha) == 12
    assert set(report.aspects) == {"structure", "dependencies", "risks", "entrypoints"}
    assert all(a["ok"] for a in report.aspects.values())
    assert report.summary == "merged"
    assert load_job("clio-test", tmp_path).status == "PERSISTED"
    report_file = (tmp_path / "jobs" / "clio-test.report.json")
    assert report_file.is_file()
    types = [e.type for e in seen]
    assert EVENT_JOB_CLONED in types and EVENT_JOB_PERSISTED in types
    assert types.count(EVENT_SUBAGENT_START) == 4
    assert types.count(EVENT_SUBAGENT_DONE) == 4


async def test_failed_clone_marks_job_failed(tmp_path):
    limits = Limits(workspace_root=tmp_path / "sandbox")
    sandbox = Sandbox(root=tmp_path / "sandbox", limits=limits)
    client = MockLLM(handler=_mock_handler(limits))
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    orch = Orchestrator(sandbox, client, bus=bus, limits=limits)
    with pytest.raises(Exception):
        await orch.run("https://github.com/omhome16/does-not-exist-xyz.git", root=tmp_path, job_id="clio-fail")
    job = load_job("clio-fail", tmp_path)
    assert job.status == "FAILED"
    assert any(e.type == EVENT_JOB_FAILED for e in seen)


def test_report_roundtrip():
    report = AnalysisReport(
        job_id="clio-1", repo_url="https://github.com/x/y.git", commit_sha="abc",
        aspects={"a": {"ok": True, "content": "z"}}, summary="s",
        created_at="2026-08-10T00:00:00+00:00",
    )
    restored = AnalysisReport.from_dict(report.to_dict())
    assert restored == report
```

- [ ] **Step 2: Run — verify FAIL**

Run: `python -m pytest tests/test_orchestrator.py -v`
Expected: FAIL — `cannot import name 'Orchestrator' from 'clio.orchestrator'`

- [ ] **Step 3: Implement**

```python
# src/clio/orchestrator.py
"""The orchestrator: phase machine that drives the whole analysis pipeline."""
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from clio.clone import CloneError, clone_repo
from clio.config import Limits, get_limits
from clio.events import (
    EVENT_JOB_ANALYZING, EVENT_JOB_CLONED, EVENT_JOB_CLONING, EVENT_JOB_CREATED,
    EVENT_JOB_FAILED, EVENT_JOB_INDEXING, EVENT_JOB_PERSISTED, EVENT_JOB_SYNTHESIZING,
    EVENT_SUBAGENT_DONE, Event, EventBus,
)
from clio.job import AnalysisJob, jobs_dir, new_job, record_clone, update_status
from clio.llm import LLMClient
from clio.sandbox import Sandbox
from clio.scheduler import fan_out
from clio.subagent import Subagent, SubagentReport, SubagentSpec
from clio.tools import ToolRegistry

ASPECT_TASK = (
    "Analyze the repository {repo} (commit {commit}) for the aspect: {aspect}.\n"
    "Use the available tools to inspect the workspace. "
    "Reply with a final JSON object containing your findings."
)


def make_aspect_specs() -> tuple[SubagentSpec, ...]:
    return (
        SubagentSpec(
            name="structure",
            role="files and layout",
            system_prompt=(
                "You map a repository's file layout: top-level organization, "
                "package boundaries, and what each major directory contains."
            ),
            tools=("list_tree", "read_file"),
        ),
        SubagentSpec(
            name="dependencies",
            role="import and dependency relationships",
            system_prompt=(
                "You trace how modules depend on each other: imports, shared "
                "components, and coupling hotspots."
            ),
            tools=("grep", "read_file", "list_tree"),
        ),
        SubagentSpec(
            name="risks",
            role="quality risks and failure points",
            system_prompt=(
                "You find quality risks: dead code, swallowed exceptions, "
                "missing tests, hardcoded secrets, and fragile patterns."
            ),
            tools=("grep", "read_file"),
        ),
        SubagentSpec(
            name="entrypoints",
            role="entry points and run flow",
            system_prompt=(
                "You identify entry points (main functions, CLI, scripts, "
                "servers) and trace the main execution flow."
            ),
            tools=("list_tree", "read_file", "git_log"),
        ),
    )


SYNTH_SPEC = SubagentSpec(
    name="synthesizer",
    role="merge aspect findings into an architecture summary",
    system_prompt=(
        "You are the synthesis stage. Merge per-aspect findings into a final "
        "architecture summary. Reply with a JSON object: "
        '{"summary": "...", "modules": ["..."]}.'
    ),
    tools=(),
)

SYNTH_TASK = (
    "Per-aspect findings for {repo}:\n{findings}\n"
    'Produce the final summary JSON: {{"summary": "...", "modules": ["..."]}}.'
)


@dataclass
class AnalysisReport:
    job_id: str
    repo_url: str
    commit_sha: str
    aspects: dict[str, dict]
    summary: str
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AnalysisReport":
        return cls(**data)


class Orchestrator:
    def __init__(
        self,
        sandbox: Sandbox,
        client: LLMClient,
        *,
        bus: EventBus | None = None,
        limits: Limits | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._client = client
        self._bus = bus
        self._limits = limits or get_limits()

    def _emit(self, event_type: str, job_id: str, data: dict) -> None:
        if self._bus is not None:
            self._bus.publish(Event(type=event_type, job_id=job_id, data=data))

    async def run(
        self,
        url: str,
        root: Path,
        *,
        job_id: str | None = None,
    ) -> AnalysisReport:
        job = new_job(url, job_id=job_id)
        self._emit(EVENT_JOB_CREATED, job.job_id, {"url": url})
        try:
            update_status(job, "CLONING", root)
            self._emit(EVENT_JOB_CLONING, job.job_id, {})
            clone = clone_repo(url, self._sandbox, job.job_id)
            self._emit(EVENT_JOB_CLONED, job.job_id, {"commit_sha": clone.commit_sha})
            record_clone(job, clone, root)
            self._emit(EVENT_JOB_INDEXING, job.job_id, {})

            update_status(job, "ANALYZING", root)
            self._emit(EVENT_JOB_ANALYZING, job.job_id, {})
            registry = ToolRegistry(self._sandbox, job.job_id, limits=self._limits)
            specs = make_aspect_specs()
            subs = {
                spec.name: Subagent(
                    spec, self._client, registry,
                    bus=self._bus, job_id=job.job_id,
                    model=self._limits.cheap_model, max_steps=self._limits.max_agent_steps,
                )
                for spec in specs
            }
            task = ASPECT_TASK.format(repo=url, commit=clone.commit_sha, aspect="{aspect}")
            outcomes = await fan_out(
                list(specs),
                lambda spec: subs[spec.name].run(task.format(aspect=spec.role)),
                max_concurrency=self._limits.max_concurrency,
            )
            aspects: dict[str, dict] = {}
            for spec in specs:
                outcome = outcomes[spec]
                if isinstance(outcome, BaseException):
                    aspects[spec.name] = {"ok": False, "error": repr(outcome), "content": ""}
                    self._emit(EVENT_SUBAGENT_DONE, job.job_id, {"name": spec.name, "ok": False})
                else:
                    aspects[spec.name] = outcome.to_dict()

            update_status(job, "SYNTHESIZING", root)
            self._emit(EVENT_JOB_SYNTHESIZING, job.job_id, {})
            synth = Subagent(
                SYNTH_SPEC, self._client, registry,
                bus=None, job_id=job.job_id,
                model=self._limits.frontier_model, max_steps=self._limits.max_agent_steps,
            )
            synth_report = await synth.run(
                SYNTH_TASK.format(repo=url, findings=json.dumps(aspects, indent=2))
            )
            # Synthesizer emits its findings as JSON; the summary field is the
            # "merged" verdict, falling back to the raw content if unparseable.
            try:
                summary = json.loads(synth_report.content).get("summary", synth_report.content)
            except json.JSONDecodeError:
                summary = synth_report.content
            report = AnalysisReport(
                job_id=job.job_id,
                repo_url=url,
                commit_sha=clone.commit_sha,
                aspects=aspects,
                summary=summary,
                created_at=datetime.now(UTC).isoformat(),
            )
            jobs_dir(root).mkdir(parents=True, exist_ok=True)
            (jobs_dir(root) / f"{job.job_id}.report.json").write_text(
                json.dumps(report.to_dict(), indent=2), encoding="utf-8"
            )
            update_status(job, "PERSISTED", root)
            self._emit(EVENT_JOB_PERSISTED, job.job_id, {"report": f"{job.job_id}.report.json"})
            return report
        except Exception as exc:
            try:
                update_status(job, "FAILED", root)
            except Exception:
                pass
            self._emit(EVENT_JOB_FAILED, job.job_id, {"error": str(exc)})
            raise
```

- [ ] **Step 4: Run — 3 passed (network needed for clone failure test; offline it FAILS that one)**

Run: `python -m pytest tests/test_orchestrator.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/clio/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: orchestrator phase machine with event-driven pipeline"
```

---

### Task 8: CLI demo + end-to-end test

**Files:**
- Create: `src/clio/cli.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `clio.cli.build_parser() -> argparse.ArgumentParser` — args: `url`, `--provider {mock,gemini}` (default mock), `--job-id`.
  - `clio.cli.amain(args: argparse.Namespace) -> int` — wires bus → prints each event as `[ts] type data`; builds `Sandbox(get_limits().workspace_root)`; runs orchestrator; prints `REPORT:` + JSON; returns 0.
  - `clio.cli.main()` — `SystemExit(asyncio.run(amain(build_parser().parse_args())))`; `if __name__ == "__main__": main()`.
  - Mock provider handler: cheap model → `{"tool": "list_tree", "args": {}}` on first call, then `{"final": "..."}`; frontier → final summary JSON. Reuses the orchestrator's task pattern.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli.py
import json
import sys

import pytest

from clio.cli import amain, build_parser


def test_parser_defaults():
    args = build_parser().parse_args(["https://github.com/x/y.git"])
    assert args.url == "https://github.com/x/y.git"
    assert args.provider == "mock"


def test_parser_invalid_provider_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["https://github.com/x/y.git", "--provider", "nope"])


async def test_cli_end_to_end_mock(tmp_path, local_repo, monkeypatch, capsys):
    monkeypatch.setenv("CLIO_WORKSPACE_ROOT", str(tmp_path / "sandbox"))
    args = build_parser().parse_args([local_repo.as_uri()])
    assert await amain(args) == 0
    out = capsys.readouterr().out
    assert "job.cloned" in out
    assert "REPORT:" in out
    payload = out.split("REPORT:", 1)[1]
    report = json.loads(payload)
    assert report["summary"] == "merged"
    assert (tmp_path / "sandbox" / "jobs").is_dir()
```

- [ ] **Step 2: Run — verify FAIL**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL — `cannot import name 'amain' from 'clio.cli'`

- [ ] **Step 3: Implement**

```python
# src/clio/cli.py
"""Headless CLI demo: analyze a repo with visible event stream."""
import argparse
import asyncio
import json

from clio.config import Limits, get_limits
from clio.events import Event, EventBus, SseFormatter
from clio.llm import LLMMessage, MockLLM
from clio.orchestrator import Orchestrator
from clio.sandbox import Sandbox


def _mock_handler(limits: Limits):
    def handler(messages: list[LLMMessage], model: str | None) -> str:
        if model == limits.frontier_model:
            return json.dumps({"final": '{"summary": "merged", "modules": ["core"]}'})
        if len(messages) < 3:
            return json.dumps({"tool": "list_tree", "args": {}})
        return json.dumps({"final": '{"findings": ["mock finding"]}'})
    return handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clio", description="Analyze a git repository")
    parser.add_argument("url", help="https://github.com/... or file:// repo URL")
    parser.add_argument(
        "--provider", choices=["mock", "gemini"], default="mock",
        help="LLM provider (default: mock, no API key needed)",
    )
    parser.add_argument("--job-id", default=None, help="override the generated job id")
    return parser


async def amain(args: argparse.Namespace) -> int:
    limits = get_limits()
    bus = EventBus()
    bus.subscribe(lambda e: print(f"[{e.ts[11:19]}] {e.type} {json.dumps(e.data)[:160]}"))
    sandbox = Sandbox(root=limits.workspace_root, limits=limits)
    if args.provider == "gemini":
        from clio.llm import GeminiClient
        client = GeminiClient()
    else:
        client = MockLLM(handler=_mock_handler(limits))
    orchestrator = Orchestrator(sandbox, client, bus=bus, limits=limits)
    report = await orchestrator.run(args.url, root=sandbox.root, job_id=args.job_id)
    print("REPORT:")
    print(json.dumps(report.to_dict(), indent=2))
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(amain(build_parser().parse_args())))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run — 3 passed**

Run: `python -m pytest tests/test_cli.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/clio/cli.py tests/test_cli.py
git commit -m "feat: headless cli demo with live event stream"
```

---

## Full-suite verification

- [ ] Run `python -m pytest -q` — expected 82 passed total (36 existing − 4 old config + 7 new config + 5 events + 10 tools + 11 llm + 5 subagent + 6 scheduler + 3 orchestrator + 3 cli = 82). Offline machines lose the 2 network tests (clone bad-source, orchestrator failed-clone): expect 80 on those.
- [ ] Manual demo (no API key needed):

```bash
python -m clio.cli https://github.com/omhome16/Clio.git
```

Expected: a stream of `[time] job.*` and `[time] subagent.*` lines ending in `REPORT:` + JSON with 4 aspects and `"summary": "merged"`; `sandbox/jobs/clio-*.report.json` written; `sandbox/<job_id>/` holds a shallow clone.

## Self-review notes

- Every task ends with an independently testable deliverable; interfaces cross-checked (registry.workspace property exists for tests, `ToolRegistry(sandbox, job_id, limits=...)` signature matches orchestrator usage; `fan_out` item keys are `SubagentSpec` dataclasses and results dict is keyed by the same spec instances).
- Mock-based tests keep CI deterministic; Gemini path is real but opt-in via `--provider gemini` and never imported in tests.
- The orchestrator re-emits `EVENT_SUBAGENT_DONE` with `ok=False` for failed subagent runs (the subagent itself only emits on completion); this keeps the event stream complete without duplicating success events.
- Known caveat: `_grep` walks the whole workspace (fine for M1 scale; M2 adds the code graph).
- The `test_cli_end_to_end_mock` runs the true pipeline (clone → 4 subagents → synth) in mock mode; it is the smoke test for the whole milestone.
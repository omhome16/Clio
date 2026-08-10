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

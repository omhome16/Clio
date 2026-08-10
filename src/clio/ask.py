# src/clio/ask.py
"""Agentic Ask panel: per-job chat sessions that answer questions about an
analyzed repo by calling sandboxed tools (files, graph, impact, archive)."""
from __future__ import annotations

import json
from pathlib import Path

from clio.config import Limits, get_limits
from clio.events import EVENT_ASK_FINAL, EVENT_ASK_TOOL, Event, EventBus
from clio.impact import impact_of_symbol
from clio.llm import LLMClient, LLMMessage
from clio.reports import ReportArchive
from clio.sandbox import Sandbox
from clio.subagent import Subagent, SubagentSpec
from clio.tools import BUILTIN_TOOLS, Tool, ToolRegistry, ToolResult

ASK_SYSTEM_PROMPT = (
    "You are Clio's repository analyst. Answer the user's question about the "
    "analyzed repository using the available tools; cite module names and "
    "symbol ids when relevant. When you have the answer, reply with the JSON "
    '{"final": "your answer"}. Be concise.'
)


def _make_chat_tools(job_id: str) -> tuple[Tool, ...]:
    """Archive/graph tools bound to one job. The sandbox root is derived from
    the workspace the registry passes in (workspace.parent)."""

    def graph_query(args: dict, workspace: Path) -> str:
        store = ReportArchive(workspace.parent).graph_store(job_id)
        kind = args.get("kind", "")
        if kind == "callers_of":
            return json.dumps(store.callers_of(args["symbol_id"]))
        if kind == "callees_of":
            return json.dumps(store.callees_of(args["symbol_id"]))
        if kind == "modules_importing":
            return json.dumps(store.modules_importing(args["module"]))
        if kind == "module_imports":
            return json.dumps(store.module_imports(args["module"]))
        if kind == "has_symbol":
            return json.dumps(store.has_symbol(args["symbol_id"]))
        raise ValueError(f"unknown graph_query kind '{kind}'")

    def impact(args: dict, workspace: Path) -> str:
        report = impact_of_symbol(
            ReportArchive(workspace.parent), job_id, args["symbol_id"]
        )
        return json.dumps(report.to_dict())

    def list_jobs(args: dict, workspace: Path) -> str:
        jobs = [
            {
                "job_id": r["job_id"],
                "summary": r.get("summary", ""),
                "created_at": r.get("created_at", ""),
            }
            for r in ReportArchive(workspace.parent).list_reports()
        ]
        return json.dumps(jobs)

    def get_report(args: dict, workspace: Path) -> str:
        report = ReportArchive(workspace.parent).get_report(args["job_id"])
        if report is None:
            return f"(no report for {args['job_id']})"
        return json.dumps(report)

    return (
        Tool(
            name="graph_query",
            description=(
                "Query the code graph. kind: callers_of(symbol_id), "
                "callees_of(symbol_id), modules_importing(module), "
                "module_imports(module), has_symbol(symbol_id)"
            ),
            handler=graph_query,
        ),
        Tool(name="impact", description="Impact analysis of a symbol (module::name)", handler=impact),
        Tool(name="list_jobs", description="List persisted analysis jobs", handler=list_jobs),
        Tool(name="get_report", description="Get a persisted report by job id", handler=get_report),
    )


class _InstrumentedRegistry:
    """Wraps a ToolRegistry, publishing an ask.tool event per executed tool."""

    def __init__(self, registry: ToolRegistry, bus: EventBus, job_id: str) -> None:
        self._registry = registry
        self._bus = bus
        self._job_id = job_id

    def names(self) -> list[str]:
        return self._registry.names()

    async def execute(self, name: str, args: dict) -> ToolResult:
        result = await self._registry.execute(name, args)
        self._bus.publish(Event(
            type=EVENT_ASK_TOOL, job_id=self._job_id,
            data={
                "tool": name, "args": args, "ok": result.ok,
                "result": result.content[:400], "error": result.error,
            },
        ))
        return result


class AskSession:
    """Persistent chat context for one job; each question runs one Subagent
    turn with prior conversation carried in the task text."""

    def __init__(
        self,
        job_id: str,
        root: Path | str,
        client: LLMClient,
        limits: Limits | None = None,
    ) -> None:
        self.job_id = job_id
        self.root = Path(root)
        self._client = client
        self._limits = limits or get_limits()
        self.history: list[LLMMessage] = []

    def _task_text(self, question: str) -> str:
        if not self.history:
            return f"Question: {question}"
        prior = "\n".join(f"{m.role}: {m.content}" for m in self.history[-6:])
        return f"[Prior conversation]\n{prior}\n\nQuestion: {question}"

    def _registry(self) -> ToolRegistry:
        sandbox = Sandbox(root=self.root, limits=self._limits)
        tools = (*BUILTIN_TOOLS, *_make_chat_tools(self.job_id))
        return ToolRegistry(sandbox, self.job_id, tools=tools, limits=self._limits)

    async def run_turn(self, question: str, bus: EventBus | None = None) -> dict:
        registry = self._registry()
        sub_registry = (
            _InstrumentedRegistry(registry, bus, self.job_id) if bus is not None else registry
        )
        spec = SubagentSpec(
            name="ask", role="chat analyst",
            system_prompt=ASK_SYSTEM_PROMPT, tools=sub_registry.names(),
        )
        subagent = Subagent(
            spec, self._client, sub_registry, bus=bus, job_id=self.job_id,
            model=self._limits.cheap_model,
        )
        report = await subagent.run(self._task_text(question))
        self.history.append(LLMMessage(role="user", content=question))
        self.history.append(LLMMessage(role="assistant", content=report.content))
        if bus is not None:
            bus.publish(Event(
                type=EVENT_ASK_FINAL, job_id=self.job_id,
                data={
                    "answer": report.content, "ok": report.ok,
                    "steps": report.steps, "tool_calls": report.tool_calls,
                },
            ))
        return {
            "answer": report.content, "ok": report.ok,
            "steps": report.steps, "tool_calls": report.tool_calls,
        }

# tests/test_ask.py
import json

import pytest

from clio.ask import AskSession, _make_chat_tools
from clio.events import Event, EventBus
from clio.llm import LLMMessage
from clio.sandbox import Sandbox
from clio.tools import BUILTIN_TOOLS, ToolRegistry


class ScriptedClient:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[list[LLMMessage]] = []

    async def complete(self, messages, **kwargs):
        self.calls.append(list(messages))
        return self._responses.pop(0)


def _registry(tmp_path, job_id="job-1"):
    tools = (*BUILTIN_TOOLS, *_make_chat_tools(job_id))
    return ToolRegistry(Sandbox(root=tmp_path / "sandbox"), job_id, tools=tools)


async def test_graph_callers_and_callees(tmp_path, seed_job):
    seed_job(tmp_path / "sandbox", "job-1", "2026-08-10T00:00:00+00:00", {
        "a.py": "from b import f\n\ndef run():\n    return f()\n",
        "b.py": "def f():\n    return 1\n",
    })
    reg = _registry(tmp_path)
    out = await reg.execute("graph_query", {"kind": "callers_of", "symbol_id": "f"})
    assert out.ok
    assert json.loads(out.content) == [["a::run", 4]]
    out = await reg.execute("graph_query", {"kind": "callees_of", "symbol_id": "a::run"})
    assert out.ok
    assert json.loads(out.content) == [["f", 4]]


async def test_graph_module_queries(tmp_path, seed_job):
    seed_job(tmp_path / "sandbox", "job-1", "2026-08-10T00:00:00+00:00", {
        "a.py": "from b import f\n\ndef run():\n    return f()\n",
        "b.py": "def f():\n    return 1\n",
    })
    reg = _registry(tmp_path)
    out = await reg.execute("graph_query", {"kind": "modules_importing", "module": "b"})
    assert out.ok and json.loads(out.content) == ["a"]
    out = await reg.execute("graph_query", {"kind": "module_imports", "module": "a"})
    assert out.ok and json.loads(out.content) == ["b.f"]
    out = await reg.execute("graph_query", {"kind": "has_symbol", "symbol_id": "b::f"})
    assert out.ok and json.loads(out.content) is True
    out = await reg.execute("graph_query", {"kind": "has_symbol", "symbol_id": "nope"})
    assert out.ok and json.loads(out.content) is False


async def test_graph_query_unknown_kind(tmp_path, seed_job):
    seed_job(tmp_path / "sandbox", "job-1", "2026-08-10T00:00:00+00:00",
             {"a.py": "def f():\n    return 1\n"})
    reg = _registry(tmp_path)
    out = await reg.execute("graph_query", {"kind": "bogus"})
    assert not out.ok
    assert "unknown graph_query kind" in out.error


async def test_impact_tool_shape(tmp_path, seed_job):
    seed_job(tmp_path / "sandbox", "job-1", "2026-08-10T00:00:00+00:00",
             {"a.py": "def f():\n    return 1\n"})
    reg = _registry(tmp_path)
    out = await reg.execute("impact", {"symbol_id": "a::f"})
    assert out.ok
    report = json.loads(out.content)
    assert report["scope"] == "a::f"
    assert report["verdict"] == "contained"


async def test_archive_tools(tmp_path, seed_job):
    seed_job(tmp_path / "sandbox", "job-1", "2026-08-10T00:00:00+00:00",
             {"a.py": "def f():\n    return 1\n"})
    reg = _registry(tmp_path)
    out = await reg.execute("list_jobs", {})
    assert out.ok
    assert json.loads(out.content) == [{
        "job_id": "job-1", "summary": "merged",
        "created_at": "2026-08-10T00:00:00+00:00",
    }]
    out = await reg.execute("get_report", {"job_id": "job-1"})
    assert out.ok and json.loads(out.content)["job_id"] == "job-1"
    out = await reg.execute("get_report", {"job_id": "nope"})
    assert out.ok and "no report" in out.content


async def test_chat_registry_sandbox_guards(tmp_path):
    reg = _registry(tmp_path)
    out = await reg.execute("read_file", {"path": "../../etc/passwd"})
    assert not out.ok
    assert "escapes" in out.error
    out = await reg.execute("list_tree", {})
    assert out.ok and "(empty)" in out.content


async def test_ask_session_tool_to_final(tmp_path):
    client = ScriptedClient([
        json.dumps({"tool": "graph_query", "args": {"kind": "has_symbol", "symbol_id": "a::f"}}),
        json.dumps({"final": "yes"}),
    ])
    session = AskSession("job-1", tmp_path / "sandbox", client)
    out = await session.run_turn("does a::f exist?")
    assert out["ok"] and out["answer"] == "yes"
    assert out["tool_calls"] == 1
    assert [(m.role, m.content) for m in session.history] == [
        ("user", "does a::f exist?"),
        ("assistant", "yes"),
    ]


async def test_ask_session_history_carries_prior_turns(tmp_path):
    client = ScriptedClient([json.dumps({"final": "one"}), json.dumps({"final": "two"})])
    session = AskSession("job-1", tmp_path / "sandbox", client)
    await session.run_turn("q1")
    await session.run_turn("q2")
    task2 = client.calls[1][-1].content
    assert "[Prior conversation]" in task2
    assert "q1" in task2 and "one" in task2


async def test_ask_session_publishes_tool_and_final_events(tmp_path):
    client = ScriptedClient([
        json.dumps({"tool": "impact", "args": {"symbol_id": "x"}}),
        json.dumps({"final": "ok"}),
    ])
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    session = AskSession("job-1", tmp_path / "sandbox", client)
    await session.run_turn("question", bus=bus)
    types = [e.type for e in seen]
    assert "ask.tool" in types and "ask.final" in types
    tool = next(e for e in seen if e.type == "ask.tool")
    assert tool.data["tool"] == "impact"
    assert tool.data["ok"] is False  # missing graph db -> tool error, still streamed
    final = next(e for e in seen if e.type == "ask.final")
    assert final.data["answer"] == "ok" and final.data["ok"] is True

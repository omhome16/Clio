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

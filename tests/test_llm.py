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

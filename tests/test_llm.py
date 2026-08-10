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


# --- HTTP plumbing and Gemini over urllib (M7) ---
import io
import json
import urllib.error

from clio.config import get_limits
from clio.llm import GeminiClient, LLMError, LLMMessage, _post_json, mock_handler


def test_post_json_sends_expected_request(monkeypatch):
    captured = {}

    class FakeResp:
        def __init__(self, body: bytes):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self._body

    def fake_urlopen(req, timeout=60):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        captured["body"] = req.data
        captured["content_type"] = req.headers["Content-Type"]
        return FakeResp(b'{"ok": true}')

    monkeypatch.setattr("clio.llm.urllib.request.urlopen", fake_urlopen)
    result = _post_json("https://api.test/v1/x", {"a": 1})
    assert result == {"ok": True}
    assert captured["url"] == "https://api.test/v1/x"
    assert captured["method"] == "POST"
    assert captured["body"] == b'{"a": 1}'
    assert captured["content_type"] == "application/json"


def test_post_json_http_error_raises_llm_error(monkeypatch):
    def boom(req, timeout=60):
        raise urllib.error.HTTPError(
            "https://api.test/v1/x", 429, "Too Many Requests", None,
            io.BytesIO(b"rate limited"),
        )

    monkeypatch.setattr("clio.llm.urllib.request.urlopen", boom)
    with pytest.raises(LLMError, match="429"):
        _post_json("https://api.test/v1/x", {})


def test_post_json_network_error_raises_llm_error(monkeypatch):
    def boom(req, timeout=60):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr("clio.llm.urllib.request.urlopen", boom)
    with pytest.raises(LLMError, match="boom"):
        _post_json("https://api.test/v1/x", {})


async def test_gemini_builds_expected_request(monkeypatch):
    captured = {}

    def fake_post(url, payload, timeout=60):
        captured["url"] = url
        captured["payload"] = payload
        return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    monkeypatch.setattr("clio.llm._post_json", fake_post)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    client = GeminiClient()
    out = await client.complete(
        [LLMMessage("user", "hello"), LLMMessage("model", "hi")], max_tokens=42
    )
    assert out == "ok"
    assert captured["payload"]["contents"] == [
        {"role": "user", "parts": [{"text": "hello"}]},
        {"role": "model", "parts": [{"text": "hi"}]},
    ]
    assert captured["payload"]["generationConfig"] == {"maxOutputTokens": 42}
    assert captured["url"].startswith(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=test-key"
    )


async def test_gemini_joins_multi_part_text(monkeypatch):
    def fake_post(url, payload, timeout=60):
        return {"candidates": [{"content": {"parts": [{"text": "a "}, {"text": "b"}]}}]}

    monkeypatch.setattr("clio.llm._post_json", fake_post)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert await GeminiClient().complete([LLMMessage("user", "x")]) == "a b"


def test_mock_handler_scripted():
    handler = mock_handler(get_limits())
    out = handler(
        [LLMMessage("user", "a"), LLMMessage("model", "b"), LLMMessage("user", "c")],
        "cheap",
    )
    assert json.loads(out) == {"final": '{"findings": ["mock finding"]}'}

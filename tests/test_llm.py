import pytest

from clio.llm import (
    GeminiClient, LLMError, LLMMessage, FakeLLM, ToolCall, parse_reply,
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


async def test_fake_llm_pops_scripted():
    fake = FakeLLM(responses=["one", "two"])
    out1 = await fake.complete([LLMMessage(role="user", content="hi")], model="m")
    out2 = await fake.complete([LLMMessage(role="user", content="hi")], model="m")
    assert (out1, out2) == ("one", "two")
    assert len(fake.calls) == 2


async def test_fake_llm_exhausted_raises():
    fake = FakeLLM(responses=[])
    with pytest.raises(LLMError):
        await fake.complete([LLMMessage(role="user", content="hi")])


async def test_fake_llm_handler_mode():
    fake = FakeLLM(handler=lambda messages, model: f"handled-{model}")
    out = await fake.complete([LLMMessage(role="user", content="hi")], model="cheap")
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
from clio.llm import FakeLLM, GeminiClient, LLMError, LLMMessage, _post_json, fake_handler


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
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=test-key"
    )


async def test_gemini_joins_multi_part_text(monkeypatch):
    def fake_post(url, payload, timeout=60):
        return {"candidates": [{"content": {"parts": [{"text": "a "}, {"text": "b"}]}}]}

    monkeypatch.setattr("clio.llm._post_json", fake_post)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert await GeminiClient().complete([LLMMessage("user", "x")]) == "a b"


def test_fake_handler_scripted():
    handler = fake_handler(get_limits())
    out = handler(
        [LLMMessage("user", "a"), LLMMessage("model", "b"), LLMMessage("user", "c")],
        "cheap",
    )
    assert out == "fake guide text"
# --- Groq provider + client factory (M7) ---
from clio.llm import GroqClient, make_client


async def test_groq_builds_expected_request(monkeypatch):
    captured = {}

    def fake_post(url, payload, timeout=60):
        captured["url"] = url
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "sure"}}]}

    monkeypatch.setattr("clio.llm._post_json", fake_post)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    client = GroqClient()
    out = await client.complete(
        [LLMMessage("user", "hi")], model="llama-3.3-70b-versatile", max_tokens=7
    )
    assert out == "sure"
    assert captured["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert captured["payload"] == {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 7,
    }


async def test_groq_default_model(monkeypatch):
    captured = {}

    def fake_post(url, payload, timeout=60):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "x"}}]}

    monkeypatch.setattr("clio.llm._post_json", fake_post)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    await GroqClient().complete([LLMMessage("user", "hi")])
    assert captured["payload"]["model"] == "llama-3.3-70b-versatile"


def test_groq_requires_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMError):
        GroqClient(api_key=None)


def test_make_client_unknown_raises(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMError):
        make_client("wat")


def test_make_client_gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert isinstance(make_client("gemini"), GeminiClient)


def test_make_client_groq(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    assert isinstance(make_client("groq"), GroqClient)


def test_make_client_groq_without_key_raises(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMError):
        make_client("groq")


# --- Ollama provider (local server, OpenAI-compatible endpoint) ---
from clio.llm import OllamaClient


async def test_ollama_builds_expected_request(monkeypatch):
    captured = {}

    def fake_post(url, payload, timeout=60):
        captured["url"] = url
        captured["payload"] = payload
        captured["timeout"] = timeout
        return {"choices": [{"message": {"content": "sure"}}]}

    monkeypatch.setattr("clio.llm._post_json", fake_post)
    client = OllamaClient(base_url="http://localhost:11434/v1", timeout=900)
    out = await client.complete(
        [LLMMessage("user", "hi")], model="qwen2.5-coder:7b", max_tokens=7
    )
    assert out == "sure"
    assert captured["url"] == "http://localhost:11434/v1/chat/completions"
    assert captured["payload"] == {
        "model": "qwen2.5-coder:7b",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 7,
    }


async def test_ollama_default_model(monkeypatch):
    captured = {}

    def fake_post(url, payload, timeout=60):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "x"}}]}

    monkeypatch.setattr("clio.llm._post_json", fake_post)
    await OllamaClient().complete([LLMMessage("user", "hi")])
    assert captured["payload"]["model"] == "qwen2.5-coder:7b"


async def test_ollama_generous_timeout(monkeypatch):
    captured = {}

    def fake_post(url, payload, timeout=60):
        captured["timeout"] = timeout
        return {"choices": [{"message": {"content": "x"}}]}

    monkeypatch.setattr("clio.llm._post_json", fake_post)
    await OllamaClient().complete([LLMMessage("user", "hi")])
    assert captured["timeout"] == 900


def test_ollama_needs_no_key():
    client = OllamaClient()
    assert client is not None


def test_make_client_ollama(monkeypatch):
    assert isinstance(make_client("ollama"), OllamaClient)


# --- Rate limiting + 429-aware retry (Phase 0) ---
from clio.llm import (
    RateLimitError, ServerError, NetworkError, RateLimiter, is_retryable,
    parse_retry_after,
)


def test_parse_retry_after_header_wins():
    assert parse_retry_after("32", "retry in 5s") == 32.0


def test_parse_retry_after_body_fallback():
    body = "Quota exceeded for metric: x, limit: 5. Please retry in 32.345s."
    assert parse_retry_after(None, body) == 32.345


def test_parse_retry_after_none_when_absent():
    assert parse_retry_after(None, "all good") is None


def test_is_retryable_classifies():
    assert is_retryable(RateLimitError("quota"))
    assert is_retryable(ServerError("500"))
    assert is_retryable(NetworkError("dns"))
    assert not is_retryable(LLMError("bad request 400"))


def test_post_json_429_raises_rate_limit_error(monkeypatch):
    def boom(req, timeout=60):
        raise urllib.error.HTTPError(
            "https://api.test/v1/x", 429, "Too Many Requests", None,
            io.BytesIO(b"Please retry in 12.5s."),
        )

    monkeypatch.setattr("clio.llm.urllib.request.urlopen", boom)
    with pytest.raises(RateLimitError) as ei:
        _post_json("https://api.test/v1/x", {})
    assert ei.value.retry_after == 12.5


def test_post_json_500_raises_server_error(monkeypatch):
    def boom(req, timeout=60):
        raise urllib.error.HTTPError(
            "https://api.test/v1/x", 503, "Unavailable", None, io.BytesIO(b"down"),
        )

    monkeypatch.setattr("clio.llm.urllib.request.urlopen", boom)
    with pytest.raises(ServerError):
        _post_json("https://api.test/v1/x", {})


def test_post_json_400_raises_plain_llm_error(monkeypatch):
    def boom(req, timeout=60):
        raise urllib.error.HTTPError(
            "https://api.test/v1/x", 400, "Bad Request", None, io.BytesIO(b"nope"),
        )

    monkeypatch.setattr("clio.llm.urllib.request.urlopen", boom)
    with pytest.raises(LLMError):
        _post_json("https://api.test/v1/x", {})


def test_rate_limiter_spacing():
    limiter = RateLimiter(rpm=120)  # 0.5s per slot
    assert limiter._interval == pytest.approx(0.5)


async def test_retry_waits_rate_limit_hint(monkeypatch):
    from clio.llm import _complete_with_retry

    calls = {"n": 0}

    def fake_post(url, payload, timeout=60):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RateLimitError("retry in 0.01s", retry_after=0.01)
        return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    monkeypatch.setattr("clio.llm._post_json", fake_post)
    out = await _complete_with_retry(
        "https://api.test/v1/x", {},
        extract=lambda d: "".join(
            p.get("text", "") for p in d["candidates"][0]["content"]["parts"]
        ),
        limiter=None, max_retries=2,
    )
    assert out == "ok"
    assert calls["n"] == 2


async def test_retry_exhausted_raises(monkeypatch):
    from clio.llm import _complete_with_retry

    def fake_post(url, payload, timeout=60):
        raise RateLimitError("retry in 0.01s", retry_after=0.01)

    monkeypatch.setattr("clio.llm._post_json", fake_post)
    with pytest.raises(RateLimitError):
        await _complete_with_retry(
            "https://api.test/v1/x", {},
            extract=lambda d: "x", limiter=None, max_retries=2,
        )


async def test_retry_non_retryable_immediate(monkeypatch):
    from clio.llm import _complete_with_retry

    calls = {"n": 0}

    def fake_post(url, payload, timeout=60):
        calls["n"] += 1
        raise LLMError("bad request 400")

    monkeypatch.setattr("clio.llm._post_json", fake_post)
    with pytest.raises(LLMError):
        await _complete_with_retry(
            "https://api.test/v1/x", {},
            extract=lambda d: "x", limiter=None, max_retries=2,
        )
    assert calls["n"] == 1  # no retry on permanent errors

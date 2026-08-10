"""Model-agnostic LLM client interface, mock, and Gemini implementation."""
import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from clio.config import Limits, get_limits, load_env


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


def _post_json(url: str, payload: dict, timeout: int = 60) -> dict:
    """POST a JSON payload and return the parsed JSON response.

    Synchronous by design; async callers wrap it in ``asyncio.to_thread``.
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.headers["Content-Type"] = "application/json"
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")[:500]
        raise LLMError(f"LLM API error {err.code} from {url}: {body}") from err
    except urllib.error.URLError as err:
        raise LLMError(f"LLM API request to {url} failed: {err.reason}") from err


def mock_handler(limits: Limits):
    def handler(messages: list[LLMMessage], model: str | None) -> str:
        if model == limits.frontier_model:
            return json.dumps({"final": '{"summary": "merged", "modules": ["core"]}'})
        if len(messages) < 3:
            return json.dumps({"tool": "list_tree", "args": {}})
        return json.dumps({"final": '{"findings": ["mock finding"]}'})
    return handler


class GeminiClient:
    """Gemini REST client using only the stdlib (``urllib``)."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        load_env()
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self._api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        self._base_url = base_url or "https://generativelanguage.googleapis.com/v1beta"

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        max_tokens: int = 2000,
    ) -> str:
        model = model or "gemini-2.0-flash"
        payload = {
            "contents": [
                {"role": "user" if m.role == "user" else "model",
                 "parts": [{"text": m.content}]}
                for m in messages
            ],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        url = f"{self._base_url}/models/{model}:generateContent?key={self._api_key}"
        data = await asyncio.to_thread(_post_json, url, payload)
        return "".join(
            part.get("text", "")
            for part in data["candidates"][0]["content"]["parts"]
        )


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

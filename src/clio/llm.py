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

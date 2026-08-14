"""Model-agnostic LLM client interface, Gemini, and Groq implementations.

``FakeLLM`` is a scriptable offline stub used only by tests — it is not a
provider and cannot be selected from the product.
"""
import asyncio
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from clio.config import Limits, get_limits, load_env


class LLMError(RuntimeError):
    pass


class RateLimitError(LLMError):
    """HTTP 429 / quota exhaustion. ``retry_after`` is the suggested wait in
    seconds (parsed from the provider body or Retry-After header)."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ServerError(LLMError):
    """HTTP 5xx — transient backend failures; safe to retry."""


class NetworkError(LLMError):
    """Connection-level failures (timeouts, DNS, refused) — safe to retry."""


def is_retryable(exc: BaseException) -> bool:
    """True for transient failures (quota, 5xx, network) that warrant a retry."""
    return isinstance(exc, (RateLimitError, ServerError, NetworkError, TimeoutError))


_RETRY_IN_BODY = re.compile(r"retry\s+in\s+(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


def parse_retry_after(header: str | None, body: str) -> float | None:
    """Best-effort retry delay: Retry-After header first, then `retry in Xs`
    from the response body."""
    if header:
        try:
            return max(float(header), 0.0)
        except ValueError:
            pass  # rare HTTP-date form; fall through to body parsing
    match = _RETRY_IN_BODY.search(body)
    if match:
        return float(match.group(1))
    return None


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


class FakeLLM:
    """Test-only offline stub: serves scripted responses or a handler.

    Never reachable from the product (see ``make_client``); exists so tests
    exercise the harness without network access.
    """
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


def _redact(url: str) -> str:
    """Strip ``?key=...`` from a URL before it hits a log or error message."""
    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    kept = [p for p in query.split("&") if not p.startswith("key=")]
    return base + ("?" + "&".join(kept) if kept else "")


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
    req.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )  # some providers (Groq) WAF-block the default python-urllib UA
    safe_url = _redact(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")[:500]
        retry_after = parse_retry_after(
            getattr(err, "headers", None).get("Retry-After") if getattr(err, "headers", None) else None,
            body,
        )
        if err.code == 429:
            raise RateLimitError(
                f"LLM API error {err.code} from {safe_url}: {body}",
                retry_after=retry_after,
            ) from err
        if 500 <= err.code < 600:
            raise ServerError(
                f"LLM API error {err.code} from {safe_url}: {body}"
            ) from err
        raise LLMError(f"LLM API error {err.code} from {safe_url}: {body}") from err
    except urllib.error.URLError as err:
        raise NetworkError(f"LLM API request to {safe_url} failed: {err.reason}") from err
    except TimeoutError as err:
        raise NetworkError(f"LLM API request to {safe_url} timed out") from err


def fake_handler(limits: Limits):
    def handler(messages: list[LLMMessage], model: str | None) -> str:
        return "fake guide text"
    return handler


class RateLimiter:
    """Thread-safe token bucket: at most one request per `60/rpm` seconds.

    Shared process-wide per provider so parallel jobs, subagents, and Ask
    sessions can never burst past the free-tier budget. Works across threads
    because web.py runs each job in its own asyncio loop.
    """

    def __init__(self, rpm: int = 5) -> None:
        self._interval = 60.0 / max(1, rpm)
        self._lock = threading.Lock()
        self._next_slot = 0.0

    async def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                wait = self._next_slot - now
                if wait <= 0:
                    self._next_slot = now + self._interval
                    return
            await asyncio.sleep(min(wait, 2.0))

    def reset(self) -> None:
        with self._lock:
            self._next_slot = 0.0


_global_limiters: dict[str, RateLimiter] = {}
_global_lock = threading.Lock()


def get_global_limiter(provider: str, limits: Limits | None = None) -> RateLimiter | None:
    """Process-wide limiter per provider, or None when rate limiting is off."""
    limits = limits or get_limits()
    if not limits.rate_limit:
        return None
    with _global_lock:
        limiter = _global_limiters.get(provider)
        if limiter is None:
            limiter = RateLimiter(limits.rpm)
            _global_limiters[provider] = limiter
        return limiter


async def _complete_with_retry(
    url: str,
    payload: dict,
    *,
    extract: Callable[[dict], str],
    limiter: RateLimiter | None,
    max_retries: int,
    timeout: int = 60,
) -> str:
    """POST with a shared rate limiter and retry ladder.

    Retryable failures (429 with its suggested wait, 5xx, network) are retried
    up to ``max_retries`` times, honouring the provider's ``retry in Xs`` hint.
    Permanent 4xx errors surface immediately.
    """
    last: BaseException | None = None
    for attempt in range(max_retries + 1):
        if limiter is not None:
            await limiter.acquire()
        try:
            data = await asyncio.to_thread(_post_json, url, payload, timeout)
            return extract(data)
        except RateLimitError as exc:
            last = exc
            await asyncio.sleep(exc.retry_after or min(2.0 ** attempt, 8.0))
        except (ServerError, NetworkError, TimeoutError) as exc:
            last = exc
            await asyncio.sleep(min(2.0 ** attempt, 8.0))
    assert last is not None
    raise last


class GeminiClient:
    """Gemini REST client using only the stdlib (``urllib``)."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        load_env()
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self._api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        self._base_url = base_url or "https://generativelanguage.googleapis.com/v1beta"
        limits = get_limits()
        self._limiter = get_global_limiter("gemini", limits)
        self._max_retries = limits.max_retries

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        max_tokens: int = 2000,
    ) -> str:
        model = model or "gemini-2.5-flash"
        payload = {
            "contents": [
                {"role": "user" if m.role == "user" else "model",
                 "parts": [{"text": m.content}]}
                for m in messages
            ],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        url = f"{self._base_url}/models/{model}:generateContent?key={self._api_key}"

        def extract(data: dict) -> str:
            return "".join(
                part.get("text", "")
                for part in data["candidates"][0]["content"]["parts"]
            )

        return await _complete_with_retry(
            url, payload, extract=extract,
            limiter=self._limiter, max_retries=self._max_retries,
        )


class GroqClient:
    """OpenAI-compatible client for Groq's API (default: llama-3.3-70b-versatile)."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.groq.com/openai/v1",
    ):
        load_env()
        self._api_key = api_key or os.environ.get("GROQ_API_KEY")
        if not self._api_key:
            raise LLMError("GROQ_API_KEY is not set")
        self._base_url = base_url.rstrip("/")
        limits = get_limits()
        self._limiter = get_global_limiter("groq", limits)
        self._max_retries = limits.max_retries

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        max_tokens: int = 2000,
    ) -> str:
        model = model or "llama-3.3-70b-versatile"
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
        }
        url = f"{self._base_url}/chat/completions"

        def extract(data: dict) -> str:
            return data["choices"][0]["message"]["content"]

        return await _complete_with_retry(
            url, payload, extract=extract,
            limiter=self._limiter, max_retries=self._max_retries,
        )


class OllamaClient:
    """Client for a local Ollama server via its OpenAI-compatible endpoint.

    No API key and no rate limiting (the model runs on your own machine), but
    requests get a generous timeout: a 7B coder model offloaded to CPU can
    take minutes to finish a long completion.
    """

    def __init__(self, base_url: str | None = None, timeout: int = 900):
        load_env()
        self._base_url = (
            base_url or os.environ.get("CLIO_OLLAMA_BASE_URL", "http://localhost:11434/v1")
        ).rstrip("/")
        self._timeout = timeout
        limits = get_limits()
        self._limiter = None  # local server: no quota to pace
        self._max_retries = limits.max_retries

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        max_tokens: int = 2000,
    ) -> str:
        model = model or "qwen2.5-coder:7b"
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
        }
        url = f"{self._base_url}/chat/completions"

        def extract(data: dict) -> str:
            return data["choices"][0]["message"]["content"]

        return await _complete_with_retry(
            url, payload, extract=extract,
            limiter=None, max_retries=self._max_retries, timeout=self._timeout,
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


def make_client(provider: str, limits: Limits | None = None) -> LLMClient:
    """Build the client for ``provider`` (gemini | groq | ollama). Unknown
    provider names raise ``LLMError`` so misconfiguration fails loudly."""
    load_env()
    if provider == "gemini":
        return GeminiClient()
    if provider == "groq":
        return GroqClient()
    if provider == "ollama":
        return OllamaClient()
    raise LLMError(
        f"unknown provider {provider!r} (choose from: gemini, groq, ollama). "
        "Set CLIO_PROVIDER in .env"
    )

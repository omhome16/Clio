# M7 — LLM providers (`.env` config, urllib Gemini, Groq client)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `httpx`/Gemini SDK dependency with a stdlib `urllib` HTTP layer and add a second real provider (Groq), configured via `.env` / `CLIO_PROVIDER`, so the CLI and dashboard can run real analyses without any third-party packages.

**Architecture:** `config.py` gains a `load_env()` dotenv loader (`.env` never overrides existing env vars) and `get_provider()`. `llm.py` gains a synchronous `_post_json(url, payload)` helper invoked from async via `asyncio.to_thread`, a rewritten urllib-based `GeminiClient` (drop `httpx`), a new `GroqClient`, and a `make_client(provider, limits)` factory. `cli.py` and `web.py` build their client through `make_client`; the mock handler moves to `llm.py`.

**Tech Stack:** Python 3.11 stdlib only (`urllib.request`, `asyncio.to_thread`, `os`, `pathlib`). No new dependencies. No `httpx` remains.

## Global Constraints

- Python 3.11+; stdlib-only for runtime deps (zero new dependencies)
- Remove `httpx` from `pyproject.toml` (the old `GeminiClient` lazy-imports it); no other deps change
- Offline-friendly: default provider stays `mock` (no API key), all tests pass with no network and no keys
- Provider env contract: `CLIO_PROVIDER` ∈ `mock|gemini|groq`; `GEMINI_API_KEY`; `GROQ_API_KEY`; `CLIO_ENV_FILE` (default `.env` in CWD)
- TDD: failing test first, minimal implementation, full suite green after each task
- One commit per task; `git commit -m "<type>(<scope>): <summary>"` matching repo style
- Tests run from the repo root: `python -m pytest` (or `pytest`)
- Spec: `docs/superpowers/specs/2026-08-10-m7-m9-interactive-dashboard-design.md` (commit `92b672e`)

## What M7 delivers

1. **`.env` loading** — `config.load_env()` reads `$CLIO_ENV_FILE` (default `./.env`), applying `KEY=VALUE` lines without overriding already-set env vars; `config.get_provider()` returns `CLIO_PROVIDER` or `"mock"`.
2. **stdlib HTTP layer** — `llm._post_json(url, payload)` sync helper raising `LLMError` on HTTP/network failures; async clients wrap it in `asyncio.to_thread`.
3. **Gemini via urllib** — `GeminiClient` rewritten on the raw `generateContent` REST endpoint; same public interface; `httpx` deleted from dependencies.
4. **Groq provider** — `GroqClient` (OpenAI-compatible `/chat/completions`, default model `llama-3.3-70b-versatile`).
5. **Factory + wiring** — `make_client(provider, limits)`; CLI `--provider` accepts `groq` and defaults to `$CLIO_PROVIDER`; dashboard `run_job` uses `make_client(get_provider(), limits)`.
6. **`.env.example`** committed at repo root with all three provider variables.

## Design decisions

- **`load_env` semantics:** existing environment variables always win over `.env`; keys are stripped, quotes are stripped from values, `#` comments and blank lines skipped. `get_limits()` and `get_provider()` call `load_env()` so a `.env` supplies both limits and provider.
- **`_post_json` is synchronous** and called via `asyncio.to_thread` — matches the existing blocking-style clients (`MockLLM.complete` blocks on a handler) and keeps all providers behind the same `LLMClient` protocol.
- **Gemini request/response:** request uses `contents[{role, parts:[{text}]}]` + `generationConfig.maxOutputTokens`; response joins all `candidates[0].content.parts[].text`. Default model `gemini-2.0-flash` (same default the harness used before).
- **`mock_handler` moves** from `cli.py` to `llm.py` verbatim (name `mock_handler`) so `make_client` can build the default mock without importing `cli` (which would be circular). `cli.py` re-exports it as `_mock_handler` in M7 Task 2 so `web.py`'s existing import keeps working; M7 Task 4 removes the alias.
- **Unknown provider names fall back to mock** (safe default — no crash on a typo'd `CLIO_PROVIDER`).
- **`_post_json` needs the stdlib's `urllib.error`** for HTTPError/URLError mapping; `HTTPError` bodies are truncated to 500 chars in the `LLMError` message.
- **CLI default changes** from literal `"mock"` to `get_provider()` — evaluated at `build_parser()` call time, so `test_parser_defaults` (asserts `"mock"`) stays green with no env set.

## Contracts

- `clio.config.load_env() -> None` — loads `.env` (path from `CLIO_ENV_FILE`, default `.env` in CWD) into `os.environ`, never overriding existing vars
- `clio.config.get_provider() -> str` — `CLIO_PROVIDER` or `"mock"`
- `clio.llm._post_json(url: str, payload: dict, timeout: int = 60) -> dict` — sync POST; raises `LLMError` on `HTTPError`/`URLError`
- `clio.llm.GeminiClient(api_key: str | None = None, base_url: str | None = None)` — `complete(messages, *, model=None, max_tokens=2000)`, default model `"gemini-2.0-flash"`; raises `LLMError("GEMINI_API_KEY is not set")` when key missing
- `clio.llm.GroqClient(api_key: str | None = None, base_url: str = "https://api.groq.com/openai/v1")` — `complete(...)`, default model `"llama-3.3-70b-versatile"`; raises `LLMError("GROQ_API_KEY is not set")` when key missing
- `clio.llm.mock_handler(limits: Limits) -> Callable[[list[LLMMessage], str | None], str]` — the M1 scripted handler, moved verbatim
- `clio.llm.make_client(provider: str, limits: Limits | None = None) -> LLMClient` — `gemini`/`groq`/anything-else(`mock`); unknown names fall back to mock
- `LLMClient` protocol unchanged: `async complete(messages: list[LLMMessage], *, model: str | None = None, max_tokens: int = 2000) -> str`

---

## Task 1: `config.py` — `.env` loading + provider resolution

**Files:**
- Modify: `src/clio/config.py` (add `load_env`, `get_provider`; call `load_env()` at top of `get_limits`)
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Consumes: existing `get_limits()` internals (env lookups in `config.py`)
- Produces: `load_env()`, `get_provider()` — used by Tasks 2-4

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config.py`:

```python
# --- .env loading and provider resolution (M7) ---
import os

from clio.config import get_provider


def test_dotenv_applies_values(monkeypatch, tmp_path):
    env_file = tmp_path / "clio.env"
    env_file.write_text(
        "# local config\nCLIO_PROVIDER=groq\nGEMINI_API_KEY=secret-123\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLIO_ENV_FILE", str(env_file))
    monkeypatch.delenv("CLIO_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert get_provider() == "groq"
    assert os.environ["GEMINI_API_KEY"] == "secret-123"


def test_dotenv_does_not_override_existing(monkeypatch, tmp_path):
    env_file = tmp_path / "clio.env"
    env_file.write_text("CLIO_PROVIDER=groq\n", encoding="utf-8")
    monkeypatch.setenv("CLIO_ENV_FILE", str(env_file))
    monkeypatch.setenv("CLIO_PROVIDER", "gemini")
    assert get_provider() == "gemini"


def test_dotenv_supplies_limits(monkeypatch, tmp_path):
    env_file = tmp_path / "clio.env"
    env_file.write_text("CLIO_MAX_FILES=10\n", encoding="utf-8")
    monkeypatch.setenv("CLIO_ENV_FILE", str(env_file))
    monkeypatch.delenv("CLIO_MAX_FILES", raising=False)
    assert get_limits().max_files == 10


def test_get_provider_from_env(monkeypatch):
    monkeypatch.setenv("CLIO_PROVIDER", "groq")
    monkeypatch.delenv("CLIO_ENV_FILE", raising=False)
    assert get_provider() == "groq"


def test_get_provider_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("CLIO_PROVIDER", raising=False)
    monkeypatch.delenv("CLIO_ENV_FILE", raising=False)
    assert get_provider() == "mock"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_config.py -v`
Expected: 5 FAILs — `ImportError: cannot import name 'get_provider'` (and `load_env` missing); suite total 64 tests, 5 failed.

- [ ] **Step 3: Implement** — add to `src/clio/config.py` (imports already include `os` and `Path`):

```python
def load_env() -> None:
    """Load ``KEY=VALUE`` lines from ``$CLIO_ENV_FILE`` (default ``.env`` in the
    current directory) into ``os.environ`` without overriding existing variables."""
    path = Path(os.environ.get("CLIO_ENV_FILE", ".env"))
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_provider() -> str:
    """LLM provider name from ``CLIO_PROVIDER`` (default ``"mock"``)."""
    load_env()
    return os.environ.get("CLIO_PROVIDER", "mock")
```

And insert `load_env()` as the first statement of `get_limits()` (before the existing env lookups).

- [ ] **Step 4: Run the full suite to verify green**

Run: `python -m pytest -q`
Expected: 152 passed (147 + 5), 0 failed.

- [ ] **Step 5: Commit**

```bash
git add src/clio/config.py tests/test_config.py
git commit -m "feat(config): add .env loader and provider resolution"
```

---

## Task 2: `llm.py` — stdlib HTTP layer + urllib Gemini + `mock_handler` move

**Files:**
- Modify: `src/clio/llm.py` (add `asyncio`, `urllib.request`, `urllib.error` imports; add `from clio.config import Limits, get_limits, load_env`; add `_post_json`; rewrite `GeminiClient`; add `mock_handler`)
- Modify: `src/clio/cli.py` (replace the `_mock_handler` function with an alias import; keep `MockLLM` import for now)
- Test: `tests/test_llm.py` (append)

**Interfaces:**
- Consumes: Task 1 `load_env`; existing `LLMClient` protocol/`LLMMessage`/`MockLLM`
- Produces: `_post_json`, `GeminiClient` (new impl, same interface), `mock_handler(limits)`; `cli._mock_handler` still importable (needed by `web.py` until Task 4)

- [ ] **Step 1: Write the failing tests** — append to `tests/test_llm.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_llm.py -v`
Expected: 6 FAILs — `ImportError: cannot import name '_post_json'` / `'mock_handler'` (the old Gemini impl lazy-imports `httpx` and never calls `_post_json`, so the captured-request tests fail). Suite total 75 tests, 6 failed. Note: `test_gemini_requires_key` (existing) must still PASS — the new `GeminiClient` constructor is the only key check.

- [ ] **Step 3: Implement** — in `src/clio/llm.py`:

Imports to add at the top (with the existing `import json`/`import os`/`import re` block):

```python
import asyncio
import urllib.error
import urllib.request

from clio.config import Limits, get_limits, load_env
```

Add `_post_json` and `mock_handler` (after `MockLLM`/`LLMClient` definitions, before `GeminiClient`):

```python
def _post_json(url: str, payload: dict, timeout: int = 60) -> dict:
    """POST a JSON payload and return the parsed JSON response.

    Synchronous by design; async callers wrap it in ``asyncio.to_thread``.
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
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
```

Replace the entire `GeminiClient` class (its old httpx implementation) with:

```python
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
```

In `src/clio/cli.py`, delete the `_mock_handler` function (lines 16-23) and change the import on line 10:

```python
from clio.llm import MockLLM, mock_handler as _mock_handler
```

(`LLMMessage` is no longer used in `cli.py`; `MockLLM` stays until Task 4.)

- [ ] **Step 4: Run the full suite to verify green**

Run: `python -m pytest -q`
Expected: 158 passed (152 + 6), 0 failed. Existing `test_gemini_requires_key`, the CLI e2e tests (`_mock_handler` behavior via the alias), and the web suite (imports `_mock_handler` from `clio.cli`) must all still pass.

- [ ] **Step 5: Commit**

```bash
git add src/clio/llm.py src/clio/cli.py tests/test_llm.py
git commit -m "feat(llm): stdlib HTTP layer and urllib Gemini client"
```

---

## Task 3: `llm.py` — `GroqClient` + `make_client` factory

**Files:**
- Modify: `src/clio/llm.py` (add `GroqClient`, `make_client`)
- Test: `tests/test_llm.py` (append)

**Interfaces:**
- Consumes: Task 1 `load_env`, Task 2 `_post_json`/`mock_handler`
- Produces: `GroqClient`, `make_client(provider, limits)` — consumed by Task 4

- [ ] **Step 1: Write the failing tests** — append to `tests/test_llm.py`:

```python
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


def test_make_client_mock_default():
    assert isinstance(make_client("mock"), MockLLM)


def test_make_client_unknown_falls_back_to_mock(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert isinstance(make_client("wat"), MockLLM)


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
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_llm.py -v`
Expected: 7 FAILs — `ImportError: cannot import name 'GroqClient'` / `'make_client'`. Suite total 82 tests, 7 failed.

- [ ] **Step 3: Implement** — in `src/clio/llm.py`, add after `GeminiClient`:

```python
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
        data = await asyncio.to_thread(_post_json, url, payload)
        return data["choices"][0]["message"]["content"]
```

And at the end of the file:

```python
def make_client(provider: str, limits: Limits | None = None) -> LLMClient:
    """Build the client for ``provider`` (mock | gemini | groq); unknown
    names fall back to the mock client."""
    load_env()
    if provider == "gemini":
        return GeminiClient()
    if provider == "groq":
        return GroqClient()
    return MockLLM(handler=mock_handler(limits or get_limits()))
```

- [ ] **Step 4: Run the full suite to verify green**

Run: `python -m pytest -q`
Expected: 166 passed (158 + 8), 0 failed.

- [ ] **Step 5: Commit**

```bash
git add src/clio/llm.py tests/test_llm.py
git commit -m "feat(llm): add Groq provider and make_client factory"
```

---

## Task 4: Wire CLI + dashboard, drop httpx, `.env.example`, README

**Files:**
- Modify: `src/clio/cli.py` (`--provider` choices + default via `get_provider()`, `amain` via `make_client`)
- Modify: `src/clio/web.py` (`run_job` via `make_client(get_provider(), limits)`; remove `_mock_handler`/`MockLLM` imports)
- Modify: `pyproject.toml` (remove `httpx` from `dependencies`)
- Create: `.env.example` (repo root)
- Modify: `README.md` (status table: M7 row)
- Test: `tests/test_cli.py`, `tests/test_web.py` (append)

**Interfaces:**
- Consumes: Task 3 `make_client`; Task 1 `get_provider`
- Produces: stable CLI/API contracts — `clio build <url> --provider {mock,gemini,groq}`; dashboard honors `CLIO_PROVIDER`/`.env`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_parser_accepts_groq():
    args = build_parser().parse_args(["https://github.com/x/y.git", "--provider", "groq"])
    assert args.provider == "groq"


def test_parser_default_provider_from_env(monkeypatch):
    monkeypatch.setenv("CLIO_PROVIDER", "groq")
    args = build_parser().parse_args(["https://github.com/x/y.git"])
    assert args.provider == "groq"
```

Append to `tests/test_web.py`:

```python
def test_run_job_builds_provider_client(monkeypatch, tmp_path):
    calls = {}

    class FakeClient:
        async def complete(self, messages, **kwargs):
            return '{"final": "done"}'

    def fake_make_client(provider, limits=None):
        calls["provider"] = provider
        calls["limits"] = limits
        return FakeClient()

    class FakeOrchestrator:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, url, root, job_id):
            calls["job_id"] = job_id
            return None

    monkeypatch.setattr("clio.web.make_client", fake_make_client)
    monkeypatch.setattr("clio.web.Orchestrator", FakeOrchestrator)
    monkeypatch.setenv("CLIO_PROVIDER", "groq")
    dashboard = Dashboard(root=tmp_path)
    dashboard.run_job("file:///tmp/x", "job-1")
    assert calls["provider"] == "groq"
    assert calls["job_id"] == "job-1"
```

(Adjust the `tests/test_web.py` import block as needed — `Dashboard` should already be imported; add `tmp_path` usage is via pytest fixture, no import needed.)

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_cli.py tests/test_web.py -v`
Expected: 3 FAILs — `SystemExit` from `--provider groq` (invalid choice) in `test_parser_accepts_groq`; default stays `"mock"` in `test_parser_default_provider_from_env`; `run_job` ignores `CLIO_PROVIDER` in the web test.

- [ ] **Step 3: Implement**

In `src/clio/cli.py`:

- Change the import on line 10 to `from clio.llm import make_client` (`MockLLM`, `mock_handler`, `LLMMessage` no longer used).
- Change the provider argument to:

```python
    parser.add_argument(
        "--provider", choices=["mock", "gemini", "groq"], default=get_provider(),
        help="LLM provider (default: $CLIO_PROVIDER, mock if unset)",
    )
```

- Replace the provider if/else in `amain` (lines 46-50) with:

```python
    client = make_client(args.provider, limits)
```

In `src/clio/web.py`:

- Change line 15: remove `from clio.cli import _mock_handler`.
- Change line 17 to `from clio.config import Limits, get_limits, get_provider`.
- Change line 20: remove `from clio.llm import MockLLM`; add `from clio.llm import make_client`.
- Replace line 399 in `run_job`:

```python
        client = make_client(get_provider(), limits)
```

In `pyproject.toml`: change `dependencies = ["httpx>=0.27"]` to `dependencies = []` (line 11). Leave everything else untouched — `httpx` was the only runtime dependency, imported lazily by the old `GeminiClient`.

Create `.env.example` at the repo root:

```bash
# Clio configuration — copy to .env (or point CLIO_ENV_FILE at it) and fill in.
# Provider: mock (no key), gemini, or groq.
CLIO_PROVIDER=mock

# https://aistudio.google.com/apikey
GEMINI_API_KEY=

# https://console.groq.com/keys
GROQ_API_KEY=
```

- [ ] **Step 4: Run the full suite to verify green**

Run: `python -m pytest -q`
Expected: 169 passed (166 + 3), 0 failed. The existing `test_parser_defaults` (asserts `"mock"` default) passes because `CLIO_PROVIDER` is unset and no `.env` exists at the repo root.

- [ ] **Step 5: Commit**

```bash
git add src/clio/cli.py src/clio/web.py pyproject.toml .env.example tests/test_cli.py tests/test_web.py
git commit -m "feat(cli,web): wire provider factory, drop httpx, add .env.example"
```

---

## Final review

> **Executed as part of M7 (commit log in order):** `9d97c54` Task 1 → `fd24cf1` Task 2 → `396357e` Task 3 → `2da3ae3` Task 4 (bundled a 3-line env-leak cleanup in `tests/test_config.py`, discovered when web tests began honoring `CLIO_PROVIDER`) → `4841ee3` `fix(store): dedupe repeated imports and symbols on graph save` (pre-existing M2 bug surfaced by the M7 demo run: repeated `import` statements in a real repo violated the `imports` PK; regression test `test_save_dedupes_repeated_imports_and_symbols` added, 170 total).

- [ ] **Step 1: `git log --oneline`** — four M7 commits present: `feat(config): add .env loader and provider resolution` → `feat(llm): stdlib HTTP layer and urllib Gemini client` → `feat(llm): add Groq provider and make_client factory` → `feat(cli,web): wire provider factory, drop httpx, add .env.example`
- [ ] **Step 2: Full suite** — `python -m pytest -q` → 170 passed (169 M7 + 1 regression test for the store fix, see below)
- [ ] **Step 3: Dependency check** — `pip show httpx` reports not-installed (or the project installs cleanly with `dependencies = []`); `python -c "import clio.llm, clio.cli, clio.web"` imports cleanly with no network access
- [ ] **Step 4: Manual CLI demo** — `python -m clio.cli --help` shows `--provider {mock,gemini,groq}` defaulting to `mock`; with `CLIO_PROVIDER=groq` and `GROQ_API_KEY` set, `python -m clio build https://github.com/omhome16/Clio.git` runs against Groq (skip if no key — mock path must run identically)
- [ ] **Step 5: Update README** — append `| M7 — LLM providers (.env config, urllib Gemini, Groq client) | ✅ Done |` to the milestone table in `README.md`; commit `docs: mark M7 done`
- [ ] **Step 6: Merge to `main`** — commit the plan doc and milestone on `main` with the M7 commit hashes; final commit message: `docs(plans): add M7 providers plan` (plan doc committed alongside the first M7 task or at completion — repo convention is plan-first, so commit `docs/plans/2026-08-10-m7-llm-providers.md` before executing Task 1)

## Deferred features (from the M7-M9 spec)

- **Ask panel (M8)** — dashboard UI to ask the LLM follow-up questions about a report. Deferred: requires M7 providers; the spec's M8 milestone.
- **Module map (M9)** — interactive visual map of modules. Deferred: M9.
- **Streaming responses** — token-by-token SSE from providers. Deferred: not in the spec's M7-M9 scope; needs `stream: true` handling in `_post_json` (out of scope).

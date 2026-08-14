# tests/test_ask.py
import json

import pytest

from clio.ask import (
    ASK_SYSTEM_PROMPT, NO_MATCH_ANSWER, ChatSession, load_chat_index,
)
from clio.events import Event, EventBus
from clio.graph import build_repo_graph
from clio.llm import LLMMessage
from clio.retrieval import build_retrieval_index


class ScriptedClient:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[list[LLMMessage]] = []

    async def complete(self, messages, **kwargs):
        self.calls.append(list(messages))
        return self._responses.pop(0)


def _files():
    return {
        "a.py": "def greet():\n    return 1\n",
        "b.py": "from a import greet\n\ndef run():\n    return greet()\n",
        "README.md": "# demo\n\nGreets with photocopies.\n",
    }


def _session(tmp_path, client, job_id="job-1"):
    seed = tmp_path / "sandbox"
    seed.mkdir(parents=True, exist_ok=True)
    return ChatSession(job_id, seed, client)


async def test_answer_grounded_in_retrieval(tmp_path, seed_job):
    seed_job(tmp_path / "sandbox", "job-1", "2026-08-10T00:00:00+00:00", _files())
    client = ScriptedClient(["greet returns the number 1."])
    session = _session(tmp_path, client)
    out = await session.answer("what does greet do")
    assert out["ok"] is True
    assert out["answer"] == "greet returns the number 1."
    assert any(src["path"] == "a.py" for src in out["sources"])
    assert len(client.calls) == 1
    last = client.calls[0][-1].content
    assert "--- a.py:1-2 ---" in last
    assert "Question: what does greet do" in last


async def test_sources_never_come_from_the_model(tmp_path, seed_job):
    seed_job(tmp_path / "sandbox", "job-1", "2026-08-10T00:00:00+00:00", _files())
    client = ScriptedClient(["hallucinated nonsense"])
    session = _session(tmp_path, client)
    out = await session.answer("what does greet do")
    assert [s["path"] for s in out["sources"]][0] == "a.py"
    assert out["answer"] == "hallucinated nonsense"


async def test_call_question_surfaces_caller_chunk(tmp_path, seed_job):
    seed_job(tmp_path / "sandbox", "job-1", "2026-08-10T00:00:00+00:00", _files())
    client = ScriptedClient(["b calls it."])
    session = _session(tmp_path, client)
    out = await session.answer("who calls greet")
    paths = [s["path"] for s in out["sources"]]
    assert "b.py" in paths


async def test_no_match_skips_llm(tmp_path, seed_job):
    seed_job(tmp_path / "sandbox", "job-1", "2026-08-10T00:00:00+00:00", _files())
    client = ScriptedClient([])
    session = _session(tmp_path, client)
    out = await session.answer("what about zzqzxwq")
    assert out["ok"] is False
    assert out["answer"] == NO_MATCH_ANSWER
    assert client.calls == []


async def test_overview_question_uses_readme(tmp_path, seed_job):
    seed_job(tmp_path / "sandbox", "job-1", "2026-08-10T00:00:00+00:00", _files())
    client = ScriptedClient(["overview text"])
    session = _session(tmp_path, client)
    out = await session.answer("what does this project do")
    assert out["ok"] is True
    assert out["answer"] == "overview text"
    last = client.calls[0][-1].content
    assert "photocopies" in last
    assert any(src["path"] == "README.md" for src in out["sources"])


async def test_overview_without_readme_uses_module_context(tmp_path, seed_job):
    files = {"a.py": "def greet():\n    return 1\n", "b.py": "def run():\n    return 2\n"}
    seed_job(tmp_path / "sandbox", "job-1", "2026-08-10T00:00:00+00:00", files)
    client = ScriptedClient(["module summary"])
    session = _session(tmp_path, client)
    out = await session.answer("what does this repo do")
    assert out["ok"] is True
    last = client.calls[0][-1].content
    assert "a" in last and "b" in last


async def test_prompt_contains_repo_context(tmp_path, seed_job):
    seed_job(tmp_path / "sandbox", "job-1", "2026-08-10T00:00:00+00:00", _files())
    client = ScriptedClient(["ok"])
    session = _session(tmp_path, client)
    await session.answer("what does greet do")
    last = client.calls[0][-1].content
    assert "Repository context:" in last


def test_anti_dodge_prompt():
    assert "best-effort" in ASK_SYSTEM_PROMPT.lower()
    assert "never" in ASK_SYSTEM_PROMPT.lower()


def test_extract_query_terms_with_fake_handler(tmp_path):
    import asyncio

    from clio.ask import extract_query_terms
    from clio.llm import FakeLLM, fake_handler
    from clio.config import get_limits

    client = FakeLLM(handler=fake_handler(get_limits()))
    out = asyncio.run(extract_query_terms("how does the store persist", client, get_limits()))
    assert out == {}


def test_query_understanding_feeds_symbols_when_no_exact_hit(tmp_path):
    import asyncio

    root = tmp_path / "web"
    root.mkdir()
    (root / "store.py").write_text(
        "def persist(data):\n    with open('db.json', 'w') as f:\n        f.write(data)\n",
        encoding="utf-8",
    )
    graph = build_repo_graph(root)

    class UnderstandingFake:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, model=None):
            self.calls += 1
            if "Extract" in messages[0].content:
                return '{"symbols": ["persist"], "paths": ["store.py"], "keywords": ["save", "database"]}'
            return "The persist function writes data to db.json."

    client = UnderstandingFake()
    session = ChatSession("job", tmp_path, client)
    session._index = build_retrieval_index(root, graph)
    session._graph = graph
    result = asyncio.run(session.answer("how does it save data to a file"))
    assert result["ok"]
    assert any("store.py" == s["path"] for s in result["sources"])
    assert client.calls >= 2


def test_compaction_summarizes_old_turns(tmp_path):
    import asyncio

    root = tmp_path / "web"
    root.mkdir()
    (root / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    graph = build_repo_graph(root)

    class CompactingFake:
        def __init__(self):
            self.summary_calls = 0

        async def complete(self, messages, model=None):
            if "Compaction" in messages[0].content:
                self.summary_calls += 1
                return "Objective: understand the store. Files: store.py. Decisions: none. Open: how it persists. Next: inspect persist()."
            return "ok"

    client = CompactingFake()
    session = ChatSession("job", tmp_path, client)
    session._index = build_retrieval_index(root, graph)
    session._graph = graph
    session.history = [
        {"role": "user", "content": "question with " + "padding " * 3000},
        {"role": "assistant", "content": "answer " + "padding " * 3000},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
    ]
    asyncio.run(session._maybe_compact())
    assert client.summary_calls == 1
    assert session.summary and "Objective:" in session.summary
    assert len(session.history) <= 2
    assert len(session.archive) == 2


def test_memory_bank_roundtrip(tmp_path):
    job = "clio-test"
    session = ChatSession(job, tmp_path, None)
    session.write_memory(job, tmp_path, extra={"next": "inspect persist()"})
    text = session.load_memory(tmp_path, job)
    assert "inspect persist()" in text
    assert (tmp_path / "jobs" / f"{job}.memory" / "activeContext.md").is_file()


def test_history_block_includes_summary(tmp_path):
    from clio.ask import _history_block

    out = _history_block(
        [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
        summary="OBJECTIVE: find the bug",
    )
    assert "OBJECTIVE: find the bug" in out


async def test_history_carries_prior_turns(tmp_path, seed_job):
    seed_job(tmp_path / "sandbox", "job-1", "2026-08-10T00:00:00+00:00", _files())
    client = ScriptedClient(["one", "two"])
    session = _session(tmp_path, client)
    await session.answer("what does greet do")
    await session.answer("who calls greet")
    second = client.calls[1][-1].content
    assert "[Prior conversation]" in second
    assert "User: what does greet do" in second and "Assistant: one" in second


async def test_answer_publishes_event(tmp_path, seed_job):
    seed_job(tmp_path / "sandbox", "job-1", "2026-08-10T00:00:00+00:00", _files())
    client = ScriptedClient(["it returns 1"])
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    session = _session(tmp_path, client)
    await session.answer("what does greet do", bus=bus)
    finals = [e for e in seen if e.type == "ask.final"]
    assert len(finals) == 1
    assert finals[0].data["answer"] == "it returns 1"
    assert finals[0].data["ok"] is True


async def test_load_chat_index_from_seeded_job(tmp_path, seed_job):
    seed_job(tmp_path / "sandbox", "job-1", "2026-08-10T00:00:00+00:00", _files())
    index, graph = load_chat_index("job-1", tmp_path / "sandbox")
    hits = index.search("photocopy")
    assert hits and hits[0].chunk.path == "README.md"
    assert list(graph.modules)[0] == "a"
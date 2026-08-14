# src/clio/ask.py
"""Retrieval-grounded chat engine.

Every question is answered in ONE completion: the hybrid retrieval index
finds the most relevant code chunks, they are packed into the prompt, and
the model only reads and summarizes — it never hunts through tools (the
agentic loop this replaces failed on small and even frontier models).

Sources are deterministic: they come from retrieval, never from the model,
so citations cannot be hallucinated. A question that matches nothing is
answered without any LLM call.
"""
from __future__ import annotations

import json as _json
import re
from pathlib import Path

from clio.config import Limits, get_limits
from clio.events import EVENT_ASK_FINAL, Event, EventBus
from clio.llm import LLMClient, LLMMessage
from clio.retrieval import (
    Hit, RetrievalIndex, build_retrieval_index, pack_hits, sources_from_hits,
)
from clio.store import GraphStore

QUERY_EXTRACT_SYSTEM = (
    "Extract search terms from a question about a code repository. "
    "Output STRICT JSON only, no prose: "
    '{"symbols": [up to 5 likely function/class names], '
    '"paths": [up to 5 likely file paths], '
    '"keywords": [up to 5 search keywords]}. '
    "Use empty arrays when nothing applies."
)

ASK_SYSTEM_PROMPT = (
    "You are Clio, an expert code analyst. Below are excerpts from a "
    "repository, each marked with a header like --- path:start-end ---. "
    "Answer the user's question using ONLY the provided excerpts. Cite "
    "sources inline as [path:line]. If the excerpts contain any relevant "
    "detail, answer from it; never refuse by saying the excerpts are "
    "insufficient or do not state something. If a specific detail is truly "
    "missing, give your best-effort answer from what is shown, say exactly "
    "what is missing, and point at the file most likely to hold it. Be "
    "concise and concrete; never invent APIs, files, or behavior."
)

OVERVIEW_RE = re.compile(
    r"^(what|what'?s|whats|describe|explain|summarise|summarize|"
    r"tell me about|about)[^?]*?\b(repo|repository|project|codebase|"
    r"application|app|clio)\b"
    r"|^(what does (this|the|it) do)\b"
    r"|^(what is (this|the|it) about)\b",
    re.I,
)

_README_NAMES = ("README.md", "README.rst", "readme.md", "README.txt", "README")

CHUNK_BUDGET_CHARS = 12_000
HISTORY_BUDGET_CHARS = 3_000
REPO_CONTEXT_CHARS = 1_500
COMPACT_CHARS = 9_000
COMPACT_KEEP_TURNS = 6
MEMORY_BUDGET_CHARS = 2_000
COMPACT_SYSTEM = (
    "Compaction: you are compressing a chat session with a code-analysis "
    "assistant. Write a structured summary (max 600 chars) with these "
    "sections: Objective: ... | Files: ... | Decisions: ... | Open "
    "questions: ... | Next steps: ... . Keep concrete file:line references. "
    "This summary replaces the conversation history."
)
NO_MATCH_ANSWER = (
    "Nothing in the indexed code matched your question. Try rephrasing with "
    'module or function names — e.g. "how does the store persist?" instead '
    'of "how does it save data?".'
)


def load_chat_index(job_id: str, root: Path | str) -> tuple[RetrievalIndex, object]:
    """Build the retrieval index for a persisted job (workspace + graph)."""
    root = Path(root)
    workspace = root / job_id
    graph = GraphStore(root / "jobs" / f"{job_id}.graph.db").load()
    return build_retrieval_index(workspace, graph), graph


def _history_block(history: list[dict], budget: int = HISTORY_BUDGET_CHARS,
                   summary: str | None = None) -> str:
    parts: list[str] = []
    used = 0
    if summary:
        parts.append(f"[Session summary]\n{summary}")
        used = len(summary)
    for turn in history[-6:]:
        line = f"{turn['role'].capitalize()}: {turn['content']}"
        if used + len(line) > budget:
            break
        parts.append(line)
        used += len(line)
    return "\n".join(parts)


async def extract_query_terms(question: str, client: LLMClient,
                              limits: Limits) -> dict:
    """One flash call to pull symbols/paths/keywords; fail-soft to {}."""
    try:
        text = await client.complete(
            [
                LLMMessage(role="system", content=QUERY_EXTRACT_SYSTEM),
                LLMMessage(role="user", content=f"Question: {question}"),
            ],
            model=limits.cheap_model,
        )
    except Exception:
        return {}
    if not text:
        return {}
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return {}
    try:
        data = _json.loads(text[start:end + 1])
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "symbols": [str(s) for s in data.get("symbols", [])][:5],
        "paths": [str(p) for p in data.get("paths", [])][:5],
        "keywords": [str(k) for k in data.get("keywords", [])][:5],
    }


def _weak_hits(hits: list[Hit]) -> bool:
    """True when every hit matched only by BM25 (no structural signal)."""
    return bool(hits) and all(set(h.reasons) <= {"bm25"} for h in hits)


class ChatSession:
    """One chat per job: retrieval index + bounded conversation history."""

    def __init__(
        self,
        job_id: str,
        root: Path | str,
        client: LLMClient,
        index: RetrievalIndex | None = None,
        limits: Limits | None = None,
    ) -> None:
        self.job_id = job_id
        self.root = Path(root)
        self._client = client
        self._index = index
        self._limits = limits or get_limits()
        self.history: list[dict] = []
        self.summary: str | None = None
        self.archive: list[dict] = []
        self._last_sources: list[dict] = []
        self._understanding_cache: dict[str, dict] = {}

    async def compact(self, bus: EventBus | None = None) -> str:
        lines = [f"{t['role'].capitalize()}: {t['content']}" for t in self.history]
        text = await self._client.complete(
            [
                LLMMessage(role="system", content=COMPACT_SYSTEM),
                LLMMessage(role="user", content="\n".join(lines)[-12_000:]),
            ],
            model=self._limits.cheap_model,
        )
        summary = (text or "").strip()[:1200]
        if summary:
            self.summary = summary
            keep = max(len(self.history) - 2, 0)
            self.archive.extend(self.history[:keep])
            self.history = self.history[keep:]
        return summary

    async def _maybe_compact(self, bus: EventBus | None = None) -> None:
        if self.summary is None and sum(len(t["content"]) for t in self.history) > COMPACT_CHARS:
            await self.compact(bus)

    def write_memory(self, job_id: str, root: Path, extra: dict | None = None) -> None:
        mem_dir = Path(root) / "jobs" / f"{job_id}.memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        objective = self.history[0]["content"][:300] if self.history else ""
        key_files = sorted({s["path"] for s in self._last_sources})
        active = [
            "# activeContext.md",
            f"Objective: {objective}",
            "Key files: " + ", ".join(key_files[:12]),
            "Open questions: (see chat)",
            f"Next steps: {(extra or {}).get('next', '')}",
        ]
        (mem_dir / "activeContext.md").write_text("\n".join(active), encoding="utf-8")
        (mem_dir / "progress.md").write_text(
            f"# progress.md\nTurns answered: {len(self.history)}\n", encoding="utf-8"
        )

    def load_memory(self, root: Path, job_id: str) -> str:
        path = Path(root) / "jobs" / f"{job_id}.memory" / "activeContext.md"
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:MEMORY_BUDGET_CHARS]
        except OSError:
            return ""

    def _ensure_index(self) -> RetrievalIndex:
        if self._index is None:
            self._index, self._graph = load_chat_index(self.job_id, self.root)
        return self._index

    def _repo_context(self) -> str:
        graph = getattr(self, "_graph", None)
        if graph is None:
            return ""
        modules = list(graph.modules)[:20]
        counts = [
            f"{mod} ({sum(1 for s in graph.symbols if s.module == mod)} symbols)"
            for mod in modules
        ]
        parts = ["Modules: " + ", ".join(counts)]
        try:
            from clio.repomap import build_repo_map
            parts.append("Repo map:\n" + build_repo_map(
                Path(graph.root), graph, budget_chars=700))
        except Exception:
            pass
        return "\n".join(parts)[:REPO_CONTEXT_CHARS]

    def _readme_chunk(self) -> Hit | None:
        for chunk in self._index.chunks:
            if chunk.doc and chunk.path in _README_NAMES:
                return Hit(chunk=chunk, score=0.0, reasons=["readme"])
        return None

    def _prompt(self, question: str, hits: list[Hit]) -> list[LLMMessage]:
        excerpts = pack_hits(hits, CHUNK_BUDGET_CHARS)
        parts = [f"Code excerpts from the repository:\n\n{excerpts}"]
        if not self.history:
            memory = self.load_memory(self.root, self.job_id)
            if memory:
                parts.append(f"[Prior session memory]\n{memory}")
        prior = _history_block(self.history, summary=self.summary)
        if prior:
            parts.append(f"[Prior conversation]\n{prior}")
        context = self._repo_context()
        if context:
            parts.append(f"Repository context: {context}")
        parts.append(f"Question: {question}")
        return [
            LLMMessage(role="system", content=ASK_SYSTEM_PROMPT),
            LLMMessage(role="user", content="\n\n".join(parts)),
        ]

    async def _answer_overview(self, question: str, bus: EventBus | None) -> dict:
        hit = self._readme_chunk()
        if hit is not None:
            evidence = f"README.md:\n\n{hit.chunk.text}"
            sources = sources_from_hits([hit])
        else:
            evidence = self._repo_context() or "(repository has no README)"
            sources = []
        parts = [f"Repository overview evidence:\n\n{evidence}"]
        prior = _history_block(self.history)
        if prior:
            parts.append(f"[Prior conversation]\n{prior}")
        parts.append(f"Question: {question}")
        messages = [
            LLMMessage(role="system", content=ASK_SYSTEM_PROMPT),
            LLMMessage(role="user", content="\n\n".join(parts)),
        ]
        text = await self._client.complete(messages, model=self._limits.cheap_model)
        answer = (text or "").strip() or "(the model returned an empty answer)"
        return self._finish(question, answer, sources, ok=True, bus=bus)

    def _finish(self, question: str, answer: str, sources: list[dict],
                *, ok: bool, bus: EventBus | None) -> dict:
        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": answer})
        result = {"answer": answer, "sources": sources, "ok": ok}
        if bus is not None:
            bus.publish(Event(type=EVENT_ASK_FINAL, job_id=self.job_id, data=result))
        return result

    def _search_with_terms(self, question: str, terms: dict,
                           index: RetrievalIndex) -> list[Hit]:
        boost_question = question
        if terms.get("keywords"):
            boost_question += " " + " ".join(terms["keywords"])
        hits = index.search(boost_question, top_k=8)
        if not hits:
            hits = []
            for path in terms.get("paths", []):
                path = path.replace("\\", "/")
                for chunk in index.chunks:
                    if chunk.path == path:
                        hits.append(Hit(chunk=chunk, score=10.0,
                                        reasons=["query-understanding path"]))
                        break
        return hits

    async def answer(self, question: str, bus: EventBus | None = None) -> dict:
        """Answer a question with sources; returns {answer, sources, ok}."""
        await self._maybe_compact(bus)
        index = self._ensure_index()
        if OVERVIEW_RE.match(question.strip()):
            return await self._answer_overview(question, bus)
        hits = index.search(question, top_k=8)
        if hits and _weak_hits(hits):
            terms = self._understanding_cache.get(question)
            if terms is None:
                terms = await extract_query_terms(question, self._client, self._limits)
                self._understanding_cache[question] = terms
            if terms:
                hits = self._search_with_terms(question, terms, index)
        if not hits:
            return self._finish(question, NO_MATCH_ANSWER, [], ok=False, bus=bus)
        sources = sources_from_hits(hits)
        self._last_sources = sources
        messages = self._prompt(question, hits)
        text = await self._client.complete(messages, model=self._limits.cheap_model)
        answer = (text or "").strip() or "(the model returned an empty answer)"
        return self._finish(question, answer, sources, ok=True, bus=bus)
# Clio

> **Paste a repository link. Clio reads it from the ground up, teaches you how
> it works — then answers anything you ask, every answer cited to the real
> code.**

Clio is a local, zero-dependency repo explainer: clone any git repository into
a sandbox, build a deterministic code graph (symbols, imports, calls) plus a
hybrid retrieval index, stream a staged **guide** (what it is → how it runs →
modules → run it), and then answer questions with **grounded answers** whose
citations come from retrieval — never hallucinated by the model.

Built for students and developers who want to truly understand a codebase.

---

## Why it's different

| Generic codebase chatbot | Clio |
|---|---|
| Reads files on request | Clones into a **sandbox** and indexes the whole repo first |
| Answers from memory | Answers **only from code excerpts** the hybrid index retrieves |
| Citations are invented | Sources are deterministic: BM25 + call-graph ranking, `file:line` chips you can open |
| One-shot answers | A **guide** teaches you the repo from the ground up, then chat takes over |
| Forgets between visits | Persists guide, graph, and chat sessions across restarts |

The pipeline **is** the demo: watch clone → graph → guide stages stream live.

---

## Quickstart

Requirements: Python 3.11+ (no third-party dependencies).

```bash
# 1. install (editable, so `python -m clio` works from anywhere)
pip install -e .

# 2. configure your provider key
copy .env.example .env        # Groq is the default; Ollama works keyless

# 3a. run the dashboard (terminal shows live logs; clio.log gets full tracebacks)
python -m clio.web            # → http://127.0.0.1:8790

# 3b. or headless CLI analysis
python -m clio https://github.com/user/repo.git

# tests
python -m pytest              # 250+ tests, offline (uses a scripted fake LLM)
```

`.env` (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `CLIO_PROVIDER` | `gemini` | `gemini` (default) or `groq` or `ollama` |
| `GEMINI_API_KEY` | — | required for the Gemini provider |
| `GROQ_API_KEY` | — | required for the Groq provider |
| `CLIO_CHEAP_MODEL` | `gemini-2.5-flash` | model for guide + chat answers |
| `CLIO_FRONTIER_MODEL` | `gemini-2.5-flash` | reserved for heavy synthesis |
| `CLIO_OLLAMA_BASE_URL` | `http://localhost:11434/v1` | local model endpoint |
| `CLIO_ALLOWED_HOSTS` | *(any https host)* | comma-separated host allowlist for cloning |

If a provider key is missing or the API rejects the request, the error is
logged to the terminal and `clio.log` (never printed as a raw URL — `?key=`
is redacted).

### Running on a small GPU (Ollama fallback)

`qwen2.5-coder:3b` fits ~4 GB VRAM and is fast on CPU-bound questions. The
agentic Ask loop that used to fail on small models is gone — chat is one
retrieval + one completion, which small models handle well.

```powershell
ollama pull qwen2.5-coder:3b
setx OLLAMA_CONTEXT_LENGTH 8192   # restart Ollama after this
# then set CLIO_PROVIDER=ollama and the models to qwen2.5-coder:3b in .env
```

---

## How to try it

1. Start the dashboard (`python -m clio.web`), paste **any** git URL —
   GitHub, GitLab, a self-hosted server, or a `file:///` path.
2. Watch the guide stages stream: *What it is → How it runs → Modules → Run
   it*. Each stage is grounded in README, entry points, call edges, and
   module tables.
3. Read the guide tabs on the left; expand the **Module explorer** for a
   symbol-by-symbol breakdown.
4. Ask anything on the right — "how does login work?", "who calls `verify`?"
   — and open the `file:line` **source chips** in the file viewer.
5. CLI equivalent: `python -m clio <url>` prints the event stream, then the
   report.

---

## How it works

```
Dashboard (vanilla JS + SSE, no build step)
        │  SSE events / JSON API
Local HTTP server (stdlib http.server)
        │
Analysis pipeline   ← clone → graph → cluster → guide (staged, streamed)
        │
Retrieval engine    ← Index-V2: symbol-granular chunks with headers +
        │              skeleton chunks, doc tier, repo map (personalized
        │              PageRank), RRF fusion of BM25 / symbol / path /
        │              caller / neighbor / README signals (stdlib only)
Chat engine         ← ONE completion per question over packed excerpts;
        │              sources come from retrieval, never the model;
        │              query understanding, compaction + memory bank
Stores              ← sandbox workspace, code graph (SQLite),
                       guide.json + report archive
```

- **Guide** — deterministic evidence bundles (full README with badge noise
  stripped, entry points, repo map, module table, clusters, run commands
  mined from the whole README + Makefile/package.json/requirements/pyproject/
  docker-compose) drive each stage; the LLM only rewrites them into short
  prose, citing sources inline. Hallucinated citations are linted against the
  workspace and replaced with the deterministic text — the guide is always
  complete. `clio.json` in the repo root can steer the guide (`repo_notes`,
  `run_commands`).
- **Chat** — RRF retrieval finds the most relevant chunks (one per file),
  packed with headers into the prompt; overview questions route to README +
  repo context instead of code chunks. Weakly-matched questions get one
  flash call to extract symbols/paths/keywords before re-searching. Long
  sessions are compacted into a structured summary, and a per-job memory
  bank (`activeContext.md`) carries context across sessions. A question that
  matches nothing is answered without any LLM call.
- **Eval harness** — `python -m tests.eval.run_eval <repo>` mines git
  history for fix commits and scores retrieval (MRR, Recall@k, BCY@8).
- **No agentic loops** — the tool-calling subagent era of Clio is retired;
  it failed on small models and hallucinated on large ones.

---

## Tech stack

- **Python 3.11+ stdlib only** — `http.server` (dashboard + SSE), `ast`
  (code graph), regex extractors for a dozen languages, `urllib` (LLM calls),
  `sqlite3` (graph store), hand-rolled BM25 (no numpy)
- **Zero-dependency frontend** — vanilla JS, no build step
- **Model-agnostic LLM adapter** — Groq, Gemini, and Ollama via raw REST

---

## Repository layout

```
src/clio/
  clone.py        safe cloning: validation, timeouts, size guard
  graph.py        multi-language code graph (symbols, imports, calls)
  extractors.py   AST + regex extraction for 12+ languages
  clustering.py   package/connectivity clusters
  retrieval.py    Index-V2: symbol chunks, doc tier, RRF search
  repomap.py      personalized PageRank repo map
  guide.py        staged guide builder (deterministic + LLM polish)
  ask.py          chat engine (overview routing, understanding, memory)
  orchestrator.py pipeline state machine
  web.py          dashboard: API + single-page frontend
  llm.py          providers (gemini, groq, ollama), retries, rate limiter
```

---

## Status

| Phase | Status |
|---|---|
| Sandbox + clone + tree tools | ✅ |
| Multi-language code graph + SQLite store + clustering | ✅ |
| Hybrid retrieval engine (BM25 + graph ranking) | ✅ |
| Staged guide + orchestrator rework (no subagents) | ✅ |
| Chat-first dashboard redesign (guide tabs, citations, file viewer, module explorer) | ✅ |
| Any-host cloning + Groq default + Ollama fallback | ✅ |
| v2 rework: Index-V2, Guide-V2, Chat-V2 + git-history eval harness | ✅ |

Project-based learning, built in public. No deadline — depth first.
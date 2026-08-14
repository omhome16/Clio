# Clio

> **Paste a repository link. Clio reads it from the ground up, teaches you how
> it works — then answers anything you ask, every answer cited to the real
> code.**

Clio is a local, zero-dependency repo explainer: clone any git repository into
a sandbox, build a deterministic code graph (symbols, imports, calls) plus an
**Index-V2** hybrid retrieval index, stream a staged **guide** (what it is →
how it runs → modules → run it), and then answer questions with **grounded
answers** whose citations come from retrieval — never hallucinated by the
model.

Built for students and developers who want to truly understand a codebase.

---

## Why it's different

| Generic codebase chatbot | Clio |
|---|---|
| Reads files on request | Clones into a **sandbox** and indexes the whole repo first |
| Answers from memory | Answers **only from code excerpts** the index retrieves |
| Citations are invented | Sources are deterministic: RRF fusion of BM25 + symbol/call/path/neighbor signals, `file:line` chips you can open |
| One-shot answers | A **guide** teaches you the repo from the ground up, then chat takes over |
| Forgets between visits | Persists guide, graph, and chat sessions; a **memory bank** (`activeContext.md`) survives restarts |
| Nobody checks if it works | Ships a **git-history eval harness** — real fix commits score MRR / Recall@k / BCY@8 |

The pipeline **is** the demo: watch clone → graph → guide stages stream live.

---

## Quickstart

Requirements: Python 3.11+ (no third-party dependencies).

```bash
# 1. install (editable, so `python -m clio` works from anywhere)
pip install -e .

# 2. configure your provider key
copy .env.example .env        # Gemini is the default; Ollama works keyless

# 3a. run the dashboard (terminal shows live logs; clio.log gets full tracebacks)
python -m clio.web            # → http://127.0.0.1:8790

# 3b. or headless CLI analysis
python -m clio https://github.com/user/repo.git

# tests
python -m pytest              # 252 tests, offline (uses a scripted fake LLM)
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
| `CLIO_MAX_REPO_SIZE_MB` | `50` | clone size guard (aborts beyond this) |
| `CLIO_MAX_FILES` | `20000` | file-count guard for indexing |
| `CLIO_CLONE_TIMEOUT_S` | `120` | clone timeout in seconds |
| `CLIO_WORKSPACE_ROOT` | `sandbox` | where clones land |
| `CLIO_RPM` | `5` | LLM requests-per-minute cap |
| `CLIO_RATE_LIMIT` | `1` | set `0` to disable the rate limiter |
| `CLIO_ENV_FILE` | `.env` | alternate env file path |

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
Index-V2            ← symbol-granular chunks, doc tier, repo map
        │              (personalized PageRank), RRF fusion
Guide-V2            ← deterministic evidence bundles + citation linting
Chat-V2             ← overview routing, query understanding, compaction,
        │              memory bank — ONE completion per question
Stores              ← sandbox workspace, code graph (SQLite),
                       guide.json + report archive
```

### 1. Index-V2 — `retrieval.py` (stdlib only, no embeddings)

The index is built once per workspace from the code graph + raw files:

- **Chunking.** Code files are split at symbol boundaries: every class /
  function gets its own chunk with a **header** (signature line + path) and
  its exact line range (`end_line` recorded by the AST visitor). Oversized
  symbols are split at blank lines. Remaining gaps become plain chunks, and
  each file contributes one **skeleton chunk** (signatures only) for
  overview-style questions.
- **Doc tier.** Markdown / RST / text files (`*.md`, `*.rst`, `*.txt`,
  `*.mkd`, root READMEs, `docs/`) are indexed whole-file up to 40 KB — no
  line-splitting — so a README stays a single retrievable unit.
- **Tokenization.** camelCase / snake_case splitting, a light stemmer
  (`ies→y`, `es→`, `s→`), stopwords, min length 3. BM25 with `k1=1.5,
  b=0.75`.
- **Repo map.** `repomap.py` builds a file-reference graph (imports + call
  edges) and runs **personalized PageRank** (α=0.85, query-personalized) to
  rank files by relevance; the result is rendered into the guide and chat
  context, not just searched.
- **Search = RRF fusion.** `rrf_merge(k=60)` combines ranked lists:
  `bm25` → `symbol match` (definitions) → `module match` → `path match` →
  `callers` (call/use intent words) → `import neighbors` → `README`. Callers
  never outrank definitions, and README leads only when a query has no
  symbol/path/module signal at all. Result: top-8 hits, **one chunk per
  file**, deterministic scores.

### 2. Guide-V2 — `guide.py` (staged, deterministic, linted)

```
 for stage in (what, how, modules, run):
     emit job.stage {stage, started}
     facts = evidence bundles ("--- E1: ... ---"), e.g.
        what    → full README (badges stripped, ≤200 KB) + entry points
        how     → entry points + call edges from them (caller → callee)
        modules → personalized PageRank repo map + module table + clusters
        run     → run_hints: commands mined from the whole README, Makefile,
                  package.json (nested), requirements*, pyproject,
                  docker-compose, justfile (cap 12)
     clio.json (repo root) can steer: repo_notes, run_commands
     AGENTS.md / CLAUDE.md / .windsurfrules feed the prompt (≤4 KB)
     text = ONE completion(cheap model, "answer only from this evidence")
            → citations [path:line] are linted against the workspace;
              missing paths → deterministic fallback for that stage
     emit job.stage {stage, done}
 → guide.json {readme, entrypoints, stages{stage: {text, sources}}}
```

The guide is *always* complete — the model polishes, it never invents, and
citation linting guarantees it can't hallucinate file references.

### 3. Chat-V2 — `ask.py` (grounded, ONE completion, gated extras)

```
 user: "how does login work?"
  │
 ChatSession.answer(q)
  ├─ overview question? (OVERVIEW_RE) → README chunk + repo map/module
  │     context, no code retrieval
  ├─ else index.search(q, top_k=8)     ← Index-V2
  │     └─ no hits      → NO_MATCH_ANSWER, zero LLM calls
  │     └─ weak hits    → ONE flash call extracts symbols/paths/keywords
  │          (cached per session), then re-search + exact-path fallback
  ├─ pack_hits(hits, 12 KB)            excerpts marked "--- path:start-end ---"
  ├─ maybe compact: history > 9 KB → archive old turns into a structured
  │     summary (keeps the last Q&A pair), injected as context
  └─ ONE client.complete(cheap model)  ← no tools, no loops
  │
  ▼
 {answer, sources: [{path, start, score}]}   ← sources came from retrieval,
                                              never from the model
 → ask.final SSE event → chat bubble + clickable file:line chips
```

**Memory bank.** When a chat session ends, the last sources and a summary are
written to `jobs/<job>.memory/` (`activeContext.md`, `progress.md`) and
re-loaded into the prompt on the next session — context survives restarts.

### 4. Evaluation — `tests/eval/` (no LLM, no network)

```bash
python -m tests.eval.run_eval <repo> [out.jsonl]
```

Mines git history for **fix commits** (subject matches fix words), turns each
commit message into a query and its changed files into gold paths, then scores
retrieval: **MRR, Recall@1/5/8, BCY@8** (best-commit yield). On the FinEdge
sandbox (19 commits, 4 fix queries):

| metric | P0 baseline | v2 (Index-V2 + RRF + doc tier) |
|---|---|---|
| MRR | 0.208 | 0.198 |
| Recall@8 | 0.333 | **0.583** |
| BCY@8 | 0.333 | **0.583** |

### Design rules

- **One LLM completion per stage/question** — plus exactly one gated call for
  query understanding or compaction. No agentic loops, ever.
- **Deterministic fallbacks** — every model output is validated (citations
  linted, empty replies caught) and replaced with deterministic text if it
  fails. The guide and chat are always complete.
- **The model never chooses sources** — retrieval decides; the model can only
  rewrite what it's given. That's what makes citations trustworthy.

---

## Tech stack

- **Python 3.11+ stdlib only** — `http.server` (dashboard + SSE), `ast`
  (code graph), regex extractors for 12+ languages, `urllib` (LLM calls),
  `sqlite3` (graph store), hand-rolled BM25 + RRF (no numpy)
- **Zero-dependency frontend** — vanilla JS, no build step
- **Model-agnostic LLM adapter** — Gemini, Groq, and Ollama via raw REST,
  with retries, backoff, and a per-minute rate limiter

---

## Repository layout

```
src/clio/
  clone.py        safe cloning: URL validation, host allowlist, timeout, size guard
  sandbox.py      workspace path containment
  tree.py         tree listing + workspace stats
  graph.py        multi-language code graph (symbols, imports, calls, end lines)
  extractors.py   AST (Python) + regex (JS/TS, Go, Rust, Ruby, Java, C#,
                  C/C++, PHP, Swift, Kotlin, Bash) extraction
  clustering.py   package/connectivity clusters
  store.py        SQLite graph store (symbols, imports, calls)
  retrieval.py    Index-V2: symbol chunks, doc tier, RRF search, pack_hits
  repomap.py      personalized PageRank repo map
  guide.py        Guide-V2: evidence bundles, citation linting, run_hints
  ask.py          Chat-V2: overview routing, query understanding, memory
  orchestrator.py pipeline state machine (clone → graph → cluster → guide)
  impact.py       impact reports for a symbol/module
  map.py          module-resolution + layout helpers for the map panel
  reports.py      report archive
  job.py          job records (JSON on disk)
  events.py       event bus + SSE payloads
  llm.py          providers (gemini, groq, ollama), retries, rate limiter
  logging.py      terminal + clio.log tracebacks
  config.py       Limits + .env loading (see table above)
  cli.py          headless entrypoint: python -m clio <url>
  web.py          dashboard: API + single-page frontend + SSE
```

### HTTP API

| Endpoint | Purpose |
|---|---|
| `POST /api/analyze?url=…` | start an analysis job |
| `GET /api/stream?job_id=…` | SSE event stream (clone → graph → guide stages) |
| `GET /api/guide?job_id=…` | the finished guide JSON |
| `GET /api/modules?job_id=…` | symbol-by-symbol module breakdown |
| `POST /api/ask?job_id=…&q=…` | grounded chat answer + sources |
| `GET /api/suggest?job_id=…` | follow-up question suggestions |
| `GET /api/file?job_id=…&path=…` | raw file content for the viewer |
| `GET /api/jobs` / `DELETE /api/jobs/:id` | job lifecycle |

---

## Status

| Phase | Status |
|---|---|
| Sandbox + clone + tree tools | ✅ |
| Multi-language code graph + SQLite store + clustering | ✅ |
| Hybrid retrieval engine (BM25 + graph ranking) | ✅ |
| Staged guide + orchestrator rework (no subagents) | ✅ |
| Chat-first dashboard redesign (guide tabs, citations, file viewer, module explorer) | ✅ |
| Any-host cloning + Gemini default + Ollama fallback | ✅ |
| **v2 rework:** Index-V2 (symbol chunks, doc tier, repo map, RRF) | ✅ |
| **v2 rework:** Guide-V2 (evidence bundles, citation linting, run_hints) | ✅ |
| **v2 rework:** Chat-V2 (overview routing, query understanding, compaction, memory bank) | ✅ |
| Git-history eval harness (`python -m tests.eval.run_eval <repo>`) | ✅ |

Project-based learning, built in public. No deadline — depth first.

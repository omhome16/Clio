# Clio — Architecture (ASCII)

All diagrams are drawn from the actual code (`src/clio/*`).

---

## 1. System architecture (1 process, thread pool + per-job asyncio)

```
╔══════════════════════════════════════════════════════════════════════╗
║  BROWSER (zero-dep dashboard: static HTML+JS, no build step)          ║
║  ┌──────────────┐ ┌──────────────┐ ┌──────────────────────────────┐  ║
║  │ paste bar +  │ │ guide tabs   │ │ chat (citations + file       │  ║
║  │ repo history │ │ (what/how/   │ │  viewer drawer + module      │  ║
║  │              │ │  modules/run)│ │  explorer + suggestion chips)│  ║
║  └──────────────┘ └──────────────┘ └──────────────────────────────┘  ║
╚════════════════════════╤════════════════════════════════════════════╝
                         │  HTTP /api/*  +  SSE /api/stream, /api/ask
                         ▼
╔══════════════════════════════════════════════════════════════════════╗
║  python -m clio.web  →  http.server ThreadingHTTPServer :8790        ║
║                                                                      ║
║   _Handler (one thread per request)                                  ║
║      POST /api/analyze ──► spawns daemon thread ──► run_job()        ║
║      GET  /api/stream  ──► drains per-job event deque (SSE)          ║
║      GET  /api/ask     ──► spawns thread  ──► run_ask()              ║
║      GET  /api/guide /api/modules /api/file /api/suggest            ║
║      GET  /api/jobs, /graph, /map, /tree, DELETE ...                 ║
║                                                                      ║
║   Dashboard (in-memory state):                                       ║
║     _queues: {job_id → deque[Event]}   ← SSE queues                 ║
║     _done, _ask_queues, _ask_sessions (ChatSession per job)          ║
║                                                                      ║
║   run_job thread:  Orchestrator.run()  (single asyncio.run)          ║
║   run_ask thread:  ChatSession.answer() (single asyncio.run)         ║
╚════════════════════════╤════════════════════════════════════════════╝
                         │
              ┌──────────┼──────────────────────────┐
              ▼          ▼                          ▼
╔════════════════╗ ╔═══════════════════════╗ ╔══════════════════════════╗
║  LLM ADAPTERS  ║ ║  ANALYSIS CORE       ║ ║  STORES (on disk)        ║
║  (one per      ║ ║  Orchestrator       ║ ║  sandbox/<job>/   repo    ║
║   provider)    ║ ║  Guide (staged)     ║ ║  jobs/<job>.json status  ║
║  GroqClient    ║ ║  RetrievalIndex     ║ ║  jobs/<job>.graph.db     ║
║  GeminiClient  ║ ║   (BM25+graph)      ║ ║  jobs/<job>.guide.json   ║
║  OllamaClient  ║ ║  ChatSession        ║ ║  jobs/<job>.report.json  ║
║  * urllib POST ║ ║  EventBus → SSE     ║ ║  (SQLite: modules,       ║
║  * JSON reply  ║ ╚══════════════════════╝ ║   symbols, calls, edges) ║
╚════════════════╝                          ╚══════════════════════════╝
```

## 2. Analysis pipeline — phase state machine (one job)

```
 POST /api/analyze {url}          CLI: python -m clio <url>
        │
        ▼
 ┌──────────────┐        ┌─────────────────────────────┐
 │ QUEUED       │        │ new_job() → job_id clio-xxxx │
 └──────┬───────┘        └─────────────────────────────┘
        ▼
 ┌──────────────┐        ┌─────────────────────────────┐
 │ CLONING      │───────►│ clone_repo(): git clone     │
 │              │        │  --depth 1 (timeout 120s,   │
 └──────┬───────┘        │  any https host unless      │
        │                │  CLIO_ALLOWED_HOSTS, ≤50 MB │
        ▼                └─────────────────────────────┘
 ┌──────────────┐        ┌─────────────────────────────┐
 │ INDEXING     │───────►│ build_repo_graph(): ast +   │
 │              │        │  regex extractors for 12+   │
 └──────┬───────┘        │  languages → RepoGraph      │
        │                │  → GraphStore.save()        │
        ▼                └─────────────────────────────┘
 ┌──────────────┐        ┌─────────────────────────────┐
 │ GUIDING      │───────►│ build_guide(): 4 stages,    │
 │ (job.stage)  │        │  each = deterministic facts │
 │              │        │  + ONE cheap-model call     │
 └──────┬───────┘        │  fallback: facts verbatim   │
        │                └─────────────────────────────┘
        ▼
 ┌──────────────┐        write guide.json + report.json
 │ PERSISTED    │──────────────────────────────────────►
 └──────┬───────┘
        ▼
 job.persisted event → SSE → frontend loads guide/modules/chips
```

## 3. Retrieval engine — `retrieval.py` (Index-V2, no embeddings, no libraries)

```
 build_retrieval_index(workspace, graph)
        │
        ├─ every text file (≤1 MB, no null bytes, excluded dirs skipped)
        │   → files split two ways:
        │      code   → per-symbol chunks (header + body, oversized
        │                symbols split at blank lines) + gap chunks
        │      docs   → whole-file chunks (doc tier: *.md/*.rst/*.txt,
        │                root READMEs, docs/)
        ├─ symbol chunks carry: header (signature line), fqn, line range
        ├─ skeleton chunks: signatures only, one per file — for overview
        └─ chunk → terms: camelCase/snake_case split, light stemmer
            (ies→y, es→, s→), stopwords, min length 3

 search(q, top_k):                      ← RRF fusion (k=60)
   ranked lists:
     bm25     (score > 0 only, capped)     base lexical signal
     sym      definitions of queried symbols
     mod      chunks whose module matches the query
     path     query terms appearing in the file path
     calls    callers of queried symbols (head of the list only for
              explicit call/use intent words — otherwise appended,
              so callers never outrank definitions)
     neighbor modules of hits
     readme   README chunks (head of the list only when NO symbol/path/
              module matched — i.e. pure lexical questions; overview
              questions in chat route here via OVERVIEW_RE)
   → RRF-merge, one chunk per file (skeleton kept only if its file has
     no other match), top_k hits with scores 1/(k+rank)
```

No vector DB, no numpy, no LangGraph: the graph *is* the ranking signal,
and a personalized-PageRank **repo map** (`repomap.py`, query-personalized)
is rendered into the guide and chat context.

## 4. The guide — `guide.py` (staged, deterministic, streamed, linted)

```
 for stage in (what, how, modules, run):
     emit job.stage {stage, started}
     facts = deterministic evidence bundles ("--- E1: label ---"), e.g.
        what    → full README (badges stripped, ≤200 KB) + entry points
        how     → entry points + call edges from them (caller → callee)
        modules → personalized PageRank repo map + module table + clusters
        run     → run_hints: bash fences + commands mined from the whole
                  README, Makefile, package.json (nested), requirements,
                  pyproject, docker-compose, justfile (cap 12)
     clio.json (repo root) can override/steer: repo_notes, run_commands;
     AGENTS.md / CLAUDE.md / .windsurfrules feed the prompt too
     text = ONE completion(cheap model, "answer only from this evidence")
            → every citation [path:line] is linted against the workspace;
              missing paths → deterministic fallback text for that stage
     emit job.stage {stage, done}
 → guide.json {readme, entrypoints, stages{stage: {text, sources}}}
```

The guide is *always* complete — the model polishes, it never invents, and
citation linting guarantees the model can't hallucinate file references.

## 5. Code graph construction — `graph.py`

```
 repo/                      modules (dotted)       symbols          imports
 ├── src/ ────────────────► "clio.orchestrator"     Orchestrator      clio.clone
 │   ├── orchestrator.py                            Orchestrator.run  clio.graph
 │   ├── graph.py      ──► "clio.graph"             RepoGraph        ast
 │   ├── ask.py        ──► "clio.ask"               ChatSession      clio.retrieval
 │   └── __init__.py   ──► "clio"                   (package)
 └── tests/
     └── test_x.py    ───► "tests.test_x"

 │        │             │
 │        ▼             ▼
 │  Python: ast.parse → visitor walks imports/classes/functions/calls
 │  Others: regex extractors (JS/TS, Go, Rust, Ruby, Java, C#, C/C++,
 │          PHP, Swift, Kotlin, Bash) — see extractors.py
 │
 │     caller format:  "module::scope"          e.g. "clio.orchestrator::Orchestrator.run"
 │     callee resolve:  top-level name  → "module::name"
 │                      self/cls.attr   → "module::Class.method"
 │                      obj.attr        → "obj.attr" (best effort)
 │                      imported symbol → bare name (unresolved)
 │
 ▼
 GraphStore (SQLite)  ← saved as jobs/<job>.graph.db
    tables: modules, symbols, imports, calls — deduped on save
    queries: callers_of(symbol_id) → [(caller, line)]
             callees_of / modules_importing / module_imports / symbol_ids_in
```

## 6. Chat — `ask.py` (retrieval-grounded, ONE completion + gated extra)

```
 user: "who calls greet?"
  │
  ▼
 ChatSession.answer(q)                 history: last 6 turns, ≤3 KB budget
  │
  ├─ overview question? (OVERVIEW_RE) → README chunk + repo map/module
  │     context, NO retrieval            (no LLM call if no README)
  ├─ else index.search(q, top_k=8)     ← Index-V2 retrieval (§3)
  │     └─ no hits → NO_MATCH_ANSWER, no LLM call at all
  │     └─ weak hits (bm25-only reasons) → ONE flash call extracts
  │          query terms (symbols/paths/keywords, cached per session);
  │          re-search with keywords + exact-path fallback
  ├─ pack_hits(hits, 12 KB budget)     excerpts marked "--- path:start-end ---"
  ├─ system: "answer ONLY from the excerpts, cite [path:line]",
  │          anti-dodge: never refuse on thin excerpts, point at files
  ├─ possibly compact first: >9 KB history → archive old turns into a
  │     structured summary (keeps last Q&A pair), injected as context
  └─ ONE client.complete(cheap model)  ← no tools, no loops
  │
  ▼
 {answer, sources: [{path, start, score}]}   ← sources came from retrieval,
                                              never from the model
 → ask.final SSE event → chat bubble + clickable file:line chips
```

Memory bank: at the end of each session (web `run_ask` finally-block) the
session's sources and a summary are written to `jobs/<job>.memory/`
(`activeContext.md`, `progress.md`) and re-loaded into the prompt on the
next session — context survives across restarts.

History lives per job in `Dashboard._ask_sessions`; the index is rebuilt from
`workspace + graph.db` on first question.

## 7. Evaluation — `tests/eval/` (git-history gold sets, no LLM, no network)

```
 python -m tests.eval.run_eval <repo> [--out goldset.jsonl]
   └─ git log --format=%H <HEAD> --all        (git history)
        └─ fix commits (subject matches fix words) → changed files = gold
        └─ queries = commit subjects (bodies dropped, cleaned)
        └─ written as goldset.jsonl (only repos with ≥4 fix commits)
   └─ for each query: index.search(q, top_k=8)
        └─ MRR, Recall@1/5/8, BCY@8 (best-commit yield) over gold paths
```

Run on FinEdge (19 commits, 4 fix queries):

| metric | P0 baseline | v2 (Index-V2 + RRF + doc tier) |
|---|---|---|
| MRR | 0.208 | 0.198 |
| Recall@8 | 0.333 | 0.583 |
| BCY@8 | 0.333 | 0.583 |

Same order-of-magnitude MRR on a 4-query sample (≈ noise), +75% recall —
commit-message queries are adversarial (they describe intent, not content),
so absolute numbers stay modest; the harness is meant for relative tracking.

## 7. Event bus → SSE (everything visible)

```
                                     CLI (terminal)
 EventBus.publish(event)  ────────►  prints each event line + full report
        │
        ├─► orchestrator events: job.created → job.cloning → job.cloned
        │          → job.indexing → job.graphed → job.guiding
        │          → job.stage {what|how|modules|run, started|done} ×8
        │          → job.persisted  (or job.failed)
        │
        ├─► chat events: ask.final {answer, sources, ok}
        │
        └─► Dashboard._publish → deque per job
                  │
                  ▼  GET /api/stream?job_id=...  (SSE, snapshot-drains queue)
        frontend: stage checklist (clone → graph → 4 guide stages)
```

## 8. Data flow in one line

```
URL → clone → code graph → hybrid index → staged guide → cited chat
```

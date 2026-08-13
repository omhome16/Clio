# Clio — Architecture (ASCII)

All diagrams are drawn from the actual code (`src/clio/*`).

---

## 1. System architecture (2 processes, 1 thread pool)

```
╔══════════════════════════════════════════════════════════════════════╗
║  BROWSER (zero-dep dashboard: static HTML+JS, no build step)          ║
║  ┌──────────┐ ┌──────────┐ ┌──────────────┐ ┌─────────────────────┐  ║
║  │ bento    │ │ event    │ │ module map   │ │ Ask panel (chat)    │  ║
║  │ grid     │ │ ledger   │ │ (SVG) +     │ │  + tool-call stream │  ║
║  │ (hero,   │ │ (SSE     │ │ impact mode │ │                     │  ║
║  │ status,  │ │ stream)  │ │ + folder    │ │                     │  ║
║  │ history) │ │          │ │  tree       │ │                     │  ║
║  └──────────┘ └──────────┘ └──────────────┘ └─────────────────────┘  ║
╚════════════════════════╤════════════════════════════════════════════╝
                         │  HTTP /api/*  +  SSE /api/stream
                         ▼
╔══════════════════════════════════════════════════════════════════════╗
║  python -m clio.web  →  http.server ThreadingHTTPServer :8790        ║
║                                                                      ║
║   _Handler (one thread per request)                                  ║
║      POST /api/analyze ──► spawns daemon thread ──► run_job()        ║
║      GET  /api/stream  ──► drains per-job event deque (SSE)          ║
║      GET  /api/ask     ──► spawns thread  ──► run_ask()              ║
║      GET  /api/jobs, /graph, /map, /tree, DELETE ...                 ║
║                                                                      ║
║   Dashboard (in-memory state):                                       ║
║     _jobs:  {job_id → deque[Event]}   ← SSE queues                  ║
║     _done, _ask_queues, _ask_sessions                                ║
║                                                                      ║
║   run_job thread:  Orchestrator.run()  (single asyncio.run)          ║
║   run_ask thread:  AskSession.run_turn() (single asyncio.run)        ║
╚════════════════════════╤════════════════════════════════════════════╝
                         │
              ┌──────────┼──────────────────────────┐
              ▼          ▼                          ▼
╔════════════════╗ ╔═══════════════════════╗ ╔══════════════════════════╗
║  LLM ADAPTERS  ║ ║  HARNESS RUNTIME      ║ ║  STORES (on disk)        ║
║  (one key per  ║ ║  Orchestrator        ║ ║  sandbox/<job>/   repo    ║
║   provider)    ║ ║  Scheduler (asyncio  ║ ║  jobs/<job>.json status  ║
║  GeminiClient  ║ ║   semaphore fan-out) ║ ║  jobs/<job>.graph.db     ║
║  GroqClient    ║ ║  Subagent pool       ║ ║  jobs/<job>.report.json  ║
║  * urllib POST ║ ║  ToolRegistry        ║ ║  (SQLite: modules,       ║
║  * JSON reply  ║ ║  EventBus → SSE      ║ ║   symbols, calls, edges) ║
╚════════════════╝ ╚══════════════════════╝ ╚══════════════════════════╝
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
 └──────┬───────┘        │  host allowlist github.com, │
        │                │  size guard ≤ 50 MB)        │
        ▼                └─────────────────────────────┘
 ┌──────────────┐        ┌─────────────────────────────┐
 │ INDEXING     │───────►│ build_repo_graph():         │
 │              │        │  ast.parse every *.py       │
 └──────┬───────┘        │  → RepoGraph(modules,       │
        │                │    symbols, imports, calls) │
        ▼                │  → GraphStore.save() (SQLite)│
 ┌──────────────┐        └─────────────────────────────┘
 │ ANALYZING    │◄───────┐
 │ (fan-out)    │        │ 4 aspects, bounded concurrency
 └──────┬───────┘        └─────────────────────────────┐
        ▼                                              │
 ┌──────────────┐        ┌─────────────────────────────┘
 │ SYNTHESIZING │        │ 1 frontier-model call:
 │              │────────►  {"summary": "...", "modules": [...]}
 └──────┬───────┘        └─────────────────────────────┐
        ▼                                              │
 ┌──────────────┐        ┌─────────────────────────────┘
 │ PERSISTED    │        │ write report.json, save job
 └──────┬───────┘
        ▼
 job.persisted event → SSE → dashboard lamp goes green
```

## 3. The agent loop (one subagent's life) — `subagent.py`

```
                 ┌──────────────────────────────────────────┐
                 │  Subagent.run(task)                      │
                 │  messages = [system_prompt, task]        │
                 └──────────────────┬───────────────────────┘
                                    │
              ┌─────────────────────▼─────────────────────┐
              │  loop:  steps < max_agent_steps (10)      │
              │                                           │
              │  1. _compact()  ← trim context to 16K     │
              │     chars if over budget (drop middle,    │
              │     keep head+tail)                       │
              │  2. client.complete(messages)             │
              │     ★ 1 API call — the costly step        │
              │  3. parse_reply(text)                     │
              │                                           │
              │      ┌───► kind="tool"?
              │      │      │
              │      │      ├── registry.execute(tool,args) │
              │      │      │   (sandboxed, 12K char cap)   │
              │      │      ├── append assistant+tool msg   │
              │      │      └── continue loop ──────────┐   │
              │      │                                  │   │
              │      ├──► kind="final"?  ──► break ✓    │   │
              │      │                                  │   │
              │      └──► kind="none" (bad JSON)  ──► break ✗│
              └──────────────────────────────────────────┬───┘
                                                         │
                                                         ▼
                    SubagentReport {name, content, steps, tool_calls, ok}
                    → published as subagent.done event → SSE
```

Tools the subagents can call (each capped: timeout 30s, output ≤ 12,000 chars,
path locked to sandbox):

```
list_tree   → tree.py walk, excludes node_modules/.git etc.
read_file   → file contents, path verified via ensure_contained()
grep        → substring match, 200-hit cap
git_log     → git log --oneline -n 20
```

## 4. Fan-out orchestration — `orchestrator.py` + `scheduler.py`

```
                        orchestrator.run()
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼        (and risks,
   structure()           dependencies()         entrypoints()    each with
   tools: list_tree      tools: grep,           tools: list_tree, ISOLATED
   read_file             read_file,            read_file,        context —
                         list_tree             git_log           16K chars
 ┌────────────────────────┐  ┌────────────────────────┐  ┌───────────────────────┐
 │ Subagent instance #1   │  │ Subagent instance #2   │  │ Subagent instance #4  │
 │ own message list       │  │ own message list       │  │ own message list      │
 └────────────────────────┘  └────────────────────────┘  └───────────────────────┘
        │                          │                              │
        └─────────────┬────────────┴──────────────┬───────────────┘
                      ▼                           ▼
        ┌──────────────────────────────┐  ┌──────────────────────────────┐
        │ fan_out(list, worker,       │  │ Semaphore(4) — at most 4     │
        │      max_concurrency=4,     │  │ LLM calls in flight at once  │
        │      max_retries=2,         │  │ (this is the burst against   │
        │      backoff=0.5s)          │  │ Gemini's 5 req/min free tier)│
        └──────────────────────────────┘  └──────────────────────────────┘
                      │
                      ▼  every aspect → to_dict()
        ┌──────────────────────────────┐
        │ aspects = {                 │
        │   "structure":  {ok,steps,  │
        │               tool_calls,   │
        │               content},     │
        │   "dependencies": {...},    │
        │   ...                       │
        │ }                           │
        └──────────────┬───────────────┘
                       ▼  json.dumps(aspects, indent=2)  ← fed whole to:
        ┌──────────────────────────────┐
        │ SYNTH_SPEC ("synthesizer")   │   frontier model, no tools,
        │ tasks: merge findings →      │   1 call, expects:
        │ {"summary":"...","modules":  │   {"summary": "...", "modules": ["core", ...]}
        │  [...]}                      │
        └──────────────┬───────────────┘
                       ▼
        AnalysisReport {job_id, repo_url, commit_sha, aspects, summary,
                        graph_stats, created_at}  → report.json + PERSISTED
```

## 5. Code graph construction — `graph.py`

```
 repo/                      modules (dotted)       symbols          imports
 ├── src/ ────────────────► "clio.orchestrator"     Orchestrator      clio.clone
 │   ├── orchestrator.py                            Orchestrator.run  clio.graph
 │   ├── graph.py      ──► "clio.graph"             RepoGraph        ast
 │   ├── subagent.py   ──► "clio.subagent"          Subagent.run     clio.llm
 │   └── __init__.py   ──► "clio"                   (package)
 └── tests/
     └── test_x.py    ───► "tests.test_x"

 │        │             │
 │        ▼             ▼
 │  parse_module():  ast.parse → visitor walks:
 │    visit_Import / visit_ImportFrom   → imports: module → [targets]
 │    visit_ClassDef                   → symbols: kind=class
 │    visit_FunctionDef                → symbols: kind=function|method
 │    visit_Call                       → calls:   CallEdge(caller, callee, line)
 │
 │     caller format:  "module::scope"          e.g. "clio.orchestrator::Orchestrator.run"
 │     callee resolve:  top-level name  → "module::name"
 │                      self/cls.attr   → "module::Class.method"
 │                      obj.attr        → "obj.attr" (best effort)
 │
 ▼
 GraphStore (SQLite)  ← saved as jobs/<job>.graph.db
    tables: modules, symbols, imports, calls — deduped on save
    queries: callers_of(symbol_id) → [(caller, line)]
             callees_of / modules_importing / module_imports / has_symbol
```

## 6. Impact analysis — the killer feature

```
                target: "clio.orchestrator::Orchestrator.run"
                             │
              ┌──────────────┴───────────────────┐
              ▼                                  ▼
   reverse traversal (callers, depth 3)   module importers
              │                                  │
              ▼                                  ▼
   affected  = { modules that would break }   union
              │
              ▼
   clusters_hit = cluster_by_package(graph)  (package-name clustering)
   verdict = "contained"        if ≤ 1 cluster hit
           | "cross-cutting"    if ≥ 2 clusters hit
           | "missing"          if symbol not in graph
              │
              ▼
   ImpactReport {scope, affected_modules, callers[(sym,line)],
                 clusters_hit, verdict}
   → to_dict() → served at /api/jobs/<id>/graph/map?impact=...
   → frontend: red ripple animation over the SVG nodes
```

## 7. Ask panel (persistent chat over tools) — `ask.py`

```
 user: "what calls app.service::greet?"
 │
 ▼
 AskSession.run_turn(q)          history: last 6 msgs carried in task text
 │                                 (session persists per job in _ask_sessions)
 ▼
 Subagent(spec="ask", model=cheap)  ← tool loop like §3, but toolset is:
 │
 ├── BUILTIN_TOOLS       read_file, list_tree, grep, git_log
 ├── graph_query         callers_of / callees_of / modules_importing /
 │                         module_imports / has_symbol   (SQLite lookups)
 ├── impact              impact_of_symbol(...)           (blast radius JSON)
 ├── list_jobs / get_report   (ReportArchive)
 │
 ▼
 each tool call → ask.tool SSE event → "⚙" tool-line in chat UI
 final answer   → ask.final SSE event  → bubble in chat UI
```

## 8. Event bus → SSE (everything visible)

```
                                     CLI (terminal)
 EventBus.publish(event)  ────────►  prints each event line + full report
        │
        ├─► orchestrator events: job.created → job.cloning → job.cloned
        │          → job.indexing → job.graphed → job.analyzing
        │          → subagent.start/tool/done (×4) → job.synthesizing
        │          → job.persisted  (or job.failed)
        │
        └─► Dashboard._publish → deque per job
                  │
                  ▼  GET /api/stream?job_id=...  (SSE, snapshot-drains queue)
        dashboard: log rows (t, type, data) + status lamp + live badge
```

## 9. Data flow in one line

```
URL → clone → AST graph → 4×LLM fan-out → 1×LLM merge → report+graph → SSE → map/ask/impact
```
# Clio

> **A repo analyzer with a visible nervous system.**
>
> Paste a GitHub repo URL. Clio spins up a sandbox, clones the repo, fans out
> parallel subagents to dissect every aspect of the codebase, and materializes
> a living architecture graph you can interrogate — including **what breaks
> if anything breaks**.

Built for students and developers who want to truly understand a codebase:
see the connections, explore the architecture, ask questions, and get
impact analysis with evidence.

---

## Why it's different

| Generic codebase chatbot | Clio |
|---|---|
| Reads files on request | Clones into a **sandbox** and indexes the whole repo first |
| Answers questions | Builds a **code graph** (imports, symbols, calls) and answers from it |
| Static replies | **Parallel subagents** analyze aspects concurrently — you watch it work |
| "What does X do?" | **"What breaks if X breaks?"** — blast radius with `file:line` evidence |
| Forgets between visits | Persists analysis + Q&A across sessions |

The harness execution **is** the demo: live subagent fan-out, phase timeline,
token meters, crash-safe resume.

---

## Architecture

```
Frontend (vanilla JS + vis.js)       ← live harness view, architecture
        │  SSE/WebSocket                 graph, impact mode, chat
FastAPI API layer
        │
Harness runtime (the showpiece)      ← orchestrator state machine, async
        │                                 scheduler, tool registry, subagent
        │                                 pool, model-agnostic adapter, event bus
Analysis pipeline                    ← ingest → index → fan-out →
        │                                 synthesize → graph-build → persist
Stores                               ← sandbox workspace, code graph (SQLite),
                                         knowledge store, session store
```

Details: see `blueprint.md` (not tracked — ask the author).

---

## Features (planned)

- **Sandboxed analysis** — the repo is cloned into an isolated workspace
  with timeouts, output caps, and disk guards; analysis tools never touch
  the network.
- **Parallel subagent fan-out** — module map, dependencies, data flow,
  entry points, risks, tests, docs — each aspect in its own isolated
  context window; frontend model for fan-out, strong model for synthesis.
- **Architecture map** — interactive graph of modules, files, symbols,
  and their relationships.
- **Impact analysis** — pick any file/symbol/module and get a ranked
  "what breaks if this breaks" report with evidence snippets.
- **Persistent Q&A** — ask follow-ups across sessions; the analysis is
  remembered, not re-read.

---

## Tech stack

- **Python 3 + FastAPI** (async runtime, SSE event streams)
- **tree-sitter** (language-aware code parsing)
- **SQLite** (code graph + sessions)
- **sentence-transformers** (local embeddings for retrieval — free)
- **vanilla JS + vis.js** (frontend, no build step)
- **Model-agnostic LLM adapter** (Gemini free tier first, any provider later)

---

## Status

| Phase | Status |
|---|---|
| Blueprint | ✅ Done |
| M0 — sandbox + clone + tree tools | ✅ Done |
| M1 — harness runtime (events, tools, subagents, scheduler, orchestrator, CLI) | ✅ Done |
| M2 — code graph (extraction, SQLite store, clustering) | ✅ Done |
| M3 — synthesis + persistence (report archive) | ✅ Done |
| M4 — impact analysis | ✅ Done |
| M5 — frontend (zero-dependency live dashboard: SSE stream + archive API) | ✅ Done |
| M6 — evals + benchmark (golden repo suite, deterministic graders, phase timing) | ✅ Done |
| M7 — LLM providers (.env config, urllib Gemini, Groq client, provider factory) | ✅ Done |
| M8 — agentic Ask panel (chat over sandboxed tools, SSE) + theme system | ✅ Done |
| M9 — architecture map (deterministic SVG module map, hover tracing, impact mode) | ✅ Done |

Project-based learning, built in public. No deadline — depth first.

---

## Roadmap highlights

- Impact analysis with rank of breakage categories (compile/runtime, tests,
  data-flow, public API)
- GoldenRepo eval suite + harness ablations (fan-out on/off, graph
  on/off, model swap) with cost/latency/accuracy curves
- Stretch: change-aware re-analysis ("what changed in this architecture
  since March?"), multi-repo comparison
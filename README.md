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
Dashboard (vanilla JS + SSE, no build step)
        │  SSE event stream / JSON API
Local HTTP server (stdlib http.server)
        │
Harness runtime (the showpiece)      ← orchestrator state machine, async
        │                                 scheduler, tool registry, subagent
        │                                 pool, model-agnostic adapter, event bus
Analysis pipeline                    ← ingest → index → fan-out →
        │                                 synthesize → graph-build → persist
Stores                               ← sandbox workspace, code graph (SQLite),
                                         report archive, session store
```

Details: see `blueprint.md` (not tracked — ask the author).

---

## Quickstart

Requirements: Python 3.11+ (no third-party dependencies).

```bash
# 1. install (editable, so `python -m clio` works from anywhere)
pip install -e .

# 2. configure your provider key
copy .env.example .env        # then fill in GEMINI_API_KEY (or GROQ_API_KEY)

# 3a. run the dashboard (terminal shows live logs; clio.log gets full tracebacks)
python -m clio.web            # → http://127.0.0.1:8790

# 3b. or headless CLI analysis
python -m clio https://github.com/user/repo.git

# tests
python -m pytest              # 195 tests, offline (uses a scripted fake LLM)
```

`.env` (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `CLIO_PROVIDER` | `gemini` | `gemini` or `groq` |
| `GEMINI_API_KEY` | — | required for the Gemini provider |
| `GROQ_API_KEY` | — | required for the Groq provider |
| `CLIO_CHEAP_MODEL` | `gemini-2.5-flash` | model for fan-out subagents |
| `CLIO_FRONTIER_MODEL` | `gemini-2.5-flash` | model for synthesis/merge |

If a provider key is missing or the API rejects the request, the error is
logged to the terminal and `clio.log` (never printed as a raw URL — `?key=`
is redacted).

---

## How to try it

1. Start the dashboard (`python -m clio.web`), paste a repo URL — anything
   on GitHub works; for a quick smoke test, point it at a small repo.
2. Watch the **event ledger** stream live: clone → graph → subagent fan-out
   → synthesis → persist. The status lamp glows while a job runs.
3. Click a job in **Job history** → the JSON report and the **Module map**
   load. Hover modules to trace edges; click one and hit **Impact** to see
   which modules break if it breaks.
4. Open **Ask** (top right) and interrogate the repo — the model answers
   over sandboxed tools (`list_tree`, `graph_query`, `impact`, …) and you
   watch each tool call stream in.
5. CLI equivalent: `python -m clio <url>` prints the event stream, then the
   report; add `--impact app::serve` for a symbol's blast radius.

## What to review

- **Frontend** — dashboard redesign (cards, system font stack, dark/light
  theme, rounded module map, chat-style Ask panel). Screenshots from the
  final QA pass: `C:\Users\omnaw\AppData\Local\Temp\opencode\shots\`.
- **Logging** — `src/clio/logging.py` wires console + file logging; every
  job/ask failure now prints a full traceback to the launching terminal.
- **Map fix** — `resolve_module` now strips symbol suffixes, so
  `from clio.config import Limits` draws the right edge (was: missing edges
  on src-layout repos).
- **No mock anywhere** — `FakeLLM` exists only in tests; the product has
  zero canned responses.

---

## Features

- **Sandboxed analysis** — the repo is cloned into an isolated workspace
  with timeouts, output caps, and disk guards; analysis tools never touch
  the network.
- **Parallel subagent fan-out** — module map, dependencies, data flow,
  entry points, risks, tests, docs — each aspect in its own isolated
  context window; cheap model for fan-out, strong model for synthesis.
- **Architecture map** — deterministic SVG map of modules, clusters, and
  edges; hover tracing and impact mode.
- **Impact analysis** — pick any file/symbol/module and get a ranked
  "what breaks if this breaks" report with evidence.
- **Persistent Q&A** — ask follow-ups across sessions; the analysis is
  remembered, not re-read.

---

## Tech stack

- **Python 3.11+ stdlib only** — `http.server` (dashboard + SSE), `ast`
  (code graph), `urllib` (LLM API calls), `sqlite3` (graph store)
- **Hand-rolled SVG map** — zero-dependency frontend, no build step
- **Model-agnostic LLM adapter** — Gemini and Groq via raw REST

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
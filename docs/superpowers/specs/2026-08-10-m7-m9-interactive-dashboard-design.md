# M7–M9 Design — Real providers, agentic Ask panel, architecture map

- Date: 2026-08-10
- Status: Approved by user (2026-08-10). Sequence: M7 providers → M8 Ask panel + theming → M9 architecture map.
- Project: Clio (zero-dependency AI code-analysis harness). Repo: https://github.com/omhome16/Clio

## Goals

1. **M7 — Model provider layer**: Groq + Gemini as real providers behind the
   existing `LLMClient` interface, stdlib-only (urllib), configured via env
   or a git-ignored `.env`. Note: `pip install httpx` DOES work on the dev
   machine (outdated assumption in the M5 plan), but we stay stdlib for the
   project's zero-dependency identity.
2. **M8 — Agentic Ask panel**: a persistent chat in the dashboard that can
   answer any question about an analyzed repo by calling tools (files, graph
   queries, impact analysis, archive). Reuses the existing `Subagent` loop
   and `ToolRegistry` — no new agent framework. Plus a light/dark theme
   system for the whole dashboard.
3. **M9 — Architecture map**: interactive SVG module/edge map with impact
   mode, hand-rolled (no vis.js, no CDN), styled in both themes.

## M7 — Provider layer

### Design
- Rewrite `GeminiClient` to stdlib: `urllib.request` wrapped via
  `asyncio.to_thread` (keeps the async `complete()` contract). Same REST
  shape: `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=...`,
  `contents: [{role, parts:[{text}]}]`, `generationConfig.maxOutputTokens`.
  Key from `GEMINI_API_KEY` (env or `.env`).
- New `GroqClient`: OpenAI-compatible
  `POST https://api.groq.com/openai/v1/chat/completions` with
  `{"model", "messages", "max_tokens"}`. Key from `GROQ_API_KEY`.
  Defaults: cheap `llama-3.3-70b-versatile`, frontier `llama-3.3-70b-versatile`
  (free tier), overridable via `CLIO_CHEAP_MODEL` / `CLIO_FRONTIER_MODEL`.
- `.env` loader in `config.py`: ~12-line stdlib parser; values are read in
  order: real env vars win, then `.env` file, then defaults. `.env` already
  git-ignored; commit `.env.example` with placeholders.
- `make_client(provider: str, limits) -> LLMClient` factory in `llm.py`.
  Provider names: `mock`, `gemini`, `groq`. `CLIO_PROVIDER` env selects the
  default; CLI gains `--provider groq` (mock|gemini already exist);
  dashboard analyze endpoint uses `CLIO_PROVIDER`.
- Keys: user-provided keys live only in the local `.env` (never committed).

### Tests (TDD, mocked HTTP, no network)
- Gemini: request URL/shape, response parsing (multi-part text), key-missing
  error, model override.
- Groq: request shape (OpenAI format), response parsing, non-200 handling.
- `make_client`: provider resolution + env override.
- Target: +9 tests → 156.

## M8 — Agentic Ask panel + theme system

### Design
- `ChatSession` (in `web.py`/new `ask.py`): persistent message context per
  job; runs the existing `Subagent` loop with `ToolRegistry` seeded with
  chat tools:
  - `read_file(path)` — sandbox-contained
  - `grep(pattern, module=None)`
  - `list_tree(dir=None)`
  - `graph_query(kind, symbol_id|module)` — callers_of, callees_of,
    modules_importing, module_imports, has_symbol
  - `impact(symbol_id)` — ImpactReport dict
  - `list_jobs()` / `get_report(job_id)` — archive
  - Tool registry: `clio.tools` already provides read_file/grep/list_tree
    with sandbox containment and output caps — reuse them.
- SSE endpoint `GET /api/ask?job_id=<id>&q=<text>`: one turn per request;
  emits `ask.tool` (name+args+result), `ask.final` (answer), then
  `event: done`. Chat context lives on the Dashboard (per job id).
- UI: collapsible Ask sidebar (right). Bubbles, tool calls shown inline
  ("`impact(...)` → cross-cutting"), streaming answer, Enter to send,
  disabled while running, empty-state copy, error handling (provider
  errors shown as failures, retry allowed).
- Theme system: CSS custom properties under `[data-theme="light"|"dark"]`
  on `<html>`; toggle button in masthead; default `prefers-color-scheme`;
  persisted in localStorage. Dark palette = cyanotype system inverted
  (deep warm ink paper, brightened Prussian blue, re-checked contrast
  ratios >= 4.5:1). Applies to ledger, tables, report, chat, and later map.

### Tests
- Chat tools (each tool's args/return shape, sandbox guards).
- ChatSession loop with mock provider (multi-turn, tool→final path).
- Ask SSE endpoint (stream content, done marker, unknown job).
- Theme: JS toggle presence, default from prefers-color-scheme.
- Target: +11 tests → 167.

## M9 — Architecture map + impact mode

### Design
- Backend: extend graph API with `/api/jobs/<id>/graph/map` returning
  `{"nodes": [{id, module, cluster, symbols, x, y}], "edges": [{from, to, kind}]}`
  where layout (x,y) is computed deterministically server-side:
  `cluster_by_package` → one column per cluster, modules stacked with
  symbol-count-based sizing; edges = imports + resolved calls.
- Frontend: inline SVG in the dashboard (both themes). Hover highlights
  neighbors; click a module opens a detail panel (symbols, modules list,
  "impact" button); impact mode animates reverse-edge propagation in red
  (CSS/SMIL, `prefers-reduced-motion` respected) and lists ranked affected
  modules from `impact_of_module`.
- Determinism: same repo+job → same coordinates (sorted iteration).

### Tests
- Map payload endpoint (nodes/edges present, deterministic coords, 404).
- Layout function unit tests (clusters → columns, stable ordering).
- Impact-mode data (affected list + verdict from impact_of_module).
- Target: +10 tests → 177.

## Shared decisions
- All milestones: TDD via subagents, plan docs in `docs/plans/`, branch
  `feat/m<id>-...`, full-suite green before merge, live demo, README status
  update, merge to main, push, delete branch.
- No new runtime dependencies anywhere.
- Keys never enter git; `.env.example` is the committed template.

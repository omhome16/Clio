# Clio v2 — Rework Design (2026-08-14)

Status: approved by user on 2026-08-14.

## Context

Clio ("paste any link → it teaches you the repo from the ground up") failed
user acceptance testing on three observed failures when analyzing
FinEdge-RAG-chatbot:

1. "Run it" guide stage: "no specific commands or scripts were found" —
   root cause: `run_hints()` scans only the first 2000 chars of the README
   (`readme_head`); FinEdge's first code fence is at char 3,788. Command
   regex misses `git`, `cd`, `source`, `cp`, `uvicorn`; only root
   `package.json`/`Makefile` are scanned (scripts live in `frontend/`).
2. "What it is" / chat overview answers: shallow or evasive ("the provided
   excerpts do not state what the entire project does") — the 2000-char head
   is mostly `shields.io` badge HTML; overview-question tokens are stopwords
   so BM25 gets almost nothing; nested READMEs outrank the top-level README
   intro; the system prompt invites the cop-out; no repo-level context in
   the chat prompt.
3. Junk citations (`golden_set.csv`, `.gitkeep`) — every text file is
   indexed equally; docs/code/configs/data have no tiers.

## Decisions (user-approved)

- Retrieval backbone: **pure lexical + graph** (repo map + call-graph + BM25
  + query-understanding). No embeddings, no vector DB, stdlib only.
  Rationale: Cody publicly deprecated embeddings; Aider's repo map alone hit
  70.3% file-identification on SWE-bench; community hybrid tests show
  BM25+identifier ≈98%.
- Guide: **same 4-tab rail** (what/how/modules/run), internals rebuilt.
- Chat sessions: **compaction + memory bank** (keep-tail + structured
  summary at budget threshold + per-job activeContext.md/progress.md).
- Eval: **git-history gold set** harness (fix-commits → gold files; MRR,
  Recall@k, budgeted coverage BCY@8k, abstention subset).

## Constraints (unchanged)

- Zero-dependency Python stdlib; single process; `http.server` + SSE.
- ONE cheap LLM (gemini-2.5-flash); no agentic loops anywhere in the
  product path.
- Citations always deterministic (from retrieval, never the model).
- Guide always complete via deterministic fallback.
- Existing test suite stays green (≥256 at start; grows per component).

## Architecture

```
URL ─► CLONE ─► GRAPH (ast/regex → modules/symbols/imports/calls, SQLite)
                │
                ▼
        INDEX-V2 (per job, mtime/content-hash cached)
        ├─ symbol chunks: one function/class per chunk, headers
        │   (# path, # class, signature, docstring line), FQN IDs,
        │   module-skeleton + class-skeleton chunks
        ├─ doc tier: README/docs/AGENTS.md/CLAUDE.md ranked as docs,
        │   top-level README chunk-1 boost
        ├─ repo map: signatures ranked by personalized PageRank on
        │   the file-reference graph, binary-search fit to ~1.5K tokens
        └─ signal index: symbols, paths, modules, call edges, import neighbors
                │
   ┌────────────┴─────────────────────────────┐
   ▼                                          ▼
GUIDE-V2 (4-tab rail, internals rebuilt)    CHAT-V2
 ├─ what:   full-README analysis (badges     ├─ query understanding: ONE flash
 │          stripped) + entries + repo map     call → symbols/paths/terms
 │          → evidence bundle → synthesis   ├─ hybrid retrieval: BM25 + symbol +
 │          with inline citations → lint      path + module + call-graph +
 ├─ how:    graph walk from entrypoints       import-neighbor, fused by RRF
 ├─ modules: repo map + module table +      ├─ budget pack (headers, dedup)
 │          hot modules (fan-in rank)       ├─ ONE completion + citation lint
 ├─ run:    FULL README fences + git/cd/    │  (every [path:line] verified to
 │          source/cp/uvicorn added +        │  exist and be in the pack)
 │          nested package.json/Makefile/   └─ session memory: keep-tail +
 │          requirements.txt/docker-compose    structured compaction + per-job
 └─ each:   clio.json steering + CLAUDE.md    activeContext.md (memory bank)
           honored; deterministic fallback    + resume-from-summary
```

## Components

### 1. Index-V2 — `src/clio/retrieval.py` (rewrite)

- **Symbol chunks**: use the existing graph pass. For Python modules use
  `ast`-derived line ranges; one chunk per `FunctionDef`/`AsyncFunctionDef`/
  `ClassDef` (with decorators + leading docstring), oversized symbols split
  at statement boundaries with repeated headers. For non-Python languages
  keep line-based chunks but attach graph symbols.
- **Chunk headers**: `# path`, `# in class X`, full signature, first
  docstring line; FQN (`module::Class.method`) as stable chunk ID.
- **Skeleton chunks**: per file a "module skeleton" chunk (all signatures,
  bodies elided — imports attached, since naive chunkers put import blocks
  in top-k), per large class a class-skeleton chunk.
- **Doc tier**: `README*`, `docs/**`, `AGENTS.md`, `CLAUDE.md`,
  `.windsurfrules` → doc flag; doc chunks get a doc bonus; top-level README
  chunk 1 gets an extra bonus.
- **Signals + RRF fusion**: exact symbol match, identifier-substring match,
  path match, BM25 (over header+body terms), call-graph (callers of matched
  symbols, callers of callees), import-neighbor BFS. Merge top-k lists by
  Reciprocal Rank Fusion (score = Σ 1/(k+rank)).
- **Repo map**: file-reference graph (referencer→definer edges from the
  graph pass), personalized PageRank via power iteration (~30 lines),
  signature-only rendering, binary-search fit to target budget
  (default ~1.5K tokens; expand when question has no file anchors),
  mtime-keyed render cache.
- **Content-hash cache**: store `hash(file)` in SQLite; on rebuild, skip
  unchanged files. (P2+, if cheap.)
- **Budget discipline**: hard caps on packed chars; track estimated usage.

### 2. Guide-V2 — `src/clio/guide.py` (rewrite of internals)

- **`what`**: full README with badge/HTML noise stripped (strip `![..](..)`,
  `https://img.shields.io` lines, `[![..](..)]` links), plus entry points +
  repo map excerpt. Evidence bundle with `--- marker ---` blocks.
- **`how`**: entry points + `call_edges_for` + import-neighbor modules.
- **`modules`**: repo map + module table + hot modules (fan-in from
  `graph.imports`) + clusters.
- **`run`**: `run_hints` v2 — scan the FULL README (all `README*`), regex
  widened (`git`, `cd`, `source`, `cp`, `uvicorn`, `curl`, `ssh`, `set`,
  `export`, `touch`, `mkdir`), scan nested (depth ≤2) `package.json`,
  `Makefile`, `requirements*.txt`, `pyproject.toml` (project.scripts),
  `docker-compose*.yml`, `justfile`, `Gemfile` (`bundle install`), `Cargo.toml`
  (`cargo run`). Keep dedupe + cap 12.
- **Evidence bundles + citation lint**: each stage gets numbered evidence
  blocks; system prompt requires `[marker]` inline citations; lint pass
  verifies every cited path exists in the sandbox; on lint failure, fall
  back to deterministic facts.
- **Steering**: optional `clio.json` at repo root (`repo_notes`,
  `run_commands`, stage overrides) merged into evidence; honor repo
  `CLAUDE.md`/`AGENTS.md` (capped) as additional `what`/`modules` evidence.
- **Fallback**: deterministic facts verbatim (unchanged safety property).

### 3. Chat-V2 — `src/clio/ask.py` (rewrite)

- **Query understanding**: if the question names no exact symbol/path
  (check against index first), one flash call extracts candidate symbols,
  paths, and search terms; these feed retrieval. Cache per normalized
  question. Fail-soft: on any error, use raw question.
- **Retrieval**: Index-V2 `search()` (hybrid + RRF + budget pack).
- **Overview questions**: doc tier + repo map + module table in the pack;
  system prompt variant for overview intents ("synthesize from all
  evidence; do not refuse").
- **Specific questions**: exact-symbol + call-graph path; "never invent;
  say what is missing" retained.
- **Citation lint**: after completion, verify every `[path:line]` anchor
  exists in the sandbox and was in the pack; drop/flag invalid ones.
- **Session memory**:
  - keep-tail: last 6 turns verbatim, ≤3 KB budget (existing).
  - compaction: when history exceeds a budget, one flash call writes a
    structured summary (objective, files+decisions, open questions, next
    steps); summary + recent turns become the history; old turns preserved
    non-destructively in the session store.
  - memory bank: per-job `activeContext.md` + `progress.md` written at
    session end (next steps, key files, decisions); new sessions on the
    same job load them into the system context.
  - resume-from-summary: sessions > threshold offer summary-based resume.

### 4. Eval harness — `tests/eval/`

- `goldset.py`: clone (or use sandbox) a repo; `git log`; filter fix-style
  commits (message matches `fix|bug|close|resolve|hotfix` patterns, exclude
  docs-only via `--stat`); gold files = files changed; query = commit
  message (cleaned). Emits `goldset.jsonl`.
- Metrics: MRR, Recall@k (k=1,5,8), budgeted coverage BCY@8k, abstention
  subset (queries whose gold set is docs-only → retrieval should return
  nothing useful; measures the selective gate).
- `run_eval.py`: scores Index-V2 `search()` over a gold set; prints a
  table. Baseline run on current code first, then after P1/P2/P3.
- Target repos: clio itself, FinEdge-RAG-chatbot (sandbox copy), 2 others
  (e.g. a small Python lib with active history).

### 5. Frontend — `src/clio/web.py` (minimal)

- Keep the 4-tab rail and chat UI. Add per-guide-section source chips
  (from evidence markers). No new screens; no build step.

## Phasing

- **P0** Baseline: run eval harness against current code; record numbers.
- **P1** Evidence fixes (guide.py run_hints v2 + what-stage README
  analysis + doc tier in retrieval + chat prompt variants). Smallest code,
  fixes observed failures. Verify with re-analysis of FinEdge.
- **P2** Index-V2: symbol chunks + headers + skeleton chunks + RRF + repo
  map + query understanding.
- **P3** Chat session memory: compaction + memory bank + resume.
- **P4** Harness integration, full suite, docs (README + architecture.md
  updated), final FinEdge + self analysis.

## Testing

- Existing suite stays green; new unit tests per component:
  - `test_retrieval.py`: symbol chunk boundaries, headers, RRF ordering,
    doc tier, repo map budget fit, PageRank sanity.
  - `test_guide.py`: run_hints v2 (full README, nested scripts, widened
    regex), evidence lint pass/fail, clio.json steering.
  - `test_ask.py`: query understanding, compaction, memory bank
    read/write, citation lint.
  - `tests/eval/test_goldset.py`: goldset extraction determinism.
- Verification: `python -m pytest -q` green; dashboard manual test by user.

## Out of scope (explicitly not stealing)

Embeddings/vector DB, MCP, LSP, subagent child-session orchestration,
agentic loops, shadow-git checkpoints, per-module wiki pages, model
routing (only one model available).

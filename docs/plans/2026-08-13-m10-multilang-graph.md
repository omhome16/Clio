# M10 — Multi-language repo graph + hardening

Date: 2026-08-13
Spec: `docs/spec.md` M10 section (informal — this plan *is* the spec)
Current suite: 228 passed (0 failed)
Target suite: 232 passed (+4)

## What

Make Clio's code graph useful for non-Python repositories and harden the
pipeline around it:

1. **Regex-tier extractors** (`src/clio/extractors.py`) — file-level imports
   and top-level symbols for 12 languages: JavaScript/TypeScript, Go, Rust,
   Java, C/C++, C#, Ruby, PHP, Kotlin, Swift, Bash. Python keeps the deep
   `ast` tier (symbols + resolved call edges); the store and clustering layers
   stay language-agnostic.
2. **Language-aware clustering** — foreign modules are slash-path ids
   (`cmd/util`), so `cluster_by_package` splits on both `.` and `/`.
3. **Language metadata** — per-module language persisted in the SQLite
   snapshot (`meta.languages`), exposed via `RepoGraph.language_stats()` /
   `GraphStore.language_stats()`, surfaced in the graph API, the map panel
   footer, `job.graphed` events, and `report.json`.
4. **Go import resolution** — imports under the repo's `go.mod` module path
   are stripped to in-repo paths (`github.com/acme/app/cmd/util` →
   `cmd/util`) so impact analysis works for Go repos.
5. **Slash-aware impact matching** — `modules_importing` also matches
   `/`-prefix submodules, not just dotted ones.

Plus the uncommitted hardening batch this milestone picked up: LLM rate
limiter + retryable error taxonomy + FakeLLM, CLI logging entrypoint, repo
map (PageRank fit-to-budget) + entrypoint/risk packing in the orchestrator,
and web job lifecycle (delete/clear/tree).

## Design

### Extractors — `src/clio/extractors.py`

- `detect_language(path)` — suffix map, `None` for unknown.
- `foreign_module_name(path, root)` — posix rel path sans extension.
- `extract(text, lang, importer_dir="", go_module="")` →
  `(symbols [(name, kind, line)], imports [str])`.
- Import patterns per language; JS/TS relative specifiers (`./x`, `../y`)
  resolved against the importer's directory to repo-relative paths.
- Symbol patterns per language, one symbol per name/kind, line = 1-based.
- Deliberately no call edges for foreign languages (file-level tier).

### Graph — `src/clio/graph.py`

`build_repo_graph` iterates *all* source files (`iter_source_files`) and
dispatches: `.py` → `ast` tier; other detected languages → regex tier;
everything else (config/data/markdown) skipped. `RepoGraph.languages` maps
module → language; `language_stats()` counts per language.

### Clustering — `src/clio/clustering.py`

`_segments()` splits on both separators so `src/util.ts` clusters under
`src` next to Python packages.

### Store — `src/clio/store.py`

`save` writes `languages` as JSON into `meta`; `load` rehydrates it.
`language_stats()` counts from meta without loading the whole graph.
`stats()` contract (4 ints) unchanged.

### Go resolution

`_go_module_path(root)` parses `go.mod`; `extract(..., go_module=...)`
strips the prefix from matching imports. External imports untouched.

### Impact

`modules_importing` gains `target.startswith(module + "/")` alongside the
dotted prefix, so directory-level queries match foreign modules.

## Risks

- **`stats()` contract** is pinned by tests — do not add keys to it; new
  counts go to `language_stats()`.
- **`top_prefix` semantics** changed for dotted modules — verify the
  existing clustering tests still pass.
- **Deduplication**: `extract` dedupes symbols per (name, kind); imports
  deduped and capped (300 symbols / 80 imports per file) against pathological
  files.
- **Line endings** (CRLF warnings on Windows) are cosmetic; `git diff`
  ignores whitespace-only noise.
- **Regex tier is best-effort** — misses on exotic syntax are acceptable;
  it must never crash the graph build.

## Final run

Run: `python -m pytest -q`
Expected: 239 passed, 0 failed.

## Definition of done

- Mixed-language repo (Python + TS + Go + Ruby) produces one graph; clusters
  are path-aware; languages persist across save/load.
- `/api/jobs/<id>/graph` returns `languages`; map panel footer shows the
  breakdown; `report.json` graph section carries it.
- Go repos: internal imports match module ids (impact works).
- `modules_importing` matches `/` submodules.
- Suite green; README status table updated; committed.

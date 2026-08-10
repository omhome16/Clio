# M9 — Architecture Map

Date: 2026-08-10
Spec: `docs/spec.md` M9 section
Current suite: 184 passed (0 failed)
Target suite: 194 passed (+10)

## What

Interactive architecture map of a job's module graph, rendered as inline SVG in the
dashboard (both themes). Deterministic server-side layout; hover highlights neighbors;
click opens a module detail panel; impact mode animates reverse-edge propagation in red
(respecting `prefers-reduced-motion`) and lists ranked affected modules.

## Design

### Backend — new `src/clio/map.py`

`layout_graph(graph: RepoGraph) -> dict` returns `{"nodes": [...], "edges": [...]}`:

- **Nodes** — one per module, with metadata from the layout: `{id, module, cluster,
  symbols, x, y}` where `symbols` = count of graph symbols in that module.
- **Layout** (deterministic):
  - Columns = `cluster_by_package(graph, 1)` sorted by name. `x = col * COL_W`
    (COL_W = 260), module x centered in the column.
  - Within a column, modules sorted by name, stacked top-down: `y = row * ROW_H`
    (ROW_H = 120). No randomness anywhere — same repo + job => same coordinates.
- **Edges** — `{from, to, kind}` where `kind` is `"import"` | `"call"` | `"both"`:
  - Imports: `graph.imports` where src is a module and the target *resolves* to a
    module node. Resolution rule (mirrors `clustering.connected_components`): module m
    matches target t when `m == t`, `m.startswith(t + ".")`, or `m.endswith("." + t)`
    (handles `src.clio.x` <-> `clio.x` aliasing). Skip self-edges.
  - Calls: from graph.calls — caller module = `caller.rsplit("::", 1)[0]`; callee must
    contain `"::"`, callee module = `callee.rsplit("::", 1)[0]`; resolve callee module
    like targets. Skip unresolvable and self edges.
  - Pair dedupe: one edge per (from, to); kind = `"both"` if both kinds exist.
  - Sorted by (from, to).

### Endpoint — `GET /api/jobs/<id>/graph/map`

Extend `do_GET` routing in `web.py`: check `rest.endswith("/graph/map")` BEFORE
`rest.endswith("/graph")`; `_job_map(job_id)` loads the graph via
`archive.get_graph(job_id)` (404 JSON if missing) and returns `layout_graph(graph)`.

Optional `?impact=<module>` query param: response gains
`"impact": impact_of_module(archive, job_id, module).to_dict()` (uses existing
`impact_of_module` — affected_modules already sorted, verdict from clusters hit).
The frontend uses this for impact mode.

### Frontend — SVG map in the dashboard

Added to INDEX_HTML, placed after the report section:

- New section: heading "Module map" + SVG container `<svg id="map">`.
- JS `loadMap(jobId)`: fetch map endpoint, render edges as `<line class="edge
  e-<kind>">` and nodes as `<g class="node" data-module>` with `<rect>` (sized by
  symbol count) + `<text>`. `viewBox` computed from max node x/y.
- Hover (`mouseenter`/`mouseleave`): highlight the node and its neighbors (both
  directions) — CSS `.map-hover`/`.map-neighbor` classes.
- Click node: fill module detail panel (`#map-detail`) — module name, cluster,
  symbol count, neighbors list, and an "impact" button.
- Impact mode: "impact" button fetches the map with `?impact=<module>`, then:
  - Lists `impact.affected_modules` ranked in the detail panel (top = breaks first,
    order as returned by impact_of_module).
  - SVG impact mode: `.impact` class on affected nodes + edges; red
    `animate`/CSS transition propagates along reverse edges using per-node
    `animation-delay` proportional to rank. `@media (prefers-reduced-motion:
    reduce)` disables the animation (instant highlight only).
- Both themes: map colors come from the existing CSS variables (`--color-edge`,
  `--color-accent`, `--color-surface`, etc.) — no hardcoded colors.

## Tasks

### Task 1 — layout module

Files:
- NEW `src/clio/map.py`
- NEW `tests/test_map.py`

Code:
- `layout_graph(graph)` per Design above. Use `clustering.cluster_by_package`.
- Symbols per module: `sum(1 for s in graph.symbols if s.module == module)`.
- Export `COL_W`, `ROW_H` constants for tests.

Tests (4):
1. `test_layout_is_deterministic` — same graph twice → identical payload.
2. `test_layout_columns_match_clusters` — modules of one cluster share x; different
   clusters have different columns; cluster order = sorted names.
3. `test_layout_nodes_carry_metadata` — node has id/module/cluster/symbols/x/y; y
   strictly increases with module name order within a column.
4. `test_layout_edges_imports_calls_and_dedupe` — fixture repo with an import, a
   resolved call, a self-import (skipped), and a pair with both kinds → edge set
   matches expected (from, to, kind) tuples exactly.

Run: `python -m pytest tests/test_map.py -q`
Expected: 4 passed.
Run: `python -m pytest -q`
Expected: 188 passed (184 + 4), 0 failed.
Commit: `feat(map): deterministic SVG architecture-map layout (+4 tests)`

### Task 2 — map endpoint

Files:
- EDIT `src/clio/web.py`
- EDIT `tests/test_web.py`

Code:
- `do_GET`: route `rest.endswith("/graph/map")` before the `/graph` branch.
- `_job_map(self, job_id)`: 404 JSON for missing graph; else parse `impact` query
  param (`self.query` dict already exists — reuse it), include impact payload when
  present, `_send_json(200, layout_graph(graph))`.

Tests (3):
5. `test_api_job_map_payload` — seeded job → 200, nodes/edges non-empty, node keys
   present; unknown job → 404.
6. `test_api_job_map_impact_param` — seeded job with import chain → `?impact=<module>`
   returns impact.verdict + affected_modules sorted; `?impact=unknown` → verdict
   "missing".
7. `test_api_job_map_deterministic_http` — two GETs → identical bodies.

Run: `python -m pytest tests/test_web.py -q`
Expected: all web tests pass (new 3).
Run: `python -m pytest -q`
Expected: 191 passed (188 + 3), 0 failed.
Commit: `feat(web): /api/jobs/<id>/graph/map endpoint with impact param (+3 tests)`

### Task 3 — SVG map UI

Files:
- EDIT `src/clio/web.py` (INDEX_HTML)

Code:
- Map section (SVG container + detail panel + impact-mode toggle) after the report
  section; CSS + JS per Design. `loadMap` on job selection (`showJob`); all map
  colors via CSS variables; reduced-motion guard.

Tests (3, presence only — JS is not executed):
8. `test_index_map_present` — INDEX_HTML has `id="map"` SVG, `graph/map` fetch, and
   the "Module map" heading.
9. `test_index_map_detail_panel` — detail panel element id + "impact" button string.
10. `test_index_map_reduced_motion` — INDEX_HTML contains a `prefers-reduced-motion`
    block gating the impact animation.

Run: `python -m pytest -q`
Expected: 194 passed (191 + 3), 0 failed.
Commit: `feat(web): SVG architecture map with impact mode (+3 tests)`

### Task 4 — final review

- README: M9 row → ✅ Done.
- Live demo: seed a temp repo (multi-package), verify `/graph/map` + `?impact` over
  HTTP with urllib; spot-check payload shape.
- Full suite `python -m pytest -q` → 194 passed.
- Commit README, push.

## Risks

- **Route ordering**: `/graph/map` must be checked before `/graph` (the latter
  matches the longer path only via endswith — the `/graph/map` check must come first).
- **Call resolution**: callees without `::` are function-local calls — skip, don't
  crash. `graph.calls` keys are caller symbol ids.
- **Self-edges** (module imports itself via dotted prefix) — skip.
- **Impact depth**: `impact_of_module` default depth 3 — fine for the demo; no knob
  needed.
- **Existing `/graph` handler** (`_job_graph`) must remain untouched and keep its
  tests green.

## Final run

Run: `python -m pytest -q`
Expected: 194 passed, 0 failed.

## Definition of done

- `/api/jobs/<id>/graph/map` returns deterministic nodes+edges; `?impact=` adds
  ranked affected modules + verdict.
- Dashboard shows the SVG map in both themes; hover highlights neighbors; click opens
  the detail panel; impact mode animates red propagation (reduced-motion safe).
- 194 passed; README updated; pushed to GitHub.

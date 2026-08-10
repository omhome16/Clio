# M3+M4 — Report archive + impact analysis

- Date: 2026-08-10
- Branch: `feat/m3-m4-persistence-impact`
- Precondition: M0+M1+M2 merged to `main` (112 tests passing)
- Target: 128 tests passing (112 + 1 store `has_symbol` + 6 reports + 7 impact + 2 cli)
- Offline-friendly: zero network tests, zero new dependencies

## What M3+M4 deliver

M1 already delivered the synthesis phase itself (synthesizer subagent merging aspect
findings) and raw persistence (job records, `report.json`, `graph.db`). This plan closes
the two remaining milestones:

1. **M3 — queryable report archive** (`reports.py`): `ReportArchive` over the jobs
   directory — list reports, get one by id, latest by `created_at`, load a job's
   `RepoGraph` back from its `.graph.db`. Corrupt/missing artifacts degrade to `None`/`[]`.
2. **M4 — impact analysis** (`impact.py`): the flagship. Given a symbol id
   (`module::name`) or module, answer **"what breaks if X breaks"**:
   - `impact_of_symbol` — walk *reverse* call edges (callers → their callers, up to
     `depth`) plus reverse import edges of the symbol's module; collect affected
     modules, the caller edges, and which clusters are hit.
   - `impact_of_module` — transitive importers (up to `depth` hops).
   - Verdict: `missing` (symbol/module not in graph), `contained` (one cluster hit),
     or `cross-cutting` (2+ clusters) — the signal that a change ripples across
     architecture boundaries.
3. **CLI**: `python -m clio.cli <url> --impact module::symbol` runs the normal pipeline
   then prints `IMPACT:` JSON instead of `REPORT:`.

## Design decisions

- **Reverse edges only need what the store already answers.** `callers_of` gives the
  reverse call graph; `modules_importing` gives reverse imports (exact + submodule
  prefix match). No new schema.
- **Depth semantics.** Symbol scope: `depth` = number of caller hops walked (depth 1 =
  direct callers only). Module scope: `depth` = import hops beyond the module itself.
- **Clustering reuse.** Cluster membership comes from the existing
  `cluster_by_package` (depth 1), so the verdict reflects package-boundary crossing.
- **`has_symbol` added to GraphStore** (one-line query, tested) so `impact_of_symbol`
  can distinguish "no callers" from "symbol does not exist".
- **CLI stays backward compatible**: `--impact` is an optional flag; plain `clio <url>`
  behavior (REPORT output) is unchanged.

## Contracts

- `clio.store.GraphStore.has_symbol(symbol_id) -> bool`
- `clio.reports.ReportArchive(root)` with `list_reports() -> list[dict]`,
  `get_report(job_id) -> dict | None`, `latest() -> dict | None`,
  `get_graph(job_id) -> RepoGraph | None`, `graph_store(job_id) -> GraphStore`.
- `clio.impact.ImpactReport(scope, affected_modules, callers, clusters_hit, verdict)`
  with `to_dict()`; `impact_of_symbol(archive, job_id, symbol_id, depth=3)`,
  `impact_of_module(archive, job_id, module, depth=3)`.
- `clio.cli`: `--impact <symbol_id>` flag on the existing parser.
- `tests/conftest.py`: new `seed_job` fixture (writes a graph.db + report.json for a job).

---

## Task 1: `has_symbol` + `ReportArchive`

### Step 1: Add the `seed_job` fixture to `tests/conftest.py`

Append to the current `conftest.py` (imports at the top stay as they are; add these
imports if not present: `import json`, `from clio.graph import build_repo_graph`,
`from clio.store import GraphStore`):

```python
@pytest.fixture
def seed_job(write_tree):
    """Seed a persisted job: graph.db + report.json for one job_id."""
    def _seed(root: Path, job_id: str, created_at: str, files: dict[str, str]) -> None:
        graph = build_repo_graph(write_tree(files))
        jobs = root / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        GraphStore(jobs / f"{job_id}.graph.db").save(graph)
        report = {
            "job_id": job_id, "repo_url": "https://github.com/x/y.git",
            "summary": "merged", "created_at": created_at,
        }
        (jobs / f"{job_id}.report.json").write_text(json.dumps(report), encoding="utf-8")
    return _seed
```

### Step 2: Append `has_symbol` to `src/clio/store.py`

Add this method to `GraphStore` (after `symbol_ids_in`):

```python
    def has_symbol(self, symbol_id: str) -> bool:
        with self._session() as conn:
            row = conn.execute(
                "SELECT 1 FROM symbols WHERE id = ?", (symbol_id,)
            ).fetchone()
        return row is not None
```

### Step 3: Append to `tests/test_store.py`

```python
def test_has_symbol(tmp_path, write_tree):
    root = write_tree({"one.py": "def f():\n    return 1\n"})
    db = tmp_path / "graph.db"
    GraphStore(db).save(build_repo_graph(root))
    store = GraphStore(db)
    assert store.has_symbol("one::f")
    assert not store.has_symbol("one::missing")
```

### Step 4: Write `tests/test_reports.py`

```python
# tests/test_reports.py
from clio.reports import ReportArchive


def test_list_reports(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": ""})
    seed_job(root, "job-2", "2026-08-10T01:00:00+00:00", {"b.py": ""})
    reports = ReportArchive(root).list_reports()
    assert [r["job_id"] for r in reports] == ["job-1", "job-2"]
    assert all(r["summary"] == "merged" for r in reports)


def test_list_reports_empty(tmp_path):
    assert ReportArchive(tmp_path).list_reports() == []


def test_get_report(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": ""})
    archive = ReportArchive(root)
    assert archive.get_report("job-1")["job_id"] == "job-1"
    assert archive.get_report("nope") is None


def test_latest(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": ""})
    seed_job(root, "job-2", "2026-08-10T01:00:00+00:00", {"b.py": ""})
    assert ReportArchive(root).latest()["job_id"] == "job-2"
    assert ReportArchive(tmp_path / "empty").latest() is None


def test_get_graph(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": "def f():\n    return 1\n"})
    archive = ReportArchive(root)
    graph = archive.get_graph("job-1")
    assert graph is not None and graph.symbol_count == 1
    assert archive.get_graph("nope") is None


def test_corrupt_report_skipped(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "bad.report.json").write_text("{not json", encoding="utf-8")
    assert ReportArchive(tmp_path).list_reports() == []
    assert ReportArchive(tmp_path).get_report("bad") is None
```

### Step 5: Run — verify FAIL

Run: `python -m pytest tests/test_reports.py -v`
Expected: collection error — `cannot import name 'ReportArchive' from 'clio.reports'`.

### Step 6: Write `src/clio/reports.py`

```python
# src/clio/reports.py
"""Queryable archive over persisted job artifacts (reports + graph dbs)."""
from __future__ import annotations

import json
from pathlib import Path

from clio.graph import RepoGraph
from clio.store import GraphStore


class ReportArchive:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _jobs_dir(self) -> Path:
        return self.root / "jobs"

    def _report_path(self, job_id: str) -> Path:
        return self._jobs_dir() / f"{job_id}.report.json"

    def _graph_path(self, job_id: str) -> Path:
        return self._jobs_dir() / f"{job_id}.graph.db"

    def list_reports(self) -> list[dict]:
        reports: list[dict] = []
        if not self._jobs_dir().is_dir():
            return reports
        for path in sorted(self._jobs_dir().glob("*.report.json")):
            try:
                reports.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        return reports

    def get_report(self, job_id: str) -> dict | None:
        path = self._report_path(job_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def latest(self) -> dict | None:
        reports = [r for r in self.list_reports() if r.get("created_at")]
        if not reports:
            return None
        return max(reports, key=lambda r: r["created_at"])

    def get_graph(self, job_id: str) -> RepoGraph | None:
        if not self._graph_path(job_id).is_file():
            return None
        try:
            return GraphStore(self._graph_path(job_id)).load()
        except Exception:
            return None

    def graph_store(self, job_id: str) -> GraphStore:
        return GraphStore(self._graph_path(job_id))
```

### Step 7: Run — verify 7 passed (1 new store + 6 reports)

Run: `python -m pytest tests/test_store.py tests/test_reports.py -v`
Expected: 10 passed (9 store + 1 new) and 6 passed (reports).

### Step 8: Commit

`git add src/clio/store.py src/clio/reports.py tests/conftest.py tests/test_store.py tests/test_reports.py`
then `git commit -m "feat: queryable report archive over job artifacts"`

---

## Task 2: Impact analysis (`src/clio/impact.py` + `tests/test_impact.py`)

### Step 1: Write `tests/test_impact.py`

Line math reference for fixtures:
- `"def f():\n    return 1\n\ndef g():\n    return f()\n"` — f=1, blank=3, g=4, `f()` call=5
- chain file `"def a():\n    return 1\n\ndef b():\n    return a()\n\ndef c():\n    return b()\n"` —
  a=1, b=4, `a()` call=5, c=7, `b()` call=8

```python
# tests/test_impact.py
from clio.impact import impact_of_module, impact_of_symbol
from clio.reports import ReportArchive


def test_symbol_direct_callers(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-x", "2026-08-10T00:00:00+00:00",
             {"one.py": "def f():\n    return 1\n\ndef g():\n    return f()\n"})
    archive = ReportArchive(root)
    impact = impact_of_symbol(archive, "job-x", "one::f")
    assert impact.callers == [("one::g", 5)]
    assert impact.affected_modules == ["one"]
    assert impact.clusters_hit == ["one"]
    assert impact.verdict == "contained"


def test_symbol_transitive_callers(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-x", "2026-08-10T00:00:00+00:00",
             {"chain.py": "def a():\n    return 1\n\ndef b():\n    return a()\n\ndef c():\n    return b()\n"})
    impact = impact_of_symbol(ReportArchive(root), "job-x", "chain::a", depth=2)
    assert impact.callers == [("chain::b", 5), ("chain::c", 8)]


def test_symbol_depth_cap(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-x", "2026-08-10T00:00:00+00:00",
             {"chain.py": "def a():\n    return 1\n\ndef b():\n    return a()\n\ndef c():\n    return b()\n"})
    impact = impact_of_symbol(ReportArchive(root), "job-x", "chain::a", depth=1)
    assert impact.callers == [("chain::b", 5)]


def test_symbol_cross_cutting(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-x", "2026-08-10T00:00:00+00:00", {
        "pkg_a/__init__.py": "",
        "pkg_a/one.py": "def f():\n    return 1\n\ndef g():\n    return f()\n",
        "pkg_b/__init__.py": "",
        "pkg_b/two.py": "import pkg_a.one\n",
    })
    impact = impact_of_symbol(ReportArchive(root), "job-x", "pkg_a.one::f")
    assert impact.callers == [("pkg_a.one::g", 5)]
    assert impact.affected_modules == ["pkg_a.one", "pkg_b.two"]
    assert impact.clusters_hit == ["pkg_a", "pkg_b"]
    assert impact.verdict == "cross-cutting"


def test_symbol_missing(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-x", "2026-08-10T00:00:00+00:00", {"one.py": "def f():\n    return 1\n"})
    impact = impact_of_symbol(ReportArchive(root), "job-x", "nope::missing")
    assert impact.verdict == "missing"
    assert impact.affected_modules == [] and impact.callers == []


def test_module_contained(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-x", "2026-08-10T00:00:00+00:00", {"one.py": "def f():\n    return 1\n"})
    impact = impact_of_module(ReportArchive(root), "job-x", "one")
    assert impact.affected_modules == ["one"]
    assert impact.verdict == "contained"


def test_module_transitive_importers(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-x", "2026-08-10T00:00:00+00:00", {
        "pkg_a/__init__.py": "",
        "pkg_a/one.py": "def f():\n    return 1\n",
        "pkg_b/__init__.py": "",
        "pkg_b/two.py": "import pkg_a.one\n",
        "pkg_c/__init__.py": "",
        "pkg_c/three.py": "import pkg_b.two\n",
    })
    impact = impact_of_module(ReportArchive(root), "job-x", "pkg_a.one", depth=2)
    assert impact.affected_modules == ["pkg_a.one", "pkg_b.two", "pkg_c.three"]
    assert impact.verdict == "cross-cutting"
```

### Step 2: Run — verify FAIL

Run: `python -m pytest tests/test_impact.py -v`
Expected: collection error — `cannot import name 'impact_of_symbol' from 'clio.impact'`.

### Step 3: Write `src/clio/impact.py`

```python
# src/clio/impact.py
"""Impact analysis: what breaks if a symbol or module breaks.

Walks reverse edges from the graph store: callers of a symbol (up to `depth`
hops) plus importers of its module; or importers of a module (up to `depth`
hops). Verdict: "missing" (not in graph), "contained" (one cluster hit),
"cross-cutting" (2+ clusters hit).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from clio.clustering import cluster_by_package
from clio.reports import ReportArchive
from clio.store import GraphStore


@dataclass
class ImpactReport:
    scope: str                  # symbol id ("module::name") or module name
    affected_modules: list[str]
    callers: list[tuple[str, int]]  # (caller symbol, line); empty for module scope
    clusters_hit: list[str]
    verdict: str                # "missing" | "contained" | "cross-cutting"

    def to_dict(self) -> dict:
        return asdict(self)


def _clusters_hit(store: GraphStore, affected: set[str]) -> list[str]:
    graph = store.load()
    return sorted(
        c.name for c in cluster_by_package(graph)
        if any(m in c.modules for m in affected)
    )


def _verdict(clusters_hit: list[str]) -> str:
    return "contained" if len(clusters_hit) <= 1 else "cross-cutting"


def impact_of_symbol(
    archive: ReportArchive, job_id: str, symbol_id: str, depth: int = 3,
) -> ImpactReport:
    store = archive.graph_store(job_id)
    if not store.has_symbol(symbol_id):
        return ImpactReport(
            scope=symbol_id, affected_modules=[], callers=[],
            clusters_hit=[], verdict="missing",
        )
    seen: set[tuple[str, str, int]] = set()
    frontier = [symbol_id]
    callers: list[tuple[str, int]] = []
    affected: set[str] = set()
    for _ in range(depth):
        next_frontier: list[str] = []
        for target in frontier:
            for caller, line in store.callers_of(target):
                if (caller, target, line) in seen:
                    continue
                seen.add((caller, target, line))
                callers.append((caller, line))
                affected.add(caller.rsplit("::", 1)[0])
                next_frontier.append(caller)
        frontier = next_frontier
    for importer in store.modules_importing(symbol_id.rsplit("::", 1)[0]):
        affected.add(importer)
    clusters_hit = _clusters_hit(store, affected)
    return ImpactReport(
        scope=symbol_id,
        affected_modules=sorted(affected),
        callers=sorted(callers),
        clusters_hit=clusters_hit,
        verdict=_verdict(clusters_hit),
    )


def impact_of_module(
    archive: ReportArchive, job_id: str, module: str, depth: int = 3,
) -> ImpactReport:
    store = archive.graph_store(job_id)
    if module not in store.load().modules:
        return ImpactReport(
            scope=module, affected_modules=[], callers=[],
            clusters_hit=[], verdict="missing",
        )
    affected: set[str] = set()
    frontier: set[str] = {module}
    for _ in range(depth + 1):
        next_frontier: set[str] = set()
        for m in frontier:
            if m in affected:
                continue
            affected.add(m)
            next_frontier.update(store.modules_importing(m))
        frontier = next_frontier
    clusters_hit = _clusters_hit(store, affected)
    return ImpactReport(
        scope=module,
        affected_modules=sorted(affected),
        callers=[],
        clusters_hit=clusters_hit,
        verdict=_verdict(clusters_hit),
    )
```

### Step 4: Run — verify 7 passed

Run: `python -m pytest tests/test_impact.py -v`
Expected: 7 passed.

### Step 5: Commit

`git add src/clio/impact.py tests/test_impact.py` then
`git commit -m "feat: impact analysis over reverse call and import edges"`

---

## Task 3: CLI `--impact` flag

### Step 1: Edit `src/clio/cli.py`

a) Add the import (with the other clio imports):

```python
from clio.impact import impact_of_symbol
from clio.reports import ReportArchive
```

b) In `build_parser()`, after the `--job-id` argument:

```python
    parser.add_argument(
        "--impact", default=None,
        help="symbol id (module::name) to run impact analysis for; prints IMPACT instead of REPORT",
    )
```

c) In `amain()`, replace the tail (from `print("REPORT:")` through `return 0`) with:

```python
    if args.impact:
        archive = ReportArchive(sandbox.root)
        impact = impact_of_symbol(archive, report.job_id, args.impact)
        print("IMPACT:")
        print(json.dumps(impact.to_dict(), indent=2))
    else:
        print("REPORT:")
        print(json.dumps(report.to_dict(), indent=2))
    return 0
```

### Step 2: Append to `tests/test_cli.py`

```python
async def test_cli_impact_e2e(tmp_path, local_repo, monkeypatch, capsys):
    monkeypatch.setenv("CLIO_WORKSPACE_ROOT", str(tmp_path / "sandbox"))
    args = build_parser().parse_args([local_repo.as_uri(), "--impact", "app.service::greet"])
    assert await amain(args) == 0
    out = capsys.readouterr().out
    assert "job.graphed" in out
    assert "IMPACT:" in out
    payload = out.split("IMPACT:", 1)[1]
    impact = json.loads(payload)
    assert impact["verdict"] == "contained"
    assert "app.main" in impact["affected_modules"]


async def test_cli_impact_missing_symbol(tmp_path, local_repo, monkeypatch, capsys):
    monkeypatch.setenv("CLIO_WORKSPACE_ROOT", str(tmp_path / "sandbox"))
    args = build_parser().parse_args([local_repo.as_uri(), "--impact", "app::ghost"])
    assert await amain(args) == 0
    out = capsys.readouterr().out
    assert '"verdict": "missing"' in out
```

### Step 3: Run — verify 2 new passed + full file

Run: `python -m pytest tests/test_cli.py -v`
Expected: 5 passed (3 existing + 2 new).

### Step 4: Commit

`git add src/clio/cli.py tests/test_cli.py` then
`git commit -m "feat: cli --impact flag printing impact report"`

---

## Full-suite verification

- [ ] Run `python -m pytest -q` — expected **128 passed** (112 + 1 store + 6 reports + 7 impact + 2 cli = 128). All offline.
- [ ] Manual demo (no API key needed):

```bash
python -m clio.cli https://github.com/omhome16/Clio.git --impact clio.orchestrator::run
```

Expected: the normal event stream, then `IMPACT:` + JSON with `"verdict": "cross-cutting"`
(Clio's `clio.*` code is called from `tests.*` and the CLI — expect 2+ clusters), a
non-empty `callers` list, and `affected_modules` spanning `clio` and `tests` packages.
- [ ] Update `README.md` status table: mark M3 and M4 done.
- [ ] Commit: `git add README.md docs/plans/2026-08-10-m3-m4-persistence-impact.md` then `git commit -m "docs: mark M3 and M4 complete in README"`
- [ ] Merge to `main`, push, delete `feat/m3-m4-persistence-impact`.

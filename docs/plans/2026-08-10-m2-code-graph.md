# M2 — Code Graph (extraction, SQLite store, clustering)

- Date: 2026-08-10
- Branch: `feat/m2-code-graph`
- Precondition: M0 + M1 merged to `main` (82 tests passing)
- Target: 112 tests passing (82 baseline + 12 graph + 9 store + 7 clustering + 2 new integration; the CLI e2e test extends an existing M1 test, not a new one)
- Offline-friendly: zero network tests, zero new dependencies (stdlib `ast` + `sqlite3`)

## What M2 delivers

The data layer that M4's impact analysis ("what breaks if X breaks") feeds on:

1. **Repo graph extraction** (`graph.py`) — walks a cloned workspace, parses every Python
   module with the stdlib `ast` module, and extracts: modules (dotted package paths),
   symbols (functions/classes/methods with line numbers), import edges, and best-effort
   call edges (intra-module + `self.method` resolution).
2. **SQLite graph store** (`store.py`) — persists a `RepoGraph` snapshot to a `.db` file
   and answers graph queries: callers/callees of a symbol, who-imports-what, stats.
3. **Module clustering** (`clustering.py`) — groups modules by package prefix
   (with external-edge counts) and computes connected components over import edges.
4. **Orchestrator integration** — the INDEXING phase now builds + persists the graph
   (`jobs/<job_id>.graph.db`), emits a new `job.graphed` event with stats, and the
   analysis report carries a `graph` summary dict.

## Design decisions

- **stdlib `ast` instead of tree-sitter for now.** Zero dependencies, works offline on
  this machine, deterministic across platforms. The parser is isolated in `graph.py`
  behind `build_repo_graph()`; a tree-sitter implementation can be swapped in later
  (M6 evals) without touching the store or clustering layers.
- **Best-effort call edges.** Resolution rules (documented in code): bare name matching a
  top-level def in the same module → `module::name`; `self./cls.` method calls →
  `module::Class.method` when the method exists; any other `obj.attr()` → `obj.attr`
  (external, unresolved); names starting with `_` are skipped. Deeper call-graph
  precision is an M6 eval concern, not a blocker here.
- **Stateless GraphStore.** Every method opens its own SQLite connection and closes it
  (no held file handles — Windows temp-dir cleanup would otherwise fail). `save()` is a
  full-snapshot replace, so re-saving is idempotent.
- **Graph lives next to reports** at `jobs/<job_id>.graph.db`; `job.graphed` carries
  `{modules, symbols, calls, clusters}`.

## Contracts

- `clio.graph`: `Symbol(name, kind, module, line)`, `CallEdge(caller, callee, line)`,
  `RepoGraph(root, modules, symbols, imports, calls, skipped)` with
  `module_count/symbol_count/call_count` properties; functions
  `module_name_for(path, root)`, `iter_python_files(root)`, `parse_module(source, module)`,
  `build_repo_graph(root)`, constant `IGNORED_DIRS`.
- `clio.store`: `GraphStore(db_path)` with `save(graph)`, `load()`, `stats()`,
  `callers_of(symbol_id)`, `callees_of(symbol_id)`, `modules_importing(module)`,
  `module_imports(module)`, `symbol_ids_in(module)`.
- `clio.clustering`: `Cluster(name, modules, symbols, external_edges)`,
  `cluster_by_package(graph, depth=1)`, `connected_components(graph)`, `top_prefix(module)`.
- Symbol id convention everywhere: `f"{module}::{name}"` (methods use `Class.method`).
- `clio.events`: new constant `EVENT_JOB_GRAPHED = "job.graphed"` (additive).
- `clio.orchestrator`: `AnalysisReport` gains `graph: dict | None = None` (backward
  compatible: `from_dict` of old reports still works).

---

## Task 1: Repo graph extraction (`src/clio/graph.py` + `tests/test_graph.py`)

### Step 1: Add the shared `write_tree` fixture to `tests/conftest.py`

Replace the current `local_repo` fixture and add the `write_tree` factory fixture. The
`local_repo` fixture gains a tiny `app` Python package (needed by Task 4's assertions
and harmless to M1 tests):

```python
"""Shared fixtures for Clio tests."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def write_tree(tmp_path: Path):
    """Write a dict of relative-path -> content files under tmp_path/repo."""
    def _write(files: dict[str, str]) -> Path:
        root = tmp_path / "repo"
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return root
    return _write


@pytest.fixture
def local_repo(tmp_path: Path) -> Path:
    """A tiny real git repo (offline, deterministic) ready to be cloned."""
    repo = tmp_path / "source-repo"
    repo.mkdir()
    (repo / "hello.txt").write_text("hello clio\n", encoding="utf-8")
    (repo / "app").mkdir()
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "service.py").write_text(
        "def greet(name: str) -> str:\n    return f'hello {name}'\n",
        encoding="utf-8",
    )
    (repo / "app" / "main.py").write_text(
        "from app.service import greet\n\n\ndef run() -> str:\n    return greet('clio')\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init", "-q"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "add", "."], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        [
            "git", "-c", "user.email=test@clio.local", "-c", "user.name=Clio Test",
            "commit", "-q", "-m", "init",
        ],
        cwd=repo, check=True, capture_output=True,
    )
    return repo
```

### Step 2: Write `tests/test_graph.py`

```python
# tests/test_graph.py
from pathlib import Path

from clio.graph import CallEdge, build_repo_graph, module_name_for


def test_extracts_modules_and_paths(tmp_path, write_tree):
    root = write_tree({
        "pkg/__init__.py": "",
        "pkg/one.py": "def alpha():\n    return 1\n",
        "main.py": "import pkg.one\n",
    })
    graph = build_repo_graph(root)
    assert set(graph.modules) == {"pkg", "pkg.one", "main"}
    assert graph.modules["pkg.one"] == str(Path("pkg") / "one.py")


def test_module_name_for(tmp_path):
    root = Path(tmp_path)
    assert module_name_for(root / "pkg" / "__init__.py", root) == "pkg"
    assert module_name_for(root / "pkg" / "one.py", root) == "pkg.one"
    assert module_name_for(root / "main.py", root) == "main"


def test_ignores_ignored_dirs(tmp_path, write_tree):
    root = write_tree({
        "pkg/__init__.py": "",
        ".venv/secret.py": "def x():\n    pass\n",
        "node_modules/dep.py": "y = 1\n",
        "__pycache__/cache.py": "z = 2\n",
    })
    graph = build_repo_graph(root)
    assert set(graph.modules) == {"pkg"}


def test_extracts_symbols_and_kinds(tmp_path, write_tree):
    root = write_tree({
        "pkg/__init__.py": "",
        "pkg/one.py": (
            "def alpha():\n    return 1\n\n"
            "class Thing:\n    def beta(self):\n        return 2\n\n"
            "async def gamma():\n    return 3\n"
        ),
    })
    graph = build_repo_graph(root)
    syms = {(s.name, s.kind) for s in graph.symbols}
    assert ("alpha", "function") in syms
    assert ("Thing", "class") in syms
    assert ("Thing.beta", "method") in syms
    assert ("gamma", "function") in syms
    beta = next(s for s in graph.symbols if s.name == "Thing.beta")
    assert beta.module == "pkg.one" and beta.line == 5


def test_intra_module_call_edge(tmp_path, write_tree):
    root = write_tree({
        "one.py": "def gamma():\n    return 1\n\n"
                  "def alpha():\n    return gamma()\n",
    })
    graph = build_repo_graph(root)
    assert graph.calls == [CallEdge(caller="one::alpha", callee="one::gamma", line=5)]


def test_self_method_call_edge(tmp_path, write_tree):
    root = write_tree({
        "one.py": "class Thing:\n"
                  "    def beta(self):\n"
                  "        return self.helper()\n"
                  "    def helper(self):\n"
                  "        return 1\n",
    })
    graph = build_repo_graph(root)
    assert graph.calls == [CallEdge(caller="one::Thing.beta", callee="one::Thing.helper", line=3)]


def test_import_edges(tmp_path, write_tree):
    root = write_tree({
        "pkg/__init__.py": "",
        "pkg/one.py": "import os\nimport pkg.two\nfrom pkg.two import helper\n",
        "pkg/two.py": "def helper():\n    return 1\n",
    })
    graph = build_repo_graph(root)
    assert graph.imports["pkg.one"] == ["os", "pkg.two", "pkg.two.helper"]


def test_relative_import_resolution(tmp_path, write_tree):
    root = write_tree({
        "pkg/__init__.py": "",
        "pkg/one.py": "from . import two\nfrom .two import helper\nfrom .. import other\n",
        "pkg/two.py": "",
        "other.py": "",
    })
    graph = build_repo_graph(root)
    assert graph.imports["pkg.one"] == ["pkg.two", "pkg.two.helper"]


def test_private_and_external_calls(tmp_path, write_tree):
    root = write_tree({
        "one.py": "import os\n"
                  "def _secret():\n    return 1\n"
                  "def alpha():\n"
                  "    _secret()\n"
                  "    return os.getcwd()\n",
    })
    graph = build_repo_graph(root)
    assert graph.calls == [CallEdge(caller="one::alpha", callee="os.getcwd", line=6)]


def test_parse_error_skipped_and_recorded(tmp_path, write_tree):
    root = write_tree({
        "pkg/__init__.py": "",
        "pkg/good.py": "def ok():\n    return 1\n",
        "pkg/bad.py": "def broken(:\n",
    })
    graph = build_repo_graph(root)
    assert "pkg.bad" not in graph.modules
    assert str(Path("pkg") / "bad.py") in graph.skipped


def test_count_properties(tmp_path, write_tree):
    root = write_tree({
        "a.py": "def f():\n    return 1\n",
        "b.py": "def g():\n    return f()\n",
    })
    graph = build_repo_graph(root)
    assert graph.module_count == 2
    assert graph.symbol_count == 2
    assert graph.call_count == 1


def test_empty_repo(tmp_path):
    root = Path(tmp_path) / "empty"
    root.mkdir()
    graph = build_repo_graph(root)
    assert graph.module_count == 0
    assert graph.symbol_count == 0
    assert graph.skipped == []
```

### Step 3: Run — verify FAIL

Run: `python -m pytest tests/test_graph.py -v`
Expected: collection error — `cannot import name 'build_repo_graph' from 'clio.graph'`.

### Step 4: Write `src/clio/graph.py`

```python
# src/clio/graph.py
"""Repo graph extraction using the stdlib `ast` module (Python codebases).

The parser is isolated behind build_repo_graph(); other languages (tree-sitter
etc.) can plug in later without touching the store or clustering layers.
"""
from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env",
    "node_modules", ".tox", ".pytest_cache", ".mypy_cache", "dist", "build",
}


@dataclass
class Symbol:
    name: str        # "foo", "Thing", or "Thing.beta" for methods
    kind: str        # "function" | "class" | "method"
    module: str      # dotted package path, e.g. "clio.orchestrator"
    line: int


@dataclass
class CallEdge:
    caller: str      # "module::symbol" ("module" for module-level calls)
    callee: str      # best-effort resolved target
    line: int


@dataclass
class RepoGraph:
    root: str
    modules: dict[str, str] = field(default_factory=dict)        # dotted -> rel path
    symbols: list[Symbol] = field(default_factory=list)
    imports: dict[str, list[str]] = field(default_factory=dict)  # module -> targets
    calls: list[CallEdge] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)             # unparseable files

    @property
    def module_count(self) -> int:
        return len(self.modules)

    @property
    def symbol_count(self) -> int:
        return len(self.symbols)

    @property
    def call_count(self) -> int:
        return len(self.calls)


def module_name_for(path: Path, root: Path) -> str:
    """Dotted module name for a .py file: pkg/one.py -> "pkg.one";
    pkg/__init__.py -> "pkg"; root-level main.py -> "main"."""
    rel = path.relative_to(root)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1].removesuffix(".py")
    return ".".join(parts) if parts else "(root)"


def iter_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def parse_module(
    source: str, module: str
) -> tuple[list[Symbol], list[str], list[CallEdge]]:
    """Parse one module -> (symbols, import targets, call edges).
    Raises SyntaxError when the source does not parse."""
    tree = ast.parse(source)
    top_names = {
        n.name for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    class_methods: dict[str, set[str]] = defaultdict(set)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    class_methods[node.name].add(member.name)
    visitor = _ModuleVisitor(module, top_names, class_methods)
    visitor.visit(tree)
    return visitor.symbols, visitor.imports, visitor.calls


class _ModuleVisitor(ast.NodeVisitor):
    def __init__(
        self,
        module: str,
        top_names: set[str],
        class_methods: dict[str, set[str]],
    ) -> None:
        self.module = module
        self._top_names = top_names
        self._class_methods = class_methods
        self._scope: list[str] = []
        self._in_class = False
        self.symbols: list[Symbol] = []
        self.imports: list[str] = []
        self.calls: list[CallEdge] = []

    def _caller(self) -> str:
        # "module::Class.method" for methods, "module::func" for functions,
        # "module" for module-level calls (dotted scope join, not "::").
        return "::".join([self.module, ".".join(self._scope)])

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        parts = self.module.split(".") if self.module else []
        if node.level >= 1:
            if node.level > len(parts):
                self.generic_visit(node)
                return
            base = parts[: len(parts) - node.level]
            module_name = ".".join(base + [node.module]) if node.module else ".".join(base)
        else:
            module_name = node.module or ""
        if module_name:
            for alias in node.names:
                if alias.name != "*":
                    self.imports.append(f"{module_name}.{alias.name}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        callee = self._resolve_callee(node)
        if callee is not None:
            self.calls.append(
                CallEdge(caller=self._caller(), callee=callee, line=node.lineno)
            )
        self.generic_visit(node)

    def _resolve_callee(self, node: ast.Call) -> str | None:
        f = node.func
        if isinstance(f, ast.Name):
            name = f.id
            if name.startswith("_"):
                return None
            if name in self._top_names:
                return f"{self.module}::{name}"
            return name
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            obj, attr = f.value.id, f.attr
            if obj in ("self", "cls") and self._scope:
                methods = self._class_methods.get(self._scope[0], set())
                if attr in methods:
                    return f"{self.module}::{self._scope[0]}.{attr}"
                return None
            if obj in self._top_names:
                return f"{self.module}::{obj}.{attr}"
            if not obj.startswith("_"):
                return f"{obj}.{attr}"
        return None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter_def(node.name, "function", node.lineno)
        self.generic_visit(node)
        self._scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        prev = self._in_class
        self._in_class = True
        self._scope.append(node.name)
        self.symbols.append(
            Symbol(name=node.name, kind="class", module=self.module, line=node.lineno)
        )
        self.generic_visit(node)
        self._scope.pop()
        self._in_class = prev

    def _enter_def(self, name: str, kind: str, lineno: int) -> None:
        if not self._scope:
            self.symbols.append(
                Symbol(name=name, kind=kind, module=self.module, line=lineno)
            )
        elif self._in_class and len(self._scope) == 1:
            self.symbols.append(
                Symbol(
                    name=f"{self._scope[0]}.{name}", kind="method",
                    module=self.module, line=lineno,
                )
            )
        self._scope.append(name)


def build_repo_graph(root: Path) -> RepoGraph:
    root = Path(root)
    graph = RepoGraph(root=str(root))
    for path in iter_python_files(root):
        module = module_name_for(path, root)
        if not module:
            continue
        try:
            symbols, imports, calls = parse_module(
                path.read_text(encoding="utf-8", errors="replace"), module
            )
        except SyntaxError:
            graph.skipped.append(str(path.relative_to(root)))
            continue
        graph.modules[module] = str(path.relative_to(root))
        graph.symbols.extend(symbols)
        graph.imports[module] = sorted(imports)
        graph.calls.extend(calls)
    return graph
```

### Step 5: Run — verify 12 passed

Run: `python -m pytest tests/test_graph.py -v`
Expected: 12 passed.

### Step 6: Commit

`git add src/clio/graph.py tests/test_graph.py tests/conftest.py` then
`git commit -m "feat: repo graph extraction via stdlib ast"`

---

## Task 2: SQLite graph store (`src/clio/store.py` + `tests/test_store.py`)

### Step 1: Write `tests/test_store.py`

```python
# tests/test_store.py
from clio.graph import RepoGraph, build_repo_graph
from clio.store import GraphStore


def test_save_load_roundtrip(tmp_path, write_tree):
    graph = build_repo_graph(write_tree({
        "pkg/__init__.py": "",
        "pkg/one.py": "def alpha():\n    return beta()\n\ndef beta():\n    return 1\n",
    }))
    db = tmp_path / "graph.db"
    GraphStore(db).save(graph)
    assert GraphStore(db).load() == graph


def test_stats_counts(tmp_path, write_tree):
    graph = build_repo_graph(write_tree({
        "pkg/__init__.py": "",
        "pkg/one.py": "def alpha():\n    return beta()\n\ndef beta():\n    return 1\n",
    }))
    db = tmp_path / "graph.db"
    GraphStore(db).save(graph)
    assert GraphStore(db).stats() == {"modules": 2, "symbols": 2, "imports": 0, "calls": 1}


def test_save_replaces_snapshot(tmp_path, write_tree):
    root = write_tree({"a.py": "def f():\n    return 1\n"})
    db = tmp_path / "graph.db"
    store = GraphStore(db)
    store.save(build_repo_graph(root))
    (root / "b.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    store.save(build_repo_graph(root))
    assert GraphStore(db).stats() == {"modules": 2, "symbols": 2, "imports": 0, "calls": 0}


def test_callers_of(tmp_path, write_tree):
    root = write_tree({
        "a.py": "def f():\n    return 1\n\ndef g():\n    return f()\n",
    })
    db = tmp_path / "graph.db"
    GraphStore(db).save(build_repo_graph(root))
    assert GraphStore(db).callers_of("a::f") == [("a::g", 5)]


def test_callees_of(tmp_path, write_tree):
    root = write_tree({
        "a.py": "def f():\n    return 1\n\ndef g():\n    return f()\n",
    })
    db = tmp_path / "graph.db"
    GraphStore(db).save(build_repo_graph(root))
    assert GraphStore(db).callees_of("a::g") == [("a::f", 5)]


def test_modules_importing(tmp_path, write_tree):
    root = write_tree({
        "pkg/__init__.py": "",
        "pkg/one.py": "from pkg.two import helper\n",
        "pkg/two.py": "",
        "pkg/three.py": "import pkg.two\n",
    })
    db = tmp_path / "graph.db"
    GraphStore(db).save(build_repo_graph(root))
    assert GraphStore(db).modules_importing("pkg.two") == ["pkg.one", "pkg.three"]


def test_module_imports(tmp_path, write_tree):
    root = write_tree({
        "one.py": "import os\nfrom pathlib import Path\n",
    })
    db = tmp_path / "graph.db"
    GraphStore(db).save(build_repo_graph(root))
    assert GraphStore(db).module_imports("one") == ["os", "pathlib.Path"]


def test_symbol_ids_in(tmp_path, write_tree):
    root = write_tree({
        "pkg/__init__.py": "",
        "pkg/one.py": "def b():\n    return 1\n\ndef a():\n    return b()\n",
    })
    db = tmp_path / "graph.db"
    GraphStore(db).save(build_repo_graph(root))
    assert GraphStore(db).symbol_ids_in("pkg.one") == ["pkg.one::a", "pkg.one::b"]


def test_empty_graph_roundtrip(tmp_path):
    graph = RepoGraph(root="")
    db = tmp_path / "graph.db"
    GraphStore(db).save(graph)
    assert GraphStore(db).load() == graph
    assert GraphStore(db).stats() == {"modules": 0, "symbols": 0, "imports": 0, "calls": 0}
```

### Step 2: Run — verify FAIL

Run: `python -m pytest tests/test_store.py -v`
Expected: collection error — `cannot import name 'GraphStore' from 'clio.store'`.

### Step 3: Write `src/clio/store.py`

```python
# src/clio/store.py
"""SQLite persistence and querying for RepoGraph snapshots."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from clio.graph import CallEdge, RepoGraph, Symbol

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS modules (name TEXT PRIMARY KEY, path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS symbols (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    module TEXT NOT NULL,
    line INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
    src TEXT NOT NULL,
    target TEXT NOT NULL,
    PRIMARY KEY (src, target)
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller TEXT NOT NULL,
    callee TEXT NOT NULL,
    line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_module ON symbols(module);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller);
CREATE INDEX IF NOT EXISTS idx_imports_target ON imports(target);
"""


class GraphStore:
    """Snapshot store for a RepoGraph. `save()` replaces the whole graph, so
    re-saving is idempotent. Every method opens and closes its own connection:
    the db file is never held open (Windows-safe, temp-dir friendly)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.executescript(SCHEMA)
        return conn

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def save(self, graph: RepoGraph) -> None:
        with self._session() as conn:
            conn.execute("DELETE FROM meta")
            conn.execute("DELETE FROM modules")
            conn.execute("DELETE FROM symbols")
            conn.execute("DELETE FROM imports")
            conn.execute("DELETE FROM calls")
            conn.execute("INSERT INTO meta(key, value) VALUES ('root', ?)", (graph.root,))
            conn.executemany(
                "INSERT INTO modules(name, path) VALUES (?, ?)",
                sorted(graph.modules.items()),
            )
            conn.executemany(
                "INSERT INTO symbols(id, name, kind, module, line) VALUES (?, ?, ?, ?, ?)",
                [
                    (f"{s.module}::{s.name}", s.name, s.kind, s.module, s.line)
                    for s in graph.symbols
                ],
            )
            conn.executemany(
                "INSERT INTO imports(src, target) VALUES (?, ?)",
                [
                    (src, target)
                    for src, targets in graph.imports.items()
                    for target in targets
                ],
            )
            conn.executemany(
                "INSERT INTO calls(caller, callee, line) VALUES (?, ?, ?)",
                [(c.caller, c.callee, c.line) for c in graph.calls],
            )

    def load(self) -> RepoGraph:
        with self._session() as conn:
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            modules = dict(conn.execute("SELECT name, path FROM modules").fetchall())
            symbols = [
                Symbol(name=name, kind=kind, module=module, line=line)
                for name, kind, module, line in conn.execute(
                    "SELECT name, kind, module, line FROM symbols ORDER BY rowid"
                )
            ]
            imports: dict[str, list[str]] = {m: [] for m in modules}
            for src, target in conn.execute(
                "SELECT src, target FROM imports ORDER BY src, target"
            ):
                imports.setdefault(src, []).append(target)
            calls = [
                CallEdge(caller=caller, callee=callee, line=line)
                for caller, callee, line in conn.execute(
                    "SELECT caller, callee, line FROM calls ORDER BY id"
                )
            ]
        return RepoGraph(
            root=meta.get("root", ""),
            modules=modules,
            symbols=symbols,
            imports=imports,
            calls=calls,
        )

    def stats(self) -> dict:
        with self._session() as conn:
            modules = conn.execute("SELECT COUNT(*) FROM modules").fetchone()[0]
            symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            imports = conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
            calls = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        return {"modules": modules, "symbols": symbols, "imports": imports, "calls": calls}

    def callers_of(self, symbol_id: str) -> list[tuple[str, int]]:
        with self._session() as conn:
            return conn.execute(
                "SELECT caller, line FROM calls WHERE callee = ? ORDER BY caller, line",
                (symbol_id,),
            ).fetchall()

    def callees_of(self, symbol_id: str) -> list[tuple[str, int]]:
        with self._session() as conn:
            return conn.execute(
                "SELECT callee, line FROM calls WHERE caller = ? ORDER BY callee, line",
                (symbol_id,),
            ).fetchall()

    def modules_importing(self, module: str) -> list[str]:
        """Modules importing `module` directly or any of its submodules."""
        with self._session() as conn:
            rows = conn.execute(
                "SELECT src FROM imports WHERE target = ? OR target LIKE ? ORDER BY src",
                (module, module + ".%"),
            ).fetchall()
        return [row[0] for row in rows]

    def module_imports(self, module: str) -> list[str]:
        with self._session() as conn:
            rows = conn.execute(
                "SELECT target FROM imports WHERE src = ? ORDER BY target", (module,)
            ).fetchall()
        return [row[0] for row in rows]

    def symbol_ids_in(self, module: str) -> list[str]:
        with self._session() as conn:
            rows = conn.execute(
                "SELECT id FROM symbols WHERE module = ? ORDER BY name", (module,)
            ).fetchall()
        return [row[0] for row in rows]
```

### Step 4: Run — verify 9 passed

Run: `python -m pytest tests/test_store.py -v`
Expected: 9 passed.

### Step 5: Commit

`git add src/clio/store.py tests/test_store.py` then
`git commit -m "feat: sqlite graph store with query apis"`

---

## Task 3: Module clustering (`src/clio/clustering.py` + `tests/test_clustering.py`)

### Step 1: Write `tests/test_clustering.py`

```python
# tests/test_clustering.py
from clio.clustering import cluster_by_package, connected_components, top_prefix
from clio.graph import RepoGraph, build_repo_graph


def test_top_prefix():
    assert top_prefix("clio.orchestrator") == "clio"
    assert top_prefix("main") == "main"


def test_cluster_by_package_basic(tmp_path, write_tree):
    root = write_tree({
        "pkg/__init__.py": "",
        "pkg/one.py": "def a():\n    return 1\n",
        "pkg/two.py": "def b():\n    return 2\n",
        "main.py": "import pkg.one\n",
    })
    graph = build_repo_graph(root)
    clusters = cluster_by_package(graph)
    assert [c.name for c in clusters] == ["main", "pkg"]
    assert clusters[0].modules == ["main"] and clusters[0].symbols == 0
    assert clusters[1].modules == ["pkg", "pkg.one", "pkg.two"]
    assert clusters[1].symbols == 2


def test_cluster_symbol_and_external_counts(tmp_path, write_tree):
    root = write_tree({
        "pkg/__init__.py": "",
        "pkg/one.py": "import os\ndef a():\n    return 1\n",
        "pkg/two.py": "from pkg.one import a\n",
    })
    graph = build_repo_graph(root)
    cluster = cluster_by_package(graph)[0]
    assert cluster.name == "pkg"
    assert cluster.symbols == 1
    assert cluster.external_edges == 1


def test_cluster_depth_two(tmp_path, write_tree):
    root = write_tree({
        "pkg/__init__.py": "",
        "pkg/a.py": "def a():\n    return 1\n",
        "pkg/b.py": "def b():\n    return 2\n",
    })
    graph = build_repo_graph(root)
    clusters = cluster_by_package(graph, depth=2)
    assert [c.name for c in clusters] == ["pkg", "pkg.a", "pkg.b"]


def test_connected_components(tmp_path, write_tree):
    root = write_tree({
        "pkg/__init__.py": "",
        "pkg/one.py": "from pkg.two import b\n",
        "pkg/two.py": "import pkg.one\n",
        "main.py": "import os\n",
    })
    graph = build_repo_graph(root)
    assert connected_components(graph) == [["main"], ["pkg", "pkg.one", "pkg.two"]]


def test_components_single_module(tmp_path, write_tree):
    root = write_tree({"one.py": "def f():\n    return 1\n"})
    graph = build_repo_graph(root)
    assert connected_components(graph) == [["one"]]


def test_empty_graph(tmp_path):
    empty = RepoGraph(root="")
    assert cluster_by_package(empty) == []
    assert connected_components(empty) == []
```

### Step 2: Run — verify FAIL

Run: `python -m pytest tests/test_clustering.py -v`
Expected: collection error — `cannot import name 'cluster_by_package' from 'clio.clustering'`.

### Step 3: Write `src/clio/clustering.py`

```python
# src/clio/clustering.py
"""Module clustering: package-level grouping and import-graph components."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from clio.graph import RepoGraph


@dataclass
class Cluster:
    name: str
    modules: list[str]
    symbols: int
    external_edges: int


def top_prefix(module: str) -> str:
    """First dotted segment of a module or import target."""
    return module.split(".", 1)[0]


def cluster_by_package(graph: RepoGraph, depth: int = 1) -> list[Cluster]:
    """Group modules by the first `depth` dotted segments of their package path.
    Modules shorter than `depth` stay in their own cluster. Deterministic order."""
    buckets: dict[str, list[str]] = defaultdict(list)
    for module in graph.modules:
        parts = module.split(".")
        prefix = ".".join(parts[:depth]) if len(parts) > depth else module
        buckets[prefix].append(module)
    clusters: list[Cluster] = []
    for name in sorted(buckets):
        members = sorted(buckets[name])
        member_set = set(members)
        symbols = sum(1 for s in graph.symbols if s.module in member_set)
        external_edges = sum(
            1
            for module in members
            for target in graph.imports.get(module, [])
            if top_prefix(target) != name
        )
        clusters.append(
            Cluster(name=name, modules=members, symbols=symbols, external_edges=external_edges)
        )
    return clusters


def connected_components(graph: RepoGraph) -> list[list[str]]:
    """Undirected components over import edges where both sides are in-repo
    modules. A target connects via its top-level package when that package is
    itself a module, or via the full dotted name when it is one."""
    adj: dict[str, set[str]] = defaultdict(set)
    for module in graph.modules:
        adj[module]
    for src, targets in graph.imports.items():
        if src not in graph.modules:
            continue
        for target in targets:
            top = target.split(".", 1)[0]
            if top in graph.modules:
                adj[src].add(top)
                adj[top].add(src)
            elif target in graph.modules:
                adj[src].add(target)
                adj[target].add(src)
    seen: set[str] = set()
    components: list[list[str]] = []
    for module in sorted(graph.modules):
        if module in seen:
            continue
        stack = [module]
        seen.add(module)
        component: list[str] = []
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in sorted(adj[current]):
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        components.append(sorted(component))
    return components
```

### Step 4: Run — verify 7 passed

Run: `python -m pytest tests/test_clustering.py -v`
Expected: 7 passed.

### Step 5: Commit

`git add src/clio/clustering.py tests/test_clustering.py` then
`git commit -m "feat: module clustering by package and import components"`

---

## Task 4: Orchestrator integration (graph phase + report field)

### Step 1: Add the `EVENT_JOB_GRAPHED` constant to `src/clio/events.py`

Insert after the `EVENT_JOB_INDEXING = "job.indexing"` line:

```python
EVENT_JOB_GRAPHED = "job.graphed"
```

### Step 2: Extend `tests/test_orchestrator.py`

a) Add `EVENT_JOB_GRAPHED` to the events import block (alphabetical):

```python
from clio.events import (
    EVENT_JOB_CLONED, EVENT_JOB_FAILED, EVENT_JOB_GRAPHED, EVENT_JOB_PERSISTED,
    EVENT_SUBAGENT_DONE, EVENT_SUBAGENT_START, Event, EventBus,
)
```

b) Append these two tests at the end of the file:

```python
async def test_pipeline_builds_graph(tmp_path, local_repo):
    limits = Limits(workspace_root=tmp_path / "sandbox", max_agent_steps=5)
    sandbox = Sandbox(root=tmp_path / "sandbox", limits=limits)
    client = MockLLM(handler=_mock_handler(limits))
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    orch = Orchestrator(sandbox, client, bus=bus, limits=limits)
    report = await orch.run(local_repo.as_uri(), root=tmp_path, job_id="clio-graph")
    assert report.graph is not None
    assert report.graph["modules"] >= 3
    assert report.graph["symbols"] >= 2
    assert report.graph["clusters"] >= 1
    assert (tmp_path / "jobs" / "clio-graph.graph.db").is_file()
    types = [e.type for e in seen]
    assert types.count(EVENT_JOB_GRAPHED) == 1


def test_report_roundtrip_with_graph():
    report = AnalysisReport(
        job_id="clio-1", repo_url="https://github.com/x/y.git", commit_sha="abc",
        aspects={"a": {"ok": True, "content": "z"}}, summary="s",
        created_at="2026-08-10T00:00:00+00:00",
        graph={"modules": 3, "symbols": 2, "calls": 1, "clusters": 2},
    )
    restored = AnalysisReport.from_dict(report.to_dict())
    assert restored == report
```

### Step 3: Extend `tests/test_cli.py` end-to-end test

In `test_cli_end_to_end_mock`, after the line `assert "job.cloned" in out` add:

```python
    assert "job.graphed" in out
```

And after the line `assert report["summary"] == "merged"` add:

```python
    assert report["graph"]["modules"] >= 3
```

### Step 4: Update `src/clio/orchestrator.py`

a) Imports — add these lines (keep the existing ones):

```python
from clio.clustering import cluster_by_package
from clio.events import (
    EVENT_JOB_ANALYZING, EVENT_JOB_CLONED, EVENT_JOB_CLONING, EVENT_JOB_CREATED,
    EVENT_JOB_FAILED, EVENT_JOB_GRAPHED, EVENT_JOB_INDEXING, EVENT_JOB_PERSISTED,
    EVENT_JOB_SYNTHESIZING, EVENT_SUBAGENT_DONE, Event, EventBus,
)
from clio.graph import build_repo_graph
from clio.store import GraphStore
```

b) `AnalysisReport` — add the graph field (last field):

```python
    created_at: str
    graph: dict | None = None
```

c) In `run()`, replace this line:

```python
            self._emit(EVENT_JOB_INDEXING, job.job_id, {})
```

with:

```python
            self._emit(EVENT_JOB_INDEXING, job.job_id, {})
            graph = build_repo_graph(self._sandbox.workspace(job.job_id))
            graph_stats = {
                "modules": graph.module_count,
                "symbols": graph.symbol_count,
                "calls": graph.call_count,
                "clusters": len(cluster_by_package(graph)),
            }
            jobs_dir(root).mkdir(parents=True, exist_ok=True)
            GraphStore(jobs_dir(root) / f"{job.job_id}.graph.db").save(graph)
            self._emit(EVENT_JOB_GRAPHED, job.job_id, graph_stats)
```

d) In the `AnalysisReport(...)` construction inside `run()`, add the graph field:

```python
            report = AnalysisReport(
                job_id=job.job_id,
                repo_url=url,
                commit_sha=clone.commit_sha,
                aspects=aspects,
                summary=summary,
                created_at=datetime.now(UTC).isoformat(),
                graph=graph_stats,
            )
```

### Step 5: Run — verify 3 new tests pass, then the full file

Run: `python -m pytest tests/test_orchestrator.py tests/test_cli.py -v`
Expected: 5 passed in test_orchestrator.py (3 existing + 2 new), 3 passed in test_cli.py.

### Step 6: Commit

`git add src/clio/events.py src/clio/orchestrator.py tests/test_orchestrator.py tests/test_cli.py`
then `git commit -m "feat: graph phase in orchestrator with graphed event"`

---

## Full-suite verification

- [ ] Run `python -m pytest -q` — expected **112 passed** (82 baseline + 12 graph + 9 store + 7 clustering + 2 new integration = 112; the CLI e2e test extends an existing M1 test). All offline; no network tests in M2.
- [ ] Manual demo (no API key needed):

```bash
python -m clio.cli https://github.com/omhome16/Clio.git
```

Expected: the stream now includes a `[time] job.graphed {"modules": ..., "symbols": ..., "calls": ..., "clusters": ...}` line, and the final `REPORT:` JSON contains a `"graph"` section with the same stats. `sandbox/jobs/clio-*.graph.db` exists next to the report.
- [ ] Update `README.md` status table: `| M2 — code graph | ✅ Done |`
- [ ] Commit: `git add README.md docs/plans/2026-08-10-m2-code-graph.md` then `git commit -m "docs: mark M2 complete in README"`
- [ ] Merge to `main`, push, delete `feat/m2-code-graph`.

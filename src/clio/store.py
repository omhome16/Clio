# src/clio/store.py
"""SQLite persistence and querying for RepoGraph snapshots."""
from __future__ import annotations

import json
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
    line INTEGER NOT NULL,
    end_line INTEGER NOT NULL DEFAULT 0
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
        try:
            conn.execute("ALTER TABLE symbols ADD COLUMN end_line INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
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
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('languages', ?)",
                (json.dumps(graph.languages),),
            )
            conn.executemany(
                "INSERT INTO modules(name, path) VALUES (?, ?)",
                sorted(graph.modules.items()),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO symbols(id, name, kind, module, line, end_line) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (f"{s.module}::{s.name}", s.name, s.kind, s.module, s.line, s.end_line)
                    for s in graph.symbols
                ],
            )
            conn.executemany(
                "INSERT OR IGNORE INTO imports(src, target) VALUES (?, ?)",
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
                Symbol(name=name, kind=kind, module=module, line=line, end_line=end_line)
                for name, kind, module, line, end_line in conn.execute(
                    "SELECT name, kind, module, line, end_line FROM symbols ORDER BY rowid"
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
        languages: dict[str, str] = {}
        raw = meta.get("languages")
        if raw:
            try:
                languages = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                languages = {}
        return RepoGraph(
            root=meta.get("root", ""),
            modules=modules,
            symbols=symbols,
            imports=imports,
            calls=calls,
            languages=languages,
        )

    def stats(self) -> dict:
        """Quick counts without rehydrating the whole graph."""
        with self._session() as conn:
            return {
                "modules": conn.execute("SELECT COUNT(*) FROM modules").fetchone()[0],
                "symbols": conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
                "imports": conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0],
                "calls": conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0],
            }

    def language_stats(self) -> dict[str, int]:
        """Per-language module counts from the stored snapshot metadata."""
        with self._session() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'languages'"
            ).fetchone()
        if not row or not row[0]:
            return {}
        try:
            languages = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return {}
        counts = {lang: 0 for lang in set(languages.values())}
        for lang in languages.values():
            counts[lang] += 1
        return dict(sorted(counts.items()))

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
        """Modules importing `module` directly, any of its submodules, or —
        for src-layout repos — a known module whose dotted path ends with the
        import target's module part (module "src.clio.x" imported as "clio.x").
        Slash-path modules (foreign languages) match on "/" prefixes too."""
        with self._session() as conn:
            rows = conn.execute(
                "SELECT src, target FROM imports ORDER BY src, target"
            ).fetchall()
        srcs: set[str] = set()
        for src, target in rows:
            tmod = target.rsplit(".", 1)[0] if "." in target else target
            if (
                module == target
                or module == tmod
                or target.startswith(module + ".")
                or target.startswith(module + "/")
                or module.endswith("." + tmod)
            ):
                srcs.add(src)
        return sorted(srcs)

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

    def has_symbol(self, symbol_id: str) -> bool:
        with self._session() as conn:
            row = conn.execute(
                "SELECT 1 FROM symbols WHERE id = ?", (symbol_id,)
            ).fetchone()
        return row is not None

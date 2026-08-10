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

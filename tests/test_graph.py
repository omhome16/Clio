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

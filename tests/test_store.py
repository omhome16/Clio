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

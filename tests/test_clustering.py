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

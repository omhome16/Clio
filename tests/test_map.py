# tests/test_map.py
from clio.graph import CallEdge, RepoGraph, build_repo_graph
from clio.map import COL_W, ROW_H, layout_graph, resolve_module


def _layout(files: dict[str, str], tmp_path, write_tree):
    return layout_graph(build_repo_graph(write_tree(files)))


def test_layout_is_deterministic(tmp_path, write_tree):
    files = {
        "pkg/__init__.py": "",
        "pkg/one.py": "def a():\n    return 1\n",
        "pkg/two.py": "import pkg.one\ndef b():\n    return pkg.one.a()\n",
        "main.py": "from pkg.two import b\n",
    }
    graph = build_repo_graph(write_tree(files))
    assert layout_graph(graph) == layout_graph(graph)


def test_layout_columns_match_clusters(tmp_path, write_tree):
    result = _layout({
        "alpha.py": "def top():\n    return 1\n",
        "pkg/__init__.py": "",
        "pkg/one.py": "def a():\n    return 1\n",
        "pkg/two.py": "def b():\n    return 2\n",
    }, tmp_path, write_tree)
    by_module = {n["module"]: n for n in result["nodes"]}
    assert [n["cluster"] for n in result["nodes"]] == ["alpha", "pkg", "pkg", "pkg"]
    assert by_module["alpha"]["x"] == 0 * COL_W
    assert by_module["pkg"]["x"] == 1 * COL_W
    assert by_module["pkg.one"]["x"] == 1 * COL_W
    assert by_module["pkg.two"]["x"] == 1 * COL_W


def test_layout_nodes_carry_metadata(tmp_path, write_tree):
    result = _layout({
        "pkg/__init__.py": "",
        "pkg/one.py": "def a():\n    return 1\n",
        "pkg/two.py": "def b():\n    return 2\n",
    }, tmp_path, write_tree)
    for node in result["nodes"]:
        assert set(node) == {"id", "module", "cluster", "symbols", "x", "y"}
        assert node["id"] == node["module"]
        assert node["cluster"] == "pkg"
    symbols = {n["module"]: n["symbols"] for n in result["nodes"]}
    assert symbols == {"pkg": 0, "pkg.one": 1, "pkg.two": 1}
    pkg_rows = sorted(n["y"] for n in result["nodes"] if n["module"] != "pkg")
    assert pkg_rows == [ROW_H, 2 * ROW_H]


def _graph_with(modules: list[str], imports: dict[str, list[str]] | None = None,
                calls: list[CallEdge] | None = None) -> RepoGraph:
    return RepoGraph(
        root="",
        modules={m: m.replace(".", "/") + ".py" for m in modules},
        imports=imports or {},
        calls=calls or [],
    )


def test_layout_edges_imports_calls_and_dedupe():
    graph = _graph_with(
        ["main", "pkg", "pkg.one", "pkg.two"],
        imports={
            "main": ["pkg.two.b"],
            "pkg.two": ["pkg.one", "pkg.two.x", "os"],
            "pkg": ["pkg"],
        },
        calls=[
            CallEdge(caller="main::run", callee="pkg.two::b", line=1),
            CallEdge(caller="main::run", callee="plain_name", line=2),
            CallEdge(caller="pkg.one::a", callee="pkg.two::b", line=3),
            CallEdge(caller="pkg.two::b", callee="pkg.two::helper", line=4),
        ],
    )
    edges = {(e["from"], e["to"], e["kind"]) for e in layout_graph(graph)["edges"]}
    assert edges == {
        ("main", "pkg.two", "both"),
        ("pkg.one", "pkg.two", "call"),
        ("pkg.two", "pkg.one", "import"),
    }


def test_resolve_module_aliases():
    modules = ["src.clio.config", "src.clio.x", "pkg", "pkg.one", "pkg.two"]
    assert resolve_module("pkg.one", modules) == "pkg.one"
    assert resolve_module("pkg.two.b", modules) == "pkg.two"
    assert resolve_module("clio.config", modules) == "src.clio.config"
    assert resolve_module("os", modules) is None
    assert resolve_module("pkg.one.a", modules) == "pkg.one"


def test_resolve_module_strips_symbol_suffix():
    modules = ["src.clio.config", "src.clio.llm", "pkg", "pkg.one", "pkg.two"]
    assert resolve_module("clio.config.Limits", modules) == "src.clio.config"
    assert resolve_module("clio.config.get_limits", modules) == "src.clio.config"
    assert resolve_module("clio.llm.GeminiClient", modules) == "src.clio.llm"
    assert resolve_module("urllib.parse.urlparse", modules) is None
    assert resolve_module("argparse", modules) is None

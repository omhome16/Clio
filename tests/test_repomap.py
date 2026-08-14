# tests/test_repomap.py
import pytest

from clio.graph import build_repo_graph
from clio.repomap import (
    build_repo_map, file_reference_graph, personalized_pagerank,
)


def test_reference_graph_from_imports_and_calls(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "a.py").write_text("import b\n\ndef run():\n    b.helper()\n", encoding="utf-8")
    (root / "b.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    graph = build_repo_graph(root)
    edges = file_reference_graph(graph)
    assert "b" in edges.get("a", set()) or "a" in edges.get("b", set())


def test_pagerank_prefers_central_module():
    edges = {"hub": {"a", "b", "c"}, "a": {"hub"}, "b": {"hub"}, "c": {"hub"}}
    scores = personalized_pagerank(edges, personal={"hub": 1.0})
    assert scores["hub"] > scores["a"] > 0


def test_repo_map_contains_signatures(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "app.py").write_text(
        "def greet(name):\n    return f'hi {name}'\n\nclass Greeter:\n"
        "    def hello(self):\n        return 'yo'\n",
        encoding="utf-8",
    )
    graph = build_repo_graph(root)
    text = build_repo_map(root, graph)
    assert "def greet(name)" in text
    assert "app" in text


def test_repo_map_budget_fit(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "app.py").write_text("\n".join(f"def fn{i}():\n    return {i}\n" for i in range(80)),
                                encoding="utf-8")
    graph = build_repo_graph(root)
    text = build_repo_map(root, graph, budget_chars=500)
    assert len(text) <= 600
    assert text
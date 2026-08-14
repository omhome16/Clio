from pathlib import Path

import pytest

from clio.graph import build_repo_graph
from clio.retrieval import (
    Hit, build_retrieval_index, pack_hits, sources_from_hits, tokenize,
)


def test_tokenize_splits_camel_and_snake_drops_stopwords():
    terms = tokenize("GraphStore.save_graph and the module")
    assert "graph" in terms and "store" in terms
    assert "save" in terms
    assert "and" not in terms and "the" not in terms


def _seed_repo(root: Path) -> None:
    (root / "README.md").write_text(
        "# demo\n\nThis project greets users with photocopies of their name.\n",
        encoding="utf-8",
    )
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "service.py").write_text(
        "def greet(name: str) -> str:\n    return 'hi ' + name\n",
        encoding="utf-8",
    )
    (root / "app" / "main.py").write_text(
        "from app.service import greet\n\n\n"
        "def run() -> None:\n    print(greet('clio'))\n",
        encoding="utf-8",
    )


def _index(root: Path):
    _seed_repo(root)
    graph = build_repo_graph(root)
    return graph, build_retrieval_index(root, graph)


def test_symbol_match_ranks_definition_chunk(tmp_path):
    graph, index = _index(tmp_path)
    hits = index.search("how does greet work")
    assert hits and hits[0].chunk.path == "app/service.py"
    assert any("symbol match" in r for r in hits[0].reasons)


def test_call_graph_answers_who_calls(tmp_path):
    _, index = _index(tmp_path)
    hits = index.search("who calls greet")
    assert hits and hits[0].chunk.path == "app/main.py"
    assert any("calls" in r for r in hits[0].reasons)


def test_import_neighbor_boost(tmp_path):
    _, index = _index(tmp_path)
    hits = index.search("what uses the service module")
    paths = [h.chunk.path for h in hits]
    assert "app/service.py" in paths
    assert any("import neighbor" in r for h in hits for r in h.reasons)


def test_readme_is_retrievable(tmp_path):
    _, index = _index(tmp_path)
    hits = index.search("photocopy")
    assert hits and hits[0].chunk.path == "README.md"
    assert "photocopies" in hits[0].chunk.text


def test_one_chunk_per_file(tmp_path):
    _, index = _index(tmp_path)
    hits = index.search("greet service run")
    paths = [h.chunk.path for h in hits]
    assert len(paths) == len(set(paths))


def test_empty_repo_returns_no_hits(tmp_path):
    graph = build_repo_graph(tmp_path)
    index = build_retrieval_index(tmp_path, graph)
    assert index.search("anything") == []


def test_pack_hits_respects_budget(tmp_path):
    _, index = _index(tmp_path)
    hits = index.search("greet service", top_k=8)
    packed = pack_hits(hits, budget_chars=40)
    assert len(packed) < 200


def test_sources_from_hits_shape(tmp_path):
    _, index = _index(tmp_path)
    hits = index.search("greet")
    sources = sources_from_hits(hits)
    assert sources
    src = sources[0]
    assert {"path", "start", "end", "snippet"} <= set(src)
    assert src["path"].endswith(".py")
    assert src["end"] >= src["start"]


def test_foreign_language_chunks_attributed(tmp_path):
    (root := tmp_path / "web").mkdir(parents=True)
    (root / "index.js").write_text(
        "function render(name) {\n  return '<b>' + name + '</b>';\n}\n",
        encoding="utf-8",
    )
    graph, index = _index(root)
    assert any(sym.module == "index" for sym in graph.symbols)
    hits = index.search("render")
    assert hits and hits[0].chunk.path == "index.js"


def test_question_with_module_dots(tmp_path):
    _, index = _index(tmp_path)
    hits = index.search("app.service greet")
    assert hits
    assert any("module match" in r for h in hits for r in h.reasons)


def test_doc_file_indexed_as_single_chunk(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "architecture.md").write_text(
        "\n".join(f"line {i} of the architecture document" for i in range(400)),
        encoding="utf-8",
    )
    _, index = _index(tmp_path)
    docs = [c for c in index.chunks if c.path == "docs/architecture.md"]
    assert len(docs) == 1
    assert docs[0].start == 1 and docs[0].end == 400


def test_root_readme_boosted_above_code(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "widget.py").write_text(
        "# zebroid logic lives here, spelled only in comments\n"
        "def process():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "# demo\n\nThe zebroid subsystem is the core of this repo.\n", encoding="utf-8"
    )
    graph = build_repo_graph(tmp_path)
    index = build_retrieval_index(tmp_path, graph)
    hits = index.search("zebroid")
    assert hits and hits[0].chunk.path == "README.md"
    assert any("readme" in r for h in hits for r in h.reasons)


def test_symbol_end_line_recorded(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "app.py").write_text(
        "def first():\n    return 1\n\n\ndef second():\n    return 2\n", encoding="utf-8"
    )
    graph = build_repo_graph(root)
    first = next(s for s in graph.symbols if s.name == "first")
    second = next(s for s in graph.symbols if s.name == "second")
    assert first.end_line == 2
    assert second.end_line == 6


def test_symbol_chunks_do_not_split_functions(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    src = "\n".join(
        [f"def fn{i}():\n    return {i}\n" for i in range(20)]
    )
    (root / "app.py").write_text(src, encoding="utf-8")
    graph = build_repo_graph(root)
    index = build_retrieval_index(root, graph)
    chunks = [c for c in index.chunks if c.path == "app.py"]
    assert len(chunks) >= 20
    assert all(not c.is_skeleton for c in chunks if not c.is_skeleton)


def test_chunk_header_contains_fqn_and_signature(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "app.py").write_text(
        "class Greeter:\n    def greet(self, name):\n        return f'hi {name}'\n",
        encoding="utf-8",
    )
    graph = build_repo_graph(root)
    index = build_retrieval_index(root, graph)
    method_chunk = next(
        c for c in index.chunks if c.fqn and c.fqn.endswith("Greeter.greet")
    )
    assert "# app.py" in method_chunk.header
    assert "greet(self, name)" in method_chunk.header


def test_skeleton_chunk_has_signatures_only(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "app.py").write_text(
        "import os\n\n\ndef alpha():\n    return os.getcwd()\n\n\n"
        "class Beta:\n    def run(self):\n        return 1\n",
        encoding="utf-8",
    )
    graph = build_repo_graph(root)
    index = build_retrieval_index(root, graph)
    skeleton = next(c for c in index.chunks if c.is_skeleton and c.path == "app.py")
    assert "def alpha()" in skeleton.text
    assert "class Beta" in skeleton.text
    assert "return os.getcwd()" not in skeleton.text


def test_skeleton_chunk_never_wins_over_symbol_chunk(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "app.py").write_text(
        "def zebra():\n    return 'stripes'\n", encoding="utf-8"
    )
    graph = build_repo_graph(root)
    index = build_retrieval_index(root, graph)
    hits = index.search("zebra stripes")
    assert hits
    assert hits[0].chunk.path == "app.py"
    assert not hits[0].chunk.is_skeleton


def test_pack_hits_includes_headers(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "app.py").write_text(
        "def zebra():\n    return 'stripes'\n", encoding="utf-8"
    )
    graph = build_repo_graph(root)
    index = build_retrieval_index(root, graph)
    packed = pack_hits(index.search("zebra"))
    assert "def zebra()" in packed


def test_rrf_merges_ranked_lists():
    from clio.retrieval import rrf_merge

    merged = rrf_merge([[0, 1, 2], [2, 0, 3], [3]])
    assert merged[0] == 0
    assert merged[1] in (2, 3)
    assert len(merged) == 4


def test_search_uses_rrf_ordering(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "one.py").write_text("def alpha():\n    beta()\n", encoding="utf-8")
    (root / "two.py").write_text("def beta():\n    pass\n", encoding="utf-8")
    graph = build_repo_graph(root)
    index = build_retrieval_index(root, graph)
    hits = index.search("where is beta called", top_k=5)
    paths = [h.chunk.path for h in hits]
    assert "one.py" in paths
    assert "two.py" in paths
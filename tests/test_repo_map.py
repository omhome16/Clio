# tests/test_repo_map.py
import pytest

from clio.graph import build_repo_graph
from clio.repo_map import fitted_repo_map, module_importance, ranked_modules


def _py_repo(write_tree):
    return write_tree(
        {
            "app/__init__.py": "",
            "app/core.py": (
                "def seeded():\n    pass\n\n"
                "def helper():\n    return 1\n"
            ),
            "app/service.py": (
                "from app.core import seeded, helper\n\n"
                "def serve():\n"
                "    x = seeded()\n"
                "    return helper() + x\n"
            ),
            "app/main.py": (
                "from app.service import serve\n\n"
                "def run():\n"
                "    return serve()\n"
            ),
        }
    )


def test_module_importance_ranks_leaves_highest(write_tree):
    root = _py_repo(write_tree)
    graph = build_repo_graph(root)
    ranks = module_importance(graph)
    # PageRank flows down the reference chain (importers feed importees):
    # core is referenced by service, which is referenced by main.
    assert ranks["app.core"] > ranks["app.service"] > ranks["app.main"]
    assert "app.core" in ranks


def test_ranked_modules_ordered(write_tree):
    root = _py_repo(write_tree)
    graph = build_repo_graph(root)
    ordered = ranked_modules(graph)
    assert ordered[0] == "app.core"  # most-referenced = architectural spine
    assert set(ordered) >= {"app.core", "app.service", "app.main"}


def test_fitted_repo_map_fits_budget(write_tree):
    root = _py_repo(write_tree)
    graph = build_repo_graph(root)
    text = fitted_repo_map(graph, budget_chars=1200)
    assert "app.main" in text
    assert "serve" in text
    assert len(text) <= 1200 * 1.25


def test_fitted_repo_map_empty_graph():
    from clio.graph import RepoGraph

    text = fitted_repo_map(RepoGraph(root="."))
    assert "no code graph" in text


def test_fitted_repo_map_tight_budget_shrinks(write_tree):
    root = _py_repo(write_tree)
    graph = build_repo_graph(root)
    big = fitted_repo_map(graph, budget_chars=1200)
    small = fitted_repo_map(graph, budget_chars=60)
    assert len(small) <= len(big)

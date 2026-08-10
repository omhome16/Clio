# tests/test_bench.py
import pytest

from clio.bench import benchmark


def test_benchmark_phases_present(write_tree, tmp_path):
    root = write_tree({"app/__init__.py": "", "app/service.py": "def greet() -> str:\n    return 'hi'\n"})
    report = benchmark(root, tmp_path / "out", impact_target="app.service::greet")
    assert list(report.phases) == ["graph", "save", "load", "cluster", "impact"]
    assert all(t >= 0 for t in report.phases.values())
    assert report.metadata["modules"] == 2


def test_benchmark_persists_graph_db(write_tree, tmp_path):
    root = write_tree({"app/__init__.py": "", "app/service.py": "def greet() -> str:\n    return 'hi'\n"})
    report = benchmark(root, tmp_path / "out", impact_target="app.service::greet")
    db = tmp_path / "out" / "jobs" / "bench.graph.db"
    assert db.is_file()
    assert report.metadata["db_bytes"] > 0
    assert report.metadata["calls"] == 0


def test_bench_main_prints_json(write_tree, tmp_path, capsys):
    root = write_tree({"app/__init__.py": "", "app/service.py": "def greet() -> str:\n    return 'hi'\n"})
    from clio.bench import main as bench_main
    code = bench_main([str(root), "--root", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert code == 0
    assert '"phases"' in out
    assert "graph" in out

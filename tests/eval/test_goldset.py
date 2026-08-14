import json
import subprocess
from pathlib import Path

from tests.eval.goldset import (
    build_goldset,
    metric_budgeted_coverage,
    metric_mrr,
    metric_recall_at_k,
)


def test_build_goldset_extracts_fix_commits(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def main():\n    pass\n")
    (repo / "README.md").write_text("# repo\n")
    for cmd in [
        ["git", "init"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ]:
        subprocess.run(cmd, cwd=repo, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
    (repo / "app.py").write_text("def main():\n    return 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "fix: return correct value"], cwd=repo, capture_output=True
    )

    out = tmp_path / "gold.jsonl"
    gs = build_goldset(repo, out)
    assert len(gs) == 1
    assert gs[0]["query"] == "fix: return correct value"
    assert "app.py" in gs[0]["gold_paths"]
    assert gs[0]["docs_only"] is False
    assert json.loads(out.read_text())["query"] == "fix: return correct value"


def test_docs_only_commit_flagged(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# repo\n")
    for cmd in [
        ["git", "init"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ]:
        subprocess.run(cmd, cwd=repo, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
    (repo / "README.md").write_text("# repo\nmore docs\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "fix: clarify docs"], cwd=repo, capture_output=True
    )

    gs = build_goldset(repo, tmp_path / "g.jsonl")
    assert len(gs) == 1
    assert gs[0]["docs_only"] is True


def test_metrics():
    gold = {"src/a.py", "src/b.py"}
    assert metric_mrr(["src/a.py", "x.py"], gold) == 1.0
    assert metric_mrr(["x.py", "src/a.py"], gold) == 0.5
    assert metric_recall_at_k(["src/a.py"], gold, 8) == 0.5
    assert metric_budgeted_coverage(["x.py", "src/a.py", "src/b.py"], gold, 8) == 1.0
    assert metric_budgeted_coverage(["x.py", "src/a.py"], gold, 8) == 0.5
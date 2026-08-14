"""CLI: python -m tests.eval.run_eval <repo> [goldset.jsonl]"""
import sys
from pathlib import Path

from clio.graph import build_repo_graph
from clio.retrieval import build_retrieval_index
from tests.eval.goldset import build_goldset, load_goldset, run_eval


def make_index_factory(workspace: Path):
    graph = build_repo_graph(workspace)
    index = build_retrieval_index(workspace, graph)

    def factory(query: str) -> list[str]:
        hits = index.search(query, top_k=8)
        return [h.chunk.path for h in hits]

    return factory


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m tests.eval.run_eval <repo> [goldset.jsonl]")
        return 1
    repo = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else repo / "goldset.jsonl"
    if out.parent == repo:
        goldset = build_goldset(repo, out)
    elif out.is_file():
        goldset = load_goldset(out)
    else:
        goldset = build_goldset(repo, out)
    results = run_eval(make_index_factory(repo), goldset)
    for key, value in results.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
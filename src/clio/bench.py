# src/clio/bench.py
"""Phase benchmark for the graph stack: extraction, persistence, queries."""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from clio.clustering import cluster_by_package
from clio.graph import build_repo_graph
from clio.impact import impact_of_symbol
from clio.reports import ReportArchive
from clio.store import GraphStore

PHASES = ("graph", "save", "load", "cluster", "impact")


@dataclass
class BenchReport:
    repo: str
    phases: dict[str, float]   # phase name -> seconds
    metadata: dict


def _timed(fn) -> tuple[float, object]:
    start = time.perf_counter()
    result = fn()
    return time.perf_counter() - start, result


def benchmark(root: Path, out_root: Path, impact_target: str = "app.service::greet") -> BenchReport:
    root = Path(root)
    out = Path(out_root)
    jobs = out / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    t_graph, graph = _timed(lambda: build_repo_graph(root))
    db_path = jobs / "bench.graph.db"
    t_save, _ = _timed(lambda: GraphStore(db_path).save(graph))
    t_load, loaded = _timed(lambda: GraphStore(db_path).load())
    t_cluster, _ = _timed(lambda: cluster_by_package(loaded))
    t_impact, _ = _timed(
        lambda: impact_of_symbol(ReportArchive(out), "bench", impact_target)
    )
    metadata = {
        "modules": loaded.module_count,
        "symbols": loaded.symbol_count,
        "calls": loaded.call_count,
        "db_bytes": db_path.stat().st_size,
    }
    return BenchReport(
        repo=str(root),
        phases={
            "graph": t_graph, "save": t_save, "load": t_load,
            "cluster": t_cluster, "impact": t_impact,
        },
        metadata=metadata,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clio.bench", description="Benchmark the analysis stack")
    parser.add_argument("repo", help="path to a local python repo")
    parser.add_argument("--root", default="sandbox", help="output root for the bench graph db")
    parser.add_argument("--impact", default="app.service::greet", help="impact query target")
    args = parser.parse_args(argv)
    report = benchmark(Path(args.repo), Path(args.root), impact_target=args.impact)
    print(f"bench {report.repo}")
    for name, seconds in report.phases.items():
        print(f"  {name:<8}{seconds * 1000:8.1f} ms")
    print(json.dumps(asdict(report), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

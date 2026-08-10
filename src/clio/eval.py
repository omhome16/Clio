# src/clio/eval.py
"""Golden-repo evals: deterministic precision/recall vs hand-verified keys."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from clio.graph import RepoGraph, build_repo_graph
from clio.impact import impact_of_symbol
from clio.reports import ReportArchive
from clio.store import GraphStore

_EPS = 1e-9


@dataclass
class GoldenCase:
    name: str
    files: dict[str, str]
    expected_symbols: set[str]                # "module::symbol"
    expected_edges: set[tuple[str, str]]      # (caller, callee) as emitted
    impact: dict[str, dict]                   # symbol_id -> {"verdict": str, "affected": [..]}
    min_recall: float = 1.0


@dataclass
class EvalResult:
    case: str
    metrics: dict[str, float]
    passed: bool
    failures: list[str]


def golden_cases() -> list[GoldenCase]:
    return [
        GoldenCase(
            name="toy",
            files={
                "app/__init__.py": "",
                "app/service.py": "def greet(name: str) -> str:\n    return f'hello {name}'\n",
                "app/main.py": "from app.service import greet\n\n\ndef run() -> str:\n    return greet('clio')\n",
            },
            expected_symbols={"app.service::greet", "app.main::run"},
            expected_edges={("app.main::run", "greet")},
            impact={
                "app.service::greet": {"verdict": "contained", "affected": ["app.main"]},
                "app.main::run": {"verdict": "contained", "affected": []},
            },
        ),
        GoldenCase(
            name="nested",
            files={
                "core/__init__.py": "",
                "core/utils.py": "def helper() -> str:\n    return 'ok'\n",
                "core/engine.py": (
                    "from core.utils import helper\n\n\n"
                    "def make() -> str:\n"
                    "    return helper()\n\n\n"
                    "class Engine:\n"
                    "    def run(self) -> str:\n"
                    "        return make()\n"
                ),
                "web/__init__.py": "",
                "web/app.py": (
                    "from core.engine import Engine\n\n\n"
                    "def start() -> str:\n"
                    "    return Engine().run()\n"
                ),
            },
            expected_symbols={
                "core.utils::helper", "core.engine::make",
                "core.engine::Engine", "core.engine::Engine.run",
                "web.app::start",
            },
            expected_edges={
                ("core.engine::make", "helper"),
                ("core.engine::Engine.run", "core.engine::make"),
                ("web.app::start", "Engine"),
            },
            impact={
                "core.engine::make": {
                    "verdict": "cross-cutting", "affected": ["core.engine", "web.app"],
                },
            },
        ),
        GoldenCase(
            name="regression",
            files={
                "lib/__init__.py": "",
                "lib/mathx.py": "def double(x: int) -> int:\n    return x * 2\n",
                "app/__init__.py": "",
                "app/run.py": (
                    "from lib.mathx import double\n\n\n"
                    "def compute(x: int) -> int:\n"
                    "    return double(x) + external_helper(x)\n"
                ),
                "broken/__init__.py": "",
                "broken/naughty.py": "def bad(:\n    pass\n",
            },
            expected_symbols={
                "lib.mathx::double", "app.run::compute", "broken.naughty::bad",
            },
            expected_edges={
                ("app.run::compute", "double"),
                ("app.run::compute", "external_helper"),
            },
            impact={},
            min_recall=2 / 3,
        ),
    ]


def _build_graph(files: dict[str, str], root: Path) -> RepoGraph:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return build_repo_graph(root)


def _symbol_ids(graph: RepoGraph) -> set[str]:
    return {f"{s.module}::{s.name}" for s in graph.symbols}


def _edge_ids(graph: RepoGraph) -> set[tuple[str, str]]:
    return {(c.caller, c.callee) for c in graph.calls}


def _precision(extracted: set, expected: set) -> float:
    if not extracted:
        return 0.0
    return len(extracted & expected) / len(extracted)


def _recall(extracted: set, expected: set) -> float:
    if not expected:
        return 1.0
    return len(extracted & expected) / len(expected)


def evaluate_case(case: GoldenCase, root: Path) -> EvalResult:
    root = Path(root)
    job_id = f"eval-{case.name}"
    graph = _build_graph(case.files, root / "repo" / case.name)
    (root / "jobs").mkdir(parents=True, exist_ok=True)
    GraphStore(root / "jobs" / f"{job_id}.graph.db").save(graph)
    extracted_symbols = _symbol_ids(graph)
    extracted_edges = _edge_ids(graph)
    metrics = {
        "symbol_precision": _precision(extracted_symbols, case.expected_symbols),
        "symbol_recall": _recall(extracted_symbols, case.expected_symbols),
        "edge_precision": _precision(extracted_edges, case.expected_edges),
        "edge_recall": _recall(extracted_edges, case.expected_edges),
    }
    failures: list[str] = []
    archive = ReportArchive(root)
    for symbol_id, expected in case.impact.items():
        report = impact_of_symbol(archive, job_id, symbol_id)
        verdict_ok = report.verdict == expected["verdict"]
        metrics[f"impact:{symbol_id}:verdict_ok"] = 1.0 if verdict_ok else 0.0
        if not verdict_ok:
            failures.append(f"{symbol_id}: verdict {report.verdict} != {expected['verdict']}")
        if set(report.affected_modules) != set(expected["affected"]):
            failures.append(f"{symbol_id}: affected {report.affected_modules} != {expected['affected']}")
    if metrics["symbol_recall"] < case.min_recall - _EPS:
        failures.append(
            f"symbol recall {metrics['symbol_recall']:.3f} < {case.min_recall}"
        )
    return EvalResult(case=case.name, metrics=metrics, passed=not failures, failures=failures)


def run_golden_suite(root: Path) -> list[EvalResult]:
    return [evaluate_case(case, root) for case in golden_cases()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clio.eval", description="Run the golden repo eval suite")
    parser.add_argument("--root", default="sandbox", help="workspace root for eval artifacts")
    args = parser.parse_args(argv)
    results = run_golden_suite(Path(args.root))
    print(f"{'case':<12}{'sym-p':>7}{'sym-r':>7}{'edge-p':>7}{'edge-r':>7}  status")
    for r in results:
        m = r.metrics
        status = "PASS" if r.passed else "FAIL"
        print(
            f"{r.case:<12}{m['symbol_precision']:>7.2f}{m['symbol_recall']:>7.2f}"
            f"{m['edge_precision']:>7.2f}{m['edge_recall']:>7.2f}  {status}"
        )
        for f in r.failures:
            print(f"    - {f}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

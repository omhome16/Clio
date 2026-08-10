# M6 — Evals + benchmark (golden repos, deterministic graders, phase timing)

- Date: 2026-08-10
- Branch: `feat/m6-evals-benchmark`
- Precondition: M0-M5 merged to `main` (137 tests passing)
- Target: 147 tests passing (137 + 7 eval + 3 bench)
- Offline-friendly: stdlib-only, mock LLM never invoked (pure AST/graph work), no API keys

## What M6 delivers

The blueprint's "honest evals" section becomes real: a golden-repo suite with
hand-verified answer keys and deterministic graders (symbol/edge precision &
recall vs AST ground truth, impact-query verdict checks), plus a benchmark of
the graph stack's phases. These numbers ARE the resume: precision/recall
tables and ms-per-phase curves, reproducible offline.

1. **`clio/eval.py`** — `GoldenCase` + `evaluate_case` + `run_golden_suite`:
   three shipped cases (toy, nested, regression) with expected symbols, call
   edges, and impact answers; per-case metric table; PASS/FAIL summary.
   Entrypoint: `python -m clio.eval`.
2. **`clio/bench.py`** — `benchmark()` times graph extraction, save, load,
   clustering, and an impact query on a local repo; reports per-phase ms plus
   metadata (modules/symbols/calls/db bytes). Entrypoint: `python -m clio.bench`.
3. No changes to `cli.py` (M1 contract stays stable); eval/bench are separate
   modules with `__main__` blocks.

## Design decisions

- **Answer keys are hand-verified against the actual extractor.** Edges are
  stored as-emitted by `build_repo_graph` — e.g. a call to an imported name
  `greet(...)` resolves to the plain callee `"greet"`, and `Engine().run()`
  contributes an unresolved edge for the inner `Engine()` call (`("web.app::start", "Engine")`).
  The plan's keys were traced and machine-verified against `graph.py`'s
  resolution rules before dispatch.
- **Impact answers use `impact_of_symbol` through `ReportArchive`** exactly
  like the CLI: graph saved to `<root>/jobs/eval-<case>.graph.db`, checks
  compare verdict AND the affected-modules set.
- **Regression case is honest.** `broken/naughty.py` is intentionally
  unparseable (`def bad(:`), so it lands in `skipped` and symbol recall drops
  to 2/3. The case's `min_recall` is set to 2/3, so the suite stays green
  while the table documents the known loss. Tests pin the exact value.
- **Bench phases** are the stack's hot path: graph extraction, SQLite save,
  SQLite load, clustering, one impact query (callers + importers + verdict).
  Timing via `time.perf_counter`; metadata from `RepoGraph` counts + db size.
- **Threshold semantics:** a case passes when symbol recall >= `min_recall`
  (epsilon 1e-9) and every impact check matches. Precision/recall both 1.0
  when the expected set is empty.

## Contracts

- `clio.eval.GoldenCase(name, files, expected_symbols, expected_edges, impact, min_recall=1.0)`
- `clio.eval.evaluate_case(case, root) -> EvalResult` (metrics dict, passed, failures)
- `clio.eval.run_golden_suite(root) -> list[EvalResult]`
- `clio.eval.main(argv=None) -> int` — prints table; exit 1 when any case fails
- `clio.bench.benchmark(root, out_root, impact_target) -> BenchReport` (phases dict in seconds)
- `clio.bench.main(argv=None) -> int` — prints ms table + full JSON; `--impact` flag
- Symbol ids: `module::name` (methods `module::Class.method`), as emitted by the graph.

---

## Task 1: `clio/eval.py` — golden suite + deterministic graders

### Step 1: Write `tests/test_eval.py` (graders first)

```python
# tests/test_eval.py
import pytest

from clio.eval import GoldenCase, evaluate_case, golden_cases, run_golden_suite


def test_toy_symbol_metrics_perfect(tmp_path):
    result = evaluate_case(golden_cases()[0], tmp_path)
    assert result.passed
    assert result.metrics["symbol_precision"] == 1.0
    assert result.metrics["symbol_recall"] == 1.0


def test_toy_edge_metrics_perfect(tmp_path):
    result = evaluate_case(golden_cases()[0], tmp_path)
    assert result.passed
    assert result.metrics["edge_precision"] == 1.0
    assert result.metrics["edge_recall"] == 1.0


def test_nested_impact_cross_cutting(tmp_path):
    result = evaluate_case(golden_cases()[1], tmp_path)
    assert result.passed
    assert result.metrics["impact:core.engine::make:verdict_ok"] == 1.0


def test_missing_impact_symbol_graded(tmp_path):
    case = GoldenCase(
        name="missing",
        files={"a.py": "def f():\n    return 1\n"},
        expected_symbols={"a::f"},
        expected_edges=set(),
        impact={"a::nope": {"verdict": "missing", "affected": []}},
    )
    result = evaluate_case(case, tmp_path)
    assert result.passed
    assert result.metrics["impact:a::nope:verdict_ok"] == 1.0


def test_regression_recall_degradation_detected(tmp_path):
    result = evaluate_case(golden_cases()[2], tmp_path)
    assert result.passed
    assert result.metrics["symbol_recall"] == pytest.approx(2 / 3)


def test_golden_suite_aggregates(tmp_path):
    results = run_golden_suite(tmp_path)
    assert [r.case for r in results] == ["toy", "nested", "regression"]
    assert all(r.passed for r in results)


def test_eval_main_prints_table(tmp_path, capsys):
    from clio.eval import main as eval_main
    code = eval_main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    for name in ("toy", "nested", "regression"):
        assert name in out
    assert "PASS" in out
```

### Step 2: Run — verify FAIL

Run: `python -m pytest tests/test_eval.py -v`
Expected: collection error — `cannot import name 'GoldenCase' from 'clio.eval'`.

### Step 3: Write `src/clio/eval.py`

```python
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
    graph = _build_graph(case.files, root / "repo")
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
```

### Step 4: Run — verify 7 passed

Run: `python -m pytest tests/test_eval.py -v`
Expected: 7 passed.

### Step 5: Commit

`git add src/clio/eval.py tests/test_eval.py` then
`git commit -m "feat: golden repo eval suite with deterministic graders"`

---

## Task 2: `clio/bench.py` — phase benchmark

### Step 1: Write `tests/test_bench.py` (report shape first)

```python
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
```

### Step 2: Run — verify FAIL

Run: `python -m pytest tests/test_bench.py -v`
Expected: collection error — `cannot import name 'benchmark' from 'clio.bench'`.

### Step 3: Write `src/clio/bench.py`

```python
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
```

### Step 4: Run — verify 3 passed

Run: `python -m pytest tests/test_bench.py -v`
Expected: 3 passed.

### Step 5: Commit

`git add src/clio/bench.py tests/test_bench.py` then
`git commit -m "feat: benchmark harness for graph stack phases"`

---

## Full-suite verification

- [ ] Run `python -m pytest -q` — expected **147 passed** (137 + 7 eval + 3 bench). All offline.
- [ ] Manual demo (no API key needed, dogfood on the Clio repo itself):

```powershell
python -m clio.eval --root C:\Users\omnaw\AppData\Local\Temp\opencode\m6-eval
python -m clio.bench src --root C:\Users\omnaw\AppData\Local\Temp\opencode\m6-bench --impact src.clio.orchestrator::Orchestrator.run
```

Expected: eval prints the toy/nested/regression table, all PASS, symbol recall
2/3 on regression (documented loss); bench prints ms per phase for `src/` and a
JSON report with real metadata (modules/symbols/calls/db_bytes).
- [ ] Update `README.md` status table: mark M6 done.
- [ ] Commit: `git add README.md docs/plans/2026-08-10-m6-evals-benchmark.md` then `git commit -m "docs: mark M6 complete in README"`
- [ ] Merge to `main`, push, delete `feat/m6-evals-benchmark`.

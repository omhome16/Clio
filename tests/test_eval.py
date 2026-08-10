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

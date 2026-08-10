# tests/test_reports.py
from clio.reports import ReportArchive


def test_list_reports(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": ""})
    seed_job(root, "job-2", "2026-08-10T01:00:00+00:00", {"b.py": ""})
    reports = ReportArchive(root).list_reports()
    assert [r["job_id"] for r in reports] == ["job-1", "job-2"]
    assert all(r["summary"] == "merged" for r in reports)


def test_list_reports_empty(tmp_path):
    assert ReportArchive(tmp_path).list_reports() == []


def test_get_report(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": ""})
    archive = ReportArchive(root)
    assert archive.get_report("job-1")["job_id"] == "job-1"
    assert archive.get_report("nope") is None


def test_latest(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": ""})
    seed_job(root, "job-2", "2026-08-10T01:00:00+00:00", {"b.py": ""})
    assert ReportArchive(root).latest()["job_id"] == "job-2"
    assert ReportArchive(tmp_path / "empty").latest() is None


def test_get_graph(tmp_path, seed_job):
    root = tmp_path / "root"
    seed_job(root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": "def f():\n    return 1\n"})
    archive = ReportArchive(root)
    graph = archive.get_graph("job-1")
    assert graph is not None and graph.symbol_count == 1
    assert archive.get_graph("nope") is None


def test_corrupt_report_skipped(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "bad.report.json").write_text("{not json", encoding="utf-8")
    assert ReportArchive(tmp_path).list_reports() == []
    assert ReportArchive(tmp_path).get_report("bad") is None

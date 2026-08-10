# tests/test_web.py
import json
import urllib.request

import pytest

from clio.reports import ReportArchive
from clio.web import Dashboard

INDEX_MARKER = "clio-dashboard"


@pytest.fixture
def dashboard(tmp_path):
    dash = Dashboard(tmp_path / "sandbox", port=0)
    url = dash.start()
    yield dash, url
    dash.stop()


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _post(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_index_served(dashboard):
    _, url = dashboard
    status, body = _get(url + "/")
    assert status == 200
    assert INDEX_MARKER in body


def test_api_jobs_list(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": ""})
    seed_job(dash.root, "job-2", "2026-08-10T01:00:00+00:00", {"b.py": ""})
    status, body = _get(url + "/api/jobs")
    assert status == 200
    jobs = json.loads(body)["jobs"]
    assert [j["job_id"] for j in jobs] == ["job-1", "job-2"]


def test_api_job_report(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": "def f():\n    return 1\n"})
    status, body = _get(url + "/api/jobs/job-1")
    assert status == 200
    assert json.loads(body)["job_id"] == "job-1"
    assert _get(url + "/api/jobs/nope")[0] == 404


def test_api_job_graph(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": "def f():\n    return 1\n"})
    status, body = _get(url + "/api/jobs/job-1/graph")
    assert status == 200
    payload = json.loads(body)
    assert payload["stats"]["modules"] == 1
    assert payload["stats"]["symbols"] == 1
    assert payload["clusters"] and payload["clusters"][0]["name"] == "a"
    assert _get(url + "/api/jobs/nope/graph")[0] == 404


def test_api_unknown_route(dashboard):
    _, url = dashboard
    assert _get(url + "/api/nope")[0] == 404


def test_analyze_and_stream(dashboard, local_repo):
    dash, url = dashboard
    status, body = _post(url + "/api/analyze?url=" + urllib.parse.quote(local_repo.as_uri()))
    assert status == 200
    job_id = json.loads(body)["job_id"]
    stream_url = f"{url}/api/stream?job_id={job_id}"
    with urllib.request.urlopen(stream_url, timeout=60) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/event-stream"
        data = resp.read().decode("utf-8")
    types = [line.split(": ", 1)[1] for line in data.splitlines() if line.startswith("data: ")]
    events = [json.loads(line) for line in types if '"type"' in line]
    seen = {e["type"] for e in events}
    assert {"job.created", "job.cloned", "job.graphed", "job.persisted"} <= seen
    assert "subagent.start" in seen
    report = ReportArchive(dash.root).get_report(job_id)
    assert report is not None and report["summary"] == "merged"


def test_analyze_failed_job_streams_failure(dashboard):
    _, url = dashboard
    status, body = _post(url + "/api/analyze?url=" + urllib.parse.quote("file:///definitely/missing/repo"))
    assert status == 200
    job_id = json.loads(body)["job_id"]
    stream_url = f"{url}/api/stream?job_id={job_id}"
    with urllib.request.urlopen(stream_url, timeout=60) as resp:
        data = resp.read().decode("utf-8")
    assert "job.failed" in data
    assert "event: done" in data

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
    assert all("status" in j for j in jobs)


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


def test_api_job_map_payload(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {
        "pkg/__init__.py": "",
        "pkg/one.py": "def a():\n    return 1\n",
        "pkg/two.py": "import pkg.one\ndef b():\n    return 1\n",
        "main.py": "import pkg.two\n",
    })
    status, body = _get(url + "/api/jobs/job-1/graph/map")
    assert status == 200
    payload = json.loads(body)
    assert {n["module"] for n in payload["nodes"]} == {"main", "pkg", "pkg.one", "pkg.two"}
    for node in payload["nodes"]:
        assert set(node) == {"id", "module", "cluster", "symbols", "x", "y"}
    assert {e["to"] for e in payload["edges"] if e["from"] == "main"} == {"pkg.two"}
    assert ("pkg.two", "pkg.one", "import") in {
        (e["from"], e["to"], e["kind"]) for e in payload["edges"]
    }
    assert _get(url + "/api/jobs/nope/graph/map")[0] == 404


def test_api_job_map_impact_param(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {
        "pkg/__init__.py": "",
        "pkg/one.py": "def a():\n    return 1\n",
        "pkg/two.py": "import pkg.one\ndef b():\n    return 1\n",
        "main.py": "import pkg.two\n",
    })
    status, body = _get(url + "/api/jobs/job-1/graph/map?impact=pkg.one")
    assert status == 200
    impact = json.loads(body)["impact"]
    assert impact["scope"] == "pkg.one"
    assert impact["verdict"] == "cross-cutting"
    assert impact["affected_modules"] == ["main", "pkg.one", "pkg.two"]
    status, body = _get(url + "/api/jobs/job-1/graph/map?impact=unknown")
    assert json.loads(body)["impact"]["verdict"] == "missing"


def test_api_job_map_deterministic_http(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {
        "pkg/__init__.py": "",
        "pkg/one.py": "def a():\n    return 1\n",
        "main.py": "import pkg.one\n",
    })
    _, first = _get(url + "/api/jobs/job-1/graph/map")
    _, second = _get(url + "/api/jobs/job-1/graph/map")
    assert first == second


def test_index_map_present():
    from clio.web import INDEX_HTML
    assert 'id="map"' in INDEX_HTML
    assert "Module map" in INDEX_HTML
    assert "/graph/map" in INDEX_HTML


def test_index_map_detail_panel():
    from clio.web import INDEX_HTML
    assert 'id="map-detail"' in INDEX_HTML
    assert "Impact" in INDEX_HTML


def test_index_map_reduced_motion():
    from clio.web import INDEX_HTML
    assert "prefers-reduced-motion" in INDEX_HTML
    assert "#map .node.impact rect { animation:none; }" in INDEX_HTML


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


def test_run_job_builds_provider_client(monkeypatch, tmp_path):
    calls = {}

    class FakeClient:
        async def complete(self, messages, **kwargs):
            return '{"final": "done"}'

    def fake_make_client(provider, limits=None):
        calls["provider"] = provider
        calls["limits"] = limits
        return FakeClient()

    class FakeOrchestrator:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, url, root, job_id):
            calls["job_id"] = job_id
            return None

    monkeypatch.setattr("clio.web.make_client", fake_make_client)
    monkeypatch.setattr("clio.web.Orchestrator", FakeOrchestrator)
    monkeypatch.setenv("CLIO_PROVIDER", "groq")
    dashboard = Dashboard(root=tmp_path)
    dashboard.run_job("file:///tmp/x", "job-1")
    assert calls["provider"] == "groq"
    assert calls["job_id"] == "job-1"


def test_api_ask_streams_answer(dashboard, seed_job, monkeypatch):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00",
             {"a.py": "def f():\n    return 1\n"})

    class FakeClient:
        async def complete(self, messages, **kwargs):
            return '{"final": "a::f is a function"}'

    monkeypatch.setattr("clio.web.make_client", lambda provider, limits=None: FakeClient())
    status, body = _get(url + "/api/ask?job_id=job-1&q=" + urllib.parse.quote("what is a::f?"))
    assert status == 200
    assert "ask.final" in body
    assert "a::f is a function" in body
    assert "event: done" in body


def test_api_ask_unknown_job_404(dashboard):
    _, url = dashboard
    assert _get(url + "/api/ask?job_id=nope&q=hi")[0] == 404


def test_api_ask_missing_question_400(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": ""})
    assert _get(url + "/api/ask?job_id=job-1")[0] == 400

def test_index_theme_toggle_present(dashboard):
    _, url = dashboard
    status, body = _get(url + "/")
    assert status == 200
    assert "clio-theme" in body
    assert "prefers-color-scheme" in body
    assert 'data-theme="dark"' in body

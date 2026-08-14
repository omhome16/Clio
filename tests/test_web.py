# tests/test_web.py
import json
import urllib.request

import pytest

from clio.reports import ReportArchive
from clio.web import Dashboard

INDEX_MARKER = "clio-app"


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


def _delete(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _inject_fake_llm(monkeypatch) -> None:
    from clio.config import get_limits
    from clio.llm import FakeLLM, fake_handler

    monkeypatch.setattr(
        "clio.web.make_client",
        lambda provider, limits=None: FakeLLM(handler=fake_handler(get_limits())),
    )


def test_index_served(dashboard):
    _, url = dashboard
    status, body = _get(url + "/")
    assert status == 200
    assert INDEX_MARKER in body


def test_index_has_chat_and_guide_markers():
    from clio.web import INDEX_HTML
    assert "chat-form" in INDEX_HTML
    assert "data-stage" in INDEX_HTML
    assert "/api/guide" in INDEX_HTML
    assert "/api/file" in INDEX_HTML


def test_api_jobs_list(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": ""})
    seed_job(dash.root, "job-2", "2026-08-10T01:00:00+00:00", {"b.py": ""})
    status, body = _get(url + "/api/jobs")
    assert status == 200
    jobs = json.loads(body)["jobs"]
    assert [j["job_id"] for j in jobs] == ["job-1", "job-2"]
    for job in jobs:
        assert job["summary"] == "merged"
        assert job["status"] == "PERSISTED"
        assert job["url"] == "https://github.com/x/y.git"


def test_api_guide(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": ""})
    guide = {
        "stages": {
            "what": {"text": "A tiny demo.", "sources": ["README.md"]},
            "how": {"text": "It runs.", "sources": []},
            "modules": {"text": "One module.", "sources": []},
            "run": {"text": "python a.py", "sources": []},
        }
    }
    (dash.root / "jobs" / "job-1.guide.json").write_text(
        json.dumps(guide), encoding="utf-8")
    status, body = _get(url + "/api/guide?job_id=job-1")
    assert status == 200
    payload = json.loads(body)
    assert payload["stages"]["what"]["text"] == "A tiny demo."
    assert payload["repo"] == "https://github.com/x/y.git"
    assert _get(url + "/api/guide?job_id=nope")[0] == 404


def test_api_modules(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {
        "pkg/__init__.py": "",
        "pkg/one.py": "import pkg.two\ndef alpha():\n    return 1\n",
        "pkg/two.py": "def beta():\n    return 2\n",
    })
    status, body = _get(url + "/api/modules?job_id=job-1")
    assert status == 200
    payload = json.loads(body)
    assert payload["count"] == 3
    by_name = {m["name"]: m for m in payload["modules"]}
    assert by_name["pkg.one"]["symbols"] == ["alpha"]
    assert by_name["pkg.one"]["imports"] == ["pkg.two"]
    assert by_name["pkg.two"]["path"] == "pkg/two.py"
    assert _get(url + "/api/modules?job_id=nope")[0] == 404


def test_api_file(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {
        "app.py": "def f():\n    return 1\n",
    })
    status, body = _get(url + "/api/file?job_id=job-1&path=app.py")
    assert status == 200
    payload = json.loads(body)
    assert payload["path"] == "app.py"
    assert payload["lines"] == ["def f():", "    return 1"]
    assert _get(url + "/api/file?job_id=job-1&path=missing.py")[0] == 404


def test_api_file_rejects_path_traversal(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {"app.py": "x"})
    assert _get(url + "/api/file?job_id=job-1&path=../outside.py")[0] == 403
    assert _get(url + "/api/file?job_id=job-1&path=..%2Foutside.py")[0] == 403


def test_api_suggest(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {
        "pkg/__init__.py": "",
        "pkg/one.py": "def alpha():\n    return 1\n",
        "pkg/two.py": "def beta():\n    return 2\n",
        "README.md": "# demo\n",
    })
    status, body = _get(url + "/api/suggest?job_id=job-1")
    assert status == 200
    chips = json.loads(body)["chips"]
    assert chips and len(chips) <= 6
    assert any("What does" in c or "Where is" in c for c in chips)


def test_api_unknown_route(dashboard):
    _, url = dashboard
    assert _get(url + "/api/nope")[0] == 404


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
    assert {e["to"] for e in payload["edges"] if e["from"] == "main"} == {"pkg.two"}
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


def test_api_job_tree(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {
        "pkg/__init__.py": "",
        "pkg/one.py": "def a():\n    return 1\n",
        "main.py": "import pkg.one\n",
    })
    status, body = _get(url + "/api/jobs/job-1/tree")
    assert status == 200
    payload = json.loads(body)
    assert payload["count"] == 3
    assert payload["files"] == ["main.py", "pkg/__init__.py", "pkg/one.py"]
    assert _get(url + "/api/jobs/nope/tree")[0] == 404


def test_api_delete_job_removes_artifacts(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": "def f():\n    return 1\n"})
    assert _get(url + "/api/jobs/job-1")[0] == 200
    status, body = _delete(url + "/api/jobs/job-1")
    assert status == 200
    assert json.loads(body)["deleted"] == "job-1"
    assert _get(url + "/api/jobs/job-1")[0] == 404
    assert _delete(url + "/api/jobs/job-1")[0] == 404


def test_api_clear_jobs(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": ""})
    seed_job(dash.root, "job-2", "2026-08-10T01:00:00+00:00", {"b.py": ""})
    status, body = _delete(url + "/api/jobs")
    assert status == 200
    assert json.loads(body)["deleted"] == 2
    assert json.loads(_get(url + "/api/jobs")[1])["jobs"] == []


def test_analyze_and_stream(dashboard, local_repo, monkeypatch):
    _inject_fake_llm(monkeypatch)
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
    assert "job.stage" in seen
    report = ReportArchive(dash.root).get_report(job_id)
    assert report is not None and report["summary"] == "fake guide text"
    guide_path = dash.root / "jobs" / f"{job_id}.guide.json"
    assert guide_path.is_file()


def test_analyze_failed_job_streams_failure(dashboard, monkeypatch):
    _inject_fake_llm(monkeypatch)
    _, url = dashboard
    status, body = _post(url + "/api/analyze?url=" + urllib.parse.quote("file:///definitely/missing/repo"))
    assert status == 200
    job_id = json.loads(body)["job_id"]
    stream_url = f"{url}/api/stream?job_id={job_id}"
    with urllib.request.urlopen(stream_url, timeout=60) as resp:
        data = resp.read().decode("utf-8")
    assert "job.failed" in data
    assert "event: done" in data


def test_api_ask_streams_answer(dashboard, seed_job, monkeypatch):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00",
             {"helper.py": "def helper():\n    return 1\n"})

    class FakeClient:
        async def complete(self, messages, **kwargs):
            return "helper returns the number 1"

    monkeypatch.setattr("clio.web.make_client", lambda provider, limits=None: FakeClient())
    status, body = _get(url + "/api/ask?job_id=job-1&q=" + urllib.parse.quote("what is helper"))
    assert status == 200
    assert "ask.final" in body
    assert "helper returns the number 1" in body
    assert "event: done" in body


def test_ask_after_analyze_writes_memory(dashboard, local_repo, monkeypatch):
    _inject_fake_llm(monkeypatch)
    dash, url = dashboard
    status, body = _post(url + "/api/analyze?url=" + urllib.parse.quote(local_repo.as_uri()))
    assert status == 200
    job_id = json.loads(body)["job_id"]
    stream_url = f"{url}/api/stream?job_id={job_id}"
    with urllib.request.urlopen(stream_url, timeout=60) as resp:
        resp.read()
    status, body = _get(url + "/api/ask?job_id=" + job_id + "&q=" + urllib.parse.quote("what does this project do"))
    assert status == 200
    assert "ok" in body and "event: done" in body
    mem = dash.root / "jobs" / f"{job_id}.memory" / "activeContext.md"
    assert mem.is_file()
    assert "Objective:" in mem.read_text(encoding="utf-8")


def test_api_ask_unknown_job_404(dashboard):
    _, url = dashboard
    assert _get(url + "/api/ask?job_id=nope&q=hi")[0] == 404


def test_api_ask_missing_question_400(dashboard, seed_job):
    dash, url = dashboard
    seed_job(dash.root, "job-1", "2026-08-10T00:00:00+00:00", {"a.py": ""})
    assert _get(url + "/api/ask?job_id=job-1")[0] == 400
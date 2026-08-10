# src/clio/web.py
"""Zero-dependency local dashboard: live event stream + archive API."""
from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from clio.cli import _mock_handler
from clio.clustering import cluster_by_package
from clio.config import Limits, get_limits
from clio.events import Event, EventBus
from clio.llm import MockLLM
from clio.orchestrator import Orchestrator
from clio.reports import ReportArchive
from clio.sandbox import Sandbox

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Clio - analysis dashboard</title>
<style>
:root { --bg:#0d1117; --fg:#e6edf3; --muted:#8b949e; --accent:#58a6ff;
        --ok:#3fb950; --bad:#f85149; --border:#30363d; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:14px/1.5 ui-monospace, "Cascadia Mono", Consolas, monospace; }
header { padding:16px 24px; border-bottom:1px solid var(--border); }
h1 { margin:0; font-size:18px; }
h1 span { color:var(--accent); }
main { display:grid; grid-template-columns: 1fr 1fr; gap:24px; padding:24px; }
@media (max-width:900px) { main { grid-template-columns:1fr; } }
.card { border:1px solid var(--border); border-radius:8px; padding:16px; }
h2 { margin:0 0 12px; font-size:13px; text-transform:uppercase;
     letter-spacing:.08em; color:var(--muted); }
input[type=text] { width:70%; padding:8px; background:#161b22;
  border:1px solid var(--border); border-radius:6px; color:var(--fg); }
button { padding:8px 16px; background:var(--accent); color:#0d1117;
         border:0; border-radius:6px; font-weight:700; cursor:pointer; }
button:disabled { opacity:.5; cursor:default; }
#log { height:360px; overflow:auto; margin:12px 0 0; padding:8px;
       background:#010409; border:1px solid var(--border); border-radius:6px;
       font-size:12px; white-space:pre-wrap; }
#log div { padding:1px 0; }
.tool { color:#d29922; } .ok { color:var(--ok); } .bad { color:var(--bad); }
table { width:100%; border-collapse:collapse; font-size:12px; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--border); }
tr[data-job]:hover { background:#161b22; cursor:pointer; }
pre { max-height:360px; overflow:auto; background:#010409; padding:8px;
      border:1px solid var(--border); border-radius:6px; font-size:11px; }
.status { display:inline-block; padding:1px 8px; border-radius:10px; font-size:11px; }
.status.ready { background:#1f6feb33; color:var(--accent); }
.status.done { background:#3fb95022; color:var(--ok); }
</style>
</head>
<body class="clio-dashboard">
<header>
  <h1>Clio <span>analysis dashboard</span> - mock provider (no API key)</h1>
</header>
<main>
  <section class="card">
    <h2>Run analysis</h2>
    <input type="text" id="url" placeholder="https://github.com/user/repo.git" value="https://github.com/omhome16/Clio.git">
    <button id="go">Analyze</button>
    <div id="log"></div>
  </section>
  <section class="card">
    <h2>Job history</h2>
    <table>
      <thead><tr><th>job</th><th>status</th><th>summary</th></tr></thead>
      <tbody id="jobs"></tbody>
    </table>
    <h2 style="margin-top:16px">Report</h2>
    <pre id="report">Pick a job from the table.</pre>
  </section>
</main>
<script>
const logEl = document.getElementById("log");
const go = document.getElementById("go");
const urlInput = document.getElementById("url");
function log(line, cls) {
  const div = document.createElement("div");
  div.textContent = line;
  if (cls) div.className = cls;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}
async function analyze() {
  go.disabled = true;
  logEl.textContent = "";
  let jobId = null;
  try {
    const resp = await fetch("/api/analyze?url=" + encodeURIComponent(urlInput.value), { method: "POST" });
    const body = await resp.json();
    if (!resp.ok) { log("error: " + body.error, "bad"); return; }
    jobId = body.job_id;
    const es = new EventSource("/api/stream?job_id=" + jobId);
    es.onmessage = (e) => {
      const ev = JSON.parse(e.data);
      const cls = ev.type.includes("failed") ? "bad"
        : ev.type.includes("done") ? "ok"
        : ev.type.includes("tool") ? "tool" : "";
      log("[" + (ev.ts || "").slice(11, 19) + "] " + ev.type + " " + JSON.stringify(ev.data), cls);
    };
    es.onerror = () => { es.close(); go.disabled = false; log("stream closed", "ok"); refreshJobs(); };
  } catch (err) {
    log("error: " + err, "bad");
    go.disabled = false;
  }
}
async function refreshJobs() {
  const resp = await fetch("/api/jobs");
  const body = await resp.json();
  const tbody = document.getElementById("jobs");
  tbody.textContent = "";
  for (const job of body.jobs) {
    const tr = document.createElement("tr");
    tr.dataset.job = job.job_id;
    tr.innerHTML = "<td>" + job.job_id + "</td>"
      + "<td><span class='status " + (job.status === "PERSISTED" ? "done" : "ready") + "'>"
      + job.status + "</span></td>"
      + "<td>" + (job.summary || "") + "</td>";
    tr.addEventListener("click", () => showJob(job.job_id));
    tbody.appendChild(tr);
  }
}
async function showJob(jobId) {
  const pre = document.getElementById("report");
  const resp = await fetch("/api/jobs/" + jobId);
  pre.textContent = JSON.stringify(await resp.json(), null, 2);
}
go.addEventListener("click", analyze);
refreshJobs();
</script>
</body>
</html>
"""


class Dashboard:
    def __init__(self, root: Path, port: int = 8790) -> None:
        self.root = Path(root)
        self.port = port
        self._lock = threading.Lock()
        self._jobs: dict[str, deque[Event]] = {}
        self._done: dict[str, bool] = {}
        self._httpd: ThreadingHTTPServer | None = None

    def start(self) -> str:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        self._httpd.dashboard = self
        thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        thread.start()
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    def register_job(self, job_id: str) -> None:
        with self._lock:
            self._jobs[job_id] = deque()
            self._done[job_id] = False

    def snapshot(self, job_id: str) -> tuple[list[Event], bool]:
        with self._lock:
            q = self._jobs.get(job_id)
            if q is None:
                return [], self._done.get(job_id, True)
            pending = list(q)
            q.clear()
            return pending, self._done.get(job_id, False)

    def _publish(self, job_id: str, event: Event) -> None:
        with self._lock:
            self._jobs[job_id].append(event)

    def run_job(self, url: str, job_id: str) -> None:
        limits = get_limits()
        sandbox = Sandbox(root=self.root, limits=limits)
        bus = EventBus()
        bus.subscribe(lambda e: self._publish(job_id, e))
        client = MockLLM(handler=_mock_handler(limits))
        orchestrator = Orchestrator(sandbox, client, bus=bus, limits=limits)
        try:
            asyncio.run(orchestrator.run(url, root=sandbox.root, job_id=job_id))
        except Exception as exc:
            self._publish(
                job_id, Event(type="job.failed", job_id=job_id, data={"error": str(exc)})
            )
        finally:
            with self._lock:
                self._done[job_id] = True


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence request logging
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html(INDEX_HTML)
            return
        if path == "/api/jobs":
            archive = ReportArchive(self.server.dashboard.root)
            self._send_json(200, {"jobs": archive.list_reports()})
            return
        if path.startswith("/api/jobs/"):
            rest = path[len("/api/jobs/"):]
            if rest.endswith("/graph"):
                self._job_graph(rest[: -len("/graph")])
            else:
                self._job_report(rest)
            return
        if path == "/api/stream":
            self._stream(urllib.parse.parse_qs(parsed.query).get("job_id", [""])[0])
            return
        self._json_error(404, "not found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/analyze":
            url = urllib.parse.parse_qs(parsed.query).get("url", [""])[0]
            if not url:
                self._json_error(400, "missing url parameter")
                return
            job_id = f"clio-{uuid4().hex[:8]}"
            self.server.dashboard.register_job(job_id)
            thread = threading.Thread(
                target=self.server.dashboard.run_job, args=(url, job_id), daemon=True
            )
            thread.start()
            self._send_json(200, {"job_id": job_id})
            return
        self._json_error(404, "not found")

    def _job_report(self, job_id: str) -> None:
        report = ReportArchive(self.server.dashboard.root).get_report(job_id)
        if report is None:
            self._json_error(404, f"no report for {job_id}")
            return
        self._send_json(200, report)

    def _job_graph(self, job_id: str) -> None:
        archive = ReportArchive(self.server.dashboard.root)
        graph = archive.get_graph(job_id)
        if graph is None:
            self._json_error(404, f"no graph for {job_id}")
            return
        clusters = [
            {"name": c.name, "modules": c.modules, "symbols": c.symbols,
             "external_edges": c.external_edges}
            for c in cluster_by_package(graph)
        ]
        self._send_json(200, {"stats": archive.graph_store(job_id).stats(), "clusters": clusters})

    def _stream(self, job_id: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        while True:
            pending, done = self.server.dashboard.snapshot(job_id)
            for event in pending:
                payload = json.dumps({"type": event.type, "data": event.data, "ts": event.ts})
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
            if done and not pending:
                self.wfile.write(b"event: done\ndata: {}\n\n")
                self.wfile.flush()
                break
            time.sleep(0.1)

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

from clio.clustering import cluster_by_package
from clio.config import Limits, get_limits, get_provider
from clio.events import Event, EventBus
from clio.job import load_job
from clio.llm import make_client
from clio.orchestrator import Orchestrator
from clio.reports import ReportArchive
from clio.sandbox import Sandbox

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clio — analysis dashboard</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' fill='%23F4F0E6'/%3E%3Cpath d='M8 1.5v13M1.5 8h13' stroke='%231E50C8' stroke-width='2.5'/%3E%3C/svg%3E">
<style>
:root {
  --paper:#F4F0E6; --paper-2:#ECE6D6; --ink:#26221B; --muted:#6F675A;
  --rule:#D8D1C0; --blue:#1E50C8; --blue-d:#173CA0; --ok:#336B42; --bad:#B23A2D;
}
* { box-sizing:border-box; }
html { color-scheme:light; }
body { margin:0; background:var(--paper); color:var(--ink);
       font:13px/1.6 ui-monospace,"Cascadia Mono",Consolas,monospace; }
::selection { background:var(--blue); color:var(--paper); }
a, button, input { font:inherit; color:inherit; }
:focus-visible { outline:2px solid var(--blue); outline-offset:2px; }

/* masthead — the one moment where the typeset voice appears */
header { border-bottom:1px solid var(--ink);
  background-image:linear-gradient(var(--rule) 1px, transparent 1px),
                   linear-gradient(90deg, var(--rule) 1px, transparent 1px);
  background-size:28px 28px; }
.mast { display:flex; align-items:baseline; justify-content:space-between;
        gap:16px; flex-wrap:wrap; max-width:1240px; margin:0 auto;
        padding:18px 32px 16px; }
.brand { display:flex; align-items:center; gap:14px; }
.crosshair { position:relative; width:16px; height:16px; flex:none; }
.crosshair::before, .crosshair::after { content:""; position:absolute;
  background:var(--blue); }
.crosshair::before { left:7px; top:0; width:2px; height:16px; }
.crosshair::after { left:0; top:7px; width:16px; height:2px; }
h1 { margin:0; font-family:Georgia,"Times New Roman",serif; font-size:22px;
     font-weight:600; letter-spacing:.22em; }
h1 .sub { font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;
          font-size:11px; font-weight:400; letter-spacing:.16em;
          color:var(--muted); text-transform:uppercase; margin-left:14px; }
.sys { display:flex; align-items:center; gap:8px; font-size:11px;
       letter-spacing:.12em; text-transform:uppercase; color:var(--muted); }
.lamp { width:8px; height:8px; background:var(--muted); }
.lamp.running { background:var(--blue); animation:blink 1s steps(2,start) infinite; }
.lamp.ok { background:var(--ok); }
.lamp.bad { background:var(--bad); }
@keyframes blink { to { visibility:hidden; } }

main { display:grid; grid-template-columns:340px minmax(0,1fr); gap:28px;
       max-width:1240px; margin:0 auto; padding:28px 32px 40px; }
@media (max-width:920px) { main { grid-template-columns:1fr; } }

.eyebrow { margin:0 0 12px; font-size:11px; font-weight:400;
           letter-spacing:.16em; text-transform:uppercase; color:var(--muted); }
.eyebrow::before { content:"\\258D\\00a0 "; color:var(--blue); }

.field-label { display:block; margin-bottom:6px; font-size:11px;
               letter-spacing:.14em; text-transform:uppercase;
               color:var(--muted); }
input[type=text] { width:100%; margin-bottom:14px; padding:9px 2px;
  background:transparent; border:0; border-bottom:1px solid var(--ink);
  outline:none; font-size:13px; }
input[type=text]:focus { border-bottom-color:var(--blue); }
input::placeholder { color:var(--muted); }
button { width:100%; padding:11px 16px; background:var(--blue); color:var(--paper);
  border:1px solid var(--blue); border-radius:0; cursor:pointer;
  font-size:12px; font-weight:600; letter-spacing:.14em;
  text-transform:uppercase; }
button:hover { background:var(--blue-d); border-color:var(--blue-d); }
button:active { transform:translateY(1px); }
button:disabled { opacity:.45; cursor:default; }
.state { margin-top:18px; border-top:1px solid var(--rule); }
.state-row { display:flex; justify-content:space-between; gap:12px;
  padding:8px 2px; border-bottom:1px solid var(--rule); font-size:12px; }
.state-row span:first-child { font-size:11px; letter-spacing:.1em;
  text-transform:uppercase; color:var(--muted); }
#state.running { color:var(--blue); }
#state.ok { color:var(--ok); }
#state.bad { color:var(--bad); }

.ledger-head { display:flex; align-items:baseline; justify-content:space-between; }
.live { display:none; font-size:10px; letter-spacing:.14em;
        text-transform:uppercase; color:var(--paper); background:var(--blue);
        padding:2px 6px; }
.live.on { display:inline-block; }
#log { max-height:300px; overflow:auto; border:1px solid var(--rule); }
.log-row { display:grid; grid-template-columns:62px 150px minmax(0,1fr);
           gap:10px; padding:5px 10px; border-bottom:1px solid var(--rule);
           font-size:12px; animation:in 160ms ease-out; }
.log-row:last-child { border-bottom:0; }
.log-row .t { color:var(--muted); }
.log-row .e { font-weight:600; }
.log-row .d { color:var(--muted); overflow:hidden; text-overflow:ellipsis;
              white-space:nowrap; }
.log-row.tool .e { color:var(--blue); }
.log-row.ok .e { color:var(--ok); }
.log-row.bad .e { color:var(--bad); }
.empty { padding:14px 10px; color:var(--muted); font-size:12px; }
@keyframes in { from { opacity:0; transform:translateY(2px); } }

table { width:100%; border-collapse:collapse; font-size:12px; }
th { text-align:left; padding:6px 10px; border-bottom:1px solid var(--ink);
     font-size:10px; font-weight:400; letter-spacing:.14em;
     text-transform:uppercase; color:var(--muted); }
td { padding:7px 10px; border-bottom:1px solid var(--rule); vertical-align:top; }
tr[data-job] { cursor:pointer; }
tr[data-job]:hover td { background:var(--paper-2); }
tr[data-job].selected td { background:var(--paper-2);
  box-shadow:inset 3px 0 0 var(--blue); }
.tag { display:inline-block; border:1px solid var(--rule); padding:1px 6px;
       font-size:10px; letter-spacing:.1em; }
.tag.ok { border-color:var(--ok); color:var(--ok); }
.tag.bad { border-color:var(--bad); color:var(--bad); }
pre { margin:0; max-height:300px; overflow:auto; background:var(--paper-2);
      border:1px solid var(--rule); padding:12px; font-size:11px;
      line-height:1.5; white-space:pre-wrap; word-break:break-word; }

footer { border-top:1px solid var(--rule); }
.foot { display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap;
        max-width:1240px; margin:0 auto; padding:12px 32px;
        font-size:10px; letter-spacing:.1em; text-transform:uppercase;
        color:var(--muted); }
@media (prefers-reduced-motion: reduce) {
  .lamp.running { animation:none; }
  .log-row { animation:none; }
}
</style>
</head>
<body class="clio-dashboard">
<header>
  <div class="mast">
    <div class="brand">
      <span class="crosshair" aria-hidden="true"></span>
      <h1>CLIO<span class="sub">analysis dashboard</span></h1>
    </div>
    <div class="sys"><span class="lamp" id="lamp" aria-hidden="true"></span>
      mock provider · offline · zero dependencies</div>
  </div>
</header>
<main>
  <section aria-labelledby="run-label">
    <h2 class="eyebrow" id="run-label">Run analysis</h2>
    <form id="run-form">
      <label class="field-label" for="url">Repository</label>
      <input type="text" id="url" spellcheck="false"
             placeholder="https://github.com/user/repo.git"
             value="https://github.com/omhome16/Clio.git">
      <button id="go" type="submit">Run analysis →</button>
    </form>
    <div class="state">
      <div class="state-row"><span>State</span><span id="state">idle</span></div>
      <div class="state-row"><span>Jobs</span><span id="job-count">0</span></div>
    </div>
  </section>
  <section aria-labelledby="ledger-label">
    <div class="ledger-head">
      <h2 class="eyebrow" id="ledger-label">Event ledger</h2>
      <span class="live" id="live-tag">Live</span>
    </div>
    <div id="log" role="log" aria-live="polite"></div>
    <h2 class="eyebrow" style="margin-top:24px">Job history</h2>
    <table>
      <thead><tr><th>Job</th><th>Status</th><th>Summary</th></tr></thead>
      <tbody id="jobs"></tbody>
    </table>
    <h2 class="eyebrow" style="margin-top:24px">Report</h2>
    <pre id="report">Select a job from the history table.</pre>
  </section>
</main>
<footer>
  <div class="foot">
    <span>Clio — local analysis harness · events stream over SSE</span>
    <span>mock provider (no API key)</span>
  </div>
</footer>
<script>
const logEl = document.getElementById("log");
const go = document.getElementById("go");
const urlInput = document.getElementById("url");
const stateEl = document.getElementById("state");
const lampEl = document.getElementById("lamp");
const countEl = document.getElementById("job-count");
const liveTag = document.getElementById("live-tag");

function setState(text, cls) {
  stateEl.textContent = text;
  stateEl.className = cls || "";
  lampEl.className = "lamp" + (cls ? " " + cls : "");
}

function freshLog() {
  if (!logEl.querySelector(".log-row")) logEl.textContent = "";
}

function logEvent(ev) {
  freshLog();
  const row = document.createElement("div");
  row.className = "log-row";
  const cls = ev.type.includes("failed") ? "bad"
    : ev.type === "job.persisted" || ev.type.includes("done") ? "ok"
    : ev.type.includes("tool") ? "tool" : "";
  if (cls) row.classList.add(cls);
  const t = document.createElement("span");
  t.className = "t";
  t.textContent = (ev.ts || "").slice(11, 19);
  const e = document.createElement("span");
  e.className = "e";
  e.textContent = ev.type;
  const d = document.createElement("span");
  d.className = "d";
  d.textContent = JSON.stringify(ev.data);
  row.append(t, e, d);
  logEl.appendChild(row);
  logEl.scrollTop = logEl.scrollHeight;
}

function logLine(text, cls) {
  freshLog();
  const row = document.createElement("div");
  row.className = "log-row" + (cls ? " " + cls : "");
  const t = document.createElement("span");
  t.className = "t";
  t.textContent = new Date().toISOString().slice(11, 19);
  const e = document.createElement("span");
  e.className = "e";
  e.textContent = text;
  row.append(t, e);
  logEl.appendChild(row);
  logEl.scrollTop = logEl.scrollHeight;
}

async function analyze() {
  go.disabled = true;
  logEl.textContent = "";
  liveTag.classList.add("on");
  let failed = false;
  try {
    const resp = await fetch("/api/analyze?url=" + encodeURIComponent(urlInput.value),
                             { method: "POST" });
    const body = await resp.json();
    if (!resp.ok) {
      logLine("error: " + body.error, "bad");
      setState("failed", "bad");
      go.disabled = false;
      liveTag.classList.remove("on");
      return;
    }
    const jobId = body.job_id;
    setState("running " + jobId, "running");
    const es = new EventSource("/api/stream?job_id=" + jobId);
    es.onmessage = (e) => {
      const ev = JSON.parse(e.data);
      if (!ev.type) return;
      if (ev.type === "job.failed") {
        failed = true;
        setState("failed", "bad");
      }
      logEvent(ev);
    };
    es.onerror = () => {
      es.close();
      go.disabled = false;
      liveTag.classList.remove("on");
      setState(failed ? "failed" : "persisted", failed ? "bad" : "ok");
      refreshJobs();
    };
  } catch (err) {
    logLine("error: " + err, "bad");
    setState("failed", "bad");
    go.disabled = false;
    liveTag.classList.remove("on");
  }
}

async function refreshJobs() {
  const resp = await fetch("/api/jobs");
  const body = await resp.json();
  const tbody = document.getElementById("jobs");
  tbody.textContent = "";
  if (!body.jobs.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.className = "empty";
    td.textContent = "No jobs yet — run an analysis.";
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  for (const job of body.jobs) {
    const tr = document.createElement("tr");
    tr.dataset.job = job.job_id;
    const id = document.createElement("td");
    id.textContent = job.job_id;
    const st = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = "tag" + (job.status === "PERSISTED" ? " ok"
      : job.status === "FAILED" ? " bad" : "");
    tag.textContent = job.status;
    st.appendChild(tag);
    const sm = document.createElement("td");
    sm.textContent = job.summary || "";
    tr.append(id, st, sm);
    tr.addEventListener("click", () => showJob(job.job_id, tr));
    tbody.appendChild(tr);
  }
  countEl.textContent = String(body.jobs.length);
}

async function showJob(jobId, tr) {
  const pre = document.getElementById("report");
  document.querySelectorAll("tr.selected").forEach((r) => r.classList.remove("selected"));
  if (tr) tr.classList.add("selected");
  const resp = await fetch("/api/jobs/" + jobId);
  pre.textContent = resp.ok
    ? JSON.stringify(await resp.json(), null, 2)
    : "No report for " + jobId + ".";
}

document.getElementById("run-form").addEventListener("submit", (e) => {
  e.preventDefault();
  analyze();
});
logLine("No activity yet — run an analysis to fill the ledger.", "");
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
        client = make_client(get_provider(), limits)
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
            jobs = []
            for report in archive.list_reports():
                row = dict(report)
                job = load_job(row["job_id"], self.server.dashboard.root)
                row["status"] = job.status if job is not None else "PERSISTED"
                jobs.append(row)
            self._send_json(200, {"jobs": jobs})
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

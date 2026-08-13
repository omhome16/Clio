# src/clio/web.py
"""Zero-dependency local dashboard: live event stream + archive API."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from clio.ask import AskSession
from clio.clustering import cluster_by_package
from clio.config import Limits, get_limits, get_provider
from clio.events import EVENT_ASK_FINAL, EVENT_ASK_TOOL, Event, EventBus
from clio.impact import impact_of_module
from clio.job import jobs_dir, load_job
from clio.llm import make_client
from clio.logging import setup_logging
from clio.map import layout_graph
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
:root { color-scheme: light; }
html[data-theme="light"] { --paper:#F6F5F1; --paper-2:#FFFFFF; --ink:#20242C;
  --muted:#70767F; --rule:#E3E1DA; --blue:#2F6FED; --blue-d:#1F5CD8;
  --blue-soft:rgba(47,111,237,.09); --ok:#2E9E5B; --ok-soft:rgba(46,158,91,.10);
  --bad:#D64545; --bad-soft:rgba(214,69,69,.10);
  --shadow:0 1px 2px rgba(20,24,32,.04), 0 6px 20px rgba(20,24,32,.05); }
html[data-theme="dark"] { color-scheme:dark; --paper:#0F1217; --paper-2:#171B22;
  --ink:#E8EBF1; --muted:#98A1B0; --rule:#2A303A; --blue:#6FA8FF; --blue-d:#8DBBFF;
  --blue-soft:rgba(111,168,255,.12); --ok:#45C47C; --ok-soft:rgba(69,196,124,.12);
  --bad:#F0746E; --bad-soft:rgba(240,116,110,.12);
  --shadow:0 1px 2px rgba(0,0,0,.28), 0 8px 24px rgba(0,0,0,.32); }
* { box-sizing:border-box; }
html { height:100%; }
body { margin:0; min-height:100%; background:var(--paper); color:var(--ink);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility; }
::selection { background:var(--blue); color:#fff; }
a, button, input { font:inherit; color:inherit; }
:focus-visible { outline:2px solid var(--blue); outline-offset:2px; }

header { position:sticky; top:0; z-index:30; border-bottom:1px solid var(--rule);
  background:color-mix(in srgb, var(--paper) 88%, transparent);
  backdrop-filter:blur(10px); }
.mast { display:flex; align-items:center; justify-content:space-between; gap:16px;
  flex-wrap:wrap; max-width:1400px; margin:0 auto; padding:14px 32px; }
.brand { display:flex; align-items:center; gap:12px; }
.crosshair { width:34px; height:34px; border-radius:10px; position:relative;
  background:var(--blue-soft);
  border:1px solid color-mix(in srgb, var(--blue) 35%, transparent); }
.crosshair::before, .crosshair::after { content:""; position:absolute;
  background:var(--blue); }
.crosshair::before { left:16px; top:8px; width:2px; height:18px; border-radius:1px; }
.crosshair::after { left:8px; top:16px; width:18px; height:2px; border-radius:1px; }
h1 { margin:0; font-size:20px; font-weight:750; letter-spacing:.22em; line-height:1.2; }
h1 .sub { font-family:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace;
  font-size:10px; font-weight:500; letter-spacing:.18em; color:var(--muted);
  text-transform:uppercase; margin-left:12px; }
.sys { display:flex; align-items:center; gap:10px; }
.provider-pill { display:inline-flex; align-items:center; gap:7px;
  padding:5px 12px; border:1px solid var(--rule); border-radius:999px;
  background:var(--paper-2);
  font-family:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace;
  font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); }
.lamp { width:8px; height:8px; border-radius:50%; background:var(--muted); flex:none; }
.lamp.running { background:var(--blue); animation:pulse 1.2s infinite; }
.lamp.ok { background:var(--ok); }
.lamp.bad { background:var(--bad); }
@keyframes pulse {
  0% { box-shadow:0 0 0 0 color-mix(in srgb, var(--blue) 45%, transparent); }
  70% { box-shadow:0 0 0 7px transparent; }
  100% { box-shadow:0 0 0 0 transparent; } }

/* ---- bento grid ---- */
.bento { display:grid; grid-template-columns:repeat(12, 1fr);
  grid-auto-rows:minmax(0,auto); gap:20px;
  max-width:1400px; margin:0 auto; padding:24px 32px 48px; align-items:start; }
.cell { background:var(--paper-2); border:1px solid var(--rule);
  border-radius:16px; box-shadow:var(--shadow); padding:18px 20px; min-width:0; }
.hero { grid-column:1/5; }
.status { grid-column:1/5; }
.history { grid-column:1/5; grid-row:3/9; }
.ask { grid-column:5/13; grid-row:1/4; }
.ledger { grid-column:5/13; grid-row:4/5; }
.map { grid-column:5/10; grid-row:5/9; }
.tree { grid-column:10/13; grid-row:5/9; }
.report { grid-column:5/13; grid-row:9/10; }
@media (max-width:1100px) {
  .bento { grid-template-columns:1fr; }
  .hero, .status, .history, .ask, .ledger, .map, .tree, .report {
    grid-column:1/2; grid-row:auto; }
}
@media (max-width:920px) {
  .ask { position:fixed; top:0; right:0; bottom:0; width:400px; max-width:94vw;
    transform:translateX(103%); transition:transform .2s cubic-bezier(.4,0,.2,1);
    display:flex; flex-direction:column; z-index:40; }
  .ask.open { transform:none; box-shadow:-16px 0 40px rgba(0,0,0,.22); }
  .ask .ask-log { flex:1; }
}

.eyebrow { margin:0 0 14px; font-size:11px; font-weight:650;
  letter-spacing:.14em; text-transform:uppercase; color:var(--muted);
  display:flex; align-items:center; gap:8px; }
.eyebrow::before { content:""; width:6px; height:6px; border-radius:2px;
  background:var(--blue); }
.eyebrow .spacer { flex:1; }
.eyebrow .tag-sub { font-weight:500; letter-spacing:.1em; }

.field-label { display:block; margin-bottom:7px; font-size:11px; font-weight:600;
  letter-spacing:.1em; text-transform:uppercase; color:var(--muted); }
input[type=text] { width:100%; margin-bottom:12px; padding:11px 14px;
  background:var(--paper); border:1px solid var(--rule); border-radius:10px;
  outline:none; font-size:13px; transition:border-color .15s, box-shadow .15s; }
input[type=text]:focus { border-color:var(--blue); box-shadow:0 0 0 3px var(--blue-soft); }
input::placeholder { color:var(--muted); }

button { width:100%; padding:11px 16px; border-radius:10px; border:1px solid var(--blue);
  background:var(--blue); color:#fff; cursor:pointer; font-size:12px; font-weight:650;
  letter-spacing:.08em; text-transform:uppercase;
  transition:background .15s, transform .1s, box-shadow .15s; }
button:hover { background:var(--blue-d); border-color:var(--blue-d); }
button:active { transform:translateY(1px); }
button:disabled { opacity:.5; cursor:default; }
.theme-btn { width:auto; padding:7px 14px; border-radius:999px; background:transparent;
  color:var(--muted); border:1px solid var(--rule); }
.theme-btn:hover { color:var(--ink); border-color:var(--ink); background:var(--paper); }
.danger-btn { width:auto; padding:7px 14px; border-radius:999px; background:transparent;
  color:var(--bad); border:1px solid color-mix(in srgb, var(--bad) 45%, transparent); }
.danger-btn:hover { background:var(--bad-soft); border-color:var(--bad); }

.hint { margin:10px 0 0; font-size:12px; color:var(--muted); }

.state { border-top:0; }
.state-row { display:flex; justify-content:space-between; align-items:center; gap:12px;
  padding:9px 2px; border-bottom:1px solid var(--rule); font-size:13px; }
.state-row:last-child { border-bottom:0; }
.state-row span:first-child { font-size:11px; font-weight:600; letter-spacing:.1em;
  text-transform:uppercase; color:var(--muted); }
#state { font-family:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace;
  font-size:12px; }
#state.running { color:var(--blue); }
#state.ok { color:var(--ok); }
#state.bad { color:var(--bad); }

.ledger-head { display:flex; align-items:center; justify-content:space-between;
  gap:12px; margin-bottom:12px; }
.ledger-head .eyebrow { margin:0; }
.live { display:none; align-items:center; gap:6px; font-size:10px; font-weight:700;
  letter-spacing:.12em; text-transform:uppercase; color:var(--ok);
  background:var(--ok-soft); border:1px solid color-mix(in srgb, var(--ok) 35%, transparent);
  padding:3px 10px; border-radius:999px; }
.live.on { display:inline-flex; }
.live::before { content:""; width:6px; height:6px; border-radius:50%;
  background:var(--ok); animation:blink 1.4s infinite; }
@keyframes blink { 0%,100% { opacity:1; } 50% { opacity:.25; } }

#log { max-height:300px; overflow:auto; border:1px solid var(--rule);
  border-radius:10px; background:var(--paper); }
.log-row { display:grid; grid-template-columns:60px 150px minmax(0,1fr); gap:10px;
  padding:6px 12px; border-bottom:1px solid var(--rule);
  font-family:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace;
  font-size:11.5px; animation:in 160ms ease-out; }
.log-row:last-child { border-bottom:0; }
.log-row .t { color:var(--muted); }
.log-row .e { font-weight:650; }
.log-row .d { color:var(--muted); overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; }
.log-row.tool .e { color:var(--blue); }
.log-row.ok .e { color:var(--ok); }
.log-row.bad .e { color:var(--bad); }
@keyframes in { from { opacity:0; transform:translateY(3px); } }
.empty { padding:12px 10px; color:var(--muted); font-size:12px; }

table { width:100%; border-collapse:collapse; font-size:13px; }
th { text-align:left; padding:7px 8px; border-bottom:1px solid var(--rule);
  font-family:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace;
  font-size:10px; font-weight:600; letter-spacing:.14em; text-transform:uppercase;
  color:var(--muted); }
td { padding:8px 8px; border-bottom:1px solid var(--rule); vertical-align:top; }
tr:last-child td { border-bottom:0; }
tr[data-job] { cursor:pointer; }
tr[data-job]:hover td { background:var(--blue-soft); }
tr[data-job].selected td { background:var(--blue-soft);
  box-shadow:inset 3px 0 0 var(--blue); }
.job-repo { max-width:150px; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; color:var(--muted); font-size:11.5px; }
.tag { display:inline-block; border:1px solid var(--rule); padding:2px 8px;
  border-radius:999px; font-family:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace;
  font-size:10px; font-weight:600; letter-spacing:.1em; }
.tag.ok { border-color:color-mix(in srgb, var(--ok) 50%, transparent);
  color:var(--ok); background:var(--ok-soft); }
.tag.bad { border-color:color-mix(in srgb, var(--bad) 50%, transparent);
  color:var(--bad); background:var(--bad-soft); }
.del { width:26px; height:26px; padding:0; border-radius:8px; border:1px solid transparent;
  background:transparent; color:var(--muted); font-size:14px; line-height:1;
  flex:none; }
.del:hover { color:var(--bad); border-color:color-mix(in srgb, var(--bad) 45%, transparent);
  background:var(--bad-soft); }

pre { margin:0; max-height:300px; overflow:auto; background:var(--paper);
  border:1px solid var(--rule); border-radius:10px; padding:14px; font-size:12px;
  font-family:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace;
  line-height:1.6; white-space:pre-wrap; word-break:break-word; }

#map-detail { border:1px solid var(--rule); background:var(--paper);
  border-radius:10px; padding:12px 14px; font-size:12.5px; margin-bottom:12px;
  color:var(--ink); }
#map-detail strong { font-weight:650; }
#map-detail .impact-list { margin:8px 0 0; padding-left:20px; }
#map-detail ol.impact-list li { padding:2px 0; }
#map-detail button { width:auto; margin-top:12px; padding:7px 16px; font-size:11px; }
.map-wrap { overflow:auto; max-height:520px; border:1px solid var(--rule);
  border-radius:10px; background:var(--paper); }
#map { display:block; width:100%; height:auto; background:var(--paper); }
#map .edge { stroke:var(--rule); stroke-width:1.6; opacity:.65; }
#map .edge.e-call, #map .edge.e-both { stroke:var(--blue); stroke-dasharray:4 3; opacity:.75; }
#map .edge.map-edge-on { stroke:var(--ink); opacity:1; }
#map .edge.impact { stroke:var(--bad); opacity:1; }
#map .node { cursor:pointer; }
#map .node rect { fill:var(--paper-2); stroke:var(--rule); stroke-width:1; rx:9;
  transition:fill .12s, stroke .12s; }
#map .node text { fill:var(--muted); font-size:10.5px;
  font-family:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace; }
#map .node.map-hover rect { stroke:var(--blue); stroke-width:2; fill:var(--blue-soft); }
#map .node.map-hover text, #map .node.map-neighbor text { fill:var(--ink); }
#map .node.map-neighbor rect { stroke:var(--blue); }
#map .node.impact rect { fill:var(--bad); fill-opacity:.16; stroke:var(--bad);
  animation:impactPulse 700ms ease-out; }
@keyframes impactPulse {
  0% { fill:var(--bad); fill-opacity:.5; stroke-width:3; }
  100% { fill:var(--bad); fill-opacity:.16; }
}
#map .cluster-label { fill:var(--muted); font-size:10px; font-weight:650;
  letter-spacing:.14em; text-transform:uppercase;
  font-family:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace; }

/* ---- folder tree ---- */
#tree { max-height:560px; overflow:auto; border:1px solid var(--rule);
  border-radius:10px; background:var(--paper); padding:8px 6px; }
.tree-root, .tree-root ul { list-style:none; margin:0; padding:0; }
.tree-root ul { padding-left:16px; }
.tree-item { font-size:12px; border-radius:6px; }
.tree-item .tree-row { display:flex; align-items:center; gap:6px; padding:3px 6px;
  border-radius:6px; cursor:pointer; }
.tree-item .tree-row:hover { background:var(--blue-soft); }
.tree-toggle { width:16px; height:16px; padding:0; border:none; background:transparent;
  color:var(--muted); font-size:10px; line-height:1; flex:none; display:flex;
  align-items:center; justify-content:center; }
.tree-toggle:hover { color:var(--ink); background:transparent; }
.tree-icon { flex:none; width:14px; text-align:center; color:var(--muted); font-size:11px; }
.tree-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  font-family:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace; font-size:11.5px; }
.tree-name.dir { font-weight:600; }
.tree-item .tree-children { display:none; }
.tree-item.open > .tree-children { display:block; }
.tree-item.open > .tree-row .tree-toggle { transform:rotate(90deg); }

footer { border-top:1px solid var(--rule); }
.foot { display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap;
  max-width:1400px; margin:0 auto; padding:14px 32px; font-size:11px;
  font-family:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace;
  letter-spacing:.06em; text-transform:uppercase; color:var(--muted); }

/* ---- ask (chat) cell ---- */
.ask { display:flex; flex-direction:column; }
.ask-head { display:flex; justify-content:space-between; align-items:center; gap:10px; }
.ask-head .eyebrow { margin:0; }
.ask-job { font-family:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace;
  font-size:10.5px; color:var(--muted); padding:3px 10px; border:1px solid var(--rule);
  border-radius:999px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  max-width:220px; }
.ask-log { flex:1; overflow:auto; border:1px solid var(--rule); border-radius:12px;
  background:var(--paper); margin:12px 0; padding:12px; display:flex;
  flex-direction:column; gap:8px; min-height:260px; }
.bubble { padding:9px 12px; border:1px solid var(--rule); border-radius:12px;
  background:var(--paper-2); font-size:12.5px; white-space:pre-wrap;
  align-self:flex-start; max-width:85%; }
.bubble.user { background:var(--blue); border-color:var(--blue); color:#fff;
  align-self:flex-end; border-bottom-right-radius:4px; }
.bubble.bad { border-color:color-mix(in srgb, var(--bad) 55%, transparent);
  color:var(--bad); background:var(--bad-soft); }
.tool-line { font-family:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace;
  font-size:10.5px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted);
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.tool-line.ok::before { content:"\2699\00a0"; color:var(--ok); }
.tool-line.bad::before { content:"\2715\00a0"; color:var(--bad); }
#ask-q { margin-bottom:10px; }
#ask-send { width:auto; padding:11px 26px; }

@media (prefers-reduced-motion: reduce) {
  .lamp.running { animation:none; }
  .log-row { animation:none; }
  .ask { transition:none; }
  .live::before { animation:none; }
  #map .node.impact rect { animation:none; }
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
    <div class="sys">
      <span class="provider-pill"><span class="lamp" id="lamp" aria-hidden="true"></span>
        <span id="provider">__PROVIDER__</span></span>
      <button id="theme-btn" type="button" class="theme-btn" aria-label="Toggle dark mode">Theme</button>
      <button id="ask-open" type="button" class="theme-btn">Ask &#9656;</button>
    </div>
  </div>
</header>
<main class="bento">
  <section class="cell hero" aria-labelledby="run-label">
    <h2 class="eyebrow" id="run-label">New analysis</h2>
    <form id="run-form">
      <label class="field-label" for="url">Repository URL</label>
      <input type="text" id="url" spellcheck="false"
             placeholder="https://github.com/user/repo.git"
             value="https://github.com/omhome16/Clio.git">
      <button id="go" type="submit">Run analysis &#8594;</button>
    </form>
    <p class="hint">Paste any git URL, or reopen a past thread from the history panel.</p>
  </section>
  <section class="cell status" aria-label="System status">
    <h2 class="eyebrow">System</h2>
    <div class="state">
      <div class="state-row"><span>State</span><span id="state">idle</span></div>
      <div class="state-row"><span>Jobs</span><span id="job-count">0</span></div>
    </div>
  </section>
  <section class="cell history" aria-label="Thread history">
    <h2 class="eyebrow">Threads <span class="spacer"></span>
      <button id="clear-jobs" type="button" class="danger-btn">Clear all</button></h2>
    <table>
      <thead><tr><th>Repo</th><th>Status</th><th>Summary</th><th></th></tr></thead>
      <tbody id="jobs"></tbody>
    </table>
  </section>
  <aside class="cell ask" id="ask-panel" aria-label="Ask about this analysis">
    <div class="ask-head">
      <h2 class="eyebrow">Ask <span class="spacer"></span>
        <span class="ask-job" id="ask-job">no thread selected</span></h2>
      <button id="ask-close" type="button" class="theme-btn" aria-label="Close ask panel">&#10005;</button>
    </div>
    <div id="ask-log" class="ask-log" role="log" aria-live="polite">
      <div class="empty">Select a thread in the history panel, then ask about it.</div>
    </div>
    <form id="ask-form">
      <label class="field-label" for="ask-q">Question</label>
      <input type="text" id="ask-q" spellcheck="false" placeholder="e.g. what calls app.service::greet?">
      <button id="ask-send" type="submit">Ask &#8594;</button>
    </form>
  </aside>
  <section class="cell ledger" aria-labelledby="ledger-label">
    <div class="ledger-head">
      <h2 class="eyebrow" id="ledger-label">Event ledger</h2>
      <span class="live" id="live-tag">Live</span>
    </div>
    <div id="log" role="log" aria-live="polite"></div>
  </section>
  <section class="cell map">
    <h2 class="eyebrow">Module map</h2>
    <div id="map-detail" class="empty">Select a thread, then click a module to inspect it.</div>
    <div class="map-wrap"><svg id="map" role="img" aria-label="Module architecture map"></svg></div>
  </section>
  <section class="cell tree">
    <h2 class="eyebrow">Folder tree</h2>
    <div id="tree" role="tree">
      <div class="empty">Select a thread to see its repository tree.</div>
    </div>
  </section>
  <section class="cell report">
    <h2 class="eyebrow">Report</h2>
    <pre id="report">Select a thread from the history panel.</pre>
  </section>
</main>
<footer>
  <div class="foot">
    <span>Clio — local analysis harness · events stream over SSE</span>
    <span>provider: __PROVIDER__</span>
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

(function initTheme() {
  const saved = localStorage.getItem("clio-theme");
  const root = document.documentElement;
  root.dataset.theme = saved ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.getElementById("theme-btn").addEventListener("click", () => {
    root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
    localStorage.setItem("clio-theme", root.dataset.theme);
  });
})();

const askPanel = document.getElementById("ask-panel");
const askLog = document.getElementById("ask-log");
const askForm = document.getElementById("ask-form");
const askQ = document.getElementById("ask-q");
const askSend = document.getElementById("ask-send");
const askJobEl = document.getElementById("ask-job");
let activeJob = null;

document.getElementById("ask-open").addEventListener("click", () => {
  askPanel.classList.add("open");
  if (window.innerWidth > 920) askPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  askQ.focus();
});
document.getElementById("ask-close").addEventListener("click", () => {
  askPanel.classList.remove("open");
});

function addBubble(text, cls) {
  if (askLog.querySelector(".empty")) askLog.textContent = "";
  const b = document.createElement("div");
  b.className = "bubble" + (cls ? " " + cls : "");
  b.textContent = text;
  askLog.appendChild(b);
  askLog.scrollTop = askLog.scrollHeight;
}

function addToolLine(name, args, ok) {
  const t = document.createElement("div");
  t.className = "tool-line " + (ok ? "ok" : "bad");
  t.textContent = name + "(" + JSON.stringify(args).slice(0, 90) + ")";
  askLog.appendChild(t);
  askLog.scrollTop = askLog.scrollHeight;
}

askForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const q = askQ.value.trim();
  if (!q || askSend.disabled || !activeJob) return;
  askQ.value = "";
  askSend.disabled = true;
  addBubble(q, "user");
  const es = new EventSource("/api/ask?job_id=" + encodeURIComponent(activeJob) +
                             "&q=" + encodeURIComponent(q));
  es.onmessage = (ev) => {
    const p = JSON.parse(ev.data);
    if (p.type === "ask.tool") addToolLine(p.data.tool, p.data.args, p.data.ok);
    if (p.type === "ask.final") addBubble(p.data.answer, p.data.ok ? "" : "bad");
  };
  es.addEventListener("done", () => { es.close(); askSend.disabled = false; });
  es.onerror = () => { es.close(); askSend.disabled = false; };
});

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

function clearViews() {
  document.getElementById("report").textContent = "Select a thread from the history panel.";
  document.getElementById("map").textContent = "";
  document.getElementById("map-detail").className = "empty";
  document.getElementById("map-detail").textContent = "Select a thread, then click a module to inspect it.";
  document.getElementById("tree").textContent = "";
  document.getElementById("tree").appendChild(Object.assign(document.createElement("div"), {
    className: "empty", textContent: "Select a thread to see its repository tree.",
  }));
  askLog.textContent = "";
  askLog.appendChild(Object.assign(document.createElement("div"), {
    className: "empty", textContent: "Select a thread in the history panel, then ask about it.",
  }));
  askJobEl.textContent = "no thread selected";
  activeJob = null;
  document.querySelectorAll("tr.selected").forEach((r) => r.classList.remove("selected"));
}

async function deleteJob(jobId) {
  const resp = await fetch("/api/jobs/" + encodeURIComponent(jobId), { method: "DELETE" });
  if (!resp.ok) {
    logLine("delete failed for " + jobId + ": " + (await resp.json()).error, "bad");
    return;
  }
  logLine("deleted thread " + jobId, "ok");
  if (activeJob === jobId) clearViews();
  refreshJobs();
}

async function clearJobs() {
  const resp = await fetch("/api/jobs", { method: "DELETE" });
  if (!resp.ok) {
    logLine("clear failed: " + (await resp.json()).error, "bad");
    return;
  }
  logLine("cleared " + (await resp.json()).deleted + " threads", "ok");
  clearViews();
  refreshJobs();
}

document.getElementById("clear-jobs").addEventListener("click", () => {
  if (!confirm("Delete ALL threads and their reports?")) return;
  clearJobs();
});

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
    td.colSpan = 4;
    td.className = "empty";
    td.textContent = "No threads yet — run an analysis.";
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  for (const job of body.jobs) {
    const tr = document.createElement("tr");
    tr.dataset.job = job.job_id;
    const repo = document.createElement("td");
    const repoLabel = document.createElement("div");
    repoLabel.className = "job-repo";
    repoLabel.textContent = job.url || job.job_id;
    repo.appendChild(repoLabel);
    const st = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = "tag" + (job.status === "PERSISTED" ? " ok"
      : job.status === "FAILED" ? " bad" : "");
    tag.textContent = job.status;
    st.appendChild(tag);
    const sm = document.createElement("td");
    sm.textContent = job.summary || "";
    const del = document.createElement("td");
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "del";
    delBtn.title = "Delete thread";
    delBtn.setAttribute("aria-label", "Delete thread " + job.job_id);
    delBtn.textContent = "\u00d7";
    delBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (!confirm("Delete thread " + job.job_id + "?")) return;
      deleteJob(job.job_id);
    });
    del.appendChild(delBtn);
    tr.append(repo, st, sm, del);
    tr.addEventListener("click", () => showJob(job.job_id, tr));
    tbody.appendChild(tr);
  }
  countEl.textContent = String(body.jobs.length);
}

async function showJob(jobId, tr) {
  activeJob = jobId;
  askJobEl.textContent = jobId;
  const pre = document.getElementById("report");
  document.querySelectorAll("tr.selected").forEach((r) => r.classList.remove("selected"));
  if (tr) tr.classList.add("selected");
  const resp = await fetch("/api/jobs/" + jobId);
  pre.textContent = resp.ok
    ? JSON.stringify(await resp.json(), null, 2)
    : "No report for " + jobId + ".";
  loadMap(jobId);
  loadTree(jobId);
}

const NS = "http://www.w3.org/2000/svg";
let mapJob = null;

async function loadMap(jobId) {
  mapJob = jobId;
  const svg = document.getElementById("map");
  const detail = document.getElementById("map-detail");
  detail.className = "empty";
  detail.textContent = "Loading map\u2026";
  const resp = await fetch("/api/jobs/" + jobId + "/graph/map");
  if (!resp.ok || mapJob !== jobId) {
    svg.textContent = "";
    if (resp.ok) detail.textContent = "";
    return;
  }
  const payload = await resp.json();
  renderMap(svg, payload);
  detail.className = "empty";
  let summary = payload.nodes.length + " modules, " + payload.edges.length +
    " edges \u2014 hover to trace, click a module for detail.";
  const langs = await fetch("/api/jobs/" + jobId + "/graph");
  if (langs.ok && mapJob === jobId) {
    const body = await langs.json();
    const parts = Object.entries(body.languages || {})
      .map(([lang, n]) => lang + " \u00d7" + n);
    if (parts.length) summary += " \u00b7 " + parts.join(", ");
  }
  detail.textContent = summary;
}

function renderMap(svg, payload) {
  const nodes = payload.nodes, edges = payload.edges;
  const byId = {};
  nodes.forEach((n) => byId[n.id] = n);
  const neighbors = {};
  edges.forEach((e) => {
    (neighbors[e.from] = neighbors[e.from] || []).push(e.to);
    (neighbors[e.to] = neighbors[e.to] || []).push(e.from);
  });
  const byCluster = {};
  nodes.forEach((n) => (byCluster[n.cluster] = byCluster[n.cluster] || []).push(n));
  const w = Math.max(0, ...nodes.map((n) => n.x)) + 280;
  const h = Math.max(0, ...nodes.map((n) => n.y)) + 160;
  svg.setAttribute("viewBox", "0 0 " + w + " " + h);
  svg.textContent = "";
  const defs = document.createElementNS(NS, "defs");
  const marker = document.createElementNS(NS, "marker");
  marker.setAttribute("id", "arrow");
  marker.setAttribute("viewBox", "0 0 8 8");
  marker.setAttribute("refX", "7");
  marker.setAttribute("refY", "4");
  marker.setAttribute("markerWidth", "6");
  marker.setAttribute("markerHeight", "6");
  marker.setAttribute("orient", "auto-start-reverse");
  const poly = document.createElementNS(NS, "path");
  poly.setAttribute("d", "M0,0 L8,4 L0,8 z");
  poly.setAttribute("fill", "var(--rule)");
  marker.appendChild(poly);
  defs.appendChild(marker);
  svg.appendChild(defs);
  edges.forEach((e) => {
    const a = byId[e.from], b = byId[e.to];
    if (!a || !b) return;
    const l = document.createElementNS(NS, "line");
    l.setAttribute("x1", a.x + 130);
    l.setAttribute("y1", a.y + 20);
    l.setAttribute("x2", b.x + 130);
    l.setAttribute("y2", b.y + 20);
    l.classList.add("edge", "e-" + e.kind);
    l.dataset.from = e.from;
    l.dataset.to = e.to;
    svg.appendChild(l);
  });
  nodes.forEach((n) => {
    const g = document.createElementNS(NS, "g");
    g.classList.add("node");
    g.dataset.module = n.id;
    const hh = 36 + Math.min(6, n.symbols) * 6;
    const r = document.createElementNS(NS, "rect");
    r.setAttribute("x", n.x + 8);
    r.setAttribute("y", n.y - hh / 2 + 20);
    r.setAttribute("width", 244);
    r.setAttribute("height", hh);
    const t = document.createElementNS(NS, "text");
    t.setAttribute("x", n.x + 130);
    t.setAttribute("y", n.y + 20);
    t.setAttribute("text-anchor", "middle");
    t.setAttribute("dominant-baseline", "middle");
    t.textContent = n.id;
    g.append(r, t);
    g.addEventListener("mouseenter", () => highlightMap(n.id, neighbors, svg, true));
    g.addEventListener("mouseleave", () => highlightMap(n.id, neighbors, svg, false));
    g.addEventListener("click", () => showModuleDetail(n, neighbors));
    svg.appendChild(g);
  });
  Object.keys(byCluster).sort().forEach((cluster) => {
    const ys = byCluster[cluster].map((n) => n.y);
    const x = byCluster[cluster][0].x;
    const label = document.createElementNS(NS, "text");
    label.setAttribute("x", x + 130);
    label.setAttribute("y", Math.min.apply(null, ys) - 30);
    label.setAttribute("text-anchor", "middle");
    label.classList.add("cluster-label");
    label.textContent = cluster;
    svg.appendChild(label);
  });
}

function highlightMap(id, neighbors, svg, on) {
  svg.querySelectorAll(".node").forEach((g) => {
    g.classList.toggle("map-hover", on && g.dataset.module === id);
    g.classList.toggle("map-neighbor",
      on && (neighbors[id] || []).includes(g.dataset.module));
  });
  svg.querySelectorAll(".edge").forEach((l) => {
    const nbs = neighbors[id] || [];
    const touch = l.dataset.from === id || l.dataset.to === id;
    const between = nbs.includes(l.dataset.from) && nbs.includes(l.dataset.to);
    l.classList.toggle("map-edge-on", on && (touch || between));
  });
}

function showModuleDetail(node, neighbors) {
  const detail = document.getElementById("map-detail");
  detail.className = "";
  detail.textContent = "";
  const head = document.createElement("div");
  head.innerHTML = "<strong>" + node.id + "</strong> &middot; cluster " + node.cluster +
    " &middot; " + node.symbols + " symbols";
  detail.appendChild(head);
  const list = document.createElement("ul");
  list.className = "impact-list";
  (neighbors[node.id] || []).sort().forEach((nb) => {
    const li = document.createElement("li");
    li.textContent = nb;
    list.appendChild(li);
  });
  const btn = document.createElement("button");
  btn.textContent = "Impact";
  btn.addEventListener("click", () => showImpact(node.id));
  detail.append(head, list, btn);
}

async function showImpact(module) {
  const svg = document.getElementById("map");
  const detail = document.getElementById("map-detail");
  const resp = await fetch("/api/jobs/" + mapJob + "/graph/map?impact=" +
                           encodeURIComponent(module));
  if (!resp.ok) return;
  const impact = (await resp.json()).impact;
  svg.querySelectorAll(".node.impact").forEach((g) => g.classList.remove("impact"));
  impact.affected_modules.forEach((m, i) => {
    const g = svg.querySelector('.node[data-module="' + m + '"]');
    if (!g) return;
    g.classList.add("impact");
    g.querySelector("rect").style.animationDelay = (i * 140) + "ms";
  });
  svg.querySelectorAll(".edge").forEach((l) => {
    l.classList.toggle("impact",
      impact.affected_modules.includes(l.dataset.from) &&
      impact.affected_modules.includes(l.dataset.to));
  });
  detail.className = "";
  detail.textContent = "";
  const head = document.createElement("div");
  head.innerHTML = "<strong>Impact of " + module + "</strong> &middot; " + impact.verdict;
  detail.appendChild(head);
  const list = document.createElement("ol");
  list.className = "impact-list";
  impact.affected_modules.forEach((m) => {
    const li = document.createElement("li");
    li.textContent = m;
    list.appendChild(li);
  });
  detail.append(head, list);
}

async function loadTree(jobId) {
  const box = document.getElementById("tree");
  box.textContent = "";
  const resp = await fetch("/api/jobs/" + encodeURIComponent(jobId) + "/tree");
  if (!resp.ok) {
    box.appendChild(Object.assign(document.createElement("div"), {
      className: "empty", textContent: "No workspace for this thread.",
    }));
    return;
  }
  const payload = await resp.json();
  if (!payload.count) {
    box.appendChild(Object.assign(document.createElement("div"), {
      className: "empty", textContent: "This thread has no files.",
    }));
    return;
  }
  const root = { name: "", dir: true, children: {} };
  payload.files.forEach((path) => {
    const parts = path.split("/");
    let node = root;
    parts.forEach((part, i) => {
      const isFile = i === parts.length - 1;
      if (!node.children[part]) {
        node.children[part] = { name: part, dir: !isFile, children: {} };
      }
      node = node.children[part];
    });
  });
  function render(node, container, depth) {
    Object.keys(node.children).sort().forEach((name) => {
      const child = node.children[name];
      const li = document.createElement("li");
      li.className = "tree-item";
      const row = document.createElement("div");
      row.className = "tree-row";
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "tree-toggle";
      toggle.setAttribute("aria-label", "Toggle " + name);
      toggle.textContent = "\u25b8";
      const icon = document.createElement("span");
      icon.className = "tree-icon";
      icon.textContent = child.dir ? "📁" : "📄";
      const label = document.createElement("span");
      label.className = "tree-name" + (child.dir ? " dir" : "");
      label.textContent = name;
      row.append(toggle, icon, label);
      li.appendChild(row);
      if (child.dir) {
        const ul = document.createElement("ul");
        ul.className = "tree-children";
        li.appendChild(ul);
        if (depth < 1) li.classList.add("open");
        toggle.addEventListener("click", () => li.classList.toggle("open"));
        row.addEventListener("click", () => li.classList.toggle("open"));
        render(child, ul, depth + 1);
      } else {
        toggle.style.visibility = "hidden";
      }
      container.appendChild(li);
    });
  }
  render(root, box, 0);
}

document.getElementById("run-form").addEventListener("submit", (e) => {
  e.preventDefault();
  analyze();
});
logLine("No activity yet \u2014 run an analysis to fill the ledger.", "");
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
        self._ask_queues: dict[str, deque[dict]] = {}
        self._ask_done: dict[str, bool] = {}
        self._ask_sessions: dict[str, AskSession] = {}
        self._httpd: ThreadingHTTPServer | None = None

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._jobs and not self._done.get(job_id, True)

    def delete_job(self, job_id: str) -> bool:
        """Remove every artifact of a job (records, report, graph, workspace)."""
        deleted = False
        for path in (jobs_dir(self.root) / f"{job_id}.json",
                     jobs_dir(self.root) / f"{job_id}.report.json",
                     jobs_dir(self.root) / f"{job_id}.graph.db"):
            if path.is_file():
                try:
                    path.unlink()
                    deleted = True
                except OSError:
                    pass
        workspace = self.root / job_id
        if workspace.is_dir():
            shutil.rmtree(workspace, ignore_errors=True)
            deleted = True
        with self._lock:
            self._jobs.pop(job_id, None)
            self._done.pop(job_id, None)
            self._ask_queues.pop(job_id, None)
            self._ask_done.pop(job_id, None)
            self._ask_sessions.pop(job_id, None)
        return deleted

    def clear_jobs(self) -> int:
        """Delete every job on disk; returns how many were removed."""
        ids: set[str] = set()
        for pattern in ("*.json", "*.report.json", "*.graph.db"):
            ids.update(p.stem for p in jobs_dir(self.root).glob(pattern))
        return sum(1 for job_id in sorted(ids) if self.delete_job(job_id))

    def start(self) -> str:
        setup_logging()
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
        logger = logging.getLogger("clio.web")
        limits = get_limits()
        sandbox = Sandbox(root=self.root, limits=limits)
        bus = EventBus()
        bus.subscribe(lambda e: self._publish(job_id, e))
        try:
            client = make_client(get_provider(), limits)
            orchestrator = Orchestrator(sandbox, client, bus=bus, limits=limits)
            asyncio.run(orchestrator.run(url, root=sandbox.root, job_id=job_id))
        except Exception as exc:
            logger.exception("job %s failed: %s", job_id, exc)
            self._publish(
                job_id, Event(type="job.failed", job_id=job_id, data={"error": str(exc)})
            )
        finally:
            with self._lock:
                self._done[job_id] = True

    def ask_session(self, job_id: str) -> AskSession:
        with self._lock:
            session = self._ask_sessions.get(job_id)
            if session is None:
                session = AskSession(
                    job_id, self.root, make_client(get_provider(), get_limits())
                )
                self._ask_sessions[job_id] = session
            return session

    def ask_start(self, job_id: str) -> None:
        with self._lock:
            self._ask_queues[job_id] = deque()
            self._ask_done[job_id] = False

    def _publish_ask(self, job_id: str, payload: dict) -> None:
        with self._lock:
            self._ask_queues[job_id].append(payload)

    def ask_snapshot(self, job_id: str) -> tuple[list[dict], bool]:
        with self._lock:
            q = self._ask_queues.get(job_id)
            if q is None:
                return [], self._ask_done.get(job_id, True)
            pending = list(q)
            q.clear()
            return pending, self._ask_done.get(job_id, False)

    def run_ask(self, job_id: str, question: str) -> None:
        logger = logging.getLogger("clio.web")
        session = self.ask_session(job_id)
        bus = EventBus()

        def forward(event: Event) -> None:
            if event.type in (EVENT_ASK_TOOL, EVENT_ASK_FINAL):
                self._publish_ask(job_id, {"type": event.type, "data": event.data})

        bus.subscribe(forward)
        try:
            asyncio.run(session.run_turn(question, bus=bus))
        except Exception as exc:
            logger.exception("ask on job %s failed: %s", job_id, exc)
            self._publish_ask(job_id, {
                "type": EVENT_ASK_FINAL,
                "data": {"answer": f"error: {exc}", "ok": False},
            })
        finally:
            with self._lock:
                self._ask_done[job_id] = True


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
            self._send_html(INDEX_HTML.replace("__PROVIDER__", get_provider()))
            return
        if path == "/api/jobs":
            archive = ReportArchive(self.server.dashboard.root)
            jobs = []
            for report in archive.list_reports():
                row = dict(report)
                job = load_job(row["job_id"], self.server.dashboard.root)
                row["status"] = job.status if job is not None else "PERSISTED"
                row["url"] = job.url if job is not None else row.get("url", "")
                jobs.append(row)
            self._send_json(200, {"jobs": jobs})
            return
        if path.startswith("/api/jobs/"):
            rest = path[len("/api/jobs/"):]
            if rest.endswith("/graph/map"):
                self._job_map(rest[: -len("/graph/map")], parsed.query)
            elif rest.endswith("/graph"):
                self._job_graph(rest[: -len("/graph")])
            elif rest.endswith("/tree"):
                self._job_tree(rest[: -len("/tree")])
            else:
                self._job_report(rest)
            return
        if path == "/api/stream":
            self._stream(urllib.parse.parse_qs(parsed.query).get("job_id", [""])[0])
            return
        if path == "/api/ask":
            params = urllib.parse.parse_qs(parsed.query)
            self._ask(params.get("job_id", [""])[0], params.get("q", [""])[0])
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

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        dash = self.server.dashboard
        if parsed.path == "/api/jobs":
            running = [jid for jid in dash._jobs if dash.is_running(jid)]
            if running:
                self._json_error(409, f"job {running[0]} is still running")
                return
            count = dash.clear_jobs()
            self._send_json(200, {"deleted": count})
            return
        if parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path[len("/api/jobs/"):]
            if dash.is_running(job_id):
                self._json_error(409, f"job {job_id} is still running")
                return
            if not dash.delete_job(job_id):
                self._json_error(404, f"no job {job_id}")
                return
            self._send_json(200, {"deleted": job_id})
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
        self._send_json(200, {
            "stats": archive.graph_store(job_id).stats(),
            "languages": archive.graph_store(job_id).language_stats(),
            "clusters": clusters,
        })

    def _job_map(self, job_id: str, query: str) -> None:
        archive = ReportArchive(self.server.dashboard.root)
        graph = archive.get_graph(job_id)
        if graph is None:
            self._json_error(404, f"no graph for {job_id}")
            return
        payload = layout_graph(graph)
        module = urllib.parse.parse_qs(query).get("impact", [""])[0]
        if module:
            payload["impact"] = impact_of_module(archive, job_id, module).to_dict()
        self._send_json(200, payload)

    def _job_tree(self, job_id: str) -> None:
        dash = self.server.dashboard
        job = load_job(job_id, dash.root)
        workspace = job.workspace if job is not None and job.workspace else dash.root / job_id
        if not workspace.is_dir():
            self._json_error(404, f"no workspace for {job_id}")
            return
        limits = get_limits()
        files: list[str] = []
        truncated = False
        cap = 2000

        def walk(dirpath: Path) -> None:
            nonlocal truncated
            for child in sorted(dirpath.iterdir(), key=lambda p: (p.is_dir(), p.name.lower())):
                if child.is_dir():
                    if child.name in limits.exclude_dirs:
                        continue
                    walk(child)
                else:
                    files.append(child.relative_to(workspace).as_posix())
                    if len(files) >= cap:
                        truncated = True
                        return

        walk(workspace)
        self._send_json(200, {"files": files, "count": len(files), "truncated": truncated})

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


    def _ask(self, job_id: str, question: str) -> None:
        dash = self.server.dashboard
        if not question:
            self._json_error(400, "missing q parameter")
            return
        archive = ReportArchive(dash.root)
        known = job_id in dash._jobs or archive.get_report(job_id) is not None
        if not known:
            self._json_error(404, f"no job {job_id}")
            return
        dash.ask_start(job_id)
        threading.Thread(target=dash.run_ask, args=(job_id, question), daemon=True).start()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        while True:
            pending, done = dash.ask_snapshot(job_id)
            for payload in pending:
                line = json.dumps({"type": payload["type"], "data": payload["data"]})
                self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                self.wfile.flush()
            if done and not pending:
                self.wfile.write(b"event: done\ndata: {}\n\n")
                self.wfile.flush()
                break
            time.sleep(0.1)


def _main(argv: list[str] | None = None) -> int:
    """Run the dashboard from the terminal: ``python -m clio.web``."""
    parser = argparse.ArgumentParser(prog="clio-web", description="Clio analysis dashboard")
    parser.add_argument("--root", default="sandbox", help="workspace root (jobs live under root/jobs)")
    parser.add_argument("--port", type=int, default=8790, help="port to serve on")
    args = parser.parse_args(argv)
    setup_logging(file="clio.log")
    dashboard = Dashboard(Path(args.root), port=args.port)
    url = dashboard.start()
    logging.getLogger("clio").info(
        "dashboard ready at %s (provider: %s)", url, get_provider()
    )
    print(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        dashboard.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

# src/clio/web.py
"""Zero-dependency local dashboard: paste a repo, read its guide, ask anything.

API:
  GET  /                        the app (chat-first single page)
  POST /api/analyze?url=        start an analysis, returns {job_id}
  GET  /api/stream?job_id=      SSE stream of job events (job.*, job.stage)
  GET  /api/jobs                archive list
  GET  /api/jobs/<id>           report json
  GET  /api/jobs/<id>/graph     graph stats + clusters
  GET  /api/jobs/<id>/tree      workspace file list
  DELETE /api/jobs              clear archive (409 if running)
  DELETE /api/jobs/<id>         delete one job
  GET  /api/ask?job_id=&q=      SSE stream of a chat answer (ask.final)
  GET  /api/guide?job_id=       the staged guide (guide.json)
  GET  /api/modules?job_id=     module explorer data
  GET  /api/file?job_id=&path=  file content with line numbers
  GET  /api/suggest?job_id=     deterministic question chips
"""
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

from clio.ask import ChatSession
from clio.clustering import cluster_by_package
from clio.config import Limits, get_limits, get_provider
from clio.events import EVENT_ASK_FINAL, Event, EventBus
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
<title>Clio — paste a repo, understand it, ask anything</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' fill='%23F5F1E7'/%3E%3Ccircle cx='8' cy='8' r='5.5' fill='none' stroke='%239A6A1F' stroke-width='1.6'/%3E%3Cpath d='M8 1v14M1 8h14' stroke='%239A6A1F' stroke-width='1.2'/%3E%3C/svg%3E">
<style>
:root { color-scheme: light;
  --bg: #F5F1E7; --panel: #FDFBF6; --ink: #232018; --muted: #8A8475;
  --line: #E4DECD; --bronze: #9A6A1F; --bronze-soft: #F0E5D0;
  --ok: #4E7A3E; --bad: #A13D2F; --radius: 12px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font-size: 15px; line-height: 1.5; }
a { color: var(--bronze); text-decoration: none; }
a:hover { text-decoration: underline; }
button { font: inherit; cursor: pointer; }
input { font: inherit; }

header { display: flex; align-items: center; gap: 14px; padding: 14px 22px;
  border-bottom: 1px solid var(--line); background: var(--panel); }
.brand { font-weight: 700; letter-spacing: 0.02em; font-size: 17px; }
.brand .dot { color: var(--bronze); }
.provider { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); border: 1px solid var(--line); border-radius: 999px; padding: 2px 10px; }
#repo-path { color: var(--muted); font-size: 13px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; max-width: 46vw; }
#status-tag { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
  border-radius: 999px; padding: 3px 10px; border: 1px solid var(--line); color: var(--muted); }
#status-tag.ok { color: var(--ok); border-color: var(--ok); }
#status-tag.bad { color: var(--bad); border-color: var(--bad); }
.spacer { flex: 1; }
.icon-btn { background: none; border: 1px solid var(--line); border-radius: 8px;
  color: var(--muted); padding: 5px 10px; font-size: 13px; }
.icon-btn:hover { color: var(--ink); border-color: var(--muted); }

main { max-width: 1100px; margin: 0 auto; padding: 40px 22px 80px; }
.hidden { display: none !important; }

.hero { max-width: 640px; margin: 0 auto; text-align: center; padding-top: 7vh; }
.hero h1 { font-size: 34px; margin: 0 0 10px; letter-spacing: -0.02em; }
.hero p { color: var(--muted); margin: 0 0 26px; }
.paste-bar { display: flex; gap: 8px; background: var(--panel);
  border: 1px solid var(--line); border-radius: 14px; padding: 6px;
  box-shadow: 0 8px 30px rgba(35,32,24,0.06); }
.paste-bar input { flex: 1; border: none; outline: none; background: none;
  padding: 10px 12px; font-size: 15px; }
.paste-bar input::placeholder { color: var(--muted); }
.btn { border: none; border-radius: 9px; background: var(--bronze); color: #fff;
  padding: 10px 22px; font-weight: 600; font-size: 14px; }
.btn:hover { filter: brightness(1.07); }
.btn:disabled { opacity: 0.55; cursor: default; }
.btn.ghost { background: var(--bronze-soft); color: var(--bronze); }

#progress { text-align: left; margin: 26px auto 0; max-width: 640px;
  background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 16px 18px; }
#progress h3 { margin: 0 0 10px; font-size: 13px; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--muted); }
.stage-row { display: flex; align-items: center; gap: 10px; padding: 4px 0; color: var(--muted); font-size: 14px; }
.stage-row .mark { width: 18px; text-align: center; color: var(--line); }
.stage-row.active { color: var(--ink); }
.stage-row.active .mark { color: var(--bronze); }
.stage-row.done .mark { color: var(--ok); }

#history { margin-top: 44px; text-align: left; }
#history h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.1em;
  color: var(--muted); margin: 0 0 12px; }
.repo-card { background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 14px 18px; margin-bottom: 10px;
  display: flex; align-items: center; gap: 14px; cursor: pointer; }
.repo-card:hover { border-color: var(--bronze); }
.repo-card .name { font-weight: 600; font-size: 14px; }
.repo-card .sub { color: var(--muted); font-size: 13px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.repo-card .when { color: var(--muted); font-size: 12px; white-space: nowrap; }
.repo-card .tag { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
  border-radius: 999px; padding: 2px 9px; border: 1px solid var(--line); color: var(--muted); }
.repo-card .tag.ok { color: var(--ok); border-color: var(--ok); }
.repo-card .tag.bad { color: var(--bad); border-color: var(--bad); }

.repo-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 22px; align-items: start; }
@media (max-width: 860px) { .repo-grid { grid-template-columns: 1fr; } }

.left-panel { display: flex; flex-direction: column; gap: 16px; }
.tabs { display: flex; gap: 4px; background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 5px; }
.tab { border: none; background: none; padding: 8px 14px; border-radius: 8px;
  color: var(--muted); font-weight: 600; font-size: 13.5px; }
.tab:hover { color: var(--ink); }
.tab.active { background: var(--bronze-soft); color: var(--bronze); }
.stage { display: none; background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 20px 22px; min-height: 180px; }
.stage.active { display: block; }
.stage h2 { margin: 0 0 12px; font-size: 18px; letter-spacing: -0.01em; }
.stage p { margin: 0 0 10px; }
.stage .facts { color: var(--muted); font-size: 13.5px; white-space: pre-wrap;
  font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; font-size: 12.5px; }
.stage .muted-note { color: var(--muted); font-size: 13px; }

.module-box { background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius); overflow: hidden; }
.module-box h3 { margin: 0; padding: 12px 18px; font-size: 13px;
  text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted);
  border-bottom: 1px solid var(--line); cursor: pointer; user-select: none;
  display: flex; justify-content: space-between; align-items: center; }
.module-box h3 .count { color: var(--bronze); font-size: 12px; }
.module-list { max-height: 380px; overflow-y: auto; }
.module-row { border-bottom: 1px solid var(--line); }
.module-row:last-child { border-bottom: none; }
.module-row > .mod-head { display: flex; align-items: center; gap: 8px;
  padding: 10px 18px; cursor: pointer; }
.module-row > .mod-head:hover { background: var(--bronze-soft); }
.mod-head .caret { color: var(--line); transition: transform 0.15s; }
.module-row.open .caret { transform: rotate(90deg); color: var(--bronze); }
.mod-head .mname { font-weight: 600; font-size: 13.5px; }
.mod-head .mpath { color: var(--muted); font-size: 12px; }
.mod-body { display: none; padding: 4px 18px 12px 42px; }
.module-row.open .mod-body { display: block; }
.mod-body .sym { font-size: 13px; padding: 3px 0; cursor: pointer; }
.mod-body .sym:hover { color: var(--bronze); }
.mod-body .imports { color: var(--muted); font-size: 12px; padding: 6px 0 2px; }
.mod-body .imports span { display: inline-block; background: var(--bronze-soft);
  border-radius: 6px; padding: 1px 8px; margin: 2px 4px 2px 0; font-size: 12px; }

.right-panel { display: flex; flex-direction: column; gap: 12px; }
#chat { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 18px 20px; min-height: 420px; max-height: 58vh; overflow-y: auto;
  display: flex; flex-direction: column; gap: 14px; }
.bubble { max-width: 88%; padding: 11px 15px; border-radius: 14px; font-size: 14.5px; }
.bubble.user { align-self: flex-end; background: var(--bronze); color: #fff;
  border-bottom-right-radius: 4px; }
.bubble.clio { align-self: flex-start; background: var(--bg); border: 1px solid var(--line);
  border-bottom-left-radius: 4px; white-space: pre-wrap; }
.bubble.clio.bad { border-color: var(--bad); color: var(--bad); }
.bubble .src { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.src-chip { border: 1px solid var(--bronze); color: var(--bronze); background: #fff;
  border-radius: 999px; font-size: 12px; padding: 2px 10px; cursor: pointer; }
.src-chip:hover { background: var(--bronze-soft); }
.bubble .meta { margin-top: 8px; font-size: 11px; color: var(--muted); }

#chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip { border: 1px solid var(--line); background: var(--panel); color: var(--muted);
  border-radius: 999px; font-size: 13px; padding: 6px 13px; }
.chip:hover { color: var(--bronze); border-color: var(--bronze); }
.chat-bar { display: flex; gap: 8px; background: var(--panel); border: 1px solid var(--line);
  border-radius: 14px; padding: 6px; }
.chat-bar input { flex: 1; border: none; outline: none; background: none; padding: 10px 12px; }
.chat-bar input::placeholder { color: var(--muted); }

#drawer { position: fixed; inset: 0; background: rgba(35,32,24,0.4);
  display: flex; justify-content: flex-end; z-index: 40; }
#drawer .sheet { width: min(680px, 94vw); height: 100%; background: var(--panel);
  display: flex; flex-direction: column; }
.sheet .head { display: flex; align-items: center; gap: 10px; padding: 12px 18px;
  border-bottom: 1px solid var(--line); }
.sheet .head .fpath { font-family: ui-monospace, Consolas, monospace; font-size: 13px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sheet pre { flex: 1; overflow: auto; margin: 0; padding: 14px 18px; font-size: 12.5px;
  line-height: 1.55; font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; }
.sheet pre .ln { color: var(--muted); user-select: none; padding-right: 14px; }
.sheet pre .hl { background: var(--bronze-soft); }
.sheet .empty { flex: 1; display: flex; align-items: center; justify-content: center;
  color: var(--muted); }
</style>
</head>
<body id="clio-app">
<header>
  <span class="brand">Clio<span class="dot">.</span></span>
  <span class="provider" id="provider">__PROVIDER__</span>
  <span id="repo-path" class="hidden"></span>
  <span id="status-tag" class="hidden"></span>
  <span class="spacer"></span>
  <button id="btn-delete" class="icon-btn hidden">forget repo</button>
  <button id="btn-home" class="icon-btn">home</button>
</header>

<main id="view-home">
  <div class="hero">
    <h1>Paste a repository link.</h1>
    <p>Clio reads it from the ground up, builds you a guide — then answers
    anything you ask, every answer cited to the real code.</p>
    <form id="analyze-form" class="paste-bar">
      <input id="url-input" type="url" required
        placeholder="https://github.com/owner/repo" spellcheck="false">
      <button class="btn" type="submit">Analyze</button>
    </form>
    <div id="progress" class="hidden">
      <h3>Reading the repository</h3>
      <div id="stage-rows"></div>
    </div>
  </div>
  <section id="history">
    <h2>Previously read</h2>
    <div id="history-list"></div>
  </section>
</main>

<main id="view-repo" class="hidden">
  <div class="repo-grid">
    <div class="left-panel">
      <nav class="tabs" id="tabs">
        <button class="tab active" data-stage="what">What it is</button>
        <button class="tab" data-stage="how">How it runs</button>
        <button class="tab" data-stage="modules">Modules</button>
        <button class="tab" data-stage="run">Run it</button>
      </nav>
      <article class="stage active" id="stage-what"><div id="what-body"></div></article>
      <article class="stage" id="stage-how"><div id="how-body"></div></article>
      <article class="stage" id="stage-modules"><div id="modules-body"></div></article>
      <article class="stage" id="stage-run"><div id="run-body"></div></article>
      <section class="module-box" id="module-box">
        <h3>Module explorer <span class="count" id="module-count"></span></h3>
        <div class="module-list" id="module-list"></div>
      </section>
    </div>
    <div class="right-panel">
      <div id="chat"></div>
      <div id="chips"></div>
      <form id="chat-form" class="chat-bar">
        <input id="chat-input" type="text" placeholder="Ask anything about this repo…"
          autocomplete="off" spellcheck="false">
        <button class="btn" type="submit">Ask</button>
      </form>
    </div>
  </div>
</main>

<div id="drawer" class="hidden">
  <div class="sheet">
    <div class="head">
      <span class="fpath" id="drawer-path"></span>
      <span class="spacer"></span>
      <button id="drawer-close" class="icon-btn">close</button>
    </div>
    <pre id="drawer-code" class="hidden"></pre>
    <div id="drawer-empty" class="empty hidden">No file content.</div>
  </div>
</div>

<script>
"use strict";
const $ = (id) => document.getElementById(id);
let activeJob = null;

function fmtWhen(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) +
    " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}
function repoLabel(url) {
  if (!url) return "unknown repo";
  return url.replace(/^https?:\\/\\//, "").replace(/\\.git$/, "").replace(/\\/$/, "");
}

/* ---------- home ---------- */
$("analyze-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const url = $("url-input").value.trim();
  if (!url) return;
  try {
    const resp = await fetch("/api/analyze?url=" + encodeURIComponent(url), { method: "POST" });
    const body = await resp.json();
    if (!resp.ok) throw new Error(body.error || "analyze failed");
    startProgress(url, body.job_id);
  } catch (err) {
    alert("Could not start analysis: " + err.message);
  }
});

const STAGE_LABELS = [
  ["clone", "Cloning the repository"],
  ["graph", "Mapping symbols, imports and calls"],
  ["what", "What it is"],
  ["how", "How it runs"],
  ["modules", "Modules"],
  ["run", "Run it"],
];
function startProgress(url, jobId) {
  activeJob = jobId;
  $("url-input").value = "";
  $("progress").classList.remove("hidden");
  const rows = $("stage-rows");
  rows.innerHTML = "";
  for (const [key, label] of STAGE_LABELS) {
    const row = document.createElement("div");
    row.className = "stage-row";
    row.dataset.key = key;
    row.innerHTML = '<span class="mark">&#8729;</span><span>' + label + "</span>";
    rows.appendChild(row);
  }
  const es = new EventSource("/api/stream?job_id=" + encodeURIComponent(jobId));
  let done = false;
  es.onmessage = (msg) => {
    if (done) return;
    const evt = JSON.parse(msg.data);
    const type = evt.type;
    if (type === "job.cloning") setStage("clone", "active");
    if (type === "job.graphed") {
      setStage("clone", "done");
      setStage("graph", "active");
    }
    if (type === "job.stage") {
      const stage = evt.data.stage;
      if (evt.data.status === "started") {
        setStage("graph", "done");
        setStage(stage, "active");
      } else {
        setStage(stage, "done");
      }
    }
    if (type === "job.persisted" || type === "job.failed") {
      done = true;
      es.close();
      if (type === "job.persisted") loadRepo(jobId);
      else {
        setStage("clone", "done");
        $("progress").classList.add("hidden");
        alert("Analysis failed. See the terminal log for details.");
      }
    }
  };
}
function setStage(key, cls) {
  const row = document.querySelector('.stage-row[data-key="' + key + '"]');
  if (!row) return;
  row.classList.remove("active", "done");
  row.classList.add(cls);
  row.querySelector(".mark").innerHTML = cls === "done" ? "&#10003;" : cls === "active" ? "&#8226;" : "&#8729;";
}

/* ---------- archive ---------- */
async function refreshHistory() {
  const list = $("history-list");
  try {
    const resp = await fetch("/api/jobs");
    const body = await resp.json();
    const jobs = (body.jobs || []).sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
    list.innerHTML = "";
    if (!jobs.length) {
      list.innerHTML = '<div class="repo-card"><span class="sub">No repositories yet — paste one above.</span></div>';
      return;
    }
    for (const job of jobs) {
      const card = document.createElement("div");
      card.className = "repo-card";
      const ok = job.status === "PERSISTED", bad = job.status === "FAILED";
      card.innerHTML =
        '<div style="flex:1;min-width:0"><div class="name">' + repoLabel(job.repo_url || job.url) + "</div>" +
        '<div class="sub">' + (job.summary || "") + "</div></div>" +
        '<span class="tag ' + (bad ? "bad" : ok ? "ok" : "") + '">' + (job.status || "QUEUED") + "</span>" +
        '<span class="when">' + fmtWhen(job.created_at) + "</span>";
      card.addEventListener("click", () => loadRepo(job.job_id));
      list.appendChild(card);
    }
  } catch (err) {
    list.innerHTML = '<div class="repo-card"><span class="sub">Archive unavailable: ' + err.message + "</span></div>";
  }
}

/* ---------- repo view ---------- */
async function loadRepo(jobId) {
  activeJob = jobId;
  $("view-home").classList.add("hidden");
  $("view-repo").classList.remove("hidden");
  $("progress").classList.add("hidden");
  $("btn-delete").classList.remove("hidden");
  $("status-tag").classList.remove("hidden");
  $("chat").innerHTML = "";
  $("chips").innerHTML = "";
  resetStages();
  const [guide, modules, suggest] = await Promise.all([
    fetchJson("/api/guide?job_id=" + encodeURIComponent(jobId)),
    fetchJson("/api/modules?job_id=" + encodeURIComponent(jobId)),
    fetchJson("/api/suggest?job_id=" + encodeURIComponent(jobId)),
  ]);
  const pathEl = $("repo-path");
  pathEl.textContent = guide && guide.repo ? guide.repo : jobId;
  pathEl.classList.remove("hidden");
  const tag = $("status-tag");
  tag.textContent = "ready";
  tag.className = "ok";
  renderGuide(guide);
  renderModules(modules);
  renderChips(suggest ? suggest.chips : []);
  const welcome = addBubble("clio", "The guide on the left explains this repo from the ground up. Ask me anything — every answer comes with citations into the actual code.", []);
  welcome.querySelector(".meta").textContent = "What would you like to know?";
}
async function fetchJson(url) {
  try {
    const resp = await fetch(url);
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) { return null; }
}

function resetStages() {
  for (const id of ["what", "how", "modules", "run"]) {
    $(id + "-body").innerHTML = '<p class="muted-note">…</p>';
  }
}
function renderGuide(guide) {
  if (!guide || !guide.stages) {
    for (const id of ["what", "how", "modules", "run"]) {
      $(id + "-body").innerHTML = '<p class="muted-note">No guide for this job yet.</p>';
    }
    return;
  }
  for (const stage of ["what", "how", "modules", "run"]) {
    const s = guide.stages[stage];
    const body = $(stage + "-body");
    if (!s || !s.text) {
      body.innerHTML = '<p class="muted-note">Nothing written for this stage.</p>';
      continue;
    }
    body.innerHTML = "<p>" + esc(s.text) + "</p>";
    if (s.sources && s.sources.length) {
      body.innerHTML += '<div class="src">' + s.sources.map((p) =>
        '<button class="src-chip" onclick="openFile(' + escAttr(p) + ')">' + esc(p) + "</button>"
      ).join("") + "</div>";
    }
  }
}
function esc(s) { return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
function escAttr(s) { return s.replace(/"/g, "&quot;"); }

$("tabs").addEventListener("click", (ev) => {
  const tab = ev.target.closest(".tab");
  if (!tab) return;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
  document.querySelectorAll(".stage").forEach((st) => st.classList.toggle("active", st.id === "stage-" + tab.dataset.stage));
});

function renderModules(payload) {
  const count = $("module-count");
  const list = $("module-list");
  if (!payload || !payload.modules || !payload.modules.length) {
    count.textContent = "";
    list.innerHTML = '<div class="mod-head" style="cursor:default"><span class="mname">no modules</span></div>';
    return;
  }
  count.textContent = payload.modules.length + " modules";
  list.innerHTML = "";
  for (const mod of payload.modules) {
    const row = document.createElement("div");
    row.className = "module-row";
    const syms = (mod.symbols || []).map((s) =>
      '<div class="sym" onclick="openFile(' + escAttr(mod.path) + ')">' + esc(s) + "</div>"
    ).join("");
    const imports = (mod.imports || []).length
      ? '<div class="imports">imports: ' + (mod.imports || []).map((i) => "<span>" + esc(i) + "</span>").join("") + "</div>"
      : "";
    row.innerHTML =
      '<div class="mod-head"><span class="caret">&#9654;</span>' +
      '<span class="mname">' + esc(mod.name) + "</span>" +
      '<span class="mpath">' + esc(mod.path) + "</span></div>" +
      '<div class="mod-body">' + syms + imports + "</div>";
    row.querySelector(".mod-head").addEventListener("click", () => row.classList.toggle("open"));
    list.appendChild(row);
  }
}

/* ---------- chat ---------- */
function addBubble(kind, text, sources) {
  const chat = $("chat");
  const bubble = document.createElement("div");
  bubble.className = "bubble " + kind;
  const srcHtml = sources && sources.length
    ? '<div class="src">' + sources.map((s) =>
        '<button class="src-chip" onclick="openFile(' + escAttr(s.path) + ')">' +
        esc(s.path + ":" + (s.start || 1)) + "</button>").join("") + "</div>"
    : "";
  bubble.innerHTML = esc(text) + srcHtml + '<div class="meta"></div>';
  chat.appendChild(bubble);
  chat.scrollTop = chat.scrollHeight;
  return bubble;
}
function ask(question) {
  if (!activeJob || !question) return;
  addBubble("user", question, null);
  const waiting = addBubble("clio", "Reading the code…", null);
  const es = new EventSource("/api/ask?job_id=" + encodeURIComponent(activeJob) +
    "&q=" + encodeURIComponent(question));
  es.onmessage = (msg) => {
    const evt = JSON.parse(msg.data);
    if (evt.type !== "ask.final") return;
    waiting.remove();
    const data = evt.data || {};
    addBubble("clio" + (data.ok ? "" : " bad"), data.answer || "(no answer)", data.sources || []);
  };
  es.addEventListener("error", () => es.close());
  es.addEventListener("done", () => es.close());
}
$("chat-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const input = $("chat-input");
  const q = input.value.trim();
  if (!q) return;
  input.value = "";
  ask(q);
});
function renderChips(chips) {
  const box = $("chips");
  box.innerHTML = "";
  for (const chip of chips) {
    const b = document.createElement("button");
    b.className = "chip";
    b.textContent = chip;
    b.addEventListener("click", () => ask(chip));
    box.appendChild(b);
  }
}

/* ---------- file drawer ---------- */
window.openFile = async function openFile(path) {
  if (!activeJob) return;
  const drawer = $("drawer");
  drawer.classList.remove("hidden");
  $("drawer-path").textContent = path;
  $("drawer-code").classList.add("hidden");
  $("drawer-empty").classList.remove("hidden");
  const data = await fetchJson("/api/file?job_id=" + encodeURIComponent(activeJob) +
    "&path=" + encodeURIComponent(path));
  if (!data || !data.lines) return;
  $("drawer-empty").classList.add("hidden");
  const pre = $("drawer-code");
  pre.classList.remove("hidden");
  pre.innerHTML = data.lines.map((line, i) =>
    '<span class="ln">' + (i + 1) + "</span>" + esc(line)
  ).join("\\n");
};
$("drawer-close").addEventListener("click", () => $("drawer").classList.add("hidden"));
$("drawer").addEventListener("click", (ev) => { if (ev.target === $("drawer")) $("drawer").classList.add("hidden"); });

/* ---------- header actions ---------- */
$("btn-home").addEventListener("click", () => {
  activeJob = null;
  $("view-repo").classList.add("hidden");
  $("view-home").classList.remove("hidden");
  $("btn-delete").classList.add("hidden");
  $("status-tag").classList.add("hidden");
  $("repo-path").classList.add("hidden");
  refreshHistory();
});
$("btn-delete").addEventListener("click", async () => {
  if (!activeJob || !confirm("Forget this repository and all its artifacts?")) return;
  const resp = await fetch("/api/jobs/" + encodeURIComponent(activeJob), { method: "DELETE" });
  if (resp.ok) $("btn-home").click();
  else alert("Could not delete job.");
});

refreshHistory();
</script>
</body>
</html>
"""


class Dashboard:
    """Owns the analysis workers and per-job event/ask queues."""

    def __init__(self, root: Path | str, port: int = 8790) -> None:
        self.root = Path(root)
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()
        self._queues: dict[str, deque] = {}
        self._done: dict[str, bool] = {}
        self._jobs: dict[str, dict] = {}
        self._ask_queues: dict[str, deque] = {}
        self._ask_done: dict[str, bool] = {}
        self._ask_sessions: dict[str, ChatSession] = {}

    def start(self) -> str:
        handler = _make_handler()
        httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        httpd.dashboard = self
        self._httpd = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        host, port = httpd.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()

    def register_job(self, job_id: str) -> None:
        with self._lock:
            self._queues[job_id] = deque()
            self._done[job_id] = False
            self._jobs[job_id] = {}

    def _publish(self, job_id: str, event: Event) -> None:
        with self._lock:
            self._queues[job_id].append(event)

    def snapshot(self, job_id: str) -> tuple[list[Event], bool]:
        with self._lock:
            q = self._queues.get(job_id)
            if q is None:
                return [], self._done.get(job_id, True)
            pending = list(q)
            q.clear()
            return pending, self._done.get(job_id, False)

    def is_running(self, job_id: str) -> bool:
        return self._queues.get(job_id) is not None and not self._done.get(job_id, True)

    def delete_job(self, job_id: str) -> bool:
        from clio.job import jobs_dir

        with self._lock:
            existed = job_id in self._queues
            self._queues.pop(job_id, None)
            self._done.pop(job_id, None)
            self._jobs.pop(job_id, None)
            self._ask_queues.pop(job_id, None)
            self._ask_done.pop(job_id, None)
            self._ask_sessions.pop(job_id, None)
        artifacts = list(jobs_dir(self.root).glob(f"{job_id}.*"))
        workspace = self.root / job_id
        existed = existed or bool(artifacts) or workspace.is_dir()
        for path in artifacts:
            path.unlink(missing_ok=True)
        if workspace.is_dir():
            shutil.rmtree(workspace, ignore_errors=True)
        return existed

    def clear_jobs(self) -> int:
        ids: set[str] = set()
        for pattern in ("*.json", "*.graph.db"):
            ids.update(p.stem for p in jobs_dir(self.root).glob(pattern))
        return sum(1 for job_id in sorted(ids) if self.delete_job(job_id))

    def run_job(self, url: str, job_id: str) -> None:
        logger = logging.getLogger("clio.web")
        bus = EventBus()
        bus.subscribe(lambda event: self._publish(job_id, event))
        try:
            limits = get_limits()
            client = make_client(get_provider(), limits)
            sandbox = Sandbox(root=self.root, limits=limits)
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

    def ask_session(self, job_id: str) -> ChatSession:
        with self._lock:
            session = self._ask_sessions.get(job_id)
            if session is None:
                session = ChatSession(
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
            if event.type == EVENT_ASK_FINAL:
                self._publish_ask(job_id, {"type": event.type, "data": event.data})

        bus.subscribe(forward)
        try:
            asyncio.run(session.answer(question, bus=bus))
        except Exception as exc:
            logger.exception("ask on job %s failed: %s", job_id, exc)
            self._publish_ask(job_id, {
                "type": EVENT_ASK_FINAL,
                "data": {"answer": f"error: {exc}", "ok": False},
            })
        finally:
            try:
                session.write_memory(job_id, self.root)
            except Exception:
                logger.exception("memory write failed for job %s", job_id)
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
        if path == "/api/guide":
            self._job_guide(urllib.parse.parse_qs(parsed.query).get("job_id", [""])[0])
            return
        if path == "/api/modules":
            self._job_modules(urllib.parse.parse_qs(parsed.query).get("job_id", [""])[0])
            return
        if path == "/api/file":
            params = urllib.parse.parse_qs(parsed.query)
            self._job_file(params.get("job_id", [""])[0], params.get("path", [""])[0])
            return
        if path == "/api/suggest":
            self._job_suggest(urllib.parse.parse_qs(parsed.query).get("job_id", [""])[0])
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

    def _job_guide(self, job_id: str) -> None:
        path = jobs_dir(self.server.dashboard.root) / f"{job_id}.guide.json"
        if not path.is_file():
            self._json_error(404, f"no guide for {job_id}")
            return
        try:
            guide = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._json_error(500, f"guide for {job_id} is unreadable")
            return
        job = load_job(job_id, self.server.dashboard.root)
        guide["repo"] = job.url if job is not None else ""
        self._send_json(200, guide)

    def _job_modules(self, job_id: str) -> None:
        archive = ReportArchive(self.server.dashboard.root)
        graph = archive.get_graph(job_id)
        if graph is None:
            self._json_error(404, f"no graph for {job_id}")
            return
        modules = []
        for module in sorted(graph.modules):
            modules.append({
                "name": module,
                "path": Path(graph.modules[module]).as_posix(),
                "symbols": sorted(s.name for s in graph.symbols if s.module == module),
                "imports": list(graph.imports.get(module, ())),
            })
        self._send_json(200, {"modules": modules, "count": len(modules)})

    def _job_file(self, job_id: str, path: str) -> None:
        dash = self.server.dashboard
        job = load_job(job_id, dash.root)
        workspace = job.workspace if job is not None and job.workspace else dash.root / job_id
        if not workspace.is_dir():
            self._json_error(404, f"no workspace for {job_id}")
            return
        if not path:
            self._json_error(400, "missing path parameter")
            return
        workspace = workspace.resolve()
        target = (workspace / path).resolve()
        if not str(target).casefold().startswith(str(workspace).casefold()):
            self._json_error(403, "path escapes the workspace")
            return
        if not target.is_file():
            self._json_error(404, f"no file {path}")
            return
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            self._json_error(500, f"cannot read {path}")
            return
        self._send_json(200, {"path": path, "lines": text.splitlines()})

    def _job_suggest(self, job_id: str) -> None:
        dash = self.server.dashboard
        archive = ReportArchive(dash.root)
        chips: list[str] = []
        guide_path = jobs_dir(dash.root) / f"{job_id}.guide.json"
        guide: dict | None = None
        if guide_path.is_file():
            try:
                guide = json.loads(guide_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                guide = None
        if guide and guide.get("stages", {}).get("what"):
            chips.append("What does this project do?")
            chips.append("How do I run it?")
        graph = archive.get_graph(job_id)
        if graph is not None:
            seen: set[str] = set()
            for sym in sorted(graph.symbols, key=lambda s: s.name):
                if sym.name in seen or len(chips) >= 5:
                    continue
                seen.add(sym.name)
                chips.append(f"Where is {sym.name} defined?")
            if len(chips) < 5:
                for module in sorted(graph.modules)[:4]:
                    chips.append(f"What is in {module}?")
        self._send_json(200, {"chips": chips[:6]})

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


def _make_handler() -> type[BaseHTTPRequestHandler]:
    return _Handler


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
# src/clio/repomap.py
"""Repo map: signature-level overview ranked by personalized PageRank."""
from __future__ import annotations

from pathlib import Path

from clio.graph import RepoGraph
from clio.retrieval import _signature_line


def file_reference_graph(graph: RepoGraph) -> dict[str, set[str]]:
    local = set(graph.modules)
    edges: dict[str, set[str]] = {m: set() for m in local}
    for module, targets in graph.imports.items():
        for target in targets:
            for mod in local:
                if target == mod or target.startswith(mod + "."):
                    edges[module].add(mod)
                    edges[mod].add(module)
    for edge in graph.calls:
        caller = edge.caller.split("::", 1)[0]
        callee = edge.callee.split("::", 1)[0]
        if caller in edges and callee in edges:
            edges[caller].add(callee)
    return edges


def personalized_pagerank(
    edges: dict[str, set[str]],
    personal: dict[str, float],
    alpha: float = 0.85,
    iters: int = 30,
) -> dict[str, float]:
    nodes = list(edges)
    rank = {n: 1.0 / len(nodes) if nodes else 0.0 for n in nodes}
    p_total = sum(personal.values()) or 1.0
    personal = {n: v / p_total for n, v in personal.items()}
    out_degree = {n: max(len(edges.get(n, ())), 1) for n in nodes}
    for _ in range(iters):
        new: dict[str, float] = {}
        for n in nodes:
            contrib = 0.0
            for m, targets in edges.items():
                if n in targets:
                    contrib += rank[m] / out_degree[m]
            new[n] = (1 - alpha) * personal.get(n, 0.0) + alpha * contrib
        rank = new
    return rank


def _query_personalization(graph: RepoGraph, query: str) -> dict[str, float]:
    from clio.retrieval import tokenize

    terms = set(tokenize(query))
    personal: dict[str, float] = {}
    for module in graph.modules:
        if any(t in module for t in terms):
            personal[module] = 1.0
    return personal


def render_repo_map(
    graph: RepoGraph,
    scores: dict[str, float],
    workspace: Path,
    top: int = 60,
    budget_chars: int = 1500,
) -> str:
    symbols = sorted(graph.symbols, key=lambda s: (-scores.get(s.module, 0.0), s.line))
    lines: list[str] = []
    seen_mods: set[str] = set()
    for sym in symbols[: top * 3]:
        if len(lines) >= top:
            break
        score = scores.get(sym.module, 0.0)
        if score <= 0:
            continue
        rel = graph.modules.get(sym.module, "")
        if not rel:
            continue
        if sym.module not in seen_mods:
            seen_mods.add(sym.module)
            lines.append(f"# {sym.module}")
        path = workspace / rel
        try:
            src = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        sig = _signature_line(src, sym.line)
        lines.append(f"  {sym.name}  ({sym.kind})" + (f"  — {sig}" if sig else ""))
    text = "\n".join(lines)
    if len(text) > budget_chars:
        text = text[:budget_chars]
    return text


def build_repo_map(workspace: Path, graph: RepoGraph, query: str = "",
                   budget_chars: int = 1500) -> str:
    edges = file_reference_graph(graph)
    personal = _query_personalization(graph, query)
    if not personal:
        personal = {m: 1.0 for m in edges}
    scores = personalized_pagerank(edges, personal)
    return render_repo_map(graph, scores, workspace, budget_chars=budget_chars)

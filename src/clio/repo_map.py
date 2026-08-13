# src/clio/repo_map.py
"""Deterministic repo map: rank modules by PageRank, then fit to a token budget.

Based on the Repository Map pattern (aider): parse -> rank -> fit. This lets a
single-shot aspect agent see the architectural spine of the repo in a few
hundred chars instead of re-discovering it via tool calls.
"""
from __future__ import annotations

from collections import defaultdict

from clio.graph import RepoGraph


def module_importance(
    graph: RepoGraph, damp: float = 0.85, iters: int = 25
) -> dict[str, float]:
    """PageRank over the directed module import graph (src -> target).

    Only edges whose target resolves to a local module participate, so external
    package names never pollute the graph. Returns {module: score}."""
    modules = set(graph.modules)

    def _resolve_target(raw: str) -> str | None:
        if raw in modules:
            return raw
        head = raw.rsplit(".", 1)[0]
        if head in modules:
            return head
        for m in modules:
            if m.endswith("." + head):
                return m
        return None

    out_edges: dict[str, list[str]] = {m: [] for m in modules}
    for src, targets in graph.imports.items():
        if src not in modules:
            continue
        for t in targets:
            resolved = _resolve_target(t)
            if resolved is not None and resolved != src:
                out_edges[src].append(resolved)

    scores = {m: 1.0 / max(len(modules), 1) for m in modules}
    if not modules:
        return scores
    dangling = {m for m in modules if not out_edges[m]}
    for _ in range(iters):
        new_scores = {m: (1.0 - damp) / len(modules) for m in modules}
        for src, targets in out_edges.items():
            if not targets:
                continue
            share = scores[src] / len(targets)
            for t in targets:
                new_scores[t] += damp * share
        if dangling:
            spill = damp * sum(scores[m] for m in dangling) / len(modules)
            for m in modules:
                new_scores[m] += spill
        scores = new_scores
    return scores


def ranked_modules(graph: RepoGraph, top: int = 30) -> list[str]:
    """Modules sorted by PageRank (ties broken by symbol count, then name)."""
    ranks = module_importance(graph)
    symbol_count = defaultdict(int)
    for s in graph.symbols:
        symbol_count[s.module] += 1
    return sorted(
        ranks,
        key=lambda m: (-ranks[m], -symbol_count.get(m, 0), m),
    )[:top]


def fitted_repo_map(
    graph: RepoGraph,
    budget_chars: int = 1200,
    top: int = 30,
    max_symbols_per_module: int = 6,
) -> str:
    """A compact, ranked map of the repo fitted to ``budget_chars``.

    Lines look like ``module/path::symbol  : kind`` for the most important
    symbols in each important module. Binary-search the module count so the
    whole map fits the budget."""
    if not graph.modules:
        return "(no code graph extracted for this repository)"
    ordered = ranked_modules(graph, top=top)
    call_degree: dict[str, int] = defaultdict(int)
    for c in graph.calls:
        call_degree[c.callee] += 1

    def _map_text(count: int) -> str:
        lines = ["repo map (ranked):"]
        for module in ordered[:count]:
            path = graph.modules.get(module, module)
            symbols = sorted(
                (s for s in graph.symbols if s.module == module),
                key=lambda s: (-call_degree.get(f"{module}::{s.name}", 0), s.line),
            )[:max_symbols_per_module]
            heading = f"{module}  ({path})"
            lines.append(heading)
            for s in symbols:
                degree = call_degree.get(f"{module}::{s.name}", 0)
                suffix = f"  [calls: {degree}]" if degree else ""
                lines.append(f"  {s.name}  : {s.kind}{suffix}")
        return "\n".join(lines)

    # Binary search the largest count that fits the budget.
    lo, hi = 0, len(ordered)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(_map_text(mid)) <= budget_chars:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return _map_text(best) if best else (
        "(repo too large for a full map; top files inline in the pack)"
    )

# src/clio/map.py
"""Deterministic module-map layout: cluster columns -> module rows -> SVG coordinates.

Coordinates are pure functions of the graph: clusters sorted by name get one column
each (x = col * COL_W), modules sorted by name stack top-down inside their column
(y = row * ROW_H). No randomness, so the same repo + job always yields the same map.

Edges are module-level: imports (graph.imports targets) plus resolved calls
(CallEdge callees that carry a module path). One edge per (from, to) pair; kind is
"import", "call", or "both".
"""
from __future__ import annotations

from clio.clustering import cluster_by_package
from clio.graph import RepoGraph

COL_W = 260
ROW_H = 120


def resolve_module(target: str, modules: list[str]) -> str | None:
    """Map an import target or callee to a module node id.

    Exact module wins. Otherwise the longest module the target is nested under
    (target "pkg.two.b" -> module "pkg.two"), else the shallowest module the target
    is an ancestor of (target "clio" -> module "clio.x"; alias "clio.config" ->
    "src.clio.config" via the .endswith rule). Import targets carry symbol
    suffixes ("clio.config.Limits"), so unresolved targets retry with trailing
    dotted segments stripped ("clio.config.Limits" -> "clio.config"). Returns
    None when nothing matches.
    """
    while True:
        exact = target if target in modules else None
        if exact is not None:
            return exact
        deeper = [m for m in modules if target.startswith(m + ".")]
        if deeper:
            return max(deeper, key=len)
        under = [
            m for m in modules
            if m.startswith(target + ".") or m.endswith("." + target)
        ]
        if under:
            return min(under, key=len)
        if "." not in target:
            return None
        target = target.rsplit(".", 1)[0]


def layout_graph(graph: RepoGraph) -> dict:
    """Return {"nodes": [...], "edges": [...]} with deterministic x, y coordinates."""
    modules = sorted(graph.modules)
    node_ids = set(modules)

    nodes: list[dict] = []
    for col, cluster in enumerate(sorted(cluster_by_package(graph), key=lambda c: c.name)):
        x = col * COL_W
        for row, module in enumerate(sorted(cluster.modules)):
            nodes.append({
                "id": module,
                "module": module,
                "cluster": cluster.name,
                "symbols": sum(1 for s in graph.symbols if s.module == module),
                "x": x,
                "y": row * ROW_H,
            })

    edges_by_pair: dict[tuple[str, str], set[str]] = {}
    for module, targets in graph.imports.items():
        if module not in node_ids:
            continue
        for target in targets:
            other = resolve_module(target, modules)
            if other is None or other == module:
                continue
            edges_by_pair.setdefault((module, other), set()).add("import")
    for edge in graph.calls:
        caller_module = edge.caller.rsplit("::", 1)[0]
        if caller_module not in node_ids:
            continue
        if "::" in edge.callee:
            callee_module = edge.callee.rsplit("::", 1)[0]
        elif "." in edge.callee:
            callee_module = edge.callee
        else:
            continue
        other = resolve_module(callee_module, modules)
        if other is None or other == caller_module:
            continue
        edges_by_pair.setdefault((caller_module, other), set()).add("call")

    edges = [
        {
            "from": pair[0],
            "to": pair[1],
            "kind": "both" if len(kinds) == 2 else next(iter(kinds)),
        }
        for pair, kinds in sorted(edges_by_pair.items())
    ]
    return {"nodes": nodes, "edges": edges}

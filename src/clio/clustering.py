# src/clio/clustering.py
"""Module clustering: package-level grouping and import-graph components."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from clio.graph import RepoGraph


@dataclass
class Cluster:
    name: str
    modules: list[str]
    symbols: int
    external_edges: int


def top_prefix(module: str) -> str:
    """First dotted segment of a module or import target."""
    return module.split(".", 1)[0]


def cluster_by_package(graph: RepoGraph, depth: int = 1) -> list[Cluster]:
    """Group modules by the first `depth` dotted segments of their package path.
    Modules shorter than `depth` stay in their own cluster. Deterministic order."""
    buckets: dict[str, list[str]] = defaultdict(list)
    for module in graph.modules:
        parts = module.split(".")
        prefix = ".".join(parts[:depth]) if len(parts) > depth else module
        buckets[prefix].append(module)
    clusters: list[Cluster] = []
    for name in sorted(buckets):
        members = sorted(buckets[name])
        member_set = set(members)
        symbols = sum(1 for s in graph.symbols if s.module in member_set)
        external_edges = sum(
            1
            for module in members
            for target in graph.imports.get(module, [])
            if top_prefix(target) != name
        )
        clusters.append(
            Cluster(name=name, modules=members, symbols=symbols, external_edges=external_edges)
        )
    return clusters


def connected_components(graph: RepoGraph) -> list[list[str]]:
    """Undirected components over import edges where both sides are in-repo
    modules. A target connects via its top-level package when that package is
    itself a module, or via the full dotted name when it is one."""
    adj: dict[str, set[str]] = defaultdict(set)
    for module in graph.modules:
        adj[module]
    for src, targets in graph.imports.items():
        if src not in graph.modules:
            continue
        for target in targets:
            top = target.split(".", 1)[0]
            if top in graph.modules:
                adj[src].add(top)
                adj[top].add(src)
            elif target in graph.modules:
                adj[src].add(target)
                adj[target].add(src)
    seen: set[str] = set()
    components: list[list[str]] = []
    for module in sorted(graph.modules):
        if module in seen:
            continue
        stack = [module]
        seen.add(module)
        component: list[str] = []
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in sorted(adj[current]):
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        components.append(sorted(component))
    return components

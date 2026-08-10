# src/clio/impact.py
"""Impact analysis: what breaks if a symbol or module breaks.

Walks reverse edges from the graph store: callers of a symbol (up to `depth`
hops) plus importers of its module; or importers of a module (up to `depth`
hops). Verdict: "missing" (not in graph), "contained" (one cluster hit),
"cross-cutting" (2+ clusters hit).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from clio.clustering import cluster_by_package
from clio.reports import ReportArchive
from clio.store import GraphStore


@dataclass
class ImpactReport:
    scope: str                  # symbol id ("module::name") or module name
    affected_modules: list[str]
    callers: list[tuple[str, int]]  # (caller symbol, line); empty for module scope
    clusters_hit: list[str]
    verdict: str                # "missing" | "contained" | "cross-cutting"

    def to_dict(self) -> dict:
        return asdict(self)


def _clusters_hit(store: GraphStore, affected: set[str]) -> list[str]:
    graph = store.load()
    return sorted(
        c.name for c in cluster_by_package(graph)
        if any(m in c.modules for m in affected)
    )


def _verdict(clusters_hit: list[str]) -> str:
    return "contained" if len(clusters_hit) <= 1 else "cross-cutting"


def impact_of_symbol(
    archive: ReportArchive, job_id: str, symbol_id: str, depth: int = 3,
) -> ImpactReport:
    store = archive.graph_store(job_id)
    if not store.has_symbol(symbol_id):
        return ImpactReport(
            scope=symbol_id, affected_modules=[], callers=[],
            clusters_hit=[], verdict="missing",
        )
    seen: set[tuple[str, str, int]] = set()
    frontier = [symbol_id]
    callers: list[tuple[str, int]] = []
    affected: set[str] = set()
    for _ in range(depth):
        next_frontier: list[str] = []
        for target in frontier:
            for caller, line in store.callers_of(target):
                if (caller, target, line) in seen:
                    continue
                seen.add((caller, target, line))
                callers.append((caller, line))
                affected.add(caller.rsplit("::", 1)[0])
                next_frontier.append(caller)
        frontier = next_frontier
    for importer in store.modules_importing(symbol_id.rsplit("::", 1)[0]):
        affected.add(importer)
    clusters_hit = _clusters_hit(store, affected)
    return ImpactReport(
        scope=symbol_id,
        affected_modules=sorted(affected),
        callers=sorted(callers),
        clusters_hit=clusters_hit,
        verdict=_verdict(clusters_hit),
    )


def impact_of_module(
    archive: ReportArchive, job_id: str, module: str, depth: int = 3,
) -> ImpactReport:
    store = archive.graph_store(job_id)
    if module not in store.load().modules:
        return ImpactReport(
            scope=module, affected_modules=[], callers=[],
            clusters_hit=[], verdict="missing",
        )
    affected: set[str] = set()
    frontier: set[str] = {module}
    for _ in range(depth + 1):
        next_frontier: set[str] = set()
        for m in frontier:
            if m in affected:
                continue
            affected.add(m)
            next_frontier.update(store.modules_importing(m))
        frontier = next_frontier
    clusters_hit = _clusters_hit(store, affected)
    return ImpactReport(
        scope=module,
        affected_modules=sorted(affected),
        callers=[],
        clusters_hit=clusters_hit,
        verdict=_verdict(clusters_hit),
    )

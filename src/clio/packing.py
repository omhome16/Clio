# src/clio/packing.py
"""Deterministic context packs: each aspect agent gets pre-packed evidence so a
single LLM call suffices — no tool loop, no re-discovery, low token cost.

Packing is 100% heuristic + AST; no model in the loop. This is what makes the
free-tier budget work: 2 aspects + 1 synthesis = 3 calls per job.
"""
from __future__ import annotations

from pathlib import Path

from clio.config import Limits
from clio.graph import RepoGraph
from clio.repo_map import ranked_modules

RISK_PATTERNS = (
    (r"TODO|FIXME|HACK|XXX", "todo"),
    (r"except\s*:", "bare except"),
    (r"except Exception(\s+as\s+\w+)?:", "broad except"),
    (r"(?i)password\s*=\s*['\"]", "hardcoded credential"),
    (r"(?i)api[_-]?key\s*=\s*['\"]", "hardcoded credential"),
    (r"pass\s*$", "stub pass"),
)

ENTRYPOINT_HINTS = (
    "main", "cli", "server", "app", "cmd", "run", "setup", "build", "entry",
    "__main__", "manage", "index", "launcher",
)
DOC_HINTS = ("readme", "contributing", "getting-started", "architecture")


def _source_files(workspace: Path, graph: RepoGraph, limits: Limits) -> list[Path]:
    """Ordered, deduped source files: graph modules first (by rank), then the
    full workspace walk (covers repos with no code graph)."""
    seen: set[Path] = set()
    ordered: list[Path] = []
    for module in ranked_modules(graph, top=40):
        rel = graph.modules.get(module)
        if not rel:
            continue
        path = (workspace / rel).resolve()
        if path.is_file() and path not in seen:
            seen.add(path)
            ordered.append(path)
    for path in sorted(workspace.rglob("*")):
        if path.is_file() and any(part in limits.exclude_dirs for part in path.relative_to(workspace).parts):
            continue
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            ordered.append(path)
    return ordered


def _risk_hits(workspace: Path, graph: RepoGraph, limits: Limits, cap: int = 24) -> str:
    import re

    hits: list[str] = []
    for path in _source_files(workspace, graph, limits):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > 400_000:  # skip huge files (minified, lockfiles)
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if len(hits) >= cap:
                return "\n".join(hits)
            for pattern, label in RISK_PATTERNS:
                if re.search(pattern, line.strip()):
                    rel = path.relative_to(workspace).as_posix()
                    hits.append(f"{rel}:{lineno}  [{label}]  {line.strip()[:120]}")
                    break
    return "\n".join(hits) or "(no obvious risk markers found)"


def _entrypoint_files(
    workspace: Path, graph: RepoGraph, limits: Limits, cap: int = 8,
) -> list[tuple[Path, str]]:
    """Candidate entrypoint/doc files (by name hints) plus the top-ranked
    modules' files as fallback coverage."""
    candidates: list[tuple[Path, str]] = []
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()

    ordered = _source_files(workspace, graph, limits)
    for path in ordered:
        rel = path.relative_to(workspace).as_posix()
        name = path.name.lower()
        base = path.stem.lower()
        if any(h in name or h in base for h in ENTRYPOINT_HINTS):
            candidates.append((path, "entrypoint"))
        elif any(h in name for h in DOC_HINTS):
            candidates.append((path, "doc"))
    for path, kind in candidates:
        rel = path.relative_to(workspace).as_posix()
        if rel in seen:
            continue
        seen.add(rel)
        out.append((path, kind))
        if len(out) >= cap:
            break
    # fallback: top ranked modules' first file each
    for path in ordered:
        if len(out) >= cap:
            break
        rel = path.relative_to(workspace).as_posix()
        if rel in seen:
            continue
        seen.add(rel)
        out.append((path, "core"))
    return out


def _snippet(path: Path, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:max_chars]


def pack_entrypoints(
    workspace: Path, graph: RepoGraph, limits: Limits,
) -> str:
    parts: list[str] = ["Entry points and run flow:"]
    per_file = max(limits.aspect_pack_chars // 2 // 8, 400)
    budget = limits.aspect_pack_chars
    used = 0
    for path, kind in _entrypoint_files(workspace, graph, limits, cap=6):
        rel = path.relative_to(workspace).as_posix()
        head = f"[{kind}] {rel}"
        snippet = _snippet(path, per_file)
        block = f"{head}\n{snippet}"
        if used + len(block) > budget:
            block = block[: budget - used]
        parts.append(block)
        used += len(block) + 1
        if used >= budget:
            break
    return "\n".join(parts)


def pack_risks(
    workspace: Path, graph: RepoGraph, limits: Limits,
) -> str:
    hits = _risk_hits(workspace, graph, limits)
    parts = ["Risks / failure points (deterministic scan):", hits]
    budget = limits.aspect_pack_chars
    used = len(hits)
    for module in ranked_modules(graph, top=8):
        rel = graph.modules.get(module)
        if not rel:
            continue
        path = (workspace / rel).resolve()
        if not path.is_file():
            continue
        block = f"\n[file] {rel}\n{_snippet(path, 1500)}"
        if used + len(block) > budget:
            block = block[: budget - used]
        parts.append(block)
        used += len(block) + 1
        if used >= budget:
            break
    return "\n".join(parts)

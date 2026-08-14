# src/clio/guide.py
"""The guide: a staged, code-grounded walkthrough of a repository.

Deterministic facts (README head, entry points, module table, clusters, run
commands) are derived straight from the repo — the LLM only rewrites each
stage into short prose, always grounded in excerpts, never inventing files.
If the model fails or stalls, the deterministic text is used instead, so the
guide is always complete. Stages stream as ``job.stage`` events so the UI
can show live progress while building.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from clio.config import Limits, get_limits
from clio.events import EVENT_JOB_STAGE, Event, EventBus
from clio.graph import RepoGraph
from clio.llm import LLMClient, LLMMessage

GUIDE_STAGES = ("what", "how", "modules", "run")

README_NAMES = ("README.md", "README.rst", "readme.md", "README")
README_HEAD_CHARS = 2000

ENTRY_NAMES = {"main", "run", "serve", "start", "cli", "server", "app"}
ENTRY_MODULES = {"main", "app", "cli", "server", "__main__"}

BASH_FENCE = re.compile(r"```(?:bash|sh|shell|console)?\s*\n(.*?)```", re.S)
MAKEFILE_TARGET = re.compile(r"^([A-Za-z0-9_.-]+):", re.M)
PACKAGE_SCRIPT = re.compile(r'"([A-Za-z0-9_:-]+)"\s*:\s*"([^"]+)"')
RUN_COMMAND_V2 = re.compile(
    r"^(docker compose|docker-compose|git clone|pip install|pip3 install|"
    r"python3 -m|python -m|npm run|bundle install|bundle exec|go run|go test|"
    r"npx|npm|pnpm|yarn|uv run|poetry run|docker|pip3|pip|python3|python|uv|"
    r"poetry|make|gradlew|mvn|sbt|mix|cargo|rake|uvicorn|gunicorn|flask|"
    r"celery|node|ruby|curl|wget|ssh|cd|source|cp|export|set|touch|mkdir|./)\b",
    re.M,
)
DOCKER_COMPOSE_NAMES = (
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
)


def readme_texts(workspace: Path, max_chars: int = 200_000) -> list[str]:
    """Full texts of README candidates (all README* at root), capped."""
    texts: list[str] = []
    for name in ("README.md", "README.rst", "readme.md", "README.txt", "README"):
        path = workspace / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
            except OSError:
                continue
            texts.append(text)
            break
    return texts


def _fence_commands(text: str) -> list[str]:
    out: list[str] = []
    for block in BASH_FENCE.findall(text):
        for line in block.splitlines():
            line = line.strip()
            if RUN_COMMAND_V2.match(line):
                out.append(line)
    return out


def _package_scripts(pkg: Path) -> list[str]:
    try:
        text = pkg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [f"npm run {name}" for name, _ in PACKAGE_SCRIPT.findall(text)]


def _makefile_targets(mk: Path) -> list[str]:
    try:
        text = mk.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    targets: list[str] = []
    for line in text.splitlines():
        if line and not line.startswith((" ", "\t", ".", "#")) and ":" in line:
            name = line.split(":", 1)[0].strip()
            if name and re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                targets.append(name)
    return [f"make {t}" for t in targets[:4]]


BADGE_RE = re.compile(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)|!\[[^\]]*\]\([^)]*\)")


def strip_readme_noise(text: str) -> str:
    text = BADGE_RE.sub("", text)
    lines = []
    for line in text.splitlines():
        if "shields.io" in line:
            continue
        if line.lstrip().startswith(("<p align", "</p>", "<img")):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def evidence_blocks(blocks: list[tuple[str, str]]) -> str:
    return "\n\n".join(
        f"--- E{i + 1}: {label} ---\n{content}" for i, (label, content) in enumerate(blocks)
    )


ANCHOR_RE = re.compile(r"\[([A-Za-z0-9_./-]+)(?::(\d+))?\]")


def lint_citations(text: str, workspace: Path) -> list[str]:
    bad: list[str] = []
    for path, line_no in ANCHOR_RE.findall(text):
        if not (workspace / path).is_file():
            bad.append(path + (f":{line_no}" if line_no else ""))
    return bad


def load_repo_notes(workspace: Path) -> dict:
    path = workspace / "clio.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, (str, list, dict))}


def repo_memory_text(workspace: Path, max_chars: int = 4000) -> str:
    parts = []
    for name in ("AGENTS.md", "CLAUDE.md", ".windsurfrules"):
        path = workspace / name
        if path.is_file():
            try:
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    return "\n".join(parts)[:max_chars]

STAGE_SYSTEMS: dict[str, str] = {
    "what": (
        "You are writing the opening of a code guide. Summarize what this "
        "project does, based ONLY on the README excerpt and entry points "
        "shown. 2-4 sentences, plain prose, no headings, no file lists."
    ),
    "how": (
        "You are writing the 'how it works' section of a code guide. Explain "
        "the main execution flow using ONLY the entry points and call edges "
        "shown. 3-6 sentences, plain prose, no headings."
    ),
    "modules": (
        "You are writing the 'modules' section of a code guide. Describe the "
        "main modules and what each is responsible for, based only on the "
        "module table and clusters shown. 3-6 sentences, plain prose, no "
        "headings."
    ),
    "run": (
        "You are writing the 'how to run' section of a code guide. Explain "
        "how to run the project using ONLY the commands and scripts shown. "
        "2-4 sentences, plain prose, no headings."
    ),
}


def readme_head(workspace: Path) -> str:
    for name in README_NAMES:
        path = workspace / name
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8", errors="replace")[:README_HEAD_CHARS]
            except OSError:
                return ""
    return ""


def entrypoint_modules(graph: RepoGraph) -> list[str]:
    """Modules with a main/run-style symbol or a main-style module name."""
    found: set[str] = set()
    for sym in graph.symbols:
        if sym.name in ENTRY_NAMES:
            found.add(sym.module)
    for module in graph.modules:
        if module.rsplit(".", 1)[-1] in ENTRY_MODULES:
            found.add(module)
    return sorted(found)


def module_table(graph: RepoGraph, top: int = 12) -> str:
    rows = []
    for module in graph.modules:
        symbols = sum(1 for s in graph.symbols if s.module == module)
        imports = len(graph.imports.get(module, ()))
        rows.append((module, symbols, imports))
    rows.sort(key=lambda r: r[1], reverse=True)
    lines = ["module                    symbols  imports"]
    for module, symbols, imports in rows[:top]:
        lines.append(f"{module:<26}{symbols:>8}{imports:>9}")
    return "\n".join(lines)


def clusters_block(graph: RepoGraph) -> str:
    from clio.clustering import cluster_by_package

    clusters = cluster_by_package(graph)
    if not clusters:
        return "(no clusters detected)"
    return "\n".join(
        f"- {c.name} ({len(c.modules)} modules): {', '.join(c.modules[:8])}"
        for c in clusters
    )


def call_edges_for(graph: RepoGraph, modules: list[str]) -> str:
    """Call edges whose caller lives in the given modules, e.g. app.main -> x."""
    lines: list[str] = []
    for edge in graph.calls:
        caller = edge.caller.split("::", 1)[0]
        if caller in modules:
            callee = edge.callee if "::" in edge.callee else f"<imported> {edge.callee}"
            lines.append(f"{edge.caller} -> {callee} (line {edge.line})")
    return "\n".join(lines[:40])


def run_hints(workspace: Path) -> list[str]:
    hints: list[str] = []
    for text in readme_texts(workspace):
        hints.extend(_fence_commands(text))
    for mk in ("Makefile", "makefile", "GNUmakefile"):
        path = workspace / mk
        if path.is_file():
            hints.extend(_makefile_targets(path))
            break
    pkg = workspace / "package.json"
    if pkg.is_file():
        hints.extend(_package_scripts(pkg))
    for req in sorted(workspace.glob("requirements*.txt")):
        hints.append(f"pip install -r {req.name}")
    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file():
        hints.append("pip install -e .")
    for name in DOCKER_COMPOSE_NAMES:
        if (workspace / name).is_file():
            hints.append("docker compose up")
            break
    for just in ("justfile", "just"):
        if (workspace / just).is_file():
            hints.append("just")
            break
    for pkg2 in sorted(workspace.rglob("package.json")):
        rel = pkg2.relative_to(workspace)
        if len(rel.parts) - 1 <= 1 and pkg2 != pkg:
            hints.extend(_package_scripts(pkg2))
    seen: list[str] = []
    for hint in hints:
        if hint not in seen:
            seen.append(hint)
    return seen[:12]


def _facts(stage: str, graph: RepoGraph, workspace: Path, readme: str,
           entries: list[str], table: str, clusters: str,
           hints: list[str]) -> list[tuple[str, str]]:
    if stage == "what":
        blocks = []
        if readme:
            blocks.append(("README", readme))
        if entries:
            blocks.append(("Entry points", ", ".join(entries)))
        return blocks
    if stage == "how":
        edges = call_edges_for(graph, entries)
        body = "Entry points: " + (", ".join(entries) or "none detected")
        if edges:
            body += f"\n\nCall edges from entry points:\n{edges}"
        return [("Entry points & call edges", body)]
    if stage == "modules":
        from clio.repomap import build_repo_map
        repo_map = build_repo_map(workspace, graph, budget_chars=1400)
        body = f"Repo map (signatures, ranked):\n{repo_map}\n\nModule table:\n{table}\n\nClusters:\n{clusters}"
        return [("Repo map", repo_map), ("Module table", table), ("Clusters", clusters)]
    return [("Run instructions", "\n".join(hints) or "(no run instructions found)")]


def _fallback(stage: str, graph: RepoGraph, readme: str, entries: list[str],
              table: str, hints: list[str]) -> str:
    if stage == "what":
        if readme:
            first = next((l for l in readme.splitlines() if l.strip()), "")
            return first[:300] if first else "This repository has no README."
        top = ", ".join(list(graph.modules)[:5])
        return (
            f"This repository contains {graph.module_count} modules and "
            f"{graph.symbol_count} symbols. Main modules: {top}."
        )
    if stage == "how":
        return "Entry points: " + (", ".join(entries) or "none detected") + "."
    if stage == "modules":
        return table
    return "\n".join(hints) or "(no run instructions found)"


def _emit(bus: EventBus | None, job_id: str | None, data: dict) -> None:
    if bus is not None:
        bus.publish(Event(type=EVENT_JOB_STAGE, job_id=job_id or "", data=data))


async def build_guide(
    workspace: Path,
    graph: RepoGraph,
    client: LLMClient,
    *,
    job_id: str | None = None,
    bus: EventBus | None = None,
    limits: Limits | None = None,
) -> dict:
    """Build the guide; returns {readme, entrypoints, stages: {stage: {text, sources}}}."""
    limits = limits or get_limits()
    raw_readme = "\n\n".join(readme_texts(workspace))
    readme = strip_readme_noise(raw_readme)[:README_HEAD_CHARS]
    entries = entrypoint_modules(graph)
    table = module_table(graph)
    clusters = clusters_block(graph)
    hints = run_hints(workspace)
    notes = load_repo_notes(workspace)
    memory = repo_memory_text(workspace)
    steering = notes.get("repo_notes")
    if not steering and memory:
        steering = memory

    stages: dict[str, dict] = {}
    for stage in GUIDE_STAGES:
        _emit(bus, job_id, {"stage": stage, "status": "started"})
        blocks = _facts(stage, graph, workspace, readme, entries, table, clusters, hints)
        facts = evidence_blocks(blocks)
        sources = [f"{label}.md" if label == "README" else label.lower().replace(" ", "-")
                   for label, _ in blocks]
        if stage == "run" and notes.get("run_commands"):
            hints2 = list(dict.fromkeys(notes["run_commands"] + hints))
            facts = f"--- E1: Run instructions ---\n" + "\n".join(hints2)
        prompt = f"Repository evidence:\n\n{facts}"
        if steering:
            prompt += (
                "\n\nProject note: " + steering[:800] +
                "\nFold this context into your answer when relevant."
            )
        text = None
        if facts:
            messages = [
                LLMMessage(role="system", content=STAGE_SYSTEMS[stage]),
                LLMMessage(role="user", content=prompt),
            ]
            try:
                text = await client.complete(messages, model=limits.cheap_model)
            except Exception:
                text = None
        if not (text and text.strip()):
            text = _fallback(stage, graph, readme, entries, table, hints)
        elif lint_citations(text, workspace):
            text = _fallback(stage, graph, readme, entries, table, hints)
        stages[stage] = {"text": text.strip(), "sources": sources}
        _emit(bus, job_id, {"stage": stage, "status": "done"})
    return {"readme": readme, "entrypoints": entries, "stages": stages}
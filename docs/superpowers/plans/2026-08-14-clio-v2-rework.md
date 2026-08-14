# Clio v2 Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild Clio's retrieval, guide, and chat engines so repo analysis answers are grounded, complete, and verifiable (approved spec: `docs/superpowers/specs/2026-08-14-clio-v2-rework-design.md`).

**Architecture:** Pure-stdlib pipeline — Index-V2 (symbol chunks + doc tier + repo map + RRF fusion), Guide-V2 (evidence bundles + citation lint + full-README run analysis), Chat-V2 (query understanding + compaction + memory bank), plus a git-history eval harness. No embeddings, no agentic loops, one `gemini-2.5-flash` call per completion.

**Tech Stack:** Python stdlib only (`ast`, `re`, `sqlite3`, `http.server`, `urllib`), pytest, SSE events.

## Global Constraints

- Zero third-party dependencies anywhere in `src/`.
- Single process; `http.server` + SSE unchanged. No agentic loops in the product path.
- ONE LLM call per completion (guide stage, chat answer); query-understanding and compaction each add exactly one call on their gated paths.
- Citations always deterministic (from retrieval, never from the model); the model never invents files.
- Guide is always complete: any stage failure falls back to deterministic facts verbatim.
- Windows dev environment; PowerShell 5.1; run tests with `python -m pytest -q` from repo root.
- Existing suite stays green: every task ends with `python -m pytest -q` passing (starting state: 256 tests).
- Repo root: `D:\AI\AIML\SUNRISE COUNTDOWN\ai-craftsman-portfolio\projects\clio`.
- Sandbox copies of FinEdge exist at `sandbox/clio-2cc7c16e/` etc. (12,141-char README, first code fence at char 3,788 — the canonical regression case).

---

### Task 1: Eval harness — gold sets from git history (P0)

**Files:**
- Create: `tests/eval/__init__.py`
- Create: `tests/eval/goldset.py`
- Create: `tests/eval/run_eval.py`
- Create: `tests/eval/test_goldset.py`

**Interfaces:**
- Produces:
  - `build_goldset(repo_path: Path, out_path: Path, max_commits: int = 400) -> list[dict]` — walks `git log` (full history, NOT `--depth 1`), keeps fix-style commits, gold files = changed non-doc files, query = cleaned commit message; writes `goldset.jsonl` lines `{"query": str, "gold_paths": [str], "docs_only": bool}`.
  - `load_goldset(path: Path) -> list[dict]`
  - `metric_mrr(hit_paths: list[str], gold: set[str]) -> float`
  - `metric_recall_at_k(hit_paths: list[str], gold: set[str], k: int) -> float`
  - `metric_budgeted_coverage(hit_paths: list[str], gold: set[str], k: int) -> float` (fraction of gold files among the first k unique files hit)
  - `run_eval(index_factory, goldset: list[dict], top_k: int = 8) -> dict` — for each query, retrieve via `index_factory(query)` returning ordered file paths; aggregates MRR, Recall@1/5/8, budgeted coverage BCY@8, and abstention accuracy (for `docs_only` queries, a correct abstention = returning no code files among top hits; score = 1 if best hit is a doc file or no hits).

- [ ] **Step 1: Write the failing tests**

`tests/eval/test_goldset.py`:

```python
import json
from pathlib import Path

from clio.graph import build_repo_graph
from clio.retrieval import build_retrieval_index
from tests.eval.goldset import (
    build_goldset, metric_mrr, metric_recall_at_k, metric_budgeted_coverage,
)


def test_build_goldset_extracts_fix_commits(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def main():\n    pass\n")
    (repo / "README.md").write_text("# repo\n")
    subprocess = __import__("subprocess")
    for cmd in [
        ["git", "init"], ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ]:
        subprocess.run(cmd, cwd=repo, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
    (repo / "app.py").write_text("def main():\n    return 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fix: return correct value"], cwd=repo, capture_output=True)

    out = tmp_path / "gold.jsonl"
    gs = build_goldset(repo, out)
    assert len(gs) == 1
    assert gs[0]["query"] == "fix: return correct value"
    assert "app.py" in gs[0]["gold_paths"]
    assert gs[0]["docs_only"] is False
    assert json.loads(out.read_text())["query"] == "fix: return correct value"


def test_metrics():
    gold = {"src/a.py", "src/b.py"}
    assert metric_mrr(["src/a.py", "x.py"], gold) == 1.0
    assert metric_mrr(["x.py", "src/a.py"], gold) == 0.5
    assert metric_recall_at_k(["src/a.py"], gold, 8) == 0.5
    assert metric_budgeted_coverage(["x.py", "src/a.py", "src/b.py"], gold, 8) == 1.0
    assert metric_budgeted_coverage(["x.py", "src/a.py"], gold, 8) == 0.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/eval/test_goldset.py -q`
Expected: FAIL with `ModuleNotFoundError: tests.eval.goldset`

- [ ] **Step 3: Implement `tests/eval/goldset.py`**

```python
"""Gold-set extraction from git history: fix commits -> gold files."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

FIX_WORDS = re.compile(
    r"\b(fix|fixes|fixed|bug|bugfix|bug fix|hotfix|close|closes|closed|"
    r"resolve|resolves|resolved|repair|correct|patch|error|crash)\b",
    re.I,
)
DOC_SUFFIXES = (".md", ".rst", ".txt", "license", "contributing", "dockerfile")
_IGNORE_DIRS = {".git", "node_modules", "dist", "build", "__pycache__", ".venv"}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    return proc.stdout.strip()


def _is_doc_file(rel: str) -> bool:
    low = rel.lower()
    return low.endswith(DOC_SUFFIXES) or "docs/" in low


def _clean_query(msg: str) -> str:
    return re.sub(r"\s+", " ", msg).strip()[:300]


def build_goldset(repo_path: Path, out_path: Path, max_commits: int = 400) -> list[dict]:
    """Walk git log; fix commits become {query, gold_paths, docs_only}."""
    repo_path = Path(repo_path)
    _git(repo_path, "rev-parse", "--is-inside-work-tree")  # raise if not a repo
    rows: list[dict] = []
    for subject, body, files_raw in zip(
        _git(repo_path, "log", f"--max-count={max_commits}", "--format=%s").splitlines(),
        _git(repo_path, "log", f"--max-count={max_commits}", "--format=%b").split("\n\n"),
        _git(repo_path, "log", f"--max-count={max_commits}", "--name-only", "--format=").splitlines(),
    ):
        if not subject or not FIX_WORDS.search(subject):
            continue
        files = [f for f in files_raw if f.strip()]
        files = [f.replace("/", "/") for f in files if not f.startswith(_IGNORE_DIRS)]
        code_files = [f for f in files if not _is_doc_file(f)]
        if not files:
            continue
        rows.append({
            "query": _clean_query(f"{subject} {body}".strip()),
            "gold_paths": code_files or files,
            "docs_only": not code_files,
        })
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return rows


def load_goldset(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def metric_mrr(hit_paths: list[str], gold: set[str]) -> float:
    for i, p in enumerate(hit_paths):
        if p in gold:
            return 1.0 / (i + 1)
    return 0.0


def metric_recall_at_k(hit_paths: list[str], gold: set[str], k: int) -> float:
    if not gold:
        return 0.0
    return sum(1 for p in hit_paths[:k] if p in gold) / len(gold)


def metric_budgeted_coverage(hit_paths: list[str], gold: set[str], k: int) -> float:
    if not gold:
        return 0.0
    return sum(1 for p in hit_paths[:k] if p in gold) / len(gold)


def run_eval(index_factory, goldset: list[dict], top_k: int = 8) -> dict:
    """index_factory(query) -> ordered list of file paths (posix rel paths)."""
    mrrs, r1, r5, r8, bcys, abstain_ok, abstain_total = [], [], [], [], [], [], 0
    for row in goldset:
        hits = index_factory(row["query"])[:top_k]
        gold = set(row["gold_paths"])
        mrrs.append(metric_mrr(hits, gold))
        r1.append(metric_recall_at_k(hits, gold, 1))
        r5.append(metric_recall_at_k(hits, gold, 5))
        r8.append(metric_recall_at_k(hits, gold, 8))
        bcys.append(metric_budgeted_coverage(hits, gold, 8))
        if row["docs_only"]:
            abstain_total += 1
            abstain_ok += 0 if any(p in gold for p in hits[:1]) else 1
    n = max(len(goldset), 1)
    return {
        "queries": len(goldset),
        "mrr": sum(mrrs) / n,
        "recall@1": sum(r1) / n,
        "recall@5": sum(r5) / n,
        "recall@8": sum(r8) / n,
        "bcy@8": sum(bcys) / n,
        "abstain_acc": (sum(abstain_ok) / abstain_total) if abstain_total else None,
    }
```

- [ ] **Step 4: Implement `tests/eval/run_eval.py`**

```python
"""CLI: python -m tests.eval.run_eval <repo-or-goldset> [goldset.jsonl]"""
import sys
from pathlib import Path

from clio.graph import build_repo_graph
from clio.retrieval import RetrievalIndex, build_retrieval_index
from tests.eval.goldset import build_goldset, load_goldset, run_eval


def make_index_factory(workspace: Path):
    graph = build_repo_graph(workspace)
    index: RetrievalIndex = build_retrieval_index(workspace, graph)

    def factory(query: str) -> list[str]:
        hits = index.search(query, top_k=8)
        return [h.chunk.path for h in hits]

    return factory


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m tests.eval.run_eval <repo> [out-goldset.jsonl]")
        return 1
    repo = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else repo / "goldset.jsonl"
    goldset = build_goldset(repo, out) if out.parent == repo else load_goldset(out)
    results = run_eval(make_index_factory(repo), goldset)
    for k, v in results.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/eval/test_goldset.py -q`
Expected: PASS (2 tests)

- [ ] **Step 6: Baseline run (P0)**

Run: `python -m tests.eval.run_eval "D:\AI\AIML\SUNRISE COUNTDOWN\ai-craftsman-portfolio\projects\clio"`
Run: `python -m tests.eval.run_eval sandbox\clio-2cc7c16e` (FinEdge has full history — verify `git log` works there; if the sandbox copy is shallow, run `git fetch --unshallow` in it first)
Expected: prints metric table; record the numbers in the Task 11 verification notes (these are the P0 baseline).

- [ ] **Step 7: Commit**

```bash
git add tests/eval
git commit -m "test: add git-history eval harness (gold sets, MRR/Recall/BCY)"
```

---

### Task 2: run_hints v2 — full README + widened commands + nested configs (P1)

**Files:**
- Modify: `src/clio/guide.py` (replace `BASH_FENCE`-based `run_hints`, add helpers)
- Test: `tests/test_guide.py`

**Interfaces:**
- Consumes: `RepoGraph` (unchanged), `Path` workspace.
- Produces: `run_hints(workspace: Path) -> list[str]` (new signature — no longer takes truncated `readme`), `readme_texts(workspace: Path, max_chars: int = 200_000) -> list[str]` (full README candidates), `RUN_COMMAND_V2` regex module constant.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_guide.py`)

```python
def test_run_hints_finds_commands_past_2000_chars(tmp_path):
    readme = tmp_path / "README.md"
    head = "# Repo\n" + ("<img src='https://img.shields.io/badge/x'/>\n" * 80)
    readme.write_text(head + "## Run\n\n```bash\nuvicorn backend.main:app --reload --port 8000\n```\n", encoding="utf-8")
    hints = run_hints(tmp_path)
    assert any("uvicorn" in h for h in hints)


def test_run_hints_plain_fence_and_git_cd(tmp_path):
    (tmp_path / "README.md").write_text(
        "```\ngit clone https://github.com/x/y.git\ncd y\nsource venv/bin/activate\npip install -r requirements.txt\n```\n",
        encoding="utf-8",
    )
    hints = run_hints(tmp_path)
    for cmd in ("git clone", "cd y", "source venv", "pip install"):
        assert any(h.startswith(cmd) for h in hints)


def test_run_hints_nested_package_json(tmp_path):
    (tmp_path / "frontend" / "package.json").write_text(
        '{"scripts": {"dev": "vite", "build": "tsc && vite build"}}', encoding="utf-8"
    )
    hints = run_hints(tmp_path)
    assert any(h == "npm run dev" for h in hints)


def test_run_hints_docker_compose(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services:\n  web:\n    build: .\n", encoding="utf-8")
    hints = run_hints(tmp_path)
    assert any("docker compose up" in h for h in hints)


def test_run_hints_requirements_txt(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
    hints = run_hints(tmp_path)
    assert any(h.startswith("pip install -r requirements.txt") for h in hints)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_guide.py -q`
Expected: the new tests FAIL (old `run_hints` finds nothing for fence past 2K chars; nested package.json missed; `git`/`cd`/`source` not matched).

- [ ] **Step 3: Implement run_hints v2 in `src/clio/guide.py`**

Replace the `run_hints` function and add helpers:

```python
RUN_COMMAND_V2 = re.compile(
    r"^(docker compose|docker-compose|git clone|pip install|pip3 install|"
    r"python3 -m|python -m|npm run|bundle install|bundle exec|go run|go test|"
    r"npx|npm|pnpm|yarn|uv run|poetry run|docker|pip3|pip|python3|python|uv|"
    r"poetry|make|gradlew|mvn|sbt|mix|cargo|rake|uvicorn|gunicorn|flask|"
    r"celery|node|ruby|curl|wget|ssh|cd|source|cp|export|set|touch|mkdir|./)\b",
    re.M,
)
DOCKER_COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml",
                        "compose.yml", "compose.yaml")


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
    scripts = PACKAGE_SCRIPT.findall(text)
    return [f"npm run {name}" for name, _ in scripts]


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
    for depth in range(2):
        for pkg2 in sorted(workspace.rglob("package.json")):
            rel = pkg2.relative_to(workspace)
            if len(rel.parts) - 1 <= depth and pkg2 != pkg:
                hints.extend(_package_scripts(pkg2))
    seen: list[str] = []
    for hint in hints:
        if hint not in seen:
            seen.append(hint)
    return seen[:12]
```

- [ ] **Step 4: Update `guide.py` call site**

In `build_guide`, replace:
```python
hints = run_hints(workspace, readme)
```
with:
```python
hints = run_hints(workspace)
```
and replace `_facts("run", ...)` fallback so the run stage facts show the hints (existing behavior — `"\n".join(hints) or "(no run instructions found)"` — stays correct).

- [ ] **Step 5: Run the guide tests**

Run: `python -m pytest tests/test_guide.py -q`
Expected: PASS (new + existing).

- [ ] **Step 6: Regression check on the FinEdge sandbox**

Run:
```python
python -c "from pathlib import Path; from clio.guide import run_hints; print(run_hints(Path('sandbox/clio-2cc7c16e')))"
```
Expected: output includes `npm run dev`, `npm run build`, `pip install -r requirements.txt`, `docker ...`, `uvicorn ...` style hints (previously empty).

- [ ] **Step 7: Full suite**

Run: `python -m pytest -q`
Expected: PASS (≥262 tests).

- [ ] **Step 8: Commit**

```bash
git add src/clio/guide.py tests/test_guide.py
git commit -m "feat: run_hints v2 — full README, wider commands, nested configs"
```

---

### Task 3: Guide evidence bundles + citation lint + what-stage README cleanup + steering (P1)

**Files:**
- Modify: `src/clio/guide.py`
- Test: `tests/test_guide.py`

**Interfaces:**
- Consumes: `readme_texts` (Task 2), `RepoGraph`.
- Produces:
  - `strip_readme_noise(text: str) -> str` — removes `shields.io`/badge HTML lines, `[![..](..)](..)` badges.
  - `evidence_blocks(blocks: list[tuple[str, str]]) -> str` — renders `--- E{n}: label ---\ncontent` blocks.
  - `lint_citations(text: str, workspace: Path) -> list[str]` — returns invalid anchors (path not on disk); `[]` when clean.
  - `load_repo_notes(workspace: Path) -> dict` — reads optional `clio.json` (`repo_notes`, `run_commands`).
  - `repo_memory_text(workspace: Path, max_chars: int = 4000) -> str` — concatenated `CLAUDE.md`/`AGENTS.md` at root (capped).

- [ ] **Step 1: Write the failing tests**

```python
def test_strip_readme_noise():
    noisy = "[![PyPI](https://img.shields.io/pypi/v/x.svg)](https://pypi.org/x)\n"
    noisy += "<p align=\"center\"><img src=\"https://img.shields.io/badge/Python-3.11-3776AB\"/></p>\n"
    noisy += "# Real title\nActual description here.\n"
    cleaned = strip_readme_noise(noisy)
    assert "shields.io" not in cleaned
    assert "# Real title" in cleaned and "Actual description here" in cleaned


def test_evidence_blocks_numbered():
    blocks = [("README", "line one"), ("Entry points", "app.main")]
    text = evidence_blocks(blocks)
    assert "--- E1: README ---" in text and "--- E2: Entry points ---" in text


def test_lint_citations(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    assert lint_citations("see [app.py:1] and [missing.py:3]", tmp_path) == ["missing.py:3"]
    assert lint_citations("no anchors here", tmp_path) == []


def test_repo_notes_steering(tmp_path):
    (tmp_path / "clio.json").write_text(
        '{"repo_notes": "This is a research project.", "run_commands": ["make all"]}',
        encoding="utf-8",
    )
    notes = load_repo_notes(tmp_path)
    assert notes["repo_notes"] == "This is a research project."
    assert notes["run_commands"] == ["make all"]


def test_repo_memory_text(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Run tests with pytest.\n", encoding="utf-8")
    assert "pytest" in repo_memory_text(tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_guide.py -q`
Expected: FAIL (`strip_readme_noise` etc. undefined).

- [ ] **Step 3: Implement the helpers in `src/clio/guide.py`**

```python
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
    for path, _line in ANCHOR_RE.findall(text):
        if not (workspace / path).is_file():
            bad.append(path + (f":{_line}" if _line else ""))
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
```

Add `import json` at the top of `guide.py`.

- [ ] **Step 4: Rewire `_facts` + `build_guide` for evidence bundles and steering**

In `build_guide`:

```python
readme = "\n\n".join(readme_texts(workspace))
readme = strip_readme_noise(readme)
notes = load_repo_notes(workspace)
memory = repo_memory_text(workspace)
```

In `_facts("what", ...)`:

```python
if stage == "what":
    blocks = [("README", readme[:6000] or "(no README found)")]
    if entries:
        blocks.append(("Entry points", ", ".join(entries)))
    if memory:
        blocks.append(("Repo memory (AGENTS.md/CLAUDE.md)", memory))
    if notes.get("repo_notes"):
        blocks.insert(0, ("Authoritative notes (clio.json)", str(notes["repo_notes"])))
    return evidence_blocks(blocks), ["README.md"] if readme else [f"{e}.py" for e in entries[:3]]
```

In `_facts("run", ...)`:

```python
if stage == "run":
    cmds = list(hints)
    extra = notes.get("run_commands")
    if isinstance(extra, list):
        cmds = [str(c) for c in extra] + cmds
    return "\n".join(cmds) or "(no run instructions found)", []
```

- [ ] **Step 5: Add citation lint to the stage loop**

In `build_guide`, after the completion succeeds:

```python
bad = lint_citations(text, workspace) if text else []
if bad:
    text = None  # refuse fabricated anchors; fall back to deterministic facts
```

- [ ] **Step 6: Run the guide tests**

Run: `python -m pytest tests/test_guide.py -q`
Expected: PASS.

- [ ] **Step 7: Full suite**

Run: `python -m pytest -q`
Expected: PASS (check that `test_cli.py` summary assertions still match — the `what` facts changed shape; update those assertions if they assert on the exact `facts` string: `test_cli.py` asserts `"fake guide text"` in report summary, which is unaffected).

- [ ] **Step 8: Commit**

```bash
git add src/clio/guide.py tests/test_guide.py
git commit -m "feat: guide evidence bundles, citation lint, README noise cleanup, clio.json steering"
```

---

### Task 4: Doc tier in retrieval + top-level README boost (P1)

**Files:**
- Modify: `src/clio/retrieval.py`
- Test: `tests/test_retrieval.py`

**Interfaces:**
- Consumes: unchanged `build_retrieval_index(workspace, graph)`.
- Produces: `Chunk.is_doc: bool`, `Chunk.is_readme_top: bool`, constants `DOC_BONUS = 5.0`, `README_TOP_BONUS = 8.0`; `search()` adds these bonuses and never returns doc chunks as symbol matches.

- [ ] **Step 1: Write the failing tests**

```python
def test_doc_tier_boosts_readme_for_overview_questions(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "README.md").write_text(
        "# Shop\nThis project is an online shop with payments and inventory.\n" * 5,
        encoding="utf-8",
    )
    (root / "app.py").write_text(
        "def main():\n    process_payments()\n    update_inventory()\n\n"
        "def process_payments():\n    pass\n\n"
        "def update_inventory():\n    pass\n",
        encoding="utf-8",
    )
    graph = build_repo_graph(root)
    index = build_retrieval_index(root, graph)
    hits = index.search("what does the shop do", top_k=3)
    assert hits and hits[0].chunk.path == "README.md"


def test_doc_chunks_never_symbol_match(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "README.md").write_text("words about the parser here\n", encoding="utf-8")
    (root / "parser.py").write_text("def parse(text):\n    return text\n", encoding="utf-8")
    graph = build_repo_graph(root)
    index = build_retrieval_index(root, graph)
    hits = index.search("where is parse defined", top_k=3)
    assert hits[0].chunk.path == "parser.py"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_retrieval.py -q`
Expected: new tests FAIL (README chunk not boosted; symbol match only hits parser but BM25 ties may surface README — the first test asserts README first).

- [ ] **Step 3: Implement doc tier in `src/clio/retrieval.py`**

```python
DOC_BONUS = 5.0
README_TOP_BONUS = 8.0
DOC_PATHS = ("README", "readme", "docs/", "AGENTS.md", "CLAUDE.md", ".windsurfrules")
README_TOP_LINES = 120


def _is_doc(path: str) -> bool:
    return any(path.startswith(p) or path == p for p in DOC_PATHS) or "/docs/" in f"/{path}"
```

In `Chunk`, add fields:

```python
is_doc: bool = False
is_readme_top: bool = False
```

In `build_retrieval_index`, when creating a chunk:

```python
is_doc = _is_doc(rel)
is_readme_top = is_doc and rel.startswith(("README", "readme")) and start <= README_TOP_LINES
chunks.append(Chunk(
    path=rel, start=start, end=end, text=text,
    terms=tokenize(text), module=module, symbols=symbols,
    is_doc=is_doc, is_readme_top=is_readme_top,
))
```

In `search()`, after the symbol/module/path bonus loop:

```python
for i, chunk in enumerate(self.chunks):
    if chunk.is_readme_top:
        scores[i] += README_TOP_BONUS
        reasons[i].append("top-level README intro")
    elif chunk.is_doc:
        scores[i] += DOC_BONUS
        reasons[i].append("documentation")
```

In the symbol-match loop, skip doc chunks (definitions must come from code):

```python
for i in self._sym_terms.get(term, ()):
    if self.chunks[i].is_doc:
        continue
    scores[i] += SYMBOL_BONUS
```

- [ ] **Step 4: Run the retrieval tests**

Run: `python -m pytest tests/test_retrieval.py -q`
Expected: PASS (existing tests unchanged semantics; new tests pass).

- [ ] **Step 5: Full suite**

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/clio/retrieval.py tests/test_retrieval.py
git commit -m "feat: doc tier ranking with top-level README boost"
```

---

### Task 5: Chat overview routing + repo-level context + anti-dodge prompt (P1)

**Files:**
- Modify: `src/clio/ask.py`
- Test: `tests/test_ask.py`

**Interfaces:**
- Consumes: `RetrievalIndex.search`, `module_table(graph)`, `entrypoint_modules(graph)` from `guide.py`.
- Produces:
  - `intent_of(question: str) -> str` — `"overview" | "specific"`.
  - `repo_context_block(graph, readme_head: str, budget_chars: int = 1500) -> str` — module table + entry points + README first line.
  - `OVERVIEW_SYSTEM_PROMPT`, `SPECIFIC_SYSTEM_PROMPT` constants; `_prompt()` uses the right one; `ChatSession` gains `self.graph` and `self.readme`.

- [ ] **Step 1: Write the failing tests**

```python
def test_intent_overview():
    assert intent_of("what does this project do?") == "overview"
    assert intent_of("give me an overview of the codebase") == "overview"
    assert intent_of("how does the store persist data?") == "specific"
    assert intent_of("who calls parse?") == "specific"


def test_overview_question_gets_repo_context(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "README.md").write_text("# Shop\nAn online shop.\n", encoding="utf-8")
    (root / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    graph = build_repo_graph(root)
    client = FakeLLM(handler=fake_handler(get_limits()))
    session = ChatSession("job", tmp_path, client)
    session._index = build_retrieval_index(root, graph)
    session.graph = graph
    session.readme = "An online shop."
    result = asyncio.run(session.answer("what does this project do?"))
    assert result["ok"]
    assert "app.py" in str(result["sources"])


def test_specific_question_keeps_grounding_prompt():
    # the SPECIFIC prompt must contain the never-invent instruction
    assert "never invent" in SPECIFIC_SYSTEM_PROMPT
    assert "synthesize" in OVERVIEW_SYSTEM_PROMPT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ask.py -q`
Expected: FAIL (`intent_of` etc. undefined).

- [ ] **Step 3: Implement in `src/clio/ask.py`**

```python
OVERVIEW_WORDS = (
    "overview", "about", "purpose", "summary", "summarize", "introduce",
    "explain this", "what is this", "what does this project", "what kind",
    "what the project", "describe this",
)

OVERVIEW_SYSTEM_PROMPT = (
    "You are Clio, an expert code analyst. Below are excerpts from a "
    "repository plus a repo map (module table, entry points, README). "
    "Answer the user's overview question by SYNTHESIZING from ALL the "
    "evidence — combine the README, module table, and excerpts into a "
    "clear summary. Cite sources inline as [path:line] where you use "
    "them. Do not refuse or say 'the excerpts do not state...'; if "
    "something is genuinely unknown, say so briefly and keep going. "
    "Never invent files, modules, or behavior."
)

SPECIFIC_SYSTEM_PROMPT = (
    "You are Clio, an expert code analyst. Below are excerpts from a "
    "repository, each marked with a header like --- path:start-end ---. "
    "Answer the user's question using ONLY the provided excerpts. Cite "
    "sources inline as [path:line]. If the excerpts do not contain the "
    "answer, say exactly what is missing and where it might be found. Be "
    "concise and concrete; never invent APIs, files, or behavior."
)


def intent_of(question: str) -> str:
    low = question.strip().lower()
    return "overview" if any(w in low for w in OVERVIEW_WORDS) else "specific"


def repo_context_block(graph, readme_head: str, budget_chars: int = 1500) -> str:
    from clio.guide import entrypoint_modules, module_table
    parts = []
    if readme_head.strip():
        parts.append("README (first lines): " + readme_head.strip().splitlines()[0][:200])
    entries = entrypoint_modules(graph)
    if entries:
        parts.append("Entry points: " + ", ".join(entries))
    table = module_table(graph, top=8)
    if table.strip():
        parts.append("Module table:\n" + table)
    block = "\n\n".join(parts)
    return block[:budget_chars]
```

Update `ChatSession.__init__` to accept and store `graph: RepoGraph | None = None`, `readme: str = ""`. In `answer()`, after hits are computed:

```python
intent = intent_of(question)
messages = self._prompt(question, hits, intent, index)
```

and rewrite `_prompt` to accept `intent`:

```python
def _prompt(self, question, hits, intent, index):
    excerpts = pack_hits(hits, CHUNK_BUDGET_CHARS)
    system = OVERVIEW_SYSTEM_PROMPT if intent == "overview" else SPECIFIC_SYSTEM_PROMPT
    parts = [f"Code excerpts from the repository:\n\n{excerpts}"]
    if intent == "overview" and self.graph is not None:
        context = repo_context_block(self.graph, self.readme)
        if context:
            parts.insert(0, f"[Repository overview]\n{context}")
    prior = _history_block(self.history)
    if prior:
        parts.append(f"[Prior conversation]\n{prior}")
    parts.append(f"Question: {question}")
    return [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content="\n\n".join(parts)),
    ]
```

Update `web.py` so `ChatSession` is constructed with `graph` and `readme` from the persisted job (see Task 11 for the wiring; for this task, keep `web.py` unchanged — the defaults `None`/`""` keep it working; Task 11 wires the persisted values).

- [ ] **Step 4: Run the ask tests**

Run: `python -m pytest tests/test_ask.py -q`
Expected: PASS.

- [ ] **Step 5: Full suite**

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/clio/ask.py tests/test_ask.py
git commit -m "feat: chat overview intent routing with repo-level context"
```

---

### Task 6: Index-V2 — symbol chunks, headers, skeleton chunks (P2)

**Files:**
- Modify: `src/clio/graph.py` (`Symbol` gains `end_line`; Python visitor records `end_lineno`)
- Modify: `src/clio/retrieval.py` (chunking rewrite)
- Test: `tests/test_retrieval.py`

**Interfaces:**
- Consumes: `build_repo_graph` (unchanged call shape).
- Produces:
  - `Symbol.end_line: int = 0` (0 = unknown for regex-tier languages).
  - `symbol_chunk_plan(lines: list[str], ranges: list[tuple[int, int, str]]) -> list[tuple[int, int]]` — merges symbol ranges with gaps; oversized ranges split at statement boundaries (blank lines).
  - `chunk_header(path: str, module: str, name: str, signature: str) -> str` — `# path`, `# module::name`, signature line.
  - `Chunk.header: str`, `Chunk.fqn: str` fields; `build_retrieval_index` produces symbol chunks with headers and skeleton chunks (`is_skeleton: bool`).

- [ ] **Step 1: Write the failing tests**

```python
def test_symbol_end_line_recorded(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "app.py").write_text(
        "def first():\n    return 1\n\n\ndef second():\n    return 2\n", encoding="utf-8"
    )
    graph = build_repo_graph(root)
    first = next(s for s in graph.symbols if s.name == "first")
    second = next(s for s in graph.symbols if s.name == "second")
    assert first.end_line == 2
    assert second.end_line == 5


def test_symbol_chunks_do_not_split_functions(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    src = "\n".join(
        [f"def fn{i}():\n    return {i}\n" for i in range(20)]
    )  # 60 lines, one 150-line chunk before; now one chunk per symbol
    (root / "app.py").write_text(src, encoding="utf-8")
    graph = build_repo_graph(root)
    index = build_retrieval_index(root, graph)
    chunks = [c for c in index.chunks if c.path == "app.py"]
    assert len(chunks) >= 20
    assert all(not c.is_skeleton for c in chunks if not c.is_skeleton)


def test_chunk_header_contains_fqn_and_signature(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "app.py").write_text(
        "class Greeter:\n    def greet(self, name):\n        return f'hi {name}'\n",
        encoding="utf-8",
    )
    graph = build_repo_graph(root)
    index = build_retrieval_index(root, graph)
    method_chunk = next(
        c for c in index.chunks if c.fqn and c.fqn.endswith("Greeter.greet")
    )
    assert "# app.py" in method_chunk.header
    assert "greet(self, name)" in method_chunk.header
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_retrieval.py -q`
Expected: FAIL (`end_line` missing on Symbol; chunk count < 20).

- [ ] **Step 3: Add `end_line` to `src/clio/graph.py`**

```python
@dataclass
class Symbol:
    name: str
    kind: str
    module: str
    line: int
    end_line: int = 0
```

In `_ModuleVisitor`, wherever symbols are appended with `node.lineno`, append `end_lineno=getattr(node, "end_lineno", node.lineno) or node.lineno`. Check the visitor code (graph.py:122-226) and update each `Symbol(...)` construction inside `visit_FunctionDef`/`visit_AsyncFunctionDef`/`visit_ClassDef` accordingly. For the regex extractors (`extractors.py`), `end_line` stays 0.

- [ ] **Step 4: Rewrite chunking in `src/clio/retrieval.py`**

Add fields to `Chunk`: `header: str = ""`, `fqn: str = ""`, `is_skeleton: bool = False`.

Add helper functions:

```python
def _signature_line(lines: list[str], line: int) -> str:
    """The def/class line plus continuation while parens are unbalanced (cap 3 lines)."""
    start = line - 1
    if start < 0 or start >= len(lines):
        return ""
    text = lines[start]
    if "(" in text and text.count("(") > text.count(")"):
        for extra in range(1, 3):
            if start + extra < len(lines):
                text += " " + lines[start + extra].strip()
                if text.count("(") <= text.count(")"):
                    break
    return text.strip()


def symbol_chunk_plan(lines: list[str], ranges: list[tuple[int, int, str]]) -> list[tuple[int, int]]:
    """Symbol ranges -> (start, end) line windows; gaps become their own windows."""
    plan: list[tuple[int, int]] = []
    cursor = 1
    for start, end, _name in sorted(ranges, key=lambda r: (r[0], r[1])):
        if start > cursor:
            plan.append((cursor, start - 1))
        plan.append((start, end))
        cursor = end + 1
    if cursor <= len(lines):
        plan.append((cursor, len(lines)))
    # split oversized windows at blank lines (cap CHUNK_LINES)
    split: list[tuple[int, int]] = []
    for start, end in plan:
        while end - start + 1 > CHUNK_LINES:
            cut = end - CHUNK_LINES + 1
            for i in range(end, start - 1, -1):
                if i > start and not lines[i - 1].strip():
                    cut = i
                    break
            split.append((start, cut - 1))
            start = cut
        split.append((start, end))
    return split
```

In `build_retrieval_index`, replace the line-window loop with the symbol plan:

```python
sym_ranges = [(s.line, s.end_line or s.line, f"{s.module}::{s.name}", s.name)
              for s in syms if s.line > 0]
ranges = [(ln, en, fqn) for ln, en, fqn, _name in sym_ranges]
plan = symbol_chunk_plan(lines, [(ln, en, fqn) for ln, en, fqn in ranges])
fqn_by_line = {ln: (fqn, name) for ln, en, fqn, name in sym_ranges}
for start, end in plan:
    window = lines[start - 1:end]
    text = "\n".join(window)
    fqn, name = fqn_by_line.get(start, ("", ""))
    header = chunk_header(rel, module, name, _signature_line(lines, start)) if fqn else ""
    symbols = [sid for sid, line in syms if start <= line <= end]
    chunks.append(Chunk(
        path=rel, start=start, end=end, text=text,
        terms=tokenize(text), module=module, symbols=symbols,
        is_doc=is_doc, is_readme_top=is_readme_top,
        header=header, fqn=fqn,
    ))
```

Skeleton chunk per file (signatures with bodies elided), appended last:

```python
sig_lines = []
for ln, _en, fqn, name in sym_ranges:
    sig = _signature_line(lines, ln)
    if sig:
        sig_lines.append(f"# {fqn}\n{sig}")
if sig_lines:
    skeleton = "\n\n".join(sig_lines)
    chunks.append(Chunk(
        path=rel, start=0, end=0, text=skeleton,
        terms=tokenize(skeleton), module=module, symbols=[],
        is_doc=is_doc, is_skeleton=True,
        header=f"# {rel} (skeleton — signatures only)", fqn="",
    ))
```

and:

```python
def chunk_header(path: str, module: str, name: str, signature: str) -> str:
    parts = [f"# {path}"]
    if name:
        parts.append(f"# {module}::{name}" if module else f"# {name}")
    if signature:
        parts.append(f"# {signature}")
    return "\n".join(parts)
```

**Important:** `RetrievalIndex.search()` currently has a `seen_files` dedupe (one chunk per file) — with skeleton chunks this would hide symbol chunks when a skeleton ranks first. Fix: dedupe by `fqn` first, then by file, and skip `is_skeleton` chunks in the file-level dedupe pass (they only win when nothing else in the file matched). Also `pack_hits` should render `hit.chunk.header + "\n" + hit.chunk.text` when header is non-empty.

- [ ] **Step 5: Update `pack_hits` to include headers**

```python
block = head + (hit.chunk.header + "\n" if hit.chunk.header else "") + hit.chunk.text
```

- [ ] **Step 6: Run the retrieval tests**

Run: `python -m pytest tests/test_retrieval.py -q`
Expected: PASS — existing tests may need expectation updates if they assert chunk counts or exact texts; update them to the new reality (symbol chunks are *more* numerous; one-per-file dedupe unchanged).

- [ ] **Step 7: Full suite**

Run: `python -m pytest -q`
Expected: PASS (watch `test_ask.py` — pack_hits format changed slightly; assertions on `snippet` fields are unaffected).

- [ ] **Step 8: Commit**

```bash
git add src/clio/graph.py src/clio/retrieval.py tests/test_retrieval.py
git commit -m "feat: symbol-granular chunks with headers and skeleton chunks"
```

---

### Task 7: Repo map — personalized PageRank + budget fit (P2)

**Files:**
- Create: `src/clio/repomap.py`
- Test: `tests/test_repomap.py`

**Interfaces:**
- Consumes: `RepoGraph`, `Symbol.end_line` (Task 6).
- Produces:
  - `file_reference_graph(graph: RepoGraph) -> dict[str, set[str]]` — caller module → set of callee modules (resolve callee prefix to a local module via `graph.modules`).
  - `personalized_pagerank(edges: dict[str, set[str]], personal: dict[str, float], alpha: float = 0.85, iters: int = 30) -> dict[str, float]`
  - `render_repo_map(graph: RepoGraph, scores: dict[str, float], workspace: Path, top: int = 60, budget_chars: int = 1500) -> str`
  - `build_repo_map(workspace: Path, graph: RepoGraph, query: str = "", budget_chars: int = 1500) -> str` — convenience wrapper used by guide + chat.

- [ ] **Step 1: Write the failing tests**

```python
import textwrap

from clio.graph import build_repo_graph
from clio.repomap import (
    build_repo_map, file_reference_graph, personalized_pagerank,
)


def test_reference_graph_from_imports_and_calls(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "a.py").write_text("import b\n\ndef run():\n    b.helper()\n", encoding="utf-8")
    (root / "b.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    graph = build_repo_graph(root)
    edges = file_reference_graph(graph)
    assert "b" in edges.get("a", set()) or "a" in edges.get("b", set())


def test_pagerank_prefers_central_module():
    edges = {"hub": {"a", "b", "c"}, "a": {"hub"}, "b": {"hub"}, "c": {"hub"}}
    scores = personalized_pagerank(edges, personal={"hub": 1.0})
    assert scores["hub"] > scores["a"] > 0


def test_repo_map_contains_signatures(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "app.py").write_text(
        "def greet(name):\n    return f'hi {name}'\n\nclass Greeter:\n"
        "    def hello(self):\n        return 'yo'\n",
        encoding="utf-8",
    )
    graph = build_repo_graph(root)
    text = build_repo_map(root, graph)
    assert "def greet(name)" in text
    assert "app" in text


def test_repo_map_budget_fit(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "app.py").write_text("\n".join(f"def fn{i}():\n    return {i}\n" for i in range(80)),
                                encoding="utf-8")
    graph = build_repo_graph(root)
    text = build_repo_map(root, graph, budget_chars=500)
    assert len(text) <= 600
    assert text  # non-empty
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_repomap.py -q`
Expected: FAIL (`ModuleNotFoundError: clio.repomap`).

- [ ] **Step 3: Implement `src/clio/repomap.py`**

```python
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
    for sym in symbols[:top * 3]:
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
```

- [ ] **Step 4: Run the repomap tests**

Run: `python -m pytest tests/test_repomap.py -q`
Expected: PASS.

- [ ] **Step 5: Wire the map into the `modules` guide stage and chat**

In `guide.py::_facts("modules", ...)`, prepend the map:

```python
if stage == "modules":
    from clio.repomap import build_repo_map
    repo_map = build_repo_map(workspace, graph, budget_chars=1400)
    body = f"Repo map (signatures, ranked):\n{repo_map}\n\nModule table:\n{table}\n\nClusters:\n{clusters}"
    return body, []
```

(`_facts` needs the `workspace` parameter — thread it through `build_guide`.)

In `ask.py::repo_context_block`, include the map when it fits:

```python
from clio.repomap import build_repo_map
repo_map = build_repo_map(Path(graph.root), graph, budget_chars=700)
parts.append("Repo map:\n" + repo_map)
```

- [ ] **Step 6: Run guide + ask + repomap tests**

Run: `python -m pytest tests/test_guide.py tests/test_ask.py tests/test_repomap.py -q`
Expected: PASS.

- [ ] **Step 7: Full suite**

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/clio/repomap.py tests/test_repomap.py src/clio/guide.py src/clio/ask.py
git commit -m "feat: repo map with personalized PageRank"
```

---

### Task 8: RRF fusion of retrieval signals (P2)

**Files:**
- Modify: `src/clio/retrieval.py`
- Test: `tests/test_retrieval.py`

**Interfaces:**
- Produces: `rrf_merge(ranked_lists: list[list[int]], k: int = 60) -> list[int]`; `RetrievalIndex.search()` refactored to run sub-retrievers and fuse.

- [ ] **Step 1: Write the failing tests**

```python
def test_rrf_merges_ranked_lists():
    from clio.retrieval import rrf_merge
    merged = rrf_merge([[0, 1, 2], [2, 0, 3], [3]])
    assert merged[0] == 0  # present at rank 1 and 2
    assert merged[1] in (2, 3)
    assert len(merged) == 4


def test_search_uses_rrf_ordering(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "one.py").write_text("def alpha():\n    beta()\n", encoding="utf-8")
    (root / "two.py").write_text("def beta():\n    pass\n", encoding="utf-8")
    graph = build_repo_graph(root)
    index = build_retrieval_index(root, graph)
    hits = index.search("where is beta called", top_k=5)
    paths = [h.chunk.path for h in hits]
    assert "one.py" in paths  # the caller surfaces via the call-graph signal
    assert "two.py" in paths  # the definition surfaces via symbol signal
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_retrieval.py -q`
Expected: new tests FAIL (`rrf_merge` undefined; the caller may not surface in top-5 under additive scoring).

- [ ] **Step 3: Implement RRF in `src/clio/retrieval.py`**

```python
def rrf_merge(ranked_lists: list[list[int]], k: int = 60) -> list[int]:
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, idx in enumerate(ranked):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda i: -scores[i])
```

Refactor `search()`:

```python
def search(self, question: str, top_k: int = 8) -> list[Hit]:
    terms = tokenize(question)
    if not terms or not self._n:
        return []
    lists: list[list[int]] = []

    bm25 = self._bm25_scores(terms)
    lists.append(sorted(range(self._n), key=lambda i: -bm25[i])[:top_k * 4])

    sym_list: list[int] = []
    mod_list: list[int] = []
    path_list: list[int] = []
    for term in set(terms):
        sym_list.extend(i for i in self._sym_terms.get(term, ()) if not self.chunks[i].is_doc)
        mod_list.extend(i for i in self._mod_terms.get(term, ()))
        path_list.extend(i for i in self._path_terms.get(term, ()))
    lists.append(list(dict.fromkeys(sym_list)))
    lists.append(list(dict.fromkeys(mod_list)))
    lists.append(list(dict.fromkeys(path_list)))

    call_list = self._caller_hits(terms)
    if call_list:
        lists.append(call_list)

    neighbor_list = self._neighbor_hits(set(mod_list))
    if neighbor_list:
        lists.append(neighbor_list)

    order = rrf_merge([l for l in lists if l])
    reasons: dict[int, list[str]] = {}
    seen_files: set[str] = set()
    hits: list[Hit] = []
    for i in order:
        path = self.chunks[i].path
        key = self.chunks[i].fqn or path
        if key in seen_files:
            continue
        seen_files.add(key)
        if self.chunks[i].is_skeleton and path in seen_files:
            continue
        r = []
        if bm25[i] > 0:
            r.append(f"bm25")
        if i in set(sym_list):
            r.append("symbol match")
        if i in set(call_list):
            r.append("caller hit")
        hits.append(Hit(chunk=self.chunks[i], score=sum(1.0 / (60 + rk) for rk in range(0)), reasons=r))
        if len(hits) >= top_k:
            break
    return hits
```

Add the two helper methods `_caller_hits(terms)` and `_neighbor_hits(modules)` using the existing `self._callers`, `self._neighbors`, `self._module_chunks` structures (reuse the logic previously inline in `search()`). Preserve the "who calls X" intent bump: when terms contain call/use words, put caller hits at the head of `lists` (they already rank via RRF position 1).

Update existing `tests/test_retrieval.py` expectations where they assert on `hit.score` values or `reasons` content — scores are now RRF-derived, so assert on `reasons`/order/paths instead of numeric scores.

- [ ] **Step 4: Run the retrieval tests**

Run: `python -m pytest tests/test_retrieval.py -q`
Expected: PASS.

- [ ] **Step 5: Full suite**

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/clio/retrieval.py tests/test_retrieval.py
git commit -m "feat: RRF fusion of BM25, symbol, path, caller, neighbor signals"
```

---

### Task 9: Query understanding — one flash call to extract symbols/paths/terms (P2)

**Files:**
- Modify: `src/clio/ask.py`
- Test: `tests/test_ask.py`

**Interfaces:**
- Consumes: `LLMClient.complete`, `Limits.cheap_model`.
- Produces:
  - `QUERY_EXTRACT_SYSTEM` constant.
  - `extract_query_terms(question: str, client: LLMClient, limits: Limits) -> dict` — strict-JSON request; returns `{"symbols": [...], "paths": [...], "keywords": [...]}`; on any failure returns `{}` (fail-soft).
  - `ChatSession._understanding_cache: dict[str, dict]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_extract_query_terms_with_fake_handler(tmp_path):
    client = FakeLLM(handler=fake_handler(get_limits()))  # returns "fake guide text"
    out = asyncio.run(extract_query_terms("how does the store persist", client, get_limits()))
    assert out == {}  # non-JSON answer -> fail-soft empty


def test_query_understanding_feeds_symbols_when_no_exact_hit(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "store.py").write_text(
        "def persist(data):\n    with open('db.json', 'w') as f:\n        f.write(data)\n",
        encoding="utf-8",
    )
    graph = build_repo_graph(root)

    class UnderstandingFake:
        def __init__(self):
            self.calls = 0
        async def complete(self, messages, model=None):
            self.calls += 1
            if "Extract" in messages[0].content:
                return '{"symbols": ["persist"], "paths": ["store.py"], "keywords": ["save", "database"]}'
            return "The persist function writes data to db.json."

    client = UnderstandingFake()
    session = ChatSession("job", tmp_path, client)
    session._index = build_retrieval_index(root, graph)
    session.graph = graph
    session.readme = ""
    result = asyncio.run(session.answer("how does it save data to a file"))
    assert result["ok"]
    assert any("store.py" == s["path"] for s in result["sources"])
    assert client.calls >= 2  # understanding + answer
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ask.py -q`
Expected: FAIL (`extract_query_terms` undefined).

- [ ] **Step 3: Implement in `src/clio/ask.py`**

```python
import json as _json

QUERY_EXTRACT_SYSTEM = (
    "You are extracting search terms from a question about a code "
    "repository. Output STRICT JSON only, no prose: "
    '{"symbols": [up to 5 likely function/class names], '
    '"paths": [up to 5 likely file paths], '
    '"keywords": [up to 5 search keywords]}. '
    "Use empty arrays when nothing applies."
)


async def extract_query_terms(question: str, client: LLMClient,
                              limits: Limits) -> dict:
    try:
        text = await client.complete(
            [
                LLMMessage(role="system", content=QUERY_EXTRACT_SYSTEM),
                LLMMessage(role="user", content=f"Question: {question}"),
            ],
            model=limits.cheap_model,
        )
    except Exception:
        return {}
    if not text:
        return {}
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return {}
    try:
        data = _json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return {
        "symbols": [str(s) for s in data.get("symbols", [])][:5],
        "paths": [str(p) for p in data.get("paths", [])][:5],
        "keywords": [str(k) for k in data.get("keywords", [])][:5],
    }
```

In `ChatSession`:

- `__init__` gains `self._understanding_cache: dict[str, dict] = {}`.
- `answer()` becomes:

```python
async def answer(self, question: str, bus: EventBus | None = None) -> dict:
    index = self._ensure_index()
    hits = index.search(question, top_k=8)
    if not hits or hits[0].score < 2.0:
        if not hits:
            terms = self._understanding_cache.get(question)
            if terms is None:
                terms = await extract_query_terms(question, self._client, self._limits)
                self._understanding_cache[question] = terms
            if terms:
                hits = self._search_with_terms(question, terms, index)
    if not hits:
        result = {"answer": NO_MATCH_ANSWER, "sources": [], "ok": False}
        self._append(question, result["answer"], bus)
        return result
    ...
```

with:

```python
def _search_with_terms(self, question: str, terms: dict, index: RetrievalIndex) -> list[Hit]:
    boost_question = question
    if terms.get("keywords"):
        boost_question += " " + " ".join(terms["keywords"])
    hits = index.search(boost_question, top_k=8)
    if not hits:
        hits = []
        for path in terms.get("paths", []):
            path = path.replace("\\", "/")
            for chunk in index.chunks:
                if chunk.path == path:
                    hits.append(Hit(chunk=chunk, score=10.0, reasons=["query-understanding path"]))
                    break
    return hits
```

(`Hit` is already imported in `ask.py`.)

- [ ] **Step 4: Run the ask tests**

Run: `python -m pytest tests/test_ask.py -q`
Expected: PASS.

- [ ] **Step 5: Full suite**

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/clio/ask.py tests/test_ask.py
git commit -m "feat: query understanding — flash extraction of symbols/paths/terms"
```

---

### Task 10: Chat session compaction + memory bank (P3)

**Files:**
- Modify: `src/clio/ask.py`
- Test: `tests/test_ask.py`

**Interfaces:**
- Consumes: `LLMClient.complete`, `Limits.cheap_model`, `EVENT_ASK_FINAL`.
- Produces:
  - `COMPACT_CHARS = 9000`, `COMPACT_KEEP_TURNS = 6`, `COMPACT_SYSTEM` constant.
  - `ChatSession.compact(bus: EventBus | None = None) -> str` — one flash call writing a structured summary; sets `self.summary`; truncates history to last 6 turns; old turns appended to `self.archive`.
  - `ChatSession._maybe_compact(bus)` — triggers `compact()` when history chars exceed `COMPACT_CHARS` and no summary yet.
  - `ChatSession.write_memory(job_id: str, root: Path)` — writes `activeContext.md` + `progress.md` into `root / "jobs" / f"{job_id}.memory"` dir (or the workspace when writable).
  - `ChatSession.load_memory(root, job_id) -> str` — reads `activeContext.md` (capped 2 KB) for system context.
  - `MemoryRecorder` event hook: `web.py` calls `write_memory` when a job chat ends (Task 11 wiring).

- [ ] **Step 1: Write the failing tests**

```python
def test_compaction_summarizes_old_turns(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    graph = build_repo_graph(root)

    class CompactingFake:
        def __init__(self):
            self.summary_calls = 0
        async def complete(self, messages, model=None):
            if "Compaction" in messages[0].content:
                self.summary_calls += 1
                return "Objective: understand the store. Files: store.py. Decisions: none. Open: how it persists. Next: inspect persist()."
            return "ok"

    client = CompactingFake()
    session = ChatSession("job", tmp_path, client)
    session._index = build_retrieval_index(root, graph)
    session.graph = graph
    session.readme = ""
    session.history = [
        {"role": "user", "content": "question with " + "padding " * 3000},
        {"role": "assistant", "content": "answer " + "padding " * 3000},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
    ]
    asyncio.run(session._maybe_compact())
    assert client.summary_calls == 1
    assert session.summary and "Objective:" in session.summary
    assert len(session.history) <= 2  # last turn pair kept
    assert len(session.archive) == 2


def test_memory_bank_roundtrip(tmp_path):
    job = "clio-test"
    root = tmp_path
    session = ChatSession(job, root, FakeLLM(handler=fake_handler(get_limits())))
    session.write_memory(job, root, extra={"next": "inspect persist()"})
    text = session.load_memory(root, job)
    assert "inspect persist()" in text
    assert (root / "jobs" / f"{job}.memory" / "activeContext.md").is_file()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ask.py -q`
Expected: FAIL (`compact`, `write_memory` undefined).

- [ ] **Step 3: Implement in `src/clio/ask.py`**

```python
COMPACT_CHARS = 9000
COMPACT_KEEP_TURNS = 6
COMPACT_SYSTEM = (
    "You are compressing a chat session with a code-analysis assistant. "
    "Write a structured summary (max 600 chars) with these sections: "
    "Objective: ... | Files: ... | Decisions: ... | Open questions: ... | "
    "Next steps: ... . Keep concrete file:line references. This summary "
    "replaces the conversation history."
)
MEMORY_BUDGET_CHARS = 2000
```

Add to `ChatSession.__init__`: `self.summary: str | None = None`, `self.archive: list[dict] = []`.

```python
async def compact(self, bus: EventBus | None = None) -> str:
    lines = [f"{t['role'].capitalize()}: {t['content']}" for t in self.history]
    text = await self._client.complete(
        [
            LLMMessage(role="system", content=COMPACT_SYSTEM),
            LLMMessage(role="user", content="\n".join(lines)[-12000:]),
        ],
        model=self._limits.cheap_model,
    )
    summary = (text or "").strip()[:1200]
    if summary:
        self.summary = summary
        keep = max(len(self.history) - COMPACT_KEEP_TURNS, 0)
        self.archive.extend(self.history[:keep])
        self.history = self.history[keep:]
    return summary


async def _maybe_compact(self, bus: EventBus | None = None) -> None:
    if self.summary is None and sum(len(t["content"]) for t in self.history) > COMPACT_CHARS:
        await self.compact(bus)


def _history_block(self, history: list[dict], budget: int = HISTORY_BUDGET_CHARS) -> str:
    parts: list[str] = []
    if self.summary:
        parts.append(f"[Session summary]\n{self.summary}")
    used = len(self.summary or "")
    for turn in history[-6:]:
        line = f"{turn['role'].capitalize()}: {turn['content']}"
        if used + len(line) > budget:
            break
        parts.append(line)
        used += len(line)
    return "\n".join(parts)
```

Call `await self._maybe_compact(bus)` at the top of `answer()`.

Memory bank:

```python
def write_memory(self, job_id: str, root: Path, extra: dict | None = None) -> None:
    mem_dir = Path(root) / "jobs" / f"{job_id}.memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    objective = self.history[0]["content"][:300] if self.history else ""
    key_files = sorted({s["path"] for s in self._last_sources}) if hasattr(self, "_last_sources") else []
    active = [
        "# activeContext.md",
        f"Objective: {objective}",
        "Key files: " + ", ".join(key_files[:12]),
        "Open questions: (see chat)",
        f"Next steps: {(extra or {}).get('next', '')}",
    ]
    (mem_dir / "activeContext.md").write_text("\n".join(active), encoding="utf-8")
    (mem_dir / "progress.md").write_text(
        f"# progress.md\nTurns answered: {len(self.history)}\n", encoding="utf-8"
    )


def load_memory(self, root: Path, job_id: str) -> str:
    path = Path(root) / "jobs" / f"{job_id}.memory" / "activeContext.md"
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:MEMORY_BUDGET_CHARS]
    except OSError:
        return ""
```

Store sources on the session for `write_memory`: in `answer()`, set `self._last_sources = sources` when sources exist. Wire memory into `_prompt`: prepend `[Prior session memory]\n{load_memory(...)}` when the session has no history yet (first turn of a resumed session) — do it in `answer()` by adding the block when `self.history` is empty and memory exists.

- [ ] **Step 4: Run the ask tests**

Run: `python -m pytest tests/test_ask.py -q`
Expected: PASS.

- [ ] **Step 5: Full suite**

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/clio/ask.py tests/test_ask.py
git commit -m "feat: chat compaction with structured summary + per-job memory bank"
```

---

### Task 11: Integration — web wiring, eval results, docs (P4)

**Files:**
- Modify: `src/clio/web.py` (ChatSession constructed with `graph` + `readme` from the persisted job; `write_memory` on session close)
- Modify: `README.md`, `docs/architecture.md`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: Task 5/10 additions to `ChatSession`; `GraphStore.load()`; persisted `guide.json`.
- Produces: nothing new — integration only.

- [ ] **Step 1: Update `web.py` session construction**

Where `ChatSession(job_id, self.root, client)` is created, change to load the persisted graph and guide:

```python
from clio.store import GraphStore
from clio.ask import ChatSession

def _session_for(self, job_id: str) -> ChatSession:
    session = self._ask_sessions.get(job_id)
    if session is None:
        graph = GraphStore(self.root / "jobs" / f"{job_id}.graph.db").load()
        readme = ""
        guide_path = self.root / "jobs" / f"{job_id}.guide.json"
        if guide_path.is_file():
            try:
                readme = json.loads(guide_path.read_text(encoding="utf-8"))["readme"]
            except (OSError, ValueError, KeyError):
                readme = ""
        client = LLMClient(...)  # keep the existing client construction
        session = ChatSession(job_id, self.root, client, graph=graph, readme=readme)
        session.history = []  # fresh session
        self._ask_sessions[job_id] = session
    return session
```

(Keep the existing client/provider construction in `web.py`; only the construction call site changes.)

Add memory wiring: when the ask handler finishes, call `session.write_memory(job_id, self.root)` inside the existing `run_ask` thread, and load memory on first turn (the `_prompt` change from Task 10 handles injection when history is empty).

- [ ] **Step 2: Add a web test**

In `tests/test_web.py`:

```python
def test_ask_uses_persisted_graph_context(tmp_path, ...):
    # run a full analyze via the dashboard, then ask an overview question
    # and assert the answer request contained the repo map/README context
    # (fake handler returns "fake guide text"; assert 200 + ok True)
```

Use the existing `test_analyze_and_stream` scaffolding (fake LLM factory) — replicate its setup, then POST `/api/ask?job_id=...&q=what does this project do`, assert `resp.ok` and `body["ok"]` is True.

- [ ] **Step 3: Run the web tests**

Run: `python -m pytest tests/test_web.py -q`
Expected: PASS.

- [ ] **Step 4: Eval before/after comparison**

Run:
```
python -m tests.eval.run_eval "D:\AI\AIML\SUNRISE COUNTDOWN\ai-craftsman-portfolio\projects\clio" tests\eval\results-clio.jsonl
python -m tests.eval.run_eval sandbox\clio-2cc7c16e tests\eval\results-finedge.jsonl
```
Expected: metrics at or above the Task 1 baseline; record both tables in `docs/architecture.md` under a "Retrieval eval" section.

- [ ] **Step 5: Update docs**

- `README.md`: add a "How it works" bullet list reflecting Index-V2 (symbol chunks, doc tier, repo map, RRF), guide evidence bundles, chat query understanding + memory bank, and the eval harness (`python -m tests.eval.run_eval <repo>`).
- `docs/architecture.md`: update §3 (retrieval) and §4 (guide) and §6 (chat) to the v2 mechanisms; add the eval section with the P0 vs P4 numbers.

- [ ] **Step 6: Full suite + end-to-end verification**

Run: `python -m pytest -q`
Expected: PASS (all tests, including the 3 eval tests).

Restart the dashboard:
```powershell
$c = Get-NetTCPConnection -LocalPort 8790 -State Listen -ErrorAction SilentlyContinue
if ($c) { Stop-Process -Id $c[0].OwningProcess -Force }
Start-Process python -ArgumentList "-m","clio.web","--port","8790" -WorkingDirectory "<repo root>" -WindowStyle Hidden
```
Then analyze `file:///D:/AI/AIML/SUNRISE COUNTDOWN/ai-craftsman-portfolio/projects/clio` and the FinEdge sandbox path; verify:
- "Run it" tab lists commands (uvicorn/npm/docker for FinEdge),
- "What it is" has a real synthesis,
- chat answers "what does this project do" without the cop-out,
- citations point at files that exist.

- [ ] **Step 7: Commit**

```bash
git add src/clio/web.py tests/test_web.py README.md docs/architecture.md tests/eval
git commit -m "feat: integrate v2 pipeline, eval results, docs"
```

---

## Verification notes (2026-08-14, completed)

- **T11 wiring:** `ask_session()` constructs `ChatSession(job_id, self.root, client)` — the plan's persisted `graph=`/`readme=` params are unnecessary because `load_chat_index(workspace)` rebuilds both from the workspace directly (same behavior, fewer stale-cache paths). Memory wiring done as planned: `run_ask` writes memory in a `finally` block (guarded), `test_ask_after_analyze_writes_memory` covers it.
- **Eval (FinEdge sandbox, 4 fix queries after `git fetch --unshallow`):**

| metric | P0 baseline (Task 1) | v2 final | delta |
|---|---|---|---|
| MRR | 0.208 | 0.198 | −0.010 (≈ noise on 4 queries) |
| Recall@1 | 0.0 | 0.0 | — |
| Recall@5 | 0.333 | 0.333 | — |
| Recall@8 | 0.333 | **0.583** | +75% |
| BCY@8 | 0.333 | **0.583** | +75% |

- **Deviation from plan step:** Step 7 (commit) was skipped — the user's standing rule is commits only on explicit request.
- **Two indexing fixes made during T11:** (1) README head-insert in RRF now fires only when the query has NO symbol/path/module matches (pure lexical questions); (2) eval artifacts (`goldset.jsonl`, `results-*.jsonl`) are excluded from the index — they were polluting eval runs of the repo that contained them. A doc-chunk BM25 penalty was tried to lift code over docs on code queries; it lowered MRR, so it was reverted.
- **End-to-end verified headless:** FinEdge guide → all 4 stages synthesize with real LLM + sources; chat overview question answered from README (no cop-out). Dashboard restarted on `:8790` serving final code; rendered JS passes `node --check`; full suite 293 passed.

## Self-Review Notes

- **Spec coverage:** all spec sections map to tasks — Index-V2 (Tasks 4, 6, 7, 8, 9), Guide-V2 (2, 3, 7), Chat-V2 (5, 9, 10), eval (1, 11), frontend (11 minimal wiring; UI unchanged by design).
- **Type consistency:** `run_hints` signature change is contained in Tasks 2-3 (call sites updated in the same task). `ChatSession` constructor additions are additive with defaults, so `web.py` keeps working until Task 11 wires them.
- **Known risk:** Task 6 changes chunk boundaries — `test_retrieval.py` expectations on chunk counts/text may need updating; Task 8 changes `score` semantics — tests must assert on order/reasons, not scores. Both called out in their steps.

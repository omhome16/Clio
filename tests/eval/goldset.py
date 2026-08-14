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
DOC_SUFFIXES = (".md", ".rst", ".txt")
_IGNORE_DIRS = {".git", "node_modules", "dist", "build", "__pycache__", ".venv"}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    return proc.stdout.strip()


def _is_doc_file(rel: str) -> bool:
    low = rel.lower()
    return low.endswith(DOC_SUFFIXES) or "docs/" in low or low.endswith("license")


def _clean_query(msg: str) -> str:
    return re.sub(r"\s+", " ", msg).strip()[:300]


_HASH_RE = re.compile(r"^[0-9a-f]{40}$")


def _is_hash(line: str) -> bool:
    return bool(_HASH_RE.match(line.strip()))


def build_goldset(repo_path: Path, out_path: Path, max_commits: int = 400) -> list[dict]:
    """Walk git log; fix commits become {query, gold_paths, docs_only}."""
    repo_path = Path(repo_path)
    _git(repo_path, "rev-parse", "--is-inside-work-tree")  # raise if not a repo
    subjects = _git(repo_path, "log", f"--max-count={max_commits}", "--format=%s").splitlines()
    # each commit's files follow its %H line until the next %H line
    raw = _git(repo_path, "log", f"--max-count={max_commits}", "--name-only", "--format=%H")
    groups: list[list[str]] = []
    cur: list[str] = []
    for line in raw.splitlines():
        if _is_hash(line):
            groups.append(cur)
            cur = []
        else:
            cur.append(line)
    groups.append(cur)
    rows: list[dict] = []
    for subject, files in zip(subjects, groups[1:]):
        if not subject or not FIX_WORDS.search(subject):
            continue
        names = [f for f in files if f.strip()]
        names = [f for f in names if not any(
            f.startswith(part + "/") or f == part for part in _IGNORE_DIRS
        )]
        if not names:
            continue
        code_files = [f for f in names if not _is_doc_file(f)]
        rows.append({
            "query": _clean_query(subject),
            "gold_paths": code_files or names,
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
    mrrs, r1, r5, r8, bcys = [], [], [], [], []
    abstain_ok = 0
    abstain_total = 0
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
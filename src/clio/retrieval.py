# src/clio/retrieval.py
"""Hybrid code retrieval for chat Q&A.

Combines the same signals fast code-search products use:

1. **BM25 lexical scoring** over line-chunks of every text file (source,
   README, configs) — the classic ranked-retrieval baseline, implemented
   here in pure stdlib.
2. **Symbol attribution**: chunks are tagged with the graph symbols defined
   in their line range; identifiers in the question that match symbol names
   boost those chunks directly.
3. **Call-graph edges**: if a question names a symbol, the chunks holding the
   lines that *call* it get boosted — "who calls X" is answered by the graph.
4. **Import-graph expansion**: modules adjacent (importing / imported by) to
   matched modules get a boost, surfacing the right neighborhoods.

The index is built once per repo (pure text ops, seconds) and searches in
milliseconds. No third-party dependencies.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from clio.graph import RepoGraph

CHUNK_LINES = 150
CHUNK_OVERLAP = 25
MAX_FILE_BYTES = 1_000_000

SYMBOL_BONUS = 12.0
CALL_BONUS = 10.0
MODULE_BONUS = 6.0
NEIGHBOR_BONUS = 3.0
PATH_BONUS = 2.0
README_BONUS = 8.0

DOC_MAX_CHARS = 40_000

CALL_INTENT_WORDS = frozenset(
    ("call", "calls", "called", "calling", "use", "uses", "used", "using", "usage")
)

_ROOT_README_NAMES = ("README.md", "README.rst", "readme.md", "README.txt", "README")
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".mkd")

K1 = 1.5
B = 0.75

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

_STOPWORDS = frozenset(
    """a an and are as at be but by can do does for from has have how in is it its
    of on or so that the this to was we what when where which who why will with you
    your code file files module modules repo repository project function class
    method def import return if else elif not all any about into than then there
    these they them yes no just tell me explain give show help need want would could
    should does doing done make makes made""".split()
)


def _stem(word: str) -> str:
    """Light suffix stripping so 'call' matches 'calls' and 'process'
    matches 'processes'. Applied consistently to docs and queries, so the
    exact stems don't matter — only that both sides agree."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("es"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    """Lowercase tokens: identifiers split on camelCase and snake_case,
    light-stemmed, stopwords and short words dropped."""
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text):
        for part in _CAMEL_RE.split(tok):
            for sub in part.split("_"):
                sub = _stem(sub.lower())
                if len(sub) >= 3 and sub not in _STOPWORDS:
                    out.append(sub)
    return out


@dataclass
class Chunk:
    path: str                                  # posix rel path
    start: int                                 # 1-based first line
    end: int
    text: str
    terms: list[str] = field(default_factory=list)
    module: str = ""                           # graph module name, "" for docs/configs
    symbols: list[str] = field(default_factory=list)  # symbol ids defined in range
    doc: bool = False                          # True for docs/config/readme chunks
    header: str = ""                           # rendered chunk_header()
    fqn: str = ""                              # symbol fqn, "" for body/skeleton/doc
    is_skeleton: bool = False                  # signatures-only chunk for the file

    @property
    def key(self) -> str:
        return f"{self.path}:{self.start}-{self.end}"


@dataclass
class Hit:
    chunk: Chunk
    score: float
    reasons: list[str] = field(default_factory=list)


def _iter_text_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in ("node_modules", ".git", "venv", ".venv", "__pycache__",
                        "dist", "build", ".idea", ".vscode") for part in path.parts):
            continue
        if path.name in ("goldset.jsonl",) or (path.suffix == ".jsonl" and path.name.startswith("results-")):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        try:
            head = path.open("rb").read(8192)
        except OSError:
            continue
        if b"\x00" in head:  # binary
            continue
        files.append(path)
    return files


def _is_doc_file(rel: str, root: bool) -> bool:
    if rel in _ROOT_README_NAMES or (root and rel.startswith("README")):
        return True
    if rel.startswith("docs/"):
        return True
    return rel.endswith(_DOC_SUFFIXES)


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


def chunk_header(path: str, module: str, name: str, signature: str) -> str:
    parts = [f"# {path}"]
    if name:
        parts.append(f"# {module}::{name}" if module else f"# {name}")
    if signature:
        parts.append(f"# {signature}")
    return "\n".join(parts)


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


def build_retrieval_index(workspace: Path, graph: RepoGraph) -> "RetrievalIndex":
    """Chunk every text file and attribute graph symbols by line range."""
    workspace = Path(workspace)
    path_to_module = {
        Path(rel).as_posix(): mod for mod, rel in graph.modules.items()
    }
    symbols_by_module: dict[str, list[tuple[str, int, int, str]]] = {}
    for sym in graph.symbols:
        symbols_by_module.setdefault(sym.module, []).append(
            (f"{sym.module}::{sym.name}", sym.line, sym.end_line, sym.name))
    for module in symbols_by_module:
        symbols_by_module[module].sort(key=lambda item: item[1])

    chunks: list[Chunk] = []
    for path in _iter_text_files(workspace):
        rel = path.relative_to(workspace).as_posix()
        module = path_to_module.get(rel, "")
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        syms = symbols_by_module.get(module, [])
        doc = _is_doc_file(rel, root=path.parent == workspace)
        if doc:
            text = "\n".join(lines)[:DOC_MAX_CHARS]
            if text:
                chunks.append(Chunk(
                    path=rel, start=1, end=len(lines), text=text,
                    terms=tokenize(text), module=module, doc=True,
                ))
            continue
        sym_ranges = [(ln, en or ln, fqn, name) for fqn, ln, en, name in syms]
        ranges = [(ln, en, fqn) for ln, en, fqn, _name in sym_ranges]
        plan = symbol_chunk_plan(lines, ranges)
        fqn_by_line = {ln: (fqn, name) for ln, en, fqn, name in sym_ranges}
        for start, end in plan:
            text = "\n".join(lines[start - 1:end])
            fqn, name = fqn_by_line.get(start, ("", ""))
            header = chunk_header(rel, module, name, _signature_line(lines, start)) if fqn else ""
            symbols = [sid for sid, ln, _en, _n in syms if start <= ln <= end]
            chunks.append(Chunk(
                path=rel, start=start, end=end, text=text,
                terms=tokenize(text), module=module, symbols=symbols,
                doc=doc, header=header, fqn=fqn,
            ))
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
                doc=doc, is_skeleton=True,
                header=f"# {rel} (skeleton — signatures only)", fqn="",
            ))
    return RetrievalIndex(chunks, graph)


def rrf_merge(ranked_lists: list[list[int]], k: int = 60) -> list[int]:
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, idx in enumerate(ranked):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda i: -scores[i])


class RetrievalIndex:
    """BM25 + symbol/call/import boosted search over the chunk corpus."""

    def __init__(self, chunks: list[Chunk], graph: RepoGraph) -> None:
        self.chunks = chunks
        self._n = len(chunks)
        self._df: Counter[str] = Counter()
        self._lengths: list[int] = []
        self._avg_len = 1.0
        for chunk in chunks:
            for term in set(chunk.terms):
                self._df[term] += 1
            self._lengths.append(len(chunk.terms))
        if chunks:
            self._avg_len = sum(self._lengths) / len(chunks)

        # term -> chunk indices, per signal source
        self._sym_terms: dict[str, set[int]] = {}
        self._mod_terms: dict[str, set[int]] = {}
        self._path_terms: dict[str, set[int]] = {}
        self._root_readme: set[int] = set()
        for i, chunk in enumerate(chunks):
            if chunk.doc and chunk.path in _ROOT_README_NAMES:
                self._root_readme.add(i)
            for term in set(tokenize(" ".join(chunk.symbols))):
                self._sym_terms.setdefault(term, set()).add(i)
            if chunk.module:
                for term in set(tokenize(chunk.module.replace("/", " "))):
                    self._mod_terms.setdefault(term, set()).add(i)
            for term in set(tokenize(chunk.path.replace("/", " "))):
                self._path_terms.setdefault(term, set()).add(i)

        # module adjacency (local modules only) + callers of each symbol
        local_modules = set(graph.modules)
        self._neighbors: dict[str, set[str]] = {m: set() for m in local_modules}
        for src, targets in graph.imports.items():
            if src not in self._neighbors:
                continue
            for target in targets:
                for mod in local_modules:
                    if target == mod or target.startswith(mod + ".") or target.startswith(mod + "/"):
                        self._neighbors[src].add(mod)
                        self._neighbors.setdefault(mod, set()).add(src)
        self._module_chunks: dict[str, list[int]] = {}
        for i, chunk in enumerate(chunks):
            if chunk.module:
                self._module_chunks.setdefault(chunk.module, []).append(i)
        self._callers: dict[str, list[tuple[str, int]]] = {}
        for edge in graph.calls:
            self._callers.setdefault(edge.callee, []).append((edge.caller, edge.line))

    def _bm25_scores(self, terms: list[str]) -> list[float]:
        if not self._n:
            return []
        idf: dict[str, float] = {}
        for term in set(terms):
            df = self._df.get(term, 0)
            idf[term] = math.log(1 + (self._n - df + 0.5) / (df + 0.5))
        scores = [0.0] * self._n
        for i, chunk in enumerate(self.chunks):
            tf = Counter(chunk.terms)
            dl = self._lengths[i]
            score = 0.0
            for term in set(terms):
                f = tf.get(term, 0)
                if f:
                    score += idf[term] * f * (K1 + 1) / (f + K1 * (1 - B + B * dl / self._avg_len))
            scores[i] = score
        return scores

    def _caller_hits(self, terms: list[str]) -> list[int]:
        out: list[int] = []
        seen: set[int] = set()
        for term in set(terms):
            for sym_id, callers in self._callers.items():
                if term not in sym_id.split("::")[-1].split("."):
                    continue
                for caller, line in callers:
                    for i in self._module_chunks.get(caller.split("::")[0], ()):
                        if i not in seen and self.chunks[i].start <= line <= self.chunks[i].end:
                            seen.add(i)
                            out.append(i)
        return out

    def _neighbor_hits(self, modules: set[str]) -> list[int]:
        out: list[int] = []
        for module in modules:
            for neighbor in self._neighbors.get(module, ()):
                out.extend(self._module_chunks.get(neighbor, ()))
        return out

    def search(self, question: str, top_k: int = 8) -> list[Hit]:
        """Rank chunks for a question; at most one chunk per file."""
        terms = tokenize(question)
        if not terms or not self._n:
            return []
        bm25 = self._bm25_scores(terms)

        sym_set: set[int] = set()
        mod_set: set[int] = set()
        path_set: set[int] = set()
        matched_modules: set[str] = set()
        for term in set(terms):
            for i in self._sym_terms.get(term, ()):
                if not self.chunks[i].doc:
                    sym_set.add(i)
            for i in self._mod_terms.get(term, ()):
                mod_set.add(i)
                if self.chunks[i].module:
                    matched_modules.add(self.chunks[i].module)
            for i in self._path_terms.get(term, ()):
                path_set.add(i)

        call_list = self._caller_hits(terms)
        neighbor_set = set(self._neighbor_hits(matched_modules))
        readme_list = [i for i in self._root_readme if bm25[i] > 0]

        lists: list[list[int]] = []
        lists.append([
            i for i in sorted(range(self._n), key=lambda i: -bm25[i]) if bm25[i] > 0
        ][: top_k * 4])
        if call_list and any(t in CALL_INTENT_WORDS for t in terms):
            lists.insert(0, call_list)
        elif call_list and not sym_set:
            lists.append(call_list)
        lists.append(list(sym_set))
        lists.append(list(mod_set))
        lists.append(list(path_set))
        if neighbor_set:
            lists.append(list(neighbor_set))
        if readme_list:
            if not sym_set and not path_set and not mod_set:
                lists.insert(0, readme_list)
            else:
                lists.append(readme_list)

        order = rrf_merge([l for l in lists if l])
        seen_fqns: set[str] = set()
        seen_files: set[str] = set()
        hits: list[Hit] = []
        for i in order:
            chunk = self.chunks[i]
            if chunk.fqn:
                if chunk.fqn in seen_fqns:
                    continue
                seen_fqns.add(chunk.fqn)
            if chunk.path in seen_files:
                continue
            seen_files.add(chunk.path)
            reasons = []
            if bm25[i] > 0:
                reasons.append("bm25")
            if i in sym_set:
                reasons.append("symbol match")
            if i in mod_set:
                reasons.append("module match")
            if i in path_set:
                reasons.append("path match")
            if i in readme_list:
                reasons.append("readme")
            for term in set(terms):
                for sym_id, callers in self._callers.items():
                    if term in sym_id.split("::")[-1].split("."):
                        for caller, line in callers:
                            for j in self._module_chunks.get(caller.split("::")[0], ()):
                                if j == i and chunk.start <= line <= chunk.end:
                                    reasons.append(f"calls {sym_id} at line {line}")
                        break
            if i in neighbor_set:
                reasons.append("import neighbor")
            hits.append(Hit(chunk=chunk, score=1.0 / 60.0, reasons=reasons))
            if len(hits) >= top_k:
                break
        return hits


def pack_hits(hits: list[Hit], budget_chars: int = 12_000) -> str:
    """Serialize top hits into one prompt block under the char budget."""
    parts: list[str] = []
    used = 0
    for hit in hits:
        head = f"--- {hit.chunk.key} ---\n"
        block = head + (hit.chunk.header + "\n" if hit.chunk.header else "") + hit.chunk.text
        if used + len(block) > budget_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts)


def sources_from_hits(hits: list[Hit]) -> list[dict]:
    """Deterministic citation list: {path, start, end, snippet}."""
    return [
        {
            "path": hit.chunk.path,
            "start": hit.chunk.start,
            "end": hit.chunk.end,
            "snippet": hit.chunk.text[:160],
        }
        for hit in hits
    ]
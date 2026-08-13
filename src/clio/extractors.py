# src/clio/extractors.py
"""Regex-tier import + symbol extraction for non-Python languages.

File-level granularity on purpose (imports + classes/functions with line
numbers). Modern parsers are overkill for a repo map / impact graph — the
OmniGraph ADR and the `symbols`/`codeindex` projects reach the same conclusion:
regex handles file-level dependency analysis reliably when the syntax is
regular. Call edges are left to the Python ``ast`` path only.
"""
from __future__ import annotations

import re
from pathlib import Path

LANGUAGES = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift", ".sh": "bash",
}


def detect_language(path: Path) -> str | None:
    return LANGUAGES.get(path.suffix.lower())


def foreign_module_name(path: Path, root: Path) -> str:
    """Module id for a non-Python file: posix rel path sans extension."""
    rel = path.relative_to(root)
    parts = list(rel.with_suffix("").parts)
    return "/".join(parts) if parts else "(root)"


def _resolve_relative(target: str, importer_dir: str) -> str:
    """Normalise './x' / '../x' import specifiers to repo-relative posix."""
    if target.startswith("/"):
        return target.lstrip("/")
    if not (target.startswith("./") or target.startswith("../")):
        return target
    parts = importer_dir.split("/") if importer_dir else []
    for seg in target.split("/"):
        if seg == "." or seg == "":
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


# --- import patterns per language: (regex, target_group) ---
_IMPORT_PATTERNS: dict[str, list[tuple[str, int]]] = {
    "javascript": [
        (r"""import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]""", 1),
        (r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)""", 1),
        (r"""require\s*\(\s*['"]([^'"]+)['"]""", 1),
    ],
    "typescript": [
        (r"""import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]""", 1),
        (r"""import\s*\(\s*['"]([^'"]+)['"]""", 1),
        (r"""require\s*\(\s*['"]([^'"]+)['"]""", 1),
    ],
    "go": [],
    "rust": [(r"\buse\s+([a-zA-Z0-9_::]+)", 1)],
    "java": [(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", 1)],
    "c": [(r"""^\s*#\s*include\s*[<"]([^>"]+)[>"]""", 1)],
    "cpp": [(r"""^\s*#\s*include\s*[<"]([^>"]+)[>"]""", 1)],
    "csharp": [(r"^\s*using\s+([\w.]+)\s*;", 1)],
    "ruby": [(r"""^\s*require\s+['"]([^'"]+)['"]""", 1)],
    "php": [(r"^\s*use\s+([A-Za-z0-9_\\]+)", 1)],
    "kotlin": [(r"^\s*import\s+([\w.]+)\s*$", 1)],
    "swift": [(r"^\s*import\s+([\w.]+)\s*$", 1)],
}

# --- symbol patterns per language: (regex, kind) ---
_SYMBOL_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "javascript": [
        (r"^(?:export\s+)?(?:async\s*)?function\s+(\w+)", "function"),
        (r"^(?:export\s+)?class\s+(\w+)", "class"),
        (r"^(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_]\w*)\s*=>", "function"),
        (r"^(?:export\s+)?const\s+(\w+)\s*=\s*function", "function"),
    ],
    "typescript": [
        (r"^(?:export\s+)?(?:async\s*)?function\s+(\w+)", "function"),
        (r"^(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", "class"),
        (r"^(?:export\s+)?interface\s+(\w+)", "interface"),
        (r"^(?:export\s+)?type\s+(\w+)\s*=", "type"),
        (r"^(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_]\w*)\s*=>", "function"),
    ],
    "go": [
        (r"^func\s+(?:\([^)]*\)\s+)?(\w+)", "function"),
        (r"^type\s+(\w+)\s+(?:struct|interface|type)", "class"),
    ],
    "rust": [
        (r"^(?:pub(?:\([^)]*\))?\s+)?fn\s+(\w+)", "function"),
        (r"^(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+(\w+)", "class"),
        (r"^(?:pub(?:\([^)]*\))?\s+)?type\s+(\w+)", "type"),
    ],
    "java": [
        (r"^\s*(?:public|protected|private)?\s*(?:static\s+)?(?:final\s+)?(?:class|interface|enum|record)\s+(\w+)", "class"),
        (r"^\s*(?:public|protected|private)\s+(?:static\s+)?(?:final\s+)?[\w<>,?\[\].\s]+\s+(\w+)\s*\(", "method"),
    ],
    "c": [
        (r"^\s*(?:static\s+)?(?:inline\s+)?(?:const\s+)?[\w:*&<>]+\s+(\w+)\s*\([^;{]*\)\s*\{", "function"),
        (r"^\s*class\s+(\w+)", "class"),
        (r"^\s*struct\s+(\w+)", "class"),
    ],
    "cpp": [
        (r"^\s*(?:static\s+)?(?:inline\s+)?(?:const\s+)?[\w:*&<>,]+(?:\s*::)?\s+(\w+)\s*\([^;{]*\)\s*(?:const\s*)?\{", "function"),
        (r"^\s*(?:class|struct)\s+(\w+)", "class"),
    ],
    "csharp": [
        (r"^\s*(?:public|private|protected|internal)?\s*(?:static\s+)?(?:abstract\s+|sealed\s+|partial\s+)?(?:class|interface|struct|enum|record)\s+(\w+)", "class"),
        (r"^\s*(?:public|private|protected|internal)?\s*(?:static\s+)?[\w<>,?\[\] ]+\s+(\w+)\s*\(", "method"),
    ],
    "ruby": [
        (r"^\s*class\s+(\w+)", "class"),
        (r"^\s*module\s+(\w+)", "class"),
        (r"^\s*def\s+(\w+)", "function"),
    ],
    "php": [
        (r"^\s*(?:abstract\s+|final\s+)?class\s+(\w+)", "class"),
        (r"^\s*interface\s+(\w+)", "class"),
        (r"^\s*(?:public|private|protected|static)\s+function\s+(\w+)", "function"),
    ],
    "kotlin": [
        (r"^\s*(?:data\s+|sealed\s+)?class\s+(\w+)", "class"),
        (r"^\s*(?:interface|object)\s+(\w+)", "class"),
        (r"^\s*(?:fun)\s+([A-Za-z_]\w*)\s*\(", "function"),
    ],
    "swift": [
        (r"^\s*(?:public|internal|private|fileprivate)?\s*(?:final\s+)?(?:class|struct|protocol|enum)\s+(\w+)", "class"),
        (r"^\s*(?:public|internal|private|fileprivate)?\s*(?:static\s+)?func\s+(\w+)", "function"),
    ],
    "bash": [(r"^\s*(\w+)\s*\(\s*\)\s*\{", "function")],
}


def _go_imports(text: str) -> list[str]:
    targets: list[str] = []
    for m in re.finditer(r'^\s*import\s+"([^"]+)"', text, re.MULTILINE):
        targets.append(m.group(1))
    for block in re.findall(r"import\s*\(\s*([\s\S]*?)\s*\)", text):
        for m in re.finditer(r'"([^"]+)"', block):
            targets.append(m.group(1))
    return targets


def extract(
    text: str, lang: str, importer_dir: str = ""
) -> tuple[list[tuple[str, str, int]], list[str]]:
    """Return (symbols as (name, kind, line), import targets). No call edges."""
    symbols: list[tuple[str, str, int]] = []
    imports: list[str] = []

    if lang == "go":
        imports = _go_imports(text)
    else:
        for pattern, group in _IMPORT_PATTERNS.get(lang, []):
            for m in re.finditer(pattern, text, re.MULTILINE):
                imports.append(m.group(group))
    if lang in ("javascript", "typescript"):
        imports = [_resolve_relative(t, importer_dir) for t in imports]

    for pattern, kind in _SYMBOL_PATTERNS.get(lang, []):
        for m in re.finditer(pattern, text, re.MULTILINE):
            name = m.group(1)
            line = text.count("\n", 0, m.start()) + 1
            if not any(n == name and k == kind for n, k, _ in symbols):
                symbols.append((name, kind, line))

    seen: set[str] = set()
    deduped: list[str] = []
    for t in imports:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return symbols[:300], deduped[:80]

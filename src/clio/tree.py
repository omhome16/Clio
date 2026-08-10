# src/clio/tree.py
"""Repository tree listing and workspace statistics."""
from dataclasses import dataclass
from pathlib import Path

from clio.config import Limits, get_limits


class TreeLimitError(RuntimeError):
    """A tree walk exceeded its configured caps."""


def list_tree(
    root: Path,
    *,
    exclude_dirs: tuple[str, ...] | None = None,
    max_files: int | None = None,
    max_depth: int | None = None,
) -> list[Path]:
    limits = get_limits()
    excluded = exclude_dirs if exclude_dirs is not None else limits.exclude_dirs
    cap = max_files if max_files is not None else limits.max_files

    results: list[Path] = []

    def walk(dirpath: Path, depth: int) -> None:
        if max_depth is not None and depth > max_depth:
            raise TreeLimitError(f"max depth {max_depth} exceeded at {dirpath}")
        if len(results) > cap:
            raise TreeLimitError(f"max files {cap} exceeded")
        for child in dirpath.iterdir():
            if child.is_dir():
                if child.name in excluded:
                    continue
                walk(child, depth + 1)
            else:
                results.append(child)

    walk(root, depth=0)
    return sorted(p.relative_to(root) for p in results)


@dataclass(frozen=True)
class WorkspaceStats:
    file_count: int
    size_bytes: int
    extensions: dict[str, int]


def workspace_stats(
    root: Path,
    *,
    exclude_dirs: tuple[str, ...] | None = None,
    max_files: int | None = None,
) -> WorkspaceStats:
    limits = get_limits()
    excluded = exclude_dirs if exclude_dirs is not None else limits.exclude_dirs
    cap = max_files if max_files is not None else limits.max_files

    file_count = 0
    size_bytes = 0
    extensions: dict[str, int] = {}

    def walk(dirpath: Path) -> None:
        nonlocal file_count, size_bytes
        for child in dirpath.iterdir():
            if child.is_dir():
                if child.name in excluded:
                    continue
                walk(child)
            else:
                file_count += 1
                size_bytes += child.stat().st_size
                ext = child.suffix.lower()
                extensions[ext] = extensions.get(ext, 0) + 1
                if file_count > cap:
                    raise TreeLimitError(f"max files {cap} exceeded")

    walk(root)
    return WorkspaceStats(file_count=file_count, size_bytes=size_bytes, extensions=extensions)

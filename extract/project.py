#!/usr/bin/env python3
"""Project-level helpers for reusable C/C++ source analysis."""

import re
from collections.abc import Generator
from pathlib import Path

from .tree_sitter_extractor import (
    ALL_SOURCE_EXTENSIONS,
    C_EXTENSIONS,
    is_cpp_available,
)


EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".tox",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        ".eggs",
        "egg-info",
    }
)

PROJECT_MARKERS = (
    ".git",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "CMakeLists.txt",
    "meson.build",
    "Makefile",
)

def find_project_root(start: Path) -> Path:
    """Find a likely project root by walking upward to common markers."""
    current = start if start.is_dir() else start.parent
    for _ in range(20):
        if any((current / marker).exists() for marker in PROJECT_MARKERS):
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return start if start.is_dir() else start.parent


def source_extensions(*, include_cpp: bool | None = None) -> frozenset[str]:
    """Return source extensions to scan."""
    if include_cpp is None:
        include_cpp = is_cpp_available()
    return ALL_SOURCE_EXTENSIONS if include_cpp else C_EXTENSIONS


def discover_source_files(
    root: Path,
    *,
    max_files: int = 0,
    include_cpp: bool | None = None,
    exclude_dirs: frozenset[str] = EXCLUDE_DIRS,
) -> Generator[Path, None, None]:
    """Discover C/C++ source files under root, excluding common build dirs."""
    exts = source_extensions(include_cpp=include_cpp)
    count = 0
    if root.is_file():
        if root.suffix in exts:
            yield root
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in exts:
            continue
        try:
            parts = set(path.relative_to(root).parts)
        except ValueError:
            continue
        if parts & exclude_dirs:
            continue
        yield path
        count += 1
        if max_files and count >= max_files:
            return


def deduplicate_findings(findings: list[dict]) -> list[dict]:
    """Deduplicate findings by type, file, and normalized detail."""

    def normalize(detail: str) -> str:
        text = re.sub(r"line \d+", "line N", detail)
        return re.sub(r"'[^']+?'", "'VAR'", text)

    groups: dict[tuple[str, str, str], list[dict]] = {}
    for finding in findings:
        key = (
            finding.get("type", ""),
            finding.get("file", ""),
            normalize(finding.get("detail", "")),
        )
        groups.setdefault(key, []).append(finding)

    result = []
    for group in groups.values():
        canonical = group[0]
        if len(group) > 1:
            canonical["duplicate_count"] = len(group) - 1
            canonical["duplicate_locations"] = [
                {"file": item.get("file", ""), "line": item.get("line", 0)}
                for item in group[1:]
            ]
        result.append(canonical)
    return result

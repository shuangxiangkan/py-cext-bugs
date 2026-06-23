#!/usr/bin/env python3
"""Heuristic discovery for CPython C extension source files.

This is a project-layout discovery layer, not a C/C++ AST extraction layer and
not a full build-system evaluator. It recognizes common Python extension build
entry points and falls back to scanning source files that include Python.h.
"""

import argparse
import json
import re
import sys
from pathlib import Path


SOURCE_EXTENSIONS = (".c", ".cpp", ".cxx", ".cc")
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


def _should_skip(path: Path, root: Path) -> bool:
    try:
        parts = set(path.relative_to(root).parts)
    except ValueError:
        return True
    return bool(parts & EXCLUDE_DIRS)


def _find_c_files(root: Path) -> list[Path]:
    result = []
    if not root.is_dir():
        return result
    for suffix in SOURCE_EXTENSIONS:
        for path in sorted(root.rglob(f"*{suffix}")):
            if path.is_file() and not _should_skip(path, root):
                result.append(path)
    return sorted(set(result))


def _find_h_files(root: Path, c_files: list[Path]) -> list[Path]:
    dirs = {path.parent for path in c_files}
    headers = []
    for directory in dirs:
        for header in sorted(directory.glob("*.h")):
            if header.is_file() and not _should_skip(header, root):
                headers.append(header)
    return headers


def _detect_setup_py(root: Path) -> list[dict] | None:
    setup_py = root / "setup.py"
    if not setup_py.is_file():
        return None
    try:
        content = setup_py.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "Extension(" not in content and "ext_modules" not in content:
        return None

    extensions = []
    ext_pattern = re.compile(
        r'Extension\s*\(\s*["\']([^"\']+)["\']\s*,\s*'
        r"(?:sources\s*=\s*)?"
        r"\[([^\]]*)\]",
        re.DOTALL,
    )
    for match in ext_pattern.finditer(content):
        sources_text = match.group(2)
        extensions.append(
            {
                "module_name": match.group(1),
                "source_files": re.findall(r'["\']([^"\']+)["\']', sources_text),
                "detection_method": "setup_py",
            }
        )
    return extensions or None


def _detect_pyproject_toml(root: Path) -> list[dict] | None:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        content = pyproject.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    extensions = []
    if "tool.setuptools.ext-modules" in content or "ext-modules" in content:
        ext_pattern = re.compile(
            r"\[\[tool\.setuptools\.ext-modules\]\]\s*\n(.*?)(?=\n\[|\Z)",
            re.DOTALL,
        )
        for match in ext_pattern.finditer(content):
            block = match.group(1)
            name_match = re.search(r'name\s*=\s*"([^"]+)"', block)
            sources_match = re.search(r"sources\s*=\s*\[([^\]]*)\]", block)
            if not name_match:
                continue
            sources = []
            if sources_match:
                sources = re.findall(r'"([^"]+)"', sources_match.group(1))
            extensions.append(
                {
                    "module_name": name_match.group(1),
                    "source_files": sources,
                    "detection_method": "pyproject_toml",
                }
            )

    if "tool.meson-python" in content or "meson-python" in content:
        meson_result = _detect_meson_build(root)
        if meson_result:
            return meson_result

    if "tool.scikit-build" in content or "tool.scikit-build-core" in content:
        cmake_result = _detect_cmake(root)
        if cmake_result:
            return cmake_result

    return extensions or None


def _detect_meson_build(root: Path) -> list[dict] | None:
    meson = root / "meson.build"
    if not meson.is_file():
        return None
    try:
        content = meson.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "extension_module(" not in content:
        return None

    extensions = []
    ext_pattern = re.compile(
        r"extension_module\s*\(\s*'([^']+)'\s*,\s*"
        r"(?:sources\s*:\s*)?"
        r"\[([^\]]*)\]",
        re.DOTALL,
    )
    for match in ext_pattern.finditer(content):
        extensions.append(
            {
                "module_name": match.group(1),
                "source_files": re.findall(r"'([^']+)'", match.group(2)),
                "detection_method": "meson_build",
            }
        )
    return extensions or None


def _detect_cmake(root: Path) -> list[dict] | None:
    cmake = root / "CMakeLists.txt"
    if not cmake.is_file():
        return None
    try:
        content = cmake.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    extensions = []
    for pattern_name, method in [
        (r"pybind11_add_module", "cmake_pybind11"),
        (r"Python3_add_library", "cmake_python3"),
    ]:
        pattern = re.compile(pattern_name + r"\s*\(\s*(\w+)\s+(.*?)\)", re.DOTALL)
        for match in pattern.finditer(content):
            tokens = match.group(2).split()
            source_files = [tok for tok in tokens if tok.endswith(SOURCE_EXTENSIONS)]
            extensions.append(
                {
                    "module_name": match.group(1),
                    "source_files": source_files,
                    "detection_method": method,
                }
            )
    return extensions or None


def _detect_python_h_fallback(root: Path) -> list[dict] | None:
    python_c_files = []
    for c_file in _find_c_files(root):
        try:
            content = c_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r'#\s*include\s*[<"]Python\.h[">]', content):
            python_c_files.append(c_file)

    if not python_c_files:
        return None

    groups: dict[Path, list[Path]] = {}
    for path in python_c_files:
        groups.setdefault(path.parent, []).append(path)

    extensions = []
    for directory, files in groups.items():
        module_name = directory.name
        for path in files:
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = re.search(r"PyMODINIT_FUNC\s+PyInit_(\w+)", content)
            if match:
                module_name = match.group(1)
                break
        extensions.append(
            {
                "module_name": module_name,
                "source_files": [
                    str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
                    for path in files
                ],
                "detection_method": "python_h_include",
            }
        )
    return extensions


def _scan_init_functions(root: Path, source_files: list[str]) -> dict[str, str]:
    init_funcs = {}
    for rel_path in source_files:
        full_path = root / rel_path
        if not full_path.is_file():
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(r"PyMODINIT_FUNC\s+PyInit_(\w+)", content)
        if match:
            init_funcs[rel_path] = f"PyInit_{match.group(1)}"
    return init_funcs


def _scan_limited_api(root: Path, source_files: list[str]) -> tuple[bool, str | None]:
    for rel_path in source_files:
        full_path = root / rel_path
        if not full_path.is_file():
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(
            r"#\s*define\s+Py_LIMITED_API\s+(0x[0-9A-Fa-f]+|\w+)?",
            content,
        )
        if match:
            return True, match.group(1) if match.group(1) else None
    return False, None


def _get_python_requires(root: Path) -> str | None:
    setup_py = root / "setup.py"
    if setup_py.is_file():
        try:
            content = setup_py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        match = re.search(r'python_requires\s*=\s*["\']([^"\']+)["\']', content)
        if match:
            return match.group(1)

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            content = pyproject.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        match = re.search(r'requires-python\s*=\s*"([^"]+)"', content)
        if match:
            return match.group(1)
    return None


def _count_lines(root: Path, source_files: list[str]) -> int:
    total = 0
    for rel_path in source_files:
        full_path = root / rel_path
        if not full_path.is_file():
            continue
        try:
            total += full_path.read_text(encoding="utf-8", errors="replace").count("\n") + 1
        except OSError:
            pass
    return total


def _detect_code_generation(root: Path, source_files: list[str]) -> str:
    has_cython = any(not _should_skip(path, root) for path in root.rglob("*.pyx"))
    has_mypyc = False
    has_pybind11 = False
    has_hand_written = False

    for rel_path in source_files:
        full_path = root / rel_path
        if not full_path.is_file():
            continue
        try:
            header = full_path.read_bytes()[:500].decode("utf-8", errors="replace")
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            has_hand_written = True
            continue
        if "Generated by Cython" in header:
            has_cython = True
        elif "CPyDef_" in header or "mypyc" in header.lower():
            has_mypyc = True
        elif "PYBIND11_MODULE" in header or "py::class_" in header:
            has_pybind11 = True
        elif re.search(r"\bCPyDef_\w+|\bCPyStatic_\w+|\bCPyModule_\w+", content):
            has_mypyc = True
        elif re.search(r"PYBIND11_MODULE\s*\(|py::class_", content):
            has_pybind11 = True
        else:
            has_hand_written = True

    generators = []
    if has_cython:
        generators.append("cython")
    if has_mypyc:
        generators.append("mypyc")
    if has_pybind11:
        generators.append("pybind11")
    if not generators:
        return "hand_written"
    if has_hand_written or len(generators) > 1:
        return "mixed"
    return generators[0]


def _detect_type_stubs(root: Path) -> list[str]:
    result = []
    for path in sorted(root.rglob("*.pyi")):
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(
            part.startswith(".") or part in {"build", "dist", "__pycache__", "site-packages"}
            for part in parts
        ):
            continue
        result.append(str(path.relative_to(root)))
    return result


def discover(target: str | Path) -> dict:
    """Discover CPython C extension source files at the given path."""
    target_path = Path(target).resolve()
    root = target_path.parent if target_path.is_file() else target_path

    extensions = None
    for detect_fn in (
        _detect_setup_py,
        _detect_pyproject_toml,
        _detect_meson_build,
        _detect_cmake,
        _detect_python_h_fallback,
    ):
        extensions = detect_fn(root)
        if extensions:
            break
    extensions = extensions or []

    all_source_files = set()
    for extension in extensions:
        source_paths = []
        for source_file in extension["source_files"]:
            full_path = root / source_file
            if full_path.is_file():
                source_paths.append(full_path)
                all_source_files.add(source_file)
        extension["header_files"] = [
            str(header.relative_to(root))
            for header in _find_h_files(root, source_paths)
        ]

    all_source_files_list = sorted(all_source_files)
    limited_api, limited_api_version = _scan_limited_api(root, all_source_files_list)

    return {
        "project_root": str(root),
        "scan_root": str(target_path),
        "extensions": extensions,
        "python_requires": _get_python_requires(root),
        "limited_api": limited_api,
        "limited_api_version": limited_api_version,
        "init_functions": _scan_init_functions(root, all_source_files_list),
        "total_c_files": len(_find_c_files(root)),
        "total_lines": _count_lines(root, all_source_files_list),
        "code_generation": _detect_code_generation(root, all_source_files_list),
        "type_stubs": _detect_type_stubs(root),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", default=".")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = discover(args.target)
    except Exception as exc:
        json.dump({"error": str(exc), "type": type(exc).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 1
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

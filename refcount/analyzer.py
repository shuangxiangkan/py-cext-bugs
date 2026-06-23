#!/usr/bin/env python3
"""Reference-count ownership checks built on generic C/C++ extraction.

The AST extraction layer is intentionally separate from this module. Refcount
rules consume generic function/call/return data plus a configurable API
semantics table that classifies calls as returning new references, borrowed
references, or stealing references.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from analysis.sources import (
    discover_source_files,
    find_project_root,
    first_unscannable_cpp_file,
)
from refcount.ownership_transfer import analyze_function_ownership
from analysis.parsing import (
    extract_functions,
    find_assigned_variable,
    find_calls_in_scope,
    find_return_statements,
    get_node_text,
    parse_bytes_for_file,
    strip_casts,
)


_DATA_DIR = Path(__file__).resolve().parent
DEFAULT_API_OWNERSHIP_PATH = _DATA_DIR / "api_ownership.json"

DEFAULT_EXECUTING_APIS = frozenset(
    {
        "Py_DECREF",
        "Py_XDECREF",
        "Py_CLEAR",
        "PyObject_SetAttr",
        "PyObject_SetAttrString",
        "PyObject_SetItem",
        "PyObject_DelItem",
        "PyObject_Call",
        "PyObject_CallObject",
        "PyObject_CallFunction",
        "PyObject_CallMethod",
        "PyObject_CallNoArgs",
        "PyObject_CallOneArg",
        "PyObject_RichCompare",
        "PyObject_IsTrue",
        "PyObject_Hash",
        "PyObject_Str",
        "PyObject_Repr",
        "PyObject_Format",
        "PyObject_Bytes",
        "PyObject_ASCII",
        "PyErr_SetObject",
        "PyErr_Format",
        "PyObject_GetAttr",
        "PyObject_GetAttrString",
        "PyObject_GetItem",
    }
)

DEFAULT_RELEASE_APIS = frozenset({"Py_DECREF", "Py_XDECREF", "Py_CLEAR", "Py_SETREF"})
DEFAULT_INCREF_APIS = frozenset({"Py_INCREF", "Py_XINCREF"})
DEFAULT_IMMUTABLE_BORROWED_APIS = frozenset({"PyTuple_GetItem", "PyTuple_GET_ITEM"})
DEFAULT_ALWAYS_STEAL_APIS = frozenset({"PyList_SetItem", "PyTuple_SetItem"})
DEFAULT_OWNED_WRAPPER_TYPES = frozenset()
DEFAULT_WRAPPER_BORROW_METHODS = frozenset({"borrow", "borrow_o", "get"})
DEFAULT_WRAPPER_RELEASE_METHODS = frozenset(
    {"release", "relinquish_ownership", "detach", "DISOWN", "CLEAR"}
)


@dataclass(frozen=True)
class RefcountSemantics:
    """API ownership semantics used by the refcount checks."""

    new_ref_apis: frozenset[str]
    borrowed_ref_apis: frozenset[str]
    steal_ref_apis: frozenset[str]
    release_apis: frozenset[str] = DEFAULT_RELEASE_APIS
    incref_apis: frozenset[str] = DEFAULT_INCREF_APIS
    executing_apis: frozenset[str] = DEFAULT_EXECUTING_APIS
    immutable_borrowed_apis: frozenset[str] = DEFAULT_IMMUTABLE_BORROWED_APIS
    always_steal_apis: frozenset[str] = DEFAULT_ALWAYS_STEAL_APIS
    owned_wrapper_types: frozenset[str] = DEFAULT_OWNED_WRAPPER_TYPES
    wrapper_borrow_methods: frozenset[str] = DEFAULT_WRAPPER_BORROW_METHODS
    wrapper_release_methods: frozenset[str] = DEFAULT_WRAPPER_RELEASE_METHODS

    @property
    def python_executing_apis(self) -> frozenset[str]:
        """Calls that may run user code and invalidate borrowed references."""
        return self.new_ref_apis | self.executing_apis


def load_refcount_semantics(path: str | Path | None = None) -> RefcountSemantics:
    """Load API ownership semantics from JSON.

    The default JSON describes CPython C API ownership rules, but callers can
    pass a different file to analyze another ownership API.
    """
    table_path = Path(path) if path else DEFAULT_API_OWNERSHIP_PATH
    data = json.loads(table_path.read_text(encoding="utf-8"))
    return RefcountSemantics(
        new_ref_apis=frozenset(data.get("new_ref_apis", [])),
        borrowed_ref_apis=frozenset(data.get("borrowed_ref_apis", [])),
        steal_ref_apis=frozenset(data.get("steal_ref_apis", [])),
        incref_apis=frozenset(data.get("incref_apis", DEFAULT_INCREF_APIS)),
        owned_wrapper_types=frozenset(
            data.get("owned_wrapper_types", DEFAULT_OWNED_WRAPPER_TYPES)
        ),
        wrapper_borrow_methods=frozenset(
            data.get("wrapper_borrow_methods", DEFAULT_WRAPPER_BORROW_METHODS)
        ),
        wrapper_release_methods=frozenset(
            data.get("wrapper_release_methods", DEFAULT_WRAPPER_RELEASE_METHODS)
        ),
    )


def _var_in_text(var: str, text: str) -> bool:
    return bool(re.search(r"\b" + re.escape(var) + r"\b", text))


def _first_arg(arguments_text: str) -> str:
    return strip_casts(arguments_text.split(",")[0]).strip()


def _last_simple_arg(arguments_text: str) -> str | None:
    args = [arg.strip() for arg in arguments_text.split(",")]
    if not args:
        return None
    arg = strip_casts(args[-1]).strip()
    return arg if re.match(r"^\w+$", arg) else None


def _return_is_guarded_null_check(return_node, var: str, source_bytes: bytes) -> bool:
    """Return true for patterns like ``if (var == NULL) return NULL;``."""
    node = return_node.parent
    while node and node.type != "function_definition":
        if node.type == "if_statement":
            condition = node.child_by_field_name("condition")
            if not condition:
                return False
            condition_text = get_node_text(condition, source_bytes)
            escaped = re.escape(var)
            return bool(
                re.search(r"\b" + escaped + r"\s*==\s*NULL\b", condition_text)
                or re.search(r"\bNULL\s*==\s*" + escaped + r"\b", condition_text)
                or re.search(r"!\s*" + escaped + r"\b", condition_text)
            )
        node = node.parent
    return False


def _is_owned_wrapper_constructor(call_node, var: str, source_bytes: bytes, semantics) -> bool:
    """Return true if a new-ref call is immediately passed into an RAII wrapper."""
    wrapper_types = getattr(semantics, "owned_wrapper_types", frozenset())
    if not wrapper_types:
        return False
    wrapper_pattern = "|".join(re.escape(name) for name in wrapper_types)
    # The wrapper takes ownership via any of the three init syntaxes:
    # ``var(expr)``, ``var{expr}``, or copy-init ``var = expr``.
    pattern = re.compile(
        rf"\b(?:const\s+)?(?:[\w:]+::)?(?:{wrapper_pattern})\s+"
        + re.escape(var)
        + r"\s*[\(\{=]"
    )
    node = call_node
    while node and node.type != "function_definition":
        if node.type == "declaration":
            return bool(pattern.search(get_node_text(node, source_bytes)))
        node = node.parent
    return False


def _source_files_to_scan(scan_root: Path) -> list[Path]:
    """Return the C/C++ source files that refcount analysis should scan."""
    return list(discover_source_files(scan_root))


def check_potential_leaks(func, source_bytes: bytes, semantics: RefcountSemantics):
    """Check for new references that are never released, returned, or stolen."""
    findings = []
    body = func["body_node"]
    all_calls = find_calls_in_scope(body, source_bytes)
    all_calls.sort(key=lambda call: call["start_byte"])

    return_values = {
        strip_casts(ret["value_text"]).strip()
        for ret in find_return_statements(body, source_bytes)
        if ret["value_text"]
    }
    release_calls = find_calls_in_scope(
        body, source_bytes, api_names=set(semantics.release_apis)
    )
    released_vars = {_first_arg(call["arguments_text"]) for call in release_calls}

    steal_calls = find_calls_in_scope(
        body, source_bytes, api_names=set(semantics.steal_ref_apis)
    )
    stolen_vars = {
        var
        for call in steal_calls
        for var in [_last_simple_arg(call["arguments_text"])]
        if var
    }

    for call in all_calls:
        if call["function_name"] not in semantics.new_ref_apis:
            continue
        var = find_assigned_variable(call["node"], source_bytes)
        if not var:
            continue
        if _is_owned_wrapper_constructor(call["node"], var, source_bytes, semantics):
            continue
        if var not in released_vars and var not in return_values and var not in stolen_vars:
            findings.append(
                {
                    "type": "potential_leak",
                    "function": func["name"],
                    "line": call["start_line"],
                    "confidence": "medium",
                    "detail": (
                        f"New reference from {call['function_name']}() assigned "
                        f"to '{var}' may not be released"
                    ),
                    "api_call": call["function_name"],
                    "variable": var,
                }
            )
    return findings


def check_leak_on_error(func, source_bytes: bytes, semantics: RefcountSemantics):
    """Check for an error return between acquisition and release."""
    findings = []
    body = func["body_node"]
    all_calls = find_calls_in_scope(body, source_bytes)
    all_calls.sort(key=lambda call: call["start_byte"])

    error_returns = [
        ret for ret in find_return_statements(body, source_bytes)
        if ret["value_text"] == "NULL"
    ]

    acquired = {}
    for call in all_calls:
        if call["function_name"] not in semantics.new_ref_apis:
            continue
        var = find_assigned_variable(call["node"], source_bytes)
        if var:
            acquired[var] = (call, call["start_byte"])

    release_positions = {}
    for call in find_calls_in_scope(body, source_bytes, set(semantics.release_apis)):
        var = _first_arg(call["arguments_text"])
        if var not in release_positions or call["start_byte"] < release_positions[var]:
            release_positions[var] = call["start_byte"]

    for var, (call, acquire_byte) in acquired.items():
        release_byte = release_positions.get(var)
        if release_byte is None:
            continue
        for err_return in error_returns:
            err_byte = err_return["node"].start_byte
            if not (acquire_byte < err_byte < release_byte):
                continue
            if _return_is_guarded_null_check(err_return["node"], var, source_bytes):
                continue
            parent = err_return["node"].parent
            if not parent:
                continue
            block_text = get_node_text(parent, source_bytes)
            if _var_in_text(var, block_text) and any(
                api in block_text for api in semantics.release_apis
            ):
                continue
            findings.append(
                {
                    "type": "potential_leak_on_error",
                    "function": func["name"],
                    "line": err_return["start_line"],
                    "confidence": "medium",
                    "detail": (
                        f"Error return at line {err_return['start_line']} may leak "
                        f"'{var}' acquired at line {call['start_line']} via "
                        f"{call['function_name']}()"
                    ),
                    "api_call": call["function_name"],
                    "variable": var,
                    "error_return_line": err_return["start_line"],
                    "acquire_line": call["start_line"],
                }
            )
    return findings


def check_borrowed_ref_across_call(
    func,
    source_bytes: bytes,
    semantics: RefcountSemantics,
):
    """Check for borrowed references used after an intervening executing call."""
    findings = []
    body = func["body_node"]
    all_calls = find_calls_in_scope(body, source_bytes)
    all_calls.sort(key=lambda call: call["start_byte"])

    for i, call in enumerate(all_calls):
        if call["function_name"] not in semantics.borrowed_ref_apis:
            continue
        if call["function_name"] in semantics.immutable_borrowed_apis:
            continue

        borrowed_var = find_assigned_variable(call["node"], source_bytes)
        if borrowed_var is None:
            continue

        for j in range(i + 1, len(all_calls)):
            intervening = all_calls[j]
            if intervening["function_name"] not in semantics.python_executing_apis:
                continue

            found_in_later_call = False
            for later in all_calls[j + 1 :]:
                if not _var_in_text(borrowed_var, later["arguments_text"]):
                    continue
                findings.append(
                    {
                        "type": "borrowed_ref_across_call",
                        "function": func["name"],
                        "line": call["start_line"],
                        "confidence": "high",
                        "detail": (
                            f"Borrowed ref '{borrowed_var}' from "
                            f"{call['function_name']}() used after "
                            f"{intervening['function_name']}() at line "
                            f"{intervening['start_line']}"
                        ),
                        "borrowed_api": call["function_name"],
                        "borrowed_var": borrowed_var,
                        "intervening_call": intervening["function_name"],
                        "intervening_line": intervening["start_line"],
                        "use_after_line": later["start_line"],
                    }
                )
                found_in_later_call = True
                break

            if not found_in_later_call:
                after_text = source_bytes[
                    intervening["node"].end_byte : body.end_byte
                ].decode("utf-8", errors="replace")
                esc = re.escape(borrowed_var)
                if (
                    re.search(r"\b" + esc + r"\s*->", after_text)
                    or re.search(r"\*\s*" + esc + r"\b", after_text)
                    or re.search(r"=\s*" + esc + r"\s*;", after_text)
                ):
                    findings.append(
                        {
                            "type": "borrowed_ref_across_call",
                            "function": func["name"],
                            "line": call["start_line"],
                            "confidence": "medium",
                            "detail": (
                                f"Borrowed ref '{borrowed_var}' from "
                                f"{call['function_name']}() used after "
                                f"{intervening['function_name']}() at line "
                                f"{intervening['start_line']}"
                            ),
                            "borrowed_api": call["function_name"],
                            "borrowed_var": borrowed_var,
                            "intervening_call": intervening["function_name"],
                            "intervening_line": intervening["start_line"],
                        }
                    )
            break
    return findings


def check_stolen_ref_misuse(func, source_bytes: bytes, semantics: RefcountSemantics):
    """Check for a release after a call steals ownership."""
    findings = []
    body = func["body_node"]
    all_calls = find_calls_in_scope(body, source_bytes)
    all_calls.sort(key=lambda call: call["start_byte"])

    for i, call in enumerate(all_calls):
        if call["function_name"] not in semantics.steal_ref_apis:
            continue
        stolen_var = _last_simple_arg(call["arguments_text"])
        if not stolen_var:
            continue
        for later in all_calls[i + 1 :]:
            if later["function_name"] in semantics.release_apis and _var_in_text(
                stolen_var, later["arguments_text"]
            ):
                findings.append(
                    {
                        "type": "stolen_ref_not_nulled",
                        "function": func["name"],
                        "line": later["start_line"],
                        "confidence": "high",
                        "detail": (
                            f"Variable '{stolen_var}' released at line "
                            f"{later['start_line']} after being stolen by "
                            f"{call['function_name']}() at line {call['start_line']}"
                        ),
                        "steal_api": call["function_name"],
                        "variable": stolen_var,
                        "steal_line": call["start_line"],
                    }
                )
                break
    return findings


def check_stolen_ref_double_free(
    func,
    source_bytes: bytes,
    semantics: RefcountSemantics,
):
    """Check for releasing a value stolen even on failure."""
    findings = []
    body = func["body_node"]
    all_calls = find_calls_in_scope(body, source_bytes)
    all_calls.sort(key=lambda call: call["start_byte"])

    for call in all_calls:
        if call["function_name"] not in semantics.always_steal_apis:
            continue
        stolen_var = _last_simple_arg(call["arguments_text"])
        if not stolen_var:
            continue

        parent = call["node"].parent
        while parent and parent.type not in ("if_statement", "function_definition"):
            parent = parent.parent
        if parent is None or parent.type != "if_statement":
            continue

        if_body = None
        for child in parent.children:
            if child.type == "compound_statement":
                if_body = child
                break
        if if_body is None:
            continue

        body_text = get_node_text(if_body, source_bytes)
        for release_api in semantics.release_apis:
            pattern = rf"\b{re.escape(release_api)}\s*\(\s*{re.escape(stolen_var)}\s*\)"
            if not re.search(pattern, body_text):
                continue
            findings.append(
                {
                    "type": "stolen_ref_double_free",
                    "function": func["name"],
                    "line": call["start_line"],
                    "confidence": "high",
                    "detail": (
                        f"{call['function_name']}() at line {call['start_line']} "
                        f"always steals '{stolen_var}', but error path releases it"
                    ),
                    "steal_api": call["function_name"],
                    "variable": stolen_var,
                    "steal_line": call["start_line"],
                }
            )
            break
    return findings


CHECKERS = (
    check_potential_leaks,
    check_leak_on_error,
    check_borrowed_ref_across_call,
    check_stolen_ref_misuse,
    check_stolen_ref_double_free,
)


def _ownership_finding_to_dict(finding) -> dict:
    """Convert an ownership-flow finding to the analyzer JSON shape."""
    result = {
        "type": finding.type,
        "function": finding.function,
        "line": finding.line,
        "confidence": finding.confidence,
        "detail": finding.detail,
        "variable": finding.variable,
    }
    if finding.acquire_line is not None:
        result["acquire_line"] = finding.acquire_line
    if finding.api_call is not None:
        result["api_call"] = finding.api_call
    return result


def _finding_key(finding: dict) -> tuple:
    """Return a stable key for exact finding de-duplication."""
    return (
        finding.get("type"),
        finding.get("file"),
        finding.get("function"),
        finding.get("line"),
        finding.get("variable"),
        finding.get("api_call"),
    )


def _legacy_leak_covered_by_path(finding: dict, path_leak_keys: set[tuple]) -> bool:
    """Return true when data-flow already reported the same leak family."""
    if finding.get("type") not in {"potential_leak", "potential_leak_on_error"}:
        return False
    return (
        finding.get("function"),
        finding.get("variable"),
        finding.get("api_call"),
    ) in path_leak_keys


def analyze_file(
    filepath: Path,
    *,
    project_root: Path | None = None,
    semantics: RefcountSemantics | None = None,
) -> dict:
    """Analyze one C/C++ file for refcount ownership issues."""
    semantics = semantics or load_refcount_semantics()
    project_root = project_root or filepath.parent
    source_bytes = filepath.read_bytes()
    tree = parse_bytes_for_file(source_bytes, filepath)
    functions = extract_functions(tree, source_bytes)

    findings = []
    try:
        rel = str(filepath.relative_to(project_root))
    except ValueError:
        rel = str(filepath)

    seen_findings = set()
    for func in functions:
        # The data-flow pass builds a CFG and iterates to a fixed point; isolate
        # its failure so one pathological function cannot abort the whole scan.
        try:
            ownership = analyze_function_ownership(func, source_bytes, semantics)
            ownership_findings = [
                _ownership_finding_to_dict(finding)
                for finding in ownership.findings
            ]
        except Exception:
            ownership_findings = []
        path_leak_keys = {
            (
                finding.get("function"),
                finding.get("variable"),
                finding.get("api_call"),
            )
            for finding in ownership_findings
            if finding.get("type") == "potential_leak_on_path"
        }

        for checker in CHECKERS:
            for finding in checker(func, source_bytes, semantics):
                if _legacy_leak_covered_by_path(finding, path_leak_keys):
                    continue
                finding["file"] = rel
                key = _finding_key(finding)
                if key not in seen_findings:
                    seen_findings.add(key)
                    findings.append(finding)
        for result in ownership_findings:
            result["file"] = rel
            key = _finding_key(result)
            if key not in seen_findings:
                seen_findings.add(key)
                findings.append(result)

    return {
        "file": rel,
        "functions_analyzed": len(functions),
        "findings": findings,
    }


def analyze_path(
    target: str | Path,
    *,
    max_files: int = 0,
    semantics: RefcountSemantics | None = None,
    api_ownership: str | Path | None = None,
) -> dict:
    """Analyze a file or directory for refcount ownership issues."""
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)
    scan_root = target_path if target_path.is_dir() else target_path.parent
    semantics = semantics or load_refcount_semantics(api_ownership)

    findings = []
    skipped = []
    warnings = []
    functions_analyzed = 0
    files_analyzed = 0

    # C++ files are scanned by default, but only when tree-sitter-cpp is
    # installed. If it is missing, surface a warning instead of silently
    # skipping the C++ sources discovery dropped.
    unscannable_cpp = first_unscannable_cpp_file(target_path)
    if unscannable_cpp is not None:
        message = (
            "tree-sitter-cpp is not installed; C++ sources such as "
            f"{unscannable_cpp} were skipped. Install it with "
            "'pip install tree-sitter-cpp' to scan C++ files."
        )
        warnings.append(message)
        print(message, file=sys.stderr)

    source_files = _source_files_to_scan(target_path)
    if max_files:
        source_files = source_files[:max_files]

    for filepath in source_files:
        try:
            result = analyze_file(
                filepath,
                project_root=project_root,
                semantics=semantics,
            )
        except OSError as exc:
            skipped.append({"file": str(filepath), "reason": str(exc)})
            continue
        if not result["functions_analyzed"]:
            continue
        files_analyzed += 1
        functions_analyzed += result["functions_analyzed"]
        findings.extend(result["findings"])

    by_type = defaultdict(int)
    by_confidence = defaultdict(int)
    for finding in findings:
        by_type[finding["type"]] += 1
        by_confidence[finding["confidence"]] += 1

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_analyzed": files_analyzed,
        "functions_analyzed": functions_analyzed,
        "findings": findings,
        "summary": {
            "total_findings": len(findings),
            "by_type": dict(by_type),
            "by_confidence": dict(by_confidence),
        },
        "skipped_files": skipped,
        "warnings": warnings,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", default=".")
    parser.add_argument("--max-files", type=int, default=0)
    parser.add_argument(
        "--api-ownership", help="JSON file with refcount API ownership tables"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = analyze_path(
            args.target,
            max_files=args.max_files,
            api_ownership=args.api_ownership,
        )
    except Exception as exc:
        json.dump({"error": str(exc), "type": type(exc).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 1
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""CFG/data-flow based ownership transfer for CPython refcount analysis."""

import re
from dataclasses import dataclass

from analysis.controlflow import ControlFlowGraph, build_function_cfg
from analysis.dataflow import DataFlowResult, analyze_forward
from refcount.ownership_state import (
    BORROWED,
    ESCAPED,
    NULL,
    OWNED,
    RELEASED,
    RETURNED,
    STOLEN,
    UNKNOWN,
    OwnershipState,
    merge_ownership_states,
)


@dataclass(frozen=True)
class OwnershipFinding:
    """A refcount ownership issue found by data-flow analysis."""

    type: str
    function: str
    line: int
    variable: str
    confidence: str
    detail: str
    acquire_line: int | None = None
    api_call: str | None = None


@dataclass
class OwnershipAnalysis:
    """Ownership data-flow result for one function."""

    cfg: ControlFlowGraph
    dataflow: DataFlowResult
    findings: list[OwnershipFinding]


def analyze_function_ownership(func, source_bytes: bytes, semantics) -> OwnershipAnalysis:
    """Run a small ownership data-flow analysis for one extracted function."""
    cfg = build_function_cfg(func, source_bytes)

    def transfer(node, state):
        return transfer_ownership_node(node, state, semantics)

    result = analyze_forward(
        cfg,
        OwnershipState(),
        transfer,
        merge_ownership_states,
        edge_transfer=lambda edge, state: refine_ownership_edge(cfg, edge, state),
        # Convergence is linear in node count (~1.25x); scale the cap with the
        # CFG so large but well-behaved functions are not falsely rejected.
        max_iterations=max(1000, len(cfg.nodes) * 100),
    )
    return OwnershipAnalysis(
        cfg=cfg,
        dataflow=result,
        findings=_find_return_leaks(func["name"], cfg, result),
    )


def transfer_ownership_node(node, state: OwnershipState, semantics) -> OwnershipState:
    """Apply ownership effects for one CFG node."""
    if node.kind in {"declaration", "statement"}:
        return _transfer_statement(node.text, node.start_line, state, semantics)
    if node.kind == "if":
        return _transfer_condition_calls(
            node.condition or "",
            node.start_line,
            state,
            semantics,
        )
    if node.kind == "return":
        value = _return_value(node.text)
        if value and value != "NULL" and _is_simple_name(value):
            return state.mark_reference(value, RETURNED, line=node.start_line)
    return state


def refine_ownership_edge(
    cfg: ControlFlowGraph,
    edge,
    state: OwnershipState,
) -> OwnershipState:
    """Refine ownership state along true/false branches."""
    source = cfg.nodes[edge.source]
    if source.kind != "if" or edge.kind not in {"true", "false"}:
        return state

    null_var, null_edge = _null_check(source.condition or "")
    if null_var and edge.kind == null_edge:
        return state.mark_reference(null_var, NULL, line=source.start_line)
    return state


def _transfer_statement(
    text: str,
    line: int,
    state: OwnershipState,
    semantics,
) -> OwnershipState:
    assigned = _assigned_call(text)
    if assigned:
        variable, api, arguments = assigned
        if api in semantics.new_ref_apis or api in semantics.borrowed_ref_apis:
            return _mark_acquired(state, variable, api, line, semantics)
        return _transfer_call(api, arguments, line, state, semantics)

    assigned_name = _assigned_name(text, state)
    if assigned_name:
        variable, target = assigned_name
        return state.alias(variable, target)

    escaped = _escaped_assignment(text, state)
    if escaped:
        return state.mark_reference(escaped, ESCAPED, line=line)

    declared = _declared_variable(text)
    if declared:
        return state.mark(declared, UNKNOWN, line=line)

    call = _simple_call(text)
    if call:
        api, arguments = call
        return _transfer_call(api, arguments, line, state, semantics)
    return state


def _transfer_condition_calls(
    condition: str,
    line: int,
    state: OwnershipState,
    semantics,
) -> OwnershipState:
    # References can be acquired inside a condition, e.g.
    # ``if ((obj = PyList_New(0)) == NULL)`` or ``if (!(obj = PyDict_New()))``.
    for variable, api, _ in _assigned_calls(condition):
        state = _mark_acquired(state, variable, api, line, semantics)
    for api, arguments in _calls(condition):
        state = _transfer_call(api, arguments, line, state, semantics)
    return state


def _mark_acquired(
    state: OwnershipState,
    variable: str,
    api: str,
    line: int,
    semantics,
) -> OwnershipState:
    if api in semantics.new_ref_apis:
        return state.mark(variable, OWNED, line=line, api=api)
    if api in semantics.borrowed_ref_apis:
        return state.mark(variable, BORROWED, line=line, api=api)
    return state


def _transfer_call(
    api: str,
    arguments: str,
    line: int,
    state: OwnershipState,
    semantics,
) -> OwnershipState:
    if api in semantics.release_apis:
        variable = _first_simple_arg(arguments)
        if variable:
            return state.mark_reference(variable, RELEASED, line=line, api=api)
    if api in semantics.incref_apis:
        # Acquiring an owned reference on a value we were only borrowing.
        # Restricted to borrowed values so we never fabricate ownership for
        # parameters or singletons (e.g. Py_INCREF(Py_None)).
        variable = _first_simple_arg(arguments)
        if variable and state.get(variable).state == BORROWED:
            return state.mark_reference(variable, OWNED, line=line, api=api)
    if api in semantics.steal_ref_apis:
        variable = _last_simple_arg(arguments)
        if variable:
            return state.mark_reference(variable, STOLEN, line=line, api=api)
    return state


def _find_return_leaks(
    function: str,
    cfg: ControlFlowGraph,
    result: DataFlowResult,
) -> list[OwnershipFinding]:
    findings = []
    for node in cfg.nodes:
        if node.id not in result.in_states:
            continue
        # Any node that flows to the synthetic exit is a function exit point:
        # a return statement, but also Py_RETURN_NONE-style macros (parsed as
        # plain statements) and fall-through at the end of the body.
        if not any(edge.target == cfg.exit_id for edge in cfg.successors(node.id)):
            continue

        returned = _return_value(node.text)
        if node.kind == "return":
            state = result.in_states[node.id]
        else:
            state = result.out_states.get(node.id, result.in_states[node.id])
        returned_var = (
            state.resolve(returned)
            if returned and _is_simple_name(returned)
            else None
        )
        for variable, ownership in state.owned_variables().items():
            if variable == returned_var:
                continue
            findings.append(
                OwnershipFinding(
                    type="potential_leak_on_path",
                    function=function,
                    line=node.start_line,
                    variable=variable,
                    confidence="medium",
                    detail=(
                        f"Owned reference '{variable}' may reach return at line "
                        f"{node.start_line} without being released"
                    ),
                    acquire_line=ownership.line,
                    api_call=ownership.api,
                )
            )
    return findings


def _assigned_call(text: str) -> tuple[str, str, str] | None:
    if "=" not in text:
        return None
    left, right = text.split("=", 1)
    variable = _assigned_variable(left)
    call = _simple_call(right)
    if not variable or not call:
        return None
    api, arguments = call
    return variable, api, arguments


def _assigned_name(text: str, state: OwnershipState) -> tuple[str, str] | None:
    text = text.strip().rstrip(";")
    if _has_non_assignment_operator(text) or text.count("=") != 1:
        return None
    left, right = text.split("=", 1)
    target = right.strip()
    if not _is_simple_name(target):
        return None
    variable = _assigned_variable(left)
    if not variable:
        return None
    if not _looks_like_declaration(left) and not state.has(variable):
        return None
    return variable, target


def _escaped_assignment(text: str, state: OwnershipState) -> str | None:
    text = text.strip().rstrip(";")
    if _has_non_assignment_operator(text) or text.count("=") != 1:
        return None
    left, right = text.split("=", 1)
    target = right.strip()
    if not _is_simple_name(target):
        return None
    if _is_escape_lvalue(left.strip(), state):
        return target
    return None


def _declared_variable(text: str) -> str | None:
    text = text.strip().rstrip(";")
    if "=" in text or "(" in text or ")" in text:
        return None
    if "*" not in text and not re.search(r"\bPyObject\b", text):
        return None
    return _assigned_variable(text)


def _assigned_variable(text: str) -> str | None:
    if "->" in text or "." in text:
        return None
    names = re.findall(r"\b[A-Za-z_]\w*\b", text)
    return names[-1] if names else None


def _looks_like_declaration(text: str) -> bool:
    return "*" in text or bool(re.search(r"\b(PyObject|Py_ssize_t|int|long|char)\b", text))


def _is_escape_lvalue(text: str, state: OwnershipState) -> bool:
    if "->" in text or "." in text or "[" in text:
        return True
    return _is_simple_name(text) and not text.startswith("Py") and not state.has(text)


def _has_non_assignment_operator(text: str) -> bool:
    return bool(
        re.search(
            r"==|!=|<=|>=|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=",
            text,
        )
    )


def _simple_call(text: str) -> tuple[str, str] | None:
    text = text.strip().rstrip(";")
    match = re.match(r"^([A-Za-z_]\w*)\s*\((.*)\)$", text)
    if not match:
        return None
    return match.group(1), match.group(2).strip()


def _calls(text: str) -> list[tuple[str, str]]:
    return [
        (match.group(1), match.group(2).strip())
        for match in re.finditer(r"\b([A-Za-z_]\w*)\s*\(([^()]*)\)", text)
    ]


def _assigned_calls(text: str) -> list[tuple[str, str, str]]:
    """Find ``var = api(args)`` assignments embedded anywhere in an expression."""
    return [
        (match.group(1), match.group(2), match.group(3).strip())
        for match in re.finditer(
            r"\b([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*\(([^()]*)\)", text
        )
    ]


def _return_value(text: str) -> str | None:
    match = re.match(r"^return(?:\s+(.+?))?;$", text.strip())
    if not match:
        return None
    value = match.group(1)
    return value.strip() if value else None


def _first_simple_arg(arguments: str) -> str | None:
    args = [arg.strip() for arg in arguments.split(",")]
    if not args:
        return None
    return args[0] if _is_simple_name(args[0]) else None


def _last_simple_arg(arguments: str) -> str | None:
    args = [arg.strip() for arg in arguments.split(",")]
    if not args:
        return None
    arg = re.sub(r"\([^)]+\)\s*", "", args[-1]).strip()
    return arg if _is_simple_name(arg) else None


def _null_check(condition: str) -> tuple[str | None, str | None]:
    condition = condition.strip()
    if condition.startswith("(") and condition.endswith(")"):
        condition = condition[1:-1].strip()

    # Reduce an embedded acquisition so that `(obj = New()) == NULL` and
    # `!(obj = New())` are recognized as null checks on `obj`.
    condition = re.sub(
        r"([A-Za-z_]\w*)\s*=\s*[A-Za-z_]\w*\s*\([^()]*\)", r"\1", condition
    )
    condition = re.sub(r"(?<!\w)\(\s*([A-Za-z_]\w*)\s*\)", r"\1", condition)

    match = re.match(r"^([A-Za-z_]\w*)\s*==\s*NULL$", condition)
    if match:
        return match.group(1), "true"
    match = re.match(r"^NULL\s*==\s*([A-Za-z_]\w*)$", condition)
    if match:
        return match.group(1), "true"
    match = re.match(r"^([A-Za-z_]\w*)\s*!=\s*NULL$", condition)
    if match:
        return match.group(1), "false"
    match = re.match(r"^NULL\s*!=\s*([A-Za-z_]\w*)$", condition)
    if match:
        return match.group(1), "false"
    match = re.match(r"^!\s*([A-Za-z_]\w*)$", condition)
    if match:
        return match.group(1), "true"
    if _is_simple_name(condition):
        return condition, "false"
    return None, None


def _is_simple_name(text: str) -> bool:
    return bool(re.match(r"^[A-Za-z_]\w*$", text))

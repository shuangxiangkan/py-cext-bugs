"""Tests for CFG/data-flow based refcount ownership transfer."""

import sys
import unittest
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

try:
    import tree_sitter  # noqa: F401
    import tree_sitter_c  # noqa: F401
except ImportError:
    HAS_TREE_SITTER = False
else:
    HAS_TREE_SITTER = True

if HAS_TREE_SITTER:
    from analysis.parsing import extract_functions, parse_string
    from refcount.analyzer import load_refcount_semantics
    from refcount.ownership_state import BORROWED, ESCAPED, OWNED, RETURNED, STOLEN
    from refcount.ownership_transfer import analyze_function_ownership
else:
    extract_functions = None
    parse_string = None
    load_refcount_semantics = None
    analyze_function_ownership = None


RETURN_OWNED = """\
static PyObject *
ok(void)
{
    PyObject *obj = PyList_New(0);
    if (obj == NULL)
        return NULL;
    return obj;
}
"""


LEAK_ON_ERROR = """\
static PyObject *
leak(int fail)
{
    PyObject *obj = PyList_New(0);
    if (obj == NULL)
        return NULL;
    if (fail)
        return NULL;
    return obj;
}
"""


CLEANUP_GOTO = """\
static PyObject *
cleanup(int fail)
{
    PyObject *obj = PyList_New(0);
    if (!obj)
        goto error;
    if (fail)
        goto error;
    return obj;
error:
    Py_XDECREF(obj);
    return NULL;
}
"""


BORROWED_RETURN_NULL = """\
static PyObject *
borrowed(PyObject *list)
{
    PyObject *item = PyList_GET_ITEM(list, 0);
    return NULL;
}
"""


LEAK_BEFORE_RETURN_NONE = """\
static PyObject *
leaky_none(void)
{
    PyObject *obj = PyList_New(0);
    if (obj == NULL)
        return NULL;
    Py_RETURN_NONE;
}
"""


COND_ASSIGN_LEAK = """\
static PyObject *
cond_assign(int fail)
{
    PyObject *obj;
    if ((obj = PyList_New(0)) == NULL)
        return NULL;
    if (fail)
        return NULL;
    return obj;
}
"""


COND_ASSIGN_BANG_OK = """\
static PyObject *
cond_assign_bang(void)
{
    PyObject *obj;
    if (!(obj = PyDict_New()))
        return NULL;
    return obj;
}
"""


INCREF_BORROW_LEAK = """\
static PyObject *
incref_borrow_leak(PyObject *list)
{
    PyObject *item = PyList_GET_ITEM(list, 0);
    Py_INCREF(item);
    Py_RETURN_NONE;
}
"""


INCREF_BORROW_RETURN = """\
static PyObject *
incref_borrow_return(PyObject *list)
{
    PyObject *item = PyList_GET_ITEM(list, 0);
    Py_INCREF(item);
    return item;
}
"""


STEAL_CALL = """\
static PyObject *
steal(PyObject *list)
{
    PyObject *item = PyLong_FromLong(42);
    if (item == NULL)
        return NULL;
    PyList_SetItem(list, 0, item);
    return NULL;
}
"""


VOID_RELEASE_LAST = """\
static void
release_last(void)
{
    PyObject *obj = PyList_New(0);
    Py_DECREF(obj);
}
"""


VOID_STEAL_LAST = """\
static void
steal_last(PyObject *list)
{
    PyObject *item = PyLong_FromLong(42);
    PyList_SetItem(list, 0, item);
}
"""


ALIAS_RELEASE = """\
static PyObject *
alias_release(void)
{
    PyObject *obj = PyList_New(0);
    PyObject *alias = obj;
    Py_DECREF(alias);
    Py_RETURN_NONE;
}
"""


ALIAS_RETURN = """\
static PyObject *
alias_return(void)
{
    PyObject *obj = PyList_New(0);
    PyObject *alias = obj;
    return alias;
}
"""


ALIAS_STEAL = """\
static PyObject *
alias_steal(PyObject *list)
{
    PyObject *obj = PyLong_FromLong(42);
    PyObject *alias = obj;
    PyList_SetItem(list, 0, alias);
    Py_RETURN_NONE;
}
"""


FIELD_ESCAPE = """\
typedef struct {
    PyObject *cached;
} Holder;

static int
field_escape(Holder *self)
{
    PyObject *obj = PyList_New(0);
    self->cached = obj;
    return 0;
}
"""


GLOBAL_ESCAPE = """\
static PyObject *global_cached;

static int
global_escape(void)
{
    PyObject *obj = PyList_New(0);
    global_cached = obj;
    return 0;
}
"""


ARRAY_ESCAPE = """\
static int
array_escape(PyObject **items)
{
    PyObject *obj = PyList_New(0);
    items[0] = obj;
    return 0;
}
"""


ALIAS_ESCAPE = """\
typedef struct {
    PyObject *cached;
} Holder;

static int
alias_escape(Holder *self)
{
    PyObject *obj = PyList_New(0);
    PyObject *alias = obj;
    self->cached = alias;
    return 0;
}
"""


RETURN_NEW_CALL = """\
static PyObject *
return_new_call(void)
{
    return PyUnicode_FromString("hello");
}
"""


RETURN_NEWREF_BORROWED = """\
static PyObject *
return_newref_borrowed(PyObject *list)
{
    PyObject *item = PyList_GET_ITEM(list, 0);
    return Py_NewRef(item);
}
"""


RETURN_NEWREF_ALIAS = """\
static PyObject *
return_newref_alias(PyObject *list)
{
    PyObject *item = PyList_GET_ITEM(list, 0);
    PyObject *alias = item;
    return Py_NewRef(alias);
}
"""


@unittest.skipUnless(
    HAS_TREE_SITTER,
    "tree-sitter and tree-sitter-c are required for ownership-flow tests",
)
class TestOwnershipFlow(unittest.TestCase):
    """Test ownership transfer over CFG/data-flow."""

    def _analyze(self, code):
        source_bytes = code.encode("utf-8")
        tree = parse_string(code)
        functions = extract_functions(tree, source_bytes)
        self.assertEqual(len(functions), 1)
        return analyze_function_ownership(
            functions[0],
            source_bytes,
            load_refcount_semantics(),
        )

    def _node_containing(self, analysis, text, kind=None):
        matches = [node for node in analysis.cfg.nodes if text in node.text]
        if kind is not None:
            matches = [node for node in matches if node.kind == kind]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_returned_owned_reference_is_not_reported_as_leak(self):
        analysis = self._analyze(RETURN_OWNED)
        ret = self._node_containing(analysis, "return obj;", "return")

        self.assertEqual(analysis.findings, [])
        self.assertEqual(analysis.dataflow.in_states[ret.id].get("obj").state, OWNED)
        self.assertEqual(analysis.dataflow.out_states[ret.id].get("obj").state, RETURNED)

    def test_owned_reference_reaching_error_return_is_reported(self):
        analysis = self._analyze(LEAK_ON_ERROR)

        findings = [finding for finding in analysis.findings if finding.variable == "obj"]

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].type, "potential_leak_on_path")
        self.assertEqual(findings[0].api_call, "PyList_New")

    def test_null_guard_does_not_report_failed_allocation_path(self):
        analysis = self._analyze(RETURN_OWNED)
        null_return = self._node_containing(analysis, "return NULL;", "return")

        self.assertEqual(
            analysis.dataflow.in_states[null_return.id].get("obj").state,
            "null",
        )
        self.assertEqual(analysis.findings, [])

    def test_goto_cleanup_release_suppresses_error_path_leak(self):
        analysis = self._analyze(CLEANUP_GOTO)

        self.assertEqual(analysis.findings, [])

    def test_borrowed_reference_is_not_reported_as_leak(self):
        analysis = self._analyze(BORROWED_RETURN_NULL)
        ret = self._node_containing(analysis, "return NULL;", "return")

        self.assertEqual(analysis.dataflow.in_states[ret.id].get("item").state, BORROWED)
        self.assertEqual(analysis.findings, [])

    def test_leak_before_py_return_none_is_reported(self):
        analysis = self._analyze(LEAK_BEFORE_RETURN_NONE)

        findings = [finding for finding in analysis.findings if finding.variable == "obj"]

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].type, "potential_leak_on_path")
        self.assertEqual(findings[0].api_call, "PyList_New")

    def test_condition_assignment_is_tracked_as_owned(self):
        analysis = self._analyze(COND_ASSIGN_LEAK)

        findings = [finding for finding in analysis.findings if finding.variable == "obj"]

        # Leaked on the `if (fail)` path, but not falsely reported on the
        # allocation-failure path guarded by `(obj = ...) == NULL`.
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].api_call, "PyList_New")

    def test_condition_assignment_bang_guard_has_no_false_positive(self):
        analysis = self._analyze(COND_ASSIGN_BANG_OK)

        self.assertEqual(analysis.findings, [])
        ret = self._node_containing(analysis, "return obj;", "return")
        self.assertEqual(analysis.dataflow.in_states[ret.id].get("obj").state, OWNED)

    def test_incref_on_borrowed_then_leaked_is_reported(self):
        analysis = self._analyze(INCREF_BORROW_LEAK)

        findings = [finding for finding in analysis.findings if finding.variable == "item"]

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].api_call, "Py_INCREF")

    def test_incref_on_borrowed_then_returned_is_not_reported(self):
        analysis = self._analyze(INCREF_BORROW_RETURN)
        ret = self._node_containing(analysis, "return item;", "return")

        self.assertEqual(analysis.dataflow.in_states[ret.id].get("item").state, OWNED)
        self.assertEqual(analysis.findings, [])

    def test_steal_api_marks_argument_stolen(self):
        analysis = self._analyze(STEAL_CALL)
        returns = [
            node
            for node in analysis.cfg.nodes
            if node.kind == "return"
            and node.text == "return NULL;"
            and analysis.dataflow.in_states[node.id].get("item").state == STOLEN
        ]

        self.assertEqual(len(returns), 1)
        self.assertEqual(analysis.findings, [])

    def test_final_release_statement_uses_output_state_at_fallthrough_exit(self):
        analysis = self._analyze(VOID_RELEASE_LAST)

        self.assertEqual(analysis.findings, [])

    def test_final_steal_statement_uses_output_state_at_fallthrough_exit(self):
        analysis = self._analyze(VOID_STEAL_LAST)

        self.assertEqual(analysis.findings, [])

    def test_alias_release_suppresses_original_leak(self):
        analysis = self._analyze(ALIAS_RELEASE)

        self.assertEqual(analysis.findings, [])

    def test_alias_return_suppresses_original_leak(self):
        analysis = self._analyze(ALIAS_RETURN)
        ret = self._node_containing(analysis, "return alias;", "return")

        self.assertEqual(analysis.dataflow.out_states[ret.id].get("obj").state, RETURNED)
        self.assertEqual(analysis.findings, [])

    def test_alias_steal_suppresses_original_leak(self):
        analysis = self._analyze(ALIAS_STEAL)

        self.assertEqual(analysis.findings, [])

    def test_field_assignment_marks_owned_reference_escaped(self):
        analysis = self._analyze(FIELD_ESCAPE)
        ret = self._node_containing(analysis, "return 0;", "return")

        self.assertEqual(analysis.dataflow.in_states[ret.id].get("obj").state, ESCAPED)
        self.assertEqual(analysis.findings, [])

    def test_global_assignment_marks_owned_reference_escaped(self):
        analysis = self._analyze(GLOBAL_ESCAPE)
        ret = self._node_containing(analysis, "return 0;", "return")

        self.assertEqual(analysis.dataflow.in_states[ret.id].get("obj").state, ESCAPED)
        self.assertEqual(analysis.findings, [])

    def test_array_assignment_marks_owned_reference_escaped(self):
        analysis = self._analyze(ARRAY_ESCAPE)
        ret = self._node_containing(analysis, "return 0;", "return")

        self.assertEqual(analysis.dataflow.in_states[ret.id].get("obj").state, ESCAPED)
        self.assertEqual(analysis.findings, [])

    def test_alias_escape_marks_original_reference_escaped(self):
        analysis = self._analyze(ALIAS_ESCAPE)
        ret = self._node_containing(analysis, "return 0;", "return")

        self.assertEqual(analysis.dataflow.in_states[ret.id].get("obj").state, ESCAPED)
        self.assertEqual(analysis.findings, [])

    def test_direct_return_new_ref_call_has_no_false_positive(self):
        analysis = self._analyze(RETURN_NEW_CALL)

        self.assertEqual(analysis.findings, [])

    def test_return_py_newref_on_borrowed_has_no_false_positive(self):
        analysis = self._analyze(RETURN_NEWREF_BORROWED)
        ret = self._node_containing(analysis, "return Py_NewRef(item);", "return")

        # A new ref is returned to a borrowed value; the borrow itself is never
        # owned, so it must not be flagged as a leak.
        self.assertEqual(analysis.dataflow.in_states[ret.id].get("item").state, BORROWED)
        self.assertEqual(analysis.findings, [])

    def test_return_py_newref_on_borrowed_alias_has_no_false_positive(self):
        analysis = self._analyze(RETURN_NEWREF_ALIAS)
        ret = self._node_containing(analysis, "return Py_NewRef(alias);", "return")

        # `alias` resolves to the borrowed `item`; still no leak.
        self.assertEqual(analysis.dataflow.in_states[ret.id].get("alias").state, BORROWED)
        self.assertEqual(analysis.findings, [])


if __name__ == "__main__":
    unittest.main()

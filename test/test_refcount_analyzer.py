"""Tests for refcount.analyzer."""

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
    import refcount.analyzer as refcounts
else:
    refcounts = None
from helpers import (
    EXTENSION_WITH_BUGS,
    MINIMAL_EXTENSION,
    TempSourceTree,
)


LEAK_ON_ERROR = """\
#include <Python.h>

static PyObject *
leaky_error(PyObject *self, PyObject *args)
{
    PyObject *first = PyList_New(0);
    if (first == NULL)
        return NULL;

    PyObject *second = PyDict_New();
    if (second == NULL) {
        /* BUG: first is leaked here */
        return NULL;
    }
    Py_DECREF(first);
    Py_DECREF(second);
    Py_RETURN_NONE;
}
"""


CLEAN_REFCOUNTS = """\
#include <Python.h>

static PyObject *
clean_func(PyObject *self, PyObject *args)
{
    PyObject *result = PyList_New(0);
    if (result == NULL)
        return NULL;

    PyObject *item = PyLong_FromLong(42);
    if (item == NULL) {
        Py_DECREF(result);
        return NULL;
    }

    if (PyList_Append(result, item) < 0) {
        Py_DECREF(item);
        Py_DECREF(result);
        return NULL;
    }
    Py_DECREF(item);
    return result;
}
"""


STOLEN_REF_CODE = """\
#include <Python.h>

static PyObject *
correct_steal(PyObject *self, PyObject *args)
{
    PyObject *list = PyList_New(1);
    if (list == NULL)
        return NULL;
    PyObject *item = PyLong_FromLong(42);
    if (item == NULL) {
        Py_DECREF(list);
        return NULL;
    }
    PyList_SetItem(list, 0, item);
    /* item is stolen -- don't touch it */
    return list;
}
"""


MACRO_WRAPPED = """\
#include <Python.h>

#define STATS(x) x

static PyObject *
macro_wrapped(PyObject *self, PyObject *args)
{
    PyObject *result;
    STATS( result = PyDict_New() );
    if (result == NULL)
        return NULL;
    return result;
}
"""


SETITEM_DOUBLE_FREE = """\
#include <Python.h>

static PyObject *
setitem_double_free(PyObject *self, PyObject *args)
{
    PyObject *list = PyList_New(1);
    if (list == NULL)
        return NULL;

    PyObject *item = PyLong_FromLong(42);
    if (item == NULL) {
        Py_DECREF(list);
        return NULL;
    }

    if (PyList_SetItem(list, 0, item) < 0) {
        Py_DECREF(item);  /* BUG: double-free -- SetItem always steals */
        Py_DECREF(list);
        return NULL;
    }
    return list;
}
"""


SETITEM_CORRECT = """\
#include <Python.h>

static PyObject *
setitem_correct(PyObject *self, PyObject *args)
{
    PyObject *list = PyList_New(1);
    if (list == NULL)
        return NULL;

    PyObject *item = PyLong_FromLong(42);
    if (item == NULL) {
        Py_DECREF(list);
        return NULL;
    }

    if (PyList_SetItem(list, 0, item) < 0) {
        /* item was already stolen -- don't DECREF it */
        Py_DECREF(list);
        return NULL;
    }
    return list;
}
"""


@unittest.skipUnless(
    HAS_TREE_SITTER,
    "tree-sitter and tree-sitter-c are required for refcount analyzer tests",
)
class TestRefcountAnalyzer(unittest.TestCase):
    """Test reference counting error detection."""

    def test_borrowed_ref_across_call(self):
        with TempSourceTree({"buggy.c": EXTENSION_WITH_BUGS}) as root:
            result = refcounts.analyze_path(root / "buggy.c")
            types = [finding["type"] for finding in result["findings"]]
            self.assertIn("borrowed_ref_across_call", types)
            borrow = [
                finding
                for finding in result["findings"]
                if finding["type"] == "borrowed_ref_across_call"
            ][0]
            self.assertEqual(borrow["confidence"], "high")
            self.assertIn("item", borrow["borrowed_var"])

    def test_clean_code_no_serious_findings(self):
        with TempSourceTree({"clean.c": CLEAN_REFCOUNTS}) as root:
            result = refcounts.analyze_path(root / "clean.c")
            serious = [
                finding
                for finding in result["findings"]
                if finding["type"]
                in ("borrowed_ref_across_call", "stolen_ref_not_nulled")
            ]
            self.assertEqual(len(serious), 0)

    def test_leak_on_error_path(self):
        with TempSourceTree({"leak.c": LEAK_ON_ERROR}) as root:
            result = refcounts.analyze_path(root / "leak.c")
            types = [finding["type"] for finding in result["findings"]]
            self.assertTrue(
                "potential_leak_on_path" in types
                or "potential_leak_on_error" in types
                or "potential_leak" in types
            )

    def test_dataflow_path_leak_is_reported(self):
        with TempSourceTree({"leak.c": LEAK_ON_ERROR}) as root:
            result = refcounts.analyze_path(root / "leak.c")
            path_leaks = [
                finding
                for finding in result["findings"]
                if finding["type"] == "potential_leak_on_path"
            ]
            self.assertTrue(path_leaks)
            self.assertEqual(path_leaks[0]["variable"], "first")
            self.assertEqual(path_leaks[0]["api_call"], "PyList_New")

    def test_clean_code_no_dataflow_path_leak(self):
        with TempSourceTree({"clean.c": CLEAN_REFCOUNTS}) as root:
            result = refcounts.analyze_path(root / "clean.c")
            path_leaks = [
                finding
                for finding in result["findings"]
                if finding["type"] == "potential_leak_on_path"
            ]
            self.assertEqual(path_leaks, [])

    def test_correct_stolen_ref(self):
        with TempSourceTree({"steal.c": STOLEN_REF_CODE}) as root:
            result = refcounts.analyze_path(root / "steal.c")
            stolen = [
                finding
                for finding in result["findings"]
                if finding["type"] == "stolen_ref_not_nulled"
            ]
            self.assertEqual(len(stolen), 0)

    def test_minimal_extension_runs(self):
        with TempSourceTree({"myext.c": MINIMAL_EXTENSION}) as root:
            result = refcounts.analyze_path(root / "myext.c")
            self.assertGreaterEqual(result["functions_analyzed"], 2)
            self.assertIn("findings", result)
            self.assertIn("summary", result)

    def test_output_envelope(self):
        with TempSourceTree({"buggy.c": EXTENSION_WITH_BUGS}) as root:
            result = refcounts.analyze_path(root / "buggy.c")
            self.assertIn("project_root", result)
            self.assertIn("scan_root", result)
            self.assertIn("functions_analyzed", result)
            self.assertIn("findings", result)
            self.assertIn("summary", result)
            self.assertIn("total_findings", result["summary"])
            self.assertIn("by_type", result["summary"])


@unittest.skipUnless(
    HAS_TREE_SITTER,
    "tree-sitter and tree-sitter-c are required for refcount analyzer tests",
)
class TestStolenRefDoubleFree(unittest.TestCase):
    """Test detection of releases after always-stealing APIs."""

    def test_detects_setitem_double_free(self):
        with TempSourceTree({"df.c": SETITEM_DOUBLE_FREE}) as root:
            result = refcounts.analyze_path(root / "df.c")
            types = [finding["type"] for finding in result["findings"]]
            self.assertIn("stolen_ref_double_free", types)
            finding = [
                item
                for item in result["findings"]
                if item["type"] == "stolen_ref_double_free"
            ][0]
            self.assertEqual(finding["confidence"], "high")
            self.assertEqual(finding["variable"], "item")
            self.assertEqual(finding["steal_api"], "PyList_SetItem")

    def test_correct_setitem_no_finding(self):
        with TempSourceTree({"ok.c": SETITEM_CORRECT}) as root:
            result = refcounts.analyze_path(root / "ok.c")
            double_frees = [
                finding
                for finding in result["findings"]
                if finding["type"] == "stolen_ref_double_free"
            ]
            self.assertEqual(len(double_frees), 0)


@unittest.skipUnless(
    HAS_TREE_SITTER,
    "tree-sitter and tree-sitter-c are required for refcount analyzer tests",
)
class TestMacroWrapped(unittest.TestCase):
    """Test handling of macro-wrapped variable names."""

    def test_macro_wrapped_assignment(self):
        with TempSourceTree({"ext.c": MACRO_WRAPPED}) as root:
            result = refcounts.analyze_path(root / "ext.c")
            for finding in result["findings"]:
                if "variable" in finding:
                    self.assertNotIn("STATS", finding["variable"])
                    self.assertNotIn("(", finding["variable"])


if __name__ == "__main__":
    unittest.main()

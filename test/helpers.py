"""Test helpers and C fixtures for py-cext-bugs refcount tests."""

import shutil
import tempfile
from pathlib import Path


class TempSourceTree:
    """Create a temporary source tree for scanner tests."""

    def __init__(self, files: dict[str, str]):
        self.files = files
        self._tmpdir = None

    def __enter__(self) -> Path:
        self._tmpdir = tempfile.mkdtemp(prefix="py_cext_bugs_test_")
        root = Path(self._tmpdir)
        for rel_path, content in self.files.items():
            path = root / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return root

    def __exit__(self, *args):
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)


MINIMAL_EXTENSION = """\
#include <Python.h>

static PyObject *
myext_hello(PyObject *self, PyObject *args)
{
    return PyUnicode_FromString("hello");
}

static PyMethodDef myext_methods[] = {
    {"hello", myext_hello, METH_NOARGS, "Say hello."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef myext_module = {
    PyModuleDef_HEAD_INIT,
    "myext",
    NULL,
    -1,
    myext_methods
};

PyMODINIT_FUNC
PyInit_myext(void)
{
    return PyModule_Create(&myext_module);
}
"""


SETUP_PY_TEMPLATE = """\
from setuptools import setup, Extension

setup(
    name="{name}",
    ext_modules=[
        Extension("{name}", sources={sources}),
    ],
    python_requires="{python_requires}",
)
"""


EXTENSION_WITH_BUGS = """\
#include <Python.h>

static PyObject *
leaky_function(PyObject *self, PyObject *args)
{
    PyObject *result = PyList_New(0);
    if (result == NULL)
        return NULL;

    PyObject *item = PyLong_FromLong(42);
    /* BUG: if Append fails, item is leaked */
    if (PyList_Append(result, item) < 0) {
        Py_DECREF(result);
        return NULL;
    }
    Py_DECREF(item);
    return result;
}

static PyObject *
borrowed_ref_bug(PyObject *self, PyObject *args)
{
    PyObject *list;
    if (!PyArg_ParseTuple(args, "O", &list))
        return NULL;

    /* BUG: borrowed ref from GET_ITEM, then callback into Python */
    PyObject *item = PyList_GET_ITEM(list, 0);
    PyObject *str_item = PyObject_Str(item);
    if (str_item == NULL)
        return NULL;

    /* Using item after it may have been invalidated */
    PyObject *result = PyObject_RichCompare(item, str_item, Py_EQ);
    Py_DECREF(str_item);
    return result;
}
"""

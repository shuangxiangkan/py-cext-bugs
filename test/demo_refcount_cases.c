#include <Python.h>

static PyObject *
demo_leak_on_error(PyObject *self, PyObject *args)
{
    PyObject *first = PyList_New(0);
    if (first == NULL)
        return NULL;

    PyObject *second = PyDict_New();
    if (second == NULL) {
        return NULL;
    }

    Py_DECREF(first);
    Py_DECREF(second);
    Py_RETURN_NONE;
}

static PyObject *
demo_borrowed_ref_across_call(PyObject *self, PyObject *args)
{
    PyObject *list;
    if (!PyArg_ParseTuple(args, "O", &list))
        return NULL;

    PyObject *item = PyList_GET_ITEM(list, 0);
    PyObject *text = PyObject_Str(item);
    if (text == NULL)
        return NULL;

    PyObject *result = PyObject_RichCompare(item, text, Py_EQ);
    Py_DECREF(text);
    return result;
}

static PyObject *
demo_stolen_ref_double_free(PyObject *self, PyObject *args)
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
        Py_DECREF(item);
        Py_DECREF(list);
        return NULL;
    }

    return list;
}

static PyObject *
demo_clean(PyObject *self, PyObject *args)
{
    PyObject *result = PyList_New(0);
    if (result == NULL)
        return NULL;

    PyObject *item = PyLong_FromLong(7);
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

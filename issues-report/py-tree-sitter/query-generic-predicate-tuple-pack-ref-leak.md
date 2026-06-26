# Reference leak when building generic predicate arguments

`query_new()` builds predicate argument tuples with temporary unicode objects
created directly inside `PyTuple_Pack()`.

File: `tree_sitter/binding/query.c`

Function: `query_new`

```c
item = PyTuple_Pack(2, PyUnicode_FromStringAndSize(arg_value, length),
                    PyUnicode_FromString("capture"));
```

and:

```c
item = PyTuple_Pack(2, PyUnicode_FromStringAndSize(arg_value, length),
                    PyUnicode_FromString("string"));
```

`PyUnicode_FromStringAndSize()` and `PyUnicode_FromString()` return new
references. `PyTuple_Pack()` does not steal them, so the temporary unicode
references are leaked.

Suggested fix: store the two unicode objects in locals, check them, pass them to
`PyTuple_Pack()`, then decref the locals.

```c
PyObject *value = PyUnicode_FromStringAndSize(arg_value, length);
PyObject *kind = PyUnicode_FromString("capture");
if (value == NULL || kind == NULL) {
    Py_XDECREF(value);
    Py_XDECREF(kind);
    goto error;
}
item = PyTuple_Pack(2, value, kind);
Py_DECREF(value);
Py_DECREF(kind);
```

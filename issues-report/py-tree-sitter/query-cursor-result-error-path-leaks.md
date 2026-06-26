# Partial result leaks in query cursor error paths

`query_cursor_matches()` and `query_cursor_captures()` can return `NULL` without
releasing their partially-built result containers.

File: `tree_sitter/binding/query_cursor.c`

Functions: `query_cursor_matches`, `query_cursor_captures`

Examples:

```c
PyObject *result = PyList_New(0);
...
return PyErr_Occurred() == NULL ? result : NULL;
```

```c
PyObject *result = PyDict_New();
...
if (PyErr_Occurred()) {
    return NULL;
}
...
return PyErr_Occurred() == NULL ? result : NULL;
```

If an error is set after `result` is created, these paths return `NULL` and
leak the owned list/dict.

Suggested fix:

```c
if (PyErr_Occurred() != NULL) {
    Py_DECREF(result);
    return NULL;
}
return result;
```

The early `return NULL` in `query_cursor_captures()` should also decref
`result` before returning.

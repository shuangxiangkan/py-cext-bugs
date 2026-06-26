# Reference leak in query cursor progress callback

`query_cursor_progress_callback()` leaks the Python callback result.

File: `tree_sitter/binding/query_cursor.c`

Function: `query_cursor_progress_callback`

```c
static bool query_cursor_progress_callback(TSQueryCursorState *state) {
    PyObject *result =
        PyObject_CallFunction((PyObject *)state->payload, "I", state->current_byte_offset);
    return PyObject_IsTrue(result);
}
```

`PyObject_CallFunction()` returns a new reference. `PyObject_IsTrue()` does not
consume that reference, so each successful progress callback leaks `result`.

Suggested fix:

```c
PyObject *result = PyObject_CallFunction(...);
if (result == NULL) {
    return false;
}
int truth = PyObject_IsTrue(result);
Py_DECREF(result);
return truth > 0;
```

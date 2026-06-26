# Reference leak in parser progress callback

`parser_progress_callback()` leaks the Python callback result.

File: `tree_sitter/binding/parser.c`

Function: `parser_progress_callback`

```c
static bool parser_progress_callback(TSParseState *state) {
    PyObject *result = PyObject_CallFunction((PyObject *)state->payload, "Ip",
                                             state->current_byte_offset, state->has_error);
    return PyObject_IsTrue(result);
}
```

`PyObject_CallFunction()` returns a new reference. `PyObject_IsTrue()` only reads
the object; it does not steal or release it. The callback result should be
decref'd after the truth value is computed.

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

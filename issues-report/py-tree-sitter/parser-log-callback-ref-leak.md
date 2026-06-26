# Reference leaks in parser logger callback

`log_callback()` leaks both the `LogType` enum object and the Python callback
return value.

File: `tree_sitter/binding/parser.c`

Function: `log_callback`

```c
PyObject *log_type_enum =
    PyObject_CallFunction((PyObject *)logger_payload->log_type_type, "i", log_type);
PyObject_CallFunction(logger_payload->callback, "Os", log_type_enum, buffer);
```

Both calls return new references:

- `log_type_enum` from the enum constructor
- the return value from `logger_payload->callback`

Neither reference is released.

Suggested fix:

```c
PyObject *log_type_enum = PyObject_CallFunction(...);
if (log_type_enum == NULL) {
    return;
}
PyObject *result = PyObject_CallFunction(logger_payload->callback, "Os", log_type_enum, buffer);
Py_DECREF(log_type_enum);
Py_XDECREF(result);
```

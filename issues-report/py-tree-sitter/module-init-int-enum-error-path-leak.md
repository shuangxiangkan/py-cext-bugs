# Error-path leak of `enum.IntEnum` during module initialization

`PyInit__binding()` can leak the imported `enum.IntEnum` object if a later step
fails before `Py_DECREF(int_enum)`.

File: `tree_sitter/binding/module.c`

Function: `PyInit__binding`

```c
PyObject *int_enum = import_attribute("enum", "IntEnum");
if (int_enum == NULL) {
    goto cleanup;
}
state->log_type_type = (PyTypeObject *)PyObject_CallFunction(
    int_enum, "s{sisi}", "LogType", "PARSE", TSLogTypeParse, "LEX", TSLogTypeLex);
if (state->log_type_type == NULL ||
    PyModule_AddObjectRef(module, "LogType", (PyObject *)state->log_type_type) < 0) {
    goto cleanup;
};
Py_DECREF(int_enum);
```

If `PyObject_CallFunction()` or `PyModule_AddObjectRef()` fails, execution jumps
to `cleanup` before `int_enum` is decref'd.

Suggested fix: decref `int_enum` before the failure jump, or use a cleanup label
that includes `Py_XDECREF(int_enum)`.

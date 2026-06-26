# Error-path leak in parser read callback argument construction

`parser_read_wrapper()` can leak one callback argument object if the other
argument allocation fails.

File: `tree_sitter/binding/parser.c`

Function: `parser_read_wrapper`

```c
PyObject *byte_offset_obj = PyLong_FromUnsignedLong(byte_offset);
PyObject *position_obj = point_new_internal(wrapper_payload->state, position);
if (!position_obj || !byte_offset_obj) {
    *bytes_read = 0;
    return NULL;
}
```

If one allocation succeeds and the other fails, the successful object is still
owned by this function but is not released.

Suggested fix:

```c
if (!position_obj || !byte_offset_obj) {
    Py_XDECREF(byte_offset_obj);
    Py_XDECREF(position_obj);
    *bytes_read = 0;
    return NULL;
}
```

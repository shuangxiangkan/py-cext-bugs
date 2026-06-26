# Reference leak in `Point.__new__`

`point_new()` leaks the temporary integer objects passed to `PyTuple_Pack()`.

File: `tree_sitter/binding/point.c`

Function: `point_new`

```c
PyObject *row_obj = PyLong_FromUnsignedLong(row), *col_obj = PyLong_FromUnsignedLong(column);
PyObject *self = PyTuple_Pack(2, row_obj, col_obj);
if (!self) {
    return NULL;
}
Py_SET_TYPE(self, type);
return self;
```

`PyLong_FromUnsignedLong()` returns new references. `PyTuple_Pack()` does not
steal them; it stores its own references in the tuple. The local references to
`row_obj` and `col_obj` should be released after `PyTuple_Pack()`.

Suggested fix:

```c
PyObject *row_obj = PyLong_FromUnsignedLong(row);
PyObject *col_obj = PyLong_FromUnsignedLong(column);
if (row_obj == NULL || col_obj == NULL) {
    Py_XDECREF(row_obj);
    Py_XDECREF(col_obj);
    return NULL;
}
PyObject *self = PyTuple_Pack(2, row_obj, col_obj);
Py_DECREF(row_obj);
Py_DECREF(col_obj);
if (self == NULL) {
    return NULL;
}
```

# Error-path leaks in range list builders

`tree_changed_ranges()` and `tree_get_included_ranges()` leak resources if
`PyObject_New(Range, ...)` fails inside the loop.

File: `tree_sitter/binding/tree.c`

Functions: `tree_changed_ranges`, `tree_get_included_ranges`

```c
TSRange *ranges = ts_tree_get_changed_ranges(self->tree, tree, &length);
PyObject *result = PyList_New(length);
...
Range *range = PyObject_New(Range, state->range_type);
if (range == NULL) {
    return NULL;
}
```

On this failure path, both `result` and the `ranges` buffer are still owned by
the function and should be released before returning.

The same pattern exists in `tree_get_included_ranges()` with
`ts_tree_included_ranges()`.

Suggested fix:

```c
if (range == NULL) {
    Py_DECREF(result);
    PyMem_Free(ranges);
    return NULL;
}
```

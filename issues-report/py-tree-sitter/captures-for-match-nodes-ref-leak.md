# Reference leak in `captures_for_match`

`captures_for_match()` leaks the `nodes` list inserted into the `captures`
dictionary.

File: `tree_sitter/binding/query_predicates.c`

Function: `captures_for_match`

```c
PyObject *nodes = nodes_for_capture_index(state, capture.index, match, tree);
if (PyDict_SetItem(captures, capture_name_obj, nodes) == -1) {
    return NULL;
}
Py_DECREF(capture_name_obj);
```

`nodes_for_capture_index()` returns a new reference. `PyDict_SetItem()` does not
steal it; it stores its own reference in the dict. The local `nodes` reference
should be decref'd after the successful insertion.

Suggested fix:

```c
PyObject *nodes = nodes_for_capture_index(...);
if (nodes == NULL) {
    Py_DECREF(captures);
    Py_DECREF(capture_name_obj);
    return NULL;
}
if (PyDict_SetItem(captures, capture_name_obj, nodes) == -1) {
    Py_DECREF(captures);
    Py_DECREF(capture_name_obj);
    Py_DECREF(nodes);
    return NULL;
}
Py_DECREF(capture_name_obj);
Py_DECREF(nodes);
```

# Possible Reference leak in callable-backed `node_get_text()`

I found a possible reference leak in `node_get_text()` when `Node.text` is backed by a Python source callback.

File: `tree_sitter/binding/node.c`

Function: `node_get_text`

Relevant code:

```c
PyObject *rv = PyObject_Call(tree->source, args, NULL);
Py_XDECREF(args);

PyObject *rv_bytearray = PyByteArray_FromObject(rv);
if (rv_bytearray == NULL) {
    Py_DECREF(collected_bytes);
    Py_XDECREF(rv);
    return NULL;
}

PyObject *new_collected_bytes = PyByteArray_Concat(collected_bytes, rv_bytearray);
Py_DECREF(rv_bytearray);
Py_DECREF(collected_bytes);
if (new_collected_bytes == NULL) {
    Py_XDECREF(rv);
    return NULL;
}
collected_bytes = new_collected_bytes;

size_t bytes_read = (size_t)PyBytes_Size(rv);
const char *rv_str = PyBytes_AsString(rv);
for (size_t i = 0; i < bytes_read; ++i) {
    if (rv_str[i] == '\n') {
        ++current_point.row;
        current_point.column = 0;
    } else {
        ++current_point.column;
    }
}
current_offset += bytes_read;
```

`PyObject_Call()` returns a new reference:

```c
PyObject *rv = PyObject_Call(tree->source, args, NULL);
```

The error paths correctly release `rv`:

```c
Py_XDECREF(rv);
return NULL;
```

But the successful path never decrefs it. `PyByteArray_FromObject()`,
`PyBytes_Size()`, and `PyBytes_AsString()` do not steal the reference, so one
callback result is leaked per successful chunk.

Suggested fix: decref `rv` after it is no longer needed on the success path.

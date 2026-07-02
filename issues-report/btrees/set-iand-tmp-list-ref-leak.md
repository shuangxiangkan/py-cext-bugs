# Reference leak in `set_iand()` on the `NotImplemented` fallback

I found a reference leak in `set_iand()` (the in-place `&=` operator for Set).
`tmp_list` is allocated before checking whether `other` is iterable; when
`PyObject_GetIter(other)` fails, the function returns `Py_NotImplemented` without
releasing `tmp_list`.

File: `src/BTrees/SetTemplate.c`

Function: `set_iand`

Relevant code:

```c
tmp_list = PyList_New(0);
if (tmp_list == NULL) {
    return NULL;
}

iter = PyObject_GetIter(other);
if (iter == NULL) {
    PyErr_Clear();
    Py_INCREF(Py_NotImplemented);
    return Py_NotImplemented;
}
```

`PyList_New(0)` returns a new owned reference. If `other` is not iterable,
`PyObject_GetIter(other)` returns NULL and the function returns
`Py_NotImplemented` on the early-exit path, leaking the already-created
`tmp_list`. Every `Set &= non_iterable` leaks one empty list.

Note: `SetTemplate.c` is included into every concrete Set module, so this leak is
compiled into every Set type.

Suggested fix — release `tmp_list` before the fallback return:

```c
iter = PyObject_GetIter(other);
if (iter == NULL) {
    Py_DECREF(tmp_list);
    PyErr_Clear();
    Py_INCREF(Py_NotImplemented);
    return Py_NotImplemented;
}
```

# Reference leak in `TreeSet_iand()` on the `NotImplemented` fallback

I found a reference leak in `TreeSet_iand()` (the in-place `&=` operator for
TreeSet). It has the same shape as the `set_iand()` leak: `tmp_list` is allocated
before checking whether `other` is iterable, and is never released when
`PyObject_GetIter(other)` fails.

File: `src/BTrees/TreeSetTemplate.c`

Function: `TreeSet_iand`

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

`PyList_New(0)` returns a new owned reference. When `other` is not iterable,
`PyObject_GetIter(other)` returns NULL and the function returns
`Py_NotImplemented`, leaking `tmp_list`. Every `TreeSet &= non_iterable` leaks one
empty list.

Note: `TreeSetTemplate.c` is included into every concrete TreeSet module, so this
leak is compiled into every TreeSet type.

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

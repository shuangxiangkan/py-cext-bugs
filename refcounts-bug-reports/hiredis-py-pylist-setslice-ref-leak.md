# hiredis-py: possible leaked temporary list in PushNotificationType_New

## Summary

`hiredis-py` has a likely reference leak in `PushNotificationType_New()`:

- Project: `python-c-repos/hiredis-py`
- File: `src/reader.c`
- Function: `PushNotificationType_New`
- Line: `245`
- Category: CPython reference leak
- Confidence: high candidate, should be confirmed by maintainers or tests

Both scanners reported this area:

| Tool | Files | Functions | Findings | Relevant finding |
|---|---:|---:|---:|---|
| `cext-review-toolkit` | 3 | 30 | 6 | `PushNotificationType_New`, `res` from `PyList_New()` |
| `py-cext-bugs` | 3 | 30 | 8 | `PushNotificationType_New`, `res` from `PyList_New()` |

Result files:

- `scan-results/hiredis-py-cext-review-toolkit-refcounts.json`
- `scan-results/hiredis-py-py-cext-bugs-refcount.json`

## Code

Current code:

```c
static PyObject* PushNotificationType_New(Py_ssize_t size) {
    /* Check for negative size */
    if (size < 0) {
        PyErr_SetString(PyExc_SystemError, "negative list size");
        return NULL;
    }

    /* Check for potential overflow */
    if ((size_t)size > PY_SSIZE_T_MAX / sizeof(PyObject*)) {
        return PyErr_NoMemory();
    }

#ifdef PYPY_VERSION
    PyObject* obj = PyObject_CallObject((PyObject *) &PushNotificationType, NULL);
#else
    PyObject* obj = PyType_GenericNew(&PushNotificationType, NULL, NULL);
#endif
    if (obj == NULL) {
        return NULL;
    }

   int res = PyList_SetSlice(obj, PY_SSIZE_T_MAX, PY_SSIZE_T_MAX, PyList_New(size));

   if (res == -1) {
       Py_DECREF(obj);
       return NULL;
   }

    return obj;
}
```

## Why This Looks Like A Bug

`PyList_New(size)` returns a new reference.

That new list is passed directly as the fourth argument to `PyList_SetSlice()`:

```c
PyList_SetSlice(obj, PY_SSIZE_T_MAX, PY_SSIZE_T_MAX, PyList_New(size));
```

`PyList_SetSlice()` does not steal a reference to the replacement sequence. It uses the sequence to update the target list. Therefore the temporary list returned by `PyList_New(size)` still needs to be decref'ed by the caller.

Because the temporary object is not stored in a local variable, the code has no way to call `Py_DECREF()` on it after `PyList_SetSlice()` returns. This appears to leak one empty/preallocated temporary list each time `PushNotificationType_New()` is called.

The error path also has a related issue: if `PyList_New(size)` fails, the code passes `NULL` into `PyList_SetSlice()` instead of checking allocation failure first. That makes the failure handling less direct and may rely on downstream behavior.

## Suggested Fix

Use a temporary variable and decref it after `PyList_SetSlice()`:

```c
PyObject *items = PyList_New(size);
if (items == NULL) {
    Py_DECREF(obj);
    return NULL;
}

int res = PyList_SetSlice(obj, PY_SSIZE_T_MAX, PY_SSIZE_T_MAX, items);
Py_DECREF(items);

if (res == -1) {
    Py_DECREF(obj);
    return NULL;
}
```

This preserves the current behavior while correctly releasing the caller-owned reference from `PyList_New()`.

## Notes On Other Scanner Findings

The other findings in `hiredis-py` look lower confidence:

- `createArrayObject`, `createIntegerObject`, and `createDoubleObject` return new Python objects through hiredis parser callbacks. Their ownership is managed by the hiredis reply parser contract, `freeObject()`, and `Reader_gets()`. The scanners do not model that external ownership protocol.
- `PyInit_hiredis` stores exception classes in module state. They are traversed and cleared by `hiredis_ModuleTraverse()` and `hiredis_ModuleClear()`, so those reports look like module-state lifecycle false positives rather than ordinary leaks.

The `PushNotificationType_New()` temporary list issue is the strongest candidate from this scan.

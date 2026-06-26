# yappi: possible reference leaks in context IDs and profile item metadata

## Summary

`yappi` has several likely CPython reference leaks in `yappi/_yappi.c`. The
strongest candidates are:

- `_current_context_id()` stores a newly-created `_yappi_tid` integer in a
  thread-state dict and does not release the caller-owned reference after the
  successful `PyDict_SetItemString()`.
- `_ccode2pit()` stores owned Python objects in `_pit` metadata fields, but
  `_del_pit()` only releases `pit->fn_descriptor`. In one path it also
  unnecessarily `Py_INCREF`s a freshly-owned `method_descriptor`, leaking an
  extra reference.

- Project: `python-c-repos/yappi`
- Component: hand-written C extension (`yappi/_yappi.c`)
- Category: CPython reference leak
- Confidence: high candidate for `_yappi_tid` and `method_descriptor`, medium
  candidate for broader `_pit` metadata cleanup

Both scanners reported this area:

| Tool | Files | Functions | Findings | Relevant findings |
|---|---:|---:|---:|---|
| `cext-review-toolkit` | 7 | 113 | 7 | `_current_context_id`, `_ccode2pit`, `PyInit__yappi` |
| `py-cext-bugs` | 7 | 113 | 10 | `_current_context_id`, `_ccode2pit`, `PyInit__yappi` |

Result files:

- `scan-results/yappi-cext-review-toolkit-refcounts.json`
- `scan-results/yappi-py-cext-bugs-refcount.json`

## Affected Sites

| Function | File:line | Leaked value | Confidence |
|---|---|---|---|
| `_current_context_id` | `yappi/_yappi.c:466` | `ytid` from `PyLong_FromLongLong()` | High |
| `_ccode2pit` | `yappi/_yappi.c:628` | extra ref to `method_descriptor` | High |
| `_ccode2pit` / `_del_pit` | `yappi/_yappi.c:610`, `641`, `655` | `_pit.name` / `_pit.modname` metadata refs | Medium |
| `PyInit__yappi` | `yappi/_yappi.c:2263`, `2278` | module / exception refs on init failure | Low to medium |

## 1. `_current_context_id`: leaked `_yappi_tid`

Current code:

```c
ytid = PyDict_GetItemString(ts->dict, "_yappi_tid");
if (!ytid) {
    ytid = PyLong_FromLongLong(ycurthreadindex++);
    if (!ytid) {
        PyErr_Clear();
        return 0;
    }
    if (PyDict_SetItemString(ts->dict, "_yappi_tid", ytid) < 0) {
        Py_DECREF(ytid);
        PyErr_Clear();
        return 0;
    }
}
rc = PyLong_AsVoidPtr(ytid);
```

`PyLong_FromLongLong()` returns a new reference. `PyDict_SetItemString()` does
not steal that reference; it increments/stores its own reference in the dict.
The error path decrefs `ytid`, but the success path does not.

That leaves the function-owned reference leaked for every thread/context that
creates a new `_yappi_tid`.

Suggested fix:

```c
if (PyDict_SetItemString(ts->dict, "_yappi_tid", ytid) < 0) {
    Py_DECREF(ytid);
    PyErr_Clear();
    return 0;
}
Py_DECREF(ytid);
ytid = PyDict_GetItemString(ts->dict, "_yappi_tid");
```

Alternatively, read `rc` before `Py_DECREF(ytid)` and return that value, since
`ytid` remains alive through the dict.

## 2. `_ccode2pit`: extra reference to `method_descriptor`

Current code:

```c
method_descriptor = PyObject_GetAttr(obj_type, name);
if (method_descriptor) {
    pit->fn_descriptor = method_descriptor;
    Py_INCREF(method_descriptor);
}
```

`PyObject_GetAttr()` returns a new reference. Assigning that object into
`pit->fn_descriptor` is enough to transfer ownership of that new reference to
the `_pit` metadata. The extra `Py_INCREF(method_descriptor)` creates a second
owned reference.

The cleanup function only releases `pit->fn_descriptor` once:

```c
static void
_del_pit(_pit *pit)
{
    ...
    Py_DECREF(pit->fn_descriptor);
}
```

So the extra reference created by `Py_INCREF(method_descriptor)` is not balanced
and appears to leak.

Suggested fix:

```c
method_descriptor = PyObject_GetAttr(obj_type, name);
if (method_descriptor) {
    pit->fn_descriptor = method_descriptor;  /* owns GetAttr's new ref */
}
```

## 3. `_pit` metadata cleanup may be incomplete

Manual review found a broader ownership concern around `_pit` metadata:

```c
pit->modname = _pycfunction_module_name(cfn);
...
pit->name = PyObject_Repr(mo);
...
pit->name = PyStr_FromString(cfn->m_ml->ml_name);
```

These assignments store new references in `pit->modname` and `pit->name`.
The normal Python-code path also stores increfed objects:

```c
Py_INCREF(cobj->co_filename);
pit->modname = cobj->co_filename;
...
Py_INCREF(cobj->co_name);
pit->name = cobj->co_name;
```

However `_del_pit()` only releases `pit->fn_descriptor` and does not release
`pit->name` or `pit->modname`.

```c
static void
_del_pit(_pit *pit)
{
    ...
    Py_DECREF(pit->fn_descriptor);
}
```

If `_del_pit()` is intended to own and clear `_pit` Python object fields, it
should also release `pit->name` and `pit->modname`:

```c
Py_XDECREF(pit->name);
Py_XDECREF(pit->modname);
Py_XDECREF(pit->fn_descriptor);
```

This is a broader candidate than the scanner-reported lines, and should be
confirmed against yappi's intended `_pit` lifetime model before filing.

## 4. Module init failure cleanup

`PyInit__yappi()` creates a module and an exception object, then returns `NULL`
directly if `_init_profiler()` fails:

```c
m = PyModule_Create(&_yappi_module);
...
YappiProfileError = PyErr_NewException("_yappi.error", NULL, NULL);
PyDict_SetItemString(d, "error", YappiProfileError);
...
if (!_init_profiler()) {
    PyErr_SetString(YappiProfileError, "profiler cannot be initialized.");
    return NULL;
}
```

This is a low-probability initialization failure path, but it appears to miss
cleanup for `m` and `YappiProfileError`. It also does not check whether
`PyErr_NewException()` or `PyDict_SetItemString()` failed.

This issue is lower priority than the per-context and per-profile-item leaks,
but it is worth fixing while touching the module init code.

## Notes On Likely False Positives

Some scanner findings in this run look like false positives:

- `_current_context_id`, `PyDict_GetItemString(ts->dict, "_yappi_tid")`: this
  returns a borrowed reference, but the code uses it immediately and does not
  mutate the dict before `PyLong_AsVoidPtr()`.
- `_get_frame_elapsed`, `PyDict_GetItem(test_timings, formatted_string)`: this
  borrowed value is read immediately with `PyLong_AsLongLong()` after the lookup
  key is released. Releasing the lookup key does not invalidate the dict value.

The strongest reportable issues are the missing `Py_DECREF(ytid)`, the extra
`Py_INCREF(method_descriptor)`, and the likely missing `_pit.name` /
`_pit.modname` cleanup.

> Note for disclosure: this report is from static analysis plus source review;
> it has not been confirmed with a runtime reproducer such as repeated
> multicontext profiling or builtin-call profiling under tracemalloc/RSS
> measurement.

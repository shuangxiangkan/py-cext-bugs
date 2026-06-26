# numexpr: possible reference leaks in initialization paths

## Summary

`numexpr` has one strong CPython reference leak candidate in
`NumExpr_init()` and one lower-priority module initialization ownership issue.

The strongest candidate is an allocation-failure path in `NumExpr_init()`: after
`constsig` is created with `PyBytes_FromStringAndSize()`, failure to allocate the
parallel `itemsizes` array returns without releasing `constsig`.

There is also a lower-priority module initialization issue where
`PyModule_AddObject()` is called with borrowed `Py_True` / `Py_False` singletons
without first creating an owned reference.

- Project: `python-c-repos/numexpr`
- Component: C++ CPython extension (`numexpr/*.cpp`)
- Category: CPython owned-reference leak / module initialization ownership
- Confidence: high candidate for the `NumExpr_init()` allocation-failure leak,
  medium-low for the singleton module-add issue on modern Python versions

Scan results:

| Tool | Files | Functions | Findings | Relevant findings |
|---|---:|---:|---:|---|
| `cext-review-toolkit` | 7 | 48 | 0 | none |
| `py-cext-bugs` | 7 | 48 | 5 | `NumExpr_init`, plus false positives in `NumExpr_run` |

Result files:

- `scan-results/numexpr-cext-review-toolkit-refcounts.json`
- `scan-results/numexpr-py-cext-bugs-refcount.json`

## 1. `NumExpr_init`: leaked `constsig` if `itemsizes` allocation fails

Current code in `numexpr/numexpr_object.cpp`:

```cpp
if (!(constants = PyTuple_New(n_constants)))
    return -1;
if (!(constsig = PyBytes_FromStringAndSize(NULL, n_constants))) {
    Py_DECREF(constants);
    return -1;
}
if (!(itemsizes = PyMem_New(int, n_constants))) {
    Py_DECREF(constants);
    return -1;
}
```

`PyBytes_FromStringAndSize()` returns a new reference. If `constsig` is created
successfully but `PyMem_New(int, n_constants)` fails, the function releases
`constants` and returns `-1`, but it does not release `constsig`.

This looks like a real error-path leak. Nearby failure paths already release
both `constants` and `constsig`, for example when `PySequence_GetItem()` fails:

```cpp
if (!(o = PySequence_GetItem(o_constants, i))) {
    Py_DECREF(constants);
    Py_DECREF(constsig);
    PyMem_Del(itemsizes);
    return -1;
}
```

Suggested fix:

```cpp
if (!(itemsizes = PyMem_New(int, n_constants))) {
    Py_DECREF(constants);
    Py_DECREF(constsig);
    return -1;
}
```

## 2. Module init: borrowed `Py_True` / `Py_False` passed to `PyModule_AddObject`

Current code in `numexpr/module.cpp`:

```cpp
#ifdef USE_VML
    if(PyModule_AddObject(m, "use_vml", Py_True) < 0) INITERROR;
#else
    if(PyModule_AddObject(m, "use_vml", Py_False) < 0) INITERROR;
#endif
```

`PyModule_AddObject()` is a stealing API on success. `Py_True` and `Py_False`
are borrowed singleton references here, so the call should not pass them
directly as owned references.

Suggested fix:

```cpp
#ifdef USE_VML
    Py_INCREF(Py_True);
    if (PyModule_AddObject(m, "use_vml", Py_True) < 0) {
        Py_DECREF(Py_True);
        INITERROR;
    }
#else
    Py_INCREF(Py_False);
    if (PyModule_AddObject(m, "use_vml", Py_False) < 0) {
        Py_DECREF(Py_False);
        INITERROR;
    }
#endif
```

On newer Python versions, immortal booleans may make the practical impact much
smaller, but the ownership contract is still incorrect.

If targeting newer CPython APIs, using `PyModule_AddObjectRef()` or
`PyModule_Add()` with the appropriate ownership semantics would also avoid this
pattern.

## Lower-priority import failure cleanup

The module initialization function has several `INITERROR` returns after module
or dict objects have already been created:

```cpp
m = PyModule_Create(&moduledef);
...
d = PyDict_New();
if (!d) INITERROR;
...
if (add_symbol(d, sname, name, "add_op") < 0) { INITERROR; }
...
if (PyModule_AddObject(m, "opcodes", d) < 0) INITERROR;
```

These paths can leak partially initialized module-level objects on import
failure. This is lower priority than the `NumExpr_init()` runtime object leak
because it only happens while importing the extension and usually during rare
allocation or module construction failures.

## Notes On Likely False Positives

`py-cext-bugs` also reported two possible leaks for variable `o` in
`NumExpr_run()`. These look like false positives.

The relevant code uses `PyTuple_GET_ITEM(args, i)`, which returns a borrowed
reference:

```cpp
PyObject *o = PyTuple_GET_ITEM(args, i); // borrowed ref
```

When the object is stored for later cleanup, the code either creates a new
reference with `PyArray_FROM_OTF()` or explicitly increments the input:

```cpp
if (!PyArray_Check(o)) {
    a = PyArray_FROM_OTF(o, typecode, NPY_ARRAY_NOTSWAPPED);
}
else {
    Py_INCREF(o);
    a = o;
}
operands[i+1] = (PyArrayObject *)a;
```

Both success and failure exits then release the owned references through
`operands[]`:

```cpp
cleanup_and_exit:
    for (i = 0; i < n_inputs+1; i++) {
        Py_XDECREF(operands[i]);
        Py_XDECREF(dtypes[i]);
    }
    return ret;

fail:
    for (i = 0; i < n_inputs+1; i++) {
        Py_XDECREF(operands[i]);
        Py_XDECREF(dtypes[i]);
    }
    return NULL;
```

The same scanner run also reported `constants` and `constsig` reaching the
normal `return check_program(self)` path in `NumExpr_init()`. Those look like
false positives too: the code transfers ownership into `self` with
`REPLACE_OBJ(constsig)` and `REPLACE_OBJ(constants)`, and `NumExpr_dealloc()`
later decrefs `self->constsig` and `self->constants`.

## Overall assessment

The `NumExpr_init()` `constsig` path is the best candidate to report or patch.
It is a conventional owned-reference cleanup bug on an allocation-failure path,
and the fix is minimal.

The module initialization issues are worth cleaning up, but they should be
presented as lower-priority import-time ownership problems rather than the main
bug.

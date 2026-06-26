# pysat: possible import-time and propagator error-path reference leaks

## Summary

`pysat` has several possible CPython reference-counting issues in its C/C++
extension code. Most scanner findings in this project are likely false positives
around `PyTuple_SET_ITEM()` / `PyList_SET_ITEM()` steal-reference semantics, but
manual review found a smaller set of plausible issues:

- `pyiter_to_pyitervector()` can leak already-collected `PyObject *` items when
  it fails part-way through converting an iterable.
- `pyformula`, `pycard`, and `pysolvers` module initialization paths have
  incomplete cleanup around global exception/helper objects.
- Some tuple/list construction paths use unchecked `PyLong_FromLongLong()`
  results with `PyTuple_SET_ITEM()`, which is more of an allocation-failure
  robustness issue than a direct leak.

- Project: `python-c-repos/pysat`
- Component: CPython C/C++ extensions (`formula/*.cc`, `cardenc/pycard.cc`,
  `solvers/pysolvers.cc`)
- Category: CPython owned-reference leak / import-time cleanup / error-path
  cleanup
- Confidence: medium-high for `pyiter_to_pyitervector()`, medium for module
  initialization cleanup, low-medium for unchecked tuple items

Scan results:

| Tool | Files | Functions | Findings | Relevant findings |
|---|---:|---:|---:|---|
| `cext-review-toolkit` | 6 | 32 | 35 | many `SET_ITEM` reports, `PyInit_pyformula`, `vector_to_pylist` |
| `py-cext-bugs` | 6 | 32 | 26 | mostly `stolen_ref_not_nulled`, plus `PyInit_pyformula` paths |

Result files:

- `scan-results/pysat-cext-review-toolkit-refcounts.json`
- `scan-results/pysat-py-cext-bugs-refcount.json`

## Affected Sites

| Function | File:line | Leaked value | Confidence |
|---|---|---|---|
| `pyiter_to_pyitervector` | `solvers/pysolvers.cc:1135` | previously collected `PyIter_Next()` items in `vect` on mid-loop failure | Medium-high |
| `PyInit_pyformula` | `formula/pyformula.cc:119` | extra `formula_err` reference on `PyModule_AddObject()` failure | Medium |
| `PyInit_pyformula` | `formula/pyformula.cc:126` | `formula_err` / `dec_cls` / `dec_inf` on later import-init failures | Medium |
| `PyInit_pycard` | `cardenc/pycard.cc:209` | `CardError` / module object on init failure | Medium |
| `PyInit_pysolvers` | `solvers/pysolvers.cc:1032` | `SATError` / module object on init failure | Medium |
| parse helpers | `formula/pf_parse.cc:80` | unchecked `PyLong_FromLongLong()` passed to `PyTuple_SET_ITEM()` | Low-medium |

## 1. `pyiter_to_pyitervector`: leaked collected items on mid-loop failure

Current code in `solvers/pysolvers.cc`:

```cpp
static bool pyiter_to_pyitervector(PyObject *obj, vector<PyObject*>& vect)
{
    PyObject *i_obj = PyObject_GetIter(obj);

    if (i_obj == NULL) {
        PyErr_SetString(PyExc_RuntimeError,
                "Object does not seem to be an iterable.");
        return false;
    }

    PyObject *l_obj;
    while ((l_obj = PyIter_Next(i_obj)) != NULL) {
        if (!PyList_Check(l_obj)) {
            Py_DECREF(l_obj);
            Py_DECREF(i_obj);
            // TODO also need to decref everything in vect
            PyErr_SetString(PyExc_TypeError, "list expected");
            return false;
        }
        vect.push_back(l_obj); // PyIter_Next() already returns a new reference
    }

    Py_DECREF(i_obj);
    return true;
}
```

`PyIter_Next()` returns a new reference for each item. The function transfers
those new references into `vect` without wrapping them in RAII. If a later item
is not a list, the function decrefs only the current `l_obj` and iterator, then
returns `false`.

The code comment explicitly notes the missing cleanup:

```cpp
// TODO also need to decref everything in vect
```

Unless every caller reliably decrefs all partially collected entries after a
`false` return, this leaks the objects already pushed into `vect`.

Suggested fix direction:

```cpp
if (!PyList_Check(l_obj)) {
    Py_DECREF(l_obj);
    Py_DECREF(i_obj);
    for (auto *item : vect) {
        Py_DECREF(item);
    }
    vect.clear();
    PyErr_SetString(PyExc_TypeError, "list expected");
    return false;
}
```

Alternatively, store collected objects in a small RAII wrapper while building
the vector, and only release ownership to callers on success.

## 2. `PyInit_pyformula`: incomplete cleanup after global exception/helper creation

Current code in `formula/pyformula.cc`:

```cpp
formula_err = PyErr_NewException((char *)"pyformula.error", NULL, NULL);
if (formula_err == NULL) {
    Py_DECREF(m);
    return NULL;
}

Py_INCREF(formula_err);
if (PyModule_AddObject(m, "error", formula_err) < 0) {
    Py_DECREF(formula_err);
    Py_DECREF(m);
    return NULL;
}

PyObject *decmod = PyImport_ImportModule("decimal");
if (decmod == NULL) {
    Py_DECREF(m);
    return NULL;
}

dec_cls = PyObject_GetAttrString(decmod, "Decimal");
...
dec_inf = PyObject_CallFunctionObjArgs(dec_cls, infstr, NULL);
```

The success path appears intentional: `PyModule_AddObject()` steals one
reference, while the extra `Py_INCREF(formula_err)` keeps a module-global
reference for later error reporting.

The failure paths are less complete:

- If `PyModule_AddObject()` fails, only one `Py_DECREF(formula_err)` is done
  after an explicit `Py_INCREF()`, leaving the original new reference
  questionable.
- If importing `decimal`, getting `Decimal`, creating `"+inf"`, or creating
  `dec_inf` fails, the code only decrefs the module object. It does not clean up
  previously created globals such as `formula_err` or `dec_cls`.

Because these are module-init failures, impact is lower than a runtime leak, but
the ownership paths are incomplete.

Suggested fix direction:

Use a shared cleanup label that clears every global already initialized:

```cpp
error:
    Py_XDECREF(dec_inf);
    dec_inf = NULL;
    Py_XDECREF(dec_cls);
    dec_cls = NULL;
    Py_XDECREF(formula_err);
    formula_err = NULL;
    Py_XDECREF(m);
    return NULL;
```

Care is needed around `PyModule_AddObject()` because it steals a reference only
on success.

## 3. `PyInit_pycard` and `PyInit_pysolvers`: exception object init cleanup

`cardenc/pycard.cc`:

```cpp
CardError = PyErr_NewException((char *)"pycard.error", NULL, NULL);
Py_INCREF(CardError);

if (PyModule_AddObject(m, "error", CardError) < 0) {
    Py_DECREF(CardError);
    return NULL;
}
```

`solvers/pysolvers.cc`:

```cpp
SATError = PyErr_NewException((char *)"pysolvers.error", NULL, NULL);
Py_INCREF(SATError);

if (PyModule_AddObject(m, "error", SATError) < 0) {
    Py_DECREF(SATError);
    return NULL;
}
```

Both follow the same pattern:

- `PyErr_NewException()` returns a new reference.
- `Py_INCREF()` creates an additional global reference.
- `PyModule_AddObject()` steals a reference on success.

The success path is likely intentional. The failure paths are more fragile:

- There is no check that `PyErr_NewException()` returned non-NULL before
  `Py_INCREF()`.
- On `PyModule_AddObject()` failure, only one reference is released.
- The module object `m` is not decref'd in these failure branches.

Suggested fix direction:

```cpp
CardError = PyErr_NewException(...);
if (CardError == NULL) {
    Py_DECREF(m);
    return NULL;
}
Py_INCREF(CardError);
if (PyModule_AddObject(m, "error", CardError) < 0) {
    Py_DECREF(CardError);  // extra global ref
    Py_DECREF(CardError);  // original ref not stolen
    Py_DECREF(m);
    CardError = NULL;
    return NULL;
}
```

Equivalent cleanup-label logic would be cleaner and less error-prone.

## 4. Unchecked `PyLong_FromLongLong()` with `PyTuple_SET_ITEM()`

Several parse helpers build result tuples with patterns like this:

```cpp
ret = PyTuple_New(3);
if (ret == NULL) {
    Py_DECREF(clauses);
    Py_DECREF(comments);
    return NULL;
}

PyTuple_SET_ITEM(ret, 0, PyLong_FromLongLong(nv));
PyTuple_SET_ITEM(ret, 1, clauses);
PyTuple_SET_ITEM(ret, 2, comments);
return ret;
```

`PyLong_FromLongLong()` returns a new reference, and `PyTuple_SET_ITEM()` steals
it. The steal semantics are correct on success, so this is not the leak reported
by the scanners.

However, if `PyLong_FromLongLong()` fails and returns `NULL`, the macro receives
a null item. This is an allocation-failure robustness issue and may lead to an
invalid tuple or crash, depending on build/runtime checks.

Suggested fix direction:

```cpp
PyObject *nv_obj = PyLong_FromLongLong(nv);
if (nv_obj == NULL) {
    Py_DECREF(ret);
    Py_DECREF(clauses);
    Py_DECREF(comments);
    return NULL;
}
PyTuple_SET_ITEM(ret, 0, nv_obj);
```

The same pattern appears in WCNF/CNF+ tuple builders.

## Likely False Positives / Lower Priority Findings

Most high-confidence scanner findings in this project are probably not real
bugs. Examples:

- `formula/pf_parse.cc` result tuple builders transfer list references into
  tuples with `PyTuple_SET_ITEM()` and immediately return the tuple.
- `cardenc/pycard.cc` uses `PyTuple_SET_ITEM()` and `PyList_SET_ITEM()` in
  fixed-size tuple/list builders; the transferred objects are not later decref'd
  on the same path.
- `clauseset_to_pylist()` and `vector_to_pylist()` use `PyList_SET_ITEM()` to
  steal newly-created list/int objects, which is the intended CPython API
  contract.

These are good examples where the scanners are conservative around stolen
references. They should not be reported as leaks unless a later cleanup path can
also decref the same local pointer.

## Overall Assessment

This is a weaker report than the `python-ldap` findings, because the strongest
scanner hits are mostly steal-reference false positives. The best real candidate
is `pyiter_to_pyitervector()`, which even has an inline TODO describing the
missing decref loop. The module initialization issues are plausible but limited
to import-time failure paths.

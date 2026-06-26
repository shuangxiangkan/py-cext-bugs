# BTrees: likely reference leaks in set-operation helpers

## Summary

`BTrees` has several likely CPython reference leaks in its template-generated C
extension code. The strongest candidates are ordinary runtime paths in set and
bucket operations:

- `bucket_sub()`, `bucket_or()`, and `bucket_and()` create a temporary argument
  tuple with `Py_BuildValue("OO", ...)` and pass it to the module-level set
  operation helpers without releasing it.
- `set_iand()` and `TreeSet_iand()` allocate `tmp_list` before checking whether
  `other` is iterable. If `PyObject_GetIter(other)` fails, they return
  `Py_NotImplemented` without releasing `tmp_list`.

There are also lower-severity latent `PyList_SetItem()` error-path ownership
issues in `bucket_items()` and `bucket_byValue()`.

- Project: `python-c-repos/BTrees`
- Component: hand-written/template C extension (`src/BTrees/*.c`)
- Category: CPython owned-reference leak / error-path cleanup
- Confidence: high candidate for the runtime leaks, medium-low severity for the
  latent `PyList_SetItem()` error paths

Both scanners reported relevant areas:

| Tool | Files | Functions | Findings | Relevant findings |
|---|---:|---:|---:|---|
| `cext-review-toolkit` | 11 | 170 | 18 | `bucket_sub`, `bucket_or`, `bucket_and`, `set_iand`, `TreeSet_iand`, `bucket_items` |
| `py-cext-bugs` | 11 | 170 | 35 | same areas, plus path-aware repeats in module init |

Result files:

- `scan-results/BTrees-cext-review-toolkit-refcounts.json`
- `scan-results/BTrees-py-cext-bugs-refcount.json`

## 1. `bucket_sub` / `bucket_or` / `bucket_and`: leaked temporary args tuple

Current code:

```c
static PyObject *
bucket_sub(PyObject *self, PyObject *other)
{
    PyObject *args = Py_BuildValue("OO", self, other);
    return difference_m(NULL, args);
}

static PyObject *
bucket_or(PyObject *self, PyObject *other)
{
    PyObject *args = Py_BuildValue("OO", self, other);
    return union_m(NULL, args);
}

static PyObject *
bucket_and(PyObject *self, PyObject *other)
{
    PyObject *args = Py_BuildValue("OO", self, other);
    return intersection_m(NULL, args);
}
```

`Py_BuildValue("OO", ...)` returns a new reference to a tuple. The called helper
functions parse the tuple but do not steal or release it:

```c
difference_m(PyObject *ignored, PyObject *args)
{
  PyObject *o1, *o2;

  UNLESS(PyArg_ParseTuple(args, "OO", &o1, &o2)) return NULL;
  ...
}
```

So each call to these bucket binary operators appears to leak the temporary
`args` tuple.

Suggested fix:

```c
static PyObject *
bucket_sub(PyObject *self, PyObject *other)
{
    PyObject *args = Py_BuildValue("OO", self, other);
    PyObject *result;

    if (args == NULL) {
        return NULL;
    }
    result = difference_m(NULL, args);
    Py_DECREF(args);
    return result;
}
```

Apply the same pattern to `bucket_or()` and `bucket_and()`.

## 2. `set_iand`: leaked `tmp_list` on `NotImplemented` fallback

Current code:

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

If `other` is not iterable, `PyObject_GetIter(other)` fails and the function
returns `Py_NotImplemented`, but the already-created `tmp_list` is never
released.

Suggested fix:

```c
iter = PyObject_GetIter(other);
if (iter == NULL) {
    Py_DECREF(tmp_list);
    PyErr_Clear();
    Py_INCREF(Py_NotImplemented);
    return Py_NotImplemented;
}
```

## 3. `TreeSet_iand`: same `tmp_list` leak

`TreeSet_iand()` has the same shape:

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

This leaks `tmp_list` on the `NotImplemented` fallback path. The same fix applies:

```c
if (iter == NULL) {
    Py_DECREF(tmp_list);
    PyErr_Clear();
    Py_INCREF(Py_NotImplemented);
    return Py_NotImplemented;
}
```

## 4. Latent `PyList_SetItem()` error-path double-free

The scanners also reported `PyList_SetItem()` ownership issues in
`bucket_items()` and `bucket_byValue()`.

Example:

```c
if (PyList_SetItem(r, i-low, item) < 0)
    goto err;

item = 0;

...

err:
    PER_UNUSE(self);
    Py_XDECREF(r);
    Py_XDECREF(item);
    return NULL;
```

`PyList_SetItem()` steals the item reference even when insertion fails. If the
failure branch were reachable, `Py_XDECREF(item)` in the shared error block
would release the same object again.

In these specific functions, the target list and index are constructed locally
and should be valid:

- `bucket_items()` creates `r = PyList_New(high-low+1)` and writes indexes
  `i-low`.
- `bucket_byValue()` pre-counts matching values, creates `r = PyList_New(l)`,
  and writes indexes from `0` to `l - 1`.

So the `PyList_SetItem()` failure branch is probably unreachable under current
invariants. This is still incorrect defensive error handling, but it is lower
priority than the ordinary runtime leaks above.

Suggested defensive fix:

```c
if (PyList_SetItem(r, i-low, item) < 0) {
    item = NULL;  /* PyList_SetItem already stole/released it */
    goto err;
}
```

or return directly from a local error block that does not decref `item`.

## Notes On Likely False Positives

Several scanner findings look like false positives after manual triage:

- `BTreeItemsTemplate.c:getBucketEntry`: `Py_DECREF(key)` and
  `Py_DECREF(value)` only run when `PyTuple_New(2)` failed, so
  `PyTuple_SET_ITEM()` was not executed.
- `BTree_newBucket`, `BTree_split_root`, and `BTree_grow`: the borrowed-ref
  reports come from conservative handling of `Py_TYPE(...)` and do not look like
  real borrowed-reference lifetime bugs.
- `BTreeModuleTemplate.c:module_init`: `BTreeType_setattro_allowed_names`,
  interned strings, and `ConflictError` are module-level/static state. There may
  be low-priority import-failure cleanup gaps, but they are not as strong as the
  runtime leaks above.
- `set_ior` / `TreeSet_ior`: the scanners reported `update_args`, but the code
  decrefs it after calling `Set_update()` / `TreeSet_update()`.

The strongest reportable issues are the leaked temporary `args` tuples in
`bucket_sub()` / `bucket_or()` / `bucket_and()`, plus the `tmp_list` leaks in
`set_iand()` and `TreeSet_iand()`.

> Note for disclosure: this report is based on static analysis plus source
> review. It has not been confirmed with a runtime reproducer such as repeated
> set operations under `sys.gettotalrefcount` or debug allocator checks.

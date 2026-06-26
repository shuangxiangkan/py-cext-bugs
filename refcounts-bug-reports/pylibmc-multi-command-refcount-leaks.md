# pylibmc: possible reference leaks in multi-command error paths

## Summary

`pylibmc` has several likely CPython reference leaks in its hand-written C
extension. The strongest candidates are in runtime multi-command paths:

- `_PylibMC_RunSetCommandMulti()` leaks already-owned temporary objects if
  allocation of the `failed` result list fails.
- `_PylibMC_IncrMulti()` leaks an empty `key_prefix` object by overwriting the
  only local pointer with `NULL`, and can also leak a non-empty prefix when
  `keys_tmp` allocation fails.
- `PylibMC_Client_delete_multi()` leaks `prefix` if fetching the `delete`
  method fails.
- `_PylibMC_AddServerCallback()` leaks the `val` dict if
  `memcached_stat_get_keys()` fails.

- Project: `python-c-repos/pylibmc`
- Component: hand-written C extension (`src/_pylibmcmodule.c`)
- Category: CPython owned-reference leak / error-path cleanup
- Confidence: high candidate for the runtime cleanup paths, lower severity for
  module-init-only leaks

Both scanners reported relevant areas:

| Tool | Files | Functions | Findings | Relevant findings |
|---|---:|---:|---:|---|
| `cext-review-toolkit` | 1 | 66 | 14 | `_PylibMC_IncrMulti`, `delete_multi`, `get_behaviors`, `_make_excs` |
| `py-cext-bugs` | 1 | 66 | 17 | `_PylibMC_RunSetCommandMulti`, `_PylibMC_IncrMulti`, `delete_multi`, `_PylibMC_AddServerCallback` |

Result files:

- `scan-results/pylibmc-cext-review-toolkit-refcounts.json`
- `scan-results/pylibmc-py-cext-bugs-refcount.json`

## 1. `_PylibMC_RunSetCommandMulti`: cleanup skipped after `PyList_New(0)` failure

Current code:

```c
serialized = PyMem_New(pylibmc_mset, nkeys);
if (serialized == NULL) {
    goto cleanup;
}

if (key_prefix_raw != NULL) {
    key_prefix = PyBytes_FromStringAndSize(key_prefix_raw, key_prefix_len);
}

...

if ((failed = PyList_New(0)) == NULL)
    return PyErr_NoMemory();

...

cleanup:
    if (serialized != NULL) {
        for (i = 0; i < nkeys; i++) {
            _PylibMC_FreeMset(&serialized[i]);
        }
        PyMem_Free(serialized);
    }
    Py_XDECREF(key_prefix);
    Py_XDECREF(key_str_map);

    return failed;
```

If `PyList_New(0)` fails at `src/_pylibmcmodule.c:980`, the function returns
immediately. That bypasses the `cleanup` block that releases:

- `serialized`
- each initialized `serialized[i]` via `_PylibMC_FreeMset()`
- `key_prefix`
- `key_str_map`

This is a real allocation-failure leak. It is low probability, but the fix is
straightforward: set the error and jump to cleanup instead of returning directly.

Suggested fix:

```c
failed = PyList_New(0);
if (failed == NULL) {
    PyErr_NoMemory();
    goto cleanup;
}
```

## 2. `_PylibMC_IncrMulti`: leaked `key_prefix`

Current code:

```c
if (key_prefix_raw != NULL) {
    key_prefix = PyBytes_FromStringAndSize(key_prefix_raw, key_prefix_len);

    if (key_prefix != NULL && PyBytes_Size(key_prefix) == 0)
        key_prefix = NULL;
}

keys_tmp = PyList_New(nkeys);
if (keys_tmp == NULL)
    return NULL;
```

There are two related leaks here.

First, if `key_prefix_raw` is an empty prefix, `PyBytes_FromStringAndSize()`
returns a new reference and the code replaces the only local pointer with
`NULL`. The object is then no longer reachable and the cleanup block cannot
release it.

Second, if a non-empty `key_prefix` was created and `PyList_New(nkeys)` fails,
the function returns directly without `Py_DECREF(key_prefix)`.

Suggested fix:

```c
if (key_prefix_raw != NULL) {
    key_prefix = PyBytes_FromStringAndSize(key_prefix_raw, key_prefix_len);
    if (key_prefix == NULL) {
        return NULL;
    }
    if (PyBytes_Size(key_prefix) == 0) {
        Py_DECREF(key_prefix);
        key_prefix = NULL;
    }
}

keys_tmp = PyList_New(nkeys);
if (keys_tmp == NULL) {
    Py_XDECREF(key_prefix);
    return NULL;
}
```

Alternatively, move `keys_tmp` failure handling to the existing `cleanup` block.

## 3. `PylibMC_Client_delete_multi`: leaked `prefix` when method lookup fails

Current code:

```c
if (prefix_raw != NULL)
    prefix = PyBytes_FromStringAndSize(prefix_raw, prefix_len);

if ((delete = PyObject_GetAttrString((PyObject *)self, "delete")) == NULL)
    return NULL;
```

If `prefix` is successfully created and `PyObject_GetAttrString()` fails, the
function returns without releasing `prefix`.

Suggested fix:

```c
if (prefix_raw != NULL) {
    prefix = PyBytes_FromStringAndSize(prefix_raw, prefix_len);
    if (prefix == NULL) {
        return NULL;
    }
}

delete = PyObject_GetAttrString((PyObject *)self, "delete");
if (delete == NULL) {
    Py_XDECREF(prefix);
    return NULL;
}
```

## 4. `_PylibMC_AddServerCallback`: leaked `val` on stat key failure

Current code:

```c
if ((val = PyDict_New()) == NULL)
    return MEMCACHED_FAILURE;

stat_keys = memcached_stat_get_keys(mc, stat, &rc);
if (rc != MEMCACHED_SUCCESS)
    return rc;
```

`PyDict_New()` returns a new reference. If `memcached_stat_get_keys()` fails,
the callback returns without releasing `val`.

Suggested fix:

```c
stat_keys = memcached_stat_get_keys(mc, stat, &rc);
if (rc != MEMCACHED_SUCCESS) {
    Py_DECREF(val);
    return rc;
}
```

## Lower-priority module-init leaks

Manual review also found several import-time leaks. These are real ownership
issues, but lower severity because they happen while constructing module-level
constants and exception metadata.

### `_make_excs`

```c
PyList_Append(exc_objs,
              Py_BuildValue("sO", "Error", (PyObject *)PylibMCExc_Error));
PyList_Append(exc_objs,
              Py_BuildValue("sO", "CacheMiss", (PyObject *)PylibMCExc_CacheMiss));

...

PyObject_SetAttrString(err->exc, "retcode", PyLong_FromLong(err->rc));

...

PyList_Append(exc_objs,
              Py_BuildValue("sO", err->name, (PyObject *)err->exc));
```

`PyList_Append()` and `PyObject_SetAttrString()` do not steal references. The
temporary objects returned by `Py_BuildValue()` and `PyLong_FromLong()` should be
stored in locals and decref'ed after successful insertion.

### `_make_behavior_consts`

```c
PyList_Append(names, PyUnicode_FromString(b->name));
...
PyList_Append(names, PyUnicode_FromString(b->name));
```

`PyUnicode_FromString()` returns a new reference, and `PyList_Append()` does not
steal it. Each temporary string should be decref'ed after append, with error
checking.

## Notes On False Positives

Some high-confidence scanner findings in this run look like false positives
after manual triage:

- `_PylibMC_IncrMulti`, `PyList_SetItem(keys_tmp, i, key)`: the code does
  `Py_INCREF(key)` before `PyList_SetItem()`. The list steals that extra
  reference, while the later `Py_DECREF(key)` releases the iterator-owned
  reference.
- `PylibMC_Client_get_multi`, `PyDict_GetItem()`: the code immediately
  `Py_INCREF`s the borrowed value from `PyDict_GetItem()` before releasing the
  temporary lookup key.
- `_key_normalized_obj`, `retval`: the helper's documented contract is to return
  an owned normalized key reference through `*key`.
- `_PylibMC_serialize_native`, `store_val`: returned through `*dest` and later
  released by `_PylibMC_FreeMset()` or transferred with `Py_BuildValue("N", ...)`
  in `PylibMC_Client_serialize()`.

The strongest reportable runtime issues are the skipped cleanup in
`_PylibMC_RunSetCommandMulti()` and the `key_prefix` leaks in `_PylibMC_IncrMulti()`.

> Note for disclosure: this report is based on static analysis plus source
> review. It has not been confirmed with a runtime reproducer such as forced
> allocation failures or repeated import testing under a debug allocator.

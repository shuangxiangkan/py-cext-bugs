# pygit2: likely reference-counting bugs in refdb backend and filter paths

## Summary

`pygit2` has several likely CPython reference-counting bugs in its C extension
code. The strongest candidates are in custom refdb backend callbacks and blob
filter stream callbacks:

- `pygit2_refdb_backend_write()` passes owned and borrowed objects to
  `Py_BuildValue()` with the `N` format, then decrefs the same owned objects
  again.
- `pygit2_refdb_backend_rename()` has an apparently inverted
  `build_signature()` success check and can leak `who`; it also passes borrowed
  booleans with `N`.
- `pygit2_refdb_backend_lookup()` returns a raw `git_reference *` from a Python
  `Reference` object without releasing the Python object, which looks like a
  reference leak and possibly a lifetime/ownership bug.
- `blob_filter_stream_write()` and `blob_filter_stream_close()` leak return
  values from Python method calls.

There are also several lower-severity error-path leaks in list-building helpers
and module enum caching.

- Project: `python-c-repos/pygit2`
- Component: CPython C extension (`src/*.c`)
- Category: CPython owned-reference leak / double DECREF / stolen-reference
  misuse / error-path cleanup
- Confidence: high for the blob callback leaks and `Py_BuildValue("N")` misuse;
  medium-high for refdb callback lifetime issues; medium for list-building
  error paths

Scan results:

| Tool | Files | Functions | Findings | Relevant findings |
|---|---:|---:|---:|---|
| `cext-review-toolkit` | 28 | 427 | 29 | `blob_filter_stream_close`, `Repository_listall_branches_impl`, `Tree_diff_to_index`, refdb helpers |
| `py-cext-bugs` | 28 | 427 | 27 | `blob_filter_stream_write/close`, `DiffHunk_lines__get__`, `Patch_hunks__get__`, refdb helpers |

Result files:

- `scan-results/pygit2-cext-review-toolkit-refcounts.json`
- `scan-results/pygit2-py-cext-bugs-refcount.json`

## 1. `pygit2_refdb_backend_write`: `Py_BuildValue("N")` steals references, then code decrefs them again

Current code in `src/refdb_backend.c`:

```c
PyObject *args = NULL, *ref = NULL, *who = NULL, *old = NULL;

if ((ref = wrap_reference((git_reference *)_ref, NULL)) == NULL)
    goto euser;
if ((who = build_signature(NULL, _who, "utf-8")) == NULL)
    goto euser;
if ((old = git_oid_to_python(_old)) == NULL)
    goto euser;
if ((args = Py_BuildValue("(NNNsNs)", ref,
        force ? Py_True : Py_False,
        who, message, old, old_target)) == NULL)
    goto euser;

PyObject_CallObject(be->write, args);
err = git_error_for_exc();
out:
    Py_DECREF(ref);
    Py_DECREF(who);
    Py_DECREF(old);
    Py_DECREF(args);
    return err;
```

The `N` format in `Py_BuildValue()` steals a reference. On the success path,
`ref`, `who`, and `old` have already been transferred into `args`, but the code
then calls `Py_DECREF(ref)`, `Py_DECREF(who)`, and `Py_DECREF(old)` again. That
looks like a double-decref/use-after-free risk.

The call also passes `force ? Py_True : Py_False` with `N`. `Py_True` and
`Py_False` are borrowed singleton references here, so passing them to a
stealing format without `Py_INCREF()` is incorrect ownership semantics.

There is a second cleanup issue: the `euser` path jumps to `out`, where the code
uses `Py_DECREF()` rather than `Py_XDECREF()`. If one of the earlier allocations
fails, some of `ref`, `who`, `old`, or `args` can still be `NULL`, so the cleanup
path can crash instead of returning `GIT_EUSER`.

Suggested fix direction:

```c
PyObject *py_force = force ? Py_True : Py_False;
Py_INCREF(py_force);

args = Py_BuildValue("(OOOsOs)", ref, py_force, who, message, old, old_target);
Py_DECREF(ref);
Py_DECREF(py_force);
Py_DECREF(who);
Py_DECREF(old);

if (args == NULL)
    return GIT_EUSER;
```

Alternatively, keep `N` only for objects whose ownership should truly transfer
into `args`, and then set the corresponding local pointers to `NULL` before the
shared cleanup block. Use `Py_XDECREF()` in cleanup paths.

## 2. `pygit2_refdb_backend_rename`: inverted signature check and stolen borrowed boolean

Current code:

```c
PyObject *args, *who;

if ((who = build_signature(NULL, _who, "utf-8")) != NULL)
    return GIT_EUSER;
if ((args = Py_BuildValue("(ssNNs)", old_name, new_name,
        force ? Py_True : Py_False, who, message)) == NULL) {
    Py_DECREF(who);
    return GIT_EUSER;
}
```

The first condition appears inverted. If `build_signature()` succeeds and
returns a new reference, the function immediately returns `GIT_EUSER` without
decrefing `who`. If it fails and returns `NULL`, execution continues and passes
`who == NULL` into `Py_BuildValue()`.

This function also uses `N` for `force ? Py_True : Py_False`, which is a
borrowed singleton reference, not an owned reference. It should either use `O`
or increment the singleton before using `N`.

Later error paths can leak the returned `Reference` object:

```c
Reference *ref = (Reference *)PyObject_CallObject(be->rename, args);
...
if ((err = git_error_for_exc()) != 0)
    return err;

if (!PyObject_IsInstance((PyObject *)ref, (PyObject *)&ReferenceType)) {
    PyErr_SetString(PyExc_TypeError, "Expected object of type pygit2.Reference");
    return GIT_EUSER;
}

git_reference_dup(out, ref->reference);
Py_DECREF(ref);
```

If `be->rename` returns a non-`Reference` object, `ref` is not released before
returning `GIT_EUSER`.

Suggested fix direction:

```c
who = build_signature(NULL, _who, "utf-8");
if (who == NULL)
    return GIT_EUSER;

args = Py_BuildValue("(ssOOs)", old_name, new_name,
                     force ? Py_True : Py_False, who, message);
Py_DECREF(who);
if (args == NULL)
    return GIT_EUSER;
```

Then ensure the callback result is decref'd on every path after
`PyObject_CallObject()`.

## 3. `pygit2_refdb_backend_lookup`: leaked callback result and possible lifetime bug

Current code:

```c
result = (Reference *)PyObject_CallObject(be->lookup, args);
Py_DECREF(args);

if ((err = git_error_for_exc()) != 0)
    goto out;

if (!PyObject_IsInstance((PyObject *)result, (PyObject *)&ReferenceType)) {
    PyErr_SetString(PyExc_TypeError, "Expected object of type pygit2.Reference");
    err = GIT_EUSER;
    goto out;
}

*out = result->reference;
out:
    return err;
```

`PyObject_CallObject()` returns a new reference. The function never decrefs
`result`, so the Python `Reference` object leaks.

There is also a possible lifetime issue: the function returns the inner
`git_reference *` through `*out` without duplicating it. `pygit2_refdb_backend_rename()`
uses `git_reference_dup(out, ref->reference)` before releasing `ref`, which
suggests `lookup()` should likely duplicate the reference too:

```c
git_reference_dup(out, result->reference);
Py_DECREF(result);
```

If the raw pointer is intentionally borrowed from the Python object, then the
function needs an explicit ownership/lifetime comment and a matching reference
ownership strategy. As written, it looks suspicious.

## 4. Refdb boolean callbacks leak `result` on Python error paths

`pygit2_refdb_backend_has_log()` and `pygit2_refdb_backend_ensure_log()` both
create a callback result, then return on `git_error_for_exc()` without releasing
the result:

```c
result = PyObject_CallObject(be->has_log, args);
Py_DECREF(args);

if ((err = git_error_for_exc()) != 0) {
    return err;
}
```

The same shape exists for `ensure_log`.

If the callback result is non-`NULL` while an exception/error is detected, the
new reference leaks. If the callback returns `NULL`, there is no object to
release, but the code is not robust about the two cases.

Suggested fix:

```c
result = PyObject_CallObject(be->has_log, args);
Py_DECREF(args);

if ((err = git_error_for_exc()) != 0) {
    Py_XDECREF(result);
    return err;
}
```

`pygit2_refdb_backend_exists()` has the opposite cleanup hazard: it jumps to a
shared `out` block and calls `Py_DECREF(result)`, so if the callback returned
`NULL`, the function may dereference a null pointer. That cleanup should also
use `Py_XDECREF(result)`.

## 5. `blob_filter_stream_write` and `blob_filter_stream_close`: leaked method-call return values

Current code in `src/blob.c`:

```c
result = PyObject_CallMethod(stream->py_ready, "set", NULL);
if (result == NULL)
{
    PyErr_Clear();
    git_error_set(GIT_ERROR_OS, "failed to signal queue ready");
    err = GIT_ERROR;
    goto done;
}
pos += chunk_size;
```

`PyObject_CallMethod()` returns a new reference. The first call in the loop
properly decrefs `result`, but this second `py_ready.set()` result is not
released.

`blob_filter_stream_close()` has two more calls with the same issue:

```c
result = PyObject_CallMethod(stream->py_closed, "set", NULL);
...
result = PyObject_CallMethod(stream->py_ready, "set", NULL);
```

Neither successful call decrefs `result`. The fix is straightforward:

```c
result = PyObject_CallMethod(stream->py_ready, "set", NULL);
if (result == NULL) {
    ...
}
Py_DECREF(result);
```

Apply the same pattern to both calls in `blob_filter_stream_close()`. If both
calls are made even after the first one fails, initialize `result = NULL` and
decref only the successful call result before overwriting it.

## 6. List-building getters leak partially built lists on errors

`DiffHunk_lines__get__()`:

```c
py_lines = PyList_New(self->n_lines);
for (i = 0; i < self->n_lines; ++i) {
    err = git_patch_get_line_in_hunk(&line, self->patch->patch, self->idx, i);
    if (err < 0)
        return Error_set(err);

    py_line = wrap_diff_line(line, self);
    if (py_line == NULL)
        return NULL;

    PyList_SetItem(py_lines, i, py_line);
}
return py_lines;
```

If `git_patch_get_line_in_hunk()` or `wrap_diff_line()` fails after `py_lines`
has been allocated, the function returns without releasing the list.

`Patch_hunks__get__()` has the same shape:

```c
py_hunks = PyList_New(hunk_amounts);
for (i = 0; i < hunk_amounts; i++) {
    py_hunk = wrap_diff_hunk(self, i);
    if (py_hunk == NULL)
        return NULL;

    PyList_SET_ITEM((PyObject*) py_hunks, i, py_hunk);
}
return py_hunks;
```

Suggested fix direction: add a shared error block that decrefs the partially
filled list and returns the existing error.

These functions should also check the result of `PyList_New()` before entering
the loop.

## 7. `_cache_enums`: leaked imported module on success

Current code in `src/pygit2.c`:

```c
PyObject *enums = PyImport_ImportModule("pygit2.enums");
if (enums == NULL) {
    return NULL;
}

...

Py_RETURN_NONE;

fail:
    Py_DECREF(enums);
    forget_enums();
    return NULL;
```

The failure path decrefs `enums`, but the success path returns without releasing
the imported module object. The enum class references are stored separately in
global variables, so the local module reference should be released before
`Py_RETURN_NONE`.

Suggested fix:

```c
Py_DECREF(enums);
Py_RETURN_NONE;
```

## 8. Smaller error-path leaks

### `Repository_listall_branches_impl`

```c
list = PyList_New(0);
if (list == NULL)
    return NULL;

if ((err = git_branch_iterator_new(&iter, self->repo, list_flags)) < 0)
    return Error_set(err);
```

If `git_branch_iterator_new()` fails, `list` leaks. Later error paths already
clean up the list through `Py_CLEAR(list)`.

### `Tree_diff_to_index`

```c
PyObject *py_idx_ptr = PyObject_GetAttrString(py_idx, "_pointer");
if (!py_idx_ptr)
    return NULL;

...

if (Object__load((Object*)self) == NULL) { return NULL; }
```

If `Object__load()` fails, `py_idx_ptr` is not released. The function already
has an `error:` block that decrefs `py_idx_ptr`; this path should jump there
instead of returning directly.

## Notes On Likely False Positives

Several scanner findings are likely false positives or lower priority after
manual review:

- `Commit_gpg_signature__get__()` returns `Py_BuildValue("NN", ...)`; `N`
  intentionally steals the two bytes objects, so this is not a leak on the
  success path.
- `wrap_diff_file()` stores `raw_path` in the `DiffFile` object; `DiffFile_dealloc()`
  clears it later.
- `pygit2_filter_payload_new()` stores `payload->py_filter`, and
  `pygit2_filter_payload_free()` decrefs it.
- `RefdbBackend_init()` stores callback methods such as `be->lookup` and
  `be->exists`; `RefdbBackend_dealloc()` clears these fields.

## Overall assessment

The highest-value report candidates are the `refdb_backend.c` ownership bugs
and the `blob.c` callback leaks. They are ordinary callback/runtime paths and
do not depend only on allocation failure.

The list-building, enum-caching, branch-listing, and tree-diff issues are also
credible cleanup bugs, but they are narrower error paths and should be presented
as secondary candidates.

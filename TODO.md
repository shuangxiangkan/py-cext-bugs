# TODO

Refcount analyzer precision improvements found while comparing
`py-cext-bugs` with `cext-review-toolkit` on Pillow.

## 1. Model `Py_BuildValue("N...")` ownership transfer

`Py_BuildValue` format unit `N` steals a reference. The current path-aware
ownership flow does not inspect format strings, so it can report a false leak
when an owned object is passed through an `N` slot.

Pillow examples:

- `src/_imagingft.c:981`
- `src/_imagingft.c:1251`

Both return:

```c
return Py_BuildValue("N(ii)", image, x_offset, y_offset);
```

`image` is intentionally transferred to the returned tuple. The analyzer should
mark the corresponding argument as `returned` or `stolen` instead of reporting
`potential_leak_on_path`.

Suggested implementation:

- Add a small parser for `Py_BuildValue` format strings.
- Track which object arguments correspond to `N` units.
- In `refcount/ownership_transfer.py`, mark those arguments as no longer
  locally owned.
- Add tests for `"N"`, `"NN"`, nested formats such as `"N(ii)"`, and mixed
  formats such as `"OO N"`/`"(NO)"`.

## 2. Model `_PyBytes_Resize(&obj, size)` failure ownership

The analyzer currently reports that `buf` may leak when `_PyBytes_Resize`
returns an error. This is likely a false positive because `_PyBytes_Resize`
takes a pointer-to-object and owns the failure cleanup semantics for the
original bytes object.

Pillow example:

- `src/encode.c:145`

```c
buf = PyBytes_FromStringAndSize(NULL, bufsize);
if (_PyBytes_Resize(&buf, (status > 0) ? status : 0) < 0) {
    return NULL;
}
```

Suggested implementation:

- Add API semantics for pointer-to-owned-object resizing APIs.
- On `_PyBytes_Resize(&buf, ...) < 0`, treat `buf` as released or invalidated
  on the error branch.
- On success, keep `buf` owned.
- Add tests for both direct error-return and success-then-`Py_DECREF` patterns.

## 3. Suppress safe module-dict borrowed-reference patterns

The borrowed-reference checker reports `PyModule_GetDict(m)` results used after
creating a temporary object during module setup. In Pillow these look like
low-value false positives: the module owns the dict, and a local
`PyUnicode_FromString`/`PyUnicode_FromFormat` call does not invalidate the
module dictionary.

Pillow examples:

- `src/_avif.c:915`
- `src/_imaging.c:4301`
- `src/_imagingcms.c:1467`
- `src/_imagingft.c:1647`
- `src/_webp.c:790`

Typical pattern:

```c
PyObject *d = PyModule_GetDict(m);
PyObject *v = PyUnicode_FromString(...);
if (!v) {
    return -1;
}
PyDict_SetItemString(d, "name", v);
Py_DECREF(v);
```

Suggested implementation:

- Treat borrowed references from `PyModule_GetDict(module_arg)` as stable within
  module initialization functions, or
- Narrow `borrowed_ref_across_call` so it only flags intervening calls that can
  mutate or destroy the owning container.
- Add a regression test for the module-dict setup pattern above.

## 4. Keep comparing against equal scan corpora

`py-cext-bugs` now scans C/C++ files through
`analysis.sources.discover_source_files`. This aligned the Pillow scan with
`cext-review-toolkit`:

```text
Pillow:   76 files, 1047 functions
greenlet: 4 files, 26 functions
```

Future precision comparisons should keep file/function counts aligned before
claiming that a change reduced false positives.

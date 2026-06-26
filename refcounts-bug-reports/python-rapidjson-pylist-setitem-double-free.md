# python-rapidjson: `PyList_SetItem` error-path double-free

## Summary

Three sites in the decoder's `EndObject` / `EndArray` handlers call
`PyList_SetItem()` and then `Py_DECREF()` the item again when `PyList_SetItem`
returns `-1`. `PyList_SetItem()` **steals the item reference even on failure**, so
the extra `Py_DECREF` on the error branch is a double-free.

The bug is real: the failure branch has the wrong stolen-reference handling.
Under normal parser invariants the branch should not be reached, because the
target is a list and `listLen - 1` points at a placeholder appended earlier.
However, the array branch in `Handle()` ignores `PyList_Append()` failures, so a
low-memory append failure can break that invariant and leave the later
`PyList_SetItem()` call with no placeholder to replace. That makes this more
than a purely cosmetic dead branch, although the practical trigger is still an
allocation-failure/error-path scenario rather than ordinary JSON input.

- **Project:** python-rapidjson
- **Version:** 1.23 (commit `93ed158`)
- **Component:** hand-written C++ extension (`rapidjson.cpp`)
- **Class:** double-free on an error path / incorrect stolen-ref handling
- **Severity:** Low to medium — real error-path bug, mainly reachable through allocation failure

## Affected sites

| Function | `PyList_SetItem` | Erroneous `Py_DECREF` | Item |
|----------|------------------|-----------------------|------|
| `EndObject` (key/value-pairs branch) | `rapidjson.cpp:1055` | `rapidjson.cpp:1061` | `pair` |
| `EndObject` (array branch) | `rapidjson.cpp:1080` | `rapidjson.cpp:1086` | `replacement` |
| `EndArray` (array branch) | `rapidjson.cpp:1175` | `rapidjson.cpp:1181` | `replacement` |

## The pattern

```cpp
/* rapidjson.cpp:1053 (EndObject, key/value-pairs branch) */
Py_ssize_t listLen = PyList_GET_SIZE(current.object);
rc = PyList_SetItem(current.object, listLen - 1, pair);

// NB: PyList_SetItem() steals a reference on the replacement, so it
// must not be DECREFed when the operation succeeds

if (rc == -1) {
    Py_DECREF(pair);          // <-- double-free: PyList_SetItem already stole `pair`
    return false;
}
```

`PyList_SetItem()` always consumes (steals) its `item` argument. On failure it
does `Py_XDECREF(item)` internally before returning `-1` (it fails only for a
non-list object or an out-of-range index). So after `rc == -1`, `pair` has
already been released; the `Py_DECREF(pair)` is a second release of the same
reference.

The in-code comment is the tell: it says the item "must not be DECREFed when the
operation **succeeds**", implying the author believed a `Py_DECREF` *is* required
on failure. That is the misunderstanding: `PyList_SetItem` steals on failure
too.

The same shape appears in the `EndObject` array branch (`replacement`, lines
1080/1086) and the `EndArray` array branch (`replacement`, lines 1175/1181).

## Reachability

In all three sites the call is:

```cpp
Py_ssize_t listLen = PyList_GET_SIZE(current.object);
PyList_SetItem(current.object, listLen - 1, item);
```

`PyList_SetItem` returns `-1` only when:

1. the first argument is not a list, or
2. the index is out of range (`i < 0 || i >= len`).

On the normal success path, neither should happen:

- `current.object` is always a Python `list` on these branches (the
  key/value-pairs container and the array container are both built as lists; the
  dict branches use `PyDict_SetItem` / `PyObject_SetItem`, which do not steal and
  are handled correctly).
- During parsing, a placeholder element is appended to `current.object` with
  `PyList_Append` (e.g. `rapidjson.cpp:896`, `:912`) before the matching
  `End*` handler runs, so `listLen >= 1` and the index `listLen - 1` is always in
  range.

The important wrinkle is `Handle()`'s array branch:

```cpp
/* rapidjson.cpp:911 */
} else {
    PyList_Append(current.object, value);
    Py_DECREF(value);
}
```

This ignores the return value from `PyList_Append()`. If that append fails, most
likely due to allocation failure, the parser still returns success from
`Handle()`. The placeholder was not inserted, but the nested object/array parse
can continue, leaving a later `EndObject()` / `EndArray()` path to compute
`listLen - 1` against a list that may not contain the expected slot.

There is also a related lifetime problem in that same path: after a failed
append, `Py_DECREF(value)` can release the newly-created nested container even
though `StartObject()` / `StartArray()` will continue using the raw pointer.
So the exact runtime symptom under OOM may be a use-after-free, a preserved
pending exception, or the `PyList_SetItem()` double-free branch, depending on
where execution gets after the unchecked append. The `PyList_SetItem()` handling
is still wrong and should be fixed independently.

## Suggested fix

Drop the `Py_DECREF` on the error branch at all three sites — `PyList_SetItem`
already released the item:

```cpp
    rc = PyList_SetItem(current.object, listLen - 1, pair);
    if (rc == -1) {
        return false;        /* PyList_SetItem already stole/released `pair` */
    }
```

(If a defensive check against an empty list is desired, guard the index before
calling `PyList_SetItem` rather than DECREFing afterward.)

Also fix the precursor in `Handle()` by checking the array-append result:

```cpp
} else {
    int rc = PyList_Append(current.object, value);
    Py_DECREF(value);
    if (rc == -1) {
        return false;
    }
}
```

## How it was found

Static pre-screening with two reference-counting analyzers (`py-cext-bugs` and
`cext-review-toolkit`), followed by manual triage:

- `py-cext-bugs` flagged these as `stolen_ref_double_free` (the items `pair` /
  `replacement` stolen by `PyList_SetItem` then DECREFed again on the error
  branch).
- `cext-review-toolkit` did not report these `PyList_SetItem` sites in this
  scan.
- Manual triage confirmed the `PyList_SetItem` steal-on-failure semantics and
  found that normal parser invariants make the branch unlikely, but an unchecked
  `PyList_Append()` failure in the parent array path can invalidate those
  invariants.

> Note for disclosure: this report is from static analysis plus source review. It
> describes a correctness defect on an allocation-failure/error path, not a
> runtime-observed crash from ordinary input. Worth a defensive fix.

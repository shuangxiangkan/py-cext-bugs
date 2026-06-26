# msgspec: reference leak in `mpack_decode_ext` (Ext payload bytes)

## Summary

`mpack_decode_ext()` creates an owned `bytes` object with
`PyBytes_FromStringAndSize()` and passes it to `Ext_New()`, then returns without
releasing its own reference. Because `Ext_New()` takes its *own* reference
(`Py_INCREF`), the caller's reference is leaked. This leaks one `bytes` object
(the extension payload) on every MessagePack **Ext** value decoded through the
two affected branches.

- **Project:** msgspec
- **Version:** 0.21.1+ (commit `34ead0a`, `git describe`: `0.21.1-50-g34ead0a`)
- **Component:** hand-written C extension (`src/msgspec/_core.c`)
- **Class:** leaked owned reference (caller keeps ownership after a borrowing API)
- **Severity:** Medium — size-proportional heap leak in a hot decode path

## Affected sites

| Function | File:line | Leaked value | Branch |
|----------|-----------|--------------|--------|
| `mpack_decode_ext` | `src/msgspec/_core.c:16424` | `data` (`PyBytes_FromStringAndSize`) | `type->types & MS_TYPE_EXT` (decoding into a typed `Ext` field) |
| `mpack_decode_ext` | `src/msgspec/_core.c:16441` | `data` (`PyBytes_FromStringAndSize`) | Any decode with `self->ext_hook == NULL` |

## The leak

```c
/* src/msgspec/_core.c, inside mpack_decode_ext() */
else if (type->types & MS_TYPE_EXT) {
    data = PyBytes_FromStringAndSize(data_buf, size);   // new owned ref (refcount 1)
    if (data == NULL) return NULL;
    return Ext_New(code, data);                         // <-- caller's ref never released
}
...
else if (self->ext_hook == NULL) {
    data = PyBytes_FromStringAndSize(data_buf, size);   // new owned ref
    if (data == NULL) return NULL;
    return Ext_New(code, data);                         // <-- same leak
}
```

`Ext_New()` **borrows** (does not steal) its `data` argument — it takes its own
reference with `Py_INCREF`:

```c
/* src/msgspec/_core.c:9182 */
Ext_New(long code, PyObject *data) {
    Ext *out = (Ext *)Ext_Type.tp_alloc(&Ext_Type, 0);
    if (out == NULL)
        return NULL;
    out->code = code;
    Py_INCREF(data);          // <-- takes its OWN reference
    out->data = data;
    return (PyObject *)out;
}
```

And `Ext_dealloc()` releases exactly that one reference:

```c
/* src/msgspec/_core.c:9264 */
static void
Ext_dealloc(Ext *self) {
    Py_XDECREF(self->data);
    Py_TYPE(self)->tp_free((PyObject *)self);
}
```

So the `Ext` object owns one reference to `data`, balanced by `Ext_dealloc`. The
reference that `mpack_decode_ext` created with `PyBytes_FromStringAndSize` is a
*second*, separate reference that is never released. Refcount trace for one
decoded Ext:

1. `data = PyBytes_FromStringAndSize(...)` → refcount **1** (owned by `mpack_decode_ext`).
2. `Ext_New(code, data)` → `Py_INCREF` → refcount **2** (Ext owns one).
3. `return` without `Py_DECREF(data)` → caller's reference is dropped on the floor.
4. Later, the `Ext` is freed → `Ext_dealloc` → `Py_XDECREF(self->data)` → refcount **1**.

The payload `bytes` object never reaches refcount 0 and is leaked.

## Why this is a real bug (not a false positive)

1. **`Ext_New` borrows, the caller owns.** `Ext_New` does `Py_INCREF(data)`, so a
   caller that passes an *owned* reference must `Py_DECREF` it afterward. The two
   `mpack_decode_ext` sites create `data` fresh and never release it.

2. **The other `Ext_New` caller proves the contract.** The Python-level `Ext`
   constructor (`src/msgspec/_core.c:9261`) passes a `data` that is a **borrowed**
   function argument (validated, then `return Ext_New(code, data);`). It correctly
   does *not* `Py_DECREF`, because it does not own `data`. The same call shape is
   correct for borrowed input and buggy for the freshly-allocated input in
   `mpack_decode_ext`.

3. **No transfer consumes the extra reference.** `data` is not stored, returned,
   or stolen by anything other than `Ext_New`'s own `Py_INCREF`.

## Impact

Decoding any MessagePack data that contains Ext values leaks one `bytes` object
(the extension payload, `size` bytes) per Ext, on the two paths above:

- decoding into a typed `msgspec.msgpack.Ext` field (`MS_TYPE_EXT`);
- decoding `Any` with custom ext codes and no `ext_hook` configured.

In services that decode many messages containing Ext values, this accumulates as
steady RSS growth proportional to the total Ext payload bytes decoded.

(Note: the `code == -1` datetime branch and the `ext_hook != NULL` branch are not
affected — they either delegate to `mpack_decode_datetime` or build `data` only
as a memoryview, not via `PyBytes_FromStringAndSize`.)

## Suggested fix

Release the caller's reference after `Ext_New` takes its own. At both sites:

```c
data = PyBytes_FromStringAndSize(data_buf, size);
if (data == NULL) return NULL;
PyObject *result = Ext_New(code, data);
Py_DECREF(data);          /* <-- add: Ext_New took its own reference */
return result;
```

(`Py_XDECREF` is not required since the `data == NULL` case already returned.)

## How it was found

Static pre-screening with two independent reference-counting analyzers
(`py-cext-bugs` and `cext-review-toolkit`), followed by manual triage:

- Both tools flagged both sites — `py-cext-bugs` as `potential_leak_on_path`
  (lines 16424 / 16441, the leaking `return`), `cext-review-toolkit` as
  `potential_leak` (lines 16422 / 16439, the acquisition).
- Triage confirmed the leak by reading `Ext_New` (it `Py_INCREF`s `data`),
  `Ext_dealloc` (it `Py_XDECREF`s `self->data`, so the Ext owns exactly one
  reference), and the sibling `Ext_New` caller (which passes a *borrowed*
  argument, establishing that `Ext_New` does not steal).

> Note for disclosure: this report is from static analysis plus source review; it
> has not been confirmed with a runtime reproducer (e.g. tracemalloc / RSS growth
> while decoding Ext-bearing MessagePack). Recommended before filing upstream.

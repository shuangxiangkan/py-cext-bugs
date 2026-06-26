# python-zstandard: reference leak in writer/reader `close()` / `__exit__()`

## Summary

Six writer/reader teardown paths in the C extension call a Python method via
`PyObject_CallMethod()`, NULL-check the result, and then return on the success
path **without releasing the returned reference**. Each call therefore leaks one
owned reference. At least one of these (`ZstdCompressionWriter.close`) leaks a
freshly allocated `int` object, so it is a genuine heap memory leak, not merely
a refcount imbalance on a singleton.

- **Project:** python-zstandard
- **Version:** 0.25.0 (commit `7a77a75`, "global: release 0.25.0")
- **Component:** hand-written C extension (`c-ext/`)
- **Class:** leaked reference on the success path (missing `Py_DECREF`)
- **Severity:** Medium — accumulates on every `with`-block exit / `close()` call

## Affected sites

| Function | File:line (acquire) | Leaked value comes from | Notes |
|----------|---------------------|-------------------------|-------|
| `ZstdCompressionWriter_close`   | `c-ext/compressionwriter.c:219`   | `self.flush(1)` → `PyLong_FromSsize_t(...)` | Leaks a **new `int`** every call (real memory leak) |
| `ZstdCompressionWriter_exit`    | `c-ext/compressionwriter.c:53`    | `self.close()` | `__exit__` leaks `close()`'s result |
| `ZstdDecompressionWriter_close` | `c-ext/decompressionwriter.c:134` | `self.flush()` | |
| `ZstdDecompressionWriter_exit`  | `c-ext/decompressionwriter.c:41`  | `self.close()` | Inline `PyObject_CallMethod()` result is NULL-checked but not released |
| `compressionreader_exit`        | `c-ext/compressionreader.c:57`    | `self.close()` | `__exit__` leaks `close()`'s result |
| `decompressionreader_exit`      | `c-ext/decompressionreader.c:57`  | `self.close()` | Inline `PyObject_CallMethod()` result is NULL-checked but not released |

These chain: `__exit__()` calls `close()`, and `close()` calls `flush()`. A
single `with` exit on a compression writer therefore leaks both the
`__exit__()` call's `close()` result and the `int` returned by `flush()`.

## The leak pattern

`ZstdCompressionWriter_close` (`c-ext/compressionwriter.c:211`):

```c
static PyObject *ZstdCompressionWriter_close(ZstdCompressionWriter *self) {
    PyObject *result;

    if (self->closed) {
        Py_RETURN_NONE;
    }

    self->closing = 1;
    result = PyObject_CallMethod((PyObject *)self, "flush", "I", 1);  // new ref (owned)
    self->closing = 0;
    self->closed = 1;

    if (NULL == result) {
        return NULL;                  // NULL path: nothing to release — OK
    }

    /* Call close on underlying stream as well. */
    if (self->closefd && PyObject_HasAttrString(self->writer, "close")) {
        return PyObject_CallMethod(self->writer, "close", NULL);   // <-- `result` leaked
    }

    Py_RETURN_NONE;                   // <-- `result` leaked
}
```

`PyObject_CallMethod()` always returns a **new reference** (or `NULL`). After the
NULL check, `result` holds an owned reference that is never released on either
success path (the early `return` at the inner `if`, or the final
`Py_RETURN_NONE`).

`ZstdCompressionWriter_exit` (`c-ext/compressionwriter.c:43`) and
`compressionreader_exit` (`c-ext/compressionreader.c:43`) have the same shape:

```c
PyObject *result = PyObject_CallMethod((PyObject *)self, "close", NULL);
if (NULL == result) {
    return NULL;
}
/* ... no Py_DECREF(result) ... */
Py_RETURN_FALSE;                      // <-- `result` leaked
```

`ZstdDecompressionWriter_exit` (`c-ext/decompressionwriter.c:37`) and
`decompressionreader_exit` (`c-ext/decompressionreader.c:44`) use the same
ownership pattern inline:

```c
if (NULL == PyObject_CallMethod((PyObject *)self, "close", NULL)) {
    return NULL;
}
/* ... no local variable, so the successful result cannot be decref'ed ... */
Py_RETURN_FALSE;                      // <-- close() result leaked
```

## Why this is a real bug (not a false positive)

1. **`flush()` returns a freshly allocated object, not a singleton.**
   `ZstdCompressionWriter_flush` ends with
   `return PyLong_FromSsize_t(totalWrite);` (`c-ext/compressionwriter.c:208`).
   So `ZstdCompressionWriter_close` leaks a new `int` on every call — an actual
   heap leak, not just a refcount bump on `None`.

2. **Sibling functions in the same files release `result` correctly.**
   Throughout `c-ext/compressionreader.c` the same `result` variable is released
   with `Py_XDECREF(result)` (e.g. lines 276, 320, 331, 398, 415, 427, 452,
   463). These teardown methods are simply missing that cleanup.

3. **There is no aliasing/transfer that consumes the reference.** `result` is
   not returned, stored, stolen, or passed to anything that takes ownership; it
   is only NULL-checked and then dropped.

## Impact

Code that uses the streaming compressor/decompressor as a context manager —
the documented, idiomatic usage — leaks a reference on every exit:

```python
with zstd.ZstdCompressor().stream_writer(fh) as w:
    w.write(data)
# <-- __exit__ -> close() -> flush(): leaks an int (+ close()'s result)
```

In long-running services that compress/decompress many payloads via `with`
blocks, this accumulates and shows up as steady RSS growth.

## Suggested fix

Release `result` before the success-path returns. For
`ZstdCompressionWriter_close`:

```c
    if (NULL == result) {
        return NULL;
    }
    Py_DECREF(result);                /* <-- add */

    if (self->closefd && PyObject_HasAttrString(self->writer, "close")) {
        return PyObject_CallMethod(self->writer, "close", NULL);
    }

    Py_RETURN_NONE;
```

For the `__exit__` methods, store the result of `PyObject_CallMethod()` in a
local variable, add `Py_DECREF(result);` after the NULL check, and before
`Py_RETURN_FALSE;`. Apply the equivalent change to
`ZstdDecompressionWriter_close`.

## How it was found

Static pre-screening with two independent reference-counting analyzers
(`py-cext-bugs` and `cext-review-toolkit`), followed by manual triage:

- Both tools flagged the main writer/reader close paths — `py-cext-bugs` as
  `potential_leak_on_path` (pinpointing leaking return lines such as 229/232),
  and `cext-review-toolkit` as `potential_leak` / `potential_leak_on_error`.
- Manual follow-up found two additional inline `__exit__()` sites with the same
  ownership bug: `ZstdDecompressionWriter_exit` and `decompressionreader_exit`.
- Triage confirmed the leak by checking that `flush()` returns a new `PyLong`
  and that sibling functions release the same variable, ruling out the common
  false-positive shapes (NULL-guarded return, escape into a field/output
  parameter, stealing call).

> Note for disclosure: this report is from static analysis plus source review;
> it has not been confirmed with a runtime reproducer (e.g. tracemalloc / RSS
> growth under repeated `with` blocks). Recommended before filing upstream.

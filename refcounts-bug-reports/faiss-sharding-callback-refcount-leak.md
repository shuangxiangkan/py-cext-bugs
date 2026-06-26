# faiss: reference leak in `PyCallbackShardingFunction::operator()`

## Summary

`PyCallbackShardingFunction::operator()` calls a Python sharding callback with
`PyObject_CallFunction()` (which returns a new owned reference), reads its integer
value with `PyLong_AsLongLong()`, and returns **without releasing the callback
result**. Each invocation leaks one Python `int` object. Every sibling callback
in the same file releases its result correctly, which makes this an isolated
missing `Py_DECREF`.

- **Project:** faiss
- **Version:** commit `124bfa1d4`
- **Component:** hand-written Python C-API callback bridge (`faiss/python/python_callbacks.cpp`)
- **Class:** leaked owned reference (missing `Py_DECREF` on the callback result)
- **Severity:** Medium — one leaked `int` per shard lookup; accumulates in
  sharded index build/search

## Affected site

| Function | File:line | Leaked value |
|----------|-----------|--------------|
| `PyCallbackShardingFunction::operator()` | `faiss/python/python_callbacks.cpp:150` | `shard_id` (`PyObject_CallFunction`) |

## The leak

```cpp
/* faiss/python/python_callbacks.cpp:148 */
int64_t PyCallbackShardingFunction::operator()(int64_t i, int64_t shard_count) {
    PyThreadLock gil;
    PyObject* shard_id = PyObject_CallFunction(callback, "LL", i, shard_count);  // new owned ref
    if (shard_id == nullptr) {
        FAISS_THROW_MSG("propagate py error");
    }
    return PyLong_AsLongLong(shard_id);   // <-- reads value, never Py_DECREF(shard_id)
}
```

`PyObject_CallFunction()` returns a **new reference**. `PyLong_AsLongLong()` only
reads the value of `shard_id`; it does not consume the reference. The function
then returns the C `int64_t`, dropping the only owned reference to `shard_id`
without releasing it. The `PyThreadLock gil` local is just an RAII GIL guard and
is unrelated to `shard_id`.

## Why this is a real bug (not a false positive)

The other three callbacks in the same file follow the same
"call → use result → `Py_DECREF`" shape and all release their result. The
closest analogue, `PyCallbackIDSelector::is_member`, is identical except it
correctly decrefs:

```cpp
/* faiss/python/python_callbacks.cpp:121 */
bool PyCallbackIDSelector::is_member(faiss::idx_t id) const {
    PyThreadLock gil;
    PyObject* result = PyObject_CallFunction(callback, "(n)", int(id));
    if (result == nullptr) {
        FAISS_THROW_MSG("propagate py error");
    }
    bool b = PyObject_IsTrue(result);
    Py_DECREF(result);          // <-- present here, missing in the sharding callback
    return b;
}
```

`PyCallbackIOWriter::operator()` (line 55) and `PyCallbackIOReader::operator()`
(lines 84/89/94/99) likewise `Py_DECREF` their `result`. Only the sharding
callback omits it. The callback object itself is managed correctly (constructor
`Py_INCREF`s `callback`, destructor `Py_DECREF`s it); the leak is solely the
per-call `shard_id` result.

## Impact

`PyCallbackShardingFunction::operator()` is invoked once per element routed
through a Python-supplied sharding function. A leaked `int` accumulates per call,
so building/searching a sharded index with a Python sharding callback leaks
proportionally to the number of shard lookups performed.

## Suggested fix

Release the result after reading its value:

```cpp
    if (shard_id == nullptr) {
        FAISS_THROW_MSG("propagate py error");
    }
    int64_t result = PyLong_AsLongLong(shard_id);
    Py_DECREF(shard_id);        /* <-- add */
    return result;
```

(Reading the value into a local before the `Py_DECREF` also avoids using
`shard_id` after it is released.)

## How it was found

Manual source review during a two-scanner pass (`py-cext-bugs` and
`cext-review-toolkit`).

**Both scanners missed this site.** Triage of *why* surfaced a C++ extraction
gap common to both tools: they do not extract out-of-line `Class::operator()`
method definitions correctly. The qualified declarator
`PyCallbackShardingFunction::operator()` is mis-parsed (the method name resolves
to the class name and the wrong body is associated), so the `operator()` body is
never analyzed for reference counting. The sibling `is_member` callback *was*
analyzed — because it is a normally named method, not an `operator()` overload —
and (correctly) produced no finding since it releases its result. The leak was
found by reading the file directly rather than from a scanner finding.

So this report doubles as a regression case: once `Class::operator()` extraction
is fixed in the scanner, re-running it on `python_callbacks.cpp` should flag
`shard_id` at line 150 as a `potential_leak` (new reference from
`PyObject_CallFunction` neither released nor returned).

> Note for disclosure: this report is from source review; it has not been
> confirmed with a runtime reproducer (e.g. tracemalloc / RSS growth while
> running a sharded index with a Python sharding callback). Recommended before
> filing upstream.

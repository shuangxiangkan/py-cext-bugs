# Reference leak in `PyCallbackShardingFunction::operator()`

`PyCallbackShardingFunction::operator()` leaks the object returned by the Python
sharding callback on every call.

File: `faiss/python/python_callbacks.cpp`

Function: `PyCallbackShardingFunction::operator()`

Relevant code:

```cpp
int64_t PyCallbackShardingFunction::operator()(int64_t i, int64_t shard_count) {
    PyThreadLock gil;
    PyObject* shard_id = PyObject_CallFunction(callback, "LL", i, shard_count);
    if (shard_id == nullptr) {
        FAISS_THROW_MSG("propagate py error");
    }
    return PyLong_AsLongLong(shard_id);
}
```

`PyObject_CallFunction()` returns a new reference. `PyLong_AsLongLong()` only
reads the value of `shard_id`; it does not steal or release the reference. The
function then returns the C `int64_t` and drops the only owned reference to
`shard_id` without releasing it, so one Python `int` is leaked per call.

The sibling callback in the same file, `PyCallbackIDSelector::is_member`, has the
identical shape but releases its result correctly:

```cpp
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

`PyCallbackIOWriter::operator()` and `PyCallbackIOReader::operator()` likewise
`Py_DECREF` their `result`. Only the sharding callback omits it.

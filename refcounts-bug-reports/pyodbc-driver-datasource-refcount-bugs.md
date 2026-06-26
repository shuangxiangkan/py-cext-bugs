# pyodbc: likely reference bugs in driver and datasource helpers

## Summary

`pyodbc` has several likely CPython reference-counting bugs in its C++ extension
code. The strongest candidates are ordinary runtime paths in the module-level
`drivers()` and `dataSources()` helpers:

- `mod_drivers()` leaks every appended driver-name string by detaching a local
  RAII wrapper after `PyList_Append()`.
- `mod_drivers()` can double-decref its result list on the ODBC error path
  because the list is owned by an `Object` RAII wrapper but is also manually
  decref'd.
- `mod_datasources()` leaks `key` and `val` strings after inserting them into
  the result dict.
- `GetDiagRecs()` leaks `msg_list` on a low-probability buffer reallocation
  failure path.

There are also lower-priority import/startup cleanup leaks in initialization
helpers such as `CnxnInfo_init()`.

- Project: `python-c-repos/pyodbc`
- Component: C++ CPython extension (`src/*.cpp`)
- Category: CPython owned-reference leak / double DECREF / error-path cleanup
- Confidence: high for `mod_drivers()` and `mod_datasources()`, medium for
  `GetDiagRecs()` and startup cleanup paths

Scan results:

| Tool | Files | Functions | Findings | Relevant findings |
|---|---:|---:|---:|---|
| `cext-review-toolkit` | 11 | 143 | 40 | `mod_datasources`, `GetDiagRecs`, startup/global init paths |
| `py-cext-bugs` | 11 | 143 | 68 | same areas, with path-aware repeats |

Result files:

- `scan-results/pyodbc-cext-review-toolkit-refcounts.json`
- `scan-results/pyodbc-py-cext-bugs-refcount.json`

## 1. `mod_drivers`: leaked driver names after `PyList_Append`

Current code in `src/pyodbcmodule.cpp`:

```cpp
Object result(PyList_New(0));
if (!result)
    return 0;

...

Object name(PyUnicode_FromString((const char*)szDriverDesc));
if (!name)
    return 0;

if (PyList_Append(result, name.Get()) != 0)
    return 0;
name.Detach();
```

`PyUnicode_FromString()` returns a new reference. `Object name(...)` owns that
reference and would normally release it when the local wrapper goes out of
scope.

`PyList_Append()` increments the reference count of the appended object. After
the append succeeds, the local owned reference should be released by the RAII
wrapper. Instead, `name.Detach()` clears the wrapper without decrefing the local
reference. The list keeps its own reference, so the detached local reference is
leaked once per driver name.

Suggested fix:

```cpp
if (PyList_Append(result, name.Get()) != 0)
    return 0;
```

Do not call `name.Detach()` here.

## 2. `mod_drivers`: possible double DECREF of `result` on ODBC error path

The same function also has a likely double-decref path:

```cpp
Object result(PyList_New(0));
...
if (ret != SQL_NO_DATA)
{
    Py_DECREF(result);
    return RaiseErrorFromHandle(0, "SQLDrivers", SQL_NULL_HANDLE, SQL_NULL_HANDLE);
}

return result.Detach();
```

`result` is an `Object` RAII wrapper. Its destructor calls `Py_XDECREF(p)`.
Calling `Py_DECREF(result)` manually does not clear the wrapper's internal
pointer. When the function returns, the wrapper destructor will decref the same
list again.

Suggested fix:

```cpp
if (ret != SQL_NO_DATA)
{
    return RaiseErrorFromHandle(0, "SQLDrivers", SQL_NULL_HANDLE, SQL_NULL_HANDLE);
}
```

Let the `Object result` destructor release the list on this error path.

## 3. `mod_datasources`: leaked `key` and `val` after dict insertion

Current code in `src/pyodbcmodule.cpp`:

```cpp
PyObject* key = PyUnicode_FromString((const char*)szDSN);
PyObject* val = PyUnicode_FromString((const char*)szDesc);

if(key && val)
    PyDict_SetItem(result, key, val);
```

On Windows the same shape exists with `PyUnicode_DecodeUTF16()`.

`PyDict_SetItem()` increments the references for both key and value. The local
new references returned by `PyUnicode_FromString()` / `PyUnicode_DecodeUTF16()`
are not released afterward, so each datasource entry leaks both objects.

The code also ignores allocation and insertion failures. If only one of `key`
or `val` is created, that object is not released. If `PyDict_SetItem()` fails,
both local references should still be released and the partially built result
should be cleaned up.

Suggested fix:

```cpp
PyObject* key = PyUnicode_FromString((const char*)szDSN);
PyObject* val = PyUnicode_FromString((const char*)szDesc);

if (!key || !val) {
    Py_XDECREF(key);
    Py_XDECREF(val);
    Py_DECREF(result);
    return 0;
}

if (PyDict_SetItem(result, key, val) < 0) {
    Py_DECREF(key);
    Py_DECREF(val);
    Py_DECREF(result);
    return 0;
}

Py_DECREF(key);
Py_DECREF(val);
```

The same cleanup pattern should be applied to the Windows `PyUnicode_DecodeUTF16`
branch.

## 4. `GetDiagRecs`: leaked `msg_list` on buffer reallocation failure

Current code in `src/cursor.cpp`:

```cpp
msg_list = PyList_New(0);
if (!msg_list)
    return 0;

...

if (!PyMem_Realloc((BYTE**) &cMessageText, (iMessageLen + 1) * sizeof(uint16_t))) {
    PyMem_Free(cMessageText);
    PyErr_NoMemory();
    return 0;
}
```

`msg_list` is a new reference. If resizing `cMessageText` fails, the function
returns immediately without releasing `msg_list`.

This is an allocation-failure path, so it is lower probability than the
`mod_drivers()` and `mod_datasources()` leaks, but it is still a conventional
owned-reference cleanup bug.

Suggested fix:

```cpp
if (!PyMem_Realloc((BYTE**) &cMessageText, (iMessageLen + 1) * sizeof(uint16_t))) {
    PyMem_Free(cMessageText);
    Py_DECREF(msg_list);
    PyErr_NoMemory();
    return 0;
}
```

## Lower-priority startup cleanup leak: `CnxnInfo_init`

Current code in `src/cnxninfo.cpp`:

```cpp
map_hash_to_info = PyDict_New();

update = PyUnicode_FromString("update");
if (!map_hash_to_info || !update)
    return false;

hashlib = PyImport_ImportModule("hashlib");
if (!hashlib)
    return false;
```

If `update` creation fails after `map_hash_to_info` succeeds, the dict is left
owned by the global pointer. If importing `hashlib` fails, both previous globals
remain live even though initialization reports failure.

This is lower priority because it is an import/startup failure path and these
objects are intended to be process-lifetime globals on success. Still, the
failure path should clean up partially initialized globals or keep the module in
a clearly consistent state.

## Notes On Likely False Positives

Many scanner findings in this project are false positives because `pyodbc`
uses a local RAII wrapper:

```cpp
class Object
{
public:
    Object(PyObject* _p = 0) { p = _p; }
    ~Object() { Py_XDECREF(p); }
    PyObject* Detach() { PyObject* pT = p; p = 0; return pT; }
    PyObject* Get() { return p; }
};
```

The static scanners currently do not model this wrapper. As a result, findings
such as `Object d(PyImport_ImportModule(...))`, `Object args(Py_BuildValue(...))`,
and `Object cleaned(PyObject_CallMethod(...))` often look like leaks to the
scanner but are actually released by the `Object` destructor.

Specific likely false positives include:

- `create_name_map()` reporting `colinfo` after `PyTuple_SET_ITEM()`: the code
  sets `colinfo = 0` immediately after the steal.
- `GetDiagRecs()` reporting `msg_class` and `msg_value` after
  `PyTuple_SetItem()`: the DECREF block is only used when the tuple insertion
  block is not executed.
- Most `decimal.cpp` findings: temporary objects are generally wrapped in
  `Object`, while `decimal`, `re_sub`, `re_escape`, and `re_compile` are
  intended module-global references.

## Overall assessment

The best report candidates are the normal runtime bugs in `mod_drivers()` and
`mod_datasources()`. They do not require unusual allocation failure to trigger:
calling `pyodbc.drivers()` or `pyodbc.dataSources()` on a system with returned
entries should exercise them.

`GetDiagRecs()` and `CnxnInfo_init()` are still useful cleanup candidates, but
they are less severe because they depend on low-probability allocation or
startup failure paths.

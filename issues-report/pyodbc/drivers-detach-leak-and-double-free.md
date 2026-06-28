# Reference leak and double-DECREF in `pyodbc.drivers()`

I found two reference-counting bugs in `mod_drivers()`: it leaks every driver-name
string, and it double-decrefs its result list on the ODBC error path.

File: `src/pyodbcmodule.cpp`

Function: `mod_drivers`

## 1. Leaked driver name after `PyList_Append`

```cpp
Object name(PyUnicode_FromString((const char*)szDriverDesc));
if (!name)
    return 0;

if (PyList_Append(result, name.Get()) != 0)
    return 0;
name.Detach();
```

`Object name(...)` owns the new reference from `PyUnicode_FromString()`.
`PyList_Append()` increments the reference count (the list keeps its own
reference), so after a successful append the local reference should be released
by the `Object` destructor. Instead, `name.Detach()` clears the wrapper's pointer
*without* decrefing, so the local reference is leaked once per driver name.

The `Object` class comment in `src/wrapper.h` states this directly: it "does *not*
increment the reference count on acquisition but it *does* decrement the count if
you don't use Detach." So `Detach()` here is exactly what drops the cleanup.

Fix: just remove the `name.Detach();` call.

## 2. Double-DECREF of `result` on the ODBC error path

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

`result` is an `Object` RAII wrapper (with an `operator PyObject*()`). The manual
`Py_DECREF(result)` decrements the list's refcount but does **not** clear the
wrapper's internal pointer. When the function returns, `~Object()` runs
`Py_XDECREF(p)` on the same (already-freed) list, double-decrefing it.

Fix: let the `Object result` destructor release the list — drop the manual
`Py_DECREF(result)`:

```cpp
if (ret != SQL_NO_DATA)
{
    return RaiseErrorFromHandle(0, "SQLDrivers", SQL_NULL_HANDLE, SQL_NULL_HANDLE);
}
```

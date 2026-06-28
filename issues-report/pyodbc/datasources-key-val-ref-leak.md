# Reference leak in `pyodbc.dataSources()`

I found a reference leak in `mod_datasources()`: every entry returned by
`pyodbc.dataSources()` leaks both its key and value strings.

File: `src/pyodbcmodule.cpp`

Function: `mod_datasources`

Relevant code:

```cpp
PyObject* key = PyUnicode_FromString((const char*)szDSN);
PyObject* val = PyUnicode_FromString((const char*)szDesc);

if(key && val)
    PyDict_SetItem(result, key, val);
```

(The Windows branch has the same shape with `PyUnicode_DecodeUTF16()`.)

`PyUnicode_FromString()` / `PyUnicode_DecodeUTF16()` return new references.
`PyDict_SetItem()` does not steal them — it increments its own references for
both key and value. The local `key` and `val` references are never released, so
each datasource entry leaks two string objects on every `dataSources()` call.

The code also ignores allocation/insertion failures: if only one of `key` / `val`
is created the other is not released, and a failed `PyDict_SetItem()` is not
handled.

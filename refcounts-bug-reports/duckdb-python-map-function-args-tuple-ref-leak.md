# duckdb-python: likely argument tuple reference leak in Python map UDF call

## Summary

`duckdb-python` has a likely CPython reference leak in the implementation of
Python map UDF calls. The code builds an argument tuple with `PyTuple_Pack()`
directly inside `PyObject_CallObject()`, so the newly-created tuple reference is
never released.

The scanners reported the `PyObject_CallObject()` return value as a possible
leak, but manual review suggests that returned object is correctly transferred
to pybind11 via `py::reinterpret_steal`. The real leak is the anonymous
`PyTuple_Pack()` result used as the call argument tuple.

- Project: `python-c-repos/duckdb-python`
- Component: C++/pybind11 extension (`src/duckdb_py/map.cpp`)
- Category: CPython owned-reference leak
- Confidence: high

Scan results:

| Tool | Files | Functions | Findings | Relevant findings |
|---|---:|---:|---:|---|
| `cext-review-toolkit` | 44 | 609 | 5 | `FunctionCall`, `CreateVectorizedFunction`, `CreateNativeFunction` |
| `py-cext-bugs` | 81 | 878 | 6 | same core areas, plus one path-sensitive `FunctionCall` finding |

Result files:

- `scan-results/duckdb-python-cext-review-toolkit-refcounts.json`
- `scan-results/duckdb-python-py-cext-bugs-refcount.json`

## Affected Site

| Function | File:line | Leaked value | Confidence |
|---|---|---|---|
| `FunctionCall` | `src/duckdb_py/map.cpp:41` | anonymous tuple returned by `PyTuple_Pack(1, in_df.ptr())` | High |

## Details

Current code in `src/duckdb_py/map.cpp`:

```cpp
static py::object FunctionCall(NumpyResultConversion &conversion, const vector<Identifier> &names, PyObject *function) {
    py::dict in_numpy_dict;
    for (idx_t col_idx = 0; col_idx < names.size(); col_idx++) {
        in_numpy_dict[names[col_idx].c_str()] = conversion.ToArray(col_idx);
    }

    auto &import_cache = *DuckDBPyConnection::ImportCache();
    auto pandas_df = import_cache.pandas.DataFrame();
    auto in_df = pandas_df(in_numpy_dict);
    D_ASSERT(in_df.ptr());

    D_ASSERT(function);
    auto *df_obj = PyObject_CallObject(function, PyTuple_Pack(1, in_df.ptr()));
    if (!df_obj) {
        PyErr_PrintEx(1);
        throw InvalidInputException("Python error. See above for a stack trace.");
    }

    auto df = py::reinterpret_steal<py::object>(df_obj);
    ...
    return df;
}
```

`PyTuple_Pack()` returns a new reference. `PyObject_CallObject()` receives the
tuple as its `args` parameter, but it does not steal that reference. Because the
tuple is created anonymously inside the call expression, the function has no
handle to release it afterwards.

This likely leaks one tuple each time `FunctionCall()` invokes the Python map
UDF. The function is used in both bind-time schema inference and execution:

```cpp
auto df = FunctionCall(conversion, data.in_names, data.function);
```

The `df_obj` return value itself is probably not leaked:

```cpp
auto df = py::reinterpret_steal<py::object>(df_obj);
```

`PyObject_CallObject()` returns a new reference on success, and
`py::reinterpret_steal<py::object>()` correctly transfers that owned reference
into a pybind11 RAII object.

## Suggested Fix

Use a pybind11-managed tuple:

```cpp
py::tuple args(1);
args[0] = in_df;
auto *df_obj = PyObject_CallObject(function, args.ptr());
```

or keep the raw tuple and release it after the call:

```cpp
PyObject *args = PyTuple_Pack(1, in_df.ptr());
if (args == nullptr) {
    throw py::error_already_set();
}
auto *df_obj = PyObject_CallObject(function, args);
Py_DECREF(args);
if (!df_obj) {
    PyErr_PrintEx(1);
    throw InvalidInputException("Python error. See above for a stack trace.");
}
```

The pybind11 tuple version is less error-prone and matches the style used in
the rest of the project.

## Scanner Interpretation

Both scanners reported findings around this function, but the reported symbol
was `df_obj`. That is slightly misleading:

- `df_obj` is correctly adopted by `py::reinterpret_steal<py::object>()`.
- the anonymous `PyTuple_Pack()` result is the object whose reference is lost.

The other reported findings in `src/duckdb_py/python_udf.cpp` appear to be
false positives for the same reason: `PyObject_CallObject()` returns are passed
to `py::reinterpret_steal<py::object>()`, and failed calls return `nullptr`
without creating a new reference to release.

## Overall Assessment

This is a strong, focused report candidate. The bug is small but likely real:
`PyTuple_Pack()` creates an owned reference, `PyObject_CallObject()` does not
steal it, and the code has no later `Py_DECREF()` for the tuple.

# py-tree-sitter: likely reference leaks in callbacks, tuple construction, and query helpers

## Summary

`py-tree-sitter` has several likely CPython reference-counting leaks in its
hand-written C extension. The strongest candidates are ordinary runtime paths:

- parser/query progress callbacks leak the Python callback result object.
- parser logger callbacks leak both the constructed enum object and the
  callback return value.
- `Point.__new__()` leaks the temporary `row` and `column` integers after
  `PyTuple_Pack()`.
- callable-backed `Node.text` extraction leaks each callback return object.
- query predicate capture helpers leak lists inserted into dictionaries.

There are also several error-path cleanup leaks around query result builders,
range-list builders, and parser callback argument construction.

- Project: `python-c-repos/py-tree-sitter`
- Component: CPython C extension (`tree_sitter/binding/*.c`)
- Category: CPython owned-reference leak / callback cleanup / error-path cleanup
- Confidence: high for the callback, `Point.__new__`, callable source, and
  query predicate leaks; medium for import/init and allocation-failure paths

Scan results:

| Tool | Files | Functions | Findings | Relevant findings |
|---|---:|---:|---:|---|
| `cext-review-toolkit` | 12 | 186 | 45 | callback returns, `point_new`, query/tree cleanup paths |
| `py-cext-bugs` | 12 | 186 | 55 | same core areas plus path-sensitive cleanup findings |

Result files:

- `scan-results/py-tree-sitter-cext-review-toolkit-refcounts.json`
- `scan-results/py-tree-sitter-py-cext-bugs-refcount.json`

## Affected Sites

| Function | File:line | Leaked value | Confidence |
|---|---|---|---|
| `parser_progress_callback` | `tree_sitter/binding/parser.c:97` | callback result from `PyObject_CallFunction()` | High |
| `query_cursor_progress_callback` | `tree_sitter/binding/query_cursor.c:97` | callback result from `PyObject_CallFunction()` | High |
| `log_callback` | `tree_sitter/binding/parser.c:309` | `log_type_enum` and logger callback result | High |
| `point_new` | `tree_sitter/binding/point.c:20` | `row_obj` / `col_obj` after `PyTuple_Pack()` | High |
| `node_get_text` | `tree_sitter/binding/node.c:618` | callable source return value `rv` on each successful loop iteration | High |
| `captures_for_match` | `tree_sitter/binding/query_predicates.c:32` | `nodes` list after `PyDict_SetItem()` | High |
| `query_cursor_matches` | `tree_sitter/binding/query_cursor.c:122` | partial `result` list on final error return | Medium-high |
| `query_cursor_captures` | `tree_sitter/binding/query_cursor.c:190` | partial `result` dict on error return | Medium-high |
| `tree_changed_ranges` | `tree_sitter/binding/tree.c:120` | `result` list and `ranges` buffer when `PyObject_New()` fails | Medium |
| `tree_get_included_ranges` | `tree_sitter/binding/tree.c:142` | `result` list and `ranges` buffer when `PyObject_New()` fails | Medium |
| `parser_read_wrapper` | `tree_sitter/binding/parser.c:61` | one successfully-created argument object if the other allocation fails | Medium |
| `PyInit__binding` | `tree_sitter/binding/module.c:133` | `int_enum` on later init failure | Low-medium |

## 1. Progress callbacks leak their return values

Current code in `tree_sitter/binding/parser.c`:

```c
static bool parser_progress_callback(TSParseState *state) {
    PyObject *result = PyObject_CallFunction((PyObject *)state->payload, "Ip",
                                             state->current_byte_offset, state->has_error);
    return PyObject_IsTrue(result);
}
```

`PyObject_CallFunction()` returns a new reference. The result is only inspected
with `PyObject_IsTrue()` and is never released. This leaks one Python object
per progress callback invocation.

`tree_sitter/binding/query_cursor.c` has the same pattern:

```c
static bool query_cursor_progress_callback(TSQueryCursorState *state) {
    PyObject *result =
        PyObject_CallFunction((PyObject *)state->payload, "I", state->current_byte_offset);
    return PyObject_IsTrue(result);
}
```

Suggested fix direction:

```c
PyObject *result = PyObject_CallFunction(...);
if (result == NULL) {
    return false;
}
int truth = PyObject_IsTrue(result);
Py_DECREF(result);
return truth > 0;
```

The exact error propagation policy depends on how tree-sitter expects callback
failures to be reported, but the owned reference should always be released.

## 2. `log_callback` leaks the enum object and callback return value

Current code in `tree_sitter/binding/parser.c`:

```c
PyObject *log_type_enum =
    PyObject_CallFunction((PyObject *)logger_payload->log_type_type, "i", log_type);
PyObject_CallFunction(logger_payload->callback, "Os", log_type_enum, buffer);
```

Both calls return new references:

- `log_type_enum` is a new enum object.
- the logger callback return value is also a new reference.

Neither is decref'd. If logging is enabled, every log callback can leak both
objects.

Suggested fix direction:

```c
PyObject *log_type_enum = PyObject_CallFunction(...);
if (log_type_enum == NULL) {
    return;
}
PyObject *result = PyObject_CallFunction(logger_payload->callback, "Os", log_type_enum, buffer);
Py_DECREF(log_type_enum);
Py_XDECREF(result);
```

## 3. `point_new` leaks tuple element temporaries

Current code in `tree_sitter/binding/point.c`:

```c
PyObject *row_obj = PyLong_FromUnsignedLong(row), *col_obj = PyLong_FromUnsignedLong(column);
PyObject *self = PyTuple_Pack(2, row_obj, col_obj);
if (!self) {
    return NULL;
}
Py_SET_TYPE(self, type);
return self;
```

`PyLong_FromUnsignedLong()` returns new references. `PyTuple_Pack()` does not
steal those references; it stores its own references in the tuple. The local
`row_obj` and `col_obj` references are not released on the success path.

The failure path also needs cleanup if only one `PyLong` or the tuple
allocation succeeds.

Suggested fix direction:

```c
PyObject *row_obj = PyLong_FromUnsignedLong(row);
PyObject *col_obj = PyLong_FromUnsignedLong(column);
if (row_obj == NULL || col_obj == NULL) {
    Py_XDECREF(row_obj);
    Py_XDECREF(col_obj);
    return NULL;
}
PyObject *self = PyTuple_Pack(2, row_obj, col_obj);
Py_DECREF(row_obj);
Py_DECREF(col_obj);
if (self == NULL) {
    return NULL;
}
Py_SET_TYPE(self, type);
return self;
```

## 4. Callable-backed `node_get_text` leaks each callback return object

Current code in `tree_sitter/binding/node.c`:

```c
PyObject *rv = PyObject_Call(tree->source, args, NULL);
Py_XDECREF(args);

PyObject *rv_bytearray = PyByteArray_FromObject(rv);
...
size_t bytes_read = (size_t)PyBytes_Size(rv);
const char *rv_str = PyBytes_AsString(rv);
...
current_offset += bytes_read;
```

On error paths, `rv` is released with `Py_XDECREF(rv)`. On the successful loop
path, `rv` is used to compute the read length and scan for newlines, but it is
not decref'd before the loop continues.

This means a callable source used by `Node.text` can leak one object per chunk
returned by the Python callback.

Suggested fix direction:

```c
size_t bytes_read = (size_t)PyBytes_Size(rv);
const char *rv_str = PyBytes_AsString(rv);
if (bytes_read == (size_t)-1 || rv_str == NULL) {
    Py_DECREF(rv);
    Py_DECREF(collected_bytes);
    return NULL;
}
...
Py_DECREF(rv);
current_offset += bytes_read;
```

The function should also avoid calling `PyByteArray_FromObject(rv)` when
`rv == NULL`, but the main refcount issue is the missing success-path decref.

## 5. `captures_for_match` leaks `nodes` after `PyDict_SetItem`

Current code in `tree_sitter/binding/query_predicates.c`:

```c
PyObject *nodes = nodes_for_capture_index(state, capture.index, match, tree);
if (PyDict_SetItem(captures, capture_name_obj, nodes) == -1) {
    return NULL;
}
Py_DECREF(capture_name_obj);
```

`nodes_for_capture_index()` returns a new list. `PyDict_SetItem()` increments
the key and value references; it does not steal the caller's references.
`capture_name_obj` is released after the successful insertion, but `nodes` is
not.

There are also missing cleanup steps on failure:

- if `capture_name_obj` creation fails, `captures` is leaked.
- if `PyDict_SetItem()` fails, `captures`, `capture_name_obj`, and `nodes` are
  leaked.

Suggested fix direction:

```c
PyObject *nodes = nodes_for_capture_index(...);
if (nodes == NULL) {
    Py_DECREF(captures);
    Py_DECREF(capture_name_obj);
    return NULL;
}
if (PyDict_SetItem(captures, capture_name_obj, nodes) == -1) {
    Py_DECREF(captures);
    Py_DECREF(capture_name_obj);
    Py_DECREF(nodes);
    return NULL;
}
Py_DECREF(capture_name_obj);
Py_DECREF(nodes);
```

## 6. Query result builders leak partial results on error

`query_cursor_matches()` builds a result list:

```c
PyObject *result = PyList_New(0);
...
return PyErr_Occurred() == NULL ? result : NULL;
```

If an error is set during the loop, the function returns `NULL` without
decref'ing the partially-built `result` list.

`query_cursor_captures()` has the same final pattern with a dict:

```c
PyObject *result = PyDict_New();
...
return PyErr_Occurred() == NULL ? result : NULL;
```

It also has an earlier direct return:

```c
if (PyErr_Occurred()) {
    return NULL;
}
```

Both paths should decref the partial `result` before returning `NULL`.

Suggested fix direction:

```c
if (PyErr_Occurred() != NULL) {
    Py_DECREF(result);
    return NULL;
}
return result;
```

These functions also ignore return values from calls such as `PyDict_SetDefault`,
`PyList_Append`, `PySet_Add`, `PySequence_List`, and `PyDict_SetItem`, so a
larger cleanup-label refactor would likely be safer than patching only the
final return statement.

## 7. Range-list builders leak on allocation failure

Current code in `tree_sitter/binding/tree.c`:

```c
PyObject *result = PyList_New(length);
...
Range *range = PyObject_New(Range, state->range_type);
if (range == NULL) {
    return NULL;
}
```

Both `tree_changed_ranges()` and `tree_get_included_ranges()` allocate a C
`ranges` buffer from tree-sitter and a Python `result` list. If `PyObject_New()`
fails inside the loop, the function returns immediately without releasing
either resource.

Suggested fix direction:

```c
if (range == NULL) {
    Py_DECREF(result);
    PyMem_Free(ranges);
    return NULL;
}
```

## 8. Smaller cleanup candidates

### `parser_read_wrapper`

`parser_read_wrapper()` creates two Python argument objects:

```c
PyObject *byte_offset_obj = PyLong_FromUnsignedLong(byte_offset);
PyObject *position_obj = point_new_internal(wrapper_payload->state, position);
if (!position_obj || !byte_offset_obj) {
    *bytes_read = 0;
    return NULL;
}
```

If one allocation succeeds and the other fails, the successful object is leaked.
Use `Py_XDECREF(byte_offset_obj)` and `Py_XDECREF(position_obj)` before this
return.

The later `rv` handling after `PyObject_GetBuffer()` appears intentional:
`source_view->obj` owns the buffer reference until the next call or parse
completion.

### `PyInit__binding`

`PyInit__binding()` imports `enum.IntEnum`:

```c
PyObject *int_enum = import_attribute("enum", "IntEnum");
...
if (state->log_type_type == NULL ||
    PyModule_AddObjectRef(module, "LogType", (PyObject *)state->log_type_type) < 0) {
    goto cleanup;
};
Py_DECREF(int_enum);
```

If `state->log_type_type` construction or `PyModule_AddObjectRef()` fails, the
function jumps to cleanup without releasing `int_enum`.

### Generic query predicate tuple construction

`query_new()` builds tuples like this:

```c
item = PyTuple_Pack(2, PyUnicode_FromStringAndSize(arg_value, length),
                    PyUnicode_FromString("capture"));
```

`PyTuple_Pack()` does not steal the temporary unicode references created in the
argument list. These temporaries should be stored, checked, passed to
`PyTuple_Pack()`, and decref'd afterwards.

## Likely False Positives / Lower Priority Findings

Several scanner findings in this project are probably not real leaks:

- Module-level type objects stored in `ModuleState` are released by
  `module_free()`.
- Object fields initialized with `Py_NewRef()` are often released by the
  corresponding deallocator.
- Some `stolen_ref_not_nulled` findings around `PyList_SetItem()` in
  `query_new()` appear conservative; after a successful steal, there is no
  obvious later cleanup path that decrefs the same local pointer again.
- Some `borrowed_ref_across_call` reports in predicate helpers are confused by
  nested calls. For example, `node_get_text()` returns a new reference, even if
  the `Node *` argument was obtained through `PyList_GetItem()`.

## Overall Assessment

This looks like a strong report candidate. The callback return leaks,
`Point.__new__()` leak, callable-source `node_get_text()` leak, and
`captures_for_match()` list leak are all based on straightforward CPython
ownership rules and are likely real bugs. The query/tree cleanup issues are
mostly error-path leaks, still worth reporting but slightly lower severity.

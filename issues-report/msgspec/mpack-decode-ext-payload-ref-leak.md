# Reference leak in `mpack_decode_ext()` for MessagePack Ext payloads

I found a reference leak in `mpack_decode_ext()` when a MessagePack **Ext** value is
decoded into an `Ext` object. The `bytes` payload created for the `Ext` is leaked,
one per decoded Ext value.

File: `src/msgspec/_core.c`

Function: `mpack_decode_ext`

Relevant code (two affected branches):

```c
else if (type->types & MS_TYPE_EXT) {
    data = PyBytes_FromStringAndSize(data_buf, size);
    if (data == NULL) return NULL;
    return Ext_New(code, data);          // <-- data never decref'd
}
...
else if (self->ext_hook == NULL) {
    data = PyBytes_FromStringAndSize(data_buf, size);
    if (data == NULL) return NULL;
    return Ext_New(code, data);          // <-- same leak
}
```

`PyBytes_FromStringAndSize()` returns a new reference, so `data` is owned by
`mpack_decode_ext()`. `Ext_New()` does **not** steal that reference — it takes its
own with `Py_INCREF`:

```c
static PyObject *
Ext_New(long code, PyObject *data) {
    Ext *out = (Ext *)Ext_Type.tp_alloc(&Ext_Type, 0);
    if (out == NULL)
        return NULL;
    out->code = code;
    Py_INCREF(data);        // <-- takes its own reference
    out->data = data;
    return (PyObject *)out;
}
```

and `Ext_dealloc()` releases exactly that one reference:

```c
static void
Ext_dealloc(Ext *self) {
    Py_XDECREF(self->data);
    Py_TYPE(self)->tp_free((PyObject *)self);
}
```

So the `Ext` object owns one reference (balanced by `Ext_dealloc`), and the
reference `mpack_decode_ext()` created with `PyBytes_FromStringAndSize()` is a
second, separate reference that is never released. The payload `bytes` never
reaches refcount 0.

The borrowing contract is confirmed by the other `Ext_New()` caller, the
`Ext(code, data)` constructor (`Ext_new`), which passes a **borrowed** tuple
argument and correctly does *not* decref it:

```c
data = PyTuple_GET_ITEM(args, 1);   // borrowed
...
return Ext_New(code, data);         // correct: caller doesn't own data
```

The same call shape is correct for that borrowed input but leaks in
`mpack_decode_ext()`, where `data` is freshly allocated and owned.

## Affected paths

Both branches leak one `bytes` object (the `size`-byte Ext payload) per decoded
Ext value:

- decoding into a typed `msgspec.msgpack.Ext` field (`MS_TYPE_EXT`);
- decoding `Any` with a non-datetime ext code and no `ext_hook` configured.

The `code == -1` datetime branch and the `ext_hook != NULL` branch are not
affected: the former delegates to `mpack_decode_datetime`, and the latter builds
the payload as a `memoryview` that is decref'd at the `done:` label.

Suggested fix: decref `data` after `Ext_New()` takes its own reference, at both
sites:

```c
data = PyBytes_FromStringAndSize(data_buf, size);
if (data == NULL) return NULL;
PyObject *result = Ext_New(code, data);
Py_DECREF(data);        /* Ext_New took its own reference */
return result;
```

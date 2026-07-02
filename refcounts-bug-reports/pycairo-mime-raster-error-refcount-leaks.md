# pycairo: reference leaks in text-to-glyph conversion, MIME data, and callbacks

## Summary

`pycairo` has several CPython reference-counting leaks in its C extension
code. The strongest findings are ordinary successful runtime paths:

- `scaled_font_text_to_glyphs()` leaks one argument tuple for every glyph and,
  when requested, every text cluster it converts.
- `_raster_source_release_func()` leaks the return value of a Python release
  callback when the callback correctly returns `None`.
- `surface_set_mime_data()` and `surface_get_mime_data()` create interned MIME
  type strings with `PyUnicode_InternFromString()` and do not release the local
  new references.
- `error_get_type_combined()` builds an argument tuple for `PyType_Type.tp_new()`
  and does not release it.

Many scanner findings in this project are false positives around
`Py_BuildValue("N")` transfer semantics and the local `PyModule_Add()`
compatibility wrapper.

- Project: `python-c-repos/pycairo`
- Component: CPython C extension (`cairo/*.c`)
- Category: CPython owned-reference leak / callback cleanup
- Confidence: high

Scan results:

| Tool | Files | Functions | Findings | Relevant findings |
|---|---:|---:|---:|---|
| `cext-review-toolkit` | 19 | 347 | 20 | `surface_set_mime_data`, `error_get_type_combined`, enum/tuple helper noise |
| `py-cext-bugs` | 19 | 347 | 25 | `_raster_source_release_func`, `surface_set_mime_data`, `scaled_font_text_to_glyphs` |

## 1. `scaled_font_text_to_glyphs`: argument tuple leaks inside loops

Current code in `cairo/font.c`:

```c
for(i=0; i < num_glyphs; i++) {
  cairo_glyph_t *glyph = &glyphs[i];
  glyph_args = Py_BuildValue(
    "(kdd)", glyph->index, glyph->x, glyph->y);
  if (glyph_args == NULL)
    goto error;
  pyglyph = PyObject_Call(
    (PyObject *)&PycairoGlyph_Type, glyph_args, NULL);
  if (pyglyph == NULL) {
    Py_DECREF (glyph_args);
    goto error;
  }
  PyList_SET_ITEM (glyph_list, i, pyglyph);
}
```

`Py_BuildValue()` returns a new owned reference. `PyObject_Call()` borrows its
argument tuple and does not steal it. The failure branch decrefs `glyph_args`,
but the normal successful branch inserts only `pyglyph` into the list and
never releases `glyph_args`.

The cluster loop has the same bug:

```c
cluster_args = Py_BuildValue(
  "(ii)", cluster->num_bytes, cluster->num_glyphs);
if (cluster_args == NULL)
  goto error;
pycluster = PyObject_Call(
  (PyObject *)&PycairoTextCluster_Type, cluster_args, NULL);
if (pycluster == NULL) {
  Py_DECREF (cluster_args);
  goto error;
}
PyList_SET_ITEM (cluster_list, i, pycluster);
```

Consequently, every successful `ScaledFont.text_to_glyphs()` call leaks one
tuple per returned glyph and, with cluster output enabled, one additional
tuple per returned cluster. This is deterministic and scales with the amount
of converted text.

Suggested fix at both sites:

```c
pyglyph = PyObject_Call(
  (PyObject *)&PycairoGlyph_Type, glyph_args, NULL);
Py_DECREF(glyph_args);
if (pyglyph == NULL)
  goto error;
```

Apply the equivalent ordering to `cluster_args`.

## 2. `_raster_source_release_func`: leaked callback result on successful `None` return

Current code in `cairo/pattern.c`:

```c
result = PyObject_CallFunction ((PyObject *)user_data, "(O)", pysurface);
if (result == NULL)
  goto error;

if (result != Py_None) {
  Py_DECREF (result);
  PyErr_SetString (PyExc_TypeError,
    "Return value of release callback needs to be None");
  result = NULL;
  goto error;
}

Py_DECREF (pysurface);
PyGILState_Release (gstate);
cairo_surface_destroy (surface);
return;
```

`PyObject_CallFunction()` returns a new reference. If the Python release
callback correctly returns `None`, `result == Py_None` and the function exits
successfully without releasing that new reference.

The non-`None` error path does decref `result`, so the leak only affects the
normal successful callback path.

Suggested fix:

```c
if (result != Py_None) {
  Py_DECREF (result);
  PyErr_SetString (PyExc_TypeError,
    "Return value of release callback needs to be None");
  result = NULL;
  goto error;
}
Py_DECREF (result);

Py_DECREF (pysurface);
PyGILState_Release (gstate);
cairo_surface_destroy (surface);
return;
```

Alternatively, use a shared success cleanup block that decrefs both `result`
and `pysurface`.

## 3. `surface_set_mime_data`: leaked `mime_intern`

Current code in `cairo/surface.c`:

```c
mime_intern = PyUnicode_InternFromString (mime_type);
surface_capsule = PyCapsule_New(o->surface, NULL, NULL);
view_capsule = PyCapsule_New(view, NULL, NULL);
user_data = Py_BuildValue("(NNOO)", surface_capsule, view_capsule, obj, mime_intern);
if (user_data == NULL) {
  PyBuffer_Release (view);
  PyMem_Free (view);
  return NULL;
}
```

`PyUnicode_InternFromString()` returns a new reference. The code uses
`mime_intern` in two ways:

- as the cairo user-data key
- as the fourth item in `user_data` via the `"O"` format

`"O"` increments/keeps a reference in the tuple but does not steal the local
reference. The local `mime_intern` reference is never released on success or
failure paths.

There is also no explicit NULL check for `mime_intern`, `surface_capsule`, or
`view_capsule` before the `Py_BuildValue()` call. If `mime_intern` creation
fails, the subsequent code may pass a null value into `Py_BuildValue()`.

Suggested fix direction:

```c
mime_intern = PyUnicode_InternFromString(mime_type);
if (mime_intern == NULL)
  goto error;

surface_capsule = PyCapsule_New(o->surface, NULL, NULL);
view_capsule = PyCapsule_New(view, NULL, NULL);
user_data = Py_BuildValue("(NNOO)", surface_capsule, view_capsule, obj, mime_intern);
if (user_data == NULL)
  goto error;

...

Py_DECREF(mime_intern);
Py_RETURN_NONE;
```

Every return path after `mime_intern` is created should decref it exactly once.
Because `mime_intern` is also stored inside `user_data`, the tuple keeps its own
reference.

## 4. `surface_get_mime_data`: leaked `mime_intern`

Current code:

```c
mime_intern = PyUnicode_InternFromString (mime_type);
user_data = cairo_surface_get_user_data(
  o->surface, (cairo_user_data_key_t *)mime_intern);

if (user_data == NULL) {
  /* In case the mime data wasn't set through the Python API just copy it */
  return Py_BuildValue("y#", buffer, buffer_len);
} else {
  obj = PyTuple_GET_ITEM(user_data, 2);
  Py_INCREF(obj);
  return obj;
}
```

Again, `PyUnicode_InternFromString()` returns a new reference. After the
interned object is used as the cairo user-data key, the local reference should
be released before either return path.

Suggested fix:

```c
mime_intern = PyUnicode_InternFromString(mime_type);
if (mime_intern == NULL)
  return NULL;

user_data = cairo_surface_get_user_data(
  o->surface, (cairo_user_data_key_t *)mime_intern);
Py_DECREF(mime_intern);

if (user_data == NULL) {
  return Py_BuildValue("y#", buffer, buffer_len);
}
obj = PyTuple_GET_ITEM(user_data, 2);
Py_INCREF(obj);
return obj;
```

## 5. `error_get_type_combined`: leaked `new_type_args`

Current code in `cairo/error.c`:

```c
class_dict = PyDict_New ();
if (class_dict == NULL)
    return NULL;

new_type_args = Py_BuildValue ("s(OO)O", name,
                               error, other, class_dict);
Py_DECREF (class_dict);
if (new_type_args == NULL)
    return NULL;

new_type = PyType_Type.tp_new (&PyType_Type, new_type_args, NULL);
return new_type;
```

`Py_BuildValue()` returns a new tuple. `PyType_Type.tp_new()` receives the tuple
as an argument; it does not steal the caller's reference to `new_type_args`.

Suggested fix:

```c
new_type = PyType_Type.tp_new(&PyType_Type, new_type_args, NULL);
Py_DECREF(new_type_args);
return new_type;
```

## Notes On Likely False Positives

Several scanner findings look like false positives after manual review:

- `exec_cairo()` reports `capi`, but this project defines a compatibility
  `PyModule_Add()` wrapper that calls `PyModule_AddObjectRef()` and then
  `Py_XDECREF(value)`, so the local capsule reference is handled.
- `surface_set_mime_data()` reports `surface_capsule` and `view_capsule`, but
  they are passed to `Py_BuildValue("(NNOO)", ...)`; the `N` format steals those
  new references.
- `scaled_font_text_to_glyphs()` reports `glyph_list`, `cluster_list`, and
  `flags`; those specific references are transferred by
  `Py_BuildValue("(NNN)")` or returned directly. In contrast, the
  `glyph_args` and `cluster_args` references described above are genuine
  leaks.
- `int_enum_reduce()` reports `num`, but `Py_BuildValue("(O, (N))", ...)`
  transfers `num`.
- The tuple/repr helpers in `glyph.c`, `rectangle.c`, `textcluster.c`, and
  `textextents.c` create temporary tuples or format strings and release them
  after the call. These look like scanner template false positives.

## Overall assessment

The strongest issue is `scaled_font_text_to_glyphs()` because it is a
deterministic success-path leak in loops and the number of leaked tuples grows
with every converted glyph and cluster.

The raster callback leak is also deterministic whenever a release callback
correctly returns `None`. The `surface.c` MIME-data interned-string leaks are
triggered by ordinary `set_mime_data()` / `get_mime_data()` usage. The
`error_get_type_combined()` leak is smaller, but remains a straightforward
owned-reference cleanup bug.

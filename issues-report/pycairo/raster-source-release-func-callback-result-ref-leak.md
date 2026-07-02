# Reference leak in `_raster_source_release_func()` callback result

I found a reference leak in `_raster_source_release_func()`. When the Python
release callback correctly returns `None`, its return value is never released,
leaking one object per successful release.

File: `cairo/pattern.c`

Function: `_raster_source_release_func`

Relevant code:

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

`PyObject_CallFunction()` returns a new reference. If the callback returns `None`
(the documented, correct behavior), `result == Py_None` and the function reaches
the successful `return` without ever decref'ing `result`.

The non-`None` branch does decref `result`, so only the normal successful path
leaks.

Suggested fix — decref `result` on the success path:

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

Alternatively, use a shared success-cleanup block that decrefs both `result` and
`pysurface`.

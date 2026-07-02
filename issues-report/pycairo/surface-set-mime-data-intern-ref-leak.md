# Reference leak in `surface_set_mime_data()` interned MIME-type string

I found a reference leak in `surface_set_mime_data()`. The interned MIME-type
string created with `PyUnicode_InternFromString()` is never released, leaking one
reference per `Surface.set_mime_data()` call.

File: `cairo/surface.c`

Function: `surface_set_mime_data`

Relevant code:

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

`PyUnicode_InternFromString()` returns a new owned reference. `mime_intern` is
used two ways:

- as the cairo user-data key, and
- as the fourth item of `user_data` via the `"O"` format.

The `"O"` format adds a reference held by the tuple but does not steal the local
reference. `mime_intern` is never decref'd on any path (the success return, or
either of the two error returns after `cairo_surface_set_user_data` /
`cairo_surface_set_mime_data` fail).

Because the string is interned, this is safe to fix: the intern table keeps a
permanent reference, so the key address stays valid after the extra local
reference is dropped.

Secondary issue: there is no NULL check for `mime_intern`, `surface_capsule`, or
`view_capsule` before the `Py_BuildValue()` call.

Suggested fix — check and release `mime_intern` on every path after it is
created:

```c
mime_intern = PyUnicode_InternFromString(mime_type);
if (mime_intern == NULL)
  goto error;   /* release view/buffer, return NULL */

surface_capsule = PyCapsule_New(o->surface, NULL, NULL);
view_capsule = PyCapsule_New(view, NULL, NULL);
user_data = Py_BuildValue("(NNOO)", surface_capsule, view_capsule, obj, mime_intern);
if (user_data == NULL)
  goto error;

...

Py_DECREF(mime_intern);
Py_RETURN_NONE;
```

The tuple keeps its own reference via the `"O"` format, so decref'ing the local
`mime_intern` exactly once on each return path is correct.

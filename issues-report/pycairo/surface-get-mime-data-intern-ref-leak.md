# Reference leak in `surface_get_mime_data()` interned MIME-type string

I found a reference leak in `surface_get_mime_data()`. The interned MIME-type
string created with `PyUnicode_InternFromString()` is never released, leaking one
reference per `Surface.get_mime_data()` call.

File: `cairo/surface.c`

Function: `surface_get_mime_data`

Relevant code:

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

`PyUnicode_InternFromString()` returns a new owned reference. `mime_intern` is
used only as the cairo user-data key, then neither return path releases it, so
one reference leaks on every call.

As with `set_mime_data()`, the string is interned, so the intern table keeps a
permanent reference and the key address stays valid after the local reference is
dropped.

Suggested fix — release `mime_intern` once, right after it is used as the key:

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

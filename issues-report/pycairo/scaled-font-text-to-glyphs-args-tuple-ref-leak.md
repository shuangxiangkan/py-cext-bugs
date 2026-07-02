# Reference leaks in `scaled_font_text_to_glyphs()` argument tuples

I found reference leaks in `scaled_font_text_to_glyphs()`. On the successful path
it leaks one argument tuple per converted glyph and, when cluster output is
enabled, one additional tuple per converted cluster.

File: `cairo/font.c`

Function: `scaled_font_text_to_glyphs`

Relevant code (glyph loop):

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
but the successful branch inserts only `pyglyph` into the list and never
releases `glyph_args`.

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

Every successful `ScaledFont.text_to_glyphs()` call leaks one tuple per returned
glyph and, with cluster output enabled, one additional tuple per returned
cluster. The leak is deterministic and scales with the amount of converted text.

Suggested fix at both sites — decref the args tuple right after the call:

```c
pyglyph = PyObject_Call(
  (PyObject *)&PycairoGlyph_Type, glyph_args, NULL);
Py_DECREF (glyph_args);
if (pyglyph == NULL)
  goto error;
PyList_SET_ITEM (glyph_list, i, pyglyph);
```

Apply the equivalent ordering to `cluster_args`.

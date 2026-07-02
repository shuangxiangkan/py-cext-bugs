# Reference leak in `bucket_sub()` / `bucket_or()` / `bucket_and()` argument tuple

I found a reference leak in the bucket set-operation operators `bucket_sub()`,
`bucket_or()`, and `bucket_and()`. Each builds a temporary argument tuple with
`Py_BuildValue("OO", ...)` and passes it to a module-level helper without ever
releasing it, leaking one tuple per `-` / `|` / `&` operation on a bucket.

File: `src/BTrees/BucketTemplate.c`

Functions: `bucket_sub`, `bucket_or`, `bucket_and`

Relevant code:

```c
static PyObject *
bucket_sub(PyObject *self, PyObject *other)
{
    PyObject *args = Py_BuildValue("OO", self, other);
    return difference_m(NULL, args);
}

static PyObject *
bucket_or(PyObject *self, PyObject *other)
{
    PyObject *args = Py_BuildValue("OO", self, other);
    return union_m(NULL, args);
}

static PyObject *
bucket_and(PyObject *self, PyObject *other)
{
    PyObject *args = Py_BuildValue("OO", self, other);
    return intersection_m(NULL, args);
}
```

`Py_BuildValue("OO", ...)` returns a new owned reference to a tuple. The helper
functions only *borrow* the tuple — they parse it with `PyArg_ParseTuple` and
neither steal nor release it:

```c
static PyObject *
difference_m(PyObject *ignored, PyObject *args)
{
  PyObject *o1, *o2;
  UNLESS(PyArg_ParseTuple(args, "OO", &o1, &o2)) return NULL;
  ...
}
```

Each operator returns the helper's result directly and never decrefs `args`, so
the temporary tuple is leaked on every call.

Secondary issue: `args` is not NULL-checked before use. If `Py_BuildValue()`
fails it returns NULL, which is then passed straight into the helper's
`PyArg_ParseTuple`.

Note: `BucketTemplate.c` is included into every concrete BTree family module
(`_OOBTree`, `_IIBTree`, `_OIBTree`, …), so this leak is compiled into every
bucket type.

Suggested fix (apply to all three):

```c
static PyObject *
bucket_sub(PyObject *self, PyObject *other)
{
    PyObject *args = Py_BuildValue("OO", self, other);
    PyObject *result;

    if (args == NULL)
        return NULL;
    result = difference_m(NULL, args);
    Py_DECREF(args);
    return result;
}
```

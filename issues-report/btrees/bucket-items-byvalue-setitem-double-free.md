# Latent double-free on `PyList_SetItem()` error path in `bucket_items()` / `bucket_byValue()`

I found a latent reference-counting bug on the `PyList_SetItem()` failure path in
`bucket_items()` and `bucket_byValue()`. `PyList_SetItem()` steals its item
reference (and decrefs it even on failure), but the shared error block decrefs the
same item again — a double-free if the failure branch were ever reached.

File: `src/BTrees/BucketTemplate.c`

Functions: `bucket_items`, `bucket_byValue`

Relevant code (`bucket_items`):

```c
if (PyList_SetItem(r, i-low, item) < 0)
    goto err;

item = 0;

...

err:
    PER_UNUSE(self);
    Py_XDECREF(r);
    Py_XDECREF(item);
    return NULL;
```

`PyList_SetItem()` steals the `item` reference on success and decrefs it on
failure. On the failure branch, `item` is still non-NULL (it is only cleared with
`item = 0` *after* the successful check), so `Py_XDECREF(item)` in the `err:`
block releases the same object a second time.

`bucket_byValue()` has the identical pattern with `PyList_SetItem(r, l, item)`.

Severity: latent. In both functions the target list is freshly created with the
exact required size (`r = PyList_New(high-low+1)` / `PyList_New(l)`) and the write
indexes (`i-low`, and the pre-counted `l`) are always in range, so
`PyList_SetItem()` cannot actually fail here. This is incorrect defensive error
handling rather than a reachable bug, and is lower priority than the runtime
leaks elsewhere in the extension.

Suggested defensive fix — clear `item` before jumping, since `PyList_SetItem()`
already released it:

```c
if (PyList_SetItem(r, i-low, item) < 0) {
    item = NULL;  /* PyList_SetItem already stole/released it */
    goto err;
}
```

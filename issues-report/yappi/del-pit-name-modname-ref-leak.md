# `_del_pit()` leaks `pit->name` and `pit->modname`

I found a reference leak in `_del_pit()`: it releases `pit->fn_descriptor` but
never releases `pit->name` or `pit->modname`, both of which hold owned
references. Every time a `_pit` is destroyed (e.g. on `clear_stats()`), its name
and module-name objects leak.

File: `yappi/_yappi.c`

Function: `_del_pit`

Relevant code:

```c
// the pit will be cleared by the relevant freelist. we do not free it here.
// we only DECREF the CodeObject or the MethodDescriptive string.
static void
_del_pit(_pit *pit)
{
    // the pit will be freed by fldestrot() in clear_stats, otherwise it stays
    // for later enumeration
    _pit_children_info *it, *next;

    // free children
    it = pit->children;
    while (it) {
        next = (_pit_children_info *)it->next;
        yfree(it);
        it = next;
    }
    pit->children = NULL;
    Py_DECREF(pit->fn_descriptor);
    /* pit->name and pit->modname are never released */
}
```

`pit->name` and `pit->modname` are always assigned owned references:

- `pit->modname = _pycfunction_module_name(cfn)` (returns a new reference)
- `pit->name = PyObject_Repr(mo)` / `PyStr_FromString(...)` / `PyStr_FromFormat(...)`
- in the Python-code path: `Py_INCREF(cobj->co_filename); pit->modname = cobj->co_filename;`
  and `Py_INCREF(cobj->co_name); pit->name = cobj->co_name;`

`_del_pit()` is the per-pit cleanup (called via `_pitenumdel()` when stats are
cleared), and no other code path releases `pit->name` / `pit->modname`. So both
leak whenever a pit is destroyed — accumulating across repeated profile /
`clear_stats()` cycles, proportional to the number of profiled functions.

## On the existing comment

The comment above `_del_pit()` says it "only DECREF the CodeObject or the
MethodDescriptive string." That string is `pit->fn_descriptor`, and releasing it
is correct. But `pit->name` and `pit->modname` are **separately owned**
references, not aliases of `pit->fn_descriptor`:

- In the Python-code path, `fn_descriptor` is the code object, while `name` /
  `modname` are its `co_name` / `co_filename` attributes, each independently
  `Py_INCREF`-ed — so `Py_DECREF(fn_descriptor)` does not release them.
- In the C-function path, `name` / `modname` are freshly created strings
  (`PyObject_Repr`, `PyStr_FromString`, `_pycfunction_module_name`), unrelated to
  `fn_descriptor`.

And `fldestroy()` only reclaims the pit struct's memory from the freelist; it
does not touch the Python references stored inside the pit. So nothing releases
`name` / `modname`, and they leak (real string objects in the C-function path).

Suggested fix:

```c
    pit->children = NULL;
    Py_XDECREF(pit->name);
    Py_XDECREF(pit->modname);
    Py_DECREF(pit->fn_descriptor);
```

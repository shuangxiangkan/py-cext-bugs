# python-ldap: likely error-path reference leaks in LDAP message and mod conversion helpers

## Summary

`python-ldap` has several likely CPython reference-counting leaks in its
hand-written C extension. The strongest candidates are error paths:

- `Tuple_to_LDAPMod()` leaks an item returned by `PySequence_GetItem()` when
  the item has the wrong type.
- `LDAPmessage_to_python()` leaks `attrdict` and sometimes `pyctrls` on several
  LDAP/Python allocation error paths.
- `LDAPraise_for_message()` leaks the partially-built `info` dictionary if
  control conversion fails.
- `LDAPinit_constants()` has macro-generated import/init failure paths that can
  leak exception objects and integer objects.

- Project: `python-c-repos/python-ldap`
- Component: CPython C extension (`Modules/*.c`)
- Category: CPython owned-reference leak / error-path cleanup
- Confidence: high for `Tuple_to_LDAPMod`, medium-high for
  `LDAPmessage_to_python` and `LDAPraise_for_message`, medium for module init
  cleanup paths

Scan results:

| Tool | Files | Functions | Findings | Relevant findings |
|---|---:|---:|---:|---|
| `cext-review-toolkit` | 9 | 58 | 11 | `LDAPmessage_to_python`, `LDAPraise_for_message`, constants/error helpers |
| `py-cext-bugs` | 9 | 58 | 12 | `Tuple_to_LDAPMod`, `LDAPmessage_to_python`, `LDAPraise_for_message`, `LDAPinit_constants` |

Result files:

- `scan-results/python-ldap-cext-review-toolkit-refcounts.json`
- `scan-results/python-ldap-py-cext-bugs-refcount.json`

## Affected Sites

| Function | File:line | Leaked value | Confidence |
|---|---|---|---|
| `Tuple_to_LDAPMod` | `Modules/LDAPObject.c:166` | `item` from `PySequence_GetItem()` on wrong-type error path | High |
| `LDAPmessage_to_python` | `Modules/message.c:55` | `attrdict` when `ldap_get_entry_controls()` fails | High |
| `LDAPmessage_to_python` | `Modules/message.c:72` | `attrdict` when `LDAPControls_to_List()` fails | High |
| `LDAPmessage_to_python` | `Modules/message.c:161` | `attrdict` and `pyctrls` when `PyUnicode_FromString(dn)` fails | High |
| `l_ldap_str2dn` | `Modules/functions.c:140` | local `rdnlist` ref after append-to-parent when inner append fails | Medium-high |
| `LDAPraise_for_message` | `Modules/constants.c:138` | `info` dict when `LDAPControls_to_List()` fails | Medium-high |
| `LDAPinit_constants` | `Modules/constants.c:242` | `exc` / `nobj` in `add_err` macro failure paths | Medium |

## 1. `Tuple_to_LDAPMod`: leaked sequence item on wrong-type path

Current code in `Modules/LDAPObject.c`:

```c
item = PySequence_GetItem(list, i);
if (item == NULL)
    goto error;
if (!PyBytes_Check(item)) {
    LDAPerror_TypeError
        ("Tuple_to_LDAPMod(): expected a byte string in the list",
         item);
    goto error;
}
lm->mod_bvalues[i]->bv_len = PyBytes_Size(item);
lm->mod_bvalues[i]->bv_val = PyBytes_AsString(item);
Py_DECREF(item);
```

`PySequence_GetItem()` returns a new reference. On the success path, `item` is
released after extracting bytes data. On the wrong-type error path, the function
sets a `TypeError` and jumps to `error` without releasing `item`.

Suggested fix:

```c
if (!PyBytes_Check(item)) {
    LDAPerror_TypeError(
        "Tuple_to_LDAPMod(): expected a byte string in the list",
        item);
    Py_DECREF(item);
    goto error;
}
```

This is a straightforward owned-reference leak on an invalid-input path.

## 2. `LDAPmessage_to_python`: leaked `attrdict` on entry-control failure

Current code in `Modules/message.c`:

```c
attrdict = PyDict_New();
if (attrdict == NULL) {
    Py_DECREF(result);
    ldap_msgfree(m);
    ldap_memfree(dn);
    return NULL;
}

rc = ldap_get_entry_controls(ld, entry, &serverctrls);
if (rc) {
    Py_DECREF(result);
    ldap_msgfree(m);
    ldap_memfree(dn);
    return LDAPerror(ld);
}
```

`attrdict` is a new reference. If `ldap_get_entry_controls()` fails, the error
path releases `result`, the LDAP message, and `dn`, but not `attrdict`.

Suggested fix:

```c
if (rc) {
    Py_DECREF(attrdict);
    Py_DECREF(result);
    ldap_msgfree(m);
    ldap_memfree(dn);
    return LDAPerror(ld);
}
```

## 3. `LDAPmessage_to_python`: leaked `attrdict` when control conversion fails

Current code:

```c
if (!(pyctrls = LDAPControls_to_List(serverctrls))) {
    int err = LDAP_NO_MEMORY;

    ldap_set_option(ld, LDAP_OPT_ERROR_NUMBER, &err);
    Py_DECREF(result);
    ldap_msgfree(m);
    ldap_memfree(dn);
    ldap_controls_free(serverctrls);
    return LDAPerror(ld);
}
```

At this point `attrdict` has already been created. If
`LDAPControls_to_List()` fails, the function leaks `attrdict`.

Suggested fix:

```c
Py_DECREF(attrdict);
Py_DECREF(result);
ldap_msgfree(m);
ldap_memfree(dn);
ldap_controls_free(serverctrls);
return LDAPerror(ld);
```

## 4. `LDAPmessage_to_python`: leaked `attrdict` and `pyctrls` when DN conversion fails

Current code:

```c
pydn = PyUnicode_FromString(dn);
if (pydn == NULL) {
    Py_DECREF(result);
    ldap_msgfree(m);
    ldap_memfree(dn);
    return NULL;
}
```

By this point, both `attrdict` and `pyctrls` may be owned local references:

- `attrdict` was created by `PyDict_New()`.
- `pyctrls` was created by `LDAPControls_to_List(serverctrls)`.

If `PyUnicode_FromString(dn)` fails, neither is released.

Suggested fix:

```c
if (pydn == NULL) {
    Py_DECREF(attrdict);
    Py_XDECREF(pyctrls);
    Py_DECREF(result);
    ldap_msgfree(m);
    ldap_memfree(dn);
    return NULL;
}
```

## 5. `l_ldap_str2dn`: local `rdnlist` ref leak after parent append

Current code in `Modules/functions.c`:

```c
rdnlist = PyList_New(0);
if (!rdnlist)
    goto failed;
if (PyList_Append(tmp, rdnlist) == -1) {
    Py_DECREF(rdnlist);
    goto failed;
}

for (j = 0; rdn[j]; j++) {
    ...
    if (PyList_Append(rdnlist, tuple) == -1) {
        Py_DECREF(tuple);
        goto failed;
    }
    Py_DECREF(tuple);
}
Py_DECREF(rdnlist);
```

After `PyList_Append(tmp, rdnlist)` succeeds, the parent list owns one
reference and the local variable still owns another. The normal path releases
the local reference at the end of the loop.

If the inner `PyList_Append(rdnlist, tuple)` fails, the function jumps to
`failed` without releasing the local `rdnlist` reference. Cleanup of `tmp` will
release the parent-list reference, but not the local reference.

Suggested fix:

```c
if (PyList_Append(rdnlist, tuple) == -1) {
    Py_DECREF(tuple);
    Py_DECREF(rdnlist);
    goto failed;
}
```

## 6. `LDAPraise_for_message`: leaked `info` dict on control conversion failure

Current code in `Modules/constants.c`:

```c
info = PyDict_New();
if (info == NULL) {
    ldap_memfree(matched);
    ldap_memfree(error);
    ldap_memvfree((void **)refs);
    ldap_controls_free(serverctrls);
    return NULL;
}

...

if (!(pyctrls = LDAPControls_to_List(serverctrls))) {
    int err = LDAP_NO_MEMORY;

    ldap_set_option(l, LDAP_OPT_ERROR_NUMBER, &err);
    ldap_memfree(matched);
    ldap_memfree(error);
    ldap_memvfree((void **)refs);
    ldap_controls_free(serverctrls);
    return PyErr_NoMemory();
}
```

If `LDAPControls_to_List()` fails, `info` has already been allocated and partly
filled. The error path releases LDAP-owned resources but not the Python dict.

Suggested fix:

```c
Py_DECREF(info);
ldap_memfree(matched);
ldap_memfree(error);
ldap_memvfree((void **)refs);
ldap_controls_free(serverctrls);
return PyErr_NoMemory();
```

## 7. `LDAPinit_constants`: macro-generated init failure leaks

Current `add_err` macro:

```c
#define add_err(n) do {  \
    exc = PyErr_NewException("ldap." #n, LDAPexception_class, NULL);  \
    if (exc == NULL) return -1;  \
    nobj = PyLong_FromLong(LDAP_##n); \
    if (nobj == NULL) return -1; \
    if (PyObject_SetAttrString(exc, "errnum", nobj) != 0) return -1; \
    Py_DECREF(nobj); \
    errobjects[LDAP_##n+LDAP_ERROR_OFFSET] = exc;  \
    if (PyModule_AddObject(m, #n, exc) != 0) return -1;  \
    Py_INCREF(exc);  \
} while (0)
```

Several failure paths return without releasing owned references:

- If `PyLong_FromLong()` fails, `exc` is leaked.
- If `PyObject_SetAttrString()` fails, both `exc` and `nobj` are leaked.
- If `PyModule_AddObject()` fails, `exc` is not released.

This is import-time error-path cleanup, so lower impact than request/runtime
leaks, but the ownership issue is real.

The macro also stores `exc` in `errobjects` and adds it to the module. The exact
success-path ownership should be reviewed carefully before changing it, but the
failure paths should use cleanup labels or explicit decrefs.

## Likely False Positives / Lower Priority Findings

Some scanner findings do not look like real leaks:

- `LDAPerror_TypeError()` and `LDAPerr()` build `args` with `Py_BuildValue()`,
  pass it to `PyErr_SetObject()`, and then release it with `Py_DECREF(args)`.
- `LDAPControls_to_List()` uses `PyList_SET_ITEM(res, i, pyctrl)`, which steals
  the `pyctrl` reference on success.
- The `borrowed_ref_across_call` warning around `PyDict_GetItem()` in
  `LDAPmessage_to_python()` appears safe because the code immediately turns the
  borrowed reference into an owned reference with `Py_INCREF(valuelist)`.

## Overall Assessment

This is a good report candidate. The strongest issue is the
`Tuple_to_LDAPMod()` invalid-input leak, followed by several clear
`LDAPmessage_to_python()` and `LDAPraise_for_message()` error-path cleanup
leaks. Most are not normal success-path leaks, but they are based on standard
CPython ownership rules and should be straightforward to fix with local cleanup
changes.

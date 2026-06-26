# psutil: possible refcount bugs in platform-specific error paths

## Summary

`psutil` has several possible CPython reference-counting bugs in
platform-specific C extension code. Most are error-path cleanup issues: an
owned Python object is created, a later operation fails, and the function either
returns without releasing the object or jumps to a cleanup block that may
`Py_DECREF()` a NULL pointer.

- Project: `python-c-repos/psutil`
- Component: hand-written C extension code under `psutil/arch/`
- Category: CPython reference leak / invalid `Py_DECREF`
- Confidence: medium candidate, should be confirmed by maintainers or tests

Both scanners reported this area, but with different noise levels:

| Tool | Files | Functions | Findings |
|---|---:|---:|---:|
| `cext-review-toolkit` | 84 | 312 | 78 |
| `py-cext-bugs` | 84 | 312 | 33 |

Result files:

- `scan-results/psutil-cext-review-toolkit-refcounts.json`
- `scan-results/psutil-py-cext-bugs-refcount.json`

## Candidate Issues

| Function | File:line | Issue |
|---|---|---|
| `psutil_disk_partitions` | `psutil/arch/linux/disk.c:22` | `py_retlist` may leak if argument parsing fails |
| `psutil_disk_io_counters` | `psutil/arch/sunos/disk.c:21` | `py_retdict` may leak if `kstat_read()` fails |
| `psutil_proc_environ` | `psutil/arch/sunos/proc.c:179` | `py_retdict` may leak if argument parsing fails |
| `psutil_winservice_enumerate` | `psutil/arch/windows/services.c:136` | `py_retlist` may leak if `OpenSCManager()` fails |
| `psutil_winservice_enumerate` | `psutil/arch/windows/services.c:173` | `error:` may `Py_DECREF(py_name)` when `py_name == NULL` |
| `psutil_net_connections` | `psutil/arch/windows/socks.c:122` | early error paths may `Py_DECREF()` NULL objects |

## Details

### Linux `psutil_disk_partitions`: leaked list on parse failure

Current code creates `py_retlist` before parsing arguments:

```c
PyObject *py_retlist = PyList_New(0);

if (py_retlist == NULL)
    return NULL;

if (!PyArg_ParseTuple(args, "s", &mtab_path))
    return NULL;
```

`PyList_New(0)` returns a new reference. If `PyArg_ParseTuple()` fails, the
function returns `NULL` without `Py_DECREF(py_retlist)`. The later `error:`
cleanup block does release `py_retlist`, but this direct return bypasses it.

### SunOS `psutil_disk_io_counters`: leaked dict on `kstat_read()` failure

`py_retdict` is allocated before walking kstats:

```c
PyObject *py_retdict = PyDict_New();
...
if (kstat_read(kc, ksp, &kio) == -1) {
    kstat_close(kc);
    return psutil_oserror();
}
```

If `kstat_read()` fails, the function closes `kc` and returns immediately.
That bypasses the `error:` block that would `Py_DECREF(py_retdict)`.

### SunOS `psutil_proc_environ`: leaked dict on parse failure

`psutil_proc_environ()` has the same allocate-before-parse shape:

```c
PyObject *py_retdict = PyDict_New();

if (!py_retdict)
    return PyErr_NoMemory();

if (!PyArg_ParseTuple(args, "is", &pid, &procfs_path))
    return NULL;
```

If parsing fails, `py_retdict` is still owned by the function and is not
released. A small cleanup fix would be to parse arguments before allocating the
dict, or to `Py_DECREF(py_retdict)` before returning.

### Windows `psutil_winservice_enumerate`: leaked list on `OpenSCManager()` failure

`py_retlist` is created before `OpenSCManager()`:

```c
PyObject *py_retlist = PyList_New(0);
...
sc = OpenSCManager(NULL, NULL, SC_MANAGER_ENUMERATE_SERVICE);
if (sc == NULL) {
    psutil_oserror_wsyscall("OpenSCManager");
    return NULL;
}
```

If `OpenSCManager()` fails, the owned list is not released.

The same function also has a possible invalid DECREF in the shared error path:

```c
py_name = PyUnicode_FromWideChar(
    lpService[i].lpServiceName, wcslen(lpService[i].lpServiceName)
);
if (py_name == NULL)
    goto error;

...
error:
    Py_DECREF(py_name);
```

When `PyUnicode_FromWideChar()` fails, `py_name` is NULL and the `error:` label
uses `Py_DECREF(py_name)` rather than `Py_XDECREF(py_name)`. That looks like a
possible NULL dereference on allocation failure.

### Windows `psutil_net_connections`: early error path can DECREF NULL

`psutil_net_connections()` initializes `py_retlist` to NULL, creates several
temporary `PyLong` objects, then parses arguments:

```c
PyObject *py_retlist = NULL;
PyObject *_AF_INET = PyLong_FromLong((long)AF_INET);
PyObject *_AF_INET6 = PyLong_FromLong((long)AF_INET6);
PyObject *_SOCK_STREAM = PyLong_FromLong((long)SOCK_STREAM);
PyObject *_SOCK_DGRAM = PyLong_FromLong((long)SOCK_DGRAM);

if (!PyArg_ParseTuple(args, _Py_PARSE_PID "OO", &pid, &py_af_filter, &py_type_filter)) {
    goto error;
}
```

The `error:` label later does:

```c
error:
    psutil_conn_decref_objs();
    Py_XDECREF(py_addr_tuple_local);
    Py_XDECREF(py_addr_tuple_remote);
    Py_DECREF(py_retlist);
```

If argument parsing fails, `py_retlist` has not been created yet, so
`Py_DECREF(py_retlist)` may receive NULL. The temporary `_AF_INET` /
`_AF_INET6` / `_SOCK_STREAM` / `_SOCK_DGRAM` objects are also not NULL-checked
before the macro decrefs them, so allocation failure in one of those
`PyLong_FromLong()` calls may create another invalid DECREF path.

## Why These Look Plausible

These candidates are not ordinary scanner-only reports. In each case, the
source shows a concrete ownership path:

1. A CPython API returns a new owned reference.
2. A later operation can fail.
3. The failing path either returns before cleanup or enters cleanup with a NULL
   object that is released with `Py_DECREF()` instead of `Py_XDECREF()`.

The strongest candidates are the direct early-return leaks in
`linux/disk.c`, `sunos/disk.c`, `sunos/proc.c`, and `windows/services.c`, plus
the NULL `Py_DECREF` paths in `windows/services.c` and `windows/socks.c`.

## Suggested Fix Direction

The fixes are small and local:

- Parse arguments before allocating result containers where possible.
- Replace direct early `return NULL` paths with `goto error` after setting the
  Python exception, when an owned object has already been created.
- Use `Py_XDECREF()` for cleanup labels that may be reached before an object is
  initialized.
- Check the temporary `PyLong_FromLong()` allocations in `windows/socks.c`
  before using or decrefing those objects.

## Notes On Scanner Noise

Many other scanner findings in this project appear lower confidence:

- `PyModule_AddObject()` failure paths that `Py_DECREF()` the object are usually
  correct because `PyModule_AddObject()` steals only on success.
- Several `PyList_SetItem()` findings are likely false positives where the code
  sets the local variable to NULL after the steal.
- `cext-review-toolkit` reports many allocation NULL-check paths as
  `potential_leak_on_error`; most of those are not real leaks.

This report therefore focuses only on the platform-specific paths where manual
source review found a concrete missing cleanup or invalid DECREF possibility.

> Note for disclosure: this report is from static analysis plus source review;
> it has not been confirmed with a runtime reproducer on the affected platforms.

# Refcount scan triage summary

This is a simple summary of the projects scanned with:

- `cext-review-toolkit`
- `py-cext-bugs`

## Counting notes

- `cext findings` and `py-cext findings` are read directly from
  `scan-results/*.json`.
- `Manual true bugs` means independent issues that were kept in a written report
  under `refcounts-bug-reports/`.
- `False/unaccepted ~=` is an approximate count: `tool findings - manual true
  bugs`, floored at `0`.
- This is not a strict per-finding precision audit. Some true bugs were found
  manually even when a tool did not report them, and some projects were scanned
  but not written into reports.
- Projects without a report are counted as `0` manual true bugs here; their
  findings are treated as unaccepted/noisy for this simple table.

## Totals

| Scope | cext findings | py-cext findings | manual true bugs | cext false/unaccepted ~= | py-cext false/unaccepted ~= |
|---|---:|---:|---:|---:|---:|
| scanned projects with result JSON | 535 | 684 | 62 | 476 | 623 |
| manual-only report not in scan JSON | - | - | 6 | - | - |

Manual-only report:

- `python-zstandard`: 6 true bug sites in
  [python-zstandard-context-manager-refcount-leaks.md](python-zstandard-context-manager-refcount-leaks.md)

## Per-project summary

| Project | cext findings | py-cext findings | Manual true bugs | cext false/unaccepted ~= | py-cext false/unaccepted ~= | Report |
|---|---:|---:|---:|---:|---:|---|
| `BTrees` | 18 | 35 | 4 | 14 | 31 | [btrees-set-operation-refcount-leaks.md](btrees-set-operation-refcount-leaks.md) |
| `annoy` | 1 | 0 | 0 | 1 | 0 | - |
| `bottleneck` | 0 | 0 | 0 | 0 | 0 | - |
| `contourpy` | 0 | 0 | 0 | 0 | 0 | - |
| `crc32c` | 0 | 0 | 0 | 0 | 0 | - |
| `duckdb-python` | 5 | 6 | 1 | 4 | 5 | [duckdb-python-map-function-args-tuple-ref-leak.md](duckdb-python-map-function-args-tuple-ref-leak.md) |
| `faiss-python` | 0 | 0 | 1 | 0 | 0 | [faiss-sharding-callback-refcount-leak.md](faiss-sharding-callback-refcount-leak.md) |
| `greenlet` | 1 | 1 | 0 | 1 | 1 | - |
| `guppy3` | 13 | 14 | 0 | 13 | 14 | - |
| `hiredis-py` | 6 | 8 | 1 | 5 | 7 | [hiredis-py-pylist-setslice-ref-leak.md](hiredis-py-pylist-setslice-ref-leak.md) |
| `hnswlib` | 0 | 0 | 0 | 0 | 0 | - |
| `markupsafe` | 0 | 0 | 0 | 0 | 0 | - |
| `mmh3` | 0 | 0 | 0 | 0 | 0 | - |
| `mrab-regex` | 32 | 9 | 0 | 32 | 9 | - |
| `msgspec` | 101 | 84 | 1 | 100 | 83 | [msgspec-mpack-decode-ext-refcount-leak.md](msgspec-mpack-decode-ext-refcount-leak.md) |
| `murmurhash` | 0 | 0 | 0 | 0 | 0 | - |
| `numexpr` | 0 | 5 | 2 | 0 | 3 | [numexpr-init-refcount-leaks.md](numexpr-init-refcount-leaks.md) |
| `pillow` | 28 | 6 | 0 | 28 | 6 | - |
| `preshed` | 0 | 0 | 0 | 0 | 0 | - |
| `psutil` | 78 | 33 | 6 | 72 | 27 | [psutil-platform-error-path-refcount-bugs.md](psutil-platform-error-path-refcount-bugs.md) |
| `py-lmdb` | 15 | 14 | 0 | 15 | 14 | - |
| `py-setproctitle` | 0 | 0 | 0 | 0 | 0 | - |
| `py-tree-sitter` | 45 | 55 | 10 | 35 | 45 | [py-tree-sitter-callback-and-query-refcount-leaks.md](py-tree-sitter-callback-and-query-refcount-leaks.md) |
| `pycairo` | 20 | 25 | 4 | 16 | 21 | [pycairo-mime-raster-error-refcount-leaks.md](pycairo-mime-raster-error-refcount-leaks.md) |
| `pycryptodome` | 0 | 0 | 0 | 0 | 0 | - |
| `pycurl` | 15 | 63 | 0 | 15 | 63 | - |
| `pygit2` | 29 | 27 | 8 | 21 | 19 | [pygit2-refdb-filter-refcount-bugs.md](pygit2-refdb-filter-refcount-bugs.md) |
| `pylibmc` | 14 | 17 | 4 | 10 | 13 | [pylibmc-multi-command-refcount-leaks.md](pylibmc-multi-command-refcount-leaks.md) |
| `pyodbc` | 40 | 68 | 4 | 36 | 64 | [pyodbc-driver-datasource-refcount-bugs.md](pyodbc-driver-datasource-refcount-bugs.md) |
| `pyopencl` | 0 | 0 | 0 | 0 | 0 | - |
| `pysat` | 35 | 26 | 4 | 31 | 22 | [pysat-import-and-propagator-refcount-leaks.md](pysat-import-and-propagator-refcount-leaks.md) |
| `pysimdjson` | 0 | 0 | 0 | 0 | 0 | - |
| `python-blosc` | 1 | 1 | 0 | 1 | 1 | - |
| `python-ldap` | 11 | 12 | 7 | 4 | 5 | [python-ldap-error-path-refcount-leaks.md](python-ldap-error-path-refcount-leaks.md) |
| `python-lz4` | 0 | 2 | 0 | 0 | 2 | - |
| `python-rapidjson` | 10 | 152 | 1 | 9 | 151 | [python-rapidjson-pylist-setitem-double-free.md](python-rapidjson-pylist-setitem-double-free.md) |
| `sentencepiece` | 0 | 0 | 0 | 0 | 0 | - |
| `simplejson` | 10 | 11 | 0 | 10 | 11 | - |
| `siphashc` | 0 | 0 | 0 | 0 | 0 | - |
| `snappy` | 0 | 0 | 0 | 0 | 0 | - |
| `snowball` | 0 | 0 | 0 | 0 | 0 | - |
| `tiledb-py` | 0 | 0 | 0 | 0 | 0 | - |
| `yappi` | 7 | 10 | 4 | 3 | 6 | [yappi-context-and-pit-refcount-leaks.md](yappi-context-and-pit-refcount-leaks.md) |

## Quick takeaways

- `py-cext-bugs` often reports fewer noisy findings than `cext-review-toolkit`
  on some projects, but not always. For example, it is much noisier on
  `python-rapidjson`, `pyodbc`, and `pycurl`.
- `cext-review-toolkit` missed some manually found bugs, such as the FAISS
  callback issue and the `numexpr` findings in this simple summary.
- A large number of findings are conservative scanner noise around stolen
  references, module state lifetime, pybind11/RAII ownership, and generated C
  extension patterns.
- The strongest true-positive reports tend to involve simple CPython ownership
  rules: new reference from `PyObject_Call*`, `Py_BuildValue`, `PyList_New`,
  `PyBytes_FromStringAndSize`, or `PySequence_GetItem`, followed by a missing
  `Py_DECREF` on a success or error path.

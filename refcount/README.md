# refcount

CPython C extension reference-count analysis.

Main files:

- `analyzer.py`: recursively scans C/C++ source files, runs the checkers, and emits JSON findings.
- `ownership.py`: immutable ownership state model used by data-flow analysis.
- `ownership_flow.py`: CFG/data-flow based CPython ownership transfer rules.
- `api_tables.json`: CPython C API ownership tables: new refs, borrowed refs, and stolen refs.

Run directly through the top-level CLI:

```bash
python py-cext-bugs/main.py refcount path/to/project
```

## Analysis model

The analyzer combines two styles of checks:

- Legacy local pattern checks for specific high-signal cases, such as borrowed
  refs used after Python-executing calls and releases after stealing APIs.
- Path-aware ownership data-flow for leak detection.

The path-aware analysis uses this pipeline:

1. `analyzer.py` parses each C/C++ file under the requested scan root and extracts functions.
2. `extract/cfg.py` builds a statement-level CFG for each function.
3. `extract/dataflow.py` propagates `OwnershipState` over that CFG.
4. `ownership_flow.py` applies CPython C API transfer rules.
5. Exit nodes are checked for variables that may still be `owned`.

`OwnershipState` tracks each variable as one of:

- `owned`: this path owns a reference and must release, return, steal, or escape it.
- `borrowed`: this path has a borrowed reference.
- `released`: the reference was released by `Py_DECREF`, `Py_XDECREF`, `Py_CLEAR`, etc.
- `stolen`: ownership was transferred to a stealing API.
- `returned`: ownership was returned to the caller.
- `escaped`: ownership was stored outside the local variable, such as a field or global.
- `null`: a NULL-check branch proved the variable is NULL.
- `unknown`: no useful ownership fact is known.
- `mixed`: multiple CFG paths disagree.

## Transfer rules

The first data-flow implementation intentionally handles a conservative subset
of CPython ownership patterns:

- New-reference APIs from `api_tables.json` mark the assigned variable as `owned`.
- Borrowed-reference APIs mark the assigned variable as `borrowed`.
- Release APIs mark the referenced variable as `released`.
- Stealing APIs mark the stolen argument as `stolen`.
- `return obj;` marks `obj` as `returned`.
- `return Py_NewRef(obj);` and other direct new-reference API returns are
  treated as returning a new reference.
- `Py_INCREF(obj)` turns a known borrowed reference into an owned reference.
- `if (obj == NULL)`, `if (NULL == obj)`, `if (obj != NULL)`, `if (!obj)`,
  and `if (obj)` refine true/false branch state.
- Assignments inside conditions, such as
  `if ((obj = PyList_New(0)) == NULL)`, are tracked.
- Simple aliases, such as `PyObject *alias = obj`, are resolved when releasing,
  returning, stealing, or escaping.
- Assignments to fields, globals, and array elements mark the right-hand
  reference as `escaped`.

## Findings

The main data-flow finding is:

- `potential_leak_on_path`: an owned reference may reach a function exit without
  being released, returned, stolen, or escaped.

The analyzer also keeps legacy high-signal checks, including:

- `borrowed_ref_across_call`
- `stolen_ref_not_nulled`
- `stolen_ref_double_free`
- selected legacy leak checks when not covered by path-aware analysis

When a path-aware leak and a legacy leak refer to the same function, variable,
and API call, the path-aware finding is kept and the legacy duplicate is
suppressed.

## Limitations

This is still a static heuristic analyzer. It intentionally does not try to be
a full C compiler or whole-program ownership prover. Known limits include:

- Cross-function ownership contracts are not modeled.
- Macro-heavy cleanup code may be opaque to Tree-sitter.
- `switch` bodies are currently opaque in the CFG.
- Success-only stealing APIs, such as some module-add APIs, are still modeled
  coarsely by the current API table format.
- Escaped references are not proven safe; they are only removed from local leak
  reporting so a reviewer can inspect ownership at a higher level.

Use the findings as candidates for review. The final decision should consider
nearby cleanup code, project conventions, destructor behavior, CPython API
semantics, and generated-code patterns.

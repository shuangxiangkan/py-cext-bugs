# py-cext-bugs

Small toolkit for finding likely CPython C extension reference-counting bugs.

It is designed as a static pre-screening layer: it finds suspicious ownership
paths in C/C++ extension code and emits JSON findings for a human reviewer or
LLM-based reviewer to inspect. It does not compile or execute the target
project.

It has three main layers:

- `analysis/`: generic, check-agnostic C/C++ code analysis (parsing, control flow, data flow, source discovery).
- `refcount/`: CPython C API ownership/refcount analysis.
- `tests/`: demo C cases and unit tests.

## Requirements

```bash
pip install -r requirements.txt
```

This installs `tree-sitter`, `tree-sitter-c`, and `tree-sitter-cpp`. C++ files
(`.cpp`, `.cc`, `.cxx`, `.hpp`) are scanned by default. If `tree-sitter-cpp` is
missing, C++ sources are skipped (not mis-parsed) and a warning is emitted.

C++ support covers C-style function bodies plus inline class/struct methods,
`extern "C"`/`namespace` blocks, range-for, basic try/catch control flow, common
casts, and configurable RAII wrapper handoff for new-reference APIs. It does
**not** model full C++ object lifetime semantics such as move/copy behavior,
class-member ownership, smart pointers, or pybind11/nanobind `py::object`.

Run refcount analysis:

```bash
python py-cext-bugs/cli.py refcount path/to/project
```

Output is JSON printed to stdout. Redirect it if you want a file:

```bash
python py-cext-bugs/cli.py refcount path/to/project > refcount_findings.json
```

## How refcount analysis works

The refcount analyzer runs in several stages:

1. Discover C/C++ source files.
   `analysis/sources.py` recursively finds C/C++ source files under the target,
   excluding common generated, build, and virtualenv directories.

2. Parse C/C++ source with Tree-sitter.
   `analysis/parsing.py` extracts functions, calls, assignments,
   return statements, declarations, and related source locations.

3. Build a small intraprocedural CFG.
   `analysis/controlflow.py` models statement-level control flow, including sequential
   statements, `if`/`else`, `goto` labels, `return`, loops, `break`, and
   `continue`. This lets the analyzer understand common CPython cleanup
   patterns such as `goto error; ... error: Py_XDECREF(obj); return NULL;`.

4. Run forward data-flow.
   `analysis/dataflow.py` propagates state over the CFG to a fixed point. The
   refcount layer uses it to track ownership state along each path.

5. Track ownership state.
   `refcount/ownership_state.py` represents variables as `owned`, `borrowed`,
   `released`, `stolen`, `returned`, `escaped`, `null`, `unknown`, or `mixed`.
   `refcount/ownership_transfer.py` applies CPython API semantics from
   `api_ownership.json` to update those states.

6. Emit findings.
   `refcount/analyzer.py` combines legacy pattern checks with the newer
   path-aware ownership analysis. When both old linear checks and data-flow
   checks report the same leak family, the path-aware finding is preferred.

## Current precision improvements

The analyzer includes several optimizations to reduce false positives:

- Path-aware leak detection: reports an owned reference only if it reaches a
  function exit on a real CFG path without being released, returned, stolen, or
  escaped.
- Cleanup-label handling through CFG edges: `goto error` and similar patterns
  naturally flow through cleanup blocks.
- NULL-guard refinement: conditions such as `if (obj == NULL)` and `if (!obj)`
  refine the true/false branch state.
- Condition assignment tracking: patterns such as
  `if ((obj = PyList_New(0)) == NULL)` are recognized.
- Simple alias tracking: `PyObject *alias = obj; Py_DECREF(alias);` updates the
  original object.
- Escape tracking: assignments into fields, globals, or array slots such as
  `self->cached = obj` mark the object as escaped instead of leaked.
- Direct return call handling: `return Py_NewRef(obj);` and direct new-reference
  API returns such as `return PyUnicode_FromString("x");` are treated as
  returned values.
- Basic `Py_INCREF` handling: incrementing a known borrowed reference turns it
  into an owned reference that must later be returned or released.

The output is still a candidate-bug list, not a final proof. Complex macros,
cross-function ownership contracts, generated code, and project-specific
destructors may still require LLM or human review.

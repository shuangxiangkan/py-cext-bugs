# extract

Generic C/C++ source extraction utilities.

This folder should stay independent of CPython-specific rules. It provides:

- Tree-sitter parsing for C, with optional C++ support.
- Function, call, assignment, return, global declaration, and struct extraction.
- General project source-file discovery helpers.

Use this layer when an analyzer needs structured C/C++ information but should not know about Python C API ownership rules.

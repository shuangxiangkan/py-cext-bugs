# tests

Tests and demo inputs for `py-cext-bugs`.

Useful files:

- `demo_refcount_cases.c`: small C file with intentional refcount bug patterns.
- `test_refcount_analyzer.py`: tests refcount analysis behavior.
- `helpers.py`: test fixtures and temporary source-tree helper.

Run tests:

```bash
PYTHONPATH=py-cext-bugs python -m unittest discover -s py-cext-bugs/tests -p 'test_*.py'
```

Generate demo JSON:

```bash
python py-cext-bugs/cli.py refcount py-cext-bugs/tests/demo_refcount_cases.c
```

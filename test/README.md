# test

Tests and demo inputs for `py-cext-bugs`.

Useful files:

- `demo_refcount_cases.c`: small C file with intentional refcount bug patterns.
- `test_refcount_analyzer.py`: tests refcount analysis behavior.
- `test_c_extension_discovery.py`: tests C extension discovery.
- `helpers.py`: test fixtures and temporary source-tree helper.

Run tests:

```bash
PYTHONPATH=py-cext-bugs python -m unittest discover -s py-cext-bugs/test -p 'test_*.py'
```

Generate demo JSON:

```bash
python py-cext-bugs/main.py refcount py-cext-bugs/test/demo_refcount_cases.c
```

# -*- coding: utf-8 -*-
"""
Puts the extension's `lib` folder on sys.path for the whole test
session.

FORTY-SEVENTH CORRECTION: `test_services.py` did its own
`sys.path.insert` at import time, but `test_balancing_fixes.py` did
not - it just documented `PYTHONPATH=lib` in its docstring and relied
on the caller remembering. Since pytest imports test modules in
alphabetical order, `test_balancing_fixes` is imported FIRST, so a
plain `python -m pytest tests/` aborted the entire run with
`ModuleNotFoundError: No module named 'panelmgr'` during collection -
before the module that fixes the path was ever reached. A conftest is
loaded ahead of every test module, so the path is set once, centrally,
and either invocation works.
"""

import os
import sys

_LIB = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "lib")
)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

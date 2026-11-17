# -*- coding: utf-8 -*-
"""Revizto Clashes - read Revizto / Navisworks clash exports (CSV, XLSX, BCF)
and match them to Revit elements.

Pure-Python modules (no Revit, unit-tested under tests/ReviztoClashes):
    parse    - read CSV / XLSX / BCF files into clash dicts
    match    - pick the Revit element for each clash side (host or link)
    report   - CSV export, status groups, number formatting

Revit-side module (needs the Revit API):
    revit_side - sources (host + loaded links), resolve, bounding boxes

IronPython 2.7 and CPython 3 compatible: no f-strings, no type hints.
"""

# -*- coding: utf-8 -*-
"""SLD Checker - compares single line diagrams drawn with Detail Items
against the electrical model (equipment, circuits, fixtures).

Modules
  config   parameter mapping + tolerances (user override JSON in %APPDATA%)
  rules    pure text/number helpers: normalise names, parse ratings,
           poles, cables, loads, classify MDB / SMDB / DB / TX
  checks   pure comparison logic -> list of findings (no Revit imports)
  collect  Revit reads (read-only) -> plain record dicts
  report   pyRevit output report + CSV export

Only collect.py and report.py import the Revit API, so rules/checks can be
unit-tested with plain CPython (see tests/SLDChecker).
"""

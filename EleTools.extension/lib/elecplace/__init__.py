# -*- coding: utf-8 -*-
"""
elecplace - library for the "Auto-Place Fixtures" pyRevit command.

Modules
  compat    Revit 2023-2026 compatibility + unit helpers
  mathutil  pure-python interval math / spatial index (unit-testable)
  settings  persistent user options (Shift+Click the button)
  linkdata  linked model + room extraction
  geometry  room boundary -> wall segments, face lookup, openings
  planner   rules, validation, duplicate check, dry-run placement plan
  executor  transactions and family instance creation
  ui        pyRevit forms dialogs
  report    output window report + CSV export
"""
__version__ = "1.0.0"

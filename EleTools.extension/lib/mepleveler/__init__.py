# -*- coding: utf-8 -*-
"""MEP Level Fixer.

A pyRevit library that re-associates MEP elements with the project level that
matches their real physical elevation, while guaranteeing that the elements do
not move by a single internal unit.

Architecture
------------
    revit_compat.py  - version shims (ElementId.Value vs IntegerValue, units...)
    log.py           - logging framework (rotating file + pyRevit output)
    config.py        - category catalogue + persisted user settings
    geometry.py      - elevation extraction and geometric fingerprinting
    levels.py        - project level cache + level resolution strategies
    params.py        - level / offset parameter discovery per element type
    engine.py        - collection, analysis, transactional execution
    reporting.py     - CSV / Excel-compatible reports and on-screen summaries
    ui/              - WPF windows (main dialog, preview dialog)

The public entry point used by the pushbutton is :func:`mepleveler.main.run`.
"""

__version__ = '1.0.0'
__author__ = 'BHEEM'

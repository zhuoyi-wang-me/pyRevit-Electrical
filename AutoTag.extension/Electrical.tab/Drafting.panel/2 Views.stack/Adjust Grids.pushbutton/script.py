# -*- coding: utf-8 -*-
"""Adjust Grids - line up the grid ends and bubbles of chosen views, and turn off linked grids.

Press the button: a window lists every view that can show grids (the active view, or the views of
the active sheet / of the sheets selected in the Project Browser, are ticked). Tick the views,
choose the bubble sides and distances, and whether the grids of linked models are turned off,
then "Apply to ticked views". One transaction: one Undo puts everything back.
The window is modeless - it stays open while you work in Revit.
    ga_window.py     the window (modeless)
    ga_geometry.py   where a grid has to end (no Revit)
    ga_revit.py      the views; 2D extents and bubbles; the filter for linked grids
"""
__persistentengine__ = True          # the window outlives this script: keep its engine alive

import sys

# The kept engine also keeps the modules it imported. When no window is open, they are loaded
# again from disk, so that an updated tool is used without reloading pyRevit.
_window_module = sys.modules.get('ga_window')
try:
    _open = any(window.IsVisible for window in getattr(_window_module, '_OPEN', []))
except Exception:
    _open = False
if not _open:
    for _name in ('ga_window', 'ga_revit', 'ga_geometry'):
        sys.modules.pop(_name, None)

import ga_window

ga_window.main()

# -*- coding: utf-8 -*-
"""Auto Tag Layout - tags for an enlarged layout, stacked at the sides without overlapping.

Open the enlarged layout view, press the button, tick the categories, choose the tag
family type for each, press Create tags. The window stays open while you work in
Revit; Refresh reads the active view and the selection again. The model is changed
only by Create tags (one transaction: one Undo removes all tags of a run).
    atl_window.py   the window (modeless)
    atl_layout.py   where each tag goes (no overlap, leaders that do not cross)
    atl_revit.py    reading the view, creating and placing the tags
"""
__persistentengine__ = True          # the window outlives this script: keep its engine alive

import sys

# The kept engine also keeps the modules it imported. When no window is open, they are loaded
# again from disk, so that an updated tool is used without reloading pyRevit.
_MODULES = ('atl_window', 'atl_options', 'atl_revit', 'atl_layout')
_window_module = sys.modules.get('atl_window')
_open = False
try:
    _open = any(window.IsVisible for window in getattr(_window_module, '_OPEN', []))
except Exception:
    _open = False
if not _open:
    for _name in _MODULES:
        sys.modules.pop(_name, None)

import atl_window

atl_window.main()

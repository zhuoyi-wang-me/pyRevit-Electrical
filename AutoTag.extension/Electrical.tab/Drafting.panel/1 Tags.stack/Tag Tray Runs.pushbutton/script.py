# -*- coding: utf-8 -*-
"""Tag Tray Runs - tags along the cable tray (and conduit) runs of a plan, repeated every N metres.

Open the plan (e.g. a containment part plan), press the button, choose the tag type, how often the
tag is repeated along a run and how far it stands from the tray, press "Tag runs". Each tag
stands beside its tray where no other tray, tag or text is. One transaction: one Undo removes them.
    trt_window.py   the window (modeless)
    trt_layout.py   where along a run, and where beside the tray (no Revit)
    trt_revit.py    reading the view, creating and placing the tags
Uses the view helpers of Auto Tag Layout (the button next to this one).
"""
__persistentengine__ = True          # the window outlives this script: keep its engine alive

import sys

# The kept engine also keeps the modules it imported. When no window is open, they are loaded
# again from disk, so that an updated tool is used without reloading pyRevit.
_window_module = sys.modules.get('trt_window')
try:
    _open = any(window.IsVisible for window in getattr(_window_module, '_OPEN', []))
except Exception:
    _open = False
if not _open:
    for _name in ('trt_window', 'trt_options', 'trt_revit', 'trt_layout', 'atl_revit', 'atl_layout'):
        sys.modules.pop(_name, None)

import trt_window

trt_window.main()

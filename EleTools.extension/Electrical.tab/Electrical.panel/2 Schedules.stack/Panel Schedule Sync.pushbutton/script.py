# -*- coding: utf-8 -*-
"""pyRevit entry point. All Revit API work is delegated to ExternalEvent."""
from __future__ import print_function
__persistentengine__ = True
import os
import sys

# The window module and its XAML live inside this bundle, so make the bundle
# folder importable regardless of which extension the button is dropped into.
COMMAND_DIR = os.path.dirname(os.path.abspath(__file__))
if COMMAND_DIR not in sys.path:
    sys.path.insert(0, COMMAND_DIR)

from panel_schedule_sync import PanelScheduleSyncWindow

try:
    _panel_schedule_sync_window
except NameError:
    _panel_schedule_sync_window = None

def _clear_window():
    global _panel_schedule_sync_window
    _panel_schedule_sync_window = None

def show_window():
    global _panel_schedule_sync_window
    if _panel_schedule_sync_window is not None:
        try:
            _panel_schedule_sync_window.Activate()
            return
        except Exception:
            _panel_schedule_sync_window = None
    _panel_schedule_sync_window = PanelScheduleSyncWindow()
    _panel_schedule_sync_window.Closed += lambda sender, args: _clear_window()
    _panel_schedule_sync_window.Show()
    _panel_schedule_sync_window.request_refresh()

show_window()

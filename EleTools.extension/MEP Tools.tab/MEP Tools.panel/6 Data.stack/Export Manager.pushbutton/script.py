# -*- coding: utf-8 -*-
"""Export Manager -- modeless pyRevit tool.

__persistentengine__ keeps this script's IronPython engine alive after the
command returns.  That is REQUIRED for a modeless window: without it the
engine is torn down as soon as the button click finishes and every WPF
event handler / ExternalEvent handler would point at a dead engine
(typical symptom: the window opens, then any click crashes or does nothing).
"""
__title__ = "Export\nManager"
__persistentengine__ = True

import os
import sys

import System
from pyrevit import HOST_APP, forms, revit

WINDOW_KEY = "pyRevit.ExportManager.Window"
WINDOW_DOC_KEY = "pyRevit.ExportManager.Document"

# Generic sibling module names that another pyRevit tool running in a
# persistent engine could also own (e.g. its own "helpers.py").  Purge them
# so this tool always imports ITS copies from this folder, and so edits to
# the .py files are picked up without restarting Revit.
_LOCAL_MODULES = (
    "ui", "helpers", "filename_builder", "format_manager", "selection_sets",
    "sheet_filter", "sheet_filter_ui", "view_search", "revit_event",
    "export_pdf", "export_dwg", "dwg_ole_binder",
)


def _existing_window(doc):
    """Bring an already-open Export Manager to the front instead of
    opening a second copy.  Returns True if one was re-used."""
    domain = System.AppDomain.CurrentDomain
    existing = domain.GetData(WINDOW_KEY)
    if existing is None:
        return False
    try:
        if not existing.IsLoaded:
            raise RuntimeError("stale")
        bound_doc = domain.GetData(WINDOW_DOC_KEY)
        same = False
        try:
            same = bound_doc is not None and bound_doc.IsValidObject and bound_doc.Equals(doc)
        except Exception:
            same = False
        if same:
            if existing.WindowState == System.Windows.WindowState.Minimized:
                existing.WindowState = System.Windows.WindowState.Normal
            existing.Activate()
            return True
        # Open for another model: close it and start fresh for this one.
        existing.Close()
    except Exception:
        pass
    domain.SetData(WINDOW_KEY, None)
    domain.SetData(WINDOW_DOC_KEY, None)
    return False


def main():
    doc = revit.doc
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Open a project model first.", title="Export Manager", exitscript=True)

    if _existing_window(doc):
        return

    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    for name in _LOCAL_MODULES:
        sys.modules.pop(name, None)

    import ui
    ui.run(doc, HOST_APP.uiapp)


main()

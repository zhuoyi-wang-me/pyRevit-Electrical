# -*- coding: utf-8 -*-
"""Breaker Check - is the breaker rating of each circuit enough for its load, and update it where it is not.

    current  = load in W / (voltage x power factor)      (3-pole: / (1.73 x voltage x power factor))
    required = current x 1.25, then the next breaker rating of your list
    UNDERSIZED when the breaker is below "required", OVERSIZED when it is larger than that next rating

The model is changed only by "Update ticked breakers..." in the window, after a question.
    bc_window.py   the window          bc_check.py   the comparison
    bc_revit.py    reading circuits and writing the new ratings
"""
from System.Collections.Generic import List

from pyrevit import forms, revit, script
from Autodesk.Revit import DB

import bc_panels
import bc_revit
import bc_window

doc = revit.doc
uidoc = revit.uidoc

# ---- which panels: the ones selected in Revit, otherwise a list to tick ----
panels = bc_revit.all_panels(doc)
if not panels:
    forms.alert('No panel with circuits was found in this model.', exitscript=True)
selected = set(eid for eid in uidoc.Selection.GetElementIds())
chosen = [element for element in panels.values() if element.Id in selected]
if not chosen:
    names = bc_panels.choose(panels.keys())              # the window with search and tick boxes
    if not names:
        script.exit()
    chosen = [panels[name] for name in names]

# ---- the window; it returns the circuits to select in Revit, if the user asked for that ----
to_select = bc_window.main(chosen)
if to_select:
    uidoc.Selection.SetElementIds(List[DB.ElementId](to_select))

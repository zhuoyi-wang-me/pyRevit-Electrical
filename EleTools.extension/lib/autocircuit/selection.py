# -*- coding: utf-8 -*-
"""
Filled-region selection for the "selected region(s) only" scope.

Two ways to choose the area:
    * pre-selection - select filled regions in a plan view, then start the tool
    * picking       - "Pick Filled Region(s)" in the window; the window closes,
                      the user picks in the active view (Finish / Esc), and the
                      window reopens with its state preserved (see script.py)
Regions are stored by UniqueId, which is stable across sessions and syncs.
"""
import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
from Autodesk.Revit.DB import FilledRegion
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException

PLAN_VIEW_TYPES = ('FloorPlan', 'CeilingPlan', 'EngineeringPlan', 'AreaPlan')


class FilledRegionFilter(ISelectionFilter):
    """Lets the user click only filled regions."""
    __namespace__ = 'ElecAutoCircuit'   # needed by pythonnet, ignored by IronPython

    def AllowElement(self, element):
        return isinstance(element, FilledRegion)

    def AllowReference(self, reference, position):
        return False


def active_view_is_plan(doc):
    view = doc.ActiveView
    try:
        return str(view.ViewType) in PLAN_VIEW_TYPES
    except Exception:
        return False


def preselected_filled_regions(doc, uidoc):
    """UniqueIds of filled regions in the current Revit selection (may be empty)."""
    ids = []
    for eid in uidoc.Selection.GetElementIds():
        el = doc.GetElement(eid)
        if isinstance(el, FilledRegion):
            ids.append(el.UniqueId)
    return ids


def pick_filled_regions(doc, uidoc):
    """
    Interactive multi-pick in the active view. Returns a list of UniqueIds, or None
    if the user pressed Esc (caller keeps the previous selection).
    """
    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element, FilledRegionFilter(),
            'Select the filled region(s) that define the circuiting area, then click Finish')
    except OperationCanceledException:
        return None
    ids = []
    for ref in refs:
        el = doc.GetElement(ref.ElementId)
        if isinstance(el, FilledRegion) and el.UniqueId not in ids:
            ids.append(el.UniqueId)
    return ids

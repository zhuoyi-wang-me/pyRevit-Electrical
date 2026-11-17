# -*- coding: utf-8 -*-
"""Worksets Mode - the "Worksets" tab of V/G for the HOST model:
per-view workset visibility (Shown / Hidden / Using Global Setting).

API: View.GetWorksetVisibility / SetWorksetVisibility (WorksetVisibility
enum), FilteredWorksetCollector, WorksetDefaultVisibilitySettings for the
global default. Template-controlled by "V/G Overrides Worksets".

Worksets *inside a linked model* (Custom link display > Worksets tab) are
not a separate public API; they travel inside the link's display
settings object, which Revit Links mode copies as a whole.
"""
from Autodesk.Revit.DB import (FilteredWorksetCollector, WorksetKind, WorksetVisibility,
                                WorksetDefaultVisibilitySettings)

NAMES = {"Visible": "Shown", "Hidden": "Hidden", "UseGlobalSetting": "Using Global Setting"}
_FROM_NAME = {"shown": "Visible", "visible": "Visible", "show": "Visible",
              "hidden": "Hidden", "hide": "Hidden",
              "using global setting": "UseGlobalSetting", "useglobalsetting": "UseGlobalSetting",
              "global": "UseGlobalSetting", "": "UseGlobalSetting"}


def available(doc):
    try:
        return bool(doc.IsWorkshared)
    except Exception:
        return False


def get_worksets(doc):
    """(WorksetId, name) for user worksets."""
    if not available(doc):
        return []
    return [(w.Id, w.Name) for w in
            FilteredWorksetCollector(doc).OfKind(WorksetKind.UserWorkset).ToWorksets()]


def _vis_name(v):
    for n in ("Visible", "Hidden", "UseGlobalSetting"):
        if v == getattr(WorksetVisibility, n):
            return n
    return "UseGlobalSetting"


def get_state(view, wid):
    return _vis_name(view.GetWorksetVisibility(wid))


def status_label(view, wid):
    st = get_state(view, wid)
    if st != "UseGlobalSetting":
        return NAMES[st]
    try:
        g = WorksetDefaultVisibilitySettings.GetWorksetDefaultVisibilitySettings(view.Document)
        return "Using Global Setting ({})".format("Visible" if g.IsWorksetVisible(wid) else "Not Visible")
    except Exception:
        return NAMES[st]


def _set(view, wid, name):
    before = get_state(view, wid)
    if before == name:
        return "same"
    view.SetWorksetVisibility(wid, getattr(WorksetVisibility, name))
    after = get_state(view, wid)
    if after != name:
        raise RuntimeError(u"set '{}' but view reads '{}'".format(NAMES[name], NAMES[after]))
    return "changed"


def copy_workset(source_view, target_view, wid):
    return _set(target_view, wid, get_state(source_view, wid))


def reset_workset(target_view, wid):
    return _set(target_view, wid, "UseGlobalSetting")


def apply_row(target_view, wid, text):
    key = (u"%s" % (text or "")).strip().lower()
    key = key.split("(")[0].strip()   # "Using Global Setting (Visible)" -> base
    if key not in _FROM_NAME:
        raise RuntimeError(u"unknown workset visibility '{}'".format(text))
    return _set(target_view, wid, _FROM_NAME[key])

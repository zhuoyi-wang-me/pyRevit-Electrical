# -*- coding: utf-8 -*-
"""View discovery, classification and filtering helpers.

Kept independent of any UI framework so it can be unit-tested and reused
from other tools.
"""
from Autodesk.Revit.DB import FilteredElementCollector, View, ViewType

from vismanager.compat import eid_val, is_invalid

# The 5 view families the UI exposes as type-filter chips (with live
# counts). Anything outside these families (drafting views, legends,
# schedules, sheets, etc.) is left out entirely.
VIEW_TYPE_GROUPS = {
    "3D Views": (ViewType.ThreeD,),
    "Floor Plans": (ViewType.FloorPlan, ViewType.AreaPlan, ViewType.EngineeringPlan),
    "Sections": (ViewType.Section, ViewType.Detail),
    "Ceiling Plans": (ViewType.CeilingPlan,),
    "Elevations": (ViewType.Elevation,),
}

GROUP_ORDER = ["3D Views", "Floor Plans", "Sections", "Ceiling Plans", "Elevations"]


def group_for_view_type(view_type):
    for group, types in VIEW_TYPE_GROUPS.items():
        if view_type in types:
            return group
    return None


def get_all_views_and_templates(doc, exclude_view_id=None):
    """Return (views, templates). Each item is a dict:
    {id, name, obj, view_type, group, template_name}.

    FIX: uses eid_val() instead of .IntegerValue (removed in Revit 2026),
    skips views that don't allow graphic overrides, and records the name
    of the template applied to each view so the UI can show it.
    """
    collector = FilteredElementCollector(doc).OfClass(View)
    views, templates = [], []
    exclude_val = eid_val(exclude_view_id) if exclude_view_id is not None else None

    for v in collector:
        try:
            group = group_for_view_type(v.ViewType)
        except Exception:
            group = None
        if group is None:
            continue
        try:
            if not v.AreGraphicsOverridesAllowed():
                continue
        except Exception:
            pass

        template_name = ""
        if not v.IsTemplate:
            try:
                tid = v.ViewTemplateId
                if not is_invalid(tid):
                    t = doc.GetElement(tid)
                    template_name = t.Name if t is not None else ""
            except Exception:
                pass

        entry = {
            "id": eid_val(v.Id),
            "name": v.Name,
            "obj": v,
            "view_type": v.ViewType,
            "group": group,
            "template_name": template_name,
        }

        if v.IsTemplate:
            templates.append(entry)
            continue
        if exclude_val is not None and entry["id"] == exclude_val:
            continue
        views.append(entry)

    return views, templates


def group_counts(entries):
    counts = dict((g, 0) for g in GROUP_ORDER)
    for e in entries:
        counts[e["group"]] = counts.get(e["group"], 0) + 1
    return counts

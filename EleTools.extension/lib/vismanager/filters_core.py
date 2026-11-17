# -*- coding: utf-8 -*-
"""Filters Mode core operations + OverrideGraphicSettings (de)serialization.

A Revit filter (ParameterFilterElement) is a document-level element. What
varies per view is: (a) whether it's applied, (b) enabled state, (c)
visibility, and (d) its OverrideGraphicSettings. Copying a filter means:
make sure it's applied on the target, then copy (b)-(d).

FIXES in this version
- Membership checks compare integer ids (no reliance on ElementId.__eq__
  through IronPython's `in`, and no .IntegerValue - removed in Revit 2026).
- Solid line pattern (id -3000) was treated as "no override": exported as
  "Default", ignored by has_override(). Now handled explicitly.
- Import resolved patterns by raw element id only, so a file from another
  project could apply a random wrong pattern or throw. Patterns are now
  resolved by NAME first (id only as a same-name tiebreaker).
- Fill-pattern visibility toggles and Detail Level overrides were never
  exported/imported/detected. Added (older files without these columns
  still import fine).
- Numeric cells coming back from XLSX as floats ("5.0") failed int().
"""
from Autodesk.Revit.DB import (ParameterFilterElement, FilteredElementCollector,
                                OverrideGraphicSettings, ElementId, Color,
                                FillPatternElement, LinePatternElement,
                                ViewDetailLevel)

from vismanager.compat import eid_val, make_eid, is_invalid

SOLID_LINE_ID = -3000  # LinePatternElement.GetSolidPatternId()


# ---------------------------------------------------------------- filters
def get_view_filters(view):
    """(ElementId, name) for filters applied to `view` (pass the
    *effective* view - i.e. its controlling template - from the caller).
    """
    out = []
    doc = view.Document
    for fid in view.GetFilters():
        felem = doc.GetElement(fid)
        if isinstance(felem, ParameterFilterElement):
            out.append((fid, felem.Name))
    return out


def get_all_project_filters(doc):
    return [(f.Id, f.Name) for f in
            FilteredElementCollector(doc).OfClass(ParameterFilterElement)]


def _has_filter(view, filter_id):
    target = eid_val(filter_id)
    return any(eid_val(f) == target for f in view.GetFilters())


def copy_filter_to_view(source_view, target_view, filter_id):
    """Apply filter to target (if missing), copy overrides + enabled +
    visibility. Must run inside a Transaction. Raises on hard failure.
    """
    if not _has_filter(target_view, filter_id):
        target_view.AddFilter(filter_id)  # raises if the filter isn't valid for this view

    target_view.SetFilterOverrides(filter_id, source_view.GetFilterOverrides(filter_id))
    target_view.SetFilterVisibility(filter_id, source_view.GetFilterVisibility(filter_id))
    try:  # Revit 2021+
        target_view.SetIsFilterEnabled(filter_id, source_view.GetIsFilterEnabled(filter_id))
    except AttributeError:
        pass
    return True


def remove_filter_from_view(target_view, filter_id):
    """Remove a filter. Returns False if it wasn't applied."""
    if _has_filter(target_view, filter_id):
        target_view.RemoveFilter(filter_id)
        return True
    return False


def apply_filter_state(target_view, filter_id, ogs, enabled=None, visible=None):
    """Used by import: apply filter + overrides + optional flags."""
    if not _has_filter(target_view, filter_id):
        target_view.AddFilter(filter_id)
    target_view.SetFilterOverrides(filter_id, ogs)
    if visible is not None:
        target_view.SetFilterVisibility(filter_id, visible)
    if enabled is not None:
        try:
            target_view.SetIsFilterEnabled(filter_id, enabled)
        except AttributeError:
            pass


# ------------------------------------------------------- value helpers
def _to_int(s, default=None):
    try:
        return int(float(str(s).strip()))
    except Exception:
        return default


def _to_bool(s, default=None):
    v = str(s if s is not None else "").strip().lower()
    if v in ("yes", "true", "1", "y", "enabled", "visible"):
        return True
    if v in ("no", "false", "0", "n", "disabled", "hidden"):
        return False
    return default


def _fmt_color(c):
    if c is None or not c.IsValid:
        return "Default"
    return "RGB({},{},{})".format(c.Red, c.Green, c.Blue)


def _parse_color(s):
    s = (u"%s" % (s if s is not None else "")).strip()
    if not s or s == "Default":
        return None
    if s.upper().startswith("RGB(") and s.endswith(")"):
        s = s[4:-1]
    parts = [p.strip() for p in s.split(",")]
    if len(parts) == 3:
        try:
            r, g, b = [max(0, min(255, int(float(p)))) for p in parts]
            return Color(r, g, b)
        except Exception:
            return None
    return None


def _fmt_pattern(doc, pattern_id):
    if is_invalid(pattern_id):
        return "Default"
    pid = eid_val(pattern_id)
    if pid == SOLID_LINE_ID:
        return "Solid|{}".format(SOLID_LINE_ID)
    elem = doc.GetElement(pattern_id)
    if elem is None:
        return "Default"
    try:
        name = elem.Name
    except Exception:
        name = "Pattern"
    return u"{}|{}".format(name, pid)


def _parse_pattern(doc, s, kind):
    """kind: 'line' or 'fill'. Resolve by name first, id second."""
    s = (u"%s" % (s if s is not None else "")).strip()
    if not s or s == "Default":
        return ElementId.InvalidElementId

    name, pid = s, None
    if "|" in s:
        name, id_str = s.rsplit("|", 1)
        pid = _to_int(id_str)
    elif _to_int(s) is not None:
        name, pid = "", _to_int(s)
    name = name.strip()

    if kind == "line" and (name.lower() == "solid" or pid == SOLID_LINE_ID):
        return LinePatternElement.GetSolidPatternId()

    cls = LinePatternElement if kind == "line" else FillPatternElement
    if pid is not None:
        elem = doc.GetElement(make_eid(pid))
        if isinstance(elem, cls) and (not name or elem.Name == name):
            return elem.Id
    if name:
        for elem in FilteredElementCollector(doc).OfClass(cls):
            if elem.Name == name:
                return elem.Id
    return ElementId.InvalidElementId


def _fmt_weight(w):
    return str(w) if w is not None else "-1"


def _fmt_bool(b):
    return "Yes" if b else "No"


_DETAIL_NAMES = {
    ViewDetailLevel.Coarse: "Coarse",
    ViewDetailLevel.Medium: "Medium",
    ViewDetailLevel.Fine: "Fine",
}


# ------------------------------------------------------ serialization
def override_to_dict(doc, ogs):
    d = {
        "Projection_Line_Color": _fmt_color(ogs.ProjectionLineColor),
        "Projection_Line_Pattern": _fmt_pattern(doc, ogs.ProjectionLinePatternId),
        "Projection_Line_Weight": _fmt_weight(ogs.ProjectionLineWeight),
        "Surface_Fill_Foreground_Color": _fmt_color(ogs.SurfaceForegroundPatternColor),
        "Surface_Fill_Foreground_Pattern": _fmt_pattern(doc, ogs.SurfaceForegroundPatternId),
        "Surface_Fill_Background_Color": _fmt_color(ogs.SurfaceBackgroundPatternColor),
        "Surface_Fill_Background_Pattern": _fmt_pattern(doc, ogs.SurfaceBackgroundPatternId),
        "Cut_Line_Color": _fmt_color(ogs.CutLineColor),
        "Cut_Line_Pattern": _fmt_pattern(doc, ogs.CutLinePatternId),
        "Cut_Line_Weight": _fmt_weight(ogs.CutLineWeight),
        "Cut_Fill_Foreground_Color": _fmt_color(ogs.CutForegroundPatternColor),
        "Cut_Fill_Foreground_Pattern": _fmt_pattern(doc, ogs.CutForegroundPatternId),
        "Cut_Fill_Background_Color": _fmt_color(ogs.CutBackgroundPatternColor),
        "Cut_Fill_Background_Pattern": _fmt_pattern(doc, ogs.CutBackgroundPatternId),
        "Transparency": str(ogs.Transparency),
        "Halftone": _fmt_bool(ogs.Halftone),
        "Surface_Fill_Foreground_Visible": _fmt_bool(ogs.IsSurfaceForegroundPatternVisible),
        "Surface_Fill_Background_Visible": _fmt_bool(ogs.IsSurfaceBackgroundPatternVisible),
        "Cut_Fill_Foreground_Visible": _fmt_bool(ogs.IsCutForegroundPatternVisible),
        "Cut_Fill_Background_Visible": _fmt_bool(ogs.IsCutBackgroundPatternVisible),
        "Detail_Level": _DETAIL_NAMES.get(ogs.DetailLevel, "Default"),
    }
    return d


OVERRIDE_FIELDS = [
    "Projection_Line_Color", "Projection_Line_Pattern", "Projection_Line_Weight",
    "Surface_Fill_Foreground_Color", "Surface_Fill_Foreground_Pattern",
    "Surface_Fill_Background_Color", "Surface_Fill_Background_Pattern",
    "Cut_Line_Color", "Cut_Line_Pattern", "Cut_Line_Weight",
    "Cut_Fill_Foreground_Color", "Cut_Fill_Foreground_Pattern",
    "Cut_Fill_Background_Color", "Cut_Fill_Background_Pattern",
    "Transparency", "Halftone",
    "Surface_Fill_Foreground_Visible", "Surface_Fill_Background_Visible",
    "Cut_Fill_Foreground_Visible", "Cut_Fill_Background_Visible",
    "Detail_Level",
]


def has_override(ogs):
    """True if any graphic property differs from the blank default."""
    for c in (ogs.ProjectionLineColor, ogs.CutLineColor,
              ogs.SurfaceForegroundPatternColor, ogs.SurfaceBackgroundPatternColor,
              ogs.CutForegroundPatternColor, ogs.CutBackgroundPatternColor):
        if c is not None and c.IsValid:
            return True
    if ogs.ProjectionLineWeight not in (-1, None) or ogs.CutLineWeight not in (-1, None):
        return True
    for pid in (ogs.ProjectionLinePatternId, ogs.CutLinePatternId,
                ogs.SurfaceForegroundPatternId, ogs.SurfaceBackgroundPatternId,
                ogs.CutForegroundPatternId, ogs.CutBackgroundPatternId):
        if not is_invalid(pid):   # includes Solid line (-3000)
            return True
    if ogs.Transparency or ogs.Halftone:
        return True
    if not (ogs.IsSurfaceForegroundPatternVisible and ogs.IsSurfaceBackgroundPatternVisible
            and ogs.IsCutForegroundPatternVisible and ogs.IsCutBackgroundPatternVisible):
        return True
    try:
        if ogs.DetailLevel != ViewDetailLevel.Undefined:
            return True
    except Exception:
        pass
    return False


def dict_to_override(doc, d):
    """Rebuild an OverrideGraphicSettings from an exported row."""
    ogs = OverrideGraphicSettings()

    def color(key, setter):
        c = _parse_color(d.get(key))
        if c is not None:
            setter(c)

    def pattern(key, kind, setter):
        pid = _parse_pattern(doc, d.get(key), kind)
        if not is_invalid(pid):
            setter(pid)

    def weight(key, setter):
        w = _to_int(d.get(key))
        if w is not None and 1 <= w <= 16:
            setter(w)

    def visible(key, setter):
        b = _to_bool(d.get(key))
        if b is False:
            setter(False)

    color("Projection_Line_Color", ogs.SetProjectionLineColor)
    pattern("Projection_Line_Pattern", "line", ogs.SetProjectionLinePatternId)
    weight("Projection_Line_Weight", ogs.SetProjectionLineWeight)

    color("Cut_Line_Color", ogs.SetCutLineColor)
    pattern("Cut_Line_Pattern", "line", ogs.SetCutLinePatternId)
    weight("Cut_Line_Weight", ogs.SetCutLineWeight)

    pattern("Surface_Fill_Foreground_Pattern", "fill", ogs.SetSurfaceForegroundPatternId)
    color("Surface_Fill_Foreground_Color", ogs.SetSurfaceForegroundPatternColor)
    pattern("Surface_Fill_Background_Pattern", "fill", ogs.SetSurfaceBackgroundPatternId)
    color("Surface_Fill_Background_Color", ogs.SetSurfaceBackgroundPatternColor)
    pattern("Cut_Fill_Foreground_Pattern", "fill", ogs.SetCutForegroundPatternId)
    color("Cut_Fill_Foreground_Color", ogs.SetCutForegroundPatternColor)
    pattern("Cut_Fill_Background_Pattern", "fill", ogs.SetCutBackgroundPatternId)
    color("Cut_Fill_Background_Color", ogs.SetCutBackgroundPatternColor)

    visible("Surface_Fill_Foreground_Visible", ogs.SetSurfaceForegroundPatternVisible)
    visible("Surface_Fill_Background_Visible", ogs.SetSurfaceBackgroundPatternVisible)
    visible("Cut_Fill_Foreground_Visible", ogs.SetCutForegroundPatternVisible)
    visible("Cut_Fill_Background_Visible", ogs.SetCutBackgroundPatternVisible)

    t = _to_int(d.get("Transparency"))
    if t is not None and 0 < t <= 100:
        ogs.SetSurfaceTransparency(t)
    if _to_bool(d.get("Halftone")):
        ogs.SetHalftone(True)

    lvl = str(d.get("Detail_Level") or "").strip().lower()
    lvl_map = {"coarse": ViewDetailLevel.Coarse, "medium": ViewDetailLevel.Medium,
               "fine": ViewDetailLevel.Fine}
    if lvl in lvl_map:
        ogs.SetDetailLevel(lvl_map[lvl])
    return ogs


# ------------------------------------------------ verification (v1.2)
def filter_snapshot(view, filter_id):
    """Comparable state of one filter in a view, or None if not applied.
    Used to verify a copy really landed and to tell 'changed' from
    'already identical'.
    """
    if not _has_filter(view, filter_id):
        return None
    try:
        enabled = bool(view.GetIsFilterEnabled(filter_id))
    except AttributeError:
        enabled = True
    ov = override_to_dict(view.Document, view.GetFilterOverrides(filter_id))
    return (bool(view.GetFilterVisibility(filter_id)), enabled, tuple(sorted(ov.items())))


def filter_position(view, filter_id):
    """(index, total) in the view's filter priority order (top = 0 wins),
    or None if the API/filter isn't available."""
    try:
        ordered = list(view.GetOrderedFilters())   # Revit 2021+
    except AttributeError:
        ordered = list(view.GetFilters())
    target = eid_val(filter_id)
    for i, f in enumerate(ordered):
        if eid_val(f) == target:
            return i, len(ordered)
    return None


def describe_diff(a, b):
    """Short human description of what differs between two snapshots."""
    if a is None or b is None:
        return "applied" if b is not None else "not applied"
    parts = []
    if a[0] != b[0]:
        parts.append("visibility")
    if a[1] != b[1]:
        parts.append("enabled")
    da, db = dict(a[2]), dict(b[2])
    parts += [k for k in sorted(da) if da.get(k) != db.get(k)]
    return ", ".join(parts)

# -*- coding: utf-8 -*-
"""V/G Overrides Mode - host model ("own model") category overrides and
visibility, as set in the Model / Annotation / Analytical / Imported tabs
of the Visibility/Graphics dialog.

FIXES in this version
- Subcategories (Doors > Swing, Walls > Common Edges, user-created
  subcategories, ...) were never listed, so they could not be copied,
  reset, exported or imported. They are now included as "Parent : Sub".
- copy/reset only touched the hidden flag if SetCategoryOverrides had
  succeeded first. Categories that allow visibility control but not
  graphic overrides therefore never got their visibility copied. The two
  are now independent (View.IsCategoryOverridable /
  Category.AllowsVisibilityControl).
- Failures now raise with a reason instead of returning a bare False.
- Every category knows which view-template "Include" row governs it
  (template_key) so the caller can detect template-controlled views.
"""
from Autodesk.Revit.DB import CategoryType, OverrideGraphicSettings

from vismanager.compat import eid_val
from vismanager.filters_core import has_override

CATEGORY_TYPE_LABELS = {
    CategoryType.Model: "Model",
    CategoryType.Annotation: "Annotation",
    CategoryType.AnalyticalModel: "Analytical",
}

SEP = " : "


def _allows_vis(cat, view):
    try:
        return bool(cat.get_AllowsVisibilityControl(view))
    except Exception:
        return False


def _overridable(view, cat):
    try:
        return bool(view.IsCategoryOverridable(cat.Id))
    except Exception:
        return False


def get_categories(doc, view):
    """List of dicts {cat, name, group, template_key} for every category
    and subcategory that is a valid V/G target in `view`.
    """
    out = []
    for cat in doc.Settings.Categories:
        try:
            label = CATEGORY_TYPE_LABELS.get(cat.CategoryType)
        except Exception:
            label = None
        if label is None:
            continue
        # Non-built-in top-level categories are DWG/DXF import categories.
        key = "Imported" if (label == "Model" and eid_val(cat.Id) > 0) else label
        if not (_allows_vis(cat, view) or _overridable(view, cat)):
            continue
        out.append({"cat": cat, "name": cat.Name, "group": key, "template_key": key})

        try:
            subs = list(cat.SubCategories)
        except Exception:
            subs = []
        for sub in subs:
            if not (_allows_vis(sub, view) or _overridable(view, sub)):
                continue
            out.append({"cat": sub, "name": cat.Name + SEP + sub.Name,
                        "group": key, "template_key": key})
    return out


def category_status_label(view, category):
    tags = []
    try:
        if _allows_vis(category, view) and view.GetCategoryHidden(category.Id):
            tags.append("Hidden")
    except Exception:
        pass
    try:
        if has_override(view.GetCategoryOverrides(category.Id)):
            tags.append("Override")
    except Exception:
        pass
    return ", ".join(tags)


def get_category_state(view, category):
    """(OverrideGraphicSettings, is_hidden)."""
    try:
        ogs = view.GetCategoryOverrides(category.Id)
    except Exception:
        ogs = OverrideGraphicSettings()
    try:
        hidden = bool(view.GetCategoryHidden(category.Id))
    except Exception:
        hidden = False
    return ogs, hidden


def copy_category_override(source_view, target_view, category, copy_visibility=True):
    """Copy graphics + hidden state for one (sub)category. Must run inside
    a Transaction. Raises RuntimeError if nothing could be applied.
    """
    did = False
    if _overridable(target_view, category):
        target_view.SetCategoryOverrides(category.Id, source_view.GetCategoryOverrides(category.Id))
        did = True
    if copy_visibility and _allows_vis(category, target_view):
        target_view.SetCategoryHidden(category.Id, bool(source_view.GetCategoryHidden(category.Id)))
        did = True
    if not did:
        raise RuntimeError("category can't be overridden or hidden in this view type")
    return True


def apply_category_state(target_view, category, ogs, hidden):
    """Used by import. hidden=None leaves visibility untouched."""
    did = False
    if _overridable(target_view, category):
        target_view.SetCategoryOverrides(category.Id, ogs)
        did = True
    if hidden is not None and _allows_vis(category, target_view):
        target_view.SetCategoryHidden(category.Id, bool(hidden))
        did = True
    if not did:
        raise RuntimeError("category can't be overridden or hidden in this view type")
    return True


def reset_category_override(target_view, category, unhide=True):
    """Blank overrides and (optionally) unhide. Must run inside a Transaction."""
    return apply_category_state(target_view, category, OverrideGraphicSettings(),
                                False if unhide else None)


# ------------------------------------------------ verification (v1.2)
def category_snapshot(view, category, applicability_view=None):
    """Comparable (overrides, hidden) state. Fields that aren't applicable
    in `applicability_view` (default: `view`) are recorded as None so a
    source/target comparison only checks what the target can hold.
    """
    from vismanager.filters_core import override_to_dict
    ref = applicability_view or view
    ogs, hidden = get_category_state(view, category)
    ov = tuple(sorted(override_to_dict(view.Document, ogs).items())) \
        if _overridable(ref, category) else None
    hid = hidden if _allows_vis(category, ref) else None
    return (ov, hid)

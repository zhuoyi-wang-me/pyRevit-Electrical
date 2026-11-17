# -*- coding: utf-8 -*-
"""Revit Links Mode - the "Revit Links" tab of V/G: per-view, per-link
instance display settings (By Host View / By Linked View / Custom).

API: View.GetLinkOverrides / SetLinkOverrides / RemoveLinkOverrides with
Autodesk.Revit.DB.RevitLinkGraphicsSettings (LinkVisibilityType,
LinkedViewId) and the LinkVisibility enum.

FIXES in this version
- ROOT CAUSE of "By Linked View doesn't copy": the old code built a NEW
  RevitLinkGraphicsSettings and only copied LinkedViewId for Custom.
  "By Linked View" *requires* a linked view, so Revit either rejected it
  or it silently landed wrong. Now the source settings object is passed
  through as-is, which also preserves every Custom sub-setting.
- A linked view is only valid for a host view of the same view type
  (plan -> plan, section -> section ...). Copying a plan's linked view to
  a 3D/section target failed with a cryptic ArgumentException. Now we
  remap to a same-named view of the right type inside the link, or fail
  with a readable reason.
- Status/export only resolved the linked view name for Custom, so export
  -> import of "By Linked View" lost the view. Both types now carry it.
- Availability is detected from the API itself (hasattr on View),
  not assumed from a version number.
- Unloaded links are reported instead of throwing on GetLinkDocument.
"""
from Autodesk.Revit.DB import (FilteredElementCollector, RevitLinkInstance, ElementId,
                                View as DBView)

from vismanager.compat import is_invalid

try:
    from Autodesk.Revit.DB import RevitLinkGraphicsSettings, LinkVisibility
    LINKS_API_AVAILABLE = hasattr(DBView, "SetLinkOverrides")
except ImportError:
    RevitLinkGraphicsSettings = None
    LinkVisibility = None
    LINKS_API_AVAILABLE = False

_UNAVAILABLE_MSG = ("The Revit Links display-settings API (View.SetLinkOverrides / "
                    "RevitLinkGraphicsSettings) is not available in this Revit version.")

TYPE_NAMES = ("ByHostView", "ByLinkView", "Custom")


def _vtype_name(vtype):
    if LinkVisibility is None:
        return "ByHostView"
    if vtype == LinkVisibility.ByLinkView:
        return "ByLinkView"
    if vtype == LinkVisibility.Custom:
        return "Custom"
    return "ByHostView"


def get_project_links(doc):
    """(ElementId, name) for every top-level Revit link instance."""
    out = []
    for link in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
        try:
            name = link.Name
        except Exception:
            name = "Link {}".format(link.Id)
        out.append((link.Id, name))
    return out


def _link_doc(host_doc, link_id):
    link = host_doc.GetElement(link_id)
    try:
        return link.GetLinkDocument() if link is not None else None
    except Exception:
        return None


def _linked_view(host_doc, link_id, linked_view_id):
    if is_invalid(linked_view_id):
        return None
    ldoc = _link_doc(host_doc, link_id)
    return ldoc.GetElement(linked_view_id) if ldoc is not None else None


def _linked_view_name(host_doc, link_id, linked_view_id):
    lv = _linked_view(host_doc, link_id, linked_view_id)
    return lv.Name if lv is not None else ""


def _get_settings(view, link_id):
    try:
        return view.GetLinkOverrides(link_id)   # None == By Host View (no override)
    except Exception:
        return None


def link_status_label(view, link_id):
    """'By Host View', 'By Linked View (name)', 'Custom (name)'."""
    if not LINKS_API_AVAILABLE:
        return "N/A"
    s = _get_settings(view, link_id)
    if s is None:
        return "By Host View"
    name = _vtype_name(s.LinkVisibilityType)
    if name == "ByHostView":
        return "By Host View"
    lv = _linked_view_name(view.Document, link_id, s.LinkedViewId)
    label = "By Linked View" if name == "ByLinkView" else "Custom"
    return "{} ({})".format(label, lv) if lv else label


def _find_compatible_linked_view(host_doc, link_id, target_view, wanted_name):
    """A view in the link with the target's ViewType, preferring `wanted_name`."""
    ldoc = _link_doc(host_doc, link_id)
    if ldoc is None:
        return None
    candidates = [v for v in FilteredElementCollector(ldoc).OfClass(DBView)
                  if not v.IsTemplate and v.ViewType == target_view.ViewType]
    for v in candidates:
        if wanted_name and v.Name == wanted_name:
            return v
    return None


def _check_link_loaded(host_doc, link_id):
    if _link_doc(host_doc, link_id) is None:
        raise RuntimeError("link is not loaded - reload it to read/set its linked views")


def _apply(target_view, link_id, settings, wanted_view_name=""):
    """Validate/remap the linked view for this target, then SetLinkOverrides."""
    doc = target_view.Document
    vname = _vtype_name(settings.LinkVisibilityType)

    if vname in ("ByLinkView", "Custom"):
        lv = _linked_view(doc, link_id, settings.LinkedViewId)
        if lv is None and wanted_view_name:
            _check_link_loaded(doc, link_id)
            lv = _find_compatible_linked_view(doc, link_id, target_view, wanted_view_name)
            if lv is None:
                raise RuntimeError(u"linked view '{}' ({}) not found in the link".format(
                    wanted_view_name, target_view.ViewType))
            settings.LinkedViewId = lv.Id
        elif lv is not None and lv.ViewType != target_view.ViewType:
            alt = _find_compatible_linked_view(doc, link_id, target_view, lv.Name)
            if alt is None:
                raise RuntimeError(
                    u"linked view '{}' is a {} view; target is {}. Revit only accepts a "
                    u"linked view of the same type - and no same-named {} view exists in "
                    u"the link.".format(lv.Name, lv.ViewType, target_view.ViewType,
                                        target_view.ViewType))
            settings.LinkedViewId = alt.Id
        elif lv is None and vname == "ByLinkView":
            raise RuntimeError("'By Linked View' needs a linked view but none is set/resolvable")

    target_view.SetLinkOverrides(link_id, settings)
    return True


def copy_link_override(source_view, target_view, link_id):
    """Copy display settings for one link instance. Inside a Transaction."""
    if not LINKS_API_AVAILABLE:
        raise RuntimeError(_UNAVAILABLE_MSG)
    src = _get_settings(source_view, link_id)
    if src is None or _vtype_name(src.LinkVisibilityType) == "ByHostView":
        if _get_settings(target_view, link_id) is not None:
            target_view.RemoveLinkOverrides(link_id)
        return True
    # GetLinkOverrides returns a detached copy - safe to pass/modify.
    return _apply(target_view, link_id, src)


def reset_link_override(target_view, link_id):
    """Back to By Host View."""
    if not LINKS_API_AVAILABLE:
        raise RuntimeError(_UNAVAILABLE_MSG)
    if _get_settings(target_view, link_id) is not None:
        target_view.RemoveLinkOverrides(link_id)
    return True


def override_to_dict(view, link_id):
    if not LINKS_API_AVAILABLE:
        return {"VisibilityType": "ByHostView", "LinkedViewName": ""}
    s = _get_settings(view, link_id)
    if s is None:
        return {"VisibilityType": "ByHostView", "LinkedViewName": ""}
    name = _vtype_name(s.LinkVisibilityType)
    lv = "" if name == "ByHostView" else _linked_view_name(view.Document, link_id, s.LinkedViewId)
    return {"VisibilityType": name, "LinkedViewName": lv}


def dict_to_override_and_apply(target_view, link_id, d):
    """Import: rebuild settings from a row and apply. Inside a Transaction.
    Note: Custom sub-settings (phase, filters, ...) are not in the file, so
    an imported Custom row restores the type + linked view only.
    """
    if not LINKS_API_AVAILABLE:
        raise RuntimeError(_UNAVAILABLE_MSG)
    type_name = (u"%s" % (d.get("VisibilityType") or "ByHostView")).strip()
    if type_name not in TYPE_NAMES:
        type_name = "ByHostView"
    if type_name == "ByHostView":
        return reset_link_override(target_view, link_id)

    s = RevitLinkGraphicsSettings()
    s.LinkVisibilityType = getattr(LinkVisibility, "ByLinkView" if type_name == "ByLinkView"
                                   else "Custom")
    s.LinkedViewId = ElementId.InvalidElementId
    wanted = (u"%s" % (d.get("LinkedViewName") or "")).strip()
    return _apply(target_view, link_id, s, wanted_view_name=wanted)


# ======================================================================
# v1.3 - full "Revit Links" tab coverage
# ----------------------------------------------------------------------
# The V/G "Revit Links" tab row = Visibility | Halftone | Underlay |
# Display Settings. v1.2 only compared/copied Display Settings, and only
# by its label ("By Host View" == "By Host View" -> "identical"), so
#   * hidden/shown links (Visibility checkbox) were never copied,
#   * Halftone was never copied,
#   * two Custom settings with different sub-settings looked identical.
# Visibility/Halftone are per-view, per-link-instance element state
# (the same data as Hide in View > Elements / Override Graphics by
# Element), so they are never template-controlled and don't exist on
# view templates.
# ======================================================================
try:
    from System.Collections.Generic import List as _NetList
except Exception:  # pragma: no cover
    _NetList = None

from vismanager.compat import eid_val as _eid_val

_SKIP_PROPS = ("IsValidObject",)


def _norm(host_doc, link_id, prop_name, v):
    """Make a property value comparable/printable."""
    if isinstance(v, ElementId):
        if prop_name == "LinkedViewId":
            return _linked_view_name(host_doc, link_id, v) or "<none>"
        return _eid_val(v)
    if v is None or isinstance(v, (bool, int, float)):
        return v
    try:
        return u"%s" % v
    except Exception:
        return repr(v)


def settings_snapshot(view, link_id):
    """Every readable property of the link's RevitLinkGraphicsSettings,
    normalised (LinkedViewId -> linked view NAME so a remapped view of the
    same name still compares equal). ('ByHostView',) when not overridden.
    Uses reflection so whatever sub-settings this Revit version exposes
    are included automatically.
    """
    if not LINKS_API_AVAILABLE:
        return ("N/A",)
    s = _get_settings(view, link_id)
    if s is None or _vtype_name(s.LinkVisibilityType) == "ByHostView":
        return ("ByHostView",)
    doc = view.Document
    items = []
    try:
        props = s.GetType().GetProperties()
    except Exception:
        props = []
    for p in props:
        try:
            if p.Name in _SKIP_PROPS or not p.CanRead or len(p.GetIndexParameters()) > 0:
                continue
            items.append((p.Name, _norm(doc, link_id, p.Name, p.GetValue(s, None))))
        except Exception:
            continue
    if not items:  # reflection unavailable - fall back to the essentials
        items = [("LinkVisibilityType", _vtype_name(s.LinkVisibilityType)),
                 ("LinkedViewId", _linked_view_name(doc, link_id, s.LinkedViewId) or "<none>")]
    return tuple(sorted(items))


def _as_dict(snap):
    if snap and isinstance(snap[0], tuple):
        return dict(snap)
    return {"LinkVisibilityType": snap[0] if snap else ""}


def diff_settings(a, b):
    da, db = _as_dict(a), _as_dict(b)
    return ", ".join(sorted(k for k in set(da) | set(db) if da.get(k) != db.get(k)))


def _inst(view, link_id):
    return view.Document.GetElement(link_id)


def _type_of(view, link_id):
    inst = _inst(view, link_id)
    try:
        return view.Document.GetElement(inst.GetTypeId()) if inst is not None else None
    except Exception:
        return None


def _hidden(elem, view):
    try:
        return bool(elem.IsHidden(view)) if elem is not None else False
    except Exception:
        return False


def _can_hide(elem, view):
    try:
        return bool(elem.CanBeHidden(view)) if elem is not None else False
    except Exception:
        return False


def element_state(view, link_id):
    """(type_hidden, instance_hidden, element-override dict) in `view`, or
    None on view templates (they can't hold element-level state).

    v1.4: the V/G Revit Links tab has a TYPE row (the .rvt) with instance
    rows nested under it; each can be unticked separately. v1.3 only
    looked at the instance, so a link hidden at the type row could never
    be un-hidden.
    """
    from vismanager.filters_core import override_to_dict
    if view.IsTemplate:
        return None
    inst = _inst(view, link_id)
    if inst is None:
        return None
    try:
        ov = tuple(sorted(override_to_dict(view.Document, view.GetElementOverrides(link_id)).items()))
    except Exception:
        ov = None
    return (_hidden(_type_of(view, link_id), view), _hidden(inst, view), ov)


def element_status(view, link_id):
    st = element_state(view, link_id)
    if st is None:
        return ""
    tags = []
    if st[0]:
        tags.append("Hidden (type)")
    elif st[1]:
        tags.append("Hidden")
    if st[2] and dict(st[2]).get("Halftone") == "Yes":
        tags.append("Halftone")
    return ", ".join(tags)


def _ids(eid):
    lst = _NetList[ElementId]()
    lst.Add(eid)
    return lst


def _set_elem_hidden(view, elem, hidden, what):
    """Hide/unhide one element. Returns True if a call was made."""
    if elem is None or _hidden(elem, view) == bool(hidden):
        return False
    if hidden:
        if not _can_hide(elem, view):
            raise RuntimeError(u"Revit doesn't allow hiding the link {} in this view".format(what))
        view.HideElements(_ids(elem.Id))
    else:
        view.UnhideElements(_ids(elem.Id))
    return True


def _regen(view):
    """Hidden state is only reliable after regeneration (v1.3 read it
    back immediately, saw stale data, reported 'failed' and rolled back
    a change that had actually worked)."""
    try:
        view.Document.Regenerate()
    except Exception:
        pass


def _visibility_context(view, link_id):
    """Other reasons a link can be invisible that are NOT the V/G
    Revit Links checkbox - reported as notes to explain what you see."""
    notes = []
    inst = _inst(view, link_id)
    try:
        if not view.IsWorksetVisible(inst.WorksetId):
            notes.append("the link's workset is hidden in this view")
    except Exception:
        pass
    try:
        from Autodesk.Revit.DB import BuiltInCategory
        from vismanager.compat import make_eid
        cat_id = make_eid(int(BuiltInCategory.OST_RvtLinks))
        if view.GetCategoryHidden(cat_id):
            notes.append("the 'RVT Links' category is hidden in this view")
    except Exception:
        pass
    return notes


def _explain(src, before, after, view, link_id):
    labels = ("type row", "instance row")
    bits = []
    for i, lab in enumerate(labels):
        if after[i] != src[i]:
            bits.append(u"{} hidden: source={} target before={} after={}".format(
                lab, src[i], before[i], after[i]))
    if after[2] != src[2]:
        bits.append("halftone/overrides")
    t = _type_of(view, link_id)
    bits.append(u"CanBeHidden type={} instance={}".format(
        _can_hide(t, view), _can_hide(_inst(view, link_id), view)))
    return "; ".join(bits)


def _apply_state(target_view, link_id, type_hidden, inst_hidden, ogs):
    changed = False
    if type_hidden is not None:
        changed |= _set_elem_hidden(target_view, _type_of(target_view, link_id), type_hidden, "type (.rvt row)")
    if inst_hidden is not None:
        changed |= _set_elem_hidden(target_view, _inst(target_view, link_id), inst_hidden, "instance")
    if ogs is not None:
        target_view.SetElementOverrides(link_id, ogs)
    if changed:
        _regen(target_view)


def copy_element_state(source_view, target_view, link_id):
    """Copy Visibility (type + instance rows) and Halftone/by-element
    overrides. Returns "same" | "changed" | ("changed", note) | "n/a"."""
    if target_view.IsTemplate:
        return "n/a"
    src = element_state(source_view, link_id)
    before = element_state(target_view, link_id)
    if src is None:
        return "n/a"
    if before == src:
        return "same"
    _apply_state(target_view, link_id, src[0], src[1],
                 source_view.GetElementOverrides(link_id))
    after = element_state(target_view, link_id)
    if after != src:
        raise RuntimeError(u"Revit didn't keep it - " + _explain(src, before, after, target_view, link_id))
    ctx = _visibility_context(target_view, link_id)
    if ctx and not (src[0] or src[1]):
        return ("changed", u"link un-hidden, but it may still not display because " + " and ".join(ctx))
    return "changed"


def reset_element_state(target_view, link_id):
    """Unhide (type + instance) + clear by-element overrides (halftone)."""
    from Autodesk.Revit.DB import OverrideGraphicSettings
    if target_view.IsTemplate:
        return "n/a"
    before = element_state(target_view, link_id)
    _apply_state(target_view, link_id, False, False, OverrideGraphicSettings())
    after = element_state(target_view, link_id)
    if after[0] or after[1]:
        raise RuntimeError(u"link is still hidden after reset - " +
                           _explain((False, False, after[2]), before, after, target_view, link_id))
    return "same" if after == before else "changed"


def apply_element_row(target_view, link_id, visible, halftone):
    """Import: visible/halftone are True/False/None (None = leave).
    Visible=Yes unhides both rows; Visible=No hides the instance row."""
    if target_view.IsTemplate:
        return "n/a"
    before = element_state(target_view, link_id)
    ogs = None
    if halftone is not None:
        cur = target_view.GetElementOverrides(link_id)
        if bool(cur.Halftone) != bool(halftone):
            cur.SetHalftone(bool(halftone))
            ogs = cur
    if visible is None:
        _apply_state(target_view, link_id, None, None, ogs)
    elif visible:
        _apply_state(target_view, link_id, False, False, ogs)
    else:
        _apply_state(target_view, link_id, None, True, ogs)
    after = element_state(target_view, link_id)
    if visible is not None and bool(after[0] or after[1]) == bool(visible):
        raise RuntimeError(u"Revit didn't keep visibility - " +
                           _explain((not visible and after[0], not visible, after[2]), before, after,
                                    target_view, link_id))
    return "same" if after == before else "changed"


# ======================================================================
# v1.5 - Revit-native full "Revit Links" tab copy
# ----------------------------------------------------------------------
# The public API only exposes LinkVisibilityType + LinkedViewId. The
# Halftone / Underlay columns and every Custom sub-tab (Basics, Model
# Categories, Annotation Categories, Analytical, Import, Filters,
# Worksets, ...) cannot be read or written directly. Revit itself copies
# them through view templates ("Apply Template Properties"), so we do the
# same: build a throw-away template from the source that controls ONLY
# "V/G Overrides RVT Links", ApplyViewTemplateParameters() it to each
# target, then delete it. Granularity is the whole tab (all links).
# ======================================================================
import time as _time


def _links_param_val():
    from vismanager.compat import TEMPLATE_PARAM, eid_val
    bip = TEMPLATE_PARAM.get("links")
    if bip is None:
        raise RuntimeError("VIS_GRAPHICS_RVT_LINKS isn't available in this Revit version")
    return eid_val(ElementId(bip))


def build_links_only_template(source_view):
    """Create a temporary template carrying source_view's Revit Links tab
    and controlling nothing else. Must run inside a Transaction."""
    from vismanager.compat import eid_val
    doc = source_view.Document
    tmp = None
    if source_view.IsTemplate:
        try:
            from Autodesk.Revit.DB import ViewDuplicateOption
            tmp = doc.GetElement(source_view.Duplicate(ViewDuplicateOption.Duplicate))
        except Exception:
            tmp = None
    if tmp is None:
        if source_view.IsTemplate:
            raise RuntimeError("couldn't duplicate the source template")
        tmp = source_view.CreateViewTemplate()
    try:
        tmp.Name = u"__VisManager_links_tmp_{}".format(int(_time.time() * 1000))
    except Exception:
        pass

    links_val = _links_param_val()
    all_ids = list(tmp.GetTemplateParameterIds())
    if not any(eid_val(i) == links_val for i in all_ids):
        doc.Delete(tmp.Id)
        raise RuntimeError("this view type's templates don't include 'V/G Overrides RVT Links'")
    others = _NetList[ElementId]()
    for i in all_ids:
        if eid_val(i) != links_val:
            others.Add(i)
    tmp.SetNonControlledTemplateParameterIds(others)
    return tmp


def apply_links_tab(tmp_template, source_view, target_view, link_ids):
    """Apply the temp template's Revit Links tab to target_view, then
    verify what the API *can* read (display type + linked view per link).
    Halftone / Underlay / Custom sub-tabs are copied by Revit but are not
    readable, so they can't be verified here."""
    if target_view.IsTemplate:
        raise RuntimeError(u"Revit can't apply template properties to a view template - "
                           u"open the template's V/G and use its settings, or apply to views")
    if tmp_template.ViewType != target_view.ViewType:
        raise RuntimeError(u"source is a {} view, target is {} - Revit only transfers the "
                           u"Revit Links tab between views of the same type"
                           .format(source_view.ViewType, target_view.ViewType))
    target_view.ApplyViewTemplateParameters(tmp_template)
    try:
        target_view.Document.Regenerate()
    except Exception:
        pass
    bad = []
    for lid in link_ids:
        if settings_snapshot(target_view, lid) != settings_snapshot(source_view, lid):
            inst = target_view.Document.GetElement(lid)
            bad.append(inst.Name if inst is not None else str(lid))
    if bad:
        raise RuntimeError(u"display type/linked view still differs for: " + "; ".join(bad[:10]))
    return ("changed", u"entire Revit Links tab applied by Revit (Halftone, Underlay and "
                       u"Custom sub-tabs included - these can't be read back by the API, "
                       u"spot-check one link in V/G)")


def delete_temp_template(tmp_template):
    try:
        tmp_template.Document.Delete(tmp_template.Id)
        return True
    except Exception:
        return False

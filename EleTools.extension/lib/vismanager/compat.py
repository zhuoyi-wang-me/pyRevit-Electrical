# -*- coding: utf-8 -*-
"""Cross-version helpers shared by every VisManager module.

Why this module exists (root causes of several "nothing happens" bugs):

1. ElementId.IntegerValue was deprecated in Revit 2024 and REMOVED in
   Revit 2026 (replaced by ElementId.Value, Int64). The original code
   used .IntegerValue everywhere, so on 2026 every mode crashed while
   building the lists. eid_val()/make_eid() work on 2023-2026.

2. When a view has a View Template applied and that template *controls*
   "V/G Overrides Model / Annotation / Analytical / Filters / Revit
   Links", the view's own settings are ignored - Revit displays the
   template's settings. The original code read from and wrote to the
   view itself, so:
     - source lists showed stale/wrong data for template-driven views,
     - COPY/REMOVE "succeeded" on target views but nothing changed on
       screen.
   controlling_template() detects this so callers can read from the
   template and skip (with a clear reason) targets they can't change.
"""
from Autodesk.Revit.DB import (ElementId, BuiltInParameter, IFailuresPreprocessor,
                                FailureSeverity, FailureProcessingResult)

try:
    from System import Int64 as _Int64
except Exception:  # pragma: no cover - always available inside Revit
    _Int64 = None


# ----------------------------------------------------------- ElementId
def eid_val(eid):
    """Integer value of an ElementId on any Revit version (2023-2026+)."""
    if eid is None:
        return -1
    try:
        return int(eid.Value)          # Revit 2024+
    except AttributeError:
        return int(eid.IntegerValue)   # Revit 2023 and earlier


def make_eid(n):
    """Build an ElementId from an int on any Revit version."""
    n = int(n)
    if _Int64 is not None and hasattr(ElementId, "Value"):
        try:
            return ElementId(_Int64(n))
        except Exception:
            pass
    return ElementId(n)


def is_invalid(eid):
    return eid is None or eid == ElementId.InvalidElementId


# ------------------------------------------------ view template control
def _bip(name):
    return getattr(BuiltInParameter, name, None)


# Template "include" rows, keyed by what VisManager manipulates.
TEMPLATE_PARAM = {
    "filters": _bip("VIS_GRAPHICS_FILTERS"),
    "links": _bip("VIS_GRAPHICS_RVT_LINKS"),
    "Model": _bip("VIS_GRAPHICS_MODEL"),
    "Annotation": _bip("VIS_GRAPHICS_ANNOTATION"),
    "Analytical": _bip("VIS_GRAPHICS_ANALYTICAL_MODEL"),
    "Imported": _bip("VIS_GRAPHICS_IMPORT"),
    "worksets": _bip("VIS_GRAPHICS_WORKSETS"),
}


def controlling_template(view, key):
    """Return the View Template that controls setting `key` (see
    TEMPLATE_PARAM) for `view`, or None if the view controls it itself.
    Templates themselves always return None. key=None means "not a
    template-controllable setting" (e.g. per-element hide) -> None.
    """
    if key is None:
        return None
    try:
        if view.IsTemplate:
            return None
        tid = view.ViewTemplateId
    except Exception:
        return None
    if is_invalid(tid):
        return None
    tmpl = view.Document.GetElement(tid)
    if tmpl is None:
        return None
    bip = TEMPLATE_PARAM.get(key)
    if bip is None:
        # Unknown parameter on this Revit version: assume the template
        # controls it (safer than silently writing to an ignored view).
        return tmpl
    target = eid_val(ElementId(bip))
    try:
        for pid in tmpl.GetNonControlledTemplateParameterIds():
            if eid_val(pid) == target:
                return None   # unchecked in the template's "Include" column
    except Exception:
        return None
    return tmpl


def effective_view(view, key):
    """The element whose settings Revit actually displays for `key`:
    the controlling template if there is one, otherwise the view.
    """
    return controlling_template(view, key) or view


def template_include_off(template, key):
    """True if `template` is a template whose Include checkbox for `key`
    is OFF - writing to it works but has no effect on its views.
    """
    if key is None:
        return False
    try:
        if not template.IsTemplate:
            return False
    except Exception:
        return False
    bip = TEMPLATE_PARAM.get(key)
    if bip is None:
        return False
    target = eid_val(ElementId(bip))
    try:
        return any(eid_val(p) == target for p in template.GetNonControlledTemplateParameterIds())
    except Exception:
        return False


def in_temporary_view_properties(view):
    """True if the view is in Temporary View Properties mode: Revit then
    displays a temporary template, so real V/G changes don't show."""
    try:
        from Autodesk.Revit.DB import TemporaryViewMode
        return bool(view.IsInTemporaryViewMode(TemporaryViewMode.TemporaryViewProperties))
    except Exception:
        return False


# --------------------------------------------------- transactions
class WarningSwallower(IFailuresPreprocessor):
    """Deletes plain warnings so a single harmless warning doesn't pop a
    dialog mid-batch or, worse, roll back the whole transaction.
    Errors are left alone so Revit still reports them.
    """
    def PreprocessFailures(self, failures_accessor):
        try:
            for f in failures_accessor.GetFailureMessages():
                if f.GetSeverity() == FailureSeverity.Warning:
                    failures_accessor.DeleteWarning(f)
        except Exception:
            pass
        return FailureProcessingResult.Continue


def attach_warning_swallower(transaction):
    opts = transaction.GetFailureHandlingOptions()
    opts.SetFailuresPreprocessor(WarningSwallower())
    transaction.SetFailureHandlingOptions(opts)

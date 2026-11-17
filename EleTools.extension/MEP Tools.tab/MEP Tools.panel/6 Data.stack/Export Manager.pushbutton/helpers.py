# -*- coding: utf-8 -*-
"""Shared helpers for the pyRevit Revit 2025.4 Export Manager.

Keep UI controls and export logic out of this module.  It provides common
Revit/document functions used by ui.py, export_pdf.py and export_dwg.py.

MODELESS NOTE
-------------
The window is modeless, so the document/UIDocument can NOT be captured once
at import time (the user may switch views, switch documents or close the
model while the window stays open).  ui.run() calls set_context() once with
the document the window is bound to, and everything else asks for the
*current* UIDocument through active_uidoc() at the moment it is needed.
"""

import os

from Autodesk.Revit.DB import (
    BoundingBoxXYZ,
    Transaction,
    View,
    ViewSheet,
    XYZ
)


# Bound by set_context().  Kept as module attributes (not functions) so the
# existing "helpers.doc" call sites keep working.
doc = None
_uiapp = None


class ExportCancelled(Exception):
    """Raised from a progress callback when the user presses Cancel.

    The export engines attach whatever they finished so far as
    ``.partial`` before re-raising, so the summary can still report it.
    """

    def __init__(self, message="Export cancelled by user."):
        Exception.__init__(self, message)
        self.partial = None


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

def set_context(document, uiapp):
    """Bind the helpers to the document the Export Manager window serves."""
    global doc, _uiapp
    doc = document
    _uiapp = uiapp


def uiapp():
    return _uiapp


def doc_is_valid(document=None):
    """False once the bound model has been closed."""
    target = document if document is not None else doc
    if target is None:
        return False
    try:
        return bool(target.IsValidObject)
    except Exception:
        return False


def same_document(first, second):
    if first is None or second is None:
        return False
    try:
        if first.Equals(second):
            return True
    except Exception:
        pass
    try:
        return (first.PathName or first.Title) == (second.PathName or second.Title)
    except Exception:
        return False


def active_uidoc():
    """The active UIDocument *only if* it shows the bound document.

    Returns None when the user has switched Revit to another model, so the
    'Current window' export ranges can never export a view from the wrong
    document.
    """
    if _uiapp is None or not doc_is_valid():
        return None
    try:
        current = _uiapp.ActiveUIDocument
    except Exception:
        return None
    if current is None:
        return None
    try:
        if same_document(current.Document, doc):
            return current
    except Exception:
        pass
    return None


def document_title(document=None):
    target = document if document is not None else doc
    try:
        return target.Title or ""
    except Exception:
        return ""


def element_id_value(element_id):
    """ElementId -> int.  ElementId.Value exists from Revit 2024 and
    IntegerValue is obsolete in 2024/2025 and removed in 2026."""
    if element_id is None:
        return None
    try:
        return int(element_id.Value)
    except Exception:
        pass
    try:
        return int(element_id.IntegerValue)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Grid item
# ---------------------------------------------------------------------------

class ExportItem(object):
    """A WPF-friendly wrapper around a Revit sheet or view."""

    def __init__(self, element, number="", name=None, filename=None):
        self.IsSelected = False
        self.Element = element
        self.Number = number or ""
        self.Name = name or _element_name(element)
        self.FileName = filename or self.Number or self.Name
        # Populated by sheet_filter_ui.apply_to() with the values of the
        # parameters currently being filtered on.  Empty when no sheet
        # filter is active.
        self.FilterValues = ""
        # View-only metadata (blank for sheets).  Filled by view_search.
        self.ViewTypeName = ""
        self.LevelName = ""
        self.SheetRef = ""
        self.TemplateName = ""
        # Pre-computed, lower-cased search fields so typing in the search
        # box never touches the Revit API (safe + fast in a modeless form).
        self.SearchFields = {}
        self.SearchText = ""


def _element_name(element):
    try:
        return element.Name or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def safe_filename(value, fallback="Export"):
    """Return a Windows-safe filename stem with no extension."""
    result = str(value or "").strip()
    for character in '<>:"/\\|?*':
        result = result.replace(character, "-")
    result = result.rstrip(". ")
    return result or fallback


def ensure_folder(folder):
    """Validate/create an output directory and return its absolute path."""
    if not folder or not str(folder).strip():
        raise ValueError("Select an output folder.")
    folder = os.path.abspath(str(folder).strip())
    if not os.path.exists(folder):
        os.makedirs(folder)
    return folder


def selected_items(items):
    """Return only checked ExportItem objects from a sheet/view collection."""
    selected = []
    for item in items or []:
        try:
            if item.IsSelected:
                selected.append(item)
        except Exception:
            pass
    return selected


def is_printable(element):
    """True only for sheets/views Revit can print or export."""
    try:
        return bool(element.CanBePrinted)
    except Exception:
        return False


def is_view(element):
    """True if the element is a Revit View or ViewSheet."""
    try:
        return isinstance(element, View)
    except Exception:
        return False


def element_label(element):
    """Friendly item label for progress bars and error messages."""
    try:
        number = element.SheetNumber
        if number:
            return number
    except Exception:
        pass
    try:
        if element.Name:
            return element.Name
    except Exception:
        pass
    return "Unknown Item"


def split_results(results):
    """Return exported/skipped/error counts from an export-engine result dict."""
    results = results or {}
    return (
        len(results.get("exported", [])),
        len(results.get("skipped", [])),
        len(results.get("errors", []))
    )


def merge_results(*result_sets):
    """Merge result dictionaries returned from export_pdf/export_dwg."""
    merged = {"exported": [], "skipped": [], "errors": [], "warnings": []}
    for result in result_sets:
        if not result:
            continue
        for key in merged:
            merged[key].extend(result.get(key, []))
    return merged


def summary_text(results, heading="Export Complete", max_lines=25):
    """Build a concise text summary suitable for forms.alert().

    Long error/warning lists are truncated so the dialog never grows
    taller than the screen on a 500-sheet export.
    """
    exported, skipped, errors = split_results(results)
    lines = [heading, "", "Exported: {}".format(exported)]
    if skipped:
        lines.append("Skipped: {}".format(skipped))

    detail = []
    for error in results.get("errors", []):
        detail.append("- {}: {}".format(
            error.get("name", "Unknown Item"),
            error.get("error", "Unknown error")
        ))
    if errors:
        lines.append("Errors: {}".format(errors))

    warnings = results.get("warnings", [])
    warning_lines = []
    for warning in warnings:
        warning_lines.append("- {}: {}".format(
            warning.get("name", "Export"),
            warning.get("warning", "")
        ))

    if detail:
        lines.extend(detail[:max_lines])
        if len(detail) > max_lines:
            lines.append("  ... and {} more error(s)".format(len(detail) - max_lines))
    if warning_lines:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(warning_lines[:max_lines])
        if len(warning_lines) > max_lines:
            lines.append("  ... and {} more warning(s)".format(len(warning_lines) - max_lines))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Visible-portion crop
# ---------------------------------------------------------------------------

def get_open_uiview(uidoc, view_id):
    """Return the open UIView showing view_id, or None if it isn't open."""
    if uidoc is None:
        return None
    try:
        for uiview in uidoc.GetOpenUIViews():
            if uiview.ViewId == view_id:
                return uiview
    except Exception:
        pass
    return None


def visible_portion_crop_box(uidoc, view):
    """Return a BoundingBoxXYZ matching what is currently on screen for an
    open view, or None if that view isn't open in any Revit window.

    FIX: UIView.GetZoomCorners() returns MODEL coordinates, but
    View.CropBox is expressed in the view's own coordinate system
    (CropBox.Transform).  The original code copied the model X/Y straight
    into the crop box, which only happened to work for un-rotated plans at
    the project origin and produced a wrong crop for sections, elevations
    and rotated plans.  The corners are now mapped through the inverse of
    the crop transform first.
    """
    uiview = get_open_uiview(uidoc, view.Id)
    if uiview is None:
        return None
    try:
        corners = uiview.GetZoomCorners()
        existing = view.CropBox
        to_view = existing.Transform.Inverse
        first = to_view.OfPoint(corners[0])
        second = to_view.OfPoint(corners[1])

        box = BoundingBoxXYZ()
        box.Transform = existing.Transform
        box.Min = XYZ(min(first.X, second.X), min(first.Y, second.Y), existing.Min.Z)
        box.Max = XYZ(max(first.X, second.X), max(first.Y, second.Y), existing.Max.Z)
        return box
    except Exception:
        return None


def apply_temp_crop(document, view, box):
    """Temporarily override a view's crop region. Returns the original
    (crop_box, crop_active, crop_visible) state so it can be restored.

    Must run inside a valid Revit API context (the ExternalEvent handler).
    """
    original = (view.CropBox, view.CropBoxActive, view.CropBoxVisible)
    transaction = Transaction(document, "Export Manager - temporary crop")
    transaction.Start()
    try:
        view.CropBoxActive = True
        view.CropBoxVisible = True
        view.CropBox = box
        transaction.Commit()
    except Exception:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
        raise
    return original


def restore_crop(document, view, original):
    """Undo apply_temp_crop(). Safe to call even if the override failed."""
    crop_box, crop_active, crop_visible = original
    transaction = Transaction(document, "Export Manager - restore crop")
    transaction.Start()
    try:
        view.CropBox = crop_box
        view.CropBoxActive = crop_active
        view.CropBoxVisible = crop_visible
        transaction.Commit()
    except Exception:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()


def maybe_apply_visible_crop(document, uidoc, element, export_range, active_view):
    """If export_range is 'visible' and element is the view currently open
    and active in Revit, temporarily crop it to the on-screen extent.

    Returns a zero-argument callable that restores the original crop, or
    None if nothing changed.  Sheets are skipped.
    """
    if export_range != "visible" or uidoc is None or active_view is None:
        return None
    if element is None or isinstance(element, ViewSheet):
        return None
    try:
        if element.Id != active_view.Id:
            return None
    except Exception:
        return None

    box = visible_portion_crop_box(uidoc, element)
    if box is None:
        return None

    try:
        original = apply_temp_crop(document, element, box)
    except Exception:
        return None

    def _restore():
        restore_crop(document, element, original)

    return _restore


# ---------------------------------------------------------------------------
# WPF helpers
# ---------------------------------------------------------------------------

def set_items_source(control, values):
    """Replace a WPF list/DataGrid ItemsSource safely."""
    control.ItemsSource = None
    control.ItemsSource = list(values or [])


def refresh_grid(control):
    """Refresh a bound WPF DataGrid/ListBox if one is supplied."""
    try:
        control.CommitEdit()
        control.CommitEdit()
    except Exception:
        pass
    try:
        control.Items.Refresh()
    except Exception:
        pass

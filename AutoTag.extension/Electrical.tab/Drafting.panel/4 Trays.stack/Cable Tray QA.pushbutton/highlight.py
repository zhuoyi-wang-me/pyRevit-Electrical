# -*- coding: utf-8 -*-
"""Cable Tray QA/QC - highlighting and navigation in Revit.

"Highlight Errors" changes nothing: errors are shown with Revit's ordinary
SELECTION (the blue highlight), which disappears with "Clear Highlights" or as
soon as the user clicks elsewhere.

"Colour by issue" is the ONE action of this tool that writes to the model, and
only because the user asks for it: it gives the elements of each kind of issue
their own colour in the ACTIVE VIEW (per-element graphic overrides, one
transaction, undoable with Ctrl+Z). "Remove colours" takes them off again.

The window is modeless, so Revit may only be touched from an ExternalEvent.
RevitRunner wraps that: runner.run(function) queues `function(uiapp)` to be
executed by Revit on its own thread at the next idle moment.
"""
from System.Collections.Generic import List

from Autodesk.Revit.DB import ElementId, ViewSheet
from Autodesk.Revit.UI import ExternalEvent, IExternalEventHandler

from revit_reader import id_value, make_id


class _Handler(IExternalEventHandler):
    """Runs the queued function inside Revit's API context."""

    def __init__(self):
        self.action = None
        self.on_error = None

    def Execute(self, uiapp):
        action, self.action = self.action, None
        if action is None:
            return
        try:
            action(uiapp)
        except Exception as error:
            if self.on_error is not None:
                try:
                    self.on_error(error)
                except Exception:
                    pass

    def GetName(self):
        return 'Cable Tray QA/QC'


class RevitRunner(object):
    """runner.run(function) -> function(uiapp) executed by Revit when idle."""

    def __init__(self, on_error=None):
        self._handler = _Handler()
        self._handler.on_error = on_error
        self._event = ExternalEvent.Create(self._handler)

    def run(self, action):
        self._handler.action = action
        self._event.Raise()

    def dispose(self):
        try:
            self._event.Dispose()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# what to select
# ---------------------------------------------------------------------------
def ids_of(rows, include_trays=True):
    """Element ids to select for some result rows: the annotation and,
    when it is a real tag, the cable tray it points at."""
    ids = []
    seen = set()
    for row in rows:
        values = [row.get('annotation_id'), row.get('element_id') if include_trays else None]
        # a cable tray row stands for every segment of the run
        values.extend(row.get('element_ids') or [])
        for value in values:
            if value is not None and value not in seen:
                seen.add(value)
                ids.append(value)
    return ids


def _id_list(doc, values):
    """List[ElementId] of the ids that still exist in the model."""
    result = List[ElementId]()
    for value in values:
        element_id = make_id(value)
        if doc.GetElement(element_id) is not None:
            result.Add(element_id)
    return result


def rows_in_active_view(uidoc, rows):
    """The rows whose annotation is visible in the view the user is looking at:
    the active view itself, or - when a sheet is active - any view on it."""
    view = uidoc.ActiveView
    view_id = id_value(view.Id)
    if isinstance(view, ViewSheet):
        return [row for row in rows if row.get('sheet_id') == view_id or row.get('view_id') == view_id]
    return [row for row in rows if row.get('view_id') == view_id]


# ---------------------------------------------------------------------------
# actions (call through RevitRunner)
# ---------------------------------------------------------------------------
def highlight_errors(uidoc, error_rows):
    """Select the error annotations (and their trays). Prefers the ones in the
    active view; if none of the errors is in the active view, selects them all
    so they light up as the user opens each sheet.

    Returns (selected count, True if limited to the active view)."""
    located = [row for row in error_rows if row.get('annotation_id') is not None]
    here = rows_in_active_view(uidoc, located)
    chosen = here if here else located
    ids = _id_list(uidoc.Document, ids_of(chosen))
    uidoc.Selection.SetElementIds(ids)
    uidoc.RefreshActiveView()
    return ids.Count, bool(here)


def clear_highlights(uidoc):
    uidoc.Selection.SetElementIds(List[ElementId]())
    uidoc.RefreshActiveView()


def open_sheet(uidoc, row):
    """Open the sheet of a result row and select its annotation there.
    Returns a message for the status bar."""
    doc = uidoc.Document
    sheet = doc.GetElement(make_id(row['sheet_id'])) if row.get('sheet_id') is not None else None
    if sheet is None:
        return open_view(uidoc, row)
    uidoc.ActiveView = sheet
    ids = _id_list(doc, ids_of([row]))
    uidoc.Selection.SetElementIds(ids)
    uidoc.RefreshActiveView()
    return u'Sheet {0} - {1}'.format(row.get('sheet_number'), row.get('sheet_name'))


def open_view(uidoc, row):
    """Open the model view that holds the annotation, select it and zoom to it."""
    doc = uidoc.Document
    view = doc.GetElement(make_id(row['view_id'])) if row.get('view_id') is not None else None
    if view is None:
        # a cable tray row is a model element, not something in one view:
        # let Revit find a view that shows it and zoom there
        ids = _id_list(doc, ids_of([row]))
        if not ids.Count:
            return u'This row has no view to open (the tag is missing from the model).'
        uidoc.Selection.SetElementIds(ids)
        uidoc.ShowElements(ids)
        return u'{0} cable tray segment(s) of {1}'.format(ids.Count, row.get('tag') or 'this row')
    uidoc.ActiveView = view
    ids = _id_list(doc, ids_of([row]))
    uidoc.Selection.SetElementIds(ids)
    if ids.Count:
        try:
            uidoc.ShowElements(ids)
        except Exception:
            pass
    return u'View {0}'.format(row.get('view_name'))


# ---------------------------------------------------------------------------
# colour by issue (view graphic overrides - the only thing that modifies the model)
# ---------------------------------------------------------------------------
class ColourError(Exception):
    """A reason the colours cannot be applied, in words for the status bar."""


def _visible_ids(doc, view):
    from Autodesk.Revit.DB import FilteredElementCollector
    return set(id_value(element_id)
               for element_id in FilteredElementCollector(doc, view.Id).ToElementIds())


def _solid_fill_id(doc):
    """Id of the solid fill pattern, or None (lines are then coloured only)."""
    try:
        from Autodesk.Revit.DB import FillPatternElement, FilteredElementCollector
        for pattern in FilteredElementCollector(doc).OfClass(FillPatternElement):
            try:
                if pattern.GetFillPattern().IsSolidFill:
                    return pattern.Id
            except Exception:
                continue
    except Exception:
        pass
    return None


def _override(rgb, solid_id, weight):
    from Autodesk.Revit.DB import Color, OverrideGraphicSettings
    colour = Color(rgb[0], rgb[1], rgb[2])
    settings = OverrideGraphicSettings()
    # each setter is tried on its own: the pattern setters were renamed in
    # Revit 2019 and a tag has no surface to fill
    for apply in (lambda: settings.SetProjectionLineColor(colour),
                  lambda: settings.SetCutLineColor(colour),
                  lambda: settings.SetProjectionLineWeight(weight) if weight else None,
                  lambda: settings.SetSurfaceForegroundPatternColor(colour),
                  lambda: settings.SetSurfaceForegroundPatternId(solid_id) if solid_id is not None else None,
                  lambda: settings.SetCutForegroundPatternColor(colour),
                  lambda: settings.SetCutForegroundPatternId(solid_id) if solid_id is not None else None):
        try:
            apply()
        except Exception:
            pass
    return settings


def _model_view(uidoc):
    view = uidoc.ActiveView
    if view is None or isinstance(view, ViewSheet):
        raise ColourError('Open a model view (plan, section or 3D) first: elements cannot be '
                          'coloured on a sheet.')
    return view


def colour_by_status(uidoc, rows, colours, weight=0):
    """Give the elements of each result row the colour of its status, in the
    active view. Only elements that are in that view are touched; an element
    that belongs to rows of two statuses keeps the first (rows come sorted
    most serious first).

    Returns ({status: elements coloured}, [ids coloured], elements not in this view)."""
    from Autodesk.Revit.DB import Transaction
    doc = uidoc.Document
    view = _model_view(uidoc)
    visible = _visible_ids(doc, view)
    plan = []                      # (status, id)
    seen = set()
    elsewhere = 0
    for row in rows:
        if row['status'] not in colours:
            continue
        for value in ids_of([row]):
            if value in seen:
                continue
            seen.add(value)
            if value in visible:
                plan.append((row['status'], value))
            else:
                elsewhere += 1
    if not plan:
        return {}, [], elsewhere
    solid_id = _solid_fill_id(doc)
    settings = {}
    counts = {}
    transaction = Transaction(doc, 'Cable Tray QA - colour by issue')
    transaction.Start()
    try:
        for status, value in plan:
            if status not in settings:
                settings[status] = _override(colours[status], solid_id, weight)
            view.SetElementOverrides(make_id(value), settings[status])
            counts[status] = counts.get(status, 0) + 1
        transaction.Commit()
    except Exception:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
        raise
    uidoc.RefreshActiveView()
    return counts, [value for _status, value in plan], elsewhere


def remove_colours(uidoc, values):
    """Take the colour overrides off these elements in the active view.
    Returns how many were reset."""
    from Autodesk.Revit.DB import OverrideGraphicSettings, Transaction
    doc = uidoc.Document
    view = _model_view(uidoc)
    visible = _visible_ids(doc, view)
    targets = [value for value in set(values) if value in visible]
    if not targets:
        return 0
    blank = OverrideGraphicSettings()
    transaction = Transaction(doc, 'Cable Tray QA - remove colours')
    transaction.Start()
    try:
        for value in targets:
            view.SetElementOverrides(make_id(value), blank)
        transaction.Commit()
    except Exception:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
        raise
    uidoc.RefreshActiveView()
    return len(targets)
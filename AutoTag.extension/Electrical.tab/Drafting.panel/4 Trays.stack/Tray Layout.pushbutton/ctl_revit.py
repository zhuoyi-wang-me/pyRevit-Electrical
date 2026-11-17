# -*- coding: utf-8 -*-
"""Cable Tray Layout - everything that touches Revit.

Creates (or updates) one drafting view per tray and fills it with ordinary,
editable view-specific elements: detail lines, detail arcs and text notes.
"""
import math

import clr
import System
from Autodesk.Revit.DB import (Arc, BuiltInParameter, DimensionStyleType, DimensionType, ElementId,
                               ElementTypeGroup, FilteredElementCollector, HorizontalTextAlignment, Line,
                               ReferenceArray, TextNote, TextNoteOptions, TextNoteType,
                               Transaction, TransactionStatus, ViewDrafting, ViewFamily, ViewFamilyType, XYZ)
from Autodesk.Revit.DB.ExtensibleStorage import AccessLevel, Entity, Schema, SchemaBuilder

import ctl_drawing

MM_PER_FOOT = 304.8
VIEW_PREFIX = 'Cable Tray - '
DEFAULT_COMBINED_NAME = 'Cable Tray Layouts'         # the view that holds several trays

# how the trays are put into views
ONE_VIEW, VIEW_PER_TRAY = 'one', 'each'
VIEW_MODES = [
    (ONE_VIEW, 'All selected trays in one view, one below the other'),
    (VIEW_PER_TRAY, 'One view per tray ("Cable Tray - <Tray ID>")'),
]
TEXT_SIZE_MM = 2.5                         # the text type nearest to this paper size is used

# view scales offered in the window; 0 = chosen to suit the cables
DEFAULT_SCALE = 50
SCALE_CHOICES = [(50, '1 : 50'), (0, 'Automatic (suits the cable sizes)'), (1, '1 : 1'), (2, '1 : 2'), (5, '1 : 5'),
                 (10, '1 : 10'), (20, '1 : 20'), (25, '1 : 25'), (100, '1 : 100')]

# what to do when the view of a tray already exists
UPDATE, COPY, SKIP = 'update', 'copy', 'skip'
EXISTING = [
    (UPDATE, 'Update the existing view (redraw what this tool drew)'),
    (COPY, 'Keep it and create a new numbered view'),
    (SKIP, 'Skip that tray'),
]

# preferred line styles; the default detail-line style is kept when none exists
STYLES = {
    'tray': ('<Wide Lines>', 'Wide Lines'),
    'cable': ('<Medium Lines>', 'Medium Lines'),
    'thin': ('<Thin Lines>', 'Thin Lines'),
}


class DrawError(Exception):
    pass


def id_value(element_id):
    """The number of an ElementId in Revit 2024+ (Value) and older (IntegerValue)."""
    value = getattr(element_id, 'Value', None)
    return int(element_id.IntegerValue if value is None else value)


def make_id(value):
    try:
        return ElementId(System.Int64(value))
    except Exception:
        return ElementId(int(value))


def view_name(tray_id):
    """'Cable Tray - <id>', without the characters Revit refuses in names."""
    clean = u''.join(u'-' if ch in u'\\:{}[]|;<>?`~' else ch for ch in u'{0}'.format(tray_id)).strip()
    return VIEW_PREFIX + clean


# ---------------------------------------------------------------------------
# remembering what was drawn (stored on the view itself)
# ---------------------------------------------------------------------------
_SCHEMA_GUID = System.Guid('b7c1d2e3-4f5a-4b6c-8d7e-9f0a1b2c3d4e')
_FIELDS = ('Tray', 'Ids')


def _schema():
    schema = Schema.Lookup(_SCHEMA_GUID)
    if schema is not None:
        return schema
    builder = SchemaBuilder(_SCHEMA_GUID)
    builder.SetReadAccessLevel(AccessLevel.Public)
    builder.SetWriteAccessLevel(AccessLevel.Public)
    builder.SetSchemaName('CableTrayLayoutView')
    builder.SetDocumentation('Drafting view drawn by the Cable Tray Layout tool, and the elements it drew')
    for name in _FIELDS:
        builder.AddSimpleField(name, clr.GetClrType(System.String))
    return builder.Finish()


def _remember(view, tray_id, ids):
    schema = _schema()
    entity = Entity(schema)
    entity.Set[System.String](schema.GetField('Tray'), u'{0}'.format(tray_id))
    entity.Set[System.String](schema.GetField('Ids'), ','.join(str(id_value(i)) for i in ids))
    view.SetEntity(entity)


def generated_ids(view):
    """ElementIds this tool drew in the view; None when the view is not one of ours."""
    schema = Schema.Lookup(_SCHEMA_GUID)
    if schema is None:
        return None
    try:
        entity = view.GetEntity(schema)
        if entity is None or not entity.IsValid():
            return None
        text = entity.Get[System.String](schema.GetField('Ids'))
    except Exception:
        return None
    return [make_id(int(part)) for part in (text or '').split(',') if part.strip()]


# ---------------------------------------------------------------------------
# looking things up
# ---------------------------------------------------------------------------
def drafting_views(doc):
    return dict((view.Name, view) for view in FilteredElementCollector(doc).OfClass(ViewDrafting)
                if not view.IsTemplate)


def _drafting_type_id(doc):
    for view_type in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        if view_type.ViewFamily == ViewFamily.Drafting:
            return view_type.Id
    raise DrawError('This project has no drafting view type.')


def text_type(doc, target_mm=TEXT_SIZE_MM):
    """(type id, size in mm) of the text type whose size is nearest to target_mm."""
    best = None
    for kind in FilteredElementCollector(doc).OfClass(TextNoteType):
        try:
            size = kind.get_Parameter(BuiltInParameter.TEXT_SIZE).AsDouble() * MM_PER_FOOT
        except Exception:
            continue
        if size > 0 and (best is None or abs(size - target_mm) < abs(best[1] - target_mm)):
            best = (kind.Id, size)
    if best is None:
        return doc.GetDefaultElementTypeId(ElementTypeGroup.TextNoteType), target_mm
    return best


def _name_of(element):
    """Element.Name, which IronPython cannot always read directly on a type."""
    try:
        return element.Name
    except Exception:
        return element.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM).AsString()


def dimension_types(doc):
    """The linear dimension types of the project for the window:
    ([(name, id)] sorted by name, index of the project's default one)."""
    found = []
    for kind in FilteredElementCollector(doc).OfClass(DimensionType):
        try:
            if kind.StyleType != DimensionStyleType.Linear:
                continue
            name = u'{0}'.format(_name_of(kind) or u'')
        except Exception:
            continue
        if name.strip():                                  # Revit also keeps nameless internal ones
            found.append((name, kind.Id))
    found.sort(key=lambda item: item[0].lower())
    default = 0
    try:
        wanted = id_value(doc.GetDefaultElementTypeId(ElementTypeGroup.LinearDimensionType))
        for index, item in enumerate(found):
            if id_value(item[1]) == wanted:
                default = index
    except Exception:
        pass
    return found, default


def text_types(doc):
    """The text types of the project for the window: [(name, id, size in mm)],
    smallest first."""
    found = []
    for kind in FilteredElementCollector(doc).OfClass(TextNoteType):
        try:
            size = kind.get_Parameter(BuiltInParameter.TEXT_SIZE).AsDouble() * MM_PER_FOOT
            found.append((u'{0}'.format(_name_of(kind)), kind.Id, size))
        except Exception:
            continue
    return sorted(found, key=lambda item: (item[2], item[0].lower()))


def _unique_name(names, wanted):
    number = 2
    while u'{0} ({1})'.format(wanted, number) in names:
        number += 1
    return u'{0} ({1})'.format(wanted, number)


# ---------------------------------------------------------------------------
# the canvas: millimetres in, Revit elements out
# ---------------------------------------------------------------------------
class RevitCanvas(object):
    def __init__(self, doc, view, text_type_id, thin_only=False, dimension_type=None):
        self.doc = doc
        self.view = view
        self.text_type_id = text_type_id
        self.thin_only = thin_only
        self.dimension_type = dimension_type              # a DimensionType of the project, or None = Revit's default
        self.dimension_count = 0
        self.dimension_problems = []
        self.dimension_text = None                        # text height of the dimension style in model mm
        self.unattached = 0                               # chains that could not be attached to the cables
        self._regenerated = False
        self.ids = []
        self._styles = None
        try:
            self._shortest = doc.Application.ShortCurveTolerance
        except Exception:
            self._shortest = 0.0026

    @staticmethod
    def _point(x, y):
        return XYZ(x / MM_PER_FOOT, y / MM_PER_FOOT, 0.0)

    def _add(self, curve, style):
        element = self.doc.Create.NewDetailCurve(self.view, curve)
        self.ids.append(element.Id)
        self._regenerated = False                         # a new line: regenerate before the next dimension
        try:
            if self._styles is None:                      # read the available styles once
                self._styles = {}
                for style_id in element.GetLineStyleIds():
                    found = self.doc.GetElement(style_id)
                    if found is not None:
                        self._styles[found.Name] = found
            for name in STYLES.get('thin' if self.thin_only else style, ()):
                if name in self._styles:
                    element.LineStyle = self._styles[name]
                    break
        except Exception:
            pass                                          # the default style is fine
        return element

    def line(self, x1, y1, x2, y2, style='thin'):
        start, end = self._point(x1, y1), self._point(x2, y2)
        if start.DistanceTo(end) <= self._shortest:
            return None                                   # Revit cannot make a line this short
        return self._add(Line.CreateBound(start, end), style)

    def dimension(self, lines, placement, label=None):
        """A real Revit dimension between detail lines (what line() returned),
        with the dimension type chosen in the window. placement is
        (x1, y1, x2, y2) in mm: where the dimension line lies. More than two
        lines give one chain dimension. Returns False when Revit refuses, so
        the caller can draw the dimension with lines and text instead."""
        try:
            if not self._regenerated:
                self.doc.Regenerate()                     # new lines have no reference before this
                self._regenerated = True
            references = ReferenceArray()
            for item in lines:
                if isinstance(item, tuple):               # (arc, end): the left or right edge of a cable
                    reference = item[0].GeometryCurve.GetEndPointReference(item[1])
                else:                                     # a line: a tray side, the tray bottom ...
                    reference = item.GeometryCurve.Reference
                if reference is None:
                    raise DrawError('a detail line gave no reference')
                references.Append(reference)
            where = Line.CreateBound(self._point(placement[0], placement[1]), self._point(placement[2], placement[3]))
            if self.dimension_type is not None:
                made = self.doc.Create.NewDimension(self.view, where, references, self.dimension_type)
            else:
                made = self.doc.Create.NewDimension(self.view, where, references)
            if made is None:
                raise DrawError('Revit returned no dimension')
        except Exception as error:
            self.dimension_problems.append(u'{0}'.format(error))
            return False
        self.ids.append(made.Id)
        self.dimension_count += 1
        if label:
            try:
                made.Below = label                        # the name of the dimension, under its value
            except Exception:
                pass
        return True

    def circle(self, x, y, radius, style='cable'):
        centre = self._point(x, y)
        r = radius / MM_PER_FOOT
        try:
            self._add(Arc.Create(centre, r, 0.0, 2.0 * math.pi, XYZ.BasisX, XYZ.BasisY), style)
        except Exception:
            # some versions refuse a closed circle here: two half circles instead
            self._add(Arc.Create(centre, r, 0.0, math.pi, XYZ.BasisX, XYZ.BasisY), style)
            self._add(Arc.Create(centre, r, math.pi, 2.0 * math.pi, XYZ.BasisX, XYZ.BasisY), style)

    def cable(self, x, y, radius, style='cable'):
        """A main cable, drawn as its upper and lower half so that a dimension
        can be ATTACHED to it: the two ends of the upper half are exactly the
        left and the right edge of the cable. Returns (left edge, right edge),
        each a handle for dimension()."""
        centre = self._point(x, y)
        r = radius / MM_PER_FOOT
        upper = self._add(Arc.Create(centre, r, 0.0, math.pi, XYZ.BasisX, XYZ.BasisY), style)
        self._add(Arc.Create(centre, r, math.pi, 2.0 * math.pi, XYZ.BasisX, XYZ.BasisY), style)
        # the upper half runs from the right edge (its start, end 0) to the left edge (its end, end 1)
        return (upper, 1), (upper, 0)

    def text(self, x, y, text, align='left'):
        options = TextNoteOptions(self.text_type_id)
        options.HorizontalAlignment = {'center': HorizontalTextAlignment.Center,
                                       'right': HorizontalTextAlignment.Right}.get(align, HorizontalTextAlignment.Left)
        note = TextNote.Create(self.doc, self.view.Id, self._point(x, y), u'{0}'.format(text), options)
        self.ids.append(note.Id)


# ---------------------------------------------------------------------------
# one tray
# ---------------------------------------------------------------------------
def combined_view_name(text):
    """The name of a view that holds several trays, without the characters
    Revit refuses in names."""
    clean = u''.join(u'-' if ch in u'\\:{}[]|;<>?`~' else ch for ch in u'{0}'.format(text or u'')).strip()
    return clean or DEFAULT_COMBINED_NAME


def draw_tray(doc, layout, existing=UPDATE, scale=DEFAULT_SCALE, text_type_id=None, dimension_type_id=None):
    """Create or update the drafting view of ONE tray, 'Cable Tray - <id>'.
    Call inside a transaction. Returns (view or None, what happened)."""
    return draw_trays(doc, [layout], view_name(getattr(layout.tray, 'label', layout.tray.id)), existing, scale,
                      text_type_id, dimension_type_id)


def draw_trays(doc, layouts, wanted, existing=UPDATE, scale=DEFAULT_SCALE, text_type_id=None,
               dimension_type_id=None):
    """Create or update ONE drafting view named `wanted` that holds all the
    trays of `layouts`, one below the other. Call inside a transaction.

    scale: the view scale (50 = 1:50); None or 0 = chosen to suit the cables.
    text_type_id: the text type of the project to use; None = nearest to 2.5 mm.
    dimension_type_id: the dimension type of the project to use; None = Revit's default.
    Returns (view or None, what happened as a text)."""
    views = drafting_views(doc)
    view, action = None, 'created'
    name = wanted
    if wanted in views:
        if existing == SKIP:
            return None, 'skipped: the view "{0}" already exists'.format(wanted)
        old_ids = generated_ids(views[wanted])
        if existing == UPDATE and old_ids is not None:
            view, action = views[wanted], 'updated'
            for element_id in old_ids:
                element = doc.GetElement(element_id)
                # only what still belongs to this view; anything the user added stays
                if element is not None and id_value(element.OwnerViewId) == id_value(view.Id):
                    doc.Delete(element_id)
        else:
            name = _unique_name(views, wanted)
            action = 'created as "{0}"'.format(name)
            if existing == UPDATE:
                action += ' (a view named "{0}" exists but was not drawn by this tool, so it was left alone)'.format(wanted)
    # The text type is one that EXISTS in the project: the one chosen in the
    # window, else the one nearest to 2.5 mm. No text type is ever created.
    type_id, text_mm = None, None
    if text_type_id is not None:
        try:
            chosen = doc.GetElement(text_type_id)
            type_id, text_mm = chosen.Id, chosen.get_Parameter(BuiltInParameter.TEXT_SIZE).AsDouble() * MM_PER_FOOT
        except Exception:
            type_id = None
    if type_id is None:
        type_id, text_mm = text_type(doc)
    # the scale at which this text suits the cables of every tray
    natural = min(ctl_drawing.choose_scale(layout, text_mm) for layout in layouts)
    remark = ''
    if not scale:
        scale = natural
    elif scale > natural * 2:
        remark = (' (at 1:{0} the {1:.1f} mm text is {2:.0f} mm high in the model, large against the cables; '
                  'a smaller text style or a scale near 1:{3} gives a compact drawing)').format(
                      scale, text_mm, text_mm * scale, natural)
    if view is None:
        view = ViewDrafting.Create(doc, _drafting_type_id(doc))
        view.Name = name
    view.Scale = scale
    # from 1:20 on even "medium" lines are thicker than a small cable: thin lines only
    dimension_type = None
    if dimension_type_id is not None:
        try:
            dimension_type = doc.GetElement(dimension_type_id)
        except Exception:
            dimension_type = None
    canvas = RevitCanvas(doc, view, type_id, thin_only=scale >= 20, dimension_type=dimension_type)
    # the text height of the dimension style, in the model: the dimension rows are spaced by it
    try:
        style = dimension_type
        if style is None:
            style = doc.GetElement(doc.GetDefaultElementTypeId(ElementTypeGroup.LinearDimensionType))
        canvas.dimension_text = style.get_Parameter(BuiltInParameter.TEXT_SIZE).AsDouble() * MM_PER_FOOT * scale
    except Exception:
        canvas.dimension_text = None                      # unknown: the text style's height is used
    ctl_drawing.compose_stack(canvas, layouts, text_mm * scale)
    _remember(view, u' | '.join(u'{0}'.format(layout.tray.id) for layout in layouts), canvas.ids)
    dimensions = ', {0} dimension(s)'.format(canvas.dimension_count)
    if canvas.unattached:
        dimensions += (' - {0} dimension chain(s) could NOT be attached to the cables; short lines were drawn '
                       'for them instead (Revit: {1})').format(canvas.unattached, canvas.dimension_problems[0])
    elif canvas.dimension_problems:
        dimensions += (' - {0} dimension(s) could NOT be created and are drawn as lines and text instead '
                       '(Revit: {1})').format(len(canvas.dimension_problems), canvas.dimension_problems[0])
    return view, '{0}, scale 1:{1}, {2} elements{3}{4}'.format(action, scale, len(canvas.ids), dimensions, remark)


# ---------------------------------------------------------------------------
# the cable table, in a view of its own
# ---------------------------------------------------------------------------
DEFAULT_TABLE_NAME = 'Cable Tray Schedule'
DEFAULT_TABLE_SCALE = 50
PER_TRAY, ONE_TABLE = 'per_tray', 'one'
TABLE_LAYOUTS = [
    (PER_TRAY, 'A table for each tray'),
    (ONE_TABLE, 'One table for all trays'),
]
TABLE_SCALES = [(50, '1 : 50'), (1, '1 : 1'), (2, '1 : 2'), (5, '1 : 5'), (10, '1 : 10'), (20, '1 : 20'),
                (25, '1 : 25'), (100, '1 : 100'), (200, '1 : 200')]


def draw_table(doc, trays, wanted, existing=UPDATE, scale=DEFAULT_TABLE_SCALE, text_type_id=None, layout=None):
    """Create or update ONE drafting view named `wanted` that holds the cable
    table of these trays (CABLE TAG | FED FROM | FED TO | CABLE). Call inside
    a transaction. It has its OWN scale and text style and shares nothing
    with the tray views but the canvas.

    layout: PER_TRAY = a separate small table for each tray, one below the
    other (as under each tray drawing); ONE_TABLE = one table for all Tray
    IDs with the headings once. None = TABLE_LAYOUTS' first.

    scale: the view scale (50 = 1:50). text_type_id: a text type of the
    project; None = the one nearest to 2.5 mm. Returns (view or None, what happened)."""
    import ctl_table
    views = drafting_views(doc)
    view, action = None, 'created'
    name = wanted
    if wanted in views:
        if existing == SKIP:
            return None, 'skipped: the view "{0}" already exists'.format(wanted)
        old_ids = generated_ids(views[wanted])
        if existing == UPDATE and old_ids is not None:
            view, action = views[wanted], 'updated'
            for element_id in old_ids:
                element = doc.GetElement(element_id)
                # only what still belongs to this view; anything the user added stays
                if element is not None and id_value(element.OwnerViewId) == id_value(view.Id):
                    doc.Delete(element_id)
        else:
            name = _unique_name(views, wanted)
            action = 'created as "{0}"'.format(name)
            if existing == UPDATE:
                action += ' (a view named "{0}" exists but was not drawn by this tool, so it was left alone)'.format(wanted)
    type_id, text_mm = None, None
    if text_type_id is not None:
        try:
            chosen = doc.GetElement(text_type_id)
            type_id, text_mm = chosen.Id, chosen.get_Parameter(BuiltInParameter.TEXT_SIZE).AsDouble() * MM_PER_FOOT
        except Exception:
            type_id = None
    if type_id is None:
        type_id, text_mm = text_type(doc)
    scale = int(scale) if scale else DEFAULT_TABLE_SCALE
    if view is None:
        view = ViewDrafting.Create(doc, _drafting_type_id(doc))
        view.Name = name
    view.Scale = scale
    canvas = RevitCanvas(doc, view, type_id, thin_only=True)
    if layout == ONE_TABLE:
        ctl_table.compose_table(canvas, trays, text_mm * scale)
    else:
        ctl_table.compose_tables(canvas, trays, text_mm * scale)
    _remember(view, u'TABLE', canvas.ids)
    rows = sum(len(tray.rows) for tray in trays)
    return view, '{0}, scale 1:{1}, {2} tray(s), {3} cable row(s)'.format(action, scale, len(trays), rows)


def run_transaction(doc, name, action):
    """action() inside one transaction; rolled back when it raises."""
    transaction = Transaction(doc, name)
    transaction.Start()
    try:
        value = action()
        # Commit() does not raise when Revit rolls the change back - it says so
        if transaction.Commit() != TransactionStatus.Committed:
            raise DrawError('Revit rolled the change back; nothing was drawn')
        return value
    except Exception:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
        raise


def show_view(uidoc, view):
    """Open the view and fit the drawing in the window. Not inside a transaction."""
    try:
        uidoc.ActiveView = view
        for ui_view in uidoc.GetOpenUIViews():
            if id_value(ui_view.ViewId) == id_value(view.Id):
                ui_view.ZoomToFit()
    except Exception:
        pass

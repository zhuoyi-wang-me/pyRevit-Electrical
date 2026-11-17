# -*- coding: utf-8 -*-
"""Table Gen - draws an Excel table (tg_excel.Table) in a Revit drafting view.

Nothing new is added to the model's styles: the user picks, in the window,
    - the text type for the cell texts (and optionally another one for bold Excel cells),
    - the line styles for thin / medium / thick Excel borders,
    - optionally a filled region type for coloured Excel cells.

The view is drawn at 1:1, so the table has its real PAPER size on a sheet.

Speed: all text notes are created first, the view is regenerated ONCE, every text is measured,
(optionally) columns / rows are widened so the texts fit, and only then are the texts moved into
place and the borders drawn. Reading a text's size right after moving another one would make
Revit regenerate the view for every text - that was the old, slow way.

The link to the Excel file (path, worksheet, range, options, the elements drawn) is stored in the
view itself (extensible storage), so UPDATE re-reads the Excel and redraws the same view: the
viewports on sheets stay where they are.
"""
import json
import math
import datetime

import clr
from System import Guid, String, Int64
from System.Collections.Generic import List
from Autodesk.Revit.DB import (FilteredElementCollector, Transaction, ElementId, XYZ, Line, CurveLoop,
                               ViewFamilyType, ViewFamily, ViewDrafting, View, Viewport,
                               TextNote, TextNoteType, TextNoteOptions, HorizontalTextAlignment,
                               FilledRegion, FilledRegionType, GraphicsStyle, GraphicsStyleType,
                               BuiltInCategory, Element, ElementTransformUtils, ElementTypeGroup)
from Autodesk.Revit.DB.ExtensibleStorage import Schema, SchemaBuilder, Entity, AccessLevel

import tg_excel

MM = 1.0 / 304.8                       # mm -> Revit feet (the view is 1:1, so paper mm = model mm)
PAD_MM = 0.8                           # text distance from the left / right cell edge
VPAD_MM = 0.5                          # ... and from the top / bottom edge
DEFAULT_LINES = {1: [u'Thin Lines'], 2: [u'Medium Lines'], 3: [u'Wide Lines', u'Thick Lines']}
SCHEMA_GUID = Guid('5d3f2a71-8c4e-4b9a-a1f7-2e6c9b0d4e13')
SCHEMA_NAME = 'EleTools_TableGen'


# ============================================================================ small helpers
def _idv(element_id):
    try:
        return int(str(element_id.Value).rstrip('L'))
    except AttributeError:
        return element_id.IntegerValue


def _eid(number):
    try:
        return ElementId(Int64(number))
    except Exception:
        return ElementId(int(number))


def _name(element):
    try:
        return element.Name
    except Exception:
        try:
            return Element.Name.GetValue(element)
        except Exception:
            return u''


# ============================================================================ what the user can pick
def text_types(doc):
    """[(name, id number)] of every text type in the model, by name."""
    found = [(_name(t), _idv(t.Id)) for t in FilteredElementCollector(doc).OfClass(TextNoteType)]
    return sorted(found, key=lambda x: x[0].lower())


def default_text_type(doc):
    try:
        return _idv(doc.GetDefaultElementTypeId(ElementTypeGroup.TextNoteType))
    except Exception:
        types = text_types(doc)
        return types[0][1] if types else 0


def line_styles(doc):
    """[(name, GraphicsStyle id number)] of the model's line styles (Lines sub-categories)."""
    found = []
    try:
        parent = doc.Settings.Categories.get_Item(BuiltInCategory.OST_Lines)
        for sub in parent.SubCategories:
            try:
                style = sub.GetGraphicsStyle(GraphicsStyleType.Projection)
                if style is not None and not sub.Name.startswith(u'<'):
                    found.append((sub.Name, _idv(style.Id)))
            except Exception:
                pass
    except Exception:
        pass
    return sorted(found, key=lambda x: x[0].lower())


NOT_A_BORDER = ('centre', 'center', 'hidden', 'dash', 'dot', 'demol', 'overhead', 'beyond', 'invisible', 'phantom')
WEIGHT_WORDS = {1: ('thin',), 2: ('medium',), 3: ('wide', 'thick', 'heavy')}


def default_line_style(doc, weight):
    """Thin/Medium/Wide Lines; else a style whose name says thin/medium/wide; never a centre / hidden /
    dashed style (those would draw the table with broken lines)."""
    styles = line_styles(doc)
    for wanted in DEFAULT_LINES.get(weight, []):
        for name, number in styles:
            if name.lower() == wanted.lower():
                return number
    solid = [(n, i) for n, i in styles if not any(w in n.lower() for w in NOT_A_BORDER)]
    for name, number in solid:
        if any(w in name.lower() for w in WEIGHT_WORDS.get(weight, ())):
            return number
    # pen-numbered templates ("1 Pen", "3 Pen" ...): the solid style whose line weight is nearest
    weights = _line_weights(doc)
    target = {1: 1, 2: 3, 3: 5}.get(weight, 1)
    weighed = [(abs(weights[i] - target), len(n), i) for n, i in solid if weights.get(i)]
    if weighed:
        return min(weighed)[2]
    if solid:
        return solid[0][1]
    return styles[0][1] if styles else 0


def _line_weights(doc):
    """{GraphicsStyle id number: projection line weight} of the line styles."""
    found = {}
    try:
        parent = doc.Settings.Categories.get_Item(BuiltInCategory.OST_Lines)
        for sub in parent.SubCategories:
            try:
                style = sub.GetGraphicsStyle(GraphicsStyleType.Projection)
                found[_idv(style.Id)] = sub.GetLineWeight(GraphicsStyleType.Projection) or 0
            except Exception:
                pass
    except Exception:
        pass
    return found


def default_line_style_name(doc, weight):
    number = default_line_style(doc, weight)
    for name, n in line_styles(doc):
        if n == number:
            return name
    return None


def fill_types(doc):
    """[(name, id number)] of the filled region types."""
    found = [(_name(t), _idv(t.Id)) for t in FilteredElementCollector(doc).OfClass(FilledRegionType)]
    return sorted(found, key=lambda x: x[0].lower())


class Options(object):
    """What the window sets; stored with the table so UPDATE draws it the same way."""
    FIELDS = ('scale', 'borders', 'skip_hidden', 'fit', 'text_type', 'bold_type',
              'line_thin', 'line_medium', 'line_thick', 'fill_type')

    def __init__(self, **values):
        self.scale = 1.0                 # table size factor (1.0 = as in Excel at 100 % zoom)
        self.borders = 'excel'           # 'excel' | 'all' | 'none'
        self.skip_hidden = True
        self.fit = True                  # widen columns / rows so the texts fit
        self.text_type = 0               # TextNoteType id number (0 = the model's default type)
        self.bold_type = 0               # TextNoteType for bold Excel cells (0 = same as text_type)
        self.line_thin = 0               # GraphicsStyle id numbers for thin / medium / thick borders
        self.line_medium = 0
        self.line_thick = 0
        self.fill_type = 0               # FilledRegionType for coloured cells (0 = no fills)
        for key, value in values.items():
            if key in self.FIELDS:
                setattr(self, key, value)

    def as_dict(self):
        return dict((key, getattr(self, key)) for key in self.FIELDS)


class Result(object):
    def __init__(self, name):
        self.name = name
        self.view = None
        self.texts = self.lines = self.fills = 0
        self.rows = self.cols = self.merged = 0
        self.size = (0.0, 0.0)
        self.widened_cols = 0
        self.taller_rows = 0
        self.too_wide = []               # cell refs whose text is still wider than the cell
        self.too_tall = []               # cell refs whose text is still taller than the row
        self.failed = []                 # (cell ref / item, error)
        self.notes = []                  # e.g. a chosen style that no longer exists
        self.placed_on = u''
        self.error = u''
        self.seconds = 0.0


# ============================================================================ the link stored in the view
def _schema(create=False):
    schema = Schema.Lookup(SCHEMA_GUID)
    if schema is None and create:
        builder = SchemaBuilder(SCHEMA_GUID)
        builder.SetSchemaName(SCHEMA_NAME)
        builder.SetReadAccessLevel(AccessLevel.Public)
        builder.SetWriteAccessLevel(AccessLevel.Public)
        builder.AddSimpleField('Data', String)
        schema = builder.Finish()
    return schema


def read_link(view):
    schema = _schema()
    if schema is None or view is None:
        return None
    try:
        entity = view.GetEntity(schema)
        if entity is None or not entity.IsValid():
            return None
        return json.loads(entity.Get[String]('Data'))
    except Exception:
        return None


def write_link(view, link):
    entity = Entity(_schema(create=True))
    entity.Set[String]('Data', json.dumps(link))
    view.SetEntity(entity)


def find_tables(doc):
    """[(view, link)] for every drafting view made by Table Gen, by view name."""
    if _schema() is None:
        return []
    found = []
    for view in FilteredElementCollector(doc).OfClass(ViewDrafting):
        link = read_link(view)
        if link:
            found.append((view, link))
    return sorted(found, key=lambda item: _name(item[0]).lower())


def file_stamp(path):
    try:
        from System.IO import File
        return File.GetLastWriteTime(path).ToString('yyyy-MM-dd HH:mm:ss')
    except Exception:
        return u''


# ============================================================================ views
def unique_view_name(doc, wanted):
    names = set(_name(v).lower() for v in FilteredElementCollector(doc).OfClass(View))
    name, number = wanted, 2
    while name.lower() in names:
        name = u'{0} ({1})'.format(wanted, number)
        number += 1
    return name


def new_drafting_view(doc, name):
    family = [t for t in FilteredElementCollector(doc).OfClass(ViewFamilyType) if t.ViewFamily == ViewFamily.Drafting]
    if not family:
        raise tg_excel.TableError(u'This model has no drafting view type.')
    view = ViewDrafting.Create(doc, family[0].Id)
    view.Name = unique_view_name(doc, name)
    try:
        view.Scale = 1
    except Exception:
        pass
    return view


def place_on_sheet(doc, sheet, view):
    if not Viewport.CanAddViewToSheet(doc, sheet.Id, view.Id):
        return False
    outline = sheet.Outline
    centre = XYZ((outline.Min.U + outline.Max.U) / 2.0, (outline.Min.V + outline.Max.V) / 2.0, 0)
    Viewport.Create(doc, sheet.Id, view.Id, centre)
    return True


# ============================================================================ drawing
def _checked_type(doc, number, cls, fallback, result, label):
    """The element id of a chosen type / style, or the fallback when it no longer exists."""
    if number:
        element = doc.GetElement(_eid(number))
        if element is not None and isinstance(element, cls):
            return element.Id
        result.notes.append(u'the chosen {0} no longer exists - used the default'.format(label))
    return _eid(fallback) if fallback else None


def _rect_loop(x0, y0, x1, y1):
    loop = CurveLoop()
    points = [XYZ(x0 * MM, y0 * MM, 0), XYZ(x1 * MM, y0 * MM, 0), XYZ(x1 * MM, y1 * MM, 0), XYZ(x0 * MM, y1 * MM, 0)]
    for k in range(4):
        loop.Append(Line.CreateBound(points[k], points[(k + 1) % 4]))
    return loop


def _move(doc, note, dx, dy):
    """Move a text note by (dx, dy) mm - via its Coord (fast), else MoveElement."""
    if abs(dx) < 0.005 and abs(dy) < 0.005:
        return
    try:
        coord = note.Coord
        note.Coord = XYZ(coord.X + dx * MM, coord.Y + dy * MM, coord.Z)
    except Exception:
        ElementTransformUtils.MoveElement(doc, note.Id, XYZ(dx * MM, dy * MM, 0))


def draw(doc, view, table, options, result):
    """Draw the table into the view. Returns the ids of everything drawn. Runs inside a transaction."""
    s = float(options.scale or 1.0)
    ids = []
    pad, vpad = PAD_MM * s, VPAD_MM * s
    widths = [(table.xs[k + 1] - table.xs[k]) * s for k in range(len(table.xs) - 1)]
    heights = [(table.ys[k] - table.ys[k + 1]) * s for k in range(len(table.ys) - 1)]
    result.rows, result.cols, result.merged = len(heights), len(widths), table.merged

    def edges():
        xs, ys = [0.0], [0.0]
        for w in widths:
            xs.append(xs[-1] + w)
        for h in heights:
            ys.append(ys[-1] - h)
        return xs, ys

    text_type = _checked_type(doc, options.text_type, TextNoteType, default_text_type(doc), result, u'text type')
    bold_type = _checked_type(doc, options.bold_type, TextNoteType, 0, result, u'bold text type') or text_type

    # ---- 1. create every text at the top-left of its cell (no measuring yet)
    xs, ys = edges()
    notes = []
    for item in table.texts:
        i0, j0, i1, j1 = item.span
        indent = item.indent_mm * s
        type_id = bold_type if item.font.bold else text_type
        try:
            note_options = TextNoteOptions(type_id)
            text = item.text.replace(u'\n', u'\r')
            position = XYZ((xs[j0] + pad + indent) * MM, (ys[i0] - vpad) * MM, 0)
            if item.wrap:
                note_options.HorizontalAlignment = {'left': HorizontalTextAlignment.Left,
                                                    'right': HorizontalTextAlignment.Right}.get(
                                                        item.halign, HorizontalTextAlignment.Center)
                width = max(xs[j1 + 1] - xs[j0] - 2 * pad - indent, 1.0) * MM
                try:
                    width = max(width, TextNote.GetMinimumAllowedWidth(doc, type_id))
                    width = min(width, TextNote.GetMaximumAllowedWidth(doc, type_id))
                except Exception:
                    pass
                note = TextNote.Create(doc, view.Id, position, width, text, note_options)
            else:
                note_options.HorizontalAlignment = HorizontalTextAlignment.Left
                note = TextNote.Create(doc, view.Id, position, text, note_options)
            ids.append(note.Id)
            notes.append([note, item, None])
            result.texts += 1
        except Exception as error:
            result.failed.append((item.ref, u'{0}'.format(error)))

    # ---- 2. ONE regenerate, then measure every text (no change in between: no further regenerates)
    doc.Regenerate()
    for entry in notes:
        try:
            box = entry[0].get_BoundingBox(view)
        except Exception:
            box = None
        if box is not None:
            entry[2] = (box.Min.X / MM, box.Max.X / MM, box.Min.Y / MM, box.Max.Y / MM)

    # ---- 3. widen columns / rows so the texts fit (like Excel's AutoFit)
    if options.fit:
        old_widths, old_heights = list(widths), list(heights)
        for note, item, box in notes:
            if box is None or item.rotation in (90, 180):
                continue
            i0, j0, i1, j1 = item.span
            width, height = box[1] - box[0], box[3] - box[2]
            if not item.wrap:
                need = width + 2 * pad + item.indent_mm * s
                # empty neighbour cells the text may run over, as in Excel
                if item.halign == 'left':
                    extra = sum(widths[j1 + 1:item.reach_right])
                elif item.halign == 'right':
                    extra = sum(widths[item.reach_left:j0])
                else:
                    extra = 2 * min(sum(widths[j1 + 1:item.reach_right]), sum(widths[item.reach_left:j0]))
                have = sum(widths[j0:j1 + 1]) + extra
                if need > have + 0.05:
                    widths[j1] += need - have                     # the last column of the cell grows
            need_h = height + 2 * vpad
            have_h = sum(heights[i0:i1 + 1])
            if need_h > have_h + 0.05:
                heights[i1] += need_h - have_h
        result.widened_cols = len([k for k in range(len(widths)) if widths[k] > old_widths[k] + 0.01])
        result.taller_rows = len([k for k in range(len(heights)) if heights[k] > old_heights[k] + 0.01])
    xs, ys = edges()
    result.size = (xs[-1], -ys[-1])

    # ---- 4. move every text to its place in its (final) cell
    for note, item, box in notes:
        if box is None:
            continue
        i0, j0, i1, j1 = item.span
        x0, x1, yt, yb = xs[j0], xs[j1 + 1], ys[i0], ys[i1 + 1]
        min_x, max_x, min_y, max_y = box
        indent = item.indent_mm * s
        if item.rotation in (90, 180):
            dx = (x0 + x1) / 2.0 - (min_x + max_x) / 2.0
            dy = (yt + yb) / 2.0 - (min_y + max_y) / 2.0
        else:
            if item.halign == 'left':
                dx = (x0 + pad + indent) - min_x
            elif item.halign == 'right':
                dx = (x1 - pad) - max_x
            else:
                dx = (x0 + x1) / 2.0 - (min_x + max_x) / 2.0
            if item.valign == 'top':
                dy = (yt - vpad) - max_y
            elif item.valign == 'bottom':
                dy = (yb + vpad) - min_y
            else:
                dy = (yt + yb) / 2.0 - (min_y + max_y) / 2.0
        try:
            _move(doc, note, dx, dy)
        except Exception as error:
            result.failed.append((item.ref, u'move: {0}'.format(error)))
            continue
        if item.rotation in (90, 180):
            centre = XYZ((x0 + x1) / 2.0 * MM, (yt + yb) / 2.0 * MM, 0)
            try:
                ElementTransformUtils.RotateElement(doc, note.Id, Line.CreateBound(centre, centre + XYZ.BasisZ),
                                                    math.pi / 2 if item.rotation == 90 else -math.pi / 2)
            except Exception:
                pass
            continue
        # still too big? (fit switched off, or a merged cell)
        width, height = max_x - min_x, max_y - min_y
        room = x1 - x0 - 2 * pad - indent
        if not item.wrap:
            if item.halign == 'left':
                room += xs[item.reach_right] - x1
            elif item.halign == 'right':
                room += x0 - xs[item.reach_left]
            else:
                room += 2 * min(xs[item.reach_right] - x1, x0 - xs[item.reach_left])
        if width > room + 0.3:
            result.too_wide.append(item.ref)
        if height > (yt - yb) + 0.3:
            result.too_tall.append(item.ref)

    # ---- 5. fills (one chosen filled region type for every coloured cell)
    fill_id = _checked_type(doc, options.fill_type, FilledRegionType, 0, result, u'filled region type')
    if fill_id is not None and table.grid_fills:
        invisible = None
        wanted = int(BuiltInCategory.OST_InvisibleLines)
        for style in FilteredElementCollector(doc).OfClass(GraphicsStyle):
            category = style.GraphicsStyleCategory
            if category is not None and _idv(category.Id) == wanted:
                invisible = style.Id
                break
        for i0, j0, i1, j1, rgb in table.grid_fills:
            try:
                loops = List[CurveLoop]()
                loops.Add(_rect_loop(xs[j0], ys[i0], xs[j1 + 1], ys[i1 + 1]))
                region = FilledRegion.Create(doc, fill_id, view.Id, loops)
                if invisible is not None:
                    try:
                        region.SetLineStyleId(invisible)
                    except Exception:
                        pass
                ids.append(region.Id)
                result.fills += 1
            except Exception as error:
                result.failed.append((u'fill', u'{0}'.format(error)))

    # ---- 6. borders, with the chosen line styles
    styles = {}
    for weight, number in ((1, options.line_thin), (2, options.line_medium), (3, options.line_thick)):
        style_id = _checked_type(doc, number, GraphicsStyle, default_line_style(doc, weight), result,
                                 {1: u'thin', 2: u'medium', 3: u'thick'}[weight] + u' line style')
        styles[weight] = doc.GetElement(style_id) if style_id is not None else None
    if options.borders == 'excel' and len(table.grid_lines) < max(4, len(heights) // 2):
        result.notes.append(u'the Excel sheet has (almost) no cell borders - the grid you see in Excel is only '
                            u'its screen grid. For a ruled table choose Borders: "Every cell" and UPDATE.')
    short = doc.Application.ShortCurveTolerance / MM
    for kind, fixed, start, end, weight in table.grid_lines:
        if kind == 'h':
            a, b = XYZ(xs[start] * MM, ys[fixed] * MM, 0), XYZ(xs[end] * MM, ys[fixed] * MM, 0)
            length = xs[end] - xs[start]
        else:
            a, b = XYZ(xs[fixed] * MM, ys[start] * MM, 0), XYZ(xs[fixed] * MM, ys[end] * MM, 0)
            length = ys[start] - ys[end]
        if abs(length) < short:
            continue
        try:
            curve = doc.Create.NewDetailCurve(view, Line.CreateBound(a, b))
            if styles.get(weight) is not None:
                curve.LineStyle = styles[weight]
            ids.append(curve.Id)
            result.lines += 1
        except Exception as error:
            result.failed.append((u'line', u'{0}'.format(error)))
    return [_idv(i) for i in ids]


# ============================================================================ create / update
def _link(path, sheet, range_value, options, ids):
    return {'path': path, 'sheet': sheet, 'range': range_value, 'options': options.as_dict(), 'ids': ids,
            'file_time': file_stamp(path), 'updated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}


def create_table(doc, workbook, sheet_name, range_value, view_name, options, sheet=None):
    result = Result(view_name)
    started = datetime.datetime.now()
    try:
        table = tg_excel.build_table(workbook, sheet_name, range_value, options.skip_hidden, options.borders)
    except tg_excel.TableError as error:
        result.error = u'{0}'.format(error)
        return result
    transaction = Transaction(doc, u'Table Gen - create table')
    transaction.Start()
    try:
        view = new_drafting_view(doc, view_name)
        result.name = _name(view)
        ids = draw(doc, view, table, options, result)
        write_link(view, _link(workbook.path, sheet_name, range_value, options, ids))
        if sheet is not None:
            if place_on_sheet(doc, sheet, view):
                result.placed_on = u'{0} - {1}'.format(sheet.SheetNumber, sheet.Name)
        transaction.Commit()
        result.view = view
    except Exception as error:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
        result.error = u'Nothing was created: {0}'.format(error)
    result.seconds = (datetime.datetime.now() - started).total_seconds()
    return result


def update_table(doc, view, new_path=None, new_options=None):
    """Re-read the Excel of a Table Gen view and redraw it (the view and its viewports stay).
    new_options (a dict) replaces stored settings, e.g. the text type chosen in the window."""
    result = Result(_name(view))
    started = datetime.datetime.now()
    link = read_link(view)
    if not link:
        result.error = u'This view was not made by Table Gen.'
        return result
    path = new_path or link.get('path')
    stored = dict(link.get('options') or {})
    if new_options:
        stored.update(new_options)
    options = Options(**stored)
    try:
        workbook = tg_excel.Workbook(path)
        sheet_name = link.get('sheet')
        if sheet_name not in [s.name for s in workbook.sheets]:
            if len(workbook.sheets) == 1 and workbook.path.lower().endswith('.csv'):
                sheet_name = workbook.sheets[0].name
            else:
                raise tg_excel.TableError(u'Worksheet "{0}" is not in {1}.'.format(sheet_name, path))
        table = tg_excel.build_table(workbook, sheet_name, link.get('range') or u'', options.skip_hidden,
                                     options.borders)
    except tg_excel.TableError as error:
        result.error = u'{0}'.format(error)
        return result
    transaction = Transaction(doc, u'Table Gen - update table')
    transaction.Start()
    try:
        old = List[ElementId]()
        for number in link.get('ids') or []:
            element = doc.GetElement(_eid(number))
            if element is not None and element.OwnerViewId == view.Id:
                old.Add(element.Id)
        if old.Count:
            doc.Delete(old)
        ids = draw(doc, view, table, options, result)
        write_link(view, _link(path, sheet_name, link.get('range') or u'', options, ids))
        transaction.Commit()
        result.view = view
    except Exception as error:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
        result.error = u'Nothing was changed: {0}'.format(error)
    result.seconds = (datetime.datetime.now() - started).total_seconds()
    return result


def report(results):
    out = []
    for r in results:
        out.append(u'=' * 60)
        out.append(u'Table view: {0}'.format(r.name))
        if r.error:
            out.append(u'  NOT DONE: {0}'.format(r.error))
            continue
        out.append(u'  {0} rows x {1} columns, {2} merged area(s) - {3:.0f} x {4:.0f} mm on the sheet - {5:.1f} s'
                   .format(r.rows, r.cols, r.merged, r.size[0], r.size[1], r.seconds))
        out.append(u'  drawn: {0} texts, {1} lines, {2} fills'.format(r.texts, r.lines, r.fills))
        if r.widened_cols or r.taller_rows:
            out.append(u'  to fit the texts: {0} column(s) made wider, {1} row(s) made taller'
                       .format(r.widened_cols, r.taller_rows))
        if r.placed_on:
            out.append(u'  placed on sheet {0} (centre - move it where you want)'.format(r.placed_on))
        for note in r.notes:
            out.append(u'  note: ' + note)
        if r.too_wide:
            out.append(u'  {0} text(s) wider than their cell - tick "Widen columns / rows to fit text", '
                       u'or choose a smaller text type: {1}'.format(len(r.too_wide), u', '.join(r.too_wide[:30])))
        if r.too_tall:
            out.append(u'  {0} text(s) taller than their row: {1}'
                       .format(len(r.too_tall), u', '.join(r.too_tall[:30])))
        if r.failed:
            out.append(u'  {0} item(s) could not be drawn:'.format(len(r.failed)))
            out += [u'    {0}: {1}'.format(a, b) for a, b in r.failed[:20]]
    return u'\n'.join(out)

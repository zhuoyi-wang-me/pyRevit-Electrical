# -*- coding: utf-8 -*-
"""Place Views on Sheet.

Pick a sheet, tick several views, and they are placed on the sheet in a clean grid:
row by row from the top-left, never overlapping, at least the chosen spacing apart
(50 mm by default), the whole block centred on the sheet. Views that do not fit go
on additional sheets (same title block) or are reported - the user chooses.

MODIFIES THE MODEL: creates viewports (and, if asked, sheets) in ONE transaction -
one Undo removes everything this tool did.

Revit 2022 - 2025 (2026 too), IronPython 2.7 or CPython engine.
Shift+click the button for the settings (spacing, sheet margins, order).

    script.py     this file - selecting, measuring, placing (everything that touches Revit)
    pvs_layout.py the packing algorithm (no Revit)
    config.py     the settings dialog (Shift+click)
"""
from pyrevit import revit, DB, forms, script

import pvs_layout

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()
logger = script.get_logger()

TITLE = 'Place Views on Sheet'
MM = 1.0 / 304.8                       # one millimetre in feet (Revit's internal length unit)

# ---------------------------------------------------------------------------
# settings - stored by pyRevit per user, changed with Shift+click (config.py)
# ---------------------------------------------------------------------------
DEFAULTS = {
    'spacing_mm': 50.0,                # minimum gap between viewports (titles included)
    'margin_left_mm': 20.0,            # kept free inside the title block border
    'margin_right_mm': 20.0,           #   (raise the one on the title-strip side, e.g. 190)
    'margin_top_mm': 20.0,
    'margin_bottom_mm': 20.0,
    'tallest_first': True,             # True: tall views first (tighter rows); False: as listed
}


def setting(name):
    cfg = script.get_config()
    value = cfg.get_option(name, DEFAULTS[name])
    if isinstance(DEFAULTS[name], bool):
        if isinstance(value, bool):
            return value
        return u'{0}'.format(value).strip().lower() in (u'true', u'1', u'yes')   # stored as text: "False" must be False
    try:
        return float(value)
    except (TypeError, ValueError):
        return DEFAULTS[name]


# ---------------------------------------------------------------------------
# version-safe helpers
# ---------------------------------------------------------------------------
def id_value(element_id):
    """ElementId.Value (2024+) or ElementId.IntegerValue (up to 2025, removed in 2026)."""
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue


def outline_box(outline):
    """Revit Outline -> (x low, y low, x high, y high), or None when it is empty."""
    if outline is None:
        return None
    try:
        if outline.IsEmpty:
            return None
    except Exception:
        pass
    low, high = outline.MinimumPoint, outline.MaximumPoint
    if high.X - low.X <= 1e-9 and high.Y - low.Y <= 1e-9:
        return None
    return (low.X, low.Y, high.X, high.Y)


def mm(feet):
    return feet / MM


# ---------------------------------------------------------------------------
# which views can go on a sheet
# ---------------------------------------------------------------------------
PLACEABLE = [
    (DB.ViewType.FloorPlan, 'Floor Plans'),
    (DB.ViewType.CeilingPlan, 'Ceiling Plans'),
    (DB.ViewType.EngineeringPlan, 'Structural Plans'),
    (DB.ViewType.AreaPlan, 'Area Plans'),
    (DB.ViewType.Section, 'Sections'),
    (DB.ViewType.Elevation, 'Elevations'),
    (DB.ViewType.Detail, 'Detail Views'),
    (DB.ViewType.DraftingView, 'Drafting Views'),
    (DB.ViewType.Legend, 'Legends'),
    (DB.ViewType.ThreeD, '3D Views'),
]
# keyed by the enum NAME: int(enum) is not reliable on the CPython (pythonnet 3) engine
GROUP_OF = dict((str(kind), label) for kind, label in PLACEABLE)


def placed_on_sheets():
    """{view id value: [sheet]} of every view that is on a sheet already."""
    found = {}
    for viewport in DB.FilteredElementCollector(doc).OfClass(DB.Viewport):
        try:
            sheet = doc.GetElement(viewport.SheetId)
            found.setdefault(id_value(viewport.ViewId), []).append(sheet)
        except Exception:
            pass
    return found


def candidate_views():
    """Views of the supported types that are not templates, grouped for the picker."""
    views = []
    for view in DB.FilteredElementCollector(doc).OfClass(DB.View):
        try:
            if view.IsTemplate or str(view.ViewType) not in GROUP_OF:
                continue
            views.append(view)
        except Exception:
            continue
    return sorted(views, key=lambda v: (GROUP_OF[str(v.ViewType)], v.Name.lower()))


def sheet_label(sheet):
    return u'{0} - {1}'.format(sheet.SheetNumber, sheet.Name)


class ViewItem(forms.TemplateListItem):
    """One row of the view picker: the name, and where the view is already placed."""

    def __init__(self, view, on_sheets, checked=False):
        forms.TemplateListItem.__init__(self, view, checked=checked)
        self.on_sheets = on_sheets

    @property
    def name(self):
        text = self.item.Name
        if self.on_sheets:
            text += u'   [on sheet {0}]'.format(u', '.join(s.SheetNumber for s in self.on_sheets if s))
        return text


def unwrap(item):
    try:
        return item.unwrap()
    except AttributeError:
        return getattr(item, 'item', item)


# ---------------------------------------------------------------------------
# the sheet: where views may go, and what is on it already
# ---------------------------------------------------------------------------
A1 = (0.0, 0.0, 841.0 * MM, 594.0 * MM)


def title_block(sheet):
    blocks = list(DB.FilteredElementCollector(doc, sheet.Id)
                  .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
                  .WhereElementIsNotElementType())
    return blocks[0] if blocks else None


def usable_area(sheet):
    """(x low, y low, x high, y high) inside the title block, less the margins. Also says where it
    came from, for the report."""
    block = title_block(sheet)
    box, source = None, None
    if block is not None:
        bb = block.get_BoundingBox(sheet)
        if bb is not None:
            box, source = (bb.Min.X, bb.Min.Y, bb.Max.X, bb.Max.Y), u'title block "{0}"'.format(block.Name)
    if box is None:
        try:
            outline = sheet.Outline                                 # BoundingBoxUV of the sheet
            if outline.Max.U - outline.Min.U > 0.5 and outline.Max.V - outline.Min.V > 0.5:
                box, source = (outline.Min.U, outline.Min.V, outline.Max.U, outline.Max.V), u'sheet outline (no title block)'
        except Exception:
            pass
    if box is None:
        box, source = A1, u'A1 841 x 594 mm (no title block on the sheet)'
    area = (box[0] + setting('margin_left_mm') * MM, box[1] + setting('margin_bottom_mm') * MM,
            box[2] - setting('margin_right_mm') * MM, box[3] - setting('margin_top_mm') * MM)
    if area[2] - area[0] <= 0 or area[3] - area[1] <= 0:
        raise ValueError(u'The sheet margins are larger than the sheet ({0}). Shift+click the button to change them.'.format(source))
    return area, source


def sheet_obstacles(sheet):
    """Boxes of what is on the sheet already: viewports (with their titles) and schedules."""
    boxes = []
    for viewport in DB.FilteredElementCollector(doc, sheet.Id).OfClass(DB.Viewport):
        box = viewport_extent(viewport)
        if box is not None:
            boxes.append(box)
    for schedule in DB.FilteredElementCollector(doc, sheet.Id).OfClass(DB.ScheduleSheetInstance):
        try:
            if schedule.IsTitleblockRevisionSchedule:
                continue                                            # part of the title block
        except Exception:
            pass
        bb = schedule.get_BoundingBox(sheet)
        if bb is not None:
            boxes.append((bb.Min.X, bb.Min.Y, bb.Max.X, bb.Max.Y))
    return boxes


# ---------------------------------------------------------------------------
# viewports: their REAL extent (box + view title)
# ---------------------------------------------------------------------------
def viewport_extent(viewport):
    """The outline of the viewport box together with its title (label), on the sheet.
    GetLabelOutline is Revit 2022+; a hidden title gives an empty outline and is ignored."""
    box = outline_box(viewport.GetBoxOutline())
    label = None
    try:
        label = outline_box(viewport.GetLabelOutline())
    except Exception:
        label = None
    return pvs_layout.union([box, label])


class Measured(object):
    """One view after its viewport was made for measuring."""

    def __init__(self, view, viewport):
        self.view = view
        self.viewport = viewport
        self.extent = viewport_extent(viewport)
        if self.extent is None:
            raise ValueError(u'Revit gave the viewport no outline')
        centre = viewport.GetBoxCenter()
        self.box_centre = (centre.X, centre.Y)
        # where the box centre sits relative to the lower-left of the full extent: placing the
        # extent at a slot then means putting the box centre at slot corner + this offset
        self.offset = (centre.X - self.extent[0], centre.Y - self.extent[1])
        self.w = self.extent[2] - self.extent[0]
        self.h = self.extent[3] - self.extent[1]

    @property
    def key(self):
        return id_value(self.view.Id)

    def centre_for(self, slot):
        return DB.XYZ(slot[0] + self.offset[0], slot[1] + self.offset[1], 0.0)


# ---------------------------------------------------------------------------
# 1. choosing the sheet and the views
# ---------------------------------------------------------------------------
def choose_sheet():
    active = uidoc.ActiveView
    if isinstance(active, DB.ViewSheet):
        choice = forms.alert(u'Place the views on the open sheet\n\n{0}?'.format(sheet_label(active)),
                             title=TITLE, options=['Use this sheet', 'Choose another sheet'])
        if choice == 'Use this sheet':
            return active
        if choice is None:
            return None
    return forms.select_sheets(title=u'{0} - choose the sheet'.format(TITLE), multiple=False,
                               button_name='Use this sheet')


def choose_views(sheet):
    on = placed_on_sheets()
    preselected = set()
    try:
        for element_id in uidoc.Selection.GetElementIds():       # views ticked in the Project Browser
            preselected.add(id_value(element_id))
    except Exception:
        pass
    groups = {}
    for view in candidate_views():
        groups.setdefault(GROUP_OF[str(view.ViewType)], []).append(
            ViewItem(view, on.get(id_value(view.Id), []), checked=id_value(view.Id) in preselected))
    if not groups:
        forms.alert(u'This model has no views that can be placed on a sheet.', title=TITLE)
        return None
    groups[u'All views'] = [item for name in sorted(groups) for item in groups[name]]
    picked = forms.SelectFromList.show(
        groups, title=u'{0} - views for {1}'.format(TITLE, sheet_label(sheet)),
        group_selector_title=u'View type:', multiselect=True, button_name=u'Place on sheet',
        width=640, height=720, default_group=u'All views')
    if not picked:
        return None
    seen, views = set(), []
    for item in picked:
        view = unwrap(item)
        if id_value(view.Id) not in seen:                         # a view is in "All views" and its own group
            seen.add(id_value(view.Id))
            views.append(view)
    return views, on


def ask_spacing():
    current = setting('spacing_mm')
    text = forms.ask_for_string(default=u'{0:g}'.format(current), title=TITLE,
                                prompt=u'Minimum spacing between viewports, in mm (titles included):')
    if text is None:
        return None
    try:
        value = float(text.strip().replace(u',', u'.'))
    except ValueError:
        forms.alert(u'"{0}" is not a number.'.format(text), title=TITLE)
        return None
    if value < 0:
        forms.alert(u'The spacing cannot be negative.', title=TITLE)
        return None
    cfg = script.get_config()
    cfg.spacing_mm = value
    script.save_config()
    return value


# ---------------------------------------------------------------------------
# 2. checking each view before anything is created
# ---------------------------------------------------------------------------
def precheck(views, sheet, on):
    """(placeable views, [(view name, reason)])"""
    ok, skipped = [], []
    for view in views:
        sheets = [s for s in on.get(id_value(view.Id), []) if s is not None]
        is_legend = view.ViewType == DB.ViewType.Legend
        if sheets and not is_legend:
            where = u', '.join(sheet_label(s) for s in sheets)
            same = any(id_value(s.Id) == id_value(sheet.Id) for s in sheets)
            skipped.append((view.Name, u'already on {0}'.format(u'this sheet' if same else u'sheet ' + where)))
            continue
        if is_legend and any(id_value(s.Id) == id_value(sheet.Id) for s in sheets):
            skipped.append((view.Name, u'this legend is on this sheet already'))
            continue
        try:
            allowed = DB.Viewport.CanAddViewToSheet(doc, sheet.Id, view.Id)
        except Exception as error:
            allowed = False
            logger.debug(error)
        if not allowed:
            skipped.append((view.Name, u'Revit does not allow this view on a sheet (CanAddViewToSheet is False)'))
            continue
        ok.append(view)
    return ok, skipped


# ---------------------------------------------------------------------------
# 3. measuring, packing and placing - one transaction
# ---------------------------------------------------------------------------
class ModelChange(object):
    """One Revit transaction: committed when the block ends normally, rolled back on ANY error,
    and the error is always passed on - so a rolled-back run can never be reported as placed."""

    def __init__(self, name):
        self.transaction = DB.Transaction(doc, name)

    def __enter__(self):
        self.transaction.Start()
        return self

    def __exit__(self, kind, value, trace):
        if kind is None:
            status = self.transaction.Commit()
            if status != DB.TransactionStatus.Committed:
                raise RuntimeError(u'Revit did not keep the change (transaction status: {0}).'.format(status))
        else:
            try:
                if self.transaction.HasStarted() and not self.transaction.HasEnded():
                    self.transaction.RollBack()
            except Exception:
                pass
        return False



def new_sheet_like(sheet, number):
    """An empty sheet with the same title block, named after the first one."""
    block = title_block(sheet)
    type_id = block.GetTypeId() if block is not None else DB.ElementId.InvalidElementId
    new = DB.ViewSheet.Create(doc, type_id)
    try:
        new.Name = u'{0} ({1})'.format(sheet.Name, number)
    except Exception:
        pass
    try:
        new.SheetNumber = u'{0}-{1}'.format(sheet.SheetNumber, number)
    except Exception:
        pass                                                      # number taken: keep the one Revit chose
    return new


def place(sheet, views, spacing_mm, report):
    spacing = spacing_mm * MM
    tallest_first = setting('tallest_first')
    area, source = usable_area(sheet)
    report['area'] = (source, mm(area[2] - area[0]), mm(area[3] - area[1]))
    existing = sheet_obstacles(sheet)
    report['obstacles'] = len(existing)
    middle = DB.XYZ((area[0] + area[2]) / 2.0, (area[1] + area[3]) / 2.0, 0.0)

    with ModelChange(u'Place views on sheet'):
        # -- measure: make every viewport in the middle of the sheet, let Revit draw it, read its extent
        measured = []
        for view in views:
            try:
                viewport = DB.Viewport.Create(doc, sheet.Id, view.Id, middle)
            except Exception as error:
                text = u'{0}'.format(error).splitlines()[0] if u'{0}'.format(error) else u'unknown error'
                report['skipped'].append((view.Name, u'Revit refused to place it ({0}) - an empty view cannot go on a sheet'.format(text)))
                continue
            if viewport is None:
                report['skipped'].append((view.Name, u'Revit did not create a viewport'))
                continue
            measured.append((view, viewport))
        doc.Regenerate()                                           # viewports get their real size and title
        pieces = []
        for view, viewport in measured:
            try:
                piece = Measured(view, viewport)
                if piece.extent is None:
                    raise ValueError(u'the viewport has no extent')
                pieces.append(piece)
            except Exception as error:
                doc.Delete(viewport.Id)
                report['skipped'].append((view.Name, u'its size could not be read ({0})'.format(error)))
        by_key = dict((piece.key, piece) for piece in pieces)

        # -- pack on the chosen sheet (keeping clear of what was on it before)
        sheet_number = 1
        target, target_area, obstacles = sheet, area, existing
        waiting = [(p.key, p.w, p.h) for p in pieces]
        ask_more_sheets = None
        while waiting:
            result = pvs_layout.pack(waiting, target_area, spacing, obstacles, tallest_first=tallest_first)
            problems = pvs_layout.check(result, obstacles, spacing, target_area)
            if problems:                                            # never expected - but never overlap
                raise RuntimeError(u'Layout check failed: {0}'.format(problems[:3]))
            for key in result.too_big:
                piece = by_key[key]
                doc.Delete(piece.viewport.Id)
                report['skipped'].append((piece.view.Name, u'too large for the sheet: {0:.0f} x {1:.0f} mm, usable area {2:.0f} x {3:.0f} mm '
                                          u'(reduce the crop region or the view scale)'.format(
                                              mm(piece.w), mm(piece.h), mm(target_area[2] - target_area[0]), mm(target_area[3] - target_area[1]))))
            for key in result.order:
                piece = by_key[key]
                slot = result.placed[key]
                if target is not sheet:
                    # a viewport cannot move to another sheet: delete it and make it again there
                    doc.Delete(piece.viewport.Id)
                    piece.viewport = DB.Viewport.Create(doc, target.Id, piece.view.Id, piece.centre_for(slot))
                piece.viewport.SetBoxCenter(piece.centre_for(slot))
                report['placed'].append((piece.view.Name, target, mm(piece.w), mm(piece.h)))
            report['centred'].append((target, result.centred))
            if not result.left_over:
                break
            if not result.order and target is not sheet:
                # an EMPTY extra sheet took nothing: these views can never fit - stop instead of looping
                for key in result.left_over:
                    piece = by_key[key]
                    doc.Delete(piece.viewport.Id)
                    report['skipped'].append((piece.view.Name, u'does not fit on an empty sheet of this size'))
                break
            if ask_more_sheets is None:
                names = u'\n'.join(u'  - ' + by_key[k].view.Name for k in result.left_over[:15])
                more = u'\n  ... and {0} more'.format(len(result.left_over) - 15) if len(result.left_over) > 15 else u''
                choice = forms.alert(u'{0} view(s) do not fit on {1}:\n\n{2}{3}\n\nPut them on new sheets with the same title block?'
                                     .format(len(result.left_over), sheet_label(target), names, more),
                                     title=TITLE, options=[u'Create new sheets', u'Leave them off'])
                ask_more_sheets = choice == u'Create new sheets'
            if not ask_more_sheets:
                for key in result.left_over:
                    piece = by_key[key]
                    doc.Delete(piece.viewport.Id)
                    report['skipped'].append((piece.view.Name, u'did not fit on {0} (you chose not to create new sheets)'.format(sheet_label(sheet))))
                break
            sheet_number += 1
            target = new_sheet_like(sheet, sheet_number)
            doc.Regenerate()                                       # the new title block gets its size
            target_area, _source = usable_area(target)
            obstacles = []
            report['new_sheets'].append(target)
            waiting = [(key, by_key[key].w, by_key[key].h) for key in result.left_over]
    return report


# ---------------------------------------------------------------------------
# 4. the report
# ---------------------------------------------------------------------------
def show_report(report, spacing_mm):
    output.print_md(u'## {0}'.format(TITLE))
    if report.get('area'):
        source, w, h = report['area']
        output.print_md(u'Usable area **{0:.0f} x {1:.0f} mm** ({2}, less the margins) - spacing **{3:g} mm** - '
                        u'{4} item(s) were on the sheet already and were kept clear of.'.format(w, h, source, spacing_mm, report.get('obstacles', 0)))
    if report['placed']:
        rows = [[name, u'{0} {1}'.format(output.linkify(target.Id), sheet_label(target)), u'{0:.0f} x {1:.0f}'.format(w, h)]
                for name, target, w, h in report['placed']]
        output.print_table(rows, columns=[u'View', u'Sheet', u'Size on sheet (mm)'], title=u'Placed: {0}'.format(len(rows)))
    for target, mode in report['centred']:
        if mode != 'both':
            output.print_md(u'Sheet {0}: the layout is {1} - centring would have run into something on the sheet.'.format(
                target.SheetNumber, {'horizontal': u'centred left-right only', 'vertical': u'centred up-down only',
                                     'no': u'left at the top-left'}.get(mode, mode)))
    if report['new_sheets']:
        output.print_md(u'**New sheets:** ' + u', '.join(u'{0} {1}'.format(output.linkify(s.Id), sheet_label(s)) for s in report['new_sheets']))
    if report['skipped']:
        output.print_table([[name, reason] for name, reason in report['skipped']], columns=[u'View', u'Not placed because'],
                           title=u'Skipped: {0}'.format(len(report['skipped'])))
    lines = [u'{0} view(s) placed.'.format(len(report['placed']))]
    if report['new_sheets']:
        lines.append(u'{0} new sheet(s) created.'.format(len(report['new_sheets'])))
    if report['skipped']:
        lines.append(u'{0} view(s) skipped - the output window lists why.'.format(len(report['skipped'])))
    if report['placed']:
        lines.append(u'\nOne Undo (Ctrl+Z) removes everything this tool did.')
    return u'\n'.join(lines)


def main():
    if doc.IsFamilyDocument:
        forms.alert(u'Open a project, not a family.', title=TITLE)
        return
    sheet = choose_sheet()
    if sheet is None:
        return
    chosen = choose_views(sheet)
    if not chosen:
        return
    views, on = chosen
    spacing_mm = ask_spacing()
    if spacing_mm is None:
        return
    report = {'placed': [], 'skipped': [], 'new_sheets': [], 'centred': []}
    ok, skipped = precheck(views, sheet, on)
    report['skipped'].extend(skipped)
    if not ok:
        forms.alert(show_report(report, spacing_mm) + u'\n\nNothing could be placed.', title=TITLE)
        return
    try:
        place(sheet, ok, spacing_mm, report)
    except Exception as error:
        # ModelChange rolled the transaction back: nothing of this run is left in the model
        logger.error(u'{0}'.format(error))
        forms.alert(u'Nothing was placed - the change was undone.\n\n{0}'.format(error), title=TITLE)
        return
    message = show_report(report, spacing_mm)
    try:
        uidoc.ActiveView = sheet                                   # show the result
    except Exception:
        pass
    forms.alert(message, title=TITLE)


main()

# -*- coding: utf-8 -*-
"""CBS SLD - everything that talks to Revit.

    read      CBS panels and their circuits (as the panel schedule has them)
    draw      the shapes of cbs_layout as detail lines and text notes in a
              drafting view; every element is tagged with the panel it shows,
              so "Redraw" can replace exactly the tool's own lines and text
"""
import re

import clr
import System

from Autodesk.Revit.DB import (BuiltInCategory, BuiltInParameter, CurveElement, Element, ElementId,
                               ElementTypeGroup, FilteredElementCollector, GraphicsStyleType,
                               HorizontalTextAlignment, Line, TextNote, TextNoteOptions, TextNoteType,
                               VerticalTextAlignment, ViewDrafting, ViewFamily, ViewFamilyType, XYZ)
from Autodesk.Revit.DB.ExtensibleStorage import AccessLevel, Entity, Schema, SchemaBuilder

MM = 1.0 / 304.8

SPECIAL_LINE_STYLES = ('<Room Separation>', '<Area Boundary>', '<Space Separation>', '<Sketch>',
                       '<Insulation Batting Lines>', '<Fabric Envelope>', '<Fabric Sheets>',
                       '<Path of Travel Lines>', '<Axis of Rotation>', '<Load Area Separation>',
                       '<Invisible lines>', '<Lines>')


def idv(element_id):
    try:
        return int(element_id.Value)
    except AttributeError:
        return int(element_id.IntegerValue)


def make_id(number):
    try:
        return ElementId(System.Int64(number))
    except Exception:
        return ElementId(int(number))


def name_of(element):
    try:
        return element.Name
    except Exception:
        try:
            return Element.Name.GetValue(element)
        except Exception:
            return u''


def _text(element, *names):
    for name in names:
        bip = getattr(BuiltInParameter, name, None)
        if bip is None:
            continue
        try:
            p = element.get_Parameter(bip)
            value = (p.AsString() or p.AsValueString() or u'').strip() if p is not None else u''
            if value:
                return value
        except Exception:
            pass
    return u''


# ============================================================================ panels + circuits
def panel_name(panel):
    return _text(panel, 'RBS_ELEC_PANEL_NAME', 'ALL_MODEL_MARK') or name_of(panel) or u'id {0}'.format(idv(panel.Id))


def panels(doc):
    """[(name, element)] of all electrical equipment, by name."""
    found = []
    for element in (FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_ElectricalEquipment)
                    .WhereElementIsNotElementType()):
        found.append((panel_name(element), element))
    return sorted(found, key=lambda item: natural_key(item[0]))


def natural_key(text):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', text or u'')]


def _circuits(panel):
    try:
        return list(panel.MEPModel.GetAssignedElectricalSystems() or [])
    except Exception:
        pass
    from Autodesk.Revit.DB.Electrical import ElectricalSystem
    found = []
    for system in FilteredElementCollector(panel.Document).OfClass(ElectricalSystem):
        try:
            if system.BaseEquipment is not None and system.BaseEquipment.Id == panel.Id:
                found.append(system)
        except Exception:
            pass
    return found


def supply_of(panel):
    """The name of the panel this one is fed from ('' when not connected)."""
    value = _text(panel, 'RBS_ELEC_PANEL_SUPPLY_FROM_PARAM')
    if value:
        return value
    try:
        for system in panel.MEPModel.GetElectricalSystems():
            base = system.BaseEquipment
            if base is not None and base.Id != panel.Id:
                return panel_name(base)
    except Exception:
        pass
    return u''


def _is_spare(system):
    try:
        return str(system.CircuitType) in ('Spare', 'Space')
    except Exception:
        return False


def classify(element, kinds):
    """The key of the kind an element is: the first kind with a keyword found in its family / type name,
    else the kind without keywords (the default)."""
    words = u' '.join([_text(element, 'ELEM_FAMILY_PARAM'), _text(element, 'ELEM_TYPE_PARAM'),
                       name_of(element)]).upper()
    default = None
    for key, _label, _module, keywords in kinds:
        if not keywords:
            default = default or key
            continue
        if [1 for word in keywords if word and word.upper() in words]:
            return key
    return default or kinds[-1][0]


_LEVELS = {}
# a circuit with fittings on several levels that include the panel's own level is drawn AT the panel
# (False: on the level with most of its fittings)
MIXED_AT_PANEL = True


def _level_below(element):
    doc = element.Document
    z = None
    try:
        z = element.Location.Point.Z
    except Exception:
        try:
            box = element.get_BoundingBox(None)
            z = (box.Min.Z + box.Max.Z) / 2.0
        except Exception:
            return None
    key = doc.PathName or doc.Title
    if key not in _LEVELS:
        from Autodesk.Revit.DB import Level
        _LEVELS[key] = sorted([(float(lv.ProjectElevation), lv) for lv in FilteredElementCollector(doc).OfClass(Level)],
                              key=lambda item: item[0])
    below = [lv for elevation, lv in _LEVELS[key] if elevation <= z + 0.01]
    if below:
        return below[-1]
    return _LEVELS[key][0][1] if _LEVELS[key] else None


def _is_lighting_fixture(element):
    try:
        return idv(element.Category.Id) == int(BuiltInCategory.OST_LightingFixtures)
    except Exception:
        return False


def level_of(element, use_location=None):
    """(level name, elevation in feet) of an element, or None.

    use_location True (default for LIGHTING FIXTURES): the level is taken from where the fixture really is -
    the highest level at or below its insertion point - NOT its Reference Level / Schedule Level. A light
    placed on Level 1 with a 4000 mm offset that hangs under the Level 2 slab ... counts on the level it is
    actually in. Other elements keep their reference level (their location is the fallback)."""
    doc = element.Document
    level = None
    if use_location is None:
        use_location = _is_lighting_fixture(element)
    if use_location:
        level = _level_below(element)
        if level is not None:
            try:
                return name_of(level), float(level.ProjectElevation)
            except Exception:
                level = None
    try:
        if element.LevelId is not None and idv(element.LevelId) > 0:
            level = doc.GetElement(element.LevelId)
    except Exception:
        pass
    if level is None:
        for name in ('FAMILY_LEVEL_PARAM', 'INSTANCE_REFERENCE_LEVEL_PARAM', 'SCHEDULE_LEVEL_PARAM',
                     'INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM'):
            bip = getattr(BuiltInParameter, name, None)
            try:
                p = element.get_Parameter(bip) if bip is not None else None
                if p is not None and p.AsElementId() is not None and idv(p.AsElementId()) > 0:
                    level = doc.GetElement(p.AsElementId())
                    break
            except Exception:
                pass
    if level is None:
        # FIX: a fitting with no level (face-hosted on a linked ceiling, ...) had none and stayed at the panel.
        # Now the level it stands on: the highest level at or below its height.
        level = _level_below(element)
    if level is None:
        return None
    try:
        return name_of(level), float(level.ProjectElevation)
    except Exception:
        return name_of(level), float(getattr(level, 'Elevation', 0.0))


def read_panel(panel, kinds, sides='slots', spares=False):
    """{'name', 'feed', 'left': [row], 'right': [row], 'circuits': n} - the panel's circuits in panel
    schedule order. sides 'slots': odd slots left, even slots right (the two columns of the panel
    schedule); 'half': the first half left, the rest right."""
    rows = []
    home = level_of(panel)
    for system in _circuits(panel):
        spare = _is_spare(system)
        if spare and not spares:
            continue
        try:
            slot = int(system.StartSlot)
        except Exception:
            slot = 0
        counts = {}
        levels = {}
        if not spare:
            try:
                for element in system.Elements:
                    key = classify(element, kinds)
                    counts[key] = counts.get(key, 0) + 1
                    found = level_of(element)
                    if found is not None:
                        levels[found] = levels.get(found, 0) + 1
            except Exception:
                pass
        # the level most of the circuit's fittings are on.
        # FIX: a MIXED circuit (fittings on several levels - a stair / riser circuit) went to its majority
        # level, a tie to the LOWEST one, so an L01 panel drew stair circuits down on B1 and its column spanned
        # every level. Now a mixed circuit that also serves the panel's own level stays at the panel.
        level = max(levels, key=lambda item: (levels[item], -item[1])) if levels else None
        if MIXED_AT_PANEL and len(levels) > 1 and home is not None and home in levels:
            level = home
        number = u''
        try:
            number = system.CircuitNumber or u''
        except Exception:
            pass
        rows.append({'circuit': number, 'slot': slot, 'counts': counts, 'spare': spare,
                     'load': _text(system, 'RBS_ELEC_CIRCUIT_NAME'), 'level': level,
                     'mixed': len(levels) > 1,
                     # [(level name, fittings)] highest level first - for the log
                     'spread': [(k[0], n) for k, n in sorted(levels.items(), key=lambda kv: -kv[0][1])]})
    rows.sort(key=lambda row: (row['slot'] if row['slot'] else 99999, natural_key(row['circuit'])))
    if sides == 'slots' and [1 for row in rows if row['slot']]:
        left = [row for row in rows if row['slot'] % 2 == 1]
        right = [row for row in rows if row['slot'] % 2 == 0]
    else:
        half = (len(rows) + 1) // 2
        left, right = rows[:half], rows[half:]
    # zones: 0 = the panel's level, 1 = the next level up with fittings, -1 = the next one down, ...
    elevations = sorted(set([row['level'][1] for row in rows if row['level']] + ([home[1]] if home else [])))
    names, zone_elev = {}, {}
    base = elevations.index(home[1]) if home else None
    for row in rows:
        row['zone'] = 0
        if home and row['level'] and abs(row['level'][1] - home[1]) > 1e-6:
            row['zone'] = elevations.index(row['level'][1]) - base
            names[row['zone']] = row['level'][0]
            zone_elev[row['zone']] = round(row['level'][1], 4)
    if home:
        names[0] = home[0]
        zone_elev[0] = round(home[1], 4)
    return {'name': panel_name(panel), 'feed': supply_of(panel), 'left': left, 'right': right,
            'circuits': len(rows), 'uid': panel.UniqueId, 'zone_names': names, 'zone_elev': zone_elev,
            'level': home[0] if home else u''}


# ============================================================================ what the user can pick
def text_types(doc):
    found = [(name_of(t), idv(t.Id)) for t in FilteredElementCollector(doc).OfClass(TextNoteType)]
    return sorted(found, key=lambda item: item[0].lower())


def default_text_type(doc):
    try:
        return idv(doc.GetDefaultElementTypeId(ElementTypeGroup.TextNoteType))
    except Exception:
        return 0


def text_size_mm(doc, type_number):
    """(height in paper mm, width factor) of a text type."""
    text_type = doc.GetElement(make_id(type_number))
    height, factor = 2.5, 1.0
    try:
        height = text_type.get_Parameter(BuiltInParameter.TEXT_SIZE).AsDouble() * 304.8
    except Exception:
        pass
    try:
        factor = text_type.get_Parameter(BuiltInParameter.TEXT_WIDTH_SCALE).AsDouble() or 1.0
    except Exception:
        pass
    return max(height, 0.5), factor


def line_styles(doc):
    """[(name, GraphicsStyle id number)] of the line styles a detail line can take."""
    found = []
    try:
        parent = doc.Settings.Categories.get_Item(BuiltInCategory.OST_Lines)
        for sub in parent.SubCategories:
            try:
                if sub.Name in SPECIAL_LINE_STYLES:
                    continue
                style = sub.GetGraphicsStyle(GraphicsStyleType.Projection)
                if style is not None:
                    found.append((sub.Name, idv(style.Id)))
            except Exception:
                pass
    except Exception:
        pass
    return sorted(found, key=lambda item: (not item[0].startswith('<'), item[0].lower()))


def drafting_views(doc):
    return dict((name_of(v), v) for v in FilteredElementCollector(doc).OfClass(ViewDrafting) if not v.IsTemplate)


# ============================================================================ tagging
_GUID = System.Guid('c3b1f2a4-58d7-4e0b-9a6c-2f1e7d4b8a51')


def _schema():
    schema = Schema.Lookup(_GUID)
    if schema is not None:
        return schema
    builder = SchemaBuilder(_GUID)
    builder.SetReadAccessLevel(AccessLevel.Public)
    builder.SetWriteAccessLevel(AccessLevel.Public)
    builder.SetSchemaName('EleToolsCbsSld')
    builder.SetDocumentation('Detail lines and text drawn by the CBS SLD tool')
    builder.AddSimpleField('Panel', clr.GetClrType(System.String))
    return builder.Finish()


def _tag(element, panel_uid):
    schema = _schema()
    entity = Entity(schema)
    entity.Set[System.String](schema.GetField('Panel'), panel_uid)
    element.SetEntity(entity)


def tagged_ids(doc, view, panel_uid=None):
    """Ids of the lines / text in the view that this tool drew (for one panel, or for any)."""
    schema = Schema.Lookup(_GUID)
    if schema is None:
        return []
    found = []
    # every element of the view: lines and text, and also the copied details (detail items, filled regions,
    # groups, images ...)
    for element in FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType():
        if True:
            try:
                entity = element.GetEntity(schema)
                if entity is None or not entity.IsValid():
                    continue
                if panel_uid is None or entity.Get[System.String](schema.GetField('Panel')) == panel_uid:
                    found.append(element.Id)
            except Exception:
                pass
    return found


# ============================================================================ drawing
def target_view(doc, name, scale, mode):
    """(view, made_new). mode 'redraw': reuse the drafting view of that name; 'new': always a new one."""
    views = drafting_views(doc)
    if mode == 'redraw' and name in views:
        return views[name], False
    family = [t for t in FilteredElementCollector(doc).OfClass(ViewFamilyType)
              if t.ViewFamily == ViewFamily.Drafting]
    if not family:
        raise ValueError('The model has no drafting view type.')
    view = ViewDrafting.Create(doc, family[0].Id)
    wanted, number = name, 2
    while wanted in views:
        wanted = u'{0} ({1})'.format(name, number)
        number += 1
    try:
        view.Name = wanted
    except Exception:
        pass
    try:
        view.Scale = int(scale)
    except Exception:
        pass
    return view, True


# ============================================================================ fixed details (copied)
DETAILS_TAG = u'CBS-DETAILS'
_SKIP_CATEGORIES = set(int(getattr(BuiltInCategory, name)) for name in
                       ('OST_IOSSketchGrid', 'OST_SketchLines', 'OST_Viewers', 'OST_CLines')
                       if hasattr(BuiltInCategory, name))


def _detail_ids(doc, view):
    """The view's own elements that can be copied: detail lines, text, detail items, filled regions, groups,
    images, ... (top-level only: members of a group come with their group)."""
    from System.Collections.Generic import List
    ids = List[ElementId]()
    for element in FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType():
        try:
            if element.OwnerViewId != view.Id or element.Category is None:
                continue
            if element.GroupId is not None and element.GroupId != ElementId.InvalidElementId:
                continue
            if idv(element.Category.Id) in _SKIP_CATEGORIES:
                continue
            ids.Add(element.Id)
        except Exception:
            continue
    return ids


def details_size(doc, source):
    """(width, height) in feet (model size) of the details drawn in the source view, or None."""
    box = _ids_box(doc, source, _detail_ids(doc, source))
    if box is None:
        return None
    return box[2] - box[0], box[3] - box[1]


def _ids_box(doc, view, ids):
    xs, ys = [], []
    for element_id in ids:
        try:
            box = doc.GetElement(element_id).get_BoundingBox(view)
        except Exception:
            box = None
        if box is None:
            continue
        xs += [box.Min.X, box.Max.X]
        ys += [box.Min.Y, box.Max.Y]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def copy_details(doc, source, target, right_x, top_y):
    """Copy every detail of the source drafting view into the target view, so that the copied details' right
    edge is at right_x and their top at top_y (feet). Returns the number of elements copied.
    The copies are tagged, so a Redraw replaces them as well."""
    from Autodesk.Revit.DB import CopyPasteOptions, ElementTransformUtils, Transform
    ids = _detail_ids(doc, source)
    box = _ids_box(doc, source, ids)
    if box is None or not ids.Count:
        return 0
    move = Transform.CreateTranslation(XYZ(right_x - box[2], top_y - box[3], 0))
    options = CopyPasteOptions()
    copied = ElementTransformUtils.CopyElements(source, ids, target, move, options)
    for element_id in copied:
        try:
            _tag(doc.GetElement(element_id), DETAILS_TAG)
        except Exception:
            pass                                      # some kinds cannot carry the tag - redraw keeps them
    return copied.Count


_H = {'L': HorizontalTextAlignment.Left, 'C': HorizontalTextAlignment.Center, 'R': HorizontalTextAlignment.Right}
_V = {'T': VerticalTextAlignment.Top, 'M': VerticalTextAlignment.Middle, 'B': VerticalTextAlignment.Bottom}


def measure(doc, view, texts, types, unit_ft):
    """{(text, size): (width px, height px)}: the real size of each text in this view, read from Revit.
    Every text is created once, measured (one regenerate for all) and deleted again (inside the caller's
    transaction)."""
    notes = []
    option_of = {}
    for text, size in texts:
        if not text:
            continue
        if size not in option_of:
            option = TextNoteOptions(make_id(types[size]))
            option.HorizontalAlignment = HorizontalTextAlignment.Left
            option_of[size] = option
        try:
            notes.append(((text, size), TextNote.Create(doc, view.Id, XYZ(0, 0, 0), text.replace('\n', '\r'),
                                                         option_of[size])))
        except Exception:
            pass
    if not notes:
        return {}
    doc.Regenerate()
    found = {}
    for key, note in notes:
        try:
            box = note.get_BoundingBox(view)
            found[key] = ((box.Max.X - box.Min.X) / unit_ft, (box.Max.Y - box.Min.Y) / unit_ft)
        except Exception:
            pass
    for _key, note in notes:
        try:
            doc.Delete(note.Id)
        except Exception:
            pass
    return found


def _settle(doc, view, placed):
    """Move every text so that its real outline sits on its anchor (left / centre / right, top / middle /
    bottom) - whatever point Revit took the given position to be."""
    if not placed:
        return 0
    doc.Regenerate()
    moved = 0
    for note, (x, y), h, v in placed:
        try:
            box = note.get_BoundingBox(view)
            ax = box.Min.X if h == 'L' else (box.Max.X if h == 'R' else (box.Min.X + box.Max.X) / 2.0)
            ay = box.Max.Y if v == 'T' else (box.Min.Y if v == 'B' else (box.Min.Y + box.Max.Y) / 2.0)
            dx, dy = x - ax, y - ay
            if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                note.Coord = XYZ(note.Coord.X + dx, note.Coord.Y + dy, note.Coord.Z)
                moved += 1
        except Exception:
            pass
    return moved


def draw(doc, view, shapes, unit_ft, origin, styles, types, panel_uid):
    """Lines and text notes for the shapes. unit_ft: model feet per layout px; origin (x, y) feet;
    styles {'thin': id number, 'box': id number}; types {'dev'|'lbl'|'title': id number}.
    Returns (lines, texts)."""
    ox, oy = origin
    style_elements = dict((key, doc.GetElement(make_id(value))) for key, value in styles.items() if value)
    options = {}
    placed = []
    lines = texts = 0
    for shape in shapes:
        if shape[0] == 'line':
            a = XYZ(ox + shape[1] * unit_ft, oy - shape[2] * unit_ft, 0)
            b = XYZ(ox + shape[3] * unit_ft, oy - shape[4] * unit_ft, 0)
            if a.DistanceTo(b) < doc.Application.ShortCurveTolerance * 1.5:
                continue
            curve = doc.Create.NewDetailCurve(view, Line.CreateBound(a, b))
            style = style_elements.get(shape[5]) or style_elements.get('thin')
            if style is not None:
                try:
                    curve.LineStyle = style
                except Exception:
                    pass
            _tag(curve, panel_uid)
            lines += 1
        else:
            _kind, x, y, text, size, h, v = shape[:7]
            angle = shape[7] if len(shape) > 7 else 0.0   # degrees (the vertical BUILDING DATA NETWORK text)
            if not text or size not in types:
                continue
            key = (size, h, v, angle)
            if key not in options:
                option = TextNoteOptions(make_id(types[size]))
                option.HorizontalAlignment = _H[h]
                try:
                    option.VerticalAlignment = _V[v]
                except Exception:
                    pass
                if angle:
                    import math
                    option.Rotation = math.radians(angle)
                options[key] = option
            target = (ox + x * unit_ft, oy - y * unit_ft)
            note = TextNote.Create(doc, view.Id, XYZ(target[0], target[1], 0),
                                   text.replace('\n', '\r'), options[key])
            _tag(note, panel_uid)
            placed.append((note, target, h, v))
            texts += 1
    _settle(doc, view, placed)
    return lines, texts

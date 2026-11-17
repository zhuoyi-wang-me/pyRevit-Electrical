# -*- coding: utf-8 -*-
"""Cable Tray QA/QC - reads cable tray annotations from the Revit model.

READ-ONLY: this module never opens a transaction.

Three readers, each a function that takes a ReaderContext and returns a list
of annotation dicts in the same shape, so one can be changed or a new one added
without touching the comparison:

    read_independent_tags     real Revit tags (preferred)
    read_text_notes           Text Notes that look like a tray annotation
    read_generic_annotations  Generic Annotation families (text in parameters)

READERS at the bottom maps a name to each function; read_annotations() runs the
ones that are switched on.

Performance: views-on-sheets are indexed ONCE from the Viewport elements
(one collector), and each reader makes ONE collector pass over the document.
Nothing is collected per sheet or per view.
"""
from Autodesk.Revit.DB import (BuiltInCategory, BuiltInParameter, ElementId,
                               FamilyInstance, FilteredElementCollector, IndependentTag,
                               StorageType, TextNote, UnitUtils, ViewSheet, Viewport)

import config
from annotation_parser import could_be_tray_annotation

try:
    from Autodesk.Revit.DB import UnitTypeId
    _MILLIMETRES = UnitTypeId.Millimeters
except Exception:                                   # Revit 2020 and older
    from Autodesk.Revit.DB import DisplayUnitType
    _MILLIMETRES = DisplayUnitType.DUT_MILLIMETERS


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def id_value(element_id):
    """ElementId -> int on every Revit version (.Value from 2024, .IntegerValue before)."""
    if element_id is None:
        return None
    try:
        return int(element_id.Value)
    except AttributeError:
        return int(element_id.IntegerValue)


def make_id(value):
    try:
        from System import Int64
        return ElementId(Int64(value))
    except Exception:
        return ElementId(int(value))


def _category_ids(names):
    ids = set()
    for name in names:
        category = getattr(BuiltInCategory, name, None)
        if category is not None:
            ids.add(id_value(ElementId(category)))
    return ids


def _category_id(element):
    try:
        category = element.Category
        return id_value(category.Id) if category is not None else None
    except Exception:
        return None


def _name(element):
    try:
        return element.Name or ''
    except Exception:
        return ''


def _to_mm(internal):
    return UnitUtils.ConvertFromInternalUnits(internal, _MILLIMETRES)


def _text_parameter(element, name):
    try:
        parameter = element.LookupParameter(name)
        if parameter is None or not parameter.HasValue:
            return ''
        if parameter.StorageType == StorageType.String:
            return parameter.AsString() or ''
        return parameter.AsValueString() or ''
    except Exception:
        return ''


# ---------------------------------------------------------------------------
# sheet / view index
# ---------------------------------------------------------------------------
class SheetIndex(object):
    """Which sheet(s) every view is on. Built once per scan.

    by_view   {view id: [(sheet id, sheet number, sheet name), ...]}
              a sheet is also listed under its OWN id, because annotations can
              be drawn directly on a sheet.
    """

    def __init__(self, doc):
        self.sheets = {}
        self.by_view = {}
        for sheet in FilteredElementCollector(doc).OfClass(ViewSheet):
            try:
                if sheet.IsPlaceholder:
                    continue
                entry = (id_value(sheet.Id), sheet.SheetNumber or '', _name(sheet))
            except Exception:
                continue
            self.sheets[entry[0]] = entry
            self.by_view.setdefault(entry[0], []).append(entry)
        # DEPENDENT VIEWS. A sheet usually shows a dependent view (one part of
        # a floor split by matchlines), but a tag drawn in it is OWNED by the
        # primary (parent) view: IndependentTag.OwnerViewId is the parent,
        # which itself is not on any sheet. Without this index every such tag
        # looked like "a tag in a view that is not on a sheet" and was ignored.
        #   dependents {primary view id: [DependentView, ...]}
        self.dependents = {}
        for viewport in FilteredElementCollector(doc).OfClass(Viewport):
            try:
                sheet = self.sheets.get(id_value(viewport.SheetId))
                if sheet is None:
                    continue
                view_id = id_value(viewport.ViewId)
                self.by_view.setdefault(view_id, []).append(sheet)
            except Exception:
                continue
            try:
                view = doc.GetElement(viewport.ViewId)
                primary_id = id_value(view.GetPrimaryViewId())
            except Exception:
                continue
            if primary_id is not None and primary_id > 0 and primary_id != view_id:
                self.dependents.setdefault(primary_id, []).append(DependentView(view, view_id, sheet))
        for entries in self.by_view.values():
            entries.sort(key=lambda entry: entry[1])

    def sheets_of(self, view_id):
        return self.by_view.get(view_id, [])


# How far outside a dependent view's crop region an annotation may sit and
# still belong to it (tags are often placed just outside the model crop,
# inside the annotation crop), and the furthest it may be before it is treated
# as not shown in that view at all. Model units are feet.
_CROP_MARGIN_FT = 1.0 / 0.3048            # 1 m
_CROP_NEAREST_FT = 5.0 / 0.3048           # 5 m


class DependentView(object):
    """A dependent view placed on a sheet, with its crop region."""

    def __init__(self, view, view_id, sheet):
        self.view_id = view_id
        self.name = _name(view)
        self.sheet = sheet
        self.crop = None                  # (inverse transform, min, max) or None = shows everything
        try:
            if view.CropBoxActive:
                box = view.CropBox
                self.crop = (box.Transform.Inverse, box.Min, box.Max)
        except Exception:
            self.crop = None

    def distance(self, point):
        """0 when the point is inside the crop region (seen along the view
        direction), else how far outside it is, in feet."""
        if self.crop is None or point is None:
            return 0.0
        try:
            inverse, low, high = self.crop
            local = inverse.OfPoint(point)
            dx = max(low.X - local.X, 0.0, local.X - high.X)
            dy = max(low.Y - local.Y, 0.0, local.Y - high.Y)
            return (dx * dx + dy * dy) ** 0.5
        except Exception:
            return 0.0


class ReaderContext(object):
    """What every reader needs, plus a cache of view names."""

    def __init__(self, doc, known_tags, only_on_sheets, sheet_ids=None):
        self.doc = doc
        self.known_tags = known_tags or set()
        self.only_on_sheets = only_on_sheets
        # None = every sheet; a set of sheet ids = only annotations that are
        # on at least one of THOSE sheets are read
        self.sheet_ids = set(sheet_ids) if sheet_ids is not None else None
        self.index = SheetIndex(doc)
        self.tag_categories = _category_ids(config.TAG_CATEGORIES)
        self.tray_categories = _category_ids(config.TRAY_CATEGORIES)
        self.multi_category = _category_ids(['OST_MultiCategoryTags'])
        self._view_names = {}
        self._type_labels = {}
        self.skipped = {}                  # reason -> count, shown after the scan
        self.via_dependent = 0             # annotations found through a dependent view

    def type_label(self, element):
        """'Family : Type' of an annotation, cached per type. This is what tells
        the user WHICH tag family produced a row (e.g. a size-only tag family
        whose label has no calculation tag)."""
        try:
            type_id = element.GetTypeId()
            key = id_value(type_id)
        except Exception:
            return ''
        if key not in self._type_labels:
            label = ''
            try:
                symbol = self.doc.GetElement(type_id)
                if symbol is not None:
                    try:
                        from Autodesk.Revit.DB import Element
                        name = Element.Name.GetValue(symbol)     # .Name is not readable on types
                    except Exception:
                        name = _name(symbol)
                    family = getattr(symbol, 'FamilyName', '') or ''
                    label = u'{0} : {1}'.format(family, name) if family and name != family else (name or family)
            except Exception:
                label = ''
            self._type_labels[key] = label
        return self._type_labels[key]

    def skip(self, reason):
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def view_name(self, view_id):
        if view_id not in self._view_names:
            view = self.doc.GetElement(make_id(view_id)) if view_id is not None else None
            self._view_names[view_id] = _name(view) if view is not None else ''
        return self._view_names[view_id]

    def place(self, owner_view_id, point=None):
        """(view id, view name, sheets) for an annotation, or None when it must
        be ignored (not on a sheet and only sheets are checked).

        point: where the annotation is (its tag head). Needed when the owner
        view is the PRIMARY of dependent views: the annotation then belongs to
        the dependent view whose crop region contains it, and the row reports
        that dependent view and its sheet."""
        view_id = id_value(owner_view_id)
        if view_id is None or view_id < 0:
            return None

        # every (view, sheets) this annotation can be seen through
        candidates = []
        direct = self.index.sheets_of(view_id)
        if direct:
            candidates.append((view_id, list(direct)))
        dependents = self.index.dependents.get(view_id)
        if dependents:
            ranked = sorted(((dependent.distance(point), dependent) for dependent in dependents),
                            key=lambda item: item[0])
            inside = [dependent for gap, dependent in ranked if gap <= _CROP_MARGIN_FT]
            if not inside and ranked[0][0] <= _CROP_NEAREST_FT:
                inside = [ranked[0][1]]            # just outside every crop: the nearest one
            for dependent in inside:
                candidates.append((dependent.view_id, [dependent.sheet]))
            if inside:
                self.via_dependent += 1
            elif not direct:
                self.skip('annotations outside the crop region of every dependent view on a sheet')
                return None

        if self.sheet_ids is not None:
            # keep only the selected sheets, so the row reports a sheet the
            # user actually asked for
            on_any_sheet = bool(candidates)
            candidates = [(candidate_view, [sheet for sheet in sheets if sheet[0] in self.sheet_ids])
                          for candidate_view, sheets in candidates]
            candidates = [item for item in candidates if item[1]]
            if not candidates:
                if on_any_sheet:
                    self.skip('annotations on sheets that are not selected')
                else:
                    self.skip('annotations in views that are not on a sheet')
                return None
        if not candidates:
            if self.only_on_sheets:
                self.skip('annotations in views that are not on a sheet')
                return None
            return view_id, self.view_name(view_id), []
        shown_view = candidates[0][0]
        sheets = []
        for _view, found in candidates:
            for sheet in found:
                if sheet not in sheets:
                    sheets.append(sheet)
        return shown_view, self.view_name(shown_view), sheets

    def record(self, kind, text, annotation, placement, element=None):
        view_id, view_name, sheets = placement
        first = sheets[0] if sheets else (None, '', '')
        data = {
            'kind': kind, 'text': text,
            'family': self.type_label(annotation) or kind,
            'annotation_id': id_value(annotation.Id),
            'element_id': id_value(element.Id) if element is not None else None,
            'view_id': view_id, 'view_name': view_name,
            'sheet_id': first[0], 'sheet_number': first[1], 'sheet_name': first[2],
            'sheet_ids': [sheet[0] for sheet in sheets],
            'other_sheets': [sheet[1] for sheet in sheets[1:]],
            'model_width': None, 'model_height': None, 'model_tag': '',
        }
        if element is not None:
            data['model_width'], data['model_height'] = tray_size_mm(element)
            data['model_tag'] = tray_tag(self.doc, element)
        return data


# ---------------------------------------------------------------------------
# the tagged cable tray
# ---------------------------------------------------------------------------
def tray_size_mm(element):
    """(width, height) of a cable tray in mm, or (None, None) for a fitting."""
    values = []
    for name in ('RBS_CABLETRAY_WIDTH_PARAM', 'RBS_CABLETRAY_HEIGHT_PARAM'):
        parameter_id = getattr(BuiltInParameter, name, None)
        try:
            parameter = element.get_Parameter(parameter_id) if parameter_id is not None else None
            if parameter is None or not parameter.HasValue:
                return None, None
            values.append(round(_to_mm(parameter.AsDouble()), 3))
        except Exception:
            return None, None
    return values[0], values[1]


def tray_tag(doc, element):
    """Calculation tag stored on the tray itself (instance, then type)."""
    sources = [element]
    try:
        sources.append(doc.GetElement(element.GetTypeId()))
    except Exception:
        pass
    for name in config.TRAY_TAG_PARAMETERS:
        for source in sources:
            if source is None:
                continue
            value = _text_parameter(source, name).strip()
            if value:
                return value
    return ''


def _point_of(annotation):
    """Where an annotation sits: tag head, text note origin or family
    insertion point. None when it cannot be read."""
    for getter in (lambda a: a.TagHeadPosition, lambda a: a.Coord, lambda a: a.Location.Point):
        try:
            point = getter(annotation)
            if point is not None:
                return point
        except Exception:
            continue
    return None


def _tagged_elements(doc, tag):
    """The local element(s) a tag points at (a tag can have several hosts)."""
    try:
        return [element for element in tag.GetTaggedLocalElements() if element is not None]
    except Exception:
        pass
    try:                                             # Revit 2021 and older
        element = tag.GetTaggedLocalElement()
        return [element] if element is not None else []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# reader 1: IndependentTag (preferred)
# ---------------------------------------------------------------------------
def read_independent_tags(context):
    """Real Revit tags. The visible text comes from IndependentTag.TagText and
    the tagged cable tray is read as well, so the check does not depend on the
    text alone."""
    records = []
    for tag in FilteredElementCollector(context.doc).OfClass(IndependentTag):
        category_id = _category_id(tag)
        if category_id not in context.tag_categories:
            continue
        try:
            placement = context.place(tag.OwnerViewId, _point_of(tag))
        except Exception:
            placement = None
        if placement is None:
            continue                        # the reason was counted by place()
        hosts = _tagged_elements(context.doc, tag)
        element = None
        for host in hosts:
            if _category_id(host) in context.tray_categories:
                element = host
                break
        if category_id in context.multi_category and element is None:
            continue                        # a multi-category tag on something else
        try:
            text = tag.TagText or ''
        except Exception:
            text = ''                       # orphaned tag (host deleted / link unloaded)
        if not hosts and not text.strip():
            context.skip('orphaned tags with no host and no text')
            continue
        records.append(context.record('Tag', text, tag, placement, element))
    return records


# ---------------------------------------------------------------------------
# reader 2: TextNote
# ---------------------------------------------------------------------------
def read_text_notes(context):
    """Text Notes. A note is only used when it looks like a tray annotation,
    otherwise every general note in the project would be reported as invalid."""
    records = []
    for note in FilteredElementCollector(context.doc).OfClass(TextNote):
        try:
            placement = context.place(note.OwnerViewId, _point_of(note))
            text = note.Text or ''
        except Exception:
            continue
        if placement is None:
            continue
        if not could_be_tray_annotation(text, context.known_tags):
            continue
        records.append(context.record('Text Note', text, note, placement))
    return records


# ---------------------------------------------------------------------------
# reader 3: Generic Annotation family
# ---------------------------------------------------------------------------
def read_generic_annotations(context):
    """Generic Annotation families whose text is held in parameters
    (config.GENERIC_ANNOTATION_PARAMETERS: tag first, size line second)."""
    records = []
    category = getattr(BuiltInCategory, 'OST_GenericAnnotation', None)
    if category is None:
        return records
    wanted = (config.GENERIC_ANNOTATION_FAMILY_CONTAINS or '').lower()
    collector = (FilteredElementCollector(context.doc).OfCategory(category)
                 .WhereElementIsNotElementType())
    for instance in collector:
        if not isinstance(instance, FamilyInstance):
            continue
        try:
            symbol = instance.Symbol
            if wanted and wanted not in (symbol.FamilyName or '').lower():
                continue
            placement = context.place(instance.OwnerViewId, _point_of(instance))
        except Exception:
            continue
        if placement is None:
            continue
        lines = []
        for name in config.GENERIC_ANNOTATION_PARAMETERS:
            value = _text_parameter(instance, name) or _text_parameter(symbol, name)
            if value.strip():
                lines.append(value.strip())
        text = '\n'.join(lines)
        if not could_be_tray_annotation(text, context.known_tags):
            continue
        records.append(context.record('Generic Annotation', text, instance, placement))
    return records


# ---------------------------------------------------------------------------
# reader 4: the cable trays themselves (tag stored in a parameter)
# ---------------------------------------------------------------------------
AUTO_PARAMETER = '(auto-detect)'


def _string_parameters(element):
    """[(parameter name, text)] of the text parameters that have a value."""
    found = []
    try:
        for parameter in element.Parameters:
            try:
                if parameter.StorageType != StorageType.String or not parameter.HasValue:
                    continue
                value = (parameter.AsString() or '').strip()
                if value:
                    found.append((parameter.Definition.Name, value))
            except Exception:
                continue
    except Exception:
        pass
    return found


def _cable_trays(doc):
    category = getattr(BuiltInCategory, 'OST_CableTray', None)
    if category is None:
        return []
    return list(FilteredElementCollector(doc).OfCategory(category).WhereElementIsNotElementType())


def list_tray_parameters(doc):
    """Names of the text parameters found on cable trays (instance and type),
    for the "Tag parameter" list. One tray per type is sampled."""
    names = set()
    seen_types = set()
    for tray in _cable_trays(doc):
        try:
            type_id = tray.GetTypeId()
            key = id_value(type_id)
        except Exception:
            continue
        if key in seen_types:
            continue
        seen_types.add(key)
        for source in (tray, doc.GetElement(type_id)):
            if source is None:
                continue
            try:
                for parameter in source.Parameters:
                    try:
                        if parameter.StorageType == StorageType.String:
                            names.add(parameter.Definition.Name)
                    except Exception:
                        continue
            except Exception:
                continue
    return sorted(names, key=lambda name: name.lower())


def _tray_tag_from(value, known_tags):
    """The calculation tag held in a parameter value. The whole value is the
    tag; if the value holds more text, a tag the Excel knows wins."""
    from annotation_parser import normalize_tag, parse_annotation
    whole = normalize_tag(value)
    if not whole or whole in known_tags:
        return whole
    parsed = parse_annotation(value, known_tags)
    if parsed['tag_source'] == 'excel':
        return parsed['tag']
    return whole


def read_cable_trays(context):
    """The cable tray ELEMENTS, not their annotations: the calculation tag is
    read from a parameter of the tray and the size is the tray's real width
    and height.

    Which parameter: context.tray_parameter, or - on auto-detect - every text
    parameter in which at least one tray holds a tag from the Excel.

    A tray run is many segments with the same tag, so segments are grouped:
    one record per (tag, width, height) with the number of segments. A run
    drawn with two sizes therefore gives two rows.
    """
    doc = context.doc
    trays = _cable_trays(doc)
    type_values = {}

    def values_of(tray):
        values = _string_parameters(tray)
        try:
            type_id = tray.GetTypeId()
            key = id_value(type_id)
            if key not in type_values:
                symbol = doc.GetElement(type_id)
                type_values[key] = _string_parameters(symbol) if symbol is not None else []
            values = values + type_values[key]
        except Exception:
            pass
        return values

    wanted = context.tray_parameter
    per_tray = []
    if wanted and wanted != AUTO_PARAMETER:
        names = [wanted]
        for tray in trays:
            value = _text_parameter(tray, wanted)
            if not value:
                try:
                    symbol = doc.GetElement(tray.GetTypeId())
                    value = _text_parameter(symbol, wanted) if symbol is not None else ''
                except Exception:
                    value = ''
            per_tray.append((tray, [(wanted, value)] if value.strip() else []))
    else:
        # auto-detect: which parameters hold tags that the Excel knows?
        from annotation_parser import normalize_tag
        hits = {}
        for tray in trays:
            values = values_of(tray)
            per_tray.append((tray, values))
            for name, value in values:
                if normalize_tag(value) in context.known_tags:
                    hits[name] = hits.get(name, 0) + 1
        names = [name for name, _count in sorted(hits.items(), key=lambda item: -item[1])]
    context.tray_parameters_used = names

    groups = {}
    order = []
    untagged = {}
    for tray, values in per_tray:
        by_name = dict(values)
        tag = ''
        for name in names:                       # most used parameter first
            if by_name.get(name):
                tag = _tray_tag_from(by_name[name], context.known_tags)
                if tag:
                    break
        width, height = tray_size_mm(tray)
        if not tag:
            key = (width, height)
            untagged.setdefault(key, []).append(tray)
            continue
        key = (tag, width, height)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(tray)

    def level_of(tray):
        try:
            level = tray.ReferenceLevel
            return _name(level) if level is not None else ''
        except Exception:
            return ''

    def record(tag, width, height, members):
        first = members[0]
        levels = sorted(set(level for level in (level_of(tray) for tray in members) if level))
        description = context.type_label(first)
        if levels:
            description = u'{0}  |  Level: {1}'.format(description, u', '.join(levels[:4]))
        ids = [id_value(tray.Id) for tray in members]
        problems = []
        if not tag:
            problems.append('no calculation tag in the cable tray parameter')
        if width is None or height is None:
            problems.append('cable tray size could not be read')
        return {
            'kind': 'Cable Tray', 'text': tag or u'',
            'family': context.type_label(first) or 'Cable Tray',
            'annotation_id': ids[0], 'element_id': ids[0], 'element_ids': ids,
            'view_id': None, 'view_name': '(model element)',
            'sheet_id': None, 'sheet_number': '', 'sheet_name': '', 'sheet_ids': [],
            'other_sheets': [], 'model_width': None, 'model_height': None, 'model_tag': '',
            'segments': len(members),
            'parsed': {'tag': tag or None, 'tag_source': 'parameter', 'quantity': 1,
                       'width': width, 'height': height, 'description': description,
                       'problems': problems},
        }

    records = [record(key[0], key[1], key[2], groups[key]) for key in order]
    total_untagged = sum(len(members) for members in untagged.values())
    if total_untagged:
        if context.report_untagged:
            for (width, height), members in sorted(untagged.items(), key=lambda item: -len(item[1])):
                records.append(record('', width, height, members))
        else:
            context.skipped['cable trays with no calculation tag'] = total_untagged
    context.tray_counts = {'trays': len(trays), 'tagged': len(trays) - total_untagged,
                           'untagged': total_untagged}
    return records


# name -> reader. Add a function here to support another kind of annotation.
READERS = [
    ('tags', read_independent_tags),
    ('text_notes', read_text_notes),
    ('generic_annotations', read_generic_annotations),
    ('cable_trays', read_cable_trays),
]


def list_sheets(doc):
    """Every (non-placeholder) sheet with the text of its parameters, for the
    sheet picker and its filters. One collector pass; read-only.

    Returns [{'id', 'number', 'name', 'params': {parameter name: text}}]
    sorted by sheet number.
    """
    sheets = []
    for sheet in FilteredElementCollector(doc).OfClass(ViewSheet):
        try:
            if sheet.IsPlaceholder:
                continue
            entry = {'id': id_value(sheet.Id), 'number': sheet.SheetNumber or '',
                     'name': _name(sheet), 'params': {}}
        except Exception:
            continue
        try:
            for parameter in sheet.Parameters:
                try:
                    if not parameter.HasValue:
                        continue
                    name = parameter.Definition.Name
                    if parameter.StorageType == StorageType.String:
                        value = parameter.AsString()
                    else:
                        value = parameter.AsValueString()
                    value = (value or '').strip()
                    if name and value and name not in entry['params']:
                        entry['params'][name] = value
                except Exception:
                    continue
        except Exception:
            pass
        sheets.append(entry)
    sheets.sort(key=lambda entry: entry['number'])
    return sheets


def read_annotations(doc, known_tags, readers, only_on_sheets=True, sheet_ids=None,
                     tray_parameter=None, report_untagged=False):
    """Run the selected readers.

    readers:         names from READERS, e.g. ['tags'] or ['tags', 'cable_trays'].
    sheet_ids:       None = every sheet; a set of sheet ids = only those sheets
                     (annotation readers only - cable trays are model elements).
    tray_parameter:  for 'cable_trays': the parameter that holds the tag, or
                     None / AUTO_PARAMETER to detect it.
    report_untagged: for 'cable_trays': list trays that have no tag.
    Returns (records, info) where info is a dict with counts for the status bar.
    """
    context = ReaderContext(doc, known_tags, only_on_sheets, sheet_ids)
    context.tray_parameter = tray_parameter
    context.report_untagged = report_untagged
    context.tray_parameters_used = []
    context.tray_counts = {}
    records = []
    counts = {}
    for name, reader in READERS:
        if name not in readers:
            continue
        found = reader(context)
        counts[name] = len(found)
        records.extend(found)
    info = {'counts': counts, 'skipped': dict(context.skipped),
            'via_dependent': context.via_dependent,
            'tray_parameters': list(context.tray_parameters_used),
            'tray_counts': dict(context.tray_counts),
            'dependent_views': sum(len(found) for found in context.index.dependents.values()),
            'sheets': len(context.index.sheets),
            'views_on_sheets': len(context.index.by_view) - len(context.index.sheets)}
    return records, info

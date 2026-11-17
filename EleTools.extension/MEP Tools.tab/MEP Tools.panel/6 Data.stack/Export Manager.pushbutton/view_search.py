# -*- coding: utf-8 -*-
"""Search + filter logic for the selection grid (Views AND Sheets).

UI-free, like sheet_filter.py: no WPF, no forms.alert().  ui.py calls it.

Search syntax (case-insensitive, typed into the main Search box)
-----------------------------------------------------------------
    level 01            every word must match somewhere      (AND)
    "level 01"          quoted phrase must match as-is
    -dependent          exclude rows containing "dependent"
    type:section        match only in one field
    sheet:A101  level:"Level 01"  name:power  template:none  file:rev

Fields for views : type, name, level, sheet, template, file
Fields for sheets: number, name, file, filter
Any unknown "xyz:abc" token is treated as ordinary text.

All Revit reads happen ONCE when items are indexed (index_view_item /
index_sheet_item).  Typing only walks pre-built Python strings, so the
search is fast on thousands of views and never touches the Revit API from
the modeless window's UI thread.
"""

import re

from Autodesk.Revit.DB import BuiltInParameter, ElementId


# Drop-down sentinels ------------------------------------------------------
ALL_TYPES = "<All view types>"
ALL_LEVELS = "<All levels>"
NO_LEVEL = "<No level>"

SHEET_ANY = "Placed or not placed"
SHEET_PLACED = "Placed on a sheet"
SHEET_UNPLACED = "Not on any sheet"
SHEET_STATES = [SHEET_ANY, SHEET_PLACED, SHEET_UNPLACED]

VIEW_FIELDS = ("type", "name", "level", "sheet", "template", "file")
SHEET_FIELDS = ("number", "name", "file", "filter")

_TOKEN = re.compile(r'(-?)(?:(\w+):)?(?:"([^"]*)"|(\S+))')
_CAMEL = re.compile(r'(?<=[a-z0-9])(?=[A-Z])')


# ---------------------------------------------------------------------------
# Indexing (the only place the Revit API is read)
# ---------------------------------------------------------------------------

def friendly_view_type(view):
    """ViewType.FloorPlan -> 'Floor Plan', DraftingView -> 'Drafting View'."""
    try:
        return _CAMEL.sub(" ", str(view.ViewType))
    except Exception:
        return ""


def _level_name(view):
    try:
        level = view.GenLevel
        if level is not None:
            return level.Name or ""
    except Exception:
        pass
    return ""


def _param_string(element, built_in):
    try:
        parameter = element.get_Parameter(built_in)
        if parameter is not None:
            return parameter.AsString() or parameter.AsValueString() or ""
    except Exception:
        pass
    return ""


def _sheet_ref(view):
    """'A101 - Ground Floor Plan' when the view is placed on a sheet."""
    number = _param_string(view, BuiltInParameter.VIEWPORT_SHEET_NUMBER)
    if not number:
        return ""
    name = _param_string(view, BuiltInParameter.VIEWPORT_SHEET_NAME)
    return "{} - {}".format(number, name) if name else number


def _template_name(view, doc):
    try:
        template_id = view.ViewTemplateId
        if template_id is None or template_id == ElementId.InvalidElementId:
            return ""
        template = doc.GetElement(template_id)
        return template.Name if template is not None else ""
    except Exception:
        return ""


def _lower(value):
    try:
        return (value or "").lower()
    except Exception:
        return ""


def index_view_item(item, doc):
    """Read view metadata once and build the search index for one row."""
    view = item.Element
    item.ViewTypeName = friendly_view_type(view)
    item.LevelName = _level_name(view)
    item.SheetRef = _sheet_ref(view)
    item.TemplateName = _template_name(view, doc)
    item.Number = item.ViewTypeName
    rebuild_view_search(item)


def rebuild_view_search(item):
    """Re-index after FileName changes (no Revit API access)."""
    item.SearchFields = {
        "type": _lower(item.ViewTypeName),
        "name": _lower(item.Name),
        "level": _lower(item.LevelName),
        "sheet": _lower(item.SheetRef),
        "template": _lower(item.TemplateName) or "none",
        "file": _lower(item.FileName),
    }
    # "none" is only a field keyword (template:none); keep it out of the
    # free-text haystack so typing "none" doesn't match every view.
    item.SearchText = " | ".join(
        value for key, value in item.SearchFields.items()
        if not (key == "template" and not item.TemplateName)
    )


def rebuild_sheet_search(item):
    item.SearchFields = {
        "number": _lower(item.Number),
        "name": _lower(item.Name),
        "file": _lower(item.FileName),
        "filter": _lower(getattr(item, "FilterValues", "")),
    }
    item.SearchText = " | ".join(item.SearchFields.values())


def rebuild_search(item, mode):
    if mode == "Views":
        rebuild_view_search(item)
    else:
        rebuild_sheet_search(item)


# ---------------------------------------------------------------------------
# Query parsing / matching
# ---------------------------------------------------------------------------

class Term(object):
    __slots__ = ("text", "field", "negate")

    def __init__(self, text, field=None, negate=False):
        self.text = text
        self.field = field
        self.negate = negate


def parse_query(query, fields):
    """Split the search box text into Term objects."""
    terms = []
    for match in _TOKEN.finditer(query or ""):
        negate, field, quoted, bare = match.groups()
        text = quoted if quoted is not None else bare
        field = (field or "").lower()
        if field and field not in fields:
            # Not a known field -> treat "abc:def" literally.
            text = "{}:{}".format(field, text)
            field = ""
        text = (text or "").strip().lower()
        if quoted is None:
            # A phrase still being typed ("level 0  -- no closing quote yet)
            # arrives as bare words carrying a stray quote; searching for the
            # quote character itself would blank the list until it is closed.
            text = text.strip('"')
        if not text:
            continue
        if text == "-" and not field:
            continue
        terms.append(Term(text, field or None, bool(negate)))
    return terms


def _term_hit(item, term):
    if term.field:
        return term.text in item.SearchFields.get(term.field, "")
    return term.text in item.SearchText


def matches_query(item, terms):
    for term in terms:
        hit = _term_hit(item, term)
        if term.negate and hit:
            return False
        if not term.negate and not hit:
            return False
    return True


def search(items, query, mode):
    """Free-text stage used by both Sheets and Views mode."""
    fields = VIEW_FIELDS if mode == "Views" else SHEET_FIELDS
    terms = parse_query(query, fields)
    if not terms:
        return list(items)
    # FilterValues change with the sheet filter, so re-index sheets (pure
    # string work, no Revit API).
    if mode != "Views":
        for item in items:
            rebuild_sheet_search(item)
    return [item for item in items if matches_query(item, terms)]


# ---------------------------------------------------------------------------
# Views-mode drop-down filters
# ---------------------------------------------------------------------------

def view_type_options(items):
    names = sorted(set(i.ViewTypeName for i in items if i.ViewTypeName))
    return [ALL_TYPES] + names


def level_options(items):
    names = sorted(set(i.LevelName for i in items if i.LevelName),
                   key=lambda value: value.lower())
    options = [ALL_LEVELS] + names
    if any(not i.LevelName for i in items):
        options.append(NO_LEVEL)
    return options


def filter_views(items, view_type=ALL_TYPES, level=ALL_LEVELS, sheet_state=SHEET_ANY):
    result = []
    for item in items:
        if view_type and view_type != ALL_TYPES and item.ViewTypeName != view_type:
            continue
        if level and level != ALL_LEVELS:
            if level == NO_LEVEL:
                if item.LevelName:
                    continue
            elif item.LevelName != level:
                continue
        if sheet_state == SHEET_PLACED and not item.SheetRef:
            continue
        if sheet_state == SHEET_UNPLACED and item.SheetRef:
            continue
        result.append(item)
    return result

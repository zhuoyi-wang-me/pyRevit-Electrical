# -*- coding: utf-8 -*-
"""Sheet search / parameter filter logic for the pyRevit Export Manager.

This module is deliberately UI-free: no WPF, no XAML, no forms.alert().
All window handling lives in sheet_filter_ui.py, which drives this module
through a small API:

    configure(doc)                  -- set the active Revit document
    set_sheets(sheets)              -- hand over the collected ViewSheets
    parameter_names()               -- filterable sheet parameter names
    parameter_values(name)          -- distinct values found for one name
    read_value(sheet, name)         -- safe single parameter read
    filter_sheets(criteria)         -- ViewSheet -> ViewSheet
    filter_items(items, criteria)   -- ExportItem -> ExportItem
    describe(sheet, criteria)       -- "Discipline = Electrical | ..."

Performance notes (requirement: thousands of sheets)
----------------------------------------------------
Reading a Revit parameter is a managed-to-native call, so doing it once
per sheet per keystroke does not scale.  Instead every parameter is read
at most once per session and cached in a value index:

    {parameter name: {sheet id: display value}}

The first filter on a parameter costs one pass over the sheets; every
later evaluation of that parameter is a dictionary lookup.  Parameter
*discovery* only samples the first PARAMETER_SAMPLE_SIZE sheets, because
sheet category parameters (built-in, project and shared) are defined for
the whole category, not per element.
"""

from Autodesk.Revit.DB import (
    FilteredElementCollector,
    StorageType,
    ViewSheet
)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------
# Kept as plain strings so they can be bound straight into a WPF ComboBox
# and written into a saved-filter JSON file without translation.

OP_ANY_OF = "is any of"
OP_NONE_OF = "is none of"
OP_CONTAINS = "contains"
OP_NOT_CONTAINS = "does not contain"
OP_STARTS_WITH = "starts with"
OP_ENDS_WITH = "ends with"
OP_IS_EMPTY = "is empty"
OP_IS_NOT_EMPTY = "is not empty"

OPERATORS = [
    OP_ANY_OF,
    OP_NONE_OF,
    OP_CONTAINS,
    OP_NOT_CONTAINS,
    OP_STARTS_WITH,
    OP_ENDS_WITH,
    OP_IS_EMPTY,
    OP_IS_NOT_EMPTY
]

# Operators that take their values from the multi-select value list.
VALUE_LIST_OPERATORS = (OP_ANY_OF, OP_NONE_OF)
# Operators that take a single free-typed string.
TEXT_OPERATORS = (OP_CONTAINS, OP_NOT_CONTAINS, OP_STARTS_WITH, OP_ENDS_WITH)
# Operators that need no value at all.
NO_VALUE_OPERATORS = (OP_IS_EMPTY, OP_IS_NOT_EMPTY)

# Stand-in shown in the value list for sheets where the parameter is blank
# or not present at all -- lets the user deliberately target those sheets.
EMPTY_LABEL = "<none>"

# Sheet parameters are category-wide, so a handful of sheets is enough to
# discover them.  Raising this only slows down opening the dialog.
PARAMETER_SAMPLE_SIZE = 25

# Virtual fields that are not real parameters on ViewSheet but that users
# always expect to be able to filter on.  read_value() resolves these
# directly; they mirror the names used by filename_builder.
SHEET_NUMBER = "Sheet Number"
SHEET_NAME = "Sheet Name"
VIRTUAL_PARAMETERS = [SHEET_NUMBER, SHEET_NAME]



# StorageType.None cannot be written directly -- "None" is a Python
# keyword -- so resolve the enum member by name once, here.
STORAGE_NONE = getattr(StorageType, "None", None)


_doc = None
_sheets = []
# {parameter name: {sheet key: display value}}
_value_index = {}
_parameter_names = None


# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------

class FilterCriterion(object):
    """One "parameter <operator> value(s)" rule.

    Attributes are plain fields (not Python properties) so that WPF data
    binding in the DataGrid behaves exactly like helpers.ExportItem does
    elsewhere in this extension.  ValuesText is the display mirror of
    Values and is refreshed by refresh_display().
    """

    def __init__(self, parameter, operator=OP_ANY_OF, values=None):
        self.IsEnabled = True
        self.Parameter = str(parameter or "")
        self.Operator = str(operator or OP_ANY_OF)
        self.Values = [str(value) for value in (values or [])]
        self.ValuesText = ""
        self.refresh_display()

    def refresh_display(self):
        """Rebuild the human-readable value column for the filter grid."""
        if self.Operator in NO_VALUE_OPERATORS:
            self.ValuesText = "--"
        else:
            self.ValuesText = ", ".join(self.Values) or "--"
        return self.ValuesText

    def label(self):
        """Short text form, e.g. 'Discipline is any of Electrical'."""
        if self.Operator in NO_VALUE_OPERATORS:
            return "{} {}".format(self.Parameter, self.Operator)
        return "{} {} {}".format(self.Parameter, self.Operator, self.ValuesText)


def enabled_criteria(criteria):
    """Drop unticked rows so the grid can keep them for later re-use."""
    active = []
    for criterion in criteria or []:
        try:
            if criterion.IsEnabled and criterion.Parameter:
                active.append(criterion)
        except Exception:
            pass
    return active


# ---------------------------------------------------------------------------
# Context / caching
# ---------------------------------------------------------------------------

def configure(doc):
    """Set the active Revit document before using this module."""
    global _doc
    _doc = doc
    invalidate()


def _require_context():
    if _doc is None:
        raise RuntimeError("Call sheet_filter.configure(doc) first.")


def invalidate(parameter=None):
    """Drop cached values (all of them, or just one parameter's)."""
    global _value_index, _parameter_names
    if parameter is None:
        _value_index = {}
        _parameter_names = None
    else:
        _value_index.pop(parameter, None)


def collect_sheets(doc=None):
    """Return every ViewSheet in the document, sheet-number ordered.

    Placeholder sheets are kept: they are legitimate rows in a sheet list
    even though they cannot be exported.  The export engines already skip
    anything that is not printable.
    """
    target = doc or _doc
    if target is None:
        raise RuntimeError("Call sheet_filter.configure(doc) first.")
    sheets = list(
        FilteredElementCollector(target).OfClass(ViewSheet).ToElements()
    )
    sheets.sort(key=lambda sheet: _sheet_number(sheet).lower())
    return sheets


def set_sheets(sheets):
    """Register the sheet collection the UI is working with and reset the
    caches.  Call this whenever the sheet list is (re)loaded."""
    global _sheets
    _sheets = list(sheets or [])
    invalidate()
    return _sheets


def sheets():
    return _sheets


def sheet_count():
    return len(_sheets)


def _sheet_number(sheet):
    try:
        return sheet.SheetNumber or ""
    except Exception:
        return ""


def _sheet_key(sheet):
    """Stable cache key for a sheet.

    ElementId.IntegerValue is deprecated from Revit 2024 onward and
    ElementId.Value is not present before it, so try both.
    """
    try:
        return sheet.Id.Value
    except Exception:
        pass
    try:
        return sheet.Id.IntegerValue
    except Exception:
        return id(sheet)


# ---------------------------------------------------------------------------
# Reusable parameter retrieval
# ---------------------------------------------------------------------------

def _parameter_display(parameter):
    """Best-effort display string for any parameter storage type.

    AsValueString() is preferred for non-text parameters because it
    honours the project's units and resolves Yes/No and key-schedule
    values the way the Revit UI shows them.
    """
    if parameter is None:
        return ""

    try:
        storage = parameter.StorageType
    except Exception:
        storage = None

    try:
        if storage == StorageType.String:
            return parameter.AsString() or ""

        text = parameter.AsValueString()
        if text:
            return text

        if storage == StorageType.Integer:
            return str(parameter.AsInteger())
        if storage == StorageType.Double:
            return str(parameter.AsDouble())
        if storage == StorageType.ElementId:
            element_id = parameter.AsElementId()
            if element_id is None:
                return ""
            element = _doc.GetElement(element_id) if _doc else None
            if element is None:
                return ""
            return getattr(element, "Name", "") or ""
    except Exception:
        pass

    # Last resort: a plain text read, which is what most sheet
    # parameters are anyway.
    try:
        return parameter.AsString() or ""
    except Exception:
        return ""


def read_value(sheet, parameter_name):
    """Read one parameter from one sheet and never raise.

    Requirement 7 (missing parameters) is handled here: a parameter that
    does not exist on this sheet, or exists but has no value, both return
    an empty string, which the operators treat as "empty".
    """
    if sheet is None or not parameter_name:
        return ""

    try:
        if parameter_name == SHEET_NUMBER:
            return _sheet_number(sheet)
        if parameter_name == SHEET_NAME:
            try:
                return sheet.Name or ""
            except Exception:
                return ""
        return _parameter_display(sheet.LookupParameter(parameter_name))
    except Exception:
        # Corrupt or workshared-locked element: treat as no value rather
        # than breaking the whole filter pass.
        return ""


def _index_for(parameter_name):
    """Return (and lazily build) the {sheet key: value} map for one
    parameter.  This is the single place a parameter is ever read."""
    cached = _value_index.get(parameter_name)
    if cached is not None:
        return cached

    values = {}
    for sheet in _sheets:
        values[_sheet_key(sheet)] = read_value(sheet, parameter_name)
    _value_index[parameter_name] = values
    return values


def cached_value(sheet, parameter_name):
    """Cached read used by the filter loop.

    Falls back to a direct read for sheets that are not part of the
    registered collection (for example the active-view export item).
    """
    index = _index_for(parameter_name)
    key = _sheet_key(sheet)
    if key in index:
        return index[key]
    value = read_value(sheet, parameter_name)
    index[key] = value
    return value


# ---------------------------------------------------------------------------
# Parameter and value discovery
# ---------------------------------------------------------------------------

def parameter_names(refresh=False):
    """Filterable sheet parameter names: built-in, project and shared.

    Only the first PARAMETER_SAMPLE_SIZE sheets are inspected because
    sheet parameters are defined per category, not per element.  Any
    parameter with no storage type is skipped -- there is nothing to
    compare against.
    """
    global _parameter_names
    if _parameter_names is not None and not refresh:
        return _parameter_names

    names = set(VIRTUAL_PARAMETERS)

    for sheet in _sheets[:PARAMETER_SAMPLE_SIZE]:
        try:
            for parameter in sheet.Parameters:
                try:
                    definition = parameter.Definition
                    if definition is None or not definition.Name:
                        continue
                    if STORAGE_NONE is not None and \
                            parameter.StorageType == STORAGE_NONE:
                        continue
                    names.add(definition.Name)
                except Exception:
                    # Skip the individual parameter, keep the sheet.
                    pass
        except Exception:
            # Skip the individual sheet, keep going.
            pass

    _parameter_names = sorted(names, key=lambda value: value.lower())
    return _parameter_names


def parameter_values(parameter_name, include_empty=True):
    """Distinct values present in the model for one parameter, sorted.

    Blank values collapse into a single EMPTY_LABEL entry so the user can
    filter for "sheets where Status has not been filled in yet".
    """
    if not parameter_name:
        return []

    index = _index_for(parameter_name)
    found = set()
    has_empty = False
    for value in index.values():
        if value:
            found.add(value)
        else:
            has_empty = True

    values = sorted(found, key=lambda value: value.lower())
    if has_empty and include_empty:
        values.insert(0, EMPTY_LABEL)
    return values


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _matches_criterion(sheet, criterion):
    """Evaluate a single rule against a single sheet."""
    value = cached_value(sheet, criterion.Parameter)
    operator = criterion.Operator

    if operator == OP_IS_EMPTY:
        return not value
    if operator == OP_IS_NOT_EMPTY:
        return bool(value)

    if operator in VALUE_LIST_OPERATORS:
        # Values inside one criterion are OR-ed: "Discipline is any of
        # Electrical, Mechanical".  EMPTY_LABEL stands for a blank value.
        wanted = [str(item).lower() for item in criterion.Values]
        if not wanted:
            return True  # nothing chosen yet -- do not hide anything
        current = value.lower()
        hit = current in wanted or (not value and EMPTY_LABEL in wanted)
        return hit if operator == OP_ANY_OF else not hit

    # Text operators use the first value only.
    needle = (criterion.Values[0] if criterion.Values else "").lower()
    if not needle:
        return True
    haystack = value.lower()

    if operator == OP_CONTAINS:
        return needle in haystack
    if operator == OP_NOT_CONTAINS:
        return needle not in haystack
    if operator == OP_STARTS_WITH:
        return haystack.startswith(needle)
    if operator == OP_ENDS_WITH:
        return haystack.endswith(needle)

    # Unknown operator: fail open rather than hiding every sheet.
    return True


def matches(sheet, criteria):
    """True when a sheet satisfies every enabled criterion (AND logic)."""
    for criterion in enabled_criteria(criteria):
        try:
            if not _matches_criterion(sheet, criterion):
                return False
        except Exception:
            # A rule that cannot be evaluated must not silently drop the
            # sheet; report it as non-matching only for that rule.
            return False
    return True


def filter_sheets(criteria, sheet_list=None):
    """Return the ViewSheets matching all enabled criteria."""
    active = enabled_criteria(criteria)
    candidates = sheet_list if sheet_list is not None else _sheets
    if not active:
        return list(candidates)

    # Warm each parameter's cache once, before the per-sheet loop.
    for criterion in active:
        _index_for(criterion.Parameter)

    return [sheet for sheet in candidates if matches(sheet, active)]


def filter_items(items, criteria):
    """Same as filter_sheets() but for helpers.ExportItem wrappers, which
    is what the selection grid is actually bound to."""
    active = enabled_criteria(criteria)
    if not active:
        return list(items or [])

    for criterion in active:
        _index_for(criterion.Parameter)

    matched = []
    for item in items or []:
        element = getattr(item, "Element", None)
        if element is None:
            continue
        if matches(element, active):
            matched.append(item)
    return matched


def describe(sheet, criteria):
    """Filter-parameter values for one sheet, for the grid's value column.

    Example: "Discipline = Electrical | Status = IFC".  Parameters with
    no value are shown as EMPTY_LABEL so a blank cell is never ambiguous.
    """
    parts = []
    seen = set()
    for criterion in enabled_criteria(criteria):
        name = criterion.Parameter
        if name in seen:
            continue
        seen.add(name)
        value = cached_value(sheet, name)
        parts.append("{} = {}".format(name, value or EMPTY_LABEL))
    return "  |  ".join(parts)


def summary_text(matched, total):
    """One-line result summary for the UI."""
    if not total:
        return "No sheets found in this model."
    if matched == total:
        return "Showing all {} sheet(s) -- no filter applied.".format(total)
    return "Matched {} of {} sheet(s).".format(matched, total)

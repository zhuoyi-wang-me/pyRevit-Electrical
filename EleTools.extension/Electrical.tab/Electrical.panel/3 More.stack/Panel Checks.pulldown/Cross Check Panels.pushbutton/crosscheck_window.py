# -*- coding: utf-8 -*-
"""Cross Check MEP Elements vs Detail Items (pyRevit, modeless WPF).

Workflow:
  1. Tick one or more MEP categories (Electrical / Mechanical / Plumbing),
     pick a scope, click "Collect Elements".
  2. Optionally narrow by Family and/or Level (leave blank = include all)
     and by the Advanced Filter rows (schedule-style AND filters).
  3. Build the pairing key from one or more field pairs, and the
     comparison from one or more field pairs + condition.
  4. "Run Cross Check" checks BOTH directions:
       MEP element -> Detail Item : MATCH / PARTIAL MATCH / NOT MATCH /
                                    NO MATCH / AMBIGUOUS
       Detail Item -> MEP element : ORPHAN DETAIL ITEM
     Select rows (or none = all) and use Select / Zoom To / Isolate.
  5. Export CSV, or save the whole setup as a named template.

Revit 2022 - 2027 compatible (ElementId.Value / IntegerValue handled).
"""
import os
import re
import json
import traceback

from pyrevit import forms, script
from Autodesk.Revit.DB import (BuiltInCategory, BuiltInParameter, FilteredElementCollector,
                               ElementMulticategoryFilter, FamilyInstance, StorageType,
                               ElementId, Element, Transaction, TemporaryViewMode)
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent
from System.Collections.Generic import List
from System.Windows import Thickness, CornerRadius, FontWeights
from System.Windows.Controls import Border, TextBlock
from System.Windows.Data import CollectionViewSource
from System.Windows.Input import Cursors
from System.Windows.Media import BrushConverter
from System.Windows.Forms import SaveFileDialog, DialogResult
from System.IO import File
from System.Text import Encoding
from System import DateTime

# The window is modeless, so every doc/uidoc/Element call made from a
# button click is marshalled into Revit's API context via an ExternalEvent.
# The persistent engine keeps this script's IronPython engine alive for as
# long as the window is open.
#
# NOTE: there is deliberately NO module-level `doc` / `uidoc`. They used to
# be captured once at launch, which went stale as soon as the user switched
# documents while the window stayed open. The document is now always taken
# from the live UIApplication inside the ExternalEvent (see ApiQueueHandler).
#
# This is a MODULE imported by script.py, not the button script itself:
# pyRevit clears the global names of a button script when it ends, which
# broke every handler of the still-open window.

config = script.get_config()

TOOL_TITLE = "Cross Check MEP"
LAST_USED_KEY = "__last_used__"
SCOPE_ACTIVE = "Active View only"
SCOPE_PROJECT = "Entire Project"
# The single-line-diagram case: the panels are in the model, the symbols are
# Detail Items in the drafting view that is open.
SCOPE_SPLIT = "Model: project / Detail Items: active view"
SCOPE_OPTIONS = [SCOPE_ACTIVE, SCOPE_PROJECT, SCOPE_SPLIT]
NO_LEVEL = "<No Level>"
ALL_STATUSES = "ALL"
STATUS_ORDER = ["MATCH", "PARTIAL MATCH", "NOT MATCH", "NO MATCH", "AMBIGUOUS", "ORPHAN DETAIL ITEM"]
# status -> (light fill, dark text, full tint). The first two are the same as
# the status pills of the grid in ui.xaml; the tint fills the chip that is
# the active filter. iOS system colours.
STATUS_COLOURS = {
    ALL_STATUSES: ("#EFEFF4", "#3C3C43", "#1C1C1E"),
    "MATCH": ("#DDF6E3", "#1B7A34", "#34C759"),
    "PARTIAL MATCH": ("#DCEBFF", "#0058C7", "#007AFF"),
    "NOT MATCH": ("#FFE1DF", "#C4231A", "#FF3B30"),
    "NO MATCH": ("#FFEFD6", "#A85F00", "#FF9500"),
    "AMBIGUOUS": ("#F1E3FA", "#7E2FA8", "#AF52DE"),
    "ORPHAN DETAIL ITEM": ("#D9F2F7", "#17758A", "#30B0C7"),
}
MAX_IDS_IN_NOTE = 10


# ---------------------------------------------------------------------------
# MEP category registry. Enum members that don't exist in the running Revit
# version are skipped (e.g. Plumbing Equipment is 2024+), so one script runs
# on every version. The first two labels are unchanged from the old tool so
# templates saved by the previous version still load.
# ---------------------------------------------------------------------------
_MEP_CATEGORY_DEFS = [
    ("Electrical", "Electrical Equipment", "OST_ElectricalEquipment"),
    ("Electrical", "Electrical Fixtures", "OST_ElectricalFixtures"),
    ("Electrical", "Lighting Fixtures", "OST_LightingFixtures"),
    ("Electrical", "Lighting Devices", "OST_LightingDevices"),
    ("Electrical", "Communication Devices", "OST_CommunicationDevices"),
    ("Electrical", "Data Devices", "OST_DataDevices"),
    ("Electrical", "Fire Alarm Devices", "OST_FireAlarmDevices"),
    ("Electrical", "Nurse Call Devices", "OST_NurseCallDevices"),
    ("Electrical", "Security Devices", "OST_SecurityDevices"),
    ("Electrical", "Telephone Devices", "OST_TelephoneDevices"),
    ("Electrical", "Audio Visual Devices", "OST_AudioVisualDevices"),
    ("Electrical", "Conduits", "OST_Conduit"),
    ("Electrical", "Conduit Fittings", "OST_ConduitFitting"),
    ("Electrical", "Cable Trays", "OST_CableTray"),
    ("Electrical", "Cable Tray Fittings", "OST_CableTrayFitting"),
    ("Mechanical", "Mechanical Equipment", "OST_MechanicalEquipment"),
    ("Mechanical", "Mechanical Control Devices", "OST_MechanicalControlDevices"),
    ("Mechanical", "Air Terminals", "OST_DuctTerminal"),
    ("Mechanical", "Ducts", "OST_DuctCurves"),
    ("Mechanical", "Duct Fittings", "OST_DuctFitting"),
    ("Mechanical", "Duct Accessories", "OST_DuctAccessory"),
    ("Mechanical", "Flex Ducts", "OST_FlexDuctCurves"),
    ("Plumbing", "Plumbing Fixtures", "OST_PlumbingFixtures"),
    ("Plumbing", "Plumbing Equipment", "OST_PlumbingEquipment"),
    ("Plumbing", "Pipes", "OST_PipeCurves"),
    ("Plumbing", "Pipe Fittings", "OST_PipeFitting"),
    ("Plumbing", "Pipe Accessories", "OST_PipeAccessory"),
    ("Plumbing", "Flex Pipes", "OST_FlexPipeCurves"),
    ("Plumbing", "Sprinklers", "OST_Sprinklers"),
]

CATEGORY_MAP = {}       # label -> BuiltInCategory
CATEGORY_GROUP = {}     # label -> discipline
CATEGORY_ORDER = []     # labels in display order
for _group, _label, _enum_name in _MEP_CATEGORY_DEFS:
    _bic = getattr(BuiltInCategory, _enum_name, None)
    if _bic is not None:
        CATEGORY_MAP[_label] = _bic
        CATEGORY_GROUP[_label] = _group
        CATEGORY_ORDER.append(_label)

DEFAULT_CATEGORIES = ["Electrical Equipment", "Electrical Fixtures"]


# ---------------------------------------------------------------------------
# Revit API compatibility helpers
# ---------------------------------------------------------------------------
def eid_int(element_id):
    """ElementId -> int. IntegerValue was deprecated in Revit 2024 and
    removed in 2026; .Value is the replacement."""
    if element_id is None:
        return -1
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue


def elem_name(element):
    """element.Name can raise in IronPython for some ElementType subclasses
    (ambiguous property on the type hierarchy). Fall back to the base getter."""
    if element is None:
        return ""
    try:
        return element.Name or ""
    except Exception:
        try:
            return Element.Name.GetValue(element) or ""
        except Exception:
            return ""


def type_of(element):
    """Works for FamilyInstances AND system families (Duct, Pipe, Conduit,
    Cable Tray...), which have no .Symbol."""
    try:
        type_id = element.GetTypeId()
        if type_id is None or type_id == ElementId.InvalidElementId:
            return None
        return element.Document.GetElement(type_id)
    except Exception:
        return None


def is_alive(element):
    try:
        return element is not None and element.IsValidObject
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Search + checkbox list
# ---------------------------------------------------------------------------
class CheckableItem(object):
    def __init__(self, label, group=""):
        self.Label = label
        self.Group = group
        self.IsChecked = False


class CheckList(object):
    """Search TextBox + checkbox ListBox + optional All/None buttons."""
    def __init__(self, listbox, search_box, select_all_btn=None, select_none_btn=None):
        self.listbox = listbox
        self.search_box = search_box
        self.items = []
        search_box.TextChanged += self._on_search
        if select_all_btn:
            select_all_btn.Click += self._on_select_all
        if select_none_btn:
            select_none_btn.Click += self._on_select_none

    def set_labels(self, labels, groups=None):
        groups = groups or {}
        self.items = [CheckableItem(label, groups.get(label, "")) for label in labels]
        self.search_box.Text = ""
        self.listbox.ItemsSource = self.items

    def set_checked(self, labels):
        wanted = set(labels)
        for item in self.items:
            item.IsChecked = item.Label in wanted
        self._refresh_bindings()

    def check_group(self, group):
        for item in self.items:
            if item.Group == group:
                item.IsChecked = True
        self._refresh_bindings()

    def checked_labels(self):
        return [item.Label for item in self.items if item.IsChecked]

    def _apply_filter(self):
        view = CollectionViewSource.GetDefaultView(self.listbox.ItemsSource)
        if view is None:
            return
        text = (self.search_box.Text or "").strip().lower()
        if text:
            view.Filter = lambda obj, needle=text: (needle in obj.Label.lower()
                                                   or needle in (obj.Group or "").lower())
        else:
            view.Filter = None
        view.Refresh()

    def _refresh_bindings(self):
        items = self.items
        self.listbox.ItemsSource = None
        self.listbox.ItemsSource = items
        self._apply_filter()

    def _on_search(self, sender, args):
        self._apply_filter()

    def _on_select_all(self, sender, args):
        for item in self.items:
            item.IsChecked = True
        self._refresh_bindings()

    def _on_select_none(self, sender, args):
        for item in self.items:
            item.IsChecked = False
        self._refresh_bindings()


# ---------------------------------------------------------------------------
# Parameter access. Instance parameter first, then TYPE parameter - the old
# version only looked at instance parameters, so type-level MEP data
# (Type Mark, Manufacturer, Model, many voltage/flow params) never matched.
# ---------------------------------------------------------------------------
def find_param(element, name):
    parameter = element.LookupParameter(name)
    if parameter is not None:
        return parameter
    etype = type_of(element)
    if etype is not None:
        return etype.LookupParameter(name)
    return None


def param_to_string(parameter):
    if parameter is None or not parameter.HasValue:
        return ""
    if parameter.StorageType == StorageType.String:
        return parameter.AsString() or ""
    shown_value = parameter.AsValueString()
    if shown_value:
        return shown_value
    if parameter.StorageType == StorageType.Integer:
        return str(parameter.AsInteger())
    if parameter.StorageType == StorageType.Double:
        return str(parameter.AsDouble())
    if parameter.StorageType == StorageType.ElementId:
        return str(eid_int(parameter.AsElementId()))
    return ""


BUILTIN_FIELD_KEYS = ("element_name", "family_name", "type_name", "category_name")


class ParamChoice(object):
    def __init__(self, key, label):
        self.key = key
        self.label = label

    def __str__(self):
        return self.label

    def value(self, element):
        if self.key is None:
            return ""
        if self.key == "element_name":
            return elem_name(element)
        if self.key == "family_name":
            return family_name_of(element)
        if self.key == "type_name":
            return elem_name(type_of(element))
        if self.key == "category_name":
            return category_name_of(element)
        try:
            return param_to_string(find_param(element, self.key))
        except Exception:
            return ""

    def exists(self, element):
        if self.key in BUILTIN_FIELD_KEYS:
            return True
        if self.key is None:
            return False
        return find_param(element, self.key) is not None


NONE_CHOICE = ParamChoice(None, "(none)")


def _add_param_names(element, names):
    if element is None:
        return
    for parameter in element.Parameters:
        try:
            if parameter.Definition and parameter.Definition.Name:
                names.add(parameter.Definition.Name)
        except Exception:
            pass


def choices_for(elements):
    """Instance + type parameter names. Samples ONE element per type -
    iterating every parameter of every element was the main slowdown on
    large MEP models (thousands of fittings)."""
    names = set()
    seen_types = set()
    for element in elements:
        type_key = eid_int(element.GetTypeId())
        if type_key in seen_types:
            continue
        seen_types.add(type_key)
        _add_param_names(element, names)
        _add_param_names(type_of(element), names)
    result = [ParamChoice("element_name", "Element Name"),
              ParamChoice("family_name", "Family Name"),
              ParamChoice("type_name", "Type Name"),
              ParamChoice("category_name", "Category")]
    result.extend([ParamChoice(name, name) for name in sorted(names, key=lambda x: x.lower())])
    return result


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------
CONDITION_OPTIONS = [
    "Equals", "Does Not Equal", "Numerically Equals",
    "Is Greater Than", "Is Greater Than Or Equal To",
    "Is Less Than", "Is Less Than Or Equal To",
    "Contains", "Does Not Contain",
    "Begins With", "Does Not Begin With",
    "Ends With", "Does Not End With",
    "Has A Value", "Has No Value", "Parameter Exists",
]
DEFAULT_CONDITION = "Equals"
# Conditions that only look at the MEP side, so they are still meaningful
# when there is no Detail Item to compare against.
UNARY_CONDITIONS = ("Parameter Exists", "Has A Value", "Has No Value")

# First number in a string, tolerating unit suffixes ("208 V", "1,200 VA").
_NUMBER_RE = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?|[-+]?\.\d+")


def to_number(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        pass
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except Exception:
        return None


def _numeric(a, b, op):
    af, bf = to_number(a), to_number(b)
    if af is None or bf is None:
        return False
    return op(af, bf)


def evaluate_condition(evalue, dvalue, condition, ignore_case):
    a = evalue or ""
    b = dvalue or ""
    a_cmp = a.lower() if ignore_case else a
    b_cmp = b.lower() if ignore_case else b

    if condition == "Equals":
        return a_cmp == b_cmp
    if condition == "Does Not Equal":
        return a_cmp != b_cmp
    if condition == "Numerically Equals":
        return _numeric(a, b, lambda x, y: abs(x - y) <= 1e-6 * max(1.0, abs(x), abs(y)))
    # An empty text is "contained in" / "begins" / "ends" every text, which
    # reported a tick whenever the Detail Item value was blank.
    if not b_cmp and condition in ("Contains", "Begins With", "Ends With"):
        return False
    if condition == "Contains":
        return b_cmp in a_cmp
    if condition == "Does Not Contain":
        return b_cmp not in a_cmp
    if condition == "Begins With":
        return a_cmp.startswith(b_cmp)
    if condition == "Does Not Begin With":
        return not a_cmp.startswith(b_cmp)
    if condition == "Ends With":
        return a_cmp.endswith(b_cmp)
    if condition == "Does Not End With":
        return not a_cmp.endswith(b_cmp)
    if condition == "Is Greater Than":
        return _numeric(a, b, lambda x, y: x > y)
    if condition == "Is Greater Than Or Equal To":
        return _numeric(a, b, lambda x, y: x >= y)
    if condition == "Is Less Than":
        return _numeric(a, b, lambda x, y: x < y)
    if condition == "Is Less Than Or Equal To":
        return _numeric(a, b, lambda x, y: x <= y)
    if condition == "Has A Value":
        return bool(a.strip())
    if condition == "Has No Value":
        return not a.strip()
    return a_cmp == b_cmp


def evaluate_field_condition(field, element, condition, ignore_case, compare_value=None,
                             compare_field=None, compare_element=None):
    if condition == "Parameter Exists":
        return field.exists(element)
    actual = field.value(element).strip()
    if compare_field is not None and compare_element is not None:
        target = compare_field.value(compare_element).strip()
    else:
        target = compare_value or ""
    return evaluate_condition(actual, target, condition, ignore_case)


def select_condition(combo, condition=None):
    combo.SelectedItem = condition if condition in CONDITION_OPTIONS else DEFAULT_CONDITION


# ---------------------------------------------------------------------------
# Advanced filter rows
# ---------------------------------------------------------------------------
class FilterRow(object):
    def __init__(self, param_combo, condition_combo, value_box):
        self.param_combo = param_combo
        self.condition_combo = condition_combo
        self.value_box = value_box

    def set_fields(self, fields):
        self.param_combo.ItemsSource = [NONE_CHOICE] + list(fields)
        self.param_combo.SelectedIndex = 0
        self.condition_combo.ItemsSource = CONDITION_OPTIONS
        select_condition(self.condition_combo)
        self.value_box.Text = ""

    def is_active(self):
        field = self.param_combo.SelectedItem
        return field is not None and field.key is not None

    def matches(self, element, ignore_case):
        if not self.is_active():
            return True
        condition = self.condition_combo.SelectedItem or DEFAULT_CONDITION
        return evaluate_field_condition(self.param_combo.SelectedItem, element, condition,
                                        ignore_case, compare_value=self.value_box.Text or "")

    def get_state(self):
        if not self.is_active():
            return None
        return {"param": self.param_combo.SelectedItem.label,
                "condition": self.condition_combo.SelectedItem or DEFAULT_CONDITION,
                "value": self.value_box.Text or ""}

    def set_state(self, state):
        if not state:
            self.param_combo.SelectedIndex = 0
            select_condition(self.condition_combo)
            self.value_box.Text = ""
            return
        for item in self.param_combo.ItemsSource or []:
            if getattr(item, "label", None) == state.get("param"):
                self.param_combo.SelectedItem = item
                break
        select_condition(self.condition_combo, state.get("condition"))
        self.value_box.Text = state.get("value", "")


class FilterPanel(object):
    def __init__(self, rows):
        self.rows = rows

    def set_fields(self, fields):
        for row in self.rows:
            row.set_fields(fields)

    def apply(self, elements, ignore_case):
        active_rows = [row for row in self.rows if row.is_active()]
        if not active_rows:
            return elements
        return [e for e in elements if all(row.matches(e, ignore_case) for row in active_rows)]

    def get_state(self):
        return [row.get_state() for row in self.rows]

    def set_state(self, states):
        states = states or [None] * len(self.rows)
        for row, state in zip(self.rows, states):
            row.set_state(state)


def normalize(value):
    return (value or "").strip().lower()


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
def _keep(element, exclude_nested):
    if element.Category is None:
        return False
    if exclude_nested and isinstance(element, FamilyInstance):
        try:
            if element.SuperComponent is not None:
                return False
        except Exception:
            pass
    return True


def _collector(doc, view_id):
    return FilteredElementCollector(doc, view_id) if view_id else FilteredElementCollector(doc)


def collect_mep_elements(doc, view_id, categories, exclude_nested):
    cat_filter = ElementMulticategoryFilter(List[BuiltInCategory](categories))
    elements = _collector(doc, view_id).WherePasses(cat_filter).WhereElementIsNotElementType()
    return [e for e in elements if _keep(e, exclude_nested)]


def collect_detail_items(doc, view_id, exclude_nested):
    # OfClass(FamilyInstance): the Detail Items category also contains Filled
    # Regions and Repeating Details, which were showing up as "<No Family>"
    # orphans in the old version.
    elements = (_collector(doc, view_id)
                .OfClass(FamilyInstance)
                .OfCategory(BuiltInCategory.OST_DetailComponents))
    return [e for e in elements if _keep(e, exclude_nested)]


# ---------------------------------------------------------------------------
# Family / category / level resolution (cached per collection run)
# ---------------------------------------------------------------------------
_family_cache = {}
_level_cache = {}


def clear_caches():
    _family_cache.clear()
    _level_cache.clear()


def family_name_of(element):
    key = eid_int(element.Id)
    if key in _family_cache:
        return _family_cache[key]
    result = "<No Family>"
    etype = type_of(element)
    if etype is not None:
        try:
            result = etype.FamilyName or "<Unnamed Family>"
        except Exception:
            result = "<Unnamed Family>"
    _family_cache[key] = result
    return result


def category_name_of(element):
    try:
        return element.Category.Name if element.Category else ""
    except Exception:
        return ""


_LEVEL_BIPS = [getattr(BuiltInParameter, name, None) for name in (
    "INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM",   # Schedule Level
    "FAMILY_LEVEL_PARAM",                   # Level (hosted families)
    "INSTANCE_REFERENCE_LEVEL_PARAM",       # Reference Level
    "RBS_START_LEVEL_PARAM",                # Reference Level on ducts/pipes/conduit/tray
    "SCHEDULE_LEVEL_PARAM",
)]
_LEVEL_BIPS = [bip for bip in _LEVEL_BIPS if bip is not None]


def _level_name(doc, level_id):
    if level_id is None or eid_int(level_id) <= 0:
        return None
    level = doc.GetElement(level_id)
    return elem_name(level) if level is not None else None


def level_of(element, _depth=0):
    element_id = eid_int(element.Id)
    if element_id in _level_cache:
        return _level_cache[element_id]

    doc = element.Document
    result = None

    try:
        result = _level_name(doc, element.LevelId)
    except Exception:
        pass

    # Detail Items are view-specific and have no LevelId: use the level of
    # the view that owns them (plan views). Drafting views have none.
    if not result:
        try:
            owner_view_id = element.OwnerViewId
            if eid_int(owner_view_id) > 0:
                owner_view = doc.GetElement(owner_view_id)
                gen_level = getattr(owner_view, "GenLevel", None)
                if gen_level is not None:
                    result = elem_name(gen_level)
        except Exception:
            pass

    # Built-in parameters are language-independent (the old name lookup
    # only worked in English Revit).
    if not result:
        for bip in _LEVEL_BIPS:
            try:
                parameter = element.get_Parameter(bip)
                if parameter and parameter.HasValue and parameter.StorageType == StorageType.ElementId:
                    result = _level_name(doc, parameter.AsElementId())
                    if result:
                        break
            except Exception:
                pass

    if not result:
        for param_name in ("Schedule Level", "Reference Level", "Level"):
            try:
                parameter = element.LookupParameter(param_name)
                if parameter and parameter.HasValue:
                    if parameter.StorageType == StorageType.ElementId:
                        result = _level_name(doc, parameter.AsElementId())
                    else:
                        result = parameter.AsValueString() or parameter.AsString()
                    if result:
                        break
            except Exception:
                pass

    if not result and _depth < 3:
        try:
            host = getattr(element, "Host", None)
            if host is not None and eid_int(host.Id) != element_id:
                host_level = level_of(host, _depth + 1)
                if host_level != "<No Level>":
                    result = host_level
        except Exception:
            pass

    result = result or NO_LEVEL
    _level_cache[element_id] = result
    return result


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def csv_value(value):
    text = value if value is not None else ""
    try:
        text = unicode(text)  # noqa: F821  (IronPython 2.7)
    except NameError:
        text = str(text)
    return u'"{}"'.format(text.replace(u'"', u'""'))


def export_csv(header, rows):
    dialog = SaveFileDialog()
    dialog.Title = "Export Cross Check List"
    dialog.Filter = "CSV files (*.csv)|*.csv"
    dialog.FileName = "MEP_Cross_Check_{}.csv".format(DateTime.Now.ToString("yyyyMMdd_HHmm"))
    if dialog.ShowDialog() != DialogResult.OK:
        return None
    content = [u','.join([csv_value(item) for item in header])]
    for row in rows:
        content.append(u','.join([csv_value(item) for item in row]))
    # UTF-8 with BOM so Excel shows the check marks / non-ASCII names correctly.
    File.WriteAllText(dialog.FileName, u"\r\n".join(content), Encoding.UTF8)
    return dialog.FileName


# ---------------------------------------------------------------------------
# Result row
# ---------------------------------------------------------------------------
CHECK = u"\u2713 "
CROSS = u"\u2717 "
ARROW = u"\u2192"


def _label_for(element):
    if element is None:
        return "-"
    return u"{} (Id {})".format(elem_name(element), eid_int(element.Id))


class ResultRow(object):
    """One grid row. Either side may be None (orphan). extra_ids carries
    the candidate Detail Items of an AMBIGUOUS row so Select/Zoom can show them."""
    def __init__(self, status, mep, detail, key_text, comparisons, notes,
                 mep_level, detail_level, extra_ids=None):
        self.Status = status
        self.Category = category_name_of(mep) if mep is not None else ""
        self.MepElement = _label_for(mep)
        self.MepLevel = mep_level
        self.Detail = _label_for(detail)
        self.DetailLevel = detail_level
        self.MatchKey = key_text
        self.FieldComparison = u" | ".join(
            [u"{}{} [{}]: {}{}{}".format(CHECK if c["equal"] else CROSS, c["label"], c["condition"],
                                         c["evalue"] or "<blank>", ARROW, c["dvalue"] or "<blank>")
             for c in comparisons]) if comparisons else "-"
        self.Notes = notes
        self.MepId = mep.Id if mep is not None else None
        self.DetailId = detail.Id if detail is not None else None
        self.ExtraIds = list(extra_ids or [])
        self.comparisons = comparisons


# ---------------------------------------------------------------------------
# ExternalEvent handler with a queue
# ---------------------------------------------------------------------------
class ApiQueueHandler(IExternalEventHandler):
    """Runs queued callables inside Revit's API context. Each callable
    receives the CURRENT UIDocument. A queue (instead of the old single
    `action` slot) means a second click before Revit got round to the first
    one no longer silently drops the first request."""
    def __init__(self, ensure_globals):
        self.queue = []
        self._ensure_globals = ensure_globals
        self.on_error = None

    def Execute(self, uiapp):
        self._ensure_globals()
        pending, self.queue = self.queue, []
        uidoc = uiapp.ActiveUIDocument
        for label, action in pending:
            if uidoc is None:
                forms.alert("No active Revit document.", title=TOOL_TITLE, warn_icon=True)
                return
            try:
                action(uidoc)
            except Exception:
                details = traceback.format_exc()
                if self.on_error:
                    try:
                        self.on_error(label)
                    except Exception:
                        pass
                try:
                    forms.alert("'{}' failed.".format(label), sub_msg=details,
                                title=TOOL_TITLE, warn_icon=True)
                except Exception:
                    pass

    def GetName(self):
        return "Cross Check MEP - API Queue"


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class CrossCheckWindow(forms.WPFWindow):
    def __init__(self, xaml_file, ensure_globals):
        forms.WPFWindow.__init__(self, xaml_file)
        self._ensure_globals = ensure_globals
        self._ensure_globals()

        self.event_handler = ApiQueueHandler(ensure_globals)
        self.event_handler.on_error = lambda label: self.set_status("'{}' failed - see message.".format(label))
        self.ext_event = ExternalEvent.Create(self.event_handler)
        self.Closed += self._on_closed

        self.collected_doc = None
        self.mep_elements = []
        self.detail_items = []
        self.source_fields = []
        self.detail_fields = []
        self.key_pairs = []      # [(ParamChoice, ParamChoice)]
        self.value_pairs = []    # [(ParamChoice, ParamChoice, condition)]
        self.results = []        # every row of the last run
        self.shown_results = []  # the rows that pass the status chip + search
        self.status_filter = ALL_STATUSES

        self.cmbScope.ItemsSource = SCOPE_OPTIONS
        self.cmbScope.SelectedIndex = 1
        self.cmbValCondition.ItemsSource = CONDITION_OPTIONS
        select_condition(self.cmbValCondition)

        self.categories = CheckList(self.lstCategories, self.txtSearchCategories,
                                    self.btnCategoriesAll, self.btnCategoriesNone)
        self.categories.set_labels(CATEGORY_ORDER, CATEGORY_GROUP)
        self.categories.set_checked(DEFAULT_CATEGORIES)
        self.btnCatElectrical.Click += lambda s, a: self.categories.check_group("Electrical")
        self.btnCatMechanical.Click += lambda s, a: self.categories.check_group("Mechanical")
        self.btnCatPlumbing.Click += lambda s, a: self.categories.check_group("Plumbing")

        self.mep_families = CheckList(self.lstElecFamilies, self.txtSearchElecFamilies,
                                      self.btnElecFamiliesAll, self.btnElecFamiliesNone)
        self.detail_families = CheckList(self.lstDetailFamilies, self.txtSearchDetailFamilies,
                                         self.btnDetailFamiliesAll, self.btnDetailFamiliesNone)
        self.levels = CheckList(self.lstLevels, self.txtSearchLevels,
                                self.btnLevelsAll, self.btnLevelsNone)

        self.mep_filter = FilterPanel([
            FilterRow(self.cmbElecFilterParam1, self.cmbElecFilterCond1, self.txtElecFilterValue1),
            FilterRow(self.cmbElecFilterParam2, self.cmbElecFilterCond2, self.txtElecFilterValue2),
            FilterRow(self.cmbElecFilterParam3, self.cmbElecFilterCond3, self.txtElecFilterValue3),
            FilterRow(self.cmbElecFilterParam4, self.cmbElecFilterCond4, self.txtElecFilterValue4),
        ])
        self.detail_filter = FilterPanel([
            FilterRow(self.cmbDetailFilterParam1, self.cmbDetailFilterCond1, self.txtDetailFilterValue1),
            FilterRow(self.cmbDetailFilterParam2, self.cmbDetailFilterCond2, self.txtDetailFilterValue2),
            FilterRow(self.cmbDetailFilterParam3, self.cmbDetailFilterCond3, self.txtDetailFilterValue3),
            FilterRow(self.cmbDetailFilterParam4, self.cmbDetailFilterCond4, self.txtDetailFilterValue4),
        ])

        self.refresh_template_list()

    # -- plumbing -----------------------------------------------------------
    def set_status(self, text):
        self.txtStatus.Text = text

    def _on_closed(self, sender, args):
        try:
            self.ext_event.Dispose()
        except Exception:
            pass

    def run_in_api_context(self, label, func):
        """Queue func(uidoc) to run inside Revit's API context."""
        try:
            self.event_handler.queue.append((label, func))
            self.set_status("{}...".format(label))
            self.ext_event.Raise()
        except Exception as ex:
            self.set_status("Could not queue '{}': {}".format(label, ex))

    def _same_document(self, uidoc):
        doc = uidoc.Document
        ok = (self.collected_doc is not None and is_alive(self.collected_doc)
              and self.collected_doc.Equals(doc))
        if not ok:
            forms.alert("The active document is not the one the elements were collected from.\n\n"
                        "Switch back to it, or click 'Collect Elements' again.",
                        title=TOOL_TITLE, warn_icon=True)
            self.set_status("Document changed - re-collect.")
        return ok

    def _selected_categories(self):
        return [CATEGORY_MAP[label] for label in self.categories.checked_labels() if label in CATEGORY_MAP]

    # -- collection -----------------------------------------------------------
    def collect_elements(self, sender, args):
        self._ensure_globals()
        categories = self._selected_categories()
        if not categories:
            forms.alert("Select at least one MEP category.", title=TOOL_TITLE, warn_icon=True)
            return
        self.run_in_api_context("Collecting elements",
                                lambda uidoc: self._do_collect_elements(uidoc, categories))

    def _do_collect_elements(self, uidoc, categories):
        doc = uidoc.Document
        clear_caches()
        scope = self.cmbScope.SelectedItem
        exclude_nested = bool(self.chkExcludeNested.IsChecked)
        active_id = uidoc.ActiveView.Id
        mep_view_id = active_id if scope == SCOPE_ACTIVE else None
        detail_view_id = active_id if scope in (SCOPE_ACTIVE, SCOPE_SPLIT) else None

        self.collected_doc = doc
        self.mep_elements = collect_mep_elements(doc, mep_view_id, categories, exclude_nested)
        self.detail_items = collect_detail_items(doc, detail_view_id, exclude_nested)
        self._set_results([])

        if not self.mep_elements:
            self.source_fields, self.detail_fields = [], []
            forms.alert("No elements of the selected MEP categories were found in the selected scope.",
                        title=TOOL_TITLE, warn_icon=True)
            self.set_status("Nothing collected.")
            return
        if not self.detail_items:
            forms.alert("No Detail Items were found in the selected scope.\n\n"
                        "You can still run the check - every MEP element will be reported as NO MATCH.",
                        title=TOOL_TITLE, warn_icon=True)

        mep_families = sorted(set(family_name_of(e) for e in self.mep_elements), key=lambda x: x.lower())
        detail_families = sorted(set(family_name_of(d) for d in self.detail_items), key=lambda x: x.lower())
        levels = sorted(set([level_of(e) for e in self.mep_elements] +
                            [level_of(d) for d in self.detail_items]), key=lambda x: x.lower())

        self.mep_families.set_labels(mep_families)
        self.detail_families.set_labels(detail_families)
        self.levels.set_labels(levels)

        self.source_fields = choices_for(self.mep_elements)
        self.detail_fields = choices_for(self.detail_items) if self.detail_items else choices_for([])
        self.cmbKeyElec.ItemsSource = self.source_fields
        self.cmbKeyDetail.ItemsSource = self.detail_fields
        self.cmbValElec.ItemsSource = self.source_fields
        self.cmbValDetail.ItemsSource = self.detail_fields
        self.mep_filter.set_fields(self.source_fields)
        self.detail_filter.set_fields(self.detail_fields)

        # Re-bind existing key/value pairs to the new field objects by label.
        self._rebind_pairs([[sf.label, df.label] for sf, df in self.key_pairs],
                           [[sf.label, df.label, c] for sf, df, c in self.value_pairs])

        self.set_status(u"{} MEP element(s), {} Detail Item(s) collected from '{}'."
                        .format(len(self.mep_elements), len(self.detail_items), doc.Title))

    # -- field pairs -----------------------------------------------------------
    def _refresh_pair_lists(self):
        self.lstKeyPairs.ItemsSource = [u"{} = {}".format(sf.label, df.label) for sf, df in self.key_pairs]
        self.lstValuePairs.ItemsSource = [u"{} {} {}".format(sf.label, cond, df.label)
                                          for sf, df, cond in self.value_pairs]

    def _rebind_pairs(self, key_labels, value_labels):
        def find(fields, label):
            for field in fields:
                if field.label == label:
                    return field
            return None

        self.key_pairs = []
        for source_label, detail_label in key_labels:
            sf, df = find(self.source_fields, source_label), find(self.detail_fields, detail_label)
            if sf and df:
                self.key_pairs.append((sf, df))
        self.value_pairs = []
        for entry in value_labels:
            if len(entry) == 3:
                source_label, detail_label, condition = entry
            else:
                source_label, detail_label = entry
                condition = DEFAULT_CONDITION
            sf, df = find(self.source_fields, source_label), find(self.detail_fields, detail_label)
            if sf and df:
                self.value_pairs.append((sf, df, condition))
        self._refresh_pair_lists()

    def add_key_pair(self, sender, args):
        self._ensure_globals()
        sf, df = self.cmbKeyElec.SelectedItem, self.cmbKeyDetail.SelectedItem
        if not sf or not df:
            forms.alert("Select both an MEP and a Detail Item field first.", title=TOOL_TITLE, warn_icon=True)
            return
        self.key_pairs.append((sf, df))
        self._refresh_pair_lists()

    def remove_key_pair(self, sender, args):
        self._ensure_globals()
        index = self.lstKeyPairs.SelectedIndex
        if index >= 0:
            self.key_pairs.pop(index)
            self._refresh_pair_lists()

    def add_value_pair(self, sender, args):
        self._ensure_globals()
        sf, df = self.cmbValElec.SelectedItem, self.cmbValDetail.SelectedItem
        condition = self.cmbValCondition.SelectedItem or DEFAULT_CONDITION
        if not sf or not df:
            forms.alert("Select both an MEP and a Detail Item field first.", title=TOOL_TITLE, warn_icon=True)
            return
        self.value_pairs.append((sf, df, condition))
        self._refresh_pair_lists()

    def remove_value_pair(self, sender, args):
        self._ensure_globals()
        index = self.lstValuePairs.SelectedIndex
        if index >= 0:
            self.value_pairs.pop(index)
            self._refresh_pair_lists()

    # -- cross check -------------------------------------------------------------
    def run_cross_check(self, sender, args):
        self._ensure_globals()
        if not self.mep_elements:
            forms.alert("Click 'Collect Elements' first.", title=TOOL_TITLE, warn_icon=True)
            return
        if not self.key_pairs:
            forms.alert("Add at least one pairing key field.", title=TOOL_TITLE, warn_icon=True)
            return
        if not self.value_pairs:
            forms.alert("Add at least one comparison value field.", title=TOOL_TITLE, warn_icon=True)
            return
        self.run_in_api_context("Running cross check", self._do_run_cross_check)

    def _do_run_cross_check(self, uidoc):
        if not self._same_document(uidoc):
            return
        ignore_case = not self.chkCaseSensitive.IsChecked

        # Elements deleted since "Collect" would throw InvalidObjectException.
        mep_pool = [e for e in self.mep_elements if is_alive(e)]
        detail_pool = [d for d in self.detail_items if is_alive(d)]
        dropped = (len(self.mep_elements) - len(mep_pool)) + (len(self.detail_items) - len(detail_pool))

        selected_mep_families = set(self.mep_families.checked_labels())
        selected_detail_families = set(self.detail_families.checked_labels())
        selected_levels = set(self.levels.checked_labels())

        if selected_mep_families:
            mep_pool = [e for e in mep_pool if family_name_of(e) in selected_mep_families]
        if selected_detail_families:
            detail_pool = [d for d in detail_pool if family_name_of(d) in selected_detail_families]
        if selected_levels:
            mep_pool = [e for e in mep_pool if level_of(e) in selected_levels]
            # A Detail Item in a drafting view (a single line diagram) has no
            # level at all. Filtering it out would turn every panel of the
            # ticked level into NO MATCH, so those items always stay.
            detail_pool = [d for d in detail_pool
                           if level_of(d) in selected_levels or level_of(d) == NO_LEVEL]

        mep_before, detail_before = len(mep_pool), len(detail_pool)
        mep_pool = self.mep_filter.apply(mep_pool, ignore_case)
        detail_pool = self.detail_filter.apply(detail_pool, ignore_case)

        if not mep_pool:
            forms.alert("No MEP elements remain after filtering ({} before the Advanced Filter, 0 after)."
                        .format(mep_before), title=TOOL_TITLE, warn_icon=True)
            self.set_status("No MEP elements after filtering.")
            return
        if not detail_pool:
            forms.alert("No Detail Items remain after filtering ({} before the Advanced Filter, 0 after).\n\n"
                        "Continuing - every MEP element will be listed as NO MATCH."
                        .format(detail_before), title=TOOL_TITLE, warn_icon=True)

        key_pairs, value_pairs = list(self.key_pairs), list(self.value_pairs)
        compare_all = bool(self.chkCompareAll.IsChecked)

        def compare_pair(mep, detail):
            comparisons = []
            for sf, df, condition in value_pairs:
                comparisons.append({
                    "label": sf.label, "condition": condition,
                    "evalue": sf.value(mep).strip(), "dvalue": df.value(detail).strip(),
                    "equal": evaluate_field_condition(sf, mep, condition, ignore_case,
                                                      compare_field=df, compare_element=detail)})
            equal_count = len([c for c in comparisons if c["equal"]])
            if equal_count == len(comparisons):
                return "MATCH", "", comparisons
            if equal_count == 0:
                return "NOT MATCH", "All compared fields differ", comparisons
            return ("PARTIAL MATCH",
                    "Mismatch: " + ", ".join([c["label"] for c in comparisons if not c["equal"]]),
                    comparisons)

        def raw_key_parts(element, use_source):
            return [(pair[0] if use_source else pair[1]).value(element).strip() for pair in key_pairs]

        def normalized_key(parts):
            return tuple([normalize(p) if ignore_case else p for p in parts])

        def key_label(parts, use_source):
            labels = []
            for (sf, df), part in zip(key_pairs, parts):
                labels.append(u"{}: {}".format((sf if use_source else df).label, part or "<blank>"))
            return u" | ".join(labels)

        def one_sided(element, use_source):
            """Comparisons when there's nothing on the other side. Only unary
            conditions can be evaluated; everything else is a fail (the old
            version compared against "" and showed a misleading check mark)."""
            out = []
            for sf, df, condition in value_pairs:
                field = sf if use_source else df
                val = field.value(element).strip()
                equal = (evaluate_field_condition(field, element, condition, ignore_case)
                         if use_source and condition in UNARY_CONDITIONS else False)
                out.append({"label": field.label, "condition": condition, "equal": equal,
                            "evalue": val if use_source else "", "dvalue": "" if use_source else val})
            return out

        # Pre-compute keys once (each .value() is an API call).
        mep_keys = [(e, raw_key_parts(e, True)) for e in mep_pool]
        detail_keys = [(d, raw_key_parts(d, False)) for d in detail_pool]

        detail_index, mep_index = {}, {}
        for detail, parts in detail_keys:
            if all(parts):
                detail_index.setdefault(normalized_key(parts), []).append(detail)
        for mep, parts in mep_keys:
            if all(parts):
                mep_index.setdefault(normalized_key(parts), []).append(mep)

        results = []
        for mep, parts in mep_keys:
            key_text = key_label(parts, True)
            mep_level = level_of(mep)
            complete = all(parts)
            nkey = normalized_key(parts)
            candidates = detail_index.get(nkey, []) if complete else []
            shared = len(mep_index.get(nkey, [])) if complete else 0
            dup_note = "Key shared by {} MEP elements".format(shared) if shared > 1 else ""

            if not candidates:
                notes = "Incomplete pairing key" if not complete else "No Detail Item found"
                results.append(ResultRow("NO MATCH", mep, None, key_text, one_sided(mep, True),
                                         "; ".join(filter(None, [notes, dup_note])), mep_level, ""))
            elif len(candidates) > 1 and compare_all:
                # The same panel drawn on several diagrams: check every copy.
                for number, detail in enumerate(candidates):
                    status, notes, comparisons = compare_pair(mep, detail)
                    copy_note = "Detail Item {} of {} with this key".format(number + 1, len(candidates))
                    results.append(ResultRow(status, mep, detail, key_text, comparisons,
                                             "; ".join(filter(None, [notes, copy_note, dup_note])),
                                             mep_level, level_of(detail)))
            elif len(candidates) > 1:
                ids = [eid_int(c.Id) for c in candidates]
                shown = ", ".join(str(i) for i in ids[:MAX_IDS_IN_NOTE])
                if len(ids) > MAX_IDS_IN_NOTE:
                    shown += ", ..."
                notes = "{} Detail Items share this key (Ids {})".format(len(candidates), shown)
                results.append(ResultRow("AMBIGUOUS", mep, None, key_text, one_sided(mep, True),
                                         "; ".join(filter(None, [notes, dup_note])), mep_level, "",
                                         extra_ids=[c.Id for c in candidates]))
            else:
                detail = candidates[0]
                status, notes, comparisons = compare_pair(mep, detail)
                results.append(ResultRow(status, mep, detail, key_text, comparisons,
                                         "; ".join(filter(None, [notes, dup_note])),
                                         mep_level, level_of(detail)))

        for detail, parts in detail_keys:
            if all(parts) and mep_index.get(normalized_key(parts)):
                continue
            notes = "Incomplete pairing key" if not all(parts) else "No MEP element found"
            results.append(ResultRow("ORPHAN DETAIL ITEM", None, detail, key_label(parts, False),
                                     one_sided(detail, False), notes, "", level_of(detail)))

        self._set_results(results)
        counts = {}
        for row in results:
            counts[row.Status] = counts.get(row.Status, 0) + 1
        summary = ", ".join(["{}: {}".format(k, v) for k, v in sorted(counts.items())])
        if dropped:
            summary += " ({} deleted element(s) skipped)".format(dropped)
        self.set_status("{} result(s). {}".format(len(results), summary))
        self._save_state(LAST_USED_KEY)

    # -- model interaction ------------------------------------------------------
    def _selected_or_all_ids(self):
        selected_rows = list(self.dgResults.SelectedItems) if self.dgResults.ItemsSource else []
        # No row highlighted = every row that is SHOWN (the status / search
        # filter counts), not every result.
        rows = selected_rows or self.shown_results
        seen, ids = set(), []
        for row in rows:
            for element_id in [row.MepId, row.DetailId] + row.ExtraIds:
                if element_id is not None and eid_int(element_id) not in seen:
                    seen.add(eid_int(element_id))
                    ids.append(element_id)
        return ids

    @staticmethod
    def _valid_ids(doc, ids):
        return [i for i in ids if doc.GetElement(i) is not None]

    @staticmethod
    def _ids_showable_in(doc, view, ids):
        """Drop view-specific elements (Detail Items) owned by another view -
        they can never be shown/isolated in the active view."""
        keep = []
        for element_id in ids:
            element = doc.GetElement(element_id)
            if element is None:
                continue
            owner = element.OwnerViewId
            if eid_int(owner) > 0 and owner != view.Id:
                continue
            keep.append(element_id)
        return keep

    def _run_on_ids(self, label, func):
        ids = self._selected_or_all_ids()
        if not ids:
            forms.alert("No results to use.", title=TOOL_TITLE, warn_icon=True)
            return

        def action(uidoc):
            if not self._same_document(uidoc):
                return
            valid = self._valid_ids(uidoc.Document, ids)
            if not valid:
                self.set_status("None of those elements exist any more.")
                return
            func(uidoc, valid)
        self.run_in_api_context(label, action)

    def select_in_model(self, sender, args):
        self._ensure_globals()

        def do(uidoc, ids):
            uidoc.Selection.SetElementIds(List[ElementId](ids))
            self.set_status("{} element(s) selected.".format(len(ids)))
        self._run_on_ids("Selecting", do)

    def zoom_to(self, sender, args):
        self._ensure_globals()

        def do(uidoc, ids):
            showable = self._ids_showable_in(uidoc.Document, uidoc.ActiveView, ids)
            # If nothing is in the active view, let Revit pick a view.
            target = showable or ids
            uidoc.ShowElements(List[ElementId](target))
            skipped = len(ids) - len(target)
            self.set_status("Zoomed to {} element(s){}.".format(
                len(target), " ({} in other views skipped)".format(skipped) if skipped else ""))
        self._run_on_ids("Zooming", do)

    def isolate_in_view(self, sender, args):
        self._ensure_globals()

        def do(uidoc, ids):
            doc, view = uidoc.Document, uidoc.ActiveView
            if not view.CanUseTemporaryVisibilityModes():
                forms.alert("The active view does not support Temporary Hide/Isolate.",
                            title=TOOL_TITLE, warn_icon=True)
                return
            showable = self._ids_showable_in(doc, view, ids)
            if not showable:
                forms.alert("None of those elements can be shown in the active view.",
                            title=TOOL_TITLE, warn_icon=True)
                return
            with Transaction(doc, "Cross Check - Isolate Elements") as t:
                t.Start()
                view.IsolateElementsTemporary(List[ElementId](showable))
                t.Commit()
            self.set_status("{} element(s) isolated.".format(len(showable)))
        self._run_on_ids("Isolating", do)

    def clear_isolate(self, sender, args):
        self._ensure_globals()

        def do(uidoc):
            doc, view = uidoc.Document, uidoc.ActiveView
            if not view.IsInTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate):
                self.set_status("Nothing to clear.")
                return
            with Transaction(doc, "Cross Check - Clear Isolate") as t:
                t.Start()
                view.DisableTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate)
                t.Commit()
            self.set_status("Isolate cleared.")
        self.run_in_api_context("Clearing isolate", do)

    # -- results housekeeping ------------------------------------------------------
    def export_results_csv(self, sender, args):
        self._ensure_globals()
        if not self.results:
            forms.alert("No results to export.", title=TOOL_TITLE, warn_icon=True)
            return
        header = ["Status", "Category", "MEP Element ID", "MEP Element", "MEP Level",
                  "Detail Item ID", "Detail Item", "Detail Level", "Match Key"]
        for index, (sf, df, condition) in enumerate(self.value_pairs):
            header.append(u"{}. {} [{}] (MEP)".format(index + 1, sf.label, condition))
            header.append(u"{}. {} [{}] (Detail)".format(index + 1, df.label, condition))
            header.append(u"{}. {} [{}] Match".format(index + 1, sf.label, condition))
        header.append("Notes")

        rows = []
        for row in (self.shown_results or self.results):
            csv_row = [row.Status, row.Category, eid_int(row.MepId) if row.MepId else "",
                       row.MepElement, row.MepLevel,
                       eid_int(row.DetailId) if row.DetailId else "", row.Detail, row.DetailLevel,
                       row.MatchKey]
            for comparison in row.comparisons:
                csv_row.extend([comparison["evalue"], comparison["dvalue"],
                                "Yes" if comparison["equal"] else "No"])
            csv_row.append(row.Notes)
            rows.append(csv_row)
        try:
            exported_file = export_csv(header, rows)
        except Exception as ex:
            forms.alert("Export failed (is the file open in Excel?)\n\n{}".format(ex),
                        title=TOOL_TITLE, warn_icon=True)
            return
        if exported_file:
            self.set_status("Exported to {}".format(exported_file))

    def clear_results(self, sender, args):
        self._ensure_globals()
        self._set_results([])
        self.set_status("Results cleared.")

    # -- results: status chips + search ---------------------------------------------
    def _set_results(self, results):
        self.results = list(results)
        self.status_filter = ALL_STATUSES
        for row in self.results:
            row.search_text = u" ".join(unicode(v) for v in (          # noqa: F821
                row.Status, row.Category, row.MepElement, row.MepLevel, row.Detail,
                row.DetailLevel, row.MatchKey, row.FieldComparison, row.Notes)).lower()
        self._show_results()

    def _show_results(self):
        """Fill the grid with the rows that pass the status chip and the search
        box, and redraw the chips with their counts."""
        words = (self.txtResultSearch.Text or "").lower().split()
        searched = [r for r in self.results if all(w in r.search_text for w in words)]
        self.shown_results = [r for r in searched
                              if self.status_filter in (ALL_STATUSES, r.Status)]
        self.dgResults.ItemsSource = self.shown_results if self.results else None

        counts = {}
        for row in searched:
            counts[row.Status] = counts.get(row.Status, 0) + 1
        self.pnlChips.Children.Clear()
        if not self.results:
            return
        converter = BrushConverter()
        for status in [ALL_STATUSES] + [s for s in STATUS_ORDER if counts.get(s)]:
            count = len(searched) if status == ALL_STATUSES else counts[status]
            chip = Border()
            chip.Tag = status
            light, dark, tint = STATUS_COLOURS.get(status, STATUS_COLOURS[ALL_STATUSES])
            active = status == self.status_filter
            chip.CornerRadius = CornerRadius(13)
            chip.Padding = Thickness(12, 4, 12, 4)
            chip.Margin = Thickness(0, 0, 6, 0)
            chip.Cursor = Cursors.Hand
            # the active filter is the filled one, like a selected iOS chip
            chip.Background = converter.ConvertFromString(tint if active else light)
            chip.ToolTip = "Show only these rows"
            text = TextBlock()
            text.Text = u"{}  {}".format(status, count)
            text.FontSize = 11.5
            text.FontWeight = FontWeights.SemiBold
            text.Foreground = converter.ConvertFromString("#FFFFFF" if active else dark)
            chip.Child = text
            chip.MouseLeftButtonUp += self._chip_clicked
            self.pnlChips.Children.Add(chip)

    def _chip_clicked(self, sender, args):
        self.status_filter = sender.Tag
        self._show_results()

    def result_search_changed(self, sender, args):
        if getattr(self, "results", None) is not None:
            self._show_results()

    def result_double_click(self, sender, args):
        """Double-click a row = Zoom To it."""
        if self.dgResults.SelectedItem is not None:
            self.zoom_to(sender, args)

    # -- templates ---------------------------------------------------------------
    def _get_templates(self):
        try:
            return json.loads(config.get_option('templates_json', '{}'))
        except Exception:
            return {}

    def _store_templates(self, templates):
        config.templates_json = json.dumps(templates)
        script.save_config()

    def refresh_template_list(self):
        names = sorted([n for n in self._get_templates().keys() if n != LAST_USED_KEY],
                       key=lambda x: x.lower())
        self.cmbTemplates.ItemsSource = names

    def _current_state(self):
        return {
            "version": 2,
            "categories": self.categories.checked_labels(),
            "scope": self.cmbScope.SelectedItem,
            "exclude_nested": bool(self.chkExcludeNested.IsChecked),
            "elec_families": self.mep_families.checked_labels(),
            "detail_families": self.detail_families.checked_labels(),
            "levels": self.levels.checked_labels(),
            "key_pairs": [[sf.label, df.label] for sf, df in self.key_pairs],
            "value_pairs": [[sf.label, df.label, cond] for sf, df, cond in self.value_pairs],
            "case_sensitive": bool(self.chkCaseSensitive.IsChecked),
            "compare_all": bool(self.chkCompareAll.IsChecked),
            "elec_filter": self.mep_filter.get_state(),
            "detail_filter": self.detail_filter.get_state(),
        }

    def _save_state(self, name):
        templates = self._get_templates()
        templates[name] = self._current_state()
        self._store_templates(templates)

    def _apply_state(self, state, status_when_done=None):
        # v1 templates only stored the two electrical categories - same labels.
        self.categories.set_checked(state.get("categories", DEFAULT_CATEGORIES))
        if state.get("scope") in SCOPE_OPTIONS:
            self.cmbScope.SelectedItem = state["scope"]
        self.chkExcludeNested.IsChecked = state.get("exclude_nested", True)

        categories = self._selected_categories()
        if not categories:
            forms.alert("The template has no MEP category available in this Revit version.",
                        title=TOOL_TITLE, warn_icon=True)
            return
        self.run_in_api_context(
            "Loading template",
            lambda uidoc: self._apply_state_after_collect(uidoc, state, categories, status_when_done))

    def _apply_state_after_collect(self, uidoc, state, categories, status_when_done):
        self._do_collect_elements(uidoc, categories)
        if not self.mep_elements:
            return
        self.mep_families.set_checked(state.get("elec_families", []))
        self.detail_families.set_checked(state.get("detail_families", []))
        self.levels.set_checked(state.get("levels", []))
        self._rebind_pairs(state.get("key_pairs", []), state.get("value_pairs", []))
        self.chkCaseSensitive.IsChecked = state.get("case_sensitive", False)
        self.chkCompareAll.IsChecked = state.get("compare_all", False)
        self.mep_filter.set_state(state.get("elec_filter"))
        self.detail_filter.set_state(state.get("detail_filter"))

        missing = (len(state.get("key_pairs", [])) - len(self.key_pairs) +
                   len(state.get("value_pairs", [])) - len(self.value_pairs))
        message = status_when_done or "Template applied."
        if missing:
            message += " ({} field pair(s) not found in this model)".format(missing)
        self.set_status(message)

    def save_template(self, sender, args):
        self._ensure_globals()
        name = forms.ask_for_string(default="My Template", prompt="Template name:", title="Save Template")
        if not name:
            return
        if name == LAST_USED_KEY:
            forms.alert("That name is reserved. Choose another.", title=TOOL_TITLE, warn_icon=True)
            return
        self._save_state(name)
        self.refresh_template_list()
        self.set_status(u"Template '{}' saved.".format(name))

    def load_template(self, sender, args):
        self._ensure_globals()
        name = self.cmbTemplates.SelectedItem
        if not name:
            forms.alert("Select a saved template first.", title=TOOL_TITLE, warn_icon=True)
            return
        state = self._get_templates().get(name)
        if not state:
            forms.alert("Template not found.", title=TOOL_TITLE, warn_icon=True)
            return
        self._apply_state(state, status_when_done=u"Template '{}' loaded.".format(name))

    def import_last_template(self, sender, args):
        self._ensure_globals()
        state = self._get_templates().get(LAST_USED_KEY)
        if not state:
            forms.alert("No previous run found yet.", title=TOOL_TITLE, warn_icon=True)
            return
        self._apply_state(state, status_when_done="Last used setup restored.")

    def delete_template(self, sender, args):
        self._ensure_globals()
        name = self.cmbTemplates.SelectedItem
        if not name:
            forms.alert("Select a saved template first.", title=TOOL_TITLE, warn_icon=True)
            return
        if not forms.alert(u"Delete template '{}'?".format(name), yes=True, no=True, title=TOOL_TITLE):
            return
        templates = self._get_templates()
        if name in templates:
            del templates[name]
            self._store_templates(templates)
            self.refresh_template_list()
            self.set_status(u"Template '{}' deleted.".format(name))


# ---------------------------------------------------------------------------
# Global-namespace guard for the modeless window.
# Taken AFTER every definition above (the old snapshot was taken before the
# window class and also re-injected the stale module-level doc/uidoc).
# Only names that are missing or were nulled by engine cleanup are restored.
# ---------------------------------------------------------------------------
_MODULE_SNAPSHOT = dict(globals())


def _ensure_globals(_snapshot=_MODULE_SNAPSHOT):
    module_globals = globals()
    for name, value in _snapshot.items():
        if module_globals.get(name) is None and value is not None:
            module_globals[name] = value


XAML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.xaml")
_OPEN = []      # the open window: keeps it alive and makes the button reuse it


def main():
    """Called by script.py. A second click on the button brings the open
    window to the front instead of stacking another one on top."""
    for window in list(_OPEN):
        try:
            if window.IsVisible:
                window.Activate()
                return window
        except Exception:
            pass
        _OPEN.remove(window)
    window = CrossCheckWindow(XAML_PATH, _ensure_globals)
    _OPEN.append(window)
    window.show(modal=False)
    return window

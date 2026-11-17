# -*- coding: utf-8 -*-
"""Find Detail Items whose chosen parameter (default MEE_Component) matches
the selected electrical element. MODELLESS window.

pyRevit (IronPython engine) on Revit 2022-2026.

Window:
  1. Detail Item parameter
       - pick/type the parameter name, or
       - select a detail item in Revit and click "Pick Detail Item": the list
         shows ALL its parameters + values and the view it lives in; select
         the row holding your value and click "Use This Parameter".
       - Views: Drafting / Drafting+Detail+Legends / ALL views (plans etc.)
  2. Source element - Get From Selection, choose Panel Name / Panel / ...
  3. Search - in the chosen parameter, or in ANY text parameter (finds
     which parameter really holds the value).

Speed: window opens instantly; one collector pass; only the needed
parameters are read. Safety: all API calls go through an ExternalEvent and
the engine is persistent.
"""
from __future__ import print_function

__persistentengine__ = True   # REQUIRED for modeless windows + ExternalEvent

import os
import re

from pyrevit import forms, revit, script
from Autodesk.Revit.DB import (BuiltInCategory, BuiltInParameter, Element,
                               ElementFilter, ElementId, ElementOwnerViewFilter,
                               FamilyInstance, FilteredElementCollector,
                               LogicalOrFilter, StorageType, View, ViewType)
from Autodesk.Revit.UI import ExternalEvent, IExternalEventHandler
from System import Uri
from System.Collections.Generic import List
from System.Windows.Input import Key
from System.Windows.Media.Imaging import BitmapImage


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
DEFAULT_DETAIL_PARAMETER = "MEE_Component"

SOURCE_BUILTIN_PARAMS = [
    ("Panel Name", "RBS_ELEC_PANEL_NAME"),
    ("Panel", "RBS_ELEC_CIRCUIT_PANEL_PARAM"),
]
SOURCE_PARAMETER_NAMES = ["Panel Name", "Panel", "Distribution Board", "DB Name"]

MULTI_VALUE_SPLIT = re.compile(r"[,;|\r\n]+")
MODE_EXACT, MODE_LOOSE, MODE_CONTAINS, MODE_STARTS, MODE_ALL = range(5)
SCOPE_DRAFTING, SCOPE_DRAFT_DETAIL, SCOPE_ALL_VIEWS = range(3)
SEARCH_CHOSEN, SEARCH_ANY = range(2)
MAX_RESULTS = 5000

BUNDLE_DIR = os.path.dirname(__file__)


# --------------------------------------------------------------------------
# Settings (remembered between sessions via pyRevit config)
# --------------------------------------------------------------------------
def load_settings():
    # Default: Detail Items in ALL views (plans used as single-line diagrams too).
    param, detail_only, scope = DEFAULT_DETAIL_PARAMETER, True, SCOPE_ALL_VIEWS
    try:
        cfg = script.get_config()
        param = cfg.get_option("detail_param", DEFAULT_DETAIL_PARAMETER) or param
        detail_only = str(cfg.get_option("detail_only_v7", "True")) == "True"
        scope = int(cfg.get_option("view_scope_v7", str(SCOPE_ALL_VIEWS)))
    except Exception:
        pass
    return param, detail_only, scope


def save_settings(param, detail_only, scope):
    try:
        cfg = script.get_config()
        cfg.detail_param = param
        cfg.detail_only_v7 = str(bool(detail_only))
        cfg.view_scope_v7 = str(int(scope))
        script.save_config()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------
def id_value(element_id):
    """ElementId -> int for Revit 2022 (IntegerValue) .. 2026 (Value)."""
    if hasattr(element_id, "Value"):
        return element_id.Value
    return element_id.IntegerValue


def doc_key(doc):
    return u"{0}|{1}".format(doc.Title, doc.PathName)


def name_key(name):
    return re.sub(r"[\s_\-]+", "", name or "").lower()


def normalized(value):
    if value is None:
        return u""
    text = value.replace(u"\u00a0", u" ")
    text = re.sub(u"[\u2010-\u2015\u2212]", u"-", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def loose(value):
    """Only letters/digits: 'DB-01', 'DB 01', 'db_01', 'DB.01' -> 'db01'."""
    return re.sub(r"[\W_]+", u"", normalized(value), flags=re.UNICODE)


def split_values(value):
    parts = [value] + MULTI_VALUE_SPLIT.split(value or u"")
    out = []
    for part in parts:
        n = normalized(part)
        if n and n not in out:
            out.append(n)
    return out


def parameter_text(parameter):
    if parameter is None:
        return None
    try:
        if parameter.StorageType == StorageType.String:
            value = parameter.AsString()
        else:
            value = parameter.AsValueString()
    except Exception:
        value = None
    if value and value.strip():
        return value.strip()
    return None


def element_type(element):
    try:
        type_id = element.GetTypeId()
        if type_id and type_id != ElementId.InvalidElementId:
            return element.Document.GetElement(type_id)
    except Exception:
        pass
    return None


def safe_name(element, default=u""):
    """Name that works for ElementType/FamilySymbol in IronPython
    (plain .Name raises 'AttributeError: Name' on those classes)."""
    if element is None:
        return default
    for getter in (lambda e: Element.Name.GetValue(e),
                   lambda e: Element.Name.__get__(e),
                   lambda e: e.Name):
        try:
            name = getter(element)
            if name:
                return name
        except Exception:
            continue
    for bip_name in ("SYMBOL_NAME_PARAM", "ALL_MODEL_TYPE_NAME", "VIEW_NAME"):
        bip = getattr(BuiltInParameter, bip_name, None)
        if bip is None:
            continue
        try:
            value = parameter_text(element.get_Parameter(bip))
            if value:
                return value
        except Exception:
            continue
    return default


def category_name(element):
    try:
        if element.Category is not None:
            return element.Category.Name or u"<No Category>"
    except Exception:
        pass
    return u"<No Category>"


def describe_element(element):
    cat = category_name(element)
    fam, typ = u"", u""
    etype = element_type(element)
    if isinstance(element, FamilyInstance):
        try:
            symbol = element.Symbol
            fam = safe_name(symbol.Family)
            typ = safe_name(symbol)
        except Exception:
            pass
    if not fam and etype is not None:
        try:
            fam = etype.FamilyName
        except Exception:
            fam = u""
    if not typ:
        typ = safe_name(etype) if etype is not None else safe_name(element)
    return cat, fam or u"-", typ or u"-"


def parameter_values(owner, level):
    """[(param_name, value, label)] for every non-empty parameter."""
    out = []
    if owner is None:
        return out
    for parameter in owner.Parameters:
        try:
            pname = parameter.Definition.Name
        except Exception:
            continue
        value = parameter_text(parameter)
        if value:
            out.append((pname, value, u"{0} ({1})".format(pname, level)))
    return out


def read_named_values(element, names):
    wanted = set(name_key(n) for n in names)
    found = []
    for owner, level in ((element, u"instance"), (element_type(element), u"type")):
        for pname, value, label in parameter_values(owner, level):
            if name_key(pname) in wanted:
                found.append((value, label))
    return found


# --------------------------------------------------------------------------
# Data records (plain attributes -> WPF bindings)
# --------------------------------------------------------------------------
class SourceOption(object):
    def __init__(self, param_label, value):
        self.ParamLabel = param_label
        self.Value = value
        self.Label = u"{0}  =  {1}".format(param_label, value)


class ElementRecord(object):
    """One view-owned element; only cheap info is read at scan time."""
    def __init__(self, element, view_id, view_name, is_detail, type_key, names):
        self.Element = element
        self.ElementIdObj = element.Id
        self.ViewIdObj = view_id
        self.ViewName = view_name
        self.IsDetailItem = is_detail
        self.TypeKey = type_key
        self.CategoryName, self.FamilyName, self.TypeName = names
        self.IdText = str(id_value(element.Id))


class IndexEntry(object):
    def __init__(self, record, value, source, param_name):
        self.ElementIdObj = record.ElementIdObj
        self.ViewIdObj = record.ViewIdObj
        self.ViewName = record.ViewName
        self.CategoryName = record.CategoryName
        self.FamilyName = record.FamilyName
        self.TypeName = record.TypeName
        self.IdText = record.IdText
        self.Value = value
        self.Source = source
        self.ParamName = param_name
        self.FilterText = u" ".join([value, self.ViewName, self.CategoryName,
                                     self.FamilyName, self.TypeName,
                                     self.IdText, source]).lower()


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
def make_matcher(raw, mode):
    wanted = split_values(raw)
    wanted_loose = [w for w in (loose(x) for x in wanted) if w]

    def hit(value):
        if mode == MODE_ALL:
            return True
        parts = split_values(value)
        if mode == MODE_EXACT:
            return any(p in wanted for p in parts)
        lparts = [loose(p) for p in parts]
        if mode == MODE_LOOSE:
            return any(p in wanted_loose for p in lparts)
        if mode == MODE_CONTAINS:
            return any(w in p for w in wanted_loose for p in lparts)
        if mode == MODE_STARTS:
            return any(p.startswith(w) for w in wanted_loose for p in lparts)
        return False
    return hit


# --------------------------------------------------------------------------
# Revit reads (always inside a valid API context)
# --------------------------------------------------------------------------
def view_type_text(view):
    try:
        return str(view.ViewType)
    except Exception:
        return u"?"


def get_target_views(doc, scope):
    types = None
    if scope == SCOPE_DRAFTING:
        types = [ViewType.DraftingView]
    elif scope == SCOPE_DRAFT_DETAIL:
        types = [ViewType.DraftingView, ViewType.Detail, ViewType.Legend]
    views = []
    for view in FilteredElementCollector(doc).OfClass(View).WhereElementIsNotElementType():
        try:
            if view.IsTemplate:
                continue
            if types is None or view.ViewType in types:
                views.append(view)
        except Exception:
            continue
    return views


def parameter_names(owner):
    out = []
    if owner is None:
        return out
    for parameter in owner.Parameters:
        try:
            out.append(parameter.Definition.Name)
        except Exception:
            continue
    return out


def string_values(owner, level):
    """[(name, value, label)] for non-empty TEXT parameters (fast AsString)."""
    out = []
    if owner is None:
        return out
    for parameter in owner.Parameters:
        try:
            if parameter.StorageType != StorageType.String:
                continue
            value = parameter.AsString()
            if value and value.strip():
                name = parameter.Definition.Name
                out.append((name, value.strip(), u"{0} ({1})".format(name, level)))
        except Exception:
            continue
    return out


def scan_views(doc, detail_only, scope):
    """ONE collector pass (quick filters) over elements owned by the views."""
    views = get_target_views(doc, scope)
    stats = {"views": len(views), "scanned": 0, "detail_items": 0,
             "detail_only": detail_only, "scope": scope}
    records, names = [], {}
    if not views:
        return records, names, stats

    view_names = {}
    for v in views:
        label = u"{0}  [{1}]".format(safe_name(v, u"<Unnamed View>"), view_type_text(v))
        view_names[id_value(v.Id)] = label

    if detail_only:
        # Fast path: one category quick filter; view membership via OwnerViewId.
        collector = (FilteredElementCollector(doc)
                     .OfCategory(BuiltInCategory.OST_DetailComponents)
                     .WhereElementIsNotElementType())
    else:
        owner_filters = List[ElementFilter]()
        for view in views:
            owner_filters.Add(ElementOwnerViewFilter(view.Id))
        owner_filter = (owner_filters[0] if owner_filters.Count == 1
                        else LogicalOrFilter(owner_filters))
        collector = (FilteredElementCollector(doc)
                     .WhereElementIsNotElementType().WherePasses(owner_filter))

    detail_cat = int(BuiltInCategory.OST_DetailComponents)
    type_names_done = set()
    describe_cache = {}
    for element in collector:
        try:
            view_key = id_value(element.OwnerViewId)
            if view_key not in view_names:
                continue
            is_detail = (element.Category is not None and
                         id_value(element.Category.Id) == detail_cat)
            type_id = element.GetTypeId()
            type_key = id_value(type_id) if type_id != ElementId.InvalidElementId else -1
        except Exception:
            continue
        stats["scanned"] += 1
        if is_detail:
            stats["detail_items"] += 1

        if type_key == -1 or type_key not in type_names_done:
            found = parameter_names(element)
            if type_key != -1:
                type_names_done.add(type_key)
                found += parameter_names(doc.GetElement(type_id))
            for pname in found:
                names.setdefault(name_key(pname), set()).add(pname)

        if type_key != -1 and type_key in describe_cache:
            info = describe_cache[type_key]
        else:
            info = describe_element(element)
            if type_key != -1:
                describe_cache[type_key] = info
        records.append(ElementRecord(element, element.OwnerViewId, view_names[view_key],
                                     is_detail, type_key, info))
    return records, names, stats


def values_for_parameter(doc, records, spellings):
    """Read ONLY the chosen parameter (instance + type, all duplicates)."""
    index, type_cache = [], {}
    for record in records:
        element = record.Element
        try:
            if not element.IsValidObject:
                continue
        except Exception:
            continue
        found = []
        for spelling in spellings:
            for parameter in element.GetParameters(spelling):
                value = parameter_text(parameter)
                if value:
                    found.append((value, u"{0} (instance)".format(spelling), spelling))
        if record.TypeKey != -1:
            if record.TypeKey not in type_cache:
                tvals = []
                etype = doc.GetElement(element.GetTypeId())
                if etype is not None:
                    for spelling in spellings:
                        for parameter in etype.GetParameters(spelling):
                            value = parameter_text(parameter)
                            if value:
                                tvals.append((value, u"{0} (type)".format(spelling), spelling))
                type_cache[record.TypeKey] = tvals
            found += type_cache[record.TypeKey]
        for value, label, pname in found:
            index.append(IndexEntry(record, value, label, pname))
    index.sort(key=lambda e: (e.ViewName.lower(), e.Value.lower()))
    return index


def search_any_parameter(doc, records, hit):
    """Search the value in EVERY text parameter (instance + type)."""
    results, type_cache = [], {}
    for record in records:
        element = record.Element
        try:
            if not element.IsValidObject:
                continue
        except Exception:
            continue
        items = string_values(element, u"instance")
        if record.TypeKey != -1:
            if record.TypeKey not in type_cache:
                type_cache[record.TypeKey] = string_values(
                    doc.GetElement(element.GetTypeId()), u"type")
            items = items + type_cache[record.TypeKey]
        for pname, value, label in items:
            if hit(value):
                results.append(IndexEntry(record, value, label, pname))
                if len(results) >= MAX_RESULTS:
                    return results
    return results


def read_source_options(element):
    """Every non-empty parameter of the selected element, preferred first."""
    options, seen = [], set()

    def add(label, value):
        key = (label, normalized(value))
        if value and key not in seen:
            seen.add(key)
            options.append(SourceOption(label, value))

    for label, bip_name in SOURCE_BUILTIN_PARAMS:
        bip = getattr(BuiltInParameter, bip_name, None)
        if bip is not None:
            add(label, parameter_text(element.get_Parameter(bip)))
    for value, label in read_named_values(element, SOURCE_PARAMETER_NAMES):
        add(label, value)
    add(u"Mark", parameter_text(element.get_Parameter(BuiltInParameter.ALL_MODEL_MARK)))
    others = (parameter_values(element, u"instance") +
              parameter_values(element_type(element), u"type"))
    for _, value, label in sorted(others, key=lambda x: x[2].lower()):
        add(label, value)
    return options


# --------------------------------------------------------------------------
# ExternalEvent plumbing
# --------------------------------------------------------------------------
class ApiQueue(IExternalEventHandler):
    def __init__(self):
        self.jobs = []
        self.on_error = None

    def Execute(self, uiapp):
        jobs, self.jobs = self.jobs, []
        for job in jobs:
            try:
                job(uiapp)
            except Exception as error:
                if self.on_error:
                    self.on_error(error)

    def GetName(self):
        return "Find Related Detail Item"


# --------------------------------------------------------------------------
# Modeless window
# --------------------------------------------------------------------------
class FinderWindow(forms.WPFWindow):
    # Class-level defaults: WPF fires events while XAML loads, before __init__.
    _ready = False
    _records = []
    _param_names = {}
    _stats = None
    _index = []
    _matches = []
    _index_doc = None
    _detail_ids = set()
    _param = DEFAULT_DETAIL_PARAMETER
    _inspecting = False

    def __init__(self, api_queue, api_event):
        forms.WPFWindow.__init__(self, os.path.join(BUNDLE_DIR, "FindRelatedDetailItem.xaml"))
        self._queue = api_queue
        self._event = api_event
        self._queue.on_error = lambda e: self._status(u"Revit error: {0}".format(e))
        self._records, self._param_names, self._stats = [], {}, None
        self._index, self._matches = [], []
        self._index_doc = None
        self._ready = False
        param, detail_only, scope = load_settings()
        self._param = param
        self.param_combo.Text = param
        self.detail_only_check.IsChecked = detail_only
        self._set_window_icon()
        self.Closed += self._on_closed
        self._ready = True

    # ---- helpers -------------------------------------------------------
    def _set_window_icon(self):
        path = os.path.join(BUNDLE_DIR, "icon.png")
        if os.path.exists(path):
            try:
                self.Icon = BitmapImage(Uri(path))
            except Exception:
                pass

    def _set_value_header(self, text):
        try:
            self.results_list.View.Columns[0].Header = text
        except Exception:
            pass

    def _status(self, text):
        self.status_text.Text = text

    def run_api(self, job, busy_text=u"Working..."):
        self._status(busy_text)
        self._queue.jobs.append(job)
        self._event.Raise()

    def _on_closed(self, sender, args):
        try:
            self._event.Dispose()
        except Exception:
            pass

    def _detail_only(self):
        return bool(self.detail_only_check.IsChecked)

    def _scope(self):
        # Always every view: single-line diagrams are often floor plans,
        # not drafting views. With "Detail Items only" this is still fast.
        return SCOPE_ALL_VIEWS

    def _save(self):
        save_settings(self._param, self._detail_only(), self._scope())

    def _fill_param_combo(self):
        names = sorted((sorted(sp)[0] for sp in self._param_names.values()),
                       key=lambda n: n.lower())
        self._ready = False
        self.param_combo.ItemsSource = names
        self.param_combo.SelectedIndex = -1
        self.param_combo.Text = self._param
        self._ready = True

    def apply_parameter(self, name):
        name = (name or u"").strip()
        if not name:
            self._status(u"Enter a Detail Item parameter name.")
            return
        self.run_api(lambda uiapp: self.job_apply(uiapp, name),
                     u"Reading '{0}' values...".format(name))

    # ---- API-context jobs ---------------------------------------------
    def job_apply(self, uiapp, name):
        doc = uiapp.ActiveUIDocument.Document
        if doc_key(doc) != self._index_doc:
            self.job_rebuild(uiapp, name)
            return
        key = name_key(name)
        spellings = sorted(self._param_names.get(key, set([name])))
        name = spellings[0]
        self._param = name
        self._ready = False
        self.param_combo.Text = name
        self.search_in_combo.SelectedIndex = SEARCH_CHOSEN
        self._ready = True
        self._inspecting = False
        self._set_value_header(name)
        self._save()

        self._index = values_for_parameter(doc, self._records, spellings)
        ids = set(e.IdText for e in self._index)
        if key not in self._param_names:
            self.param_info.Text = (
                u"'{0}' does NOT exist on any scanned element. Untick 'Detail Items category only', or select one of your detail items in "
                u"Revit and click 'Pick Detail Item' to see its real parameter names, or set "
                u"Search in = 'ANY text parameter'.".format(name))
        elif not self._index:
            self.param_info.Text = u"'{0}' exists but is EMPTY on all scanned elements.".format(name)
        else:
            self.param_info.Text = (u"'{0}' has a value on {1} element(s) ({2} Detail Items)."
                                    .format(name, len(ids), len(ids & self._detail_ids)))
        self.search()

    def job_rebuild(self, uiapp, name=None):
        doc = uiapp.ActiveUIDocument.Document
        self._records, self._param_names, self._stats = scan_views(
            doc, self._detail_only(), self._scope())
        self._detail_ids = set(r.IdText for r in self._records if r.IsDetailItem)
        self._index_doc = doc_key(doc)
        self._fill_param_combo()
        self.job_apply(uiapp, name or self._param)

    def job_first_load(self, uiapp):
        self.job_rebuild(uiapp)
        self.job_selection(uiapp)

    def job_selection(self, uiapp):
        uidoc = uiapp.ActiveUIDocument
        doc = uidoc.Document
        if self._index_doc != doc_key(doc):
            self.job_rebuild(uiapp)
        ids = list(uidoc.Selection.GetElementIds())
        if len(ids) != 1:
            self.selection_text.Text = (u"Select exactly ONE element in Revit, then click "
                                        u"'Get From Selection'. ({0} selected)".format(len(ids)))
            self.source_combo.ItemsSource = []
            self.search()
            return
        element = doc.GetElement(ids[0])
        cat, fam, typ = describe_element(element)
        options = read_source_options(element)
        self.selection_text.Text = u"Selected: {0}  |  {1} : {2}  |  Id {3}".format(
            cat, fam, typ, id_value(element.Id))
        self.source_combo.ItemsSource = options
        if options:
            self.source_combo.SelectedIndex = 0
        else:
            self._status(u"Selected element has no parameter values.")

    def job_inspect(self, uiapp):
        """Show ALL parameters of the detail item selected in Revit."""
        uidoc = uiapp.ActiveUIDocument
        doc = uidoc.Document
        ids = list(uidoc.Selection.GetElementIds())
        if len(ids) != 1:
            self._status(u"Select exactly ONE detail item in Revit (e.g. the one showing "
                         u"'SMDB-...'), then click 'Pick Detail Item'.")
            return
        element = doc.GetElement(ids[0])
        owner_id = element.OwnerViewId
        view = doc.GetElement(owner_id) if owner_id != ElementId.InvalidElementId else None
        if view is not None:
            view_label = u"{0}  [{1}]".format(safe_name(view), view_type_text(view))
        else:
            view_label = u"<model element - not owned by a view>"
        type_id = element.GetTypeId()
        type_key = id_value(type_id) if type_id != ElementId.InvalidElementId else -1
        record = ElementRecord(element, owner_id, view_label,
                               element.Category is not None and id_value(element.Category.Id)
                               == int(BuiltInCategory.OST_DetailComponents),
                               type_key, describe_element(element))
        rows = []
        for owner, level in ((element, u"instance"), (element_type(element), u"type")):
            for pname, value, label in parameter_values(owner, level):
                rows.append(IndexEntry(record, value, label, pname))
        rows.sort(key=lambda e: e.Source.lower())

        notes = []
        if view is None:
            notes.append(u"It is NOT a view-specific element.")
        else:
            pass
        if not record.IsDetailItem:
            notes.append(u"Its category is '{0}', not Detail Items -> untick "
                         u"'Detail Items category only'.".format(record.CategoryName))
        try:
            if isinstance(element, FamilyInstance) and element.SuperComponent is not None:
                notes.append(u"It is a nested shared component.")
        except Exception:
            pass

        self._inspecting = True
        self._matches = rows
        self._set_value_header(u"Value")
        self._show()
        self.param_info.Text = (u"PICKED: {0} | {1} : {2} | Id {3} | View: {4}. {5}  "
                                u"-> Select the row that holds your value and click "
                                u"'Use This Parameter'.".format(
                                    record.CategoryName, record.FamilyName, record.TypeName,
                                    record.IdText, view_label, u" ".join(notes)))
        self._status(u"Showing {0} parameter value(s) of the picked element.".format(len(rows)))

    def job_search_any(self, uiapp, raw, mode):
        doc = uiapp.ActiveUIDocument.Document
        if doc_key(doc) != self._index_doc:
            self.job_rebuild(uiapp)
        self._inspecting = False
        self._matches = search_any_parameter(doc, self._records, make_matcher(raw, mode))
        self._set_value_header(u"Value")
        self._show()
        pnames = sorted(set(e.ParamName for e in self._matches))
        if self._matches:
            self._status(u"{0} hit(s) for '{1}' in parameter(s): {2}. Select a row and click "
                         u"'Use This Parameter'.  {3}".format(
                             len(self._matches), raw, u", ".join(pnames[:8]), self._summary()))
        else:
            self._status(u"'{0}' is not in ANY text parameter of the scanned elements. Untick "
                         u"'Detail Items category only' and search again.  {1}"
                         .format(raw, self._summary()))

    def job_open(self, uiapp, entry):
        uidoc = uiapp.ActiveUIDocument
        doc = uidoc.Document
        element = doc.GetElement(entry.ElementIdObj)
        if element is None:
            self._status(u"Element {0} no longer exists - click Rebuild Index.".format(entry.IdText))
            return
        view = None
        if entry.ViewIdObj is not None and entry.ViewIdObj != ElementId.InvalidElementId:
            view = doc.GetElement(entry.ViewIdObj)
        if view is not None:
            try:
                uidoc.ActiveView = view
            except Exception:
                uidoc.RequestViewChange(view)
        ids = List[ElementId]()
        ids.Add(element.Id)
        uidoc.Selection.SetElementIds(ids)
        try:
            uidoc.ShowElements(ids)
            self._status(u"Opened '{0}' and selected Id {1}.".format(entry.ViewName, entry.IdText))
        except Exception:
            self._status(u"Selected Id {0} (it may be hidden in the view).".format(entry.IdText))

    # ---- search ---------------------------------------------------------
    def search(self):
        mode = self.mode_combo.SelectedIndex
        raw = (self.value_box.Text or u"").strip()
        if self.search_in_combo.SelectedIndex == SEARCH_ANY:
            if not raw:
                self._status(u"Enter a value to search in ANY text parameter.")
                return
            if mode == MODE_ALL:
                mode = MODE_LOOSE
            self.run_api(lambda uiapp: self.job_search_any(uiapp, raw, mode),
                         u"Searching ALL text parameters for '{0}'...".format(raw))
            return

        self._inspecting = False
        self._set_value_header(self._param)
        if mode != MODE_ALL and not raw:
            self._matches = []
            self._show()
            self._status(u"Enter a value (or choose 'Show ALL'). " + self._summary())
            return
        hit = make_matcher(raw, mode)
        self._matches = [e for e in self._index if hit(e.Value)]
        self._show()
        if self._matches:
            self._status(u"{0} match(es).  {1}".format(len(self._matches), self._summary()))
        else:
            self._status(u"No match for '{0}' in '{1}'. Try Search in = 'ANY text parameter'.  {2}"
                         .format(raw, self._param, self._summary()))

    def _summary(self):
        s = self._stats
        if not s:
            return u""
        cats = u"Detail Items only" if s.get("detail_only") else u"all categories"
        return (u"[ALL {0} views, {1} elements scanned, {2}]"
                .format(s["views"], s["scanned"], cats))

    def _show(self):
        query = (self.filter_box.Text or u"").strip().lower()
        self.results_list.ItemsSource = [e for e in self._matches
                                         if not query or query in e.FilterText]

    # ---- UI events ------------------------------------------------------
    def on_param_selected(self, sender, args):
        if self._ready and self.param_combo.SelectedItem is not None:
            self.apply_parameter(self.param_combo.SelectedItem)

    def on_param_keydown(self, sender, args):
        if not self._ready:
            return
        if args.Key == Key.Enter:
            self.apply_parameter(self.param_combo.Text)
            args.Handled = True

    def on_param_apply(self, sender, args):
        if self._ready:
            self.apply_parameter(self.param_combo.Text)

    def on_pick_detail(self, sender, args):
        if self._ready:
            self.run_api(self.job_inspect, u"Reading picked element...")

    def on_use_parameter(self, sender, args):
        if not self._ready:
            return
        entry = self.results_list.SelectedItem
        if entry is None:
            self._status(u"Select a row first, then click 'Use This Parameter'.")
            return
        self.apply_parameter(entry.ParamName)

    def on_scope_changed(self, sender, args):
        if not self._ready or self._index_doc is None:
            return
        self._save()
        self.run_api(self.job_rebuild, u"Re-scanning views...")

    def on_detail_only_changed(self, sender, args):
        if not self._ready or self._index_doc is None:
            return
        self._save()
        self.run_api(self.job_rebuild, u"Re-scanning views...")

    def on_get_selection(self, sender, args):
        if self._ready:
            self.run_api(self.job_selection, u"Reading selection...")

    def on_rebuild_clicked(self, sender, args):
        if self._ready:
            self.run_api(self.job_rebuild, u"Scanning views...")

    def on_source_changed(self, sender, args):
        if not self._ready:
            return
        option = self.source_combo.SelectedItem
        if option is not None:
            self.value_box.Text = option.Value
            self.search()

    def on_search_clicked(self, sender, args):
        if self._ready:
            self.search()

    def on_value_keydown(self, sender, args):
        if self._ready and args.Key == Key.Enter:
            self.search()

    def on_mode_changed(self, sender, args):
        if self._ready and self.search_in_combo.SelectedIndex == SEARCH_CHOSEN:
            self.search()

    def on_search_in_changed(self, sender, args):
        pass    # takes effect on the next Search

    def on_filter_changed(self, sender, args):
        if self._ready:
            self._show()

    def _open_current(self):
        entry = self.results_list.SelectedItem
        if entry is None:
            self._status(u"Select a result first.")
            return
        self.run_api(lambda uiapp: self.job_open(uiapp, entry), u"Opening view...")

    def on_open_clicked(self, sender, args):
        if self._ready:
            self._open_current()

    def on_result_double_click(self, sender, args):
        if self._ready:
            self._open_current()

    def on_close_clicked(self, sender, args):
        self.Close()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main():
    queue = ApiQueue()
    event = ExternalEvent.Create(queue)       # must be created in API context
    window = FinderWindow(queue, event)
    window.Show()                              # MODELLESS - appears immediately
    window.run_api(window.job_first_load, u"Scanning views...")


main()

# -*- coding: utf-8 -*-
"""Copy text from any string parameter into each circuit's Load Name.

WHAT IT DOES
------------
1. Finds circuits in the current selection, the active view or the whole
   model -- directly, or THROUGH the electrical fixtures, electrical
   equipment and other devices you pick (their circuits are collected).
2. Optionally keeps only the circuits whose own parameter, or whose
   connected element's parameter, has a given value ("Filter by value").
3. Reads a text parameter from the circuit itself or from one of its
   connected elements (narrowed by category / family type).
4. Shows every circuit with its current and new Load Name BEFORE anything
   is changed.
5. On Apply, writes the new Load Names in ONE transaction (the only step
   that modifies the model; Cancel on the progress bar rolls it all back).

REVIT API USED
--------------
FilteredElementCollector(doc[, viewId]).OfCategory(...)   seeds per category
FamilyInstance.MEPModel.GetElectricalSystems()            circuits of a device
MEPModel.GetAssignedElectricalSystems()                   circuits a panel feeds
ElectricalSystem.BaseEquipment / .Elements / .CircuitNumber / .PanelName
Element.GetParameters(name), Parameter.AsString / AsValueString
BuiltInParameter.RBS_ELEC_CIRCUIT_NAME                    "Load Name"
Transaction, Parameter.Set

Targets pyRevit's IronPython 2.7 engine, Revit 2021 - 2026.
"""

__title__ = "Transfer\nLoad Name"
__author__ = 'BHEEM'

from pyrevit import DB, forms, revit, script
from pyrevit.forms import WPFWindow

from System import Object
from System.Collections.Generic import List
from System.Collections.ObjectModel import ObservableCollection
from System.Windows import RoutedEventHandler
from System.Windows.Controls.Primitives import ButtonBase
import safetable  # crash-safe output tables (lib/safetable.py)

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()
logger = script.get_logger()

TITLE = "Transfer Load Name"

# Cap on rows pushed into the DataGrid. Counts in the status line are always
# computed over the full set; this only limits what gets rendered.
MAX_PREVIEW_ROWS = 500
# Cap on the distinct values offered by the "pick a value" list.
MAX_PICK_VALUES = 300

# "Load Name" on an ElectricalSystem. Note this is NOT the same parameter as
# RBS_WIRE_CIRCUIT_LOAD_NAME, whose user-visible name is "Circuit Load Name"
# and which is a read-only reporting parameter on wires / connected elements.
LOAD_NAME_BIP = DB.BuiltInParameter.RBS_ELEC_CIRCUIT_NAME
LOAD_NAME_ALIASES = ("Load Name",)

MODE_CIRCUIT = "circuit"
MODE_ELEMENT = "element"

SCOPE_SELECTION = "selection"
SCOPE_VIEW = "view"
SCOPE_MODEL = "model"

# What a circuit was found through.
VIA_CIRCUIT = "circuits"
VIA_FIXTURE = "fixtures"
VIA_EQUIPMENT = "equipment"
VIA_OTHER = "other"
VIA_LABELS = [(VIA_CIRCUIT, "as circuits"),
              (VIA_FIXTURE, "via fixtures"),
              (VIA_EQUIPMENT, "via equipment"),
              (VIA_OTHER, "via other devices")]

# Which circuits a piece of electrical equipment contributes.
EQUIP_SUPPLY = "The circuit that supplies it"
EQUIP_FED = "The circuits it feeds (panels)"
EQUIP_BOTH = "Both"
EQUIP_MODES = [EQUIP_SUPPLY, EQUIP_FED, EQUIP_BOTH]

# Categories searched for "other devices" in the view / model scopes.
# Looked up by name so a Revit version lacking one of them is harmless.
OTHER_DEVICE_CATEGORIES = (
    "OST_LightingFixtures", "OST_LightingDevices", "OST_DataDevices",
    "OST_FireAlarmDevices", "OST_CommunicationDevices", "OST_SecurityDevices",
    "OST_NurseCallDevices", "OST_TelephoneDevices", "OST_MechanicalEquipment",
)

NO_CATEGORY = "<no category>"
NO_FILTER = "<no filter>"

# Filter-by-value conditions.
OP_EQUALS = "equals"
OP_NOT_EQUALS = "does not equal"
OP_CONTAINS = "contains"
OP_NOT_CONTAINS = "does not contain"
OP_STARTS = "starts with"
OP_ENDS = "ends with"
OP_EMPTY = "is empty"
OP_NOT_EMPTY = "is not empty"
OPERATORS = [OP_EQUALS, OP_NOT_EQUALS, OP_CONTAINS, OP_NOT_CONTAINS,
             OP_STARTS, OP_ENDS, OP_EMPTY, OP_NOT_EMPTY]
OPERATORS_WITHOUT_VALUE = (OP_EMPTY, OP_NOT_EMPTY)

# Preview action -> (text colour, chip background).
ACTION_UPDATE = "Update"
ACTION_STYLE = {
    ACTION_UPDATE: ("#FF116D4A", "#FFE3F4EC"),
    "No change": ("#FF667585", "#FFEEF2F6"),
    "Already set": ("#FF9A5200", "#FFFDEFD9"),
    "Source empty": ("#FF9A5200", "#FFFDEFD9"),
    "No element passes filter": ("#FF9A5200", "#FFFDEFD9"),
    "Read-only": ("#FFA5281B", "#FFFBE4E1"),
    "No Load Name param": ("#FFA5281B", "#FFFBE4E1"),
}


def bindable(items):
    """Wrap a Python sequence in a collection WPF can bind to.

    Assigning a raw Python list to ItemsSource happens to work in some
    IronPython builds and silently binds to nothing in others.
    """
    collection = ObservableCollection[Object]()
    for item in items:
        collection.Add(item)
    return collection


# ---------------------------------------------------------------------------
# Version-safe helpers
# ---------------------------------------------------------------------------

def eid_value(element_id):
    """ElementId.Value (Revit 2024+) falling back to IntegerValue (<= 2023)."""
    if element_id is None:
        return -1
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue


def bic_value(name):
    """int of a BuiltInCategory looked up by name, or None if this Revit
    version does not have it."""
    category = getattr(DB.BuiltInCategory, name, None)
    if category is None:
        return None
    try:
        return int(category)
    except Exception:
        return None


def safe_name(element):
    """Name of an element or element type. Never raises.

    FIX: element.Name is not readable from IronPython for most element TYPES
    (FamilySymbol and friends only expose the setter), so every entry of the
    Family : Type list used to read "Family : Id 123456". The base-class
    getter and the type-name parameter are tried before giving up.
    """
    if element is None:
        return ""
    try:
        name = element.Name
        if name:
            return name
    except Exception:
        pass
    try:
        name = DB.Element.Name.GetValue(element)
        if name:
            return name
    except Exception:
        pass
    for bip_name in ("SYMBOL_NAME_PARAM", "ALL_MODEL_TYPE_NAME"):
        bip = getattr(DB.BuiltInParameter, bip_name, None)
        if bip is None:
            continue
        try:
            parameter = element.get_Parameter(bip)
            if parameter is not None and parameter.AsString():
                return parameter.AsString()
        except Exception:
            pass
    return "Id {0}".format(eid_value(element.Id))


def category_of(element):
    """(category id, category name) for an element, both always safe."""
    try:
        category = element.Category
    except Exception:
        category = None
    if category is None:
        return -1, NO_CATEGORY
    try:
        name = category.Name or NO_CATEGORY
    except Exception:
        name = NO_CATEGORY
    return eid_value(category.Id), name


def element_type_of(element):
    try:
        type_id = element.GetTypeId()
    except Exception:
        return None
    if eid_value(type_id) <= 0:
        return None
    return doc.GetElement(type_id)


def type_of(element):
    """(type id, "Family : Type" label) for an element, both always safe."""
    try:
        type_id = element.GetTypeId()
    except Exception:
        return -1, safe_name(element)
    key = eid_value(type_id)
    if key <= 0:
        return key, safe_name(element)
    element_type = doc.GetElement(type_id)
    if element_type is None:
        return key, safe_name(element)
    type_name = safe_name(element_type)
    family_name = getattr(element_type, "FamilyName", None)
    if not family_name:
        parameter = element_type.get_Parameter(
            DB.BuiltInParameter.ALL_MODEL_FAMILY_NAME)
        if parameter is not None:
            family_name = parameter.AsString()
    if family_name:
        return key, "{0} : {1}".format(family_name, type_name)
    return key, type_name


def is_text_param(parameter):
    return (parameter is not None
            and parameter.StorageType == DB.StorageType.String)


def load_name_param(circuit):
    """Resolve the writable Load Name parameter on a circuit.

    get_Parameter returns None when the parameter is absent, it does not
    raise, so a try/except around it can never trigger a fallback.
    """
    parameter = circuit.get_Parameter(LOAD_NAME_BIP)
    if parameter is not None:
        return parameter
    for alias in LOAD_NAME_ALIASES:
        parameter = circuit.LookupParameter(alias)
        if is_text_param(parameter):
            return parameter
    return None


def parameter_display(parameter, text_only):
    """Value of a parameter as the user sees it, or None when it does not
    qualify (text_only and the parameter is not a string)."""
    try:
        if parameter.StorageType == DB.StorageType.String:
            return parameter.AsString() or ""
        if text_only:
            return None
        return parameter.AsValueString() or ""
    except Exception:
        return None


def parameters_named(element, name):
    """Every parameter of that name on the element.

    Element.GetParameters(name) returns all of them (a family parameter and
    a shared parameter can share a name; LookupParameter returns only the
    first, which may be the empty one).
    """
    try:
        return list(element.GetParameters(name))
    except Exception:
        pass
    found = []
    try:
        for parameter in element.Parameters:
            try:
                if parameter.Definition.Name == name:
                    found.append(parameter)
            except Exception:
                continue
    except Exception:
        pass
    return found


class ValueReader(object):
    """Reads parameter values and remembers them.

    FIX (performance): the preview used to walk every parameter of every
    candidate element again on each keystroke in Prefix / Suffix and on each
    option click -- on a few hundred circuits that made the window stall
    while typing. Values are now read once per (element, parameter) and
    reused until Refresh.

    An INSTANCE value wins; when the instance has none, the element's TYPE
    is tried, so type parameters (Type Mark, Description ...) work as a
    source and as a filter too.
    """

    def __init__(self):
        self._cache = {}

    def clear(self):
        self._cache = {}

    def text(self, element, name, text_only=False):
        key = (eid_value(element.Id), name, text_only)
        if key in self._cache:
            return self._cache[key]
        value = self._read(element, name, text_only)
        if not (value and value.strip()):
            element_type = element_type_of(element)
            if element_type is not None:
                type_value = self._read(element_type, name, text_only)
                if type_value and type_value.strip():
                    value = type_value
        self._cache[key] = value
        return value

    @staticmethod
    def _read(element, name, text_only):
        """First non-empty value among the parameters of that name; '' when
        the parameter exists but is empty; None when it does not exist."""
        fallback = None
        for parameter in parameters_named(element, name):
            value = parameter_display(parameter, text_only)
            if value is None:
                continue
            if value.strip():
                return value
            if fallback is None:
                fallback = ""
        return fallback


# ---------------------------------------------------------------------------
# Circuit gathering
# ---------------------------------------------------------------------------

def is_circuit(element):
    return isinstance(element, DB.Electrical.ElectricalSystem)


def seed_kind(element):
    """Which "find circuits through" tick box an element belongs to."""
    if is_circuit(element):
        return VIA_CIRCUIT
    category_id = category_of(element)[0]
    if category_id == bic_value("OST_ElectricalFixtures"):
        return VIA_FIXTURE
    if category_id == bic_value("OST_ElectricalEquipment"):
        return VIA_EQUIPMENT
    return VIA_OTHER


def systems_of(element):
    """(supply circuits, circuits fed) for a family instance.

    FIX: the old code returned the result of the FIRST API call that gave
    anything. For a panel that is the one circuit supplying it, so the
    circuits the panel feeds were never found when a panel was selected.
    Every available call is combined here and each circuit is classified by
    its BaseEquipment: a circuit whose base equipment is this element is one
    it FEEDS, any other is one that SUPPLIES it.
    """
    mep_model = getattr(element, "MEPModel", None)
    if mep_model is None:
        return [], []
    found = {}
    for attr in ("GetElectricalSystems", "GetAssignedElectricalSystems"):
        getter = getattr(mep_model, attr, None)
        if getter is None:
            continue
        try:
            for system in getter() or []:
                if is_circuit(system):
                    found[eid_value(system.Id)] = system
        except Exception:
            continue
    if not found:
        try:                                     # Revit <= 2020
            for system in getattr(mep_model, "ElectricalSystems", None) or []:
                if is_circuit(system):
                    found[eid_value(system.Id)] = system
        except Exception:
            pass

    own_id = eid_value(element.Id)
    supply, fed = [], []
    for system in found.values():
        try:
            base = system.BaseEquipment
        except Exception:
            base = None
        if base is not None and eid_value(base.Id) == own_id:
            fed.append(system)
        else:
            supply.append(system)
    return supply, fed


def circuits_of(element, equipment_mode=EQUIP_BOTH):
    """Circuits an element contributes. Equipment follows equipment_mode;
    every other device contributes the circuit(s) it is connected to."""
    supply, fed = systems_of(element)
    if seed_kind(element) != VIA_EQUIPMENT:
        return supply + fed
    if equipment_mode == EQUIP_SUPPLY:
        return supply
    if equipment_mode == EQUIP_FED:
        return fed
    return supply + fed


def scope_elements(scope, kinds):
    """Candidate seed elements for a scope, limited to the ticked kinds."""
    if scope == SCOPE_SELECTION:
        elements = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
        return [e for e in elements if e is not None]

    view_id = None
    if scope == SCOPE_VIEW:
        view = doc.ActiveView
        if view is None or view.IsTemplate:
            return []
        view_id = view.Id

    def collect(category_name):
        category = getattr(DB.BuiltInCategory, category_name, None)
        if category is None:
            return []
        if view_id is None:
            collector = DB.FilteredElementCollector(doc)
        else:
            collector = DB.FilteredElementCollector(doc, view_id)
        return list(collector.OfCategory(category).WhereElementIsNotElementType())

    elements = []
    if VIA_CIRCUIT in kinds:
        # Circuits have no graphics, so a view-scoped collector finds none;
        # in the view scope circuits come through the devices instead.
        elements.extend(e for e in collect("OST_ElectricalCircuit") if is_circuit(e))
    if VIA_FIXTURE in kinds:
        elements.extend(collect("OST_ElectricalFixtures"))
    if VIA_EQUIPMENT in kinds:
        elements.extend(collect("OST_ElectricalEquipment"))
    if VIA_OTHER in kinds:
        for name in OTHER_DEVICE_CATEGORIES:
            elements.extend(collect(name))
    return elements


def gather_circuits(scope, kinds, equipment_mode):
    """(circuits sorted by id, {kind: how many circuits came through it})."""
    found = {}
    counts = {}
    for element in scope_elements(scope, kinds):
        kind = seed_kind(element)
        if kind not in kinds:
            continue
        circuits = [element] if kind == VIA_CIRCUIT else circuits_of(element, equipment_mode)
        for circuit in circuits:
            key = eid_value(circuit.Id)
            if key not in found:
                found[key] = circuit
                counts[kind] = counts.get(kind, 0) + 1
    return [found[key] for key in sorted(found)], counts


class CircuitElements(object):
    """Connected elements per circuit, read once and kept until Refresh."""

    def __init__(self):
        self._cache = {}

    def clear(self):
        self._cache = {}

    def of(self, circuit):
        key = eid_value(circuit.Id)
        if key not in self._cache:
            try:
                elements = [e for e in circuit.Elements]
            except Exception:
                elements = []
            elements.sort(key=lambda e: eid_value(e.Id))
            self._cache[key] = elements
        return self._cache[key]


# ---------------------------------------------------------------------------
# Filter by parameter value
# ---------------------------------------------------------------------------

def value_matches(value, operator, needle):
    """Case-insensitive test of one value against a condition."""
    text = (value or "").strip().lower()
    wanted = (needle or "").strip().lower()
    if operator == OP_EMPTY:
        return not text
    if operator == OP_NOT_EMPTY:
        return bool(text)
    if operator == OP_EQUALS:
        return text == wanted
    if operator == OP_NOT_EQUALS:
        return text != wanted
    if operator == OP_CONTAINS:
        return wanted in text
    if operator == OP_NOT_CONTAINS:
        return wanted not in text
    if operator == OP_STARTS:
        return text.startswith(wanted)
    if operator == OP_ENDS:
        return text.endswith(wanted)
    return True


class ValueFilter(object):
    """Keep circuits by the value of a parameter.

    target MODE_CIRCUIT : the parameter is read on the circuit itself.
    target MODE_ELEMENT : a circuit passes when at least one of its
                          connected elements matches -- and only those
                          matching elements may then supply the text.
    """

    def __init__(self, target=MODE_CIRCUIT, name="", operator=OP_EQUALS, needle=""):
        self.target = target
        self.name = name or ""
        self.operator = operator
        self.needle = needle or ""

    @property
    def active(self):
        if not self.name or self.name == NO_FILTER:
            return False
        return self.operator in OPERATORS_WITHOUT_VALUE or bool(self.needle.strip())

    def element_passes(self, element, reader):
        return value_matches(reader.text(element, self.name), self.operator, self.needle)

    def circuit_passes(self, circuit, reader, members):
        if not self.active:
            return True
        if self.target == MODE_CIRCUIT:
            return self.element_passes(circuit, reader)
        return any(self.element_passes(e, reader) for e in members.of(circuit))

    def describe(self):
        if not self.active:
            return ""
        where = "circuit" if self.target == MODE_CIRCUIT else "connected element"
        if self.operator in OPERATORS_WITHOUT_VALUE:
            return "{0} '{1}' {2}".format(where, self.name, self.operator)
        return "{0} '{1}' {2} '{3}'".format(where, self.name, self.operator,
                                            self.needle.strip())


# ---------------------------------------------------------------------------
# Source filtering
# ---------------------------------------------------------------------------

class FacetItem(object):
    """A checkable category or family-type entry in the filter sidebar.

    Bound one-way for Display and two-way for IsSelected. There is no
    INotifyPropertyChanged here on purpose: implementing .NET events from
    IronPython is fiddly, and nothing needs source-to-UI push. When the code
    sets IsSelected itself (the All / None buttons) it rebinds the whole
    ItemsSource instead.
    """

    def __init__(self, key, label, count, category_id=None):
        self.Key = key
        self.Label = label
        self.Count = count
        self.Display = "{0}  ({1})".format(label, count)
        self.IsSelected = True
        self.CategoryId = category_id


class SourceFilter(object):
    """Which elements on a circuit are allowed to supply the text."""

    def __init__(self, mode, members, category_ids=None, type_ids=None,
                 value_filter=None, reader=None):
        self.mode = mode
        self.members = members
        # None means "no restriction", which skips the membership test
        # entirely rather than building a set of everything.
        self.category_ids = category_ids
        self.type_ids = type_ids
        self.value_filter = value_filter
        self.reader = reader

    def elements_for(self, circuit):
        if self.mode == MODE_CIRCUIT:
            return [circuit]
        elements = self.members.of(circuit)
        if self.category_ids is not None:
            elements = [e for e in elements
                        if category_of(e)[0] in self.category_ids]
        if self.type_ids is not None:
            elements = [e for e in elements
                        if type_of(e)[0] in self.type_ids]
        flt = self.value_filter
        if flt is not None and flt.active and flt.target == MODE_ELEMENT:
            elements = [e for e in elements if flt.element_passes(e, self.reader)]
        return elements


def collect_facets(circuits, members):
    """Category and family-type facets across every connected element.

    One pass, no parameter reads, so this stays cheap even model-wide.
    """
    categories = {}
    types = {}
    for circuit in circuits:
        for element in members.of(circuit):
            category_id, category_name = category_of(element)
            if category_id in categories:
                categories[category_id][1] += 1
            else:
                categories[category_id] = [category_name, 1]

            type_id, type_label = type_of(element)
            if type_id in types:
                types[type_id][2] += 1
            else:
                types[type_id] = [type_label, category_id, 1]

    category_items = [FacetItem(key, value[0], value[1])
                      for key, value in categories.items()]
    category_items.sort(key=lambda i: i.Label.lower())

    type_items = [FacetItem(key, value[0], value[2], category_id=value[1])
                  for key, value in types.items()]
    type_items.sort(key=lambda i: i.Label.lower())
    return category_items, type_items


def scan_parameter_names(elements, text_only):
    """Sorted parameter names found on the elements and on their types.

    An element's parameter set is a function of its type and category, so one
    representative per (type id, category id) is enough. On a real project
    this turns a five-figure parameter walk into a two-figure one.
    """
    names = set()
    seen_keys = set()
    none_storage = getattr(DB.StorageType, "None", None)
    for element in elements:
        key = (type_of(element)[0], category_of(element)[0])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        for owner in (element, element_type_of(element)):
            if owner is None:
                continue
            try:
                parameters = list(owner.Parameters)
            except Exception:
                continue
            for parameter in parameters:
                try:
                    storage = parameter.StorageType
                    if text_only and storage != DB.StorageType.String:
                        continue
                    if none_storage is not None and storage == none_storage:
                        continue
                    names.add(parameter.Definition.Name)
                except Exception:
                    continue
    return sorted(names, key=lambda n: n.lower())


# ---------------------------------------------------------------------------
# Preview model
# ---------------------------------------------------------------------------

def circuit_label(circuit):
    try:
        number = circuit.CircuitNumber or ""
    except Exception:
        number = ""
    if number:
        return number
    return "Id {0}".format(eid_value(circuit.Id))


def panel_label(circuit):
    try:
        return circuit.PanelName or "<unassigned>"
    except Exception:
        return "<unassigned>"


class PreviewRow(object):
    """Row object for the DataGrid.

    Property names are PascalCase because they are bound from XAML. "New" is
    a reserved word in binding paths, hence NewValue.
    """

    def __init__(self, circuit, current, new_value, source, category, status,
                 will_change):
        self.Circuit = circuit_label(circuit)
        self.Panel = panel_label(circuit)
        self.Current = current
        self.NewValue = new_value
        self.Source = source
        self.Category = category
        self.Status = status
        self.StatusColor, self.StatusBack = ACTION_STYLE.get(
            status, ACTION_STYLE["No change"])
        self.circuit_id = circuit.Id
        self.source_id = None
        self.will_change = will_change
        self._haystack = " ".join([
            self.Circuit, self.Panel, self.Current, self.NewValue,
            self.Source, self.Category, self.Status]).lower()

    def matches(self, terms):
        return all(term in self._haystack for term in terms)


def build_preview(circuits, source_filter, reader, param_name, overwrite, trim,
                  prefix, suffix):
    rows = []
    for circuit in circuits:
        target = load_name_param(circuit)
        if target is None:
            rows.append(PreviewRow(circuit, "", "", "", "",
                                   "No Load Name param", False))
            continue
        current = target.AsString() or ""
        if target.IsReadOnly:
            rows.append(PreviewRow(circuit, current, "", "", "",
                                   "Read-only", False))
            continue

        candidates = source_filter.elements_for(circuit)
        if not candidates:
            rows.append(PreviewRow(circuit, current, "", "", "",
                                   "No element passes filter", False))
            continue

        value = None
        source_element = None
        for element in candidates:
            candidate = reader.text(element, param_name, True)
            if candidate and candidate.strip():
                value = candidate
                source_element = element
                break

        if value is None:
            rows.append(PreviewRow(circuit, current, "", "", "",
                                   "Source empty", False))
            continue

        if trim:
            value = value.strip()
        value = "{0}{1}{2}".format(prefix, value, suffix)

        if source_filter.mode == MODE_CIRCUIT:
            source_text = "(this circuit)"
            category_text = "Electrical Circuits"
        else:
            source_text = safe_name(source_element)
            category_text = category_of(source_element)[1]

        if current and not overwrite:
            rows.append(PreviewRow(circuit, current, value, source_text,
                                   category_text, "Already set", False))
            continue
        if current == value:
            rows.append(PreviewRow(circuit, current, value, source_text,
                                   category_text, "No change", False))
            continue

        row = PreviewRow(circuit, current, value, source_text, category_text,
                         ACTION_UPDATE, True)
        if source_element is not None and source_filter.mode == MODE_ELEMENT:
            row.source_id = source_element.Id
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class TransferLoadNameWindow(WPFWindow):

    def __init__(self, xaml_path, preselected):
        WPFWindow.__init__(self, xaml_path)
        self._ready = False
        self._all_circuits = []       # found in the scope
        self._circuits = []           # ... that also pass the value filter
        self._all_categories = []
        self._all_types = []
        self._type_states = {}
        self._facets_stale = True
        self._param_names = []
        self._wanted_param = None     # source parameter the user last picked
        self._filter_names = []
        self._rows = []
        self._visible_rows = []
        self.reader = ValueReader()
        self.members = CircuitElements()
        self.result = None

        try:
            self.doc_name.Text = doc.Title
        except Exception:
            pass
        self.equip_mode.ItemsSource = bindable(EQUIP_MODES)
        self.equip_mode.SelectedIndex = 2
        self.flt_op.ItemsSource = bindable(OPERATORS)
        self.flt_op.SelectedIndex = 0
        self.flt_target_circuit.IsChecked = True
        self.mode_circuit.IsChecked = True
        if preselected:
            self.scope_selection.IsChecked = True
        else:
            self.scope_selection.IsEnabled = False
            self.scope_selection.ToolTip = "Nothing was selected in Revit when the tool started"
            self.scope_view.IsChecked = True

        self._wire_events()
        self._fit_to_screen()
        self._ready = True
        self.reload_circuits()

    def _fit_to_screen(self):
        """Never open larger than the usable screen (small laptops)."""
        try:
            from System.Windows import SystemParameters
            area = SystemParameters.WorkArea
            self.Width = max(self.MinWidth, min(self.Width, area.Width - 40))
            self.Height = max(self.MinHeight, min(self.Height, area.Height - 40))
        except Exception:
            pass

    def _wire_events(self):
        """All handlers are attached here, not in the XAML.

        IronPython's XAML reader cannot bind handlers inside a DataTemplate
        (the facet tick boxes) nor attached events (CheckBox.Click on a
        ListBox), so those have to be wired in code anyway; keeping every
        handler in one place makes the XAML restylable without touching
        behaviour. Tick boxes inside the two facet lists are caught as their
        Click bubbles up to the ListBox.
        """
        for radio in (self.scope_selection, self.scope_view, self.scope_model):
            radio.Checked += self.on_scope_changed
        for box in (self.via_circuits, self.via_fixtures, self.via_equipment,
                    self.via_other):
            box.Click += self.on_scope_changed
        self.equip_mode.SelectionChanged += self.on_scope_changed

        self.flt_target_circuit.Checked += self.on_filter_target_changed
        self.flt_target_element.Checked += self.on_filter_target_changed
        self.flt_param_filter.TextChanged += self.on_filter_param_search
        self.flt_param.SelectionChanged += self.on_filter_param_changed
        self.flt_op.SelectionChanged += self.on_filter_changed
        self.flt_value.TextChanged += self.on_filter_changed
        self.flt_values.SelectionChanged += self.on_filter_value_picked
        self.btn_flt_clear.Click += self.on_filter_clear

        self.mode_circuit.Checked += self.on_mode_changed
        self.mode_element.Checked += self.on_mode_changed
        self.param_filter.TextChanged += self.on_param_filter_changed
        self.param_combo.SelectionChanged += self.on_param_changed

        self.cat_list.AddHandler(ButtonBase.ClickEvent,
                                 RoutedEventHandler(self.on_category_changed))
        self.type_list.AddHandler(ButtonBase.ClickEvent,
                                  RoutedEventHandler(self.on_type_changed))
        self.type_filter.TextChanged += self.on_type_filter_changed
        self.btn_cat_all.Click += self.on_cat_all_click
        self.btn_cat_none.Click += self.on_cat_none_click
        self.btn_type_all.Click += self.on_type_all_click
        self.btn_type_none.Click += self.on_type_none_click

        self.opt_overwrite.Click += self.on_option_changed
        self.opt_trim.Click += self.on_option_changed
        self.opt_prefix.TextChanged += self.on_option_changed
        self.opt_suffix.TextChanged += self.on_option_changed

        self.row_search.TextChanged += self.on_row_search_changed
        self.btn_search_clear.Click += self.on_search_clear_click
        self.opt_changes_only.Click += self.on_row_search_changed
        self.preview_grid.MouseDoubleClick += self.on_row_double_click

        self.btn_refresh.Click += self.on_refresh_click
        self.btn_apply.Click += self.on_apply_click
        self.btn_cancel.Click += self.on_cancel_click

    # -- state -------------------------------------------------------------

    @property
    def scope(self):
        if self.scope_selection.IsChecked:
            return SCOPE_SELECTION
        if self.scope_view.IsChecked:
            return SCOPE_VIEW
        return SCOPE_MODEL

    @property
    def kinds(self):
        pairs = ((self.via_circuits, VIA_CIRCUIT), (self.via_fixtures, VIA_FIXTURE),
                 (self.via_equipment, VIA_EQUIPMENT), (self.via_other, VIA_OTHER))
        return set(kind for box, kind in pairs if box.IsChecked)

    @property
    def mode(self):
        return MODE_CIRCUIT if self.mode_circuit.IsChecked else MODE_ELEMENT

    @property
    def selected_param(self):
        item = self.param_combo.SelectedItem
        return item if item else None

    def set_status(self, text):
        self.status_text.Text = text

    def value_filter(self):
        name = self.flt_param.SelectedItem
        return ValueFilter(
            MODE_CIRCUIT if self.flt_target_circuit.IsChecked else MODE_ELEMENT,
            "" if (not name or name == NO_FILTER) else name,
            self.flt_op.SelectedItem or OP_EQUALS,
            self.flt_value.Text or "")

    def current_filter(self):
        """Build a SourceFilter from the sidebar state.

        A facet list with everything ticked passes None, so the common case
        costs no per-element membership tests.
        """
        if self.mode == MODE_CIRCUIT:
            return SourceFilter(MODE_CIRCUIT, self.members)

        category_ids = None
        if self._all_categories:
            chosen = set(i.Key for i in self._all_categories if i.IsSelected)
            if len(chosen) != len(self._all_categories):
                category_ids = chosen

        type_ids = None
        if self._all_types:
            chosen = set(i.Key for i in self._all_types
                         if self._type_states.get(i.Key, True))
            if len(chosen) != len(self._all_types):
                type_ids = chosen

        return SourceFilter(MODE_ELEMENT, self.members, category_ids, type_ids,
                            self.value_filter(), self.reader)

    # -- data: circuits ----------------------------------------------------

    def reload_circuits(self):
        """Gather circuits for the scope and the ticked seed kinds."""
        if not self._ready:
            return
        self.equip_mode.IsEnabled = bool(self.via_equipment.IsChecked)
        self.reader.clear()
        self.members.clear()
        try:
            self._all_circuits, counts = gather_circuits(
                self.scope, self.kinds, self.equip_mode.SelectedItem or EQUIP_BOTH)
        except Exception as error:
            logger.debug("Gathering circuits failed: %s", error)
            self._all_circuits, counts = [], {}

        parts = ["{0} {1}".format(counts[kind], label)
                 for kind, label in VIA_LABELS if counts.get(kind)]
        note = "{0} circuit(s) found".format(len(self._all_circuits))
        if parts:
            note += ":  " + ",  ".join(parts)
        if self.scope == SCOPE_VIEW and VIA_CIRCUIT in self.kinds:
            note += ".  Circuits have no graphics, so in a view they are found through the devices."
        self.scope_note.Text = note
        self.reload_filter_parameters()

    # -- data: filter by value ---------------------------------------------

    def _filter_targets(self):
        """Elements the value filter's parameter list is built from."""
        if self.flt_target_circuit.IsChecked:
            return list(self._all_circuits)
        elements = []
        for circuit in self._all_circuits:
            elements.extend(self.members.of(circuit))
        return elements

    def reload_filter_parameters(self):
        if not self._ready:
            return
        previous = self.flt_param.SelectedItem
        self._filter_names = scan_parameter_names(self._filter_targets(), False)
        self.apply_filter_param_search(keep=previous)

    def apply_filter_param_search(self, keep=None):
        needle = (self.flt_param_filter.Text or "").strip().lower()
        names = [n for n in self._filter_names if needle in n.lower()] \
            if needle else list(self._filter_names)
        visible = [NO_FILTER] + names
        self._ready = False
        self.flt_param.ItemsSource = bindable(visible)
        self.flt_param.SelectedItem = keep if (keep and keep in visible) else NO_FILTER
        self._ready = True
        self.reload_filter_values()

    def reload_filter_values(self):
        """Offer the values that actually occur for the chosen parameter."""
        name = self.flt_param.SelectedItem
        values = []
        if name and name != NO_FILTER:
            seen = set()
            for element in self._filter_targets():
                value = (self.reader.text(element, name) or "").strip()
                if value and value not in seen:
                    seen.add(value)
                    if len(seen) >= MAX_PICK_VALUES:
                        break
            values = sorted(seen, key=lambda v: v.lower())
        self._ready = False
        self.flt_values.ItemsSource = bindable(values)
        self.flt_values.SelectedIndex = -1
        self.flt_values.IsEnabled = bool(values)
        self._ready = True
        self.apply_value_filter()

    def apply_value_filter(self):
        """Reduce the circuits in scope to those passing the value filter."""
        if not self._ready:
            return
        flt = self.value_filter()
        needs_value = (self.flt_op.SelectedItem or OP_EQUALS) not in OPERATORS_WITHOUT_VALUE
        self.flt_value.IsEnabled = needs_value
        self.flt_values.IsEnabled = needs_value and self.flt_values.Items.Count > 0
        self._circuits = [c for c in self._all_circuits
                          if flt.circuit_passes(c, self.reader, self.members)]
        if flt.active:
            self.flt_note.Text = "Keeps {0} of {1} circuit(s):  {2}".format(
                len(self._circuits), len(self._all_circuits), flt.describe())
        elif flt.name:
            self.flt_note.Text = "Type or pick a value to filter on."
        else:
            self.flt_note.Text = "No filter: all {0} circuit(s) are processed.".format(
                len(self._all_circuits))
        self._facets_stale = True
        self.rebuild_facets()

    # -- data: source ------------------------------------------------------

    def rebuild_facets(self):
        """Recompute category / type facets for the current circuit set.

        Collected lazily: in circuit mode the facets are never consulted, so
        walking every connected element in the model to build them would be
        pure waste. Switching to element mode collects them then.
        """
        element_mode = self.mode == MODE_ELEMENT
        self.element_filter_group.IsEnabled = element_mode
        self.element_filter_group.IsExpanded = element_mode

        if element_mode and self._facets_stale:
            previous_categories = dict((i.Key, i.IsSelected) for i in self._all_categories)
            previous_types = dict(self._type_states)
            self._all_categories, self._all_types = collect_facets(
                self._circuits, self.members)
            # Keep the user's ticks when the circuit set changes (a new
            # filter value must not silently re-tick every category).
            for item in self._all_categories:
                item.IsSelected = previous_categories.get(item.Key, True)
            self._type_states = dict((i.Key, previous_types.get(i.Key, True))
                                     for i in self._all_types)
            self._facets_stale = False

            self._ready = False
            self.cat_list.ItemsSource = bindable(self._all_categories)
            self._ready = True

        self.refresh_type_list()

    def refresh_type_list(self):
        """Show types belonging to ticked categories, honouring the search box."""
        allowed = set(i.Key for i in self._all_categories if i.IsSelected)
        needle = (self.type_filter.Text or "").strip().lower()

        visible = []
        for item in self._all_types:
            if item.CategoryId not in allowed:
                continue
            if needle and needle not in item.Label.lower():
                continue
            item.IsSelected = self._type_states.get(item.Key, True)
            visible.append(item)

        self._ready = False
        self.type_list.ItemsSource = bindable(visible)
        self._ready = True

        self.facet_note.Text = "{0} category(ies), {1} of {2} type(s) shown".format(
            len(self._all_categories), len(visible), len(self._all_types))
        self.reload_parameters()

    def reload_parameters(self):
        if not self._ready:
            return
        # The parameter the USER last picked, not whatever is selected now:
        # while a filter value is being typed the circuit set can be empty
        # for a moment, the list empties, and the choice used to fall back
        # to the first parameter in the alphabet for good.
        previous = self._wanted_param or self.selected_param
        source_filter = self.current_filter()
        elements = []
        for circuit in self._circuits:
            elements.extend(source_filter.elements_for(circuit))
        self._param_names = scan_parameter_names(elements, True)
        self.apply_param_filter(keep=previous)

    def apply_param_filter(self, keep=None):
        needle = (self.param_filter.Text or "").strip().lower()
        if needle:
            visible = [n for n in self._param_names if needle in n.lower()]
        else:
            visible = list(self._param_names)

        # Suppress the SelectionChanged storm while the list is rebuilt.
        self._ready = False
        self.param_combo.ItemsSource = bindable(visible)
        if keep and keep in visible:
            self.param_combo.SelectedItem = keep
        elif visible:
            self.param_combo.SelectedIndex = 0
        self._ready = True

        self.param_note.Text = "{0} text parameter(s) available".format(
            len(self._param_names))
        self.refresh_preview()

    def refresh_preview(self):
        if not self._ready:
            return
        param_name = self.selected_param
        if not self._all_circuits:
            self._rows = []
            self.render_rows("No circuits found. Check the scope and the 'find circuits through' boxes.")
            return
        if not self._circuits:
            self._rows = []
            self.render_rows("No circuit passes the value filter.")
            return
        if not param_name:
            self._rows = []
            self.render_rows("No text parameter selected.")
            return

        self._rows = build_preview(
            self._circuits,
            self.current_filter(),
            self.reader,
            param_name,
            bool(self.opt_overwrite.IsChecked),
            bool(self.opt_trim.IsChecked),
            self.opt_prefix.Text or "",
            self.opt_suffix.Text or "",
        )
        self.render_rows()

    def render_rows(self, empty_message=None):
        """Apply the search box to the built rows and repaint the grid.

        Search is not cosmetic: Apply acts on the rows left visible here, so
        the status line spells the number out rather than leaving the user to
        infer it.
        """
        terms = [t for t in (self.row_search.Text or "").lower().split() if t]
        rows = [r for r in self._rows if r.matches(terms)] if terms else list(self._rows)
        if self.opt_changes_only.IsChecked:
            rows = [r for r in rows if r.will_change]

        self._visible_rows = rows
        change_count = len([r for r in rows if r.will_change])

        self.preview_grid.ItemsSource = bindable(rows[:MAX_PREVIEW_ROWS])
        self.btn_apply.IsEnabled = change_count > 0
        self.btn_apply.Content = ("APPLY ({0})".format(change_count)
                                  if change_count else "APPLY")
        self.count_text.Text = "{0} to update".format(change_count)

        if empty_message:
            self.set_status(empty_message)
            return

        message = "{0} circuit(s)".format(len(self._circuits))
        if len(self._circuits) != len(self._all_circuits):
            message += " of {0} pass the value filter".format(len(self._all_circuits))
        else:
            message += " in scope"
        if len(rows) != len(self._rows):
            message += ", {0} row(s) shown".format(len(rows))
        message += ", {0} will be updated.".format(change_count)
        if len(rows) > MAX_PREVIEW_ROWS:
            message += " Showing the first {0}.".format(MAX_PREVIEW_ROWS)
        self.set_status(message)

    # -- handlers ----------------------------------------------------------

    def on_scope_changed(self, sender, args):
        self.reload_circuits()

    def on_filter_target_changed(self, sender, args):
        if not self._ready:
            return
        self.reload_filter_parameters()

    def on_filter_param_search(self, sender, args):
        if not self._ready:
            return
        self.apply_filter_param_search(keep=self.flt_param.SelectedItem)

    def on_filter_param_changed(self, sender, args):
        if not self._ready:
            return
        self.reload_filter_values()

    def on_filter_changed(self, sender, args):
        self.apply_value_filter()

    def on_filter_value_picked(self, sender, args):
        if not self._ready:
            return
        value = self.flt_values.SelectedItem
        if value:
            self.flt_value.Text = value        # TextChanged re-applies the filter

    def on_filter_clear(self, sender, args):
        self._ready = False
        self.flt_param_filter.Text = ""
        self.flt_value.Text = ""
        self.flt_op.SelectedIndex = 0
        self._ready = True
        self.apply_filter_param_search(keep=None)

    def on_mode_changed(self, sender, args):
        if not self._ready:
            return
        self.rebuild_facets()

    def on_category_changed(self, sender, args):
        if not self._ready:
            return
        self.refresh_type_list()

    def on_type_changed(self, sender, args):
        if not self._ready:
            return
        # Only the listed items carry live checkbox state. Reading
        # _all_types here would write back stale values for every type the
        # search box or the category filter is currently hiding.
        source = self.type_list.ItemsSource
        if source is not None:
            for item in source:
                self._type_states[item.Key] = item.IsSelected
        self.reload_parameters()

    def on_type_filter_changed(self, sender, args):
        if not self._ready:
            return
        self.refresh_type_list()

    def on_cat_all_click(self, sender, args):
        self.set_all_categories(True)

    def on_cat_none_click(self, sender, args):
        self.set_all_categories(False)

    def set_all_categories(self, state):
        for item in self._all_categories:
            item.IsSelected = state
        # FacetItem has no change notification, so rebind to repaint.
        self._ready = False
        self.cat_list.ItemsSource = bindable(self._all_categories)
        self._ready = True
        self.refresh_type_list()

    def on_type_all_click(self, sender, args):
        self.set_visible_types(True)

    def on_type_none_click(self, sender, args):
        self.set_visible_types(False)

    def set_visible_types(self, state):
        """All / None act on the types currently listed, not the whole model."""
        source = self.type_list.ItemsSource
        if source is not None:
            for item in source:
                item.IsSelected = state
                self._type_states[item.Key] = state
        self.refresh_type_list()

    def on_param_changed(self, sender, args):
        if self._ready and self.selected_param:
            self._wanted_param = self.selected_param      # a user choice
        self.refresh_preview()

    def on_param_filter_changed(self, sender, args):
        if not self._ready:
            return
        self.apply_param_filter(keep=self._wanted_param or self.selected_param)

    def on_option_changed(self, sender, args):
        self.refresh_preview()

    def on_row_search_changed(self, sender, args):
        if not self._ready:
            return
        self.render_rows()

    def on_search_clear_click(self, sender, args):
        self.row_search.Text = ""

    def on_refresh_click(self, sender, args):
        self.reload_circuits()

    def on_row_double_click(self, sender, args):
        """Select the circuit (and the element the text was read from).

        FIX: this also called uidoc.ShowElements(), which for a circuit --
        an element with no graphics -- pops up Revit's "no good view found"
        dialog on top of this window. Selecting is enough.
        """
        row = self.preview_grid.SelectedItem
        if row is None:
            return
        try:
            ids = List[DB.ElementId]()
            ids.Add(row.circuit_id)
            if row.source_id is not None:
                ids.Add(row.source_id)
            uidoc.Selection.SetElementIds(ids)
            self.set_status("Circuit {0} is selected in Revit (visible when this "
                            "window closes).".format(row.Circuit))
        except Exception as error:
            logger.debug("Could not select element: %s", error)

    def on_apply_click(self, sender, args):
        self.result = [r for r in self._visible_rows if r.will_change]
        self.Close()

    def on_cancel_click(self, sender, args):
        self.result = None
        self.Close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def apply_rows(rows):
    """Write the new Load Names in one transaction.

    Returns (updated count, failures [(circuit, reason)], cancelled).
    An explicit Transaction is used rather than revit.Transaction so that a
    cancelled progress bar can roll the whole thing back. Committing a
    partial run on cancel is worse than doing nothing.
    """
    updated = 0
    failures = []
    cancelled = False
    transaction = DB.Transaction(doc, "Transfer Circuit Load Names")
    transaction.Start()
    try:
        with forms.ProgressBar(title="Updating Load Names ({value}/{max_value})",
                               cancellable=True) as progress:
            for index, row in enumerate(rows):
                if progress.cancelled:
                    cancelled = True
                    break
                progress.update_progress(index + 1, len(rows))

                circuit = doc.GetElement(row.circuit_id)
                if circuit is None:
                    failures.append((row.Circuit, "circuit no longer exists"))
                    continue
                parameter = load_name_param(circuit)
                if parameter is None or parameter.IsReadOnly:
                    failures.append((row.Circuit, "parameter not writable"))
                    continue
                try:
                    if parameter.Set(row.NewValue):
                        updated += 1
                    else:
                        failures.append((row.Circuit, "Set() returned false"))
                except Exception as error:
                    failures.append((row.Circuit, str(error)))

        if cancelled:
            transaction.RollBack()
            return 0, failures, True
        status = transaction.Commit()
        # Commit() does not raise when Revit rolls the transaction back
        # (for example a circuit owned by another user); it returns the status.
        if status != DB.TransactionStatus.Committed:
            return 0, failures + [("(all)", "Revit rolled the transaction back: "
                                            "{0}".format(status))], False
    except Exception:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
        raise
    return updated, failures, False


def main():
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Open a project document first. This tool does not run "
                    "in the family editor.", title=TITLE, exitscript=True)

    preselected = bool(uidoc.Selection.GetElementIds())
    window = TransferLoadNameWindow(script.get_bundle_file("ui.xaml"),
                                    preselected)
    window.ShowDialog()

    rows = window.result
    if not rows:
        script.exit()

    updated, failures, cancelled = apply_rows(rows)
    if cancelled:
        forms.alert("Cancelled. No Load Name was changed.", title=TITLE)
        return

    message = "Updated {0} of {1} circuit(s).".format(updated, len(rows))
    if not failures:
        forms.alert(message, title=TITLE)
        return

    # Only open the output window when there is something to read in it.
    forms.alert(message + "\n\n{0} could not be updated - see the list in the "
                "output window.".format(len(failures)), title=TITLE)
    output.print_md("## Transfer Load Name")
    output.print_md(message)
    # print_table renders one table. Calling print_md once per row does not:
    # each call is its own markdown block, so the rows never form a table.
    safetable.print_table(output, 
        table_data=[[circuit, reason] for circuit, reason in failures[:100]],
        title="Failures",
        columns=["Circuit", "Reason"],
    )


if __name__ == "__main__":
    main()

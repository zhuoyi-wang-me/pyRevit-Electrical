# -*- coding: utf-8 -*-
"""Revit API access for the Electrical Load Report button.

All Revit API calls live here; elr_report / elr_xlsx are pure Python.
Supports Revit 2020-2026 (pyRevit IronPython 2.7 / 3.4).
"""
import re

import clr  # noqa: F401  (needed for generic List[...] under IronPython)
from System.Collections.Generic import List

from Autodesk.Revit.DB import (BuiltInCategory, BuiltInParameter, Domain, ElementId,
                               ElementMulticategoryFilter, FilteredElementCollector,
                               StorageType, UnitUtils, ViewSchedule)
from Autodesk.Revit.DB.Electrical import ElectricalSystem  # was imported from the wrong namespace

try:
    from Autodesk.Revit.DB.Electrical import ElectricalSystemType
except ImportError:  # pragma: no cover
    ElectricalSystemType = None

try:
    text_type = unicode  # noqa: F821
except NameError:
    text_type = str

CONNECTOR_CLASSIFICATION = u'[Electrical Connector] Load Classification'
UNCLASSIFIED = u'(Unclassified)'
DIRECT_PANEL = u'Direct (no panel)'

LOAD_CATEGORIES = [  # (UI label, BuiltInCategory name)
    (u'Electrical Fixtures', 'OST_ElectricalFixtures'),
    (u'Mechanical Equipment', 'OST_MechanicalEquipment'),
    (u'Lighting Fixtures', 'OST_LightingFixtures'),
]
SCOPES = [(u'Entire model', 'model'), (u'Current selection', 'selection'), (u'Active view', 'view')]
PHASE_FILTERS = [(u'3-phase only', (3,)), (u'Single-phase only (1 & 2 pole)', (1, 2)), (u'All circuits', None)]

DIRECT_PARAM_CANDIDATES = [u'Elec apparent Power', u'Elec Apparent Power', u'Electrical Apparent Power',
                           u'Apparent Load', u'Apparent Power']
DEMAND_PARAM_NAMES = [u'Demand Load', u'Estimated Demand Load', u'Demand Apparent Power']
TRUE_PARAM_NAMES = [u'True Load', u'True Power', u'Connected Load']
APPARENT_PARAM_NAMES = [u'Apparent Load', u'Apparent Power', u'Connected Apparent Power']

_NUM_RE = re.compile(r'-?\d+(?:\.\d+)?')
_VA_PER_KVA_INTERNAL = 10763.9104167  # 1 kVA in Revit internal power units (kg*ft^2/s^3)


class ScopeError(Exception):
    """User-facing problem with the chosen scope."""


class Cancelled(Exception):
    """Raised when the user presses Cancel during export."""


# --------------------------------------------------------------------------- #
# Version-compat primitives
# --------------------------------------------------------------------------- #
def eid_int(eid):
    """ElementId -> int. IntegerValue was removed in Revit 2026 (use .Value)."""
    try:
        return int(eid.Value)
    except AttributeError:
        return int(eid.IntegerValue)


def bip(name):
    return getattr(BuiltInParameter, name, None)


def bic(name):
    return getattr(BuiltInCategory, name, None)


def bip_param(element, name):
    b = bip(name)
    if element is None or b is None:
        return None
    try:
        return element.get_Parameter(b)
    except Exception:
        return None


def internal_to_kilo(raw):
    """Revit internal power units -> kW / kVA (same factor for both)."""
    if raw is None:
        return None
    try:
        from Autodesk.Revit.DB import UnitTypeId  # 2021+
        return UnitUtils.ConvertFromInternalUnits(raw, UnitTypeId.KilovoltAmperes)
    except Exception:
        pass
    try:
        from Autodesk.Revit.DB import DisplayUnitType  # <= 2021
        return UnitUtils.ConvertFromInternalUnits(raw, DisplayUnitType.DUT_KILOVOLT_AMPERES)
    except Exception:
        # NOTE: the original fell back to raw / 1000, which is wrong by x10.76.
        return raw / _VA_PER_KVA_INTERNAL


def is_measurable(param):
    """True when the parameter carries units (e.g. Apparent Power), False for plain Number/Text."""
    try:
        definition = param.Definition
    except Exception:
        return True
    for getter in ('GetDataType', 'GetSpecTypeId'):  # 2022+, 2021
        fn = getattr(definition, getter, None)
        if fn is not None:
            try:
                spec = fn()
                # Revit counts "Number" as measurable (its unit is "General"),
                # so a plain Number load of 3000 (VA typed in) was converted as
                # internal power units: 0.279 kVA instead of 3 kVA.
                type_id = safe_text(getattr(spec, 'TypeId', spec)).lower()
                if type_id.startswith(u'autodesk.spec:spec.number'):
                    return False
                return bool(UnitUtils.IsMeasurableSpec(spec))
            except Exception:
                pass
    try:
        from Autodesk.Revit.DB import UnitType  # <= 2021
        return definition.UnitType != UnitType.UT_Number
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# Parameter / element helpers
# --------------------------------------------------------------------------- #
def safe_text(value):
    try:
        return text_type(value) if value is not None else u''
    except Exception:
        return u''


def element_name(element):
    try:
        return element.Name or u''
    except Exception:
        return u''


def type_of(doc, element):
    try:
        tid = element.GetTypeId()
        if tid is not None and tid != ElementId.InvalidElementId:
            return doc.GetElement(tid)
    except Exception:
        pass
    return None


def get_param(doc, element, name):
    """LookupParameter on the instance, then on its type (Type Mark etc.)."""
    if not name or element is None:
        return None
    found = None
    for target in (element, type_of(doc, element)):
        if target is None:
            continue
        try:
            p = target.LookupParameter(name)
        except Exception:
            p = None
        if p is not None:
            if p.HasValue:
                return p
            found = found or p
    return found


def first_param(doc, element, names):
    for name in names:
        p = get_param(doc, element, name)
        if p is not None and p.HasValue:
            return p
    return None


def param_text(doc, p):
    if p is None:
        return u''
    try:
        st = p.StorageType
        if st == StorageType.String:
            return (p.AsString() or u'').strip()
        if st == StorageType.ElementId:
            target = doc.GetElement(p.AsElementId())
            if target is not None:
                return element_name(target)
        value = p.AsValueString()
        if value:
            return value.strip()
        if not p.HasValue:
            return u''
        if st == StorageType.Integer:
            return safe_text(p.AsInteger())
        if st == StorageType.Double:
            return safe_text(round(p.AsDouble(), 6))
    except Exception:
        pass
    return u''


def param_number(p):
    """Raw numeric value (internal units for doubles) or None."""
    if p is None:
        return None
    try:
        if not p.HasValue:
            return None
        st = p.StorageType
        if st == StorageType.Double:
            return p.AsDouble()
        if st == StorageType.Integer:
            return float(p.AsInteger())
    except Exception:
        pass
    return None


def param_kilo(p, unitless_is_kilo=False):
    """Parameter power value in kW/kVA, whatever its storage/spec. None if empty."""
    if p is None:
        return None
    try:
        if not p.HasValue:
            return None
        st = p.StorageType
        if st == StorageType.String:
            text = (p.AsString() or u'').replace(u',', u'')
            match = _NUM_RE.search(text)
            if not match:
                return None
            value, low = float(match.group(0)), text.lower()
            if u'mva' in low or u'mw' in low:
                return value * 1000.0
            if u'kva' in low or u'kw' in low:
                return value
            return value if unitless_is_kilo else value / 1000.0
        if st == StorageType.Integer:
            value = float(p.AsInteger())
            return value if unitless_is_kilo else value / 1000.0
        if st == StorageType.Double:
            raw = p.AsDouble()
            if is_measurable(p):
                return internal_to_kilo(raw)
            # Plain "Number" parameter: unit is whatever the user typed in.
            return raw if unitless_is_kilo else raw / 1000.0
    except Exception:
        pass
    return None


def family_type_label(element):
    try:
        symbol = element.Symbol
        return u'{0} : {1}'.format(symbol.FamilyName, element_name(symbol))
    except Exception:
        return element_name(element)


def category_name(element):
    try:
        return element.Category.Name
    except Exception:
        return u''


def panel_name(doc, element):
    """Panel Name parameter. FamilyInstance.Name is the TYPE name, which the
    original script used - every panel of the same type collapsed together."""
    for p in (bip_param(element, 'RBS_ELEC_PANEL_NAME'), bip_param(element, 'ALL_MODEL_MARK')):
        text = param_text(doc, p)
        if text:
            return text
    return element_name(element) or u'Panel {0}'.format(eid_int(element.Id))


def panel_prefix(name):
    match = re.match(r'[A-Za-z]+', name or u'')
    return match.group(0).upper() if match else u'Other'


# --------------------------------------------------------------------------- #
# Connectors / circuits
# --------------------------------------------------------------------------- #
def electrical_connectors(element):
    try:
        manager = element.MEPModel.ConnectorManager
    except Exception:
        return []
    if manager is None:
        return []
    result = []
    for connector in manager.Connectors:
        try:
            if connector.Domain == Domain.DomainElectrical:
                result.append(connector)
        except Exception:
            pass
    return result


def connector_param(connector, bip_name):
    b = bip(bip_name)
    if connector is None or b is None:
        return None
    try:
        info = connector.GetMEPConnectorInfo()
    except Exception:
        return None
    if info is None:
        return None
    pid = ElementId(b)  # ElementId(BuiltInParameter) works in every version; ElementId(int) does not in 2026
    try:
        contains = getattr(info, 'ContainsConnectorParameter', None)
        if contains is not None and not contains(pid):
            return None
    except Exception:
        pass
    try:
        value = info.GetConnectorParameterValue(pid)
        return None if value is None else value.Value
    except Exception:
        return None


def connector_for_system(element, system):
    sid = eid_int(system.Id)
    connectors = electrical_connectors(element)
    for connector in connectors:
        try:
            owner = connector.MEPSystem
            if owner is not None and eid_int(owner.Id) == sid:
                return connector
        except Exception:
            pass
    return connectors[0] if len(connectors) == 1 else None


def classification_from_value(doc, value):
    if value is None:
        return u''
    try:
        if isinstance(value, ElementId):
            target = doc.GetElement(value)
            return element_name(target) if target is not None else u''
    except Exception:
        pass
    text = safe_text(value).strip()
    return u'' if text in (u'', u'-1', u'0') else text


def load_classification(doc, element, source, connector=None):
    if source and source != CONNECTOR_CLASSIFICATION:
        text = param_text(doc, get_param(doc, element, source))
        if text:
            return text
    ordered = ([connector] if connector is not None else []) + \
              [c for c in electrical_connectors(element) if c is not connector]
    for c in ordered:
        text = classification_from_value(doc, connector_param(c, 'RBS_ELEC_LOAD_CLASSIFICATION'))
        if text:
            return text
    return param_text(doc, bip_param(element, 'RBS_ELEC_LOAD_CLASSIFICATION')) or UNCLASSIFIED


def is_power_circuit(system):
    if ElectricalSystemType is None:
        return True
    try:
        return system.SystemType == ElectricalSystemType.PowerCircuit
    except Exception:
        return True


def system_poles(system):
    for prop in ('PolesNumber', 'NumberOfPoles'):
        try:
            value = int(getattr(system, prop))
            if value > 0:
                return value
        except Exception:
            pass
    value = param_number(bip_param(system, 'RBS_ELEC_NUMBER_OF_POLES'))
    return int(value) if value else None


def element_poles(doc, element):
    """Poles from the load itself (Direct mode). 0/empty means 'unknown',
    not 'single phase' - the original treated an empty value as 0 and stopped."""
    for target in (element, type_of(doc, element)):
        value = param_number(bip_param(target, 'RBS_ELEC_NUMBER_OF_POLES'))
        if value and value > 0:
            return int(round(value))
    best = None
    for connector in electrical_connectors(element):
        try:
            value = int(connector_param(connector, 'RBS_ELEC_NUMBER_OF_POLES'))
        except Exception:
            continue
        if value > 0:
            best = max(best or 0, value)
    return best


def circuit_number(system):
    for getter in (lambda s: s.CircuitNumber,
                   lambda s: param_text(None, bip_param(s, 'RBS_ELEC_CIRCUIT_NUMBER'))):
        try:
            value = getter(system)
            if value:
                return safe_text(value)
        except Exception:
            pass
    return element_name(system)


def element_systems(element):
    try:
        model = element.MEPModel
    except Exception:
        return []
    if model is None:
        return []
    source = None
    fn = getattr(model, 'GetElectricalSystems', None)
    if fn is not None:
        try:
            source = fn()
        except Exception:
            source = None
    if source is None:
        source = getattr(model, 'ElectricalSystems', None) or []
    seen, result = set(), []
    for system in source:
        try:
            sid = eid_int(system.Id)
        except Exception:
            continue
        if sid not in seen and is_power_circuit(system):
            seen.add(sid)
            result.append(system)
    return result


def base_equipment(system):
    try:
        return system.BaseEquipment
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Model snapshot (panels, circuits, parameter names)
# --------------------------------------------------------------------------- #
class PanelInfo(object):
    def __init__(self, element, name):
        self.element = element
        self.id = eid_int(element.Id)
        self.name = name
        self.prefix = panel_prefix(name)
        self.circuits = 0
        self.by_poles = {}

    def matching_circuits(self, poles_filter):
        if poles_filter is None:
            return self.circuits
        return sum(self.by_poles.get(p, 0) for p in poles_filter)

    def describe(self):
        three = self.by_poles.get(3, 0)
        return u'{0} circuit(s) · {1} three-phase'.format(self.circuits, three)


class ModelData(object):
    """One-off snapshot taken when the dialog opens."""

    def __init__(self, doc):
        self.doc = doc
        self.systems = [s for s in FilteredElementCollector(doc).OfClass(ElectricalSystem).ToElements()
                        if is_power_circuit(s)]
        self.panels = {}
        self.panel_systems = {}
        for eq in (FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_ElectricalEquipment)
                   .WhereElementIsNotElementType().ToElements()):
            self.panels[eid_int(eq.Id)] = PanelInfo(eq, panel_name(doc, eq))
        for system in self.systems:
            base = base_equipment(system)
            if base is None:
                continue
            pid = eid_int(base.Id)
            info = self.panels.get(pid)
            if info is None:
                info = self.panels[pid] = PanelInfo(base, panel_name(doc, base))
            info.circuits += 1
            poles = system_poles(system)
            info.by_poles[poles] = info.by_poles.get(poles, 0) + 1
            self.panel_systems.setdefault(pid, []).append(system)
        self._make_names_unique()

    def _make_names_unique(self):
        """Loads, sheets and the Panel Check are keyed by panel NAME, so two
        panels with the same name (or two unnamed panels, which fall back to
        their type name) were merged into one panel in the report. Duplicates
        get their element id appended."""
        count = {}
        for info in self.panels.values():
            key = info.name.lower()
            count[key] = count.get(key, 0) + 1
        for info in self.panels.values():
            if count[info.name.lower()] > 1:
                info.name = u'{0} [{1}]'.format(info.name, info.id)

    def sorted_panels(self):
        from elr_report import natural_key
        return sorted(self.panels.values(), key=lambda p: (natural_key(p.name), p.id))

    def panel_classification_loads(self, panel_ids, class_names):
        """What Revit itself reports on each panel, per load classification:
        the panel parameter "<classification name> Connected Apparent Power"
        - the same numbers as an Electrical Equipment schedule with those
        fields. Unlike a sum of the fixtures, this includes everything fed
        through the panel (other categories, downstream panels).

        Returns [(panel name, {classification name: kVA}, total connected kVA,
        total demand kVA)], or None when no panel has such a parameter."""
        rows, found = [], False
        for pid in panel_ids:
            info = self.panels.get(pid)
            if info is None:
                continue
            values = {}
            for name in class_names:
                try:
                    parameter = info.element.LookupParameter(u'{0} Connected Apparent Power'.format(name))
                except Exception:
                    parameter = None
                if parameter is None:
                    continue
                found = True
                value = param_kilo(parameter)
                if value:
                    values[name] = value
            rows.append((info.name, values,
                         param_kilo(bip_param(info.element, 'RBS_ELEC_PANEL_TOTALLOAD_PARAM')),
                         param_kilo(bip_param(info.element, 'RBS_ELEC_PANEL_TOTALESTLOAD_PARAM'))))
        return rows if found else None

    def panel_totals(self, panel_ids):
        """{panel name: (Revit total connected kVA, Revit total est. demand kVA)}"""
        result = {}
        for pid in panel_ids:
            info = self.panels.get(pid)
            if info is None:
                continue
            result[info.name] = (param_kilo(bip_param(info.element, 'RBS_ELEC_PANEL_TOTALLOAD_PARAM')),
                                 param_kilo(bip_param(info.element, 'RBS_ELEC_PANEL_TOTALESTLOAD_PARAM')))
        return result


def classification_abbreviations(doc):
    """{load classification name: its Abbreviation} for the classifications
    of the project - used as the column headings of the panel table."""
    result = {}
    try:
        from Autodesk.Revit.DB.Electrical import ElectricalLoadClassification
        for item in FilteredElementCollector(doc).OfClass(ElectricalLoadClassification).ToElements():
            try:
                name = (element_name(item) or u'').strip()
                code = (item.Abbreviation or u'').strip()
                if name and code:
                    result[name] = code
            except Exception:
                continue
    except Exception:
        pass
    return result


def classification_names(doc):
    """Names of every load classification defined in the project, so the
    panel table can list them all - also the ones no exported panel uses."""
    names = []
    try:
        from Autodesk.Revit.DB.Electrical import ElectricalLoadClassification
        for item in FilteredElementCollector(doc).OfClass(ElectricalLoadClassification).ToElements():
            try:
                name = (element_name(item) or u'').strip()
                if name and name not in names:
                    names.append(name)
            except Exception:
                continue
    except Exception:
        pass
    return names


def _category_filter(category_names):
    cats = List[BuiltInCategory]()
    for name in category_names:
        value = bic(name)
        if value is not None:
            cats.Add(value)
    if cats.Count == 0:
        raise ScopeError(u'Tick at least one load category.')
    return ElementMulticategoryFilter(cats)


def parameter_names(doc, category_names):
    """Instance + type parameter names, sampling one instance per type (fast)."""
    names, seen_types = set(), set()
    try:
        elements = (FilteredElementCollector(doc).WherePasses(_category_filter(category_names))
                    .WhereElementIsNotElementType().ToElements())
    except ScopeError:
        return []
    for element in elements:
        try:
            tid = eid_int(element.GetTypeId())
        except Exception:
            tid = None
        if tid in seen_types:
            continue
        seen_types.add(tid)
        for target in (element, type_of(doc, element)):
            if target is None:
                continue
            try:
                for p in target.Parameters:
                    if p is not None and p.Definition is not None and p.Definition.Name:
                        names.add(p.Definition.Name)
            except Exception:
                pass
    return sorted(names, key=lambda s: s.lower())


def collect_elements(doc, uidoc, model, category_names, scope):
    """Load elements for the chosen scope. Selected panels/circuits expand to their loads."""
    cat_filter = _category_filter(category_names)
    if scope == 'view':
        view = doc.ActiveView
        if view is None or isinstance(view, ViewSchedule) or view.IsTemplate:
            raise ScopeError(u'The active view cannot contain model elements. Open a plan, section or 3D view.')
        try:
            collector = FilteredElementCollector(doc, view.Id)
        except Exception as ex:
            raise ScopeError(u'Cannot collect from the active view: {0}'.format(ex))
    elif scope == 'selection':
        ids = _expand_selection(doc, uidoc, model)
        if not ids:
            raise ScopeError(u'Nothing is selected. Select loads, circuits or panels in Revit, then run again.')
        id_list = List[ElementId]()
        for eid in ids:
            id_list.Add(eid)
        collector = FilteredElementCollector(doc, id_list)
    else:
        collector = FilteredElementCollector(doc)
    return list(collector.WherePasses(cat_filter).WhereElementIsNotElementType().ToElements())


def _expand_selection(doc, uidoc, model):
    result, seen = [], set()

    def add(eid):
        key = eid_int(eid)
        if key not in seen:
            seen.add(key)
            result.append(eid)

    def add_system(system):
        try:
            for member in system.Elements:
                add(member.Id)
        except Exception:
            pass

    for eid in uidoc.Selection.GetElementIds():
        element = doc.GetElement(eid)
        if element is None:
            continue
        if isinstance(element, ElectricalSystem):
            add_system(element)
        elif eid_int(eid) in model.panel_systems:
            for system in model.panel_systems[eid_int(eid)]:
                add_system(system)
        else:
            add(eid)
    return result


# --------------------------------------------------------------------------- #
# Load extraction
# --------------------------------------------------------------------------- #
class CollectResult(object):
    def __init__(self):
        self.records = []
        self.warnings = []
        self.diagnostics = dict((k, 0) for k in (
            'elements_in_scope', 'records', 'not_circuited', 'no_panel', 'panel_not_selected',
            'phase_filtered', 'unknown_poles', 'circuit_share', 'no_own_load', 'demand_estimated',
            'zero_load', 'errors'))


class LoadCollector(object):
    """opts keys: mode, poles_filter, panel_ids, classification_source,
    default_identifier, rules [(text, param)], direct_param, unitless_is_kilo"""

    def __init__(self, doc, model, opts, progress=None, cancelled=None):
        self.doc = doc
        self.model = model
        self.opts = opts
        self.progress = progress
        self.cancelled = cancelled
        self._circuit_cache = {}

    def run(self, elements):
        result = CollectResult()
        result.diagnostics['elements_in_scope'] = total = len(elements)
        direct = self.opts['mode'] == 'direct'
        for index, element in enumerate(elements):
            if index % 20 == 0:
                if self.cancelled and self.cancelled():
                    raise Cancelled()
                if self.progress:
                    self.progress(index, total)
            try:
                if direct:
                    self._direct(element, result)
                else:
                    self._circuited(element, result)
            except Cancelled:
                raise
            except Exception as ex:
                result.diagnostics['errors'] += 1
                result.warnings.append(u'Element {0}: {1}'.format(eid_int(element.Id), ex))
        if self.progress:
            self.progress(total, total)
        result.diagnostics['records'] = len(result.records)
        return result

    # -- shared --------------------------------------------------------------
    def _identifier(self, element, classification, fallback):
        lower = (classification or u'').lower()
        name = self.opts.get('default_identifier')
        for text, param in self.opts.get('rules', []):
            if text and param and text.lower() in lower:
                name = param
                break
        value = param_text(self.doc, get_param(self.doc, element, name)) if name else u''
        return value or fallback

    def _record(self, element, panel, circuit, classification, connected, demand, apparent,
                source, demand_estimated, result):
        if not (apparent or connected):
            result.diagnostics['zero_load'] += 1
        if demand_estimated:
            result.diagnostics['demand_estimated'] += 1
        fallback = circuit or family_type_label(element) or safe_text(eid_int(element.Id))
        result.records.append({
            'panel': panel,
            'classification': classification,
            'circuit': circuit,
            'identifier': self._identifier(element, classification, fallback),
            'family_type': family_type_label(element),
            'category': category_name(element),
            'element_id': eid_int(element.Id),
            'connected_kw': connected,
            'demand_kw': demand,
            'apparent_kva': apparent,
            'load_source': source,
            'demand_estimated': demand_estimated,
        })

    def _demand(self, element, fallback):
        value = param_kilo(bip_param(element, 'RBS_ELEC_DEMANDFACTOR_DEMANDLOAD_PARAM'))
        if value is None:
            value = param_kilo(first_param(self.doc, element, DEMAND_PARAM_NAMES))
        return (fallback, True) if value is None else (value, False)

    # -- direct mode -----------------------------------------------------------
    def _direct(self, element, result):
        poles_filter = self.opts.get('poles_filter')
        if poles_filter:
            poles = element_poles(self.doc, element)
            if poles is None:
                result.diagnostics['unknown_poles'] += 1
                return
            if poles not in poles_filter:
                result.diagnostics['phase_filtered'] += 1
                return
        p = get_param(self.doc, element, self.opts.get('direct_param'))
        apparent = param_kilo(p, self.opts.get('unitless_is_kilo', False))
        if apparent is None:
            result.warnings.append(u'Element {0}: "{1}" is empty - exported as 0.'.format(
                eid_int(element.Id), self.opts.get('direct_param')))
            apparent = 0.0
        demand, estimated = self._demand(element, apparent)
        classification = load_classification(self.doc, element, self.opts.get('classification_source'))
        self._record(element, DIRECT_PANEL, u'', classification, None, demand, apparent,
                     u'parameter', estimated, result)

    # -- circuited mode --------------------------------------------------------
    def _circuit_info(self, system):
        sid = eid_int(system.Id)
        info = self._circuit_cache.get(sid)
        if info is None:
            apparent = param_kilo(bip_param(system, 'RBS_ELEC_APPARENT_LOAD'))
            true = param_kilo(bip_param(system, 'RBS_ELEC_TRUE_LOAD'))
            if apparent is None:
                try:
                    apparent = internal_to_kilo(float(system.ApparentLoad))
                except Exception:
                    apparent = None
            if true is None:
                try:
                    true = internal_to_kilo(float(system.TrueLoad))
                except Exception:
                    true = None
            # What is left of the circuit's load once the members that carry
            # their OWN load are taken off, and how many members carry none.
            # Sharing the whole circuit load over every member (the old way)
            # gave a socket with 0 W a third of the isolator's load on the
            # same circuit, and that load was then exported twice.
            members, own_total, without = 0, 0.0, 0
            try:
                for member in system.Elements:
                    members += 1
                    own, _source = self._own_apparent(member, system)
                    if own:
                        own_total += own
                    else:
                        without += 1
            except Exception:
                pass
            remaining = max(0.0, (apparent or 0.0) - own_total)
            if remaining <= max(0.001, 0.01 * (apparent or 0.0)):
                remaining = 0.0                     # rounding only: nothing left to share
            info = self._circuit_cache[sid] = (apparent, true, max(1, members), remaining, without,
                                               own_total)
        return info

    def _own_apparent(self, element, system):
        """(kVA, source) the element itself carries on this circuit - its
        connector, else an instance parameter - or (None, None)."""
        connector = connector_for_system(element, system)
        raw = connector_param(connector, 'RBS_ELEC_APPARENT_LOAD')
        try:
            if raw:
                return internal_to_kilo(float(raw)), u'connector'
        except Exception:
            pass
        value = param_kilo(bip_param(element, 'RBS_ELEC_APPARENT_LOAD'))
        if value is None:
            value = param_kilo(first_param(self.doc, element, APPARENT_PARAM_NAMES))
        if value:
            return value, u'instance parameter'
        return None, None

    def _circuited(self, element, result):
        systems = element_systems(element)
        if not systems:
            result.diagnostics['not_circuited'] += 1
            return
        poles_filter = self.opts.get('poles_filter')
        panel_ids = self.opts['panel_ids']
        for system in systems:
            base = base_equipment(system)
            if base is None:
                result.diagnostics['no_panel'] += 1
                continue
            pid = eid_int(base.Id)
            if pid not in panel_ids:
                result.diagnostics['panel_not_selected'] += 1
                continue
            if poles_filter:
                poles = system_poles(system)
                if poles is None:
                    result.diagnostics['unknown_poles'] += 1
                    continue
                if poles not in poles_filter:
                    result.diagnostics['phase_filtered'] += 1
                    continue
            self._circuit_record(element, system, pid, result)

    def _circuit_record(self, element, system, pid, result):
        connector = connector_for_system(element, system)
        circ_apparent, circ_true, members, remaining, without, own_total = self._circuit_info(system)

        # Apparent load: connector on THIS circuit > instance parameter.
        apparent, source = self._own_apparent(element, system)
        if not apparent:
            if own_total and not remaining:
                # Other elements on this circuit carry all of its load (e.g. the
                # isolator has the kVA and the socket has 0 W): this element has
                # nothing of its own, so it is NOT exported. Exporting a share
                # of the circuit here counted that load twice.
                result.diagnostics['no_own_load'] += 1
                return
            if remaining and without:
                # part of the circuit load belongs to no element in particular:
                # only that part is shared, and only among the elements without a value
                apparent, source = remaining / without, u'circuit share'
                result.diagnostics['circuit_share'] += 1
            else:
                apparent, source = 0.0, u'no load data'

        # True (connected) load: explicit value > apparent x power factor > circuit ratio.
        true = param_kilo(bip_param(element, 'RBS_ELEC_TRUE_LOAD'))
        if true is None:
            true = param_kilo(first_param(self.doc, element, TRUE_PARAM_NAMES))
        if true is None:
            pf = connector_param(connector, 'RBS_ELEC_POWER_FACTOR')
            try:
                pf = float(pf)
            except Exception:
                pf = None
            if pf and 0.0 < pf <= 1.0:
                true = apparent * pf
            elif circ_apparent and circ_true is not None:
                true = apparent * circ_true / circ_apparent
            else:
                true = apparent

        demand, estimated = self._demand(element, true)
        classification = load_classification(self.doc, element, self.opts.get('classification_source'), connector)
        self._record(element, self.model.panels[pid].name, circuit_number(system), classification,
                     true, demand, apparent, source, estimated, result)

# -*- coding: utf-8 -*-
"""
Electrical Calculator - read the loads of selected Revit elements.

Reading is READ-ONLY (the load-classification probe's transaction is always
rolled back). The only model change is write_ratings(): the isolator rating
written to the related fixtures, after the user confirms, as one Undo step.

Demand: each element's Load Classification is resolved and the project's
Demand Factor of that classification (rule + ranges) is read, so the calculator
applies the same demand factors as Revit's panel schedules.

Per element (FamilyInstance) the value is taken from, in order:
    distribution board / panel : its Total Connected Load
    other devices              : Apparent Load parameter (built-in, instance or type)
                                 -> value on the family's power connector
                                 -> the "Electrical Data" text, e.g. "230 V/1-1200 VA"
Voltage, number of poles and power factor are read the same way. Values are
converted from Revit's internal units to VA / V (autocircuit.revit_compat).

Must run in a valid Revit API context - the modeless window calls it through an
ExternalEvent (see window.py).
"""
from __future__ import division

import re

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
import Autodesk.Revit.DB as DB
from Autodesk.Revit.DB import FamilyInstance, ElementId, UnitUtils
from Autodesk.Revit.UI import IExternalEventHandler
from Autodesk.Revit.UI.Selection import ISelectionFilter

import logging

from autocircuit import revit_compat as rc
from autocircuit.collectors import find_param, is_panel

_LOG = logging.getLogger('ElectricalCalculator')

# Categories that normally carry an electrical load.
LOAD_CATEGORIES = ('OST_ElectricalFixtures', 'OST_ElectricalEquipment', 'OST_LightingFixtures',
                   'OST_LightingDevices', 'OST_MechanicalEquipment', 'OST_PlumbingFixtures',
                   'OST_SpecialityEquipment', 'OST_FireAlarmDevices', 'OST_CommunicationDevices',
                   'OST_DataDevices', 'OST_SecurityDevices', 'OST_NurseCallDevices', 'OST_TelephoneDevices')
_CAT_IDS = set(int(getattr(DB.BuiltInCategory, n)) for n in LOAD_CATEGORIES
               if getattr(DB.BuiltInCategory, n, None) is not None)

# User-defined parameter names tried after the built-ins (instance, then type).
LOAD_NAMES = ('Apparent Load', 'Electrical Load', 'Load (VA)')
VOLT_NAMES = ('Voltage',)
POLE_NAMES = ('Number of Poles', 'Poles')
PF_NAMES = ('Power Factor', 'PF')

# "230 V/1-1200 VA", "400 V/3-15.5 kVA" (built-in Electrical Data text)
_ELEC_DATA = re.compile(r'([\d.,]+)\s*V\s*/\s*(\d)\s*-\s*([\d.,]+)\s*(k?)VA', re.IGNORECASE)


def _bip(name):
    return getattr(DB.BuiltInParameter, name, None)


def _family_label(el):
    try:
        return u'{0} : {1}'.format(el.Symbol.Family.Name, rc.element_name(el.Symbol))
    except Exception:
        return rc.element_name(el)


def is_load_element(el):
    """FamilyInstance in a load category or with an electrical power connector."""
    if not isinstance(el, FamilyInstance) or el.Category is None:
        return False
    if rc.eid_int(el.Category.Id) in _CAT_IDS:
        return True
    return bool(rc.power_connectors(el))


def _electrical_data(el):
    p = el.get_Parameter(_bip('RBS_ELECTRICAL_DATA')) if _bip('RBS_ELECTRICAL_DATA') else None
    text = p.AsString() if p is not None and p.HasValue else None
    m = _ELEC_DATA.search(text or '')
    if not m:
        return None
    va = float(m.group(3).replace(',', '.')) * (1000.0 if m.group(4) else 1.0)
    return float(m.group(1).replace(',', '.')), int(m.group(2)), va


def _number(doc, el, names, bip_name, kind=None):
    value = rc.param_number(find_param(doc, el, names, (bip_name,)), kind)
    if not value:
        value = rc.connector_number(el, bip_name, kind)
    return value or None


def read_element(doc, el):
    """dict with id, name, category, va, volts, poles, pf, source - or None if not a load element."""
    if not is_load_element(el):
        return None
    item = {'id': rc.eid_int(el.Id), 'name': _family_label(el),
            'category': el.Category.Name, 'va': None, 'volts': None, 'poles': None,
            'pf': None, 'source': ''}
    try:
        panel = is_panel(el)
        if panel:
            bip = _bip('RBS_ELEC_PANEL_TOTALLOAD_PARAM')
            item['va'] = rc.param_number(el.get_Parameter(bip), 'power') if bip else None
            item['source'] = 'panel connected load'
            # the board already applies the demand factors of everything on it
            dbip = _bip('RBS_ELEC_PANEL_TOTALESTLOAD_PARAM')
            demand = rc.param_number(el.get_Parameter(dbip), 'power') if dbip else None
            item['demand_va'] = demand if demand and demand > 0 else (item['va'] or 0.0)
        if not item['va']:
            item['va'] = _number(doc, el, LOAD_NAMES, 'RBS_ELEC_APPARENT_LOAD', 'power')
            item['source'] = 'apparent load' if item['va'] else ''
        item['volts'] = _number(doc, el, VOLT_NAMES, 'RBS_ELEC_VOLTAGE', 'voltage')
        poles = _number(doc, el, POLE_NAMES, 'RBS_ELEC_NUMBER_OF_POLES')
        item['poles'] = int(poles) if poles else (3 if panel else None)
        pf = _number(doc, el, PF_NAMES, 'RBS_ELEC_POWER_FACTOR')
        if pf and pf > 1:                                  # entered as a percentage
            pf = pf / 100.0
        item['pf'] = pf

        data = _electrical_data(el)                        # last resort / fill gaps
        if data:
            volts, poles, va = data
            if not item['va'] and va:
                item['va'], item['source'] = va, 'electrical data'
            item['volts'] = item['volts'] or volts
            item['poles'] = item['poles'] or poles
        if not item['va']:
            item['source'] = 'no load found'
        item['poles'] = item['poles'] or 1
    except Exception as ex:                                # one bad family must not stop the rest
        item['source'] = u'error: {0}'.format(ex)
    return item


def read_elements(doc, elements):
    """(items, ignored_count) for a list of elements - with load classification."""
    items, pairs, ignored = [], [], 0
    for el in elements:
        item = read_element(doc, el) if el is not None else None
        if item is None:
            ignored += 1
        else:
            items.append(item)
            pairs.append((el, item))
    classify(doc, pairs)
    return items, ignored


# ------------------------------------------------------ load classification
class _Probe(object):
    """Stand-in for autocircuit's FixtureInfo - the resolver sets these two."""
    def __init__(self):
        self.classification = None
        self.classification_source = None


def classify(doc, pairs):
    """
    Set item['classification'] to the project's Load Classification name, as
    Revit assigns it when the element is circuited:
        already circuited -> read from its existing circuit
        not circuited     -> autocircuit's probe: a temporary circuit per family
                             type inside a transaction that is ALWAYS rolled back
    Panels are skipped (their own Total Demand Load is used).
    """
    if not pairs:
        return
    from autocircuit.collectors import ProjectClassificationResolver
    resolver = ProjectClassificationResolver(doc, _LOG)
    todo = []
    for el, item in pairs:
        item.setdefault('classification', None)
        if item.get('demand_va') is not None:              # panel
            continue
        try:
            systems = rc.element_power_systems(el)
            name = resolver._read(systems[0]) if systems else ''
        except Exception:
            name = ''
        if name:
            item['classification'] = name
        else:
            todo.append((el, item))
    if not todo:
        return
    probes = [(el, _Probe()) for el, _ in todo]
    try:
        resolver.resolve(probes)
    except Exception as ex:
        _LOG.warning('Load classification probe failed: %s', ex)
    for (_, item), (_, probe) in zip(todo, probes):
        item['classification'] = probe.classification or None


def demand_definitions(doc):
    """
    {classification name: {'name', 'rule', 'values': [(min, max, factor)], 'additional_va'}}
    for every Load Classification in the project that has a Demand Factor.
    Load ranges are converted from Revit internal units to VA.
    """
    from Autodesk.Revit.DB.Electrical import ElectricalLoadClassification
    result = {}
    for lc in DB.FilteredElementCollector(doc).OfClass(ElectricalLoadClassification):
        name = rc.element_name(lc)
        try:
            dfid = lc.DemandFactorId
            d = doc.GetElement(dfid) if dfid is not None and rc.eid_int(dfid) > 0 else None
            if d is None:
                continue
            rule = str(d.RuleType)
            by_load = 'Load' in rule
            values = []
            for v in d.GetValues():
                lo, hi = v.MinRange, v.MaxRange
                if by_load:
                    lo = rc.from_internal(lo, 'VoltAmperes')
                    hi = rc.from_internal(hi, 'VoltAmperes') if hi < 1e30 else 1e30
                values.append((lo, hi, v.Factor))
            additional = 0.0
            if getattr(d, 'IncludeAdditionalLoad', False):
                additional = rc.from_internal(d.AdditionalLoad, 'VoltAmperes')
            result[name] = {'name': rc.element_name(d), 'rule': rule, 'values': values,
                            'additional_va': additional}
        except Exception as ex:                            # unreadable -> treated as no demand factor
            _LOG.warning('Demand factor of "%s" not readable: %s', name, ex)
    return result


def read_selection(uidoc):
    doc = uidoc.Document
    return read_elements(doc, [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()])


# ------------------------------------------- isolator rating -> fixture
MM_TO_FT = 1.0 / 304.8


def load_epc():
    """Equipment Power Check's matching module (pushbutton folder, not lib)."""
    import os
    from eleccalc.transfer import load_module
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.normpath(os.path.join(here, '..', '..', 'Electrical.tab', 'Electrical.panel',
                                         '4 Panels.stack', 'Equipment Power Check.pushbutton', 'epc_match.py'))
    if not os.path.exists(path):
        raise IOError('Equipment Power Check matching not found: ' + path)
    return load_module('eleccalc_epc_match', path)


def _text_param(doc, el, name):
    """Instance value, then type value, of a text-like parameter."""
    if not name:
        return u''
    owners = [el]
    try:
        owners.append(doc.GetElement(el.GetTypeId()))
    except Exception:
        pass
    for owner in owners:
        p = owner.LookupParameter(name) if owner is not None else None
        if p is not None and p.HasValue:
            text = p.AsString() if p.StorageType == DB.StorageType.String else p.AsValueString()
            if (text or u'').strip():
                return text.strip()
    return u''


def _type_name(doc, el):
    try:
        return rc.element_name(doc.GetElement(el.GetTypeId()))
    except Exception:
        return u''


def _epc_item(epc, doc, el, tag_param, key):
    pt = rc.element_point(el)
    tag, tag_from = epc.tag_or_type(_text_param(doc, el, tag_param), _type_name(doc, el), True)
    return epc.Item(key, u'{0} [{1}]'.format(_family_label(el), key), u'host',
                    (pt.X, pt.Y, pt.Z) if pt is not None else None, tag=tag, element_id=key,
                    tag_from=tag_from)


def _has_instance_param(el, name):
    p = el.LookupParameter(name)
    return p is not None


def find_related_fixtures(doc, load_ids, opts):
    """
    {load id: [(fixture id, label, how, note)]} for the selected loads.
    opts: param, tag_param, max_plan_mm, max_height_mm, name_filter, ignore_tags
    Only Electrical Fixtures with the rating parameter as an INSTANCE parameter
    are candidates (a type parameter would change every isolator of that type).
    """
    from eleccalc import transfer
    param = opts['param']

    # 1. selected elements that ARE the isolator (fixture with the parameter):
    #    written directly - no search of the model at all
    result, others = {}, []
    elec_fixture = int(DB.BuiltInCategory.OST_ElectricalFixtures)
    for lid in load_ids:
        el = doc.GetElement(rc.to_eid(lid))
        if el is None:
            result[lid] = []
        elif (el.Category is not None and rc.eid_int(el.Category.Id) == elec_fixture
              and _has_instance_param(el, param)):
            result[lid] = [(lid, _family_label(el), transfer.HOW_SELF, u'')]
        else:
            others.append(el)
    if not others:
        return result

    # 2. selected equipment: search the fixtures that serve it (tag, then nearest)
    epc = load_epc()
    words = [w.strip().lower() for w in (opts.get('name_filter') or u'').split(u',') if w.strip()]
    fixtures = []
    for f in (DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_ElectricalFixtures)
              .WhereElementIsNotElementType()):
        if not isinstance(f, FamilyInstance) or not _has_instance_param(f, param):
            continue
        label = _family_label(f).lower()
        if words and not any(w in label for w in words):
            continue
        fixtures.append(_epc_item(epc, doc, f, opts['tag_param'], rc.eid_int(f.Id)))
    fixtures, _ = epc.drop_ignored(fixtures, opts.get('ignore_tags') or [])
    loads = [_epc_item(epc, doc, el, opts['tag_param'], rc.eid_int(el.Id)) for el in others]
    result.update(transfer.match(loads, fixtures, epc, True, True,
                                 opts['max_plan_mm'] * MM_TO_FT, opts['max_height_mm'] * MM_TO_FT))
    return result


_WRITABLE = (DB.StorageType.Double, DB.StorageType.Integer, DB.StorageType.String)


def _writable_names(el):
    names = set()
    for p in el.Parameters:
        try:
            if not p.IsReadOnly and p.StorageType in _WRITABLE:
                names.add(p.Definition.Name)
        except Exception:
            continue
    return names


def fixture_param_names(doc, load_ids):
    """
    Writable instance parameters (number / integer / text) the rating can go into,
    taken from the fixtures that will receive it:
        selected Electrical Fixtures (isolators)                -> their parameters
        selected equipment -> the model's Electrical Fixtures   -> one instance per type
    Sorted list of names.
    """
    elec_fixture = int(DB.BuiltInCategory.OST_ElectricalFixtures)
    names, need_model = set(), False
    for lid in load_ids:
        el = doc.GetElement(rc.to_eid(lid))
        if el is None or el.Category is None:
            continue
        if rc.eid_int(el.Category.Id) == elec_fixture:
            names |= _writable_names(el)
        else:
            need_model = True
    if need_model:
        seen_types = set()
        for f in (DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_ElectricalFixtures)
                  .WhereElementIsNotElementType()):
            tid = rc.eid_int(f.GetTypeId())
            if tid in seen_types:
                continue
            seen_types.add(tid)
            names |= _writable_names(f)
    return sorted(names, key=lambda n: n.lower())


def _is_current(param):
    try:
        spec = getattr(DB, 'SpecTypeId', None)
        return spec is not None and param.Definition.GetDataType().TypeId == spec.Current.TypeId
    except Exception:
        return False


def write_ratings(doc, writes, param_name, text_format=u'{0}A'):
    """
    Write {fixture id: {'rating', ...}} into the fixtures' rating parameter -
    ONE transaction, so the whole transfer is a single Undo.
    Number / Integer / Current parameters get the number (A), Text parameters
    get text_format (e.g. '40A'). Returns {fixture id: status text}.
    """
    from eleccalc import transfer
    status = {}
    t = DB.Transaction(doc, 'Electrical Calculator - isolator ratings')
    t.Start()
    try:
        for fid, w in sorted(writes.items()):
            el = doc.GetElement(rc.to_eid(fid))
            p = el.LookupParameter(param_name) if el is not None else None
            if p is None:
                status[fid] = u'parameter "{0}" not found'.format(param_name)
                continue
            if p.IsReadOnly:
                status[fid] = u'parameter is read-only'
                continue
            if rc.is_owned_by_other(doc, el.Id):
                status[fid] = u'owned by another user - not written'
                continue
            rating = w['rating']
            st = p.StorageType
            if st == DB.StorageType.Double:
                value = float(rating)
                if _is_current(p):
                    value = UnitUtils.ConvertToInternalUnits(value, DB.UnitTypeId.Amperes)
                same = p.HasValue and abs(p.AsDouble() - value) < 1e-6
            elif st == DB.StorageType.Integer:
                value = int(round(rating))
                same = p.HasValue and p.AsInteger() == value
            elif st == DB.StorageType.String:
                value = transfer.text_value(rating, text_format)
                same = (p.AsString() or u'') == value
            else:
                status[fid] = u'unsupported parameter type'
                continue
            if same:
                status[fid] = u'unchanged ({0} A)'.format(rating)
                continue
            status[fid] = u'written {0} A'.format(rating) if p.Set(value) else u'Revit refused the value'
        t.Commit()
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        raise
    return status


def pick_loads(uidoc):
    """Let the user pick load elements in the active view. None = cancelled (Esc)."""
    from Autodesk.Revit.UI.Selection import ObjectType
    from Autodesk.Revit.Exceptions import OperationCanceledException
    doc = uidoc.Document
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.Element, LoadElementFilter(),
                                           'Pick electrical fixtures / equipment - click Finish when done')
    except OperationCanceledException:
        return None
    return [doc.GetElement(r) for r in refs]


def select_ids(uidoc, ids):
    from System.Collections.Generic import List
    uidoc.Selection.SetElementIds(List[ElementId]([rc.to_eid(i) for i in ids]))


class LoadElementFilter(ISelectionFilter):
    """Only electrical loads can be picked."""

    def AllowElement(self, el):
        try:
            return is_load_element(el)
        except Exception:
            return False

    def AllowReference(self, ref, point):
        return False


class ApiCall(IExternalEventHandler):
    """Runs one queued callable(uiapp) inside Revit's API context."""

    def __init__(self):
        self.action = None
        self.on_error = None

    def Execute(self, uiapp):
        action, self.action = self.action, None
        if action is None:
            return
        try:
            action(uiapp)
        except Exception as ex:
            if self.on_error is not None:
                self.on_error(ex)

    def GetName(self):
        return 'Electrical Calculator - read loads'

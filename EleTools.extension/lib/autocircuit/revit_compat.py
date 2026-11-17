# -*- coding: utf-8 -*-
"""
Revit API compatibility layer (Revit 2019 - 2026).

Every call whose signature changed across versions goes through here so the rest
of the tool never branches on version:

    ElementId.IntegerValue (<=2023)  -> ElementId.Value (2024+; IntegerValue removed 2026)
    ElementId(int)                   -> ElementId(Int64) (2024+)
    DisplayUnitType (<=2020)         -> UnitTypeId / ForgeTypeId (2021+)
    Definition.ParameterType (<=2021)-> Definition.GetDataType() (2022+)
    MEPModel.ElectricalSystems       -> GetElectricalSystems() (2021+)
    ElectricalEquipment.AssignedElectricalSystems -> GetAssignedElectricalSystems() (2021+)
    Document.Create.NewElectricalSystem -> ElectricalSystem.Create() (2019+)
"""
import re

import clr
clr.AddReference('RevitAPI')
import System
import Autodesk.Revit.DB as DB
from Autodesk.Revit.DB import ElementId, UnitUtils, Domain, StorageType
from Autodesk.Revit.DB.Electrical import ElectricalSystem, ElectricalSystemType
from System.Collections.Generic import List


# ------------------------------------------------------------------ element ids
def eid_int(eid):
    if eid is None:
        return -1
    try:
        return int(eid.Value)
    except AttributeError:
        return int(eid.IntegerValue)


def to_eid(value):
    try:
        return ElementId(System.Int64(value))
    except Exception:
        return ElementId(int(value))


def element_name(el):
    if el is None:
        return ''
    try:
        return el.Name or ''
    except Exception:
        try:
            return DB.Element.Name.GetValue(el) or ''
        except Exception:
            return ''


# ------------------------------------------------------------------------ units
_DUT = {'VoltAmperes': 'DUT_VOLT_AMPERES', 'Volts': 'DUT_VOLTS', 'Watts': 'DUT_WATTS'}


def from_internal(value, unit_name):
    unit_type_id = getattr(DB, 'UnitTypeId', None)
    if unit_type_id is not None and hasattr(unit_type_id, unit_name):
        return UnitUtils.ConvertFromInternalUnits(value, getattr(unit_type_id, unit_name))
    return UnitUtils.ConvertFromInternalUnits(value, getattr(DB.DisplayUnitType, _DUT[unit_name]))


_SPECS = {
    'power': (('ApparentPower', 'ElectricalPower', 'Wattage', 'HvacPower'),
              ('ElectricalApparentPower', 'ElectricalPower', 'ElectricalWattage', 'HVACPower')),
    'voltage': (('ElectricalPotential',), ('ElectricalPotential',)),
}
_UNIT_FOR = {'power': 'VoltAmperes', 'voltage': 'Volts'}


def _param_is(param, kind):
    specs, legacy = _SPECS[kind]
    definition = param.Definition
    get_dt = getattr(definition, 'GetDataType', None)
    if get_dt is not None:
        try:
            dt = get_dt().TypeId
            spec_cls = getattr(DB, 'SpecTypeId', None)
            for name in specs:
                sid = getattr(spec_cls, name, None) if spec_cls is not None else None
                if sid is not None and sid.TypeId == dt:
                    return True
            return False
        except Exception:
            pass
    try:
        return str(definition.ParameterType) in legacy
    except Exception:
        return False


_NUM = re.compile(r'([-+]?\d+(?:[.,]\d+)?)\s*([kKmM]?)')


def parse_number(text):
    m = _NUM.search(text or '')
    if not m:
        return None
    v = float(m.group(1).replace(',', '.'))
    return v * 1000.0 if m.group(2) in ('k', 'K') else v


def param_number(param, kind=None):
    """Numeric value of a parameter. kind='power' -> VA, 'voltage' -> V, None -> raw."""
    if param is None or not param.HasValue:
        return None
    st = param.StorageType
    if st == StorageType.Double:
        v = param.AsDouble()
        if kind and _param_is(param, kind):
            return from_internal(v, _UNIT_FOR[kind])
        return v
    if st == StorageType.Integer:
        return float(param.AsInteger())
    if st == StorageType.String:
        return parse_number(param.AsString())
    return None


# ----------------------------------------------------------- electrical systems
def _items(collection):
    if collection is None:
        return []
    return [x for x in collection]


def element_power_systems(fi):
    mep = getattr(fi, 'MEPModel', None)
    if mep is None:
        return []
    try:
        getter = getattr(mep, 'GetElectricalSystems', None)
        systems = getter() if getter is not None else mep.ElectricalSystems
    except Exception:
        return []
    return [s for s in _items(systems) if s.SystemType == ElectricalSystemType.PowerCircuit]


def panel_assigned_systems(panel):
    mep = getattr(panel, 'MEPModel', None)
    if mep is None:
        return []
    try:
        getter = getattr(mep, 'GetAssignedElectricalSystems', None)
        return _items(getter() if getter is not None else mep.AssignedElectricalSystems)
    except Exception:
        return []


def power_connectors(fi):
    mep = getattr(fi, 'MEPModel', None)
    cm = getattr(mep, 'ConnectorManager', None) if mep is not None else None
    if cm is None:
        return []
    out = []
    for c in cm.Connectors:
        try:
            if c.Domain == Domain.DomainElectrical and 'Power' in str(c.ElectricalSystemType):
                out.append(c)
        except Exception:
            continue
    return out


_CONNECTOR_UNITS = {'power': 'VoltAmperes', 'voltage': 'Volts'}


def connector_number(fi, bip_name, kind=None):
    """Value set on the element's own electrical (power) connector - Apparent
    Load / Voltage / Number of Poles - for families that do NOT expose it as an
    instance or type parameter. Without this such devices were read as 0 VA,
    1 pole, 0 V: the circuit VA limit was ignored for them and a 3-phase load
    could be packed with single-phase ones.
    kind='power' -> VA, 'voltage' -> V, None -> raw (poles). None when not set."""
    bip = getattr(DB.BuiltInParameter, bip_name, None)
    if bip is None:
        return None
    pid = ElementId(bip)
    for connector in power_connectors(fi):
        try:
            info = connector.GetMEPConnectorInfo()
            value = info.GetConnectorParameterValue(pid) if info is not None else None
            raw = getattr(value, 'Value', None) if value is not None else None
        except Exception:
            continue
        if raw is None:
            continue
        try:
            number = float(raw)
        except Exception:
            continue
        if kind in _CONNECTOR_UNITS:
            number = from_internal(number, _CONNECTOR_UNITS[kind])
        if number:
            return number
    return None


def create_power_system(doc, element_ids):
    ids = List[ElementId](element_ids)
    if hasattr(ElectricalSystem, 'Create'):
        try:
            return ElectricalSystem.Create(doc, ids, ElectricalSystemType.PowerCircuit)
        except TypeError:   # overload missing in very old builds
            pass
    return doc.Create.NewElectricalSystem(ids, ElectricalSystemType.PowerCircuit)


# --------------------------------------------------------------------- location
def element_point(el):
    loc = el.Location
    if isinstance(loc, DB.LocationPoint):
        return loc.Point
    if isinstance(loc, DB.LocationCurve):
        return loc.Curve.Evaluate(0.5, True)
    bb = el.get_BoundingBox(None)
    if bb is not None:
        return bb.Min.Add(bb.Max).Multiply(0.5)
    return None


def element_level_id(el):
    lid = getattr(el, 'LevelId', None)
    if lid is not None and eid_int(lid) > 0:
        return lid
    for name in ('FAMILY_LEVEL_PARAM', 'INSTANCE_REFERENCE_LEVEL_PARAM', 'SCHEDULE_LEVEL_PARAM'):
        bip = getattr(DB.BuiltInParameter, name, None)
        p = el.get_Parameter(bip) if bip is not None else None
        if p is not None and p.HasValue and p.StorageType == StorageType.ElementId:
            lid = p.AsElementId()
            if eid_int(lid) > 0:
                return lid
    return None


def is_owned_by_other(doc, element_id):
    if not doc.IsWorkshared:
        return False
    try:
        status = DB.WorksharingUtils.GetCheckoutStatus(doc, element_id)
        return status == DB.CheckoutStatus.OwnedByOtherUser
    except Exception:
        return False

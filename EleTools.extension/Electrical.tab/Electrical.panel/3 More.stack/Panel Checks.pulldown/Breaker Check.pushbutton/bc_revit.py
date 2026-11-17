# -*- coding: utf-8 -*-
"""Breaker Check - reading circuits from the model and writing new breaker ratings.

Reading never changes the model. write() is the only function that does:
one transaction, only the Rating of the circuits the user ticked.
"""
from Autodesk.Revit import DB
from Autodesk.Revit.DB import Electrical as DBE

import bc_check

WRITTEN, FAILED, READ_ONLY = 'UPDATED', 'FAILED', 'READ-ONLY'


def panel_name(element):
    param = element.get_Parameter(DB.BuiltInParameter.RBS_ELEC_PANEL_NAME)
    return (param.AsString() if param is not None else None) or element.Name


def circuits_of(element):
    try:
        systems = element.MEPModel.GetAssignedElectricalSystems()
        return list(systems) if systems is not None else []
    except Exception:
        return []


def _is_load_circuit(circuit):
    return circuit.SystemType == DBE.ElectricalSystemType.PowerCircuit and circuit.CircuitType == DBE.CircuitType.Circuit


def all_panels(doc):
    """{name: element} of the electrical equipment that feeds at least one circuit."""
    found = {}
    collector = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_ElectricalEquipment).WhereElementIsNotElementType()
    for element in collector:
        if circuits_of(element):
            found[panel_name(element)] = element
    return found


def read(panels):
    """One bc_check.Line for each power circuit of the panels (spares and spaces are skipped). Not yet checked."""
    lines = []
    for panel in panels:
        name = panel_name(panel)
        for circuit in circuits_of(panel):
            try:
                if not _is_load_circuit(circuit):
                    continue
                # Revit keeps loads and voltages in its own internal units: convert to W, V and A
                load = DB.UnitUtils.ConvertFromInternalUnits(circuit.TrueLoad, DB.UnitTypeId.Watts)
                voltage = DB.UnitUtils.ConvertFromInternalUnits(circuit.Voltage, DB.UnitTypeId.Volts)
                try:
                    revit_current = DB.UnitUtils.ConvertFromInternalUnits(circuit.ApparentCurrent, DB.UnitTypeId.Amperes)
                except Exception:
                    revit_current = None
                lines.append(bc_check.Line(name, circuit.CircuitNumber or u'', circuit.LoadName or u'', int(circuit.PolesNumber),
                                           load, voltage, float(circuit.Rating), revit_current, circuit.Id))
                try:                                           # the load type of the circuit, for the load-type rules
                    lines[-1].classification = (circuit.LoadClassifications or u'').strip()
                except Exception:
                    pass
            except Exception as error:
                line = bc_check.Line(name, u'?', u'circuit id {0}'.format(circuit.Id), 1, 0.0, None, 0.0, None, circuit.Id)
                line.unreadable = u'{0}'.format(error)
                lines.append(line)
    return lines


def model_ratings(doc):
    """The breaker ratings that are already used on power circuits in this model, smallest first."""
    found = set()
    for circuit in DB.FilteredElementCollector(doc).OfClass(DBE.ElectricalSystem):
        try:
            if _is_load_circuit(circuit) and circuit.Rating > 0:
                found.add(float(circuit.Rating))
        except Exception:
            pass
    return sorted(found)


def write(doc, changes):
    """changes = [(line, new rating in A)]. Sets the Rating of each circuit; line.outcome says what happened.
    Returns {outcome: count}."""
    if not changes:
        return {}
    transaction = DB.Transaction(doc, 'Breaker Check: update breaker ratings')
    transaction.Start()
    try:
        for line, rating in changes:
            try:
                circuit = doc.GetElement(line.element_id)
                param = circuit.get_Parameter(DB.BuiltInParameter.RBS_ELEC_CIRCUIT_RATING_PARAM)
                if param is None or param.IsReadOnly:
                    line.outcome = READ_ONLY
                    continue
                done = param.Set(DB.UnitUtils.ConvertToInternalUnits(float(rating), DB.UnitTypeId.Amperes))
                line.outcome = WRITTEN if done else FAILED + u': Revit refused the value'
            except Exception as error:
                line.outcome = FAILED + u': {0}'.format(error)
        status = transaction.Commit()
        if status != DB.TransactionStatus.Committed:
            for line, _ in changes:
                line.outcome = FAILED + u': Revit did not commit the change ({0})'.format(status)
    except Exception as error:
        try:
            if transaction.HasStarted() and not transaction.HasEnded():
                transaction.RollBack()
        except Exception:
            pass
        for line, _ in changes:
            line.outcome = FAILED + u': {0}'.format(error)
    counts = {}
    for line, _ in changes:
        key = line.outcome.split(u':')[0]
        counts[key] = counts.get(key, 0) + 1
    return counts

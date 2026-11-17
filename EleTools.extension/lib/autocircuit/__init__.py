# -*- coding: utf-8 -*-
"""
ElecAutoCircuit - automated electrical circuiting for Autodesk Revit (pyRevit).

Package layout
--------------
Pure Python (no Revit API - unit-testable in plain CPython, see /tests):
    config          settings JSON load / merge / validate / save
    logger          rotating file log in %APPDATA%/ElecAutoCircuit/logs
    models          FixtureInfo, PanelInfo, RoomNode, PlannedCircuit, CircuitPlan
    geometry        point-in-polygon, region index, spatial ordering
    planner         classification -> room buckets -> packing -> room merging
    panel_selector  region / type / spare-pole / utilisation based panel choice
    reporting       report tables, CSV and native .xlsx export

Revit-dependent:
    revit_compat    version shims (2019 - 2026)
    collectors      read-only model extraction (fixtures, rooms, regions, panels)
    circuit_builder ElectricalSystem creation inside TransactionGroup / Transactions
    engine          orchestration used by the UI
    ui              pyRevit WPF window
"""
__version__ = '1.0.0'

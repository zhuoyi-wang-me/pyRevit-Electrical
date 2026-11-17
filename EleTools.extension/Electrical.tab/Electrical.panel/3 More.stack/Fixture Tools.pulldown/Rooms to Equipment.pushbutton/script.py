# -*- coding: utf-8 -*-
"""Rooms to Equipment - pyRevit tool.

Transfers Room / MEP Space information from the elements that sit in the rooms
to the equipment that serves them. One dedicated window per MEP system
(see PROFILES):

    Air Systems         Air Terminals       -> Mechanical Equipment   (ducts)
    Plumbing Systems    Plumbing Fixtures   -> Plumbing / Mech. Equip. (pipes)
    Fire Protection     Sprinklers          -> Fire Protection Equip.  (pipes)
    Electrical Systems  Fixtures / Devices  -> Panels                  (circuits)

Ducts and pipes are followed through the *physical* network (ConnectorManager
-> Connector.AllRefs); electrical uses circuits (ElectricalSystem.BaseEquipment).
The workflow below is written for air systems; the other systems work the same.

Workflow
    1. Collect every Air Terminal and Mechanical Equipment in the host model.
    2. Resolve the Room / Space of each terminal: host MEP Space -> host Room
       -> Room in a loaded linked model (point transformed into link space).
    3. Trace the duct network once (read-only, cached per network component)
       and pick the serving equipment by connector flow direction, with an
       optional upstream cascade (terminal -> VAV/FCU -> AHU).
    4. Open the dialog (ui.xaml):
         - Step 1: check which Mechanical Equipment to update (search, filter,
           Revit selection, pick in model) with a live preview of what each
           equipment serves.
         - Step 2: map each result (room numbers, names, area, airflow ...) to
           any writable instance parameter found on the equipment.
         - Step 3: options (cascade, units, separator, dry run, CSV).
       Mapping and options are remembered between sessions.
    5. Write the mapped parameters of the checked equipment in ONE transaction
       (skipped in report-only mode), then print the report / export CSV.

Engines : IronPython 2.7 (recommended for the WPF dialog) and CPython 3
Revit   : 2021 - 2026
"""

import os
import re
import json
import time
import codecs
from collections import OrderedDict, deque

from pyrevit import revit, DB, forms, script
import safetable  # crash-safe output tables (lib/safetable.py)

# DuctSystemType lives in the Autodesk.Revit.DB.Mechanical namespace,
# not in Autodesk.Revit.DB, so it must be imported explicitly.
try:
    from Autodesk.Revit.DB.Mechanical import DuctSystemType
except ImportError:
    DuctSystemType = DB.Mechanical.DuctSystemType
try:
    from Autodesk.Revit.DB.Plumbing import PipeSystemType
except Exception:
    PipeSystemType = None
try:
    from Autodesk.Revit.DB.Electrical import ElectricalSystemType
except Exception:
    ElectricalSystemType = None


# =============================================================================
# SYSTEM PROFILES - one dedicated window per MEP system
# =============================================================================
# The tool always does the same job: find the Room / Space of every "terminal"
# (the element that sits in the room), find the equipment that serves it, and
# write the served rooms to that equipment. A profile says what "terminal",
# "equipment" and "connected" mean for one MEP system.
#
#   domain            'hvac' / 'piping'  -> physical connector trace
#                     'electrical'       -> circuits (element -> panel -> panel)
#   terminal_cats     categories of the elements that sit in the rooms
#   connected_only    terminal categories only used when the element is really
#                     connected (e.g. Mechanical Equipment on a circuit)
#   equipment_cats    categories of the serving equipment (written to)
#   classes           the three class slots (internal keys Supply / Return /
#                     Exhaust) and what they are called in this system
#   class_map         system type enum member -> class slot
#   bi_dir            flow direction assumed for a bidirectional connector
#   flow_fields       slot -> (label, default parameter): totals offered in
#                     the mapping step and as columns in the equipment list
#   units             display unit -> factor from Revit internal units
#   spec / legacy_spec / flow_type   typed parameter that receives the total
# Add or edit a profile here; the picker and the window follow automatically.

AIR_UNITS = OrderedDict([('L/s', 28.316846592), ('m3/h', 101.9406477312), ('CFM', 60.0)])
PIPE_UNITS = OrderedDict([('L/s', 28.316846592), ('L/min', 1699.01079552),
                          ('m3/h', 101.9406477312), ('GPM', 448.831168831)])
POWER_UNITS = OrderedDict([('VA', 0.09290304), ('kVA', 0.00009290304)])

PROFILES = OrderedDict([
    ('air', {
        'title': 'Air Systems', 'header': '#1F3A5F', 'accent': '#0B6FB8',
        'domain': 'hvac',
        'terminal_cats': ['OST_DuctTerminal'], 'connected_only': [],
        'equipment_cats': ['OST_MechanicalEquipment'],
        'term': 'Air Terminal', 'terms': 'Air Terminals', 'terms_short': 'Terminals', 'abbr': 'AT',
        'equip': 'Mechanical Equipment', 'net': 'duct', 'qty': 'Airflow',
        'cascade': u'Also credit upstream equipment (terminal → VAV → AHU)',
        'classes': OrderedDict([('Supply', 'Supply'), ('Return', 'Return'), ('Exhaust', 'Exhaust')]),
        'class_map': [('SupplyAir', 'Supply'), ('ReturnAir', 'Return'),
                      ('ExhaustAir', 'Exhaust'), ('OtherAir', 'Other')],
        'class_words': [('supply', 'Supply'), ('return', 'Return'), ('exhaust', 'Exhaust')],
        'bi_dir': {'Supply': 'In', 'Return': 'Out', 'Exhaust': 'Out'},
        'flow_fields': OrderedDict([('Supply', ('Total Supply Airflow', 'Total Supply Airflow')),
                                    ('Return', ('Total Return Airflow', 'Total Return Airflow')),
                                    ('Exhaust', ('Total Exhaust Airflow', ''))]),
        'flow_bip': 'RBS_DUCT_FLOW_PARAM',
        'units': AIR_UNITS, 'unit': 'L/s',
        'spec': 'AirFlow', 'legacy_spec': 'HVACAirflow', 'flow_type': 'Air Flow',
    }),
    ('plumbing', {
        'title': 'Plumbing Systems', 'header': '#14534A', 'accent': '#0E7C6B',
        'domain': 'piping',
        'terminal_cats': ['OST_PlumbingFixtures'], 'connected_only': [],
        'equipment_cats': ['OST_PlumbingEquipment', 'OST_MechanicalEquipment'],
        'term': 'Plumbing Fixture', 'terms': 'Plumbing Fixtures', 'terms_short': 'Fixtures', 'abbr': 'PF',
        'equip': 'Plumbing / Mechanical Equipment', 'net': 'pipe', 'qty': 'Flow',
        'cascade': u'Also credit upstream equipment (fixture → heater / booster → tank / pump)',
        'classes': OrderedDict([('Supply', 'Cold Water'), ('Return', 'Hot Water'), ('Exhaust', 'Sanitary')]),
        'class_map': [('DomesticColdWater', 'Supply'), ('DomesticHotWater', 'Return'),
                      ('Sanitary', 'Exhaust')],
        'class_words': [('cold', 'Supply'), ('hot', 'Return'), ('sanitary', 'Exhaust')],
        'bi_dir': {'Supply': 'In', 'Return': 'In', 'Exhaust': 'Out'},
        'flow_fields': OrderedDict([('Supply', ('Total Cold Water Flow', '')),
                                    ('Return', ('Total Hot Water Flow', '')),
                                    ('Exhaust', ('Total Sanitary Flow', ''))]),
        'flow_bip': 'RBS_PIPE_FLOW_PARAM',
        'units': PIPE_UNITS, 'unit': 'L/s',
        'spec': 'Flow', 'legacy_spec': 'PipingFlow', 'flow_type': 'Flow',
    }),
    ('fire', {
        'title': 'Fire Protection', 'header': '#7A1F1A', 'accent': '#B3261E',
        'domain': 'piping',
        'terminal_cats': ['OST_Sprinklers'], 'connected_only': [],
        'equipment_cats': ['OST_MechanicalEquipment', 'OST_PlumbingEquipment'],
        'term': 'Sprinkler', 'terms': 'Sprinklers', 'terms_short': 'Sprinklers', 'abbr': 'SPR',
        'equip': 'Fire Protection Equipment', 'net': 'pipe', 'qty': 'Flow',
        'cascade': u'Also credit upstream equipment (sprinkler → valve set → fire pump)',
        'classes': OrderedDict([('Supply', 'Fire Protection')]),
        'class_map': [('FireProtectWet', 'Supply'), ('FireProtectDry', 'Supply'),
                      ('FireProtectPreaction', 'Supply'), ('FireProtectOther', 'Supply')],
        'class_words': [('fire', 'Supply')],
        'bi_dir': {'Supply': 'In'},
        'flow_fields': OrderedDict([('Supply', ('Total Sprinkler Flow', ''))]),
        'flow_bip': 'RBS_PIPE_FLOW_PARAM',
        'units': PIPE_UNITS, 'unit': 'L/min',
        'spec': 'Flow', 'legacy_spec': 'PipingFlow', 'flow_type': 'Flow',
    }),
    ('electrical', {
        'title': 'Electrical Systems', 'header': '#5B4310', 'accent': '#9A6A00',
        'domain': 'electrical',
        'terminal_cats': ['OST_ElectricalFixtures', 'OST_LightingFixtures', 'OST_LightingDevices',
                          'OST_DataDevices', 'OST_CommunicationDevices', 'OST_FireAlarmDevices',
                          'OST_SecurityDevices', 'OST_NurseCallDevices', 'OST_TelephoneDevices',
                          'OST_MechanicalEquipment'],
        'connected_only': ['OST_MechanicalEquipment'],
        'equipment_cats': ['OST_ElectricalEquipment'],
        'term': 'Device', 'terms': 'Fixtures / Devices', 'terms_short': 'Devices', 'abbr': 'dev',
        'equip': 'Electrical Equipment (panels)', 'net': 'electrical', 'qty': 'Load',
        'cascade': u'Also credit upstream panels (device → panel → main board / transformer)',
        'classes': OrderedDict([('Supply', 'Power'), ('Return', 'Data / Comms'),
                                ('Exhaust', 'Fire Alarm / Security')]),
        'class_map': [('PowerCircuit', 'Supply'), ('PowerBalanced', 'Supply'), ('PowerUnBalanced', 'Supply'),
                      ('Data', 'Return'), ('Telephone', 'Return'), ('Communication', 'Return'),
                      ('FireAlarm', 'Exhaust'), ('Security', 'Exhaust'), ('NurseCall', 'Exhaust'),
                      ('Controls', 'Exhaust')],
        'class_words': [],
        'bi_dir': {},
        'flow_fields': OrderedDict([('Supply', ('Total Connected Load', ''))]),
        'flow_bip': 'RBS_ELEC_APPARENT_LOAD',
        'units': POWER_UNITS, 'unit': 'VA',
        'spec': 'ApparentPower', 'legacy_spec': 'ElectricalApparentPower', 'flow_type': 'Apparent Power',
    }),
])

# field key of each class slot (kept from the first version so saved air
# system mappings still load)
FLOW_FIELD_KEYS = OrderedDict([('Supply', 'supply_flow'), ('Return', 'return_flow'),
                               ('Exhaust', 'exhaust_flow')])

# which parameter types can receive which kind of value ('airflow' is the
# flow / load total of the active profile; its typed entry is set per profile)
COMPATIBLE = {
    'text': ('Text',),
    'area': ('Area', 'Number', 'Integer', 'Text'),
    'airflow': ('Air Flow', 'Number', 'Integer', 'Text'),
    'count': ('Integer', 'Number', 'Text'),
}

# Active profile and the values derived from it (set by set_profile).
PROFILE = None
PROFILE_KEY = None
FIELDS = OrderedDict()
EQUIPMENT_CATEGORIES = []
TERMINAL_CATEGORIES = []
AIRFLOW_UNIT = 'L/s'
AIRFLOW_UNITS = []
_CLASS_MAP = ()


def make_fields(p):
    """Data that can be written to the equipment:
        key: (label in dialog, value kind, default parameter name, description)
    kind: 'text' -> Text parameters only
          'area' / 'airflow' / 'count' -> typed / Number / Integer / Text
    The default parameter name is pre-selected the first time the tool runs."""
    fields = OrderedDict([
        ('room_numbers', ('Served Room Numbers', 'text', 'Served Room Numbers',
                          'Unique room / space numbers, naturally sorted')),
        ('room_names', ('Served Room Names', 'text', 'Served Room Names',
                        'Room / space names')),
        ('room_list', ('Room Number + Name', 'text', '',
                       'Number and name combined in one parameter, e.g. "101 - Gate Lounge; 102 - Corridor". '
                       'Format is chosen in the options.')),
        ('departments', ('Served Departments', 'text', '',
                         'Unique departments of the served rooms')),
        ('levels', ('Served Levels', 'text', '',
                    'Unique levels of the served rooms')),
        ('total_area', ('Total Served Area', 'area', 'Total Served Area',
                        'Sum of the areas of the unique served rooms')),
    ])
    for slot, (label, default) in p['flow_fields'].items():
        fields[FLOW_FIELD_KEYS[slot]] = (
            label, 'airflow', default,
            u'Sum of the {} {} of the {}'.format(p['classes'][slot].lower(), p['qty'].lower(),
                                                p['terms'].lower()))
    fields['space_count'] = ('Number of Served Spaces', 'count', 'Number of Served Spaces',
                             'Count of unique served rooms / spaces')
    fields['terminal_count'] = (u'Number of {}'.format(p['terms']), 'count', '',
                                u'Count of {} credited to the equipment'.format(p['terms'].lower()))
    return fields


def set_profile(key):
    """Switch every profile dependent setting to one MEP system."""
    global PROFILE, PROFILE_KEY, FIELDS, EQUIPMENT_CATEGORIES, TERMINAL_CATEGORIES
    global AIRFLOW_UNIT, AIRFLOW_UNITS, _CLASS_MAP
    p = PROFILES[key]
    PROFILE, PROFILE_KEY = p, key
    FIELDS = make_fields(p)
    EQUIPMENT_CATEGORIES = list(p['equipment_cats'])
    TERMINAL_CATEGORIES = list(p['terminal_cats'])
    AIRFLOW_UNITS = list(p['units'])
    AIRFLOW_UNIT = p['unit']
    COMPATIBLE['airflow'] = (p['flow_type'], 'Number', 'Integer', 'Text')
    enum = {'hvac': DuctSystemType, 'piping': PipeSystemType,
            'electrical': ElectricalSystemType}.get(p['domain'])
    # Built defensively with getattr so a renamed enum member can never stop
    # the script from loading.
    _CLASS_MAP = tuple((getattr(enum, member), slot) for member, slot in p['class_map']
                       if enum is not None and hasattr(enum, member))


def cls_label(slot):
    """Class slot -> the name it has in the active system."""
    return PROFILE['classes'].get(slot, slot)


set_profile('air')

# Default for the "credit upstream equipment" option (terminal -> VAV -> AHU).
CASCADE_UPSTREAM = True

# If the connector trace finds nothing, use MEPSystem.BaseEquipment as a
# fallback (flagged in the report as "system fallback").
USE_SYSTEM_FALLBACK = True

# Room / Space sources, tried in this order.
USE_SPACES = True          # MEP Spaces in the host model
USE_HOST_ROOMS = True      # Rooms in the host model
USE_LINKED_ROOMS = True    # Rooms in loaded Revit links (architectural model)

# Vertical offsets (metres) tried from the terminal insertion point when
# searching for a room. Ceiling diffusers often sit just above a room's
# upper limit, so we probe downwards first.
POINT_Z_OFFSETS_M = [0.0, -0.3, -1.0, -2.0, 0.3]

# "Room Data by Location" only: a wall-mounted socket's insertion point lies on
# the wall face, i.e. on the room boundary. When nothing is found there, these
# distances (metres) in front of (+) and behind (-) the element are tried.
FACE_OFFSETS_M = [0.3, -0.3, 0.6, -0.6]

# Room de-duplication: rooms with the same (level, number) are one room even
# if they were found through different elements (e.g. a Space and a linked
# Room). Set DEDUPE_BY_NUMBER = False to de-duplicate by element only.
DEDUPE_BY_NUMBER = True
DEDUPE_INCLUDE_LEVEL = True

UNIQUE_ROOM_NAMES = True    # "Served Room Names" lists each name once
LIST_SEPARATOR = '; '       # default; selectable in the dialog
MAX_TEXT_LENGTH = 1000      # longer lists are truncated with "(+N more)"

# How each room is written in the "Room Number + Name" field.
# Tokens:  {number} {name} {NAME} (upper case) {Name} (title case)
#          {level} {department} {area} {occupancy}
# A (...) or [...] group containing a token that is empty is dropped as a
# whole, so "{number} - {name} ({occupancy} pax)" becomes "101 - Lobby" when
# occupancy is blank. Dangling separators are trimmed as well.
# Add your own presets here; they appear in the dialog automatically.
ROOM_LIST_FORMATS = OrderedDict([
    # number first
    (u'101 - Gate Lounge',            u'{number} - {name}'),
    (u'101 \u2013 Gate Lounge',        u'{number} \u2013 {name}'),
    (u'101 Gate Lounge',              u'{number} {name}'),
    (u'101: Gate Lounge',             u'{number}: {name}'),
    (u'101 / Gate Lounge',            u'{number} / {name}'),
    (u'101 | Gate Lounge',            u'{number} | {name}'),
    (u'101_Gate Lounge',              u'{number}_{name}'),
    (u'101 (Gate Lounge)',            u'{number} ({name})'),
    (u'101 [Gate Lounge]',            u'{number} [{name}]'),
    (u'[101] Gate Lounge',            u'[{number}] {name}'),
    (u'101 - GATE LOUNGE',            u'{number} - {NAME}'),
    # name first
    (u'Gate Lounge (101)',            u'{name} ({number})'),
    (u'Gate Lounge - 101',            u'{name} - {number}'),
    (u'Gate Lounge [101]',            u'{name} [{number}]'),
    (u'Gate Lounge: 101',             u'{name}: {number}'),
    (u'GATE LOUNGE (101)',            u'{NAME} ({number})'),
    # with extra room data
    (u'L01 / 101 - Gate Lounge',      u'{level} / {number} - {name}'),
    (u'101 - Gate Lounge (L01)',      u'{number} - {name} ({level})'),
    (u'101 - Gate Lounge [Airside]',  u'{number} - {name} [{department}]'),
    (u'101 - Gate Lounge (85.0 m2)',  u'{number} - {name} ({area})'),
    (u'101 - Gate Lounge (120 pax)',  u'{number} - {name} ({occupancy} pax)'),
    (u'101 - Gate Lounge (L01, Airside)', u'{number} - {name} ({level}, {department})'),
])
CUSTOM_FORMAT_LABEL = u'Custom template...'
ROOM_LIST_FORMAT = u'{number} - {name}'

AREA_UNIT = 'm2'            # default; 'm2' or 'ft2'  (flow units: see PROFILES)

CLEAR_UNSERVED_EQUIPMENT = False  # default of the "clear" option

MAX_COMPONENT_ELEMENTS = 250000   # safety stop for a single network walk
REPORT_MAX_ROWS = 500             # rows per table in the output window (CSV has all)
REPORT_EQUIPMENT_DETAIL = True    # default of the "detailed report" option
PROGRESS_STEP = 20                # refresh progress bar every N terminals

# =============================================================================
# CONSTANTS / UNIT CONVERSION (Revit internal units: ft, ft2, ft3/s)
# =============================================================================

FT_PER_M = 1.0 / 0.3048
SQFT_TO = {'m2': 0.09290304, 'ft2': 1.0}
FLOW_CLASSES = ('Supply', 'Return', 'Exhaust', 'Other', 'Unknown')


def flow_display(v_internal):
    """Flow / load from Revit internal units (ft3/s, or kg.ft2/s3 for power)
    to the unit chosen in the dialog."""
    units = PROFILE['units']
    factor = units.get(AIRFLOW_UNIT)
    if factor is None:
        factor = units[PROFILE['unit']]
    return (v_internal or 0.0) * factor


def area_display(v_sqft):
    return (v_sqft or 0.0) * SQFT_TO.get(AREA_UNIT, 0.09290304)


def fmt_flow(v_internal):
    if AIRFLOW_UNIT == 'kVA':
        return '{:,.2f} {}'.format(flow_display(v_internal), AIRFLOW_UNIT)
    return '{:,.1f} {}'.format(flow_display(v_internal), AIRFLOW_UNIT)


def fmt_area(v_sqft):
    return '{:,.2f} {}'.format(area_display(v_sqft), AREA_UNIT)


# =============================================================================
# GENERIC HELPERS
# =============================================================================

def eid_int(eid):
    """ElementId -> int. Revit 2024+ uses .Value, older versions .IntegerValue."""
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def cat_ids(cat_names):
    """Set of integer category ids for a list of BuiltInCategory names."""
    out = set()
    for name in cat_names:
        bic = getattr(DB.BuiltInCategory, name, None)
        if bic is not None:
            out.add(eid_int(DB.ElementId(bic)))
    return out


def cat_id(el):
    try:
        cat = el.Category
        return eid_int(cat.Id) if cat is not None else None
    except Exception:
        return None


def elem_name(el):
    """Element.Name that also works on the CPython engine."""
    try:
        return el.Name or ''
    except Exception:
        try:
            return DB.Element.Name.__get__(el) or ''
        except Exception:
            return ''


def family_type_label(el):
    try:
        sym = el.Symbol
        return u'{}: {}'.format(sym.FamilyName, elem_name(sym))
    except Exception:
        return elem_name(el)


def bip(name):
    """Tolerant BuiltInParameter lookup (names differ slightly across versions)."""
    return getattr(DB.BuiltInParameter, name, None)


def param_text(el, bip_name=None, lookup_name=None):
    """Read a parameter as display text: built-in first, then by name."""
    try:
        p = None
        b = bip(bip_name) if bip_name else None
        if b is not None:
            p = el.get_Parameter(b)
        if (p is None or not p.HasValue) and lookup_name:
            p = el.LookupParameter(lookup_name)
        if p is None or not p.HasValue:
            return ''
        if p.StorageType == DB.StorageType.String:
            return p.AsString() or ''
        return p.AsValueString() or ''
    except Exception:
        return ''


_NUM_RE = re.compile(r'(\d+)')


def natural_key(s):
    """Sort '2' before '10' ('L1-2' < 'L1-10')."""
    return [int(t) if t.isdigit() else t.lower() for t in _NUM_RE.split(s or '')]


def unique(seq):
    seen = set()
    out = []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def join_limited(items, sep=None, limit=None):
    """Join a list, truncating with '(+N more)' so text parameters stay usable.

    Separator / limit are read at call time so the dialog's choice applies.
    """
    sep = LIST_SEPARATOR if sep is None else sep
    limit = MAX_TEXT_LENGTH if limit is None else limit
    out = ''
    for i, it in enumerate(items):
        piece = it if not out else sep + it
        if len(out) + len(piece) > limit - 14:
            return u'{}{}(+{} more)'.format(out, sep, len(items) - i)
        out += piece
    return out


def collect_instances(doc, cat_names):
    out = []
    for name in cat_names:
        b = getattr(DB.BuiltInCategory, name, None)
        if b is None:
            continue
        out.extend(DB.FilteredElementCollector(doc)
                   .OfCategory(b)
                   .WhereElementIsNotElementType()
                   .ToElements())
    return out


def count_instances(doc, cat_name):
    b = getattr(DB.BuiltInCategory, cat_name, None)
    if b is None:
        return 0
    return (DB.FilteredElementCollector(doc).OfCategory(b)
            .WhereElementIsNotElementType().GetElementCount())


# =============================================================================
# CONNECTOR HELPERS
# =============================================================================

_PHYSICAL_TYPES = (DB.ConnectorType.End, DB.ConnectorType.Curve,
                   DB.ConnectorType.Physical)

def trace_domain():
    """Connector domain walked by the active profile (None for electrical)."""
    return {'hvac': DB.Domain.DomainHvac,
            'piping': DB.Domain.DomainPiping}.get(PROFILE['domain'])


def get_hvac_connectors(el):
    """Connectors of the active domain (duct or pipe) of an MEPCurve,
    FabricationPart or FamilyInstance. Empty for the electrical profile."""
    domain = trace_domain()
    if domain is None:
        return []
    cm = None
    try:
        cm = el.ConnectorManager          # Duct, FlexDuct, FabricationPart
    except Exception:
        cm = None
    if cm is None:
        try:
            mep_model = el.MEPModel       # FamilyInstance (fittings, terminals, equipment)
            cm = mep_model.ConnectorManager if mep_model is not None else None
        except Exception:
            cm = None
    if cm is None:
        return []
    out = []
    for c in cm.Connectors:
        try:
            if c.Domain == domain:
                out.append(c)
        except Exception:
            pass
    return out


def physical_refs(conn):
    """Connectors physically joined to `conn` (logical/system refs excluded)."""
    try:
        if not conn.IsConnected:
            return []
        refs = conn.AllRefs
    except Exception:
        return []
    out = []
    domain = trace_domain()
    for r in refs:
        try:
            if r.ConnectorType not in _PHYSICAL_TYPES:
                continue
            if r.Domain != domain:
                continue
            out.append(r)
        except Exception:
            continue
    return out


def dir_key(conn):
    """'In' / 'Out' / 'Bi' (bidirectional or unknown)."""
    try:
        d = conn.Direction
        if d == DB.FlowDirectionType.In:
            return 'In'
        if d == DB.FlowDirectionType.Out:
            return 'Out'
    except Exception:
        pass
    return 'Bi'


def opposite(d):
    return {'In': 'Out', 'Out': 'In'}.get(d, 'Bi')


_UNDEFINED_TYPES = ('UndefinedSystemType', 'Fitting', 'Global')


def _map_system_type(value):
    """System type enum value -> class slot of the active profile.

    A defined type that the profile does not list (e.g. a vent or hydronic
    pipe in the plumbing window) is 'Other', so it can never be mistaken for
    one of the profile's own classes. Undefined types return None."""
    if value is None:
        return None
    for enum_val, key in _CLASS_MAP:
        if value == enum_val:
            return key
    try:
        name = value.ToString()
    except Exception:
        name = '{}'.format(value)
    return None if name in _UNDEFINED_TYPES else 'Other'


def cls_key(conn):
    """Class slot of a connector: Supply / Return / Exhaust / Other / Unknown
    (see the 'classes' of the active profile for what the slots are called)."""
    attr = {'hvac': 'DuctSystemType', 'piping': 'PipeSystemType'}.get(PROFILE['domain'])
    try:
        k = _map_system_type(getattr(conn, attr))
        if k:
            return k
    except Exception:
        pass
    try:
        system = conn.MEPSystem
        if system is not None:
            k = _map_system_type(system.SystemType)
            if k:
                return k
    except Exception:
        pass
    return 'Unknown'


def cls_ok(a, b):
    return a == b or a == 'Unknown' or b == 'Unknown'


# =============================================================================
# NETWORK TRACER
# =============================================================================

class Component(object):
    """A connected set of duct network elements bounded by terminals/equipment."""

    __slots__ = ('id', 'size', 'equipment', 'terminals', 'open_ends', 'truncated')

    def __init__(self, cid):
        self.id = cid
        self.size = 0
        # boundary tuples: (equipment_id, connector_id, direction, classification)
        self.equipment = set()
        self.terminals = set()
        self.open_ends = 0
        self.truncated = False


class NetworkTracer(object):
    """Walks duct networks via Connector.AllRefs with component caching.

    Cost is O(network elements) overall, not O(terminals x network).
    """

    def __init__(self, equipment_cat_ids, terminal_cat_ids,
                 max_elements=MAX_COMPONENT_ELEMENTS):
        self.equip_cats = equipment_cat_ids
        self.term_cats = terminal_cat_ids
        self.max_elements = max_elements
        self.equipment_elements = {}    # equipment id -> Element
        self._elem_comp = {}            # network element id -> Component
        self.components = []
        self._ancestor_cache = {}

    # -- classification ------------------------------------------------------
    def kind(self, el):
        cid = cat_id(el)
        if cid in self.equip_cats:
            return 'equipment'
        if cid in self.term_cats:
            return 'terminal'
        return 'network'

    def _boundary(self, ref, owner, oid):
        self.equipment_elements[oid] = owner
        return (oid, ref.Id, dir_key(ref), cls_key(ref))

    # -- component walk --------------------------------------------------------
    def get_component(self, start_el):
        """Return the cached Component containing start_el, walking it if new."""
        sid = eid_int(start_el.Id)
        comp = self._elem_comp.get(sid)
        if comp is not None:
            return comp
        comp = Component(len(self.components))
        self.components.append(comp)
        self._elem_comp[sid] = comp
        stack = [start_el]
        while stack:
            cur = stack.pop()
            comp.size += 1
            if comp.size > self.max_elements:
                comp.truncated = True
                break
            cur_id = eid_int(cur.Id)
            for c in get_hvac_connectors(cur):
                try:
                    if c.ConnectorType == DB.ConnectorType.End and not c.IsConnected:
                        comp.open_ends += 1
                        continue
                except Exception:
                    pass
                for r in physical_refs(c):
                    owner = r.Owner
                    if owner is None:
                        continue
                    oid = eid_int(owner.Id)
                    if oid == cur_id:
                        continue
                    k = self.kind(owner)
                    if k == 'equipment':
                        comp.equipment.add(self._boundary(r, owner, oid))
                    elif k == 'terminal':
                        comp.terminals.add(oid)
                    elif oid not in self._elem_comp:
                        self._elem_comp[oid] = comp
                        stack.append(owner)
        return comp

    def reach(self, conn, origin_id):
        """Equipment boundaries / terminals reachable from one connector."""
        equips, terms, comps = set(), set(), []
        for r in physical_refs(conn):
            owner = r.Owner
            if owner is None:
                continue
            oid = eid_int(owner.Id)
            if oid == origin_id:
                continue
            k = self.kind(owner)
            if k == 'equipment':
                equips.add(self._boundary(r, owner, oid))
            elif k == 'terminal':
                terms.add(oid)
            else:
                comp = self.get_component(owner)
                comps.append(comp)
                equips |= comp.equipment
                terms |= comp.terminals
        return equips, terms, comps

    @staticmethod
    def select_serving(equips, demand_dir, cls, exclude):
        """Equipment facing the demand side.

        A supply terminal (connector In) is served by an equipment connector
        pointing Out into the same network; VAV inlets on the same main (In)
        are siblings and are rejected. Bidirectional connectors are only used
        when no connector with a definite direction matches.
        """
        want = opposite(demand_dir)
        cands = [e for e in equips if e[0] not in exclude and cls_ok(e[3], cls)]
        if demand_dir == 'Bi':
            return cands, bool(cands)
        strict = [e for e in cands if e[2] == want]
        if strict:
            return strict, False
        loose = [e for e in cands if e[2] == 'Bi']
        return loose, bool(loose)

    # -- upstream cascade ------------------------------------------------------
    def ancestors(self, boundary, demand_dir, cls):
        """Equipment upstream of `boundary` (e.g. AHU above a VAV box).

        Returns OrderedDict {ancestor_id: child_id_it_was_reached_through}.
        """
        key = (boundary[0], boundary[1], demand_dir, cls)
        if key in self._ancestor_cache:
            return self._ancestor_cache[key]
        result = OrderedDict()
        visited = set([boundary[0]])
        queue = deque([(boundary[0], boundary[1])])
        while queue:
            eq_id, arrival_conn = queue.popleft()
            eq_el = self.equipment_elements.get(eq_id)
            if eq_el is None:
                continue
            for c in get_hvac_connectors(eq_el):
                try:
                    if c.Id == arrival_conn or not c.IsConnected:
                        continue
                except Exception:
                    continue
                # continue out of the equipment on the side opposite to the
                # terminals: same direction as the terminal's own connector
                if dir_key(c) not in (demand_dir, 'Bi'):
                    continue
                if not cls_ok(cls_key(c), cls):
                    continue
                equips, _, _ = self.reach(c, eq_id)
                parents, _ = self.select_serving(equips, demand_dir, cls, visited)
                for p in parents:
                    if p[0] in visited:
                        continue
                    visited.add(p[0])
                    result[p[0]] = eq_id
                    queue.append((p[0], p[1]))
        self._ancestor_cache[key] = result
        return result

    # -- per terminal ------------------------------------------------------------
    def trace_terminal(self, term, t_cls, conns=None):
        """Trace one air terminal.

        Returns dict(status, direct=[ids], upstream={id: via_id},
                     ambiguous, network_size, open_ends, truncated, note)
        """
        tid = eid_int(term.Id)
        res = {'status': 'OK', 'direct': [], 'upstream': OrderedDict(),
               'ambiguous': False, 'network_size': 0, 'open_ends': 0,
               'truncated': False, 'note': ''}
        conns = conns if conns is not None else get_hvac_connectors(term)
        if not conns:
            res['status'] = 'NO_CONNECTORS'
            res['note'] = 'Family has no HVAC connector'
            return res
        connected = [c for c in conns if c.IsConnected]
        if not connected:
            res['status'] = 'NOT_CONNECTED'
            res['note'] = 'Connector(s) not connected to any duct'
            return res

        direct_bounds = []
        seen_comp = set()
        for c in connected:
            # A plumbing fixture has one connector per service (cold, hot,
            # sanitary): each connector is traced with its OWN class. An air
            # terminal has a single connector, so this equals the terminal class.
            c_cls = cls_key(c)
            if c_cls == 'Unknown':
                c_cls = t_cls
            d = dir_key(c)
            if d == 'Bi':   # family author left direction bidirectional
                d = PROFILE['bi_dir'].get(c_cls, 'Bi')
            equips, _, comps = self.reach(c, tid)
            for comp in comps:
                if comp.id not in seen_comp:
                    seen_comp.add(comp.id)
                    res['network_size'] += comp.size
                    res['open_ends'] += comp.open_ends
                    res['truncated'] = res['truncated'] or comp.truncated
            sel, amb = self.select_serving(equips, d, c_cls, set([tid]))
            res['ambiguous'] = res['ambiguous'] or amb
            for b in sel:
                if b[0] not in [x[0][0] for x in direct_bounds]:
                    direct_bounds.append((b, d, c_cls))

        res['direct'] = [b[0] for b, _, _ in direct_bounds]
        if CASCADE_UPSTREAM:
            for b, d, c_cls in direct_bounds:
                for anc, via in self.ancestors(b, d, c_cls).items():
                    if anc not in res['direct'] and anc not in res['upstream']:
                        res['upstream'][anc] = via

        if not res['direct']:
            res['status'] = 'NO_EQUIPMENT'
            res['note'] = ('Network of {} element(s), {} open end(s); no {} equipment found'
                           .format(res['network_size'], res['open_ends'],
                                   cls_label(t_cls).lower() if t_cls != 'Unknown' else 'matching'))
        elif res['truncated']:
            res['note'] = 'Network walk truncated at MAX_COMPONENT_ELEMENTS'
        return res

    def neighbours(self, eq_id):
        """(equipment ids, terminal ids) on the networks joined to an equipment."""
        equips, terms = set(), set()
        el = self.equipment_elements.get(eq_id)
        if el is None:
            return equips, terms
        for c in get_hvac_connectors(el):
            try:
                if not c.IsConnected:
                    continue
            except Exception:
                continue
            bounds, found, _ = self.reach(c, eq_id)
            equips.update(b[0] for b in bounds)
            terms |= found
        return equips, terms


# =============================================================================
# ELECTRICAL: CIRCUIT TRACER (same interface as NetworkTracer)
# =============================================================================

def electrical_systems(el):
    """Circuits an element is connected to as a LOAD."""
    try:
        model = el.MEPModel
    except Exception:
        model = None
    if model is None:
        return []
    try:
        systems = model.GetElectricalSystems()             # Revit 2021+
    except Exception:
        try:
            systems = model.ElectricalSystems              # Revit 2020 and older
        except Exception:
            systems = None
    return list(systems) if systems is not None else []


def assigned_systems(el):
    """Circuits a panel SUPPLIES."""
    try:
        systems = el.MEPModel.GetAssignedElectricalSystems()
        return list(systems) if systems is not None else []
    except Exception:
        pass
    me = eid_int(el.Id)                                    # older API: filter by panel
    return [s for s in electrical_systems(el) if _base_id(s) == me]


def _base_id(system):
    try:
        base = system.BaseEquipment
        return eid_int(base.Id) if base is not None else None
    except Exception:
        return None


def circuit_class(system):
    try:
        return _map_system_type(system.SystemType) or 'Unknown'
    except Exception:
        return 'Unknown'


def electrical_load(el):
    """Apparent load of a device in internal units: instance, then type."""
    sources = [el]
    try:
        sources.append(el.Document.GetElement(el.GetTypeId()))
    except Exception:
        pass
    b = bip(PROFILE['flow_bip'])
    for src in sources:
        if src is None:
            continue
        for getter in ((lambda s: s.get_Parameter(b)) if b is not None else None,
                       lambda s: s.LookupParameter('Apparent Load')):
            if getter is None:
                continue
            try:
                p = getter(src)
                if (p is not None and p.HasValue and p.StorageType == DB.StorageType.Double
                        and p.AsDouble() > 0):
                    return p.AsDouble()
            except Exception:
                pass
    return 0.0


class CircuitTracer(object):
    """Device -> circuit -> panel, and panel -> feeder circuit -> upstream panel.

    Electrical connections are logical (circuits), not physical connectors, so
    nothing is walked: ElectricalSystem.BaseEquipment gives the panel directly.
    """

    def __init__(self, equipment_cat_ids, terminal_cat_ids):
        self.equip_cats = equipment_cat_ids
        self.term_cats = terminal_cat_ids
        self.equipment_elements = {}    # panel id -> Element
        self.components = []            # circuit ids seen (for the report count)
        self._circuits = set()
        self._upstream_cache = {}

    def _panel(self, system):
        """Panel id of a circuit (registered), or None if it has no panel."""
        try:
            base = system.BaseEquipment
        except Exception:
            base = None
        if base is None:
            return None
        bid = eid_int(base.Id)
        self.equipment_elements[bid] = base
        return bid

    def _seen(self, system):
        try:
            sid = eid_int(system.Id)
        except Exception:
            return
        if sid not in self._circuits:
            self._circuits.add(sid)
            self.components.append(sid)

    def upstream(self, panel_id):
        """OrderedDict {upstream panel id: panel it was reached through}."""
        if panel_id in self._upstream_cache:
            return self._upstream_cache[panel_id]
        result = OrderedDict()
        visited = set([panel_id])
        queue = deque([panel_id])
        while queue:
            pid = queue.popleft()
            el = self.equipment_elements.get(pid)
            if el is None:
                continue
            for system in electrical_systems(el):
                bid = self._panel(system)
                if bid is None or bid in visited:      # bid == pid: a circuit it supplies
                    continue
                self._seen(system)
                visited.add(bid)
                result[bid] = pid
                queue.append(bid)
        self._upstream_cache[panel_id] = result
        return result

    def trace_terminal(self, term, t_cls, conns=None):
        tid = eid_int(term.Id)
        res = {'status': 'OK', 'direct': [], 'upstream': OrderedDict(),
               'ambiguous': False, 'network_size': 0, 'open_ends': 0,
               'truncated': False, 'note': ''}
        systems = electrical_systems(term)
        if not systems:
            res['status'] = 'NOT_CONNECTED'
            res['note'] = 'Not connected to any circuit'
            return res
        no_panel = 0
        for system in systems:
            self._seen(system)
            res['network_size'] += 1
            bid = self._panel(system)
            if bid is None:
                no_panel += 1
            elif bid != tid and bid not in res['direct']:
                res['direct'].append(bid)
        if CASCADE_UPSTREAM:
            for bid in res['direct']:
                for anc, via in self.upstream(bid).items():
                    if anc != tid and anc not in res['direct'] and anc not in res['upstream']:
                        res['upstream'][anc] = via
        if not res['direct']:
            res['status'] = 'NO_EQUIPMENT'
            res['note'] = '{} circuit(s), none assigned to a panel'.format(len(systems))
        elif no_panel:
            res['note'] = '{} of {} circuit(s) not assigned to a panel'.format(no_panel, len(systems))
        return res

    def neighbours(self, eq_id):
        """(panel ids, device ids) on the circuits of a panel."""
        equips, terms = set(), set()
        el = self.equipment_elements.get(eq_id)
        if el is None:
            return equips, terms
        for system in assigned_systems(el):
            try:
                members = list(system.Elements)
            except Exception:
                members = []
            for member in members:
                mid = eid_int(member.Id)
                cid = cat_id(member)
                if cid in self.equip_cats:
                    self.equipment_elements[mid] = member
                    equips.add(mid)
                elif cid in self.term_cats:
                    terms.add(mid)
        equips.update(self.upstream(eq_id).keys())
        equips.discard(eq_id)
        return equips, terms


# =============================================================================
# ROOM / SPACE RESOLUTION
# =============================================================================

def last_phase(d):
    try:
        phases = list(d.Phases)
        return phases[-1] if phases else None
    except Exception:
        return None


def view_phase(doc, view):
    try:
        p = view.get_Parameter(DB.BuiltInParameter.VIEW_PHASE)
        if p is not None:
            ph = doc.GetElement(p.AsElementId())
            if ph is not None:
                return ph
    except Exception:
        pass
    return last_phase(doc)


def _fi_spatial(fi, prop, phase):
    """FamilyInstance.Space[phase] / Room[phase] on both Python engines."""
    if phase is not None:
        try:
            return getattr(fi, 'get_' + prop)(phase)
        except Exception:
            pass
        try:
            return getattr(fi, prop)[phase]
        except Exception:
            pass
    try:
        return getattr(fi, prop)
    except Exception:
        return None


def _at_point(fn, pt, phase):
    try:
        return fn(pt, phase) if phase is not None else fn(pt)
    except Exception:
        return None


def _placed(sp):
    try:
        return sp is not None and sp.Area > 0
    except Exception:
        return False


def collect_links(doc):
    """Loaded Revit links with their transforms, phases and element counts."""
    links, counts_cache = [], {}
    for li in DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance):
        info = {'name': elem_name(li), 'instance_id': eid_int(li.Id),
                'doc': None, 'loaded': False, 'rooms': 0, 'terminals': 0}
        try:
            ldoc = li.GetLinkDocument()
        except Exception:
            ldoc = None
        if ldoc is not None:
            key = ldoc.PathName or ldoc.Title
            if key not in counts_cache:
                counts_cache[key] = (count_instances(ldoc, 'OST_Rooms'),
                                     sum(count_instances(ldoc, c) for c in TERMINAL_CATEGORIES))
            info.update({'doc': ldoc, 'loaded': True, 'key': key,
                         'inverse': li.GetTotalTransform().Inverse,
                         'phase': last_phase(ldoc),
                         'rooms': counts_cache[key][0],
                         'terminals': counts_cache[key][1]})
        links.append(info)
    return links


class RoomResolver(object):
    """Finds the Space/Room for a terminal: host Space -> host Room -> linked Room."""

    def __init__(self, doc, phase, links, prefer_rooms=False, probe_facing=False):
        # prefer_rooms : host Room -> linked Room -> Space instead of the default order
        # probe_facing : also test points in front of / behind the element, for
        #                wall-mounted devices whose insertion point is on the wall face
        self.prefer_rooms = prefer_rooms
        self.face_offsets = [d * FT_PER_M for d in FACE_OFFSETS_M] if probe_facing else []
        self.doc = doc
        self.phase = phase
        self.doc_key = doc.PathName or doc.Title
        self.use_spaces = USE_SPACES and count_instances(doc, 'OST_MEPSpaces') > 0
        self.use_rooms = USE_HOST_ROOMS and count_instances(doc, 'OST_Rooms') > 0
        self.links = [l for l in links if USE_LINKED_ROOMS and l['loaded'] and l['rooms'] > 0]
        self.z_offsets = [z * FT_PER_M for z in POINT_Z_OFFSETS_M]
        self._cache = {}

    def _test_points(self, fi):
        base = None
        try:
            loc = fi.Location
            if isinstance(loc, DB.LocationPoint):
                base = loc.Point
        except Exception:
            pass
        if base is None:
            try:
                bb = fi.get_BoundingBox(None)
                base = (bb.Min + bb.Max) * 0.5
            except Exception:
                return []
        pts = [DB.XYZ(base.X, base.Y, base.Z + dz) for dz in self.z_offsets]
        if self.face_offsets:
            try:
                facing = fi.FacingOrientation
                if facing is not None and abs(facing.X) + abs(facing.Y) > 1e-6:
                    for d in self.face_offsets:
                        pts.append(DB.XYZ(base.X + facing.X * d, base.Y + facing.Y * d, base.Z))
            except Exception:
                pass
        return pts

    def resolve(self, fi):
        """Returns (room_info or None, how_it_was_found)."""
        pts = self._test_points(fi)
        if self.prefer_rooms:
            steps = (self._in_host_rooms, self._in_linked_rooms, self._in_spaces)
        else:
            steps = (self._in_spaces, self._in_host_rooms, self._in_linked_rooms)
        for step in steps:
            found = step(fi, pts)
            if found is not None:
                return found
        return None, 'No placed Space/Room at the element location'

    def _in_spaces(self, fi, pts):
        if not self.use_spaces:
            return None
        sp = _fi_spatial(fi, 'Space', self.phase)
        if _placed(sp):
            return self._info(sp, 'Space', self.doc_key, 'Host'), 'FamilyInstance.Space'
        for i, pt in enumerate(pts):
            sp = _at_point(self.doc.GetSpaceAtPoint, pt, self.phase)
            if _placed(sp):
                return self._info(sp, 'Space', self.doc_key, 'Host'), 'Space at point #{}'.format(i)
        return None

    def _in_host_rooms(self, fi, pts):
        if not self.use_rooms:
            return None
        rm = _fi_spatial(fi, 'Room', self.phase)
        if _placed(rm):
            return self._info(rm, 'Room', self.doc_key, 'Host'), 'FamilyInstance.Room'
        for i, pt in enumerate(pts):
            rm = _at_point(self.doc.GetRoomAtPoint, pt, self.phase)
            if _placed(rm):
                return self._info(rm, 'Room', self.doc_key, 'Host'), 'Room at point #{}'.format(i)
        return None

    def _in_linked_rooms(self, fi, pts):
        for lk in self.links:
            for i, pt in enumerate(pts):
                rm = _at_point(lk['doc'].GetRoomAtPoint, lk['inverse'].OfPoint(pt), lk['phase'])
                if _placed(rm):
                    return (self._info(rm, 'Linked Room', lk['key'], lk['name']),
                            'Linked room at point #{}'.format(i))
        return None

    def _info(self, el, source, doc_key, doc_label):
        uid = (doc_key, eid_int(el.Id))
        if uid in self._cache:
            return self._cache[uid]
        try:
            level = el.Level.Name if el.Level is not None else ''
        except Exception:
            level = ''
        number = (el.Number or '') if hasattr(el, 'Number') else param_text(el, 'ROOM_NUMBER')
        name = param_text(el, 'ROOM_NAME')
        department = param_text(el, 'ROOM_DEPARTMENT', 'Department')
        if source == 'Space':
            try:
                occupancy = '{:g} people'.format(el.NumberofPeople)
            except Exception:
                occupancy = param_text(el, None, 'Occupancy')
        else:
            occupancy = param_text(el, 'ROOM_OCCUPANCY', 'Occupancy')

        def _prop(n):
            try:
                return getattr(el, n)
            except Exception:
                return None

        info = {
            'uid': uid, 'element_id': uid[1], 'source': source, 'doc_label': doc_label,
            'element': el if doc_label == 'Host' else None,
            'number': number, 'name': name, 'department': department,
            'level': level, 'area': el.Area, 'occupancy': occupancy,
            # Space airflow properties exist only on MEP Spaces
            'space_design_supply': _prop('DesignSupplyAirflow') if source == 'Space' else None,
            'space_actual_supply': _prop('ActualSupplyAirflow') if source == 'Space' else None,
            'space_actual_return': _prop('ActualReturnAirflow') if source == 'Space' else None,
            'space_actual_exhaust': _prop('ActualExhaustAirflow') if source == 'Space' else None,
        }
        if DEDUPE_BY_NUMBER and number.strip():
            lvl = level.strip().lower() if DEDUPE_INCLUDE_LEVEL else ''
            info['dedupe_key'] = ('num', lvl, number.strip().lower())
        else:
            info['dedupe_key'] = ('uid',) + uid
        self._cache[uid] = info
        return info


# =============================================================================
# TERMINAL DATA
# =============================================================================

def terminal_class(term, conns):
    for c in conns:
        k = cls_key(c)
        if k != 'Unknown':
            return k
    text = param_text(term, 'RBS_SYSTEM_CLASSIFICATION_PARAM').lower()
    for word, key in PROFILE['class_words']:
        if word in text:
            return key
    return 'Unknown'


def terminal_data(term):
    """(connectors, primary class slot, {class slot: flow / load}) of a terminal."""
    if PROFILE['domain'] == 'electrical':
        cls = 'Unknown'
        for system in electrical_systems(term):
            cls = circuit_class(system)
            if cls != 'Unknown':
                break
        return [], cls, {'Supply': electrical_load(term)}
    conns = get_hvac_connectors(term)
    cls = terminal_class(term, conns)
    if PROFILE['domain'] == 'piping':
        # one connector per service: keep each service's own flow
        flows = {}
        for c in conns:
            try:
                value = c.Flow
            except Exception:
                value = 0.0
            if value:
                k = cls_key(c)
                k = cls if k == 'Unknown' else k
                flows[k] = flows.get(k, 0.0) + value
        if not flows:
            flows = {cls: terminal_flow(term, [])}
        if len(PROFILE['flow_fields']) == 1:
            # a system with a single total (fire protection): every connector
            # counts, also one whose system type is not set yet
            flows = {list(PROFILE['flow_fields'])[0]: sum(flows.values())}
        return conns, cls, flows
    return conns, cls, {cls: terminal_flow(term, conns)}


def terminal_flow(term, conns):
    """Terminal flow in internal units (ft3/s): Flow parameter, then connectors."""
    b = bip(PROFILE['flow_bip'])
    try:
        p = term.get_Parameter(b) if b is not None else None
        if p is not None and p.HasValue and p.AsDouble() > 0:
            return p.AsDouble()
    except Exception:
        pass
    total = 0.0
    for c in conns:
        try:
            total += c.Flow
        except Exception:
            pass
    return total


# =============================================================================
# AGGREGATION
# =============================================================================

def new_equipment_entry(el):
    return {
        'el': el, 'id': eid_int(el.Id),
        'label': family_type_label(el),
        'mark': param_text(el, 'ALL_MODEL_MARK'),
        'system': param_text(el, 'RBS_SYSTEM_NAME_PARAM') or param_text(el, 'RBS_ELEC_PANEL_NAME'),
        'terminals': OrderedDict(),            # terminal id -> relation text
        'direct_count': 0, 'upstream_count': 0,
        'rooms': OrderedDict(),                # dedupe key -> room info
        'room_terms': {},                      # dedupe key -> terminal count
        'room_flow': {},                       # dedupe key -> {cls: flow}
        'flow': dict((k, 0.0) for k in FLOW_CLASSES),
        'no_room_terminals': 0,
        'write_status': 'Report only',
    }


def aggregate(term_records, tracer, include_upstream=True):
    eq_data = OrderedDict()
    for rec in term_records.values():
        credits = [(eq, 'direct') for eq in rec['direct']]
        if include_upstream:
            credits += [(eq, 'via {}'.format(via)) for eq, via in rec['upstream'].items()]
        for eq_id, relation in credits:
            el = tracer.equipment_elements.get(eq_id)
            if el is None:
                continue
            ed = eq_data.get(eq_id)
            if ed is None:
                ed = eq_data[eq_id] = new_equipment_entry(el)
            if rec['id'] in ed['terminals']:
                continue                       # never double count a terminal
            ed['terminals'][rec['id']] = relation
            if relation == 'direct':
                ed['direct_count'] += 1
            else:
                ed['upstream_count'] += 1
            flows = rec.get('flows') or {rec['cls']: rec['flow']}
            for cls, value in flows.items():
                ed['flow'][cls] = ed['flow'].get(cls, 0.0) + value
            room = rec['room']
            if room is None:
                ed['no_room_terminals'] += 1
                continue
            k = room['dedupe_key']
            if k not in ed['rooms']:
                ed['rooms'][k] = room
                ed['room_terms'][k] = 0
                ed['room_flow'][k] = dict((c, 0.0) for c in FLOW_CLASSES)
            ed['room_terms'][k] += 1
            for cls, value in flows.items():
                ed['room_flow'][k][cls] = ed['room_flow'][k].get(cls, 0.0) + value
    return eq_data


_ROOM_TOKEN_RE = re.compile(r'\{(number|name|NAME|Name|level|department|area|occupancy)\}')
_EMPTY_BRACKETS_RE = re.compile(r'[\(\[]\s*[\)\]]')
_BRACKET_GROUP_RE = re.compile(r'\s*(\([^()\[\]]*\)|\[[^()\[\]]*\])')
_TRIM_CHARS = u' -\u2013\u2014:/|_,;.'


def format_room(template, r):
    """Apply a room template such as '{number} - {name} ({level})'."""
    name = r.get('name') or ''
    occupancy = (r.get('occupancy') or '').replace(' people', '')
    values = {
        'number': r.get('number') or '', 'name': name,
        'NAME': name.upper(), 'Name': name.title(),
        'level': r.get('level') or '', 'department': r.get('department') or '',
        'area': (u'{:,.1f} {}'.format(area_display(r['area']), AREA_UNIT)
                 if r.get('area') else ''),
        'occupancy': occupancy,
    }

    def fill(txt):
        return _ROOM_TOKEN_RE.sub(lambda mt: values[mt.group(1)], txt)

    def group(mt):
        # drop an optional (...) / [...] group if any token inside it is empty
        chunk = mt.group(0)
        tokens = _ROOM_TOKEN_RE.findall(chunk)
        if tokens and not all(values[t] for t in tokens):
            return ''
        return fill(chunk)

    text = _BRACKET_GROUP_RE.sub(group, template or u'{number} - {name}')
    text = fill(text)
    text = _EMPTY_BRACKETS_RE.sub('', text)            # "(" + empty + ")" left by blank tokens
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip(_TRIM_CHARS)


def sorted_rooms(ed):
    return sorted(ed['rooms'].values(), key=lambda r: natural_key(r['number']))


def equipment_values(ed):
    """{field key: (kind, value)} for every FIELDS entry. Numbers in internal units."""
    rooms = sorted_rooms(ed)
    numbers = unique([r['number'] or '(no number)' for r in rooms])
    names = [r['name'] or '(no name)' for r in rooms]
    if UNIQUE_ROOM_NAMES:
        names = unique(names)
    room_list = unique([t for t in (format_room(ROOM_LIST_FORMAT, r) for r in rooms) if t])
    departments = unique([r['department'] for r in rooms if r['department']])
    levels = sorted(unique([r['level'] for r in rooms if r['level']]), key=natural_key)
    return {
        'room_numbers': ('text', join_limited(numbers)),
        'room_names': ('text', join_limited(names)),
        'room_list': ('text', join_limited(room_list)),
        'departments': ('text', join_limited(departments)),
        'levels': ('text', join_limited(levels)),
        'total_area': ('area', sum(r['area'] for r in rooms)),
        'supply_flow': ('airflow', ed['flow'].get('Supply', 0.0)),
        'return_flow': ('airflow', ed['flow'].get('Return', 0.0)),
        'exhaust_flow': ('airflow', ed['flow'].get('Exhaust', 0.0)),
        'space_count': ('count', len(rooms)),
        'terminal_count': ('count', len(ed['terminals'])),
    }


def empty_values():
    out = {}
    for key, (_, kind, _, _) in FIELDS.items():
        out[key] = (kind, '' if kind == 'text' else 0.0)
    return out


# =============================================================================
# PARAMETER DISCOVERY AND WRITING
# =============================================================================

def _spec_base(type_id):
    return (type_id or '').split('-')[0]


def spec_kind(param):
    """'airflow' / 'area' / 'number' / 'yesno' / 'other' for a parameter."""
    try:                                              # Revit 2022+
        tid = _spec_base(param.Definition.GetDataType().TypeId)
        for kind, spec in (('airflow', PROFILE['spec']), ('area', 'Area'), ('number', 'Number')):
            spec_id = getattr(DB.SpecTypeId, spec, None)
            if spec_id is not None and tid == _spec_base(spec_id.TypeId):
                return kind
        try:
            if tid == _spec_base(DB.SpecTypeId.Boolean.YesNo.TypeId):
                return 'yesno'
        except Exception:
            pass
        return 'other'
    except Exception:
        pass
    try:                                              # Revit 2021 and older
        pt = param.Definition.ParameterType
        for kind, name in (('airflow', PROFILE['legacy_spec']), ('area', 'Area'),
                           ('number', 'Number'), ('yesno', 'YesNo')):
            if pt == getattr(DB.ParameterType, name, None):
                return kind
    except Exception:
        pass
    return 'other'


def param_type_label(param):
    """Short type label for the dialog, or None if the parameter is unusable."""
    st = param.StorageType
    if st == DB.StorageType.String:
        return 'Text'
    if st == DB.StorageType.Double:
        return {'airflow': PROFILE['flow_type'], 'area': 'Area',
                'number': 'Number'}.get(spec_kind(param))
    if st == DB.StorageType.Integer:
        return None if spec_kind(param) == 'yesno' else 'Integer'
    return None


def discover_parameters(equipment):
    """Writable instance parameters on the equipment, one instance per type.

    Returns (OrderedDict name -> {'type': label, 'types': n}, total type count)
    """
    reps = OrderedDict()
    for el in equipment:
        try:
            reps.setdefault(eid_int(el.GetTypeId()), el)
        except Exception:
            pass
    found = {}
    for el in reps.values():
        seen = set()
        for p in el.Parameters:
            try:
                if p.IsReadOnly:
                    continue
                name = p.Definition.Name
                if not name or name in seen:
                    continue
                label = param_type_label(p)
                if label is None:
                    continue
                seen.add(name)
                entry = found.setdefault(name, {'type': label, 'types': 0})
                entry['types'] += 1
            except Exception:
                continue
    ordered = OrderedDict(sorted(found.items(), key=lambda kv: kv[0].lower()))
    return ordered, len(reps)


def _display(kind, value):
    if kind == 'airflow':
        return flow_display(value)
    if kind == 'area':
        return area_display(value)
    return float(value)


def _as_text(kind, value):
    if kind == 'text':
        return value
    if kind == 'airflow':
        return fmt_flow(value)
    if kind == 'area':
        return fmt_area(value)
    return '{}'.format(int(value))


def write_param(param, kind, value):
    """Set one parameter. Returns 'written' / 'unchanged'; raises on problems."""
    if param.IsReadOnly:
        raise ValueError('read-only')
    st = param.StorageType
    if st == DB.StorageType.String:
        text = _as_text(kind, value)
        if (param.AsString() or '') == text:
            return 'unchanged'
        param.Set(text)
        return 'written'
    if kind == 'text':
        raise ValueError('text value but parameter is not Text')
    if st == DB.StorageType.Double:
        sk = spec_kind(param)
        if sk == kind:                 # typed Air Flow / Area -> internal units
            v = float(value)
        elif sk == 'number':           # plain Number -> display units
            v = _display(kind, value)
        else:
            raise ValueError('parameter unit type does not match {}'.format(kind))
        if abs(param.AsDouble() - v) < 1e-6:
            return 'unchanged'
        param.Set(v)
        return 'written'
    if st == DB.StorageType.Integer:
        v = int(round(_display(kind, value)))
        if param.AsInteger() == v:
            return 'unchanged'
        param.Set(v)
        return 'written'
    raise ValueError('unsupported storage type')


def write_equipment(doc, el, values, mapping, missing_params, logger):
    """Write the mapped values to one equipment element (inside a Transaction)."""
    if doc.IsWorkshared:
        try:
            status = DB.WorksharingUtils.GetCheckoutStatus(doc, el.Id)
            if status == DB.CheckoutStatus.OwnedByOtherUser:
                return 'SKIPPED - owned by another user'
        except Exception:
            pass
    written, unchanged, problems = 0, 0, []
    for key, pname in mapping.items():
        if not pname:
            continue
        kind, value = values[key]
        p = el.LookupParameter(pname)
        if p is None:
            missing_params[pname] = missing_params.get(pname, 0) + 1
            problems.append('missing "{}"'.format(pname))
            continue
        try:
            if write_param(p, kind, value) == 'written':
                written += 1
            else:
                unchanged += 1
        except Exception as ex:
            problems.append(u'"{}": {}'.format(pname, ex))
            logger.warning(u'Equipment {} parameter "{}": {}'.format(eid_int(el.Id), pname, ex))
    status = '{} written, {} unchanged'.format(written, unchanged)
    if problems:
        status += ' | ' + '; '.join(problems)
    return status


# =============================================================================
# CSV EXPORT HELPERS
# =============================================================================

def _csv_cell(v):
    s = '' if v is None else u'{}'.format(v)
    if any(ch in s for ch in (',', '"', '\n', '\r')):
        s = u'"{}"'.format(s.replace('"', '""'))
    return s


def write_csv(path, header, rows):
    # utf-8-sig so Excel opens Arabic / accented room names correctly
    with codecs.open(path, 'w', 'utf-8-sig') as f:
        f.write(u','.join(_csv_cell(h) for h in header) + u'\r\n')
        for row in rows:
            f.write(u','.join(_csv_cell(c) for c in row) + u'\r\n')


# =============================================================================
# DIALOG
# =============================================================================

NO_PARAM = '<Do not write>'
AREA_UNITS = ['m2', 'ft2']
SEPARATORS = OrderedDict([('Semicolon  ( ; )', '; '), ('Comma  ( , )', ', '),
                          ('Pipe  ( | )', ' | '), ('Slash  ( / )', ' / ')])
COL_TERMS = 5                     # DataGrid column indexes (see ui.xaml)
FLOW_COLUMNS = OrderedDict([('Supply', 8), ('Return', 9), ('Exhaust', 10)])   # class slot -> column


def _xml(s):
    return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
             .replace('"', '&quot;'))


def build_xaml():
    """Load ui.xaml and inject one mapping row per FIELDS entry."""
    path = os.path.join(os.path.dirname(__file__), 'ui.xaml')
    with codecs.open(path, 'r', 'utf-8') as f:
        xaml = f.read()
    kind_text = {'text': 'Text', 'area': 'Area / Number / Integer / Text',
                 'airflow': '{} / Number / Integer / Text'.format(PROFILE['flow_type']),
                 'count': 'Integer / Number / Text'}
    rows = []
    for key, (label, kind, _, desc) in FIELDS.items():
        rows.append(
            u'<Grid Margin="0,0,0,7"><Grid.ColumnDefinitions>'
            u'<ColumnDefinition Width="150"/><ColumnDefinition Width="*"/>'
            u'</Grid.ColumnDefinitions>'
            u'<StackPanel VerticalAlignment="Center" ToolTip="{desc}">'
            u'<TextBlock Text="{label}" FontWeight="SemiBold" Foreground="#1F2D3D" TextTrimming="CharacterEllipsis"/>'
            u'<TextBlock Text="{kinds}" Foreground="#6B7785" FontSize="10" TextTrimming="CharacterEllipsis"/>'
            u'</StackPanel>'
            u'<ComboBox Grid.Column="1" x:Name="map_{key}" IsTextSearchEnabled="True" ToolTip="{desc}"/>'
            u'</Grid>'.format(key=key, label=_xml(label), desc=_xml(desc), kinds=_xml(kind_text[kind])))
    return xaml.replace('{{MAPPING_ROWS}}', u'\n'.join(rows))


def _net_list(items):
    import System
    from System.Collections.Generic import List
    lst = List[System.String]()
    for i in items:
        lst.Add(i)
    return lst


def _like(text):
    """Escape a user string for a DataView RowFilter LIKE expression."""
    out = []
    for ch in text:
        if ch in '[]*%':
            out.append('[' + ch + ']')
        elif ch == "'":
            out.append("''")
        else:
            out.append(ch)
    return ''.join(out)


class RoomsToEquipmentWindow(forms.WPFWindow):
    """Step 1 equipment selection, step 2 parameter mapping, step 3 options."""

    def __init__(self, ctx, state):
        forms.WPFWindow.__init__(self, build_xaml(), literal_string=True)
        self.ctx = ctx
        self.state = state
        self.action = None
        self._busy = False               # suppress events during bulk edits
        self._param_display = {}         # combo text -> parameter name
        self._apply_profile()
        self._init_scope()
        self._build_table()
        self._init_mapping()
        self._init_options()
        self._wire_events()
        self.ui('summary_tb').Text = ctx['summary']
        self.ui('search_tb').Text = state.get('search', '')
        self.ui('only_served_cb').IsChecked = state.get('only_served', True)
        self._refresh_preview_columns()
        self._apply_filter()
        self._update_counts()
        self._update_preview()

    # ---- small helpers ------------------------------------------------------------
    def ui(self, name):
        return self.FindName(name)

    def _checked(self, name):
        return bool(self.ui(name).IsChecked)

    def _eq_data(self):
        return self.ctx['eq_cascade'] if self._checked('cascade_cb') else self.ctx['eq_direct']

    def _apply_profile(self):
        """Make this the dedicated window of the active MEP system: title,
        header colour and every label that names the elements involved."""
        from System.Windows.Media import BrushConverter
        p = PROFILE
        terms, equip = p['terms'], p['equip']
        title = u'Rooms to Equipment – {}'.format(p['title'])
        self.Title = title
        self.ui('title_tb').Text = title
        self.ui('subtitle_tb').Text = (u'Transfer Room / Space data from {} to the {} that serves them'
                                       .format(terms, equip))
        try:
            convert = BrushConverter().ConvertFromString
            self.ui('header_bd').Background = convert(p['header'])
            self.ui('sel_count_tb').Foreground = convert(p['accent'])
        except Exception:
            pass
        self.ui('step2_title_tb').Text = u'Select the {} to update'.format(equip)
        self.ui('only_served_cb').Content = u'Only equipment serving scanned {}'.format(terms.lower())
        self.ui('scope_view_rb').ToolTip = (u'{} visible in the active plan, section, elevation or 3D view '
                                            u'(section box respected)'.format(terms))
        self.ui('complete_cb').Content = (
            u'Complete totals: also trace {} outside the scope that are on the same {} systems '
            u'(recommended)'.format(terms.lower(), p['net']))
        self.ui('complete_cb').ToolTip = (u'Without this, equipment serving several floors would only '
                                          u'receive the rooms and {} of the scanned floors.'
                                          .format(p['qty'].lower()))
        self.ui('btn_from_sel').ToolTip = (u'Checks equipment selected in Revit, and equipment serving '
                                           u'selected {}'.format(terms))
        self.ui('mapping_hint_tb').Text = (
            u'Writable instance parameters found on {}. [Text · 12/14 types] = storage type and how '
            u'many equipment types have the parameter. Leave a row on <Do not write> to skip it.'
            .format(equip))
        self.ui('cascade_cb').Content = p['cascade']
        self.ui('flow_unit_label_tb').Text = u'{} unit'.format(p['qty'])
        self.ui('units_hint_tb').Text = (
            u'Units apply to Number / Integer / Text parameters and the report. {} and Area parameters '
            u'always follow the project units.'.format(p['flow_type']))
        self.ui('equip_grid').Columns[COL_TERMS].Header = p['terms_short']

    # ---- step 1: equipment table ---------------------------------------------------
    def _build_table(self):
        import clr
        # System.Data is not loaded by pyRevit by default; IronPython can only
        # import .NET namespaces from assemblies that have been referenced.
        clr.AddReference('System.Data')
        import System
        from System.Data import DataTable
        tbl = DataTable('equipment')
        for name, t in (('Update', System.Boolean), ('Id', System.Int64),
                        ('Mark', System.String), ('Equipment', System.String),
                        ('System', System.String), ('Level', System.String),
                        ('Terminals', System.Int32), ('Spaces', System.Int32),
                        ('Rooms', System.String), ('Supply', System.Double),
                        ('Return', System.Double), ('Exhaust', System.Double)):
            tbl.Columns.Add(name, clr.GetClrType(t))
        checked = self.state.get('checked', set())
        for eq in self.ctx['equipment']:
            row = tbl.NewRow()
            row['Update'] = eq['id'] in checked
            row['Id'] = eq['id']
            row['Mark'] = eq['mark']
            row['Equipment'] = eq['label']
            row['System'] = eq['system']
            row['Level'] = eq['level']
            row['Terminals'] = 0
            row['Spaces'] = 0
            row['Rooms'] = ''
            row['Supply'] = 0.0
            row['Return'] = 0.0
            row['Exhaust'] = 0.0
            tbl.Rows.Add(row)
        self.table = tbl
        self.ui('equip_grid').ItemsSource = tbl.DefaultView

    def _refresh_preview_columns(self):
        """Fill Terminals / Spaces / Rooms and the flow columns for the current options."""
        data = self._eq_data()
        digits = 2 if AIRFLOW_UNIT == 'kVA' else 1
        self._busy = True
        try:
            for row in self.table.Rows:
                ed = data.get(int(row['Id']))
                if ed is None:
                    row['Terminals'], row['Spaces'], row['Rooms'] = 0, 0, ''
                    for slot in FLOW_COLUMNS:
                        row[slot] = 0.0
                    continue
                numbers = unique([r['number'] for r in sorted_rooms(ed)])
                row['Terminals'] = len(ed['terminals'])
                row['Spaces'] = len(ed['rooms'])
                row['Rooms'] = join_limited(numbers, limit=200)
                for slot in FLOW_COLUMNS:
                    row[slot] = round(flow_display(ed['flow'].get(slot, 0.0)), digits)
        finally:
            self._busy = False
        # one column per total the active system has; the others are hidden
        cols = self.ui('equip_grid').Columns
        for slot, index in FLOW_COLUMNS.items():
            used = slot in PROFILE['flow_fields']
            cols[index].Visibility = self._visibility(used)
            if used:
                cols[index].Header = u'{} ({})'.format(cls_label(slot), AIRFLOW_UNIT)
        self._apply_filter()

    def _commit_edits(self):
        """Finish pending checkbox edits so filters and results see them."""
        try:
            for drv in self.table.DefaultView:
                if drv.IsEdit:
                    drv.EndEdit()
        except Exception:
            pass

    def _apply_filter(self, *args):
        self._commit_edits()
        text = (self.ui('search_tb').Text or '').strip()
        self.ui('search_hint').Visibility = (
            self._visibility(False) if text else self._visibility(True))
        parts = []
        for term in text.split():
            t = _like(term)
            parts.append(u"(Mark LIKE '%{0}%' OR Equipment LIKE '%{0}%' OR System LIKE '%{0}%' "
                         u"OR Level LIKE '%{0}%' OR Rooms LIKE '%{0}%' "
                         u"OR CONVERT(Id, 'System.String') LIKE '%{0}%')".format(t))
        if self._checked('only_served_cb'):
            # keep checked rows visible so nothing is hidden-but-selected
            parts.append('(Terminals > 0 OR Update = true)')
        try:
            self.table.DefaultView.RowFilter = ' AND '.join(parts)
        except Exception:
            self.table.DefaultView.RowFilter = ''
        self._update_counts()

    @staticmethod
    def _visibility(visible):
        import clr
        clr.AddReference('PresentationCore')
        from System.Windows import Visibility
        return Visibility.Visible if visible else Visibility.Collapsed

    def _set_rows(self, rows, value_fn):
        self._commit_edits()
        self._busy = True
        try:
            for row in rows:
                row['Update'] = value_fn(row)
        finally:
            self._busy = False
        self._update_counts()

    def _visible_rows(self):
        return [drv.Row for drv in self.table.DefaultView]

    def checked_ids(self):
        return set(int(r['Id']) for r in self.table.Rows if r['Update'])

    def _update_counts(self, *args):
        if self._busy:
            return
        n_checked = len(self.checked_ids())
        self.ui('sel_count_tb').Text = '{} of {} equipment checked for update'.format(
            n_checked, self.table.Rows.Count)
        self.ui('visible_count_tb').Text = '{} shown'.format(self.table.DefaultView.Count)
        run = self.ui('btn_run')
        run.IsEnabled = self.ctx['scanned']
        if not self.ctx['scanned']:
            run.Content = 'Scan first'
        else:
            run.Content = ('Run report' if self._checked('dryrun_cb')
                           else 'Update {} Equipment'.format(n_checked))
        self._update_status()

    # ---- step 2: mapping --------------------------------------------------------------
    def _init_mapping(self):
        params = self.ctx['params']
        n_types = self.ctx['type_count']
        saved = self.state.get('mapping', {})
        for key, (label, kind, default, _) in FIELDS.items():
            items = [NO_PARAM]
            for name, info in params.items():
                if info['type'] in COMPATIBLE[kind]:
                    disp = u'{}   [{} · {}/{} types]'.format(name, info['type'], info['types'], n_types)
                    self._param_display[disp] = name
                    items.append(disp)
            combo = self.ui('map_' + key)
            combo.ItemsSource = _net_list(items)
            wanted = saved.get(key, default) if key in saved else default
            combo.SelectedIndex = self._index_for(items, wanted)
            combo.SelectionChanged += self._on_mapping_changed

    def _index_for(self, items, pname):
        if not pname:
            return 0
        for i, disp in enumerate(items):
            if self._param_display.get(disp, '').lower() == pname.lower():
                return i
        return 0

    def mapping(self):
        out = OrderedDict()
        for key in FIELDS:
            disp = self.ui('map_' + key).SelectedItem
            out[key] = self._param_display.get(disp) if disp else None
        return out

    def _auto_match(self, sender, args):
        for key, (label, kind, default, _) in FIELDS.items():
            combo = self.ui('map_' + key)
            items = list(combo.ItemsSource)
            idx = self._index_for(items, default) or self._index_for(items, label)
            if idx:
                combo.SelectedIndex = idx
        self._update_status()

    def _clear_mapping(self, sender, args):
        for key in FIELDS:
            self.ui('map_' + key).SelectedIndex = 0
        self._update_status()

    def _on_mapping_changed(self, sender, args):
        self._update_status()
        self._update_preview()

    # ---- step 3: options ----------------------------------------------------------------
    def _init_options(self):
        o = self.state.get('options', {})
        self.ui('cascade_cb').IsChecked = o.get('cascade', CASCADE_UPSTREAM)
        self.ui('clear_cb').IsChecked = o.get('clear', CLEAR_UNSERVED_EQUIPMENT)
        self.ui('dryrun_cb').IsChecked = o.get('dry_run', False)
        self.ui('csv_cb').IsChecked = o.get('csv', False)
        self.ui('detail_cb').IsChecked = o.get('detail', REPORT_EQUIPMENT_DETAIL)
        for name, items, value in (('airflow_unit_cb', AIRFLOW_UNITS, o.get('airflow_unit', AIRFLOW_UNIT)),
                                   ('area_unit_cb', AREA_UNITS, o.get('area_unit', AREA_UNIT)),
                                   ('sep_cb', list(SEPARATORS), o.get('separator_label', list(SEPARATORS)[0])),
                                   ('room_format_cb', list(ROOM_LIST_FORMATS) + [CUSTOM_FORMAT_LABEL],
                                    o.get('room_format_label', list(ROOM_LIST_FORMATS)[0]))):
            cb = self.ui(name)
            cb.ItemsSource = _net_list(items)
            cb.SelectedIndex = items.index(value) if value in items else 0
        self.ui('room_format_tb').Text = o.get('room_format_custom', u'{number} - {name}')
        self._apply_units()
        self._update_format_ui()

    def options(self):
        return {
            'cascade': self._checked('cascade_cb'),
            'clear': self._checked('clear_cb'),
            'dry_run': self._checked('dryrun_cb'),
            'csv': self._checked('csv_cb'),
            'detail': self._checked('detail_cb'),
            'airflow_unit': self.ui('airflow_unit_cb').SelectedItem or AIRFLOW_UNIT,
            'area_unit': self.ui('area_unit_cb').SelectedItem or AREA_UNIT,
            'separator_label': self.ui('sep_cb').SelectedItem or list(SEPARATORS)[0],
            'room_format_label': self.ui('room_format_cb').SelectedItem or list(ROOM_LIST_FORMATS)[0],
            'room_format_custom': self.ui('room_format_tb').Text or '',
        }

    def _apply_units(self):
        apply_options_to_globals(self.options())

    def _on_units_changed(self, sender, args):
        self._apply_units()
        self._update_format_ui()
        self._refresh_preview_columns()
        self._update_preview()

    def _update_format_ui(self):
        """Enable the custom template box and show a live example."""
        custom = self.ui('room_format_cb').SelectedItem == CUSTOM_FORMAT_LABEL
        self.ui('room_format_tb').IsEnabled = custom
        self.ui('room_format_tb').Opacity = 1.0 if custom else 0.5
        sample = None
        drv = self.ui('equip_grid').SelectedItem if hasattr(self, 'table') else None
        if drv is not None:
            ed = self._eq_data().get(int(drv.Row['Id']))
            if ed and ed['rooms']:
                sample = sorted_rooms(ed)[0]
        if sample is None:
            sample = {'number': '101', 'name': 'Gate Lounge', 'level': 'L01',
                      'department': 'Airside', 'area': 85.0 / 0.09290304, 'occupancy': '120'}
        example = format_room(ROOM_LIST_FORMAT, sample)
        if custom and not _ROOM_TOKEN_RE.search(self.ui('room_format_tb').Text or ''):
            example = u'⚠ add at least one token, e.g. {number} - {name}'
        self.ui('room_format_example_tb').Text = u'Example:  {}'.format(example)

    def _on_cascade_changed(self, sender, args):
        self._refresh_preview_columns()
        self._update_preview()

    # ---- status / preview ------------------------------------------------------------------
    def _update_status(self, *args):
        if self._busy:
            return
        mapped = [p for p in self.mapping().values() if p]
        dupes = set(p for p in mapped if mapped.count(p) > 1)
        if not self.ctx['scanned']:
            msg = u'Step 1: choose a scan scope and click Scan.'
        elif self._scope_changed():
            msg = u'⚠ Scan scope changed - click Rescan to refresh the equipment list.'
        elif self._checked('dryrun_cb'):
            msg = 'Report only: the model will not be modified.'
        elif dupes:
            msg = u'⚠ The same parameter is mapped twice: {}'.format(', '.join(sorted(dupes)))
        elif not mapped:
            msg = u'⚠ Map at least one parameter in step 2.'
        else:
            msg = u'{} parameter(s) will be written to each checked equipment.'.format(len(mapped))
        self.ui('status_tb').Text = msg

    def _update_preview(self, *args):
        self._update_format_ui()
        grid = self.ui('equip_grid')
        drv = grid.SelectedItem
        if drv is None:
            self.ui('preview_tb').Text = 'Click a row in the equipment list to preview the values.'
            return
        ed = self._eq_data().get(int(drv.Row['Id']))
        if not self.ctx['scanned']:
            self.ui('preview_tb').Text = 'Scan first (step 1) to preview values.'
            return
        if ed is None:
            self.ui('preview_tb').Text = u'No scanned {} are traced to this equipment.'.format(
                PROFILE['terms'].lower())
            return
        values = equipment_values(ed)
        lines = []
        for key, pname in self.mapping().items():
            kind, value = values[key]
            shown = value if kind == 'text' else _as_text(kind, value)
            target = pname or '(not written)'
            lines.append(u'{}\n  → {}: {}'.format(FIELDS[key][0], target, shown if shown != '' else '-'))
        self.ui('preview_tb').Text = u'\n'.join(lines)

    # ---- events -----------------------------------------------------------------------
    def _wire_events(self):
        self.ui('search_tb').TextChanged += self._apply_filter
        self.ui('only_served_cb').Checked += self._apply_filter
        self.ui('only_served_cb').Unchecked += self._apply_filter
        self.ui('cascade_cb').Checked += self._on_cascade_changed
        self.ui('cascade_cb').Unchecked += self._on_cascade_changed
        self.ui('dryrun_cb').Checked += self._update_counts
        self.ui('dryrun_cb').Unchecked += self._update_counts
        for name in ('airflow_unit_cb', 'area_unit_cb', 'sep_cb', 'room_format_cb'):
            self.ui(name).SelectionChanged += self._on_units_changed
        self.ui('room_format_tb').TextChanged += self._on_units_changed
        self.table.ColumnChanged += self._update_counts
        self.ui('equip_grid').SelectionChanged += self._update_preview
        self.ui('btn_all').Click += lambda s, a: self._set_rows(self._visible_rows(), lambda r: True)
        self.ui('btn_none').Click += lambda s, a: self._set_rows(list(self.table.Rows), lambda r: False)
        self.ui('btn_invert').Click += lambda s, a: self._set_rows(self._visible_rows(), lambda r: not r['Update'])
        self.ui('btn_check_hl').Click += self._check_highlighted
        self.ui('btn_from_sel').Click += self._from_revit_selection
        self.ui('btn_pick').Click += lambda s, a: self._close_with('pick')
        self.ui('btn_automatch').Click += self._auto_match
        self.ui('btn_clearmap').Click += self._clear_mapping
        self.ui('btn_cancel').Click += lambda s, a: self._close_with(None)
        self.ui('btn_back').Click += lambda s, a: self._close_with('back')
        self.ui('btn_run').Click += self._run
        for name in ('scope_all_rb', 'scope_view_rb', 'scope_levels_rb'):
            self.ui(name).Checked += self._update_scope_ui
        self.ui('complete_cb').Checked += self._update_scope_ui
        self.ui('complete_cb').Unchecked += self._update_scope_ui
        self.levels_table.ColumnChanged += self._update_scope_ui
        self.ui('btn_levels_all').Click += lambda s, a: self._set_levels(True)
        self.ui('btn_levels_none').Click += lambda s, a: self._set_levels(False)
        self.ui('btn_scan').Click += self._scan

    def _check_highlighted(self, sender, args):
        rows = [item.Row for item in self.ui('equip_grid').SelectedItems]
        self._set_rows(rows, lambda r: True)

    def _from_revit_selection(self, sender, args):
        ids = self.ctx['selection_equipment']()
        if not ids:
            forms.alert(u'Nothing usable is selected in Revit.\n\nSelect {} and/or {} first, '
                        u'or use "Pick in model...".'.format(PROFILE['equip'], PROFILE['terms']))
            return
        self._set_rows([r for r in self.table.Rows if int(r['Id']) in ids], lambda r: True)
        self._apply_filter()   # checked rows always stay visible

    def _run(self, sender, args):
        self._commit_edits()
        if not self.ctx['scanned']:
            forms.alert('Choose a scan scope and click Scan first.')
            return
        if self._scope_changed() and not forms.alert(
                'The scan scope was changed but not re-scanned.\n\n'
                'Continue with the results of the previous scan?', yes=True, no=True):
            return
        checked = self.checked_ids()
        mapping = self.mapping()
        mapped = [p for p in mapping.values() if p]
        if not checked:
            forms.alert('Check at least one equipment in step 2.')
            return
        if not self._checked('dryrun_cb'):
            if not mapped:
                forms.alert('Choose at least one target parameter in step 3, '
                            'or tick "Report only".')
                return
            dupes = sorted(set(p for p in mapped if mapped.count(p) > 1))
            if dupes:
                forms.alert(u'These parameters are mapped to more than one field:\n\n{}'
                            .format('\n'.join(dupes)))
                return
            if not forms.alert(u'Write {} parameter(s) to {} {}?'
                               .format(len(mapped), len(checked), PROFILE['equip']), yes=True, no=True):
                return
        self._close_with('run')

    def _close_with(self, action):
        self._commit_edits()
        self._commit_level_edits()
        self.action = action
        self.state['checked'] = self.checked_ids()
        self.state['mapping'] = dict(self.mapping())
        self.state['options'] = self.options()
        self.state['search'] = self.ui('search_tb').Text or ''
        self.state['only_served'] = self._checked('only_served_cb')
        self.state['scope'] = self.scope()
        self.state['levels'] = self.selected_levels()
        self.state['complete'] = self._checked('complete_cb')
        self.Close()

    # ---- step 1: scan scope ---------------------------------------------------------
    def _init_scope(self):
        import clr
        clr.AddReference('System.Data')
        import System
        from System.Data import DataTable
        view_rb = self.ui('scope_view_rb')
        if self.ctx['view_ok']:
            view_rb.Content = u'Active view:  {}'.format(self.ctx['view_name'])
        else:
            view_rb.Content = u'Active view (not a plan / section / 3D view)'
            view_rb.IsEnabled = False
        scope = self.state.get('scope', 'all')
        if scope == 'view' and not self.ctx['view_ok']:
            scope = 'all'
        self.ui({'all': 'scope_all_rb', 'view': 'scope_view_rb',
                 'levels': 'scope_levels_rb'}.get(scope, 'scope_all_rb')).IsChecked = True
        self.ui('complete_cb').IsChecked = self.state.get('complete', True)

        tbl = DataTable('levels')
        for name, t in (('Sel', System.Boolean), ('Id', System.Int64), ('Label', System.String)):
            tbl.Columns.Add(name, clr.GetClrType(t))
        chosen = self.state.get('levels', set())
        for lvl in self.ctx['levels']:
            row = tbl.NewRow()
            row['Sel'] = lvl['id'] in chosen
            row['Id'] = lvl['id']
            row['Label'] = lvl['label']
            tbl.Rows.Add(row)
        self.levels_table = tbl
        self.ui('levels_list').ItemsSource = tbl.DefaultView
        self.ui('scan_info_tb').Text = self.ctx['scan_info']
        self.ui('btn_scan').Content = 'Rescan' if self.ctx['scanned'] else 'Scan'
        self._update_scope_ui()

    def scope(self):
        if self._checked('scope_view_rb'):
            return 'view'
        if self._checked('scope_levels_rb'):
            return 'levels'
        return 'all'

    def selected_levels(self):
        # reads the proposed value while a checkbox edit is pending, so it is
        # safe to call from the table's ColumnChanged event
        return set(int(r['Id']) for r in self.levels_table.Rows if r['Sel'])

    def _commit_level_edits(self):
        try:
            for drv in self.levels_table.DefaultView:
                if drv.IsEdit:
                    drv.EndEdit()
        except Exception:
            pass

    def _set_levels(self, value):
        for r in self.levels_table.Rows:
            r['Sel'] = value
        self._update_scope_ui()

    def current_scan_params(self):
        return normalize_scan_params(self.scope(), self.selected_levels(),
                                     self._checked('complete_cb'))

    def _scope_changed(self):
        params = self.ctx.get('scan_params')
        return params is not None and self.current_scan_params() != params

    def _update_scope_ui(self, *args):
        levels_on = self._checked('scope_levels_rb')
        self.ui('levels_panel').IsEnabled = levels_on
        self.ui('levels_panel').Opacity = 1.0 if levels_on else 0.45
        self.ui('complete_cb').IsEnabled = not self._checked('scope_all_rb')
        hint = ''
        if levels_on and not self.selected_levels():
            hint = 'Tick at least one level.'
        elif self._scope_changed():
            hint = 'Scope changed - click Rescan to apply.'
        elif (self.ctx['scanned'] and not self._checked('scope_all_rb')
              and not self._checked('complete_cb')):
            hint = ('Complete totals is off: equipment serving other floors will only '
                    'receive the rooms and {} inside this scope.'.format(PROFILE['qty'].lower()))
        self.ui('scope_hint_tb').Text = hint
        if hasattr(self, 'table'):
            self._update_status()

    def _scan(self, sender, args):
        if self._checked('scope_levels_rb') and not self.selected_levels():
            forms.alert('Tick at least one level to scan.')
            return
        self._close_with('scan')


def apply_options_to_globals(opts):
    """Units / separator are module-level so formatting helpers pick them up."""
    global AIRFLOW_UNIT, AREA_UNIT, LIST_SEPARATOR, ROOM_LIST_FORMAT
    unit = opts.get('airflow_unit')
    AIRFLOW_UNIT = unit if unit in PROFILE['units'] else PROFILE['unit']
    AREA_UNIT = opts.get('area_unit') or AREA_UNIT
    LIST_SEPARATOR = SEPARATORS.get(opts.get('separator_label'), LIST_SEPARATOR)
    label = opts.get('room_format_label')
    if label == CUSTOM_FORMAT_LABEL:
        ROOM_LIST_FORMAT = (opts.get('room_format_custom') or '').strip() or ROOM_LIST_FORMAT
    else:
        ROOM_LIST_FORMAT = ROOM_LIST_FORMATS.get(label, ROOM_LIST_FORMAT)


def pick_equipment(uidoc, equip_cats):
    """Let the user pick equipment in the model; returns a set of ids."""
    from Autodesk.Revit.UI.Selection import ObjectType
    from Autodesk.Revit.Exceptions import OperationCanceledException
    sel_filter = None
    try:
        from Autodesk.Revit.UI.Selection import ISelectionFilter

        class EquipmentFilter(ISelectionFilter):
            __namespace__ = 'RoomsToEquipment'   # required by the CPython engine

            def AllowElement(self, el):
                return cat_id(el) in equip_cats

            def AllowReference(self, ref, pt):
                return False

        sel_filter = EquipmentFilter()
    except Exception:
        sel_filter = None
    msg = u'Pick {} to update, then click Finish (Esc to cancel)'.format(PROFILE['equip'])
    try:
        if sel_filter is not None:
            refs = uidoc.Selection.PickObjects(ObjectType.Element, sel_filter, msg)
        else:
            refs = uidoc.Selection.PickObjects(ObjectType.Element, msg)
    except OperationCanceledException:
        return set()
    doc = uidoc.Document
    ids = set()
    for r in refs:
        el = doc.GetElement(r.ElementId)
        if el is not None and cat_id(el) in equip_cats:
            ids.add(eid_int(el.Id))
    return ids


# =============================================================================
# SCANNING - scope: entire model / active view / selected levels
# =============================================================================

UNASSIGNED_LEVEL = -1
_LEVEL_PARAMS = ('FAMILY_LEVEL_PARAM', 'INSTANCE_REFERENCE_LEVEL_PARAM',
                 'INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM', 'SCHEDULE_LEVEL_PARAM')
_SCAN_VIEW_TYPES = ('FloorPlan', 'CeilingPlan', 'EngineeringPlan', 'AreaPlan',
                    'ThreeD', 'Section', 'Elevation', 'Detail')


def valid_eid(eid):
    try:
        return eid is not None and eid_int(eid) > 0
    except Exception:
        return False


def collect_levels(doc):
    levels = list(DB.FilteredElementCollector(doc).OfClass(DB.Level)
                  .WhereElementIsNotElementType().ToElements())
    levels.sort(key=lambda l: l.ProjectElevation)
    return [{'id': eid_int(l.Id), 'name': elem_name(l), 'elev': l.ProjectElevation}
            for l in levels]


def element_level_id(el, levels):
    """Level of an element: LevelId -> level parameters -> elevation fallback."""
    try:
        if valid_eid(el.LevelId):
            return eid_int(el.LevelId)
    except Exception:
        pass
    for name in _LEVEL_PARAMS:
        b = bip(name)
        if b is None:
            continue
        try:
            p = el.get_Parameter(b)
            if p is not None and p.StorageType == DB.StorageType.ElementId:
                v = p.AsElementId()
                if valid_eid(v):
                    return eid_int(v)
        except Exception:
            pass
    z = None                                  # highest level at or below the element
    try:
        z = el.Location.Point.Z
    except Exception:
        try:
            z = el.get_BoundingBox(None).Min.Z
        except Exception:
            return UNASSIGNED_LEVEL
    best = UNASSIGNED_LEVEL
    for lvl in levels:
        if lvl['elev'] <= z + 0.01:
            best = lvl['id']
    return best


def view_supports_scan(view):
    try:
        if view is None or view.IsTemplate:
            return False
        return any(view.ViewType == getattr(DB.ViewType, n, None) for n in _SCAN_VIEW_TYPES)
    except Exception:
        return False


def ids_in_view(doc, view, cat_names):
    out = set()
    for name in cat_names:
        b = getattr(DB.BuiltInCategory, name, None)
        if b is None:
            continue
        for eid in (DB.FilteredElementCollector(doc, view.Id).OfCategory(b)
                    .WhereElementIsNotElementType().ToElementIds()):
            out.add(eid_int(eid))
    return out


def credited_equipment(rec):
    return list(rec['direct']) + list(rec['upstream'].keys())


class Scanner(object):
    """Owns the tracer, room resolver and a per-terminal record cache.

    Records are computed once per terminal per session, so re-scanning a
    different scope only processes terminals that were not seen before.
    """

    def __init__(self, doc, logger):
        self.doc = doc
        self.logger = logger
        self.equip_cats = cat_ids(EQUIPMENT_CATEGORIES)
        self.term_cats = cat_ids(TERMINAL_CATEGORIES)
        self.all_terminals = collect_instances(doc, TERMINAL_CATEGORIES)
        # categories that only count when really connected (e.g. Mechanical
        # Equipment in the electrical window: only the ones on a circuit)
        optional = cat_ids(PROFILE['connected_only'])
        if optional:
            self.all_terminals = [t for t in self.all_terminals
                                  if cat_id(t) not in optional or electrical_systems(t)]
        self.all_equipment = collect_instances(doc, EQUIPMENT_CATEGORIES)
        self.term_by_id = dict((eid_int(t.Id), t) for t in self.all_terminals)
        self.levels = collect_levels(doc)
        level_names = dict((l['id'], l['name']) for l in self.levels)

        self.term_level = dict((tid, element_level_id(t, self.levels))
                               for tid, t in self.term_by_id.items())
        self.equip_level = {}
        self.equip_rows = {}
        for e in self.all_equipment:
            i = eid_int(e.Id)
            lid = element_level_id(e, self.levels)
            self.equip_level[i] = lid
            self.equip_rows[i] = {'id': i, 'mark': param_text(e, 'ALL_MODEL_MARK'),
                                  'label': family_type_label(e),
                                  'system': (param_text(e, 'RBS_SYSTEM_NAME_PARAM')
                                             or param_text(e, 'RBS_ELEC_PANEL_NAME')),
                                  'level': level_names.get(lid, '')}
        self.level_term_counts = {}
        for lid in self.term_level.values():
            self.level_term_counts[lid] = self.level_term_counts.get(lid, 0) + 1

        self.phase = view_phase(doc, doc.ActiveView)
        self.links = collect_links(doc)
        self.resolver = RoomResolver(doc, self.phase, self.links)
        if PROFILE['domain'] == 'electrical':
            self.tracer = CircuitTracer(self.equip_cats, self.term_cats)
        else:
            self.tracer = NetworkTracer(self.equip_cats, self.term_cats)
        for e in self.all_equipment:
            self.tracer.equipment_elements[eid_int(e.Id)] = e
        self.records = {}

    # ---- one terminal ------------------------------------------------------------
    def record(self, term):
        tid = eid_int(term.Id)
        rec = self.records.get(tid)
        if rec is not None:
            return rec
        rec = {'id': tid, 'el': term, 'label': family_type_label(term),
               'mark': param_text(term, 'ALL_MODEL_MARK'), 'cls': 'Unknown',
               'flow': 0.0, 'flows': {}, 'room': None, 'room_note': '', 'direct': [],
               'upstream': OrderedDict(), 'status': 'OK', 'note': '',
               'ambiguous': False, 'level_id': self.term_level.get(tid, UNASSIGNED_LEVEL)}
        try:
            conns, rec['cls'], rec['flows'] = terminal_data(term)
            rec['flow'] = sum(rec['flows'].values())
            rec['room'], rec['room_note'] = self.resolver.resolve(term)
            tr = self.tracer.trace_terminal(term, rec['cls'], conns)
            rec.update({'status': tr['status'], 'note': tr['note'],
                        'direct': tr['direct'], 'upstream': tr['upstream'],
                        'ambiguous': tr['ambiguous']})
            if not rec['direct'] and USE_SYSTEM_FALLBACK:
                for c in conns:
                    system = c.MEPSystem if c.IsConnected else None
                    base = system.BaseEquipment if system is not None else None
                    if base is not None and cat_id(base) in self.equip_cats:
                        bid = eid_int(base.Id)
                        self.tracer.equipment_elements[bid] = base
                        rec['direct'] = [bid]
                        rec['status'] = 'SYSTEM_FALLBACK'
                        rec['note'] = (u'Not physically traced ({}); used system "{}" base equipment'
                                       .format(tr['status'], elem_name(system)))
                        break
        except Exception as ex:
            rec['status'] = 'ERROR'
            rec['note'] = u'{}'.format(ex)
            self.logger.error(u'Terminal {}: {}'.format(tid, ex))
        self.records[tid] = rec
        return rec

    # ---- scope ---------------------------------------------------------------------
    def scope_ids(self, scope, level_ids, view):
        """(terminal ids, equipment ids) inside the scope."""
        if scope == 'view':
            return (ids_in_view(self.doc, view, TERMINAL_CATEGORIES),
                    ids_in_view(self.doc, view, EQUIPMENT_CATEGORIES))
        if scope == 'levels':
            return (set(t for t, l in self.term_level.items() if l in level_ids),
                    set(e for e, l in self.equip_level.items() if l in level_ids))
        return set(self.term_by_id), set(self.equip_rows)

    def scope_label(self, scope, level_ids, view):
        if scope == 'view':
            return u'Active view "{}"'.format(elem_name(view))
        if scope == 'levels':
            names = [l['name'] for l in self.levels if l['id'] in level_ids]
            if UNASSIGNED_LEVEL in level_ids:
                names.append('(no level)')
            return u'Levels: {}'.format(', '.join(names))
        return 'Entire model'

    def scan(self, scope, level_ids, view, complete):
        """Scan a scope. Returns a result dict, or None if cancelled."""
        t0 = time.time()
        term_ids, equip_ids = self.scope_ids(scope, level_ids, view)
        ordered = [t for t in self.term_by_id if t in term_ids]     # stable model order
        todo = [t for t in ordered if t not in self.records]
        if todo:
            with forms.ProgressBar(title=u'Tracing {} '.format(PROFILE['terms'].lower())
                                   + '({value} of {max_value})', cancellable=True) as pb:
                for i, tid in enumerate(todo):
                    if pb.cancelled:
                        return None
                    if i % PROGRESS_STEP == 0:
                        pb.update_progress(i, len(todo))
                    self.record(self.term_by_id[tid])
        records = OrderedDict((t, self.records[t]) for t in ordered)

        added = 0
        if complete and scope != 'all':
            added = self._complete_systems(records)
            if added is None:
                return None

        credited = set()
        for tid in ordered:
            credited.update(credited_equipment(records[tid]))
        eq_direct = aggregate(records, self.tracer, include_upstream=False)
        eq_cascade = aggregate(records, self.tracer, include_upstream=True)
        untraced = len([t for t in ordered if not records[t]['direct']])
        return {
            'scope': scope, 'level_ids': set(level_ids), 'complete': complete,
            'label': self.scope_label(scope, level_ids, view),
            'records': records, 'in_scope': set(ordered), 'added': added,
            'untraced': untraced,
            'eq_direct': eq_direct, 'eq_cascade': eq_cascade,
            'scope_equipment': (equip_ids | credited) & set(self.tracer.equipment_elements),
            'elapsed': time.time() - t0,
        }

    def _complete_systems(self, records):
        """Add every terminal on the duct systems of the equipment found so far.

        Walks outwards from each serving equipment through the cached network
        components (supply, return and downstream boxes), so a roof AHU serving
        five floors gets all five floors even when only one floor is scanned.
        Returns the number of terminals added, or None if cancelled.
        """
        frontier = deque()
        for rec in list(records.values()):
            frontier.extend(credited_equipment(rec))
        seen_eq, added = set(), 0
        with forms.ProgressBar(title='Completing connected systems ({value} of {max_value})',
                               cancellable=True) as pb:
            while frontier:
                if pb.cancelled:
                    return None
                eq = frontier.popleft()
                if eq in seen_eq:
                    continue
                seen_eq.add(eq)
                # duct / pipe networks or circuits joined to this equipment
                equips, terms = self.tracer.neighbours(eq)
                for tid in terms:
                    if tid in records or tid not in self.term_by_id:
                        continue
                    rec = self.record(self.term_by_id[tid])
                    records[tid] = rec
                    added += 1
                    frontier.extend(e for e in credited_equipment(rec) if e not in seen_eq)
                    if added % PROGRESS_STEP == 0:
                        pb.update_progress(added, added + len(frontier) + 1)
                frontier.extend(e for e in equips if e not in seen_eq)
        return added

    def selection_equipment(self, uidoc):
        """Equipment selected in Revit + equipment serving selected (scanned) terminals."""
        ids = set()
        for eid in uidoc.Selection.GetElementIds():
            i = eid_int(eid)
            if i in self.tracer.equipment_elements:
                ids.add(i)
            rec = self.records.get(i)
            if rec:
                ids.update(credited_equipment(rec))
        return ids


# =============================================================================
# MAIN
# =============================================================================

def _state_key():
    """Each system window remembers its own mapping and options. The air
    window keeps the original key so settings saved before still load."""
    return 'ui_state' if PROFILE_KEY == 'air' else 'ui_state_' + PROFILE_KEY


def load_saved_state(levels):
    try:
        cfg = script.get_config()
        state = json.loads(cfg.get_option(_state_key(), '{}') or '{}')
    except Exception:
        state = {}
    names = set(state.get('level_names', []))
    state['levels'] = set(l['id'] for l in levels if l['name'] in names)
    if '(no level)' in names:
        state['levels'].add(UNASSIGNED_LEVEL)
    state.setdefault('scope', 'all')
    state.setdefault('complete', True)
    return state


def save_state(state, levels):
    try:
        by_id = dict((l['id'], l['name']) for l in levels)
        by_id[UNASSIGNED_LEVEL] = '(no level)'
        cfg = script.get_config()
        cfg.set_option(_state_key(), json.dumps({
            'mapping': state.get('mapping', {}),
            'options': state.get('options', {}),
            'only_served': state.get('only_served', True),
            'scope': state.get('scope', 'all'),
            'complete': state.get('complete', True),
            'level_names': [by_id[i] for i in state.get('levels', set()) if i in by_id]}))
        script.save_config()
    except Exception:
        pass


def normalize_scan_params(scope, level_ids, complete):
    """Comparable scan settings: levels only matter for the levels scope,
    complete totals only when the scope is not the entire model."""
    return (scope,
            frozenset(level_ids) if scope == 'levels' else frozenset(),
            bool(complete) if scope != 'all' else False)


def build_context(scanner, scan, state, params, type_count, view, view_ok, uidoc):
    """Everything the dialog needs for one opening."""
    terms = PROFILE['terms'].lower()
    abbr = PROFILE['abbr']
    if scan is None:
        eq_ids = set(scanner.equip_rows)
        summary = (u'{:,} {}  ·  {:,} equipment\nNot scanned yet'
                   .format(len(scanner.term_by_id), terms, len(scanner.equip_rows)))
        scan_info = 'Choose a scope and click Scan.'
    else:
        eq_ids = scan['scope_equipment'] | (state.get('checked', set()) & set(scanner.equip_rows))
        n_in = len(scan['in_scope'])
        summary = (u'{}\n{:,} {}  ·  {:,} traced  ·  {:,} untraced'
                   .format(scan['label'], n_in, terms, n_in - scan['untraced'], scan['untraced']))
        scan_info = u'Scanned {:,} {}'.format(n_in, terms)
        if scan['added']:
            scan_info += u' (+{:,} outside scope for complete totals)'.format(scan['added'])
        scan_info += u' in {:.1f} s'.format(scan['elapsed'])
    level_rows = []
    for lvl in scanner.levels:
        level_rows.append({'id': lvl['id'], 'label': u'{}  ·  {:+.2f} m  ·  {} {}'.format(
            lvl['name'], lvl['elev'] * 0.3048, scanner.level_term_counts.get(lvl['id'], 0), abbr)})
    if scanner.level_term_counts.get(UNASSIGNED_LEVEL):
        level_rows.append({'id': UNASSIGNED_LEVEL, 'label': u'(no level)  ·  {} {}'.format(
            scanner.level_term_counts[UNASSIGNED_LEVEL], abbr)})
    empty = OrderedDict()
    return {
        'scanned': scan is not None,
        'scan_params': (normalize_scan_params(scan['scope'], scan['level_ids'], scan['complete'])
                        if scan else None),
        'equipment': sorted([scanner.equip_rows[i] for i in eq_ids if i in scanner.equip_rows],
                            key=lambda x: natural_key(x['mark'] or x['label'])),
        'eq_direct': scan['eq_direct'] if scan else empty,
        'eq_cascade': scan['eq_cascade'] if scan else empty,
        'params': params, 'type_count': type_count,
        'selection_equipment': lambda: scanner.selection_equipment(uidoc),
        'summary': summary, 'scan_info': scan_info,
        'levels': level_rows,
        'view_name': elem_name(view) if view is not None else '',
        'view_ok': view_ok,
    }


# =============================================================================
# ROOM DATA BY LOCATION - each electrical fixture / device gets its own room
# =============================================================================
# Unlike the system windows this does not follow circuits: every element gets
# the Room / Space it physically sits in, written to its own parameters.

LOCATION_KEY = 'location'
LOCATION_ACCENT = '#5B3F9E'
LOCATION_CATEGORIES = OrderedDict([
    ('OST_ElectricalFixtures', 'Electrical Fixtures'),
    ('OST_LightingFixtures', 'Lighting Fixtures'),
    ('OST_LightingDevices', 'Lighting Devices'),
    ('OST_ElectricalEquipment', 'Electrical Equipment'),
    ('OST_DataDevices', 'Data Devices'),
    ('OST_CommunicationDevices', 'Communication Devices'),
    ('OST_FireAlarmDevices', 'Fire Alarm Devices'),
    ('OST_SecurityDevices', 'Security Devices'),
    ('OST_NurseCallDevices', 'Nurse Call Devices'),
    ('OST_TelephoneDevices', 'Telephone Devices'),
])
LOCATION_DEFAULT_CATEGORIES = ['OST_ElectricalFixtures']

# key: (label in dialog, default parameter name, description). All are text.
# No default parameter: the user picks the target from the parameters that
# exist on the elements (nothing is pre-selected, nothing is ever created).
# "Auto-match" still offers the label as a suggestion.
LOCATION_FIELDS = OrderedDict([
    ('number', ('Room Number', '', 'Number of the room / space the element is in')),
    ('name', ('Room Name', '', 'Name of the room / space')),
    ('room_list', ('Room Number + Name', '', 'Number and name in one parameter, e.g. "101 - Gate Lounge". '
                                             'Format is chosen in the options.')),
    ('department', ('Room Department', '', 'Department of the room')),
    ('level', ('Room Level', '', 'Level of the room')),
    ('source', ('Room Source', '', 'Where the room was found: Space, Room, or Linked Room (link name)')),
])
PREFER_CHOICES = OrderedDict([('Rooms first, then MEP Spaces', True),
                              ('MEP Spaces first, then Rooms', False)])
LOCATION_SCOPES = OrderedDict([('all', 'loc_scope_all'), ('view', 'loc_scope_view'),
                               ('selection', 'loc_scope_sel')])


def location_values(room, fmt=None):
    """{field key: text} for one element's room (all blank when room is None).
    fmt: Room Number + Name template (default: the one chosen in the options)."""
    if room is None:
        return dict((k, u'') for k in LOCATION_FIELDS)
    source = room['source']
    if room.get('doc_label') and room['doc_label'] != 'Host':
        source = u'{} ({})'.format(source, room['doc_label'])
    return {'number': room['number'] or u'', 'name': room['name'] or u'',
            'room_list': format_room(fmt or ROOM_LIST_FORMAT, room),
            'department': room['department'] or u'', 'level': room['level'] or u'',
            'source': source}


COMBINE_SEPARATOR = u' - '


def location_targets(mapping):
    """{field: parameter} -> OrderedDict {parameter: [fields]} - ONE value per parameter.

    Several fields may go to the same parameter; they are combined:
      Room Number + Room Name in one parameter -> the "Room Number + Name"
      format chosen in the options (e.g. "101 - Gate Lounge");
      any other combination -> the values joined with " - " in field order.
    """
    out = OrderedDict()
    for key in LOCATION_FIELDS:
        pname = mapping.get(key)
        if pname:
            out.setdefault(pname, []).append(key)
    for pname, keys in out.items():
        if 'number' in keys and 'name' in keys:
            first = min(keys.index('number'), keys.index('name'))
            rest = [k for k in keys if k not in ('number', 'name', 'room_list')]
            out[pname] = rest[:first] + ['room_list'] + rest[first:]
    return out


def location_param_value(values, keys):
    """Text written to one parameter from its field(s)."""
    return COMBINE_SEPARATOR.join(values[k] for k in keys if values.get(k))


def location_elements(doc, uidoc, cat_names, scope, view):
    """Elements of the ticked categories inside the chosen scope."""
    if scope == 'selection':
        wanted = cat_ids(cat_names)
        found = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
        return [e for e in found if e is not None and cat_id(e) in wanted]
    if scope == 'view' and view is not None:
        out = []
        for name in cat_names:
            b = getattr(DB.BuiltInCategory, name, None)
            if b is not None:
                out.extend(DB.FilteredElementCollector(doc, view.Id).OfCategory(b)
                           .WhereElementIsNotElementType().ToElements())
        return out
    return collect_instances(doc, cat_names)


def locate_rooms(doc, elements, prefer_rooms):
    """Room / Space of every element (read-only).

    Returns (results, resolver); a result is
    {'el', 'id', 'room' (info dict or None), 'how', 'status'}.
    """
    resolver = RoomResolver(doc, view_phase(doc, doc.ActiveView), collect_links(doc),
                            prefer_rooms=prefer_rooms, probe_facing=True)
    results = []
    for el in elements:
        try:
            room, how = resolver.resolve(el)
        except Exception as ex:
            room, how = None, u'{}'.format(ex)
        results.append({'el': el, 'id': eid_int(el.Id), 'room': room, 'how': how, 'status': ''})
    return results, resolver


def write_location(doc, results, mapping, only_empty, clear_missing, logger):
    """Write the mapped room values to each element (inside a Transaction).

    Returns (stats, {parameter name: elements missing it}).
    """
    stats = {'written': 0, 'unchanged': 0, 'no_room': 0, 'skipped': 0, 'problems': 0}
    missing = {}
    for r in results:
        el, room = r['el'], r['room']
        if room is None:
            stats['no_room'] += 1
            if not clear_missing:
                r['status'] = 'No room found - not changed'
                continue
        if doc.IsWorkshared:
            try:
                if (DB.WorksharingUtils.GetCheckoutStatus(doc, el.Id)
                        == DB.CheckoutStatus.OwnedByOtherUser):
                    r['status'] = 'SKIPPED - owned by another user'
                    stats['skipped'] += 1
                    continue
            except Exception:
                pass
        values = location_values(room)
        written, unchanged, kept, problems = 0, 0, 0, []
        for pname, keys in location_targets(mapping).items():
            p = el.LookupParameter(pname)
            if p is None:
                missing[pname] = missing.get(pname, 0) + 1
                problems.append(u'missing "{}"'.format(pname))
                continue
            try:
                if only_empty and room is not None and (p.AsString() or '').strip():
                    kept += 1
                    continue
                if write_param(p, 'text', location_param_value(values, keys)) == 'written':
                    written += 1
                else:
                    unchanged += 1
            except Exception as ex:
                problems.append(u'"{}": {}'.format(pname, ex))
                logger.warning(u'Element {} parameter "{}": {}'.format(r['id'], pname, ex))
        status = '{} written, {} unchanged'.format(written, unchanged)
        if kept:
            status += ', {} kept (already filled)'.format(kept)
        if room is None:
            status = 'No room found - cleared ({})'.format(status)
        if problems:
            status += ' | ' + '; '.join(problems)
            stats['problems'] += 1
        stats['written' if written else 'unchanged'] += 1
        r['status'] = status
    return stats, missing


def location_xaml():
    """location.xaml with the shared styles of ui.xaml and one mapping row per field."""
    folder = os.path.dirname(__file__)
    with codecs.open(os.path.join(folder, 'location.xaml'), 'r', 'utf-8') as f:
        xaml = f.read()
    with codecs.open(os.path.join(folder, 'ui.xaml'), 'r', 'utf-8') as f:
        found = re.search(r'<Window\.Resources>.*?</Window\.Resources>', f.read(), re.S)
    rows = []
    for key, (label, _, desc) in LOCATION_FIELDS.items():
        rows.append(
            u'<Grid Margin="0,0,0,7"><Grid.ColumnDefinitions>'
            u'<ColumnDefinition Width="140"/><ColumnDefinition Width="*"/>'
            u'</Grid.ColumnDefinitions>'
            u'<TextBlock Text="{label}" FontWeight="SemiBold" Foreground="#1F2D3D" '
            u'VerticalAlignment="Center" TextTrimming="CharacterEllipsis" ToolTip="{desc}"/>'
            u'<ComboBox Grid.Column="1" x:Name="map_{key}" IsTextSearchEnabled="True" ToolTip="{desc}"/>'
            u'</Grid>'.format(key=key, label=_xml(label), desc=_xml(desc)))
    return (xaml.replace('{{RESOURCES}}', found.group(0) if found else u'')
                .replace('{{MAPPING_ROWS}}', u'\n'.join(rows)))


class RoomByLocationWindow(forms.WPFWindow):
    """Step 1 elements, step 2 parameter mapping, step 3 options; result grid."""

    def __init__(self, doc, uidoc, state):
        forms.WPFWindow.__init__(self, location_xaml(), literal_string=True)
        self.doc, self.uidoc, self.state = doc, uidoc, state
        self.action = None
        self.results = None              # None = not searched yet
        self.resolver = None
        self._scan_key = None            # settings the results belong to
        self._param_display = {}
        self._loading = True
        self.view = doc.ActiveView
        self.view_ok = view_supports_scan(self.view)
        self._type_boxes = []            # family : type tick boxes
        self._excluded = set(state.get('excluded_types', []))   # unticked "Family: Type" labels
        self._hold = False               # True while Tick / Untick shown runs
        self._init_elements()
        self._init_options()
        self._build_table()
        self._reload_types()
        self._reload_params()
        self._wire_events()
        self._loading = False
        self._update_ui()

    def ui(self, name):
        return self.FindName(name)

    def _checked(self, name):
        return bool(self.ui(name).IsChecked)

    # ---- step 1 ---------------------------------------------------------------------
    def _init_elements(self):
        from System.Windows import Thickness
        from System.Windows.Controls import CheckBox
        saved = self.state.get('categories') or LOCATION_DEFAULT_CATEGORIES
        self._cat_boxes = []
        for name, label in LOCATION_CATEGORIES.items():
            if getattr(DB.BuiltInCategory, name, None) is None:
                continue
            box = CheckBox()
            box.Content = u'{}   ({:,})'.format(label, count_instances(self.doc, name))
            box.Tag = name
            box.IsChecked = name in saved
            box.Margin = Thickness(2, 1, 2, 1)
            self._cat_boxes.append(box)
            self.ui('cat_list').Items.Add(box)
        view_rb = self.ui('loc_scope_view')
        if self.view_ok:
            view_rb.Content = u'Active view:  {}'.format(elem_name(self.view))
        else:
            view_rb.Content = u'Active view (not a plan / section / 3D view)'
            view_rb.IsEnabled = False
        try:
            n_selected = self.uidoc.Selection.GetElementIds().Count
        except Exception:
            n_selected = 0
        sel_rb = self.ui('loc_scope_sel')
        sel_rb.Content = u'Current Revit selection  ({:,} selected)'.format(n_selected)
        sel_rb.IsEnabled = n_selected > 0
        scope = self.state.get('scope', 'all')
        if (scope == 'view' and not self.view_ok) or (scope == 'selection' and not n_selected):
            scope = 'all'
        self.ui(LOCATION_SCOPES.get(scope, 'loc_scope_all')).IsChecked = True
        labels = list(PREFER_CHOICES)
        self.ui('prefer_cb').ItemsSource = _net_list(labels)
        wanted = self.state.get('prefer_label')
        self.ui('prefer_cb').SelectedIndex = labels.index(wanted) if wanted in labels else 0

    def categories(self):
        return [box.Tag for box in self._cat_boxes if box.IsChecked]

    def scope(self):
        for key, name in LOCATION_SCOPES.items():
            if self._checked(name):
                return key
        return 'all'

    def prefer_rooms(self):
        return PREFER_CHOICES.get(self.ui('prefer_cb').SelectedItem, True)

    # ---- family : type filter ---------------------------------------------------------
    def _sync_excluded(self):
        """Remember which types are unticked. Types of categories that are not
        ticked right now keep their state, so it survives switching categories."""
        for box in self._type_boxes:
            if box.IsChecked:
                self._excluded.discard(box.Tag)
            else:
                self._excluded.add(box.Tag)

    def _reload_types(self):
        """List every Family : Type of the ticked categories with its count."""
        from System.Windows import Thickness
        from System.Windows.Controls import CheckBox
        self._sync_excluded()
        counts = {}
        for el in collect_instances(self.doc, self.categories()):
            label = family_type_label(el)
            counts[label] = counts.get(label, 0) + 1
        listbox = self.ui('type_list')
        listbox.Items.Clear()
        self._type_boxes = []
        from System.Windows.Controls import TextBlock
        for label in sorted(counts, key=natural_key):
            box = CheckBox()
            # a TextBlock, not a plain string: WPF would swallow the first "_"
            # of a family name as a keyboard-shortcut marker
            text = TextBlock()
            text.Text = u'{}   ({:,})'.format(label, counts[label])
            box.Content = text
            box.Tag = label
            box.IsChecked = label not in self._excluded
            box.Margin = Thickness(2, 1, 2, 1)
            box.Checked += self._on_type_changed
            box.Unchecked += self._on_type_changed
            self._type_boxes.append(box)
            listbox.Items.Add(box)
        self._filter_types()

    def _filter_types(self, *args):
        """The search box only narrows the LIST; it does not change the ticks."""
        text = (self.ui('type_search_tb').Text or '').strip().lower()
        self.ui('type_search_hint').Visibility = RoomsToEquipmentWindow._visibility(not text)
        words = text.split()
        for box in self._type_boxes:
            label = box.Tag.lower()
            box.Visibility = RoomsToEquipmentWindow._visibility(all(w in label for w in words))
        self._update_type_count()

    def _update_type_count(self):
        ticked = len([b for b in self._type_boxes if b.IsChecked])
        self.ui('type_count_tb').Text = '{} of {} types ticked'.format(ticked, len(self._type_boxes))

    def _set_shown_types(self, value):
        from System.Windows import Visibility
        self._hold = True
        try:
            for box in self._type_boxes:
                if box.Visibility == Visibility.Visible:
                    box.IsChecked = value
        finally:
            self._hold = False
        self._on_type_changed(None, None)

    def _on_type_changed(self, sender, args):
        if self._loading or self._hold:
            return
        self._update_type_count()
        self._update_ui()

    def unticked_types(self):
        return set(box.Tag for box in self._type_boxes if not box.IsChecked)

    def type_filter(self):
        """Set of ticked 'Family: Type' labels, or None when every type is ticked."""
        if not self.unticked_types():
            return None
        return set(box.Tag for box in self._type_boxes if box.IsChecked)

    def _current_key(self):
        return (tuple(self.categories()), self.scope(), self.prefer_rooms(),
                frozenset(self.unticked_types()))

    def _stale(self):
        return self.results is not None and self._scan_key != self._current_key()

    def _find(self, sender, args):
        """Read-only: find the room of every element in scope and list it."""
        from System.Windows.Input import Cursors
        cats = self.categories()
        if not cats:
            forms.alert('Tick at least one category.')
            return
        wanted = self.type_filter()
        if wanted is not None and not wanted:
            forms.alert('Tick at least one Family : Type.')
            return
        self.Cursor = Cursors.Wait
        t0 = time.time()
        try:
            elements = location_elements(self.doc, self.uidoc, cats, self.scope(), self.view)
            if wanted is not None:
                elements = [e for e in elements if family_type_label(e) in wanted]
            self.results, self.resolver = locate_rooms(self.doc, elements, self.prefer_rooms())
        finally:
            self.Cursor = Cursors.Arrow
        self._scan_key = self._current_key()
        self._fill_table()
        self.ui('find_info_tb').Text = u'{:,} element(s) checked in {:.1f} s'.format(
            len(self.results), time.time() - t0)
        self._update_ui()

    # ---- result grid ------------------------------------------------------------------
    def _build_table(self):
        import clr
        clr.AddReference('System.Data')
        import System
        from System.Data import DataTable
        tbl = DataTable('elements')
        for name, t in (('Id', System.Int64), ('Category', System.String), ('Element', System.String),
                        ('Mark', System.String), ('Number', System.String), ('Name', System.String),
                        ('Level', System.String), ('Source', System.String), ('HasRoom', System.Boolean),
                        ('Preview', System.String)):
            tbl.Columns.Add(name, clr.GetClrType(t))
        self.table = tbl
        self.ui('loc_grid').ItemsSource = tbl.DefaultView

    def _format(self):
        """Room Number + Name template currently chosen in the options."""
        return ROOM_LIST_FORMATS.get(self.ui('room_format_cb').SelectedItem, ROOM_LIST_FORMAT)

    def _preview_text(self, room, targets, fmt):
        """'Param = value; Param2 = value' - exactly what Write will put on the element."""
        if not targets:
            return u'(choose parameters in step 2)'
        if room is None:
            return u'(cleared)' if self._checked('clear_cb') else u'(not changed - no room)'
        values = location_values(room, fmt)
        return u';   '.join(u'{} = {}'.format(p, location_param_value(values, keys))
                            for p, keys in targets.items())

    def _refresh_preview(self, *args):
        """Re-calculate the "Will be written" column after a mapping / format change."""
        if self._loading or not self.results:
            return
        targets, fmt = location_targets(self.mapping()), self._format()
        by_id = dict((r['id'], r['room']) for r in self.results)
        for row in self.table.Rows:
            row['Preview'] = self._preview_text(by_id.get(int(row['Id'])), targets, fmt)

    def _fill_table(self):
        tbl = self.table
        targets, fmt = location_targets(self.mapping()), self._format()
        tbl.BeginLoadData()
        try:
            tbl.Rows.Clear()
            for r in self.results:
                el, room = r['el'], r['room']
                values = location_values(room, fmt)
                row = tbl.NewRow()
                row['Id'] = r['id']
                try:
                    row['Category'] = el.Category.Name
                except Exception:
                    row['Category'] = ''
                row['Element'] = family_type_label(el)
                row['Mark'] = param_text(el, 'ALL_MODEL_MARK')
                row['Number'] = values['number']
                row['Name'] = values['name']
                row['Level'] = values['level']
                row['Source'] = values['source'] if room is not None else u'— no room found'
                row['HasRoom'] = room is not None
                row['Preview'] = self._preview_text(room, targets, fmt)
                tbl.Rows.Add(row)
        finally:
            tbl.EndLoadData()
        self._apply_filter()

    def _apply_filter(self, *args):
        text = (self.ui('search_tb').Text or '').strip()
        self.ui('search_hint').Visibility = RoomsToEquipmentWindow._visibility(not text)
        parts = []
        for term in text.split():
            t = _like(term)
            parts.append(u"(Category LIKE '%{0}%' OR Element LIKE '%{0}%' OR Mark LIKE '%{0}%' "
                         u"OR Number LIKE '%{0}%' OR Name LIKE '%{0}%' OR Level LIKE '%{0}%' "
                         u"OR CONVERT(Id, 'System.String') LIKE '%{0}%')".format(t))
        if self._checked('only_missing_cb'):
            parts.append('HasRoom = false')
        try:
            self.table.DefaultView.RowFilter = ' AND '.join(parts)
        except Exception:
            self.table.DefaultView.RowFilter = ''
        self.ui('visible_count_tb').Text = '{:,} shown'.format(self.table.DefaultView.Count)

    # ---- step 2: mapping ----------------------------------------------------------------
    def _param_source(self):
        """Elements whose parameters fill the drop-downs: the SELECTED elements
        (of the ticked categories) when the scope is the Revit selection, else
        every element of the ticked categories (one per type)."""
        cats = self.categories()
        if self.scope() == 'selection':
            return location_elements(self.doc, self.uidoc, cats, 'selection', self.view), 'selected'
        return collect_instances(self.doc, cats), 'model'

    def _reload_params(self):
        """Writable Text parameters that exist on the elements (selection or model).
        Only parameters the elements already have are offered - none are created."""
        current = self.mapping() if self._param_display else None
        saved = self.state.get('mapping', {})
        elements, source = self._param_source()
        params, n_types = discover_parameters(elements)
        self._param_display = {}
        items = [NO_PARAM]
        for name, info in params.items():
            if info['type'] == 'Text':
                disp = u'{}   [{}/{} {}types]'.format(name, info['types'], n_types,
                                                     'selected ' if source == 'selected' else '')
                self._param_display[disp] = name
                items.append(disp)
        self._param_source_label = (u'{:,} selected element(s)'.format(len(elements)) if source == 'selected'
                                    else u'all elements of the ticked categories')
        was_loading, self._loading = self._loading, True
        try:
            for key, (_, default, _) in LOCATION_FIELDS.items():
                if current is not None:
                    wanted = current.get(key)
                else:
                    wanted = saved.get(key, default) if key in saved else default
                combo = self.ui('map_' + key)
                combo.ItemsSource = _net_list(items)
                combo.SelectedIndex = self._index_for(items, wanted)
        finally:
            self._loading = was_loading

    def _index_for(self, items, pname):
        if not pname:
            return 0
        for i, disp in enumerate(items):
            if self._param_display.get(disp, '').lower() == pname.lower():
                return i
        return 0

    def mapping(self):
        out = OrderedDict()
        for key in LOCATION_FIELDS:
            disp = self.ui('map_' + key).SelectedItem
            out[key] = self._param_display.get(disp) if disp else None
        return out

    def _auto_match(self, sender, args):
        for key, (label, default, _) in LOCATION_FIELDS.items():
            combo = self.ui('map_' + key)
            items = list(combo.ItemsSource)
            idx = self._index_for(items, default) or self._index_for(items, label)
            if idx:
                combo.SelectedIndex = idx
        self._update_ui()

    def _clear_mapping(self, sender, args):
        for key in LOCATION_FIELDS:
            self.ui('map_' + key).SelectedIndex = 0
        self._update_ui()

    # ---- step 3: options -----------------------------------------------------------------
    def _init_options(self):
        o = self.state.get('options', {})
        self.ui('only_empty_cb').IsChecked = o.get('only_empty', False)
        self.ui('clear_cb').IsChecked = o.get('clear', False)
        self.ui('dryrun_cb').IsChecked = o.get('dry_run', False)
        labels = list(ROOM_LIST_FORMATS)
        self.ui('room_format_cb').ItemsSource = _net_list(labels)
        wanted = o.get('room_format_label')
        self.ui('room_format_cb').SelectedIndex = labels.index(wanted) if wanted in labels else 0

    def options(self):
        return {'only_empty': self._checked('only_empty_cb'), 'clear': self._checked('clear_cb'),
                'dry_run': self._checked('dryrun_cb'),
                'room_format_label': self.ui('room_format_cb').SelectedItem or list(ROOM_LIST_FORMATS)[0]}

    # ---- status ----------------------------------------------------------------------------
    def _update_ui(self, *args):
        if self._loading:
            return
        targets = location_targets(self.mapping())
        mapped = list(targets)
        combined = [(p, k) for p, k in targets.items() if len(k) > 1]
        run = self.ui('btn_run')
        self.ui('btn_find').Content = 'Find rooms' if self.results is None else 'Find again'
        if self.results is None:
            self.ui('count_tb').Text = 'Click "Find rooms" to list the elements and their rooms.'
            self.ui('summary_tb').Text = 'Not searched yet'
            msg = 'Step 1: tick the categories, choose a scope and click Find rooms.'
            run.IsEnabled = False
            run.Content = 'Find rooms first'
        else:
            total = len(self.results)
            with_room = len([r for r in self.results if r['room'] is not None])
            self.ui('count_tb').Text = u'{:,} of {:,} element(s) are inside a room'.format(with_room, total)
            self.ui('summary_tb').Text = u'{:,} elements  ·  {:,} with a room  ·  {:,} without'.format(
                total, with_room, total - with_room)
            run.IsEnabled = total > 0
            run.Content = ('Run report' if self._checked('dryrun_cb')
                           else u'Write room data to {:,} element(s)'.format(
                               total if self._checked('clear_cb') else with_room))
            if self._stale():
                msg = u'⚠ Categories, types, scope or room source changed - click Find again.'
            elif self._checked('dryrun_cb'):
                msg = 'Report only: the model will not be modified.'
            elif not mapped:
                if len(self._param_display) == 0:
                    msg = (u'⚠ The {} have no writable Text parameter - add one to the family / '
                           u'project first (this tool never creates parameters).'.format(
                               getattr(self, '_param_source_label', u'elements')))
                else:
                    msg = u'⚠ Choose the target parameters in step 2 (listed from {}).'.format(
                        getattr(self, '_param_source_label', u'the elements'))
            else:
                msg = u'{} parameter(s) will be written to each element - see "Will be written".'.format(
                    len(mapped))
                if combined:
                    msg += u'  Combined: ' + u', '.join(
                        u'{} ← {}'.format(p, u' + '.join(LOCATION_FIELDS[k][0] if k != 'room_list'
                                                         else u'Room Number + Name' for k in keys))
                        for p, keys in combined)
        self.ui('status_tb').Text = msg

    def _on_categories_changed(self, sender, args):
        if self._loading:
            return
        self._reload_types()
        self._reload_params()
        self._update_ui()

    def _on_scope_changed(self, sender, args):
        """Switching to / from 'Current Revit selection' changes which elements
        the parameter drop-downs are read from."""
        if self._loading:
            return
        self._reload_params()
        self._update_ui()

    # ---- events ----------------------------------------------------------------------------
    def _wire_events(self):
        for box in self._cat_boxes:
            box.Checked += self._on_categories_changed
            box.Unchecked += self._on_categories_changed
        for name in LOCATION_SCOPES.values():
            self.ui(name).Checked += self._on_scope_changed
        self.ui('prefer_cb').SelectionChanged += self._update_ui
        for key in LOCATION_FIELDS:
            self.ui('map_' + key).SelectionChanged += self._update_ui
            self.ui('map_' + key).SelectionChanged += self._refresh_preview
        self.ui('room_format_cb').SelectionChanged += self._refresh_preview
        for name in ('clear_cb', 'dryrun_cb'):
            self.ui(name).Checked += self._update_ui
            self.ui(name).Unchecked += self._update_ui
        self.ui('clear_cb').Checked += self._refresh_preview
        self.ui('clear_cb').Unchecked += self._refresh_preview
        self.ui('search_tb').TextChanged += self._apply_filter
        self.ui('only_missing_cb').Checked += self._apply_filter
        self.ui('only_missing_cb').Unchecked += self._apply_filter
        self.ui('type_search_tb').TextChanged += self._filter_types
        self.ui('btn_types_all').Click += lambda s, a: self._set_shown_types(True)
        self.ui('btn_types_none').Click += lambda s, a: self._set_shown_types(False)
        self.ui('btn_find').Click += self._find
        self.ui('btn_automatch').Click += self._auto_match
        self.ui('btn_clearmap').Click += self._clear_mapping
        self.ui('btn_back').Click += lambda s, a: self._close_with('back')
        self.ui('btn_cancel').Click += lambda s, a: self._close_with(None)
        self.ui('btn_run').Click += self._run

    def _run(self, sender, args):
        if not self.results:
            forms.alert('Click "Find rooms" first.')
            return
        if self._stale() and not forms.alert(
                'Categories, scope or room source were changed after the last search.\n\n'
                'Continue with the elements of the previous search?', yes=True, no=True):
            return
        if not self._checked('dryrun_cb'):
            mapped = list(location_targets(self.mapping()))   # one entry per parameter
            if not mapped:
                forms.alert('Choose at least one target parameter in step 2, or tick "Report only".')
                return
            n = len(self.results) if self._checked('clear_cb') else len(
                [r for r in self.results if r['room'] is not None])
            if not forms.alert(u'Write {} parameter(s) to {:,} element(s)?'.format(len(mapped), n),
                               yes=True, no=True):
                return
        self._close_with('run')

    def _close_with(self, action):
        self.action = action
        self.state['categories'] = self.categories()
        self._sync_excluded()
        self.state['excluded_types'] = sorted(self._excluded)
        self.state['scope'] = self.scope()
        self.state['prefer_label'] = self.ui('prefer_cb').SelectedItem
        self.state['mapping'] = dict(self.mapping())
        self.state['options'] = self.options()
        self.Close()


def run_location(doc, uidoc, output, logger):
    """The 'Room Data by Location' window. Returns 'back' for the system picker."""
    global ROOM_LIST_FORMAT
    cfg_key = 'ui_state_' + LOCATION_KEY
    try:
        state = json.loads(script.get_config().get_option(cfg_key, '{}') or '{}')
    except Exception:
        state = {}
    win = RoomByLocationWindow(doc, uidoc, state)
    win.ShowDialog()
    try:
        script.get_config().set_option(cfg_key, json.dumps(state))
        script.save_config()
    except Exception:
        pass
    if win.action == 'back':
        return 'back'
    if win.action != 'run' or not win.results:
        return None

    opts = state['options']
    ROOM_LIST_FORMAT = ROOM_LIST_FORMATS.get(opts.get('room_format_label'), ROOM_LIST_FORMAT)
    mapping = OrderedDict((k, state['mapping'][k]) for k in LOCATION_FIELDS if state['mapping'].get(k))
    results = win.results
    stats, missing = None, {}
    if not opts['dry_run']:
        with revit.Transaction('Room Data by Location: write rooms to elements'):
            stats, missing = write_location(doc, results, mapping, opts['only_empty'],
                                            opts['clear'], logger)
    print_location_report(output, doc, results, mapping, opts, stats, missing, win.resolver)
    return None


def print_location_report(output, doc, results, mapping, opts, stats, missing, resolver):
    output.set_title('Room Data by Location')
    with_room = [r for r in results if r['room'] is not None]
    no_room = [r for r in results if r['room'] is None]
    output.print_md('# Room Data by Location - Electrical')
    output.print_md(
        u'**Model:** {}  \n**Mode:** {}  \n'
        u'**Room sources:** Spaces={} | Host rooms={} | Linked models with rooms={}  \n'
        u'**Order:** {}'
        .format(doc.Title, 'Report only' if opts['dry_run'] else 'Write parameters',
                resolver.use_spaces, resolver.use_rooms, len(resolver.links),
                'Rooms first' if resolver.prefer_rooms else 'MEP Spaces first'))

    output.print_md('## Parameter mapping')
    safetable.print_table(output, [[LOCATION_FIELDS[k][0], v] for k, v in mapping.items()]
                       or [['-', 'nothing mapped']], columns=['Data', 'Element parameter'])

    rows = [['Elements checked', len(results)], ['Inside a room / space', len(with_room)],
            ['No room found', len(no_room)]]
    if stats is not None:
        rows += [['Elements updated', stats['written']],
                 ['Elements already correct (unchanged)', stats['unchanged']],
                 ['Skipped - owned by another user', stats['skipped']],
                 ['Elements with a parameter problem', stats['problems']]]
    output.print_md('## Summary')
    safetable.print_table(output, rows, columns=['Item', 'Count'])

    if missing:
        output.print_md('## Parameters missing on some elements')
        safetable.print_table(output, [[k, v] for k, v in missing.items()],
                           columns=['Parameter', 'Elements missing it'])

    if no_room:
        output.print_md('## Elements without a room')
        output.print_md('*Usual causes: the element is outside every room boundary (corridor '
                        'ceiling void, shaft, external wall), the room is not placed / not '
                        'enclosed, or the architectural link is unloaded.*')
        table = [[_link(output, r['el'], r['id']), family_type_label(r['el']),
                  param_text(r['el'], 'ALL_MODEL_MARK'), r['how'], r['status']] for r in no_room]
        safetable.print_table(output, _cap(table, output, 'elements'),
                           columns=['ID', 'Element', 'Mark', 'Reason', 'Result'])

    by_room = OrderedDict()
    for r in with_room:
        room = r['room']
        entry = by_room.setdefault(room['dedupe_key'], [room, 0])
        entry[1] += 1
    output.print_md('## Elements per room')
    table = [[room['number'], room['name'], room['level'], location_values(room)['source'], n]
             for room, n in sorted(by_room.values(), key=lambda e: natural_key(e[0]['number']))]
    safetable.print_table(output, _cap(table, output, 'rooms'),
                       columns=['Room number', 'Room name', 'Level', 'Found in', 'Elements'])

    problems = [r for r in with_room if '|' in r['status'] or r['status'].startswith('SKIPPED')]
    if problems:
        output.print_md('## Elements not fully written')
        table = [[_link(output, r['el'], r['id']), family_type_label(r['el']),
                  r['room']['number'], r['status']] for r in problems]
        safetable.print_table(output, _cap(table, output, 'elements'),
                           columns=['ID', 'Element', 'Room', 'Result'])


# ---- system picker: one card per MEP system -----------------------------------

PICKER_XAML = u'''<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Rooms to Equipment" SizeToContent="WidthAndHeight" ResizeMode="NoResize"
        WindowStartupLocation="CenterScreen" Background="#F3F5F8"
        FontFamily="Segoe UI" FontSize="12" ShowInTaskbar="False">
    <Window.Resources>
        <Style x:Key="CardBtn" TargetType="Button">
            <Setter Property="Margin" Value="6"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="HorizontalContentAlignment" Value="Stretch"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border x:Name="bd" Background="White" BorderBrush="#DDE3EA" BorderThickness="1"
                                CornerRadius="6" Padding="0">
                            <ContentPresenter/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="bd" Property="BorderBrush" Value="#5B6B7C"/>
                                <Setter TargetName="bd" Property="Background" Value="#FAFBFC"/>
                            </Trigger>
                            <Trigger Property="IsEnabled" Value="False">
                                <Setter TargetName="bd" Property="Opacity" Value="0.5"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>
    </Window.Resources>
    <StackPanel>
        <Border Background="#1F2D3D" Padding="18,12">
            <StackPanel>
                <TextBlock Text="Rooms to Equipment" FontSize="18" FontWeight="SemiBold" Foreground="White"/>
                <TextBlock Text="Choose what to do. Each choice opens in its own window with its own saved settings."
                           Foreground="#BFD0E3" Margin="0,2,0,0"/>
            </StackPanel>
        </Border>
        <UniformGrid Columns="2" Margin="8,8,8,4">
{{CARDS}}
        </UniformGrid>
        <TextBlock Margin="16,0,16,12" Foreground="#6B7785" FontSize="11"
                   Text="A system is greyed out when the model has none of its elements. Esc closes this window."/>
    </StackPanel>
</Window>'''

PICKER_CARD = u'''            <Button x:Name="card_{key}" Style="{{StaticResource CardBtn}}" IsEnabled="{enabled}">
                <Grid Width="360" Height="92">
                    <Border Width="7" HorizontalAlignment="Left" Background="{accent}" CornerRadius="6,0,0,6"/>
                    <StackPanel Margin="22,0,12,0" VerticalAlignment="Center">
                        <TextBlock Text="{title}" FontSize="15" FontWeight="SemiBold" Foreground="#1F2D3D"/>
                        <TextBlock Text="{flow}" Foreground="#6B7785" Margin="0,2,0,0" TextTrimming="CharacterEllipsis"/>
                        <TextBlock Text="{counts}" Foreground="{accent}" FontWeight="SemiBold" Margin="0,7,0,0"/>
                    </StackPanel>
                </Grid>
            </Button>'''


def system_counts(doc):
    """{profile key: (terminal count, equipment count)} - counts only, fast."""
    out = OrderedDict()
    for key, p in PROFILES.items():
        optional = set(p['connected_only'])
        out[key] = (sum(count_instances(doc, c) for c in p['terminal_cats'] if c not in optional),
                    sum(count_instances(doc, c) for c in p['equipment_cats']))
    # room data by location: (electrical fixtures, the other electrical categories)
    out[LOCATION_KEY] = (count_instances(doc, 'OST_ElectricalFixtures'),
                         sum(count_instances(doc, c) for c in LOCATION_CATEGORIES
                             if c != 'OST_ElectricalFixtures'))
    return out


def picker_xaml(counts):
    cards = []
    for key, p in PROFILES.items():
        n_terms, n_equip = counts.get(key, (0, 0))
        cards.append(PICKER_CARD.format(
            key=key, accent=p['accent'], title=_xml(p['title']),
            enabled='True' if (n_terms and n_equip) else 'False',
            flow=_xml(u'{} → {}'.format(p['terms'], p['equip'])),
            counts=_xml(u'{:,} {}  ·  {:,} equipment'.format(n_terms, p['terms'].lower(), n_equip))))
    n_fixtures, n_other = counts.get(LOCATION_KEY, (0, 0))
    cards.append(PICKER_CARD.format(
        key=LOCATION_KEY, accent=LOCATION_ACCENT, title=_xml('Room Data by Location'),
        enabled='True' if (n_fixtures or n_other) else 'False',
        flow=_xml(u'Electrical fixtures / devices → the room each one sits in'),
        counts=_xml(u'{:,} electrical fixtures  ·  {:,} other electrical elements'
                    .format(n_fixtures, n_other))))
    return PICKER_XAML.replace('{{CARDS}}', u'\n'.join(cards))


class SystemPickerWindow(forms.WPFWindow):
    """First window: pick the MEP system to work on."""

    def __init__(self, counts):
        forms.WPFWindow.__init__(self, picker_xaml(counts), literal_string=True)
        self.choice = None
        for key in list(PROFILES) + [LOCATION_KEY]:
            self.FindName('card_' + key).Click += self._handler(key)
        self.KeyDown += self._on_key

    def _handler(self, key):
        def choose(sender, args):
            self.choice = key
            self.Close()
        return choose

    def _on_key(self, sender, args):
        from System.Windows.Input import Key
        if args.Key == Key.Escape:
            self.Close()


def main():
    doc = revit.doc
    uidoc = revit.uidoc
    output = script.get_output()
    logger = script.get_logger()

    if doc.IsFamilyDocument:
        forms.alert('Run this tool in a project model, not a family.', exitscript=True)

    # picker -> dedicated system window; "Systems" in that window comes back here
    while True:
        picker = SystemPickerWindow(system_counts(doc))
        picker.ShowDialog()
        if picker.choice is None:
            return
        if picker.choice == LOCATION_KEY:
            set_profile('electrical')          # parameter type labels of the electrical window
            result = run_location(doc, uidoc, output, logger)
        else:
            set_profile(picker.choice)
            result = run_system(doc, uidoc, output, logger)
        if result != 'back':
            return


def run_system(doc, uidoc, output, logger):
    """The dedicated window of the active profile: scan, select, map, write.
    Returns 'back' when the user asked for the system picker again."""
    scanner = Scanner(doc, logger)
    if not scanner.all_equipment:
        forms.alert(u'No {} found in the active model.'.format(PROFILE['equip']))
        return 'back'
    if not scanner.all_terminals:
        forms.alert(u'No connected {} found in the active model.'.format(PROFILE['terms']))
        return 'back'

    params, type_count = discover_parameters(scanner.all_equipment)
    view = doc.ActiveView
    view_ok = view_supports_scan(view)

    state = load_saved_state(scanner.levels)
    state['checked'] = scanner.selection_equipment(uidoc)
    if state['scope'] == 'view' and not view_ok:
        state['scope'] = 'all'

    # dialog loop: re-opened after "Scan" and "Pick in model"
    scan = None
    while True:
        ctx = build_context(scanner, scan, state, params, type_count, view, view_ok, uidoc)
        win = RoomsToEquipmentWindow(ctx, state)
        win.ShowDialog()
        if win.action == 'pick':
            state['checked'] |= pick_equipment(uidoc, scanner.equip_cats)
            continue
        if win.action == 'scan':
            result = scanner.scan(state['scope'], set(state['levels']), view, state['complete'])
            if result is None:
                forms.alert('Scan cancelled - the previous results are kept.')
            else:
                scan = result
                if not state['checked']:
                    state['checked'] = scanner.selection_equipment(uidoc)
            continue
        break
    save_state(state, scanner.levels)
    if win.action == 'back':
        return 'back'
    if win.action != 'run' or scan is None:
        return None

    opts = state['options']
    apply_options_to_globals(opts)
    eq_data = scan['eq_cascade'] if opts['cascade'] else scan['eq_direct']
    mapping = OrderedDict((k, v) for k, v in state['mapping'].items() if v)
    targets = state['checked']
    tracer = scanner.tracer
    t0 = time.time()

    # ---- write (single transaction) ---------------------------------------------
    missing_params = {}
    if not opts['dry_run']:
        to_write = [(ed['el'], equipment_values(ed)) for k, ed in eq_data.items() if k in targets]
        if opts['clear']:
            to_write += [(tracer.equipment_elements[k], empty_values())
                         for k in targets if k not in eq_data and k in tracer.equipment_elements]
        if to_write:
            with revit.Transaction(u'Rooms to Equipment ({}): write served spaces'
                                   .format(PROFILE['title'])):
                for el, values in to_write:
                    k = eid_int(el.Id)
                    try:
                        status = write_equipment(doc, el, values, mapping, missing_params, logger)
                    except Exception as ex:
                        status = u'ERROR: {}'.format(ex)
                        logger.error(u'Equipment {}: {}'.format(k, ex))
                    if k in eq_data:
                        eq_data[k]['write_status'] = status

    # ---- report -----------------------------------------------------------------------
    print_report(output, doc, scan, eq_data, targets, tracer, scanner.links,
                 missing_params, opts, mapping, scanner.phase, scanner.resolver,
                 scan['elapsed'] + (time.time() - t0))

    if opts['csv']:
        folder = forms.pick_folder()
        if folder:
            export_csv(folder, doc, scan, eq_data, targets)
            output.print_md(u'**CSV exported to:** `{}`'.format(folder))
    return None


# =============================================================================
# REPORT
# =============================================================================

def _link(output, el, fallback_id):
    try:
        return output.linkify(el.Id)
    except Exception:
        return '{}'.format(fallback_id)


def _cap(rows, output, what):
    if len(rows) > REPORT_MAX_ROWS:
        output.print_md('*Showing first {} of {} {} - export CSV for the full list.*'
                        .format(REPORT_MAX_ROWS, len(rows), what))
        return rows[:REPORT_MAX_ROWS]
    return rows


def print_report(output, doc, scan, eq_data, targets, tracer, links,
                 missing_params, opts, mapping, phase, resolver, elapsed):
    p = PROFILE
    terms, qty = p['terms'], p['qty']
    slots = list(p['flow_fields'])                 # totals this system has
    output.set_title(u'Rooms to Equipment - {}'.format(p['title']))
    term_records = scan['records']
    # problem lists cover the scan scope; totals also include completed systems
    recs = [r for t, r in term_records.items() if t in scan['in_scope']]
    traced = [r for r in recs if r['direct']]
    untraced = [r for r in recs if not r['direct']]
    no_room = [r for r in recs if r['room'] is None]
    fallback = [r for r in recs if r['status'] == 'SYSTEM_FALLBACK']
    ambiguous = [r for r in recs if r['ambiguous']]
    shared = [r for r in recs if len(r['direct']) > 1]
    in_scope = [ed for k, ed in eq_data.items() if k in targets]
    unserved = [k for k in targets if k not in eq_data]

    output.print_md(u'# Rooms to Equipment - {}'.format(p['title']))
    output.print_md(
        u'**Model:** {}  \n**System:** {} ({} → {})  \n**Scan scope:** {}  \n'
        u'**Complete totals:** {}  \n'
        u'**Mode:** {}  \n**Upstream cascade:** {}  \n**Phase used:** {}  \n'
        u'**Room sources:** Spaces={} | Host rooms={} | Linked models with rooms={}  \n'
        u'**Units:** {} {}, area {}  \n**Run time:** {:.1f} s'
        .format(doc.Title, p['title'], terms, p['equip'], scan['label'],
                ('On (+{} outside the scope)'.format(scan['added']) if scan['complete']
                 else u'Off - equipment totals include only {} inside the scope'.format(terms.lower())),
                'Report only' if opts['dry_run'] else 'Write parameters',
                'On' if opts['cascade'] else 'Off', elem_name(phase) if phase else 'n/a',
                resolver.use_spaces, resolver.use_rooms, len(resolver.links),
                qty.lower(), AIRFLOW_UNIT, AREA_UNIT, elapsed))

    output.print_md('## Parameter mapping')
    safetable.print_table(output, [[FIELDS[k][0], v] for k, v in mapping.items()] or [['-', 'nothing mapped']],
                       columns=['Data', 'Equipment parameter'])

    output.print_md('## Summary')
    safetable.print_table(output, [
        [u'{} in scan scope'.format(terms), len(recs)],
        [u'{} added outside scope (complete totals)'.format(terms), scan['added']],
        ['Traced to equipment', len(traced)],
        ['  of which via MEPSystem fallback', len(fallback)],
        ['Could not be traced', len(untraced)],
        ['Without Room/Space', len(no_room)],
        ['Direction ambiguous (check connector flow directions)', len(ambiguous)],
        ['Connected to more than one equipment (shared network)', len(shared)],
        ['Circuits read' if p['domain'] == 'electrical' else 'Network components walked',
         len(tracer.components)],
        [u'Checked equipment serving {}'.format(terms.lower()), len(in_scope)],
        [u'Checked equipment with no traced {}'.format(terms.lower()), len(unserved)],
    ], columns=['Item', 'Count'])

    if missing_params:
        output.print_md('## Parameters missing on some equipment')
        safetable.print_table(output, [[k, v] for k, v in missing_params.items()],
                           columns=['Parameter', 'Equipment missing it'])

    output.print_md(u'## {}'.format(p['equip']))
    ordered = sorted(in_scope, key=lambda e: natural_key(e['mark'] or e['label']))
    rows = []
    for ed in ordered:
        numbers = unique([r['number'] for r in sorted_rooms(ed)])
        rows.append([
            _link(output, ed['el'], ed['id']), ed['label'], ed['mark'], ed['system'],
            '{} / {}'.format(ed['direct_count'], ed['upstream_count']),
            len(ed['rooms']), join_limited(numbers, limit=120),
            fmt_area(sum(r['area'] for r in ed['rooms'].values()))]
            + [fmt_flow(ed['flow'].get(s, 0.0)) for s in slots]
            + [ed['no_room_terminals'], ed['write_status']])
    safetable.print_table(output, _cap(rows, output, 'equipment'), columns=[
        'ID', 'Equipment', 'Mark', 'System',
        u'{} direct / via downstream equip.'.format(p['terms_short']),
        'Spaces', 'Room Numbers', 'Area']
        + [cls_label(s) for s in slots]
        + [u'{} w/o room'.format(p['terms_short']), 'Write status'])

    if opts.get('detail'):
        output.print_md('## Equipment detail')
        for ed in ordered[:REPORT_MAX_ROWS]:
            output.print_md(u'### {} {} ({})'.format(ed['mark'], ed['label'], _link(output, ed['el'], ed['id'])))
            room_rows = []
            for r in sorted_rooms(ed):
                k = r['dedupe_key']
                rf = ed['room_flow'][k]
                room_rows.append([
                    r['number'], r['name'], r['department'], r['level'], fmt_area(r['area']),
                    r['occupancy'], u'{} ({})'.format(r['source'], r['doc_label']),
                    ed['room_terms'][k]]
                    + [fmt_flow(rf.get(s, 0.0)) for s in slots]
                    + ([fmt_flow(r['space_design_supply']) if r['space_design_supply'] is not None
                        else '-'] if p['domain'] == 'hvac' else []))
            if room_rows:
                safetable.print_table(output, room_rows, columns=[
                    'Number', 'Name', 'Department', 'Level', 'Area', 'Occupancy', 'Source',
                    p['terms_short']]
                    + [u'{} {}'.format(cls_label(s), qty.lower()) for s in slots]
                    + (['Space design supply'] if p['domain'] == 'hvac' else []))
            term_links = [_link(output, term_records[t]['el'], t) for t in list(ed['terminals'])[:200]]
            output.print_md(u'**Connected {} ({}):** {}{}'.format(
                terms, len(ed['terminals']), ', '.join(term_links),
                ' ...' if len(ed['terminals']) > 200 else ''))

    if untraced:
        output.print_md(u'## {} that could not be traced'.format(terms))
        rows = [[_link(output, r['el'], r['id']), r['label'], r['mark'], cls_label(r['cls']),
                 fmt_flow(r['flow']), r['room']['number'] if r['room'] else '-',
                 r['status'], r['note']] for r in untraced]
        safetable.print_table(output, _cap(rows, output, terms.lower()), columns=[
            'ID', p['term'], 'Mark', 'Class', qty, 'Room', 'Status', 'Reason'])

    if no_room:
        output.print_md(u'## {} without Room/Space'.format(terms))
        rows = [[_link(output, r['el'], r['id']), r['label'], r['mark'], cls_label(r['cls']),
                 ', '.join('{}'.format(d) for d in r['direct']) or '-', r['room_note']]
                for r in no_room]
        safetable.print_table(output, _cap(rows, output, terms.lower()), columns=[
            'ID', p['term'], 'Mark', 'Class', 'Equipment IDs', 'Reason'])

    if fallback or ambiguous or shared:
        output.print_md('## Warnings')
        rows = []
        for r in fallback:
            rows.append([_link(output, r['el'], r['id']), 'System fallback', r['note']])
        for r in ambiguous:
            rows.append([_link(output, r['el'], r['id']), 'Ambiguous direction',
                         'Bidirectional connectors used to pick equipment'])
        for r in shared:
            rows.append([_link(output, r['el'], r['id']), 'Shared network',
                         'Credited to equipment {}'.format(', '.join('{}'.format(d) for d in r['direct']))])
        safetable.print_table(output, _cap(rows, output, 'warnings'), columns=[p['term'], 'Warning', 'Detail'])

    if unserved:
        output.print_md(u'## Checked equipment with no traced {}'.format(terms.lower()))
        rows = []
        for k in sorted(unserved):
            el = tracer.equipment_elements.get(k)
            if el is not None:
                rows.append([_link(output, el, k), family_type_label(el), param_text(el, 'ALL_MODEL_MARK')])
        safetable.print_table(output, _cap(rows, output, 'equipment'), columns=['ID', 'Equipment', 'Mark'])

    if [l for l in links if l['terminals'] or not l['loaded']]:
        output.print_md('## Linked models')
        rows = [[l['name'], 'Loaded' if l['loaded'] else 'NOT LOADED', l['rooms'], l['terminals']]
                for l in links]
        safetable.print_table(output, rows, columns=['Link', 'Status', 'Rooms',
                                          u'{} (not traceable)'.format(terms)])
        output.print_md(u'*{} inside linked models cannot be connected to host '
                        u'equipment through the API (connections do not cross links). Run the '
                        u'tool inside the model that owns the {} network.*'.format(terms, p['net']))


def export_csv(folder, doc, scan, eq_data, targets):
    term_records = scan['records']
    slots = list(PROFILE['flow_fields'])
    short = PROFILE['terms_short']
    stamp = time.strftime('%Y%m%d_%H%M')
    base = u'{}_RoomsToEquipment_{}_{}'.format(doc.Title.replace(' ', '_'), PROFILE_KEY, stamp)

    eq_rows, room_rows = [], []
    for k, ed in eq_data.items():
        if k not in targets:
            continue
        rooms = sorted_rooms(ed)
        eq_rows.append([
            ed['id'], ed['label'], ed['mark'], ed['system'], len(ed['terminals']),
            ed['direct_count'], ed['upstream_count'], len(rooms),
            LIST_SEPARATOR.join(unique([r['number'] for r in rooms])),
            LIST_SEPARATOR.join(unique([r['name'] for r in rooms])),
            round(area_display(sum(r['area'] for r in rooms)), 3)]
            + [round(flow_display(ed['flow'].get(s, 0.0)), 3) for s in slots]
            + [ed['no_room_terminals'], ed['write_status'],
               LIST_SEPARATOR.join('{}'.format(t) for t in ed['terminals'])])
        for r in rooms:
            kk = r['dedupe_key']
            room_rows.append([ed['id'], ed['mark'], r['number'], r['name'], r['department'],
                              r['level'], round(area_display(r['area']), 3), r['occupancy'],
                              r['source'], r['doc_label'], ed['room_terms'][kk]]
                             + [round(flow_display(ed['room_flow'][kk].get(s, 0.0)), 3) for s in slots])

    flow_headers = [u'{} ({})'.format(cls_label(s), AIRFLOW_UNIT) for s in slots]
    write_csv(os.path.join(folder, base + '_equipment.csv'), [
        'Equipment ID', 'Equipment', 'Mark', 'System', short + ' total', short + ' direct',
        short + ' via downstream equipment', 'Spaces', 'Room Numbers', 'Room Names',
        'Area ({})'.format(AREA_UNIT)] + flow_headers + [
        short + ' without room', 'Write status', short + ' IDs'], eq_rows)

    write_csv(os.path.join(folder, base + '_rooms.csv'), [
        'Equipment ID', 'Equipment Mark', 'Room Number', 'Room Name', 'Department', 'Level',
        'Area ({})'.format(AREA_UNIT), 'Occupancy', 'Source', 'Source model', short] + flow_headers,
        room_rows)

    t_rows = []
    for r in term_records.values():
        room = r['room'] or {}
        t_rows.append([
            r['id'], r['label'], r['mark'], cls_label(r['cls']), round(flow_display(r['flow']), 3),
            room.get('number', ''), room.get('name', ''), room.get('department', ''),
            room.get('level', ''), room.get('source', ''), r['room_note'],
            LIST_SEPARATOR.join('{}'.format(d) for d in r['direct']),
            LIST_SEPARATOR.join('{}'.format(u) for u in r['upstream']),
            r['status'], r['note'], 'Yes' if r['id'] in scan['in_scope'] else 'No (complete totals)'])
    write_csv(os.path.join(folder, base + '_terminals.csv'), [
        'Element ID', PROFILE['term'], 'Mark', 'Class', '{} ({})'.format(PROFILE['qty'], AIRFLOW_UNIT),
        'Room Number', 'Room Name', 'Department', 'Level', 'Room source', 'Room lookup',
        'Serving equipment (direct)', 'Upstream equipment', 'Status', 'Note', 'In scan scope'], t_rows)


if __name__ == '__main__':
    main()

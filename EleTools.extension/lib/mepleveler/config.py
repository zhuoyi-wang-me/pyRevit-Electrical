# -*- coding: utf-8 -*-
"""Category catalogue and persisted user settings.

The category catalogue is intentionally declarative: adding support for a new
category is a one line change here, no code change anywhere else.

Settings are stored as JSON in ``%APPDATA%\\pyRevit\\MEPLevelFixer\\settings.json``
so they survive pyRevit reloads, Revit restarts and version upgrades.
"""

import os
import json
import codecs

from . import log
from .revit_compat import bic

# ---------------------------------------------------------------------------
# Processing scopes
# ---------------------------------------------------------------------------

SCOPE_SELECTION = 'selection'
SCOPE_VIEW = 'view'
SCOPE_MODEL = 'model'

SCOPE_LABELS = {
    SCOPE_SELECTION: 'Selected elements only',
    SCOPE_VIEW: 'Current view only',
    SCOPE_MODEL: 'Entire model',
}

# ---------------------------------------------------------------------------
# Level matching strategies
# ---------------------------------------------------------------------------

STRATEGY_NEAREST = 'nearest'        # smallest absolute distance
STRATEGY_BELOW = 'below'            # highest level at or below the element
STRATEGY_BELOW_SNAP = 'below_snap'  # as above, but snap up within a tolerance

STRATEGY_LABELS = {
    STRATEGY_NEAREST: 'Nearest level (smallest vertical distance)',
    STRATEGY_BELOW: 'Level at or below the element (storey logic)',
    STRATEGY_BELOW_SNAP: 'Level below, snapping up within tolerance',
}

# ---------------------------------------------------------------------------
# Elevation sources
# ---------------------------------------------------------------------------

ELEV_AUTO = 'auto'            # category aware: point -> curve midpoint -> bbox
ELEV_INSERTION = 'insertion'  # LocationPoint / curve start
ELEV_MIDPOINT = 'midpoint'    # curve midpoint
ELEV_BBOX = 'bbox'            # bounding box centre

ELEV_LABELS = {
    ELEV_AUTO: 'Automatic (category aware)',
    ELEV_INSERTION: 'Insertion point',
    ELEV_MIDPOINT: 'Element midpoint',
    ELEV_BBOX: 'Bounding box centre',
}

# ---------------------------------------------------------------------------
# Supported categories:  (discipline, display name, BuiltInCategory name)
# ---------------------------------------------------------------------------

CATEGORY_DEFS = [
    # ---- HVAC ------------------------------------------------------------
    ('HVAC', 'Ducts', 'OST_DuctCurves'),
    ('HVAC', 'Duct Fittings', 'OST_DuctFitting'),
    ('HVAC', 'Duct Accessories', 'OST_DuctAccessory'),
    ('HVAC', 'Flex Ducts', 'OST_FlexDuctCurves'),
    ('HVAC', 'Duct Insulations', 'OST_DuctInsulations'),
    ('HVAC', 'Air Terminals', 'OST_DuctTerminal'),
    ('HVAC', 'Mechanical Equipment', 'OST_MechanicalEquipment'),
    ('HVAC', 'Mechanical Control Devices', 'OST_MechanicalControlDevices'),

    # ---- Plumbing --------------------------------------------------------
    ('Plumbing', 'Pipes', 'OST_PipeCurves'),
    ('Plumbing', 'Pipe Fittings', 'OST_PipeFitting'),
    ('Plumbing', 'Pipe Accessories', 'OST_PipeAccessory'),
    ('Plumbing', 'Flex Pipes', 'OST_FlexPipeCurves'),
    ('Plumbing', 'Pipe Insulations', 'OST_PipeInsulations'),
    ('Plumbing', 'Plumbing Fixtures', 'OST_PlumbingFixtures'),
    ('Plumbing', 'Plumbing Equipment', 'OST_PlumbingEquipment'),
    ('Plumbing', 'Sprinklers', 'OST_Sprinklers'),

    # ---- Electrical ------------------------------------------------------
    ('Electrical', 'Conduits', 'OST_Conduit'),
    ('Electrical', 'Conduit Fittings', 'OST_ConduitFitting'),
    ('Electrical', 'Cable Trays', 'OST_CableTray'),
    ('Electrical', 'Cable Tray Fittings', 'OST_CableTrayFitting'),
    ('Electrical', 'Electrical Equipment', 'OST_ElectricalEquipment'),
    ('Electrical', 'Electrical Fixtures', 'OST_ElectricalFixtures'),
    ('Electrical', 'Lighting Fixtures', 'OST_LightingFixtures'),
    ('Electrical', 'Lighting Devices', 'OST_LightingDevices'),
    ('Electrical', 'Data Devices', 'OST_DataDevices'),
    ('Electrical', 'Communication Devices', 'OST_CommunicationDevices'),
    ('Electrical', 'Fire Alarm Devices', 'OST_FireAlarmDevices'),
    ('Electrical', 'Nurse Call Devices', 'OST_NurseCallDevices'),
    ('Electrical', 'Security Devices', 'OST_SecurityDevices'),
    ('Electrical', 'Telephone Devices', 'OST_TelephoneDevices'),

    # ---- Other MEP -------------------------------------------------------
    ('Other MEP', 'Specialty Equipment', 'OST_SpecialityEquipment'),
    ('Other MEP', 'Generic Models', 'OST_GenericModel'),
]

#: Categories enabled the very first time the tool is launched.
DEFAULT_CATEGORIES = [
    'OST_DuctCurves', 'OST_DuctFitting', 'OST_DuctAccessory',
    'OST_FlexDuctCurves', 'OST_DuctTerminal', 'OST_MechanicalEquipment',
    'OST_PipeCurves', 'OST_PipeFitting', 'OST_PipeAccessory',
    'OST_PlumbingFixtures',
    'OST_Conduit', 'OST_ConduitFitting', 'OST_CableTray',
    'OST_CableTrayFitting', 'OST_ElectricalEquipment',
    'OST_ElectricalFixtures', 'OST_LightingFixtures', 'OST_LightingDevices',
    'OST_DataDevices', 'OST_CommunicationDevices', 'OST_FireAlarmDevices',
    'OST_NurseCallDevices', 'OST_SecurityDevices', 'OST_SpecialityEquipment',
]

#: Category specific elevation rules. Anything not listed uses ELEV_AUTO.
#: Rationale: linear MEP runs are judged by their midpoint so that a riser
#: crossing two levels is assigned to the level it mostly belongs to, while
#: face hosted devices are judged by their insertion point.
CATEGORY_ELEVATION_RULES = {
    'OST_DuctCurves': ELEV_MIDPOINT,
    'OST_FlexDuctCurves': ELEV_MIDPOINT,
    'OST_PipeCurves': ELEV_MIDPOINT,
    'OST_FlexPipeCurves': ELEV_MIDPOINT,
    'OST_Conduit': ELEV_MIDPOINT,
    'OST_CableTray': ELEV_MIDPOINT,
    'OST_DuctTerminal': ELEV_INSERTION,
    'OST_LightingFixtures': ELEV_INSERTION,
    'OST_LightingDevices': ELEV_INSERTION,
    'OST_ElectricalFixtures': ELEV_INSERTION,
    'OST_DataDevices': ELEV_INSERTION,
    'OST_CommunicationDevices': ELEV_INSERTION,
    'OST_FireAlarmDevices': ELEV_INSERTION,
    'OST_NurseCallDevices': ELEV_INSERTION,
    'OST_SecurityDevices': ELEV_INSERTION,
    'OST_TelephoneDevices': ELEV_INSERTION,
    'OST_MechanicalEquipment': ELEV_BBOX,
    'OST_ElectricalEquipment': ELEV_BBOX,
    'OST_PlumbingEquipment': ELEV_BBOX,
    'OST_SpecialityEquipment': ELEV_BBOX,
}


class CategoryDef(object):
    """A single supported category, resolved against the running Revit API."""

    def __init__(self, discipline, name, bic_name):
        self.discipline = discipline
        self.name = name
        self.bic_name = bic_name
        self.bic = bic(bic_name)

    @property
    def available(self):
        return self.bic is not None

    @property
    def elevation_rule(self):
        return CATEGORY_ELEVATION_RULES.get(self.bic_name, ELEV_AUTO)

    def __repr__(self):
        return '<CategoryDef %s>' % self.bic_name


def available_categories():
    """All catalogue entries that exist in the running Revit version."""
    return [c for c in (CategoryDef(*row) for row in CATEGORY_DEFS)
            if c.available]


def category_map():
    """Dict of BuiltInCategory name -> CategoryDef."""
    return dict((c.bic_name, c) for c in available_categories())


# ---------------------------------------------------------------------------
# Persisted settings
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    'scope': SCOPE_VIEW,
    'categories': list(DEFAULT_CATEGORIES),
    'strategy': STRATEGY_NEAREST,
    'elevation_source': ELEV_AUTO,

    # Movement verification tolerance, in decimal feet (internal units).
    # 0.0005 ft ~ 0.15 mm. Anything above this is treated as "the element moved"
    # and the change for that element is rolled back.
    'move_tolerance': 0.0005,

    # Snap distance used by STRATEGY_BELOW_SNAP, in decimal feet (1.0 ft ~ 300 mm).
    'snap_tolerance': 1.0,

    # Levels whose name contains one of these fragments are never used as a
    # target (case insensitive). Kept as a secondary filter; the primary
    # control is the per-model target level selection below.
    'excluded_level_keywords': [],

    # Target levels, remembered per model:  {model key: [level name, ...]}.
    # An empty or missing entry means "every level is a valid target", which
    # is what a brand new model starts with.
    'level_selection_by_model': {},

    'skip_grouped': True,
    'skip_design_option_elements': False,
    'skip_pinned': False,
    'skip_nested_components': True,
    'use_schedule_level_fallback': True,
    'verify_connected_elements': True,
    'batch_size': 250,
    'debug': False,
    'last_report_folder': '',
}


class Settings(object):
    """Simple JSON backed settings object with attribute access."""

    def __init__(self, data=None):
        self._data = dict(DEFAULT_SETTINGS)
        if data:
            self._data.update(data)

    # -- attribute access -------------------------------------------------
    def __getattr__(self, item):
        try:
            return self.__dict__['_data'][item]
        except KeyError:
            raise AttributeError(item)

    def __setattr__(self, key, value):
        if key.startswith('_'):
            object.__setattr__(self, key, value)
        else:
            self._data[key] = value

    def get(self, key, default=None):
        return self._data.get(key, default)

    def as_dict(self):
        return dict(self._data)

    # -- per model target levels -----------------------------------------
    @staticmethod
    def model_key(doc):
        """Key used to remember settings that only make sense per model.

        The document title is used rather than the full path so that a local
        copy, a detached copy and the central file share one entry.
        """
        try:
            return doc.Title or 'unknown'
        except Exception:
            return 'unknown'

    def levels_for(self, doc):
        """Target level names remembered for *doc* (empty list = all levels)."""
        store = self._data.get('level_selection_by_model') or {}
        return list(store.get(self.model_key(doc), []))

    def set_levels_for(self, doc, level_names):
        """Remember the target level names for *doc*."""
        store = self._data.get('level_selection_by_model')
        if not isinstance(store, dict):
            store = {}
        store[self.model_key(doc)] = list(level_names or [])
        # Keep the file small: only remember the 50 most recent models.
        if len(store) > 50:
            for key in list(store.keys())[:-50]:
                store.pop(key, None)
        self._data['level_selection_by_model'] = store

    # -- persistence ------------------------------------------------------
    @staticmethod
    def path():
        return os.path.join(log.data_dir(), 'settings.json')

    @classmethod
    def load(cls):
        try:
            path = cls.path()
            if os.path.isfile(path):
                with codecs.open(path, 'r', encoding='utf-8') as handle:
                    return cls(json.load(handle))
        except Exception:
            pass
        return cls()

    def save(self):
        try:
            with codecs.open(self.path(), 'w', encoding='utf-8') as handle:
                handle.write(json.dumps(self._data, indent=2,
                                        ensure_ascii=False))
            return True
        except Exception:
            return False

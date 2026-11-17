# -*- coding: utf-8 -*-
"""
elecplace.settings
==================
User-tunable options, persisted in the pyRevit user config file
(%APPDATA%\\pyRevit\\pyRevit_config.ini, section [elecplace_settings]).

Edit them by SHIFT+CLICKING the "Auto-Place Fixtures" button (config.py).
All distances are stored in millimetres and exposed in feet (Revit
internal units) through the *_ft properties.
"""
from pyrevit import script

from elecplace.compat import mm_to_ft

SECTION = "elecplace_settings"

# (key, default, label shown in the settings dialog, python type)
DEFAULTS = [
    ("corner_clearance_mm", 300.0, "Min. distance from wall ends / corners (mm)", float),
    ("opening_clearance_mm", 150.0, "Clearance beside doors & windows (mm)", float),
    ("duplicate_tolerance_mm", 150.0, "Duplicate detection radius (mm)", float),
    ("min_segment_length_mm", 600.0, "Ignore wall segments shorter than (mm)", float),
    ("min_piece_length_mm", 100.0, "Ignore usable wall pieces shorter than (mm)", float),
    ("default_height_mm", 450.0, "Default mounting height (mm)", float),
    ("max_height_mm", 6000.0, "Maximum accepted mounting height (mm)", float),
    ("avoid_openings", True, "Avoid door & window openings", bool),
    ("use_finish_boundary", True, "Use finish-face room boundary (else wall centre)", bool),
    ("include_room_base_offset", False, "Add Room 'Base Offset' to FFL", bool),
    ("allow_unhosted_fallback", False, "Allow level-based (non-hosted) families", bool),
    ("equipment_max_distance_mm", 1500.0, "Equipment rules: max. distance equipment-to-wall (mm)", float),
    ("equipment_spacing_mm", 300.0, "Equipment rules: spacing between fixtures of one equipment (mm)", float),
    ("assign_schedule_level", True, "Set Schedule Level of placed fixtures", bool),
    ("avoid_clashes", True, "Avoid clashes with MSFDs, accessories and equipment", bool),
    ("clash_include_mep_curves", False, "Also avoid ducts, pipes, cable trays and conduits", bool),
    ("clash_clearance_mm", 50.0, "Clear gap kept around each new fixture (mm)", float),
    ("clash_max_slide_mm", 1000.0, "Max. distance a fixture may slide to avoid a clash (mm)", float),
    ("clash_step_mm", 50.0, "Slide step when searching a clear position (mm)", float),
    ("fixture_default_size_mm", 100.0, "Fixture size if the family geometry cannot be read (mm)", float),
    ("export_csv", True, "Export CSV report", bool),
    ("write_comments", False, "Write tag / room / served unit into fixture 'Comments'", bool),
    ("comment_tag", "ElecAutoPlace", "Tag written to fixture 'Comments'", str),
]


def _coerce(raw, typ, default):
    """Config values may come back as strings - convert safely."""
    try:
        if typ is bool:
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        return typ(raw)
    except Exception:
        return default


class Settings(object):
    """Plain attribute bag with helpers for unit conversion."""

    def __init__(self):
        for key, default, _label, _typ in DEFAULTS:
            setattr(self, key, default)

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls):
        obj = cls()
        try:
            cfg = script.get_config(SECTION)
            for key, default, _label, typ in DEFAULTS:
                setattr(obj, key, _coerce(cfg.get_option(key, default), typ, default))
        except Exception:
            pass  # fall back to defaults - never block the tool on config
        return obj

    def save(self):
        cfg = script.get_config(SECTION)
        for key, _default, _label, _typ in DEFAULTS:
            cfg.set_option(key, getattr(self, key))
        script.save_config()

    def reset(self):
        self.__init__()

    # -- derived values in Revit internal units (feet) ---------------------
    @property
    def corner_clearance_ft(self):
        return mm_to_ft(self.corner_clearance_mm)

    @property
    def opening_clearance_ft(self):
        return mm_to_ft(self.opening_clearance_mm)

    @property
    def duplicate_tolerance_ft(self):
        return mm_to_ft(self.duplicate_tolerance_mm)

    @property
    def min_segment_length_ft(self):
        return mm_to_ft(self.min_segment_length_mm)

    @property
    def min_piece_length_ft(self):
        return mm_to_ft(self.min_piece_length_mm)

    @property
    def default_height_ft(self):
        return mm_to_ft(self.default_height_mm)

    @property
    def equipment_max_distance_ft(self):
        return mm_to_ft(self.equipment_max_distance_mm)

    @property
    def equipment_spacing_ft(self):
        return mm_to_ft(self.equipment_spacing_mm)

    @property
    def clash_clearance_ft(self):
        return mm_to_ft(self.clash_clearance_mm)

    @property
    def clash_max_slide_ft(self):
        return mm_to_ft(self.clash_max_slide_mm)

    @property
    def clash_step_ft(self):
        return mm_to_ft(max(self.clash_step_mm, 10.0))

    @property
    def fixture_default_size_ft(self):
        return mm_to_ft(self.fixture_default_size_mm)

    @property
    def max_height_ft(self):
        return mm_to_ft(self.max_height_mm)

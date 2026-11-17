# -*- coding: utf-8 -*-
"""
config.py
==========
Persists wizard settings (balancing rules, spare/space percentages,
window size, last-used panel selection filters) to a JSON file under
%APPDATA%\\pyRevit\\PanelScheduleManager\\settings.json, so the tool
"remembers" the user's preferred configuration between Revit sessions
(requirement: "Settings persistence").
"""

import copy
import os
import json

from panelmgr.logger import get_logger
from panelmgr.models import (
    BalancingSettings, SpareSpaceSettings, WaysBracketRow, PanelTypeConfigRow,
    SPARE_SPACE_MODE_WAYS_TABLE, DEFAULT_WAYS_TABLE, DEFAULT_TYPE_CONFIG_TABLE,
)

logger = get_logger(__name__)

_SETTINGS_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "pyRevit", "PanelScheduleManager"
)
_SETTINGS_FILE = os.path.join(_SETTINGS_DIR, "settings.json")
SETTINGS_VERSION = 2

_DEFAULTS = {
    "balancing": {
        "enabled": True,
        "max_imbalance_pct": 5.0,
        "respect_locked_circuits": True,
        "arrange_ryb_after_balance": True,
        # THIRTY-SIXTH CORRECTION: these two were missing from the
        # defaults, from the loader and from the writer, so the two
        # settings the Step 2 UI actually binds to
        # (PreserveExistingSchedule) and the spare-load balancing
        # switch were discarded on every save and reset to the model
        # defaults on every restart.
        # FORTY-SIXTH CORRECTION: was True, which switched balancing off
        # for every panel that already had a schedule - see
        # models.BalancingSettings.
        "preserve_existing_schedule": False,
        "balance_via_spare_loads": True,
    },
    "spare_space": {
        "spare_percentage": 10.0,
        "space_percentage": 10.0,
        "reserved_slots": 0,
        "auto_fill_remaining": True,
        # Same omission as above: the Step 3 diversity-factor control
        # and the flat per-pole spare load override never persisted.
        "spare_load_va_per_pole": 0.0,
        "spare_diversity_factor": 1.0,
        "mode": SPARE_SPACE_MODE_WAYS_TABLE,
        "ways_table": [[r.Ways, r.Spare, r.Space] for r in DEFAULT_WAYS_TABLE],
        "type_config_table": [[r.Prefix, r.SpareRating, r.Poles] for r in DEFAULT_TYPE_CONFIG_TABLE],
    },
    "window": {
        "width": 980,
        "height": 680,
    },
}


def load_settings():
    """Returns a dict merged from defaults + persisted overrides."""
    # `dict(_DEFAULTS)` is only a SHALLOW copy - the nested
    # "balancing"/"spare_space"/"window" dicts were handed straight
    # back to the caller, so anything that mutated the returned dict
    # (or the lists inside it) corrupted the module-level defaults for
    # the rest of the session. `_deep_merge` against an empty override
    # gives a genuine copy.
    # `_deep_merge` only ever copies branches that the OVERRIDE
    # mentions, so merging against {} still handed back the very same
    # nested dicts. copy.deepcopy is the only thing that actually
    # detaches them.
    data = copy.deepcopy(_DEFAULTS)
    if os.path.isfile(_SETTINGS_FILE):
        try:
            with open(_SETTINGS_FILE, "r") as f:
                persisted = json.load(f)
            data = _deep_merge(copy.deepcopy(_DEFAULTS), persisted)
            # A settings file written before version 2 holds
            # "preserve_existing_schedule": true only because that was
            # the old default, saved automatically after every run - not
            # because anyone chose it. Left in place it would keep
            # balancing switched off after the upgrade. Reset it once;
            # from version 2 on the saved value is the user's own.
            if int(persisted.get("version", 1) or 1) < SETTINGS_VERSION:
                data["balancing"]["preserve_existing_schedule"] = False
        except Exception:
            logger.exception("Failed to read settings.json - using defaults")
    return data


def save_settings(data):
    try:
        if not os.path.isdir(_SETTINGS_DIR):
            os.makedirs(_SETTINGS_DIR)
        with open(_SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        logger.exception("Failed to write settings.json")


def _deep_merge(base, override):
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def balancing_settings_from_dict(data):
    b = data.get("balancing", _DEFAULTS["balancing"])
    return BalancingSettings(
        enabled=b.get("enabled", True),
        max_imbalance_pct=b.get("max_imbalance_pct", 5.0),
        respect_locked_circuits=b.get("respect_locked_circuits", True),
        arrange_ryb_after_balance=b.get("arrange_ryb_after_balance", True),
        preserve_existing_schedule=b.get("preserve_existing_schedule", False),
        balance_via_spare_loads=b.get("balance_via_spare_loads", True),
    )


def spare_space_settings_from_dict(data):
    s = data.get("spare_space", _DEFAULTS["spare_space"])
    ways_table = [
        WaysBracketRow(row[0], row[1], row[2])
        for row in s.get("ways_table", _DEFAULTS["spare_space"]["ways_table"])
    ]
    type_config_table = [
        PanelTypeConfigRow(row[0], row[1], row[2])
        for row in s.get("type_config_table", _DEFAULTS["spare_space"]["type_config_table"])
    ]
    return SpareSpaceSettings(
        spare_percentage=s.get("spare_percentage", 10.0),
        space_percentage=s.get("space_percentage", 10.0),
        reserved_slots=s.get("reserved_slots", 0),
        auto_fill_remaining=s.get("auto_fill_remaining", True),
        spare_load_va_per_pole=s.get("spare_load_va_per_pole", 0.0),
        spare_diversity_factor=s.get("spare_diversity_factor", 1.0),
        mode=s.get("mode", SPARE_SPACE_MODE_WAYS_TABLE),
        ways_table=ways_table,
        type_config_table=type_config_table,
    )


def settings_to_dict(balancing_settings, spare_space_settings, window=None):
    data = {
        "version": SETTINGS_VERSION,
        "balancing": {
            "enabled": balancing_settings.enabled,
            "max_imbalance_pct": balancing_settings.max_imbalance_pct,
            "respect_locked_circuits": balancing_settings.respect_locked_circuits,
            "arrange_ryb_after_balance": balancing_settings.arrange_ryb_after_balance,
            "preserve_existing_schedule": balancing_settings.preserve_existing_schedule,
            "balance_via_spare_loads": balancing_settings.balance_via_spare_loads,
        },
        "spare_space": {
            "spare_percentage": spare_space_settings.spare_percentage,
            "space_percentage": spare_space_settings.space_percentage,
            "reserved_slots": spare_space_settings.reserved_slots,
            "auto_fill_remaining": spare_space_settings.auto_fill_remaining,
            "spare_load_va_per_pole": spare_space_settings.spare_load_va_per_pole,
            "spare_diversity_factor": spare_space_settings.spare_diversity_factor,
            "mode": spare_space_settings.mode,
            "ways_table": [
                [r.Ways, r.Spare, r.Space] for r in spare_space_settings.ways_table
            ],
            "type_config_table": [
                [r.Prefix, r.SpareRating, r.Poles]
                for r in spare_space_settings.type_config_table
            ],
        },
    }
    if window:
        data["window"] = window
    return data

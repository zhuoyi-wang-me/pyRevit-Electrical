# -*- coding: utf-8 -*-
"""
Settings management.

Resolution order (later wins, dictionaries are deep-merged, lists replaced):
    1. <extension>/config/default_settings.json      factory defaults (read-only)
    2. %APPDATA%/ElecAutoCircuit/settings_global.json last settings saved from any project
    3. %APPDATA%/ElecAutoCircuit/settings_<project>.json  project-specific settings

Only pure-Python modules are imported here so the planner tests can use it.
"""
from __future__ import division

import codecs
import copy
import hashlib
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
EXT_ROOT = os.path.normpath(os.path.join(_HERE, '..', '..'))
DEFAULTS_FILE = os.path.join(EXT_ROOT, 'config', 'default_settings.json')
APP_DIR = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'), 'ElecAutoCircuit')
GLOBAL_FILE = os.path.join(APP_DIR, 'settings_global.json')


# --------------------------------------------------------------------------- io
def ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path)
    return path


def read_json(path):
    with codecs.open(path, 'r', 'utf-8-sig') as fh:
        return json.loads(fh.read())


def write_json(path, data):
    ensure_dir(os.path.dirname(path))
    tmp = path + '.tmp'
    with codecs.open(tmp, 'w', 'utf-8') as fh:
        fh.write(json.dumps(data, indent=2, ensure_ascii=False))
    # atomic-ish replace so a crash never leaves a half written settings file
    if os.path.exists(path):
        os.remove(path)
    os.rename(tmp, path)


def deep_merge(base, override):
    """Return a new dict: `override` recursively merged into a copy of `base`."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# --------------------------------------------------------------------- settings
def load_defaults():
    return read_json(DEFAULTS_FILE)


def project_key(title, path=''):
    """Stable, filesystem-safe key for a Revit document."""
    safe = re.sub(r'[^A-Za-z0-9_\-]+', '_', title or 'untitled')[:60]
    digest = hashlib.md5((path or title or '').encode('utf-8')).hexdigest()[:8]
    return '{0}_{1}'.format(safe, digest)


def settings_path(key):
    return os.path.join(APP_DIR, 'settings_{0}.json'.format(key))


def load_settings(key=None, logger=None):
    settings = load_defaults()
    for path in ([settings_path(key)] if key else []) + [GLOBAL_FILE]:
        if os.path.exists(path):
            try:
                settings = deep_merge(settings, read_json(path))
                if logger:
                    logger.info('Settings loaded from %s', path)
                break
            except Exception as ex:  # corrupt file -> fall back to defaults, never crash
                if logger:
                    logger.warning('Ignoring unreadable settings file %s: %s', path, ex)
    return settings


def save_settings(settings, key=None):
    """Project file keeps the selected filled regions; the global (last used) file does
    not, because element ids of one project mean nothing in another."""
    global_copy = copy.deepcopy(settings)
    scope = global_copy.setdefault('scope', {})
    scope['mode'] = 'all'
    scope['selected_region_ids'] = []
    write_json(GLOBAL_FILE, global_copy)
    if key:
        write_json(settings_path(key), settings)


# ------------------------------------------------------------------- validation
def _number(errors, value, label, lo=None, hi=None):
    try:
        v = float(value)
    except (TypeError, ValueError):
        errors.append('{0} must be a number'.format(label))
        return None
    if lo is not None and v < lo:
        errors.append('{0} must be >= {1}'.format(label, lo))
    if hi is not None and v > hi:
        errors.append('{0} must be <= {1}'.format(label, hi))
    return v


def validate(settings, check_scope=True):
    """Return a list of human-readable problems (empty list == valid)."""
    errors = []
    scope = settings.get('scope', {})
    if check_scope and scope.get('mode') == 'selected_regions' and not scope.get('selected_region_ids'):
        errors.append('Scope is "selected filled regions only" but no filled region is selected - '
                      'pick at least one or choose "Whole model".')
    g = settings.get('general', {})
    _number(errors, g.get('min_fixtures_before_merge', 0), 'Minimum devices before merging', 0)
    _number(errors, g.get('max_merge_distance_m', 0), 'Max merge distance', 0)
    _number(errors, g.get('max_panel_utilization_pct', 100), 'Max panel utilization %', 1, 100)
    _number(errors, g.get('transaction_batch_size', 50), 'Transaction batch size', 1)

    rules = settings.get('circuit_rules', {})
    if not rules:
        errors.append('At least one circuit rule is required.')
    for name, rule in rules.items():
        n = _number(errors, rule.get('max_devices', 0), 'Rule "{0}" max devices'.format(name), 0)
        va = _number(errors, rule.get('max_load_va', 0), 'Rule "{0}" max load'.format(name), 0)
        _number(errors, rule.get('dedicated_above_va', 0), 'Rule "{0}" dedicated threshold'.format(name), 0)
        if not rule.get('dedicated') and n == 0 and va == 0:
            errors.append('Rule "{0}" has neither a device nor a load limit '
                          '(every device would land on one circuit).'.format(name))

    seen = set()
    for i, entry in enumerate(settings.get('classification_map', [])):
        cls = (entry.get('classification') or '').strip()
        row = 'Mapping row {0}'.format(i + 1)
        if not cls:
            errors.append('{0}: classification is empty.'.format(row))
            continue
        if cls.lower() in seen:
            errors.append('{0}: "{1}" is mapped more than once.'.format(row, cls))
        seen.add(cls.lower())
        if not (entry.get('panel_type') or '').strip():
            continue   # blank panel type = classification deliberately not circuited
        if entry.get('rule') not in rules:
            errors.append('{0} ("{1}"): rule "{2}" does not exist.'.format(row, cls, entry.get('rule')))
    return errors

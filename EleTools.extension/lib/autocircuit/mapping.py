# -*- coding: utf-8 -*-
"""
Load-classification mapping synchronised with the PROJECT.

The project's ElectricalLoadClassification elements are the source of truth:
every classification defined in the project gets exactly one mapping row.
    * rows the user already configured keep their panel type / rule
    * new project classifications get a suggestion (keyword rules in settings)
    * saved rows no longer in the project are kept but flagged in_project = False
    * wildcard rows (e.g. 'Lighting*') are kept as user patterns
A blank panel type means "do not circuit this classification".

Pure Python - unit-tested outside Revit.
"""
import fnmatch


def is_pattern(text):
    return any(ch in (text or '') for ch in '*?[')


def suggest(name, suggestions):
    low = (name or '').strip().lower()
    for s in suggestions or []:
        if fnmatch.fnmatch(low, (s.get('match') or '').strip().lower()):
            return (s.get('panel_type') or '').upper(), s.get('rule') or ''
    return '', ''


def sync_mapping(existing, project_names, suggestions):
    """Return (rows, added_names, missing_names)."""
    remaining = {}
    order = []
    for e in existing or []:
        key = (e.get('classification') or '').strip().lower()
        if key and key not in remaining:
            remaining[key] = e
            order.append(key)

    rows, added = [], []
    done = set()
    for name in project_names:
        key = name.strip().lower()
        # Revit allows classifications that differ only by case / spaces (e.g. "Power"
        # and "POWER "); one row per key, otherwise validate() rejects the duplicate
        # and Preview / Create can never run.
        if key in done:
            continue
        done.add(key)
        e = remaining.pop(key, None)
        if e is not None:
            rows.append({'classification': name, 'panel_type': (e.get('panel_type') or '').upper(),
                         'rule': e.get('rule') or '', 'in_project': True})
        else:
            panel_type, rule = suggest(name, suggestions)
            rows.append({'classification': name, 'panel_type': panel_type, 'rule': rule, 'in_project': True})
            added.append(name)

    missing = []
    for key in order:
        if key in remaining:
            e = remaining[key]
            rows.append({'classification': e.get('classification', ''), 'panel_type': (e.get('panel_type') or '').upper(),
                         'rule': e.get('rule') or '', 'in_project': False})
            if not is_pattern(e.get('classification')):
                missing.append(e.get('classification'))
    return rows, added, missing

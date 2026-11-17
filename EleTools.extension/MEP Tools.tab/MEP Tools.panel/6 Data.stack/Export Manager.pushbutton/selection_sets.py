# -*- coding: utf-8 -*-
"""Named 'Selection Set' storage for the pyRevit Export Manager.

Lets the user save the currently checked sheets (or views) under a name,
and reload that exact set of checks later -- independent of whatever
search filter happens to be active at the time.

Saved sets are stored as JSON files in a SelectionSets folder beside this
module. Each set remembers which mode (Sheets/Views) it was built for.
"""

import json
import os

import helpers


_sets_folder = None


def configure(sets_folder=None):
    global _sets_folder
    _sets_folder = sets_folder or os.path.join(os.path.dirname(__file__), "SelectionSets")


def _require_context():
    if _sets_folder is None:
        raise RuntimeError("Call selection_sets.configure() first.")


def _ensure_folder():
    _require_context()
    if not os.path.exists(_sets_folder):
        os.makedirs(_sets_folder)
    return _sets_folder


def _safe_name(name):
    result = str(name).strip()
    for character in '<>:"/\\|?*':
        result = result.replace(character, "-")
    return result.rstrip(". ")


def safe_name(name):
    """The name a set is actually stored under (illegal filename characters
    replaced).  ui.py shows/compares this, not the raw text the user typed."""
    return _safe_name(name or "")


def set_exists(name):
    """Case-insensitive, because the sets are files on Windows.

    FIX: the overwrite check compared the raw typed name, case-sensitively,
    against the stored names -- so saving 'my set' silently replaced
    'My Set', and 'A/B' silently replaced 'A-B'.
    """
    target = safe_name(name).lower()
    if not target:
        return False
    return any(existing.lower() == target for existing in available_set_names())


def _set_path(name):
    return os.path.join(_ensure_folder(), _safe_name(name) + ".json")


def available_set_names(mode=None):
    """List saved set names, optionally filtered to a given mode."""
    _ensure_folder()
    names = []
    for filename in os.listdir(_sets_folder):
        if not filename.lower().endswith(".json"):
            continue
        name = os.path.splitext(filename)[0]
        if mode is not None:
            try:
                with open(os.path.join(_sets_folder, filename), "r") as stream:
                    data = json.load(stream)
                if data.get("mode") != mode:
                    continue
            except Exception:
                continue
        names.append(name)
    return sorted(names, key=lambda value: value.lower())


def save_set(name, mode, items):
    """Save a named set from a list of ExportItem-like objects (each with
    .Element, .Number, .Name)."""
    if not name:
        raise ValueError("Enter a name for the selection set.")
    if not items:
        raise ValueError("No checked items to save -- check some rows first.")

    entries = []
    for item in items:
        element_id = None
        try:
            element_id = helpers.element_id_value(item.Element.Id)
        except Exception:
            pass
        entries.append({
            "id": element_id,
            "number": getattr(item, "Number", ""),
            "name": getattr(item, "Name", "")
        })

    payload = {"mode": mode, "items": entries}
    with open(_set_path(name), "w") as stream:
        json.dump(payload, stream, indent=2)


def load_set(name):
    """Return {"mode": ..., "items": [{"id", "number", "name"}, ...]}."""
    if not name:
        raise ValueError("Choose a saved selection set first.")
    with open(_set_path(name), "r") as stream:
        return json.load(stream)


def delete_set(name):
    path = _set_path(name)
    if os.path.exists(path):
        os.remove(path)


def match_items(saved_entries, candidate_items, mode="Sheets"):
    """Match saved entries against the live item list for the current mode.

    Tries element id first (most reliable within the same model), then
    falls back to Number, then Name, in case ids shifted between sessions.
    Returns (matched_items, missing_count).

    FIX: in Views mode "Number" is the view TYPE ("FloorPlan"), which is
    shared by hundreds of views, so the Number fallback used to tick the
    wrong (first) floor plan whenever an id had changed.  Number is now
    only used as a key in Sheets mode, where it is unique.  Duplicate hits
    are also de-duplicated.
    """
    by_id = {}
    by_number = {}
    by_name = {}
    for item in candidate_items:
        key = None
        try:
            key = helpers.element_id_value(item.Element.Id)
        except Exception:
            pass
        if key is not None:
            by_id[key] = item
        if mode == "Sheets" and getattr(item, "Number", ""):
            by_number.setdefault(item.Number, item)
        if getattr(item, "Name", ""):
            by_name.setdefault(item.Name, item)

    matched = []
    seen = set()
    missing = 0
    for entry in saved_entries:
        found = None
        if entry.get("id") is not None:
            try:
                found = by_id.get(int(entry["id"]))
            except (TypeError, ValueError):
                found = None
        if found is None and mode == "Sheets" and entry.get("number"):
            found = by_number.get(entry["number"])
        if found is None and entry.get("name"):
            found = by_name.get(entry["name"])

        if found is not None:
            if id(found) not in seen:
                seen.add(id(found))
                matched.append(found)
        else:
            missing += 1

    return matched, missing

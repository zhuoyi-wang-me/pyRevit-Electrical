# -*- coding: utf-8 -*-
"""Named 'Template' storage for the Load Transfer tool.

Saves the whole setup -- which source categories are checked, the target
Detail Item family, the matching parameters, and every parameter mapping
row (including multipliers) -- under a name, so a configuration you've
tuned for one family/project can be reloaded instantly instead of
re-picking everything by hand.

Saved templates are stored as JSON files in a Templates folder beside
this module.
"""

import json
import os


_templates_folder = None


def configure(templates_folder=None):
    global _templates_folder
    _templates_folder = templates_folder or os.path.join(os.path.dirname(__file__), "Templates")


def _require_context():
    if _templates_folder is None:
        raise RuntimeError("Call templates.configure() first.")


def _ensure_folder():
    _require_context()
    if not os.path.exists(_templates_folder):
        os.makedirs(_templates_folder)
    return _templates_folder


def _safe_name(name):
    result = str(name).strip()
    for character in '<>:"/\\|?*':
        result = result.replace(character, "-")
    return result.rstrip(". ")


def _template_path(name):
    return os.path.join(_ensure_folder(), _safe_name(name) + ".json")


def available_template_names():
    _ensure_folder()
    names = []
    for filename in os.listdir(_templates_folder):
        if filename.lower().endswith(".json"):
            names.append(os.path.splitext(filename)[0])
    return sorted(names, key=lambda value: value.lower())


def save_template(name, config):
    """config: {
        "include_equipment": bool, "include_fixtures": bool,
        "target_family_key": str,
        "match_source_param": str, "match_target_param": str,
        "mappings": [{"source": str, "target": str, "multiplier": str}, ...]
    }"""
    if not name:
        raise ValueError("Enter a name for the template.")
    with open(_template_path(name), "w") as stream:
        json.dump(config, stream, indent=2)


def load_template(name):
    if not name:
        raise ValueError("Choose a saved template first.")
    with open(_template_path(name), "r") as stream:
        return json.load(stream)


def delete_template(name):
    path = _template_path(name)
    if os.path.exists(path):
        os.remove(path)

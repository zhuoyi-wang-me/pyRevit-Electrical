# -*- coding: utf-8 -*-
"""Named filename-format storage for the pyRevit Export Manager.

This module works with filename_builder.py and the existing XAML controls:
    lstSelectedParams, dgSeparators, txtPreview, txtFormatPreview,
    btnSaveFormat, btnLoadFormat

Saved formats are stored as JSON files in a Formats folder beside this module.
"""

import json
import os

from pyrevit import forms

import filename_builder


_window = None
_formats_folder = None


def configure(window, formats_folder=None):
    """Set the active window and optionally override the Formats folder."""
    global _window, _formats_folder
    _window = window
    _formats_folder = formats_folder or os.path.join(
        os.path.dirname(__file__), "Formats"
    )


def _require_context():
    if _window is None:
        raise RuntimeError("Call format_manager.configure(window) first.")


def _ensure_formats_folder():
    if not os.path.exists(_formats_folder):
        os.makedirs(_formats_folder)
    return _formats_folder


def _safe_name(name):
    result = str(name).strip()
    for character in '<>:"/\\|?*':
        result = result.replace(character, "-")
    return result.rstrip(". ")


def _format_rows():
    """Return serializable filename rows in their exact selected order."""
    filename_builder.update_format_preview()  # commits a DataGrid edit first
    rows = []
    for item in filename_builder.separator_items:
        rows.append({
            "Parameter": str(item.Parameter),
            "Separator": str(item.Separator or "")
        })
    return rows


def _set_items(control, values):
    control.ItemsSource = None
    control.ItemsSource = list(values)


def save_format():
    """Ask for a format name, then save sequence and separators as JSON."""
    _require_context()

    name = forms.ask_for_string(
        default="My Format",
        prompt="Enter a name for this filename format",
        title="Save Export Format"
    )
    if not name:
        return None

    name = _safe_name(name)
    if not name:
        forms.alert("Enter a valid format name.", title="Export Manager")
        return None

    filepath = os.path.join(_ensure_formats_folder(), name + ".json")
    if os.path.exists(filepath):
        replace = forms.alert(
            "A format named '{}' already exists. Replace it?".format(name),
            yes=True,
            no=True,
            title="Replace Export Format"
        )
        if not replace:
            return None

    payload = {
        "version": 2,
        "name": name,
        "items": _format_rows(),
        "prefix": filename_builder.prefix_text,
        "suffix": filename_builder.suffix_text,
    }

    with open(filepath, "w") as stream:
        json.dump(payload, stream, indent=4)

    forms.alert("Format saved:\n{}".format(name), title="Export Manager")
    return filepath


def _available_format_names():
    _ensure_formats_folder()
    names = []
    for filename in os.listdir(_formats_folder):
        if filename.lower().endswith(".json"):
            names.append(os.path.splitext(filename)[0])
    return sorted(names, key=lambda value: value.lower())


def _read_affixes(filepath):
    """(prefix, suffix) saved with a rule; ('', '') for rules saved before
    prefix / suffix existed, so a loaded rule always defines the whole name."""
    try:
        with open(filepath, "r") as stream:
            data = json.load(stream)
        if isinstance(data, dict):
            return str(data.get("prefix", "") or ""), str(data.get("suffix", "") or "")
    except Exception:
        pass
    return "", ""


def _read_rows(filepath):
    with open(filepath, "r") as stream:
        data = json.load(stream)

    # Supports both the new object format and older saved list-only formats.
    if isinstance(data, dict):
        rows = data.get("items", [])
    else:
        rows = data

    if not isinstance(rows, list):
        raise ValueError("This file is not a valid Export Manager format.")
    return rows


def load_format():
    """Choose a named format and restore its sequence, separators and previews."""
    _require_context()
    names = _available_format_names()
    if not names:
        forms.alert("No saved formats found.", title="Export Manager")
        return None

    name = forms.SelectFromList.show(
        names,
        title="Load Export Format",
        button_name="Load",
        multiselect=False
    )
    if not name:
        return None

    filepath = os.path.join(_formats_folder, name + ".json")
    try:
        rows = _read_rows(filepath)

        sequence = []
        separator_rows = []
        for row in rows:
            parameter = str(row.get("Parameter", "")).strip()
            if not parameter:
                continue
            separator = str(row.get("Separator", ""))
            sequence.append(parameter)
            separator_rows.append(
                filename_builder.SeparatorItem(parameter, separator)
            )

        if not sequence:
            raise ValueError("The selected format contains no filename parameters.")

        filename_builder.separator_items = separator_rows
        _set_items(_window.lstSelectedParams, sequence)
        _set_items(_window.dgSeparators, separator_rows)
        prefix, suffix = _read_affixes(filepath)
        filename_builder.set_affixes(prefix, suffix, notify=False)
        filename_builder.show_affixes()
        filename_builder.update_preview()
        filename_builder.update_format_preview()

        forms.alert("Format loaded:\n{}".format(name), title="Export Manager")
        return filepath

    except Exception as error:
        forms.alert(str(error), title="Load Format Error")
        return None


def btnSaveFormat_Click(sender, args):
    save_format()


def btnLoadFormat_Click(sender, args):
    load_format()


def bind_events():
    """Wire the Save/Load buttons after configure(window) has been called."""
    _require_context()
    _window.btnSaveFormat.Click += btnSaveFormat_Click
    _window.btnLoadFormat.Click += btnLoadFormat_Click

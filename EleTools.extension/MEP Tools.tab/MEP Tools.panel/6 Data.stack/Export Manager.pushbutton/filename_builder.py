# -*- coding: utf-8 -*-
"""Filename builder for the pyRevit Export Manager.

Expected XAML control names:
    lstAvailableParams, lstSelectedParams, dgSeparators,
    txtPreview, txtFormatPreview

Usage from ui.py:
    import filename_builder
    filename_builder.configure(window, doc)
    filename_builder.load_parameters()
    window.btnAddParam.Click += filename_builder.btnAddParam_Click
    window.btnRemoveParam.Click += filename_builder.btnRemoveParam_Click
    window.btnMoveUp.Click += filename_builder.btnMoveUp_Click
    window.btnMoveDown.Click += filename_builder.btnMoveDown_Click

The module deliberately has no duplicate UI logic.  The selected-parameter
list is the source of truth; the separator table mirrors it in the same order.
"""

from Autodesk.Revit.DB import FilteredElementCollector, ViewSheet


_window = None
_doc = None
_on_change = None
available_params = []
separator_items = []

# Each export mode ("Sheets" / "Views") remembers its own filename sequence,
# so switching modes doesn't clobber the other one's configuration.
_mode_sequences = {}
DEFAULT_VIEW_SEQUENCE = ["View Name"]

# Fixed text put in front of / after every file name (txtFilePrefix /
# txtFileSuffix), e.g. prefix "QTC-" or suffix "_For Review". Shared by
# Sheets and Views mode; saved with a naming rule.
prefix_text = ""
suffix_text = ""


class SeparatorItem(object):
    """One filename component and the separator placed after it."""

    def __init__(self, parameter, separator="-"):
        self.Parameter = parameter
        self.Separator = separator


def configure(window, doc, on_change=None):
    """Set the active WPF window and Revit document before using this module.

    on_change() is called whenever the filename sequence or a separator
    changes, so ui.py can refresh the grid's "Custom File Name" column.
    FIX: previously that column (and the search on it) stayed stale until
    the user switched Sheets/Views mode.
    """
    global _window, _doc, _on_change
    _window = window
    _doc = doc
    _on_change = on_change


def _notify_change():
    if _on_change is None:
        return
    try:
        _on_change()
    except Exception:
        pass


def _require_context():
    if _window is None or _doc is None:
        raise RuntimeError("Call filename_builder.configure(window, doc) first.")


def _items(control):
    """Return a normal Python list from a WPF ItemsControl."""
    result = []
    try:
        for value in control.Items:
            result.append(value)
    except Exception:
        pass
    return result


def _set_items(control, values):
    """Replace a control ItemsSource without leaving stale WPF bindings."""
    control.ItemsSource = None
    control.ItemsSource = list(values)


PROJECT_INFO_PREFIX = "Project: "


def _project_info_parameter_names():
    """Every Project Information parameter (built-in + shared/project),
    prefixed so 'Project: Project Number' can never collide with a
    same-named sheet/view parameter."""
    names = set()
    try:
        info = _doc.ProjectInformation
        if info is None:
            return names
        for parameter in info.Parameters:
            try:
                definition = parameter.Definition
                if definition and definition.Name:
                    names.add(PROJECT_INFO_PREFIX + definition.Name)
            except Exception:
                pass
    except Exception:
        pass
    return names


def _safe_parameter_names(element, mode="Sheets"):
    names = set()

    # These are handled specially by get_parameter_value().
    if mode == "Views":
        names.update(["View Name", "View Type"])
    else:
        names.update(["Sheet Number", "Sheet Name", "Current Revision", "Revisions on Sheet"])

    # Project Information parameters are available regardless of which
    # sheet/view element is selected as the sample, and even if the model
    # has no sheets/views loaded yet.
    names.update(_project_info_parameter_names())

    if element is not None:
        try:
            for parameter in element.Parameters:
                try:
                    definition = parameter.Definition
                    if definition and definition.Name:
                        names.add(definition.Name)
                except Exception:
                    pass
        except Exception:
            pass

    return sorted(names, key=lambda value: value.lower())


def _first_sheet():
    sheets = FilteredElementCollector(_doc).OfClass(ViewSheet).ToElements()
    for sheet in sheets:
        return sheet
    return None


def load_parameters(sample_element=None, mode="Sheets"):
    """Load all usable parameters for the given mode ('Sheets' or 'Views')
    into the Available Parameters list, using a sample element (a sheet or
    a view) to discover the extra project/shared parameters available.
    """
    global available_params
    _require_context()

    if sample_element is None and mode == "Sheets":
        sample_element = _first_sheet()

    available_params = _safe_parameter_names(sample_element, mode)
    _apply_parameter_search()
    return available_params


def _apply_parameter_search():
    """Show the available parameters, narrowed by the optional
    txtParamSearch box (a model can expose 100+ sheet parameters)."""
    query = ""
    try:
        query = (_window.txtParamSearch.Text or "").strip().lower()
    except Exception:
        query = ""
    if query:
        shown = [name for name in available_params if query in name.lower()]
    else:
        shown = list(available_params)
    _set_items(_window.lstAvailableParams, shown)


def _sanitize_filename_part(value):
    text = str(value).strip()
    for character in '<>:"/\\|?*':
        text = text.replace(character, "-")
    return text


def _parameter_text(parameter):
    if parameter is None:
        return ""
    try:
        return parameter.AsString() or parameter.AsValueString() or ""
    except Exception:
        return ""


def get_parameter_value(element, parameter_name):
    """Read virtual sheet fields plus ordinary Revit parameters safely."""
    if element is None:
        return ""

    try:
        if parameter_name.startswith(PROJECT_INFO_PREFIX):
            actual_name = parameter_name[len(PROJECT_INFO_PREFIX):]
            info = _doc.ProjectInformation
            if info is None:
                return ""
            return _parameter_text(info.LookupParameter(actual_name))

        if parameter_name == "Sheet Number":
            return getattr(element, "SheetNumber", "") or ""

        if parameter_name == "Sheet Name":
            return getattr(element, "Name", "") or ""

        if parameter_name == "View Name":
            return getattr(element, "Name", "") or ""

        if parameter_name == "View Type":
            try:
                return str(element.ViewType)
            except Exception:
                return ""

        # In the user's projects this built-in sheet parameter stores the
        # displayed revision (for example 00/P01), unlike Revision.SequenceNumber.
        if parameter_name == "Current Revision":
            return _parameter_text(element.LookupParameter("Current Revision"))

        if parameter_name == "Revisions on Sheet":
            value = _parameter_text(element.LookupParameter("Revisions on Sheet"))
            if value:
                return value

            # Fallback only when the project exposes no string parameter.
            revision_ids = element.GetAllRevisionIds()
            values = []
            for revision_id in revision_ids:
                revision = _doc.GetElement(revision_id)
                try:
                    number = revision.RevisionNumber
                    if number:
                        values.append(str(number))
                except Exception:
                    pass
            return ",".join(values)

        return _parameter_text(element.LookupParameter(parameter_name))
    except Exception:
        return ""


def _clean_affix(value):
    """Prefix / suffix text made safe for a file name. The user's own spaces
    and separators are kept as typed (e.g. 'QTC_' or ' - For Review'); only
    characters Windows refuses in a name are replaced. Blank -> ''."""
    raw = value or ""
    for character in '<>:"/\\|?*':
        raw = raw.replace(character, "-")
    return raw if raw.strip() else ""


def set_affixes(prefix=None, suffix=None, notify=True):
    """Set the fixed prefix / suffix text. None = leave that one unchanged."""
    global prefix_text, suffix_text
    if prefix is not None:
        prefix_text = _clean_affix(prefix)
    if suffix is not None:
        suffix_text = _clean_affix(suffix)
    if notify and _window is not None:
        update_format_preview()


def _with_affixes(name):
    name = prefix_text + (name or "") + suffix_text
    return name.rstrip(" .") or name          # Windows drops trailing dots / spaces


def build_filename(element, items=None, fallback_name="Export"):
    """Build a safe filename using the configured order and separators.

    Each separator belongs to the parameter before it.  The final separator
    is intentionally ignored, so a filename never ends with '-' or '_'.
    The fixed prefix / suffix text is added around the result.
    """
    rows = items if items is not None else separator_items
    resolved = []

    for row in rows:
        parameter = getattr(row, "Parameter", "")
        value = get_parameter_value(element, parameter)
        if value:
            resolved.append((_sanitize_filename_part(value), getattr(row, "Separator", "-")))

    if not resolved:
        try:
            fallback_name = getattr(element, "SheetNumber", "") or getattr(element, "Name", "") or fallback_name
        except Exception:
            pass
        return _with_affixes(_sanitize_filename_part(fallback_name))

    filename = ""
    for index, pair in enumerate(resolved):
        value, separator = pair
        filename += value
        if index < len(resolved) - 1:
            filename += str(separator or "")

    return _with_affixes(filename or fallback_name)


def _template_preview():
    result = ""
    for index, item in enumerate(separator_items):
        result += str(item.Parameter)
        if index < len(separator_items) - 1:
            result += str(item.Separator or "")
    if not result:
        return result
    return prefix_text + result + suffix_text


def update_preview():
    """Refresh the preview in File Name Builder."""
    _require_context()
    try:
        _window.txtPreview.Text = _template_preview()
    except Exception:
        pass


def update_format_preview():
    """Refresh the preview in File Name Format after committing an edit."""
    _require_context()
    try:
        # Commit a separator typed directly into the WPF DataGrid.
        _window.dgSeparators.CommitEdit()
        _window.dgSeparators.CommitEdit()
    except Exception:
        pass

    try:
        _window.txtFormatPreview.Text = _template_preview()
    except Exception:
        pass
    update_preview()
    _notify_change()


def load_separator_table():
    """Synchronize separator rows with the selected-parameter list.

    Existing separators are retained by parameter name while Add, Remove and
    Move Up/Down change the filename sequence.
    """
    global separator_items
    _require_context()

    existing = {}
    for item in separator_items:
        existing[item.Parameter] = item.Separator

    new_items = []
    for selected in _items(_window.lstSelectedParams):
        parameter = str(selected)
        new_items.append(SeparatorItem(parameter, existing.get(parameter, "-")))

    separator_items = new_items
    _set_items(_window.dgSeparators, separator_items)
    update_format_preview()
    return separator_items


def _sequence_rows(mode):
    """Return the persisted [(parameter, separator), ...] sequence for a
    mode, creating a sensible default the first time that mode is used
    (Views defaults to just 'View Name' so exported view files are named
    after the view)."""
    if mode not in _mode_sequences:
        names = DEFAULT_VIEW_SEQUENCE if mode == "Views" else []
        _mode_sequences[mode] = [(name, "-") for name in names]
    return _mode_sequences[mode]


def _save_mode_sequence(mode):
    """Persist the currently active sequence AND its separators under
    this mode.

    FIX: only the parameter names used to be remembered, so going
    Sheets -> Views -> Sheets silently reset every custom separator
    (and the separators of a loaded format) back to '-'.
    """
    try:
        # Pick up a separator still being typed in the grid.
        _window.dgSeparators.CommitEdit()
        _window.dgSeparators.CommitEdit()
    except Exception:
        pass
    _mode_sequences[mode] = [
        (str(item.Parameter), str(item.Separator or ""))
        for item in separator_items
    ]


def restore_mode_sequence(mode, previous_mode=None):
    """Save the outgoing mode's sequence (if given), then load the target
    mode's own sequence into the Filename Builder / Format tabs. Call this
    whenever the export mode (Sheets/Views) changes."""
    global separator_items
    _require_context()
    if previous_mode:
        _save_mode_sequence(previous_mode)

    rows = _sequence_rows(mode)
    # load_separator_table() keeps separators by parameter name from
    # separator_items, so seed it with this mode's saved separators first.
    separator_items = [SeparatorItem(name, separator) for name, separator in rows]
    _set_items(_window.lstSelectedParams, [name for name, _ in rows])
    load_separator_table()


def _selected_sequence():
    return [str(value) for value in _items(_window.lstSelectedParams)]


def _replace_selected_sequence(sequence, selected_value=None):
    _set_items(_window.lstSelectedParams, sequence)
    if selected_value is not None:
        try:
            _window.lstSelectedParams.SelectedItem = selected_value
        except Exception:
            pass
    load_separator_table()


def btnAddParam_Click(sender, args):
    """Add the chosen available parameter to the end of the filename order."""
    _require_context()
    selected = _window.lstAvailableParams.SelectedItem
    if selected is None:
        return

    sequence = _selected_sequence()
    selected = str(selected)
    if selected not in sequence:
        sequence.append(selected)
    _replace_selected_sequence(sequence, selected)


def btnRemoveParam_Click(sender, args):
    """Remove the selected filename component."""
    _require_context()
    selected = _window.lstSelectedParams.SelectedItem
    if selected is None:
        return

    selected = str(selected)
    sequence = [value for value in _selected_sequence() if value != selected]
    _replace_selected_sequence(sequence)


def btnMoveUp_Click(sender, args):
    """Move the selected filename component one position earlier."""
    _require_context()
    selected = _window.lstSelectedParams.SelectedItem
    if selected is None:
        return

    selected = str(selected)
    sequence = _selected_sequence()
    try:
        index = sequence.index(selected)
    except ValueError:
        return
    if index > 0:
        sequence[index - 1], sequence[index] = sequence[index], sequence[index - 1]
    _replace_selected_sequence(sequence, selected)


def btnMoveDown_Click(sender, args):
    """Move the selected filename component one position later."""
    _require_context()
    selected = _window.lstSelectedParams.SelectedItem
    if selected is None:
        return

    selected = str(selected)
    sequence = _selected_sequence()
    try:
        index = sequence.index(selected)
    except ValueError:
        return
    if index < len(sequence) - 1:
        sequence[index], sequence[index + 1] = sequence[index + 1], sequence[index]
    _replace_selected_sequence(sequence, selected)


def btnRefreshFormat_Click(sender, args):
    """FIX: this XAML button existed but was never wired to anything."""
    update_format_preview()


def dgSeparators_CurrentCellChanged(sender, args):
    """Pick up a separator typed into the grid as soon as the user moves
    to another cell, instead of only on 'Refresh Preview'."""
    update_format_preview()


def lstAvailableParams_MouseDoubleClick(sender, args):
    btnAddParam_Click(sender, args)


def txtParamSearch_TextChanged(sender, args):
    _apply_parameter_search()


def txtAffix_TextChanged(sender, args):
    """Prefix / suffix typed -> update the preview and every file name."""
    try:
        prefix = _window.txtFilePrefix.Text
        suffix = _window.txtFileSuffix.Text
    except Exception:
        return
    set_affixes(prefix, suffix)


def show_affixes():
    """Put the current prefix / suffix into their text boxes (after a rule is loaded)."""
    try:
        _window.txtFilePrefix.Text = prefix_text
        _window.txtFileSuffix.Text = suffix_text
    except Exception:
        pass


def bind_events():
    """Optionally wire the standard Filename Builder XAML buttons."""
    _require_context()
    _window.btnAddParam.Click += btnAddParam_Click
    _window.btnRemoveParam.Click += btnRemoveParam_Click
    _window.btnMoveUp.Click += btnMoveUp_Click
    _window.btnMoveDown.Click += btnMoveDown_Click
    _window.lstAvailableParams.MouseDoubleClick += lstAvailableParams_MouseDoubleClick
    try:
        _window.btnRefreshFormat.Click += btnRefreshFormat_Click
    except Exception:
        pass
    try:
        _window.dgSeparators.CurrentCellChanged += dgSeparators_CurrentCellChanged
    except Exception:
        pass
    try:
        _window.txtParamSearch.TextChanged += txtParamSearch_TextChanged
    except Exception:
        pass
    try:
        _window.txtFilePrefix.TextChanged += txtAffix_TextChanged
        _window.txtFileSuffix.TextChanged += txtAffix_TextChanged
    except Exception:
        pass

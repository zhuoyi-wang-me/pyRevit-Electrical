# -*- coding: utf-8 -*-
"""WPF controller for the Sheet Search & Filter panel.

All Revit reading and matching lives in sheet_filter.py; this module only
moves values between that logic and the XAML controls.

Expected XAML control names (all optional -- the module reports
is_available() == False and the rest of Export Manager keeps working if
the XAML has not been updated yet):

    cmbFilterParameter, cmbFilterOperator, txtFilterValueSearch,
    lstFilterValues, txtFilterText, btnAddFilter, btnRemoveFilter,
    btnClearFilters, dgFilters, txtFilterSummary, expFilters

Usage from ui.py:
    import sheet_filter_ui
    sheet_filter_ui.configure(window, helpers.doc, on_change=filter_items)
    sheet_filter_ui.bind_events()
    sheet_filter_ui.reload(sheet_elements)     # after loading sheets
    matched = sheet_filter_ui.apply_to(items)  # inside filter_items()
"""

from pyrevit import forms

from System.Windows import RoutedEventHandler
from System.Windows.Controls.Primitives import ButtonBase

import helpers
import sheet_filter


_window = None
_on_change = None
_suspend_change = False
# Last known tuple of 'On' states, see _on_enabled_maybe_changed().
_enabled_state = ()

# Active rules, in the order the user added them.  Bound to dgFilters.
criteria = []

# Distinct values for the parameter currently chosen in cmbFilterParameter,
# cached so that typing in the value search box does not re-read the model.
_current_values = []


# ---------------------------------------------------------------------------
# Control access
# ---------------------------------------------------------------------------

def _control(*names):
    """First existing XAML control from a list of names, or None.

    Mirrors ui._control() so this panel degrades gracefully when the XAML
    is older than the Python.
    """
    for name in names:
        try:
            return getattr(_window, name)
        except Exception:
            pass
    return None


def configure(window, doc, on_change=None):
    """Set the window, the document and the callback that re-applies the
    filter to the selection grid."""
    global _window, _on_change
    _window = window
    _on_change = on_change
    sheet_filter.configure(doc)


def is_available():
    """True only when the filter panel exists in the loaded XAML."""
    if _window is None:
        return False
    return _control("cmbFilterParameter") is not None and \
        _control("dgFilters") is not None


def set_enabled(enabled):
    """Enable/disable the whole panel -- used to grey it out in Views
    mode, where sheet parameters do not apply."""
    panel = _control("expFilters", "grpFilters")
    if panel is None:
        return
    try:
        panel.IsEnabled = bool(enabled)
    except Exception:
        pass


def _notify():
    """Ask ui.py to re-run its filter pipeline and refresh the grid."""
    if _suspend_change or _on_change is None:
        return
    try:
        _on_change()
    except Exception as error:
        forms.alert(
            "The sheet filter could not be applied:\n\n{}".format(error),
            title="Sheet Filter"
        )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def reload(sheet_elements):
    """Register a freshly collected sheet list and rebuild the parameter
    drop-down.  Existing rules are kept, since they are usually still
    valid after a reload, but their cached values are dropped."""
    global _suspend_change
    if not is_available():
        sheet_filter.set_sheets(sheet_elements)
        return

    _suspend_change = True
    try:
        sheet_filter.set_sheets(sheet_elements)

        names = sheet_filter.parameter_names(refresh=True)
        parameter_box = _control("cmbFilterParameter")
        previous = None
        try:
            previous = parameter_box.SelectedItem
        except Exception:
            pass

        helpers.set_items_source(parameter_box, names)
        if previous is not None and str(previous) in names:
            parameter_box.SelectedItem = str(previous)
        elif names:
            parameter_box.SelectedIndex = 0

        operator_box = _control("cmbFilterOperator")
        if operator_box is not None and operator_box.SelectedItem is None:
            helpers.set_items_source(operator_box, sheet_filter.OPERATORS)
            operator_box.SelectedIndex = 0

        # Drop rules whose parameter no longer exists in this model.
        _prune_criteria(names)
        _refresh_criteria_grid()
        _load_values_for_selected_parameter()
        _apply_operator_state()
    finally:
        _suspend_change = False


def _prune_criteria(valid_names):
    global criteria
    kept = [c for c in criteria if c.Parameter in valid_names]
    if len(kept) != len(criteria):
        criteria = kept


def _load_values_for_selected_parameter():
    """Fill the value list with the distinct values of the chosen
    parameter, honouring the value search box."""
    global _current_values
    value_list = _control("lstFilterValues")
    if value_list is None:
        return

    parameter = _selected_parameter()
    if not parameter:
        helpers.set_items_source(value_list, [])
        _current_values = []
        return

    try:
        _current_values = sheet_filter.parameter_values(parameter)
    except Exception as error:
        _current_values = []
        forms.alert(
            "Could not read values for '{}':\n\n{}".format(parameter, error),
            title="Sheet Filter"
        )

    _apply_value_search()


def _apply_value_search():
    """Narrow the value list by the text in txtFilterValueSearch.

    Needed for parameters such as Sheet Name where a large project can
    produce hundreds of distinct values.
    """
    value_list = _control("lstFilterValues")
    if value_list is None:
        return

    search = ""
    box = _control("txtFilterValueSearch")
    if box is not None:
        try:
            search = (box.Text or "").strip().lower()
        except Exception:
            search = ""

    if search:
        shown = [v for v in _current_values if search in str(v).lower()]
    else:
        shown = list(_current_values)

    helpers.set_items_source(value_list, shown)


# ---------------------------------------------------------------------------
# Reading the builder controls
# ---------------------------------------------------------------------------

def _selected_parameter():
    box = _control("cmbFilterParameter")
    try:
        return str(box.SelectedItem or "")
    except Exception:
        return ""


def _selected_operator():
    box = _control("cmbFilterOperator")
    try:
        value = str(box.SelectedItem or "")
    except Exception:
        value = ""
    return value or sheet_filter.OP_ANY_OF


def _selected_values():
    """Every value ticked/highlighted in the multi-select value list."""
    value_list = _control("lstFilterValues")
    values = []
    try:
        for value in value_list.SelectedItems:
            values.append(str(value))
    except Exception:
        pass
    return values


def _typed_text():
    box = _control("txtFilterText")
    try:
        return (box.Text or "").strip()
    except Exception:
        return ""


def _apply_operator_state():
    """Grey out whichever value input the current operator does not use,
    so the panel always shows what is expected of the user."""
    operator = _selected_operator()
    uses_list = operator in sheet_filter.VALUE_LIST_OPERATORS
    uses_text = operator in sheet_filter.TEXT_OPERATORS

    for name, enabled in (
        ("lstFilterValues", uses_list),
        ("txtFilterValueSearch", uses_list),
        ("txtFilterText", uses_text)
    ):
        control = _control(name)
        if control is None:
            continue
        try:
            control.IsEnabled = enabled
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Criteria grid
# ---------------------------------------------------------------------------

def _enabled_signature():
    """Which rules are ticked 'On' right now -- used to tell a real toggle
    from a plain click/arrow-key move inside the filter grid."""
    return tuple(bool(criterion.IsEnabled) for criterion in criteria)


def _refresh_criteria_grid():
    global _enabled_state
    grid = _control("dgFilters")
    if grid is None:
        return
    for criterion in criteria:
        criterion.refresh_display()

    selected = None
    try:
        selected = grid.SelectedItem
    except Exception:
        pass
    # Record the state BEFORE rebinding: rebinding raises CurrentCellChanged,
    # which must not mistake this refresh for a tick-box toggle (the caller
    # notifies once itself).
    _enabled_state = _enabled_signature()
    helpers.set_items_source(grid, criteria)
    # Rebinding drops the highlighted row; put it back so "Remove" still
    # has a target after the list is refreshed.
    if selected is not None and any(selected is c for c in criteria):
        try:
            grid.SelectedItem = selected
        except Exception:
            pass


def active_criteria():
    return sheet_filter.enabled_criteria(criteria)


def has_active_criteria():
    return bool(active_criteria())


# ---------------------------------------------------------------------------
# Applying the filter
# ---------------------------------------------------------------------------

def apply_to(items):
    """Return the subset of ExportItems matching every enabled rule and
    fill each matched item's FilterValues cell.

    Called by ui.filter_items() before the free-text search runs.
    """
    items = list(items or [])
    active = active_criteria()

    if not active:
        for item in items:
            try:
                item.FilterValues = ""
            except Exception:
                pass
        update_summary(len(items), len(items))
        return items

    try:
        matched = sheet_filter.filter_items(items, active)
    except Exception as error:
        forms.alert(
            "Sheet filter failed, showing all sheets instead:\n\n{}".format(error),
            title="Sheet Filter"
        )
        return items

    # Only matched rows are visible, so only they need the value column.
    for item in matched:
        try:
            item.FilterValues = sheet_filter.describe(item.Element, active)
        except Exception:
            item.FilterValues = ""

    update_summary(len(matched), len(items))
    return matched


def update_summary(matched, total, extra=""):
    control = _control("txtFilterSummary")
    if control is None:
        return
    text = sheet_filter.summary_text(matched, total)
    if active_criteria():
        text += "  ({} filter(s) active)".format(len(active_criteria()))
    if extra:
        text += "  " + extra
    try:
        control.Text = text
    except Exception:
        pass


# ---------------------------------------------------------------------------
# XAML event handlers
# ---------------------------------------------------------------------------

def cmbFilterParameter_SelectionChanged(sender, args):
    if _suspend_change:
        return
    _load_values_for_selected_parameter()


def cmbFilterOperator_SelectionChanged(sender, args):
    if _suspend_change:
        return
    _apply_operator_state()


def txtFilterValueSearch_TextChanged(sender, args):
    if _suspend_change:
        return
    _apply_value_search()


def btnAddFilter_Click(sender, args):
    """Validate the builder controls and append a new rule."""
    parameter = _selected_parameter()
    if not parameter:
        forms.alert("Choose a sheet parameter first.", title="Sheet Filter")
        return

    operator = _selected_operator()

    if operator in sheet_filter.VALUE_LIST_OPERATORS:
        values = _selected_values()
        if not values:
            forms.alert(
                "Select one or more values for '{}'.\n\n"
                "Hold Ctrl or Shift to pick several -- a sheet matches if "
                "the parameter equals any of them.".format(parameter),
                title="Sheet Filter"
            )
            return
    elif operator in sheet_filter.TEXT_OPERATORS:
        text = _typed_text()
        if not text:
            forms.alert(
                "Type the text that '{}' should {}.".format(parameter, operator),
                title="Sheet Filter"
            )
            return
        values = [text]
    else:
        values = []

    # One rule per parameter+operator pair: adding the same pair again
    # replaces its values instead of stacking a contradictory AND.
    for existing in criteria:
        if existing.Parameter == parameter and existing.Operator == operator:
            existing.Values = values
            existing.IsEnabled = True
            existing.refresh_display()
            _refresh_criteria_grid()
            _notify()
            return

    criteria.append(sheet_filter.FilterCriterion(parameter, operator, values))
    _refresh_criteria_grid()
    _notify()


def btnRemoveFilter_Click(sender, args):
    grid = _control("dgFilters")
    selected = None
    try:
        selected = grid.SelectedItem
    except Exception:
        pass

    if selected is None:
        forms.alert("Select a filter row to remove.", title="Sheet Filter")
        return

    try:
        criteria.remove(selected)
    except ValueError:
        return

    _refresh_criteria_grid()
    _notify()


def btnClearFilters_Click(sender, args):
    if not criteria:
        return
    del criteria[:]
    _refresh_criteria_grid()
    _notify()


def _on_enabled_maybe_changed():
    """Re-apply the filter only when an 'On' tick box really changed.

    FIX: this used to rebuild the filter grid and re-run the whole sheet
    filter on EVERY CurrentCellChanged -- i.e. on every click or arrow key
    inside the grid, toggled or not.  Rebinding the grid from inside its
    own CurrentCellChanged also re-entered this handler and threw away the
    highlighted row, so "Remove" could report "Select a filter row to
    remove" right after the user had clicked one.
    """
    global _enabled_state
    if _suspend_change:
        return
    grid = _control("dgFilters")
    try:
        grid.CommitEdit()
        grid.CommitEdit()
    except Exception:
        pass
    state = _enabled_signature()
    if state == _enabled_state:
        return
    _enabled_state = state
    _notify()


def dgFilters_CurrentCellChanged(sender, args):
    _on_enabled_maybe_changed()


def dgFilters_Click(sender, args):
    """The 'On' tick box is a one-click CheckBox in a template column; its
    Click bubbles up to the grid, so the filter re-applies immediately
    instead of waiting for the user to click somewhere else."""
    _on_enabled_maybe_changed()


def bind_events():
    """Wire the filter panel controls, skipping any that are absent."""
    if not is_available():
        return

    parameter_box = _control("cmbFilterParameter")
    if parameter_box is not None:
        parameter_box.SelectionChanged += cmbFilterParameter_SelectionChanged

    operator_box = _control("cmbFilterOperator")
    if operator_box is not None:
        helpers.set_items_source(operator_box, sheet_filter.OPERATORS)
        operator_box.SelectedIndex = 0
        operator_box.SelectionChanged += cmbFilterOperator_SelectionChanged

    value_search = _control("txtFilterValueSearch")
    if value_search is not None:
        value_search.TextChanged += txtFilterValueSearch_TextChanged

    add_button = _control("btnAddFilter")
    if add_button is not None:
        add_button.Click += btnAddFilter_Click

    remove_button = _control("btnRemoveFilter")
    if remove_button is not None:
        remove_button.Click += btnRemoveFilter_Click

    clear_button = _control("btnClearFilters")
    if clear_button is not None:
        clear_button.Click += btnClearFilters_Click

    grid = _control("dgFilters")
    if grid is not None:
        grid.CurrentCellChanged += dgFilters_CurrentCellChanged
        try:
            grid.AddHandler(ButtonBase.ClickEvent, RoutedEventHandler(dgFilters_Click))
        except Exception:
            pass

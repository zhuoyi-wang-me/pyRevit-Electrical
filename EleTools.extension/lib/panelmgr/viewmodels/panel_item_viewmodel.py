# -*- coding: utf-8 -*-
"""
panel_item_viewmodel.py
=========================
Wraps one PanelInfo for display/selection in the Step 1 DataGrid, and
one report row for the Step 4/5 preview and results grids.
"""

from panelmgr.services.report_service import result_property_name
from panelmgr.viewmodels.mvvm_base import ObservableObject


class TemplateOption(ObservableObject):
    """One entry in the Panel Schedule Template selection ComboBox."""

    def __init__(self, name, element_id):
        ObservableObject.__init__(self)
        self.Name = name
        self.ElementId = element_id  # None means "use Revit's default"


class PanelItemViewModel(ObservableObject):
    def __init__(self, panel_element, panel_info, on_selection_changed=None):
        ObservableObject.__init__(self)
        self.panel_element = panel_element
        self.panel_info = panel_info
        self._is_selected = True
        # Called (no args) whenever IsSelected changes. Our custom
        # RelayCommand only re-queries CanExecute when explicitly told
        # to (unlike WPF's built-in CommandManager.RequerySuggested
        # auto-requery), so without this hook, checking/unchecking a
        # panel's checkbox in the Step 1 DataGrid never re-enables the
        # Next button - it silently stays disabled even after a valid
        # selection is made. See main_viewmodel._populate_panels,
        # which passes self._refresh_can_execute here.
        self._on_selection_changed = on_selection_changed

    @property
    def Name(self):
        return self.panel_info.name

    @property
    def PhaseConfig(self):
        return self.panel_info.phase_config

    @property
    def NumSlots(self):
        return self.panel_info.num_slots

    @property
    def CircuitCount(self):
        return len(self.panel_info.circuits)

    @property
    def HasExistingSchedule(self):
        return "Yes" if self.panel_info.panel_schedule_view_id else "No"

    @property
    def IsSelected(self):
        return self._is_selected

    @IsSelected.setter
    def IsSelected(self, value):
        changed = self.set_field("_is_selected", bool(value), "IsSelected")
        if changed and self._on_selection_changed:
            self._on_selection_changed()


class PreviewRowViewModel(ObservableObject):
    """One row in the Step 4 preview grid: before/after per panel."""

    def __init__(self, panel_name, before_r, before_y, before_b,
                 after_r, after_y, after_b, before_imbalance,
                 after_imbalance, spares, spaces, spare_rating=None, poles=None):
        ObservableObject.__init__(self)
        self.PanelName = panel_name
        self.BeforeR = round(before_r, 0)
        self.BeforeY = round(before_y, 0)
        self.BeforeB = round(before_b, 0)
        self.AfterR = round(after_r, 0)
        self.AfterY = round(after_y, 0)
        self.AfterB = round(after_b, 0)
        self.BeforeImbalance = round(before_imbalance, 2)
        self.AfterImbalance = round(after_imbalance, 2)
        self.SparesAdded = spares
        self.SpacesAdded = spaces
        self.SpareRating = spare_rating if spare_rating is not None else "-"
        self.Poles = poles if poles is not None else "-"


class ResultRowViewModel(ObservableObject):
    """One row in the Step 5 results summary grid."""

    def __init__(self, row_dict):
        ObservableObject.__init__(self)
        for k, v in row_dict.items():
            setattr(self, result_property_name(k), v)
        self._raw = row_dict

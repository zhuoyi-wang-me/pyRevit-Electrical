# -*- coding: utf-8 -*-
"""
main_viewmodel.py
===================
The single ViewModel backing MainWindow.xaml's 5-step wizard. Holds
all bindable state and commands; delegates every piece of business
logic to `panelmgr.services.*` and every piece of Revit access to
`panelmgr.revit_utils`, so this file stays focused on UI
orchestration, threading/progress plumbing and error handling.

Threading note: Revit's API is single-threaded and all element
access/transactions MUST happen on Revit's main thread. To keep the
UI responsive during a long batch we run the batch on a background
.NET thread but marshal each Revit-touching step back onto the UI
thread that owns Revit's context using `System.Windows.Threading
.Dispatcher`. In practice, because pyRevit's `forms.WPFWindow` is
shown modally with `ShowDialog()` on Revit's own UI thread, the
simplest and safest approach (used here) is to run the batch
synchronously on that same thread while pumping `DoEvents`-style
`Dispatcher.Invoke` calls between panels so the progress bar/status
text still repaint and the Cancel button remains clickable. This
avoids any cross-thread Revit API access, which is unsupported and
will throw `InvalidOperationException`.
"""

import time

from System.Collections.ObjectModel import ObservableCollection
from System.Windows import Visibility
from System.Windows.Threading import DispatcherPriority
from System import Action

from panelmgr import config, revit_utils
from panelmgr.services import schedule_service, report_service
from panelmgr.models import (
    WaysBracketRow, PanelTypeConfigRow,
    SPARE_SPACE_MODE_WAYS_TABLE, SPARE_SPACE_MODE_PERCENTAGE,
    DEFAULT_FALLBACK_RATING, DEFAULT_FALLBACK_POLES,
)
from panelmgr.logger import get_logger
from panelmgr.viewmodels.mvvm_base import ObservableObject, RelayCommand
from panelmgr.viewmodels.panel_item_viewmodel import (
    PanelItemViewModel, PreviewRowViewModel, ResultRowViewModel, TemplateOption
)

logger = get_logger(__name__)


class MainViewModel(ObservableObject):

    TOTAL_STEPS = 5

    def __init__(self, doc, uidoc):
        ObservableObject.__init__(self)
        self._doc = doc
        self._uidoc = uidoc
        self._window = None
        self._cancel_requested = False

        settings_data = config.load_settings()
        self._balancing_settings = config.balancing_settings_from_dict(settings_data)
        self._spare_space_settings = config.spare_space_settings_from_dict(settings_data)

        self._current_step = 1
        self._is_busy = False
        self._status_message = "Ready."
        self._progress_value = 0
        self._progress_max = 100
        self._results_summary = ""

        self.AllPanels = ObservableCollection[object]()
        self.PreviewRows = ObservableCollection[object]()
        self.ResultRows = ObservableCollection[object]()
        self.AvailableTemplates = ObservableCollection[object]()
        self._selected_template_id = None

        self.WaysRows = ObservableCollection[object]()
        for row in self._spare_space_settings.ways_table:
            self.WaysRows.Add(row)
        self.TypeConfigRows = ObservableCollection[object]()
        for row in self._spare_space_settings.type_config_table:
            self.TypeConfigRows.Add(row)

        self._preview_cache = None
        self._last_selected_panel_infos = None

        self._build_commands()
        self.load_from_selection(None)

    # ------------------------------------------------------------------
    # Window plumbing
    # ------------------------------------------------------------------
    def attach_window(self, window):
        self._window = window

    def _run_on_ui(self, action_fn):
        """Marshals a zero-arg callable onto the WPF Dispatcher so
        progress/status updates repaint immediately even while we are
        in the middle of a long-running loop on the same thread."""
        if self._window is not None:
            self._window.Dispatcher.Invoke(
                Action(action_fn), DispatcherPriority.Background
            )
        else:
            action_fn()

    # ------------------------------------------------------------------
    # Bindable properties
    # ------------------------------------------------------------------
    @property
    def CurrentStep(self):
        return self._current_step

    @CurrentStep.setter
    def CurrentStep(self, value):
        if self.set_field("_current_step", value, "CurrentStep"):
            for i in range(1, self.TOTAL_STEPS + 1):
                self.raise_property_changed("Step{}Visibility".format(i))
            self.raise_property_changed("StepIndicatorText")
            self._refresh_can_execute()

    def _step_visibility(self, step_number):
        return Visibility.Visible if self._current_step == step_number else Visibility.Collapsed

    @property
    def Step1Visibility(self):
        return self._step_visibility(1)

    @property
    def Step2Visibility(self):
        return self._step_visibility(2)

    @property
    def Step3Visibility(self):
        return self._step_visibility(3)

    @property
    def Step4Visibility(self):
        return self._step_visibility(4)

    @property
    def Step5Visibility(self):
        return self._step_visibility(5)

    @property
    def StepIndicatorText(self):
        return "Step {} of {}".format(self._current_step, self.TOTAL_STEPS)

    @property
    def IsBusy(self):
        return self._is_busy

    @IsBusy.setter
    def IsBusy(self, value):
        self.set_field("_is_busy", value, "IsBusy")
        self._refresh_can_execute()

    @property
    def StatusMessage(self):
        return self._status_message

    @StatusMessage.setter
    def StatusMessage(self, value):
        self.set_field("_status_message", value, "StatusMessage")

    @property
    def ProgressValue(self):
        return self._progress_value

    @ProgressValue.setter
    def ProgressValue(self, value):
        self.set_field("_progress_value", value, "ProgressValue")

    @property
    def ProgressMax(self):
        return self._progress_max

    @ProgressMax.setter
    def ProgressMax(self, value):
        self.set_field("_progress_max", value, "ProgressMax")

    @property
    def ResultsSummary(self):
        return self._results_summary

    @ResultsSummary.setter
    def ResultsSummary(self, value):
        self.set_field("_results_summary", value, "ResultsSummary")

    # --- Step 2: balancing settings -----------------------------------
    @property
    def BalancingEnabled(self):
        return self._balancing_settings.enabled

    @BalancingEnabled.setter
    def BalancingEnabled(self, value):
        self._balancing_settings.enabled = bool(value)
        self.raise_property_changed("BalancingEnabled")

    @property
    def MaxImbalancePct(self):
        return self._balancing_settings.max_imbalance_pct

    @MaxImbalancePct.setter
    def MaxImbalancePct(self, value):
        self._balancing_settings.max_imbalance_pct = float(value)
        self.raise_property_changed("MaxImbalancePct")

    @property
    def RespectLockedCircuits(self):
        return self._balancing_settings.respect_locked_circuits

    @RespectLockedCircuits.setter
    def RespectLockedCircuits(self, value):
        self._balancing_settings.respect_locked_circuits = bool(value)
        self.raise_property_changed("RespectLockedCircuits")

    @property
    def ArrangeRYBAfterBalance(self):
        return self._balancing_settings.arrange_ryb_after_balance

    @ArrangeRYBAfterBalance.setter
    def ArrangeRYBAfterBalance(self, value):
        self._balancing_settings.arrange_ryb_after_balance = bool(value)
        self.raise_property_changed("ArrangeRYBAfterBalance")

    @property
    def PreserveExistingSchedule(self):
        return self._balancing_settings.preserve_existing_schedule

    @PreserveExistingSchedule.setter
    def PreserveExistingSchedule(self, value):
        self._balancing_settings.preserve_existing_schedule = bool(value)
        self.raise_property_changed("PreserveExistingSchedule")

    @property
    def SpareDiversityFactor(self):
        return self._spare_space_settings.spare_diversity_factor

    @SpareDiversityFactor.setter
    def SpareDiversityFactor(self, value):
        try:
            self._spare_space_settings.spare_diversity_factor = float(value)
        except (TypeError, ValueError):
            self._spare_space_settings.spare_diversity_factor = 1.0
        self.raise_property_changed("SpareDiversityFactor")

    # --- Step 3: spare / space settings ---------------------------------
    @property
    def SparePercentage(self):
        return self._spare_space_settings.spare_percentage

    @SparePercentage.setter
    def SparePercentage(self, value):
        self._spare_space_settings.spare_percentage = float(value)
        self.raise_property_changed("SparePercentage")

    @property
    def SpacePercentage(self):
        return self._spare_space_settings.space_percentage

    @SpacePercentage.setter
    def SpacePercentage(self, value):
        self._spare_space_settings.space_percentage = float(value)
        self.raise_property_changed("SpacePercentage")

    @property
    def ReservedSlots(self):
        return self._spare_space_settings.reserved_slots

    @ReservedSlots.setter
    def ReservedSlots(self, value):
        self._spare_space_settings.reserved_slots = int(value)
        self.raise_property_changed("ReservedSlots")

    @property
    def AutoFillRemaining(self):
        return self._spare_space_settings.auto_fill_remaining

    @AutoFillRemaining.setter
    def AutoFillRemaining(self, value):
        self._spare_space_settings.auto_fill_remaining = bool(value)
        self.raise_property_changed("AutoFillRemaining")

    @property
    def IsWaysTableMode(self):
        return self._spare_space_settings.mode == SPARE_SPACE_MODE_WAYS_TABLE

    @IsWaysTableMode.setter
    def IsWaysTableMode(self, value):
        self._spare_space_settings.mode = (
            SPARE_SPACE_MODE_WAYS_TABLE if value else SPARE_SPACE_MODE_PERCENTAGE
        )
        self.raise_property_changed("IsWaysTableMode")
        self.raise_property_changed("IsPercentageMode")
        self.raise_property_changed("WaysTableModeVisibility")
        self.raise_property_changed("PercentageModeVisibility")

    @property
    def IsPercentageMode(self):
        return not self.IsWaysTableMode

    @IsPercentageMode.setter
    def IsPercentageMode(self, value):
        self.IsWaysTableMode = not value

    @property
    def WaysTableModeVisibility(self):
        return Visibility.Visible if self.IsWaysTableMode else Visibility.Collapsed

    @property
    def PercentageModeVisibility(self):
        return Visibility.Visible if self.IsPercentageMode else Visibility.Collapsed

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def _build_commands(self):
        self.NextCommand = RelayCommand(self.go_next, self._can_go_next)
        self.BackCommand = RelayCommand(self.go_back, self._can_go_back)
        self.RefreshPanelsCommand = RelayCommand(self.load_from_selection)
        self.ScanAllPanelsCommand = RelayCommand(self.scan_all_panels)
        self.SelectAllCommand = RelayCommand(self.select_all)
        self.SelectNoneCommand = RelayCommand(self.select_none)
        self.RunBatchCommand = RelayCommand(self.run_batch, self._can_run_batch)
        self.CancelCommand = RelayCommand(self.cancel_batch)
        self.ExportReportCommand = RelayCommand(self.export_report, self._can_export)
        self.CloseCommand = RelayCommand(self.close_window)
        self.AddWaysRowCommand = RelayCommand(self.add_ways_row)
        self.AddTypeConfigRowCommand = RelayCommand(self.add_type_config_row)

    def add_ways_row(self, _param):
        row = WaysBracketRow(0, 0, 0)
        self._spare_space_settings.ways_table.append(row)
        self.WaysRows.Add(row)

    def add_type_config_row(self, _param):
        row = PanelTypeConfigRow("NEW", DEFAULT_FALLBACK_RATING, DEFAULT_FALLBACK_POLES)
        self._spare_space_settings.type_config_table.append(row)
        self.TypeConfigRows.Add(row)

    def _refresh_can_execute(self):
        for cmd in (self.NextCommand, self.BackCommand, self.RunBatchCommand,
                    self.ExportReportCommand):
            cmd.raise_can_execute_changed()

    # --- Step 1: panel discovery / selection ----------------------------
    def load_from_selection(self, _param):
        """
        Default panel-loading path: populates AllPanels from whatever
        electrical equipment is ALREADY SELECTED in Revit (Project
        Browser, a view, or the panel elements themselves), rather
        than scanning every panel in the model. This is both faster on
        large projects and matches the actual workflow - the user
        already knows which panels they want to work on, so there's no
        need to build/read PanelInfo for every panel in the model just
        to show a picker list. If nothing is selected, this shows an
        empty list with guidance rather than silently falling back to
        a full scan - use `scan_all_panels` (Step 1's "Scan All
        Panels" button) for that explicitly.
        """
        try:
            elements = revit_utils.selected_panels_from_selection(self._doc, self._uidoc)
            self._populate_panels(elements)
            if elements:
                self.StatusMessage = "Loaded {} panel(s) from your current " \
                    "Revit selection.".format(len(elements))
            else:
                self.StatusMessage = (
                    "Nothing selected. Select one or more electrical panels "
                    "in Revit (Project Browser, a view, or the panel "
                    "elements themselves) before opening this tool, or use "
                    "'Scan All Panels' below to browse the whole model."
                )
        except Exception:
            logger.exception("load_from_selection failed")
            self.StatusMessage = "Error loading panels from selection - see log."

        self._load_templates()

    def scan_all_panels(self, _param):
        """
        Explicit, opt-in full-model scan (the tool's OLD default
        behavior). Kept available for cases where the user genuinely
        wants to browse every panel in the project rather than
        pre-selecting specific ones in Revit, but no longer runs
        automatically - a live model showed this producing a long,
        noisy list the user didn't actually want to scan through.
        """
        try:
            self.StatusMessage = "Scanning entire model for panels - this " \
                "may take a moment on large projects..."
            elements = revit_utils.get_all_electrical_panels(self._doc)
            self._populate_panels(elements)
            self.StatusMessage = "Found {} panel(s) in the model.".format(len(elements))
        except Exception:
            logger.exception("scan_all_panels failed")
            self.StatusMessage = "Error scanning panels - see log."

        self._load_templates()

    def _populate_panels(self, elements):
        self.AllPanels.Clear()
        for el in elements:
            info = revit_utils.build_panel_info(self._doc, el)
            self.AllPanels.Add(PanelItemViewModel(
                el, info, on_selection_changed=self._refresh_can_execute
            ))
        # Selection state just changed wholesale (new list loaded) -
        # make sure Next's enabled state reflects it immediately.
        self._refresh_can_execute()

    def _load_templates(self):
        """
        Populates AvailableTemplates from the project's
        PanelScheduleTemplate elements (Electrical Settings -> Panel
        Schedule Templates), plus a leading "Project Default" option
        that falls back to whatever Revit's 2-argument
        CreateInstanceView overload picks on its own. Restores the
        last-used selection by name from persisted settings, if it
        still exists in this project.
        """
        try:
            previously_selected_name = None
            current = self.SelectedTemplate
            if current is not None:
                previously_selected_name = current.Name
            else:
                settings_data = config.load_settings()
                previously_selected_name = settings_data.get("template", {}).get("last_selected_name")

            self.AvailableTemplates.Clear()
            default_option = TemplateOption("Project Default", None)
            self.AvailableTemplates.Add(default_option)

            templates = revit_utils.get_panel_schedule_templates(self._doc)
            for name, element_id in templates:
                self.AvailableTemplates.Add(TemplateOption(name, element_id))

            match = default_option
            if previously_selected_name:
                for opt in self.AvailableTemplates:
                    if opt.Name == previously_selected_name:
                        match = opt
                        break
            self.SelectedTemplate = match
        except Exception:
            logger.exception("_load_templates failed")

    @property
    def SelectedTemplate(self):
        return getattr(self, "_selected_template_option", None)

    @SelectedTemplate.setter
    def SelectedTemplate(self, value):
        self._selected_template_option = value
        self._selected_template_id = value.ElementId if value else None
        self.raise_property_changed("SelectedTemplate")

    def select_all(self, _param):
        for p in self.AllPanels:
            p.IsSelected = True

    def select_none(self, _param):
        for p in self.AllPanels:
            p.IsSelected = False

    def _selected_panels(self):
        return [p for p in self.AllPanels if p.IsSelected]

    # --- Wizard navigation -----------------------------------------------
    def _can_go_next(self, _param=None):
        if self._is_busy:
            return False
        if self._current_step == 1:
            return len(self._selected_panels()) > 0
        return self._current_step < self.TOTAL_STEPS

    def _can_go_back(self, _param=None):
        return (not self._is_busy) and self._current_step > 1

    def go_next(self, _param):
        if self._current_step == 3:
            # Entering Step 4 (Preview) -> compute preview synchronously.
            self._compute_preview()
        self.CurrentStep = min(self.TOTAL_STEPS, self._current_step + 1)

    def go_back(self, _param):
        self.CurrentStep = max(1, self._current_step - 1)

    # --- Step 4: preview ---------------------------------------------------
    def _compute_preview(self):
        try:
            self.StatusMessage = "Computing preview..."
            selected = self._selected_panels()
            panel_infos = [(p.panel_element, p.panel_info) for p in selected]
            self._last_selected_panel_infos = panel_infos

            preview = schedule_service.run_preview(
                panel_infos, self._balancing_settings, self._spare_space_settings
            )
            self._preview_cache = preview

            self.PreviewRows.Clear()
            for panel_element, panel_info in panel_infos:
                entry = preview[panel_info.element_id]
                bal = entry["balancing_result"]
                ss = entry["spare_space_report"]
                row = PreviewRowViewModel(
                    panel_name=panel_info.name,
                    before_r=_load_or_zero(bal.before_phase_loads, "R"),
                    before_y=_load_or_zero(bal.before_phase_loads, "Y"),
                    before_b=_load_or_zero(bal.before_phase_loads, "B"),
                    after_r=_load_or_zero(bal.after_phase_loads, "R"),
                    after_y=_load_or_zero(bal.after_phase_loads, "Y"),
                    after_b=_load_or_zero(bal.after_phase_loads, "B"),
                    before_imbalance=bal.before_imbalance_pct,
                    after_imbalance=bal.after_imbalance_pct,
                    spares=ss.spares_added,
                    spaces=ss.spaces_added,
                    spare_rating=entry.get("spare_rating"),
                    poles=entry.get("poles"),
                )
                self.PreviewRows.Add(row)
            # Say plainly which panels will really be rebalanced: a
            # preserved panel shows identical before / after figures and
            # that used to look like "balancing does not work".
            preserved = len([1 for _el, info in panel_infos
                             if preview[info.element_id].get("preserve")])
            to_move = sum(
                len(preview[info.element_id]["balancing_result"].moved_circuits)
                for _el, info in panel_infos
            )
            if not self._balancing_settings.enabled:
                mode = "Load balancing is switched OFF in step 2 - no circuit will be moved."
            elif preserved == len(panel_infos):
                mode = ("NO panel will be rebalanced: all {} already have a schedule and "
                        "'Preserve existing panel schedules' is ticked in step 2. Untick "
                        "it to balance them.".format(preserved))
            else:
                mode = "{} panel(s) will be rebalanced ({} circuit(s) change phase)".format(
                    len(panel_infos) - preserved, to_move)
                if preserved:
                    mode += ", {} left as they are (preserved)".format(preserved)
                mode += "."
            self.StatusMessage = "Preview ready for {} panel(s). {}".format(
                len(panel_infos), mode)
        except Exception:
            logger.exception("_compute_preview failed")
            self.StatusMessage = "Error computing preview - see log."

    # --- Step 5: execute ------------------------------------------------
    def _can_run_batch(self, _param=None):
        return (not self._is_busy) and self._current_step == 5 \
            and self._last_selected_panel_infos is not None

    def cancel_batch(self, _param):
        self._cancel_requested = True
        self.StatusMessage = "Cancelling..."

    def run_batch(self, _param):
        if self._is_busy:
            return
        self._cancel_requested = False
        self.IsBusy = True
        self.ProgressValue = 0
        self.ProgressMax = max(1, len(self._last_selected_panel_infos))
        self.ResultRows.Clear()
        start = time.time()

        def progress_cb(index, total, panel_name, message):
            def _update():
                self.ProgressValue = index
                self.ProgressMax = total
                self.StatusMessage = "[{}/{}] {} - {}".format(
                    index, total, panel_name, message
                )
            self._run_on_ui(_update)

        def cancel_check():
            return self._cancel_requested

        try:
            balancing_results, ss_reports, failed_panels = schedule_service.execute_batch(
                self._doc,
                self._last_selected_panel_infos,
                self._balancing_settings,
                self._spare_space_settings,
                progress_callback=progress_cb,
                cancel_check=cancel_check,
                template_id=self._selected_template_id,
            )
            ss_by_name = dict((r.panel_name, r) for r in ss_reports)
            rows = report_service.build_report_rows(balancing_results, ss_by_name)
            for row_dict in rows:
                self.ResultRows.Add(ResultRowViewModel(row_dict))
            for failed_name, failed_error in failed_panels:
                self.ResultRows.Add(ResultRowViewModel({
                    "Panel": failed_name,
                    "Total Connected Load (W)": "-",
                    "R Load (W)": "-", "Y Load (W)": "-", "B Load (W)": "-",
                    "Total Connected Load (VA)": "-",
                    "Imbalance Before (%)": "-", "Imbalance After (%)": "-",
                    "RYB Status": "-",
                    "Circuits Moved": "-",
                    "Spares Added": "-", "Spaces Added": "-", "Reserved Slots": "-",
                    # Must match build_report_rows' key set exactly, or the
                    # extra columns bind to a property that does not exist
                    # on these rows and render blank.
                    "Schedule": "-",
                    "Spare Load Booked (VA)": "-",
                    "Spare Load by Phase (VA)": "-",
                    "Imbalance Incl. Spares (%)": "-",
                    "Status": "FAILED: {}".format(failed_error),
                }))

            self._last_report_rows = rows
            elapsed = time.time() - start
            summary = (
                "Processed {} of {} panel(s) successfully in {:.1f}s. "
                "Average imbalance reduced from {:.1f}% to {:.1f}%."
            ).format(
                len(balancing_results), len(self._last_selected_panel_infos), elapsed,
                _avg([r.before_imbalance_pct for r in balancing_results]),
                _avg([r.after_imbalance_pct for r in balancing_results]),
            ) if balancing_results else "No panels completed successfully."
            if failed_panels:
                summary += " {} panel(s) FAILED and were skipped (their own " \
                    "changes were rolled back individually; successful panels " \
                    "were kept): {}.".format(
                        len(failed_panels),
                        ", ".join(name for name, _err in failed_panels)
                    )
            misaligned_total = sum(
                len(r.misaligned_3pole_circuits) for r in balancing_results
            )
            if misaligned_total:
                summary += " WARNING: {} 3-pole circuit(s) did not land on a " \
                    "genuine R-Y-B sequence after the move - see the 3-Pole " \
                    "R-Y-B column below for exactly which ones.".format(
                        misaligned_total
                    )
            self.ResultsSummary = summary
            self.StatusMessage = "Batch complete." if not failed_panels else \
                "Batch complete with {} failure(s) - see results below.".format(len(failed_panels))

            # Persist settings used, for next session.
            data = config.settings_to_dict(
                self._balancing_settings, self._spare_space_settings
            )
            selected = self.SelectedTemplate
            data["template"] = {
                "last_selected_name": selected.Name if selected else "Project Default"
            }
            config.save_settings(data)

        except schedule_service.BatchCancelled:
            self.StatusMessage = "Batch cancelled by user. All changes rolled back."
            self.ResultsSummary = "Cancelled - no changes were made."
        except Exception as ex:
            logger.exception("run_batch failed")
            self.StatusMessage = "Error during batch - see log for details."
            self.ResultsSummary = "Failed: {}".format(ex)
        finally:
            self.IsBusy = False

    def _can_export(self, _param=None):
        return bool(getattr(self, "_last_report_rows", None))

    def export_report(self, _param):
        try:
            from pyrevit import forms as _forms
            path = _forms.save_file(file_ext="html")
            if not path:
                return
            report_service.export_html(self._last_report_rows, path)
            self.StatusMessage = "Report exported to {}".format(path)
        except Exception:
            logger.exception("export_report failed")
            self.StatusMessage = "Could not export report - see log."

    def close_window(self, _param):
        if self._window is not None:
            self._window.Close()


def _load_or_zero(phase_loads, phase):
    pl = phase_loads.get(phase)
    return pl.load_va if pl else 0.0


def _avg(values):
    values = list(values)
    if not values:
        return 0.0
    return sum(values) / float(len(values))

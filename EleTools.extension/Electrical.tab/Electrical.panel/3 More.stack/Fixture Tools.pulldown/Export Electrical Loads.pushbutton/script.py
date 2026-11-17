# -*- coding: utf-8 -*-
"""Electrical Load Report - export loads by panel and load classification to Excel.

Layers:
    script.py     WPF controller (this file)
    elr_revit.py  Revit API access and load extraction
    elr_report.py Pure-Python aggregation into sheets
    elr_xlsx.py   Dependency-free XLSX writer
"""
import datetime
import os
import sys
import traceback

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from System import Action
from System.Windows import FontWeights, Thickness, Visibility
from System.Windows.Controls import CheckBox, Orientation, StackPanel, TextBlock
from System.Windows.Media import BrushConverter
from System.Windows.Threading import Dispatcher, DispatcherPriority

from pyrevit import forms, revit, script

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import elr_report as rep  # noqa: E402
import elr_revit as er  # noqa: E402
import elr_xlsx as xl  # noqa: E402

TITLE = 'Electrical Load Report'
ALL_TYPES = 'All panel types'
UNIT_OPTIONS = ['VA / W  (divide by 1000)', 'kVA / kW  (use as entered)']
CATEGORY_CONTROLS = [('CatElectricalFixtures', 'OST_ElectricalFixtures'),
                     ('CatMechanicalEquipment', 'OST_MechanicalEquipment'),
                     ('CatLightingFixtures', 'OST_LightingFixtures')]
REPORT_CHECKS = ['GroupByPanel', 'GroupByClassification', 'SumByIdentifier', 'IncludeCircuit',
                 'IncludeCount', 'IncludeConnected', 'IncludeDemand', 'IncludeApparent',
                 'IncludeSummary', 'IncludePanelCheck', 'IncludePanelMatrix', 'IncludePanelRevit', 'IncludeDiagnostics', 'OpenWhenDone',
                 'HideEmptyPanels']
_GRAY = BrushConverter().ConvertFromString('#64748B')


def do_events():
    """Let WPF repaint/handle clicks during the long loop (UpdateLayout does not repaint)."""
    try:
        Dispatcher.CurrentDispatcher.Invoke(DispatcherPriority.Background, Action(lambda: None))
    except Exception:
        pass


def open_path(path):
    from System.Diagnostics import Process, ProcessStartInfo
    info = ProcessStartInfo(path)
    info.UseShellExecute = True  # required on .NET 8 (Revit 2025+)
    Process.Start(info)


class Settings(object):
    """Per-user settings persisted in the pyRevit config file."""

    def __init__(self):
        try:
            self.cfg = script.get_config()
        except Exception:
            self.cfg = None

    def get(self, key, default):
        if self.cfg is None:
            return default
        try:
            value = self.cfg.get_option(key, default)
        except Exception:
            return default
        if value is None:
            return default
        if isinstance(default, bool):
            return str(value).strip().lower() in ('true', '1', 'yes')
        if isinstance(default, int):
            try:
                return int(value)
            except Exception:
                return default
        return value

    def set(self, key, value):
        if self.cfg is not None:
            try:
                setattr(self.cfg, key, value)
            except Exception:
                pass

    def save(self):
        try:
            script.save_config()
        except Exception:
            pass


def select_combo(combo, candidates):
    items = [str(i) for i in combo.Items]
    lowered = [i.lower() for i in items]
    for candidate in candidates:
        if not candidate:
            continue
        if candidate in items:
            combo.SelectedIndex = items.index(candidate)
            return True
        if candidate.lower() in lowered:
            combo.SelectedIndex = lowered.index(candidate.lower())
            return True
    return False


class LoadReportWindow(forms.WPFWindow):
    def __init__(self, xaml_file, doc, uidoc, model, param_names):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc, self.uidoc, self.model = doc, uidoc, model
        self.param_names = param_names
        self.settings = Settings()
        self._busy = False
        self._cancel = False
        self._panel_rows = []  # [(CheckBox, PanelInfo)]

        self.DocTitleText.Text = doc.Title
        self.ModelStatsText.Text = u'{0} panels · {1} power circuits'.format(len(model.panels), len(model.systems))
        self._fill_lists()
        self._fill_panels()
        self._restore_settings()
        self._wire_events()
        self._refresh_mode()
        self.apply_panel_filters()
        if self._picked_in_revit:
            self._status('Ready', u'{0} panel(s) selected in Revit are ticked; all others are unticked.'.format(
                self._picked_in_revit))
        else:
            self._status('Ready', 'Tick the panels to export (only ticked panels are exported), then export.')

    # ------------------------------------------------------------------ setup
    def _fill_lists(self):
        for label, _ in er.SCOPES:
            self.ScopeCombo.Items.Add(label)
        for label, _ in er.PHASE_FILTERS:
            self.PhaseCombo.Items.Add(label)
        for label in UNIT_OPTIONS:
            self.DirectUnitCombo.Items.Add(label)
        self.ClassificationCombo.Items.Add(er.CONNECTOR_CLASSIFICATION)
        for name in self.param_names:
            self.ClassificationCombo.Items.Add(name)
            self.DirectParamCombo.Items.Add(name)
        for combo in (self.IdentifierCombo, self.Rule1Combo, self.Rule2Combo):
            combo.Items.Add('(none)')
            for name in self.param_names:
                combo.Items.Add(name)
        self.PrefixFilter.Items.Add(ALL_TYPES)
        for prefix in sorted(set(p.prefix for p in self.model.panels.values())):
            self.PrefixFilter.Items.Add(prefix)
        self.PrefixFilter.SelectedIndex = 0

    def _fill_panels(self):
        for info in self.model.sorted_panels():
            name = TextBlock()
            name.Text = info.name
            name.FontWeight = FontWeights.SemiBold
            meta = TextBlock()
            meta.Text = info.describe()
            meta.FontSize = 11
            meta.Foreground = _GRAY
            content = StackPanel()
            content.Orientation = Orientation.Vertical
            content.Children.Add(name)
            content.Children.Add(meta)
            cb = CheckBox()
            cb.Content = content
            cb.Tag = info.id
            cb.Margin = Thickness(4, 3, 4, 3)
            cb.ToolTip = u'{0}  (Element Id {1})'.format(info.name, info.id)
            cb.Checked += self._on_panel_checked
            cb.Unchecked += self._on_panel_checked
            self.PanelList.Items.Add(cb)
            self._panel_rows.append((cb, info))

    def _restore_settings(self):
        s = self.settings
        saved_mode = s.get('mode', 'circuited')
        self.ModeDirect.IsChecked = saved_mode == 'direct'
        self.ModePanels.IsChecked = saved_mode == 'panels'
        self.ModeCircuited.IsChecked = saved_mode not in ('direct', 'panels')
        self.ScopeCombo.SelectedIndex = min(s.get('scope_index', 0), self.ScopeCombo.Items.Count - 1)
        self.PhaseCombo.SelectedIndex = min(s.get('phase_index', 0), self.PhaseCombo.Items.Count - 1)
        self.DirectUnitCombo.SelectedIndex = min(s.get('unit_index', 0), 1)
        for control, cat in CATEGORY_CONTROLS:
            getattr(self, control).IsChecked = s.get('cat_' + cat, cat == 'OST_ElectricalFixtures')
        for name in REPORT_CHECKS:
            control = getattr(self, name)
            control.IsChecked = s.get('chk_' + name, bool(control.IsChecked))

        select_combo(self.ClassificationCombo, [s.get('classification', ''), 'Load Classification',
                                                'Classification', 'Power Category', 'EL_Load_Class'])
        if self.ClassificationCombo.SelectedIndex < 0:
            self.ClassificationCombo.SelectedIndex = 0
        select_combo(self.IdentifierCombo, [s.get('identifier', ''), 'MEE_ToServe', 'MEE_serve', 'Mark', 'Load Name'])
        if self.IdentifierCombo.SelectedIndex < 0:
            self.IdentifierCombo.SelectedIndex = 0
        self.Rule1Text.Text = s.get('rule1_text', 'Kitchen')
        self.Rule2Text.Text = s.get('rule2_text', '')
        select_combo(self.Rule1Combo, [s.get('rule1_param', ''), 'MEE_serve1', 'Mark'])
        select_combo(self.Rule2Combo, [s.get('rule2_param', '')])
        for combo in (self.Rule1Combo, self.Rule2Combo):
            if combo.SelectedIndex < 0:
                combo.SelectedIndex = 0
        select_combo(self.DirectParamCombo, [s.get('direct_param', '')] + er.DIRECT_PARAM_CANDIDATES)
        if self.DirectParamCombo.SelectedIndex < 0 and self.DirectParamCombo.Items.Count:
            self.DirectParamCombo.SelectedIndex = 0

        # Which panels start ticked:
        #   1. panels (or circuits) selected in Revit when the button was clicked
        #   2. else the panels ticked last time
        #   3. else all of them
        # Before, a Revit selection was ignored here, so every panel stayed
        # ticked and every circuit was exported.
        picked = self._panels_selected_in_revit()
        self._picked_in_revit = len(picked)
        saved_panels = set(n for n in s.get('panels', '').split('|') if n)
        matched = [cb for cb, info in self._panel_rows if info.name in saved_panels]
        for cb, info in self._panel_rows:
            if picked:
                cb.IsChecked = info.id in picked
            else:
                cb.IsChecked = (cb in matched) if matched else True

        stamp = datetime.date.today().strftime('%Y%m%d')
        default_name = u'{0}_Electrical_Loads_{1}.xlsx'.format(self._safe_title(), stamp)
        folder = s.get('output_folder', '')
        if not folder or not os.path.isdir(folder):
            folder = os.path.join(os.path.expanduser('~'), 'Documents')
        self.OutputPath.Text = os.path.join(folder, default_name)

    def _panels_selected_in_revit(self):
        """Ids of the panels in the current Revit selection: the panels
        themselves, and the panel of any selected circuit."""
        found = set()
        try:
            selected = list(self.uidoc.Selection.GetElementIds())
        except Exception:
            return found
        for eid in selected:
            key = er.eid_int(eid)
            if key in self.model.panels:
                found.add(key)
                continue
            try:
                element = self.doc.GetElement(eid)
                base = er.base_equipment(element) if isinstance(element, er.ElectricalSystem) else None
                if base is not None and er.eid_int(base.Id) in self.model.panels:
                    found.add(er.eid_int(base.Id))
            except Exception:
                pass
        return found

    def _panel_ids_for_export(self):
        """Ids of the ticked panels, or None when the user cancels.

        A panel that is ticked but hidden by the search box / type filter /
        "hide empty panels" still counted, which is how "I picked three
        panels" exported every circuit in the model. When ticked panels are
        hidden, the user is now asked which set is meant."""
        ticked = [(cb, info) for cb, info in self._panel_rows if cb.IsChecked]
        shown = [(cb, info) for cb, info in ticked if cb.Visibility == Visibility.Visible]
        hidden = len(ticked) - len(shown)
        if hidden and shown:
            only_shown = u'Only the {0} shown'.format(len(shown))
            all_ticked = u'All {0} ticked'.format(len(ticked))
            choice = forms.alert(
                u'{0} ticked panel(s) are hidden by the search / filter.\n\n'
                u'Export only the {1} ticked panel(s) you can see in the list, or all {2} ticked '
                u'panels?'.format(hidden, len(shown), len(ticked)),
                title=TITLE, options=[only_shown, all_ticked, 'Cancel'])
            if choice == only_shown:
                for cb, _ in self._panel_rows:
                    if cb.Visibility != Visibility.Visible:
                        cb.IsChecked = False        # make the list say what is exported
                ticked = shown
            elif choice != all_ticked:
                return None
        return set(int(cb.Tag) for cb, _ in ticked)

    def _save_settings(self):
        s = self.settings
        s.set('mode', 'direct' if self.ModeDirect.IsChecked else
              'panels' if self.ModePanels.IsChecked else 'circuited')
        s.set('scope_index', self.ScopeCombo.SelectedIndex)
        s.set('phase_index', self.PhaseCombo.SelectedIndex)
        s.set('unit_index', self.DirectUnitCombo.SelectedIndex)
        for control, cat in CATEGORY_CONTROLS:
            s.set('cat_' + cat, bool(getattr(self, control).IsChecked))
        for name in REPORT_CHECKS:
            s.set('chk_' + name, bool(getattr(self, name).IsChecked))
        s.set('classification', str(self.ClassificationCombo.SelectedItem or ''))
        s.set('identifier', str(self.IdentifierCombo.SelectedItem or ''))
        s.set('rule1_text', self.Rule1Text.Text or '')
        s.set('rule2_text', self.Rule2Text.Text or '')
        s.set('rule1_param', str(self.Rule1Combo.SelectedItem or ''))
        s.set('rule2_param', str(self.Rule2Combo.SelectedItem or ''))
        s.set('direct_param', str(self.DirectParamCombo.SelectedItem or ''))
        s.set('panels', '|'.join(info.name for cb, info in self._panel_rows if cb.IsChecked))
        s.set('output_folder', os.path.dirname(self.OutputPath.Text or ''))
        s.save()

    def _wire_events(self):
        self.SearchBox.TextChanged += lambda s, e: self.apply_panel_filters()
        self.PrefixFilter.SelectionChanged += lambda s, e: self.apply_panel_filters()
        self.PhaseCombo.SelectionChanged += lambda s, e: self.apply_panel_filters()
        self.HideEmptyPanels.Checked += lambda s, e: self.apply_panel_filters()
        self.HideEmptyPanels.Unchecked += lambda s, e: self.apply_panel_filters()
        self.ModeCircuited.Checked += lambda s, e: self._refresh_mode()
        self.ModeDirect.Checked += lambda s, e: self._refresh_mode()
        self.ModePanels.Checked += lambda s, e: self._refresh_mode()
        self.SelectVisibleButton.Click += lambda s, e: self._set_visible_checked(True)
        self.ClearVisibleButton.Click += lambda s, e: self._set_visible_checked(False)
        self.BrowseButton.Click += self.on_browse
        self.ExportButton.Click += self.on_export
        self.CloseButton.Click += self.on_close
        self.Closing += self.on_closing

    # ------------------------------------------------------------- UI state
    def _safe_title(self):
        title = os.path.splitext(self.doc.Title or 'Model')[0]
        return ''.join(c if c.isalnum() or c in '-_ ' else '_' for c in title).strip() or 'Model'

    def _status(self, text, detail=''):
        self.StatusText.Text = text
        self.DetailText.Text = detail or ''
        do_events()

    def _current_poles_filter(self):
        index = max(0, self.PhaseCombo.SelectedIndex)
        return er.PHASE_FILTERS[index][1]

    def apply_panel_filters(self):
        search = (self.SearchBox.Text or '').strip().lower()
        self.SearchHint.Visibility = Visibility.Collapsed if search else Visibility.Visible
        prefix = str(self.PrefixFilter.SelectedItem or ALL_TYPES)
        hide_empty = bool(self.HideEmptyPanels.IsChecked)
        poles = self._current_poles_filter()
        for cb, info in self._panel_rows:
            visible = ((not search or search in info.name.lower()) and
                       (prefix == ALL_TYPES or info.prefix == prefix) and
                       (not hide_empty or info.matching_circuits(poles) > 0))
            cb.Visibility = Visibility.Visible if visible else Visibility.Collapsed
        self._update_panel_count()

    def _update_panel_count(self):
        selected = sum(1 for cb, _ in self._panel_rows if cb.IsChecked)
        shown = sum(1 for cb, _ in self._panel_rows if cb.Visibility == Visibility.Visible)
        hidden = sum(1 for cb, _ in self._panel_rows
                     if cb.IsChecked and cb.Visibility != Visibility.Visible)
        text = u'{0} ticked · {1} shown · {2} total'.format(selected, shown, len(self._panel_rows))
        if hidden:
            text += u'  ·  {0} TICKED BUT HIDDEN'.format(hidden)
        self.PanelCountText.Text = text

    def _on_panel_checked(self, sender, args):
        self._update_panel_count()

    def _set_visible_checked(self, value):
        for cb, _ in self._panel_rows:
            if cb.Visibility == Visibility.Visible:
                cb.IsChecked = value

    def _refresh_mode(self):
        direct = bool(self.ModeDirect.IsChecked)
        self.PanelCard.IsEnabled = not direct
        self.PanelCard.Opacity = 0.5 if direct else 1.0
        self.DirectPanel.Visibility = Visibility.Visible if direct else Visibility.Collapsed
        for name in ('GroupByPanel', 'IncludeCircuit', 'IncludePanelCheck', 'IncludePanelMatrix', 'IncludePanelRevit', 'IncludeConnected'):
            getattr(self, name).IsEnabled = not direct
        # "Panel loads only": nothing about fixtures applies
        panels_only = bool(self.ModePanels.IsChecked)
        for name in ['ScopeCombo', 'PhaseCombo'] + [control for control, _ in CATEGORY_CONTROLS] + \
                [n for n in REPORT_CHECKS if n not in ('IncludeDiagnostics', 'OpenWhenDone', 'HideEmptyPanels')]:
            control = getattr(self, name, None)
            if control is not None:
                control.IsEnabled = not panels_only and (not direct or name not in (
                    'GroupByPanel', 'IncludeCircuit', 'IncludePanelCheck', 'IncludePanelMatrix',
                    'IncludePanelRevit', 'IncludeConnected'))
        self.ExportButton.Content = 'Export panel loads' if panels_only else 'Export to Excel'
        self.IncludeApparent.Content = 'Load parameter (kVA)' if direct else 'Apparent (kVA)'

    def _set_busy(self, busy):
        self._busy = busy
        self._cancel = False
        self.BodyGrid.IsEnabled = not busy
        self.ExportButton.IsEnabled = not busy
        self.CloseButton.Content = 'Cancel' if busy else 'Close'
        if not busy:
            self.Progress.Value = 0

    # --------------------------------------------------------------- events
    def on_browse(self, sender, args):
        current = self.OutputPath.Text or ''
        path = forms.save_file(file_ext='xlsx', init_dir=os.path.dirname(current),
                               default_name=os.path.basename(current) or 'Electrical_Loads.xlsx')
        if path:
            self.OutputPath.Text = path

    def on_close(self, sender, args):
        if self._busy:
            self._cancel = True
            self._status('Cancelling…')
        else:
            self.Close()

    def on_closing(self, sender, args):
        if self._busy:  # never tear the window down mid-loop
            self._cancel = True
            args.Cancel = True

    def _on_progress(self, index, total):
        self.Progress.Maximum = max(1, total)
        self.Progress.Value = index
        self.StatusText.Text = u'Reading loads… {0} of {1}'.format(index, total)
        do_events()

    # --------------------------------------------------------------- export
    def _read_options(self, panel_ids=None):
        direct = bool(self.ModeDirect.IsChecked)
        categories = [cat for control, cat in CATEGORY_CONTROLS if getattr(self, control).IsChecked]
        if not categories:
            raise ValueError('Tick at least one load category.')
        if panel_ids is None:
            panel_ids = set(int(cb.Tag) for cb, _ in self._panel_rows if cb.IsChecked)
        if not direct and not panel_ids:
            raise ValueError('Select at least one panel, or switch to Direct mode.')
        path = (self.OutputPath.Text or '').strip().strip('"')
        if not path.lower().endswith('.xlsx'):
            raise ValueError('The output file must end in .xlsx.')
        if not os.path.isabs(path):
            raise ValueError('Enter a full output path (e.g. C:\\Reports\\loads.xlsx).')
        direct_param = str(self.DirectParamCombo.SelectedItem or '')
        if direct and not direct_param:
            raise ValueError('Choose the direct load parameter.')

        def combo_value(combo):
            value = str(combo.SelectedItem or '')
            return '' if value == '(none)' else value

        opts = {
            'mode': rep.MODE_DIRECT if direct else rep.MODE_CIRCUITED,
            'scope': er.SCOPES[max(0, self.ScopeCombo.SelectedIndex)][1],
            'scope_label': er.SCOPES[max(0, self.ScopeCombo.SelectedIndex)][0],
            'phase_label': er.PHASE_FILTERS[max(0, self.PhaseCombo.SelectedIndex)][0],
            'poles_filter': self._current_poles_filter(),
            'categories': categories,
            'panel_ids': panel_ids,
            'classification_source': str(self.ClassificationCombo.SelectedItem or er.CONNECTOR_CLASSIFICATION),
            'default_identifier': combo_value(self.IdentifierCombo),
            'rules': [((self.Rule1Text.Text or '').strip(), combo_value(self.Rule1Combo)),
                      ((self.Rule2Text.Text or '').strip(), combo_value(self.Rule2Combo))],
            'direct_param': direct_param,
            'direct_label': direct_param,
            'unitless_is_kilo': self.DirectUnitCombo.SelectedIndex == 1,
            'output_path': path,
            'group_panel': bool(self.GroupByPanel.IsChecked),
            'group_classification': bool(self.GroupByClassification.IsChecked),
            'sum_by_identifier': bool(self.SumByIdentifier.IsChecked),
            'include_circuit': bool(self.IncludeCircuit.IsChecked),
            'include_count': bool(self.IncludeCount.IsChecked),
            'include_connected': bool(self.IncludeConnected.IsChecked),
            'include_demand': bool(self.IncludeDemand.IsChecked),
            'include_apparent': bool(self.IncludeApparent.IsChecked),
            'include_summary': bool(self.IncludeSummary.IsChecked),
            'include_panel_check': bool(self.IncludePanelCheck.IsChecked),
            'include_panel_matrix': bool(self.IncludePanelMatrix.IsChecked),
            'include_panel_revit': bool(self.IncludePanelRevit.IsChecked),
            'class_codes': er.classification_abbreviations(self.doc),
            'all_classes': er.classification_names(self.doc),
            'include_diagnostics': bool(self.IncludeDiagnostics.IsChecked),
        }
        if not rep.load_columns(opts):
            raise ValueError('Tick at least one load column (Connected, Demand or Apparent).')
        return opts

    def on_export(self, sender, args):
        if self._busy:
            return
        panel_ids = None
        if not self.ModeDirect.IsChecked:
            panel_ids = self._panel_ids_for_export()
            if panel_ids is None:
                return                      # cancelled at the "hidden panels" question
        if self.ModePanels.IsChecked:
            self._export_panels_only(panel_ids)
            return
        try:
            opts = self._read_options(panel_ids)
        except ValueError as ex:
            forms.alert(str(ex), title=TITLE)
            return
        self._set_busy(True)
        try:
            self._run_export(opts)
        except er.ScopeError as ex:
            self._status('Nothing to export', str(ex))
            forms.alert(str(ex), title=TITLE)
        except er.Cancelled:
            self._status('Export cancelled', 'No file was written.')
        except Exception as ex:
            self._status('Export failed', str(ex))
            details = traceback.format_exc()
            script.get_output().print_md('### Electrical Load Report – error\n```\n{0}\n```'.format(details))
            forms.alert('Export failed:\n{0}\n\nFull traceback is in the pyRevit output window.'.format(ex),
                        title=TITLE, warn_icon=True)
        finally:
            self._set_busy(False)

    def _export_panels_only(self, panel_ids):
        """'Panel loads only': one sheet with Revit's own load of each ticked
        panel per load classification. No fixture is read at all."""
        path = (self.OutputPath.Text or '').strip().strip('"')
        problem = None
        if not panel_ids:
            problem = 'Tick at least one panel.'
        elif not path.lower().endswith('.xlsx') or not os.path.isabs(path):
            problem = 'Enter a full output path that ends in .xlsx (e.g. C:\\Reports\\loads.xlsx).'
        if problem:
            forms.alert(problem, title=TITLE)
            return
        self._set_busy(True)
        try:
            self._status(u'Reading panel loads…')
            opts = {'mode': rep.MODE_CIRCUITED, 'panel_ids': panel_ids,
                    'class_codes': er.classification_abbreviations(self.doc),
                    'all_classes': er.classification_names(self.doc)}
            opts['panel_class_loads'] = self.model.panel_classification_loads(panel_ids, opts['all_classes'])
            sheet = rep.panel_revit_sheet(opts)
            if sheet is None:
                message = (u'The panels of this model have no per-classification load fields '
                           u'("<classification> Connected Apparent Power"), so there is nothing to '
                           u'export.\n\nCheck Manage > MEP Settings > Load Classifications.')
                self._status('Nothing to export', message.splitlines()[0])
                forms.alert(message, title=TITLE)
                return
            sheets = [sheet]
            if self.IncludeDiagnostics.IsChecked:
                try:
                    version = self.doc.Application.VersionNumber
                except Exception:
                    version = ''
                sheets.append(rep.diagnostics_sheet([
                    (u'Model', self.doc.Title), (u'Model path', self.doc.PathName or u'(not saved)'),
                    (u'Revit version', version), (u'Exported by', os.environ.get('USERNAME', '')),
                    (u'Exported at', datetime.datetime.now().strftime('%Y-%m-%d %H:%M')),
                    (u'Mode', u'Panel loads only (Revit panel values, no fixtures)'),
                    (u'Panels exported', len(sheet.rows))], {}, []))
            written = xl.write_workbook(path, sheets)
            self._save_settings()
            self._status(u'Exported the loads of {0} panel(s) · {1} classification column(s)'.format(
                len(sheet.rows), len(sheet.headers) - 3),
                (u'Target was locked, saved as {0}'.format(os.path.basename(written))
                 if written != path else written))
            if self.OpenWhenDone.IsChecked:
                try:
                    open_path(written)
                except Exception as ex:
                    forms.alert(u'Saved to:\n{0}\n\nCould not open it: {1}'.format(written, ex), title=TITLE)
            else:
                forms.alert(u'Report saved to:\n{0}'.format(written), title=TITLE)
        except Exception as ex:
            self._status('Export failed', str(ex))
            script.get_output().print_md('### Electrical Load Report – error\n```\n{0}\n```'.format(
                traceback.format_exc()))
            forms.alert('Export failed:\n{0}\n\nFull traceback is in the pyRevit output window.'.format(ex),
                        title=TITLE, warn_icon=True)
        finally:
            self._set_busy(False)

    def _run_export(self, opts):
        self._status('Collecting elements…')
        elements = er.collect_elements(self.doc, self.uidoc, self.model, opts['categories'], opts['scope'])
        collector = er.LoadCollector(self.doc, self.model, opts,
                                     progress=self._on_progress, cancelled=lambda: self._cancel)
        result = collector.run(elements)
        diag = result.diagnostics

        if not result.records:
            message = rep.explain_empty(diag, opts)
            self._status('No matching loads', message.splitlines()[0])
            forms.alert(message, title=TITLE)
            return

        self._status('Writing workbook…')
        panel_totals = None
        if opts['mode'] == rep.MODE_CIRCUITED and opts['include_panel_check']:
            panel_totals = self.model.panel_totals(opts['panel_ids'])
            exported_panels = set(r['panel'] for r in result.records)
            panel_totals = dict((k, v) for k, v in panel_totals.items() if k in exported_panels)
        if opts['mode'] == rep.MODE_CIRCUITED and opts.get('include_panel_revit'):
            # Revit's own per-classification loads of each ticked panel (what an
            # Electrical Equipment schedule shows). None = this model's panels
            # have no such parameters; the table is then summed from the loads.
            opts['panel_class_loads'] = self.model.panel_classification_loads(
                opts['panel_ids'], opts.get('all_classes') or [])
        sheets = rep.build_sheets(result.records, opts, panel_totals, self._run_info(opts),
                                  diag, result.warnings)
        path = xl.write_workbook(opts['output_path'], sheets)
        self._save_settings()

        total_kva = sum(r.get('apparent_kva') or 0.0 for r in result.records)
        panels = len(set(r['panel'] for r in result.records))
        self.Progress.Value = self.Progress.Maximum
        headline = u'Exported {0} load record(s) · {1} of {2} ticked panel(s) · {3:,.1f} kVA'.format(
            len(result.records), panels, len(opts['panel_ids']) if opts['mode'] == rep.MODE_CIRCUITED
            else panels, total_kva)
        notes = []
        if path != opts['output_path']:
            notes.append(u'Target was locked, saved as {0}'.format(os.path.basename(path)))
        if diag.get('circuit_share'):
            notes.append(u'{0} load(s) estimated from circuit totals'.format(diag['circuit_share']))
        if diag.get('no_own_load'):
            notes.append(u'{0} element(s) without a load of their own left out'.format(diag['no_own_load']))
        if result.warnings:
            notes.append(u'{0} warning(s) – see Diagnostics sheet'.format(len(result.warnings)))
        if (opts['mode'] == rep.MODE_CIRCUITED and opts.get('include_panel_revit')
                and not opts.get('panel_class_loads')):
            notes.append(u'no "Panel Loads by Class" sheet: the panels have no per-classification load fields')
        self._status(headline, u' · '.join(notes) or path)

        if self.OpenWhenDone.IsChecked:
            try:
                open_path(path)
            except Exception as ex:
                forms.alert(u'Saved to:\n{0}\n\nCould not open it: {1}'.format(path, ex), title=TITLE)
        else:
            choice = forms.alert(u'Report saved to:\n{0}'.format(path), title=TITLE,
                                 options=['Open file', 'Open folder', 'Close'])
            if choice == 'Open file':
                open_path(path)
            elif choice == 'Open folder':
                open_path(os.path.dirname(path))

    def _run_info(self, opts):
        try:
            version = self.doc.Application.VersionNumber
        except Exception:
            version = ''
        return [
            (u'Model', self.doc.Title),
            (u'Model path', self.doc.PathName or u'(not saved)'),
            (u'Revit version', version),
            (u'Exported by', os.environ.get('USERNAME', '')),
            (u'Exported at', datetime.datetime.now().strftime('%Y-%m-%d %H:%M')),
            (u'Mode', u'Direct ({0})'.format(opts['direct_param']) if opts['mode'] == rep.MODE_DIRECT
             else u'Circuited'),
            (u'Scope', opts['scope_label']),
            (u'Phase filter', opts['phase_label']),
            (u'Categories', u', '.join(c.replace('OST_', '') for c in opts['categories'])),
            (u'Panels selected', len(opts['panel_ids']) if opts['mode'] == rep.MODE_CIRCUITED else u'n/a'),
            (u'Classification source', opts['classification_source']),
        ]


def main():
    doc = revit.doc
    if doc is None:
        forms.alert('Open a project first.', title=TITLE, exitscript=True)
    if doc.IsFamilyDocument:
        forms.alert('This tool works in project files, not families.', title=TITLE, exitscript=True)
    model = er.ModelData(doc)
    names = er.parameter_names(doc, [cat for _, cat in CATEGORY_CONTROLS])
    window = LoadReportWindow(os.path.join(_HERE, 'ui.xaml'), doc, revit.uidoc, model, names)
    window.show_dialog()


if __name__ == '__main__':
    main()

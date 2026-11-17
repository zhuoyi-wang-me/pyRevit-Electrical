# -*- coding: utf-8 -*-
"""
WPF front-end built on pyRevit forms.WPFWindow.

Grids are bound to System.Data.DataTable (robust two-way binding under both
IronPython and pythonnet). The window is modal, so button handlers run inside the
external-command API context and may open transactions directly.
"""
import copy
import os

import clr
clr.AddReference('System.Data')
clr.AddReference('PresentationFramework')
import System
from System.Data import DataTable, DataRowState
from System.Collections.Generic import List
from System.Windows.Controls import DataGridEditingUnit
from Autodesk.Revit.DB import ElementId
from pyrevit import forms

from autocircuit import config, reporting
from autocircuit import revit_compat as rc
from autocircuit.collectors import filled_region_label
from autocircuit.engine import AutoCircuitEngine, ScopeError
from autocircuit.logger import log_file_path
from autocircuit.mapping import sync_mapping

_TYPES = {
    'str': System.Type.GetType('System.String'),
    'int': System.Type.GetType('System.Int32'),
    'float': System.Type.GetType('System.Double'),
    'bool': System.Type.GetType('System.Boolean'),
}
_NAME_SOURCES = ['comments', 'type_name']
_INCLUSION = ['fixture', 'room']


# ------------------------------------------------------------ table helpers
def make_table(columns):
    table = DataTable()
    for name, kind in columns:
        table.Columns.Add(name, _TYPES[kind])
    return table


def add_row(table, values):
    row = table.NewRow()
    for i, v in enumerate(values):
        row[i] = System.DBNull.Value if v is None else v
    table.Rows.Add(row)


def live_rows(table):
    return [r for r in table.Rows if r.RowState != DataRowState.Deleted]


def cell(row, col, default=None):
    v = row[col]
    if v is None or isinstance(v, System.DBNull):
        return default
    return v


def table_from(headers, rows):
    kinds = []
    for i in range(len(headers)):
        sample = next((r[i] for r in rows if r[i] not in (None, '')), '')
        kinds.append('bool' if isinstance(sample, bool) else
                     'int' if isinstance(sample, int) else
                     'float' if isinstance(sample, float) else 'str')
    table = make_table(list(zip(headers, kinds)))
    for r in rows:
        add_row(table, [None if v in (None, '') and kinds[i] != 'str' else
                        (v if kinds[i] != 'str' else u'{0}'.format(v if v is not None else ''))
                        for i, v in enumerate(r)])
    return table


def guarded(func):
    """Log + alert any exception raised by a UI handler instead of killing the window."""
    def wrapper(self, sender, args):
        try:
            return func(self, sender, args)
        except ScopeError as ex:
            self.log.warning('UI action %s - scope: %s', func.__name__, ex)
            forms.alert(str(ex), title='ElecAutoCircuit - scope')
        except Exception as ex:
            self.log.exception('UI action %s failed', func.__name__)
            forms.alert('Unexpected error: {0}\n\nDetails were written to:\n{1}'.format(ex, log_file_path()),
                        title='ElecAutoCircuit')
    wrapper.__name__ = func.__name__
    return wrapper


# ----------------------------------------------------------------- window
class AutoCircuitWindow(forms.WPFWindow):
    RULE_COLS = [('Rule', 'str'), ('MaxDevices', 'int'), ('MaxLoadVA', 'float'),
                 ('Dedicated', 'bool'), ('DedicatedAboveVA', 'float')]
    MAP_COLS = [('Classification', 'str'), ('PanelType', 'str'), ('Rule', 'str'), ('InProject', 'bool')]
    REGION_COLS = [('Region', 'str'), ('Levels', 'str'), ('PanelsByLocation', 'str'), ('OverridePanels', 'str')]

    def __init__(self, xaml_file, doc, uidoc, logger, state=None, picked_region_ids=None):
        """
        state             : settings dict carried over when the window reopens after picking
        picked_region_ids : UniqueIds of filled regions just picked / pre-selected -> the
                            scope switches to "selected filled region(s) only"
        """
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = doc
        self.uidoc = uidoc
        self.log = logger
        self.key = config.project_key(doc.Title, doc.PathName)
        self.settings = state if state is not None else config.load_settings(self.key, logger)
        self.plan = None
        self.request_pick = False
        self.pick_state = None
        scope = self.settings.setdefault('scope', {'mode': 'all', 'selected_region_ids': [], 'inclusion': 'fixture'})
        if picked_region_ids:
            scope['mode'] = 'selected_regions'
            scope['selected_region_ids'] = list(picked_region_ids)
        self.scope_ids = list(scope.get('selected_region_ids') or [])
        self.project_names = AutoCircuitEngine(doc, self.settings, logger).model_classifications()
        added, missing = self._sync_settings_map(self.settings)
        self._populate(self.settings)
        if picked_region_ids:
            self._status('{0} filled region(s) selected - only the area inside them will be circuited.'.format(
                len(picked_region_ids)))
        else:
            self._status(self._sync_message(added, missing))

    # ------------------------------------------------ project classifications
    def _sync_settings_map(self, s):
        rows, added, missing = sync_mapping(s.get('classification_map', []), self.project_names,
                                            s.get('classification_suggestions', []))
        s['classification_map'] = rows
        return added, missing

    def _sync_message(self, added, missing):
        msg = '{0} load classification(s) loaded from the project'.format(len(self.project_names))
        if added:
            msg += ' | {0} new (suggested panel type - please review)'.format(len(added))
        if missing:
            msg += ' | {0} saved row(s) not in project: {1}'.format(len(missing), ', '.join(missing))
        return msg

    # ---------------------------------------------------------- populate
    def _populate(self, s):
        g, r = s['general'], s.get('regions', {})
        self.tb_min_fixtures.Text = str(g.get('min_fixtures_before_merge', 3))
        self.tb_merge_distance.Text = str(g.get('max_merge_distance_m', 15))
        self.tb_max_util.Text = str(g.get('max_panel_utilization_pct', 100))
        self.cb_allow_merge.IsChecked = bool(g.get('allow_room_merging', True))
        self.cb_merge_tails.IsChecked = bool(g.get('merge_partial_tails', False))
        self.cb_prefer_level.IsChecked = bool(g.get('prefer_same_level', False))
        self.cb_linked_rooms.IsChecked = bool(g.get('use_linked_rooms', True))
        self.cb_spaces.IsChecked = bool(g.get('use_spaces', True))
        self.cb_read_family.IsChecked = bool(g.get('read_classification_from_family', True))
        self.cb_cross_region.IsChecked = bool(g.get('allow_cross_region', False))
        self.cb_all_or_nothing.IsChecked = bool(g.get('all_or_nothing', False))
        self.cb_auto_report.IsChecked = bool(g.get('auto_export_report', False))
        self.tb_report_folder.Text = g.get('report_folder', '') or ''

        sc = s.get('scope', {})
        self.scope_ids = list(sc.get('selected_region_ids') or [])
        self.rb_scope_selected.IsChecked = sc.get('mode') == 'selected_regions'
        self.rb_scope_all.IsChecked = sc.get('mode') != 'selected_regions'
        inc = sc.get('inclusion', 'fixture')
        self.cmb_scope_inclusion.SelectedIndex = _INCLUSION.index(inc) if inc in _INCLUSION else 0
        self._refresh_scope_text()

        self.tb_region_prefix.Text = r.get('filled_region_type_prefix', '')
        src = r.get('name_source', 'comments')
        self.cmb_region_name_source.SelectedIndex = _NAME_SOURCES.index(src) if src in _NAME_SOURCES else 0
        self.cb_region_level_filter.IsChecked = bool(r.get('level_filter', True))

        self.rules_table = make_table(self.RULE_COLS)
        for name in sorted(s.get('circuit_rules', {})):
            rule = s['circuit_rules'][name]
            add_row(self.rules_table, [name, int(rule.get('max_devices', 0) or 0),
                                       float(rule.get('max_load_va', 0) or 0), bool(rule.get('dedicated')),
                                       float(rule.get('dedicated_above_va', 0) or 0)])
        self.rules_grid.ItemsSource = self.rules_table.DefaultView

        self.map_table = make_table(self.MAP_COLS)
        for e in s.get('classification_map', []):
            add_row(self.map_table, [e.get('classification', ''), e.get('panel_type', ''), e.get('rule', ''),
                                     bool(e.get('in_project', False))])
        self.map_grid.ItemsSource = self.map_table.DefaultView

        self.region_table = make_table(self.REGION_COLS)
        for region, names in sorted(s.get('region_panel_overrides', {}).items()):
            add_row(self.region_table, [region, '', '', ', '.join(names)])
        self.region_grid.ItemsSource = self.region_table.DefaultView

    # ----------------------------------------------------------- harvest
    @staticmethod
    def _commit(*grids):
        for grid in grids:
            grid.CommitEdit(DataGridEditingUnit.Cell, True)
            grid.CommitEdit(DataGridEditingUnit.Row, True)

    @staticmethod
    def _num(textbox, label):
        try:
            return float(textbox.Text.strip().replace(',', '.'))
        except ValueError:
            raise ValueError('"{0}" must be a number.'.format(label))

    def _refresh_scope_text(self):
        if not self.scope_ids:
            self.txt_scope.Text = 'No filled region selected.'
            return
        by_comments = self.settings.get('regions', {}).get('name_source', 'comments') == 'comments'
        labels = [filled_region_label(self.doc, u, by_comments) for u in self.scope_ids]
        found = [l for l in labels if l]
        text = u'Selected: ' + (u', '.join(found) if found else u'none found')
        if len(found) < len(labels):
            text += u'  |  {0} selected region(s) no longer exist - pick again'.format(len(labels) - len(found))
        self.txt_scope.Text = text

    def _harvest(self, check_scope=True):
        """Read the UI into a validated settings dict (None if invalid)."""
        self._commit(self.rules_grid, self.map_grid, self.region_grid)
        s = copy.deepcopy(self.settings)
        g = s['general']
        try:
            g['min_fixtures_before_merge'] = int(self._num(self.tb_min_fixtures, 'Minimum devices before merging'))
            g['max_merge_distance_m'] = self._num(self.tb_merge_distance, 'Max merge distance')
            g['max_panel_utilization_pct'] = self._num(self.tb_max_util, 'Max panel utilization')
        except ValueError as ex:
            self.log.warning('Settings invalid: %s', ex)
            forms.alert(str(ex), title='ElecAutoCircuit')
            return None
        g['allow_room_merging'] = bool(self.cb_allow_merge.IsChecked)
        g['merge_partial_tails'] = bool(self.cb_merge_tails.IsChecked)
        g['prefer_same_level'] = bool(self.cb_prefer_level.IsChecked)
        g['use_linked_rooms'] = bool(self.cb_linked_rooms.IsChecked)
        g['use_spaces'] = bool(self.cb_spaces.IsChecked)
        g['read_classification_from_family'] = bool(self.cb_read_family.IsChecked)
        g['allow_cross_region'] = bool(self.cb_cross_region.IsChecked)
        g['all_or_nothing'] = bool(self.cb_all_or_nothing.IsChecked)
        g['auto_export_report'] = bool(self.cb_auto_report.IsChecked)
        g['report_folder'] = self.tb_report_folder.Text.strip()

        s['scope'] = {
            'mode': 'selected_regions' if self.rb_scope_selected.IsChecked else 'all',
            'selected_region_ids': list(self.scope_ids),
            'inclusion': _INCLUSION[max(0, self.cmb_scope_inclusion.SelectedIndex)],
        }
        s['regions'] = {
            'filled_region_type_prefix': self.tb_region_prefix.Text.strip(),
            'name_source': _NAME_SOURCES[max(0, self.cmb_region_name_source.SelectedIndex)],
            'level_filter': bool(self.cb_region_level_filter.IsChecked),
        }

        rules = {}
        for row in live_rows(self.rules_table):
            name = (cell(row, 'Rule', '') or '').strip()
            if not name:
                continue
            rule = dict(self.settings['circuit_rules'].get(name, {}))   # keep unknown keys
            rule.update({'max_devices': int(cell(row, 'MaxDevices', 0) or 0),
                         'max_load_va': float(cell(row, 'MaxLoadVA', 0) or 0),
                         'dedicated': bool(cell(row, 'Dedicated', False)),
                         'dedicated_above_va': float(cell(row, 'DedicatedAboveVA', 0) or 0)})
            rules[name] = rule
        s['circuit_rules'] = rules

        in_project = set(n.lower() for n in self.project_names)
        s['classification_map'] = [
            {'classification': (cell(r, 'Classification', '') or '').strip(),
             'panel_type': (cell(r, 'PanelType', '') or '').strip().upper(),
             'rule': (cell(r, 'Rule', '') or '').strip(),
             'in_project': (cell(r, 'Classification', '') or '').strip().lower() in in_project}
            for r in live_rows(self.map_table)
            if (cell(r, 'Classification', '') or '').strip()]

        overrides = {}
        for r in live_rows(self.region_table):
            names = [n.strip() for n in (cell(r, 'OverridePanels', '') or '').split(',') if n.strip()]
            if names:
                overrides[cell(r, 'Region', '')] = names
        s['region_panel_overrides'] = overrides

        errors = config.validate(s, check_scope=check_scope)
        if errors:
            self.log.warning('Settings invalid:\n- %s', '\n- '.join(errors))
            forms.alert('Please fix the following:\n\n- ' + '\n- '.join(errors), title='ElecAutoCircuit')
            return None
        self.settings = s
        return s

    # ------------------------------------------------------------ helpers
    def _status(self, text):
        self.status_text.Text = text
        self.log.info(text)

    def _show_plan(self, plan):
        headers, rows = reporting.circuit_rows(plan)
        self.preview_grid.ItemsSource = table_from(headers, rows).DefaultView
        ok = plan.successful()
        verb = 'created' if plan.mode == 'execute' else 'to create'
        self.txt_summary.Text = (
            u'Scope: ' + u'{0}'.format(plan.info.get('Scope', 'Whole model')) + u'\n' +
            u'{0} devices processed  |  {1} circuits {2}  |  {3} uncircuited  |  {4} room merges  |  '
            u'{5:,.0f} VA  |  {6} warnings  |  {7} errors  |  {8:.1f}s'.format(
                plan.total_fixtures, len(ok), verb, len(reporting.uncircuited(plan)), len(plan.merges),
                sum(c.load_va for c in ok), len(plan.warnings), len(plan.errors), plan.duration_s))

    def _require_plan(self):
        if self.plan is None:
            forms.alert('Run a Preview or Create Circuits first.', title='ElecAutoCircuit')
            return False
        return True

    def _default_report_folder(self):
        folder = self.tb_report_folder.Text.strip()
        return folder or os.path.join(os.path.expanduser('~'), 'Documents', 'AutoCircuit Reports')

    # ----------------------------------------------------- rule / mapping
    @guarded
    def btn_add_rule_click(self, sender, args):
        add_row(self.rules_table, ['New Rule', 10, 1800.0, False, 0.0])

    @guarded
    def btn_remove_rule_click(self, sender, args):
        for item in list(self.rules_grid.SelectedItems):
            item.Row.Delete()

    @guarded
    def btn_add_map_click(self, sender, args):
        add_row(self.map_table, ['', '', 'Equipment', False])   # e.g. a wildcard pattern

    @guarded
    def btn_remove_map_click(self, sender, args):
        for item in list(self.map_grid.SelectedItems):
            item.Row.Delete()

    @guarded
    def btn_import_classes_click(self, sender, args):
        """Re-read the project's Load Classifications and rebuild the mapping grid."""
        s = self._harvest()
        if s is None:
            return
        self.project_names = AutoCircuitEngine(self.doc, s, self.log).model_classifications()
        added, missing = self._sync_settings_map(s)
        self.settings = s
        self.map_table = make_table(self.MAP_COLS)
        for e in s['classification_map']:
            add_row(self.map_table, [e['classification'], e['panel_type'], e['rule'], e['in_project']])
        self.map_grid.ItemsSource = self.map_table.DefaultView
        self._status(self._sync_message(added, missing))

    # ---------------------------------------------------------------- scope
    @guarded
    def btn_pick_regions_click(self, sender, args):
        """Close, let the user pick in the view, reopen (loop lives in script.py)."""
        s = self._harvest(check_scope=False)
        if s is None:
            return
        self.pick_state = s
        self.request_pick = True
        self.Close()

    @guarded
    def btn_clear_regions_click(self, sender, args):
        self.scope_ids = []
        self.rb_scope_all.IsChecked = True
        self._refresh_scope_text()
        self._status('Scope reset to the whole model.')

    # -------------------------------------------------------------- regions
    @guarded
    def btn_scan_regions_click(self, sender, args):
        s = self._harvest()
        if s is None:
            return
        rows = AutoCircuitEngine(self.doc, s, self.log).scan_regions()
        overrides = s.get('region_panel_overrides', {})
        self.region_table.Rows.Clear()
        for r in rows:
            add_row(self.region_table, [r['region'], r['levels'], r['panels'],
                                        ', '.join(overrides.get(r['region'], []))])
        for region in sorted(set(overrides) - set(r['region'] for r in rows)):
            add_row(self.region_table, [region, '(not found in model)', '', ', '.join(overrides[region])])
        unassigned = s['general'].get('unassigned_region_name', '<No Region>')
        self._status('{0} region(s) found.'.format(len([r for r in rows if r['region'] != unassigned])))

    # ------------------------------------------------------------ execution
    @guarded
    def btn_preview_click(self, sender, args):
        s = self._harvest()
        if s is None:
            return
        self._status('Analysing model...')
        self.plan = AutoCircuitEngine(self.doc, s, self.log).analyse()
        self._show_plan(self.plan)
        self._status('Preview complete - no changes were made to the model.')

    @guarded
    def btn_create_click(self, sender, args):
        s = self._harvest()
        if s is None:
            return
        if not forms.alert('Create circuits in "{0}"?\n\nThe model is re-analysed first. The whole run is a '
                           'single Undo step.'.format(self.doc.Title), yes=True, no=True, title='ElecAutoCircuit'):
            return
        self._status('Creating circuits...')
        self.plan = AutoCircuitEngine(self.doc, s, self.log).execute()
        self._show_plan(self.plan)
        msg = 'Done: {0} circuit(s) created.'.format(len(self.plan.successful()))
        if s['general'].get('auto_export_report'):
            folder = self._default_report_folder()
            sheets = reporting.build_sheets(self.plan)
            base = reporting.default_basename(self.plan)
            reporting.export_csv(sheets, folder, base)
            reporting.export_xlsx(sheets, os.path.join(folder, base + '.xlsx'))
            msg += ' Report saved to ' + folder
        self._status(msg)
        if self.plan.errors:
            forms.alert('{0}\n\n{1} error(s), first:\n{2}'.format(msg, len(self.plan.errors), self.plan.errors[0]),
                        title='ElecAutoCircuit')

    @guarded
    def btn_export_csv_click(self, sender, args):
        if not self._require_plan():
            return
        folder = forms.pick_folder(title='Folder for CSV report')
        if folder:
            paths = reporting.export_csv(reporting.build_sheets(self.plan), folder,
                                         reporting.default_basename(self.plan))
            self._status('{0} CSV file(s) written to {1}'.format(len(paths), folder))

    @guarded
    def btn_export_xlsx_click(self, sender, args):
        if not self._require_plan():
            return
        path = forms.save_file(file_ext='xlsx', default_name=reporting.default_basename(self.plan),
                               title='Save Excel report')
        if path:
            reporting.export_xlsx(reporting.build_sheets(self.plan), path)
            self._status('Excel report written to ' + path)

    @guarded
    def btn_select_uncircuited_click(self, sender, args):
        if not self._require_plan():
            return
        ids = sorted(set(f.element_id for f, _ in reporting.uncircuited(self.plan)))
        self.uidoc.Selection.SetElementIds(List[ElementId]([rc.to_eid(i) for i in ids]))
        self._status('{0} uncircuited element(s) selected - close the window to inspect.'.format(len(ids)))

    @guarded
    def btn_browse_report_click(self, sender, args):
        folder = forms.pick_folder(title='Default report folder')
        if folder:
            self.tb_report_folder.Text = folder

    # -------------------------------------------------------------- settings
    @guarded
    def btn_save_click(self, sender, args):
        s = self._harvest()
        if s is not None:
            config.save_settings(s, self.key)
            self._status('Settings saved to ' + config.settings_path(self.key))

    @guarded
    def btn_reset_click(self, sender, args):
        if forms.alert('Reset all settings to factory defaults?', yes=True, no=True, title='ElecAutoCircuit'):
            self.settings = config.load_defaults()
            self._sync_settings_map(self.settings)
            self._populate(self.settings)
            self._status('Defaults restored (not yet saved).')

    @guarded
    def btn_export_settings_click(self, sender, args):
        s = self._harvest()
        if s is None:
            return
        path = forms.save_file(file_ext='json', default_name='AutoCircuit_settings', title='Export settings')
        if path:
            config.write_json(path, s)
            self._status('Settings exported to ' + path)

    @guarded
    def btn_import_settings_click(self, sender, args):
        path = forms.pick_file(file_ext='json', title='Import settings')
        if not path:
            return
        s = config.deep_merge(config.load_defaults(), config.read_json(path))
        errors = config.validate(s)
        if errors:
            forms.alert('Imported file is invalid:\n\n- ' + '\n- '.join(errors), title='ElecAutoCircuit')
            return
        self.settings = s
        self._sync_settings_map(s)
        self._populate(s)
        self._status('Settings imported from {0} (not yet saved).'.format(path))

    def btn_close_click(self, sender, args):
        self.Close()

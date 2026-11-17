# -*- coding: utf-8 -*-
"""Cable Tray QA/QC - the main window and the order of the steps.

    1. Select the cable tray calculation Excel      excel_reader.py
    2. Scan the annotations on the Revit sheets     revit_reader.py
    3. Compare Excel with Revit                     annotation_parser.py + comparison.py
    4. Show, highlight, navigate, export            highlight.py + report.py

WHY THIS IS NOT IN script.py
The window is modeless: it stays open after the button script has finished.
pyRevit clears the global names of a button script when it ends, so a button
clicked later could no longer see anything script.py had imported - every
action failed with "name 'time' is not defined". The names of an imported
MODULE are not cleared, so the window lives here and script.py only starts it.

The check itself is read-only and "Highlight" uses Revit's ordinary selection.
The one thing that writes to the model is "Colour by issue" (view graphic
overrides in the active view), and only when the user clicks it.
"""
import os
import time

from pyrevit import forms, revit, script

import config
import comparison
import excel_reader
import highlight
import report
import revit_reader
import sheet_picker

XAML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui.xaml')
_OPEN_WINDOWS = []          # keeps the modeless window (and its ExternalEvent) alive

NA = config.NOT_AVAILABLE
# check mode -> its radio button in ui.xaml
MODES = {'model': 'rb_model', 'sheets': 'rb_sheets', 'both': 'rb_both'}
ALL_ROWS, ERRORS_ONLY = 'All results', 'Errors only'


def _like(text):
    """Escape user text for a DataView RowFilter LIKE expression."""
    out = []
    for character in text:
        if character in '[]*%':
            out.append('[' + character + ']')
        elif character == "'":
            out.append("''")
        else:
            out.append(character)
    return ''.join(out)


def _number(value):
    return NA if value is None else '{0:g}'.format(value)


class CableTrayQAWindow(forms.WPFWindow):
    def __init__(self, uidoc):
        forms.WPFWindow.__init__(self, XAML_PATH)
        self.uidoc = uidoc
        self.doc = uidoc.Document
        self.excel = None                 # excel_reader.ExcelData
        self.result = None                # comparison.QAResult
        self.readers_used = ''
        # Sheets are listed once, here, while the button script is still inside
        # Revit's API context; the picker dialog then needs no Revit access.
        self.sheets = revit_reader.list_sheets(self.doc)
        self.sheet_ids = None             # None = all sheets, else a set of sheet ids
        self.sheet_counts = {}            # sheet id -> annotations found in the last check
        # text parameters found on cable trays, for the "Tag parameter" list
        self.tray_parameters = revit_reader.list_tray_parameters(self.doc)
        self.coloured = {}                # view id -> element ids coloured by "Colour by issue"
        self.runner = highlight.RevitRunner(on_error=self._revit_error)
        self.cfg = script.get_config()
        self._build_table()
        self._init_controls()
        self._wire()
        self._show_summary(None)
        self._set_status('Step 1: select the cable tray calculation Excel file.')

    # ------------------------------------------------------------------ setup
    def _init_controls(self):
        self.model_tb.Text = self.doc.Title
        self.chk_tags.IsChecked = config.READ_TAGS
        self.chk_text.IsChecked = config.READ_TEXT_NOTES
        self.chk_generic.IsChecked = config.READ_GENERIC_ANNOTATIONS
        self.chk_sheets.IsChecked = config.ONLY_VIEWS_ON_SHEETS
        for scope in config.DUPLICATE_SCOPES:
            self.dup_cb.Items.Add(config.DUPLICATE_SCOPE_LABELS[scope])
        self.dup_cb.SelectedIndex = config.DUPLICATE_SCOPES.index(config.DUPLICATE_SCOPE)
        for label in [ALL_ROWS, ERRORS_ONLY] + list(config.STATUS_ORDER):
            self.status_cb.Items.Add(label)
        self.status_cb.SelectedIndex = 0
        self._show_sheet_choice()
        self.chk_untagged.IsChecked = False
        self.chk_cover.IsChecked = bool(self.cfg.get_option('flag_not_tagged', config.FLAG_NOT_TAGGED))
        self._fill_tray_parameters()
        self._build_legend()
        # What to check. The default is the cable trays in the MODEL; the
        # last choice is remembered.
        mode = self.cfg.get_option('check_mode', config.CHECK_MODE)
        if mode not in MODES:
            mode = config.CHECK_MODE
        getattr(self, MODES[mode]).IsChecked = True
        self._mode_changed()

    def mode(self):
        for name, control in MODES.items():
            if getattr(self, control).IsChecked:
                return name
        return config.CHECK_MODE

    def _mode_changed(self, sender=None, args=None):
        """Show only the options that belong to the chosen check."""
        from System.Windows import Visibility
        mode = self.mode()
        trays = mode in ('model', 'both')
        sheets = mode in ('sheets', 'both')
        self.chk_trays.IsChecked = trays
        self.trays_panel.Visibility = Visibility.Visible if trays else Visibility.Collapsed
        self.ann_panel.Visibility = Visibility.Visible if sheets else Visibility.Collapsed
        # "not tagged on a sheet" needs to know which sheets count
        cover = trays and bool(self.chk_cover.IsChecked)
        self.sheets_panel.Visibility = (Visibility.Visible if sheets or cover
                                        else Visibility.Collapsed)
        self._trays_toggled()
        try:
            self.cfg.check_mode = mode
            self.cfg.flag_not_tagged = bool(self.chk_cover.IsChecked)
            script.save_config()
        except Exception:
            pass
        if sender is not None:
            if self.result is not None:
                self._set_status('Check changed - click "Run check" to update the results.')
            elif self.excel is not None:
                self._set_status('Click "Run check".')

    def _fill_tray_parameters(self):
        """(auto-detect) + every text parameter found on the cable trays."""
        previous = self.tray_param_cb.SelectedItem
        self.tray_param_cb.Items.Clear()
        self.tray_param_cb.Items.Add(revit_reader.AUTO_PARAMETER)
        for name in self.tray_parameters:
            self.tray_param_cb.Items.Add(name)
        names = [revit_reader.AUTO_PARAMETER] + list(self.tray_parameters)
        self.tray_param_cb.SelectedIndex = names.index(previous) if previous in names else 0

    def _trays_toggled(self, sender=None, args=None):
        on = bool(self.chk_trays.IsChecked)
        self.tray_param_cb.IsEnabled = on
        self.chk_untagged.IsEnabled = on
        self.chk_cover.IsEnabled = on

    def _scan_plan(self):
        """(readers to run, readers the comparison reports on, coverage mode).

        With "Flag trays that are not tagged on a sheet" on, the cable tray
        tags on the sheets are read too. In the model-only check they are used
        for that alone and are not listed ('only')."""
        shown = self._readers()
        if 'cable_trays' not in shown or not self.chk_cover.IsChecked:
            return shown, shown, None
        if self.mode() == 'model':
            return ['tags'] + shown, shown, 'only'
        if len(shown) > 1:
            return shown, shown, 'also'
        return shown, shown, None

    # ------------------------------------------------------------------ model colours
    def _build_legend(self):
        """One chip per kind of issue, in the colour it gets in the model.
        Clicking a chip shows only that kind in the list."""
        from System.Windows import CornerRadius, Thickness
        from System.Windows.Controls import Border, TextBlock
        from System.Windows.Input import Cursors
        from System.Windows.Media import Color, SolidColorBrush, Brushes
        self.legend_panel.Children.Clear()
        for status in config.STATUS_ORDER:
            rgb = config.MODEL_COLOURS.get(status)
            if rgb is None or status == config.PASS:
                continue
            chip = Border()
            chip.Tag = status
            chip.CornerRadius = CornerRadius(10)
            chip.Padding = Thickness(9, 2, 9, 2)
            chip.Margin = Thickness(0, 0, 6, 0)
            chip.Cursor = Cursors.Hand
            chip.Background = SolidColorBrush(Color.FromRgb(rgb[0], rgb[1], rgb[2]))
            chip.ToolTip = 'Show only these rows'
            text = TextBlock()
            text.Text = status
            text.FontSize = 10.5
            text.Foreground = Brushes.White
            chip.Child = text
            chip.MouseLeftButtonUp += self._legend_clicked
            self.legend_panel.Children.Add(chip)

    def _legend_clicked(self, sender, args):
        self.status_cb.SelectedItem = sender.Tag

    def _shown_rows(self):
        """The result rows that pass the 'Show' filter and the search box."""
        if self.result is None:
            return []
        return [self.result.rows[int(view_row.Row['Key'])] for view_row in self.table.DefaultView]

    def _wire(self):
        for control in MODES.values():
            getattr(self, control).Checked += self._mode_changed
        self.btn_sheets.Click += self._choose_sheets
        self.btn_all_sheets.Click += self._all_sheets
        self.btn_browse.Click += self._browse
        self.btn_run.Click += self._run_clicked
        self.btn_highlight.Click += self._highlight
        self.btn_clear.Click += self._clear
        self.btn_colour.Click += self._colour
        self.btn_uncolour.Click += self._uncolour
        self.chk_cover.Click += self._mode_changed
        self.btn_sheet.Click += self._open_sheet
        self.btn_view.Click += self._open_view
        self.btn_export.Click += self._export
        self.btn_close.Click += lambda sender, args: self.Close()
        self.grid.MouseDoubleClick += self._open_sheet
        self.grid.SelectionChanged += self._update_buttons
        self.status_cb.SelectionChanged += self._apply_filter
        self.search_tb.TextChanged += self._apply_filter
        self.Closed += self._closed

    def _build_table(self):
        import clr
        clr.AddReference('System.Data')
        import System
        from System.Data import DataTable
        table = DataTable('results')
        for name, kind in (('Key', System.Int32), ('Status', System.String), ('Tag', System.String),
                           ('Sheet', System.String), ('SheetName', System.String),
                           ('View', System.String), ('ExcelW', System.String),
                           ('ExcelH', System.String), ('RevitW', System.String),
                           ('RevitH', System.String), ('Qty', System.String),
                           ('Description', System.String), ('Comments', System.String),
                           ('ElementId', System.String), ('Family', System.String),
                           ('IsError', System.Boolean)):
            table.Columns.Add(name, clr.GetClrType(kind))
        self.table = table
        self.grid.ItemsSource = table.DefaultView

    # ------------------------------------------------------------------ helpers
    def _set_status(self, text):
        self.status_tb.Text = text

    def _revit_error(self, error):
        self._set_status(u'Revit could not do that: {0}'.format(error))

    def _closed(self, sender, args):
        self.runner.dispose()

    def _readers(self):
        mode = self.mode()
        names = []
        if mode in ('sheets', 'both'):
            if self.chk_tags.IsChecked:
                names.append('tags')
            if self.chk_text.IsChecked:
                names.append('text_notes')
            if self.chk_generic.IsChecked:
                names.append('generic_annotations')
        if mode in ('model', 'both'):
            names.append('cable_trays')
        return names

    def _same_document(self, uidoc):
        try:
            return uidoc is not None and uidoc.Document.Equals(self.doc)
        except Exception:
            return False

    # ------------------------------------------------------------------ sheets to check
    def _selected_sheet_ids(self):
        """The ticked sheets that still exist, or None for 'all sheets'."""
        if self.sheet_ids is None:
            return None
        existing = set(sheet['id'] for sheet in self.sheets)
        return self.sheet_ids & existing

    def _show_sheet_choice(self):
        total = len(self.sheets)
        chosen = self._selected_sheet_ids()
        if chosen is None or len(chosen) == total:
            self.sheets_tb.Text = 'All {0:,} sheets'.format(total)
            self.btn_all_sheets.IsEnabled = False
            return
        numbers = [sheet['number'] for sheet in self.sheets if sheet['id'] in chosen]
        shown = ', '.join(numbers[:8]) + (' ...' if len(numbers) > 8 else '')
        self.sheets_tb.Text = u'{0:,} of {1:,} sheets:  {2}'.format(len(chosen), total, shown)
        self.btn_all_sheets.IsEnabled = True

    def _choose_sheets(self, sender, args):
        if not self.sheets:
            forms.alert('This model has no sheets.', title=config.TITLE)
            return
        picker = sheet_picker.SheetPickerWindow(self.sheets, self._selected_sheet_ids(),
                                                self.sheet_counts)
        try:
            picker.Owner = self
        except Exception:
            pass
        picker.ShowDialog()
        if picker.result is None:
            return                          # cancelled: keep the previous choice
        self.sheet_ids = None if len(picker.result) == len(self.sheets) else set(picker.result)
        self._show_sheet_choice()
        self._sheets_changed()

    def _all_sheets(self, sender, args):
        self.sheet_ids = None
        self._show_sheet_choice()
        self._sheets_changed()

    def _sheets_changed(self):
        if self.result is not None:
            self._set_status('Sheet selection changed - click "Run check" to update the results.')
        elif self.excel is not None:
            self._set_status('Click "Run check" to scan the selected sheets and compare.')

    # ------------------------------------------------------------------ step 1: Excel
    def _browse(self, sender=None, args=None):
        """Ask for the workbook. The path is never stored in the code; only the
        last FOLDER is remembered so the dialog opens where you left it."""
        folder = self.cfg.get_option('last_folder', '')
        path = forms.pick_file(
            files_filter='Cable tray calculation (*.xlsx;*.xlsm;*.csv)|*.xlsx;*.xlsm;*.csv',
            init_dir=folder if folder and os.path.isdir(folder) else '',
            title='Select the Cable Tray Calculation Excel file')
        if not path:
            return False
        return self.load_excel(path)

    def load_excel(self, path):
        try:
            data = excel_reader.read_calculation(path)
        except excel_reader.ExcelError as error:
            forms.alert(u'{0}'.format(error), title=config.TITLE)
            return False
        except Exception as error:
            forms.alert(u'The Excel file could not be read.\n\n{0}'.format(error), title=config.TITLE)
            return False
        self.excel = data
        self.excel_tb.Text = path
        try:
            self.cfg.last_folder = os.path.dirname(path)
            script.save_config()
        except Exception:
            pass
        info = (u'Worksheet "{0}", header row {1}: {2} calculation tag(s). Columns used: '
                u'{3} / {4} / {5}.'.format(data.sheet, data.header_row, len(data.by_tag),
                                           data.headers['tag'], data.headers['width'],
                                           data.headers['height']))
        if data.duplicates:
            info += u' {0} tag(s) are listed more than once.'.format(len(data.duplicates))
        if data.skipped_blank:
            info += u' {0} row(s) without a tag were skipped.'.format(data.skipped_blank)
        bad = len([row for row in data.rows if row['problems']])
        if bad:
            info += u' {0} row(s) have a width or height that is not a number.'.format(bad)
        self.excel_info_tb.Text = info
        self._set_status('Step 2: click "Run check" to scan the sheets and compare.')
        return True

    # ------------------------------------------------------------------ steps 2-3: scan + compare
    def _run_clicked(self, sender, args):
        if self.excel is None and not self._browse():
            return
        if not self._readers():
            forms.alert('Tick at least one kind of annotation to read.', title=config.TITLE)
            return
        self._set_status('Scanning the sheets...')
        self.runner.run(lambda uiapp: self.run_check(uiapp.ActiveUIDocument))

    def run_check(self, uidoc):
        """Scan + compare. Must run in Revit's API context (the button script
        itself, or the ExternalEvent)."""
        if not self._same_document(uidoc):
            # the user switched model: check the one that is active now
            self.uidoc, self.doc = uidoc, uidoc.Document
            self.model_tb.Text = self.doc.Title
            self.sheet_ids, self.sheet_counts = None, {}      # another model, other sheets
            self.tray_parameters = revit_reader.list_tray_parameters(self.doc)
            self._fill_tray_parameters()
        from System.Windows.Input import Cursors
        self.Cursor = Cursors.Wait
        started = time.time()
        try:
            # re-read the workbook so an edit made in Excel since is picked up
            if self.excel is not None and os.path.isfile(self.excel.path):
                try:
                    self.excel = excel_reader.read_calculation(self.excel.path)
                except Exception:
                    pass                  # keep the list already loaded
            to_read, readers, coverage = self._scan_plan()
            scope = config.DUPLICATE_SCOPES[max(0, self.dup_cb.SelectedIndex)]
            # sheets added / renamed / deleted since the window opened
            self.sheets = revit_reader.list_sheets(self.doc)
            chosen = self._selected_sheet_ids()
            if chosen is not None and not chosen:
                chosen = None               # every ticked sheet was deleted: check all
                self.sheet_ids = None
            self._show_sheet_choice()
            annotations, info = revit_reader.read_annotations(
                self.doc, self.excel.tags(), to_read,
                bool(self.chk_sheets.IsChecked) or coverage == 'only', chosen,
                tray_parameter=self.tray_param_cb.SelectedItem,
                report_untagged=bool(self.chk_untagged.IsChecked))
            total_sheets = len(self.sheets)
            checked_sheets = total_sheets if chosen is None else len(chosen)
            self.result = comparison.compare(self.excel, annotations, scope,
                                             checked_sheets, total_sheets, readers, coverage)
            # remembered with the result: the selection may change before the export
            self.result_sheets_text = self._sheets_checked_text()
            # annotations per sheet, for the picker's "only annotated sheets" filter
            for sheet_id in (chosen if chosen is not None else [s['id'] for s in self.sheets]):
                self.sheet_counts[sheet_id] = 0
            for record in annotations:
                for sheet_id in record.get('sheet_ids') or []:
                    if sheet_id in self.sheet_counts:
                        self.sheet_counts[sheet_id] += 1
            self.readers_used = ', '.join(
                '{0} {1}'.format(count, name.replace('_', ' ')) for name, count in info['counts'].items())
        finally:
            self.Cursor = Cursors.Arrow
        self._fill_table()
        self._show_summary(self.result.summary)
        only_trays = readers == ['cable_trays']
        if only_trays:
            message = u'Cable trays checked in {0:.1f} s.'.format(time.time() - started)
        else:
            message = u'{0} annotation(s) on {1} sheet(s) checked in {2:.1f} s ({3}).'.format(
                len([a for a in annotations if a.get('kind') != 'Cable Tray']), checked_sheets,
                time.time() - started, self.readers_used or 'nothing read')
        if 'cable_trays' in readers:
            counts = info.get('tray_counts') or {}
            used = info.get('tray_parameters') or []
            message += u' Cable trays: {0:,} in the model, {1:,} with a tag, {2:,} without.'.format(
                counts.get('trays', 0), counts.get('tagged', 0), counts.get('untagged', 0))
            if used:
                message += u' Tag read from parameter: {0}.'.format(u', '.join(used))
            else:
                message += (u' No cable tray parameter holds a tag from the Excel - choose the '
                            u'parameter in the "Tag parameter" list.')
        if coverage:
            flagged = self.result.summary.get('not_tagged', 0)
            scanned = info['counts'].get('tags', 0)
            if coverage == 'only':
                message = message.rstrip('.') + u'. {0:,} tag(s) read to see which trays are tagged.'.format(scanned)
            message += (u' {0:,} tagged run(s) carry no tag on {1}.'.format(
                flagged, 'the {0} checked sheet(s)'.format(checked_sheets)
                if checked_sheets < total_sheets else 'any sheet') if flagged else
                u' Every tagged run carries a tag on a sheet.')
        if self.result.summary['partial']:
            message += (u' Only {0} of {1} sheets were checked, so MISSING means "not on these '
                        u'sheets".'.format(checked_sheets, total_sheets))
        if info.get('via_dependent'):
            message += u' {0:,} of them were found through dependent views.'.format(info['via_dependent'])
        for reason, count in info['skipped'].items():
            message += u' Skipped: {0} {1}.'.format(count, reason)
        by_family = self.result.summary.get('invalid_by_family') or {}
        if by_family:
            # which annotation types the INVALID rows come from - usually a tag
            # family whose label does not hold the calculation tag or the size
            top = sorted(by_family.items(), key=lambda item: -item[1])[:3]
            message += u' Invalid annotations by type: {0}.'.format(
                u'; '.join(u'{0} ({1:,})'.format(name or 'unknown', count) for name, count in top))
        if not annotations and not only_trays:
            message += (u' No cable tray annotation was found - check which kinds of annotation '
                        u'are ticked above.')
        self._set_status(message)

    # ------------------------------------------------------------------ results
    def _fill_table(self):
        table = self.table
        table.BeginLoadData()
        try:
            table.Rows.Clear()
            for row in self.result.rows:
                item = table.NewRow()
                item['Key'] = row['key']
                item['Status'] = row['status']
                item['Tag'] = row['tag'] or NA
                item['Sheet'] = row['sheet_number']
                item['SheetName'] = row['sheet_name']
                item['View'] = row['view_name']
                item['ExcelW'] = _number(row['excel_width'])
                item['ExcelH'] = _number(row['excel_height'])
                item['RevitW'] = _number(row['revit_width'])
                item['RevitH'] = _number(row['revit_height'])
                item['Qty'] = NA if row['quantity'] is None else str(row['quantity'])
                item['Description'] = row['description']
                item['Comments'] = row['comments']
                shown_id = row['element_id'] if row['element_id'] is not None else row['annotation_id']
                item['ElementId'] = NA if shown_id is None else str(shown_id)
                item['Family'] = row.get('family') or ''
                item['IsError'] = row['status'] != config.PASS
                table.Rows.Add(item)
        finally:
            table.EndLoadData()
        self._apply_filter()

    def _show_summary(self, summary):
        tiles = (('t_excel', 'excel_tags'), ('t_revit', 'revit_tags'), ('t_matched', 'matched'),
                 ('t_missing', 'missing'), ('t_extra', 'extra'), ('t_width', 'width_mismatch'),
                 ('t_height', 'height_mismatch'), ('t_dup', 'duplicates'), ('t_invalid', 'invalid'),
                 ('t_errors', 'errors'))
        for name, key in tiles:
            getattr(self, name).Text = '-' if summary is None else '{0:,}'.format(summary[key])
        # "-" = not checked (the option is off, or only annotations were read)
        self.t_nottag.Text = ('{0:,}'.format(summary.get('not_tagged', 0))
                              if summary is not None and summary.get('coverage') else '-')
        from System.Windows.Media import BrushConverter
        colour = '#1F2D3D'
        if summary is not None:
            colour = '#00B85C' if summary['errors'] == 0 else '#E53935'
        brush = BrushConverter().ConvertFromString(colour)
        self.errors_tile.Background = brush
        self.errors_tile.BorderBrush = brush
        self._update_buttons()

    def _apply_filter(self, sender=None, args=None):
        from System.Windows import Visibility
        text = (self.search_tb.Text or '').strip()
        self.search_hint.Visibility = Visibility.Collapsed if text else Visibility.Visible
        parts = []
        choice = self.status_cb.SelectedItem
        if choice == ERRORS_ONLY:
            parts.append('IsError = true')
        elif choice and choice != ALL_ROWS:
            parts.append(u"Status = '{0}'".format(choice))
        for word in text.split():
            w = _like(word)
            parts.append(u"(Tag LIKE '%{0}%' OR Sheet LIKE '%{0}%' OR SheetName LIKE '%{0}%' "
                         u"OR View LIKE '%{0}%' OR Description LIKE '%{0}%' "
                         u"OR Comments LIKE '%{0}%' OR ElementId LIKE '%{0}%' "
                         u"OR Family LIKE '%{0}%')".format(w))
        try:
            self.table.DefaultView.RowFilter = ' AND '.join(parts)
        except Exception:
            self.table.DefaultView.RowFilter = ''
        self.shown_tb.Text = '{0:,} of {1:,} rows'.format(self.table.DefaultView.Count,
                                                          self.table.Rows.Count)

    def _update_buttons(self, sender=None, args=None):
        has_result = self.result is not None
        row = self._selected_row()
        if row is not None and sender is self.grid:
            # the Comments column is often too narrow: show the selected row in full
            text = u'{0}  |  {1}  |  {2}'.format(row['status'], row['tag'] or NA, row['comments'])
            if row.get('family'):
                text += u'  |  {0}'.format(row['family'])
            self._set_status(text)
        self.btn_highlight.IsEnabled = has_result and bool(self.result.errors())
        self.btn_clear.IsEnabled = has_result
        self.btn_export.IsEnabled = has_result
        self.btn_colour.IsEnabled = has_result
        self.btn_uncolour.IsEnabled = has_result or bool(self.coloured)
        self.btn_sheet.IsEnabled = row is not None and row.get('sheet_id') is not None
        # a cable tray row has no view of its own, but Revit can still find it
        self.btn_view.IsEnabled = row is not None and (row.get('view_id') is not None
                                                       or row.get('element_id') is not None)

    def _selected_row(self):
        if self.result is None:
            return None
        item = self.grid.SelectedItem
        if item is None:
            return None
        try:
            return self.result.rows[int(item.Row['Key'])]
        except Exception:
            return None

    # ------------------------------------------------------------------ Revit actions
    def _in_revit(self, action):
        """Run action(uidoc) inside Revit and put its message in the status bar."""
        def run(uiapp):
            uidoc = uiapp.ActiveUIDocument
            if not self._same_document(uidoc):
                self._set_status('These results belong to another model. Activate "{0}" or '
                                 'run the check again.'.format(self.doc.Title))
                return
            message = action(uidoc)
            if message:
                self._set_status(message)
        self.runner.run(run)

    def _highlight(self, sender, args):
        errors = self.result.errors()

        def action(uidoc):
            count, here = highlight.highlight_errors(uidoc, errors)
            missing = len([row for row in errors if row['status'] == config.MISSING])
            if not count:
                return ('Nothing to highlight: the {0} error(s) are tags missing from the '
                        'drawings, so there is no element to select.'.format(missing))
            where = 'in the active view' if here else 'in the project (none are in the active view)'
            note = (' {0} missing tag(s) have no element to highlight - see the list.'.format(missing)
                    if missing else '')
            return '{0} element(s) selected {1}. Selection only - nothing was changed.{2}'.format(
                count, where, note)
        self._in_revit(action)

    def _clear(self, sender, args):
        def action(uidoc):
            highlight.clear_highlights(uidoc)
            return 'Highlights cleared.'
        self._in_revit(action)

    def _colour(self, sender, args):
        """Colour, in the active Revit view, the elements of the rows that are
        shown in the list - each kind of issue in its own colour. PASS rows are
        coloured only when the list is filtered to PASS."""
        only_pass = self.status_cb.SelectedItem == config.PASS
        rows = [row for row in self._shown_rows()
                if (row['status'] == config.PASS) == only_pass]
        if not rows:
            self._set_status('Nothing to colour: no issue is shown in the list.')
            return

        def action(uidoc):
            try:
                counts, ids, elsewhere = highlight.colour_by_status(
                    uidoc, rows, config.MODEL_COLOURS, config.MODEL_COLOUR_LINE_WEIGHT)
            except highlight.ColourError as error:
                return u'{0}'.format(error)
            view_id = revit_reader.id_value(uidoc.ActiveView.Id)
            if ids:
                self.coloured.setdefault(view_id, set()).update(ids)
            self._update_buttons()
            no_element = len([row for row in rows if row['status'] == config.MISSING])
            if not ids:
                return (u'Nothing coloured: none of the {0:,} element(s) of the shown rows is in '
                        u'the active view. Open the view that shows these cable trays and click '
                        u'again.'.format(elsewhere) if elsewhere else
                        u'Nothing to colour: the shown rows are missing tags, which have no element.')
            parts = [u'{0} {1:,}'.format(status, counts[status])
                     for status in config.STATUS_ORDER if counts.get(status)]
            message = u'Coloured in view "{0}": {1}.'.format(uidoc.ActiveView.Name, u', '.join(parts))
            if elsewhere:
                message += u' {0:,} element(s) are not in this view.'.format(elsewhere)
            if no_element:
                message += u' {0:,} MISSING row(s) have no element.'.format(no_element)
            return message + u' "Remove colours" or Ctrl+Z takes them off.'
        self._in_revit(action)

    def _uncolour(self, sender, args):
        def action(uidoc):
            view_id = revit_reader.id_value(uidoc.ActiveView.Id)
            ids = set(self.coloured.get(view_id) or [])
            if self.result is not None:
                # also what an earlier session coloured: every element of the results
                ids.update(highlight.ids_of(self.result.rows))
            try:
                count = highlight.remove_colours(uidoc, ids)
            except highlight.ColourError as error:
                return u'{0}'.format(error)
            self.coloured.pop(view_id, None)
            self._update_buttons()
            return (u'Colours removed from {0:,} element(s) in view "{1}".'.format(
                count, uidoc.ActiveView.Name) if count else
                u'There is nothing to remove in this view.')
        self._in_revit(action)

    def _open_sheet(self, sender, args):
        row = self._selected_row()
        if row is None:
            return
        if (row.get('sheet_id') is None and row.get('view_id') is None
                and row.get('element_id') is None):
            self._set_status('"{0}" is missing from the drawings, so there is no sheet to open.'
                             .format(row['tag']))
            return
        self._in_revit(lambda uidoc: u'Opened: ' + highlight.open_sheet(uidoc, row))

    def _open_view(self, sender, args):
        row = self._selected_row()
        if row is None:
            return
        self._in_revit(lambda uidoc: u'Opened: ' + highlight.open_view(uidoc, row))

    # ------------------------------------------------------------------ report
    def _sheets_checked_text(self):
        """What the report's Summary sheet says about the sheets of THIS result."""
        summary = self.result.summary
        if not summary.get('partial'):
            return 'All {0} sheets'.format(summary.get('sheets_total') or len(self.sheets))
        chosen = self._selected_sheet_ids() or set()
        numbers = [sheet['number'] for sheet in self.sheets if sheet['id'] in chosen]
        return u'{0} of {1} sheets: {2}'.format(summary['sheets_checked'], summary['sheets_total'],
                                                ', '.join(numbers))

    def _export(self, sender, args):
        if self.result is None:
            return
        default = u'{0}_CableTray_QA_{1}'.format(self.doc.Title, time.strftime('%Y%m%d_%H%M'))
        path = forms.save_file(file_ext='xlsx', default_name=default,
                               title='Export the Cable Tray QA report')
        if not path:
            return
        try:
            report.write_report(path, self.result, {
                'model': self.doc.Title, 'excel': self.excel.path, 'sheet': self.excel.sheet,
                'readers': self.readers_used, 'sheets_checked': self.result_sheets_text})
        except Exception as error:
            forms.alert(u'{0}'.format(error), title=config.TITLE)
            return
        self._set_status(u'Report written: {0}'.format(path))
        if forms.alert(u'The QA report was written to:\n{0}\n\nOpen it now?'.format(path),
                       title=config.TITLE, yes=True, no=True):
            try:
                os.startfile(path)
            except Exception:
                pass


def main():
    uidoc = revit.uidoc
    if uidoc is None or uidoc.Document is None or uidoc.Document.IsFamilyDocument:
        forms.alert('Open a project model first. This tool does not run in the Family Editor.',
                    title=config.TITLE, exitscript=True)
    window = CableTrayQAWindow(uidoc)
    # The button script itself runs inside Revit's API context, so the first
    # check can run directly: click the button, pick the Excel, see the results.
    if window._browse():
        try:
            window.run_check(uidoc)
        except Exception as error:
            window._set_status(u'The check could not be completed: {0}'.format(error))
    _OPEN_WINDOWS.append(window)
    window.Closed += lambda sender, args: _OPEN_WINDOWS.remove(window) if window in _OPEN_WINDOWS else None
    window.show()                       # modeless


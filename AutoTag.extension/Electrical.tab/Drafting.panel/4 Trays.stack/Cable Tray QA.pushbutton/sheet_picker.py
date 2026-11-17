# -*- coding: utf-8 -*-
"""Cable Tray QA/QC - the "Sheets to check" dialog.

Pure UI: it works on the list of sheets already read by
revit_reader.list_sheets(), so it never touches Revit and can be shown as an
ordinary modal dialog from the modeless main window.

Filters (they only narrow the LIST - a ticked sheet stays ticked when a
filter hides it):
    search            words in the sheet number or name
    number series     the part of the number before its first digit ("E-")
    parameter = value any sheet parameter that has a value
    annotated only    sheets that had cable tray annotations in the last check
    ticked only
"""
import codecs
import os
import re

from pyrevit import forms

ANY_SERIES, ANY_PARAMETER, ANY_VALUE = '(all)', '(any parameter)', '(any value)'
_SERIES = re.compile(r'^[^\d]*')


def series_of(number):
    """'E-301' -> 'E-', 'M-CC-12' -> 'M-CC-', '1001' -> '(digits)'."""
    prefix = _SERIES.match(number or '').group().strip()
    return prefix if prefix else '(digits)'


def _like(text):
    out = []
    for character in text:
        if character in '[]*%':
            out.append('[' + character + ']')
        elif character == "'":
            out.append("''")
        else:
            out.append(character)
    return ''.join(out)


def _xaml():
    folder = os.path.dirname(__file__)
    with codecs.open(os.path.join(folder, 'sheets.xaml'), 'r', 'utf-8') as handle:
        xaml = handle.read()
    with codecs.open(os.path.join(folder, 'ui.xaml'), 'r', 'utf-8') as handle:
        found = re.search(r'<Window\.Resources>.*?</Window\.Resources>', handle.read(), re.S)
    return xaml.replace('{{RESOURCES}}', found.group(0) if found else u'')


class SheetPickerWindow(forms.WPFWindow):
    """picker = SheetPickerWindow(sheets, selected_ids, counts); picker.ShowDialog()
    then picker.result is None (cancelled) or the set of ticked sheet ids."""

    def __init__(self, sheets, selected_ids=None, counts=None):
        forms.WPFWindow.__init__(self, _xaml(), literal_string=True)
        self.sheets = sheets
        self.counts = counts or {}
        self.result = None
        self._busy = False
        self._by_id = dict((sheet['id'], sheet) for sheet in sheets)
        self._build_table(selected_ids)
        self._init_filters()
        self._wire()
        self._apply_filter()

    # ------------------------------------------------------------------ data
    def _build_table(self, selected_ids):
        import clr
        clr.AddReference('System.Data')
        import System
        from System.Data import DataTable
        table = DataTable('sheets')
        for name, kind in (('Sel', System.Boolean), ('Id', System.Int64), ('Number', System.String),
                           ('Name', System.String), ('Series', System.String),
                           ('Value', System.String), ('Found', System.String),
                           ('HasFound', System.Boolean)):
            table.Columns.Add(name, clr.GetClrType(kind))
        table.BeginLoadData()
        for sheet in self.sheets:
            row = table.NewRow()
            # no selection yet = every sheet is checked
            row['Sel'] = True if selected_ids is None else sheet['id'] in selected_ids
            row['Id'] = sheet['id']
            row['Number'] = sheet['number']
            row['Name'] = sheet['name']
            row['Series'] = series_of(sheet['number'])
            row['Value'] = ''
            count = self.counts.get(sheet['id'])
            row['Found'] = '' if count is None else str(count)
            row['HasFound'] = bool(count)
            table.Rows.Add(row)
        table.EndLoadData()
        self.table = table
        self.grid.ItemsSource = table.DefaultView

    def _init_filters(self):
        self.series_cb.Items.Add(ANY_SERIES)
        for series in sorted(set(series_of(sheet['number']) for sheet in self.sheets)):
            self.series_cb.Items.Add(series)
        self.series_cb.SelectedIndex = 0
        # only parameters that can tell sheets apart (at least two different
        # values, or a value on some sheets and not on others)
        values = {}
        for sheet in self.sheets:
            for name, value in sheet['params'].items():
                values.setdefault(name, set()).add(value)
        self._values = {}
        for name, found in values.items():
            on_all = len([1 for sheet in self.sheets if name in sheet['params']]) == len(self.sheets)
            if len(found) > 1 or not on_all:
                self._values[name] = found
        self.param_cb.Items.Add(ANY_PARAMETER)
        for name in sorted(self._values, key=lambda item: item.lower()):
            self.param_cb.Items.Add(name)
        self.param_cb.SelectedIndex = 0
        self._fill_values()
        has_counts = bool(self.counts)
        self.chk_annotated.IsEnabled = has_counts
        if not has_counts:
            self.chk_annotated.Content = ('Only sheets with cable tray annotations '
                                          '(available after the first check)')

    def _fill_values(self):
        self._busy = True
        try:
            self.value_cb.Items.Clear()
            self.value_cb.Items.Add(ANY_VALUE)
            name = self.param_cb.SelectedItem
            for value in sorted(self._values.get(name, []), key=lambda item: item.lower()):
                self.value_cb.Items.Add(value)
            self.value_cb.SelectedIndex = 0
            self.value_cb.IsEnabled = name in self._values
            # show the chosen parameter's value as a column
            for row in self.table.Rows:
                sheet = self._by_id[int(row['Id'])]
                row['Value'] = sheet['params'].get(name, '') if name in self._values else ''
            # column 3 of sheets.xaml is the parameter value column
            self.grid.Columns[3].Header = (u'{0}'.format(name).upper() if name in self._values
                                           else 'PARAMETER VALUE')
        finally:
            self._busy = False

    # ------------------------------------------------------------------ events
    def _wire(self):
        self.search_tb.TextChanged += self._apply_filter
        self.series_cb.SelectionChanged += self._apply_filter
        self.param_cb.SelectionChanged += self._parameter_changed
        self.value_cb.SelectionChanged += self._apply_filter
        for box in (self.chk_annotated, self.chk_ticked):
            box.Checked += self._apply_filter
            box.Unchecked += self._apply_filter
        self.btn_reset.Click += self._reset
        self.btn_all.Click += lambda sender, args: self._set_shown(lambda row: True)
        self.btn_none.Click += lambda sender, args: self._set_shown(lambda row: False)
        self.btn_invert.Click += lambda sender, args: self._set_shown(lambda row: not row['Sel'])
        self.btn_ok.Click += self._ok
        self.btn_cancel.Click += lambda sender, args: self.Close()
        self.table.ColumnChanged += self._update_count
        self.grid.PreviewKeyDown += self._key

    def _parameter_changed(self, sender, args):
        self._fill_values()
        self._apply_filter()

    def _commit(self):
        """Finish a tick that is still being edited so it is counted."""
        try:
            for view_row in self.table.DefaultView:
                if view_row.IsEdit:
                    view_row.EndEdit()
        except Exception:
            pass

    def _apply_filter(self, sender=None, args=None):
        if self._busy:
            return
        from System.Windows import Visibility
        self._commit()
        text = (self.search_tb.Text or '').strip()
        self.search_hint.Visibility = Visibility.Collapsed if text else Visibility.Visible
        parts = []
        for word in text.split():
            parts.append(u"(Number LIKE '%{0}%' OR Name LIKE '%{0}%')".format(_like(word)))
        series = self.series_cb.SelectedItem
        if series and series != ANY_SERIES:
            parts.append(u"Series = '{0}'".format(series.replace("'", "''")))
        value = self.value_cb.SelectedItem
        if self.param_cb.SelectedItem in self._values and value and value != ANY_VALUE:
            parts.append(u"Value = '{0}'".format(value.replace("'", "''")))
        if self.chk_annotated.IsChecked:
            parts.append('HasFound = true')
        if self.chk_ticked.IsChecked:
            parts.append('Sel = true')
        try:
            self.table.DefaultView.RowFilter = ' AND '.join(parts)
        except Exception:
            self.table.DefaultView.RowFilter = ''
        self._update_count()

    def _reset(self, sender, args):
        self._busy = True
        try:
            self.search_tb.Text = ''
            self.series_cb.SelectedIndex = 0
            self.param_cb.SelectedIndex = 0
            self.chk_annotated.IsChecked = False
            self.chk_ticked.IsChecked = False
        finally:
            self._busy = False
        self._fill_values()
        self._apply_filter()

    def _set_shown(self, value_of):
        self._commit()
        rows = [view_row.Row for view_row in self.table.DefaultView]
        self._busy = True
        try:
            for row in rows:
                row['Sel'] = bool(value_of(row))
        finally:
            self._busy = False
        self._update_count()

    def _key(self, sender, args):
        """Space ticks / unticks every highlighted row together."""
        from System.Windows.Input import Key
        if args.Key != Key.Space:
            return
        rows = [item.Row for item in self.grid.SelectedItems]
        if not rows:
            return
        target = not all(bool(row['Sel']) for row in rows)
        self._busy = True
        try:
            for row in rows:
                row['Sel'] = target
        finally:
            self._busy = False
        self._update_count()
        args.Handled = True

    # ------------------------------------------------------------------ result
    def ticked_ids(self):
        return set(int(row['Id']) for row in self.table.Rows if row['Sel'])

    def _update_count(self, sender=None, args=None):
        if self._busy:
            return
        ticked = len(self.ticked_ids())
        self.count_tb.Text = '{0:,} of {1:,} sheets ticked'.format(ticked, self.table.Rows.Count)
        self.shown_tb.Text = '{0:,} shown'.format(self.table.DefaultView.Count)
        self.btn_ok.IsEnabled = ticked > 0
        self.btn_ok.Content = 'Use {0:,} sheet(s)'.format(ticked) if ticked else 'Tick at least one sheet'

    def _ok(self, sender, args):
        self._commit()
        ticked = self.ticked_ids()
        if not ticked:
            return
        self.result = ticked
        self.Close()

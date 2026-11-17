# -*- coding: utf-8 -*-
"""Cable Tray Layout - the window.

Choose the file, see every tray with its calculated widths, tick the trays to
draw, press Create views. Nothing is changed in the model before that button.
"""
import json
import os
import traceback

import clr
# FIX: the WPF libraries must be referenced before "System.Windows" can be imported
for _name in ('PresentationFramework', 'PresentationCore', 'WindowsBase', 'System.Xaml'):
    clr.AddReference(_name)

from System.Windows import GridLength, GridUnitType, HorizontalAlignment, TextAlignment, Thickness
from System.Windows.Controls import CheckBox, ColumnDefinition, Grid, TextBlock
from System.Windows.Media import BrushConverter

from pyrevit import forms, revit
from Autodesk.Revit.DB import TransactionGroup

import ctl_calc
import ctl_input
import ctl_revit

SETTINGS = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'EleTools', 'CableTrayLayout', 'settings.json')
COLUMN_WIDTHS = (None, 60, 55, 90, 90, 80, 150)            # as the headings in ui.xaml; None = the rest
RED, AMBER, GREEN, GREY = '#B42318', '#B54708', '#067647', '#5A6270'


def _load_settings():
    try:
        with open(SETTINGS) as handle:
            return json.load(handle)
    except Exception:
        return {}


def _save_settings(values):
    try:
        folder = os.path.dirname(SETTINGS)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with open(SETTINGS, 'w') as handle:
            json.dump(values, handle)
    except Exception:
        pass                                               # remembering the last file is only a convenience


class TrayWindow(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, os.path.join(os.path.dirname(__file__), 'ui.xaml'))
        self.doc = revit.doc
        self.trays = []                                    # ctl_calc.Tray, as read from the file
        self.notes = []
        self.rows = []                                     # (CheckBox, Tray, Layout or None)
        self.last_view = None                              # opened after the window closes
        self.report = None
        self._busy = False
        self._brushes = BrushConverter()
        settings = _load_settings()

        for key, title in ctl_calc.METHODS:
            self.cmbMethod.Items.Add(title)
        for key, title in ctl_revit.EXISTING:
            self.cmbExisting.Items.Add(title)
        self.cmbMethod.SelectedIndex = self._index(ctl_calc.METHODS, settings.get('method'))
        self.cmbExisting.SelectedIndex = self._index(ctl_revit.EXISTING, settings.get('existing'))
        for key, title in ctl_revit.SCALE_CHOICES:
            self.cmbScale.Items.Add(title)
        self.cmbScale.SelectedIndex = self._index(ctl_revit.SCALE_CHOICES, settings.get('scale', ctl_revit.DEFAULT_SCALE))
        for key, title in ctl_revit.VIEW_MODES:
            self.cmbViews.Items.Add(title)
        self.cmbViews.SelectedIndex = self._index(ctl_revit.VIEW_MODES, settings.get('views'))
        self.txtViewName.Text = settings.get('view_name') or ctl_revit.DEFAULT_COMBINED_NAME
        self.cmbViews.SelectionChanged += self._guard(self.on_views)
        self.on_views()
        # the linear dimension types that exist in this project
        self.dimension_types, default = ctl_revit.dimension_types(self.doc)
        for name, _ in self.dimension_types:
            self.cmbDimType.Items.Add(name)
        if self.dimension_types:
            names = [item[0] for item in self.dimension_types]
            wanted = settings.get('dimension_type')
            self.cmbDimType.SelectedIndex = names.index(wanted) if wanted in names else default
        # the text types that exist in this project; none is ever created
        self.text_types = ctl_revit.text_types(self.doc)
        for name, _, size in self.text_types:
            self.cmbTextType.Items.Add(u'{0}   ({1:.2f} mm)'.format(name, size))
        if self.text_types:
            names = [item[0] for item in self.text_types]
            if settings.get('text_type') in names:
                self.cmbTextType.SelectedIndex = names.index(settings.get('text_type'))
            else:                                          # first time: the one nearest to 2.5 mm
                sizes = [abs(item[2] - ctl_revit.TEXT_SIZE_MM) for item in self.text_types]
                self.cmbTextType.SelectedIndex = sizes.index(min(sizes))

        # the cable table view: its own name, scale and text style, independent of the tray views
        self.chkTable.IsChecked = bool(settings.get('table', False))
        self.txtTableName.Text = settings.get('table_name') or ctl_revit.DEFAULT_TABLE_NAME
        for key, title in ctl_revit.TABLE_SCALES:
            self.cmbTableScale.Items.Add(title)
        self.cmbTableScale.SelectedIndex = self._index(ctl_revit.TABLE_SCALES,
                                                       settings.get('table_scale', ctl_revit.DEFAULT_TABLE_SCALE))
        for name, _, size in self.text_types:
            self.cmbTableText.Items.Add(u'{0}   ({1:.2f} mm)'.format(name, size))
        if self.text_types:
            names = [item[0] for item in self.text_types]
            if settings.get('table_text') in names:
                self.cmbTableText.SelectedIndex = names.index(settings.get('table_text'))
            else:
                self.cmbTableText.SelectedIndex = max(0, self.cmbTextType.SelectedIndex)
        for key, title in ctl_revit.TABLE_LAYOUTS:         # a table for each tray, or one table for all
            self.cmbTableLayout.Items.Add(title)
        self.cmbTableLayout.SelectedIndex = self._index(ctl_revit.TABLE_LAYOUTS, settings.get('table_layout'))
        self.chkTable.Click += self._guard(self.on_table)
        self.on_table()
        # what to create: the tray views, the table view, or both
        self.chkTrays.IsChecked = bool(settings.get('trays', True))
        self.chkTrays.Click += self._guard(self.on_outputs)
        self.chkTable.Click += self._guard(self.on_outputs)
        self.on_outputs()

        self.btnBrowse.Click += self._guard(self.on_browse)
        self.btnTemplate.Click += self._guard(self.on_template)
        self.btnCreate.Click += self._guard(self.on_create)
        self.btnClose.Click += self._guard(lambda: self.Close())
        self.cmbSheet.SelectionChanged += self._guard(self.on_sheet)
        self.cmbMethod.SelectionChanged += self._guard(self.refresh)
        self.chkAll.Click += self._guard(self.on_all)

        last = settings.get('file')
        if last and os.path.isfile(last):
            self._guard(lambda: self.open_file(last, settings.get('sheet')))(None, None)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _index(pairs, key):
        keys = [pair[0] for pair in pairs]
        return keys.index(key) if key in keys else 0

    def _guard(self, action):
        """An event handler that shows an error instead of failing silently."""
        def handler(sender, args):
            try:
                action()
            except ctl_input.InputError as error:
                self.show_problem(u'{0}'.format(error))
            except Exception:
                self.show_problem(traceback.format_exc())
        return handler

    def show_problem(self, text):
        self.txtMessages.Text = text
        self.txtMessages.Foreground = self._brushes.ConvertFromString(RED)

    def show_messages(self, lines):
        self.txtMessages.Text = u'\n'.join(lines)
        self.txtMessages.Foreground = self._brushes.ConvertFromString('#1F2933')

    @property
    def method(self):
        return ctl_calc.METHODS[max(0, self.cmbMethod.SelectedIndex)][0]

    @property
    def existing(self):
        return ctl_revit.EXISTING[max(0, self.cmbExisting.SelectedIndex)][0]

    @property
    def scale(self):
        return ctl_revit.SCALE_CHOICES[max(0, self.cmbScale.SelectedIndex)][0]

    @property
    def view_mode(self):
        return ctl_revit.VIEW_MODES[max(0, self.cmbViews.SelectedIndex)][0]

    def on_views(self):
        # per-tray views are named by the tray; and no name is needed when no tray views are created
        self.txtViewName.IsEnabled = self.view_mode == ctl_revit.ONE_VIEW and bool(self.chkTrays.IsChecked)

    def on_table(self):
        wanted = bool(self.chkTable.IsChecked)
        for control in (self.txtTableName, self.cmbTableScale, self.cmbTableText, self.cmbTableLayout):
            control.IsEnabled = wanted

    @property
    def table_layout(self):
        return ctl_revit.TABLE_LAYOUTS[max(0, self.cmbTableLayout.SelectedIndex)][0]

    def on_outputs(self):
        """Tray views and / or table view: grey out what is not going to be created."""
        trays = bool(self.chkTrays.IsChecked)
        self.cmbViews.IsEnabled = trays
        self.txtViewName.IsEnabled = trays and self.view_mode == ctl_revit.ONE_VIEW
        for control in (self.cmbScale, self.cmbTextType, self.cmbDimType):
            control.IsEnabled = trays
        self.btnCreate.Content = ('Create views' if trays and self.chkTable.IsChecked else
                                  'Create tray views' if trays else
                                  'Create table' if self.chkTable.IsChecked else 'Create views')
        self.update_count()

    @property
    def table_text_type(self):
        """(name, id, size) of the text type chosen for the table, or None."""
        index = self.cmbTableText.SelectedIndex
        return self.text_types[index] if 0 <= index < len(self.text_types) else None

    @property
    def table_scale(self):
        return ctl_revit.TABLE_SCALES[max(0, self.cmbTableScale.SelectedIndex)][0]

    @property
    def dimension_type(self):
        """(name, id) of the chosen dimension type, or None."""
        index = self.cmbDimType.SelectedIndex
        return self.dimension_types[index] if 0 <= index < len(self.dimension_types) else None

    @property
    def text_type(self):
        """(name, id, size) of the chosen text type, or None."""
        index = self.cmbTextType.SelectedIndex
        return self.text_types[index] if 0 <= index < len(self.text_types) else None

    # ------------------------------------------------------------------
    # the file
    # ------------------------------------------------------------------
    def on_browse(self):
        from Microsoft.Win32 import OpenFileDialog
        dialog = OpenFileDialog()
        dialog.Title = 'Cable schedule'
        dialog.Filter = 'Cable schedule (*.xlsx;*.xlsm;*.csv)|*.xlsx;*.xlsm;*.csv|All files (*.*)|*.*'
        current = self.txtFile.Text
        if current and os.path.isdir(os.path.dirname(current)):
            dialog.InitialDirectory = os.path.dirname(current)
        if dialog.ShowDialog():
            self.open_file(dialog.FileName, None)

    def on_template(self):
        from Microsoft.Win32 import SaveFileDialog
        from System.IO import File
        from System.Text import UTF8Encoding
        dialog = SaveFileDialog()
        dialog.Title = 'Save the example cable schedule'
        dialog.Filter = 'CSV file (*.csv)|*.csv'
        dialog.FileName = 'Cable tray schedule template.csv'
        if dialog.ShowDialog():
            File.WriteAllText(dialog.FileName, ctl_input.TEMPLATE, UTF8Encoding(True))     # with BOM, so Excel opens it right
            self.txtStatus.Text = 'Template saved: ' + dialog.FileName

    def open_file(self, path, sheet):
        self.trays, self.notes = [], []
        self.txtFile.Text = path
        try:
            self._busy = True                              # filling the sheet list must not trigger a reload
            try:
                self.cmbSheet.Items.Clear()
                self.cmbSheet.IsEnabled = False
                names = ctl_input.sheet_names(path)
                for name in names:
                    self.cmbSheet.Items.Add(name)
                self.cmbSheet.IsEnabled = len(names) > 1
                if names:
                    self.cmbSheet.SelectedIndex = names.index(sheet) if sheet in names else 0
            finally:
                self._busy = False
            self.read()
        finally:
            self.refresh()                                 # also after an error: the old list must go

    def on_sheet(self):
        if self._busy or self.cmbSheet.SelectedItem is None:
            return
        self.trays, self.notes = [], []
        try:
            self.read()
        finally:
            self.refresh()

    def read(self):
        sheet = self.cmbSheet.SelectedItem
        self.trays, self.notes = ctl_input.load(self.txtFile.Text, None if sheet is None else u'{0}'.format(sheet))

    # ------------------------------------------------------------------
    # the list of trays
    # ------------------------------------------------------------------
    def _cell(self, grid, column, text, colour=None, right=False):
        block = TextBlock()
        block.Text = text
        if right:
            block.TextAlignment = TextAlignment.Right
        if colour:
            block.Foreground = self._brushes.ConvertFromString(colour)
        if column == len(COLUMN_WIDTHS) - 1:
            block.Margin = Thickness(14, 0, 0, 0)
        Grid.SetColumn(block, column)
        grid.Children.Add(block)

    def refresh(self):
        """Recalculate every tray with the chosen method and rebuild the list."""
        previous = dict((tray.label, bool(box.IsChecked)) for box, tray, _ in self.rows)
        self.rows = []
        self.panelTrays.Children.Clear()
        lines = list(self.notes)
        # a Tray ID that stands for several trays (e.g. 900 + 750 mm) gives one line per tray
        parts = []
        for tray in self.trays:
            if tray.errors:
                parts.append(tray)
            else:
                found, notes = ctl_calc.split_tray(tray, self.method)
                parts.extend(found)
                lines.extend(notes)
        for tray in parts:
            layout = None if tray.errors else ctl_calc.build(tray, self.method)
            grid = Grid()
            for width in COLUMN_WIDTHS:
                definition = ColumnDefinition()
                definition.Width = GridLength(1, GridUnitType.Star) if width is None else GridLength(width)
                grid.ColumnDefinitions.Add(definition)
            self._cell(grid, 0, tray.label)
            if layout is None:
                result, colour = 'Error - not drawn', RED
                lines.append(u'{0}: cannot be drawn'.format(tray.id))
                lines.extend(u'      ' + text for text in tray.errors)
            else:
                f = ctl_calc.fmt
                self._cell(grid, 1, str(layout.cable_count), right=True)
                self._cell(grid, 2, str(len(layout.layers)), right=True)
                self._cell(grid, 3, f(layout.layout_width), right=True)
                self._cell(grid, 4, f(layout.required_width), right=True)
                self._cell(grid, 5, f(tray.width), right=True)
                warnings = layout.warnings()
                if layout.exceeds_width:
                    result, colour = 'Exceeds tray width', RED
                elif warnings:
                    result, colour = 'Exceeds tray height', AMBER
                else:
                    result, colour = 'OK', GREEN
                for text in warnings + tray.warnings + layout.notes:
                    lines.append(u'{0}: {1}'.format(tray.label, text))
            self._cell(grid, len(COLUMN_WIDTHS) - 1, result, colour)
            box = CheckBox()
            box.Content = grid
            box.HorizontalContentAlignment = HorizontalAlignment.Stretch
            box.Margin = Thickness(2, 3, 2, 3)
            box.IsEnabled = layout is not None
            box.IsChecked = layout is not None and previous.get(tray.label, True)
            box.Click += self._guard(self.update_count)
            self.panelTrays.Children.Add(box)
            self.rows.append((box, tray, layout))
        if self.trays:
            self.show_messages(lines or ['All trays were read without remarks.'])
        self.update_count()

    def update_count(self):
        drawable = [box for box, _, layout in self.rows if layout is not None]
        chosen = [box for box in drawable if box.IsChecked]
        self.txtCount.Text = '{0} of {1} tray(s) selected'.format(len(chosen), len(self.rows)) if self.rows else ''
        self.chkAll.IsChecked = bool(drawable) and len(chosen) == len(drawable)
        # something to draw from, and something to create (tray views and / or the table)
        self.btnCreate.IsEnabled = bool(chosen) and (bool(self.chkTrays.IsChecked) or bool(self.chkTable.IsChecked))

    def on_all(self):
        wanted = bool(self.chkAll.IsChecked)
        for box, _, layout in self.rows:
            if layout is not None:
                box.IsChecked = wanted
        self.update_count()

    # ------------------------------------------------------------------
    # drawing
    # ------------------------------------------------------------------
    def on_create(self):
        chosen = [(tray, layout) for box, tray, layout in self.rows if layout is not None and box.IsChecked]
        if not chosen:
            return
        sheet = self.cmbSheet.SelectedItem
        _save_settings({'file': self.txtFile.Text, 'sheet': None if sheet is None else u'{0}'.format(sheet),
                        'method': self.method, 'existing': self.existing, 'scale': self.scale,
                        'text_type': None if self.text_type is None else self.text_type[0],
                        'dimension_type': None if self.dimension_type is None else self.dimension_type[0],
                        'views': self.view_mode, 'view_name': self.txtViewName.Text,
                        'trays': bool(self.chkTrays.IsChecked),
                        'table': bool(self.chkTable.IsChecked), 'table_name': self.txtTableName.Text,
                        'table_scale': self.table_scale, 'table_layout': self.table_layout,
                        'table_text': None if self.table_text_type is None else self.table_text_type[0]})
        # the trays of the table: whole Tray IDs, in file order (a Tray ID split over two trays is one block)
        chosen_ids = set(tray.id for tray, _ in chosen)
        table_trays = [tray for tray in self.trays if tray.id in chosen_ids and not tray.errors]
        if self.table_layout == ctl_revit.PER_TRAY:
            # a table for each tray AS DRAWN: a Tray ID split over two trays gives two tables,
            # each with the cables that lie in that tray
            table_trays = [tray for tray, _ in chosen]
        if not self.chkTrays.IsChecked:
            chosen = []                                    # only the table was asked for: no tray views are drawn
        text_type_id = None if self.text_type is None else self.text_type[1]
        dimension_type_id = None if self.dimension_type is None else self.dimension_type[1]
        done, failed, last = [], [], None
        group = TransactionGroup(self.doc, 'Cable tray layout views')
        group.Start()
        try:
            if self.view_mode == ctl_revit.ONE_VIEW and chosen:
                # every selected tray in ONE view, one below the other, in one transaction
                name = ctl_revit.combined_view_name(self.txtViewName.Text)
                layouts = [layout for _, layout in chosen]
                try:
                    view, what = ctl_revit.run_transaction(
                        self.doc, 'Cable tray layouts',
                        lambda: ctl_revit.draw_trays(self.doc, layouts, name, self.existing, self.scale,
                                                     text_type_id, dimension_type_id))
                    done.append(u'View "{0}" with {1} tray(s): {2}'.format(name, len(layouts), what))
                    for tray, layout in chosen:
                        for text in layout.warnings():
                            done.append(u'      {0}: {1}'.format(tray.label, text))
                    last = view
                except Exception as error:
                    failed.append(u'FAILED, nothing was drawn - {0}'.format(error))
                chosen = []                                # nothing left for the per-tray loop below
            for tray, layout in chosen:
                try:
                    # one transaction per tray: a tray that fails is rolled back and the others are kept
                    view, what = ctl_revit.run_transaction(
                        self.doc, 'Cable tray layout ' + tray.id,
                        lambda layout=layout: ctl_revit.draw_tray(self.doc, layout, self.existing, self.scale,
                                                                  text_type_id, dimension_type_id))
                    done.append(u'{0}: {1}'.format(tray.label, what))
                    for text in layout.warnings():
                        done.append(u'      ' + text)
                    if view is not None:
                        last = view
                except Exception as error:
                    failed.append(u'{0}: FAILED, nothing was drawn for this tray - {1}'.format(tray.label, error))
            # the cable table, in its own view with its own scale and text style (its own transaction:
            # if it fails, the tray views drawn above are kept)
            if self.chkTable.IsChecked and table_trays:
                table_name = ctl_revit.combined_view_name(self.txtTableName.Text) \
                    if self.txtTableName.Text.strip() else ctl_revit.DEFAULT_TABLE_NAME
                table_text_id = None if self.table_text_type is None else self.table_text_type[1]
                try:
                    table_view, what = ctl_revit.run_transaction(
                        self.doc, 'Cable tray schedule table',
                        lambda: ctl_revit.draw_table(self.doc, table_trays, table_name, self.existing,
                                                     self.table_scale, table_text_id, self.table_layout))
                    done.append(u'Table view "{0}": {1}'.format(table_name, what))
                    if last is None:
                        last = table_view
                except Exception as error:
                    failed.append(u'Table view: FAILED, the table was not drawn - {0}'.format(error))
            group.Assimilate()
        except Exception:
            if group.HasStarted() and not group.HasEnded():
                group.RollBack()
            raise
        lines = done + failed
        if last is None:
            self.show_problem(u'\n'.join(lines) or 'Nothing was drawn.')
            return
        self.last_view, self.report = last, lines
        self.Close()


def main():
    doc = revit.doc
    if doc is None or doc.IsFamilyDocument:
        forms.alert('Open a project before running Cable Tray Layout.', title='Cable Tray Layout')
        return
    window = TrayWindow()
    window.ShowDialog()
    if window.last_view is not None:
        ctl_revit.show_view(revit.uidoc, window.last_view)
        forms.alert(u'\n'.join(window.report), title='Cable Tray Layout', warn_icon=False)

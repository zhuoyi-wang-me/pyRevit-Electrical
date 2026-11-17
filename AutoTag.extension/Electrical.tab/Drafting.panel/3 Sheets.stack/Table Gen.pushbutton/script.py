# -*- coding: utf-8 -*-
"""Table Gen - Excel table into Revit as a drafting view, with its Excel look. UPDATE re-reads the Excel.

CREATE: choose the Excel file (.xlsx / .xlsm / .csv), the worksheet and the range (empty = the print
area, or else every used cell). A new drafting view is made with the table drawn at its paper size
(text notes, detail lines, filled regions). Optionally it is placed on the active sheet.

UPDATE: the list shows the Table Gen views of this model. Tick them and UPDATE: the Excel is read again
and the view redrawn - the viewports on sheets stay where they are. "Excel changed" marks the tables
whose file was saved after the last update. CHANGE FILE points a table to another workbook.
UPDATE also applies everything chosen in the window (text types, line styles, cell fill, borders,
table size, fit, hidden rows) - only the file, worksheet and range stay as stored with each table.

No text types, line styles or fill types are created: the window lists the model's own ones.
"""
import os
import io
import json
import datetime

TITLE = u'Table Gen'
DATA_DIR = os.path.join(os.environ.get('APPDATA', ''), 'EleTools', 'TableGen')
SETTINGS = os.path.join(DATA_DIR, 'settings.json')
LOG = os.path.join(DATA_DIR, 'last_run.txt')
SAME_TYPE = u'<same as Text type>'
NO_FILL = u'<no fill>'
BORDERS = [(u'As in Excel', 'excel'), (u'Every cell (thin) + Excel borders', 'all'), (u'None', 'none')]


def load_settings():
    try:
        with io.open(SETTINGS, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except Exception:
        return {}


def save_settings(values):
    try:
        if not os.path.isdir(DATA_DIR):
            os.makedirs(DATA_DIR)
        with io.open(SETTINGS, 'w', encoding='utf-8') as handle:
            handle.write(unicode(json.dumps(values, indent=2)))
    except Exception:
        pass


def write_log(text):
    try:
        if not os.path.isdir(DATA_DIR):
            os.makedirs(DATA_DIR)
        with io.open(LOG, 'w', encoding='utf-8') as handle:
            handle.write(u'Table Gen - {0}\n{1}\n'.format(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), text))
    except Exception:
        pass


XAML = u'''<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Table Gen - Excel to Revit table" Width="640" Height="760" MinWidth="560" MinHeight="640"
        WindowStartupLocation="CenterScreen" FontFamily="Segoe UI" FontSize="12" Background="#F4F6F8">
    <Grid Margin="16">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>

        <Border Grid.Row="0" Background="White" BorderBrush="#C9D1DA" BorderThickness="1" Padding="12" Margin="0,0,0,12">
            <StackPanel>
                <TextBlock Text="NEW TABLE FROM EXCEL" FontWeight="Bold" Margin="0,0,0,8"/>
                <Grid Margin="0,0,0,6">
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="110"/>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                    </Grid.ColumnDefinitions>
                    <TextBlock Text="Excel file" VerticalAlignment="Center"/>
                    <TextBox x:Name="path_tb" Grid.Column="1" Height="24" VerticalContentAlignment="Center" IsReadOnly="True"/>
                    <Button x:Name="browse_btn" Grid.Column="2" Content="Browse..." Width="76" Margin="6,0,0,0"/>
                </Grid>
                <Grid Margin="0,0,0,6">
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="110"/>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                        <ColumnDefinition Width="130"/>
                    </Grid.ColumnDefinitions>
                    <TextBlock Text="Worksheet" VerticalAlignment="Center"/>
                    <ComboBox x:Name="sheet_cb" Grid.Column="1" Height="24"/>
                    <TextBlock Grid.Column="2" Text="Range" VerticalAlignment="Center" Margin="12,0,6,0"/>
                    <TextBox x:Name="range_tb" Grid.Column="3" Height="24" VerticalContentAlignment="Center"
                             ToolTip="Like A1:H40. Empty = the print area, or else every used cell."/>
                </Grid>
                <TextBlock x:Name="info_tb" Foreground="#5F5F5F" Margin="110,0,0,8" TextWrapping="Wrap"/>
                <Grid Margin="0,0,0,6">
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="110"/>
                        <ColumnDefinition Width="*"/>
                    </Grid.ColumnDefinitions>
                    <TextBlock Text="View name" VerticalAlignment="Center"/>
                    <TextBox x:Name="name_tb" Grid.Column="1" Height="24" VerticalContentAlignment="Center"/>
                </Grid>
                <Grid Margin="0,0,0,6">
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="110"/>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                        <ColumnDefinition Width="*"/>
                    </Grid.ColumnDefinitions>
                    <TextBlock Text="Text type" VerticalAlignment="Center"/>
                    <ComboBox x:Name="text_cb" Grid.Column="1" Height="24"/>
                    <TextBlock Grid.Column="2" Text="Bold cells" VerticalAlignment="Center" Margin="12,0,6,0"
                               ToolTip="Text type for cells that are bold in Excel (headers)"/>
                    <ComboBox x:Name="bold_cb" Grid.Column="3" Height="24"/>
                </Grid>
                <Grid Margin="0,0,0,6">
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="110"/>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                        <ColumnDefinition Width="*"/>
                    </Grid.ColumnDefinitions>
                    <TextBlock Text="Lines thin" VerticalAlignment="Center"
                               ToolTip="Line style for thin Excel borders"/>
                    <ComboBox x:Name="thin_cb" Grid.Column="1" Height="24"/>
                    <TextBlock Grid.Column="2" Text="medium" VerticalAlignment="Center" Margin="8,0,6,0"/>
                    <ComboBox x:Name="medium_cb" Grid.Column="3" Height="24"/>
                    <TextBlock Grid.Column="4" Text="thick" VerticalAlignment="Center" Margin="8,0,6,0"/>
                    <ComboBox x:Name="thick_cb" Grid.Column="5" Height="24"/>
                </Grid>
                <Grid Margin="0,0,0,6">
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="110"/>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                        <ColumnDefinition Width="*"/>
                    </Grid.ColumnDefinitions>
                    <TextBlock Text="Borders" VerticalAlignment="Center"/>
                    <ComboBox x:Name="borders_cb" Grid.Column="1" Height="24"/>
                    <TextBlock Grid.Column="2" Text="Cell fill" VerticalAlignment="Center" Margin="12,0,6,0"
                               ToolTip="Filled region type for cells that have a colour in Excel"/>
                    <ComboBox x:Name="fill_cb" Grid.Column="3" Height="24"/>
                </Grid>
                <Grid Margin="0,0,0,8">
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="110"/>
                        <ColumnDefinition Width="70"/>
                        <ColumnDefinition Width="*"/>
                    </Grid.ColumnDefinitions>
                    <TextBlock Text="Table size %" VerticalAlignment="Center"
                               ToolTip="Column widths and row heights: 100 = the size Excel shows at 100 % zoom"/>
                    <TextBox x:Name="scale_tb" Grid.Column="1" Height="24" VerticalContentAlignment="Center" Text="100"/>
                </Grid>
                <WrapPanel Margin="110,0,0,10">
                    <CheckBox x:Name="fit_chk" Content="Widen columns / rows to fit text" IsChecked="True" Margin="0,0,16,4"
                              ToolTip="Like Excel's AutoFit: a column becomes wider (a row taller) where a text does not fit"/>
                    <CheckBox x:Name="hidden_chk" Content="Skip hidden rows / columns" IsChecked="True" Margin="0,0,16,4"/>
                    <CheckBox x:Name="place_chk" Content="Place on the active sheet" Margin="0,0,16,4"/>
                </WrapPanel>
                <Button x:Name="create_btn" Content="CREATE TABLE" Height="34" FontWeight="Bold" Background="#2E7D32" Foreground="White"/>
            </StackPanel>
        </Border>

        <TextBlock Grid.Row="1" Text="TABLES IN THIS MODEL" FontWeight="Bold" Margin="0,0,0,4"/>
        <Border Grid.Row="2" BorderBrush="#C9D1DA" BorderThickness="1" Background="White" Margin="0,0,0,6">
            <ScrollViewer VerticalScrollBarVisibility="Auto">
                <StackPanel x:Name="tables_sp" Margin="8,6"/>
            </ScrollViewer>
        </Border>
        <Grid Grid.Row="3" Margin="0,0,0,8">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
            </Grid.ColumnDefinitions>
            <Button x:Name="all_btn" Content="All" Width="50"/>
            <Button x:Name="none_btn" Grid.Column="1" Content="None" Width="50" Margin="6,0,0,0"/>
            <Button x:Name="changed_btn" Grid.Column="2" Content="Changed" Width="70" Margin="6,0,0,0"
                    ToolTip="Tick the tables whose Excel file was saved after their last update"/>
            <Button x:Name="relink_btn" Grid.Column="4" Content="Change file..." Width="96" Margin="6,0,0,0"
                    ToolTip="Point the ticked table(s) to another Excel file, then update them"/>
            <Button x:Name="update_btn" Grid.Column="5" Content="UPDATE TICKED" Width="120" Height="30" Margin="6,0,0,0"
                    ToolTip="Re-reads the Excel and redraws the ticked tables with the settings above (text types, lines, borders, fill, size)"
                    FontWeight="Bold" Background="#1565C0" Foreground="White"/>
        </Grid>
        <TextBlock x:Name="status_tb" Grid.Row="4" Foreground="#5F5F5F" TextWrapping="Wrap"/>
    </Grid>
</Window>'''


def _window_class():
    from pyrevit import forms
    import tg_excel
    import tg_revit

    class TableGenWindow(forms.WPFWindow):
        def __init__(self, doc, active_sheet):
            forms.WPFWindow.__init__(self, XAML, literal_string=True)
            self.doc = doc
            self.active_sheet = active_sheet
            self.choice = None
            self.workbook = None
            self.settings = load_settings()
            for label, _key in BORDERS:
                self.borders_cb.Items.Add(label)
            s = self.settings
            # the model's own text types, line styles and filled region types (names remembered, not ids:
            # ids differ from model to model)
            texts = tg_revit.text_types(doc)
            lines = tg_revit.line_styles(doc)
            fills = tg_revit.fill_types(doc)
            default_text = [n for n, i in texts if i == tg_revit.default_text_type(doc)]
            self.pickers = {
                'text_type': (self.text_cb, texts, s.get('text_type_name') or (default_text[0] if default_text else None), None),
                'bold_type': (self.bold_cb, texts, s.get('bold_type_name'), SAME_TYPE),
                'line_thin': (self.thin_cb, lines, self._line_choice(s, 'line_thin_name', 1), None),
                'line_medium': (self.medium_cb, lines, self._line_choice(s, 'line_medium_name', 2), None),
                'line_thick': (self.thick_cb, lines, self._line_choice(s, 'line_thick_name', 3), None),
                'fill_type': (self.fill_cb, fills, s.get('fill_type_name'), NO_FILL),
            }
            for key, (box, items, wanted, empty) in self.pickers.items():
                if empty:
                    box.Items.Add(empty)
                for name, _number in items:
                    box.Items.Add(name)
                names = [n for n, _i in items]
                if wanted in names:
                    box.SelectedIndex = names.index(wanted) + (1 if empty else 0)
                else:
                    box.SelectedIndex = 0
            self.scale_tb.Text = u'{0}'.format(s.get('scale', 100))
            keys = [k for _l, k in BORDERS]
            self.borders_cb.SelectedIndex = keys.index(s.get('borders')) if s.get('borders') in keys else 0
            self.fit_chk.IsChecked = s.get('fit', True)
            self.hidden_chk.IsChecked = s.get('skip_hidden', True)
            if active_sheet is not None:
                self.place_chk.IsChecked = True
                self.place_chk.Content = u'Place on the active sheet ({0})'.format(active_sheet.SheetNumber)
            else:
                self.place_chk.IsEnabled = False
                self.place_chk.ToolTip = u'Open a sheet first to place the table on it.'
            self.browse_btn.Click += self._browse
            self.sheet_cb.SelectionChanged += self._sheet_changed
            self.range_tb.TextChanged += self._describe
            self.create_btn.Click += self._create
            self.all_btn.Click += lambda a, b: self._tick(lambda link: True)
            self.none_btn.Click += lambda a, b: self._tick(lambda link: False)
            self.changed_btn.Click += lambda a, b: self._tick(self._changed)
            self.update_btn.Click += self._update
            self.relink_btn.Click += self._relink
            self._fill_tables()
            last = s.get('path')
            if last and os.path.isfile(last):
                self._open(last, s.get('sheet'))
            else:
                self.info_tb.Text = u'Choose an Excel file (.xlsx, .xlsm or .csv).'

        # ------------------------------------------------------------ the new-table part
        def _browse(self, sender, args):
            from Microsoft.Win32 import OpenFileDialog
            dialog = OpenFileDialog()
            dialog.Filter = u'Excel workbooks (*.xlsx;*.xlsm;*.csv)|*.xlsx;*.xlsm;*.csv|All files (*.*)|*.*'
            dialog.Title = u'Excel file for the table'
            if self.path_tb.Text and os.path.isdir(os.path.dirname(self.path_tb.Text)):
                dialog.InitialDirectory = os.path.dirname(self.path_tb.Text)
            if dialog.ShowDialog():
                self._open(dialog.FileName, None)

        def _open(self, path, sheet_name):
            self.path_tb.Text = path
            self.sheet_cb.Items.Clear()
            self.workbook = None
            try:
                self.workbook = tg_excel.Workbook(path)
            except tg_excel.TableError as error:
                self.info_tb.Text = u'{0}'.format(error)
                return
            except Exception as error:
                self.info_tb.Text = u'The file could not be read: {0}'.format(error)
                return
            names = [s.name for s in self.workbook.sheets]
            for name in names:
                self.sheet_cb.Items.Add(name)
            self.sheet_cb.SelectedIndex = names.index(sheet_name) if sheet_name in names else 0

        def _sheet_info(self):
            if self.workbook is None or self.sheet_cb.SelectedIndex < 0:
                return None
            return self.workbook.sheets[self.sheet_cb.SelectedIndex]

        def _sheet_changed(self, sender, args):
            info = self._sheet_info()
            if info is None:
                return
            self.range_tb.Text = tg_excel.range_text(info.print_area) if info.print_area else u''
            self.name_tb.Text = u'TABLE - {0}'.format(info.name)
            self._describe(None, None)

        def _describe(self, sender, args):
            info = self._sheet_info()
            if info is None:
                return
            bounds, text = tg_excel.describe(self.workbook, info.name, self.range_tb.Text)
            if bounds and not (self.range_tb.Text or u'').strip():
                text = u'used cells ' + text
            elif bounds and info.print_area and tg_excel.parse_range(self.range_tb.Text) == info.print_area:
                text = u'print area ' + text
            self.info_tb.Text = text

        def _number(self, box, label, low, high):
            try:
                value = float((box.Text or u'').replace('%', '').strip())
            except ValueError:
                value = None
            if value is None or not low <= value <= high:
                forms.alert(u'{0} must be a number from {1} to {2}.'.format(label, low, high), title=TITLE)
                return None
            return value

        def _picked(self):
            """{'text_type': id number, ...} and {'text_type_name': name, ...} from the drop-downs."""
            ids, names = {}, {}
            for key, (box, items, _wanted, empty) in self.pickers.items():
                index = box.SelectedIndex - (1 if empty else 0)
                if 0 <= index < len(items):
                    names[key + '_name'], ids[key] = items[index]
                else:
                    names[key + '_name'], ids[key] = None, 0
            return ids, names

        def _line_choice(self, settings, key, weight):
            """The remembered line style, unless it is a centre / hidden / dashed one (an old wrong
            default); then the best solid style of this model."""
            saved = settings.get(key)
            if saved and not any(w in saved.lower() for w in tg_revit.NOT_A_BORDER):
                return saved
            return tg_revit.default_line_style_name(self.doc, weight)

        def _remember(self, options=None):
            """Save the chosen style NAMES (and size / checkboxes) for next time."""
            settings = load_settings()
            settings.update(self._picked()[1])
            settings['fit'] = bool(self.fit_chk.IsChecked)
            settings['skip_hidden'] = bool(self.hidden_chk.IsChecked)
            settings['borders'] = BORDERS[max(0, self.borders_cb.SelectedIndex)][1]
            if options is not None:
                settings['scale'] = int(round(options.scale * 100))
            save_settings(settings)

        def _style_options(self):
            """Everything in the window except file / sheet / range - UPDATE applies it to the ticked tables."""
            ids, _names = self._picked()
            ids['fit'] = bool(self.fit_chk.IsChecked)
            ids['borders'] = BORDERS[max(0, self.borders_cb.SelectedIndex)][1]
            ids['skip_hidden'] = bool(self.hidden_chk.IsChecked)
            try:
                scale = float((self.scale_tb.Text or u'').replace('%', '').strip())
                if 10 <= scale <= 1000:
                    ids['scale'] = scale / 100.0
            except ValueError:
                pass
            return ids

        def _options(self):
            scale = self._number(self.scale_tb, u'Table size %', 10, 1000)
            if scale is None:
                return None
            if not self._picked()[0].get('text_type'):
                forms.alert(u'Choose a text type.', title=TITLE)
                return None
            values = self._style_options()
            values['scale'] = scale / 100.0
            return tg_revit.Options(**values)

        def _create(self, sender, args):
            info = self._sheet_info()
            if info is None:
                forms.alert(u'Choose an Excel file and a worksheet first.', title=TITLE)
                return
            bounds, text = tg_excel.describe(self.workbook, info.name, self.range_tb.Text)
            if bounds is None:
                forms.alert(text, title=TITLE)
                return
            options = self._options()
            if options is None:
                return
            name = (self.name_tb.Text or u'').strip() or u'TABLE - {0}'.format(info.name)
            for bad in u'{}[]:\\|;<>?`~':
                name = name.replace(bad, u'-')
            self._remember(options)
            settings = load_settings()
            settings.update({'path': self.workbook.path, 'sheet': info.name})
            save_settings(settings)
            self.choice = {'action': 'create', 'sheet': info.name, 'range': (self.range_tb.Text or u'').strip(),
                           'name': name, 'options': options,
                           'place': self.active_sheet if self.place_chk.IsChecked else None}
            self.Close()

        # ------------------------------------------------------------ the existing tables part
        def _fill_tables(self):
            from System.Windows import Thickness
            from System.Windows.Controls import CheckBox
            self.tables = []
            self.tables_sp.Children.Clear()
            for view, link in tg_revit.find_tables(self.doc):
                path = link.get('path') or u''
                state = u''
                if not os.path.isfile(path):
                    state = u'   (EXCEL FILE NOT FOUND)'
                elif tg_revit.file_stamp(path) != link.get('file_time'):
                    state = u'   (Excel changed)'
                box = CheckBox()
                box.Margin = Thickness(0, 2, 0, 2)
                box.Content = u'{0}    -    {1} / {2} {3}   updated {4}{5}'.format(
                    tg_revit._name(view), os.path.basename(path), link.get('sheet') or u'',
                    link.get('range') or u'', link.get('updated') or u'', state)
                box.ToolTip = path
                self.tables_sp.Children.Add(box)
                self.tables.append((view, link, box))
            if not self.tables:
                from System.Windows.Controls import TextBlock
                note = TextBlock()
                note.Text = u'No Table Gen tables in this model yet.'
                note.Foreground = self.status_tb.Foreground
                self.tables_sp.Children.Add(note)
            changed = len([t for t in self.tables if self._changed(t[1])])
            self.status_tb.Text = u'{0} table(s) in this model{1}.'.format(
                len(self.tables), u', {0} with a changed Excel file'.format(changed) if changed else u'')

        @staticmethod
        def _changed(link):
            path = link.get('path') or u''
            return os.path.isfile(path) and tg_revit.file_stamp(path) != link.get('file_time')

        def _tick(self, rule):
            for view, link, box in self.tables:
                box.IsChecked = bool(rule(link))

        def _ticked(self):
            return [view for view, link, box in self.tables if box.IsChecked]

        def _update(self, sender, args):
            views = self._ticked()
            if not views:
                forms.alert(u'Tick the table(s) to update.', title=TITLE)
                return
            self._remember()
            self.choice = {'action': 'update', 'views': views, 'path': None, 'styles': self._style_options()}
            self.Close()

        def _relink(self, sender, args):
            views = self._ticked()
            if not views:
                forms.alert(u'Tick the table(s) that should use another Excel file.', title=TITLE)
                return
            from Microsoft.Win32 import OpenFileDialog
            dialog = OpenFileDialog()
            dialog.Filter = u'Excel workbooks (*.xlsx;*.xlsm;*.csv)|*.xlsx;*.xlsm;*.csv'
            dialog.Title = u'New Excel file for {0} table(s)'.format(len(views))
            if not dialog.ShowDialog():
                return
            self._remember()
            self.choice = {'action': 'update', 'views': views, 'path': dialog.FileName, 'styles': self._style_options()}
            self.Close()

    return TableGenWindow


def main():
    from pyrevit import forms, revit, script
    from Autodesk.Revit.DB import ViewSheet
    import tg_revit
    doc = revit.doc
    if doc is None or doc.IsFamilyDocument:
        forms.alert(u'Open a project first.', title=TITLE)
        return
    active = doc.ActiveView if isinstance(doc.ActiveView, ViewSheet) else None
    window = _window_class()(doc, active)
    window.ShowDialog()
    choice = window.choice
    if not choice:
        return
    results = []
    write_log(u'started: {0}\n(if this is all the log shows, the run stopped - see the error below or in pyRevit)'
              .format(choice['action']))
    try:
        if choice['action'] == 'create':
            results.append(tg_revit.create_table(doc, window.workbook, choice['sheet'], choice['range'],
                                                 choice['name'], choice['options'], choice['place']))
        else:
            for view in choice['views']:
                results.append(tg_revit.update_table(doc, view, choice['path'], choice.get('styles')))
        text = tg_revit.report(results)
    except Exception:
        import traceback
        error = traceback.format_exc()
        write_log(u'ERROR in {0}:\n{1}'.format(choice['action'], error))
        forms.alert(u'Table Gen stopped with an error - it is written in\n{0}\n\n{1}'.format(LOG, error[-800:]),
                    title=TITLE)
        return
    write_log(text)
    output = script.get_output()
    output.set_title(TITLE)
    print(text)
    # open the new table view
    done = [r for r in results if r.view is not None and not r.error]
    if choice['action'] == 'create' and done and not choice['place']:
        try:
            revit.uidoc.ActiveView = done[0].view
        except Exception:
            pass


if __name__ == '__main__':
    main()

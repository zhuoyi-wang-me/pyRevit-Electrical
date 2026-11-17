# -*- coding: utf-8 -*-
"""Update Revision Clouds - give the revision clouds of many sheets a new revision in one go.

Press the button: a window lists every sheet (the sheets selected in the Project Browser, or the
active sheet, are ticked). Tick the sheets, choose which clouds to change ("any revision" or one
revision) and the revision they get, then UPDATE.
    - clouds drawn on the sheet itself are changed;
    - optionally also the clouds drawn inside the views placed on the sheet (a dependent view
      shows the clouds of its parent view, so those are included too);
    - clouds of an ISSUED revision are locked by Revit: they are skipped and listed;
    - only revisions that are not issued can be chosen as the new revision.
One transaction: one Undo puts everything back. A report lists what changed per sheet.
"""
from Autodesk.Revit.DB import (FilteredElementCollector, ViewSheet, RevisionCloud, Revision,
                               ElementId, Transaction, View)

TITLE = u'Update Revision Clouds'
ANY_REVISION = u'<Any revision>'


# ======================================================================= small helpers
def _idv(element_id):
    """The number of an ElementId (Revit 2024+ .Value, older .IntegerValue)."""
    if element_id is None:
        return -1
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue


def _text(value):
    try:
        return u'{0}'.format(value or u'').strip()
    except Exception:
        return u''


def sheet_label(sheet):
    return u'{0} - {1}'.format(_text(sheet.SheetNumber), _text(sheet.Name))


def revision_label(revision):
    """'Seq 3  |  Rev B  |  2026-10-20  |  For construction  (issued)'"""
    parts = [u'Seq {0}'.format(revision.SequenceNumber)]
    try:
        # with "numbering per sheet" Revit has no project-wide number and raises here
        number = _text(revision.RevisionNumber)
    except Exception:
        number = u''
    if number:
        parts.append(u'Rev ' + number)
    for value in (revision.RevisionDate, revision.Description):
        if _text(value):
            parts.append(_text(value))
    label = u'  |  '.join(parts)
    if revision.Issued:
        label += u'  (issued)'
    return label


# ======================================================================= reading the model
def get_sheets(doc):
    """All real sheets (no placeholders), by sheet number."""
    sheets = [s for s in FilteredElementCollector(doc).OfClass(ViewSheet)
              if not s.IsPlaceholder and not s.IsTemplate]
    return sorted(sheets, key=lambda s: _text(s.SheetNumber).lower())


def get_revisions(doc):
    """All revisions in sequence order."""
    revisions = [doc.GetElement(i) for i in Revision.GetAllRevisionIds(doc)]
    return [r for r in revisions if r is not None]


def clouds_by_view(doc):
    """{owner view id number: [revision clouds]} for the whole model (read once)."""
    found = {}
    for cloud in FilteredElementCollector(doc).OfClass(RevisionCloud):
        found.setdefault(_idv(cloud.OwnerViewId), []).append(cloud)
    return found


def views_on_sheet(doc, sheet):
    """Id numbers of the views placed on a sheet, plus the parent of each dependent view
    (a dependent view shows the clouds drawn in its parent)."""
    numbers = set()
    try:
        placed = list(sheet.GetAllPlacedViews())
    except Exception:
        placed = []
    for view_id in placed:
        numbers.add(_idv(view_id))
        view = doc.GetElement(view_id)
        try:
            parent = view.GetPrimaryViewId() if view is not None else None
        except Exception:
            parent = None
        if parent is not None and _idv(parent) > 0:
            numbers.add(_idv(parent))
    numbers.discard(_idv(sheet.Id))
    return numbers


class SheetClouds(object):
    """The clouds one sheet shows: on the sheet itself and inside its views."""
    def __init__(self, sheet, own, in_views, view_numbers):
        self.sheet = sheet
        self.own = own
        self.in_views = in_views
        self.view_numbers = view_numbers

    def clouds(self, include_views, from_revision_id=None):
        chosen = list(self.own) + (list(self.in_views) if include_views else [])
        if from_revision_id is not None:
            wanted = _idv(from_revision_id)
            chosen = [c for c in chosen if _idv(c.RevisionId) == wanted]
        return chosen


def collect(doc, sheets):
    """{sheet id number: SheetClouds} and {view id number: set of sheet id numbers showing it}."""
    by_view = clouds_by_view(doc)
    result, shown_on = {}, {}
    for sheet in sheets:
        numbers = views_on_sheet(doc, sheet)
        in_views = []
        for number in sorted(numbers):
            in_views.extend(by_view.get(number, []))
            shown_on.setdefault(number, set()).add(_idv(sheet.Id))
        result[_idv(sheet.Id)] = SheetClouds(sheet, by_view.get(_idv(sheet.Id), []), in_views, numbers)
    return result, shown_on


# ======================================================================= the change
class Result(object):
    def __init__(self):
        self.per_sheet = []        # (sheet label, changed, already, locked)
        self.changed = 0
        self.already = 0
        self.locked = []           # (sheet label, cloud id, revision label)
        self.errors = []           # (sheet label, cloud id, text)
        self.shared = []           # (view name, other sheet labels) - views also on unticked sheets
        self.target = u''


def update_clouds(doc, sheet_clouds, shown_on, sheet_ids, target_id, from_revision_id=None, include_views=True):
    """Give every cloud of the ticked sheets the target revision. One transaction."""
    result = Result()
    target = doc.GetElement(target_id)
    result.target = revision_label(target) if target is not None else u'?'
    if target is None:
        result.errors.append((u'-', u'-', u'the chosen revision no longer exists'))
        return result
    if target.Issued:
        result.errors.append((u'-', u'-', u'the chosen revision is issued - Revit does not allow clouds on it'))
        return result
    wanted = _idv(target_id)
    ticked = set(_idv(i) for i in sheet_ids)
    done = set()                   # a cloud in a view on two sheets is changed once
    issued = {}                    # revision id number -> issued?
    labels = {}

    def is_issued(revision_id):
        number = _idv(revision_id)
        if number not in issued:
            revision = doc.GetElement(revision_id)
            issued[number] = bool(revision is not None and revision.Issued)
            labels[number] = revision_label(revision) if revision is not None else u'?'
        return issued[number]

    transaction = Transaction(doc, TITLE)
    transaction.Start()
    try:
        for number in sheet_ids:
            entry = sheet_clouds.get(_idv(number))
            if entry is None:
                continue
            label = sheet_label(entry.sheet)
            changed = already = locked = 0
            for cloud in entry.clouds(include_views, from_revision_id):
                key = _idv(cloud.Id)
                if key in done:
                    continue
                done.add(key)
                if _idv(cloud.RevisionId) == wanted:
                    already += 1
                    continue
                if is_issued(cloud.RevisionId):
                    locked += 1
                    result.locked.append((label, key, labels[_idv(cloud.RevisionId)]))
                    continue
                try:
                    cloud.RevisionId = target_id
                    changed += 1
                except Exception as error:
                    result.errors.append((label, key, _text(error).split(u'\n')[0]))
            result.per_sheet.append((label, changed, already, locked))
            result.changed += changed
            result.already += already
            # views of this sheet that other (unticked) sheets also show - their clouds changed there too
            if include_views:
                for view_number in entry.view_numbers:
                    others = shown_on.get(view_number, set()) - ticked
                    if others and changed:
                        view = doc.GetElement(ElementId(view_number))
                        names = [sheet_label(sheet_clouds[o].sheet) for o in sorted(others) if o in sheet_clouds]
                        result.shared.append((_text(getattr(view, 'Name', view_number)), names))
        transaction.Commit()
    except Exception as error:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
        result.errors.append((u'-', u'-', u'Nothing was changed: {0}'.format(error)))
        result.changed = 0
    return result


def report(result, from_label):
    line = u'=' * 44
    out = [line, TITLE, line,
           u'New revision:     ' + result.target,
           u'Clouds changed:   ' + from_label, u'',
           u'Sheets:           {0:>5}'.format(len(result.per_sheet)),
           u'Clouds updated:   {0:>5}'.format(result.changed),
           u'Already on it:    {0:>5}'.format(result.already),
           u'Locked (issued):  {0:>5}'.format(len(result.locked)),
           u'Errors:           {0:>5}'.format(len(result.errors)), line]
    if result.per_sheet:
        out += [u'', u'Per sheet (updated / already / locked):']
        out += [u'  {0}   {1} / {2} / {3}'.format(l, c, a, k) for l, c, a, k in result.per_sheet]
    if result.locked:
        out += [u'', u'Locked - the cloud belongs to an issued revision (un-tick "Issued" in Sheet Issues/Revisions to change it):']
        out += [u'  {0}   cloud {1}   {2}'.format(l, i, r) for l, i, r in result.locked]
    if result.shared:
        out += [u'', u'Note - these views are also on sheets you did not tick, their clouds changed there too:']
        out += [u'  {0}   ->   {1}'.format(v, u', '.join(n)) for v, n in result.shared]
    if result.errors:
        out += [u'', u'Errors:'] + [u'  {0}   cloud {1}   {2}'.format(l, i, t) for l, i, t in result.errors]
    return u'\n'.join(out)


# ======================================================================= the window
XAML = u'''<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Update Revision Clouds" Width="560" Height="680" MinWidth="460" MinHeight="520"
        WindowStartupLocation="CenterScreen" FontFamily="Segoe UI" FontSize="12" Background="#F4F6F8">
    <Grid Margin="16">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>
        <TextBlock Grid.Row="0" Text="Sheets" FontWeight="Bold" Margin="0,0,0,4"/>
        <Grid Grid.Row="1" Margin="0,0,0,6">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
            </Grid.ColumnDefinitions>
            <TextBox x:Name="search_tb" Grid.Column="0" Height="24" VerticalContentAlignment="Center" ToolTip="Search sheet number or name"/>
            <Button x:Name="all_btn" Grid.Column="1" Content="All" Width="56" Margin="6,0,0,0"/>
            <Button x:Name="none_btn" Grid.Column="2" Content="None" Width="56" Margin="6,0,0,0"/>
            <CheckBox x:Name="clouds_only_chk" Grid.Column="3" Content="Only with clouds" VerticalAlignment="Center" Margin="10,0,0,0"/>
        </Grid>
        <Border Grid.Row="2" BorderBrush="#C9D1DA" BorderThickness="1" Background="White" Margin="0,0,0,12">
            <ScrollViewer VerticalScrollBarVisibility="Auto">
                <StackPanel x:Name="sheets_sp" Margin="8,6"/>
            </ScrollViewer>
        </Border>
        <StackPanel Grid.Row="3">
            <TextBlock Text="Change clouds of" FontWeight="Bold" Margin="0,0,0,4"/>
            <ComboBox x:Name="from_cb" Margin="0,0,0,10"/>
            <TextBlock Text="New revision" FontWeight="Bold" Margin="0,0,0,4"/>
            <ComboBox x:Name="to_cb" Margin="0,0,0,10"/>
            <CheckBox x:Name="views_chk" Content="Also clouds inside the views placed on the sheets" IsChecked="True" Margin="0,0,0,12"/>
            <Button x:Name="update_btn" Content="UPDATE" Height="34" FontWeight="Bold" Background="#2E7D32" Foreground="White"/>
            <TextBlock x:Name="status_tb" Foreground="#5F5F5F" TextWrapping="Wrap" Margin="0,8,0,0"/>
        </StackPanel>
    </Grid>
</Window>'''


def _window_class():
    from pyrevit import forms

    class RevisionCloudsWindow(forms.WPFWindow):
        def __init__(self, doc, preselected):
            forms.WPFWindow.__init__(self, XAML, literal_string=True)
            from System.Windows import Thickness
            from System.Windows.Controls import CheckBox
            self.doc = doc
            self.choice = None
            self.sheets = get_sheets(doc)
            self.sheet_clouds, self.shown_on = collect(doc, self.sheets)
            self.revisions = get_revisions(doc)
            self.open_revisions = [r for r in self.revisions if not r.Issued]
            # sheet list
            self.boxes = []
            for sheet in self.sheets:
                box = CheckBox()
                box.Margin = Thickness(0, 2, 0, 2)
                box.IsChecked = _idv(sheet.Id) in preselected
                box.Checked += self._refresh
                box.Unchecked += self._refresh
                self.sheets_sp.Children.Add(box)
                self.boxes.append((sheet, box))
            # revisions
            self.from_cb.Items.Add(ANY_REVISION)
            for revision in self.revisions:
                self.from_cb.Items.Add(revision_label(revision))
            self.from_cb.SelectedIndex = 0
            for revision in self.open_revisions:
                self.to_cb.Items.Add(revision_label(revision))
            if self.open_revisions:
                self.to_cb.SelectedIndex = len(self.open_revisions) - 1      # the latest open revision
            # events
            self.search_tb.TextChanged += self._filter
            self.clouds_only_chk.Checked += self._filter
            self.clouds_only_chk.Unchecked += self._filter
            self.all_btn.Click += lambda s, a: self._tick_visible(True)
            self.none_btn.Click += lambda s, a: self._tick_visible(False)
            self.from_cb.SelectionChanged += self._refresh
            self.views_chk.Checked += self._refresh
            self.views_chk.Unchecked += self._refresh
            self.update_btn.Click += self._update
            self._refresh(None, None)
            if not self.open_revisions:
                self.update_btn.IsEnabled = False
                self.status_tb.Text = (u'Every revision is issued (or there is none). Add a revision, or un-tick '
                                       u'"Issued", in View > Sheet Issues/Revisions first.')

        # ---------------------------------------------------------- helpers
        def _from_id(self):
            index = self.from_cb.SelectedIndex
            return self.revisions[index - 1].Id if index > 0 else None

        def _refresh(self, sender, args):
            """Cloud counts in the labels and the total of the ticked sheets."""
            include = bool(self.views_chk.IsChecked)
            from_id = self._from_id()
            total, ticked = 0, 0
            for sheet, box in self.boxes:
                count = len(self.sheet_clouds[_idv(sheet.Id)].clouds(include, from_id))
                box.Content = u'{0}    ({1} cloud{2})'.format(sheet_label(sheet), count, u'' if count == 1 else u's')
                box.Tag = count
                if box.IsChecked:
                    ticked += 1
                    total += count
            if self.open_revisions:
                self.status_tb.Text = u'{0} sheet(s) ticked - {1} cloud(s) to check.'.format(ticked, total)
            self._filter(None, None)          # "Only with clouds" depends on the counts

        def _filter(self, sender, args):
            from System.Windows import Visibility
            words = _text(self.search_tb.Text).lower().split()
            only = bool(self.clouds_only_chk.IsChecked)
            for sheet, box in self.boxes:
                text = sheet_label(sheet).lower()
                show = all(w in text for w in words) and (not only or (box.Tag or 0) > 0)
                box.Visibility = Visibility.Visible if show else Visibility.Collapsed

        def _tick_visible(self, value):
            from System.Windows import Visibility
            for sheet, box in self.boxes:
                if box.Visibility == Visibility.Visible:
                    box.IsChecked = value

        def _update(self, sender, args):
            ticked = [sheet.Id for sheet, box in self.boxes if box.IsChecked]
            if not ticked:
                forms.alert(u'Tick at least one sheet.', title=TITLE)
                return
            if self.to_cb.SelectedIndex < 0:
                forms.alert(u'Choose the new revision.', title=TITLE)
                return
            self.choice = {
                'sheets': ticked,
                'from': self._from_id(),
                'from_label': self.from_cb.SelectedItem,
                'to': self.open_revisions[self.to_cb.SelectedIndex].Id,
                'views': bool(self.views_chk.IsChecked),
            }
            self.Close()

    return RevisionCloudsWindow


def preselected_sheets(doc, uidoc):
    """Sheets selected in the Project Browser, else the active sheet."""
    numbers = set()
    try:
        for element_id in uidoc.Selection.GetElementIds():
            if isinstance(doc.GetElement(element_id), ViewSheet):
                numbers.add(_idv(element_id))
    except Exception:
        pass
    if not numbers and isinstance(doc.ActiveView, ViewSheet):
        numbers.add(_idv(doc.ActiveView.Id))
    return numbers


def main():
    from pyrevit import forms, revit, script
    doc, uidoc = revit.doc, revit.uidoc
    if doc is None or doc.IsFamilyDocument:
        forms.alert(u'Open a project first.', title=TITLE)
        return
    window = _window_class()(doc, preselected_sheets(doc, uidoc))
    window.ShowDialog()
    choice = window.choice
    if not choice:
        return                                          # closed without UPDATE
    result = update_clouds(doc, window.sheet_clouds, window.shown_on, choice['sheets'], choice['to'],
                           choice['from'], choice['views'])
    output = script.get_output()
    output.set_title(TITLE)
    print(report(result, choice['from_label']))


if __name__ == '__main__':
    main()

# -*- coding: utf-8 -*-
"""Auto Tag Layout - the window (modeless: it stays open while you work in Revit).

Lists the categories seen in the active view, with a tick and a tag type for each.
"Preview options" shows the possible arrangements as small drawings; the one the user
picks is created. "Create recommended" creates the best-ranked one at once.
"Refresh" reads the active view and the selection again.

Because the window does not block Revit, Revit may only be touched through an
ExternalEvent: every button that reads or changes the model hands its work to Revit.
"""
import json
import os
import traceback

import clr
# the WPF libraries must be referenced before "System.Windows" can be imported
for _name in ('PresentationFramework', 'PresentationCore', 'WindowsBase', 'System.Xaml'):
    clr.AddReference(_name)

from System.Windows import GridLength, GridUnitType, Thickness, VerticalAlignment
from System.Windows.Controls import CheckBox, ColumnDefinition, ComboBox, Grid
from System.Windows.Media import BrushConverter

from pyrevit import forms, revit
from Autodesk.Revit.UI import ExternalEvent, IExternalEventHandler

import atl_layout
import atl_options
import atl_revit

SETTINGS = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'EleTools', 'AutoTagLayout', 'settings.json')

RED, DARK, GREEN = '#C42B1C', '#1B1B1B', '#0F7B0F'
BAR = {RED: ('#FDE7E9', '#F3C6CA'), GREEN: ('#DFF6DD', '#BFE5BC'), DARK: ('#F9F9F9', '#E5E5E5')}
_OPEN = []                                                 # keeps the open window and its ExternalEvent alive


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
        pass                                               # remembering the choices is only a convenience


def number(text, least):
    """Number typed by the user ('10', '2,5'); None when it is not a number of at least `least`."""
    try:
        value = float((text or u'').strip().replace(u',', u'.'))
    except ValueError:
        return None
    return value if value >= least else None


class _Handler(IExternalEventHandler):
    """Runs the queued work when Revit is ready for it."""

    def __init__(self, on_error):
        self.queue = []
        self.on_error = on_error

    def Execute(self, uiapp):
        pending, self.queue = self.queue, []
        for action in pending:
            try:
                action(uiapp.ActiveUIDocument)
            except Exception:
                try:
                    self.on_error(traceback.format_exc())
                except Exception:
                    pass

    def GetName(self):
        return 'Auto Tag Layout'


class TagWindow(forms.WPFWindow):
    def __init__(self, uidoc):
        forms.WPFWindow.__init__(self, os.path.join(os.path.dirname(__file__), 'ui.xaml'))
        self._brushes = BrushConverter()
        self.settings = _load_settings()
        self.rows = []                                     # (CheckBox, ComboBox of tag types, Group)
        self.places = {}                                   # group name -> ComboBox "where the tags go"
        self.doc = self.view = None
        self.selected = set()
        self._handler = _Handler(lambda text: self.say(text, RED))
        self._event = ExternalEvent.Create(self._handler)


        self.txtOffset.Text = u'{0}'.format(self.settings.get('offset', u'10'))
        self.txtGap.Text = u'{0}'.format(self.settings.get('gap', u'1'))
        self.txtLeaderGap.Text = u'{0}'.format(self.settings.get('leader_gap', u'3'))

        self.chkSkip.IsChecked = bool(self.settings.get('skip', True))
        self.chkOne.IsChecked = bool(self.settings.get('one', True))
        self.chkLinks.IsChecked = bool(self.settings.get('links', False))
        self.chkInside.IsChecked = bool(self.settings.get('inside', True))

        self.chkSelected.Click += self._guard(lambda: self.in_revit(self.read))
        self.chkLinks.Click += self._guard(lambda: self.in_revit(self.read))
        self.btnRefresh.Click += self._guard(lambda: self.in_revit(lambda uidoc: self.read(uidoc, True)))
        self.btnAll.Click += self._guard(lambda: self.tick(True))
        self.btnNone.Click += self._guard(lambda: self.tick(False))
        self.btnCreate.Click += self._guard(self.on_create)
        self.btnPreview.Click += self._guard(self.on_preview)
        self.btnClose.Click += self._guard(lambda: self.Close())
        self.Closed += self.on_closed
        # the window is opened from the button, inside Revit: this first reading needs no event
        self._guard(lambda: self.read(uidoc, True))(None, None)

    # ------------------------------------------------------------------
    def _guard(self, action):
        """An event handler that shows an error instead of failing silently."""
        def handler(sender, args):
            try:
                action()
            except Exception:
                self.say(traceback.format_exc(), RED)
        return handler

    def in_revit(self, action):
        """Let Revit run action(uidoc) as soon as it is ready."""
        self._handler.queue.append(action)
        self._event.Raise()

    def on_closed(self, sender, args):
        try:
            self._event.Dispose()
        except Exception:
            pass
        if self in _OPEN:
            _OPEN.remove(self)

    def say(self, text, colour=DARK):
        self._colour = colour
        self.txtMessages.Text = text
        self.txtMessages.Foreground = self._brushes.ConvertFromString(colour)
        background, border = BAR.get(colour, BAR[DARK])
        self.barMessages.Background = self._brushes.ConvertFromString(background)
        self.barMessages.BorderBrush = self._brushes.ConvertFromString(border)

    # ------------------------------------------------------------------
    # what is in the active view (read only) - runs inside Revit
    # ------------------------------------------------------------------
    def read(self, uidoc, fresh=False, keep_message=False):
        """fresh: the view or the selection may have changed - look at the selection again."""
        self.doc, self.view = uidoc.Document, uidoc.ActiveView
        try:
            self.selected = set(eid.Value for eid in uidoc.Selection.GetElementIds())
        except Exception:
            self.selected = set()
        if fresh:
            self.chkSelected.IsChecked = bool(self.selected)
        self.chkSelected.IsEnabled = bool(self.selected)
        self.chkSelected.Content = (u'Only the {0} element(s) selected in Revit'.format(len(self.selected)) if self.selected
                                    else u'Only selected elements (none selected)')
        if not self.selected:
            self.chkSelected.IsChecked = False

        ticked = dict((group.name, bool(box.IsChecked)) for box, _, group in self.rows)
        chosen = dict((group.name, combo.SelectedItem) for _, combo, group in self.rows)
        placed = dict((name, self.mode_of(name)) for name in self.places)
        self.pnlGroups.Children.Clear()
        self.rows = []
        self.places = {}
        problem = atl_revit.view_problem(self.view)
        if problem:
            self.lblView.Text = u'View: {0}'.format(self.view.Name)
            self.btnCreate.IsEnabled = self.btnPreview.IsEnabled = False
            self.say(problem + u'  Then press Refresh.', RED)
            return
        only = self.selected if self.chkSelected.IsChecked else None
        groups = atl_revit.find_groups(self.doc, self.view, only, bool(self.chkLinks.IsChecked))
        remembered = self.settings.get('types') or {}
        was_ticked = self.settings.get('ticked') or []
        for group in groups:
            row = Grid()
            row.Margin = Thickness(8, 5, 8, 5)
            first, second, third = ColumnDefinition(), ColumnDefinition(), ColumnDefinition()
            first.Width = GridLength(1, GridUnitType.Star)
            second.Width = GridLength(1.25, GridUnitType.Star)
            third.Width = GridLength(0.85, GridUnitType.Star)
            row.ColumnDefinitions.Add(first)
            row.ColumnDefinitions.Add(second)
            row.ColumnDefinitions.Add(third)
            box = CheckBox()
            box.Content = u'{0}   ({1})'.format(group.name, len(group.targets))
            box.VerticalAlignment = VerticalAlignment.Center
            box.IsChecked = ticked.get(group.name, group.name in was_ticked)
            combo = ComboBox()
            labels = [label for label, _ in group.types]
            for label in labels:
                combo.Items.Add(label)
            wanted = chosen.get(group.name) or remembered.get(group.name)
            combo.SelectedIndex = labels.index(wanted) if wanted in labels else 0
            Grid.SetColumn(combo, 1)
            # where the tags of this category go: at the sides with a leader, or beside the element;
            # room tags always go inside their room
            place = ComboBox()
            place.Margin = Thickness(8, 0, 0, 0)
            if group.rooms:
                place.Items.Add(u'Inside the room')
                place.SelectedIndex = 0
                place.IsEnabled = False
            else:
                for _key, title in atl_revit.MODES:
                    place.Items.Add(title)
                keys = [key for key, _title in atl_revit.MODES]
                wanted_mode = placed.get(group.name) or (self.settings.get('modes') or {}).get(group.name) or group.mode
                place.SelectedIndex = keys.index(wanted_mode) if wanted_mode in keys else 0
            Grid.SetColumn(place, 2)
            self.places[group.name] = place
            row.Children.Add(box)
            row.Children.Add(combo)
            row.Children.Add(place)
            self.pnlGroups.Children.Add(row)
            self.rows.append((box, combo, group))
        total = sum(len(group.targets) for group in groups)
        self.lblView.Text = u'View: {0}   |   scale 1 : {1}   |   {2} element(s) in {3} categories can be tagged'.format(
            self.view.Name, self.view.Scale, total, len(groups))
        self.btnCreate.IsEnabled = self.btnPreview.IsEnabled = bool(groups)
        if keep_message:
            return
        if not groups:
            self.say(u'Nothing to tag: this view shows no elements for which a tag family is loaded. '
                     u'Load the tag families (Insert > Load Family) and press Refresh.', RED)
        else:
            self.say(u'Tick the categories, choose the tag for each and where it goes: at the sides with a leader, or beside the element '
                     u'(lighting). Room tags go inside their room. "Preview options" shows the possible arrangements to choose from; '
                     u'"Create recommended" creates the best one at once. '
                     u'This window can stay open: after changing the view or the selection in Revit, press Refresh.')

    def mode_of(self, name):
        """Where the tags of the category go, as chosen in its row."""
        place = self.places.get(name)
        if place is None or not place.IsEnabled:
            return None
        return atl_revit.MODES[max(0, place.SelectedIndex)][0]

    def tick(self, state):
        for box, _, _ in self.rows:
            box.IsChecked = bool(state)

    # ------------------------------------------------------------------
    # create: the only place where the model is changed - runs inside Revit
    # ------------------------------------------------------------------
    def inputs(self):
        """(gap, offset, leader gap) typed by the user, saved with the other choices; None after showing what is wrong."""
        offset, gap, leader_gap = number(self.txtOffset.Text, 0.0), number(self.txtGap.Text, 0.0), number(self.txtLeaderGap.Text, 0.0)
        if offset is None or gap is None or leader_gap is None:
            self.say(u'The distance and the two spaces must be numbers in mm, e.g. 10, 1 and 3.', RED)
            return None
        if not [box for box, _, _ in self.rows if box.IsChecked]:
            self.say(u'Tick at least one category.', RED)
            return None
        self.settings.update({
            'offset': self.txtOffset.Text, 'gap': self.txtGap.Text, 'leader_gap': self.txtLeaderGap.Text,
            'skip': bool(self.chkSkip.IsChecked), 'one': bool(self.chkOne.IsChecked), 'links': bool(self.chkLinks.IsChecked), 'inside': bool(self.chkInside.IsChecked),
            'ticked': [group.name for box, _, group in self.rows if box.IsChecked],
        })
        types = self.settings.get('types') or {}
        for _, combo, group in self.rows:
            types[group.name] = group.types[max(0, combo.SelectedIndex)][0]
        self.settings['types'] = types
        modes = self.settings.get('modes') or {}
        for name in self.places:
            if self.mode_of(name):
                modes[name] = self.mode_of(name)
        self.settings['modes'] = modes
        _save_settings(self.settings)
        return gap, offset, leader_gap

    def on_create(self):
        """Create recommended: the arrangement that ranks first, without looking at the others."""
        values = self.inputs()
        if values is None:
            return
        self.say(u'Creating tags ...')
        self.in_revit(lambda uidoc: self.create(uidoc, values[0], values[1], values[2], atl_revit.BEST))

    def on_preview(self):
        values = self.inputs()
        if values is None:
            return
        self.say(u'Working out the arrangements ...')
        self.in_revit(lambda uidoc: self.preview(uidoc, *values))

    def still_this_view(self, uidoc):
        if self.view is None or uidoc.ActiveView.Id != self.view.Id or uidoc.Document.Title != self.doc.Title:
            self.read(uidoc, True)                         # the list on screen belongs to another view
            self.say(u'The active view has changed, so the list was read again. Check it and press the button once more.', RED)
            return False
        return True

    def choices(self):
        """[(group, tag type, where the tags go)] of the ticked rows."""
        return [(group, group.types[max(0, combo.SelectedIndex)][1], self.mode_of(group.name) or group.mode)
                for box, combo, group in self.rows if box.IsChecked]

    def preview(self, uidoc, gap, offset, leader_gap):
        """Work out the arrangements (nothing stays in the model), show them, create the one the user picks."""
        if not self.still_this_view(uidoc):
            return
        found = []
        report = atl_revit.create_tags(self.doc, self.view, self.choices(), gap, offset, atl_layout.BOTH,
                                       bool(self.chkSkip.IsChecked), bool(self.chkOne.IsChecked), leader_gap,
                                       bool(self.chkInside.IsChecked), preview=found)
        if report.error:
            self.say(report.error, RED)
            return
        if not found:
            self.say(u'\n'.join(report.lines()[1:]) or u'There is nothing to tag with these choices.', RED)
            return
        note = u''
        if report.annotation is not None:
            note = u'Annotation crop: {0:.0f} mm left and {1:.0f} mm right of the crop region.'.format(*report.annotation)
        key = self.pick(found, report, note)
        if key is None:
            self.say(u'No arrangement chosen: nothing was created.')
            return
        self.create(uidoc, gap, offset, leader_gap, key)

    def pick(self, found, report, note):
        """The window with the previews; returns the key of the chosen arrangement or None."""
        return atl_options.choose(found, report.view_box, report.limits, self, note)

    def create(self, uidoc, gap, offset, leader_gap, variant=atl_revit.BEST):
        if not self.still_this_view(uidoc):
            return
        choices = self.choices()
        report = atl_revit.create_tags(self.doc, self.view, choices, gap, offset, atl_layout.BOTH,
                                       bool(self.chkSkip.IsChecked), bool(self.chkOne.IsChecked), leader_gap,
                                       bool(self.chkInside.IsChecked), variant=variant)
        if report.error:
            self.say(report.error, RED)
            return
        problem = report.failed or report.unaligned or report.outside or report.pulled_in or not report.created
        self.read(uidoc, keep_message=True)                # count again what is left to tag
        self.say(u'\n'.join(report.lines()) + (u'\nUndo (Ctrl+Z) in Revit removes all of them at once.' if report.created else u''),
                 RED if problem else GREEN)


def main():
    uidoc = revit.uidoc
    for window in list(_OPEN):                             # already open: bring it to the front and read again
        try:
            if window.IsVisible:
                window.read(uidoc, True)
                window.Activate()
                return window
        except Exception:
            pass
    window = TagWindow(uidoc)
    _OPEN.append(window)
    window.show()                                          # modeless
    return window

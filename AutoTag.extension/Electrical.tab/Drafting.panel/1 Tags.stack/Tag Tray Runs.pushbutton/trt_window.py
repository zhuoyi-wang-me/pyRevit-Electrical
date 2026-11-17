# -*- coding: utf-8 -*-
"""Tag Tray Runs - the window (modeless: it stays open while you work in Revit).

Shows the cable trays and conduits of the active plan, a tag type for each, how often the tag is
repeated along a run and how far it stands from the tray. "Tag runs" creates the tags (one
transaction: one Undo removes them all). "Refresh" reads the active view and the selection again.

Because the window does not block Revit, Revit may only be touched through an ExternalEvent.
"""
import json
import os
import traceback

import clr
# the WPF libraries must be referenced before "System.Windows" can be imported
for _name in ('PresentationFramework', 'PresentationCore', 'WindowsBase', 'System.Xaml'):
    clr.AddReference(_name)

from System.Windows.Media import BrushConverter

from pyrevit import forms, revit
from Autodesk.Revit.UI import ExternalEvent, IExternalEventHandler

import trt_revit

SETTINGS = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'EleTools', 'TagTrayRuns', 'settings.json')
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
    """Number typed by the user ('15', '2,5'); None when it is not a number of at least `least`."""
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
        return 'Tag Tray Runs'


class RunWindow(forms.WPFWindow):
    def __init__(self, uidoc):
        forms.WPFWindow.__init__(self, os.path.join(os.path.dirname(__file__), 'ui.xaml'))
        self._brushes = BrushConverter()
        self.settings = _load_settings()
        self.doc = self.view = None
        self.selected = set()
        self.types = {}
        self._handler = _Handler(lambda text: self.say(text, RED))
        self._event = ExternalEvent.Create(self._handler)
        # one row per category: (category, tick, tag type box, count label)
        self.rows = [('OST_CableTray', self.chkTrays, self.cmbTrays, self.lblTrays),
                     ('OST_Conduit', self.chkConduits, self.cmbConduits, self.lblConduits)]
        ticked = self.settings.get('ticked', ['OST_CableTray'])
        for category, box, combo, label in self.rows:
            box.IsChecked = category in ticked
        self.txtSpacing.Text = u'{0}'.format(self.settings.get('spacing', u'15'))
        self.txtOffset.Text = u'{0}'.format(self.settings.get('offset', u'2'))
        self.txtGap.Text = u'{0}'.format(self.settings.get('gap', u'1'))
        self.txtLeader.Text = u'{0}'.format(self.settings.get('leader', u'8'))
        self.txtBundle.Text = u'{0}'.format(self.settings.get('bundle_mm', u'15'))
        self.chkBundle.IsChecked = bool(self.settings.get('bundle', True))
        for key, label in trt_revit.STYLES:
            self.cmbStyle.Items.Add(label)
        styles = [key for key, label in trt_revit.STYLES]
        style = self.settings.get('style', trt_revit.LEADER)
        self.cmbStyle.SelectedIndex = styles.index(style) if style in styles else 0
        self.chkLinks.IsChecked = bool(self.settings.get('links', False))
        self.chkSkip.IsChecked = bool(self.settings.get('skip', True))
        self.chkInside.IsChecked = bool(self.settings.get('inside', True))
        self.chkHatch.IsChecked = bool(self.settings.get('hatch', True))
        self.chkWalls.IsChecked = bool(self.settings.get('walls', True))

        self.chkLinks.Click += self._guard(lambda: self.in_revit(self.read))
        self.chkSelected.Click += self._guard(lambda: self.in_revit(lambda uidoc: self.read(uidoc, keep_selection=True)))
        self.btnRefresh.Click += self._guard(lambda: self.in_revit(self.read))
        self.btnCreate.Click += self._guard(self.on_create)
        self.btnPreview.Click += self._guard(self.on_preview)
        self.btnClose.Click += self._guard(lambda: self.Close())
        self.Closed += self.on_closed
        # the window is opened from the button, inside Revit: this first reading needs no event
        self._guard(lambda: self.read(uidoc))(None, None)

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
        self.txtMessages.Text = text
        self.txtMessages.Foreground = self._brushes.ConvertFromString(colour)
        background, border = BAR.get(colour, BAR[DARK])
        self.barMessages.Background = self._brushes.ConvertFromString(background)
        self.barMessages.BorderBrush = self._brushes.ConvertFromString(border)

    # ------------------------------------------------------------------
    # reading the model (inside Revit)
    # ------------------------------------------------------------------
    def read(self, uidoc, keep_selection=False):
        self.doc, self.view = uidoc.Document, uidoc.ActiveView
        try:
            self.selected = set(eid.Value for eid in uidoc.Selection.GetElementIds())
        except Exception:
            self.selected = set()
        self.chkSelected.IsEnabled = bool(self.selected)
        if not self.selected:
            self.chkSelected.IsChecked = False
        elif not keep_selection:
            self.chkSelected.IsChecked = True              # elements are selected: start with only those
        self.chkSelected.Content = (u'Only selected elements ({0})'.format(len(self.selected)) if self.selected
                                    else u'Only selected elements (none)')
        problem = trt_revit.view_problem(self.view)
        self.lblView.Text = u'View: {0}   |   scale 1 : {1}'.format(self.view.Name, getattr(self.view, 'Scale', u'?'))
        if problem:
            self.btnCreate.IsEnabled = self.btnPreview.IsEnabled = False
            for category, box, combo, label in self.rows:
                label.Text = u''
            self.say(problem, RED)
            return
        self.types = trt_revit.tag_types(self.doc)
        targets = trt_revit.find_targets(self.doc, self.view, bool(self.chkLinks.IsChecked), self._only())
        wanted = self.settings.get('types', {})
        for category, box, combo, label in self.rows:
            current = combo.SelectedItem or wanted.get(category)
            combo.Items.Clear()
            for name, symbol in self.types.get(category, []):
                combo.Items.Add(name)
            if current in [name for name, symbol in self.types.get(category, [])]:
                combo.SelectedItem = current
            elif combo.Items.Count:
                combo.SelectedIndex = 0
            count = len(targets.get(category, []))
            label.Text = u'{0} piece(s) in the view'.format(count) if self.types.get(category) else u'no tag type loaded'
            box.IsEnabled = combo.IsEnabled = bool(self.types.get(category))
        # the hatches (filled regions) of the view
        hatches = trt_revit.region_types(self.doc, self.view)
        wanted_hatch = self.cmbHatch.SelectedItem if self.cmbHatch.SelectedIndex > 0 else self.settings.get('hatch_name')
        self.cmbHatch.Items.Clear()
        self.cmbHatch.Items.Add(u'Any filled region')
        for name in sorted(hatches):
            self.cmbHatch.Items.Add(name)
        self.cmbHatch.SelectedItem = wanted_hatch if wanted_hatch in hatches else u'Any filled region'
        self.chkHatch.IsEnabled = self.cmbHatch.IsEnabled = bool(hatches)
        self.chkHatch.Content = (u'Keep the tags inside the hatch ({0} in the view)'.format(sum(hatches.values())) if hatches
                                 else u'Keep the tags inside the hatch (none in view)')
        self.btnCreate.IsEnabled = self.btnPreview.IsEnabled = True
        self.say(u'Every tray run gets its tag, repeated along long runs: away from the tray with a leader (as drawn by hand), '
                 u'or right beside it. The tags keep clear of other trays, walls, tags and text. "Preview options..." shows the '
                 u'arrangements first; only creating changes the model.')

    def _only(self):
        return self.selected if (self.chkSelected.IsChecked and self.selected) else None

    def still_this_view(self, uidoc):
        try:
            same = uidoc.ActiveView.Id == self.view.Id and uidoc.Document.Equals(self.doc)
        except Exception:
            same = uidoc.ActiveView is self.view
        if not same:
            self.read(uidoc)
            self.say(u'The active view changed: the window was read again for it. Check the choices and press "Tag runs" again.', RED)
        return same

    # ------------------------------------------------------------------
    # changing the model
    # ------------------------------------------------------------------
    def choices(self):
        found = []
        for category, box, combo, label in self.rows:
            if box.IsChecked and box.IsEnabled and combo.SelectedItem:
                symbol = dict(self.types.get(category, [])).get(combo.SelectedItem)
                if symbol is not None:
                    found.append(trt_revit.Choice(category, symbol))
        return found

    def params(self):
        """The settings of the window for create_tags; None (with a message) when something is not right."""
        spacing, offset, gap = number(self.txtSpacing.Text, 1.0), number(self.txtOffset.Text, 0.0), number(self.txtGap.Text, 0.0)
        leader, bundle_mm = number(self.txtLeader.Text, 0.0), number(self.txtBundle.Text, 0.0)
        if spacing is None or offset is None or gap is None or leader is None or bundle_mm is None:
            self.say(u'Repeat every: a number of metres (at least 1). The distances and the space around tags: mm on the sheet, 0 or more.', RED)
            return None
        style = trt_revit.STYLES[max(self.cmbStyle.SelectedIndex, 0)][0]
        choices = self.choices()
        if not choices:
            self.say(u'Tick Cable Trays or Conduits and choose a tag type for it.', RED)
            return None
        hatch_name = self.cmbHatch.SelectedItem if self.cmbHatch.SelectedIndex > 0 else None
        _save_settings({'ticked': [c.category for c in choices], 'spacing': self.txtSpacing.Text, 'offset': self.txtOffset.Text,
                        'gap': self.txtGap.Text, 'leader': self.txtLeader.Text, 'style': style,
                        'bundle': bool(self.chkBundle.IsChecked), 'bundle_mm': self.txtBundle.Text,
                        'links': bool(self.chkLinks.IsChecked), 'skip': bool(self.chkSkip.IsChecked),
                        'inside': bool(self.chkInside.IsChecked), 'hatch': bool(self.chkHatch.IsChecked),
                        'hatch_name': hatch_name, 'walls': bool(self.chkWalls.IsChecked),
                        'types': dict((category, combo.SelectedItem) for category, box, combo, label in self.rows if combo.SelectedItem)})
        return dict(choices=choices, spacing_m=spacing, offset_mm=offset, gap_mm=gap, include_links=bool(self.chkLinks.IsChecked),
                    skip_tagged=bool(self.chkSkip.IsChecked), inside_annotation=bool(self.chkInside.IsChecked), selected_ids=self._only(),
                    style=style, leader_mm=leader, bundle=bool(self.chkBundle.IsChecked), bundle_mm=bundle_mm,
                    hatch=bool(self.chkHatch.IsChecked and self.chkHatch.IsEnabled), hatch_names=[hatch_name] if hatch_name else None,
                    walls=bool(self.chkWalls.IsChecked))

    def on_create(self):
        params = self.params()
        if params is not None:
            self.say(u'Working ...')
            self.in_revit(lambda uidoc: self.run(uidoc, params))

    def on_preview(self):
        params = self.params()
        if params is not None:
            self.say(u'Working out the arrangements ...')
            self.in_revit(lambda uidoc: self.preview(uidoc, params))

    def _call(self, params, **more):
        values = dict(params)
        values.update(more)
        choices = values.pop('choices')
        return trt_revit.create_tags(self.doc, self.view, choices, **values)

    def run(self, uidoc, params, variant_key=None):
        if not self.still_this_view(uidoc):
            return
        report = self._call(params, variant_key=variant_key)
        self.show_report(report)

    def preview(self, uidoc, params):
        """Work out the arrangements (nothing kept), let the user pick one, create that one - one request to Revit."""
        if not self.still_this_view(uidoc):
            return
        options = []
        report = self._call(params, preview=options)
        if report.error:
            self.say(report.error, RED)
            return
        if not options or not any(option.placements for option in options):
            self.say(u'\n'.join(report.lines()[1:]) or u'Nothing to tag in this view.', RED)
            return
        import trt_options
        chosen = trt_options.choose(options, owner=self)
        if chosen is None:
            self.say(u'Preview closed: nothing was created.')
            return
        self.show_report(self._call(params, variant_key=chosen))

    def show_report(self, report):
        if report.error:
            self.say(report.error, RED)
            return
        problem = report.failed or report.crowded or not report.created
        self.say(u'\n'.join(report.lines()) + (u'\nUndo (Ctrl+Z) in Revit removes all of them at once.' if report.created else u''),
                 RED if problem else GREEN)

def main():
    uidoc = revit.uidoc
    for window in list(_OPEN):                             # already open: bring it to the front and read again
        try:
            if window.IsVisible:
                window.read(uidoc)
                window.Activate()
                return window
        except Exception:
            pass
    window = RunWindow(uidoc)
    _OPEN.append(window)
    window.Show()                                          # modeless
    return window

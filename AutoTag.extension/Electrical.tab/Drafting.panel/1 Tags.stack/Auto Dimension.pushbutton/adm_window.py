# -*- coding: utf-8 -*-
"""Auto Dimension - the window (modeless: it stays open while you work in Revit).

Tick the equipment and the kinds of dimension, choose the dimension style, press
"Create dimensions". "Refresh" reads the active view and the selection again.

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

from System.Windows.Media import BrushConverter

from pyrevit import forms, revit
from Autodesk.Revit.UI import ExternalEvent, IExternalEventHandler

import adm_revit

SETTINGS = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'EleTools', 'AutoDimension', 'settings.json')
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
        return 'Auto Dimension'


class DimensionWindow(forms.WPFWindow):
    def __init__(self, uidoc):
        forms.WPFWindow.__init__(self, os.path.join(os.path.dirname(__file__), 'ui.xaml'))
        self._brushes = BrushConverter()
        self.settings = _load_settings()
        self.doc = self.view = None
        self.selected = set()
        self.types = []                                    # [(name, DimensionType)]
        self._handler = _Handler(lambda text: self.say(text, RED))
        self._event = ExternalEvent.Create(self._handler)

        s = self.settings
        self.chkEquipment.IsChecked = bool(s.get('equipment', True))
        self.chkFixtures.IsChecked = bool(s.get('fixtures', True))
        self.chkChain.IsChecked = bool(s.get('chain', True))
        self.chkWalls.IsChecked = bool(s.get('walls', True))
        self.chkLinks.IsChecked = bool(s.get('links', True))
        self.chkHeights.IsChecked = bool(s.get('heights', True))
        self.chkLevel.IsChecked = bool(s.get('level', True))
        self.txtOffset.Text = u'{0}'.format(s.get('offset', u'10'))

        self.btnRefresh.Click += self._guard(lambda: self.in_revit(lambda uidoc: self.read(uidoc, True)))
        self.chkSelected.Click += self._guard(lambda: self.in_revit(self.read))
        self.chkChain.Click += self._guard(self.on_chain)
        self.btnCreate.Click += self._guard(self.on_create)
        self.btnClose.Click += self._guard(lambda: self.Close())
        self.btnDetails.Click += self._guard(self.on_details)
        self.Closed += self.on_closed
        self.on_chain()
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

    def on_details(self):
        """Open the file with the details of the last run (which references, what Revit said to each try)."""
        path = adm_revit.DETAILS_PATH
        if os.path.isfile(path):
            os.startfile(path)
        else:
            self.say(u'There are no details yet: press Create dimensions first.', RED)

    def on_chain(self):
        """Walls belong to the chain: without the chain they cannot be chosen."""
        self.chkWalls.IsEnabled = self.chkLinks.IsEnabled = bool(self.chkChain.IsChecked)

    def say(self, text, colour=DARK):
        self.txtMessages.Text = text
        self.txtMessages.Foreground = self._brushes.ConvertFromString(colour)
        background, border = BAR.get(colour, BAR[DARK])
        self.barMessages.Background = self._brushes.ConvertFromString(background)
        self.barMessages.BorderBrush = self._brushes.ConvertFromString(border)

    # ------------------------------------------------------------------
    # what is in the active view (read only) - runs inside Revit
    # ------------------------------------------------------------------
    def read(self, uidoc, fresh=False, keep_message=False):
        self.doc, self.view = uidoc.Document, uidoc.ActiveView
        try:
            self.selected = set(eid.Value for eid in uidoc.Selection.GetElementIds())
        except Exception:
            self.selected = set()
        if fresh:
            self.chkSelected.IsChecked = bool(self.selected)
        if not self.selected:
            self.chkSelected.IsChecked = False
        self.chkSelected.IsEnabled = bool(self.selected)
        self.chkSelected.Content = (u'Only the {0} element(s) selected in Revit'.format(len(self.selected)) if self.selected
                                    else u'Only selected elements (none selected)')

        # the linear dimension styles of this project; none is created
        wanted = self.cmbType.SelectedItem or self.settings.get('type')
        self.types = adm_revit.dimension_types(self.doc)
        self.cmbType.Items.Clear()
        names = [name for name, _ in self.types]
        for name in names:
            self.cmbType.Items.Add(name)
        if names:
            self.cmbType.SelectedIndex = names.index(wanted) if wanted in names else 0

        problem = adm_revit.view_problem(self.view)
        if problem:
            self.lblView.Text = u'View: {0}'.format(self.view.Name)
            self.btnCreate.IsEnabled = False
            self.say(problem + u'  Then press Refresh.', RED)
            return
        plan = adm_revit.Frame(self.view).is_plan
        found = adm_revit.counts(self.doc, self.view, self.selected if self.chkSelected.IsChecked else None)
        self.chkEquipment.Content = u'Electrical Equipment   ({0})'.format(found.get('OST_ElectricalEquipment', 0))
        self.chkFixtures.Content = u'Electrical Fixtures   ({0})   -   dimensioned from their centre'.format(found.get('OST_ElectricalFixtures', 0))
        self.chkHeights.IsEnabled = self.chkLevel.IsEnabled = not plan
        self.lblView.Text = u'View: {0}   |   {1}   |   scale 1 : {2}'.format(
            self.view.Name, u'plan' if plan else u'section / elevation', self.view.Scale)
        self.btnCreate.IsEnabled = bool(sum(found.values())) and bool(names)
        if keep_message:
            return
        if not names:
            self.say(u'This project has no linear dimension style.', RED)
        elif not sum(found.values()):
            self.say(u'This view shows no electrical equipment or fixtures. Open the layout view and press Refresh.', RED)
        else:
            self.say(u'Press Create dimensions. Each row of equipment gets one chain: wall, equipment, gap, equipment ... wall.'
                     + (u'' if plan else u' In this section the heights are dimensioned as well.')
                     + u' This window can stay open: after changing the view or the selection in Revit, press Refresh.')

    # ------------------------------------------------------------------
    # create: the only place where the model is changed - runs inside Revit
    # ------------------------------------------------------------------
    def on_create(self):
        offset = number(self.txtOffset.Text, 0.0)
        if offset is None:
            self.say(u'The distance must be a number in mm, e.g. 10.', RED)
            return
        categories = [name for name, box in (('OST_ElectricalEquipment', self.chkEquipment), ('OST_ElectricalFixtures', self.chkFixtures))
                      if box.IsChecked]
        if not categories:
            self.say(u'Tick Electrical Equipment, Electrical Fixtures or both.', RED)
            return
        if not (self.chkChain.IsChecked or self.chkHeights.IsChecked or self.chkLevel.IsChecked):
            self.say(u'Tick at least one kind of dimension.', RED)
            return
        if self.cmbType.SelectedIndex < 0:
            self.say(u'Choose a dimension style.', RED)
            return
        self.settings.update({
            'equipment': bool(self.chkEquipment.IsChecked), 'fixtures': bool(self.chkFixtures.IsChecked),
            'chain': bool(self.chkChain.IsChecked), 'walls': bool(self.chkWalls.IsChecked), 'links': bool(self.chkLinks.IsChecked),
            'heights': bool(self.chkHeights.IsChecked), 'level': bool(self.chkLevel.IsChecked),
            'offset': self.txtOffset.Text, 'type': self.cmbType.SelectedItem,
        })
        _save_settings(self.settings)
        self.say(u'Creating dimensions ...')
        self.in_revit(lambda uidoc: self.create(uidoc, categories, offset))

    def create(self, uidoc, categories, offset):
        if self.view is None or uidoc.ActiveView.Id != self.view.Id or uidoc.Document.Title != self.doc.Title:
            self.read(uidoc, True)                         # what is on screen belongs to another view
            self.say(u'The active view has changed, so it was read again. Check the choices and press Create dimensions once more.', RED)
            return
        options = adm_revit.Options(bool(self.chkChain.IsChecked), bool(self.chkWalls.IsChecked), bool(self.chkHeights.IsChecked),
                                    bool(self.chkLevel.IsChecked), bool(self.chkLinks.IsChecked), offset)
        dim_type = self.types[self.cmbType.SelectedIndex][1]
        report = adm_revit.create(self.doc, self.view, categories, dim_type, options,
                                  self.selected if self.chkSelected.IsChecked else None)
        if report.error:
            self.say(report.error, RED)
            return
        problem = report.failed or report.no_reference or report.wall_refused or report.chain_broken or not report.dimensions
        self.say(u'\n'.join(report.lines()) + (u'\nUndo (Ctrl+Z) in Revit removes all of them at once.' if report.dimensions else u''),
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
    window = DimensionWindow(uidoc)
    _OPEN.append(window)
    window.show()                                          # modeless
    return window

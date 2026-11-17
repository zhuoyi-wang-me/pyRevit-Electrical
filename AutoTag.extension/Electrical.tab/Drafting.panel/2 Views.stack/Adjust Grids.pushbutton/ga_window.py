# -*- coding: utf-8 -*-
"""Adjust Grids - the window (modeless: it stays open while you work in Revit).

Left: every view of the model that can show grids, with a tick each (search, type, "only on sheets").
Right: where the bubbles go and how far outside the crop the grids end; and whether the grids of
linked models are turned off. "Apply to ticked views" does both in ONE transaction (one Undo).
Nothing is changed in the model before that button (or "Turn linked grids on again").

Because the window does not block Revit, Revit may only be touched through an ExternalEvent:
every button that reads or changes the model hands its work to Revit.
"""
import json
import os
import traceback

import clr
# the WPF libraries must be referenced before "System.Windows" can be imported
for _name in ('PresentationFramework', 'PresentationCore', 'WindowsBase', 'System.Xaml'):
    clr.AddReference(_name)

from System.Windows import GridLength, GridUnitType, Thickness, VerticalAlignment, Visibility, TextTrimming
from System.Windows.Controls import CheckBox, ColumnDefinition, Grid, TextBlock
from System.Windows.Media import BrushConverter

from pyrevit import forms, revit
from Autodesk.Revit.UI import ExternalEvent, IExternalEventHandler

from Autodesk.Revit import DB
try:
    from Autodesk.Revit.UI.Selection import ISelectionFilter
except Exception:                                          # outside Revit (tests)
    ISelectionFilter = object

import ga_geometry
import ga_revit

SETTINGS = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'EleTools', 'GridAdjust', 'settings.json')
RED, DARK, GREEN = '#C42B1C', '#1B1B1B', '#0F7B0F'
BAR = {RED: ('#FDE7E9', '#F3C6CA'), GREEN: ('#DFF6DD', '#BFE5BC'), DARK: ('#F9F9F9', '#E5E5E5')}
ALL_KINDS = u'All view types'
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


def number(text):
    """Number typed by the user ('10', '2,5'); None when it is not a number of 0 or more."""
    try:
        value = float((text or u'').strip().replace(u',', u'.'))
    except ValueError:
        return None
    return value if value >= 0 else None


def _same(doc_a, doc_b):
    if doc_a is None or doc_b is None:
        return False
    try:
        return bool(doc_a.Equals(doc_b))
    except Exception:
        return doc_a is doc_b


class _Handler(IExternalEventHandler):
    """Runs the queued work when Revit is ready for it."""

    def __init__(self, on_error):
        self.queue = []
        self.on_error = on_error

    def Execute(self, uiapp):
        pending, self.queue = self.queue, []
        uidoc = uiapp.ActiveUIDocument
        if uidoc is None:                                  # FIX: no model open in Revit - say so instead of a traceback
            try:
                self.on_error(u'No model is open in Revit - open one, then press the button again.')
            except Exception:
                pass
            return
        for action in pending:
            try:
                action(uidoc)
            except Exception:
                try:
                    self.on_error(traceback.format_exc())
                except Exception:
                    pass

    def GetName(self):
        return 'Adjust Grids'


class _LineOrGrid(ISelectionFilter):
    """Only detail lines and grids can be picked."""

    def AllowElement(self, element):
        return ga_revit.is_detail_line(element) or isinstance(element, DB.Grid)

    def AllowReference(self, reference, point):
        return False


class GridWindow(forms.WPFWindow):
    def __init__(self, uidoc):
        forms.WPFWindow.__init__(self, os.path.join(os.path.dirname(__file__), 'ui.xaml'))
        self._brushes = BrushConverter()
        self.doc = None
        self.rows = []                                     # (ViewRow, CheckBox, the row's Grid)
        self.ticked = set()                                # ids (numbers) of the ticked views
        self._handler = _Handler(lambda text: self.say(text, RED))
        self._event = ExternalEvent.Create(self._handler)

        settings = _load_settings()
        sides = settings.get('sides', [ga_geometry.LEFT, ga_geometry.BOTTOM])
        self.boxes = {ga_geometry.LEFT: self.chkLeft, ga_geometry.RIGHT: self.chkRight,
                      ga_geometry.TOP: self.chkTop, ga_geometry.BOTTOM: self.chkBottom}
        for side, box in self.boxes.items():
            box.IsChecked = side in sides
        self.chkAdjust.IsChecked = bool(settings.get('adjust', True))
        self.chkBubbles.IsChecked = bool(settings.get('bubbles', True))
        self.chkLengthen.IsChecked = bool(settings.get('align_all', True))      # line up every end (the user's wish: no ends inside, others outside)
        self.chkFromModel.IsChecked = bool(settings.get('from_model', True))
        self.chkToGrids.IsChecked = bool(settings.get('to_grids', False))
        self.chkHideLinked.IsChecked = bool(settings.get('hide_linked', False))
        self.chkOwnType.IsChecked = bool(settings.get('own_type', True))
        self.chkOnSheets.IsChecked = bool(settings.get('on_sheets', False))
        self.chkLineBubble.IsChecked = bool(settings.get('line_bubble', True))
        self.chkLineTicked.IsChecked = bool(settings.get('line_ticked', False))
        self.chkLineDelete.IsChecked = bool(settings.get('line_delete', False))
        self.chkLineMulti.IsChecked = bool(settings.get('line_multi', False))
        self.txtBubble.Text = u'{0}'.format(settings.get('bubble_mm', u'10'))
        self.txtPlain.Text = u'{0}'.format(settings.get('plain_mm', u'2'))
        self.cmbKind.Items.Add(ALL_KINDS)
        for kind, label in ga_revit.KIND_LABELS:
            self.cmbKind.Items.Add(label)
        self.cmbKind.SelectedIndex = 0

        self.chkAdjust.Click += self._guard(self.on_switches)
        self.chkBubbles.Click += self._guard(self.on_switches)
        self.chkHideLinked.Click += self._guard(self.on_switches)
        self.txtSearch.TextChanged += self._guard(self.show_rows)
        self.cmbKind.SelectionChanged += self._guard(self.show_rows)
        self.chkOnSheets.Click += self._guard(self.show_rows)
        self.btnAll.Click += self._guard(lambda: self.tick_shown(True))
        self.btnNone.Click += self._guard(self.untick_all)
        self.btnPick.Click += self._guard(lambda: self.in_revit(self.pick))
        self.btnRefresh.Click += self._guard(lambda: self.in_revit(self.read))
        self.btnApply.Click += self._guard(self.on_apply)
        self.btnShowLinked.Click += self._guard(self.on_show_linked)
        self.btnAlignLines.Click += self._guard(self.on_align_lines)
        self.btnClose.Click += self._guard(lambda: self.Close())
        self.Closed += self.on_closed
        self.on_switches()
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

    def on_switches(self):
        self.panAdjust.IsEnabled = bool(self.chkAdjust.IsChecked)
        self.chkOwnType.IsEnabled = bool(self.chkHideLinked.IsChecked)
        self.lblSides.Text = (u'Bubbles on these sides of the crop region (off on the others)' if self.chkBubbles.IsChecked
                              else u'Sides that get the longer distance (the bubbles are left as they are)')

    # ------------------------------------------------------------------
    # reading the model (inside Revit)
    # ------------------------------------------------------------------
    def read(self, uidoc):
        """List the views of the active model. The ticks are kept when it is the same model."""
        doc = uidoc.Document
        same = _same(self.doc, doc)
        self.doc = doc
        rows = ga_revit.list_views(doc)
        keys = set(row.key for row in rows)
        if same:
            self.ticked &= keys
        else:
            self.ticked = set(view.Id.Value for view in ga_revit.target_views(doc, uidoc)[0]) & keys
        self.build_rows(rows)
        links = ga_revit.link_count(doc)
        self.lblLinks.Text = (u'{0} linked model(s) in this model. A view filter "{1}" hides their grids; this model\'s grids stay.'
                              .format(links, ga_revit.LINK_FILTER) if links else u'This model has no linked models.')
        title = getattr(doc, 'Title', u'') or u''
        self.lblScope.Text = u'{0}   |   {1} view(s) that can show grids. Tick the views to work on.'.format(title, len(rows))
        self.say(u'The grids drawn in the crop region of the ticked views get their ends on a frame around it, so all ends on one '
                 u'side stand on one line. Grids hidden from the view stay hidden. The window can stay open while you work in Revit.')

    def pick(self, uidoc):
        """Tick the active view / the views of the active sheet / of the sheets and views selected in the Project Browser."""
        if not _same(self.doc, uidoc.Document):
            self.read(uidoc)
        views, how = ga_revit.target_views(self.doc, uidoc)
        keys = set(row.key for row, box, line in self.rows)
        found = [view.Id.Value for view in views if view.Id.Value in keys]
        self.ticked |= set(found)
        for row, box, line in self.rows:
            box.IsChecked = row.key in self.ticked
        self.count()
        self.say(u'Ticked {0} view(s): {1}.'.format(len(found), how) if found else u'Nothing to tick: ' + how + u'.',
                 DARK if found else RED)

    # ------------------------------------------------------------------
    # the view list (window only)
    # ------------------------------------------------------------------
    def build_rows(self, rows):
        self.lstViews.Children.Clear()
        self.rows = []
        for row in rows:
            line = Grid()
            line.Margin = Thickness(0, 1, 0, 1)
            for width in (GridLength(1, GridUnitType.Star), GridLength(110), GridLength(90)):
                column = ColumnDefinition()
                column.Width = width
                line.ColumnDefinitions.Add(column)
            box = CheckBox()
            box.Content = row.name
            box.ToolTip = row.name + (u'   (sheet {0})'.format(row.sheet) if row.sheet else u'   (on no sheet)')
            box.IsChecked = row.key in self.ticked
            box.VerticalAlignment = VerticalAlignment.Center
            box.Click += self._ticker(row, box)
            line.Children.Add(box)
            for column, text in ((1, row.kind), (2, row.sheet or u'-')):
                label = TextBlock()
                label.Text = text
                label.Foreground = self._brushes.ConvertFromString('#5F5F5F')
                label.VerticalAlignment = VerticalAlignment.Center
                label.TextTrimming = TextTrimming.CharacterEllipsis
                Grid.SetColumn(label, column)
                line.Children.Add(label)
            self.lstViews.Children.Add(line)
            self.rows.append((row, box, line))
        self.show_rows()

    def _ticker(self, row, box):
        def handler(sender, args):
            if box.IsChecked:
                self.ticked.add(row.key)
            else:
                self.ticked.discard(row.key)
            self.count()
        return handler

    def shown(self, row):
        words = (self.txtSearch.Text or u'').strip().lower()
        kind = self.cmbKind.SelectedItem
        if kind and kind != ALL_KINDS and row.kind != kind:
            return False
        if self.chkOnSheets.IsChecked and not row.sheet:
            return False
        return not words or words in row.name.lower() or words in row.sheet.lower()

    def show_rows(self):
        for row, box, line in self.rows:
            line.Visibility = Visibility.Visible if self.shown(row) else Visibility.Collapsed
        self.count()

    def tick_shown(self, state):
        for row, box, line in self.rows:
            if self.shown(row):
                box.IsChecked = state
                if state:
                    self.ticked.add(row.key)
                else:
                    self.ticked.discard(row.key)
        self.count()

    def untick_all(self):
        self.ticked = set()
        for row, box, line in self.rows:
            box.IsChecked = False
        self.count()

    def count(self):
        shown = sum(1 for row, box, line in self.rows if line.Visibility == Visibility.Visible)
        self.lblCount.Text = u'{0} ticked   |   {1} of {2} shown'.format(len(self.ticked), shown, len(self.rows))

    # ------------------------------------------------------------------
    # changing the model
    # ------------------------------------------------------------------
    def options(self):
        """The Options from the window; None (with a message) when something is not right."""
        bubble, plain = number(self.txtBubble.Text), number(self.txtPlain.Text)
        adjust, hide = bool(self.chkAdjust.IsChecked), bool(self.chkHideLinked.IsChecked)
        if adjust and (bubble is None or plain is None):
            self.say(u'The two distances must be numbers in mm, 0 or more - e.g. 10 and 2.', RED)
            return None
        sides = [side for side in ga_geometry.SIDES if self.boxes[side].IsChecked]
        if adjust and self.chkBubbles.IsChecked and not sides:
            self.say(u'Tick at least one side for the bubbles, or untick "Switch the bubbles".', RED)
            return None
        if not adjust and not hide:
            self.say(u'Tick "Adjust the grids of this model", "Turn off the grids of linked models" or both.', RED)
            return None
        if not self.ticked:
            self.say(u'Tick at least one view in the list (or press "Active / selected").', RED)
            return None
        values = _load_settings()
        values.update({'sides': sides, 'bubbles': bool(self.chkBubbles.IsChecked), 'align_all': bool(self.chkLengthen.IsChecked), 'from_model': bool(self.chkFromModel.IsChecked),
                       'bubble_mm': self.txtBubble.Text, 'plain_mm': self.txtPlain.Text, 'adjust': adjust, 'hide_linked': hide,
                       'own_type': bool(self.chkOwnType.IsChecked), 'on_sheets': bool(self.chkOnSheets.IsChecked),
                       'to_grids': bool(self.chkToGrids.IsChecked)})
        _save_settings(values)
        return ga_revit.Options(sides, bubble or 0.0, plain or 0.0, bool(self.chkBubbles.IsChecked), bool(self.chkLengthen.IsChecked),
                                adjust=adjust, linked='hide' if hide else None, own_type=bool(self.chkOwnType.IsChecked),
                                from_model=bool(self.chkFromModel.IsChecked), to_grids=bool(self.chkToGrids.IsChecked))

    def on_apply(self):
        options = self.options()
        if options is not None:
            self.say(u'Working ...')
            self.in_revit(lambda uidoc: self.run(uidoc, options))

    def on_show_linked(self):
        if not self.ticked:
            self.say(u'Tick the views in which the linked grids should be shown again.', RED)
            return
        self.in_revit(lambda uidoc: self.run(uidoc, ga_revit.Options(adjust=False, linked='show')))

    # ------------------------------------------------------------------
    # align the grid ends to detail lines
    # ------------------------------------------------------------------
    def on_align_lines(self):
        values = _load_settings()
        values.update({'line_bubble': bool(self.chkLineBubble.IsChecked), 'line_ticked': bool(self.chkLineTicked.IsChecked),
                       'line_delete': bool(self.chkLineDelete.IsChecked), 'line_multi': bool(self.chkLineMulti.IsChecked)})
        _save_settings(values)
        self.say(u'Pick the detail line(s) in the view - and, if only some grids should move, those grids too - then press '
                 u'Finish in the options bar (Esc cancels).')
        self.in_revit(self.align_lines)

    def align_lines(self, uidoc):
        """The detail lines selected in Revit (or picked now), and the grids selected with them -> align_to_lines."""
        if not _same(self.doc, uidoc.Document):
            self.read(uidoc)
        doc = self.doc
        chosen = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
        lines = [e for e in chosen if e is not None and ga_revit.is_detail_line(e)]
        grids = [e for e in chosen if e is not None and isinstance(e, DB.Grid)]
        if not lines:
            from Autodesk.Revit.UI.Selection import ObjectType
            try:
                picked = uidoc.Selection.PickObjects(ObjectType.Element, _LineOrGrid(),
                                                     u'Pick the detail line(s) - and the grids, if only some should move - then Finish')
            except Exception:
                self.say(u'Picking cancelled - nothing was changed.')
                return
            chosen = [doc.GetElement(r.ElementId) for r in picked]
            lines = [e for e in chosen if e is not None and ga_revit.is_detail_line(e)]
            grids = [e for e in chosen if e is not None and isinstance(e, DB.Grid)]
        if not lines:
            self.say(u'No detail line picked. Draw a detail line where the grid ends should be, then press the button again.', RED)
            return
        view = doc.GetElement(lines[0].OwnerViewId) or uidoc.ActiveView
        here = [line for line in lines if line.OwnerViewId.Value == view.Id.Value]
        others = []
        if self.chkLineTicked.IsChecked:
            for row, box, line in self.rows:
                if row.key in self.ticked:
                    other = doc.GetElement(row.view_id)
                    if other is not None:
                        others.append(other)
        report = ga_revit.align_to_lines(doc, view, here, only_grids=set(g.Id.Value for g in grids) or None, views=others,
                                         bubble=bool(self.chkLineBubble.IsChecked), delete=bool(self.chkLineDelete.IsChecked),
                                         multi=bool(self.chkLineMulti.IsChecked))
        text = report.text()
        if len(here) < len(lines):
            text += u'\n{0} line(s) drawn in another view were not used (the lines of one view at a time).'.format(len(lines) - len(here))
        if report.grids:
            text += u'\nUndo (Ctrl+Z) in Revit puts all of it back at once.'
        self.say(text, RED if (report.error or not report.grids or report.skipped) else GREEN)

    def run(self, uidoc, options):
        if not _same(self.doc, uidoc.Document):
            self.read(uidoc)
            self.say(u'Another model is active now: the list was read again for it. Check the ticks and press the button again.', RED)
            return
        views = []
        for row, box, line in self.rows:
            if row.key in self.ticked:
                view = self.doc.GetElement(row.view_id)
                if view is not None:
                    views.append(view)
        if not views:
            self.say(u'The ticked views are no longer in the model - press Refresh.', RED)
            return
        report = ga_revit.adjust(self.doc, views, options)
        if report.error:
            self.say(report.error, RED)
            return
        changed = report.grids or report.link_views or report.retyped
        problem = (report.skipped or report.bubble_trouble or report.clash or report.by_link_view or report.link_problem
                   or (report.adjusted and not report.grids) or (report.linked and not report.link_views))
        self.say(u'\n'.join(report.lines()) + (u'\nUndo (Ctrl+Z) in Revit puts all of it back at once.' if changed else u''),
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
    window = GridWindow(uidoc)
    _OPEN.append(window)
    window.Show()                                          # modeless
    return window

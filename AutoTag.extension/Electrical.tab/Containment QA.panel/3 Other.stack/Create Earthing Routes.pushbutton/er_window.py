# -*- coding: utf-8 -*-
"""Earthing Route Generator - the window and the order of the steps.

    1. Select Earth Bar / Select Panels        er_revit.pick_...
    2. Preview Routes                          er_geometry.plan_route (A*)
                                               -> temporary magenta detail lines
    3. Clear Preview / Create Routes           detail lines in the chosen style

The window is modeless, so the plan can be panned and zoomed while a preview
is on screen. Revit may only be touched from an ExternalEvent; every button
therefore queues its work through RevitRunner.

This is a MODULE (imported by script.py): pyRevit empties the global names of
a button script when it ends, which would break every button of a window that
is still open.

WHAT THIS TOOL WRITES TO THE MODEL: detail lines in the active floor plan,
nothing else. Preview lines are real detail lines too (Revit has no other way
to show a line in a view), drawn in magenta and deleted again by Clear
Preview, Create Routes, Cancel, or the next run of the tool.
"""
import math
import os
import time

from pyrevit import forms, revit, script

from Autodesk.Revit.UI import ExternalEvent, IExternalEventHandler

import er_config as config
import er_geometry as geometry
import er_revit as rv

XAML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui.xaml')
_OPEN = []                 # keeps the open window (and its ExternalEvent) alive
FT = config.FEET_PER_MM
# what to do where lines take the same way: label in the window -> mode
SHARE_MODES = [('Side by side, with an offset', 'offset'),
               ('One line with tees', 'join'),
               ('On top of each other', 'overlap')]


LOG_PATH = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'EleTools',
                        'EarthingRoute', 'last_run.log')


def trace(text, restart=False):
    """One line in last_run.log, written and closed at once. If Revit ever
    stops in the middle of a run, the last line says how far it got."""
    try:
        folder = os.path.dirname(LOG_PATH)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with open(LOG_PATH, 'w' if restart else 'a') as log:
            log.write('{0}  {1}\n'.format(time.strftime('%H:%M:%S'), text))
    except Exception:
        pass


rv.trace = trace          # er_revit writes its steps to the same log

# NOTE: there is deliberately NO "repaint the window" helper here. The first
# version pumped Windows messages (Dispatcher.PushFrame) between routes to
# refresh the status text. Inside an ExternalEvent that lets Revit re-enter
# its own idle processing while the API is in use, and Revit closed without a
# message the moment Preview was clicked. Work done inside Revit must simply
# run to its end.


class _Handler(IExternalEventHandler):
    def __init__(self):
        self.queue = []
        self.on_error = None

    def Execute(self, uiapp):
        pending, self.queue = self.queue, []
        for action in pending:
            try:
                action(uiapp)
            except Exception as error:
                if self.on_error is not None:
                    try:
                        self.on_error(error)
                    except Exception:
                        pass

    def GetName(self):
        return config.TITLE


class RevitRunner(object):
    """runner.run(function) -> function(uiapp) executed by Revit when idle."""

    def __init__(self, on_error=None):
        self._handler = _Handler()
        self._handler.on_error = on_error
        self._event = ExternalEvent.Create(self._handler)

    def run(self, action):
        self._handler.queue.append(action)
        self._event.Raise()

    def dispose(self):
        try:
            self._event.Dispose()
        except Exception:
            pass


class Row(object):
    """One line of the result table."""

    def __init__(self, panel, status, kind, length='', bends='', note=''):
        self.Panel, self.Status, self.Kind = panel, status, kind
        self.Length, self.Bends, self.Note = length, bends, note


def to_number(text, name, minimum, maximum):
    try:
        value = float(str(text).strip().replace(',', '.'))
    except Exception:
        raise ValueError(u'{0}: "{1}" is not a number.'.format(name, text))
    if value < minimum or value > maximum:
        raise ValueError(u'{0} must be between {1:g} and {2:g} mm.'.format(name, minimum, maximum))
    return value


# What the tool draws. Each task names the two things that are picked:
#   'panels'  panels               -> one earth bar
#   'pit'     earth bars           -> one earth pit, or a point clicked outside the room
#   'loop'    earth bars           -> each other, in a ring (nothing else to pick)
# (label in the list, task, heading + button of the target, heading + button
#  of the sources, the word for one source)
TASKS = [
    ('Panels to an earth bar', 'panels',
     '1   EARTH BAR', 'Select Earth Bar', '2   PANELS', 'Select Panels', 'panel'),
    ('Earth bars to an earth pit / out of the room', 'pit',
     '1   EARTH PIT / EXIT POINT', 'Select Earth Pit', '2   EARTH BARS', 'Select Earth Bars', 'earth bar'),
    ('Earth bar to earth bar loop', 'loop',
     '', '', '1   EARTH BARS TO LOOP', 'Select Earth Bars', 'earth bar'),
]
ARROW = u' → '


class EarthingWindow(forms.WPFWindow):
    def __init__(self, uidoc):
        forms.WPFWindow.__init__(self, XAML_PATH)
        self.doc = uidoc.Document
        self.view_id = rv.id_value(uidoc.ActiveView.Id)
        self.view_name = uidoc.ActiveView.Name
        self.task = 'panels'
        self.bar = None                    # the target: {'id', 'label'} (+ 'point' for a clicked exit point)
        self.panels = []                   # the sources: [{'id', 'label'}]
        self.legs = []                     # loop: [{'id': from, 'to': to, 'label': 'A -> B'}]
        self.routes = {}                   # panel id -> geometry.Route
        self.routes_key = None             # the settings those routes were made with
        self.frame = None
        self.preview_ids = []
        self.obstacle_boxes = []           # [(CheckBox, [category names])]
        self.cfg = script.get_config()
        self.runner = RevitRunner(on_error=self._revit_error)
        self._closing = False
        self._init_controls(uidoc)
        self._wire()
        self._show_selection()

    # ------------------------------------------------------------------ setup
    def _init_controls(self, uidoc):
        from System.Windows import Thickness
        from System.Windows.Controls import CheckBox, TextBlock
        self.view_tb.Text = u'Floor plan:  {0}'.format(self.view_name)
        def get(name, default):
            # pyRevit's get_option RAISES when the setting has never been saved
            # and the default is None, so no call may depend on its default
            try:
                value = self.cfg.get_option(name, default if default is not None else '')
            except Exception:
                return default
            return default if (default is None and value == '') else value
        # line styles
        names = rv.line_style_names(self.doc)
        for name in names:
            self.style_cb.Items.Add(name)
        saved = get('line_style', '')
        hinted = [n for n in names if [h for h in config.LINE_STYLE_HINTS if h in n.lower()]]
        self.style_cb.SelectedItem = saved if saved in names else (hinted[0] if hinted else None)
        # numbers
        self.clearance_tb.Text = '{0:g}'.format(get('clearance_mm', config.CLEARANCE_MM))
        for value in config.GRID_CHOICES_MM:
            self.grid_cb.Items.Add('{0:g}'.format(value))
        self.grid_cb.Text = '{0:g}'.format(get('grid_mm', config.GRID_MM))
        # obstacle categories
        ticked = get('obstacles', None)
        hosts = {'Structural': self.obs_structural, 'Mechanical': self.obs_mechanical,
                 'Electrical': self.obs_electrical}
        for label, group, categories, default in config.OBSTACLES:
            box = CheckBox()
            text = TextBlock()
            text.Text = label
            box.Content = text
            box.Tag = label
            box.Margin = Thickness(0, 2, 0, 2)
            box.IsChecked = (label in ticked) if isinstance(ticked, list) else default
            hosts[group].Children.Add(box)
            self.obstacle_boxes.append((box, categories))
        self.chk_links.IsChecked = bool(get('include_links', config.INCLUDE_LINKS))
        self.chk_doors.IsChecked = bool(get('through_doors', config.ROUTE_THROUGH_DOORS))
        self.chk_edge.IsChecked = bool(get('connect_at_edge', config.CONNECT_AT_EDGE))
        self.chk_entry.IsChecked = bool(get('bar_entry', config.BAR_ENTRY_SQUARE))
        for label, _mode in SHARE_MODES:
            self.share_cb.Items.Add(label)
        modes = [mode for _label, mode in SHARE_MODES]
        saved_mode = get('shared_runs', config.SHARED_RUNS)
        self.share_cb.SelectedIndex = modes.index(saved_mode) if saved_mode in modes else 0
        self.offset_tb.Text = '{0:g}'.format(get('offset_mm', config.PARALLEL_OFFSET_MM))
        try:
            self.bend_tb.Text = '{0:g}'.format(float(get('bend_mm', config.BEND_RADIUS_MM)))
        except Exception:
            self.bend_tb.Text = '{0:g}'.format(config.BEND_RADIUS_MM)
        self.share_cb.SelectionChanged += self._share_changed
        self._share_changed()
        self.chk_breaks.IsChecked = bool(get('break_at_crossings', config.BREAK_AT_CROSSINGS))
        self.chk_square.IsChecked = bool(get('square_to_bar', True))
        self.chk_room.IsChecked = bool(get('inside_room', True))
        self.chk_close.IsChecked = bool(get('close_loop', config.CLOSE_LOOP))
        for entry in TASKS:
            self.task_cb.Items.Add(entry[0])
        self.task_cb.SelectedIndex = 0
        self._apply_task()
        self.task_cb.SelectionChanged += self._task_changed
        # panels already selected in Revit when the button was clicked
        try:
            for element_id in uidoc.Selection.GetElementIds():
                element = self.doc.GetElement(element_id)
                if element is not None and rv.is_panel(element):
                    self.panels.append({'id': rv.id_value(element.Id), 'label': rv.element_label(element)})
        except Exception:
            pass

    def _wire(self):
        self.btn_bar.Click += self._select_bar
        self.btn_point.Click += self._pick_exit_point
        self.btn_panels.Click += self._select_panels
        self.btn_all.Click += lambda sender, args: self._tick_all(True)
        self.btn_none.Click += lambda sender, args: self._tick_all(False)
        self.btn_preview.Click += self._preview
        self.btn_clear.Click += self._clear_preview
        self.btn_create.Click += self._create
        self.btn_cancel.Click += lambda sender, args: self.Close()
        self.Closing += self._on_closing

    # ------------------------------------------------------------------ task
    def _task_entry(self):
        return TASKS[max(0, self.task_cb.SelectedIndex)]

    def _apply_task(self):
        """Labels and buttons for the chosen task."""
        from System.Windows import Visibility
        _label, task, bar_head, bar_button, panels_head, panels_button, _word = self._task_entry()
        self.task = task
        show = lambda on: Visibility.Visible if on else Visibility.Collapsed
        self.bar_card.Visibility = show(task != 'loop')
        self.bar_head.Text = bar_head
        self.btn_bar.Content = bar_button
        self.btn_point.Visibility = show(task == 'pit')
        self.panels_head.Text = panels_head
        self.btn_panels.Content = panels_button
        self.chk_close.Visibility = show(task == 'loop')


    def _task_changed(self, sender, args):
        """Another task: what was picked for the old one does not apply."""
        self._apply_task()
        self.bar, self.panels, self.legs, self.routes = None, [], [], {}
        self.grid.ItemsSource = None
        self._show_selection()

    def _items(self):
        """What is routed: the picked panels / earth bars, or the legs of the loop."""
        return self.legs if self.task == 'loop' else self.panels

    def _target_id(self, item):
        """The id of the element a route ends on (0 = a clicked exit point)."""
        return item['to'] if 'to' in item else self.bar['id']

    def _share_mode(self):
        return SHARE_MODES[max(0, self.share_cb.SelectedIndex)][1]

    def _share_changed(self, sender=None, args=None):
        self.offset_tb.IsEnabled = self._share_mode() == 'offset'

    def _tick_all(self, state):
        for box, _categories in self.obstacle_boxes:
            box.IsChecked = state

    # ------------------------------------------------------------------ helpers
    def _status(self, text):
        self.status_tb.Text = text

    def _revit_error(self, error):
        try:
            self.Show()
        except Exception:
            pass
        self._status(u'Revit could not do that: {0}'.format(error))

    def _show_selection(self):
        word = self._task_entry()[6]
        self.bar_tb.Text = self.bar['label'] if self.bar else 'Not selected'
        if not self.panels:
            self.panels_tb.Text = 'None selected'
        else:
            names = [panel['label'] for panel in self.panels]
            shown = u', '.join(names[:12]) + (u' ...' if len(names) > 12 else u'')
            self.panels_tb.Text = u'{0} {1}(s):  {2}'.format(len(names), word, shown)
        if self.task == 'loop':
            ready = len(self.panels) >= 2
        else:
            ready = bool(self.bar and self.panels)
        self.btn_preview.IsEnabled = ready
        self.btn_create.IsEnabled = ready
        if ready:
            self._status('Click "Preview Routes".')
        elif self.task == 'loop':
            self._status('Select the earth bars to connect to each other (at least two).')
        elif self.task == 'pit':
            self._status('Step 1: select the earth pit, or click an exit point outside the room.'
                         if not self.bar else 'Step 2: select the earth bars.')
        else:
            self._status('Step 1: select the earth bar.' if not self.bar
                         else 'Step 2: select the electrical panels.')

    def _in_plan(self, action, hide=False):
        """Run action(uidoc) in Revit, but only while the floor plan this
        window was opened for is the active view."""
        def run(uiapp):
            uidoc = uiapp.ActiveUIDocument
            try:
                same = (uidoc is not None and uidoc.Document.Equals(self.doc)
                        and rv.id_value(uidoc.ActiveView.Id) == self.view_id)
            except Exception:
                same = False
            if not same:
                self._status(u'Make the floor plan "{0}" the active view again, then repeat.'.format(
                    self.view_name))
                return
            if hide:
                self.Hide()
            try:
                message = action(uidoc)
            finally:
                if hide and not self._closing:
                    self.Show()
                    self.Activate()
            if message:
                self._status(message)
        self.runner.run(run)

    # ------------------------------------------------------------------ steps 1-2: picking
    def _select_bar(self, sender, args):
        """The target: the earth bar (panels task) or the earth pit (pit task)."""
        pit = self.task == 'pit'

        def action(uidoc):
            element = (rv.pick_element(uidoc, 'Select the EARTH PIT (Esc to cancel)') if pit
                       else rv.pick_earth_bar(uidoc))
            if element is None:
                return 'Not changed.'
            self.bar = {'id': rv.id_value(element.Id), 'label': rv.element_label(element)}
            self.panels = [panel for panel in self.panels if panel['id'] != self.bar['id']]
            self.routes = {}
            self._show_selection()
        self._in_plan(action, hide=True)

    def _pick_exit_point(self, sender, args):
        """Pit task: instead of a pit, a point clicked outside the room."""
        def action(uidoc):
            point = rv.pick_point(uidoc, 'Click the point where the lines leave the room (Esc to cancel)')
            if point is None:
                return 'Not changed.'
            self.bar = {'id': 0, 'point': point,
                        'label': u'Exit point ({0:.0f}, {1:.0f}) mm'.format(point.X / FT, point.Y / FT)}
            self.routes = {}
            self._show_selection()
        self._in_plan(action, hide=True)

    def _select_panels(self, sender, args):
        """The sources: panels, or - in the other two tasks - earth bars."""
        task = self.task

        def action(uidoc):
            if task == 'panels':
                elements = rv.pick_panels(uidoc)
            else:
                elements = rv.pick_elements(uidoc, 'Select the EARTH BARS, then click Finish (Esc to cancel)')
            if elements is None:
                return 'Not changed.'
            bar_id = self.bar['id'] if self.bar else None
            seen = set()
            self.panels = []
            for element in elements:
                value = rv.id_value(element.Id)
                if value != bar_id and value not in seen:
                    seen.add(value)
                    self.panels.append({'id': value, 'label': rv.element_label(element)})
            if task != 'loop':
                self.panels.sort(key=lambda panel: panel['label'])     # a loop starts at the first one picked
            self.legs, self.routes = [], {}
            self._show_selection()
        self._in_plan(action, hide=True)

    # ------------------------------------------------------------------ settings
    def _settings(self):
        """What is set in the window, checked. Raises ValueError with a message."""
        settings = {
            'clearance_mm': to_number(self.clearance_tb.Text, 'Clearance', 0, 5000),
            'grid_mm': to_number(self.grid_cb.Text, 'Grid spacing', 10, 2000),
            'style': self.style_cb.SelectedItem,
            'labels': [box.Tag for box, _c in self.obstacle_boxes if box.IsChecked],
            'categories': [name for box, categories in self.obstacle_boxes if box.IsChecked
                           for name in categories],
            'links': bool(self.chk_links.IsChecked),
            'doors': bool(self.chk_doors.IsChecked),
            'edge': bool(self.chk_edge.IsChecked),
            'entry': bool(self.chk_entry.IsChecked),
            'share': self._share_mode(),
            'offset_mm': (to_number(self.offset_tb.Text, 'Offset', 10, 5000)
                          if self._share_mode() == 'offset' else 0.0),
            'bend_mm': to_number(self.bend_tb.Text or '0', 'Bend radius', 0, 5000),
        }
        settings['join'] = settings['share'] == 'join'
        settings['task'] = self.task
        settings['close'] = bool(self.chk_close.IsChecked)
        settings['breaks'] = bool(self.chk_breaks.IsChecked)
        settings['square_to_bar'] = bool(self.chk_square.IsChecked)
        settings['in_room'] = bool(self.chk_room.IsChecked)
        if self.task == 'loop':
            if len(self.panels) < 2:
                raise ValueError('Select at least two earth bars to connect.')
        else:
            if not self.bar:
                raise ValueError('Select the earth pit or click an exit point first.' if self.task == 'pit'
                                 else 'Select the earth bar first.')
            if not self.panels:
                raise ValueError('Select at least one earth bar.' if self.task == 'pit'
                                 else 'Select at least one panel.')
        return settings

    def _remember(self, settings):
        try:
            self.cfg.clearance_mm = settings['clearance_mm']
            self.cfg.grid_mm = settings['grid_mm']
            self.cfg.line_style = settings['style'] or ''
            self.cfg.obstacles = list(settings['labels'])
            self.cfg.include_links = settings['links']
            self.cfg.through_doors = settings['doors']
            self.cfg.connect_at_edge = settings['edge']
            self.cfg.bar_entry = settings['entry']
            self.cfg.break_at_crossings = settings['breaks']
            self.cfg.bend_mm = settings['bend_mm']
            try:
                self.cfg.square_to_bar = settings['square_to_bar']
                self.cfg.inside_room = settings['in_room']
            except Exception:
                pass
            self.cfg.close_loop = settings['close']
            self.cfg.shared_runs = settings['share']
            if settings['offset_mm']:
                self.cfg.offset_mm = settings['offset_mm']
            script.save_config()
        except Exception:
            pass

    # ------------------------------------------------------------------ routing
    def _calculate(self, uidoc, settings):
        """Fill self.routes (panel id -> Route). Read-only in Revit."""
        target = self.bar or {}
        point = target.get('point')
        key = (settings['task'], target.get('id'), (point.X, point.Y) if point is not None else None,
               tuple(panel['id'] for panel in self.panels), settings['clearance_mm'],
               settings['grid_mm'], tuple(settings['categories']), settings['links'], settings['doors'],
               settings['edge'], settings['share'], settings['offset_mm'], settings['entry'],
               settings['close'], settings['breaks'], settings.get('square_to_bar', True), settings.get('in_room', True))
        if self.routes and self.routes_key == key:
            return                                   # nothing changed since the preview
        started = time.time()
        trace('calculate ({0}): {1} item(s), clearance {2:g} mm, grid {3:g} mm'.format(
            settings['task'], len(self.panels), settings['clearance_mm'], settings['grid_mm']), restart=True)
        doc, view = self.doc, uidoc.ActiveView
        angle = 0.0
        if settings.get('square_to_bar', True):
            # the routes run square to the earth bar; when the bar is square to the drawing but the panels
            # (transformers) of the room are turned together, square to them
            def element_of(item_id):
                try:
                    return doc.GetElement(rv.make_id(item_id)) if item_id else None
                except Exception:
                    return None
            bar_element = element_of(target.get('id'))
            panels = [e for e in (element_of(p['id']) for p in self.panels) if e is not None]
            try:
                trace('angles of the earth bar: {0}'.format(', '.join('{0} {1:.1f}'.format(k, math.degrees(v))
                                                                        for k, v in sorted(rv.element_angles(bar_element, view).items())) or 'none'))
                trace('angles of the panels: {0}'.format(', '.join('{0:.1f}'.format(math.degrees(rv.route_angle(e, view))) for e in panels) or 'none'))
            except Exception:
                pass
            # FIX: the earth bar's angle came first - a bar turned 33.7 degrees in a room whose transformers
            # stand at -45 laid every line out at the bar's angle: staircases round the transformers and lines
            # crossing. The panels / transformers set out with the room decide; the bar only when they do not
            # agree on one angle. A bar turned on that layout still gets its last legs square into it.
            angle = rv.common_angle(panels, view)
            if not angle and settings.get('in_room', True):
                # the bars / panels do not agree: the walls of the room they stand in decide
                try:
                    where = [rv.connection_point(e) for e in panels + ([bar_element] if bar_element is not None else [])]
                    if point is not None:
                        where.append(point)
                    angle = rv.room_angle(doc, view, where, radius=config.ROOM_ANGLE_RADIUS_MM * FT)
                    if angle:
                        trace('angle from the room walls: {0:.1f}'.format(math.degrees(angle)))
                except Exception as error:
                    trace('room angle not read: {0}'.format(error))
            angle = angle or rv.route_angle(bar_element, view)
        frame = rv.PlanFrame(view, angle)
        trace('routes laid out {0}'.format('square to the drawing' if not angle else
                                           'turned {0:.1f} degrees with the room'.format(math.degrees(angle))))
        self.frame = frame
        self.routes = {}
        self.grid_note = u''
        if settings['task'] == 'loop':
            self._calculate_loop(doc, view, frame, settings)
        else:
            self._calculate_to_target(doc, view, frame, settings)
        self._add_breaks(doc, view, frame, settings)
        self.routes_key = key
        self.took = time.time() - started
        trace('calculated in {0:.1f} s, {1} crossing(s)'.format(self.took, self.crossings))

    def _obstacles(self, doc, view, frame, settings, points):
        """The obstacles round the given (u, v) points, read ONCE per run."""
        reach = max(config.BUFFERS_MM) * FT + 2.0
        us, vs = [p[0] for p in points], [p[1] for p in points]
        region = (min(us) - reach, min(vs) - reach, max(us) + reach, max(vs) + reach)
        trace('reading obstacles: {0} categories, links {1}'.format(len(settings['categories']), settings['links']))
        obstacles = rv.collect_obstacles(doc, view, frame, settings['categories'], region,
                                         settings['links'], settings['doors'])
        self.room_names = []
        if settings.get('in_room', True):
            try:
                shapes, names = rv.room_walls(doc, view, frame, points)
            except Exception as error:
                shapes, names = [], []
                trace('room boundary not read: {0}'.format(error))
            # FIX: the boundary was added to the WALLS, and every door opening is cut out of the walls - also out
            # of the room boundary where it crosses a door: the lines left the room through its doors and ran
            # round the outside. As an obstacle that no door opens, the lines stay inside.
            obstacles.others.extend(shapes)
            self.room_names = names if shapes else []
            self.room_note = (u'' if shapes else
                              u'the earth bar and the panels stand in different rooms ({0}) - lines not kept inside one room'.format(', '.join(names))
                              if len(names) > 1 else u'no room found at the earth bar / panels - lines are not kept inside a room')
            trace('room boundary: {0} piece(s) of {1}'.format(len(shapes), ', '.join(names) or 'no room found'))
        trace('obstacles: {0} walls, {1} others, {2} doors, {3} turned boxes'.format(
            len(obstacles.walls), len(obstacles.others), len(obstacles.doors),
            len([1 for _k, s in obstacles.walls + obstacles.others if s[0] == 'box'])))
        self.obstacle_text = u', '.join(u'{0:,} in {1}'.format(count, source)
                                        for source, count in sorted(obstacles.counts.items())) or 'none'
        return obstacles

    def _room_leg(self, doc, view, settings, element, toward, frame):
        """The first stretch of a line out of an earth bar that stands in a room TURNED against the layout of
        the run (a TX room at 45 degrees, the corridor and the pit square to the drawing): routed square to
        the room's own walls, from the bar out through the room's best door to a point just outside it.
        The rest of the line is then routed from there in the run's layout.
        FIX: one layout for the whole run made the part inside the turned room a staircase.
        Returns {'points': [(u, v) in `frame`, from the bar to outside the door], 'out': (u, v)} or None
        (bar in a room square to the run, the other end in the same room, no door, no way out)."""
        if not settings.get('in_room', True) or not settings.get('square_to_bar', True) or element is None:
            return None
        here = rv.connection_point(element)
        angle = rv.room_angle(doc, view, [here], radius=config.ROOM_ANGLE_RADIUS_MM * FT)
        if abs(rv._fold(angle - getattr(frame, 'angle', 0.0))) < math.radians(1.0):
            return None                              # the room is square to the run already
        local = rv.PlanFrame(view, angle)
        shapes, names = rv.room_walls(doc, view, local, [local.uv(here)])
        segments = [shape for _key, shape in shapes]
        if not segments:
            return None
        here_uv, toward_uv = local.uv(here), local.uv(toward)

        def inside(p):
            hit = False
            for s in segments:
                a, b = (s[1], s[2]), (s[3], s[4])
                if (a[1] > p[1]) != (b[1] > p[1]) and p[0] < a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1]):
                    hit = not hit
            return hit
        if inside(toward_uv):
            return None                              # the other end is in the same room
        us = [s[1] for s in segments] + [s[3] for s in segments]
        vs = [s[2] for s in segments] + [s[4] for s in segments]
        region = (min(us) - 3.0, min(vs) - 3.0, max(us) + 3.0, max(vs) + 3.0)
        found = rv.collect_obstacles(doc, view, local, settings['categories'], region, settings['links'], True)
        clearance = settings['clearance_mm'] * FT
        step = settings['grid_mm'] * FT

        def distance_to_boundary(p):
            best = 1e18
            for s in segments:
                du, dv = s[3] - s[1], s[4] - s[2]
                length2 = du * du + dv * dv
                t = 0.0 if length2 < 1e-12 else max(0.0, min(1.0, ((p[0] - s[1]) * du + (p[1] - s[2]) * dv) / length2))
                best = min(best, ((p[0] - s[1] - t * du) ** 2 + (p[1] - s[2] - t * dv) ** 2) ** 0.5)
            return best
        best = None
        for cu, cv, du, dv, half_width, half_wall in found.doors:
            if distance_to_boundary((cu, cv)) > half_wall + 1.0:
                continue                             # a door of another room
            reach = half_wall + clearance + 3.0 * step
            for sign in (1.0, -1.0):
                out = (cu - dv * sign * reach, cv + du * sign * reach)
                if inside(out):
                    continue
                score = (((here_uv[0] - cu) ** 2 + (here_uv[1] - cv) ** 2) ** 0.5 +
                         ((out[0] - toward_uv[0]) ** 2 + (out[1] - toward_uv[1]) ** 2) ** 0.5)
                if best is None or score < best[0]:
                    best = (score, out, (cu, cv))
        if best is None:
            trace('room leg: no door found in {0}'.format(', '.join(names)))
            return None
        _score, out, door = best
        box = rv.plan_box(element, local)
        slot = None
        if settings['entry'] and box is not None:
            slot = geometry.bar_slot(box, door, [], max(settings['offset_mm'] * FT, step),
                                     clearance + 2.0 * step, settings['edge'], turned=True)
        start = slot[0] if slot else here_uv
        own = (0, rv.id_value(element.Id))
        others = [shape for key, shape in found.others if key != own] + ([geometry.box_rect(box)] if slot else [])
        walls = [shape for _key, shape in found.walls] + segments      # the room's outline: its doors open it
        route = geometry.plan_route(start, out, walls, found.doors, others, clearance, step,
                                    [value * FT for value in config.BUFFERS_MM],
                                    turn_penalty=config.TURN_PENALTY_MM * FT / step,
                                    escape_distance=config.ESCAPE_MM * FT, max_cells=config.MAX_GRID_CELLS)
        if not route.points:
            trace('room leg: no way out of {0} ({1})'.format(', '.join(names), route.reason))
            return None
        points = ([slot[1]] if slot else []) + list(route.points)
        if not slot and settings['edge'] and box is not None:
            points = geometry.trim_ends(points, box, None)
        z = frame.elevations()[0] if frame.elevations() else 0.0
        points = [frame.uv(local.xyz(u, v, z)) for u, v in geometry.clean(points)]
        trace(u'room leg: {0} m inside {1} at {2:.1f} degrees, out by its door'.format(
            round(geometry.length_of(points) / FT / 1000.0, 1), ', '.join(names), math.degrees(angle)).encode('ascii', 'replace'))
        return {'points': points, 'out': points[-1]}

    def _calculate_loop(self, doc, view, frame, settings):
        """Earth bar to earth bar: a ring through the picked bars. It starts at
        the first one picked and always goes on to the nearest bar not yet
        connected; with 'Close the loop' the last goes back to the first."""
        self.legs = []
        bars = []
        for item in self.panels:
            element = doc.GetElement(rv.make_id(item['id']))
            if element is not None:
                bars.append((item, frame.uv(rv.connection_point(element)), rv.plan_box(element, frame)))
        if len(bars) < 2:
            raise ValueError('At least two of the earth bars must still exist.')
        obstacles = self._obstacles(doc, view, frame, settings, [bar[1] for bar in bars])
        walls = [shape for _key, shape in obstacles.walls]
        order = geometry.tour([bar[1] for bar in bars])
        pairs = list(zip(order, order[1:]))
        if settings['close'] and len(order) > 2:
            pairs.append((order[-1], order[0]))
        clearance = settings['clearance_mm'] * FT
        step = settings['grid_mm'] * FT
        offset = settings['offset_mm'] * FT if settings['share'] == 'offset' else 0.0
        spacing = max(offset, step)
        lead = max(clearance + 2.0 * step, spacing)
        buffers = [value * FT for value in config.BUFFERS_MM]
        # Lines already on each bar (the panel lines): the loop takes a free
        # spot beside them and enters the bar the same way they do.
        there = rv.all_route_segments(doc, view, frame, set(
            (bars[a][0]['id'], bars[b][0]['id']) for a, b in pairs))
        taken = dict((index, geometry.occupied(bar[2], there, step) if bar[2] is not None else [])
                     for index, bar in enumerate(bars))

        def end_of(index, other):
            """(point to route from / to, point on the bar or None, obstacle
            shapes, outline to trim to or None) for one end of a leg."""
            item, centre, box = bars[index]
            slot = None
            if settings['entry'] and box is not None:
                slot = geometry.bar_slot(box, bars[other][1], taken[index], spacing, lead, settings['edge'],
                                         turned=True)
            if slot is None:                           # a bar at an angle, or the option is off
                return centre, None, [], (box if settings['edge'] else None)
            taken[index].append((slot[2], slot[3]))
            return slot[0], slot[1], [geometry.box_rect(box)], None

        beside = []
        for index, (a, b) in enumerate(pairs):
            first, second = bars[a], bars[b]
            trace('loop leg {0} of {1}'.format(index + 1, len(pairs)))
            start, start_touch, start_shapes, start_box = end_of(a, b)
            finish, finish_touch, finish_shapes, finish_box = end_of(b, a)
            # a bar in a room turned against the run: the stretch inside that room is routed with the room
            prefix = suffix = None
            elements = [doc.GetElement(rv.make_id(first[0]['id'])), doc.GetElement(rv.make_id(second[0]['id']))]
            try:
                head = self._room_leg(doc, view, settings, elements[0], rv.connection_point(elements[1]), frame)
                tail = self._room_leg(doc, view, settings, elements[1], rv.connection_point(elements[0]), frame)
            except Exception as error:
                trace('room leg failed: {0}'.format(error))
                head = tail = None
            if head:
                prefix = head['points']
                start, start_touch, start_shapes, start_box = head['out'], None, [], None
            if tail:
                suffix = list(reversed(tail['points']))
                finish, finish_touch, finish_shapes, finish_box = tail['out'], None, [], None
            ends = set([(0, first[0]['id']), (0, second[0]['id'])])
            others = [shape for key, shape in obstacles.others if key not in ends]
            route = geometry.plan_route(
                start, finish, walls, obstacles.doors, others + start_shapes + finish_shapes,
                clearance, step, buffers,
                turn_penalty=config.TURN_PENALTY_MM / settings['grid_mm'],
                escape_distance=config.ESCAPE_MM * FT, max_cells=config.MAX_GRID_CELLS,
                beside=beside, spacing=spacing, strict=True)
            if route.points:
                # the straight legs out of the first bar and into the second
                points = ([start_touch] if start_touch else []) + list(route.points) + (
                    [finish_touch] if finish_touch else [])
                route.points = geometry.trim_ends(geometry.clean(points), start_box, finish_box)
                route.points = geometry.clean((list(prefix[:-1]) if prefix else []) + list(route.points) +
                                              (list(suffix[1:]) if suffix else []))
                route.length = geometry.length_of(route.points)
                route.bends = geometry.bends_of(route.points)
                beside.extend(zip(route.points, route.points[1:]))
            leg = {'id': first[0]['id'], 'to': second[0]['id'],
                   'label': first[0]['label'] + ARROW + second[0]['label']}
            self.legs.append(leg)
            self.routes[leg['id']] = route
        self.route_step = step
        self.order = list(self.legs)
        try:
            # everything the router saw, in plan (u, v) feet - to replay a bad loop outside Revit
            dump = {'task': 'loop', 'angle': getattr(frame, 'angle', None),
                    'bars': [(bar[0]['id'], bar[1], bar[2]) for bar in bars], 'pairs': pairs,
                    'clearance': clearance, 'step': step, 'spacing': spacing, 'lead': lead, 'buffers': buffers,
                    'walls': list(obstacles.walls), 'doors': list(obstacles.doors), 'others': list(obstacles.others),
                    'room': list(getattr(self, 'room_names', [])),
                    'legs': [(leg['id'], leg['to'], self.routes[leg['id']].points, self.routes[leg['id']].reason,
                              getattr(self.routes[leg['id']], 'crossed', None)) for leg in self.legs]}
            folder = os.path.join(os.environ.get('APPDATA', ''), 'EleTools', 'EarthingRoute')
            with open(os.path.join(folder, 'last_geometry.txt'), 'w') as handle:
                handle.write(repr(dump))
        except Exception as error:
            trace('geometry dump failed: {0}'.format(error))
    def _add_breaks(self, doc, view, frame, settings):
        """Break each line where it crosses a line that was there before it:
        the earthing lines already in the view, then the lines of this run in
        the order they were routed. Sets route.pieces and self.crossings."""
        items = [item for item in getattr(self, 'order', self._items())
                 if self.routes.get(item['id']) is not None and self.routes[item['id']].points]
        pairs = set((item['id'], self._target_id(item)) for item in self._items())
        earlier = rv.all_route_segments(doc, view, frame, pairs)
        scale = rv.view_scale(view)
        margin = config.BREAK_MARGIN_PAPER_MM * scale * FT
        half_width = config.BREAK_HALF_WIDTH_PAPER_MM * scale * FT
        height = config.BREAK_HEIGHT_PAPER_MM * scale * FT
        total = 0
        for item in items:
            route = self.routes[item['id']]
            own = geometry.crossings_of(route.points, earlier)
            route.pieces, route.breaks = None, 0
            if own and settings['breaks']:
                pieces, marks, count = geometry.break_at_crossings(route.points, earlier, margin, half_width, height)
                route.pieces, route.breaks = pieces + marks, count
            total += own
            earlier.extend(zip(route.points, route.points[1:]))
        self.crossings = total
        self.breaks = sum(self.routes[item['id']].breaks for item in items)

    def _calculate_to_target(self, doc, view, frame, settings):
        """Panels to an earth bar / earth bars to an earth pit or exit point."""
        point = self.bar.get('point')
        if point is not None:
            bar_element = None                       # a clicked exit point
            end_point = frame.uv(point)
            end_box = None
        else:
            bar_element = doc.GetElement(rv.make_id(self.bar['id']))
            if bar_element is None:
                raise ValueError('The earth bar / earth pit no longer exists. Select it again.')
            end_point = frame.uv(rv.connection_point(bar_element))
            # outlines in the plan, to stop the lines on the edges
            end_box = rv.plan_box(bar_element, frame) if settings['edge'] else None
        boxes = {}
        starts = {}
        source_touch = {}           # earth bar id -> where its line leaves the bar
        source_shape = {}           # earth bar id -> the bar as an obstacle for its own line
        leave_square = settings['task'] == 'pit' and settings['entry']
        if leave_square:
            # the sources are earth bars: their lines leave them the way the
            # panel lines enter them - square to the long side, on a free spot
            there = rv.all_route_segments(doc, view, frame, set(
                (panel['id'], self.bar['id']) for panel in self.panels))
            step_ft = settings['grid_mm'] * FT
            gap = max(settings['offset_mm'] * FT, step_ft)
            lead_out = max(settings['clearance_mm'] * FT + 2.0 * step_ft, gap)
        for panel in self.panels:
            element = doc.GetElement(rv.make_id(panel['id']))
            if element is None:
                route = geometry.Route()
                route.reason = 'the panel no longer exists'
                self.routes[panel['id']] = route
                continue
            starts[panel['id']] = frame.uv(rv.connection_point(element))
            if settings['edge']:
                boxes[panel['id']] = rv.plan_box(element, frame)
            if leave_square:
                box = boxes.get(panel['id']) or rv.plan_box(element, frame)
                slot = geometry.bar_slot(box, end_point, geometry.occupied(box, there, step_ft),
                                         gap, lead_out, settings['edge'], turned=True) if box is not None else None
                if slot is not None:
                    starts[panel['id']] = slot[0]
                    source_touch[panel['id']] = slot[1]
                    source_shape[panel['id']] = geometry.box_rect(box)
                    boxes[panel['id']] = None          # it already starts on the edge
        room_legs = {}              # panel id -> its first stretch, inside its own turned room
        if settings['task'] == 'pit':
            toward = point if point is not None else rv.connection_point(bar_element)
            for panel in self.panels:
                element = doc.GetElement(rv.make_id(panel['id']))
                if element is None or panel['id'] not in starts:
                    continue
                try:
                    leg = self._room_leg(doc, view, settings, element, toward, frame)
                except Exception as error:
                    trace('room leg failed: {0}'.format(error))
                    leg = None
                if leg:
                    room_legs[panel['id']] = leg['points']
                    starts[panel['id']] = leg['out']
                    source_touch.pop(panel['id'], None)
                    source_shape.pop(panel['id'], None)
                    boxes[panel['id']] = None
        self.order = []
        self.route_step = settings['grid_mm'] * FT
        if not starts:
            return
        # the obstacles are collected ONCE, for the region of all routes
        obstacles = self._obstacles(doc, view, frame, settings, [end_point] + list(starts.values()))
        walls = [shape for _key, shape in obstacles.walls]
        bar_key = (0, self.bar['id'])
        clearance = settings['clearance_mm'] * FT
        step = settings['grid_mm'] * FT
        buffers = [value * FT for value in config.BUFFERS_MM]
        # Lines a new route may tee into: the earthing lines of OTHER panels
        # to this earth bar that are already in the view, then every route
        # made in this run. The panel nearest to the earth bar goes first, so
        # the far ones join the near ones.
        join = beside = None
        offset = settings['offset_mm'] * FT
        if settings['share'] in ('join', 'offset'):
            lines = rv.route_segments(doc, view, frame, self.bar['id'],
                                      set(panel['id'] for panel in self.panels))
            trace('{0} existing line piece(s), mode {1}'.format(len(lines), settings['share']))
            if settings['share'] == 'join':
                join = lines
            else:
                beside = lines
        order = sorted([panel for panel in self.panels if panel['id'] in starts],
                       key=lambda panel: (abs(starts[panel['id']][0] - end_point[0]) +
                                          abs(starts[panel['id']][1] - end_point[1])))
        # Side by side: each line gets its own spot on the earth bar, spread
        # along its long side in the order the panels lie along it, so the
        # lines arrive next to each other instead of on one point.
        ends = {}
        pull_axis = None
        touches = {}                # panel id -> where its line meets the earth bar
        bar_obstacle = []
        bar_box = end_box or (rv.plan_box(bar_element, frame) if bar_element is not None else None)
        entries = None
        route_step = step               # the grid the routes are really made on
        spacing = offset                # the distance really kept between lines
        anchor = None
        self.grid_note = u''

        def obstacles_for(panel):
            # a route's own panel and the earth bar are not obstacles for it;
            # the OTHER selected panels are
            own = (0, panel['id'])
            mine = [source_shape[panel['id']]] if panel['id'] in source_shape else []
            return [shape for key, shape in obstacles.others if key != own and key != bar_key] + mine

        turned_bar = geometry.is_turned(bar_box)
        tails = {}                  # panel id -> the last points into a turned bar
        turned_variants = [None]    # ways into a turned bar to try (see turned_entries)
        first_round = 1             # how many of them are always tried before the best is taken
        entry_order = list(order)   # the order of `starts` the variants were made for
        if settings['entry'] and turned_bar and order:
            # FIX: an earth bar turned on the routing frame (square to the drawing, in a room laid out at
            # 45 degrees) got no square entry - the lines came into it at 45 degrees with a small jog. Now
            # each line runs to a point in front of its own spot and its last leg goes square into the bar.
            separate = settings['share'] == 'offset' and len(order) > 1
            lead = clearance + 2.0 * route_step
            entries, note = geometry.turned_entries(bar_box, [starts[panel['id']] for panel in order]
                                                    if separate else [starts[order[0]['id']]],
                                                    route_step, max(offset, route_step) if separate else route_step,
                                                    lead, settings['edge'])
            if note:
                self.grid_note = note
            if entries:
                anchor = entries[0][0]
                for index, panel in enumerate(order):
                    ends[panel['id']], tails[panel['id']] = entries[index if separate else 0]
                    touches[panel['id']] = tails[panel['id']][-1]
                if separate:
                    # ways into the bar to try: spots centred / packed at either end of the bar (first round),
                    # then the rows reached on staggered lines (only when the first round still crosses)
                    turned_variants = []
                    for stagger, shift in ((0, 0), (0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (-1, 1), (1, -1), (-1, -1)):
                        variant = geometry.turned_entries(bar_box, [starts[panel['id']] for panel in order],
                                                          route_step, max(offset, route_step), lead,
                                                          settings['edge'], stagger=stagger, shift=shift)[0]
                        if variant and variant not in turned_variants:
                            turned_variants.append(variant)
                    first_round = 3
                bar_obstacle = [geometry.box_rect(bar_box)]
                trace('bar entry (bar turned on the frame): {0} spot(s)'.format(len(entries)))
                entries = None                       # handled: skip the square-bar steps below
        elif settings['entry'] and bar_box is not None and order:
            # Lines enter the bar square to its long side: each is routed to a
            # point in front of its own spot and gets a last straight leg in.
            # With tees or overlapping lines there is one shared spot.
            separate = settings['share'] == 'offset'
            axis = geometry.long_axis(bar_box)
            points = [starts[panel['id']] for panel in order]
            keys = None
            if separate and len(order) > 1:
                # 1. How far apart can the lines be on the bar? The grid is
                #    then made to FIT that distance, with the spots exactly on
                #    grid lines. On a coarser grid two spots share a cell and
                #    their lines can only reach them by crossing.
                spots = geometry.spread_along(bar_box, len(order), offset)
                gap = abs(spots[1][0] - spots[0][0]) + abs(spots[1][1] - spots[0][1])
                smallest = config.MIN_AUTO_GRID_MM * FT
                if gap >= step:
                    route_step = gap / max(1, int(round(gap / step)))
                else:
                    route_step = max(gap, smallest)
                spacing = max(route_step, min(offset, gap))
                notes = []
                if abs(route_step - step) > step * 0.01:
                    notes.append(u'grid set to {0:.0f} mm to fit the lines on the earth bar'.format(route_step / FT))
                if gap < offset * 0.999:
                    notes.append(u'the earth bar has room for {0} lines only {1:.0f} mm apart (not {2:g} mm)'.format(
                        len(order), gap / FT, settings['offset_mm']))
                if gap < smallest * 0.999:
                    notes.append(u'that is too close to draw apart: crossings are likely - use fewer panels '
                                 u'per earth bar')
                self.grid_note = u'; '.join(notes)
                trace('bar: {0} lines, gap {1:.0f} mm, grid {2:.0f} mm'.format(len(order), gap / FT, route_step / FT))
            lead = max(clearance + 2.0 * route_step, spacing if separate else 0.0)
            if separate and len(order) > 1:
                # 2. Which line gets which spot? Each line is first routed on
                #    its own to the middle of the bar, to see from where it
                #    really arrives (panels in a back row have to go round the
                #    front row). The spots are handed out in that order.
                scout = geometry.bar_entries(bar_box, points[:1], route_step, lead, settings['edge'])
                if scout:
                    target = scout[0][0]
                    along_u = axis == 'u'
                    keys = []
                    for index, panel in enumerate(order):
                        trace('scouting panel {0} of {1}'.format(index + 1, len(order)))
                        trial = geometry.plan_route(
                            starts[panel['id']], target, walls, obstacles.doors,
                            obstacles_for(panel) + [geometry.box_rect(bar_box)], clearance,
                            max(step, route_step), buffers,           # a look only: the coarse grid will do
                            turn_penalty=config.TURN_PENALTY_MM * FT / max(step, route_step),
                            escape_distance=config.ESCAPE_MM * FT, max_cells=config.MAX_GRID_CELLS,
                            pull_axis=axis, anchor=target)
                        path = trial.points or [starts[panel['id']]]
                        keys.append([round(point[0] if along_u else point[1], 4) for point in reversed(path)][1:]
                                    or [round(path[0][0] if along_u else path[0][1], 4)])
            entries = geometry.bar_entries(bar_box, points if separate else points[:1],
                                           spacing if separate else route_step, lead, settings['edge'],
                                           keys=keys)
            if entries:
                anchor = entries[0][0]
        if entries:
            for index, panel in enumerate(order):
                approach, touch = entries[index if len(entries) > 1 else 0]
                ends[panel['id']] = approach
                touches[panel['id']] = touch
            # the bar itself is now in the way of a line that would come from behind
            bar_obstacle = [geometry.box_rect(bar_box)]
            trace('bar entry: {0} spot(s)'.format(len(entries)))
            pull_axis = geometry.long_axis(bar_box)
            if len(entries) > 1:
                # the order in which the lines can nest instead of crossing
                if keys is not None:
                    middle = round(target[0] if along_u else target[1], 4)
                    order = [order[index] for index in geometry.nest_order(keys, middle)]
                else:
                    order = [order[index] for index in geometry.entry_order(
                        [touches[panel['id']] for panel in order], bar_box)]
        elif settings['share'] == 'offset' and len(order) > 1 and not touches:
            if bar_box is not None:
                slots = geometry.spread_along(bar_box, len(order), offset)
                axis = (slots[-1][0] - slots[0][0], slots[-1][1] - slots[0][1])
                along = sorted(order, key=lambda panel: (starts[panel['id']][0] * axis[0] +
                                                         starts[panel['id']][1] * axis[1]))
                for slot, panel in zip(slots, along):
                    ends[panel['id']] = slot
        if anchor is None and point is not None:
            # a clicked exit point: the grid passes through it, so the lines end on it without a small jog
            anchor = end_point

        def route_all(sequence):
            """Route the lines in this order; earlier lines are in the way of later ones."""
            routes = {}
            join_ = list(join) if join is not None else None
            beside_ = list(beside) if beside is not None else None
            for index, panel in enumerate(sequence):
                trace('routing panel {0} of {1} (id {2})'.format(index + 1, len(self.panels), panel['id']))
                others = obstacles_for(panel) + bar_obstacle
                routes[panel['id']] = geometry.plan_route(
                    starts[panel['id']], ends.get(panel['id'], end_point), walls, obstacles.doors, others,
                    clearance, route_step, buffers,
                    turn_penalty=config.TURN_PENALTY_MM * FT / route_step,
                    escape_distance=config.ESCAPE_MM * FT, max_cells=config.MAX_GRID_CELLS, join=join_,
                    beside=beside_, spacing=spacing, pull_axis=pull_axis, anchor=anchor,
                    strict=beside_ is not None)      # earlier lines are walls: no crossing
                route = routes[panel['id']]
                entered = False
                if route.points and panel['id'] in touches and not route.joined:
                    # the last leg: straight into the bar from the point in front of it
                    route.points = geometry.clean(list(route.points) + list(tails.get(panel['id']) or [touches[panel['id']]]))
                    route.length = geometry.length_of(route.points)
                    route.bends = geometry.bends_of(route.points)
                    entered = True
                if route.points and panel['id'] in room_legs:
                    # the stretch inside the bar's own (turned) room first
                    route.points = geometry.clean(list(room_legs[panel['id']][:-1]) + list(route.points))
                    route.length = geometry.length_of(route.points)
                    route.bends = geometry.bends_of(route.points)
                if route.points and panel['id'] in source_touch:
                    # the first leg: straight out of the earth bar it starts on
                    route.points = geometry.clean([source_touch[panel['id']]] + list(route.points))
                    route.length = geometry.length_of(route.points)
                    route.bends = geometry.bends_of(route.points)
                if route.points and settings['edge']:
                    # routed centre to centre, then cut back to the two outlines
                    # (a line that ends in a tee keeps that end as it is)
                    route.points = geometry.trim_ends(route.points, boxes.get(panel['id']),
                                                      None if (route.joined or entered) else end_box)
                    route.length = geometry.length_of(route.points)
                    route.bends = geometry.bends_of(route.points)
                if route.points and join_ is not None:
                    join_.extend(zip(route.points, route.points[1:]))
                if route.points and beside_ is not None:
                    beside_.extend(zip(route.points, route.points[1:]))
            return routes

        sequences = [list(order)]
        if tails and len(order) > 1:
            # FIX: into a turned earth bar the nearest-first order made one line cut across another (lines
            # coming from below have to nest from the lowest spot up). Every order in which the lines can
            # nest is tried: nearest first, and spot by spot from either end of the bar; the one with the
            # fewest crossings (then forced lines, then length) is kept.
            bu_, bv_, bdu, bdv = geometry._bar_axes(bar_box)[:4]
            along = sorted(order, key=lambda panel: (touches[panel['id']][0] - bu_) * bdu +
                                                    (touches[panel['id']][1] - bv_) * bdv)
            for candidate in (list(reversed(along)), along):
                if candidate not in sequences:
                    sequences.append(candidate)
        best = None
        attempt = 0
        for number, variant in enumerate(turned_variants):
            if number >= first_round and best is not None and best[0][:3] == (0, 0, 0):
                break                   # a clean way in was found in the first round: the shortest of those
            if variant is not None:
                for index, panel in enumerate(entry_order):
                    ends[panel['id']], tails[panel['id']] = variant[index]
                anchor = variant[0][0]
            for sequence in sequences:
                attempt += 1
                if attempt > 1:
                    trace('trying another way in ({0})'.format(attempt))
                routes = route_all(sequence)
                lines = [routes[p['id']].points for p in sequence if routes[p['id']].points]
                score = (len(sequence) - len(lines), geometry.crossings(lines),
                         sum(1 for p in sequence if getattr(routes[p['id']], 'forced', False)),
                         sum(geometry.length_of(points) for points in lines))
                if best is None or score < best[0]:
                    best = (score, sequence, routes, variant)
        order = best[1]
        if best[3] is not None:
            for index, panel in enumerate(entry_order):
                ends[panel['id']], tails[panel['id']] = best[3][index]
        trace('routing: {0} crossing(s), {1} way(s) tried'.format(best[0][1], attempt))
        self.routes.update(best[2])
        self.route_step = route_step
        self.order = list(order)            # the order the lines were routed in (for the breaks)
        try:
            # everything the router saw, in plan (u, v) feet - to replay a bad result outside Revit
            dump = {'angle': frame.angle if hasattr(frame, 'angle') else None, 'end_point': end_point,
                    'bar_box': bar_box, 'end_box': end_box, 'starts': starts, 'boxes': boxes,
                    'ends': ends, 'touches': touches, 'entries': entries, 'anchor': anchor,
                    'pull_axis': pull_axis, 'clearance': clearance, 'route_step': route_step, 'spacing': spacing,
                    'buffers': buffers, 'share': settings['share'], 'room': list(self.room_names),
                    'walls': list(obstacles.walls), 'doors': list(obstacles.doors), 'others': list(obstacles.others),
                    'order': [panel['id'] for panel in order],
                    'routes': dict((pid, (r.points, r.reason if hasattr(r, 'reason') else None,
                                          getattr(r, 'crossed', None), getattr(r, 'step', None)))
                                   for pid, r in self.routes.items())}
            folder = os.path.join(os.environ.get('APPDATA', ''), 'EleTools', 'EarthingRoute')
            with open(os.path.join(folder, 'last_geometry.txt'), 'w') as handle:
                handle.write(repr(dump))
        except Exception as error:
            trace('geometry dump failed: {0}'.format(error))

    def _rows(self, settings, extra=None):
        """Result rows + the text report. extra: panel id -> (status, kind, note)."""
        rows, lines, details = [], [], []
        for panel in self._items():
            route = self.routes.get(panel['id'])
            if route is None:
                continue
            if route.points is None:
                status, kind = 'FAILED', 'fail'
                note = u'No valid route found with {0:g} mm clearance ({1}).'.format(
                    settings['clearance_mm'], route.reason)
                length = bends = ''
            else:
                status, kind, note = 'SUCCESS', 'ok', ''
                if getattr(route, 'joined', False):
                    note = u'Tees into the earthing line of another panel.'
                length = '{0:.2f}'.format(route.length / FT / 1000.0)
                bends = str(route.bends)
                if getattr(route, 'breaks', 0):
                    note = (note + u' ' if note else u'') + u'{0} break(s) where it crosses other lines.'.format(
                        route.breaks)
                elif getattr(route, 'forced', False):
                    kind = 'warn'
                    note = u'Crosses another earthing line: there was no way beside the lines already routed.'
                if route.crossed:
                    kind = 'warn'
                    note = (u'CHECK: the line crosses an obstacle next to the panel or the earth bar '
                            u'(it is inside or behind one).')
                if route.step > getattr(self, 'route_step', settings['grid_mm'] * FT) * 1.01:
                    note = (note + u' ' if note else u'') + u'Long route: grid coarsened to {0:.0f} mm.'.format(
                        route.step / FT)
            if extra and panel['id'] in extra:
                status, kind, extra_note = extra[panel['id']]
                note = (extra_note + (u' ' + note if note else u'')).strip()
            rows.append(Row(panel['label'], status, kind, length, bends, note))
            lines.append(u'{0:<28}{1}'.format(panel['label'][:27], status))
            if note:
                details.append(u'{0}:\n    {1}'.format(panel['label'], note))
        self.grid.ItemsSource = rows
        report = [u'EARTHING ROUTE REPORT', u'',
                  (u'Loop      : {0} earth bar(s)'.format(len(self.panels)) if self.task == 'loop' else
                   u'{0}: {1}'.format('Earth pit ' if self.task == 'pit' else 'Earth bar ', self.bar['label'])),
                  u'Floor plan: {0}'.format(self.view_name),
                  u'Clearance {0:g} mm, grid {1:g} mm'.format(settings['clearance_mm'], settings['grid_mm']),
                  u'Obstacles : {0}'.format(getattr(self, 'obstacle_text', ''))] + (
                      [u'Room      : lines kept inside {0}'.format(', '.join(self.room_names))] if getattr(self, 'room_names', None) else
                      [u'Room      : ' + getattr(self, 'room_note', u'')] if settings.get('in_room', True) and getattr(self, 'room_note', u'') else []) + (
                      [u'Note      : {0}'.format(self.grid_note)] if getattr(self, 'grid_note', u'') else []) + [
                  u'',
                  u'{0:<28}{1}'.format('From' if self.task != 'panels' else 'Panel', 'Status'), u'-' * 44] + lines
        if details:
            report += [u''] + details
        self.report_tb.Text = u'\n'.join(report)
        return rows

    # ------------------------------------------------------------------ preview
    def _remove_preview(self, uidoc):
        """Delete every preview line in the view - those of this window and
        any left behind by an earlier run. Returns how many."""
        view = uidoc.ActiveView
        ids = list(self.preview_ids)
        known = set(rv.id_value(i) for i in ids)
        for key, found in rv.tagged_lines(self.doc, view).items():
            if key[2] == rv.KIND_PREVIEW:
                ids.extend(i for i in found if rv.id_value(i) not in known)
        self.preview_ids = []
        if not ids:
            return 0
        return rv.run_transaction(self.doc, 'Earthing routes - clear preview',
                                  lambda: rv.delete_ids(self.doc, ids))

    def _bent(self, good, settings):
        """[(panel, piece, radius per vertex), ...]: every drawn piece of the routes with the bend radius of
        each of its corners - concentric where lines run side by side."""
        items = [(panel, piece) for panel, route in good for piece in (route.pieces or [route.points])]
        radius = settings.get('bend_mm', 0.0) * FT
        reach = max(settings.get('offset_mm', 0.0) or config.PARALLEL_OFFSET_MM, 50.0) * FT * 12.0
        radii = geometry.bend_radii([piece for _panel, piece in items], radius, reach)
        return [(panel, piece, r) for (panel, piece), r in zip(items, radii)]

    def _preview(self, sender, args):
        try:
            settings = self._settings()
        except ValueError as error:
            forms.alert(str(error), title=config.TITLE)
            return
        self._remember(settings)
        self._status(u'Calculating... the window does not respond until it is done.')

        def action(uidoc):
            self._calculate(uidoc, settings)
            self._remove_preview(uidoc)
            view = uidoc.ActiveView
            good = [(panel, self.routes[panel['id']]) for panel in self._items()
                    if self.routes.get(panel['id']) is not None and self.routes[panel['id']].points]

            def draw():
                ids = []
                for panel, piece, radii in self._bent(good, settings):
                    ids.extend(rv.draw_route(self.doc, view, self.frame, piece, panel['id'],
                                             self._target_id(panel), rv.KIND_PREVIEW, bend_radius=radii))
                return ids
            if good:
                trace('drawing the preview of {0} route(s)'.format(len(good)))
                self.preview_ids = rv.run_transaction(self.doc, 'Earthing routes - preview', draw)
                trace('preview drawn: {0} line(s)'.format(len(self.preview_ids)))
                uidoc.RefreshActiveView()
            rows = self._rows(settings)
            failed = len([row for row in rows if row.Kind == 'fail'])
            crossed = getattr(self, 'crossings', 0)
            return (u'Preview: {0} route(s) shown in magenta, {1} failed ({2:.1f} s). {3} Pan and zoom '
                    u'to check them, then "Create Routes" or "Clear Preview".'.format(
                        len(good), failed, getattr(self, 'took', 0.0),
                        (u'The lines do not cross each other.' if not crossed else
                         (u'{0} crossing(s), drawn with a break.'.format(crossed)
                          if getattr(self, 'breaks', 0) else
                          u'{0} crossing(s) of lines could not be avoided.'.format(crossed))) +
                        (u' Note: {0}.'.format(self.grid_note) if getattr(self, 'grid_note', u'') else u'')))
        self._in_plan(action)

    def _clear_preview(self, sender, args):
        def action(uidoc):
            count = self._remove_preview(uidoc)
            uidoc.RefreshActiveView()
            return (u'Preview cleared ({0} line(s) removed).'.format(count) if count
                    else 'There is no preview to clear.')
        self._in_plan(action)

    # ------------------------------------------------------------------ create
    def _create(self, sender, args):
        try:
            settings = self._settings()
            if not settings['style']:
                raise ValueError('Choose the line style for the earthing lines.')
        except ValueError as error:
            forms.alert(str(error), title=config.TITLE)
            return
        self._remember(settings)

        def action(uidoc):
            self._calculate(uidoc, settings)
            view = uidoc.ActiveView
            existing = rv.tagged_lines(self.doc, view)
            good = [(panel, self.routes[panel['id']]) for panel in self._items()
                    if self.routes.get(panel['id']) is not None and self.routes[panel['id']].points]
            connected = [panel for panel, _route in good
                         if (panel['id'], self._target_id(panel), rv.KIND_ROUTE) in existing]
            choice = None
            if connected:
                names = u'\n'.join(u'    {0}'.format(panel['label']) for panel in connected[:15])
                if len(connected) > 15:
                    names += u'\n    ... and {0} more'.format(len(connected) - 15)
                choice = forms.alert(
                    u'Already connected:\n\n{0}\n\nWhat should happen to these?'.format(names),
                    title=config.TITLE,
                    options=['Skip', 'Replace Existing', 'Cancel'])
                if choice not in ('Skip', 'Replace Existing'):
                    return 'Nothing was created.'
            connected_ids = set(panel['id'] for panel in connected)
            extra = {}

            bent = {}
            for panel, piece, radii in self._bent(good, settings):
                bent.setdefault(panel['id'], []).append((piece, radii))

            def draw():
                made = 0
                for panel, route in good:
                    already = panel['id'] in connected_ids
                    if already and choice == 'Skip':
                        extra[panel['id']] = ('ALREADY CONNECTED', 'warn', 'Skipped: the existing route was kept.')
                        continue
                    if already:
                        rv.delete_ids(self.doc, existing[(panel['id'], self._target_id(panel), rv.KIND_ROUTE)])
                        extra[panel['id']] = ('REPLACED', 'ok', 'The existing route was replaced.')
                    for piece, radii in bent.get(panel['id'], []):
                        rv.draw_route(self.doc, view, self.frame, piece, panel['id'], self._target_id(panel),
                                      rv.KIND_ROUTE, settings['style'], bend_radius=radii)
                    made += 1
                return made
            self._remove_preview(uidoc)
            trace('creating {0} route(s) in style {1}'.format(len(good), settings['style']))
            made = rv.run_transaction(self.doc, 'Earthing routes', draw) if good else 0
            trace('created {0} route(s)'.format(made))
            uidoc.RefreshActiveView()
            rows = self._rows(settings, extra)
            failed = len([row for row in rows if row.Kind == 'fail'])
            return (u'{0} route(s) created as detail lines in style "{1}". {2} skipped, {3} failed. '
                    u'One Undo removes them.'.format(
                        made, settings['style'],
                        len([1 for value in extra.values() if value[0] == 'ALREADY CONNECTED']), failed))
        self._in_plan(action)

    # ------------------------------------------------------------------ closing
    def _on_closing(self, sender, args):
        """Take the preview lines away with the window."""
        self._closing = True
        runner = self.runner

        def action(uiapp):
            try:
                uidoc = uiapp.ActiveUIDocument
                if uidoc is not None and uidoc.Document.Equals(self.doc):
                    ids = [i for i in self.preview_ids if self.doc.GetElement(i) is not None]
                    if ids:
                        rv.run_transaction(self.doc, 'Earthing routes - clear preview',
                                           lambda: rv.delete_ids(self.doc, ids))
                        uidoc.RefreshActiveView()
            finally:
                runner.dispose()
                if self in _OPEN:
                    _OPEN.remove(self)
        if self.preview_ids:
            runner.run(action)
        else:
            runner.dispose()
            if self in _OPEN:
                _OPEN.remove(self)


def main():
    uidoc = revit.uidoc
    if uidoc is None or uidoc.Document is None or not rv.is_floor_plan(uidoc.ActiveView):
        forms.alert('Please open a Floor Plan view before running Earthing Route Generator.',
                    title=config.TITLE, exitscript=True)
        return None
    for window in list(_OPEN):
        try:
            if window.IsVisible:
                if window.view_id == rv.id_value(uidoc.ActiveView.Id):
                    window.Activate()
                    return window
                window.Close()                # opened for another plan
        except Exception:
            pass
    window = EarthingWindow(uidoc)
    _OPEN.append(window)
    window.show()                              # modeless
    return window

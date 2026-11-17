# -*- coding: utf-8 -*-
"""Create Panel Schedules - make the missing panel schedules of the electrical panels.

    1. Scan the Electrical Equipment of this model and keep the PANELS (family Part Type
       Panelboard, Switchboard or Other Panel - see PANEL_PART_TYPES).
    2. Read every existing panel schedule view (PanelScheduleView.GetPanel()) -> which
       panels already have one.
    3. The WINDOW (ui.xaml): the user ticks the panels and picks the template for each kind of
       panel (Branch = Panelboard, Switchboard, Data = Other Panel; each list shows only the
       templates of that kind). Panels that already have a schedule are greyed out: skipped.
    4. A schedule is created for each ticked panel with
       PanelScheduleView.CreateInstanceView(doc, templateId, panel.Id) - or, for
       '<Revit default>', CreateInstanceView(doc, panel.Id) (the default template of the type).
    5. Report: panels found, skipped, created, not panels, errors - with clickable ids.
       Step trace for "check logs": %APPDATA%\\pyRevit\\CreatePanelSchedules_trace.log

WHAT THIS CHANGES IN THE MODEL: only new panel schedule views. Nothing else is touched.
Everything is ONE transaction (one Undo); each panel runs in its own sub-transaction, so a
panel that fails is rolled back alone and the other panels go on.

Revit 2023+ (IronPython 2.7 / pyRevit). ElementId.IntegerValue is gone in Revit 2026 -
eid_int() reads .Value first.
"""
from pyrevit import forms, revit, script

from Autodesk.Revit.DB import (BuiltInCategory, BuiltInParameter, ElementId, FailureProcessingResult,
                               FailureSeverity, FamilyInstance, FilteredElementCollector,
                               IFailuresPreprocessor, PartType, SubTransaction, Transaction,
                               TransactionStatus)
from Autodesk.Revit.DB.Electrical import PanelScheduleTemplate, PanelScheduleView

import io
import os
import time

import safetable  # crash-safe output tables (lib/safetable.py)

TITLE = u'Create Panel Schedules'
XAML = os.path.join(os.path.dirname(__file__), 'ui.xaml')
DEFAULT_TEMPLATE = u'<Revit default>'

# kind of panel -> (Part Type name, PanelScheduleType name of its templates, window combo)
KINDS = [('branch', 'PanelBoard', 'Branch', 'branch'),
         ('switchboard', 'SwitchBoard', 'Switchboard', 'switch'),
         ('data', 'OtherPanel', 'Data', 'data')]
KIND_LABEL = {'branch': u'Branch panel', 'switchboard': u'Switchboard', 'data': u'Data panel', 'unknown': u'?'}


# ============================================================================ trace ("check logs")
_T0 = [time.time()]


def trace(message, new=False):
    """One line per step in %APPDATA%\\pyRevit\\CreatePanelSchedules_trace.log (flushed at once)."""
    try:
        folder = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'pyRevit')
        if not os.path.isdir(folder):
            os.makedirs(folder)
        if new:
            _T0[0] = time.time()
        with io.open(os.path.join(folder, 'CreatePanelSchedules_trace.log'), 'w' if new else 'a',
                     encoding='utf-8') as handle:
            handle.write(u'{0} +{1:6.1f}s  {2}\n'.format(time.strftime('%H:%M:%S'), time.time() - _T0[0], message))
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        pass

# ============================================================================ settings
# Family Part Types that get a panel schedule in Revit. Read by name, so a name missing in an
# older / newer API is simply ignored instead of breaking the button.
PANEL_PART_TYPE_NAMES = ('PanelBoard', 'SwitchBoard', 'OtherPanel')
PANEL_PART_TYPES = set(int(getattr(PartType, name)) for name in PANEL_PART_TYPE_NAMES
                       if hasattr(PartType, name))

# Equipment whose Part Type cannot be read (very old families): try to create a schedule anyway?
# True = try (Revit refuses non-panels and that is reported as an error), False = leave them out.
TRY_UNKNOWN_PART_TYPE = True

output = script.get_output()


# ============================================================================ small helpers
def eid_int(element_id):
    """The number of an ElementId - .Value (Revit 2024+), else .IntegerValue (2023)."""
    try:
        return int(element_id.Value)
    except AttributeError:
        return int(element_id.IntegerValue)


def text(value):
    try:
        return u'{0}'.format(value or u'').strip()
    except Exception:
        return u''


def panel_name(panel):
    """The Panel Name parameter, else the element name."""
    try:
        value = panel.get_Parameter(BuiltInParameter.RBS_ELEC_PANEL_NAME).AsString()
        if value and value.strip():
            return value.strip()
    except Exception:
        pass
    return text(getattr(panel, 'Name', u'')) or u'(no name)'


def type_label(panel):
    """'Family : Type' of the panel."""
    try:
        symbol = panel.Symbol
        return u'{0} : {1}'.format(symbol.Family.Name, symbol.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM).AsString())
    except Exception:
        return u'?'


def part_type(panel):
    """The family's Part Type as an int, or None when it cannot be read."""
    try:
        family = panel.Symbol.Family
        param = family.get_Parameter(BuiltInParameter.FAMILY_CONTENT_PART_TYPE)
        if param is not None and param.HasValue:
            return param.AsInteger()
    except Exception:
        pass
    return None


def panel_kind(panel):
    """'branch' / 'switchboard' / 'data' (which templates fit it), or 'unknown'."""
    value = part_type(panel)
    for kind, part_name, _schedule_type, _combo in KINDS:
        if hasattr(PartType, part_name) and value == int(getattr(PartType, part_name)):
            return kind
    return 'unknown'


def templates_by_kind(doc):
    """{kind: [(name, ElementId)]} of the panel schedule templates, by PanelScheduleType.
    The default template of a type is listed first and marked '(default)'."""
    by_type = dict((schedule_type, kind) for kind, _p, schedule_type, _c in KINDS)
    found = dict((kind, []) for kind, _p, _s, _c in KINDS)
    for template in FilteredElementCollector(doc).OfClass(PanelScheduleTemplate):
        try:
            kind = by_type.get(text(template.GetPanelScheduleType()))
        except Exception:
            kind = None
        if kind is None:
            continue
        is_default = False
        try:
            is_default = bool(template.IsDefault)
        except Exception:
            pass
        name = text(template.Name) + (u'   (default)' if is_default else u'')
        found[kind].append((0 if is_default else 1, name.lower(), name, template.Id))
    return dict((kind, [(n, i) for _d, _l, n, i in sorted(items)]) for kind, items in found.items())


def part_type_name(value):
    if value is None:
        return u'unknown'
    try:
        from System import Enum
        return text(Enum.ToObject(PartType, value))
    except Exception:
        return text(value)


class _SwallowWarnings(IFailuresPreprocessor):
    """Warnings (not errors) during the creation are dismissed, so no dialog stops the batch.
    Errors are left to Revit and are reported if the transaction does not commit."""

    def PreprocessFailures(self, accessor):
        for failure in accessor.GetFailureMessages():
            try:
                if failure.GetSeverity() == FailureSeverity.Warning:
                    accessor.DeleteWarning(failure)
            except Exception:
                pass
        return FailureProcessingResult.Continue


# ============================================================================ 1. the panels
def collect_panels(doc, only_ids=None):
    """(panels, not_panels) - Electrical Equipment instances split by family Part Type.
    only_ids: a set of element ids (ints) to limit the scan to (the user's selection)."""
    panels, not_panels = [], []
    collector = (FilteredElementCollector(doc)
                 .OfCategory(BuiltInCategory.OST_ElectricalEquipment)
                 .WhereElementIsNotElementType())
    for element in collector:
        if not isinstance(element, FamilyInstance):
            continue
        if only_ids is not None and eid_int(element.Id) not in only_ids:
            continue
        value = part_type(element)
        if value in PANEL_PART_TYPES or (value is None and TRY_UNKNOWN_PART_TYPE):
            panels.append(element)
        else:
            not_panels.append(element)
    panels.sort(key=lambda p: panel_name(p).lower())
    return panels, not_panels


# ============================================================================ 2. existing schedules
def existing_schedules(doc):
    """{panel id (int): [schedule view names]} of every panel schedule INSTANCE view (templates are
    not schedules of a panel)."""
    found = {}
    for view in FilteredElementCollector(doc).OfClass(PanelScheduleView):
        try:
            if view.IsTemplate or view.IsPanelScheduleTemplate():
                continue
        except Exception:
            pass
        try:
            panel_id = view.GetPanel()
        except Exception:
            continue
        if panel_id is None or panel_id == ElementId.InvalidElementId:
            continue
        found.setdefault(eid_int(panel_id), []).append(text(view.Name))
    return found


# ============================================================================ 3. create
def create_schedules(doc, panels, template_ids=None):
    """Create a schedule for each panel. Returns (results, transaction_error).
    template_ids: {kind: template ElementId, or None = the Revit default of the type}.
    results: [(panel, status, detail)] - status 'created' / 'error'."""
    template_ids = template_ids or {}
    results = []
    transaction = Transaction(doc, TITLE)
    options = transaction.GetFailureHandlingOptions()
    swallow = _SwallowWarnings()                    # keep a reference while the transaction runs
    options.SetFailuresPreprocessor(swallow)
    transaction.SetFailureHandlingOptions(options)
    transaction.Start()
    try:
        for panel in panels:
            # one sub-transaction per panel: a failure rolls back this panel only
            sub = SubTransaction(doc)
            sub.Start()
            template_id = template_ids.get(panel_kind(panel))
            try:
                if template_id is not None:
                    view = PanelScheduleView.CreateInstanceView(doc, template_id, panel.Id)
                else:
                    view = PanelScheduleView.CreateInstanceView(doc, panel.Id)
                if view is None:
                    raise Exception(u'Revit returned no schedule (no default template for this panel type?)')
                sub.Commit()
                results.append((panel, 'created', text(view.Name)))
                trace(u'  created  {0}  ->  {1}'.format(panel_name(panel), text(view.Name)))
            except Exception as error:
                if sub.HasStarted() and not sub.HasEnded():
                    sub.RollBack()
                message = text(error).split(u'\n')[0] or u'unknown error'
                results.append((panel, 'error', message))
                trace(u'  ERROR    {0}: {1}'.format(panel_name(panel), message))
        status = transaction.Commit()
    except Exception as error:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
        return results, u'Nothing was created: {0}'.format(text(error))
    if status != TransactionStatus.Committed:
        # Revit rolled the whole transaction back (an error-level failure while regenerating)
        return results, u'Revit did not commit the transaction ({0}) - NOTHING was created.'.format(status)
    return results, None


# ============================================================================ 4. the window
class PanelRow(object):
    """One panel in the window's list."""

    def __init__(self, panel, schedules, duplicate_name):
        self.panel = panel
        self.name = panel_name(panel)
        self.type = type_label(panel)
        self.kind = panel_kind(panel)
        self.schedules = schedules                    # [] = no schedule yet
        self.duplicate_name = duplicate_name
        self.box = None

    @property
    def has_schedule(self):
        return bool(self.schedules)

    def search_text(self):
        return u'{0} {1} {2}'.format(self.name, self.type, KIND_LABEL[self.kind]).lower()


def _window_class():
    from System.Windows import Thickness, Visibility, FontWeights
    from System.Windows.Controls import CheckBox, ColumnDefinition, Grid, TextBlock
    from System.Windows.Media import BrushConverter
    brushes = BrushConverter()
    MUTED = brushes.ConvertFromString('#8A94A3')
    WARN = brushes.ConvertFromString('#B45309')
    OK = brushes.ConvertFromString('#2E7D32')
    SHOW = [u'Panels without a schedule', u'All panels', u'Ticked panels only']

    class ScheduleWindow(forms.WPFWindow):
        def __init__(self, rows, templates, preticked):
            forms.WPFWindow.__init__(self, XAML)
            self.rows = rows
            self.templates = templates
            self.choice = None
            self._filling = True
            config = script.get_config('create_panel_schedules')
            self.config = config
            # ---- the list: one row per panel
            for row in rows:
                grid = Grid()
                for width in (2, 3, 0, 2):
                    col = ColumnDefinition()
                    col.Width = System_GridLength(width)
                    grid.ColumnDefinitions.Add(col)
                cells = [row.name + (u'   (same name as another panel)' if row.duplicate_name else u''),
                         row.type, KIND_LABEL[row.kind],
                         (u'has: ' + u', '.join(row.schedules)) if row.has_schedule else u'no schedule']
                for index, value in enumerate(cells):
                    block = TextBlock()
                    block.Text = value
                    block.TextTrimming = System_TextTrimming()
                    block.Margin = Thickness(0, 0, 8, 0)
                    block.ToolTip = value
                    if row.has_schedule:
                        block.Foreground = MUTED
                    elif index == 3:
                        block.Foreground = OK
                    if index == 0 and row.duplicate_name and not row.has_schedule:
                        block.Foreground = WARN
                    if index == 0:
                        block.FontWeight = FontWeights.SemiBold
                    Grid.SetColumn(block, index)
                    grid.Children.Add(block)
                box = CheckBox()
                box.Content = grid
                box.HorizontalContentAlignment = System_Stretch()
                box.Margin = Thickness(0, 3, 0, 3)
                box.IsEnabled = not row.has_schedule  # existing schedules are never touched
                box.IsChecked = (not row.has_schedule) and (row in preticked)
                box.Checked += self._ticked
                box.Unchecked += self._ticked
                row.box = box
                self.rows_sp.Children.Add(box)
            # ---- filters
            for text_ in SHOW:
                self.show_cb.Items.Add(text_)
            self.show_cb.SelectedIndex = 0
            self.search_tb.TextChanged += self._filter
            self.show_cb.SelectionChanged += self._filter
            self.all_btn.Click += lambda s, a: self._tick_shown(True)
            self.none_btn.Click += lambda s, a: self._tick_shown(False)
            # ---- templates: one list per kind, only the templates of that kind
            kinds_present = set(r.kind for r in rows if not r.has_schedule)
            for kind, _part, _stype, combo_name in KINDS:
                combo = getattr(self, combo_name + '_cb')
                combo.Items.Add(DEFAULT_TEMPLATE)
                for name, _tid in templates.get(kind, []):
                    combo.Items.Add(name)
                remembered = getattr(config, 'template_' + kind, None)
                names = [n for n, _i in templates.get(kind, [])]
                combo.SelectedIndex = (names.index(remembered) + 1) if remembered in names else 0
                count = sum(1 for r in rows if r.kind == kind and not r.has_schedule)
                getattr(self, combo_name + '_lbl').Text = u'{0}  ({1} without schedule)'.format(
                    {'branch': u'Branch panels (Panelboard)', 'switchboard': u'Switchboards',
                     'data': u'Data panels (Other Panel)'}[kind], count)
                getattr(self, combo_name + '_row').Visibility = \
                    Visibility.Visible if kind in kinds_present else Visibility.Collapsed
            self.create_btn.Click += lambda s, a: self._finish(False)
            self.report_btn.Click += lambda s, a: self._finish(True)
            self.close_btn.Click += lambda s, a: self.Close()
            self._filling = False
            self._filter(None, None)

        # ---- list behaviour
        def _ticked(self, sender, args):
            if self._filling:
                return
            if self.show_cb.SelectedIndex == 2:
                self._filter(None, None)
            else:
                self._count()

        def _filter(self, sender, args):
            words = (self.search_tb.Text or u'').lower().split()
            mode = self.show_cb.SelectedIndex
            for row in self.rows:
                show = all(w in row.search_text() for w in words)
                if mode == 0:
                    show = show and not row.has_schedule
                elif mode == 2:
                    show = show and bool(row.box.IsChecked)
                row.box.Visibility = Visibility.Visible if show else Visibility.Collapsed
            self._count()

        def _tick_shown(self, value):
            self._filling = True
            for row in self.rows:
                if row.box.IsEnabled and row.box.Visibility == Visibility.Visible:
                    row.box.IsChecked = value
            self._filling = False
            self._filter(None, None)

        def _count(self):
            ticked = sum(1 for r in self.rows if r.box.IsChecked)
            hidden = sum(1 for r in self.rows if r.box.IsChecked and r.box.Visibility != Visibility.Visible)
            without = sum(1 for r in self.rows if not r.has_schedule)
            text_ = u'{0} panel(s): {1} without schedule, {2} with schedule (skipped).   {3} ticked'.format(
                len(self.rows), without, len(self.rows) - without, ticked)
            if hidden:
                text_ += u'  ({0} of them hidden by the filter)'.format(hidden)
            self.count_tb.Text = text_
            self.create_btn.Content = u'CREATE {0} SCHEDULE(S)'.format(ticked) if ticked else u'CREATE SCHEDULES'

        # ---- the result
        def _finish(self, report_only):
            chosen = [r for r in self.rows if r.box.IsChecked and not r.has_schedule]
            if not chosen and not report_only:
                forms.alert(u'Tick at least one panel without a schedule.', title=TITLE)
                return
            template_ids, template_names = {}, {}
            for kind, _part, _stype, combo_name in KINDS:
                combo = getattr(self, combo_name + '_cb')
                index = combo.SelectedIndex
                items = self.templates.get(kind, [])
                if index > 0 and index - 1 < len(items):
                    template_names[kind], template_ids[kind] = items[index - 1]
                    setattr(self.config, 'template_' + kind, items[index - 1][0])
                else:
                    template_names[kind], template_ids[kind] = DEFAULT_TEMPLATE, None
                    setattr(self.config, 'template_' + kind, u'')
            try:
                script.save_config()
            except Exception:
                pass
            self.choice = {'panels': chosen, 'report_only': report_only,
                           'template_ids': template_ids, 'template_names': template_names}
            self.Close()

    return ScheduleWindow


def System_GridLength(stars):
    """Star width for the list columns (0 = fixed 110 px, the Kind column)."""
    from System.Windows import GridLength, GridUnitType
    return GridLength(110) if stars == 0 else GridLength(stars, GridUnitType.Star)


def System_TextTrimming():
    from System.Windows import TextTrimming
    return TextTrimming.CharacterEllipsis


def System_Stretch():
    from System.Windows import HorizontalAlignment
    return HorizontalAlignment.Stretch


# ============================================================================ 5. report
def report(rows, results, not_panels, transaction_error, choice):
    """The output window: summary, every panel with its result, equipment that is not a panel."""
    report_only = choice['report_only']
    created = [r for r in results if r[1] == 'created'] if not transaction_error else []
    errors = [r for r in results if r[1] == 'error']
    skipped = [r for r in rows if r.has_schedule]
    chosen = set(id(r.panel) for r in choice['panels'])

    output.print_md(u'## {0}'.format(TITLE))
    lines = [
        (u'Panels found', len(rows)),
        (u'Existing schedules - skipped', len(skipped)),
        (u'Panels ticked', len(choice['panels'])),
        (u'New schedules created', len(created) if not report_only else u'0  (report only)'),
        (u'Errors', len(errors) + (1 if transaction_error else 0)),
        (u'Electrical Equipment that is not a panel (ignored)', len(not_panels)),
    ]
    safetable.print_table(output, [[name, value] for name, value in lines], columns=[u'', u'Count'])
    templates = [[KIND_LABEL[k], choice['template_names'].get(k, DEFAULT_TEMPLATE)] for k, _p, _s, _c in KINDS
                 if any(r.kind == k for r in choice['panels'])]
    if templates:
        safetable.print_table(output, templates, columns=[u'Kind of panel', u'Template used'], title=u'Templates')
    if transaction_error:
        output.print_md(u'**{0}**'.format(transaction_error))

    result_of = dict((id(p), (status, detail)) for p, status, detail in results)
    table = []
    for row in rows:
        link = output.linkify(row.panel.Id, row.name)
        if row.has_schedule:
            table.append([link, row.type, u'SKIPPED', u'has: ' + u', '.join(row.schedules)])
        elif id(row.panel) in result_of:
            status, detail = result_of[id(row.panel)]
            table.append([link, row.type, u'CREATED' if status == 'created' and not transaction_error else u'ERROR', detail])
        elif id(row.panel) in chosen:
            table.append([link, row.type, u'WOULD CREATE',
                          choice['template_names'].get(row.kind, DEFAULT_TEMPLATE)])
        else:
            table.append([link, row.type, u'NOT TICKED', u'no schedule'])
    safetable.print_table(output, table, columns=[u'Panel', u'Family : Type', u'Result', u'Schedule / template / message'],
                          title=u'Panels')
    if not_panels:
        safetable.print_table(output, [[output.linkify(e.Id, panel_name(e)), type_label(e), part_type_name(part_type(e))]
                                       for e in not_panels],
                              columns=[u'Equipment', u'Family : Type', u'Part Type'],
                              title=u'Electrical Equipment that is not a panel (no schedule possible)', limit=200)
    return created, errors, skipped


# ============================================================================ main
def main():
    doc = revit.doc
    if doc is None or doc.IsFamilyDocument:
        forms.alert(u'Open a project first.', title=TITLE)
        return
    trace(u'{0} - {1}'.format(TITLE, doc.Title), new=True)

    # ---- 1 + 2: the panels and the schedules they already have
    panels, not_panels = collect_panels(doc)
    if not panels:
        forms.alert(u'No electrical panel found (Electrical Equipment with Part Type Panelboard, '
                    u'Switchboard or Other Panel).', title=TITLE)
        return
    have = existing_schedules(doc)
    names = {}
    for panel in panels:
        names.setdefault(panel_name(panel).lower(), []).append(panel)
    rows = [PanelRow(p, have.get(eid_int(p.Id), []), len(names[panel_name(p).lower()]) > 1) for p in panels]
    templates = templates_by_kind(doc)
    trace(u'panels {0}, with schedule {1}, not panels {2}; templates: {3}'.format(
        len(rows), sum(1 for r in rows if r.has_schedule), len(not_panels),
        u', '.join(u'{0} {1}'.format(len(v), k) for k, v in sorted(templates.items()))))
    kinds = {}
    for row in rows:
        kinds[row.kind] = kinds.get(row.kind, 0) + 1
    trace(u'panel kinds: ' + u', '.join(u'{0} {1}'.format(n, k) for k, n in sorted(kinds.items())))
    for template in FilteredElementCollector(doc).OfClass(PanelScheduleTemplate):
        try:
            trace(u'  template "{0}"  type={1}  default={2}'.format(
                text(template.Name), text(template.GetPanelScheduleType()), getattr(template, 'IsDefault', u'?')))
        except Exception as error:
            trace(u'  template ? ({0})'.format(error))

    # pre-ticked: the panels selected in Revit; with no panel selected, every panel without a schedule
    selected = set(eid_int(i) for i in revit.uidoc.Selection.GetElementIds())
    sel_rows = [r for r in rows if eid_int(r.panel.Id) in selected and not r.has_schedule]
    preticked = sel_rows if sel_rows else [r for r in rows if not r.has_schedule]

    # ---- 3: the window - the user ticks the panels and picks the templates
    trace(u'opening the window ({0} pre-ticked)'.format(len(preticked)))
    window = _window_class()(rows, templates, preticked)
    trace(u'window built')
    window.ShowDialog()
    choice = window.choice
    if not choice:
        trace(u'closed without creating')
        return
    trace(u'{0}: {1} panel(s); templates {2}'.format(
        u'REPORT ONLY' if choice['report_only'] else u'CREATE', len(choice['panels']),
        u', '.join(u'{0}={1}'.format(k, v) for k, v in sorted(choice['template_names'].items()))))

    # ---- 4: create
    if choice['report_only'] or not choice['panels']:
        results, transaction_error = [], None
    else:
        results, transaction_error = create_schedules(doc, [r.panel for r in choice['panels']],
                                                      choice['template_ids'])
    if transaction_error:
        trace(u'TRANSACTION: ' + transaction_error)

    # ---- 5: report + completion message
    created, errors, skipped = report(rows, results, not_panels, transaction_error, choice)
    trace(u'done: {0} created, {1} errors, {2} skipped'.format(len(created), len(errors), len(skipped)))
    message = (u'Panels found: {0}\nExisting schedules skipped: {1}\nPanels ticked: {2}\n'
               u'New schedules created: {3}\nErrors: {4}').format(
        len(rows), len(skipped), len(choice['panels']), len(created), len(errors) + (1 if transaction_error else 0))
    if choice['report_only']:
        message += u'\n\nReport only - nothing was changed.'
    elif created:
        message += u'\n\nOne Undo (Ctrl+Z) removes all new schedules.'
    forms.alert(message, sub_msg=u'Details and clickable panel ids are in the output window.', title=TITLE)


if __name__ == '__main__':
    try:
        main()
    except Exception as _error:
        # never a silent stop: the full error goes to the trace log ("check logs") and to the user
        import traceback
        trace(u'STOPPED BY ERROR: {0}\n{1}'.format(_error, traceback.format_exc()))
        forms.alert(u'{0} stopped: {1}'.format(TITLE, _error),
                    sub_msg=u'Details: %APPDATA%\\pyRevit\\CreatePanelSchedules_trace.log', title=TITLE)


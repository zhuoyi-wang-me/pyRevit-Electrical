# -*- coding: utf-8 -*-
"""Breaker Check - the window.

Shows every circuit with its load current and breaker rating. Where they do
not match, a new rating from YOUR list is proposed in the column
"New breaker A" (you can type another value). Nothing is changed in the model
until "Update ticked breakers..." is pressed and confirmed.
"""
import codecs
import json
import os
import traceback

import clr
# the WPF libraries must be referenced before "System.Windows" can be imported
for _name in ('PresentationFramework', 'PresentationCore', 'WindowsBase', 'System.Xaml', 'System.Data'):
    clr.AddReference(_name)

from System import Boolean, Int32, String
from System.Data import DataTable
from System.Windows.Controls import DataGridEditingUnit, DataGridLength, DataGridLengthUnitType, DataGridTemplateColumn
from System.Windows.Media import BrushConverter

from pyrevit import forms, revit

import bc_check
import bc_panels
import bc_revit
import bc_rules
import bc_template

SETTINGS = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'EleTools', 'BreakerCheck', 'settings.json')
TEXT_COLUMNS = ['Panel', 'Circuit', 'Load name', 'Load type', 'Poles', 'Load W', 'Voltage V', 'Current A', 'Required A', 'Breaker A', 'Used', 'Result']
NEW, REMARK, TICK, KEY = 'New breaker A', 'Remark', 'Update', 'Key'
# the three cells that are drawn by a template of ui.xaml: column -> (template, width)
TEMPLATES = {TICK: ('TickCell', 64), 'Result': ('ResultCell', 124), NEW: ('NewCell', 112)}
RED, DARK, GREEN = '#C42B1C', '#1B1B1B', '#0F7B0F'
# the message bar, as the Windows 11 info bar: text colour -> (background, border)
BAR = {RED: ('#FDE7E9', '#F3C6CA'), GREEN: ('#DFF6DD', '#BFE5BC'), DARK: ('#F9F9F9', '#E5E5E5')}


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
        pass                                               # remembering the values is only a convenience


def _fmt(value, pattern):
    return pattern.format(float(value)) if value is not None else u''


class CheckWindow(forms.WPFWindow):
    def __init__(self, panels):
        forms.WPFWindow.__init__(self, os.path.join(os.path.dirname(__file__), 'ui.xaml'))
        self.doc = revit.doc
        self.panels = panels                               # Revit elements
        self.lines = []
        self.table = None
        self.select_ids = []                               # circuits to select in Revit after closing
        self._brushes = BrushConverter()
        settings = _load_settings()
        # as the office calculation sheet: power factor 0.95, breaker at least 1.25 x the current
        # (settings saved before the power factor existed are not used for the margin)
        self.txtPf.Text = u'{0}'.format(settings.get('pf', u'0.95'))
        self.txtMargin.Text = u'{0}'.format(settings.get('margin', u'1.25') if 'pf' in settings else u'1.25')
        # the ratings to choose from: the user's own list; the first time, the ratings already used in this model
        self.txtRatings.Text = settings.get('ratings') or u', '.join(u'{0:g}'.format(r) for r in bc_revit.model_ratings(self.doc))
        self.rules = bc_template.rules_from(settings)      # breaker rating per load type, as the project requires
        self.grid.AutoGeneratingColumn += self.on_column
        self.btnPanels.Click += self._guard(self.on_panels)
        self.btnRules.Click += self._guard(self.on_rules)
        self.btnOpenTemplate.Click += self._guard(self.on_open_template)
        self.btnSaveTemplate.Click += self._guard(self.on_save_template)
        self.btnRecheck.Click += self._guard(self.refresh)
        self.chkProblems.Click += self._guard(self.show)
        self.btnTick.Click += self._guard(lambda: self.tick(True))
        self.btnUntick.Click += self._guard(lambda: self.tick(False))
        self.btnSelect.Click += self._guard(self.on_select)
        self.btnExport.Click += self._guard(self.on_export)
        self.btnUpdate.Click += self._guard(self.on_update)
        self.btnClose.Click += self._guard(lambda: self.Close())
        self._guard(self.refresh)(None, None)

    # ------------------------------------------------------------------
    def _guard(self, action):
        """An event handler that shows an error instead of failing silently."""
        def handler(sender, args):
            try:
                action()
            except Exception:
                self.say(traceback.format_exc(), RED)
        return handler

    def say(self, text, colour=DARK):
        self._colour = colour
        self.txtMessages.Text = text
        self.txtMessages.Foreground = self._brushes.ConvertFromString(colour)
        background, border = BAR.get(colour, BAR[DARK])
        self.barMessages.Background = self._brushes.ConvertFromString(background)
        self.barMessages.BorderBrush = self._brushes.ConvertFromString(border)

    def on_panels(self):
        "Choose other panels without leaving the window."
        found = bc_revit.all_panels(self.doc)
        names = bc_panels.choose(found.keys(), [bc_revit.panel_name(p) for p in self.panels], self)
        if names:
            self.panels = [found[name] for name in names]
            self.refresh()

    # ------------------------------------------------------------------
    # load types and the template
    # ------------------------------------------------------------------
    def template(self):
        return bc_template.build(self.txtPf.Text, self.txtMargin.Text, self.txtRatings.Text, self.rules)

    def found_classifications(self):
        """[(classification, number of circuits)] of the circuits in the table, most frequent first."""
        counts = {}
        for line in self.lines:
            name = (line.classification or u'').strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    def on_rules(self):
        rules = bc_rules.edit(self.rules, self.found_classifications(), self)
        if rules is not None:
            self.rules = rules
            self.refresh()

    def on_save_template(self):
        if self.inputs() is None:
            return
        path = forms.save_file(file_ext='json', default_name='breaker_check_template.json')
        if path:
            bc_template.save(path, self.template())
            self.say(u'Template saved: {0}'.format(path), GREEN)

    def on_open_template(self):
        path = forms.pick_file(file_ext='json', title='Breaker Check template')
        if not path:
            return
        try:
            data = bc_template.load(path)
        except bc_template.TemplateError as error:
            self.say(u'{0}'.format(error), RED)
            return
        self.txtPf.Text = u'{0}'.format(data.get('power_factor') or self.txtPf.Text)
        self.txtMargin.Text = u'{0}'.format(data.get('margin') or self.txtMargin.Text)
        self.txtRatings.Text = u'{0}'.format(data.get('ratings') or self.txtRatings.Text)
        self.rules = bc_template.rules_from(data)
        self.refresh()
        self.say(u'Template opened: {0}\n'.format(path) + self.txtMessages.Text, self._colour)

    def inputs(self):
        """(margin, ratings, power factor) typed by the user, or None after showing what is wrong."""
        try:
            margin = bc_check.parse_number(self.txtMargin.Text)
            if margin is None or margin < 1:
                raise ValueError()
        except ValueError:
            self.say(u'"Breaker at least ... x load current" must be a number of 1 or more, e.g. 1 or 1.25.', RED)
            return None
        try:
            ratings = bc_check.parse_ratings(self.txtRatings.Text)
        except ValueError:
            self.say(u'The ratings must be numbers separated by commas, e.g. 16, 20, 25, 32.', RED)
            return None
        try:
            pf = bc_check.parse_number(self.txtPf.Text)
            if pf is None or pf <= 0 or pf > 1:
                raise ValueError()
        except ValueError:
            self.say(u'The power factor must be a number above 0 and not above 1, e.g. 0.95.', RED)
            return None
        return margin, ratings, pf

    # ------------------------------------------------------------------
    # read and check: nothing is changed in the model
    # ------------------------------------------------------------------
    def refresh(self):
        values = self.inputs()
        if values is None:
            return
        margin, ratings, pf = values
        settings = self.template()                             # what is in use is remembered for the next time
        settings.update({'margin': self.txtMargin.Text, 'pf': self.txtPf.Text})
        _save_settings(settings)
        self.btnRules.Content = u'Load types ({0})...'.format(len(self.rules))
        self.lines = bc_check.sort_lines([line.check(margin, ratings, pf, self.rules) for line in bc_revit.read(self.panels)])
        names = sorted(bc_revit.panel_name(p) for p in self.panels)
        # one line only, however many panels: the names are in the tooltip
        self.lblPanels.Text = (names[0] if len(names) == 1 else u'{0} panels'.format(len(names))) + u'   |   {0} circuits'.format(len(self.lines))
        self.lblPanels.ToolTip = u', '.join(names[:60]) + (u' ... and {0} more'.format(len(names) - 60) if len(names) > 60 else u'')
        self.show()
        mismatch = len([l for l in self.lines if l.result in bc_check.MISMATCH])
        if not self.lines:
            self.say(u'The chosen panels have no power circuits.', RED)
        elif mismatch:
            self.say(u'{0} breaker(s) do not match. A new rating is proposed and ticked where your list has one. '
                     u'Nothing is changed until you press "Update ticked breakers...".'.format(mismatch), RED)
        else:
            self.say(u'Every breaker matches its load type and its load.' if (ratings or self.rules) else u'Every breaker rating is enough for its load.', GREEN)
        if not ratings:
            self.say(self.txtMessages.Text + u'\nEnter the ratings to choose from: without them no new rating can be proposed and oversized breakers cannot be found.', RED)

    def show(self):
        """Fill the table from self.lines."""
        counts = bc_check.counts(self.lines)
        self.numUndersized.Text = u'{0}'.format(counts.get(bc_check.UNDERSIZED, 0))
        self.numNoRating.Text = u'{0}'.format(counts.get(bc_check.NO_RATING, 0))
        self.numOversized.Text = u'{0}'.format(counts.get(bc_check.OVERSIZED, 0))
        self.numOk.Text = u'{0}'.format(counts.get(bc_check.OK, 0))
        self.numOther.Text = u'{0}'.format(len(self.lines) - sum(counts.get(k, 0) for k in bc_check.MISMATCH + (bc_check.OK,)))
        table = DataTable()
        table.Columns.Add(TICK, clr.GetClrType(Boolean))
        for name in TEXT_COLUMNS:
            table.Columns.Add(name, clr.GetClrType(String)).ReadOnly = True
        table.Columns.Add(NEW, clr.GetClrType(String))          # the only text the user can edit
        table.Columns.Add(REMARK, clr.GetClrType(String)).ReadOnly = True
        table.Columns.Add(KEY, clr.GetClrType(Int32))           # which line of self.lines; not shown
        only_problems = bool(self.chkProblems.IsChecked)
        for index, line in enumerate(self.lines):
            if only_problems and line.result in (bc_check.OK, bc_check.NO_LOAD):
                continue
            row = table.NewRow()
            row[TICK] = line.proposed is not None
            row['Panel'], row['Circuit'], row['Load name'] = line.panel, line.number, line.name
            row['Poles'] = u'{0}'.format(line.poles)
            row['Load type'] = u'{0}  min {1:g} A'.format(line.rule.text, line.rule.rating_a) if line.rule is not None else u''
            row['Load W'], row['Voltage V'] = _fmt(line.load_w, u'{0:.0f}'), _fmt(line.voltage_v, u'{0:.0f}')
            row['Current A'] = _fmt(line.current_a, u'{0:.1f}')
            row['Required A'] = _fmt(line.required_a, u'{0:.1f}')
            row['Breaker A'] = _fmt(line.rating_a or None, u'{0:g}')
            row['Used'] = _fmt(line.used, u'{0:.0f} %')
            row['Result'] = line.result
            row[NEW] = _fmt(line.proposed, u'{0:g}')
            row[REMARK] = u'; '.join(t for t in (line.outcome, line.remark) if t)
            row[KEY] = index
            table.Rows.Add(row)
        self.table = table
        self.grid.ItemsSource = table.DefaultView

    def on_column(self, sender, args):
        name = args.PropertyName
        if name == KEY:
            args.Cancel = True                                 # the row key is not for the user
        elif name in TEMPLATES:                                # tick, result pill and the field for the new rating
            key, width = TEMPLATES[name]
            column = DataGridTemplateColumn()
            column.Header = name
            column.CellTemplate = self.FindResource(key)
            column.SortMemberPath = name
            column.Width = DataGridLength(width)
            args.Column = column
        elif name in ('Load name', REMARK):                    # these two share the space that is left
            args.Column.Width = DataGridLength(1, DataGridLengthUnitType.Star)
            args.Column.MinWidth = 90

    def commit_edits(self):
        """Take over a value the user is still typing."""
        try:
            self.grid.CommitEdit(DataGridEditingUnit.Row, True)
        except Exception:
            pass

    def tick(self, state):
        self.commit_edits()
        for row in self.table.Rows:
            row[TICK] = bool(state) and bool(u'{0}'.format(row[NEW]).strip())

    # ------------------------------------------------------------------
    def chosen_lines(self):
        keys = []
        for item in self.grid.SelectedItems:
            try:
                keys.append(int(item[KEY]))
            except Exception:
                pass
        return [self.lines[key] for key in keys]

    def on_select(self):
        lines = self.chosen_lines()
        if not lines:
            self.say(u'Click one or more rows first (Ctrl or Shift for several).', RED)
            return
        self.select_ids = [line.element_id for line in lines if line.element_id is not None]
        self.Close()

    def on_export(self):
        path = forms.save_file(file_ext='csv', default_name='breaker_check.csv')
        if not path:
            return
        self.commit_edits()
        names = TEXT_COLUMNS + [NEW, REMARK]

        def cell(value):
            text = u'{0}'.format(value)
            return u'"' + text.replace(u'"', u'""') + u'"' if any(ch in text for ch in u',"\r\n') else text
        with codecs.open(path, 'w', 'utf-8-sig') as handle:
            handle.write(u','.join(names) + u'\r\n')
            for row in self.table.Rows:
                handle.write(u','.join(cell(row[name]) for name in names) + u'\r\n')
        self.say(u'Saved: {0}'.format(path))

    # ------------------------------------------------------------------
    # update: the only place where the model is changed, after a question
    # ------------------------------------------------------------------
    def on_update(self):
        values = self.inputs()
        if values is None:
            return
        margin = values[0]
        self.commit_edits()
        changes, refused = [], []
        for row in self.table.Rows:
            if not row[TICK]:
                continue
            line = self.lines[int(row[KEY])]
            label = u'{0} / {1}'.format(line.panel, line.number)
            try:
                rating = bc_check.parse_number(row[NEW])
            except ValueError:
                refused.append(u'{0}: "{1}" is not a number'.format(label, row[NEW]))
                continue
            if rating is None or rating <= 0:
                refused.append(u'{0}: no new rating'.format(label))
            elif line.current_a is not None and line.current_a * margin > rating + 1e-9:
                refused.append(u'{0}: {1:g} A is still below the required {2:.1f} A'.format(label, rating, line.current_a * margin))
            elif line.rating_a and abs(line.rating_a - rating) < 1e-9:
                refused.append(u'{0}: already {1:g} A'.format(label, rating))
            else:
                changes.append((line, rating))
        if not changes:
            self.say(u'Nothing to update.' + (u'\nNot used:\n' + u'\n'.join(refused) if refused else u' Tick the rows to update.'), RED)
            return
        listing = [u'{0} / {1}:  {2}  ->  {3:g} A'.format(l.panel, l.number, _fmt(l.rating_a or None, u'{0:g} A') or u'no rating', r) for l, r in changes]
        more = u'\n... and {0} more'.format(len(listing) - 15) if len(listing) > 15 else u''
        question = (u'Change the breaker rating of {0} circuit(s) in the model?\n\n{1}{2}\n\n'
                    u'Check that cable sizes still suit the new ratings.').format(len(changes), u'\n'.join(listing[:15]), more)
        if not forms.alert(question, title='Breaker Check', yes=True, no=True):
            self.say(u'Nothing changed.')
            return
        counts = bc_revit.write(self.doc, changes)
        outcomes = dict((line.element_id, line.outcome) for line, _ in changes)
        self.refresh()                                         # read the model again: the table shows what is in it now
        for line in self.lines:
            line.outcome = outcomes.get(line.element_id, u'')
        self.show()
        text = u'Update finished.  ' + u',  '.join(u'{0}: {1}'.format(k, v) for k, v in sorted(counts.items()))
        if refused:
            text += u'\nNot used:\n' + u'\n'.join(refused)
        failed = [k for k in counts if k != bc_revit.WRITTEN]
        self.say(text, RED if (failed or refused) else GREEN)


def main(panels):
    window = CheckWindow(panels)
    window.ShowDialog()
    return window.select_ids

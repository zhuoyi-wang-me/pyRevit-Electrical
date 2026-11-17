# -*- coding: utf-8 -*-
"""
Electrical Calculator - modeless WPF window.

The calculation tabs are pure maths, so the window can stay open while the user
keeps working in Revit. The "From model" tab reads the loads of selected
elements: because the window is modeless, those Revit calls are queued through
an ExternalEvent (revit_loads.ApiCall) and run when Revit is ready. Read-only -
the model is never changed. Results are recalculated live on every keystroke.

Only one window exists per Revit session: clicking the button again brings the
open window to the front instead of opening a second one.
"""
import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
import System
from System.Windows import Clipboard, WindowState
from System.Windows.Media import Brushes
from System.Windows.Controls import TextChangedEventHandler
from System.Windows.Controls.Primitives import TextBoxBase
clr.AddReference('System.Data')
from System.Data import DataTable
from pyrevit import forms

from eleccalc import calc

_INSTANCE_KEY = 'EleTools.ElectricalCalculator.Window'
_SUPPLY = [(u'Auto (from elements)', 'auto'), (u'1-phase', 1), (u'3-phase', 3)]
_MOTOR_MODES = [u'Auto-detect (Mechanical Equipment / classification)', u'Treat all as motors',
                u'No motor loads']
_ISO_COLS = [('Id', 'System.Int64'), ('Family Type', 'System.String'), ('Load Classification', 'System.String'),
             ('VA', 'System.Double'), ('V', 'System.Double'), ('Ib (A)', 'System.Double'),
             ('Required (A)', 'System.Double'), ('Isolator (A)', 'System.String'), ('Poles', 'System.String'),
             ('Duty', 'System.String'), ('Fixture', 'System.String'), ('Match', 'System.String'),
             ('Written', 'System.String'), ('Note', 'System.String')]
_DEMAND_MODES = [u'From load classification (Revit demand factors)', u'Manual - one factor for all']
_GRID_COLS = [('Id', 'System.Int64'), ('Category', 'System.String'), ('Family Type', 'System.String'),
              ('Load Classification', 'System.String'),
              ('VA', 'System.Double'), ('V', 'System.Double'), ('Poles', 'System.Int32'),
              ('PF', 'System.String'), ('Source', 'System.String')]
_PHASES = [(u'1-phase', 1, 230.0), (u'3-phase', 3, 400.0)]
_UNITS = ['kW', 'kVA', 'A']
_INSULATION = [(u'XLPE 90°C (Cu SWA)', 'XLPE'), (u'PVC 70°C (Cu SWA)', 'PVC')]
_MANUAL = u'Manual mV/A/m'


# ------------------------------------------------------------- formatting
def _num(text):
    """Text box -> float. Blank -> None. Accepts ',' as decimal separator."""
    text = (text or '').strip().replace(',', '.')
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        raise calc.CalcError(u'"{0}" is not a number.'.format(text))


def _num_or(text, default):
    """Like _num, but blank -> default (a typed 0 is kept and rejected by calc)."""
    value = _num(text)
    return default if value is None else value


def _size_label(size):
    return u'{0:g}'.format(size)


def _lines(rows):
    """[(label, value)] -> aligned text block."""
    width = max(len(l) for l, _ in rows)
    return u'\n'.join(u'{0}  {1}'.format(l.ljust(width), v) if l else v for l, v in rows)


# ------------------------------------------------------------------ window
class ElectricalCalculatorWindow(forms.WPFWindow):

    def __init__(self, xaml_file):
        # handle_esc=False: Esc must not close a window that is meant to stay open
        forms.WPFWindow.__init__(self, xaml_file, handle_esc=False)
        self._busy = False
        self.model_items = []
        self._init_revit_bridge()
        self._fill_combos()
        self._wire_events()
        self._wire_model_tab()
        self._wire_iso_tab()
        self.Closed += self._on_closed
        self.vd_mv.IsReadOnly = True                       # table value until "Manual" is chosen
        self.vd_mv.Background = Brushes.WhiteSmoke
        self.recalc_all()

    # ------------------------------------------------------------ set-up
    def _fill_combos(self):
        for combo in (self.lc_phase, self.vd_phase, self.cs_phase, self.pf_phase):
            for label, _, _ in _PHASES:
                combo.Items.Add(label)
            combo.SelectedIndex = 1                       # 3-phase 400 V default
        for u in _UNITS:
            self.lc_unit.Items.Add(u)
        self.lc_unit.SelectedIndex = 0
        for label, _ in _INSULATION:
            self.vd_insulation.Items.Add(label)
            self.cs_insulation.Items.Add(label)
        self.vd_insulation.Items.Add(_MANUAL)
        self.vd_insulation.SelectedIndex = 0
        self.cs_insulation.SelectedIndex = 0
        self._fill_vd_sizes(select=10)

    def _fill_vd_sizes(self, select=None):
        key = self._vd_key()
        current = select if select is not None else self._vd_size()
        self.vd_size.Items.Clear()
        sizes = calc.cable_sizes(key or 'XLPE')
        for s in sizes:
            self.vd_size.Items.Add(_size_label(s))
        idx = sizes.index(current) if current in sizes else 0
        self.vd_size.SelectedIndex = idx

    def _wire_events(self):
        tabs = {
            self.recalc_load: (['lc_volts', 'lc_value', 'lc_pf'], ['lc_unit']),
            self.recalc_vd: (['vd_volts', 'vd_current', 'vd_length', 'vd_mv', 'vd_limit'], ['vd_size']),
            self.recalc_cable: (['cs_volts', 'cs_ib', 'cs_length', 'cs_ca', 'cs_cg', 'cs_ci',
                                 'cs_vdlimit', 'cs_breaker'], ['cs_insulation']),
            self.recalc_pf: (['pf_volts', 'pf_kw', 'pf_now', 'pf_target'], []),
            self.recalc_sc: (['sc_kva', 'sc_volts', 'sc_z', 'sc_source'], []),
        }
        for func, (texts, combos) in tabs.items():
            for name in texts:
                getattr(self, name).TextChanged += self._handler(func)
            for name in combos:
                getattr(self, name).SelectionChanged += self._handler(func)

        # phase selectors also swap the default voltage (230 <-> 400)
        for combo, volts, func in ((self.lc_phase, self.lc_volts, self.recalc_load),
                                   (self.vd_phase, self.vd_volts, self.recalc_vd),
                                   (self.cs_phase, self.cs_volts, self.recalc_cable),
                                   (self.pf_phase, self.pf_volts, self.recalc_pf)):
            combo.SelectionChanged += self._phase_handler(combo, volts, func)

        self.vd_insulation.SelectionChanged += self._on_vd_insulation
        self.vd_size.SelectionChanged += self._on_vd_size
        self.cb_topmost.Checked += self._on_topmost
        self.cb_topmost.Unchecked += self._on_topmost
        self.btn_copy.Click += self._on_copy
        self.btn_close.Click += lambda s, a: self.Close()

    def _handler(self, func):
        def handler(sender, args):
            if not self._busy:
                func()
        return handler

    def _phase_handler(self, combo, volts_box, func):
        def handler(sender, args):
            if self._busy:
                return
            new_default = _PHASES[max(0, combo.SelectedIndex)][2]
            if (volts_box.Text or '').strip() in ('', '230', '400'):     # only replace a default value
                self._busy = True
                volts_box.Text = u'{0:g}'.format(new_default)
                self._busy = False
            if combo is self.vd_phase:
                self._update_vd_mv()
            func()
        return handler

    # ---------------------------------------------------------- reading
    @staticmethod
    def _phase(combo):
        return _PHASES[max(0, combo.SelectedIndex)][1]

    def _vd_key(self):
        idx = self.vd_insulation.SelectedIndex
        return _INSULATION[idx][1] if 0 <= idx < len(_INSULATION) else None   # None = manual

    def _vd_size(self):
        item = self.vd_size.SelectedItem
        return float(item) if item else None

    def _show(self, box, func):
        """Run a calculation; show its text, or the input problem instead."""
        try:
            box.Text = func()
            box.Foreground = Brushes.Black
        except calc.CalcError as ex:
            box.Text = u'{0}'.format(ex)
            box.Foreground = Brushes.DarkRed
        except Exception as ex:                            # never let a typo kill the window
            box.Text = u'Error: {0}'.format(ex)
            box.Foreground = Brushes.DarkRed

    # ------------------------------------------ 0 from model (Revit selection)
    def _init_revit_bridge(self):
        """ExternalEvent used to read the model from this modeless window.
        Created here because the window is built while the button's script runs,
        i.e. inside a valid API context (ExternalEvent.Create requires one)."""
        self._api = None
        self._api_problem = None
        self._last_model = None
        try:
            from Autodesk.Revit.UI import ExternalEvent
            from eleccalc import revit_loads
            self._rl = revit_loads
            self._api_handler = revit_loads.ApiCall()
            self._api_handler.on_error = self._on_api_error
            self._api = ExternalEvent.Create(self._api_handler)
        except Exception as ex:                            # calculators still work without it
            self._api_problem = ex

    def _wire_model_tab(self):
        for label, _ in _SUPPLY:
            self.md_supply.Items.Add(label)
        self.md_supply.SelectedIndex = 0
        self.md_supply.SelectionChanged += self._handler(self.recalc_model)
        for label in _DEMAND_MODES:
            self.md_demand_mode.Items.Add(label)
        self.md_demand_mode.SelectedIndex = 0
        self.md_demand_mode.SelectionChanged += self._on_demand_mode
        self.demand_defs = {}
        for box in (self.md_volts, self.md_demand, self.md_pf):
            box.TextChanged += self._handler(self.recalc_model)
        self.btn_use_selection.Click += self._on_use_selection
        self.btn_pick.Click += self._on_pick
        self.btn_show_in_model.Click += self._on_show_in_model
        self.btn_clear_model.Click += self._on_clear_model
        self.btn_to_cable.Click += self._on_to_cable
        self.btn_to_load.Click += self._on_to_load

    def _run_in_revit(self, action, waiting_text):
        """Queue action(uiapp) - Revit runs it as soon as it is idle."""
        if self._api is None:
            self.md_result.Text = u'Reading the model is not available: {0}'.format(self._api_problem)
            return
        self._api_handler.action = action
        self.md_result.Foreground = Brushes.Black
        self.md_result.Text = waiting_text
        self._api.Raise()

    def _on_api_error(self, ex):
        self.Show()
        self.md_result.Foreground = Brushes.DarkRed
        self.md_result.Text = u'Revit error: {0}'.format(ex)

    @staticmethod
    def _active_uidoc(uiapp):
        uidoc = uiapp.ActiveUIDocument
        if uidoc is None or uidoc.Document.IsFamilyDocument:
            raise calc.CalcError('Open a Revit project first.')
        return uidoc

    def _classification_mode(self):
        return self.md_demand_mode.SelectedIndex != 1

    def _on_demand_mode(self, sender, args):
        self.md_demand_label.Text = (u'Fallback demand (%)' if self._classification_mode()
                                     else u'Demand factor (%)')
        self.recalc_model()

    def _read_demand_defs(self, doc):
        try:
            self.demand_defs = self._rl.demand_definitions(doc)
        except Exception as ex:
            self.demand_defs = {}
            self.txt_status.Text = u'Demand factors not readable: {0}'.format(ex)

    def _on_use_selection(self, sender, args):
        def action(uiapp):
            try:
                uidoc = self._active_uidoc(uiapp)
            except calc.CalcError as ex:
                self.md_result.Text = u'{0}'.format(ex)
                return
            items, ignored = self._rl.read_selection(uidoc)
            self._read_demand_defs(uidoc.Document)
            self._set_model(items, ignored)
            self._fill_iso_params(uidoc.Document, items)
        self._run_in_revit(action, u'Reading the Revit selection...')

    def _on_pick(self, sender, args):
        def action(uiapp):
            try:
                uidoc = self._active_uidoc(uiapp)
            except calc.CalcError as ex:
                self.md_result.Text = u'{0}'.format(ex)
                return
            self.Hide()                                    # free the view for picking
            try:
                elements = self._rl.pick_loads(uidoc)
            finally:
                self.Show()
                self.Activate()
            if elements is None:                           # Esc
                self.txt_status.Text = u'Pick cancelled'
                self.recalc_model()
                return
            items, ignored = self._rl.read_elements(uidoc.Document, elements)
            self._read_demand_defs(uidoc.Document)
            self._set_model(items, ignored)
            self._fill_iso_params(uidoc.Document, items)
        self._run_in_revit(action, u'Pick electrical loads in the view, then click Finish (Esc = cancel).')

    def _on_show_in_model(self, sender, args):
        ids = [i['id'] for i in self.model_items]
        if not ids:
            return

        def action(uiapp):
            self._rl.select_ids(self._active_uidoc(uiapp), ids)
            self.txt_status.Text = u'{0} element(s) selected in Revit'.format(len(ids))
            self.recalc_model()
        self._run_in_revit(action, u'Selecting in Revit...')

    def _on_clear_model(self, sender, args):
        self._set_model([], 0)

    def _set_model(self, items, ignored):
        self.model_items = items
        self._ignored = ignored
        table = DataTable()
        for name, type_name in _GRID_COLS:
            table.Columns.Add(name, System.Type.GetType(type_name))
        for i in items:
            row = table.NewRow()
            row['Id'] = i['id']
            row['Category'] = i['category']
            row['Family Type'] = i['name']
            row['Load Classification'] = (u'(panel - own demand)' if i.get('demand_va') is not None
                                          else i.get('classification') or u'-')
            row['VA'] = round(i['va'] or 0.0, 1)
            row['V'] = round(i['volts'] or 0.0, 1)
            row['Poles'] = int(i['poles'] or 1)
            row['PF'] = u'{0:.2f}'.format(i['pf']) if i.get('pf') else u'default'
            row['Source'] = i['source']
            table.Rows.Add(row)
        self.md_grid.ItemsSource = table.DefaultView
        self.txt_status.Text = u''
        self._iso_matches, self._iso_status = {}, {}      # new selection -> find fixtures again
        self.recalc_model()
        self.recalc_iso()

    def recalc_model(self):
        self._last_model = None
        if not self.model_items:
            ignored = getattr(self, '_ignored', 0)
            self.md_result.Foreground = Brushes.Black
            self.md_result.Text = (u'No electrical loads in the selection ({0} other element(s) ignored).'.format(ignored)
                                   if ignored else
                                   u"Select electrical fixtures or equipment in Revit, then click "
                                   u"'Use current selection' - or 'Pick in model...'.")
            return

        def run():
            by_class = self._classification_mode()
            r = calc.summarise_loads(self.model_items, _SUPPLY[max(0, self.md_supply.SelectedIndex)][1],
                                     _num(self.md_volts.Text), _num_or(self.md_demand.Text, 100.0),
                                     _num_or(self.md_pf.Text, 0.9),
                                     demand_defs=self.demand_defs if by_class else None)
            self._last_model = r
            counted = u'{0} element(s), {1} with load'.format(r['count'], r['with_load'])
            if getattr(self, '_ignored', 0):
                counted += u', {0} non-electrical ignored'.format(self._ignored)
            rows = [(u'Selection', counted),
                    (u'Connected load', u'{0:,.2f} kVA / {1:,.2f} kW  (pf {2:.2f})'.format(
                        r['connected_kva'], r['connected_kw'], r['pf'])),
                    (u'Demand load', u'{0:,.2f} kVA / {1:,.2f} kW  ({2:.0f} % overall)'.format(
                        r['demand_kva'], r['demand_kw'], r['demand_factor'] * 100)),
                    (u'Supply', u'{0}-phase {1:g} V'.format(r['phases'], r['volts'])),
                    (u'Design current Ib', u'{0:,.2f} A  (on demand load)'.format(r['ib'])),
                    (u'Next breaker', u'{0} A'.format(r['breaker'] or u'> 4000')),
                    (u'', u''),
                    (u'', u'Demand by load classification:' if by_class else u'Demand (manual):')]
            for g in r['by_classification']:
                rows.append((u'  ' + g['name'], u'{0} no.  {1:,.0f} VA -> {2:,.0f} VA  ({3:.0f} %)  {4}'.format(
                    g['count'], g['connected_va'], g['demand_va'], g['factor'] * 100, g['rule'])))
            if r['missing']:
                rows.append((u'', u'! {0} element(s) without Apparent Load - see "Source" column.'.format(r['missing'])))
            if r['no_pf']:
                rows.append((u'', u'! {0} element(s) use the default pf.'.format(r['no_pf'])))
            for w in r['warnings']:
                rows.append((u'', u'! ' + w))
            return _lines(rows)
        self._show(self.md_result, run)

    def _on_to_cable(self, sender, args):
        r = self._last_model
        if r is None:
            self.txt_status.Text = u'Read a selection first'
            return
        self.cs_phase.SelectedIndex = 0 if r['phases'] == 1 else 1
        self.cs_volts.Text = u'{0:g}'.format(r['volts'])
        self.cs_ib.Text = u'{0:.2f}'.format(r['ib'])
        self.tabs.SelectedIndex = 3
        self.recalc_cable()

    def _on_to_load(self, sender, args):
        r = self._last_model
        if r is None:
            self.txt_status.Text = u'Read a selection first'
            return
        self.lc_phase.SelectedIndex = 0 if r['phases'] == 1 else 1
        self.lc_volts.Text = u'{0:g}'.format(r['volts'])
        self.lc_unit.SelectedIndex = _UNITS.index('kVA')
        self.lc_pf.Text = u'{0:.2f}'.format(r['pf'])
        self.lc_value.Text = u'{0:.3f}'.format(r['demand_kva'])
        self.tabs.SelectedIndex = 1
        self.recalc_load()

    # ------------------------------------------------- 6 isolator rating
    def _wire_iso_tab(self):
        self._iso_tsv = u''
        for label, _, _ in _PHASES:
            self.iso_phase.Items.Add(label)
        self.iso_phase.SelectedIndex = 1
        for u in _UNITS:
            self.iso_unit.Items.Add(u)
        self.iso_unit.SelectedIndex = 0
        for m in _MOTOR_MODES:
            self.iso_motor_mode.Items.Add(m)
        self.iso_motor_mode.SelectedIndex = 0

        quick = self._handler(self.recalc_iso_quick)
        both = self._handler(self._recalc_iso_both)
        for box in (self.iso_volts, self.iso_value, self.iso_pf):
            box.TextChanged += quick
        self.iso_unit.SelectionChanged += quick
        self.iso_phase.SelectionChanged += self._phase_handler(self.iso_phase, self.iso_volts, self.recalc_iso_quick)
        self.iso_motor.Checked += quick
        self.iso_motor.Unchecked += quick
        for box in (self.iso_motor_margin, self.iso_other_margin, self.iso_min):
            box.TextChanged += both
        self.iso_four_pole.Checked += both
        self.iso_four_pole.Unchecked += both
        self.iso_motor_mode.SelectionChanged += self._handler(self.recalc_iso)
        self.btn_iso_selection.Click += self._on_use_selection    # same reader as "From model"
        self.btn_iso_pick.Click += self._on_pick

        # transfer to related fixture
        self._iso_ratings, self._iso_matches, self._iso_status = {}, {}, {}

        def reset_matches(sender, args):
            if self._iso_matches:
                self._iso_matches, self._iso_status = {}, {}
                self.recalc_iso()
        for box in (self.iso_tag_param, self.iso_dist, self.iso_height,
                    self.iso_fx_filter, self.iso_ignore):
            box.TextChanged += reset_matches
        # parameter drop-down (editable): picking an item or typing a name
        self.iso_param.SelectionChanged += reset_matches
        self.iso_param.AddHandler(TextBoxBase.TextChangedEvent, TextChangedEventHandler(reset_matches))
        self.btn_iso_find.Click += self._on_iso_find
        self.btn_iso_write.Click += self._on_iso_write
        self.btn_iso_show_fx.Click += self._on_iso_show_fixtures

    def _fill_iso_params(self, doc, items):
        """Fill the parameter drop-down from the fixtures that will receive the rating.
        Keeps the user's choice when it still exists, else prefers 'Rating'."""
        current = (self.iso_param.Text or u'').strip()
        try:
            names = self._rl.fixture_param_names(doc, [i['id'] for i in items])
        except Exception as ex:
            self.txt_status.Text = u'Parameter list not readable: {0}'.format(ex)
            return
        self.iso_param.Items.Clear()
        for n in names:
            self.iso_param.Items.Add(n)
        lower = [n.lower() for n in names]
        for wanted in (current, u'Rating'):
            if wanted and wanted.lower() in lower:
                self.iso_param.SelectedIndex = lower.index(wanted.lower())
                return
        self.iso_param.Text = current                     # typed name not on these fixtures - keep it
        if names:
            self.txt_status.Text = u'"{0}" not found on the fixtures - pick a parameter'.format(current)

    def _iso_transfer_opts(self):
        param = (self.iso_param.Text or u'').strip()
        if not param:
            raise calc.CalcError('Enter the fixture parameter name (e.g. Rating).')
        return {'param': param, 'tag_param': (self.iso_tag_param.Text or u'').strip(),
                'max_plan_mm': _num_or(self.iso_dist.Text, 3000.0),
                'max_height_mm': _num_or(self.iso_height.Text, 4000.0),
                'name_filter': self.iso_fx_filter.Text,
                'ignore_tags': [w.strip() for w in (self.iso_ignore.Text or u'').split(u',') if w.strip()]}

    def _on_iso_find(self, sender, args):
        if not self.model_items:
            self.txt_status.Text = u'Read a selection first'
            return
        try:
            opts = self._iso_transfer_opts()
        except calc.CalcError as ex:
            self.txt_status.Text = u'{0}'.format(ex)
            return
        ids = [i['id'] for i in self.model_items]

        def action(uiapp):
            doc = self._active_uidoc(uiapp).Document
            self._iso_matches = self._rl.find_related_fixtures(doc, ids, opts)
            self._iso_status = {}
            found = len([1 for v in self._iso_matches.values() if v])
            self.txt_status.Text = u'Fixtures found for {0} of {1} load(s)'.format(found, len(ids))
            self.recalc_iso()
        self._run_in_revit(action, self.md_result.Text)
        self.iso_result.Text = u'Finding related fixtures...'

    def _on_iso_write(self, sender, args):
        """One click: uses the selection already read - finds the related fixtures
        only if that has not been done yet (and only searches the model for
        selected EQUIPMENT; selected isolators are written directly), then asks and writes."""
        from eleccalc import transfer
        if not self.model_items:
            self.txt_status.Text = u"Select loads in Revit and click 'Use current selection' first"
            return
        if not self._iso_ratings:
            self.txt_status.Text = u'No rated loads to transfer'
            return
        try:
            opts = self._iso_transfer_opts()
        except calc.CalcError as ex:
            self.txt_status.Text = u'{0}'.format(ex)
            return
        ids = [i['id'] for i in self.model_items]
        text_fmt = (self.iso_text_fmt.Text or u'{0}A').strip()

        def action(uiapp):
            doc = self._active_uidoc(uiapp).Document
            if not self._iso_matches:
                self._iso_matches = self._rl.find_related_fixtures(doc, ids, opts)
                self._iso_status = {}
            self.recalc_iso()                              # show the matches behind the question
            writes, conflicts = transfer.plan_writes(self._iso_ratings, self._iso_matches)
            if not writes:
                self.txt_status.Text = u'Nothing to write - no related fixture with "{0}" found'.format(opts['param'])
                return
            direct = len([1 for w in writes.values() if w['how'] == transfer.HOW_SELF])
            msg = u'Write the isolator rating to "{0}" on {1} fixture(s)?'.format(opts['param'], len(writes))
            if direct:
                msg += u'\n\n{0} of them are the selected isolators themselves.'.format(direct)
            if len(writes) > direct:
                msg += u'\n{0} found from the selected equipment (tag / nearest) - check the Fixture column.'.format(
                    len(writes) - direct)
            if conflicts:
                msg += (u'\n\n{0} fixture(s) are related to loads with different ratings - '
                        u'the largest rating is written.').format(len(conflicts))
            msg += u'\n\nThis changes the model (one Undo step).'
            if not forms.alert(msg, yes=True, no=True, title='Electrical Calculator'):
                self.txt_status.Text = u'Not written'
                return
            self._iso_status = self._rl.write_ratings(doc, writes, opts['param'], text_fmt)
            done = len([s for s in self._iso_status.values() if s.startswith(u'written')])
            self.txt_status.Text = u'{0} fixture(s) updated'.format(done)
            self.recalc_iso()
        self._run_in_revit(action, self.md_result.Text)
        self.iso_result.Text = u'Writing ratings...'

    def _on_iso_show_fixtures(self, sender, args):
        ids = sorted(set(fid for v in self._iso_matches.values() for fid, _, _, _ in v))
        if not ids:
            self.txt_status.Text = u"Click 'Preview matches' or 'Write ratings' first"
            return

        def action(uiapp):
            self._rl.select_ids(self._active_uidoc(uiapp), ids)
            self.txt_status.Text = u'{0} fixture(s) selected in Revit'.format(len(ids))
            self.recalc_model()
        self._run_in_revit(action, self.md_result.Text)

    def _recalc_iso_both(self):
        self.recalc_iso_quick()
        self.recalc_iso()

    def _iso_rules(self):
        return {'motor_margin_pct': _num_or(self.iso_motor_margin.Text, 125.0),
                'other_margin_pct': _num_or(self.iso_other_margin.Text, 100.0),
                'min_rating': _num_or(self.iso_min.Text, 20.0),
                'four_pole': bool(self.iso_four_pole.IsChecked)}

    def recalc_iso_quick(self):
        def run():
            phases = self._phase(self.iso_phase)
            volts = _num(self.iso_volts.Text)
            lc = calc.load_current(phases, volts, _num(self.iso_value.Text),
                                   _UNITS[max(0, self.iso_unit.SelectedIndex)], _num(self.iso_pf.Text))
            motor = bool(self.iso_motor.IsChecked)
            r = calc.isolator_rating(lc['kva'] * 1000.0, volts, 3 if phases == 3 else 1, motor,
                                     **self._iso_rules())
            margin = self._iso_rules()['motor_margin_pct' if motor else 'other_margin_pct']
            rows = [(u'Isolator', u'{0} A  {1}  {2}'.format(r['rating'] or u'-', r['poles'], r['duty'])),
                    (u'Load current Ib', u'{0:,.2f} A'.format(r['ib'])),
                    (u'Required', u'{0:,.2f} A  (Ib x {1:g} %)'.format(r['required'], margin))]
            if r['note']:
                rows.append((u'', u'! ' + r['note']))
            return _lines(rows)
        self._show(self.iso_quick_result, run)

    def recalc_iso(self):
        self._iso_tsv = u''
        if not self.model_items:
            self.iso_grid.ItemsSource = None
            self.iso_result.Foreground = Brushes.Black
            self.iso_result.Text = (u"Select loads in Revit and click 'Use current selection' "
                                    u"to rate an isolator for each one.")
            return

        def run():
            rules = self._iso_rules()
            mode = self.iso_motor_mode.SelectedIndex
            table = DataTable()
            for name, type_name in _ISO_COLS:
                table.Columns.Add(name, System.Type.GetType(type_name))
            tsv = [u'\t'.join(n for n, _ in _ISO_COLS)]
            counts = {}
            skipped = 0
            self._iso_ratings = {}
            for i in self.model_items:
                row = table.NewRow()
                row['Id'] = i['id']
                row['Family Type'] = i['name']
                row['Load Classification'] = i.get('classification') or u'-'
                row['VA'] = round(i['va'] or 0.0, 1)
                row['V'] = round(i['volts'] or 0.0, 1)
                motor = (mode == 1) or (mode == 0 and calc.is_motor_load(
                    i.get('category'), i.get('classification'), i.get('name')))
                try:
                    r = calc.isolator_rating(i['va'], i['volts'], i['poles'], motor, **rules)
                    row['Ib (A)'] = round(r['ib'], 2)
                    row['Required (A)'] = round(r['required'], 2)
                    row['Isolator (A)'] = u'{0}'.format(r['rating'] or u'-')
                    row['Poles'] = r['poles']
                    row['Duty'] = r['duty']
                    row['Note'] = r['note']
                    key = (r['rating'] or 99999, r['poles'], r['duty'])
                    counts[key] = counts.get(key, 0) + 1
                    self._iso_ratings[i['id']] = r['rating']
                except calc.CalcError as ex:
                    if u'margin' in u'{0}'.format(ex).lower():
                        raise                              # rule problem -> show it instead of the table
                    row['Note'] = u'no load - not rated' if not i['va'] else u'{0}'.format(ex)
                    skipped += 1
                if self._iso_matches:
                    found = self._iso_matches.get(i['id']) or []
                    row['Fixture'] = u', '.join(lbl for _, lbl, _, _ in found) or u'NOT FOUND'
                    row['Match'] = u', '.join(sorted(set(how for _, _, how, _ in found)))
                    row['Written'] = u', '.join(self._iso_status.get(fid, u'') for fid, _, _, _ in found
                                                if self._iso_status.get(fid))
                    notes = [n for _, _, _, n in found if n]
                    if notes:
                        prev = row['Note']
                        prev = u'' if prev is None or isinstance(prev, System.DBNull) else u'{0}'.format(prev)
                        row['Note'] = u'; '.join(x for x in [prev] + notes if x)
                table.Rows.Add(row)
                tsv.append(u'\t'.join(u'{0}'.format(row[n]) for n, _ in _ISO_COLS))
            self.iso_grid.ItemsSource = table.DefaultView
            self._iso_tsv = u'\n'.join(tsv)

            rows = [(u'Isolators', u'{0} rated, {1} not rated'.format(len(self.model_items) - skipped, skipped))]
            for (rating, poles, duty) in sorted(counts):
                label = u'{0} A'.format(rating) if rating != 99999 else u'> 1600 A'
                rows.append((u'  {0} {1} {2}'.format(label, poles, duty), u'x {0}'.format(counts[(rating, poles, duty)])))
            if skipped:
                rows.append((u'', u'! elements without Apparent Load are listed with a note.'))
            if self._iso_matches:
                missing = len([1 for i in self.model_items if not self._iso_matches.get(i['id'])])
                rows.append((u'Related fixtures', u'found for {0} load(s), NOT FOUND for {1}'.format(
                    len(self.model_items) - missing, missing)))
            if self._iso_status:
                written = len([s for s in self._iso_status.values() if s.startswith(u'written')])
                rows.append((u'Written', u'{0} of {1} fixture(s) - see "Written" column'.format(
                    written, len(self._iso_status))))
            return _lines(rows)
        self._show(self.iso_result, run)

    # ------------------------------------------------- 1 load current
    def recalc_load(self):
        def run():
            phases = self._phase(self.lc_phase)
            r = calc.load_current(phases, _num(self.lc_volts.Text), _num(self.lc_value.Text),
                                  _UNITS[max(0, self.lc_unit.SelectedIndex)], _num(self.lc_pf.Text))
            return _lines([
                (u'Current', u'{0:,.2f} A'.format(r['amps'])),
                (u'Active power', u'{0:,.2f} kW'.format(r['kw'])),
                (u'Apparent power', u'{0:,.2f} kVA'.format(r['kva'])),
                (u'Reactive power', u'{0:,.2f} kVAr'.format(r['kvar'])),
                (u'Next breaker', u'{0} A'.format(calc.next_breaker(r['amps']) or u'> 4000')),
            ])
        self._show(self.lc_result, run)

    # ------------------------------------------------- 2 voltage drop
    def _on_vd_insulation(self, sender, args):
        if self._busy:
            return
        manual = self._vd_key() is None
        self.vd_size.IsEnabled = not manual
        self.vd_mv.IsReadOnly = not manual
        self.vd_mv.Background = (Brushes.White if manual
                                 else Brushes.WhiteSmoke)
        if not manual:
            self._busy = True
            self._fill_vd_sizes()
            self._busy = False
            self._update_vd_mv()
        self.recalc_vd()

    def _on_vd_size(self, sender, args):
        if self._busy:
            return
        self._update_vd_mv()
        self.recalc_vd()

    def _update_vd_mv(self):
        key, size = self._vd_key(), self._vd_size()
        if key is None or size is None:
            return
        _, mv = calc.cable_data(key, size, self._phase(self.vd_phase))
        self._busy = True
        self.vd_mv.Text = u'{0:g}'.format(mv)
        self._busy = False

    def recalc_vd(self):
        def run():
            volts, ib = _num(self.vd_volts.Text), _num(self.vd_current.Text)
            limit = _num(self.vd_limit.Text)
            r = calc.voltage_drop(self._phase(self.vd_phase), volts, ib, _num(self.vd_length.Text),
                                  _num(self.vd_mv.Text), limit)
            rows = [(u'Volt drop', u'{0:,.2f} V'.format(r['vd_v'])),
                    (u'Volt drop %', u'{0:.2f} %'.format(r['vd_pct'])),
                    (u'Voltage at load', u'{0:,.1f} V'.format(r['v_end']))]
            if limit:
                rows.append((u'Max length', u'{0:,.0f} m at {1:g} %'.format(r['max_length_m'], limit)))
                rows.append((u'', u'OK - within limit' if r['ok'] else u'FAILS - exceeds {0:g} %'.format(limit)))
            return _lines(rows)
        self._show(self.vd_result, run)

    # -------------------------------------------- 3 cable & breaker size
    def recalc_cable(self):
        def run():
            ins = _INSULATION[max(0, self.cs_insulation.SelectedIndex)][1]
            r = calc.size_cable(self._phase(self.cs_phase), _num(self.cs_volts.Text), _num(self.cs_ib.Text),
                                _num(self.cs_length.Text), ins,
                                _num_or(self.cs_ca.Text, 1.0), _num_or(self.cs_cg.Text, 1.0),
                                _num_or(self.cs_ci.Text, 1.0), _num_or(self.cs_vdlimit.Text, 5.0),
                                _num(self.cs_breaker.Text))
            rows = [(u'Breaker In', u'{0:g} A'.format(r['in_a'])),
                    (u'Factors CaxCgxCi', u'{0:.3f}'.format(r['factor'])),
                    (u'It required', u'≥ {0:,.1f} A'.format(r['it_required']))]
            c = r['cable']
            if c is None:
                rows.append((u'', u'No single {0} cable up to 300 mm² meets current + volt drop.\n'
                                  u'Use parallel cables, a shorter route or a higher voltage.'.format(ins)))
            else:
                cores = u'2-core' if self._phase(self.cs_phase) == 1 else u'4-core'
                rows += [(u'Cable', u'{0} x {1:g} mm² Cu {2} SWA'.format(cores, c['size_mm2'], ins)),
                         (u'It / Iz', u'{0:g} A / {1:,.1f} A'.format(c['it'], c['iz'])),
                         (u'Volt drop', u'{0:,.2f} V  ({1:.2f} %)'.format(c['vd_v'], c['vd_pct'])),
                         (u'Sized by', r['governed_by'])]
                if r['governed_by'] == 'voltage drop':
                    rows.append((u'', u'(current alone needs {0:g} mm²)'.format(r['size_by_current'])))
            return _lines(rows)
        self._show(self.cs_result, run)

    # ------------------------------------------------- 4 PF correction
    def recalc_pf(self):
        def run():
            r = calc.pf_correction(_num(self.pf_kw.Text), _num(self.pf_now.Text), _num(self.pf_target.Text),
                                   self._phase(self.pf_phase), _num(self.pf_volts.Text))
            return _lines([
                (u'Capacitor bank', u'{0:,.1f} kVAr'.format(r['kvar'])),
                (u'kVA before / after', u'{0:,.1f} / {1:,.1f} kVA'.format(r['kva_before'], r['kva_after'])),
                (u'Current before / after', u'{0:,.1f} / {1:,.1f} A'.format(r['amps_before'], r['amps_after'])),
                (u'Capacity released', u'{0:,.1f} kVA'.format(r['kva_released'])),
            ])
        self._show(self.pf_result, run)

    # ------------------------------------------------- 5 fault level
    def recalc_sc(self):
        def run():
            r = calc.transformer_fault(_num(self.sc_kva.Text), _num(self.sc_volts.Text),
                                       _num(self.sc_z.Text), _num(self.sc_source.Text))
            return _lines([
                (u'Full-load current', u'{0:,.0f} A'.format(r['ifl_a'])),
                (u'Fault current Isc', u'{0:,.2f} kA'.format(r['isc_ka'])),
                (u'Fault level', u'{0:,.1f} MVA'.format(r['fault_mva'])),
                (u'Isc (infinite source)', u'{0:,.2f} kA'.format(r['isc_inf_ka'])),
                (u'', u'Switchgear breaking capacity must be ≥ Isc.'),
            ])
        self._show(self.sc_result, run)

    def recalc_all(self):
        self._update_vd_mv()
        for f in (self.recalc_load, self.recalc_vd, self.recalc_cable, self.recalc_pf, self.recalc_sc,
                  self.recalc_iso_quick):
            f()

    # ------------------------------------------------------------ footer
    def _on_topmost(self, sender, args):
        self.Topmost = bool(self.cb_topmost.IsChecked)

    def _on_copy(self, sender, args):
        boxes = [self.md_result, self.lc_result, self.vd_result, self.cs_result, self.pf_result, self.sc_result,
                 self.iso_result]
        tab = self.tabs.SelectedItem
        idx = max(0, self.tabs.SelectedIndex)
        text = boxes[idx].Text
        if boxes[idx] is self.iso_result:                  # quick check + full table (pastes into Excel)
            text = u'{0}\n\n{1}\n\n{2}'.format(self.iso_quick_result.Text, text, self._iso_tsv).rstrip()
        try:
            Clipboard.SetText(u'{0}\n{1}'.format(tab.Header, text))
            self.txt_status.Text = u'Copied'
        except Exception:                                  # clipboard locked by another app
            self.txt_status.Text = u'Clipboard busy - try again'

    def _on_closed(self, sender, args):
        System.AppDomain.CurrentDomain.SetData(_INSTANCE_KEY, None)
        if self._api is not None:
            try:
                self._api.Dispose()
            except Exception:
                pass


# ------------------------------------------------------------------ entry
def show(xaml_file):
    """Open the calculator, or bring the already-open one to the front."""
    existing = System.AppDomain.CurrentDomain.GetData(_INSTANCE_KEY)
    if existing is not None:
        try:
            if existing.WindowState == WindowState.Minimized:
                existing.WindowState = WindowState.Normal
            existing.Activate()
            return existing
        except Exception:                                  # stale reference after a reload
            pass
    window = ElectricalCalculatorWindow(xaml_file)
    System.AppDomain.CurrentDomain.SetData(_INSTANCE_KEY, window)
    window.show(modal=False)
    return window

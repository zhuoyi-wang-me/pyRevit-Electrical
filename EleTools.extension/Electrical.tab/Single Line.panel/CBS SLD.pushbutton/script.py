# -*- coding: utf-8 -*-
"""CBS SLD - the single line diagram of central battery (CBS) panels, drawn
from their circuits as detail lines and text notes in a drafting view.

Per circuit (in panel schedule order: odd slots left, even slots right):
    [EM / ADDRESSABLE MODULE] -- n NOS. -- [EM / ADDRESSABLE MODULE] -- CKT NO. R1 -- | panel |
Exit signs get a plain EXIT box. Under the panel: the interface lines
(FA, BMS, DLS, fed from, data, DC power).

Only adds detail lines / text notes (and a drafting view when needed).
Redraw replaces only the lines and text this tool drew for that panel.
"""
__title__ = 'CBS\nSLD'
__author__ = 'BHEEM'

import io
import os
import time
import traceback

from Autodesk.Revit.DB import Transaction

from pyrevit import forms, revit, script

import cbs_layout as layout
import cbs_revit as rv

doc = revit.doc
TITLE = 'CBS Single Line Diagram'
XAML = os.path.join(os.path.dirname(__file__), 'ui.xaml')
LOG_DIR = os.path.join(os.environ.get('APPDATA', ''), 'EleTools', 'CbsSld')

SIDES = [('Panel schedule columns (odd slots left, even right)', 'slots'),
         ('First half left, second half right', 'half')]
EXISTING = [('Redraw it (replace this tool\'s lines and text)', 'redraw'),
            ('Make a new view', 'new')]
OUTPUTS = [('One drafting view per panel', 'each'),
           ('All selected panels in one view (by level)', 'all'),
           ('All selected panels fitted to the sheet (by level, several views if needed)', 'fit')]
# (name, drawing area width, height in mm) - the sheet less the title block and margins = the BORDER drawn
SHEETS = [('A0', 1000.0, 800.0), ('A1', 780.0, 560.0), ('A2', 540.0, 390.0), ('A3', 370.0, 270.0)]
SHEET_OFFSET_MM = 10.0                                 # kept free INSIDE the border on every side
COMPACT_STEPS = [1.0, 0.85, 0.7, 0.55, 0.45, 0.35]     # spacings tried (roomiest first) to fit one sheet
NO_DETAILS = u'<None>'
DETAILS_GAP_MM = 10.0                                  # paper gap between the copied details and the bar
DEFAULT_LEFT =u'FA INTERFACE\nBMS HARDWARE INTERFACE'
DEFAULT_RIGHT = u'[DLS] LDB - SUB CIRCUIT|MONITORING INTERFACE\nFED FROM {feed}\nDATA CABLE\nDC POWER CABLE'


def log(lines):
    try:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR)
        with io.open(os.path.join(LOG_DIR, 'last_run.txt'), 'w', encoding='utf-8') as handle:
            handle.write(u'\n'.join(u'{0}'.format(line) for line in lines))
    except Exception:
        pass


def _pick(combo, items, wanted):
    """Fill a combo with names; select `wanted` (a name) or the first."""
    combo.Items.Clear()
    for name in items:
        combo.Items.Add(name)
    if wanted in items:
        combo.SelectedIndex = items.index(wanted)
    elif items:
        combo.SelectedIndex = 0


class CbsWindow(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, XAML)
        self.cfg = script.get_config()
        get = lambda name, default: getattr(self.cfg, name, default) if hasattr(self.cfg, name) else default
        self.all_panels = rv.panels(doc)
        self.text_types = rv.text_types(doc)
        self.styles = rv.line_styles(doc)
        type_names = [name for name, _n in self.text_types]
        default_type = [name for name, n in self.text_types if n == rv.default_text_type(doc)]
        default_type = default_type[0] if default_type else None
        _pick(self.cmbDevText, type_names, get('dev_text', default_type))
        _pick(self.cmbLblText, type_names, get('lbl_text', default_type))
        _pick(self.cmbTitleText, type_names, get('title_text', self._largest_type() or default_type))
        style_names = [name for name, _n in self.styles]
        _pick(self.cmbLine, style_names, get('line_style', '<Thin Lines>'))
        _pick(self.cmbBoxLine, style_names, get('box_style', '<Medium Lines>'))
        dashed = [n for n in style_names if [w for w in ('center', 'centre', 'dash') if w in n.lower()]]
        _pick(self.cmbLevelLine, style_names, get('level_style', dashed[0] if dashed else '<Thin Lines>'))
        # building data network + fixed details (all-panels / fitted views)
        _pick(self.cmbNetText, type_names, get('net_text', self._largest_type() or default_type))
        _pick(self.cmbDataLine, style_names, get('data_style', dashed[0] if dashed else '<Thin Lines>'))
        self.chkNetwork.IsChecked = bool(get('network', True))
        detail_names = sorted(rv.drafting_views(doc), key=lambda n: n.lower())
        _pick(self.cmbDetails, [NO_DETAILS] + detail_names, get('details', NO_DETAILS))
        self.chkLevels.IsChecked = bool(get('levels', True))
        self.txtLevelPrefix.Text = get('level_prefix', u'LEVEL ')
        self.chkLevels.Click += self._show_circuits
        _pick(self.cmbOutput, [label for label, _v in OUTPUTS],
              dict((v, l) for l, v in OUTPUTS).get(get('output', 'each')))
        _pick(self.cmbSheet, [s[0] for s in SHEETS], get('sheet', 'A1'))
        self.txtAllName.Text = get('all_name', u'CBS SLD - All Panels')
        self.txtKeepLeft.Text = u'{0}'.format(get('keep_left', u'0'))
        self.chkBorder.IsChecked = bool(get('border', True))
        _pick(self.cmbSides, [label for label, _v in SIDES],
              dict((v, l) for l, v in SIDES).get(get('sides', 'slots')))
        _pick(self.cmbExisting, [label for label, _v in EXISTING],
              dict((v, l) for l, v in EXISTING).get(get('existing', 'redraw')))
        for box, name in ((self.txtExitWords, 'exit_words'), (self.txtExitLabel, 'exit_label'),
                          (self.txtEmLabel, 'em_label'), (self.txtModule, 'module_text'),
                          (self.txtData, 'data_text'), (self.txtPrefix, 'prefix'), (self.txtSuffix, 'suffix'),
                          (self.txtViewName, 'view_name'), (self.txtScale, 'scale')):
            value = get(name, None)
            if value is not None:
                box.Text = value
        self.txtLeftIf.Text = get('left_if', DEFAULT_LEFT)
        self.txtRightIf.Text = get('right_if', DEFAULT_RIGHT)
        self.chkSpares.IsChecked = bool(get('spares', False))
        self.txtSearch.Text = get('search', 'CBS')
        self.txtSearch.TextChanged += self._filter
        self.lstPanels.SelectionChanged += self._show_circuits
        self.cmbSides.SelectionChanged += self._show_circuits
        self.chkSpares.Click += self._show_circuits
        self.btnGenerate.Click += self._generate
        self.btnClose.Click += lambda s, a: self.Close()
        self._filter()

    # ------------------------------------------------------------------ helpers
    def _largest_type(self):
        best = None
        for name, number in self.text_types:
            size = rv.text_size_mm(doc, number)[0]
            if best is None or size > best[0]:
                best = (size, name)
        return best[1] if best else None

    def _filter(self, *args):
        words = (self.txtSearch.Text or u'').strip().upper().split()
        self.shown = [(name, el) for name, el in self.all_panels if all(w in name.upper() for w in words)]
        self.lstPanels.Items.Clear()
        for name, _el in self.shown:
            self.lstPanels.Items.Add(name)
        self.txtStatus.Text = u'{0} panel(s) listed. Select the CBS panels to draw.'.format(len(self.shown))

    def _selected(self):
        names = set(self.lstPanels.SelectedItems)
        return [(name, el) for name, el in self.shown if name in names]

    def _kinds(self):
        words = [w.strip() for w in (self.txtExitWords.Text or u'').split(',') if w.strip()]
        return [('EXIT', self.txtExitLabel.Text.strip() or u'EXIT', False, words),
                ('EM', self.txtEmLabel.Text.strip() or u'EM', bool(self.txtModule.Text.strip()), [])]

    def _sides(self):
        return SIDES[max(0, self.cmbSides.SelectedIndex)][1]

    def _show_circuits(self, *args):
        self.lstCircuits.Items.Clear()
        chosen = self._selected()
        if not chosen:
            self.txtCircuitsHead.Text = 'Circuits'
            return
        name, element = chosen[0]
        data = rv.read_panel(element, self._kinds(), self._sides(), bool(self.chkSpares.IsChecked))
        self.txtCircuitsHead.Text = u'Circuits of {0} - fed from {1}'.format(name, data['feed'] or u'?')
        options = self._options(None)
        for side, rows in (('L', data['left']), ('R', data['right'])):
            for row in rows:
                text = layout.count_text(row, options) or (u'SPARE' if row['spare'] else u'no devices')
                level = row['level'][0] if row.get('level') else u'-'
                self.lstCircuits.Items.Add(u'{0} {1:<6} slot {2:<3} {3:<10} {4}{5}'.format(
                    side, row['circuit'], row['slot'], level, text, u'  (several levels)' if row.get('mixed') else u''))

    def _options(self, sizes):
        o = layout.Options()
        if sizes:
            dev, lbl, title, factor = sizes
            o.lbl_px = layout.DEV_PX * lbl / dev
            o.title_px = layout.DEV_PX * title / dev
            o.width_factor = factor
        o.circuit_prefix = self.txtPrefix.Text
        o.count_suffix = self.txtSuffix.Text
        kinds = self._kinds()
        o.kinds = [(key, label, module) for key, label, module, _w in kinds]
        o.module_text = self.txtModule.Text.strip().replace('|', '\n')
        o.data_text = self.txtData.Text.strip()
        o.network = bool(self.chkNetwork.IsChecked)
        o.levels = bool(self.chkLevels.IsChecked)
        o.level_prefix = self.txtLevelPrefix.Text or u''
        o.left_interfaces = [t.replace('|', '\n') for t in (self.txtLeftIf.Text or u'').splitlines() if t.strip()]
        o.right_interfaces = [t.replace('|', '\n') for t in (self.txtRightIf.Text or u'').splitlines() if t.strip()]
        return o

    def _type_id(self, combo):
        return self.text_types[[n for n, _i in self.text_types].index(combo.SelectedItem)][1]

    def _style_id(self, combo):
        if combo.SelectedItem is None:
            return None
        return self.styles[[n for n, _i in self.styles].index(combo.SelectedItem)][1]

    def _remember(self):
        try:
            c = self.cfg
            c.dev_text, c.lbl_text, c.title_text = (self.cmbDevText.SelectedItem, self.cmbLblText.SelectedItem,
                                                    self.cmbTitleText.SelectedItem)
            c.line_style, c.box_style = self.cmbLine.SelectedItem, self.cmbBoxLine.SelectedItem
            c.sides, c.existing = self._sides(), EXISTING[max(0, self.cmbExisting.SelectedIndex)][1]
            c.exit_words, c.exit_label, c.em_label = self.txtExitWords.Text, self.txtExitLabel.Text, self.txtEmLabel.Text
            c.module_text, c.data_text = self.txtModule.Text, self.txtData.Text
            c.prefix, c.suffix = self.txtPrefix.Text, self.txtSuffix.Text
            c.view_name, c.scale = self.txtViewName.Text, self.txtScale.Text
            c.left_if, c.right_if = self.txtLeftIf.Text, self.txtRightIf.Text
            c.spares, c.search = bool(self.chkSpares.IsChecked), self.txtSearch.Text
            c.levels, c.level_prefix = bool(self.chkLevels.IsChecked), self.txtLevelPrefix.Text
            c.level_style = self.cmbLevelLine.SelectedItem
            c.output = OUTPUTS[max(0, self.cmbOutput.SelectedIndex)][1]
            c.sheet, c.all_name = self.cmbSheet.SelectedItem, self.txtAllName.Text
            c.keep_left = self.txtKeepLeft.Text
            c.border = bool(self.chkBorder.IsChecked)
            c.net_text, c.data_style = self.cmbNetText.SelectedItem, self.cmbDataLine.SelectedItem
            c.network, c.details = bool(self.chkNetwork.IsChecked), self.cmbDetails.SelectedItem
            script.save_config()
        except Exception:
            pass

    # ------------------------------------------------------------------ all panels in one view
    def _keep_left(self):
        """The paper width (mm) kept free on the left of the sheet; ValueError when it is not a usable number."""
        value = float((self.txtKeepLeft.Text or u'0').strip().replace(u',', u'.') or 0)
        sheet = SHEETS[max(0, self.cmbSheet.SelectedIndex)]
        if value < 0 or value > sheet[1] - 50.0:
            raise ValueError
        return value

    def _drawing_area(self, details_mm=0.0):
        """(label, width, height) in mm the diagram may use: the sheet's drawing area less the free left part
        (at least as wide as the copied details + their gap)."""
        name, width, height = SHEETS[max(0, self.cmbSheet.SelectedIndex)]
        keep = max(self._keep_left(), details_mm + DETAILS_GAP_MM if details_mm > 0 else 0.0)
        off = SHEET_OFFSET_MM
        label = u'{0} less {1:.0f} mm offset'.format(name, off)
        if keep > 0:
            label += u' and {0:.0f} mm free on the left'.format(keep)
        return (label, width - 2.0 * off - keep, height - 2.0 * off)

    @staticmethod
    def _paper_size(placed, level_shapes, options, dev_mm):
        """(width, height) in mm on paper of a combined arrangement - from the real extents of everything."""
        shapes = [s for _data, panel_shapes in placed for s in panel_shapes] + list(level_shapes)
        if not shapes:
            return 0.0, 0.0
        x0, y0, x1, y1 = layout.extents(shapes, options)
        k = dev_mm / layout.DEV_PX
        return abs(x1 - x0) * k, abs(y1 - y0) * k

    def _best_compact(self, datas, options, dev_mm, sheet, lines):
        """The roomiest spacing (Options.compact) at which ALL the panels fit inside the border on one sheet.
        Only the minimum sizes shrink (panel box, circuit / count lines, gap between columns) - text keeps its
        size and its clearances. When even the tightest spacing does not fit, the tightest is used (fewest
        views)."""
        tried = []
        for step in COMPACT_STEPS:
            options.compact = step
            placed, level_shapes, _summary = layout.combine(datas, options)
            w, h = self._paper_size(placed, level_shapes, options, dev_mm)
            tried.append(u'{0:.0f}%: {1:.0f} x {2:.0f} mm'.format(step * 100.0, w, h))
            if w <= sheet[1] and h <= sheet[2]:
                lines.append(u'   spacing {0:.0f}% fits all panels on one sheet ({1})'.format(
                    step * 100.0, u'; '.join(tried)))
                return step
        lines.append(u'   even the tightest spacing does not fit one sheet ({0}) - split over several views'.format(
            u'; '.join(tried)))
        return COMPACT_STEPS[-1]

    def _paginate(self, datas, options, dev_mm, sheet, lines):
        """[[data, ...], ...] - the panels split into pages whose combined diagram (by level) fits the drawing
        area of the sheet. First fit: each panel (top level first, then by name) goes onto the first page it
        still fits on, else a new page. A panel too big for the sheet on its own gets a page of its own."""
        order = sorted(datas, key=lambda d: (-(d.get('zone_elev') or {}).get(0, -1e9), rv.natural_key(d.get('name'))))
        pages = []
        for data in order:
            placed_on = None
            for page in pages:
                placed, level_shapes, _summary = layout.combine(page + [data], options)
                w, h = self._paper_size(placed, level_shapes, options, dev_mm)
                if w <= sheet[1] and h <= sheet[2]:
                    page.append(data)
                    placed_on = page
                    break
            if placed_on is None:
                pages.append([data])
        lines.append(u'fit to {0} ({1:.0f} x {2:.0f} mm): {3} page(s): {4}'.format(
            sheet[0], sheet[1], sheet[2], len(pages),
            u' | '.join(u', '.join(d['name'] for d in page) for page in pages)))
        return pages

    def _draw_all(self, chosen, options, types, styles, scale, mode, dev_mm, lines, done, fit=False):
        """Every selected panel in ONE drafting view, arranged by level (see cbs_layout.combine).
        fit=True: as many views as needed, each fitting the chosen sheet size ("... - 2", "... - 3")."""
        datas = [rv.read_panel(panel, self._kinds(), self._sides(), bool(self.chkSpares.IsChecked))
                 for _name, panel in chosen]
        view_name = (self.txtAllName.Text or u'CBS SLD - All Panels').strip()
        view, made = rv.target_view(doc, view_name, scale, mode)
        unit_ft = dev_mm / layout.DEV_PX * view.Scale * rv.MM
        # real text sizes for all panels at once
        sizes = {'dev': layout.DEV_PX, 'lbl': options.lbl_px, 'title': options.title_px}
        wanted = set()
        for data in datas:
            wanted |= set((s[3], s[4]) for s in layout.layout(data, options) if s[0] == 'text')
        wanted |= set((options.level_prefix + n, 'lbl') for d in datas for n in (d.get('zone_names') or {}).values())
        if options.network:
            wanted.add((options.cable_text, 'lbl'))
        missing = [key for key in wanted if (key[0], round(sizes[key[1]], 3)) not in options.measured]
        for (text, size), value in rv.measure(doc, view, missing, types, unit_ft).items():
            options.measured[(text, round(sizes[size], 3))] = value
        lines.append(u'all panels in "{0}"; measured {1} text(s); data network {2}'.format(
            rv.name_of(view), len(missing), u'on' if options.network else u'off'))
        # the fixed details (copied from a drafting view): their paper width is kept free on the left
        details, details_mm = None, 0.0
        if self.cmbDetails.SelectedItem and self.cmbDetails.SelectedItem != NO_DETAILS:
            details = rv.drafting_views(doc).get(self.cmbDetails.SelectedItem)
            size = rv.details_size(doc, details) if details is not None else None
            if size is None:
                lines.append(u'   details view "{0}": nothing to copy'.format(self.cmbDetails.SelectedItem))
                done.append(u'Details view "{0}" has nothing to copy.'.format(self.cmbDetails.SelectedItem))
                details = None
            else:
                details_mm = size[0] / rv.MM / view.Scale
                details_h = size[1] / rv.MM / view.Scale
                lines.append(u'   details view "{0}" (1:{1}): {2:.0f} x {3:.0f} mm on paper at 1:{4}'.format(
                    rv.name_of(details), details.Scale, details_mm, details_h, view.Scale))
                if details.Scale != view.Scale:
                    done.append(u'Details view is 1:{0}, the SLD view 1:{1} - their text sizes differ on paper.'.format(
                        details.Scale, view.Scale))
        sheet = self._drawing_area(details_mm)
        base = SHEETS[max(0, self.cmbSheet.SelectedIndex)]
        typed = self._keep_left()
        lines.append(u'   width for the diagram: {0} border {1:.0f} mm - 2 x offset {2:.0f} mm - free left {3:.0f} mm = '
                     u'{4:.0f} mm; height {5:.0f} - 2 x {2:.0f} = {6:.0f} mm   (free left = the larger of "Free left '
                     u'(mm)" {7:.0f} and details {8:.0f} + gap {9:.0f})'.format(
                         base[0], base[1], SHEET_OFFSET_MM, base[1] - 2.0 * SHEET_OFFSET_MM - sheet[1], sheet[1],
                         base[2], sheet[2], typed, details_mm, DETAILS_GAP_MM if details_mm else 0.0))
        if fit:
            options.compact = self._best_compact(datas, options, dev_mm, sheet, lines)
        pages = self._paginate(datas, options, dev_mm, sheet, lines) if fit else [datas]
        if len(pages) > 1 and OUTPUTS[max(0, self.cmbOutput.SelectedIndex)][1] == 'all':
            done.append(u'Too big for one {0} sheet: split over {1} views to stay inside the border '
                        u'(untick Border to force one view).'.format(sheet[0], len(pages)))
        for number, page in enumerate(pages):
            if number:
                view, made = rv.target_view(doc, u'{0} - {1}'.format(view_name, number + 1), scale, mode)
            self._draw_page(view, made, page, options, types, styles, dev_mm, unit_ft, sheet, lines, done,
                            u'page {0}/{1}: '.format(number + 1, len(pages)) if len(pages) > 1 else u'')
            if details is not None:
                self._copy_details(details, view, placed_extent=self._last_left_top, unit_ft=unit_ft,
                                   lines=lines, done=done)
            if number == 0:
                first = view
        if fit and mode == 'redraw':
            # pages left over from an earlier run with more pages are NOT deleted - say so
            stale = [n for n in rv.drafting_views(doc)
                     if n.startswith(view_name + u' - ') and n[len(view_name) + 3:].isdigit()
                     and int(n[len(view_name) + 3:]) > len(pages)]
            if stale:
                lines.append(u'   older extra page view(s) not used any more (delete them if not needed): '
                             + u', '.join(stale))
                done.append(u'Not used any more: ' + u', '.join(stale))
        return first

    def _draw_border(self, view, sheet, options, dev_mm, unit_ft, styles, types, lines):
        """The outline of the sheet's drawing area (paper size of SHEETS) around the diagram: the diagram's
        top-left corner (data network bar / first level line) sits at the inner corner of the free left part,
        a thin line marks where the free left part ends. Tagged 'CBS-BORDER' - a redraw replaces it."""
        name, full_w, full_h = SHEETS[max(0, self.cmbSheet.SelectedIndex)]
        off = SHEET_OFFSET_MM
        keep = full_w - 2.0 * off - sheet[1]                   # free left part, mm (typed or details)
        px = layout.DEV_PX / dev_mm                            # layout px per paper mm
        left_px, top_px = self._last_left_top
        # border | offset | free left part | diagram ... | offset | border
        x1 = left_px                                           # where the free left part ends
        x0 = x1 - (keep + off) * px
        y0 = top_px - off * px
        x2, y2 = x0 + full_w * px, y0 + full_h * px
        shapes = [('line', x0, y0, x2, y0, 'box'), ('line', x2, y0, x2, y2, 'box'),
                  ('line', x2, y2, x0, y2, 'box'), ('line', x0, y2, x0, y0, 'box')]
        if keep > 0:
            shapes.append(('line', x1, y0, x1, y2, 'thin'))
        rv.draw(doc, view, shapes, unit_ft, (0.0, 0.0), styles, types, u'CBS-BORDER')
        lines.append(u'   border: {0} {1:.0f} x {2:.0f} mm, {3:.0f} mm offset inside, free left {4:.0f} mm marked'.format(
            name, full_w, full_h, off, keep))

    def _copy_details(self, details, view, placed_extent, unit_ft, lines, done):
        """The details of the details view, copied left of the diagram (gap DETAILS_GAP_MM), tops level."""
        if details.Id == view.Id:
            done.append(u'Details view is the SLD view itself - not copied.')
            return
        left_px, top_px = placed_extent
        right_x = left_px * unit_ft - DETAILS_GAP_MM * rv.MM * view.Scale
        top_y = -top_px * unit_ft
        try:
            count = rv.copy_details(doc, details, view, right_x, top_y)
            lines.append(u'   details copied: {0} element(s)'.format(count))
        except Exception as error:
            lines.append(u'   details NOT copied: {0}'.format(error))
            done.append(u'Details not copied into "{0}": {1}'.format(rv.name_of(view), error))

    def _draw_page(self, view, made, datas, options, types, styles, dev_mm, unit_ft, sheet, lines, done, tag):
        """One combined arrangement (by level) of these panels in this view."""
        placed, level_shapes, summary = layout.combine(datas, options)
        lines.append(u'{0}"{1}"'.format(tag, rv.name_of(view)))
        removed = 0
        if not made:
            for element_id in rv.tagged_ids(doc, view, None):     # the whole arrangement is new
                try:
                    doc.Delete(element_id)
                    removed += 1
                except Exception:
                    pass
        everything = []
        for data, shapes in placed:
            count_lines, count_texts = rv.draw(doc, view, shapes, unit_ft, (0.0, 0.0), styles, types, data['uid'])
            everything += shapes
            lines.append(u'   {0}: {1} circuit(s), level {2}, {3} lines, {4} texts'.format(
                data['name'], data['circuits'], data.get('level') or '-', count_lines, count_texts))
            # the circuits drawn in another level's band - they make the panel's column span those levels
            every = (data.get('left') or []) + (data.get('right') or [])
            spread = lambda r: u' / '.join(u'{0} {1}'.format(n, c) for n, c in r.get('spread') or [])
            away = [r for r in every if r.get('zone')]
            if away:
                lines.append(u'      on other levels: ' + u', '.join(u'{0} -> {1}{2}'.format(
                    r['circuit'], r['level'][0] if r.get('level') else u'?',
                    u' (mixed: {0})'.format(spread(r)) if r.get('mixed') else u'') for r in away))
            kept = [r for r in every if r.get('mixed') and not r.get('zone')]
            if kept:
                lines.append(u'      mixed, kept at the panel: ' + u', '.join(
                    u'{0} ({1})'.format(r['circuit'], spread(r)) for r in kept))
        rv.draw(doc, view, level_shapes, unit_ft, (0.0, 0.0), styles, types, u'CBS-LEVELS')
        everything += level_shapes
        # left edge and top of the whole diagram (px) - the copied details go left of it
        net = summary.get('network')
        if net:
            self._last_left_top = (net['x0'], net['top'])
        else:
            x0, y0, _x1, _y1 = layout.extents(everything, options)
            self._last_left_top = (x0, y0)
        clashes = layout.overlaps(everything, options, shrink=1.0)
        crossed = layout.lines_through_text(everything, options, shrink=1.0)
        lines.append(u'   line crossings left: {0} (unavoidable ones are drawn with a break in the circuit line)'.format(
            layout.line_crossings(everything)))
        lines.append(u'   text overlaps: {0}; lines through text: {1}{2}'.format(
            len(clashes), len(crossed), u' - ' + u'; '.join(u'"{0}"'.format(a.replace('\n', ' ')) for a, _l in crossed[:12])
            if crossed else u''))
        # size on paper (real extents of everything drawn), against the chosen sheet
        paper_w, paper_h = self._paper_size(placed, level_shapes, options, dev_mm)
        if self.chkBorder.IsChecked:
            self._draw_border(view, sheet, options, dev_mm, unit_ft, styles, types, lines)
        if net:
            k = dev_mm / layout.DEV_PX
            lines.append(u'   diagram width {0:.0f} mm = data network bar {1:.0f} mm + bar to panels / levels {2:.0f} mm'.format(
                paper_w, (net['x1'] - net['x0']) * k, paper_w - (net['x1'] - net['x0']) * k))
        k = dev_mm / layout.DEV_PX
        for name, x, w, span in sorted(summary.get('columns') or [], key=lambda c: c[1]):
            # a panel spans every level band its circuits reach - another panel can only stand above / below
            # it where it does not use the same band
            lines.append(u'   column: {0} at {1:.0f} mm, {2:.0f} mm wide, uses level bands {3}'.format(
                name, x * k, w * k, u', '.join(span)))
        fits = paper_w <= sheet[1] and paper_h <= sheet[2]
        fit_text = u'{0:.0f} x {1:.0f} mm on paper - {2} {3} ({4} x {5} mm drawing area)'.format(
            paper_w, paper_h, u'fits' if fits else u'does NOT fit', sheet[0], sheet[1], sheet[2])
        if not fits:
            factor = min(sheet[1] / paper_w, sheet[2] / paper_h)
            if len(datas) == 1:
                fit_text += u'; this ONE panel is bigger than the sheet - pick a larger sheet or text types about ' \
                            u'{0:.0f}% of the size (device text {1:.1f} mm)'.format(factor * 100.0, dev_mm * factor)
            else:
                fit_text += u'; use Output "fitted to the sheet" to split it over several views, or pick text ' \
                            u'types about {0:.0f}% of the size (device text {1:.1f} mm)'.format(
                                factor * 100.0, dev_mm * factor)
        lines.append(u'   ' + fit_text)
        done.append(u'{0}{1} panel(s) in "{2}", {3} level(s){4}. {5}'.format(
            tag, len(datas), rv.name_of(view), summary['levels'],
            u', {0} old removed'.format(removed) if removed else u'', fit_text))
        return view

    # ------------------------------------------------------------------ generate
    def _generate(self, sender, args):
        lines = [u'CBS SLD run {0}'.format(time.strftime('%Y-%m-%d %H:%M:%S'))]
        chosen = self._selected()
        if not chosen:
            forms.alert('Select one or more panels in the list.', title=TITLE)
            return
        if self.cmbDevText.SelectedItem is None or self.cmbLblText.SelectedItem is None \
                or self.cmbTitleText.SelectedItem is None:
            forms.alert('Choose the three text types.', title=TITLE)
            return
        try:
            scale = int(float(self.txtScale.Text))
            if scale < 1 or scale > 5000:
                raise ValueError
        except Exception:
            forms.alert('View scale: type a number such as 50 or 100.', title=TITLE)
            return
        try:
            self._keep_left()
        except Exception:
            forms.alert(u'Free left (mm): type a number from 0 up to the sheet width less 50 mm '
                        u'(A1: up to {0:.0f}).'.format(SHEETS[1][1] - 50.0), title=TITLE)
            return
        self._remember()
        started = time.time()
        types = {'dev': self._type_id(self.cmbDevText), 'lbl': self._type_id(self.cmbLblText),
                 'title': self._type_id(self.cmbTitleText)}
        dev_mm, factor = rv.text_size_mm(doc, types['dev'])
        lbl_mm = rv.text_size_mm(doc, types['lbl'])[0]
        title_mm = rv.text_size_mm(doc, types['title'])[0]
        options = self._options((dev_mm, lbl_mm, title_mm, factor))
        if self.cmbNetText.SelectedItem is not None:
            types['net'] = self._type_id(self.cmbNetText)
            options.net_px = layout.DEV_PX * rv.text_size_mm(doc, types['net'])[0] / dev_mm
        else:
            options.network = False
        styles = {'thin': self._style_id(self.cmbLine), 'box': self._style_id(self.cmbBoxLine),
                  'level': self._style_id(self.cmbLevelLine), 'data': self._style_id(self.cmbDataLine)}
        mode = EXISTING[max(0, self.cmbExisting.SelectedIndex)][1]
        lines.append(u'text mm: device {0:.2f}, label {1:.2f}, title {2:.2f}; width factor {3:.2f}; scale 1:{4}; '
                     u'mode {5}'.format(dev_mm, lbl_mm, title_mm, factor, scale, mode))
        done, last_view = [], None
        transaction = Transaction(doc, 'CBS SLD')
        transaction.Start()
        output = OUTPUTS[max(0, self.cmbOutput.SelectedIndex)][1]
        try:
            if output in ('all', 'fit'):
                # Border ticked: the drawing must stay INSIDE the border - one view when it fits, else as many
                # views as needed (as "fitted to the sheet")
                fit = output == 'fit' or bool(self.chkBorder.IsChecked)
                last_view = self._draw_all(chosen, options, types, styles, scale, mode, dev_mm, lines, done,
                                           fit=fit)
            for name, panel in (chosen if output == 'each' else []):
                data = rv.read_panel(panel, self._kinds(), self._sides(), bool(self.chkSpares.IsChecked))
                view_name = (self.txtViewName.Text or u'CBS SLD - {panel}').replace('{panel}', name)
                view, made = rv.target_view(doc, view_name, scale, mode)
                unit_ft = dev_mm / layout.DEV_PX * view.Scale * rv.MM
                # the real size of every text in Revit, then the layout made with those sizes (no overlaps)
                sizes = {'dev': layout.DEV_PX, 'lbl': options.lbl_px, 'title': options.title_px}
                first_pass = layout.layout(data, options)
                wanted = set((s[3], s[4]) for s in first_pass if s[0] == 'text')
                missing = [key for key in wanted if (key[0], round(sizes[key[1]], 3)) not in options.measured]
                for (text, size), value in rv.measure(doc, view, missing, types, unit_ft).items():
                    options.measured[(text, round(sizes[size], 3))] = value
                lines.append(u'   measured {0} text(s); e.g. module {1}'.format(
                    len(missing), options.measured.get((options.module_text, round(layout.DEV_PX, 3)))))
                shapes = layout.layout(data, options)
                clashes = layout.overlaps(shapes, options, shrink=1.0)
                lines.append(u'   text overlaps after layout: {0}{1}'.format(
                    len(clashes), u' - ' + u'; '.join(u'"{0}" / "{1}"'.format(a.replace('\n', ' '), b.replace('\n', ' '))
                                                      for a, b in clashes[:12]) if clashes else u''))
                lines.append(u'   line crossings left: {0}'.format(layout.line_crossings(shapes)))
                crossed = layout.lines_through_text(shapes, options, shrink=1.0)
                lines.append(u'   lines through text: {0}{1}'.format(
                    len(crossed), u' - ' + u'; '.join(u'"{0}"'.format(a.replace('\n', ' ')) for a, _l in crossed[:12])
                    if crossed else u''))
                removed = 0
                if not made:
                    old = rv.tagged_ids(doc, view, data['uid'])
                    for element_id in old:
                        try:
                            doc.Delete(element_id)
                            removed += 1
                        except Exception:
                            pass
                count_lines, count_texts = rv.draw(doc, view, shapes, unit_ft, (0.0, 0.0), styles, types, data['uid'])
                done.append(u'{0}: {1} circuit(s) -> "{2}" ({3} lines, {4} texts{5})'.format(
                    name, data['circuits'], rv.name_of(view), count_lines, count_texts,
                    u', {0} old removed'.format(removed) if removed else u''))
                lines.append(done[-1])
                lines.append(u'   panel level: {0}; levels by zone: {1}'.format(data.get('level'), data.get('zone_names')))
                for side in ('left', 'right'):
                    lines.append(u'   {0}: '.format(side) + u', '.join(
                        u'{0}={1} [{2} z{3}]'.format(r['circuit'], r['counts'], r['level'][0] if r.get('level') else '-',
                                                      r.get('zone', 0)) for r in data[side]))
                last_view = view
            transaction.Commit()
        except Exception as error:
            if transaction.HasStarted() and not transaction.HasEnded():
                transaction.RollBack()
            lines.append(u'FAILED: {0}'.format(error))
            lines.append(traceback.format_exc())
            log(lines)
            forms.alert(u'Nothing was drawn:\n{0}\n\nDetails: {1}'.format(error, os.path.join(LOG_DIR, 'last_run.txt')),
                        title=TITLE)
            return
        lines.append(u'took {0:.1f} s'.format(time.time() - started))
        log(lines)
        self.txtStatus.Text = u'Done in {0:.1f} s. '.format(time.time() - started) + u' | '.join(done)
        if last_view is not None and (len(chosen) == 1 or output in ('all', 'fit')):
            try:
                revit.uidoc.RequestViewChange(last_view)
            except Exception:
                pass


if __name__ == '__main__':
    try:
        CbsWindow().ShowDialog()
    except Exception as error:
        log([u'window failed: {0}'.format(error), traceback.format_exc()])
        forms.alert(u'CBS SLD could not start:\n{0}'.format(error), title=TITLE)

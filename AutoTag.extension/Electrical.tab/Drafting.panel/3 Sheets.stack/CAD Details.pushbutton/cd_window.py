# -*- coding: utf-8 -*-
"""CAD Details - the window (modal).

    1. DWG file      Browse / Read Details: cd_convert (DWG -> DXF with AutoCAD's console), cd_dxf (read),
                     cd_split (one sheet -> its details)
    2. Details       tick / rename; the selected one is drawn in the preview
    3. Settings      view scale, the line style for all lines, DWG units, the type for patterned hatches,
                     view name prefix, text / hatches / split by frames
    4. Create        cd_revit.create - one drafting view per ticked detail (one Undo)
"""
import math
import os
import time
import traceback

import clr
for _name in ('PresentationFramework', 'PresentationCore', 'WindowsBase'):
    clr.AddReference(_name)
from System.Windows import Thickness, VerticalAlignment
from System.Windows.Controls import CheckBox, ColumnDefinition, Grid, TextBlock, TextBox, Canvas
from System.Windows.Media import BrushConverter, PointCollection, RotateTransform
from System.Windows.Shapes import Polygon, Polyline
from System.Windows import Point, GridLength, GridUnitType

from pyrevit import forms, revit, script

import cd_convert
import cd_dxf
import cd_revit
import cd_split

LOG = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'EleTools', 'CadDetails', 'last_run.log')
CAD_FACTORS = [1, 2, 5, 10, 20, 25, 50, 100]
MATCH = u'(a type per CAD text height)'
SAME_AS_CAD = u'Same as CAD (rebuild each pattern)'
UNITS = [(u'From the DWG', None), (u'Millimetres', 1.0), (u'Centimetres', 10.0), (u'Metres', 1000.0), (u'Inches', 25.4),
         (u'Feet', 304.8)]


def trace(text, restart=False):
    try:
        folder = os.path.dirname(LOG)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with open(LOG, 'w' if restart else 'a') as handle:
            handle.write((u'{0}  {1}\n'.format(time.strftime('%H:%M:%S'), text)).encode('utf-8'))
    except Exception:
        pass


class CadDetailsWindow(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui.xaml'))
        self.doc = revit.doc
        self.cfg = script.get_config('CadDetails')
        self.brushes = BrushConverter()
        self.drawing = None
        self.details = []
        self.rows = []                 # (detail, CheckBox, TextBox name, Grid row)
        for factor in CAD_FACTORS:
            self.cad_cb.Items.Add(u'1 : {0}'.format(factor))
        factor = int(self.cfg.get_option('cad', 10))
        self.cad_cb.SelectedIndex = CAD_FACTORS.index(factor) if factor in CAD_FACTORS else CAD_FACTORS.index(10)
        self.texts = cd_revit.text_types(self.doc)
        self.texttype_cb.Items.Add(MATCH)
        for name, _t in self.texts:
            self.texttype_cb.Items.Add(name)
        wanted = self.cfg.get_option('texttype', u'')
        text_names = [n for n, _t in self.texts]
        if wanted == MATCH:
            self.texttype_cb.SelectedIndex = 0
        elif wanted in text_names:
            self.texttype_cb.SelectedIndex = text_names.index(wanted) + 1
        else:
            small = [k for k, n in enumerate(text_names) if u'2.5' in n]
            self.texttype_cb.SelectedIndex = (small[0] + 1) if small else (1 if text_names else 0)
        self.rb_single.IsChecked = bool(self.cfg.get_option('single', False))
        self.rb_views.IsChecked = not self.rb_single.IsChecked
        self.single_tb.Text = self.cfg.get_option('single_name', u'CAD Details')
        for scale in cd_revit.SCALES:
            self.scale_cb.Items.Add(u'1 : {0}'.format(scale))
        scale = int(self.cfg.get_option('scale', 10))
        self.scale_cb.SelectedIndex = cd_revit.SCALES.index(scale) if scale in cd_revit.SCALES else cd_revit.SCALES.index(10)
        self.styles = cd_revit.line_styles(self.doc)
        for name, _style in self.styles:
            self.style_cb.Items.Add(name)
        wanted = self.cfg.get_option('style', u'Thin Lines')
        names = [n for n, _s in self.styles]
        self.style_cb.SelectedIndex = names.index(wanted) if wanted in names else (names.index(u'Thin Lines') if u'Thin Lines' in names else 0)
        for label, _value in UNITS:
            self.units_cb.Items.Add(label)
        self.units_cb.SelectedIndex = 0
        self.regions = cd_revit.region_types(self.doc)
        self.pattern_cb.Items.Add(SAME_AS_CAD)
        for name, _t in self.regions:
            self.pattern_cb.Items.Add(name)
        wanted = self.cfg.get_option('pattern', SAME_AS_CAD)
        if wanted in (u'?', u''):
            wanted = SAME_AS_CAD                                      # remembered from the "?" names of an earlier version
        regions = [n for n, _t in self.regions]
        if wanted in regions:
            self.pattern_cb.SelectedIndex = regions.index(wanted) + 1
        else:
            self.pattern_cb.SelectedIndex = 0                         # the CAD's own patterns (the default)
        self.prefix_tb.Text = self.cfg.get_option('prefix', u'TYP - ')
        self.file_tb.Text = self.cfg.get_option('file', u'')
        self.chk_text.IsChecked = bool(self.cfg.get_option('text', True))
        self.chk_fills.IsChecked = bool(self.cfg.get_option('fills', True))
        self.chk_frames.IsChecked = bool(self.cfg.get_option('frames', True))
        self.preview_cv.SizeChanged += lambda s, e: self._draw(self._shown)
        for combo in (self.cad_cb, self.scale_cb, self.texttype_cb, self.units_cb):
            combo.SelectionChanged += lambda s, e: self._hint()
        self._hint()
        self._shown = None
        if cd_convert.find_console() is None:
            self.status_tb.Text = (u'AutoCAD was not found on this PC: pick DXF files (Save As DXF in AutoCAD) - a DWG cannot '
                                   u'give its text and hatches without it.')

    # ------------------------------------------------------------------ helpers
    def _status(self, text):
        self.status_tb.Text = text

    def _remember(self):
        try:
            self.cfg.scale = cd_revit.SCALES[self.scale_cb.SelectedIndex]
            self.cfg.cad = CAD_FACTORS[self.cad_cb.SelectedIndex]
            self.cfg.texttype = self.texttype_cb.SelectedItem or u''
            self.cfg.single = bool(self.rb_single.IsChecked)
            self.cfg.single_name = self.single_tb.Text
            self.cfg.style = self.style_cb.SelectedItem or u''
            self.cfg.pattern = self.pattern_cb.SelectedItem or u''
            self.cfg.prefix = self.prefix_tb.Text
            self.cfg.file = self.file_tb.Text
            self.cfg.text = bool(self.chk_text.IsChecked)
            self.cfg.fills = bool(self.chk_fills.IsChecked)
            self.cfg.frames = bool(self.chk_frames.IsChecked)
            script.save_config()
        except Exception:
            pass

    def _brush(self, colour):
        return self.brushes.ConvertFromString(colour)

    def _fallback_pattern(self):
        """The filled region type for patterned hatches: the chosen one - or, with "Same as CAD", the base for the
        rebuilt types (also used for a hatch whose pattern cannot be rebuilt): a diagonal type, else the first."""
        if not self.regions:
            return None
        index = self.pattern_cb.SelectedIndex
        if index >= 1:
            return self.regions[index - 1][1].Id
        names = [n.lower() for n, _t in self.regions]
        diagonal = [k for k, n in enumerate(names) if u'diagonal' in n or u'hatch' in n]
        return self.regions[diagonal[0] if diagonal else 0][1].Id

    def _text_size_mm(self):
        """The paper size (mm) of the chosen text type, or None."""
        index = self.texttype_cb.SelectedIndex
        if index <= 0:
            return None
        try:
            from Autodesk.Revit.DB import BuiltInParameter
            return self.texts[index - 1][1].get_Parameter(BuiltInParameter.TEXT_SIZE).AsDouble() * 304.8
        except Exception:
            return None

    def _hint(self):
        """What the scale settings do, in the user's own example: CAD 4000 -> Revit 400, CAD text 250 -> 2.5 mm."""
        try:
            factor = CAD_FACTORS[self.cad_cb.SelectedIndex]
            scale = cd_revit.SCALES[self.scale_cb.SelectedIndex]
        except Exception:
            return
        units = self.units_mm()
        heights = sorted(s['height'] for d in self.details for s in d.shapes if s['kind'] == 'text') if self.details else []
        common = heights[len(heights) // 2] if heights else 250.0
        options = cd_revit.Options(scale=scale, units_mm=units, cad_factor=factor)
        size = self._text_size_mm()
        text_part = (u'all texts in "{0}" ({1:g} mm on paper)'.format(self.texttype_cb.SelectedItem, round(size, 2))
                     if size is not None else u'text types made per height')
        self.hint_tb.Text = (u'CAD 4000 mm -> {0:g} mm in Revit.   CAD text {1:g} mm -> {2:g} mm on paper at 1 : {3}.   Text: {4}.'.format(
            round(4000.0 * units / factor, 2), round(common, 1), round(cd_revit.paper_mm(common, options), 2), scale, text_part))

    def units_mm(self):
        chosen = UNITS[self.units_cb.SelectedIndex][1]
        if chosen is not None:
            return chosen
        return (self.drawing.units_mm if self.drawing is not None else None) or 1.0

    # ------------------------------------------------------------------ 1 the file
    def browse_click(self, sender, args):
        start = os.path.dirname(self.file_tb.Text) if self.file_tb.Text and os.path.isdir(os.path.dirname(self.file_tb.Text)) else None
        path = forms.pick_file(files_filter='CAD drawings (*.dwg;*.dxf)|*.dwg;*.dxf', init_dir=start, title='Pick the DWG with the details')
        if path:
            self.file_tb.Text = path
            self.read_click(None, None)

    def read_click(self, sender, args):
        path = (self.file_tb.Text or u'').strip().strip(u'"')
        if not os.path.isfile(path):
            forms.alert(u'Pick a DWG file first.', title='CAD Details')
            return
        self._remember()
        self._status(u'Reading {0} ... (AutoCAD converts it in the background - a few seconds)'.format(os.path.basename(path)))
        trace(u'read ' + path, restart=True)
        try:
            dxf = cd_convert.to_dxf(path, log=trace)
            self.drawing = cd_dxf.read(dxf)
            gap_text = (self.gap_tb.Text or u'').strip().replace(u',', u'.')
            try:
                gap = float(gap_text) / ((UNITS[self.units_cb.SelectedIndex][1] or self.drawing.units_mm or 1.0)) if gap_text else None
            except ValueError:
                gap = None
            self.details = cd_split.split(self.drawing, gap=gap, use_frames=bool(self.chk_frames.IsChecked))
            self._show_gap()
        except Exception as error:
            trace(traceback.format_exc())
            self.drawing, self.details = None, []
            self._fill_list()
            self._status(u'Could not read the drawing: {0}'.format(error))
            return
        self._fill_list()
        self._hint()
        notes = []
        if self.drawing.units_mm is None:
            notes.append(u'the DWG does not say its units - millimetres assumed (change "DWG units" if not)')
        if self.drawing.skipped:
            notes.append(u'not converted: ' + u', '.join(u'{0} x{1}'.format(k, v) for k, v in sorted(self.drawing.skipped.items())))
        if getattr(self.drawing, 'mirrored', 0):
            notes.append(u'{0} mirrored object(s) put the right way round'.format(self.drawing.mirrored))
        if getattr(self.drawing, 'stray', 0):
            notes.append(u'{0} stray object(s) far away from the drawing left out'.format(self.drawing.stray))
        small = len([d for d in self.details if getattr(d, 'small', False)])
        if small:
            notes.append(u'{0} small scrap(s) of a few lines not ticked'.format(small))
        if self.drawing.hidden_layers:
            notes.append(u'{0} layer(s) off / frozen / not plotted left out'.format(len(self.drawing.hidden_layers)))
        self._status(u'{0} detail(s) found in {1}.{2} Tick and rename them, then Create Drafting Views.'.format(
            len(self.details), os.path.basename(path), (u' ' + u'; '.join(notes) + u'.') if notes else u''))
        trace(u'{0} details: {1}'.format(len(self.details), u'; '.join(d.name for d in self.details)))

    def split_click(self, sender, args):
        """Split the drawing already read again, with the gap typed (or automatic)."""
        if self.drawing is None:
            self.read_click(None, None)
            return
        text = (self.gap_tb.Text or u'').strip().replace(u',', u'.')
        gap = None
        if text:
            try:
                gap = float(text) / (self.units_mm() or 1.0)       # CAD mm -> drawing units
            except ValueError:
                forms.alert(u'The gap must be a number of millimetres (or empty for automatic).', title='CAD Details')
                return
        self.details = cd_split.split(self.drawing, gap=gap, use_frames=bool(self.chk_frames.IsChecked))
        self._fill_list()
        self._show_gap()
        self._status(u'{0} detail(s) with a gap of {1:.0f} mm.'.format(len(self.details), getattr(self.drawing, 'gap_used', 0) * self.units_mm()))

    def _show_gap(self):
        used = getattr(self.drawing, 'gap_used', None) if self.drawing is not None else None
        if used and not (self.gap_tb.Text or u'').strip():
            self.gap_tb.ToolTip = u'Automatic: {0:.0f} mm. Type another value and press Split again.'.format(used * self.units_mm())
        if used:
            self.gap_tb.Tag = used

    # ------------------------------------------------------------------ 2 the details
    def _fill_list(self):
        self.list_sp.Children.Clear()
        self.rows = []
        for detail in self.details:
            row = Grid()
            row.Margin = Thickness(0, 2, 0, 2)
            for width in (GridLength(26), GridLength(1, GridUnitType.Star), GridLength(120)):
                column = ColumnDefinition()
                column.Width = width
                row.ColumnDefinitions.Add(column)
            box = CheckBox()
            box.IsChecked = not getattr(detail, 'small', False)          # scraps of a few shapes: not ticked
            box.VerticalAlignment = VerticalAlignment.Center
            box.Click += lambda s, e: self._count()
            name = TextBox()
            name.Text = detail.name
            name.Padding = Thickness(3, 2, 3, 2)
            name.GotFocus += self._shower(detail)
            counts = detail.counts()
            info = TextBlock()
            info.Text = u'  {0} ln  {1} txt  {2} fill'.format(counts['curves'], counts['text'], counts['fill'])
            info.Foreground = self._brush('#5F5F5F')
            info.VerticalAlignment = VerticalAlignment.Center
            Grid.SetColumn(name, 1)
            Grid.SetColumn(info, 2)
            row.Children.Add(box)
            row.Children.Add(name)
            row.Children.Add(info)
            row.MouseLeftButtonDown += self._shower(detail)
            self.list_sp.Children.Add(row)
            self.rows.append((detail, box, name, row))
        self._count()
        self._draw(self.details[0] if self.details else None)

    def _shower(self, detail):
        def handler(sender, args):
            self._draw(detail)
        return handler

    def _count(self):
        ticked = sum(1 for d, box, n, r in self.rows if box.IsChecked)
        self.count_tb.Text = u'   {0} of {1} ticked'.format(ticked, len(self.rows))
        self.btn_create.IsEnabled = ticked > 0

    def all_click(self, sender, args):
        for d, box, n, r in self.rows:
            box.IsChecked = True
        self._count()

    def none_click(self, sender, args):
        for d, box, n, r in self.rows:
            box.IsChecked = False
        self._count()

    def _draw(self, detail):
        """The detail in the preview: lines dark, hatches grey, text as text."""
        self._shown = detail
        canvas = self.preview_cv
        canvas.Children.Clear()
        if detail is None:
            self.preview_head.Text = u'PREVIEW'
            return
        self.preview_head.Text = u'PREVIEW - ' + detail.name
        width = canvas.ActualWidth or 600.0
        height = canvas.ActualHeight or 380.0
        x0, y0, x1, y1 = detail.box
        k = min((width - 20) / max(x1 - x0, 1e-9), (height - 20) / max(y1 - y0, 1e-9))
        ox = (width - (x1 - x0) * k) / 2.0
        oy = (height - (y1 - y0) * k) / 2.0

        def to(p):
            return Point(ox + (p[0] - x0) * k, height - oy - (p[1] - y0) * k)

        dark, grey = self._brush('#1B1B1B'), self._brush('#B8C2CC')
        for shape in detail.shapes:
            if shape['kind'] == 'fill':
                for segs in shape['loops']:
                    polygon = Polygon()
                    points = PointCollection()
                    for seg in segs:
                        for p in cd_dxf.seg_points(seg):
                            points.Add(to(p))
                    polygon.Points = points
                    polygon.Fill = grey if not shape['solid'] else self._brush('#6B7785')
                    canvas.Children.Add(polygon)
        for shape in detail.shapes:
            if shape['kind'] == 'curves':
                line = Polyline()
                points = PointCollection()
                last = None
                for seg in shape['segs']:
                    for p in cd_dxf.seg_points(seg):
                        if last is None or abs(p[0] - last[0]) + abs(p[1] - last[1]) > 1e-9:
                            points.Add(to(p))
                            last = p
                line.Points = points
                line.Stroke = dark
                line.StrokeThickness = 1.0
                canvas.Children.Add(line)
            elif shape['kind'] == 'text':
                block = TextBlock()
                block.Text = shape['text']
                block.FontSize = max(5.0, shape['height'] * k * 1.25)
                block.Foreground = self._brush('#0B5394')
                left, bottom = cd_dxf.text_box(shape)[0], cd_dxf.text_box(shape)[3]
                corner = to((left, bottom))
                Canvas.SetLeft(block, corner.X)
                Canvas.SetTop(block, corner.Y)
                if shape['angle']:
                    block.RenderTransform = RotateTransform(-shape['angle'])
                canvas.Children.Add(block)

    # ------------------------------------------------------------------ 4 create
    def create_click(self, sender, args):
        """Create - every error goes to the log and the report box (it must never fail silently)."""
        trace(u'create pressed')
        try:
            self._create()
        except Exception:
            text = traceback.format_exc()
            trace(u'create FAILED:\n' + text)
            self.report_tb.Text = u'Create failed - nothing or only part was made:\n' + text
            self._status(u'Create failed - see the report and the log.')

    def _create(self):
        chosen = []
        for detail, box, name, row in self.rows:
            if box.IsChecked:
                detail.name = (name.Text or detail.name).strip() or detail.name
                chosen.append(detail)
        if not chosen:
            return
        self._remember()
        options = cd_revit.Options(scale=cd_revit.SCALES[self.scale_cb.SelectedIndex],
                                   line_style=self.styles[self.style_cb.SelectedIndex][1] if self.styles else None,
                                   text=bool(self.chk_text.IsChecked), fills=bool(self.chk_fills.IsChecked),
                                   pattern_type=self._fallback_pattern(),
                                   cad_patterns=self.pattern_cb.SelectedIndex == 0,
                                   prefix=self.prefix_tb.Text or u'', units_mm=self.units_mm(),
                                   cad_factor=CAD_FACTORS[self.cad_cb.SelectedIndex],
                                   text_type=self.texts[self.texttype_cb.SelectedIndex - 1][1].Id if self.texttype_cb.SelectedIndex > 0 else None,
                                   single=bool(self.rb_single.IsChecked), single_name=self.single_tb.Text or u'CAD Details')
        self._status(u'Creating {0} drafting view(s) ...'.format(len(chosen)))
        trace(u'creating {0} view(s): scale 1:{1}, CAD 1:{2}, text type {3}, single view {4}'.format(
            len(chosen), options.scale, options.cad_factor, self.texttype_cb.SelectedItem, options.single))
        report = cd_revit.create(self.doc, chosen, options, log=trace)
        self.report_tb.Text = report.text()
        trace(report.text())
        self._status(u'{0} drafting view(s) created - see the report.'.format(len(report.views)) if report.views else
                     u'Nothing created - see the report.')
        if report.views and self.chk_open.IsChecked and getattr(report, 'last_view', None) is not None:
            try:
                revit.uidoc.ActiveView = getattr(report, 'first_view', None) or report.last_view
            except Exception:
                pass

    def close_click(self, sender, args):
        self._remember()
        self.Close()


def main():
    if revit.doc is None or revit.doc.IsFamilyDocument:
        forms.alert(u'Open a project first.', title='CAD Details')
        return
    CadDetailsWindow().ShowDialog()

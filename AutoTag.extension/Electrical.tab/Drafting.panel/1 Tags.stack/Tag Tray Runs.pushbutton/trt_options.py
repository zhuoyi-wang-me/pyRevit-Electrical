# -*- coding: utf-8 -*-
"""Tag Tray Runs - the window with the previews.

Shows each arrangement (trt_revit.Option) as a drawing: the hatched area, the walls, the trays,
the tag texts as boxes and their leaders. Click one and press "Create this one" (or double click).
Nothing is in the model while this window is open. A card can be enlarged with "Larger".
"""
import clr
for _name in ('PresentationFramework', 'PresentationCore', 'WindowsBase', 'System.Xaml'):
    clr.AddReference(_name)

from System.Windows import (CornerRadius, FontWeights, HorizontalAlignment, Point, TextTrimming, TextWrapping, Thickness,
                            Window, WindowStartupLocation)
from System.Windows.Controls import Border, Button, Canvas, ScrollBarVisibility, ScrollViewer, StackPanel, TextBlock, WrapPanel
from System.Windows.Controls import Orientation
from System.Windows.Media import BrushConverter, DoubleCollection, LineSegment, PathFigure, PathGeometry
from System.Windows.Shapes import Path, Rectangle

import trt_layout

CARD_W, CARD_H = 580.0, 360.0
ACCENT, RED, PALE = '#005FB8', '#C42B1C', '#E5E5E5'
_brush = BrushConverter().ConvertFromString


def _bounds(option):
    d = option.drawing or {}
    if d.get('crop') is not None:
        return d['crop']
    us, vs = [], []
    for a, b, half in d.get('trays', []):
        us += [a[0], b[0]]
        vs += [a[1], b[1]]
    for p in option.placements:
        us.append(p.centre[0])
        vs.append(p.centre[1])
    if not us:
        return (0.0, 0.0, 1.0, 1.0)
    return (min(us), min(vs), max(us), max(vs))


def draw(option, width=CARD_W, height=CARD_H):
    """A Canvas with the picture of the arrangement."""
    canvas = Canvas()
    canvas.Width, canvas.Height = width, height
    canvas.ClipToBounds = True
    canvas.Background = _brush('#FFFFFF')
    low_u, low_v, high_u, high_v = _bounds(option)
    span_u, span_v = max(high_u - low_u, 1e-6), max(high_v - low_v, 1e-6)
    margin = 6.0
    k = min((width - 2 * margin) / span_u, (height - 2 * margin) / span_v)
    left, top = (width - span_u * k) / 2.0, (height - span_v * k) / 2.0

    def px(p):
        return Point(left + (p[0] - low_u) * k, top + (high_v - p[1]) * k)      # v grows upwards, the screen downwards

    def path(polylines, stroke, thickness, fill=None, closed=False, dashed=False):
        geometry = PathGeometry()
        for points in polylines:
            if len(points) < 2:
                continue
            figure = PathFigure()
            figure.StartPoint = px(points[0])
            figure.IsClosed, figure.IsFilled = closed, fill is not None
            for p in points[1:]:
                figure.Segments.Add(LineSegment(px(p), True))
            geometry.Figures.Add(figure)
        shape = Path()
        shape.Data = geometry
        shape.Stroke, shape.StrokeThickness = _brush(stroke), thickness
        if fill is not None:
            shape.Fill = _brush(fill)
        if dashed:
            shape.StrokeDashArray = DoubleCollection([4.0, 3.0])
        canvas.Children.Add(shape)

    d = option.drawing or {}
    for area in d.get('areas', []):                                         # the hatched area
        path(area.loops, '#C8C8C8', 0.6, fill='#EFEFEF', closed=True)
    if d.get('limits') is not None:
        l, b, r, t = d['limits']
        path([[(l, b), (r, b), (r, t), (l, t), (l, b)]], '#B0B0B0', 0.8, dashed=True)
    path([[a, b] for a, b, half in d.get('walls', [])], '#3A3A3A', 1.0)        # walls
    path([[a, b] for a, b, half in d.get('trays', [])], '#9A9A9A', 2.2)        # trays
    leaders, boxes = [], []
    for p in option.placements:
        w, h = p.piece.size
        if p.arrow is not None:
            leaders.append(trt_layout.leader_points(p.centre, p.corner, p.arrow, w))
        l, b, r, t = trt_layout.box_at(p.centre, w, h)
        boxes.append([(l, b), (r, b), (r, t), (l, t), (l, b)])
    path(leaders, RED, 0.8)
    path(boxes, ACCENT, 0.8, fill='#E6F0FA', closed=True)
    return canvas


class OptionsWindow(Window):
    def __init__(self, options, owner=None):
        self.options = list(options)
        self.chosen = None
        self.selected = None
        self.cards = []
        self.Title = 'Tag Tray Runs - choose an arrangement'
        self.Width, self.Height = 1320.0, 960.0
        self.MinWidth, self.MinHeight = 700.0, 500.0
        self.Background = _brush('#F3F3F3')
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen
        if owner is not None:
            try:
                self.Owner = owner
                self.Resources.MergedDictionaries.Add(owner.Resources)        # the buttons look as in the main window
                self.FontFamily, self.FontSize = owner.FontFamily, owner.FontSize
            except Exception:
                pass
        root = StackPanel()
        root.Margin = Thickness(20, 16, 20, 16)
        heading = TextBlock()
        heading.Text = u'Choose an arrangement'
        heading.FontSize, heading.FontWeight = 22.0, FontWeights.SemiBold
        root.Children.Add(heading)
        caption = TextBlock()
        caption.Text = (u'Blue boxes are the tag texts, red lines the leaders, grey the trays, black the walls, the light grey area the hatch. '
                        u'Nothing is in the model yet. Click the one you want (double click to create at once), then "Create this one".')
        caption.Foreground = _brush('#5C5C5C')
        caption.TextWrapping = TextWrapping.Wrap
        caption.Margin = Thickness(0, 2, 0, 12)
        root.Children.Add(caption)
        wrap = WrapPanel()
        wrap.Orientation = Orientation.Horizontal
        for option in self.options:
            wrap.Children.Add(self._card(option))
        scroller = ScrollViewer()
        scroller.VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        scroller.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled
        scroller.Height = 760.0
        scroller.Content = wrap
        root.Children.Add(scroller)
        buttons = StackPanel()
        buttons.Orientation = Orientation.Horizontal
        buttons.HorizontalAlignment = HorizontalAlignment.Right
        buttons.Margin = Thickness(0, 12, 0, 0)
        self.btnLarge = Button()
        self.btnLarge.Content = u'Larger'
        self.btnLarge.Height, self.btnLarge.MinWidth = 32.0, 90.0
        self.btnLarge.Margin = Thickness(0, 0, 8, 0)
        self.btnLarge.Click += lambda sender, args: self.enlarge()
        self.btnCreate = Button()
        self.btnCreate.Content = u'Create this one'
        self.btnCreate.Height, self.btnCreate.MinWidth = 32.0, 150.0
        try:
            self.btnCreate.Style = self.FindResource('Accent')
        except Exception:
            pass
        self.btnCreate.Click += lambda sender, args: self.accept()
        cancel = Button()
        cancel.Content = u'Cancel'
        cancel.Height, cancel.MinWidth = 32.0, 90.0
        cancel.Margin = Thickness(8, 0, 0, 0)
        cancel.IsCancel = True
        cancel.Click += lambda sender, args: self.Close()
        for button in (self.btnLarge, self.btnCreate, cancel):
            buttons.Children.Add(button)
        root.Children.Add(buttons)
        self.Content = root
        best = [option for option in self.options if option.best]
        self.select((best or self.options)[0] if self.options else None)

    def _card(self, option):
        card = Border()
        card.Background = _brush('White')
        card.CornerRadius = CornerRadius(8)
        card.BorderThickness = Thickness(2)
        card.BorderBrush = _brush(PALE)
        card.Padding = Thickness(10)
        card.Margin = Thickness(0, 0, 12, 12)
        body = StackPanel()
        title = TextBlock()
        title.Text = option.title + (u'     - recommended' if option.best else u'')
        title.FontWeight = FontWeights.SemiBold
        title.TextTrimming = TextTrimming.CharacterEllipsis
        title.Width = CARD_W
        if option.best:
            title.Foreground = _brush('#0F7B0F')
        body.Children.Add(title)
        body.Children.Add(draw(option))
        summary = TextBlock()
        summary.Text = option.summary()
        summary.Foreground = _brush('#5C5C5C')
        summary.FontSize = 12.0
        summary.Width = CARD_W
        summary.TextTrimming = TextTrimming.CharacterEllipsis
        body.Children.Add(summary)
        card.Child = body
        card.MouseLeftButtonDown += lambda sender, args: self.clicked(option, args)
        self.cards.append((card, option))
        return card

    def clicked(self, option, args):
        self.select(option)
        try:
            if args.ClickCount >= 2:
                self.accept()
        except Exception:
            pass

    def select(self, option):
        self.selected = option
        for card, one in self.cards:
            card.BorderBrush = _brush(ACCENT if one is option else PALE)
        self.btnCreate.IsEnabled = self.btnLarge.IsEnabled = option is not None

    def enlarge(self):
        """The selected arrangement in a large window, to look at the details."""
        if self.selected is None:
            return
        big = Window()
        big.Title = u'Tag Tray Runs - ' + self.selected.title
        big.Width, big.Height = 1500.0, 1000.0
        big.WindowStartupLocation = WindowStartupLocation.CenterScreen
        big.Owner = self
        scroller = ScrollViewer()
        scroller.HorizontalScrollBarVisibility = scroller.VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        scroller.Content = draw(self.selected, 2800.0, 1900.0)
        big.Content = scroller
        big.ShowDialog()

    def accept(self):
        if self.selected is not None:
            self.chosen = self.selected.key
            self.Close()


def choose(options, owner=None):
    """The key of the arrangement the user wants to create; None when he cancelled."""
    window = OptionsWindow(options, owner)
    window.ShowDialog()
    return window.chosen

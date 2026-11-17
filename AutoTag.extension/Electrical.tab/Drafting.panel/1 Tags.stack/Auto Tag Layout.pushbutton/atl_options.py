# -*- coding: utf-8 -*-
"""Auto Tag Layout - the window with the previews.

Shows each possible arrangement (atl_layout.Option) as a small drawing: the crop region,
the elements, the tag texts as boxes and the leaders. The user clicks the one he likes and
presses "Create this one". Nothing is in the model while this window is open.
"""
import clr
for _name in ('PresentationFramework', 'PresentationCore', 'WindowsBase', 'System.Xaml'):
    clr.AddReference(_name)

from System.Windows import (CornerRadius, FontWeights, HorizontalAlignment, Point, TextTrimming, TextWrapping, Thickness, VerticalAlignment,
                            Window, WindowStartupLocation)
from System.Windows.Controls import Border, Button, Canvas, ScrollBarVisibility, ScrollViewer, StackPanel, TextBlock, WrapPanel
from System.Windows.Controls import Orientation
from System.Windows.Media import BrushConverter, DoubleCollection, PointCollection
from System.Windows.Shapes import Ellipse, Line, Polygon, Polyline, Rectangle

import atl_layout

CARD_W, CARD_H = 360.0, 250.0             # the drawing inside a card
ACCENT, RED, GREY, PALE = '#005FB8', '#C42B1C', '#8A8A8A', '#E5E5E5'
_brush = BrushConverter().ConvertFromString


def _bounds(option, view_box, limits):
    """(u low, v low, u high, v high) of everything a preview shows."""
    us, vs = [], []
    for item in getattr(option, 'fixed', []):               # tags beside their element / inside their room
        box = item.box
        us += [box[0], box[2]]
        vs += [box[1], box[3]]
        if item.rect is not None:
            us += [item.rect[0], item.rect[2]]
            vs += [item.rect[1], item.rect[3]]
    for item in option.items:
        box = item.box
        us += [box[0], box[2]]
        vs += [box[1], box[3]]
        for u, v in item.leader:
            us.append(u)
            vs.append(v)
        for u, v in (item.outline or []):
            us.append(u)
            vs.append(v)
        if item.seg is not None:
            us += [item.seg[0][0], item.seg[1][0]]
            vs += [item.seg[0][1], item.seg[1][1]]
    for box in (view_box, limits):
        if box is not None and None not in box:
            us += [box[0], box[2]]
            vs += [box[1], box[3]]
    if not us:
        return (0.0, 0.0, 1.0, 1.0)
    return (min(us), min(vs), max(us), max(vs))


def draw(option, view_box=None, limits=None, width=CARD_W, height=CARD_H):
    """A Canvas with the picture of the arrangement."""
    canvas = Canvas()
    canvas.Width, canvas.Height = width, height
    canvas.ClipToBounds = True
    low_u, low_v, high_u, high_v = _bounds(option, view_box, limits)
    span_u, span_v = max(high_u - low_u, 1e-6), max(high_v - low_v, 1e-6)
    margin = 8.0
    scale = min((width - 2 * margin) / span_u, (height - 2 * margin) / span_v)
    left = (width - span_u * scale) / 2.0
    top = (height - span_v * scale) / 2.0

    def px(u, v):
        return left + (u - low_u) * scale, top + (high_v - v) * scale       # v grows upwards, the screen downwards

    def rectangle(box, stroke, fill=None, dashed=False, thickness=1.0):
        (x1, y1), (x2, y2) = px(box[0], box[3]), px(box[2], box[1])
        shape = Rectangle()
        shape.Width, shape.Height = max(x2 - x1, 1.0), max(y2 - y1, 1.0)
        shape.Stroke, shape.StrokeThickness = _brush(stroke), thickness
        if fill:
            shape.Fill = _brush(fill)
        if dashed:
            shape.StrokeDashArray = DoubleCollection([4.0, 3.0])
        Canvas.SetLeft(shape, x1)
        Canvas.SetTop(shape, y1)
        canvas.Children.Add(shape)

    if limits is not None and None not in limits:
        rectangle(limits, '#B0B0B0', dashed=True)                             # the annotation crop
    if view_box is not None and None not in view_box:
        rectangle(view_box, '#9A9A9A', '#FAFAFA')                             # the crop region
    for item in option.items:                                                # the elements
        if item.outline:
            shape = Polygon()
            points = PointCollection()
            for u, v in item.outline:
                x, y = px(u, v)
                points.Add(Point(x, y))
            shape.Points = points
            shape.Fill, shape.Stroke, shape.StrokeThickness = _brush('#CFCFCF'), _brush(GREY), 0.8
            canvas.Children.Add(shape)
        elif item.seg is not None:
            line = Line()
            (line.X1, line.Y1), (line.X2, line.Y2) = px(*item.seg[0]), px(*item.seg[1])
            line.Stroke, line.StrokeThickness = _brush('#B5B5B5'), 3.0
            canvas.Children.Add(line)
    for item in getattr(option, 'fixed', []):                                # tags without leader: green
        if item.rect is not None and item.key is not None and getattr(item.key, 'mode', None) != 'room':
            rectangle(item.rect, GREY, '#DCDCDC', thickness=0.6)
        rectangle(item.box, '#0F7B0F', '#DFF6DD', thickness=0.8)
    for item in option.items:                                                # leaders, arrows, tags
        leader = Polyline()
        points = PointCollection()
        for u, v in item.leader:
            x, y = px(u, v)
            points.Add(Point(x, y))
        leader.Points = points
        leader.Stroke, leader.StrokeThickness = _brush(RED), 1.0
        canvas.Children.Add(leader)
        x, y = px(item.u, item.v)
        dot = Ellipse()
        dot.Width = dot.Height = 4.0
        dot.Fill = _brush(RED)
        Canvas.SetLeft(dot, x - 2.0)
        Canvas.SetTop(dot, y - 2.0)
        canvas.Children.Add(dot)
        rectangle(item.box, ACCENT, '#E6F0FA', thickness=0.8)
    return canvas


class OptionsWindow(Window):
    def __init__(self, options, view_box=None, limits=None, owner=None, note=u''):
        self.options = list(options)
        self.chosen = None                 # the key of the option to create, after "Create this one"
        self.selected = None
        self.cards = []                    # (Border, Option)
        self.Title = 'Auto Tag Layout - choose an arrangement'
        self.Width, self.Height = 1290.0, 780.0
        self.MinWidth, self.MinHeight = 520.0, 420.0
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
        caption.Text = (u'Blue boxes are the tag texts, red lines the leaders, grey the elements, the dashed line the annotation crop. '
                        u'Green boxes are tags beside their element or inside their room (the same in every arrangement). '
                        u'Nothing is in the model yet. Click the one you want, then Create.' + (u'  ' + note if note else u''))
        caption.Foreground = _brush('#5C5C5C')
        caption.TextWrapping = TextWrapping.Wrap
        caption.Margin = Thickness(0, 2, 0, 12)
        root.Children.Add(caption)

        wrap = WrapPanel()
        wrap.Orientation = Orientation.Horizontal
        for option in self.options:
            wrap.Children.Add(self._card(option, view_box, limits))
        scroller = ScrollViewer()
        scroller.VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        scroller.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled
        scroller.Height = 590.0
        scroller.Content = wrap
        root.Children.Add(scroller)

        buttons = StackPanel()
        buttons.Orientation = Orientation.Horizontal
        buttons.HorizontalAlignment = HorizontalAlignment.Right
        buttons.Margin = Thickness(0, 12, 0, 0)
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
        buttons.Children.Add(self.btnCreate)
        buttons.Children.Add(cancel)
        root.Children.Add(buttons)
        self.Content = root
        best = [option for option in self.options if option.best]
        self.select((best or self.options)[0] if self.options else None)

    def _card(self, option, view_box, limits):
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
        body.Children.Add(draw(option, view_box, limits))
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
            if args.ClickCount >= 2:                                         # a double click creates at once
                self.accept()
        except Exception:
            pass

    def select(self, option):
        self.selected = option
        for card, one in self.cards:
            card.BorderBrush = _brush(ACCENT if one is option else PALE)
        self.btnCreate.IsEnabled = option is not None

    def accept(self):
        if self.selected is not None:
            self.chosen = self.selected.key
            self.Close()


def choose(options, view_box=None, limits=None, owner=None, note=u''):
    """The key of the arrangement the user wants to create; None when he cancelled."""
    window = OptionsWindow(options, view_box, limits, owner, note)
    window.ShowDialog()
    return window.chosen

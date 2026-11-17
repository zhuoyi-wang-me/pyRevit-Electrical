# -*- coding: utf-8 -*-
"""Breaker Check - the window in which the panels are chosen (search, tick, select all)."""
import os

import clr
for _name in ('PresentationFramework', 'PresentationCore', 'WindowsBase', 'System.Xaml'):
    clr.AddReference(_name)

from System.Windows import Visibility
from System.Windows.Controls import CheckBox

from pyrevit import forms


class PanelWindow(forms.WPFWindow):
    def __init__(self, names, ticked=()):
        forms.WPFWindow.__init__(self, os.path.join(os.path.dirname(__file__), 'panels.xaml'))
        self.names = sorted(names)
        self.state = dict((name, name in ticked) for name in self.names)     # name -> ticked, also for hidden rows
        self.result = None                                                   # the chosen names after "Check breakers"
        self.txtSearch.TextChanged += lambda s, a: self.fill()
        self.btnAll.Click += lambda s, a: self.set_visible(True)
        self.btnNone.Click += lambda s, a: self.set_visible(False)
        self.btnOk.Click += lambda s, a: self.accept()
        self.btnCancel.Click += lambda s, a: self.Close()
        self.fill()
        self.txtSearch.Focus()

    def visible_names(self):
        """The names that contain every word typed in the search field."""
        words = self.txtSearch.Text.lower().split()
        return [name for name in self.names if all(word in name.lower() for word in words)]

    def fill(self):
        self.lblHint.Visibility = Visibility.Collapsed if self.txtSearch.Text else Visibility.Visible
        self.lstPanels.Items.Clear()
        for name in self.visible_names():
            box = CheckBox()
            box.Content = name
            box.IsChecked = self.state[name]
            box.Click += self.on_tick
            self.lstPanels.Items.Add(box)
        self.count()

    def on_tick(self, sender, args):
        self.state[sender.Content] = bool(sender.IsChecked)
        self.count()

    def set_visible(self, value):
        """Select all / Clear work on the rows that the search shows."""
        for name in self.visible_names():
            self.state[name] = value
        self.fill()

    def count(self):
        chosen = len([name for name in self.names if self.state[name]])
        shown = self.lstPanels.Items.Count
        text = u'{0} of {1} selected'.format(chosen, len(self.names))
        if shown != len(self.names):
            text += u'  -  {0} shown'.format(shown)
        self.lblCount.Text = text
        self.btnOk.IsEnabled = chosen > 0

    def accept(self):
        self.result = [name for name in self.names if self.state[name]]
        self.Close()


def choose(names, ticked=(), owner=None):
    """The names the user ticked, or None when the window was cancelled."""
    window = PanelWindow(names, ticked)
    if owner is not None:
        window.Owner = owner
    window.ShowDialog()
    return window.result

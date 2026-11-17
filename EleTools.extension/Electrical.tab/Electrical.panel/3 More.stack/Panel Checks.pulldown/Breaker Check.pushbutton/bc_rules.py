# -*- coding: utf-8 -*-
"""Breaker Check - the window in which the breaker rating of each load type is set."""
import os

import clr
for _name in ('PresentationFramework', 'PresentationCore', 'WindowsBase', 'System.Xaml', 'System.Data'):
    clr.AddReference(_name)

from System import String
from System.Data import DataTable
from System.Windows.Controls import DataGridEditingUnit

from pyrevit import forms

import bc_check

TYPE, RATING = 'Load type', 'Minimum breaker A'


class RulesWindow(forms.WPFWindow):
    def __init__(self, rules, found, styles=None):
        """rules: [bc_check.Rule] to start from; found: [(classification, number of circuits)]."""
        forms.WPFWindow.__init__(self, os.path.join(os.path.dirname(__file__), 'rules.xaml'))
        if styles is not None:
            self.Resources.MergedDictionaries.Add(styles)      # the Windows 11 styles of the main window
        self.result = None                                     # [Rule] after "Use these"
        self.found = list(found)
        self.table = DataTable()
        self.table.Columns.Add(TYPE, clr.GetClrType(String))
        self.table.Columns.Add(RATING, clr.GetClrType(String))
        for rule in rules:
            self.add(rule.text, u'{0:g}'.format(rule.rating_a))
        self.gridRules.ItemsSource = self.table.DefaultView
        for name, count in self.found:
            self.lstFound.Items.Add(u'{0}   ({1})'.format(name, count))
        self.btnAdd.Click += lambda s, a: self.on_add()
        self.btnDelete.Click += lambda s, a: self.on_delete()
        self.btnOk.Click += lambda s, a: self.accept()
        self.btnCancel.Click += lambda s, a: self.Close()

    def add(self, text, rating=u''):
        row = self.table.NewRow()
        row[TYPE], row[RATING] = text, rating
        self.table.Rows.Add(row)

    def commit(self):
        try:
            self.gridRules.CommitEdit(DataGridEditingUnit.Row, True)
        except Exception:
            pass

    def on_add(self):
        """Put the picked classifications in the list; the rating is typed by the user."""
        self.commit()
        have = set((u'{0}'.format(row[TYPE])).strip().lower() for row in self.table.Rows)
        for index in sorted(self.lstFound.Items.IndexOf(item) for item in self.lstFound.SelectedItems):
            name = self.found[index][0]
            if name.lower() not in have:
                self.add(name)
                have.add(name.lower())

    def on_delete(self):
        self.commit()
        view = self.gridRules.SelectedItem
        try:
            view.Row.Delete()
            self.table.AcceptChanges()
        except Exception:
            pass                                               # nothing selected, or the empty last row

    def read(self):
        """([Rule], problem or None) from the table; empty rows are skipped."""
        rules, seen = [], set()
        for row in self.table.Rows:
            text = (u'{0}'.format(row[TYPE]) if row[TYPE] is not None else u'').strip()
            rating_text = (u'{0}'.format(row[RATING]) if row[RATING] is not None else u'').strip()
            if not text and not rating_text:
                continue
            if not text:
                return None, u'A row has a rating but no load type.'
            try:
                rating = bc_check.parse_number(rating_text)
            except ValueError:
                rating = None
            if rating is None or rating <= 0:
                return None, u'"{0}": enter the minimum breaker rating in A, e.g. 16.'.format(text)
            if text.lower() in seen:
                return None, u'"{0}" is in the list twice.'.format(text)
            seen.add(text.lower())
            rules.append(bc_check.Rule(text, rating))
        return rules, None

    def accept(self):
        self.commit()
        rules, problem = self.read()
        if problem:
            self.lblProblem.Text = problem
            return
        self.result = rules
        self.Close()


def edit(rules, found, owner=None):
    """The rules the user confirmed, or None when the window was cancelled."""
    window = RulesWindow(rules, found, owner.Resources if owner is not None else None)
    if owner is not None:
        window.Owner = owner
    window.ShowDialog()
    return window.result

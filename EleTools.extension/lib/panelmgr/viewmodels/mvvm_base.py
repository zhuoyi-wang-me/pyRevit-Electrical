# -*- coding: utf-8 -*-
"""
mvvm_base.py
=============
Minimal MVVM plumbing for IronPython + WPF data binding:

    ObservableObject  - implements INotifyPropertyChanged so WPF
                        bindings refresh automatically when a property
                        changes.
    RelayCommand       - implements ICommand so XAML can bind
                        Button.Command directly to a ViewModel method,
                        including CanExecute-driven enable/disable
                        (e.g. disabling "Next" until a panel is
                        selected).

This mirrors the standard WPF MVVM Toolkit pattern, reimplemented
without external NuGet dependencies since pyRevit scripts run inside
IronPython against the CLR that ships with Revit.
"""

import clr
clr.AddReference("System")
clr.AddReference("WindowsBase")

from System.ComponentModel import INotifyPropertyChanged
from System.Windows.Input import ICommand


class ObservableObject(INotifyPropertyChanged):
    """Base class providing INotifyPropertyChanged for ViewModels."""

    def __init__(self):
        self._property_changed_handlers = []

    # --- INotifyPropertyChanged -------------------------------------------------
    def add_PropertyChanged(self, handler):
        self._property_changed_handlers.append(handler)

    def remove_PropertyChanged(self, handler):
        if handler in self._property_changed_handlers:
            self._property_changed_handlers.remove(handler)

    def raise_property_changed(self, prop_name):
        from System.ComponentModel import PropertyChangedEventArgs
        args = PropertyChangedEventArgs(prop_name)
        for handler in list(self._property_changed_handlers):
            handler.Invoke(self, args)

    # Convenience for subclasses: set a backing field and notify only
    # if the value actually changed.
    def set_field(self, field_name, value, prop_name):
        current = getattr(self, field_name, None)
        if current == value:
            return False
        setattr(self, field_name, value)
        self.raise_property_changed(prop_name)
        return True


class RelayCommand(ICommand):
    """
    ICommand implementation binding a Button/MenuItem directly to a
    Python callable. `can_execute_fn` is optional; when omitted the
    command is always enabled.
    """

    def __init__(self, execute_fn, can_execute_fn=None):
        self._execute_fn = execute_fn
        self._can_execute_fn = can_execute_fn
        self._can_execute_changed_handlers = []

    def add_CanExecuteChanged(self, handler):
        self._can_execute_changed_handlers.append(handler)

    def remove_CanExecuteChanged(self, handler):
        if handler in self._can_execute_changed_handlers:
            self._can_execute_changed_handlers.remove(handler)

    def CanExecute(self, parameter):
        if self._can_execute_fn is None:
            return True
        try:
            return bool(self._can_execute_fn(parameter))
        except Exception:
            return False

    def Execute(self, parameter):
        self._execute_fn(parameter)

    def raise_can_execute_changed(self):
        from System import EventArgs
        for handler in list(self._can_execute_changed_handlers):
            handler.Invoke(self, EventArgs.Empty)

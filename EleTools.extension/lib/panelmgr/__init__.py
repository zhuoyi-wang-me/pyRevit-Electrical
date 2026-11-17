# -*- coding: utf-8 -*-
"""
panelmgr
========
Shared library for the Panel Schedule Manager pyRevit extension.

Package layout:
    config.py            Persisted user settings (JSON, %appdata%)
    logger.py             Central logging configuration
    models.py             Plain data classes (no Revit API dependency)
    revit_utils.py         Revit API access layer (documents, panels,
                          electrical systems, panel schedule views)
    services/            Business logic, orchestrated by the ViewModel
    viewmodels/           MVVM ViewModels (INotifyPropertyChanged)
    views/                XAML view definitions
    resources/            Shared XAML styles / templates

`models.py` and the `services` package are written to be Revit-API
free wherever possible so the load-balancing / spare-space math can be
unit tested with plain CPython (see README.md "Testing Strategy").
"""

__version__ = "1.0.0"

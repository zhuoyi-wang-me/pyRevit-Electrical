# -*- coding: utf-8 -*-
"""
Electrical Calculator - always-open (modeless) calculation window.

Read-only except the Isolator tab's 'Write ratings to fixtures' (asks first,
one Undo step), so the window can stay open while you keep working. Clicking the button again brings the open window to the front.

Tabs: From model (loads of selected elements) | Load current | Volt drop |
      Cable & breaker | PF correction | Fault level
Model reading: lib/eleccalc/revit_loads.py (via ExternalEvent - modeless window).
Calculation core: lib/eleccalc/calc.py (unit-tested in tests/ElectricalCalculator).
"""
__title__ = 'Electrical\nCalculator'
__author__ = 'BHEEM'

# Keep this script's IronPython engine alive after the script returns -
# the modeless window's button / text-box handlers run in it.
__persistentengine__ = True

import os

from pyrevit import forms

try:
    from eleccalc.window import show
    show(os.path.join(os.path.dirname(__file__), 'ui.xaml'))
except Exception as ex:
    forms.alert('Electrical Calculator could not open:\n{0}'.format(ex), title='Electrical Calculator')

# -*- coding: utf-8 -*-
"""Earthing Route Generator - draws 2D earthing lines in the active floor plan.

Deliberately tiny: the window stays open after this script ends, and pyRevit
empties a button script's global names at that moment. The real code lives in
er_window.py (window), er_revit.py (Revit) and er_geometry.py (routing), whose
names are never cleared.
"""
# keeps the IronPython engine (and the imported modules) alive for the window
__persistentengine__ = True

import er_window

er_window.main()

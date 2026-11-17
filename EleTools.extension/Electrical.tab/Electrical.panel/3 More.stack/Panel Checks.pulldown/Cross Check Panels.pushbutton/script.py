# -*- coding: utf-8 -*-
"""Cross Check MEP Elements vs Detail Items (pyRevit, modeless WPF).

This file is deliberately tiny. The window stays open after this script has
finished, and pyRevit empties a button script's global names when it ends -
every name the window's buttons needed was then gone ("name ... is not
defined", or a button that silently did nothing). The real code therefore
lives in crosscheck_window.py: the names of an imported module are never
cleared.
"""
# Keeps this script's IronPython engine (and the imported module) alive for
# as long as the modeless window is open.
__persistentengine__ = True

import crosscheck_window

crosscheck_window.main()

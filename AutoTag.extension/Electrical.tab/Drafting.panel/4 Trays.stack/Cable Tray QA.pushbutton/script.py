# -*- coding: utf-8 -*-
"""Cable Tray QA/QC - pyRevit button script.

This file only STARTS the tool. The window and everything it does are in
qa_window.py, on purpose: the window is modeless and outlives this script, and
pyRevit clears a button script's global names when the script ends. Code that
runs later (a button click in the window) must therefore live in an imported
module, whose names are kept. Do not move the window class back into this file.
"""
__title__ = 'Cable Tray\nQA/QC'
__doc__ = ('Checks the cable tray calculation list (Excel) against the cable tray '
           'annotations on the Revit sheets: missing, extra, width / height '
           'mismatch, duplicate and invalid annotations. Read-only.')
__persistentengine__ = True          # keeps the engine alive for the modeless window

import qa_window

if __name__ == '__main__':
    qa_window.main()

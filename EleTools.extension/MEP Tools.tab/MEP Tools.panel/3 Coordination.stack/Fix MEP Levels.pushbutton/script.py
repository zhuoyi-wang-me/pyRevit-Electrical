# -*- coding: utf-8 -*-
"""Fix MEP Levels.

Re-associates MEP elements with the project level that matches their real
physical elevation and recalculates the offset so that the element does not
move by a single internal unit.

Shift+Click opens this help text instead of running the tool.
"""

__title__ = 'Fix MEP\nLevels'
__author__ = 'BHEEM'
__doc__ = ('Corrects the Reference Level / Level of MEP elements based on '
           'their actual elevation.\n\n'
           'Offsets are recalculated automatically so that nothing moves. '
           'Every element is verified after the change and rolled back if it '
           'is displaced.\n\n'
           'Supports selected elements, the active view or the whole model, '
           'with a category picker, a preview dialog and a CSV report.')
__min_revit_ver__ = 2022
__max_revit_ver__ = 2026
# No __context__: the button is enabled whenever a project document is open.

from pyrevit import revit

from mepleveler import main

# ``revit.doc`` / ``revit.uidoc`` are the pyRevit shortcuts for the active
# document and UI document.
main.run(revit.doc, revit.uidoc)

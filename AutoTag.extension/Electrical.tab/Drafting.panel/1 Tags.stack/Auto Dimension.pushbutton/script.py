# -*- coding: utf-8 -*-
"""Auto Dimension - chain dimensions for electrical equipment and fixtures, in plans and sections.

Along each row of equipment:   wall | equipment | gap | equipment | ... | wall
In sections also the height of the equipment and the distance from the level to its top.
The window stays open while you work in Revit; the model is changed only by
"Create dimensions" (one transaction: one Undo removes all dimensions of a run).
    adm_window.py   the window (modeless)
    adm_layout.py   rows, chains and the walls that end them (no Revit)
    adm_revit.py    reading the view, finding the references, creating the dimensions
"""
__persistentengine__ = True          # the window outlives this script: keep its engine alive

import adm_window

adm_window.main()

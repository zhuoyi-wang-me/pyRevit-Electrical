# -*- coding: utf-8 -*-
"""Cable Tray Layout - drafting views of cable tray cross-sections from a cable schedule.

Kept tiny on purpose. The work is in:
    ctl_window.py   the window
    ctl_input.py    reading and checking the Excel / CSV file
    ctl_calc.py     gaps, layout width, 20 % future space
    ctl_drawing.py  what is drawn (lines, circles, texts)
    ctl_revit.py    the drafting view, detail lines and text notes
"""
import ctl_window

ctl_window.main()

# -*- coding: utf-8 -*-
"""CAD Details - typical installation details from a DWG -> native Revit drafting views.

The code lives in cd_window.py (window), cd_convert.py (DWG -> DXF with AutoCAD's console), cd_dxf.py (reading),
cd_split.py (one sheet -> its details) and cd_revit.py (the drafting views). Cached copies are dropped first, so
an edited module is always the one that runs.
"""
import sys

for _name in ('cd_window', 'cd_revit', 'cd_split', 'cd_dxf', 'cd_convert'):
    sys.modules.pop(_name, None)

import cd_window

cd_window.main()
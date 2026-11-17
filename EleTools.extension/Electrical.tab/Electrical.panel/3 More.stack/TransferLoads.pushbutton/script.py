# -*- coding: utf-8 -*-
__title__ = "Load Transfer"
__doc__ = """Opens the Load Transfer window: pick source categories, the
target Detail Item family, the matching parameter, preview which elements
pair up (ambiguous matches are flagged, never guessed), narrow the list to
only the elements you want with the Transfer Filter (e.g. AHU, HRU) or the
per-row Include ticks, then review a full OLD vs NEW table of every value
that would be written (exportable to CSV) before committing the transfer --
with an optional unit multiplier per row.
"""

# The window is MODELESS (Revit stays usable while it is open). pyRevit must keep this script's
# engine alive after the script finishes, or the window's buttons and External Event would stop.
__persistentengine__ = True

import ui

ui.run()

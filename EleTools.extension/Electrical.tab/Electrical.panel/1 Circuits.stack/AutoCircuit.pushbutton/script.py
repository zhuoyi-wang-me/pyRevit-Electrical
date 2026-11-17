# -*- coding: utf-8 -*-
"""
ElecAutoCircuit - pyRevit entry point.

Automatically creates power & lighting circuits by room, load classification,
panel type, distribution region and user-defined circuit rules.

Scope "selected filled region(s) only":
    * select filled regions in a plan view BEFORE clicking the button, or
    * click "Pick Filled Region(s)" in the window - the window closes, you pick
      in the active plan view (Finish / Esc) and the window reopens as it was.
"""
__title__ = 'Auto\nCircuit'
__author__ = 'BHEEM'
__doc__ = ('Automatically create electrical circuits room-by-room, respecting load '
           'classification -> panel type mapping, distribution regions, circuit '
           'limits and panel spare capacity. Select filled regions first to circuit '
           'only inside them.')

import os

from pyrevit import revit, forms

from autocircuit.logger import get_logger, log_file_path
from autocircuit.selection import active_view_is_plan, pick_filled_regions, preselected_filled_regions
from autocircuit.ui import AutoCircuitWindow

logger = get_logger()
doc = revit.doc
uidoc = revit.uidoc

if doc is None or doc.IsFamilyDocument:
    forms.alert('Open a Revit project (not a family) to use Auto Circuit.', exitscript=True)
if doc.IsReadOnly:
    forms.alert('The active document is read-only.', exitscript=True)

XAML = os.path.join(os.path.dirname(__file__), 'AutoCircuit.xaml')

try:
    logger.info('==== ElecAutoCircuit started on "%s" (Revit %s) ====',
                doc.Title, doc.Application.VersionNumber)
    state = None                                          # settings carried across re-opens
    picked = preselected_filled_regions(doc, uidoc) or None
    if picked:
        logger.info('%d pre-selected filled region(s) used as scope', len(picked))

    while True:
        window = AutoCircuitWindow(XAML, doc, uidoc, logger, state=state, picked_region_ids=picked)
        window.ShowDialog()
        if not window.request_pick:
            break
        state = window.pick_state
        if not active_view_is_plan(doc):
            forms.alert('Open a floor / ceiling plan that shows the filled regions, then pick again.\n'
                        'The previous selection is kept.', title='ElecAutoCircuit')
            picked = None
            continue
        picked = pick_filled_regions(doc, uidoc)          # None = Esc -> keep previous selection
        if picked is not None and not picked:
            picked = None
except Exception as ex:
    logger.exception('ElecAutoCircuit crashed')
    forms.alert('ElecAutoCircuit stopped with an error:\n{0}\n\nLog: {1}'.format(ex, log_file_path()))

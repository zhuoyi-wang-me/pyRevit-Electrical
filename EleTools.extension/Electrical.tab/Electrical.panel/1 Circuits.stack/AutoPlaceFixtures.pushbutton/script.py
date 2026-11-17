# -*- coding: utf-8 -*-
"""
Auto-Place Fixtures from Linked Rooms  (v3)
===========================================
pyRevit command (IronPython 2.7) - Revit 2023 / 2024 / 2025 / 2026

WORKFLOW (single window - lib/elecplace/ui_main.xaml)
  Header   choose the architectural link; eligible rooms are counted live.
  Rules    build any number of rules:
             * ON ROOM WALLS: family type + height above FFL + quantity that
               is FIXED or driven by ROOM SIZE (1 per wall length, 1 per
               floor area, or area bands) with min / max + target rooms.
             * WALL NEXT TO EQUIPMENT: a socket / isolator / spur / control
               box on the nearest wall to every selected Mechanical / Plumbing
               equipment or fixture, Duct Accessory (e.g. MSFD) or Pipe
               Accessory - height above FFL or relative to the equipment.
             * ON THE EQUIPMENT: hosted on the equipment face (FCU, BCU...)
               on a chosen side with two offsets (host or linked equipment).
           Types can come from electrical, lighting, device, electrical /
           mechanical equipment and plumbing categories.
  Preview  per room target vs planned + every skip / shortfall reason.
  Options  clearances, duplicate radius, boundary mode, level-based toggle.
  Place    runs the plan inside one TransactionGroup (single Undo).
Rules are auto-restored next session and can be saved as JSON templates.
SHIFT+CLICK still opens the classic settings dialog (config.py).

Revit API references used by this command (see lib/elecplace/* for detail)
  Autodesk.Revit.DB:
    FilteredElementCollector, ElementMulticategoryFilter, Category
    (IsBuiltInCategoryValid), RevitLinkInstance (GetLinkDocument,
    GetTotalTransform), Transform (OfPoint, OfVector, Inverse),
    Architecture.Room (GetBoundarySegments, IsPointInRoom, Area,
    UnboundedHeight, Location, Level, BaseOffset), SpatialElementBoundaryOptions,
    BoundarySegment, Wall, WallKind, HostObjectUtils.GetSideFaces,
    ShellLayerType, PlanarFace.Project, Reference.CreateLinkReference,
    FamilySymbol (Activate, IsActive), Family.FamilyPlacementType,
    Document.Create.NewFamilyInstance, ElementTransformUtils.RotateElement,
    FamilyInstance (FacingOrientation, flipFacing, SuperComponent),
    TransactionGroup, Transaction, SubTransaction, IFailuresPreprocessor,
    UnitUtils, UnitFormatUtils, SpecTypeId, UnitTypeId, ElementId.Value
  pyRevit: forms.WPFWindow, forms.alert, forms.SelectFromList,
    forms.save_file / pick_file, script.get_output, script.get_logger
"""
__title__ = "Auto-Place\nFixtures"
__author__ = 'BHEEM'
__doc__ = ("Place electrical, mechanical and plumbing families on walls of rooms from a "
           "linked architectural model - fixed or room-size based quantities, or next to "
           "equipment. Shift+Click for settings.")

import os
import sys
import time
import traceback

from pyrevit import forms, revit, script

# pyRevit adds <extension>/lib to sys.path automatically. This fallback keeps
# the command working if the folder is copied somewhere unusual.
_LIB = os.path.dirname(os.path.abspath(__file__))
while _LIB and not _LIB.lower().endswith(".extension") and os.path.dirname(_LIB) != _LIB:
    _LIB = os.path.dirname(_LIB)                      # up to the *.extension folder (works inside a .stack)
_LIB = os.path.join(_LIB, "lib")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.append(_LIB)

from elecplace import executor, linkdata, planner, report, ui     # noqa: E402
from elecplace.settings import Settings                            # noqa: E402

logger = script.get_logger()      # honours pyRevit's debug / verbose modes
output = script.get_output()
_RUN = {"ctx": None, "title": u""}   # for the trace log if main() crashes


def main():
    doc = revit.doc
    ctx = report.RunContext()
    _RUN["ctx"] = ctx
    try:
        _RUN["title"] = doc.Title
    except Exception:
        pass

    # ------------------------------------------------------------------
    # 0. Guards
    # ------------------------------------------------------------------
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Open a PROJECT document (not a family) and try again.", exitscript=True)
    if doc.IsReadOnly:
        forms.alert("The active document is read-only.", exitscript=True)

    settings = Settings.load()
    ctx.settings = settings

    links, unloaded = linkdata.get_loaded_links(doc)
    if not links:
        forms.alert("No LOADED Revit links found.",
                    sub_msg="Unloaded links: {}. Load the architectural link and retry."
                    .format(unloaded), exitscript=True)

    symbols_by_cat = planner.collect_placeable_symbols(doc)
    if not symbols_by_cat:
        forms.alert("No placeable family types are loaded.",
                    sub_msg="Load a face-based electrical fixture (or a mechanical / plumbing "
                            "family) and retry.", exitscript=True)

    # ------------------------------------------------------------------
    # 1-5. Main window: link, rules, preview, options (read-only)
    # ------------------------------------------------------------------
    window = ui.MainWindow(doc, links, settings, symbols_by_cat, logger)
    window.ShowDialog()
    res = window.result
    if not res:
        ctx.cancelled = True
        report.write_trace(ctx, _RUN["title"], stage=u"cancelled in window (nothing placed)")
        script.exit()                      # cancelled - nothing changed

    ctx.link = res["link"]
    ctx.rooms = res["rooms"]
    ctx.skipped_rooms = res["skipped_rooms"]
    ctx.rules = res["rules"]
    ctx.plan = res["plan"]

    # ------------------------------------------------------------------
    # 6. Placement (TransactionGroup -> Transaction per room -> SubTransaction)
    # ------------------------------------------------------------------
    try:
        ctx.result = executor.execute(doc, ctx.plan, settings, logger)
    except Exception as ex:
        ctx.fatal_error = ex
        logger.error("Placement aborted, all changes rolled back: {}".format(ex))
        logger.debug(traceback.format_exc())

    # ------------------------------------------------------------------
    # 7. Report
    # ------------------------------------------------------------------
    ctx.t_end = time.time()
    # trace first: it survives even if rendering the report fails
    report.write_trace(ctx, _RUN["title"], stage=u"placed" if ctx.result else u"placement failed")
    report.render(output, ctx)


# Entry point - top-level guard so unexpected errors are logged cleanly.
try:
    main()
except SystemExit:
    raise
except Exception as err:
    logger.error("Auto-Place Fixtures failed: {}".format(err))
    logger.debug(traceback.format_exc())
    if _RUN["ctx"] is not None:
        report.write_trace(_RUN["ctx"], _RUN["title"], stage=u"crashed",
                           error_text=traceback.format_exc())
    forms.alert("Unexpected error:\n{}".format(err),
                sub_msg="Fixtures already placed before the error stay in the model "
                        "(one Undo removes them). Details: {}".format(report.TRACE_FILE))

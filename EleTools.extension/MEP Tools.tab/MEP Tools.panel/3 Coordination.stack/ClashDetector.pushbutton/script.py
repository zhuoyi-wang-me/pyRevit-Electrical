# -*- coding: utf-8 -*-
"""MEP Clash Detector - Version 1.2

Detects hard clashes of MEP services in the ACTIVE FLOOR PLAN view - against
each other and against Architecture / Structure - in the host model AND in
Revit links, highlights them, groups them into clash sets, presents them in
a modeless window and exports them to XLSX.

Checked elements
    MEP  : Cable Trays, Cable Tray Fittings, Ducts, Duct Fittings, Pipes,
           Pipe Fittings (host and/or linked models)
    ARC  : walls, floors, ceilings, roofs, doors, windows, stairs ... (user choice)
    STR  : columns, framing, foundations, structural walls / floors (user choice)
    Rules: MEP vs MEP (fixed category matrix, optional) and MEP vs ARC/STR.
           ARC/STR vs ARC/STR is never checked (not an MEP question).

Sources
    A "Clash Scope" dialog lets the user tick, per model (host + every loaded
    Revit link), whether its MEP and/or its Arch/Struct elements are used, and
    which ARC / STR categories to check. Choices are remembered per model.
    Host elements   : FilteredElementCollector(doc, view.Id) - visible in view.
    Linked elements : link document + BoundingBoxIntersectsFilter built from
                      the view crop box and view range, transformed into link
                      coordinates. Geometry is moved into host coordinates
                      with SolidUtils.CreateTransformed(GetTotalTransform()).

Architecture
    1.  COLLECT      - host: view collector; links: view-region collector.
    2.  BROAD PHASE  - uniform 3D spatial hash + axis aligned bounding boxes.
    3.  NARROW PHASE - BooleanOperationsUtils.ExecuteBooleanOperation(Intersect)
                       on the element solids, clash point = centroid of the
                       intersection solid.
    4.  GROUP        - clashes closer than SET_RADIUS_MM are merged into
                       "clash sets" (single-linkage clustering, 3D).
    5.  REPORT       - OverrideGraphicSettings + MODELESS WPF window + XLSX
                       export + optional Revit Selection Filters per set.

Modeless window
    The window stays open while you work in Revit. It never touches the
    Revit API directly: every model/view action is queued on a
    RevitActionHandler and executed through an ExternalEvent, i.e. inside a
    valid API context. Pure UI work (search, filter, sort, regroup) runs
    locally. The IronPython engine is kept alive (__persistentengine__) so
    the handlers remain valid after the command returns.

Author  : <your name / company>
Version : 1.2.0
Tested  : Revit 2023 / 2024 / 2025 / 2026  (pyRevit 4.8.x and pyRevit 5.x)
Engine  : IronPython 2.7 (pyRevit default). Events are wired in code, not in
          XAML, so the window no longer depends on IronPython's wpf loader.
"""

# ---------------------------------------------------------------------------
# pyRevit bundle metadata (also duplicated in bundle.yaml)
# ---------------------------------------------------------------------------
__title__ = "Clash\nDetector"
__author__ = 'BHEEM'
__doc__ = ("Detects clashes between Cable Trays, Ducts, Pipes and their "
           "fittings inside the ACTIVE floor plan view.")
__min_revit_ver__ = 2023
__max_revit_ver__ = 2026
# REQUIRED for modeless windows: keeps the IronPython engine (and therefore
# the window's event handlers and the ExternalEvent handler) alive after the
# command returns. Without it Revit can crash on the first button click.
__persistentengine__ = True
# NOTE: a '__context__ = "active-floor-plan"' line can be enabled on pyRevit
# versions that support view-type contexts so the button greys out outside of
# plan views. The check is also performed in code (see validate_active_view)
# so the tool stays portable across pyRevit versions.

# ---------------------------------------------------------------------------
# SECTION 1 - IMPORTS
# ---------------------------------------------------------------------------
import os
import re
import json
import math
import traceback
from datetime import datetime

import clr

# .NET references required by the WPF window and the fallback XLSX writer.
# They are wrapped because Revit 2025/2026 run on .NET 8, where some of these
# assemblies are already loaded and AddReference can raise.
for _assembly in ("System", "System.Core", "PresentationCore",
                  "PresentationFramework", "WindowsBase",
                  "System.IO.Compression", "System.IO.Compression.FileSystem"):
    try:
        clr.AddReference(_assembly)
    except Exception:
        pass

from System.Collections.Generic import List                         # noqa: E402
from System.Collections.ObjectModel import ObservableCollection     # noqa: E402
from System.ComponentModel import ListSortDirection                 # noqa: E402
from System import AppDomain, EventHandler, Int64                   # noqa: E402
from System.Diagnostics import Process                              # noqa: E402
from System.Windows import (                                        # noqa: E402
    MessageBox, MessageBoxButton, MessageBoxImage, MessageBoxResult,
    TextTrimming, Thickness, VerticalAlignment, WindowState)
from System.Windows.Controls import (                               # noqa: E402
    CheckBox, DataGridRow, ItemsControl, Orientation, StackPanel, TextBlock)
from System.Windows.Media import Brushes                            # noqa: E402
from System.Windows.Input import Key                                # noqa: E402

# Revit API -----------------------------------------------------------------
from Autodesk.Revit.DB import (                                     # noqa: E402
    BuiltInCategory,
    BuiltInParameter,
    BooleanOperationsType,
    BooleanOperationsUtils,
    BoundingBoxIntersectsFilter,
    Color,
    Element,
    ElementId,
    ElementMulticategoryFilter,
    FilteredElementCollector,
    FillPatternElement,
    FillPatternTarget,
    GeometryInstance,
    Level,
    MEPCurve,
    Options,
    Outline,
    OverrideGraphicSettings,
    PlanViewPlane,
    Reference,
    RevitLinkInstance,
    SelectionFilterElement,
    Solid,
    SolidUtils,
    Transaction,
    TransactionStatus,
    TemporaryViewMode,
    ViewDetailLevel,
    ViewPlan,
    ViewType,
    Wall,
    XYZ,
)
from Autodesk.Revit.DB.Events import DocumentClosingEventArgs       # noqa: E402
from Autodesk.Revit.UI import (                                     # noqa: E402
    ExternalEvent,
    ExternalEventRequest,
    IExternalEventHandler,
)

# pyRevit -------------------------------------------------------------------
from pyrevit import forms, script                                   # noqa: E402

# ---------------------------------------------------------------------------
# SECTION 2 - GLOBALS, LOGGING AND CONFIGURATION
# ---------------------------------------------------------------------------
logger = script.get_logger()          # writes to the pyRevit output window
output = script.get_output()          # pyRevit output window (used for errors)

doc = __revit__.ActiveUIDocument.Document          # noqa: F821 (pyRevit global)
uidoc = __revit__.ActiveUIDocument                 # noqa: F821
app = doc.Application

# Revit 2024 switched ElementId to 64 bit (ElementId.Value / ElementId(Int64)).
# Revit 2026 REMOVED ElementId.IntegerValue and the ElementId(Int32) ctor.
REVIT_YEAR = int(app.VersionNumber)
USE_INT64_IDS = REVIT_YEAR >= 2024

# Directory of this script - used to locate ui.xaml
SCRIPT_DIR = os.path.dirname(__file__)
XAML_FILE = os.path.join(SCRIPT_DIR, "ui.xaml")
SCOPE_XAML_FILE = os.path.join(SCRIPT_DIR, "scope.xaml")


class Config(object):
    """Central tuning parameters. Edit here, not inside the algorithms."""

    # Minimum intersection volume (cubic feet) required to report a clash.
    # 0.0001 ft3 == ~2.8 cm3. Raise it to filter out 'touching' geometry,
    # lower it for very small services.
    MIN_CLASH_VOLUME = 0.0001

    # Bounding boxes are grown by this amount (feet) before the overlap test.
    # A small negative value ignores surface-to-surface contact.
    BBOX_TOLERANCE = 0.0

    # Size (feet) of one cell of the uniform spatial hash used in the broad
    # phase. ~10 ft is a good compromise for building services.
    GRID_CELL_SIZE = 10.0

    # Geometry detail level. Fine returns true fitting geometry but is slower.
    DETAIL_LEVEL = ViewDetailLevel.Fine

    # Skip clashes where both elements report the same MEP system name.
    IGNORE_SAME_SYSTEM = True

    # Report clashes between two fittings as well (not required by spec).
    INCLUDE_FITTING_TO_FITTING = False

    # Warn the user before processing more than this many elements.
    LARGE_MODEL_WARNING = 6000

    # Graphic override transparency (0 = opaque, 100 = invisible).
    OVERRIDE_TRANSPARENCY = 0

    # Projection line weight applied to clashing elements (1-16).
    OVERRIDE_LINE_WEIGHT = 6

    # --- Clash sets -----------------------------------------------------
    # Clash points closer than this (3D, millimetres) end up in the same set.
    # Grouping is single-linkage: A-B 800 mm and B-C 800 mm puts A, B and C
    # in one set even if A-C is 1600 mm - a congested zone stays one set.
    # 0 disables grouping (every clash is its own set). Editable in the UI.
    SET_RADIUS_MM = 1000.0

    # Prefix of the Revit Selection Filters written by "Save Selection Sets".
    # Existing filters with this prefix + view name are updated / replaced.
    SELECTION_SET_PREFIX = "Clash Set"

    # Extra margin (feet) around the clash points when zooming.
    ZOOM_PADDING_FT = 3.0

    # --- Linked models --------------------------------------------------
    # Linked elements are collected inside the view crop box (XY) and view
    # range (Z: view depth -> top clip), grown by this margin (feet) so that
    # slabs / beams sitting exactly on a boundary are not missed.
    LINK_REGION_MARGIN_FT = 1.0


# --- Category configuration -------------------------------------------------
# Every supported category, its short label and its highlight colour.
BIC_CABLE_TRAY = BuiltInCategory.OST_CableTray
BIC_CABLE_TRAY_FITTING = BuiltInCategory.OST_CableTrayFitting
BIC_DUCT = BuiltInCategory.OST_DuctCurves
BIC_DUCT_FITTING = BuiltInCategory.OST_DuctFitting
BIC_PIPE = BuiltInCategory.OST_PipeCurves
BIC_PIPE_FITTING = BuiltInCategory.OST_PipeFitting

def _bic_int(bic):
    """BuiltInCategory -> int on every engine.

    '.value__' only exists on IronPython; pythonnet exposes enums as ints
    (2.x) or as objects convertible with int() (3.x).
    """
    try:
        return int(bic)
    except Exception:
        return int(bic.value__)


# Ints are used as dictionary keys because enum members are not reliably
# hashable across engines.
CT = _bic_int(BIC_CABLE_TRAY)
CTF = _bic_int(BIC_CABLE_TRAY_FITTING)
DT = _bic_int(BIC_DUCT)
DTF = _bic_int(BIC_DUCT_FITTING)
PP = _bic_int(BIC_PIPE)
PPF = _bic_int(BIC_PIPE_FITTING)

TARGET_BICS = [BIC_CABLE_TRAY, BIC_CABLE_TRAY_FITTING,
               BIC_DUCT, BIC_DUCT_FITTING,
               BIC_PIPE, BIC_PIPE_FITTING]

CATEGORY_LABELS = {
    CT: "Cable Tray",
    CTF: "Cable Tray Fitting",
    DT: "Duct",
    DTF: "Duct Fitting",
    PP: "Pipe",
    PPF: "Pipe Fitting",
}

FITTING_CATS = (CTF, DTF, PPF)
MEP_CATS = set(CATEGORY_LABELS.keys())
MEP_BICS = list(TARGET_BICS)


class BuildingCategory(object):
    """An Architecture / Structure category the user can tick.

    'structural' splits Walls / Floors on their Structural checkbox:
    None = any, True = only structural, False = only non-structural.
    """

    def __init__(self, key, label, discipline, bic, structural=None,
                 default=True):
        self.key = key
        self.label = label
        self.discipline = discipline          # "ARC" or "STR"
        self.bic = bic
        self.cat_int = _bic_int(bic)
        self.structural = structural
        self.default = default


_B = BuiltInCategory
BUILDING_CATEGORIES = [
    BuildingCategory("ARC_WALL", "Wall", "ARC", _B.OST_Walls, False),
    BuildingCategory("ARC_FLOOR", "Floor", "ARC", _B.OST_Floors, False),
    BuildingCategory("ARC_CEILING", "Ceiling", "ARC", _B.OST_Ceilings),
    BuildingCategory("ARC_ROOF", "Roof", "ARC", _B.OST_Roofs),
    BuildingCategory("ARC_COLUMN", "Column (Arch)", "ARC", _B.OST_Columns),
    BuildingCategory("ARC_STAIR", "Stair", "ARC", _B.OST_Stairs),
    BuildingCategory("ARC_DOOR", "Door", "ARC", _B.OST_Doors, default=False),
    BuildingCategory("ARC_WINDOW", "Window", "ARC", _B.OST_Windows, default=False),
    BuildingCategory("ARC_RAILING", "Railing", "ARC", _B.OST_StairsRailing,
                     default=False),
    BuildingCategory("ARC_RAMP", "Ramp", "ARC", _B.OST_Ramps, default=False),
    BuildingCategory("ARC_CW_PANEL", "Curtain Panel", "ARC",
                     _B.OST_CurtainWallPanels, default=False),
    BuildingCategory("ARC_CW_MULLION", "Curtain Mullion", "ARC",
                     _B.OST_CurtainWallMullions, default=False),
    BuildingCategory("ARC_GENERIC", "Generic Model", "ARC", _B.OST_GenericModel,
                     default=False),
    BuildingCategory("STR_COLUMN", "Structural Column", "STR",
                     _B.OST_StructuralColumns),
    BuildingCategory("STR_FRAMING", "Structural Framing (Beam)", "STR",
                     _B.OST_StructuralFraming),
    BuildingCategory("STR_FOUNDATION", "Structural Foundation", "STR",
                     _B.OST_StructuralFoundation),
    BuildingCategory("STR_WALL", "Structural Wall", "STR", _B.OST_Walls, True),
    BuildingCategory("STR_FLOOR", "Structural Floor / Slab", "STR", _B.OST_Floors,
                     True),
]
BUILDING_BY_KEY = dict((d.key, d) for d in BUILDING_CATEGORIES)
BUILDING_BY_CAT = {}
for _def in BUILDING_CATEGORIES:
    BUILDING_BY_CAT.setdefault(_def.cat_int, []).append(_def)
WALL_CAT = _bic_int(_B.OST_Walls)

# ViewPlan also covers ceiling, area and structural plans. The spec is
# "active FLOOR PLAN"; add ViewType.CeilingPlan here to allow RCPs too.
ALLOWED_VIEW_TYPES = (ViewType.FloorPlan,)

# Colours (requirement 3)
COLOR_RED = Color(255, 0, 0)        # Cable Tray clashes
COLOR_BLUE = Color(0, 70, 255)      # Duct clashes
COLOR_GREEN = Color(0, 170, 60)     # Pipe clashes
COLOR_MAGENTA = Color(255, 0, 255)  # Fitting clashes
COLOR_ORANGE = Color(255, 140, 0)   # Host Arch / Struct elements

# Colour applied to each element depending on its own category. A fitting is
# always magenta, which means a "Duct Fitting vs Pipe" clash shows one magenta
# element and one green element - immediately readable on screen.
ELEMENT_COLORS = {
    CT: COLOR_RED,
    DT: COLOR_BLUE,
    PP: COLOR_GREEN,
    CTF: COLOR_MAGENTA,
    DTF: COLOR_MAGENTA,
    PPF: COLOR_MAGENTA,
}


def element_colour(cat_int, discipline):
    """Override colour of a HOST element (linked elements cannot be overridden)."""
    if discipline != "MEP":
        return COLOR_ORANGE
    return ELEMENT_COLORS.get(cat_int, COLOR_MAGENTA)

# Colour label used for the clash row itself (fitting wins, then tray > duct > pipe)
def clash_colour_label(cat_a, cat_b):
    """Return the legend label of a clash pair."""
    if cat_a in FITTING_CATS or cat_b in FITTING_CATS:
        return "Magenta - Fitting clash"
    if CT in (cat_a, cat_b):
        return "Red - Cable Tray clash"
    if DT in (cat_a, cat_b):
        return "Blue - Duct clash"
    return "Green - Pipe clash"


def _pair_key(a, b):
    """Order independent key for a category pair."""
    return (a, b) if a <= b else (b, a)


# Requirement 2 - the exact list of category combinations to test.
ALLOWED_PAIRS = set([
    _pair_key(CT, DT),      # Cable Tray  <-> Duct
    _pair_key(CT, PP),      # Cable Tray  <-> Pipe
    _pair_key(DT, PP),      # Duct        <-> Pipe
    _pair_key(CTF, DT),     # Cable Tray Fitting <-> Duct
    _pair_key(CTF, PP),     # Cable Tray Fitting <-> Pipe
    _pair_key(DTF, PP),     # Duct Fitting       <-> Pipe
    _pair_key(DTF, CT),     # Duct Fitting       <-> Cable Tray
    _pair_key(PPF, CT),     # Pipe Fitting       <-> Cable Tray
    _pair_key(PPF, DT),     # Pipe Fitting       <-> Duct
])

if Config.INCLUDE_FITTING_TO_FITTING:
    ALLOWED_PAIRS |= set([
        _pair_key(CTF, DTF), _pair_key(CTF, PPF), _pair_key(DTF, PPF),
    ])


def pair_allowed(info_a, info_b, mep_vs_mep):
    """Clash rule between two collected elements.

    MEP vs MEP     : only the category matrix above, and only if enabled.
    MEP vs ARC/STR : always (the user already chose the ARC/STR categories).
    ARC/STR pairs  : never.
    """
    a_mep = info_a.discipline == "MEP"
    b_mep = info_b.discipline == "MEP"
    if a_mep and b_mep:
        return mep_vs_mep and _pair_key(info_a.cat_int, info_b.cat_int) in ALLOWED_PAIRS
    return a_mep or b_mep


def clash_class(info_a, info_b):
    other = info_b if info_a.discipline == "MEP" else info_a
    return "MEP vs {0}".format(other.discipline)


# ---------------------------------------------------------------------------
# SECTION 3 - SMALL REVIT HELPERS
# ---------------------------------------------------------------------------
def safe_name(element):
    """Element.Name that never raises (some elements have no name)."""
    if element is None:
        return ""
    try:
        return element.Name
    except Exception:
        try:
            return Element.Name.__get__(element)
        except Exception:
            return ""


def get_param_string(element, bip):
    """Read a BuiltInParameter as string, returning '' when not available."""
    try:
        prm = element.get_Parameter(bip)
        if prm and prm.HasValue:
            val = prm.AsString()
            if val:
                return val
            val = prm.AsValueString()
            if val:
                return val
    except Exception:
        pass
    return ""


def get_family_and_type(element):
    """Return (family_name, type_name) for any element."""
    fam_name, type_name = "", ""
    try:
        # element.Document, not the host doc: linked elements live in the link
        etype = element.Document.GetElement(element.GetTypeId())
        if etype is not None:
            type_name = safe_name(etype)
            try:
                # ElementType.FamilyName works for both system and loadable families
                fam_name = etype.FamilyName
            except Exception:
                fam_name = ""
        # Loadable families (fittings) expose the real family through the symbol
        if not fam_name and hasattr(element, "Symbol") and element.Symbol:
            fam_name = safe_name(element.Symbol.Family)
    except Exception as err:
        logger.debug("get_family_and_type failed for %s: %s", element.Id, err)
    return fam_name, type_name


def get_level_name(element, active_view):
    """Best-effort level name of an element."""
    owner = element.Document          # host or link document
    # 1 - Element.LevelId (works for most MEP / ARC elements)
    try:
        lvl_id = element.LevelId
        if lvl_id and lvl_id != ElementId.InvalidElementId:
            lvl = owner.GetElement(lvl_id)
            if lvl is not None:
                return safe_name(lvl)
    except Exception:
        pass
    # 2 - Reference level parameters
    for bip in (BuiltInParameter.RBS_START_LEVEL_PARAM,
                BuiltInParameter.FAMILY_LEVEL_PARAM,
                BuiltInParameter.INSTANCE_REFERENCE_LEVEL_PARAM,   # beams
                BuiltInParameter.FAMILY_BASE_LEVEL_PARAM,          # columns
                BuiltInParameter.SCHEDULE_LEVEL_PARAM):
        try:
            prm = element.get_Parameter(bip)
            if prm and prm.HasValue:
                lvl = owner.GetElement(prm.AsElementId())
                if lvl is not None:
                    return safe_name(lvl)
        except Exception:
            continue
    # 3 - Fall back to the level generated by the plan view
    try:
        if active_view.GenLevel is not None:
            return safe_name(active_view.GenLevel)
    except Exception:
        pass
    return "<no level>"


def get_system_name(element):
    """Return the MEP system name of an element ('' when it has none).

    Cable trays have no MEP system in Revit, so they normally return ''.
    Only non-empty, identical names are treated as 'same system'.
    """
    # MEPCurve (duct / pipe) exposes MEPSystem directly
    try:
        if isinstance(element, MEPCurve):
            mep_sys = element.MEPSystem
            if mep_sys is not None:
                return safe_name(mep_sys)
    except Exception:
        pass
    # Fittings: read the system through the MEPModel connectors
    try:
        mep_model = getattr(element, "MEPModel", None)
        if mep_model is not None:
            cm = mep_model.ConnectorManager
            if cm is not None:
                for conn in cm.Connectors:
                    try:
                        if conn.MEPSystem is not None:
                            return safe_name(conn.MEPSystem)
                    except Exception:
                        continue
    except Exception:
        pass
    # Parameter fallback (System Name on fittings). System *classification*
    # ("Supply Air", "Domestic Cold Water") is deliberately NOT used: two
    # different systems share a classification, so treating it as a name
    # silently discarded real clashes under IGNORE_SAME_SYSTEM.
    return get_param_string(element, BuiltInParameter.RBS_SYSTEM_NAME_PARAM)


def element_id_value(eid):
    """ElementId -> int. Value on 2024+ (IntegerValue is gone in 2026)."""
    if USE_INT64_IDS:
        return int(eid.Value)
    return int(eid.IntegerValue)


def make_element_id(value):
    """int -> ElementId. The Int32 constructor was removed in Revit 2026."""
    if USE_INT64_IDS:
        return ElementId(Int64(value))
    return ElementId(int(value))


def get_category_int(element):
    """BuiltInCategory value of an element as int, or 0."""
    try:
        return element_id_value(element.Category.Id)
    except Exception:
        return 0


def to_id_list(int_ids):
    """Build a .NET List[ElementId] from a python iterable of int ids."""
    id_list = List[ElementId]()
    for i in int_ids:
        id_list.Add(make_element_id(i))
    return id_list


def run_in_transaction(document, name, action):
    """Run action() inside a transaction; return its result or None.

    The original code ignored Transaction.Commit()'s return status (Revit
    usually reports failure through the status, not an exception) and
    called RollBack() on transactions that had already ended, which raises
    InvalidOperationException and masked the real error.
    """
    trans = Transaction(document, name)
    try:
        if trans.Start() != TransactionStatus.Started:
            logger.error("Could not start transaction '%s'", name)
            return None
        result = action()
        status = trans.Commit()
        if status != TransactionStatus.Committed:
            logger.error("Transaction '%s' ended with status %s", name, status)
            return None
        return result
    except Exception as err:
        logger.error("Transaction '%s' failed: %s", name, err)
        return None
    finally:
        if trans.HasStarted() and not trans.HasEnded():
            trans.RollBack()
        trans.Dispose()


def get_solid_fill_pattern_id():
    """Return the ElementId of the '<Solid fill>' drafting pattern.

    Language independent: the fill patterns are searched by IsSolidFill
    instead of by name.
    """
    try:
        patterns = FilteredElementCollector(doc).OfClass(FillPatternElement)
        for pat_elem in patterns:
            pat = pat_elem.GetFillPattern()
            if pat.IsSolidFill and pat.Target == FillPatternTarget.Drafting:
                return pat_elem.Id
    except Exception as err:
        logger.debug("Solid fill pattern lookup failed: %s", err)
    return ElementId.InvalidElementId


# ---------------------------------------------------------------------------
# SECTION 4 - GEOMETRY EXTRACTION
# ---------------------------------------------------------------------------
def make_geometry_options():
    """Geometry options tuned for speed (no references, no invisible objects)."""
    opts = Options()
    opts.ComputeReferences = False
    opts.IncludeNonVisibleObjects = False
    opts.DetailLevel = Config.DETAIL_LEVEL
    return opts


def _harvest_solids(geo_element, solids):
    """Recursively collect valid solids from a GeometryElement."""
    if geo_element is None:
        return
    for geo_obj in geo_element:
        if isinstance(geo_obj, Solid):
            try:
                if geo_obj.Volume > 1e-9 and geo_obj.Faces.Size > 0:
                    solids.append(geo_obj)
            except Exception:
                continue
        elif isinstance(geo_obj, GeometryInstance):
            # GetInstanceGeometry() already returns model coordinates
            try:
                _harvest_solids(geo_obj.GetInstanceGeometry(), solids)
            except Exception:
                continue


def get_element_solids(element, opts):
    """Return the list of solids of an element in model coordinates."""
    solids = []
    try:
        geo = element.get_Geometry(opts)
        _harvest_solids(geo, solids)
    except Exception as err:
        logger.debug("Geometry extraction failed for %s: %s", element.Id, err)
    return solids


# ---------------------------------------------------------------------------
# SECTION 5 - DATA OBJECTS
# ---------------------------------------------------------------------------
class Source(object):
    """One model the elements are collected from: the host or a link instance."""

    def __init__(self, name, document, link_instance=None,
                 include_mep=True, include_building=False):
        self.name = name
        self.doc = document
        self.link_instance = link_instance
        self.include_mep = include_mep
        self.include_building = include_building
        if link_instance is None:
            self.key = "host"
            self.link_int = 0
            self.transform = None
        else:
            self.key = link_instance.UniqueId
            self.link_int = element_id_value(link_instance.Id)
            total = link_instance.GetTotalTransform()
            self.transform = None if total.IsIdentity else total
        self.count = 0

    @property
    def is_link(self):
        return self.link_instance is not None


class _Box(object):
    """Minimal BoundingBoxXYZ stand-in (Min / Max XYZ) in host coordinates."""

    def __init__(self, minimum, maximum):
        self.Min = minimum
        self.Max = maximum


def transform_box(bbox, transform):
    """Axis aligned box of a (link-space) bounding box in host coordinates."""
    if transform is None:
        return bbox
    corners = [transform.OfPoint(XYZ(x, y, z))
               for x in (bbox.Min.X, bbox.Max.X)
               for y in (bbox.Min.Y, bbox.Max.Y)
               for z in (bbox.Min.Z, bbox.Max.Z)]
    return _Box(XYZ(min(p.X for p in corners), min(p.Y for p in corners),
                    min(p.Z for p in corners)),
                XYZ(max(p.X for p in corners), max(p.Y for p in corners),
                    max(p.Z for p in corners)))


def is_structural(element, cat_int):
    """Structural checkbox of a wall / floor."""
    bip = (BuiltInParameter.WALL_STRUCTURAL_SIGNIFICANT if cat_int == WALL_CAT
           else BuiltInParameter.FLOOR_PARAM_IS_STRUCTURAL)
    try:
        prm = element.get_Parameter(bip)
        return bool(prm is not None and prm.AsInteger() == 1)
    except Exception:
        return False


def classify(element, cat_int, source, building_keys):
    """-> (discipline, label, building_key) or None when not wanted."""
    if cat_int in MEP_CATS:
        if source.include_mep:
            return "MEP", CATEGORY_LABELS[cat_int], None
        return None
    if not source.include_building:
        return None
    for definition in BUILDING_BY_CAT.get(cat_int, ()):
        if (definition.structural is None
                or definition.structural == is_structural(element, cat_int)):
            if definition.key in building_keys:
                return definition.discipline, definition.label, definition.key
            return None
    return None


class ElementInfo(object):
    """Lightweight cache around a host or linked element.

    Bounding box and solids are expressed in HOST coordinates. Geometry is
    extracted lazily (only when the broad phase says the element might
    clash) - the single biggest performance win on large models.
    """

    def __init__(self, element, source, active_view, classification, bbox):
        self.element = element
        self.source = source
        self.id_int = element_id_value(element.Id)
        self.link_int = source.link_int
        self.cat_int = get_category_int(element)
        self.discipline, self.cat_label, self.building_key = classification
        self.family, self.type_name = get_family_and_type(element)
        self.level = get_level_name(element, active_view)
        self.system = get_system_name(element) if self.discipline == "MEP" else ""
        self.bbox = transform_box(bbox, source.transform)
        self._solids = None          # lazy
        self._geom_failed = False

    @property
    def ref(self):
        """(link instance id or 0, element id) - unique across models."""
        return (self.link_int, self.id_int)

    def solids(self, opts):
        """Lazy solid accessor with caching (host coordinates)."""
        if self._solids is None and not self._geom_failed:
            solids = get_element_solids(self.element, opts)
            transform = self.source.transform
            if transform is not None:
                moved = []
                for solid in solids:
                    try:
                        moved.append(SolidUtils.CreateTransformed(solid, transform))
                    except Exception as err:
                        logger.debug("Transform failed for %s: %s", self.id_int, err)
                solids = moved
            self._solids = solids
            if not self._solids:
                self._geom_failed = True
        return self._solids or []

    def release_geometry(self):
        """Free cached geometry (large models can hold a lot of memory)."""
        self._solids = None


class ClashResult(object):
    """One clash row.

    Attribute names are PascalCase because they are bound directly by the
    WPF DataGrid columns in ui.xaml. Side A is always the MEP element.
    """

    def __init__(self, number, info_a, info_b, point, volume, view_name):
        self.ClashId = number
        self.ClashName = "Clash-{0:04d}".format(number)

        self.ElementAId = info_a.id_int
        self.ElementBId = info_b.id_int
        self.LinkAId = info_a.link_int
        self.LinkBId = info_b.link_int
        self.SourceA = info_a.source.name
        self.SourceB = info_b.source.name
        self.DisciplineA = info_a.discipline
        self.DisciplineB = info_b.discipline
        self.CatAInt = info_a.cat_int
        self.CatBInt = info_b.cat_int
        self.CategoryA = info_a.cat_label
        self.CategoryB = info_b.cat_label
        self.FamilyA = info_a.family
        self.TypeA = info_a.type_name
        self.FamilyB = info_b.family
        self.TypeB = info_b.type_name
        self.SystemA = info_a.system or "-"
        self.SystemB = info_b.system or "-"
        self.Level = info_a.level or info_b.level
        self.ViewName = view_name
        self.ClashClass = clash_class(info_a, info_b)
        self.ClashType = "{0} vs {1}".format(info_a.cat_label, info_b.cat_label)
        self.Colour = clash_colour_label(info_a.cat_int, info_b.cat_int)
        self.IsLinked = "Yes" if (self.LinkAId or self.LinkBId) else "No"

        # Colour for every HOST side (linked elements cannot be overridden)
        self.host_colours = []
        for info in (info_a, info_b):
            if info.link_int == 0:
                self.host_colours.append(
                    (info.id_int, element_colour(info.cat_int, info.discipline)))

        # Raw values (internal feet) kept for export / zoom, strings for the UI
        self.PointX = point.X
        self.PointY = point.Y
        self.PointZ = point.Z
        self.X = "{0:.3f}".format(point.X)
        self.Y = "{0:.3f}".format(point.Y)
        self.Z = "{0:.3f}".format(point.Z)
        self.Volume = "{0:.5f}".format(volume)
        self.VolumeRaw = volume

        # Filled in by group_clashes()
        self.SetId = 0
        self.SetName = ""
        self.SetSize = 1

        # Pre-computed lowercase blob makes the search box instant
        self.SearchBlob = u" ".join([
            str(self.ClashId), self.ClashName, str(self.ElementAId),
            str(self.ElementBId), self.CategoryA, self.CategoryB,
            self.FamilyA, self.TypeA, self.FamilyB, self.TypeB,
            self.SystemA, self.SystemB, self.Level, self.ClashType,
            self.ClashClass, self.SourceA, self.SourceB,
        ]).lower()

    @property
    def refs(self):
        return [(self.LinkAId, self.ElementAId), (self.LinkBId, self.ElementBId)]

    @property
    def ref_names(self):
        return {(self.LinkAId, self.ElementAId): self.SourceA,
                (self.LinkBId, self.ElementBId): self.SourceB}

    def matches(self, needle):
        return (not needle or needle in self.SearchBlob
                or needle in self.SetName.lower())

    def as_row(self):
        """Ordered values for the Excel export (must match EXPORT_HEADERS)."""
        return [
            self.ClashId, self.ClashName, self.SetName, self.SetSize,
            self.ClashClass, self.ClashType,
            self.SourceA, self.DisciplineA, self.ElementAId, self.CategoryA,
            self.FamilyA, self.TypeA, self.SystemA,
            self.SourceB, self.DisciplineB, self.ElementBId, self.CategoryB,
            self.FamilyB, self.TypeB, self.SystemB,
            self.Level, self.PointX, self.PointY, self.PointZ,
            self.VolumeRaw, self.ViewName,
        ]


EXPORT_HEADERS = [
    "Clash #", "Clash Name", "Set", "Clashes in Set", "Clash Class", "Clash Type",
    "Source A", "Discipline A", "Element A ID", "Category A", "Family A",
    "Type A", "System A",
    "Source B", "Discipline B", "Element B ID", "Category B", "Family B",
    "Type B", "System B",
    "Level", "X (ft)", "Y (ft)", "Z (ft)", "Intersection Volume (ft3)", "View",
]


# ---------------------------------------------------------------------------
# SECTION 6 - COLLECTION (requirement 1)
# ---------------------------------------------------------------------------
def validate_active_view():
    """Ensure the active view is a plan view that can host overrides."""
    active_view = doc.ActiveView
    if active_view is None:
        forms.alert("No active view.", title="MEP Clash Detector", exitscript=True)
    if (not isinstance(active_view, ViewPlan)
            or active_view.ViewType not in ALLOWED_VIEW_TYPES):
        forms.alert(
            "The active view is a {0}.\n\n"
            "This tool must be run from a FLOOR PLAN view."
            .format(active_view.ViewType),
            title="MEP Clash Detector", exitscript=True)
    if active_view.IsTemplate:
        forms.alert("View templates cannot be processed.",
                    title="MEP Clash Detector", exitscript=True)
    return active_view


# --- Scope (which models / categories) --------------------------------------
_MEP_TOKENS = set(["MEP", "MEC", "MECH", "ME", "ELE", "ELEC", "EL", "PLB", "PLU",
                   "PLUMB", "HVAC", "FP", "FF", "FA", "FIRE", "DRN", "ELV", "LV",
                   "SERVICES"])
_BLD_TOKENS = set(["ARC", "ARCH", "AR", "A", "STR", "ST", "S", "STRUC", "STRUCT",
                   "CIV", "CIVIL", "INT", "FACADE"])
_MEP_PREFIXES = ("MECH", "ELEC", "PLUMB", "HVAC")
_BLD_PREFIXES = ("ARCH", "STRUC", "FACAD", "INTERIOR")


def guess_link_roles(name):
    """Pre-tick MEP / Arch-Struct from the link file name (TWR-ARC-L01 ...)."""
    tokens = [t for t in re.split(r"[^A-Z0-9]+", u"{0}".format(name).upper()) if t]
    mep = any(t in _MEP_TOKENS or t.startswith(_MEP_PREFIXES) for t in tokens)
    bld = any(t in _BLD_TOKENS or t.startswith(_BLD_PREFIXES) for t in tokens)
    if mep and not bld:
        return True, False
    if bld and not mep:
        return False, True
    return True, True


def default_scope():
    return {
        "host_mep": True,
        "host_building": False,
        "links": {},            # link instance UniqueId -> {"mep", "building"}
        "building_keys": [d.key for d in BUILDING_CATEGORIES if d.default],
        "mep_vs_mep": True,
    }


def _json_path(file_id, fallback_name):
    try:
        return script.get_document_data_file(file_id, "json", add_cmd_name=False)
    except Exception:
        return os.path.join(os.environ.get("TEMP", SCRIPT_DIR), fallback_name)


def _load_json(path):
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_json(path, data):
    try:
        with open(path, "w") as handle:
            json.dump(data, handle)
    except Exception as err:
        logger.debug("Could not write %s: %s", path, err)


def load_scope():
    scope = default_scope()
    stored = _load_json(_json_path("MEPClashScope", "MEPClashDetector_scope.json"))
    for key in scope:
        if key in stored:
            scope[key] = stored[key]
    scope["building_keys"] = [k for k in scope["building_keys"]
                              if k in BUILDING_BY_KEY]
    return scope


def save_scope(scope):
    _save_json(_json_path("MEPClashScope", "MEPClashDetector_scope.json"), scope)


def list_links(document):
    """Every RevitLinkInstance of the host: dicts for the scope dialog."""
    rows = []
    for inst in FilteredElementCollector(document).OfClass(RevitLinkInstance):
        link_doc = None
        try:
            link_doc = inst.GetLinkDocument()
        except Exception:
            pass
        title = (link_doc.Title if link_doc is not None
                 else safe_name(inst).split(" : ")[0])
        if title.lower().endswith(".rvt"):
            title = title[:-4]
        rows.append({"instance": inst, "doc": link_doc, "title": title,
                     "key": inst.UniqueId, "loaded": link_doc is not None,
                     "id": element_id_value(inst.Id)})
    # Same file linked twice -> append the instance id to tell them apart
    titles = [r["title"] for r in rows]
    for row in rows:
        row["name"] = (row["title"] if titles.count(row["title"]) == 1
                       else u"{0} [{1}]".format(row["title"], row["id"]))
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def resolve_sources(document, scope):
    """Turn the stored scope into Source objects for the current session."""
    sources = []
    if scope.get("host_mep") or scope.get("host_building"):
        sources.append(Source("Host", document, None,
                              bool(scope.get("host_mep")),
                              bool(scope.get("host_building"))))
    links_cfg = scope.get("links", {})
    for row in list_links(document):
        cfg = links_cfg.get(row["key"])
        if not cfg or not row["loaded"]:
            continue
        if cfg.get("mep") or cfg.get("building"):
            sources.append(Source(row["name"], row["doc"], row["instance"],
                                  bool(cfg.get("mep")), bool(cfg.get("building"))))
    return sources


# --- Region used for linked models -------------------------------------------
_BIG = 1.0e5        # feet - "unbounded"


def _plane_z(view, view_range, plane):
    """Elevation of a view range plane, or None when unlimited / unknown."""
    try:
        level = view.Document.GetElement(view_range.GetLevelId(plane))
        if isinstance(level, Level):
            return level.ProjectElevation + view_range.GetOffset(plane)
    except Exception:
        pass
    return None


def view_region(view):
    """(min XYZ, max XYZ) of the view in host coordinates: crop box XY (if
    active) and view range Z (view depth -> top clip)."""
    zmin, zmax = -_BIG, _BIG
    try:
        view_range = view.GetViewRange()
        top = _plane_z(view, view_range, PlanViewPlane.TopClipPlane)
        bottom = _plane_z(view, view_range, PlanViewPlane.ViewDepthPlane)
        if bottom is None:
            bottom = _plane_z(view, view_range, PlanViewPlane.BottomClipPlane)
        if top is not None:
            zmax = top
        if bottom is not None:
            zmin = bottom
    except Exception as err:
        logger.debug("View range unavailable: %s", err)
    xmin, ymin, xmax, ymax = -_BIG, -_BIG, _BIG, _BIG
    try:
        if view.CropBoxActive:
            box = view.CropBox
            pts = [box.Transform.OfPoint(XYZ(x, y, box.Min.Z))
                   for x in (box.Min.X, box.Max.X) for y in (box.Min.Y, box.Max.Y)]
            xmin, xmax = min(p.X for p in pts), max(p.X for p in pts)
            ymin, ymax = min(p.Y for p in pts), max(p.Y for p in pts)
    except Exception as err:
        logger.debug("Crop box unavailable: %s", err)
    if zmin > zmax:
        zmin, zmax = zmax, zmin
    pad = Config.LINK_REGION_MARGIN_FT
    return (XYZ(xmin - pad, ymin - pad, zmin - pad),
            XYZ(xmax + pad, ymax + pad, zmax + pad))


def region_outline_in_link(region, transform):
    """Host-space region -> Outline in link coordinates."""
    minimum, maximum = region
    if transform is None:
        return Outline(minimum, maximum)
    inverse = transform.Inverse
    box = transform_box(_Box(minimum, maximum), inverse)
    return Outline(box.Min, box.Max)


def collect_elements(active_view, sources, scope):
    """Collect MEP / ARC / STR elements from every source.

    Host  : FilteredElementCollector(doc, view.Id) returns only what is
            visible in the view (hidden, filtered, cropped elements excluded).
    Links : the user explicitly ticked the link in the scope dialog, so its
            elements are taken inside the view region (crop + view range),
            independent of the link's visibility settings in the view.
    """
    building_keys = set(scope.get("building_keys", []))
    building_bics = []
    for definition in BUILDING_CATEGORIES:
        if definition.key in building_keys and definition.bic not in building_bics:
            building_bics.append(definition.bic)
    region = view_region(active_view)

    infos = []
    for source in sources:
        bics = []
        if source.include_mep:
            bics.extend(MEP_BICS)
        if source.include_building:
            bics.extend(building_bics)
        if not bics:
            continue
        cat_filter = ElementMulticategoryFilter(List[BuiltInCategory](bics))
        if source.is_link:
            collector = (FilteredElementCollector(source.doc)
                         .WherePasses(cat_filter)
                         .WherePasses(BoundingBoxIntersectsFilter(
                             region_outline_in_link(region, source.transform)))
                         .WhereElementIsNotElementType())
        else:
            collector = (FilteredElementCollector(source.doc, active_view.Id)
                         .WherePasses(cat_filter)
                         .WhereElementIsNotElementType())

        for element in collector:
            try:
                cat_int = get_category_int(element)
                classification = classify(element, cat_int, source, building_keys)
                if classification is None:
                    continue
                # Stacked wall parents repeat their sub-walls' geometry
                if isinstance(element, Wall) and element.IsStackedWall:
                    continue
                bbox = element.get_BoundingBox(None)
                if bbox is None:
                    continue
                infos.append(ElementInfo(element, source, active_view,
                                         classification, bbox))
                source.count += 1
            except Exception as err:
                logger.debug("Skipped element %s in %s: %s",
                             element.Id, source.name, err)
    return infos


# ---------------------------------------------------------------------------
# SECTION 7 - BROAD PHASE (spatial hash + AABB)
# ---------------------------------------------------------------------------
def _cell_range(minimum, maximum, size):
    """Inclusive cell index range covered by an interval."""
    return range(int(math.floor(minimum / size)),
                 int(math.floor(maximum / size)) + 1)


def build_spatial_index(infos):
    """Uniform 3D grid: {(ix, iy, iz): [index, index, ...]}."""
    size = Config.GRID_CELL_SIZE
    grid = {}
    for idx, info in enumerate(infos):
        bb = info.bbox
        for ix in _cell_range(bb.Min.X, bb.Max.X, size):
            for iy in _cell_range(bb.Min.Y, bb.Max.Y, size):
                for iz in _cell_range(bb.Min.Z, bb.Max.Z, size):
                    grid.setdefault((ix, iy, iz), []).append(idx)
    return grid


def bboxes_overlap(bb1, bb2, tol=0.0):
    """Fast axis aligned bounding box overlap test."""
    return not (bb1.Min.X - tol > bb2.Max.X or bb2.Min.X - tol > bb1.Max.X or
                bb1.Min.Y - tol > bb2.Max.Y or bb2.Min.Y - tol > bb1.Max.Y or
                bb1.Min.Z - tol > bb2.Max.Z or bb2.Min.Z - tol > bb1.Max.Z)


def candidate_pairs(infos, mep_vs_mep=True):
    """Yield unique index pairs that survive the broad phase filters.

    Filters applied here (cheap first, expensive last):
        1. same grid cell
        2. allowed category combination
        3. not the same element / not already tested
        4. bounding boxes overlap
        5. not the same MEP system
    """
    grid = build_spatial_index(infos)
    tested = set()
    tol = Config.BBOX_TOLERANCE

    for _cell, bucket in grid.items():
        count = len(bucket)
        if count < 2:
            continue
        for i in range(count - 1):
            idx_a = bucket[i]
            info_a = infos[idx_a]
            for j in range(i + 1, count):
                idx_b = bucket[j]
                if idx_a == idx_b:
                    continue
                key = (idx_a, idx_b) if idx_a < idx_b else (idx_b, idx_a)
                if key in tested:
                    continue
                tested.add(key)

                info_b = infos[idx_b]

                # 2 - discipline / category rule
                if not pair_allowed(info_a, info_b, mep_vs_mep):
                    continue
                # 4 - bounding boxes
                if not bboxes_overlap(info_a.bbox, info_b.bbox, tol):
                    continue
                # 5 - same system (only meaningful inside one model)
                if (Config.IGNORE_SAME_SYSTEM
                        and info_a.link_int == info_b.link_int):
                    sys_a, sys_b = info_a.system, info_b.system
                    if sys_a and sys_b and sys_a.lower() == sys_b.lower():
                        continue
                yield key


# ---------------------------------------------------------------------------
# SECTION 8 - NARROW PHASE (Boolean intersection)
# ---------------------------------------------------------------------------
def solids_intersection(info_a, info_b, opts):
    """Return (clash_point, volume) or (None, 0.0).

    Uses BooleanOperationsUtils.ExecuteBooleanOperation with
    BooleanOperationsType.Intersect. The clash point is the centroid of the
    resulting intersection solid, which is the most meaningful single point
    for navigation and coordination reports.
    """
    solids_a = info_a.solids(opts)
    if not solids_a:
        return None, 0.0
    solids_b = info_b.solids(opts)
    if not solids_b:
        return None, 0.0

    best_volume = 0.0
    best_point = None

    for sol_a in solids_a:
        for sol_b in solids_b:
            try:
                result = BooleanOperationsUtils.ExecuteBooleanOperation(
                    sol_a, sol_b, BooleanOperationsType.Intersect)
            except Exception as err:
                # Boolean operations legitimately fail on some tessellated or
                # self-intersecting geometry: log and keep going.
                logger.debug("Boolean failed (%s vs %s): %s",
                             info_a.id_int, info_b.id_int, err)
                continue

            if result is None:
                continue
            try:
                volume = result.Volume
            except Exception:
                continue

            if volume > best_volume:
                best_volume = volume
                try:
                    best_point = result.ComputeCentroid()
                except Exception:
                    best_point = None

    if best_volume > Config.MIN_CLASH_VOLUME:
        if best_point is None:
            # Fallback: midpoint of the two bounding box centres
            ca = info_a.bbox.Min.Add(info_a.bbox.Max).Multiply(0.5)
            cb = info_b.bbox.Min.Add(info_b.bbox.Max).Multiply(0.5)
            best_point = ca.Add(cb).Multiply(0.5)
        return best_point, best_volume
    return None, 0.0


def scope_breakdown(infos, max_lines=14):
    """Readable 'what is being counted' text: per source, per category."""
    by_source, by_cat = {}, {}
    for info in infos:
        src = info.source.name
        by_source[src] = by_source.get(src, 0) + 1
        key = (info.discipline, info.cat_label)
        by_cat[key] = by_cat.get(key, 0) + 1
    lines = [u"By model:"]
    for name in sorted(by_source, key=lambda n: -by_source[n]):
        lines.append(u"   {0}: {1}".format(name, by_source[name]))
    lines.append(u"By category:")
    ordered = sorted(by_cat.items(), key=lambda kv: -kv[1])
    for (discipline, label), count in ordered[:max_lines]:
        lines.append(u"   [{0}] {1}: {2}".format(discipline, label, count))
    if len(ordered) > max_lines:
        lines.append(u"   ... {0} more categories".format(len(ordered) - max_lines))
    return u"\n".join(lines)


def _mep_first(info_a, info_b):
    """Report the MEP element as side A."""
    if info_a.discipline != "MEP" and info_b.discipline == "MEP":
        return info_b, info_a
    return info_a, info_b


class DetectionCancelled(Exception):
    """Raised when the user declines the large-view warning.

    Replaces script.exit(): SystemExit is not an Exception subclass, so when
    detection was re-run from the window's Refresh button it escaped every
    'except Exception' and reached the WPF dispatcher unhandled.
    """


def detect_clashes(active_view, scope, show_progress=True):
    """Full detection pipeline. Returns (clashes, stats_dict)."""
    started = datetime.now()
    opts = make_geometry_options()

    sources = resolve_sources(active_view.Document, scope)
    infos = collect_elements(active_view, sources, scope)
    element_count = len(infos)
    source_stats = [(s.name, s.include_mep, s.include_building, s.count)
                    for s in sources]
    logger.info("Collected %s elements in view '%s' from %s models",
                element_count, active_view.Name, len(sources))

    if element_count == 0:
        return [], {"elements": 0, "pairs": 0, "clashes": 0, "seconds": 0.0,
                    "cancelled": False, "sources": source_stats,
                    "linked": 0, "building": 0}

    if element_count > Config.LARGE_MODEL_WARNING:
        proceed = forms.alert(
            u"{0} elements are in scope for this view:\n\n{1}\n\n"
            u"Only the categories above are checked (no accessories, flex "
            u"duct/pipe, conduits, equipment, terminals...). Use the 'Scope "
            u"Check' button for a full breakdown of the view.\n\n"
            u"Clash detection may take several minutes.\n"
            u"Tip: crop the view or reduce the scope to speed it up.\n\n"
            u"Continue?".format(element_count, scope_breakdown(infos)),
            title="Large view", yes=True, no=True)
        if not proceed:
            raise DetectionCancelled()

    pairs = list(candidate_pairs(infos, bool(scope.get("mep_vs_mep", True))))
    pair_count = len(pairs)
    logger.info("Broad phase produced %s candidate pairs", pair_count)

    clashes = []
    view_name = active_view.Name
    counter = 0
    cancelled = False

    if show_progress and pair_count:
        with forms.ProgressBar(title="Checking clashes: {value} of {max_value}",
                               cancellable=True, step=25) as pbar:
            for i, (idx_a, idx_b) in enumerate(pairs):
                if pbar.cancelled:
                    logger.warning("Detection cancelled by user.")
                    cancelled = True
                    break
                pbar.update_progress(i + 1, pair_count)
                info_a, info_b = _mep_first(infos[idx_a], infos[idx_b])
                point, volume = solids_intersection(info_a, info_b, opts)
                if point is not None:
                    counter += 1
                    clashes.append(ClashResult(counter, info_a, info_b,
                                               point, volume, view_name))
    else:
        for idx_a, idx_b in pairs:
            info_a, info_b = _mep_first(infos[idx_a], infos[idx_b])
            point, volume = solids_intersection(info_a, info_b, opts)
            if point is not None:
                counter += 1
                clashes.append(ClashResult(counter, info_a, info_b,
                                           point, volume, view_name))

    # Release cached geometry as soon as the run is finished
    for info in infos:
        info.release_geometry()

    elapsed = (datetime.now() - started).total_seconds()
    stats = {"elements": element_count, "pairs": pair_count,
             "clashes": len(clashes), "seconds": elapsed,
             "cancelled": cancelled, "sources": source_stats,
             "linked": sum(1 for i in infos if i.link_int),
             "building": sum(1 for i in infos if i.discipline != "MEP")}
    logger.info("Found %s clashes in %.1f s", len(clashes), elapsed)
    return clashes, stats


# ---------------------------------------------------------------------------
# SECTION 9 - GRAPHIC OVERRIDES (requirement 3)
# ---------------------------------------------------------------------------
def _highlight_store_path():
    """Per-document JSON file remembering which HOST elements this tool
    coloured, so a later run / Clear can reset them."""
    return _json_path("MEPClashHighlights", "MEPClashDetector_highlights.json")


def _load_store():
    return _load_json(_highlight_store_path())


def _save_store(data):
    _save_json(_highlight_store_path(), data)


class HighlightManager(object):
    """Applies / clears OverrideGraphicSettings on clashing elements."""

    def __init__(self, document, active_view):
        self.doc = document
        self.view = active_view
        self.view_key = active_view.UniqueId
        self.solid_fill_id = get_solid_fill_pattern_id()
        # Start with whatever a previous run left in this view
        self.highlighted = set(int(i) for i in
                               _load_store().get(self.view_key, []))

    def _persist(self):
        data = _load_store()
        if self.highlighted:
            data[self.view_key] = sorted(self.highlighted)
        else:
            data.pop(self.view_key, None)
        _save_store(data)

    def _build_override(self, colour):
        """Create an OverrideGraphicSettings for one colour (API 2019+ names)."""
        ogs = OverrideGraphicSettings()
        ogs.SetProjectionLineColor(colour)
        ogs.SetProjectionLineWeight(Config.OVERRIDE_LINE_WEIGHT)
        ogs.SetCutLineColor(colour)
        ogs.SetCutLineWeight(Config.OVERRIDE_LINE_WEIGHT)
        if self.solid_fill_id != ElementId.InvalidElementId:
            try:
                ogs.SetSurfaceForegroundPatternId(self.solid_fill_id)
                ogs.SetSurfaceForegroundPatternColor(colour)
                ogs.SetSurfaceForegroundPatternVisible(True)
                ogs.SetCutForegroundPatternId(self.solid_fill_id)
                ogs.SetCutForegroundPatternColor(colour)
                ogs.SetCutForegroundPatternVisible(True)
            except Exception as err:
                logger.debug("Surface pattern override unavailable: %s", err)
        try:
            ogs.SetSurfaceTransparency(Config.OVERRIDE_TRANSPARENCY)
        except Exception:
            pass
        return ogs

    def apply(self, clashes):
        """Colour every clashing HOST element; reset stale colours.

        Revit cannot override individual elements of a linked model, so
        linked elements are not coloured (they are selected on click).
        """
        colour_map = {}
        for clash in clashes:
            for eid_int, colour in clash.host_colours:
                colour_map[eid_int] = colour

        stale = [i for i in self.highlighted if i not in colour_map]
        if not colour_map and not stale:
            return 0

        def _do():
            plain = OverrideGraphicSettings()
            for eid_int in stale:
                try:
                    self.view.SetElementOverrides(make_element_id(eid_int), plain)
                except Exception:
                    pass            # element deleted since the last run
            ogs_cache = {}
            done = set()
            for eid_int, colour in colour_map.items():
                key = (colour.Red, colour.Green, colour.Blue)
                if key not in ogs_cache:
                    ogs_cache[key] = self._build_override(colour)
                try:
                    self.view.SetElementOverrides(make_element_id(eid_int),
                                                  ogs_cache[key])
                    done.add(eid_int)
                except Exception as err:
                    logger.debug("Override failed for %s: %s", eid_int, err)
            return done

        done = run_in_transaction(self.doc,
                                  "MEP Clash Detector - Highlight clashes", _do)
        if done is None:
            return 0
        self.highlighted = done
        self._persist()
        return len(done)

    def clear(self):
        """Remove every override applied by this tool and stop isolation."""
        ids = list(self.highlighted)

        def _do():
            plain = OverrideGraphicSettings()
            cleared = 0
            for eid_int in ids:
                try:
                    self.view.SetElementOverrides(make_element_id(eid_int), plain)
                    cleared += 1
                except Exception:
                    continue
            try:
                self.view.DisableTemporaryViewMode(
                    TemporaryViewMode.TemporaryHideIsolate)
            except Exception:
                pass
            return cleared

        cleared = run_in_transaction(self.doc,
                                     "MEP Clash Detector - Clear highlights", _do)
        if cleared is None:
            return 0
        self.highlighted = set()
        self._persist()
        return cleared

    def isolate(self, int_ids):
        """Temporarily isolate the given elements in the active view."""
        if not int_ids:
            return False
        ids = to_id_list(int_ids)
        ok = run_in_transaction(
            self.doc, "MEP Clash Detector - Isolate clashes",
            lambda: self.view.IsolateElementsTemporary(ids) or True)
        return bool(ok)


# ---------------------------------------------------------------------------
# SECTION 10 - CLASH SETS (multiple clashes at the same location)
# ---------------------------------------------------------------------------
MM_PER_FOOT = 304.8


class ClashSet(object):
    """A group of clashes whose clash points lie close to each other.

    PascalCase attributes are bound by the sets DataGrid in ui.xaml.
    """

    def __init__(self, number, members):
        self.SetId = number
        self.SetName = "Set-{0:03d}".format(number)
        self.clashes = members
        self.ClashCount = len(members)
        self.Kind = "Multiple" if len(members) > 1 else "Single"

        names = {}
        for clash in members:
            names.update(clash.ref_names)
        self.refs = sorted(names.keys())            # (link id or 0, element id)
        self.ref_names = names
        self.host_ids = [r[1] for r in self.refs if r[0] == 0]
        self.ElementCount = len(self.refs)
        self.LinkedCount = len(self.refs) - len(self.host_ids)
        self.Sources = ", ".join(sorted(set(names.values())))
        self.ClashClasses = ", ".join(sorted(set(c.ClashClass for c in members)))

        self.ClashTypes = ", ".join(sorted(set(c.ClashType for c in members)))

        levels = {}
        for clash in members:
            levels[clash.Level] = levels.get(clash.Level, 0) + 1
        self.Level = sorted(levels.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

        xs = [c.PointX for c in members]
        ys = [c.PointY for c in members]
        zs = [c.PointZ for c in members]
        self.CenterX = sum(xs) / len(xs)
        self.CenterY = sum(ys) / len(ys)
        self.CenterZ = sum(zs) / len(zs)
        self.ExtentRaw = math.sqrt((max(xs) - min(xs)) ** 2 +
                                   (max(ys) - min(ys)) ** 2 +
                                   (max(zs) - min(zs)) ** 2)
        self.X = "{0:.3f}".format(self.CenterX)
        self.Y = "{0:.3f}".format(self.CenterY)
        self.Z = "{0:.3f}".format(self.CenterZ)
        self.Extent = "{0:.2f}".format(self.ExtentRaw)
        self.ClashIds = ", ".join(str(c.ClashId) for c in members)

    @property
    def points(self):
        return [(c.PointX, c.PointY, c.PointZ) for c in self.clashes]

    def as_row(self):
        """Ordered values for the export (must match SET_EXPORT_HEADERS)."""
        linked = [u"{0}:{1}".format(self.ref_names[r], r[1])
                  for r in self.refs if r[0] != 0]
        return [self.SetName, self.ClashCount, self.ElementCount,
                self.LinkedCount, self.Level, self.ClashClasses,
                self.ClashTypes, self.Sources, self.CenterX, self.CenterY,
                self.CenterZ, self.ExtentRaw, self.ClashIds,
                ", ".join(str(i) for i in self.host_ids), ", ".join(linked)]


SET_EXPORT_HEADERS = [
    "Set", "Clashes", "Elements", "Linked Elements", "Level", "Clash Classes",
    "Clash Types", "Sources", "Centre X (ft)", "Centre Y (ft)", "Centre Z (ft)",
    "Extent (ft)", "Clash #s", "Host Element IDs", "Linked Element IDs",
]


def group_clashes(clashes, radius_mm):
    """Cluster clashes into ClashSet objects.

    Single-linkage clustering in 3D: two clashes belong to the same set when
    their clash points are within radius_mm of each other, directly or through
    a chain of clashes. A uniform grid with cell size == radius keeps it close
    to O(n): only the 27 neighbouring cells are compared. Using Z means clashes
    on different levels never merge.

    Sets are numbered by size (largest hotspot = Set-001), then level, X, Y.
    Every clash gets SetId / SetName / SetSize written back.
    """
    count = len(clashes)
    parent = list(range(count))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        root_i, root_j = find(i), find(j)
        if root_i != root_j:
            parent[max(root_i, root_j)] = min(root_i, root_j)

    radius = float(radius_mm or 0.0) / MM_PER_FOOT
    if radius > 0 and count > 1:
        r2 = radius * radius
        grid, cells = {}, []
        for idx, clash in enumerate(clashes):
            key = (int(math.floor(clash.PointX / radius)),
                   int(math.floor(clash.PointY / radius)),
                   int(math.floor(clash.PointZ / radius)))
            cells.append(key)
            grid.setdefault(key, []).append(idx)
        for idx, clash in enumerate(clashes):
            cx, cy, cz = cells[idx]
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for other in grid.get((cx + dx, cy + dy, cz + dz), ()):
                            if other <= idx:
                                continue
                            oth = clashes[other]
                            dist2 = ((clash.PointX - oth.PointX) ** 2 +
                                     (clash.PointY - oth.PointY) ** 2 +
                                     (clash.PointZ - oth.PointZ) ** 2)
                            if dist2 <= r2:
                                union(idx, other)

    buckets = {}
    for idx in range(count):
        buckets.setdefault(find(idx), []).append(clashes[idx])

    def order(members):
        return (-len(members), members[0].Level,
                round(min(c.PointX for c in members), 3),
                round(min(c.PointY for c in members), 3))

    clash_sets = []
    for number, members in enumerate(sorted(buckets.values(), key=order), 1):
        members.sort(key=lambda c: c.ClashId)
        clash_set = ClashSet(number, members)
        for clash in members:
            clash.SetId = number
            clash.SetName = clash_set.SetName
            clash.SetSize = len(members)
        clash_sets.append(clash_set)
    return clash_sets


_ILLEGAL_NAME_CHARS = u'{}[]|;<>?`~\\:'


def _clean_name(text):
    return u"".join(c for c in u"{0}".format(text)
                    if c not in _ILLEGAL_NAME_CHARS).strip() or u"View"


def selection_set_prefix(view_name):
    return u"{0} - {1} - ".format(Config.SELECTION_SET_PREFIX,
                                  _clean_name(view_name))


def save_selection_sets(document, view, clash_sets):
    """Write one Revit Selection Filter (SelectionFilterElement) per set.

    Filters named '<prefix><view> - Set-###' from an earlier run are updated
    in place (so views that already use them keep working) and leftovers
    that no longer correspond to a set are deleted. A selection filter can
    only hold HOST elements, so linked elements are left out and sets made
    only of linked elements are skipped.
    Returns (written, removed, skipped) or None when the transaction failed.
    """
    prefix = selection_set_prefix(view.Name)
    existing = {}
    for sfe in FilteredElementCollector(document).OfClass(SelectionFilterElement):
        name = safe_name(sfe)
        if name.startswith(prefix):
            existing[name] = sfe

    wanted, skipped = [], 0
    for clash_set in clash_sets:
        valid = [i for i in clash_set.host_ids
                 if document.GetElement(make_element_id(i)) is not None]
        if valid:
            wanted.append((prefix + clash_set.SetName, valid))
        else:
            skipped += 1

    def _do():
        written, removed = 0, 0
        for name, ids in wanted:
            sfe = existing.pop(name, None)
            if sfe is None:
                sfe = SelectionFilterElement.Create(document, name)
            sfe.SetElementIds(to_id_list(ids))
            written += 1
        for stale in existing.values():
            try:
                document.Delete(stale.Id)
                removed += 1
            except Exception:
                pass
        return written, removed, skipped

    return run_in_transaction(document,
                              "MEP Clash Detector - Save selection sets", _do)


def resolve_refs(document, refs):
    """(link id or 0, element id) pairs -> (host ids, [(link instance, element)]).

    Must run in an API context. Deleted elements / unloaded links are dropped.
    """
    host_ids, linked, links = [], [], {}
    for link_int, eid in refs:
        if link_int == 0:
            if document.GetElement(make_element_id(eid)) is not None:
                host_ids.append(eid)
            continue
        if link_int not in links:
            inst = document.GetElement(make_element_id(link_int))
            link_doc = None
            try:
                link_doc = inst.GetLinkDocument() if inst is not None else None
            except Exception:
                pass
            links[link_int] = (inst, link_doc)
        inst, link_doc = links[link_int]
        if link_doc is None:
            continue
        element = link_doc.GetElement(make_element_id(eid))
        if element is not None:
            linked.append((inst, element))
    return host_ids, linked


def link_instance_ids(linked):
    return sorted(set(element_id_value(inst.Id) for inst, _el in linked))


def select_refs(ui_document, host_ids, linked):
    """Select host AND linked elements.

    Selection.SetReferences (Revit 2023+) accepts link references, so the
    linked beam / wall itself is selected, not the whole link. Older
    behaviour is kept as a fallback: host ids + the link instances.
    """
    try:
        refs = List[Reference]()
        for eid in host_ids:
            refs.Add(Reference(ui_document.Document.GetElement(make_element_id(eid))))
        for inst, element in linked:
            refs.Add(Reference(element).CreateLinkReference(inst))
        ui_document.Selection.SetReferences(refs)
        return
    except Exception as err:
        logger.debug("SetReferences unavailable (%s) - selecting link instances", err)
    ui_document.Selection.SetElementIds(
        to_id_list(list(host_ids) + link_instance_ids(linked)))


def zoom_to_points(ui_document, view, points, padding):
    """Zoom the open UIView of 'view' to the box around the given points.

    Tighter than UIDocument.ShowElements, which zooms to the full extent of a
    30 m duct instead of the clash on it. Returns False if the view is not
    open, so the caller can fall back to ShowElements.
    """
    if not points:
        return False
    ui_view = None
    for candidate in ui_document.GetOpenUIViews():
        if candidate.ViewId == view.Id:
            ui_view = candidate
            break
    if ui_view is None:
        return False
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    zs = [p[2] for p in points]
    ui_view.ZoomAndCenterRectangle(
        XYZ(min(xs) - padding, min(ys) - padding, min(zs) - padding),
        XYZ(max(xs) + padding, max(ys) + padding, max(zs) + padding))
    return True


# ---------------------------------------------------------------------------
# SECTION 11 - EXCEL EXPORT (requirement 6)
# ---------------------------------------------------------------------------
_DECIMAL_HEADERS = set(["X (ft)", "Y (ft)", "Z (ft)", "Intersection Volume (ft3)",
                        "Centre X (ft)", "Centre Y (ft)", "Centre Z (ft)",
                        "Extent (ft)"])


def build_summary(clashes, clash_sets, stats, active_view, radius_mm):
    """Summary statistics block (list of (label, value))."""
    by_type, by_level, by_colour = {}, {}, {}
    for clash in clashes:
        by_type[clash.ClashType] = by_type.get(clash.ClashType, 0) + 1
        by_level[clash.Level] = by_level.get(clash.Level, 0) + 1
        by_colour[clash.Colour] = by_colour.get(clash.Colour, 0) + 1

    model_name = doc.Title
    try:
        if doc.PathName:
            model_name = os.path.basename(doc.PathName)
    except Exception:
        pass

    multi = [s for s in clash_sets if s.ClashCount > 1]
    largest = max(clash_sets, key=lambda s: s.ClashCount) if clash_sets else None

    rows = [
        ("MEP Clash Detector - Report", ""),
        ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Revit model", model_name),
        ("Revit version", "{0} {1}".format(app.VersionName, app.VersionBuild)),
        ("Active view", active_view.Name),
        ("Elements processed", stats.get("elements", 0)),
        ("Candidate pairs tested", stats.get("pairs", 0)),
        ("Total clashes", len(clashes)),
        ("Detection cancelled (partial)", "Yes" if stats.get("cancelled") else "No"),
        ("Detection time (s)", round(stats.get("seconds", 0.0), 1)),
        ("Min clash volume (ft3)", Config.MIN_CLASH_VOLUME),
        ("Linked elements in scope", stats.get("linked", 0)),
        ("Arch / Struct elements in scope", stats.get("building", 0)),
        ("", ""),
        ("Models checked", ""),
    ]
    for name, mep, building, count in stats.get("sources", []):
        roles = " + ".join([r for r, on in (("MEP", mep), ("Arch/Struct", building))
                            if on])
        rows.append((u"   {0} ({1})".format(name, roles), count))
    rows += [
        ("", ""),
        ("Clash set radius (mm)", radius_mm),
        ("Clash sets", len(clash_sets)),
        ("Sets with multiple clashes", len(multi)),
        ("Clashes inside multi-clash sets", sum(s.ClashCount for s in multi)),
        ("Largest set", "{0} ({1} clashes)".format(largest.SetName, largest.ClashCount)
         if largest else "-"),
        ("", ""),
        ("Clashes by type", ""),
    ]
    for key in sorted(by_type.keys()):
        rows.append(("   " + key, by_type[key]))
    by_class = {}
    for clash in clashes:
        by_class[clash.ClashClass] = by_class.get(clash.ClashClass, 0) + 1
    rows.append(("", ""))
    rows.append(("Clashes by class", ""))
    for key in sorted(by_class.keys()):
        rows.append(("   " + key, by_class[key]))
    rows.append(("", ""))
    rows.append(("Clashes by level", ""))
    for key in sorted(by_level.keys()):
        rows.append(("   " + key, by_level[key]))
    rows.append(("", ""))
    rows.append(("Clashes by highlight colour", ""))
    for key in sorted(by_colour.keys()):
        rows.append(("   " + key, by_colour[key]))
    return rows


def build_sheets(clashes, clash_sets, summary_rows):
    """[(sheet name, headers, rows)] shared by both export back-ends."""
    return [
        ("Clashes", EXPORT_HEADERS, [c.as_row() for c in clashes]),
        ("Clash Sets", SET_EXPORT_HEADERS, [s.as_row() for s in clash_sets]),
        ("Summary", ["Item", "Value"], [list(r) for r in summary_rows]),
    ]


def export_with_openpyxl(path, sheets):
    """Preferred export path - requires openpyxl (CPython engine)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="1F4E79")
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    wkb = Workbook()
    wkb.remove(wkb.active)
    for name, headers, rows in sheets:
        sht = wkb.create_sheet(name)
        sht.append(headers)
        for col_idx in range(1, len(headers) + 1):
            cell = sht.cell(row=1, column=col_idx)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center",
                                       wrap_text=True)
            cell.border = border
        for row in rows:
            sht.append(list(row))
        for col_idx, head in enumerate(headers, 1):
            if head in _DECIMAL_HEADERS:
                for row_idx in range(2, sht.max_row + 1):
                    sht.cell(row=row_idx, column=col_idx).number_format = "0.000"
        sht.freeze_panes = "A2"
        sht.auto_filter.ref = "A1:{0}{1}".format(
            get_column_letter(len(headers)), max(sht.max_row, 1))
        for col_idx in range(1, len(headers) + 1):
            longest = len(str(headers[col_idx - 1]))
            for row_idx in range(2, sht.max_row + 1):
                val = sht.cell(row=row_idx, column=col_idx).value
                if val is not None:
                    longest = max(longest, len(u"{0}".format(val)))
            sht.column_dimensions[get_column_letter(col_idx)].width = \
                min(max(longest + 2, 10), 60)
    wkb.save(path)
    return path


# --- Fallback writer (pure .NET, always available under IronPython) ---------
try:
    _NUMERIC_TYPES = (int, long, float)     # noqa: F821  IronPython 2
except NameError:
    _NUMERIC_TYPES = (int, float)           # Python 3


def _xml_escape(value):
    text = u"{0}".format(value)
    return (text.replace(u"&", u"&amp;").replace(u"<", u"&lt;")
                .replace(u">", u"&gt;").replace(u'"', u"&quot;"))


def _col_letter(index):
    """1 -> A, 27 -> AA"""
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _sheet_xml(headers, data_rows):
    """Build a minimal but valid SpreadsheetML worksheet."""
    parts = [
        u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        u'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        u'<sheetViews><sheetView workbookViewId="0">'
        u'<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        u'</sheetView></sheetViews>',
        u'<cols>',
    ]
    for i in range(1, len(headers) + 1):
        parts.append(u'<col min="{0}" max="{0}" width="20" customWidth="1"/>'.format(i))
    parts.append(u'</cols><sheetData>')

    def cell(col, row, value, style=None):
        ref = u"{0}{1}".format(_col_letter(col), row)
        style_attr = u' s="{0}"'.format(style) if style else u""
        if isinstance(value, _NUMERIC_TYPES) and not isinstance(value, bool):
            return u'<c r="{0}"{1}><v>{2}</v></c>'.format(ref, style_attr, value)
        return (u'<c r="{0}"{1} t="inlineStr"><is><t xml:space="preserve">{2}</t>'
                u'</is></c>'.format(ref, style_attr, _xml_escape(value)))

    parts.append(u'<row r="1" ht="28" customHeight="1">')
    for col, head in enumerate(headers, start=1):
        parts.append(cell(col, 1, head, style=1))
    parts.append(u'</row>')

    row_index = 1
    for data in data_rows:
        row_index += 1
        parts.append(u'<row r="{0}">'.format(row_index))
        for col, value in enumerate(data, start=1):
            parts.append(cell(col, row_index, value))
        parts.append(u'</row>')

    parts.append(u'</sheetData>')
    parts.append(u'<autoFilter ref="A1:{0}{1}"/>'.format(
        _col_letter(len(headers)), max(len(data_rows) + 1, 1)))
    parts.append(u'</worksheet>')
    return u"".join(parts)


_STYLES_XML = (
    u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    u'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    u'<fonts count="2">'
    u'<font><sz val="11"/><name val="Calibri"/></font>'
    u'<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
    u'</fonts>'
    u'<fills count="3">'
    u'<fill><patternFill patternType="none"/></fill>'
    u'<fill><patternFill patternType="gray125"/></fill>'
    u'<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E79"/>'
    u'<bgColor indexed="64"/></patternFill></fill>'
    u'</fills>'
    u'<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    u'<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    u'<cellXfs count="2">'
    u'<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    u'<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" '
    u'applyFill="1" applyAlignment="1">'
    u'<alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
    u'</cellXfs>'
    u'<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    u'</styleSheet>'
)

_ROOT_RELS_XML = (
    u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    u'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    u'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    u'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
)

_SPREADSHEET_CT = u"application/vnd.openxmlformats-officedocument.spreadsheetml."
_REL_NS = u"http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def _package_parts(sheets):
    """All XML parts of an .xlsx holding the given sheets."""
    count = len(sheets)
    content_types = [
        u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        u'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        u'<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
        u'package.relationships+xml"/>'
        u'<Default Extension="xml" ContentType="application/xml"/>'
        u'<Override PartName="/xl/workbook.xml" ContentType="{0}sheet.main+xml"/>'
        u'<Override PartName="/xl/styles.xml" ContentType="{0}styles+xml"/>'
        .format(_SPREADSHEET_CT)]
    workbook = [
        u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        u'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        u'xmlns:r="{0}"><sheets>'.format(_REL_NS[:-1])]
    rels = [
        u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        u'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    parts = {"_rels/.rels": _ROOT_RELS_XML, "xl/styles.xml": _STYLES_XML}

    for idx, (name, headers, rows) in enumerate(sheets, 1):
        content_types.append(
            u'<Override PartName="/xl/worksheets/sheet{0}.xml" '
            u'ContentType="{1}worksheet+xml"/>'.format(idx, _SPREADSHEET_CT))
        workbook.append(u'<sheet name="{0}" sheetId="{1}" r:id="rId{1}"/>'
                        .format(_xml_escape(name[:31]), idx))
        rels.append(u'<Relationship Id="rId{0}" Type="{1}worksheet" '
                    u'Target="worksheets/sheet{0}.xml"/>'.format(idx, _REL_NS))
        parts["xl/worksheets/sheet{0}.xml".format(idx)] = _sheet_xml(headers, rows)

    rels.append(u'<Relationship Id="rId{0}" Type="{1}styles" Target="styles.xml"/>'
                .format(count + 1, _REL_NS))
    content_types.append(u'</Types>')
    workbook.append(u'</sheets></workbook>')
    rels.append(u'</Relationships>')
    parts["[Content_Types].xml"] = u"".join(content_types)
    parts["xl/workbook.xml"] = u"".join(workbook)
    parts["xl/_rels/workbook.xml.rels"] = u"".join(rels)
    return parts


def export_with_dotnet(path, sheets):
    """XLSX writer built on System.IO.Compression (no openpyxl needed)."""
    from System.IO import File, FileMode, FileAccess, StreamWriter
    from System.IO.Compression import ZipArchive, ZipArchiveMode, CompressionLevel
    from System.Text import UTF8Encoding

    parts = _package_parts(sheets)
    if File.Exists(path):
        File.Delete(path)

    stream = File.Open(path, FileMode.Create, FileAccess.Write)
    try:
        archive = ZipArchive(stream, ZipArchiveMode.Create)
        try:
            encoding = UTF8Encoding(False)
            for entry_name, xml in parts.items():
                entry = archive.CreateEntry(entry_name, CompressionLevel.Optimal)
                writer = StreamWriter(entry.Open(), encoding)
                writer.Write(xml)
                writer.Flush()
                writer.Close()
        finally:
            archive.Dispose()
    finally:
        stream.Close()
    return path


def export_to_excel(path, clashes, clash_sets, stats, active_view, radius_mm):
    """Write the report to 'path'. Must run in a Revit API context."""
    summary_rows = build_summary(clashes, clash_sets, stats, active_view, radius_mm)
    sheets = build_sheets(clashes, clash_sets, summary_rows)
    try:
        export_with_openpyxl(path, sheets)
        logger.info("Exported %s clashes with openpyxl -> %s", len(clashes), path)
    except ImportError:
        logger.info("openpyxl not available on this engine - using .NET writer")
        export_with_dotnet(path, sheets)
    except Exception as err:
        logger.warning("openpyxl export failed (%s) - using .NET writer", err)
        export_with_dotnet(path, sheets)
    return path


def _safe_filename(name):
    keep = "-_ ()"
    return "".join(c for c in name if c.isalnum() or c in keep).strip() or "View"


# ---------------------------------------------------------------------------
# SECTION 12 - EXTERNAL EVENT (Revit API access from the modeless window)
# ---------------------------------------------------------------------------
class RevitActionHandler(IExternalEventHandler):
    """Runs python callables, queued by the window, in a valid API context.

    A modeless window's click handlers run OUTSIDE any Revit API context:
    starting a transaction, changing the selection or zooming there is
    unsupported and can corrupt the session. The window therefore posts a
    callable here and raises the ExternalEvent; Revit calls Execute() as soon
    as it is idle.

    Actions posted with the same key replace each other while still pending,
    so scrolling through 50 rows with the arrow keys zooms once, not 50 times.
    """

    def __init__(self):
        self._pending = []

    def post(self, key, action):
        if key is not None:
            self._pending = [item for item in self._pending if item[0] != key]
        self._pending.append((key, action))

    def Execute(self, uiapp):
        pending, self._pending = self._pending, []
        for _key, action in pending:
            try:
                action(uiapp)
            except DetectionCancelled:
                pass
            except Exception as err:
                logger.error("MEP Clash Detector action failed: %s\n%s",
                             err, traceback.format_exc())

    def GetName(self):
        return "MEP Clash Detector"


# One results window per Revit session. AppDomain data survives between
# button clicks (the engine is persistent), so a second click re-activates
# the open window instead of stacking a new one on top of it.
_WINDOW_SLOT = "MEPClashDetector.ResultsWindow"


def _get_open_window():
    try:
        window = AppDomain.CurrentDomain.GetData(_WINDOW_SLOT)
        if window is not None and getattr(window, "is_open", False):
            return window
    except Exception:
        pass
    return None


def _register_window(window):
    AppDomain.CurrentDomain.SetData(_WINDOW_SLOT, window)


# ---------------------------------------------------------------------------
# SECTION 13 - MODELESS WPF RESULTS WINDOW (requirement 5)
# ---------------------------------------------------------------------------
def _observable(items):
    collection = ObservableCollection[object]()
    for item in items:
        collection.Add(item)
    return collection


def _sort_items(items, path, ascending):
    """Sort python objects by attribute; numeric-looking text sorts as number."""
    def key_func(obj):
        value = getattr(obj, path, "")
        if isinstance(value, _NUMERIC_TYPES) and not isinstance(value, bool):
            return (0, float(value), u"")
        try:
            return (0, float(value), u"")
        except (TypeError, ValueError):
            return (1, 0.0, u"{0}".format(value).lower())
    return sorted(items, key=key_func, reverse=not ascending)


def _is_row_event(grid, args):
    """True if a mouse event came from a data row (not header / scrollbar)."""
    try:
        return isinstance(ItemsControl.ContainerFromElement(
            grid, args.OriginalSource), DataGridRow)
    except Exception:
        return False


class ClashResultsWindow(forms.WPFWindow):
    """Modeless results browser.

    Threading / API rules followed here:
      * UI-only work (search, filter, sort, regroup) runs directly.
      * Anything touching the model, the selection or the view is wrapped in
        a callable and executed through RevitActionHandler / ExternalEvent.
      * Every API action re-validates that the document and view still exist
        and that the document is the active one (the user may have switched
        models while the window was open).
      * The window closes itself when its document is closed.
    """

    def __init__(self, xaml_path, clashes, stats, active_view, highlighter,
                 handler, ext_event, scope):
        forms.WPFWindow.__init__(self, xaml_path)
        self.is_open = True

        # Everything that needs the API is captured now, while the command
        # is still running in a valid API context.
        self._doc = active_view.Document
        self.doc_title = self._doc.Title
        self._view = active_view
        self._view_name = active_view.Name
        self.view_unique_id = active_view.UniqueId
        self._app = self._doc.Application
        self._highlighter = highlighter
        self._handler = handler
        self._ext_event = ext_event

        self._scope = scope
        self._radius_mm = Config.SET_RADIUS_MM
        self._sort = {"clash": [None, True], "set": [None, True]}
        self._suspend_events = False
        self._all_clashes, self._sets = [], []
        self._base, self._visible_sets, self._visible_clashes = [], [], []

        self.grid = self.FindName("clash_grid")
        self.set_grid = self.FindName("set_grid")
        self.search_box = self.FindName("search_box")
        self.type_filter = self.FindName("type_filter")
        self.class_filter = self.FindName("class_filter")
        self.radius_box = self.FindName("radius_box")
        self.multi_only = self.FindName("multi_only")
        self.status_text = self.FindName("status_text")
        self.summary_text = self.FindName("summary_text")
        self.clash_header = self.FindName("clash_header")

        self.Title = u"MEP Clash Detector - {0}".format(self._view_name)
        self.radius_box.Text = u"{0:g}".format(self._radius_mm)
        self._load_results(clashes, stats)
        self._wire_events()

    # -- wiring --------------------------------------------------------------
    def _wire_events(self):
        self.search_box.TextChanged += self.on_filters_changed
        self.type_filter.SelectionChanged += self.on_filters_changed
        self.class_filter.SelectionChanged += self.on_filters_changed
        self.multi_only.Checked += self.on_filters_changed
        self.multi_only.Unchecked += self.on_filters_changed
        self.radius_box.KeyDown += self.on_radius_key

        self.set_grid.Sorting += self.on_set_sorting
        self.set_grid.SelectionChanged += self.on_set_selected
        self.set_grid.MouseDoubleClick += self.on_set_double_click

        self.grid.Sorting += self.on_clash_sorting
        self.grid.SelectionChanged += self.on_clash_selected
        self.grid.MouseDoubleClick += self.on_clash_double_click

        for name, handler in (("btn_export", self.on_export),
                              ("btn_save_sets", self.on_save_sets),
                              ("btn_scope", self.on_scope),
                              ("btn_refresh", self.on_refresh),
                              ("btn_isolate", self.on_isolate),
                              ("btn_clear", self.on_clear_highlights),
                              ("btn_regroup", self.on_regroup),
                              ("btn_all_sets", self.on_show_all_sets),
                              ("btn_close", self.on_close)):
            self.FindName(name).Click += handler

        self.Closed += self._on_closed
        self._doc_closing = EventHandler[DocumentClosingEventArgs](
            self._on_doc_closing)
        self._app.DocumentClosing += self._doc_closing

    # -- small helpers -------------------------------------------------------
    def _set_status(self, text):
        self.status_text.Text = text

    def _info(self, text, title="MEP Clash Detector"):
        MessageBox.Show(self, text, title, MessageBoxButton.OK,
                        MessageBoxImage.Information)

    def _ask(self, text, title="MEP Clash Detector"):
        return MessageBox.Show(self, text, title, MessageBoxButton.YesNo,
                               MessageBoxImage.Question) == MessageBoxResult.Yes

    def _selected_clash(self):
        return self.grid.SelectedItem

    def _selected_set(self):
        return self.set_grid.SelectedItem

    # -- ExternalEvent plumbing ---------------------------------------------
    def _post(self, key, action, busy_text=None):
        """Queue an API action and ask Revit to run it when idle."""
        if not self.is_open:
            return
        self._handler.post(key, action)
        try:
            request = self._ext_event.Raise()
        except Exception as err:
            self._set_status(u"Could not reach Revit: {0}".format(err))
            return
        if request == ExternalEventRequest.Denied:
            self._set_status("Revit refused the request (is a command or edit "
                             "mode active?). Finish it and click again.")
        elif request == ExternalEventRequest.TimedOut:
            self._set_status("Revit is busy - the action will run when it is idle.")
        elif busy_text:
            self._set_status(busy_text)

    def _api_context(self, uiapp, require_active=True):
        """Validate doc / view inside Execute(). Returns the UIDocument or None."""
        try:
            alive = self._doc.IsValidObject and self._view.IsValidObject
        except Exception:
            alive = False
        if not alive:
            self._set_status("The model or view of these results no longer "
                             "exists. Close this window and run the tool again.")
            return None
        uidocument = uiapp.ActiveUIDocument
        is_active = (uidocument is not None
                     and uidocument.Document.Equals(self._doc))
        if require_active and not is_active:
            self._set_status(u"Switch back to '{0}' to use this action."
                             .format(self.doc_title))
            return None
        return uidocument if is_active else True

    def _isolation_ids(self, refs):
        """Host ids + the link instances of linked refs (API context).

        Isolating a single linked element is impossible in Revit, so the whole
        link instance is kept visible next to the isolated host elements.
        """
        host_ids, linked = resolve_refs(self._doc, refs)
        return host_ids + link_instance_ids(linked)

    # -- data plumbing (UI only) ---------------------------------------------
    def _load_results(self, clashes, stats):
        self._all_clashes = list(clashes)
        self._stats = stats
        if self._sort["clash"][0]:
            self._all_clashes = _sort_items(self._all_clashes,
                                            *self._sort["clash"])
        self._regroup()
        self._populate_type_filter()
        self._refresh_all()

    def _regroup(self):
        self._sets = group_clashes(self._all_clashes, self._radius_mm)
        if self._sort["set"][0]:
            self._sets = _sort_items(self._sets, *self._sort["set"])

    def _populate_type_filter(self):
        self._suspend_events = True
        try:
            for combo, first, values in (
                    (self.type_filter, "All clash types",
                     sorted(set([c.ClashType for c in self._all_clashes]))),
                    (self.class_filter, "All classes",
                     sorted(set([c.ClashClass for c in self._all_clashes])))):
                combo.Items.Clear()
                combo.Items.Add(first)
                for value in values:
                    combo.Items.Add(value)
                combo.SelectedIndex = 0
        finally:
            self._suspend_events = False

    def _refresh_all(self):
        """Search / type / multi-only filters -> sets grid -> clash grid."""
        needle = (self.search_box.Text or u"").strip().lower()
        selected_type = self.type_filter.SelectedItem
        if selected_type == "All clash types":
            selected_type = None
        selected_class = self.class_filter.SelectedItem
        if selected_class == "All classes":
            selected_class = None
        self._base = [c for c in self._all_clashes
                      if c.matches(needle)
                      and (not selected_type or c.ClashType == selected_type)
                      and (not selected_class or c.ClashClass == selected_class)]
        base_sets = set(c.SetId for c in self._base)
        multi_only = bool(self.multi_only.IsChecked)
        self._visible_sets = [s for s in self._sets
                              if s.SetId in base_sets
                              and (not multi_only or s.ClashCount > 1)]

        previous = self._selected_set()
        previous_id = previous.SetId if previous is not None else None
        self._suspend_events = True
        try:
            self.set_grid.ItemsSource = _observable(self._visible_sets)
            for clash_set in self._visible_sets:
                if clash_set.SetId == previous_id:
                    self.set_grid.SelectedItem = clash_set
                    break
        finally:
            self._suspend_events = False
        self._restore_sort_glyph(self.set_grid, "set")
        self._refresh_clash_grid()

    def _refresh_clash_grid(self):
        selected = self._selected_set()
        visible_ids = set(s.SetId for s in self._visible_sets)
        self._visible_clashes = [
            c for c in self._base
            if c.SetId in visible_ids
            and (selected is None or c.SetId == selected.SetId)]
        self._suspend_events = True
        try:
            self.grid.ItemsSource = _observable(self._visible_clashes)
        finally:
            self._suspend_events = False
        self._restore_sort_glyph(self.grid, "clash")
        self.clash_header.Text = (
            u"Clashes in {0}".format(selected.SetName) if selected is not None
            else u"All listed clashes")
        self._update_status()

    def _restore_sort_glyph(self, grid, which):
        """Rebinding ItemsSource wipes SortDirection - put the arrow back."""
        path, ascending = self._sort[which]
        for column in grid.Columns:
            if path and column.SortMemberPath == path:
                column.SortDirection = (ListSortDirection.Ascending if ascending
                                        else ListSortDirection.Descending)
            else:
                column.SortDirection = None

    def _update_status(self):
        multi = [s for s in self._sets if s.ClashCount > 1]
        cancelled = ("   |   DETECTION CANCELLED - results are partial"
                     if self._stats.get("cancelled") else "")
        self._set_status(
            u"Showing {0} of {1} clashes in {2} sets   |   {3} elements "
            u"({4} linked, {5} arch/struct) from {6} models   |   "
            u"{7} pairs tested   |   {8:.1f} s{9}".format(
                len(self._visible_clashes), len(self._all_clashes),
                len(self._visible_sets), self._stats.get("elements", 0),
                self._stats.get("linked", 0), self._stats.get("building", 0),
                len(self._stats.get("sources", [])),
                self._stats.get("pairs", 0), self._stats.get("seconds", 0.0),
                cancelled))
        by_class = {}
        for clash in self._all_clashes:
            by_class[clash.ClashClass] = by_class.get(clash.ClashClass, 0) + 1
        types = u"  |  ".join([u"{0}: {1}".format(k, by_class[k])
                               for k in sorted(by_class.keys())])
        sets = (u"Clash sets (radius {0:g} mm): {1} total, {2} with multiple "
                u"clashes covering {3} clashes".format(
                    self._radius_mm, len(self._sets), len(multi),
                    sum(s.ClashCount for s in multi)))
        self.summary_text.Text = (sets + u"\n" + types) if types else \
            u"No clashes found."

    # -- UI-only event handlers ----------------------------------------------
    def on_filters_changed(self, sender, args):
        if self._suspend_events:
            return
        try:
            self._refresh_all()
        except Exception as err:
            logger.error("Filter failed: %s", err)

    def on_radius_key(self, sender, args):
        if args.Key == Key.Enter:
            self.on_regroup(sender, args)

    def on_regroup(self, sender, args):
        try:
            radius = float((self.radius_box.Text or u"").replace(u",", u"."))
            if radius < 0:
                raise ValueError()
        except ValueError:
            self._info("Set radius must be a number of millimetres (0 or more).")
            self.radius_box.Text = u"{0:g}".format(self._radius_mm)
            return
        self._radius_mm = radius
        self.set_grid.UnselectAll()
        self._regroup()
        self._refresh_all()

    def on_show_all_sets(self, sender, args):
        self.set_grid.UnselectAll()     # SelectionChanged refreshes the grid

    def _sort_grid(self, which, args):
        path = args.Column.SortMemberPath
        args.Handled = True
        if not path:
            return
        state = self._sort[which]
        ascending = (not state[1]) if state[0] == path else True
        state[0], state[1] = path, ascending
        if which == "clash":
            self._all_clashes = _sort_items(self._all_clashes, path, ascending)
        else:
            self._sets = _sort_items(self._sets, path, ascending)
        self._refresh_all()

    def on_set_sorting(self, sender, args):
        try:
            self._sort_grid("set", args)
        except Exception as err:
            logger.error("Sorting failed: %s", err)

    def on_clash_sorting(self, sender, args):
        try:
            self._sort_grid("clash", args)
        except Exception as err:
            logger.error("Sorting failed: %s", err)

    # -- selection -> zoom (API) ---------------------------------------------
    def on_set_selected(self, sender, args):
        if self._suspend_events:
            return
        self._refresh_clash_grid()
        clash_set = self._selected_set()
        if clash_set is not None:
            self._zoom(clash_set.points, clash_set.refs,
                       u"{0} ({1} clashes)".format(clash_set.SetName,
                                                   clash_set.ClashCount))

    def on_clash_selected(self, sender, args):
        if self._suspend_events:
            return
        clash = self._selected_clash()
        if clash is not None:
            self._zoom([(clash.PointX, clash.PointY, clash.PointZ)],
                       clash.refs, clash.ClashName)

    def _zoom(self, points, refs, label):
        def action(uiapp):
            uidocument = self._api_context(uiapp)
            if uidocument is None:
                return
            if uidocument.ActiveView.Id != self._view.Id:
                try:
                    uidocument.ActiveView = self._view
                except Exception:
                    uidocument.RequestViewChange(self._view)
            host_ids, linked = resolve_refs(self._doc, refs)
            select_refs(uidocument, host_ids, linked)
            zoomed = zoom_to_points(uidocument, self._view, points,
                                    Config.ZOOM_PADDING_FT)
            if not zoomed and host_ids:
                uidocument.ShowElements(to_id_list(host_ids))
            uidocument.RefreshActiveView()
            self._set_status(u"Selected and zoomed to {0}.".format(label))
        self._post("zoom", action)

    def on_set_double_click(self, sender, args):
        if not _is_row_event(self.set_grid, args):
            return
        clash_set = self._selected_set()
        if clash_set is not None:
            self._isolate(clash_set.refs, clash_set.SetName)

    def on_clash_double_click(self, sender, args):
        if not _is_row_event(self.grid, args):
            return
        clash = self._selected_clash()
        if clash is not None:
            self._isolate(clash.refs, clash.ClashName)

    # -- buttons (API) -------------------------------------------------------
    def _isolate(self, refs, label):
        def action(uiapp):
            if self._api_context(uiapp) is None:
                return
            ids = self._isolation_ids(refs)
            if ids and self._highlighter.isolate(ids):
                self._set_status(u"Isolated {0} elements ({1}).".format(len(ids), label))
            else:
                self._set_status(u"Nothing to isolate for {0}.".format(label))
        self._post("isolate", action, u"Isolating {0}...".format(label))

    def on_isolate(self, sender, args):
        clash, clash_set = self._selected_clash(), self._selected_set()
        if clash is not None:
            self._isolate(clash.refs, clash.ClashName)
        elif clash_set is not None:
            self._isolate(clash_set.refs, clash_set.SetName)
        else:
            ids = set()
            for item in self._visible_clashes:
                ids.update(item.refs)
            if not ids:
                self._info("Nothing to isolate.")
                return
            self._isolate(sorted(ids), "all listed clashes")

    def on_clear_highlights(self, sender, args):
        def action(uiapp):
            if self._api_context(uiapp) is None:
                return
            cleared = self._highlighter.clear()
            self._set_status(u"Cleared overrides on {0} elements.".format(cleared))
        self._post("clear", action, "Clearing highlights...")

    def _rerun(self):
        """Re-run detection with self._scope (API context only)."""
        try:
            clashes, stats = detect_clashes(self._view, self._scope)
        except DetectionCancelled:
            self._set_status("Run cancelled - previous results kept.")
            return
        run_in_transaction(
            self._doc, "MEP Clash Detector - Reset view mode",
            lambda: self._view.DisableTemporaryViewMode(
                TemporaryViewMode.TemporaryHideIsolate))
        self._highlighter.apply(clashes)     # also resets stale colours
        self.set_grid.UnselectAll()
        self._load_results(clashes, stats)

    def on_refresh(self, sender, args):
        def action(uiapp):
            if self._api_context(uiapp) is None:
                return
            self._rerun()
        self._post("refresh", action, "Re-running clash detection...")

    def on_scope(self, sender, args):
        """Change models / ARC / STR categories, then re-run."""
        def action(uiapp):
            if self._api_context(uiapp) is None:
                return
            scope = ask_scope(self._doc, self._scope)   # modal, in API context
            if scope is None:
                self._set_status("Scope unchanged.")
                return
            self._scope = scope
            save_scope(scope)
            self._rerun()
        self._post("scope", action, "Opening clash scope...")

    def on_export(self, sender, args):
        if not self._all_clashes:
            self._info("There is nothing to export.")
            return

        def action(uiapp):
            if self._api_context(uiapp, require_active=False) is None:
                return
            default_name = "ClashReport_{0}_{1}.xlsx".format(
                _safe_filename(self._view_name),
                datetime.now().strftime("%Y%m%d_%H%M"))
            path = forms.save_file(file_ext="xlsx", default_name=default_name)
            if not path:
                self._set_status("Export cancelled.")
                return
            try:
                export_to_excel(path, self._all_clashes, self._sets,
                                self._stats, self._view, self._radius_mm)
            except Exception as err:
                logger.error("Export failed: %s\n%s", err, traceback.format_exc())
                self._set_status(u"Export failed: {0}".format(err))
                return
            self._set_status(u"Exported to {0}".format(path))
            if self._ask(u"Report saved:\n{0}\n\nShow it in Explorer?".format(path),
                         "Export complete"):
                Process.Start("explorer.exe", u'/select,"{0}"'.format(path))
        self._post("export", action, "Exporting...")

    def on_save_sets(self, sender, args):
        multi_only = bool(self.multi_only.IsChecked)
        candidates = [s for s in self._sets if not multi_only or s.ClashCount > 1]
        if not candidates:
            self._info("There are no clash sets to save.")
            return
        prefix = selection_set_prefix(self._view_name)
        if not self._ask(
                u"Create / update {0} Revit Selection Filters named\n"
                u"'{1}Set-###'?\n\n{2}Only HOST elements can be stored in a "
                u"selection filter - linked elements are left out.\n\n"
                u"Filters with this prefix from an earlier "
                u"run are updated, leftovers are deleted.".format(
                    len(candidates), prefix,
                    u"" if multi_only else
                    u"Tip: tick 'Multi-clash sets only' to skip single clashes.\n\n")):
            return

        def action(uiapp):
            if self._api_context(uiapp, require_active=False) is None:
                return
            result = save_selection_sets(self._doc, self._view, candidates)
            if result is None:
                self._set_status("Saving selection sets failed - see the pyRevit "
                                 "output window.")
            else:
                skipped = (u", {0} sets skipped (only linked elements - Revit "
                           u"cannot store those)".format(result[2])
                           if result[2] else u"")
                self._set_status(u"Saved {0} selection sets (host elements), "
                                 u"removed {1} old ones{2}. Manage > Selection > "
                                 u"Load.".format(result[0], result[1], skipped))
        self._post("save_sets", action, "Saving selection sets...")

    def on_close(self, sender, args):
        """Close button - highlights intentionally stay in the view."""
        self.Close()

    # -- lifetime --------------------------------------------------------------
    def _on_doc_closing(self, sender, args):
        try:
            if args.Document.Equals(self._doc):
                self.Close()
        except Exception:
            pass

    def _on_closed(self, sender, args):
        self.is_open = False
        try:
            self._app.DocumentClosing -= self._doc_closing
        except Exception:
            pass
        try:
            self._ext_event.Dispose()
        except Exception:
            pass
        try:
            if AppDomain.CurrentDomain.GetData(_WINDOW_SLOT) is self:
                AppDomain.CurrentDomain.SetData(_WINDOW_SLOT, None)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# SECTION 14 - CLASH SCOPE DIALOG (models + ARC / STR categories)
# ---------------------------------------------------------------------------
class ScopeWindow(forms.WPFWindow):
    """Modal dialog: which models and which ARC / STR categories to check.

    Always shown from an API context (the command itself, or an
    ExternalEvent when opened from the results window).
    """

    def __init__(self, xaml_path, host_title, link_rows, scope):
        forms.WPFWindow.__init__(self, xaml_path)
        self.result = None
        self._scope = scope
        self._link_boxes = []            # (row, mep checkbox, arc/str checkbox)
        self._cat_boxes = {}             # building key -> checkbox

        models = self.FindName("models_panel")
        self._host_mep, self._host_bld = self._add_model_row(
            models, u"Host model: {0}".format(host_title),
            scope.get("host_mep", True), scope.get("host_building", False), True)
        links_cfg = scope.get("links", {})
        for row in link_rows:
            cfg = links_cfg.get(row["key"])
            if cfg is None:
                mep, bld = guess_link_roles(row["name"])
            else:
                mep, bld = cfg.get("mep", False), cfg.get("building", False)
            label = u"Link: {0}{1}".format(
                row["name"], u"" if row["loaded"] else u"   (not loaded)")
            mep_cb, bld_cb = self._add_model_row(models, label, mep, bld,
                                                 row["loaded"])
            self._link_boxes.append((row, mep_cb, bld_cb))
        if not link_rows:
            note = TextBlock()
            note.Text = u"This model has no Revit links."
            note.Foreground = Brushes.Gray
            note.Margin = Thickness(0, 4, 0, 0)
            models.Children.Add(note)

        chosen = set(scope.get("building_keys", []))
        panels = {"ARC": self.FindName("arc_panel"), "STR": self.FindName("str_panel")}
        for definition in BUILDING_CATEGORIES:
            box = CheckBox()
            box.Content = definition.label
            box.IsChecked = definition.key in chosen
            box.Width = 200
            box.Margin = Thickness(0, 3, 0, 3)
            panels[definition.discipline].Children.Add(box)
            self._cat_boxes[definition.key] = box

        self.mep_vs_mep = self.FindName("mep_vs_mep")
        self.mep_vs_mep.IsChecked = bool(scope.get("mep_vs_mep", True))

        for name, handler in (("btn_run", self.on_run),
                              ("btn_cancel", self.on_cancel),
                              ("btn_arc_all", lambda s, a: self._tick("ARC", True)),
                              ("btn_arc_none", lambda s, a: self._tick("ARC", False)),
                              ("btn_str_all", lambda s, a: self._tick("STR", True)),
                              ("btn_str_none", lambda s, a: self._tick("STR", False))):
            self.FindName(name).Click += handler

    @staticmethod
    def _add_model_row(panel, label, mep, bld, enabled):
        row = StackPanel()
        row.Orientation = Orientation.Horizontal
        row.Margin = Thickness(0, 2, 0, 2)
        text = TextBlock()
        text.Text = label
        text.ToolTip = label
        text.Width = 470
        text.TextTrimming = TextTrimming.CharacterEllipsis
        text.VerticalAlignment = VerticalAlignment.Center
        if not enabled:
            text.Foreground = Brushes.Gray
        boxes = []
        for caption, value in (("MEP", mep), ("Arch / Struct", bld)):
            box = CheckBox()
            box.Content = caption
            box.Width = 120
            box.IsEnabled = enabled
            box.IsChecked = bool(value) and enabled
            box.VerticalAlignment = VerticalAlignment.Center
            boxes.append(box)
        row.Children.Add(text)
        row.Children.Add(boxes[0])
        row.Children.Add(boxes[1])
        panel.Children.Add(row)
        return boxes[0], boxes[1]

    def _tick(self, discipline, value):
        for key, box in self._cat_boxes.items():
            if BUILDING_BY_KEY[key].discipline == discipline:
                box.IsChecked = value

    def _message(self, text):
        MessageBox.Show(self, text, "Clash scope", MessageBoxButton.OK,
                        MessageBoxImage.Warning)

    def on_cancel(self, sender, args):
        self.Close()

    def on_run(self, sender, args):
        links = dict(self._scope.get("links", {}))   # keep unloaded links' choice
        for row, mep_cb, bld_cb in self._link_boxes:
            if row["loaded"]:
                links[row["key"]] = {"mep": bool(mep_cb.IsChecked),
                                     "building": bool(bld_cb.IsChecked)}
        scope = {
            "host_mep": bool(self._host_mep.IsChecked),
            "host_building": bool(self._host_bld.IsChecked),
            "links": links,
            "building_keys": [d.key for d in BUILDING_CATEGORIES
                              if self._cat_boxes[d.key].IsChecked],
            "mep_vs_mep": bool(self.mep_vs_mep.IsChecked),
        }
        loaded = [(r, m, b) for r, m, b in self._link_boxes if r["loaded"]]
        any_mep = scope["host_mep"] or any(m.IsChecked for _r, m, _b in loaded)
        any_bld = scope["host_building"] or any(b.IsChecked for _r, _m, b in loaded)
        if not any_mep:
            self._message("Tick MEP on at least one model (host or link).")
            return
        building_active = any_bld and bool(scope["building_keys"])
        if not scope["mep_vs_mep"] and not building_active:
            self._message("Nothing to check.\n\nEither tick 'MEP vs MEP', or tick "
                          "'Arch / Struct' on a model AND at least one ARC / STR "
                          "category.")
            return
        if any_bld and not scope["building_keys"]:
            self._message("'Arch / Struct' is ticked on a model but no ARC / STR "
                          "category is selected.")
            return
        if scope["building_keys"] and not any_bld:
            self._message("ARC / STR categories are selected but no model has "
                          "'Arch / Struct' ticked - tick it on the architecture / "
                          "structure link(s).")
            return
        self.result = scope
        self.Close()


def ask_scope(document, scope):
    """Show the scope dialog. Returns the new scope dict or None (cancelled)."""
    dialog = ScopeWindow(SCOPE_XAML_FILE, document.Title, list_links(document),
                         scope)
    dialog.ShowDialog()
    return dialog.result


# ---------------------------------------------------------------------------
# SECTION 15 - ENTRY POINT
# ---------------------------------------------------------------------------
def main():
    """Validation -> scope -> detection -> highlighting -> modeless UI."""
    active_view = validate_active_view()
    for xaml in (XAML_FILE, SCOPE_XAML_FILE):
        if not os.path.isfile(xaml):
            forms.alert("{0} was not found next to script.py."
                        .format(os.path.basename(xaml)), title="MEP Clash Detector")
            return

    # Second click while the window is open: re-use it for the same view,
    # replace it for a different view / model.
    existing = _get_open_window()
    if existing is not None:
        try:
            same = (existing.view_unique_id == active_view.UniqueId
                    and existing.doc_title == doc.Title)
            if same:
                if existing.WindowState == WindowState.Minimized:
                    existing.WindowState = WindowState.Normal
                existing.Activate()
                return
            existing.Close()
        except Exception:
            pass

    scope = ask_scope(doc, load_scope())
    if scope is None:
        return                         # user cancelled the scope dialog
    save_scope(scope)

    logger.info("MEP Clash Detector started on view '%s'", active_view.Name)
    try:
        clashes, stats = detect_clashes(active_view, scope)
    except DetectionCancelled:
        return

    highlighter = HighlightManager(doc, active_view)
    # Always call apply(): with no clashes it still resets colours left in
    # the view by a previous run.
    applied = highlighter.apply(clashes)
    logger.info("Applied graphic overrides to %s elements", applied)
    if not clashes:
        forms.alert(
            "No clashes were found in view '{0}'.\n\n"
            "Elements processed: {1} ({4} linked, {5} arch/struct)\n"
            "Models: {6}\nPairs tested: {2}\nTime: {3:.1f} s"
            .format(active_view.Name, stats["elements"], stats["pairs"],
                    stats["seconds"], stats.get("linked", 0),
                    stats.get("building", 0),
                    ", ".join(s[0] for s in stats.get("sources", [])) or "-"),
            title="MEP Clash Detector")
        return


    # ExternalEvent.Create must be called in an API context - i.e. here.
    handler = RevitActionHandler()
    ext_event = ExternalEvent.Create(handler)
    window = ClashResultsWindow(XAML_FILE, clashes, stats, active_view,
                                highlighter, handler, ext_event, scope)
    _register_window(window)
    window.Show()                     # modeless: the command returns now


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass                                    # raised by forms.alert(exitscript)
    except Exception as exc:                    # last line of defence
        logger.error("Unhandled error: %s", exc)
        output.print_md("### MEP Clash Detector - unhandled error")
        output.print_code(traceback.format_exc())
        forms.alert("The tool stopped with an error:\n\n{0}\n\n"
                    "See the pyRevit output window for the full traceback."
                    .format(exc), title="MEP Clash Detector")
